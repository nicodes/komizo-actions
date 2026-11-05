#!/usr/bin/env python3
"""Inspect deployed dependencies against exact historical source, without builds."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

SHA = re.compile('[a-f0-9]{40}')
IMAGE_ID = re.compile('sha256:[a-f0-9]{64}')
COMPONENT = re.compile('[a-z][a-z0-9-]{0,62}')


def source_path(root, name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(ord(c) < 32 for c in name)):
        raise ValueError('scan policy requires bounded source paths')
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or '.git' in path.parts:
        raise ValueError('scan policy paths must remain in their source checkout')
    selected = root / path
    if any(p.is_symlink() for p in [selected, *selected.parents] if p.is_relative_to(root)):
        raise ValueError('scan source and evidence paths must not traverse links')
    return selected


def read(root, name, maximum=65536):
    path = source_path(root, name)
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('scan policy must be bounded regular source')
    return json.loads(path.read_text())


def installed(name):
    if (not isinstance(name, str) or len(name) > 256 or not name.startswith('/')
            or '\\' in name or any(ord(c) < 32 for c in name)
            or '..' in PurePosixPath(name).parts or name == '/'):
        raise ValueError('runtime inspection requires fixed absolute image paths')
    return name


def literals(values):
    if (not isinstance(values, list) or len(values) > 32
            or any(not isinstance(v, str) or not 1 <= len(v) <= 256
                   or any(ord(c) < 32 for c in v) for v in values)):
        raise ValueError('forbidden runtime markers require bounded literal data')
    return values


def policy(root, path):
    value = read(root, str(path.relative_to(root)))
    if (not isinstance(value, dict) or set(value) != {'version', 'deployment', 'rust', 'go'}
            or type(value.get('version')) is not int or value['version'] != 1):
        raise ValueError('unsupported deployed dependency policy')
    if not isinstance(value['deployment'], str) or not value['deployment'].startswith('deploy/'):
        raise ValueError('deployment selection remains source-owned data')
    read(root, value['deployment'])
    for language in ['rust', 'go']:
        records = value[language]
        if (not isinstance(records, dict) or len(records) > 16
                or any(not COMPONENT.fullmatch(name) for name in records)):
            raise ValueError('runtime checks require distinct bounded component records')
    for record in value['rust'].values():
        if (not isinstance(record, dict) or set(record) - {'source_lock', 'legacy_manifest', 'lock_installed',
                'binaries', 'user', 'forbidden'} or not {'source_lock', 'lock_installed', 'binaries', 'user', 'forbidden'} <= set(record)):
            raise ValueError('Rust inspection requires its native source, user, binaries and lock contract')
        source_path(root, record['source_lock'])
        if not record['source_lock'].endswith('Cargo.lock'):
            raise ValueError('Rust source inventory must select a Cargo.lock')
        if record.get('legacy_manifest'):
            source_path(root, record['legacy_manifest'])
            if not record['legacy_manifest'].endswith('go.mod'):
                raise ValueError('historical fallback must identify its actual Go manifest')
        installed(record['lock_installed'])
        binaries = record['binaries']
        if (not isinstance(binaries, list) or not 1 <= len(binaries) <= 16
                or any(not isinstance(binary, str) for binary in binaries) or len(set(binaries)) != len(binaries)):
            raise ValueError('Rust inspection requires distinct native image executables')
        for binary in binaries:
            installed(binary)
        if not isinstance(record['user'], str) or not re.fullmatch('[0-9]{1,10}:[0-9]{1,10}', record['user']):
            raise ValueError('Rust inspection requires the native numeric runtime UID/GID')
        literals(record['forbidden'])
    for record in value['go'].values():
        if not isinstance(record, dict) or set(record) - {'expected_go', 'upstream', 'forbidden'}:
            raise ValueError('Go inspection selects only its native compiler, upstream paths and markers')
        if 'expected_go' in record and (not isinstance(record['expected_go'], str)
                or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', record['expected_go'])):
            raise ValueError('Go compiler selection must be an exact native version')
        upstream = record.get('upstream', [])
        if not isinstance(upstream, list) or len(upstream) > 16:
            raise ValueError('upstream binaries require a bounded native path list')
        for binary in upstream:
            installed(binary)
        literals(record.get('forbidden', []))
    return value


def helper(tools, name):
    spec = importlib.util.spec_from_file_location('deployed_' + name.replace('-', '_'), tools / 'helpers' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def capture(arguments, environment, timeout=60):
    value = subprocess.check_output(arguments, env=environment, text=True, timeout=timeout)
    if len(value.encode()) > 8 * 1024 * 1024:
        raise ValueError('inspection metadata exceeds its bound')
    return value


def api(path, environment):
    return json.loads(capture(['gh', 'api', path], environment))


def source_tree(repository, revision, environment):
    if not SHA.fullmatch(revision):
        raise ValueError('deployed revision must be an exact source commit')
    value = api(f'repos/{repository}/git/trees/{revision}?recursive=1', environment)
    if (not isinstance(value, dict) or value.get('truncated') is not False
            or not isinstance(value.get('tree'), list) or len(value['tree']) > 100000):
        raise ValueError('complete bounded historical source tree required')
    entries = {}
    for entry in value['tree']:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or entry['path'] in entries:
            raise ValueError('historical source entries must be unambiguous')
        entries[entry['path']] = entry
    return entries


def source_lock(repository, tree, record, environment):
    entry = tree.get(record['source_lock'])
    if entry is None:
        legacy = tree.get(record.get('legacy_manifest'), {})
        if legacy.get('type') == 'blob' and SHA.fullmatch(legacy.get('sha', '')):
            return None
        raise ValueError('unknown deployed implementation; neither native Rust lock nor historical Go manifest exists')
    if entry.get('type') != 'blob' or not SHA.fullmatch(entry.get('sha', '')):
        raise ValueError('exact historical source lockfile required')
    blob = api(f'repos/{repository}/git/blobs/{entry["sha"]}', environment)
    if (not isinstance(blob, dict) or blob.get('encoding') != 'base64'
            or type(blob.get('size')) is not int or not 0 < blob['size'] <= 2 * 1024 * 1024
            or not isinstance(blob.get('content'), str)):
        raise ValueError('bounded base64 historical lockfile required')
    data = base64.b64decode(''.join(blob['content'].split()), validate=True)
    if len(data) != blob['size'] or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != entry['sha']:
        raise ValueError('historical lockfile bytes differ from their exact Git blob')
    return data


def run(arguments, environment, timeout=60, **kwargs):
    subprocess.run(arguments, env=environment, check=True, timeout=timeout, **kwargs)


def rust_image(identity, revision, lock, record, reports, component, environment):
    info = json.loads(capture(['docker', 'image', 'inspect', identity], environment))
    if (not isinstance(info, list) or len(info) != 1 or info[0].get('Id') != identity
            or not IMAGE_ID.fullmatch(identity)):
        raise ValueError('exact immutable runtime image required')
    config = info[0].get('Config', {})
    if config.get('Labels', {}).get('org.opencontainers.image.revision') != revision:
        raise ValueError('runtime source revision mismatch')
    if config.get('User') != record['user']:
        raise ValueError('runtime UID/GID changed')
    container = capture(['docker', 'create', identity], environment).strip()
    if not re.fullmatch('[a-f0-9]{64}', container):
        raise ValueError('exact inspection container identity required')
    try:
        with tempfile.TemporaryDirectory(prefix='deployed-rust-') as directory:
            root = Path(directory)
            for index, binary in enumerate(record['binaries']):
                destination = root / ('binary-' + str(index))
                run(['docker', 'cp', container + ':' + binary, str(destination)], environment)
                if destination.is_symlink() or not destination.is_file() or destination.stat().st_size > 1024 ** 3:
                    raise ValueError('runtime executable must be a bounded regular file')
                data = destination.read_bytes()
                if not data.startswith(b'\x7fELF'):
                    raise ValueError('native Rust executable missing')
                if any(marker.encode() in data for marker in record['forbidden']):
                    raise ValueError('development authority shipped')
            inputs = root / 'inputs'
            inputs.mkdir()
            path = inputs / 'Cargo.lock'
            run(['docker', 'cp', container + ':' + record['lock_installed'], str(path)], environment)
            if (path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024
                    or path.read_bytes() != lock):
                raise ValueError('deployed lockfile differs from the exact historical source')
            report = reports / f'{revision}-{component}-rust.json'
            run(['trivy', 'fs', '--quiet', '--scanners', 'vuln', '--pkg-types', 'library',
                 '--include-dev-deps', '--list-all-pkgs', '--format', 'json', '--output', str(report), str(inputs)],
                environment, timeout=600)
            findings = json.loads(report.read_text())
            if (findings.get('SchemaVersion') != 2 or not isinstance(findings.get('Results'), list)
                    or not any(result.get('Type') == 'cargo' and result.get('Packages') for result in findings['Results'])):
                raise ValueError('Rust dependency inventory was not scanned')
            if any(v.get('Severity') in ('HIGH', 'CRITICAL') for result in findings['Results']
                   for v in result.get('Vulnerabilities', [])):
                raise ValueError('High/Critical Rust dependency findings')
    finally:
        run(['docker', 'rm', '-v', container], environment, timeout=30, stdout=subprocess.DEVNULL)
    return dict(image_id=identity, revision=revision, inventory='cargo')


def scan(root, path, tools, environment=None):
    root = Path(root).resolve()
    path = Path(path).absolute()
    environment = dict(os.environ) if environment is None else environment
    data = policy(root, path)
    deployed = helper(tools, 'scan-deployed')
    go = helper(tools, 'scan-image')
    config = deployed.load_config(source_path(root, data['deployment']))
    if config['repository'] != environment.get('GITHUB_REPOSITORY', config['repository']):
        raise ValueError('deployed policy differs from its actual source repository')
    if set(config['components']) != set(data['rust']) | set(data['go']):
        raise ValueError('every deployed component requires its actual native scanner')
    revisions = deployed.deployed_revisions(config['repository'], config['environment'])
    reports = source_path(root, '.artifacts/security/deployed')
    reports.mkdir(parents=True, exist_ok=True)
    results, failures = [], []
    for revision in revisions:
        tree = source_tree(config['repository'], revision, environment) if data['rust'] else {}
        for component, image in zip(config['components'], deployed.product_images(config, revision), strict=True):
            try:
                record = data['rust'].get(component)
                lock = source_lock(config['repository'], tree, record, environment) if record else None
                run(['docker', 'pull', image], environment, timeout=300)
                inspected = json.loads(capture(['docker', 'image', 'inspect', image], environment))
                if not isinstance(inspected, list) or len(inspected) != 1 or not IMAGE_ID.fullmatch(inspected[0].get('Id', '')):
                    raise ValueError('deployed image identity is missing or ambiguous')
                identity = inspected[0]['Id']
                report = reports / f'{revision}-{component}-image.json'
                run(['trivy', 'image', '--image-src', 'docker', '--scanners', 'vuln', '--pkg-types', 'os,library',
                     '--severity', 'HIGH,CRITICAL', '--format', 'json', '--output', str(report), identity], environment, timeout=600)
                run(['trivy', 'image', '--image-src', 'docker', '--scanners', 'vuln', '--pkg-types', 'os,library',
                     '--severity', 'HIGH,CRITICAL', '--ignore-unfixed', '--exit-code', '1', identity], environment, timeout=600)
                if record and lock is not None:
                    result = rust_image(identity, revision, lock, record, reports, component, environment)
                else:
                    if component not in data['go']:
                        raise ValueError('historical implementation has no declared Go runtime scanner')
                    selected = data['go'][component]
                    result = go.scan(identity, expected_go=selected.get('expected_go'),
                                     upstream=selected.get('upstream', []), forbidden=selected.get('forbidden', []))
                results.append(dict(result, image=image, revision=revision, component=component))
            except (OSError, AssertionError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                failures.append(f'{image}: {error}')
    evidence = dict(scope='deployed-dependencies', revisions=revisions, images=results, failures=failures)
    (reports / 'summary.json').write_text(json.dumps(evidence, indent=2) + '\n')
    if failures:
        raise ValueError('deployed dependency audit failed; complete findings retained in .artifacts/security/deployed/summary.json')
    return evidence
