#!/usr/bin/env python3
"""Bind verified release identities to a short-lived GitHub Actions identity."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse
import urllib.request

MAX = 64 * 1024
DIGEST = re.compile(r'sha256:[a-f0-9]{64}')


def unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('duplicate release field')
        result[name] = value
    return result


def read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX:
        raise ValueError('release evidence must be a bounded regular file')
    return json.loads(path.read_bytes(), object_pairs_hook=unique)


def context(env, version):
    if not re.fullmatch(r'[a-f0-9]{40}', version) or version != env.get('GITHUB_SHA'):
        raise ValueError('new releases must match the workflow source')
    if env.get('GITHUB_REF') != 'refs/heads/main' or env.get('GITHUB_EVENT_NAME') not in {'push', 'workflow_dispatch'}:
        raise ValueError('release authority requires a main deployment workflow')
    repo = env.get('GITHUB_REPOSITORY', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('invalid source repository')
    for field in ('GITHUB_REPOSITORY_ID', 'GITHUB_RUN_ID'):
        if not re.fullmatch(r'[1-9][0-9]{0,19}', env.get(field, '')):
            raise ValueError('missing immutable workflow identity')
    return {'version': 1, 'repository': repo, 'repository_id': env['GITHUB_REPOSITORY_ID'],
            'revision': version, 'tested_commit': version, 'run_id': env['GITHUB_RUN_ID']}


def image_set(images, version):
    if not isinstance(images, dict) or not 2 <= len(images) <= 16:
        raise ValueError('release needs a bounded complete image set')
    for ref, image_id in images.items():
        if not isinstance(ref, str) or not re.fullmatch(r'[a-z0-9./_-]+:'+version, ref):
            raise ValueError('invalid release image reference')
        if not isinstance(image_id, str) or not DIGEST.fullmatch(image_id):
            raise ValueError('invalid Docker configuration identity')
    if sum(ref.endswith('-config:'+version) for ref in images) != 1:
        raise ValueError('release requires one configuration image')
    return images


def execution(proof, env):
    if not isinstance(proof, dict) or any(proof.get(key) != env.get(key) for key in ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_SHA')):
        raise ValueError('evidence belongs to another workflow execution')


def publication(data, env, version):
    if data.get('version') != 1 or data.get('revision') != version or data.get('tested_commit') != version:
        raise ValueError('publication does not establish this tested release')
    execution(data.get('publisher_context'), env)
    build, tests = data.get('build_provenance', {}), data.get('test_provenance', {})
    if build.get('source_commit') != version or tests.get('source_commit') != version or tests.get('tested_commit') != version:
        raise ValueError('test and build sources differ')
    if build.get('tracked_source_clean') is not True or tests.get('tracked_source_clean') is not True:
        raise ValueError('publication used modified tracked source')
    execution(build.get('build_context'), env)
    execution(tests.get('execution_context'), env)
    return image_set(data.get('images'), version)


def inspect(ref):
    value = subprocess.check_output(['docker', 'image', 'inspect', ref, '--format', '{{.Id}}'], text=True, timeout=30).strip()
    if not DIGEST.fullmatch(value):
        raise ValueError('Docker did not return a configuration identity')
    return value


def manifest(kind, source, image_base, gate_id, version, env, inspect_image=inspect):
    result = context(env, version)
    if kind == 'publication':
        data = read(source)
        images = publication(data, env, version)
        if 'stateful_contract' in data:
            result['stateful_contract'] = stateful_contract(data['stateful_contract'], version)
    elif kind == 'transferred-images':
        data = read(source)
        if data.get('source') != version:
            raise ValueError('transferred archive belongs to another source')
        images = image_set(data.get('images'), version)
        if any(inspect_image(ref) != image_id for ref, image_id in images.items()):
            raise ValueError('loaded images differ from the verified transfer manifest')
    elif kind in {'gate-artifact', 'same-job-gate'}:
        if not re.fullmatch(r'ghcr.io/[a-z0-9._-]+/[a-z0-9._/-]+', image_base):
            raise ValueError('invalid gate image family')
        images = {image_base+'-'+part+':'+version: inspect_image(image_base+'-'+part+':'+version) for part in ('gate', 'config')}
        if kind == 'gate-artifact' and (not DIGEST.fullmatch(gate_id) or images[image_base+'-gate:'+version] != gate_id):
            raise ValueError('gate differs from the verified Build job image')
        images = image_set(images, version)
    else:
        raise ValueError('unknown verified evidence kind')
    result['images'] = images
    return result


def stateful_contract(value, version):
    # Transport preserves the verified producer's contract without inventing
    # another admission policy. Root's typed validator checks all semantics
    # before pulls or configuration changes; this entire object is audience-bound.
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or value.get('source_revision') != version or len(json.dumps(value).encode()) > 32768):
        raise ValueError('stateful contract has an invalid bounded source identity')
    return value


def token(audience, env):
    url = env.get('ACTIONS_ID_TOKEN_REQUEST_URL', '')
    if urllib.parse.urlparse(url).scheme != 'https' or not env.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN'):
        raise ValueError('deployment job needs id-token: write')
    url += ('&' if '?' in url else '?') + urllib.parse.urlencode({'audience': audience})
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer '+env['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read(MAX+1)
    if len(body) > MAX:
        raise ValueError('Actions identity response exceeds limit')
    value = json.loads(body).get('value')
    if not isinstance(value, str) or len(value) > 24*1024 or value.count('.') != 2:
        raise ValueError('Actions did not return a bounded identity token')
    print('::add-mask::'+value, flush=True)
    return value


def envelope(path, version, env, request_token=token):
    expected = context(env, version)
    data = read(path)
    required = set(expected) | {'images'}
    if not required <= set(data) <= required | {'stateful_contract'} or any(data.get(name) != value for name, value in expected.items()):
        raise ValueError('release manifest differs from this workflow context')
    image_set(data.get('images'), version)
    if 'stateful_contract' in data:
        stateful_contract(data['stateful_contract'], version)
    raw = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
    audience = 'komizo-release:sha256:'+hashlib.sha256(raw).hexdigest()
    return {'manifest': base64.b64encode(raw).decode(), 'token': request_token(audience, env)}


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW, 0o600)) as output:
        json.dump(value, output, sort_keys=True, separators=(',', ':'))
        output.write('\n')
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('manifest', 'envelope'))
    parser.add_argument('--version', required=True)
    parser.add_argument('--source', default='')
    parser.add_argument('--kind', default='publication')
    parser.add_argument('--image-base', default='')
    parser.add_argument('--expected-gate-id', default='')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True, timeout=10).strip()
    if actual != args.version:
        raise ValueError('checked-out source differs from release version')
    if args.operation == 'manifest':
        result = manifest(args.kind, args.source, args.image_base, args.expected_gate_id, args.version, os.environ)
    else:
        result = envelope(args.source, args.version, os.environ)
    write(args.output, result)


if __name__ == '__main__':
    main()
