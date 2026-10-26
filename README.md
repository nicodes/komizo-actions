<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 0a067721a35395ba31029a143727746962862e2da08dc33e041af1539d8fb645 -->

# Komizo Actions

Reusable GitHub Actions for Komizo deployments and project maintenance. Pin actions and workflows to a reviewed full commit SHA.

Deployment actions include `connect`, `publish-config`, `publish`, `activate`, `health-check` and `preview`. Callers supply their own project metadata, server, user and authorized host profile.

The reusable `reusable-backup.yml`, `reusable-security-scan.yml` and `reusable-dependency-maintenance.yml` workflows use the caller’s `komizo.json` and `komizo.lock`. Callers grant `attestations: read`; backups also require `id-token: write` and the declared transport/storage secrets.

Use `recovery-evidence` with exact run and artifact IDs to check backup transport. It leaves decryption and database restoration to the private local recovery procedure.

For development, install the [Komizo release](https://github.com/nicodes/komizo#install) matching `komizo.lock`, then run `komizo check`.
