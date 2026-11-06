"""Preview event, source, publication and credential admission without host effects."""
from contextlib import redirect_stdout
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(os.environ['TOOLS_ROOT'])/'cookbook'))
import configuration as sdk_configuration
spec=importlib.util.spec_from_file_location('preview_authority',ROOT/'scripts/preview.py')
preview=importlib.util.module_from_spec(spec);spec.loader.exec_module(preview)


class PreviewAuthority(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='preview authority ')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.head,self.base,self.tree='a'*40,'b'*40,'c'*40
        self.environment=dict(GITHUB_REPOSITORY='owner/demo',GITHUB_SHA=self.base,
            GITHUB_WORKFLOW_SHA=self.base,GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='issue_comment',
            GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/pr-preview.yml@refs/heads/main',
            GITHUB_SERVER_URL='https://github.com',GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='2')
        self.event=dict(action='created',issue=dict(number=42,pull_request={'url':'fixture'}),
            comment=dict(body=' /preview \n',author_association='OWNER'))
        self.request=dict(number=42,state='open',draft=False,author_association='OWNER',user={'login':'owner'},
            head=dict(sha=self.head,ref='feature',repo={'full_name':'owner/demo'}),
            base=dict(sha=self.base,ref='main',repo={'full_name':'owner/demo'}),labels=[{'name':'preview'}])
        self.policy=dict(definition='deploy/preview.json',recipe='react-native',project='demo',backend=True,
            image_base='ghcr.io/owner/demo',shipping_components=['gate','api','config'],components=['gate','api'],
            test_groups=['lint','unit','security','integration','e2e'],artifact_definition='deploy/release.json',
            release_metadata=[],origin_layout='subdomain',health_paths={'app':'/','api':'/readyz'},
            secrets=['CLERK_SECRET_KEY'],origin_variables={'ALLOWED_ORIGINS':['app','api'],
                'CLERK_AUTHORIZED_PARTIES':['app']},environment={'DEV_AUTH_ENABLED':'true'})
        self.plan=dict(version=1,repository='owner/demo',controller=self.base,controller_tree=self.tree,
            trigger='issue_comment',pr=42,head=self.head,base=self.base,action='up',policy=self.policy)

    def lookup(self,*arguments):
        return copy.deepcopy(self.request)

    def git(self,root,*arguments):
        return self.tree if arguments==('rev-parse','HEAD^{tree}') else self.base

    def context(self):
        return (patch.object(preview,'git',side_effect=self.git),
            patch.object(preview,'native_policy',return_value=self.policy),
            patch.object(preview,'read_event',side_effect=lambda _:copy.deepcopy(self.event)))

    def request_plan(self):
        a,b,c=self.context()
        with a,b,c:
            return preview.request_plan(self.root,self.root,'deploy/preview.json',self.environment,self.lookup)

    def validate(self,plan=None):
        a,b,c=self.context()
        with a,b,c:
            return preview.validate_plan(json.dumps(plan or self.plan),self.root,self.root,self.environment,self.lookup)

    def test_comment_and_labeled_push_retain_opt_in_source_identity(self):
        result=self.request_plan()
        self.assertEqual(json.loads(result['plan']),self.plan)
        self.assertEqual(self.validate(),self.plan)
        self.environment['GITHUB_EVENT_NAME']='pull_request_target'
        self.event={'action':'synchronize','pull_request':copy.deepcopy(self.request)}
        result=self.request_plan()
        self.assertEqual(result['action'],'up')
        self.assertEqual(result['head'],self.head)
        self.request['labels']=[]
        with self.assertRaisesRegex(ValueError,'no longer carries'):self.request_plan()

    def test_unrequested_untrusted_fork_draft_and_bot_events_never_reach_full(self):
        for change in ['ordinary','untrusted','fork','draft','bot']:
            event,request=copy.deepcopy(self.event),copy.deepcopy(self.request)
            if change=='ordinary':self.event['comment']['body']='looks good'
            elif change=='untrusted':self.event['comment']['author_association']='CONTRIBUTOR'
            elif change=='fork':self.request['head']['repo']['full_name']='fork/demo'
            elif change=='draft':self.request['draft']=True
            else:self.request['user']['login']='dependabot[bot]'
            with self.subTest(change=change):self.assertEqual(self.request_plan()['enabled'],'false')
            self.event,self.request=event,request

    def test_closed_down_requires_no_current_build_or_open_pr(self):
        self.environment['GITHUB_EVENT_NAME']='pull_request_target'
        self.request.update(state='closed',draft=True,author_association='CONTRIBUTOR')
        self.event={'action':'closed','pull_request':copy.deepcopy(self.request)}
        result=self.request_plan();self.assertEqual(result['action'],'down')
        self.assertEqual(self.validate(json.loads(result['plan']))['action'],'down')

    def test_stale_head_base_controller_and_injected_policy_are_refused(self):
        for field in ['head','base']:
            old=self.request[field]['sha'];self.request[field]['sha']='d'*40
            with self.subTest(field=field),self.assertRaises(ValueError):self.validate()
            self.request[field]['sha']=old
        for field,value in [('controller','d'*40),('controller_tree','d'*40),('head','d'*40),
                            ('pr',True),('repository','other/demo')]:
            plan=copy.deepcopy(self.plan);plan[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.validate(plan)
        plan=copy.deepcopy(self.plan);plan['policy']['secrets'].append('PRODUCTION_KEY')
        with self.assertRaisesRegex(ValueError,'trusted main'):self.validate(plan)

    def test_source_cannot_redeclare_publication_family_checks_or_host_policy(self):
        config={'recipe':'react-native','artifacts':{'definition':'deploy/release.json'}}
        definition={'project':'demo','image_base':'ghcr.io/owner/demo','tests':self.policy['test_groups']}
        declaration=Mock(return_value=self.policy)
        image=SimpleNamespace(definition=Mock(return_value=definition),components=Mock(return_value=['gate','api','config']))
        modules=(SimpleNamespace(declaration=declaration),SimpleNamespace(load=lambda _:config,ci_settings=sdk_configuration.ci_settings,
                                                                        __file__=str(self.root/'cookbook/configuration.py')),image)
        with patch.object(preview,'tools_modules',return_value=modules):
            preview.source_policy(self.plan,self.root,self.root)
            for key,value in [('project','other'),('image_base','ghcr.io/other/demo'),('tests',['lint','unit'])]:
                old=definition[key];definition[key]=value
                with self.subTest(key=key),self.assertRaisesRegex(ValueError,'trusted Preview'):preview.source_policy(self.plan,self.root,self.root)
                definition[key]=old
            declaration.return_value=self.policy|{'origin_layout':'shared-path'}
            with self.assertRaisesRegex(ValueError,'credentials, origins'):preview.source_policy(self.plan,self.root,self.root)

    def test_api_target_origin_isolation_and_static_host_routes(self):
        urls=preview.targets(self.plan,'https://demo-pr42.example.test','https://demo-pr42-api.example.test')
        self.assertEqual(urls['app']['origin'],'https://demo-pr42.example.test')
        for app,api in [('https://demo.example.test/42','https://demo-api.example.test/42'),
                        ('https://demo-pr41.example.test','https://demo-pr41-api.example.test'),
                        ('https://demo-pr42.example.test','https://demo-pr42-api.other.test'),
                        ('https://user:pass@demo-pr42.example.test','https://demo-pr42-api.example.test'),
                        ('https://demo-pr42','https://demo-pr42-api')]:
            with self.subTest(app=app),self.assertRaises(ValueError):preview.targets(self.plan,app,api)
        self.policy.update(backend=False,origin_layout='resolved',secrets=[],origin_variables={})
        self.assertEqual(preview.targets(self.plan,'https://demo.example.test/42','')['app']['url'],'https://demo.example.test/42')
        self.assertIsNone(preview.stack_environment(self.plan,{},{}))

    def test_development_keys_only_and_secret_stdin_transport(self):
        urls=preview.targets(self.plan,'https://demo-pr42.example.test','https://demo-pr42-api.example.test')
        environment=dict(PREVIEW_CLERK_SECRET_KEY='sk_test_fixture',PREVIEW_CLERK_PUBLISHABLE_KEY='pk_test_fixture',
            PREVIEW_OPENROUTER_API_KEY='not-declared')
        body=preview.stack_environment(self.plan,urls,environment)
        self.assertIn('ALLOWED_ORIGINS=https://demo-pr42.example.test,https://demo-pr42-api.example.test\n',body)
        self.assertNotIn('OPENROUTER',body)
        execute=Mock()
        with redirect_stdout(io.StringIO()):preview.write_environment(self.plan,urls,environment,execute)
        arguments,options=execute.call_args
        self.assertEqual(options['input'],body)
        self.assertNotIn('sk_test_fixture',' '.join(arguments[0]))
        self.assertEqual(options['stdout'],subprocess.DEVNULL);self.assertEqual(options['stderr'],subprocess.DEVNULL)
        for key,value in [('PREVIEW_CLERK_SECRET_KEY','sk_live_fixture'),('PREVIEW_CLERK_PUBLISHABLE_KEY','pk_live_fixture'),
                          ('PREVIEW_CLERK_SECRET_KEY','sk_test_fixture\nEVIL=true')]:
            with self.subTest(key=key),self.assertRaises(ValueError):preview.stack_environment(self.plan,urls,environment|{key:value})

    def publication_evidence(self):
        return dict(version=1,plan=self.plan,publication=dict(revision=self.head,tested_commit=self.head,
            archive_sha256='e'*64,build_provenance={'source_commit':self.head},test_provenance={'tested_commit':self.head},
            publisher_context=self.environment|{'GITHUB_JOB':'publish'}))

    def test_declared_provider_urls_derive_only_from_the_actual_development_public_frontend(self):
        self.policy['clerk_urls']={'CLERK_ISSUER':'issuer','CLERK_JWKS_URL':'jwks'}
        urls=preview.targets(self.plan,'https://demo-pr42.example.test','https://demo-pr42-api.example.test')
        def key(host):return 'pk_test_'+base64.urlsafe_b64encode(host.encode()).decode().rstrip('=')
        environment=dict(PREVIEW_CLERK_SECRET_KEY='sk_test_fixture',
            PREVIEW_CLERK_PUBLISHABLE_KEY=key('fixture.clerk.accounts.dev$'))
        body=preview.stack_environment(self.plan,urls,environment)
        self.assertIn('CLERK_ISSUER=https://fixture.clerk.accounts.dev\n',body)
        self.assertIn('CLERK_JWKS_URL=https://fixture.clerk.accounts.dev/.well-known/jwks.json\n',body)
        for public in [key('production.example.test$'),key('fixture.clerk.accounts.dev'),
                key('evil.example.test/fixture.clerk.accounts.dev$'),key('-invalid.clerk.accounts.dev$'),
                'pk_test_fixture','pk_live_'+key('fixture.clerk.accounts.dev$').removeprefix('pk_test_')]:
            with self.subTest(public=public),self.assertRaises(ValueError):
                preview.stack_environment(self.plan,urls,environment|{'PREVIEW_CLERK_PUBLISHABLE_KEY':public})

    def test_host_effects_require_actual_successful_same_attempt_publisher(self):
        evidence=self.publication_evidence();path=self.root/'publication.json'
        jobs=dict(total_count=1,jobs=[dict(name='publish / Publish exact Preview images',conclusion='success',head_sha=self.base)])
        def run():
            path.write_text(json.dumps(evidence));preview.publication_evidence(self.plan,path,self.environment,lambda _:jobs)
        run()
        for key,value in [('revision','f'*40),('tested_commit','f'*40),('archive_sha256','bad')]:
            old=evidence['publication'][key];evidence['publication'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):run()
            evidence['publication'][key]=old
        for key,value in [('GITHUB_RUN_ATTEMPT','1'),('GITHUB_SHA',self.head),('GITHUB_JOB','validate')]:
            old=evidence['publication']['publisher_context'][key];evidence['publication']['publisher_context'][key]=value
            with self.subTest(context=key),self.assertRaises(ValueError):run()
            evidence['publication']['publisher_context'][key]=old
        for key,value in [('conclusion','failure'),('head_sha',self.head),('name','unrelated publisher')]:
            old=jobs['jobs'][0][key];jobs['jobs'][0][key]=value
            with self.subTest(job=key),self.assertRaises(ValueError):run()
            jobs['jobs'][0][key]=old
        jobs['total_count']=2
        with self.assertRaisesRegex(ValueError,'complete current-attempt'):run()

    def test_publisher_admits_real_same_run_proof_and_stages_only_its_closed_archive(self):
        source=self.root/'source';source.mkdir();(source/'cookbook.toml').write_text('recipe = "react-native"\n')
        incoming=self.root/'incoming';archive=incoming/'.artifacts/release';archive.mkdir(parents=True)
        names=['images.tar.gz','images.tar.gz.sha256','images.json','release.json']
        for name in names:(archive/name).write_bytes(b'[]' if name=='images.json' else name.encode())
        proof=dict(version=1,source_commit=self.head,source_tree=self.tree,cookbook_commit='d'*40,
            recipe='react-native',status='passed',tracked_source_clean=True,build_verified=True,tests_verified=True,
            test_groups=self.policy['test_groups'],head=self.head,base=self.base,
            configuration_sha256=hashlib.sha256((source/'cookbook.toml').read_bytes()).hexdigest(),
            execution_context=self.environment|{'GITHUB_JOB':'validate'},
            artifacts={'.artifacts/release/'+name:hashlib.sha256((archive/name).read_bytes()).hexdigest() for name in names})
        proof_path=incoming/'.artifacts/cookbook-validation.json'
        jobs=dict(total_count=1,jobs=[dict(name='full / Full source checks / Make full',conclusion='success',
            head_sha=self.base,labels=['ubuntu-24.04'])])
        execute=Mock()
        def publish(*arguments,**options):
            destination=source/'.artifacts/release'
            self.assertEqual({p.name for p in destination.iterdir()},set(names))
            self.assertEqual((destination/'images.tar.gz').read_bytes(),b'images.tar.gz')
            self.assertEqual(options['env']['REQUEST_HEAD'],self.head)
            self.assertEqual(options['env']['GITHUB_SHA'],self.base)
            self.assertEqual(options['env']['COOKBOOK_PREVIEW_NUMBER'],'42')
            (destination/'publication.json').write_text(json.dumps(self.publication_evidence()['publication']|
                {'archive_sha256':proof['artifacts']['.artifacts/release/images.tar.gz']}))
        execute.side_effect=publish
        config={'recipe':'react-native','ci':{'runner':'ubuntu-24.04'}}
        def lookup(path):return jobs if '/actions/' in path else self.request
        def invoke():
            proof_path.write_text(json.dumps(proof))
            with patch.object(preview,'git',side_effect=lambda _,*args:self.tree if args[-1]=='HEAD^{tree}' else self.head),\
                 patch.object(preview,'source_policy',return_value=(config,{})),\
                 patch.object(preview.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
                preview.publication(self.plan,self.root,source,self.root,'d'*40,incoming,self.environment,lookup,execute)
        for mutation in ['attempt','host','archive','arbitrary']:
            original=copy.deepcopy(proof)
            if mutation=='attempt':proof['execution_context']['GITHUB_RUN_ATTEMPT']='1'
            elif mutation=='host':jobs['jobs'][0]['conclusion']='failure'
            elif mutation=='archive':proof['artifacts']['.artifacts/release/images.tar.gz']='f'*64
            else:(archive/'arbitrary.py').write_text('raise SystemExit(1)')
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):invoke()
            self.assertFalse(execute.called)
            self.assertFalse((source/'.artifacts/release').exists())
            proof=original;jobs['jobs'][0]['conclusion']='success'
            (archive/'arbitrary.py').unlink(missing_ok=True)
        invoke()
        self.assertEqual(execute.call_count,1)
        command=execute.call_args.args[0]
        self.assertEqual(command[1],str((self.root/'helpers/release.py').resolve()))
        self.assertNotIn('make',command)
        self.assertEqual(json.loads((self.root/'.artifacts/preview-publication.json').read_text())['plan'],self.plan)

    def test_reusable_roles_keep_source_execution_separate_from_package_and_host_authority(self):
        workflows=ROOT/'.github/workflows'
        publisher=(workflows/'cookbook-preview-publish.yml').read_text()
        effects=(workflows/'cookbook-preview-effects.yml').read_text()
        request=(workflows/'cookbook-preview-request.yml').read_text()
        self.assertNotIn('packages: write',request)
        self.assertNotIn('environment: Preview',publisher)
        self.assertNotIn('secrets.CLERK_SECRET_KEY',publisher)
        self.assertNotIn('secrets.OPENROUTER_API_KEY',publisher)
        self.assertNotIn('make ',publisher)
        self.assertNotIn('npm ',publisher)
        self.assertNotIn('uses: ./_preview_source',publisher)
        self.assertIn('persist-credentials: false',publisher)
        self.assertIn('name: cookbook-full-${{ steps.plan.outputs.head }}-${{ github.run_id }}-${{ github.run_attempt }}',publisher)
        self.assertIn('environment: Preview',effects)
        self.assertNotIn('path: _preview_source',effects)
        self.assertNotIn('packages: write',effects)
        self.assertLess(effects.index('scripts/preview.py evidence'),effects.index('uses: ./_preview_actions/connect'))
        self.assertLess(effects.index('scripts/preview.py environment'),effects.index('name: Apply preview up or down'))
        self.assertIn("steps.plan.outputs.action == 'up' && steps.plan.outputs.backend == 'true'",effects)

    def test_exact_api_revision_is_required_without_provider_calls(self):
        urls=preview.targets(self.plan,'https://demo-pr42.example.test','https://demo-pr42-api.example.test')
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self,_):return json.dumps({'revision':self.head}).encode()
        response=Response();response.head=self.head
        opener=Mock(return_value=response)
        preview.verify_health(self.plan,urls,opener)
        self.assertEqual([call.args[0].full_url for call in opener.call_args_list],
            ['https://demo-pr42.example.test/','https://demo-pr42-api.example.test/readyz'])
        response.head='f'*40
        with self.assertRaisesRegex(ValueError,'another source'):preview.verify_health(self.plan,urls,opener)


if __name__=='__main__':unittest.main()
