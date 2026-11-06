#!/usr/bin/env python3
"""Trusted Preview controller; native application code runs only in readonly Make jobs."""
import argparse
import base64
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
sys.path.insert(0,str(ROOT/'scripts'))
import recipe_data
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


def accepted_automation(tools,tools_ref,komizo_ref,lookup=api):
    if not SHA.fullmatch(tools_ref) or not SHA.fullmatch(komizo_ref):
        raise ValueError('Preview automation requires immutable source pins')
    if git(tools,'rev-parse','HEAD')!=tools_ref or git(ROOT,'rev-parse','HEAD')!=komizo_ref:
        raise ValueError('checked-out Preview automation differs from its accepted pins')
    for repository,revision in [('tools',tools_ref),('komizo-actions',komizo_ref)]:
        if lookup(f'repos/nicodes/{repository}/compare/{revision}...main').get('status') not in {'ahead','identical'}:
            raise ValueError('Preview automation has not been accepted on main')


def native_policy(root,tools,declaration):
    preview,configuration,_=tools_modules(tools)
    config=recipe_data.load(root,tools,configuration)
    if config.get('preview')!={'definition':declaration}:
        raise ValueError('Preview input differs from its trusted native declaration')
    reference=re.search(r'^COOKBOOK_REF := ([a-f0-9]{40})$',(root/'Makefile').read_text(),re.M)
    if not reference or reference[1]!=git(tools,'rev-parse','HEAD'):
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


def request_plan(root,tools,declaration,environment,lookup=api):
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
    if plan['action']=='up':
        if decision['sha']!=head or base!=source:
            raise ValueError('Preview request is stale; request the current head and main base')
        require_current(plan,current)
    return {'enabled':'true','action':plan['action'],'pr':str(number),'head':head,'base':base,
            'reason':decision['reason'],'plan':json.dumps(plan,sort_keys=True,separators=(',',':'))}


def validate_plan(raw,root,tools,environment,lookup=api,*,fresh=True):
    if not isinstance(raw,str) or len(raw.encode())>131072:
        raise ValueError('Preview plan exceeds its bound')
    plan=json.loads(raw)
    expected={'version','repository','controller','controller_tree','trigger','pr','head','base','action','policy'}
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
            or config['artifacts']['definition']!=policy['artifact_definition']
            or config['artifacts'].get('release_metadata',[])!=policy['release_metadata']):
        raise ValueError('PR source cannot change the trusted Preview publication family, components or required checks')
    if supplied!=policy:
        raise ValueError('PR source cannot override trusted Preview identity, credentials, origins or runner policy')
    return config,definition


def publication(plan,root,source_root,tools,tools_ref,incoming,environment,lookup=api,execute=subprocess.run):
    if plan['action']!='up' or git(source_root,'rev-parse','HEAD')!=plan['head']:
        raise ValueError('Preview publication requires the exact requested head checkout')
    if subprocess.run(['git','-C',str(source_root),'diff','--quiet','HEAD','--'],timeout=30).returncode:
        raise ValueError('Preview publication source must remain clean')
    config,definition=source_policy(plan,source_root,tools)
    authority=module('komizo_application_publication',ROOT/'scripts/application-publication.py')
    proof=authority.read(incoming/'.artifacts/cookbook-validation.json')
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
    destination=source_root/'.artifacts/release'
    authority.stage(incoming,proof,destination,plan['policy']['release_metadata'])
    # Only the checked-out pinned helper runs here. No PR program, Makefile,
    # package lifecycle, test or build step executes with package-write access.
    values=dict(environment,COOKBOOK_PREVIEW_NUMBER=str(plan['pr']),REQUEST_HEAD=plan['head'],REQUEST_BASE=plan['base'])
    require_current(plan,current_pr(repository,plan['pr'],lookup))
    execute([sys.executable,str(tools.resolve()/'helpers/release.py'),'publish','--project',plan['policy']['project'],
             '--revision',plan['head'],'--components',*plan['policy']['shipping_components'],
             '--definition',plan['policy']['artifact_definition'],'--test-job','validate',
             '--test-evidence-directory',str((incoming/'.artifacts/verification').resolve())],
            cwd=source_root,env=values,check=True,timeout=1800)
    published=authority.read(destination/'publication.json')
    if (published.get('revision')!=plan['head'] or published.get('tested_commit')!=plan['head']
            or published.get('archive_sha256')!=proof['artifacts'].get('.artifacts/release/images.tar.gz')):
        raise ValueError('Preview publisher did not retain exact validated source and archive evidence')
    evidence=root/'.artifacts/preview-publication.json'
    evidence.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    evidence.write_text(json.dumps({'version':1,'plan':plan,'publication':published},sort_keys=True)+'\n')


def targets(plan,app_url,api_url):
    values={}
    for name,url in [('app',app_url),('api',api_url)]:
        if name=='api' and not plan['policy']['backend']:
            if url:raise ValueError('static Preview must not acquire an API route')
            continue
        value=urlsplit(url)
        if (value.scheme!='https' or not value.hostname or '.' not in value.hostname or value.username or value.password or value.port
                or value.query or value.fragment or len(url)>2048):
            raise ValueError('Preview targets must be host-verified public HTTPS routes')
        project,number=plan['policy']['project'],plan['pr']
        label=value.hostname.split('.')[0]
        allowed={'pr-'+str(number),project+'-pr'+str(number)}
        if name=='api':allowed={entry+'-api' for entry in allowed}
        if plan['policy']['origin_layout']=='subdomain':
            if value.path not in {'','/'} or label not in allowed:
                raise ValueError('the Clerk Preview requires a distinct PR subdomain; shared-path routing is refused')
        else:
            path_route=value.path=='/'+str(number) and label==project+('-api' if name=='api' else '')
            if not path_route and (value.path not in {'','/'} or label not in allowed):
                raise ValueError('resolved Preview route does not belong to this app and PR')
        values[name]={'url':url.rstrip('/'),'origin':value.scheme+'://'+value.netloc}
    if 'api' in values:
        app=urlsplit(values['app']['origin']);api=urlsplit(values['api']['origin'])
        if api.hostname!=app.hostname.split('.')[0]+'-api.'+app.hostname.split('.',1)[1]:
            raise ValueError('Preview API and app routes name different targets')
    return values


def publication_evidence(plan,path,environment,lookup=api):
    authority=module('komizo_application_publication',ROOT/'scripts/application-publication.py')
    value=authority.read(path)
    if not isinstance(value,dict) or set(value)!={'version','plan','publication'} or value.get('version')!=1 or value.get('plan')!=plan:
        raise ValueError('Preview host effects require this exact same-run publication plan')
    published=value['publication']
    if (published.get('revision')!=plan['head'] or published.get('tested_commit')!=plan['head']
            or not re.fullmatch('[a-f0-9]{64}',published.get('archive_sha256',''))
            or published.get('build_provenance',{}).get('source_commit')!=plan['head']
            or published.get('test_provenance',{}).get('tested_commit')!=plan['head']):
        raise ValueError('Preview host effects require exact validated source publication')
    context=published.get('publisher_context',{})
    for key in ['GITHUB_SERVER_URL','GITHUB_REPOSITORY','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_SHA','GITHUB_WORKFLOW_REF','GITHUB_WORKFLOW_SHA','GITHUB_EVENT_NAME']:
        if not environment.get(key) or context.get(key)!=environment[key]:
            raise ValueError('Preview publication belongs to another workflow execution')
    repository=plan['repository'];run=environment['GITHUB_RUN_ID'];attempt=environment['GITHUB_RUN_ATTEMPT']
    if not run.isdecimal() or not attempt.isdecimal():
        raise ValueError('invalid Preview publication execution identity')
    jobs=lookup(f'repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100')
    if jobs.get('total_count')!=len(jobs.get('jobs',[])):
        raise ValueError('Preview publication requires complete current-attempt job metadata')
    publishers=[job for job in jobs['jobs'] if job['name'].endswith('/ Publish exact Preview images')]
    if (context.get('GITHUB_JOB')!='publish' or len(publishers)!=1 or publishers[0].get('conclusion')!='success'
            or publishers[0].get('head_sha')!=plan['controller']):
        raise ValueError('the trusted same-source Preview publisher has not succeeded')


def development_clerk_urls(public):
    """Derive provider endpoints only from an explicit development public key."""
    if not isinstance(public,str) or not re.fullmatch('pk_test_[A-Za-z0-9_-]{1,2048}',public):
        raise ValueError('Preview Clerk endpoints require a bounded development public key')
    encoded=public.removeprefix('pk_test_')
    try:
        decoded=base64.b64decode(encoded+'='*((-len(encoded))%4),altchars=b'-_',validate=True).decode('ascii')
    except (ValueError,UnicodeError):
        raise ValueError('Preview Clerk public key has an invalid frontend encoding') from None
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.clerk\.accounts\.dev\$',decoded):
        raise ValueError('Preview Clerk endpoints must belong to the development provider frontend')
    issuer='https://'+decoded[:-1]
    return {'issuer':issuer,'jwks':issuer+'/.well-known/jwks.json'}


def stack_environment(plan,urls,environment):
    policy=plan['policy']
    if not policy['backend']:
        if policy['secrets'] or policy['origin_variables']:
            raise ValueError('static Preview must not receive application credentials')
        return None
    values=dict(policy['environment'])
    mapping={'CLERK_SECRET_KEY':'PREVIEW_CLERK_SECRET_KEY','OPENROUTER_API_KEY':'PREVIEW_OPENROUTER_API_KEY'}
    for key in policy['secrets']:
        value=environment.get(mapping[key],'')
        if not value or len(value)>8192 or any(ord(c)<32 or ord(c)==127 for c in value):
            raise ValueError('a declared development credential is unavailable or malformed')
        if key=='CLERK_SECRET_KEY' and not value.startswith('sk_test_'):
            raise ValueError('Preview must never receive the production Clerk tenant secret')
        values[key]=value
    public=environment.get('PREVIEW_CLERK_PUBLISHABLE_KEY','')
    if not re.fullmatch('pk_test_[A-Za-z0-9_-]+',public):
        raise ValueError('Preview must use the development Clerk public key')
    values['CLERK_PUBLISHABLE_KEY']=public
    if policy.get('clerk_urls'):
        endpoints=development_clerk_urls(public)
        for name,kind in policy['clerk_urls'].items():values[name]=endpoints[kind]
    for key,names in policy['origin_variables'].items():
        values[key]=','.join(urls[name]['origin'] for name in names)
    return ''.join(key+'='+value+'\n' for key,value in values.items())


def write_environment(plan,urls,environment,execute=subprocess.run):
    body=stack_environment(plan,urls,environment)
    if body is None:return
    for key in plan['policy']['secrets']:
        value=environment.get({'CLERK_SECRET_KEY':'PREVIEW_CLERK_SECRET_KEY','OPENROUTER_API_KEY':'PREVIEW_OPENROUTER_API_KEY'}[key],'')
        print('::add-mask::'+value.replace('%','%25'))
    command=f"doas -n /usr/local/bin/write-preview-stackenv {plan['policy']['project']} '{plan['pr']}'"
    execute(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=15','deploy-target',command],
            input=body,text=True,check=True,timeout=60,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


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
    parser.add_argument('operation',choices=['request','plan','publish','evidence','environment','health','report'])
    parser.add_argument('--tools',type=Path,default=Path('_preview_cookbook'))
    parser.add_argument('--declaration',default='')
    parser.add_argument('--source-root',type=Path,default=Path('_preview_source'))
    parser.add_argument('--incoming',type=Path,default=Path('.artifacts/preview-input'))
    args=parser.parse_args()
    try:
        environment=dict(os.environ);root=Path.cwd()
        accepted_automation(args.tools,environment['TOOLS_REF'],environment['KOMIZO_REF'])
        if args.operation=='request':
            output(request_plan(root,args.tools,args.declaration,environment),environment)
            return
        plan=validate_plan(environment['PLAN'],root,args.tools,environment)
        if args.operation=='plan':
            policy=plan['policy']
            output({'project':policy['project'],'pr':plan['pr'],'head':plan['head'],'base':plan['base'],
                'action':plan['action'],'images':' '.join(policy['image_base']+'-'+name+':'+plan['head'] for name in policy['components']),
                'backend':str(policy['backend']).lower(),'openrouter':str('OPENROUTER_API_KEY' in policy['secrets']).lower()},environment)
        elif args.operation=='publish':
            publication(plan,root,args.source_root,args.tools,environment['TOOLS_REF'],args.incoming,environment)
        elif args.operation=='evidence':
            publication_evidence(plan,root/'.artifacts/preview-publication.json',environment)
        else:
            if plan['action']=='up':
                publication_evidence(plan,root/'.artifacts/preview-publication.json',environment)
            urls=targets(plan,environment.get('PREVIEW_URL',''),environment.get('API_URL','')) if plan['action']=='up' else {}
            if args.operation=='environment':write_environment(plan,urls,environment)
            elif args.operation=='health':verify_health(plan,urls)
            else:report(plan,urls)
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
        parser.exit(1,str(error)+'\n')


if __name__=='__main__':main()
