"""Exercise current data-only SDK APIs without sharing historical module caches."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
_run = subprocess.run


def call(module, method, *arguments, **options):
    program = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import recipe_context,recipe_data
cookbook,tools=map(Path,sys.argv[2:4])
context=recipe_context.Context(cookbook,sys.argv[4],tools)
context.activate()
module,method,args,options=json.loads(sys.argv[5])
if module=='recipe_data':
 result=recipe_data.load(Path(args[0]),cookbook)
else:
 target=context.module(module)
 if method in ('declaration','workflow_files','relative'):args[0]=Path(args[0])
 result=getattr(target,method)(*args,**options)
print(json.dumps(result,default=str))
'''
    request = json.dumps([module, method, arguments, options], default=str)
    result = _run([sys.executable, '-I', '-B', '-c', program, str(ROOT / 'scripts'),
        os.environ['RECIPE_TASK_SDK'], os.environ['RECIPE_TASK_TOOLS'],
        os.environ['RECIPE_TASK_SDK_REF'], request], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr)
    return json.loads(result.stdout)


def load(root):
    return call('recipe_data', 'load', root)


class Module:
    def __init__(self, name):
        self.name = name

    def __getattr__(self, method):
        if method not in {'declaration', 'workflow_files', 'relative', 'step_timeouts'}:
            raise AttributeError(method)
        def invoke(*arguments, **options):
            result = call(self.name, method, *arguments, **options)
            return Path(result) if method == 'relative' else result
        return invoke


def write(path, document):
    """Write literal fixture data as TOML, using inline tables for nested values."""
    def value(item):
        if isinstance(item, dict):
            return '{' + ','.join(json.dumps(key) + '=' + value(entry) for key, entry in item.items()) + '}'
        if isinstance(item, list):
            return '[' + ','.join(map(value, item)) + ']'
        if type(item) in (str, int, bool):
            return json.dumps(item)
        raise TypeError(type(item))
    path.write_text(''.join(json.dumps(key) + '=' + value(item) + '\n' for key, item in document.items()))
