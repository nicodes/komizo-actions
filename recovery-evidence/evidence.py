#!/usr/bin/env python3
"""Verify immutable GitHub backup transport; never decrypt or restore a database."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import types
import zipfile

TOOLS_REVISION = '7873bb0fdae1726295dbf7f9317c58daa374b2a6'
RECEIVER_SHA256 = '95a63bf3375035d0869bf02663a48e7e53987da1840d6d95f0df0b0ce99585e4'
MAX_ARTIFACT_BYTES = 2 * 1024**3


def receiver(tools_root):
    """Execute only the exact reviewed stdlib reader, independent of TOOLS_ROOT."""
    if not tools_root.is_absolute():
        raise ValueError('transport SDK requires an absolute checkout path')
    helper = tools_root / 'helpers/receive-backup.py'
    before = helper.lstat()
    if stat.S_ISLNK(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
        raise ValueError('transport reader must not be a link or reparse point')
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 32 * 1024:
        raise ValueError('transport reader must be a bounded regular file')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(helper, flags), 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 32 * 1024 or
                (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError('transport reader must be a bounded regular file')
        raw = source.read(32 * 1024 + 1)
    if hashlib.sha256(raw).hexdigest() != RECEIVER_SHA256:
        raise ValueError('transport reader differs from reviewed Tools ' + TOOLS_REVISION)
    module = types.ModuleType('komizo_receive_backup')
    module.__file__ = str(helper)
    exec(compile(raw, str(helper), 'exec'), module.__dict__)
    return module


def api(path):
    return json.loads(subprocess.check_output(['gh','api',path],text=True,timeout=60))

def positive(value):
    if not re.fullmatch(r'[1-9][0-9]{0,19}', value): raise ValueError('expected a positive immutable run/artifact ID')
    return value

def provenance(run, artifact, repo, run_id, artifact_id):
    if run['id'] != int(run_id) or artifact['id'] != int(artifact_id): raise ValueError('immutable ID mismatch')
    if run['repository']['full_name'] != repo or run['head_repository']['full_name'] != repo:
        raise ValueError('backup source must belong to this repository')
    if run['head_branch'] != 'main' or run['conclusion'] != 'success' or run['status'] != 'completed':
        raise ValueError('backup must come from a successful completed main run')
    if run['path'] not in {'.github/workflows/cd.yml','.github/workflows/backup.yml'}:
        raise ValueError('backup source is not an approved workflow')
    if run['event'] not in {'push','schedule','workflow_dispatch'}: raise ValueError('untrusted backup event')
    if artifact['expired'] or artifact.get('workflow_run',{}).get('id') != int(run_id):
        raise ValueError('backup artifact expired or belongs to another run')
    if not re.fullmatch(r'backup-[0-9]+(?:-[0-9]+)?',artifact['name']):
        raise ValueError('artifact is not an encrypted backup export')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', artifact.get('digest','')):
        raise ValueError('immutable artifact digest missing')


def recovery(root, tools_root):
    module = receiver(tools_root)
    repo=os.environ['GITHUB_REPOSITORY'];run_id=positive(os.environ['SOURCE_RUN_ID']);artifact_id=positive(os.environ['SOURCE_ARTIFACT_ID'])
    run=api(f'repos/{repo}/actions/runs/{run_id}'); artifact=api(f'repos/{repo}/actions/artifacts/{artifact_id}')
    provenance(run,artifact,repo,run_id,artifact_id)
    with tempfile.TemporaryDirectory(prefix='recovery-evidence-') as scratch:
        scratch=Path(scratch);size=artifact['size_in_bytes']
        if size > MAX_ARTIFACT_BYTES or shutil.disk_usage(scratch).free < size*5+512*1024**2:
            raise ValueError('artifact exceeds bounded runner capacity; validate this backup locally')
        archive=scratch/'artifact.zip'
        with archive.open('wb') as stream:
            subprocess.run(['gh','api',f'repos/{repo}/actions/artifacts/{artifact_id}/zip'],stdout=stream,check=True,timeout=900)
        with archive.open('rb') as stream:
            digest = hashlib.file_digest(stream,'sha256').hexdigest()
        if digest != artifact['digest'].split(':',1)[1]:
            raise ValueError('immutable backup artifact digest differs')
        exported=scratch/'export.tar'
        with zipfile.ZipFile(archive) as source,tarfile.open(exported,'w') as target:
            if sorted(source.namelist()) != ['receipt.json','snapshot.cms']:
                raise ValueError('backup artifact must contain only encrypted snapshot and receipt')
            for name in source.namelist():
                entry=source.getinfo(name)
                if entry.file_size > (64*1024 if name=='receipt.json' else MAX_ARTIFACT_BYTES): raise ValueError('backup member exceeds bound')
                member=tarfile.TarInfo(name);member.size=entry.file_size
                with source.open(entry) as stream:target.addfile(member,stream)
        receipt=module.receive(exported,scratch/'checked')
        report={'transport_verified':True,'repository':repo,'run_id':int(run_id),'artifact_id':int(artifact_id),
                'source_revision':run['head_sha'],'ciphertext_sha256':receipt['ciphertext_sha256'],
                'metadata_authenticated':False,'database_restored':False,
                'next_step':'Use the reviewed local restore procedure; private recovery key stays local.'}
        evidence=root/'.artifacts/recovery';evidence.mkdir(parents=True,exist_ok=True)
        (evidence/'transport.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools-root', required=True, type=Path)
    arguments = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('GITHUB_REF')!='refs/heads/main':
        raise ValueError('recovery evidence requires trusted main')
    repository=os.environ.get('GITHUB_REPOSITORY','')
    if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+',repository):
        raise ValueError('invalid caller repository')
    recovery(Path.cwd(), arguments.tools_root)

if __name__=='__main__':main()
