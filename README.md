<!-- Generated from private documentation source. Do not edit directly. Source SHA256: a814aca1a943f69e4c3499105763bf6b9f72a50bb71958e6ce96605debefbb64 -->

# komizo Actions

Optional GitHub adapters for Komizo deployment, scoped connectivity, previews, release evidence and encrypted backup transport. Pin actions and reusable workflows to reviewed full commits. Callers supply their own images, targets and authorized host policy; the host independently enforces admission.

Action dependencies remain inline in workflow YAML and generators. There is no separate action-pin registry. Existing deployment and Preview `komizo_ref` declarations retain their own reviewed Actions authority.

Authored delivery, backup, recovery and publication declarations use TOML, including `deploy/komizo.toml`. Configuration publication emits native Compose YAML and hostnames; generated release and acceptance receipts remain JSON.

## Recipe source identities

Recipe-aware callers use [Cookbook](https://github.com/nicodes/cookbook) for generated Mise tasks, recipe data and complete native validation. Set `cookbook-ref` to the reviewed full Cookbook commit and leave the historical `tools-ref` input empty. The selected Cookbook provider's own `mise.toml` supplies its literal `vars.tools_ref` dependency on [Tools](https://github.com/nicodes/tools). A consumer cannot choose a different helper revision.

`COOKBOOK_ROOT` and `COOKBOOK_REF` identify the actual Cookbook checkout and commit. `TOOLS_ROOT` and the exported `TOOLS_REF` identify its separate Tools checkout and commit. Trusted preparation verifies the fixed repository origins, clean immutable source and owning-main ancestry before recipe execution. It then prepares the exact caller SDK versions explicitly; recipe tests consume prepared inputs.

The corresponding recipe driver arguments are `--cookbook-root` and `--cookbook-ref`. Where supported, `--tools-root` supplies an already prepared location whose identity must match Cookbook's dependency. Cookbook recipe acceptance binds `cookbook_repository`, `cookbook_commit`, `tools_repository`, `tools_commit` and `entrypoint_contract = "mise-v1"`. Operational and Preview plans retain the same explicit source identities.

Independently pinned historical adapters retain their genuine Tools-hosted recipe SDK and original command protocol. Their `tools-ref` input names that actual Tools commit. The historical and Cookbook selectors cannot be mixed, and a Cookbook commit cannot stand in for Tools. Native deployment actions remain usable independently of recipes.

## Accepted recipe artifacts

Generated recipe callers compose read-only source validation with separate publication and host-effect jobs. `cookbook-publish.yml` and `application-publication` consume exact same-run complete recipe artifacts. They verify the successful validation job, source/tree, recipe provider, complete native coverage and closed image archive before publishing. Publication executes no application setup or build program. Nonshipping fixture images remain outside the shipping publication set.

The externally matched successful host check remains `Make full` for existing protection and receipt compatibility. Cookbook mode runs canonical Mise tasks.

Recipe-aware adapters read effective settings through the caller's prepared, pinned Cookbook source. Shared CI defaults, literal native test groups and mandatory static metadata use that SDK's decoder and capability definitions. Generic verification and release helpers come from its actual Tools dependency. Raw configuration hashes and the complete accepted artifact scope remain bound to the original source.

`application-delivery` reads the tracked native declaration, refuses obsolete main before host effects, captures a declared encrypted backup, activates the accepted immutable manifest and runs explicit health assertions. Secret mappings remain visible in the caller. Production retains its authenticated root `cd.yml`, protected environment, serialization and declared total deadline.

`godot-static-delivery` uses the same Cookbook and derived Tools identities while retaining its constrained gate/configuration protocol. It admits the exact full Godot proof, source-owned blueprint and closed image archive before publication. The trusted publisher substitutes only a verified immutable image digest into the accepted configuration blueprint. Existing release records, historical recovery and fixed-origin public verification remain explicit capabilities. Historical image verification does not issue a current-source full-validation receipt.

Every caller still requires its own complete source CI and declared delivery and live checks. Shared adapter validation does not establish deployment or installed-runtime acceptance.

## Preview

Preview automation separates trusted request decisions, read-only validation of the selected source, accepted-artifact publication and protected host effects. These recipe-aware roles use the same Cookbook and derived Tools contract. Cleanup has its own path. Product source checks receive no publication, host or provider credentials.

Existing Preview producers bind the actual selected PR head, native workflow declaration, shipping components and public build inputs. Trusted publication verifies the same run's complete proof and image archive without executing caller programs in the package-write job. Current PR head/base, source identity, original aggregate and command deadlines, protected environments and credential mappings remain enforced through host effects. Preview policy and runtime acceptance stay with the caller.

## Explicit operations

The cookbook operational workflows read bounded project-owned declarations through the selected Cookbook source and use its Tools dependency for generic helpers. They retain the caller’s real runners, job and step deadlines, protected environments, schedules, evidence policy and native assertions. `cookbook-source-audit.yml` handles declared source/artifact checks; `cookbook-security-audit.yml` additionally selects an actual deployed-image policy. Reporting and deployed-registry permissions use separate workflow variants when those capabilities are absent. Artifact validation declares setup, build, security and selected assertion phases explicitly. Source-only audit receipts remain scoped to their declared checks.

Production checks run genuine caller-owned assertions. Paid checks require explicit default-off dispatch, trusted current main, their original protected environment and deadline, and any declared successful-deployment prerequisite. An inline paid assertion shares its original production job’s total budget. Provider credentials enter only the selected paid execution step.

`routine-backup` prepares an audience-bound OIDC operation frame, invokes a fixed declared exporter, validates ciphertext, uploads the immutable pair and removes its private authority file. Only the backup job receives its required OIDC scope and scoped host/storage secrets. First-deployment absence proofs are explicit data. Capture, schema and data assertions remain application-owned. The older `reusable-backup.yml` interface continues to call its caller-owned export script for independently pinned consumers.

`recovery-evidence` verifies exact completed run and artifact IDs without decrypting or restoring application data. The optional legacy retention workflow checks the existing encrypted AGE pair and checksum before retaining it; it never restores data. `backup-proof` requires Python 3 and a new absolute output file under a protected caller-owned directory.

The shared `scripts/backup.py` has export/upload and explicit local restore entrypoints. A named restore profile selects tracked native configuration. Local restoration requires an absolute private `BACKUP_RECOVERY_KEY_FILE`, packages only `snapshot.cms` and `receipt.json`, and validates the project, isolation, database integrity, boot/API/frontend and cleanup report before atomic retention. `--pull` explicitly selects image retrieval. The key stays local; Actions recovery remains encrypted transport verification. Production restoration is a separate authorized operation.

## Independent tools and contribution

Build/test recipes, generated Mise entrypoints and recipe workflows live in [Cookbook](https://github.com/nicodes/cookbook). Independent SDK setup, native tooling, container utilities and dependency/source checks live in [Tools](https://github.com/nicodes/tools). Deployment adapters also support native callers without a recipe, SDK lock or hosted Komizo account. The optional `check-secrets` compatibility action delegates its read-only policy to Tools. Setup explicitly prepares its hash-locked isolated parser; checking never installs dependencies. Configuration publication retains its independently prepared parser runtime.

For this adapter repository’s native contributor gate, install Mise, prepare the exact Tools and recipe test sources selected by its checked-in CI and configuration, and run `mise install` followed by `mise run check`. Focused native commands remain `mise run lint`, `mise run test` and `mise run clean`. These exercise parsers, secrets, release integrity, SSH transport and workflow admission; this repository has no application development service.
