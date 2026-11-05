#!/usr/bin/env python3
"""Run explicit trusted-main operational roles using pinned shared automation."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import tomllib

ROOT=Path(__file__).resolve().parents[1]
SHA=re.compile('[a-f0-9]{40}')
WORKFLOWS={'backup':'backup.yml','recovery':'recovery.yml','security':'security-scan.yml','production':'production-verification.yml','paid':'production-verification.yml'}


def capture(arguments,root=None,environment=None,timeout=30):
    return subprocess.check_output(arguments,cwd=root,env=environment,text=True,timeout=timeout).strip()


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def tools_modules(tools):
    sys.path.insert(0,str(tools.resolve()/'cookbook'))
    import operational_workflows,node_component,images
    return operational_workflows,node_component,images


def authority(root,tools,operation,selected,environment,lookup=None):
    lookup=lookup or (lambda path:capture(['gh','api',path]))
    repository=environment.get('GITHUB_REPOSITORY','')
    revision=environment.get('GITHUB_SHA','')
    if (not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*',repository)
            or not SHA.fullmatch(revision) or environment.get('GITHUB_REF')!='refs/heads/main'
            or environment.get('GITHUB_EVENT_NAME') not in {'workflow_dispatch','schedule'}
            or environment.get('GITHUB_WORKFLOW_SHA')!=revision
            or environment.get('GITHUB_WORKFLOW_REF')!=repository+'/.github/workflows/'+WORKFLOWS[operation]+'@refs/heads/main'
            or capture(['git','rev-parse','HEAD'],root,environment)!=revision):
        raise ValueError('operational effects require their exact trusted-main workflow source')
    if operation=='paid' and environment['GITHUB_EVENT_NAME']!='workflow_dispatch':raise ValueError('paid checks require explicit manual dispatch')
    if capture(['git','status','--porcelain','--untracked-files=no'],root,environment):raise ValueError('operational native source was modified')
    revisions={name:environment.get(key,'') for name,key in [('tools','TOOLS_REF'),('komizo-actions','KOMIZO_REF')]}
    for name,commit in revisions.items():
        if not SHA.fullmatch(commit):raise ValueError('operational automation requires immutable pins')
        checkout=tools if name=='tools' else ROOT
        if capture(['git','rev-parse','HEAD'],checkout,environment)!=commit:raise ValueError('operational checkout differs from its reviewed pin')
        response=json.loads(lookup(f'repos/nicodes/{name}/compare/{commit}...main'))
        if response.get('status') not in {'ahead','identical'}:raise ValueError('operational automation must first be accepted on main')
    workflow,_,_=tools_modules(tools)
    config=tomllib.loads((root/'cookbook.toml').read_text())
    data=workflow.declaration(root,config)
    if data['definition']!=environment.get('DECLARATION') or data['komizo_ref']!=revisions['komizo-actions']:
        raise ValueError('operational input differs from trusted native policy')
    record=next((item for item in data.get('paid',[]) if item['id']==selected),None) if operation=='paid' else data.get(operation)
    if record is None:raise ValueError('this operational role has no actual native implementation')
    if operation=='paid':
        event=json.loads(Path(environment['GITHUB_EVENT_PATH']).read_text())
        if event.get('inputs',{}).get(selected) not in {True,'true'}:raise ValueError('paid native check was not explicitly requested')
    expected=workflow.workflow_files(root,revisions['tools'],config)
    path='.github/workflows/'+WORKFLOWS[operation]
    if (root/path).read_text()!=expected[path]:raise ValueError('operational caller differs from its canonical authority contract')
    if environment.get('OPERATION_RUNNER')!=record['runner'] or environment.get('OPERATION_TIMEOUT')!=str(record['timeout_minutes']):
        raise ValueError('operational runner/deadline differs from native policy')
    if environment.get('OPERATION_ENVIRONMENT','')!=record.get('environment',''):
        raise ValueError('operational approval environment differs from native policy')
    if operation in {'backup','production','paid'}:
        current=json.loads(lookup(f'repos/{repository}/git/ref/heads/main')).get('object',{}).get('sha')
        if current!=revision:raise ValueError('operational source is no longer current main')
    if operation=='paid' and record.get('deployment_required'):
        runs=json.loads(lookup(f'repos/{repository}/actions/workflows/cd.yml/runs?head_sha={revision}&status=success&per_page=100'))
        if not any(value.get('head_sha')==revision and value.get('head_branch')=='main' and value.get('conclusion')=='success' for value in runs.get('workflow_runs',[])):
            raise ValueError('paid production check requires a successful deployment of this exact revision')
    return config,data,record


def execute(arguments,root,environment,timeout=300):
    subprocess.run(arguments,cwd=root,env=environment,check=True,timeout=timeout)


def pinned(arguments,root,environment,timeout=300):
    execute(['mise','exec','--',*arguments],root,environment,timeout)


def native(root,record,environment,run=pinned):
    kind=record['runner'];path=record['path']
    if kind=='cargo-test':
        arguments=['cargo','test','--locked','--manifest-path',path,'--test',record['target'],record['filter'],'--','--ignored','--exact','--nocapture']
    elif kind=='cargo-run':arguments=['cargo','run','--locked','--manifest-path',path,'--bin',record['binary'],'--',*record.get('args',[])]
    else:arguments=[{'python':'python3','node':'node','bun':'bun','bash':'bash'}[kind],path,*record.get('args',[])]
    run(arguments,root,dict(environment,**record.get('environment',{})),record.get('timeout',180))


def prepare(root,tools,config,record,operation,environment):
    _,node,images=tools_modules(tools)
    pins=tomllib.loads((root/'mise.toml').read_text())['tools']
    prepare=record.get('prepare',{}) if operation!='paid' else {'rust':True,'native':record.get('prepare',[])}
    tools_needed={'python'}
    checks=[record['check']] if operation=='paid' else record.get('checks',[])
    for check in [*checks,*prepare.get('native',[])]:
        if check['runner'] in {'node','bun'}:tools_needed.add(check['runner'])
        if check['runner'].startswith('cargo-'):tools_needed.add('rust')
    if prepare.get('rust'):tools_needed.add('rust')
    packages=config.get('node',{}).get('packages',[])
    selected=[item for item in packages if item['directory'] in prepare.get('packages',[])]
    if {item['directory'] for item in selected}!=set(prepare.get('packages',[])):raise ValueError('operational dependency preparation must select real native packages')
    for package in selected:tools_needed.add('bun' if package.get('manager','npm')=='bun' else 'node')
    if operation=='security':
        if config['recipe']=='godot-game':import godot_game as adapter
        else:import react_native as adapter
        tools_needed.update(adapter.required_tools(root,config,'security'))
        selected=packages
    if not tools_needed<=pins.keys():raise ValueError('operational SDKs require exact native Mise pins')
    environment.update(MISE_AUTO_INSTALL='false',MISE_EXEC_AUTO_INSTALL='false',MISE_TRUSTED_CONFIG_PATHS=str(root),
        TOOLS_ROOT=str(tools.resolve()),KOMIZO_ACTIONS_ROOT=str(ROOT),PYTHONDONTWRITEBYTECODE='1',
        EXPECTED_REVISION=environment['GITHUB_SHA'],APP_VERSION=environment['GITHUB_SHA'],PLAYWRIGHT_BROWSERS_PATH=str(root/'.artifacts/playwright'))
    execute(['mise','install',*sorted(tools_needed)],root,environment)
    node.install(root,selected,environment,pinned)
    if prepare.get('browser'):
        directory=root/prepare['browser']
        package=next((item for item in selected if item['directory']==prepare['browser']),None)
        if package is None:raise ValueError('operational browser requires its frozen native package')
        command=['bun','x','--no-install','playwright'] if package.get('manager','npm')=='bun' else ['npm','exec','--no','--','playwright']
        pinned([*command,'install','--with-deps','--only-shell','chromium'],directory,environment)
    for check in prepare.get('native',[]):native(root,check,environment)


def source_security(root,tools,config,environment):
    _,node,images=tools_modules(tools)
    arguments=['python3',str(tools/'helpers/source-secrets.py')]
    if config.get('security',{}).get('fixtures'):arguments+=['--fixtures',config['security']['fixtures']]
    pinned(arguments,root,environment)
    node.audit(root,config.get('node',{}).get('packages',[]),environment,pinned)
    manifests=sorted({record['manifest'] for record in config.get('components',{}).get('rust',[])})
    if manifests:images.rust_inventory(root,manifests,environment,pinned)


def outputs(values,environment):
    with open(environment['GITHUB_OUTPUT'],'a') as stream:
        for name,value in values.items():
            delimiter=secrets.token_hex(16)
            stream.write(name+'<<'+delimiter+'\n'+str(value)+'\n'+delimiter+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['plan','prepare','run','report'])
    parser.add_argument('--tools',type=Path,default=Path('_operation_cookbook'))
    args=parser.parse_args()
    try:
        root=Path.cwd();environment=dict(os.environ);operation=environment['OPERATION']
        config,data,record=authority(root,args.tools,operation,environment.get('OPERATION_CHECK',''),environment)
        if args.command=='plan':
            outputs({'project':data['project'],'backup':record.get('config',''),
                'allowed-products':json.dumps([data['project']]),'evidence':'\n'.join(record.get('evidence',[]))},environment)
        elif args.command=='prepare':prepare(root,args.tools,config,record,operation,environment)
        elif args.command=='report':
            execute([sys.executable,str(args.tools/'helpers/report-failure.py')],root,environment)
        elif operation=='security':
            source_security(root,args.tools,config,environment)
            if record.get('deployed_policy'):
                driver=module('operational_deployed_scan',ROOT/'scripts/deployed_scan.py')
                driver.scan(root,root/record['deployed_policy'],args.tools)
        elif operation=='production':
            for check in record['checks']:native(root,check,environment)
        elif operation=='paid':
            key=environment.get('OPENROUTER_API_KEY','')
            if not key or any(ord(c)<32 for c in key):raise ValueError('explicit paid check credential is unavailable')
            native(root,record['check'],environment)
        else:raise ValueError('backup/recovery effects use only their pinned dedicated helpers')
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
        parser.exit(1,str(error)+'\n')


if __name__=='__main__':main()
