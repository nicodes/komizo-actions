import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('secret_delivery',Path(__file__).resolve().parents[1]/'set-secrets/push.py')
delivery=importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


class SecretDelivery(unittest.TestCase):
    def test_complete_source_bound_set_crosses_stdin_once_and_never_arguments(self):
        calls=[]
        env={'SECRET_CMD':'doas /usr/local/bin/set-secret-example','KOMIZO_SECRET_FIRST':'synthetic-one',
             'KOMIZO_SECRET_SECOND':'synthetic-two','RELEASE_MANIFEST':'fixture.json','GITHUB_SHA':'a'*40}
        proof={'manifest':'synthetic','token':'fixture.identity.signature'}
        def run(args,**kwargs):calls.append((args,kwargs));return SimpleNamespace(returncode=0)
        with patch.object(delivery.subprocess,'check_output',return_value='a'*40+'\n'):
            self.assertEqual(delivery.push(env,run,lambda path,source,context:proof),2)
        self.assertEqual(len(calls),1)
        args,kwargs=calls[0]
        self.assertEqual(args[-1],'doas /usr/local/bin/set-release-secrets-example')
        self.assertNotIn('synthetic-one',' '.join(args))
        frame,body=kwargs['input'].split(b'\n',1)
        self.assertEqual(json.loads(base64.b64decode(frame.split(b':',1)[1])),proof)
        self.assertEqual(json.loads(body)['secrets'],{'FIRST':'synthetic-one','SECOND':'synthetic-two'})

    def test_invalid_sets_source_or_host_refusal_never_create_partial_success(self):
        for values in ({'NAMES':'GITHUB_TOKEN','GITHUB_TOKEN':'fixture'},
                       {'KOMIZO_SECRET_EMPTY':''},{'KOMIZO_SECRET_BAD':'line\nbreak'},
                       {'KOMIZO_SECRET_HUGE':'x'*(1<<20)}):
            with self.subTest(values=next(iter(values))),self.assertRaises(ValueError):
                delivery.secret_set(values)
        env={'SECRET_CMD':'doas /usr/local/bin/set-secret-example','KOMIZO_SECRET_VALID':'fixture',
             'RELEASE_MANIFEST':'fixture.json','GITHUB_SHA':'a'*40}
        calls=[]
        def run(args,**kwargs):calls.append(args);return SimpleNamespace(returncode=1)
        with patch.object(delivery.subprocess,'check_output',return_value='b'*40):
            with self.assertRaises(ValueError):delivery.push(env,run,lambda *args:{})
        self.assertFalse(calls)
        with patch.object(delivery.subprocess,'check_output',return_value='a'*40):
            with self.assertRaises(ValueError):delivery.push(env,run,lambda *args:{})
        self.assertEqual(len(calls),1)
