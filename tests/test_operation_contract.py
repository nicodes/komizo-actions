"""Native request/receipt phases distinguish mutation completion from observation."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import oci_registry
import submission
from test_submission import ENV, receipt


# Successful phases emitted by the native lifecycle. In-progress states are
# observable only through selected-operation inspection, never completion.
NATIVE_SUCCESS = {'deploy': {'ready', 'prepared_stopped'}, 'start': {'ready'},
                  'restart': {'ready'}, 'stop': {'prepared_stopped'},
                  'remove': {'prepared_stopped'}, 'backup': {'ready'}}
NATIVE_PENDING = ['admitted', 'staging', 'jobs', 'installing', 'activating', 'readiness']


class NativeOperationContract(unittest.TestCase):
    def body(self, action):
        return {'action': action, 'application': 'mailroom', 'instance': 'pr-42' if action == 'remove' else 'production',
                'release_digest': 'sha256:' + 'a' * 64, 'nonce': 'b' * 32}

    def test_each_mutation_requires_its_native_success_phase(self):
        for action, accepted in NATIVE_SUCCESS.items():
            body = self.body(action)
            for phase in ['ready', 'prepared_stopped']:
                value = receipt(body, phase=phase)
                if action == 'backup':
                    value['backup'] = self.backup_inventory(body['release_digest'])
                with self.subTest(action=action, phase=phase):
                    if phase in accepted:
                        self.assertEqual(submission.receipt(oci_registry.canonical(value), body), value)
                    else:
                        with self.assertRaises(ValueError): submission.receipt(oci_registry.canonical(value), body)
            for phase in ['failed', 'needs_reconciliation']:
                value = receipt(body, phase=phase, ok=False, reconciliation_required=phase == 'needs_reconciliation')
                self.assertFalse(submission.receipt(oci_registry.canonical(value), body)['ok'])
            for phase in NATIVE_PENDING:
                with self.assertRaises(ValueError):
                    submission.receipt(oci_registry.canonical(receipt(body, phase=phase, ok=False)), body)

    def test_operation_inspection_observes_pending_without_claiming_completion(self):
        body = self.body('inspect') | {'operation': 'c' * 32}
        for phase in [*NATIVE_PENDING, 'ready', 'prepared_stopped', 'failed', 'needs_reconciliation']:
            value = receipt(body, operation_id=body['operation'], phase=phase,
                ok=phase in {'ready', 'prepared_stopped'}, reconciliation_required=phase == 'needs_reconciliation')
            self.assertEqual(submission.receipt(oci_registry.canonical(value), body), value)
            if phase in NATIVE_PENDING: self.assertFalse(value['ok'])
            for invalid in [value | {'ok': not value['ok']},
                            value | {'reconciliation_required': not value['reconciliation_required']},
                            value | {'operation_id': 'd' * 32}, value | {'instance': 'foreign'},
                            value | {'release_digest': 'sha256:' + 'd' * 64}, value | {'phase': 'unknown'}]:
                with self.subTest(phase=phase), self.assertRaises(ValueError):
                    submission.receipt(oci_registry.canonical(invalid), body)

    def test_instance_inspection_accepts_current_or_its_exact_installed_target(self):
        body = self.body('inspect')
        value = {'kind': 'komizo.instance', 'version': 1, 'application': body['application'], 'instance': body['instance'],
            'installed': {'release_digest': body['release_digest'], 'desired_running': False, 'operation_id': 'c' * 32},
            'prepared': {'release_digest': 'sha256:' + 'd' * 64, 'operation_id': 'e' * 32}}
        for target in [body['release_digest'], 'current']:
            self.assertEqual(submission.receipt(oci_registry.canonical(value), body | {'release_digest': target}), value)
        for change in [{'installed': None}, {'instance': 'pr-43'}, {'application': 'foreign'},
                       {'installed': value['installed'] | {'release_digest': 'sha256:' + 'f' * 64}}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                submission.receipt(oci_registry.canonical(value | change), body)

    def test_pending_inspection_cli_retains_the_observed_phase_without_success(self):
        body = self.body('inspect') | {'operation': 'c' * 32, 'timeout_seconds': 5}
        value = receipt(body, operation_id=body['operation'], phase='jobs', ok=False)
        real_submit = submission.submit
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            root = Path(directory)
            config = root / 'ssh-config'; config.write_text('Host deploy-target\n'); config.chmod(0o600)
            output = io.StringIO()
            with patch.dict(os.environ, {'KOMIZO_SSH_CONFIG': str(config), 'GITHUB_OUTPUT': str(root / 'outputs')}), \
                    patch.object(sys, 'argv', ['submission.py', '--application', 'mailroom', '--action', 'inspect',
                        '--release-digest', body['release_digest'], '--operation', body['operation'], '--timeout', '5']), \
                    patch.object(submission, 'request', return_value=body), \
                    patch.object(submission, 'submit', side_effect=lambda config, body, **kwargs:
                        real_submit(config, body, **kwargs, execute=lambda *args, **options: oci_registry.canonical(value))), \
                    contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exited:
                submission.main()
            self.assertEqual(exited.exception.code, 1)
            self.assertEqual(json.loads(output.getvalue()), value)
            self.assertEqual(json.loads((root / '.artifacts/komizo-operation.json').read_bytes()), value)
            self.assertIn('phase=jobs\n', (root / 'outputs').read_text())

    def backup_inventory(self, digest):
        return {'status': 'ready', 'release_digest': digest, 'files': [
            {'name': 'snapshot.cms', 'size_bytes': 123, 'sha256': 'sha256:' + 'c' * 64},
            {'name': 'receipt.json', 'size_bytes': 456, 'sha256': 'sha256:' + 'd' * 64}]}

    def test_backup_operation_inspection_retains_its_exact_validated_export_inventory(self):
        body = self.body('inspect') | {'operation': 'c' * 32}
        value = receipt(body, operation_id=body['operation'], backup=self.backup_inventory(body['release_digest']))
        self.assertEqual(submission.receipt(oci_registry.canonical(value), body), value)
        for case in ['release', 'duplicate', 'path', 'size', 'hash', 'extra']:
            invalid = copy.deepcopy(value)
            pair = invalid['backup']['files']
            if case == 'release': invalid['backup']['release_digest'] = 'sha256:' + 'f' * 64
            elif case == 'duplicate': pair[1] = copy.deepcopy(pair[0])
            elif case == 'path': pair[0]['name'] = '../snapshot.cms'
            elif case == 'size': pair[0]['size_bytes'] = True
            elif case == 'hash': pair[0]['sha256'] = 'unknown'
            else: invalid['backup']['extra'] = 'unknown'
            with self.subTest(case=case), self.assertRaises(ValueError):
                submission.receipt(oci_registry.canonical(invalid), body)

    def test_prepared_start_captures_its_previous_release_before_target_activation(self):
        body = self.body('start')
        previous = 'sha256:' + 'b' * 64
        for phase in ['ready', 'failed', 'needs_reconciliation']:
            value = receipt(body, phase=phase, ok=phase == 'ready', reconciliation_required=phase == 'needs_reconciliation',
                previous_release_digest=previous, backup=self.backup_inventory(previous))
            self.assertEqual(submission.receipt(oci_registry.canonical(value), body), value)
            for invalid in [value | {'previous_release_digest': None},
                            value | {'previous_release_digest': 'sha256:' + 'f' * 64},
                            value | {'backup': self.backup_inventory(body['release_digest'])},
                            value | {'backup': self.backup_inventory('sha256:' + 'f' * 64)},
                            value | {'backup': {'status': 'absent'}}]:
                with self.subTest(phase=phase), self.assertRaises(ValueError):
                    submission.receipt(oci_registry.canonical(invalid), body)
            missing = value.copy(); missing.pop('previous_release_digest')
            with self.assertRaises(ValueError): submission.receipt(oci_registry.canonical(missing), body)

    def test_inspected_prepared_start_backup_is_bound_to_previous_in_all_durable_phases(self):
        body = self.body('inspect') | {'operation': 'c' * 32}
        previous = 'sha256:' + 'b' * 64
        for phase in [*NATIVE_PENDING, 'ready', 'prepared_stopped', 'failed', 'needs_reconciliation']:
            value = receipt(body, operation_id=body['operation'], phase=phase,
                ok=phase in {'ready', 'prepared_stopped'}, reconciliation_required=phase == 'needs_reconciliation',
                previous_release_digest=previous, backup=self.backup_inventory(previous))
            self.assertEqual(submission.receipt(oci_registry.canonical(value), body), value)
            for captured in [body['release_digest'], 'sha256:' + 'f' * 64]:
                invalid = value | {'backup': self.backup_inventory(captured)}
                with self.subTest(phase=phase), self.assertRaises(ValueError):
                    submission.receipt(oci_registry.canonical(invalid), body)

    def test_backup_capture_does_not_extend_to_other_actions_or_foreign_installed_targets(self):
        previous = 'sha256:' + 'b' * 64
        for action in ['deploy', 'restart', 'stop', 'remove']:
            body = self.body(action)
            phase = 'prepared_stopped' if action in {'stop', 'remove'} else 'ready'
            invalid = receipt(body, phase=phase, previous_release_digest=previous, backup=self.backup_inventory(previous))
            with self.subTest(action=action), self.assertRaises(ValueError):
                submission.receipt(oci_registry.canonical(invalid), body)
        body = self.body('backup')
        invalid = receipt(body, previous_release_digest=previous, backup=self.backup_inventory(previous))
        with self.assertRaises(ValueError): submission.receipt(oci_registry.canonical(invalid), body)

    def test_absent_backup_is_not_an_inspectable_release_target_or_a_replayed_mutation(self):
        issued = []
        for digest in ['absent', 'current']:
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                submission.request('mailroom', 'production', 'inspect', ENV, release_digest=digest,
                    operation='c' * 32, token_request=lambda *args: issued.append(args))
        self.assertEqual(issued, [])
        body = self.body('inspect') | {'release_digest': 'absent', 'operation': 'c' * 32}
        value = receipt(body, operation_id=body['operation'], backup={'status': 'absent'})
        with self.assertRaises(ValueError): submission.receipt(oci_registry.canonical(value), body)

    def test_owner_only_actions_do_not_acquire_ci_request_authority(self):
        issued = []
        for action in ['data-prepare', 'data-accept', 'secret-set', 'recover', 'expire']:
            with self.subTest(action=action), self.assertRaises(ValueError):
                submission.request('mailroom', 'production', action, ENV, release_digest='sha256:' + 'a' * 64,
                    token_request=lambda *args: issued.append(args))
        self.assertEqual(issued, [])


if __name__ == '__main__': unittest.main()
