"""Historical deployment trusts real Git identities and never manufactures acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(os.environ['TOOLS_ROOT']) / 'cookbook'))
import configuration as sdk_configuration
spec = importlib.util.spec_from_file_location('historical_delivery', ROOT/'scripts/historical_delivery.py')
historical = importlib.util.module_from_spec(spec)
spec.loader.exec_module(historical)
LIMITS = dict(checkout=3, setup=5, plan=3, registry=2, inspect=5, deploy=30, diagnostics=2)


class HistoricalDelivery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root, self.tools, self.actions = [self.directory/name for name in ['app', 'tools', 'actions']]
        for directory, name in [(self.tools, 'tools'), (self.actions, 'komizo-actions'), (self.root, 'application')]:
            directory.mkdir()
            self.git(directory, 'init', '-q', '--initial-branch=main')
            self.git(directory, 'config', 'user.name', 'Fixture')
            self.git(directory, 'config', 'user.email', 'fixture@example.invalid')
            self.git(directory, 'remote', 'add', 'origin', 'https://github.com/nicodes/'+name+'.git')
            (directory/'product.py').write_text('raise RuntimeError("product code must never execute")\n')
            self.commit(directory)
        self.target = self.git(self.root, 'rev-parse', 'HEAD')
        self.pins = [self.git(directory, 'rev-parse', 'HEAD') for directory in [self.tools, self.actions]]
        self.canonical_cd = 'canonical trusted-main deployment '+self.pins[0]+'\n'
        self.canonical_make = 'COOKBOOK_REF := '+self.pins[0]+'\n'
        self.data = dict(version=1, project='application', komizo_ref=self.pins[1], historical_deploy=True,
                         deploy_runner='blacksmith-4vcpu-ubuntu-2404', deploy_timeout=30,
                         environment='production', service_env_profile='database-v2', expected_generation='a'*32,
                         health_urls=[{'url': 'https://app.example/health', 'contains': '{revision}'}], diagnostics=True)
        (self.root/'.github/workflows').mkdir(parents=True)
        (self.root/'deploy').mkdir()
        (self.root/'cookbook.toml').write_text('version=1\nrecipe="godot-game"\n[delivery]\ndefinition="deploy/delivery.json"\n[artifacts]\ndefinition="deploy/release.json"\n')
        (self.root/'deploy/delivery.json').write_text(json.dumps(self.data))
        (self.root/'deploy/release.json').write_text(json.dumps({'version': 1, 'project': 'application',
            'image_base': 'ghcr.io/example/application', 'inputs': {'images': [
                'ghcr.io/example/application-api:{revision}', 'ghcr.io/example/application-config:{revision}']}}))
        (self.root/'.github/workflows/cd.yml').write_text(self.canonical_cd)
        (self.root/'Makefile').write_text(self.canonical_make)
        self.commit(self.root)
        self.event = self.directory/'event.json'
        self.event.write_text(json.dumps({'inputs': {'version': self.target}}))
        self.env = dict(os.environ, GITHUB_REPOSITORY='example/application', GITHUB_REF='refs/heads/main',
                        GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1',
                        GITHUB_WORKFLOW_REF='example/application/.github/workflows/cd.yml@refs/heads/main',
                        GITHUB_EVENT_PATH=str(self.event), HISTORICAL_VERSION=self.target, TOOLS_REF=self.pins[0],
                        KOMIZO_REF=self.pins[1], DECLARATION='deploy/delivery.json',
                        HISTORICAL_RUNNER=self.data['deploy_runner'], HISTORICAL_TIMEOUT='30',
                        HISTORICAL_ENVIRONMENT='production', HISTORICAL_STEP_TIMEOUTS=json.dumps(LIMITS))
        self.refresh_source()
        workflow = SimpleNamespace(declaration=lambda root, config: json.loads((root/'deploy/delivery.json').read_text()),
                                   historical_deadlines=lambda data: LIMITS)
        generator = SimpleNamespace(workflow_files=lambda root, pin: {'.github/workflows/cd.yml': self.canonical_cd},
                                    makefile=lambda root, pin: self.canonical_make)
        decoder = SimpleNamespace(load=sdk_configuration.load, ci_settings=sdk_configuration.ci_settings,
                                  __file__=str(self.tools/'cookbook/configuration.py'))
        self.modules = patch.object(historical, 'tools_modules', return_value=(workflow, generator, decoder)).start()
        self.addCleanup(patch.stopall)

    def git(self, directory, *arguments):
        return subprocess.check_output(['git', '-C', str(directory), *arguments], text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, directory):
        self.git(directory, 'add', '.')
        self.git(directory, 'commit', '-qm', 'Native fixture source')

    def refresh_source(self):
        revision = self.git(self.root, 'rev-parse', 'HEAD')
        self.env.update(GITHUB_SHA=revision, GITHUB_WORKFLOW_SHA=revision)

    def lookup(self, path):
        if '/git/ref/' in path:
            return json.dumps({'object': {'sha': self.env['GITHUB_SHA']}})
        selected = path.split('/compare/')[1].split('...')[0]
        directory = self.tools if '/nicodes/tools/' in path else self.actions if '/nicodes/komizo-actions/' in path else self.root
        self.git(directory, 'merge-base', '--is-ancestor', selected, 'HEAD')
        return json.dumps({'status': 'ahead', 'base_commit': {'sha': selected}, 'merge_base_commit': {'sha': selected}})

    def plan(self, env=None, lookup=None):
        return historical.authority(self.root, self.tools, self.actions, env or self.env, lookup or self.lookup)

    def test_manual_target_binds_health_to_old_release_without_claiming_current_source_acceptance(self):
        plan = self.plan()
        self.assertEqual(plan['version'], self.target)
        self.assertNotEqual(plan['source_commit'], self.target)
        self.assertEqual(plan['health-urls'], 'https://app.example/health '+self.target)
        self.assertEqual(plan['kind'], 'historical-application-release')
        self.assertIs(plan['full_source_acceptance'], False)
        self.assertEqual(plan['expected-generation'], 'a'*32)

    def test_pr_push_wrong_source_and_nonliteral_versions_refuse_before_sdk_or_registry(self):
        for key, value in [('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_EVENT_NAME', 'push'),
                           ('GITHUB_REF', 'refs/pull/1/merge'), ('GITHUB_WORKFLOW_SHA', 'b'*40),
                           ('GITHUB_WORKFLOW_REF', 'example/application/.github/workflows/other.yml@refs/heads/main'),
                           ('HISTORICAL_VERSION', 'main'), ('HISTORICAL_VERSION', 'A'*40),
                           ('HISTORICAL_VERSION', self.env['GITHUB_SHA']), ('GITHUB_RUN_ATTEMPT', '0')]:
            self.modules.reset_mock()
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.plan(self.env | {key: value})
            self.modules.assert_not_called()

    def test_dispatch_inputs_and_current_main_are_not_overridable(self):
        for inputs in [{'version': 'f'*40}, {'version': self.target, 'source': 'f'*40}]:
            self.event.write_text(json.dumps({'inputs': inputs}))
            with self.assertRaises(ValueError):
                self.plan()
        self.event.write_text(json.dumps({'inputs': {'version': self.target}}))
        with self.assertRaisesRegex(ValueError, 'no longer current main'):
            self.plan(lookup=lambda path: json.dumps({'object': {'sha': 'f'*40}}) if '/git/ref/' in path else self.lookup(path))
        with self.assertRaisesRegex(ValueError, 'accepted main ancestors'):
            self.plan(lookup=lambda path: json.dumps({'status': 'diverged'}) if '/compare/' in path else self.lookup(path))

    def test_fixed_profile_generation_capability_and_secret_mapping_cannot_be_relaxed(self):
        for change in [{'historical_deploy': False}, {'mode': 'godot-static'}, {'service_env_profile': ''},
                       {'expected_generation': 'b'*31}, {'secrets': {'KOMIZO_SECRET_TOKEN': 'TOKEN'}},
                       {'optional_secrets': {'KOMIZO_SECRET_TOKEN': 'TOKEN'}}]:
            (self.root/'deploy/delivery.json').write_text(json.dumps(self.data | change))
            self.commit(self.root); self.refresh_source()
            registry = Mock()
            with self.subTest(change=change), self.assertRaises(ValueError):
                historical.inspect_images(self.plan(), self.root, self.env, registry)
            registry.assert_not_called()

    def test_native_runner_environment_and_each_deadline_remain_bound(self):
        for key, value in [('HISTORICAL_RUNNER', 'ubuntu-latest'), ('HISTORICAL_TIMEOUT', '31'),
                           ('HISTORICAL_ENVIRONMENT', 'unprotected'), ('HISTORICAL_STEP_TIMEOUTS', '{}'),
                           ('KOMIZO_SCOPED_PASSWORD', 'never-transport')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.plan(self.env | {key: value})

    def test_poisoned_sdk_checkout_is_refused_before_importing_its_modules(self):
        for directory in [self.tools, self.actions]:
            for name in ['product.py', 'untracked.py']:
                original = (directory/name).read_bytes() if (directory/name).exists() else None
                (directory/name).write_text('raise RuntimeError("SDK poisoning")\n')
                self.modules.reset_mock()
                with self.subTest(directory=directory, name=name), self.assertRaises(ValueError):
                    self.plan()
                self.modules.assert_not_called()
                if original is None:
                    (directory/name).unlink()
                else:
                    (directory/name).write_bytes(original)
        self.git(self.tools, 'remote', 'set-url', 'origin', 'https://example.invalid/tools.git')
        with self.assertRaises(ValueError):
            self.plan()

    def test_ignored_sdk_poison_wrong_pin_and_oversized_metadata_are_refused(self):
        (self.tools/'.gitignore').write_text('cache/\n')
        self.commit(self.tools)
        self.env['TOOLS_REF'] = self.git(self.tools, 'rev-parse', 'HEAD')
        (self.tools/'cache').mkdir()
        (self.tools/'cache/poison.py').write_text('raise RuntimeError("ignored poison")\n')
        self.modules.reset_mock()
        with self.assertRaises(ValueError):
            self.plan()
        self.modules.assert_not_called()
        with self.assertRaises(ValueError):
            self.plan(self.env | {'TOOLS_REF': 'f'*40})
        with self.assertRaisesRegex(ValueError, 'exceeds its bound'):
            historical.document('x'*(historical.MAX_BYTES+1))

    def test_git_environment_poison_is_ignored_and_tracked_source_changes_refuse(self):
        self.assertEqual(self.plan(self.env | {'GIT_DIR': '/nonexistent', 'GIT_CONFIG_COUNT': '1',
                                              'GIT_CONFIG_KEY_0': 'core.fsmonitor', 'GIT_CONFIG_VALUE_0': 'foreign-command'})['version'], self.target)
        (self.root/'product.py').write_text('changed\n')
        with self.assertRaisesRegex(ValueError, 'exact and clean'):
            self.plan()

    def test_changed_committed_caller_and_foreign_image_family_refuse(self):
        (self.root/'.github/workflows/cd.yml').write_text('noncanonical controller\n')
        self.commit(self.root); self.refresh_source()
        with self.assertRaisesRegex(ValueError, 'canonical'):
            self.plan()
        (self.root/'.github/workflows/cd.yml').write_text(self.canonical_cd)
        definition = json.loads((self.root/'deploy/release.json').read_text())
        definition['inputs']['images'].append('ghcr.io/foreign/application-api:{revision}')
        (self.root/'deploy/release.json').write_text(json.dumps(definition))
        self.commit(self.root); self.refresh_source()
        with self.assertRaisesRegex(ValueError, 'closed declared GHCR'):
            self.plan()

    def test_all_preexisting_components_must_be_available_and_no_build_or_publish_is_called(self):
        plan = self.plan()
        manifest = json.dumps({'schemaVersion': 2, 'config': {'digest': 'sha256:'+'b'*64}})
        inspect = Mock(return_value=manifest)
        receipt = historical.inspect_images(plan, self.root, self.env, inspect)
        self.assertEqual([call.args[0] for call in inspect.call_args_list], plan['images'])
        self.assertEqual(len(receipt['published_images_inspected']), 2)
        self.assertIs(receipt['full_source_acceptance'], False)
        for response in ['{}', '{"schemaVersion":1}', '{"schemaVersion":2,"config":{"digest":"mutable"}}',
                         '{"schemaVersion":2,"manifests":[],"config":{"digest":"sha256:'+'b'*64+'"}}']:
            with self.subTest(response=response), self.assertRaises(ValueError):
                historical.inspect_images(plan, self.root, self.env, lambda _: response)
        with self.assertRaisesRegex(ValueError, 'registry failure'):
            historical.inspect_images(plan, self.root, self.env, Mock(side_effect=ValueError('registry failure')))

    def test_leaf_uses_readonly_permissions_and_no_host_credentials_before_published_image_inspection(self):
        leaf = yaml.safe_load((ROOT/'.github/workflows/cookbook-historical-deploy.yml').read_text())
        job = leaf['jobs']['historical']
        self.assertEqual(job['permissions'], {key: 'read' for key in ['contents', 'actions', 'packages', 'attestations']})
        self.assertNotIn('KOMIZO_DEPLOY_KEY', job['env'])
        steps = job['steps']; inspect_index = next(index for index, step in enumerate(steps) if step.get('id') == 'inspect')
        deploy_index = next(index for index, step in enumerate(steps) if step.get('id') == 'deploy')
        self.assertLess(inspect_index, deploy_index)
        self.assertEqual(steps[deploy_index]['with']['release-manifest'], '')
        self.assertEqual(steps[deploy_index]['uses'], './_historical_actions/deploy')
        self.assertTrue(all('KOMIZO_DEPLOY_KEY' not in step.get('env', {}) for step in steps[:deploy_index]))
        self.assertFalse(any('make build' in step.get('run', '') or 'make test' in step.get('run', '') for step in steps))
        self.assertTrue(all('timeout-minutes' in step for step in steps))


if __name__ == '__main__':
    unittest.main()
