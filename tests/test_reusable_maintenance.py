"""Storage credentials stay in upload; routine backup never receives a recovery key."""
from pathlib import Path
import unittest
import yaml
ROOT = Path(__file__).resolve().parents[1]


class NativeMaintenance(unittest.TestCase):
    def test_backup_uses_current_pinned_transport_without_private_frames_or_shell_exporters(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/cookbook-backup.yml').read_text())
        job = workflow['jobs']['backup']
        self.assertEqual(job['permissions']['id-token'], 'write')
        source = str(job)
        self.assertNotIn('backup-proof', source)
        self.assertNotIn('operation.frame', source)
        self.assertNotIn('BACKUP_RECOVERY_KEY', source)
        connect = [step for step in job['steps'] if step.get('uses', '').endswith('/connect')]
        self.assertEqual(len(connect), 1); self.assertNotIn('user', connect[0].get('with', {}))
        for step in job['steps']:
            if any(key.startswith('BACKUP_S3_') for key in step.get('env', {})):
                self.assertIn('backup.py upload', step.get('run', ''))
                self.assertNotIn('backup.py export', step.get('run', ''))

    def test_delivery_retains_runner_storage_scope_and_immutable_ciphertext_artifact(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/cookbook-deliver.yml').read_text())
        job = workflow['jobs']['deliver']
        for step in job['steps']:
            values = step.get('env', {})
            if any(key.startswith('BACKUP_S3_') for key in values):
                self.assertTrue(step.get('uses', '').endswith('/application-backup-upload'))
                self.assertEqual(values['BACKUP_ALLOWED_SLUGS'], '${{ steps.plan.outputs.allowed-products }}')
        retained = [step for step in job['steps'] if step.get('uses', '').startswith('actions/upload-artifact@')]
        self.assertEqual(len(retained), 2)
        activation=next(step for step in retained if step['with']['name'].startswith('activation-'))
        delivery=next(step for step in retained if step['with']['name'].startswith('delivery-'))
        self.assertEqual(activation['with']['path'],'.artifacts/komizo-operation.json')
        self.assertIn('.artifacts/backup/',delivery['with']['path'])
        for step in retained:
            self.assertEqual(step['if'], 'always()')
            self.assertIn('github.run_attempt', step['with']['name'])
            self.assertEqual(step['timeout-minutes'],'${{ fromJSON(inputs.step-timeouts).artifact }}')


if __name__ == '__main__': unittest.main()
