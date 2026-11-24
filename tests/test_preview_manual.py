"""A manual preview request is read from the dispatch payload and refused loudly when it is not eligible."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import submission


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


request = load('manual_preview_request', 'preview-request/request.py')
preview = load('manual_preview', 'scripts/preview.py')

REPO = 'owner/demo'
HEAD, BASE, TREE = 'a' * 40, 'b' * 40, 'c' * 40
CONTROLLER = REPO + '/.github/workflows/pr-preview.yml@refs/heads/main'


def pull(**overrides):
    value = {'number': 42, 'state': 'open', 'draft': False, 'author_association': 'MEMBER',
             'user': {'login': 'someone'}, 'labels': [],
             'head': {'sha': HEAD, 'ref': 'feature', 'repo': {'full_name': REPO}},
             'base': {'sha': BASE, 'ref': 'main', 'repo': {'full_name': REPO}}}
    return value | overrides


def dispatch(pr='42', action='up'):
    return {'inputs': {'pr': pr, 'action': action}}


def resolve(event, pr=None):
    return request.resolve(event, 'workflow_dispatch', REPO, 'preview', lambda *_: pr or pull())


class Resolve(unittest.TestCase):
    def test_up_and_down_read_the_dispatch_inputs(self):
        up = resolve(dispatch())
        self.assertEqual((up['action'], up['pr'], up['sha'], up['head_ref']), ('up', '42', HEAD, 'feature'))
        self.assertEqual(resolve(dispatch(action='down'))['action'], 'down')

    def test_the_pull_request_is_fetched_by_the_requested_number(self):
        seen = []
        request.resolve(dispatch(pr='1234'), 'workflow_dispatch', REPO, 'preview', lambda repo, number: seen.append((repo, number)) or pull(number=1234))
        self.assertEqual(seen, [(REPO, 1234)])

    def test_a_bad_action_is_an_error_not_a_quiet_no_op(self):
        for action in ['', 'UP', 'restart', None, 'up down']:
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'action'):
                resolve(dispatch(action=action))
        with self.assertRaisesRegex(ValueError, 'action'):
            resolve({'inputs': {'pr': '42'}})
        with self.assertRaisesRegex(ValueError, 'action'):
            resolve({})

    def test_a_non_numeric_pull_request_number_is_an_error(self):
        for number in ['abc', '', '0', '-1', '4 2', '42\n', '4.2', '99999999999', 42, None]:
            with self.subTest(pr=number), self.assertRaisesRegex(ValueError, 'pr input'):
                resolve(dispatch(pr=number))

    def test_a_closed_pull_request_cannot_come_up_but_can_come_down(self):
        with self.assertRaisesRegex(request.Refused, 'not open'):
            resolve(dispatch(), pull(state='closed'))
        self.assertEqual(resolve(dispatch(action='down'), pull(state='closed'))['action'], 'down')

    def test_a_fork_head_is_refused_both_ways(self):
        fork = pull(head={'sha': HEAD, 'ref': 'feature', 'repo': {'full_name': 'someone/demo'}})
        for action in ['up', 'down']:
            with self.subTest(action=action), self.assertRaisesRegex(request.Refused, 'not in this repository'):
                resolve(dispatch(action=action), fork)
        with self.assertRaisesRegex(request.Refused, 'not in this repository'):
            resolve(dispatch(), pull(head={'sha': HEAD, 'ref': 'feature', 'repo': None}))

    def test_the_existing_eligibility_checks_still_apply(self):
        with self.assertRaisesRegex(request.Refused, 'author'):
            resolve(dispatch(), pull(author_association='CONTRIBUTOR'))
        with self.assertRaisesRegex(request.Refused, 'draft'):
            resolve(dispatch(), pull(draft=True))
        for action in ['up', 'down']:
            with self.assertRaisesRegex(request.Refused, 'dependabot'):
                resolve(dispatch(action=action), pull(user={'login': 'dependabot[bot]'}))

    def test_a_label_is_not_required(self):
        self.assertEqual(resolve(dispatch(), pull(labels=[]))['action'], 'up')

    def test_other_events_keep_their_decisions(self):
        with self.assertRaises(request.Refused):
            request.resolve({}, 'push', REPO, 'preview')


class Controller(unittest.TestCase):
    def setUp(self):
        self.environment = {'GITHUB_REPOSITORY': REPO, 'GITHUB_SHA': BASE, 'GITHUB_REF': 'refs/heads/main',
                            'GITHUB_WORKFLOW_SHA': BASE, 'GITHUB_WORKFLOW_REF': CONTROLLER,
                            'GITHUB_EVENT_NAME': 'workflow_dispatch'}

    def test_a_manual_dispatch_from_the_trusted_main_controller_is_accepted(self):
        self.assertEqual(preview.controller(self.environment), (REPO, BASE))

    def test_the_event_driven_controller_is_unchanged(self):
        for event in ['pull_request_target', 'issue_comment']:
            self.assertEqual(preview.controller(self.environment | {'GITHUB_EVENT_NAME': event}), (REPO, BASE))
        for event in ['push', 'schedule', 'pull_request', '']:
            with self.assertRaises(ValueError):
                preview.controller(self.environment | {'GITHUB_EVENT_NAME': event})

    def test_another_workflow_branch_or_source_is_refused(self):
        for change in [{'GITHUB_WORKFLOW_REF': REPO + '/.github/workflows/deploy.yml@refs/heads/main'},
                       {'GITHUB_WORKFLOW_REF': CONTROLLER.replace('refs/heads/main', 'refs/heads/topic')},
                       {'GITHUB_REF': 'refs/heads/topic'}, {'GITHUB_WORKFLOW_SHA': HEAD}, {'GITHUB_SHA': 'short'}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                preview.controller(self.environment | change)


class Plan(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {'GITHUB_REPOSITORY': REPO, 'GITHUB_REPOSITORY_ID': '9', 'GITHUB_SHA': BASE,
                            'GITHUB_REF': 'refs/heads/main', 'GITHUB_WORKFLOW_SHA': BASE, 'GITHUB_WORKFLOW_REF': CONTROLLER,
                            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SERVER_URL': 'https://github.com',
                            'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '1'}
        self.event = dispatch(); self.pull = pull()
        self.policy = {'definition': 'deploy/preview.toml', 'project': 'demo', 'backend': True,
                       'test_groups': ['lint', 'unit', 'security'], 'ttl_seconds': 3600}

    def lookup(self, *_):
        return copy.deepcopy(self.pull)

    def patches(self):
        return (patch.object(preview, 'git', side_effect=lambda root, *a: TREE if a == ('rev-parse', 'HEAD^{tree}') else BASE),
                patch.object(preview, 'native_policy', return_value=self.policy),
                patch.object(preview, 'read_event', side_effect=lambda _: copy.deepcopy(self.event)))

    def planned(self):
        a, b, c = self.patches()
        with a, b, c:
            return preview.request_plan(self.root, self.root, 'deploy/preview.toml', self.environment, self.lookup)

    def validated(self, raw):
        a, b, c = self.patches()
        with a, b, c:
            return preview.validate_plan(raw, self.root, self.root, self.environment, self.lookup)

    def test_up_plans_the_current_head_and_main_base_without_a_label(self):
        result = self.planned()
        plan = json.loads(result['plan'])
        self.assertEqual((result['enabled'], result['action'], result['pr'], result['head'], result['base']),
                         ('true', 'up', '42', HEAD, BASE))
        self.assertEqual((plan['trigger'], plan['action'], plan['controller'], plan['policy']), ('workflow_dispatch', 'up', BASE, self.policy))
        self.assertEqual(self.validated(result['plan']), plan)

    def test_down_plans_without_the_pull_request_being_open(self):
        self.event = dispatch(action='down'); self.pull = pull(state='closed')
        result = self.planned()
        self.assertEqual((result['action'], result['head']), ('down', HEAD))
        self.assertEqual(self.validated(result['plan'])['action'], 'down')

    def test_every_refusal_fails_the_plan_with_its_reason(self):
        cases = [(dispatch(), pull(state='closed'), 'not open'),
                 (dispatch(), pull(head={'sha': HEAD, 'ref': 'f', 'repo': {'full_name': 'x/demo'}}), 'not in this repository'),
                 (dispatch(action='sideways'), pull(), 'action'), (dispatch(pr='forty-two'), pull(), 'pr input')]
        for event, request_value, reason in cases:
            with self.subTest(reason=reason):
                self.event, self.pull = event, request_value
                with self.assertRaisesRegex(ValueError, reason):
                    self.planned()

    def test_a_pull_request_whose_base_is_not_this_main_is_stale(self):
        self.pull = pull(base={'sha': 'd' * 40, 'ref': 'main', 'repo': {'full_name': REPO}})
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.planned()

    def test_a_plan_is_bound_to_its_dispatch_inputs_and_current_pull_request(self):
        raw = self.planned()['plan']
        self.event = dispatch(pr='43'); self.pull = pull(number=43)
        with self.assertRaisesRegex(ValueError, 'authorized event'):
            self.validated(raw)
        self.event = dispatch(); self.pull = pull(state='closed')
        with self.assertRaisesRegex(ValueError, 'not open|ineligible'):
            self.validated(raw)
        self.pull = pull(head={'sha': 'e' * 40, 'ref': 'feature', 'repo': {'full_name': REPO}})
        with self.assertRaisesRegex(ValueError, 'relabeled|stale'):
            self.validated(raw)


class Origins(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.env = Path(temporary.name) / 'env'
        self.plan = {'action': 'up', 'trigger': 'workflow_dispatch', 'pr': 42, 'head': HEAD, 'policy': {'backend': True}}

    def build(self, app='https://pr-42.example.dev', api='https://pr-42-api.example.dev', timeout='30', **changes):
        preview.build_environment(self.plan | changes, app, api, timeout, {'GITHUB_ENV': str(self.env)})

    def test_the_planned_origins_reach_the_build_environment(self):
        self.build()
        self.assertEqual(self.env.read_text().splitlines(), [
            'COOKBOOK_PREVIEW_NUMBER=42', 'COOKBOOK_PREVIEW_APP_ORIGIN=https://pr-42.example.dev',
            'COOKBOOK_CANDIDATE_COMMIT=' + HEAD, 'COOKBOOK_VALIDATION_TIMEOUT=30',
            'COOKBOOK_PREVIEW_API_ORIGIN=https://pr-42-api.example.dev'])

    def test_a_static_preview_has_no_api_origin(self):
        self.build(api='', policy={'backend': False})
        self.assertNotIn('API_ORIGIN', self.env.read_text())

    def test_origins_of_another_instance_or_shape_are_refused(self):
        for app, api in [('https://pr-43.example.dev', 'https://pr-43-api.example.dev'), ('http://pr-42.example.dev', 'https://pr-42-api.example.dev'),
                         ('https://pr-42.example.dev\nX=1', 'https://pr-42-api.example.dev'), ('https://pr-42.example.dev', '')]:
            with self.subTest(app=app), self.assertRaises(ValueError):
                self.build(app, api)
        self.assertFalse(self.env.exists())

    def test_only_a_manual_up_and_a_bounded_deadline_are_accepted(self):
        for changes in [{'trigger': 'issue_comment'}, {'action': 'down'}]:
            with self.assertRaises(ValueError):
                self.build(**changes)
        for timeout in ['', '0', '1000', '3x', '30\nX=1']:
            with self.assertRaises(ValueError):
                self.build(timeout=timeout)


class Report(unittest.TestCase):
    def run_report(self, action, trigger, existing=()):
        calls = []
        def lookup(path, method='GET', body=None):
            calls.append((method, path, body))
            return list(existing) if method == 'GET' else None
        plan = {'repository': REPO, 'pr': 42, 'head': HEAD, 'action': action, 'trigger': trigger}
        urls = {'app': {'url': 'https://pr-42.example.dev'}} if action == 'up' else {}
        preview.report(plan, urls, lookup)
        return calls

    def test_a_manual_preview_comments_without_the_redeploy_label(self):
        calls = self.run_report('up', 'workflow_dispatch')
        self.assertFalse([call for call in calls if '/labels' in call[1]])
        posted = [call for call in calls if call[0] == 'POST']
        self.assertEqual(len(posted), 1)
        self.assertIn('https://pr-42.example.dev', posted[0][2]['body'])
        self.assertIn('<!-- preview -->', posted[0][2]['body'])

    def test_a_manual_teardown_comments_even_without_an_earlier_comment(self):
        calls = self.run_report('down', 'workflow_dispatch')
        self.assertFalse([call for call in calls if '/labels' in call[1]])
        self.assertIn('torn down', [call for call in calls if call[0] == 'POST'][0][2]['body'])

    def test_a_manual_comment_replaces_the_sticky_one(self):
        sticky = {'user': {'login': 'github-actions[bot]'}, 'body': '<!-- preview --> old', 'id': 7}
        calls = self.run_report('up', 'workflow_dispatch', [sticky])
        self.assertEqual([call[0] for call in calls if call[0] != 'GET'], ['PATCH'])

    def test_the_event_driven_report_keeps_its_label_lifecycle(self):
        calls = self.run_report('up', 'issue_comment')
        self.assertIn(('POST', f'repos/{REPO}/issues/42/labels', {'labels': ['preview']}), calls)
        self.assertTrue([call for call in self.run_report('down', 'issue_comment') if call[0] == 'DELETE'])


def environment(**changes):
    return {'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_REPOSITORY': REPO, 'GITHUB_REPOSITORY_ID': '9',
            'GITHUB_SHA': BASE, 'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_WORKFLOW_REF': CONTROLLER,
            'GITHUB_WORKFLOW_SHA': BASE} | changes


class Submission(unittest.TestCase):
    def test_the_preview_controller_may_submit_when_dispatched_by_hand(self):
        submission.workflow(environment())

    def test_a_dispatched_preview_controller_must_be_its_exact_source(self):
        with self.assertRaisesRegex(ValueError, 'exact trusted main controller'):
            submission.workflow(environment(GITHUB_WORKFLOW_SHA=HEAD))

    def test_other_dispatched_workflows_keep_their_rules(self):
        other = REPO + '/.github/workflows/deploy.yml@refs/heads/main'
        submission.workflow(environment(GITHUB_WORKFLOW_REF=other, GITHUB_WORKFLOW_SHA=HEAD))
        with self.assertRaises(ValueError):
            submission.workflow(environment(GITHUB_REF='refs/heads/topic'))

    def test_the_event_driven_controller_is_unchanged(self):
        for event in ['pull_request_target', 'issue_comment']:
            submission.workflow(environment(GITHUB_EVENT_NAME=event))
            with self.assertRaises(ValueError):
                submission.workflow(environment(GITHUB_EVENT_NAME=event, GITHUB_WORKFLOW_REF=REPO + '/.github/workflows/deploy.yml@refs/heads/main'))


if __name__ == '__main__':
    unittest.main()
