import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import native_recipe_fixture

spec = importlib.util.spec_from_file_location('profile_backup', Path(__file__).parents[1] / 'scripts/backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
TOOLS = Path(os.environ['RECIPE_TASK_SDK'])


class RestoreProfiles(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'deploy').mkdir()
        self.inputs = self.root / '.artifacts/restore/download'
        self.inputs.mkdir(parents=True)
        (self.inputs / 'snapshot.cms').write_bytes(b'opaque encrypted fixture')
        (self.inputs / 'receipt.json').write_text('{"format":1}\n')
        (self.inputs / 'never-package.pem').write_text('private fixture excluded from ciphertext archive')
        self.key = self.root / 'recovery.key'
        self.key.write_text('local fixture key')
        self.key.chmod(0o600)
        self.cookbook = self.root / 'selected-cookbook'
        (self.cookbook/'cookbook').mkdir(parents=True)
        (self.cookbook/'cookbook/application_restore.py').write_text('# Explicit selected verifier fixture; never executed in these transport tests.\n')
        self.config = self.root / 'deploy/restore.toml'
        native = dict(project='demo', backend='postgresql', image_base='ghcr.io/owner/demo',
            runtime_role='demo_runtime', dsn_file='/run/secrets/database.url', api_port=8080,
            health_path='/health', root='/srv/public/app', env={})
        native_recipe_fixture.write(self.config,native)
        native_recipe_fixture.write(self.root / 'deploy/restore-previous.toml',native | dict(project='previous'))
        self.valid = dict(version=1, project='demo', published_ports=[], database_integrity='ok',
            database_boot='passed', api_boot='passed', frontend_artifact='passed', cleanup='passed', database_restore='passed',
            scope='disposable authenticated snapshot restoration',current_source_ci_acceptance=False,
            network='none; shared isolated loopback only',media_integrity='passed',
            http_assertions={'api_health':'passed','frontend_document':'passed','unknown_host_refusal':'passed'},
            revision='a'*40,snapshot_release={'digest':'sha256:'+'b'*64,
                'source':{'repository':'example/demo','repository_id':'123','revision':'a'*40},
                'provider':{'cookbook_revision':'c'*40,'tools_revision':'d'*40,'core_revision':'e'*40}},
            images={'api':'sha256:'+'f'*64},static_artifacts={'web':'sha256:'+'a'*64},
            files_restored=1,archive_sha256='b'*64,ciphertext_sha256='c'*64)
        self.report = copy.deepcopy(self.valid)
        self.archives = []
        self.commands = []

    def child(self, arguments, stdout, env):
        archive = Path(arguments[arguments.index('--export') + 1])
        self.archives.append(archive)
        self.commands.append(arguments)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)
        with tarfile.open(archive) as source:
            self.assertEqual(source.getnames(), ['snapshot.cms', 'receipt.json'])
            self.assertEqual(source.extractfile('snapshot.cms').read(), b'opaque encrypted fixture')
            self.assertTrue(all(member.isfile() and member.mode == 0o600 for member in source))
        self.assertNotIn('BACKUP_RECOVERY_KEY_FILE', env)
        self.assertEqual(arguments[arguments.index('--key') + 1], str(self.key))
        stdout.write(json.dumps(self.report).encode())
        return SimpleNamespace(wait=lambda timeout: 0, poll=lambda: 0)

    def restore(self, pull=False):
        return backup.restore_pair(self.root, 'demo', self.config, TOOLS, self.inputs, self.key, cookbook=self.cookbook, pull=pull)

    def test_only_encrypted_pair_is_packaged_and_exact_report_is_private_and_retained(self):
        before = {name: (self.inputs / name).read_bytes() for name in ['snapshot.cms', 'receipt.json', 'never-package.pem']}
        with patch.object(backup.subprocess, 'Popen', side_effect=self.child):
            self.assertEqual(self.restore(pull=True), self.valid)
        self.assertIn('--pull', self.commands[0])
        report = self.root / '.artifacts/restore/report.json'
        self.assertEqual(json.loads(report.read_text()), self.valid)
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(not path.exists() for path in self.archives))
        self.assertEqual(before, {name: (self.inputs / name).read_bytes() for name in before})
        self.assertEqual(self.key.read_text(), 'local fixture key')

    def test_project_ports_integrity_boot_and_cleanup_failures_preserve_previous_report(self):
        report = self.root / '.artifacts/restore/report.json'
        report.write_text('prior evidence\n')
        for changes in [dict(version=2), dict(project='previous'), dict(published_ports=['8080']),
                        dict(database_integrity='failed'), dict(database_boot='failed'), dict(api_boot='failed'),
                        dict(frontend_artifact='failed'), dict(cleanup='failed'),dict(current_source_ci_acceptance=True),
                        dict(snapshot_release={}),dict(media_integrity='failed'),dict(http_assertions={})]:
            self.report = self.valid | changes
            with self.subTest(changes=changes), patch.object(backup.subprocess, 'Popen', side_effect=self.child):
                with self.assertRaises(ValueError):
                    self.restore()
            self.assertEqual(report.read_text(), 'prior evidence\n')
        self.assertTrue(all(not path.exists() for path in self.archives))

    def test_nonprivate_relative_and_linked_keys_refuse_before_starting_any_restore(self):
        with patch.object(backup.subprocess, 'Popen') as child:
            self.key.chmod(0o644)
            with self.assertRaises(ValueError):
                self.restore()
            self.key.chmod(0o600)
            with self.assertRaises(ValueError):
                backup.restore_pair(self.root, 'demo', self.config, TOOLS, self.inputs, Path('relative-key'), cookbook=self.cookbook)
            target = self.root / 'actual-key'
            self.key.rename(target)
            self.key.symlink_to(target)
            with self.assertRaises(ValueError):
                self.restore()
            child.assert_not_called()

    def test_linked_or_oversized_inputs_cannot_package_other_local_data(self):
        ciphertext = self.inputs / 'snapshot.cms'
        ciphertext.unlink()
        ciphertext.symlink_to(self.key)
        with patch.object(backup.subprocess, 'Popen') as child:
            with self.assertRaises(OSError):
                self.restore()
            child.assert_not_called()
        ciphertext.unlink()
        ciphertext.write_bytes(b'opaque encrypted fixture')
        (self.inputs / 'receipt.json').write_bytes(b'x' * (64 * 1024 + 1))
        with patch.object(backup.subprocess, 'Popen') as child:
            with self.assertRaises(ValueError):
                self.restore()
            child.assert_not_called()

    def test_interrupt_terminates_only_its_child_before_removing_temporary_archive(self):
        child = Mock()
        child.wait.side_effect = [backup.transport.Interrupted(signal.SIGTERM), 0]
        child.poll.return_value = None
        def start(arguments, **kwargs):
            self.archives.append(Path(arguments[arguments.index('--export') + 1]))
            return child
        with patch.object(backup.subprocess, 'Popen', side_effect=start):
            with self.assertRaises(SystemExit) as interrupted:
                self.restore()
        self.assertEqual(interrupted.exception.code, 143)
        child.terminate.assert_called_once()
        child.kill.assert_not_called()
        self.assertEqual(child.wait.call_args.kwargs['timeout'], 180)
        self.assertFalse(self.archives[0].exists())
        self.assertTrue((self.inputs / 'snapshot.cms').exists())

    def select_profile(self,name,project):
        context=SimpleNamespace(cookbook=TOOLS,module=lambda _:native_recipe_fixture.Module('operational_workflows'))
        return backup.restore_profile(self.root,TOOLS,name,project,context=context)

    def test_only_declared_default_and_historical_profiles_select_native_configuration(self):
        config = dict(version=1, project='demo', komizo_ref='a' * 40,
            publish_runner='ubuntu-24.04', publish_timeout=35, deploy_runner='ubuntu-24.04',
            deploy_timeout=100, health_urls=['https://example.test/readyz'],
            notification_url='https://example.test/')
        native_recipe_fixture.write(self.root / 'deploy/delivery.toml',config)
        native_recipe_fixture.write(self.root / 'deploy/operations.toml',dict(version=1, project='demo',
            restore_profiles=[dict(name='default', project='demo', config='deploy/restore.toml'),
                              dict(name='legacy', project='previous', config='deploy/restore-previous.toml')]))
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[delivery]\ndefinition="deploy/delivery.toml"\n[operations]\ndefinition="deploy/operations.toml"\n')
        with patch.object(__import__('recipe_data'),'load',side_effect=lambda root,tools: native_recipe_fixture.load(root)):
            self.assertEqual(self.select_profile('default', 'demo'), self.config)
            self.assertEqual(self.select_profile('legacy', 'previous'), self.root / 'deploy/restore-previous.toml')
            for name, project in [('default', 'previous'), ('unknown', 'demo'), ('../outside', 'demo')]:
                with self.subTest(name=name, project=project), self.assertRaises(ValueError):
                    self.select_profile(name, project)

    def test_local_default_resolves_the_existing_exact_make_pin_without_fetching(self):
        source=self.root/'stage'
        source.mkdir()
        def git(*arguments):
            return subprocess.check_output(['git','-c','user.name=Fixture','-c','user.email=fixture@example.test',
                '-c','commit.gpgsign=false','-C',str(source),*arguments],text=True,stderr=subprocess.DEVNULL).strip()
        git('init')
        (source/'README').write_text('fixture\n')
        (source/'.gitignore').write_text('ignored\n')
        git('add','.')
        git('commit','-qm','Fixture cookbook')
        revision=git('rev-parse','HEAD')
        destination=self.root/'.artifacts/cookbook'/revision
        destination.parent.mkdir(parents=True)
        source.rename(destination)
        (self.root/'Makefile').write_text('COOKBOOK_REF := '+revision+'\n')
        self.assertEqual(backup.prepared_cookbook(self.root),destination)
        (destination/'ignored').write_text('poison')
        with self.assertRaises(ValueError):
            backup.prepared_cookbook(self.root)


if __name__ == '__main__':
    unittest.main()
