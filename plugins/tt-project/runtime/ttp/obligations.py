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
"<evidence>"`) ends the obligation. An owner task that ends (done, failed, cancelled, or gone) without one
leaves it owed. Owners that cannot act are not aged (_owner_cover): one that ended leaves the overdue set
while another owner is still live, and once every owner ended without a receipt the obligation raises one
episode, "no live owner is left", when the newest owner's clock runs out. An owner blocked on an open user
ask (a hold anchored on it, `waits:ask:<id>`, or an open ask naming it as #<id> while it is blocked or
waiting) is covered by that ask: never late while the ask is open, and its clock restarts when the ask is
resolved (`ask_end`). An owner asleep by design on a model-free wake (_asleep: a waiting hand-off's
retry_when while its not_before is ahead, a deferral's start_when or a start_after still ahead, with a
recorded reason) is covered the same way while that wait lasts: its clock restarts from the wake
(`sleep_end`: the start_after time, else the check that saw it wake). Not covered: a probe on its own `ttp detach` job or an owner with a live job
(the job rule above governs), a woken wait, a start_when whose last answer is broken or passed, a probe
naming a missing, failed or cancelled task, and a wait older than coordinator.defer_max_days. Milestones
raise whatever the owners do. The harness records the evidence as given: checking it stays with the owner.

Inside a run the CLI cannot write the project's database, so `progress` and `receipt` append to the
run's RUN_FILE, and the daemon files them (only for an obligation that run's task owns). Outside a run
(the user, the relay) they are filed at once.

Daemon.check_obligations looks every CHECK_EVERY_S, model-free: it files new run records, and for each
open obligation with an owner past its clock (or no owner at all, past the clock from its creation)
raises one high observation keyed `obligation:<id>:<episode>` for the coordinator. First it attaches the
open tasks that carry an owner's work on (_attach_carriers; a coordinator task_add does the same for its new
task): the daemon's `Review #<id>`, `Fix review #<id>` and `Re-review #<id>` hops and a task_add `continues`
naming an owner, noted in the obligation's history, so an owner that finished leaves its chain owning it.
A "no live owner is left" observation lists the open tasks whose title or spec names a former owner as
#<id>, the candidates to attach (_candidates). It is not raised
again on a timer while the same owners stay stalled; an owner not yet covered that stalls (say one
attached after an earlier owner ended) opens the next episode, and once no owner is late the episode ends.
Milestones have stable keys, absolute due times and evidence_required. The coordinator adds them on
its obligation action; an owner records a checked milestone with `ttp obligation receipt <id>
"<evidence>" --milestone <key>`, leaving the obligation open. A missing milestone receipt at its due
time raises one durable high episode, even with fresh owner progress or a healthy heartbeat. A milestone
episode that ends (its receipt, the obligation's receipt, a drop) retires its still-queued event, so the
coordinator is not woken for it. Status and the web app show the next deadline in the project's home time zone.
Known gap: beyond ended owners, open asks and model-free sleeps, nothing checks whether an owner can advance the obligation,
so one whose owners all wait or stall is caught only by the 2 h progress rule (or a milestone falling due).
A wider coverage check was dropped: it took ordinary waits, backoffs and review stacks for stuck owners
and woke the coordinator.

A mistaken or no longer wanted obligation is withdrawn with `ttp obligation drop <id> "<why>"`.

Promises: a coordinator `reply` or `notify` that commits to a deliverable or a time ("I'll report the
sha by 15:00", "ETA 18:30") is recorded as an obligation with a `promise` field (record_promises, called
when the turn's actions are applied): the chat, the message, the promised sentences, the due time (the
first time named, in the project's home zone; else coordinator.promise_default_h, PROMISE_DEFAULT_H,
after the reply) and the tasks of the same batch that will reply to that chat (task_add or task_update
with that `reply_chat`). A promise no batch task owns, from a reply with no `owners` or `delivers`, is
attached to the one open task (queued, running, waiting or blocked) the reply names as `#<id>` whose
`reply_chat` is that chat, as a manual attach would, and the attach is kept in its `history` (_named_owner;
none or several such tasks: it stays unowned). It is always recorded; an obligation the same batch records or attaches is
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
Keys of closed obligations (receipt or dropped) and delivered ones are citations and pass: only an open key
claims ownership. An order's or promise's owner tasks (its `tasks`) own it as its attached owners do. The
coordinator's STATE shows each open task's open keys (open_keys_by_task), so it sees what a task owns.
"""
from __future__ import annotations

import fnmatch
import os
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
REOWN_LABEL = "reown:"        # + obligation id: the owner task the daemon started for it
RUN_FILES_DAYS = 7            # run records are looked for in runs started this recently (or still running)
RUN_IDS_PER_QUERY = 500       # kept run offsets checked per query (SQLite caps the variables in one)
CLOSED = ("done", "failed", "cancelled", "superseded")   # superseded: a review a later passing one covers (db.SUPERSEDED)
_CLOSED_QS = ",".join("?" * len(CLOSED))
DEFER_MAX_DAYS = 14.0         # coordinator.defer_max_days: a sleep this old no longer covers its owner
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
        elif not _open(o) or o.get("delivered"):
            continue   # a closed or delivered key is a citation: no owner is told to work on it
        elif tid is None:
            attach.append(oid)
        elif int(tid) not in _owner_ids(o) and (oid, int(tid)) not in attaching:
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
                      "episode": 0, "raised": None, "added": time.time()}


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
        if why := unfit_owner(p.db, (o.get("promise") or {}).get("class"), tids):
            raise ValueError(f"order {oid}: {why}")
        new = _attach(p, o, tids, now)
        if (o.get("promise") or {}).get("order") is not None:   # an order's owners are its tasks (_kept)
            o["promise"]["tasks"] = sorted(set(o["promise"]["tasks"]) | {int(t) for t in tids})
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


CARRY_LABELS = (("auto_review:", "reviews"), ("review_fix:", "fixes the review"),
                ("continues:", "continues"), ("follows:", "follows up"))
HOP_TITLE = re.compile(r"\s*(Review|Fix review|Re-review) #(\d+)\b")


def _carried(t) -> dict[str, str]:
    """Task id -> how open task `t` carries that task's work on: the daemon's review, fix and re-review
    hops (their labels and `Review #<id>` / `Fix review #<id>` / `Re-review #<id>` titles), a task_add
    `continues` (its `continues:` or `follows:` label) and a `continue:` follow-up of its parent."""
    from .chaincap import CONT_TITLE
    out: dict[str, str] = {}
    try:
        labels = json.loads(t["labels"] or "[]")
    except ValueError:
        labels = []
    for lb in labels if isinstance(labels, list) else []:
        for pre, how in CARRY_LABELS:
            if isinstance(lb, str) and lb.startswith(pre) and lb[len(pre):].isdigit():
                out.setdefault(lb[len(pre):], how)
    title = t["title"] or ""
    if m := HOP_TITLE.match(title):
        out.setdefault(m.group(2), m.group(1).lower() + "s")
    if t["parent"] and CONT_TITLE.match(title):
        out.setdefault(str(t["parent"]), "continues")
    return out


def _attach_carriers(p, st: dict, now: float, tids=None) -> list[tuple[str, int]]:
    """Attach the open tasks (or only `tids`) that carry an owner's work on (_carried) to each open
    obligation it owns, inside the caller's transaction, and note each in the obligation's history. A
    carrier of a carrier is attached too. Promises and delivered obligations are left as they are: their
    closing waits on their owners finishing."""
    obs = [o for o in st["items"].values() if _open(o) and o["owners"] and not o.get("promise")
           and not o.get("delivered")]
    if not obs:
        return []
    sql = f"SELECT id, title, labels, parent FROM tasks WHERE status NOT IN ({_CLOSED_QS})"
    args: list = [*CLOSED]
    if tids is not None:
        ids = [int(t) for t in tids]
        if not ids:
            return []
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        args += ids
    carriers = {str(r["id"]): _carried(r) for r in p.db.q(sql, args)}
    carriers = {t: c for t, c in carriers.items() if c}
    out: list[tuple[str, int]] = []
    for o in obs:
        grew = True
        while grew:   # a review of a fix that was just attached is attached in the same pass
            grew = False
            for tid in sorted(carriers, key=int):
                via = sorted((s for s in carriers[tid] if s in o["owners"]), key=int)
                if tid in o["owners"] or not via:
                    continue
                _attach(p, o, [int(tid)], now)
                o.setdefault("history", []).append({
                    "at": now, "event": "attached", "task": int(tid),
                    "why": f"#{tid} {carriers[tid][via[0]]} #{via[0]}, an owner: it carries that work on"})
                out.append((o["id"], int(tid)))
                grew = True
    return out


def attach_carriers(p, tids=None, now: float | None = None) -> list[tuple[str, int]]:
    """_attach_carriers in its own transaction: (obligation id, task id) for each attach made."""
    now = time.time() if now is None else now
    with p.db.tx():
        st = _load(p.db)
        out = _attach_carriers(p, st, now, tids)
        if out:
            p.db.set_kv(KV, st)
    return out


def _candidates(db, o: dict, limit: int = 10) -> list[dict]:
    """Open tasks, not owners, whose title or spec names a former owner of `o` as #<id>: the ones that may
    carry its work on, for an obligation with no live owner left."""
    ids = sorted(o["owners"], key=int)
    if not ids:
        return []
    like = " OR ".join(["title LIKE ? OR spec LIKE ?"] * len(ids))
    rows = db.q(f"SELECT id, title, spec, status FROM tasks WHERE status NOT IN ({_CLOSED_QS}) AND ({like}) ORDER BY id",
                [*CLOSED, *(x for t in ids for x in (f"%#{t}%",) * 2)])
    out = []
    for r in rows:
        named = set(TASK_REF.findall(r["title"] or "")) | set(TASK_REF.findall(r["spec"] or ""))
        if str(r["id"]) not in o["owners"] and named & set(ids):
            out.append({"id": r["id"], "title": r["title"], "status": r["status"],
                        "names": sorted(named & set(ids), key=int)})
    return out[:limit]



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
            if _accepts(o) and (why := _receipt_unmet(p.db, o, evidence, task)):
                raise ValueError(f"{oid}: receipt refused: {why}, so the acceptance is not met. Record it once an "
                                 f"owner meets it, or give evidence that does (a commit, a file, a met hand-off)")
            o["receipt"] = {"at": now, "task": task, "evidence": evidence}
            _retire(p.db, now, "obligation receipt", prefix=f"obligation:{oid}:milestone:")
            _retire(p.db, now, "promise kept", prefix=f"obligation:{oid}:promise")
            msg = f"{oid} has its receipt; it is no longer owed"
        p.db.set_kv(KV, st)
    return msg


def _receipt_unmet(db, o: dict, evidence: str, task: int | None) -> str | None:
    """Why a receipt for `o` cites an owner hand-off that leaves the acceptance unmet: the filing task
    itself ended so, or every owner task the evidence names (#<id>) did (unmet_handoff, the other owners
    as carriers). Evidence naming no owner (a commit, a file, a URL) is the recorder's word."""
    ids = _owner_ids(o)
    if task is not None and (why := unmet_handoff(db, ids, only=[int(task)])):
        return why
    cited = list(dict.fromkeys(int(t) for t in TASK_REF.findall(evidence or "") if int(t) in ids))
    whys = [unmet_handoff(db, ids, only=[t]) for t in cited]
    return "; ".join(whys) if cited and all(whys) else None


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
    """When an owner's clock last started: its attach time, its latest progress, or the end of the user
    ask it was blocked on (ask_end), whichever is latest."""
    return max(float(own["since"]), float(own.get("progress") or 0), float(own.get("ask_end") or 0),
               float(own.get("sleep_end") or 0))


def _max_sleep_s(cfg) -> float:
    try:
        return float(((cfg or {}).get("coordinator") or {}).get("defer_max_days") or DEFER_MAX_DAYS) * 86400
    except (AttributeError, TypeError, ValueError):
        return DEFER_MAX_DAYS * 86400


def _asleep(db, t, now: float, max_s: float, gates: dict) -> tuple[str, float | None] | None:
    """(what it sleeps on, its known wake or None) for an owner asleep by design on a model-free wake with a
    recorded reason, else None (module doc)."""
    from . import detach, ends
    from .db import deferral, load_result
    from .sweepskip import _dead_refs
    if t["status"] != "queued":
        return None
    prev = load_result(t["result"])
    if prev.get("status") == "waiting" and not deferral(t).get("when"):
        # A waiting hand-off is stored queued; probe_waiting keeps not_before ahead while its probe says
        # not yet and brings it to now once the probe passes (or its timer runs out): then it is due.
        probe = prev.get("retry_when")
        if prev.get("woke") or not isinstance(probe, str) or not probe.strip() or detach.CHECK_RE.search(probe) \
                or not (prev.get("waiting_for") or t["blocked_reason"]) or float(t["not_before"] or 0) <= now:
            return None
        try:
            since = float(prev["waiting_since"])
        except (KeyError, TypeError, ValueError):
            return None
        if now - since >= max_s or _dead_refs(db, t, probe):
            return None
        return f"retry_when `{probe.strip()[:120]}`", None
    d = deferral(t)
    if not (d.get("why") or t["blocked_reason"]):
        return None
    if d.get("after") is not None and d["after"] > now:
        return "start_after", d["after"]
    when = d.get("when")
    if not when or detach.CHECK_RE.search(when) or now - float(d.get("since") or t["created"]) >= max_s \
            or _dead_refs(db, t, when):
        return None
    got = gates.get(str(t["id"])) or {}
    if got.get("when") == when and got.get("rc") not in ends.NOT_YET_RCS:
        return None   # broken, or it passed and the task starts
    return f"start_when `{when.strip()[:120]}`", None


def _owner_cover(db, obs, now: float | None = None, cfg=None, jobs=()) -> dict[str, tuple]:
    """Per owner task id of these obligations that cannot act now: ("finished", its status, or "gone" for
    a task that no longer exists), ("ask", the id of the open user ask it is blocked on: a hold anchored
    on it, or an open ask naming it as #<id> while it is blocked or waiting), or ("asleep", what it sleeps
    on, its known wake or None) (_asleep; never an owner in `jobs`, those with a live job). Database reads
    only."""
    from . import startgate, waits
    ids = sorted({int(t) for o in obs for t in o["owners"]})
    if not ids:
        return {}
    now = time.time() if now is None else now
    rows = {str(t["id"]): t for t in
            db.q(f"SELECT * FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)}
    asks, gates, out = None, None, {}
    for tid in map(str, ids):
        t = rows.get(tid)
        if t is None or t["status"] in CLOSED:
            out[tid] = ("finished", t["status"] if t else "gone")
        elif t["status"] in ("blocked", "waiting"):
            asks = waits.open_asks(db) if asks is None else asks
            held = waits._held_ask(t)
            linked = [a["id"] for a in asks if a["id"] == held] + [a["id"] for a in waits.asks_for(int(tid), asks)]
            if linked:
                out[tid] = ("ask", str(linked[0]))
        if tid not in out and tid not in jobs and t["status"] == "queued":
            gates = (db.kv(startgate.KEY, {}) or {}) if gates is None else gates
            try:
                sleep = _asleep(db, t, now, _max_sleep_s(cfg), gates)
            except Exception:   # an unreadable record covers nothing: the owner keeps its clock
                sleep = None
            if sleep:
                out[tid] = ("asleep", *sleep)
    return out


def _sleep_clocks(o: dict, cover: dict[str, tuple], now: float) -> bool:
    """Record on each owner the wake it sleeps on (`sleep`: {what, until}); once it no longer sleeps, its
    clock restarts from the wake (`sleep_end`: its known wake, else now). Returns whether a record changed."""
    changed = False
    for tid, own in o["owners"].items():
        c = cover.get(tid)
        rec = {"what": c[1], "until": c[2]} if c and c[0] == "asleep" else None
        if rec is not None and own.get("sleep") != rec:
            own["sleep"], changed = rec, True
        elif rec is None and own.get("sleep") is not None:
            until = own.pop("sleep").get("until")
            own["sleep_end"], changed = min(now, float(until)) if until is not None else now, True
    return changed


def _ask_clocks(o: dict, cover: dict[str, tuple], now: float) -> bool:
    """Record on each owner the open ask it is blocked on (`ask`); once that ask is no longer open, its clock
    restarts then (`ask_end`). Returns whether a record changed."""
    changed = False
    for tid, own in o["owners"].items():
        c = cover.get(tid)
        ask = c[1] if c and c[0] == "ask" else None
        if ask is not None and own.get("ask") != ask:
            own["ask"], changed = ask, True
        elif ask is None and own.get("ask") is not None:
            own.pop("ask")
            own["ask_end"], changed = now, True
    return changed


def overdue(o: dict, now: float, cover: dict[str, tuple] | None = None) -> list[tuple[str, float]]:
    """(owner task id, or "" for no owner or no live owner left; seconds since its last durable progress)
    for each clock run out. `cover` (_owner_cover) leaves out owners that finished or wait on an open ask
    (and one whose recorded ask just ended, until check restarts its clock); once every owner finished, the
    newest owner's clock stands for them all. A promise has no owner clocks: only its due time counts
    (promise_late)."""
    if o["receipt"] or o.get("dropped") or o.get("promise"):
        return []
    if not o["owners"]:
        age = now - float(o["created"])
        return [("", age)] if age >= CLOCK_S else []
    cover = cover or {}
    if all((cover.get(t) or ("",))[0] == "finished" for t in o["owners"]):
        age = now - max(_last(own) for own in o["owners"].values())
        return [("", age)] if age >= CLOCK_S else []
    out = []
    for tid, own in sorted(o["owners"].items(), key=lambda kv: int(kv[0])):
        if tid in cover or own.get("ask") is not None or own.get("sleep") is not None:
            continue
        age = now - _last(own)
        if age >= CLOCK_S:
            out.append((tid, age))
    return out


RESULT_FILES_MAX = 20000   # files a results glob looks at per job and pass: the walk itself stops after this many directory entries or RESULT_WALK_S seconds
RESULT_WALK_S = 2.0


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


def _glob_walk(root: str, pat: str):
    """Yield paths under root matching the glob `pat` (`**` spans folders), visiting at most
    RESULT_FILES_MAX directory entries and for at most RESULT_WALK_S seconds, so a big tree never stalls
    the caller; the walk then just ends (partial results). Literal leading parts ('out/', '..') are
    joined directly, as pathlib does, and the walk enters only folders whose path can still lead to a
    match, so a fixed-prefix glob never spends its budget on unrelated folders."""
    parts = [x for x in pat.split("/") if x not in ("", ".")]
    while parts and parts[0] != "**" and not re.search(r"[*?[]", parts[0]):
        root = os.path.join(root, parts.pop(0))
    if not parts:
        if os.path.lexists(root):
            yield Path(root)
        return
    rx = [None if x == "**" else re.compile(fnmatch.translate(x)) for x in parts]
    end = time.monotonic() + RESULT_WALK_S
    seen = 0

    def match(names, i=0, j=0):
        if j == len(rx):
            return i == len(names)
        if rx[j] is None:
            return any(match(names, k, j + 1) for k in range(i, len(names) + 1))
        return i < len(names) and rx[j].match(names[i]) is not None and match(names, i + 1, j + 1)

    def leads(names, i=0, j=0):
        """Whether a path below folder `names` can still match: each part matches until a '**'."""
        if j == len(rx):
            return False
        if rx[j] is None or i == len(names):
            return True
        return rx[j].match(names[i]) is not None and leads(names, i + 1, j + 1)

    stack = [(root, ())]
    while stack:
        d, rel = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    if seen > RESULT_FILES_MAX or time.monotonic() > end:
                        return
                    names = rel + (e.name,)
                    if match(names):
                        yield Path(e.path)
                    if leads(names) and e.is_dir(follow_symlinks=False):
                        stack.append((e.path, names))
        except OSError:
            continue


def _result_file(job: dict, now: float) -> tuple[float, str] | None:
    """(mtime, path relative to the job's folder) of the newest file matching the job's declared results
    glob that changed after the job started and within CLOCK_S, else None."""
    pat, cwd = job.get("results"), job.get("cwd")
    if not pat or not cwd or Path(pat).is_absolute():
        return None
    since = max(float(job.get("started") or 0), now - CLOCK_S)
    best = None
    try:
        for f in _glob_walk(str(cwd), pat):
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
                 live: dict[str, list[dict]] | None = None, candidates: list[dict] | None = None) -> str:
    parts = []
    for tid, age in late:
        if not tid and o["owners"]:
            parts.append("no live owner is left: " + ", ".join(
                f"#{t} ({status.get(t, 'gone')} without a receipt)" for t in sorted(o["owners"], key=int))
                + f"; the newest durable progress was {age / 3600:.1f} h ago")
            if candidates:
                parts.append("open tasks naming a former owner (attach the one that carries it on: `ttp obligation "
                             f"attach {o['id']} <task>`): " + "; ".join(
                                 f"#{c['id']} ({c['status']}, names #{', #'.join(c['names'])}) {c['title'][:100]}"
                                 for c in candidates))
            else:
                parts.append("no open task names a former owner")
            continue
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


def _raise(db, now: float, fp: str, kind: str, text: str, task: int | None, data: dict | None = None,
           severity: str = "high") -> bool:
    if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        return False
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task,data) VALUES(?,?,?,?,?,?,?,?,?)",
         (now, "daemon", kind, fp, severity, text[:4000], "queued", task, json.dumps(data) if data else None))
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


def _reown(p, o: dict, now: float, raised: list[str]) -> bool:
    """An obligation whose no-live-owner wake was answered and still has no live owner once the turns that
    could have answered it ended: that fix did not land (an attach of a task that finished meanwhile, a
    rejected task_add). Once per owner set (`reowned`), model-free: a new owner task, or, once the
    daemon's own owner also ended without a receipt, one more high wake; never a second daemon owner.
    Returns whether it changed."""
    db, oid = p.db, o["id"]
    state = sorted(o["owners"], key=int)
    if (o.get("reowned") or {}).get("state") == state:
        return False
    if db.one("SELECT id FROM events WHERE fingerprint=? AND status IN ('new','queued')",
              (f"obligation:{oid}:{o['episode']}",)):
        return False   # the wake is not answered yet
    first = (o.get("answered") or {}).get("episode") != o["episode"]
    if first:   # first seen answered: only a turn already running then can have answered it
        o["answered"] = {"episode": o["episode"], "at": now}
    if db.one("SELECT id FROM runs WHERE role='coordinator' AND status='running' AND started<=?",
              (o["answered"]["at"],)):
        return first   # the turn that answered it may still attach an owner
    label = f"{REOWN_LABEL}{oid}"
    rows = db.q(f"SELECT id, status, labels FROM tasks WHERE id IN ({','.join('?' * len(state))})",
                [int(t) for t in state]) if state else []
    status = {str(r["id"]): r["status"] for r in rows}
    former = ", ".join(f"#{t} ({status.get(t, 'gone')})" for t in state) or "none"
    mine = [str(r["id"]) for r in rows if f'"{label}"' in (r["labels"] or "")]   # labels: a JSON list of strings
    o["reowned"] = {"state": state, "at": now}
    if mine:
        fp = f"obligation:{oid}:reown:{'-'.join(state)}"
        o["reowned"]["wake"] = fp
        text = (f"Obligation {oid} is still owed to the user with no live owner: {o['text']}\n"
                f"- its overdue wake was handled but no owner was attached, and the daemon's own owner "
                f"#{', #'.join(mine)} ended without a receipt; owners: {former}; last evidence: {_evidence(o)}\n"
                f"Attach the task that produces it (`ttp obligation attach {oid} <task>`), record the receipt, "
                f"or drop it with the reason (evidence starting 'not owed:' is the owner's case for dropping it). "
                f"The daemon starts no second owner for it.")
        if _raise(db, now, fp, "obligation_overdue", text, int(mine[-1])):
            raised.append(fp)
        return True
    spec = (f"Obligation {oid} is owed to the user and has had no live owner since its overdue wake: "
            f"{o['text']}\n\nFormer owners, each ended without a receipt: {former}. Their last evidence: "
            f"{_evidence(o)}.\n\nThe daemon started this task because the coordinator's answer to the wake attached "
            "no owner. Read the former owners' results, then produce what is owed and record the receipt, or, if "
            "it is already delivered, record the receipt with that evidence. If it is no longer owed (its subject "
            "ended or was superseded, such as a PR closed and replaced, or a user decision covered it), record no "
            f"receipt: record `ttp obligation progress {oid} \"not owed: <evidence>\"`; once this task ends the daemon "
            "wakes the coordinator once, quoting it, to drop it. Do only what the charter and its "
            "policies allow: a step that needs the user's approval (a PR leaving draft, a merge, a post as the "
            "user) is named in your hand-off as the decision needed, never taken here. If it cannot be produced, "
            "say why in your hand-off.")
    tid = db.add_task(f"Carry obligation {oid} to its receipt: {' '.join(o['text'].split())[:120]}", spec,
                      kind="work", tier="standard", priority=1, origin="daemon", labels=[label])
    _attach(p, o, [tid], now)
    o.setdefault("history", []).append({
        "at": now, "event": "attached", "task": tid,
        "why": f"the daemon: no live owner after its overdue wake was answered (owners {former})"})
    o["reowned"]["task"] = tid
    return True


def check(p, now: float | None = None, cfg: dict | None = None) -> list[str]:
    """Raise one keyed observation per stall (file_runs first). An episode covers the owners late when it
    was raised: none again while they stay late, a new one when an owner it does not cover runs late, and
    it ends once no owner is late. Returns the fingerprints raised."""
    now = time.time() if now is None else now
    if cfg is None:
        try:
            cfg = p.config()
        except Exception:   # an unreadable config keeps the default defer_max_days
            cfg = {}
    db = p.db
    raised: list[str] = []
    st0 = _load(db)
    live = _live_jobs(p, st0)
    jobs = _job_results(p, st0, now, live)
    with db.tx():
        st = _load(db)
        changed = bool(_attach_carriers(p, st, now))   # a chain carrying an owner's work on owns it too
        cover = _owner_cover(db, [o for o in st["items"].values() if _open(o) and not o.get("promise")],
                             now, cfg, set(live))
        for o in st["items"].values():
            if (ev := _delivered(db, o)) is not None:   # a reply delivered it and its last owner finished
                _close(db, o, now, ev)
                changed = True
                continue
            changed = _check_milestones(p, o, now, raised) or changed
            changed = _check_promise(p, o, st, now, raised) or changed
            kept, job_changed = _job_progress(o, jobs)
            changed = job_changed or changed
            if _open(o) and not o.get("promise"):
                changed = _ask_clocks(o, cover, now) or changed
                changed = _sleep_clocks(o, cover, now) or changed
            late = [x for x in overdue(o, now, cover) if x[0] not in kept]   # a live job of its own still producing
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
                if late_ids == [""]:
                    changed = _reown(p, o, now, raised) or changed
                continue
            o["episode"] = int(o.get("episode") or 0) + 1
            o["raised"], o["covered"] = now, late_ids
            changed = True
            fp = f"obligation:{o['id']}:{o['episode']}"
            if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
                continue
            ids = [int(t) for t, _ in late if t] or [int(t) for t in sorted(o["owners"], key=int)]   # no live owner: all of them
            status = {str(t["id"]): t["status"] for t in
                      db.q(f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
            text = anomaly_text(o, late, status, live, _candidates(db, o) if "" in late_ids and o["owners"] else None)
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, "daemon", "obligation_overdue", fp, "high", text[:4000], "queued", ids[0] if ids else None))
            raised.append(fp)
        if changed:
            db.set_kv(KV, st)
    return raised


def lines(db, now: float | None = None, where=None, open_only: bool = False) -> list[str]:
    from . import timefmt
    now = time.time() if now is None else now
    out = []
    obs = items(db)
    cover = _owner_cover(db, [o for o in obs if _open(o) and not o.get("promise")], now,
                         where if isinstance(where, dict) else None)
    for o in obs:
        if open_only and (o["receipt"] or o.get("dropped")):
            continue
        if o["receipt"]:
            state = f"received ({o['receipt']['evidence'][:80]})"
        elif o.get("dropped"):
            state = f"dropped ({o['dropped']['why'][:80]})"
        elif o.get("promise"):
            pr = o["promise"]
            state = ("claimed; " if pr.get("claim") else f"ordered ({pr.get('class')}); " if pr.get("order") is not None
                     else "promised; ") + _promise_state(o, now, where)
            if (pr.get("claim") or {}).get("last") == CLAIM_UNREACHABLE:
                state += "; unchecked: its check's host did not answer (exit 255), retrying"
            if pr.get("order") is not None and not pr.get("started"):
                state += f"; not started (start by {timefmt.long(pr['start_by'], where)})"
        else:
            late = {t for t, _ in overdue(o, now, cover)
                    if not (o["owners"].get(t) or {}).get("job") or now - o["owners"][t]["job"]["at"] >= CLOCK_S}
            owners = ", ".join(_owner_state(t, w, cover.get(t), late, now)
                               for t, w in sorted(o["owners"].items(), key=lambda kv: int(kv[0])))
            state = "owed; " + (f"owners: {owners}" + ("; no live owner left OVERDUE" if "" in late else "")
                                if owners else "no owner attached" + (" OVERDUE" if late else ""))
            pending = [m for m in (o.get("milestones") or {}).values() if not m["receipt"]]
            if pending:
                m = min(pending, key=lambda m: (m["due"], m["key"]))
                state += f"; next milestone {m['key']} due {timefmt.long(m['due'], where)}"
                if now >= m["due"]:
                    state += " OVERDUE"
        if _open(o) and (why := _held_open(db, o)):
            state += f"; held open: {why}, acceptance not met"
        out.append(f"{o['id']}  {o['text'][:100]}  [{state}]")
    return out


def _held_open(db, o: dict) -> str | None:
    """Why `o`, every owner ended, is not closed by its owners finishing (unmet_handoff), for lines()."""
    ids = _owner_ids(o)
    if not ids or not _accepts(o):
        return None
    rows = db.q(f"SELECT status FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)
    if len(rows) < len(ids) or any(r["status"] not in CLOSED for r in rows):
        return None
    return unmet_handoff(db, ids)


def _owner_state(tid: str, own: dict, cover: tuple[str, str] | None, late: set, now: float) -> str:
    """One owner in lines(): 'done, no receipt' once it ended, 'waiting on ask <id>' while an open ask
    covers it, 'asleep on <what>' while it sleeps on a model-free wake, else its clock (and OVERDUE when late)."""
    if cover and cover[0] == "finished":
        return f"#{tid} {cover[1]}, no receipt"
    if cover and cover[0] == "asleep":
        return f"#{tid} asleep on {cover[1]}"
    if cover:
        return f"#{tid} waiting on ask {cover[1]}"
    return (f"#{tid} {(now - _last(own)) / 3600:.1f} h"
            + (f" (job result {(now - own['job']['at']) / 3600:.1f} h ago)" if own.get("job") else "")
            + (" OVERDUE" if tid in late else ""))


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
CLOCK_RE = re.compile(r"\b(?P<h>2[0-3]|[01]?\d)(?::(?P<m>[0-5]\d))?\s*(?P<ap>[ap]\.?m\b\.?)?(?:\s*(?P<utc>UTC|GMT)\b)?", re.I)
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


def authored(text: str) -> str:
    """`text` without the lines the harness appends to a coordinator message before it is posted (onhost's
    'Status now:' queue forecasts, wakemode's mode line): what the coordinator itself wrote. A queue
    forecast is the harness's, never the sender's own promise. One definition: a second one below once
    replaced this and let the mode line's ETA mint a promise."""
    from . import onhost, wakemode
    text = text or ""
    for head in (onhost.STATUS_HEAD, wakemode.HEAD):
        cut = text.find("\n" + head)
        text = text[:cut] if cut >= 0 else text
    return text


def commitments(text: str) -> list[str]:
    """The sentences of `text` that commit the sender to a deliverable or a time: first-person future with
    a delivery verb ("I'll report back") or a send verb and its object ("I'll post each result here"),
    "it goes up as the draft PR", first-person future with a time ("we'll have it by 15:00"), or
    an ETA with a time. Never a question or an offer ("if you want, I'll ..."), and never a line the
    harness appended (authored): a runtime footer's ETA is no promise anyone made."""
    out = []
    for s in _sentences(authored(text)):
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


def open_keys_by_task(db) -> dict[int, list[str]]:
    """Task id -> the open, undelivered obligation keys it owns (attached, or an order's or promise's
    task), in key order: the keys a task_update spec may name as its own (spec_check)."""
    out: dict[int, list[str]] = {}
    for o in items(db):
        if _open(o) and not o.get("delivered"):
            for t in _owner_ids(o):
                out.setdefault(t, []).append(o["id"])
    return out


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


NAMED_OPEN = ("queued", "running", "waiting", "blocked")   # a task a reply names that can still produce it
TASK_REF = re.compile(r"(?<![\w&/])#(\d+)\b")


def _named_owner(db, chat: str, text: str) -> int | None:
    """The one open task `text` names as #<id> whose reply_chat is `chat`; None for none or several."""
    ids = sorted({int(m) for m in TASK_REF.findall(text or "")})
    if not ids or chat == "all":
        return None
    rows = db.q(f"SELECT id, status, reply_chat FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)
    hits = [r["id"] for r in rows if r["status"] in NAMED_OPEN and r["reply_chat"] and _chat(r["reply_chat"]) == chat]
    return hits[0] if len(hits) == 1 else None


def record_promises(p, sent: list[tuple[int, str | None, str]], tasks: dict[int, str | None],
                    obligations: list[str] = (), now: float | None = None, explicit=(),
                    plans: dict[str, str] | None = None) -> list[str]:
    """Record the commitments in this batch's sent messages (message id, chat, text). `tasks`: the batch's
    tasks by id with their reply_chat; one replying to a promise's chat owns it, unless it was cancelled or
    the promise's words name others of them (#id): those own it. Only the sender's own words count (authored:
    never the harness's appended status footer). `obligations`: the ids the
    batch recorded or attached owners to; one owned by a task that owns the promise is linked to it, and
    the promise closes with it. `explicit`: the message ids of replies that named `owners` or `delivers`;
    any other promise no batch task owns is attached to the one open task its text names in that chat
    (_named_owner). Every promise is recorded. `plans`: {obligation id: plan change} of this batch
    (replan.batch_plans); a new time for a promise past its due with none is flagged (replan.flag_reply).
    Returns a note per promise."""
    now = time.time() if now is None else now
    sent = [(mid, chat, authored(text)) for mid, chat, text in sent]
    found = [(mid, _chat(chat), said) for mid, chat, text in sent if (said := commitments(text))]
    if not found:
        return []
    full = {mid: text for mid, _, text in sent}
    gone = {r["id"] for r in p.db.q(f"SELECT id FROM tasks WHERE status='cancelled' AND id IN "
                                    f"({','.join('?' * len(tasks))})", list(tasks))} if tasks else set()
    from . import replan, timefmt
    tz = timefmt.tzinfo(p)
    hours = default_h(p.config())
    plans = plans or {}
    notes = []
    with p.db.tx():
        st = _load(p.db)
        for mid, chat, said in found:
            text = " ".join(" ".join(said).split())[:PROMISE_TEXT_MAX]
            due = next((d for s in said if (d := promise_due(s, now, tz)) is not None), None)
            owners = sorted(t for t, c in tasks.items() if c is not None and _chat(c) == chat and t not in gone)
            cited = {int(m) for x in said for m in TASK_REF.findall(x)} & set(owners)
            owners = sorted(cited) or owners   # the batch task its own words name owns it, not every one
            named = None if owners or mid in explicit else _named_owner(p.db, chat, full.get(mid, ""))
            if named is not None:
                owners = [named]
            links = sorted({oid for oid in obligations if (o := st["items"].get(oid)) and not o.get("promise")
                            and set(o["owners"]) & {str(t) for t in owners}})
            same = next((o for o in st["items"].values() if _open(o) and o.get("promise")
                         and o["promise"]["chat"] == chat and o["text"] == text), None)
            if same:
                pr = same["promise"]
                if named is not None and pr["tasks"]:
                    owners = []   # it has owners already
                elif named is not None:
                    _auto_attach(p, same, named, mid, now)
                    notes.append(f"promise {same['id']}: #{named} attached as its owner (named in message #{mid})")
                pr["tasks"] = sorted(set(pr["tasks"]) | set(owners))
                pr["obligations"] = sorted(set(pr.get("obligations") or []) | set(links))
                if pr["message"] == mid:
                    continue   # the same turn applied again
                pr["message"] = mid
                notes += replan.flag_reply(p, st, chat, mid, due, owners, full.get(mid, ""), plans, now,
                                           skip=same["id"])
                if due is not None and due != pr["due"]:
                    if replan.missed(pr, now):
                        replan.note_miss(pr, now)
                        replan.note_move(pr, due, now, plans.get(same["id"]), None, f"reply #{mid}")
                        notes += [] if plans.get(same["id"]) else replan.flag(p, same, mid, now)
                    pr.update(due=due, timed=True, raised=None, set_at=now)   # set_at: when this due was promised
                notes.append(f"promise {same['id']} in chat {chat} updated from message #{mid}")
                continue
            notes += replan.flag_reply(p, st, chat, mid, due, owners, full.get(mid, ""), plans, now)
            oid = f"o{st['next']}"
            st["next"] += 1
            st["items"][oid] = {"id": oid, "text": text, "by": "coordinator", "created": now, "owners": {},
                                "receipt": None, "dropped": None, "episode": 0, "raised": None, "covered": [],
                                "promise": {"chat": chat, "message": mid, "due": due if due else now + hours * 3600,
                                            "timed": due is not None, "tasks": owners, "obligations": links,
                                            "raised": None}}
            if named is not None:
                _auto_attach(p, st["items"][oid], named, mid, now)
            notes.append(f"promise {oid} recorded from message #{mid}: due {timefmt.long(st['items'][oid]['promise']['due'], p)}"
                         + (f", owned by #{', #'.join(map(str, owners))}" if owners else ", no task owns it")
                         + (f" (attached: the reply names it and it replies to chat {chat})" if named is not None else "")
                         + (f", linked to {', '.join(links)}" if links else ""))
        p.db.set_kv(KV, st)
    return notes


def _auto_attach(p, o: dict, tid: int, mid: int, now: float) -> None:
    """Attach the task a promise's reply named (_named_owner) as a manual attach does, and keep it in the
    promise's history."""
    _attach(p, o, [tid], now)
    o.setdefault("history", []).append({"at": now, "event": "attached", "task": tid,
                                        "why": f"message #{mid} names #{tid}, an open task replying to chat "
                                               f"{o['promise']['chat']}"})


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
    if _accepts(o) and unmet_handoff(db, pr["tasks"]):   # an order's owners ended, its acceptance unmet
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
    if pr.get("claim"):
        return _check_claim(p, o, now, raised)
    fp = f"obligation:{o['id']}:promise"
    kept = _kept(p.db, o, st)
    if kept:
        o["receipt"] = {"at": now, "task": None, "evidence": kept}
        _retire(p.db, now, "promise kept", prefix=fp)
        return True
    stuck_changed = _check_landing_stuck(p, o, now, raised)
    if pr.get("order") is not None:
        return _check_order(p, o, now, raised) or stuck_changed
    if now < pr["due"] or pr.get("raised") is not None:
        return stuck_changed
    from . import replan, timefmt
    pr["raised"] = now
    twice = replan.note_miss(pr, now) >= 2
    owners = ", ".join(f"#{t}" for t in pr["tasks"]) or "no task"
    text = (f"Promise {o['id']} to the user is overdue (due {timefmt.long(pr['due'], p)}"
            + ("" if pr["timed"] else ", the default for a promise naming no time")
            + f"): in chat {pr['chat']}, message #{pr['message']}, you said: {o['text']}\n"
              f"Owner: {owners}. Send the deliverable or an honest update in that chat now, without being asked "
              f"(new ETA and why). Then close it: `obligation` with key {o['id']} and `evidence` (what was sent) "
              f"or `why` (no longer owed). A new promise in the update is tracked anew.")
    text += replan.REPLAN_TEXT.format(key=o["id"]) if twice else ""
    fp += f":{int(pr['due'])}"   # a promise given a new due time can fall due once more
    if _raise(p.db, now, fp, PROMISE_KIND, text, pr["tasks"][0] if pr["tasks"] else None, _must(o, twice)):
        raised.append(fp)
    return True


def _landing_stuck(p, o: dict, now: float) -> tuple[str, str, int | None] | None:
    """(fingerprint, text, gated task) of the first owner of open promise or order `o` that waits on a landing
    nothing can make (LANDING_STUCK_KIND), past the grace; None when none. Saved rows only. The fingerprint
    sits under `obligation:<id>:promise`, so a kept, closed or dropped promise retires it."""
    from . import deadwait, eta, landed
    for tid in _owner_ids(o):
        t = p.db.task(tid)
        if not t or t["status"] in CLOSED or t["status"] == "running":
            continue
        for n in eta.landing_waits(t):
            g = p.db.task(n)
            if not g or g["status"] != "done" or landed.abandoned(p.db, n) or landed.recorded(p.db, n)[0]:
                continue   # open work lands it, abandoned has its own notice, or it landed
            if not (stuck := deadwait.stuck_by(p.db, n)):
                continue
            try:
                grace = float((p.config().get("waiting") or {}).get("landed_wait_dead_s", deadwait.GRACE_S) or 0)
            except Exception:
                grace = float(deadwait.GRACE_S)
            if now - stuck["at"] < grace:
                continue   # a fix or re-review the same step queues gets its chance
            held = {w["id"] for w in deadwait.waiters(p.db, {n})} | {tid}
            if deadwait.path(p.db, n, held):
                continue
            ep = f"r{stuck['review']}" if stuck["row"] is None else f"q{stuck['row']}"
            kind = "Order" if o["promise"].get("order") is not None else "Promise"
            text = (f"{kind} {o['id']} cannot finish: its owner #{tid} waits on landed:#{n}, but #{n} {g['title'][:100]} "
                    f"is done and not landed, {stuck['what']}, and no open review, fix, rebase or push carries it "
                    f"on. Its ETA reads 'after landed:#{n}'. Act now: queue the fix and re-review (or the push) of "
                    f"#{n}, re-point or clear #{tid}'s wait, or tell the user in chat {o['promise']['chat']} and "
                    f"re-date or drop {o['id']} (`obligation` with key {o['id']}).")
            return f"obligation:{o['id']}:promise:landing_stuck:{n}:{ep}", text, n
    return None


def _check_landing_stuck(p, o: dict, now: float, raised: list[str]) -> bool:
    """One must-act event per episode for an open promise or order stuck behind a landing nothing can make
    (_landing_stuck), retired once that clears. Returns whether `o` changed."""
    got = _landing_stuck(p, o, now)
    was = o.get("landing_stuck")
    if got and got[0] == was:
        return False
    if was:
        _retire(p.db, now, "landing path back", was)
    o["landing_stuck"] = got[0] if got else None
    if got and _raise(p.db, now, got[0], LANDING_STUCK_KIND, got[1], got[2], _must(o)):
        raised.append(got[0])
    return bool(got or was)


def _promise_state(o: dict, now: float, where) -> str:
    from . import timefmt
    pr = o["promise"]
    late = now >= pr["due"]
    ids = _owner_ids(o)
    return (f"chat {pr['chat']}, message #{pr['message']}, "
            + (f"owner #{', #'.join(map(str, ids))}" if ids else "no owner task")
            + f"; due {timefmt.long(pr['due'], where)}" + ("" if pr["timed"] else " (default)")
            + (f" OVERDUE {(now - pr['due']) / 3600:.1f} h" if late else ""))


def open_promises(db) -> list[dict]:
    """The open promises and orders. Claims are promise-shaped too but left out: no work is owed on one,
    the daemon's check_claims judges it model-free and its miss raises claim_failed."""
    return [o for o in items(db) if (pr := o.get("promise")) and not pr.get("claim") and _open(o)]


def promise_lines(db, now: float | None = None, where=None) -> list[str]:
    """The digest's section: every open promise, overdue ones first."""
    now = time.time() if now is None else now
    rows = sorted(open_promises(db), key=lambda o: o["promise"]["due"])
    if not rows:
        return []
    late = sum(1 for o in rows if now >= o["promise"]["due"])
    out = [f"## Promises made in chat: {len(rows)} open" + (f", {late} OVERDUE" if late else "")
           + " (send each deliverable in its chat; an honest update waits for its digest; close with `obligation` key + evidence)"]
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
    due = [o for o in items(db) if o.get("promise") and not o["promise"].get("claim")
           and since <= float(o["promise"]["due"]) <= now]
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


def _accepts(o: dict) -> bool:
    """Whether `o` owes a result its owners' hand-offs must meet: an obligation or an order, not a plain
    promise or claim (a promise to report is kept by the report)."""
    pr = o.get("promise")
    return not pr or pr.get("order") is not None


# An owner's checked hand-off that leaves its acceptance unmet keeps the obligation open: its owners
# finishing is no receipt (unmet_handoff). It counts for a task that ended (or is blocked) with a hand-off
# whose status is one of UNMET_STATUSES, or whose summary starts with the cells mark (cells.marked:
# 'partial:' or 'cells unreported:'), or that failed. One excused: another owner that ended met carries
# its work on (_carried: a continue, fix or re-review hop). Prose is not read for it (no keyword list):
# a worker says so by its hand-off's status or a 'partial:' start. Fails open: an extra overdue wake costs a
# coordinator turn, a false close loses the order.
UNMET_STATUSES = ("blocked", "failed", "waiting")


def _handoff_unmet(t) -> str | None:
    """Why task row `t`'s own hand-off leaves its acceptance unmet, or None."""
    from . import cells
    from .db import load_result
    if t["status"] not in CLOSED + ("blocked",):
        return None
    res = load_result(t["result"])
    hs, summary = str(res.get("status") or ""), " ".join(str(res.get("summary") or "").split())
    if cells.marked(summary):
        return f"#{t['id']}'s hand-off is marked {summary[:120]!r}"
    if t["status"] == "failed" or hs in UNMET_STATUSES:
        return f"#{t['id']} ended {t['status']} with a {hs or 'no'} hand-off" + (f": {summary[:120]!r}" if summary else "")
    return None


def unmet_handoff(db, ids, only=None) -> str | None:
    """Why the owners `ids` leave the acceptance unmet: the first one (of `only`, when given) whose checked
    hand-off does (_handoff_unmet) and that no other owner, ended met, carries on; None when none does."""
    ids = sorted({int(t) for t in ids or []} | set(only or []))
    if not ids:
        return None
    rows = db.q(f"SELECT id, status, result, title, labels, parent FROM tasks "
                f"WHERE id IN ({','.join('?' * len(ids))})", ids)
    why = {int(r["id"]): _handoff_unmet(r) for r in rows}
    carried = {c for r in rows if r["status"] == "done" and not why[int(r["id"])]
               for c, how in _carried(r).items() if how != "reviews"}   # a review passing meets nothing
    return next((why[t] for t in sorted(why) if why[t] and str(t) not in carried
                 and (only is None or t in only)), None)


def live_owner_ids(db, o: dict) -> list[int]:
    """The owners of `o` that can still deliver it, for judging its progress (ETA, urgency); the stored
    owner list (_owner_ids) keeps every one as its history. Left out: an owner that is gone, cancelled,
    abandoned or superseded (`ttp task abandon`/`superseded`). A failed or cancelled owner whose work a
    continuation carries on (_continuations, not itself failed or cancelled) stands for that continuation;
    a failed one with none stays, so its estimate names the failure."""
    from .landed import abandoned, superseded
    ids = _owner_ids(o)
    rows = {t["id"]: t for t in db.q(f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(ids))})",
                                     ids)} if ids else {}
    replaced = superseded(db, ids)
    out: set[int] = set()
    for tid in ids:
        t = rows.get(tid)
        if t is None or tid in replaced or abandoned(db, tid):
            continue
        if t["status"] in ("failed", "cancelled"):
            if carry := [r["id"] for r in _continuations(db, tid) if r["status"] not in ("failed", "cancelled")]:
                out.update(carry)
            elif t["status"] == "failed":
                out.add(tid)
            continue
        out.add(tid)
    return sorted(out)


def _delivered(db, o: dict) -> str | None:
    """Evidence that a coordinator reply delivered `o` (its `delivers` named it) and every owner finished,
    none leaving the acceptance unmet (unmet_handoff; a plain promise's report is its deliverable)."""
    dv = o.get("delivered")
    if not dv or not _open(o):
        return None
    ids = _owner_ids(o)
    if ids:
        rows = db.q(f"SELECT status FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)
        if len(rows) < len(ids) or any(r["status"] not in CLOSED for r in rows):
            return None
        if _accepts(o) and unmet_handoff(db, ids):
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
                    elif _accepts(o) and (why := unmet_handoff(p.db, _owner_ids(o))):
                        notes.append(f"{o['id']} not closed by reply #{mid}: {why}, so the acceptance is not met; "
                                     f"it stays owed until an owner meets it (or `obligation` with `why` drops it)")
                    else:
                        notes.append(f"{o['id']} delivered by reply #{mid}; it closes once "
                                     f"#{', #'.join(map(str, _owner_ids(o)))} finish")
        p.db.set_kv(KV, st)
    return notes


# orders and claims --------------------------------------------------------------------------------
# An order (add_order): a user message that asks for something, recorded by the coordinator's
# `obligation` action with `order` (the message id), `action_class` (read, change or device), optional
# `due` (else promise_default_h after it is recorded) and `start_by` (else ORDER_START_S after the
# message), and its owner tasks. It is promise-shaped (the `promise` field with `order`), so it has no
# owner clocks and closes like a promise (_kept). check() raises one high `order_late` event when no
# owner run has started by its start_by, and one high `order_overdue` event at its due time.
# A claim (record_claims): a coordinator reply or notify saying something is done, live, running or
# landed can carry `claims`, each `{text, task | action | expect, due}`. A `task` (or `action`: the index
# of a task_add of the same batch) holds once it ends done; an `expect` probe (start_when rules:
# read-only, under a minute) holds once it exits 0. It runs model-free (the daemon's check_claims) and
# is judged at its due: exit 1 is "not yet", 75 or a timeout keep waiting up to CLAIM_WAIT_S more; 255
# (ssh could not reach the probe's host, as while it reboots) is "could not check": retried, never a miss.
# A miss raises one high `claim_failed` event. Overdue promises and orders, late orders and failed
# claims carry `must_act` in their event data: the coordinator wake they start must act on each
# (mustact.py).
# A status claim ("#12 is queued", "#12 has started", `state` set or read from such a line, see
# claim_state) observes a task's status now: check_claims holds it against the task record at send
# time and record_claims does not track it as a promise that the task ends done.
ORDER_LATE_KIND = "order_late"
ORDER_KIND = "order_overdue"
CLAIM_KIND = "claim_failed"
# An open promise or order whose owner waits on a landing (eta.landing_waits) that nothing can make:
# the gated task is done, not landed, its last review failed or was cancelled (or its push row failed:
# deadwait.stuck_by) and no open task carries it on (deadwait.path). One high event per episode (the
# gate and the failed review or row), after waiting.landed_wait_dead_s; it is retired once a path appears.
LANDING_STUCK_KIND = "order_landing_stuck"
MUST_ACT_KINDS = (PROMISE_KIND, ORDER_LATE_KIND, ORDER_KIND, CLAIM_KIND, LANDING_STUCK_KIND)
ORDER_CLASSES = ("read", "change", "device")
ORDER_START_S = 1800          # an order with no start_by: its owner's run starts within this after the message
CLAIM_WAIT_S = 1800           # a claim's probe that cannot answer (75, timeout) past its due: failed after this
CLAIM_UNREACHABLE = 255       # ssh's own exit: the probe's host did not answer; the claim stays unchecked and is retried
CLAIM_HOPS_MAX = 50           # continuation hops a task claim's check walks from its named task (_continuations)
CLAIM_WAITING = (75, 255, "timeout", None)
# A change or device order is owned by a task that can make the change or run the job: a question,
# plan or review task is refused as its owner, model-free (unfit_owner), when the coordinator names it.
# Owners the daemon attaches itself (attach_carriers: a review carrying an owner's work on) are hops, not
# this choice, and are not checked.
UNFIT_OWNER_KINDS = ("question", "plan", "review")
# A status claim's `state` and the task statuses that make it true at send time ("started": a run began).
CLAIM_STATES = {"queued": ("queued",), "running": ("running",), "waiting": ("waiting",),
                "started": ("running", "waiting", "blocked", "review", "done", "failed")}
_OBSERVED = re.compile(r"\b(?:is|are|has|have|was|were|been|now|got)\s+(?:(?:still|now|already|just|been)\s+){0,2}"
                       r"(queued|started|running|waiting)\b|\b(?:in|on) the queue\b", re.I)
_OUTCOME = re.compile(r"\b(?:done|land(?:s|ed|ing)?|push(?:es|ed)?|finish(?:es|ed)?|complete[sd]?|merged?|fixed|"
                      r"ready|live|deliver(?:s|ed)?|report(?:s|ed)?|result)\b", re.I)
ORDER_AUDIT_DAYS = 7


def _must(o: dict, replan: bool = False) -> dict:
    """An overdue item's must-act data; `replan`: its second miss or later (replan.py)."""
    return {"must_act": {"key": o["id"], "chat": o["promise"]["chat"], "tasks": _owner_ids(o),
                         **({"replan": True} if replan else {})}}


def _when(value, what: str) -> float:
    try:
        return _due(value)
    except ValueError as e:
        raise ValueError(f"{what}: {str(e).replace('milestone due', 'a time')}") from e


def _hours(p) -> float:
    try:
        return default_h(p.config())
    except Exception:   # an unreadable config keeps the default
        return PROMISE_DEFAULT_H


def unfit_owner(db, cls: str | None, tids, kind: str | None = None) -> str | None:
    """Why tasks `tids` cannot own an order of class `cls`, or None when they fit. `kind`: the kind of a
    task not added yet (a task_add naming the order), checked instead of reading `tids`."""
    if cls not in ("change", "device"):
        return None
    pairs = [("this task_add", kind)] if kind is not None else \
        [(f"#{t}", (db.task(int(t)) or {}).get("kind")) for t in tids or []]
    for who, k in pairs:
        if k in UNFIT_OWNER_KINDS:
            return (f"{who} is a {k} task, which cannot carry a {cls} order: own it with a task that makes the "
                    f"change or runs the job (code, harness or work)")
    return None


def _owners_known(db, tids) -> list[int]:
    ids = sorted({int(t) for t in tids or []})
    for tid in ids:
        if not db.task(tid):
            raise ValueError(f"no task #{tid}")
    return ids


def open_order(db, message: int) -> dict | None:
    """The open obligation recorded for the order user message `message` carries, if any."""
    return next((x for x in _load(db)["items"].values()
                 if _open(x) and (x.get("promise") or {}).get("order") == int(message)), None)


def add_order(p, text: str, message: int, owners=(), due=None, start_by=None, action_class: str | None = None,
              chat: str | None = None, now: float | None = None, reason: str | None = None) -> str:
    """Record the order user message `message` carries, or update the open one recorded for it. Returns
    a note. `reason` (why the user gave it) and the message's own words are kept with it, so an old
    order is re-read by its intent once that reason is gone (oldorders.py)."""
    from . import oldorders, replan, timefmt
    now = time.time() if now is None else now
    text = " ".join(str(text or "").split())[:TEXT_MAX]
    if not text:
        raise ValueError("an order needs `text`: what the user asked for")
    if action_class not in ORDER_CLASSES:
        raise ValueError(f"an order needs `action_class`: one of {', '.join(ORDER_CLASSES)}")
    row = p.db.one("SELECT ts, chat FROM messages WHERE id=? AND direction='in'", (int(message),))
    if not row:
        raise ValueError(f"order: #{message} is not a user message")
    due_at = _when(due, "order due") if due not in (None, "") else None
    start_at = _when(start_by, "order start_by") if start_by not in (None, "") else float(row["ts"]) + ORDER_START_S
    with p.db.tx():
        ids = _owners_known(p.db, owners)
        st = _load(p.db)
        o = next((x for x in st["items"].values() if _open(x) and (x.get("promise") or {}).get("order") == int(message)), None)
        if why := unfit_owner(p.db, action_class, sorted(set(ids) | set((o or {}).get("promise", {}).get("tasks") or []))):
            raise ValueError(f"order (message #{message}): {why}")
        if o:
            pr = o["promise"]
            if due_at is not None and due_at != pr["due"] and replan.missed(pr, now):
                new = sorted(set(ids) - set(_owner_ids(o)))   # a new owner is a plan change (replan.py)
                if not new:
                    raise ValueError(replan.refusal(o, timefmt.long(pr["due"], p)))
                replan.note_miss(pr, now)
                replan.note_move(pr, due_at, now, f"new owner #{', #'.join(map(str, new))}", None, "order")
            pr["tasks"] = sorted(set(pr["tasks"]) | set(ids))
            pr["class"] = action_class
            if oldorders.one_line(reason):
                pr["reason"] = oldorders.one_line(reason)
            if due_at is not None and due_at != pr["due"]:
                pr.update(due=due_at, timed=True, raised=None, set_at=now)
            if start_by not in (None, "") and start_at != pr["start_by"]:
                pr.update(start_by=start_at, late=None)
            note = f"order {o['id']} (message #{message}) updated"
        else:
            oid = f"o{st['next']}"
            st["next"] += 1
            o = st["items"][oid] = {
                "id": oid, "text": text, "by": "coordinator", "created": now, "owners": {}, "receipt": None,
                "dropped": None, "episode": 0, "raised": None, "covered": [],
                "promise": {"chat": _chat(chat or row["chat"]), "message": int(message),
                            "due": due_at if due_at is not None else now + _hours(p) * 3600,
                            "timed": due_at is not None, "tasks": ids, "obligations": [], "raised": None,
                            "set_at": now, "order": int(message), "class": action_class, "start_by": start_at,
                            "started": None, "late": None, "reason": oldorders.one_line(reason),
                            "context": oldorders.clean_context(p, oldorders.context_for(p.db, "", int(message)))}}
            note = f"order {oid} recorded from message #{message}"
        pr = o["promise"]
        p.db.set_kv(KV, st)
    return (note + f": due {timefmt.long(pr['due'], p)}, start by {timefmt.long(pr['start_by'], p)}, "
            + (f"owned by #{', #'.join(map(str, pr['tasks']))}" if pr["tasks"] else "no owner task yet"))


def link_order(p, oid: str, message: int) -> str:
    """Record that open obligation `oid` already stands for the order user message `message` carries
    (order_messages). Returns a note."""
    if not p.db.one("SELECT 1 FROM messages WHERE id=? AND direction='in'", (int(message),)):
        raise ValueError(f"order: #{message} is not a user message")
    with p.db.tx():
        st = _load(p.db)
        o = _need(st, oid)
        if not _open(o):
            raise ValueError(f"obligation {oid} is closed: record the order as a new one (`order` with no `key`)")
        o["orders"] = sorted(set(o.get("orders") or []) | {int(message)})[-50:]
        p.db.set_kv(KV, st)
    return f"obligation {oid} linked to the order of message #{message}"


def order_messages(db) -> set[int]:
    """The user messages whose order the ledger holds: recorded (add_order) or linked (link_order)."""
    out: set[int] = set()
    for o in items(db):
        if (o.get("promise") or {}).get("order") is not None:
            out.add(int(o["promise"]["order"]))
        out |= {int(m) for m in o.get("orders") or []}
    return out


def order_starts(db, since: float, until: float, now: float | None = None) -> tuple[int, int]:
    """(orders whose message came between `since` and `until`, those whose first owner run started more
    than ORDER_START_S after the message or not at all by `now`)."""
    now = time.time() if now is None else now
    orders = {int(o["promise"]["order"]): o["promise"] for o in items(db) if (o.get("promise") or {}).get("order") is not None}
    if not orders:
        return 0, 0
    ids = list(orders)
    ts = {r["id"]: float(r["ts"]) for r in db.q(f"SELECT id, ts FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids)}
    n = late = 0
    for m, pr in orders.items():
        at = ts.get(m)
        if at is None or not since <= at < until or (pr.get("started") is None and now - at <= ORDER_START_S):
            continue   # out of the window, or too new to say
        n += 1
        late += (float(pr["started"]) if pr.get("started") is not None else now) - at > ORDER_START_S
    return n, late


def order_audit_line(db, now: float | None = None) -> str:
    """The daily audit's line: user orders unstarted past ORDER_START_S over the last ORDER_AUDIT_DAYS days,
    and the days before them (before and after a change); '' with no order in either window."""
    now = time.time() if now is None else now
    span = ORDER_AUDIT_DAYS * 86400
    n, late = order_starts(db, now - span, now, now)
    n0, late0 = order_starts(db, now - 2 * span, now - span, now)
    if not n and not n0:
        return ""
    return (f"user orders unstarted past {ORDER_START_S // 60} min: {late} of {n} in the last {ORDER_AUDIT_DAYS} days"
            f" ({late0} of {n0} in the {ORDER_AUDIT_DAYS} days before)")


def _order_line(o: dict) -> str:
    pr = o["promise"]
    ids = _owner_ids(o)
    return (f"Order {o['id']} from the user (message #{pr['order']}, chat {pr['chat']}, {pr.get('class')}): "
            f"{o['text']}\nOwner: " + (f"#{', #'.join(map(str, ids))}" if ids else "no task"))


def _check_order(p, o: dict, now: float, raised: list[str]) -> bool:
    """An open order's start and due checks (after _kept found it not done). Returns whether `o` changed."""
    from . import replan, timefmt
    pr, db, changed = o["promise"], p.db, False
    ids = _owner_ids(o)
    if pr.get("started") is None and ids:
        q = ",".join("?" * len(ids))
        if db.one(f"SELECT id FROM runs WHERE task IN ({q}) LIMIT 1", ids):
            pr["started"], changed = now, True
            _retire(db, now, "order started", prefix=f"obligation:{o['id']}:order:late")
    if pr.get("started") is None and now >= pr["start_by"] and pr.get("late") is None:
        pr["late"], changed = now, True
        fp = f"obligation:{o['id']}:order:late:{int(pr['start_by'])}"
        text = (_order_line(o) + f"\nNo owner run started by {timefmt.long(pr['start_by'], p)}. Start it now "
                f"(`task_update` with `wake_now` on its owner, or `task_add` naming {o['id']}; `obligation` with key "
                f"{o['id']} and `owners` attaches it), or tell the user in that chat why it waits and until when.")
        if _raise(db, now, fp, ORDER_LATE_KIND, text, ids[0] if ids else None, _must(o)):
            raised.append(fp)
    if now >= pr["due"] and pr.get("raised") is None:
        pr["raised"], changed = now, True
        twice = replan.note_miss(pr, now) >= 2
        fp = f"obligation:{o['id']}:order:{int(pr['due'])}"
        text = (_order_line(o) + f"\nIt is overdue (due {timefmt.long(pr['due'], p)}"
                + ("" if pr["timed"] else ", the default for an order naming no time")
                + f"). Send the result or an honest update in that chat now, without being asked (new time and "
                  f"why), re-order it or escalate. Then close it: `obligation` with key {o['id']} and `evidence` "
                  f"(what was delivered) or `why` (no longer owed); `due` with key {o['id']} sets a new time.")
        text += replan.REPLAN_TEXT.format(key=o["id"]) if twice else ""
        if _raise(db, now, fp, ORDER_KIND, text, ids[0] if ids else None, _must(o, twice)):
            raised.append(fp)
    return changed


def claim_state(c: dict) -> str | None:
    """The status a task-backed claim observes (a key of CLAIM_STATES), else None: its `state`, or a
    claim with no `due` whose text only says the task is queued, started, running or waiting (no outcome,
    no future or time). A claim that also names an outcome stays a claim that the task ends done."""
    if c.get("task") in (None, ""):
        return None
    if c.get("state") not in (None, ""):
        return str(c["state"])
    text = " ".join(str(c.get("text") or "").split())
    if c.get("due") not in (None, "") or _OUTCOME.search(text) or FUTURE_RE.search(text) or _has_time(text):
        return None
    seen = {(m.group(1) or "queued").lower() for m in _OBSERVED.finditer(text)}
    return seen.pop() if len(seen) == 1 else None


def check_claims(claims, p, new_tasks=None) -> None:
    """Static checks of a reply's or notify's `claims` before it is sent; raises on the first bad one.
    `new_tasks`: the action indexes of this batch's task_add actions, when known."""
    from .coordinator import check_start_when
    if not isinstance(claims, list):
        raise ValueError("`claims` is a list of {text, task | action | expect, due}")
    for c in claims:
        if not isinstance(c, dict) or not " ".join(str(c.get("text") or "").split()):
            raise ValueError("each claim needs `text`: what you told the user holds")
        backing = [k for k in ("task", "action", "expect") if c.get(k) not in (None, "")]
        if len(backing) != 1:
            raise ValueError("each claim needs exactly one backing: `task` (its id), `action` (the index of a "
                             "task_add in this batch) or `expect` (a read-only probe, exit 0 = it holds)")
        if c.get("task") not in (None, "") and not p.db.task(int(c["task"])):
            raise ValueError(f"claim: no task #{c['task']}")
        if c.get("action") not in (None, "") and new_tasks is not None and int(c["action"]) not in new_tasks:
            raise ValueError(f"claim: action {c['action']} is not a task_add of this batch")
        if c.get("state") not in (None, "") and (c["state"] not in CLAIM_STATES or c.get("task") in (None, "")):
            raise ValueError(f"claim `state` is one of {', '.join(CLAIM_STATES)}, with `task`: what its record "
                             f"shows now")
        if state := claim_state(c):
            t = p.db.task(int(c["task"]))
            if t["status"] not in CLAIM_STATES[state]:
                raise ValueError(f"claim: #{c['task']} is {t['status']} in its record, not {state} "
                                 f"({str(c['text'])[:80]}): state its status from the record")
        if c.get("expect") not in (None, ""):
            check_start_when(str(c["expect"]), p)
        if c.get("due") not in (None, ""):
            _when(c["due"], "claim due")


def record_claims(p, sent: list[tuple[int, str | None, list]], new_tasks: dict[int, int],
                  now: float | None = None) -> list[str]:
    """Track the claims the batch's replies and notifies carried: (message id, chat, claims) each.
    `new_tasks`: action index -> the task its task_add created. Returns a note per claim."""
    now = time.time() if now is None else now
    notes: list[str] = []
    todo = []
    for mid, chat, claims in sent:
        for c in claims or []:
            if state := claim_state(c):   # a status checked at send time (check_claims), not a promise
                notes.append(f"claim not tracked: #{c['task']} is {state} at send time, a status, not that it "
                             f"ends done ({str(c['text'])[:80]})")
                continue
            task = int(c["task"]) if c.get("task") not in (None, "") else None
            if c.get("action") not in (None, ""):
                task = new_tasks.get(int(c["action"]))
                if task is None:
                    notes.append(f"claim not tracked: action {c['action']} added no task ({str(c['text'])[:80]})")
                    continue
            expect = str(c["expect"]) if c.get("expect") not in (None, "") else None
            due = _when(c["due"], "claim due") if c.get("due") not in (None, "") else \
                now if expect else now + _hours(p) * 3600
            todo.append((mid, _chat(chat), " ".join(str(c["text"]).split())[:TEXT_MAX], task, expect, due,
                         c.get("due") not in (None, "")))
    if not todo:
        return notes
    with p.db.tx():
        st = _load(p.db)
        for mid, chat, text, task, expect, due, timed in todo:
            if any(_open(x) and (x.get("promise") or {}).get("claim") and x["promise"]["message"] == mid
                   and x["text"] == text for x in st["items"].values()):
                continue   # the same turn applied again
            oid = f"o{st['next']}"
            st["next"] += 1
            st["items"][oid] = {
                "id": oid, "text": text, "by": "coordinator", "created": now, "owners": {}, "receipt": None,
                "dropped": None, "episode": 0, "raised": None, "covered": [],
                "promise": {"chat": chat, "message": mid, "due": due, "timed": timed,
                            "tasks": [task] if task else [], "obligations": [], "raised": None, "set_at": now,
                            "claim": {"task": task, "expect": expect, "last": None, "at": None}}}
            notes.append(f"claim {oid} from message #{mid} tracked: " + (f"#{task} ends done" if task else
                                                                         f"`{expect[:80]}` exits 0"))
        p.db.set_kv(KV, st)
    return notes


def _continuations(db, tid: int) -> list[dict]:
    """The tasks that carry task `tid` on, hop by hop (chaincap.prev_of: a `continues:` or `follows:`
    label, a `continue:` follow-up of its parent), in walk order: each once, at most CLAIM_HOPS_MAX."""
    from .chaincap import prev_of
    nxt: dict[int, list[dict]] = {}
    for r in db.q("SELECT id, status, title, labels, parent FROM tasks WHERE labels LIKE '%continues:%' "
                  "OR labels LIKE '%follows:%' OR (parent IS NOT NULL AND (title LIKE '%continue%:%' "
                  "OR title LIKE '%mechanical%:%')) ORDER BY id"):
        if (prev := prev_of(r)) is not None:
            nxt.setdefault(prev, []).append(r)
    out, seen, todo = [], {tid}, [tid]
    while todo and len(out) < CLAIM_HOPS_MAX:
        for r in nxt.get(todo.pop(0), []):
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(r)
                todo.append(r["id"])
    return out[:CLAIM_HOPS_MAX]


def _check_claim(p, o: dict, now: float, raised: list[str]) -> bool:
    """Close a claim that holds, or raise its one claim_failed event. Returns whether `o` changed. A named
    task that failed, was cancelled or is gone still holds through a task that continues it
    (_continuations) and ended done, and waits for its due time while one is open."""
    from . import replan, timefmt
    pr = o["promise"]
    c, db, fp = pr["claim"], p.db, f"obligation:{o['id']}:claim"   # + its due: a new due can fail once more
    if c.get("task"):
        t = db.task(int(c["task"]))
        status = t["status"] if t else "gone"
        if status == "done":
            o["receipt"] = {"at": now, "task": int(c["task"]), "evidence": f"#{c['task']} ended done"}
            _retire(db, now, "claim held", prefix=fp)
            return True
        if status == "superseded":   # a later passing review covers it: closed, held through that one
            from .db import SUPERSEDED_KEY, load_result
            by = (load_result(t["result"]).get(SUPERSEDED_KEY) or {}).get("by_review")
            o["receipt"] = {"at": now, "task": int(c["task"]), "evidence": f"#{c['task']} was superseded"
                            + (f" by review #{by}, which passed" if by else " by a later passing review")}
            _retire(db, now, "claim held", prefix=fp)
            return True
        if status in ("failed", "cancelled", "gone"):
            hops = _continuations(db, int(c["task"]))
            if done := next((h for h in hops if h["status"] == "done"), None):
                o["receipt"] = {"at": now, "task": done["id"], "evidence": f"#{done['id']}, which continues "
                                f"#{c['task']} ({status}), ended done"}
                _retire(db, now, "claim held", prefix=fp)
                return True
            if live := [h for h in hops if h["status"] not in CLOSED]:
                if now < pr["due"]:
                    return False
                miss = (f"#{c['task']} is {status} and its continuation #{live[0]['id']} is still "
                        f"{live[0]['status']} at its due time")
            else:
                miss = f"#{c['task']} is {status}" + (
                    f", and so is each task that continues it (#{', #'.join(str(h['id']) for h in hops)})"
                    if hops else "")
        elif now >= pr["due"]:
            miss = f"#{c['task']} is still {status} at its due time"
        else:
            return False
    else:
        if c.get("last") == 0:
            o["receipt"] = {"at": now, "task": None, "evidence": f"`{c['expect'][:120]}` exited 0"}
            _retire(db, now, "claim held", prefix=fp)
            return True
        answered = c.get("at") is not None and c["at"] >= pr["due"]
        if c.get("last") == CLAIM_UNREACHABLE:
            return False   # could not check: the next due_claims pass runs it again
        if now < pr["due"] or (not answered or c.get("last") in CLAIM_WAITING) and now < pr["due"] + CLAIM_WAIT_S:
            return False
        miss = (f"its check `{c['expect'][:200]}` exited {c['last']}" if answered and c["last"] not in CLAIM_WAITING
                else f"its check `{c['expect'][:200]}` could not answer ({c.get('last')}) for "
                     f"{CLAIM_WAIT_S // 60} min after its due time")
    if pr.get("raised") is not None:
        return False
    pr["raised"] = now
    twice = now >= pr["due"] and replan.note_miss(pr, now) >= 2
    text = (f"Claim {o['id']} you told the user (chat {pr['chat']}, message #{pr['message']}, due "
            f"{timefmt.long(pr['due'], p)}) does not hold: {miss}. You said: {o['text']}\n"
            f"Correct it in that chat now (what is true, what is being done, by when), and fix or re-order the "
            f"work. Then close it: `obligation` with key {o['id']} and `evidence` or `why`.")
    text += replan.REPLAN_TEXT.format(key=o["id"]) if twice else ""
    fp += f":{int(pr['due'])}"
    if _raise(db, now, fp, CLAIM_KIND, text, int(c["task"]) if c.get("task") else None, _must(o, twice)):
        raised.append(fp)
    return True


def due_claims(db, now: float | None = None) -> list[tuple[str, str, str]]:
    """(id, probe, cache key) of the open claims whose expect probe the daemon runs now: from their due
    time on, until they hold or fail. The key names the claim and its due, so a run of the same probe
    by another caller (a probe stall's re-check, another claim) or for an earlier due is never taken
    as this claim's answer."""
    now = time.time() if now is None else now
    return [(o["id"], o["promise"]["claim"]["expect"], f"claim:{o['id']}:{int(o['promise']['due'])}") for o in items(db)
            if _open(o) and (pr := o.get("promise") or {}).get("claim") and pr["claim"].get("expect")
            and pr.get("raised") is None and now >= pr["due"]]


def claim_result(p, oid: str, rc, now: float | None = None) -> list[str]:
    """Record a run of claim `oid`'s probe (its exit code, "timeout" or "could not start") and judge it at
    once. Returns the fingerprints raised."""
    now = time.time() if now is None else now
    raised: list[str] = []
    with p.db.tx():
        st = _load(p.db)
        o = st["items"].get(oid)
        if not o or not _open(o) or not (o.get("promise") or {}).get("claim"):
            return raised
        o["promise"]["claim"].update(last=rc, at=now)
        _check_claim(p, o, now, raised)
        p.db.set_kv(KV, st)
    return raised


def set_due(p, oid: str, due, now: float | None = None, plan: str | None = None, user: int | None = None) -> str:
    """Give an open order, promise or claim a new due time: it can fall due once more. Past its due, the
    move needs `plan` (the batch's plan change, replan.plan_change) or `user` (the user's message asking
    for it); else it is refused (replan.py)."""
    from . import replan, timefmt
    now = time.time() if now is None else now
    at = _when(due, f"{oid} due")
    with p.db.tx():
        st = _load(p.db)
        o = _need(st, oid)
        if _open(o) and not o.get("promise"):   # still owed: retrying the move cannot help, closing can
            raise ValueError(f"{oid} is open but has no promise or due to move: close it with evidence "
                             f"(receipt) or why (drop)")
        if not o.get("promise") or not _open(o):
            raise ValueError(f"{oid} is not an open order, promise or claim")
        pr = o["promise"]
        late = replan.missed(pr, now)
        refused = late and not plan and user is None
        if late:
            replan.note_miss(pr, now)   # kept on a refusal too
        if not refused:
            replan.note_move(pr, at, now, plan, user, "due")
            pr.update(due=at, timed=True, raised=None, set_at=now)
        p.db.set_kv(KV, st)
    if refused:
        raise ValueError(replan.refusal(o, timefmt.long(pr["due"], p)))
    why = f"plan change: {plan}" if plan else f"the user asked in message #{user}" if user is not None else ""
    return f"{oid} now due {timefmt.long(at, p)}" + (f" ({why})" if late and why else "")
