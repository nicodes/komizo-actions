import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('application_publication', Path(__file__).resolve().parents[1] / 'scripts/application-publication.py')
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class ApplicationPublication(unittest.TestCase):
    def setUp(self):
        self.environment = dict(GITHUB_SERVER_URL='https://github.com', GITHUB_REPOSITORY='owner/demo',
                                GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2', GITHUB_SHA='a' * 40,
                                GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/ci.yml@refs/heads/main',
                                GITHUB_WORKFLOW_SHA='a' * 40, GITHUB_EVENT_NAME='workflow_dispatch')
        self.proof = dict(version=1, source_commit='a' * 40, source_tree='b' * 40, cookbook_commit='c' * 40,
                          recipe='react-native', status='passed', tracked_source_clean=True, build_verified=True,
                          tests_verified=True, test_groups=['lint', 'unit', 'security', 'e2e'],
                          execution_context=self.environment | {'GITHUB_JOB': 'validate'})
        self.config = dict(recipe='react-native', ci={'runner': 'ubuntu-24.04'})
        self.jobs = dict(total_count=1, jobs=[dict(name='validate / Full source checks / Make full',
                           conclusion='success', head_sha='a' * 40, labels=['ubuntu-24.04'])])

    def accept(self):
        publication.accept(self.proof, self.config, 'a' * 40, 'c' * 40, 'b' * 40,
                           ['lint', 'unit', 'security', 'e2e'], self.jobs, self.environment)

    def test_current_complete_same_run_host_is_admitted(self):
        self.accept()

    def test_preview_acceptance_cannot_publish_as_production_even_at_the_same_source_revision(self):
        self.environment.update(GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/cd.yml@refs/heads/main',
            GITHUB_EVENT_NAME='push')
        self.proof['execution_context'].update(
            GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/pr-preview.yml@refs/heads/main',
            GITHUB_EVENT_NAME='issue_comment')
        with self.assertRaisesRegex(ValueError,'another workflow execution'):
            self.accept()

    def test_old_attempt_partial_or_failed_host_is_refused(self):
        for key, value in [('source_commit', 'd' * 40), ('source_tree', 'd' * 40),
                           ('cookbook_commit', 'd' * 40), ('test_groups', ['lint', 'unit']),
                           ('build_verified', False), ('tracked_source_clean', False)]:
            original = self.proof[key]
            self.proof[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.accept()
            self.proof[key] = original
        self.proof['execution_context']['GITHUB_RUN_ATTEMPT'] = '1'
        with self.assertRaisesRegex(ValueError, 'another workflow'):
            self.accept()
        self.proof['execution_context']['GITHUB_RUN_ATTEMPT'] = '2'
        self.jobs['jobs'][0]['conclusion'] = 'failure'
        with self.assertRaisesRegex(ValueError, 'succeeded'):
            self.accept()

    def test_wrong_runner_or_ambiguous_jobs_are_refused(self):
        self.jobs['jobs'][0]['labels'] = ['other-runner']
        with self.assertRaisesRegex(ValueError, 'another declared runner'):
            self.accept()
        self.jobs['jobs'][0]['labels'] = ['ubuntu-24.04']
        self.jobs['total_count'] = 2
        with self.assertRaisesRegex(ValueError, 'complete current-attempt'):
            self.accept()

    def test_only_closed_hash_bound_native_packet_is_staged_without_execution(self):
        from test_release_packet import make_packet
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); incoming = root / 'incoming/.artifacts/release'; incoming.mkdir(parents=True)
            make_packet(incoming)
            proof = {'artifacts': {'.artifacts/release/' + name: hashlib.sha256((incoming / name).read_bytes()).hexdigest()
                                   for name in ['release.json', 'inventory.json', 'static/web.tar.gz']}}
            publication.stage(root / 'incoming', proof, root / 'accepted')
            self.assertEqual((root / 'accepted/release.json').read_bytes(), (incoming / 'release.json').read_bytes())
            (incoming / 'static/web.tar.gz').write_bytes(b'changed')
            with self.assertRaises(ValueError): publication.stage(root / 'incoming', proof, root / 'refused')
            self.assertFalse((root / 'refused').exists())

    def test_unaccepted_extra_metadata_or_programs_are_refused_before_staging(self):
        from test_release_packet import make_packet
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); incoming = root / 'incoming/.artifacts/release'; incoming.mkdir(parents=True)
            make_packet(incoming)
            proof = {'artifacts': {'.artifacts/release/' + name: hashlib.sha256((incoming / name).read_bytes()).hexdigest()
                                   for name in ['release.json', 'inventory.json', 'static/web.tar.gz']}}
            (incoming / 'untrusted.py').write_text('raise SystemExit("must never execute")')
            with self.assertRaises(ValueError): publication.stage(root / 'incoming', proof, root / 'refused')
            self.assertFalse((root / 'refused').exists())


if __name__ == '__main__': unittest.main()
