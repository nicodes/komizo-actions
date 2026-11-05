#!/usr/bin/env python3
"""Read declaration data through the accepted cookbook without loading product code."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tomllib

tools = Path('_delivery_cookbook').resolve()
sys.path.insert(0, str(tools / 'cookbook'))
import delivery_workflows


def plan(root, project, path):
    config = tomllib.loads((root / 'cookbook.toml').read_text())
    if config.get('delivery') != {'definition': path}:
        raise ValueError('delivery input differs from its pinned recipe declaration')
    data = delivery_workflows.declaration(root, config)
    if data['project'] != project:
        raise ValueError('delivery project differs from its source declaration')
    readiness = data.get('readiness', '')
    if readiness and (not readiness.startswith('deploy/health/') or not readiness.endswith('.py')):
        raise ValueError('product readiness belongs in deploy/health')
    for name in [path, *([readiness] if readiness else []), *([data['backup']] if data.get('backup') else [])]:
        subprocess.run(['git', 'ls-files', '--error-unmatch', '--', name], check=True, stdout=subprocess.DEVNULL, timeout=10)
    if data.get('upload_backup') and not data.get('backup'):
        raise ValueError('backup upload requires explicit encrypted capture')
    health = [entry['url'] + ' ' + entry['contains'].replace('{revision}', os.environ['GITHUB_SHA'])
              if isinstance(entry, dict) else entry for entry in data['health_urls']]
    return {'health-urls': '\n'.join(health), 'backup': data.get('backup', ''),
            'upload': str(data.get('upload_backup', False)).lower(), 'readiness': readiness,
            'diagnostics': str(data.get('diagnostics', False)).lower(),
            'allowed-products': json.dumps([project])}


if __name__ == '__main__':
    try:
        values = plan(Path.cwd(), os.environ['PROJECT'], os.environ['DECLARATION'])
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            for key, value in values.items():
                delimiter = secrets.token_hex(16)
                output.write(f'{key}<<{delimiter}\n{value}\n{delimiter}\n')
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
