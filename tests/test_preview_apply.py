"""Preview URL selection is concrete, source-bound and precedes host effects."""
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('preview_apply', ROOT / 'scripts/preview.py')
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)
import release_packet
import submission


class PreviewApply(unittest.TestCase):
    def setUp(self):
        self.plan = {'action': 'up', 'pr': 42, 'policy': {'project': 'demo', 'backend': True, 'ttl_seconds': 600}}
        self.environment = {'KOMIZO_SSH_CONFIG': '/private/config'}
        self.alias = {'host': 'pr-42.example.test', 'path': '/api/*',
                      'target': {'kind': 'service', 'name': 'api', 'port': 8080}}
        self.app = {'host': 'pr-42.example.test', 'path': '/', 'target': {'kind': 'asset', 'name': 'web'}}
        self.api = {'host': 'pr-42-api.example.test', 'path': '/',
                    'target': {'kind': 'service', 'name': 'api', 'port': 8080}}
        self.descriptor = {'workload': {'routes': [self.alias, self.app, self.api]}}
        self.result = {'ok': True, 'phase': 'ready', 'release_digest': 'sha256:' + 'a' * 64, 'operation_id': 'b' * 32}

    def apply(self, descriptor=None, result=None):
        descriptor = self.descriptor if descriptor is None else descriptor
        documents = lambda path: descriptor if path.name == 'release.json' else {'proof': 'bounded'}
        with patch.object(release_packet, 'document', side_effect=documents), \
             patch.object(submission, 'request') as request, \
             patch.object(submission, 'submit', return_value=self.result if result is None else result) as submit, \
             patch.object(preview, 'output') as output:
            preview.apply(self.plan, self.environment)
            return request, submit, output

    def test_same_origin_alias_never_becomes_the_api_health_url(self):
        for routes in [[self.alias, self.app, self.api], [self.api, self.alias, self.app], [self.app, self.api, self.alias]]:
            with self.subTest(routes=routes):
                request, submit, output = self.apply({'workload': {'routes': routes}})
                request.assert_called_once()
                submit.assert_called_once()
                values = output.call_args.args[0]
                self.assertEqual(values['preview-url'], 'https://pr-42.example.test')
                self.assertEqual(values['api-url'], 'https://pr-42-api.example.test')
                self.assertEqual(preview.targets(self.plan, values['preview-url'], values['api-url'])['api']['url'], values['api-url'])

    def test_invalid_targets_refuse_before_request_authority_or_submission(self):
        for change in ['wildcard_only', 'wrong_pr', 'foreign_api', 'app_subpath', 'duplicate_api', 'control_host']:
            descriptor = copy.deepcopy(self.descriptor)
            routes = descriptor['workload']['routes']
            if change == 'wildcard_only': routes.pop()
            elif change == 'wrong_pr': routes[1]['host'] = 'pr-41.example.test'
            elif change == 'foreign_api': routes[2]['host'] = 'pr-42-api.other.test'
            elif change == 'app_subpath': routes[1]['path'] = '/app'
            elif change == 'duplicate_api': routes.append(copy.deepcopy(routes[2]))
            else: routes[1]['host'] = 'pr-42.example.test\n'
            with self.subTest(change=change), \
                 patch.object(release_packet, 'document', return_value=descriptor), \
                 patch.object(submission, 'request') as request, \
                 patch.object(submission, 'submit') as submit, \
                 patch.object(preview, 'output') as output:
                with self.assertRaises(ValueError): preview.apply(self.plan, self.environment)
                request.assert_not_called()
                submit.assert_not_called()
                output.assert_not_called()

    def test_stopped_preparation_and_failed_operations_do_not_publish_live_urls(self):
        for phase, ok in [('prepared_stopped', True), ('failed', False), ('needs_reconciliation', False)]:
            with self.subTest(phase=phase), \
                 patch.object(release_packet, 'document', side_effect=lambda path: self.descriptor if path.name == 'release.json' else {}), \
                 patch.object(submission, 'request'), \
                 patch.object(submission, 'submit', return_value=self.result | {'phase': phase, 'ok': ok}), \
                 patch.object(preview, 'output') as output:
                with self.assertRaisesRegex(ValueError, 'did not become ready'): preview.apply(self.plan, self.environment)
                output.assert_not_called()

    def test_static_preview_emits_no_api_url(self):
        self.plan['policy']['backend'] = False
        _, _, output = self.apply({'workload': {'routes': [self.app]}})
        self.assertEqual(output.call_args.args[0]['api-url'], '')

    def test_teardown_requires_its_completed_removal_before_reporting_absence(self):
        self.plan['action'] = 'down'
        installed = {'installed': {'release_digest': self.result['release_digest']}}
        with patch.object(submission, 'request'), \
             patch.object(submission, 'submit', side_effect=[installed, self.result | {'phase': 'prepared_stopped'}]), \
             patch.object(preview, 'output') as output:
            with self.assertRaisesRegex(ValueError, 'teardown did not finish'): preview.apply(self.plan, self.environment)
            output.assert_not_called()

    def test_target_validation_rejects_nonconcrete_or_control_character_hosts(self):
        for host in ['pr-42.*.test', 'pr-42.example..test', 'pr-42.example.test\n', 'pr-42.example.test\t', '-pr-42.example.test']:
            with self.subTest(host=host), self.assertRaises(ValueError):
                preview.targets(self.plan, 'https://' + host, 'https://pr-42-api.example.test')


if __name__ == '__main__':
    unittest.main()
