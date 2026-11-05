#!/usr/bin/env python3
"""Publish selected exact-HEAD Preview images without changing native host policy."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


preview = module('producer_preview', ROOT / 'scripts/preview.py')
authority = module('producer_publication', ROOT / 'scripts/application-publication.py')
git, api = preview.git, preview.api
SHA = re.compile('[a-f0-9]{40}')
DIGEST = re.compile('[a-f0-9]{64}')


def tracked(root, name, maximum=4 << 20):
    if not isinstance(name, str):
        raise ValueError('Preview inputs must be tracked path strings')
    value = root / name
    if (Path(name).is_absolute() or '\\' in name
            or '..' in Path(name).parts or not value.is_file()
            or value.is_symlink() or any(p.is_symlink() for p in value.parents)
            or value.stat().st_size > maximum):
        raise ValueError('Preview policy inputs must be bounded tracked regular files')
    subprocess.run(['git', '-C', str(root), 'ls-files', '--error-unmatch', '--', name],
                   check=True, stdout=subprocess.DEVNULL, timeout=10)
    body = value.read_bytes()
    if body != subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:' + name], timeout=10):
        raise ValueError('Preview policy differs from its immutable source blob')
    return body


def native_policy(root, tools, name):
    if not isinstance(name, str) or not re.fullmatch('deploy/[A-Za-z0-9_.-]+\\.json', name):
        raise ValueError('Preview producer policy belongs to its native deploy owner')
    data = json.loads(tracked(root, name, 32768))
    keys = {'version', 'project', 'components', 'workflow_sha256', 'build', 'publish', 'producer_timeout_minutes'}
    if not isinstance(data, dict) or set(data) != keys or data.get('version') != 1:
        raise ValueError('Preview producer requires closed native policy data')
    _, configuration, images = preview.tools_modules(tools)
    config = configuration.load(root)
    if config['recipe'] != 'react-native':
        raise ValueError('this producer requires complete native React Native acceptance')
    definition = images.definition(root, config)
    if data['project'] != definition['project']:
        raise ValueError('Preview project differs from native artifact authority')
    selected = data['components']
    if (not isinstance(selected, list) or not selected or len(set(selected)) != len(selected)
            or any(not isinstance(n, str) for n in selected) or 'gate' not in selected
            or not set(selected) <= set(images.components(definition))):
        raise ValueError('Preview may publish only distinct declared native image components')
    for role, fields in [('build', {'runner', 'timeout_minutes', 'environment'}),
                         ('publish', {'runner', 'timeout_minutes'})]:
        record = data[role]
        if (not isinstance(record, dict) or set(record) != fields
                or not isinstance(record.get('runner'), str)
                or not re.fullmatch('[A-Za-z0-9_.-]{1,80}', record['runner'])
                or type(record.get('timeout_minutes')) is not int
                or not 1 <= record['timeout_minutes'] <= 180):
            raise ValueError('Preview producer must retain bounded native runners and deadlines')
    total = data['producer_timeout_minutes']
    if (type(total) is not int or not 1 <= total <= 180
            or data['build']['timeout_minutes'] != total
            or data['publish']['timeout_minutes'] > total
            or data['build']['runner'] != config['ci']['runner']
            or data['build']['timeout_minutes'] > config['ci']['timeout_minutes']
            or data['build']['environment'] != 'Preview'):
        raise ValueError('Preview producer must retain its native aggregate budget and public build carrier')
    caller = tracked(root, '.github/workflows/pr-preview.yml')
    if not isinstance(data['workflow_sha256'], str) or not DIGEST.fullmatch(data['workflow_sha256']) or hashlib.sha256(caller).hexdigest() != data['workflow_sha256']:
        raise ValueError('Preview caller differs from its reviewed native byte contract')
    reference = re.search(rb'^COOKBOOK_REF := ([a-f0-9]{40})$', tracked(root, 'Makefile'), re.M)
    if not reference or reference[1].decode() != git(tools, 'rev-parse', 'HEAD'):
        raise ValueError('Preview producer must use its exact source-declared SDK')
    return {**data, 'definition': name, 'recipe': config['recipe'], 'image_base': definition['image_base'],
            'shipping_components': images.components(definition), 'test_groups': definition['tests'],
            'artifact_definition': config['artifacts']['definition'],
            'release_metadata': config['artifacts'].get('release_metadata', []),
            'cookbook_sha256': hashlib.sha256(tracked(root, 'cookbook.toml')).hexdigest(),
            'mise_sha256': hashlib.sha256(tracked(root, 'mise.toml')).hexdigest()}


def controller(root, environment):
    repository, source = preview.controller(environment)
    if git(root, 'rev-parse', 'HEAD') != source or subprocess.run(
            ['git', '-C', str(root), 'diff', '--quiet', 'HEAD', '--'], timeout=10).returncode:
        raise ValueError('Preview controller must be its clean trusted-main checkout')
    return repository, source


def resolve(environment, lookup=api):
    repository, _ = preview.controller(environment)
    resolver = module('producer_event_request', ROOT / 'preview-request/request.py')
    normalized = 'pull_request' if environment['GITHUB_EVENT_NAME'] == 'pull_request_target' else environment['GITHUB_EVENT_NAME']
    try:
        return resolver.resolve(preview.read_event(environment), normalized, repository, 'preview',
                                lambda repo, number: preview.current_pr(repo, number, lookup))
    except resolver.Refused as error:
        return {'enabled': 'false', 'reason': str(error)}


def request(root, tools, name, environment, lookup=api):
    repository, source = controller(root, environment)
    event = preview.read_event(environment)
    resolver = module('producer_request', ROOT / 'preview-request/request.py')
    normalized = 'pull_request' if environment['GITHUB_EVENT_NAME'] == 'pull_request_target' else environment['GITHUB_EVENT_NAME']
    try:
        decision = resolver.resolve(event, normalized, repository, 'preview',
                                    lambda repo, number: preview.current_pr(repo, number, lookup))
    except resolver.Refused as error:
        return {'enabled': 'false', 'reason': str(error)}
    result = {'enabled': 'true', **decision}
    # Teardown remains independent of producer availability and old source policy.
    if decision['action'] == 'down':
        return result
    policy = native_policy(root, tools, name)
    current = preview.current_pr(repository, int(decision['pr']), lookup)
    plan = {'version': 1, 'repository': repository, 'controller': source,
            'controller_tree': git(root, 'rev-parse', 'HEAD^{tree}'),
            'trigger': environment['GITHUB_EVENT_NAME'], 'pr': int(decision['pr']),
            'head': decision['sha'], 'base': current.get('base', {}).get('sha'), 'policy': policy}
    fresh(plan, environment, lookup)
    result.update(plan=json.dumps(plan, sort_keys=True, separators=(',', ':')), base=plan['base'],
                  build_runner=policy['build']['runner'], build_timeout=str(policy['build']['timeout_minutes']),
                  publish_runner=policy['publish']['runner'], publish_timeout=str(policy['producer_timeout_minutes']),
                  publication_timeout=str(policy['publish']['timeout_minutes']))
    return result


def fresh(plan, environment, lookup=api):
    repository, source = preview.controller(environment)
    if (plan['repository'] != repository or plan['controller'] != source or plan['base'] != source
            or plan['trigger'] != environment['GITHUB_EVENT_NAME']
            or lookup(f'repos/{repository}/branches/main')['commit']['sha'] != source):
        raise ValueError('Preview controller/base is no longer current main')
    current = preview.current_pr(repository, plan['pr'], lookup)
    preview.require_current(plan, current)
    resolver = module('producer_fresh_request', ROOT / 'preview-request/request.py')
    normalized = 'pull_request' if plan['trigger'] == 'pull_request_target' else plan['trigger']
    try:
        decision = resolver.resolve(preview.read_event(environment), normalized, repository, 'preview',
                                    lambda repo, number: preview.current_pr(repo, number, lookup))
    except resolver.Refused as error:
        raise ValueError('Preview is no longer an authorized request: ' + str(error)) from None
    if decision['action'] != 'up' or decision['sha'] != plan['head'] or int(decision['pr']) != plan['pr']:
        raise ValueError('Preview is no longer the authorized selected-head request')


def validate_plan(raw, root, tools, environment, lookup=api):
    if not isinstance(raw, str) or len(raw.encode()) > 131072:
        raise ValueError('Preview producer plan exceeds its bound')
    plan = json.loads(raw)
    keys = {'version', 'repository', 'controller', 'controller_tree', 'trigger', 'pr', 'head', 'base', 'policy'}
    if (not isinstance(plan, dict) or set(plan) != keys or plan.get('version') != 1
            or type(plan.get('pr')) is not int or not 1 <= plan['pr'] <= 9999999999
            or any(not isinstance(plan.get(k), str) or not SHA.fullmatch(plan[k]) for k in ['controller', 'controller_tree', 'head', 'base'])
            or not isinstance(plan.get('policy'), dict) or not isinstance(plan['policy'].get('definition'), str)):
        raise ValueError('invalid closed Preview producer plan')
    _, source = controller(root, environment)
    if (plan['controller'] != source or plan['controller_tree'] != git(root, 'rev-parse', 'HEAD^{tree}')
            or plan['policy'] != native_policy(root, tools, plan['policy']['definition'])):
        raise ValueError('Preview plan differs from its trusted immutable caller/policy')
    fresh(plan, environment, lookup)
    return plan


def remaining(plan, jobs, now=None):
    now = now or datetime.now(timezone.utc)
    if jobs.get('total_count') != len(jobs.get('jobs', [])):
        raise ValueError('Preview producer requires complete current-attempt job metadata')
    hosts = [j for j in jobs['jobs'] if j['name'].endswith('/ Make full')]
    if (len(hosts) != 1 or hosts[0].get('conclusion') != 'success'
            or hosts[0].get('head_sha') != plan['controller']
            or plan['policy']['build']['runner'] not in hosts[0].get('labels', [])):
        raise ValueError('Preview requires its sole successful source-bound native full job')
    try:
        started = datetime.fromisoformat(hosts[0]['started_at'].replace('Z', '+00:00'))
    except (ValueError, KeyError, TypeError):
        raise ValueError('Preview producer requires actual GitHub source-job start time') from None
    if started.tzinfo is None or started > now + timedelta(seconds=5):
        raise ValueError('Preview source-job timing is invalid')
    deadline = started + timedelta(minutes=plan['policy']['producer_timeout_minutes'])
    seconds = math.floor((deadline - now).total_seconds())
    if seconds <= 0:
        raise ValueError('the original aggregate Preview producer deadline has expired')
    return seconds, deadline


def archive_inventory(path, expected):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 << 30:
        raise ValueError('Preview archive exceeds its bounded transport size')
    files, regular, metadata, count, size, metadata_size = set(), set(), {}, 0, 0, 0
    try:
        with tarfile.open(path, mode='r|gz') as package:
            for member in package:
                name = member.name
                relative = PurePosixPath(name)
                count += 1; size += member.size
                canonical = str(relative) + ('/' if member.isdir() and name.endswith('/') else '')
                if (count > 200000 or size > 8 << 30 or member.size < 0 or name != canonical
                        or relative.is_absolute() or '..' in relative.parts
                        or '\\' in name or name in files or not (member.isfile() or member.isdir())):
                    raise ValueError('Preview image archive has unsafe or excessive entries')
                files.add(name)
                if member.isfile(): regular.add(name)
                if member.isfile() and (name == 'manifest.json' or name.endswith('.json') or name.startswith('blobs/sha256/')) and member.size <= 2 << 20:
                    metadata_size += member.size
                    if metadata_size > 16 << 20:
                        raise ValueError('Preview image metadata exceeds its bound')
                    metadata[name] = package.extractfile(member).read()
    except tarfile.TarError as error:
        raise ValueError('Preview archive is not a complete safe Docker archive') from error
    try:
        records = json.loads(metadata['manifest.json'])
    except (KeyError, ValueError):
        raise ValueError('Preview archive has no bounded Docker manifest') from None
    found = {}
    if not isinstance(records, list) or not records:
        raise ValueError('Preview archive has no declared images')
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get('RepoTags'), list) or not record['RepoTags']
                or not isinstance(record.get('Config'), str) or record['Config'] not in metadata
                or not isinstance(record.get('Layers'), list)
                or any(not isinstance(layer, str) or layer not in regular for layer in record['Layers'])):
            raise ValueError('Preview archive image descriptors are incomplete')
        identity = 'sha256:' + hashlib.sha256(metadata[record['Config']]).hexdigest()
        for tag in record['RepoTags']:
            if not isinstance(tag, str) or tag in found:
                raise ValueError('Preview archive has ambiguous image tags')
            found[tag] = identity
    if found != expected:
        raise ValueError('Preview archive image tags/IDs differ from its complete validated native family')
    return found


def derive(full_directory, destination, components, image_base, revision):
    original = authority.read(full_directory / 'release.json')
    references = [image_base + '-' + c + ':' + revision for c in components]
    if any(ref not in original['images'] for ref in references):
        raise ValueError('selected Preview images are absent from the validated manifest')
    if destination.exists() or destination.is_symlink() or any(p.is_symlink() for p in destination.parents):
        raise ValueError('Preview derivation requires a fresh regular destination')
    destination.mkdir(mode=0o700, parents=True)
    for name in ['images.tar.gz', 'images.tar.gz.sha256']:
        os.link(full_directory / name, destination / name, follow_symlinks=False)
    derived = original | {'images': {ref: original['images'][ref] for ref in references}}
    (destination / 'release.json').write_text(json.dumps(derived, indent=2) + '\n')
    return {'original_manifest_sha256': hashlib.sha256((full_directory / 'release.json').read_bytes()).hexdigest(),
            'selected_manifest_sha256': hashlib.sha256((destination / 'release.json').read_bytes()).hexdigest(),
            'selected_images': derived['images'], 'archive_sha256': original['archive_sha256']}


def payload(incoming, proof):
    entries = proof.get('artifacts')
    if not isinstance(entries, dict) or not entries or len(entries) > 200000:
        raise ValueError('Preview requires bounded complete native artifact identities')
    total = 0
    for name, digest in entries.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise ValueError('Preview artifact identities must be path/hash strings')
        if name.startswith('image:'):
            if not re.fullmatch('sha256:[a-f0-9]{64}', digest):
                raise ValueError('Preview image identity is invalid')
            continue
        relative = PurePosixPath(name); path = incoming / name
        if (not DIGEST.fullmatch(digest) or name != str(relative) or relative.is_absolute()
                or '..' in relative.parts or '\\' in name or not path.is_file() or path.is_symlink()
                or any(p.is_symlink() for p in path.parents) or path.stat().st_size > 2 << 30):
            raise ValueError('Preview artifacts must be bounded safe regular files')
        total += path.stat().st_size
        if total > 8 << 30:
            raise ValueError('Preview native artifact payload exceeds its bound')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != digest:
            raise ValueError('Preview artifact bytes differ from complete native acceptance')


def bounded(command, *, tools, deadline, cwd, env, input=None, output=None):
    seconds = math.floor((deadline - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        raise ValueError('Preview publication deadline expired before its next command')
    helper = Path(env.get('PREVIEW_PRODUCER_PROCESS_HELPER', str(tools / 'helpers/dev.py')))
    processes = module('producer_process_ownership', helper)
    processes.adopt_descendants()
    child = subprocess.Popen(command, cwd=cwd, env=env, text=True, stdin=subprocess.PIPE if input is not None else None,
                             stdout=output, start_new_session=True)
    try:
        child.communicate(input=input, timeout=seconds)
        if child.returncode:
            raise subprocess.CalledProcessError(child.returncode, command)
    finally:
        processes.stop_descendants(child)


def extract_packet(archive, destination):
    if destination.exists() or destination.is_symlink() or any(p.is_symlink() for p in destination.parents):
        raise ValueError('Preview packet requires a fresh regular destination')
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        names = [entry.filename for entry in entries]
        if not entries or len(entries) > 200000 or len(names) != len(set(names)) or sum(e.file_size for e in entries) > 8 << 30:
            raise ValueError('Preview packet has excessive or ambiguous entries')
        for entry in entries:
            relative = PurePosixPath(entry.filename)
            mode = (entry.external_attr >> 16) & 0o170000
            name = entry.filename.rstrip('/') if entry.is_dir() else entry.filename
            if (name != str(relative) or relative.is_absolute() or '..' in relative.parts or '\\' in entry.filename
                    or entry.file_size > 2 << 30 or mode not in {0, 0o040000, 0o100000}):
                raise ValueError('Preview packet contains an unsafe nonregular path')
        destination.mkdir(mode=0o700, parents=True)
        for entry in entries:
            path = destination / entry.filename
            if entry.is_dir():
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with package.open(entry) as source, path.open('xb') as target:
                    os.chmod(path, 0o600)
                    while chunk := source.read(1 << 20):
                        target.write(chunk)


def retrieve(root, repository, revision, values, tools, deadline, execute=bounded):
    if not preview.REPOSITORY.fullmatch(repository) or not SHA.fullmatch(revision):
        raise ValueError('Preview retrieval requires a fixed repository and immutable revision')
    if root.is_symlink() or any(p.is_symlink() for p in root.parents) or (root.exists() and any(root.iterdir())):
        raise ValueError('Preview retrieval requires an empty regular checkout')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    import base64
    token = values.get('GH_TOKEN', '')
    if not token or len(token) > 512 or any(c.isspace() or c == '\0' for c in token):
        raise ValueError('Preview retrieval requires its bounded workflow token')
    credentials = base64.b64encode(('x-access-token:' + token).encode()).decode()
    environment = dict(values, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
        GIT_LFS_SKIP_SMUDGE='1', GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
        GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + credentials, GIT_CONFIG_KEY_1='core.hooksPath', GIT_CONFIG_VALUE_1='/dev/null')
    for arguments in [['init', '-q'], ['remote', 'add', 'origin', 'https://github.com/' + repository + '.git'],
            ['fetch', '--no-tags', '--depth=1', 'origin', revision], ['checkout', '--force', '--detach', 'FETCH_HEAD']]:
        execute(['git', *arguments], tools=tools, deadline=deadline, cwd=root, env=environment)
    if git(root, 'rev-parse', 'HEAD') != revision:
        raise ValueError('Preview retrieval returned another commit')


def run_writer(root, tools, source_root, incoming, environment, lookup=api, execute=bounded):
    # The immutable workflow bootstraps this trusted process under the actual
    # source job deadline. Its supervisor owns every descendant, including
    # checkout, metadata reads, decompression and final publication.
    raw = environment.get('PLAN', '')
    if len(raw.encode()) > 131072:
        raise ValueError('Preview producer plan exceeds its bound')
    plan = json.loads(raw)
    _, actual_deadline = remaining(plan, lookup(f"repos/{environment['GITHUB_REPOSITORY']}/actions/runs/{environment['GITHUB_RUN_ID']}/attempts/{environment['GITHUB_RUN_ATTEMPT']}/jobs?per_page=100"))
    deadline = datetime.fromisoformat(environment['PREVIEW_PRODUCER_DEADLINE'])
    if deadline != actual_deadline:
        raise ValueError('Preview bootstrap deadline differs from the actual source job')
    retrieve(root, environment['GITHUB_REPOSITORY'], environment['GITHUB_SHA'], environment, tools, deadline, execute)
    retrieve(tools, 'nicodes/tools', environment['TOOLS_REF'], environment, tools, deadline, execute)
    preview.accepted_automation(tools, environment['TOOLS_REF'], environment['KOMIZO_REF'], lookup)
    plan = validate_plan(raw, root, tools, environment, lookup)
    retrieve(source_root, plan['repository'], plan['head'], environment, tools, deadline, execute)
    artifacts = lookup(f"repos/{plan['repository']}/actions/runs/{environment['GITHUB_RUN_ID']}/artifacts?per_page=100")
    if artifacts.get('total_count') != len(artifacts.get('artifacts', [])):
        raise ValueError('Preview requires complete same-run artifact metadata')
    name = f"cookbook-full-{plan['head']}-{environment['GITHUB_RUN_ID']}-{environment['GITHUB_RUN_ATTEMPT']}"
    matches = [a for a in artifacts['artifacts'] if a.get('name') == name]
    if (len(matches) != 1 or matches[0].get('expired') is not False
            or type(matches[0].get('id')) is not int or matches[0]['id'] < 1
            or type(matches[0].get('size_in_bytes')) is not int or not 1 <= matches[0]['size_in_bytes'] <= 2 << 30
            or not re.fullmatch('sha256:[a-f0-9]{64}', matches[0].get('digest', ''))):
        raise ValueError('Preview requires one bounded complete current-attempt native packet')
    metadata = matches[0]
    with tempfile.TemporaryDirectory(prefix='preview-native-packet-') as directory:
        packet = Path(directory) / 'native.zip'
        with packet.open('xb') as target:
            os.chmod(packet, 0o600)
            execute(['gh', 'api', '--method', 'GET', f"repos/{plan['repository']}/actions/artifacts/{metadata['id']}/zip"],
                    tools=tools, deadline=deadline, cwd=root, env=environment, output=target)
        with packet.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if packet.stat().st_size != metadata['size_in_bytes'] or 'sha256:' + digest != metadata['digest']:
            raise ValueError('Preview native packet differs from its immutable Actions artifact')
        extract_packet(packet, incoming)
    publication(plan, root, source_root, tools, environment['TOOLS_REF'], incoming, environment, lookup, execute)
    seconds, _ = remaining(plan, lookup(f"repos/{plan['repository']}/actions/runs/{environment['GITHUB_RUN_ID']}/attempts/{environment['GITHUB_RUN_ATTEMPT']}/jobs?per_page=100"))
    if seconds < 120:
        raise ValueError('Preview producer has insufficient original budget to retain publication evidence')
    preview.output({'head': plan['head'], 'remaining_minutes': str(seconds // 60 - 1)}, environment)


def publication(plan, root, source_root, tools, tools_ref, incoming, environment, lookup=api, execute=bounded):
    # Every helper path must survive the deliberate switch to the data-only
    # source checkout as cwd; the standalone CLI accepts relative paths.
    tools, source_root, incoming = tools.resolve(), source_root.resolve(), incoming.resolve()
    _, configuration, images = preview.tools_modules(tools)
    if git(source_root, 'rev-parse', 'HEAD') != plan['head'] or subprocess.run(
            ['git', '-C', str(source_root), 'diff', '--quiet', 'HEAD', '--'], timeout=10).returncode:
        raise ValueError('Preview publisher requires the clean actual selected HEAD')
    config = configuration.load(source_root)
    policy = plan['policy']; definition = images.definition(source_root, config)
    if (hashlib.sha256(tracked(source_root, 'cookbook.toml')).hexdigest() != policy['cookbook_sha256']
            or hashlib.sha256(tracked(source_root, 'mise.toml')).hexdigest() != policy['mise_sha256']
            or definition['project'] != policy['project'] or definition['image_base'] != policy['image_base']
            or images.components(definition) != policy['shipping_components'] or definition['tests'] != policy['test_groups']):
        raise ValueError('selected source cannot override trusted build, SDK, artifact or component authority')
    proof = authority.read(incoming / '.artifacts/cookbook-validation.json')
    jobs = lookup(f"repos/{plan['repository']}/actions/runs/{environment['GITHUB_RUN_ID']}/attempts/{environment['GITHUB_RUN_ATTEMPT']}/jobs?per_page=100")
    writers = [job for job in jobs.get('jobs', []) if job.get('name', '').endswith('/ Publish selected Preview images')]
    if (environment.get('GITHUB_JOB') != 'publish'
            or environment.get('PRODUCER_RUNNER') != policy['publish']['runner']
            or environment.get('PRODUCER_TIMEOUT') != str(policy['producer_timeout_minutes'])
            or environment.get('PUBLICATION_TIMEOUT') != str(policy['publish']['timeout_minutes'])
            or len(writers) != 1 or writers[0].get('head_sha') != plan['controller']
            or policy['publish']['runner'] not in writers[0].get('labels', [])
            or writers[0].get('status') != 'in_progress'):
        raise ValueError('Preview writer must retain its reviewed actual runner and publication deadline')
    authority.accept(proof, config, plan['head'], tools_ref, git(source_root, 'rev-parse', 'HEAD^{tree}'),
                     policy['test_groups'], jobs, environment, workflow_source=plan['controller'])
    if (proof.get('head') != plan['head'] or proof.get('base') != plan['base']
            or proof.get('configuration_sha256') != policy['cookbook_sha256']):
        raise ValueError('Preview acceptance did not build this exact requested HEAD/base/configuration')
    build = authority.read(incoming / '.artifacts/verification/build.json')
    test = authority.read(incoming / '.artifacts/verification/test.json')
    if (build.get('identity') != test.get('input_identity') or build.get('artifacts') != proof.get('artifacts')
            or not isinstance(build.get('identity'), str) or not DIGEST.fullmatch(build['identity'])
            or build.get('provenance', {}).get('input_identity') != build.get('identity')
            or test.get('configuration_sha256') != build.get('provenance', {}).get('configuration_sha256')):
        raise ValueError('Preview build/tests did not accept identical source inputs and artifact bytes')
    for record in [build['provenance'], test]:
        if (record.get('source_commit') != plan['head'] or record.get('source_tree') != proof['source_tree']
                or record.get('tracked_source_clean') is not True or record.get('execution_context') != proof['execution_context']):
            raise ValueError('Preview native receipts belong to another source or execution')
    if (test.get('tested_commit') != plan['head']
            or set(test.get('test_receipts', {})) != set(policy['test_groups'])
            or any(record != {'input_identity': build['identity'], 'source_commit': plan['head']}
                   for record in test.get('test_receipts', {}).values())):
        raise ValueError('Preview lacks complete fresh native group receipts')
    _, deadline = remaining(plan, jobs)
    payload(incoming, proof)
    if any(p.is_symlink() for p in (source_root / '.artifacts').parents) or (source_root / '.artifacts').is_symlink():
        raise ValueError('Preview source artifacts must have regular parents')
    full = source_root / '.artifacts/preview-validated'
    authority.stage(incoming, proof, full, policy['release_metadata'])
    release = authority.read(full / 'release.json')
    refs = {policy['image_base'] + '-' + c + ':' + plan['head'] for c in policy['shipping_components']}
    if (release.get('revision') != plan['head'] or set(release.get('images', {})) != refs
            or release.get('archive_sha256') != proof['artifacts'].get('.artifacts/release/images.tar.gz')):
        raise ValueError('Preview archive belongs to another revision or native image family')
    provenance = release.get('provenance', {})
    if (provenance.get('input_identity') != build['identity'] or provenance.get('source_commit') != plan['head']
            or provenance.get('source_tree') != proof['source_tree'] or provenance.get('tracked_source_clean') is not True
            or provenance.get('configuration_sha256') != build['provenance'].get('configuration_sha256')
            or provenance.get('build_context') != proof['execution_context']):
        raise ValueError('Preview release provenance differs from the exact accepted native build/tests')
    image_entries = {name.removeprefix('image:'): identity for name, identity in proof['artifacts'].items() if name.startswith('image:')}
    if image_entries != release['images']:
        raise ValueError('Preview image IDs differ from complete native build acceptance')
    archive_inventory(full / 'images.tar.gz', release['images'])
    selected = source_root / '.artifacts/preview-selected'
    derived = derive(full, selected, policy['components'], policy['image_base'], plan['head'])
    remaining(plan, jobs)
    fresh(plan, environment, lookup)
    values = dict(environment, COOKBOOK_PREVIEW_NUMBER=str(plan['pr']), REQUEST_HEAD=plan['head'], REQUEST_BASE=plan['base'])
    token = environment.get('GH_TOKEN', '')
    if not token or len(token) > 512 or any(c.isspace() or c == '\0' for c in token):
        raise ValueError('Preview publisher requires its bounded registry token')
    with tempfile.TemporaryDirectory(prefix='preview-registry-') as directory:
        values['DOCKER_CONFIG'] = directory
        # The aggregate starts at the actual readonly source job, including all setup/download time.
        execute(['docker', 'login', 'ghcr.io', '--username', environment['GITHUB_REPOSITORY'].split('/')[0], '--password-stdin'],
                tools=tools, deadline=deadline, cwd=root, env=values, input=token+'\n')
        fresh(plan, environment, lookup)
        publication_deadline = min(deadline, datetime.now(timezone.utc) + timedelta(minutes=policy['publish']['timeout_minutes']))
        execute([sys.executable, str(tools / 'helpers/release.py'), 'publish', '--project', policy['project'],
                 '--revision', plan['head'], '--components', *policy['components'], '--definition', policy['artifact_definition'],
                 '--directory', str(selected), '--test-job', 'validate', '--test-evidence-directory', str((incoming / '.artifacts/verification').resolve())],
                tools=tools, deadline=publication_deadline, cwd=source_root, env=values)
    published = authority.read(selected / 'publication.json')
    if published.get('revision') != plan['head'] or published.get('tested_commit') != plan['head'] or published.get('images') != derived['selected_images']:
        raise ValueError('Preview publication did not retain exact selected-HEAD image identities')
    report = root / '.artifacts/preview-producer-publication.json'
    report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    report.write_text(json.dumps({'version': 1, 'plan': plan, 'derivation': derived, 'publication': published,
                                 'aggregate_deadline': deadline.isoformat()}, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['resolve', 'request', 'plan', 'verify', 'publish', 'run'])
    parser.add_argument('--tools', type=Path, default=Path('_preview_producer_tools'))
    parser.add_argument('--declaration', default='')
    parser.add_argument('--source-root', type=Path, default=Path('_preview_producer_source'))
    parser.add_argument('--incoming', type=Path, default=Path('.artifacts/preview-producer-input'))
    args = parser.parse_args()
    try:
        environment = dict(os.environ); root = Path.cwd()
        if args.operation == 'resolve':
            preview.output(resolve(environment), environment)
            return
        if args.operation == 'run':
            run_writer(root, args.tools.resolve(), args.source_root.resolve(), args.incoming.resolve(), environment)
            return
        preview.accepted_automation(args.tools, environment['TOOLS_REF'], environment['KOMIZO_REF'])
        if args.operation == 'request':
            preview.output(request(root, args.tools, args.declaration, environment), environment)
            return
        plan = validate_plan(environment['PLAN'], root, args.tools, environment)
        if args.operation in {'verify', 'plan'}:
            preview.output({'head': plan['head'], 'base': plan['base'], 'pr': plan['pr']}, environment)
        else:
            publication(plan, root, args.source_root, args.tools, environment['TOOLS_REF'], args.incoming, environment)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
