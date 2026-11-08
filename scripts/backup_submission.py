"""Receive exact ciphertext exports through the same bounded submission transport."""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time

import oci_registry
import submission
from release_packet import regular


def export_header(raw, request, expected):
    if not raw or len(raw) > 16384:
        raise ValueError('ciphertext export header exceeds its bound')
    value = json.loads(raw, object_pairs_hook=oci_registry.unique_object)
    if value != {'kind': 'komizo.export', 'version': 1, 'operation_id': request['operation'],
                 'application': request['application'], 'instance': request['instance'],
                 'release_digest': request['release_digest'], 'export_name': expected['name'],
                 'size_bytes': expected['size_bytes'], 'sha256': expected['sha256']}:
        raise ValueError('ciphertext export differs from its durable backup receipt')
    if type(value['version']) is not int or type(value['size_bytes']) is not int:
        raise ValueError('ciphertext export metadata types are invalid')
    return value


def download(config, request, expected, target, *, timeout=900, arguments=None, pass_fds=()):
    if request.get('action') != 'export' or request.get('export_name') != expected['name']:
        raise ValueError('ciphertext retrieval requires an exact export request')
    argv = arguments or submission.ssh_arguments(config)
    body = oci_registry.canonical(request) + b'\n'
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, pass_fds=pass_fds)
    header = bytearray(); admitted = False; count = 0; errors = 0; offset = 0; actual = hashlib.sha256()
    try:
        with Path(target).open('xb') as output, selectors.DefaultSelector() as selector:
            os.fchmod(output.fileno(), 0o600)
            for pipe, events in [(process.stdin, selectors.EVENT_WRITE), (process.stdout, selectors.EVENT_READ), (process.stderr, selectors.EVENT_READ)]:
                os.set_blocking(pipe.fileno(), False); selector.register(pipe, events)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise ValueError('ciphertext export exceeded its deadline')
                for event, _ in selector.select(min(1, remaining)):
                    pipe = event.fileobj
                    if pipe is process.stdin:
                        offset += os.write(pipe.fileno(), body[offset:offset + 65536])
                        if offset == len(body): selector.unregister(pipe); pipe.close()
                        continue
                    block = os.read(pipe.fileno(), 65536)
                    if not block: selector.unregister(pipe); pipe.close(); continue
                    if pipe is process.stderr:
                        errors += len(block)
                        if errors > 65536: raise ValueError('ciphertext transport errors exceed their bound')
                        continue
                    if not admitted:
                        before, separator, after = block.partition(b'\n')
                        header.extend(before)
                        if len(header) > 16384: raise ValueError('ciphertext export header exceeds its bound')
                        if not separator: continue
                        export_header(bytes(header), request, expected); admitted = True; block = after
                    count += len(block)
                    if count > expected['size_bytes']: raise ValueError('ciphertext export contains trailing bytes')
                    actual.update(block); output.write(block)
            if process.wait(timeout=max(.01, deadline - time.monotonic())) != 0:
                raise ValueError('ciphertext transport did not finish successfully')
            if not admitted or count != expected['size_bytes'] or 'sha256:' + actual.hexdigest() != expected['sha256']:
                raise ValueError('ciphertext export differs from its admitted bytes')
            output.flush(); os.fsync(output.fileno())
    except BaseException:
        Path(target).unlink(missing_ok=True)
        raise
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
        for pipe in [process.stdin, process.stdout, process.stderr]: pipe.close()


def capture(project, stage, environment, *, pass_fds=(), timeout=900):
    deadline = time.monotonic() + timeout
    def remaining():
        value = int(deadline - time.monotonic())
        if value < 1: raise ValueError('encrypted backup capture exceeded its original deadline')
        return value
    instance = 'production'
    config = environment['KOMIZO_SSH_CONFIG']
    current = submission.request(project, instance, 'inspect', environment, release_digest='current', timeout_seconds=remaining())
    installed = submission.submit(config, current, timeout=remaining())['installed']
    target = installed['release_digest'] if installed is not None else 'absent'
    body = submission.request(project, instance, 'backup', environment, release_digest=target, timeout_seconds=remaining())
    result = submission.submit(config, body, timeout=remaining())
    if not result['ok']: raise ValueError('protected encrypted backup did not complete')
    if result['backup']['status'] == 'absent':
        path = Path(stage) / 'first-deployment.json'
        with path.open('x') as output:
            os.fchmod(output.fileno(), 0o600); json.dump(result, output, sort_keys=True)
        return 'first-deployment.json'
    destination = Path(stage) / 'export'; destination.mkdir(mode=0o700)
    for file in result['backup']['files']:
        body = submission.request(project, instance, 'export', environment, release_digest=target,
                                  operation=result['operation_id'], export_name=file['name'], timeout_seconds=remaining())
        download(config, body, file, destination / file['name'], timeout=remaining(), pass_fds=pass_fds)
    receipt = json.loads((destination / 'receipt.json').read_bytes(), object_pairs_hook=oci_registry.unique_object)
    if (not isinstance(receipt, dict) or receipt.get('format') != 1
            or any(not isinstance(receipt.get(key), str) or not re.fullmatch('[a-f0-9]{64}', receipt[key])
                   for key in ['archive_sha256', 'ciphertext_sha256'])
            or not isinstance(receipt.get('image_id'), str) or not oci_registry.DIGEST.fullmatch(receipt['image_id'])):
        raise ValueError('encrypted backup receipt has an invalid envelope identity')
    with regular(destination / 'snapshot.cms', 8 << 30) as (stream, _):
        if hashlib.file_digest(stream, 'sha256').hexdigest() != receipt['ciphertext_sha256']:
            raise ValueError('encrypted backup pair has inconsistent ciphertext integrity')
    return 'export'
