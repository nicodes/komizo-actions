"""Every public effect workflow selects the real Cookbook and its derived helpers."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class NativeWorkflowContext(unittest.TestCase):
    def assert_environment_scope(self, workflow, name, job):
        effects = {('cookbook-deliver.yml', 'deliver'): '${{ inputs.environment }}',
                   ('cookbook-preview-effects.yml', 'effects'): 'Preview'}
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
            expected = 'Preview' if workflow == 'cookbook-preview-request.yml' else '${{ inputs.environment }}'
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
            ('cookbook-deliver.yml', 'deliver'), ('cookbook-preview-effects.yml', 'effects'),
            ('cookbook-backup.yml', 'backup'), ('cookbook-legacy-retention.yml', 'verify'),
            ('cookbook-notify.yml', 'notify'), ('cookbook-preview-request.yml', 'request'),
            ('cookbook-production-check.yml', 'verify'), ('cookbook-recovery.yml', 'verify'),
            ('cookbook-report.yml', 'notify')})

    def test_operational_environment_contract_refuses_tracking_or_changed_secret_scope(self):
        for value in ['${{ inputs.environment }}',
                      {'name': '${{ inputs.environment }}', 'deployment': True},
                      {'name': '${{ inputs.environment }}', 'deployment': 'false'},
                      {'name': 'other-environment', 'deployment': False}]:
            with self.subTest(environment=value), self.assertRaises(AssertionError):
                self.assert_environment_scope('cookbook-notify.yml', 'notify', {'environment': value})
        for workflow, name, environment in [('cookbook-deliver.yml', 'deliver', '${{ inputs.environment }}'),
                                             ('cookbook-preview-effects.yml', 'effects', 'Preview')]:
            with self.subTest(workflow=workflow), self.assertRaises(AssertionError):
                self.assert_environment_scope(workflow, name,
                                              {'environment': {'name': environment, 'deployment': False}})

    def test_public_recipe_interfaces_require_real_cookbook_and_have_no_tools_alias(self):
        for path in [*ROOT.glob('.github/workflows/cookbook-*.yml'), *ROOT.glob('*/action.yml')]:
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
        publish = yaml.safe_load((ROOT / '.github/workflows/cookbook-publish.yml').read_text())['jobs']['publish']
        self.assertEqual(publish['permissions']['packages'], 'write')
        self.assertNotIn('id-token', publish['permissions'])
        self.assertNotIn('KOMIZO_DEPLOY_KEY', str(publish))
        delivery = yaml.safe_load((ROOT / '.github/workflows/cookbook-deliver.yml').read_text())['jobs']['deliver']
        self.assertEqual(delivery['environment'], '${{ inputs.environment }}')
        self.assertFalse(yaml.safe_load((ROOT / '.github/workflows/cookbook-deliver.yml').read_text())['concurrency']['cancel-in-progress'])
        steps = delivery['steps']
        authorizer = next(i for i, step in enumerate(steps) if 'scripts/authorize.py' in step.get('run', ''))
        deploy = next(i for i, step in enumerate(steps) if step.get('uses', '').endswith('/deploy'))
        backup = next(i for i, step in enumerate(steps) if step.get('uses', '').endswith('/application-backup-export'))
        self.assertLess(backup, authorizer); self.assertLess(authorizer, deploy)
        self.assertNotIn('KOMIZO_SECRET_', str(delivery))
        self.assertFalse(any(step.get('uses', '').endswith('/application-readiness') for step in steps))
        product = yaml.safe_load((ROOT / '.github/workflows/cookbook-readiness.yml').read_text())
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
