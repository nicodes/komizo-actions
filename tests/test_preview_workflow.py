"""The manual single-job preview keeps every effect ordered and every gate local."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / '.github/workflows/preview.yml').read_text()
WORKFLOW = yaml.safe_load(TEXT)
JOB = WORKFLOW['jobs']['preview']
STEPS = JOB['steps']
NAMES = [step['name'] for step in STEPS]
UP = "steps.plan.outputs.action == 'up'"
EVERY_STEP_KEY = ['plan', 'checkout', 'setup', 'connect', 'prepare', 'build', 'test', 'publish', 'download', 'deploy',
                  'readiness', 'report', 'finish', 'notify']


def index(name):
    return NAMES.index(name)


def using(path):
    return [i for i, step in enumerate(STEPS) if str(step.get('uses', '')).endswith(path)]


def running(text):
    return [i for i, step in enumerate(STEPS) if text in step.get('run', '')]


class PreviewWorkflow(unittest.TestCase):
    def test_one_named_job_with_its_least_authority(self):
        self.assertEqual(list(WORKFLOW['jobs']), ['preview'])
        self.assertEqual((WORKFLOW['name'], JOB['name']), ('Preview', 'Preview'))
        self.assertEqual(set(WORKFLOW['on']), {'workflow_call'})
        self.assertEqual(WORKFLOW['permissions'], {})
        self.assertEqual(JOB['permissions'], {'contents': 'read', 'actions': 'read', 'pull-requests': 'write',
                                              'packages': 'write', 'attestations': 'read', 'id-token': 'write'})
        self.assertNotIn('issues', JOB['permissions'])
        self.assertEqual(JOB['environment'], 'Preview')
        self.assertEqual(JOB['runs-on'], '${{ inputs.runner }}')
        self.assertEqual(JOB['timeout-minutes'], '${{ inputs.timeout-minutes }}')
        self.assertEqual(WORKFLOW['concurrency'], {'group': 'preview-${{ github.event.inputs.pr }}', 'cancel-in-progress': False})

    def test_interface(self):
        call = WORKFLOW['on']['workflow_call']
        self.assertEqual(set(call['inputs']), {'cookbook-ref', 'komizo-ref', 'declaration', 'runner', 'timeout-minutes',
                                               'deploy-timeout-minutes', 'step-timeouts', 'notify', 'preview-clerk-key'})
        self.assertIs(call['inputs']['notify']['default'], True)
        self.assertEqual(call['inputs']['preview-clerk-key']['default'], '')
        self.assertNotIn('ttl', TEXT.lower().replace('ttl_seconds', ''))
        self.assertEqual(set(call['secrets']), {'KOMIZO_DEPLOY_KEY', 'DISCORD_WEBHOOK_URL'})
        self.assertTrue(call['secrets']['KOMIZO_DEPLOY_KEY']['required'])
        self.assertFalse(call['secrets']['DISCORD_WEBHOOK_URL']['required'])
        outputs = {'action', 'preview-url', 'api-url', 'release-digest'}
        self.assertEqual(set(call['outputs']), outputs); self.assertEqual(set(JOB['outputs']), outputs)

    def test_every_step_is_named_and_every_checkout_is_shallow_without_credentials(self):
        self.assertTrue(all(NAMES)); self.assertEqual(len(NAMES), len(set(NAMES)))
        checkouts = [step for step in STEPS if str(step.get('uses', '')).startswith('actions/checkout@')]
        self.assertEqual(len(checkouts), 4)
        for step in checkouts:
            self.assertEqual(step['with']['fetch-depth'], 1)
            self.assertIs(step['with']['persist-credentials'], False)

    def test_step_deadlines_come_from_the_json_with_fallbacks(self):
        for step in STEPS:
            deadline = step.get('timeout-minutes')
            if isinstance(deadline, str):
                self.assertRegex(deadline, r'^\$\{\{ fromJSON\(inputs\.step-timeouts\)\.[a-z]+ \|\| [0-9]+ \}\}$')

    def test_source_is_the_requested_head_for_up_and_main_for_down(self):
        source = STEPS[index('Check out source')]
        self.assertEqual(source['with']['ref'], '${{ steps.head.outputs.sha || github.sha }}')
        head = STEPS[index('Resolve the requested pull request head')]
        self.assertEqual(head['if'], "github.event.inputs.action == 'up'")
        self.assertLess(index('Resolve the requested pull request head'), index('Check out source'))
        controller = STEPS[index('Check out the trusted main controller')]
        self.assertEqual((controller['with']['ref'], controller['with']['path']), ('${{ github.sha }}', '_preview_controller'))
        self.assertLess(index('Check out source'), index('Check out the trusted main controller'))
        self.assertLess(index('Check out the trusted main controller'), index('Plan the preview request from trusted main'))

    def test_dispatch_inputs_are_checked_before_any_use(self):
        guard = STEPS[0]['run']
        for text in ['workflow_dispatch', 'refs/heads/main', 'PREVIEW_ACTION" == up', 'PREVIEW_ACTION" == down', 'PREVIEW_PR" =~ ^[1-9]']:
            self.assertIn(text, guard)
        self.assertNotIn('github.event.inputs', ''.join(step.get('run', '') for step in STEPS))

    def test_up_builds_then_tests_then_publishes_then_authorizes_then_applies(self):
        order = ['Prepare recipe sources and dependencies', 'Build', 'Test', 'Publish', 'Authorize',
                 'Bound the preview operation to its remaining phase budget', 'Apply the exact preview operation',
                 'Verify exact source readiness on verified routes', 'Comment the preview lifecycle on the pull request']
        self.assertEqual([index(name) for name in order], sorted(index(name) for name in order))
        publish, authorize = running('deliver.py publish'), running('deliver.py authorize')
        self.assertEqual((publish, authorize), ([index('Publish')], [index('Authorize')]))
        for step in publish + authorize:
            self.assertIn('--preview', STEPS[step]['run']); self.assertIn('--controller-root _preview_controller', STEPS[step]['run'])
        self.assertEqual(using('_cookbook/build'), [index('Build')]); self.assertEqual(using('_cookbook/test'), [index('Test')])

    def test_the_host_key_never_exists_while_pull_request_code_runs(self):
        connect = using('_preview_actions/connect')
        self.assertEqual(connect, [index('Prepare host connection')])
        self.assertGreater(connect[0], index('Test'))
        routes = STEPS[index('Resolve public routes from protected host policy')]
        self.assertLess(index('Resolve public routes from protected host policy'), index('Prepare recipe sources and dependencies'))
        self.assertIn('trap cleanup EXIT', routes['run']); self.assertIn('cleanup.cjs', routes['run'])
        self.assertLess(routes['run'].index('connect.cjs'), routes['run'].index('preview.py routes'))
        carriers = [i for i, step in enumerate(STEPS) if 'KOMIZO_DEPLOY_KEY' in str(step.get('env', ''))]
        self.assertEqual(carriers, [index('Resolve public routes from protected host policy'), index('Prepare host connection')])
        self.assertLess(index('Record the planned origins for the build'), index('Prepare recipe sources and dependencies'))
        self.assertLess(index('Resolve public routes from protected host policy'), index('Record the planned origins for the build'))

    def test_the_routes_step_fails_closed_and_the_key_never_spans_a_build_step(self):
        run = STEPS[index('Resolve public routes from protected host policy')]['run']
        for text in ['trap cleanup EXIT', 'cleanup.cjs || status=1', 'rm -rf "$directory" || status=1',
                     '[[ ! -e "$directory" ]] || status=1', 'exit "$status"']:
            self.assertIn(text, run)
        # Every connect either closes inside its own step or is the final post-test connect action.
        opens = [i for i, step in enumerate(STEPS) if 'connect.cjs' in step.get('run', '') or str(step.get('uses', '')).endswith('/connect')]
        closes = [i for i, step in enumerate(STEPS) if 'cleanup.cjs' in step.get('run', '')]
        build = [i for i, step in enumerate(STEPS) if any(str(step.get('uses', '')).endswith(f'_cookbook/{name}') for name in ['prepare', 'build', 'test'])]
        self.assertEqual(opens, [index('Resolve public routes from protected host policy'), index('Prepare host connection')])
        self.assertEqual(closes, [opens[0]])
        self.assertFalse([i for i in build if opens[0] <= i <= closes[0]])
        self.assertTrue(all(i < opens[1] for i in build))

    def test_the_build_and_publish_steps_run_only_for_up(self):
        for name in ['Resolve public routes from protected host policy', 'Record the planned origins for the build',
                     'Prepare recipe sources and dependencies', 'Build', 'Test', 'Publish', 'Authorize',
                     'Verify exact source readiness on verified routes', 'Require the checked-out source to be the planned head']:
            self.assertEqual(STEPS[index(name)]['if'], UP, name)
        self.assertEqual(STEPS[index('Finish')]['if'], 'always() && ' + UP)
        self.assertEqual(STEPS[index('Announce successful Preview')]['if'], 'inputs.notify && ' + UP)
        self.assertTrue(STEPS[index('Announce successful Preview')]['continue-on-error'])

    def test_down_applies_removal_and_comments_without_building(self):
        for name in ['Plan the preview request from trusted main', 'Revalidate the plan before host access',
                     'Prepare host connection', 'Apply the exact preview operation', 'Comment the preview lifecycle on the pull request']:
            self.assertNotIn('if', STEPS[index(name)], name)
        self.assertIn('preview.py apply', STEPS[index('Apply the exact preview operation')]['run'])
        self.assertIn('preview.py report', STEPS[index('Comment the preview lifecycle on the pull request')]['run'])

    def test_registry_authority_reaches_only_publication_and_the_up_apply(self):
        apply = STEPS[index('Apply the exact preview operation')]['env']
        self.assertEqual(apply['KOMIZO_REGISTRY_TOKEN'], "${{ steps.plan.outputs.action == 'up' && github.token || '' }}")
        carriers = [step['name'] for step in STEPS if 'KOMIZO_REGISTRY_TOKEN' in step.get('env', {})]
        self.assertEqual(carriers, ['Publish', 'Apply the exact preview operation'])

    def test_the_effects_budget_is_separate_from_the_whole_job(self):
        start = STEPS[index('Start preview effect budget')]; bound = STEPS[index('Bound the preview operation to its remaining phase budget')]
        self.assertEqual(start['env']['TOTAL_MINUTES'], '${{ inputs.deploy-timeout-minutes }}')
        self.assertEqual(bound['env']['TOTAL_MINUTES'], '${{ inputs.deploy-timeout-minutes }}')
        self.assertIn('--from-now', start['run']); self.assertIn('--phase-seconds', bound['run'])
        self.assertLess(index('Test'), index('Start preview effect budget'))

    def test_the_cookbook_receives_the_preview_clerk_key_and_an_unprepared_checkout(self):
        prepare = STEPS[index('Prepare recipe sources and dependencies')]['with']
        self.assertEqual(prepare['checkout'], 'false'); self.assertEqual(prepare['source'], '${{ steps.plan.outputs.head }}')
        self.assertIn('inputs.preview-clerk-key', prepare['preview-clerk-key'])

    def test_a_failed_effect_or_report_is_not_reported_as_an_issue(self):
        self.assertNotIn('report-failure', TEXT)
        self.assertNotIn('issues', JOB['permissions'])


if __name__ == '__main__':
    unittest.main()
