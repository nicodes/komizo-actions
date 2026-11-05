import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('backup_commands', Path(__file__).resolve().parents[1] / 'scripts/backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class BackupCommands(unittest.TestCase):
    def test_timestamp_parity(self):
        for value in ['2026-09-04T12:00:33+00:00', '2026-09-04T12:00:33Z', '2026-09-04T12:00:33']:
            self.assertEqual(backup.stamp({'verified_at': value}), '20260904T120033Z')
        self.assertEqual(backup.stamp({}, datetime(2026, 1, 2, tzinfo=timezone.utc)), '20260102T000000Z')
        with self.assertRaisesRegex(ValueError, 'invalid receipt'):
            backup.stamp({'verified_at': 'invalid'})

    def test_exporter_cannot_execute_other_host_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'backup.json'
            for command in ['export-other-backup', 'export-demo-backup;id', '/bin/sh']:
                path.write_text(json.dumps({'exporter': command}))
                with self.assertRaises(ValueError):
                    backup.declaration(path, 'demo')
        with self.assertRaises(ValueError):
            backup.declaration(None, '../other')

    def test_failed_transport_cleans_only_its_own_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(backup.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'ssh')):
                with self.assertRaises(subprocess.CalledProcessError):
                    backup.export('demo', {}, directory, directory, {})
            self.assertFalse((directory / 'export.tar').exists())
            (directory / 'export.tar').write_bytes(b'retained')
            with self.assertRaises(FileExistsError):
                backup.export('demo', {}, directory, directory, {})
            self.assertEqual((directory / 'export.tar').read_bytes(), b'retained')

    def test_exact_absence_proof_and_private_frame(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frame = root / 'frame'
            frame.write_bytes(b'private authority')
            frame.chmod(0o600)
            expected = dict(version=1, project='demo', first_deployment=True, containers_absent=True)
            def transport(command, **kwargs):
                self.assertEqual(command[-1], '/usr/local/bin/export-backup-demo')
                self.assertEqual(kwargs['stdin'].read(), b'private authority')
                kwargs['stdout'].write(json.dumps(expected).encode())
            config = dict(proof_exporter='export-backup-demo', first_deployment=['containers_absent'])
            with patch.object(backup.subprocess, 'run', side_effect=transport):
                backup.export('demo', config, root, root, dict(KOMIZO_BACKUP_PROOF_FILE=str(frame)))
            self.assertEqual(json.loads((root / 'first-deployment.json').read_text()), expected)
            self.assertEqual((root / 'first-deployment.json').stat().st_mode & 0o777, 0o600)
            self.assertFalse((root / 'export.tar').exists())

    def test_tar_is_delegated_to_checked_ciphertext_receiver(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receiver = Mock()
            def transport(command, **kwargs):
                self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
                kwargs['stdout'].write(b'tar fixture')
            with patch.object(backup.subprocess, 'run', side_effect=transport), patch.object(backup, 'helper', return_value=receiver):
                backup.export('demo', {}, root, root, {})
            receiver.receive.assert_called_once_with(root / 'export.tar', root / 'export')

    def test_upload_uses_receipt_time_and_explicit_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'export').mkdir()
            (root / 'export/receipt.json').write_text(json.dumps(dict(verified_at='2026-09-04T12:00:33Z')))
            with patch.object(backup.subprocess, 'run') as run:
                backup.upload('demo', root, root)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('--slug') + 1], 'demo')
            self.assertEqual(command[command.index('--taken-at') + 1], '20260904T120033Z')


if __name__ == '__main__':
    unittest.main()
