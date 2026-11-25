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
import time
import tomllib

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import recipe_data
import recipe_context
from workflow_run import read_event,dispatch_inputs
SHA=re.compile('[a-f0-9]{40}')
WORKFLOWS={'backup':('backup.yml',),'security':('security.yml',),'production':('deploy.yml','verify-production.yml'),
    'paid':('deploy.yml','verify-production.yml')}


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


def runtime_environment(root,tools,environment,context=None):
    values=dict(environment,MISE_AUTO_INSTALL='false',MISE_EXEC_AUTO_INSTALL='false',MISE_TRUSTED_CONFIG_PATHS=str(root),
        TOOLS_ROOT=str(tools.resolve()),KOMIZO_ACTIONS_ROOT=str(ROOT),PYTHONDONTWRITEBYTECODE='1',
        EXPECTED_REVISION=environment['GITHUB_SHA'],APP_VERSION=environment['GITHUB_SHA'],
        PLAYWRIGHT_BROWSERS_PATH=str(root/'.artifacts/playwright'))
    if context is not None:
        values.update(COOKBOOK_ROOT=str(context.cookbook),COOKBOOK_REF=context.cookbook_revision,TOOLS_REF=context.tools_revision)
    return values


def deployment_run(event, repository, revision):
    """Identify the one Deploy run whose completion triggered a separate verification workflow.

    The run is identified by its workflow file path: a run's name is its run-name (for example "Deploy · main").
    """
    run = event.get('workflow_run')
    if not isinstance(run, dict):
        raise ValueError('workflow_run verification requires its triggering Deploy run')
    for key in ['repository', 'head_repository']:
        value = run.get(key)
        if not isinstance(value, dict) or value.get('full_name') != repository:
            raise ValueError('workflow_run verification accepts only this repository\'s own Deploy run')
    run_id, attempt = run.get('id'), run.get('run_attempt')
    if (type(run_id) is not int or run_id <= 0 or type(attempt) is not int or attempt <= 0
            or run.get('path') != '.github/workflows/deploy.yml'
            or run.get('head_branch') != 'main' or run.get('event') not in {'push', 'workflow_dispatch'}
            or run.get('status') != 'completed' or run.get('conclusion') != 'success'):
        raise ValueError('workflow_run verification requires a successful main Deploy run')
    if run.get('head_sha') != revision:
        raise ValueError('workflow_run verification applies only while its deployed revision is current main')
    return run_id, attempt


def deployment_runs(repository, revision, lookup):
    """Every Deploy run of this revision, whatever its state, each at its latest attempt.

    The listing is filtered by head SHA only. The `status=` filter is served from an index that can lag a
    completed run by many minutes, whereas the head SHA listing is current, so success is judged here from each
    run's own fields. A re-run replaces its run's attempt in place, so a run is never listed twice. A listing
    that is not complete within one page is refused rather than judged from part of it."""
    runs = json.loads(lookup(f'repos/{repository}/actions/workflows/deploy.yml/runs?head_sha={revision}&per_page=100'))
    rows = runs.get('workflow_runs')
    if not isinstance(rows, list) or type(runs.get('total_count')) is not int or runs['total_count'] != len(rows):
        raise ValueError('complete Deploy run records of this revision are required')
    return [run for run in rows if isinstance(run, dict)]


def successful_deployment(repository, revision, lookup, exact=None):
    endpoint = f'repos/{repository}/actions'
    for run in deployment_runs(repository, revision, lookup):
        run_id, attempt = run.get('id'), run.get('run_attempt')
        if exact is not None and (run_id, attempt) != exact:
            continue
        if (type(run_id) is not int or run_id <= 0 or type(attempt) is not int or attempt <= 0
                or run.get('head_sha') != revision or run.get('head_branch') != 'main'
                or run.get('conclusion') != 'success' or run.get('path') != '.github/workflows/deploy.yml'
                or run.get('event') not in {'push', 'workflow_dispatch'}):
            continue
        jobs = json.loads(lookup(f'{endpoint}/runs/{run_id}/attempts/{attempt}/jobs?per_page=100'))
        rows = jobs.get('jobs')
        if not isinstance(rows, list) or type(jobs.get('total_count')) is not int or jobs['total_count'] != len(rows):
            raise ValueError('complete deployment job records are required')
        deployment = [job for job in rows if job.get('name') == 'Deploy / Deploy']
        if len(deployment) != 1:
            continue
        job = deployment[0]
        if (job.get('status') == 'completed' and job.get('conclusion') == 'success'
                and job.get('run_id') == run_id and job.get('run_attempt') == attempt and job.get('head_sha') == revision):
            return True
    return False


DEPLOYMENT_WAITS=6
DEPLOYMENT_PAUSE=10


def awaited_deployment(repository, revision, lookup, exact, *, waits=DEPLOYMENT_WAITS, pause=DEPLOYMENT_PAUSE, sleep=time.sleep):
    """Confirm that the exact triggering Deploy run succeeded, tolerating a listing that lags its completion.

    The head SHA listing is current, but a run whose completion just triggered this workflow may still be
    missing from it for a few seconds. (The `status=success` filter lags far longer and is not used.) Each miss
    reads that one run: a completed run without a success conclusion refuses at once, anything else waits and
    reads again, at most `waits` times. Only a later read that shows the exact run and attempt successful under
    every condition of `successful_deployment` passes, so the wait never weakens the check."""
    run_id, attempt = exact
    for waited in range(waits + 1):
        if successful_deployment(repository, revision, lookup, exact):
            return
        run = json.loads(lookup(f'repos/{repository}/actions/runs/{run_id}/attempts/{attempt}'))
        if isinstance(run, dict) and run.get('status') == 'completed' and run.get('conclusion') != 'success':
            raise ValueError('production verification requires a successful deployment of this exact Deploy run')
        if waited < waits:
            sleep(pause)
    raise ValueError('production verification requires a successful deployment of this exact Deploy run;'
                     f' it was not reported successful after waiting {waits * pause} seconds')


def authority(root,tools,operation,selected,environment,lookup=None,*,supplied_tools=None,return_context=False,sleep=time.sleep):
    lookup=lookup or (lambda path:capture(['gh','api',path]))
    repository=environment.get('GITHUB_REPOSITORY','')
    revision=environment.get('GITHUB_SHA','')
    if (operation not in WORKFLOWS or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*',repository)
            or not SHA.fullmatch(revision) or environment.get('GITHUB_REF')!='refs/heads/main'
            or environment.get('GITHUB_EVENT_NAME') not in {'workflow_dispatch','schedule','workflow_run'}
            or environment.get('GITHUB_WORKFLOW_SHA')!=revision
            or environment.get('GITHUB_WORKFLOW_REF') not in [repository+'/.github/workflows/'+name+'@refs/heads/main' for name in WORKFLOWS[operation]]
            or capture(['git','rev-parse','HEAD'],root,environment)!=revision):
        raise ValueError('operational effects require their exact trusted-main workflow source')
    if any(not re.fullmatch('[1-9][0-9]{0,19}',environment.get(key,'')) for key in ['GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT']):
        raise ValueError('operational effects require an identified Actions execution')
    if operation=='paid' and environment['GITHUB_EVENT_NAME']!='workflow_dispatch':raise ValueError('paid checks require explicit manual dispatch')
    if capture(['git','status','--porcelain','--untracked-files=no'],root,environment):raise ValueError('operational native source was modified')
    context=None;triggering=None
    cookbook_ref=environment.get('COOKBOOK_REF','')
    if not cookbook_ref and supplied_tools is not None:
        raise ValueError('an independent helper location requires the Cookbook source contract')
    revisions={name:environment.get(key,'') for name,key in [('komizo-actions','KOMIZO_REF')]}
    if not cookbook_ref:revisions['tools']=environment.get('TOOLS_REF','')
    for name,commit in revisions.items():
        if not SHA.fullmatch(commit):raise ValueError('operational automation requires immutable pins')
        checkout=tools if name=='tools' else ROOT
        checked_checkout(checkout,name,commit,environment)
        response=json.loads(lookup(f'repos/nicodes/{name}/compare/{commit}...main'))
        if not isinstance(response,dict) or response.get('status') not in {'ahead','identical'}:raise ValueError('operational automation must first be accepted on main')
    if cookbook_ref:
        context=recipe_context.accepted_context(tools,cookbook_ref,lambda path:json.loads(lookup(path)),supplied_tools)
        if environment.get('TOOLS_REF','') not in {'',context.tools_revision}:
            raise ValueError('operational helper identity differs from the Cookbook dependency')
    if context is not None:
        workflow=context.module('operational_workflows')
        for name in ['configuration','node_component','images']:context.module(name)
    else:workflow,_,_=tools_modules(tools)
    config=recipe_data.load(root,tools)
    data=workflow.declaration(root,config)
    if data['definition']!=environment.get('DECLARATION') or data['komizo_ref']!=revisions['komizo-actions']:
        raise ValueError('operational input differs from trusted native policy')
    record=next((item for item in data.get('paid',[]) if item['id']==selected),None) if operation=='paid' else data.get(operation)
    if record is None:raise ValueError('this operational role has no actual native implementation')
    if operation=='production' and selected and not any(item['id']==selected and item.get('inline') for item in data.get('paid',[])):
        raise ValueError('ordinary production may select only its original inline paid assertion')
    if operation in {'production','paid'}:
        if context is None:
            raise ValueError('Deploy verification requires the accepted Cookbook source')
        event=read_event(environment, maximum=1 << 20)
        try:inputs=dispatch_inputs(event)
        except ValueError:raise ValueError('verify dispatch metadata is invalid') from None
        caller=environment['GITHUB_WORKFLOW_REF'].split('@',1)[0].rsplit('/',1)[1]
        # Deploy's manual dispatch selects verification with mode; the separate verification workflow has no mode.
        mode=inputs.get('mode') if caller=='deploy.yml' else inputs.get('mode','verify')
        if environment['GITHUB_EVENT_NAME']=='workflow_dispatch' and mode!='verify':
            raise ValueError('production assertions require explicit Deploy verify mode')
        if environment['GITHUB_EVENT_NAME']=='workflow_run':
            if operation!='production' or caller!='verify-production.yml':
                raise ValueError('only the separate production verification caller may follow a Deploy run')
            triggering=deployment_run(event,repository,revision)
        if environment['GITHUB_EVENT_NAME']=='schedule' and event.get('schedule') not in record.get('schedule',[]):
            raise ValueError('production assertion schedule differs from its native policy')
        if operation=='paid' and inputs.get(selected) not in (True,'true'):
            raise ValueError('paid native check was not explicitly requested')
        expected=context.module('generate').workflow_files(root,cookbook_ref,config)
    else:
        expected=workflow.workflow_files(root,cookbook_ref if context else revisions['tools'],config)
    path='.github/workflows/'+environment['GITHUB_WORKFLOW_REF'].split('@',1)[0].rsplit('/',1)[1]
    if (root/path).read_text()!=expected[path]:raise ValueError('operational caller differs from its canonical authority contract')
    if environment.get('OPERATION_RUNNER')!=record['runner'] or environment.get('OPERATION_TIMEOUT')!=str(record['timeout_minutes']):
        raise ValueError('operational runner/deadline differs from native policy')
    if environment.get('OPERATION_ENVIRONMENT','')!=record.get('environment',''):
        raise ValueError('operational approval environment differs from native policy')
    if operation=='backup' and environment.get('OPERATION_REPORT_TIMEOUT')!=str(record.get('report_timeout',5)):
        raise ValueError('audit report deadline differs from native policy')
    expected_timeouts=workflow.step_timeouts(operation,record)
    if operation=='security':expected_timeouts={name:value for name,value in expected_timeouts.items() if not name.startswith('report')}
    if json.loads(environment.get('OPERATION_STEP_TIMEOUTS','{}'))!=expected_timeouts:
        raise ValueError('operational step deadlines differ from native policy')
    if operation in {'backup','production','paid'}:
        current=json.loads(lookup(f'repos/{repository}/git/ref/heads/main')).get('object',{}).get('sha')
        if current!=revision:raise ValueError('operational source is no longer current main')
    if triggering is not None:awaited_deployment(repository,revision,lookup,triggering,sleep=sleep)
    if operation=='paid' and record.get('deployment_required') and not successful_deployment(repository,revision,lookup):
        raise ValueError('paid production check requires a successful deployment of this exact revision')
    result=(config,data,record)
    return (*result,context) if return_context else result


def execute(arguments,root,environment,timeout=300):
    subprocess.run(arguments,cwd=root,env=environment,check=True,timeout=timeout)


def pinned(arguments,root,environment,timeout=300):
    execute(['mise','exec','--',*arguments],root,environment,timeout)


def install_browser(arguments,root,environment,*,attempts=3,pause=5,sleep=time.sleep):
    """Install the declared browser, retrying a failed or timed-out install a bounded number of times.

    The install only fetches packages, and the assertions that follow check the exact result, so a later
    successful attempt is equivalent to a first one; the last failure is raised unchanged."""
    for attempt in range(1,attempts+1):
        try:return pinned(arguments,root,environment)
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired):
            if attempt==attempts:raise
            sleep(pause)


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


def prepare(root,tools,config,record,operation,environment,*,helpers=None,context=None):
    _,node,images=tools_modules(tools)
    pins=tomllib.loads((root/'mise.toml').read_text())['tools']
    prepare=record.get('prepare',{}) if operation!='paid' else {'native':record.get('prepare',[])}
    tools_needed={'python'}
    checks=[record['check']] if operation=='paid' else record.get('checks',[])
    for check in [*checks,*prepare.get('native',[])]:
        if check['runner'] in {'node','node-test','bun'}:tools_needed.add('node' if check['runner']=='node-test' else check['runner'])
        if check['runner']=='playwright':tools_needed.update(['node',*(['bun'] if check['manager']=='bun' else [])])
        if check['runner'].startswith('cargo-'):tools_needed.add('rust')
    if prepare.get('rust'):tools_needed.add('rust')
    if prepare.get('browser'):tools_needed.add('node')
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
    environment=runtime_environment(root,helpers or tools,environment,context)
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
        install_browser([*command,'install','--with-deps',*(['--only-shell'] if prepare.get('browser_only_shell',False) else []),'chromium'],directory,environment)
    if operation!='paid':
        for check in prepare.get('native',[]):native(root,check,environment)


def source_security(root,tools,config,environment,*,helpers=None):
    _,node,images=tools_modules(tools)
    arguments=['python3',str((helpers or tools)/'helpers/source-secrets.py')]
    if config.get('security',{}).get('fixtures'):arguments+=['--fixtures',config['security']['fixtures']]
    pinned(arguments,root,environment)
    node.audit(root,config.get('node',{}).get('packages',[]),environment,pinned)
    manifests=images.rust_inventory_manifests(root,config)
    if manifests:images.rust_inventory(root,manifests,environment,pinned,sbom=config.get('security',{}).get('rust_inventory_sbom',False))


def outputs(values,environment):
    with open(environment['GITHUB_OUTPUT'],'a') as stream:
        for name,value in values.items():
            delimiter=secrets.token_hex(16)
            stream.write(name+'<<'+delimiter+'\n'+str(value)+'\n'+delimiter+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['plan','prepare','run','source','deployed','report','setup-artifacts','build-artifacts','audit-artifacts','check-artifacts'])
    parser.add_argument('--tools',type=Path,help='Historical Tools-hosted recipe SDK location')
    parser.add_argument('--cookbook-root',type=Path)
    parser.add_argument('--cookbook-ref')
    parser.add_argument('--tools-root',type=Path,help='Location of the exact helper dependency declared by Cookbook')
    parser.add_argument('--activation-reader-root',type=Path,help='Separate pinned historical activation reader checkout')
    args=parser.parse_args()
    try:
        root=Path.cwd();environment=dict(os.environ);operation=environment['OPERATION']
        if args.command=='report' and operation!='backup':
            raise ValueError('operational reporting belongs only to native backup; audits use the failure reporter')
        if args.cookbook_root is not None or args.cookbook_ref is not None:
            if args.cookbook_root is None or args.cookbook_ref is None or args.tools is not None:
                raise ValueError('select the complete Cookbook contract or the historical Tools SDK')
            if environment.get('COOKBOOK_REF',args.cookbook_ref)!=args.cookbook_ref:
                raise ValueError('Cookbook CLI revision differs from the workflow source')
            environment['COOKBOOK_REF']=args.cookbook_ref
            cookbook=args.cookbook_root
        else:
            if args.tools_root is not None or environment.get('COOKBOOK_REF'):
                raise ValueError('Cookbook callers must select their explicit source contract')
            cookbook=args.tools or Path('_operation_cookbook')
        config,data,record,context=authority(root,cookbook,operation,environment.get('OPERATION_CHECK',''),environment,
            supplied_tools=args.tools_root,return_context=True)
        tools=context.tools if context else cookbook
        revision=context.cookbook_revision if context else environment['TOOLS_REF']
        environment=runtime_environment(root,tools,environment,context)
        if args.command=='plan':
            workflow=tools_modules(cookbook)[0]
            outputs({'project':data['project'],'backup':record.get('config',''),
                'allowed-products':json.dumps([data['project']]),'evidence':'\n'.join(record.get('evidence',[])),
                'deployed':str(bool(record.get('deployed_policy'))).lower(),
                'artifact-validation':str(record.get('artifact_validation',False)).lower(),
                'artifact-name':workflow.artifact_name(operation,record,environment),
                'evidence-missing':record.get('evidence_missing','error' if operation=='backup' else 'ignore')},environment)
        elif args.command=='prepare':prepare(root,cookbook,config,record,operation,environment,helpers=tools,context=context)
        elif args.command=='report':
            execute([sys.executable,str(tools/'helpers/report-failure.py')],root,environment)
        elif args.command.endswith('-artifacts'):
            if operation!='security' or not record.get('artifact_validation'):
                raise ValueError('artifact validation requires its explicit source build policy')
            recipe_data.canonical_entrypoint(root,cookbook,revision)
            commands={'setup-artifacts':'setup','build-artifacts':'build','audit-artifacts':'test:security'}
            if args.command in commands:execute(recipe_data.task_command(cookbook,commands[args.command]),root,environment,record['timeout_minutes']*60)
            else:
                import configuration,recipe
                config=recipe_data.load(root,cookbook,configuration)
                os.environ.update(environment)
                environment=recipe.configure_environment(root,config)
                adapter=recipe.adapter(config)
                selected=set(record.get('artifact_checks',[]))
                for suite,entries in config.get('tests',{}).items():
                    checks=[entry for entry in entries if entry.get('name') in selected]
                    if checks:adapter.native_tests(root,dict(config,tests={suite:checks}),environment,suite)
        elif args.command=='source':
            if operation!='security':raise ValueError('source audits require their declared security role')
            prepare(root,cookbook,config,record,operation,environment,helpers=tools,context=context)
            source_security(root,cookbook,config,environment,helpers=tools)
            for check in record.get('checks',[]):native(root,check,environment)
        elif args.command=='deployed':
            if operation!='security' or not record.get('deployed_policy'):raise ValueError('deployed audits require their actual native image policy')
            if args.activation_reader_root is None:raise ValueError('deployed audits require the separate immutable activation reader')
            driver=module('operational_deployed_scan',ROOT/'scripts/deployed_scan.py')
            with driver.registry_authority(environment) as registry_environment:
                driver.scan(root,root/record['deployed_policy'],tools,environment=registry_environment,
                    activation_reader_root=args.activation_reader_root)
        elif operation=='production':
            for check in record['checks']:native(root,check,environment)
        elif operation=='paid':
            key=environment.get('OPENROUTER_API_KEY','')
            if not key or any(ord(c)<32 for c in key):raise ValueError('explicit paid check credential is unavailable')
            # Native bootstrap and assertion share the original paid deadline.
            for check in record.get('prepare',[]):native(root,check,environment)
            native(root,record['check'],environment)
        else:raise ValueError('backup effects use only their pinned dedicated helpers')
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
        parser.exit(1,str(error)+'\n')


if __name__=='__main__':main()
