"""Collect the migrated host-only regressions in the existing cheap CI job."""
import importlib.util
from pathlib import Path
import sys

PLUGIN = Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup'
sys.path.insert(0, str(PLUGIN / 'runtime'))
for name, path in [
    ('resume', PLUGIN / 'tests/test_multigoal.py'),
    ('degenerate', PLUGIN / 'runtime/readiness_check/test_check_degenerate_output.py'),
]:
    spec = importlib.util.spec_from_file_location('bringup_imported_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    globals().update({key: value for key, value in vars(module).items()
                     if key.startswith('test_') or key.endswith('Tests')})
