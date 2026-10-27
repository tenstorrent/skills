# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Push-check passes the push queue recorded itself, so a batch retried after being cut short (a
reboot) does not run again a check that already passed on the same merged tree.

A pass is keyed on the tree of the checked head, the exact check command and env_id: the
interpreters and venv the check would use (by content, version and installed packages, not by where
they live) and the tip it was checked against, so another worktree of the same tree reuses it. Only
passes are recorded: a failure, a check skipped by if_exists or if_changed and one never run leave nothing. The
batch reads only what a batch recorded here, never the passes `ttp checks` records; it trusts the
project's state as it trusts its own markers there, which name the heads it pushes. The store keeps
the newest MAX entries younger than MAX_AGE_S.

A check that is one plain pytest run also resumes (resumable): the ttp_pytest_resume plugin appends
each test file whose every selected test passed to a file keyed the same way, and the next run of
that check under the same key deselects those files and runs the rest. Files are skipped whole,
never single tests. A whole pass removes the file; the newest RESUME_MAX younger than
RESUME_MAX_AGE_S are kept.

A cap probe (batch.Batch._caps: only a check's prompt size cap tests, through the ttp_pytest_select
plugin) is recorded passed or failed, with its output tail, keyed the same way on the tree of the
replayed entries before their version bump, so a batch rerun after a reboot skips the prefixes it
already knows."""
from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import push
from .project import Project, write_json

STORE = "push_check_passes.json"     # under the project's state: {"passes": [{"key", "tree", "cmd", "batch", "ts", "parts"}]}
MAX, MAX_AGE_S = 200, 14 * 86400
RESUME_DIR = "push_check_resume"     # under the project's state: <key>.txt, one passed test file per line
RESUME_MAX, RESUME_MAX_AGE_S = 20, 3 * 86400
CAPS = "push_cap_probes.json"        # under the project's state: {"probes": [{"key", "tree", "cmd", "ok", "tail", "batch", "ts"}]}
PLUGIN_DIR = Path(__file__).resolve().parent / "pytest_resume"
PLUGIN = "ttp_pytest_resume"
SELECT_PLUGIN = "ttp_pytest_select"  # runs only the tests whose name matches TTP_PYTEST_SELECT (a cap probe)
# A check that sets these itself would drop the plugin or the path it loads from.
_OWN_ENV = ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "env -i")


def tree_of(wt: Path, head: str) -> str:
    r = subprocess.run(["git", "-C", str(wt), "rev-parse", f"{head}^{{tree}}"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _stat(path: str | Path) -> list:
    try:
        st = os.stat(path)
        return [str(path), st.st_ino, st.st_size, st.st_mtime_ns]
    except OSError:
        return [str(path), None]


WT = "<worktree>"   # stands in for the worktree's path: two worktrees of one tree share their passes
_CACHE: dict = {}   # (what, path, inode, size, mtime, roots) -> its identity, for this process


def _roots(wt: Path) -> list[str]:
    """The worktree's path as given and resolved, longest first ("/" never)."""
    return sorted({r for r in (str(wt).rstrip("/"), os.path.realpath(wt)) if r and r != "/"}, key=len, reverse=True)


def _local(text: str, roots: list[str]) -> str:
    """`text` with each path in the worktree written from WT, so where the worktree lives does not count."""
    for root in roots:
        text = re.sub(re.escape(root) + r"(?![\w.-])", WT, text)
    return text


def _cached(what: str, path: str | Path, roots: list[str], make):
    try:
        st = os.stat(path)
    except OSError:
        return None
    k = (what, str(path), st.st_ino, st.st_size, st.st_mtime_ns, tuple(roots))
    if k not in _CACHE:
        _CACHE[k] = make()
    return _CACHE[k]


def _content(path: str | Path, roots: list[str]) -> str | None:
    """A hash of the file `path` with paths in the worktree written from WT, or None when unreadable."""
    def make():
        try:
            return hashlib.sha256(_local(Path(path).read_text(errors="replace"), roots).encode()).hexdigest()
        except OSError:
            return None
    return _cached("text", path, roots, make)


def _binary(real: str) -> tuple:
    """A resolved interpreter's (content hash, implementation and version); the version is "" when it
    does not run, the hash its size and time when it cannot be read."""
    def make():
        h = hashlib.sha256()
        try:
            with open(real, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            digest = h.hexdigest()
        except OSError:
            digest = str(_stat(real)[2:])
        try:
            r = subprocess.run([real, "-I", "-S", "-c", "import sys; print(sys.implementation.name, sys.version)"],
                               capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            version = " ".join(r.stdout.split()) if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            version = ""
        return digest, version
    return _cached("binary", real, [], make) or ("", "")


def _site(site: Path, roots: list[str]) -> list:
    """What a venv's site-packages holds: each entry by name; a distribution also by its RECORD (the
    hashes of its files) or, installed editable, its METADATA and source; .pth, .egg-link and editable
    finder files by content. Paths in the worktree read from WT."""
    try:
        names = sorted(n for n in os.listdir(site) if n != "__pycache__")
    except OSError:
        return [None]
    out: list = []
    for n in names:
        path = site / n
        if n.endswith(".dist-info"):
            try:
                editable = bool(json.loads((path / "direct_url.json").read_text()).get("dir_info", {}).get("editable"))
            except (OSError, ValueError, AttributeError):
                editable = False
            files = ("METADATA", "direct_url.json") if editable else ("RECORD",)
            out.append([n] + [_content(path / f, roots) for f in files])
        elif n.endswith((".pth", ".egg-link")) or n.startswith("__editable__"):
            out.append([n, _content(path, roots)])
        else:
            out.append(n)
    return out


def _python(py: str | Path | None, roots: list[str]) -> dict:
    """What tells one interpreter or venv from another, wherever it lives: the binary it resolves to
    (by content), its implementation and version, and for a venv its config and installed packages."""
    if not py:
        return {"interpreter": None, "version": None, "deps": None}
    digest, version = _binary(os.path.realpath(py))
    venv = Path(os.path.realpath(Path(py).parent.parent))
    deps = None
    if (cfg := venv / "pyvenv.cfg").exists():
        try:
            lines = [ln for ln in cfg.read_text(errors="replace").splitlines()
                     if ln.partition("=")[0].strip() != "prompt"]   # the folder's name, by default
        except OSError:
            lines = None
        deps = {"cfg": _local("\n".join(lines), roots + [str(venv)]) if lines is not None else None,
                "site": {os.path.relpath(d, venv): _site(Path(d), roots)
                         for d in sorted(glob.glob(str(venv / "lib*" / "python*" / "site-packages")))}}
    return {"interpreter": digest, "version": version, "deps": deps}


def env_parts(wt: Path, cmd: str, env: dict, tip: str) -> dict:
    """The parts of env_id, by name: the command as it runs (argv, with the venv python it is given),
    the interpreter (by content), its version and the installed packages (deps) of that venv and of
    the python3 on the check's PATH, the active venv and the tip. Paths in the worktree `wt` read the
    same in every worktree. A check that runs on another machine (push.remote_host) is told by that
    host, its command and env, and the tip."""
    if host := push.remote_host(cmd):
        return {"host": host, "cmd": str(cmd), "env": dict(getattr(cmd, "env", ()) or ()), "tip": tip or ""}
    roots = _roots(wt)
    argv = [_local(str(w), roots) for w in push.check_argv(cmd, wt, env, isolate=False)]   # sandboxed or not, the same tests
    venv = _python(push.venv_python(wt, env), roots)
    path = _python(shutil.which("python3", path=env.get("PATH")), roots)
    return {"argv": argv, **{k: {"venv": venv[k], "python3": path[k]} for k in ("interpreter", "version", "deps")},
            "active": _local(env.get("VIRTUAL_ENV") or "", roots), "tip": tip or ""}


def digest(parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def env_id(wt: Path, cmd: str, env: dict, tip: str) -> str:
    """The environment part of a pass's key (env_parts)."""
    return digest(env_parts(wt, cmd, env, tip))


def part_digests(parts: dict) -> dict[str, str]:
    """Each part of env_parts as a short hash, kept with a pass to say which part a miss differs in."""
    return {k: digest(v)[:16] for k, v in parts.items()}


def differs(old, new: dict) -> str:
    """The parts (part_digests) in which `new` differs from a pass's `old`, comma-separated; "" when the
    pass kept none (recorded before they were) or none differs."""
    if not isinstance(old, dict) or not old:
        return ""
    return ", ".join(k for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k))


# Beyond env_id, what changes the tests a check runs or how: so a `ttp checks` pass serves
# `ttp push --own` only where these match too.
CHECK_ENV_VARS = ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD")


def check_env_parts(wt: Path, cmds: list, env: dict) -> list[dict]:
    """Per command, the environment a `ttp checks` pass records and `ttp push --own` compares before
    it reuses that pass: env_parts without a tip (`ttp checks` checks against none), plus
    CHECK_ENV_VARS as `env` sets them (vars)."""
    roots = _roots(wt)
    own = {k: _local(env.get(k, ""), roots) for k in CHECK_ENV_VARS}
    return [{**env_parts(wt, c, env, ""), "vars": own} for c in cmds]


def check_envs(wt: Path, cmds: list, env: dict) -> list[str]:
    """check_env_parts, each as one hash."""
    return [digest(x) for x in check_env_parts(wt, cmds, env)]


def key(tree: str, cmd: str, eid: str) -> str:
    return hashlib.sha256(json.dumps([tree, cmd, eid]).encode()).hexdigest()


def _load(path: Path, field: str = "passes") -> list[dict]:
    try:
        passes = json.loads(path.read_text()).get(field)
    except (OSError, ValueError, AttributeError):
        return []
    return [e for e in passes if isinstance(e, dict)] if isinstance(passes, list) else []


def _fresh(entries: list[dict], now: float) -> list[dict]:
    return [e for e in entries if now - float(e.get("ts") or 0) < MAX_AGE_S][-MAX:]


def passed(p: Project, tree: str, cmd: str, eid: str) -> dict | None:
    """The recorded pass of `cmd` on `tree` under `eid`, or None."""
    if not tree:
        return None
    k, now = key(tree, cmd, eid), time.time()
    for e in reversed(_fresh(_load(p.state / STORE), now)):
        if e.get("key") == k:
            return e
    return None


def missed(p: Project, tree: str, cmd: str, parts: dict) -> str:
    """Why no pass of `cmd` on `tree` was reused although one is recorded under another environment:
    the env_parts it differs in, or "" when there is none (or it kept no parts)."""
    if not tree:
        return ""
    now, new = time.time(), part_digests(parts)
    for e in reversed(_fresh(_load(p.state / STORE), now)):
        if e.get("tree") == tree and e.get("cmd") == cmd and (why := differs(e.get("parts"), new)):
            return f"the pass from {e.get('batch') or '?'} on this tree ran in another environment: {why} differ"
    return ""


def record(p: Project, tree: str, cmd: str, eid: str, batch: str, parts: dict | None = None) -> str:
    """Record a pass (only ever a pass), with the part_digests of its env_parts when given; prune the
    store. Returns why it could not, or ""."""
    if not tree:
        return "no tree"
    path = p.state / STORE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + ".lock"), "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            now = time.time()
            entries = _load(path) + [{"key": key(tree, cmd, eid), "tree": tree, "cmd": cmd, "batch": batch, "ts": now,
                                      **({"parts": part_digests(parts)} if parts else {})}]
            write_json(path, {"passes": _fresh(entries, now)})
    except OSError as e:
        return str(e)
    return ""


def probe_known(p: Project, tree: str, cmd: str, eid: str) -> dict | None:
    """The recorded cap probe ({"ok", "tail", "batch"}) of `cmd` on `tree` under `eid`, or None."""
    if not tree:
        return None
    k, now = key(tree, cmd, eid), time.time()
    for e in reversed(_fresh(_load(p.state / CAPS, "probes"), now)):
        if e.get("key") == k:
            return e
    return None


def probe_record(p: Project, tree: str, cmd: str, eid: str, ok: bool, tail: str, batch: str) -> str:
    """Record a cap probe, passed or failed, so a batch rerun after a reboot skips it; prune the
    store. Returns why it could not, or ""."""
    if not tree:
        return "no tree"
    path = p.state / CAPS
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + ".lock"), "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            now = time.time()
            entries = _load(path, "probes") + [{"key": key(tree, cmd, eid), "tree": tree, "cmd": cmd, "ok": bool(ok),
                                                "tail": tail, "batch": batch, "ts": now}]
            write_json(path, {"probes": _fresh(entries, now)})
    except OSError as e:
        return str(e)
    return ""


def select_env(pattern: str, env: dict) -> dict:
    """The variables that load the select plugin to run only the tests whose name matches `pattern`."""
    pp = os.pathsep.join(filter(None, (str(PLUGIN_DIR), env.get("PYTHONPATH", ""))))
    opts = " ".join(filter(None, (env.get("PYTEST_ADDOPTS", ""), f"-p {SELECT_PLUGIN}")))
    return {"PYTHONPATH": pp, "PYTEST_ADDOPTS": opts, "TTP_PYTEST_SELECT": pattern}


def resumable(cmd: str) -> bool:
    """`cmd` runs pytest exactly once and sets none of what loads the resume plugin."""
    return len(push._PYTEST_RUN.findall(cmd)) == 1 and not any(w in cmd for w in _OWN_ENV)


def resume_file(p: Project, tree: str, cmd: str, eid: str) -> Path:
    return p.state / RESUME_DIR / f"{key(tree, cmd, eid)}.txt"


def resumed(path: Path) -> list[str]:
    """The test files recorded passing in `path` (a last line cut mid-write is not one)."""
    try:
        lines = path.read_text().split("\n")
    except OSError:
        return []
    return [line for line in lines[:-1] if line]


def resume_env(path: Path, env: dict) -> dict:
    """The variables that load the resume plugin with `path` as its record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    prune(path.parent, keep=path)
    path.touch()
    pp = os.pathsep.join(filter(None, (str(PLUGIN_DIR), env.get("PYTHONPATH", ""))))
    opts = " ".join(filter(None, (env.get("PYTEST_ADDOPTS", ""), f"-p {PLUGIN}")))
    return {"PYTHONPATH": pp, "PYTEST_ADDOPTS": opts, "TTP_PYTEST_RESUME": str(path)}


def prune(folder: Path, keep: Path | None = None) -> None:
    """Drop resume files older than RESUME_MAX_AGE_S and all but the newest RESUME_MAX."""
    now = time.time()
    try:
        files = sorted(folder.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return
    for n, f in enumerate(files):
        try:
            if f != keep and (n >= RESUME_MAX or now - f.stat().st_mtime > RESUME_MAX_AGE_S):
                f.unlink()
        except OSError:
            pass
