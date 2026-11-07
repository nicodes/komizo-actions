#!/usr/bin/env python3
"""Recheck an existing successful static CD record without granting new host authority."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from urllib.parse import urljoin

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import recipe_context

STATE=Path('.artifacts/godot-recovery')
CONTEXT=['GITHUB_SERVER_URL','GITHUB_REPOSITORY','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_SHA','GITHUB_WORKFLOW_REF','GITHUB_WORKFLOW_SHA','GITHUB_EVENT_NAME']


def read(path,maximum=4<<20):
    if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents) or path.stat().st_size>maximum:
        raise ValueError('recovery evidence must be a bounded regular file')
    return json.loads(path.read_text())


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def execute(argv,root,environment,timeout=180):return subprocess.run(argv,cwd=root,env=environment,check=True,timeout=timeout)
def capture(argv,root,environment,timeout=60):return subprocess.check_output(argv,cwd=root,env=environment,text=True,timeout=timeout)


def helper(tools,name):
    directory=str(tools/'helpers')
    if directory not in sys.path:sys.path.insert(0,directory)
    spec=importlib.util.spec_from_file_location('recovery_'+name,tools/'helpers'/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def identity(root,tools,tools_ref,environment,*,context=None):
    if (environment.get('GITHUB_REF')!='refs/heads/main' or environment.get('GITHUB_EVENT_NAME')!='workflow_dispatch'
            or not re.fullmatch('[a-f0-9]{40}',tools_ref)
            or capture(['git','rev-parse','HEAD'],root,environment).strip()!=environment.get('GITHUB_SHA')
            or capture(['git','-C',str(tools),'rev-parse','HEAD'],root,environment).strip()!=tools_ref):
        raise ValueError('static recovery requires exact trusted current-main controller and cookbook identities')
    expected_workflow=environment['GITHUB_REPOSITORY']+'/.github/workflows/recovery.yml@refs/heads/main'
    if environment.get('GITHUB_WORKFLOW_REF')!=expected_workflow:raise ValueError('recovery controller must retain its original source recovery.yml identity')
    execute(['git','diff','--exit-code','HEAD','--'],root,environment,30)
    if context is not None:
        for name in ['configuration','godot_static','node_component','godot_browser','godot_security','native_tests']:context.module(name)
    sys.path.insert(0,str(tools/'cookbook'))
    from configuration import load
    import godot_static
    config=load(root);static=godot_static.declaration(root,config)
    if not static or not static['record'] or not static['recovery'] or static['repository']!=environment['GITHUB_REPOSITORY']:
        raise ValueError('static image recovery is not an existing enrolled release capability')
    return config,static


def source_run(repository,run_id,tools,root,environment):
    if not re.fullmatch('[1-9][0-9]{0,19}',run_id):raise ValueError('recovery requires an immutable positive source run ID')
    run=json.loads(capture(['gh','api',f'repos/{repository}/actions/runs/{run_id}'],root,environment))
    if run.get('id')!=int(run_id):raise ValueError('recovery source run ID differs from the requested immutable run')
    record=helper(tools,'godot_release_record');record.REPO=repository;record.validate_run(run)
    return run


def authenticated_record(root,tools,run_id,environment):
    run=source_run(environment['GITHUB_REPOSITORY'],run_id,tools,root,environment)
    directory=root/'rollback-record'
    if not directory.is_dir() or {p.name for p in directory.iterdir()}!={'release-record.json'}:
        raise ValueError('historical CD artifact must contain only its original release record')
    record=read(directory/'release-record.json',65536)
    schema=helper(tools,'godot_release_record');schema.REPO=environment['GITHUB_REPOSITORY'];schema.validate_record(record,run)
    return run,record


def fixture(config,selected):
    entries=[entry for entry in config.get('tests',{}).get('integration',[]) if entry.get('name',entry['path'])==selected['recovery_test']]
    if len(entries)!=1 or entries[0]['runner'] not in {'bash','python'}:raise ValueError('recovery selects one existing native shipping assertion')
    return entries[0]


def setup(root,config,environment):
    import node_component,godot_browser,godot_security
    execute(['mise','install','python','node','rust','cargo:cargo-audit'],root,environment)
    directory=config['components']['browser']['package']
    packages=[entry for entry in config['node']['packages'] if entry['directory']==directory]
    sdk=lambda argv,path,env:execute(['mise','exec','--',*argv],path,env)
    node_component.install(root,packages,environment,sdk)
    godot_browser.setup(root,config,environment)
    godot_security.setup(root,config,environment,execute)


def historical_contract(root,record,environment):
    execute(['git','merge-base','--is-ancestor',record['sha'],'HEAD'],root,environment,30)
    destination=root/STATE/'historical'
    if destination.exists():raise ValueError('historical source contracts require a fresh generated destination')
    for name in ['mise.toml','deploy/gate-runtime/Cargo.lock','deploy/gate/Caddyfile']:
        body=subprocess.check_output(['git','show',record['sha']+':'+name],cwd=root,env=environment,timeout=30)
        if len(body)>1<<20:raise ValueError('historical source contract exceeds its bound')
        path=destination/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
    return destination


def verify(root,tools,tools_ref,config,selected,run_id,environment,*,context=None):
    import godot_browser,godot_security
    from native_tests import run_entries
    run,record=authenticated_record(root,tools,run_id,environment)
    values={**environment,'TOOLS_ROOT':str(tools),'GATE_IMAGE':record['gate_ref'],'GATE_REF':record['gate_ref'],'CONFIG_REF':record['config_ref'],
      'EXPECTED_SOURCE_COMMIT':record['sha'],'EXPECTED_RELEASE_ID':record['release_id'],
      'BROWSER':'chromium firefox webkit','BROWSER_ROUTE_CYCLES':'0','BROWSER_SOAK_SECONDS':'0','BROWSER_STARTUP':'0','BROWSER_TEST_MODE':''}
    execute(['docker','pull',record['gate_ref']],root,values,300)
    # This helper only inspects the exact historical config; it never starts it.
    execute(['mise','exec','--','python3',str(tools/'helpers/godot_rollback_config.py')],root,values,300)
    historical=historical_contract(root,record,values)
    values.update(GODOT_ACCEPTED_SOURCE_ROOT=str(historical),GODOT_ACCEPTED_SOURCE_COMMIT=record['sha'])
    for key in ['GH_TOKEN','GITHUB_TOKEN','MISE_GITHUB_TOKEN']:values.pop(key,None)
    run_entries(root,[fixture(config,selected)],values)
    ordinary=copy.deepcopy(config)
    candidates=[entry for entry in ordinary['components']['browser'].get('journeys',[]) if entry['image']=='gate']
    if candidates:
        if len(candidates)!=1:raise ValueError('historical recovery requires one declared ordinary game journey')
        ordinary['components']['browser']['runner']=candidates[0]['path']
    godot_browser.journeys(root,ordinary,values)
    execute(['mise','exec','--','cargo','audit','--file',str(historical/'deploy/gate-runtime/Cargo.lock'),'--deny','warnings'],root,values,300)
    godot_security.scan_references(root,config,values,execute,[('gate',record['gate_ref'])])
    proof={'version':1,'coverage':'historical-static-release','status':'passed','controller_source':environment['GITHUB_SHA'],
      'cookbook_commit':tools_ref,'configuration_sha256':sha(root/'cookbook.toml'),'source_run':run,'record':record,
      'context':{key:environment.get(key,'') for key in CONTEXT},'checks':['trusted-successful-main-cd','immutable-original-release-record','historical-constrained-config',
      'native-shipping-contract','ordinary-game-browser-matrix','historical-cargo-current-advisories','historical-image-current-security-and-sbom']}
    if context is not None:proof.update(context.identities(),entrypoint_contract='mise-v1')
    destination=root/STATE/'acceptance.json';destination.parent.mkdir(parents=True,exist_ok=True);destination.write_text(json.dumps(proof,sort_keys=True)+'\n')
    return {**record,'proof_sha256':sha(destination)}


def accept_restore(proof,digest,root,tools_ref,configuration_hash,environment,jobs,*,context=None):
    if (not re.fullmatch('[a-f0-9]{64}',digest) or proof.get('version')!=1 or proof.get('coverage')!='historical-static-release'
            or proof.get('status')!='passed' or proof.get('controller_source')!=environment.get('GITHUB_SHA')
            or proof.get('cookbook_commit')!=tools_ref or proof.get('configuration_sha256')!=configuration_hash
            or proof.get('context')!={key:environment.get(key,'') for key in CONTEXT}):
        raise ValueError('restore requires successful same-controller same-run historical verification')
    if context is not None:context.require_proof(proof)
    if jobs.get('total_count')!=len(jobs.get('jobs',[])):raise ValueError('restore requires complete actual current-attempt job metadata')
    expected_checks={'trusted-successful-main-cd','immutable-original-release-record','historical-constrained-config','native-shipping-contract',
        'ordinary-game-browser-matrix','historical-cargo-current-advisories','historical-image-current-security-and-sbom'}
    if set(proof.get('checks',[]))!=expected_checks:raise ValueError('restore requires every existing native historical validation boundary')
    actual=[job for job in jobs['jobs'] if job.get('name','').endswith('/ Verify historical static release')]
    if len(actual)!=1 or actual[0].get('conclusion')!='success' or actual[0].get('head_sha')!=environment['GITHUB_SHA']:
        raise ValueError('actual read-only historical verification has not succeeded at this controller source')
    return proof['record']


def restore(root,tools,tools_ref,config,environment,expected,*,context=None):
    path=root/STATE/'acceptance.json'
    proof=read(path)
    if sha(path)!=expected:raise ValueError('restore proof bytes differ from the successful verifier output')
    repository,run,attempt=(environment[key] for key in ['GITHUB_REPOSITORY','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT'])
    jobs=json.loads(capture(['gh','api',f'repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100'],root,environment))
    record=accept_restore(proof,expected,root,tools_ref,sha(root/'cookbook.toml'),environment,jobs,context=context)
    schema=helper(tools,'godot_release_record');schema.REPO=repository;schema.validate_record(record,proof['source_run'])
    copier=helper(tools,'registry_manifest')
    values={'CONFIG_REF':record['config_ref'],'RESTORE_REVISION':record['sha']}
    os.environ.update(values)
    copier.main()
    from delivery_workflows import declaration
    manifest_url=urljoin(declaration(root,config)['notification_url'],'release-manifest.json')
    return {**record,'manifest-url':manifest_url,'health-url':manifest_url+' '+record['release_id']}


def output(values,environment):
    with open(environment['GITHUB_OUTPUT'],'a') as stream:
        for key,value in values.items():
            if isinstance(value,str) and '\n' not in value and '\r' not in value:stream.write(f'{key}={value}\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('operation',choices=['source','setup','verify','restore'])
    parser.add_argument('--tools',type=Path);parser.add_argument('--tools-ref')
    parser.add_argument('--cookbook-root',type=Path);parser.add_argument('--cookbook-ref')
    parser.add_argument('--tools-root',type=Path)
    parser.add_argument('--source-run',default='');parser.add_argument('--proof-sha256',default='')
    args=parser.parse_args();root=Path.cwd();environment=dict(os.environ)
    try:
        context=None
        if args.cookbook_root is not None or args.cookbook_ref is not None:
            if args.cookbook_root is None or args.cookbook_ref is None or args.tools is not None or args.tools_ref is not None:
                raise ValueError('select the complete Cookbook contract or the historical Tools SDK')
            context=recipe_context.accepted_context(args.cookbook_root,args.cookbook_ref,
                lambda path:json.loads(capture(['gh','api',path],root,environment,30)),args.tools_root)
            cookbook,revision,tools=context.cookbook,context.cookbook_revision,context.tools
            environment.update(COOKBOOK_ROOT=str(cookbook),COOKBOOK_REF=revision,TOOLS_ROOT=str(tools),TOOLS_REF=context.tools_revision)
        else:
            if args.tools is None or args.tools_ref is None or args.tools_root is not None:
                raise ValueError('historical recovery requires its explicit Tools SDK source')
            cookbook=tools=args.tools.resolve();revision=args.tools_ref
        config,selected=identity(root,cookbook,revision,environment,context=context)
        if args.operation=='source':
            run=source_run(environment['GITHUB_REPOSITORY'],args.source_run,tools,root,environment)
            output({'sha':run['head_sha']},environment)
        elif args.operation=='setup':setup(root,config,environment)
        elif args.operation=='verify':output(verify(root,tools,revision,config,selected,args.source_run,environment,context=context),environment)
        else:output(restore(root,tools,revision,config,environment,args.proof_sha256,context=context),environment)
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as error:parser.exit(1,str(error)+'\n')


if __name__=='__main__':main()
