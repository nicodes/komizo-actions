#!/usr/bin/env python3
"""Release only the exact current main source after complete trusted CI acceptance."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

REPO = 'nicodes/komizo-actions'
WORKFLOW_ID = 325265928
APP_ID = 15368
SHA = re.compile(r'[a-f0-9]{40}\Z')
VERSION = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')

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
                    job["name"] == check["name"] == "Test"
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
        "no exact successful trusted main push CI run with linked Test check"
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


def validate(version, source):
    require(VERSION.fullmatch(version) and SHA.fullmatch(source), 'release requires an immutable version and full commit')
    require(os.environ.get('GITHUB_REPOSITORY', REPO) == REPO, 'release belongs to another repository')
    require(git('rev-parse', 'HEAD') == source and not git('status', '--porcelain', '--untracked-files=no'), 'release needs exact clean tracked source')
    git('fetch', '--no-tags', 'origin', 'refs/heads/main')
    require(git('rev-parse', 'FETCH_HEAD') == source, 'release source is no longer current main')
    trusted_ci(source)
    return source


def pack(version, source, directory=Path('.artifacts/release')):
    validate(version, source)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    archive = directory / ('komizo-actions-' + version + '.tar.gz')
    raw = subprocess.check_output(['git', 'archive', '--format=tar', source], timeout=30)
    require(0 < len(raw) <= 16 << 20, 'source archive exceeds its bound')
    with archive.open('xb') as output, gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
        compressed.write(raw)
    record = {'version': 1, 'repository': REPO, 'source_commit': source,
              'source_tree': git('rev-parse', source + '^{tree}'),
              'archive': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    (directory / 'release.json').write_text(json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n')
    print(archive)
    return archive


def publish(version, source, archive):
    validate(version, source)
    require(archive.is_file() and not archive.is_symlink() and archive.stat().st_size <= 16 << 20, 'release requires its exact regular source artifact')
    record_path = archive.parent / 'release.json'
    record = json.loads(record_path.read_bytes())
    require(record == {'version': 1, 'repository': REPO, 'source_commit': source,
                       'source_tree': git('rev-parse', source + '^{tree}'), 'archive': archive.name,
                       'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}, 'release artifact differs from its exact source record')
    # Verify the workflow attestation before creating any public release object.
    run('gh', 'attestation', 'verify', str(archive), '--repo', REPO, '--signer-workflow',
        REPO + '/.github/workflows/release.yml', '--source-digest', source)
    existing = completed_release(version, source)
    if not remote_tag(version, source):
        require(not git('tag', '--list', version), 'unexpected local version tag')
        git('tag', '-a', version, source, '-m', 'komizo-actions ' + version)
        run('git', 'push', 'origin', 'refs/tags/' + version + ':refs/tags/' + version, check=False)
        require(remote_tag(version, source), 'tag publication outcome is uncertain; inspect without moving it')
    validate(version, source)
    if not existing:
        run('gh', 'release', 'create', version, str(archive), str(record_path), '--repo', REPO,
            '--verify-tag', '--target', source, '--title', version, '--generate-notes', check=False)
    require(completed_release(version, source), 'release publication outcome is uncertain; inspect before retrying')
    published = api('releases/tags/' + version)
    expected = {archive.name: hashlib.sha256(archive.read_bytes()).hexdigest(),
                record_path.name: hashlib.sha256(record_path.read_bytes()).hexdigest()}
    assets = published.get('assets', [])
    require(len(assets) == len(expected) and {asset['name'] for asset in assets} == set(expected), 'release asset closure differs')
    for asset in assets:
        require(asset.get('digest') == 'sha256:' + expected[asset['name']], 'release asset bytes differ; do not overwrite or move the version')
    print('Published ' + version + ' at ' + source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['validate', 'pack', 'publish'])
    parser.add_argument('version')
    parser.add_argument('sha')
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    if args.stage == 'validate': validate(args.version, args.sha)
    elif args.stage == 'pack': pack(args.version, args.sha)
    else:
        require(args.archive is not None, 'publication requires its attested source archive')
        publish(args.version, args.sha, args.archive)


if __name__ == '__main__':
    try: main()
    except (RuntimeError, ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise SystemExit('release refused: ' + str(error)) from None
