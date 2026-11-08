"""Preserve encrypted evidence and atomic directory ownership across failure and races."""
from contextlib import chdir
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('capture_transport', Path(__file__).resolve().parents[1] / 'scripts/backup_transport.py')
transport = importlib.util.module_from_spec(spec); spec.loader.exec_module(transport)


class AtomicBackupPublication(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name); self.directory = self.root / '.artifacts/backup'
        self.directory.mkdir(parents=True)
        self.sentinel = self.directory / 'retained-evidence'; self.sentinel.write_bytes(b'preserve')

    def produce(self, stage, descriptors):
        self.assertEqual(len(descriptors), 1); self.assertTrue(os.path.isdir('/proc/self/fd/' + str(descriptors[0])))
        output = stage / 'export'; output.mkdir(mode=0o700)
        for name in ['snapshot.cms', 'receipt.json']:
            with (output / name).open('xb') as stream: os.fchmod(stream.fileno(), 0o600); stream.write(b'verified ciphertext')
        return 'export'

    def capture(self, produce=None):
        return transport.capture(self.root, Path('.artifacts/backup'), 'mailroom', produce or self.produce)

    def test_verified_pair_is_published_atomically_without_touching_retained_evidence(self):
        self.assertEqual(self.capture(), 'export')
        self.assertEqual({path.name for path in self.directory.iterdir()}, {'retained-evidence', 'export'})
        self.assertEqual(self.sentinel.read_bytes(), b'preserve')
        for path in (self.directory / 'export').iterdir(): self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_existing_output_identities_never_trigger_capture_or_replace_evidence(self):
        for name in ['export', 'export.tar', 'first-deployment.json', 'stopped.json']:
            for kind in ['file', 'directory', 'symlink', 'fifo']:
                with self.subTest(name=name, kind=kind):
                    target = self.directory / name
                    if kind == 'directory': target.mkdir()
                    elif kind == 'symlink': target.symlink_to('missing')
                    elif kind == 'fifo': os.mkfifo(target)
                    else: target.write_bytes(b'prior evidence')
                    def unexpected(*_): self.fail('capture must not run with prior output')
                    with self.assertRaises(RuntimeError): self.capture(unexpected)
                    if kind == 'directory': target.rmdir()
                    else: target.unlink()

    def test_capture_failure_cleans_only_own_staging(self):
        def failure(stage, descriptors):
            self.produce(stage, descriptors); raise ValueError('uncertain export')
        with self.assertRaises(ValueError): self.capture(failure)
        self.assertEqual({path.name for path in self.directory.iterdir()}, {'retained-evidence'})
        self.assertEqual(self.sentinel.read_bytes(), b'preserve')

    def test_atomic_noreplace_refuses_output_created_after_last_check(self):
        original = transport.refuse_prior_output; calls = 0
        def race(directory):
            nonlocal calls
            original(directory); calls += 1
            if calls == 2: (self.directory / 'export').symlink_to('missing')
        with patch.object(transport, 'refuse_prior_output', race), self.assertRaises(FileExistsError): self.capture()
        self.assertTrue((self.directory / 'export').is_symlink())
        self.assertEqual({path.name for path in self.directory.iterdir()}, {'retained-evidence', 'export'})

    def test_parent_swap_cannot_redirect_publication_or_delete_other_evidence(self):
        original = self.directory
        def swapped(stage, descriptors):
            result = self.produce(stage, descriptors)
            original.rename(original.with_name('retained'))
            original.mkdir(); (original / 'foreign').write_bytes(b'foreign')
            return result
        with self.assertRaises(RuntimeError): self.capture(swapped)
        self.assertEqual((original / 'foreign').read_bytes(), b'foreign')
        self.assertEqual((original.with_name('retained') / 'retained-evidence').read_bytes(), b'preserve')

    def test_parent_symlinks_platform_and_storage_authority_refuse(self):
        with patch.object(transport.sys, 'platform', 'darwin'), self.assertRaises(RuntimeError): self.capture()
        with patch.dict(os.environ, {'AWS_SECRET_ACCESS_KEY': 'fixture'}), self.assertRaises(ValueError): self.capture()
        foreign = self.root / 'foreign'; foreign.mkdir(); self.directory.rmdir() if not any(self.directory.iterdir()) else None
        # Test a separate checkout whose artifact parent is redirected.
        other = self.root / 'other'; other.mkdir(); (other / '.artifacts').symlink_to(foreign)
        with self.assertRaises(OSError): transport.capture(other, Path('.artifacts/backup'), 'mailroom', self.produce)


if __name__ == '__main__': unittest.main()
