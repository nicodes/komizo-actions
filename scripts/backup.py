#!/usr/bin/env python3
"""Capture sealed host backups and transport them using the pinned cookbook."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib

sys.dont_write_bytecode = True


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
    transport.refuse_storage_credentials(environment)
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


def restore_profile(root, tools, name, project):
    sys.path.insert(0, str(tools.resolve() / 'cookbook'))
    import operational_workflows
    path = operational_workflows.relative(root, 'cookbook.toml', exists=True)
    if not path.is_file() or path.stat().st_size > 262144:
        raise ValueError('restore profiles require bounded native cookbook data')
    config = tomllib.loads(path.read_text())
    data = operational_workflows.declaration(root, config)
    selected = [value for value in data.get('restore_profiles', []) if value['name'] == name]
    if len(selected) != 1 or selected[0]['project'] != project:
        raise ValueError('select a declared restore profile and its exact historical project')
    return operational_workflows.relative(root, selected[0]['config'], exists=True)


def prepared_cookbook(root):
    makefile = root / 'Makefile'
    if makefile.is_symlink() or not makefile.is_file() or makefile.stat().st_size > 65536:
        raise ValueError('run make setup for the declared cookbook before local restoration')
    pins = re.findall(r'^COOKBOOK_REF := ([a-f0-9]{40})$', makefile.read_text(), re.M)
    if len(pins) != 1:
        raise ValueError('local restoration requires one immutable generated cookbook pin')
    source = root / '.artifacts/cookbook' / pins[0]
    if source.is_symlink() or any(parent.is_symlink() for parent in source.parents):
        raise ValueError('prepared cookbook source must not traverse links')
    def git(*arguments):
        return subprocess.check_output(['git', '--no-replace-objects', '-c', 'core.hooksPath=' + os.devnull,
            '-c', 'core.fsmonitor=false', '-C', str(source), *arguments], text=True, timeout=30).strip()
    if (git('rev-parse', 'HEAD') != pins[0]
            or Path(git('rev-parse', '--show-toplevel')).resolve() != source.resolve()
            or git('status', '--porcelain', '--untracked-files=all', '--ignored=matching')):
        raise ValueError('prepare a clean exact cookbook with make setup')
    return source


def restore_report(value, project):
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or value.get('project') != project or value.get('published_ports') != []
            or value.get('database_integrity') != 'ok'
            or any(value.get(key) != 'passed' for key in ['database_boot', 'api_boot', 'frontend_artifact', 'cleanup'])):
        raise ValueError('local restore did not establish the original project, isolation, boot, integrity and cleanup assertions')
    return value


def restore_pair(root, project, config, tools, directory, key, *, pull=False):
    """Package only ciphertext, then retain a fully checked local restore report."""
    if (not key.is_absolute() or key.is_symlink() or not key.is_file()
            or key.stat().st_mode & 0o077 or not os.access(key, os.R_OK)):
        raise ValueError('set an absolute readable private local BACKUP_RECOVERY_KEY_FILE')
    if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents) or not directory.is_dir():
        raise ValueError('encrypted restore inputs must be a real local directory')
    if not config.is_file() or config.is_symlink():
        raise ValueError('restore profile configuration must be a regular source file')
    native = helper(tools, 'restore-drill')
    if native.load_config(config)['project'] != project:
        raise ValueError('restore configuration differs from the selected historical project')
    reports = root / '.artifacts/restore'
    if any(path.is_symlink() for path in [reports, *reports.parents]):
        raise ValueError('restore reports must not traverse links')
    reports.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = reports / 'report.json'
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError('restore report must be a regular local artifact')
    with tempfile.TemporaryDirectory(prefix='restore-pair-') as temporary, transport.signals():
        temporary = Path(temporary)
        archive = temporary / 'export.tar'
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as output, tarfile.open(fileobj=output, mode='w') as packed:
                for name, maximum in [('snapshot.cms', 17 * 1024 ** 3), ('receipt.json', 64 * 1024)]:
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                    with os.fdopen(descriptor, 'rb') as incoming:
                        info = os.fstat(incoming.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                            raise ValueError('restore inputs must be bounded regular encrypted files')
                        entry = tarfile.TarInfo(name)
                        entry.size, entry.mode = info.st_size, 0o600
                        packed.addfile(entry, incoming)
        finally:
            os.close(parent)
        output = temporary / 'report.json'
        command = [sys.executable, str(tools / 'helpers/restore-drill.py'), '--project', project,
                   '--config', str(config), '--export', str(archive), '--key', str(key)]
        if pull:
            command.append('--pull')
        environment = dict(os.environ)
        environment.pop('BACKUP_RECOVERY_KEY_FILE', None)
        with output.open('xb') as report:
            os.fchmod(report.fileno(), 0o600)
            child = subprocess.Popen(command, stdout=report, env=environment)
            try:
                status = child.wait(timeout=900)
                if status:
                    raise SystemExit(status if status > 0 else 128 - status)
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=180)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
        if output.stat().st_size > 1048576:
            raise ValueError('restore report exceeds the bounded native protocol')
        value = restore_report(json.loads(output.read_text()), project)
        with tempfile.NamedTemporaryFile(prefix='.report-', dir=reports, mode='w', delete=False) as final:
            pending = Path(final.name)
            try:
                os.fchmod(final.fileno(), 0o600)
                json.dump(value, final, indent=2)
                final.write('\n')
                final.flush()
                os.fsync(final.fileno())
                os.replace(pending, destination)
            finally:
                pending.unlink(missing_ok=True)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['export', 'upload', 'restore'])
    parser.add_argument('--project', required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--tools-root', type=Path)
    parser.add_argument('--restore-config', type=Path)
    parser.add_argument('--profile', help='explicit native local restore profile')
    parser.add_argument('--pull', action='store_true', help='pull the authenticated exact restore images')
    args, extra = parser.parse_known_args()
    try:
        config = declaration(args.config, args.project)
        if args.tools_root is None:
            if args.operation == 'restore' and args.profile:
                args.tools_root = prepared_cookbook(Path.cwd())
            elif os.environ.get('TOOLS_ROOT'):
                args.tools_root = Path(os.environ['TOOLS_ROOT'])
            else:
                raise ValueError('a pinned cookbook root is required')
        if args.operation != 'restore' and (args.profile or args.pull):
            raise ValueError('restore options belong only to explicit local restoration')
        if args.operation == 'export':
            if extra:
                raise ValueError('export accepts no additional arguments')
            export(args.project, config, args.tools_root, args.directory or Path('.artifacts/backup'), os.environ)
        elif args.operation == 'upload':
            if extra:
                raise ValueError('upload accepts no additional arguments')
            upload(args.project, args.tools_root, args.directory or Path('.artifacts/backup'))
        else:
            key = Path(os.environ.get('BACKUP_RECOVERY_KEY_FILE', ''))
            if not key.is_absolute():
                raise ValueError('set an absolute local BACKUP_RECOVERY_KEY_FILE')
            if args.profile:
                if extra or args.restore_config is not None:
                    raise ValueError('a named native profile selects its configuration and encrypted pair')
                root = Path.cwd()
                selected = restore_profile(root, args.tools_root, args.profile, args.project)
                restore_pair(root, args.project, selected, args.tools_root,
                             args.directory or root / '.artifacts/restore/download', key, pull=args.pull)
            else:
                subprocess.run([sys.executable, str(args.tools_root / 'helpers/restore-drill.py'),
                                '--project', args.project, '--config', str(args.restore_config or Path('deploy/restore.json')),
                                '--key', str(key), *(['--pull'] if args.pull else []), *extra], check=True)
    except (OSError, RuntimeError, AttributeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
