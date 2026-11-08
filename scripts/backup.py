#!/usr/bin/env python3
"""Capture sealed backups with pinned tools and source-owned restore profiles."""
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
sys.path.insert(0, str(Path(__file__).resolve().parent))


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
    if path and (path.suffix != '.toml' or path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
                 or not path.is_file() or path.stat().st_size > 65536):
        raise ValueError('backup source must be bounded regular TOML')
    value = tomllib.loads(path.read_text()) if path else {'version': 1}
    if (not isinstance(value, dict) or set(value) != {'version'} or type(value.get('version')) is not int or value['version'] != 1):
        raise ValueError('backup declaration requires native version 1; execution belongs to protected host policy')
    return value



spec = importlib.util.spec_from_file_location('komizo_backup_transport', Path(__file__).with_name('backup_transport.py'))
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


def export(project, config, tools, directory, environment):
    transport.refuse_storage_credentials(environment)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_submission
    def produce(stage, descriptors):
        return backup_submission.capture(project, stage, environment, pass_fds=descriptors)
    with transport.signals():
        role = transport.capture(Path.cwd(), directory, project, produce)
    if environment.get('GITHUB_OUTPUT'):
        with open(environment['GITHUB_OUTPUT'], 'a') as output:
            output.write('has-export=' + str(role == 'export').lower() + '\n')
            output.write('capture-status=' + {'export': 'ready', 'first-deployment.json': 'absent', 'stopped.json': 'skipped-stopped'}[role] + '\n')


def upload(project, tools, directory):
    receipt = directory / 'export/receipt.json'
    if receipt.is_symlink() or not receipt.is_file():
        raise ValueError('upload requires a received encrypted backup receipt')
    taken_at = stamp(json.loads(receipt.read_text()))
    subprocess.run([os.sys.executable, str(tools / 'helpers/upload-backup.py'), '--slug', project,
                    '--cms', str(directory / 'export/snapshot.cms'), '--receipt', str(receipt),
                    '--taken-at', taken_at], check=True, timeout=600)


def restore_profile(root, cookbook, name, project, *, context=None):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import recipe_data
    if context is not None:
        if cookbook.resolve() != context.cookbook.resolve():
            raise ValueError('restore profile differs from its verified Cookbook source')
        operational_workflows = context.module('operational_workflows')
    else:
        sys.path.insert(0, str(cookbook.resolve() / 'cookbook'))
        import operational_workflows
        recipe_data.checked_module(operational_workflows, cookbook.resolve(), 'operational_workflows')
    path = operational_workflows.relative(root, 'cookbook.toml', exists=True)
    if not path.is_file() or path.stat().st_size > 262144:
        raise ValueError('restore profiles require bounded native cookbook data')
    config = recipe_data.load(root, cookbook)
    data = operational_workflows.declaration(root, config)
    selected = [value for value in data.get('restore_profiles', []) if value['name'] == name]
    if len(selected) != 1 or selected[0]['project'] != project:
        raise ValueError('select a declared restore profile and its exact historical project')
    return operational_workflows.relative(root, selected[0]['config'], exists=True)


def discovery_module(name):
    # Both discovery protocols are immutable data-only SDK mirrors. A partial
    # new identity must never select the genuine historical Make protocol.
    spec = importlib.util.spec_from_file_location('backup_' + name,
        Path(__file__).resolve().parent / (name + '.py'))
    discovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(discovery)
    return discovery


def prepared_cookbook(root):
    discovery = discovery_module('cookbook_identity')
    source = root / 'mise.toml'
    if (source.exists() or source.is_symlink()) and discovery.declared(discovery.read_source(root)):
        return discovery.prepared(root)
    discovery = discovery_module('sdk_identity')
    return discovery.prepared(root)


def local_recipe_context(root, *, cookbook_root=None, cookbook_ref=None, tools_root=None):
    """Resolve prepared source offline before importing a restore recipe."""
    discovery = discovery_module('cookbook_identity')
    source = root / 'mise.toml'
    explicit = (cookbook_root is not None or cookbook_ref is not None
                or bool(os.environ.get('COOKBOOK_ROOT')) or bool(os.environ.get('COOKBOOK_REF')))
    declared = ((source.exists() or source.is_symlink())
                and discovery.declared(discovery.read_source(root)))
    if not declared:
        if explicit:
            raise ValueError('explicit Cookbook source requires its caller-declared Mise identity')
        return None
    revision = cookbook_ref or os.environ.get('COOKBOOK_REF')
    cookbook = discovery.prepared(root, revision,
        cookbook_root or os.environ.get('COOKBOOK_ROOT'))
    revision = discovery.source_identity(root)['revision']
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import recipe_context
    import recipe_data
    context = recipe_context.Context(cookbook, revision, tools_root or os.environ.get('TOOLS_ROOT'))
    if os.environ.get('TOOLS_REF') and os.environ['TOOLS_REF'] != context.tools_revision:
        raise ValueError('restore tools differ from the Cookbook-owned helper dependency')
    recipe_data.canonical_entrypoint(root, context.cookbook, revision, context.module('generate'))
    context.activate()
    return context


def restore_report(value, project):
    from release_packet import COMMIT, NUMBER
    from oci_registry import DIGEST
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or value.get('project') != project or value.get('published_ports') != []
            or value.get('database_integrity') != 'ok'
            or value.get('scope') != 'disposable authenticated snapshot restoration'
            or value.get('current_source_ci_acceptance') is not False
            or value.get('network') != 'none; shared isolated loopback only'
            or value.get('http_assertions') != {'api_health': 'passed', 'frontend_document': 'passed', 'unknown_host_refusal': 'passed'}
            or any(value.get(key) != 'passed' for key in ['database_restore', 'database_boot', 'api_boot', 'frontend_artifact', 'media_integrity', 'cleanup'])):
        raise ValueError('local restore did not establish the original project, isolation, boot, integrity and cleanup assertions')
    snapshot = value.get('snapshot_release')
    if (not isinstance(snapshot, dict) or set(snapshot) != {'digest', 'source', 'provider'}
            or not isinstance(snapshot['digest'], str) or not DIGEST.fullmatch(snapshot['digest'])):
        raise ValueError('owner restore report lacks its exact installed release identity')
    source, providers = snapshot['source'], snapshot['provider']
    if (not isinstance(source, dict) or set(source) != {'repository', 'repository_id', 'revision'}
            or not isinstance(source['repository'], str) or not re.fullmatch('[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', source['repository'])
            or not isinstance(source['repository_id'], str) or not NUMBER.fullmatch(source['repository_id'])
            or not isinstance(source['revision'], str) or not COMMIT.fullmatch(source['revision'])
            or value.get('revision') != source['revision'] or not isinstance(providers, dict)
            or set(providers) != {'cookbook_revision', 'tools_revision', 'core_revision'}
            or any(not isinstance(item, str) or not COMMIT.fullmatch(item) for item in providers.values())):
        raise ValueError('owner restore report lacks authenticated snapshot source and provider facts')
    for key in ['images', 'static_artifacts']:
        records = value.get(key)
        if (not isinstance(records, dict) or not 1 <= len(records) <= 32
                or any(not isinstance(item, str) or not DIGEST.fullmatch(item) for item in records.values())):
            raise ValueError('owner restore report lacks its exact database, API and frontend artifact identities')
    if (type(value.get('files_restored')) is not int or value['files_restored'] < 0
            or any(not isinstance(value.get(key), str) or not re.fullmatch('[a-f0-9]{64}', value[key])
                   for key in ['archive_sha256', 'ciphertext_sha256'])):
        raise ValueError('owner restore report lacks authenticated data and media integrity')
    return value


def restore_pair(root, project, config, tools, directory, key, *, cookbook, pull=False, packet=None):
    """Package only ciphertext, then retain a fully checked local restore report."""
    if (not key.is_absolute() or key.is_symlink() or not key.is_file()
            or key.stat().st_mode & 0o077 or not os.access(key, os.R_OK)):
        raise ValueError('set an absolute readable private local BACKUP_RECOVERY_KEY_FILE')
    if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents) or not directory.is_dir():
        raise ValueError('encrypted restore inputs must be a real local directory')
    if not config.is_file() or config.is_symlink():
        raise ValueError('restore profile configuration must be a regular source file')
    driver = Path(cookbook) / 'cookbook/application_restore.py'
    if (driver.is_symlink() or any(parent.is_symlink() for parent in driver.parents)
            or not driver.is_file() or driver.stat().st_size > 1 << 20):
        raise ValueError('owner restoration requires the explicitly selected authenticated Cookbook verifier')
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
        command = [sys.executable, '-I', '-B', str(driver), '--project', project,
                   '--config', str(config), '--export', str(archive), '--key', str(key)]
        if packet is not None:
            command += ['--packet', str(packet)]
        if pull:
            command.append('--pull')
        environment = dict(os.environ)
        environment.pop('BACKUP_RECOVERY_KEY_FILE', None)
        environment.update(COOKBOOK_ROOT=str(cookbook), TOOLS_ROOT=str(tools))
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
    parser.add_argument('--cookbook-root', type=Path)
    parser.add_argument('--cookbook-ref')
    parser.add_argument('--restore-config', type=Path)
    parser.add_argument('--profile', help='explicit native local restore profile')
    parser.add_argument('--pull', action='store_true', help='pull the authenticated exact restore images')
    parser.add_argument('--packet', type=Path, help='accepted exact installed release packet for explicit owner restoration')
    args = parser.parse_args()
    try:
        config = declaration(args.config, args.project)
        context = local_recipe_context(Path.cwd(), cookbook_root=args.cookbook_root,
            cookbook_ref=args.cookbook_ref, tools_root=args.tools_root)
        if context is not None:
            args.tools_root = context.tools
        elif args.tools_root is None:
            if args.operation == 'restore' and args.profile:
                args.tools_root = prepared_cookbook(Path.cwd())
            elif os.environ.get('TOOLS_ROOT'):
                args.tools_root = Path(os.environ['TOOLS_ROOT'])
            else:
                raise ValueError('a pinned cookbook root is required')
        if args.operation != 'restore' and (args.profile or args.pull or args.restore_config or args.packet):
            raise ValueError('restore options belong only to explicit local restoration')
        if args.operation == 'export':
            export(args.project, config, args.tools_root, args.directory or Path('.artifacts/backup'), os.environ)
        elif args.operation == 'upload':
            upload(args.project, args.tools_root, args.directory or Path('.artifacts/backup'))
        else:
            key = Path(os.environ.get('BACKUP_RECOVERY_KEY_FILE', ''))
            if not key.is_absolute():
                raise ValueError('set an absolute local BACKUP_RECOVERY_KEY_FILE')
            if context is None:
                raise ValueError('owner restoration requires its explicitly selected accepted Cookbook and helper providers')
            if args.profile:
                if args.restore_config is not None:
                    raise ValueError('a named native profile selects its configuration and encrypted pair')
                root = Path.cwd()
                selected = restore_profile(root, context.cookbook if context else args.tools_root,
                    args.profile, args.project, context=context)
                restore_pair(root, args.project, selected, args.tools_root,
                             args.directory or root / '.artifacts/restore/download', key, cookbook=context.cookbook,
                             pull=args.pull, packet=args.packet)
            else:
                restore_pair(Path.cwd(), args.project, args.restore_config or Path('deploy/restore.toml'), args.tools_root,
                             args.directory or Path.cwd() / '.artifacts/restore/download', key, cookbook=context.cookbook,
                             pull=args.pull, packet=args.packet)
    except (OSError, RuntimeError, AttributeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
