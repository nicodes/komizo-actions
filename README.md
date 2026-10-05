<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 388b3beb27c3796fb535a773fcb68a7f9aaceebafd594df311f6a3b2bf00aa58 -->

# komizo-actions

Reusable GitHub Actions for deploying Compose applications to hosts prepared by the Komizo CLI. The supported action invokes the app-scoped host deployment operation.

The abandoned rollout model is superseded by v0.0.7 and later. Historical tags are not a recommendation to adopt the abandoned model.

## Deploy

Pin a reviewed full commit SHA. Configure a deploy account, pinned SSH host keys and an app-scoped host allowlist before running this action.

```yaml
- uses: nicodes/komizo-actions/deploy@8558c0494f00efd97d8053db43a0ffaa826da00d
  with:
    app: example
    host: ${{ vars.DEPLOY_HOST }}
    key: ${{ secrets.DEPLOY_KEY }}
    known-hosts: ${{ vars.DEPLOY_KNOWN_HOSTS }}
    version: ${{ github.sha }}
    config-compose: deploy/compose.yml
    config-image: ghcr.io/example/app-config
    registry-user: ${{ github.actor }}
    registry-token: ${{ secrets.GITHUB_TOKEN }}
    health-urls: https://app.example.com/health
```

Use `KOMIZO_SECRET_<NAME>` environment variables only for secrets this application needs. Do not forward every available secret. Host-local profile values must stay on the host; the action sends only a nonsecret generation id. A failed activation or healthcheck stays failed and does not prove database rollback.

See each action’s `action.yml` for its current input contract. Release refs are fixed by project policy; a full commit SHA gives an immutable action pin.

## Host-local environment profiles

The action does not prove that a fresh PostgreSQL cutover is safe. Use a separate host-local status command, an exact expected generation and the supported profile. Remote output is captured in mode 0600 files. The validated receipt is `deploy: scoped-generation=<expected generation>`. There is no stage, confirm, or abort. Profile-specific credentials remain on the host and must not be sent by the runner.

## Host-local environment profiles

The action does not prove that a fresh PostgreSQL cutover is safe. Use a separate host-local status command, an exact expected generation and the supported profile. Remote output is captured in mode 0600 files. The validated receipt is `deploy: scoped-generation=<expected generation>`. There is no stage, confirm, or abort. Profile-specific credentials remain on the host and must not be sent by the runner.
