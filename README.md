<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 7fcb7fa2de71224ff4e98869d15194ac9c4154c92eda22a281fc9edb3a0e7564 -->

# komizo Actions

Optional GitHub adapters for Komizo deployment, scoped connectivity, secrets, previews, release evidence and routine host backup. Pin actions and reusable workflows to a reviewed full commit.

Deployment actions include connect, publish-config, activate, health-check and preview. Callers supply their own images, server, user and authorized host policy. The host independently enforces admission; these actions do not grant authority.

The reusable backup workflow calls the product's export script, prepares an audience-bound OIDC operation frame, and transports the encrypted snapshot with immutable object creation. It requires explicit project/storage inputs, scoped secrets, attestations read access and id-token write access for the backup job only. Products own data capture and restore assertions. Production restoration is a separate authorized operation.

Use recovery-evidence with exact completed run and artifact IDs to verify encrypted backup transport. It does not decrypt or restore application data. The standalone backup-proof action requires Python 3 and a new absolute output file under a protected caller-owned directory.

Generic SDK setup, Godot installation, container publication, dependency maintenance and source scanning live in [tools](https://github.com/nicodes/tools). Deployment adapters do not require a development recipe, SDK lock or hosted Komizo account.

For development, install mise and check out nicodes/tools at 0f95a6df60f740b527596ed2c7f4590e3f2d5820 into .ci-tools, then run mise install and mise run check. Product-owned tasks retain all parser, secret handling, release integrity, SSH transport and workflow checks.
