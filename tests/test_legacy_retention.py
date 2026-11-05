import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('legacy_retention', Path(__file__).parents[1] / 'scripts/legacy_retention.py')
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)


class LegacyRetention(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = b'opaque legacy ciphertext fixture'
        self.digest = hashlib.sha256(self.payload).hexdigest()
        (self.root / 'pb_data.tar.age').write_bytes(self.payload)
        (self.root / 'metadata').write_text('encrypted_sha256=' + self.digest + '\n')

    def test_exact_ciphertext_pair_is_verified_without_mutation(self):
        self.assertEqual(retention.verify(self.root), self.digest)
        self.assertEqual((self.root / 'pb_data.tar.age').read_bytes(), self.payload)

    def test_tampered_ciphertext_missing_duplicate_and_oversized_checksums_refuse(self):
        (self.root / 'pb_data.tar.age').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            retention.verify(self.root)
        (self.root / 'pb_data.tar.age').write_bytes(self.payload)
        for metadata in ['', ('encrypted_sha256=' + self.digest + '\n') * 2, 'x' * 65537]:
            (self.root / 'metadata').write_text(metadata)
            with self.subTest(size=len(metadata)), self.assertRaises(ValueError):
                retention.verify(self.root)

    def test_extra_plaintext_directories_and_links_refuse(self):
        extra = self.root / 'plaintext.sql'
        extra.write_text('must never be retained')
        with self.assertRaises(ValueError):
            retention.verify(self.root)
        extra.unlink()
        ciphertext = self.root / 'pb_data.tar.age'
        ciphertext.unlink()
        ciphertext.mkdir()
        with self.assertRaises(ValueError):
            retention.verify(self.root)
        ciphertext.rmdir()
        ciphertext.symlink_to(self.root / 'metadata')
        with self.assertRaises(OSError):
            retention.verify(self.root)


if __name__ == '__main__':
    unittest.main()
