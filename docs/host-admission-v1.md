# Host admission v1 — prerequisite contract, not an installed feature

Tracking: [studio #167](https://github.com/aviorstudio/fieldsofrevik/issues/167).
Deploy actions owns this policy/interface. **No existing deploy action calls this
module. No host adapter, privileged installer, cleanup, or deployment transaction
is implemented here.** Passing these pure checks neither admits a deployment nor
proves an installed host has these capabilities. Do not enable a production gate
by printing a successful JSON reply or adding a separate SSH preflight.

Source baseline: actions `079754e3eff1cf948466f50e0de9c23a40ee7137`
(v0.0.8), not the earlier Phase 1 snapshot. The observed Revik consumer still pins
`dd77ee8778d82ce25ee9e8273debffd5b92e4f1c` (v0.0.3). Its CD connects using the
production environment, `KOMIZO_SERVER_URL`, `KOMIZO_APP_NAME`, pinned
`KOMIZO_KNOWN_HOSTS`, and `KOMIZO_DEPLOY_KEY`; then invokes
`doas /usr/local/bin/deploy-<app>`. Keys must remain in the standard client/CI
credential path, never exported for inventory. No live host capability is inferred
from either source snapshot. The existing independent set-secrets/activate/health
steps cannot provide this transaction.

## Executable policy contracts

`host_admission.policy` is a Python 3.10+ library with no dependencies, shell
execution, network, deletion or command-line admission endpoint. Evidence is
typed input from a **future trusted root adapter**, not a caller-controlled
policy override. Invalid/incomplete evidence must abort; an unexpected exception
is also denial, never a fallback to legacy deploy. Success returns accounting or
protection sets only. It is not a reusable authorization token.

For **each affected filesystem** (including Docker data, release staging, config,
database/WAL/migrations and logs; group paths by filesystem before counting):

* Required free bytes = `max(5 GiB, ceil(20% usable capacity))`
  + `ceil(1.5 × exact candidate incremental peak bytes)`
  + `max(1 GiB, 2 × representative 30-minute write growth bytes)`.
* Required free inodes = `max(100000, ceil(10% usable inode capacity))`
  + `ceil(1.5 × candidate incremental peak inodes)`
  + `2 × representative 30-minute write inode growth`.
* Equality passes; one byte/inode below denies. Every fractional margin rounds
  upward independently. Unknown inode reporting or filesystem mapping denies.
  No historical free-space observation or reclaimable-image estimate is credited.
* Candidate evidence is bound to the exact complete manifest, includes overlapping
  pulls, extraction, staging and temporary peaks, and comes from retained measured
  records. Write evidence covers representative database/WAL/migration/log growth
  for 1800 seconds. Zero is allowed only when actually measured and applicable.
  A digest's syntax does **not** authenticate an evidence record: the adapter must
  establish provenance, platform/filesystem applicability and freshness. Records
  absent or no longer representative deny; no silently invented write bound.

Protect the union of complete current and previous successful sets regardless of
age, all accepted sets in the last seven days (inclusive), and at least the three
newest accepted sets **per app**. Also protect pending sets, running AND stopped
container images, explicit pins, unknown-ownership images and shared images.
Complete sets include config and every image in all profiles, addressed by
immutable locally resolvable digest. Metadata completeness is verified by the
adapter, not inferred from a nonempty list. v1 denies fewer than three accepted
sets or missing distinct previous success; bootstrap policy is not invented.
Out-of-protected images are only *eligible*, never authorized for removal. No
volume/container prune, forced removal or automatic image deletion exists here.

Offline recovery requires the complete previous successful local image/config
set, authenticated release manifest, and **direction-specific compatibility with
the post-candidate database schema and credential revision**. Credential metadata
is an opaque revision identifier, not contents. Keeping an image is not database
rollback. A candidate with irreversible/unknown schema or credential effects
denies until a separately approved compatible recovery plan exists. Compatibility
records must be verified by the adapter against actual resulting state; merely
passing an arbitrary tuple to the library proves nothing.

## Narrow privileged boundary (reserved protocol, not yet implemented)

Proposed fixed root-owned, non-deploy-user-writable per-app entrypoint:
`doas /usr/local/bin/admit-deploy-<app> v1 <operation>`, with literal operations
`capabilities`, `submit`, `status <request_id>`. The formerly reserved `deploy`
operation is renamed to `submit`; there is **no alias**. The future fixed artifact
is `/usr/local/libexec/komizo-host-admission v1 worker`. None of these executables
is implemented or installed by this prerequisite. Application identity, allowed
images, filesystem roots, health endpoints and policy come from root-owned
registration, not caller paths. No arbitrary commands, URLs, hooks, paths, environment, Docker
arguments or general root shell. doas authority must be scoped to that app.

`capabilities` is read-only and follows the exact version/capability/timing shape
checked in `host_admission.protocol`. Unknown versions/fields, unavailable flock
or a nonparticipating writer deny. A capability reply alone is not admission.
The future adapter must prove its installation and participation, not self-assert
unsupported capabilities. No fallback to the old deploy command is permitted.

The frozen initial submit descriptor is nonsecret JSON on stdin with exactly
`protocol`, `request_id`, `candidate_manifest`, `current_credential_revision`.
`host_admission.request` enforces UTF-8, a 1 MiB byte ceiling, strict identities,
and duplicate/unknown-field rejection. No caller app, paths, measurements, secret
values, secret changes, or registry token are accepted. The app is bound by the
root-owned wrapper. See [the request and record contract](host-admission-records-v1.md).

The initial profile permits normal use of existing credentials only. **No extra
durable secret copies, snapshots, spool, or revisions are authorized.** Secret-
changing deployments or deployments lacking proof of post-candidate database
compatibility must deny.
Resolved/interpolated Compose secrets must never be persisted into descriptors,
journals or records. Any future sensitive handoff requires a separately bounded
memory-only protocol; this descriptor does not implement one. Caller measurements
cannot override trusted records. `host_admission.records` only reads root-imported
nonsecret records; it does not authenticate their source, collect live inventory,
or provide an executor, queue, capability success, or permission to deploy.

Required authoritative transaction order:

1. Validate fixed operation, caller/app authorization and bounded request. Acquire
   the **same mandatory host-global kernel lock used by every writer**, with a
   600-second acquisition ceiling. Missing lock support fails closed.
2. Under the lock, read fresh filesystem/inode/image/container/release inventory,
   validate trusted measurement records and offline recovery completeness, and
   apply policy. Missing policy, inventory or compatibility denies before pulls,
   config/secret writes or activation.
3. Stage nonsecret config transactionally only after admission. Under the initial
   unchanged-credential profile, reference the existing compatible credential
   revision without copying secret values or changing credentials.
   Pull only the admitted candidate; keep the lock through re-inventory and
   headroom recheck immediately before activation. Failure must restore staged
   state without partial activation.
4. Activate and check root-registered health under the lock. Commit a new
   successful manifest only after health success; otherwise restore the complete
   previous successful set **without registry login, lookup or pull**, using
   local immutable images and config, then prove restored health.
5. Normal transaction budget is 1200 seconds; recovery budget is an additional
   600 seconds. Stop admission and enter recovery on budget exhaustion. Never
   implement a TTL that releases the lock while a mutator survives. Preserve the
   lock across the process tree, fence further admission on unresolved recovery,
   and require explicit operator intervention after an unrecoverable timeout.
   SSH loss must not release the boundary or orphan an unfenced mutation; the
   adapter needs a tested disconnect/recovery lifecycle, not `trap` alone.

All deploy, set-secret, task, manual Docker/config and background host writers
must participate or be excluded by an enforceable privilege boundary. Upgrading
only this action while legacy privileged wrappers remain callable cannot enforce
global admission. Root-owned installation/writer migration needs separate approval
and a source-owner decision; this prerequisite supplies neither. No production
cleanup, installation or service changes are authorized by read-only inventory.

## Verification and rollout gates

Run pure controls with:
`python3 -m unittest discover -s tests -p 'test_host_*.py' -v`.
They cover threshold failures/restoration, exact candidate evidence, union
retention across applications, missing local images/config, incompatible schema
and credentials, missing lock/writer capabilities and unknown protocol/timing.
Request/record controls additionally cover the frozen nonsecret descriptor,
safe root-file loading, strict schema and directional evidence bindings. These
are prerequisite tests, not a working host-admission implementation.

**Still mandatory before a deployable feature/release or #167 closure:**

* Sanitized live bytes/inodes, image IDs/references/manifest digests, installed
  wrapper versions/digests and all-writer inventory through existing pinned
  deploy access; no secrets, user data, raw config or unrestricted daemon inspect.
* Separately owned/approved root adapter and all-writer installation/migration;
  tested provenance, root registration integrity and privilege rejection.
* Disposable non-production real filesystem/daemon full and near-full byte/inode
  controls, then restored successful deployment; real two-app lock contention and
  fresh inventory after waiting, missing-lock/capability and SSH-loss controls.
* Actual nonsecret staging/restoration tests without contents in logs or extra
  secret copies; unchanged-credential enforcement, complete and partial offline
  rollback, schema/credential incompatibility, protected/shared
  images, measured timing controls, and successful restored health. A mocked
  policy result cannot substitute for these tests. No production disk-full tests.
* Exact-head full CI, approved feature release/version, separately owned consumer
  pin adoption/CD, and authorized live capability plus behavior verification.

This prerequisite can be merged without an action release. Production CD and
daemon/privileged integration tests are **not applicable to this non-integrated
library**, and remain blockers to the overall issue, not passed acceptance.
