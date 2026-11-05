"""Actual browser matrices and readonly boundaries survive shared production setup."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
from types import SimpleNamespace

spec=importlib.util.spec_from_file_location('public_verification',Path(__file__).parents[1]/'godot-production/production.py')
public=importlib.util.module_from_spec(spec);spec.loader.exec_module(public)
SOURCE='a'*40;RELEASE='b'*64


class PublicVerification(unittest.TestCase):
    def setUp(self):
        self.config={'recipe':'godot-game','node':{'packages':[{'directory':'tests/browser','manager':'npm'}]}}
        self.entry={'kind':'production','runner':'blacksmith-4vcpu-ubuntu-2204','timeout_minutes':15,
          'setup':'browser','packages':['tests/browser'],'entry':{'runner':'node','path':'tests/browser/public.mjs','timeout':900},
          'browsers':['chromium','firefox','webkit'],'xvfb':['chromium','firefox'],'probe':False}
        self.plan={'production':{'include':[self.entry]}}
        self.env={'GITHUB_REF':'refs/heads/main','GITHUB_SHA':SOURCE,'GITHUB_EVENT_NAME':'workflow_dispatch'}

    def test_only_exact_trusted_main_payload_and_declared_deadline_are_accepted(self):
        self.assertEqual(public.select(self.config,self.plan,SOURCE,RELEASE,self.env,self.entry['runner'],15),self.entry)
        for key,value in [('GITHUB_REF','refs/pull/1/merge'),('GITHUB_SHA','c'*40),('GITHUB_EVENT_NAME','pull_request')]:
            with self.subTest(key=key),self.assertRaises(ValueError):public.select(self.config,self.plan,SOURCE,RELEASE,{**self.env,key:value},self.entry['runner'],15)
        for source,release,runner,timeout in [(SOURCE,'wrong',self.entry['runner'],15),(SOURCE,RELEASE,'ubuntu-24.04',15),(SOURCE,RELEASE,self.entry['runner'],16)]:
            with self.assertRaises(ValueError):public.select(self.config,self.plan,source,release,self.env,runner,timeout)

    def test_coverage_cannot_invent_browsers_unknown_programs_or_generic_commands(self):
        for change in ['matrix','display','program','arguments','role','packages','duplicate']:
            entry=copy.deepcopy(self.entry);plan={'production':{'include':[entry]}}
            if change=='matrix':entry['browsers']=['chromium','chromium','webkit']
            if change=='display':entry['xvfb']=['unknown']
            if change=='program':entry['entry']['path']='deploy/mutate.mjs'
            if change=='arguments':entry['entry']['args']=['--url','https://unreviewed.example']
            if change=='role':entry['kind']='test'
            if change=='packages':entry['packages']=['unknown']
            if change=='duplicate':plan['production']['include'].append(entry)
            with self.subTest(change=change),self.assertRaises(ValueError):public.select(self.config,plan,SOURCE,RELEASE,self.env,self.entry['runner'],15)

    def test_native_assertions_retain_three_or_one_browsers_and_optional_blank_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);calls=[]
            def run(argv,directory,env,timeout):calls.append((argv,directory,env,timeout))
            fake=SimpleNamespace(preserve_mise=lambda env:{**env,'MISE_DATA_DIR':'/original-sdk-store'})
            with patch.dict(sys.modules,{'sdk_environment':fake}):
                public.verify(root,self.entry,{**self.env,'GH_TOKEN':'private'},SOURCE,RELEASE,run)
                self.assertEqual([c[2]['BROWSER'] for c in calls],['chromium','firefox','webkit'])
                self.assertEqual([c[0][0] for c in calls],['xvfb-run','xvfb-run','mise'])
                self.assertTrue(all(c[2]['EXPECTED_SOURCE_COMMIT']==SOURCE and c[2]['EXPECTED_RELEASE_ID']==RELEASE for c in calls))
                self.assertTrue(all('GH_TOKEN' not in c[2] and 'install' not in c[0] and 'build' not in c[0] for c in calls))
                calls.clear();entry={**self.entry,'browsers':['chromium'],'xvfb':[],'probe':True}
                public.verify(root,entry,self.env,SOURCE,RELEASE,run)
                self.assertEqual([c[2]['PUBLIC_SANDBOX_PROBE'] for c in calls],['1','0'])
                self.assertEqual([c[0][0] for c in calls],['mise','mise'])
                self.assertTrue(all(c[2]['MISE_DATA_DIR']=='/original-sdk-store' for c in calls))

    def test_sdk_setup_is_explicit_and_never_executes_game_assertions_or_compilers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);calls=[]
            def run(argv,directory,env,timeout=180):calls.append(argv)
            def install(root,packages,env,run):run(['npm','ci'],root,env)
            def setup(root,config,env,**kwargs):self.assertEqual(kwargs,{'capability':True,'public_browsers':self.entry['browsers']})
            with patch.dict(sys.modules,{'sdk_environment':SimpleNamespace(preserve_mise=lambda env:dict(env)),
                'node_component':SimpleNamespace(install=install),'godot_browser':SimpleNamespace(setup=setup)}):
                public.prepare(root,self.config,self.entry,self.env,run)
            self.assertEqual(calls,[['mise','install','python','node'],['mise','exec','--','npm','ci']])


if __name__=='__main__':unittest.main()
