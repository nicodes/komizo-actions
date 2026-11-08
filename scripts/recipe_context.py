"""Keep the accepted Cookbook implementation and its Tools dependency distinct."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

sys.dont_write_bytecode = True
SHA = re.compile(r'[a-f0-9]{40}\Z')


def discovery_module():
    spec = importlib.util.spec_from_file_location('context_cookbook_identity',
        Path(__file__).with_name('cookbook_identity.py'))
    identity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(identity)
    return identity


def git(root, *arguments):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    return subprocess.check_output(['git', '--no-replace-objects', '-c', 'core.hooksPath=' + os.devnull,
        '-c', 'core.fsmonitor=false', '-C', str(root), *arguments],
        env=environment, text=True, timeout=30).strip()


def checked_checkout(root, repository, revision):
    root = Path(root).absolute()
    if repository not in {'nicodes/cookbook', 'nicodes/tools'} or not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise ValueError('recipe automation requires its exact named source and immutable revision')
    if any(path.is_symlink() for path in [root, *root.parents]) or (root / '.git').is_symlink():
        raise ValueError('recipe automation source must not traverse links')
    if (git(root, 'rev-parse', 'HEAD') != revision
            or Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root.resolve()
            or discovery_module().canonical_origin(git(root, 'config', '--local', '--get-all', 'remote.origin.url')) != repository
            or git(root, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching')):
        raise ValueError('recipe automation source differs from its exact clean owning checkout')
    return root


def helper_reference(cookbook, revision):
    """Read the checked provider's sole helper pin before loading SDK code."""
    cookbook = checked_checkout(cookbook, 'nicodes/cookbook', revision)
    identity = discovery_module()
    source = identity.read_regular(cookbook / 'mise.toml')
    variables = tomllib.loads(source.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')).get('vars')
    reference = variables.get('tools_ref') if isinstance(variables, dict) else None
    if not isinstance(reference, str) or not SHA.fullmatch(reference):
        raise ValueError('Cookbook provider requires one literal immutable Tools dependency')
    checked_checkout(cookbook, 'nicodes/cookbook', revision)
    return {'repository': 'nicodes/tools', 'revision': reference}


def checked_module(module, cookbook, name):
    origin = getattr(module, '__file__', None)
    if not isinstance(origin, str) or Path(origin).resolve() != (cookbook / 'cookbook' / (name + '.py')).resolve():
        raise ValueError('recipe module came from another Cookbook source')
    return module


class Context:
    def __init__(self, cookbook, revision, supplied_tools=None):
        self.cookbook = checked_checkout(cookbook, 'nicodes/cookbook', revision)
        self.cookbook_revision = revision
        expected = helper_reference(self.cookbook, revision)
        dependency = self.module('dependencies')
        if dependency.tools_identity() != expected:
            raise ValueError('Cookbook helper identity differs from its checked provider pin')
        self.tools = checked_checkout(dependency.tools_root(supplied_tools), expected['repository'], expected['revision'])
        self.tools_revision = expected['revision']
        checked_checkout(self.cookbook, 'nicodes/cookbook', revision)

    def module(self, name):
        if not re.fullmatch('[a-z][a-z0-9_]*', name):
            raise ValueError('select one known Cookbook Python module')
        path = self.cookbook / 'cookbook' / (name + '.py')
        if path.is_symlink() or not path.is_file():
            raise ValueError('Cookbook module must be tracked regular source')
        git(self.cookbook, 'ls-files', '--error-unmatch', '--', 'cookbook/' + name + '.py')
        if hasattr(self, 'tools'):
            self.activate()
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(self.cookbook / 'cookbook'))
        import importlib
        return checked_module(importlib.import_module(name), self.cookbook, name)

    def identities(self):
        return {'cookbook_repository': 'nicodes/cookbook', 'cookbook_commit': self.cookbook_revision,
                'tools_repository': 'nicodes/tools', 'tools_commit': self.tools_revision}

    def environment(self, environment=None):
        return {**(os.environ if environment is None else environment),
                'COOKBOOK_ROOT': str(self.cookbook), 'COOKBOOK_REF': self.cookbook_revision,
                'TOOLS_ROOT': str(self.tools), 'TOOLS_REF': self.tools_revision}

    def activate(self):
        os.environ.update(self.environment())

    def require_accepted(self, lookup):
        for repository, revision in [('nicodes/cookbook', self.cookbook_revision), ('nicodes/tools', self.tools_revision)]:
            result = lookup('repos/' + repository + '/compare/' + revision + '...main')
            if not isinstance(result, dict) or result.get('status') not in {'ahead', 'identical'}:
                raise ValueError('recipe automation requires both real sources accepted on their owning main')

    def require_proof(self, proof):
        if (proof.get('entrypoint_contract') != 'mise-v1'
                or any(proof.get(key) != value for key, value in self.identities().items())):
            raise ValueError('recipe proof must bind the actual Cookbook and Tools sources and current entrypoint')


def accepted_context(cookbook, revision, lookup, supplied_tools=None):
    """Require source acceptance before importing any downloaded SDK module."""
    accepted_dependency(cookbook, revision, lookup)
    context = Context(cookbook, revision, supplied_tools)
    context.activate()
    return context


def accepted_dependency(cookbook, revision, lookup):
    """Select the provider-owned helper identity before its checkout."""
    tools = helper_reference(cookbook, revision)
    for repository, source in [('nicodes/cookbook', revision), (tools['repository'], tools['revision'])]:
        result = lookup('repos/' + repository + '/compare/' + source + '...main')
        if not isinstance(result, dict) or result.get('status') not in {'ahead', 'identical'}:
            raise ValueError('recipe automation source has not been accepted on its owning main')
    return tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['dependency', 'sources'])
    parser.add_argument('--cookbook-root', type=Path, required=True)
    parser.add_argument('--cookbook-ref', required=True)
    parser.add_argument('--github-output', action='store_true')
    parser.add_argument('--tools-root', type=Path)
    args = parser.parse_args()
    try:
        tools = accepted_dependency(args.cookbook_root, args.cookbook_ref,
            lambda path: json.loads(subprocess.check_output(['gh', 'api', path], timeout=30)))
        if args.command == 'sources':
            if args.tools_root is None:
                raise ValueError('source admission requires its exact prepared generic helper location')
            context = Context(args.cookbook_root, args.cookbook_ref, args.tools_root)
            values = context.environment()
            with open(os.environ['GITHUB_ENV'], 'a') as output:
                for key in ['COOKBOOK_ROOT', 'COOKBOOK_REF', 'TOOLS_ROOT', 'TOOLS_REF']:
                    if any(char in values[key] for char in '\r\n\0'):
                        raise ValueError('source locations must be bounded literal metadata')
                    output.write(key + '=' + values[key] + '\n')
        if args.github_output:
            destination = Path(os.environ['GITHUB_OUTPUT'])
            if not destination.is_absolute() or destination.is_symlink() or not destination.is_file():
                raise ValueError('dependency output requires the owning runner metadata file')
            with destination.open('a') as stream:
                stream.write('tools-ref=' + tools['revision'] + '\n')
        print(json.dumps(tools))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
