"""Historical reader identity is distinct from accepted build/runtime Tools."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('checked_activation_reader', ROOT / 'scripts/activation_reader.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class ActivationReader(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='separate activation reader ')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.checkout = self.root / 'reader'
        self.checkout.mkdir()
        self.git('init', '-q')
        self.git('remote', 'add', 'origin', 'https://github.com/nicodes/tools.git')
        (self.checkout / '.gitignore').write_text('ignored-fixture\n')
        (self.checkout / 'never-import.py').write_text('raise AssertionError("unchecked reader executed")\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Immutable reader fixture')
        self.revision = self.git('rev-parse', 'HEAD')
        self.pin = self.root / 'reader.json'
        self.identity = {'repository': 'nicodes/tools', 'revision': self.revision, 'role': 'historical-activation'}
        self.pin.write_text(json.dumps(self.identity))
        selected = patch.object(reader, 'PIN', self.pin)
        selected.start()
        self.addCleanup(selected.stop)

    def git(self, *arguments):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=' + os.devnull,
            '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            '-c', 'commit.gpgsign=false', '-C', str(self.checkout), *arguments],
            text=True, stderr=subprocess.DEVNULL).strip()

    def test_exact_clean_owning_main_reader_is_bound_without_replacing_recipe_sources(self):
        before = {key: os.environ.get(key) for key in ['TOOLS_ROOT', 'TOOLS_REF', 'COOKBOOK_ROOT', 'COOKBOOK_REF']}
        lookup = Mock(return_value={'status': 'ahead'})
        root, identity = reader.checkout(self.checkout, lookup)
        self.assertEqual(root, self.checkout)
        self.assertEqual(identity, self.identity)
        lookup.assert_called_once_with('repos/nicodes/tools/compare/' + self.revision + '...main')
        self.assertEqual(before, {key: os.environ.get(key) for key in before})

    def test_foreign_revision_mutation_untracked_and_ignored_sources_refuse_before_lookup(self):
        for prepare, restore in [
            (lambda: self.git('remote', 'set-url', 'origin', 'https://github.com/foreign/tools.git'),
             lambda: self.git('remote', 'set-url', 'origin', 'https://github.com/nicodes/tools.git')),
            (lambda: (self.checkout / 'never-import.py').write_text('changed\n'),
             lambda: self.git('checkout', '--', 'never-import.py')),
            (lambda: (self.checkout / 'untracked').write_text('unexpected\n'),
             lambda: (self.checkout / 'untracked').unlink()),
            (lambda: (self.checkout / 'ignored-fixture').write_text('unexpected\n'),
             lambda: (self.checkout / 'ignored-fixture').unlink()),
        ]:
            lookup = Mock(return_value={'status': 'identical'})
            prepare()
            try:
                with self.assertRaises(ValueError):
                    reader.checkout(self.checkout, lookup)
                lookup.assert_not_called()
            finally:
                restore()
        self.pin.write_text(json.dumps(self.identity | {'revision': '0' * 40}))
        with self.assertRaises(ValueError):
            reader.checkout(self.checkout, Mock(return_value={'status': 'identical'}))

    def test_nonaccepted_reader_and_source_change_during_acceptance_refuse(self):
        for response in [{'status': 'behind'}, {'status': 'diverged'}, {}, None]:
            with self.subTest(response=response), self.assertRaises(ValueError):
                reader.checkout(self.checkout, lambda _path: response)
        def mutate(_path):
            (self.checkout / 'unexpected').write_text('changed during lookup\n')
            return {'status': 'identical'}
        with self.assertRaises(ValueError):
            reader.checkout(self.checkout, mutate)

    def test_literal_reader_role_and_repository_are_closed_before_checkout(self):
        for change in [{'revision': None}, {'revision': 'main'}, {'revision': self.revision + '\n'},
                       {'role': 'runtime'}, {'repository': 'foreign/tools'}, {'extra': 'untrusted'}]:
            self.pin.write_text(json.dumps(self.identity | change))
            lookup = Mock()
            with self.subTest(change=change), self.assertRaises(ValueError):
                reader.checkout(self.checkout, lookup)
            lookup.assert_not_called()
        self.pin.write_text('{"repository":"nicodes/tools","repository":"foreign/tools"}')
        with self.assertRaises(ValueError):
            reader.identity()

    def test_linked_pin_or_reader_root_refuses(self):
        self.pin.unlink()
        actual = self.root / 'actual.json'
        actual.write_text(json.dumps(self.identity))
        self.pin.symlink_to(actual)
        with self.assertRaises(ValueError):
            reader.identity()
        self.pin.unlink()
        self.pin.write_text(json.dumps(self.identity))
        linked = self.root / 'linked-reader'
        linked.symlink_to(self.checkout, target_is_directory=True)
        with self.assertRaises(ValueError):
            reader.checkout(linked, lambda _path: {'status': 'identical'})

    def test_workflow_output_contains_only_the_fixed_reader_identity(self):
        output = self.root / 'output'
        output.touch()
        with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}), \
                patch('sys.argv', ['activation_reader.py', '--github-output']):
            reader.main()
        self.assertEqual(output.read_text(), 'repository=nicodes/tools\nrevision=' + self.revision + '\n')
