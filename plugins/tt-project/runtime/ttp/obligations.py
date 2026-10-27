# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Urgent user obligations: a deliverable the user asked for stays owed until a receipt exists.

An obligation (`ttp obligation add "<what>" [--owner <task>]...`) is a deliverable the user is owed,
attached to the tasks meant to produce it, its required owners (`ttp obligation attach <id> <task>...`).
Attaching tells each owner: its spec gets a section naming the obligation and how to report on it, and
a running owner hears it at once through its steer.md. Attaching again changes nothing.

Each required owner has its own durable-progress clock (CLOCK_S, two hours). It starts when the task is
attached and is reset only by that owner recording durable progress with evidence (`ttp obligation
progress <id> "<commit, file or URL>"`). Nothing else resets it: task edits, token activity, the task
running or waiting, a re-attach. An owner waiting on a `ttp detach` job of its own run is kept on time
(not reset) while that job still runs and wrote a `ttp progress report result` line within the clock,
or a new file matching the results glob it declared (`ttp detach --results <glob>`) (_job_results): a
pid or a growing log alone never counts. An overdue owner with such a live job that reported nothing
is named with its job and both ways to report (anomaly_text). A receipt (`ttp obligation receipt <id>
"<evidence>"`) ends the obligation. An owner task that ends (done, failed, cancelled) without one leaves it owed and its clock
running. The harness records the evidence as given: checking it stays with the owner.

Inside a run the CLI cannot write the project's database, so `progress` and `receipt` append to the
run's RUN_FILE, and the daemon files them (only for an obligation that run's task owns). Outside a run
(the user, the relay) they are filed at once.

Daemon.check_obligations looks every CHECK_EVERY_S, model-free: it files new run records, and for each
open obligation with an owner past its clock (or no owner at all, past the clock from its creation)
raises one high observation keyed `obligation:<id>:<episode>` for the coordinator. It is not raised
again on a timer while the same owners stay stalled; an owner not yet covered that stalls (say one
attached after an earlier owner ended) opens the next episode, and once no owner is late the episode ends.
Milestones have stable keys, absolute due times and evidence_required. The coordinator adds them on
its obligation action; an owner records a checked milestone with `ttp obligation receipt <id>
"<evidence>" --milestone <key>`, leaving the obligation open. A missing milestone receipt at its due
time raises one durable high episode, even with fresh owner progress or a healthy heartbeat. A milestone
episode that ends (its receipt, the obligation's receipt, a drop) retires its still-queued event, so the
coordinator is not woken for it. Status and the web app show the next deadline in the project's home time zone.
Known gap: nothing checks whether an owner can advance the obligation, so one whose owners all wait or
stall is caught only by the 2 h progress rule (or a milestone falling due). A daemon-side coverage check
was dropped: it took ordinary waits, backoffs and review stacks for stuck owners and woke the coordinator.

A mistaken or no longer wanted obligation is withdrawn with `ttp obligation drop <id> "<why>"`.

Promises: a coordinator `reply` or `notify` that commits to a deliverable or a time ("I'll report the
sha by 15:00", "ETA 18:30") is recorded as an obligation with a `promise` field (record_promises, called
when the turn's actions are applied): the chat, the message, the promised sentences, the due time (the
first time named, in the project's home zone; else coordinator.promise_default_h, PROMISE_DEFAULT_H,
after the reply) and the tasks of the same batch that will reply to that chat (task_add or task_update
with that `reply_chat`). It is always recorded; an obligation the same batch records or attaches is
linked to it only when one of its owners is such a task, and once every linked obligation has its
receipt or is dropped, the promise closes too. The same promise sent again to the same chat updates the open record. A
promise has no owner clocks: it is due once, at its due time, when check() raises one high
`promise_overdue` event (`obligation:<id>:promise:<due>`). It closes like any obligation (receipt or drop,
the coordinator's `obligation` action), with its linked obligations, on a blocker sweep outcome `closed` or `retired`, or by itself
once all its tasks finished and a reply went to its chat after that. Detection is conservative
(first-person future plus a delivery verb, or plus a time; never a question or an offer): a commitment
worded otherwise is not caught, and the coordinator prompt asks for a task or obligation to back it.

Delivery: a coordinator `reply` whose `delivers` names an obligation (`o1`) or an owner task (`#12`, every
open obligation it owns) marks it delivered by that reply (deliver). It closes once every owner task finished
as well, at once or on the check() after the last one finishes. A promise is delivered only by a reply to its
own chat. The link is explicit: no reply text is matched.

Specs naming one (spec_check): a coordinator task_add or task_update spec that names an obligation key
(`o6` on a line that says "obligation") must name one that exists. A new task is attached as an owner of
each open one it names; an updated task must already own it, or an `obligation` action of the same batch
attach it, else the action is refused, so no worker is told it owns an obligation its records refuse.
Keys of closed obligations (receipt or dropped) are history and pass.
"""
from __future__ import annotations

import itertools
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path

KV = "obligations"            # kv: {"next": n, "items": {id: record}, "runs": {run id: lines filed}}
RUN_FILE = "obligations.jsonl"
CLOCK_S = 2 * 3600
CHECK_EVERY_S = 300
RUN_FILES_DAYS = 7            # run records are looked for in runs started this recently (or still running)
RUN_IDS_PER_QUERY = 500       # kept run offsets checked per query (SQLite caps the variables in one)
CLOSED = ("done", "failed", "cancelled")
TEXT_MAX = 500
EVIDENCE_MAX = 300
EVIDENCE_KEPT = 10
PROMISE_DEFAULT_H = 6.0       # coordinator.promise_default_h: when a promise naming no time is due
PROMISE_KIND = "promise_overdue"
PROMISE_TEXT_MAX = 300
PROMISE_FINISH_SLACK_S = 60   # a reply this close before an owner's last update still counts as after it


SPEC_KEY = re.compile(r"(?<![\w./-])o(\d+)(?![\w-]|\.\d)")


def named(text: str) -> list[str]:
    """The obligation keys a spec names: `o<N>` on a line that says "obligation", so a model name such as
    o3 in other text is not read as one. In order, without repeats."""
    keys: list[str] = []
    for line in (text or "").splitlines():
        if "obligation" in line.lower():
            for m in SPEC_KEY.finditer(line):
                if f"o{int(m.group(1))}" not in keys:
                    keys.append(f"o{int(m.group(1))}")
    return keys


def spec_check(db, text: str, tid: int | None = None, attaching=()) -> list[str]:
    """For a coordinator task_add (`tid` None) or task_update spec: the open obligations it names that a new
    task is to be attached to. Raises ValueError for a key that does not exist, or one an updated task does
    not own and no `(key, tid)` in `attaching` (the same batch's obligation actions) attaches."""
    st, attach, unknown, unowned = _load(db), [], [], []
    for oid in named(text):
        o = st["items"].get(oid)
        if not o:
            unknown.append(oid)
        elif o["receipt"] or o.get("dropped"):
            continue
        elif tid is None:
            attach.append(oid)
        elif str(tid) not in o["owners"] and (oid, int(tid)) not in attaching:
            unowned.append(oid)
    if unknown:
        raise ValueError(f"its spec names obligation {', '.join(unknown)}, which does not exist: record it first "
                         f"(an `obligation` action with `text`, then name the key it gets), or drop the key")
    if unowned:
        raise ValueError(f"its spec names obligation {', '.join(unowned)}, which #{tid} does not own: attach it in "
                         f"the same batch (an `obligation` action with `key` and `owners`: [{tid}]), or drop the key")
    return attach


def _load(db) -> dict:
    st = db.kv(KV) or {}
    st.setdefault("next", 1)
    st.setdefault("items", {})
    st.setdefault("runs", {})
    return st


def items(db) -> list[dict]:
    return sorted(_load(db)["items"].values(), key=lambda o: int(o["id"][1:]))


def get(db, oid: str) -> dict | None:
    return _load(db)["items"].get(oid)


def _need(st: dict, oid: str) -> dict:
    o = st["items"].get(oid)
    if not o:
        raise ValueError(f"no obligation {oid}; `ttp obligation list` shows them")
    return o


def add(p, text: str, owners: list[int] = (), by: str = "user", now: float | None = None,
        milestones: list[dict] = ()) -> str:
    text = " ".join(text.split())[:TEXT_MAX]
    if not text:
        raise ValueError("an obligation needs a text: what is owed")
    now = time.time() if now is None else now
    with p.db.tx():   # one transaction with its owners: a refused owner leaves no obligation behind
        st = _load(p.db)
        oid = f"o{st['next']}"
        st["next"] += 1
        o = st["items"][oid] = {"id": oid, "text": text, "by": by, "created": now, "owners": {},
                                "receipt": None, "dropped": None, "episode": 0, "raised": None, "covered": []}
        _milestones(o, milestones)
        _attach(p, o, owners, now)
        p.db.set_kv(KV, st)
    return oid


def _due(value) -> float:
    """An absolute epoch or ISO timestamp with an explicit offset; never an ambiguous local time."""
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("missing offset")
            value = dt.timestamp()
        except ValueError as e:
            raise ValueError("milestone due needs an absolute timestamp with a time zone offset") from e
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("milestone due needs a finite positive absolute timestamp")
    try:
        datetime.fromtimestamp(value)
    except (ValueError, OverflowError, OSError) as e:
        raise ValueError("milestone due is outside the supported timestamp range") from e
    return float(value)


def _milestones(o: dict, milestones) -> None:
    if not isinstance(milestones, (list, tuple)):
        raise ValueError("milestones needs a list")
    saved = o.setdefault("milestones", {})
    for m in milestones:
        if not isinstance(m, dict):
            raise ValueError("each milestone needs key, due and evidence_required")
        key = str(m.get("key") or "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key):
            raise ValueError("milestone key needs 1-64 letters, numbers, underscores or hyphens")
        required = " ".join(str(m.get("evidence_required") or "").split())[:EVIDENCE_MAX]
        if not required:
            raise ValueError("milestone needs evidence_required")
        due = _due(m.get("due"))
        if key in saved:
            if (saved[key]["due"], saved[key]["evidence_required"]) != (due, required):
                raise ValueError(f"milestone {key} already exists with a different due time or required evidence")
            continue
        saved[key] = {"key": key, "due": due, "evidence_required": required, "receipt": None,
                      "episode": 0, "raised": None}


def add_milestones(p, oid: str, milestones: list[dict]) -> None:
    with p.db.tx():
        st = _load(p.db)
        o = _need(st, oid)
        if o["receipt"] or o.get("dropped"):
            raise ValueError(f"obligation {oid} is closed")
        before = set(o.get("milestones") or {})
        _milestones(o, milestones)
        new = set(o["milestones"]) - before
        from .coordinator import _append_update
        for key in sorted(new):
            note = _milestone_note(p, oid, o["milestones"][key])
            for tid in o["owners"]:
                t = p.db.task(int(tid))
                if t:
                    p.db.update_task(int(tid), spec=t["spec"].rstrip() + "\n\n" + note + "\n")
            def tell(note=note, key=key):
                for tid in o["owners"]:
                    for r in p.db.q("SELECT dir FROM runs WHERE task=? AND status='running' AND dir IS NOT NULL",
                                    (int(tid),)):
                        _append_update(Path(r["dir"]) / "steer.md", note, f"obligation-{oid}-milestone-{key}")
            p.db.after_commit(tell)
        p.db.set_kv(KV, st)


def drop(p, oid: str, why: str, by: str = "user", now: float | None = None) -> str:
    """Withdraw an obligation recorded by mistake or no longer wanted: it is not owed and raises nothing."""
    why = " ".join(str(why or "").split())[:EVIDENCE_MAX]
    if not why:
        raise ValueError("drop needs a reason: why it is no longer owed")
    now = time.time() if now is None else now
    with p.db.tx():
        st = _load(p.db)
        o = _need(st, oid)
        if o["receipt"]:   # delivered: it is no longer owed either way
            return f"{oid} already has its receipt; nothing to drop"
        if o.get("dropped"):
            return f"{oid} was already dropped"
        o["dropped"] = {"at": now, "by": by, "why": why}
        _retire(p.db, now, "obligation dropped", prefix=f"obligation:{oid}:milestone:")
        _retire(p.db, now, "promise dropped", prefix=f"obligation:{oid}:promise")
        p.db.set_kv(KV, st)
    return f"{oid} dropped; it is no longer owed"


def _milestone_note(p, oid: str, m: dict) -> str:
    from . import timefmt
    return (f"### Obligation {oid} milestone {m['key']}\n"
            f"Due: {timefmt.long(m['due'], p)}. Required evidence: {m['evidence_required']}.\n"
            f"After checking it, record `ttp obligation receipt {oid} \"<evidence>\" "
            f"--milestone {m['key']}`; this satisfies only this milestone.")


def _owner_note(o: dict, p) -> str:
    return (f"## Obligation {o['id']} (urgent, owed to the user)\n{o['text']}\n"
            f"This task is a required owner. Record durable progress with evidence (a commit, a file, a URL) "
            f"at least every {CLOCK_S // 3600} h: `ttp obligation progress {o['id']} \"<evidence>\"`. Once "
            f"the deliverable exists and you checked it: `ttp obligation receipt {o['id']} \"<evidence>\"`. "
            f"Running, waiting or finishing this task without a receipt leaves it owed."
            + "".join("\n\n" + _milestone_note(p, o["id"], m) for m in (o.get("milestones") or {}).values()))


def attach(p, oid: str, tids: list[int], now: float | None = None) -> list[int]:
    """Make tasks required owners; returns the ones newly attached. Each is told through its spec, and a
    running one also through its run's steer.md. An owner already attached keeps its clock."""
    now = time.time() if now is None else now
    with p.db.tx():
        st = _load(p.db)
        o = _need(st, oid)
        if o["receipt"]:
            raise ValueError(f"obligation {oid} already has a receipt")
        if o.get("dropped"):
            raise ValueError(f"obligation {oid} was dropped")
        new = _attach(p, o, tids, now)
        p.db.set_kv(KV, st)
    return new


def _attach(p, o: dict, tids, now: float) -> list[int]:
    """attach() inside the caller's transaction: every owner is checked before anything is written, and
    running owners hear it only once the transaction commits."""
    from .coordinator import _append_update
    db, oid = p.db, o["id"]
    tasks = {}
    for tid in tids:
        t = db.task(int(tid))
        if not t:
            raise ValueError(f"no task #{tid}")
        if str(tid) not in o["owners"] and t["status"] in CLOSED:
            raise ValueError(f"task #{tid} is {t['status']}: attach the task that will produce it")
        tasks[int(tid)] = t
    new: list[int] = []
    for tid, t in tasks.items():
        if str(tid) in o["owners"]:
            continue
        o["owners"][str(tid)] = {"since": now, "progress": None, "evidence": []}
        if f"## Obligation {oid} " not in (t["spec"] or ""):
            db.update_task(tid, spec=(t["spec"] or "").rstrip() + "\n\n" + _owner_note(o, p) + "\n")
        new.append(tid)
    note = _owner_note(o, p)

    def tell():
        for tid in new:
            for r in db.q("SELECT dir FROM runs WHERE task=? AND status='running' AND dir IS NOT NULL", (tid,)):
                _append_update(Path(r["dir"]) / "steer.md", note, f"obligation-{oid}-{tid}")
    db.after_commit(tell)
    return new



def _record_progress(own: dict, evidence: str, now: float) -> None:
    own["progress"] = max(float(own.get("progress") or 0), now)
    own["evidence"] = (own.get("evidence") or [])[-(EVIDENCE_KEPT - 1):] + [{"at": now, "text": evidence}]


def record(p, oid: str, kind: str, evidence: str, task: int | None, now: float | None = None,
           milestone: str | None = None) -> str:
    """File durable progress or a receipt. Progress counts only from a required owner, and resets only
    that owner's clock. Returns what was done, in a few words."""
    if kind not in ("progress", "receipt"):
        raise ValueError(f"unknown record kind {kind!r}")
    evidence = " ".join(str(evidence or "").split())[:EVIDENCE_MAX]
    if not evidence:
        raise ValueError(f"{kind} needs evidence: a commit, a file or a URL")
    now = time.time() if now is None else now
    with p.db.tx():
        st = _load(p.db)
        o = _need(st, oid)
        if o["receipt"]:
            return f"{oid} already has a receipt"
        if o.get("dropped"):
            return f"{oid} was dropped; nothing to record"
        own = o["owners"].get(str(task)) if task is not None else None
        if task is not None and own is None:
            raise ValueError(f"task #{task} is not a required owner of {oid}")
        if own is not None and now < float(own["since"]):
            return f"{oid}: record from before #{task} owned it ignored"
        if milestone is not None:
            if kind != "receipt":
                raise ValueError("a milestone requires a receipt, not general progress")
            m = (o.get("milestones") or {}).get(milestone)
            if not m:
                raise ValueError(f"no milestone {milestone} on {oid}")
            if not m["receipt"]:
                m["receipt"] = {"at": now, "task": task, "evidence": evidence}
                if m["raised"] is not None:
                    _retire(p.db, now, "milestone receipt", f"obligation:{oid}:milestone:{milestone}:{m['episode']}")
                m["raised"] = None
                if own is not None:
                    _record_progress(own, evidence, now)
            p.db.set_kv(KV, st)
            return f"milestone {milestone} on {oid} has its receipt"
        if kind == "progress":
            if own is None:
                raise ValueError("progress is recorded by an owner task, from its run")
            _record_progress(own, evidence, now)
            msg = f"progress on {oid} recorded for #{task}"
        else:
            o["receipt"] = {"at": now, "task": task, "evidence": evidence}
            _retire(p.db, now, "obligation receipt", prefix=f"obligation:{oid}:milestone:")
            _retire(p.db, now, "promise kept", prefix=f"obligation:{oid}:promise")
            msg = f"{oid} has its receipt; it is no longer owed"
        p.db.set_kv(KV, st)
    return msg


def append_run(run_dir: Path, oid: str, kind: str, evidence: str, task: str, milestone: str | None = None) -> None:
    from .project import durable_append
    durable_append(Path(run_dir) / RUN_FILE, json.dumps({"ts": time.time(), "id": oid, "kind": kind,
                                                          "evidence": evidence, "task": task, "milestone": milestone}) + "\n")


def file_runs(p, now: float | None = None) -> list[str]:
    """File the records runs wrote (RUN_FILE) that are not filed yet; returns a line per refused one.
    A record counts for the run's own task only, at the time it was written (never in the future)."""
    now = time.time() if now is None else now
    db = p.db
    seen = dict(_load(db)["runs"])
    out: list[str] = []
    # A queued task's runs too: its on_pass_cmd writes to the run it waits on, however old.
    cutoff = now - RUN_FILES_DAYS * 86400
    runs = db.q("SELECT id, task, dir, started, status FROM runs WHERE dir IS NOT NULL AND task IS NOT NULL "
                "AND (status='running' OR started>=? OR task IN (SELECT id FROM tasks WHERE status='queued'))",
                (cutoff,))
    # Before offsets were kept (below), an old run's offset was dropped once it left the window; such a
    # run back in the set (its task re-queued) has its records from inside the window filed already.
    legacy = not _load(db).get("runs_kept")
    for r in runs:
        f = Path(r["dir"]) / RUN_FILE
        try:
            # Only whole lines: an append still being written is read on a later pass, not lost.
            lines = f.read_text(encoding="utf-8", errors="replace").split("\n")[:-1]
        except OSError:
            continue
        done = int(seen.get(str(r["id"]), 0))
        old = legacy and str(r["id"]) not in seen and r["status"] != "running" and float(r["started"] or 0) < cutoff
        for line in lines[done:]:
            try:
                rec = json.loads(line)
                if old and float(rec.get("ts") or 0) < float(r["started"] or 0) + RUN_FILES_DAYS * 86400:
                    continue
                record(p, str(rec["id"]), str(rec["kind"]), str(rec.get("evidence") or ""), int(r["task"]),
                       now=min(float(rec.get("ts") or now), now), milestone=rec.get("milestone"))
            except (ValueError, KeyError, TypeError) as e:
                out.append(f"run {r['id']}: {str(e)[:200]}")
        seen[str(r["id"])] = len(lines)
    # An offset is kept while it counts lines and the run is not gone: a run leaves the set when it ages
    # out and comes back when its task is re-queued, and must not be read again from line 0. It is
    # dropped once its task is closed and every obligation that task owns has its receipt or was dropped:
    # read again, its records change nothing (record() returns early for those).
    keep = {str(r["id"]) for r in runs}
    filed = [int(k) for k, v in seen.items() if k not in keep and int(v)]
    if filed:
        st = _load(db)
        open_owners = {t for o in st["items"].values() if not (o["receipt"] or o.get("dropped"))
                       for t in o["owners"]}
        for i in range(0, len(filed), RUN_IDS_PER_QUERY):   # one SQL variable each: stay under SQLite's cap
            part = filed[i:i + RUN_IDS_PER_QUERY]
            keep |= {str(r["id"]) for r in db.q(
                f"SELECT r.id, r.task, t.status FROM runs r LEFT JOIN tasks t ON t.id=r.task "
                f"WHERE r.id IN ({','.join('?' * len(part))})", tuple(part))
                if r["status"] not in CLOSED or str(r["task"]) in open_owners}
    with db.tx():
        st = _load(db)
        st["runs"] = {k: v for k, v in seen.items() if k in keep}
        st["runs_kept"] = True
        db.set_kv(KV, st)
    return out


def _last(own: dict) -> float:
    """When an owner's clock last started: its attach time, or its latest progress if later."""
    return max(float(own["since"]), float(own.get("progress") or 0))


def overdue(o: dict, now: float) -> list[tuple[str, float]]:
    """(owner task id, or "" for no owner; seconds since its last durable progress) for each clock run out.
    A promise has no owner clocks: only its due time counts (promise_late)."""
    if o["receipt"] or o.get("dropped") or o.get("promise"):
        return []
    if not o["owners"]:
        age = now - float(o["created"])
        return [("", age)] if age >= CLOCK_S else []
    out = []
    for tid, own in sorted(o["owners"].items(), key=lambda kv: int(kv[0])):
        age = now - _last(own)
        if age >= CLOCK_S:
            out.append((tid, age))
    return out


RESULT_FILES_MAX = 20000   # files a results glob looks at per job and pass: a huge tree is not walked whole


def _owner_runs(p, owners) -> tuple[dict[str, str], dict[str, dict]]:
    """(status per owner task id, runs with a folder per run id) of these owners."""
    ids = sorted(owners, key=int)
    marks = ",".join("?" * len(ids))
    status = {str(t["id"]): t["status"] for t in p.db.q(f"SELECT id, status FROM tasks WHERE id IN ({marks})", ids)}
    runs = {str(r["id"]): r for r in
            p.db.q(f"SELECT id, task, dir FROM runs WHERE dir IS NOT NULL AND task IN ({marks})", ids)}
    return status, runs


def _open_owners(st: dict) -> set[str]:
    return {t for o in st["items"].values() if not (o["receipt"] or o.get("dropped") or o.get("promise"))
            for t in o["owners"]}


def _live_jobs(p, st: dict) -> dict[str, list[dict]]:
    """Per owner of an open obligation (task id) that has not ended, the local `ttp detach` jobs of its
    own runs that still run (detach.state: .rc absent, its lock held on this boot), each its detached.json
    entry ("name", "rc", and "results" and "cwd" when it declared a results glob)."""
    owners = _open_owners(st)
    if not owners:
        return {}
    from . import detach
    status, runs = _owner_runs(p, owners)
    out: dict[str, list[dict]] = {}
    for run in runs.values():
        tid = str(run["task"])
        if status.get(tid, "gone") in CLOSED + ("gone",):
            continue
        try:
            jobs = json.loads((Path(run["dir"]) / detach.REGISTRY).read_text())
        except (OSError, ValueError):
            continue
        for j in jobs if isinstance(jobs, list) else []:
            if not isinstance(j, dict) or not j.get("rc") or j.get("status") in detach.CLOSED:
                continue
            try:
                live = detach.state(Path(j["rc"]))[0] == "running"
            except OSError:
                live = False
            if live:
                out.setdefault(tid, []).append(j)
    return out


def _result_file(job: dict, now: float) -> tuple[float, str] | None:
    """(mtime, path relative to the job's folder) of the newest file matching the job's declared results
    glob that changed after the job started and within CLOCK_S, else None."""
    pat, cwd = job.get("results"), job.get("cwd")
    if not pat or not cwd or Path(pat).is_absolute():
        return None
    since = max(float(job.get("started") or 0), now - CLOCK_S)
    best = None
    try:
        for f in itertools.islice(Path(cwd).glob(pat), RESULT_FILES_MAX):
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if since <= m <= now and f.is_file() and (best is None or m > best[0]):
                best = (m, str(f.relative_to(cwd)))
    except (OSError, ValueError, NotImplementedError):
        return None
    return best


def _job_results(p, st: dict, now: float, live: dict[str, list[dict]] | None = None) -> dict[str, dict]:
    """Per owner of an open obligation (task id), the latest result a `ttp detach` job of one of its own
    runs made on this boot within CLOCK_S, while that job still runs: a `result` report (progress.py;
    progress._alive reads detach.state: a job that wrote its .rc or is gone does not), or a new file
    matching the results glob the job declared (`ttp detach --results`, _result_file; `live` is
    _live_jobs). A pid or a growing log alone never counts. check reads it before its transaction: it
    reads the report file, each job's state and its result files."""
    owners = _open_owners(st)
    if not owners:
        return {}
    from . import progress
    from .runner import same_boot
    out: dict[str, dict] = {}
    for tid, jobs in (live or {}).items():
        for j in jobs:
            if (hit := _result_file(j, now)) and (tid not in out or hit[0] > out[tid]["ts"]):
                out[tid] = {"ts": hit[0], "job": j["rc"], "task": tid, "reason": f"new result file {hit[1]}"}
    recs = [r for r in progress.entries(p.state)
            if r["kind"] == "result" and r.get("job") and str(r.get("task") or "") in owners
            and 0 <= now - progress._ts(r, 0.0) < CLOCK_S and same_boot(r.get("boot"))]
    if not recs:
        return out
    status, runs = _owner_runs(p, owners)
    alive: dict[str, bool] = {}
    seen: set[str] = set()
    for r in sorted(recs, key=lambda x: progress._ts(x, 0.0), reverse=True):
        tid, run = str(r["task"]), runs.get(str(r.get("run") or ""))
        if tid in seen or status.get(tid, "gone") in CLOSED + ("gone",) or not run or str(run["task"]) != tid \
                or Path(r["job"]).parent.resolve() != Path(run["dir"]).resolve():   # a job its own run started
            continue
        if r["job"] not in alive:
            alive[r["job"]] = progress._alive(r)
        if alive[r["job"]]:
            seen.add(tid)
            if tid not in out or progress._ts(r, 0.0) > float(out[tid]["ts"]):
                out[tid] = r
    return out


def _job_progress(o: dict, jobs: dict[str, dict]) -> tuple[set[str], bool]:
    """The owners of o a live job's result (_job_results) keeps on time, each recorded on its owner as
    `job` ({"at", "text"}, dropped once no live job's result is fresh); and whether a record changed. The
    clock itself is not reset: once the job ends or goes quiet for CLOCK_S, the owner is late again from
    its own last progress."""
    kept, changed = set(), False
    for tid, own in o["owners"].items():
        r = jobs.get(tid)
        if not r or float(r["ts"]) < float(own["since"]):
            if own.pop("job", None) is not None:
                changed = True
            continue
        kept.add(tid)
        rec = {"at": float(r["ts"]), "text": f"job {Path(r['job']).stem}: {r.get('reason') or 'result'}"[:EVIDENCE_MAX]}
        if own.get("job") != rec:
            own["job"], changed = rec, True
    return kept, changed


def _quiet_job(o: dict, tid: str, j: dict) -> str:
    """How an overdue owner waiting on its own live job, which reported no result within CLOCK_S, gets
    back on time without another hand-made receipt task."""
    h = CLOCK_S // 3600
    said = (f"declares results `{j['results']}` but no new matching file in {h} h" if j.get("results")
            else f"reported no result in {h} h")
    return (f"its `ttp detach` job {j.get('name') or Path(j['rc']).stem} ({j['rc']}) still runs but {said}. "
            f"Fix it once: the driver reports each finished unit with `ttp progress report result --key <k> "
            f"\"<what>\"` (or a job started with `ttp detach --results <glob>` writes a file matching it per unit), "
            f"or dispatch one light run of #{tid} to record progress from the job's files (`ttp obligation "
            f"progress {o['id']} \"<result file>\"`)")


def anomaly_text(o: dict, late: list[tuple[str, float]], status: dict[str, str],
                 live: dict[str, list[dict]] | None = None) -> str:
    parts = []
    for tid, age in late:
        if not tid:
            parts.append(f"no owner task is attached ({age / 3600:.1f} h since it was recorded)")
            continue
        st = status.get(tid, "gone")
        tail = " without a receipt" if st in CLOSED or st == "gone" else ""
        parts.append(f"#{tid} ({st}{tail}) has no durable progress for {age / 3600:.1f} h"
                     + "".join(f"; {_quiet_job(o, tid, j)}" for j in (live or {}).get(tid, [])))
    return (f"Obligation {o['id']} is still owed to the user: {o['text']}\n- " + "\n- ".join(parts)
            + f"\nGet it moving: attach or start the owner that produces it (`ttp obligation attach`), or "
              f"have the owner record progress or its receipt.")


def _evidence(o: dict) -> str:
    parts = []
    for tid, own in o["owners"].items():
        ev = max(own.get("evidence") or [], key=lambda e: e["at"], default=None)
        parts.append(f"#{tid}: {ev['text'] if ev else 'none recorded'}")
    return "; ".join(parts) or "no owner attached"


def _retire(db, now: float, why: str, *fps: str, prefix: str | None = None) -> int:
    """Mark the still-queued events of resolved episodes handled, so a later coordinator turn is not woken
    by a condition that cleared. The rows and their text stay as history; handled ones are not touched."""
    if not fps and not prefix:
        return 0
    where = " OR ".join(["fingerprint=?"] * len(fps) + (["fingerprint LIKE ?"] if prefix else []))
    args = [*fps] + ([prefix + "%"] if prefix else [])   # ids are o<n>: no LIKE wildcards in a prefix
    return db.conn.execute(f"UPDATE events SET status='handled', data=? WHERE status='queued' AND ({where})",
                           (json.dumps({"retired": why, "at": now}), *args)).rowcount


def _raise(db, now: float, fp: str, kind: str, text: str, task: int | None) -> bool:
    if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        return False
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
         (now, "daemon", kind, fp, "high", text[:4000], "queued", task))
    return True


def _check_milestones(p, o: dict, now: float, raised: list[str]) -> bool:
    from . import timefmt
    changed = False
    closed = o["receipt"] or o.get("dropped")
    for m in (o.get("milestones") or {}).values():
        if closed or m["receipt"]:
            if m["raised"] is not None:
                _retire(p.db, now, "milestone receipt" if m["receipt"] else "obligation closed",
                        f"obligation:{o['id']}:milestone:{m['key']}:{m['episode']}")
                m["raised"] = None
                changed = True
        elif now >= m["due"] and m["raised"] is None:
            m["episode"] += 1
            m["raised"] = now
            changed = True
            fp = f"obligation:{o['id']}:milestone:{m['key']}:{m['episode']}"
            text = (f"Obligation {o['id']} milestone {m['key']} is overdue: {o['text']}\n"
                    f"Due: {timefmt.long(m['due'], p)}. Required evidence: {m['evidence_required']}.\n"
                    f"Owners and last evidence: {_evidence(o)}. "
                    "Get the owner moving and record the checked milestone receipt.")
            task = int(next(iter(o["owners"]))) if o["owners"] else None
            if _raise(p.db, now, fp, "obligation_overdue", text, task):
                raised.append(fp)
    return changed


def check(p, now: float | None = None) -> list[str]:
    """Raise one keyed observation per stall (file_runs first). An episode covers the owners late when it
    was raised: none again while they stay late, a new one when an owner it does not cover runs late, and
    it ends once no owner is late. Returns the fingerprints raised."""
    now = time.time() if now is None else now
    db = p.db
    raised: list[str] = []
    st0 = _load(db)
    live = _live_jobs(p, st0)
    jobs = _job_results(p, st0, now, live)
    with db.tx():
        st = _load(db)
        changed = False
        for o in st["items"].values():
            if (ev := _delivered(db, o)) is not None:   # a reply delivered it and its last owner finished
                _close(db, o, now, ev)
                changed = True
                continue
            changed = _check_milestones(p, o, now, raised) or changed
            changed = _check_promise(p, o, st, now, raised) or changed
            kept, job_changed = _job_progress(o, jobs)
            changed = job_changed or changed
            late = [x for x in overdue(o, now) if x[0] not in kept]   # a live job of its own still producing
            late_ids = [t for t, _ in late]
            covered = [t for t in o.get("covered") or [] if t in late_ids]   # an owner back on time leaves it
            if not late:
                if o.get("raised") or o.get("covered"):
                    o["raised"], o["covered"] = None, []   # the stall is over: a later one is a new episode
                    changed = True
                continue
            if set(late_ids) <= set(covered):
                if covered != o.get("covered"):
                    o["covered"] = covered
                    changed = True
                continue
            o["episode"] = int(o.get("episode") or 0) + 1
            o["raised"], o["covered"] = now, late_ids
            changed = True
            fp = f"obligation:{o['id']}:{o['episode']}"
            if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
                continue
            ids = [int(t) for t, _ in late if t]
            status = {str(t["id"]): t["status"] for t in
                      db.q(f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, "daemon", "obligation_overdue", fp, "high", anomaly_text(o, late, status, live)[:4000], "queued",
                  ids[0] if ids else None))
            raised.append(fp)
        if changed:
            db.set_kv(KV, st)
    return raised


def lines(db, now: float | None = None, where=None, open_only: bool = False) -> list[str]:
    from . import timefmt
    now = time.time() if now is None else now
    out = []
    for o in items(db):
        if open_only and (o["receipt"] or o.get("dropped")):
            continue
        if o["receipt"]:
            state = f"received ({o['receipt']['evidence'][:80]})"
        elif o.get("dropped"):
            state = f"dropped ({o['dropped']['why'][:80]})"
        elif o.get("promise"):
            state = "promised; " + _promise_state(o, now, where)
        else:
            late = {t for t, _ in overdue(o, now)
                    if not (o["owners"].get(t) or {}).get("job") or now - o["owners"][t]["job"]["at"] >= CLOCK_S}
            owners = ", ".join(
                f"#{t} {(now - _last(w)) / 3600:.1f} h"
                + (f" (job result {(now - w['job']['at']) / 3600:.1f} h ago)" if w.get("job") else "")
                + (" OVERDUE" if t in late else "") for t, w in sorted(o["owners"].items(), key=lambda kv: int(kv[0])))
            state = "owed; " + (f"owners: {owners}" if owners else "no owner attached" + (" OVERDUE" if late else ""))
            pending = [m for m in (o.get("milestones") or {}).values() if not m["receipt"]]
            if pending:
                m = min(pending, key=lambda m: (m["due"], m["key"]))
                state += f"; next milestone {m['key']} due {timefmt.long(m['due'], where)}"
                if now >= m["due"]:
                    state += " OVERDUE"
        out.append(f"{o['id']}  {o['text'][:100]}  [{state}]")
    return out


# promises -----------------------------------------------------------------------------------------
_FUTURE = r"\b(?:I|we)(?:'ll|’ll|\s+will|\s+shall|\s+am going to|'m going to|’m going to|\s+are going to|'re going to|’re going to)\s+(?!not\b|never\b)"
_DELIVER = (r"(?:report|deliver|share|confirm|follow up|come back|get back|circle back|let you know|tell you|"
            r"update you|ping you|reply|write (?:it |this |that )?up|send (?:you|it over|back|an update|word)|"
            r"post (?:it|them|an update|updates|the results?|back|here)|have (?:it|them|the|a|an|your|that|this)\b"
            r"[^.;!?]{0,80}?\bready)\b")
# "I'll post each result here", "we'll open a PR": a send verb, a few words, then the object it delivers
_SEND = (r"(?:post|send|open|share)\s+(?:[\w'’/#()-]+\s+){0,6}?"
         r"(?:here|results?|table|PR|pull request|numbers|link)\b")
# "... and it goes up as the draft PR": a delivery the sender owns, told in the third person (not "if it goes up")
_GOES_UP = (r"(?<!\bif\s)(?<!\bwhen\s)(?<!\bwhether\s)(?<!\bunless\s)"
            r"\b(?:it|the fix|the change)\s+goes\s+up\s+as\s+(?:a|the)\s+(?:draft\s+)?(?:PR|pull request)\b")
COMMIT_RE = re.compile(_FUTURE + r"(?:[\w'’-]+\s+){0,3}?(?:" + _DELIVER + "|" + _SEND + ")|" + _GOES_UP, re.I)
FUTURE_RE = re.compile(_FUTURE + r"|\byou(?:'ll|’ll|\s+will)\s+(?:have|get|see|hear)\b", re.I)
ETA_RE = re.compile(r"\bETA\b", re.I)
OFFER_RE = re.compile(r"\b(?:if you (?:want|like|wish|prefer|agree|approve|say so)|if you'd like|should I|want\s+me\s+to|"
                      r"shall\s+I|would you like)\b", re.I)
CLOCK_RE = re.compile(r"\b(?P<h>[01]?\d|2[0-3])(?::(?P<m>[0-5]\d))?\s*(?P<ap>[ap]\.?m\b\.?)?(?:\s*(?P<utc>UTC|GMT)\b)?", re.I)
IN_RE = re.compile(r"\b(?:in|within)\s+(?:about\s+|around\s+|roughly\s+|~)?(?P<n>\d+(?:\.\d+)?)\s*"
                   r"(?P<u>minutes?|mins?|m|hours?|hrs?|h)\b", re.I)
DAY_RE = re.compile(r"\b(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
                    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(?P<d>[0-3]?\d)\b", re.I)
PROMISE_DATE_MAX_D = 14   # a named date further ahead is a misread: only its clock time counts
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def _has_time(sentence: str) -> bool:
    return bool(IN_RE.search(sentence) or any(m.group("m") or m.group("ap") for m in CLOCK_RE.finditer(sentence)))


def commitments(text: str) -> list[str]:
    """The sentences of `text` that commit the sender to a deliverable or a time: first-person future with
    a delivery verb ("I'll report back") or a send verb and its object ("I'll post each result here"),
    "it goes up as the draft PR", first-person future with a time ("we'll have it by 15:00"), or
    an ETA with a time. Never a question or an offer ("if you want, I'll ...")."""
    out = []
    for s in _sentences(text):
        if s.endswith("?") or OFFER_RE.search(s):
            continue
        timed = _has_time(s)
        if COMMIT_RE.search(s) or (timed and (FUTURE_RE.search(s) or ETA_RE.search(s))):
            out.append(s)
    return out


def promise_due(text: str, now: float, tz) -> float | None:
    """The first time `text` names, as an epoch: 'in 90 min', '15:00', '3pm', '15:00 UTC', with 'tomorrow'
    or a 'Oct 15' date. A bare clock time is its next occurrence after `now`, in the zone `tz`. A date already
    past, or more than PROMISE_DATE_MAX_D days ahead (a misread), is ignored: the clock time the user was
    promised still holds, at its next occurrence. None only when no time is named."""
    from datetime import timedelta, timezone
    m = IN_RE.search(text)
    clock = next((c for c in CLOCK_RE.finditer(text) if c.group("m") or c.group("ap")), None)
    if m and (not clock or m.start() < clock.start()):
        n = float(m.group("n"))
        return now + n * (60 if m.group("u").lower().startswith("m") else 3600)
    if not clock:
        return None
    h, mi, ap = int(clock.group("h")), int(clock.group("m") or 0), (clock.group("ap") or "").lower()
    if ap and not 1 <= h <= 12:
        return None
    if ap.startswith("p") and h != 12:
        h += 12
    elif ap.startswith("a") and h == 12:
        h = 0
    zone = timezone.utc if clock.group("utc") else tz
    base = datetime.fromtimestamp(now, zone)
    day = DAY_RE.search(text)
    if day:
        try:
            when = base.replace(month=MONTHS.index(day.group("mon")[:3].lower()) + 1, day=int(day.group("d")),
                                hour=h, minute=mi, second=0, microsecond=0)
        except ValueError:
            return None
        if when.timestamp() < now - 180 * 86400:   # a date early in the year said late in the last one
            when = when.replace(year=when.year + 1)
        if now < when.timestamp() <= now + PROMISE_DATE_MAX_D * 86400:
            return when.timestamp()
    when = base.replace(hour=h, minute=mi, second=0, microsecond=0)
    if re.search(r"\btomorrow\b", text, re.I):
        when += timedelta(days=1)
    elif when.timestamp() <= now:
        when += timedelta(days=1)
    return when.timestamp()


def default_h(cfg: dict) -> float:
    try:
        v = float(((cfg or {}).get("coordinator") or {}).get("promise_default_h", PROMISE_DEFAULT_H))
    except (TypeError, ValueError):
        return PROMISE_DEFAULT_H
    return v if math.isfinite(v) and v > 0 else PROMISE_DEFAULT_H


def _chat(c) -> str:
    return "all" if c in (None, "", "all") else str(c)


def _open(o: dict) -> bool:
    return not (o["receipt"] or o.get("dropped"))


def owned_open(db, tid: int) -> list[str]:
    """The open obligations task `tid` is a required owner of: its checks go ahead of its project's other
    waiters for a host-wide checks slot (checkcap.task_priority) until each has its receipt or is dropped."""
    return [o["id"] for o in items(db) if _open(o) and str(tid) in o["owners"]]


def owner_due(db) -> dict[int, float]:
    """Task id -> the earliest due time of the open obligations it is a required owner of (owned_open's
    owners): an unreceipted milestone's due, else a promise's, else math.inf. Dispatch starts these
    tasks first, the earliest due first (db.ready_tasks)."""
    out: dict[int, float] = {}
    for o in items(db):
        if not _open(o) or not o["owners"]:
            continue
        dues = [m["due"] for m in (o.get("milestones") or {}).values() if not m["receipt"]]
        due = min(dues) if dues else float((o.get("promise") or {}).get("due") or math.inf)
        for tid in o["owners"]:
            out[int(tid)] = min(out.get(int(tid), math.inf), due)
    return out


def record_promises(p, sent: list[tuple[int, str | None, str]], tasks: dict[int, str | None],
                    obligations: list[str] = (), now: float | None = None) -> list[str]:
    """Record the commitments in this batch's sent messages (message id, chat, text). `tasks`: the batch's
    tasks by id with their reply_chat; one replying to a promise's chat owns it. `obligations`: the ids the
    batch recorded or attached owners to; one owned by a task that owns the promise is linked to it, and
    the promise closes with it. Every promise is recorded. Returns a note per promise."""
    now = time.time() if now is None else now
    found = [(mid, _chat(chat), said) for mid, chat, text in sent if (said := commitments(text))]
    if not found:
        return []
    from . import timefmt
    tz = timefmt.tzinfo(p)
    hours = default_h(p.config())
    notes = []
    with p.db.tx():
        st = _load(p.db)
        for mid, chat, said in found:
            text = " ".join(" ".join(said).split())[:PROMISE_TEXT_MAX]
            due = next((d for s in said if (d := promise_due(s, now, tz)) is not None), None)
            owners = sorted(t for t, c in tasks.items() if c is not None and _chat(c) == chat)
            links = sorted({oid for oid in obligations if (o := st["items"].get(oid)) and not o.get("promise")
                            and set(o["owners"]) & {str(t) for t in owners}})
            same = next((o for o in st["items"].values() if _open(o) and o.get("promise")
                         and o["promise"]["chat"] == chat and o["text"] == text), None)
            if same:
                pr = same["promise"]
                pr["tasks"] = sorted(set(pr["tasks"]) | set(owners))
                pr["obligations"] = sorted(set(pr.get("obligations") or []) | set(links))
                if pr["message"] == mid:
                    continue   # the same turn applied again
                pr["message"] = mid
                if due is not None and due != pr["due"]:
                    pr.update(due=due, timed=True, raised=None)
                notes.append(f"promise {same['id']} in chat {chat} updated from message #{mid}")
                continue
            oid = f"o{st['next']}"
            st["next"] += 1
            st["items"][oid] = {"id": oid, "text": text, "by": "coordinator", "created": now, "owners": {},
                                "receipt": None, "dropped": None, "episode": 0, "raised": None, "covered": [],
                                "promise": {"chat": chat, "message": mid, "due": due if due else now + hours * 3600,
                                            "timed": due is not None, "tasks": owners, "obligations": links,
                                            "raised": None}}
            notes.append(f"promise {oid} recorded from message #{mid}: due {timefmt.long(st['items'][oid]['promise']['due'], p)}"
                         + (f", owned by #{', #'.join(map(str, owners))}" if owners else ", no task owns it")
                         + (f", linked to {', '.join(links)}" if links else ""))
        p.db.set_kv(KV, st)
    return notes


def _kept(db, o: dict, st: dict) -> str | None:
    """Evidence that a promise's linked obligations all closed, or that its tasks all finished and a reply
    went to its chat after the last of them."""
    pr = o["promise"]
    links = [st["items"].get(oid) for oid in pr.get("obligations") or []]
    if links and all(x is None or not _open(x) for x in links):
        return f"linked {', '.join(pr['obligations'])} closed"
    if not pr["tasks"]:
        return None
    rows = db.q(f"SELECT id, status, updated FROM tasks WHERE id IN ({','.join('?' * len(pr['tasks']))})", pr["tasks"])
    if len(rows) < len(pr["tasks"]) or any(r["status"] not in CLOSED for r in rows):
        return None
    finished = max(float(r["updated"] or 0) for r in rows)
    chat = None if pr["chat"] == "all" else pr["chat"]
    m = db.one("SELECT id FROM messages WHERE direction='out' AND kind='reply' AND id>? AND ts>=? "
               "AND (chat IS NULL OR chat=?) ORDER BY id LIMIT 1",
               (int(pr["message"] or 0), finished - PROMISE_FINISH_SLACK_S, chat))
    return m and f"#{', #'.join(str(r['id']) for r in rows)} finished; reply #{m['id']} went to the chat"


def _check_promise(p, o: dict, st: dict, now: float, raised: list[str]) -> bool:
    """Close a kept promise, or raise its one overdue event. Returns whether `o` changed."""
    pr = o.get("promise")
    if not pr or not _open(o):
        return False
    fp = f"obligation:{o['id']}:promise"
    kept = _kept(p.db, o, st)
    if kept:
        o["receipt"] = {"at": now, "task": None, "evidence": kept}
        _retire(p.db, now, "promise kept", prefix=fp)
        return True
    if now < pr["due"] or pr.get("raised") is not None:
        return False
    from . import timefmt
    pr["raised"] = now
    owners = ", ".join(f"#{t}" for t in pr["tasks"]) or "no task"
    text = (f"Promise {o['id']} to the user is overdue (due {timefmt.long(pr['due'], p)}"
            + ("" if pr["timed"] else ", the default for a promise naming no time")
            + f"): in chat {pr['chat']}, message #{pr['message']}, you said: {o['text']}\n"
              f"Owner: {owners}. Send the deliverable or an honest update in that chat now, without being asked "
              f"(new ETA and why). Then close it: `obligation` with key {o['id']} and `evidence` (what was sent) "
              f"or `why` (no longer owed). A new promise in the update is tracked anew.")
    fp += f":{int(pr['due'])}"   # a promise given a new due time can fall due once more
    if _raise(p.db, now, fp, PROMISE_KIND, text, pr["tasks"][0] if pr["tasks"] else None):
        raised.append(fp)
    return True


def _promise_state(o: dict, now: float, where) -> str:
    from . import timefmt
    pr = o["promise"]
    late = now >= pr["due"]
    return (f"chat {pr['chat']}, message #{pr['message']}, "
            + (f"owner #{', #'.join(map(str, pr['tasks']))}" if pr["tasks"] else "no owner task")
            + f"; due {timefmt.long(pr['due'], where)}" + ("" if pr["timed"] else " (default)")
            + (f" OVERDUE {(now - pr['due']) / 3600:.1f} h" if late else ""))


def open_promises(db) -> list[dict]:
    return [o for o in items(db) if o.get("promise") and _open(o)]


def promise_lines(db, now: float | None = None, where=None) -> list[str]:
    """The digest's section: every open promise, overdue ones first."""
    now = time.time() if now is None else now
    rows = sorted(open_promises(db), key=lambda o: o["promise"]["due"])
    if not rows:
        return []
    late = sum(1 for o in rows if now >= o["promise"]["due"])
    out = [f"## Promises made in chat: {len(rows)} open" + (f", {late} OVERDUE" if late else "")
           + " (send each deliverable or an honest update in its chat; close with `obligation` key + evidence)"]
    out += [f"- {o['id']} {o['text'][:200]} [{_promise_state(o, now, where)}]" for o in rows]
    return out


def close_promise(db, oid: str, why: str, by: str, now: float | None = None) -> bool:
    """Close an open promise (a blocker sweep outcome); False when `oid` is no open promise."""
    now = time.time() if now is None else now
    with db.tx():
        st = _load(db)
        o = st["items"].get(oid)
        if not o or not o.get("promise") or not _open(o):
            return False
        o["receipt"] = {"at": now, "task": None, "evidence": (" ".join(str(why or "").split()) or by)[:EVIDENCE_MAX]}
        _retire(db, now, "promise closed", prefix=f"obligation:{oid}:promise")
        db.set_kv(KV, st)
    return True


def promise_outcomes(db, since: float, now: float | None = None) -> dict:
    """The promises that fell due between `since` and `now` and how each ended: kept on time (its receipt
    came by the due time), kept late (after it; the longest delay in hours), dropped, or still open."""
    now = time.time() if now is None else now
    due = [o for o in items(db) if o.get("promise") and since <= float(o["promise"]["due"]) <= now]
    late = [(float(o["receipt"]["at"]) - float(o["promise"]["due"])) / 3600 for o in due
            if o["receipt"] and float(o["receipt"]["at"]) > float(o["promise"]["due"])]
    kept = sum(bool(o["receipt"]) for o in due)
    return {"due": len(due), "on_time": kept - len(late), "late": len(late),
            "max_late_h": round(max(late), 1) if late else None,
            "dropped": sum(bool(o.get("dropped")) and not o["receipt"] for o in due),
            "open": [o["id"] for o in due if _open(o)]}


def audit_line(db, since: float, now: float | None = None) -> str:
    """The self-efficiency audit's line on the promises due since `since`; '' with none due."""
    now = time.time() if now is None else now
    r = promise_outcomes(db, since, now)
    if not r["due"]:
        return ""
    return (f"promises, {(now - since) / 3600:g} h: {r['due']} due, {r['on_time']} kept on time, {r['late']} kept late"
            + (f" (max {r['max_late_h']:.1f} h)" if r["late"] else "") + f", {len(r['open'])} overdue and open"
            + (f" ({', '.join(r['open'])})" if r["open"] else "") + (f", {r['dropped']} dropped" if r["dropped"] else ""))


# delivery ----------------------------------------------------------------------------------------
DELIVER_REF_RE = re.compile(r"(?:(?P<oid>o\d+)|#?(?P<task>\d+))")


def _owner_ids(o: dict) -> list[int]:
    return sorted({int(t) for t in o["owners"]} | set((o.get("promise") or {}).get("tasks") or []))


def _delivered(db, o: dict) -> str | None:
    """Evidence that a coordinator reply delivered `o` (its `delivers` named it) and every owner finished."""
    dv = o.get("delivered")
    if not dv or not _open(o):
        return None
    ids = _owner_ids(o)
    if ids:
        rows = db.q(f"SELECT status FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)
        if len(rows) < len(ids) or any(r["status"] not in CLOSED for r in rows):
            return None
    return f"reply #{dv['message']} delivered it" + (f"; #{', #'.join(map(str, ids))} finished" if ids else "")


def _close(db, o: dict, now: float, evidence: str) -> None:
    o["receipt"] = {"at": now, "task": None, "evidence": evidence[:EVIDENCE_MAX]}
    _retire(db, now, "obligation delivered", prefix=f"obligation:{o['id']}:")


def deliver(p, sent: list[tuple[int, str | None, list]], now: float | None = None) -> list[str]:
    """Mark the obligations this batch's replies deliver (message id, chat, the reply's `delivers`: obligation
    ids, or owner task ids standing for every open obligation they own). One closes as soon as every owner
    task finished too; check() closes one whose owners finish later. A promise is delivered only by a reply
    to its own chat. Returns a note per reference."""
    now = time.time() if now is None else now
    notes = []
    with p.db.tx():
        st = _load(p.db)
        for mid, chat, refs in sent:
            chat = _chat(chat)
            for ref in refs or []:
                m = DELIVER_REF_RE.fullmatch(str(ref).strip())
                if not m:
                    notes.append(f"delivers {str(ref)[:40]!r}: not an obligation id (o1) or a task id (#12)")
                    continue
                if m["oid"]:
                    o = st["items"].get(m["oid"])
                    hits = [o] if o and _open(o) else []
                    if o and o.get("promise") and o["promise"]["chat"] != chat:
                        notes.append(f"delivers {m['oid']}: that promise was made in chat {o['promise']['chat']}; "
                                     f"only a reply there delivers it")
                        continue
                else:
                    hits = [o for o in st["items"].values() if _open(o) and int(m["task"]) in _owner_ids(o)
                            and (not o.get("promise") or o["promise"]["chat"] == chat)]
                if not hits:
                    notes.append(f"delivers {str(ref)[:40]}: no open obligation")
                for o in hits:
                    o["delivered"] = {"message": mid, "chat": chat, "at": now}
                    ev = _delivered(p.db, o)
                    if ev:
                        _close(p.db, o, now, ev)
                        notes.append(f"{o['id']} delivered by reply #{mid}; it is no longer owed")
                    else:
                        notes.append(f"{o['id']} delivered by reply #{mid}; it closes once "
                                     f"#{', #'.join(map(str, _owner_ids(o)))} finish")
        p.db.set_kv(KV, st)
    return notes
