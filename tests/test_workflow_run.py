"""PR-target jobs retain their real head while authority stays on trusted main."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import operation_budget
import workflow_run


class ActualWorkflowRun(unittest.TestCase):
    def setUp(self):
        self.environment = dict(GITHUB_SERVER_URL='https://github.com', GITHUB_REPOSITORY='example/mailroom',
            GITHUB_REPOSITORY_ID='123', GITHUB_SHA='b'*40, GITHUB_RUN_ID='456', GITHUB_RUN_ATTEMPT='2',
            GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='pull_request_target',
            GITHUB_WORKFLOW_REF='example/mailroom/.github/workflows/pr-preview.yml@refs/heads/main',
            GITHUB_WORKFLOW_SHA='b'*40)
        repository = dict(full_name='example/mailroom', id=123)
        self.metadata = dict(id=456, run_attempt=2, event='pull_request_target',
            path='.github/workflows/pr-preview.yml', head_sha='a'*40,
            repository=repository.copy(), head_repository=repository.copy())
        self.event = dict(action='closed', number=42, repository=repository.copy(),
            pull_request=dict(number=42, state='closed', user=dict(login='owner'), labels=[],
                head=dict(sha='a'*40, ref='feature', repo=repository.copy()),
                base=dict(ref='main', repo=repository.copy())))
        self.now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
        self.job = dict(id=789, run_id=456, run_attempt=2, head_sha='a'*40,
            name='down / Preview', status='in_progress',
            started_at=(self.now-timedelta(minutes=1)).isoformat())
        self.jobs = dict(total_count=1, jobs=[self.job])
        self.calls = []

    def lookup(self, path):
        self.calls.append(path)
        return copy.deepcopy(self.jobs if path.endswith('/jobs?per_page=100') else self.metadata)

    def current(self):
        return workflow_run.current(self.environment, self.lookup, event=self.event)

    def test_closed_unrequested_preview_keeps_actual_head_and_original_deadline(self):
        before = self.environment.copy()
        _, jobs, head = self.current()
        self.assertEqual(head, 'a'*40)
        self.assertEqual(self.environment, before, 'job metadata must never relabel the main controller')
        self.assertEqual(operation_budget.deadline(jobs, head, 10, 'effects', now=self.now),
                         (int((self.now+timedelta(minutes=9)).timestamp()), 540))
        self.assertEqual(self.calls, ['repos/example/mailroom/actions/runs/456/attempts/2',
            'repos/example/mailroom/actions/runs/456/attempts/2/jobs?per_page=100'])
        spec = importlib.util.spec_from_file_location('closed_preview_request', ROOT/'preview-request/request.py')
        resolver = importlib.util.module_from_spec(spec); spec.loader.exec_module(resolver)
        self.assertEqual(resolver.resolve(self.event, 'pull_request', 'example/mailroom', 'preview')['action'], 'down')

    def test_pr_target_open_push_and_close_use_event_head_without_requiring_it_equal_main(self):
        for action in ['synchronize', 'reopened', 'closed']:
            with self.subTest(action=action):
                self.event['action'] = action
                self.assertEqual(self.current()[2], 'a'*40)

    def test_issue_comment_and_main_operations_cannot_substitute_another_head(self):
        for event in ['issue_comment', 'push', 'workflow_dispatch', 'schedule']:
            with self.subTest(event=event):
                self.environment['GITHUB_EVENT_NAME'] = self.metadata['event'] = event
                with self.assertRaises(ValueError): self.current()
                self.metadata['head_sha'] = self.job['head_sha'] = 'b'*40
                self.assertEqual(self.current()[2], 'b'*40)
                self.metadata['head_sha'] = self.job['head_sha'] = 'a'*40

    def test_wrong_main_controller_or_caller_refuses_before_github_reads(self):
        for change in [dict(GITHUB_REF='refs/pull/42/merge'), dict(GITHUB_EVENT_NAME='pull_request'),
                       dict(GITHUB_WORKFLOW_SHA='a'*40), dict(GITHUB_WORKFLOW_REF='example/mailroom/.github/workflows/ci.yml@refs/heads/main')]:
            with self.subTest(change=change), patch.dict(self.environment, change), self.assertRaises(ValueError):
                self.current()
            self.assertEqual(self.calls, [])

    def test_foreign_run_attempt_event_workflow_or_repository_refuses(self):
        original = copy.deepcopy(self.metadata)
        changes = [dict(id=457), dict(run_attempt=1), dict(event='issue_comment'), dict(path='.github/workflows/cd.yml'),
                   dict(head_sha='c'*40), dict(head_sha='invalid'), dict(repository=dict(full_name='foreign/mailroom',id=123)),
                   dict(repository=dict(full_name='example/mailroom',id=124)),
                   dict(head_repository=dict(full_name='foreign/mailroom',id=123)), dict(run_attempt=True)]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.metadata = original | change; self.current()
        self.metadata = original

    def test_triggering_event_cannot_claim_fork_head_or_different_pr_identity(self):
        original = copy.deepcopy(self.event)
        for mutate in [lambda e:e.update(number=43), lambda e:e['pull_request']['head'].update(sha='c'*40),
                       lambda e:e['pull_request']['base'].update(ref='feature'),
                       lambda e:e['pull_request']['head']['repo'].update(full_name='fork/mailroom'),
                       lambda e:e['pull_request']['base']['repo'].update(id=124),
                       lambda e:e.update(repository=dict(full_name='foreign/mailroom',id=123)),
                       lambda e:e.update(pull_request=[]), lambda e:e['pull_request'].update(head=[]),
                       lambda e:e.update(number=True)]:
            self.event = copy.deepcopy(original); mutate(self.event)
            with self.subTest(event=self.event), self.assertRaises(ValueError): self.current()

    def test_jobs_require_complete_unique_same_attempt_and_run_source(self):
        original = self.job.copy()
        for change in [dict(id=True), dict(id=0), dict(run_id=457), dict(run_id=True), dict(run_attempt=1),
                       dict(head_sha='b'*40), dict(run_attempt=True)]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.jobs = dict(total_count=1,jobs=[original|change]); self.current()
        for jobs in [dict(total_count=2,jobs=[original]), dict(total_count=2,jobs=[original,original]),
                     dict(total_count=0,jobs=[]), dict(total_count=True,jobs=[original])]:
            self.jobs=jobs
            with self.subTest(jobs=jobs), self.assertRaises(ValueError): self.current()

    def test_budget_cli_writes_only_remaining_time_and_refuses_expiry_or_job_mismatch(self):
        now = self.now
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return now
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); event=root/'event.json'; event.write_text(json.dumps(self.event))
            environment=self.environment|{'GITHUB_EVENT_PATH':str(event),'GITHUB_ENV':str(root/'env'),'GITHUB_OUTPUT':str(root/'output')}
            with patch.dict(os.environ,environment,clear=True), patch.object(operation_budget,'api',side_effect=self.lookup), \
                    patch.object(operation_budget,'datetime',Clock), patch.object(sys,'argv',['operation_budget','--job','effects','--total-minutes','10']):
                operation_budget.main()
                self.assertEqual((root/'env').read_text(),'KOMIZO_OPERATION_DEADLINE='+str(int((now+timedelta(minutes=9)).timestamp()))+'\n')
                self.assertEqual((root/'output').read_text(),'seconds=540\nminutes=9\n')
                for change in [dict(started_at=(now-timedelta(minutes=10)).isoformat()),dict(status='queued'),
                               dict(head_sha='b'*40),dict(name='foreign / Apply another lifecycle')]:
                    (root/'env').write_text('');(root/'output').write_text('');self.jobs=dict(total_count=1,jobs=[self.job|change])
                    with self.subTest(change=change),self.assertRaises(SystemExit):operation_budget.main()
                    self.assertEqual((root/'env').read_text(),'');self.assertEqual((root/'output').read_text(),'')

    def test_pr_event_is_bounded_regular_runner_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);event=root/'event.json';event.write_text(json.dumps(self.event))
            self.environment['GITHUB_EVENT_PATH']=str(event)
            self.assertEqual(workflow_run.current(self.environment,self.lookup)[2],'a'*40)
            event.write_bytes(b' '*((4<<20)+1))
            with self.assertRaises(ValueError):workflow_run.current(self.environment,self.lookup)
            event.unlink();event.symlink_to(root/'absent')
            with self.assertRaises(ValueError):workflow_run.current(self.environment,self.lookup)


if __name__ == '__main__': unittest.main()
