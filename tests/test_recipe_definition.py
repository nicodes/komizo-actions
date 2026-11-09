"""Real prepared SDKs retain effective-definition and publication admission."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OLD = '14dd4792df910b8f72ae10bf01c7e426b97b667e'


class EffectiveDefinition(unittest.TestCase):
    def setUp(self):
        self.strict = Path(os.environ['RECIPE_TASK_SDK']).resolve()
        self.tools = Path(os.environ['RECIPE_TASK_TOOLS']).resolve()
        self.reference = os.environ['RECIPE_TASK_SDK_REF']
        self.legacy = Path(os.environ['RECIPE_TEST_SDK']).resolve()
        self.assertRegex(self.reference, r'^[a-f0-9]{40}$')
        self.assertEqual(self.commit(self.strict), self.reference)
        self.assertEqual(self.commit(self.legacy), OLD)
        temporary = tempfile.TemporaryDirectory(prefix='definition source ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root/'tests').mkdir()
        (self.root/'tests/native.py').write_text('raise AssertionError("declaration readers must not execute native programs")\n')
        (self.root/'deploy').mkdir()
        self.body = 'version=1\nrecipe="react-native"\n[ci]\ntimeout_minutes=90\n[[tests.unit]]\nrunner="python"\npath="tests/native.py"\ntimeout=7\n'
        (self.root/'cookbook.toml').write_text(self.body)
        (self.root/'deploy/release.toml').write_text('version=1\nproject="demo"\nimage_base="ghcr.io/example/demo"\n'
            +'tests=["lint","unit","security"]\n[inputs]\nartifacts=["public"]\nimages=[]\nenvironment=[]\nrevision_environment=[]\n')

    def commit(self, root):
        return subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()

    def run_control(self, code, *args):
        prefix = '''import importlib.util,json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);import recipe_data,recipe_context
root,tools,legacy=map(Path,sys.argv[2:5])
recipe_context.Context(tools,sys.argv[5],Path(sys.argv[6])).activate()
'''
        result = subprocess.run([sys.executable,'-I','-B','-c',prefix+code,
            str(ROOT/'scripts'),str(self.root),str(self.strict),str(self.legacy),self.reference,str(self.tools),*args],
            capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_strict_sdk_derives_the_actual_source_file_without_injecting_retired_fields(self):
        actual = self.run_control('''config=recipe_data.load(root,tools)
before=json.dumps(config,sort_keys=True)
name=recipe_data.effective_definition(tools,config)
assert json.dumps(config,sort_keys=True)==before
assert 'definition' not in config.get('artifacts',{})
assert (root/name).is_file()
print(json.dumps({'name':name,'ci':config['ci'],'tests':config['tests']}))
''')
        self.assertEqual(actual['name'],'deploy/release.toml')
        self.assertEqual(actual['ci'],{'runner':'ubuntu-24.04','timeout_minutes':90})
        self.assertEqual(actual['tests']['unit'][0]['timeout'],7)
        self.assertEqual((self.root/'cookbook.toml').read_text(),self.body)

    def test_obsolete_explicit_definition_contract_is_refused_without_source_mutation(self):
        body=self.body+'[artifacts]\ndefinition="deploy/release.json"\n'
        (self.root/'cookbook.toml').write_text(body)
        actual=self.run_control('''refused=0
for action in [lambda:recipe_data.load(root,tools),lambda:recipe_data.load(root,legacy),lambda:recipe_data.effective_definition(legacy,{'artifacts':{'definition':'deploy/release.json'}})]:
 try:action()
 except ValueError:refused+=1
 else:raise AssertionError('retired explicit definition or SDK was admitted')
print(json.dumps({'refused':refused}))
''')
        self.assertEqual(actual,{'refused':3})
        self.assertEqual((self.root/'cookbook.toml').read_text(),body)

    def test_broken_current_api_and_foreign_cached_sdk_cannot_fall_back_to_old_source_keys(self):
        actual = self.run_control('''config=recipe_data.load(root,tools)
import configuration
config['artifacts']={'definition':'deploy/legacy-trap.json'}
refused=0
for value in [None,'../outside.json','/outside.json','deploy/one\\nother.json']:
 configuration.release_definition=(lambda _config,value=value:value)
 try: recipe_data.effective_definition(tools,config)
 except ValueError: refused+=1
 else: raise AssertionError('broken current SDK fell back to legacy declaration')
configuration.release_definition=None
try: recipe_data.effective_definition(tools,config)
except ValueError: refused+=1
else: raise AssertionError('noncallable current API was admitted')
# A supplied already-loaded SDK peer is checked against this exact checkout.
try: recipe_data.effective_definition(legacy,config,configuration)
except ValueError as error:
 assert 'another cookbook SDK' in str(error)
 refused+=1
else: raise AssertionError('foreign cached SDK supplied an authority path')
print(json.dumps({'refusals':refused}))
''')
        self.assertEqual(actual['refusals'],6)

    def test_publisher_cannot_override_definition_or_select_historical_tools_recipe(self):
        script=Path(__file__).resolve().parents[1]/'scripts/application-publication.py'
        for extra in [['--definition','untrusted.toml'],['--tools','untrusted','--tools-ref','a'*40]]:
            arguments=[sys.executable,'-I','-B',str(script),'--source','a'*40,
                '--cookbook-root','untrusted','--cookbook-ref','b'*40,'--tools-root','untrusted','--project','demo',*extra]
            result=subprocess.run(arguments,capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,2)
            self.assertIn('unrecognized arguments',result.stderr)


if __name__=='__main__':unittest.main()
