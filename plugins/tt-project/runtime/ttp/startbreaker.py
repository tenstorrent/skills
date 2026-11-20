"""The run-start breaker: one cause that fails every worker start (a malformed config section, a
broken install) holds dispatch behind one alert instead of failing each queued task in turn.

Model-free. Its state is the kv KEY, so it survives a daemon restart:
{"fails": [[ts, sig, task], ...] (the last WINDOW_S of failed starts since the last good start), and
while open "open" (when), "sig", "why" (the first error), "tasks" (the tasks it failed), "tried" (the
last retry), "tried_at" (task id -> its last failed start) and "stamp" (the config and runtime it
opened under)}.

A failed start's signature is its exception type and the runtime file:line it was raised at
(daemon._raised_at). The same signature on MIN_TASKS different tasks within WINDOW_S opens the
breaker: no worker starts (a held start is no attempt), except one retry every RETRY_S. The retry
goes to a ready task the cause has not failed yet, else to the failed one tried least recently
(probe_first), so a cause that fails only some tasks cannot hold the healthy ones. It closes once a
start succeeds, the retry fails elsewhere (that line no longer fails it), or the config or runtime
changes. A single task's own failures never open it, and any good start forgets the failures before
it (started)."""
from __future__ import annotations

from typing import Callable

KEY = "run_start_breaker"
WINDOW_S = 600.0
MIN_TASKS = 3
RETRY_S = 180.0
MAX_FAILS = 50
HELD_NOTE = "held: runs cannot start"   # a held task's note (blocked_reason)


def state(db) -> dict:
    st = db.kv(KEY, {}) or {}
    return st if isinstance(st, dict) else {}


def is_open(db) -> bool:
    return bool(state(db).get("open"))


def record(db, sig: str, why: str, task_id: int, now: float, stamp: str) -> tuple[dict | None, str]:
    """Record one failed start. Returns (the breaker's state while open, else None; a log line when it
    opened or closed, else "")."""
    st = state(db)
    fails = [f for f in st.get("fails") or [] if isinstance(f, list) and len(f) == 3 and now - f[0] <= WINDOW_S]
    fails = (fails + [[now, sig, task_id]])[-MAX_FAILS:]
    said = ""
    if st.get("open") and st.get("sig") != sig:
        said = f"run-start breaker closed: the retry failed elsewhere ({sig}), not at {st.get('sig')}"
        st = {}
    st["fails"] = fails
    if st.get("open"):   # the retry failed at its line again: open, with one more task tried
        st["tried"] = now
        st["tasks"] = sorted(set(st.get("tasks") or []) | {task_id})
        st["tried_at"] = {**(st.get("tried_at") or {}), str(task_id): now}
    else:
        tasks = sorted({f[2] for f in fails if f[1] == sig})
        if len(tasks) >= MIN_TASKS:
            st.update(open=now, sig=sig, why=why[:300], tasks=tasks, tried=now, stamp=stamp,
                      tried_at={str(f[2]): f[0] for f in fails if f[1] == sig})
            said = f"run-start breaker opened: {sig} failed the starts of tasks {', '.join(f'#{t}' for t in tasks)}"
    db.set_kv(KEY, st)
    return (st if st.get("open") else None), said


def gate(db, now: float, stamp: Callable[[], str]) -> tuple[str, str]:
    """Whether a start that passed every other gate may go now: ("", log) to start ("" log unless the
    breaker just closed on a changed config or runtime), ("probe", "") for the retry, or ("held", "").
    The caller tries one probe start a tick and close()s on its success; its failure (record) restamps
    "tried". A probe that never reached its start leaves the retry to the next task."""
    st = state(db)
    if not st.get("open"):
        return "", ""
    if st.get("stamp") != stamp():
        close(db)
        return "", f"run-start breaker closed: the config or runtime changed since {st.get('sig')} opened it"
    if now - float(st.get("tried") or 0) >= RETRY_S:
        return "probe", ""
    return "held", ""


def probe_first(db, ready: list, now: float) -> list:
    """`ready` in the order a due retry tries it: tasks the open breaker's cause has not failed first
    (in their own order), then the failed ones, least recently tried first. Unchanged otherwise."""
    st = state(db)
    if not st.get("open") or now - float(st.get("tried") or 0) < RETRY_S:
        return ready
    failed, tried = set(st.get("tasks") or []), st.get("tried_at") or {}
    return sorted(ready, key=lambda t: (t["id"] in failed, float(tried.get(str(t["id"])) or 0)))


def started(db) -> bool:
    """A start succeeded: the breaker closes and its failures are forgotten, so failures on either
    side of a good start never add up. Whether it was open."""
    st = state(db)
    if st:
        close(db)
    return bool(st.get("open"))


def close(db) -> None:
    """Closed, with its failures forgotten: it reopens only on fresh evidence."""
    db.set_kv(KEY, {})


def held_note(st: dict) -> str:
    return f"{HELD_NOTE} ({st.get('sig')}); one start retries every {int(RETRY_S // 60)} min"[:500]


def alert_text(st: dict) -> str:
    return (f"Runs cannot start: {st.get('why') or st.get('sig')} failed the starts of {len(st.get('tasks') or [])} "
            f"tasks within {int(WINDOW_S // 60)} min. No workers start and no attempts are spent; one start retries "
            f"every {int(RETRY_S // 60)} min, and dispatch resumes once one succeeds or the config or runtime changes.")
