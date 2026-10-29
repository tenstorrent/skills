# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A deferred task's start_when gate as status and the web app show it: its last result (exit code
and a short reason), when it last ran and how long it has said not yet. The daemon records each
verdict (probe_deferred); past `waiting.start_gate_warn_s` still false while the daemon ran (a host
down for hours is no staleness) a stuck gate (stuck) is a warning in the feed (state 'status', clears
itself once the gate passes), never a 'needs you' item. A healthy not-yet (exit 1, 75 or 255 of a
gate that can still pass) is no warning however long it lasts: the progress check's probe stall
(once per wait) and the coordinator's defer_max_days cover a gate that never passes. Stuck is a
broken verdict (another exit, a timeout, a probe that cannot start) or a `landed:#<id>` gate whose
producer is abandoned, failed or cancelled, whose change is pushed yet the gate still says not
yet, or that nothing open can land (deadwait.path). A `landed:#<id>` gate whose producer is approved
or in a batch in the push queue names that instead: it is waiting, not stale.

Cadence (every): a start_when probe runs every PROBE_EVERY_S at first; a healthy not-yet of an
arbitrary command (a remote head, an API) backs off with the wait's length, up to MAX_EVERY_S. A
gate on a landing, a time, the task's own job or another local clock never backs off, and a
landing gate reruns on the tick after the push queue records a push (Daemon._start_probe_due).
Model-free."""
from __future__ import annotations

import json

import re

from . import deadwait, landed
from .db import deferral

KEY = "start_gates"   # kv: task id -> {when, rc, reason, at, since, run_s} of its start_when's last verdict
WARN_S = 1800         # default of waiting.start_gate_warn_s
GAP_S = 900           # verdicts come every few minutes; a longer gap was the daemon down: only this much counts
NOT_YET = (1, 75, 255)   # daemon.NOT_YET_RCS: a healthy not-yet
PROBE_EVERY_S = 180      # daemon.PROBE_EVERY_S: a start_when probe's first cadence
MAX_EVERY_S = 600        # the longest a healthy not-yet of an arbitrary command waits between runs
BACKOFF_DIV = 6          # its interval: the wait so far over this, between the two above
# Gates that never back off: a landing, a time, the task's own job, a push, checks, CI, a reply, a fix.
_PROMPT_RE = re.compile(r"^\s*landed:|ttp landed|detach --check|checks --result|push --result|\bttp ci\b|"
                        r"ack-wait|upstream --fixed|^\s*test \"\$\(date")
_DEAD = ("failed", "cancelled")
_STATES = {"review": "in review", "pushing": "being pushed", "done": "done, not on the branch yet"}


def warn_after(cfg: dict) -> float:
    """waiting.start_gate_warn_s; 0 turns the warning off."""
    return float((cfg.get("waiting") or {}).get("start_gate_warn_s", WARN_S) or 0)


def reason(db, when: str, rc, out: str = "") -> str:
    """Why the gate is still false, in a few words: for `landed:#<id>` (or `ttp landed --task <id>`)
    the producer's state, else the probe's last output line, else what its exit code means."""
    refs = landed.task_refs(when)
    if refs and refs[0].isdigit() and (t := db.task(int(refs[0]))):
        if gone := landed.abandoned(db, t["id"]):
            return f"producer #{t['id']} abandoned ({gone[0][:80]})"
        return f"producer #{t['id']} {_STATES.get(t['status'], t['status'])}"
    last = next((ln.strip() for ln in reversed((out or "").splitlines()) if ln.strip()), "")
    if last:
        return last[:120]
    return {1: "not yet", 75: "busy", 255: "host unreachable"}.get(rc, rc if isinstance(rc, str) else f"exit {rc}")


def record(db, tid: int, when: str, rc, why: str, now: float) -> None:
    """Keeps a not-yet or broken verdict; `since` is when this probe first said so, `run_s` how long
    the daemon has run since then (each gap between verdicts, at most GAP_S)."""
    gates = db.kv(KEY, {}) or {}
    prev = gates.get(str(tid)) or {}
    same = prev.get("when") == when and prev.get("since")
    since = prev.get("since") if same else now
    run_s = float(prev.get("run_s") or 0) + _gap(now - float(prev.get("at") or now)) if same else 0.0
    gates[str(tid)] = {"when": when, "rc": rc, "reason": why, "at": now, "since": since, "run_s": run_s}
    db.set_kv(KEY, gates)


def forget(db, keep: set[int] | None = None, tid: int | None = None) -> None:
    """Drops task `tid`'s record, or every record whose task is not in `keep` (no longer deferred)."""
    gates = db.kv(KEY, {}) or {}
    left = {k: v for k, v in gates.items() if (keep is None or int(k) in keep) and k != str(tid)}
    if left != gates:
        db.set_kv(KEY, left)


def view(db, task: dict, now: float, warn_s: float, gates: dict | None = None) -> dict | None:
    """{text, warn, rc, reason, at, since} of a deferred task's gate, or None before its first verdict
    (or when the probe changed since)."""
    when = deferral(task).get("when")
    g = (db.kv(KEY, {}) if gates is None else gates).get(str(task["id"])) if when else None
    if not g or g.get("when") != when:
        return None
    rc = g.get("rc")
    false_s = now - float(g.get("since") or now)
    run_s = float(g.get("run_s") or 0) + _gap(now - float(g.get("at") or now))   # none recorded: from its next verdict
    long = bool(warn_s) and run_s >= warn_s
    text = (f"gate {'not yet' if rc in (1, 75, 255) else 'broken'} "
            f"({rc if isinstance(rc, str) else f'exit {rc}'}: {g.get('reason') or '?'}), "
            f"checked {_ago(now - float(g.get('at') or now))} ago, false for {_ago(false_s)}")
    if false_s - run_s >= GAP_S:
        text += f" ({_ago(run_s)} of it with the daemon running)"
    warn = False
    if long and (queued := push_state(db, when)):
        text = f"{text}; {queued}"
    elif long and (why := stuck(db, when, rc)):
        warn = True
        text += f"; warning: still false after {_ago(warn_s)} of daemon time ({why}), check the gate is not stale"
    return {"text": text, "warn": warn, "rc": rc, "reason": g.get("reason"), "at": g.get("at"),
            "since": g.get("since")}


def stuck(db, when: str, rc) -> str:
    """Why gate `when` with verdict `rc` is stuck, '' when it is a healthy not-yet (module doc)."""
    if rc not in NOT_YET:
        return "broken"
    refs = landed.task_refs(when)
    if not (refs and refs[0].isdigit() and (t := db.task(int(refs[0])))):
        return ""
    x = t["id"]
    if (gone := landed.abandoned(db, x)) or t["status"] in _DEAD:
        return f"producer #{x} {'abandoned' if gone else t['status']}"
    ids = set(landed.carriers(db, x)) | {x}
    if db.one(f"SELECT 1 FROM push_queue WHERE status IN ('pushed','landed') "
              f"AND task IN ({','.join('?' * len(ids))})", tuple(sorted(ids))):
        return f"#{x}'s change is pushed"
    return "" if deadwait.path(db, x, set()) else f"nothing open can land #{x}"


def every(when: str, rc, waited_s: float, base: float = PROBE_EVERY_S) -> float:
    """Seconds between runs of start_when probe `when` whose last verdict is `rc` after `waited_s` of
    saying not yet: `base` (the daemon's PROBE_EVERY_S), backing off to MAX_EVERY_S / PROBE_EVERY_S
    times it for a healthy not-yet of an arbitrary command (module doc). A broken verdict keeps the
    first cadence: a fix to its probe shows soon."""
    if rc not in NOT_YET or _PROMPT_RE.search(when or "") or landed.probe_command(when) != when:
        return base
    return base * min(MAX_EVERY_S / PROBE_EVERY_S, max(1.0, waited_s / BACKOFF_DIV / PROBE_EVERY_S))


def push_state(db, when: str) -> str | None:
    """'waiting on push batch <id>' (or on the next batch) while a producer the gate waits on, or a
    task carrying its change (its review), has an approved or batched push queue row; else None."""
    ids = set()
    for ref in landed.task_refs(when):
        if ref.isdigit():
            ids |= set(landed.carriers(db, int(ref)))
    if not ids:
        return None
    rows = db.q(f"SELECT status, batch, detail FROM push_queue WHERE status IN ('approved','batched','held') "
                f"AND task IN ({','.join('?' * len(ids))}) ORDER BY id", tuple(sorted(ids)))
    held = next((r for r in rows if r["status"] == "held"), None)
    if held:
        try:
            scope = (json.loads(held.get("detail") or "{}") or {}).get("scope")
        except (TypeError, ValueError, AttributeError):
            scope = None
        return f"held for the token scope {scope}" if scope else "held for a missing token scope"
    batch = next((r["batch"] for r in rows if r["status"] == "batched" and r["batch"]), None)
    return f"waiting on push batch {batch}" if batch else "approved, waiting on the next push batch" if rows else None


def feed(db, warn_s: float, now: float) -> list[dict]:
    """Feed rows (state 'status': not the top section) for each gate false past `warn_s`."""
    gates = db.kv(KEY, {}) or {}
    if not warn_s or not gates:
        return []
    out = []
    for t in db.q("SELECT * FROM tasks WHERE status='queued' AND labels LIKE '%\"start_when:%'"):
        v = view(db, t, now, warn_s, gates)
        if v and v["warn"]:
            out.append({"id": None, "ts": v["since"], "kind": "gate", "severity": "low", "state": "status",
                        "text": f"#{t['id']} {t['title']} waits to start: {v['text']}"})
    return out


def _gap(s: float) -> float:
    return min(max(0.0, s), GAP_S)


def _ago(s: float) -> str:
    s = max(0.0, s)
    return f"{s / 3600:.1f} h" if s >= 5400 else f"{int(s // 60)} min"
