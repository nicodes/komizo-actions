"""Native backup declaration, runner-only storage authority and receipt timestamps."""
from contextlib import chdir
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('native_backup', Path(__file__).resolve().parents[1] / 'scripts/backup.py')
backup = importlib.util.module_from_spec(spec); spec.loader.exec_module(backup)


class NativeBackupCommands(unittest.TestCase):
    def test_source_declaration_cannot_select_programs_or_weaken_host_absence_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'backup.toml'; source.write_text('version=1\n')
            self.assertEqual(backup.declaration(source, 'mailroom'), {'version': 1})
            for text in ['version=true\n', 'version=2\n', 'version=1\nexporter="arbitrary"\n', 'version=1\nfirst_deployment=[]\n']:
                source.write_text(text)
                with self.assertRaises(ValueError): backup.declaration(source, 'mailroom')

    def test_storage_authority_refuses_before_host_or_local_stage_effects(self):
        with patch.object(backup.transport, 'capture') as capture, self.assertRaises(ValueError):
            backup.export('mailroom', {'version': 1}, Path('.'), Path('.artifacts/backup'), {'BACKUP_S3_SECRET_KEY': 'fixture'})
        capture.assert_not_called()

    def test_typed_capture_receives_no_s3_credentials_and_keeps_absence_receipt(self):
        with tempfile.TemporaryDirectory() as temp, chdir(temp):
            root = Path(temp); output = root / 'outputs'
            def produce(project, stage, environment, **options):
                self.assertEqual(project, 'mailroom'); self.assertNotIn('BACKUP_S3_SECRET_KEY', environment)
                path = stage / 'first-deployment.json'; path.write_text('{"backup":{"status":"absent"}}'); path.chmod(0o600)
                return 'first-deployment.json'
            import sys
            sys.path.insert(0, str(Path(backup.__file__).parent))
            import backup_submission
            with patch.object(backup_submission, 'capture', produce):
                backup.export('mailroom', {'version': 1}, root, root / '.artifacts/backup', {'GITHUB_OUTPUT': str(output)})
            self.assertEqual(output.read_text(), 'has-export=false\ncapture-status=absent\n')
            self.assertEqual((root / '.artifacts/backup/first-deployment.json').stat().st_mode & 0o777, 0o600)

    def test_upload_uses_exact_project_and_authenticated_receipt_time(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); export = root / 'export'; export.mkdir()
            (export / 'receipt.json').write_text('{"verified_at":"2026-11-08T10:11:12Z"}')
            with patch.object(backup.subprocess, 'run') as run:
                backup.upload('mailroom', Path('/verified/tools'), root)
                args = run.call_args.args[0]
                self.assertEqual(args[args.index('--slug') + 1], 'mailroom')
                self.assertEqual(args[args.index('--taken-at') + 1], '20261108T101112Z')
            (export / 'receipt.json').write_text('{"verified_at":"not a timestamp"}')
            with self.assertRaises(ValueError): backup.upload('mailroom', Path('/verified/tools'), root)


if __name__ == '__main__': unittest.main()
