"""The action token reaches source admission, never a live product command."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('production_token_control', ROOT / 'godot-production/production.py')
production = importlib.util.module_from_spec(spec)
spec.loader.exec_module(production)
COOKIE = 'a' * 40
TOOLS = 'b' * 40
PATHS = ['repos/nicodes/' + repository + '/compare/' + revision + '...main'
         for repository, revision in [('cookbook', COOKIE), ('tools', TOOLS)]]


class SourceBoundaryReached(RuntimeError):
    pass


class ProductionTokenContext(unittest.TestCase):
    def control(self, operation, token=None):
        action = yaml.safe_load((ROOT / 'godot-production/action.yml').read_text())
        steps = [step for step in action['runs']['steps']
                 if '"$GITHUB_ACTION_PATH/production.py" ' + operation + ' ' in step.get('run', '')]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]['env'].get('GH_TOKEN'), '${{ github.token }}')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests = root / 'requests.jsonl'
            gh = root / 'gh'
            gh.write_text('#!' + sys.executable + '\n'
                'import json, os, sys\n'
                'assert sys.argv[1:2] == ["api"] and sys.argv[2] in ' + repr(PATHS) + '\n'
                'present = bool(os.environ.get("GH_TOKEN"))\n'
                'with open(os.environ["UNIT_REQUESTS"], "a") as stream:\n'
                ' stream.write(json.dumps({"path": sys.argv[2], "token_present": present}) + "\\n")\n'
                'if not present: raise SystemExit(4)\n'
                'print(json.dumps({"status": "identical"}))\n')
            gh.chmod(0o700)
            environment = {'PATH': str(root) + os.pathsep + os.environ['PATH'],
                           'UNIT_REQUESTS': str(requests)}
            if token is None:
                environment['GH_TOKEN'] = 'unit-fixture-only'
            elif token == 'empty':
                environment['GH_TOKEN'] = ''
            else:
                self.assertEqual(token, 'absent')
            cookbook, tools = root / 'cookbook', root / 'tools'

            def boundary(selected, revision, lookup, supplied_tools):
                self.assertEqual((selected, revision, supplied_tools), (cookbook, COOKIE, tools))
                # Exercise the real data-only two-source lookup before any SDK
                # import. The literal helper declaration is an inert unit fixture.
                production.recipe_context.accepted_dependency(selected, revision, lookup)
                raise SourceBoundaryReached()

            arguments = ['production.py', operation, '--cookbook-root', str(cookbook),
                         '--cookbook-ref', COOKIE, '--tools-root', str(tools),
                         '--source', 'c' * 40, '--release', 'd' * 64,
                         '--runner', 'blacksmith-4vcpu-ubuntu-2204', '--timeout', '15']
            with patch.dict(os.environ, environment, clear=True), patch.object(sys, 'argv', arguments), \
                 patch.object(production.recipe_context, 'helper_reference',
                              return_value={'repository': 'nicodes/tools', 'revision': TOOLS}), \
                 patch.object(production.recipe_context, 'accepted_context', side_effect=boundary), \
                 contextlib.redirect_stderr(io.StringIO()):
                if token is None:
                    with self.assertRaises(SourceBoundaryReached):
                        production.main()
                else:
                    with self.assertRaises(SystemExit) as refused:
                        production.main()
                    self.assertEqual(refused.exception.code, 1)
            rows = [json.loads(line) for line in requests.read_text().splitlines()]
            self.assertEqual([row['path'] for row in rows], PATHS if token is None else PATHS[:1])
            self.assertTrue(all(row['token_present'] is (token is None) for row in rows))

    def test_setup_and_verify_forward_the_action_token_through_real_source_lookups(self):
        for operation in ['setup', 'verify']:
            with self.subTest(operation=operation):
                self.control(operation)

    def test_missing_or_empty_token_refuses_before_sdk_or_native_execution(self):
        for operation in ['setup', 'verify']:
            for token in ['absent', 'empty']:
                with self.subTest(operation=operation, token=token):
                    self.control(operation, token)


if __name__ == '__main__':
    unittest.main()
