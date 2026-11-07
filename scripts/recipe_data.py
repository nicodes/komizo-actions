"""Read effective recipe data through the caller's already prepared pinned SDK."""
from contextlib import chdir
import importlib
import json
from pathlib import Path, PurePosixPath
import re
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
    return checked_module(configuration if configuration is not None else importlib.import_module('configuration'), tools, 'configuration')


def regular_source(path, maximum=262144):
    path = Path(path)
    if (path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
            or not path.is_file() or path.stat().st_size > maximum):
        raise ValueError('recipe entrypoint must be bounded regular source')
    return path.read_bytes()


def native_text(raw):
    """Preserve the historical text reader's native newline interpretation."""
    return raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')


def entrypoint_revision(root, tools):
    """The selected immutable SDK owns its entrypoint identity protocol."""
    root, tools = Path(root), Path(tools).resolve()
    if (tools / 'cookbook/mise_entrypoint.py').is_file():
        sys.path.insert(0, str(tools / 'cookbook'))
        reader = checked_module(importlib.import_module('mise_entrypoint'), tools, 'mise_entrypoint')
        if not callable(getattr(reader, 'read', None)):
            raise ValueError('recipe SDK exposes no usable entrypoint reader')
        return reader.read(root)['revision']
    # Genuine historical SDKs retain their frozen Make identity protocol.
    source = native_text(regular_source(root / 'Makefile', 65536))
    pins = re.findall(r'^COOKBOOK_REF := ([a-f0-9]{40})$', source, re.M)
    if len(pins) != 1:
        raise ValueError('historical entrypoint requires one immutable SDK pin')
    return pins[0]


def canonical_entrypoint(root, tools, revision, generator=None):
    """Check source bytes without running contributor task programs."""
    root, tools = Path(root), Path(tools).resolve()
    if generator is None:
        sys.path.insert(0, str(tools / 'cookbook'))
        generator = checked_module(importlib.import_module('generate'), tools, 'generate')
    if hasattr(generator, 'entrypoint_files'):
        if not callable(generator.entrypoint_files) or not callable(getattr(generator, 'check_entrypoint', None)):
            raise ValueError('recipe SDK exposes no usable canonical entrypoint contract')
        generator.check_entrypoint(root, revision)
        return list(generator.entrypoint_files(root, revision))
    if native_text(regular_source(root / 'Makefile')) != generator.makefile(root, revision):
        raise ValueError('historical caller differs from its canonical source entrypoint')
    return ['Makefile']


def task_command(tools, operation):
    """Follow the already selected SDK's task execution ABI."""
    tools = Path(tools).resolve()
    sys.path.insert(0, str(tools / 'cookbook'))
    generator = checked_module(importlib.import_module('generate'), tools, 'generate')
    if hasattr(generator, 'task_command'):
        if not callable(generator.task_command):
            raise ValueError('recipe SDK exposes no usable task execution ABI')
        return generator.task_command(operation)
    return ['make', operation]


def source_suffix(tools, configuration=None):
    configuration = configuration_module(tools, configuration)
    if hasattr(configuration, 'parse_source_data'):
        if not callable(configuration.parse_source_data):
            raise ValueError('recipe SDK exposes no usable source decoder')
        return '.toml'
    return '.json'


def decode_source_data(tools, raw, *, maximum=8 << 20, configuration=None):
    """The selected immutable SDK owns its source data format, never a flag."""
    configuration = configuration_module(tools, configuration)
    if hasattr(configuration, 'parse_source_data'):
        if not callable(configuration.parse_source_data):
            raise ValueError('recipe SDK exposes no usable source decoder')
        return configuration.parse_source_data(raw, maximum=maximum)
    # Genuine historical SDKs keep their frozen JSON source ABI.
    if len(raw.encode() if isinstance(raw, str) else raw) > maximum:
        raise ValueError('historical source declaration exceeds its byte limit')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('historical source declaration repeats a field')
            result[key] = value
        return result
    data = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(data, dict):
        raise ValueError('historical source declaration must be an object')
    return data


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
