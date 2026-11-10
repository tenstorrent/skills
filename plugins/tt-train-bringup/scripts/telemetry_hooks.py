"""Optional, explicitly selected multigoal telemetry extension (API version 1)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

API_VERSION = 1


class GuardedTelemetry:
    """Report ordinary hook exceptions and SystemExit without aborting the runner.

    Hooks run in process; this wrapper is neither a sandbox nor a timeout.
    """

    def __init__(self, extension):
        self.extension = extension

    def safe_hook(self, method, *args, **kwargs):
        try:
            dispatch = getattr(self.extension, "safe", None)
            result = (dispatch(method, *args, **kwargs) if callable(dispatch)
                      else getattr(self.extension, method)(*args, **kwargs))
            if method == "instructions" and result is not None and not isinstance(result, str):
                raise TypeError("instructions must return text or None")
            return result
        except (Exception, SystemExit) as exc:
            print(f"telemetry: {method}: {exc}; bringup continues", file=sys.stderr)
            return None

    def close(self):
        self.safe_hook("close")


def load_telemetry(root: Path | None, **context):
    """Execute the explicitly selected plugin; the caller must trust its code.

    telemetry.json declares {"api_version": 1, "entrypoint": "relative/file.py"}.
    The entrypoint exports create(**context). Its parent is treated as a Python
    package to support relative imports. Only the resolved entrypoint is checked
    for containment; imports and callbacks execute unrestricted in this process.
    This loader does not authenticate the plugin or verify that it is enabled.
    """
    if root is None:
        return None
    try:
        root = Path(root).expanduser().resolve()
        config = json.loads((root / "telemetry.json").read_text())
        if type(config.get("api_version")) is not int or config["api_version"] != API_VERSION:
            raise ValueError("unsupported telemetry hook API version")
        entrypoint = (root / config["entrypoint"]).resolve()
        if not entrypoint.is_relative_to(root) or not entrypoint.is_file():
            raise ValueError("telemetry entrypoint must be a file inside the plugin")
        # Namespace by installation path; this hash does not verify code integrity.
        name = "_multigoal_telemetry_" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
        spec = importlib.util.spec_from_file_location(name, entrypoint,
                                                     submodule_search_locations=[str(entrypoint.parent)])
        if spec is None or spec.loader is None:
            raise ValueError("telemetry entrypoint must be a Python module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return GuardedTelemetry(module.create(**context))
    except (Exception, SystemExit) as exc:
        print(f"telemetry: extension unavailable: {exc}; bringup continues", file=sys.stderr)
        return None
