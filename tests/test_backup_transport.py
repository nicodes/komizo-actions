#!/usr/bin/env python3
"""Exercise the real runner/receiver with synthetic ciphertext and a mock SSH."""
from contextlib import chdir
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = b'fixture ciphertext, not a production backup'


class BackupRunner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='backup-runner-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Use the real generic capture and encrypted receiver, with synthetic SSH.
        snapshot = self.root / 'snapshot'
        (snapshot / 'helpers').mkdir(parents=True)
        (self.root / 'scripts').mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/'scripts/backup_transport.py',self.root/'scripts/backup_transport.py')
        (self.root/'scripts/export-backup.sh').write_text(
            '#!/bin/sh\nexec python3 scripts/backup_transport.py --project demo '
            '--exporter export-demo-backup --tools-root "$TOOLS_ROOT"\n')
        shutil.copy2(Path(os.environ['TOOLS_ROOT'])/'helpers/receive-backup.py',snapshot/'helpers/receive-backup.py')
        self.backup = self.root / '.artifacts/backup'
        self.backup.mkdir(parents=True)
        self.sentinel = self.backup / 'retained-evidence'
        self.sentinel.write_bytes(b'never remove this')
        self.archive = self.root / 'fixture.tar'
        self.make_archive()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        ssh = self.bin / 'ssh'
        ssh.write_text(f'''#!{sys.executable}
import os, pathlib, signal, sys, time
assert sys.argv[1:] == ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', 'deploy-target', 'doas', '/usr/local/bin/export-demo-backup']
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
(root / 'ssh-called').touch()
mode = os.environ.get('FIXTURE_MODE', '')
if mode == 'partial':
    sys.stdout.buffer.write(b'incomplete transport')
    sys.exit(23)
if mode == 'wait':
    sys.stdout.buffer.write(b'partial')
    sys.stdout.buffer.flush()
    (root / 'ready').touch()
    deadline = time.monotonic() + 8
    while not (root / 'release').exists():
        if time.monotonic() > deadline:
            sys.exit(24)
        time.sleep(0.01)
sys.stdout.buffer.write((root / 'fixture.tar').read_bytes())
''')
        ssh.chmod(0o700)
        self.env = {**os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}',
                    'FIXTURE_ROOT': str(self.root),
                    # Point the scripts under test at the sandbox's own copy,
                    # not at the real installed snapshot.
                    'TOOLS_ROOT': str(snapshot)}

    def make_archive(self, case='valid'):
        receipt = {'format': 1, 'archive_sha256': 'a' * 64,
                   'ciphertext_sha256': hashlib.sha256(PAYLOAD).hexdigest(),
                   'image_id': 'sha256:' + 'b' * 64}
        entries = [('snapshot.cms', PAYLOAD), ('receipt.json', json.dumps(receipt).encode())]
        if case == 'checksum':
            entries[0] = ('snapshot.cms', b'changed')
        elif case == 'incomplete':
            entries.pop()
        elif case == 'duplicate':
            entries.append(entries[0])
        elif case == 'unsafe':
            entries[0] = ('../snapshot.cms', PAYLOAD)
        elif case == 'invalid':
            self.archive.write_bytes(b'not a tar archive')
            return
        with tarfile.open(self.archive, 'w') as archive:
            for name, data in entries:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def run_runner(self, mode=''):
        return subprocess.run(['bash', 'scripts/export-backup.sh'], cwd=self.root,
                              env={**self.env, 'FIXTURE_MODE': mode},
                              capture_output=True, text=True, timeout=15)

    def assert_clean(self, success=False):
        expected = {'retained-evidence', 'export'} if success else {'retained-evidence'}
        self.assertEqual({p.name for p in self.backup.iterdir()}, expected)
        self.assertEqual(self.sentinel.read_bytes(), b'never remove this')

    def start_waiting(self, mode='wait'):
        process = subprocess.Popen(['bash', 'scripts/export-backup.sh'], cwd=self.root,
                                   env={**self.env, 'FIXTURE_MODE': mode},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)

        def stop():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.communicate(timeout=10)
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while not (self.root / 'ready').exists():
            if process.poll() is not None or time.monotonic() > deadline:
                self.fail('mock SSH did not reach ready state')
            time.sleep(0.01)
        return process

    def test_atomic_publication_refuses_output_created_after_final_check(self):
        spec = importlib.util.spec_from_file_location('runner', ROOT / 'scripts/backup_transport.py')
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        original_check = runner.refuse_prior_output
        original_run = runner.run
        target = self.backup / 'export'
        for kind in ('empty-directory', 'dangling'):
            with self.subTest(kind=kind):
                checks = 0

                def race(backup):
                    nonlocal checks
                    original_check(backup)
                    checks += 1
                    if checks == 2:
                        if kind == 'empty-directory':
                            target.mkdir()
                        else:
                            target.symlink_to('missing')

                def quiet_run(command, **kwargs):
                    kwargs.setdefault('stdout', subprocess.DEVNULL)
                    original_run(command, **kwargs)

                with chdir(self.root), patch.dict(os.environ, self.env), \
                        patch.object(runner, 'refuse_prior_output', race), \
                        patch.object(runner, 'run', quiet_run):
                    with self.assertRaises(FileExistsError):
                        def receive(archive,stage,descriptors):
                            runner.run([sys.executable,str(Path(self.env['TOOLS_ROOT'])/'helpers/receive-backup.py'),
                                        str(archive),str(stage/'export')],pass_fds=descriptors)
                            return 'export'
                        runner.capture(Path.cwd(),Path('.artifacts/backup'),'demo','export-demo-backup',receive)
                self.assertEqual(checks, 2)
                if kind == 'empty-directory':
                    self.assertEqual(list(target.iterdir()), [])
                    target.rmdir()
                else:
                    self.assertTrue(target.is_symlink())
                    self.assertEqual(os.readlink(target), 'missing')
                    target.unlink()
                self.assert_clean()

    def test_unsupported_platform_fails_closed(self):
        result = subprocess.run(
            [sys.executable, '-c', "import runpy, sys; sys.platform = 'darwin'; "
             "sys.argv=['scripts/backup_transport.py','--project','demo','--exporter','export-demo-backup',"
             "'--tools-root',__import__('os').environ['TOOLS_ROOT']]; "
             "runpy.run_path('scripts/backup_transport.py', run_name='__main__')"],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn('requires a Linux runner', result.stderr)
        self.assertFalse((self.root / 'ssh-called').exists())
        self.assert_clean()

    def test_success_private_complete_publication(self):
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_clean(success=True)
        target = self.backup / 'export'
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual({p.name for p in target.iterdir()}, {'snapshot.cms', 'receipt.json'})
        for path in target.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual((target / 'snapshot.cms').read_bytes(), PAYLOAD)

    def test_success_creates_missing_private_parents(self):
        previous = self.root / 'previous-artifacts'
        (self.root / '.artifacts').rename(previous)
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in self.backup.iterdir()}, {'export'})
        for parent in (self.root / '.artifacts', self.backup):
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
        self.assertEqual((previous / 'backup/retained-evidence').read_bytes(), b'never remove this')

    def test_partial_ssh_failure_preserves_status_and_cleans(self):
        result = self.run_runner('partial')
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assert_clean()

    def test_receiver_rejections_leave_no_canonical_or_partial_output(self):
        for case, message in (('invalid', 'truncated header'),
                              ('unsafe', 'unexpected, duplicate, or unsafe'),
                              ('incomplete', 'encrypted export is incomplete'),
                              ('duplicate', 'unexpected, duplicate, or unsafe'),
                              ('checksum', 'transport checksum mismatch')):
            with self.subTest(case=case):
                self.make_archive(case)
                result = self.run_runner()
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assert_clean()

    def test_existing_outputs_are_preserved_before_ssh(self):
        for name in ('export', 'export.tar'):
            for kind in ('file', 'directory', 'empty-directory', 'fifo', 'symlink', 'dangling'):
                with self.subTest(name=name, kind=kind):
                    path = self.backup / name
                    if kind == 'file':
                        path.write_bytes(b'prior output')
                    elif kind in ('directory', 'empty-directory'):
                        path.mkdir()
                        if kind == 'directory':
                            (path / 'evidence').write_bytes(b'prior output')
                    elif kind == 'fifo':
                        os.mkfifo(path)
                    else:
                        path.symlink_to(self.sentinel if kind == 'symlink' else 'missing')
                    before = path.lstat()
                    result = self.run_runner()
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertFalse((self.root / 'ssh-called').exists())
                    self.assertEqual(path.lstat(), before)
                    self.assertEqual(self.sentinel.read_bytes(), b'never remove this')
                    if kind == 'file':
                        self.assertEqual(path.read_bytes(), b'prior output')
                    elif kind in ('directory', 'empty-directory'):
                        if kind == 'directory':
                            self.assertEqual((path / 'evidence').read_bytes(), b'prior output')
                        shutil.rmtree(path)
                        continue
                    path.unlink()

    def test_unsafe_parents_are_refused(self):
        for relative in ('.artifacts/backup', '.artifacts'):
            for kind in ('symlink', 'dangling', 'file'):
                with self.subTest(parent=relative, kind=kind):
                    parent = self.root / relative
                    saved = parent.with_name(parent.name + '-saved')
                    parent.rename(saved)
                    if kind == 'file':
                        parent.write_bytes(b'prior parent')
                    else:
                        parent.symlink_to(saved if kind == 'symlink' else 'missing', target_is_directory=True)
                    try:
                        result = self.run_runner()
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse((self.root / 'ssh-called').exists())
                    finally:
                        parent.unlink()
                        saved.rename(parent)
                    self.assert_clean()

    def test_interrupts_preserve_status_and_owned_cleanup(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=sig):
                process = self.start_waiting()
                os.kill(process.pid, sig)
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 128 + sig, stderr)
                self.assert_clean()
                (self.root / 'ready').unlink()

    def test_concurrent_attempt_is_refused_without_touching_first(self):
        first = self.start_waiting()
        stages = list(self.backup.glob('.export-*'))
        self.assertEqual(len(stages), 1)
        self.assertEqual(stat.S_IMODE(stages[0].stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((stages[0] / 'export.tar').stat().st_mode), 0o600)
        self.assertFalse((self.backup / 'export').exists())
        second = self.run_runner()
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('another local backup export is running', second.stderr)
        self.assertEqual(list(self.backup.glob('.export-*')), stages)
        os.kill(first.pid, signal.SIGTERM)
        first.communicate(timeout=10)
        self.assert_clean()

    def test_receiver_failure_status_is_preserved(self):
        receiver = self.root / 'snapshot/helpers/receive-backup.py'
        receiver.write_text('raise SystemExit(19)\n')
        result = self.run_runner()
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assert_clean()

    def test_interrupt_during_receiver_cleans_partial_validation(self):
        receiver = self.root / 'snapshot/helpers/receive-backup.py'
        receiver.write_text("""import os, pathlib, sys, time
pathlib.Path(sys.argv[2]).mkdir()
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
(root / 'ready').touch()
time.sleep(8)
""")
        first = self.start_waiting(mode='')
        os.kill(first.pid, signal.SIGTERM)
        _, stderr = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 143, stderr)
        self.assert_clean()

    def test_parent_redirection_during_transfer_is_refused(self):
        first = self.start_waiting()
        saved = self.backup.with_name('backup-original')
        self.backup.rename(saved)
        redirected = self.root / 'redirected'
        redirected.mkdir()
        self.backup.symlink_to(redirected, target_is_directory=True)
        (self.root / 'release').touch()
        _, stderr = first.communicate(timeout=10)
        self.assertNotEqual(first.returncode, 0, stderr)
        self.assertEqual(list(redirected.iterdir()), [])
        self.assertEqual({p.name for p in saved.iterdir()}, {'retained-evidence'})
        self.assertEqual((saved / 'retained-evidence').read_bytes(), b'never remove this')

    def test_legacy_tar_created_during_transfer_is_preserved(self):
        first = self.start_waiting()
        target = self.backup / 'export.tar'
        target.write_bytes(b'late prior archive')
        (self.root / 'release').touch()
        _, stderr = first.communicate(timeout=10)
        self.assertNotEqual(first.returncode, 0, stderr)
        self.assertEqual(target.read_bytes(), b'late prior archive')
        target.unlink()
        self.assert_clean()

    def test_output_created_during_transfer_is_not_clobbered(self):
        first = self.start_waiting()
        target = self.backup / 'export'
        target.mkdir()
        (target / 'evidence').write_bytes(b'late output')
        (self.root / 'release').touch()
        _, stderr = first.communicate(timeout=10)
        self.assertNotEqual(first.returncode, 0, stderr)
        self.assertEqual((target / 'evidence').read_bytes(), b'late output')
        self.assert_clean(success=True)


if __name__ == '__main__':
    unittest.main()
