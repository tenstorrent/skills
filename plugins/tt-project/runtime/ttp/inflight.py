# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A host-wide cap on chat-provider calls in flight (account setting chat.max_inflight).

A chat endpoint may be shared with other users, so the calls every project and run on this host
send to it at once (workers' bridges, benches, probes, babysat runs) are counted together: each
call holds one slot while it is in flight, from the request until its answer (a stream's last line)
is read. Unset or 0 (the shipped default): no cap, and acquire() returns at once.

Slots are files `slot.<i>` under the account's locks/chat-inflight folder, each held by an flock on
its own open file, so a process that dies frees its slot with it (the kernel drops the lock); a
call that finds no free slot waits, polling with jitter, and its wait is counted (Waits).

Refusals: a capacity or rate-limit answer (HTTP 429, 503, 529) halves the host's allowed level at
once (floor 1, refused()); it then comes back by one per quiet minute (RESTORE_S), never above the
cap. The level lives in level.json beside the slots, written under level.lock. Calls already in
flight when the level drops finish; new ones take only slots below the level.

Priority: three classes, normal > mid > low, from the process's TTP_CHAT_PRIORITY (`low` or `mid`;
unset or anything else is normal; `ttp bench`, `ttp model-bench` and the tool probe set low, and
children inherit it). A call takes no free slot while a call of a higher class is waiting for one.
A normal or mid call that waits marks itself with a `wait.*` or `wait-mid.*` file it holds an flock
on (gone with the process, like a slot); a lower call only orders itself behind those, never ahead.

Call outcomes: every finished call may be appended to calls.jsonl beside the slots (record(), one
O_APPEND line each: time, outcome ok, refusal (REFUSALS) or error, class, and the project when
known), under a shared flock on calls.lock that pruning takes exclusively, so no append is lost
to a prune. Lines older than KEEP_S go at most once per PRUNE_EVERY_S. load() counts the trailing
minutes' calls and refusals in total, by class and by project (`ttp chat-load`).
"""
from __future__ import annotations

import fcntl
import json
import os
import random
import time
from pathlib import Path

KEY = "chat.max_inflight"   # account setting: the cap (a whole number; unset or 0: no cap)
DIR = "chat-inflight"   # under the account home's locks/
RESTORE_S = 60.0   # one more call allowed per this many quiet seconds after a refusal
POLL_S = (0.2, 1.0)   # a waiting call's next try, a random wait in this range
REFUSALS = (429, 503, 529)   # answers that lower the level
PRIORITY_ENV = "TTP_CHAT_PRIORITY"   # "low" or "mid": wait while a higher class waits (benches, probes, A/B runs)
CLASSES = ("low", "mid", "normal")   # lowest first
MARKS = {"normal": "wait.", "mid": "wait-mid."}   # a waiting call's mark file prefix, by class
CALLS, CALLS_LOCK, PRUNED = "calls.jsonl", "calls.lock", "calls.pruned"
KEEP_S, PRUNE_EVERY_S = 2 * 3600.0, 600.0   # outcomes kept this long; pruned at most this often
OUTCOMES = ("ok", "refusal", "error")


def priority(environ=None) -> str:
    """The call class from TTP_CHAT_PRIORITY: "low", "mid", else "normal"."""
    v = str((os.environ if environ is None else environ).get(PRIORITY_ENV) or "").strip().lower()
    return v if v in ("low", "mid") else "normal"


def low_priority(environ=None) -> bool:
    return priority(environ) == "low"


def cap() -> int:
    """The host's cap from the account settings, read fresh; 0 for none."""
    from .project import load_account_settings
    try:
        v = (load_account_settings().get("chat") or {}).get("max_inflight")
    except (OSError, ValueError):
        return 0
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0


def folder() -> Path:
    from .project import HOME_DIR
    return HOME_DIR / "locks" / DIR


def _read_level() -> dict:
    try:
        got = json.loads((folder() / "level.json").read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def allowed(now: float | None = None, limit: int | None = None) -> int:
    """How many calls may be in flight now: the cap, or the level a refusal lowered it to plus one per
    quiet RESTORE_S since; 0 when there is no cap."""
    c = cap() if limit is None else limit
    if c <= 0:
        return 0
    lv = _read_level()
    try:
        level, at = int(lv["level"]), float(lv["at"])
    except (KeyError, TypeError, ValueError):
        return c
    now = time.time() if now is None else now
    return max(1, min(c, level + int(max(0.0, now - at) // RESTORE_S)))


def refused(now: float | None = None) -> int:
    """Record a refusal: halve the allowed level at once (floor 1). Returns the new level (0: no cap)."""
    c = cap()
    if c <= 0:
        return 0
    d = folder()
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / "level.lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return 0
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        now = time.time() if now is None else now
        level = max(1, allowed(now, c) // 2)
        from .project import durable_write
        # A lost level after a power cut only restarts the backoff at the full cap: no sync needed.
        durable_write(d / "level.json", json.dumps({"level": level, "at": round(now, 3)}), sync=False)
        return level
    except OSError:
        return 0
    finally:
        os.close(fd)


class Slot:
    """One held slot (index -1, fd None: no cap); release() frees it, at most once."""

    def __init__(self, fd: int | None = None, index: int = -1, waited_s: float = 0.0):
        self.fd, self.index, self.waited_s = fd, index, waited_s

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                os.close(fd)   # closing the file drops its flock
            except OSError:
                pass

    def __enter__(self) -> "Slot":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _try(d: Path, n: int) -> tuple[int, int] | None:
    for i in range(n):
        try:
            fd = os.open(d / f"slot.{i}", os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd, i
        except OSError:
            os.close(fd)
    return None


def _live(f: Path) -> bool:
    """True while some process holds `f`'s flock; a file no one holds is removed (its waiter died)."""
    try:
        fd = os.open(f, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        try:
            f.unlink()
        except OSError:
            pass
        return False
    finally:
        os.close(fd)


def waiting(cls: str, d: Path | None = None) -> bool:
    """Whether a call of class `cls` (normal or mid) is waiting for a slot now."""
    d = folder() if d is None else d
    return any(_live(f) for f in d.glob(MARKS[cls] + "*")) if d.is_dir() else False


def higher_waiting(cls: str, d: Path | None = None) -> bool:
    """Whether a call of a class above `cls` is waiting for a slot now."""
    return any(waiting(c, d) for c in CLASSES[CLASSES.index(cls) + 1:])


def _mark_waiting(d: Path, cls: str = "normal") -> tuple[int, Path] | None:
    f = d / f"{MARKS[cls]}{os.getpid()}.{random.getrandbits(32):08x}"
    try:
        fd = os.open(f, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fd, f


def _unmark(mark: tuple[int, Path] | None) -> None:
    if mark:
        try:
            mark[1].unlink()
        except OSError:
            pass
        os.close(mark[0])


def acquire(timeout_s: float, sleep=time.sleep, clock=time.monotonic, low: bool | None = None,
            cls: str | None = None) -> Slot | None:
    """A slot for one call, waiting up to `timeout_s`; None when none came free in time. No cap: a
    Slot that holds nothing, at once. The cap and level are read again on every try. `cls` (default:
    TTP_CHAT_PRIORITY; `low` True or False is "low" or "normal") takes no slot while a call of a
    higher class waits."""
    if cls not in CLASSES:
        cls = priority() if low is None else "low" if low else "normal"
    start, mark = clock(), None
    try:
        while True:
            n = allowed()
            if n <= 0:
                return Slot()
            d = folder()
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                return Slot()   # the account home cannot be written: no cap can hold, never stop the call
            got = None if higher_waiting(cls, d) else _try(d, n)
            if got:
                return Slot(got[0], got[1], round(clock() - start, 3))
            if clock() - start >= timeout_s:
                return None
            if cls in MARKS and mark is None:
                mark = _mark_waiting(d, cls)
            sleep(random.uniform(*POLL_S))
    finally:
        _unmark(mark)


def held() -> int:
    """How many slots are held now (a read: each free one is locked and let go at once)."""
    d = folder()
    n = 0
    for f in sorted(d.glob("slot.*")) if d.is_dir() else ():
        try:
            fd = os.open(f, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            n += 1
        finally:
            os.close(fd)
    return n


class Waits:
    """One run's slot waits, for its exit record: calls, waits (calls that waited), total, p90, and
    calls that found no slot in time (timeouts) and refusals that lowered the level."""

    def __init__(self):
        self.calls, self.timeouts, self.refusals, self._w = 0, 0, 0, []

    def add(self, waited_s: float) -> None:
        self.calls += 1
        self._w.append(float(waited_s))

    def export(self) -> dict:
        w = sorted(self._w)
        waited = [x for x in w if x > 0.0]
        p90 = w[min(len(w) - 1, int(0.9 * len(w)))] if w else 0.0
        return {"calls": self.calls, "waits": len(waited), "waited_s": round(sum(waited), 1),
                "p90_s": round(p90, 2), "timeouts": self.timeouts, "refusals": self.refusals}

    def __bool__(self) -> bool:
        return bool(self.calls or self.timeouts or self.refusals)


def outcome(status: int | None) -> str:
    """A finished call's outcome from its HTTP status: refusal (REFUSALS), error (another 4xx or 5xx,
    or 0 for no answer), else ok."""
    if status in REFUSALS:
        return "refusal"
    return "error" if status is not None and (status == 0 or status >= 400) else "ok"


def record(result: str, cls: str | None = None, project: str | None = None, now: float | None = None) -> bool:
    """Append one finished call's outcome (ok, refusal or error) to the host's record; False when it
    could not be written (never raises). Callers that reach the endpoint themselves (bench runners)
    record theirs with record_call()."""
    if result not in OUTCOMES:
        return False
    now = time.time() if now is None else now
    row = {"ts": round(now, 3), "outcome": result, "class": cls if cls in CLASSES else priority()}
    project = project if project is not None else os.environ.get("TTP_PROJECT")
    if project:
        row["project"] = str(project)
    d = folder()
    try:
        d.mkdir(parents=True, exist_ok=True)
        lk = os.open(d / CALLS_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(lk, fcntl.LOCK_SH)
        fd = os.open(d / CALLS, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(row, separators=(",", ":")) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        return False
    finally:
        os.close(lk)
    try:
        due = now - (d / PRUNED).stat().st_mtime >= PRUNE_EVERY_S
    except OSError:
        due = True
    if due:
        prune(now, wait=False)
    return True


def record_call(status: int | None, project: str | None = None, cls: str | None = None,
                now: float | None = None) -> bool:
    """record() from a call's HTTP status (None or 2xx: ok)."""
    return record(outcome(status), cls, project, now)


def prune(now: float | None = None, wait: bool = True) -> int:
    """Drop outcome lines older than KEEP_S, holding calls.lock exclusively (`wait` False: skip when
    another process holds it). Returns the lines kept, -1 when skipped."""
    now = time.time() if now is None else now
    d = folder()
    try:
        lk = os.open(d / CALLS_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return -1
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        keep = [r for r in _rows(d) if now - r["ts"] <= KEEP_S]
        from .project import durable_write
        # A lost prune only keeps old lines a while longer: no sync needed.
        durable_write(d / CALLS, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in keep), sync=False)
        (d / PRUNED).touch()
        os.utime(d / PRUNED, (now, now))
        return len(keep)
    except OSError:
        return -1
    finally:
        os.close(lk)


def _rows(d: Path) -> list[dict]:
    try:
        text = (d / CALLS).read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            r = json.loads(line)
            r["ts"] = float(r["ts"])
        except (ValueError, TypeError, KeyError):
            continue   # a torn or foreign line
        if isinstance(r, dict) and r.get("outcome") in OUTCOMES:
            out.append(r)
    return out


def _tally(rows: list[dict]) -> dict:
    calls, refusals = len(rows), sum(r["outcome"] == "refusal" for r in rows)
    return {"calls": calls, "refusals": refusals, "errors": sum(r["outcome"] == "error" for r in rows),
            "refusal_pct": round(100.0 * refusals / calls, 2) if calls else 0.0}


def load(minutes: float = 60.0, now: float | None = None) -> dict:
    """The trailing `minutes`' finished calls on this host: calls, refusals, errors and refusal_pct in
    total, `by_class` and `by_project` (calls with no project under "")."""
    now = time.time() if now is None else now
    rows = [r for r in _rows(folder()) if 0.0 <= now - r["ts"] <= minutes * 60.0]
    by_class = {c: _tally([r for r in rows if r.get("class") == c]) for c in CLASSES}
    projects = sorted({str(r.get("project") or "") for r in rows})
    by_project = {p: _tally([r for r in rows if str(r.get("project") or "") == p]) for p in projects}
    return {"minutes": minutes, "since": round(now - minutes * 60.0, 3), **_tally(rows),
            "by_class": by_class, "by_project": by_project}
