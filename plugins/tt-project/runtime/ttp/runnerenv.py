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
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

LOCK = Path(__file__).with_name("checks-python.lock")
LOCK_ENV = "TTP_CHECKS_PYTHON_LOCK"   # in a check's env (apply): the lock, for a check run on another host
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
    """The interpreter envs are made from: this one's base outside any venv (a venv's python would see
    that venv's packages; before 3.11 sys._base_executable names it inside a venv)."""
    if sys.prefix == sys.base_prefix:
        return sys.executable
    v = sys.version_info
    for c in (getattr(sys, "_base_executable", ""), Path(sys.base_prefix) / "bin" / f"python{v.major}.{v.minor}",
              Path(sys.base_prefix) / "bin" / "python3", os.path.realpath(sys.executable)):
        if c and Path(c).exists() and not Path(os.path.abspath(c)).is_relative_to(os.path.abspath(sys.prefix)):
            return str(c)
    return sys.executable


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
        if test not in _ready:
            _ready[test] = _read(Path(test)) or _fail(Path(test), "the test env is not whole")
        return _ready[test]
    lock = Path(lock or LOCK)
    try:
        text = lock.read_bytes()
    except OSError as e:
        raise Unavailable(f"the checks' Python lock {lock} cannot be read ({e.strerror or e}); fix "
                          "checks.python_lock (`ttp config <project> checks.python_lock <file>`) or restore the file")
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
             [str(new / "bin" / "python"), "-c", "import json, platform\ntry:\n import pytest; v = pytest.__version__\n"
              "except ImportError:\n v = None\nprint(json.dumps([platform.python_version(), v]))"]]
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
    return {**out, "PYTHONNOUSERSITE": "1", "TTP_CHECKS_PYTHON_ENV": info["key"],
            LOCK_ENV: str(Path(lock or LOCK).resolve())}


def remote_lines(lock: Path | str | None, where: str, fail: str) -> str:
    """Shell lines for another host (remotecheck, slurmcheck): write this file and `lock` into folder
    `where` there, make or reuse the pinned env from them with that host's python3 (as ensure does
    here, under its TTP_HOME), and put it in force as apply does. Where it cannot be made, they print
    why and run `fail`: the check never runs with that host's user site. OSError when a file cannot be read."""
    lock = Path(lock or LOCK)
    out, w = f"mkdir -p {shlex.quote(where)}\n", shlex.quote(where)
    for name, text in (("runnerenv.py", Path(__file__).read_text()), ("checks-python.lock", lock.read_text())):
        end = f"TTP_ENV_{secrets.token_hex(8)}"
        out += f"cat > {w}/{name} <<'{end}'\n{text.rstrip(chr(10))}\n{end}\n"
    return (out + f'ttp_env=$(python3 -I {w}/runnerenv.py "$PWD"/{w}/checks-python.lock) || {{ '
            f"echo 'ttp: the checks pinned Python env could not be made on this host; the check did not run' >&2; {fail}; }}\n"
            'export PATH="$ttp_env/bin${PATH:+:$PATH}" PYTHONNOUSERSITE=1\nunset PIP_USER\n')


def _name(s: str) -> str:
    return re.sub(r"[-_.]+", "-", s).lower()


def freeze(out: Path, lock: Path | None = None) -> list[str]:
    """Writes a lock for checks.python_lock to `out`: `lock`'s pins (default LOCK) plus every package
    in the user site (~/.local) that it does not name, at the version installed there, so checks that
    import those keep them once the user site is off. Exact pins without hashes (pip's hash-checking
    needs every line hashed, and a hash of this host's wheel can refuse another host's). Reads package
    metadata only: installs nothing. Returns the pins it added from the user site."""
    from importlib import metadata
    try:
        text = Path(lock or LOCK).read_text()
    except OSError as e:
        raise Unavailable(f"the lock {lock or LOCK} cannot be read ({e.strerror or e})")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONNOUSERSITE"}
    r = subprocess.run([_base_python(), "-c", "import site; print(site.getusersitepackages())"],
                       capture_output=True, text=True, timeout=60, env=env)
    usersite = r.stdout.strip()
    pins = [re.split(r"\s--hash=", ln)[0].rstrip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    have = {_name(re.split(r"[=<>!~ ;\[]", p, maxsplit=1)[0]) for p in pins}
    added = {}
    for d in (metadata.distributions(path=[usersite]) if usersite and Path(usersite).is_dir() else ()):
        n = d.metadata["Name"]
        if n and _name(n) not in have:
            added.setdefault(_name(n), f"{n}=={d.version}")
    added = sorted(added.values(), key=str.lower)
    Path(out).write_text(f"# The checks' Python lock (checks.python_lock), written by `ttp checks --freeze-lock`:\n"
                         f"# the pins of {lock or LOCK} and the packages of the user site it does not name.\n"
                         "# Exact pins without hashes; review it, drop what the checks do not import.\n"
                         + "".join(p + "\n" for p in pins) + "".join(p + "\n" for p in added))
    return added


def describe(info: dict) -> str:
    """One line naming the env, for the checks output and record."""
    return (f"python env {info['key']} (python {info.get('python', '?')}, pytest {info.get('pytest', '?')}, "
            f"lock {str(info.get('lock_sha256', ''))[:12]} from {info.get('lock', '?')})")


if __name__ == "__main__":   # on another host (remote_lines): print the env's folder, made first when missing
    try:
        print(ensure(Path(sys.argv[1]))["path"])
    except Unavailable as e:
        print(f"ttp: {e}", file=sys.stderr)
        sys.exit(1)
