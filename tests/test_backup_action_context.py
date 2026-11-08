"""Backup adapters retain verified modern roots and the genuine legacy target."""
import json
import os
from pathlib import Path
import unittest
import yaml
import test_backup_namespace as namespaces

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ('application-backup-export', 'application-backup-upload')
EXPRESSION = "${{ env.COOKBOOK_ROOT && env.TOOLS_ROOT || format('{0}/_delivery_cookbook', github.workspace) }}"


def selected_tools_root(action, environment, workspace):
    # Evaluate this finite GitHub expression only; no action/native effect runs.
    data = yaml.load((ROOT / action / 'action.yml').read_text(), Loader=yaml.BaseLoader)
    expression = data['runs']['steps'][0]['env']['TOOLS_ROOT']
    if expression != EXPRESSION:
        raise AssertionError('backup action no longer declares the reviewed two-source boundary')
    return (environment.get('COOKBOOK_ROOT') and environment.get('TOOLS_ROOT')) or str(workspace / '_delivery_cookbook')


class BackupActionContext(unittest.TestCase):
    def setUp(self):
        self.fixture = namespaces.BackupNamespace()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        # Reproduce the old alias with a real clean Cookbook Git source.
        destination = self.fixture.root / '_delivery_cookbook'
        self.fixture.cookbook.rename(destination)
        self.fixture.cookbook = destination

    def environment(self):
        return {'COOKBOOK_ROOT': str(self.fixture.cookbook),
                'COOKBOOK_REF': self.fixture.ref,
                'TOOLS_ROOT': str(self.fixture.tools),
                'TOOLS_REF': self.fixture.tools_ref}

    def context(self, selected, *, refuses=False):
        code = """import os
os.environ.update(COOKBOOK_ROOT=str(cookbook),COOKBOOK_REF=reference)
selected = Path(SELECTED)
""".replace('SELECTED', repr(str(selected)))
        if refuses:
            code += """try: backup.local_recipe_context(root, tools_root=selected)
except ValueError: print(json.dumps({'refused':True}))
else: raise AssertionError('Cookbook cannot stand in for its actual Tools dependency')
"""
        else:
            code += """context=backup.local_recipe_context(root, tools_root=selected)
assert context.cookbook==cookbook and context.tools==tools
assert not list(cookbook.rglob('__pycache__')) and not list(tools.rglob('__pycache__'))
print(json.dumps(context.identities()))
"""
        return self.fixture.control(code)

    def test_export_and_upload_keep_actual_derived_Tools_after_common_preparation(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                selected = selected_tools_root(action, self.environment(), self.fixture.root)
                self.assertEqual(selected, str(self.fixture.tools))
                actual = self.context(selected)
                self.assertEqual(actual['cookbook_commit'], self.fixture.ref)
                self.assertEqual(actual['tools_commit'], self.fixture.tools_ref)
                self.assertNotEqual(actual['cookbook_commit'], actual['tools_commit'])

    def test_old_fixed_Cookbook_alias_is_refused_before_backup_effects(self):
        old_selected = self.fixture.root / '_delivery_cookbook'
        self.assertTrue(self.context(old_selected, refuses=True)['refused'])

    def test_no_modern_claim_keeps_original_fixed_legacy_target_even_with_ambient_Tools(self):
        for action in ACTIONS:
            for environment in [{}, {'TOOLS_ROOT': '/unselected-ambient-root'}, {'COOKBOOK_ROOT': '', 'TOOLS_ROOT': '/unselected-ambient-root'}]:
                with self.subTest(action=action, environment=environment):
                    self.assertEqual(selected_tools_root(action, environment, self.fixture.root),
                                     str(self.fixture.root / '_delivery_cookbook'))

    def test_modern_foreign_helper_is_preserved_for_strict_refusal_and_never_repaired(self):
        self.fixture.git(self.fixture.tools, 'remote', 'set-url', 'origin', 'https://github.com/foreign/tools.git')
        for action in ACTIONS:
            selected = selected_tools_root(action, self.environment(), self.fixture.root)
            self.assertEqual(selected, str(self.fixture.tools))
            self.assertTrue(self.context(selected, refuses=True)['refused'])
        self.assertEqual(self.fixture.git(self.fixture.tools, 'remote', 'get-url', 'origin'),
                         'https://github.com/foreign/tools.git')


if __name__ == '__main__':
    unittest.main()
