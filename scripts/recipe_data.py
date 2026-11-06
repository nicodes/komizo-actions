"""Read effective recipe data through the caller's already prepared pinned SDK."""
from contextlib import chdir
import importlib
from pathlib import Path, PurePosixPath
import sys


def checked_module(module, tools, name):
    expected = tools / 'cookbook' / (name + '.py')
    origin = getattr(module, '__file__', None)
    if not isinstance(origin, str) or Path(origin).resolve() != expected.resolve():
        raise ValueError('recipe reader loaded another cookbook SDK: ' + name)
    return module


def effective_definition(tools, config, configuration=None):
    """Read the caller SDK's effective definition without adding source keys."""
    tools = Path(tools).resolve()
    sys.path.insert(0, str(tools / 'cookbook'))
    configuration = checked_module(configuration if configuration is not None
        else importlib.import_module('configuration'), tools, 'configuration')
    if hasattr(configuration, 'release_definition'):
        if not callable(configuration.release_definition):
            raise ValueError('recipe SDK exposes no usable effective release definition')
        name = configuration.release_definition(config)
    else:
        # Immutable historical SDKs retain their explicit source declaration.
        name = config.get('artifacts', {}).get('definition')
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(ord(character) < 32 for character in name)):
        raise ValueError('release definition must be a literal caller-owned source path')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '.git' in path.parts or path.as_posix() != name or name == '.':
        raise ValueError('release definition must remain a canonical caller-owned source path')
    return name


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
