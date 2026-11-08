#!/usr/bin/env python3
"""Read the public Godot identity from the exact same-job accepted static packet."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oci_registry
from release_packet import Packet, document, read, regular


def identity(packet_root, evidence_root, *, deadline=None):
    deadline = time.monotonic() + 120 if deadline is None else deadline
    packet = Packet(packet_root, timeout=120)
    evidence_root = Path(evidence_root)
    if (read(evidence_root / 'release.json') != packet.release_bytes
            or read(evidence_root / 'inventory.json') != packet.inventory_raw):
        raise ValueError('public identity requires the exact admitted publication packet')
    published = document(evidence_root / 'publication.json')
    if (published.get('kind') != 'komizo.publication' or published.get('version') != 1
            or published.get('release_digest') != packet.release_digest
            or published.get('source') != packet.descriptor['source']):
        raise ValueError('public identity differs from its admitted publication')
    packet.require_proof(document(evidence_root / 'validation.json'))
    assets = packet.descriptor['workload'].get('assets', {})
    if (len(assets) != 1 or packet.descriptor['workload'].get('services')
            or packet.descriptor['workload'].get('jobs')):
        raise ValueError('public Godot identity requires one static-only asset')
    artifact = next(iter(assets.values()))['artifact']
    if packet.descriptor['artifacts'][artifact]['kind'] != 'archive':
        raise ValueError('public Godot identity requires an accepted archive')
    found = None
    seen = set()
    total = 0
    with regular(packet.root / ('static/' + artifact + '.tar.gz')) as (source, _):
        with tarfile.open(fileobj=source, mode='r|gz') as archive:
            for entry in archive:
                if time.monotonic() >= deadline:
                    raise ValueError('public identity exceeded its original delivery deadline')
                path = PurePosixPath(entry.name)
                if (len(seen) >= 20000 or entry.name in seen or str(path) != entry.name
                        or path.is_absolute() or not path.parts or '..' in path.parts
                        or any(ord(char) < 32 for char in entry.name)
                        or not (entry.isfile() or entry.isdir()) or entry.size < 0
                        or entry.size > 128 << 20 or entry.pax_headers):
                    raise ValueError('accepted static archive contains unsafe public metadata')
                seen.add(entry.name)
                total += entry.size
                if total > 1 << 30:
                    raise ValueError('public identity archive exceeds its expansion bound')
                if entry.name == 'release-manifest.json':
                    if not entry.isfile() or not 0 < entry.size <= 1 << 20:
                        raise ValueError('public release manifest exceeds its bound')
                    raw = archive.extractfile(entry).read((1 << 20) + 1)
                    if len(raw) != entry.size:
                        raise ValueError('public manifest differs from its archived size')
                    found = json.loads(raw, object_pairs_hook=oci_registry.unique_object)
    if (not isinstance(found, dict) or found.get('source_commit') != packet.descriptor['source']['revision']
            or not isinstance(found.get('release_id'), str) or not re.fullmatch('[a-f0-9]{64}', found['release_id'])):
        raise ValueError('public manifest does not name the accepted source and immutable payload')
    packet.unchanged()
    return found['release_id']


def main():
    try:
        remaining = float(os.environ['KOMIZO_OPERATION_DEADLINE']) - time.time()
        if remaining <= 0:
            raise ValueError('original delivery deadline expired')
        value = identity(Path('.artifacts/release'), Path('.artifacts/komizo-release'),
                         deadline=time.monotonic() + min(remaining, 120))
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('release-id=' + value + '\n')
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError):
        raise SystemExit('Accepted public payload identity could not be established.') from None


if __name__ == '__main__':
    main()
