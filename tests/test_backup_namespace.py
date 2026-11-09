"""Local profile discovery separates recipe and helper sources without effects."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BackupNamespace(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='offline restore recipe ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.caller = self.root/'caller'
        self.caller.mkdir()
        self.tools = self.root/'generic tools'
        self.cookbook = self.root/'recipe source'
        for path,name in [(self.tools,'tools'),(self.cookbook,'cookbook')]:
            path.mkdir()
            self.git(path,'init','-q')
            self.git(path,'remote','add','origin','https://github.com/nicodes/'+name+'.git')
        (self.tools/'helpers').mkdir()
        (self.tools/'helpers/probe.py').write_text('VALUE="generic Tools helper"\n')
        self.tools_ref = self.commit(self.tools)
        (self.cookbook/'mise.toml').write_text('[vars]\ntools_ref="'+self.tools_ref+'"\n')
        recipe = self.cookbook/'cookbook'
        recipe.mkdir()
        (recipe/'dependencies.py').write_text('from pathlib import Path\n'
            'def tools_identity(): return {"repository":"nicodes/tools","revision":'+repr(self.tools_ref)+'}\n'
            'def tools_root(supplied=None): return Path(supplied or '+repr(str(self.tools))+')\n')
        (recipe/'generate.py').write_text('from pathlib import Path\n'
            'def entrypoint_files(root, revision): return {"mise.toml":b"fixture"}\n'
            'def check_entrypoint(root, revision):\n'
            ' if (root/"cookbook.toml").read_bytes()!=b"accepted recipe fixture\\n": raise ValueError("changed caller entrypoint")\n')
        (recipe/'configuration.py').write_text('import tomllib\n'
            'def parse_source_data(raw, **options): return tomllib.loads(raw.decode() if isinstance(raw,bytes) else raw)\n'
            'def load(root): return {"recipe":"react-native","ci":{}}\n'
            'def ci_settings(config): return {}\n')
        (recipe/'operational_workflows.py').write_text('def relative(root, name, exists=False): return root/name\n'
            'def declaration(root, config): return {"restore_profiles":[{"name":"legacy","project":"previous","config":"deploy/restore.toml"}]}\n')
        self.ref = self.commit(self.cookbook)
        (self.caller/'mise.toml').write_text('[vars]\ncookbook_contract=1\ncookbook_ref="'+self.ref+'"\n')
        (self.caller/'cookbook.toml').write_bytes(b'accepted recipe fixture\n')
        (self.caller/'deploy').mkdir()
        (self.caller/'deploy/restore.toml').write_text('project="previous"\n')

    def git(self, root, *arguments):
        return subprocess.check_output(['git','-c','core.hooksPath='+os.devnull,
            '-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
            '-c','commit.gpgsign=false','-C',str(root),*arguments],text=True,stderr=subprocess.DEVNULL).strip()

    def commit(self, root):
        self.git(root,'add','.')
        self.git(root,'commit','-qm','Finite source fixture')
        return self.git(root,'rev-parse','HEAD')

    def control(self, code):
        prefix='''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);import backup
root,cookbook,tools=map(Path,sys.argv[2:5]);reference=sys.argv[5]
'''
        environment={k:v for k,v in os.environ.items() if k not in {
            'TOOLS_ROOT','TOOLS_REF','COOKBOOK_ROOT','COOKBOOK_REF'}}
        result=subprocess.run([sys.executable,'-I','-B','-c',prefix+code,str(ROOT/'scripts'),
            str(self.caller),str(self.cookbook),str(self.tools),self.ref],env=environment,
            text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_profile_comes_from_Cookbook_and_generic_helper_from_derived_Tools(self):
        result=self.control('''import os
os.environ['TOOLS_REF']=''
context=backup.local_recipe_context(root,cookbook_root=cookbook,tools_root=tools)
assert context.cookbook==cookbook and context.tools==tools
assert backup.restore_profile(root,cookbook,'legacy','previous',context=context)==root/'deploy/restore.toml'
assert backup.helper(context.tools,'probe').VALUE=='generic Tools helper'
assert not list(cookbook.rglob('__pycache__')) and not list(tools.rglob('__pycache__'))
print(json.dumps(context.identities()))
''')
        self.assertEqual(result['cookbook_commit'],self.ref)
        self.assertEqual(result['tools_commit'],self.tools_ref)

    def test_missing_partial_and_mixed_new_identity_never_load_historical_source(self):
        (self.caller/'Makefile').write_text('COOKBOOK_REF := '+self.ref+'\n')
        for source in ['[vars]\ncookbook_contract=1\n',
                       '[vars]\ncookbook_ref="'+self.ref+'"\n',
                       '[vars]\ncookbook_contract=1\ncookbook_ref="'+self.ref+'"\ntools_ref="'+self.tools_ref+'"\n']:
            (self.caller/'mise.toml').write_text(source)
            self.control('''try:backup.local_recipe_context(root,cookbook_root=cookbook,tools_root=tools)
except ValueError:print(json.dumps({'refused':True}))
else:raise AssertionError('invalid new source selected a historical protocol')
''')

    def test_default_prepared_discovery_is_offline_and_does_not_import_recipe_code(self):
        (self.cookbook/'cookbook/dependencies.py').write_text('raise AssertionError("discovery executed recipe code")\n')
        self.ref=self.commit(self.cookbook)
        (self.caller/'mise.toml').write_text('[vars]\ncookbook_contract=1\ncookbook_ref="'+self.ref+'"\n')
        destination=self.caller/'.artifacts/cookbook'/self.ref
        destination.parent.mkdir(parents=True)
        self.cookbook.rename(destination)
        self.control('''def audit(event, arguments):
 if event=='subprocess.Popen':
  command=arguments[1]
  assert command[0]=='git' and not set(command)&{'fetch','clone','pull','checkout','init'}
sys.addaudithook(audit)
assert backup.prepared_cookbook(root)==root/'.artifacts/cookbook'/reference
print(json.dumps({'offline':True}))
''')

    def test_dirty_or_foreign_Tools_and_changed_caller_refuse_before_restore(self):
        (self.caller/'cookbook.toml').write_text('changed recipe\n')
        self.control('''try:backup.local_recipe_context(root,cookbook_root=cookbook,tools_root=tools)
except ValueError:print(json.dumps({'refused':True}))
else:raise AssertionError('noncanonical caller admitted')
''')
        (self.caller/'cookbook.toml').write_bytes(b'accepted recipe fixture\n')
        self.git(self.tools,'remote','set-url','origin','https://github.com/foreign/tools.git')
        self.control('''try:backup.local_recipe_context(root,cookbook_root=cookbook,tools_root=tools)
except ValueError:print(json.dumps({'refused':True}))
else:raise AssertionError('foreign helper source admitted')
''')
        self.git(self.tools,'remote','set-url','origin','https://github.com/nicodes/tools.git')
        (self.tools/'untracked.py').write_text('raise AssertionError("must not execute")\n')
        self.control('''try:backup.local_recipe_context(root,cookbook_root=cookbook,tools_root=tools)
except ValueError:print(json.dumps({'refused':True}))
else:raise AssertionError('dirty helper source admitted')
''')

    def test_pre_SDK_discovery_mirror_has_frozen_public_bytes(self):
        self.assertEqual(hashlib.sha256((ROOT/'scripts/cookbook_identity.py').read_bytes()).hexdigest(),
                         '6eebba81caebc9dec86da926c192775557beaa7c33c9aa8e52a94204a94a4efe')


if __name__=='__main__':
    unittest.main()
