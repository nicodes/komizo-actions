"""Every public effect workflow selects the real Cookbook and its derived helpers."""
from pathlib import Path
import os
import subprocess
import json
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class NativeWorkflowContext(unittest.TestCase):
    def test_complete_result_refuses_failed_skipped_cancelled_or_missing_test(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/build-and-test.yml').read_text())
        aggregate = workflow['jobs']['build-test']
        self.assertEqual(aggregate['needs'], ['ci'])
        self.assertEqual(aggregate['if'], 'always()')
        guard = aggregate['steps'][0]['run']
        def result(value):
            # The aggregate runs without checking out the project or setting up Mise.
            with tempfile.TemporaryDirectory() as directory:
                return subprocess.run(['bash', '-euo', 'pipefail', '-c', guard], cwd=directory,
                    env=dict(os.environ, RESULTS=json.dumps(value)), capture_output=True).returncode
        passed = {'ci': {'result': 'success', 'outputs': {}}}
        self.assertEqual(result(passed), 0)
        for status in ['failure', 'skipped', 'cancelled', 'in_progress', '']:
            with self.subTest(status=status):
                self.assertNotEqual(result({'ci': {'result': status, 'outputs': {}}}), 0)
        for value in [{}, {'other': passed['ci']}, passed | {'other': passed['ci']},
                      {'ci': {'result': 'success'}}, {'ci': {'result': 'success', 'outputs': []}}]:
            with self.subTest(value=value):
                self.assertNotEqual(result(value), 0)

    def test_provider_security_remains_manual_without_issue_or_host_authority(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/maintenance.yml').read_text())
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
        self.assertEqual(workflow['jobs']['security']['permissions'],
                         {'contents': 'read', 'attestations': 'read'})
        self.assertEqual(workflow['jobs']['dependencies']['permissions'],
                         {'contents': 'read', 'issues': 'write', 'attestations': 'read'})
        self.assertNotIn('id-token', str(workflow['jobs']))

    def test_audit_reporting_and_notification_keep_disjoint_authority(self):
        workflows = ROOT / '.github/workflows'
        for name in ['audit-source.yml', 'audit-security.yml']:
            data = yaml.safe_load((workflows / name).read_text())
            self.assertEqual(set(data['jobs']), {'verify'})
            self.assertNotIn('issues', data['jobs']['verify']['permissions'])
            self.assertNotIn('id-token', data['jobs']['verify']['permissions'])
            self.assertNotIn('report-timeout', data['on']['workflow_call']['inputs'])
            self.assertNotIn('DISCORD_WEBHOOK_URL', str(data))
        report = yaml.safe_load((workflows / 'report-failure.yml').read_text())
        self.assertEqual(set(report['jobs']), {'report'})
        self.assertEqual(report['jobs']['report']['permissions'], {'contents': 'read', 'issues': 'write'})
        self.assertNotIn('environment', report['jobs']['report'])
        self.assertNotIn('secrets', report['on']['workflow_call'])
        self.assertNotIn('OPERATION', str(report))
        notify = yaml.safe_load((workflows / 'notify.yml').read_text())
        self.assertEqual(notify['jobs']['notify']['permissions'], {'contents': 'read'})
        self.assertFalse(set(notify['on']['workflow_call']['inputs']) &
                         {'report-runner', 'report-timeout', 'report-step-timeouts', 'notify'})
        for name in ['report.yml', 'audit-source-and-report.yml', 'audit-security-and-report.yml']:
            self.assertFalse((workflows / name).exists())

    def test_failure_report_refuses_foreign_source_and_untrusted_event_before_setup(self):
        report = yaml.safe_load((ROOT / '.github/workflows/report-failure.yml').read_text())
        guard = report['jobs']['report']['steps'][0]['run']
        source = 'a' * 40
        environment = dict(os.environ, COOKBOOK_REF='b' * 40, KOMIZO_REF='c' * 40,
            GITHUB_REPOSITORY='owner/application', GITHUB_SHA=source, GITHUB_WORKFLOW_SHA=source,
            GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1', GITHUB_REF='refs/heads/main',
            GITHUB_EVENT_NAME='schedule',
            GITHUB_WORKFLOW_REF='owner/application/.github/workflows/security.yml@refs/heads/main')
        def run(changes):
            return subprocess.run(['bash', '-euo', 'pipefail', '-c', guard],
                                  env=environment | changes, capture_output=True).returncode
        self.assertEqual(run({}), 0)
        self.assertEqual(run({'GITHUB_EVENT_NAME': 'push',
            'GITHUB_WORKFLOW_REF': 'owner/application/.github/workflows/deploy.yml@refs/heads/main'}), 0)
        for changes in [{'COOKBOOK_REF': 'main'}, {'KOMIZO_REF': 'main'},
            {'GITHUB_REPOSITORY': 'other'}, {'GITHUB_WORKFLOW_SHA': 'd' * 40},
            {'GITHUB_RUN_ID': '0'}, {'GITHUB_RUN_ATTEMPT': '0'},
            {'GITHUB_REF': 'refs/heads/feature'}, {'GITHUB_EVENT_NAME': 'push'}, {'GITHUB_EVENT_NAME': 'pull_request'},
            {'GITHUB_EVENT_NAME': 'pull_request_target'},
            {'GITHUB_WORKFLOW_REF': 'owner/application/.github/workflows/deploy.yml@refs/heads/main'},
            {'GITHUB_WORKFLOW_REF': 'other/application/.github/workflows/security.yml@refs/heads/main'},
            {'GITHUB_WORKFLOW_REF': 'owner/application/.github/workflows/build-and-test.yml@refs/heads/main'}]:
            with self.subTest(changes=changes):
                self.assertNotEqual(run(changes), 0)

    def assert_environment_scope(self, workflow, name, job):
        effects = {('deploy.yml', 'deliver'): '${{ inputs.environment }}',
                   ('deliver.yml', 'deliver'): '${{ inputs.environment }}',
                   ('pr-preview.yml', 'effects'): 'Preview'}
        value = job['environment']
        if (workflow, name) in effects:
            expected = effects[workflow, name]
            if isinstance(value, dict):
                self.assertEqual(value['name'], expected)
                self.assertIs(value.get('deployment', True), True)
            else:
                self.assertEqual(value, expected)
        else:
            self.assertIsInstance(value, dict)
            self.assertEqual(set(value), {'name', 'deployment'})
            expected = 'Preview' if workflow == 'request-preview.yml' else '${{ inputs.environment }}'
            self.assertEqual(value['name'], expected)
            self.assertIs(value['deployment'], False)

    def test_only_native_lifecycle_effects_create_environment_deployments(self):
        observed = set()
        for path in ROOT.glob('.github/workflows/*.yml'):
            for name, job in yaml.safe_load(path.read_text()).get('jobs', {}).items():
                if 'environment' not in job:
                    continue
                with self.subTest(workflow=path.name, job=name):
                    self.assert_environment_scope(path.name, name, job)
                observed.add((path.name, name))
        self.assertEqual(observed, {
            ('deploy.yml', 'deliver'), ('deliver.yml', 'deliver'), ('pr-preview.yml', 'effects'),
            ('backup.yml', 'backup'),
            ('notify.yml', 'notify'), ('request-preview.yml', 'request'),
            ('verify-production.yml', 'verify')})

    def test_operational_environment_contract_refuses_tracking_or_changed_secret_scope(self):
        for value in ['${{ inputs.environment }}',
                      {'name': '${{ inputs.environment }}', 'deployment': True},
                      {'name': '${{ inputs.environment }}', 'deployment': 'false'},
                      {'name': 'other-environment', 'deployment': False}]:
            with self.subTest(environment=value), self.assertRaises(AssertionError):
                self.assert_environment_scope('notify.yml', 'notify', {'environment': value})
        for workflow, name, environment in [('deploy.yml', 'deliver', '${{ inputs.environment }}'),
                                             ('pr-preview.yml', 'effects', 'Preview')]:
            with self.subTest(workflow=workflow), self.assertRaises(AssertionError):
                self.assert_environment_scope(workflow, name,
                                              {'environment': {'name': environment, 'deployment': False}})

    def test_public_recipe_interfaces_require_real_cookbook_and_have_no_tools_alias(self):
        for path in [*ROOT.glob('.github/workflows/*.yml'), *ROOT.glob('*/action.yml')]:
            value = yaml.safe_load(path.read_text())
            called = value.get('on', {}).get('workflow_call', {}) if isinstance(value.get('on'), dict) else {}
            inputs = called.get('inputs', {}) if called else value.get('inputs', {})
            if 'cookbook-ref' not in inputs: continue
            with self.subTest(path=path.name):
                self.assertTrue(inputs['cookbook-ref']['required'])
                self.assertNotIn('tools-ref', inputs)
                self.assertNotIn('inputs.tools-ref', path.read_text())
                self.assertNotIn('--tools ', path.read_text())

    def test_source_execution_has_no_host_or_oidc_authority_and_effects_use_fixed_submission(self):
        publish = yaml.safe_load((ROOT / '.github/workflows/publish.yml').read_text())['jobs']['publish']
        self.assertEqual(publish['permissions']['packages'], 'write')
        self.assertNotIn('id-token', publish['permissions'])
        self.assertNotIn('KOMIZO_DEPLOY_KEY', str(publish))
        delivery = yaml.safe_load((ROOT / '.github/workflows/deploy.yml').read_text())['jobs']['deliver']
        self.assertEqual(delivery['environment'], '${{ inputs.environment }}')
        self.assertFalse(yaml.safe_load((ROOT / '.github/workflows/deploy.yml').read_text())['concurrency']['cancel-in-progress'])
        steps = delivery['steps']
        authorizer = next(i for i, step in enumerate(steps) if 'scripts/authorize.py' in step.get('run', ''))
        deploy = next(i for i, step in enumerate(steps) if step.get('uses', '').endswith('/deploy'))
        backup = next(i for i, step in enumerate(steps) if step.get('uses', '').endswith('/backup-transfer')
                      and step['with']['mode'] == 'export')
        self.assertLess(backup, authorizer); self.assertLess(authorizer, deploy)
        self.assertNotIn('KOMIZO_SECRET_', str(delivery))
        self.assertFalse(any(step.get('uses', '').endswith('/application-readiness') for step in steps))
        product = yaml.safe_load((ROOT / '.github/workflows/readiness.yml').read_text())
        role = product['jobs']['verify']
        self.assertNotIn('id-token', role['permissions']); self.assertNotIn('packages', role['permissions'])
        self.assertNotIn('KOMIZO_DEPLOY_KEY', str(product)); self.assertNotIn('registry-token', str(product))
        self.assertNotIn('environment', role)
        sources = yaml.safe_load((ROOT / 'recipe-sources/action.yml').read_text())
        self.assertNotIn('mise', str(sources['runs']['steps']))
        self.assertFalse(any(step.get('uses', '').endswith('/sdk-setup') for step in sources['runs']['steps']))

    def test_native_lifecycle_has_no_shell_owner_secret_or_configuration_frontdoor(self):
        deploy = yaml.safe_load((ROOT / 'deploy/action.yml').read_text())
        lifecycle = yaml.safe_load((ROOT / 'run-task/action.yml').read_text())
        for action in [deploy, lifecycle]:
            self.assertFalse({'command', 'config', 'config-image', 'user', 'secrets'} & set(action['inputs']))
        for obsolete in ['publish-config', 'set-secrets', 'set-service-env', 'activate', 'godot-static-delivery', 'godot-static-recovery']:
            self.assertFalse((ROOT / obsolete / 'action.yml').exists())
        connect = yaml.safe_load((ROOT / 'connect/action.yml').read_text())
        self.assertEqual(connect['runs'], {'using': 'node24', 'main': 'connect.cjs', 'post': 'cleanup.cjs', 'post-if': 'always()'})


if __name__ == '__main__': unittest.main()
