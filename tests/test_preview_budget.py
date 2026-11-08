"""A preview publisher cannot gain time by queueing after its actual source job."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import preview


class OriginalProducerBudget(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
        self.plan = {'controller': 'a'*40, 'policy': {'producer_timeout_minutes': 60, 'validation_runner': 'fixture-4vcpu'}}
        self.job = {'name': 'full / Make full', 'conclusion': 'success', 'head_sha': 'a'*40, 'labels': ['fixture-4vcpu'],
                    'started_at': (self.now - timedelta(minutes=50)).isoformat()}
        self.jobs = {'total_count': 1, 'jobs': [self.job]}

    def test_setup_queue_and_source_runtime_all_consume_original_budget(self):
        self.assertEqual(preview.producer_deadline(self.plan, self.jobs, self.now), 600)
        self.assertEqual(preview.producer_deadline(self.plan, self.jobs, self.now + timedelta(minutes=9)), 60)
        with self.assertRaises(ValueError): preview.producer_deadline(self.plan, self.jobs, self.now + timedelta(minutes=10))

    def test_partial_metadata_failed_source_foreign_runner_future_or_missing_time_refuse(self):
        for changes in [{'conclusion': 'failure'}, {'head_sha': 'b'*40}, {'labels': ['foreign']},
                        {'started_at': (self.now + timedelta(seconds=6)).isoformat()}, {'started_at': 'invalid'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                preview.producer_deadline(self.plan, {'total_count': 1, 'jobs': [self.job | changes]}, self.now)
        with self.assertRaises(ValueError): preview.producer_deadline(self.plan, self.jobs | {'total_count': 2}, self.now)


if __name__ == '__main__': unittest.main()
