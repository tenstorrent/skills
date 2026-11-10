# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The pinned Python environment every check runs in (push.check_env), so a package installed or
upgraded in the user site (~/.local, `pip install --user`) never changes what a check imports.

A check runs with PYTHONNOUSERSITE=1 (the user site never loads), PIP_USER unset, and PATH fronted
by a harness-owned virtualenv's bin, so `python3`, `python3 -m pytest` and `pytest` resolve there.
The virtualenv sees the system's site-packages (packages the OS installs, such as PyYAML) below its
own, so its pinned pytest wins over any other. macOS Pythons ship no PyYAML, so LOCK pins it for
sys_platform darwin only; a line for another platform leaves this host's env key unchanged (_for_host).

Its packages come from a lock: `checks.python_lock` (a requirements file with exact pins, hashes
where possible; relative to the checkout) when the project sets it, else LOCK beside this file
(pytest and pytest-xdist). A `# requires-python: >=3.10` line in the lock names the Pythons its pins
install on: when the one running ttp is older (macOS's own /usr/bin/python3 is 3.9), the env is made
from the first Python on PATH or in the usual install folders that meets it (interpreter), else
nothing runs and the error names the Python needed and the one found. It is built once under
TTP_HOME/checks-env, keyed by the lock's content and the base interpreter, and reused. A build goes to a fresh folder under a file lock, then the
key's link moves to it in one rename, so a lock change builds beside the old env and a running
check never sees one rebuilt under it. A build that fails stops the checks with what to fix, never
a fallback to the user site: a host fault (fault, envfault) the checks wait on, unless the change
being checked edited the lock itself (changed_by), which then fails it."""
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


def at_commit(cfg: dict | None, top: str | Path, head: str, out: str | Path) -> Path:
    """The lock of a check of commit `head` in checkout `top` (lock_of): a relative checks.python_lock as
    committed at `head` (written to folder `out`), never the worktree's copy, which an uncommitted edit
    or a pinned run leaves another commit's. Unavailable when `head` has no such file."""
    lock = lock_of(cfg, top)
    try:
        rel = lock.resolve().relative_to(Path(top).resolve())
    except ValueError:   # LOCK or an absolute lock outside the checkout: as it is
        return lock
    r = subprocess.run(["git", "-C", str(top), "show", f"{head}:{rel.as_posix()}"], capture_output=True)
    if r.returncode != 0:
        raise Unavailable(f"checks.python_lock {rel} is not in commit {head[:12]}; commit it or fix checks.python_lock")
    path = Path(out) / f"checks-python-{hashlib.sha256(r.stdout).hexdigest()[:12]}.lock"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
        tmp.write_bytes(r.stdout)
        os.replace(tmp, path)
    return path


def changed_by(top: str | Path, cfg: dict | None, base: str, head: str) -> bool:
    """Whether commit `head` changed the checks.python_lock in checkout `top` from `base`: a lock that
    then fails to install is the head's fault; a lock it did not change is the host's (fault)."""
    lock = lock_of(cfg, top)
    try:
        rel = lock.resolve().relative_to(Path(top).resolve())
    except ValueError:
        return False
    if not base:   # nothing to compare with: the checkout's own file
        return True
    r = subprocess.run(["git", "-C", str(top), "diff", "--quiet", base, head, "--", rel.as_posix()],
                       capture_output=True)
    return r.returncode == 1


FAULT = "env:python-env:"   # an envfault signature: the pinned env cannot be made on this host


def fault(lock: Path | None, err: object, cmd: str = "") -> dict:
    """The envfault fault of the env of `lock` that could not be made: the host's, not the change's,
    so checks wait for it (envfault.record) and nothing is counted against a change."""
    lock = Path(lock or LOCK)
    try:
        sig = hashlib.sha256(lock.read_bytes()).hexdigest()[:12]
    except OSError:
        sig = lock.name
    return {"kind": "pyenv", "sig": FAULT + sig, "runner": "pinned Python env", "lock": str(lock),
            "why": str(err)[-600:], "cmd": cmd or "python env"}


def ready(lock: Path | None) -> bool:
    """Whether the env of `lock` is made. When not, starts one build of it in the background (as
    another host's runs do, __main__) unless one runs, and never waits: a probe (envfault) stays quick."""
    if test := os.environ.get("TTP_TEST_CHECKS_PYTHON_ENV"):
        return bool(_read(Path(test)))
    lock = Path(lock or LOCK)
    try:
        base, version = interpreter(lock.read_bytes(), lock)
    except (OSError, Unavailable):
        return False
    key = key_of(lock.read_bytes(), base, version)
    home = root()
    if key in _ready or _read(home / key):
        return True
    try:
        home.mkdir(parents=True, exist_ok=True)
        with open(home / f".{key}.lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)   # held: a build runs already
        # own session: the env build outlives this call by design; its flock ends it once and a rerun reuses it
        subprocess.Popen([base, "-I", __file__, str(lock.resolve())], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env=_clean(dict(os.environ)))
    except OSError:
        pass
    return False


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


def key_of(text: bytes, base: str, version: str | None = None) -> str:
    """The env's key: the lock's content as this host reads it (_for_host), and the base interpreter's
    path, version (its sys.version; default this one's) and machine."""
    ident = f"{os.path.realpath(base)}\n{version or sys.version}\n{platform.machine()}\n".encode()
    return hashlib.sha256(ident + _for_host(text)).hexdigest()[:16]


_PLATFORM = re.compile(r"""\s*sys_platform\s*(==|!=)\s*["']([^"']*)["']\s*""")


def _for_host(text: bytes) -> bytes:
    """`text` without the requirements pip skips on this host for a lone `sys_platform` marker, each
    with its continuation lines: a package pinned for one platform only (PyYAML on macOS, whose
    Pythons ship none while Linux distributions do) leaves the other platforms' keys as they were, so
    a host that cannot build a new env (a lab box without a package index) keeps the one it has."""
    out, lines, i = [], text.splitlines(keepends=True), 0
    while i < len(lines):
        j = i
        while lines[j].rstrip(b"\r\n").endswith(b"\\") and j + 1 < len(lines):
            j += 1
        req = b"".join(ln.rstrip(b"\r\n").rstrip(b"\\") for ln in lines[i:j + 1]).decode(errors="replace")
        req = re.split(r"\s--hash[=\s]", req, maxsplit=1)[0]
        m = None if req.lstrip().startswith("#") or ";" not in req else _PLATFORM.fullmatch(req.split(";", 1)[1])
        if _REQUIRES.fullmatch(req.strip()):
            pass   # picks the interpreter, which the key names itself: a lock gaining it keeps its envs
        elif not m or (sys.platform == m.group(2)) == (m.group(1) == "=="):
            out += lines[i:j + 1]
        i = j + 1
    return b"".join(out)


_REQUIRES = re.compile(r"#\s*requires-python\s*:\s*(.*?)\s*", re.I)
_CLAUSE = re.compile(r"(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)")
# Where a Python is looked for besides PATH (macOS installers, Homebrew, pyenv-free Linux defaults)
_PY_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/Library/Frameworks/Python.framework/Versions/Current/bin",
            "/opt/homebrew/opt/python@3.*/bin", "/Library/Frameworks/Python.framework/Versions/3.*/bin", "/usr/bin")
_found: dict[str, tuple[str, str, tuple] | None] = {}   # candidate path -> (base, sys.version, version_info)


def requires(text: bytes, lock: Path | str = "the lock") -> list[tuple[str, tuple]] | None:
    """The clauses of `text`'s `# requires-python:` line ([(">=", (3, 10))]), None without one.
    Unavailable for a line it cannot read: the checks then name it, never guess a Python."""
    for ln in text.decode(errors="replace").splitlines():
        if m := _REQUIRES.fullmatch(ln.strip()):
            parts = [c.strip() for c in m.group(1).split(",")]
            got = [_CLAUSE.fullmatch(c) for c in parts]
            if not parts or not all(got):
                raise Unavailable(f"the requires-python line of {lock} ({ln.strip()!r}) is not a list of "
                                  "comparisons such as `>=3.10` or `>=3.10, <3.14`; fix it in the lock")
            return [(g.group(1), tuple(int(x) for x in g.group(2).split("."))) for g in got]
    return None


def _meets(v: tuple, spec: list[tuple[str, tuple]]) -> bool:
    """Whether Python version `v` (3, 9, 6) meets every clause: == and != compare as many parts as
    the clause gives (==3.10 is any 3.10.x), the others compare 3.10 as 3.10.0."""
    for op, want in spec:
        n = max(len(want), 3)
        a, b = (tuple(v[:len(want)]), want) if op in ("==", "!=") else (
            tuple(v[:n]) + (0,) * (n - len(v[:n])), want + (0,) * (n - len(want)))
        if not {"==": a == b, "!=": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]:
            return False
    return True


def _candidates() -> list[str]:
    """Pythons to try, in order: python3 then python3.N (newest first) in each PATH folder, then in
    _PY_DIRS."""
    import glob
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    for d in _PY_DIRS:
        dirs += sorted(glob.glob(d), key=lambda p: [int(x) if x.isdigit() else -1 for x in re.split(r"(\d+)", p)],
                       reverse=True) if "*" in d else [d]
    names = ["python3"] + [f"python3.{n}" for n in range(20, 7, -1)]
    out, seen = [], set()
    for d in dirs:
        for n in names:
            c = os.path.join(d, n)
            if os.path.isfile(c) and os.access(c, os.X_OK) and c not in seen:
                seen.add(c)
                out.append(c)
    return out


def _probe(c: str) -> tuple[str, str, tuple] | None:
    """(base interpreter, sys.version, version_info) of Python `c`, its base outside any venv as
    _base_python finds it; None when it does not run (too old for this file, not a Python)."""
    if c not in _found:
        try:
            r = subprocess.run([c, "-I", __file__, "--interpreter"], capture_output=True, text=True, timeout=30,
                               env=_clean(dict(os.environ)), stdin=subprocess.DEVNULL)
            base, version, info = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else (0, 0, 0)
            _found[c] = (base, version, tuple(info)) if base else None
        except (OSError, ValueError, IndexError, TypeError, subprocess.SubprocessError):
            _found[c] = None
    return _found[c]


def interpreter(text: bytes, lock: Path | str = "the lock") -> tuple[str, str]:
    """(base interpreter, its sys.version) the env of lock `text` is made from: this one's base
    (_base_python) when it meets the lock's requires-python or the lock names none, else the first
    candidate that does. Unavailable naming the Python needed and the one found when none does."""
    base, spec = _base_python(), requires(text, lock)
    if spec is None or _meets(sys.version_info[:3], spec):
        return base, sys.version
    for c in _candidates():
        if (got := _probe(c)) and _meets(got[2], spec):
            return got[0], got[1]
    need = ", ".join(op + ".".join(map(str, v)) for op, v in spec)
    raise Unavailable(
        f"the checks' Python lock {lock} needs Python {need} (its requires-python line), but ttp runs under "
        f"Python {platform.python_version()} ({base}) and no Python on PATH or in {', '.join(_PY_DIRS)} meets "
        f"it; install a Python {need} there (or run ttp with one: TTP_PYTHON) and run the checks again")


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
    base, version = interpreter(text, lock)
    key = key_of(text, base, version)
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


def remote_lines(lock: Path | str | None, where: str, fail: str, home: str = "") -> str:
    """Shell lines for another host (remotecheck, slurmcheck): write this file and `lock` into folder
    `where` there, make or reuse the pinned env from them with that host's python3 (as ensure does
    here, under its TTP_HOME, or `home`, a shell word, when given), and put it in force as apply does.
    Where it cannot be made, they print why and run `fail`: the check never runs with that host's user
    site. TTP_CHECKS_ENV_BIN names its bin, so a check behind a spill wrapper puts it first again
    (remotecheck.REFRONT). OSError when a file cannot be read."""
    lock = Path(lock or LOCK)
    out, w = f"mkdir -p {shlex.quote(where)}\n", shlex.quote(where)
    for name, text in (("runnerenv.py", Path(__file__).read_text()), ("checks-python.lock", lock.read_text())):
        end = f"TTP_ENV_{secrets.token_hex(8)}"
        out += f"cat > {w}/{name} <<'{end}'\n{text.rstrip(chr(10))}\n{end}\n"
    return (out + f'ttp_env=$({f"TTP_HOME={home} " if home else ""}python3 -I {w}/runnerenv.py "$PWD"/{w}/checks-python.lock) || {{ '
            f"echo 'ttp: the checks pinned Python env could not be made on this host; the check did not run' >&2; {fail}; }}\n"
            'export PATH="$ttp_env/bin${PATH:+:$PATH}" PYTHONNOUSERSITE=1 TTP_CHECKS_ENV_BIN="$ttp_env/bin"\n'
            'unset PIP_USER\n')


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
    pins = [re.split(r"\s--hash=", ln)[0].rstrip() for ln in text.splitlines()
            if ln.strip() and (not ln.startswith("#") or _REQUIRES.fullmatch(ln.strip()))]
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
    if sys.argv[1:] == ["--interpreter"]:   # _probe: this Python's base, sys.version and version_info
        print(json.dumps([_base_python(), sys.version, list(sys.version_info[:3])]))
        sys.exit(0)
    try:
        print(ensure(Path(sys.argv[1]))["path"])
    except Unavailable as e:
        print(f"ttp: {e}", file=sys.stderr)
        sys.exit(1)
