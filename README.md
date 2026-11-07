<!-- Generated from private documentation source. Do not edit directly. Source SHA256: bfe668d697dfba471f679bf4a49b3fce89ad74eac075069499ccef179da49f00 -->

# komizo Actions

Optional GitHub adapters for Komizo deployment, scoped connectivity, previews, release evidence and encrypted backup transport. Pin actions and reusable workflows to reviewed full commits. Callers supply their own images, targets and authorized host policy; the host independently enforces admission.

Authored delivery, backup, recovery and publication declarations use TOML, including `deploy/komizo.toml`. Configuration publication emits native Compose YAML and hostnames; generated release and acceptance receipts remain JSON. Independently pinned historical cookbook adapters retain their original input protocol.

## Accepted recipe artifacts

Generated recipe callers compose read-only source validation with separate publication and host-effect jobs. `cookbook-publish.yml` and `application-publication` consume exact same-run full Make artifacts. They verify the successful validation job, source/tree, cookbook, complete native coverage and closed image archive before publishing. Publication executes no application setup or build program. Nonshipping fixture images remain outside the shipping publication set.

Recipe-aware adapters read effective settings through the caller's already prepared, pinned cookbook. Shared CI defaults, literal native test groups and mandatory static metadata use that SDK's decoder and capability definitions. Raw configuration hashes and the complete accepted artifact scope remain bound to the original source.

`application-delivery` reads the tracked native declaration, refuses obsolete main before host effects, captures a declared encrypted backup, activates the accepted immutable manifest and runs explicit health assertions. Secret mappings remain visible in the caller. Production retains its authenticated root `cd.yml`, protected environment, serialization and declared total deadline.

Static Godot delivery uses `godot-static-delivery` and its own constrained gate/configuration protocol. The trusted publisher substitutes only a verified immutable image digest into the accepted configuration blueprint. Existing release records, historical recovery and fixed-origin public verification remain explicit capabilities. Historical image verification does not issue a current-source full-validation receipt.

Preview automation separates trusted request decisions, read-only validation of the selected source, accepted-artifact publication and protected host effects. Cleanup has its own path. Product source checks receive no publication, host or provider credentials.

## Explicit operations

The cookbook operational workflows read bounded project-owned declarations. They retain the caller’s real runners, job and step deadlines, protected environments, schedules, evidence policy and native assertions. `cookbook-source-audit.yml` handles declared source/artifact checks; `cookbook-security-audit.yml` additionally selects an actual deployed-image policy. Reporting and deployed-registry permissions use separate workflow variants when those capabilities are absent. Artifact validation declares setup, build, security and selected assertion phases explicitly; a source-only scan does not hide a shipping build or claim complete Make acceptance.

Production checks run genuine caller-owned assertions. Paid checks require explicit default-off dispatch, trusted current main, their original protected environment and deadline, and any declared successful-deployment prerequisite. An inline paid assertion shares its original production job’s total budget. Provider credentials enter only the selected paid execution step.

`routine-backup` prepares an audience-bound OIDC operation frame, invokes a fixed declared exporter, validates ciphertext, uploads the immutable pair and removes its private authority file. Only the backup job receives its required OIDC scope and scoped host/storage secrets. First-deployment absence proofs are explicit data. Capture, schema and data assertions remain application-owned. The older `reusable-backup.yml` interface continues to call its caller-owned export script for independently pinned consumers.

`recovery-evidence` verifies exact completed run and artifact IDs without decrypting or restoring application data. The optional legacy retention workflow checks the existing encrypted AGE pair and checksum before retaining it; it never restores data. `backup-proof` requires Python 3 and a new absolute output file under a protected caller-owned directory.

The shared `scripts/backup.py` has export/upload and explicit local restore entrypoints. A named restore profile selects tracked native configuration. Local restoration requires an absolute private `BACKUP_RECOVERY_KEY_FILE`, packages only `snapshot.cms` and `receipt.json`, and validates the project, isolation, database integrity, boot/API/frontend and cleanup report before atomic retention. `--pull` explicitly selects image retrieval. The key stays local; Actions recovery remains encrypted transport verification. Production restoration is a separate authorized operation.

## Independent tools and contribution

Generic SDK setup, build/test recipes, fixtures, container tooling and dependency/source checks live in [tools](https://github.com/nicodes/tools). Deployment adapters also support native callers without a recipe, SDK lock or hosted Komizo account. The optional `check-secrets` compatibility action delegates its read-only policy to Tools. Setup explicitly prepares its hash-locked isolated parser; checking never installs dependencies. Configuration publication retains its independently prepared parser runtime.

For this adapter repository’s native contributor gate, install Mise, prepare the exact Tools source selected by its checked-in CI and configuration, and run `mise install` followed by `mise run check`. Focused native commands remain `mise run lint`, `mise run test` and `mise run clean`. These exercise parsers, secrets, release integrity, SSH transport and workflow admission; this repository has no application development service.
