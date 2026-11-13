"""Receive exact ciphertext exports through the same bounded submission transport."""
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time

import oci_registry
import submission
from release_packet import fingerprint, regular


@contextmanager
def regular_at(directory, name, maximum):
    """Read only a named encrypted member under an already admitted directory FD."""
    if name not in {'receipt.json', 'snapshot.cms'}:
        raise ValueError('owned backup reader requires an exact encrypted member')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or not 0 <= before.st_size <= maximum
                or fingerprint(os.stat(name, dir_fd=directory, follow_symlinks=False)) != fingerprint(before)):
            raise ValueError('encrypted backup requires a bounded ordinary owned file')
        yield stream, before
        if (fingerprint(os.fstat(stream.fileno())) != fingerprint(before)
                or fingerprint(os.stat(name, dir_fd=directory, follow_symlinks=False)) != fingerprint(before)):
            raise ValueError('encrypted backup changed during admission')


def owned_parent(stage, pass_fds):
    """Recognize only the transport's explicitly passed directory capability."""
    stage = Path(stage)
    if not pass_fds:
        if stage.parts[:4] == ('/', 'proc', 'self', 'fd'):
            raise ValueError('encrypted backup proc stage requires its owned transport FD')
        return None
    if (len(pass_fds) != 1 or type(pass_fds[0]) is not int or pass_fds[0] < 0
            or stage.parts != ('/', 'proc', 'self', 'fd', str(pass_fds[0]), stage.name)
            or not re.fullmatch(r'\.export-[a-z0-9_]+', stage.name)):
        raise ValueError('encrypted backup stage differs from its owned transport FD')
    parent = pass_fds[0]
    if not stat.S_ISDIR(os.fstat(parent).st_mode):
        raise ValueError('encrypted backup transport FD must be a directory')
    return parent


@contextmanager
def export_readers(stage, pass_fds):
    """The transport's exact FD capability admits its own stage, never other proc paths."""
    stage = Path(stage)
    parent = owned_parent(stage, pass_fds)
    if parent is None:
        yield lambda name, maximum: regular(stage / 'export' / name, maximum)
        return
    stage_fd = os.open(stage.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    export_fd = None
    try:
        export_fd = os.open('export', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=stage_fd)
        identities = [(parent, stage.name, stage_fd), (stage_fd, 'export', export_fd)]
        before = []
        for directory, name, opened in identities:
            info = os.fstat(opened)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or fingerprint(os.stat(name, dir_fd=directory, follow_symlinks=False)) != fingerprint(info)):
                raise ValueError('encrypted backup stage is not its private owned directory')
            before.append(fingerprint(info))
        yield lambda name, maximum: regular_at(export_fd, name, maximum)
        for (directory, name, opened), original in zip(identities, before):
            if (fingerprint(os.fstat(opened)) != original
                    or fingerprint(os.stat(name, dir_fd=directory, follow_symlinks=False)) != original):
                raise ValueError('encrypted backup stage changed during admission')
    finally:
        if export_fd is not None:
            os.close(export_fd)
        os.close(stage_fd)


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
    owned_parent(stage, pass_fds)
    deadline = time.monotonic() + timeout
    def remaining():
        value = int(deadline - time.monotonic())
        if value < 1: raise ValueError('encrypted backup capture exceeded its original deadline')
        return value
    instance = 'production'
    config = environment['KOMIZO_SSH_CONFIG']
    current = submission.request(project, instance, 'inspect', environment, release_digest='current', timeout_seconds=remaining())
    inspection = submission.submit(config, current, timeout=remaining())
    installed = inspection['installed']
    if installed is not None and installed['desired_running'] is False:
        # This is an observed stop intent, not a completed backup or an empty host.
        # Activation independently requires a fresh backup if an owner starts the
        # instance after this inspection and before candidate admission.
        path = Path(stage) / 'stopped.json'
        with path.open('x') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump({'kind': 'komizo.backup-skip', 'version': 1,
                       'reason': 'desired_stopped', 'inspection': inspection}, output, sort_keys=True)
        return 'stopped.json'
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
    expected = {file['name']: file for file in result['backup']['files']}
    with export_readers(stage, pass_fds) as reader, \
            reader('receipt.json', 4 << 20) as (receipt_stream, _), \
            reader('snapshot.cms', 8 << 30) as (stream, info):
        raw = receipt_stream.read((4 << 20) + 1)
        remaining()
        if (len(raw) != expected['receipt.json']['size_bytes']
                or oci_registry.digest(raw) != expected['receipt.json']['sha256']):
            raise ValueError('encrypted backup receipt differs from its admitted bytes')
        receipt = json.loads(raw, object_pairs_hook=oci_registry.unique_object)
        if (not isinstance(receipt, dict) or type(receipt.get('format')) is not int or receipt['format'] != 2
                or any(not isinstance(receipt.get(key), str) or not re.fullmatch('[a-f0-9]{64}', receipt[key])
                       for key in ['archive_sha256', 'ciphertext_sha256'])
                or not isinstance(receipt.get('image_id'), str) or not oci_registry.DIGEST.fullmatch(receipt['image_id'])):
            raise ValueError('encrypted backup receipt has an invalid envelope identity')
        actual = hashlib.sha256()
        while block := stream.read(65536):
            remaining()
            actual.update(block)
        remaining()
        if (info.st_size != expected['snapshot.cms']['size_bytes']
                or 'sha256:' + actual.hexdigest() != expected['snapshot.cms']['sha256']
                or actual.hexdigest() != receipt['ciphertext_sha256']):
            raise ValueError('encrypted backup pair has inconsistent ciphertext integrity')
    return 'export'
