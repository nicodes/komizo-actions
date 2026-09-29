# The secret rule

A secret is held in GitHub and delivered to the host by komizo, or it is
generated on the host and never leaves it. There is no third way.

Everything below follows from that sentence, and `check-secrets` is that
sentence as a check. The rule is written down here because it was previously
only implied — by one comment in one app's compose file — and five apps had
already drifted off it in five different directions.

## The two origins

**From GitHub.** Name it `KOMIZO_SECRET_<NAME>` in the deploy step's `env:`
block and `set-secrets` pushes it to the host as `<NAME>` in `secrets.env`.
That env block is the whole declaration: it says which secrets exist and which
ones the host gets, in one place, so there is no second list to keep in
agreement with it. The store is write-only by construction — the value arrives
on stdin, is never echoed, and `secrets.env` stays `0600 root` — so CI can
rotate a credential without being able to read the ones already there.

**Generated on the host.** Some credentials should never exist in GitHub at
all: the PostgreSQL superuser password and the role passwords derived from it.
Nobody types them, nothing needs to see them, and putting them in a secret
store only widens who could. They are provisioned on the box and read from
host-local files — `postgres-owner.env`, and the per-service files under
`secrets/current` that a scoped-env profile writes.

## What follows

**One `secrets.env`, one reader.** `secrets.env` holds *every* secret the app
keeps in GitHub, so a service that reads it reads all of them. Only the service
that needs them may. Two services on the same file is not a smaller version of
the right answer, it is the whole secret set handed to both.

> This is the Revik near-miss, and it is worth being concrete about. The four
> services there need four different things: `migrate` needs the migrator DSN,
> the API needs the app DSN and Clerk, `godot-api` needs only the bridge
> secret, `postgres` needs the owner password. Putting all four on one
> `secrets.env` would have simplified the deploy and handed the API the
> migrator DSN — the credential that can drop the schema. A boundary test
> caught it before it shipped; this check is that test, generalised.

**Split by service, not by file.** When services need different secrets, give
each one its own host-local file under `secrets/current`. The API's file has
the app DSN; the migrator's has the migrator DSN; neither can read the other.

**Nothing else writes the store.** No `scp` of an env file, no
`ssh host 'cat >> secrets.env'`, no calling `set-secret-<app>` by hand. A value
placed that way has no provenance: it is not in GitHub, no profile generated
it, and the only record that it exists is the file itself.

**Nothing secret is committed.** A key in `compose.yml` is a key in the config
image, in every clone, and in the reflog after it is "removed". Publishable
keys (`pk_live_`) are meant to be in the client bundle and are not secrets.

**Removing a name from CI does not remove it from the host.** `set-secret`
writes and never deletes. This is the one part of the rule a repository cannot
enforce, and it is where every stale credential in this portfolio came from.

## Checking it

In CI, on pull requests — it needs no deploy key, no registry token and no
server:

```yaml
- uses: nicodes/komizo-actions/check-secrets@v0.0.1
  with:
    compose: deploy/compose.yml
    workflows: |
      .github/workflows/cd.yml
      .github/workflows/preview.yml
    host-only: |
      postgres-owner.env: postgres
```

`host-only` is where the generated credentials are declared, one file at a
time, with the services allowed to read it. Widening that is then a diff in the
workflow rather than a line in a compose file nobody reviews.

Set `secrets-env-services` only to say out loud that two services share the
whole secret set. Leave it empty and at most one may.

For the host half, from your own machine:

```
scripts/host-secret-drift.sh root@myhost                      # inventory
scripts/host-secret-drift.sh root@myhost --repo ~/src/myapp   # judgement
```

It reads key **names** only — no value is fetched, printed or stored — and
reports both directions: a key on the host that no workflow delivers (nothing
rotates it), and a name CI delivers that has not landed yet (expected, if it
was added since the last deploy).

## When the check fires on something correct

Then the rule is wrong, or the app is a case the rule did not anticipate, and
the fix is to change one of the two — not to add an ignore. A check people
route around is worth less than no check, because it also reports that
everything is fine.
