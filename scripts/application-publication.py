#!/usr/bin/env python3
"""Admit same-run recipe evidence before publishing its exact image archive."""
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import recipe_data
import recipe_context
import publication


def git(*arguments):
    return subprocess.check_output(['git', *arguments], text=True, timeout=30).strip()


def read(path, limit=4 << 20):
    if not path.is_file() or path.is_symlink() or any(p.is_symlink() for p in path.parents) or path.stat().st_size > limit:
        raise ValueError('publication evidence must be a bounded regular file')
    return json.loads(path.read_text())


def accept(proof, config, source, tools, tree, groups, jobs, environment, workflow_source=None):
    if (type(proof.get('version')) is not int or proof['version'] != 1 or proof.get('source_commit') != source
            or proof.get('source_tree') != tree or proof.get('cookbook_commit') != tools
            or proof.get('recipe') != config.get('recipe') or config.get('recipe') not in {'react-native', 'godot-game'}
            or proof.get('status') != 'passed' or proof.get('tracked_source_clean') is not True
            or proof.get('build_verified') is not True or proof.get('tests_verified') is not True
            or set(proof.get('test_groups', [])) != set(groups)
            or not {'lint', 'unit', 'security'} <= set(groups)):
        raise ValueError('publication requires complete same-source application acceptance')
    context = proof.get('execution_context', {})
    for key in ['GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT',
                'GITHUB_SHA', 'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA', 'GITHUB_EVENT_NAME']:
        if not environment.get(key) or context.get(key) != environment[key]:
            raise ValueError('publication evidence belongs to another workflow execution')
    if context.get('GITHUB_JOB') != 'validate':
        raise ValueError('publication evidence must come from the declared shared validation gate')
    if jobs.get('total_count') != len(jobs.get('jobs', [])):
        raise ValueError('publication requires complete current-attempt job metadata')
    hosts = [job for job in jobs['jobs'] if job['name'].endswith('/ Make full')]
    if len(hosts) != 1 or hosts[0].get('conclusion') != 'success' or hosts[0].get('head_sha') != (workflow_source or source):
        raise ValueError('the actual full validation job must have succeeded at this source')
    if config['ci']['runner'] not in hosts[0].get('labels', []):
        raise ValueError('full validation ran on another declared runner')
    if config['recipe'] == 'godot-game' and proof.get('coverage') != 'complete-host-and-godot-matrix':
        raise ValueError('Godot publication requires complete build, shard and capability acceptance')


def stage(incoming, proof, destination, metadata=()):
    return publication.stage(incoming, proof, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--cookbook-root', type=Path, required=True)
    parser.add_argument('--cookbook-ref', required=True)
    parser.add_argument('--tools-root', type=Path, required=True, help='Helper identity is owned by the accepted Cookbook provider')
    parser.add_argument('--project', required=True)
    parser.add_argument('--incoming', type=Path, default=Path('.artifacts/delivery-input'))
    args = parser.parse_args()
    try:
        if (os.environ.get('GITHUB_REF') != 'refs/heads/main' or os.environ.get('GITHUB_EVENT_NAME') not in {'push', 'workflow_dispatch'}
                or not re.fullmatch('[a-f0-9]{40}', args.source) or args.source != os.environ.get('GITHUB_SHA')
                or git('rev-parse', 'HEAD') != args.source):
            raise ValueError('publication requires the exact final main source')
        context = recipe_context.accepted_context(args.cookbook_root, args.cookbook_ref,
            lambda path: json.loads(subprocess.check_output(['gh', 'api', path], timeout=30)), args.tools_root)
        cookbook, helpers, revision = context.cookbook, context.tools, context.cookbook_revision
        os.environ.update(COOKBOOK_ROOT=str(cookbook), COOKBOOK_REF=revision,
                          TOOLS_ROOT=str(helpers), TOOLS_REF=context.tools_revision)
        if subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], timeout=30).returncode:
            raise ValueError('publisher checkout must be clean')
        config = recipe_data.load(Path.cwd(), cookbook)
        definition_name = recipe_data.effective_definition(cookbook, config)
        proof = read(args.incoming / '.artifacts/cookbook-validation.json')
        if context is not None:
            context.require_proof(proof)
        if proof.get('configuration_sha256') != hashlib.sha256(Path('cookbook.toml').read_bytes()).hexdigest():
            raise ValueError('publication recipe differs from the accepted source')
        spec = importlib.util.spec_from_file_location('publication_verification', helpers / 'helpers/verification.py')
        verify = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verify)
        definition = verify.release_definition(Path(definition_name))
        if definition['project'] != args.project:
            raise ValueError('publication project differs from its source declaration')
        repository, run, attempt = (os.environ[key] for key in ['GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'])
        jobs = json.loads(subprocess.check_output(['gh', 'api',
            f'repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100'], timeout=30))
        accept(proof, config, args.source, revision, git('rev-parse', 'HEAD^{tree}'), definition['tests'], jobs, os.environ)
        if config['recipe'] == 'godot-game':
            sys.path.insert(0, str(cookbook.resolve() / 'cookbook'))
            import godot_ci
            recipe_data.checked_module(godot_ci, cookbook.resolve(), 'godot_ci')
            godot_ci.validate_acceptance(proof.get('godot_matrix'), config, jobs, args.source,
                revision, os.environ, proof['configuration_sha256'], root=Path.cwd())
        receipts = publication.admit_receipts(args.incoming, proof, args.source, definition['tests'])
        packet = publication.stage(args.incoming, proof, Path('.artifacts/release'), args.project)
        if packet.descriptor['source'] != {'repository': repository, 'repository_id': os.environ['GITHUB_REPOSITORY_ID'], 'revision': args.source}:
            raise ValueError('accepted release differs from its exact workflow source')
        published = publication.publish(packet, helpers, os.environ)
        publication.write_evidence(packet, proof, published, os.environ, receipts=receipts)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
