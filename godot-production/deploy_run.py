#!/usr/bin/env python3
"""Admit the Deploy run that a separate public verification follows and read its accepted public payload identity.

The separate workflow is triggered by a completed Deploy run (workflow_run) or dispatched by hand. It never sees the
in-run release packet, so it takes the payload identity that the one-job delivery retained as a small artifact of its
own run. The run it reads is admitted by the same rules as the other workflow_run verification: this repository's own
successful Deploy of the current main revision, identified by its workflow path and holding a successful Deploy job.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from workflow_run import read_event

SHA = re.compile('[a-f0-9]{40}')
RELEASE = re.compile('[a-f0-9]{64}')
REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
FILENAME = 'public-release.json'
KIND = 'komizo.public-release'
# The generated caller that may follow a Deploy run; its file name derives from its title in the Cookbook.
CALLER = '.github/workflows/verify-public-release.yml'
MAXIMUM = 4096


def operational():
    import operational as module
    return module


def artifact_name(revision, run, attempt):
    return f'public-release-{revision}-{run}-{attempt}'


def admit(environment, lookup):
    """Return (run id, attempt, artifact name or '') of the Deploy run this verification applies to.

    The artifact name is empty when that run left no public payload identity, which is how a deployment that did not
    become ready looks; a manual dispatch refuses it, a triggered run has nothing to verify.
    """
    repository = environment.get('GITHUB_REPOSITORY', '')
    revision = environment.get('GITHUB_SHA', '')
    event_name = environment.get('GITHUB_EVENT_NAME')
    if (not REPOSITORY.fullmatch(repository) or not SHA.fullmatch(revision) or environment.get('GITHUB_REF') != 'refs/heads/main'
            or event_name not in {'workflow_run', 'workflow_dispatch'} or environment.get('GITHUB_WORKFLOW_SHA') != revision
            or environment.get('GITHUB_WORKFLOW_REF') != repository + '/' + CALLER + '@refs/heads/main'):
        raise ValueError('public verification after Deploy requires its exact trusted-main caller')
    tip = json.loads(lookup(f'repos/{repository}/git/ref/heads/main')).get('object', {}).get('sha')
    if tip != revision:
        raise ValueError('public verification applies only while its deployed revision is current main')
    driver = operational()
    if event_name == 'workflow_run':
        run, attempt = driver.deployment_run(read_event(environment, maximum=1 << 20), repository, revision)
        if not driver.successful_deployment(repository, revision, lookup, (run, attempt)):
            raise ValueError('public verification requires a successful deployment of this exact Deploy run')
    else:
        run, attempt = latest(driver, repository, revision, lookup)
    found = json.loads(lookup(f'repos/{repository}/actions/runs/{run}/artifacts?per_page=100'))
    rows = found.get('artifacts')
    if not isinstance(rows, list) or type(found.get('total_count')) is not int or found['total_count'] != len(rows):
        raise ValueError('complete bounded artifact records of the Deploy run are required')
    name = artifact_name(revision, run, attempt)
    matches = [row for row in rows if isinstance(row, dict) and row.get('name') == name and row.get('expired') is False]
    if len(matches) > 1:
        raise ValueError('the Deploy run retained its public payload identity more than once')
    if not matches and event_name == 'workflow_dispatch':
        raise ValueError('the latest successful Deploy of this revision retained no public payload identity')
    return run, attempt, name if matches else ''


def latest(driver, repository, revision, lookup):
    """The newest successful Deploy run of this exact revision, by the same checks as a triggered run."""
    runs = driver.deployment_runs(repository, revision, lookup)
    candidates = sorted((run for run in runs
                         if type(run.get('id')) is int and type(run.get('run_attempt')) is int),
                        key=lambda run: (run['id'], run['run_attempt']), reverse=True)
    for run in candidates:
        if driver.successful_deployment(repository, revision, lookup, (run['id'], run['run_attempt'])):
            return run['id'], run['run_attempt']
    raise ValueError('public verification requires a successful Deploy run of this exact revision')


def read(directory, revision):
    """The accepted release identity from the downloaded artifact, bound to the verified revision."""
    directory = Path(directory)
    files = sorted(directory.iterdir()) if directory.is_dir() and not directory.is_symlink() else []
    if [path.name for path in files] != [FILENAME] or files[0].is_symlink() or not files[0].is_file():
        raise ValueError('the public payload identity artifact must hold exactly its one file')
    raw = files[0].read_bytes()
    if len(raw) > MAXIMUM:
        raise ValueError('the public payload identity exceeds its bound')
    record = json.loads(raw)
    if (not isinstance(record, dict) or set(record) != {'kind', 'version', 'source_commit', 'release_id'}
            or record['kind'] != KIND or type(record['version']) is not int or record['version'] != 1
            or record['source_commit'] != revision or not isinstance(record['release_id'], str)
            or not RELEASE.fullmatch(record['release_id'])):
        raise ValueError('the public payload identity names another source or is malformed')
    return record['release_id']


def lookup(path):
    return subprocess.check_output(['gh', 'api', path], text=True, timeout=30)


def output(name, value):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(name + '=' + value + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['admit', 'read'])
    parser.add_argument('--directory', default='')
    args = parser.parse_args()
    try:
        if args.operation == 'admit':
            run, attempt, name = admit(os.environ, lookup)
            output('run-id', str(run))
            output('run-attempt', str(attempt))
            output('artifact', name)
        else:
            output('release-id', read(args.directory, os.environ['GITHUB_SHA']))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
