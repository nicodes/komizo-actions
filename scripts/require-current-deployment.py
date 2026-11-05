#!/usr/bin/env python3
"""Refuse an obsolete queued deployment before connecting to its host."""
import os
import re
import subprocess


def require_current(expected, current):
    if not all(re.fullmatch(r'[a-f0-9]{40}', value) for value in (expected, current)):
        raise ValueError('Deployment requires exact source identities')
    if expected != current:
        raise ValueError('Main has advanced; refusing obsolete deployment before host effects')


def main():
    if os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('Production deployment requires main')
    repository = os.environ['GITHUB_REPOSITORY']
    if (not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repository)
            or repository.rsplit('/', 1)[-1] in ('.', '..')):
        raise ValueError('Invalid deployment repository')
    result = subprocess.run(['gh', 'api', f'repos/{repository}/commits/main', '--jq', '.sha'],
                            check=True, capture_output=True, text=True, timeout=30)
    require_current(os.environ['GITHUB_SHA'], result.stdout.strip())
    print('Current main admitted to the serialized host phase')


if __name__ == '__main__':
    main()
