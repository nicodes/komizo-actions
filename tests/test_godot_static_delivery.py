"""Registry retries and extracted static configuration fail closed without deployment."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

spec=importlib.util.spec_from_file_location('static_publication',Path(__file__).parents[1]/'godot-static-delivery/publish.py')
publish=importlib.util.module_from_spec(spec);spec.loader.exec_module(publish)
REF='ghcr.io/example/game-gate:'+'a'*40
IMAGE='sha256:'+'b'*64
DIGEST='ghcr.io/example/game-gate@sha256:'+'c'*64


class StaticDelivery(unittest.TestCase):
    def test_existing_gate_is_reused_only_for_the_exact_accepted_image_id(self):
        calls=[]
        def run(argv,*_args):calls.append(argv)
        with patch.object(publish,'existing',return_value=True),patch.object(publish,'digest_reference',return_value=DIGEST):
            self.assertEqual(publish.publish_gate(REF,IMAGE,Path('.'),{},run,lambda *_args:IMAGE),DIGEST)
            self.assertEqual(calls,[['docker','pull',REF]])
            with self.assertRaisesRegex(ValueError,'different game bytes'):publish.publish_gate(REF,IMAGE,Path('.'),{},run,lambda *_args:'sha256:'+'d'*64)
        self.assertFalse(any('push' in call for call in calls))

    def test_registry_transport_failure_is_not_treated_as_permission_to_replace_a_tag(self):
        for stderr in ['unauthorized','TLS handshake timeout','denied: requested access']:
            with self.subTest(stderr=stderr),patch.object(publish.subprocess,'run',return_value=SimpleNamespace(returncode=1,stderr=stderr)),self.assertRaises(ValueError):
                publish.existing(REF,Path('.'),{})
        with patch.object(publish.subprocess,'run',return_value=SimpleNamespace(returncode=1,stderr='manifest unknown')):
            self.assertFalse(publish.existing(REF,Path('.'),{}))

    def test_new_gate_is_pushed_once_and_bound_to_its_actual_immutable_reference(self):
        calls=[]
        with patch.object(publish,'existing',return_value=False),patch.object(publish,'digest_reference',return_value=DIGEST):
            self.assertEqual(publish.publish_gate(REF,IMAGE,Path('.'),{},lambda argv,*_:calls.append(argv)),DIGEST)
        self.assertEqual(calls,[['docker','push',REF]])

    def test_config_extraction_rejects_extra_linked_or_changed_files_and_always_removes_only_its_container(self):
        import hashlib
        expected={'compose.yml':hashlib.sha256(b'owned compose').hexdigest(),'hostnames':hashlib.sha256(b'owned hosts').hexdigest()}
        for change in ['none','extra','link','changed']:
            calls=[]
            def run(argv,*_args):
                calls.append(argv)
                if argv[:2]==['docker','cp']:
                    destination=Path(argv[-1]);(destination/'compose.yml').write_bytes(b'changed' if change=='changed' else b'owned compose');(destination/'hostnames').write_bytes(b'owned hosts')
                    if change=='extra':(destination/'private-key').write_bytes(b'foreign')
                    if change=='link':(destination/'hostnames').unlink();(destination/'hostnames').symlink_to(destination/'compose.yml')
            with self.subTest(change=change):
                if change=='none':publish.check_config(REF,expected,Path('.'),{},run,lambda *_:'a'*64)
                else:
                    with self.assertRaises(ValueError):publish.check_config(REF,expected,Path('.'),{},run,lambda *_:'a'*64)
                self.assertEqual(calls[-1],['docker','rm','a'*64])


if __name__=='__main__':unittest.main()
