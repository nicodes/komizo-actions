"""Use the explicitly prepared real SDK's codec for authored test sources."""
import importlib.util
import os
from pathlib import Path

path = Path(os.environ['RECIPE_SOURCE_SDK']).resolve() / 'helpers/source_config.py'
spec = importlib.util.spec_from_file_location('_prepared_source_fixture', path)
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)
write_source = source.write
read_source = source.read
dump_source = source.dumps
