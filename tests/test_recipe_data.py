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
SDK_REF = os.environ['RECIPE_TASK_SDK_REF']


def literal(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(key) + '=' + literal(item) for key, item in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(literal(item) for item in value) + ']'
    return json.dumps(value)


class CanonicalRecipeData(unittest.TestCase):
    def setUp(self):
        self.sdk = Path(os.environ['RECIPE_TASK_SDK']).resolve()
        self.tools = Path(os.environ['RECIPE_TASK_TOOLS']).resolve()
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
import recipe_data,recipe_context
root,sdk,current,helpers=map(Path,sys.argv[2:6])
recipe_context.Context(current,sys.argv[6],helpers).activate()
config=recipe_data.load(root,sdk)
import configuration
result={'config':config,'configuration_origin':str(Path(configuration.__file__).resolve())}
if sys.argv[7]=='matrix':
 import godot_ci
 result['matrix']=godot_ci.plan(None,config)
print(json.dumps(result))
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(ROOT / 'scripts'), str(self.root),
                                 str(sdk or self.sdk),str(self.sdk),str(self.tools),SDK_REF,'matrix' if matrix else 'config'],
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

    def test_obsolete_sdks_refuse_without_running_source_or_changing_declarations(self):
        body=self.write(self.native())
        for name in ['RECIPE_TEST_SDK','RECIPE_LEGACY_TEST_SDK']:
            with self.subTest(sdk=name):
                selected=Path(os.environ[name]).resolve()
                expected={'RECIPE_TEST_SDK':'14dd4792df910b8f72ae10bf01c7e426b97b667e','RECIPE_LEGACY_TEST_SDK':'d475b54c402754a5a37a44fbd65328551995025c'}[name]
                self.assertEqual(subprocess.check_output(['git','-C',str(selected),'rev-parse','HEAD'],text=True).strip(),expected)
                result=self.read(selected,failure=True)
                self.assertIn('native TOML',result.stderr)
                self.assertEqual((self.root/'cookbook.toml').read_text(),body)

    def static(self):
        config=self.native('godot-game')
        config.update(delivery={'definition':'deploy/delivery.toml'})
        (self.root/'deploy').mkdir(exist_ok=True)
        (self.root/'deploy/delivery.toml').write_text('version=1\nproject="game"\n')
        return config

    def test_native_static_declaration_has_no_implicit_config_image_metadata(self):
        raw=self.static();body=self.write(raw)
        before={path.relative_to(self.root).as_posix():path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        config=self.read()['config']
        self.assertNotIn('release_metadata',config.get('artifacts',{}))
        self.assertEqual((self.root/'cookbook.toml').read_text(),body)
        self.assertEqual(before,{path.relative_to(self.root).as_posix():path.read_bytes() for path in self.root.rglob('*') if path.is_file()})
        raw['artifacts']={'definition':'deploy/release.json'}
        self.write(raw);self.read(failure=True)
        # The closed publisher admits only exact native packet bytes/proof.
        from test_release_packet import make_packet
        incoming=self.root/'incoming';source=incoming/'.artifacts/release';source.mkdir(parents=True)
        make_packet(source)
        names=['release.json','inventory.json','static/web.tar.gz']
        proof={'artifacts':{'.artifacts/release/'+name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in names}}
        original={name:(source/name).read_bytes() for name in names}
        staged=publication.stage(incoming,proof,self.root/'admitted')
        self.assertEqual(original,{name:(staged.root/name).read_bytes() for name in names})
        (source/'config-image.tar').write_bytes(b'retired configuration transport')
        with self.assertRaises((ValueError,OSError)):
            publication.stage(incoming,proof,self.root/'refused')
        self.assertFalse((self.root/'refused').exists())

    def test_nonstatic_game_keeps_explicit_metadata_without_acquiring_static_outputs(self):
        config = self.native('godot-game')
        config['artifacts'] = {'release_metadata': ['.artifacts/release/metadata/components.json']}
        self.write(config)
        result = self.read()['config']
        self.assertEqual(result['artifacts'], config['artifacts'])

    def test_foreign_cached_configuration_refuses_before_native_declaration_admission(self):
        self.write(self.static())
        legacy=Path(os.environ['RECIPE_TEST_SDK']).resolve()
        self.assertNotEqual(legacy,self.sdk)
        program='''import importlib,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import recipe_data,recipe_context
root,sdk,other,helpers=map(Path,sys.argv[2:6])
recipe_context.Context(sdk,sys.argv[6],helpers).activate()
sys.path.insert(0,str(other/'cookbook'))
importlib.import_module('configuration')
recipe_data.load(root,sdk)
'''
        result=subprocess.run([sys.executable,'-I','-B','-c',program,str(ROOT/'scripts'),str(self.root),
            str(self.sdk),str(legacy),str(self.tools),SDK_REF],capture_output=True,text=True,timeout=30)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('recipe reader loaded another cookbook SDK: configuration',result.stderr)


if __name__ == '__main__':
    unittest.main()
