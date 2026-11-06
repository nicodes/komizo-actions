"""Real prepared SDK declarations retain publication authority and closed output."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('canonical_publication_controls', ROOT / 'scripts/application-publication.py')
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)
SDK_REF = '14dd4792df910b8f72ae10bf01c7e426b97b667e'


def literal(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(key) + '=' + literal(item) for key, item in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(literal(item) for item in value) + ']'
    return json.dumps(value)


class CanonicalRecipeData(unittest.TestCase):
    def setUp(self):
        self.sdk = Path(os.environ['RECIPE_TEST_SDK']).resolve()
        self.assertTrue((self.sdk / 'cookbook/configuration.py').is_file(), 'prepare the real accepted SDK before tests')
        actual = subprocess.check_output(['git', '-C', str(self.sdk), 'rev-parse', 'HEAD'], text=True).strip()
        self.assertEqual(actual, SDK_REF)
        directory = tempfile.TemporaryDirectory(prefix='canonical recipe ')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ['one', 'two', 'three']:
            path = self.root / 'tests' / (name + '.py')
            path.parent.mkdir(exist_ok=True)
            path.write_text('raise RuntimeError("source programs must not execute during declaration admission")\n')
            path = self.root / 'app/tests' / (name + '.gd')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('extends Node\n')

    def write(self, config):
        text = '\n'.join(key + '=' + literal(value) for key, value in config.items()) + '\n'
        (self.root / 'cookbook.toml').write_text(text)
        return text

    def native(self, recipe='react-native'):
        return {'version': 1, 'recipe': recipe, 'tests': {'unit': [
            {'runner': 'python', 'timeout': 7, 'path': 'tests/' + name + '.py', 'args': ['literal;argument']}
            for name in ['one', 'two', 'three']]}}

    def read(self, sdk=None, *, failure=False, matrix=False):
        # Each control has a fresh module cache, so an older SDK imported by
        # another existing test cannot supply the new SDK's decoder accidentally.
        program = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import recipe_data
root,sdk=map(Path,sys.argv[2:4])
config=recipe_data.load(root,sdk)
import configuration
result={'config':config,'configuration_origin':str(Path(configuration.__file__).resolve())}
if sys.argv[4]=='matrix':
 import godot_ci
 result['matrix']=godot_ci.plan(None,config)
print(json.dumps(result))
'''
        result = subprocess.run([sys.executable, '-I', '-c', program, str(ROOT / 'scripts'), str(self.root),
                                 str(sdk or self.sdk), 'matrix' if matrix else 'config'],
                                capture_output=True, text=True, timeout=30)
        if failure:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            return result
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(Path(data['configuration_origin']).is_relative_to(Path(sdk or self.sdk).resolve() / 'cookbook'))
        return data

    def acceptance(self, config):
        environment = dict(GITHUB_SERVER_URL='https://github.com', GITHUB_REPOSITORY='owner/demo',
                           GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2', GITHUB_SHA='a' * 40,
                           GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/cd.yml@refs/heads/main',
                           GITHUB_WORKFLOW_SHA='a' * 40, GITHUB_EVENT_NAME='push')
        proof = dict(version=1, source_commit='a' * 40, source_tree='b' * 40, cookbook_commit=SDK_REF,
                     recipe=config['recipe'], status='passed', tracked_source_clean=True,
                     build_verified=True, tests_verified=True, test_groups=['lint', 'unit', 'security'],
                     execution_context=environment | {'GITHUB_JOB': 'validate'})
        jobs = {'total_count': 1, 'jobs': [{'name': 'validate / Full source checks / Make full',
                 'head_sha': 'a' * 40, 'conclusion': 'success', 'labels': ['ubuntu-24.04']}]}
        def accept():
            publication.accept(proof, config, 'a' * 40, SDK_REF, 'b' * 40,
                               ['lint', 'unit', 'security'], jobs, environment)
        return accept, proof, jobs

    def test_omitted_runner_counterfactual_and_actual_sdk_default_keep_host_admission(self):
        raw = self.native()
        raw['ci'] = {'timeout_minutes': 90}
        body = self.write(raw)
        accept, _, _ = self.acceptance(raw)
        with self.assertRaises(KeyError):
            accept()
        config = self.read()['config']
        self.assertEqual(config['ci'], {'runner': 'ubuntu-24.04', 'timeout_minutes': 90})
        self.assertEqual((self.root / 'cookbook.toml').read_text(), body)
        accept, proof, jobs = self.acceptance(config)
        accept()
        for key, bad in [('source_commit', 'c' * 40), ('source_tree', 'c' * 40),
                         ('cookbook_commit', 'c' * 40), ('tracked_source_clean', False),
                         ('test_groups', ['lint', 'unit'])]:
            old = proof[key]
            proof[key] = bad
            with self.subTest(key=key), self.assertRaises(ValueError):
                accept()
            proof[key] = old
        jobs['jobs'][0]['labels'] = ['ubuntu-22.04']
        with self.assertRaisesRegex(ValueError, 'another declared runner'):
            accept()

    def test_explicit_native_runner_and_deadlines_remain_exact(self):
        config = self.native()
        config['ci'] = {'runner': 'blacksmith-4vcpu-ubuntu-2204', 'timeout_minutes': 8}
        self.write(config)
        self.assertEqual(self.read()['config']['ci'], config['ci'])
        for bad in [False, None, '', 'ubuntu-24.04;echo']:
            config['ci']['runner'] = bad
            self.write(config)
            with self.subTest(runner=bad):
                self.read(failure=True)
        config['ci'] = {'runner': 'ubuntu-24.04', 'timeout_minutes': True}
        self.write(config)
        self.read(failure=True)

    def test_cases_keep_exact_native_ids_order_policy_and_complete_matrix(self):
        config = self.native('godot-game')
        shared = {'runner': 'godot-check', 'cwd': 'app', 'timeout': 180,
                  'policy': {'deferred_autoloads': True}}
        cases = [{'name': 'test-' + name, 'path': 'app/tests/' + name + '.gd'} for name in ['one', 'two', 'three']]
        config['tests']['unit'] = [{**shared, **case} for case in cases]
        config['ci'] = {'runner': 'ubuntu-24.04', 'timeout_minutes': 45,
                        'godot': {'unit_shards': 2, 'unit_workers': 4, 'build_timeout_minutes': 25,
                                  'unit_timeout_minutes': 20, 'host_timeout_minutes': 45}}
        self.write(config)
        before = self.read(matrix=True)
        config['tests']['unit'] = [{**shared, 'cases': cases}]
        body = self.write(config)
        after = self.read(matrix=True)
        self.assertEqual(after, before)
        self.assertEqual(after['config']['ci']['godot'], config['ci']['godot'])
        self.assertEqual((self.root / 'cookbook.toml').read_text(), body)
        self.assertEqual(after['matrix']['written_case_inventory'], before['matrix']['written_case_inventory'])
        config['tests']['unit'][0]['cases'][0]['timeout'] = 180
        self.write(config)
        self.read(failure=True)

    def test_dependency_security_source_policy_survives_ci_default_merge(self):
        config = self.native()
        policy = {'runner': 'ubuntu-24.04', 'timeout_minutes': 10, 'setup_timeout_minutes': 5}
        config['ci'] = {'dependency_security': policy}
        self.write(config)
        actual = self.read()['config']['ci']
        self.assertEqual(actual['dependency_security'], policy)
        self.assertEqual((actual['runner'], actual['timeout_minutes']), ('ubuntu-24.04', 30))

    def test_existing_prepared_sdk_retains_flat_declared_contract(self):
        legacy = Path(os.environ['TOOLS_ROOT']).resolve()
        config = self.native()
        config['ci'] = {'runner': 'ubuntu-22.04', 'timeout_minutes': 40}
        body = self.write(config)
        result = self.read(legacy)['config']
        self.assertEqual(result, config)
        self.assertEqual((self.root / 'cookbook.toml').read_text(), body)

    def static(self):
        config = self.native('godot-game')
        config.update(delivery={'definition': 'deploy/delivery.json'}, artifacts={'definition': 'deploy/release.json'})
        delivery = {'mode': 'godot-static', 'komizo_ref': 'c' * 40, 'project': 'game', 'static': {
            'config': 'deploy/config.yml', 'compose': 'deploy/compose.yml', 'release_manifest': 'public/release.json',
            'record': False, 'recovery': False, 'production': False}}
        for name, body in {'deploy/delivery.json': json.dumps(delivery),
                           'deploy/release.json': json.dumps({'project': 'game', 'image_base': 'ghcr.io/example/game'}),
                           'deploy/config.yml': 'compose: compose.yml\n', 'deploy/compose.yml': 'services: {}\n'}.items():
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(body)
        return config

    def test_static_capability_owns_mandatory_metadata_and_closed_hash_bound_stage(self):
        raw = self.static()
        body = self.write(raw)
        config = self.read()['config']
        metadata = config['artifacts']['release_metadata']
        self.assertEqual(metadata, ['.artifacts/release/static-config.json', '.artifacts/release/godot-release-manifest.json'])
        self.assertNotIn('release_metadata', tomllib.loads(body)['artifacts'])
        raw['artifacts']['release_metadata'] = metadata
        self.write(raw)
        self.assertEqual(self.read()['config'], config, 'legacy explicit static metadata remains compatible')
        source = self.root / 'incoming/.artifacts/release'
        source.mkdir(parents=True)
        names = ['images.tar.gz', 'images.tar.gz.sha256', 'images.json', 'release.json',
                 *[Path(name).name for name in metadata]]
        for name in names:
            (source / name).write_bytes(b'{}')
        proof = {'artifacts': {'.artifacts/release/' + name: hashlib.sha256(b'{}').hexdigest() for name in names}}
        publication.stage(self.root / 'incoming', proof, self.root / 'accepted', metadata)
        self.assertEqual({path.name for path in (self.root / 'accepted').iterdir()}, set(names))
        (source / 'static-config.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, 'bytes differ'):
            publication.stage(self.root / 'incoming', proof, self.root / 'changed', metadata)
        (source / 'static-config.json').unlink()
        with self.assertRaisesRegex(ValueError, 'closed validated'):
            publication.stage(self.root / 'incoming', proof, self.root / 'missing', metadata)
        self.assertFalse((self.root / 'changed').exists())
        self.assertFalse((self.root / 'missing').exists())

    def test_nonstatic_game_keeps_explicit_metadata_without_acquiring_static_outputs(self):
        config = self.native('godot-game')
        config['artifacts'] = {'release_metadata': ['.artifacts/release/components.json']}
        self.write(config)
        result = self.read()['config']
        self.assertEqual(result['artifacts'], config['artifacts'])

    def test_foreign_cached_configuration_or_static_module_refuses_the_other_sdk(self):
        self.write(self.static())
        legacy = Path(os.environ['TOOLS_ROOT']).resolve()
        self.assertNotEqual(legacy, self.sdk, 'prepare distinct old/new SDK sources for the cache-origin control')
        program = '''import importlib,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import recipe_data
root,sdk,other=map(Path,sys.argv[2:5])
name=sys.argv[5]
if name=='godot_static':
 sys.path.insert(0,str(sdk/'cookbook'))
 import configuration
sys.path.insert(0,str(other/'cookbook'))
importlib.import_module(name)
recipe_data.load(root,sdk)
'''
        for name in ['configuration', 'godot_static']:
            result = subprocess.run([sys.executable, '-I', '-c', program, str(ROOT / 'scripts'), str(self.root),
                                     str(self.sdk), str(legacy), name], capture_output=True, text=True, timeout=30)
            with self.subTest(name=name):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('recipe reader loaded another cookbook SDK: ' + name, result.stderr)


if __name__ == '__main__':
    unittest.main()
