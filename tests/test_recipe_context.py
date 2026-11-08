"""Separate real repository identities before loading recipe dependencies."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RecipeContext(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='separate recipe SDK roots ')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tools = self.root / 'tools'
        self.cookbook = self.root / 'cookbook'
        for path, name in [(self.tools, 'tools'), (self.cookbook, 'cookbook')]:
            path.mkdir()
            self.git(path, 'init', '-q')
            self.git(path, 'remote', 'add', 'origin', 'https://github.com/nicodes/' + name + '.git')
            (path / '.gitignore').write_text('ignored-fixture\n')
            (path / 'product.py').write_text('raise AssertionError("product program must never execute")\n')
        self.tools_ref = self.commit(self.tools)
        (self.cookbook / 'mise.toml').write_text('[vars]\ntools_ref="' + self.tools_ref + '"\n')
        (self.cookbook / 'cookbook').mkdir()
        self.dependency = self.cookbook / 'cookbook/dependencies.py'
        self.dependency.write_text('from pathlib import Path\n'
            'def tools_identity():\n return {"repository":"nicodes/tools","revision":' + repr(self.tools_ref) + '}\n'
            'def tools_root(supplied=None):\n return Path(supplied) if supplied is not None else Path(' + repr(str(self.tools)) + ')\n')
        self.reference = self.commit(self.cookbook)

    def git(self, root, *arguments):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=' + os.devnull,
            '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            '-c', 'commit.gpgsign=false', '-C', str(root), *arguments],
            text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, root):
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-qm', 'Exact SDK fixture')
        return self.git(root, 'rev-parse', 'HEAD')

    def control(self, code):
        prefix = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);import recipe_context
cookbook,tools=map(Path,sys.argv[2:4]);revision,tools_revision=sys.argv[4:6]
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', prefix + code,
            str(ROOT / 'scripts'), str(self.cookbook), str(self.tools), self.reference, self.tools_ref],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_distinct_clean_sources_and_both_accepted_repositories_are_bound(self):
        result = self.control('''context=recipe_context.Context(cookbook,revision,tools)
queries=[]
def lookup(path):queries.append(path);return {'status':'identical'}
context.require_accepted(lookup)
assert context.cookbook==cookbook and context.tools==tools
assert context.identities()=={'cookbook_repository':'nicodes/cookbook','cookbook_commit':revision,
 'tools_repository':'nicodes/tools','tools_commit':tools_revision}
assert queries==['repos/nicodes/cookbook/compare/'+revision+'...main',
 'repos/nicodes/tools/compare/'+tools_revision+'...main']
try:context.require_accepted(lambda path:{'status':'behind' if '/tools/' in path else 'identical'})
except ValueError:pass
else:raise AssertionError('unaccepted helper source was admitted')
print(json.dumps(context.identities()))
''')
        self.assertEqual(result['cookbook_commit'], self.reference)
        self.assertEqual(result['tools_commit'], self.tools_ref)

    def test_canonical_ssh_origins_keep_each_actual_repository_identity(self):
        self.git(self.cookbook, 'remote', 'set-url', 'origin', 'git@github.com:nicodes/cookbook.git')
        self.git(self.tools, 'remote', 'set-url', 'origin', 'ssh://git@github.com/nicodes/tools.git')
        self.control('''context=recipe_context.Context(cookbook,revision,tools)
assert context.tools_revision==tools_revision
print(json.dumps(context.identities()))
''')

    def test_acceptance_precedes_downloaded_code_and_proof_cannot_alias_sources(self):
        self.control('''context=recipe_context.accepted_context(cookbook,revision,lambda _: {'status':'identical'},tools)
proof={**context.identities(),'entrypoint_contract':'mise-v1'}
context.require_proof(proof)
for bad in [proof|{'tools_commit':revision},proof|{'cookbook_repository':'nicodes/tools'},
 proof|{'entrypoint_contract':'make-v0'}]:
 try:context.require_proof(bad)
 except ValueError:pass
 else:raise AssertionError('aliased source or historical proof was admitted')
print(json.dumps({'refused':True}))
''')
        self.dependency.write_text('raise AssertionError("unaccepted SDK code executed")\n')
        self.reference = self.commit(self.cookbook)
        self.assertEqual(self.control('''try:recipe_context.accepted_context(cookbook,revision,lambda _: {'status':'behind'},tools)
except ValueError:print(json.dumps({'refused':True}))
else:raise AssertionError('unaccepted SDK was loaded')
'''), {'refused': True})

    def test_foreign_origin_and_ignored_helper_files_refuse(self):
        self.git(self.cookbook, 'remote', 'set-url', 'origin', 'https://github.com/nicodes/tools.git')
        self.assertEqual(self.control('''try:recipe_context.Context(cookbook,revision,tools)
except ValueError:print(json.dumps({'refused':True}))
else:raise AssertionError('Tools was relabeled as Cookbook')
'''), {'refused': True})
        self.git(self.cookbook, 'remote', 'set-url', 'origin', 'https://github.com/nicodes/cookbook.git')
        (self.tools / 'ignored-fixture').write_text('unexpected source\n')
        self.assertEqual(self.control('''try:recipe_context.Context(cookbook,revision,tools)
except ValueError:print(json.dumps({'refused':True}))
else:raise AssertionError('ignored helper source was admitted')
'''), {'refused': True})

    def test_import_and_runtime_environment_use_the_verified_helper_location(self):
        (self.cookbook / 'cookbook/configuration.py').write_text('import os\nhelper_root=os.environ["TOOLS_ROOT"]\n')
        self.reference = self.commit(self.cookbook)
        result = self.control('''import os
context=recipe_context.Context(cookbook,revision,tools)
os.environ['TOOLS_ROOT']=str(cookbook/'wrong-historical-helper')
module=context.module('configuration')
assert module.helper_root==str(tools)
assert os.environ['COOKBOOK_ROOT']==str(cookbook) and os.environ['TOOLS_ROOT']==str(tools)
assert context.environment({})=={'COOKBOOK_ROOT':str(cookbook),'COOKBOOK_REF':revision,
 'TOOLS_ROOT':str(tools),'TOOLS_REF':tools_revision}
print(json.dumps({'verified':True}))
''')
        self.assertEqual(result, {'verified': True})

    def test_dependency_cannot_replace_provider_pin_and_cached_reader_cannot_replace_source(self):
        self.dependency.write_text(self.dependency.read_text().replace(self.tools_ref, 'f' * 40))
        self.reference = self.commit(self.cookbook)
        self.assertEqual(self.control('''try:recipe_context.Context(cookbook,revision,tools)
except ValueError:pass
else:raise AssertionError('dependency implementation replaced the provider pin')
from types import SimpleNamespace
sys.modules['dependencies']=SimpleNamespace(__file__=str(tools/'cookbook/dependencies.py'))
try:recipe_context.Context(cookbook,revision,tools)
except ValueError as error:assert 'another Cookbook source' in str(error)
else:raise AssertionError('foreign cached dependency module was admitted')
print(json.dumps({'refused':True}))
'''), {'refused': True})

    def test_nonimmutable_provider_pin_refuses_before_loading_dependency_code(self):
        (self.cookbook / 'mise.toml').write_text('[vars]\ntools_ref="main"\n')
        self.dependency.write_text('raise AssertionError("invalid dependency was executed")\n')
        self.reference = self.commit(self.cookbook)
        self.assertEqual(self.control('''try:recipe_context.Context(cookbook,revision,tools)
except ValueError:print(json.dumps({'refused':True}))
else:raise AssertionError('mutable provider dependency was admitted')
'''), {'refused': True})

    def test_source_only_admission_uses_fixed_interpreter_and_never_activates_caller_tasks(self):
        binary=self.root/'bin';binary.mkdir();marker=self.root/'must-not-run'
        (binary/'gh').write_text("#!/bin/sh\nprintf '%s\\n' '{\"status\":\"identical\"}'\n")
        (binary/'gh').chmod(0o700)
        (binary/'mise').write_text('#!/bin/sh\ntouch "'+str(marker)+'"\nexit 91\n')
        (binary/'mise').chmod(0o700)
        caller=self.root/'caller';caller.mkdir()
        (caller/'mise.toml').write_text('[hooks]\nenter="must-never-execute"\n')
        output=self.root/'environment';output.write_text('')
        result=subprocess.run([sys.executable,'-I','-B',str(ROOT/'scripts/recipe_context.py'),'sources',
            '--cookbook-root',str(self.cookbook),'--cookbook-ref',self.reference,'--tools-root',str(self.tools)],
            cwd=caller,env=dict(os.environ,PATH=str(binary)+os.pathsep+os.environ['PATH'],GITHUB_ENV=str(output)),
            capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(marker.exists())
        self.assertIn('COOKBOOK_ROOT='+str(self.cookbook),output.read_text())
        self.assertIn('TOOLS_REF='+self.tools_ref,output.read_text())
        self.assertFalse((caller/'.artifacts').exists())


if __name__ == '__main__':
    unittest.main()
