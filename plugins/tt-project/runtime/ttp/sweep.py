# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Blocker sweep: a regular tricky-turn re-check of everything that is not moving.

Every coordinator.blocker_sweep_h hours (default 6; 0 off), and sooner on a signal, the daemon queues
one SWEEP_KIND event while anything is stuck. That event makes the coordinator's turn a tricky
one (coordinator.EFFORT_EVENT_TRIGGERS) and adds a 'Blocker sweep' section to its digest: every
blocked, waiting, in-review, setting-held and deferred task and every paused resource, with its age,
its reason and its last re-check. The coordinator records exactly one outcome per item with the
`sweep` action (OUTCOMES). `still_blocked` needs a re-checkable condition (`until_probe`, or an
`until` that is not only "keep it blocked") and a next check (`start_after`); `human_indispensable`
needs a `why`. An item the next sweep finds without an outcome since the last one, or whose
`still_blocked` had no re-checkable condition or passed its next check, or that an ask has held past
coordinator.ask_rethink_h (askrethink.py), is carried and flagged. A
valid `still_blocked` whose next check is still ahead, on an item that has not changed since (status,
spec, reason) and is no loop, is carried forward as one collapsed CARRIED line that needs no action; a
sweep due only to its interval is skipped while every item is carried. A hold no turn can lift (`ends`:
a user pause or a time-only deferral, userpause) is carried without an outcome, listed as HELD BY USER
PAUSE or DEFERRED (no action), and left out of signals and loop detection. Once a sweep records
`human_indispensable` for an item a user-set resource pause holds, the outcome keeps that pause row's
fingerprint (`pause`); until a user message arrives or the row changes or ends, every stuck task naming
that resource (its `resource:`/`exclusive:` labels, which dispatch already holds) ends with the user
too: carried, not re-judged, and free of flags raised before that outcome.

Watched holds: a hold whose end a model-free probe already watches (a deferred task's `start_when`, a
resource pause's `end_when`, both run by the daemon, which lifts the hold once its probe passes) gets a
fingerprint (`fp`): its status, spec and reason, the probe and its last exit code, the pause's `until`,
its unfinished dependencies and the open machine change-ledger entries naming its resources. Its
`still_blocked` stays carried past its next check while the probe still says not yet (a fresh 1, 75 or
255), its `until` is ahead, the fingerprint matches the one the turn judged and no inbound message
named it since (`#<id>` or the resource's name). A turn re-judges it at least every RECHECK_H hours
(the daily re-check), so the interval duty still reaches it. A sweep is not started at all, not even on a
signal, while every item is such a hold or one no turn can lift (`ends`): nothing a turn could see has
changed. Any change, a broken or stale probe, or a hold with no probe is swept as before.

Ask-held holds: a blocked task or a resource pause the coordinator set whose hold (the task's
`waits:ask:<id>` anchor or blocked reason, the pause's reason) and sweep outcome together name exactly
one open ask (`ask <id>`), with a `human_indispensable` or a `still_blocked` with no probe, is shown as
HELD BY OPEN ASK <id> and carried without a turn (ask_held) while the ask stays open, no user message
(nor the user's words relayed) came since the outcome, its status, spec and reason and its pause row
are unchanged, the ask has not passed coordinator.ask_rethink_h since (askrethink.py), and a turn
judged it within RECHECK_H (the daily re-check). Anything else brings it back for judgment.

Signals that bring a sweep forward (never closer than coordinator.blocker_sweep_min_gap_h to the last
one): a paused resource is no longer paused, an ask was resolved, a stuck task's dependency finished,
a provider's limit or logout pause ended, a provider's account or budget kind changed (budgetkind),
a `still_blocked` next check came due, or another module
called signal() (the hook for later checks, such as a broker's health lifting a pause). While every
item ends without a turn (`ends`), is a probe-watched hold or names one open ask (`ask`), only a
pause ending or one of the built-in signals above brings a sweep forward; other signal() requests (a
budget kind or account change, resolved upstream notes) wait for the interval, so a sweep still runs
at least every coordinator.blocker_sweep_h hours unless every item is carried.

Loops: a task handed off stuck twice in a row with the same normalized reason, or whose spec changed
while it stayed in the same stuck state since the last sweep, is shown as LOOP: resolve it, do not
re-addend it. Reviews: a task in review longer than coordinator.review_backlog_h (default 6) gets a
line on its review task: none queued, queued but held (and why), running, failed or finished.

Probe stalls: before each sweep check the daemon settles every `stall:probe:#<id>` open longer than
coordinator.probe_stall_dispose_h (default 1; 0 off) without a model call (dispose_probe_stalls): a
probe whose named tasks are all done is re-run (a pass lifts the hold; a not yet releases the task
once with a note to check its probe), one that greps a literal sha is flagged, one only the user can
end is left alone. The digest lists each disposition.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable

from . import askrethink, credits, failover, holdloops, mitigate, obligations, outagetask, prguard, settinghold, timefmt, unblock, userpause
from .db import DB, deferral, dependency_ids, load_result
from .coordlimits import positive

SWEEP_KIND = "blocker_sweep"
STATE_KEY = "blocker_sweep"            # kv: {"last", "n", "items": {key: snapshot}, "flags": {key: why}, "paused": [...],
#                                         "seen": {key: {"status", "since"}}: when each item was first seen stuck}
OUTCOMES_KEY = "blocker_sweep_outcomes"  # kv: {key: {"outcome", "ts", "until", "probe", "next", "why", "sweep"}}
RELAYED_KEY = "blocker_sweep_user_relayed"  # kv: when the user's words last came relayed in another project's note
SIGNAL_KEY = "blocker_sweep_signals"   # kv: [{"ts", "why", "rearm"}]: signal() requests not yet swept
PAUSE_PROBES_KEY = "pause_probes"       # kv: {resource: {"probe", "rc", "at"}}: each end_when's last verdict (pauseends)
OUTCOMES = ("unblocked", "rerouted", "retired", "closed", "still_blocked", "human_indispensable")
EVERY_H = 6.0
MIN_GAP_H = 1.0
REVIEW_BACKLOG_H = 6.0
REVIEW_LONG_H = 6.0     # the daily review lists every item stuck longer than this
LOOP = "loop: resolve, do not re-addend"
REASON_CHARS = 200
# An `until` that only restates the hold names no condition anyone could re-check.
SELF_REF = re.compile(r"\b(retain|keep|keeps|remain|remains|stay|stays|leave|still)\s+(it\s+|status\s+)?"
                      r"(blocked|in review|waiting|paused|held)\b|\baddend(um|a)?\b|\bno change\b|\bas before\b|"
                      r"^\s*(blocked|unchanged|n/?a|none|tbd|-)\s*\.?\s*$", re.I)
UNTIL_MIN_CHARS = 12
STUCK_EVENTS = tuple(unblock.STUCK)
RECHECK_H = 24.0         # a watched hold carried on its unchanged fingerprint is re-judged by a turn this often
PROBE_FRESH_S = 3600     # a probe verdict older than this says nothing about the hold now
PROBE_STALL_KEY = "blocker_sweep_probe_stalls"  # kv: {tid: {"probe", "disposition", "why", "at", "hours", "note"}}
PROBE_STALL_H = 1.0      # coordinator.probe_stall_dispose_h: a probe stall open this long is disposed model-free
PROBE_STALL_RECHECK_S = 1800  # a disposition that is not final is re-judged this often
PROBE_STALL_FINAL = ("rechecked-passed", "released-stale")   # once per task and probe text
PROBE_STALL_DISPOSITIONS = ("rechecked-passed", "released-stale", "literal-sha-flagged", "user-gated-left", "listed")
PROBE_SHA_NOTE = ("probe greps a literal sha; likely stale after a rebase or redeploy: "
                  "wait with `landed:#<id>` or `ttp landed <sha>`")
_PROBE_WORDS = re.compile(r"[\s'\"();|&<>]+")
_SECRET_PROBE = re.compile(r"\bttp\s+secret\b|secrets\.json")


def _cfg(cfg: dict, key: str, default: float) -> float:
    try:
        v = (cfg.get("coordinator") or {}).get(key, default)
        return max(0.0, float(default if v is None else v))
    except (TypeError, ValueError):
        return default


def _dur(s: float | None) -> str:
    return "unknown" if s is None else f"{max(0.0, s) / 3600:.1f}h"


def _updated(i: dict, now: float) -> str:
    """', last update 2.0h ago' for a stuck task updated (a reason, hold or addendum) after it got stuck:
    the item's age stays its episode's; '' when there is no later update."""
    upd, since = i.get("updated"), i.get("since")
    if not isinstance(upd, (int, float)) or (isinstance(since, (int, float)) and upd - since < 60):
        return ""
    return f", last update {_dur(now - upd)} ago"


def _clip(text: Any, n: int = REASON_CHARS) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def signal(db: DB, why: str, rearm: bool = False) -> None:
    """Ask for an early sweep (kept until the next sweep starts). The hook for other modules: call it
    when something a stuck item may wait on changed, e.g. a resource's health check lifts its pause.
    `rearm`: it may end a hold no turn can lift (a pause ending), so it counts even while every item
    is such a hold."""
    sigs = db.kv(SIGNAL_KEY, []) or []
    sigs = [s for s in sigs if isinstance(s, dict)][-19:]
    db.set_kv(SIGNAL_KEY, sigs + [{"ts": time.time(), "why": _clip(why, 200), "rearm": bool(rearm)}])


def user_words_relayed(db: DB, now: float | None = None) -> None:
    """The user's verbatim words came relayed in another project's note: like a user message, it ends
    every pause cover recorded before it, so the sweep re-judges those items. It lifts nothing itself."""
    now = time.time() if now is None else now
    db.set_kv(RELAYED_KEY, max(now, _num(db.kv(RELAYED_KEY, 0))))


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def outcomes(db: DB) -> dict[str, dict]:
    """The recorded sweep outcomes; an entry in another (older) format is dropped: its item is re-judged."""
    outs = db.kv(OUTCOMES_KEY, {}) or {}
    return {k: v for k, v in outs.items() if isinstance(v, dict)} if isinstance(outs, dict) else {}


def _pause_fp(row: dict) -> str:
    """A resource pause row's fingerprint: any change to it (a re-pause, a new reason) changes it."""
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _user_paused(rows: dict[str, dict]) -> dict[str, dict]:
    return {k: v for k, v in rows.items() if isinstance(v, dict) and v.get("by") == "user"}


def _task_resources(t: dict) -> set[str]:
    from .coordinator import task_resources
    return task_resources(t)


def pause_covers(db: DB, paused: dict[str, dict] | None = None) -> dict[str, float]:
    """User-paused resources a recorded `human_indispensable` still covers: name -> when that outcome
    was recorded. Each holds while its pause row is unchanged and no user message came since, nor the
    user's words relayed in another project's note (user_words_relayed)."""
    paused = _user_paused(db.paused_resources()) if paused is None else paused
    outs = [o for o in outcomes(db).values() if o.get("outcome") == "human_indispensable"
            and isinstance(o.get("pause"), dict) and o["pause"]] if paused else []
    if not outs:
        return {}
    said = max(_num((db.one("SELECT MAX(ts) ts FROM messages WHERE direction='in' AND kind='user'") or {}).get("ts")),
               _num(db.kv(RELAYED_KEY, 0)))
    out: dict[str, float] = {}
    for o in outs:
        ts = _num(o.get("ts"))
        for name, fp in o["pause"].items():
            if ts >= said and name in paused and fp == _pause_fp(paused[name]):
                out[name] = min(out.get(name, ts), ts)
    return out


def _last_stuck_events(db: DB, tid: int, n: int = 2) -> list[dict]:
    return db.q(f"SELECT ts, kind, text, data FROM events WHERE task=? AND kind IN ({','.join('?' * len(STUCK_EVENTS))}) "
                f"ORDER BY ts DESC, id DESC LIMIT ?", (tid, *STUCK_EVENTS, n))


def _reason(t: dict) -> str:
    return _clip(t["blocked_reason"] or load_result(t["result"]).get("summary") or "")


def _spec_sha(t: dict) -> str:
    # A provider failover's note is no change of the task's own spec (no LOOP flag for it).
    return hashlib.sha256(failover.strip_notes(t["spec"] or "").encode()).hexdigest()[:16]


def _moved_at(db: DB, tid: int) -> float:
    """When the task last moved on (a requeue, a hand-off out of stuck, or a run started): 0 if never."""
    ev = db.one(f"SELECT MAX(ts) ts FROM events WHERE task=? AND kind IN ({','.join('?' * len(unblock.MOVED))})",
                (tid, *sorted(unblock.MOVED)))
    run = db.one("SELECT MAX(started) ts FROM runs WHERE task=? AND role!='coordinator'", (tid,))
    return max(float((ev or {}).get("ts") or 0), float((run or {}).get("ts") or 0))


def _seen_since(db: DB, t: dict, seen: dict) -> float | None:
    """When the sweep first saw task `t` in its status (remember()), unless it moved on since: None if never."""
    prev = seen.get(f"task:{t['id']}") or {}
    start = prev.get("since") if prev.get("status") == t["status"] else None
    return float(start) if isinstance(start, (int, float)) and start >= _moved_at(db, t["id"]) else None


def entered(db: DB, t: dict, now: float, seen: dict | None = None) -> float | None:
    """When task `t` entered the stuck episode it is in (blocked, waiting or review): the start of its
    open stuck episode (unblock._task_episodes, the audit's), else when its status last turned stuck
    (tasks.stuck_since), else when the sweep first saw it so (`seen`, the STATE_KEY map; read when None),
    else None: unknown. Never its last update: a new reason, hold or addendum is not a new episode."""
    eps = unblock._task_episodes(db, t, now)
    if eps and eps[-1]["open"]:
        return eps[-1]["start"]
    if isinstance(t.get("stuck_since"), (int, float)):
        return float(t["stuck_since"])
    seen = (db.kv(STATE_KEY, {}) or {}).get("seen") or {} if seen is None else seen
    return _seen_since(db, t, seen)


def entered_map(db: DB, tasks: list[dict], now: float) -> dict[int, float | None]:
    """entered() of each task, reading the sweep's first-seen map once."""
    seen = (db.kv(STATE_KEY, {}) or {}).get("seen") or {}
    return {t["id"]: entered(db, t, now, seen) for t in tasks}


def planned_deferrals(db: DB, now: float | None = None) -> int:
    """Queued tasks deliberately deferred, their start_after still ahead: not stuck, so left out of items()."""
    now = time.time() if now is None else now
    return sum(1 for t in db.q("SELECT * FROM tasks WHERE status='queued'") if float(deferral(t).get("after") or 0) > now)


def items(db: DB, now: float | None = None, where=None) -> list[dict]:
    """Everything stuck now: {key, kind, task (or None), resource (or None), title, age_s (None when
    unknown), since, reason, status, spec_sha, ends}. Kinds: blocked, setting-held, waiting (not a self-wait),
    review, deferred, paused, user-pause (a task whose only hold is a user-pause probe), promise, outage (an open
    outage task, outagetask.py: its age from the outage's start, its due time; no outcome needed until due). `ends` is "user"
    or "time" for a hold no turn can lift (a user pause or user-set resource pause, a time-only deferral), else None. A task's age comes from its open stuck episode (unblock._task_episodes);
    without that history (a block the coordinator set) from when its status turned stuck (entered()), never
    from its last update (`updated`, shown apart), so a later update (an addendum) never resets it.
    Times in reasons are in the home zone of `where` (a Project, its settings or a zone name; None is UTC)."""
    now = time.time() if now is None else now
    out: list[dict] = []
    since_review = db.review_since()
    seen = (db.kv(STATE_KEY, {}) or {}).get("seen") or {}
    cfg = userpause.settings(where)
    paused = db.paused_resources()
    covers = pause_covers(db, _user_paused(paused))
    for t in db.q("SELECT * FROM tasks WHERE status IN ('blocked','waiting','review','queued') ORDER BY id"):
        kind, start, reason, ends = t["status"], None, _reason(t), None
        if kind == "queued":
            d = deferral(t)
            if not d or (not d.get("when") and float(t["not_before"] or 0) <= now):
                continue
            if float(d.get("after") or 0) > now:   # a planned deferral, not stuck yet (planned_deferrals())
                continue
            kind, start = "deferred", d.get("after") or d.get("since")
            reason = _clip(d.get("why") or d.get("when") or "") or (
                f"starts after {timefmt.long(float(t['not_before']), where)}"
                if t["not_before"] else "")
            if userpause.time_only(t, now):
                ends = "time"
        else:
            last = _last_stuck_events(db, t["id"], 1)
            if kind == "waiting" and last and last[0]["kind"] == "task_waiting" and unblock.is_self_wait(last[0]["data"]):
                continue
            start = since_review.get(t["id"]) if kind == "review" else entered(db, t, now, seen)
            if kind == "waiting" and last:
                reason = _clip(unblock.wait_reason(last[0])) or reason
            if kind == "blocked" and settinghold.held_key(t):
                kind = "setting-held"
        cover = None
        if kind != "review" and (pause := userpause.probe_of(t, cfg)):
            kind, ends, reason = "user-pause", "user", f"held by user pause until `{_clip(pause, 160)}` passes"
        elif kind != "review" and not ends and (names := sorted(_task_resources(t) & covers.keys())):
            # The user said only they can lift this pause: no turn re-judges what dispatch holds on it.
            ends, cover = "user", min(covers[n] for n in names)
            reason = _clip(f"held by user pause on {', '.join(names)}" + (f"; {reason}" if reason else ""))
        if not isinstance(start, (int, float)):
            start = _seen_since(db, t, seen)
        out.append({"key": f"task:{t['id']}", "kind": kind, "task": t["id"], "resource": None,
                    "title": t["title"], "age_s": None if start is None else max(0.0, now - float(start)),
                    "since": start, "updated": t["updated"], "reason": reason, "status": t["status"], "spec_sha": _spec_sha(t),
                    "ends": ends, **({"cover": cover} if cover is not None else {})})
    from . import machines
    known = machines.load() if paused else {}
    for name, v in sorted(paused.items()):
        if machines.decommissioned_user_pause(name, v, known):
            continue   # the user's pause of a retired machine: it stays as set and is no stuck work
        since = v.get("since") if isinstance(v.get("since"), (int, float)) else None
        out.append({"key": f"resource:{name}", "kind": "paused", "task": None, "resource": name, "title": name,
                    "age_s": None if since is None else max(0.0, now - float(since)), "since": since,
                    "reason": _clip(f"by {v.get('by') or 'user'}" + (f": {v['reason']}" if v.get("reason") else "")),
                    "status": "paused", "spec_sha": "",
                    # A pause the user set is lifted on their word only; no turn needs to re-check it.
                    "ends": "user" if v.get("by") == "user" else None})
    for o in obligations.open_promises(db):
        pr, late = o["promise"], now >= o["promise"]["due"]
        out.append({"key": f"promise:{o['id']}", "kind": "promise", "task": None, "resource": None,
                    "promise": o["id"], "title": o["text"], "age_s": max(0.0, now - float(o["created"])),
                    "since": o["created"], "status": "overdue" if late else "promised", "spec_sha": "",
                    "reason": f"promised in chat {pr['chat']} (message #{pr['message']}), due "
                              f"{timefmt.long(pr['due'], where)}" + (" OVERDUE" if late else "")
                              + (f", owner #{', #'.join(map(str, pr['tasks']))}" if pr["tasks"] else ""),
                    # Not due yet: listed, no outcome needed; overdue: an item like any other.
                    "ends": None if late else "due"})
    # An open mitigation (mitigate.py) shows with its due time: not due yet, listed; overdue, an item.
    keyed = {i["key"]: i for i in out}
    for t, rec in mitigate.open_ones(db):
        late, due = now >= float(rec.get("due") or 0), mitigate.due_text(rec, now, where)
        if (i := keyed.get(f"task:{t['id']}")):
            i["reason"] = _clip(f"{due}; {i['reason']}")
            continue
        out.append({"key": f"task:{t['id']}", "kind": "mitigation", "task": t["id"], "resource": None,
                    "title": t["title"], "age_s": max(0.0, now - float(rec.get("filed") or t["created"])),
                    "since": rec.get("filed") or t["created"], "status": t["status"], "spec_sha": _spec_sha(t),
                    "reason": _clip(f"{t['status']}, {due}, for {rec.get('resource')}"), "ends": None if late else "due"})
    listed = {i["key"]: i for i in out}
    for t in outagetask.open_tasks(db):
        o, key = t["outage"], f"task:{t['id']}"
        late = o["due"] is not None and now >= o["due"]
        reason = _clip(f"machine {o['alias']} {o['condition']} since {timefmt.long(o['first'], where)}"
                       + (f", task due {timefmt.long(o['due'], where)}" if o["due"] is not None else "")
                       + (" OVERDUE" if late else "") + f", task {t['status']}"
                       + (f"; {listed[key]['reason']}" if key in listed and listed[key]["reason"] else ""))
        item = {"key": key, "kind": "outage", "task": t["id"], "resource": None, "title": t["title"],
                "age_s": max(0.0, now - o["first"]), "since": o["first"], "reason": reason, "status": t["status"],
                "spec_sha": _spec_sha(t),
                # Not due yet: listed, no outcome needed; overdue (or stuck): an item like any other.
                "ends": "due" if not late and key not in listed else None}
        if key in listed:
            listed[key].update(item)   # a stuck outage task is one item, shown as the outage
        else:
            out.append(item)
    _watch(db, out, paused, now)
    _ask_holds(db, out, paused, cfg, now)
    return out


_ASK_RX = re.compile(r"\bask\s*#?(\d+)(?!\d)", re.I)   # as askrethink._cites


def _named_asks(db: DB, key: str, o: dict | None, paused: dict[str, dict]) -> tuple[set[int], dict | None]:
    """The asks a blocked task's or coordinator-set pause's hold and its outcome `o` cite, and the
    pause row (None for a task). Empty for anything else."""
    from .waits import _held_ask
    kind, _, ident = key.partition(":")
    texts, ids, row = [(o or {}).get("until"), (o or {}).get("why")], set(), None
    if kind == "task" and ident.isdigit():
        t = db.task(int(ident)) or {}
        if t.get("status") != "blocked":
            return set(), None
        texts.append(t.get("blocked_reason"))
        if (anchored := _held_ask(t)) is not None:
            ids.add(anchored)
    elif kind == "resource" and isinstance(row := paused.get(ident), dict) and row.get("by") != "user":
        texts.append(row.get("reason"))
    else:
        return set(), None
    return ids | {int(m) for x in texts for m in _ASK_RX.findall(str(x or ""))}, row


def _open_asks(db: DB) -> dict[int, dict]:
    return {int(r["id"]): r for r in db.q("SELECT id, ts, ref FROM messages WHERE direction='out' "
                                          "AND kind='ask' AND handled=0")}


def _ask_holds(db: DB, live: list[dict], paused: dict[str, dict], cfg: dict, now: float) -> None:
    """Give each blocked task and coordinator-set pause naming exactly one open ask its `ask` ({id,
    said, rethink, pause, held}): see the module doc."""
    cand = [i for i in live if not i.get("ends") and i["status"] in ("blocked", "paused")]
    asks = _open_asks(db) if cand else {}
    if not asks:
        return
    outs, h = outcomes(db), askrethink.hours(cfg)
    said = max(_num((db.one("SELECT MAX(ts) ts FROM messages WHERE direction='in' AND kind='user'") or {}).get("ts")),
               _num(db.kv(RELAYED_KEY, 0)))
    for i in cand:
        ids, row = _named_asks(db, i["key"], outs.get(i["key"]), paused)
        if len(ids) != 1 or (aid := next(iter(ids))) not in asks:
            continue
        a = asks[aid]
        rethinks = h > 0 and (a["ref"] or "").removeprefix("blocking:") in askrethink.REASONS
        i["ask"] = {"id": aid, "said": said, "rethink": float(a["ts"]) + h * 3600 if rethinks else None,
                    "pause": _pause_fp(row) if row else None}
        i["ask"]["held"] = ask_held(outs.get(i["key"]), i, now)


def _watch(db: DB, live: list[dict], paused: dict[str, dict], now: float) -> None:
    """Give each hold a model-free probe watches its `watch` ({probe, rc, until, failing}), `fp` and
    `touched` (the last inbound message naming it within RECHECK_H): see the module doc."""
    from . import ends, machine_ledger, startgate
    gates, probes, held = db.kv(startgate.KEY, {}) or {}, db.kv(PAUSE_PROBES_KEY, {}) or {}, []
    for i in live:
        if i.get("ends") or i["kind"] not in ("deferred", "paused"):
            continue
        if i["kind"] == "deferred":
            t = db.task(i["task"]) or {}
            probe, until, v, names = deferral(t).get("when"), None, gates.get(str(i["task"])) or {}, _task_resources(t)
            deps = sorted(d for d in dependency_ids(t) if isinstance(d, int) and (db.task(d) or {}).get("status") != "done")
            said = v.get("when")
        else:
            row = paused.get(i["resource"]) or {}
            probe, until, v, names, deps = row.get("end_when"), row.get("until"), probes.get(i["resource"]) or {}, \
                {i["resource"]}, []
            said = v.get("probe")
        if not isinstance(probe, str) or not probe.strip():
            continue
        rc = v.get("rc") if said == probe else None
        i["watch"] = {"probe": probe, "rc": rc, "until": until,
                      "failing": rc in ends.NOT_YET_RCS and now - _num(v.get("at")) <= PROBE_FRESH_S
                      and (not until or _num(until) > now)}
        held.append((i, sorted(names), deps))
    if not held:
        return
    msgs = db.q("SELECT ts, text FROM messages WHERE direction='in' AND ts>?", (now - RECHECK_H * 3600,))
    try:
        ledger = machine_ledger.changes()
    except Exception:   # an unreadable ledger proves nothing unchanged
        ledger = None
    for i, names, deps in held:
        pats = ([rf"#{i['task']}\b"] if i["task"] else []) + [rf"(?<![\w-]){re.escape(n)}(?![\w-])" for n in names]
        rx = re.compile("|".join(pats), re.I)
        i["touched"] = max((float(m["ts"]) for m in msgs if rx.search(m["text"] or "")), default=0.0)
        led = None if ledger is None else sorted(
            [c.get("id"), c.get("what"), c.get("expires"), c.get("until_probe")] for c in ledger
            if any(c.get("alias") == n or n.startswith(f"{c.get('alias')}-") for n in names))
        w = i["watch"]
        i["fp"] = hashlib.sha256(json.dumps([_snap(i), w["probe"], w["rc"], w["until"], deps, led],
                                            default=str).encode()).hexdigest()[:16]
        if led is None:
            w["failing"] = False


def _seen(state: dict, live: list[dict]) -> dict:
    """The `seen` map for these live items: an entry kept while the item stays in the same status and
    items() still ages it from that entry (a requeue or run since makes items() reject it: replaced)."""
    old = state.get("seen") or {}
    out = {}
    for i in live:
        o = old.get(i["key"]) or {}
        out[i["key"]] = o if o.get("status") == i["status"] and o.get("since") is not None \
            and o.get("since") == i["since"] else {"status": i["status"], "since": i["since"]}
    return out


def remember(db: DB, live: list[dict]) -> None:
    """Keep when each stuck item was first seen in its status (the age source for holds without history)."""
    state = db.kv(STATE_KEY, {}) or {}
    seen = _seen(state, live)
    if seen != (state.get("seen") or {}):
        db.set_kv(STATE_KEY, {**state, "seen": seen})


def loop(db: DB, item: dict, prev: dict | None) -> str | None:
    """Why this item looks like a re-block loop, or None: the same normalized stuck reason handed off
    twice in a row, or its spec changed since the last sweep while it stayed in the same stuck state."""
    if item["task"] is None or item["kind"] == "deferred" or item.get("ends") or (item.get("ask") or {}).get("held"):
        return None
    last = _last_stuck_events(db, item["task"], 2)
    if len(last) == 2 and last[0]["kind"] == last[1]["kind"]:
        a, b = unblock.wait_reason(last[0]), unblock.wait_reason(last[1])
        if a and a == b:
            return f"{LOOP} (handed off {unblock.STUCK[last[0]['kind']]} twice with the same reason)"
    if prev and prev.get("status") == item["status"] and prev.get("spec_sha") and \
            prev["spec_sha"] != item["spec_sha"]:
        return f"{LOOP} (spec changed while it stayed {item['status']})"
    return None


def review_diagnosis(db: DB, tid: int, now: float, hold_of: Callable[[dict], str | None] | None = None) -> str:
    """One line on the review of a task in review: none queued, queued but held (and why), running,
    failed or finished (and so waiting on its verdict being acted on)."""
    task = db.task(tid)
    revs = [r for r in db.q("SELECT * FROM tasks WHERE kind='review' AND id>? ORDER BY id DESC", (tid,))
            if tid in dependency_ids(r) or f"auto_review:{tid}" in (r["labels"] or "")]
    if not revs:
        if task and task["kind"] != "code":
            why = "the daemon queues one only for a code task with a diff and review.auto on"
        elif task and not task.get("branch") and not (pr := prguard.task_pr(task)):
            why = "no branch on record: the daemon cannot read this task's diff"
        elif task and not task.get("branch"):
            why = f"check review.auto, the review task cap and that its PR {pr} is open and readable"
        else:
            why = "check review.auto, the review task cap and the branch's diff"
        return f"no review queued ({why})"
    r = revs[0]
    runs = db.q("SELECT status, started, ended FROM runs WHERE task=? ORDER BY id", (r["id"],))
    started = f", started {len(runs)}x" if runs else ""
    if r["status"] == "queued":
        held = None
        if hold_of:
            try:
                held = hold_of(r)
            except Exception as e:   # a diagnosis must never break the sweep
                held = f"unknown ({type(e).__name__})"
        unmet = [d for d in dependency_ids(r) if d != tid and (db.task(d) or {}).get("status") != "done"]
        why = (f"dispatch holds it: {held}" if held else None) or (f"waits on #{', #'.join(map(str, unmet))}" if unmet else
                       "not started yet; no dispatch hold found")
        return f"review #{r['id']} queued {_dur(now - float(r['created'] or now))}{started}, not dispatched: {why}"
    if r["status"] == "running":
        return f"review #{r['id']} running{started}"
    summary = _clip(load_result(r["result"]).get("summary") or r["blocked_reason"] or "", 160)
    if r["status"] in ("failed", "cancelled", "blocked", "waiting"):
        return f"review #{r['id']} {r['status']}{started}" + (f": {summary}" if summary else "")
    return f"review #{r['id']} {r['status']}{started}; its verdict is not acted on yet" + (
        f": {summary}" if summary else "")


def _clock(state: dict) -> float:
    """When the sweep last counted: its last turn, or a later model-free skip's receipt (sweepskip)."""
    return max(_num(state.get("last")), _num(state.get("receipt")))


def _signals(db: DB, state: dict, live: list[dict], now: float, rearm_only: bool = False) -> list[str]:
    """What changed since the last sweep (or its receipt) that may unblock something. rearm_only: of
    signal()'s requests, only those that may end a hold no turn lifts (a pause ending)."""
    last = _clock(state)
    out = [str(s.get("why") or "signal") for s in (db.kv(SIGNAL_KEY, []) or []) if isinstance(s, dict)
           and (not rearm_only or s.get("rearm") or str(s.get("why") or "").startswith("pause ended"))]
    paused_now = {i["resource"] for i in live if i["kind"] == "paused"}
    gone = sorted(set(state.get("paused") or []) - paused_now)
    if gone:
        out.append("pause ended: " + ", ".join(gone))
    for aid, r in (db.kv(unblock.RESOLVED_KEY, {}) or {}).items():
        if isinstance(r, dict) and float(r.get("ts") or 0) > last:
            out.append(f"ask #{aid} resolved")
            break
    deps = {d for i in live if i["task"] and not i.get("ends") for d in dependency_ids(db.task(i["task"]) or {"depends_on": "[]"})
            if isinstance(d, int)}
    if deps:
        done = db.q(f"SELECT DISTINCT task FROM events WHERE kind='task_done' AND ts>? AND task IN "
                    f"({','.join('?' * len(deps))})", (last, *sorted(deps)))
        if done:
            out.append("dependency done: " + ", ".join(f"#{r['task']}" for r in done))
    for r in db.q("SELECT key, value FROM kv WHERE key LIKE 'limited:%'"):
        try:
            until = float(json.loads(r["value"]).get("until") or 0)
        except (ValueError, TypeError, AttributeError):
            continue
        if last < until <= now:
            out.append(f"provider pause over: {r['key'].split(':', 1)[1]}")
    outs = outcomes(db)
    for i in live:
        o = outs.get(i["key"]) or {}
        if i.get("ends") or o.get("outcome") not in ("still_blocked", "human_indispensable") or \
                (o["outcome"] == "human_indispensable" and not o.get("ask")):
            continue
        if watched(o, i, now) or ask_held(o, i, now):   # unchanged, as the turn judged it: no news
            continue
        if _recheck_due(o, i, now) and last < _num(o.get("ts")) + RECHECK_H * 3600 <= now:
            out.append(f"daily re-check due: {i['key']}")
        elif o.get("next") and last < float(o["next"]) <= now:
            out.append(f"next check due: {i['key']}")
    return out


def _quiet(db: DB, cfg: dict, state: dict, live: list[dict], now: float) -> bool:
    """Every item is a watched or ask-held hold carried unchanged or a hold no turn can lift, at
    least one of the former: a sweep turn would only re-read them, so none starts, not even on a signal."""
    if holdloops.flagged(db) or all(i.get("ends") for i in live):
        return False
    outs, prev, rethought = outcomes(db), state.get("items") or {}, rethink(db, cfg, live, now)
    return all(_carries(i, state, outs, now)
               and (i.get("ends") or watched(outs.get(i["key"]), i, now) or ask_held(outs.get(i["key"]), i, now))
               and not loop(db, i, prev.get(i["key"])) and i["key"] not in rethought for i in live)


def due(db: DB, cfg: dict, now: float | None = None) -> str | None:
    """Why a sweep should start now, or None: nothing stuck, sweeps off, one queued already, or
    neither its interval nor a signal (past the minimum gap) calls for one."""
    now = time.time() if now is None else now
    every = _cfg(cfg, "blocker_sweep_h", EVERY_H) * 3600
    if every <= 0:
        return None
    live = items(db, now, cfg)
    remember(db, live)
    if not live or db.one("SELECT id FROM events WHERE kind=? AND status='queued' LIMIT 1", (SWEEP_KIND,)):
        return None
    state = db.kv(STATE_KEY, {}) or {}
    since = now - _clock(state)
    if since >= every:
        outs = outcomes(db)
        prev = state.get("items") or {}
        rethought = rethink(db, cfg, live, now)
        if not holdloops.flagged(db) and all(_carries(i, state, outs, now) and not loop(db, i, prev.get(i["key"]))
                                             and i["key"] not in rethought for i in live):
            # Every item keeps a valid, not-yet-due outcome: only a pending signal calls for a sweep.
            sigs = _signals(db, state, live, now)
            return "; ".join(sigs)[:300] if sigs and not _quiet(db, cfg, state, live, now) else None
        return f"every {every / 3600:g}h"
    if since < min(every, _cfg(cfg, "blocker_sweep_min_gap_h", MIN_GAP_H) * 3600):
        return None
    # Every item ends without a turn, is probe-watched or waits on an open ask: only what may end one
    # brings the sweep forward, never a budget kind or account change.
    sigs = _signals(db, state, live, now, rearm_only=all(
        i.get("ends") or (i.get("watch") or {}).get("failing") or i.get("ask") for i in live))
    return "; ".join(sigs)[:300] if sigs and not _quiet(db, cfg, state, live, now) else None


def _snap(i: dict) -> dict:
    """What a carried outcome must still match: the item's status, spec and reason."""
    return {"status": i["status"], "spec_sha": i["spec_sha"], "reason": i["reason"]}


def carried(o: dict | None, i: dict, now: float) -> bool:
    """A still_blocked outcome that still holds for item `i`: re-checkable, its next check ahead, and
    recorded against the item as it is now. Such an item needs no new outcome at this sweep. Nor does
    a hold that ends without a turn (`ends`): a user pause or a time-only deferral, nor an ask-held one."""
    if i.get("ends") or ask_held(o, i, now):
        return True
    if not (o and o.get("outcome") == "still_blocked" and recheckable(o) and o.get("item") == _snap(i)):
        return False
    return bool(o.get("next") and float(o["next"]) > now) or watched(o, i, now)


def watched(o: dict | None, i: dict, now: float) -> bool:
    """A still_blocked on a hold its own model-free probe watches, carried past its next check: the
    probe still says not yet, nothing in the fingerprint changed, no message named it since, and a
    turn judged it within RECHECK_H."""
    w = i.get("watch") or {}
    return bool(o and w.get("failing") and o.get("outcome") == "still_blocked" and o.get("fp")
                and o["fp"] == i.get("fp") and o.get("item") == _snap(i)
                and _num(i.get("touched")) <= _num(o.get("ts")) < now <= _num(o.get("ts")) + RECHECK_H * 3600)


def _recheck_due(o: dict | None, i: dict, now: float) -> bool:
    """A watched hold whose only change is that its daily re-check came due: an item, not a FLAG."""
    w = i.get("watch") or {}
    return bool(o and w.get("failing") and o.get("fp") and o["fp"] == i.get("fp") and o.get("item") == _snap(i)
                and _num(i.get("touched")) <= _num(o.get("ts")) and now > _num(o.get("ts")) + RECHECK_H * 3600) \
        or (_ask_ok(o, i, now) and now > _num(o["ts"]) + RECHECK_H * 3600)


def _ask_ok(o: dict | None, i: dict, now: float) -> bool:
    """Outcome `o` names the one open ask item `i` waits on as its only lift, and nothing it rests on
    changed: no user message since, the same status, spec, reason and pause row, and the ask has not
    passed coordinator.ask_rethink_h since."""
    a = i.get("ask") or {}
    return bool(o and a and o.get("ask") == a["id"] and o.get("outcome") in ("still_blocked", "human_indispensable")
                and not o.get("probe") and o.get("item") == _snap(i) and o.get("ask_pause") == a.get("pause")
                and _num(a.get("said")) <= _num(o.get("ts"))
                and not (a.get("rethink") and _num(o.get("ts")) < a["rethink"] <= now))


def ask_held(o: dict | None, i: dict, now: float) -> bool:
    """An ask-held hold carried without a turn: _ask_ok, and a turn judged it within RECHECK_H."""
    return _ask_ok(o, i, now) and _num(o["ts"]) <= now <= _num(o["ts"]) + RECHECK_H * 3600


def flags(db: DB, state: dict, live: list[dict], now: float) -> dict[str, str]:
    """Items the last sweep left unresolved: no outcome since it, or a `still_blocked` with no
    re-checkable condition or past its next check."""
    last = float(state.get("last") or 0)
    prev = state.get("items") or {}
    outs = outcomes(db)
    out = {}
    for i in live:
        o = outs.get(i["key"])
        if carried(o, i, now) or _recheck_due(o, i, now):
            continue
        if i["key"] in prev and (not o or float(o.get("ts") or 0) < last):
            out[i["key"]] = "no outcome at the last sweep"
        elif o and o.get("outcome") == "still_blocked":
            if not recheckable(o):
                out[i["key"]] = "still_blocked without a re-checkable condition"
            elif o.get("next") and float(o["next"]) <= now:
                out[i["key"]] = "its next check is due"
    return out


def rethink(db: DB, cfg: dict, live: list[dict], now: float) -> dict[str, str]:
    """Blocked tasks an open ask has held past coordinator.ask_rethink_h (askrethink.py): a
    `still_blocked` on one is flagged, never carried. The coordinator decides the ask itself, or
    records `human_indispensable` with why only the user can answer it."""
    held = askrethink.held_tasks(db, cfg, now)
    outs = outcomes(db)
    return {i["key"]: f"held by open ask {held[i['task']]} past {askrethink.hours(cfg):g}h: decide the ask "
                      f"yourself, or record human_indispensable with why only the user can"
            for i in live if i["task"] in held and i.get("cover") is None
            and (outs.get(i["key"]) or {}).get("outcome") != "human_indispensable"}


def recheckable(o: dict) -> bool:
    """A still_blocked outcome names something anyone can re-check: a probe, or an `until` that is
    not only 'keep it blocked'."""
    if str(o.get("probe") or "").strip():
        return True
    until = str(o.get("until") or "").strip()
    return len(until) >= UNTIL_MIN_CHARS and not SELF_REF.search(until)


def start(db: DB, cfg: dict, why: str, now: float | None = None,
          hold_of: Callable[[dict], str | None] | None = None) -> int | None:
    """Queue the sweep's event and record what it covers, with each long review's diagnosis (hold_of:
    why dispatch would not start a queued task now, the daemon's own check); returns the event id."""
    now = time.time() if now is None else now
    state = db.kv(STATE_KEY, {}) or {}
    live = items(db, now, cfg)
    if not live:
        return None
    prev = state.get("items") or {}
    flagged = flags(db, state, live, now)
    flagged.update(rethink(db, cfg, live, now))
    loops = {i["key"]: x for i in live if (x := loop(db, i, prev.get(i["key"])))}
    stuck = holdloops.flagged(db)
    for i in live:
        if i["task"] in stuck:
            loops[i["key"]] = "; ".join(filter(None, (loops.get(i["key"]), f"{LOOP} ({stuck[i['task']]})")))
    backlog = _cfg(cfg, "review_backlog_h", REVIEW_BACKLOG_H) * 3600
    reviews = {i["key"]: review_diagnosis(db, i["task"], now, hold_of) for i in live
               if i["kind"] == "review" and backlog > 0 and (i["age_s"] or 0) >= backlog}
    outs = outcomes(db)
    carry = [i["key"] for i in live if i["key"] not in loops and i["key"] not in reviews
             and i["key"] not in flagged and carried(outs.get(i["key"]), i, now)]
    n = int(state.get("n") or 0) + 1
    with db.tx():
        db.set_kv(STATE_KEY, {"last": now, "n": n, "why": why, "flags": flagged, "loops": loops, "reviews": reviews,
                              "carried": carry, "paused": [i["resource"] for i in live if i["kind"] == "paused"],
                              "seen": _seen(state, live), "items": {i["key"]: _snap(i) for i in live},
                              "fps": {i["key"]: i["fp"] for i in live if i.get("fp")}})
        db.x("DELETE FROM kv WHERE key=?", (SIGNAL_KEY,))
        eid = db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
                   (now, "daemon", SWEEP_KIND, "normal",
                    f"Blocker sweep {n} ({why}): {len(live)} stuck item(s), {len(flagged)} flagged, "
                    f"{len(loops)} loop(s), {len(carry)} carried forward. Record one `sweep` outcome for each "
                    f"item not carried.",
                    json.dumps({"sweep": n, "items": [i["key"] for i in live], "snapshot": live}), "queued"))
    return eid


def _item_key(a: dict) -> str:
    if a.get("id") is not None:
        return f"task:{int(a['id'])}"
    if str(a.get("promise") or "").strip():
        return f"promise:{str(a['promise']).strip()}"
    name = str(a.get("resource") or "").strip()
    if not name:
        raise ValueError("sweep needs `id` (a task), `promise` (its obligation id) or `resource` (a paused resource)")
    return f"resource:{name}"


def still_deferred(db: DB, tid: int, now: float, where=None) -> str:
    """What still defers queued task `tid` ('' when nothing): a start_after ahead, a start_when not yet
    passed. '' too for a task this sweep showed blocked, waiting or in review that is queued now: its
    requeue in this turn kept the deferral as planned."""
    t = db.task(tid)
    if not t or t["status"] != "queued":
        return ""
    snap = ((db.kv(STATE_KEY, {}) or {}).get("items") or {}).get(f"task:{tid}")
    if snap and snap.get("status") != "queued":
        return ""
    d, holds = deferral(t), []
    if float(d.get("after") or 0) > now:
        holds.append(f"start_after {timefmt.long(float(d['after']), where)}")
    if d.get("when"):
        holds.append(f"start_when `{_clip(d['when'], 160)}`")
    return " and ".join(holds)


def record(db: DB, a: dict, next_at: float | None, now: float | None = None, where=None) -> str:
    """Record one `sweep` action's outcome (next_at: its `start_after`, already parsed). Raises
    ValueError on a bad one, which the turn reports back. `unblocked` never lifts a deferral: on a task
    still deferred it is refused, so only a task_update that clears start_after/start_when ends it."""
    now = time.time() if now is None else now
    outcome = str(a.get("outcome") or "")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
    key = _item_key(a)
    if key.startswith("task:") and not db.task(int(key[5:])):
        raise ValueError(f"no task #{key[5:]}")
    if outcome == "unblocked" and key.startswith("task:") and (held := still_deferred(db, int(key[5:]), now, where)):
        raise ValueError(f"#{key[5:]} is still deferred ({held}): `unblocked` keeps that deferral, so it would "
                         f"not start. Record still_blocked with its start as the next check, or clear the deferral "
                         f"first in this turn (task_update start_after \"now\", start_when \"\")")
    if key.startswith("promise:"):
        o = obligations.get(db, key[8:])
        if not o or not o.get("promise"):
            raise ValueError(f"no promise {key[8:]}")
        if outcome in ("closed", "retired"):   # kept or no longer owed: the promise is closed
            obligations.close_promise(db, key[8:], f"sweep {outcome}: " + str(a.get("why") or a.get("reason") or ""),
                                      "coordinator", now)
    o = {"outcome": outcome, "ts": now, "sweep": int((db.kv(STATE_KEY, {}) or {}).get("n") or 0)}
    if outcome == "still_blocked":
        o.update(until=_clip(a.get("until") or a.get("reason") or "", 300),
                 probe=_clip(a.get("until_probe") or "", 500))
        if not recheckable(o):
            raise ValueError(f"{key} still_blocked needs a re-checkable condition: `until_probe` (a shell probe) or "
                             f"`until` naming what must change (not 'keep it blocked')")
        if not next_at or next_at <= now:
            raise ValueError(f"{key} still_blocked needs `start_after`: when to check it next")
        o["next"] = next_at
        # The item as this sweep showed it: the outcome carries forward only while it stays so.
        state = db.kv(STATE_KEY, {}) or {}
        snap = (state.get("items") or {}).get(key)
        if snap:
            o["item"] = snap
        if fp := (state.get("fps") or {}).get(key):   # a watched hold, as the turn saw it (watched())
            o["fp"] = fp
    elif outcome == "human_indispensable":
        if not str(a.get("why") or "").strip():
            raise ValueError(f"{key} human_indispensable needs `why` only a person can do it")
        paused = _user_paused(db.paused_resources())
        names = ({key[9:]} if key.startswith("resource:") else
                 _task_resources(db.task(int(key[5:])) or {}) if key.startswith("task:") else set()) & paused.keys()
        if names:   # held by the user's own pause: carries the items naming it until the row changes
            o["pause"] = {n: _pause_fp(paused[n]) for n in sorted(names)}
    if outcome == "human_indispensable" or (outcome == "still_blocked" and not o.get("probe")):
        o["why"] = _clip(a.get("why") or a.get("reason") or a.get("text") or "", 300)
        ids, row = _named_asks(db, key, o, db.paused_resources())
        snap = ((db.kv(STATE_KEY, {}) or {}).get("items") or {}).get(key)
        if len(ids) == 1 and next(iter(ids)) in _open_asks(db) and snap:   # held only by that ask (ask_held)
            o.update(ask=next(iter(ids)), item=snap, **({"ask_pause": _pause_fp(row)} if row else {}))
    o["why"] = _clip(a.get("why") or a.get("reason") or a.get("text") or "", 300)
    outs = outcomes(db)
    outs[key] = o
    db.set_kv(OUTCOMES_KEY, {k: v for k, v in outs.items() if now - _num(v.get("ts")) < 30 * 86400})
    return key


def _last_check(o: dict | None, now: float) -> str:
    if not o:
        return "never re-checked"
    s = f"last re-check {_dur(now - float(o['ts']))} ago: {o['outcome']}"
    if o["outcome"] == "still_blocked":
        s += f" until {_clip(o.get('probe') or o.get('until'), 120)}"
        if o.get("next"):
            s += f", next check in {_dur(float(o['next']) - now)}" if float(o["next"]) > now else ", next check due"
    elif o.get("why"):
        s += f" ({_clip(o['why'], 120)})"
    return s


def _header(n: int, carry: int = 0) -> str:
    return (f"## Blocker sweep: {n} item(s)" + (f", {carry} CARRIED" if carry else "")
            + ". Return exactly one `sweep` action per item not marked CARRIED, "
            "with `id` (task), `promise` (its id) or `resource`, and `outcome`: unblocked | rerouted | retired | closed (merged or "
            "closed) | still_blocked (both required: `until_probe` or `until`, the concrete condition, AND "
            "`start_after`, the next check: a delay such as 90m, 6h or 3d, or an ISO time such as "
            "2026-10-05T09:00 in the home zone) | human_indispensable (`why`). A CARRIED item keeps its last "
            "still_blocked until its next check; act on it only with news. Make the change itself with the usual "
            "actions in this turn. A FLAG or LOOP needs a real decision, not another addendum.")


def _carries(i: dict, state: dict, outs: dict, now: float) -> bool:
    # An item a user pause's outcome covers ignores flags raised before it was covered: no sweep flags
    # a covered item, and a loop counts only once a sweep after the outcome found it again.
    marks = ("flags", "loops", "reviews")
    o = outs.get(i["key"])
    if i.get("cover") is not None:
        marks = ("reviews",) if float(i["cover"]) >= float(state.get("last") or 0) else ("loops", "reviews")
    elif ask_held(o, i, now) and _num(o["ts"]) >= _num(state.get("last")):   # it answers that sweep's marks
        marks = ("reviews",)
    return not any(i["key"] in (state.get(f) or {}) for f in marks) and carried(outs.get(i["key"]), i, now)


def _who(i: dict, n: int) -> str:
    if i["task"]:
        return f"#{i['task']} {_clip(i['title'], n)}"
    if i.get("promise") or i["key"].startswith("promise:"):
        return f"promise {i['key'][8:]} {_clip(i['title'], n)}"
    return f"resource {i['resource']}"


def _line(i: dict, state: dict, outs: dict, now: float) -> str:
    who = _who(i, 70)
    if i.get("ends") and _carries(i, state, outs, now):
        what = {"user": "HELD BY USER PAUSE", "due": "MITIGATION, NOT DUE YET" if i["kind"] == "mitigation"
                else "OUTAGE TASK, NOT DUE YET" if i["kind"] == "outage"
                else "PROMISED, NOT DUE YET"}.get(i["ends"], "DEFERRED")
        return f"- {who} | {what} (no action): {_clip(i['reason'], 160)}"
    if _carries(i, state, outs, now) and ask_held(outs.get(i["key"]), i, now):
        return f"- {who} | HELD BY OPEN ASK {i['ask']['id']} (no action): {_last_check(outs.get(i['key']), now)}"
    if _carries(i, state, outs, now):
        o = outs.get(i["key"])
        tail = "; its probe still says not yet, unchanged" if o and float(o.get("next") or 0) <= now else ""
        return f"- {who} | CARRIED (no action): {_last_check(o, now)}{tail}"
    line = f"- {who} | {i['kind']} {_dur(i['age_s'])}{_updated(i, now)} | {_clip(i['reason']) or 'no reason given'} | " \
           f"{_last_check(outs.get(i['key']), now)}"
    for field, label in (("flags", "FLAG"), ("loops", "LOOP"), ("reviews", "review")):
        value = (state.get(field) or {}).get(i["key"])
        if value:
            line += f" | {label}: {_clip(value)}"
    return line


def _settled(db: DB, key: str) -> dict:
    """A covered item that is no longer stuck, rebuilt from its durable identity (an event queued
    before snapshots existed has keys only), so the turn still records how it ended."""
    kind, _, ident = key.partition(":")
    if kind == "task" and ident.isdigit():
        t = db.task(int(ident))
        status = t["status"] if t else "gone"
        return {"key": key, "kind": "resolved", "task": int(ident), "resource": None,
                "title": t["title"] if t else "(no longer exists)", "age_s": None, "since": None,
                "reason": f"no longer stuck (now {status}): record closed or unblocked", "status": status,
                "spec_sha": _spec_sha(t) if t else "", "ends": None}
    if kind == "promise":
        o = obligations.get(db, ident) or {}
        return {"key": key, "kind": "resolved", "task": None, "resource": None, "promise": ident,
                "title": o.get("text") or "(no longer exists)", "age_s": None, "since": None,
                "reason": "promise closed: record closed", "status": "closed", "spec_sha": "", "ends": None}
    return {"key": key, "kind": "resumed", "task": None, "resource": ident, "title": ident, "age_s": None,
            "since": None, "reason": "no longer paused: record closed or unblocked", "status": "resumed",
            "spec_sha": "", "ends": None}


def _covered(db: DB, data: dict, now: float, where=None) -> list[dict]:
    # Retain an identity even if an earlier batch unblocked it; the next batch can record closed.
    by_key = {i["key"]: i for i in data.get("snapshot", [])}
    by_key.update({i["key"]: i for i in items(db, now, where)})
    return [by_key.get(k) or _settled(db, k) for k in data.get("items", [])]


def prepare(db: DB, event_id: int, cfg: dict, now: float | None = None) -> None:
    """Split this event's coverage to fit a bounded sweep section, including events queued before
    an upgrade. Persist continuations atomically; retries reuse the same first batch."""
    now = time.time() if now is None else now
    event = db.one("SELECT * FROM events WHERE id=? AND kind=?", (event_id, SWEEP_KIND))
    if not event:
        return
    data = json.loads(event["data"] or "{}")
    live = _covered(db, data, now, cfg)
    state, outs = db.kv(STATE_KEY, {}) or {}, outcomes(db)
    budget = min(16000, max(2048, positive((cfg.get("coordinator") or {}).get("sweep_prompt_chars"), 12000)))
    flagged, loops = state.get("flags") or {}, state.get("loops") or {}
    live.sort(key=lambda i: (i["key"] not in flagged and i["key"] not in loops, -(i["age_s"] or 0)))
    batches, batch = [], []
    size = len(_header(len(live))) + 32  # room for age counters changing before the digest is built
    for i in live:
        cost = len(_line(i, state, outs, now)) + 1
        if batch and size + cost > budget:
            batches.append(batch)
            batch, size = [], len(_header(len(live))) + 32
        batch.append(i)
        size += cost
    batches.append(batch)
    with db.tx():
        for n, batch in enumerate(batches):
            payload = json.dumps({**data, "items": [i["key"] for i in batch], "snapshot": batch})
            text = (f"Blocker sweep {data.get('sweep', 0)}: {len(batch)} stuck item(s) in this batch. "
                    "Record one `sweep` outcome for each item not marked CARRIED.")
            if n == 0:
                db.x("UPDATE events SET data=?, text=? WHERE id=?", (payload, text, event_id))
            else:
                db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
                     (event["ts"], "daemon", SWEEP_KIND, "normal", text, payload, "queued"))


def _probe_sha(probe: str) -> str:
    return hashlib.sha1(probe.encode()).hexdigest()[:12]


def _user_gated(db: DB, t: dict, probe: str, cfg: dict, asks: list[dict]) -> str | None:
    """Why a probe only the user can end (a user marker, a saved secret, an open ask holding the
    task) is left alone, or None."""
    from . import waits
    rx = userpause.marker(cfg)
    if userpause.is_probe(probe, cfg) or any(rx.search(w) for w in _PROBE_WORDS.split(probe) if w):
        return "it tests a user marker"
    if _SECRET_PROBE.search(probe):
        return "it tests a saved secret"
    held = waits._held_ask(t)
    if (held is not None and any(a["id"] == held for a in asks)) or waits.asks_for(t["id"], asks):
        return "an open ask holds the task"
    return None


def dispose_probe_stalls(db: DB, cfg: dict, now: float, current_probe: Callable[[dict], str | None],
                         run_probe: Callable[[str], Any], act: Callable[[dict, str, str], Any]) -> list[dict]:
    """Settle every probe stall (progress.probe_stalls' `stall:probe:#<id>`) open longer than
    coordinator.probe_stall_dispose_h (0 off) without a model call. When every task the wait names
    (`landed:#N`, a dependency) is done, and each `landed:` one is on the push branch, the probe is
    re-run (run_probe returns its exit code, None while it has none yet: Daemon._probe_now runs it off the
    tick and a later pass reads it): a pass lifts the hold (act 'passed'); a not yet
    marks the probe stale and releases the task once with a spec note (act 'release'), at most once
    per task and probe text. A probe that greps a literal sha is flagged; one only the user can end is
    left. Each item records its disposition (PROBE_STALL_DISPOSITIONS) in PROBE_STALL_KEY."""
    from . import landed, progress
    h = _cfg(cfg, "probe_stall_dispose_h", PROBE_STALL_H)
    if not h:
        return []
    raised = progress.load(db).get("raised") or {}
    done = db.kv(PROBE_STALL_KEY, {}) or {}
    asks = None
    out = []
    for key, r in sorted(raised.items()):
        if not key.startswith("stall:probe:#") or not isinstance(r, dict):
            continue
        first = r.get("first") or r.get("since")
        if not isinstance(first, (int, float)) or now - first < h * 3600:
            continue
        try:
            tid = int(key.rsplit("#", 1)[1])
        except ValueError:
            continue
        t = db.task(tid)
        probe = current_probe(t) if t and t["status"] in ("queued", "waiting") else None
        if not probe:
            continue
        sha, prev = _probe_sha(probe), done.get(str(tid)) or {}
        if prev.get("probe") == sha and (prev.get("disposition") in PROBE_STALL_FINAL
                                         or now - float(prev.get("at") or 0) < PROBE_STALL_RECHECK_S):
            continue
        if asks is None:
            asks = [{"id": int(a["id"]), "kind": a["kind"], "text": a["text"]}
                    for a in db.q("SELECT id, kind, text FROM messages WHERE direction='out' AND kind='ask' AND handled=0")]
        since = r.get("since") if isinstance(r.get("since"), (int, float)) else first
        hours = f"{max(0.0, now - since) / 3600:.1f}"
        sha_lit = landed.literal_sha(probe)
        rec = {"probe": sha, "at": now, "hours": hours}
        if why := _user_gated(db, t, probe, cfg, asks):
            rec.update(disposition="user-gated-left", why=f"{why}: left for the user, no wake")
        else:
            # A ref as written that is no task id ('$T', '0') names a task that is never done.
            raw = landed.task_refs(probe)
            good = [int(n) for n in raw if re.fullmatch(r"[0-9]+", n) and int(n) > 0]
            refs = [n for n in good if n != tid]
            deps = dependency_ids(t)
            named = sorted(set(refs) | {n for n in deps if n is not None and n != tid})
            rows = [db.task(n) for n in named]
            finished = (bool(named) and len(good) == len(raw) and None not in deps
                        and all(x and x["status"] == "done" for x in rows))
            unrun = False
            for n in refs if finished else ():
                got = run_probe(f"landed:#{n}")
                if got is None:   # no answer yet: the rest start too, all judged on a later pass
                    unrun = True
                elif got != 0:
                    finished = False
                    break
            if finished:
                rc = run_probe(probe)
                if rc is None or unrun:
                    continue
                names = ", ".join(f"#{n}" for n in named)
                if rc == 0:
                    rec.update(disposition="rechecked-passed", why=f"{names} done; the probe passes on a re-run")
                else:
                    rec.update(disposition="released-stale",
                               why=f"{names} done but the probe still says not yet (exit {rc}): released once")
                    note = (f"[probe stall] your probe has said not yet for {hours} h while the task it waits on is "
                            f"done; check it still tests what you wait for ({names} done; probe: {_clip(probe, 160)}).")
                    if sha_lit:
                        note += f" {PROBE_SHA_NOTE}"
                    rec["note"] = note
            elif sha_lit:
                rec.update(disposition="literal-sha-flagged", why=f"{PROBE_SHA_NOTE} ({sha_lit})")
            else:
                rec.update(disposition="listed",
                           why="no finished task named and no literal sha: the stall item stands")
        done[str(tid)] = rec
        db.set_kv(PROBE_STALL_KEY, done)   # before acting: a failed act never releases twice
        if rec["disposition"] == "rechecked-passed":
            act(t, "passed", rec["why"])
        elif rec["disposition"] == "released-stale":
            act(t, "release", rec["note"])
        out.append({"task": tid, **rec})
    return out


def probe_stall_lines(db: DB, now: float, hours: float = 24.0, n: int = 8) -> list[str]:
    """One line per probe-stall disposition recorded in the last `hours` (newest first)."""
    recs = sorted(((int(k), v) for k, v in (db.kv(PROBE_STALL_KEY, {}) or {}).items()
                   if isinstance(v, dict) and now - float(v.get("at") or 0) <= hours * 3600),
                  key=lambda kv: -float(kv[1].get("at") or 0))
    return [f"- probe stall #{tid} ({v.get('hours', '?')} h not yet): {v.get('disposition')}: {_clip(v.get('why'), 160)}"
            for tid, v in recs[:n]]


def digest_lines(db: DB, event_ids: list[int], now: float | None = None, cfg: dict | None = None) -> list[str]:
    """Only this batch's sweep identities; later batches stay queued for consecutive turns. A provider
    held red by its budget gate gets a line with its unblock path (free window resets, paid credits,
    the last reset used), though it is not a sweep item: the daemon consumes resets itself."""
    if not event_ids:
        return []
    events = db.q(f"SELECT data FROM events WHERE kind=? AND id IN ({','.join('?' * len(event_ids))}) ORDER BY id",
                  (SWEEP_KIND, *event_ids))
    if not events:
        return []
    now = time.time() if now is None else now
    state, outs = db.kv(STATE_KEY, {}) or {}, outcomes(db)
    live = {i["key"]: i for e in events for i in _covered(db, json.loads(e["data"] or "{}"), now, cfg)}
    flagged, loops = state.get("flags") or {}, state.get("loops") or {}
    ordered = sorted(live.values(), key=lambda i: (i["key"] not in flagged and i["key"] not in loops,
                                                   -(i["age_s"] or 0)))
    carry = sum(1 for i in ordered if _carries(i, state, outs, now))
    out = [_header(len(live), carry), *(_line(i, state, outs, now) for i in ordered)]
    for prov, g in sorted((db.kv("gates", {}) or {}).items()):
        if isinstance(g, dict) and g.get("level") == "red":
            path = credits.unblock_path(db, cfg or {}, prov, now)
            out.append(f"- provider {prov} held (no sweep action; {_clip('; '.join(g.get('reasons') or []), 160)}) | "
                       f"unblock path: {path or 'none reported (no free resets or paid credits)'}")
    out.extend(probe_stall_lines(db, now))
    if n := planned_deferrals(db, now):
        out.append(f"planned deferrals: {n}")
    return out


def review_lines(db: DB, now: float | None = None, hours: float = REVIEW_LONG_H, where=None) -> list[str]:
    """For the daily review: every item stuck longer than `hours`, with its reason and sweep outcome
    (times in the home zone of `where`)."""
    now = time.time() if now is None else now
    outs = outcomes(db)
    state = db.kv(STATE_KEY, {}) or {}
    loops = state.get("loops") or {}
    rows = [i for i in items(db, now, where) if i["age_s"] is not None and i["age_s"] >= hours * 3600]
    plan = planned_deferrals(db, now)
    tail = [f"planned deferrals: {plan}"] if plan else []
    if not rows:
        return [f"nothing stuck longer than {hours:g}h", *tail]
    out = []
    for i in sorted(rows, key=lambda x: -x["age_s"]):
        who = _who(i, 60)
        o = outs.get(i["key"])
        out.append(f"{who}: {i['kind']} {_dur(i['age_s'])}{_updated(i, now)}: {i['reason'] or 'no reason given'}; sweep: "
                   + (f"{o['outcome']} {_dur(now - float(o['ts']))} ago" if o
                      else f"none needed (ends by {i['ends']})" if i.get("ends") else "no outcome yet")
                   + (" (LOOP)" if i["key"] in loops else ""))
    return out + tail
