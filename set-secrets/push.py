"""Send a complete bounded secret set after authenticating its release source."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

MAX = 1 << 20


def secret_set(env):
    names = env.get('NAMES', '').split()
    names += sorted(key.removeprefix('KOMIZO_SECRET_') for key in env if key.startswith('KOMIZO_SECRET_'))
    names = list(dict.fromkeys(names))
    if not 1 <= len(names) <= 128:
        raise ValueError('provide a bounded set of KOMIZO_SECRET_<NAME> variables')
    values = {}
    for name in names:
        if (not re.fullmatch('[A-Za-z0-9_]{1,128}', name) or name.startswith(('GITHUB_', 'RUNNER_'))
                or name == 'KOMIZO_DEPLOY_KEY'):
            raise ValueError('reserved or invalid secret name')
        value = env.get('KOMIZO_SECRET_'+name, env.get(name, ''))
        if not value or any(character in value for character in '\r\n\x00'):
            raise ValueError('a required secret is empty or contains an unsupported newline')
        values[name] = value
    if len(json.dumps(values).encode()) > MAX:
        raise ValueError('secret set exceeds its transport bound')
    return values


def push(env, run=subprocess.run, envelope=None):
    command = env.get('SECRET_CMD', '')
    match = re.fullmatch('doas /usr/local/bin/set-secret-([a-z][a-z0-9_-]{0,62})', command)
    if not match:
        raise ValueError('command must name the fixed Komizo secret wrapper')
    values = secret_set(env)
    manifest = env.get('RELEASE_MANIFEST', '')
    if manifest:
        if envelope is None:
            spec = importlib.util.spec_from_file_location('release_proof', Path(__file__).resolve().parents[1]/'scripts/release-proof.py')
            proof = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(proof)
            envelope = proof.envelope
        source = env.get('GITHUB_SHA', '')
        actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True, timeout=10).strip()
        if source != actual:
            raise ValueError('checked-out source differs from release source')
        proof = envelope(manifest, source, env)
        frame = b'komizo-release/v1:'+base64.b64encode(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode())
        body = frame+b'\n'+json.dumps({'version': 1, 'secrets': values}, separators=(',', ':')).encode()
        commands = [('doas /usr/local/bin/set-release-secrets-'+match[1], body)]
    else:
        # Compatibility only. Protected hosts refuse this route once delivery
        # authorization is enabled; it cannot bypass the new boundary.
        commands = [(command+' '+name, value.encode()) for name, value in values.items()]
    for remote, body in commands:
        result = run(['ssh', '-T', '-o', 'BatchMode=yes', 'deploy-target', remote],
                     input=body, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode:
            raise ValueError('host refused the secret operation; inspect protected delivery evidence')
    return len(values)


def main():
    count = push(os.environ)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'count={count}\n')
    print(f'Pushed {count} secret(s).')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError):
        # Values, identity tokens and subprocess output never reach diagnostics.
        print('::error::Secret delivery refused; verify names, release identity and host authorization.', file=sys.stderr)
        raise SystemExit(1)
