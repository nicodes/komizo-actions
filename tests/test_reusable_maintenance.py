"""Check reusable trust boundaries and run the actual encrypted-upload shell."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[1]
WORKFLOWS=ROOT/'.github/workflows'

def load(name):return yaml.safe_load((WORKFLOWS/name).read_text())

def script(step):
    body=step['run']
    if 'komizo exec' in body or 'komizo run' in body or 'KOMIZO_COMMAND' in body:raise ValueError('Removed runtime wrapper')
    return body

def validate(doc,backup=False):
    events=doc.get('on',doc.get(True))
    if set(events)!={'workflow_call'}:raise ValueError('Reusable adapters may only be called explicitly')
    for name,job in doc['jobs'].items():
        permissions=job['permissions']
        if permissions.get('attestations')!='read':raise ValueError('Archive provenance access required')
        if permissions.get('id-token')=='write' and not(backup and name=='backup'):raise ValueError('OIDC authority belongs only to backup')
        if 'concurrency' in job:raise ValueError('Concurrency belongs to the caller')
        steps=job['steps'];setups=[i for i,s in enumerate(steps) if re.fullmatch('nicodes/tools/helpers@[a-f0-9]{40}',s.get('uses',''))]
        if len(setups)!=1:raise ValueError('One reviewed helper source required')
        for i,step in enumerate(steps):
            if 'run' in step:
                if i<setups[0]:raise ValueError('Authenticate before executing commands')
                script(step)
    if backup:
        steps=doc['jobs']['backup']['steps'];proof=next(i for i,s in enumerate(steps) if 'nicodes/komizo-actions/backup-proof@' in s.get('uses',''));export=next(i for i,s in enumerate(steps) if 'bash scripts/export-backup.sh' in s.get('run',''));upload=next(i for i,s in enumerate(steps) if 'upload-backup.py' in s.get('run',''))
        if not proof<export<upload:raise ValueError('Bounded proof and encryption must precede upload')
        if any('if' in steps[i] for i in [proof,export,upload]):raise ValueError('Required backup phases cannot be conditional')
        if '${{ inputs.product }}' in steps[upload]['run'] or steps[upload]['env'].get('BACKUP_PRODUCT')!='${{ inputs.product }}':raise ValueError('Product input must enter only through an environment value')

class ReusableMaintenance(unittest.TestCase):
    def test_native_setup_roles_and_required_backup_phases(self):
        for name in ['reusable-backup.yml']:validate(load(name),backup=name=='reusable-backup.yml')
        backup=load('reusable-backup.yml')
        for mutate in [lambda d:d['jobs']['report']['permissions'].update({'id-token':'write'}),lambda d:d['jobs']['backup']['steps'].pop(1),lambda d:d['jobs']['backup'].update({'concurrency':'caller-deadlock'}),lambda d:next(s for s in d['jobs']['backup']['steps'] if 'nicodes/komizo-actions/backup-proof@' in s.get('uses','')).update({'if':'false'})]:
            bad=copy.deepcopy(backup);mutate(bad)
            with self.assertRaises(ValueError):validate(bad,backup=True)

    def test_upload_shell_refuses_injected_slug_without_executing_it(self):
        doc=load('reusable-backup.yml');step=next(s for s in doc['jobs']['backup']['steps'] if 'upload-backup.py' in s.get('run',''))
        with tempfile.TemporaryDirectory(prefix='maintenance fixture ') as directory:
            root=Path(directory);export=root/'.artifacts/backup/export';export.mkdir(parents=True)
            (export/'snapshot.cms').write_bytes(b'synthetic ciphertext')
            (export/'receipt.json').write_text(json.dumps({'verified_at':'2026-10-26T12:00:00Z'}))
            marker=root/'must-not-execute'
            env={**os.environ,'TOOLS_ROOT':os.environ['TOOLS_ROOT'],'BACKUP_PRODUCT':'x"; touch '+str(marker)+'; #','BACKUP_ALLOWED_SLUGS':'["fixture"]'}
            result=subprocess.run(['bash','-euo','pipefail','-c',script(step)],env=env,cwd=root,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0);self.assertIn('slug',result.stderr);self.assertFalse(marker.exists())
            (export/'receipt.json').write_text(json.dumps({'verified_at':'bad-timestamp'}))
            result=subprocess.run(['bash','-euo','pipefail','-c',script(step)],env=env,cwd=root,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0);self.assertIn('invalid receipt verified_at',result.stderr);self.assertFalse(marker.exists())

if __name__=='__main__':unittest.main()
