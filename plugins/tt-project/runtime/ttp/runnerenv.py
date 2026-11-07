# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The pinned Python environment every check runs in (push.check_env), so a package installed or
upgraded in the user site (~/.local, `pip install --user`) never changes what a check imports.

A check runs with PYTHONNOUSERSITE=1 (the user site never loads), PIP_USER unset, and PATH fronted
by a harness-owned virtualenv's bin, so `python3`, `python3 -m pytest` and `pytest` resolve there.
The virtualenv sees the system's site-packages (packages the OS installs, such as PyYAML) below its
own, so its pinned pytest wins over any other.

Its packages come from a lock: `checks.python_lock` (a requirements file with exact pins, hashes
where possible; relative to the checkout) when the project sets it, else LOCK beside this file
(pytest and pytest-xdist). It is built once under TTP_HOME/checks-env, keyed by the lock's content
and the base interpreter, and reused. A build goes to a fresh folder under a file lock, then the
key's link moves to it in one rename, so a lock change builds beside the old env and a running
check never sees one rebuilt under it. A build that fails fails the checks with what to fix: never
a fallback to the user site."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

LOCK = Path(__file__).with_name("checks-python.lock")
INFO = "ttp-env.json"   # in an env: its key, lock hash and versions (describe)
BUILD_TIMEOUT_S = 900
_ready: dict[str, dict] = {}   # key -> info, for this process


class Unavailable(RuntimeError):
    """The pinned environment could not be built or used; the message says what to fix."""


def lock_of(cfg: dict | None, top: str | Path | None = None) -> Path:
    """The lock the checks of a project with config `cfg` use, a relative `checks.python_lock` read in
    the checkout `top`; LOCK when it sets none."""
    v = ((cfg or {}).get("checks") or {}).get("python_lock")
    if not isinstance(v, str) or not (v := v.strip()):
        return LOCK
    q = Path(v).expanduser()
    return q if q.is_absolute() else Path(top or ".") / q


def root() -> Path:
    return Path(os.environ.get("TTP_HOME") or Path.home() / ".tt-project") / "checks-env"


def _base_python() -> str:
    """The interpreter envs are made from: this one's base (a venv's own python makes no venv of the system)."""
    base = Path(getattr(sys, "_base_executable", "") or sys.executable)
    return str(base if base.exists() else Path(sys.executable))


def key_of(text: bytes, base: str) -> str:
    """The env's key: the lock's content, and the base interpreter's path, version and machine."""
    ident = f"{os.path.realpath(base)}\n{sys.version}\n{platform.machine()}\n".encode()
    return hashlib.sha256(ident + text).hexdigest()[:16]


def _clean(env: dict) -> dict:
    out = {k: v for k, v in env.items() if k not in ("PIP_USER", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")}
    return {**out, "PYTHONNOUSERSITE": "1"}


def _read(link: Path) -> dict | None:
    """The info of the env `link` points at when it is whole and its python runs, else None."""
    try:
        info = json.loads((link / INFO).read_text())
        py = link / "bin" / "python"
        r = subprocess.run([str(py), "-c", "pass"], capture_output=True, timeout=60, env=_clean(dict(os.environ)))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return {**info, "path": str(link.resolve())} if r.returncode == 0 and isinstance(info, dict) else None


def ensure(lock: Path | None = None) -> dict:
    """The info of the env built from `lock` (default LOCK), building it first when there is none:
    {key, lock, lock_sha256, python, pytest, path}. Unavailable when it cannot be built."""
    if test := os.environ.get("TTP_TEST_CHECKS_PYTHON_ENV"):   # tests: a ready env of the suite's own
        return _read(Path(test)) or _fail(Path(test), "the test env is not whole")
    lock = Path(lock or LOCK)
    try:
        text = lock.read_bytes()
    except OSError as e:
        raise Unavailable(f"the checks' Python lock {lock} cannot be read ({e.strerror or e}); fix "
                          "checks.python_lock (`ttp config checks.python_lock <file>`) or restore the file")
    base = _base_python()
    key = key_of(text, base)
    if key in _ready:
        return _ready[key]
    home = root()
    link = home / key
    if not (info := _read(link)):
        home.mkdir(parents=True, exist_ok=True)
        with open(home / f".{key}.lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)   # one build per key; the others wait and then reuse it
            if not (info := _read(link)):
                info = _build(home, link, key, lock, text, base)
    _ready[key] = info
    return info


def _fail(lock: Path, why: str) -> dict:
    raise Unavailable(f"the checks' pinned Python environment for {lock} could not be made: {why}. "
                      "Fix the lock (checks.python_lock) or the package index (pip's PIP_INDEX_URL or "
                      "PIP_FIND_LINKS) and run the checks again; they never fall back to ~/.local")


def _build(home: Path, link: Path, key: str, lock: Path, text: bytes, base: str) -> dict:
    """Makes the env in a fresh folder and moves `link` to it in one rename; the old folder stays."""
    new = home / f"{key}.{secrets.token_hex(4)}"
    env = {**_clean(dict(os.environ)), "PIP_USER": "0", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    steps = [[base, "-m", "venv", "--system-site-packages", str(new)],
             [str(new / "bin" / "python"), "-m", "pip", "install", "-q", "-r", str(lock)],
             [str(new / "bin" / "python"), "-c", "import json, platform, pytest; "
              "print(json.dumps([platform.python_version(), pytest.__version__]))"]]
    out = ""
    for argv in steps:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=BUILD_TIMEOUT_S, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            r = subprocess.CompletedProcess(argv, 1, "", str(e))
        if r.returncode != 0:
            shutil.rmtree(new, ignore_errors=True)
            tail = " / ".join((r.stderr or r.stdout).strip().splitlines()[-3:])[-400:]
            _fail(lock, f"`{' '.join(argv[:4])} ...` exited {r.returncode}: {tail}")
        out = r.stdout
    python, pytest = json.loads(out.strip().splitlines()[-1])
    info = {"key": key, "lock": str(lock), "lock_sha256": hashlib.sha256(text).hexdigest(),
            "python": python, "pytest": pytest}
    (new / INFO).write_text(json.dumps(info) + "\n")
    tmp = home / f".{key}.{secrets.token_hex(4)}.link"
    os.symlink(new.name, tmp)
    os.replace(tmp, link)
    return {**info, "path": str(new)}


def apply(env: dict, lock: Path | None = None) -> dict:
    """`env` with the pinned env in force: user site off, PIP_USER unset, its bin first on PATH, and
    TTP_CHECKS_PYTHON_ENV naming its key (so a pass's environment key tells envs apart)."""
    info = ensure(lock)
    out = {k: v for k, v in env.items() if k != "PIP_USER"}
    out["PATH"] = os.pathsep.join([str(Path(info["path"]) / "bin"), *([env["PATH"]] if env.get("PATH") else [])])
    return {**out, "PYTHONNOUSERSITE": "1", "TTP_CHECKS_PYTHON_ENV": info["key"]}


def describe(info: dict) -> str:
    """One line naming the env, for the checks output and record."""
    return (f"python env {info['key']} (python {info.get('python', '?')}, pytest {info.get('pytest', '?')}, "
            f"lock {str(info.get('lock_sha256', ''))[:12]} from {info.get('lock', '?')})")
