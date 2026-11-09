"""Native recipe readers keep strict TOML and refuse obsolete SDK protocols."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RecipeSource(unittest.TestCase):
    def setUp(self):
        self.current = Path(os.environ['RECIPE_TASK_SDK']).resolve()
        self.tools = Path(os.environ['RECIPE_TASK_TOOLS']).resolve()
        self.legacy = Path(os.environ['RECIPE_TEST_SDK']).resolve()
        self.assertTrue((self.current / 'cookbook/configuration.py').is_file(), 'prepare the actual native SDK before tests')
        reference = os.environ['RECIPE_TASK_SDK_REF']
        self.reference=reference
        if reference:
            self.assertRegex(reference, r'^[a-f0-9]{40}$')
            self.assertEqual(subprocess.check_output(['git', '-C', str(self.current), 'rev-parse', 'HEAD'], text=True).strip(), reference)
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.legacy), 'rev-parse', 'HEAD'], text=True).strip(),
                         '14dd4792df910b8f72ae10bf01c7e426b97b667e')

    def control(self, code):
        prefix = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);import recipe_data,recipe_context
current,legacy,tools=map(Path,sys.argv[2:5])
recipe_context.Context(current,sys.argv[5],tools).activate()
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', prefix + code,
            str(ROOT / 'scripts'), str(self.current), str(self.legacy),str(self.tools),self.reference],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_current_sdk_preserves_actual_mixed_declaration_shapes(self):
        result = self.control('''raw=b'health_urls=["https://example.test",{url="https://example.test/health",contains="ready"}]\\nlayout=[1,1.0,-0.0,{},[]]\\nsources=[]\\n'
value=recipe_data.decode_source_data(current,raw)
assert type(value['layout'][0]) is int and type(value['layout'][1]) is float
assert recipe_data.source_suffix(current)=='.toml'
assert recipe_data.effective_definition(current,{})=='deploy/release.toml'
print(json.dumps(value))
''')
        self.assertEqual(result['health_urls'][1]['contains'], 'ready')
        self.assertEqual(result['sources'], [])

    def test_current_sdk_refuses_json_duplicates_dates_nonfinite_and_overflow(self):
        result = self.control('''refused=0
for raw in [b'{"value":1}',b'value=1\\nvalue=2',b'value=2026-11-07',b'value=nan',b'value=9223372036854775808']:
 try:recipe_data.decode_source_data(current,raw)
 except ValueError:refused+=1
 else:raise AssertionError('invalid current source was admitted')
try:recipe_data.decode_source_data(current,b'value="oversized"',maximum=1)
except ValueError:refused+=1
else:raise AssertionError('source byte bound was bypassed')
print(json.dumps({'refused':refused}))
''')
        self.assertEqual(result, {'refused': 6})

    def test_obsolete_sdk_cannot_acquire_a_json_source_fallback(self):
        result = self.control('''refused=0
for action in [lambda:recipe_data.source_suffix(legacy),lambda:recipe_data.decode_source_data(legacy,b'{"value":1}')]:
 try:action()
 except ValueError as error:
  assert 'native TOML' in str(error);refused+=1
 else:raise AssertionError('obsolete SDK source was admitted')
print(json.dumps({'refused':refused}))
''')
        self.assertEqual(result,{'refused':2})

    def test_broken_current_api_cannot_use_historical_json_fallback(self):
        result = self.control('''configuration=recipe_data.configuration_module(current)
configuration.parse_source_data=None
refused=0
for action in [lambda:recipe_data.source_suffix(current),lambda:recipe_data.decode_source_data(current,b'{"value":1}')]:
 try:action()
 except ValueError:refused+=1
 else:raise AssertionError('broken current API acquired a historical fallback')
print(json.dumps({'refused':refused}))
''')
        self.assertEqual(result, {'refused': 2})

    def test_foreign_cached_configuration_is_refused_before_decoding(self):
        result = self.control('''sys.path.insert(0,str(legacy/'cookbook'))
import configuration
try:recipe_data.decode_source_data(current,b'value=1')
except ValueError as error:assert 'another cookbook SDK' in str(error)
else:raise AssertionError('foreign SDK decoder was admitted')
print(json.dumps({'refused':True}))
''')
        self.assertEqual(result, {'refused': True})

    def test_source_decode_does_not_execute_product_modules_or_mutate_input(self):
        result = self.control('''import tempfile
with tempfile.TemporaryDirectory() as directory:
 root=Path(directory);(root/'tests').mkdir();(root/'tests/native.py').write_text('raise RuntimeError("source executed")')
 raw=b'version=1\\nrecipe="react-native"\\n[[tests.unit]]\\nrunner="python"\\npath="tests/native.py"\\ntimeout=7\\n'
 (root/'cookbook.toml').write_bytes(raw)
 config=recipe_data.load(root,current)
 assert (root/'cookbook.toml').read_bytes()==raw
 assert config['tests']['unit'][0]['timeout']==7
 assert config['ci']['runner']=='ubuntu-24.04'
 print(json.dumps({'unchanged':True,'effective_definition':recipe_data.effective_definition(current,config)}))
''')
        self.assertEqual(result, {'unchanged': True, 'effective_definition': 'deploy/release.toml'})


if __name__ == '__main__':
    unittest.main()
