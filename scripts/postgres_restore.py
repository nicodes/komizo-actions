#!/usr/bin/env python3
"""Restore sealed or authenticated plaintext PostgreSQL dumps into owned scratch containers.

No live containers/volumes, exposed ports, external Docker endpoints or recovery
keys in containers. The caller declares the reviewed official engine digest.
"""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import tomllib

PIN = re.compile(r'(?:docker\.io/library/)?postgres@sha256:[a-f0-9]{64}')
DOCKER_ENV_EXCLUDE = {'DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'}


def declaration(path, project):
    if not isinstance(project,str) or not re.fullmatch('[a-z][a-z0-9-]{0,62}',project):
        raise ValueError('invalid PostgreSQL recovery project')
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or not path.is_file() or path.stat().st_size>16384:
        raise ValueError('PostgreSQL recovery declaration must be bounded regular data')
    if path.suffix != '.toml':
        raise ValueError('PostgreSQL recovery source must be TOML')
    return validate(tomllib.loads(path.read_text()),project)


def validate(value, project):
    keys={'version','project','image','minimum_rows','maximum_tables'}
    if (not isinstance(project,str) or not re.fullmatch('[a-z][a-z0-9-]{0,62}',project)
            or not isinstance(value,dict) or set(value)!=keys or type(value['version']) is not int or value['version']!=1
            or value['project']!=project or not isinstance(value['image'],str) or not PIN.fullmatch(value['image'])
            or type(value['minimum_rows']) is not int or not 1<=value['minimum_rows']<=1000000000
            or type(value['maximum_tables']) is not int or not 1<=value['maximum_tables']<=500):
        raise ValueError('invalid digest-bound PostgreSQL recovery declaration')
    return value


def docker_environment():
    return {key:value for key,value in os.environ.items() if key not in DOCKER_ENV_EXCLUDE}


class Drill:
    def __init__(self, project, config, tools_root):
        self.project=project
        self.config=validate(config,project)
        self.image=config['image']
        self.tools_root=Path(tools_root)

    def helper(self):
        spec=importlib.util.spec_from_file_location('recovery_snapshot',self.tools_root/'helpers/snapshot.py')
        result=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(result)
        return result

    def row_requirement(self, value):
        if value is None:return self.config['minimum_rows']
        if type(value) is not int or not self.config['minimum_rows']<=value<=1000000000:
            raise ValueError('row requirement cannot weaken the declared recovery check')
        return value

    def docker(self, *args, timeout=120):
        result = subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', *args],
                                capture_output=True, text=True, timeout=timeout, env=docker_environment())
        if result.returncode:
            # Never disclose generated passwords, SQL output or dump contents.
            raise RuntimeError(f'scratch PostgreSQL {args[0]} failed')
        return result.stdout.strip()

    def sql(self, container, statement, timeout=60):
        return self.docker('exec', container, 'psql', '-X', '-qAt', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1',
                      '-c', statement, timeout=timeout)

    def pull_engine(self):
        # Alternate official sources must serve the exact production index.
        # Finish the pull before creating scratch resources or starting readiness.
        digest = self.image.split('@', 1)[1]
        for registry in ['mirror.gcr.io/library/postgres',
                         'public.ecr.aws/docker/library/postgres', 'postgres']:
            candidate = registry + '@' + digest
            try:
                self.docker('pull', candidate, timeout=300)
                return candidate
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
        raise RuntimeError('all pinned official PostgreSQL sources failed')

    @contextmanager
    def scratch_postgres(self):
        """Run one isolated throwaway PostgreSQL and yield its container name."""
        engine = self.pull_engine()
        name = self.project+'-restore-pg-' + secrets.token_hex(8)
        network = self.project+'-restore-net-' + secrets.token_hex(8)
        started = False
        try:
            self.docker('network', 'create', '--internal', network)
            self.docker('run', '-d', '--name', name, '--label', 'nicodes.restore=' + name,
                   '--network', network, '--cap-drop', 'ALL',
                   '--cap-add', 'CHOWN', '--cap-add', 'SETUID', '--cap-add', 'SETGID', '--cap-add', 'FOWNER',
                   '--security-opt', 'no-new-privileges:true', '--memory', '512m', '--pids-limit', '128',
                   '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
                   # postgres 18+ expects the mount at /var/lib/postgresql so data lands
                   # in a major-version subdirectory; /var/lib/postgresql/data confuses it.
                   '--tmpfs', '/var/lib/postgresql:rw,nosuid,size=2g',
                   '-e', 'POSTGRES_PASSWORD=' + secrets.token_hex(32),
                   engine, timeout=600)
            started = True
            deadline = time.monotonic() + 180
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError('scratch PostgreSQL did not become ready')
                if not json.loads(self.docker('inspect', name))[0]['State']['Running']:
                    raise RuntimeError('scratch PostgreSQL exited before becoming ready')
                try:
                    self.docker('exec', name, 'pg_isready', '-U', 'postgres', '-d', 'postgres', timeout=30)
                    # The image's initialization server accepts socket queries but
                    # is about to exit. Wait for the final server, whose listener
                    # is enabled, before issuing the first mutation.
                    if not self.sql(name, 'SHOW listen_addresses', timeout=30).strip():
                        time.sleep(1)
                        continue
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('scratch PostgreSQL did not become ready') from None
                    time.sleep(1)
            yield name
        finally:
            if started:
                owned = json.loads(self.docker('inspect', name))
                if (len(owned) != 1
                        or owned[0].get('Config', {}).get('Labels', {}).get('nicodes.restore') != name
                        or not re.fullmatch(r'[a-f0-9]{64}', owned[0].get('Id', ''))):
                    raise RuntimeError('cannot establish ownership for scratch PostgreSQL cleanup')
                self.docker('rm', '-f', '-v', owned[0]['Id'], timeout=180)
            try:
                self.docker('network', 'rm', network, timeout=120)
            except RuntimeError:
                pass

    def restore_dump(self, container, dump_path):
        """Stream one custom-format dump into the scratch database via binary stdin."""
        with dump_path.open('rb') as stream:
            result = subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', 'exec', '-i', container,
                                     'pg_restore', '--exit-on-error', '--no-owner', '--no-acl',
                                     '--no-tablespaces', '--username=postgres', '--dbname=postgres'],
                                    stdin=stream, capture_output=True, timeout=1800, env=docker_environment())
        if result.returncode:
            raise RuntimeError('scratch PostgreSQL pg_restore failed')

    def verify_restored(self, container, min_rows=None):
        min_rows=self.row_requirement(min_rows)
        tables = int(self.sql(container, "SELECT count(*) FROM pg_tables"
                                  " WHERE schemaname NOT IN ('pg_catalog','information_schema')"))
        if tables < 1:
            raise ValueError('restore verification found no application tables')
        names = json.loads(self.sql(container, "SELECT COALESCE(json_agg(ARRAY[schemaname,tablename] ORDER BY schemaname,tablename),'[]') FROM pg_tables"
                               " WHERE schemaname NOT IN ('pg_catalog','information_schema')"))
        if (not isinstance(names,list) or len(names)!=tables or len(names) > self.config['maximum_tables']
                or any(not isinstance(pair,list) or len(pair)!=2
                       or any(not isinstance(name,str) or not name or len(name)>63 or '\0' in name for name in pair)
                       for pair in names)):
            raise ValueError('restore verification refuses to count that many tables')
        rows = 0
        for pair in names:
            qualified='.'.join('"'+name.replace('"','""')+'"' for name in pair)
            rows += int(self.sql(container, f'SELECT count(*) FROM {qualified}'))
        if rows < min_rows:
            raise ValueError(f'restore verification found only {rows} rows across {tables} tables')
        return {'tables': tables, 'rows': rows}

    def restore_from_dump(self, dump, expect_sha256=None, min_rows=None):
        """Keyless host drill: restore an already-decrypted dump and verify it."""
        self.row_requirement(min_rows)
        if expect_sha256 is not None:
            with dump.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expect_sha256:
                raise ValueError('decrypted dump digest differs from the authenticated receipt')
        with self.scratch_postgres() as container:
            self.restore_dump(container, dump)
            report = self.verify_restored(container, min_rows)
        report['cleanup'] = 'passed'
        return report

    def drill(self, pair, key, min_rows=None):
        """Unseal, verify and restore one sealed pair end to end. The report is redacted."""
        self.row_requirement(min_rows)
        snapshot = self.helper()
        with tempfile.TemporaryDirectory(prefix=self.project+'-restore-drill-') as temporary:
            target = Path(temporary) / 'payload'
            target.mkdir(mode=0o700)
            evidence = snapshot.unseal(pair, key, target)
            if evidence['image_reference'].removeprefix('docker.io/library/') != self.image.removeprefix('docker.io/library/'):
                raise ValueError('recovered engine reference is not the digest-pinned PostgreSQL image')
            with self.scratch_postgres() as container:
                self.restore_dump(container, target / 'database.dump')
                report = self.verify_restored(container, min_rows)
            report.update({'archive_sha256': evidence['archive_sha256'], 'image_reference': evidence['image_reference'],
                           'verified_at': evidence['verified_at'], 'cleanup': 'passed'})
            return report

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',required=True)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--tools-root',required=True,type=Path)
    parser.add_argument('--pair',type=Path,default=Path('.artifacts/restore/download'))
    parser.add_argument('--key',type=Path)
    parser.add_argument('--dump',type=Path)
    parser.add_argument('--expect-sha256')
    parser.add_argument('--min-rows',type=int)
    args=parser.parse_args()
    os.umask(0o077)
    try:
        config=declaration(args.config,args.project)
        driver=Drill(args.project,config,args.tools_root)
        driver.row_requirement(args.min_rows)
        if args.dump is not None:
            if args.key is not None or not re.fullmatch('[a-f0-9]{64}',args.expect_sha256 or ''):
                raise ValueError('keyless recovery requires only a dump and authenticated SHA256')
            report=driver.restore_from_dump(args.dump,args.expect_sha256,args.min_rows)
        else:
            key=args.key or Path(os.environ.get('BACKUP_RECOVERY_KEY_FILE',''))
            if (not key.is_absolute() or key.is_symlink() or not key.is_file() or key.stat().st_mode&0o077):
                raise ValueError('BACKUP_RECOVERY_KEY_FILE must be a private regular absolute local file')
            report=driver.drill(args.pair,key,args.min_rows)
        print(json.dumps(report,indent=2))
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        parser.exit(1,str(error)+'\n')


if __name__=='__main__':main()
