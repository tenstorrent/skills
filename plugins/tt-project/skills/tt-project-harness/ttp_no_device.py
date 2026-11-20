"""Optional pytest plugin: under `TTP_NO_DEVICE=1` (a tt-project run that holds no device), deselect
every test that would open a device, so CPU-only runs never reach the device driver outside the broker.

Use it by copying this file into the repo next to its root `conftest.py` and adding
`pytest_plugins = ["ttp_no_device"]` there, or by loading it with `-p ttp_no_device` on PYTHONPATH.
Outside a tt-project run, or with `TTP_NO_DEVICE` unset or `0`, it does nothing.

A test opens a device when it uses a device fixture (directly or through another fixture) or carries
a device marker. Both lists are configurable, as globs:
- ini `ttp_device_fixtures` / env `TTP_DEVICE_FIXTURES` (comma-separated); default `*device`.
- ini `ttp_device_markers` / env `TTP_DEVICE_MARKERS` (comma-separated); default `*hardware*`,
  `*device*`, `silicon` (so `requires_hardware` or `device` deselect with no ini setting).
The environment wins over the ini file. Deselected tests are counted in the summary; a selection
left empty exits 5 (no tests ran), which says the requested tests need a device.
"""
from __future__ import annotations

import fnmatch
import os

DEFAULT_FIXTURES = ["*device"]
DEFAULT_MARKERS = ["*hardware*", "*device*", "silicon"]


def pytest_addoption(parser):
    parser.addini("ttp_device_fixtures", "Fixture name globs that open a device (ttp_no_device).",
                  type="linelist", default=list(DEFAULT_FIXTURES))
    parser.addini("ttp_device_markers", "Marker name globs of tests that open a device (ttp_no_device).",
                  type="linelist", default=list(DEFAULT_MARKERS))


def _active() -> bool:
    return os.environ.get("TTP_NO_DEVICE", "").strip() == "1"


def _globs(config, ini: str, env: str) -> list[str]:
    raw = os.environ.get(env)
    if raw is not None:
        return [g.strip() for g in raw.split(",") if g.strip()]
    return [g for g in config.getini(ini) if g]


def _matches(names, globs) -> bool:
    return any(fnmatch.fnmatchcase(n, g) for n in names for g in globs)


def needs_device(item, fixtures: list[str], markers: list[str]) -> bool:
    """The test uses a device fixture (its closure includes indirect ones) or has a device marker."""
    used = getattr(item, "fixturenames", ()) or ()
    marks = [m.name for m in item.iter_markers()]
    return _matches(used, fixtures) or _matches(marks, markers)


def pytest_collection_modifyitems(config, items):
    if not _active():
        return
    fixtures = _globs(config, "ttp_device_fixtures", "TTP_DEVICE_FIXTURES")
    markers = _globs(config, "ttp_device_markers", "TTP_DEVICE_MARKERS")
    keep, drop = [], []
    for item in items:
        (drop if needs_device(item, fixtures, markers) else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
    config._ttp_no_device = len(drop)


def pytest_report_header(config):
    if _active():
        return "ttp_no_device: TTP_NO_DEVICE=1, tests that open a device are deselected"


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    n = getattr(config, "_ttp_no_device", 0)
    if n:
        terminalreporter.write_line(f"ttp_no_device: deselected {n} device test(s): this run holds no device "
                                    f"(TTP_NO_DEVICE=1); run them through the device broker")
