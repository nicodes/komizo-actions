"""Historical evidence is distinct from full-source validation and grants no new host identity."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('static_recovery',Path(__file__).parents[1]/'godot-static-recovery/recovery.py')
recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)
# The prepared SDK supplies the existing generic source/record validators.
TOOLS=Path(__import__('os').environ['TOOLS_ROOT'])
SOURCE='a'*40;SDK='b'*40;REPO='example/game'


def run_record():return {'id':17,'repository':{'full_name':REPO},'head_repository':{'full_name':REPO},'path':'.github/workflows/cd.yml',
 'head_branch':'main','event':'push','conclusion':'success','status':'completed','head_sha':SOURCE}


def record():return {'repository':REPO,'sha':SOURCE,'run_id':'17','release_id':'c'*64,
 'gate_ref':'ghcr.io/'+REPO+'-gate@sha256:'+'d'*64,'config_ref':'ghcr.io/'+REPO+'-config@sha256:'+'e'*64}


class HistoricalAcceptance(unittest.TestCase):
    def test_source_and_original_record_refuse_wrong_workflows_repositories_status_and_immutable_refs(self):
        baseline=run_record()
        for field,value in [('path','.github/workflows/ci.yml'),('head_branch','feature'),('conclusion','failure'),('event','pull_request')]:
            changed={**baseline,field:value}
            with self.subTest(field=field),patch.object(recovery,'capture',return_value=json.dumps(changed)),self.assertRaises(ValueError):
                recovery.source_run(REPO,'17',TOOLS,Path('.'),{})
        with patch.object(recovery,'capture',return_value=json.dumps(baseline)):
            self.assertEqual(recovery.source_run(REPO,'17',TOOLS,Path('.'),{})['head_sha'],SOURCE)
            for invalid in ['0','-1','17;touch foreign']:
                with self.assertRaises(ValueError):recovery.source_run(REPO,invalid,TOOLS,Path('.'),{})
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);directory=root/'rollback-record';directory.mkdir();path=directory/'release-record.json';path.write_text(json.dumps(record()))
            with patch.object(recovery,'source_run',return_value=baseline):
                recovery.authenticated_record(root,TOOLS,'17',{'GITHUB_REPOSITORY':REPO})
                data=record();data['config_ref']=data['config_ref'].replace('/game-config@','/foreign-config@');path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):recovery.authenticated_record(root,TOOLS,'17',{'GITHUB_REPOSITORY':REPO})

    def test_historical_contract_exports_only_three_tracked_data_files_and_never_executes_old_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            files={'mise.toml':'[tools]\nrust="1.94.0"\n[tasks.old]\nrun="touch foreign"\n','deploy/gate-runtime/Cargo.lock':'version=4\n','deploy/gate/Caddyfile':'reviewed routes\n'}
            for name,body in files.items():path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body)
            subprocess.run(['git','init','-q',str(root)],check=True)
            subprocess.run(['git','add','.'],cwd=root,check=True)
            subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','-qm','fixture'],cwd=root,check=True)
            source=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
            destination=recovery.historical_contract(root,{'sha':source},{})
            self.assertEqual({p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()},set(files))
            self.assertFalse((root/'foreign').exists())
            with self.assertRaisesRegex(ValueError,'fresh'):recovery.historical_contract(root,{'sha':source},{})

    def proof(self):
        env={key:'context-'+key for key in recovery.CONTEXT};env['GITHUB_SHA']=SOURCE
        checks=['trusted-successful-main-cd','immutable-original-release-record','historical-constrained-config','native-shipping-contract',
           'ordinary-game-browser-matrix','historical-cargo-current-advisories','historical-image-current-security-and-sbom']
        proof={'version':1,'coverage':'historical-static-release','status':'passed','controller_source':SOURCE,'cookbook_commit':SDK,
            'configuration_sha256':'f'*64,'context':dict(env),'checks':checks,'record':record(),'source_run':run_record()}
        jobs={'total_count':1,'jobs':[{'name':'Verify recovery evidence / Verify historical static release','conclusion':'success','head_sha':SOURCE}]}
        return proof,env,jobs

    def test_restore_requires_complete_distinct_historical_proof_and_actual_same_attempt_success(self):
        proof,env,jobs=self.proof()
        self.assertEqual(recovery.accept_restore(proof,'a'*64,Path('.'),SDK,'f'*64,env,jobs),record())
        for change in ['coverage','status','source','sdk','context','checks','jobs','count']:
            candidate=copy.deepcopy(proof);actual=copy.deepcopy(jobs)
            if change=='coverage':candidate['coverage']='complete-host-and-godot-matrix'
            if change=='status':candidate['status']='failed'
            if change=='source':candidate['controller_source']='0'*40
            if change=='sdk':candidate['cookbook_commit']='0'*40
            if change=='context':candidate['context']['GITHUB_RUN_ATTEMPT']='another-attempt'
            if change=='checks':candidate['checks'].pop()
            if change=='jobs':actual['jobs'][0]['conclusion']='failure'
            if change=='count':actual['total_count']=2
            with self.subTest(change=change),self.assertRaises(ValueError):recovery.accept_restore(candidate,'a'*64,Path('.'),SDK,'f'*64,env,actual)

    def test_verifier_executes_existing_shipping_browser_and_security_boundaries_without_product_builds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'cookbook.toml').write_text('version=1\nrecipe="godot-game"\n');calls=[];environment={key:'context-'+key for key in recovery.CONTEXT}
            environment.update(GITHUB_SHA=SOURCE,GH_TOKEN='read-only-token')
            config={'tests':{'integration':[{'name':'shipping','runner':'bash','path':'tests/gate/shipping.sh','timeout':300}]},
              'components':{'browser':{'runner':'tests/browser/diagnostic.mjs','journeys':[{'name':'ordinary','path':'tests/browser/ordinary.mjs','image':'gate'}]}}}
            def execute(argv,*_args):calls.append(('command',argv))
            def native(root,entries,env):
                self.assertNotIn('GH_TOKEN',env);self.assertEqual(env['GODOT_ACCEPTED_SOURCE_COMMIT'],SOURCE)
                self.assertEqual(entries[0]['path'],'tests/gate/shipping.sh');calls.append(('native',entries))
            def browser(root,config,env):
                self.assertEqual(config['components']['browser']['runner'],'tests/browser/ordinary.mjs')
                self.assertEqual(env['GATE_IMAGE'],record()['gate_ref']);self.assertEqual(env['EXPECTED_RELEASE_ID'],record()['release_id'])
                calls.append(('browser',env['BROWSER']))
            def security(root,config,env,run,records):
                self.assertEqual(records,[('gate',record()['gate_ref'])]);calls.append(('security',records))
            with patch.dict(sys.modules,{'native_tests':SimpleNamespace(run_entries=native),'godot_browser':SimpleNamespace(journeys=browser),
              'godot_security':SimpleNamespace(scan_references=security)}),patch.object(recovery,'authenticated_record',return_value=(run_record(),record())), \
              patch.object(recovery,'historical_contract',return_value=root/'historic'),patch.object(recovery,'execute',side_effect=execute):
                result=recovery.verify(root,TOOLS,SDK,config,{'repository':REPO,'recovery_test':'shipping'},'17',environment)
            self.assertEqual(result['sha'],SOURCE)
            self.assertEqual([value[0] for value in calls],['command','command','native','browser','command','security'])
            self.assertFalse(any('build' in value[1] for value in calls if value[0]=='command'))
            packet=json.loads((root/recovery.STATE/'acceptance.json').read_text())
            self.assertEqual(packet['coverage'],'historical-static-release')
            self.assertEqual(packet['record'],record())

    def test_only_the_declared_existing_shipping_fixture_can_be_selected(self):
        entry={'name':'shipping','runner':'bash','path':'tests/gate/test_shipping.sh','timeout':300}
        config={'tests':{'integration':[entry]}}
        self.assertEqual(recovery.fixture(config,{'recovery_test':'shipping'}),entry)
        with self.assertRaises(ValueError):recovery.fixture(config,{'recovery_test':'unknown'})
        config['tests']['integration'].append(entry)
        with self.assertRaises(ValueError):recovery.fixture(config,{'recovery_test':'shipping'})


if __name__=='__main__':unittest.main()
