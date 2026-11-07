"""Real, distinct source checkouts bind operational and historical adapters."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ExtractedRecipeAuthority(unittest.TestCase):
    def git(self, root, *arguments):
        return subprocess.check_output(['git', '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false',
            '-C', str(root), *arguments], text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, root):
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-qm', 'Reviewed fixture source')
        return self.git(root, 'rev-parse', 'HEAD')

    def repository(self, name):
        path = self.directory/name
        path.mkdir()
        self.git(path, 'init', '-q', '--initial-branch=main')
        self.git(path, 'remote', 'add', 'origin', 'https://github.com/nicodes/'+name+'.git')
        (path/'README').write_text(name+'\n')
        return path

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='actual separate operational SDKs ')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.tools = self.repository('tools')
        self.tools_ref = self.commit(self.tools)
        self.actions = self.repository('komizo-actions')
        self.actions_ref = self.commit(self.actions)
        self.cookbook = self.repository('cookbook')
        (self.cookbook/'mise.toml').write_text('[vars]\ntools_ref="'+self.tools_ref+'"\n')
        modules = self.cookbook/'cookbook'
        modules.mkdir()
        (modules/'dependencies.py').write_text('from pathlib import Path\n'
            'def tools_identity(): return '+repr({'repository':'nicodes/tools','revision':self.tools_ref})+'\n'
            'def tools_root(supplied=None): return Path(supplied)\n')
        (modules/'configuration.py').write_text('import tomllib\n'
            'def load(root): return tomllib.loads((root/"cookbook.toml").read_text())\n'
            'def ci_settings(config): return {}\n'
            'def release_definition(config): return "deploy/release.toml"\n'
            'def parse_source_data(raw,maximum): return tomllib.loads(raw)\n')
        (modules/'operational_workflows.py').write_text('import tomllib\n'
            'def declaration(root,config): return tomllib.loads((root/"deploy/operations.toml").read_text())\n'
            'def workflow_files(root,pin,config): return {".github/workflows/security-scan.yml":"canonical "+pin+"\\n"}\n'
            'def step_timeouts(operation,record): return {"checks":3}\n')
        (modules/'delivery_workflows.py').write_text('import tomllib\n'
            'def declaration(root,config): return tomllib.loads((root/"deploy/delivery.toml").read_text())\n'
            'def historical_deadlines(data): return {"deploy":30}\n')
        (modules/'generate.py').write_text('import tomllib\n'
            'def workflow_files(root,pin): return {".github/workflows/cd.yml":"canonical "+pin+"\\n"}\n'
            'def check_entrypoint(root,pin):\n'
            ' if tomllib.loads((root/"mise.toml").read_text())["vars"]["cookbook_ref"]!=pin: raise ValueError("wrong Cookbook task source")\n'
            'def entrypoint_files(root,pin): return ["mise.toml"]\n')
        for name in ['node_component','images']:(modules/(name+'.py')).write_text('# Deliberately no product behavior.\n')
        self.cookbook_ref = self.commit(self.cookbook)
        self.assertNotEqual(self.cookbook_ref,self.tools_ref)
        self.app = self.repository('application')
        self.version = self.commit(self.app)
        (self.app/'deploy').mkdir()
        (self.app/'.github/workflows').mkdir(parents=True)
        (self.app/'cookbook.toml').write_text('version=1\nrecipe="react-native"\n'
            '[delivery]\ndefinition="deploy/delivery.toml"\n'
            '[operations]\ndefinition="deploy/operations.toml"\n')
        (self.app/'mise.toml').write_text('[vars]\ncookbook_ref="'+self.cookbook_ref+'"\n')
        for name in ['security-scan','cd']:
            (self.app/'.github/workflows'/f'{name}.yml').write_text('canonical '+self.cookbook_ref+'\n')
        (self.app/'deploy/operations.toml').write_text('definition="deploy/operations.toml"\n'
            'project="application"\nkomizo_ref="'+self.actions_ref+'"\n'
            '[security]\nrunner="ubuntu-24.04"\ntimeout_minutes=20\nreport_timeout=5\n'
            '[production]\nrunner="ubuntu-24.04"\ntimeout_minutes=30\n'
            'checks=[{runner="python",path="deploy/health.py",timeout=180}]\n')
        (self.app/'deploy/delivery.toml').write_text('version=1\nproject="application"\n'
            'komizo_ref="'+self.actions_ref+'"\nhistorical_deploy=true\n'
            'service_env_profile="database-v2"\nexpected_generation="'+'a'*32+'"\n'
            'deploy_runner="ubuntu-24.04"\ndeploy_timeout=30\nreadiness_operation=true\n'
            'health_urls=[{url="https://app.example/health",contains="{revision}"}]\n')
        (self.app/'deploy/release.toml').write_text('version=1\nproject="application"\n'
            'image_base="ghcr.io/example/application"\n[inputs]\n'
            'images=["ghcr.io/example/application-api:{revision}"]\n')
        self.source = self.commit(self.app)
        self.event = self.directory/'dispatch.json'
        self.event.write_text(json.dumps({'inputs':{'version':self.version}}))

    def control(self, code):
        prefix = '''import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);import operational,historical_delivery
app,cookbook,tools,actions,event=map(Path,sys.argv[2:7])
source,cookie,tool,komizo,version=sys.argv[7:]
operational.ROOT=actions
env=dict(GITHUB_REPOSITORY='example/application',GITHUB_SHA=source,GITHUB_WORKFLOW_SHA=source,
 GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',
 GITHUB_WORKFLOW_REF='example/application/.github/workflows/security-scan.yml@refs/heads/main',
 GITHUB_EVENT_PATH=str(event),COOKBOOK_REF=cookie,TOOLS_REF='',KOMIZO_REF=komizo,
 DECLARATION='deploy/operations.toml',OPERATION_RUNNER='ubuntu-24.04',OPERATION_TIMEOUT='20',
 OPERATION_ENVIRONMENT='',OPERATION_REPORT_TIMEOUT='5',OPERATION_STEP_TIMEOUTS='{"checks":3}')
queries=[]
def lookup(path):
 queries.append(path)
 if '/git/ref/' in path:return json.dumps({'object':{'sha':source}})
 selected=path.split('/compare/')[1].split('...')[0]
 return json.dumps({'status':'ahead','base_commit':{'sha':selected},'merge_base_commit':{'sha':selected}})
'''
        result=subprocess.run([sys.executable,'-I','-B','-c',prefix+code,str(ROOT/'scripts'),
            *map(str,[self.app,self.cookbook,self.tools,self.actions,self.event]),
            self.source,self.cookbook_ref,self.tools_ref,self.actions_ref,self.version],
            capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_operational_source_uses_Cookbook_pin_and_actual_Tools_runtime(self):
        result=self.control('''config,data,record,context=operational.authority(app,cookbook,'security','',env,lookup,supplied_tools=tools,return_context=True)
assert record['timeout_minutes']==20
assert 'repos/nicodes/cookbook/compare/'+cookie+'...main' in queries
assert 'repos/nicodes/tools/compare/'+tool+'...main' in queries
runtime=operational.runtime_environment(app,tools,env,context)
assert runtime['COOKBOOK_ROOT']==str(cookbook) and runtime['COOKBOOK_REF']==cookie
assert runtime['TOOLS_ROOT']==str(tools) and runtime['TOOLS_REF']==tool
operational.authority(app,cookbook,'security','',env|{'TOOLS_REF':tool},lookup,supplied_tools=tools)
print(json.dumps(context.identities()))
''')
        self.assertEqual(result['cookbook_commit'],self.cookbook_ref)
        self.assertEqual(result['tools_commit'],self.tools_ref)

    def test_historical_plan_binds_both_sources_without_fabricating_full_acceptance(self):
        result=self.control('''env.update(GITHUB_WORKFLOW_REF='example/application/.github/workflows/cd.yml@refs/heads/main',
 HISTORICAL_VERSION=version,DECLARATION='deploy/delivery.toml',HISTORICAL_RUNNER='ubuntu-24.04',
 HISTORICAL_TIMEOUT='30',HISTORICAL_ENVIRONMENT='production',HISTORICAL_STEP_TIMEOUTS='{"deploy":30}')
plan=historical_delivery.authority(app,cookbook,actions,env|{'TOOLS_REF':tool},lookup,supplied_tools=tools)
assert plan['version']==version and plan['source_commit']==source and plan['full_source_acceptance'] is False
assert plan['entrypoint_contract']=='mise-v1' and plan['tools_ref']==tool
print(json.dumps(plan))
''')
        self.assertEqual(result['cookbook_repository'],'nicodes/cookbook')
        self.assertEqual(result['tools_repository'],'nicodes/tools')
        self.assertEqual(result['cookbook_commit'],self.cookbook_ref)
        self.assertEqual(result['tools_commit'],self.tools_ref)

    def test_alias_mixed_contracts_and_unaccepted_helpers_refuse_before_recipe_import(self):
        self.control('''for changes,provider in [({'TOOLS_REF':cookie},cookbook),({'COOKBOOK_REF':tool},tools)]:
 try:operational.authority(app,provider,'security','',env|changes,lookup,supplied_tools=tools)
 except ValueError:pass
 else:raise AssertionError('aliased or mixed recipe source was admitted')
def rejected(path):return json.dumps({'status':'behind'}) if '/nicodes/tools/' in path else lookup(path)
try:operational.authority(app,cookbook,'security','',env,rejected,supplied_tools=tools)
except ValueError:pass
else:raise AssertionError('unaccepted actual helper source admitted')
assert 'configuration' not in sys.modules
print(json.dumps({'refused':True}))
''')

    def test_foreign_Tools_location_cannot_replace_provider_dependency(self):
        foreign=self.repository('foreign')
        self.commit(foreign)
        self.control('''try:operational.authority(app,cookbook,'security','',env,lookup,supplied_tools=tools.parent/'foreign')
except ValueError:pass
else:raise AssertionError('foreign supplied helper location admitted')
print(json.dumps({'refused':True}))
''')

    def test_delivery_plan_and_readiness_keep_recipe_and_helper_locations_distinct(self):
        self.control('''import importlib.util
from types import SimpleNamespace
import application_readiness
context=operational.recipe_context.accepted_context(cookbook,cookie,lambda path:json.loads(lookup(path)),tools)
path=Path(sys.argv[1]).parent/'application-delivery/plan.py'
spec=importlib.util.spec_from_file_location('extracted_delivery_plan',path)
planner=importlib.util.module_from_spec(spec);spec.loader.exec_module(planner)
os.chdir(app)
os.environ.update(env)
values=planner.plan(app,'application','deploy/delivery.toml',context=context)
assert values['health-urls']=='https://app.example/health '+source and values['backup']==''
calls=[]
def prepare(root,provider,config,record,role,environment,**kwargs):
 assert provider==cookbook and kwargs['helpers']==tools and kwargs['context'] is context
 assert environment['COOKBOOK_REF']==cookie and environment['TOOLS_REF']==tool
 calls.append('prepare')
driver=SimpleNamespace(runtime_environment=operational.runtime_environment,prepare=prepare,
 native=lambda root,check,environment:calls.append(check['path']))
application_readiness.readiness(app,cookbook,env|{'DECLARATION':'deploy/delivery.toml'},driver,context=context)
assert calls==['prepare','deploy/health.py']
print(json.dumps({'distinct_locations':True,'unchanged_readiness_selection':True}))
''')

    def test_public_and_recovery_drivers_keep_checked_sources_clean_without_python_B(self):
        script='''import importlib.util,sys
from pathlib import Path
assert not sys.dont_write_bytecode
driver_path,cookbook,tools=map(Path,sys.argv[1:4]);reference=sys.argv[4]
spec=importlib.util.spec_from_file_location('native_adapter',driver_path)
driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)
assert sys.dont_write_bytecode
context=driver.recipe_context.accepted_context(cookbook,reference,lambda _: {'status':'identical'},tools)
context.module('configuration')
assert not list(cookbook.rglob('__pycache__'))
driver.recipe_context.checked_checkout(cookbook,'nicodes/cookbook',reference)
print('Exact source remains clean without -B or ambient bytecode policy')
'''
        for relative in ['godot-production/production.py','godot-static-recovery/recovery.py']:
            result=subprocess.run([sys.executable,'-I','-c',script,str(ROOT/relative),str(self.cookbook),
                str(self.tools),self.cookbook_ref],capture_output=True,text=True,timeout=30,
                env={key:value for key,value in os.environ.items() if key!='PYTHONDONTWRITEBYTECODE'})
            with self.subTest(driver=relative):self.assertEqual(result.returncode,0,result.stderr)

    def test_prepared_delivery_uses_supplied_verified_locations_and_refuses_false_bindings(self):
        import yaml
        action=yaml.safe_load((ROOT/'application-delivery/action.yml').read_text())
        plan=next(step for step in action['runs']['steps'] if step.get('id')=='plan')
        binary=self.directory/'finite-api'
        binary.mkdir()
        gh=binary/'gh'
        gh.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"status":"identical"}\'\n')
        gh.chmod(0o755)
        output=self.directory/'outputs'
        github_env=self.directory/'environment'
        environment=dict(os.environ,PATH=str(binary)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'],
            GITHUB_ACTION_PATH=str(ROOT/'application-delivery'),PROJECT='application',DECLARATION='deploy/delivery.toml',
            GITHUB_SHA=self.source,SDK_PREPARED='true',COOKBOOK_ROOT=str(self.cookbook),COOKBOOK_REF=self.cookbook_ref,
            TOOLS_ROOT=str(self.tools),TOOLS_REF=self.tools_ref,DERIVED_TOOLS_REF=self.tools_ref,
            GITHUB_OUTPUT=str(output),GITHUB_ENV=str(github_env))
        def invoke(changes):
            output.write_text('');github_env.write_text('')
            return subprocess.run(['bash','-euc',plan['run']],cwd=self.app,env=environment|changes,
                capture_output=True,text=True,timeout=30)
        result=invoke({})
        self.assertEqual(result.returncode,0,result.stderr)
        published=github_env.read_text()
        self.assertIn(str(self.cookbook),published)
        self.assertIn(str(self.tools),published)
        self.assertIn(self.cookbook_ref,published)
        self.assertIn(self.tools_ref,published)
        self.assertFalse((self.app/'_delivery_cookbook').exists())
        self.assertFalse((self.app/'_delivery_tools').exists())
        for changes in [{'TOOLS_REF':self.cookbook_ref},{'TOOLS_ROOT':str(self.cookbook)},
                        {'COOKBOOK_ROOT':str(self.tools)}]:
            with self.subTest(changes=changes):
                self.assertNotEqual(invoke(changes).returncode,0)
                self.assertEqual(github_env.read_bytes(),b'')


if __name__=='__main__':unittest.main()
