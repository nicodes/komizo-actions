#!/usr/bin/env python3
"""Admit a manual historical application release without building or publishing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import recipe_context
SHA = re.compile(r'[a-f0-9]{40}\Z')
MAX_BYTES = 4 << 20


def capture(arguments, root, environment, timeout=30):
    values = {key: value for key, value in environment.items() if not key.startswith('GIT_')}
    values.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS='0')
    if arguments[0] == 'git':
        arguments = ['git', '-c', 'core.hooksPath='+os.devnull, '-c', 'core.fsmonitor=false', *arguments[1:]]
    result = subprocess.run(arguments, cwd=root, env=values, capture_output=True, timeout=timeout, check=False)
    if result.returncode or len(result.stdout) > MAX_BYTES:
        raise ValueError('bounded historical deployment lookup failed')
    return result.stdout.decode().strip()


def regular(path, limit=MAX_BYTES):
    if (path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
            or not path.is_file() or path.stat().st_size > limit):
        raise ValueError('historical deployment inputs must be bounded regular files')
    return path.read_text()


def document(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate historical deployment field')
            result[key] = value
        return result
    if not isinstance(text, str) or len(text.encode()) > MAX_BYTES:
        raise ValueError('historical deployment metadata exceeds its bound')
    return json.loads(text, object_pairs_hook=unique)


def git(root, environment, *arguments):
    return capture(['git', *arguments], root, environment)


def checked_checkout(root, name, revision, environment):
    if (root.is_symlink() or any(parent.is_symlink() for parent in root.parents)
            or (root/'.git').is_symlink() or Path(git(root, environment, 'rev-parse', '--show-toplevel')).resolve() != root.resolve()
            or git(root, environment, 'rev-parse', 'HEAD') != revision
            or git(root, environment, 'remote', 'get-url', 'origin') not in
               {'https://github.com/nicodes/'+name, 'https://github.com/nicodes/'+name+'.git'}
            or git(root, environment, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching')):
        raise ValueError('historical deployment SDK differs from its clean pinned authority')


def tools_modules(tools):
    sys.path.insert(0, str(tools.resolve()/'cookbook'))
    import delivery_workflows
    import generate
    import configuration
    if any(not Path(module.__file__).resolve().is_relative_to(tools.resolve()/'cookbook')
           for module in [delivery_workflows, generate, configuration]):
        raise ValueError('historical deployment loaded another SDK authority')
    return delivery_workflows, generate, configuration


def ancestry(lookup, repository, older, newer):
    response = document(lookup(f'repos/{repository}/compare/{older}...{newer}?per_page=1'))
    if (not isinstance(response, dict) or response.get('status') not in {'ahead', 'identical'}
            or response.get('base_commit', {}).get('sha') != older
            or response.get('merge_base_commit', {}).get('sha') != older):
        raise ValueError('historical release and SDK sources must be accepted main ancestors')


def authority(root, tools, actions, environment, lookup=None, *, supplied_tools=None):
    root, tools, actions = root.absolute(), tools.absolute(), actions.absolute()
    lookup = lookup or (lambda path: capture(['gh', 'api', path], root, environment))
    repository, revision, version = (environment.get(key, '') for key in
                                     ['GITHUB_REPOSITORY', 'GITHUB_SHA', 'HISTORICAL_VERSION'])
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository)
            or not SHA.fullmatch(revision) or not SHA.fullmatch(version) or version == revision
            or environment.get('GITHUB_REF') != 'refs/heads/main'
            or environment.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or environment.get('GITHUB_WORKFLOW_SHA') != revision
            or environment.get('GITHUB_WORKFLOW_REF') != repository+'/.github/workflows/cd.yml@refs/heads/main'
            or any(not re.fullmatch(r'[1-9][0-9]{0,19}', environment.get(key, ''))
                   for key in ['GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'])):
        raise ValueError('historical deployment requires an explicit exact trusted-main dispatch')
    event = document(regular(Path(environment['GITHUB_EVENT_PATH']), 1 << 20))
    inputs = event.get('inputs', {}) if isinstance(event, dict) else None
    if (not isinstance(inputs, dict) or inputs.get('version') != version
            or inputs.get('source', '') not in {'', revision}):
        raise ValueError('historical deployment must match its explicit version and current source inputs')
    if (Path(git(root, environment, 'rev-parse', '--show-toplevel')).resolve() != root.resolve()
            or git(root, environment, 'rev-parse', 'HEAD') != revision
            or git(root, environment, 'status', '--porcelain', '--untracked-files=no')):
        raise ValueError('historical deployment source must be exact and clean')
    if any(key.startswith(('KOMIZO_SECRET_', 'KOMIZO_SCOPED_')) for key in environment):
        raise ValueError('historical scoped deployment transports no profile values')
    context=None
    cookbook_ref=environment.get('COOKBOOK_REF','')
    if not cookbook_ref and supplied_tools is not None:
        raise ValueError('an independent helper location requires the Cookbook source contract')
    pins = {name: environment.get(key, '') for name, key in [('komizo-actions', 'KOMIZO_REF')]}
    if not cookbook_ref:pins['tools']=environment.get('TOOLS_REF','')
    for name, pin in pins.items():
        if not SHA.fullmatch(pin):
            raise ValueError('historical deployment requires immutable SDK pins')
        checked_checkout(tools if name == 'tools' else actions, name, pin, environment)
        ancestry(lookup, 'nicodes/'+name, pin, 'main')
    if cookbook_ref:
        context=recipe_context.accepted_context(tools,cookbook_ref,lambda path:document(lookup(path)),supplied_tools)
        if environment.get('TOOLS_REF','') not in {'',context.tools_revision}:
            raise ValueError('historical helper identity differs from the Cookbook dependency')
    current = document(lookup(f'repos/{repository}/git/ref/heads/main'))
    if not isinstance(current, dict) or current.get('object', {}).get('sha') != revision:
        raise ValueError('historical controller source is no longer current main')
    ancestry(lookup, repository, version, revision)
    if context is not None:
        workflow,generator,configuration=[context.module(name) for name in ['delivery_workflows','generate','configuration']]
    else:workflow, generator, configuration = tools_modules(tools)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import recipe_data
    regular(root/'cookbook.toml', 262144)
    config = recipe_data.load(root, tools, configuration)
    data = workflow.declaration(root, config)
    if (config.get('recipe') not in {'react-native', 'godot-game'}
            or data.get('mode', 'application-images') != 'application-images'
            or data.get('historical_deploy') is not True
            or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', data.get('service_env_profile', ''))
            or not re.fullmatch(r'[a-f0-9]{32}', data.get('expected_generation', ''))
            or any(data.get(key) for key in ['secrets', 'variables', 'optional_secrets'])
            or config.get('delivery') != {'definition': environment.get('DECLARATION')}
            or data.get('komizo_ref') != pins['komizo-actions']):
        raise ValueError('historical deployment requires its fixed scoped application capability')
    if (environment.get('HISTORICAL_RUNNER') != data['deploy_runner']
            or environment.get('HISTORICAL_TIMEOUT') != str(data['deploy_timeout'])
            or environment.get('HISTORICAL_ENVIRONMENT') != data.get('environment', 'production')
            or document(environment.get('HISTORICAL_STEP_TIMEOUTS', '{}')) != workflow.historical_deadlines(data)):
        raise ValueError('historical deployment runner, approval or deadlines differ from native policy')
    recipe_revision=cookbook_ref if context else pins['tools']
    expected = generator.workflow_files(root, recipe_revision)
    if regular(root/'.github/workflows/cd.yml') != expected['.github/workflows/cd.yml']:
        raise ValueError('historical deployment caller differs from its canonical source contract')
    entrypoints = recipe_data.canonical_entrypoint(root, tools, recipe_revision, generator)
    definition_name = recipe_data.effective_definition(tools, config, configuration)
    for name in ['cookbook.toml', *entrypoints, '.github/workflows/cd.yml', environment['DECLARATION'], definition_name]:
        git(root, environment, 'ls-files', '--error-unmatch', '--', name)
    definition_path = Path(definition_name)
    if definition_path.is_absolute() or '..' in definition_path.parts:
        raise ValueError('historical image definition must remain caller-owned')
    definition = recipe_data.decode_source_data(tools, regular(root/definition_path, 1 << 20), maximum=1 << 20, configuration=configuration)
    family = definition.get('image_base', '')
    references = definition.get('inputs', {}).get('images')
    if (definition.get('version') != 1 or definition.get('project') != data['project']
            or not re.fullmatch(r'ghcr\.io/[a-z0-9][a-z0-9/._-]{0,200}', family)
            or not isinstance(references, list) or not 1 <= len(references) <= 32
            or any(not isinstance(ref, str) or not ref.startswith(family+'-') or not ref.endswith(':{revision}')
                   or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', ref[len(family)+1:-len(':{revision}')]) for ref in references)
            or len(set(references)) != len(references)):
        raise ValueError('historical release requires its closed declared GHCR image family')
    health = '\n'.join(entry['url']+' '+entry['contains'].replace('{revision}', version)
                       if isinstance(entry, dict) else entry for entry in data['health_urls'])
    return {'kind': 'historical-application-release', 'source_commit': revision, 'version': version,
            'project': data['project'], 'health-urls': health, 'service-env-profile': data['service_env_profile'],
            'expected-generation': data['expected_generation'], 'diagnostics': str(data.get('diagnostics', False)).lower(),
            'images': [ref.replace('{revision}', version) for ref in references],
            'tools_ref': context.tools_revision if context else pins['tools'], 'komizo_ref': pins['komizo-actions'], 'full_source_acceptance': False,
            'host_admission': 'Existing native host policy remains final authority; no release proof is fabricated',
            **({**context.identities(),'entrypoint_contract':'mise-v1'} if context else {})}


def inspect_images(plan, root, environment, inspect=None):
    inspect = inspect or (lambda ref: capture(['docker', 'manifest', 'inspect', ref], root, environment, 60))
    records = []
    for reference in plan['images']:
        raw = inspect(reference)
        value = document(raw)
        if (not isinstance(value, dict) or type(value.get('schemaVersion')) is not int or value['schemaVersion'] != 2
                or not (isinstance(value.get('manifests'), list) and value['manifests']
                        or isinstance(value.get('config'), dict) and value['config'].get('digest'))):
            raise ValueError('historical application component is not an available published manifest')
        descriptors = value['manifests'] if 'manifests' in value else [value['config']]
        if (not 1 <= len(descriptors) <= 128 or any(not isinstance(item, dict)
                or not isinstance(item.get('digest'), str)
                or not re.fullmatch(r'sha256:[a-f0-9]{64}', item['digest']) for item in descriptors)):
            raise ValueError('historical registry descriptors require immutable SHA256 identities')
        records.append({'reference': reference, 'registry_response_sha256': hashlib.sha256(raw.encode()).hexdigest()})
    return plan | {'published_images_inspected': records}


def outputs(values, environment):
    with open(environment['GITHUB_OUTPUT'], 'a') as stream:
        for name in ['version', 'project', 'health-urls', 'service-env-profile', 'expected-generation', 'diagnostics']:
            delimiter = secrets.token_hex(16)
            stream.write(name+'<<'+delimiter+'\n'+values[name]+'\n'+delimiter+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'inspect'])
    parser.add_argument('--tools', type=Path, help='Historical Tools-hosted recipe SDK location')
    parser.add_argument('--cookbook-root',type=Path)
    parser.add_argument('--cookbook-ref')
    parser.add_argument('--tools-root',type=Path)
    args = parser.parse_args()
    try:
        root = Path.cwd(); environment = dict(os.environ)
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
            cookbook=args.tools or Path('_historical_cookbook')
        plan = authority(root, cookbook, ROOT, environment,supplied_tools=args.tools_root)
        if args.command == 'inspect':
            plan = inspect_images(plan, root, environment)
        directory = root/'.artifacts/historical-deployment'
        if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents):
            raise ValueError('historical receipt output cannot follow symlinks')
        directory.mkdir(parents=True, exist_ok=True)
        (directory/(args.command+'.json')).write_text(json.dumps(plan, sort_keys=True, indent=2)+'\n')
        outputs(plan, environment)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error)+'\n')


if __name__ == '__main__':
    main()
