#!/usr/bin/env python3
"""Trusted Preview controller; native application code runs only in readonly validation jobs."""
import argparse
from datetime import datetime, timezone, timedelta
from functools import wraps
import signal
import math
import time
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from urllib.parse import urlsplit
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT/'scripts'))
import recipe_data
import recipe_context
import publication as native_publication
SHA=re.compile('[a-f0-9]{40}')
REPOSITORY=re.compile('[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')


def git(root,*arguments):
    return subprocess.check_output(['git','-C',str(root),*arguments],text=True,timeout=30).strip()


def api(path,method='GET',body=None):
    command=['gh','api','--method',method,path]
    if body is not None:command+=['--input','-']
    result=subprocess.run(command,input=json.dumps(body) if body is not None else None,
                          capture_output=True,text=True,check=True,timeout=30)
    return json.loads(result.stdout) if result.stdout.strip() else None


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


def tools_modules(tools):
    sys.path.insert(0,str(tools.resolve()/'cookbook'))
    import preview_workflows,configuration,images
    return preview_workflows,configuration,images


@contextmanager
def owning_checkout(root):
    """Read trusted SDK declarations against the Git checkout that owns them."""
    previous=Path.cwd()
    try:
        os.chdir(root.resolve())
        yield
    finally:
        os.chdir(previous)


def controller(environment):
    repository=environment.get('GITHUB_REPOSITORY','')
    source=environment.get('GITHUB_SHA','')
    if (not REPOSITORY.fullmatch(repository) or not SHA.fullmatch(source)
            or environment.get('GITHUB_REF')!='refs/heads/main'
            or environment.get('GITHUB_WORKFLOW_SHA')!=source
            or environment.get('GITHUB_WORKFLOW_REF')!=repository+'/.github/workflows/pr-preview.yml@refs/heads/main'
            or environment.get('GITHUB_EVENT_NAME') not in {'pull_request_target','issue_comment'}):
        raise ValueError('Preview requires its trusted-main controller')
    return repository,source


def accepted_automation(tools,tools_ref,komizo_ref,lookup=api,*,cookbook_ref=None,supplied_tools=None):
    if cookbook_ref is not None:
        if tools_ref is not None or not SHA.fullmatch(komizo_ref):
            raise ValueError('select one real Cookbook source without a historical Tools alias')
        if (git(ROOT,'rev-parse','HEAD')!=komizo_ref
                or Path(git(ROOT,'rev-parse','--show-toplevel')).resolve()!=ROOT.resolve()
                or ROOT.is_symlink() or any(parent.is_symlink() for parent in ROOT.parents)
                or (ROOT/'.git').is_symlink()
                or recipe_context.discovery_module().canonical_origin(git(ROOT,'config','--local','--get-all','remote.origin.url'))!='nicodes/komizo-actions'
                or git(ROOT,'status','--porcelain','--untracked-files=all','--ignored=matching')):
            raise ValueError('Preview execution source differs from its exact clean owning checkout')
        if lookup(f'repos/nicodes/komizo-actions/compare/{komizo_ref}...main').get('status') not in {'ahead','identical'}:
            raise ValueError('Preview automation has not been accepted on main')
        context=recipe_context.accepted_context(tools,cookbook_ref,lookup,supplied_tools)
        for name in ['preview_workflows','configuration','images']:context.module(name)
        return context
    if supplied_tools is not None:
        raise ValueError('an independent helper location requires the Cookbook source contract')
    if not SHA.fullmatch(tools_ref) or not SHA.fullmatch(komizo_ref):
        raise ValueError('Preview automation requires immutable source pins')
    if git(tools,'rev-parse','HEAD')!=tools_ref or git(ROOT,'rev-parse','HEAD')!=komizo_ref:
        raise ValueError('checked-out Preview automation differs from its accepted pins')
    for repository,revision in [('tools',tools_ref),('komizo-actions',komizo_ref)]:
        if lookup(f'repos/nicodes/{repository}/compare/{revision}...main').get('status') not in {'ahead','identical'}:
            raise ValueError('Preview automation has not been accepted on main')


def sdk_arguments(parser):
    parser.add_argument('--tools',type=Path,help='Historical Tools-hosted recipe SDK location')
    parser.add_argument('--cookbook-root',type=Path)
    parser.add_argument('--cookbook-ref')
    parser.add_argument('--tools-root',type=Path)


def automation(args,environment,default,lookup=api):
    if args.cookbook_root is not None or args.cookbook_ref is not None:
        if args.cookbook_root is None or args.cookbook_ref is None or args.tools is not None:
            raise ValueError('select the complete Cookbook contract or the historical Tools SDK')
        if environment.get('COOKBOOK_REF',args.cookbook_ref)!=args.cookbook_ref:
            raise ValueError('Preview Cookbook CLI revision differs from its trusted workflow source')
        context=accepted_automation(args.cookbook_root,None,environment['KOMIZO_REF'],lookup,
            cookbook_ref=args.cookbook_ref,supplied_tools=args.tools_root)
        return context.cookbook,context.tools,context.cookbook_revision,context,context.environment(environment)
    if args.tools_root is not None or environment.get('COOKBOOK_REF'):
        raise ValueError('Cookbook callers must select their explicit source contract')
    tools=args.tools or Path(default)
    accepted_automation(tools,environment['TOOLS_REF'],environment['KOMIZO_REF'],lookup)
    return tools,tools,environment['TOOLS_REF'],None,environment


def native_policy(root,tools,declaration):
    preview,configuration,_=tools_modules(tools)
    config=recipe_data.load(root,tools,configuration)
    if config.get('preview')!={'definition':declaration}:
        raise ValueError('Preview input differs from its trusted native declaration')
    if recipe_data.entrypoint_revision(root,tools)!=git(tools,'rev-parse','HEAD'):
        raise ValueError('Preview controller must use its exact reviewed cookbook')
    result=preview.declaration(root,config)
    preview.auth_contract(root,config)
    for name in ['cookbook.toml',declaration,result['artifact_definition']]:
        subprocess.run(['git','-C',str(root),'ls-files','--error-unmatch','--',name],
                       check=True,stdout=subprocess.DEVNULL,timeout=10)
    return result


def read_event(environment):
    path=Path(environment['GITHUB_EVENT_PATH'])
    if path.is_symlink() or not path.is_file() or path.stat().st_size>4*1024*1024:
        raise ValueError('Preview event must be bounded runner metadata')
    return json.loads(path.read_text())


def current_pr(repository,number,lookup=api):
    return lookup(f'repos/{repository}/pulls/{number}')


def require_current(plan,request):
    repository=plan['repository']
    if (request.get('number')!=plan['pr'] or request.get('state')!='open' or request.get('draft') is not False
            or request.get('author_association') not in {'OWNER','MEMBER','COLLABORATOR'}
            or request.get('user',{}).get('login')=='dependabot[bot]'
            or request.get('head',{}).get('repo',{}).get('full_name')!=repository
            or request.get('base',{}).get('repo',{}).get('full_name')!=repository
            or request.get('base',{}).get('ref')!='main'
            or request.get('head',{}).get('sha')!=plan['head']
            or request.get('base',{}).get('sha')!=plan['base']):
        raise ValueError('Preview head/base is stale or no longer an authorized same-repository request')
    if plan['trigger']=='pull_request_target' and 'preview' not in {entry.get('name') for entry in request.get('labels',[]) if isinstance(entry,dict)}:
        raise ValueError('the current pull request no longer carries the Preview label')


def request_plan(root,tools,declaration,environment,lookup=api,*,context=None):
    repository,source=controller(environment)
    if git(root,'rev-parse','HEAD')!=source:
        raise ValueError('Preview policy checkout differs from its trusted controller')
    policy=native_policy(root,tools,declaration)
    resolver=module('komizo_preview_request',ROOT/'preview-request/request.py')
    event=read_event(environment)
    event_name='pull_request' if environment['GITHUB_EVENT_NAME']=='pull_request_target' else environment['GITHUB_EVENT_NAME']
    try:
        decision=resolver.resolve(event,event_name,repository,'preview',lambda repo,number:current_pr(repo,number,lookup))
    except resolver.Refused as error:
        return {'enabled':'false','reason':str(error)}
    number=int(decision['pr'])
    current=current_pr(repository,number,lookup)
    head=current.get('head',{}).get('sha')
    base=current.get('base',{}).get('sha')
    if not isinstance(head,str) or not SHA.fullmatch(head) or not isinstance(base,str) or not SHA.fullmatch(base):
        raise ValueError('current PR metadata has invalid source identities')
    plan={'version':1,'repository':repository,'controller':source,'controller_tree':git(root,'rev-parse','HEAD^{tree}'),
          'trigger':environment['GITHUB_EVENT_NAME'],'pr':number,'head':head,'base':base,
          'action':decision['action'],'policy':policy}
    if context is not None:plan.update(context.identities(),entrypoint_contract='mise-v1')
    if plan['action']=='up':
        if decision['sha']!=head or base!=source:
            raise ValueError('Preview request is stale; request the current head and main base')
        require_current(plan,current)
    return {'enabled':'true','action':plan['action'],'pr':str(number),'head':head,'base':base,
            'reason':decision['reason'],'plan':json.dumps(plan,sort_keys=True,separators=(',',':'))}


def validate_plan(raw,root,tools,environment,lookup=api,*,fresh=True,context=None):
    if not isinstance(raw,str) or len(raw.encode())>131072:
        raise ValueError('Preview plan exceeds its bound')
    plan=json.loads(raw)
    expected={'version','repository','controller','controller_tree','trigger','pr','head','base','action','policy'}
    if context is not None:
        expected.update([*context.identities(),'entrypoint_contract'])
        if not isinstance(plan,dict):raise ValueError('invalid Preview controller plan')
        context.require_proof(plan)
    if (not isinstance(plan,dict) or set(plan)!=expected or plan.get('version')!=1
            or type(plan.get('pr')) is not int or not 1<=plan['pr']<=9999999999
            or plan.get('action') not in {'up','down'}
            or any(not isinstance(plan.get(key),str) or not SHA.fullmatch(plan[key]) for key in ['controller','controller_tree','head','base'])):
        raise ValueError('invalid Preview controller plan')
    repository,source=controller(environment)
    if (plan['repository']!=repository or plan['controller']!=source or plan['trigger']!=environment['GITHUB_EVENT_NAME']
            or git(root,'rev-parse','HEAD')!=source or git(root,'rev-parse','HEAD^{tree}')!=plan['controller_tree']):
        raise ValueError('Preview plan belongs to another controller/source')
    if not isinstance(plan['policy'],dict) or not isinstance(plan['policy'].get('definition'),str):
        raise ValueError('Preview plan has no native policy authority')
    if plan['policy']!=native_policy(root,tools,plan['policy']['definition']):
        raise ValueError('Preview policy differs from trusted main')
    event=read_event(environment)
    resolver=module('komizo_preview_request',ROOT/'preview-request/request.py')
    normalized='pull_request' if environment['GITHUB_EVENT_NAME']=='pull_request_target' else environment['GITHUB_EVENT_NAME']
    decision=resolver.resolve(event,normalized,repository,'preview',lambda repo,number:current_pr(repo,number,lookup))
    if decision['action']!=plan['action'] or int(decision['pr'])!=plan['pr']:
        raise ValueError('Preview plan differs from its authorized event')
    if plan['action']=='up':
        if plan['controller']!=plan['base'] or decision['sha']!=plan['head']:
            raise ValueError('Preview source cannot be relabeled or detached from its current base')
        if fresh:require_current(plan,current_pr(repository,plan['pr'],lookup))
    return plan


def source_policy(plan,source_root,tools):
    source_root=source_root.resolve()
    preview,configuration,images=tools_modules(tools)
    config=recipe_data.load(source_root,tools,configuration)
    policy=plan['policy']
    with owning_checkout(source_root):
        definition=images.definition(source_root,config)
        supplied=preview.declaration(source_root,config)
    if (config['recipe']!=policy['recipe'] or definition['project']!=policy['project']
            or definition['image_base']!=policy['image_base']
            or set(images.components(definition))!=set(policy['shipping_components'])
            or set(definition['tests'])!=set(policy['test_groups'])
            or recipe_data.effective_definition(tools,config,configuration)!=policy['artifact_definition']
            or config['artifacts'].get('release_metadata',[])!=policy['release_metadata']):
        raise ValueError('PR source cannot change the trusted Preview publication family, components or required checks')
    if supplied!=policy:
        raise ValueError('PR source cannot override trusted Preview identity, credentials, origins or runner policy')
    return config,definition


def producer_deadline(plan, jobs, now=None):
    now = now or datetime.now(timezone.utc)
    total = plan['policy'].get('producer_timeout_minutes')
    if total is None: return None
    if type(total) is not int or not 1 <= total <= 180 or jobs.get('total_count') != len(jobs.get('jobs', [])):
        raise ValueError('preview requires its complete original aggregate producer budget')
    hosts = [job for job in jobs['jobs'] if job.get('name', '').endswith('/ Make full')]
    if (len(hosts) != 1 or hosts[0].get('conclusion') != 'success' or hosts[0].get('head_sha') != plan['controller']
            or plan['policy']['validation_runner'] not in hosts[0].get('labels', [])):
        raise ValueError('preview aggregate budget requires the actual successful source job')
    try: started = datetime.fromisoformat(hosts[0]['started_at'].replace('Z', '+00:00'))
    except (KeyError, TypeError, ValueError): raise ValueError('preview source job has invalid timing') from None
    if started.tzinfo is None or started > now + timedelta(seconds=5):
        raise ValueError('preview source job has invalid timing')
    deadline = started + timedelta(minutes=total)
    seconds = math.floor((deadline - now).total_seconds())
    if seconds <= 0: raise ValueError('original aggregate preview producer budget expired')
    return seconds


def producer_budget(function):
    @wraps(function)
    def bounded(plan, root, source_root, tools, tools_ref, incoming, environment, lookup=api, *arguments, **options):
        if 'producer_timeout_minutes' not in plan['policy']:
            return function(plan, root, source_root, tools, tools_ref, incoming, environment, lookup, *arguments, **options)
        jobs = lookup(f"repos/{plan['repository']}/actions/runs/{environment['GITHUB_RUN_ID']}/attempts/{environment['GITHUB_RUN_ATTEMPT']}/jobs?per_page=100")
        seconds = producer_deadline(plan, jobs)
        if not hasattr(signal, 'SIGALRM'): raise ValueError('aggregate preview publication requires its Linux deadline supervisor')
        def expired(*_): raise ValueError('original aggregate preview producer budget expired')
        prior_handler = signal.signal(signal.SIGALRM, expired)
        prior_timer = signal.getitimer(signal.ITIMER_REAL); started = time.monotonic()
        signal.setitimer(signal.ITIMER_REAL, min(seconds, prior_timer[0]) if prior_timer[0] else seconds)
        try:
            return function(plan, root, source_root, tools, tools_ref, incoming, environment, lookup, *arguments, **options)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0); signal.signal(signal.SIGALRM, prior_handler)
            if prior_timer[0]: signal.setitimer(signal.ITIMER_REAL, max(.001, prior_timer[0] - (time.monotonic() - started)), prior_timer[1])
    return bounded


@producer_budget
def publication(plan,root,source_root,tools,tools_ref,incoming,environment,lookup=api,execute=subprocess.run,*,context=None):
    if plan['action']!='up' or git(source_root,'rev-parse','HEAD')!=plan['head']:
        raise ValueError('Preview publication requires the exact requested head checkout')
    if subprocess.run(['git','-C',str(source_root),'diff','--quiet','HEAD','--'],timeout=30).returncode:
        raise ValueError('Preview publication source must remain clean')
    config,definition=source_policy(plan,source_root,tools)
    authority=module('komizo_application_publication',ROOT/'scripts/application-publication.py')
    proof=authority.read(incoming/'.artifacts/cookbook-validation.json')
    if context is not None:context.require_proof(proof)
    if (proof.get('head')!=plan['head'] or proof.get('base')!=plan['base']
            or proof.get('configuration_sha256')!=hashlib.sha256((source_root/'cookbook.toml').read_bytes()).hexdigest()):
        raise ValueError('Preview acceptance does not bind the exact requested head/base/configuration')
    repository=plan['repository'];run=environment['GITHUB_RUN_ID'];attempt=environment['GITHUB_RUN_ATTEMPT']
    if not run.isdecimal() or not attempt.isdecimal():
        raise ValueError('Preview publication requires valid same-attempt run metadata')
    jobs=lookup(f'repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100')
    authority.accept(proof,config,plan['head'],tools_ref,git(source_root,'rev-parse','HEAD^{tree}'),
                     plan['policy']['test_groups'],jobs,environment,workflow_source=plan['controller'])
    if config['recipe']=='godot-game':
        sys.path.insert(0,str(tools.resolve()/'cookbook'))
        import godot_ci
        godot_ci.validate_acceptance(proof.get('godot_matrix'),config,jobs,plan['head'],tools_ref,environment,
                                    proof['configuration_sha256'],root=source_root)
    receipts = native_publication.admit_receipts(incoming, proof, plan['head'], plan['policy']['test_groups'])
    packet = native_publication.stage(incoming, proof, source_root / '.artifacts/release', plan['policy']['project'])
    if packet.descriptor['source'] != {'repository': plan['repository'], 'repository_id': environment['GITHUB_REPOSITORY_ID'], 'revision': plan['head']}:
        raise ValueError('preview packet belongs to another source')
    require_current(plan, current_pr(repository, plan['pr'], lookup))
    helpers = context.tools if context is not None else tools
    values = dict(environment, KOMIZO_REGISTRY_USER=repository.split('/')[0], KOMIZO_REGISTRY_TOKEN=environment.get('GH_TOKEN', ''))
    published = native_publication.publish(packet, helpers, values)
    require_current(plan, current_pr(repository, plan['pr'], lookup))
    destination = root / '.artifacts/komizo-release'
    evidence = native_publication.write_evidence(packet, proof, published, environment, destination, receipts=receipts)
    (root / '.artifacts/preview-publication.json').write_text(json.dumps({'version': 1, 'plan': plan,
        'publication': evidence, 'release': packet.descriptor,
        **({**context.identities(), 'entrypoint_contract': 'mise-v1'} if context is not None else {})}, sort_keys=True) + '\n')


def targets(plan, app_url, api_url):
    values = {}
    for name, url in [('app', app_url), ('api', api_url)]:
        if name == 'api' and not plan['policy']['backend']:
            if url: raise ValueError('static preview must not acquire an API route')
            continue
        value = urlsplit(url)
        if (value.scheme != 'https' or not value.hostname or '.' not in value.hostname or value.username
                or value.password or value.port or value.query or value.fragment or len(url) > 2048
                or not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*', value.path.rstrip('/'))):
            raise ValueError('preview targets must be bounded protected HTTPS routes')
        expected = 'pr-' + str(plan['pr'])
        label = value.hostname.split('.')[0]
        if name == 'app' and (label != expected or value.path not in {'', '/'}):
            raise ValueError('preview application origin differs from its isolated instance')
        if name == 'api' and label not in {expected, expected + '-api'}:
            raise ValueError('preview API origin differs from its isolated instance')
        values[name] = {'url': url.rstrip('/'), 'origin': value.scheme + '://' + value.netloc}
    if 'api' in values:
        app = urlsplit(values['app']['url']); api = urlsplit(values['api']['url'])
        if api.hostname == app.hostname:
            if not api.path or api.path == '/': raise ValueError('same-origin preview API requires its protected path')
        elif api.hostname != app.hostname.split('.')[0] + '-api.' + app.hostname.split('.', 1)[1]:
            raise ValueError('preview API and app routes name different isolated targets')
    return values


def publication_evidence(plan, path, environment, lookup=api, *, context=None):
    from release_packet import document, release
    from oci_registry import canonical, digest
    from submission import producer_proof
    value = document(path)
    expected = {'version', 'plan', 'publication', 'release'}
    if context is not None:
        expected.update([*context.identities(), 'entrypoint_contract'])
        context.require_proof(value)
    if not isinstance(value, dict) or set(value) != expected or value.get('version') != 1 or value.get('plan') != plan:
        raise ValueError('preview effects require this exact same-run publication plan')
    raw = release(value['release'], plan['policy']['project'])
    actual = digest(raw)
    if value['release']['source'] != {'repository': plan['repository'], 'repository_id': environment['GITHUB_REPOSITORY_ID'], 'revision': plan['head']}:
        raise ValueError('preview publication source differs from its selected head')
    published = value['publication']
    if (published.get('kind') != 'komizo.publication' or published.get('release_digest') != actual
            or published.get('published') != {name: artifact['reference'] for name, artifact in value['release']['artifacts'].items()}):
        raise ValueError('preview evidence has incomplete exact artifact publication')
    context_fields = published.get('execution_context', {})
    if context_fields.get('GITHUB_JOB') != 'publish':
        raise ValueError('preview evidence belongs to another producer role')
    for key in ['GITHUB_REPOSITORY', 'GITHUB_REPOSITORY_ID', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT',
                'GITHUB_SHA', 'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA']:
        if not environment.get(key) or context_fields.get(key) != environment[key]:
            raise ValueError('preview publication belongs to another workflow execution')
    repository = plan['repository']; run = environment['GITHUB_RUN_ID']; attempt = environment['GITHUB_RUN_ATTEMPT']
    jobs = lookup(f'repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100')
    if jobs.get('total_count') != len(jobs.get('jobs', [])):
        raise ValueError('preview publication requires complete current-attempt job metadata')
    publishers = [job for job in jobs['jobs'] if job['name'].endswith('/ Publish exact Preview images')]
    if len(publishers) != 1 or publishers[0].get('conclusion') != 'success' or publishers[0].get('head_sha') != plan['controller']:
        raise ValueError('trusted same-source preview publisher has not succeeded')
    return value


def apply(plan, environment):
    import submission
    from release_packet import document
    application = plan['policy']['project']; instance = 'pr-' + str(plan['pr'])
    config = environment['KOMIZO_SSH_CONFIG']
    if plan['action'] == 'up':
        descriptor = document(Path('.artifacts/komizo-release/release.json'))
        proof = document(Path('.artifacts/komizo-release/proof.json'))
        body = submission.request(application, instance, 'deploy', environment, descriptor=descriptor, release_proof=proof,
                                  ttl_seconds=plan['policy']['ttl_seconds'])
        result = submission.submit(config, body)
        if not result['ok']:
            raise ValueError('preview deployment did not become ready')
        urls = {}
        for route in descriptor['workload'].get('routes', []):
            if 'target' not in route: continue
            key = 'app' if route['target']['kind'] == 'asset' else 'api'
            if key not in urls: urls[key] = 'https://' + route['host'] + route.get('path', '/').rstrip('/')
        output({'preview-url': urls.get('app', ''), 'api-url': urls.get('api', ''),
                'release-digest': result['release_digest'], 'operation-id': result['operation_id']}, environment)
    else:
        discovery = submission.request(application, instance, 'inspect', environment, release_digest='current')
        installed = submission.submit(config, discovery)['installed']
        if installed is not None:
            body = submission.request(application, instance, 'remove', environment, release_digest=installed['release_digest'])
            result = submission.submit(config, body)
            if not result['ok']: raise ValueError('preview teardown did not finish')
        output({'preview-url': '', 'api-url': ''}, environment)


def verify_health(plan,urls,opener=urllib.request.urlopen):
    for name,path in plan['policy']['health_paths'].items():
        request=urllib.request.Request(urls[name]['url']+path)
        with opener(request,timeout=30) as response:
            if response.status!=200:raise ValueError('Preview health did not pass')
            payload=response.read(1024*1024+1)
        if len(payload)>1024*1024:raise ValueError('Preview health response exceeded its bound')
        if name=='api':
            if json.loads(payload).get('revision')!=plan['head']:
                raise ValueError('Preview API readiness reports another source revision')


def report(plan,urls,lookup=api):
    repository,number=plan['repository'],plan['pr']
    if plan['action']=='up':
        lookup(f'repos/{repository}/issues/{number}/labels','POST',{'labels':['preview']})
        body='<!-- preview -->\n### Preview — '+plan['head']+'\n\nApp: '+urls['app']['url']
        if 'api' in urls:body+='\nAPI: '+urls['api']['url']
        body+='\n\nHealth gate: up. Pushes redeploy while the preview label is on. Comment /preview down to remove it.'
    else:
        try:lookup(f'repos/{repository}/issues/{number}/labels/preview','DELETE')
        except subprocess.CalledProcessError:print('::warning::Could not remove the Preview label')
        body='<!-- preview --> ~~Preview~~ — torn down. Comment /preview to bring it back.'
    found=None
    for page in range(1,101):
        comments=lookup(f'repos/{repository}/issues/{number}/comments?per_page=100&page={page}')
        for comment in comments:
            if (comment.get('user',{}).get('login')=='github-actions[bot]' and '<!-- preview -->' in comment.get('body','')
                    and type(comment.get('id')) is int and comment['id']>0):
                found=comment['id'];break
        if found is not None or len(comments)<100:break
    else:raise ValueError('Preview comment pagination exceeded its bound')
    if found is not None:lookup(f'repos/{repository}/issues/comments/{found}','PATCH',{'body':body})
    elif plan['action']=='up':lookup(f'repos/{repository}/issues/{number}/comments','POST',{'body':body})


def output(values,environment):
    with open(environment['GITHUB_OUTPUT'],'a') as stream:
        for name,value in values.items():
            if value is None:value=''
            delimiter=secrets.token_hex(16)
            stream.write(name+'<<'+delimiter+'\n'+str(value)+'\n'+delimiter+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['request','plan','publish','evidence','routes','apply','health','report'])
    sdk_arguments(parser)
    parser.add_argument('--declaration',default='')
    parser.add_argument('--source-root',type=Path,default=Path('_preview_source'))
    parser.add_argument('--incoming',type=Path,default=Path('.artifacts/preview-input'))
    args=parser.parse_args()
    try:
        environment=dict(os.environ);root=Path.cwd()
        cookbook,tools,revision,context,environment=automation(args,environment,'_preview_cookbook')
        if args.operation=='request':
            output(request_plan(root,cookbook,args.declaration,environment,context=context),environment)
            return
        plan=validate_plan(environment['PLAN'],root,cookbook,environment,context=context)
        if args.operation=='plan':
            policy=plan['policy']
            output({'project':policy['project'],'pr':plan['pr'],'head':plan['head'],'base':plan['base'],
                'action':plan['action'],
                'backend':str(policy['backend']).lower(),'openrouter':str('OPENROUTER_API_KEY' in policy['secrets']).lower()},environment)
        elif args.operation=='publish':
            publication(plan,root,args.source_root,cookbook,revision,args.incoming,environment,context=context)
        elif args.operation=='evidence':
            publication_evidence(plan,root/'.artifacts/preview-publication.json',environment,context=context)
        elif args.operation == 'routes':
            import submission
            body = submission.request(plan['policy']['project'], 'pr-' + str(plan['pr']), 'inspect', environment, release_digest='current', timeout_seconds=60)
            planned = submission.submit(environment['KOMIZO_SSH_CONFIG'], body, timeout=60)['plan']
            output({'app-origin': planned['app_origin'], 'api-origin': planned['api_origin']}, environment)
        elif args.operation == 'apply':
            apply(plan, environment)
        else:
            if plan['action']=='up':
                publication_evidence(plan,root/'.artifacts/preview-publication.json',environment,context=context)
            urls=targets(plan,environment.get('PREVIEW_URL',''),environment.get('API_URL','')) if plan['action']=='up' else {}
            if args.operation=='health':verify_health(plan,urls)
            else:report(plan,urls)
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
        parser.exit(1,str(error)+'\n')


if __name__=='__main__':main()
