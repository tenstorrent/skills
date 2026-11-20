"""Looks the coordinator sets for itself: when it wakes next, what it checks and why.

The `look` action stores one. While any is pending, it replaces the fixed idle and idle-slot wakes
(daemon.idle_wake), so the coordinator, not a timer, decides when it checks in. A due look starts
a turn in maybe_coordinate unless its optional check, run model-free, exits 0 (nothing changed).

With coordinator.look_trim on (the default), a look set with no check of its own gets a default one:
add() snapshots the state its text names (fingerprint(): each named task's status, blocked reason and
latest run note, the open asks, and the push tip when it names landing), and screen() skips it when
that state is unchanged at its due time. A look whose text makes an unchanged state the reason to act
(an obligation key, a deadline, `if still`, `unless`) or names state outside the task records (hosts,
logs, paths, URLs, check runs) gets none and always fires. The next turn lists the looks skipped so.
A turn that starts also reads every pending look due within coordinator.wake_gap_s (ahead()), so
looks a few minutes apart, or just after another turn, cost one wake.
The overseer starts that turn itself when the tick loop is stuck (overseer.wake). Both paths take
the start lock, so they never start two turns."""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterator

from . import probespawn

KEY = "coord_looks"
DONE_KEY = "coord_looks_done"
MAX = 10                  # pending looks kept; a new one past this drops the latest-due
HORIZON_S = 7 * 86400     # a look further out is refused
CHECK_TIMEOUT_S = 20      # inside the coordinator tick step budget
START_LOCK = "coord-start.lock"
TASK_RE = re.compile(r"(?<![\w/&])#(\d{1,7})\b")
# Wording that makes an unchanged state the reason to act (obligation keys, deadlines, fallbacks), or
# names state the task records do not hold (hosts, logs, paths, URLs, check runs): no default check.
ACT_RE = re.compile(r"\bo\d+\b|\b(?:anyway|regardless|deadline|overdue|due|fallback|unless|still|"
                    r"if\s+(?:not|no)|no\s+(?:progress|answer|reply|change)|by\s+\d{1,2}:\d\d|ssh|logs?|"
                    r"hosts?|port|checks?\s+run)\b|https?:|/", re.I)
LAND_RE = re.compile(r"\b(?:push(?:ed|es)?|land(?:s|ed)?|merged?|tip|batch)\b", re.I)


def trim_on(cfg: dict) -> bool:
    """coordinator.look_trim: default checks and merging within the wake gap (default on)."""
    return bool((cfg.get("coordinator") or {}).get("look_trim", True))


def fingerprint(db, text: str, tip=None) -> dict | None:
    """The state look `text` names, read model-free, or None when an unchanged state may not skip it.
    `tip` returns the push branch tip; it is read only when the text names landing."""
    ids = sorted({int(m) for m in TASK_RE.findall(text or "")})[:20]
    if not ids or ACT_RE.search(text):
        return None
    tasks = {}
    for i in ids:
        t = db.task(i)
        if not t:
            return None
        r = db.one("SELECT id, status, dir FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (i,))
        note = Path(r["dir"]) / "progress.md" if r and r["dir"] else None
        try:
            size = note.stat().st_size if note else -1
        except OSError:
            size = -1
        tasks[str(i)] = [t["status"], t["blocked_reason"] or "", r["id"] if r else 0,
                         r["status"] if r else "", size]
    from . import waits
    fp = {"tasks": tasks, "asks": [a["id"] for a in waits.open_asks(db)]}
    if LAND_RE.search(text):
        try:
            fp["tip"] = tip() if tip else ""
        except Exception:
            fp["tip"] = ""
        if not fp["tip"]:
            return None
    return fp


def snapshot(p, text: str, check: str | None) -> dict | None:
    """add()'s state for a look on project `p`: None with a check of its own or look_trim off."""
    if (check or "").strip() or not trim_on(p.config()):
        return None
    from . import gatejudge
    return fingerprint(p.db, text, lambda: gatejudge._push_tip(p)[1])


def pending(db) -> list[dict]:
    return sorted(db.kv(KEY) or [], key=lambda x: x["at"])


def add(db, at: float, text: str, check: str | None = None, by: str = "coordinator",
        now: float | None = None, state: dict | None = None) -> dict:
    now = time.time() if now is None else now
    if at - now > HORIZON_S:
        raise ValueError(f"look: `at` is more than {HORIZON_S // 86400} days out")
    if not text.strip():
        raise ValueError("look: give `text`: what to check and why")
    look = {"id": uuid.uuid4().hex[:8], "at": max(at, now), "text": text.strip()[:1000],
            "check": (check or "").strip() or None, "by": by, "set_at": now}
    if state and not look["check"]:
        look["state"] = state
    with db.tx():
        db.set_kv(KEY, pending(db)[:MAX - 1] + [look])
    return look


def clear(db) -> int:
    with db.tx():
        n = len(pending(db))
        db.set_kv(KEY, [])
    return n


def due(db, now: float) -> list[dict]:
    return [x for x in pending(db) if x["at"] <= now]


def next_at(db) -> float | None:
    looks = pending(db)
    return looks[0]["at"] if looks else None


def settle(db, ids: list[str], status: str, run: int | None = None, now: float | None = None) -> None:
    """Drop the looks `ids` from pending; the last 20 settled stay in DONE_KEY for status and tests."""
    if not ids:
        return
    now = time.time() if now is None else now
    with db.tx():
        looks = pending(db)
        done = [{**x, "status": status, "run": run, "settled": now} for x in looks if x["id"] in ids]
        db.set_kv(KEY, [x for x in looks if x["id"] not in ids])
        db.set_kv(DONE_KEY, ((db.kv(DONE_KEY) or []) + done)[-20:])


def ahead(db, cfg: dict, now: float, have: set) -> list[dict]:
    """Pending looks not in `have` due within coordinator.wake_gap_s: the turn starting now reads them."""
    from . import eventhold
    if not trim_on(cfg):
        return []
    try:
        gap = max(float((cfg.get("coordinator") or {}).get("wake_gap_s", eventhold.DEFAULT_S)), 0.0)
    except (TypeError, ValueError):
        gap = eventhold.DEFAULT_S
    return [x for x in pending(db) if x["id"] not in have and x["at"] <= now + gap]


def unchanged_since(db, t: float) -> list[dict]:
    """Looks settled 'unchanged' (the default check) after `t`, for the next turn's digest."""
    return [x for x in db.kv(DONE_KEY) or [] if x.get("status") == "unchanged" and x.get("settled", 0) > t]


def screen(db, base: Path, looks: list[dict], now: float, p=None) -> list[dict]:
    """The due looks that still need a turn: a look whose check exits 0 is settled 'skipped'; with project
    `p` and look_trim on, one whose snapshot state is unchanged is settled 'unchanged'."""
    fire, skipped, same = [], [], []
    on = p is not None and any(x.get("state") for x in looks) and trim_on(p.config())
    for x in looks:
        if on and x.get("state") and not x.get("check"):
            from . import gatejudge
            if fingerprint(db, x["text"], lambda: gatejudge._push_tip(p)[1]) == x["state"]:
                same.append(x["id"])
                continue
        if x.get("check"):
            try:
                cmd, env = probespawn.hidden(x["check"], None)
                rc = subprocess.run(cmd, shell=True, cwd=str(base), env=env, capture_output=True,
                                    timeout=CHECK_TIMEOUT_S).returncode
            except (subprocess.TimeoutExpired, OSError):
                rc = 1   # a check that cannot answer wakes, so a look is never lost
            if rc == 0:
                skipped.append(x["id"])
                continue
        fire.append(x)
    settle(db, skipped, "skipped", now=now)
    settle(db, same, "unchanged", now=now)
    return fire


def digest(looks: list[dict], tz_fmt=None, now: float | None = None, quiet: list[dict] | None = None) -> str:
    fmt = (lambda t: tz_fmt(t)) if tz_fmt else (lambda t: str(int(t)))
    early = lambda x: f", read early with this turn" if now is not None and x["at"] > now else ""
    rows = [f"- {fmt(x['at'])} ({x['by']}{early(x)}): {x['text']}" for x in looks]
    rows += [f"- {fmt(x['at'])} skipped, its tasks, asks and push tip unchanged: {x['text']}" for x in quiet or []]
    return "## Looks you set (due now)\n" + "\n".join(rows) + \
        "\nSet your next look with a `look` action, or none to fall back to the idle wake."


@contextlib.contextmanager
def start_lock(state: Path) -> Iterator[bool]:
    """Non-blocking: yields False while the other path (daemon tick or overseer wake) is starting a turn."""
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(state / START_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)
