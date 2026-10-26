"""Real immutable-artifact/receiver flow with fixture-owned GitHub bytes."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('recovery_evidence',ROOT/'recovery-evidence/evidence.py');evidence=importlib.util.module_from_spec(spec);spec.loader.exec_module(evidence)

class RecoveryEvidence(unittest.TestCase):
    def facts(self):
        repo='example/project'
        run={'id':123,'repository':{'full_name':repo},'head_repository':{'full_name':repo},'head_branch':'main','conclusion':'success','status':'completed','path':'.github/workflows/backup.yml','event':'schedule','head_sha':'a'*40}
        artifact={'id':456,'expired':False,'workflow_run':{'id':123},'name':'backup-123-1','digest':'sha256:'+'b'*64,'size_in_bytes':100}
        return repo,run,artifact

    def test_exact_run_artifact_repository_and_main_provenance(self):
        repo,run,artifact=self.facts();evidence.provenance(run,artifact,repo,'123','456')
        for kind,field,value in [('run','id',124),('run','head_branch','untrusted'),('run','status','in_progress'),('run','conclusion','failure'),('run','path','.github/workflows/other.yml'),('run','event','pull_request'),('artifact','id',457),('artifact','expired',True),('artifact','name','arbitrary'),('artifact','digest','missing')]:
            r,a=copy.deepcopy(run),copy.deepcopy(artifact);(r if kind=='run' else a)[field]=value
            with self.assertRaises(ValueError):evidence.provenance(r,a,repo,'123','456')
        for key in ['repository','head_repository']:
            r=copy.deepcopy(run);r[key]['full_name']='other/project'
            with self.assertRaises(ValueError):evidence.provenance(r,artifact,repo,'123','456')
        for value in ['0','01','-1','main','1'*21]:
            with self.assertRaises(ValueError):evidence.positive(value)

    def test_real_receiver_checks_zip_digest_and_records_transport_only(self):
        repo,run,artifact=self.facts();payload=b'opaque synthetic fixture ciphertext'
        receipt={'format':1,'archive_sha256':'a'*64,'ciphertext_sha256':hashlib.sha256(payload).hexdigest(),'image_id':'sha256:'+'b'*64}
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as bundle:
            bundle.writestr('snapshot.cms',payload);bundle.writestr('receipt.json',json.dumps(receipt))
        data=stream.getvalue();artifact.update(digest='sha256:'+hashlib.sha256(data).hexdigest(),size_in_bytes=len(data))
        environment={'GITHUB_REPOSITORY':repo,'SOURCE_RUN_ID':'123','SOURCE_ARTIFACT_ID':'456'}
        def api(path):return run if '/runs/' in path else artifact
        def download(command,**kwargs):
            self.assertEqual(command,['gh','api','repos/'+repo+'/actions/artifacts/456/zip']);kwargs['stdout'].write(data)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,environment), patch.object(evidence,'api',side_effect=api),patch.object(evidence.subprocess,'run',side_effect=download):
            root=Path(directory);evidence.recovery(root);result=json.loads((root/'.artifacts/recovery/transport.json').read_text());self.assertTrue(result['transport_verified']);self.assertFalse(result['metadata_authenticated']);self.assertFalse(result['database_restored']);self.assertEqual(result['source_revision'],'a'*40);self.assertNotIn(payload.decode(),json.dumps(result))
            artifact['digest']='sha256:'+'f'*64
            with self.assertRaisesRegex(ValueError,'digest differs'):evidence.recovery(root)

    def test_untrusted_event_refuses_before_any_github_request(self):
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'true','GITHUB_REF':'refs/pull/1/merge'}),patch.object(evidence,'api') as api:
            with self.assertRaisesRegex(ValueError,'trusted main'):evidence.main()
            api.assert_not_called()

if __name__=='__main__':unittest.main()
