import copy
from contextlib import chdir, nullcontext
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import native_recipe_fixture

workflows = native_recipe_fixture.Module('operational_workflows')

spec = importlib.util.spec_from_file_location('operational_control', Path(__file__).parents[1] / 'scripts/operational.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class OperationalAuthority(unittest.TestCase):
    def workflow_files(self, root, pin, config):
        files = workflows.workflow_files(root, pin, config)
        fixture = files.pop('.github/workflows/maintenance.yml')
        files['.github/workflows/security.yml'] = fixture
        return files

    def git(self, root, *arguments):
        return subprocess.check_output(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
            '-c', 'commit.gpgsign=false', '-C', str(root), *arguments], text=True,
            stderr=subprocess.DEVNULL).strip()

    def repository(self, name):
        root = self.directory / name
        root.mkdir()
        self.git(root, 'init', '--initial-branch=main')
        self.git(root, 'remote', 'add', 'origin', 'https://github.com/nicodes/' + name + '.git')
        (root / 'README').write_text(name + '\n')
        (root / '.gitignore').write_text('ignored\n__pycache__/\n')
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-qm', 'Fixture source')
        return root, self.git(root, 'rev-parse', 'HEAD')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.tools, tools_sha = self.repository('tools')
        self.actions, actions_sha = self.repository('komizo-actions')
        self.root, _ = self.repository('product')
        self.operations = SimpleNamespace(declaration=workflows.declaration, workflow_files=self.workflow_files,
                                          step_timeouts=workflows.step_timeouts)
        # Git-authority controls use inert workflow fixtures. Their source
        # declarations still pass through the actual current native reader.
        decoder = patch.object(driver.recipe_data, 'load', side_effect=lambda root, tools, configuration=None: native_recipe_fixture.load(root))
        decoder.start()
        self.addCleanup(decoder.stop)
        (self.root / 'deploy').mkdir()
        (self.root / 'api').mkdir()
        (self.root / 'api/Cargo.toml').write_text('[package]\nname="fixture"\n')
        (self.root / 'deploy/health.py').write_text('assert 2 + 2 == 4\n')
        self.config = {'recipe': 'react-native', 'version': 1,
                       'delivery': {'definition': 'deploy/delivery.toml'},
                       'operations': {'definition': 'deploy/operations.toml'}}
        delivery = dict(version=1, project='demo', komizo_ref=actions_sha,
                        publish_runner='ubuntu-24.04', publish_timeout=35,
                        deploy_runner='ubuntu-24.04', deploy_timeout=100,
                        health_urls=['https://example.test/readyz'], notification_url='https://example.test/')
        native_recipe_fixture.write(self.root / 'deploy/delivery.toml', delivery)
        self.data = dict(version=1, project='demo',
            security=dict(runner='ubuntu-24.04', timeout_minutes=110, report_timeout=5,
                          deployed_policy='deploy/deployed-scan.json'),
            production=dict(runner='ubuntu-24.04', timeout_minutes=45, environment='Production',
                            checks=[dict(runner='python', path='deploy/health.py')]),
            paid=[dict(id='model_smoke', description='Run the existing live assertion', runner='ubuntu-24.04',
                       timeout_minutes=20, environment='Production', deployment_required=True,
                       check=dict(runner='cargo-run', path='api/Cargo.toml', binary='model-smoke',
                                  args=['--live'], timeout=1200))])
        (self.root / 'deploy/scan.json').write_text(json.dumps(dict(repository='owner/demo',
            image_base='ghcr.io/owner/demo',components=['gate'],environment='production')))
        (self.root / 'deploy/deployed-scan.json').write_text(json.dumps(dict(version=1,
            deployment='deploy/scan.json',rust={},go={'gate':dict(forbidden=[])})))
        native_recipe_fixture.write(self.root / 'deploy/operations.toml', self.data)
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[delivery]\ndefinition="deploy/delivery.toml"\n[operations]\ndefinition="deploy/operations.toml"\n')
        for name, text in self.workflow_files(self.root, tools_sha, self.config).items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.deploy = 'name: Deploy\n'
        (self.root / '.github/workflows/deploy.yml').write_text(self.deploy)
        self.git(self.root, 'add', '.')
        self.git(self.root, 'commit', '-qm', 'Native operational policy')
        self.revision = self.git(self.root, 'rev-parse', 'HEAD')
        self.event = self.directory / 'event.json'
        self.event.write_text(json.dumps({'inputs': {'mode': 'verify', 'model_smoke': False}}))
        self.environment = dict(os.environ, GITHUB_REPOSITORY='owner/demo', GITHUB_SHA=self.revision,
            GITHUB_WORKFLOW_SHA=self.revision, GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch',
            GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/security.yml@refs/heads/main',
            GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1', GITHUB_EVENT_PATH=str(self.event),
            TOOLS_REF=tools_sha, KOMIZO_REF=actions_sha, DECLARATION='deploy/operations.toml',
            OPERATION_RUNNER='ubuntu-24.04', OPERATION_TIMEOUT='110', OPERATION_ENVIRONMENT='',
            OPERATION_STEP_TIMEOUTS=json.dumps({key:value for key,value in workflows.step_timeouts('security',self.data['security']).items() if not key.startswith('report')}))

    def lookup(self, path):
        if '/compare/' in path:
            return json.dumps({'status': 'identical'})
        if '/git/ref/heads/main' in path:
            return json.dumps({'object': {'sha': self.revision}})
        if '/attempts/1/jobs' in path:
            return json.dumps({'total_count': 1, 'jobs': [dict(name='Deploy / Deploy', status='completed', conclusion='success',
                run_id=1234, run_attempt=1, head_sha=self.revision)]})
        return self.listing([dict(id=1234, head_sha=self.revision, head_branch='main', conclusion='success',
            path='.github/workflows/deploy.yml', event='workflow_dispatch', run_attempt=1)])

    @staticmethod
    def listing(runs):
        return json.dumps({'total_count': len(runs), 'workflow_runs': runs})

    def admit(self, environment=None, operation='security', selected='', lookup=None, sleep=lambda _: None):
        def module(name):
            if name == 'operational_workflows':
                return self.operations
            if name == 'generate':
                return SimpleNamespace(workflow_files=lambda *_: {'.github/workflows/deploy.yml': self.deploy,
                                                                      '.github/workflows/verify-production.yml': self.deploy})
            return SimpleNamespace()
        context = SimpleNamespace(tools_revision=self.environment['TOOLS_REF'], module=module)
        with patch.object(driver, 'ROOT', self.actions), patch.object(driver, 'tools_modules', return_value=(self.operations, None, None)), \
                patch.object(driver.recipe_context, 'accepted_context', return_value=context):
            return driver.authority(self.root, self.tools, operation, selected,
                                    environment or self.environment, lookup or self.lookup, sleep=sleep)

    def paid_environment(self):
        return self.environment | dict(GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/deploy.yml@refs/heads/main',
            COOKBOOK_REF='a' * 40,
            OPERATION_TIMEOUT='20', OPERATION_ENVIRONMENT='Production',
            OPERATION_STEP_TIMEOUTS=json.dumps(workflows.step_timeouts('paid',self.data['paid'][0])))

    def test_actual_clean_git_sources_and_canonical_caller_are_admitted(self):
        _, data, record = self.admit()
        self.assertEqual(data['project'], 'demo')
        self.assertEqual(record['report_timeout'], 5)

    def test_retired_operational_caller_paths_refuse_before_any_effect(self):
        for operation, filename in [('security', 'security-scan.yml'), ('security', 'audit-security.yml'), ('security', 'maintenance.yml'),
                                    ('recovery', 'recovery.yml'), ('backup', 'verify-production.yml'),
                                    ('security', 'verify-production.yml'), ('production', 'security.yml'),
                                    ('paid', 'backup.yml')]:
            environment = self.environment | {
                'GITHUB_WORKFLOW_REF': 'owner/demo/.github/workflows/' + filename + '@refs/heads/main'}
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, 'exact trusted-main workflow'):
                self.admit(environment, operation=operation)

    def test_production_and_paid_accept_deploy_or_verify_production_callers(self):
        self.assertEqual({operation: set(driver.WORKFLOWS[operation]) for operation in ['production', 'paid']},
                         {operation: {'deploy.yml', 'verify-production.yml'} for operation in ['production', 'paid']})
        self.event.write_text(json.dumps({'inputs': {'mode': 'verify', 'model_smoke': True}}))
        for filename in ['deploy.yml', 'verify-production.yml']:
            (self.root / '.github/workflows' / filename).write_text(self.deploy)
            self.git(self.root, 'add', '.')
            self.git(self.root, 'commit', '-qm', 'Caller ' + filename, '--allow-empty')
            self.revision = self.git(self.root, 'rev-parse', 'HEAD')
            environment = self.paid_environment() | dict(GITHUB_SHA=self.revision, GITHUB_WORKFLOW_SHA=self.revision,
                GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/' + filename + '@refs/heads/main')
            with self.subTest(filename=filename):
                self.admit(environment, 'paid', 'model_smoke')

    def workflow_run_environment(self, **changes):
        """The separate verify-production.yml caller triggered by a completed Deploy run on main."""
        (self.root / '.github/workflows/verify-production.yml').write_text(self.deploy)
        self.git(self.root, 'add', '.')
        self.git(self.root, 'commit', '-qm', 'Verification caller', '--allow-empty')
        self.revision = self.git(self.root, 'rev-parse', 'HEAD')
        run = dict(id=1234, run_attempt=1, name='Deploy · main', display_title='Deploy · main', path='.github/workflows/deploy.yml', head_branch='main',
                   head_sha=self.revision, event='push', status='completed', conclusion='success',
                   repository={'full_name': 'owner/demo'}, head_repository={'full_name': 'owner/demo'})
        self.event.write_text(json.dumps({'workflow_run': run | changes}))
        return self.environment | dict(GITHUB_SHA=self.revision, GITHUB_WORKFLOW_SHA=self.revision,
            GITHUB_EVENT_NAME='workflow_run', COOKBOOK_REF='a' * 40,
            GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/verify-production.yml@refs/heads/main',
            OPERATION_TIMEOUT='45', OPERATION_ENVIRONMENT='Production',
            OPERATION_STEP_TIMEOUTS=json.dumps(workflows.step_timeouts('production', self.data['production'])))

    def test_workflow_run_verifies_the_deploy_run_that_triggered_it(self):
        _, _, record = self.admit(self.workflow_run_environment(), 'production')
        self.assertEqual(record['environment'], 'Production')
        # A manual dispatch of the separate workflow has no mode input; Deploy's own dispatch still needs mode=verify.
        environment = self.workflow_run_environment() | dict(GITHUB_EVENT_NAME='workflow_dispatch')
        self.event.write_text(json.dumps({'inputs': {}}))
        self.admit(environment, 'production')
        self.event.write_text(json.dumps({'inputs': {'mode': 'deploy'}}))
        with self.assertRaisesRegex(ValueError, 'verify mode'):
            self.admit(environment, 'production')
        self.event.write_text(json.dumps({'inputs': {}}))
        with self.assertRaisesRegex(ValueError, 'verify mode'):
            self.admit(environment | dict(GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/deploy.yml@refs/heads/main'), 'production')

    def test_manual_dispatch_without_inputs_sends_null_and_is_admitted_for_the_verification_caller(self):
        environment = self.workflow_run_environment() | dict(GITHUB_EVENT_NAME='workflow_dispatch')
        realistic = {'inputs': None, 'ref': 'refs/heads/main', 'repository': {'full_name': 'owner/demo'},
                     'workflow': '.github/workflows/verify-production.yml'}
        self.event.write_text(json.dumps(realistic))
        self.admit(environment, 'production')
        for refused in [[], 'verify', 7]:
            self.event.write_text(json.dumps(realistic | {'inputs': refused}))
            with self.assertRaisesRegex(ValueError, 'dispatch metadata'):
                self.admit(environment, 'production')
        # Deploy's own dispatch still needs mode=verify, with or without inputs.
        self.event.write_text(json.dumps(realistic))
        with self.assertRaisesRegex(ValueError, 'verify mode'):
            self.admit(environment | dict(GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/deploy.yml@refs/heads/main'), 'production')

    def test_workflow_run_refuses_other_workflows_branches_failures_forks_and_superseded_revisions(self):
        for changes, message in [
                (dict(name='Build and test', path='.github/workflows/build-and-test.yml'), 'successful main Deploy'),
                (dict(path='.github/workflows/build-and-test.yml'), 'successful main Deploy'),
                (dict(head_branch='feature'), 'successful main Deploy'),
                (dict(event='pull_request'), 'successful main Deploy'),
                (dict(event='schedule'), 'successful main Deploy'),
                (dict(conclusion='failure'), 'successful main Deploy'),
                (dict(conclusion='cancelled'), 'successful main Deploy'),
                (dict(status='in_progress'), 'successful main Deploy'),
                (dict(run_attempt=0), 'successful main Deploy'),
                (dict(head_repository={'full_name': 'fork/demo'}), 'own Deploy run'),
                (dict(repository={'full_name': 'fork/demo'}), 'own Deploy run'),
                (dict(head_repository=None), 'own Deploy run'),
                (dict(head_sha='f' * 40), 'current main')]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                self.admit(self.workflow_run_environment(**changes), 'production')
        environment = self.workflow_run_environment()
        self.event.write_text(json.dumps({}))
        with self.assertRaisesRegex(ValueError, 'triggering Deploy run'):
            self.admit(environment, 'production')

    def test_workflow_run_identity_is_the_workflow_path_not_the_run_name(self):
        for name in ['Deploy', 'Deploy · main', 'Deploy · abc123', 'Anything']:
            with self.subTest(name=name):
                self.admit(self.workflow_run_environment(name=name), 'production')

    def test_workflow_run_requires_the_triggering_run_to_hold_a_successful_deploy_job(self):
        for changes in [dict(id=999), dict(run_attempt=2)]:
            environment = self.workflow_run_environment(**changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'exact Deploy run'):
                self.admit(environment, 'production')
        environment = self.workflow_run_environment()
        def lookup(path):
            value = json.loads(self.lookup(path))
            if 'jobs' in value:
                value['jobs'][0]['name'] = 'Deploy'
            return json.dumps(value)
        with self.assertRaisesRegex(ValueError, 'exact Deploy run'):
            self.admit(environment, 'production', lookup=lookup)

    def lagging_lookup(self, appears_on, run=None, other_runs=()):
        """A run listing that omits the triggering run until its `appears_on`th read."""
        reads = []
        def lookup(path):
            if '/workflows/deploy.yml/runs' in path:
                reads.append(path)
                value = json.loads(self.lookup(path))
                if len(reads) < appears_on:
                    value['workflow_runs'] = []
                return self.listing([*other_runs, *value['workflow_runs']])
            if path.endswith('/runs/1234/attempts/1'):
                return json.dumps(run or dict(id=1234, run_attempt=1, status='in_progress', conclusion=None))
            return self.lookup(path)
        lookup.reads = reads
        return lookup

    def test_the_deploy_listing_is_by_head_sha_and_never_filtered_by_status(self):
        lookup = self.lagging_lookup(1)
        self.admit(self.workflow_run_environment(), 'production', lookup=lookup)
        self.assertEqual(lookup.reads, [f'repos/owner/demo/actions/workflows/deploy.yml/runs?head_sha={self.revision}&per_page=100'])
        self.assertTrue(all('status=' not in read for read in lookup.reads))

    def test_unsuccessful_runs_of_the_same_revision_in_the_listing_are_refused(self):
        environment = self.workflow_run_environment()
        base = dict(id=1234, head_sha=self.revision, head_branch='main', conclusion='success',
                    path='.github/workflows/deploy.yml', event='workflow_dispatch', run_attempt=1)
        for change in [dict(status='in_progress', conclusion=None), dict(conclusion='failure'), dict(conclusion='cancelled'),
                       dict(head_branch='feature'), dict(event='pull_request'), dict(path='.github/workflows/other.yml'),
                       dict(head_sha='f' * 40), dict(run_attempt=2)]:
            def lookup(path, change=change):
                if '/workflows/deploy.yml/runs' in path:
                    return self.listing([base | change])
                if path.endswith('/runs/1234/attempts/1'):
                    return json.dumps(dict(id=1234, run_attempt=1, status='in_progress', conclusion=None))
                return self.lookup(path)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.admit(environment, 'production', lookup=lookup, sleep=lambda _: None)

    def test_a_rerun_deploy_run_is_only_accepted_at_the_attempt_the_listing_reports(self):
        # The listing reports a run's latest attempt: a triggering attempt 1 is refused once attempt 2 exists.
        environment = self.workflow_run_environment()
        def lookup(path):
            if '/workflows/deploy.yml/runs' in path:
                return self.listing([dict(id=1234, head_sha=self.revision, head_branch='main', conclusion='success',
                    path='.github/workflows/deploy.yml', event='workflow_dispatch', run_attempt=2)])
            if '/attempts/2/jobs' in path:
                return json.dumps({'total_count': 1, 'jobs': [dict(name='Deploy / Deploy', status='completed', conclusion='success',
                    run_id=1234, run_attempt=2, head_sha=self.revision)]})
            if path.endswith('/runs/1234/attempts/1'):
                return json.dumps(dict(id=1234, run_attempt=1, status='completed', conclusion='success'))
            return self.lookup(path)
        with self.assertRaisesRegex(ValueError, 'exact Deploy run'):
            self.admit(environment, 'production', lookup=lookup, sleep=lambda _: None)
        self.admit(self.workflow_run_environment(run_attempt=2), 'production', lookup=lookup)

    def test_an_incomplete_deploy_listing_is_refused(self):
        def lookup(path):
            if '/workflows/deploy.yml/runs' in path:
                return json.dumps({'total_count': 101, 'workflow_runs': json.loads(self.lookup(path))['workflow_runs']})
            return self.lookup(path)
        with self.assertRaisesRegex(ValueError, 'complete Deploy run records'):
            self.admit(self.workflow_run_environment(), 'production', lookup=lookup, sleep=lambda _: None)

    def test_a_listing_that_lags_the_deploy_run_passes_on_a_later_read(self):
        sleeps = []
        lookup = self.lagging_lookup(2)
        self.admit(self.workflow_run_environment(), 'production', lookup=lookup, sleep=sleeps.append)
        self.assertEqual((len(lookup.reads), sleeps), (2, [10]))

    def test_a_deploy_run_that_never_appears_is_refused_after_bounded_waits(self):
        sleeps = []
        lookup = self.lagging_lookup(10 ** 6)
        with self.assertRaisesRegex(ValueError, 'exact Deploy run; it was not reported successful after waiting 60 seconds'):
            self.admit(self.workflow_run_environment(), 'production', lookup=lookup, sleep=sleeps.append)
        self.assertEqual((len(lookup.reads), sleeps), (7, [10] * 6))

    def test_a_deploy_run_reported_failed_is_refused_without_waiting(self):
        sleeps = []
        failed = dict(id=1234, run_attempt=1, status='completed', conclusion='failure')
        lookup = self.lagging_lookup(10 ** 6, run=failed)
        with self.assertRaisesRegex(ValueError, 'exact Deploy run$'):
            self.admit(self.workflow_run_environment(), 'production', lookup=lookup, sleep=sleeps.append)
        self.assertEqual((len(lookup.reads), sleeps), (1, []))

    def test_unrelated_deploy_runs_for_the_same_revision_never_satisfy_the_wait(self):
        sleeps = []
        other = dict(id=77, head_sha=self.revision, head_branch='main', conclusion='success',
                     path='.github/workflows/deploy.yml', event='push', run_attempt=1)
        lookup = self.lagging_lookup(10 ** 6, other_runs=[other, dict(other, id=1234, run_attempt=2)])
        with self.assertRaisesRegex(ValueError, 'waiting 60 seconds'):
            self.admit(self.workflow_run_environment(), 'production', lookup=lookup, sleep=sleeps.append)
        self.assertEqual(len(sleeps), 6)

    def test_workflow_run_is_only_the_ordinary_production_check_from_the_separate_caller(self):
        environment = self.workflow_run_environment()
        with self.assertRaisesRegex(ValueError, 'manual dispatch'):
            self.admit(environment | dict(OPERATION_TIMEOUT='20', OPERATION_ENVIRONMENT='Production',
                OPERATION_STEP_TIMEOUTS=json.dumps(workflows.step_timeouts('paid', self.data['paid'][0]))), 'paid', 'model_smoke')
        with self.assertRaisesRegex(ValueError, 'exact trusted-main workflow'):
            self.admit(environment | dict(GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/deploy.yml@refs/heads/main'), 'backup')
        with self.assertRaisesRegex(ValueError, 'separate production verification caller'):
            self.admit(environment | dict(GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/deploy.yml@refs/heads/main'), 'production')
        with self.assertRaisesRegex(ValueError, 'exact trusted-main workflow'):
            self.admit(environment | dict(GITHUB_REF='refs/heads/feature'), 'production')

    def test_source_workflow_runner_budget_and_policy_identity_refusals(self):
        for changes in [dict(GITHUB_REF='refs/heads/feature'), dict(GITHUB_SHA='f' * 40),
                        dict(GITHUB_WORKFLOW_SHA='f' * 40), dict(GITHUB_EVENT_NAME='pull_request'),
                        dict(GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/build-and-test.yml@refs/heads/main'),
                        dict(GITHUB_RUN_ATTEMPT='0'), dict(OPERATION_RUNNER='ubuntu-22.04'),
                        dict(OPERATION_TIMEOUT='111'), dict(OPERATION_STEP_TIMEOUTS='{}'),
                        dict(OPERATION_ENVIRONMENT='Production'), dict(DECLARATION='deploy/other.json'),
                        dict(TOOLS_REF='main')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.admit(self.environment | changes)

    def test_modified_untracked_ignored_and_other_remote_sdk_sources_are_rejected(self):
        for path in [self.tools / 'README', self.tools / 'untracked.py', self.tools / 'ignored']:
            original = path.read_bytes() if path.exists() else None
            path.write_text('Unexpected source\n')
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                self.admit()
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)
        self.git(self.tools, 'remote', 'set-url', 'origin', 'https://github.com/other/tools.git')
        with self.assertRaisesRegex(ValueError, 'source authority'):
            self.admit()

    def test_nonaccepted_sdk_and_changed_canonical_main_caller_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'accepted on main'):
            self.admit(lookup=lambda path: json.dumps({'status': 'diverged'}))
        path = self.root / '.github/workflows/security.yml'
        path.write_text(path.read_text() + '# Changed source caller\n')
        self.git(self.root, 'add', '.')
        self.git(self.root, 'commit', '-qm', 'Change caller without its policy')
        self.revision = self.git(self.root, 'rev-parse', 'HEAD')
        environment = self.environment | dict(GITHUB_SHA=self.revision, GITHUB_WORKFLOW_SHA=self.revision)
        with self.assertRaisesRegex(ValueError, 'canonical authority'):
            self.admit(environment)

    def test_paid_decision_requires_manual_true_and_exact_successful_cd(self):
        environment = self.paid_environment()
        with self.assertRaisesRegex(ValueError, 'explicitly requested'):
            self.admit(environment, 'paid', 'model_smoke')
        self.event.write_text(json.dumps({'inputs': {'mode': 'deploy', 'model_smoke': True}}))
        with self.assertRaisesRegex(ValueError, 'explicit Deploy verify mode'):
            self.admit(environment, 'paid', 'model_smoke')
        self.event.write_text(json.dumps({'inputs': {'mode': 'verify', 'model_smoke': True}}))
        self.admit(environment, 'paid', 'model_smoke')
        with self.assertRaisesRegex(ValueError, 'manual dispatch'):
            self.admit(environment | dict(GITHUB_EVENT_NAME='schedule'), 'paid', 'model_smoke')
        for changes in [dict(head_sha='f' * 40), dict(conclusion='failure'), dict(path='.github/workflows/build-and-test.yml'),
                        dict(path='.github/workflows/cd.yml'),
                        dict(event='pull_request'), dict(run_attempt=0)]:
            def lookup(path):
                value = json.loads(self.lookup(path))
                if 'workflow_runs' in value:
                    value['workflow_runs'][0].update(changes)
                return json.dumps(value)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'successful deployment'):
                self.admit(environment, 'paid', 'model_smoke', lookup)
        for changes in [dict(conclusion='skipped'), dict(run_attempt=2), dict(head_sha='f' * 40)]:
            def lookup(path):
                value = json.loads(self.lookup(path))
                if 'jobs' in value:
                    value['jobs'][0].update(changes)
                return json.dumps(value)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'successful deployment'):
                self.admit(environment, 'paid', 'model_smoke', lookup)

    def test_oversized_or_linked_event_metadata_and_old_main_refuse_paid_effects(self):
        environment = self.paid_environment()
        self.event.write_text(' ' * 1048577)
        with self.assertRaisesRegex(ValueError, 'bounded'):
            self.admit(environment, 'paid', 'model_smoke')
        self.event.unlink()
        target = self.directory / 'actual-event.json'
        target.write_text(json.dumps({'inputs': {'mode': 'verify', 'model_smoke': True}}))
        self.event.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'bounded'):
            self.admit(environment, 'paid', 'model_smoke')
        self.event.unlink()
        self.event.write_text(target.read_text())
        def old_main(path):
            return json.dumps({'object': {'sha': 'f' * 40}}) if '/git/ref/heads/main' in path else self.lookup(path)
        with self.assertRaisesRegex(ValueError, 'current main'):
            self.admit(environment, 'paid', 'model_smoke', old_main)

    def test_retired_retention_role_has_no_operational_authority(self):
        with self.assertRaisesRegex(ValueError, 'exact trusted-main workflow source'):
            self.admit(operation='retention')

    def test_prepare_and_execution_derive_the_same_pinned_environment(self):
        original = self.environment | dict(TOOLS_ROOT='wrong', EXPECTED_REVISION='wrong', MISE_AUTO_INSTALL='true')
        first = driver.runtime_environment(self.root, self.tools, original)
        second = driver.runtime_environment(self.root, self.tools, dict(original))
        self.assertEqual(first, second)
        self.assertEqual(first['TOOLS_ROOT'], str(self.tools))
        self.assertEqual(first['EXPECTED_REVISION'], self.revision)
        self.assertEqual(first['MISE_AUTO_INSTALL'], 'false')
        calls = []
        driver.native(self.root, self.data['paid'][0]['check'], second,
                      lambda argv, root, env, timeout: calls.append((argv, env, timeout)))
        self.assertIn('--locked', calls[0][0])
        self.assertIn('--offline', calls[0][0])
        self.assertEqual(calls[0][1]['EXPECTED_REVISION'], self.revision)
        self.assertEqual(calls[0][2], 1200)

    def test_native_node_test_mode_keeps_its_exact_runner_and_deadline(self):
        calls=[]
        driver.native(self.root,dict(runner='node-test',path='tests/automation/policy.mjs',timeout=120),
            self.environment,lambda argv,root,env,timeout:calls.append((argv,root,timeout)))
        self.assertEqual(calls,[(['node','--test','tests/automation/policy.mjs'],self.root,120)])

    def test_every_leaf_prepares_its_parser_before_admission_and_keeps_credentials_late(self):
        root = Path(__file__).parents[1] / '.github/workflows'
        for name in ['backup', 'audit-security', 'verify-production', 'verify-public-production']:
            text = (root / (name + '.yml')).read_text()
            with self.subTest(workflow=name):
                self.assertLess(text.index('Prepare accepted recipe SDKs'), text.index('Admit exact trusted main'))
                self.assertNotIn('install_args: python', text)
                self.assertIn("PYTHONDONTWRITEBYTECODE: '1'", text)
        production = (root / 'verify-production.yml').read_text()
        preparation = production.split('    - name: Run owned production assertions')[0]
        self.assertNotIn('secrets.OPENROUTER_API_KEY', preparation)

    def test_inline_paid_steps_keep_credentials_late_inside_the_same_job(self):
        text = (Path(__file__).parents[1] / '.github/workflows/verify-production.yml').read_text()
        ordinary, inline = text.split('    - name: Prepare the original inline paid assertion', 1)
        prepare, run = inline.split('    - name: Run the original inline paid assertion', 1)
        self.assertNotIn('secrets.OPENROUTER_API_KEY', prepare)
        self.assertIn("inputs.operation == 'paid' && secrets.OPENROUTER_API_KEY || ''", ordinary)
        self.assertIn("inputs.operation == 'production' && inputs.inline-paid", prepare)
        self.assertIn("inputs.operation == 'production' && inputs.inline-paid", run)
        self.assertIn('OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}', run)
        self.assertIn('OPERATION: paid', run)
        self.assertIn('fromJSON(inputs.inline-step-timeouts).run', run)
        self.assertEqual(text.count('    runs-on:'), 1)

    def test_browser_preparation_preserves_full_chromium_unless_shell_was_declared(self):
        calls = []
        node = SimpleNamespace(validate=lambda value, root: (value, None), install=lambda *args: None)
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.5"\nbun="1.2.0"\nnode="24.18.1"\n')
        (self.root / 'tests/e2e').mkdir(parents=True)
        config = self.config | {'node': {'packages': [{'directory': 'tests/e2e', 'manager': 'bun'}]}}
        record = {'checks': [], 'prepare': {'packages': ['tests/e2e'], 'browser': 'tests/e2e'}}
        with patch.object(driver, 'tools_modules', return_value=(workflows, node, None)), \
                patch.object(driver, 'execute') as install, \
                patch.object(driver, 'pinned', side_effect=lambda argv, *args: calls.append(argv)):
            driver.prepare(self.root, self.tools, config, record, 'production', self.environment)
            self.assertIn('node',install.call_args.args[0])
            self.assertEqual(calls[-1], ['bun', 'x', '--no-install', 'playwright', 'install', '--with-deps', 'chromium'])
            record['prepare']['browser_only_shell'] = True
            driver.prepare(self.root, self.tools, config, record, 'production', self.environment)
            self.assertIn('--only-shell', calls[-1])

    def test_browser_install_is_retried_a_bounded_number_of_times(self):
        failure = subprocess.CalledProcessError(100, ['apt-get'])
        attempts, sleeps = [], []
        def flaky(*args):
            attempts.append(args)
            if len(attempts) < 3: raise failure
        with patch.object(driver, 'pinned', flaky):
            driver.install_browser(['playwright', 'install'], self.root, {}, sleep=sleeps.append)
        self.assertEqual((len(attempts), sleeps), (3, [5, 5]))
        attempts.clear()
        with patch.object(driver, 'pinned', side_effect=subprocess.TimeoutExpired('apt-get', 300)) as pinned:
            with self.assertRaises(subprocess.TimeoutExpired):
                driver.install_browser(['playwright', 'install'], self.root, {}, sleep=lambda _: None)
        self.assertEqual(pinned.call_count, 3)
        with patch.object(driver, 'pinned', side_effect=ValueError('refused')) as pinned:
            with self.assertRaises(ValueError):
                driver.install_browser(['playwright', 'install'], self.root, {}, sleep=lambda _: None)
        self.assertEqual(pinned.call_count, 1)

    def test_retired_operational_audit_report_refuses_before_any_sdk_or_issue_effect(self):
        with patch.dict(os.environ, {'OPERATION':'security'}, clear=True), \
                patch.object(driver,'authority') as authority, patch.object(driver,'execute') as execute, \
                patch.object(sys,'argv',['operational.py','report']):
            with self.assertRaises(SystemExit):
                driver.main()
            authority.assert_not_called()
            execute.assert_not_called()

    def test_source_and_deployed_audits_have_separate_explicit_execution_phases(self):
        effects=[]
        deployed=SimpleNamespace(scan=lambda *args,**kwargs:effects.append('deployed'),
            registry_authority=lambda environment:nullcontext(environment))
        original_authority=driver.authority
        with chdir(self.root), patch.dict(os.environ,self.environment | {'OPERATION':'security'},clear=True), \
                patch.object(driver,'ROOT',self.actions), \
                patch.object(driver,'authority',side_effect=lambda root,tools,operation,selected,environment,**kwargs:
                    original_authority(root,tools,operation,selected,environment,self.lookup,**kwargs)), \
                patch.object(driver,'tools_modules',return_value=(self.operations,None,None)), \
                patch.object(driver,'prepare',side_effect=lambda *args,**kwargs:effects.append('prepare')), \
                patch.object(driver,'source_security',side_effect=lambda *args,**kwargs:effects.append('source')), \
                patch.object(driver,'module',return_value=deployed):
            with patch.object(sys,'argv',['operational.py','source','--tools',str(self.tools)]):
                driver.main()
            self.assertEqual(effects,['prepare','source'])
            effects.clear()
            with patch.object(sys,'argv',['operational.py','deployed','--tools',str(self.tools)]),self.assertRaises(SystemExit):
                driver.main()
            self.assertEqual(effects,[])
            with patch.object(sys,'argv',['operational.py','deployed','--tools',str(self.tools),
                    '--activation-reader-root',str(self.root/'reader-tools')]):
                driver.main()
            self.assertEqual(effects,['deployed'])
        text=(Path(__file__).parents[1]/'.github/workflows/audit-security.yml').read_text()
        self.assertIn("fromJSON(inputs['step-timeouts']).source",text)
        self.assertIn("fromJSON(inputs['step-timeouts']).deployed",text)

    def test_deployed_go_policy_prepares_real_scanners_and_refuses_missing_pins(self):
        (self.root/'mise.toml').write_text('[tools]\npython="3.13.11"\ngo="1.27.3"\ntrivy="0.74.0"\n"go:golang.org/x/vuln/cmd/govulncheck"="1.7.0"\n')
        node=SimpleNamespace(validate=lambda value,root:(value,None),install=lambda *args:None)
        effects=[]
        scanner=SimpleNamespace(policy=lambda *args:{'go':{'gate':{}}})
        with patch.object(driver,'tools_modules',return_value=(workflows,node,None)), \
                patch.object(driver,'module',return_value=scanner), \
                patch.object(driver,'execute',side_effect=lambda argv,*args:effects.append(argv)):
            driver.prepare(self.root,self.tools,self.config,self.data['security'],'security',self.environment)
            self.assertTrue({'go','go:golang.org/x/vuln/cmd/govulncheck','trivy'}<=set(effects[0]))
            effects.clear()
            (self.root/'mise.toml').write_text('[tools]\npython="3.13.11"\ngo="1.27.3"\ntrivy="0.74.0"\n')
            with self.assertRaisesRegex(ValueError,'exact native Mise pins; missing: go:golang.org/x/vuln/cmd/govulncheck'):
                driver.prepare(self.root,self.tools,self.config,self.data['security'],'security',self.environment)
            self.assertEqual(effects,[])


if __name__ == '__main__':
    unittest.main()
