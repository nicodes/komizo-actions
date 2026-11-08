#!/usr/bin/env python3
"""Read declaration data through the accepted cookbook without loading product code."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tomllib

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import recipe_context
import recipe_data


def plan(root, project, path, *, context=None):
    if context is not None:
        delivery_workflows=context.module('delivery_workflows')
        config=recipe_data.load(root,context.cookbook,context.module('configuration'))
    else:
        sys.path.insert(0,str(Path('_delivery_cookbook').resolve()/'cookbook'))
        import delivery_workflows
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
            'allowed-products': json.dumps([project]),
            'readiness-operation': str(data.get('readiness_operation', False)).lower(),
            'expected-secret-generations': json.dumps(data.get('expected_secret_generations', {}), sort_keys=True)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cookbook-root',type=Path)
    parser.add_argument('--cookbook-ref')
    parser.add_argument('--tools-root',type=Path)
    args=parser.parse_args()
    try:
        context=None
        supplied_tools_ref=os.environ.get('TOOLS_REF','')
        if args.cookbook_root is not None or args.cookbook_ref is not None:
            if args.cookbook_root is None or args.cookbook_ref is None:
                raise ValueError('delivery requires its complete Cookbook source contract')
            context=recipe_context.accepted_context(args.cookbook_root,args.cookbook_ref,
                lambda path:json.loads(subprocess.check_output(['gh','api',path],text=True,timeout=30)),args.tools_root)
            if supplied_tools_ref not in {'',context.tools_revision}:
                raise ValueError('delivery helper identity differs from the Cookbook dependency')
        elif args.tools_root is not None:
            raise ValueError('an independent helper location requires the Cookbook source contract')
        values = plan(Path.cwd(), os.environ['PROJECT'], os.environ['DECLARATION'],context=context)
        if context is not None:
            values.update({'cookbook-root':str(context.cookbook),'tools-root':str(context.tools)})
            # Later separately timed backup/readiness actions use these same
            # authenticated source locations; no helper ref is caller-selected.
            with open(os.environ['GITHUB_ENV'],'a') as output:
                environment=context.environment()
                for key in ['COOKBOOK_ROOT','COOKBOOK_REF','TOOLS_ROOT','TOOLS_REF']:
                    delimiter=secrets.token_hex(16)
                    output.write(f'{key}<<{delimiter}\n{environment[key]}\n{delimiter}\n')
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            for key, value in values.items():
                delimiter = secrets.token_hex(16)
                output.write(f'{key}<<{delimiter}\n{value}\n{delimiter}\n')
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error


if __name__=='__main__':main()
