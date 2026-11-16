"""Fresh authority follows the one complete SDK route without router aliases."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import authorize


class ExactRecipeProviders(unittest.TestCase):
    cookbook = 'c' * 40
    actions = 'd' * 40
    workflows = {'host': 'cookbook-validation.yml', 'native': 'cookbook-native.yml',
                 'godot': 'cookbook-godot.yml'}

    def metadata(self, route, preview=False, combined=False):
        paths = ['nicodes/cookbook/.github/workflows/' + self.workflows[route] + '@' + self.cookbook,
            'nicodes/komizo-actions/.github/workflows/'
            + ('cookbook-preview-effects.yml' if preview else 'cookbook-deliver.yml') + '@' + self.actions]
        if not combined:
            paths.append('nicodes/komizo-actions/.github/workflows/'
                + ('cookbook-preview-publish.yml' if preview else 'cookbook-publish.yml') + '@' + self.actions)
        return {'referenced_workflows': [{'path': path, 'sha': path.rsplit('@', 1)[1]} for path in paths]}

    def admit(self, metadata, route='host', **options):
        authorize.admit_workflows(metadata, self.cookbook, self.actions, route, **options)

    def test_complete_selected_host_native_and_godot_routes_keep_production_preview_and_combined_roles(self):
        for route in self.workflows:
            for preview in [False, True]:
                for combined in [False, True]:
                    with self.subTest(route=route, preview=preview, combined=combined):
                        self.admit(self.metadata(route, preview, combined), route,
                            preview=preview, combined=combined)

    def test_wrong_recipe_or_unselected_router_cannot_replace_the_selected_route(self):
        for route in self.workflows:
            for wrong in [*set(self.workflows.values()) - {self.workflows[route]}, 'cookbook-ci.yml']:
                changed = self.metadata(route)
                changed['referenced_workflows'][0]['path'] = 'nicodes/cookbook/.github/workflows/' + wrong + '@' + self.cookbook
                with self.subTest(route=route, wrong=wrong), self.assertRaises(ValueError):
                    self.admit(changed, route)

    def test_missing_wrong_revision_and_mixed_preview_production_providers_refuse(self):
        original = self.metadata('host')
        for index in range(len(original['referenced_workflows'])):
            for mutation in ['missing', 'sha', 'pin']:
                changed = copy.deepcopy(original)
                if mutation == 'missing':
                    changed['referenced_workflows'].pop(index)
                else:
                    entry = changed['referenced_workflows'][index]
                    if mutation == 'sha': entry['sha'] = 'e' * 40
                    else: entry['path'] = entry['path'].rsplit('@', 1)[0] + '@' + 'e' * 40
                with self.subTest(index=index, mutation=mutation), self.assertRaises(ValueError):
                    self.admit(changed)
        for preview in [False, True]:
            with self.subTest(preview=preview), self.assertRaises(ValueError):
                self.admit(self.metadata('host', not preview), preview=preview)
        for route in ['unknown', 'fast', 'cookbook-ci.yml']:
            with self.subTest(route=route), self.assertRaises(ValueError): self.admit(original, route)

    def test_the_real_accepted_sdk_model_and_generated_caller_select_the_same_authority(self):
        program = '''import json,re,sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import recipe_context
context=recipe_context.Context(Path(sys.argv[2]),sys.argv[3],Path(sys.argv[4]))
generator=context.module('generate');orchestration=context.module('orchestration')
result=[]
with tempfile.TemporaryDirectory() as directory:
 root=Path(directory);(root/'check.py').write_text('assert True\\n')
 for recipe in ['react-native','rust-cli','godot-game']:
  config={'version':1,'recipe':recipe,'tests':{'unit':[{'runner':'python','path':'check.py'}]}}
  route=orchestration.model(config)['route']
  body=generator.workflow_files(root,context.cookbook_revision,config)['.github/workflows/ci.yml']
  match=re.search(r'^    uses: (nicodes/cookbook/[^\\n]+)$',body,re.MULTILINE)
  assert match is not None
  result.append({'route':route,'workflow':match.group(1)})
print(json.dumps(result))
'''
        revision = os.environ['RECIPE_TASK_SDK_REF']
        output = subprocess.check_output([sys.executable, '-I', '-B', '-c', program,
            str(Path(__file__).resolve().parents[1] / 'scripts'), os.environ['RECIPE_TASK_SDK'],
            revision, os.environ['RECIPE_TASK_TOOLS']], text=True, timeout=30)
        for item in json.loads(output):
            metadata = self.metadata(item['route'])
            metadata['referenced_workflows'][0] = {'path':item['workflow'], 'sha':revision}
            with self.subTest(route=item['route']):
                authorize.admit_workflows(metadata, revision, self.actions, item['route'])


if __name__ == '__main__': unittest.main()
