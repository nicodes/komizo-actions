import copy
from contextlib import chdir
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
        for name, text in workflows.workflow_files(self.root, tools_sha, self.config).items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git(self.root, 'add', '.')
        self.git(self.root, 'commit', '-qm', 'Native operational policy')
        self.revision = self.git(self.root, 'rev-parse', 'HEAD')
        self.event = self.directory / 'event.json'
        self.event.write_text(json.dumps({'inputs': {'model_smoke': False}}))
        self.environment = dict(os.environ, GITHUB_REPOSITORY='owner/demo', GITHUB_SHA=self.revision,
            GITHUB_WORKFLOW_SHA=self.revision, GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch',
            GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/security-scan.yml@refs/heads/main',
            GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1', GITHUB_EVENT_PATH=str(self.event),
            TOOLS_REF=tools_sha, KOMIZO_REF=actions_sha, DECLARATION='deploy/operations.toml',
            OPERATION_RUNNER='ubuntu-24.04', OPERATION_TIMEOUT='110', OPERATION_ENVIRONMENT='', OPERATION_REPORT_TIMEOUT='5',
            OPERATION_STEP_TIMEOUTS=json.dumps(workflows.step_timeouts('security',self.data['security'])))

    def lookup(self, path):
        if '/compare/' in path:
            return json.dumps({'status': 'identical'})
        if '/git/ref/heads/main' in path:
            return json.dumps({'object': {'sha': self.revision}})
        return json.dumps({'workflow_runs': [dict(head_sha=self.revision, head_branch='main', conclusion='success',
            path='.github/workflows/cd.yml', event='workflow_dispatch', run_attempt=1)]})

    def admit(self, environment=None, operation='security', selected='', lookup=None):
        with patch.object(driver, 'ROOT', self.actions), patch.object(driver, 'tools_modules', return_value=(workflows, None, None)):
            return driver.authority(self.root, self.tools, operation, selected,
                                    environment or self.environment, lookup or self.lookup)

    def paid_environment(self):
        return self.environment | dict(GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/production-verification.yml@refs/heads/main',
            OPERATION_TIMEOUT='20', OPERATION_ENVIRONMENT='Production',
            OPERATION_STEP_TIMEOUTS=json.dumps(workflows.step_timeouts('paid',self.data['paid'][0])))

    def test_actual_clean_git_sources_and_canonical_caller_are_admitted(self):
        _, data, record = self.admit()
        self.assertEqual(data['project'], 'demo')
        self.assertEqual(record['report_timeout'], 5)

    def test_source_workflow_runner_budget_and_report_identity_refusals(self):
        for changes in [dict(GITHUB_REF='refs/heads/feature'), dict(GITHUB_SHA='f' * 40),
                        dict(GITHUB_WORKFLOW_SHA='f' * 40), dict(GITHUB_EVENT_NAME='pull_request'),
                        dict(GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/ci.yml@refs/heads/main'),
                        dict(GITHUB_RUN_ATTEMPT='0'), dict(OPERATION_RUNNER='ubuntu-22.04'),
                        dict(OPERATION_TIMEOUT='111'), dict(OPERATION_REPORT_TIMEOUT='11'), dict(OPERATION_STEP_TIMEOUTS='{}'),
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
        path = self.root / '.github/workflows/security-scan.yml'
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
        self.event.write_text(json.dumps({'inputs': {'model_smoke': True}}))
        self.admit(environment, 'paid', 'model_smoke')
        with self.assertRaisesRegex(ValueError, 'manual dispatch'):
            self.admit(environment | dict(GITHUB_EVENT_NAME='schedule'), 'paid', 'model_smoke')
        for changes in [dict(head_sha='f' * 40), dict(conclusion='failure'), dict(path='.github/workflows/ci.yml'),
                        dict(event='pull_request'), dict(run_attempt=0)]:
            def lookup(path):
                value = json.loads(self.lookup(path))
                if 'workflow_runs' in value:
                    value['workflow_runs'][0].update(changes)
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
        target.write_text(json.dumps({'inputs': {'model_smoke': True}}))
        self.event.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'bounded'):
            self.admit(environment, 'paid', 'model_smoke')
        self.event.unlink()
        self.event.write_text(target.read_text())
        def old_main(path):
            return json.dumps({'object': {'sha': 'f' * 40}}) if '/git/ref/heads/main' in path else self.lookup(path)
        with self.assertRaisesRegex(ValueError, 'current main'):
            self.admit(environment, 'paid', 'model_smoke', old_main)

    def test_legacy_retention_requires_exact_manual_selection_and_immutable_ids(self):
        legacy = dict(runner='ubuntu-22.04', timeout_minutes=10,
                      environment='production', format='age-pb-v1')
        self.data['recovery'] = dict(runner='ubuntu-24.04', timeout_minutes=45,
                                    environment='production', legacy_retention=legacy)
        native_recipe_fixture.write(self.root / 'deploy/operations.toml', self.data)
        for name, text in workflows.workflow_files(self.root, self.environment['TOOLS_REF'], self.config).items():
            (self.root / name).write_text(text)
        self.git(self.root, 'add', '.')
        self.git(self.root, 'commit', '-qm', 'Legacy retention policy')
        self.revision = self.git(self.root, 'rev-parse', 'HEAD')
        environment = self.environment | dict(GITHUB_SHA=self.revision, GITHUB_WORKFLOW_SHA=self.revision,
            GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/recovery.yml@refs/heads/main',
            OPERATION_RUNNER='ubuntu-22.04', OPERATION_TIMEOUT='10', OPERATION_ENVIRONMENT='production',
            OPERATION_STEP_TIMEOUTS=json.dumps(workflows.step_timeouts('retention', legacy)))
        valid = dict(operation='retain-legacy', **{'artifact-id': '456', 'run-id': '789'})
        self.event.write_text(json.dumps(dict(inputs=valid)))
        self.admit(environment, 'retention')
        for changes in [dict(operation='verify-transport'), {'artifact-id': '../escape'}, {'run-id': '0'}]:
            self.event.write_text(json.dumps(dict(inputs=valid | changes)))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.admit(environment, 'retention')
        self.event.write_text(json.dumps(dict(inputs=valid)))
        with self.assertRaisesRegex(ValueError, 'manual dispatch'):
            self.admit(environment | dict(GITHUB_EVENT_NAME='schedule'), 'retention')

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
        for name in ['backup', 'recovery', 'security-audit', 'production-check', 'public-production-check']:
            text = (root / ('cookbook-' + name + '.yml')).read_text()
            with self.subTest(workflow=name):
                self.assertLess(text.index('Prepare accepted recipe SDKs'), text.index('Admit exact trusted main'))
                self.assertNotIn('install_args: python', text)
                self.assertIn("PYTHONDONTWRITEBYTECODE: '1'", text)
        production = (root / 'cookbook-production-check.yml').read_text()
        preparation = production.split('    - name: Run owned production assertions')[0]
        self.assertNotIn('secrets.OPENROUTER_API_KEY', preparation)

    def test_inline_paid_steps_keep_credentials_late_inside_the_same_job(self):
        text = (Path(__file__).parents[1] / '.github/workflows/cookbook-production-check.yml').read_text()
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

    def test_source_and_deployed_audits_have_separate_explicit_execution_phases(self):
        effects=[]
        deployed=SimpleNamespace(scan=lambda *args,**kwargs:effects.append('deployed'))
        original_authority=driver.authority
        with chdir(self.root), patch.dict(os.environ,self.environment | {'OPERATION':'security'},clear=True), \
                patch.object(driver,'ROOT',self.actions), \
                patch.object(driver,'authority',side_effect=lambda root,tools,operation,selected,environment,**kwargs:
                    original_authority(root,tools,operation,selected,environment,self.lookup,**kwargs)), \
                patch.object(driver,'tools_modules',return_value=(workflows,None,None)), \
                patch.object(driver,'prepare',side_effect=lambda *args,**kwargs:effects.append('prepare')), \
                patch.object(driver,'source_security',side_effect=lambda *args,**kwargs:effects.append('source')), \
                patch.object(driver,'module',return_value=deployed):
            with patch.object(sys,'argv',['operational.py','source','--tools',str(self.tools)]):
                driver.main()
            self.assertEqual(effects,['prepare','source'])
            effects.clear()
            with patch.object(sys,'argv',['operational.py','deployed','--tools',str(self.tools)]):
                driver.main()
            self.assertEqual(effects,['deployed'])
        text=(Path(__file__).parents[1]/'.github/workflows/cookbook-security-audit.yml').read_text()
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
            with self.assertRaisesRegex(ValueError,'exact native Mise pins'):
                driver.prepare(self.root,self.tools,self.config,self.data['security'],'security',self.environment)
            self.assertEqual(effects,[])


if __name__ == '__main__':
    unittest.main()
