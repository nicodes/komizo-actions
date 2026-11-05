#!/usr/bin/env python3
"""Run one declared fixed-origin, read-only Godot production assertion matrix."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib


def literal_sha(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{40}',value):
        raise ValueError('production verification requires an exact immutable source')
    return value


def select(config, plan, source, release, environment, runner, timeout):
    literal_sha(source)
    if (environment.get('GITHUB_REF')!='refs/heads/main' or source!=environment.get('GITHUB_SHA')
            or environment.get('GITHUB_EVENT_NAME') not in {'push','workflow_dispatch'}):
        raise ValueError('production verification requires the exact trusted main source')
    if not isinstance(release,str) or not re.fullmatch('[a-f0-9]{64}',release):
        raise ValueError('production verification requires the exact verified payload identity')
    if config.get('recipe')!='godot-game':raise ValueError('production browser assertions require a Godot recipe')
    entries=plan.get('production',{}).get('include',[])
    if len(entries)!=1:raise ValueError('production verification requires one explicit fixed-origin gate')
    entry=entries[0];native=entry.get('entry',{})
    if (entry.get('kind')!='production' or entry.get('runner')!=runner
            or entry.get('timeout_minutes')!=timeout or entry.get('setup')!='browser'
            or native.get('runner')!='node' or native.get('args',[])
            or not native.get('path','').startswith('tests/') or not native['path'].endswith('.mjs')
            or type(native.get('timeout')) is not int or not 1<=native['timeout']<=900):
        raise ValueError('production worker differs from its reviewed native runner, assertion or deadline')
    browsers=entry.get('browsers')
    if browsers not in [['chromium'],['chromium','firefox','webkit']]:
        raise ValueError('production verification requires its exact native browser matrix')
    xvfb=entry.get('xvfb',[])
    if not isinstance(xvfb,list) or len(xvfb)!=len(set(xvfb)) or not set(xvfb)<=set(browsers) or type(entry.get('probe',False)) is not bool:
        raise ValueError('production display and blank-probe policy must be explicit native data')
    packages=entry.get('packages',[])
    declared=[record['directory'] for record in config.get('node',{}).get('packages',[])]
    if len(packages)!=1 or packages[0] not in declared:
        raise ValueError('production verification requires one declared locked browser package')
    return entry


def execute(command, root, environment, timeout=180):
    return subprocess.run(command,cwd=root,env=environment,check=True,timeout=timeout)


def runtime_environment(root,environment):
    import sdk_environment
    values=sdk_environment.preserve_mise(environment)
    private=root/'.artifacts/godot-production'
    for key,name in {'HOME':'home','TMPDIR':'tmp','XDG_CACHE_HOME':'cache','XDG_CONFIG_HOME':'config','XDG_DATA_HOME':'data','XDG_STATE_HOME':'state'}.items():
        directory=private/name
        if any(path.is_symlink() for path in [directory,*directory.parents]):raise ValueError('production state must not traverse links')
        directory.mkdir(mode=0o700,parents=True,exist_ok=True);values[key]=str(directory)
    values['PLAYWRIGHT_BROWSERS_PATH']=str(root/'.artifacts/playwright')
    values['GDPW_BIN']=str(root/'.artifacts/browser-sdk/gd-playwright-cli')
    values['MISE_AUTO_INSTALL']='false';values['MISE_EXEC_AUTO_INSTALL']='false'
    return values


def prepare(root,config,entry,environment,run=execute):
    """Explicit SDK setup only; no product build, import, delivery or assertion."""
    import node_component
    import godot_browser
    environment=runtime_environment(root,environment)
    run(['mise','install','python','node'],root,environment)
    packages=[record for record in config.get('node',{}).get('packages',[]) if record['directory'] in entry['packages']]
    if len(packages)!=1:raise ValueError('production verification requires one declared locked package')
    sdk=lambda argv,directory,env:run(['mise','exec','--',*argv],directory,env)
    node_component.install(root,packages,environment,sdk)
    godot_browser.setup(root,config,environment,capability=True,public_browsers=entry['browsers'])


def verify(root,entry,environment,source,release,run=execute):
    """Native programs retain their own public-origin, sandbox and gameplay assertions."""
    values={**runtime_environment(root,environment),'EXPECTED_SOURCE_COMMIT':source,'EXPECTED_RELEASE_ID':release,
            'MISE_AUTO_INSTALL':'false','MISE_EXEC_AUTO_INSTALL':'false'}
    for key in ['GH_TOKEN','GITHUB_TOKEN','MISE_GITHUB_TOKEN']:values.pop(key,None)
    def one(browser,probe=False):
        command=['mise','exec','--','node',entry['entry']['path']]
        if browser in entry.get('xvfb',[]):command=['xvfb-run','-a',*command]
        selected={**values,'BROWSER':browser,'PUBLIC_SANDBOX_PROBE':'1' if probe else '0'}
        run(command,root,selected,entry['entry']['timeout'])
    if entry.get('probe'):one('chromium',True)
    for browser in entry['browsers']:one(browser)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['setup','verify'])
    parser.add_argument('--tools',type=Path,required=True)
    parser.add_argument('--tools-ref',required=True)
    parser.add_argument('--source',required=True)
    parser.add_argument('--release',required=True)
    parser.add_argument('--runner',required=True)
    parser.add_argument('--timeout',type=int,required=True)
    args=parser.parse_args();root=Path.cwd().resolve();tools=args.tools.resolve()
    try:
        literal_sha(args.tools_ref)
        for directory,expected in [(root,args.source),(tools,args.tools_ref)]:
            actual=subprocess.check_output(['git','-C',str(directory),'rev-parse','HEAD'],text=True,timeout=30).strip()
            if actual!=expected:raise ValueError('production source or cookbook differs from its immutable pin')
            if subprocess.run(['git','-C',str(directory),'diff','--quiet','HEAD','--'],timeout=30).returncode:
                raise ValueError('production verification requires unmodified tracked source')
        sys.path.insert(0,str(tools/'cookbook'))
        from configuration import load,relative
        import godot_ci
        config=load(root)
        entry=select(config,godot_ci.plan(root,config),args.source,args.release,os.environ,args.runner,args.timeout)
        relative(root,entry['entry']['path'],exists=True)
        environment={**os.environ,'TOOLS_ROOT':str(tools),'MISE_AUTO_INSTALL':'false','MISE_EXEC_AUTO_INSTALL':'false'}
        if args.operation=='setup':prepare(root,config,entry,environment)
        else:verify(root,entry,environment,args.source,args.release)
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as error:
        parser.exit(1,str(error)+'\n')


if __name__=='__main__':main()
