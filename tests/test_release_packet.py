import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import oci_registry as registry
import release_packet as packet


def descriptor(payload=b'archive bytes', revision='a' * 40):
    identity = registry.digest(payload)
    return {'kind': 'komizo.release', 'version': 1, 'application': 'mailroom',
            'providers': {'cookbook_revision': 'c'*40, 'tools_revision': 'e'*40, 'core_revision': 'f'*40},
            'source': {'repository': 'example/mailroom', 'repository_id': '123', 'revision': revision},
            'artifacts': {'web': {'kind': 'archive', 'reference': 'ghcr.io/example/mailroom-web@' + registry.digest(registry.artifact_manifest(identity, len(payload))),
                                   'digest': identity, 'size_bytes': len(payload), 'media_type': registry.STATIC_LAYER}},
            'workload': {'assets': {'web': {'artifact': 'web'}}, 'services': {}, 'jobs': {}, 'routes': []}}


def make_packet(root, payload=b'archive bytes'):
    (root / 'static').mkdir()
    (root / 'static/web.tar.gz').write_bytes(payload)
    (root / 'release.json').write_bytes(registry.canonical(descriptor(payload)))
    files = {}
    for name in ['release.json', 'static/web.tar.gz']:
        raw = (root / name).read_bytes()
        files[name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw), 'type': packet.member_type(name)}
    (root / 'inventory.json').write_bytes(registry.canonical({'version': 1, 'files': files}))
    return root


class ClosedPacket(unittest.TestCase):
    def test_static_application_has_no_image_or_configuration_container(self):
        with tempfile.TemporaryDirectory() as temp:
            admitted = packet.Packet(make_packet(Path(temp)))
            self.assertEqual(admitted.release_digest, registry.digest(registry.canonical(descriptor())))
            proof = {'artifacts': {'.artifacts/release/' + name: hashlib.sha256((Path(temp) / name).read_bytes()).hexdigest()
                                   for name in [*admitted.files, 'inventory.json']}}
            admitted.require_proof(proof)
            proof['artifacts'].pop('.artifacts/release/inventory.json')
            with self.assertRaises(ValueError): admitted.require_proof(proof)

    def test_extra_payload_and_unexpected_directories_are_refused(self):
        for name in ['unaccepted', 'images.tar.gz', 'images.json', 'nested/file']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = make_packet(Path(temp))
                extra = root / name
                extra.parent.mkdir(parents=True, exist_ok=True)
                extra.write_bytes(b'outside closed inventory')
                with self.assertRaises(ValueError): packet.Packet(root)

    def test_replaced_or_changed_bytes_refuse_after_admission(self):
        for name in ['inventory.json', 'release.json', 'static/web.tar.gz']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = make_packet(Path(temp)); admitted = packet.Packet(root)
                raw = (root / name).read_bytes()
                (root / name).write_bytes(raw + b' ')
                with self.assertRaises(ValueError): admitted.unchanged()

    def test_symlinks_hardlinks_and_fifo_are_refused_without_waiting(self):
        for kind in ['symlink', 'hardlink', 'fifo']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = make_packet(Path(temp)); target = root / 'static/web.tar.gz'; target.unlink()
                external = root.parent / (root.name + '-external'); external.write_bytes(b'archive bytes')
                try:
                    if kind == 'symlink': target.symlink_to(external)
                    elif kind == 'hardlink': os.link(external, target)
                    else: os.mkfifo(target)
                    with self.assertRaises((ValueError, OSError)): packet.Packet(root)
                finally: external.unlink()

    def test_duplicate_keys_and_static_manifest_rebinding_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = make_packet(Path(temp))
            (root / 'inventory.json').write_text('{"version":1,"version":1,"files":{}}')
            with self.assertRaises(ValueError): packet.Packet(root)
        value = descriptor(); value['artifacts']['web']['reference'] = 'ghcr.io/example/mailroom-web@sha256:' + '0'*64
        with self.assertRaises(ValueError): packet.release(value)


if __name__ == '__main__': unittest.main()
