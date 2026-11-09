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
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(os.environ['RECIPE_LEGACY_TEST_SDK'])/'cookbook'))
import configuration as sdk_configuration
spec=importlib.util.spec_from_file_location('preview_authority',ROOT/'scripts/preview.py')
preview=importlib.util.module_from_spec(spec);spec.loader.exec_module(preview)


class PreviewAuthority(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='preview authority ')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.head,self.base,self.tree='a'*40,'b'*40,'c'*40
        self.environment=dict(GITHUB_REPOSITORY='owner/demo',GITHUB_REPOSITORY_ID='123',GITHUB_SHA=self.base,
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
        config={'recipe':'react-native','artifacts':{}}
        definition={'project':'demo','image_base':'ghcr.io/owner/demo','tests':self.policy['test_groups']}
        declaration=Mock(return_value=self.policy)
        image=SimpleNamespace(definition=Mock(return_value=definition),components=Mock(return_value=['gate','api','config']))
        modules=(SimpleNamespace(declaration=declaration),SimpleNamespace(load=lambda _:config,ci_settings=sdk_configuration.ci_settings,
                                                                        parse_source_data=lambda raw,**_options:tomllib.loads(raw.decode() if isinstance(raw,bytes) else raw),
                                                                        release_definition=lambda _config:'deploy/release.json',
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
        urls=preview.targets(self.plan,'https://pr-42.example.test','https://pr-42-api.example.test')
        self.assertEqual(urls['app']['origin'],'https://pr-42.example.test')
        self.assertEqual(preview.targets(self.plan,'https://pr-42.example.test','https://pr-42.example.test/api')['api']['origin'],'https://pr-42.example.test')
        for app,api in [('https://pr-41.example.test','https://pr-41-api.example.test'),
                        ('https://pr-42.example.test','https://pr-42-api.other.test'),
                        ('https://user:pass@pr-42.example.test','https://pr-42-api.example.test'),
                        ('https://pr-42','https://pr-42-api'),('https://pr-42.example.test','https://pr-42.example.test')]:
            with self.subTest(app=app),self.assertRaises(ValueError): preview.targets(self.plan,app,api)
        self.policy.update(backend=False)
        self.assertEqual(preview.targets(self.plan,'https://pr-42.example.test','')['app']['url'],'https://pr-42.example.test')

    def publication_evidence(self):
        from test_release_packet import descriptor
        import oci_registry
        release=descriptor(revision=self.head)
        release['application']='demo';release['source'].update(repository='owner/demo',repository_id='123')
        release['artifacts']['web']['reference']=release['artifacts']['web']['reference'].replace('example/mailroom','owner/demo')
        return {'version':1,'plan':self.plan,'release':release,'publication':{
            'kind':'komizo.publication','version':1,'release_digest':oci_registry.digest(oci_registry.canonical(release)),
            'published':{name:artifact['reference'] for name,artifact in release['artifacts'].items()},
            'execution_context':self.environment|{'GITHUB_JOB':'publish'}}}

    def test_host_effects_require_actual_successful_same_attempt_publisher(self):
        evidence=self.publication_evidence();path=self.root/'publication.json'
        jobs={'total_count':1,'jobs':[{'name':'publish / Publish exact Preview images','conclusion':'success','head_sha':self.base}]}
        def check():
            path.write_text(json.dumps(evidence)); preview.publication_evidence(self.plan,path,self.environment,lambda _:jobs)
        check()
        original=copy.deepcopy(evidence)
        for change in [{'release_digest':'sha256:'+'e'*64},{'published':{}}]:
            evidence['publication'].update(change)
            with self.assertRaises(ValueError): check()
            evidence=copy.deepcopy(original)
        for key,value in [('GITHUB_RUN_ATTEMPT','1'),('GITHUB_SHA',self.head),('GITHUB_JOB','validate')]:
            evidence['publication']['execution_context'][key]=value
            with self.assertRaises(ValueError): check()
            evidence=copy.deepcopy(original)
        jobs['jobs'][0]['conclusion']='failure'
        with self.assertRaises(ValueError): check()
        jobs['jobs'][0]['conclusion']='success';jobs['total_count']=2
        with self.assertRaises(ValueError): check()

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
        self.assertLess(effects.index('scripts/authorize.py'),effects.index('scripts/preview.py apply'))
        self.assertNotIn('PREVIEW_CLERK_SECRET_KEY',effects)
        self.assertIn('id-token: write',effects)

    def test_exact_api_revision_is_required_without_provider_calls(self):
        urls=preview.targets(self.plan,'https://pr-42.example.test','https://pr-42-api.example.test')
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self,_):return json.dumps({'revision':self.head}).encode()
        response=Response();response.head=self.head
        opener=Mock(return_value=response)
        preview.verify_health(self.plan,urls,opener)
        self.assertEqual([call.args[0].full_url for call in opener.call_args_list],
            ['https://pr-42.example.test/','https://pr-42-api.example.test/readyz'])
        response.head='f'*40
        with self.assertRaisesRegex(ValueError,'another source'):preview.verify_health(self.plan,urls,opener)

    def extracted_context(self):
        identities={'cookbook_repository':'nicodes/cookbook','cookbook_commit':'d'*40,
            'tools_repository':'nicodes/tools','tools_commit':'e'*40}
        context=SimpleNamespace(identities=lambda:identities,tools=self.root/'actual-tools')
        context.require_proof=lambda proof:preview.recipe_context.Context.require_proof(context,proof)
        return context

    def test_extracted_plans_require_both_sources_and_do_not_fall_back_to_historical_shape(self):
        context=self.extracted_context()
        a,b,c=self.context()
        with a,b,c:
            result=preview.request_plan(self.root,self.root,'deploy/preview.json',self.environment,self.lookup,context=context)
            plan=json.loads(result['plan'])
            self.assertEqual(plan,self.plan|context.identities()|{'entrypoint_contract':'mise-v1'})
            self.assertEqual(preview.validate_plan(result['plan'],self.root,self.root,self.environment,self.lookup,context=context),plan)
            for change in [{'tools_commit':'d'*40},{'cookbook_repository':'nicodes/tools'},
                           {'entrypoint_contract':'make-v0'}]:
                with self.subTest(change=change),self.assertRaises(ValueError):
                    preview.validate_plan(json.dumps(plan|change),self.root,self.root,self.environment,self.lookup,context=context)
            with self.assertRaises(ValueError):
                preview.validate_plan(json.dumps(self.plan),self.root,self.root,self.environment,self.lookup,context=context)
            with self.assertRaises(ValueError):
                preview.validate_plan(result['plan'],self.root,self.root,self.environment,self.lookup)

    def test_extracted_publication_evidence_rejects_aliased_or_missing_helper_binding(self):
        context=self.extracted_context()
        plan=self.plan|context.identities()|{'entrypoint_contract':'mise-v1'}
        evidence=self.publication_evidence()|context.identities()|{'entrypoint_contract':'mise-v1','plan':plan}
        path=self.root/'publication.json'
        jobs=dict(total_count=1,jobs=[dict(name='publish / Publish exact Preview images',conclusion='success',head_sha=self.base)])
        path.write_text(json.dumps(evidence))
        preview.publication_evidence(plan,path,self.environment,lambda _:jobs,context=context)
        for change in [{'tools_commit':'d'*40},{'tools_repository':'nicodes/cookbook'},
                       {'entrypoint_contract':'make-v0'}]:
            path.write_text(json.dumps(evidence|change))
            with self.subTest(change=change),self.assertRaises(ValueError):
                preview.publication_evidence(plan,path,self.environment,lambda _:jobs,context=context)



if __name__=='__main__':unittest.main()
