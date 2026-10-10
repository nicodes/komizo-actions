<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 8a86aa94faceeb6a1d07680d3734e9d3e20fd0f9175efd81b3f2afc4724211f2 -->

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

## Pull request previews

Previews are opt-in. `preview-request` decides, before any secret enters the workflow, whether a run is a preview request; `preview` brings the stack up or down on the host.

The rule, applied inside the jobs (the workflow keeps the full `pull_request` type list the fleet workflow contract requires and adds `issue_comment: [created]`):

- `pull_request` `closed` tears the preview down, always; teardown is idempotent.
- `pull_request` `synchronize` or `reopened` redeploys only while the PR carries the `preview` label.
- `pull_request` `opened` and `ready_for_review` do nothing.
- A comment whose exact body is `/preview` deploys; `/preview down` tears down. Only comments by an OWNER, MEMBER or COLLABORATOR count, and only on pull requests.
- Every deploy requires a same-repository head (never a fork), a trusted author, and an open, non-draft PR. Dependabot pull requests are refused both ways: never deployed, and a run dependabot triggers is served the Dependabot secret store, so a teardown would run with an empty deploy key.

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review, closed]
  issue_comment:
    types: [created]
jobs:
  request:
    if: github.event_name != 'issue_comment' || github.event.issue.pull_request != null
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: read
    outputs:
      enabled: ${{ steps.request.outputs.enabled }}
      action: ${{ steps.request.outputs.action }}
      pr: ${{ steps.request.outputs.pr }}
      sha: ${{ steps.request.outputs.sha }}
    steps:
      - id: request
        uses: nicodes/komizo-actions/preview-request@<full commit sha>
        with:
          label: preview
  preview-up:
    needs: request
    if: needs.request.outputs.enabled == 'true' && needs.request.outputs.action == 'up'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
      packages: read
    steps:
      - uses: actions/checkout@<full commit sha>
        with:
          ref: ${{ needs.request.outputs.sha }}
      - uses: nicodes/komizo-actions/preview@<full commit sha>
        with:
          app: example
          pr-number: ${{ needs.request.outputs.pr }}
          images: ghcr.io/example/app-gate:${{ needs.request.outputs.sha }}
          action: up
          registry-user: ${{ github.actor }}
          registry-token: ${{ secrets.GITHUB_TOKEN }}
      - run: gh pr edit "$PR" --add-label preview
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
          PR: ${{ needs.request.outputs.pr }}
```

A `preview-down` job gates on `needs.request.outputs.action == 'down'`, runs the same composite with `action: down`, then `gh pr edit "$PR" --remove-label preview` with the same `GH_TOKEN`, `GH_REPO` and `PR` env: `gh` otherwise resolves the repository from a git remote, and a teardown job has no checkout.

Use the resolved `pr` and `sha` outputs rather than `github.event.pull_request.*`: a comment event carries no pull request payload. The workflow adds the label after a successful `up` and removes it after `down`; the action only reads it. A label left behind by host-side garbage collection costs one redeploy on the next push, which recreates the state. The `preview` label must exist in the repository (`gh label create preview`), and the job that flips it needs `pull-requests: write`.

## Host-local environment profiles

The action does not prove that a fresh PostgreSQL cutover is safe. Use a separate host-local status command, an exact expected generation and the supported profile. Remote output is captured in mode 0600 files. The validated receipt is `deploy: scoped-generation=<expected generation>`. There is no stage, confirm, or abort. Profile-specific credentials remain on the host and must not be sent by the runner.

`release-evidence` prepares a manifest from the exact tested publication or verified image transfer. Pass its output to `deploy` or `activate` as `release-manifest` and grant only the deployment job `id-token: write`. The action requests a short-lived GitHub identity whose audience hashes the exact manifest. The host independently checks its approved source identity and image IDs before activation. No permanent signing key is needed. Verified deployment requires a host version that supports the release-proof protocol and an operator-approved source policy.

Evidence kinds are `publication` (matching Build and Test receipts from the current run), `transferred-images` (loaded images matching the verified transfer), `gate-artifact` (gate ID emitted by the successful verification job), and `same-job-gate` (only after this workflow has successfully tested its exact local gate). Gate-only workflows generate the config image after gate verification; the host validates that configuration separately. Previously accepted current or previous revisions use the host's protected rollback receipt instead of requesting an identity for a different source.
