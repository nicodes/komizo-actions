import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import native_recipe_fixture

TOOLS = Path(os.environ['RECIPE_TASK_SDK'])
spec = importlib.util.spec_from_file_location('application_readiness', Path(__file__).parents[1] / 'scripts/application_readiness.py')
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)


class NativeReadiness(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'deploy/health').mkdir(parents=True)
        (self.root / 'deploy/health/verify.py').write_text('assert True\n')
        self.delivery = dict(version=1, project='demo', komizo_ref='a' * 40,
            publish_runner='ubuntu-24.04', publish_timeout=30, deploy_runner='ubuntu-24.04',
            deploy_timeout=30, health_urls=['https://example.test/readyz'],
            notification_url='https://example.test/', readiness_operation=True)
        self.operations = dict(version=1, project='demo',
            production=dict(runner='ubuntu-24.04', timeout_minutes=30, environment='production',
                checks=[dict(runner='python', path='deploy/health/verify.py')]),
            paid=[dict(id='paid_smoke', description='Original optional model check',
                runner='ubuntu-24.04', timeout_minutes=5, environment='production',
                check=dict(runner='python', path='deploy/health/verify.py'))])
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[delivery]\ndefinition="deploy/delivery.toml"\n[operations]\ndefinition="deploy/operations.toml"\n')
        self.environment = dict(DECLARATION='deploy/delivery.toml', GITHUB_SHA='b' * 40)
        decoder=patch.object(readiness.recipe_data,'load',side_effect=lambda root,tools: native_recipe_fixture.load(root))
        decoder.start();self.addCleanup(decoder.stop)
        modules=patch.dict(sys.modules,{name:native_recipe_fixture.Module(name) for name in ['delivery_workflows','operational_workflows']})
        modules.start();self.addCleanup(modules.stop)

    def write(self):
        native_recipe_fixture.write(self.root / 'deploy/delivery.toml', self.delivery)
        native_recipe_fixture.write(self.root / 'deploy/operations.toml', self.operations)

    def test_only_ordinary_checks_run_after_explicit_preparation_in_the_same_environment(self):
        self.write()
        calls = []
        environment = self.environment | {'TOOLS_ROOT': str(TOOLS)}
        driver = SimpleNamespace(runtime_environment=lambda *args: environment,
            prepare=lambda root, tools, config, record, role, env: calls.append(('prepare', role, env)),
            native=lambda root, check, env: calls.append(('native', check, env)))
        readiness.readiness(self.root, TOOLS, self.environment, driver)
        self.assertEqual([item[0] for item in calls], ['prepare', 'native'])
        self.assertEqual(calls[0][1], 'production')
        self.assertTrue(all(item[2] is environment for item in calls))
        self.assertEqual(calls[1][1], self.operations['production']['checks'][0])
        self.assertNotIn('OPENROUTER_API_KEY', environment)

    def test_changed_declaration_and_missing_production_assertions_refuse_before_effects(self):
        self.write()
        with patch.object(readiness.subprocess, 'run') as effects:
            with self.assertRaises(ValueError):
                readiness.readiness(self.root, TOOLS, self.environment | dict(DECLARATION='other.json'))
            self.operations.pop('production')
            self.operations.pop('paid')
            self.write()
            with self.assertRaisesRegex(ValueError, 'real native production assertions'):
                readiness.readiness(self.root, TOOLS, self.environment)
            effects.assert_not_called()


if __name__ == '__main__':
    unittest.main()
