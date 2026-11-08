"""Real Git main/tag races and complete trusted CI fence exact source releases."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('native_action_release', Path(__file__).resolve().parents[1] / 'scripts/release.py')
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)


class ExactSourceRelease(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name); self.remote = root / 'remote.git'; self.work = root / 'source'
        r.git('init', '--bare', str(self.remote)); r.git('init', '-b', 'main', str(self.work))
        self.cwd = contextlib.chdir(self.work); self.cwd.__enter__(); self.addCleanup(self.cwd.__exit__, None, None, None)
        r.git('config', 'user.name', 'Fixture'); r.git('config', 'user.email', 'fixture@example.invalid')
        r.git('remote', 'add', 'origin', str(self.remote))
        Path('README.md').write_text('exact accepted source\n'); Path('.gitignore').write_text('.artifacts/\n')
        r.git('add', '.'); r.git('commit', '-qm', 'source'); r.git('push', 'origin', 'main')
        self.source = r.git('rev-parse', 'HEAD'); self.version = 'v1.2.3'
        self.record = None; self.creations = 0; self.attested = True
        self.ci = patch.object(r, 'trusted_ci'); self.ci.start(); self.addCleanup(self.ci.stop)
        self.api_patch = patch.object(r, 'api', self.api); self.api_patch.start(); self.addCleanup(self.api_patch.stop)
        original = r.run
        def execute(*arguments, **options):
            if arguments[:3] == ('gh', 'attestation', 'verify'):
                if not self.attested: raise RuntimeError('wrong producer attestation')
                self.assertIn('--source-digest', arguments)
                return subprocess.CompletedProcess(arguments, 0, '', '')
            if arguments[:3] == ('gh', 'release', 'create'):
                self.creations += 1
                self.assertTrue(r.remote_tag(self.version, self.source))
                assets = [Path(argument) for argument in arguments[4:6]]
                self.record = {'tag_name': self.version, 'draft': False, 'prerelease': False,
                               'assets': [{'name': path.name, 'digest': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()} for path in assets]}
                return subprocess.CompletedProcess(arguments, 0, '', '')
            return original(*arguments, **options)
        self.runner = patch.object(r, 'run', execute); self.runner.start(); self.addCleanup(self.runner.stop)

    def api(self, path, optional=False):
        self.assertEqual(path, 'releases/tags/' + self.version); return copy.deepcopy(self.record)

    def packed(self):
        with contextlib.redirect_stdout(io.StringIO()): return r.pack(self.version, self.source)

    def publish(self, path):
        with contextlib.redirect_stdout(io.StringIO()): r.publish(self.version, self.source, path)

    def test_exact_source_artifact_publishes_once_and_completed_replay_preserves_tag_and_assets(self):
        path = self.packed(); raw = path.read_bytes(); self.publish(path)
        tag = r.git('rev-parse', 'refs/tags/' + self.version); self.publish(path)
        self.assertEqual(self.creations, 1); self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(r.git('rev-parse', self.version + '^{}'), self.source)
        self.assertEqual(r.git('rev-parse', 'refs/tags/' + self.version), tag)

    def test_current_main_advance_refuses_before_tag_or_release(self):
        path = self.packed(); Path('README.md').write_text('newer source\n'); r.git('commit', '-qam', 'advance'); r.git('push', 'origin', 'main')
        r.git('checkout', '--detach', self.source)
        with self.assertRaisesRegex(RuntimeError, 'current main'): self.publish(path)
        self.assertEqual(r.git('ls-remote', '--tags', 'origin'), ''); self.assertEqual(self.creations, 0)

    def test_changed_archive_or_record_and_wrong_attestation_refuse_without_public_effects(self):
        path = self.packed(); original = path.read_bytes(); path.write_bytes(original + b'changed')
        with self.assertRaises(RuntimeError): self.publish(path)
        path.write_bytes(original); self.attested = False
        with self.assertRaises(RuntimeError): self.publish(path)
        self.assertEqual(r.git('ls-remote', '--tags', 'origin'), ''); self.assertEqual(self.creations, 0)

    def test_lightweight_or_foreign_annotated_tag_is_never_replaced(self):
        path = self.packed(); r.git('tag', self.version, self.source); r.git('push', 'origin', 'refs/tags/' + self.version)
        original = r.git('ls-remote', '--tags', 'origin')
        with self.assertRaises(RuntimeError): self.publish(path)
        self.assertEqual(r.git('ls-remote', '--tags', 'origin'), original); self.assertEqual(self.creations, 0)

    def test_existing_release_metadata_or_asset_digest_drift_is_refused_without_overwrite(self):
        path = self.packed(); self.publish(path)
        for changed in [{'draft': True}, {'prerelease': True}, {'assets': []}]:
            saved = copy.deepcopy(self.record); self.record.update(changed)
            with self.assertRaises(RuntimeError): self.publish(path)
            self.record = saved
        self.record['assets'][0]['digest'] = 'sha256:' + '0'*64
        with self.assertRaises(RuntimeError): self.publish(path)
        self.assertEqual(self.creations, 1)


class CompleteTrustedCI(unittest.TestCase):
    def setUp(self):
        self.source = 'a'*40
        self.fixtures = {
            'actions/workflows/' + str(r.WORKFLOW_ID): {'id': r.WORKFLOW_ID, 'path': '.github/workflows/ci.yml'},
            f'actions/workflows/{r.WORKFLOW_ID}/runs': {'workflow_runs': [{'id': 10}]},
            'actions/runs/10': {'workflow_id': r.WORKFLOW_ID, 'path': '.github/workflows/ci.yml',
                'repository': {'full_name': r.REPO}, 'head_repository': {'full_name': r.REPO},
                'event': 'push', 'head_branch': 'main', 'head_sha': self.source, 'status': 'completed',
                'conclusion': 'success', 'check_suite_id': 20, 'run_attempt': 2},
            'check-suites/20': {'app': {'id': r.APP_ID}, 'head_sha': self.source, 'head_branch': 'main', 'status': 'completed', 'conclusion': 'success'},
            'actions/runs/10/attempts/2/jobs': {'jobs': [{'name': 'Test', 'status': 'completed', 'conclusion': 'success',
                'head_sha': self.source, 'run_id': 10, 'check_run_url': 'https://api.github.com/check/30'}]},
            'check-suites/20/check-runs': {'check_runs': [{'name': 'Test', 'status': 'completed', 'conclusion': 'success',
                'head_sha': self.source, 'url': 'https://api.github.com/check/30', 'app': {'id': r.APP_ID}, 'check_suite': {'id': 20}}]},
        }

    def verify(self):
        with patch.object(r, 'api', lambda path, **_: self.fixtures[path.split('?')[0]]): r.trusted_ci(self.source)

    def test_exact_main_push_linked_test_is_required(self):
        self.verify()
        for field, changed in [('event', 'pull_request'), ('head_sha', 'b'*40), ('head_branch', 'feature'),
                               ('status', 'in_progress'), ('conclusion', 'failure'), ('repository', {'full_name': 'foreign/repo'})]:
            saved = copy.deepcopy(self.fixtures); self.fixtures['actions/runs/10'][field] = changed
            with self.subTest(field=field), self.assertRaises(RuntimeError): self.verify()
            self.fixtures = saved
        for collection, path in [('jobs', 'actions/runs/10/attempts/2/jobs'), ('check_runs', 'check-suites/20/check-runs')]:
            saved = copy.deepcopy(self.fixtures); self.fixtures[path][collection][0]['conclusion'] = 'failure'
            with self.assertRaises(RuntimeError): self.verify()
            self.fixtures = saved
        self.fixtures['check-suites/20/check-runs']['check_runs'][0]['url'] = 'unlinked'
        with self.assertRaises(RuntimeError): self.verify()


if __name__ == '__main__': unittest.main()
