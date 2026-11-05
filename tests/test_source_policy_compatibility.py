"""The optional action delegates to the same prepared Tools source checker."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SourcePolicyCompatibility(unittest.TestCase):
    def test_thin_action_reuses_prepared_sdk_and_ignores_caller_python_poison(self):
        with tempfile.TemporaryDirectory(prefix='source policy caller ') as directory:
            scratch = Path(directory)
            (scratch / 'yaml.py').write_text('raise RuntimeError("caller module must not load")\n')
            compose = scratch / 'compose.yml'
            compose.write_text('services:\n  api:\n    env_file: [secrets.env]\n')
            workflow = scratch / 'delivery.yml'
            workflow.write_text('jobs:\n  deploy:\n    env:\n      KOMIZO_SECRET_KEY: ${{ secrets.KEY }}\n')
            environment = dict(os.environ, SOURCE_POLICY_SDK_ROOT=str(ROOT), COMPOSE=str(compose),
                               WORKFLOWS=str(workflow), SCOPED_ENV_DIR='secrets/current', HOST_ONLY='',
                               SECRETS_ENV_SERVICES='', PYTHONPATH=str(scratch))
            command = ['bash', str(ROOT / 'check-secrets/run.sh')]
            accepted = subprocess.run(command, cwd=scratch, env=environment, capture_output=True, timeout=30)
            self.assertEqual(accepted.returncode, 0, accepted.stderr.decode())
            compose.write_text('services:\n  api:\n    env_file: [secrets.env]\n  gate:\n    env_file: [secrets.env]\n')
            refused = subprocess.run(command, cwd=scratch, env=environment, capture_output=True, timeout=30)
            self.assertEqual(refused.returncode, 1)
            self.assertIn(b'secrets.env', refused.stderr)
            self.assertFalse((scratch / '.artifacts').exists(), 'source checks must not install dependencies')


if __name__ == '__main__':
    unittest.main()
