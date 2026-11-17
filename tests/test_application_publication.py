import hashlib
import importlib.util
import contextlib
import copy
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('application_publication', Path(__file__).resolve().parents[1] / 'scripts/application-publication.py')
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class ApplicationPublication(unittest.TestCase):
    def setUp(self):
        self.environment = dict(GITHUB_SERVER_URL='https://github.com', GITHUB_REPOSITORY='owner/demo',
                                GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2', GITHUB_SHA='a' * 40,
                                GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/build-and-test.yml@refs/heads/main',
                                GITHUB_WORKFLOW_SHA='a' * 40, GITHUB_EVENT_NAME='workflow_dispatch')
        self.proof = dict(version=1, source_commit='a' * 40, source_tree='b' * 40, cookbook_commit='c' * 40,
                          recipe='react-native', status='passed', tracked_source_clean=True, build_verified=True,
                          tests_verified=True, test_groups=['lint', 'unit', 'security', 'e2e'],
                          execution_context=self.environment | {'GITHUB_JOB': 'validate'})
        self.config = dict(recipe='react-native', ci={'runner': 'ubuntu-24.04'})
        self.jobs = dict(total_count=1, jobs=[dict(name='validate / Full source checks / Test',
                           conclusion='success', head_sha='a' * 40, labels=['ubuntu-24.04'])])

    def accept(self):
        publication.accept(self.proof, self.config, 'a' * 40, 'c' * 40, 'b' * 40,
                           ['lint', 'unit', 'security', 'e2e'], self.jobs, self.environment, job_source='a'*40)

    def test_current_complete_same_run_host_is_admitted(self):
        self.accept()

    def test_retired_producer_job_name_cannot_authorize_publication(self):
        self.jobs['jobs'][0]['name'] = 'validate / Full source checks / Tests'
        with self.assertRaisesRegex(ValueError, 'actual full validation job'):
            self.accept()

    def main_at_publication_boundary(self, packet_source, *, event='push'):
        """Run the entry point through its real acceptance and final source check."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cookbook = root / 'cookbook'; cookbook.mkdir()
            helpers = root / 'tools'; (helpers / 'helpers').mkdir(parents=True)
            (helpers / 'helpers/verification.py').write_text(
                'def release_definition(path):\n'
                "    return {'project':'demo','tests':['lint','unit','security','e2e']}\n")
            recipe = b'version = 1\n'
            (root / 'cookbook.toml').write_bytes(recipe)
            environment = self.environment | dict(GITHUB_EVENT_NAME=event, GITHUB_REF='refs/heads/main',
                                                   GITHUB_REPOSITORY_ID='1234')
            proof = copy.deepcopy(self.proof)
            proof['execution_context'] = environment | {'GITHUB_JOB': 'validate'}
            proof['configuration_sha256'] = hashlib.sha256(recipe).hexdigest()
            context = SimpleNamespace(cookbook=cookbook, tools=helpers, cookbook_revision='c'*40,
                                      tools_revision='d'*40, require_proof=Mock())
            packet = SimpleNamespace(descriptor={'source': packet_source})
            receipts = ({'build': 'accepted'}, {'tests': 'accepted'})
            published_environments = []
            def publish_step(packet, helpers, environment):
                published_environments.append(dict(environment))
                return {'web': 'accepted-reference'}
            publish = Mock(side_effect=publish_step)
            evidence = Mock()
            errors = io.StringIO()
            argv = ['application-publication.py', '--source', 'a'*40, '--project', 'demo',
                    '--cookbook-root', str(cookbook), '--cookbook-ref', 'c'*40,
                    '--tools-root', str(helpers)]
            with contextlib.ExitStack() as stack:
                stack.enter_context(contextlib.chdir(root))
                stack.enter_context(contextlib.redirect_stderr(errors))
                stack.enter_context(patch.dict(os.environ, environment, clear=True))
                stack.enter_context(patch.object(sys, 'argv', argv))
                stack.enter_context(patch.object(publication, 'git', side_effect=lambda *args:
                    'b'*40 if args == ('rev-parse', 'HEAD^{tree}') else 'a'*40))
                stack.enter_context(patch.object(publication.subprocess, 'run', return_value=SimpleNamespace(returncode=0)))
                stack.enter_context(patch.object(publication.recipe_context, 'accepted_context', return_value=context))
                stack.enter_context(patch.object(publication.recipe_data, 'load', return_value=self.config))
                stack.enter_context(patch.object(publication.recipe_data, 'effective_definition', return_value='release.toml'))
                stack.enter_context(patch.object(publication, 'read', return_value=proof))
                stack.enter_context(patch.object(publication.workflow_run, 'current', return_value=({}, self.jobs, 'a'*40)))
                stack.enter_context(patch.object(publication.publication, 'admit_receipts', return_value=receipts))
                stack.enter_context(patch.object(publication.publication, 'stage', return_value=packet))
                stack.enter_context(patch.object(publication.publication, 'publish', publish))
                stack.enter_context(patch.object(publication.publication, 'write_evidence', evidence))
                try:
                    publication.main()
                except SystemExit as error:
                    self.assertEqual(error.code, 1)
                    self.assertIn('accepted release differs from its exact workflow source', errors.getvalue())
                    publish.assert_not_called()
                    evidence.assert_not_called()
                    return False
            context.require_proof.assert_called_once_with(proof)
            publish.assert_called_once()
            self.assertIs(publish.call_args.args[0], packet)
            self.assertEqual(publish.call_args.args[1], helpers)
            self.assertEqual(published_environments, [environment | dict(
                COOKBOOK_ROOT=str(cookbook), COOKBOOK_REF='c'*40, TOOLS_ROOT=str(helpers), TOOLS_REF='d'*40)])
            evidence.assert_called_once()
            self.assertIs(evidence.call_args.kwargs['receipts'], receipts)
            return True

    def test_main_publishes_accepted_exact_push_and_manual_source(self):
        source = {'repository': 'owner/demo', 'repository_id': '1234', 'revision': 'a'*40}
        for event in ['push', 'workflow_dispatch']:
            with self.subTest(event=event):
                self.assertTrue(self.main_at_publication_boundary(source, event=event))

    def test_main_refuses_foreign_packet_source_before_any_publication(self):
        source = {'repository': 'owner/demo', 'repository_id': '1234', 'revision': 'a'*40}
        for key, value in [('repository', 'foreign/demo'), ('repository_id', '9999'), ('revision', 'e'*40)]:
            with self.subTest(key=key):
                self.assertFalse(self.main_at_publication_boundary(source | {key: value}))

    def test_pr_target_jobs_are_the_selected_head_while_proof_authority_remains_main(self):
        self.environment.update(GITHUB_EVENT_NAME='pull_request_target',GITHUB_SHA='d'*40,
            GITHUB_WORKFLOW_SHA='d'*40,GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/pr-preview.yml@refs/heads/main')
        self.proof['execution_context']=self.environment|{'GITHUB_JOB':'validate'}
        self.accept()
        self.assertEqual(self.proof['source_commit'],'a'*40)
        self.assertEqual(self.proof['execution_context']['GITHUB_SHA'],'d'*40)
        self.jobs['jobs'][0]['head_sha']='d'*40
        with self.assertRaises(ValueError):self.accept()

    def test_comment_preview_keeps_main_job_source_even_when_product_head_differs(self):
        self.environment.update(GITHUB_EVENT_NAME='issue_comment',GITHUB_SHA='d'*40,
            GITHUB_WORKFLOW_SHA='d'*40,GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/pr-preview.yml@refs/heads/main')
        self.proof['execution_context']=self.environment|{'GITHUB_JOB':'validate'}
        self.jobs['jobs'][0]['head_sha']='d'*40
        publication.accept(self.proof,self.config,'a'*40,'c'*40,'b'*40,
            ['lint','unit','security','e2e'],self.jobs,self.environment,job_source='d'*40)
        with self.assertRaises(ValueError):self.accept()

    def test_preview_acceptance_cannot_publish_as_production_even_at_the_same_source_revision(self):
        self.environment.update(GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/deploy.yml@refs/heads/main',
            GITHUB_EVENT_NAME='push')
        self.proof['execution_context'].update(
            GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/pr-preview.yml@refs/heads/main',
            GITHUB_EVENT_NAME='issue_comment')
        with self.assertRaisesRegex(ValueError,'another workflow execution'):
            self.accept()

    def test_old_attempt_partial_or_failed_host_is_refused(self):
        for key, value in [('source_commit', 'd' * 40), ('source_tree', 'd' * 40),
                           ('cookbook_commit', 'd' * 40), ('test_groups', ['lint', 'unit']),
                           ('build_verified', False), ('tracked_source_clean', False)]:
            original = self.proof[key]
            self.proof[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.accept()
            self.proof[key] = original
        self.proof['execution_context']['GITHUB_RUN_ATTEMPT'] = '1'
        with self.assertRaisesRegex(ValueError, 'another workflow'):
            self.accept()
        self.proof['execution_context']['GITHUB_RUN_ATTEMPT'] = '2'
        self.jobs['jobs'][0]['conclusion'] = 'failure'
        with self.assertRaisesRegex(ValueError, 'succeeded'):
            self.accept()

    def test_wrong_runner_or_ambiguous_jobs_are_refused(self):
        self.jobs['jobs'][0]['labels'] = ['other-runner']
        with self.assertRaisesRegex(ValueError, 'another declared runner'):
            self.accept()
        self.jobs['jobs'][0]['labels'] = ['ubuntu-24.04']
        self.jobs['total_count'] = 2
        with self.assertRaisesRegex(ValueError, 'complete current-attempt'):
            self.accept()

    def test_only_closed_hash_bound_native_packet_is_staged_without_execution(self):
        from test_release_packet import make_packet
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); incoming = root / 'incoming/.artifacts/release'; incoming.mkdir(parents=True)
            make_packet(incoming)
            proof = {'artifacts': {'.artifacts/release/' + name: hashlib.sha256((incoming / name).read_bytes()).hexdigest()
                                   for name in ['release.json', 'inventory.json', 'static/web.tar.gz']}}
            publication.stage(root / 'incoming', proof, root / 'accepted')
            self.assertEqual((root / 'accepted/release.json').read_bytes(), (incoming / 'release.json').read_bytes())
            (incoming / 'static/web.tar.gz').write_bytes(b'changed')
            with self.assertRaises(ValueError): publication.stage(root / 'incoming', proof, root / 'refused')
            self.assertFalse((root / 'refused').exists())

    def test_unaccepted_extra_metadata_or_programs_are_refused_before_staging(self):
        from test_release_packet import make_packet
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); incoming = root / 'incoming/.artifacts/release'; incoming.mkdir(parents=True)
            make_packet(incoming)
            proof = {'artifacts': {'.artifacts/release/' + name: hashlib.sha256((incoming / name).read_bytes()).hexdigest()
                                   for name in ['release.json', 'inventory.json', 'static/web.tar.gz']}}
            (incoming / 'untrusted.py').write_text('raise SystemExit("must never execute")')
            with self.assertRaises(ValueError): publication.stage(root / 'incoming', proof, root / 'refused')
            self.assertFalse((root / 'refused').exists())


if __name__ == '__main__': unittest.main()
