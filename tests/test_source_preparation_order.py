"""Real first-parent preparation and direct interpreter, without host effects."""
import ast
import hashlib
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest

import yaml
import test_recipe_setup

ROOT=Path(__file__).resolve().parents[1]
BOOTSTRAP='Bootstrap Python for protected SDK sources'
PRIVATE='Prepare the private artifact parent with actual pinned Python'
SCRIPT_SHA256='d60eacb11a7b401fd71189acdc82d96f2e222cf11023becafb6af2a85c1c4887'


def source_blocks():
    paths=[*ROOT.glob('.github/workflows/cookbook-*.yml'),*ROOT.glob('*/action.yml')]
    for path in paths:
        document=yaml.safe_load(path.read_text())
        blocks=[('action',document['runs'])] if 'runs' in document else document.get('jobs',{}).items()
        for job,block in blocks:
            yield path,job,block


class SourcePreparationOrder(unittest.TestCase):
    def test_all_modern_C_checkouts_prepare_first_parent_before_checkout(self):
        found=0;scripts=set()
        for path,job,block in source_blocks():
            steps=block.get('steps',[])
            for index,checkout in enumerate(steps):
                if '.artifacts/recipe-sdks/_cookbook' not in checkout.get('with',{}).get('path',''):
                    continue
                with self.subTest(path=path.name,job=job):
                    found+=1
                    setup,parent=steps[index-2:index]
                    self.assertEqual([setup['name'],parent['name']],[BOOTSTRAP,PRIVATE])
                    self.assertEqual(setup['uses'],'actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405')
                    self.assertEqual(setup['with'],{'python-version':'3.13.11','check-latest':False})
                    self.assertEqual(parent['env']['PRIVATE_PYTHON'],'${{ steps.sdk-private-python.outputs.python-path }}')
                    for step in [setup,parent]:
                        self.assertIn("runner.os == 'Windows'",step['if'])
                        self.assertIn("inputs.cookbook-ref != ''",step['if'])
                        if checkout.get('if'):
                            condition=checkout['if'].removeprefix('${{').removesuffix('}}').strip()
                            self.assertIn(condition,step['if'])
                        for key in ['MISE_AUTO_INSTALL','MISE_EXEC_AUTO_INSTALL','MISE_TASK_RUN_AUTO_INSTALL']:
                            self.assertEqual(step['env'][key],'false')
                        if job=='action':self.assertNotIn('timeout-minutes',step)
                    if job!='action':
                        self.assertEqual(setup['timeout-minutes'],5)
                        self.assertEqual(parent['timeout-minutes'],1)
                    # Decode the inert -c argument and authenticate the one common
                    # stdlib policy. Do not execute or import an SDK source here.
                    command=next(line for line in parent['run'].splitlines() if ' -I -B -c ' in line)
                    arguments=shlex.split(command)
                    program=ast.parse(arguments[arguments.index('-c')+1],mode='eval').body
                    self.assertIsInstance(program,ast.Call)
                    self.assertEqual(program.func.id,'exec')
                    script=ast.literal_eval(program.args[0])
                    self.assertEqual(hashlib.sha256(script.encode()).hexdigest(),SCRIPT_SHA256)
                    scripts.add(parent['run'])
        self.assertGreaterEqual(found,15)
        self.assertEqual(len(scripts),1)

    def private_command(self):
        return next(step['run'] for path,job,block in source_blocks() for step in block.get('steps',[])
                    if step.get('name')==PRIVATE)

    def test_real_private_literal_precedes_checkout_and_never_repairs_existing_or_linked_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);environment=dict(os.environ,PRIVATE_PYTHON=sys.executable,GITHUB_ENV=str(root/'env'))
            def prepare():
                return subprocess.run(['bash','-euc',self.private_command()],cwd=root,env=environment,
                                      capture_output=True,text=True,timeout=10)
            result=prepare();self.assertEqual(result.returncode,0,result.stderr)
            parent=root/'.artifacts'
            if os.name!='nt':self.assertEqual(stat.S_IMODE(parent.stat().st_mode),0o700)
            (parent/'recipe-sdks/_cookbook').mkdir(parents=True)
            self.assertTrue((parent/'recipe-sdks/_cookbook').is_dir())
            values=(root/'env').read_text().splitlines()
            self.assertIn('COOKBOOK_BOOTSTRAP_PYTHON='+sys.executable,values)
            self.assertTrue({'MISE_AUTO_INSTALL=false','MISE_EXEC_AUTO_INSTALL=false','MISE_TASK_RUN_AUTO_INSTALL=false'}<=set(values))
            if os.name!='nt':
                parent.chmod(0o777)
                result=prepare();self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(stat.S_IMODE(parent.stat().st_mode),0o777)
                # The later authenticated native guard must refuse this existing
                # state; the stdlib pre-checkout literal must not bless/repair it.
                self.assertTrue(parent.stat().st_mode & 0o022)
                (parent/'recipe-sdks/_cookbook').rmdir();(parent/'recipe-sdks').rmdir();parent.rmdir()
                target=root/'foreign';target.mkdir();parent.symlink_to(target,target_is_directory=True)
                result=prepare();self.assertNotEqual(result.returncode,0)
                self.assertIn('links or reparse points',result.stderr)
                self.assertEqual(list(target.iterdir()),[])

    def test_direct_verified_bootstrap_python_avoids_a_real_poisoned_ambient_shim(self):
        fixture=test_recipe_setup.RecipeSetup();fixture.setUp();self.addCleanup(fixture.doCleanups)
        binary=fixture.fixture.directory/'poisoned-shim';binary.mkdir()
        marker=binary/'called'
        shim=binary/'python3';shim.write_text('#!/bin/sh\nprintf poisoned > "'+str(marker)+'"\nexit 89\n');shim.chmod(0o755)
        environment=fixture.environment|{'PATH':str(binary)+os.pathsep+fixture.environment['PATH'],
                                         'COOKBOOK_BOOTSTRAP_PYTHON':sys.executable}
        old=subprocess.run(['bash','-euc','python3 -I -B -c "import sys"'],cwd=fixture.fixture.app,
                           env=environment,capture_output=True,text=True,timeout=10)
        self.assertEqual(old.returncode,89);self.assertTrue(marker.exists());marker.unlink()
        fixture.compose(environment)
        self.assertFalse(marker.exists())
        self.assertEqual(fixture.trace.read_text().splitlines()[2:],['dependencies','exact-python-install'])
        for name in ['recipe-setup','godot-production']:
            action=yaml.safe_load((ROOT/name/'action.yml').read_text())
            calls=[step['run'] for step in action['runs']['steps'] if 'run' in step and
                   ('bootstrap-python=' in step['run'] or 'recipe_context.py' in step['run'])]
            self.assertEqual(len(calls),2)
            for call in calls:self.assertTrue(call.startswith('"${COOKBOOK_BOOTSTRAP_PYTHON:-python3}" -I -B '))
            for step in action['runs']['steps']:
                if 'bootstrap-python=' in step.get('run','') or 'recipe_context.py' in step.get('run',''):
                    for key in ['MISE_AUTO_INSTALL','MISE_EXEC_AUTO_INSTALL','MISE_TASK_RUN_AUTO_INSTALL']:
                        self.assertEqual(step['env'][key],'false')


    def test_Linux_fallback_disables_ambient_auto_install_before_python_shim(self):
        action=yaml.safe_load((ROOT/'recipe-setup/action.yml').read_text())
        probe=next(step for step in action['runs']['steps'] if 'bootstrap-python=' in step.get('run',''))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);binary=root/'bin';binary.mkdir()
            marker=root/'shim-called'
            shim=binary/'python3'
            shim.write_text('#!/bin/sh\n'
                'for name in MISE_AUTO_INSTALL MISE_EXEC_AUTO_INSTALL MISE_TASK_RUN_AUTO_INSTALL; do\n'
                ' eval "value=\\$${name}"\n'
                ' [ "$value" = false ] || exit 91\n'
                'done\nprintf guarded > "'+str(marker)+'"\nexec "'+sys.executable+'" "$@"\n')
            shim.chmod(0o755)
            environment=dict(os.environ,PATH=str(binary)+os.pathsep+os.environ['PATH'],GITHUB_OUTPUT=str(root/'output'))
            environment.pop('COOKBOOK_BOOTSTRAP_PYTHON',None)
            flags={key:'true' for key in ['MISE_AUTO_INSTALL','MISE_EXEC_AUTO_INSTALL','MISE_TASK_RUN_AUTO_INSTALL']}
            old=subprocess.run(['bash','-euc',probe['run']],cwd=root,env=environment|flags,
                               capture_output=True,text=True,timeout=10)
            self.assertEqual(old.returncode,91);self.assertFalse(marker.exists())
            current=subprocess.run(['bash','-euc',probe['run']],cwd=root,env=environment|flags|probe['env'],
                                   capture_output=True,text=True,timeout=10)
            self.assertEqual(current.returncode,0,current.stderr);self.assertTrue(marker.exists())
            self.assertIn('bootstrap-python=false',(root/'output').read_text())


if __name__=='__main__':unittest.main()
