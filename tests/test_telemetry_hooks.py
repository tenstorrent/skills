"""The public runner's optional extension boundary needs no telemetry service."""
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "plugins/tt-model-bringup/scripts/telemetry_hooks.py"
SPEC = importlib.util.spec_from_file_location("telemetry_hooks_under_test", PATH)
hooks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hooks)


def plugin(tmp_path, code):
    (tmp_path / "telemetry.json").write_text(json.dumps({"api_version": 1, "entrypoint": "entry.py"}))
    (tmp_path / "entry.py").write_text(code)
    return tmp_path


def test_no_plugin_is_inert():
    assert hooks.load_telemetry(None) is None


def test_extension_loads_only_explicit_root_and_supports_relative_imports(tmp_path):
    (tmp_path / "helper.py").write_text("TEXT = 'stage evidence'\n")
    root = plugin(tmp_path, "from .helper import TEXT\n"
                  "class Extension:\n"
                  "    def instructions(self): return TEXT\n"
                  "def create(**context):\n"
                  "    assert context['hf_model'] == 'org/model'\n"
                  "    return Extension()\n")
    assert hooks.load_telemetry(root, hf_model="org/model").safe("instructions") == "stage evidence"


@pytest.mark.parametrize("code", [
    "raise RuntimeError('broken import')", "def create(**context): raise ValueError('broken setup')",
    "raise SystemExit(2)", "def create(**context): raise SystemExit(2)",
])
def test_import_and_setup_failures_are_advisory(tmp_path, code, capsys):
    assert hooks.load_telemetry(plugin(tmp_path, code)) is None
    assert "bringup continues" in capsys.readouterr().err


@pytest.mark.parametrize("error", [RuntimeError, SystemExit])
def test_failed_callbacks_and_wrong_instruction_type_are_advisory(capsys, error):
    class Extension:
        def safe(self, method, *args):
            if method == "instructions":
                return {"not": "text"}
            raise error("callback failed")
    extension = hooks.GuardedTelemetry(Extension())
    assert extension.safe("instructions") is None
    assert extension.safe("end_stage") is None
    extension.close()
    assert capsys.readouterr().err.count("bringup continues") == 3


def test_entrypoint_cannot_escape_selected_plugin(tmp_path, capsys):
    root = tmp_path / "plugin"
    root.mkdir()
    (tmp_path / "outside.py").write_text("raise AssertionError('must not execute')")
    (root / "telemetry.json").write_text(json.dumps({"api_version": 1, "entrypoint": "../outside.py"}))
    assert hooks.load_telemetry(root) is None
    assert "inside the plugin" in capsys.readouterr().err
