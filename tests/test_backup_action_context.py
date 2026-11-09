"""Backup adapters require verified native recipe and helper sources."""
import json
import os
from pathlib import Path
import unittest
import yaml
import test_backup_namespace as namespaces

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ('application-backup-export', 'application-backup-upload')
EXPRESSION = '${{ env.TOOLS_ROOT }}'


def selected_tools_root(action, environment, workspace):
    # Evaluate this finite GitHub expression only; no action/native effect runs.
    data = yaml.load((ROOT / action / 'action.yml').read_text(), Loader=yaml.BaseLoader)
    expression = data['runs']['steps'][0]['env']['TOOLS_ROOT']
    if expression != EXPRESSION:
        raise AssertionError('backup action no longer declares the reviewed two-source boundary')
    return environment.get('TOOLS_ROOT','')


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

    def test_missing_native_identity_cannot_select_ambient_or_old_recipe_sources(self):
        (self.fixture.caller/'mise.toml').unlink()
        (self.fixture.caller/'Makefile').write_text('COOKBOOK_REF := '+self.fixture.ref+'\n')
        for action in ACTIONS:
            for environment in [{}, {'TOOLS_ROOT': '/unselected-ambient-root'}]:
                with self.subTest(action=action,environment=environment):
                    selected=selected_tools_root(action,environment,self.fixture.root)
                    self.assertEqual(selected,environment.get('TOOLS_ROOT',''))
                    self.fixture.control("""try:backup.local_recipe_context(root)
except (OSError,ValueError):print(json.dumps({'refused':True}))
else:raise AssertionError('missing native identity selected an old SDK')
""")

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
