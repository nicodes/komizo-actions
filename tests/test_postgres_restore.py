"""Scratch recovery admission and cleanup controls; no live database or key material."""
import importlib.util
import json
from source_fixture import write_source
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('postgres_restore',ROOT/'scripts/postgres_restore.py')
recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)


class PostgresRestore(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='scratch recovery ')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.config=dict(version=1,project='demo',image='postgres@sha256:'+'a'*64,minimum_rows=1,maximum_tables=500)
        self.driver=recovery.Drill('demo',self.config,self.root)

    def test_closed_declaration_keeps_engine_digest_project_and_row_floor(self):
        path=self.root/'declaration.toml'
        write_source(path,self.config);self.assertEqual(recovery.declaration(path,'demo'),self.config)
        for field,value in [('image','postgres:18'),('project','foreign'),('minimum_rows',0),
                            ('minimum_rows',True),('maximum_tables',501),('command','true')]:
            config=self.config|{field:value};write_source(path,config)
            with self.subTest(field=field),self.assertRaises(ValueError):recovery.declaration(path,'demo')
        with self.assertRaises(ValueError):self.driver.row_requirement(0)

    def test_only_local_docker_endpoint_receives_redacted_failure_and_binary_stdin(self):
        dump=self.root/'database.dump';dump.write_bytes(b'PGDMPsynthetic')
        calls=[]
        def run(command,**options):
            calls.append((command,options))
            if options.get('stdin') is not None:self.assertEqual(options['stdin'].read(),b'PGDMPsynthetic')
            return SimpleNamespace(returncode=0,stdout='fixture')
        with patch.dict(os.environ,dict(DOCKER_HOST='tcp://foreign',DOCKER_CONTEXT='foreign',DOCKER_TLS_VERIFY='1',DOCKER_CERT_PATH='/foreign')),\
             patch.object(recovery.subprocess,'run',side_effect=run):
            self.driver.restore_dump('owned-container',dump)
            self.driver.docker('inspect','owned-container')
        for command,options in calls:
            self.assertEqual(command[:3],['docker','--host','unix:///var/run/docker.sock'])
            self.assertFalse(set(options['env'])&recovery.DOCKER_ENV_EXCLUDE)
            self.assertNotIn(str(dump),command)
            self.assertNotIn('--publish',command)
        with patch.object(recovery.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='password=private')):
            with self.assertRaisesRegex(RuntimeError,'scratch PostgreSQL inspect failed') as failure:self.driver.docker('inspect','owned')
        self.assertNotIn('password',str(failure.exception))

    def test_all_official_pull_fallbacks_bind_the_same_declared_digest(self):
        attempts=[]
        def docker(*arguments,**options):
            attempts.append((arguments,options))
            if len(attempts)<3:raise RuntimeError('unavailable')
            return ''
        with patch.object(self.driver,'docker',side_effect=docker):self.assertEqual(self.driver.pull_engine(),self.config['image'])
        self.assertEqual(len(attempts),3)
        self.assertTrue(all(arguments[1].endswith('@sha256:'+'a'*64) for arguments,_ in attempts))
        self.assertTrue(all(options['timeout']==300 for _,options in attempts))

    def test_generated_scratch_resources_have_internal_network_no_ports_and_owned_cleanup(self):
        commands=[];name=None
        def docker(*arguments,**options):
            nonlocal name
            commands.append(arguments)
            if arguments[0]=='run':name=arguments[arguments.index('--name')+1]
            if arguments[0]=='inspect':return json.dumps([dict(Id='b'*64,State={'Running':True},Config={'Labels':{'nicodes.restore':name}})])
            return ''
        with patch.object(self.driver,'pull_engine',return_value=self.config['image']),\
             patch.object(self.driver,'docker',side_effect=docker),patch.object(self.driver,'sql',return_value='*'):
            with self.driver.scratch_postgres() as container:self.assertTrue(container.startswith('demo-restore-pg-'))
        run=next(command for command in commands if command[0]=='run')
        self.assertIn('--cap-drop',run);self.assertIn('no-new-privileges:true',run)
        self.assertIn('512m',run);self.assertIn('128',run)
        self.assertIn(('rm','-f','-v','b'*64),commands)
        self.assertFalse(any(value in run for value in ['--publish','-p','--volume','-v']))
        self.assertIn('--internal',commands[0])

    def test_foreign_cleanup_identity_is_refused_without_removing_container(self):
        commands=[]
        def docker(*arguments,**options):
            commands.append(arguments)
            if arguments[0]=='inspect':return json.dumps([dict(Id='b'*64,State={'Running':True},Config={'Labels':{'nicodes.restore':'foreign'}})])
            return ''
        with patch.object(self.driver,'pull_engine',return_value=self.config['image']),\
             patch.object(self.driver,'docker',side_effect=docker),patch.object(self.driver,'sql',return_value='*'):
            with self.assertRaisesRegex(RuntimeError,'ownership'):
                with self.driver.scratch_postgres():pass
        self.assertFalse(any(command[0]=='rm' for command in commands))

    def test_table_inventory_is_bounded_and_identifiers_cannot_become_sql(self):
        statements=[]
        def sql(container,statement):
            statements.append(statement)
            if 'json_agg' in statement:return json.dumps([['public','table"; DROP SCHEMA public;--']])
            return '1'
        with patch.object(self.driver,'sql',side_effect=sql):
            self.assertEqual(self.driver.verify_restored('owned'),{'tables':1,'rows':1})
        self.assertEqual(statements[-1],'SELECT count(*) FROM "public"."table""; DROP SCHEMA public;--"')
        with patch.object(self.driver,'sql',side_effect=['501',json.dumps([['public',str(n)] for n in range(501)])]):
            with self.assertRaisesRegex(ValueError,'refuses'):self.driver.verify_restored('owned')

    def test_digest_row_and_recovered_engine_refusals_precede_scratch_creation(self):
        dump=self.root/'database.dump';dump.write_bytes(b'PGDMPfixture')
        scratch=Mock()
        with patch.object(self.driver,'scratch_postgres',scratch):
            with self.assertRaisesRegex(ValueError,'digest'):self.driver.restore_from_dump(dump,'f'*64)
            with self.assertRaisesRegex(ValueError,'weaken'):self.driver.restore_from_dump(dump,None,0)
            snapshot=SimpleNamespace(unseal=Mock(return_value={'image_reference':'postgres@sha256:'+'c'*64}))
            with patch.object(self.driver,'helper',return_value=snapshot):
                with self.assertRaisesRegex(ValueError,'engine'):self.driver.drill(self.root,self.root/'key')
        self.assertFalse(scratch.called)

    def test_local_key_requirement_is_checked_without_any_container_or_key_upload(self):
        config=self.root/'config.toml';write_source(config,self.config)
        result=subprocess.run([sys.executable,str(ROOT/'scripts/postgres_restore.py'),'--project','demo',
            '--config',str(config),'--tools-root',str(self.root)],env={'PATH':os.environ['PATH']},
            capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('BACKUP_RECOVERY_KEY_FILE',result.stderr)


if __name__=='__main__':unittest.main()
