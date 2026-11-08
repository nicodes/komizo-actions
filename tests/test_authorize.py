"""Fresh producer authority independently rechecks immutable source, bytes and full CI."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import authorize
import oci_registry as registry
import publication
from test_release_packet import make_packet
from test_submission import ENV


class FreshReleaseAuthority(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name); packet = make_packet(self.root)
        self.environment = ENV | {'GITHUB_WORKFLOW_REF': 'example/mailroom/.github/workflows/cd.yml@refs/heads/main',
                                  'GITHUB_WORKFLOW_SHA': 'a'*40, 'GITHUB_JOB': 'deliver'}
        self.context = self.environment | {'GITHUB_JOB': 'publish'}
        artifacts = {'.artifacts/release/' + name: hashlib.sha256((packet / name).read_bytes()).hexdigest()
                     for name in ['release.json', 'inventory.json', 'static/web.tar.gz']}
        self.proof = {'version': 1, 'source_commit': 'a'*40, 'source_tree': 'b'*40, 'cookbook_commit': 'c'*40,
                      'recipe': 'react-native', 'status': 'passed', 'tracked_source_clean': True,
                      'build_verified': True, 'tests_verified': True, 'test_groups': ['lint', 'unit', 'security'],
                      'configuration_sha256': 'f'*64, 'execution_context': self.context | {'GITHUB_JOB': 'validate'},
                      'artifacts': artifacts, 'providers': json.loads((packet/'release.json').read_bytes())['providers']}
        build_context = {'version': 1, 'source_commit': 'a'*40, 'source_tree': 'b'*40, 'tracked_source_clean': True,
                         'input_identity': 'd'*64, 'configuration_sha256': {'cookbook.toml': 'f'*64},
                         'execution_context': self.proof['execution_context']}
        build = {'identity': 'd'*64, 'artifacts': artifacts, 'provenance': build_context}
        test = build_context | {'tested_commit': 'a'*40, 'test_receipts': {group: {'input_identity': 'd'*64, 'source_commit': 'a'*40}
                                                                 for group in self.proof['test_groups']}}
        descriptor = json.loads((packet / 'release.json').read_bytes())
        published = {'kind': 'komizo.publication', 'version': 1, 'source': descriptor['source'],
                     'release_digest': registry.digest((packet / 'release.json').read_bytes()),
                     'published': {name: artifact['reference'] for name, artifact in descriptor['artifacts'].items()},
                     'validation_sha256': hashlib.sha256(registry.canonical(self.proof)).hexdigest(),
                     'inventory_sha256': hashlib.sha256((packet / 'inventory.json').read_bytes()).hexdigest(),
                     'execution_context': self.context}
        self.directory = self.root / 'evidence'; self.directory.mkdir()
        for name in ['release.json', 'inventory.json']: (self.directory / name).write_bytes((packet / name).read_bytes())
        for name, value in [('publication.json', published), ('validation.json', self.proof), ('build.json', build), ('test.json', test)]:
            (self.directory / name).write_bytes(registry.canonical(value))
        self.config = {'recipe': 'react-native', 'ci': {'runner': 'ubuntu-24.04'}}
        self.jobs = {'total_count': 2, 'jobs': [{'name': 'validate / Make full', 'conclusion': 'success', 'head_sha': 'a'*40, 'labels': ['ubuntu-24.04']},
                                              {'name': 'publish / publish', 'conclusion': 'success', 'head_sha': 'a'*40}]}

    def admit(self, environment=None, jobs=None):
        return authorize.admit(self.directory, 'mailroom', 'a'*40, self.config, 'c'*40,
                               self.proof['test_groups'], jobs or self.jobs, environment or self.environment, providers=self.proof['providers'])

    def mutate(self, name, update):
        path = self.directory / name; value = json.loads(path.read_bytes()); update(value); path.write_bytes(registry.canonical(value))

    def test_fresh_proof_binds_the_independently_admitted_release_to_current_job_identity(self):
        admitted = self.admit(); audiences = []
        proof = publication.release_proof(admitted, self.environment, token_request=lambda _, audience: audiences.append(audience) or 'a.b.c')
        unsigned = proof.copy(); unsigned.pop('proof')
        self.assertEqual(audiences, ['komizo-release:sha256:' + hashlib.sha256(registry.canonical(unsigned)).hexdigest()])
        self.assertEqual(proof['release_digest'], admitted.release_digest)
        self.assertEqual(proof['source_revision'], 'a'*40)
        self.assertEqual(set(path.name for path in self.directory.iterdir()), authorize.FILES)

    def test_wrong_caller_revision_attempt_or_repository_cannot_refresh_authority(self):
        for key, value in [('GITHUB_REPOSITORY', 'foreign/mailroom'), ('GITHUB_REPOSITORY_ID', '321'),
                           ('GITHUB_SHA', 'b'*40), ('GITHUB_RUN_ATTEMPT', '2'), ('GITHUB_RUN_ID', '457'),
                           ('GITHUB_WORKFLOW_REF', 'example/mailroom/.github/workflows/pr-preview.yml@refs/heads/main')]:
            with self.subTest(key=key), self.assertRaises(ValueError): self.admit(self.environment | {key: value})

    def test_missing_failed_or_ambiguous_full_source_or_publisher_jobs_never_refresh(self):
        for index in [0, 1]:
            changed = copy.deepcopy(self.jobs); changed['jobs'][index]['conclusion'] = 'failure'
            with self.assertRaises(ValueError): self.admit(jobs=changed)
        changed = copy.deepcopy(self.jobs); changed['jobs'].append(changed['jobs'][1]); changed['total_count'] = 3
        with self.assertRaises(ValueError): self.admit(jobs=changed)
        changed = copy.deepcopy(self.jobs); changed['total_count'] = 3
        with self.assertRaises(ValueError): self.admit(jobs=changed)

    def test_release_or_inventory_mutation_is_refused_without_reserializing_accepted_bytes(self):
        self.mutate('release.json', lambda value: value['source'].update(revision='e'*40))
        with self.assertRaises(ValueError): self.admit()

    def test_incomplete_tests_or_relabelled_build_receipts_do_not_gain_fresh_authority(self):
        self.mutate('test.json', lambda value: value['test_receipts'].pop('security'))
        with self.assertRaises(ValueError): self.admit()

    def test_missing_or_wrong_frozen_provider_facts_cannot_refresh_authority(self):
        self.mutate('release.json',lambda value:value['providers'].update(core_revision='a'*40))
        with self.assertRaises(ValueError):self.admit()

    def test_early_proof_or_unknown_files_cannot_be_smuggled_into_accepted_evidence(self):
        (self.directory / 'proof.json').write_text('expired early proof')
        with self.assertRaises(ValueError): self.admit()


if __name__ == '__main__': unittest.main()
