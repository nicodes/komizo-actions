"""Read effective recipe data through the caller's already prepared pinned SDK."""
from contextlib import chdir
import importlib
from pathlib import Path
import sys


def checked_module(module, tools, name):
    expected = tools / 'cookbook' / (name + '.py')
    origin = getattr(module, '__file__', None)
    if not isinstance(origin, str) or Path(origin).resolve() != expected.resolve():
        raise ValueError('recipe reader loaded another cookbook SDK: ' + name)
    return module


def load(root, tools, configuration=None):
    """Resolve SDK-owned defaults, literal native cases and static output data.

    Callers retain their exact checkout/pin and execution admission. This reader
    only reads bounded source declarations; it never prepares SDKs or runs tests.
    """
    root, tools = Path(root).resolve(), Path(tools).resolve()
    sys.path.insert(0, str(tools / 'cookbook'))
    configuration = checked_module(configuration or importlib.import_module('configuration'), tools, 'configuration')
    with chdir(root):
        config = configuration.load(root)
        config['ci'] = {**config.get('ci', {}), **configuration.ci_settings(config)}
        # Older SDKs without the static publication capability keep their
        # original explicit metadata. New SDKs own these mandatory output names.
        capability = tools / 'cookbook/godot_static.py'
        if config['recipe'] == 'godot-game' and config.get('delivery') and capability.is_file():
            static = checked_module(importlib.import_module('godot_static'), tools, 'godot_static')
            if static.declaration(root, config) is not None:
                metadata = config['artifacts'].get('release_metadata', [])
                config['artifacts']['release_metadata'] = metadata + [name for name in
                    [static.CONFIG_ARTIFACT, static.MANIFEST_ARTIFACT] if name not in metadata]
    return config
