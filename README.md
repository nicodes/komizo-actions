# komizo-actions

GitHub Actions that deploy to your own server. Merge, and it's live.

```yaml
- uses: nicodes/komizo-actions/deploy@v0
  with:
    app: myapp
    version: ${{ github.sha }}
    host: myapp.example.com
    key: ${{ secrets.SSH_DEPLOY_KEY }}
    known-hosts: ${{ vars.SSH_KNOWN_HOSTS }}
    config-compose: deploy/compose.yml
    config-image: ghcr.io/you/myapp-config
    registry-user: ${{ github.actor }}
    registry-token: ${{ secrets.GITHUB_TOKEN }}
    health-url: https://myapp.example.com/health
```

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
| [`publish-config`](./publish-config) | Ships `compose.yml` as an image |
| [`set-secrets`](./set-secrets) | Writes secrets the host cannot read back |
| [`set-version`](./set-version) | Makes one tag the live version |
| [`healthcheck`](./healthcheck) | Polls a URL until it answers |

Reach for the primitives when you need your own steps interleaved — a database
backup before the deploy, or a migration between the config publish and the
restart.

## Pinning

**`v0` means there is no compatibility promise yet.** The tag moves, and inputs
may still be renamed or removed between releases. A `v1` will appear once the
input surface has held still and the CLI half is public.

To pin a release so nothing changes under you, use a commit SHA:

```yaml
- uses: nicodes/komizo-actions/deploy@<sha>
```

That pin is complete: the five actions `deploy` composes are rewritten to a SHA
at release time, so they cannot float out from under it. The reasoning — and the
one thing to know if you work on this repo — is in
[the reference](./docs/actions.md#pinning).
