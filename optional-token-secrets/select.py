#!/usr/bin/env python3
"""Select explicitly named optional opaque tokens without logging their values."""
import os
import re


def selected(environment):
    result = {}
    for name, value in environment.items():
        if not name.startswith('KOMIZO_OPTIONAL_SECRET_'):
            continue
        suffix = name.removeprefix('KOMIZO_OPTIONAL_SECRET_')
        if not re.fullmatch('[A-Z][A-Z0-9_]*', suffix):
            raise ValueError('optional host token has an invalid input name')
        value = value.strip()
        if not value:
            continue
        if len(value) > 512 or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError('optional host token requires a bounded whitespace-free opaque value')
        result['KOMIZO_SECRET_' + suffix] = value
    return result


if __name__ == '__main__':
    try:
        values = selected(os.environ)
        with open(os.environ['GITHUB_ENV'], 'a') as output:
            for name, value in values.items():
                print('::add-mask::' + value)
                output.write(name + '=' + value + '\n')
    except (OSError, KeyError, ValueError) as error:
        raise SystemExit(str(error)) from error
