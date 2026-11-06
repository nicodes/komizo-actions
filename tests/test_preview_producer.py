"""Exact selected-HEAD native publication admission, without network or host effects."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(os.environ['TOOLS_ROOT']) / 'cookbook'))
import configuration as sdk_configuration
spec = importlib.util.spec_from_file_location('preview_producer', ROOT / 'scripts/preview-producer.py')
producer = importlib.util.module_from_spec(spec); spec.loader.exec_module(producer)


class PreviewProducer(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native preview producer ')
        self.addCleanup(temporary.cleanup); self.directory = Path(temporary.name)
        self.root = self.directory / 'controller'; self.root.mkdir()
        self.tools = self.directory / 'tools'; self.tools.mkdir()
        for root in [self.root, self.tools]:
            self.git(root, 'init', '-q'); self.git(root, 'config', 'user.name', 'Fixture')
            self.git(root, 'config', 'user.email', 'fixture@example.test')
        (self.tools / 'sdk.txt').write_text('accepted fixture SDK\n')
        self.commit(self.tools)
        self.tools_ref = self.git(self.tools, 'rev-parse', 'HEAD')
        self.raw_policy = dict(version=1, project='demo', components=['gate'],
            workflow_sha256=hashlib.sha256(b'reviewed raw workflow\n').hexdigest(),
            build=dict(runner='ubuntu-24.04', timeout_minutes=60, environment='Preview'),
            publish=dict(runner='ubuntu-24.04', timeout_minutes=15), producer_timeout_minutes=60)
        for name, body in {'cookbook.toml':'recipe = "react-native"\n', 'mise.toml':'[tools]\npython = "3.13.11"\n',
                'Makefile':'COOKBOOK_REF := '+self.tools_ref+'\n',
                '.github/workflows/pr-preview.yml':'reviewed raw workflow\n',
                'deploy/preview-publication.json':json.dumps(self.raw_policy),
                'deploy/release.json':json.dumps(dict(version=1,project='demo',image_base='ghcr.io/owner/demo',
                    tests=['lint','unit','security','e2e'],inputs=dict(artifacts=['.artifacts/release'],
                    images=['ghcr.io/owner/demo-'+c+':{revision}' for c in ['gate','api','config']],
                    environment=[],revision_environment=[])))}.items():
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(body)
        self.commit(self.root)
        self.base = self.git(self.root, 'rev-parse', 'HEAD')
        self.tree = self.git(self.root, 'rev-parse', 'HEAD^{tree}')
        self.source = self.directory / 'source'
        self.git(self.directory, 'clone', '-q', '--no-hardlinks', str(self.root), str(self.source))
        self.git(self.source, 'config', 'user.name', 'Fixture'); self.git(self.source, 'config', 'user.email', 'fixture@example.test')
        (self.source / 'native.txt').write_text('new native source\n'); self.commit(self.source)
        self.head = self.git(self.source, 'rev-parse', 'HEAD'); self.source_tree = self.git(self.source, 'rev-parse', 'HEAD^{tree}')
        self.config = dict(recipe='react-native', ci=dict(runner='ubuntu-24.04', timeout_minutes=90), artifacts=dict(definition='deploy/release.json'))
        self.definition = dict(project='demo', image_base='ghcr.io/owner/demo', tests=['lint','unit','security','e2e'])
        modules = (None, SimpleNamespace(load=lambda _:copy.deepcopy(self.config), ci_settings=sdk_configuration.ci_settings,
                                        __file__=str(self.tools/'cookbook/configuration.py')),
            SimpleNamespace(definition=lambda *_:copy.deepcopy(self.definition), components=lambda _:['gate','api','config']))
        self.patch = patch.object(producer.preview, 'tools_modules', return_value=modules); self.patch.start(); self.addCleanup(self.patch.stop)
        self.environment = dict(GITHUB_REPOSITORY='owner/demo', GITHUB_SHA=self.base, GITHUB_WORKFLOW_SHA=self.base,
            GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='issue_comment', GITHUB_SERVER_URL='https://github.com',
            GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/pr-preview.yml@refs/heads/main',
            GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2', GITHUB_JOB='publish', GH_TOKEN='fixture-token',
            PRODUCER_RUNNER='ubuntu-24.04', PRODUCER_TIMEOUT='60', PUBLICATION_TIMEOUT='15',
            TOOLS_REF=self.tools_ref,KOMIZO_REF='d'*40)
        self.event = dict(action='created', issue=dict(number=42, pull_request={'url':'fixture'}),
            comment=dict(body='/preview', author_association='OWNER'))
        self.event_path = self.directory / 'event.json'; self.event_path.write_text(json.dumps(self.event))
        self.environment['GITHUB_EVENT_PATH'] = str(self.event_path)
        self.pr = dict(number=42, state='open', draft=False, author_association='OWNER', user={'login':'owner'},
            head=dict(sha=self.head, ref='native', repo={'full_name':'owner/demo'}),
            base=dict(sha=self.base, ref='main', repo={'full_name':'owner/demo'}), labels=[{'name':'preview'}])
        self.policy = producer.native_policy(self.root, self.tools, 'deploy/preview-publication.json')
        self.plan = dict(version=1, repository='owner/demo', controller=self.base, controller_tree=self.tree,
            trigger='issue_comment', pr=42, head=self.head, base=self.base, policy=self.policy)
        self.jobs = dict(total_count=2, jobs=[dict(name='validate / Full source checks / Make full', conclusion='success',
            head_sha=self.base, labels=['ubuntu-24.04'], started_at=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()),
            dict(name='publish / Publish selected Preview images', status='in_progress', head_sha=self.base, labels=['ubuntu-24.04'])])

    def git(self, root, *args):
        return subprocess.check_output(['git','-C',str(root),*args], text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, root):
        self.git(root, 'add', '.'); self.git(root, 'commit', '-qm', 'Fixture source')

    def lookup(self, path):
        if '/branches/main' in path:return {'commit':{'sha':self.base}}
        if '/actions/runs/' in path:return copy.deepcopy(self.jobs)
        return copy.deepcopy(self.pr)

    def write_event(self):
        self.event_path.write_text(json.dumps(self.event))

    def test_real_tracked_policy_and_current_source_bind_the_legacy_request(self):
        result = producer.request(self.root, self.tools, 'deploy/preview-publication.json', self.environment, self.lookup)
        self.assertEqual(json.loads(result['plan']), self.plan)
        self.assertEqual(result['sha'], self.head)
        self.assertEqual(producer.validate_plan(result['plan'], self.root, self.tools, self.environment, self.lookup), self.plan)
        self.assertEqual(result['build_timeout'], '60'); self.assertEqual(result['publish_timeout'], '60'); self.assertEqual(result['publication_timeout'], '15')

    def test_target_event_preserves_all_original_lifecycle_decisions_and_independent_down(self):
        self.environment['GITHUB_EVENT_NAME'] = 'pull_request_target'
        for action, expected in [('opened','false'),('ready_for_review','false'),('synchronize','true'),('reopened','true'),('closed','true')]:
            self.event = {'action':action, 'pull_request':copy.deepcopy(self.pr)}; self.write_event()
            result = producer.resolve(self.environment, self.lookup); self.assertEqual(result['enabled'], expected)
            if action == 'closed':
                # No SDK or native policy read is allowed to strand teardown.
                with patch.object(producer, 'native_policy', side_effect=AssertionError('cleanup loaded producer policy')):
                    down = producer.request(self.root, self.tools, 'missing.json', self.environment, self.lookup)
                self.assertEqual(down['action'], 'down'); self.assertNotIn('plan', down)
        self.pr['labels'] = []; self.event = {'action':'synchronize','pull_request':self.pr}; self.write_event()
        self.assertEqual(producer.resolve(self.environment, self.lookup)['enabled'], 'false')

    def test_source_freshness_refuses_stale_base_head_controller_and_lost_authorization(self):
        for side in ['head','base']:
            old=self.pr[side]['sha'];self.pr[side]['sha']='e'*40
            with self.subTest(side=side),self.assertRaises(ValueError):producer.fresh(self.plan,self.environment,self.lookup)
            self.pr[side]['sha']=old
        for field,value in [('draft',True),('author_association','CONTRIBUTOR'),('state','closed')]:
            old=self.pr[field];self.pr[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):producer.fresh(self.plan,self.environment,self.lookup)
            self.pr[field]=old
        self.event['comment']['author_association']='CONTRIBUTOR';self.write_event()
        with self.assertRaises(Exception):producer.fresh(self.plan,self.environment,self.lookup)
        self.environment['GITHUB_WORKFLOW_REF']='owner/demo/.github/workflows/other.yml@refs/heads/main'
        with self.assertRaises(ValueError):producer.resolve(self.environment,self.lookup)

    def test_reviewed_raw_git_blob_hash_and_regular_bounded_policy_are_required(self):
        caller=self.root/'.github/workflows/pr-preview.yml';caller.write_text('unreviewed workflow\n')
        with self.assertRaisesRegex(ValueError,'immutable source blob'):producer.native_policy(self.root,self.tools,'deploy/preview-publication.json')
        self.git(self.root,'checkout','--','.github/workflows/pr-preview.yml')
        policy=self.root/'deploy/preview-publication.json';body=policy.read_bytes();policy.unlink();policy.symlink_to(self.directory/'outside.json');(self.directory/'outside.json').write_bytes(body)
        with self.assertRaisesRegex(ValueError,'regular files'):producer.native_policy(self.root,self.tools,'deploy/preview-publication.json')

    def test_closed_policy_refuses_unknown_credentials_or_component_expansion_and_budget_relaxation(self):
        for mutate in [lambda p:p.update(secrets=['NEW_KEY']), lambda p:p.update(components=['unowned']),
                lambda p:p['build'].update(timeout_minutes=90), lambda p:p['publish'].update(timeout_minutes=61),
                lambda p:p.update(workflow_sha256='e'*64)]:
            policy=copy.deepcopy(self.raw_policy);mutate(policy)
            path=self.root/'deploy/preview-publication.json';path.write_text(json.dumps(policy));self.commit(self.root)
            with self.assertRaises(ValueError):producer.native_policy(self.root,self.tools,'deploy/preview-publication.json')

    def test_original_aggregate_deadline_includes_both_jobs_and_actual_elapsed_setup(self):
        now=datetime.now(timezone.utc);self.jobs['jobs'][0]['started_at']=(now-timedelta(minutes=59)).isoformat()
        seconds,deadline=producer.remaining(self.plan,self.jobs,now);self.assertEqual(seconds,60)
        with self.assertRaisesRegex(ValueError,'expired'):producer.remaining(self.plan,self.jobs,now+timedelta(seconds=61))
        self.jobs['jobs'][0]['started_at']=(now+timedelta(minutes=1)).isoformat()
        with self.assertRaisesRegex(ValueError,'timing'):producer.remaining(self.plan,self.jobs,now)
        self.jobs['total_count']=3
        with self.assertRaisesRegex(ValueError,'complete'):producer.remaining(self.plan,self.jobs,now)

    def archive(self, destination, extra=None):
        records=[];images={};members={}
        for component in ['gate','api','config']:
            body=json.dumps({'component':component}).encode();identity=hashlib.sha256(body).hexdigest();name=identity+'.json';members[name]=body
            tag='ghcr.io/owner/demo-'+component+':'+self.head;images[tag]='sha256:'+identity
            records.append({'Config':name,'RepoTags':[tag],'Layers':['layer.tar']})
        members['manifest.json']=json.dumps(records).encode();members['layer.tar']=b'opaque Docker layer bytes'
        with tarfile.open(destination,'w:gz') as package:
            for name,body in members.items():
                member=tarfile.TarInfo(name);member.size=len(body);package.addfile(member,io.BytesIO(body))
            if extra:
                member=tarfile.TarInfo(extra[0]);member.type=extra[1];member.linkname='../../escape';package.addfile(member)
        return images

    def native_context(self):
        return {k:self.environment[k] for k in ['GITHUB_SERVER_URL','GITHUB_REPOSITORY','GITHUB_RUN_ID',
            'GITHUB_RUN_ATTEMPT','GITHUB_SHA','GITHUB_WORKFLOW_REF','GITHUB_WORKFLOW_SHA','GITHUB_EVENT_NAME']}|{'GITHUB_JOB':'validate'}

    def evidence(self):
        incoming=self.directory/'incoming';release=incoming/'.artifacts/release';release.mkdir(parents=True)
        images=self.archive(release/'images.tar.gz');digest=hashlib.sha256((release/'images.tar.gz').read_bytes()).hexdigest()
        relationship={'preview_request':{'number':42,'head':self.head,'base':self.base}}
        manifest=dict(project='demo',revision=self.head,images=images,archive_sha256=digest,
            provenance={'source_relationship':relationship,'input_identity':'f'*64,'source_commit':self.head,
                'source_tree':self.source_tree,'tracked_source_clean':True,'configuration_sha256':{},
                'build_context':self.native_context()},stateful_contract={'fixture':'original immutable data'})
        (release/'release.json').write_text(json.dumps(manifest));(release/'images.json').write_text(json.dumps(images))
        (release/'images.tar.gz.sha256').write_text(digest+'  images.tar.gz\n')
        artifacts={'.artifacts/release/'+p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in release.iterdir()}
        artifacts.update({'image:'+ref:identity for ref,identity in images.items()})
        context=self.native_context()
        proof=dict(version=1,recipe='react-native',source_commit=self.head,source_tree=self.source_tree,cookbook_commit=self.tools_ref,
            status='passed',tracked_source_clean=True,build_verified=True,tests_verified=True,test_groups=self.policy['test_groups'],
            execution_context=context,artifacts=artifacts,head=self.head,base=self.base,configuration_sha256=self.policy['cookbook_sha256'])
        identity='f'*64
        provenance=dict(source_commit=self.head,source_tree=self.source_tree,tracked_source_clean=True,execution_context=context,input_identity=identity,configuration_sha256={})
        build=dict(identity=identity,artifacts=artifacts,provenance=provenance)
        test=dict(version=1,source_commit=self.head,tested_commit=self.head,source_tree=self.source_tree,tracked_source_clean=True,execution_context=context,input_identity=identity,configuration_sha256={},
            test_receipts={g:{'input_identity':identity,'source_commit':self.head} for g in self.policy['test_groups']})
        (incoming/'.artifacts/verification').mkdir()
        for name,value in [('cookbook-validation.json',proof),('verification/build.json',build),('verification/test.json',test)]:
            (incoming/'.artifacts'/name).write_text(json.dumps(value))
        return incoming,manifest

    def test_safe_archive_inventory_rejects_traversal_links_and_undeclared_tags(self):
        path=self.directory/'archive.tar.gz';images=self.archive(path);self.assertEqual(producer.archive_inventory(path,images),images)
        for name,kind in [('../escape',tarfile.REGTYPE),('link',tarfile.SYMTYPE),('hardlink',tarfile.LNKTYPE),('fifo',tarfile.FIFOTYPE)]:
            self.archive(path,(name,kind))
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'unsafe'):producer.archive_inventory(path,images)
        self.archive(path)
        with self.assertRaisesRegex(ValueError,'tags/IDs'):producer.archive_inventory(path,dict(list(images.items())[:1]))

    def test_real_closed_archive_derives_exact_components_without_changing_any_full_proof_bytes(self):
        incoming,manifest=self.evidence();release=incoming/'.artifacts/release'
        before={p.name:p.read_bytes() for p in release.iterdir()};destination=self.directory/'selected'
        derived=producer.derive(release,destination,['gate'],'ghcr.io/owner/demo',self.head)
        selected=json.loads((destination/'release.json').read_text())
        self.assertEqual(selected,manifest|{'images':{'ghcr.io/owner/demo-gate:'+self.head:manifest['images']['ghcr.io/owner/demo-gate:'+self.head]}})
        self.assertEqual(derived['archive_sha256'],manifest['archive_sha256'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in release.iterdir()})
        self.assertEqual((release/'images.tar.gz').stat().st_ino,(destination/'images.tar.gz').stat().st_ino)

    def test_existing_accepted_helper_admits_the_derived_subset_and_authenticated_exact_head_provenance(self):
        tools=Path(os.environ['TOOLS_ROOT']);helpers=tools/'helpers'
        sys.path.insert(0,str(helpers))
        try:helper=producer.module('native_preview_release_helper',helpers/'release.py')
        finally:sys.path.pop(0)
        incoming,manifest=self.evidence();release=incoming/'.artifacts/release'
        values=self.environment|{'COOKBOOK_PREVIEW_NUMBER':'42','REQUEST_HEAD':self.head,'REQUEST_BASE':self.base}
        previous=Path.cwd()
        try:
            os.chdir(self.source)
            with patch.dict(os.environ,values,clear=True),patch.object(helper,'preview_pr',return_value=self.pr):
                binding=helper.source_binding()
                manifest.pop('stateful_contract')
                manifest['provenance']=dict(version=1,source_commit=self.head,**binding,input_identity='f'*64,
                    tested_commit=None,source_relationship=helper.source_relationship(self.head),
                    build_context=self.native_context())
                (release/'release.json').write_text(json.dumps(manifest))
                destination=self.directory/'helper-selected'
                producer.derive(release,destination,['gate'],'ghcr.io/owner/demo',self.head)
                selected=json.loads((destination/'release.json').read_text())
                self.assertEqual(helper.validate(selected,'demo',self.head,['gate'],destination/'images.tar.gz','ghcr.io/owner/demo'),
                    ['ghcr.io/owner/demo-gate:'+self.head])
                accepted=helper.validate_provenance(selected,self.head)
                self.assertEqual(accepted['source_relationship']['preview_request']['head'],self.head)
                test_path=incoming/'.artifacts/verification/test.json';test=json.loads(test_path.read_text());test.update(binding);test_path.write_text(json.dumps(test))
                tested=helper.validate_test_evidence(incoming/'.artifacts/verification',self.head,'validate',{'tests':self.policy['test_groups']})
                self.assertEqual(tested['input_identity'],accepted['input_identity'])
                self.pr['head']['sha']='e'*40
                with self.assertRaisesRegex(ValueError,'stale'):helper.validate_provenance(selected,self.head)
        finally:os.chdir(previous)

    def run_publication(self,incoming,execute):
        return producer.publication(self.plan,self.root,self.source,Path(os.path.relpath(self.tools)),self.tools_ref,
            incoming,self.environment,self.lookup,execute)

    def test_actual_admission_staging_and_subset_publish_are_completed_before_package_effects(self):
        incoming,manifest=self.evidence();calls=[]
        def execute(command,**options):
            calls.append(command)
            self.assertGreater((options['deadline']-datetime.now(timezone.utc)).total_seconds(),0)
            self.assertEqual(options['env']['COOKBOOK_PREVIEW_NUMBER'],'42')
            if command[:2]==['docker','login']:
                self.assertEqual(options['input'],'fixture-token\n');return
            self.assertEqual(command[2],'publish');self.assertNotIn('record',command)
            self.assertEqual(Path(command[1]),self.tools/'helpers/release.py')
            self.assertEqual(command[command.index('--components')+1:command.index('--definition')],['gate'])
            selected=Path(command[command.index('--directory')+1]);result=json.loads((selected/'release.json').read_text())
            (selected/'publication.json').write_text(json.dumps(dict(revision=self.head,tested_commit=self.head,images=result['images'])))
        # Exercise the actual SDK's tracked-source definition ownership, not a
        # mocked image adapter, while controller and selected HEAD are distinct Git repositories.
        sdk=Path(os.environ['TOOLS_ROOT']);sys.path.insert(0,str(sdk/'cookbook'))
        try:images=producer.module('actual_preview_sdk_images',sdk/'cookbook/images.py')
        finally:sys.path.pop(0)
        modules=(None,SimpleNamespace(load=lambda _:copy.deepcopy(self.config),ci_settings=sdk_configuration.ci_settings,
                                     __file__=str(self.tools/'cookbook/configuration.py')),images)
        with patch.object(producer.preview,'tools_modules',return_value=modules):
            self.run_publication(incoming,execute)
        self.assertEqual(len(calls),2);report=json.loads((self.root/'.artifacts/preview-producer-publication.json').read_text())
        self.assertEqual(list(report['derivation']['selected_images']),['ghcr.io/owner/demo-gate:'+self.head])
        self.assertEqual(json.loads((incoming/'.artifacts/release/release.json').read_text()),manifest)

    def test_typed_preview_reads_the_actual_owned_definition_and_restores_controller_cwd_on_refusal(self):
        sdk=Path(os.environ['TOOLS_ROOT']);sys.path.insert(0,str(sdk/'cookbook'))
        try:images=producer.module('typed_preview_actual_sdk_images',sdk/'cookbook/images.py')
        finally:sys.path.pop(0)
        policy=dict(recipe='react-native',project='demo',image_base='ghcr.io/owner/demo',
            shipping_components=['gate','api','config'],test_groups=self.definition['tests'],
            artifact_definition='deploy/release.json',release_metadata=[])
        modules=(SimpleNamespace(declaration=lambda *_:copy.deepcopy(policy)),
            SimpleNamespace(load=lambda _:copy.deepcopy(self.config),ci_settings=sdk_configuration.ci_settings,
                            __file__=str(self.tools/'cookbook/configuration.py')),images)
        nested=self.root/'_preview_source'
        self.git(self.root,'clone','-q','--no-hardlinks',str(self.source),str(nested))
        with patch.object(producer.preview,'tools_modules',return_value=modules),producer.preview.owning_checkout(self.root):
            config,definition=producer.preview.source_policy({'policy':policy},Path('_preview_source'),self.tools)
            self.assertEqual(definition['project'],'demo');self.assertEqual(Path.cwd(),self.root)
            # A regular but untracked declaration must remain refused; selecting
            # the owning cwd does not weaken the SDK's Git ownership boundary.
            (nested/'deploy/untracked.json').write_bytes((nested/'deploy/release.json').read_bytes())
            self.config['artifacts']['definition']='deploy/untracked.json'
            with self.assertRaisesRegex(ValueError,'tracked caller-owned'):
                producer.preview.source_policy({'policy':policy},Path('_preview_source'),self.tools)
            self.assertEqual(Path.cwd(),self.root)

    def test_failed_group_mixed_attempt_tampered_archive_and_wrong_writer_cannot_login(self):
        for change in ['group','attempt','archive','writer','deadline']:
            with self.subTest(change=change):
                incoming,_=self.evidence();calls=[]
                if change=='group':
                    p=incoming/'.artifacts/verification/test.json';v=json.loads(p.read_text());v['test_receipts']['unit']['source_commit']='e'*40;p.write_text(json.dumps(v))
                elif change=='attempt':
                    p=incoming/'.artifacts/cookbook-validation.json';v=json.loads(p.read_text());v['execution_context']['GITHUB_RUN_ATTEMPT']='1';p.write_text(json.dumps(v))
                elif change=='archive':(incoming/'.artifacts/release/images.tar.gz').write_bytes(b'unverified bytes')
                elif change=='writer':self.environment['PRODUCER_TIMEOUT']='15'
                else:self.jobs['jobs'][0]['started_at']=(datetime.now(timezone.utc)-timedelta(minutes=61)).isoformat()
                with self.assertRaises(ValueError):self.run_publication(incoming,lambda *a,**k:calls.append(a))
                self.assertEqual(calls,[])
                import shutil
                shutil.rmtree(incoming);self.environment['PRODUCER_TIMEOUT']='60'

    @unittest.skipUnless(sys.platform == 'linux', 'owned descendant handles are Linux runner capabilities')
    def test_expired_command_deadline_reaps_its_actual_detached_descendant(self):
        tools=Path(os.environ['TOOLS_ROOT']);program=self.directory/'owned.py';identity=self.directory/'child.pid'
        program.write_text('import pathlib,subprocess,sys,time\n'
            'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],start_new_session=True)\n'
            'pathlib.Path(sys.argv[1]).write_text(str(child.pid))\ntime.sleep(60)\n')
        with self.assertRaises(subprocess.TimeoutExpired):
            producer.bounded([sys.executable,str(program),str(identity)],tools=tools,
                deadline=datetime.now(timezone.utc)+timedelta(seconds=1.8),cwd=self.directory,env=dict(os.environ))
        with self.assertRaises(ProcessLookupError):os.kill(int(identity.read_text()),0)

    def test_native_packet_is_safely_inventoried_before_any_file_is_extracted(self):
        packet=self.directory/'packet.zip'
        with zipfile.ZipFile(packet,'w') as archive:archive.writestr('.artifacts/data.bin',b'accepted bytes')
        destination=self.directory/'extracted';producer.extract_packet(packet,destination)
        self.assertEqual((destination/'.artifacts/data.bin').read_bytes(),b'accepted bytes')
        self.assertEqual((destination/'.artifacts/data.bin').stat().st_mode&0o777,0o600)
        for kind in ['traversal','symlink','duplicate']:
            with zipfile.ZipFile(packet,'w') as archive:
                archive.writestr('first.bin',b'first')
                if kind=='symlink':
                    entry=zipfile.ZipInfo('linked');entry.external_attr=0o120777<<16;archive.writestr(entry,b'../../escape')
                elif kind=='traversal':archive.writestr('../escape',b'unsafe')
                else:
                    import warnings
                    with warnings.catch_warnings():warnings.simplefilter('ignore');archive.writestr('first.bin',b'duplicate')
            target=self.directory/('unsafe-'+kind)
            with self.subTest(kind=kind),self.assertRaises(ValueError):producer.extract_packet(packet,target)
            self.assertFalse(target.exists())

    def test_trusted_checkout_retrieves_only_the_exact_commit_without_running_source_hooks_or_mise(self):
        destination=self.directory/'retrieved';values=self.environment|{
            'PREVIEW_PRODUCER_PROCESS_HELPER':str(Path(os.environ['TOOLS_ROOT'])/'helpers/dev.py')}
        commands=[];deadline=datetime.now(timezone.utc)+timedelta(seconds=20)
        def local(command,**options):
            commands.append(command);self.assertEqual(options['deadline'],deadline)
            self.assertNotIn('fixture-token',' '.join(command));self.assertEqual(command[0],'git')
            self.assertEqual(options['env']['GIT_CONFIG_VALUE_1'],'/dev/null')
            actual=list(command)
            if actual[1:4]==['remote','add','origin']:actual[-1]=str(self.source)
            producer.bounded(actual,**options)
        producer.retrieve(destination,'owner/demo',self.head,values,self.tools,deadline,local)
        self.assertEqual(self.git(destination,'rev-parse','HEAD'),self.head)
        self.assertEqual(len(commands),4)
        self.assertNotIn('fixture-token',(destination/'.git/config').read_text())
        with self.assertRaises(ValueError):producer.retrieve(self.directory/'expired','owner/demo',self.head,values,
            self.tools,datetime.now(timezone.utc)-timedelta(seconds=1))

    def test_writer_refuses_expired_or_malicious_packets_before_publication(self):
        packet=self.directory/'transport.zip'
        with zipfile.ZipFile(packet,'w') as archive:archive.writestr('../escape',b'unsafe')
        artifact=dict(id=1,name=f'cookbook-full-{self.head}-123-2',expired=False,size_in_bytes=packet.stat().st_size,
            digest='sha256:'+hashlib.sha256(packet.read_bytes()).hexdigest())
        environment=self.environment|{'PREVIEW_PRODUCER_DEADLINE':producer.remaining(self.plan,self.jobs)[1].isoformat(),
            'PLAN':json.dumps(self.plan)}
        def lookup(path):
            return {'total_count':1,'artifacts':[artifact]} if path.endswith('/artifacts?per_page=100') else self.lookup(path)
        def download(command,**options):options['output'].write(packet.read_bytes())
        for kind in ['expired','digest','unsafe']:
            original=self.jobs['jobs'][0]['started_at']
            if kind=='expired':self.jobs['jobs'][0]['started_at']=(datetime.now(timezone.utc)-timedelta(minutes=61)).isoformat()
            if kind=='digest':artifact['digest']='sha256:'+'e'*64
            with self.subTest(kind=kind),patch.object(producer,'retrieve') as retrieve,\
                    patch.object(producer.preview,'accepted_automation'),patch.object(producer,'publication') as publish:
                with self.assertRaises(ValueError):producer.run_writer(self.root,self.tools,self.source,self.directory/'packet-input',environment,lookup,download)
                publish.assert_not_called()
                if kind=='expired':retrieve.assert_not_called()
            self.jobs['jobs'][0]['started_at']=original;artifact['digest']='sha256:'+hashlib.sha256(packet.read_bytes()).hexdigest()

    def test_actual_workflow_bootstrap_refuses_expired_metadata_and_changed_sdk_bytes_before_git(self):
        workflow=yaml.safe_load((ROOT/'.github/workflows/cookbook-preview-producer-publish.yml').read_text())
        body=workflow['jobs']['publish']['steps'][0]['run']
        program=body.split("python3 - <<'PYTHON'\n",1)[1].rsplit('\nPYTHON',1)[0]
        environment=self.environment|{'PLAN':json.dumps(self.plan),'TOOLS_REF':self.tools_ref,'KOMIZO_REF':'d'*40,
            'RUNNER_TEMP':str(self.directory)}
        helper=b'raise AssertionError("untrusted SDK bytes executed")\n'
        def response(request,**_):
            url=request.full_url
            if '/jobs?' in url:value=self.jobs
            elif '/compare/' in url:value={'status':'ahead'}
            else:
                import base64
                value={'type':'file','encoding':'base64','size':len(helper),'content':base64.b64encode(helper).decode(),'sha':'e'*40}
            return io.BytesIO(json.dumps(value).encode())
        for kind in ['expired','changed-helper']:
            original=self.jobs['jobs'][0]['started_at']
            if kind=='expired':self.jobs['jobs'][0]['started_at']=(datetime.now(timezone.utc)-timedelta(minutes=61)).isoformat()
            with self.subTest(kind=kind),patch.dict(os.environ,environment,clear=True),\
                    patch('urllib.request.urlopen',side_effect=response),patch('subprocess.Popen') as execute:
                with self.assertRaises(ValueError):exec(compile(program,'actual-writer-bootstrap','exec'),{})
                execute.assert_not_called()
            self.jobs['jobs'][0]['started_at']=original


if __name__=='__main__':unittest.main()
