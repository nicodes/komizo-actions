from contextlib import chdir
import importlib.util
import json
from source_fixture import write_source
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
    def test_storage_credentials_refuse_before_host_contact_or_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);directory=root/'capture'
            for name in ['BACKUP_S3_ACCESS_KEY','BACKUP_S3_SECRET_KEY','BACKUP_S3_HOSTNAME','BACKUP_S3_BUCKET',
                         'AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY']:
                with self.subTest(name=name),patch.object(backup.transport,'capture') as capture:
                    with self.assertRaisesRegex(ValueError,name):
                        backup.export('demo',{},root,directory,{name:'fixture-value'})
                    capture.assert_not_called()
                with patch.dict(backup.os.environ,{name:'fixture-value'}),patch.object(backup.transport,'run') as run:
                    with self.assertRaisesRegex(ValueError,name):
                        backup.transport.capture(root,directory,'demo','export-demo-backup',lambda *args:None)
                    run.assert_not_called()
                self.assertFalse(directory.exists())

    def test_timestamp_parity(self):
        for value in ['2026-09-04T12:00:33+00:00', '2026-09-04T12:00:33Z', '2026-09-04T12:00:33']:
            self.assertEqual(backup.stamp({'verified_at': value}), '20260904T120033Z')
        self.assertEqual(backup.stamp({}, datetime(2026, 1, 2, tzinfo=timezone.utc)), '20260102T000000Z')
        with self.assertRaisesRegex(ValueError, 'invalid receipt'):
            backup.stamp({'verified_at': 'invalid'})

    def test_exporter_cannot_execute_other_host_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'backup.toml'
            for command in ['export-other-backup', 'export-demo-backup;id', '/bin/sh']:
                write_source(path, {'exporter': command})
                with self.assertRaises(ValueError):
                    backup.declaration(path, 'demo')
        with self.assertRaises(ValueError):
            backup.declaration(None, '../other')

    def test_failed_transport_cleans_only_owned_stage_and_preserves_prior_evidence(self):
        with tempfile.TemporaryDirectory() as temporary, chdir(temporary):
            root = Path(temporary); directory = root/'.artifacts/backup'
            with patch.object(backup.transport, 'run', side_effect=SystemExit(23)):
                with self.assertRaises(SystemExit) as failure:
                    backup.export('demo', {}, root, directory, {})
            self.assertEqual(failure.exception.code, 23)
            self.assertEqual(list(directory.iterdir()), [])
            (directory/'export.tar').write_bytes(b'retained')
            with patch.object(backup.transport, 'run') as run, self.assertRaisesRegex(RuntimeError, 'preserving existing'):
                backup.export('demo', {}, root, directory, {})
            run.assert_not_called()
            self.assertEqual((directory/'export.tar').read_bytes(), b'retained')

    def test_exact_absence_proof_and_private_frame_publish_atomically(self):
        with tempfile.TemporaryDirectory() as temporary, chdir(temporary):
            root = Path(temporary); directory = root/'.artifacts/backup'
            frame = root/'frame'; frame.write_bytes(b'private authority'); frame.chmod(0o600)
            output = root/'output'
            expected = dict(version=1, project='demo', first_deployment=True, containers_absent=True)
            def capture(command, **kwargs):
                self.assertEqual(command[-1], '/usr/local/bin/export-backup-demo')
                self.assertEqual(kwargs['stdin'].read(), b'private authority')
                kwargs['stdout'].write(json.dumps(expected).encode())
            config = dict(proof_exporter='export-backup-demo', first_deployment=['containers_absent'])
            with patch.object(backup.transport, 'run', side_effect=capture):
                backup.export('demo', config, root, directory,
                    dict(KOMIZO_BACKUP_PROOF_FILE=str(frame), GITHUB_OUTPUT=str(output)))
            self.assertEqual(json.loads((directory/'first-deployment.json').read_text()), expected)
            self.assertEqual((directory/'first-deployment.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual(output.read_text(), 'has-export=false\n')
            self.assertEqual([p.name for p in directory.iterdir()], ['first-deployment.json'])

    def test_tar_is_checked_before_atomic_publication_and_output(self):
        with tempfile.TemporaryDirectory() as temporary, chdir(temporary):
            root = Path(temporary); directory = root/'.artifacts/backup'; receiver = Mock()
            def capture(command, **kwargs):
                self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
                kwargs['stdout'].write(b'tar fixture')
            def receive(archive, target):
                self.assertEqual(archive.read_bytes(), b'tar fixture')
                self.assertFalse((directory/'export').exists())
                target.mkdir(mode=0o700)
                (target/'snapshot.cms').write_bytes(b'validated ciphertext')
            receiver.receive.side_effect = receive
            with patch.object(backup.transport, 'run', side_effect=capture), patch.object(backup, 'helper', return_value=receiver):
                backup.export('demo', {}, root, directory, {})
            receiver.receive.assert_called_once()
            self.assertEqual((directory/'export/snapshot.cms').read_bytes(), b'validated ciphertext')
            self.assertEqual([p.name for p in directory.iterdir()], ['export'])

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
