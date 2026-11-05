#!/usr/bin/env python3
"""Capture and atomically publish a validated encrypted backup without replacing evidence.

Linux directory FDs prevent parent symlink redirection. Directory flock excludes
local attempts; renameat2(RENAME_NOREPLACE) atomically publishes only fresh output.
"""
import argparse
from contextlib import contextmanager
import ctypes
import fcntl
import importlib.util
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile


class Interrupted(SystemExit):
    def __init__(self, signum):
        self.status = 128 + signum
        super().__init__(self.status)


def interrupt(signum, frame):
    # Finish owned cleanup even if the caller sends another termination signal.
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN)
    raise Interrupted(signum)


def run(command, *, timeout=900, **kwargs):
    child = subprocess.Popen(command, **kwargs)
    try:
        status = child.wait(timeout=timeout)
        if status:
            raise SystemExit(status if status > 0 else 128 - status)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def open_directory(name, parent):
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def refuse_prior_output(backup):
    for name in ('export', 'export.tar', 'first-deployment.json'):
        try:
            os.stat(name, dir_fd=backup, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise RuntimeError(f'preserving existing backup/{name}; use a fresh checkout')


def check_parent(name, parent, opened):
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    original = os.fstat(opened)
    if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
        raise RuntimeError('backup parent changed during export; refusing publication')


@contextmanager
def signals():
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, interrupt)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def capture(root, directory, project, exporter, receive, *, proof=None, timeout=900):
    """Receive into a private FD-anchored stage; callback returns one canonical role.

    ``receive(archive, stage, pass_fds)`` must validate all ciphertext or the
    typed initialization proof and return 'export' or 'first-deployment.json'.
    The callback's child processes must inherit ``pass_fds`` when using stage
    paths. Only the returned role is published, using RENAME_NOREPLACE.
    """
    if sys.platform != 'linux':
        raise RuntimeError('backup export requires a Linux runner')
    if (not isinstance(project, str) or not re.fullmatch('[a-z][a-z0-9-]{0,62}', project)
            or exporter not in {f'export-{project}-backup', f'export-backup-{project}'}
            or type(timeout) is not int or not 1 <= timeout <= 900):
        raise ValueError('backup capture requires a fixed project exporter and bounded deadline')
    root = Path(root).absolute()
    directory = Path(directory)
    if directory.is_absolute():
        directory = directory.relative_to(root)
    if directory.parts != ('.artifacts', 'backup'):
        raise ValueError('backup capture uses only the checkout-owned artifact directory')
    if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
        raise ValueError('backup checkout root must not traverse symlinks')
    # No fallback to check-then-rename: ordinary rename can replace a directory.
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    root = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    artifacts = backup = None
    stage = None
    try:
        artifacts = open_directory('.artifacts', root)
        backup = open_directory('backup', artifacts)
        # Lock the directory inode, not a lockfile that cleanup could unlink.
        # Nonblocking and held through publication/cleanup, independent of CI queues.
        try:
            fcntl.flock(backup, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('another local backup export is running') from error
        refuse_prior_output(backup)
        stage_path = Path(tempfile.mkdtemp(prefix='.export-', dir=f'/proc/self/fd/{backup}'))
        stage = stage_path.name
        incoming = None
        try:
            if proof is not None:
                proof = Path(proof)
                if (not proof.is_absolute() or proof.is_symlink() or any(p.is_symlink() for p in proof.parents)
                        or not proof.is_file() or proof.stat().st_mode & 0o077):
                    raise ValueError('backup operation frame must be a private regular absolute file')
                incoming = proof.open('rb')
            with (stage_path / 'export.tar').open('xb') as archive:
                os.fchmod(archive.fileno(), 0o600)
                run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                     'deploy-target', 'doas', '/usr/local/bin/' + exporter],
                    stdin=incoming or subprocess.DEVNULL, stdout=archive, timeout=timeout)
        finally:
            if incoming is not None:
                incoming.close()
        role = receive(stage_path / 'export.tar', stage_path, (backup,))
        if role not in {'export', 'first-deployment.json'}:
            raise ValueError('backup receiver returned an unsupported publication role')
        accepted = stage_path / role
        if accepted.is_symlink() or not (accepted.is_dir() if role == 'export' else accepted.is_file()):
            raise ValueError('backup receiver did not produce its declared regular output')
        check_parent('.artifacts', root, artifacts)
        check_parent('backup', artifacts, backup)
        refuse_prior_output(backup)
        # RENAME_NOREPLACE (Linux): fail even if an empty directory or dangling
        # symlink appears after the check. Both paths are anchored to the same FD.
        if rename(backup, os.fsencode(f'{stage}/{role}'), backup, os.fsencode(role), 1):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return role
    finally:
        # Only this invocation's unique staging is removed. Never remove canonical
        # output or unrelated retained evidence, even on an interrupted attempt.
        failed = sys.exc_info()[0] is not None
        try:
            if stage is not None:
                shutil.rmtree(stage, dir_fd=backup)
        except OSError as error:
            if not failed:
                raise
            # Cleanup trouble must not mask the original SSH/receiver/signal status.
            print(f'backup staging cleanup failed: {error}', file=sys.stderr)
        finally:
            for fd in (backup, artifacts, root):
                if fd is not None:
                    os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--exporter', required=True)
    parser.add_argument('--tools-root', required=True, type=Path)
    args = parser.parse_args()
    receiver = args.tools_root / 'helpers/receive-backup.py'
    def receive(archive, stage, descriptors):
        run([sys.executable, str(receiver), str(archive), str(stage/'export')], pass_fds=descriptors)
        return 'export'
    try:
        with signals():
            capture(Path.cwd(), Path('.artifacts/backup'), args.project, args.exporter, receive)
    except (OSError, RuntimeError, AttributeError, ValueError) as error:
        parser.exit(1, f'backup export refused or failed: {error}\n')


if __name__ == '__main__':
    main()
