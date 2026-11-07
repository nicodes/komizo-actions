"""Reusable operational leaves retain fixed source identities and effect order."""
from pathlib import Path
import unittest

import yaml

ROOT=Path(__file__).resolve().parents[1]
LEAVES=['backup','production-check','public-production-check','recovery','source-audit',
    'source-audit-no-report','security-audit','security-audit-no-report','legacy-retention',
    'historical-deploy','report']


class OperationalWorkflowContext(unittest.TestCase):
    def test_each_operational_job_resolves_real_dependency_before_planning_or_effects(self):
        for name in LEAVES:
            workflow=yaml.safe_load((ROOT/f'.github/workflows/cookbook-{name}.yml').read_text())
            inputs=workflow['on']['workflow_call']['inputs']
            self.assertFalse(inputs['cookbook-ref']['required'])
            self.assertFalse(inputs['tools-ref']['required'])
            for job_name,job in workflow['jobs'].items():
                if name=='report' and job_name=='notify':continue
                with self.subTest(leaf=name,job=job_name):
                    steps=job['steps']
                    self.assertEqual(steps[0]['name'],'Require one exact recipe source contract')
                    self.assertIn('-z "$TOOLS_REF"',steps[0]['run'])
                    self.assertIn('^[a-f0-9]{40}$',steps[0]['run'])
                    source=[s for s in steps if s.get('with',{}).get('repository')==
                        "${{ inputs.cookbook-ref && 'nicodes/cookbook' || 'nicodes/tools' }}"]
                    self.assertEqual(len(source),1)
                    self.assertEqual(source[0]['with']['ref'],'${{ inputs.cookbook-ref || inputs.tools-ref }}')
                    preparation=[s for s in steps if s.get('uses','').endswith('/recipe-setup')]
                    self.assertEqual(len(preparation),1)
                    self.assertEqual(preparation[0]['if'],"${{ inputs.cookbook-ref != '' }}")
                    self.assertEqual(preparation[0]['with'],{'cookbook-ref':'${{ inputs.cookbook-ref }}','tools':'python'})
                    self.assertIn('.artifacts/recipe-sdks/_cookbook',source[0]['with']['path'])
                    self.assertLess(steps.index(source[0]),steps.index(preparation[0]))
                    legacy=[s for s in steps if s.get('uses','').startswith('nicodes/tools/setup@')]
                    self.assertEqual(len(legacy),1)
                    self.assertIn("inputs.cookbook-ref == ''",legacy[0]['if'])
                    self.assertEqual(preparation[0]['timeout-minutes'],legacy[0]['timeout-minutes'])
                    plan=[s for s in steps if s.get('id')=='plan']
                    if plan:self.assertLess(steps.index(preparation[0]),steps.index(plan[0]))
                    self.assertTrue(all('timeout-minutes' in s for s in steps))

    def test_Godot_public_and_recovery_adapters_forward_both_explicit_contracts(self):
        action=yaml.safe_load((ROOT/'godot-production/action.yml').read_text())
        steps=action['runs']['steps']
        native=[s for s in steps if 'production.py' in s.get('run','')]
        self.assertEqual(len(native),2)
        for step in native:
            self.assertIn('--cookbook-root .artifacts/recipe-sdks/_cookbook',step['run'])
            self.assertIn('--tools-root .artifacts/recipe-sdks/_tools',step['run'])
            self.assertIn('--tools _godot_production_cookbook --tools-ref "$TOOLS_REF"',step['run'])
        for leaf in ['godot-production','godot-recovery']:
            workflow=yaml.safe_load((ROOT/f'.github/workflows/cookbook-{leaf}.yml').read_text())
            self.assertFalse(workflow['on']['workflow_call']['inputs']['cookbook-ref']['required'])
        recovery=yaml.safe_load((ROOT/'.github/workflows/cookbook-godot-recovery.yml').read_text())
        job=recovery['jobs']['verify']
        self.assertEqual(job['timeout-minutes'],25)
        operations=[s for s in job['steps'] if 'recovery.py' in s.get('run','')]
        self.assertEqual(len(operations),3)
        for step in operations:
            self.assertIn('--cookbook-root .artifacts/recipe-sdks/_cookbook',step['run'])
            self.assertIn('--tools-root .artifacts/recipe-sdks/_tools',step['run'])
        self.assertFalse(any('recovery.py restore' in s.get('run','') for s in job['steps']))


if __name__=='__main__':unittest.main()
