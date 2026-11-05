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

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
SHA=re.compile('[a-f0-9]{40}')
WORKFLOWS={'backup':'backup.yml','recovery':'recovery.yml','retention':'recovery.yml','security':'security-scan.yml','production':'production-verification.yml','paid':'production-verification.yml'}


def capture(arguments,root=None,environment=None,timeout=30):
    if arguments and arguments[0]=='git':
        arguments=['git','-c','core.hooksPath='+os.devnull,'-c','core.fsmonitor=false',*arguments[1:]]
    return subprocess.check_output(arguments,cwd=root,env=environment,text=True,timeout=timeout).strip()


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def tools_modules(tools):
    sys.path.insert(0,str(tools.resolve()/'cookbook'))
    import operational_workflows,node_component,images
    return operational_workflows,node_component,images


def checked_checkout(checkout,name,commit,environment):
    checkout=checkout.absolute()
    if checkout.is_symlink() or any(parent.is_symlink() for parent in checkout.parents) or (checkout/'.git').is_symlink():
        raise ValueError('operational SDK sources must not traverse links')
    def git(*arguments):
        return capture(['git','-c','core.hooksPath='+os.devnull,'-c','core.fsmonitor=false',
            '-C',str(checkout),*arguments],environment=environment)
    if Path(git('rev-parse','--show-toplevel')).resolve()!=checkout.resolve() or git('rev-parse','HEAD')!=commit:
        raise ValueError('operational checkout differs from its reviewed pin')
    if git('remote','get-url','origin') not in ('https://github.com/nicodes/'+name,'https://github.com/nicodes/'+name+'.git'):
        raise ValueError('operational checkout has another source authority')
    if git('status','--porcelain','--untracked-files=all','--ignored=matching'):
        raise ValueError('operational SDK source contains modified or unexpected files')


def runtime_environment(root,tools,environment):
    return dict(environment,MISE_AUTO_INSTALL='false',MISE_EXEC_AUTO_INSTALL='false',MISE_TRUSTED_CONFIG_PATHS=str(root),
        TOOLS_ROOT=str(tools.resolve()),KOMIZO_ACTIONS_ROOT=str(ROOT),PYTHONDONTWRITEBYTECODE='1',
        EXPECTED_REVISION=environment['GITHUB_SHA'],APP_VERSION=environment['GITHUB_SHA'],
        PLAYWRIGHT_BROWSERS_PATH=str(root/'.artifacts/playwright'))


def authority(root,tools,operation,selected,environment,lookup=None):
    lookup=lookup or (lambda path:capture(['gh','api',path]))
    repository=environment.get('GITHUB_REPOSITORY','')
    revision=environment.get('GITHUB_SHA','')
    if (operation not in WORKFLOWS or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*',repository)
            or not SHA.fullmatch(revision) or environment.get('GITHUB_REF')!='refs/heads/main'
            or environment.get('GITHUB_EVENT_NAME') not in {'workflow_dispatch','schedule'}
            or environment.get('GITHUB_WORKFLOW_SHA')!=revision
            or environment.get('GITHUB_WORKFLOW_REF')!=repository+'/.github/workflows/'+WORKFLOWS[operation]+'@refs/heads/main'
            or capture(['git','rev-parse','HEAD'],root,environment)!=revision):
        raise ValueError('operational effects require their exact trusted-main workflow source')
    if any(not re.fullmatch('[1-9][0-9]{0,19}',environment.get(key,'')) for key in ['GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT']):
        raise ValueError('operational effects require an identified Actions execution')
    if operation=='paid' and environment['GITHUB_EVENT_NAME']!='workflow_dispatch':raise ValueError('paid checks require explicit manual dispatch')
    if capture(['git','status','--porcelain','--untracked-files=no'],root,environment):raise ValueError('operational native source was modified')
    revisions={name:environment.get(key,'') for name,key in [('tools','TOOLS_REF'),('komizo-actions','KOMIZO_REF')]}
    for name,commit in revisions.items():
        if not SHA.fullmatch(commit):raise ValueError('operational automation requires immutable pins')
        checkout=tools if name=='tools' else ROOT
        checked_checkout(checkout,name,commit,environment)
        response=json.loads(lookup(f'repos/nicodes/{name}/compare/{commit}...main'))
        if not isinstance(response,dict) or response.get('status') not in {'ahead','identical'}:raise ValueError('operational automation must first be accepted on main')
    workflow,_,_=tools_modules(tools)
    config=tomllib.loads((root/'cookbook.toml').read_text())
    data=workflow.declaration(root,config)
    if data['definition']!=environment.get('DECLARATION') or data['komizo_ref']!=revisions['komizo-actions']:
        raise ValueError('operational input differs from trusted native policy')
    record=next((item for item in data.get('paid',[]) if item['id']==selected),None) if operation=='paid' else data.get(operation)
    if operation=='retention':record=data.get('recovery',{}).get('legacy_retention')
    if record is None:raise ValueError('this operational role has no actual native implementation')
    if operation=='production' and selected and not any(item['id']==selected and item.get('inline') for item in data.get('paid',[])):
        raise ValueError('ordinary production may select only its original inline paid assertion')
    if operation in {'paid','retention'}:
        if environment['GITHUB_EVENT_NAME']!='workflow_dispatch':raise ValueError('legacy retention requires explicit manual dispatch')
        path=Path(environment['GITHUB_EVENT_PATH'])
        if path.is_symlink() or not path.is_file() or path.stat().st_size>1048576:raise ValueError('paid dispatch metadata must be bounded')
        event=json.loads(path.read_text())
        if not isinstance(event,dict) or not isinstance(event.get('inputs',{}),dict):raise ValueError('paid dispatch metadata is invalid')
        if operation=='paid' and event.get('inputs',{}).get(selected) not in (True,'true'):raise ValueError('paid native check was not explicitly requested')
        if operation=='retention':
            inputs=event.get('inputs',{})
            if inputs.get('operation')!='retain-legacy':raise ValueError('legacy retention was not explicitly requested')
            for key in ['artifact-id','run-id']:
                if not re.fullmatch('[1-9][0-9]{0,19}',str(inputs.get(key,''))):raise ValueError('legacy retention requires immutable artifact and run IDs')
    expected=workflow.workflow_files(root,revisions['tools'],config)
    path='.github/workflows/'+WORKFLOWS[operation]
    if (root/path).read_text()!=expected[path]:raise ValueError('operational caller differs from its canonical authority contract')
    if environment.get('OPERATION_RUNNER')!=record['runner'] or environment.get('OPERATION_TIMEOUT')!=str(record['timeout_minutes']):
        raise ValueError('operational runner/deadline differs from native policy')
    if environment.get('OPERATION_ENVIRONMENT','')!=record.get('environment',''):
        raise ValueError('operational approval environment differs from native policy')
    if operation in {'backup','security'} and environment.get('OPERATION_REPORT_TIMEOUT')!=str(record.get('report_timeout',5)):
        raise ValueError('audit report deadline differs from native policy')
    if json.loads(environment.get('OPERATION_STEP_TIMEOUTS','{}'))!=workflow.step_timeouts(operation,record):
        raise ValueError('operational step deadlines differ from native policy')
    if operation in {'backup','production','paid'}:
        current=json.loads(lookup(f'repos/{repository}/git/ref/heads/main')).get('object',{}).get('sha')
        if current!=revision:raise ValueError('operational source is no longer current main')
    if operation=='paid' and record.get('deployment_required'):
        runs=json.loads(lookup(f'repos/{repository}/actions/workflows/cd.yml/runs?head_sha={revision}&status=success&per_page=100'))
        if not any(value.get('head_sha')==revision and value.get('head_branch')=='main' and value.get('conclusion')=='success'
                and value.get('path')=='.github/workflows/cd.yml' and value.get('event') in {'push','workflow_dispatch'}
                and type(value.get('run_attempt')) is int and value['run_attempt']>0 for value in runs.get('workflow_runs',[])):
            raise ValueError('paid production check requires a successful deployment of this exact revision')
    return config,data,record


def execute(arguments,root,environment,timeout=300):
    subprocess.run(arguments,cwd=root,env=environment,check=True,timeout=timeout)


def pinned(arguments,root,environment,timeout=300):
    execute(['mise','exec','--',*arguments],root,environment,timeout)


def native(root,record,environment,run=pinned):
    kind=record['runner'];path=record['path']
    if kind=='playwright':
        arguments=(['bun','x','--no-install','playwright'] if record['manager']=='bun' else ['npm','exec','--no','--','playwright'])
        arguments+=['test','--config',Path(path).name,*record.get('args',[])]
        run(arguments,root/record['cwd'],dict(environment,**record.get('environment',{})),record.get('timeout',180))
        return
    if kind=='node-test':arguments=['node','--test',path,*record.get('args',[])]
    elif kind=='cargo-test':
        arguments=['cargo','test','--locked','--offline','--manifest-path',path,'--test',record['target'],record['filter'],'--','--ignored','--exact','--nocapture']
    elif kind=='cargo-run':arguments=['cargo','run','--locked','--offline','--manifest-path',path,'--bin',record['binary'],'--',*record.get('args',[])]
    else:arguments=[{'python':'python3','node':'node','bun':'bun','bash':'bash'}[kind],path,*record.get('args',[])]
    run(arguments,root,dict(environment,**record.get('environment',{})),record.get('timeout',180))


def prepare(root,tools,config,record,operation,environment):
    _,node,images=tools_modules(tools)
    pins=tomllib.loads((root/'mise.toml').read_text())['tools']
    prepare=record.get('prepare',{}) if operation!='paid' else {'native':record.get('prepare',[])}
    tools_needed={'python'}
    checks=[record['check']] if operation=='paid' else record.get('checks',[])
    for check in [*checks,*prepare.get('native',[])]:
        if check['runner'] in {'node','node-test','bun'}:tools_needed.add('node' if check['runner']=='node-test' else check['runner'])
        if check['runner']=='playwright':tools_needed.add('bun' if check['manager']=='bun' else 'node')
        if check['runner'].startswith('cargo-'):tools_needed.add('rust')
    if prepare.get('rust'):tools_needed.add('rust')
    packages=[node.validate(value,root)[0] for value in config.get('node',{}).get('packages',[])]
    selected=[item for item in packages if item['directory'] in prepare.get('packages',[])]
    if {item['directory'] for item in selected}!=set(prepare.get('packages',[])):raise ValueError('operational dependency preparation must select real native packages')
    for package in selected:tools_needed.add('bun' if package.get('manager','npm')=='bun' else 'node')
    if operation=='security':
        if config.get('components',{}).get('rust') or record.get('deployed_policy'):tools_needed.add('trivy')
        if record.get('deployed_policy'):
            scanner=module('operational_scan_policy',ROOT/'scripts/deployed_scan.py')
            policy=scanner.policy(root,root/record['deployed_policy'])
            if policy['go']:tools_needed.update(['go','go:golang.org/x/vuln/cmd/govulncheck'])
        selected=packages
        for package in selected:tools_needed.add('bun' if package.get('manager','npm')=='bun' else 'node')
    if not tools_needed<=pins.keys():raise ValueError('operational SDKs require exact native Mise pins')
    environment=runtime_environment(root,tools,environment)
    execute(['mise','install',*sorted(tools_needed)],root,environment)
    manifests=sorted({check['path'] for check in checks if check['runner'].startswith('cargo-')})
    if manifests:
        import rust_component
        rust_component.setup(root,[{'manifest':manifest} for manifest in manifests],environment,pinned)
    node.install(root,selected,environment,pinned)
    if prepare.get('browser'):
        directory=root/prepare['browser']
        package=next((item for item in selected if item['directory']==prepare['browser']),None)
        if package is None:raise ValueError('operational browser requires its frozen native package')
        command=['bun','x','--no-install','playwright'] if package.get('manager','npm')=='bun' else ['npm','exec','--no','--','playwright']
        pinned([*command,'install','--with-deps',*(['--only-shell'] if prepare.get('browser_only_shell',False) else []),'chromium'],directory,environment)
    if operation!='paid':
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
    parser.add_argument('command',choices=['plan','prepare','run','source','deployed','report','setup-artifacts','build-artifacts','audit-artifacts','check-artifacts'])
    parser.add_argument('--tools',type=Path,default=Path('_operation_cookbook'))
    args=parser.parse_args()
    try:
        root=Path.cwd();environment=dict(os.environ);operation=environment['OPERATION']
        config,data,record=authority(root,args.tools,operation,environment.get('OPERATION_CHECK',''),environment)
        environment=runtime_environment(root,args.tools,environment)
        if args.command=='plan':
            workflow=tools_modules(args.tools)[0]
            outputs({'project':data['project'],'backup':record.get('config',''),
                'allowed-products':json.dumps([data['project']]),'evidence':'\n'.join(record.get('evidence',[])),
                'deployed':str(bool(record.get('deployed_policy'))).lower(),
                'artifact-validation':str(record.get('artifact_validation',False)).lower(),
                'artifact-name':workflow.artifact_name(operation,record,environment),
                'evidence-missing':record.get('evidence_missing','error' if operation in {'backup','recovery'} else 'ignore')},environment)
        elif args.command=='prepare':prepare(root,args.tools,config,record,operation,environment)
        elif args.command=='report':
            execute([sys.executable,str(args.tools/'helpers/report-failure.py')],root,environment)
        elif args.command.endswith('-artifacts'):
            if operation!='security' or not record.get('artifact_validation'):
                raise ValueError('artifact validation requires its explicit source build policy')
            import generate
            if (root/'Makefile').read_text()!=generate.makefile(root,environment['TOOLS_REF']):
                raise ValueError('artifact validation requires the exact canonical source Make entrypoint')
            commands={'setup-artifacts':'setup','build-artifacts':'build','audit-artifacts':'test:security'}
            if args.command in commands:execute(['make',commands[args.command]],root,environment,record['timeout_minutes']*60)
            else:
                import configuration,recipe
                config=configuration.load(root)
                os.environ.update(environment)
                environment=recipe.configure_environment(root,config)
                adapter=recipe.adapter(config)
                selected=set(record.get('artifact_checks',[]))
                for suite,entries in config.get('tests',{}).items():
                    checks=[entry for entry in entries if entry.get('name') in selected]
                    if checks:adapter.native_tests(root,dict(config,tests={suite:checks}),environment,suite)
        elif args.command=='source':
            if operation!='security':raise ValueError('source audits require their declared security role')
            prepare(root,args.tools,config,record,operation,environment)
            source_security(root,args.tools,config,environment)
            for check in record.get('checks',[]):native(root,check,environment)
        elif args.command=='deployed':
            if operation!='security' or not record.get('deployed_policy'):raise ValueError('deployed audits require their actual native image policy')
            driver=module('operational_deployed_scan',ROOT/'scripts/deployed_scan.py')
            driver.scan(root,root/record['deployed_policy'],args.tools,environment=environment)
        elif operation=='production':
            for check in record['checks']:native(root,check,environment)
        elif operation=='paid':
            key=environment.get('OPENROUTER_API_KEY','')
            if not key or any(ord(c)<32 for c in key):raise ValueError('explicit paid check credential is unavailable')
            # Native bootstrap and assertion share the original paid deadline.
            for check in record.get('prepare',[]):native(root,check,environment)
            native(root,record['check'],environment)
        else:raise ValueError('backup/recovery effects use only their pinned dedicated helpers')
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
        parser.exit(1,str(error)+'\n')


if __name__=='__main__':main()
