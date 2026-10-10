import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('release_proof', Path(__file__).resolve().parents[1]/'scripts/release-proof.py')
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class ReleaseProofTests(unittest.TestCase):
    def setUp(self):
        self.version = 'a'*40
        self.env = dict(GITHUB_SHA=self.version, GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='push', GITHUB_REPOSITORY='owner/source', GITHUB_REPOSITORY_ID='123', GITHUB_RUN_ID='456')
        self.images = {'ghcr.io/owner/demo-api:'+self.version: 'sha256:'+'1'*64, 'ghcr.io/owner/demo-config:'+self.version: 'sha256:'+'2'*64}
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'evidence.json'

    def save(self, data):
        self.path.write_text(json.dumps(data))
        return str(self.path)

    def publication(self):
        execution = {key:self.env[key] for key in ('GITHUB_REPOSITORY','GITHUB_RUN_ID','GITHUB_SHA')}
        return dict(version=1, revision=self.version, tested_commit=self.version, images=self.images,
                    publisher_context=execution, build_provenance=dict(source_commit=self.version, tracked_source_clean=True, build_context=execution),
                    test_provenance=dict(source_commit=self.version, tested_commit=self.version, tracked_source_clean=True, execution_context=execution))

    def test_published_proof_requires_same_test_build_execution(self):
        data = self.publication()
        result = proof.manifest('publication', self.save(data), '', '', self.version, self.env)
        self.assertEqual(result['images'], self.images)
        for section, field, bad in [('build_provenance','source_commit','b'*40),('test_provenance','tracked_source_clean',False),('test_provenance','execution_context',dict(GITHUB_RUN_ID='another'))]:
            with self.subTest(section=section, field=field):
                data = self.publication()
                data[section][field] = bad
                with self.assertRaises(ValueError):
                    proof.manifest('publication', self.save(data), '', '', self.version, self.env)

    def test_transfer_requires_every_loaded_image_identity(self):
        data = dict(source=self.version, images=self.images)
        proof.manifest('transferred-images', self.save(data), '', '', self.version, self.env, self.images.__getitem__)
        with self.assertRaises(ValueError):
            proof.manifest('transferred-images', self.save(data), '', '', self.version, self.env, lambda _: 'sha256:'+'f'*64)

    def test_gate_requires_verified_job_identity(self):
        images = dict(self.images)
        images['ghcr.io/owner/demo-gate:'+self.version] = images.pop('ghcr.io/owner/demo-api:'+self.version)
        proof.manifest('gate-artifact', '', 'ghcr.io/owner/demo', 'sha256:'+'1'*64, self.version, self.env, images.__getitem__)
        with self.assertRaises(ValueError):
            proof.manifest('gate-artifact', '', 'ghcr.io/owner/demo', 'sha256:'+'f'*64, self.version, self.env, images.__getitem__)

    def test_token_audience_binds_exact_document_and_source(self):
        manifest = proof.context(self.env, self.version) | dict(images=self.images)
        calls = []
        def identity(audience, env):
            calls.append(audience)
            return 'fixture.token.signature'
        result = proof.envelope(self.save(manifest), self.version, self.env, identity)
        raw = base64.b64decode(result['manifest'])
        self.assertEqual(calls, ['komizo-release:sha256:'+hashlib.sha256(raw).hexdigest()])
        manifest['run_id'] = '999'
        with self.assertRaises(ValueError):
            proof.envelope(self.save(manifest), self.version, self.env, identity)
        self.assertEqual(len(calls), 1)

    def test_wrong_events_refs_and_ambiguous_files_fail_closed(self):
        for field, value in [('GITHUB_SHA','b'*40),('GITHUB_REF','refs/pull/1/merge'),('GITHUB_EVENT_NAME','pull_request'),('GITHUB_REPOSITORY_ID','')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                proof.context(self.env | {field:value}, self.version)
        self.path.write_text('{"images":{},"images":{}}')
        with self.assertRaises(ValueError):
            proof.read(self.path)
        self.path.write_text('x'*(proof.MAX+1))
        with self.assertRaises(ValueError):
            proof.read(self.path)
        link = self.path.parent/'link'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            proof.read(link)

    def test_private_output_does_not_follow_symlinks(self):
        target = self.path.parent/'target'
        target.write_text('preserved')
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            proof.write(self.path, {'token':'fixture'})
        self.assertEqual(target.read_text(), 'preserved')
        path = self.path.parent/'private'
        proof.write(path, {'token':'fixture'})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
