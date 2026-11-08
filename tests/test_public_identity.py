import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import oci_registry
import public_identity
from test_release_packet import make_packet


def fixture(root, *, source='a'*40, duplicate=False, link=False):
    content=io.BytesIO()
    manifest=json.dumps({'release_id':'b'*64,'source_commit':source}).encode()
    with tarfile.open(fileobj=content,mode='w:gz',format=tarfile.USTAR_FORMAT) as archive:
        for _ in range(2 if duplicate else 1):
            entry=tarfile.TarInfo('release-manifest.json');entry.size=len(manifest)
            if link:entry.type=tarfile.SYMTYPE;entry.linkname='/outside';entry.size=0
            archive.addfile(entry,None if link else io.BytesIO(manifest))
    (root/'packet').mkdir()
    packet=make_packet(root/'packet',content.getvalue())
    evidence=root/'evidence';evidence.mkdir()
    for name in ['release.json','inventory.json']:(evidence/name).write_bytes((packet/name).read_bytes())
    descriptor=json.loads((packet/'release.json').read_text())
    (evidence/'publication.json').write_bytes(oci_registry.canonical({'kind':'komizo.publication','version':1,
        'source':descriptor['source'],'release_digest':oci_registry.digest((packet/'release.json').read_bytes())}))
    proof={'.artifacts/release/'+name:hashlib.sha256((packet/name).read_bytes()).hexdigest()
           for name in ['release.json','inventory.json','static/web.tar.gz']}
    (evidence/'validation.json').write_bytes(oci_registry.canonical({'artifacts':proof}))
    return packet,evidence


class PublicIdentityTests(unittest.TestCase):
    def test_reads_the_frozen_payload_without_remote_page_or_registry_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            packet,evidence=fixture(Path(directory))
            self.assertEqual(public_identity.identity(packet,evidence),'b'*64)

    def test_rejects_wrong_source_duplicate_manifest_and_links(self):
        for options in [{'source':'c'*40},{'duplicate':True},{'link':True}]:
            with self.subTest(options=options),tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):public_identity.identity(*fixture(Path(directory),**options))

    def test_rejects_changed_evidence_or_incomplete_acceptance(self):
        for name in ['release.json','inventory.json','validation.json']:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as directory:
                packet,evidence=fixture(Path(directory))
                (evidence/name).write_text('{}')
                with self.assertRaises(ValueError):public_identity.identity(packet,evidence)

    def test_original_deadline_applies_to_archive_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):public_identity.identity(*fixture(Path(directory)),deadline=0)


if __name__=='__main__':unittest.main()
