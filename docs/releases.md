# Manual releases

The artifact is repository source, not a built archive. Releases have two
explicitly manual stages in **Release** (`workflow_dispatch`, run on `main`).
There is no automatic publication after merge and no direct push to `main`.
Existing branch protection and required PR CI remain the merge boundary.

1. Choose an unused canonical `vX.Y.Z` and the full current `main` source SHA.
   Dispatch **prepare** with `version` and `sha`. Unlike the old bump selector,
   these values are explicit and must not change on retries. Prepare fetches
   tags without force and fails on network errors or conflicting tags. It
   creates only `release/vX.Y.Z`, with a deterministic pin-only commit. Its
   commit message records the version, source SHA and intended tree. No tag or
   release is created.
2. Copy the printed `gh pr create` command and run it with your existing human
   GitHub identity. The workflow does not create PRs and has no PR write
   permission. Wait for protected PR CI and merge through the normal protected
   path. Merge, squash and rebase are supported; do not edit the candidate.
3. Obtain the PR's actual `merge_commit_sha` from the API, not its old head or
   synthetic merge ref. Wait for successful **CI** on that exact `main` push.
   Separately dispatch **publish**, providing the same `version`, that exact
   merged `sha`, and the merged candidate `pr` number.

Publication verifies the same-repository merged PR, recorded metadata,
canonical sibling pins, sibling existence and exact intended tree. The merged
source tree must equal the generated tree: changes incorporated during merge
fail closed. Reprepare a new candidate/version from current main when drift
requires it; never force-update a candidate branch or tag. Prepare retries with
identical inputs produce the same commit; prepare requires its source still be
current main. Publish can target an older merged commit still on main, not the
moving tip. No bump is recalculated at publication.

The gate requires the repository's CI workflow ID/path, a completed successful
`push` run on `main` at the exact merged SHA, and a successful linked `ci` job
and check suite from GitHub Actions. Unrelated checks named `ci`, PR checks,
dispatch runs and stale runs are not substitutes. The shared test action then
runs again from the exact merged source, before revalidation and publication.
Use the workflow for publication; invoking the Python publisher directly does
not itself run the shared test action.

Annotated tags are never forced, moved or deleted by this automation. An
existing matching tag without a release resumes creation using `--verify-tag`;
an existing completed release with the matching tag is a no-op (notes are not
edited). Wrong identities, draft/prerelease records, permission failures and
network failures stop publication. Only explicit HTTP 404 means an absent
release. Lost responses and races are followed by fresh identity checks.
Automation is serialized; administrators can still change tags or editable
GitHub release records outside this workflow. A commit SHA remains the stronger
identity boundary.

Consumers must explicitly update their version or SHA pins. Nothing here
changes runtime actions, secret transport or host admission. Between releases,
`deploy@main` still uses its recorded sibling pins, not moving main siblings.

For local candidate inspection only (clean checkout, configured `origin`):

```sh
sh scripts/release.sh prepare vX.Y.Z <full-current-main-source-sha>
```

This prints the PR command but does not push without `--push`. It does not
check out or modify the working tree. No credentials beyond the existing
workflow token and the operator's existing PR identity are required.
