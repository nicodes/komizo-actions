"""Bind actual GitHub jobs without replacing the trusted main controller identity."""
import json
from pathlib import Path
import re

import submission

SHA = re.compile(r'[a-f0-9]{40}\Z')


def read_event(environment, maximum=4 << 20):
    path = Path(environment.get('GITHUB_EVENT_PATH', ''))
    if path.is_symlink() or not path.is_file():
        raise ValueError('workflow event must be bounded runner metadata')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('workflow event must be bounded runner metadata')
    return json.loads(raw)


def source(metadata, environment, event=None):
    submission.workflow(environment)
    repository = environment['GITHUB_REPOSITORY']
    workflow = environment.get('GITHUB_WORKFLOW_REF', '')
    prefix, suffix = repository + '/', '@refs/heads/main'
    if (not workflow.startswith(prefix) or not workflow.endswith(suffix)
            or not re.fullmatch(r'\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml', workflow[len(prefix):-len(suffix)])
            or environment.get('GITHUB_WORKFLOW_SHA') != environment['GITHUB_SHA']):
        raise ValueError('actual jobs require their exact trusted main caller')
    if (not isinstance(metadata, dict) or type(metadata.get('id')) is not int
            or str(metadata['id']) != environment['GITHUB_RUN_ID']
            or type(metadata.get('run_attempt')) is not int
            or str(metadata['run_attempt']) != environment['GITHUB_RUN_ATTEMPT']
            or metadata.get('event') != environment['GITHUB_EVENT_NAME']
            or metadata.get('path') != workflow[len(prefix):-len(suffix)]):
        raise ValueError('actual run differs from its caller, event or attempt')
    for key in ['repository', 'head_repository']:
        value = metadata.get(key, {})
        if (not isinstance(value, dict) or value.get('full_name') != repository
                or type(value.get('id')) is not int or str(value['id']) != environment['GITHUB_REPOSITORY_ID']):
            raise ValueError('actual run belongs to another source repository')
    head = metadata.get('head_sha')
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise ValueError('actual run has no immutable source')
    if environment['GITHUB_EVENT_NAME'] == 'pull_request_target':
        event = read_event(environment) if event is None else event
        if not isinstance(event, dict) or not isinstance(event.get('pull_request'), dict):
            raise ValueError('PR-target requires its triggering event')
        request = event.get('pull_request', {})
        if not isinstance(request.get('head'), dict) or not isinstance(request.get('base'), dict):
            raise ValueError('PR-target event has no head or base')
        if (type(event.get('number')) is not int or event['number'] < 1
                or request.get('number') != event['number']
                or request.get('head', {}).get('sha') != head
                or request.get('base', {}).get('ref') != 'main'):
            raise ValueError('actual PR-target source differs from its triggering event')
        for value in [event.get('repository', {}), request.get('head', {}).get('repo', {}), request.get('base', {}).get('repo', {})]:
            if (not isinstance(value, dict) or value.get('full_name') != repository or type(value.get('id')) is not int
                    or str(value['id']) != environment['GITHUB_REPOSITORY_ID']):
                raise ValueError('PR-target event belongs to another source repository')
    elif head != environment['GITHUB_SHA']:
        raise ValueError('actual run differs from its trusted main source')
    return head


def current(environment, lookup, *, event=None):
    # Authenticate the controller before making even read-only GitHub calls.
    submission.workflow(environment)
    repository, run, attempt = (environment[key] for key in ['GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'])
    prefix = f'repos/{repository}/actions/runs/{run}/attempts/{attempt}'
    metadata = lookup(prefix)
    head = source(metadata, environment, event)
    jobs = lookup(prefix + '/jobs?per_page=100')
    if (not isinstance(jobs, dict) or not isinstance(jobs.get('jobs'), list)
            or type(jobs.get('total_count')) is not int or not 1 <= jobs['total_count'] <= 100
            or jobs['total_count'] != len(jobs['jobs'])):
        raise ValueError('actual run requires complete bounded current-attempt jobs')
    identities = set()
    for job in jobs['jobs']:
        if (not isinstance(job, dict) or type(job.get('id')) is not int or job['id'] < 1
                or job['id'] in identities or job.get('run_id') != metadata['id']
                or type(job.get('run_id')) is not int or type(job.get('run_attempt')) is not int
                or job['run_attempt'] != metadata['run_attempt'] or job.get('head_sha') != head):
            raise ValueError('actual job belongs to another run, source or attempt')
        identities.add(job['id'])
    return metadata, jobs, head
