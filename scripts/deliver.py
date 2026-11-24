#!/usr/bin/env python3
"""Publish and authorize one release inside the single job that built and tested it."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oci_registry as registry
import publication
import recipe_context
import recipe_data
import release_packet
import submission

RELEASE = Path('.artifacts/release')
EVIDENCE = Path('.artifacts/komizo-release')
PROOF = Path('.artifacts/cookbook-validation.json')


def api(path):
    raw = submission.transport(['gh', 'api', path], input=b'', timeout=30)
    return json.loads(raw, object_pairs_hook=registry.unique_object)


def git(*arguments):
    return subprocess.check_output(['git', *arguments], text=True, timeout=30).strip()


def read(path, limit=4 << 20):
    if not path.is_file() or path.is_symlink() or any(p.is_symlink() for p in path.parents) or path.stat().st_size > limit:
        raise ValueError('delivery evidence must be a bounded regular file')
    return json.loads(path.read_text())


def require_main_source(source, environment):
    if (environment.get('GITHUB_REF') != 'refs/heads/main' or environment.get('GITHUB_EVENT_NAME') not in {'push', 'workflow_dispatch'}
            or not re.fullmatch('[a-f0-9]{40}', source) or source != environment.get('GITHUB_SHA')
            or git('rev-parse', 'HEAD') != source):
        raise ValueError('delivery requires the exact final main source')


def accept_local(proof, config, source, tools, tree, groups, environment):
    """Accept the proof of this very job; no other job can have produced it."""
    if (type(proof.get('version')) is not int or proof['version'] != 1 or proof.get('source_commit') != source
            or proof.get('source_tree') != tree or proof.get('cookbook_commit') != tools
            or proof.get('recipe') != config.get('recipe') or config.get('recipe') not in {'react-native', 'godot-game'}
            or proof.get('status') != 'passed' or proof.get('tracked_source_clean') is not True
            or proof.get('build_verified') is not True or proof.get('tests_verified') is not True
            or set(proof.get('test_groups', [])) != set(groups)
            or not {'lint', 'unit', 'security'} <= set(groups)):
        raise ValueError('delivery requires complete same-source application acceptance')
    context = proof.get('execution_context', {})
    for key in ['GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT',
                'GITHUB_SHA', 'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA', 'GITHUB_EVENT_NAME']:
        if not environment.get(key) or context.get(key) != environment[key]:
            raise ValueError('delivery evidence belongs to another workflow execution')
    if not environment.get('GITHUB_JOB') or context.get('GITHUB_JOB') != environment['GITHUB_JOB']:
        raise ValueError('delivery evidence must come from this same job')


def run_publish(args, environment, lookup=api):
    require_main_source(args.source, environment)
    context = recipe_context.accepted_context(args.cookbook_root, args.cookbook_ref, lookup, args.tools_root)
    cookbook, helpers, revision = context.cookbook, context.tools, context.cookbook_revision
    os.environ.update(COOKBOOK_ROOT=str(cookbook), COOKBOOK_REF=revision,
                      TOOLS_ROOT=str(helpers), TOOLS_REF=context.tools_revision)
    environment = {**environment, **{key: os.environ[key] for key in ['COOKBOOK_ROOT', 'COOKBOOK_REF', 'TOOLS_ROOT', 'TOOLS_REF']}}
    if subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], timeout=30).returncode:
        raise ValueError('publisher checkout must be clean')
    config = recipe_data.load(Path.cwd(), cookbook)
    definition_name = recipe_data.effective_definition(cookbook, config)
    proof = read(PROOF)
    context.require_proof(proof)
    if proof.get('configuration_sha256') != hashlib.sha256(Path('cookbook.toml').read_bytes()).hexdigest():
        raise ValueError('publication recipe differs from the accepted source')
    spec = importlib.util.spec_from_file_location('publication_verification', helpers / 'helpers/verification.py')
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    definition = verify.release_definition(Path(definition_name))
    if definition['project'] != args.project:
        raise ValueError('publication project differs from its source declaration')
    accept_local(proof, config, args.source, revision, git('rev-parse', 'HEAD^{tree}'), definition['tests'], environment)
    receipts = publication.admit_receipts(Path.cwd(), proof, args.source, definition['tests'])
    packet = release_packet.Packet(RELEASE, application=args.project)
    packet.require_proof(proof)
    if packet.descriptor['source'] != {'repository': environment['GITHUB_REPOSITORY'],
                                       'repository_id': environment['GITHUB_REPOSITORY_ID'], 'revision': args.source}:
        raise ValueError('accepted release differs from its exact workflow source')
    published = publication.publish(packet, helpers, environment)
    publication.write_evidence(packet, proof, published, environment, EVIDENCE, receipts=receipts)


def require_current_main(environment, source, lookup):
    if lookup('repos/' + environment['GITHUB_REPOSITORY'] + '/branches/main')['commit']['sha'] != source:
        raise ValueError('production source advanced before fresh release authorization')


def run_authorize(args, environment, lookup=api):
    if environment.get('GITHUB_WORKFLOW_REF') != environment['GITHUB_REPOSITORY'] + '/.github/workflows/deploy.yml@refs/heads/main':
        raise ValueError('production authority requires its exact trusted main caller')
    if args.source != environment['GITHUB_SHA'] or git('rev-parse', 'HEAD') != args.source:
        raise ValueError('fresh authority requires its actual trusted main controller checkout')
    require_current_main(environment, args.source, lookup)
    context = recipe_context.accepted_context(args.cookbook_root, args.cookbook_ref, lookup, args.tools_root)
    proof = read(PROOF)
    context.require_proof(proof)
    accept_local(proof, {'recipe': proof.get('recipe')}, args.source, args.cookbook_ref, git('rev-parse', 'HEAD^{tree}'),
                 proof.get('test_groups', []), environment)
    if proof.get('source_tree') != git('rev-parse', 'HEAD^{tree}'):
        raise ValueError('publication source tree differs from the checked-out Git source')
    if hashlib.sha256(Path('cookbook.toml').read_bytes()).hexdigest() != proof.get('configuration_sha256'):
        raise ValueError('publication configuration differs from its exact accepted source bytes')
    packet = release_packet.Packet(RELEASE, application=args.project)
    packet.require_proof(proof)
    providers = {'cookbook_revision': context.cookbook_revision, 'tools_revision': context.tools_revision,
                 'core_revision': context.module('komizo_core').pin()['revision']}
    if (packet.descriptor.get('providers') != providers or proof.get('providers') != providers
            or packet.descriptor['source'] != {'repository': environment['GITHUB_REPOSITORY'],
                                               'repository_id': environment['GITHUB_REPOSITORY_ID'], 'revision': args.source}):
        raise ValueError('release differs from the tested providers and exact source')
    published = read(EVIDENCE / 'publication.json')
    if (published.get('release_digest') != packet.release_digest
            or published.get('validation_sha256') != hashlib.sha256(registry.canonical(proof)).hexdigest()
            or published.get('execution_context', {}).get('GITHUB_JOB') != environment['GITHUB_JOB']):
        raise ValueError('publication evidence differs from this job release')
    require_current_main(environment, args.source, lookup)
    admitted = SimpleNamespace(descriptor=packet.descriptor, release_digest=packet.release_digest,
                               release_bytes=packet.release_bytes)
    value = publication.release_proof(admitted, environment)
    with (EVIDENCE / 'proof.json').open('xb') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(registry.canonical(value))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['publish', 'authorize']:
        command = commands.add_parser(name)
        command.add_argument('--source', required=True)
        command.add_argument('--cookbook-root', type=Path, required=True)
        command.add_argument('--cookbook-ref', required=True)
        command.add_argument('--tools-root', type=Path, required=True)
        command.add_argument('--project', required=True)
    args = parser.parse_args()
    try:
        (run_publish if args.command == 'publish' else run_authorize)(args, dict(os.environ))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n' if args.command == 'publish'
                    else 'Fresh release authorization refused; no host mutation submitted.\n')


if __name__ == '__main__':
    main()
