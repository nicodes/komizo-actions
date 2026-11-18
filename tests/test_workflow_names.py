"""Workflow filenames, triggers and dependency admission share one name."""
from pathlib import Path
import tomllib
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class WorkflowNames(unittest.TestCase):
    def test_every_workflow_execution_has_an_explicit_label(self):
        paths = list((ROOT / '.github/workflows').glob('*.yml'))
        self.assertTrue(paths)
        for path in paths:
            document = yaml.safe_load(path.read_text())
            with self.subTest(workflow=path.name):
                for job in document['jobs'].values():
                    self.assertTrue(job.get('name'))
                    for step in job.get('steps', []):
                        self.assertTrue(step.get('name'))

    def test_dependency_trigger_and_policy_select_the_current_test_workflow(self):
        directory = ROOT / '.github/workflows'
        build = yaml.safe_load((directory / 'build-and-test.yml').read_text())
        dependency = yaml.safe_load((directory / 'dependency-policy.yml').read_text())
        policy = tomllib.loads((ROOT / '.github/dependency-policy.toml').read_text())
        self.assertEqual(build['name'], 'Build and test')
        self.assertEqual(dependency['on']['workflow_run']['workflows'], [build['name']])
        self.assertEqual(policy['ci_workflow'], 'build-and-test.yml')
        self.assertEqual(policy['dispatch_workflow'], 'build-and-test.yml')
        aggregate = build['jobs']['build-test']
        self.assertEqual(policy['required_checks'], [aggregate['name']])
        self.assertIn(policy['integration_job'], aggregate['needs'])
        self.assertEqual(build['jobs'][policy['integration_job']]['name'], 'Test')


if __name__ == '__main__': unittest.main()
