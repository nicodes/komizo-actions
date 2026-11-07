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


def git(*arguments):
    return subprocess.check_output(['git', *arguments], text=True, timeout=30).strip()


def read(path, limit=4 << 20):
    if not path.is_file() or path.is_symlink() or any(p.is_symlink() for p in path.parents) or path.stat().st_size > limit:
        raise ValueError('publication evidence must be a bounded regular file')
    return json.loads(path.read_text())


def accept(proof, config, source, tools, tree, groups, jobs, environment, workflow_source=None):
    if (proof.get('version') != 1 or proof.get('source_commit') != source
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
    source = incoming / '.artifacts/release'
    expected = {'images.tar.gz', 'images.tar.gz.sha256', 'images.json', 'release.json'}
    if not isinstance(metadata, (list, tuple)) or len(metadata) > 16 or len(set(metadata)) != len(metadata):
        raise ValueError('release metadata requires a bounded distinct declaration')
    for name in metadata:
        if not isinstance(name, str) or not re.fullmatch(r'\.artifacts/release/[A-Za-z0-9][A-Za-z0-9_.-]*\.json', name):
            raise ValueError('release metadata must name a flat JSON artifact')
        filename = Path(name).name
        if filename in expected:
            raise ValueError('release metadata cannot replace the core release artifacts')
        expected.add(filename)
    if not source.is_dir() or {path.name for path in source.iterdir()} != expected:
        raise ValueError('publication requires the closed validated release archive')
    for name in expected:
        path = source / name
        if not path.is_file() or path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError('publication payload must contain only regular files')
        if name == 'images.json' or '.artifacts/release/' + name in metadata:
            read(path, limit=1 << 20)
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if proof.get('artifacts', {}).get('.artifacts/release/' + name) != digest:
            raise ValueError('publication bytes differ from the full source acceptance')
    destination.mkdir(mode=0o700, parents=True)
    for name in expected:
        shutil.copyfile(source / name, destination / name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--tools', type=Path, help='Historical Tools-hosted recipe SDK')
    parser.add_argument('--tools-ref', help='Historical Tools-hosted recipe SDK revision')
    parser.add_argument('--cookbook-root', type=Path)
    parser.add_argument('--cookbook-ref')
    parser.add_argument('--tools-root', type=Path, help='Prepared helper location; identity is owned by Cookbook')
    parser.add_argument('--project', required=True)
    parser.add_argument('--incoming', type=Path, default=Path('.artifacts/delivery-input'))
    args = parser.parse_args()
    try:
        if (os.environ.get('GITHUB_REF') != 'refs/heads/main' or os.environ.get('GITHUB_EVENT_NAME') not in {'push', 'workflow_dispatch'}
                or not re.fullmatch('[a-f0-9]{40}', args.source) or args.source != os.environ.get('GITHUB_SHA')
                or git('rev-parse', 'HEAD') != args.source):
            raise ValueError('publication requires the exact final main source')
        context = None
        if args.cookbook_root is not None or args.cookbook_ref is not None:
            if args.cookbook_root is None or args.cookbook_ref is None or args.tools is not None or args.tools_ref is not None:
                raise ValueError('select one complete Cookbook identity without a historical SDK alias')
            context = recipe_context.accepted_context(args.cookbook_root, args.cookbook_ref,
                lambda path: json.loads(subprocess.check_output(['gh', 'api', path], timeout=30)), args.tools_root)
            cookbook, helpers, revision = context.cookbook, context.tools, context.cookbook_revision
            os.environ.update(COOKBOOK_ROOT=str(cookbook), COOKBOOK_REF=revision,
                              TOOLS_ROOT=str(helpers), TOOLS_REF=context.tools_revision)
        else:
            if args.tools is None or args.tools_ref is None or args.tools_root is not None:
                raise ValueError('select the complete historical recipe SDK identity')
            if git('-C', str(args.tools), 'rev-parse', 'HEAD') != args.tools_ref:
                raise ValueError('publisher cookbook checkout differs from its accepted pin')
            cookbook, helpers, revision = args.tools, args.tools, args.tools_ref
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
        stage(args.incoming, proof, Path('.artifacts/release'), config.get('artifacts', {}).get('release_metadata', []))
        evidence = args.incoming / '.artifacts/verification'
        prefix = definition['image_base'] + '-'
        references = definition['inputs']['images']
        if any(not reference.startswith(prefix) or not reference.endswith(':{revision}') for reference in references):
            raise ValueError('publication images must bind the declared family and source')
        components = [reference[len(prefix):-len(':{revision}')] for reference in references]
        subprocess.run([sys.executable, str(helpers / 'helpers/release.py'), 'publish',
                        '--project', args.project, '--revision', args.source, '--components', *components,
                        '--definition', definition_name, '--test-job', 'validate',
                        '--test-evidence-directory', str(evidence)], check=True, timeout=2400)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
