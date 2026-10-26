<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 35c5bde580abccd8672571e5b51314b075b8750acbc3baaec8e5c05c81ee2bf5 -->

# Komizo Actions

Reusable GitHub Actions for Komizo deployments and project maintenance. Pin actions and workflows to a reviewed full commit SHA.

Deployment actions include `connect`, `publish-config`, `publish`, `activate`, `health-check` and `preview`. Callers supply their own project metadata, server, user and authorized host profile.

The reusable `reusable-backup.yml`, `reusable-security-scan.yml` and `reusable-dependency-maintenance.yml` workflows use the caller’s `komizo.json` and `komizo.lock`. Callers grant `attestations: read`; backups also require `id-token: write` and the declared transport/storage secrets.

For development, install the [Komizo release](https://github.com/nicodes/komizo#install) matching `komizo.lock`, then run `komizo check`.
