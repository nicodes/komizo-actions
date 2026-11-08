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

    def test_native_request_requires_the_real_provider_before_private_route_resolution(self):
        root=Path(__file__).resolve().parents[1]
        workflow=yaml.safe_load((root/'.github/workflows/cookbook-preview-request.yml').read_text())
        inputs=workflow['on']['workflow_call']['inputs']
        self.assertTrue(inputs['cookbook-ref']['required']);self.assertNotIn('tools-ref',inputs)
        steps=workflow['jobs']['request']['steps']
        setup=next(i for i,step in enumerate(steps) if step.get('uses','').endswith('/recipe-sources'))
        routes=next(i for i,step in enumerate(steps) if step.get('id')=='routes')
        self.assertLess(setup,routes)
        self.assertIn('steps.request.outputs.enabled',steps[routes]['if'])


if __name__=='__main__':unittest.main()
