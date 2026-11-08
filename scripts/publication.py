"""Publish an already accepted immutable packet, preserving its OCI identities."""
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil

from github_identity import identity
import oci_registry
from release_packet import Packet, regular, document


def admit_receipts(incoming, proof, source, groups):
    build = document(Path(incoming) / '.artifacts/verification/build.json')
    test = document(Path(incoming) / '.artifacts/verification/test.json')
    return admit_records(build, test, proof, source, groups)


def admit_records(build, test, proof, source, groups):
    if (not isinstance(build, dict) or not isinstance(test, dict)
            or build.get('identity') != test.get('input_identity')
            or not isinstance(build.get('identity'), str)
            or not __import__('re').fullmatch('[a-f0-9]{64}', build['identity'])
            or build.get('artifacts') != proof.get('artifacts')
            or type(test.get('version')) is not int or test['version'] != 1
            or type(build.get('provenance', {}).get('version')) is not int or build['provenance']['version'] != 1
            or build.get('provenance', {}).get('input_identity') != build['identity']
            or test.get('configuration_sha256') != build.get('provenance', {}).get('configuration_sha256')):
        raise ValueError('publication build and tests did not accept identical source and output bytes')
    for record in [build['provenance'], test]:
        if (record.get('source_commit') != source or record.get('source_tree') != proof['source_tree']
                or record.get('tracked_source_clean') is not True
                or record.get('execution_context') != proof['execution_context']):
            raise ValueError('publication receipts belong to another source or execution')
    if (test.get('tested_commit') != source or set(test.get('test_receipts', {})) != set(groups)
            or any(value != {'input_identity': build['identity'], 'source_commit': source}
                   for value in test.get('test_receipts', {}).values())):
        raise ValueError('publication requires every applicable fresh test receipt')
    return build, test


def stage(incoming, proof, destination, application=None):
    admitted = Packet(Path(incoming) / '.artifacts/release', application=application)
    admitted.require_proof(proof)
    destination = Path(destination)
    if destination.exists():
        raise ValueError('publication destination must be fresh')
    destination.mkdir(mode=0o700, parents=True)
    for name in [*admitted.files, 'inventory.json']:
        target = destination / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with regular(admitted.root / name) as (source, _), target.open('xb') as output:
            shutil.copyfileobj(source, output, 1 << 20)
    admitted.unchanged()
    result = Packet(destination, application=application)
    result.require_proof(proof)
    return result


def publish(packet, tools, environment, *, registry_factory=oci_registry.Registry):
    spec = importlib.util.spec_from_file_location('_publisher_oci_archive', Path(tools) / 'helpers/oci_archive.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archives = {}
    # Validate every image before obtaining credentials or creating registry effects.
    with ExitStack() as stack:
        for name, artifact in packet.descriptor['artifacts'].items():
            if artifact['kind'] == 'image':
                archive = stack.enter_context(module.OCIArchive(packet.root / ('images/' + name + '.oci.tar'),
                    expected_reference=artifact['reference'], expected_config=artifact['config_digest']))
                labels = archive.metadata['config'].get('config', {}).get('Labels', {})
                if (archive.metadata['manifest_digest'] != artifact['digest']
                        or archive.metadata['platform'] != artifact['platform']
                        or labels.get('org.opencontainers.image.revision') != packet.descriptor['source']['revision']
                        or labels.get('org.opencontainers.image.source') != 'https://github.com/' + packet.descriptor['source']['repository']):
                    raise ValueError('OCI archive differs from its accepted release descriptor')
                archives[name] = archive
        packet.unchanged()
        username = environment.get('KOMIZO_REGISTRY_USER', '')
        password = environment.get('KOMIZO_REGISTRY_TOKEN', '')
        if not username or not password:
            raise ValueError('publication requires explicit registry authority')
        published = {}
        for name, artifact in packet.descriptor['artifacts'].items():
            server, repository, manifest_digest = oci_registry.reference(artifact['reference'])
            owner = packet.descriptor['source']['repository'].split('/')[0].lower()
            if server != 'ghcr.io' or not repository.startswith(owner + '/'):
                raise ValueError('publication cannot send authority outside its source owner')
            remote = registry_factory(server, repository, username=username, password=password)
            packet.unchanged()
            if artifact['kind'] == 'image':
                archive = archives[name]
                for blob in archive.metadata['blobs']:
                    with archive.open_blob(blob['digest']) as source:
                        remote.upload_blob(blob['digest'], blob['size'], source)
                archive.unchanged()
                remote.put_manifest(manifest_digest, archive.manifest_bytes)
            else:
                with regular(packet.root / ('static/' + name + '.tar.gz')) as (source, _):
                    remote.publish_static(artifact, source)
            published[name] = artifact['reference']
        packet.unchanged()
        for archive in archives.values():
            archive.unchanged()
        return published


def release_proof(packet, environment, *, token_request=identity):
    proof = {'kind': 'komizo.release-proof', 'version': 1, 'release_digest': packet.release_digest,
             'source_revision': packet.descriptor['source']['revision'],
             'run_id': environment['GITHUB_RUN_ID'], 'run_attempt': environment['GITHUB_RUN_ATTEMPT'],
             'workflow_ref': environment['GITHUB_WORKFLOW_REF'], 'workflow_sha': environment['GITHUB_WORKFLOW_SHA']}
    proof['proof'] = token_request(environment, 'komizo-release:sha256:' + hashlib.sha256(oci_registry.canonical(proof)).hexdigest())
    return proof


def write_evidence(packet, proof, published, environment, destination=Path('.artifacts/komizo-release'), *, receipts):
    packet.unchanged()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = {'kind': 'komizo.publication', 'version': 1, 'release_digest': packet.release_digest,
                'source': packet.descriptor['source'], 'published': published,
                'inventory_sha256': hashlib.sha256(packet.inventory_raw).hexdigest(),
                'validation_sha256': hashlib.sha256(oci_registry.canonical(proof)).hexdigest(),
                'execution_context': {key: environment[key] for key in ['GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_REPOSITORY_ID',
                    'GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA', 'GITHUB_EVENT_NAME', 'GITHUB_JOB']}}
    for name, value in [('release.json', packet.descriptor), ('inventory.json', packet.inventory),
                        ('validation.json', proof), ('build.json', receipts[0]), ('test.json', receipts[1]),
                        ('publication.json', evidence)]:
        descriptor = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(packet.release_bytes if name == 'release.json' else packet.inventory_raw if name == 'inventory.json'
                         else oci_registry.canonical(value) + b'\n')
    return evidence
