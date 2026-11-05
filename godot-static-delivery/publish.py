#!/usr/bin/env python3
"""Publish accepted static game bytes and materialize only their reviewed host config."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def execute(argv,root,environment,timeout=180):return subprocess.run(argv,cwd=root,env=environment,check=True,timeout=timeout)
def capture(argv,root,environment,timeout=60):return subprocess.check_output(argv,cwd=root,env=environment,text=True,timeout=timeout)


def digest_reference(reference,root,environment):
    family=reference.rsplit(':',1)[0]
    values=json.loads(capture(['docker','image','inspect',reference,'--format','{{json .RepoDigests}}'],root,environment))
    selected={value for value in values if re.fullmatch(re.escape(family)+r'@sha256:[a-f0-9]{64}',value)}
    if len(selected)!=1:raise ValueError('published image must have one exact immutable native repository reference')
    return selected.pop()


def existing(reference,root,environment):
    result=subprocess.run(['docker','manifest','inspect',reference],cwd=root,env=environment,capture_output=True,text=True,timeout=60)
    if result.returncode==0:return True
    if any(marker in result.stderr.lower() for marker in ['manifest unknown','no such manifest']):return False
    raise ValueError('cannot establish immutable registry tag state: '+result.stderr[-1024:])


def check_config(reference,expected,root,environment,run=execute,output=capture):
    container=output(['docker','create',reference],root,environment).strip()
    if not re.fullmatch('[a-f0-9]{12,64}',container):raise ValueError('invalid owned static configuration container')
    try:
        with tempfile.TemporaryDirectory(prefix='static-config-check-') as directory:
            directory=Path(directory)
            run(['docker','cp',container+':/config/.',str(directory)],root,environment)
            if {path.name for path in directory.iterdir()}!=set(expected):raise ValueError('static config image contains unexpected host configuration')
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_file() or path.stat().st_size>262144 or hashlib.sha256(path.read_bytes()).hexdigest()!=expected[path.name]:
                    raise ValueError('static config image differs from the exact accepted materialization')
    finally:run(['docker','rm',container],root,environment)


def publish_config(blueprint,gate_ref,source,static,root,environment,run=execute):
    reference='ghcr.io/'+blueprint['repository']+'-config:'+source
    with tempfile.TemporaryDirectory(prefix='static-publication-') as temporary:
        temporary=Path(temporary);stage=temporary/'config'
        expected=static.materialize(blueprint,gate_ref,stage,environment,root)
        if existing(reference,root,environment):
            run(['docker','pull',reference],root,environment,300)
            check_config(reference,expected,root,environment)
            return digest_reference(reference,root,environment)
        dockerfile=temporary/'Dockerfile';dockerfile.write_text(static.DOCKERFILE)
        # The constant scratch Dockerfile copies exactly two admitted data files.
        # It never executes product code, a compiler or a Dockerfile RUN instruction.
        run(['docker','build','--network','none','--file',str(dockerfile),'--tag',reference,str(stage)],root,environment,180)
        check_config(reference,expected,root,environment)
        run(['docker','push',reference],root,environment,300)
        return digest_reference(reference,root,environment)


def publish_gate(reference,expected,root,environment,run=execute,output=capture):
    if existing(reference,root,environment):
        run(['docker','pull',reference],root,environment,300)
        actual=output(['docker','image','inspect',reference,'--format','{{.Id}}'],root,environment).strip()
        if actual!=expected:raise ValueError('immutable source tag already identifies different game bytes')
    else:run(['docker','push',reference],root,environment,300)
    return digest_reference(reference,root,environment)


def admit(root,tools,source,tools_ref,incoming,environment):
    sdk=module('static_application_admission',Path(__file__).parents[1]/'scripts/application-publication.py')
    if (environment.get('GITHUB_REF')!='refs/heads/main' or environment.get('GITHUB_EVENT_NAME') not in {'push','workflow_dispatch'}
            or source!=environment.get('GITHUB_SHA') or not re.fullmatch('[a-f0-9]{40}',source)):
        raise ValueError('static publication requires the exact final main source')
    for directory,expected in [(root,source),(tools,tools_ref)]:
        if sdk.git('-C',str(directory),'rev-parse','HEAD')!=expected:raise ValueError('static publisher source or cookbook differs from its accepted pin')
        execute(['git','-C',str(directory),'diff','--exit-code','HEAD','--'],root,environment,30)
    config=tomllib.loads((root/'cookbook.toml').read_text())
    proof=sdk.read(incoming/'.artifacts/cookbook-validation.json')
    if proof.get('configuration_sha256')!=hashlib.sha256((root/'cookbook.toml').read_bytes()).hexdigest():
        raise ValueError('static publication declaration differs from complete source acceptance')
    sys.path.insert(0,str(tools/'cookbook'));sys.path.insert(0,str(tools/'helpers'))
    import godot_static,delivery_workflows,godot_ci
    import verification
    declaration=delivery_workflows.declaration(root,config)
    if declaration.get('mode')!='godot-static':raise ValueError('this publisher only preserves the static config-image protocol')
    selected=godot_static.declaration(root,config)
    definition=verification.release_definition(root/config['artifacts']['definition'])
    repository,run,attempt=(environment[key] for key in ['GITHUB_REPOSITORY','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT'])
    if repository!=selected['repository']:raise ValueError('static publication belongs to another repository')
    jobs=json.loads(capture(['gh','api',f'repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100'],root,environment))
    sdk.accept(proof,config,source,tools_ref,sdk.git('-C',str(root),'rev-parse','HEAD^{tree}'),definition['tests'],jobs,environment)
    godot_ci.validate_acceptance(proof.get('godot_matrix'),config,jobs,source,tools_ref,environment,proof['configuration_sha256'])
    sdk.stage(incoming,proof,root/'.artifacts/release',config['artifacts']['release_metadata'])
    blueprint=godot_static.verify_blueprint(root,config,environment)
    public=json.loads(godot_static.read(root/godot_static.MANIFEST_ARTIFACT))
    if public.get('source_commit')!=source or not re.fullmatch('[a-f0-9]{64}',public.get('release_id','')):
        raise ValueError('static publication requires its exact accepted ordinary release identity')
    release=module('static_release_validation',tools/'helpers/release.py')
    data=json.loads((root/'.artifacts/release/release.json').read_text());archive=root/'.artifacts/release/images.tar.gz'
    components=[reference.removeprefix(definition['image_base']+'-').removesuffix(':{revision}') for reference in definition['inputs']['images']]
    refs=release.validate(data,definition['project'],source,components,archive,definition['image_base'])
    release.validate_provenance(data,source)
    release.validate_test_evidence(incoming/'.artifacts/verification',source,'validate',definition)
    execute(['docker','load','--input',str(archive)],root,environment,600)
    if release.inspect(refs)!=data['images']:raise ValueError('transferred game image IDs differ from complete acceptance')
    return config,declaration,selected,blueprint,public,data,godot_static


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools',type=Path,required=True);parser.add_argument('--tools-ref',required=True)
    parser.add_argument('--source',required=True);parser.add_argument('--incoming',type=Path,default=Path('.artifacts/delivery-input'))
    args=parser.parse_args();root=Path.cwd();tools=args.tools.resolve();environment=dict(os.environ)
    try:
        config,declaration,selected,blueprint,public,data,static=admit(root,tools,args.source,args.tools_ref,args.incoming,environment)
        reference='ghcr.io/'+selected['repository']+'-gate:'+args.source
        expected=data['images'].get(reference)
        if not re.fullmatch('sha256:[a-f0-9]{64}',expected or ''):raise ValueError('static gate is absent from complete native image acceptance')
        gate=publish_gate(reference,expected,root,environment)
        config_ref=publish_config(blueprint,gate,args.source,static,root,environment)
        (root/'release').mkdir(exist_ok=True);(root/'release/release-manifest.json').write_text(json.dumps(public)+'\n')
        record=module('static_release_record',tools/'helpers/godot_release_record.py');record.REPO=selected['repository']
        if selected['record']:
            record.write_record()
            observed=json.loads((root/'release-record.json').read_text())
            if observed['gate_ref']!=gate or observed['config_ref']!=config_ref:raise ValueError('static release record differs from the published immutable images')
        values={'gate-id':expected,'gate-ref':gate,'config-ref':config_ref,'release-id':public['release_id'],'record':str(selected['record']).lower(),
          'health-urls':'\n'.join(entry['url']+' '+entry['contains'].replace('{revision}',args.source) if isinstance(entry,dict) else entry for entry in declaration['health_urls'])}
        with open(environment['GITHUB_OUTPUT'],'a') as output:
            for key,value in values.items():
                delimiter='static-'+os.urandom(16).hex();output.write(f'{key}<<{delimiter}\n{value}\n{delimiter}\n')
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as error:parser.exit(1,str(error)+'\n')


if __name__=='__main__':main()
