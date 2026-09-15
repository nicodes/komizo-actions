# komizo-actions

GitHub Actions that deploy to your own server. Merge, and it's live.

```yaml
- uses: nicodes/komizo-actions/deploy@v0.0.1
  env:
    KOMIZO_APP_NAME: ${{ vars.KOMIZO_APP_NAME }}
    KOMIZO_SERVER_URL: ${{ vars.KOMIZO_SERVER_URL }}
    KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}
    KOMIZO_KNOWN_HOSTS: ${{ vars.KOMIZO_KNOWN_HOSTS }}

    KOMIZO_SECRET_DATABASE_URL: ${{ secrets.DATABASE_URL }}
  with:
    version: ${{ github.sha }}
    config-compose: deploy/compose.yml
    config-image: ghcr.io/you/myapp-config
    registry-user: ${{ github.actor }}
    registry-token: ${{ secrets.GITHUB_TOKEN }}
    health-urls: |
      https://myapp.example.com/health
```

The first four are the names komizo tells you to store, and the ones these
actions look for. Passing them as `app:`, `host:`, `key:` and `known-hosts:`
still works and takes precedence — the environment is the default, not a
replacement.

Keeping the server's address out of the workflow is worth more than the line it
saves: moving an app to another box, or reaching a box by a different name while
its public DNS is mid-cutover, becomes a repository variable rather than a commit
and a deploy.

`KOMIZO_APP_NAME` is the one to hold loosely. The app name is also the name of
its gateway service in `compose.yml` — the shared proxy is pointed at
`<app>-gateway` — and it appears in its image references. Both are committed and
both must agree with it. Changing it in settings alone points the proxy at a
container that does not exist, and nothing reports that.

**Everything named `KOMIZO_SECRET_<NAME>` is pushed to the host as `<NAME>`.** A
composite action cannot read `secrets` itself — GitHub exposes that context to
workflows only — so a value has to arrive as an environment variable either way.
The prefix is what lets that env block be the only place a secret is named,
rather than a list of values and a second list agreeing with it. Renaming comes
free, since the left side is the name the host receives.

The host gets exactly what is written there and nothing else. Handing the action
`toJSON(secrets)` would be shorter still and is deliberately not how this works:
it would put every secret the job can see into the step's environment, including
the ones the app has no business holding.

That connects over SSH, publishes this commit's `compose.yml` as an image, sets
any secrets, makes the tag live, and polls until the app answers — failing the
job if it does not.

**[Full reference →](./docs/actions.md)**

## What these need

A server prepared by the `komizo` CLI. These actions are the CI half of a pair,
and they assume the other half is already in place:

- a deploy account that may run exactly two commands, neither of which is a shell
- a root-owned app directory that the deploy account cannot write to
- `compose.yml` arriving as a registry image rather than as a file copied in over
  SSH

Without that there is nothing on the far end for `deploy` to call. **The CLI is
not public yet** — if you have found this repo and want to use it, that is the
missing piece, and it is better said here than discovered from a failing job.

## Why it is split this way

The deploy key these actions hold is the least privileged thing in the system.
It can deploy a tag that already exists in your registry, and it can overwrite a
secret it is not allowed to read back. It cannot run Docker, write to the app
directory, or introduce code of its own.

That boundary is the point, and it is why `compose.yml` travels as a registry
image: whoever can change `compose.yml` can mount the host filesystem into a
container, which is the same as being root. So changing it requires registry
push, not merely holding the deploy key.

A leaked deploy key lets an attacker roll your stack back to a tag you have
already published, and overwrite secrets. It does not let them run code of their
own. Protect registry push accordingly, and pin third-party actions by SHA —
they run in the same job as the deploy key.

## The actions

Most workflows need only `deploy`, which composes the rest in the right order.

| Action | Does |
| --- | --- |
| [`deploy`](./deploy) | Everything below, correctly sequenced |
| [`connect`](./connect) | Installs the key and the pinned host key |
| [`publish-config`](./publish-config) | Ships `compose.yml` and the hostname list as an image |
| [`set-secrets`](./set-secrets) | Writes secrets the host cannot read back |
| [`activate`](./activate) | Runs the deploy on the host — the step that changes what is running |
| [`health-check`](./health-check) | Polls a URL until it answers |
| [`run-task`](./run-task) | Invokes one app-defined, host-allowlisted production task after `connect` |

Reach for the primitives when you need your own steps interleaved — a database
backup before the deploy, or a migration between the config publish and the
restart.

`activate` was briefly called `set-version`, and `health-check` was
`healthcheck`. There are no forwarding shims for the old names: nothing has
been released under them, and a shim is a second definition of an action's
defaults that changes every omitted input the moment the two disagree.

## Pinning

**Every release is its own tag, and no tag ever moves.** `@v0.0.1` names one
commit for ever, so upgrading is a visible edit in a pull request and rolling
back is naming the version before it.

There used to be a single `v0` that each release force-moved. That made `@v0` a
mutable ref: you could not tell which six files you were running, and a bad
release reached every repository the moment the tag moved. It is gone.

**`0.x` means there is no compatibility promise yet** — inputs may still be
renamed or removed between releases. A `v1` will appear once the input surface
has held still and the CLI half is public. Read the release notes before
bumping.

A commit SHA is stronger still, because a tag can in principle be deleted and
recreated where a commit cannot:

```yaml
- uses: nicodes/komizo-actions/deploy@<sha>
```

That pin is complete: the five actions `deploy` composes are rewritten to a SHA
at release time, so they cannot float out from under it. The reasoning — and the
one thing to know if you work on this repo — is in
[the reference](./docs/actions.md#pinning).
