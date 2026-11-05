"""Exercise source checkout and admission without publication or network access."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.safe_load((ROOT / 'application-publication/action.yml').read_text())
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/cookbook-publish.yml').read_text())


class ApplicationCheckout(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='publication checkout ')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'product'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Checkout controls')
        self.git('config', 'user.email', 'checkout@example.invalid')
        self.git('remote', 'add', 'origin', 'https://github.com/owner/example.git')
        (self.root / 'native.txt').write_text('native source\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'source fixture')
        self.source = self.git('rev-parse', 'HEAD')
        binaries = self.base / 'bin'
        binaries.mkdir()
        fake = binaries / 'gh'
        fake.write_text('''#!/usr/bin/env python3
import os,sys
args=sys.argv[1:]
if args == ['api','repos/owner/example/branches/main','--jq','.commit.sha']:
    print(os.environ['FIXTURE_MAIN'])
elif len(args)==4 and args[0]=='api' and args[1].startswith('repos/nicodes/') and args[2:]==['--jq','.status']:
    print('identical')
else:
    raise SystemExit('unexpected metadata request')
''')
        fake.chmod(0o700)
        self.environment = dict(os.environ, PATH=str(binaries)+os.pathsep+os.environ['PATH'],
            GITHUB_WORKSPACE=str(self.root), GITHUB_REPOSITORY='owner/example',
            GITHUB_SERVER_URL='https://github.com', GITHUB_SHA=self.source,
            GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='push', SOURCE=self.source,
            SOURCE_PREPARED='true', TOOLS_REF='b'*40, KOMIZO_REF='c'*40,
            PROJECT='example', FIXTURE_MAIN=self.source)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def guard(self, environment=None):
        for step in ACTION['runs']['steps']:
            if step.get('name') not in {'Require accepted immutable automation before publication',
                                         'Verify prepared product source before publication'}:
                continue
            result = subprocess.run(['bash', '-c', step['run']], cwd=self.root,
                env=environment or self.environment, capture_output=True, text=True, timeout=10)
            if result.returncode:
                return result
        return result

    def source_checkout(self, step):
        # checkout's default clean operation removes untracked action files.
        self.assertNotEqual(step.get('with', {}).get('clean'), False)
        self.assertEqual(step['with']['ref'], '${{ inputs.source }}')
        self.assertEqual(step['with']['fetch-depth'], 0)
        self.git('clean', '-ffdx')
        self.git('reset', '--hard', self.source)

    def test_reusable_checkout_order_keeps_local_action_available_through_finalization(self):
        steps = WORKFLOW['jobs']['publish']['steps']
        self.assertTrue(steps[0]['uses'].startswith('actions/checkout@'))
        self.source_checkout(steps[0])
        self.assertEqual(steps[1]['with']['repository'], 'nicodes/komizo-actions')
        action = self.root / steps[1]['with']['path'] / 'application-publication/action.yml'
        action.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / 'application-publication/action.yml', action)
        original = action.read_bytes()
        self.assertEqual(steps[2]['uses'], './'+steps[1]['with']['path']+'/application-publication')
        self.assertEqual(steps[2]['with']['source-prepared'], 'true')
        checkout = [step for step in ACTION['runs']['steps']
                    if step.get('uses', '').startswith('actions/checkout@') and not step.get('with', {}).get('repository')]
        self.assertEqual(len(checkout), 1)
        self.assertEqual(checkout[0]['if'], "inputs.source-prepared != 'true'")
        self.assertEqual(self.guard().returncode, 0)
        self.assertEqual(action.read_bytes(), original)
        # The former order deleted this real action file during the inner checkout.
        self.source_checkout(checkout[0])
        self.assertFalse(action.exists())

    def test_standalone_default_still_performs_a_clean_exact_source_checkout(self):
        self.assertEqual(ACTION['inputs']['source-prepared']['default'], 'false')
        self.assertFalse(ACTION['inputs']['source-prepared']['required'])
        (self.root / 'untracked.txt').write_text('must be removed\n')
        (self.root / 'native.txt').write_text('unaccepted edit\n')
        checkout = next(step for step in ACTION['runs']['steps']
            if step.get('uses', '').startswith('actions/checkout@') and not step.get('with', {}).get('repository'))
        self.source_checkout(checkout)
        self.assertFalse((self.root / 'untracked.txt').exists())
        self.assertEqual((self.root / 'native.txt').read_text(), 'native source\n')
        self.assertEqual(self.guard(self.environment | {'SOURCE_PREPARED': 'false'}).returncode, 0)

    def test_prepared_source_refuses_dirty_wrong_checkout_repository_main_or_context(self):
        self.assertEqual(self.guard().returncode, 0)
        for updates in [{'SOURCE_PREPARED': 'maybe'}, {'FIXTURE_MAIN': 'd'*40},
                        {'GITHUB_REPOSITORY': 'owner/other'}, {'GITHUB_SHA': 'd'*40},
                        {'GITHUB_REF': 'refs/pull/1/merge'}, {'GITHUB_EVENT_NAME': 'pull_request_target'}, {'GITHUB_WORKSPACE': str(self.base)}]:
            with self.subTest(updates=updates):
                self.assertNotEqual(self.guard(self.environment | updates).returncode, 0)
        (self.root / 'native.txt').write_text('unaccepted source edit\n')
        self.assertNotEqual(self.guard().returncode, 0)
        self.git('checkout', '--', 'native.txt')
        self.git('remote', 'set-url', 'origin', 'https://github.com/other/example.git')
        self.assertNotEqual(self.guard().returncode, 0)
        self.git('remote', 'set-url', 'origin', 'https://github.com/owner/example.git')
        (self.root / 'other.txt').write_text('different source\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'another source')
        self.assertNotEqual(self.guard().returncode, 0)


if __name__ == '__main__':
    unittest.main()
