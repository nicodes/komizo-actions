"""A job fetches only the commit it builds or deploys unless history is explained."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted([*ROOT.glob('.github/workflows/*.yml'), *ROOT.glob('*/action.yml')])


def violations(name, text):
    """Return every checkout that fetches full history without the cheap, explained form."""
    document = yaml.safe_load(text)
    jobs = document.get('jobs', {}).values()
    steps = [step for job in jobs for step in job.get('steps', [])]
    steps += document.get('runs', {}).get('steps', [])
    full = [step for step in steps if str(step.get('uses', '')).startswith('actions/checkout@')
            and str(step.get('with', {}).get('fetch-depth', 1)) in {'0', '-1'}]
    lines = text.splitlines()
    marked = [i for i, line in enumerate(lines)
              if line.strip() in {'fetch-depth: 0', 'fetch-depth: -1'}
              and i and lines[i - 1].strip().startswith('# History needed: ')]
    found = [name + ': full-history checkout needs filter: blob:none'
             for step in full if step['with'].get('filter') != 'blob:none']
    if len(marked) != len(full):
        found.append(name + ': every full-history checkout needs a "# History needed:" comment')
    return found


class CheckoutDepth(unittest.TestCase):
    def test_workflows_and_actions_are_covered(self):
        self.assertIn('deploy.yml', {path.name for path in FILES})
        self.assertIn('application-publication', {path.parent.name for path in FILES})

    def test_full_history_checkouts_are_blobless_and_explained(self):
        for path in FILES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual(violations(path.name, path.read_text()), [])

    def test_the_rule_rejects_unexplained_or_unfiltered_full_history(self):
        base = ('jobs:\n  j:\n    steps:\n    - uses: actions/checkout@' + 'a' * 40 + '\n      with:\n')
        self.assertEqual(violations('x', base + '        fetch-depth: 1\n'), [])
        self.assertEqual(violations('x', base + '        persist-credentials: false\n'), [])
        self.assertTrue(violations('x', base + '        fetch-depth: 0\n'))
        self.assertTrue(violations('x', base + '        # History needed: reason\n        fetch-depth: 0\n'))
        self.assertTrue(violations('x', base + '        fetch-depth: 0\n        filter: blob:none\n'))
        self.assertEqual(violations('x', base + '        # History needed: reason\n        fetch-depth: 0\n'
                                    '        filter: blob:none\n'), [])


if __name__ == '__main__':
    unittest.main()
