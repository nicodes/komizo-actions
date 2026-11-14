import base64
import copy
import hashlib
import importlib.util
import json
import os
import stat
from source_fixture import write_source
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('native_deployed_scan', Path(__file__).parents[1] / 'scripts/deployed_scan.py')
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class RegistryReadAuthority(unittest.TestCase):
    def setUp(self):
        self.environment = {'REGISTRY_USERNAME': 'fixture-owner',
                            'REGISTRY_PASSWORD': 'fixture-read-token'}

    def test_private_ghcr_only_file_is_consumed_by_the_native_runtime(self):
        tools = Path(os.environ['RECIPE_SOURCE_SDK'])
        runtime = scanner.helper(tools, 'container_runtime')
        original = dict(self.environment)
        with scanner.registry_authority(self.environment) as values:
            path = Path(values['REGISTRY_AUTH_FILE'])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(path.stat().st_nlink, 1)
            self.assertEqual(json.loads(path.read_text()), {'auths': {'ghcr.io': {
                'auth': base64.b64encode(b'fixture-owner:fixture-read-token').decode()}}})
            for environment in [values, None]:
                arguments, inherited = runtime.command(['pull', 'ghcr.io/fixture/image@sha256:' + 'a' * 64], environment)
                self.assertEqual(arguments[:5], ['podman', '--remote=false', 'pull', '--authfile', str(path)])
                self.assertEqual(inherited['REGISTRY_AUTH_FILE'], str(path))
                self.assertNotIn(self.environment['REGISTRY_PASSWORD'], arguments)
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())
        self.assertEqual(self.environment, original)

    def test_failure_removes_only_owned_credentials_and_restores_previous_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path(directory) / 'existing-auth.json'
            previous.write_text('original caller bytes\n')
            with patch.dict(os.environ, {'REGISTRY_AUTH_FILE': str(previous)}):
                with self.assertRaisesRegex(RuntimeError, '^scanner failed$'):
                    with scanner.registry_authority(self.environment) as values:
                        path = Path(values['REGISTRY_AUTH_FILE'])
                        raise RuntimeError('scanner failed')
                self.assertEqual(os.environ['REGISTRY_AUTH_FILE'], str(previous))
                self.assertFalse(path.parent.exists())
                self.assertEqual(previous.read_text(), 'original caller bytes\n')

    def test_no_previous_authority_remains_absent_after_the_scan(self):
        with patch.dict(os.environ, {}, clear=True):
            with scanner.registry_authority(self.environment):
                self.assertIn('REGISTRY_AUTH_FILE', os.environ)
            self.assertNotIn('REGISTRY_AUTH_FILE', os.environ)

    def test_missing_malformed_or_oversized_credentials_refuse_before_file_creation(self):
        for changes in [{'REGISTRY_USERNAME': ''}, {'REGISTRY_USERNAME': 'other:owner'},
                        {'REGISTRY_USERNAME': 'x' * 40}, {'REGISTRY_USERNAME': None},
                        {'REGISTRY_PASSWORD': ''}, {'REGISTRY_PASSWORD': 'x' * 32769},
                        {'REGISTRY_PASSWORD': 'secret\n'}, {'REGISTRY_PASSWORD': 'secret\x7f'},
                        {'REGISTRY_PASSWORD': None}]:
            with self.subTest(changes=list(changes)), patch.object(scanner.tempfile, 'TemporaryDirectory') as temporary:
                with self.assertRaisesRegex(ValueError, '^deployed scans require bounded explicit GHCR read credentials$'):
                    with scanner.registry_authority(self.environment | changes):
                        self.fail('invalid authority admitted')
                temporary.assert_not_called()

    def test_both_audit_workflows_prepare_podman_and_keep_credentials_in_the_deployed_step(self):
        workflows = Path(__file__).parents[1] / '.github/workflows'
        for name in ['cookbook-security-audit.yml', 'cookbook-security-audit-no-report.yml']:
            text = (workflows / name).read_text()
            with self.subTest(workflow=name):
                self.assertNotIn('docker/login-action', text)
                self.assertIn('run: bash "$TOOLS_ROOT/helpers/ci-container-runtime.sh"', text)
                before, deployed = text.split('    - name: Audit actual deployed runtime dependencies\n')
                deployed = deployed.split('    - uses: actions/upload-artifact@')[0]
                self.assertNotIn('REGISTRY_PASSWORD:', before)
                self.assertIn('REGISTRY_USERNAME: ${{ github.repository_owner }}', deployed)
                self.assertIn('REGISTRY_PASSWORD: ${{ github.token }}', deployed)
                self.assertIn("timeout-minutes: ${{ fromJSON(inputs['step-timeouts']).deployed }}", deployed)


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
        self.contract = dict(source_lock='api/Cargo.lock',
            lock_installed='/usr/share/demo/Cargo.lock', binaries=['/app/server', '/usr/local/bin/reconcile'],
            user='10001:10001', forbidden=['DEVELOPMENT_AUTHORITY'])
        self.data = dict(version=1, deployment='deploy/scan.toml', rust={'api':{'user':self.contract['user'],
            'forbidden':self.contract['forbidden'],'units':[{key:self.contract[key] for key in ['source_lock','lock_installed','binaries']}]}},os=[],
                        go={'gate': dict(forbidden=[])})
        self.deployment = dict(repository='owner/demo', application='demo',workflow='.github/workflows/cd.yml',
                               components=['api', 'gate'], environment='production')
        write_source(self.root / 'deploy/scan.toml',self.deployment)
        self.path = self.root / 'deploy/deployed-scan.toml'
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
        write_source(self.path,self.data)

    def capture(self, arguments, environment, timeout=60, **kwargs):
        self.calls.append(arguments)
        if arguments[:3] == ['podman', 'image', 'inspect']:
            return json.dumps([self.info])
        if arguments[:2] == ['podman', 'create']:
            self.assertEqual(arguments[-1], self.identity)
            return self.container
        raise AssertionError('unexpected inspection command: ' + repr(arguments))

    def run_command(self, arguments, environment, timeout=60, **kwargs):
        self.calls.append(arguments)
        if arguments[:2] == ['podman', 'cp']:
            destination = Path(arguments[-1])
            if arguments[-2].endswith('/Cargo.lock'):
                destination.write_bytes(self.runtime_lock)
            elif self.link_binary:
                destination.symlink_to(self.root / 'outside')
            else:
                destination.write_bytes(self.native + self.marker)
        elif arguments[:2] == ['trivy', 'fs']:
            Path(arguments[arguments.index('--output') + 1]).write_text(json.dumps(self.findings))
        elif arguments[:2] != ['podman', 'rm']:
            raise AssertionError('unexpected scanner effect: ' + repr(arguments))

    def inspect(self):
        with patch.object(scanner, 'capture', side_effect=self.capture), patch.object(scanner, 'run', side_effect=self.run_command):
            return scanner.rust_image(self.identity, self.revision, self.lock, self.contract,
                                      self.reports, 'api', {})

    def test_exact_git_blob_bytes_require_the_actual_declared_native_rust_lock(self):
        with patch.object(scanner, 'api', return_value=self.blob):
            self.assertEqual(scanner.source_lock('owner/demo', self.tree, self.contract, {}), self.lock)
        legacy = {'api/go.mod': dict(type='blob', sha='d' * 40)}
        with self.assertRaises(ValueError):scanner.source_lock('owner/demo',legacy,self.contract,{})
        with self.assertRaisesRegex(ValueError, 'no exact declared Rust source lock'):
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
        self.assertEqual(self.calls[-1], ['podman', 'rm', '-v', self.container])
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
            self.assertEqual(self.calls[-1], ['podman', 'rm', '-v', self.container])

    def test_uid_and_revision_mismatch_refuse_before_container_creation(self):
        original = copy.deepcopy(self.info)
        for changes in [dict(User='0:0'), dict(Labels={'org.opencontainers.image.revision': 'f' * 40})]:
            self.info = copy.deepcopy(original)
            self.info['Config'].update(changes)
            self.calls = []
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.inspect()
            self.assertFalse(any(args[:2] == ['podman', 'create'] for args in self.calls))

    def test_policy_paths_component_capabilities_and_links_fail_closed(self):
        self.data['go']['gate']['expected_go'] = '1.27.3'
        self.write()
        self.assertEqual(scanner.policy(self.root, self.path)['rust']['api']['units'][0]['binaries'], self.contract['binaries'])
        self.assertEqual(scanner.policy(self.root, self.path)['go']['gate']['expected_go'], '1.27.3')
        original = copy.deepcopy(self.data)
        mutations = [
            lambda d: d.update(version=True),
            lambda d: d.update(command='arbitrary'),
            lambda d: d.update(deployment='../elsewhere'),
            lambda d: d['rust']['api']['units'][0].update(binaries=[]),
            lambda d: d['rust']['api']['units'][0].update(lock_installed='/app/../Cargo.lock'),
            lambda d: d['rust']['api']['units'][0].update(source_lock='api/../Cargo.lock'),
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
        self.path.symlink_to(self.root / 'deploy/scan.toml')
        with self.assertRaises(ValueError):
            scanner.policy(self.root, self.path)

    def pipeline(self, legacy=False, fail=False, os_only=False, multiple_units=False, missing_activation=False):
        if legacy:self.data['rust']={};self.data['go']['api']={'forbidden':[]}
        if os_only:self.data['go'].pop('gate');self.data['os']=['gate']
        if multiple_units:self.data['rust']['api']['units'].append({'source_lock':'internal/render/Cargo.lock',
            'lock_installed':'/usr/share/demo/render.Cargo.lock','binaries':['/app/evaluate']})
        self.write()
        deployed = SimpleNamespace(load_config=lambda path: self.deployment,
            deployed_releases=lambda repository, environment: [{'revision':self.revision}],
            activation_for_deployment=lambda config,deployment,environment: {'scope':'activation_at_time','current_state':'unobserved',
                'descriptor':{'source':{'revision':self.revision},'artifacts':{name:{'config_digest':self.identity,'platform':'linux/amd64'} for name in ['api','gate']}},
                'operation':{'phase':'ready','operation_id':'f'*32},'proof':{'artifact_digest':'sha256:'+'c'*64}},
            product_images=lambda config, revision: ['ghcr.io/owner/demo-api@'+self.identity, 'ghcr.io/owner/demo-gate@'+self.identity])
        go = SimpleNamespace(scan=Mock(return_value=dict(inventory='go')))
        if missing_activation:del deployed.activation_for_deployment
        native = Mock(return_value=dict(inventory='cargo'))
        effects = []
        def execute(arguments, environment, timeout=60, **kwargs):
            effects.append(arguments)
            if fail and arguments[:2] == ['trivy', 'image']:
                raise subprocess.CalledProcessError(1, arguments)
        def capture(arguments, environment, timeout=60, **kwargs):
            return json.dumps([dict(Id=self.identity,Os='linux',Architecture='amd64')])
        with patch.object(scanner, 'helper', side_effect=lambda tools, name: deployed if name == 'scan-deployed' else go), \
                patch.object(scanner, 'source_tree', return_value=self.tree), \
                patch.object(scanner, 'source_lock', side_effect=lambda repo,tree,record,env:self.lock+(b'# renderer\n' if record['source_lock'].startswith('internal/') else b'')), \
                patch.object(scanner, 'rust_image', native), \
                patch.object(scanner, 'run', side_effect=execute), patch.object(scanner, 'capture', side_effect=capture), \
                patch.object(scanner,'scan_os',side_effect=lambda image,identity,report,environment,*_:execute(['trivy','image','--input','/verified/oci/layout',identity],environment)):
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
        self.assertEqual(summary['scope'], 'activation_at_time')
        self.assertEqual(summary['current_state'],'unobserved')
        self.assertEqual(summary['audit'],'dependencies')
        self.assertEqual(summary['activations'][0]['operation']['phase'],'ready')
        self.assertFalse(summary['failures'])

    def test_os_only_images_still_receive_the_full_os_and_library_scan(self):
        result,effects,native,go=self.pipeline(os_only=True)
        self.assertEqual(len([args for args in effects if args[:2]==['trivy','image']]),2)
        self.assertEqual(native.call_count,1);go.scan.assert_not_called()
        self.assertEqual(result['images'][1]['inventory'],'os-library')

    def test_multiple_rust_units_retain_each_distinct_lock_binary_and_report_identity(self):
        result,effects,native,go=self.pipeline(multiple_units=True)
        self.assertEqual(native.call_count,2)
        self.assertEqual([call.args[2] for call in native.call_args_list],[self.lock,self.lock+b'# renderer\n'])
        self.assertEqual([call.args[5] for call in native.call_args_list],['api-0','api-1'])
        self.assertEqual(len(result['images'][0]['units']),2)
        self.assertEqual(len([args for args in effects if args[:2]==['trivy','image']]),2)

    def test_older_publication_only_reader_cannot_be_substituted_for_activation(self):
        with self.assertRaisesRegex(ValueError,'historical activation reader'):self.pipeline(missing_activation=True)
        self.assertFalse((self.root/'.artifacts/security/deployed/summary.json').exists())

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
