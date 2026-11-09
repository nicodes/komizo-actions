<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 48698d3a6522a5a12a99054bdb67ecb48b3f1b95bbb45372d768334d845dd0d7 -->

# Komizo Actions

GitHub adapters for immutable application releases, protected host submission, previews, readiness checks and encrypted backup transport. Pin actions and reusable workflows to reviewed full commits. Host policy independently authorizes each application, workflow and action.

## Source and release preparation

Recipe-aware adapters require an exact `cookbook-ref`. Cookbook selects its reviewed Tools dependency; callers cannot substitute another helper revision. Trusted preparation verifies repository origin, source identity and provider ancestry before preparing the caller's SDKs.

Applications declare native workloads in `deploy/application.toml`. Complete source validation freezes a closed release packet containing `release.json`, `inventory.json`, selected image OCI archives, selected static archives and explicitly inventoried metadata. Static applications need no image. Publication uploads exact accepted OCI manifests and blobs without rebuilding artifacts or changing the descriptor.

`application-publication` consumes complete same-run evidence and emits an immutable six-record host release artifact. Queued host effects run in the pinned `cookbook-deliver.yml` workflow. Its final authorizer checks publication, complete validation, source, current main or pull request and pinned providers immediately before minting the release proof. Source validation, publication and installed application acceptance remain distinct evidence. Delivery retains a small typed operation-metadata artifact separately from encrypted backup files. Historical dependency audits require an exact successful ready-operation receipt and report `activation_at_time` with `current_state_unobserved`. A staged stopped release cannot satisfy that scope; current host state requires independently authenticated inspection.

## Host submission

`connect` accepts `host`, `key`, `known-hosts` and optional `port`, and returns `ssh-config`. It prepares strict host pins and a private per-job SSH configuration for the restricted `komizo-submit` account. Its post action removes owned credentials. The first remote connection submits the authenticated request.

`deploy` accepts `application`, optional `instance` (default `production`), `release`, `release-proof`, `timeout-seconds`, `expected-secret-generations` as a JSON object, and optional `registry-user` and `registry-token`. Preview deployment also requires its approved `ttl-seconds`. Outputs identify the admitted operation, release digest and phase. `proposed-operation-id` is computed before transport; an uncertain connection does not confirm admission. Inspect that identity with fresh authority before another mutation. Credentials travel only inside the bounded SSH submission body. Ambiguous acknowledgments require operation inspection before another deployment.

`run-task` accepts `application`, `instance`, `action`, `release-digest`, optional `operation`, and `timeout-seconds`. Actions are `start`, `stop`, `restart`, `inspect`, `backup` and `remove`. The `current` selector is valid only for inspection; `absent` is valid only for an authoritative first-backup absence check. Stop, restart and backup select the exact installed release. Start selects the exact prepared release when inspection reports a stopped candidate; otherwise it selects the installed release. This interface executes no arbitrary shell command.

Routine CI supplies logical secret references and optional expected generations. An owner manages private host grants and secret generations. Serving services receive only their granted references; migration authority belongs to explicit jobs.

## Previews and product checks

Preview workflows separate trusted request planning, selected-source validation, immutable publication and protected effects. The host supplies public planned origins before the descriptor is compiled and tested. Preview descriptors bind those origins, an isolated instance graph and its lifetime before acceptance. Cleanup uses the same application authority and recorded resource ownership.

`application-readiness`, `health-check`, `godot-production` and reusable production workflows retain caller-owned assertions. Paid checks require explicit default-off dispatch, their protected environment and declared budgets. Credentials enter only the selected execution step.

Source and security audit workflows retain declared runners, deadlines, native assertions and reporting permissions. `recipe-setup` prepares the exact provider identities. `application-delivery` prepares a read-only delivery plan; the protected reusable workflow performs host effects.

## Backup and recovery

`application-backup-export` submits the protected backup operation and receives only encrypted `snapshot.cms` and `receipt.json`. Ready backup metadata identifies the captured installed release separately from the operation target. Routine backup requires that captured identity to equal its requested installed release. Export metadata, exact byte counts and hashes are checked before the pair is retained atomically. `application-backup-upload` transports that immutable pair. Absence evidence requires the host to check retained records and actual instance resources.

`recovery-evidence` verifies exact completed run and artifact identities. Routine transport receives no application runtime values or private decryption key. Source restore tests use synthetic data and exact release fixtures. An explicit owner restore uses its private local `BACKUP_RECOVERY_KEY_FILE`, the snapshot's installed release identities and the application's database, media, API and frontend assertions. Installed snapshot acceptance does not issue a current-source validation receipt. Production recovery remains a separately authorized owner operation.

## Contribution

Build and test recipes live in [Cookbook](https://github.com/nicodes/cookbook); reusable native utilities live in [Tools](https://github.com/nicodes/tools). Prepare the checked-in SDKs and provider sources with Mise, then run the repository's native checks. Focused checks exercise canonical callers, exact OCI publication, authenticated requests, transport cleanup, atomic backup receipt handling and workflow authority.
