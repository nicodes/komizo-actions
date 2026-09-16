#!/usr/bin/env python3
"""Prepare pin-only candidates; validate and publish exact merged source artifacts."""

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

REPO = "nicodes/komizo-actions"
WORKFLOW_ID = 325265928
APP_ID = 15368
SUBACTIONS = {"connect", "publish-config", "set-secrets", "activate", "health-check"}
SHA = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
PREFIX = "Release-Candidate: "


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args, input=None, env=None, check=True):
    result = subprocess.run(args, input=input, text=True, capture_output=True, env=env)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result


def git(*args, **kwargs):
    return run("git", *args, **kwargs).stdout.strip()


def api(path, optional=False):
    result = run("gh", "api", f"repos/{REPO}/{path}", check=False)
    if result.returncode:
        # gh reports HTTP status on stderr. Only an explicit 404 is absence.
        if optional and re.search(r"\(HTTP 404\)", result.stderr):
            return None
        raise RuntimeError(f"GitHub API failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def pages(path, key=None):
    values = []
    for page in range(1, 1001):
        separator = "&" if "?" in path else "?"
        data = api(f"{path}{separator}per_page=100&page={page}")
        batch = data[key] if key else data
        values.extend(batch)
        if len(batch) < 100:
            return values
    raise RuntimeError("API pagination limit exceeded")


def refresh():
    # Non-force: conflicting local tags and all network failures are fatal.
    git("fetch", "--tags", "origin", "refs/heads/main")


def canonical_tree(source):
    """Construct the only allowed diff in an isolated index, never the worktree."""
    paths = git("ls-tree", "-r", "--name-only", source).splitlines()
    pattern = re.compile(r"nicodes/komizo-actions/([^\s@]+)@([A-Za-z0-9._/-]+)")
    found = set()
    with tempfile.TemporaryDirectory() as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
        git("read-tree", source, env=env)
        for path in paths:
            if not re.fullmatch(r"[^/]+/action.yml", path):
                continue
            original = run("git", "show", f"{source}:{path}").stdout

            def pin(match):
                sibling = match[1]
                require(sibling in SUBACTIONS, f"unapproved sibling: {sibling}")
                require(f"{sibling}/action.yml" in paths, f"missing sibling: {sibling}")
                found.add(sibling)
                return f"{REPO}/{sibling}@{source}"

            updated = pattern.sub(pin, original)
            if updated != original:
                entry = git("ls-tree", source, "--", path).split()
                require(entry[0] == "100644", "composed action must be a regular file")
                blob = git("hash-object", "-w", "--stdin", input=updated)
                git("update-index", "--cacheinfo", f"100644,{blob},{path}", env=env)
        require(found == SUBACTIONS, "expected all five composed siblings")
        return git("write-tree", env=env)


def metadata(version, source, tree):
    return PREFIX + json.dumps(
        dict(version=version, source=source, tree=tree), sort_keys=True
    )


def prepare(version, source, push=False):
    require(not git("status", "--porcelain"), "working tree must be clean")
    refresh()
    require(
        git("rev-parse", "FETCH_HEAD") == source, "prepare source must be fetched main"
    )
    require(not git("tag", "--list", version), "version tag already exists")
    tree = canonical_tree(source)
    require(
        tree != git("rev-parse", f"{source}^{{tree}}"),
        "candidate must change composed pins",
    )
    message = f"release {version}: pin composed actions\n\n{metadata(version, source, tree)}\n"
    timestamp = git("show", "-s", "--format=%cI", source)
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="github-actions[bot]",
        GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
        GIT_COMMITTER_NAME="github-actions[bot]",
        GIT_COMMITTER_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
        GIT_AUTHOR_DATE=timestamp,
        GIT_COMMITTER_DATE=timestamp,
    )
    commit = git("commit-tree", tree, "-p", source, input=message, env=env)
    branch = f"release/{version}"
    local = run("git", "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    require(
        local.returncode != 0 or local.stdout.strip() == commit,
        "local candidate branch differs",
    )
    remote = git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    require(
        not remote or remote.split()[0] == commit, "remote candidate branch differs"
    )
    git(
        "update-ref",
        f"refs/heads/{branch}",
        commit,
        local.stdout.strip() if local.returncode == 0 else "0" * 40,
    )
    if push:
        # Never main, never force. A concurrent writer is checked after push too.
        git("push", "origin", f"{commit}:refs/heads/{branch}")
        require(
            git("ls-remote", "--heads", "origin", f"refs/heads/{branch}").split()[0]
            == commit,
            "candidate branch changed during push",
        )
    print(f"Candidate {commit}; source {source}; tree {tree}")
    print(
        f"gh pr create --repo {REPO} --base main --head {branch} --title 'release {version}' --body 'Pin composed actions to {source}.'"
    )


def validate(version, number, merged):
    refresh()
    pr = api(f"pulls/{number}")
    require(pr["merged"] is True and pr["state"] == "closed", "PR is not merged")
    require(
        pr["base"]["repo"]["full_name"] == REPO and pr["base"]["ref"] == "main",
        "wrong PR base",
    )
    require(
        pr["head"]["repo"]["full_name"] == REPO
        and pr["head"]["ref"] == f"release/{version}",
        "wrong PR head",
    )
    require(pr["merge_commit_sha"] == merged, "PR merged SHA differs")
    head = pr["head"]["sha"]
    require(SHA.fullmatch(head), "invalid candidate head")
    git("fetch", "origin", head, merged)
    lines = git("show", "-s", "--format=%B", head).splitlines()
    records = [line for line in lines if line.startswith(PREFIX)]
    require(len(records) == 1, "missing or ambiguous candidate metadata")
    record = json.loads(records[0][len(PREFIX) :])
    require(
        set(record) == {"version", "source", "tree"} and record["version"] == version,
        "candidate metadata/version differs",
    )
    source = record["source"]
    require(
        SHA.fullmatch(source) and SHA.fullmatch(record["tree"]),
        "invalid metadata identity",
    )
    git("fetch", "origin", source)
    require(
        git("show", "-s", "--format=%P", head) == source,
        "candidate is not one pin-only commit",
    )
    tree = canonical_tree(source)
    require(
        tree
        == record["tree"]
        == git("rev-parse", f"{head}^{{tree}}")
        == git("rev-parse", f"{merged}^{{tree}}"),
        "candidate or merged tree drift; reprepare",
    )
    # Fetch main separately: FETCH_HEAD above referred to the immutable source.
    git("fetch", "origin", "refs/heads/main")
    require(
        run(
            "git", "merge-base", "--is-ancestor", merged, "FETCH_HEAD", check=False
        ).returncode
        == 0,
        "merged candidate is not on main",
    )
    require(
        run(
            "git", "merge-base", "--is-ancestor", source, merged, check=False
        ).returncode
        == 0,
        "recorded source is not on merged main history",
    )
    trusted_ci(merged)
    print(f"Validated {version} PR #{number} merged {merged} tree {tree}")


def trusted_ci(merged):
    workflow = api(f"actions/workflows/{WORKFLOW_ID}")
    require(
        workflow["id"] == WORKFLOW_ID
        and workflow["path"] == ".github/workflows/ci.yml",
        "wrong CI workflow",
    )
    runs = pages(
        f"actions/workflows/{WORKFLOW_ID}/runs?head_sha={merged}&event=push",
        "workflow_runs",
    )
    for item in runs:
        run_id = item["id"]
        evidence = api(f"actions/runs/{run_id}")
        if not (
            evidence["workflow_id"] == WORKFLOW_ID
            and evidence["path"] == ".github/workflows/ci.yml"
            and evidence["repository"]["full_name"] == REPO
            and evidence["head_repository"]["full_name"] == REPO
            and evidence["event"] == "push"
            and evidence["head_branch"] == "main"
            and evidence["head_sha"] == merged
            and evidence["status"] == "completed"
            and evidence["conclusion"] == "success"
        ):
            continue
        suite_id = evidence["check_suite_id"]
        suite = api(f"check-suites/{suite_id}")
        if not (
            suite["app"]["id"] == APP_ID
            and suite["head_sha"] == merged
            and suite["head_branch"] == "main"
            and suite["status"] == "completed"
            and suite["conclusion"] == "success"
        ):
            continue
        jobs = pages(
            f"actions/runs/{run_id}/attempts/{evidence['run_attempt']}/jobs", "jobs"
        )
        checks = pages(f"check-suites/{suite_id}/check-runs", "check_runs")
        for job in jobs:
            for check in checks:
                if (
                    job["name"] == check["name"] == "ci"
                    and job["status"] == check["status"] == "completed"
                    and job["conclusion"] == check["conclusion"] == "success"
                    and job["head_sha"] == check["head_sha"] == merged
                    and job["run_id"] == run_id
                    and job["check_run_url"] == check["url"]
                    and check["app"]["id"] == APP_ID
                    and check["check_suite"]["id"] == suite_id
                ):
                    return
    raise RuntimeError(
        "no exact successful trusted main push CI run with linked ci check"
    )


def remote_tag(version, merged):
    text = git(
        "ls-remote",
        "--tags",
        "origin",
        f"refs/tags/{version}",
        f"refs/tags/{version}^{{}}",
    )
    refs = dict(line.split()[::-1] for line in text.splitlines())
    if not refs:
        return False
    require(
        f"refs/tags/{version}" in refs
        and refs.get(f"refs/tags/{version}^{{}}") == merged,
        "existing tag is not an annotated tag for the merged SHA",
    )
    return True


def completed_release(version, merged):
    release = api(f"releases/tags/{version}", optional=True)
    if release is None:
        return False
    require(
        release["tag_name"] == version
        and not release["draft"]
        and not release["prerelease"],
        "existing release is not the expected completed release",
    )
    require(remote_tag(version, merged), "release has no matching tag")
    return True


def publish(version, number, merged):
    require(
        git("rev-parse", "HEAD") == merged and not git("status", "--porcelain"),
        "publish requires clean exact merged checkout after shared tests",
    )
    validate(version, number, merged)
    if completed_release(version, merged):
        print("Release already complete; no changes")
        return
    if not remote_tag(version, merged):
        local = git("tag", "--list", version)
        if local:
            require(
                git("cat-file", "-t", f"refs/tags/{version}") == "tag"
                and git("rev-parse", f"{version}^{{}}") == merged,
                "local tag identity differs",
            )
        else:
            git("tag", "-a", version, merged, "-m", f"komizo-actions {version}")
        result = run(
            "git",
            "push",
            "origin",
            f"refs/tags/{version}:refs/tags/{version}",
            check=False,
        )
        # Re-read even after failure: an accepted push can lose its response.
        require(
            remote_tag(version, merged),
            f"tag push did not establish expected identity: {result.stderr}",
        )
    result = run(
        "gh",
        "release",
        "create",
        version,
        "--repo",
        REPO,
        "--verify-tag",
        "--target",
        merged,
        "--title",
        version,
        "--generate-notes",
        check=False,
    )
    require(
        completed_release(version, merged),
        f"release creation did not complete: {result.stderr}",
    )
    print(f"Published {version} at {merged}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "validate", "publish"])
    parser.add_argument("version")
    parser.add_argument(
        "sha", help="full source SHA (prepare) or exact merged SHA (validate/publish)"
    )
    parser.add_argument("--pr", type=int)
    parser.add_argument(
        "--push", action="store_true", help="push candidate branch only"
    )
    args = parser.parse_args()
    require(VERSION.fullmatch(args.version), "version must be canonical vX.Y.Z")
    require(SHA.fullmatch(args.sha), "SHA must be 40 lowercase hex characters")
    if args.stage == "prepare":
        require(args.pr is None, "prepare does not accept PR")
        prepare(args.version, args.sha, args.push)
    else:
        require(
            args.pr is not None and args.pr > 0 and not args.push,
            "publish/validate requires PR and no --push",
        )
        (validate if args.stage == "validate" else publish)(
            args.version, args.pr, args.sha
        )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError, ValueError, TypeError) as error:
        raise SystemExit(f"error: {error}") from error
