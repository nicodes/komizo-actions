import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('native_deployed_scan', Path(__file__).parents[1] / 'scripts/deployed_scan.py')
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class DeployedDependencies(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'deploy').mkdir()
        self.revision = 'a' * 40
        self.identity = 'sha256:' + 'b' * 64
        self.container = 'c' * 64
        self.lock = b'version = 4\n[[package]]\nname = "fixture"\nversion = "1.0.0"\n'
        self.blob_sha = hashlib.sha1(b'blob ' + str(len(self.lock)).encode() + b'\0' + self.lock).hexdigest()
        self.tree = {'api/Cargo.lock': dict(type='blob', sha=self.blob_sha)}
        self.blob = dict(encoding='base64', size=len(self.lock), content=base64.b64encode(self.lock).decode())
        self.contract = dict(source_lock='api/Cargo.lock', legacy_manifest='api/go.mod',
            lock_installed='/usr/share/demo/Cargo.lock', binaries=['/app/server', '/usr/local/bin/reconcile'],
            user='10001:10001', forbidden=['DEVELOPMENT_AUTHORITY'])
        self.data = dict(version=1, deployment='deploy/scan.json', rust={'api': self.contract},
                        go={'api': dict(forbidden=['DEVELOPMENT_AUTHORITY']), 'gate': dict(forbidden=[])})
        self.deployment = dict(repository='owner/demo', image_base='ghcr.io/owner/demo',
                               components=['api', 'gate'], environment='production')
        (self.root / 'deploy/scan.json').write_text(json.dumps(self.deployment))
        self.path = self.root / 'deploy/deployed-scan.json'
        self.reports = self.root / 'reports'
        self.reports.mkdir()
        self.info = dict(Id=self.identity, Config=dict(User=self.contract['user'],
            Labels={'org.opencontainers.image.revision': self.revision}))
        self.calls = []
        self.marker = b''
        self.native = b'\x7fELF'
        self.runtime_lock = self.lock
        self.findings = dict(SchemaVersion=2, Results=[dict(Type='cargo', Packages=[dict(Name='fixture')])])
        self.link_binary = False

    def write(self):
        self.path.write_text(json.dumps(self.data))

    def capture(self, arguments, environment, timeout=60):
        self.calls.append(arguments)
        if arguments[:3] == ['docker', 'image', 'inspect']:
            return json.dumps([self.info])
        if arguments[:2] == ['docker', 'create']:
            self.assertEqual(arguments[-1], self.identity)
            return self.container
        raise AssertionError('unexpected inspection command: ' + repr(arguments))

    def run_command(self, arguments, environment, timeout=60, **kwargs):
        self.calls.append(arguments)
        if arguments[:2] == ['docker', 'cp']:
            destination = Path(arguments[-1])
            if arguments[-2].endswith('/Cargo.lock'):
                destination.write_bytes(self.runtime_lock)
            elif self.link_binary:
                destination.symlink_to(self.root / 'outside')
            else:
                destination.write_bytes(self.native + self.marker)
        elif arguments[:2] == ['trivy', 'fs']:
            Path(arguments[arguments.index('--output') + 1]).write_text(json.dumps(self.findings))
        elif arguments[:2] != ['docker', 'rm']:
            raise AssertionError('unexpected scanner effect: ' + repr(arguments))

    def inspect(self):
        with patch.object(scanner, 'capture', side_effect=self.capture), patch.object(scanner, 'run', side_effect=self.run_command):
            return scanner.rust_image(self.identity, self.revision, self.lock, self.contract,
                                      self.reports, 'api', {})

    def test_exact_git_blob_bytes_and_explicit_historical_go_cutover(self):
        with patch.object(scanner, 'api', return_value=self.blob):
            self.assertEqual(scanner.source_lock('owner/demo', self.tree, self.contract, {}), self.lock)
        legacy = {'api/go.mod': dict(type='blob', sha='d' * 40)}
        self.assertIsNone(scanner.source_lock('owner/demo', legacy, self.contract, {}))
        with self.assertRaisesRegex(ValueError, 'unknown deployed implementation'):
            scanner.source_lock('owner/demo', {}, self.contract, {})

    def test_missing_truncated_malformed_and_wrong_blob_bytes_are_rejected(self):
        for changes in [dict(encoding='utf8'), dict(size=0), dict(size=2 * 1024 * 1024 + 1),
                        dict(size=len(self.lock) - 1), dict(content='invalid!'),
                        dict(content=base64.b64encode(b'x' * len(self.lock)).decode())]:
            with self.subTest(changes=changes), patch.object(scanner, 'api', return_value=self.blob | changes):
                with self.assertRaises(ValueError):
                    scanner.source_lock('owner/demo', self.tree, self.contract, {})
        for tree in [dict(truncated=True, tree=[]), dict(truncated=False, tree=None),
                     dict(truncated=False, tree=[dict(path='api/Cargo.lock'), dict(path='api/Cargo.lock')])]:
            with self.subTest(tree=tree), patch.object(scanner, 'api', return_value=tree):
                with self.assertRaises(ValueError):
                    scanner.source_tree('owner/demo', self.revision, {})

    def test_rust_runtime_inventory_retains_uid_revision_markers_and_include_dev(self):
        result = self.inspect()
        self.assertEqual(result['image_id'], self.identity)
        inventory = next(args for args in self.calls if args[:2] == ['trivy', 'fs'])
        self.assertIn('--include-dev-deps', inventory)
        self.assertIn('--list-all-pkgs', inventory)
        self.assertEqual(self.calls[-1], ['docker', 'rm', '-v', self.container])
        self.assertFalse(any('build' in args for args in self.calls))

    def test_native_binary_lock_and_inventory_refusals_always_remove_owned_container(self):
        for name in ['binary', 'marker', 'lock', 'schema', 'inventory', 'high', 'critical', 'link']:
            self.calls = []
            self.native, self.marker, self.runtime_lock, self.link_binary = b'\x7fELF', b'', self.lock, False
            self.findings = dict(SchemaVersion=2, Results=[dict(Type='cargo', Packages=[dict(Name='fixture')])])
            if name == 'binary': self.native = b'not-ELF'
            elif name == 'marker': self.marker = b'DEVELOPMENT_AUTHORITY'
            elif name == 'lock': self.runtime_lock = b'different'
            elif name == 'schema': self.findings['SchemaVersion'] = 1
            elif name == 'inventory': self.findings['Results'] = []
            elif name in {'high', 'critical'}:
                self.findings['Results'][0]['Vulnerabilities'] = [dict(Severity=name.upper())]
            elif name == 'link': self.link_binary = True
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.inspect()
            self.assertEqual(self.calls[-1], ['docker', 'rm', '-v', self.container])

    def test_uid_and_revision_mismatch_refuse_before_container_creation(self):
        original = copy.deepcopy(self.info)
        for changes in [dict(User='0:0'), dict(Labels={'org.opencontainers.image.revision': 'f' * 40})]:
            self.info = copy.deepcopy(original)
            self.info['Config'].update(changes)
            self.calls = []
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.inspect()
            self.assertFalse(any(args[:2] == ['docker', 'create'] for args in self.calls))

    def test_policy_paths_component_capabilities_and_links_fail_closed(self):
        self.write()
        self.assertEqual(scanner.policy(self.root, self.path)['rust']['api']['binaries'], self.contract['binaries'])
        original = copy.deepcopy(self.data)
        mutations = [
            lambda d: d.update(version=True),
            lambda d: d.update(command='arbitrary'),
            lambda d: d.update(deployment='../elsewhere'),
            lambda d: d['rust']['api'].update(binaries=[]),
            lambda d: d['rust']['api'].update(lock_installed='/app/../Cargo.lock'),
            lambda d: d['rust']['api'].update(source_lock='api/../Cargo.lock'),
            lambda d: d['rust']['api'].update(user='root'),
            lambda d: d['go']['gate'].update(expected_go='latest'),
        ]
        for mutation in mutations:
            self.data = copy.deepcopy(original)
            mutation(self.data)
            self.write()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                scanner.policy(self.root, self.path)
        self.path.unlink()
        self.path.symlink_to(self.root / 'deploy/scan.json')
        with self.assertRaises(ValueError):
            scanner.policy(self.root, self.path)

    def pipeline(self, legacy=False, fail=False):
        self.write()
        deployed = SimpleNamespace(load_config=lambda path: self.deployment,
            deployed_revisions=lambda repository, environment: [self.revision],
            product_images=lambda config, revision: ['ghcr.io/owner/demo-api:' + revision, 'ghcr.io/owner/demo-gate:' + revision])
        go = SimpleNamespace(scan=Mock(return_value=dict(inventory='go')))
        native = Mock(return_value=dict(inventory='cargo'))
        effects = []
        def execute(arguments, environment, timeout=60, **kwargs):
            effects.append(arguments)
            if fail and arguments[:2] == ['trivy', 'image']:
                raise subprocess.CalledProcessError(1, arguments)
        def capture(arguments, environment, timeout=60):
            return json.dumps([dict(Id=self.identity)])
        with patch.object(scanner, 'helper', side_effect=lambda tools, name: deployed if name == 'scan-deployed' else go), \
                patch.object(scanner, 'source_tree', return_value=self.tree), \
                patch.object(scanner, 'source_lock', return_value=None if legacy else self.lock), \
                patch.object(scanner, 'rust_image', native), \
                patch.object(scanner, 'run', side_effect=execute), patch.object(scanner, 'capture', side_effect=capture):
            result = scanner.scan(self.root, self.path, self.root / 'tools', environment={'GITHUB_REPOSITORY': 'owner/demo'})
        return result, effects, native, go

    def test_every_component_uses_immutable_image_and_its_actual_native_scanner(self):
        result, effects, native, go = self.pipeline()
        self.assertEqual(len(result['images']), 2)
        self.assertEqual(native.call_count, 1)
        self.assertEqual(go.scan.call_count, 1)
        self.assertEqual(go.scan.call_args.args[0], self.identity)
        self.assertTrue(all(args[-1] == self.identity for args in effects if args[:2] == ['trivy', 'image']))
        self.assertFalse(any('build' in args for args in effects))
        summary = json.loads((self.root / '.artifacts/security/deployed/summary.json').read_text())
        self.assertEqual(summary['scope'], 'deployed-dependencies')
        self.assertFalse(summary['failures'])

    def test_historical_go_images_use_the_real_go_runtime_scanner(self):
        _, _, native, go = self.pipeline(legacy=True)
        native.assert_not_called()
        self.assertEqual(go.scan.call_count, 2)

    def test_native_or_os_failures_propagate_and_preserve_the_failed_evidence(self):
        with self.assertRaisesRegex(ValueError, 'audit failed'):
            self.pipeline(fail=True)
        summary = json.loads((self.root / '.artifacts/security/deployed/summary.json').read_text())
        self.assertEqual(len(summary['failures']), 2)
        self.assertFalse(summary['images'])


if __name__ == '__main__':
    unittest.main()
