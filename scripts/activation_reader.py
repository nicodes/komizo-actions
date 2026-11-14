"""Select the separate immutable historical-activation reader, never build Tools."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / '.github/activation-reader.json'


def context_module():
    spec = importlib.util.spec_from_file_location('_activation_recipe_context', ROOT / 'scripts/recipe_context.py')
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate activation reader identity field')
        result[key] = value
    return result


def identity():
    context = context_module()
    value = json.loads(context.discovery_module().read_regular(PIN), object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {'repository', 'revision', 'role'}
            or value['repository'] != 'nicodes/tools' or value['role'] != 'historical-activation'
            or not isinstance(value['revision'], str) or not re.fullmatch('[a-f0-9]{40}', value['revision'])):
        raise ValueError('activation reader requires its separate literal immutable Tools identity')
    return value


def checkout(root, lookup):
    selected = identity()
    context = context_module()
    root = context.checked_checkout(root, selected['repository'], selected['revision'])
    accepted = lookup('repos/' + selected['repository'] + '/compare/' + selected['revision'] + '...main')
    if not isinstance(accepted, dict) or accepted.get('status') not in {'ahead', 'identical'}:
        raise ValueError('activation reader must first be accepted on its owning main')
    context.checked_checkout(root, selected['repository'], selected['revision'])
    return root, selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--github-output', action='store_true', required=True)
    args = parser.parse_args()
    selected = identity()
    destination = Path(os.environ['GITHUB_OUTPUT'])
    if not destination.is_absolute() or destination.is_symlink() or not destination.is_file():
        parser.error('activation reader output requires the owning runner metadata file')
    with destination.open('a') as stream:
        stream.write('repository=' + selected['repository'] + '\n')
        stream.write('revision=' + selected['revision'] + '\n')


if __name__ == '__main__':
    main()
