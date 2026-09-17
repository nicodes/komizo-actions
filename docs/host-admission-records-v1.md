# Host admission v1: nonsecret request and root-record prerequisites

This is an executable **codec and read-only record adapter**, not an executor,
installer, queue, live inventory collector, signature verifier or admission
decision. No current action invokes it. It neither enables production gating nor
completes studio #167. The [numeric policy and rollout gates](host-admission-v1.md)
remain unchanged, including three accepted sets per app, distinct previous
success, the seven-day protection union, and no automatic deletion.

## Frozen interface with the komizo owner

Future root-owned artifact: `/usr/local/libexec/komizo-host-admission v1 worker`.
Future app-baked wrapper: `/usr/local/bin/admit-deploy-<APP>`, granting only:

* `v1 capabilities`
* `v1 submit` with the descriptor on stdin
* `v1 status <request_id>` for the same authorized app

The formerly reserved operation `deploy` is renamed to `submit`, with **no
alias**. There is no executable CLI or capability-success response in this slice.
The future worker must be supervised independently of SSH by existing rootd;
private queue/results must not use monitor-readable inbox/served directories.
Those components and their authorization/installation remain separate work.

### Submit descriptor

Exactly four JSON fields, with no extensions accepted:

| Field | Required value |
| --- | --- |
| `protocol` | Literal `komizo-host-admission/v1` |
| `request_id` | Exactly 32 lowercase hexadecimal characters |
| `candidate_manifest` | `sha256:` followed by exactly 64 lowercase hex characters |
| `current_credential_revision` | Same digest syntax; an opaque revision identity, not a secret value |

The complete UTF-8 input, including JSON whitespace, must be at most 1,048,576
bytes. Invalid UTF-8/BOM, duplicate keys (including escaped-equivalent names),
unknown fields, wrong types, non-JSON constants and trailing non-whitespace data
deny. JSON whitespace is allowed; multiple objects are not. The binary stream
reader consumes to EOF within the limit, including after short reads. It is a
codec, not a transport: the future receiver must independently enforce a deadline
and connection/concurrency bounds. Errors never echo supplied values.

`read_request(binary_stream)` / `decode_request(bytes)` return frozen `Request`.
`encode_request(Request)` returns deterministic compact UTF-8 JSON after checking
identities. The request carries **no app**: trusted wrapper registration binds the
application. It carries no paths, commands, URLs, environment, policy overrides,
measurements, registry token, secret values or secret updates. Rejecting these is
intentional, not an instruction to send them through another unguarded path.

### Initial secret policy

Normal use of existing credentials is permitted; extra durable secret copies,
snapshots, spool and revisions are not. Candidate, current and previous recovery
records must name the same existing credential revision. Secret-changing or
unproven database-compatible deployments deny. There is no key provisioning,
rotation or database rollback here. Do not persist secrets by resolving Compose
interpolation into a manifest, config snapshot, descriptor, journal or result.
Nonsecret config templates and existing credential-revision references are the
only applicable record identities. Even metadata matching does not prove that
the actual credential or database state matches: the future trusted adapter must
establish that under the transaction lock.

## Trusted root-record store

`load_records(app, platform, request, now)` reads exactly:

`/var/lib/komizo/admission/records/<app>/<candidate_manifest_hex>.json`

The app/platform/time arguments are trusted host context, not caller overrides.
The supported platform identities are `linux/amd64` and `linux/arm64`. This code
does not read app registration itself. No path or alternate store is accepted
through the request or public loader API. Every ancestor is traversed using
directory descriptors and O_NOFOLLOW, must be root:root, and must not be group-
or other-writable. The admission, records and app directories must be mode 0700.
The record must be a root:root, mode-0600, single-link regular file, opened with
O_NOFOLLOW/O_NONBLOCK and limited to 1 MiB. FIFOs, symlinks, hardlinks, unsafe
ownership/mode, oversized files, missing files and observed in-place changes
during a read deny. Producers must publish by atomic replacement, not in-place
editing; a loader can read either complete generation. No file is written.

### Authenticity is an import responsibility, not a JSON assertion

Only a separately authorized **trusted root importer** may publish into this
private store. Before doing so it must authenticate the evidence producer and
release/config manifests, verify retained measurement and compatibility evidence,
validate complete history and applicability, and retain the referenced records.
The same store must never become a landing zone for caller JSON or be writable
by a deploy account or `komizo_monitor`.

The adapter enforces this filesystem boundary, explicit provenance metadata,
schema/bindings and validity window. It does **not** verify signatures, fetch
referenced records, establish producer authority, or prove that measurements,
completeness markers or compatibility assertions are true. Digest syntax,
`root-verified-import/v1` text and root ownership alone do not establish those
facts. No importer exists in this slice. A future integration that copies
unverified caller data into the store would violate the contract, not satisfy it.

### Closed bundle schema

Every object below rejects missing, duplicate and unknown fields. Counts are
nonnegative integers, never booleans. All digest fields use the same immutable
syntax as the request. Timestamps are integer Unix seconds.

| Top-level field | Meaning |
| --- | --- |
| `schema` | Literal `komizo-host-records/v1` |
| `app`, `platform` | Exact trusted host-context identities |
| `candidate_manifest`, `current_credential_revision` | Exact request bindings |
| `provenance` | Object described below |
| `history_complete` | Literal JSON `true`, asserted only by the verified importer |
| `current`, `previous_successful` | Distinct accepted release manifest digests |
| `releases` | 1–256 complete release records; at least three accepted plus the pending candidate |
| `filesystems` | 1–32 filesystem-bound measurement records |
| `compatibility` | One direction-specific record for restoring the previous success after this candidate |

`provenance` has exactly `kind` (`root-verified-import/v1`), `producer` (1–64
lowercase letters/digits/underscore/hyphen, starting alphanumeric),
`verification_record` (digest identifying retained verification evidence),
`verified_at`, `expires_at`. Require `verified_at <= now < expires_at`.
No arbitrary freshness interval is invented: the trusted producer/importer must
set and justify applicability/expiry. Expired or future verification denies.

Each release has exactly the existing policy `Release` fields: `manifest`, `app`,
`images`, `config_image`, `accepted_at`, `schema`, `credentials`, plus `complete`
which must be literal `true`. `images` is a nonempty list of at most 256 unique
immutable digests including `config_image`; it covers all profiles, not just
running services. `schema` and `credentials` are revision digests. `accepted_at`
is null for pending or a nonnegative timestamp no later than now. Manifests must
be unique. Current/previous must exist and be accepted. Candidate must exist and
be pending. Candidate/current/previous credentials must match the request.

History must not silently omit accepted sets within the retention union, current,
previous or pending sets. The maximum is a resource limit, not permission to
truncate history to pass. A deployment with incomplete/oversized history denies;
the importer/retention policy must resolve it without automatic deletion.

Each filesystem record has exactly `filesystem_id` (digest of a trusted stable
mapping identity), `paths` (1–32 unique canonical absolute paths), `measurement`.
Filesystem identities and paths cannot repeat across records. Paths are metadata
from trusted registration, not paths this loader opens. Their actual filesystem
mapping/completeness must be re-established by the adapter under the global lock.
There are no historical available-space counters in this bundle.

`measurement` contains exactly the policy `Measurement` fields:
`candidate_manifest`, `candidate_evidence`, `write_evidence`,
`candidate_peak_bytes`, `candidate_peak_inodes`, `write_30m_bytes`,
`write_30m_inodes`, `write_window_seconds`. The candidate must match, evidence
digests are required, counts are nonnegative, and the window is exactly 1800.
Zero remains permitted only when genuinely measured/applicable; syntax does not
prove that. Records identify retained evidence, never caller headroom overrides.

`compatibility` has exactly `release_manifest` (previous success),
`post_candidate_manifest`, `post_candidate_schema`, `post_candidate_credentials`,
`rollback_schema`, `rollback_credentials`, `evidence` (retained verification
record digest). All direction-specific identities must match the corresponding
release records. An arbitrary matching tuple is not proof of real database or
credential compatibility; this adapter does not inspect their actual state.

The return value is frozen `Records` containing policy `Release` and `Measurement`
instances plus immutable provenance, filesystem and compatibility metadata. It
is not authorization, a freshness guarantee for subsequent operations or a
substitute for `protected_sets`, `check_headroom`, `check_offline_restore`, live
container/image/config inventory, all-writer locking, evidence authentication or
health/recovery tests. Nothing is pulled, activated, modified or deleted.

## Required follow-through

The initial host has no proven three-set authenticated history or measured
admission evidence. It must remain unopted-in until authentic complete records
and the other gates exist; do not clone the current set or mark build archives
as accepted deployments. Existing candidate image-transfer metadata is useful
input to a future trusted producer, not proof of host acceptance or write bounds.

Missing records in this new format do **not** require waiting for three future
deployments. A trusted importer may reconstruct genuinely accepted legacy sets
from authenticated CI identities, actual acceptance/health evidence, immutable
image/config identities and retained restore records. Legacy evidence is a
reconstruction source, not automatic eligibility: never invent acceptance times,
infer complete sets from tag counts, or omit external/profile images. For example,
Revik's five published images include its config image; its separately supplied
Redis image must also be accounted for. Database/credential compatibility,
distinct previous success, offline completeness and the full retention union
still require proof. Prospective witnessed history is an alternative when
authentic reconstruction cannot supply the floor, not a mandatory waiting period.

Next work remains the real private worker/queue, mandatory global lock and durable
fence, all-writer migration, unchanged-credential Compose transaction, actual
offline recovery and health, installer/readiness supervision, consumer adoption
and authorized live verification. No capability success, production gate, action
release or issue closure is justified by this prerequisite alone.
