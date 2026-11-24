"""One-job workflows build in a source tree that only ever differs from Git inside .artifacts/."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
FILES = ['deliver.yml', 'preview.yml']


def steps(name):
    return yaml.safe_load((ROOT / '.github/workflows' / name).read_text())['jobs']['deliver' if name == 'deliver.yml' else 'preview']['steps']


class CleanSourceTree(unittest.TestCase):
    def test_only_the_source_checkout_lands_outside_artifacts(self):
        for name in FILES:
            checkouts = [step for step in steps(name) if str(step.get('uses', '')).startswith('actions/checkout@')]
            outside = [step['name'] for step in checkouts if not str(step['with'].get('path', '')).startswith('.artifacts/')]
            with self.subTest(workflow=name):
                self.assertEqual(outside, ['Check out source'])

    def test_no_script_or_action_path_points_at_a_workspace_root_directory(self):
        for name in FILES:
            text = (ROOT / '.github/workflows' / name).read_text()
            for old in ['_delivery_actions', '_preview_actions', '_preview_controller']:
                with self.subTest(workflow=name, path=old):
                    self.assertNotIn(old, text)

    def test_a_guard_fails_on_a_modified_tree_right_before_build(self):
        for name in FILES:
            sequence = steps(name)
            names = [step['name'] for step in sequence]
            guard = sequence[names.index('Require an unmodified source tree before Build')]
            with self.subTest(workflow=name):
                self.assertEqual(names.index('Build') - 1, names.index(guard['name']))
                self.assertIn('git status --porcelain --untracked-files=normal', guard['run'])
                self.assertIn('exit 1', guard['run'])
                self.assertIn('modified', guard['run'])


if __name__ == '__main__': unittest.main()
