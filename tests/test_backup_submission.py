import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import backup_submission as backup
import oci_registry as registry
import submission
from test_submission import ENV


class CiphertextTransfer(unittest.TestCase):
    def setUp(self):
        self.payload = b'opaque encrypted data, never decrypted by CI'
        self.expected = {'name': 'snapshot.cms', 'size_bytes': len(self.payload), 'sha256': registry.digest(self.payload)}
        self.request = {'action': 'export', 'application': 'mailroom', 'instance': 'production',
                        'release_digest': 'sha256:' + 'a'*64, 'operation': 'b'*32, 'export_name': 'snapshot.cms'}
        self.header = {'kind': 'komizo.export', 'version': 1, 'operation_id': 'b'*32,
                       **{key: self.request[key] for key in ['application', 'instance', 'release_digest', 'export_name']},
                       'size_bytes': self.expected['size_bytes'], 'sha256': self.expected['sha256']}

    def source(self, raw):
        encoded = base64.b64encode(raw).decode()
        return [sys.executable, '-c', 'import base64,sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(base64.b64decode("' + encoded + '"))']

    def test_exact_framed_export_is_private_and_byte_identical(self):
        raw = registry.canonical(self.header) + b'\n' + self.payload
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'snapshot.cms'
            backup.download(None, self.request, self.expected, target, arguments=self.source(raw), timeout=5)
            self.assertEqual(target.read_bytes(), self.payload)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_truncation_trailing_data_wrong_identity_header_and_hash_refuse_without_retained_output(self):
        good = registry.canonical(self.header) + b'\n' + self.payload
        cases = [good[:-1], good + b'x', registry.canonical(self.header | {'operation_id': 'c'*32}) + b'\n' + self.payload,
                 registry.canonical(self.header | {'release_digest': 'sha256:'+'c'*64}) + b'\n' + self.payload,
                 good[:-1] + b'x', b'x'*17000, b'{}\n'+self.payload]
        for raw in cases:
            with self.subTest(length=len(raw)), tempfile.TemporaryDirectory() as temp:
                target = Path(temp) / 'snapshot.cms'
                with self.assertRaises(ValueError): backup.download(None, self.request, self.expected, target, arguments=self.source(raw), timeout=5)
                self.assertFalse(target.exists())

    def test_header_cannot_duplicate_fields_or_coerce_integer_types(self):
        for raw in [registry.canonical(self.header).replace(b'"version":1', b'"version":true'),
                    registry.canonical(self.header)[:-1] + b',"version":1}']:
            with self.assertRaises(ValueError): backup.export_header(raw, self.request, self.expected)

    def test_backup_receipt_requires_complete_unique_bound_encrypted_pair(self):
        body = {'action': 'backup', 'nonce': 'b'*32, 'application': 'mailroom', 'instance': 'production', 'release_digest': 'sha256:'+'a'*64}
        core = {'kind': 'komizo.operation', 'version': 1, 'operation_id': submission.proposed_operation(body),
                **{key: body[key] for key in ['application', 'instance', 'release_digest']},
                'phase': 'ready', 'ok': True, 'reconciliation_required': False}
        pair = [self.expected, {'name': 'receipt.json', 'size_bytes': 123, 'sha256': 'sha256:'+'c'*64}]
        valid = core | {'backup': {'status': 'ready', 'files': pair}}
        self.assertEqual(submission.receipt(registry.canonical(valid), body), valid)
        for bad in [{'status': 'absent'}, {'status': 'ready', 'files': pair[:1]},
                    {'status': 'ready', 'files': [self.expected, self.expected]},
                    {'status': 'ready', 'files': [self.expected | {'name': '../secret'}, pair[1]]}]:
            with self.assertRaises(ValueError): submission.receipt(registry.canonical(core | {'backup': bad}), body)
        absent_body = body | {'release_digest': 'absent'}
        absent = core | {'release_digest': 'absent', 'backup': {'status': 'absent'}}
        self.assertEqual(submission.receipt(registry.canonical(absent), absent_body), absent)


if __name__ == '__main__': unittest.main()
