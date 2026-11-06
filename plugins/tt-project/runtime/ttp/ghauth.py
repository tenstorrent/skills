# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""This host's gh login, checked once for every project on it (Daemon.check_gh_auth).

A logged-out gh used to fail quietly: pr-watch read no PR and still said ok, a project's own gh schedule
printed an error and exited 0, and a start_when probe exited gh's 4 per task. Now one model-free check
per host runs `gh auth status` at most every EVERY_S, only while a project here uses gh (uses_gh), in
the daemon of whichever project takes the host-wide lock first; the others skip it and read its answer
from the state file (STATE under the tt-project home). While it says logged out, each project using gh
keeps kv KV set and asserts the alert KEY: a machine-wide condition (Daemon._machine_wide), so one
project broadcasts it and the others show it quietly. It names the host and the step only the user can
take (`gh auth login`), and clears once the check passes. A gh call elsewhere that failed for its
login (auth_failed) asks for a recheck at once (nudge) rather than waiting out EVERY_S.

`gh auth status` that fails for another reason (no network, a timeout) is no answer: the last one stands.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

KEY = "gh-auth"        # the alert's condition key (alerts.USER_BLOCKERS)
KV = "gh_auth_down"    # kv while this project uses gh and the host's gh is logged out
STATE = "gh-auth.json"
LOCK = "gh-auth.lock"
EVERY_S = 900.0        # `gh auth status` runs at most this often, host-wide
TIMEOUT_S = 20.0
AUTH_RC = 4            # gh's own exit code for "authentication required"
# What gh says when its login is missing or bad (not a network fault).
AUTH_RE = re.compile(r"not logged in(?:to)?\b|\bgh auth login\b|token .{0,60}\bis (?:invalid|expired)|"
                     r"bad credentials|HTTP 401\b", re.I)
_USES_RE = re.compile(r"(?<![\w-])gh\s+\w|\bttp ci\b")


def auth_failed(rc, text: str | bytes | None = "") -> bool:
    """Whether a gh call that exited `rc` with output `text` failed for want of a login."""
    if rc in (0, None):
        return False
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return rc == AUTH_RC or bool(AUTH_RE.search(text or ""))


def _home(home: Path | None) -> Path:
    from . import project
    return Path(home or project.HOME_DIR)


def read(home: Path | None = None) -> dict:
    try:
        v = json.loads((_home(home) / STATE).read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def down(home: Path | None = None) -> dict | None:
    """The host's open gh logout ({since, error, checked_at}), else None."""
    st = read(home)
    return st if st.get("ok") is False else None


def due(st: dict, now: float, every_s: float = EVERY_S) -> bool:
    checked = float(st.get("checked_at") or 0)
    return now - checked >= every_s or float(st.get("nudged") or 0) > checked


def _locked(home: Path | None, fn, block: bool):
    d = _home(home)
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        except OSError:
            return None   # another daemon is checking now
        return fn()
    finally:
        os.close(fd)


def _write(home: Path | None, st: dict) -> None:
    from .project import write_json
    write_json(_home(home) / STATE, st, 0o600)


def nudge(now: float | None = None, home: Path | None = None) -> None:
    """Ask for a recheck at the next check, whatever EVERY_S says (a gh call just failed its login)."""
    now = time.time() if now is None else now

    def put():
        st = read(home)
        if float(st.get("nudged") or 0) <= float(st.get("checked_at") or 0):
            _write(home, {**st, "nudged": now})
    try:
        _locked(home, put, block=True)
    except OSError:
        pass


def check(now: float | None = None, home: Path | None = None, every_s: float = EVERY_S) -> dict:
    """Run `gh auth status` once it is due host-wide and no other daemon is running it; return the
    host's state as it then stands."""
    now = time.time() if now is None else now

    def run():
        st = read(home)
        if not due(st, now, every_s):
            return st
        gh = shutil.which("gh")
        if not gh:
            return st
        try:
            r = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=TIMEOUT_S,
                               stdin=subprocess.DEVNULL)
            rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.SubprocessError) as e:
            rc, out = None, type(e).__name__
        st = {k: v for k, v in st.items() if k != "nudged"}
        st["checked_at"] = now
        if rc == 0:
            st = {"ok": True, "checked_at": now}
        elif rc is not None and auth_failed(rc, out):
            lines = [x.strip() for x in out.splitlines() if x.strip()]
            st.update(ok=False, since=st.get("since") if st.get("ok") is False else now,
                      error=(lines[0] if lines else f"exit {rc}")[:200])
        _write(home, st)
        return st
    try:
        got = _locked(home, run, block=False)
    except OSError:
        got = None
    return read(home) if got is None else got


def uses_gh(db) -> bool:
    """Whether this project uses gh now: an enabled schedule that calls it (pr-watch while a task has a
    PR), or an open task with a PR or a wait or start probe that calls gh or `ttp ci`."""
    from .db import OPEN_TASKS
    prs = bool(db.one(f"SELECT id FROM tasks WHERE {OPEN_TASKS} AND pr_url IS NOT NULL AND pr_url != '' LIMIT 1"))
    for s in db.q("SELECT payload FROM schedules WHERE enabled=1"):
        try:
            payload = json.loads(s["payload"] or "{}")
        except ValueError:
            payload = {}
        if prs and isinstance(payload, dict) and payload.get("builtin") == "prs" or _USES_RE.search(s["payload"] or ""):
            return True
    if prs:
        return True
    return any(_USES_RE.search((t["labels"] or "") + " " + (t["result"] or ""))
               for t in db.q(f"SELECT labels, result FROM tasks WHERE {OPEN_TASKS} AND status IN ('queued','waiting')"))


def text(st: dict, host: str, when: str) -> str:
    return (f"gh is logged out on {host} since {when} ({st.get('error') or 'gh auth status failed'}): PR watching, "
            f"CI waits and other gh calls of every project on {host} fail until the user runs `gh auth login` "
            f"there (ssh {host}). Nothing else is needed: the alert clears once `gh auth status` passes.")
