# Host admission deferral v1 — storage visibility, not a guard

**Studio #167 remains OPEN / Backlog, explicitly deferred, not Done.** The current
reduced scope is a warning-only CD storage notice. The complex host-admission
worker, executor, history import and installation work is shelved. This document
does not resume it or authorize production changes beyond ordinary deployment.

## What the deploy action now reports

After core input validation and the existing SSH connection step, and before
config publication, secret delivery or activation, `deploy` runs one read-only
`LC_ALL=C df -Pk /` through the existing pinned `deploy-target` alias/account.
It reports **ROOTFS=/**, a UTC observation timestamp, available/total bytes and
free percentage. `df` has 1-KiB precision. It does not inspect Docker, config,
secrets, database contents or arbitrary caller paths, and needs no sudo/doas,
host Python, privileged helper, service or host configuration change.

The advisory warns when available bytes are less than
`max(5 GiB, ceil(total bytes / 5))`. Equality does not warn. Unavailable, denied,
malformed, oversized or timed-out lookup produces an **unavailable warning**, not
a healthy-space guess. The local probe has a 15-second deadline, at most one
additional second to reap its owned client, and an 8-KiB combined output limit;
it never emits raw SSH stdout/stderr. It may use an
existing SSH ControlMaster, but only terminates its own client PID on timeout.

The notice is deliberately **nonblocking**. Only this step has best-effort
failure handling. Existing input validation, connection verification, secret,
activation and health failures retain their existing behavior. It also runs
when `host` is omitted and an earlier step established `deploy-target`.

This is a root-filesystem observation, **not a Docker-storage mapping or a safety
check**. Docker or other application storage may reside on a different filesystem.
There is no candidate peak/write budget, inode gate, inventory completeness,
retention enforcement, global lock, rollback guarantee, admission token or
fail-before-mutation claim. The values can change immediately after observation.

## Preserved work and what it actually proves

| Work | Identity / result | Limits |
| --- | --- | --- |
| Pure policy/protocol prerequisite, PR #31 | Merge `42f0e4e79efee7e889b10ceac7c7e5b12ab502c8`; post-merge CI [35048664267](https://github.com/nicodes/komizo-actions/actions/runs/35048664267) | Non-integrated policy checks; no executor or host enforcement |
| Nonsecret descriptor/root-record prerequisite, PR #34 | Merge `485990af4cd839b4da59f379819c35162321da77`; post-merge CI [35183130125](https://github.com/nicodes/komizo-actions/actions/runs/35183130125) | Codec/read-only schema boundary; not evidence authentication or admission |
| Supplemental local real-filesystem controls | Corrected run passed the strict **48 MiB cumulative** payload cap: 40 MiB + 4096 successful bytes, 40 MiB + 8192 attempted bytes, 130 files | Real tmpfs accounting/ENOSPC and policy reserve controls, **not** fail-before-mutation or restored-deployment proof |
| Earlier supplemental attempt | Cumulative write budget exceeded by **4096 bytes**; evidence retained rather than erased | Peak allocation safety did not satisfy the cumulative cap; corrected run does not erase this deviation |
| Worker work-in-progress | Preserved separate `phase2-167-worker-lifecycle` worktree/branch at `485990af4cd839b4da59f379819c35162321da77`, with an uncommitted **153-line process-contract document** | **No worker implementation, worker tests or worker PR**; not merged or copied into this release |
| Historical-evidence work-in-progress | Preserved unfinished `validate.py` | **Not run**; no acceptance, complete-history or eligibility claim |

Existing incomplete worktrees, indexes, documents and evidence are preserved.
Raw/private cross-application inventory and evidence are not included in this
public change. No shelved work is deleted, merged as WIP or represented as done.
The [policy prerequisite](host-admission-v1.md) and
[nonsecret request/record contract](host-admission-records-v1.md) remain reference
contracts, not the behavior of this notice.

## What an explicit future resumption would still require

* A real supported-Compose executor with all mutating paths participating in a
  mandatory global lock and durable recovery fence, including image import,
  extraction, secret/task/manual/background writers—not a preflight facade.
* Authenticated complete release/config/image provenance and retained measurement
  records, representative candidate peaks and write bounds on every affected
  filesystem, and fresh inventory under that same transaction boundary.
* Authentic accepted history, distinct compatible previous success and offline
  image/config completeness. Genuine legacy evidence may be reconstructed when
  authenticated; missing new-format records do not mandate waiting for three
  future releases. Neither tags nor build archives prove accepted deployments.
* The approved initial unchanged-credential profile: no extra durable secret
  snapshots/spool/revisions, no secret-changing or unproven DB-incompatible
  deployment, no invented database rollback. Journals/config must not persist
  interpolated secret values.
* Real disposable-host fail-before-mutation, contention, SSH/process/daemon-loss,
  timeout/fencing and restored-deployment/health tests; coordinated installation,
  readiness, all-writer migration, consumer opt-in and authorized live rollout.

Until that work is explicitly resumed and proved, the notice must remain a
warning-only visibility feature. A green notice, prerequisite CI or this patch
release cannot mark #167 or the complex guard complete.
