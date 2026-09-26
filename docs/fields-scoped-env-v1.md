# fields-postgres-v1 scoped service env

This is the CI half of one approved profile. It is not a general secret
store, and it is not a release. Nothing here is tagged or published.

## What it is

`set-service-env` talks to the host command the CLI profile installs:

```text
doas /usr/local/bin/set-scoped-env-fieldsofrevik <stage|confirm|abort|status>
```

No other arguments. No `doas -n`. The host lock is that command's; this
action has no flag that skips it.

The only approved profile is `fields-postgres-v1`, and only for app
`fieldsofrevik`. Any other profile or app is refused before a connection.

`deploy`'s `service-env-profile` input is the lifecycle. Empty — the default
— leaves `set-secrets` exactly as it was. Set to `fields-postgres-v1`, it
does not call `set-secrets` and does not push `KOMIZO_SECRET_*` to the
aggregate `secrets.env`. A `KOMIZO_SECRET_*` variable or a `secrets:` list
beside the opt-in is refused, so a green run cannot mean "those were pushed
too".

## Values

The caller supplies ten nonempty environment variables. They are not action
inputs: an input is recorded in the workflow run.

| Variable | Key on the wire |
| --- | --- |
| `KOMIZO_SCOPED_CLERK_AUTHORIZED_PARTIES` | `CLERK_AUTHORIZED_PARTIES` |
| `KOMIZO_SCOPED_CLERK_ISSUER` | `CLERK_ISSUER` |
| `KOMIZO_SCOPED_CLERK_JWKS_URL` | `CLERK_JWKS_URL` |
| `KOMIZO_SCOPED_DATABASE_MIGRATION_URL` | `DATABASE_MIGRATION_URL` |
| `KOMIZO_SCOPED_DATABASE_URL` | `DATABASE_URL` |
| `KOMIZO_SCOPED_POSTGRES_PASSWORD` | `POSTGRES_PASSWORD` |
| `KOMIZO_SCOPED_REVIK_APP_PASSWORD` | `REVIK_APP_PASSWORD` |
| `KOMIZO_SCOPED_REVIK_BACKUP_PASSWORD` | `REVIK_BACKUP_PASSWORD` |
| `KOMIZO_SCOPED_REVIK_MIGRATOR_PASSWORD` | `REVIK_MIGRATOR_PASSWORD` |
| `KOMIZO_SCOPED_WS_SECRET` | `WS_SECRET` |

All ten are checked before the first remote call of `stage`. An empty value
is the same failure as a missing one: GitHub substitutes an empty string for
a secret that does not exist. An extra `KOMIZO_SCOPED_*` name is refused, so
a typo is not silently dropped.

`stage` sends them on stdin only. The payload is exactly 11 LF-terminated
lines and no extra byte: `v1`, then those keys in ASCII order, each
`KEY=<RFC4648 padded standard base64 of the raw value>`. The base64 is one
line even when the value contains a newline. `confirm`, `abort` and `status`
send an empty stdin. Nothing is an argument, because arguments are visible
in the host process list.

The action does not enforce a stricter charset than "nonempty, and bash can
hold it" (no NUL). The host's strict value charset is unresolved. A value the
host rejects fails the step. The value is not logged.

## Lifecycle

`deploy` with the opt-in:

1. Validates the profile, the app, the ten values, the absence of legacy
   secret names, and a nonempty `health-urls`, before connect.
2. Stages, before activate.
3. Activates.
4. Health-checks. An empty `health-urls` is refused up front. Confirm is not
   legal after a skipped check.
5. Confirms only when the health step's outcome is success. Confirm deletes
   the previous generation on the host.
6. On activation failure, health failure, or a cancellation that still lets
   the runner run an `if: always()` step, aborts if this job's stage ssh
   returned 0 and confirm did not complete.
7. Fails the job if stage succeeded and confirm did not. Abort restoring the
   link does not make the deploy green.

A composite action cannot declare a `post:` step. If the runner is killed
during the stage ssh, no marker is written and no abort runs. That case fails
loud when a later step still runs, and says the cancellation lease is
unresolved. It does not claim the link was restored.

The direct action is the same four verbs. `stage` succeeding there means
staged, not confirmed. A workflow that only stages is green with an
unconfirmed stage. Pair it with confirm or abort, including an `if: always()`
abort, or use `deploy`.

## What the host does, and what this action does not

Fields Compose is planned to read
`./secrets/current/{postgres,migrate,api,godot-api}.env`. Mapping the ten
values onto those files, and fanning `WS_SECRET` out, happen host-side. This
action does not write those files.

`stage` switches `current` atomically and retains one prior pending
generation. `confirm` deletes that previous generation. `abort` restores the
link. Abort does not roll back containers, database role credentials or
migrations.

`docker compose up` may not recreate a service whose resolved config is
unchanged. Activate does not pass `--force-recreate`, and this action does
not change that command. A staged rotation, and a green deploy, are not proof
that every container restarted onto the new values. A container that kept
running still has the environment it started with. A container that was
recreated has the new one. This action cannot tell those apart.

After a failed health check the link may already have been restored while the
containers are still the ones activate started. That split is not repaired
here.

## Unresolved on the host

These are not accepted, and this action does not close them:

- **Cancellation lease.** A runner killed during `stage`, or a host lock held
  by a session that will not return, can leave a staged generation. The
  action aborts when it has a marker and a live runner. It cannot abort a
  host it can no longer reach, and it does not invent a lease timeout.
- **Previous-generation retention.** `confirm` is what deletes the previous
  generation. If confirm never runs and abort never reaches the host, that
  generation can be retained indefinitely. Abort restores the link; it does
  not delete generations. No retention bound is implemented here because the
  host contract does not define one.
- **Strict value charset.** Unresolved. The action encodes any nonempty value
  and surfaces a host rejection without logging the value.

## Draft release note

Not published. Not a tag. Not a GitHub release. Do not run `scripts/release.sh`
for this branch.

> `set-service-env` and `deploy`'s `service-env-profile: fields-postgres-v1`
> stage ten `KOMIZO_SCOPED_*` values to
> `doas /usr/local/bin/set-scoped-env-fieldsofrevik` before activate, confirm
> only after health success, and abort on activation or health failure. The
> legacy `KOMIZO_SECRET_*` path is unchanged when the input is empty, and is
> not used when it is set. Abort does not roll back containers, database role
> credentials or migrations. `docker compose up` may not recreate a service
> whose resolved config is unchanged. The host cancellation lease,
> previous-generation retention and strict value charset remain unresolved.
