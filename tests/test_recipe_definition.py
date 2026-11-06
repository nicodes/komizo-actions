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

    def test_real_publisher_consumes_derived_definition_and_retains_source_and_raw_hash_refusals(self):
        actual = self.run_control('''from unittest.mock import patch
from types import SimpleNamespace
import hashlib,subprocess
os.chdir(root)
subprocess.run(['git','init','-q'],check=True)
subprocess.run(['git','add','cookbook.toml','deploy/release.json','tests/native.py'],check=True)
spec=importlib.util.spec_from_file_location('actual_publisher',Path(sys.argv[1])/'application-publication.py')
publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(publisher)
source,tree,reference='a'*40,'b'*40,sys.argv[5]
environment=dict(GITHUB_SERVER_URL='https://github.com',GITHUB_REPOSITORY='owner/demo',GITHUB_RUN_ID='123',
 GITHUB_RUN_ATTEMPT='2',GITHUB_SHA=source,GITHUB_WORKFLOW_SHA=source,GITHUB_EVENT_NAME='push',
 GITHUB_REF='refs/heads/main',GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/cd.yml@refs/heads/main')
incoming=root/'incoming';(incoming/'.artifacts').mkdir(parents=True)
proof=dict(version=1,source_commit=source,source_tree=tree,cookbook_commit=reference,recipe='react-native',
 status='passed',tracked_source_clean=True,build_verified=True,tests_verified=True,
 test_groups=['lint','unit','security'],configuration_sha256=hashlib.sha256((root/'cookbook.toml').read_bytes()).hexdigest(),
 execution_context=environment|{'GITHUB_JOB':'validate'})
for name in ['images.tar.gz','images.tar.gz.sha256','images.json','release.json']:
 path=incoming/'.artifacts/release'/name;path.parent.mkdir(exist_ok=True);path.write_bytes(b'{}')
proof['artifacts']={'.artifacts/release/'+p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (incoming/'.artifacts/release').iterdir()}
proof_path=incoming/'.artifacts/cookbook-validation.json';proof_path.write_text(json.dumps(proof))
jobs={'total_count':1,'jobs':[{'name':'validate / Full source checks / Make full','head_sha':source,'conclusion':'success','labels':['ubuntu-24.04']}]}
real_output,real_run=subprocess.check_output,subprocess.run;published=[]
def output(command,*args,**kwargs):
 if command[:2]==['gh','api']:return json.dumps(jobs).encode()
 return real_output(command,*args,**kwargs)
def execute(command,*args,**kwargs):
 if command[:2]==['git','diff']:return SimpleNamespace(returncode=0)
 if command[0]=='git':return real_run(command,*args,**kwargs)
 assert command[2]=='publish',command
 published.append(command);return SimpleNamespace(returncode=0)
def git(*args):
 if args[-1]=='HEAD^{tree}':return tree
 return reference if args[0]=='-C' else source
arguments=['publisher','--source',source,'--tools',str(tools),'--tools-ref',reference,'--project','demo','--incoming',str(incoming)]
with patch.dict(os.environ,environment,clear=True),patch.object(sys,'argv',arguments),patch.object(publisher,'git',side_effect=git),patch.object(publisher.subprocess,'check_output',side_effect=output),patch.object(publisher.subprocess,'run',side_effect=execute):
 publisher.main()
 assert published[0][published[0].index('--definition')+1]=='deploy/release.json'
 assert 'definition' not in recipe_data.load(root,tools).get('artifacts',{})
 # Native same-source admission still blocks changed provenance before publication.
 proof['source_tree']='c'*40;proof_path.write_text(json.dumps(proof))
 try: publisher.main()
 except SystemExit as error:assert error.code==1
 else:raise AssertionError('wrong native source tree reached publication')
 assert len(published)==1
print(json.dumps({'published_with_derived_definition':True,'changed_tree_refused':True}))
''')
        self.assertEqual(actual,{'published_with_derived_definition':True,'changed_tree_refused':True})


if __name__=='__main__':unittest.main()
