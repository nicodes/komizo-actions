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
        self.strict = Path(os.environ['RECIPE_CONTRACT_TEST_SDK']).resolve()
        self.reference = os.environ['RECIPE_CONTRACT_TEST_SDK_REF']
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
        definition = {'version':1,'project':'demo','image_base':'ghcr.io/example/demo',
            'inputs':{'artifacts':['public'], 'images':['ghcr.io/example/demo-gate:{revision}'],
                      'environment':[], 'revision_environment':[]}, 'tests':['lint','unit','security']}
        (self.root/'deploy/release.json').write_text(json.dumps(definition))

    def commit(self, root):
        return subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()

    def run_control(self, code, *args):
        prefix = '''import importlib.util,json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);import recipe_data
root,tools,legacy=map(Path,sys.argv[2:5])
'''
        result = subprocess.run([sys.executable,'-I','-B','-c',prefix+code,
            str(ROOT/'scripts'),str(self.root),str(self.strict),str(self.legacy),self.reference,*args],
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
        self.assertEqual(actual['name'],'deploy/release.json')
        self.assertEqual(actual['ci'],{'runner':'ubuntu-24.04','timeout_minutes':90})
        self.assertEqual(actual['tests']['unit'][0]['timeout'],7)
        self.assertEqual((self.root/'cookbook.toml').read_text(),self.body)

    def test_real_historical_sdk_requires_its_original_explicit_definition(self):
        body = self.body+'[artifacts]\ndefinition="deploy/release.json"\n'
        (self.root/'cookbook.toml').write_text(body)
        actual = self.run_control('''config=recipe_data.load(root,legacy)
name=recipe_data.effective_definition(legacy,config)
config['artifacts'].pop('definition')
try: recipe_data.effective_definition(legacy,config)
except ValueError: refused=True
else: raise AssertionError('historical SDK silently acquired a new default')
print(json.dumps({'name':name,'missing_explicit_refused':refused}))
''')
        self.assertEqual(actual,{'name':'deploy/release.json','missing_explicit_refused':True})
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
