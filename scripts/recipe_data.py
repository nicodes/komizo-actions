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


def configuration_module(tools, configuration=None):
    tools = Path(tools).resolve()
    sys.path.insert(0, str(tools / 'cookbook'))
    module = checked_module(configuration if configuration is not None else importlib.import_module('configuration'), tools, 'configuration')
    if not callable(getattr(module, 'parse_source_data', None)):
        raise ValueError('recipe SDK requires its native TOML source decoder')
    return module


def entrypoint_revision(root, tools):
    """The selected immutable SDK owns its entrypoint identity protocol."""
    root, tools = Path(root), Path(tools).resolve()
    if not (tools / 'cookbook/mise_entrypoint.py').is_file():
        raise ValueError('recipe SDK requires its native Mise entrypoint reader')
    sys.path.insert(0, str(tools / 'cookbook'))
    reader = checked_module(importlib.import_module('mise_entrypoint'), tools, 'mise_entrypoint')
    if not callable(getattr(reader, 'read', None)):
        raise ValueError('recipe SDK exposes no usable native Mise entrypoint reader')
    return reader.read(root)['revision']


def canonical_entrypoint(root, tools, revision, generator=None):
    """Check source bytes without running contributor task programs."""
    root, tools = Path(root), Path(tools).resolve()
    if generator is None:
        sys.path.insert(0, str(tools / 'cookbook'))
        generator = importlib.import_module('generate')
    generator = checked_module(generator, tools, 'generate')
    if not callable(getattr(generator, 'entrypoint_files', None)) or not callable(getattr(generator, 'check_entrypoint', None)):
        raise ValueError('recipe SDK requires its native canonical entrypoint contract')
    generator.check_entrypoint(root, revision)
    return list(generator.entrypoint_files(root, revision))


def task_command(tools, operation):
    """Follow the already selected SDK's task execution ABI."""
    tools = Path(tools).resolve()
    sys.path.insert(0, str(tools / 'cookbook'))
    generator = checked_module(importlib.import_module('generate'), tools, 'generate')
    if not callable(getattr(generator, 'task_command', None)):
        raise ValueError('recipe SDK requires its native task execution ABI')
    return generator.task_command(operation)


def source_suffix(tools, configuration=None):
    configuration_module(tools, configuration)
    return '.toml'


def decode_source_data(tools, raw, *, maximum=8 << 20, configuration=None):
    """The selected immutable SDK owns its source data format, never a flag."""
    configuration = configuration_module(tools, configuration)
    return configuration.parse_source_data(raw, maximum=maximum)


def effective_definition(tools, config, configuration=None):
    """Read the caller SDK's effective definition without adding source keys."""
    tools = Path(tools).resolve()
    sys.path.insert(0, str(tools / 'cookbook'))
    configuration = configuration_module(tools, configuration)
    if not callable(getattr(configuration, 'release_definition', None)):
        raise ValueError('recipe SDK requires its native effective release definition')
    name = configuration.release_definition(config)
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
    configuration = configuration_module(tools, configuration)
    if not callable(getattr(configuration, 'load', None)) or not callable(getattr(configuration, 'ci_settings', None)):
        raise ValueError('recipe SDK requires its native configuration and CI declarations')
    with chdir(root):
        config = configuration.load(root)
        config['ci'] = {**config.get('ci', {}), **configuration.ci_settings(config)}
    return config
