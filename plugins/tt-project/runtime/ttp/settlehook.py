# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The project's own settle hook: `coordinator.settle_cmd` settles routine event batches without a model.

Empty (off) by default; the project's coordinator sets it with config_set. Before a coordinator turn
for a batch of only routine events (ROUTINE_KINDS: task_done, upstream_note, observation; ride-alongs
aside), the daemon runs the command from the project root, off its tick, for at most TIMEOUT_S, with
no model. Its stdin is a JSON list of the candidate events, each {"event_id", "kind", "severity",
"text"}. It prints a JSON list of {"event_id": <id>, "settle": true, "reason": "<why>"}; an event it
leaves out, or gives `settle` false, wakes as before. A batch with any other event starts its turn as
it is: the turn runs anyway, so the hook is not run.

The runtime never settles, whatever the hook says (never_reason): user messages (a turn with one
never runs the hook), any event of another kind (Must-act-now items, obligation and promise events,
task failures, outages and machine conditions have kinds of their own), a must_act item, high or
critical severity, a review's result (a review task's task_done, or a verdict), text naming a
failure, a Restriction, a promise or obligation, an outage, a machine or host condition, a task that
owns an open obligation, a user source, and the hook's own fault observations.

A hook error, a timeout, output that is not that JSON list, or a non-zero exit settles nothing: the
batch wakes as it would have, and one low observation (key settle-hook) is raised, deduplicated.
Each settled event is marked handled and logged with the hook's reason (LOG_KEY), shown in the web
feed and counted in the daily review's self-efficiency audit. Only that event id is settled: a later
event, the same task's follow-up included, is a new event and wakes on its own."""
from __future__ import annotations

import json
import re
import threading
import time

from . import boundedchild

ROUTINE_KINDS = ("task_done", "upstream_note", "observation")
SEVERITIES = ("info", "low", "normal")
TIMEOUT_S = 20.0
KILL_WAIT_S = 5.0
MAX_CMD = 2000
MAX_OUT = 256 * 1024
MAX_REASON = 300
LOG_KEY = "settle_hook_log"     # kv: [{ts, event, kind, task, reason}] settled events
FAULTS_KEY = "settle_hook_faults"   # kv: [{ts, why}] hook runs that settled nothing on a fault
KEEP_S = 8 * 86400
FAULT_SOURCE = "daemon:settle-hook"
FAULT_KEY = "settle-hook"
TRIED_MAX = 2000   # event ids the hook already met, kept so a fail-open batch does not rerun it each tick

_FAIL = re.compile(r"\bfail(?:s|ed|ure|ures|ing)?\b|\berror(?:s|ed)?\b|\bcrash(?:ed|es)?\b", re.I)
_RESTRICT = re.compile(r"\brestrict", re.I)
_PROMISE = re.compile(r"\bpromis(?:e|es|ed)\b|\bobligation|\bmust[- ]act", re.I)
_REVIEW = re.compile(r"\bverdict\b|\breview (?:result|passed|failed|finding)", re.I)
_OUTAGE = re.compile(r"\boutage|\boffline\b|\bunreachable\b|\bunhealthy\b|\bis down\b|\bwent down\b|\breboot", re.I)
_MACHINE_SRC = re.compile(r"machine|host|outage|health|broker|device|disk|ssh|reboot|net", re.I)
_MACHINE = re.compile(r"\b(?:machine|host|node|device|broker|disk)s?\b", re.I)


def cmd_of(v) -> str:
    """config_set's validator: one command line, at most MAX_CMD chars; "" turns the hook off."""
    v = "" if v is None or v is False else v
    if not isinstance(v, str) or "\n" in v or "\r" in v or "\0" in v or len(v) > MAX_CMD:
        raise ValueError(f"coordinator.settle_cmd: give one command line (up to {MAX_CMD} chars), or \"\" for off")
    return v.strip()


def command(cfg: dict) -> str:
    try:
        return cmd_of((cfg.get("coordinator") or {}).get("settle_cmd") or "")
    except ValueError:
        return ""


def never_reason(e: dict, task: dict | None, owners) -> str | None:
    """Why the runtime keeps event `e` (with its text) from being settled, or None."""
    text = str(e.get("text") or "")
    source = str(e.get("source") or "")
    if e.get("kind") not in ROUTINE_KINDS:
        return "not a routine kind"
    if e.get("severity") not in SEVERITIES:
        return "high or critical severity"
    if source == FAULT_SOURCE:
        return "the hook's own fault"
    if source.startswith(("user", "chat")):
        return "a user message"
    try:
        if (json.loads(e.get("data") or "{}") or {}).get("must_act"):
            return "a Must-act-now item"
    except (TypeError, ValueError, AttributeError):
        pass
    if _PROMISE.search(text):
        return "a promise or obligation"
    if e.get("task") is not None and int(e["task"]) in owners:
        return "its task owns an open obligation"
    if (task or {}).get("kind") == "review" or _REVIEW.search(text):
        return "a review result"
    if _FAIL.search(text):
        return "a failure"
    if _RESTRICT.search(text):
        return "names a Restriction"
    if _OUTAGE.search(text) or (e.get("kind") == "observation" and (_MACHINE_SRC.search(source) or _MACHINE.search(text))):
        return "an outage or machine condition"
    return None


def candidates(db, evs: list[dict], tried=frozenset()) -> list[dict] | None:
    """The batch's events for the hook, with their text, or None when the batch does not go to it: an
    event that is not a ride-along and may not be settled starts the turn as it is. `tried`: ids the
    hook already met."""
    from . import obligations, pagesev
    own = [e for e in evs if not pagesev.rides_along(e)]
    if not own or any(e["id"] in tried for e in own):
        return None
    ids = [e["id"] for e in own]
    rows = {r["id"]: r for r in db.q(f"SELECT id, kind, severity, source, text, data, task FROM events "
                                     f"WHERE id IN ({','.join('?' * len(ids))})", ids)}
    try:
        owners = set(obligations.owner_due(db))
    except Exception:   # owners unknown: nothing is settled
        return None
    out = []
    for i in ids:
        e = rows.get(i)
        if e is None or never_reason(e, db.task(e["task"]) if e.get("task") is not None else None, owners):
            return None
        out.append(e)
    return out


def payload(cands: list[dict]) -> bytes:
    return json.dumps([{"event_id": e["id"], "kind": e["kind"], "severity": e["severity"],
                        "text": str(e.get("text") or "")} for e in cands]).encode()


def parse(out: bytes, ids) -> dict[int, str]:
    """{event id: reason} for each entry settling one of `ids`; ValueError when the output is not the
    JSON list of {event_id, settle, reason}."""
    data = json.loads(out.decode("utf-8") or "null")
    if not isinstance(data, list):
        raise ValueError("not a JSON list")
    got: dict[int, str] = {}
    for d in data:
        if not isinstance(d, dict) or isinstance(d.get("event_id"), bool) or not isinstance(d.get("event_id"), int):
            raise ValueError("an entry is not {event_id, settle, reason}")
        if d.get("settle") is True and d["event_id"] in ids:
            reason = d.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"event {d['event_id']}: settle without a reason")
            got[d["event_id"]] = " ".join(reason.split())[:MAX_REASON]
    return got


class Run:
    """One hook run on a thread: the daemon's tick only checks `done`."""

    def __init__(self, cmd: str, cwd: str, cands: list[dict], timeout: float | None = None):
        self.ids = frozenset(e["id"] for e in cands)
        self.cands = cands
        self.started = time.time()
        self.settle: dict[int, str] = {}
        self.fault: str | None = None
        self._cmd, self._cwd, self._timeout = cmd, cwd, TIMEOUT_S if timeout is None else timeout
        self._stdin = payload(cands)
        self._done = threading.Event()
        threading.Thread(target=self._go, name="settle-hook", daemon=True).start()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def _go(self) -> None:
        try:
            res = boundedchild.run(self._cmd, self._timeout, shell=True, cwd=self._cwd, input=self._stdin,
                                   text=False, grace=KILL_WAIT_S)
            if res.timed_out:
                self.fault = f"timed out after {self._timeout:g} s"
                return
            if res.rc is None:
                self.fault = f"could not run: {res.error}"[:300]
                return
            if res.rc != 0:
                tail = " ".join(res.err.decode("utf-8", "replace").split())[-200:]
                self.fault = f"exited {res.rc}" + (f": {tail}" if tail else "")
                return
            out = res.out
            if len(out) > MAX_OUT:
                self.fault = f"printed over {MAX_OUT} bytes"
                return
            try:
                self.settle = parse(out, self.ids)
            except (ValueError, UnicodeDecodeError) as e:
                self.fault = f"bad JSON: {e}"[:300]
        except Exception as e:
            self.fault = f"could not run: {type(e).__name__}: {e}"[:300]
        finally:
            self._done.set()


def _keep(rows, now: float) -> list:
    return [r for r in (rows or []) if isinstance(r, dict) and now - float(r.get("ts") or 0) < KEEP_S]


def apply(db, run: Run, now: float) -> list[int]:
    """Mark the hook's settled events handled (rechecked against the never-settle list, as they are
    now) and log each with its reason; returns the settled ids."""
    if not run.settle:
        return []
    ids = list(run.settle)
    try:
        from . import obligations
        owners = set(obligations.owner_due(db))
    except Exception:
        return []
    rows = db.q(f"SELECT id, kind, severity, source, text, data, task, status FROM events "
                f"WHERE id IN ({','.join('?' * len(ids))})", ids)
    ok = [r for r in rows if r["status"] == "queued"
          and not never_reason(r, db.task(r["task"]) if r.get("task") is not None else None, owners)]
    if not ok:
        return []
    done = [r["id"] for r in ok]
    db.x(f"UPDATE events SET status='handled' WHERE id IN ({','.join('?' * len(done))})", done)
    db.set_kv(LOG_KEY, _keep(db.kv(LOG_KEY), now) + [
        {"ts": now, "event": r["id"], "kind": r["kind"], "task": r["task"], "reason": run.settle[r["id"]]}
        for r in ok])
    return done


def fault(db, why: str, now: float) -> None:
    db.set_kv(FAULTS_KEY, _keep(db.kv(FAULTS_KEY), now) + [{"ts": now, "why": why}])


def fault_text(why: str) -> str:
    return (f"coordinator.settle_cmd {why}; it settled nothing and the batch woke the coordinator as usual. "
            f"Fix the project's settle hook or set coordinator.settle_cmd to \"\".")


def summary(db, since: float, until: float | None = None) -> dict:
    """Events the hook settled in [since, until), by reason, and its faults."""
    def win(rows):
        return [r for r in _keep(rows, time.time()) if float(r.get("ts") or 0) >= since
                and (until is None or float(r.get("ts") or 0) < until)]
    rows = win(db.kv(LOG_KEY))
    reasons: dict[str, int] = {}
    for r in rows:
        reasons[str(r.get("reason") or "")] = reasons.get(str(r.get("reason") or ""), 0) + 1
    return {"n": len(rows), "kinds": sorted({str(r.get("kind")) for r in rows}),
            "reasons": sorted(reasons.items(), key=lambda kv: -kv[1]), "faults": len(win(db.kv(FAULTS_KEY)))}


def audit_line(db, since: float) -> str:
    """The self-efficiency audit's line; '' when the hook settled nothing and had no fault."""
    s = summary(db, since)
    if not s["n"] and not s["faults"]:
        return ""
    top = "; ".join(f"{n}x {r[:80]}" for r, n in s["reasons"][:3])
    return (f"events the project's settle hook settled without a turn (coordinator.settle_cmd): {s['n']}"
            + (f" ({', '.join(s['kinds'])}), top reasons: {top}" if s["n"] else "")
            + (f"; {s['faults']} hook run(s) settled nothing on a fault" if s["faults"] else ""))


def feed(db, now: float, limit: int = 5) -> list[dict]:
    """The web feed's low status rows: the latest settled events with the hook's reason."""
    rows = [r for r in _keep(db.kv(LOG_KEY), now) if now - float(r.get("ts") or 0) < 86400][-limit:]
    return [{"id": None, "ts": r["ts"], "kind": "settle", "severity": "low", "state": "status",
             "text": f"settle hook settled event {r.get('event')} ({r.get('kind')}"
                     + (f", #{r['task']}" if r.get("task") else "") + f") without a turn: {r.get('reason')}"}
            for r in reversed(rows)]
