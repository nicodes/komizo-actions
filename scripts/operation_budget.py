#!/usr/bin/env python3
"""Retain the original GitHub job deadline across trusted operations and separate readiness."""
import argparse
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from authorize import api
import submission

LABELS = {'deliver': 'Deliver exact release', 'effects': 'Apply verified Preview lifecycle', 'backup': 'Capture encrypted backup'}


def deadline(jobs, source, total_minutes, role, *, now=None, completed=False, phase_seconds=None):
    now = now or datetime.now(timezone.utc)
    if (type(total_minutes) is not int or not 1 <= total_minutes <= 180 or role not in LABELS
            or jobs.get('total_count') != len(jobs.get('jobs', []))):
        raise ValueError('operation budget requires complete actual job metadata and a finite original timeout')
    selected = [job for job in jobs['jobs'] if job.get('name', '').split(' / ')[-1] == LABELS[role]]
    if (len(selected) != 1 or selected[0].get('head_sha') != source
            or selected[0].get('status') != ('completed' if completed else 'in_progress')
            or completed and selected[0].get('conclusion') != 'success'):
        raise ValueError('operation budget differs from its actual trusted source job')
    raw = selected[0].get('started_at')
    if not isinstance(raw, str): raise ValueError('operation job start time is unavailable')
    started = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if started.tzinfo is None or started > now + timedelta(seconds=5):
        raise ValueError('operation job start time is invalid')
    result = started + timedelta(minutes=total_minutes)
    if phase_seconds is not None:
        if type(phase_seconds) is not int or not 1 <= phase_seconds <= 10800:
            raise ValueError('operation phase budget is invalid')
        result = min(result, now + timedelta(seconds=phase_seconds))
    if result <= now: raise ValueError('original operation deadline expired')
    return math.floor(result.timestamp()), max(1, math.floor((result - now).total_seconds()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', choices=sorted(LABELS), required=True)
    parser.add_argument('--total-minutes', type=int, required=True)
    parser.add_argument('--completed', action='store_true')
    parser.add_argument('--phase-seconds', type=int)
    args = parser.parse_args()
    try:
        submission.workflow(os.environ)
        repository, run, attempt = (os.environ[key] for key in ['GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'])
        jobs = api(f'repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100')
        absolute, seconds = deadline(jobs, os.environ['GITHUB_SHA'], args.total_minutes, args.job,
                                     completed=args.completed, phase_seconds=args.phase_seconds)
        with open(os.environ['GITHUB_ENV'], 'a') as output:
            output.write('KOMIZO_OPERATION_DEADLINE=' + str(absolute) + '\n')
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('seconds=' + str(seconds) + '\nminutes=' + str(math.ceil(seconds / 60)) + '\n')
    except (KeyError, OSError, ValueError, TypeError):
        parser.exit(1, 'Original source operation budget unavailable or expired; no mutation submitted.\n')


if __name__ == '__main__': main()
