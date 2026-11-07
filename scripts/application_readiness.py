#!/usr/bin/env python3
"""Prepare and run the actual post-deployment policy under its caller deadline."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import recipe_data
import recipe_context

sys.dont_write_bytecode = True


def readiness(root, tools, environment, driver=None, *, context=None):
    if context is not None:
        for name in ['delivery_workflows','operational_workflows','configuration','node_component','images']:context.module(name)
    sys.path.insert(0, str(tools.resolve() / 'cookbook'))
    import delivery_workflows
    import operational_workflows
    config = recipe_data.load(root, tools)
    if config.get('delivery') != {'definition': environment['DECLARATION']}:
        raise ValueError('readiness differs from the accepted native delivery declaration')
    data = delivery_workflows.declaration(root, config)
    if data.get('readiness_operation'):
        operations = operational_workflows.declaration(root, config)
        record = operations.get('production')
        if not record:
            raise ValueError('readiness requires real native production assertions')
        if driver is None:
            path = Path(__file__).with_name('operational.py')
            spec = importlib.util.spec_from_file_location('delivery_operational', path)
            driver = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(driver)
        if context is not None:
            environment=driver.runtime_environment(root,context.tools,environment,context)
            driver.prepare(root,tools,config,record,'production',environment,helpers=context.tools,context=context)
        else:
            environment = driver.runtime_environment(root, tools, environment)
            driver.prepare(root, tools, config, record, 'production', environment)
        for check in record['checks']:
            driver.native(root, check, environment)
    elif data.get('readiness'):
        path = delivery_workflows.relative(root, data['readiness'], exists=True)
        if not data['readiness'].startswith('deploy/health/') or path.suffix != '.py':
            raise ValueError('native readiness assertions belong in deploy/health')
        subprocess.run([sys.executable, str(path)], env=environment, cwd=root, check=True)
    else:
        raise ValueError('delivery has no native readiness assertions')


def main():
    try:
        environment=dict(os.environ)
        context=None
        if environment.get('COOKBOOK_REF'):
            context=recipe_context.accepted_context(Path(environment['COOKBOOK_ROOT']),environment['COOKBOOK_REF'],
                lambda path:json.loads(subprocess.check_output(['gh','api',path],text=True,timeout=30)),
                Path(environment['TOOLS_ROOT']))
        readiness(Path.cwd(),context.cookbook if context else Path('_delivery_cookbook'),environment,context=context)
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error


if __name__=='__main__':main()
