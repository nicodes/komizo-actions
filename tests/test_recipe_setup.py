"""Compose the actual source-admission adapter with finite SDK action fixtures."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import yaml

import test_operational_recipe_context as sdk_fixture

ROOT=Path(__file__).resolve().parents[1]


class RecipeSetup(unittest.TestCase):
    def setUp(self):
        self.fixture=sdk_fixture.ExtractedRecipeAuthority();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.action=yaml.safe_load((ROOT/'recipe-setup/action.yml').read_text())
        for name,label in [('sdk-dependencies','dependencies'),('sdk-setup','exact-python-install')]:
            directory=self.fixture.cookbook/name;directory.mkdir()
            (directory/'action.yml').write_text('runs:\n  using: composite\n  steps:\n    - shell: bash\n'
                '      run: printf \'%s\\n\' '+label+' >> "$SDK_TRACE"\n')
        self.fixture.cookbook_ref=self.fixture.commit(self.fixture.cookbook)
        self.cache=self.fixture.app/'.artifacts/recipe-sdks'
        self.cache.mkdir(parents=True)
        shutil.copytree(self.fixture.cookbook,self.cache/'_cookbook')
        shutil.copytree(self.fixture.tools,self.cache/'_tools')
        binary=self.fixture.directory/'finite-api';binary.mkdir()
        gh=binary/'gh'
        gh.write_text('#!/bin/sh\nprintf \'api:%s\\n\' "$2" >> "$SDK_TRACE"\n'
            'case "$2" in\n *nicodes/tools/*) status="${TOOLS_MAIN_STATUS:-identical}";;\n'
            ' *) status=identical;;\nesac\nprintf \'{"status":"%s"}\\n\' "$status"\n')
        gh.chmod(0o755)
        self.trace=self.fixture.directory/'trace'
        self.output=self.fixture.directory/'output';self.output.write_text('')
        self.environment=dict(os.environ,PATH=str(binary)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'],
            GITHUB_ACTION_PATH=str(ROOT/'recipe-setup'),COOKBOOK_REF=self.fixture.cookbook_ref,
            SDK_TRACE=str(self.trace),GITHUB_OUTPUT=str(self.output))

    def compose(self,environment):
        for step in self.action['runs']['steps']:
            if 'run' in step:
                subprocess.run(['bash','-euc',step['run']],cwd=self.fixture.app,env=environment,
                    capture_output=True,text=True,check=True,timeout=30)
            elif step.get('uses','').startswith('actions/setup-python@'):
                # These controls use the already available supported parser.
                self.assertIn('bootstrap-python=false',self.output.read_text())
            else:
                action_path=self.fixture.app/step['uses'].removeprefix('./')/'action.yml'
                action=yaml.safe_load(action_path.read_text())
                for native in action['runs']['steps']:
                    subprocess.run(['bash','-euc',native['run']],cwd=self.fixture.app,env=environment,
                        capture_output=True,text=True,check=True,timeout=30)

    def test_actual_composed_adapter_admits_both_sources_before_any_SDK_action(self):
        self.compose(self.environment)
        lines=self.trace.read_text().splitlines()
        self.assertEqual(lines[:2],['api:repos/nicodes/cookbook/compare/'+self.fixture.cookbook_ref+'...main',
            'api:repos/nicodes/tools/compare/'+self.fixture.tools_ref+'...main'])
        self.assertEqual(lines[2:],['dependencies','exact-python-install'])
        self.assertFalse(any(self.cache.rglob('__pycache__')))

    def test_unaccepted_helper_or_dirty_Cookbook_refuses_before_any_SDK_action(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.compose(self.environment|{'TOOLS_MAIN_STATUS':'behind'})
        self.assertNotIn('dependencies',self.trace.read_text().splitlines())
        self.trace.write_text('')
        (self.cache/'_cookbook/README').write_text('unreviewed source')
        with self.assertRaises(subprocess.CalledProcessError):self.compose(self.environment)
        self.assertEqual(self.trace.read_bytes(),b'')

    def test_parser_and_native_pin_installation_remain_in_shared_checked_source_actions(self):
        steps=self.action['runs']['steps']
        self.assertEqual(steps[1]['uses'],'actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405')
        self.assertEqual(steps[1]['with'],{'python-version':'3.13.11','check-latest':False})
        self.assertEqual(steps[3]['uses'],'./.artifacts/recipe-sdks/_cookbook/sdk-dependencies')
        self.assertEqual(steps[4]['uses'],'./.artifacts/recipe-sdks/_cookbook/sdk-setup')
        self.assertEqual(steps[4]['with'],{'tools':'${{ inputs.tools }}'})
        self.assertFalse(any('nicodes/tools/setup@' in step.get('uses','') for step in steps))
        self.assertFalse(any('make' in step.get('run','').lower() for step in steps))


if __name__=='__main__':unittest.main()
