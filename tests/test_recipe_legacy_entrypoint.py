"""Historical SDK text admission retains native newline semantics and raw bytes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
REFERENCE='14dd4792df910b8f72ae10bf01c7e426b97b667e'


class LegacyEntrypoint(unittest.TestCase):
    def test_actual_historical_SDK_admits_LF_CRLF_CR_without_rewriting_or_execution(self):
        sdk=Path(os.environ['RECIPE_TEST_SDK']).resolve()
        self.assertEqual(subprocess.check_output(['git','-C',str(sdk),'rev-parse','HEAD'],text=True).strip(),REFERENCE)
        with tempfile.TemporaryDirectory(prefix='historical source newlines ') as temporary:
            root=Path(temporary)
            (root/'tests').mkdir()
            (root/'tests/native.py').write_text('raise AssertionError("product program must never execute")\n')
            (root/'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[[tests.unit]]\nrunner="python"\npath="tests/native.py"\ntimeout=7\n')
            (root/'mise.toml').write_text('[tools]\npython="3.13.11"\n')
            program='''from pathlib import Path
import sys
sys.path.insert(0,sys.argv[1]);import recipe_data
root,sdk=map(Path,sys.argv[2:4]);reference=sys.argv[4]
sys.path.insert(0,str(sdk/'cookbook'));import generate
body=generate.makefile(root,reference)
for newline in ['\\n','\\r\\n','\\r']:
 raw=body.replace('\\n',newline).encode();path=root/'Makefile';path.write_bytes(raw)
 before={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
 assert recipe_data.entrypoint_revision(root,sdk)==reference
 assert recipe_data.canonical_entrypoint(root,sdk,reference)==['Makefile']
 assert before=={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
 path.write_bytes(raw+b'# changed canonical source')
 try:recipe_data.canonical_entrypoint(root,sdk,reference)
 except ValueError:pass
 else:raise AssertionError('changed historical task bytes admitted')
print('All3 native newline forms bind the genuine historical SDK and preserve raw source')
'''
            result=subprocess.run([sys.executable,'-I','-B','-c',program,str(ROOT/'scripts'),
                str(root),str(sdk),REFERENCE],text=True,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('All3 native newline forms',result.stdout)


if __name__=='__main__':
    unittest.main()
