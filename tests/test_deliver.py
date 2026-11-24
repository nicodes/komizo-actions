"""Single-job delivery replaces cross-job proofs with local file and identity checks."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import deliver
import oci_registry as registry
import publication
from test_release_packet import make_packet
from test_submission import ENV

SOURCE, TREE, COOKBOOK = 'a' * 40, 'b' * 40, 'c' * 40
CONFIGURATION = b'version = 1\n'
GROUPS = ['lint', 'unit', 'security']


class LocalAcceptance(unittest.TestCase):
    def setUp(self):
        self.environment = ENV | {'GITHUB_WORKFLOW_REF': 'example/mailroom/.github/workflows/deploy.yml@refs/heads/main',
                                  'GITHUB_WORKFLOW_SHA': SOURCE, 'GITHUB_JOB': 'deliver'}
        self.proof = {'version': 1, 'source_commit': SOURCE, 'source_tree': TREE, 'cookbook_commit': COOKBOOK,
                      'recipe': 'react-native', 'status': 'passed', 'tracked_source_clean': True,
                      'build_verified': True, 'tests_verified': True, 'test_groups': GROUPS,
                      'execution_context': {key: self.environment[key] for key in [
                          'GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_SHA',
                          'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA', 'GITHUB_EVENT_NAME', 'GITHUB_JOB']}}
        self.config = {'recipe': 'react-native'}

    def accept(self, **changes):
        arguments = dict(proof=self.proof, config=self.config, source=SOURCE, tools=COOKBOOK, tree=TREE,
                         groups=GROUPS, environment=self.environment) | changes
        deliver.accept_local(**arguments)

    def test_proof_from_this_very_job_is_accepted(self):
        self.accept()

    def test_proof_from_another_job_id_is_refused(self):
        self.proof['execution_context']['GITHUB_JOB'] = 'validate'
        with self.assertRaisesRegex(ValueError, 'same job'):
            self.accept()
        self.proof['execution_context'].pop('GITHUB_JOB')
        with self.assertRaisesRegex(ValueError, 'same job'):
            self.accept()

    def test_tree_group_run_and_status_mismatches_are_refused(self):
        with self.assertRaises(ValueError): self.accept(tree='d' * 40)
        with self.assertRaises(ValueError): self.accept(groups=['lint', 'unit'])
        self.proof['execution_context']['GITHUB_RUN_ID'] = '999'
        with self.assertRaisesRegex(ValueError, 'another workflow execution'): self.accept()
        self.proof['execution_context']['GITHUB_RUN_ID'] = ENV['GITHUB_RUN_ID']
        self.proof['status'] = 'failed'
        with self.assertRaises(ValueError): self.accept()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.chdir(self.root))
        self.environment = ENV | {'GITHUB_WORKFLOW_REF': 'example/mailroom/.github/workflows/deploy.yml@refs/heads/main',
                                  'GITHUB_WORKFLOW_SHA': SOURCE, 'GITHUB_JOB': 'deliver'}
        (self.root / 'cookbook.toml').write_bytes(CONFIGURATION)
        release = self.root / '.artifacts/release'; release.mkdir(parents=True)
        make_packet(release)
        self.descriptor = json.loads((release / 'release.json').read_bytes())
        names = ['release.json', 'inventory.json', 'static/web.tar.gz']
        self.proof = {'version': 1, 'source_commit': SOURCE, 'source_tree': TREE, 'cookbook_commit': COOKBOOK,
                      'recipe': 'react-native', 'status': 'passed', 'tracked_source_clean': True,
                      'build_verified': True, 'tests_verified': True, 'test_groups': GROUPS,
                      'configuration_sha256': hashlib.sha256(CONFIGURATION).hexdigest(),
                      'entrypoint_contract': 'mise-v1', 'providers': self.descriptor['providers'],
                      'execution_context': {key: self.environment[key] for key in [
                          'GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_SHA',
                          'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA', 'GITHUB_EVENT_NAME', 'GITHUB_JOB']},
                      'artifacts': {'.artifacts/release/' + name: hashlib.sha256((release / name).read_bytes()).hexdigest()
                                    for name in names}}
        self.write_proof()
        evidence = self.root / '.artifacts/komizo-release'; evidence.mkdir()
        self.publication = {'kind': 'komizo.publication', 'version': 1,
                            'release_digest': registry.digest((release / 'release.json').read_bytes()),
                            'validation_sha256': hashlib.sha256(registry.canonical(self.proof)).hexdigest(),
                            'execution_context': self.proof['execution_context']}
        (evidence / 'publication.json').write_bytes(registry.canonical(self.publication))
        self.state = {'tree': TREE, 'head': SOURCE, 'main': SOURCE}
        providers = self.descriptor['providers']
        self.context = SimpleNamespace(
            cookbook_revision=providers['cookbook_revision'], tools_revision=providers['tools_revision'],
            module=lambda name: SimpleNamespace(pin=lambda: {'revision': providers['core_revision']}),
            require_proof=Mock())
        self.stack.enter_context(patch.object(deliver, 'git', side_effect=lambda *arguments:
            self.state['tree'] if arguments == ('rev-parse', 'HEAD^{tree}') else self.state['head']))
        self.stack.enter_context(patch.object(deliver.recipe_context, 'accepted_context', return_value=self.context))
        real = publication.release_proof
        self.stack.enter_context(patch.object(deliver.publication, 'release_proof',
            lambda admitted, environment: real(admitted, environment, token_request=lambda *_: 'a.b.c')))
        self.args = SimpleNamespace(source=SOURCE, project='mailroom', cookbook_root=self.root, cookbook_ref=COOKBOOK,
                                    tools_root=self.root, preview=False, controller_root=None)

    def write_proof(self):
        (self.root / '.artifacts/cookbook-validation.json').write_text(json.dumps(self.proof))

    def lookup(self, path):
        return {'commit': {'sha': self.state['main']}}

    def authorize(self):
        deliver.run_authorize(self.args, self.environment, self.lookup)


class Authorize(Fixture):
    def test_mints_one_release_proof_for_the_local_packet(self):
        self.authorize()
        value = json.loads((self.root / '.artifacts/komizo-release/proof.json').read_bytes())
        self.assertEqual(value['release_digest'], self.publication['release_digest'])
        self.assertEqual(value['source_revision'], SOURCE); self.assertEqual(value['run_id'], ENV['GITHUB_RUN_ID'])
        self.context.require_proof.assert_called_once()

    def refused(self, pattern):
        with self.assertRaisesRegex(ValueError, pattern):
            self.authorize()
        self.assertFalse((self.root / '.artifacts/komizo-release/proof.json').exists())

    def test_main_advanced_is_refused(self):
        self.state['main'] = 'd' * 40
        self.refused('no longer|advanced')

    def test_proof_from_another_job_is_refused(self):
        self.proof['execution_context']['GITHUB_JOB'] = 'validate'; self.write_proof()
        self.refused('same job')

    def test_tree_mismatch_is_refused(self):
        self.state['tree'] = 'd' * 40
        self.refused('tree|source')

    def test_cookbook_toml_mismatch_is_refused(self):
        (self.root / 'cookbook.toml').write_bytes(CONFIGURATION + b'# edited\n')
        self.refused('configuration')

    def test_wrong_caller_workflow_is_refused(self):
        self.environment['GITHUB_WORKFLOW_REF'] = 'example/mailroom/.github/workflows/other.yml@refs/heads/main'
        self.refused('trusted main caller')

    def test_head_that_is_not_the_run_source_is_refused(self):
        self.state['head'] = 'd' * 40
        self.refused('controller checkout')

    def test_publication_from_another_job_is_refused(self):
        evidence = self.root / '.artifacts/komizo-release/publication.json'
        value = json.loads(evidence.read_bytes()); value['execution_context'] = value['execution_context'] | {'GITHUB_JOB': 'publish'}
        evidence.write_bytes(registry.canonical(value))
        self.refused('this job')

    def test_packet_that_differs_from_the_proof_is_refused(self):
        self.proof['artifacts']['.artifacts/release/release.json'] = '0' * 64; self.write_proof()
        self.refused('same-source acceptance')

    def test_providers_that_differ_from_the_tested_ones_are_refused(self):
        self.proof['providers'] = self.proof['providers'] | {'core_revision': '9' * 40}; self.write_proof()
        self.refused('providers')


class Publish(Fixture):
    def run_publish(self, packet_source=None):
        helpers = self.root / 'tools'; (helpers / 'helpers').mkdir(parents=True, exist_ok=True)
        (helpers / 'helpers/verification.py').write_text(
            "def release_definition(path):\n    return {'project':'mailroom','tests':%r}\n" % GROUPS)
        self.context.cookbook, self.context.tools = self.root, helpers
        self.context.cookbook_revision = COOKBOOK; self.context.tools_revision = 'd' * 40
        self.environment['GITHUB_REPOSITORY_ID'] = '123'
        publish, evidence = Mock(return_value={'web': 'reference'}), Mock()
        receipts = ({'build': 1}, {'test': 2})
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, self.environment, clear=True))
            stack.enter_context(patch.object(deliver.subprocess, 'run', return_value=SimpleNamespace(returncode=0)))
            stack.enter_context(patch.object(deliver.recipe_data, 'load', return_value={'recipe': 'react-native'}))
            stack.enter_context(patch.object(deliver.recipe_data, 'effective_definition', return_value='release.toml'))
            admit = stack.enter_context(patch.object(deliver.publication, 'admit_receipts', return_value=receipts))
            stack.enter_context(patch.object(deliver.publication, 'publish', publish))
            stack.enter_context(patch.object(deliver.publication, 'write_evidence', evidence))
            if packet_source:
                real = deliver.release_packet.Packet
                def packet(*arguments, **keywords):
                    value = real(*arguments, **keywords)
                    value.descriptor = copy.deepcopy(value.descriptor) | {'source': packet_source}
                    return value
                stack.enter_context(patch.object(deliver.release_packet, 'Packet', packet))
            try:
                deliver.run_publish(self.args, dict(os.environ), self.lookup)
            finally:
                self.admit, self.publish, self.evidence = admit, publish, evidence
        return receipts

    def test_publishes_the_packet_in_place_and_writes_evidence(self):
        receipts = self.run_publish()
        self.admit.assert_called_once_with(Path.cwd(), self.proof, SOURCE, GROUPS)
        packet = self.publish.call_args.args[0]
        self.assertEqual(packet.root, (self.root / '.artifacts/release').resolve())
        self.assertEqual(self.evidence.call_args.args[4], Path('.artifacts/komizo-release'))
        self.assertIs(self.evidence.call_args.kwargs['receipts'], receipts)

    def test_proof_from_another_job_publishes_nothing(self):
        self.proof['execution_context']['GITHUB_JOB'] = 'validate'; self.write_proof()
        with self.assertRaisesRegex(ValueError, 'same job'): self.run_publish()
        self.publish.assert_not_called(); self.evidence.assert_not_called()

    def test_tree_cookbook_toml_and_source_mismatches_publish_nothing(self):
        self.state['tree'] = 'd' * 40
        with self.assertRaises(ValueError): self.run_publish()
        self.publish.assert_not_called()
        self.state['tree'] = TREE
        (self.root / 'cookbook.toml').write_bytes(b'changed\n')
        with self.assertRaisesRegex(ValueError, 'recipe differs'): self.run_publish()
        self.publish.assert_not_called()
        (self.root / 'cookbook.toml').write_bytes(CONFIGURATION)
        with self.assertRaisesRegex(ValueError, 'exact workflow source'):
            self.run_publish({'repository': 'foreign/mailroom', 'repository_id': '123', 'revision': SOURCE})
        self.publish.assert_not_called()

    def test_head_that_is_not_main_source_publishes_nothing(self):
        self.state['head'] = 'd' * 40
        with self.assertRaisesRegex(ValueError, 'final main source'): self.run_publish()
        self.publish.assert_not_called()


PLAN = {'action': 'up', 'head': SOURCE, 'pr': 7, 'repository': 'example/mailroom',
        'policy': {'project': 'mailroom', 'test_groups': GROUPS}}


class PreviewMixin:
    """The same local checks serve a manual preview whose plan comes from the trusted main controller."""
    def setUp(self):
        super().setUp()
        self.plan = copy.deepcopy(PLAN)
        self.environment |= {'GITHUB_EVENT_NAME': 'workflow_dispatch',
                             'GITHUB_WORKFLOW_REF': 'example/mailroom/.github/workflows/pr-preview.yml@refs/heads/main'}
        for key in ['GITHUB_EVENT_NAME', 'GITHUB_WORKFLOW_REF']:
            self.proof['execution_context'][key] = self.environment[key]
        self.write_proof()
        self.publication['validation_sha256'] = hashlib.sha256(registry.canonical(self.proof)).hexdigest()
        self.publication['execution_context'] = self.proof['execution_context']
        (self.root / '.artifacts/komizo-release/publication.json').write_bytes(registry.canonical(self.publication))
        self.args.preview, self.args.controller_root = True, self.root / 'controller'
        self.state['main'] = 'd' * 40  # a preview never requires its source to be main
        import preview
        self.validate = self.stack.enter_context(patch.object(preview, 'validate_plan', side_effect=lambda *_a, **_k: self.plan))
        self.policy = self.stack.enter_context(patch.object(preview, 'source_policy'))
        self.current = self.stack.enter_context(patch.object(preview, 'require_current'))
        self.stack.enter_context(patch.object(preview, 'current_pr', return_value={}))
        self.environment['PLAN'] = '{}'; self.context.cookbook = self.root


class PreviewAuthorize(PreviewMixin, Authorize):
    def test_mints_one_release_proof_for_the_local_packet(self):
        super().test_mints_one_release_proof_for_the_local_packet()
        self.assertGreaterEqual(self.validate.call_count, 2)
        self.policy.assert_called()

    def test_main_advanced_is_refused(self):
        self.authorize()  # main moving is irrelevant to a preview; the pull request is rechecked instead
        self.current.assert_not_called()

    def test_wrong_caller_workflow_is_refused(self):
        self.validate.side_effect = ValueError('Preview requires its trusted-main controller')
        self.refused('trusted-main controller')

    def test_head_that_is_not_the_run_source_is_refused(self):
        self.state['head'] = 'd' * 40
        self.refused('checked-out head')

    def test_a_plan_for_another_head_project_or_action_is_refused(self):
        for key, value in [('head', 'e' * 40), ('action', 'down')]:
            with self.subTest(key=key):
                self.plan = copy.deepcopy(PLAN) | {key: value}
                self.refused('authorized request')
        self.plan = copy.deepcopy(PLAN); self.plan['policy']['project'] = 'other'
        self.refused('authorized request')

    def test_only_a_manual_dispatch_is_a_preview(self):
        self.environment['GITHUB_EVENT_NAME'] = 'push'
        self.refused('manual dispatch')

    def test_a_missing_controller_checkout_is_refused(self):
        self.args.controller_root = None
        self.refused('controller checkout')


class PreviewPublish(PreviewMixin, Publish):
    def test_head_that_is_not_main_source_publishes_nothing(self):
        self.state['head'] = 'd' * 40
        with self.assertRaisesRegex(ValueError, 'checked-out head'): self.run_publish()
        self.publish.assert_not_called()

    def test_publishes_after_and_before_rechecking_the_pull_request(self):
        self.run_publish()
        self.assertEqual(self.current.call_count, 2)
        self.publish.assert_called_once()

    def test_a_stale_pull_request_publishes_nothing(self):
        self.current.side_effect = ValueError('stale')
        with self.assertRaisesRegex(ValueError, 'stale'): self.run_publish()
        self.publish.assert_not_called()


if __name__ == '__main__': unittest.main()
