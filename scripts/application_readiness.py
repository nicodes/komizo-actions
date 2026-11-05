#!/usr/bin/env python3
"""Prepare and run the actual post-deployment policy under its caller deadline."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tomllib

sys.dont_write_bytecode = True


def readiness(root, tools, environment, driver=None):
    sys.path.insert(0, str(tools.resolve() / 'cookbook'))
    import delivery_workflows
    import operational_workflows
    config = tomllib.loads((root / 'cookbook.toml').read_text())
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


if __name__ == '__main__':
    try:
        readiness(Path.cwd(), Path('_delivery_cookbook'), dict(os.environ))
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
