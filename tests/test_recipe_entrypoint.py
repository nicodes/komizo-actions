"""Prepared task SDKs bind both sources without executing product programs."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RecipeEntrypoint(unittest.TestCase):
    def setUp(self):
        self.sdk = Path(os.environ['RECIPE_TASK_SDK']).resolve()
        self.tools = Path(os.environ['RECIPE_TASK_TOOLS']).resolve()
        self.reference = os.environ['RECIPE_TASK_SDK_REF']
        self.assertRegex(self.reference, r'^[a-f0-9]{40}$')
        actual = subprocess.check_output(['git', '-C', str(self.sdk), 'rev-parse', 'HEAD'], text=True).strip()
        self.assertEqual(actual, self.reference)
        temporary = tempfile.TemporaryDirectory(prefix='native entrypoint source ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/native.py').write_text('raise AssertionError("product code executed during admission")\n')
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[[tests.unit]]\nrunner="python"\npath="tests/native.py"\ntimeout=7\n')
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\n')

    def control(self, code):
        prefix = '''import hashlib,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);import recipe_data,recipe_context
root,tools=map(Path,sys.argv[2:4]);revision=sys.argv[4]
context=recipe_context.Context(tools,revision,Path(sys.argv[5]));context.activate()
sys.path.insert(0,str(tools/'cookbook'));import generate
generated=generate.entrypoint_files(root,revision)
for name,body in generated.items():
 path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body)
before={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
'''
        run = subprocess.run([sys.executable, '-I', '-B', '-c', prefix + code,
            str(ROOT / 'scripts'), str(self.root), str(self.sdk), self.reference, str(self.tools)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_actual_generated_source_identity_and_execution_abi_are_bound(self):
        result = self.control('''assert recipe_data.entrypoint_revision(root,tools)==revision
assert recipe_data.canonical_entrypoint(root,tools,revision)==list(generated)
assert recipe_data.task_command(tools,'build')==['mise','run','build']
after={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
assert before==after
print(json.dumps({'entrypoints':list(generated),'product_executed':False,'source_unchanged':True}))
''')
        self.assertEqual(result['entrypoints'], ['mise.toml'])
        self.assertTrue(result['source_unchanged'])

    def test_changed_generated_bytes_and_cookbook_data_refuse(self):
        result = self.control(r'''name=next(iter(generated));path=root/name;original=path.read_bytes()
refused=0
for change in ['generated','data']:
 if change=='generated':path.write_bytes(original+b'# unreviewed task byte\n')
 else:
  path.write_bytes(original)
  config=root/'cookbook.toml';config.write_bytes(config.read_bytes().replace(b'timeout=7',b'timeout=8'))
 try:recipe_data.canonical_entrypoint(root,tools,revision)
 except ValueError:refused+=1
 else:raise AssertionError('unreviewed source was admitted')
print(json.dumps({'refused':refused}))
''')
        self.assertEqual(result['refused'], 2)

    def test_broken_current_api_cannot_fall_back_to_historical_make(self):
        result = self.control('''import mise_entrypoint
refused=0
for module,name,action in [(mise_entrypoint,'read',lambda:recipe_data.entrypoint_revision(root,tools)),
 (generate,'check_entrypoint',lambda:recipe_data.canonical_entrypoint(root,tools,revision)),
 (generate,'task_command',lambda:recipe_data.task_command(tools,'build'))]:
 original=getattr(module,name);setattr(module,name,None)
 try:action()
 except ValueError:refused+=1
 else:raise AssertionError('broken current API acquired a historical fallback')
 finally:setattr(module,name,original)
print(json.dumps({'refused':refused}))
''')
        self.assertEqual(result['refused'], 3)

    def test_foreign_cached_reader_refuses_before_product_execution(self):
        result = self.control('''import mise_entrypoint
original=mise_entrypoint.__file__;mise_entrypoint.__file__=str(root/'cookbook/mise_entrypoint.py')
try:recipe_data.entrypoint_revision(root,tools)
except ValueError as error:assert 'another cookbook SDK' in str(error)
else:raise AssertionError('foreign SDK supplied task authority')
finally:mise_entrypoint.__file__=original
print(json.dumps({'refused':True}))
''')
        self.assertEqual(result, {'refused': True})

    def test_pre_SDK_discovery_mirror_is_exact_accepted_public_source(self):
        shared = (self.sdk / 'cookbook/sdk_identity.py').read_bytes()
        mirror = (ROOT / 'scripts/cookbook_identity.py').read_bytes()
        self.assertEqual(hashlib.sha256(mirror).hexdigest(), hashlib.sha256(shared).hexdigest())
        self.assertEqual(mirror, shared)


if __name__ == '__main__':
    unittest.main()
