#!/usr/bin/env python3
"""Submit an app-scoped request through the fixed private SSH transport."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from github_identity import identity
import oci_registry
import release_packet


MAX_REQUEST = 4 << 20
ACTIONS = {'deploy', 'start', 'stop', 'restart', 'inspect', 'backup', 'export', 'remove'}
NONCE = re.compile(r'[a-f0-9]{32}\Z')


def proposed_operation(submitted):
    raw = 'komizo-operation-v1\0' + submitted['application'] + '\0' + submitted['instance'] + '\0' + submitted['nonce']
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def producer_proof(value, release_digest, revision):
    fields = {'kind', 'version', 'release_digest', 'source_revision', 'run_id', 'run_attempt',
              'workflow_ref', 'workflow_sha', 'proof'}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get('kind') != 'komizo.release-proof' or type(value.get('version')) is not int
            or value['version'] != 1 or value['release_digest'] != release_digest
            or value['source_revision'] != revision
            or not all(isinstance(value[key], str) and release_packet.NUMBER.fullmatch(value[key])
                       for key in ['run_id', 'run_attempt'])
            or not isinstance(value['workflow_ref'], str)
            or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml@refs/heads/main', value['workflow_ref'])
            or not isinstance(value['workflow_sha'], str) or not release_packet.COMMIT.fullmatch(value['workflow_sha'])
            or not isinstance(value['proof'], str) or not 0 < len(value['proof']) <= 24 << 10
            or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', value['proof'])):
        raise ValueError('publication requires independent exact-source workflow authority')
    return value


def workflow(environment):
    if (environment.get('GITHUB_SERVER_URL') != 'https://github.com'
            or environment.get('GITHUB_REF') != 'refs/heads/main'
            or environment.get('GITHUB_EVENT_NAME') not in {'push', 'workflow_dispatch', 'schedule', 'pull_request_target', 'issue_comment'}):
        raise ValueError('submission requires a trusted main workflow')
    if environment.get('GITHUB_EVENT_NAME') in {'pull_request_target', 'issue_comment'}:
        if (environment.get('GITHUB_WORKFLOW_REF') != environment.get('GITHUB_REPOSITORY', '') + '/.github/workflows/pr-preview.yml@refs/heads/main'
                or environment.get('GITHUB_WORKFLOW_SHA') != environment.get('GITHUB_SHA')):
            raise ValueError('preview submission requires its exact trusted main controller')
    for variable, pattern in [('GITHUB_REPOSITORY', r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+'),
                              ('GITHUB_REPOSITORY_ID', r'[1-9][0-9]{0,19}'),
                              ('GITHUB_SHA', r'[a-f0-9]{40}'), ('GITHUB_RUN_ID', r'[1-9][0-9]{0,19}'),
                              ('GITHUB_RUN_ATTEMPT', r'[1-9][0-9]{0,19}')]:
        if not re.fullmatch(pattern, environment.get(variable, '')):
            raise ValueError('submission has no immutable workflow identity')


def registry_authority(environment):
    username = environment.get('KOMIZO_REGISTRY_USER', '')
    token = environment.get('KOMIZO_REGISTRY_TOKEN', '')
    if not username and not token:
        return None
    if (not username or not token or len(username) > 256 or len(token) > 32768
            or any(char in username + token for char in '\r\n\0')):
        raise ValueError('registry input requires one bounded username and token')
    return {'server': 'ghcr.io', 'username': username, 'token': token}


def request(application, instance, action, environment, *, descriptor=None, release_digest=None,
            release_proof=None, expected_secret_generations=None, ttl_seconds=None, timeout_seconds=1500,
            operation=None, export_name=None, now=None, nonce=None, validity=120, token_request=identity):
    workflow(environment)
    if (not isinstance(application, str) or not release_packet.NAME.fullmatch(application)
            or not isinstance(instance, str) or not release_packet.NAME.fullmatch(instance)
            or action not in ACTIONS or type(validity) is not int or not 1 <= validity <= 300):
        raise ValueError('submission requires a bounded app, instance, action and validity')
    issued = int(time.time()) if now is None else now
    unique = secrets.token_hex(16) if nonce is None else nonce
    if type(issued) is not int or issued < 1 or not isinstance(unique, str) or not NONCE.fullmatch(unique):
        raise ValueError('submission requires a fresh request identity')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 1500:
        raise ValueError('submission requires a finite admitted worker budget')
    if environment.get('KOMIZO_OPERATION_DEADLINE'):
        deadline = environment['KOMIZO_OPERATION_DEADLINE']
        if not isinstance(deadline, str) or not re.fullmatch('[1-9][0-9]{0,11}', deadline):
            raise ValueError('submission has an invalid original operation deadline')
        timeout_seconds = min(timeout_seconds, int(deadline) - issued)
        if timeout_seconds < 1:
            raise ValueError('submission original operation budget expired before admission')
    body = {'kind': 'komizo.request', 'version': 1, 'authority': 'github',
            'application': application, 'instance': instance, 'action': action,
            'nonce': unique, 'issued_at': issued, 'expires_at': issued + validity,
            'timeout_seconds': timeout_seconds}
    if descriptor is not None:
        if action != 'deploy':
            raise ValueError('only deployment accepts a release descriptor')
        release_raw = release_packet.release(descriptor, application)
        actual = oci_registry.digest(release_raw)
        if release_digest is not None and release_digest != actual:
            raise ValueError('requested release digest differs from its descriptor')
        release_digest = actual
        source = descriptor['source']
        if source['repository'] != environment['GITHUB_REPOSITORY'] or source['repository_id'] != environment['GITHUB_REPOSITORY_ID']:
            raise ValueError('release repository differs from its requesting application')
        if instance == 'production' and source['revision'] != environment['GITHUB_SHA']:
            raise ValueError('production deployment requires its exact workflow source')
        if instance != 'production' and release_proof is None:
            raise ValueError('preview deployment requires independent accepted-source publication proof')
        body['release'] = descriptor
    elif action == 'deploy':
        raise ValueError('deployment requires its immutable release descriptor')
    if not isinstance(release_digest, str) or not (oci_registry.DIGEST.fullmatch(release_digest)
            or (action == 'inspect' and release_digest == 'current' and operation is None)
            or (action == 'backup' and release_digest == 'absent')):
        raise ValueError('submission requires an exact release digest')
    body['release_digest'] = release_digest
    if action == 'deploy' and instance != 'production':
        if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 604800:
            raise ValueError('preview deployment requires its source-declared finite lifetime')
        body['ttl_seconds'] = ttl_seconds
    elif ttl_seconds is not None:
        raise ValueError('instance lifetime is limited to preview deployment')
    if expected_secret_generations is not None:
        if (action != 'deploy' or not isinstance(expected_secret_generations, dict)
                or len(expected_secret_generations) > 32
                or any(not isinstance(alias, str) or not release_packet.NAME.fullmatch(alias)
                       or not isinstance(generation, str) or not NONCE.fullmatch(generation)
                       for alias, generation in expected_secret_generations.items())):
            raise ValueError('secret generation expectations require bounded policy alias identities')
        if expected_secret_generations:
            body['expected_secret_generations'] = expected_secret_generations
    if release_proof is not None:
        if descriptor is None:
            raise ValueError('publication proof is limited to exact release admission')
        body['release_proof'] = producer_proof(release_proof, release_digest, descriptor['source']['revision'])
    if operation is not None:
        if action not in {'inspect', 'export'} or not isinstance(operation, str) or not NONCE.fullmatch(operation):
            raise ValueError('operation selection requires an exact inspection or recovery identity')
        body['operation'] = operation
    if action == 'export':
        if operation is None or export_name not in {'snapshot.cms', 'receipt.json'}:
            raise ValueError('export requires its exact backup operation and file')
        body['export_name'] = export_name
    elif export_name is not None:
        raise ValueError('only export can select a protected ciphertext file')
    authority = registry_authority(environment)
    if authority is not None:
        if action != 'deploy':
            raise ValueError('registry authority is limited to release admission')
        if any(not artifact['reference'].startswith('ghcr.io/') for artifact in descriptor['artifacts'].values()):
            raise ValueError('registry authority differs from the release artifact family')
        body['registry'] = authority
    raw = oci_registry.canonical(body)
    if len(raw) > MAX_REQUEST - (32 << 10):
        raise ValueError('submission exceeds its byte bound')
    audience = 'komizo-request:sha256:' + hashlib.sha256(raw).hexdigest()
    body['proof'] = token_request(environment, audience)
    encoded = oci_registry.canonical(body)
    if len(encoded) > MAX_REQUEST:
        raise ValueError('submission exceeds its byte bound')
    return body


def receipt(raw, submitted):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_REQUEST:
        raise ValueError('submission outcome is unknown; inspect the durable operation before retrying')
    try:
        value = json.loads(raw, object_pairs_hook=oci_registry.unique_object)
    except (ValueError, UnicodeError):
        raise ValueError('submission outcome is unknown; inspect the durable operation before retrying') from None
    if submitted['action'] == 'inspect' and submitted['release_digest'] == 'current':
        required = {'kind', 'version', 'application', 'instance', 'installed'}
        if (not isinstance(value, dict) or not required <= set(value) <= required | {'plan'} or value.get('kind') != 'komizo.instance'
                or type(value.get('version')) is not int or value['version'] != 1
                or any(value.get(key) != submitted[key] for key in ['application', 'instance'])):
            raise ValueError('instance inspection returned an invalid identity')
        installed = value['installed']
        if installed is not None and (not isinstance(installed, dict)
                or set(installed) != {'release_digest', 'desired_running', 'operation_id'}
                or not isinstance(installed['release_digest'], str) or not oci_registry.DIGEST.fullmatch(installed['release_digest'])
                or type(installed['desired_running']) is not bool
                or not isinstance(installed['operation_id'], str) or not NONCE.fullmatch(installed['operation_id'])):
            raise ValueError('instance inspection returned invalid installed state')
        if 'plan' in value:
            plan = value['plan']
            if (submitted['instance'] == 'production' or not isinstance(plan, dict)
                    or set(plan) != {'app_origin', 'api_origin'}):
                raise ValueError('instance inspection returned invalid preview route policy')
            for origin in plan.values():
                if not isinstance(origin, str) or len(origin) > 2048 or any(ord(c) < 33 for c in origin):
                    raise ValueError('preview route is not a bounded HTTPS origin')
                url = urllib.parse.urlsplit(origin)
                if (url.scheme != 'https' or not url.hostname or not re.fullmatch('[a-z0-9.-]+', url.hostname)
                        or url.username or url.password or url.port or url.query or url.fragment
                        or not re.fullmatch(r'(?:/[a-zA-Z0-9_-]+)*', url.path)):
                    raise ValueError('preview route is not a bounded HTTPS origin')
        return value
    required = {'kind', 'version', 'operation_id', 'application', 'instance', 'release_digest',
                'phase', 'ok', 'reconciliation_required'}
    optional = {'previous_release_digest'} | ({'backup'} if submitted['action'] == 'backup' else set())
    if (not isinstance(value, dict) or not required <= set(value) <= required | optional
            or value.get('kind') != 'komizo.operation' or type(value.get('version')) is not int
            or value['version'] != 1 or not isinstance(value.get('operation_id'), str)
            or not NONCE.fullmatch(value['operation_id'])
            or any(value.get(key) != submitted[key] for key in ['application', 'instance', 'release_digest'])
            or type(value.get('ok')) is not bool or type(value.get('reconciliation_required')) is not bool
            or not isinstance(value['phase'], str) or value['phase'] not in {'ready', 'prepared_stopped', 'failed', 'needs_reconciliation'}
            or value['ok'] != (value['phase'] in {'ready', 'prepared_stopped'})
            or (value['phase'] == 'needs_reconciliation' and not value['reconciliation_required'])
            or ('previous_release_digest' in value and value['previous_release_digest'] is not None
                and not (isinstance(value['previous_release_digest'], str)
                         and oci_registry.DIGEST.fullmatch(value['previous_release_digest'])))):
        raise ValueError('submission outcome is unknown; inspect the durable operation before retrying')
    if submitted['action'] in {'deploy', 'start', 'stop', 'restart', 'backup', 'remove'}:
        if value['operation_id'] != proposed_operation(submitted):
            raise ValueError('submission completion belongs to another request nonce')
    elif submitted.get('operation') is not None and value['operation_id'] != submitted['operation']:
        raise ValueError('inspection completion belongs to another selected operation')
    if submitted['action'] == 'backup' and value['ok']:
        backup = value.get('backup')
        if backup == {'status': 'absent'}:
            if submitted['release_digest'] != 'absent':
                raise ValueError('backup absence differs from exact instance target')
        elif (not isinstance(backup, dict) or set(backup) != {'status', 'files'} or backup['status'] != 'ready'
                or not isinstance(backup['files'], list) or len(backup['files']) != 2):
            raise ValueError('backup completion lacks its exact encrypted file inventory')
        else:
            found = set()
            for file in backup['files']:
                if (not isinstance(file, dict) or set(file) != {'name', 'size_bytes', 'sha256'}
                        or not isinstance(file['name'], str) or file['name'] not in {'snapshot.cms', 'receipt.json'} or file['name'] in found
                        or type(file['size_bytes']) is not int or not 0 < file['size_bytes'] <= (8 << 30 if file['name'] == 'snapshot.cms' else 4 << 20)
                        or not isinstance(file['sha256'], str) or not oci_registry.DIGEST.fullmatch(file['sha256'])):
                    raise ValueError('backup inventory contains an invalid ciphertext identity')
                found.add(file['name'])
    return value


def transport(arguments, *, input, timeout, max_output=MAX_REQUEST):
    if type(max_output) is not int or not 1 <= max_output <= 32 << 20:
        raise ValueError('transport output requires a finite byte bound')
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    output = bytearray()
    error_size = 0
    offset = 0
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, events in [(process.stdin, selectors.EVENT_WRITE), (process.stdout, selectors.EVENT_READ),
                                 (process.stderr, selectors.EVENT_READ)]:
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, events)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('submission exceeded its completion deadline')
                for event, _ in selector.select(min(1, remaining)):
                    pipe = event.fileobj
                    if pipe is process.stdin:
                        offset += os.write(pipe.fileno(), input[offset:offset + 65536])
                        if offset == len(input):
                            selector.unregister(pipe)
                            pipe.close()
                    else:
                        block = os.read(pipe.fileno(), 65536)
                        if not block:
                            selector.unregister(pipe)
                            pipe.close()
                        elif pipe is process.stdout:
                            output.extend(block)
                            if len(output) > max_output:
                                raise ValueError('submission response exceeds its bound')
                        else:
                            error_size += len(block)
                            if error_size > 65536:
                                raise ValueError('submission error response exceeds its bound')
            if process.wait(timeout=max(.01, deadline - time.monotonic())) != 0:
                raise ValueError('submission did not return an authenticated completion')
        return bytes(output)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        for pipe in [process.stdin, process.stdout, process.stderr]:
            pipe.close()


def ssh_arguments(config):
    config = Path(config)
    with release_packet.regular(config, 65536) as (_, info):
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('submission requires private job-owned SSH configuration')
    return ['ssh', '-F', str(config), '-T', '-o', 'BatchMode=yes',
                 '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
                 '-o', 'ServerAliveCountMax=3', '-o', 'ControlMaster=no',
                 '-o', 'ControlPath=none', 'deploy-target', 'komizo-box submit']


def submit(config, submitted, *, timeout=1500, execute=transport):
    if type(timeout) is not int or not 1 <= timeout <= 1500:
        raise ValueError('submission timeout must be between one second and twenty-five minutes')
    encoded = oci_registry.canonical(submitted) + b'\n'
    if len(encoded) > MAX_REQUEST:
        raise ValueError('submission exceeds its byte bound')
    arguments = ssh_arguments(config)
    proposed = proposed_operation(submitted) if submitted['action'] in {'deploy', 'start', 'stop', 'restart', 'backup', 'remove'} else None
    if proposed and os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('proposed-operation-id=' + proposed + '\n')
    try:
        raw = execute(arguments, input=encoded, timeout=min(timeout, submitted['timeout_seconds']))
        return receipt(raw, submitted)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        suffix = '; proposed operation ' + proposed + ' is not confirmed admitted' if proposed else ''
        raise ValueError('submission outcome is unknown; inspect before retrying' + suffix) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', required=True)
    parser.add_argument('--instance', default='production')
    parser.add_argument('--action', default='deploy', choices=sorted(ACTIONS))
    parser.add_argument('--release', type=Path)
    parser.add_argument('--release-digest')
    parser.add_argument('--release-proof', type=Path)
    parser.add_argument('--expected-secret-generations', default='{}')
    parser.add_argument('--ttl-seconds', type=int)
    parser.add_argument('--operation')
    parser.add_argument('--ssh-config', type=Path)
    parser.add_argument('--timeout', type=int, default=1500)
    args = parser.parse_args()
    try:
        config = args.ssh_config or Path(os.environ['KOMIZO_SSH_CONFIG'])
        descriptor = release_packet.document(args.release) if args.release else None
        published = release_packet.document(args.release_proof) if args.release_proof else None
        body = request(args.application, args.instance, args.action, os.environ,
                       descriptor=descriptor, release_digest=args.release_digest,
                       release_proof=published, operation=args.operation,
                       ttl_seconds=args.ttl_seconds,
                       timeout_seconds=args.timeout,
                       expected_secret_generations=json.loads(args.expected_secret_generations,
                           object_pairs_hook=oci_registry.unique_object))
        result = submit(config, body, timeout=args.timeout)
        if os.environ.get('GITHUB_OUTPUT') and result['kind'] == 'komizo.operation':
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                for key in ['operation_id', 'release_digest', 'phase', 'ok', 'reconciliation_required']:
                    value = str(result[key]).lower() if isinstance(result[key], bool) else result[key]
                    output.write(key.replace('_', '-') + '=' + value + '\n')
        if result['kind'] == 'komizo.operation':
            path = Path('.artifacts/komizo-operation.json'); path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x') as record:
                os.fchmod(record.fileno(), 0o600); record.write(oci_registry.canonical(result).decode())
        print(oci_registry.canonical(result).decode())
        if result['kind'] == 'komizo.operation' and not result['ok']:
            raise SystemExit(1)
    except (KeyError, OSError, ValueError, TypeError):
        proposed = proposed_operation(body) if 'body' in locals() and body['action'] in {'deploy', 'start', 'stop', 'restart', 'backup', 'remove'} else None
        detail = ' Proposed operation ' + proposed + ' is not confirmed admitted.' if proposed else ''
        parser.exit(1, 'Komizo submission refused or outcome unknown; inspect before retrying.' + detail + '\n')


if __name__ == '__main__':
    main()
