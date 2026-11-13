"""Join real stage publication, request/receipt framing, downloads and pair admission."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import backup_submission as receiver
import github_identity
import oci_registry
import release_packet
import submission
from test_submission import ENV

spec = importlib.util.spec_from_file_location('joined_backup_transport',
    Path(__file__).resolve().parents[1] / 'scripts/backup_transport.py')
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)

# A real child pipe protocol stands in for the remote SSH service. No download,
# filesystem admission, framing, hash or publication implementation is mocked.
SSH = r'''
import base64,hashlib,json,sys
request=json.loads(sys.stdin.buffer.read())
pair={n:base64.b64decode(v) for n,v in json.loads(sys.argv[1]).items()}
if request['action']=='inspect':
 value={'kind':'komizo.instance','version':1,'application':request['application'],
        'instance':request['instance'],'installed':{'release_digest':'sha256:'+'a'*64,
        'desired_running':True,'operation_id':'c'*32}}
elif request['action']=='backup':
 op=hashlib.sha256(('komizo-operation-v1\0'+request['application']+'\0'+request['instance']+'\0'+request['nonce']).encode()).hexdigest()[:32]
 value={'kind':'komizo.operation','version':1,'application':request['application'],
        'instance':request['instance'],'release_digest':request['release_digest'],
        'operation_id':op,'phase':'ready','ok':True,'reconciliation_required':False,
        'backup':{'status':'ready','release_digest':request['release_digest'],
        'files':[{'name':n,'size_bytes':len(b),'sha256':'sha256:'+hashlib.sha256(b).hexdigest()} for n,b in pair.items()]}}
else:
 body=pair[request['export_name']]
 value={'kind':'komizo.export','version':1,'operation_id':request['operation'],
        'application':request['application'],'instance':request['instance'],
        'release_digest':request['release_digest'],'export_name':request['export_name'],
        'size_bytes':len(body),'sha256':'sha256:'+hashlib.sha256(body).hexdigest()}
sys.stdout.buffer.write(json.dumps(value,separators=(',',':')).encode()+b'\n')
if request['action']=='export':sys.stdout.buffer.write(body)
'''


class OIDCConnection:
    """Only external identity minting is synthetic; request construction stays real."""
    def __init__(self, *args, **kwargs): pass
    def request(self, method, path, headers):
        assert method == 'GET' and 'audience=komizo-request' in path
    def getresponse(self): return self
    status = 200
    def read(self, maximum): return b'{"value":"opaque.fixture.proof"}'
    def close(self): pass


class JoinedCapture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'ssh-config'
        self.config.write_text('Host deploy-target\n')
        self.config.chmod(0o600)
        self.cms = b'opaque synthetic CMS fixture; no plaintext or private key'
        self.pair = {'snapshot.cms': self.cms,
            'receipt.json': oci_registry.canonical({'format': 2,
                'archive_sha256': 'b' * 64, 'ciphertext_sha256': hashlib.sha256(self.cms).hexdigest(),
                'image_id': 'sha256:' + 'd' * 64})}
        self.environment = ENV | {'KOMIZO_SSH_CONFIG': str(self.config),
            'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://fixture.actions.githubusercontent.com/token',
            'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'nonsecret-fixture'}

    def joined(self, change=None):
        self.mutation_ran = False
        real_popen = subprocess.Popen
        encoded = json.dumps({n: base64.b64encode(b).decode() for n, b in self.pair.items()})
        def ssh(arguments, **options):
            self.assertEqual(arguments[0], 'ssh')
            self.assertEqual(arguments[-1], 'komizo-box submit')
            return real_popen([sys.executable, '-I', '-B', '-c', SSH, encoded], **options)
        def produce(stage, descriptors):
            self.stage = stage
            if change is None:
                return receiver.capture('mailroom', stage, self.environment, pass_fds=descriptors, timeout=15)
            # Mutation is a race fixture, not a replacement for real downloads.
            original = receiver.export_readers
            def changed(*args):
                self.mutation_ran = True
                change(stage)
                return original(*args)
            with patch.object(receiver, 'export_readers', changed):
                return receiver.capture('mailroom', stage, self.environment, pass_fds=descriptors, timeout=15)
        with patch.object(subprocess, 'Popen', ssh), \
                patch.object(github_identity.http.client, 'HTTPSConnection', OIDCConnection):
            return transport.capture(self.root, Path('.artifacts/backup'), 'mailroom', produce, timeout=15)

    def test_real_joined_FD_capture_publishes_the_exact_pair(self):
        self.assertEqual(self.joined(), 'export')
        final = self.root / '.artifacts/backup/export'
        self.assertEqual({p.name for p in final.iterdir()}, set(self.pair))
        for name, body in self.pair.items():
            self.assertEqual((final / name).read_bytes(), body)
            self.assertEqual((final / name).stat().st_mode & 0o777, 0o600)

    def test_final_ciphertext_or_receipt_corruption_refuses_without_publication(self):
        for name in self.pair:
            def change(stage, name=name):
                (stage / 'export' / name).write_bytes(self.pair[name] + b'corrupt')
            with self.subTest(name=name), self.assertRaises(ValueError): self.joined(change)
            self.assertTrue(self.mutation_ran)
            self.assertFalse((self.root / '.artifacts/backup/export').exists())

    def test_final_symlink_or_hardlink_refuses_and_preserves_foreign_bytes(self):
        foreign = self.root / 'foreign'
        for name in self.pair:
            for link in ('symlink', 'hardlink'):
                foreign.write_bytes(self.pair[name])
                def change(stage, name=name, link=link):
                    path = stage / 'export' / name; path.unlink()
                    if link == 'symlink': path.symlink_to(foreign)
                    else: os.link(foreign, path)
                with self.subTest(name=name, link=link), self.assertRaises((OSError, ValueError)):
                    self.joined(change)
                self.assertTrue(self.mutation_ran)
                self.assertFalse((self.root / '.artifacts/backup/export').exists())
                self.assertEqual(foreign.read_bytes(), self.pair[name])

    def test_parent_rename_refuses_and_does_not_touch_replacement_directory(self):
        def change(stage):
            current = self.root / '.artifacts/backup'
            current.rename(current.with_name('retained'))
            current.mkdir(); (current / 'foreign').write_bytes(b'preserve')
        with self.assertRaises(RuntimeError): self.joined(change)
        self.assertTrue(self.mutation_ran)
        self.assertEqual((self.root / '.artifacts/backup/foreign').read_bytes(), b'preserve')
        self.assertFalse((self.root / '.artifacts/retained/export').exists())

    def test_owned_reader_requires_exact_passed_FD_and_strict_regular_stays_strict(self):
        parent = self.root / 'owned'; parent.mkdir(mode=0o700)
        stage = parent / '.export-fixture'; stage.mkdir(mode=0o700)
        (stage / 'export').mkdir(mode=0o700)
        (stage / 'export/snapshot.cms').write_bytes(self.cms)
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            owned = Path('/proc/self/fd') / str(fd) / stage.name
            with self.assertRaises(ValueError):
                with release_packet.regular(owned / 'export/snapshot.cms'): pass
            for path, descriptors in [(owned, (fd + 1000,)), (owned, (fd, fd)),
                                      (parent / stage.name, (fd,)), (owned / '..', (fd,))]:
                with self.subTest(path=path, descriptors=descriptors), self.assertRaises(ValueError):
                    with receiver.export_readers(path, descriptors): pass
        finally: os.close(fd)

    def test_owned_file_and_directory_fingerprints_refuse_changes_during_read(self):
        parent = self.root / 'owned'; parent.mkdir(mode=0o700)
        stage = parent / '.export-fixture'; stage.mkdir(mode=0o700)
        export = stage / 'export'; export.mkdir(mode=0o700)
        target = export / 'snapshot.cms'; target.write_bytes(self.cms)
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            owned = Path('/proc/self/fd') / str(fd) / stage.name
            with self.assertRaises(ValueError):
                with receiver.export_readers(owned, (fd,)) as reader:
                    with reader('snapshot.cms', 1024) as (stream, _):
                        self.assertEqual(stream.read(), self.cms)
                        target.rename(export / 'retained.cms')
                        target.write_bytes(self.cms)
            with self.assertRaises(ValueError):
                with receiver.export_readers(owned, (fd,)):
                    export.rename(stage / 'retained')
                    export.mkdir(mode=0o700)
        finally: os.close(fd)

    def test_owned_special_file_and_symlink_directory_refuse_without_blocking(self):
        parent = self.root / 'owned'; parent.mkdir(mode=0o700)
        stage = parent / '.export-fixture'; stage.mkdir(mode=0o700)
        export = stage / 'export'; export.mkdir(mode=0o700)
        target = export / 'snapshot.cms'; os.mkfifo(target)
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            owned = Path('/proc/self/fd') / str(fd) / stage.name
            with receiver.export_readers(owned, (fd,)) as reader:
                with self.assertRaises(ValueError):
                    with reader('snapshot.cms', 1024): pass
            target.unlink(); export.rmdir(); export.symlink_to(self.root)
            with self.assertRaises(OSError):
                with receiver.export_readers(owned, (fd,)): pass
        finally: os.close(fd)


if __name__ == '__main__': unittest.main()
