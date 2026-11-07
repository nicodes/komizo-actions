"""Source-only static admission controls with distinct real Git fixture roots."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import yaml
import test_operational_recipe_context as fixture

ROOT=Path(__file__).resolve().parents[1]


class StaticRecipeContext(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.ExtractedRecipeAuthority();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        helpers=f.tools/'helpers';helpers.mkdir()
        (helpers/'verification.py').write_text('import tomllib\n'
            'def release_definition(path): return tomllib.loads(path.read_text())\n')
        (helpers/'release.py').write_text('def validate(*args): return {"gate":"accepted"}\n'
            'def validate_provenance(*args): pass\n'
            'def validate_test_evidence(*args): pass\n')
        f.tools_ref=f.commit(f.tools)
        (f.cookbook/'mise.toml').write_text('[vars]\ntools_ref="'+f.tools_ref+'"\n')
        modules=f.cookbook/'cookbook'
        (modules/'dependencies.py').write_text('from pathlib import Path\n'
            'def tools_identity(): return '+repr({'repository':'nicodes/tools','revision':f.tools_ref})+'\n'
            'def tools_root(supplied=None): return Path(supplied)\n')
        (modules/'godot_static.py').write_text('CONFIG_ARTIFACT=".artifacts/release/static-config.json"\n'
            'MANIFEST_ARTIFACT=".artifacts/release/static-manifest.json"\n'
            'def declaration(root,config): return {"repository":"example/application"}\n'
            'def verify_blueprint(root,config,environment): return {"source_commit":environment["SOURCE_COMMIT"]}\n'
            'def read(path): return path.read_text()\n')
        (modules/'delivery_workflows.py').write_text('def declaration(root,config): return {"mode":"godot-static"}\n')
        (modules/'godot_ci.py').write_text('import dependencies\n'
            'def validate_acceptance(matrix,config,jobs,source,revision,environment,canonical,**kwargs):\n'
            ' expected={"cookbook_repository":"nicodes/cookbook","cookbook_commit":revision,\n'
            '  "tools_repository":"nicodes/tools","tools_commit":kwargs["tools_revision"],"entrypoint_contract":"mise-v1"}\n'
            ' if matrix!=expected or kwargs["root"].name!="application" or kwargs["host"]["godot_matrix"]!=matrix:\n'
            '  raise ValueError("unit fixture matrix identity differs")\n'
            ' return ["unit fixture only"]\n')
        f.cookbook_ref=f.commit(f.cookbook)
        (f.app/'mise.toml').write_text('[vars]\ncookbook_ref="'+f.cookbook_ref+'"\n')
        (f.app/'cookbook.toml').write_text('version=1\nrecipe="godot-game"\n'
            '[delivery]\ndefinition="deploy/delivery.toml"\n'
            '[ci]\nrunner="ubuntu-24.04"\n[artifacts]\nrelease_metadata=[]\n')
        (f.app/'deploy/release.toml').write_text('project="application"\nimage_base="ghcr.io/example/application"\n'
            'tests=["lint","unit","security"]\n[inputs]\nimages=["ghcr.io/example/application-gate:{revision}"]\n')
        f.source=f.commit(f.app)
        self.incoming=f.directory/'incoming'
        self.config_hash=hashlib.sha256((f.app/'cookbook.toml').read_bytes()).hexdigest()
        binding={'cookbook_repository':'nicodes/cookbook','cookbook_commit':f.cookbook_ref,
            'tools_repository':'nicodes/tools','tools_commit':f.tools_ref,'entrypoint_contract':'mise-v1'}
        self.environment=dict(GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='push',GITHUB_SHA=f.source,
            GITHUB_WORKFLOW_SHA=f.source,GITHUB_SERVER_URL='https://github.com',GITHUB_REPOSITORY='example/application',
            GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',GITHUB_WORKFLOW_REF='example/application/.github/workflows/cd.yml@refs/heads/main',
            COOKBOOK_REF=f.cookbook_ref,TOOLS_REF='')
        proof=dict(version=1,source_commit=f.source,source_tree=f.git(f.app,'rev-parse','HEAD^{tree}'),
            recipe='godot-game',status='passed',tracked_source_clean=True,build_verified=True,tests_verified=True,
            test_groups=['lint','unit','security'],coverage='complete-host-and-godot-matrix',configuration_sha256=self.config_hash,
            godot_matrix=binding,**binding)
        proof['execution_context']={key:value for key,value in self.environment.items() if key.startswith('GITHUB_') and key!='GITHUB_REF'}|{'GITHUB_JOB':'validate'}
        release=self.incoming/'.artifacts/release';release.mkdir(parents=True)
        for name,body in {'images.tar.gz':b'unit fixture opaque archive', 'images.tar.gz.sha256':b'unit checksum\n',
            'images.json':b'{}', 'release.json':b'{"images":{}}', 'static-config.json':b'{}',
            'static-manifest.json':json.dumps({'source_commit':f.source,'release_id':'b'*64}).encode()}.items():
            (release/name).write_bytes(body)
        proof['artifacts']={'.artifacts/release/'+path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in release.iterdir()}
        (self.incoming/'.artifacts/cookbook-validation.json').write_text(json.dumps(proof))
        self.proof=proof

    def control(self,code):
        f=self.fixture
        prefix='''import importlib.util,json,os,sys
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('controlled_static_publisher',Path(sys.argv[1])/'godot-static-delivery/publish.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
app,cookbook,tools,incoming=map(Path,sys.argv[2:6]);source,cookie,tool=sys.argv[6:9]
environment=json.loads(sys.argv[9]);os.chdir(app)
queries=[]
def lookup(path):
 queries.append(path)
 return {'status':'identical'}
'''
        result=subprocess.run([sys.executable,'-I','-c',prefix+code,str(ROOT),str(f.app),str(f.cookbook),
            str(f.tools),str(self.incoming),f.source,f.cookbook_ref,f.tools_ref,json.dumps(self.environment)],
            capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout

    def test_modern_cli_supplies_both_real_sources_and_bound_environment_before_admission(self):
        self.control('''class Reached(RuntimeError):pass
original=p.subprocess.check_output
def admitted(root,provider,revision_source,revision,packet,env,*,context):
 assert provider==cookbook and context.tools==tools and revision==cookie
 assert env['COOKBOOK_ROOT']==str(cookbook) and env['COOKBOOK_REF']==cookie
 assert env['TOOLS_ROOT']==str(tools) and env['TOOLS_REF']==tool
 raise Reached()
with patch.dict(os.environ,environment,clear=True),patch.object(sys,'argv',['publish.py','--cookbook-root',str(cookbook),
 '--cookbook-ref',cookie,'--tools-root',str(tools),'--source',source]),patch.object(p,'admit',side_effect=admitted),\
 patch.object(p.recipe_context,'accepted_context',wraps=p.recipe_context.accepted_context),\
 patch.object(p.subprocess,'check_output',side_effect=lambda args,**kwargs:json.dumps(lookup(args[2])).encode() if args[:2]==['gh','api'] else original(args,**kwargs)):
 try:p.main()
 except Reached:pass
 else:raise AssertionError('the real typed CLI did not reach admission')
assert queries==['repos/nicodes/cookbook/compare/'+cookie+'...main','repos/nicodes/tools/compare/'+tool+'...main']
''')

    def test_modern_admission_checks_closed_archive_and_full_native_guards_before_Docker(self):
        self.control('''context=p.recipe_context.accepted_context(cookbook,cookie,lookup,tools)
environment=context.environment(environment)
class Reached(RuntimeError):pass
execute=p.execute
def no_effect(argv,*args):
 if argv[:2]==['docker','load']:raise Reached('all source/native/archive guards passed before Docker')
 return execute(argv,*args)
def capture(argv,*args):
 assert argv[:2]==['gh','api']
 return json.dumps({'total_count':1,'jobs':[{'name':'Godot host / Make full','conclusion':'success',
  'head_sha':source,'labels':['ubuntu-24.04']}]})
with patch.object(p,'execute',side_effect=no_effect),patch.object(p,'capture',side_effect=capture):
 try:p.admit(app,cookbook,source,cookie,incoming,environment,context=context)
 except Reached:pass
 else:raise AssertionError('the controlled Docker boundary was not reached')
assert not list(cookbook.rglob('__pycache__')) and not list(tools.rglob('__pycache__'))
''')

    def test_changed_archive_or_matrix_alias_refuses_before_Docker(self):
        for change in ['archive','matrix','proof','runner']:
            with self.subTest(change=change):
                self.control('''context=p.recipe_context.accepted_context(cookbook,cookie,lookup,tools)
environment=context.environment(environment)
change='''+repr(change)+'''
proof_path=incoming/'.artifacts/cookbook-validation.json';proof=json.loads(proof_path.read_bytes())
if change=='archive':(incoming/'.artifacts/release/images.tar.gz').write_bytes(b'changed archive')
elif change=='matrix':proof['godot_matrix']['tools_commit']=cookie
elif change=='proof':proof['tools_commit']=cookie
proof_path.write_text(json.dumps(proof))
execute=p.execute
def no_effect(argv,*args):
 assert argv[0]=='git','refused data reached a native effect'
 return execute(argv,*args)
with patch.object(p,'execute',side_effect=no_effect),patch.object(p,'capture',return_value=json.dumps({'total_count':1,
 'jobs':[{'name':'Godot host / Make full','conclusion':'success','head_sha':source,
 'labels':['foreign-runner'] if change=='runner' else ['ubuntu-24.04']}]})):
 try:p.admit(app,cookbook,source,cookie,incoming,environment,context=context)
 except ValueError:pass
 else:raise AssertionError('changed accepted data was admitted')
''')
                # Each counterfactual keeps its independent original archive/proof.
                (self.incoming/'.artifacts/release/images.tar.gz').write_bytes(b'unit fixture opaque archive')
                (self.incoming/'.artifacts/cookbook-validation.json').write_text(json.dumps(self.proof))

    def test_unaccepted_or_foreign_helper_refuses_before_recipe_imports(self):
        self.control('''for accepted,location in [(False,tools),(True,cookbook)]:
 def guarded(path):return {'status':'behind' if '/nicodes/tools/' in path and not accepted else 'identical'}
 try:p.recipe_context.accepted_context(cookbook,cookie,guarded,location)
 except ValueError:pass
 else:raise AssertionError('unaccepted or aliased helper admitted')
assert 'configuration' not in sys.modules and 'godot_static' not in sys.modules
''')

    def test_mixed_or_partial_CLI_identity_refuses_before_admission(self):
        self.control('''for sdk in [['--cookbook-root',str(cookbook)],['--cookbook-root',str(cookbook),'--cookbook-ref',cookie,
 '--tools',str(tools),'--tools-ref',tool],['--tools-root',str(tools)]]:
 with patch.dict(os.environ,environment,clear=True),patch.object(sys,'argv',['publish.py',*sdk,'--source',source]),\
  patch.object(p,'admit',side_effect=AssertionError('invalid selector reached admission')):
  try:p.main()
  except SystemExit as error:assert error.code==1
  else:raise AssertionError('partial or mixed CLI identity was admitted')
''')

    def test_mismatched_exported_identity_refuses_before_admission(self):
        self.control('''original=p.subprocess.check_output
for changes in [{'TOOLS_REF':cookie},{'COOKBOOK_REF':tool}]:
 with patch.dict(os.environ,environment|changes,clear=True),patch.object(sys,'argv',['publish.py',
  '--cookbook-root',str(cookbook),'--cookbook-ref',cookie,'--tools-root',str(tools),'--source',source]),\
  patch.object(p,'admit',side_effect=AssertionError('mismatched SDK metadata reached admission')),\
  patch.object(p.subprocess,'check_output',side_effect=lambda args,**kwargs:json.dumps(lookup(args[2])).encode() if args[:2]==['gh','api'] else original(args,**kwargs)):
  try:p.main()
  except SystemExit as error:assert error.code==1
  else:raise AssertionError('mismatched exported recipe identity was admitted')
''')

    def test_action_preserves_historical_effect_steps_and_uses_common_modern_preparation(self):
        action=yaml.safe_load((ROOT/'godot-static-delivery/action.yml').read_text())
        steps=action['runs']['steps']
        for name,pin in {'Normalize the original static gate artifact identity':'nicodes/komizo-actions/release-evidence@6aebd7bde3aa5a68efd9c79df0cee3a29beb1be8',
            'Activate the accepted source through the existing host policy':'nicodes/komizo-actions/deploy@6aebd7bde3aa5a68efd9c79df0cee3a29beb1be8',
            'Retain the original successful-CD immutable release record':'actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9'}.items():
            self.assertEqual(next(step for step in steps if step.get('name')==name)['uses'],pin)
        legacy=next(step for step in steps if step.get('uses')=='./_godot_static_cookbook/setup')
        self.assertEqual(legacy['if'],"${{ inputs.cookbook-ref == '' }}")
        selected=[step for step in steps if step.get('uses','').startswith('./.artifacts/recipe-sdks/')]
        self.assertEqual([step['uses'] for step in selected],['./.artifacts/recipe-sdks/_cookbook/sdk-dependencies',
            './.artifacts/recipe-sdks/_cookbook/sdk-setup'])
        self.assertEqual(selected[-1]['with'],{'tools':'python'})
        admission=next(index for index,step in enumerate(steps) if 'recipe_context.py' in step.get('run',''))
        self.assertLess(admission,steps.index(selected[0]))
        self.assertFalse(any('nicodes/tools/setup@' in step.get('uses','') for step in steps))


if __name__=='__main__':unittest.main()
