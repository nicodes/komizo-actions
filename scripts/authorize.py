#!/usr/bin/env python3
"""Re-admit immutable same-run publication and mint fresh release authority at the host boundary."""
import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oci_registry as registry
import publication
import recipe_data
import recipe_context
import release_packet
import submission

FILES = {'release.json', 'publication.json', 'validation.json', 'inventory.json', 'build.json', 'test.json'}
MAX_ARCHIVE = 24 << 20


def api(path):
    raw = submission.transport(['gh', 'api', path], input=b'', timeout=30)
    return json.loads(raw, object_pairs_hook=registry.unique_object)


def retrieve(repository, source, run, attempt, destination, lookup=api):
    metadata = lookup(f'repos/{repository}/actions/runs/{run}/artifacts?per_page=100')
    if metadata.get('total_count') != len(metadata.get('artifacts', [])):
        raise ValueError('release authority needs complete same-run artifact metadata')
    name = f'host-release-{source}-{run}-{attempt}'
    found = [entry for entry in metadata['artifacts'] if entry.get('name') == name]
    if (len(found) != 1 or found[0].get('expired') is not False or type(found[0].get('id')) is not int
            or found[0]['id'] < 1 or type(found[0].get('size_in_bytes')) is not int
            or not 0 < found[0]['size_in_bytes'] <= MAX_ARCHIVE
            or not isinstance(found[0].get('digest'), str) or not registry.DIGEST.fullmatch(found[0]['digest'])):
        raise ValueError('release authority requires one exact immutable source/run/attempt artifact')
    selected = found[0]
    # The signed GitHub artifact digest authenticates the whole ZIP before parsing.
    raw = submission.transport(['gh', 'api', '--method', 'GET', f"repos/{repository}/actions/artifacts/{selected['id']}/zip"],
                               input=b'', timeout=60, max_output=MAX_ARCHIVE)
    if len(raw) != selected['size_in_bytes'] or registry.digest(raw) != selected['digest']:
        raise ValueError('publication artifact bytes differ from GitHub immutable identity')
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) != len(FILES) or {entry.filename for entry in entries} != FILES:
            raise ValueError('publication evidence artifact is not closed')
        for entry in entries:
            mode = entry.external_attr >> 16 & 0o170000
            if entry.is_dir() or mode not in {0, 0o100000} or not 0 < entry.file_size <= 4 << 20:
                raise ValueError('publication evidence contains unsafe or excessive metadata')
        for entry in entries:
            body = archive.read(entry)
            if len(body) != entry.file_size: raise ValueError('publication evidence size changed')
            with (destination / entry.filename).open('xb') as output:
                os.fchmod(output.fileno(), 0o600); output.write(body)
    return destination


def admit(directory, application, source, configuration, cookbook_revision, groups, jobs, environment, *, providers, combined=False):
    submission.workflow(environment)
    directory = Path(directory)
    if {path.name for path in directory.iterdir()} != FILES:
        raise ValueError('fresh authority requires the complete closed publication evidence')
    values = {name: release_packet.document(directory / name) for name in FILES}
    descriptor, published, proof = (values[name] for name in ['release.json', 'publication.json', 'validation.json'])
    raw = release_packet.release(descriptor, application)
    if (descriptor.get('providers') != providers or proof.get('providers') != providers
            or providers.get('cookbook_revision') != cookbook_revision):
        raise ValueError('release providers differ from complete tested and explicitly selected source providers')
    if release_packet.read(directory / 'release.json') != raw:
        raise ValueError('publication release bytes are not canonical')
    identity = registry.digest(raw)
    if descriptor['source'] != {'repository': environment['GITHUB_REPOSITORY'], 'repository_id': environment['GITHUB_REPOSITORY_ID'], 'revision': source}:
        raise ValueError('publication source identity differs from the requested application')
    context = published.get('execution_context', {})
    for key in ['GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_REPOSITORY_ID', 'GITHUB_SHA',
                'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA', 'GITHUB_EVENT_NAME']:
        if context.get(key) != environment.get(key): raise ValueError('publication belongs to another caller, run or attempt')
    if (published.get('kind') != 'komizo.publication' or published.get('version') != 1
            or published.get('release_digest') != identity or published.get('source') != descriptor['source']
            or published.get('published') != {name: artifact['reference'] for name, artifact in descriptor['artifacts'].items()}
            or published.get('validation_sha256') != hashlib.sha256(registry.canonical(proof)).hexdigest()
            or published.get('inventory_sha256') != hashlib.sha256(release_packet.read(directory / 'inventory.json')).hexdigest()):
        raise ValueError('publication lacks complete exact release, inventory and validation identities')
    inventory = values['inventory.json']
    if (not isinstance(inventory, dict) or set(inventory) != {'version', 'files'} or type(inventory.get('version')) is not int
            or inventory['version'] != 1 or not isinstance(inventory.get('files'), dict)
            or not 1 <= len(inventory['files']) <= release_packet.MAX_FILES):
        raise ValueError('publication packet inventory is invalid')
    artifacts = proof.get('artifacts', {})
    for name, record in inventory['files'].items():
        if (set(record) != {'sha256', 'size', 'type'} or record['type'] != release_packet.member_type(name)
                or not isinstance(record['sha256'], str) or not release_packet.HEX.fullmatch(record['sha256'])
                or type(record['size']) is not int or not 0 <= record['size'] <= release_packet.MAX_FILE
                or artifacts.get('.artifacts/release/' + name) != record['sha256']):
            raise ValueError('publication packet differs from complete CI output acceptance')
    if artifacts.get('.artifacts/release/inventory.json') != published['inventory_sha256']:
        raise ValueError('publication inventory was not accepted by the complete source gate')
    if inventory['files'].get('release.json') != {'sha256': identity[7:], 'size': len(raw), 'type': 'release'}:
        raise ValueError('publication descriptor differs from its accepted byte inventory')
    spec = importlib.util.spec_from_file_location('_late_acceptance', Path(__file__).with_name('application-publication.py'))
    acceptance = importlib.util.module_from_spec(spec); spec.loader.exec_module(acceptance)
    acceptance.accept(proof, configuration, source, cookbook_revision, proof.get('source_tree'), groups, jobs,
                      environment, workflow_source=environment['GITHUB_SHA'])
    publication.admit_records(values['build.json'], values['test.json'], proof, source, groups)
    if combined:
        if context.get('GITHUB_JOB') != 'deliver' or environment.get('GITHUB_JOB') != 'deliver':
            raise ValueError('combined publication requires its same protected immutable job')
    else:
        if context.get('GITHUB_JOB') != 'publish': raise ValueError('publication did not use its declared producer role')
        writers = [job for job in jobs['jobs'] if job.get('name', '').endswith('/ publish') or job.get('name', '').endswith('/ Publish exact Preview images')]
        if len(writers) != 1 or writers[0].get('conclusion') != 'success' or writers[0].get('head_sha') != environment['GITHUB_SHA']:
            raise ValueError('independent trusted publisher has not completed successfully')
    return SimpleNamespace(descriptor=descriptor, release_digest=identity, release_bytes=raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--cookbook-root', required=True, type=Path)
    parser.add_argument('--cookbook-ref', required=True)
    parser.add_argument('--tools-root', required=True, type=Path)
    parser.add_argument('--directory', type=Path, default=Path('.artifacts/komizo-release'))
    parser.add_argument('--combined', action='store_true')
    parser.add_argument('--preview-plan', type=Path)
    args = parser.parse_args()
    try:
        environment = dict(os.environ); submission.workflow(environment)
        context = recipe_context.accepted_context(args.cookbook_root, args.cookbook_ref, api, args.tools_root)
        configuration = recipe_data.load(Path.cwd(), context.cookbook, context.module('configuration'))
        if recipe_context.git(Path.cwd(), 'rev-parse', 'HEAD') != environment['GITHUB_SHA']:
            raise ValueError('fresh authority requires its actual trusted main controller checkout')
        generator = context.module('generate')
        for name, expected in generator.workflow_files(Path.cwd(), args.cookbook_ref, configuration).items():
            if subprocess.check_output(['git', 'show', environment['GITHUB_SHA'] + ':' + name], timeout=30) != expected.encode():
                raise ValueError('caller workflows differ from their accepted canonical generator')
        if args.preview_plan:
            import preview
            plan = preview.validate_plan(args.preview_plan.read_text(), Path.cwd(), context.cookbook, environment, context=context)
            if plan['head'] != args.source or plan['policy']['project'] != args.application or plan['action'] != 'up':
                raise ValueError('fresh preview authority differs from its current authorized request')
            groups = plan['policy']['test_groups']
        else:
            if environment.get('GITHUB_WORKFLOW_REF') != environment['GITHUB_REPOSITORY'] + '/.github/workflows/cd.yml@refs/heads/main':
                raise ValueError('production authority requires its exact trusted main caller')
            if args.source != environment['GITHUB_SHA'] or api('repos/' + environment['GITHUB_REPOSITORY'] + '/branches/main')['commit']['sha'] != args.source:
                raise ValueError('production source is no longer current main')
            declaration = context.module('delivery_workflows').declaration(Path.cwd(), configuration)
            if bool(declaration.get('combined', False)) != args.combined: raise ValueError('combined delivery differs from authored source policy')
            groups = context.module('recipe').test_suites(Path.cwd(), configuration)
        run, attempt = environment['GITHUB_RUN_ID'], environment['GITHUB_RUN_ATTEMPT']
        metadata = api(f"repos/{environment['GITHUB_REPOSITORY']}/actions/runs/{run}")
        if (str(metadata.get('run_attempt')) != attempt or metadata.get('head_sha') != environment['GITHUB_SHA']
                or metadata.get('repository', {}).get('full_name') != environment['GITHUB_REPOSITORY']
                or metadata.get('head_repository', {}).get('full_name') != environment['GITHUB_REPOSITORY']):
            raise ValueError('authorization run belongs to another source, repository or attempt')
        references = metadata.get('referenced_workflows', [])
        required = {'nicodes/cookbook/.github/workflows/cookbook-ci.yml@' + args.cookbook_ref}
        own = environment.get('KOMIZO_REF', '')
        if not release_packet.COMMIT.fullmatch(own): raise ValueError('authorization has no immutable Actions provider')
        required.add('nicodes/komizo-actions/.github/workflows/' + ('cookbook-preview-effects.yml' if args.preview_plan else 'cookbook-deliver.yml') + '@' + own)
        if not args.combined:
            required.add('nicodes/komizo-actions/.github/workflows/' + ('cookbook-preview-publish.yml' if args.preview_plan else 'cookbook-publish.yml') + '@' + own)
        actual = {entry.get('path'): entry.get('sha') for entry in references}
        if any(actual.get(path) != path.rsplit('@', 1)[1] for path in required):
            raise ValueError('authorization requires its exact independent CI, publisher and delivery providers')
        if not args.combined:
            retrieve(environment['GITHUB_REPOSITORY'], args.source, run, attempt, args.directory)
        jobs = api(f"repos/{environment['GITHUB_REPOSITORY']}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100")
        context.require_proof(release_packet.document(args.directory / 'validation.json'))
        providers = {'cookbook_revision': context.cookbook_revision, 'tools_revision': context.tools_revision,
                     'core_revision': context.module('komizo_core').pin()['revision']}
        admitted = admit(args.directory, args.application, args.source, configuration, args.cookbook_ref, groups, jobs, environment,
                         providers=providers, combined=args.combined)
        source = api(f"repos/{environment['GITHUB_REPOSITORY']}/git/commits/{args.source}")
        proof = release_packet.document(args.directory / 'validation.json')
        if source.get('sha') != args.source or source.get('tree', {}).get('sha') != proof['source_tree']:
            raise ValueError('publication source tree differs from immutable Git source')
        authored = api(f"repos/{environment['GITHUB_REPOSITORY']}/contents/cookbook.toml?ref={args.source}")
        if (authored.get('type') != 'file' or authored.get('encoding') != 'base64'
                or type(authored.get('size')) is not int or not 0 < authored['size'] <= 262144):
            raise ValueError('publication configuration source is not a bounded immutable file')
        authored_raw = base64.b64decode(''.join(authored.get('content', '').split()), validate=True)
        if len(authored_raw) != authored['size'] or hashlib.sha256(authored_raw).hexdigest() != proof.get('configuration_sha256'):
            raise ValueError('publication configuration differs from its exact accepted source bytes')
        if args.preview_plan:
            preview.validate_plan(args.preview_plan.read_text(), Path.cwd(), context.cookbook, environment, context=context)
        elif api('repos/' + environment['GITHUB_REPOSITORY'] + '/branches/main')['commit']['sha'] != args.source:
            raise ValueError('production source advanced before fresh release authorization')
        # Mint only after all queue, backup, source and publication checks above.
        value = publication.release_proof(admitted, environment)
        target = args.directory / 'proof.json'
        with target.open('xb') as output:
            os.fchmod(output.fileno(), 0o600); output.write(registry.canonical(value))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile):
        parser.exit(1, 'Fresh release authorization refused; no host mutation submitted.\n')


if __name__ == '__main__': main()
