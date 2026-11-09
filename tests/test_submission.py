import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import oci_registry as registry
import submission
from unittest.mock import patch
from test_release_packet import descriptor

ENV = {'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_REPOSITORY': 'example/mailroom',
       'GITHUB_REPOSITORY_ID': '123', 'GITHUB_SHA': 'a'*40, 'GITHUB_RUN_ID': '456',
       'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'push'}


def receipt(body, **fields):
    return {'kind': 'komizo.operation', 'version': 1, 'operation_id': submission.proposed_operation(body),
            **{key: body[key] for key in ['application', 'instance', 'release_digest']},
            'phase': 'ready', 'ok': True, 'reconciliation_required': False, **fields}


class FixedSubmission(unittest.TestCase):
    def request(self, **values):
        return submission.request('mailroom', 'production', 'deploy', ENV,
                                  descriptor=descriptor(), now=100, nonce='b'*32,
                                  token_request=lambda env, aud: 'header.payload.signature', **values)

    def test_audience_binds_exact_release_action_nonce_and_private_registry_authority(self):
        seen = []
        env = ENV | {'KOMIZO_REGISTRY_USER': 'example', 'KOMIZO_REGISTRY_TOKEN': 'private-token'}
        body = submission.request('mailroom', 'production', 'deploy', env, descriptor=descriptor(),
                                  now=100, nonce='b'*32, token_request=lambda env, aud: seen.append(aud) or 'a.b.c')
        proof = body.pop('proof')
        self.assertEqual(seen, ['komizo-request:sha256:' + hashlib.sha256(registry.canonical(body)).hexdigest()])
        self.assertEqual(body['authority'], 'github')
        self.assertEqual(body['registry']['token'], 'private-token')
        self.assertEqual(body['expires_at'], 220)
        self.assertEqual(proof, 'a.b.c')

    def test_source_app_authority_and_freshness_cannot_be_relabeled(self):
        for key, value in [('GITHUB_SHA', 'c'*40), ('GITHUB_REF', 'refs/pull/1/merge'),
                           ('GITHUB_REPOSITORY_ID', '321'), ('GITHUB_EVENT_NAME', 'pull_request')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                submission.request('mailroom', 'production', 'deploy', ENV | {key: value}, descriptor=descriptor())
        for values in [{'validity': 301}, {'release_digest': 'sha256:'+'c'*64}]:
            with self.assertRaises(ValueError): self.request(**values)

    def test_preview_preserves_selected_source_and_independent_publisher(self):
        release = descriptor(revision='c'*40); digest = registry.digest(registry.canonical(release))
        proof = {'kind': 'komizo.release-proof', 'version': 1, 'release_digest': digest,
                 'source_revision': 'c'*40, 'run_id': '456', 'run_attempt': '1',
                 'workflow_ref': 'example/mailroom/.github/workflows/pr-preview.yml@refs/heads/main',
                 'workflow_sha': 'a'*40, 'proof': 'a.b.c'}
        body = submission.request('mailroom', 'pr-1', 'deploy', ENV, descriptor=release,
                                  release_proof=proof, ttl_seconds=3600, token_request=lambda *_: 'a.b.c')
        self.assertEqual(body['release']['source']['revision'], 'c'*40)
        self.assertEqual(body['release_proof']['workflow_sha'], 'a'*40)
        self.assertEqual(body['ttl_seconds'], 3600)
        with self.assertRaises(ValueError):
            submission.request('mailroom', 'pr-1', 'deploy', ENV, descriptor=release, token_request=lambda *_: 'a.b.c')
        with self.assertRaises(ValueError): submission.producer_proof(proof | {'source_revision': 'a'*40}, digest, 'c'*40)

    def test_receipt_must_prove_exact_terminal_operation(self):
        body = self.request(); valid = receipt(body)
        self.assertEqual(submission.receipt(registry.canonical(valid), body), valid)
        for change in [{'application': 'another'}, {'release_digest': 'sha256:'+'c'*64},
                       {'phase': 'admitted'}, {'ok': False}, {'operation_id': ''},
                       {'phase': 'needs_reconciliation', 'ok': False, 'reconciliation_required': False},
                       {'debug': 'untyped output'}]:
            with self.subTest(change=change), self.assertRaises(ValueError): submission.receipt(registry.canonical(valid | change), body)
        with self.assertRaises(ValueError): submission.receipt(b'not a receipt', body)

    def test_inspection_preserves_distinct_stopped_installed_and_prepared_identities(self):
        body = {'action': 'inspect', 'release_digest': 'current', 'application': 'mailroom', 'instance': 'production'}
        installed = {'release_digest': 'sha256:'+'a'*64, 'desired_running': False, 'operation_id': 'b'*32}
        prepared = {'release_digest': 'sha256:'+'c'*64, 'operation_id': 'd'*32}
        inspection = {'kind': 'komizo.instance', 'version': 1, 'application': 'mailroom', 'instance': 'production',
                      'installed': installed, 'prepared': prepared}
        self.assertEqual(submission.receipt(registry.canonical(inspection), body), inspection)
        for candidate in [None, 'untyped', prepared | {'release_digest': 'current'}, prepared | {'operation_id': 'short'},
                          prepared | {'release_digest': True}, prepared | {'desired_running': True}]:
            with self.subTest(prepared=candidate), self.assertRaises(ValueError):
                submission.receipt(registry.canonical(inspection | {'prepared': candidate}), body)
        for candidate in [None, installed | {'desired_running': True}]:
            with self.subTest(installed=candidate), self.assertRaises(ValueError):
                submission.receipt(registry.canonical(inspection | {'installed': candidate}), body)

    def test_ssh_uses_fixed_command_stdin_private_config_and_never_retries(self):
        body = self.request(); calls = []
        def execute(arguments, **options):
            calls.append((arguments, options)); return registry.canonical(receipt(body))
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config'; config.write_text('Host deploy-target\n'); config.chmod(0o600)
            submission.submit(config, body, execute=execute)
            arguments, options = calls[0]
            self.assertEqual(arguments[-2:], ['deploy-target', 'komizo-box submit'])
            self.assertNotIn('a.b.c', ' '.join(arguments))
            self.assertEqual(json.loads(options['input']), body)
            self.assertEqual(options['timeout'], 1500)
            config.chmod(0o644)
            with self.assertRaises(ValueError): submission.submit(config, body, execute=execute)
        self.assertEqual(len(calls), 1)

    def test_disconnect_retains_proposed_identity_without_claiming_admission(self):
        body=self.request();proposed=submission.proposed_operation(body)
        self.assertEqual(proposed,'3a136f1b4cdd49cd0c83b4231129a21f')
        with tempfile.TemporaryDirectory() as temp:
            config=Path(temp)/'config';config.write_text('Host deploy-target\n');config.chmod(0o600)
            output=Path(temp)/'output'
            def uncertain(*args,**options): raise TimeoutError('no completion response')
            with patch.dict(os.environ,{'GITHUB_OUTPUT':str(output)}), self.assertRaisesRegex(ValueError,proposed+' is not confirmed admitted'):
                submission.submit(config,body,execute=uncertain)
            self.assertEqual(output.read_text(),'proposed-operation-id='+proposed+'\n')
        with self.assertRaises(ValueError):
            submission.receipt(registry.canonical(receipt(body,operation_id='c'*32)),body)

    def test_native_transport_bounds_stdout_stderr_and_failure(self):
        self.assertEqual(submission.transport([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'], input=b'bounded', timeout=5), b'bounded')
        for script in ['import sys; sys.stdout.buffer.write(b"a" * 5000000)',
                       'import sys; sys.stderr.buffer.write(b"a" * 70000)', 'raise SystemExit(1)']:
            with self.assertRaises(ValueError): submission.transport([sys.executable, '-c', script], input=b'{}', timeout=5)


if __name__ == '__main__': unittest.main()
