"""Worker and separate readiness budgets retain the original actual job deadline."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import operation_budget
import submission
from test_submission import ENV
from test_release_packet import descriptor


class OriginalOperationBudget(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
        self.job = {'name': 'deploy / Deliver exact release', 'head_sha': 'a'*40, 'status': 'in_progress',
                    'started_at': (self.now - timedelta(minutes=20)).isoformat()}
        self.jobs = {'total_count': 1, 'jobs': [self.job]}

    def test_current_worker_uses_only_remaining_original_job_and_phase_time(self):
        absolute, seconds = operation_budget.deadline(self.jobs, 'a'*40, 30, 'deliver', now=self.now)
        self.assertEqual(seconds, 600); self.assertEqual(absolute, int((self.now + timedelta(minutes=10)).timestamp()))
        self.assertEqual(operation_budget.deadline(self.jobs, 'a'*40, 30, 'deliver', now=self.now, phase_seconds=120)[1], 120)
        with self.assertRaises(ValueError): operation_budget.deadline(self.jobs, 'a'*40, 20, 'deliver', now=self.now)

    def test_separate_readiness_queue_and_setup_consume_same_completed_deliver_budget(self):
        self.job.update(status='completed', conclusion='success')
        self.assertEqual(operation_budget.deadline(self.jobs, 'a'*40, 30, 'deliver', now=self.now, completed=True)[1], 600)
        with self.assertRaises(ValueError): operation_budget.deadline(self.jobs, 'a'*40, 30, 'deliver', now=self.now + timedelta(minutes=10), completed=True)
        self.job['conclusion'] = 'failure'
        with self.assertRaises(ValueError): operation_budget.deadline(self.jobs, 'a'*40, 30, 'deliver', now=self.now, completed=True)

    def test_partial_duplicate_wrong_source_or_unknown_actual_job_cannot_extend_budget(self):
        for jobs in [self.jobs | {'total_count': 2}, {'total_count': 2, 'jobs': [self.job, self.job]},
                     {'total_count': 1, 'jobs': [self.job | {'head_sha': 'b'*40}]}]:
            with self.assertRaises(ValueError): operation_budget.deadline(jobs, 'a'*40, 30, 'deliver', now=self.now)

    def test_request_admits_timeout_before_proof_and_keeps_jwt_lifetime_independent(self):
        seen = []
        body = submission.request('mailroom', 'production', 'deploy', ENV | {'KOMIZO_OPERATION_DEADLINE': '125'},
                                  descriptor=descriptor(), now=100, timeout_seconds=300,
                                  token_request=lambda _, audience: seen.append(audience) or 'a.b.c')
        self.assertEqual(body['timeout_seconds'], 25); self.assertEqual(body['expires_at'], 220)
        for value in ['100', '0', 'invalid']:
            with self.assertRaises(ValueError):
                submission.request('mailroom', 'production', 'deploy', ENV | {'KOMIZO_OPERATION_DEADLINE': value},
                                   descriptor=descriptor(), now=100, token_request=lambda *_: self.fail('expired request must not mint proof'))


if __name__ == '__main__': unittest.main()
