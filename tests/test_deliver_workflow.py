"""The single-job delivery workflow keeps every effect ordered and every gate local."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / '.github/workflows/deliver.yml').read_text()
WORKFLOW = yaml.safe_load(TEXT)
JOB = WORKFLOW['jobs']['deliver']
STEPS = JOB['steps']
NAMES = [step['name'] for step in STEPS]


def index(name):
    return NAMES.index(name)


def using(path):
    return [i for i, step in enumerate(STEPS) if str(step.get('uses', '')).endswith(path)]


def running(text):
    return [i for i, step in enumerate(STEPS) if text in step.get('run', '')]


class DeliverWorkflow(unittest.TestCase):
    def test_one_named_job_with_the_union_of_authority(self):
        self.assertEqual(list(WORKFLOW['jobs']), ['deliver'])
        self.assertEqual(WORKFLOW['name'], 'Deliver'); self.assertEqual(JOB['name'], 'Deploy')
        self.assertEqual(WORKFLOW['permissions'], {})
        self.assertEqual(JOB['permissions'], {'contents': 'read', 'actions': 'read', 'pull-requests': 'read',
                                              'packages': 'write', 'attestations': 'read', 'id-token': 'write',
                                              'issues': 'write'})
        self.assertEqual(WORKFLOW['concurrency'], {'group': 'deploy-production', 'cancel-in-progress': False})
        self.assertEqual(JOB['environment'], '${{ inputs.environment }}')
        self.assertEqual(JOB['runs-on'], '${{ inputs.runner }}')
        self.assertEqual(JOB['timeout-minutes'], '${{ inputs.timeout-minutes }}')

    def test_interface(self):
        call = WORKFLOW['on']['workflow_call']
        self.assertEqual(set(call['inputs']), {'cookbook-ref', 'komizo-ref', 'project', 'declaration', 'runner',
            'timeout-minutes', 'deploy-timeout-minutes', 'environment', 'step-timeouts', 'deployment-url', 'notify', 'report', 'preview-clerk-key'})
        self.assertIs(call['inputs']['notify']['default'], True); self.assertIs(call['inputs']['report']['default'], True)
        self.assertEqual(call['inputs']['preview-clerk-key']['default'], '')
        self.assertTrue(call['secrets']['KOMIZO_DEPLOY_KEY']['required'])
        for name in ['BACKUP_S3_ACCESS_KEY', 'BACKUP_S3_SECRET_KEY', 'DISCORD_WEBHOOK_URL']:
            self.assertFalse(call['secrets'][name]['required'])
        self.assertEqual(set(call['outputs']), {'phase', 'release-digest', 'public-release-id'})
        self.assertEqual(set(JOB['outputs']), {'phase', 'release-digest', 'public-release-id'})

    def test_every_step_is_named_and_every_checkout_is_shallow(self):
        self.assertTrue(all(NAMES)); self.assertEqual(len(NAMES), len(set(NAMES)))
        checkouts = [step for step in STEPS if str(step.get('uses', '')).startswith('actions/checkout@')]
        self.assertEqual(len(checkouts), 3)
        for step in checkouts:
            self.assertEqual(step['with']['fetch-depth'], 1)
            self.assertIs(step['with']['persist-credentials'], False)

    def test_backup_precedes_publish_precedes_authorize_precedes_deploy(self):
        capture, upload = index('Capture encrypted backup'), index('Upload encrypted backup')
        publish, authorize, deploy = index('Publish'), index('Authorize'), index('Deploy')
        self.assertLess(capture, upload); self.assertLess(upload, publish)
        self.assertLess(publish, authorize); self.assertLess(authorize, deploy)
        self.assertEqual(running('deliver.py publish'), [publish]); self.assertEqual(running('deliver.py authorize'), [authorize])
        self.assertLess(index('Test'), index('Plan deployment')); self.assertLess(index('Plan deployment'), capture)
        self.assertLess(index('Start delivery budget'), index('Prepare host connection'))
        self.assertLess(index('Prepare host connection'), capture)
        self.assertEqual(STEPS[capture]['if'], "steps.plan.outputs.backup != ''")
        self.assertEqual(STEPS[capture]['with']['mode'], 'export'); self.assertEqual(STEPS[upload]['with']['mode'], 'upload')

    def test_source_gates_precede_everything_and_main_staleness_is_an_output_gate(self):
        windows = ['Bootstrap Python for protected SDK sources', 'Prepare the private artifact parent with actual pinned Python']
        self.assertEqual(NAMES[:7], ['Require exact protected main source', 'Check out source', 'Check out deployment actions',
                                     *windows, 'Check out Cookbook', 'Stop when main has advanced'])
        self.assertEqual(STEPS[index('Stop when main has advanced')]['id'], 'current')
        prepare = index('Prepare recipe sources and dependencies')
        self.assertEqual(NAMES[prepare:prepare + 3], ['Prepare recipe sources and dependencies', 'Build', 'Test'])
        self.assertEqual(STEPS[prepare]['with']['checkout'], 'false'); self.assertEqual(STEPS[prepare]['with']['advisories'], 'skip')
        self.assertEqual(STEPS[prepare]['with']['source'], '${{ github.sha }}')
        for path in ['/prepare', '/build', '/test', '/finish']:
            self.assertEqual(len(using('.artifacts/recipe-sdks/_cookbook' + path)), 1)
        self.assertIn('--from-now', STEPS[index('Start delivery budget')]['run'])

    def test_deadlines_are_on_workflow_steps_not_composites(self):
        for step in STEPS:
            if 'uses' in step:
                self.assertIn('timeout-minutes', step, step['name'])
        for name, key in [('Check out source', 'checkout'), ('Plan deployment', 'plan'), ('Publish', 'publish'),
                          ('Authorize', 'download'), ('Deploy', 'deploy'), ('Capture encrypted backup', 'capture'),
                          ('Upload encrypted backup', 'upload'), ('Check readiness', 'readiness'), ('Check health', 'readiness')]:
            self.assertEqual(STEPS[index(name)]['timeout-minutes'], '${{ fromJSON(inputs.step-timeouts).%s }}' % key)

    def test_budget_uses_the_delivery_phase_not_the_whole_job(self):
        self.assertTrue(WORKFLOW['on']['workflow_call']['inputs']['deploy-timeout-minutes']['required'])
        for name in ['Start delivery budget', 'Bound deployment to its remaining native phase budget']:
            self.assertEqual(STEPS[index(name)]['env']['TOTAL_MINUTES'], '${{ inputs.deploy-timeout-minutes }}')

    def test_step_timeout_keys_are_known_or_have_fallbacks(self):
        import re
        known = {'artifact', 'build', 'capture', 'checkout', 'connect', 'deploy', 'download', 'plan', 'prepare',
                 'publish', 'readiness', 'setup', 'test', 'upload'}
        for key, fallback in re.findall(r'step-timeouts\)\.([a-z_]+)( \|\| \d+)?', TEXT):
            self.assertTrue(key in known or fallback, key)

    def test_report_and_notify_conditions(self):
        report, notify = STEPS[index('Report failure')], STEPS[index('Notify')]
        self.assertEqual(report['if'], "failure() && inputs.report && steps.current.outputs.stale != 'true'")
        self.assertIn('report-failure.py', report['run'])
        self.assertEqual(notify['if'], "always() && !cancelled() && inputs.notify && steps.current.outputs.stale != 'true'")
        self.assertIs(notify['continue-on-error'], True)
        self.assertEqual(notify['uses'], './_delivery_actions/notify-discord')
        status = notify['with']['status']
        self.assertIn("job.status == 'success'", status)
        for phase in ['ready', 'prepared_stopped']:
            self.assertIn("steps.deploy.outputs.phase == '%s'" % phase, status)
        finish = STEPS[index('Finish')]
        self.assertEqual(finish['if'], 'always()'); self.assertEqual(finish['with']['status'], '${{ job.status }}')
        self.assertLess(index('Finish'), index('Report failure')); self.assertLess(index('Report failure'), index('Notify'))

    def test_readiness_gates_and_artifacts_keep_their_shapes(self):
        self.assertEqual(STEPS[index('Check health')]['if'],
                         "steps.deploy.outputs.phase == 'ready' && steps.plan.outputs.health-urls != ''")
        self.assertIn("steps.plan.outputs.readiness != ''", STEPS[index('Check readiness')]['if'])
        self.assertIn("steps.plan.outputs.readiness-operation == 'true'", STEPS[index('Check readiness')]['if'])
        self.assertIn("steps.plan.outputs.godot-production == 'true'", STEPS[index('Read the accepted public payload identity for independent browser acceptance')]['if'])
        uploads = {step['with']['name'].split('-${{')[0]: step for step in STEPS
                   if str(step.get('uses', '')).startswith('actions/upload-artifact@')}
        self.assertEqual(set(uploads), {'activation', 'host-release', 'backup', 'backup-status'})
        for step in uploads.values():
            self.assertIn('always()', step['if'])
        self.assertIn('!.artifacts/komizo-release/proof.json', uploads['host-release']['with']['path'])
        self.assertEqual(uploads['host-release']['with']['name'], 'host-release-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}')
        self.assertEqual(uploads['activation']['with']['name'], 'activation-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}')

    def test_actions_are_pinned_by_full_sha(self):
        for step in STEPS:
            uses = step.get('uses', '')
            if uses and not uses.startswith('./'):
                self.assertRegex(uses, r'@[a-f0-9]{40}$')


if __name__ == '__main__': unittest.main()
