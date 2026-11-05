<!-- Generated from private documentation source. Do not edit directly. Source SHA256: c3b069720d9fe40e20840cc1b2ac11b30dc077e5af8e130ba75d4c1792cab7d4 -->

# komizo Actions

Optional GitHub adapters for Komizo deployment, scoped connectivity, secrets, previews, release evidence and routine host backup. Pin actions and reusable workflows to a reviewed full commit.

Deployment actions include connect, publish-config, activate, health-check and preview. Callers supply their own images, server, user and authorized host policy. The host independently enforces admission; these actions do not grant authority.

The reusable backup workflow calls the product's export script, prepares an audience-bound OIDC operation frame, and transports the encrypted snapshot with immutable object creation. It requires explicit project/storage inputs, scoped secrets, attestations read access and id-token write access for the backup job only. Products own data capture and restore assertions. Production restoration is a separate authorized operation.

Use recovery-evidence with exact completed run and artifact IDs to verify encrypted backup transport. It does not decrypt or restore application data. The standalone backup-proof action requires Python 3 and a new absolute output file under a protected caller-owned directory.

Generic SDK setup, Godot installation, container publication, dependency maintenance and source scanning live in [tools](https://github.com/nicodes/tools). Deployment adapters do not require a development recipe, SDK lock or hosted Komizo account.

For development, install mise and check out nicodes/tools at 4e94f9b8d304e92e90833158c9cfbb6450ebb67c into .ci-tools, then run mise install and mise run check. Product-owned tasks retain all parser, secret handling, release integrity, SSH transport and workflow checks.

## Contributor task interface

Contributor tool versions and task entry points live in the native Mise configuration. Use `mise run lint` for source checks, `mise run test` for the applicable behavioral tests, and `mise run check` for the complete supported gate. `mise run clean` removes declared generated outputs while preserving source, credentials and retained data. This package has no application development service.

## Application recipe delivery

Recipe callers can compose final-source validation, image publication and deployment without product shell wrappers. The `cookbook-publish.yml` workflow and `application-publication` action consume the exact same-run full Make artifact. They verify the successful validation job, source, cookbook, complete test coverage and closed image archive before publishing. Godot applications additionally require complete build, shard and capability evidence. Publication executes no product setup or build program.

`application-delivery` reads the tracked native delivery declaration, refuses obsolete main before host effects, captures a declared encrypted backup, activates the accepted immutable manifest and runs explicit health assertions. Host secret mappings remain visible in the product caller. The production caller keeps its `cd.yml` identity, protected environment and host serialization. A combined publication/deployment job can retain an existing total deadline. Body assertions can bind release metadata to the accepted source revision.

`routine-backup` keeps the existing caller backup job identity and OIDC scope. It prepares the audience-bound operation frame, invokes only a fixed declared project exporter, validates ciphertext, uploads the immutable pair and removes its private authority file. An optional exact first-deployment absence proof is declared explicitly. The shared `scripts/backup.py` offers export, upload and isolated restore entrypoints; restore still requires an explicitly supplied local recovery key. Product capture, schema and data assertions stay with their owning application. Neither source checks nor ordinary CI request production restoration.

The optional `check-secrets` compatibility action delegates its existing read-only policy to Tools. Setup explicitly prepares the hash-locked isolated parser SDK; checking never installs dependencies. Configuration publication retains its independently owned parser runtime.
