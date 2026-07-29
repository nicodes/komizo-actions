# Actions reference

Every action, every input, every output. For a guided introduction see the
[README](../README.md).

These are the CI half of komizo. They deploy to a server that has already been
set up to receive deploys — see [what these need](../README.md#what-these-need)
in the README. They do not set a server up.

Referenced directly — no checkout of this repo needed, and nothing to configure,
because this repository is public:

```yaml
- uses: nicodes/komizo-actions/connect@v0
```

## Pinning

**`v0` is deliberate: there is no compatibility promise yet.** Inputs may be
renamed or removed between releases — `app` became required and `config-env` was
removed inside the last month, and pretending otherwise by calling it `v1` would
be a promise that cannot currently be kept. A `v1` tag will appear when the input
surface has held still and the CLI half is public.

So pin by commit if a change under you would hurt:

```yaml
- uses: nicodes/komizo-actions/deploy@<sha>
```

That pin is complete. `deploy` is a composite that calls five sibling actions,
and GitHub resolves the caller's ref for `deploy/action.yml` alone — each inner
`uses:` resolves independently, at whatever ref is written in the file. So the
inner refs are rewritten to a commit SHA at release time by
[`scripts/release.sh`](../scripts/release.sh). Without that, pinning `deploy` by
SHA would leave the five steps it composes floating on the tag: a pin that looks
airtight and is not, which is worse than no pin at all.

There is no way to inherit the caller's ref — `uses:` must be a string literal
and accepts no `${{ }}` expressions, so `github.action_ref` cannot be forwarded.
Rewriting at release is the only thing that makes the pin mean what it says.

One consequence worth knowing if you work on this repo: between releases,
`deploy@main` runs the sub-actions from the *last release*, not from `main`.

---

There are two layers. **Most workflows need exactly one action:**

| Action | Does |
| --- | --- |
| [`deploy`](#deploy) | Connect, publish config, set secrets, make the tag live, health check — in the correct order |

Everything but `version` is optional, so it scales down: no config directory, no
secrets, no health check, all just omitted.

Underneath are five primitives. **Reach for them when you need your own steps
interleaved** — a database backup before the deploy, a migration between the
config publish and the restart:

| Action | Does | Needs |
| --- | --- | --- |
| [`connect`](#connect) | Installs the key + pinned host key, defines `deploy-target` | — |
| [`publish-config`](#publish-config) | Publishes `compose.yml` as an image | registry login |
| [`set-secrets`](#set-secrets) | Writes secrets the host can't read back | `connect` |
| [`activate`](#activate) | Runs the deploy on the host — the step that changes what is running | `connect` |
| [`health-check`](#health-check) | Polls a URL until it answers | — |

The two layers mix: run `connect` yourself, do whatever you need over
`ssh deploy-target`, then call `deploy` **without** `host` and it will use the
connection you already made.

## Contents

- [`deploy`](#deploy) — the whole sequence in one step
- [The `deploy-target` seam](#the-deploy-target-seam) — running your own commands on the host
- [`connect`](#connect) · [`publish-config`](#publish-config) · [`set-secrets`](#set-secrets) · [`activate`](#activate) · [`health-check`](#health-check) — the primitives, in execution order

## `deploy`

The everyday deploy. Publishes this commit's config, sets secrets, deploys the
tag — in the one order that is correct.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `version` | yes | — | Tag to deploy, normally the commit SHA. |
| `app` | no | `KOMIZO_APP_NAME` | Which app — selects the account `komizo-<app>` and the commands `deploy-<app>` and `set-secret-<app>`. Matches the name you gave it in `komizo`. Required in one form or the other. |
| `config-compose` | no | `""` | Path to the compose file to publish for this commit. Empty skips the config publish. |
| `config-hostnames` | no | `""` | File listing the hostnames this app answers on, one per line. The host generates the reverse-proxy config from it. |
| `config-image` | no | `""` | Config image reference **without** a tag. Required with `config-compose`. |
| `secrets` | no | `""` | Rarely needed — names for values passed as plain env vars. Normally the `KOMIZO_SECRET_*` env vars are the list. |
| `registry` | no | `ghcr.io` | Registry the host authenticates against. Empty to skip. |
| `registry-user` | no | `""` | Registry username. |
| `registry-token` | no | `""` | Registry password. Prefer the run-scoped `GITHUB_TOKEN`. |
| `host` | no | `$KOMIZO_SERVER_URL` | Server hostname. Supplying it makes this action connect for you; leave empty if `connect` already ran in the job. |
| `user` | no | `komizo-<app>` | Deploy account. Only needed if you overrode it. |
| `key` | no | `""` | Private half of the deploy key. Required when `host` is set. |
| `known-hosts` | no | `""` | Pinned host keys. Required when `host` is set. |
| `port` | no | `22` | SSH port. |
| `health-url` | no | `""` | URL to poll after the deploy. Empty skips the check. |

```yaml
- uses: nicodes/komizo-actions/deploy@v0
  env:
    KOMIZO_APP_NAME: ${{ vars.KOMIZO_APP_NAME }}
    KOMIZO_SERVER_URL: ${{ vars.KOMIZO_SERVER_URL }}
    KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}
    KOMIZO_KNOWN_HOSTS: ${{ vars.KOMIZO_KNOWN_HOSTS }}

    KOMIZO_SECRET_DATABASE_URL: ${{ secrets.DATABASE_URL }}
    KOMIZO_SECRET_APP_ADMIN_PASSWORD: ${{ secrets.PB_ADMIN_PASSWORD }}
  with:
    version: ${{ github.sha }}
    config-compose: deploy/compose.yml
    config-hostnames: deploy/hostnames
    config-image: ghcr.io/you/myapp-config
    registry-user: ${{ github.actor }}
    registry-token: ${{ secrets.GITHUB_TOKEN }}
```

**Outputs**

| Output | Description |
| --- | --- |
| `previous-version` | The version that was live before this ran. Empty on a first deploy. |
| `version` | The version deployed. Echoes the input, for chaining. |
| `config-image-ref` | Full reference of the config image published, *including* the tag. |
| `health-attempts-used` | Attempts the health check needed, if it ran. |

`previous-version` is the one that buys you something new: capture it, and a
failing health check can redeploy it. Because it is read off the host *before*
anything changes, it is still correct when a later step fails — which is exactly
when you want it.

```yaml
- id: deploy
  uses: nicodes/komizo-actions/deploy@v0
  with: { app: myapp, version: "${{ github.sha }}", health-url: "https://myapp.example.com/health" }

- name: Roll back
  if: failure() && steps.deploy.outputs.previous-version != ''
  uses: nicodes/komizo-actions/activate@v0
  with:
    version: ${{ steps.deploy.outputs.previous-version }}
    command: doas /usr/local/bin/deploy-myapp
```

**Why a wrapper exists at all.** Two of the orderings it enforces fail
*silently* when wired by hand:

- **Secrets after the deploy.** Containers read their environment at start, and
  the deploy is what restarts them. A secret set afterwards sits on disk unused
  until the next deploy — so it appears to work, one deploy late.
- **Config published after the deploy.** The host would pull the *previous*
  commit's `compose.yml` and report success.

Both are invisible in a green run. `deploy` makes them unrepresentable.

It validates everything up front — the `config-compose`/`config-image` pair,
and that every secret resolves to a **non-empty** value — so a misconfiguration
fails before anything is published or restarted.

Non-empty matters as much as present: GitHub substitutes an empty string for a
secret that does not exist, so a name declared in the workflow and never created
in the repository is indistinguishable from one that was supplied, to any check
that only asks whether the name is set. Unchecked, that deploys an app
configured with an empty password from a green pipeline.

## The `deploy-target` seam

`connect` leaves an SSH alias behind rather than performing an action, so any
step after it can run `ssh deploy-target ...` directly. That's the hook for
things this toolkit doesn't cover — a pre-deploy database backup, a post-deploy
migration, a one-off diagnostic. It works whether you use `deploy` or the
granular actions:

```yaml
- uses: nicodes/komizo-actions/connect@v0
  with: { host: ..., user: komizo-blog, key: ..., known-hosts: ... }

- name: Back up the database first
  run: ssh deploy-target 'docker compose -f /srv/<app>/compose.yml exec -T db pg_dump -U app app' > dump.sql

- uses: nicodes/komizo-actions/deploy@v0
  with: { app: blog, version: "${{ github.sha }}" }
```

## `connect`

Installs the deploy key and a pinned host key, then exposes the server as the
SSH alias **`deploy-target`**. Every later step just runs `ssh deploy-target`.
Verifies connectivity before finishing, so auth problems fail here with a clear
message instead of midway through a deploy.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `host` | no | `$KOMIZO_SERVER_URL` | Hostname or IP. Pass it, or set `KOMIZO_SERVER_URL` under `env:` and leave this out. |
| `user` | yes | — | Deploy account, `komizo-<app>` unless you overrode it. |
| `key` | no | `$KOMIZO_DEPLOY_KEY` | Private deploy key. Pass a secret, or set `KOMIZO_DEPLOY_KEY` under `env:` and leave this out. |
| `known-hosts` | no | `$KOMIZO_KNOWN_HOSTS` | Standard `known_hosts` lines, one per name per key — what `komizo` copies. Required unless `allow-unpinned-host` is true. |
| `port` | no | `22` | SSH port. |
| `allow-unpinned-host` | no | `false` | Discover the host key at deploy time instead of pinning it. |

```yaml
- uses: nicodes/komizo-actions/connect@v0
  env:
    KOMIZO_SERVER_URL: ${{ vars.KOMIZO_SERVER_URL }}
    KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}
    KOMIZO_KNOWN_HOSTS: ${{ vars.KOMIZO_KNOWN_HOSTS }}
  with:
    user: komizo-blog
```

Those three environment variables are the names komizo stores under, and what
both actions fall back to. A composite action cannot read `secrets` itself —
GitHub gives that context to workflows only — so the fallback is an env var set
once on the job or the step, rather than an input repeated on each one. An
explicit `key:` or `known-hosts:` still wins.

> `allow-unpinned-host: true` is trust-on-first-use on *every* run: anyone who
> can influence DNS or routing from the runner collects whatever the job sends.
> Opt in only for throwaway hosts.

**Outputs:** `host` — the host connected to, echoing the input.

Re-running `connect` is safe: it rewrites its `~/.ssh/config` block rather than
appending, so a second run replaces the first instead of racing it.

## `publish-config`

Publishes this commit's config as `<image>:<tag>`, which the host unpacks into
`/srv/<app>` at deploy time.

Each file is **named explicitly**, and only named files are published. There is
no directory sweep, so a stray key or a second environment's config sitting
next to your compose file cannot ride along into a registry.

Two files, and the second one is a list of names.

**`hostnames` is the whole of what an app tells the server about routing.** One
name per line, `#` comments allowed. The host generates the reverse-proxy config
from it and points every one of those names at your app's gateway container.

That is the entire contract. An app cannot author server config, so no app's
mistake can be another app's outage — which is what the previous arrangement
allowed, since every app's Caddy fragment was concatenated into one config
loaded by one process, and Caddy accepts exactly one global options block per
server.

Everything a request meets after the hostname match belongs to your gateway:
paths, headers, static files, which service answers what. That is a whole config
in an image you built and validated, and it runs anywhere — point any TLS
terminator at `:80` and your app serves.

Static files go in that image too. There is no longer a `public/` directory in
the config image and nothing is served off the host's disk.

A line may say which container serves the name — `api.example.com -> api`. Only
the name is validated and only the name is routed on; the rest is a label for
the interface, so a wrong arrow mislabels a chart and cannot misroute a request.
It exists because nothing on the server can work it out: the shared proxy only
ever talks to your gateway, and which container answers is decided inside it.

Names are validated here as well as on the host: letters, digits, dot and
hyphen, with an optional leading `*.`. The host has to refuse a bad one because
it writes them into a config the whole box loads — but finding out in CI, before
anything is published, is cheaper than finding out from a deploy that reverted.

No Dockerfile needed in your repo — the action stages the files under their
canonical names and generates `FROM scratch` + `COPY . /config`.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `compose` | yes | — | Path to the compose file. Published as `/config/compose.yml`, so the name on disk is yours to choose. |
| `hostnames` | no | `""` | File listing the hostnames this app answers on, one per line. Published as `/config/hostnames`. See above. |
| `image` | yes | — | Image reference **without** a tag. Must match the host's `CONFIG_IMAGE`. |
| `tag` | yes | — | Tag to publish, normally the commit SHA. |

```yaml
- uses: nicodes/komizo-actions/publish-config@v0
  with:
    compose: deploy/compose.yml
    hostnames: deploy/hostnames
    image: ghcr.io/you/myapp-config
    tag: ${{ github.sha }}
```

Because the paths are yours, per-environment layouts need no extra machinery:

```yaml
    compose: deploy/compose.prod.yml
    env: deploy/config.prod.env
```

Assumes you have already authenticated to the registry. Publishes no `:latest`
— a floating tag here would be a way to change the stack without a deploy.

Everything published is readable by anyone who can pull the image, so keep
secrets out of all three. That is what `set-secrets` is for.

**Outputs:** `image-ref` — the full pushed reference *including* the tag,
unlike the `image` input, which must not carry one.

## `set-secrets`

Pushes secret values into the host's write-only store. Requires `connect`.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `names` | no | `""` | Rarely needed — names for values passed as plain env vars under their own names. Normally the `KOMIZO_SECRET_*` env vars are the list. |
| `command` | yes | — | Privileged secret command on the host, e.g. `doas /usr/local/bin/set-secret-blog`. |

```yaml
- uses: nicodes/komizo-actions/set-secrets@v0
  env:
    KOMIZO_SECRET_DATABASE_URL: ${{ secrets.DATABASE_URL }}
    KOMIZO_SECRET_STRIPE_KEY: ${{ secrets.STRIPE_KEY }}
  with:
    command: doas /usr/local/bin/set-secret-myapp
```

**Values come from `env:`, not `with:`.** An action input is recorded in the
workflow run and readable through the API; an environment variable is not. The
value then travels over stdin rather than argv, so it never appears in the
host's process list either.

**`KOMIZO_SECRET_<NAME>` is pushed as `<NAME>`,** and the env block is therefore
the whole declaration — which secrets exist and which the host gets, in one
place, with no second list to keep in agreement with it. The left side is the
name the host receives, so `KOMIZO_SECRET_APP_ADMIN_PASSWORD:
${{ secrets.PB_ADMIN_PASSWORD }}` renames in passing, and one repository secret
can land under two names the app expects.

Only prefixed variables are swept up. `KOMIZO_DEPLOY_KEY`, `GITHUB_TOKEN` and
everything else in the job's environment stay out of it — which is the point,
and the reason this is not `toJSON(secrets)`.

**Empty is an error.** GitHub substitutes an empty string for a secret that does
not exist, so a name declared in the workflow and never created in the
repository looks exactly like one that was supplied. Every value is resolved and
checked before any of them is sent, so a run that is going to fail writes
nothing: half a set of secrets on the host is worse than none, because the
deploy that follows would start containers on a mixture of this commit's values
and the last one's.

Names are validated against `[A-Za-z0-9_]`, and a value containing a newline is
rejected by the host — an env file cannot represent one.

Writing a secret does not restart anything. Run this *before* `activate`
if the new value must take effect immediately; Compose picks it up when the
container is recreated.

**Outputs:** `count` — how many secrets were set.

## `activate`

Runs the deploy on the host: pulls the config image for this tag, unpacks it,
writes `APP_VERSION`, and brings the stack up. **This is the step that changes
what is running** — everything before it publishes and prepares. Compose
recreates only what the new config actually changed, so services whose resolved
config is unaffected keep running untouched. Requires `connect` to have run
first.

Called `set-version` until it was renamed: the old name described the smallest
thing it does rather than the thing it is. There is no shim for the old name.

Most workflows should use [`deploy`](#deploy), which runs this *after*
publishing config and setting secrets. Use this directly only when you need
your own steps in between.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `version` | yes | — | Image tag to deploy, normally a commit SHA. |
| `registry` | no | `ghcr.io` | Registry to authenticate against before pulling. Empty to skip. |
| `registry-user` | no | `""` | Registry username. |
| `registry-token` | no | `""` | Registry password. Prefer the run-scoped `GITHUB_TOKEN`. |
| `command` | yes | — | Privileged deploy command on the host, e.g. `doas /usr/local/bin/deploy-blog`. |

```yaml
- uses: nicodes/komizo-actions/activate@v0
  with:
    command: doas /usr/local/bin/deploy-myapp
    version: ${{ github.sha }}
    registry-user: ${{ github.actor }}
    registry-token: ${{ secrets.GITHUB_TOKEN }}
```

Registry credentials go to the deploy command itself, on stdin, rather than to a
separate `docker login` over the deploy user's session. They have to: the pull
runs as **root**, so it reads root's `~/.docker/config.json`, and a login as the
deploy user would write to a different file — leaving the pull anonymous. The
host drops the credentials when the deploy exits, however it exits.

Prints rollback instructions on failure. If you ran `komizo add` with a custom `--app-dir`,
the command is unchanged — the path is baked into the generated script, not
passed in. Same for `CONFIG_IMAGE`: the host knows where its config comes from,
so the action never has to name it.

**Outputs**

| Output | Description |
| --- | --- |
| `previous-version` | The version live before this ran, as reported by the host. Empty on a first deploy. |
| `version` | The version deployed. Echoes the input. |

The host reports `previous-version` because the deploy user cannot read
`/srv/<app>/.env` itself — it is `600` root. The deploy script prints it before
changing anything, so it stays correct even when a later stage fails.

## `health-check`

Polls a URL until it answers, failing the job if it never does. Replaces the
hand-rolled retry loop every repo grows its own version of.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `url` | yes | — | URL to poll. |
| `expect` | no | `""` | Substring the body must contain. Empty means any 2xx counts. |
| `attempts` | no | `30` | Tries before giving up. |
| `delay` | no | `5` | Seconds between attempts. |
| `timeout` | no | `10` | Seconds before a single request is considered failed. |

```yaml
- uses: nicodes/komizo-actions/health-check@v0
  with:
    url: https://app.example.com/healthz
    expect: '"status":"ok"'
    attempts: "60"
```

Defaults give up after about 150 seconds. On failure it prints the last
response body — which is why it doesn't use `curl -f`, since that would discard
it.

Match on `expect` only when the endpoint can return 200 while unhealthy.

**Outputs:** `attempts-used` — how many attempts it took to answer, empty if
it never did. Distinct from the `attempts` input, which is the ceiling.

---
