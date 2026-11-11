# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""This host's gh login, checked once for every project on it (Daemon.check_gh_auth).

A logged-out gh used to fail quietly: pr-watch read no PR and still said ok, a project's own gh schedule
printed an error and exited 0, and a start_when probe exited gh's 4 per task. Now one model-free check
per host runs `gh auth status` at most every EVERY_S, only while a project here uses gh (uses_gh), in
the daemon of whichever project takes the host-wide lock first; the others skip it and read its answer
from the state file (STATE under the tt-project home). While it says logged out, each project using gh
keeps kv KV set and asserts the alert KEY, unless a delivery.gh_route answers (ghroute): a machine-wide
harness condition (Daemon._machine_wide), so one project broadcasts it and the others show it quietly.
No GitHub login is kept on lab hosts, so it never asks anyone to log gh in: it names the route setting
and what is skipped, and clears once gh works (logged in here, or routed). A gh call elsewhere that failed for its
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

KEY = "gh-auth"        # the alert's condition key (a harness condition)
KV = "gh_auth_down"    # kv while this project uses gh and the host's gh is logged out
KNOWN_KV = "gh_auth_known"   # kv {since, mode}: the quiet status line of an expected logout was posted
STATE = "gh-auth.json"
LOCK = "gh-auth.lock"
ANNOUNCED = "gh-auth-alert.json"   # which project told the user of the current logout (announce), account-wide
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


def _locked(home: Path | None, fn, block: bool, lock: str = LOCK):
    d = _home(home)
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / lock, os.O_RDWR | os.O_CREAT, 0o600)
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


def nudge(now: float | None = None, home: Path | None = None, block: bool = True) -> None:
    """Ask for a recheck at the next check, whatever EVERY_S says (a gh call just failed its login).
    block=False (the daemon's tick) skips it while another daemon holds the lock: its check answers anyway."""
    now = time.time() if now is None else now

    def put():
        st = read(home)
        if float(st.get("nudged") or 0) <= float(st.get("checked_at") or 0):
            _write(home, {**st, "nudged": now})
    try:
        _locked(home, put, block=block)
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


def _commands(payload: dict) -> list:
    """The shell commands a schedule runs (its command, precheck and heal steps), never its spec prose."""
    heal = payload.get("heal") if isinstance(payload.get("heal"), dict) else {}
    return [payload.get("command"), payload.get("precheck")] + [heal.get(k) for k in ("check", "fix", "suppress",
                                                                                     "status_command")]


def _probes(result: dict) -> list:
    """The commands a hand-off has the daemon run (retry_when, on_pass_cmd and each later wait in `then`),
    never its summary, question or other prose. Only a waiting hand-off's probes still run."""
    if result.get("status") != "waiting":
        return []
    out = [result.get("retry_when"), result.get("on_pass_cmd")]
    later = result.get("then")
    for w in later if isinstance(later, list) else []:
        if isinstance(w, dict):
            out += [w.get("retry_when"), w.get("on_pass_cmd")]
    return out


def _runs_gh(cmds) -> bool:
    return any(isinstance(c, str) and _USES_RE.search(c) for c in cmds)


def uses_gh(db) -> bool:
    """Whether this project uses gh now: an enabled schedule whose commands call it (pr-watch while a task
    has a PR), or an open task with a PR, or whose start_when or hand-off probes call gh or `ttp ci`.
    Only commands that run count: a spec, summary or hand-off that mentions gh in words is no gh use."""
    from .db import OPEN_TASKS, deferral, load_result
    prs = bool(db.one(f"SELECT id FROM tasks WHERE {OPEN_TASKS} AND pr_url IS NOT NULL AND pr_url != '' LIMIT 1"))
    if prs:
        return True
    for s in db.q("SELECT payload FROM schedules WHERE enabled=1"):
        try:
            payload = json.loads(s["payload"] or "{}")
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and _runs_gh(_commands(payload)):
            return True
    return any(_runs_gh([deferral(t).get("when")] + _probes(load_result(t["result"])))
               for t in db.q(f"SELECT labels, result FROM tasks WHERE {OPEN_TASKS} AND status IN ('queued','waiting')"))


def announce(owner: str, since, home: Path | None = None) -> bool:
    """Whether `owner` (a project) tells the user of the logout that began at `since`: True only for the
    first project to ask in this logout, and for it again; every other project of this account shows it
    quietly, so a logout is one alert per account however often a project's own episode clears and
    reopens. A marker that cannot be read or written never silences anyone."""
    def take():
        f = _home(home) / ANNOUNCED
        try:
            m = json.loads(f.read_text())
        except (OSError, ValueError):
            m = {}
        if isinstance(m, dict) and m.get("since") == since and m.get("owner") not in (None, owner):
            return False
        if not (isinstance(m, dict) and m.get("since") == since):
            from .project import write_json
            write_json(f, {"since": since, "owner": owner, "at": time.time()}, 0o600)
        return True
    try:
        took = _locked(home, take, block=True, lock=ANNOUNCED + ".lock")
    except OSError:
        return True
    return took is not False


def forget(home: Path | None = None) -> None:
    """The logout is over (gh works, or a route or MODES value is set): the next one is told afresh."""
    try:
        (_home(home) / ANNOUNCED).unlink()
    except OSError:
        pass


def text(st: dict, host: str, when: str, cfg: dict | None = None) -> str:
    """The harness condition: what is skipped and the route setting (ghroute); never a login step."""
    from . import ghroute
    return ghroute.down_text(host, when, st.get("error") or "gh auth status failed", cfg)
