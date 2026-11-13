"""Actual format 2 transport through the isolated action entrypoint."""
import copy
from contextlib import chdir, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('recovery_evidence', ROOT/'recovery-evidence/evidence.py')
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class RecoveryEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = Path(os.environ.get('RECOVERY_TEST_TOOLS', ROOT/'.artifacts/recovery-transport-sdk')).resolve()
        revision = subprocess.check_output(['git', '-C', str(cls.tools), 'rev-parse', 'HEAD'], text=True).strip()
        if revision != evidence.TOOLS_REVISION:
            raise AssertionError('prepare the exact reviewed format 2 transport SDK')
        temporary = tempfile.TemporaryDirectory(prefix='real format 2 recovery transport ')
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name)
        key, certificate = root/'key.pem', root/'recipient.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-noenc',
            '-keyout',str(key),'-out',str(certificate),'-days','1',
            '-subj','/CN=synthetic-transport-control'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        package = root/'package'
        package.mkdir()
        archive = package/'data.tar.gz'
        cls.plaintext = b'synthetic private source data must remain encrypted'
        with tarfile.open(archive,'w:gz') as bundle:
            member = tarfile.TarInfo('data.db')
            member.size = len(cls.plaintext)
            bundle.addfile(member,io.BytesIO(cls.plaintext))
        (package/'verification.json').write_text(json.dumps({
            'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
            'image_id':'sha256:'+'b'*64,
            'image_reference':'ghcr.io/example/database@sha256:'+'c'*64,
            'verified_at':'2026-11-09T12:00:00+00:00'}))
        spec = importlib.util.spec_from_file_location('transport_snapshot',cls.tools/'helpers/snapshot.py')
        snapshot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(snapshot)
        sealed = snapshot.seal(package,certificate)
        cls.ciphertext = (sealed/'snapshot.cms').read_bytes()
        cls.receipt = json.loads((sealed/'receipt.json').read_text())
        if cls.receipt['format'] != 2 or not cls.ciphertext.startswith(b'KMSNAP2\0'):
            raise AssertionError('expected the actual framed format 2 envelope')

    def facts(self):
        repo = 'example/project'
        run = {'id':123,'repository':{'full_name':repo},'head_repository':{'full_name':repo},
            'head_branch':'main','conclusion':'success','status':'completed',
            'path':'.github/workflows/backup.yml','event':'schedule','head_sha':'a'*40}
        artifact = {'id':456,'expired':False,'workflow_run':{'id':123},'name':'backup-123-1',
            'digest':'sha256:'+'b'*64,'size_in_bytes':100}
        return repo,run,artifact

    def test_routine_backup_transfers_the_actual_format_two_pair_with_bound_native_receipts(self):
        from test_submission import ENV
        import backup_submission
        import submission
        real_request, real_submit, real_download = submission.request, submission.submit, backup_submission.download
        for format_value in [2, 1, True, '2']:
            with self.subTest(format=format_value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / 'ssh-config'; config.write_text('Host deploy-target\n'); config.chmod(0o600)
                environment = ENV | {'KOMIZO_SSH_CONFIG': str(config)}
                metadata = self.receipt | {'format': format_value}
                pair = {'snapshot.cms': self.ciphertext, 'receipt.json': json.dumps(metadata).encode()}
                inventory = [{'name': name, 'size_bytes': len(raw),
                    'sha256': 'sha256:' + hashlib.sha256(raw).hexdigest()} for name, raw in pair.items()]
                target = 'sha256:' + 'a' * 64
                calls = []

                def completion(arguments, **options):
                    body = json.loads(options['input']); calls.append(body)
                    self.assertEqual(arguments[-2:], ['deploy-target', 'komizo-box submit'])
                    if body['action'] == 'inspect':
                        value = {'kind': 'komizo.instance', 'version': 1, 'application': 'mailroom',
                            'instance': 'production', 'installed': {'release_digest': target,
                                'desired_running': True, 'operation_id': 'c' * 32}}
                    else:
                        self.assertEqual(body['action'], 'backup')
                        self.assertEqual(body['release_digest'], target)
                        value = {'kind': 'komizo.operation', 'version': 1, 'application': 'mailroom',
                            'instance': 'production', 'release_digest': target,
                            'operation_id': submission.proposed_operation(body), 'phase': 'ready',
                            'ok': True, 'reconciliation_required': False,
                            'backup': {'status': 'ready', 'release_digest': target, 'files': inventory}}
                    return json.dumps(value).encode() + b'\n'

                def download(config, body, expected, destination, **kwargs):
                    self.assertEqual(body['action'], 'export')
                    self.assertEqual(body['operation'], submission.proposed_operation(calls[-1]))
                    header = {'kind': 'komizo.export', 'version': 1, 'operation_id': body['operation'],
                        **{key: body[key] for key in ['application', 'instance', 'release_digest', 'export_name']},
                        'size_bytes': expected['size_bytes'], 'sha256': expected['sha256']}
                    raw = json.dumps(header).encode() + b'\n' + pair[expected['name']]
                    source = [sys.executable, '-I', '-B', '-c',
                        'import base64,sys;sys.stdin.buffer.read();sys.stdout.buffer.write(base64.b64decode("'
                        + __import__('base64').b64encode(raw).decode() + '"))']
                    real_download(config, body, expected, destination, **kwargs, arguments=source)

                with patch.dict(os.environ, {'GITHUB_OUTPUT': str(root / 'outputs')}), \
                        patch.object(submission, 'request', side_effect=lambda *args, **kwargs:
                            real_request(*args, **kwargs, token_request=lambda *_: 'synthetic.proof.signature')), \
                        patch.object(submission, 'submit', side_effect=lambda config, body, **kwargs:
                            real_submit(config, body, **kwargs, execute=completion)), \
                        patch.object(backup_submission, 'download', side_effect=download):
                    if type(format_value) is int and format_value == 2:
                        self.assertEqual(backup_submission.capture('mailroom', root, environment, timeout=20), 'export')
                        self.assertEqual((root / 'export/snapshot.cms').read_bytes(), self.ciphertext)
                        self.assertEqual((root / 'export/receipt.json').stat().st_mode & 0o777, 0o600)
                        self.assertNotIn(self.plaintext, (root / 'export/snapshot.cms').read_bytes())
                    else:
                        with self.assertRaisesRegex(ValueError, 'invalid envelope identity'):
                            backup_submission.capture('mailroom', root, environment, timeout=20)
                self.assertEqual([body['action'] for body in calls], ['inspect', 'backup'])

    def bundle(self,artifact,receipt=None,ciphertext=None,extra=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream,'w') as bundle:
            bundle.writestr('snapshot.cms',self.ciphertext if ciphertext is None else ciphertext)
            bundle.writestr('receipt.json',json.dumps(self.receipt if receipt is None else receipt))
            if extra is not None: bundle.writestr(extra,'unexpected')
        data = stream.getvalue()
        artifact.update(digest='sha256:'+hashlib.sha256(data).hexdigest(),size_in_bytes=len(data))
        return data

    def verify(self,root,run,artifact,data):
        def api(path): return run if '/runs/' in path else artifact
        def download(command,**kwargs):
            self.assertEqual(command,['gh','api','repos/example/project/actions/artifacts/456/zip'])
            kwargs['stdout'].write(data)
        with patch.dict(os.environ,{'GITHUB_REPOSITORY':'example/project','SOURCE_RUN_ID':'123',
                'SOURCE_ARTIFACT_ID':'456'}),patch.object(evidence,'api',side_effect=api), \
                patch.object(evidence.subprocess,'run',side_effect=download),redirect_stdout(io.StringIO()):
            evidence.recovery(root,self.tools)

    def published_artifacts(self, root, workflow, role):
        """Real atomic producer output, archived relative to upload search roots.

        actions/upload-artifact excludes directory entries and roots the ZIP at
        the least common search ancestor, including missing explicit file paths.
        Only the host capture itself is substituted with our real sealed fixture.
        """
        sys.path.insert(0, str(ROOT / 'scripts'))
        import backup
        def capture(project, stage, environment, **kwargs):
            self.assertEqual(project, 'mailroom')
            if role == 'export':
                output = stage / role
                output.mkdir(mode=0o700)
                (output / 'receipt.json').write_text(json.dumps(self.receipt))
                (output / 'snapshot.cms').write_bytes(self.ciphertext)
            else:
                (stage / role).write_text(json.dumps({'synthetic_status': role}))
            return role
        if role is None:
            values = {'has-export': ''}
            (root / '.artifacts/backup').mkdir(parents=True)
        else:
            outputs = root / 'capture.outputs'
            with chdir(root), patch('backup_submission.capture', side_effect=capture), \
                    patch.dict(os.environ, {'GITHUB_OUTPUT': str(outputs)}, clear=True):
                backup.export('mailroom', {'version': 1}, self.tools, Path('.artifacts/backup'), os.environ)
            values = dict(line.split('=', 1) for line in outputs.read_text().splitlines())
        # Unrelated retained diagnostics must never enter the encrypted pair.
        (root / '.artifacts/backup/diagnostic.json').write_text('{}')
        (root / '.artifacts/komizo-operation.json').write_text('{}')
        job = 'backup' if workflow == 'backup' else 'deliver'
        steps = yaml.safe_load((ROOT / f'.github/workflows/cookbook-{workflow}.yml').read_text())['jobs'][job]['steps']
        artifacts = {}
        for step in steps:
            if not step.get('uses', '').startswith('actions/upload-artifact@'):
                continue
            condition = step.get('if', '')
            if condition not in {'', 'always()'}:
                match = re.fullmatch(r"\$\{\{ always\(\) && steps\.(capture|backup)\.outputs\.has-export == '(true|false)' \}\}", condition)
                self.assertIsNotNone(match, 'preserve explicit completed-capture artifact guards')
                self.assertEqual(match[1], 'capture' if job == 'backup' else 'backup')
                if values['has-export'] != match[2]:
                    continue
            name = step['with']['name'].replace('${{ github.run_id }}', '123').replace(
                '${{ github.run_attempt }}', '1').replace('${{ inputs.source }}', 'a' * 40).replace(
                "${{ steps.plan.outputs['artifact-name'] }}", 'backup-123-1')
            paths = [root / value for value in step['with']['path'].splitlines()]
            search_root = Path(os.path.commonpath([str(path if path.is_dir() else path.parent) for path in paths]))
            files = [file for path in paths for file in (path.rglob('*') if path.is_dir() else [path]) if file.is_file()]
            self.assertEqual(step['with']['include-hidden-files'], True)
            self.assertTrue(files, 'declared completed output must not silently disappear')
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                for file in sorted(files):
                    archive.write(file, file.relative_to(search_root))
            artifacts[name] = stream.getvalue()
        return artifacts

    def test_both_workflow_producers_round_trip_atomic_format2_pair_through_strict_reader(self):
        for workflow, path, event in [('backup', 'backup', 'schedule'), ('deliver', 'cd', 'push')]:
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                outputs = self.published_artifacts(root, workflow, 'export')
                _, run, artifact = self.facts()
                run.update(path=f'.github/workflows/{path}.yml', event=event)
                self.assertIn(artifact['name'], outputs)
                data = outputs[artifact['name']]
                artifact.update(digest='sha256:' + hashlib.sha256(data).hexdigest(), size_in_bytes=len(data))
                self.verify(root, run, artifact, data)
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    self.assertEqual(sorted(archive.namelist()), ['receipt.json', 'snapshot.cms'])
                    self.assertEqual(archive.read('snapshot.cms'), self.ciphertext)
                report = json.loads((root / '.artifacts/recovery/transport.json').read_text())
                self.assertTrue(report['transport_verified'])
                self.assertFalse(report['metadata_authenticated'])
                self.assertFalse(report['database_restored'])
                self.assertEqual(report['ciphertext_sha256'], self.receipt['ciphertext_sha256'])

    def test_absent_stopped_and_unattempted_captures_never_claim_recoverable_backup(self):
        for workflow in ['backup', 'deliver']:
            with self.subTest(workflow=workflow, role='not-attempted'), tempfile.TemporaryDirectory() as directory:
                outputs = self.published_artifacts(Path(directory), workflow, None)
                self.assertNotIn('backup-123-1', outputs)
                self.assertNotIn('backup-status-123-1', outputs)
            for role in ['first-deployment.json', 'stopped.json']:
                with self.subTest(workflow=workflow, role=role), tempfile.TemporaryDirectory() as directory:
                    outputs = self.published_artifacts(Path(directory), workflow, role)
                    self.assertNotIn('backup-123-1', outputs)
                    data = outputs['backup-status-123-1']
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        self.assertEqual(archive.namelist(), [role])
                    repo, run, artifact = self.facts()
                    artifact['name'] = 'backup-status-123-1'
                    with self.assertRaisesRegex(ValueError, 'not an encrypted backup'):
                        evidence.provenance(run, artifact, repo, '123', '456')

    def test_nested_and_mixed_old_artifacts_remain_refused(self):
        for kind in ['nested-export', 'mixed-activation', 'delivery-name']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, run, artifact = self.facts()
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, 'w') as archive:
                    prefix = 'export/' if kind == 'nested-export' else ''
                    archive.writestr(prefix + 'receipt.json', json.dumps(self.receipt))
                    archive.writestr(prefix + 'snapshot.cms', self.ciphertext)
                    if kind == 'mixed-activation':
                        archive.writestr('komizo-operation.json', '{}')
                data = stream.getvalue()
                if kind == 'delivery-name':
                    artifact['name'] = 'delivery-' + 'a' * 40 + '-123-1'
                artifact.update(digest='sha256:' + hashlib.sha256(data).hexdigest(), size_in_bytes=len(data))
                with self.assertRaises(ValueError):
                    self.verify(root, run, artifact, data)
                self.assertFalse((root / '.artifacts/recovery/transport.json').exists())

    def test_exact_run_artifact_repository_and_main_provenance(self):
        repo,run,artifact = self.facts()
        evidence.provenance(run,artifact,repo,'123','456')
        for kind,field,value in [('run','id',124),('run','head_branch','untrusted'),
                ('run','status','in_progress'),('run','conclusion','failure'),
                ('run','path','.github/workflows/other.yml'),('run','event','pull_request'),
                ('artifact','id',457),('artifact','expired',True),('artifact','name','arbitrary'),
                ('artifact','digest','missing')]:
            r,a = copy.deepcopy(run),copy.deepcopy(artifact)
            (r if kind=='run' else a)[field] = value
            with self.assertRaises(ValueError): evidence.provenance(r,a,repo,'123','456')
        for key in ['repository','head_repository']:
            r = copy.deepcopy(run)
            r[key]['full_name'] = 'other/project'
            with self.assertRaises(ValueError): evidence.provenance(r,artifact,repo,'123','456')
        for value in ['0','01','-1','main','1'*21]:
            with self.assertRaises(ValueError): evidence.positive(value)

    def test_real_format2_action_cli_ignores_ambient_tools_and_never_decrypts(self):
        repo,run,artifact = self.facts()
        data = self.bundle(artifact)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root/'bin'
            binary.mkdir()
            (root/'run.json').write_text(json.dumps(run))
            (root/'artifact.json').write_text(json.dumps(artifact))
            (root/'artifact.zip').write_bytes(data)
            gh = binary/'gh'
            gh.write_text('#!'+sys.executable+'\n'+'''import json,os,sys
from pathlib import Path
root=Path(os.environ['TRANSPORT_FIXTURE'])
command=sys.argv[1:]
with (root/'requests.jsonl').open('a') as trace:trace.write(json.dumps(command)+'\\n')
assert command[0]=='api'
paths={'repos/example/project/actions/runs/123':'run.json',
       'repos/example/project/actions/artifacts/456':'artifact.json',
       'repos/example/project/actions/artifacts/456/zip':'artifact.zip'}
sys.stdout.buffer.write((root/paths[command[1]]).read_bytes())
''')
            gh.chmod(0o700)
            openssl = binary/'openssl'
            openssl.write_text('#!'+sys.executable+'\nimport os\nfrom pathlib import Path\n'
                'Path(os.environ["TRANSPORT_FIXTURE"],"decryption-attempt").touch()\nraise SystemExit(81)\n')
            openssl.chmod(0o700)
            poison = root/'ambient/helpers'
            poison.mkdir(parents=True)
            (poison/'receive-backup.py').write_text('raise AssertionError("ambient helper executed")\n')
            environment = dict(os.environ,GITHUB_ACTIONS='true',GITHUB_REF='refs/heads/main',
                GITHUB_REPOSITORY=repo,SOURCE_RUN_ID='123',SOURCE_ARTIFACT_ID='456',
                PATH=str(binary)+os.pathsep+os.environ['PATH'],TRANSPORT_FIXTURE=str(root),
                TOOLS_ROOT=str(poison.parent),BACKUP_RECOVERY_KEY_FILE=str(root/'unavailable-owner-key'))
            result = subprocess.run([sys.executable,'-I','-B',str(ROOT/'recovery-evidence/evidence.py'),
                '--tools-root',str(self.tools)],cwd=root,env=environment,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            report = json.loads((root/'.artifacts/recovery/transport.json').read_text())
            self.assertTrue(report['transport_verified'])
            self.assertFalse(report['metadata_authenticated'])
            self.assertFalse(report['database_restored'])
            self.assertEqual(report['source_revision'],'a'*40)
            self.assertEqual(report['ciphertext_sha256'],self.receipt['ciphertext_sha256'])
            self.assertEqual(json.loads(result.stdout),report)
            self.assertNotIn(self.plaintext.decode(),result.stdout+result.stderr)
            self.assertFalse((root/'decryption-attempt').exists())
            self.assertEqual(len((root/'requests.jsonl').read_text().splitlines()),3)

    def test_exact_action_and_ci_wiring_select_the_accepted_reader(self):
        action = yaml.safe_load((ROOT/'recovery-evidence/action.yml').read_text())
        checkout,verify = action['runs']['steps']
        self.assertEqual(checkout['uses'],'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1')
        self.assertEqual(checkout['with']['repository'],'nicodes/tools')
        self.assertEqual(checkout['with']['ref'],evidence.TOOLS_REVISION)
        self.assertFalse(checkout['with']['persist-credentials'])
        self.assertEqual(verify['env']['TRANSPORT_TOOLS_DIRECTORY'],
            '${{ github.workspace }}/'+checkout['with']['path'])
        self.assertIn('python3 -I -B ',verify['run'])
        self.assertIn('--tools-root "$TRANSPORT_TOOLS_DIRECTORY"',verify['run'])
        self.assertNotIn('TOOLS_ROOT',str(action))
        job = yaml.safe_load((ROOT/'.github/workflows/ci.yml').read_text())['jobs']['ci']
        prepared = next(step for step in job['steps']
            if step.get('with',{}).get('path')==checkout['with']['path'])
        self.assertEqual(prepared['with']['ref'],evidence.TOOLS_REVISION)
        self.assertEqual(job['env']['RECOVERY_TEST_TOOLS'],'${{ github.workspace }}/'+prepared['with']['path'])

    def test_mutated_reader_refuses_before_github_or_module_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root/'helpers/receive-backup.py'
            helper.parent.mkdir()
            marker = root/'must-not-execute'
            helper.write_text('from pathlib import Path\nPath('+repr(str(marker))+').touch()\n')
            with patch.object(evidence,'api') as api:
                with self.assertRaisesRegex(ValueError,'reviewed Tools'): evidence.recovery(root,root)
                api.assert_not_called()
            self.assertFalse(marker.exists())
            helper.unlink()
            helper.symlink_to(self.tools/'helpers/receive-backup.py')
            with self.assertRaisesRegex(ValueError,'link or reparse'): evidence.receiver(root)
            helper.unlink()
            helper.write_bytes(b'x'*(32*1024+1))
            with self.assertRaisesRegex(ValueError,'bounded regular'): evidence.receiver(root)
            helper.unlink()
            helper.mkdir()
            with self.assertRaisesRegex(ValueError,'bounded regular'): evidence.receiver(root)
            with self.assertRaisesRegex(ValueError,'absolute'): evidence.receiver(Path('relative'))

    def test_outer_digest_ciphertext_and_unsupported_receipt_refuse_without_report(self):
        for kind in ['artifact-digest','ciphertext','format1','boolean-format','extra-member']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _,run,artifact = self.facts()
                receipt = copy.deepcopy(self.receipt)
                payload = self.ciphertext
                if kind=='format1': receipt['format']=1
                if kind=='boolean-format': receipt['format']=True
                if kind=='ciphertext': payload=payload[:-1]+bytes([payload[-1]^1])
                data = self.bundle(artifact,receipt,payload,extra='plaintext.db' if kind=='extra-member' else None)
                if kind=='artifact-digest': artifact['digest']='sha256:'+'f'*64
                with self.assertRaises(ValueError): self.verify(root,run,artifact,data)
                self.assertFalse((root/'.artifacts/recovery/transport.json').exists())

    def test_transport_does_not_promote_replaced_plaintext_metadata_to_authenticated(self):
        _,run,artifact = self.facts()
        receipt = dict(self.receipt,image_id='sha256:'+'d'*64,
            image_reference='ghcr.io/example/unauthenticated@sha256:'+'e'*64)
        data = self.bundle(artifact,receipt)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.verify(root,run,artifact,data)
            report = json.loads((root/'.artifacts/recovery/transport.json').read_text())
            self.assertFalse(report['metadata_authenticated'])
            self.assertFalse(report['database_restored'])
            self.assertNotIn('image_id',report)
            self.assertNotIn('image_reference',report)

    def test_two_gib_capacity_refuses_before_download(self):
        repo,run,artifact = self.facts()
        artifact['size_in_bytes'] = 2*1024**3+1
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,
                {'GITHUB_REPOSITORY':repo,'SOURCE_RUN_ID':'123','SOURCE_ARTIFACT_ID':'456'}), \
                patch.object(evidence,'api',side_effect=[run,artifact]), \
                patch.object(evidence.subprocess,'run') as download:
            with self.assertRaisesRegex(ValueError,'bounded runner capacity'):
                evidence.recovery(Path(directory),self.tools)
            download.assert_not_called()
            self.assertFalse((Path(directory)/'.artifacts/recovery/transport.json').exists())

    def test_untrusted_event_refuses_before_any_github_request(self):
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'true','GITHUB_REF':'refs/pull/1/merge'}), \
                patch.object(sys,'argv',['evidence.py','--tools-root',str(self.tools)]), \
                patch.object(evidence,'api') as api:
            with self.assertRaisesRegex(ValueError,'trusted main'): evidence.main()
            api.assert_not_called()


if __name__=='__main__': unittest.main()
