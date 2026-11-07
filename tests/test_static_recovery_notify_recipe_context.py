"""Actual recipe-generator ABI and finite shell controls; never restore or notify."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT=Path(__file__).resolve().parents[1]


class StaticRecoveryAndNotifyContext(unittest.TestCase):
    def setUp(self):
        self.action=yaml.safe_load((ROOT/'godot-static-recovery/action.yml').read_text())
        self.notify=yaml.safe_load((ROOT/'.github/workflows/cookbook-notify.yml').read_text())
        self.steps=self.action['runs']['steps']
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.directory=Path(self.temporary.name);self.trace=self.directory/'trace'
        binary=self.directory/'bin';binary.mkdir()
        mise=binary/'mise';mise.write_text('#!/bin/sh\nprintf \'%s\\0\' "$@" > "$TRACE"\n');mise.chmod(0o755)
        gh=binary/'gh';gh.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$TRACE"\nprintf ahead\n');gh.chmod(0o755)
        self.environment=dict(os.environ,PATH=str(binary)+os.pathsep+os.environ['PATH'],TRACE=str(self.trace),
            GITHUB_ACTION_PATH=str(ROOT/'godot-static-recovery'),COOKBOOK_REF='',TOOLS_REF='b'*40,PROOF_SHA256='c'*64)

    def native_shell(self,step,environment):
        result=subprocess.run(['bash','-euc',step['run']],cwd=self.directory,env=environment,
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return self.trace.read_bytes().split(b'\0')[:-1]

    def test_restore_retains_legacy_argv_and_selects_distinct_modern_sources(self):
        step=next(s for s in self.steps if s.get('id')=='copy')
        legacy=self.native_shell(step,self.environment)
        prefix=[b'exec',b'--',b'python3',str(ROOT/'godot-static-recovery/recovery.py').encode(),b'restore']
        self.assertEqual(legacy,prefix+[b'--tools',b'_godot_recovery_cookbook',b'--tools-ref',b'b'*40,b'--proof-sha256',b'c'*64])
        modern=self.native_shell(step,self.environment|{'COOKBOOK_REF':'a'*40,'TOOLS_REF':''})
        self.assertEqual(modern,prefix+[b'--cookbook-root',b'.artifacts/recipe-sdks/_cookbook',b'--cookbook-ref',b'a'*40,
            b'--tools-root',b'.artifacts/recipe-sdks/_tools',b'--proof-sha256',b'c'*64])

    def test_public_verification_uses_actual_tools_helper_and_legacy_path(self):
        step=next(s for s in self.steps if s.get('name')=='Verify original public payload and source over ordinary TLS')
        self.assertEqual(self.native_shell(step,self.environment),
                         [b'exec',b'--',b'python3',b'_godot_recovery_cookbook/helpers/public_release.py'])
        self.assertEqual(self.native_shell(step,self.environment|{'COOKBOOK_REF':'a'*40,'TOOLS_REF':''}),
                         [b'exec',b'--',b'python3',b'.artifacts/recipe-sdks/_tools/helpers/public_release.py'])

    def test_identity_xor_refuses_before_any_source_checkout_or_sdk_code(self):
        step=next(s for s in self.steps if s.get('name')=='Require accepted immutable recovery cookbook')
        for changes in [{'COOKBOOK_REF':'a'*40,'TOOLS_REF':'b'*40},{'COOKBOOK_REF':'not-a-commit','TOOLS_REF':''},
                        {'COOKBOOK_REF':'','TOOLS_REF':''}]:
            with self.subTest(changes=changes):
                result=subprocess.run(['bash','-euc',step['run']],cwd=self.directory,env=self.environment|changes,
                                      capture_output=True,text=True,timeout=10)
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(self.trace.exists())
        self.native_shell(step,self.environment)
        self.assertIn('repos/nicodes/tools/compare/'+'b'*40+'...main',self.trace.read_text())
        self.native_shell(step,self.environment|{'COOKBOOK_REF':'a'*40,'TOOLS_REF':''})
        self.assertIn('repos/nicodes/cookbook/compare/'+'a'*40+'...main',self.trace.read_text())

    def test_modern_setup_matches_existing_accepted_source_adapter_before_restore(self):
        other=yaml.safe_load((ROOT/'godot-static-delivery/action.yml').read_text())['runs']['steps']
        names=['Bootstrap Python for protected SDK sources','Prepare the private artifact parent with actual pinned Python',
               'Inspect the host configuration parser without SDK imports','Bootstrap the exact supported configuration parser',
               'Admit both real sources before Cookbook SDK execution','Prepare only the checked Cookbook provider dependency',
               'Explicitly prepare the selected exact caller SDKs']
        positions=[]
        for name in names:
            selected=next(s for s in self.steps if s.get('name')==name)
            self.assertEqual(selected,next(s for s in other if s.get('name')==name))
            positions.append(self.steps.index(selected))
        self.assertEqual(positions,sorted(positions))
        checkout=next(s for s in self.steps if s.get('uses','').startswith('actions/checkout@'))
        self.assertLess(positions[1],self.steps.index(checkout))
        self.assertLess(positions[-1],next(i for i,s in enumerate(self.steps) if s.get('id')=='copy'))
        legacy=next(s for s in self.steps if s.get('uses','').startswith('jdx/mise-action@'))
        self.assertEqual(legacy['if'],"${{ inputs.cookbook-ref == '' }}")
        self.assertEqual(legacy['uses'],'jdx/mise-action@5228313ee0372e111a38da051671ca30fc5a96db')
        deploy=next(s for s in self.steps if s.get('name')=='Restore through the original admitted source-tag deployment contract')
        self.assertEqual(deploy['uses'],'nicodes/komizo-actions/deploy@22a128034be007614021467201be5924cfacc88b')
        self.assertFalse(any('timeout-minutes' in s for s in self.steps))

    def test_actual_accepted_recipe_generators_supply_supported_interfaces(self):
        cookbook=Path(os.environ['RECIPE_TASK_SDK']);tools=Path(os.environ['RECIPE_TASK_TOOLS'])
        revision=os.environ['RECIPE_TASK_SDK_REF']
        code='''import json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1]);import recipe_context
cookbook,tools=map(Path,sys.argv[2:4]);revision=sys.argv[4]
context=recipe_context.accepted_context(cookbook,revision,lambda path:{'status':'identical'},tools)
delivery=context.module('delivery_workflows');static=context.module('godot_static_workflows')
data={'project':'application','komizo_ref':'a'*40,'publish_runner':'ubuntu-24.04','publish_timeout':20,
 'deploy_runner':'ubuntu-24.04','deploy_timeout':30,'notification_url':'https://example.test/','health_urls':['https://example.test/'],
 'report':False,'notify':True}
config={'version':1,'recipe':'godot-game','delivery':{'definition':'deploy/delivery.toml'}}
with patch.object(delivery,'declaration',return_value=data),patch.object(static.godot_static,'declaration',return_value={'production':False,'recovery':True}):
 files=static.files(Path.cwd(),config,revision)
 config['recipe']='react-native'
 jobs=delivery.jobs(Path.cwd(),config,revision)
print(json.dumps({'recovery':files['.github/workflows/recovery.yml'],'notification':'jobs:\\n'+jobs}))
'''
        result=subprocess.run([sys.executable,'-I','-B','-c',code,str(ROOT/'scripts'),str(cookbook),str(tools),revision],
                              cwd=self.directory,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        generated=json.loads(result.stdout)
        recovery=yaml.safe_load(generated['recovery']);restore=next(s for s in recovery['jobs']['restore']['steps'] if s.get('uses','').startswith('nicodes/komizo-actions/godot-static-recovery@'))
        notification=yaml.safe_load(generated['notification']);notify=next(j for j in notification['jobs'].values() if j.get('uses','').startswith('nicodes/komizo-actions/.github/workflows/cookbook-notify.yml@'))
        for caller,inputs in [(restore,self.action['inputs']),(notify,self.notify['on']['workflow_call']['inputs'])]:
            values=caller['with']
            self.assertEqual(values['cookbook-ref'],revision)
            self.assertNotIn('tools-ref',values)
            self.assertFalse(set(values)-set(inputs))
            self.assertFalse({k for k,v in inputs.items() if v.get('required') is True}-set(values))


if __name__=='__main__':unittest.main()
