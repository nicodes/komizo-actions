"""A single-job step receives every variable and input its multi-job counterpart received."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github/workflows'


def load(path):
    return yaml.safe_load((ROOT / path).read_text())


def steps_of(document, job):
    return document['runs']['steps'] if job is None else document['jobs'][job]['steps']


def effective(document, job, name):
    """Job env, step env and step inputs a step runs with, as one set of keys."""
    step = next(step for step in steps_of(document, job) if step.get('name') == name)
    keys = {'env:' + key for key in (document['jobs'][job].get('env', {}) if job else {})}
    keys |= {'env:' + key for key in step.get('env', {})}
    keys |= {'with:' + key for key in step.get('with', {})}
    return keys


def pair(old_file, old_job, old_step, new_file, new_job, new_step, allowed=None):
    return (old_file, old_job, old_step, new_file, new_job, new_step, allowed or {})


DEPLOY = ('.github/workflows/deploy.yml', 'deliver')
DELIVER = ('.github/workflows/deliver.yml', 'deliver')
PREVIEW = ('.github/workflows/preview.yml', 'preview')
LEGACY_PREVIEW = ('.github/workflows/pr-preview.yml', 'effects')

PAIRS = [
    pair(*DEPLOY, name, *DELIVER, name) for name in [
        'Plan deployment', 'Prepare host connection', 'Capture encrypted backup', 'Upload encrypted backup', 'Deploy',
        'Read the accepted public payload identity for independent browser acceptance', 'Check health',
        'Retain the bounded historical activation receipt', 'Retain only the verified encrypted backup pair',
        'Retain the distinct absent or stopped backup status']
] + [
    pair('.github/workflows/readiness.yml', 'verify', 'Run actual product assertions without host or identity authority',
         *DELIVER, 'Check readiness',
         {'env:SECONDS_LEFT': 'the step timeout bounds the assertions; the job is no longer a separate queued job',
          'env:DECLARATION': 'passed as the application-readiness input declaration, which the composite exports'}),
    pair('.github/workflows/report-failure.yml', 'report', 'Report failure', *DELIVER, 'Report failure'),
    pair('.github/workflows/notify.yml', 'notify', 'Notify', *DELIVER, 'Notify'),
    pair('application-publication/action.yml', None, 'Publish exact images from successful full validation', *DELIVER, 'Publish',
         {'env:SOURCE': 'deliver.py takes the source as --source "$GITHUB_SHA"'}),
    pair(*DEPLOY, 'Re-admit accepted publication and mint fresh release authority', *DELIVER, 'Authorize',
         {'env:APPLICATION': 'deliver.py takes --project', 'env:SOURCE': 'deliver.py takes --source "$GITHUB_SHA"',
          'env:COMBINED': 'there is one delivery shape'}),
    pair(*LEGACY_PREVIEW, 'Connect only trusted lifecycle code', *PREVIEW, 'Prepare host connection',
         {'env:PLAN': 'host connection does not read the plan'}),
    pair(*LEGACY_PREVIEW, 'Recheck current source and apply exact preview operation', *PREVIEW,
         'Apply the exact preview operation'),
    pair(*LEGACY_PREVIEW, 'Verify exact source readiness on verified routes', *PREVIEW,
         'Verify exact source readiness on verified routes'),
    pair(*LEGACY_PREVIEW, 'Retain label and sticky comment lifecycle', *PREVIEW,
         'Comment the preview lifecycle on the pull request'),
    pair(*LEGACY_PREVIEW, 'Announce successful Preview', *PREVIEW, 'Announce successful Preview',
         {'env:PLAN': 'notification does not read the plan'}),
    pair(*LEGACY_PREVIEW, 'Re-admit exact publication and mint fresh preview authority', *PREVIEW, 'Authorize',
         {'env:APPLICATION': 'deliver.py takes --project', 'env:SOURCE': 'deliver.py takes --source "$PLANNED_HEAD"'}),
    pair('.github/workflows/publish-preview.yml', 'publish', 'Admit and publish the exact tested archive', *PREVIEW, 'Publish'),
]


class LegacyParity(unittest.TestCase):
    def test_every_single_job_step_keeps_the_environment_its_legacy_step_had(self):
        for old_file, old_job, old_step, new_file, new_job, new_step, allowed in PAIRS:
            with self.subTest(step=new_step, legacy=old_step):
                old = effective(load(old_file), old_job, old_step)
                new = effective(load(new_file), new_job, new_step)
                self.assertEqual(sorted(old - new - set(allowed)), [])
                for key, reason in allowed.items():
                    self.assertTrue(reason)

    def test_the_allow_list_only_names_real_differences(self):
        for old_file, old_job, old_step, new_file, new_job, new_step, allowed in PAIRS:
            old = effective(load(old_file), old_job, old_step)
            new = effective(load(new_file), new_job, new_step)
            for key in allowed:
                with self.subTest(step=new_step, key=key):
                    self.assertIn(key, old); self.assertNotIn(key, new)

    def test_readiness_proves_the_exact_commit_to_the_consumer_script(self):
        steps = steps_of(load('.github/workflows/deliver.yml'), 'deliver')
        step = next(step for step in steps if step.get('name') == 'Check readiness')
        self.assertEqual(step['env']['EXPECTED_REVISION'], '${{ github.sha }}')
        self.assertEqual(step['with']['declaration'], '${{ inputs.declaration }}')


if __name__ == '__main__': unittest.main()
