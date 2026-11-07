"""Preview authenticates distinct real sources before importing recipe code."""
import unittest
import os
from pathlib import Path
import subprocess

import yaml

import test_operational_recipe_context as sdk_fixture


class PreviewSourceContext(unittest.TestCase):
    def setUp(self):
        self.fixture=sdk_fixture.ExtractedRecipeAuthority()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        module=self.fixture.cookbook/'cookbook/preview_workflows.py'
        module.write_text('# Trusted recipe interface fixture; no product execution.\n')
        self.fixture.cookbook_ref=self.fixture.commit(self.fixture.cookbook)

    def test_real_Cookbook_and_derived_Tools_sources_are_accepted_and_bound(self):
        self.fixture.control('''import preview
preview.ROOT=actions
context=preview.accepted_automation(cookbook,None,komizo,lambda path:json.loads(lookup(path)),
 cookbook_ref=cookie,supplied_tools=tools)
assert context.cookbook==cookbook and context.tools==tools
assert context.identities()['cookbook_commit']==cookie and context.identities()['tools_commit']==tool
assert Path(sys.modules['configuration'].__file__).parent==cookbook/'cookbook'
print(json.dumps(context.identities()))
''')

    def test_mixed_or_foreign_SDK_identity_and_unaccepted_helpers_refuse_before_recipe_import(self):
        self.fixture.control('''import preview
preview.ROOT=actions
for provider,reference,legacy in [(cookbook,cookie,tool),(tools,tool,None)]:
 try:preview.accepted_automation(provider,legacy,komizo,lambda path:json.loads(lookup(path)),cookbook_ref=reference,supplied_tools=tools)
 except ValueError:pass
 else:raise AssertionError('aliased or mixed Preview SDK source admitted')
def blocked(path):return {'status':'behind'} if '/nicodes/tools/' in path else json.loads(lookup(path))
try:preview.accepted_automation(cookbook,None,komizo,blocked,cookbook_ref=cookie,supplied_tools=tools)
except ValueError:pass
else:raise AssertionError('unaccepted Preview helper source admitted')
assert 'configuration' not in sys.modules
print(json.dumps({'refused':True}))
''')

    def test_poisoned_Actions_source_refuses_before_any_recipe_import(self):
        (self.fixture.actions/'unexpected.py').write_text('raise AssertionError("unreviewed source")\n')
        self.fixture.control('''import preview
preview.ROOT=actions
try:preview.accepted_automation(cookbook,None,komizo,lambda path:json.loads(lookup(path)),cookbook_ref=cookie,supplied_tools=tools)
except ValueError:pass
else:raise AssertionError('dirty Preview execution source admitted')
assert 'dependencies' not in sys.modules and 'configuration' not in sys.modules
print(json.dumps({'refused':True}))
''')

    def test_producer_actions_refuse_mixed_input_selectors_before_SDK_preparation(self):
        root=Path(__file__).resolve().parents[1]
        for name in ['plan','verify']:
            action=yaml.safe_load((root/f'preview-producer-{name}/action.yml').read_text())
            steps=action['runs']['steps']
            guard=next(step for step in steps if step.get('name')=='Require one exact recipe source contract')
            self.assertLess(steps.index(guard),next(i for i,step in enumerate(steps) if step.get('uses','').endswith('/recipe-setup')))
            if name=='plan':self.assertIn("steps.request.outputs.action == 'up'",guard['if'])
            for cookie,tools,success in [(self.fixture.cookbook_ref,'',True),('',self.fixture.tools_ref,True),
                (self.fixture.cookbook_ref,self.fixture.tools_ref,False),('main','',False),('','',False)]:
                result=subprocess.run(['bash','-euc',guard['run']],env=dict(os.environ,COOKBOOK_REF=cookie,TOOLS_REF=tools),
                    capture_output=True,text=True,timeout=5)
                with self.subTest(action=name,cookie=cookie,tools=tools):self.assertEqual(result.returncode==0,success)


if __name__=='__main__':unittest.main()
