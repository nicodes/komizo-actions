"""Public Godot verification that follows a Deploy run reads that run's retained identity and admits only its own."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deployed = load('deploy_run_under_test', 'godot-production/deploy_run.py')
public = load('public_under_test', 'godot-production/production.py')
REVISION, OTHER, RELEASE = 'a' * 40, 'c' * 40, 'b' * 64
REPOSITORY = 'owner/game'
CALLER = REPOSITORY + '/.github/workflows/verify-public-release.yml@refs/heads/main'


class Admission(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.event = self.directory / 'event.json'
        self.artifacts = [{'name': 'public-release-' + REVISION + '-1234-1', 'expired': False}]
        self.tip = REVISION
        self.runs = [dict(id=1234, run_attempt=1, head_sha=REVISION, head_branch='main', conclusion='success',
                          path='.github/workflows/deploy.yml', event='push')]
        self.job = dict(name='Deploy / Deploy', status='completed', conclusion='success', run_id=1234, run_attempt=1,
                        head_sha=REVISION)

    def environment(self, **changes):
        run = dict(id=1234, run_attempt=1, name='Deploy · main', path='.github/workflows/deploy.yml', head_branch='main',
                   head_sha=REVISION, event='push', status='completed', conclusion='success',
                   repository={'full_name': REPOSITORY}, head_repository={'full_name': REPOSITORY})
        self.event.write_text(json.dumps({'workflow_run': run | changes.pop('run', {})}))
        return {'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_SHA': REVISION, 'GITHUB_WORKFLOW_SHA': REVISION,
                'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_WORKFLOW_REF': CALLER,
                'GITHUB_EVENT_PATH': str(self.event)} | changes

    def lookup(self, path):
        if '/git/ref/heads/main' in path:
            return json.dumps({'object': {'sha': self.tip}})
        if path.endswith('/artifacts?per_page=100'):
            return json.dumps({'total_count': len(self.artifacts), 'artifacts': self.artifacts})
        if '/attempts/' in path and '/jobs' in path:
            return json.dumps({'total_count': 1, 'jobs': [self.job]})
        return json.dumps({'workflow_runs': self.runs})

    def admit(self, environment=None):
        return deployed.admit(environment or self.environment(), self.lookup)

    def test_the_successful_main_deploy_run_that_triggered_the_workflow_is_admitted_with_its_artifact(self):
        self.assertEqual(self.admit(), (1234, 1, 'public-release-' + REVISION + '-1234-1'))

    def test_a_deployment_that_retained_no_identity_has_nothing_to_verify(self):
        self.artifacts = []
        self.assertEqual(self.admit(), (1234, 1, ''))
        self.artifacts = [{'name': 'public-release-' + REVISION + '-1234-1', 'expired': True}]
        self.assertEqual(self.admit()[2], '')

    def test_other_runs_branches_outcomes_forks_and_superseded_revisions_are_refused(self):
        for changes, message in [
                (dict(run=dict(path='.github/workflows/build-and-test.yml')), 'successful main Deploy'),
                (dict(run=dict(name='Deploy', path='.github/workflows/other.yml')), 'successful main Deploy'),
                (dict(run=dict(head_branch='feature')), 'successful main Deploy'),
                (dict(run=dict(event='pull_request')), 'successful main Deploy'),
                (dict(run=dict(conclusion='failure')), 'successful main Deploy'),
                (dict(run=dict(status='in_progress')), 'successful main Deploy'),
                (dict(run=dict(head_repository={'full_name': 'fork/game'})), 'own Deploy run'),
                (dict(run=dict(repository={'full_name': 'fork/game'})), 'own Deploy run'),
                (dict(run=dict(head_sha=OTHER)), 'current main')]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                self.admit(self.environment(**changes))
        environment = self.environment()
        self.event.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'triggering Deploy run'):
            self.admit(environment)

    def test_only_the_generated_caller_on_trusted_main_at_the_current_tip_is_admitted(self):
        for changes in [dict(GITHUB_WORKFLOW_REF=REPOSITORY + '/.github/workflows/verify-production.yml@refs/heads/main'),
                        dict(GITHUB_WORKFLOW_REF=REPOSITORY + '/.github/workflows/deploy.yml@refs/heads/main'),
                        dict(GITHUB_WORKFLOW_REF=CALLER.replace('@refs/heads/main', '@refs/heads/feature')),
                        dict(GITHUB_WORKFLOW_REF='foreign/game/.github/workflows/verify-public-release.yml@refs/heads/main'),
                        dict(GITHUB_REF='refs/heads/feature'), dict(GITHUB_EVENT_NAME='push'),
                        dict(GITHUB_EVENT_NAME='pull_request'), dict(GITHUB_WORKFLOW_SHA=OTHER),
                        dict(GITHUB_SHA='short'), dict(GITHUB_REPOSITORY='not a repository')]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'exact trusted-main caller'):
                self.admit(self.environment(**changes))
        self.tip = OTHER
        with self.assertRaisesRegex(ValueError, 'current main'):
            self.admit()

    def test_the_run_must_hold_a_successful_deploy_job_of_that_attempt(self):
        for change in [dict(name='Deploy'), dict(conclusion='failure'), dict(run_attempt=2), dict(head_sha=OTHER)]:
            self.job = dict(name='Deploy / Deploy', status='completed', conclusion='success', run_id=1234, run_attempt=1,
                            head_sha=REVISION) | change
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'exact Deploy run'):
                self.admit()

    def test_a_manual_dispatch_verifies_the_latest_successful_deploy_of_the_current_revision(self):
        environment = self.environment(GITHUB_EVENT_NAME='workflow_dispatch')
        self.runs = [dict(self.runs[0], id=1000, run_attempt=1), dict(self.runs[0], id=1234, run_attempt=1)]
        self.assertEqual(self.admit(environment)[:2], (1234, 1))
        self.runs = []
        with self.assertRaisesRegex(ValueError, 'successful Deploy run of this exact revision'):
            self.admit(environment)
        self.runs = [dict(id=1234, run_attempt=1, head_sha=OTHER, head_branch='main', conclusion='success',
                          path='.github/workflows/deploy.yml', event='push')]
        with self.assertRaisesRegex(ValueError, 'successful Deploy run of this exact revision'):
            self.admit(environment)
        self.runs = [dict(self.runs[0], head_sha=REVISION)]
        self.artifacts = []
        with self.assertRaisesRegex(ValueError, 'retained no public payload identity'):
            self.admit(environment)

    def test_the_artifact_listing_must_be_complete_and_unambiguous(self):
        self.artifacts = [{'name': 'public-release-' + REVISION + '-1234-1', 'expired': False}] * 2
        with self.assertRaisesRegex(ValueError, 'more than once'):
            self.admit()
        self.lookup_total = None
        original = self.lookup
        self.lookup = lambda path: (json.dumps({'total_count': 5, 'artifacts': []}) if path.endswith('/artifacts?per_page=100')
                                    else original(path))
        with self.assertRaisesRegex(ValueError, 'complete bounded artifact'):
            self.admit()


class Reading(unittest.TestCase):
    def write(self, record, name='public-release.json'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        (directory / name).write_text(json.dumps(record))
        return directory

    def record(self, **changes):
        return {'kind': 'komizo.public-release', 'version': 1, 'source_commit': REVISION, 'release_id': RELEASE} | changes

    def test_the_identity_of_the_verified_revision_is_read(self):
        self.assertEqual(deployed.read(self.write(self.record()), REVISION), RELEASE)

    def test_another_source_a_malformed_identity_and_extra_files_are_refused(self):
        for changes in [dict(source_commit=OTHER), dict(release_id='short'), dict(kind='other'), dict(version=2), dict(extra=1)]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'another source or is malformed'):
                deployed.read(self.write(self.record(**changes)), REVISION)
        with self.assertRaisesRegex(ValueError, 'exactly its one file'):
            deployed.read(self.write(self.record(), name='other.json'), REVISION)
        directory = self.write(self.record())
        (directory / 'second.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'exactly its one file'):
            deployed.read(directory, REVISION)
        with self.assertRaisesRegex(ValueError, 'exactly its one file'):
            deployed.read(directory / 'missing', REVISION)


class Selection(unittest.TestCase):
    """The reviewed production assertion accepts a workflow_run only from the separate caller."""

    def setUp(self):
        self.config = {'recipe': 'godot-game', 'node': {'packages': [{'directory': 'tests/browser', 'manager': 'npm'}]}}
        self.entry = {'kind': 'production', 'runner': 'ubuntu-22.04', 'timeout_minutes': 15, 'setup': 'browser',
                      'packages': ['tests/browser'], 'browsers': ['chromium'],
                      'entry': {'runner': 'node', 'path': 'tests/browser/public.mjs', 'timeout': 900}}
        self.plan = {'production': {'include': [self.entry]}}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.event = Path(temporary.name) / 'event.json'
        run = dict(id=1234, run_attempt=1, path='.github/workflows/deploy.yml', head_branch='main', head_sha=REVISION,
                   event='push', status='completed', conclusion='success', repository={'full_name': REPOSITORY},
                   head_repository={'full_name': REPOSITORY})
        self.event.write_text(json.dumps({'workflow_run': run}))
        self.env = {'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': REVISION, 'GITHUB_EVENT_NAME': 'workflow_run',
                    'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_WORKFLOW_SHA': REVISION, 'GITHUB_WORKFLOW_REF': CALLER,
                    'GITHUB_EVENT_PATH': str(self.event)}

    def select(self, **changes):
        return public.select(self.config, self.plan, REVISION, RELEASE, self.env | changes, 'ubuntu-22.04', 15)

    def test_the_separate_caller_after_a_successful_main_deploy_is_accepted(self):
        self.assertEqual(self.select(), self.entry)

    def test_any_other_workflow_or_run_is_refused(self):
        for changes in [dict(GITHUB_WORKFLOW_REF=REPOSITORY + '/.github/workflows/deploy.yml@refs/heads/main'),
                        dict(GITHUB_WORKFLOW_REF=REPOSITORY + '/.github/workflows/verify-production.yml@refs/heads/main'),
                        dict(GITHUB_WORKFLOW_SHA=OTHER), dict(GITHUB_REPOSITORY='')]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'only the separate public verification caller'):
                self.select(**changes)
        good = json.loads(self.event.read_text())['workflow_run']
        for change, message in [(dict(conclusion='failure'), 'successful main Deploy'), (dict(head_branch='feature'), 'successful main Deploy'),
                                (dict(head_sha=OTHER), 'current main'), (dict(path='.github/workflows/other.yml'), 'successful main Deploy'),
                                (dict(head_repository={'full_name': 'fork/game'}), 'own Deploy run')]:
            self.event.write_text(json.dumps({'workflow_run': good | change}))
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                self.select()

    def test_the_legacy_same_run_events_are_unchanged(self):
        for event in ['push', 'workflow_dispatch']:
            self.assertEqual(self.select(GITHUB_EVENT_NAME=event), self.entry)
        with self.assertRaisesRegex(ValueError, 'exact trusted main source'):
            self.select(GITHUB_EVENT_NAME='schedule')
        with self.assertRaisesRegex(ValueError, 'exact trusted main source'):
            self.select(GITHUB_REF='refs/pull/1/merge')


class Workflows(unittest.TestCase):
    def load(self, name):
        return yaml.safe_load((ROOT / '.github/workflows' / name).read_text())

    def test_the_deployed_verification_workflow_has_the_after_deploy_contract(self):
        workflow = self.load('verify-deployed-godot-production.yml')
        inputs = workflow['on']['workflow_call']['inputs']
        self.assertEqual(set(inputs), {'cookbook-ref', 'komizo-ref', 'source', 'runner', 'timeout-minutes'})
        self.assertNotIn('release-id', inputs)
        self.assertEqual(workflow['permissions'], {})
        job = workflow['jobs']['verify']
        self.assertEqual(job['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertIn("github.ref == 'refs/heads/main'", job['if'])
        self.assertIn("github.event_name == 'workflow_run'", job['if'])
        self.assertNotIn("'push'", job['if'])
        names = [step['name'] for step in job['steps']]
        order = ['Require exact immutable verification source and accepted automation', 'Check out source',
                 'Check out deployment actions', 'Admit the successful Deploy run this verification follows',
                 'Download the accepted public payload identity of that Deploy run', 'Read the accepted public payload identity',
                 'Verify production']
        positions = [names.index(name) for name in order]
        self.assertEqual(positions, sorted(positions))
        by_name = {step['name']: step for step in job['steps']}
        download = by_name['Download the accepted public payload identity of that Deploy run']
        self.assertEqual(download['with']['run-id'], '${{ steps.deployed.outputs.run-id }}')
        self.assertEqual(download['with']['name'], '${{ steps.deployed.outputs.artifact }}')
        self.assertEqual(download['with']['repository'], '${{ github.repository }}')
        self.assertEqual(download['if'], "steps.deployed.outputs.artifact != ''")
        verify = by_name['Verify production']
        self.assertEqual(verify['with']['release-id'], '${{ steps.public.outputs.release-id }}')
        self.assertNotIn('expected-source', verify['with'])
        self.assertEqual(verify['if'], "steps.public.outputs.release-id != ''")
        for step in job['steps']:
            uses = step.get('uses', '')
            if uses and not uses.startswith('./'):
                self.assertRegex(uses, r'@[a-f0-9]{40}$')

    def test_the_legacy_same_run_workflow_is_unchanged(self):
        workflow = self.load('verify-godot-production.yml')
        self.assertEqual(workflow['jobs']['verify']['permissions'], {'contents': 'read'})
        self.assertIn('release-id', workflow['on']['workflow_call']['inputs'])
        self.assertNotIn("workflow_run", workflow['jobs']['verify']['if'])

    def test_delivery_retains_the_identity_the_following_workflow_downloads(self):
        deliver = self.load('deliver.yml')
        steps = deliver['jobs']['deliver']['steps']
        retained = next(step for step in steps if step['name'].startswith('Retain the accepted public payload identity'))
        deployed_workflow = self.load('verify-deployed-godot-production.yml')
        download = next(step for step in deployed_workflow['jobs']['verify']['steps'] if step['name'].startswith('Download'))
        # Name pattern: public-release-<sha>-<run id>-<attempt>, as deploy_run.artifact_name builds it.
        self.assertEqual(retained['with']['name'], 'public-release-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}')
        self.assertEqual(deployed.artifact_name(REVISION, 7, 2), 'public-release-' + REVISION + '-7-2')
        self.assertTrue(download['with']['name'].endswith('outputs.artifact }}'))


if __name__ == '__main__':
    unittest.main()
