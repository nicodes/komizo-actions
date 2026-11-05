#!/usr/bin/env python3
"""Capture sealed host backups and transport them using the pinned cookbook."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess


def helper(tools, name):
    spec = importlib.util.spec_from_file_location('komizo_backup_' + name, tools / 'helpers' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(receipt, now=None):
    value = receipt.get('verified_at')
    if isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError as error:
            raise ValueError('invalid receipt verified_at') from error
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
    else:
        result = now or datetime.now(timezone.utc)
    return result.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def declaration(path, project):
    if not re.fullmatch('[a-z][a-z0-9-]{0,62}', project):
        raise ValueError('invalid backup project')
    value = json.loads(path.read_text()) if path else {}
    if not isinstance(value, dict) or set(value) - {'exporter', 'proof_exporter', 'first_deployment'}:
        raise ValueError('invalid backup declaration')
    for key in ('exporter', 'proof_exporter'):
        if key in value and value[key] not in {f'export-{project}-backup', f'export-backup-{project}'}:
            raise ValueError('backup exporter must name this project fixed host command')
    absent = value.get('first_deployment', [])
    if not isinstance(absent, list) or len(set(absent)) != len(absent) or not set(absent) <= {
            'database_volume_absent', 'media_volume_absent', 'containers_absent'}:
        raise ValueError('invalid first deployment absence policy')
    return value



spec = importlib.util.spec_from_file_location('komizo_backup_transport', Path(__file__).with_name('backup_transport.py'))
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


def export(project, config, tools, directory, environment):
    frame = environment.get('KOMIZO_BACKUP_PROOF_FILE')
    command = (config.get('proof_exporter') if frame else None) or config.get('exporter', f'export-{project}-backup')

    def receive(archive, stage, descriptors):
        with archive.open('rb') as stream:
            is_absence = stream.read(1) == b'{'
        if is_absence:
            if not config.get('first_deployment') or archive.stat().st_size > 1024:
                raise ValueError('unexpected or oversized initialization proof')
            expected = {'version': 1, 'project': project, 'first_deployment': True,
                        **dict.fromkeys(config['first_deployment'], True)}
            actual = json.loads(archive.read_text())
            if actual != expected or any(type(actual[key]) is not type(value) for key, value in expected.items()):
                raise ValueError('invalid first deployment absence proof')
            destination = stage / 'first-deployment.json'
            with destination.open('x') as output:
                output.write(json.dumps(actual, indent=2) + '\n')
                os.fchmod(output.fileno(), 0o600)
            return 'first-deployment.json'
        helper(tools, 'receive-backup').receive(archive, stage / 'export')
        return 'export'

    with transport.signals():
        role = transport.capture(Path.cwd(), directory, project, command, receive,
            proof=Path(frame) if frame and config.get('proof_exporter') else None)
    if environment.get('GITHUB_OUTPUT'):
        with open(environment['GITHUB_OUTPUT'], 'a') as output:
            output.write('has-export=' + str(role == 'export').lower() + '\n')


def upload(project, tools, directory):
    receipt = directory / 'export/receipt.json'
    if receipt.is_symlink() or not receipt.is_file():
        raise ValueError('upload requires a received encrypted backup receipt')
    taken_at = stamp(json.loads(receipt.read_text()))
    subprocess.run([os.sys.executable, str(tools / 'helpers/upload-backup.py'), '--slug', project,
                    '--cms', str(directory / 'export/snapshot.cms'), '--receipt', str(receipt),
                    '--taken-at', taken_at], check=True, timeout=600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['export', 'upload', 'restore'])
    parser.add_argument('--project', required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--directory', type=Path, default=Path('.artifacts/backup'))
    parser.add_argument('--tools-root', type=Path, default=os.environ.get('TOOLS_ROOT'))
    parser.add_argument('--restore-config', type=Path, default=Path('deploy/restore.json'))
    args, extra = parser.parse_known_args()
    try:
        config = declaration(args.config, args.project)
        if args.tools_root is None:
            raise ValueError('a pinned cookbook root is required')
        if args.operation == 'export':
            if extra:
                raise ValueError('export accepts no additional arguments')
            export(args.project, config, args.tools_root, args.directory, os.environ)
        elif args.operation == 'upload':
            if extra:
                raise ValueError('upload accepts no additional arguments')
            upload(args.project, args.tools_root, args.directory)
        else:
            key = Path(os.environ.get('BACKUP_RECOVERY_KEY_FILE', ''))
            if not key.is_absolute():
                raise ValueError('set an absolute local BACKUP_RECOVERY_KEY_FILE')
            subprocess.run([os.sys.executable, str(args.tools_root / 'helpers/restore-drill.py'),
                            '--project', args.project, '--config', str(args.restore_config),
                            '--key', str(key), *extra], check=True)
    except (OSError, RuntimeError, AttributeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
