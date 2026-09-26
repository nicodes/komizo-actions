# fields-postgres-v1 scoped service env

This is the CI half of one approved profile. It is not a release, and it does
not prove that a fresh PostgreSQL cutover is safe. Volume discovery, the
`PG_VERSION` fail-closed, and Compose recreation are host-owned. Nothing here
is tagged, merged, or installed on a host.

## What it is

Actions carries no profile value. Not a database URL, not a role password,
not `WS_SECRET`, and not the public Clerk settings. Those live in one
privileged host-local batch. This action sends only a nonsecret generation id,
and only on the deploy command.

Status is a separate read:

```text
doas /usr/local/bin/scoped-env-status-fieldsofrevik
```

No arguments. No stdin. No `doas -n`. Success is exit 0 and exactly one line:

```text
wire=v2 profile=fields-postgres-v1 source=host-local state=ready generation=<32 lowercase hex> reason=ok
```

The generation must equal `expected-generation`. A grammar-valid refusal
(`state=missing|invalid`, `generation=none`, or a reason other than `ok`)
still fails the step, even when the host exits 0. Exit 75 with empty stdout
is a lock timeout. Any other stdout is a protocol error and is not logged.
`ready`, `ok`, and a 32-hex generation travel together. Otherwise the
generation is `none`. A 32-hex generation on a missing or invalid line, or
any other pairing of those three, is a protocol error and is not logged.

The closed reason enum is `ok`, `no-current`, `bad-mode`, `symlink`,
`partial`, and `profile-mismatch`.

## Deploy

`deploy`'s `service-env-profile` input is the opt-in. Empty — the default —
leaves every app except `fieldsofrevik` on `set-secrets`. App `fieldsofrevik`
refuses an empty profile. Set to `fields-postgres-v1`, it requires app
`fieldsofrevik`, a 32-hex `expected-generation`, and `health-urls`.

`KOMIZO_SECRET_*`, `KOMIZO_SCOPED_*`, and a `secrets:` list are refused. A
green run cannot mean those were pushed.

The Fields deploy does not use the pinned `activate` action. That pin has no
fourth argument. This checkout runs:

```text
doas /usr/local/bin/deploy-fieldsofrevik '<version>' '<registry>' '<registry-user>' '<expected-generation>'
```

The registry token, if any, stays on stdin. No token means the middle two
arguments are empty quoted strings, including when the registry input still
defaults to `ghcr.io`. A mixed empty/nonempty pair is refused locally.
Legacy activate calls stay at one or three arguments.

The host rechecks the id under its lock immediately before the first
app-config mutation. Actions still requires exactly one stdout line
`deploy: scoped-generation=<the same id>` and exit 0. Absence fails, unlike
the optional `deploy: previous-version=` line. Preflight does not replace
that recheck. Postflight repeats the status compare after health.

There is no stage, confirm, or abort. A failed activate, a failed health
check, or a cancelled job stays failed. This action does not roll back
containers, database role credentials, or the host-local provision. `docker
compose up` may not recreate a service whose resolved config is unchanged,
so a green deploy is not proof that every container restarted.

## What this does not claim

The host must refuse an uncertain existing `pg_data` or role state, and must
refuse role-password rotation in v1. Those checks are not in this repository.
Do not treat a green Actions run as a fresh cutover.
