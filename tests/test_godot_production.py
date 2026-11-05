"""Actual browser matrices and readonly boundaries survive shared production setup."""
import copy
import importlib.util
import os
from pathlib import Path
import socket
import stat
import subprocess
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

    def test_existing_manual_historical_expectations_do_not_change_the_checked_out_main_code(self):
        older='c'*40
        with self.assertRaisesRegex(ValueError,'current main payload'):
            public.select(self.config,self.plan,SOURCE,RELEASE,self.env,self.entry['runner'],15,older,True)
        self.assertEqual(public.select(self.config,self.plan,SOURCE,RELEASE,self.env,self.entry['runner'],15,older,False),self.entry)
        with self.assertRaises(ValueError):public.select(self.config,self.plan,older,RELEASE,self.env,self.entry['runner'],15,older,False)

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

    def test_real_unix_socket_carrier_is_short_private_and_keeps_native_context(self):
        with tempfile.TemporaryDirectory(dir='/tmp',prefix='production-source-') as temporary:
            root=Path(temporary)/('selected-source-'+'x'*80);root.mkdir()
            fake=SimpleNamespace(preserve_mise=lambda env:{**env,'MISE_DATA_DIR':'/original-sdk-store'})
            inherited={**self.env,'TMPDIR':str(root/'inherited-temp'),'GH_TOKEN':'private-one',
                'GITHUB_TOKEN':'private-two','MISE_GITHUB_TOKEN':'private-three'}
            calls=[];suffix='org.chromium.Chromium.sample/SingletonSocket'
            with patch.dict(sys.modules,{'sdk_environment':fake}):
                old=public.runtime_environment(root,inherited);long_socket=Path(old['TMPDIR'])/suffix
                long_socket.parent.mkdir(mode=0o700)
                self.assertGreaterEqual(len(os.fsencode(long_socket)),108)
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
                    with self.assertRaisesRegex(OSError,'AF_UNIX path too long'):server.bind(str(long_socket))
                def run(argv,directory,env,timeout):
                    carrier=Path(env['TMPDIR']);path=carrier/suffix
                    self.assertEqual(carrier.parent,Path('/tmp'));self.assertFalse(carrier.is_symlink())
                    self.assertEqual(carrier.stat().st_uid,os.getuid());self.assertEqual(stat.S_IMODE(carrier.stat().st_mode),0o700)
                    self.assertLess(len(os.fsencode(path)),108)
                    self.assertEqual(argv,['mise','exec','--','node','tests/browser/public.mjs'])
                    self.assertEqual(directory,root);self.assertEqual(timeout,900)
                    self.assertEqual(env['MISE_DATA_DIR'],'/original-sdk-store')
                    for key in ['HOME','XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_STATE_HOME','PLAYWRIGHT_BROWSERS_PATH','GDPW_BIN']:
                        self.assertEqual(env[key],old[key])
                    self.assertTrue(all(key not in env for key in ['GH_TOKEN','GITHUB_TOKEN','MISE_GITHUB_TOKEN']))
                    self.assertEqual(env['EXPECTED_SOURCE_COMMIT'],SOURCE);self.assertEqual(env['EXPECTED_RELEASE_ID'],RELEASE)
                    path.parent.mkdir(mode=0o700,exist_ok=True)
                    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server,socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                        server.settimeout(1);client.settimeout(1);server.bind(str(path));server.listen(1);client.connect(str(path));client.sendall(b'generic socket context')
                        connection,_=server.accept()
                        with connection:
                            connection.settimeout(1);self.assertEqual(connection.recv(64),b'generic socket context')
                    path.unlink();calls.append(dict(env))
                entry={**self.entry,'browsers':['chromium'],'xvfb':[],'probe':True}
                before=dict(os.environ)
                with patch.dict(os.environ,{'TMPDIR':inherited['TMPDIR']}):
                    public.verify(root,entry,inherited,SOURCE,RELEASE,run)
                    self.assertEqual(os.environ['TMPDIR'],inherited['TMPDIR'])
                self.assertEqual(dict(os.environ),before)
            self.assertEqual([row['PUBLIC_SANDBOX_PROBE'] for row in calls],['1','0'])
            self.assertEqual(calls[0]['TMPDIR'],calls[1]['TMPDIR']);self.assertFalse(Path(calls[0]['TMPDIR']).exists())
            self.assertTrue(Path(old['HOME']).is_dir());self.assertTrue(Path(old['TMPDIR']).is_dir())
            print('NATIVE_PUBLIC_SOCKET_CARRIER_OK: long path refused; same suffix bind/connect/send; private context cleaned')

    def test_real_failing_child_propagates_and_cleans_the_owned_carrier(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);carriers=[];calls=[]
            def run(argv,directory,env,timeout):
                self.assertEqual(timeout,900);calls.append(argv);carrier=Path(env['TMPDIR']);carriers.append(carrier)
                (carrier/'child-state').write_text('disposable')
                subprocess.run([sys.executable,'-I','-c','raise SystemExit(7)'],cwd=directory,env=env,check=True,timeout=2)
            with patch.dict(sys.modules,{'sdk_environment':SimpleNamespace(preserve_mise=lambda env:dict(env))}):
                with self.assertRaises(subprocess.CalledProcessError) as failed:public.verify(root,{**self.entry,'probe':True},self.env,SOURCE,RELEASE,run)
            self.assertEqual(failed.exception.returncode,7);self.assertEqual(len(calls),1)
            self.assertTrue(carriers);self.assertTrue(all(not path.exists() for path in carriers))
            self.assertTrue((root/'.artifacts/godot-production/home').is_dir())

    def test_timeout_and_callback_exception_cleanup_use_fresh_contexts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);carriers=[]
            def run(argv,directory,env,timeout):
                self.assertEqual(timeout,900);carrier=Path(env['TMPDIR']);carriers.append(carrier)
                (carrier/'unfinished-child').write_text('disposable')
                if kind=='timeout':subprocess.run([sys.executable,'-I','-c','import time;time.sleep(30)'],cwd=directory,env=env,check=True,timeout=0.05)
                else:raise RuntimeError('generic callback failure')
            with patch.dict(sys.modules,{'sdk_environment':SimpleNamespace(preserve_mise=lambda env:dict(env))}):
                for kind,error in [('timeout',subprocess.TimeoutExpired),('callback',RuntimeError)]:
                    with self.subTest(kind=kind),self.assertRaises(error):public.verify(root,self.entry,self.env,SOURCE,RELEASE,run)
            self.assertEqual(len(set(carriers)),2);self.assertTrue(all(not path.exists() for path in carriers))

    def test_setup_retains_checkout_state_and_does_not_allocate_a_verify_carrier(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);environments=[]
            def run(argv,directory,env,timeout=180):environments.append(env)
            modules={'sdk_environment':SimpleNamespace(preserve_mise=lambda env:dict(env)),
                'node_component':SimpleNamespace(install=lambda *args:None),
                'godot_browser':SimpleNamespace(setup=lambda *args,**kwargs:None)}
            with patch.dict(sys.modules,modules),patch.object(public.tempfile,'TemporaryDirectory',side_effect=AssertionError('setup must not allocate verification context')):
                public.prepare(root,self.config,self.entry,self.env,run)
            self.assertEqual(len(environments),1)
            self.assertEqual(environments[0]['TMPDIR'],str(root/'.artifacts/godot-production/tmp'))
            self.assertEqual(environments[0]['HOME'],str(root/'.artifacts/godot-production/home'))


if __name__=='__main__':unittest.main()
