# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Self-efficiency audit, for the daily review: what the last day cost the user and the budget that
the project could have saved. Model-free; the review grades it.

- every ask sent (its question, blocking reason, recommendation, the user's answer and how long it
  took), asks the ask gate refused, and answers that handed the decision back ("decide yourself");
- worker runs that failed (a reboot, sleep, lost network or a resume is not a failure), runs lost
  to those causes apart (one a later run resumed kept its work: not counted as lost), tasks run
  again after a failed attempt, and tasks that continue an earlier one (`continues:<id>`), with the
  spend of each; and per continue chain (chaincap), its hops and spend, the hops the daemon
  queued and the hops a cap refused or a reason passed;
- coordinator turns that decided nothing (no action but `noop`), and idle and
  starve wakes (woken by the idle or idle-slot timer alone, with no message or event), with their cost,
  and turns the daemon skipped as repeats of one that decided nothing (turngate.py) or as quiet while
  every open task was held (quietgate.py), with the saving; blocker sweeps the model ran and those
  skipped model-free (sweepskip.py), with the saving;
- spend per finished task: the workers' own, and the coordinator's day spread over them; $ per
  done task by kind and tier;
- tasks blocked or waiting longer than `blocked_h` hours now, longest first;
- review loops: per change whose review moved in the window, its review rounds and their spend
  plus the fixes they asked for (a review's `auto_review:<task>` and `continues:<review>` labels);
- reviews finished (reviewstats): count, spend and failures by diff size (all and non-test lines), tier and first review
  against re-review, for retesting review routing;
- overrides held longer than `blocked_h`: resource pauses and active mutes, and temporary instructions
  whose end only a model can judge that were listed as possibly over a day or more ago; and at any
  age, a resource pause with no end or whose `until` has passed;
- upstream fixes shipped but not confirmed yet (how many, the oldest's age), and resolutions closed
  as 'confirmed by quiet' in the window;
- over 7 days, each Restrictions item that only blocked work: blocked runs and asks citing it reached
  `review.rule_blocks_min` while no refused action cited it (restriction_audit.only_blocks_lines).
- the check pre-stage derived from frequent detached checks failures (checkprestage): its last list
  and the failures it caught before detaching (each a worker wake saved).

An empty day is one line, NOTHING. `ttp audit` prints the same lines, or `report` as JSON. It reads
only the project's own database and harness files."""
from __future__ import annotations

import json
import time
from typing import Any

from . import budget, chaincap, checkprestage, correction, ends, obligations, quietgate, reviewstats, schedule, sharedchecks, suitecost, sweepskip, turngate, unblock, upstream
from .db import DB, continues_id
from .project import Project

WINDOW_S = 86400
BLOCKED_H = 12.0   # review.blocked_long_h: a task blocked this long is listed
NOTHING = "nothing to grade"
TOP = 5   # rows listed per section; the rest are counted


def _note(raw: Any) -> dict:
    try:
        note: Any = json.loads(raw or "{}")
    except ValueError:
        return {}
    return note if isinstance(note, dict) else {}


def _short(text: str, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 3] + "..."


def _dur(s: float) -> str:
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min"


def asks(db: DB, since: float, now: float, p: Project | None = None) -> list[dict]:
    """Asks sent since `since`, with their text, recommendation and answer (unblock.asks links them)."""
    from .coordinator import _REC_NOTE, _ask_question
    rows = unblock.asks(db, since, now, chat_min=(p.config().get("notify") or {}).get("chat_min_severity") if p else None)
    if not rows:
        return []
    msgs = {m["id"]: m for m in db.q(f"SELECT id, ts, text FROM messages WHERE id IN ({','.join('?' * len(rows))})",
                                     [r["id"] for r in rows])}
    out = []
    for r in rows:
        m = msgs.get(r["id"]) or {"ts": now, "text": ""}
        rec = m["text"].split(_REC_NOTE, 1)[1].split("\n\n")[0] if _REC_NOTE in m["text"] else ""
        out.append({**r, "question": _short(_ask_question(m["text"])), "recommendation": _short(rec, 100),
                    "wait_s": (r["answered_at"] - m["ts"]) if r.get("answered_at") else None,
                    "open_s": None if r.get("answered_at") else (r["landed"]["ts"] if r.get("landed") else now) - m["ts"]})
    return out


def rejected_asks(db: DB, since: float) -> list[str]:
    """ask_user actions the ask gate refused since `since`: the reason each got."""
    out = []
    for e in db.q("SELECT text, data FROM events WHERE kind='rejected_actions' AND ts>=? ORDER BY id", (since,)):
        try:
            items = json.loads(e["data"]) if e["data"] else None
        except ValueError:
            items = None
        if not isinstance(items, list):   # recorded before the list was kept: one joined line
            items = [e["text"]] if str(e["text"]).startswith("ask_user:") else []
        out += [str(x) for x in items if str(x).startswith("ask_user:")]
    return out


def failed_runs(db: DB, since: float) -> list[dict]:
    """Worker runs that ended since `since` in a failure of their own (budget.wasted: what the
    runaway guard counts as waste), most costly first."""
    rows = db.q("SELECT r.id, r.task, r.status, r.cost_usd, r.note, t.title FROM runs r LEFT JOIN tasks t "
                "ON t.id=r.task WHERE r.role!='coordinator' AND r.ended>=? AND r.status NOT IN ('ok','running')",
                (since,))
    return sorted([r for r in rows if budget.wasted(r)], key=lambda r: -float(r["cost_usd"] or 0))


def retried_tasks(db: DB, since: float) -> list[dict]:
    """Tasks run since `since` after at least one failed attempt."""
    return db.q("SELECT id, title, attempts, status, spent_usd FROM tasks WHERE attempts>1 AND id IN "
                "(SELECT task FROM runs WHERE role!='coordinator' AND started>=?) ORDER BY spent_usd DESC", (since,))


def lost_runs(db: DB, since: float) -> dict[str, dict]:
    """Worker runs that ended since `since` without an outcome for a cause other than their own (a
    reboot, sleep, lost network, a resume): {cause: {n, usd, kept_n, kept_usd}}. Not failures, but their
    spend is gone, except for a run a later run resumed (its session continued, `resumes`): its work
    was kept, and only its in-flight step and the context re-read on resume were lost, which the
    resuming run's own spend shows. Those are `kept_n` runs worth `kept_usd`, not counted as lost."""
    out: dict[str, dict] = {}
    rows = db.q("SELECT id, status, note, cost_usd FROM runs WHERE role!='coordinator' AND ended>=? AND status IN "
                f"({','.join('?' * len(budget.WASTED))})", (since, *budget.WASTED))
    kept = db.resumed_run_ids(min(int(r["id"]) for r in rows)) if rows else set()
    for r in rows:
        cause = _note(r["note"]).get("not_waste")
        if cause and cause != "handoff":   # a hand-off that stood is an outcome
            c = out.setdefault(str(cause), {"n": 0, "usd": 0.0, "kept_n": 0, "kept_usd": 0.0})
            c["n"] += 1
            c["usd"] += float(r["cost_usd"] or 0)
            if int(r["id"]) in kept:
                c["kept_n"] += 1
                c["kept_usd"] += float(r["cost_usd"] or 0)
    return out


def continued_tasks(db: DB, since: float, fixes: bool = False) -> list[dict]:
    """Work tasks added since `since` that continue an earlier one (`continues:<id>`; review rounds are
    review_loops'), most costly first: {id, title, of, of_usd, usd}. Continuations only, or with `fixes`
    only the review fixes (chaincap.is_review_fix), whose spend the review-loop line counts."""
    out = []
    for t in db.q("SELECT id, title, labels, spent_usd FROM tasks WHERE kind!='review' AND created>=? "
                  "AND labels LIKE '%continues:%'", (since,)):
        of = continues_id(t)
        old = db.task(of) if of is not None else None
        if old and chaincap.is_review_fix(t) == fixes:
            out.append({"id": t["id"], "title": t["title"], "of": of, "of_usd": float(old["spent_usd"] or 0),
                        "usd": float(t["spent_usd"] or 0)})
    return sorted(out, key=lambda x: -(x["of_usd"] + x["usd"]))


def coordinator_turns(db: DB, since: float) -> dict:
    """Coordinator turns since `since`: all, those that decided nothing, idle wakes and starve wakes,
    with costs. A turn decided nothing when its note says `decided` 0 (turns before that was logged
    are not counted); an idle or starve wake was due to the
    idle timer or the idle-slot (starve) timer with no message or event in its batch."""
    rows = db.q("SELECT note, cost_usd FROM runs WHERE role='coordinator' AND status='ok' AND started>=?", (since,))
    out = {"n": len(rows), "usd": 0.0, "noop": 0, "noop_usd": 0.0, "idle": 0, "idle_usd": 0.0,
           "starve": 0, "starve_usd": 0.0, "logged": 0}
    for r in rows:
        note, usd = _note(r["note"]), float(r["cost_usd"] or 0)
        out["usd"] += usd
        due = "starve" if note.get("wake_due") == "starve" else "idle" if note.get("wake_due") else None
        if due and not note.get("messages") and not note.get("events"):
            out[due] += 1
            out[f"{due}_usd"] += usd
        if "decided" in note:
            out["logged"] += 1
            if not note["decided"]:
                out["noop"] += 1
                out["noop_usd"] += usd
    return out


def finished(db: DB, since: float) -> list[dict]:
    """Tasks that ended (done or failed) since `since`, most costly first."""
    return db.q("SELECT id, title, status, kind, tier, spent_usd FROM tasks WHERE status IN ('done','failed') "
                "AND updated>=? ORDER BY spent_usd DESC", (since,))


def per_outcome(done: list[dict]) -> list[dict]:
    """$ per done task by kind and tier, from `finished` rows, most costly first: {kind, tier, n, usd, each}."""
    groups: dict[tuple, dict] = {}
    for t in done:
        if t["status"] == "done":
            g = groups.setdefault((t["kind"], t["tier"]), {"kind": t["kind"], "tier": t["tier"], "n": 0, "usd": 0.0})
            g["n"] += 1
            g["usd"] += float(t["spent_usd"] or 0)
    return sorted(({**g, "each": g["usd"] / g["n"]} for g in groups.values()), key=lambda g: -g["usd"])


def stuck_long(db: DB, now: float, hours: float) -> list[dict]:
    """Tasks blocked or waiting now (not on their own work) for more than `hours`, longest first."""
    rows = [r for r in unblock.inventory(db, now) if r["kind"] in ("blocked", "waiting")
            and r["state_s"] is not None and r["state_s"] > hours * 3600]
    return sorted(rows, key=lambda r: -r["state_s"])


def _label(labels: Any, prefix: str) -> int | None:
    try:
        vals = json.loads(labels or "[]")
    except ValueError:
        return None
    for v in vals if isinstance(vals, list) else []:
        if isinstance(v, str) and v.startswith(prefix) and v[len(prefix):].isdigit():
            return int(v[len(prefix):])
    return None


def review_loops(db: DB, since: float) -> list[dict]:
    """Per change whose review chain moved since `since`, most costly first: {change, title, rounds,
    review_usd, fix_usd}. A chain is a first review (`auto_review:<change>`) and the reviews that
    continue it (`continues:<review>`); the fixes are the other tasks those reviews checked."""
    reviews = {r["id"]: r for r in db.q("SELECT id, labels, spent_usd, updated FROM tasks WHERE kind='review'")}
    chains: dict[int, list[dict]] = {}
    for r in reviews.values():
        root, seen = r, {r["id"]}
        while (prev := _label(root["labels"], "continues:")) in reviews and prev not in seen:
            seen.add(prev)
            root = reviews[prev]
        chains.setdefault(root["id"], []).append(r)
    out = []
    for root_id, chain in chains.items():
        change = _label(reviews[root_id]["labels"], "auto_review:")
        if change is None or not any(float(r["updated"] or 0) >= since for r in chain):
            continue
        fixes = {_label(r["labels"], "auto_review:") for r in chain} - {change, None}
        fix_usd = sum(float(t["spent_usd"] or 0) for t in db.q(
            f"SELECT spent_usd FROM tasks WHERE id IN ({','.join('?' * len(fixes))})", list(fixes))) if fixes else 0.0
        task = db.task(change) or {}
        out.append({"change": change, "title": task.get("title") or "", "rounds": len(chain),
                    "review_usd": sum(float(r["spent_usd"] or 0) for r in chain), "fix_usd": fix_usd})
    return sorted(out, key=lambda x: -(x["review_usd"] + x["fix_usd"]))


def overrides(p: Project | None, db: DB, now: float, hours: float) -> list[str]:
    """Overrides held longer than `hours`: resource pauses, active mutes (not those of a schedule that is
    off or gone: they mute nothing), and temporary instructions whose end only a model can judge, listed
    as possibly over at least ends.RECHECK_S ago."""
    from . import machines
    from .screen import mutes, off_schedule_mute
    out = []
    paused = db.paused_resources(shared=False)
    known = machines.load() if paused else {}
    for name, v in sorted(paused.items()):
        if machines.decommissioned_user_pause(name, v, known):
            continue   # the user's pause of a retired machine: held as set, nothing to re-judge
        age = now - float(v.get("since") or now)
        until = float(v["until"]) if isinstance(v.get("until"), (int, float)) else None
        end = ("with no end" if until is None and not v.get("end_when")
               else f"its end passed {_dur(now - until)} ago" if until is not None and until <= now else "")
        if age > hours * 3600 or end:
            out.append(f"resource {name} paused {_dur(age)} by {v.get('by') or 'unknown'}"
                       + (f" ({_short(v['reason'], 80)})" if v.get("reason") else "") + (f", {end}" if end else ""))
    for m in mutes(db, now):
        age = now - float(m.get("since") or now)
        if age > hours * 3600 and not off_schedule_mute(db, m):
            out.append(f"mute of {m.get('source')} {str(m.get('match'))[:60]!r} held {_dur(age)}, "
                       f"ends in {_dur(max(0.0, float(m.get('until') or now) - now))}")
    if p is not None:
        listed, broken = db.kv(ends.LISTED_KEY) or {}, db.kv(ends.BROKEN_KEY) or {}
        for item in ends.temporaries(p):
            first = listed.get(item["key"])
            if item["key"] in broken or (first is not None and now - float(first) >= ends.RECHECK_S):
                out.append(f"{item['what']} possibly over ({_short(ends.describe(item['end'], p), 100)}"
                           + (f"; its probe is broken: {_short(broken[item['key']], 60)}" if item["key"] in broken else "")
                           + ")")
    return out


def _more(rows: list, shown: int = TOP) -> str:
    return f"; and {len(rows) - shown} more" if len(rows) > shown else ""


def lines(db: DB, now: float | None = None, window_s: float = WINDOW_S, blocked_h: float = BLOCKED_H,
          p: Project | None = None) -> list[str]:
    """The daily review's 'Self-efficiency audit' lines for the last `window_s`; [NOTHING] on an empty
    day. With the project `p`, its temporary instructions are checked too."""
    now = time.time() if now is None else now
    since = now - window_s
    out: list[str] = []
    sent = asks(db, since, now, p)
    for a in sent:
        rec = f"; recommended: {a['recommendation']}" if a["recommendation"] else ""
        if a["answered"]:
            how = f"answered after {_dur(a['wait_s'])}: \"{a['answer']}\""
            how += f" (handed back: \"{a['handback']}\")" if a["handback"] else ""
        elif a.get("landed"):
            how = (f"resolved: its work landed ({', '.join(f'#{t}' for t in a['landed']['tasks'])}), "
                   f"after {_dur(a['open_s'])} with no answer")
        else:
            how = f"no answer yet, open {_dur(a['open_s'])}"
        out.append(f"ask {a['id']} ({a['blocking']}): \"{a['question']}\"{rec}; {how}")
    refused = rejected_asks(db, since)
    if refused:
        out.append(f"asks refused by the ask gate: {len(refused)}: "
                   + "; ".join(_short(x, 140) for x in refused[:TOP]) + _more(refused))
    back = [a for a in sent if a["handback"]]
    if sent:
        landed = sum(bool(a.get("landed")) and not a["answered"] for a in sent)
        out.append(f"asks: {len(sent)} sent, {sum(a['answered'] for a in sent)} answered, {len(back)} handed back"
                   + (f", {landed} resolved as their work landed" if landed else ""))
    fails = failed_runs(db, since)
    if fails:
        usd = sum(float(r["cost_usd"] or 0) for r in fails)
        out.append(f"failed worker runs: {len(fails)}, ${usd:.2f}: " + ", ".join(
            f"run {r['id']} task #{r['task']} {_short(r['title'] or '', 50)} {r['status']} ${float(r['cost_usd'] or 0):.2f}"
            for r in fails[:TOP]) + _more(fails))
    again = retried_tasks(db, since)
    if again:
        out.append(f"tasks run again after a failed attempt: {len(again)}: " + ", ".join(
            f"#{t['id']} {_short(t['title'], 50)} (attempt {t['attempts']}, {t['status']}, ${float(t['spent_usd'] or 0):.2f})"
            for t in again[:TOP]) + _more(again))
    lost = lost_runs(db, since)
    if lost:
        kept_n, kept_usd = sum(c["kept_n"] for c in lost.values()), sum(c["kept_usd"] for c in lost.values())
        kept = (f" (+ {kept_n} resumed, work kept, ${kept_usd:.2f} not lost: only the in-flight step and the "
                "re-read on resume, in the resuming run's spend)") if kept_n else ""
        out.append(f"worker runs lost (not failures): {sum(c['n'] for c in lost.values())}, "
                   f"${sum(c['usd'] for c in lost.values()) - kept_usd:.2f}{kept}: " + ", ".join(
                       f"{c['n']} to {cause} ${c['usd'] - c['kept_usd']:.2f}"
                       + (f" ({c['kept_n']} resumed, work kept)" if c["kept_n"] else "")
                       for cause, c in sorted(lost.items())))
    cont, fixed = continued_tasks(db, since), continued_tasks(db, since, fixes=True)
    if cont or fixed:
        out.append(f"tasks continued from an earlier one: {len(cont)}"
                   + (f" (+ {len(fixed)} review fixes, ${sum(t['usd'] for t in fixed):.2f}, counted in the review-loop line)"
                      if fixed else "") + (": " if cont else "") + ", ".join(
            f"#{t['id']} {_short(t['title'], 50)} continues #{t['of']} (${t['of_usd']:.2f} + ${t['usd']:.2f})"
            for t in cont[:TOP]) + _more(cont))
    if fixes := correction.audit_line(db, since):
        out.append(fixes)
    if promised := obligations.audit_line(db, since, now):
        out.append(promised)
    if chains := chaincap.audit_line(db, since, TOP):
        out.append(chains)
    if shared := sharedchecks.audit_line(db, since):
        out.append(shared)
    if suite := suitecost.audit_line(db, now):
        out.append(suite)
    if pre := checkprestage.audit_line(db, since):
        out.append(pre)
    turns = coordinator_turns(db, since)
    if turns["n"]:
        noop = (f"{turns['noop']} decided nothing (${turns['noop_usd']:.2f})" if turns["logged"] else
                "turns that decided nothing: not logged")
        part = f" of {turns['logged']} logged" if turns["logged"] and turns["logged"] < turns["n"] else ""
        out.append(f"coordinator turns: {turns['n']}, ${turns['usd']:.2f}; {noop}{part}; "
                   f"{turns['idle']} idle wakes (${turns['idle_usd']:.2f}), "
                   f"{turns['starve']} starve wakes (${turns['starve_usd']:.2f})")
    cache = budget.coordinator_cache(db, now, window_s)["window"]
    if cache["turns"]:
        out.append(budget.coordinator_cache_line(cache))
    skips = turngate.skipped(db, since)
    if skips["n"]:
        out.append(f"coordinator turns skipped as repeats of a turn that decided nothing: {skips['n']}, "
                   f"~${skips['usd']:.2f} saved")
    quiet = quietgate.skipped(db, since)
    if quiet["n"]:
        out.append(f"coordinator turns skipped ({quietgate.QUIET}): {quiet['n']}, ~${quiet['usd']:.2f} saved")
    sweeps = sweepskip.counts(db, since, now)
    if sweeps["model"] or sweeps["skipped"]:
        out.append(f"blocker sweeps: {sweeps['model']} by the model, {sweeps['skipped']} skipped model-free "
                   f"(unchanged, every item ends without a turn), ~${sweeps['usd']:.2f} saved")
    done = finished(db, since)
    if done:
        work = sum(float(t["spent_usd"] or 0) for t in done)
        out.append(f"finished tasks: {len(done)} ({sum(t['status'] == 'done' for t in done)} done), workers ${work:.2f} "
                   f"(${work / len(done):.2f} each), coordinator ${turns['usd']:.2f} in the window "
                   f"(${turns['usd'] / len(done):.2f} per finished task); most costly: " + ", ".join(
                       f"#{t['id']} {_short(t['title'], 50)} {t['status']} ${float(t['spent_usd'] or 0):.2f}"
                       for t in done[:3]))
        groups = per_outcome(done)
        if groups:
            out.append("$ per done task by kind and tier: " + ", ".join(
                f"{g['kind']}/{g['tier']} {g['n']} at ${g['each']:.2f}" for g in groups))
    stuck = stuck_long(db, now, blocked_h)
    if stuck:
        out.append(f"blocked or waiting longer than {blocked_h:g} h now: {len(stuck)}: " + ", ".join(
            f"#{r['task']} {_short(r['title'], 50)} {r['kind']} {_dur(r['state_s'])}" for r in stuck[:TOP])
            + _more(stuck))
    loops = review_loops(db, since)
    if loops:
        rev, fix = sum(x["review_usd"] for x in loops), sum(x["fix_usd"] for x in loops)
        out.append(f"review loops: {len(loops)} changes, reviews ${rev:.2f} + fixes ${fix:.2f} "
                   f"(${(rev + fix) / len(loops):.2f} per change); most costly: " + ", ".join(
                       f"#{x['change']} {_short(x['title'], 50)} {x['rounds']} round{'s' if x['rounds'] > 1 else ''} "
                       f"${x['review_usd']:.2f} + fixes ${x['fix_usd']:.2f}" for x in loops[:3]))
    if routed := reviewstats.audit_line(db, since):
        out.append(routed)
    low = schedule.below_suggestion(db)
    if low:
        out.append(f"schedule caps under their suggestion: {len(low)}: " + "; ".join(low[:TOP]) + _more(low))
    from . import progress
    if stuck := progress.audit_line(db, since, now):
        out.append(stuck)
    held = overrides(p, db, now, blocked_h)
    if held:
        out.append(f"overrides held long: {len(held)}: " + "; ".join(held[:TOP]) + _more(held))
    if p is not None:
        try:
            n, oldest = upstream.unconfirmed_summary(p.name, now)
        except OSError:
            n, oldest = 0, 0.0
        if n:
            out.append(f"upstream fixes shipped, not confirmed: {n}, oldest {_dur(oldest)} since resolved")
        try:
            q = upstream.quiet_confirmed(p.name, since)
        except OSError:
            q = 0
        if q:
            out.append(f"upstream fixes confirmed by quiet: {q}")
        from . import restriction_audit
        least = int((p.config().get("review") or {}).get("rule_blocks_min", restriction_audit.BLOCKS_MIN))
        out += restriction_audit.only_blocks_lines(p, now, least)
        from .coordinator import worker_pinned_line
        if pinned := worker_pinned_line(p):
            out.append(pinned)
    return out or [NOTHING]


def report(db: DB, now: float | None = None, window_s: float = WINDOW_S, blocked_h: float = BLOCKED_H,
           p: Project | None = None) -> dict:
    """Every collector's rows for the last `window_s`, for `ttp audit --json`."""
    now = time.time() if now is None else now
    since = now - window_s
    done = finished(db, since)
    return {"window_h": window_s / 3600, "asks": asks(db, since, now, p), "asks_refused": rejected_asks(db, since),
            "failed_runs": failed_runs(db, since), "lost_runs": lost_runs(db, since),
            "retried_tasks": retried_tasks(db, since), "continued_tasks": continued_tasks(db, since),
            "review_fix_tasks": continued_tasks(db, since, fixes=True),
            "coordinator_turns": coordinator_turns(db, since),
            "coordinator_cache": budget.coordinator_cache(db, now, window_s)["window"], "turns_skipped": turngate.skipped(db, since), "turns_quiet": quietgate.skipped(db, since), "sweeps": sweepskip.counts(db, since, now),
            "finished": done, "per_outcome": per_outcome(done),
            "stuck": stuck_long(db, now, blocked_h), "review_loops": review_loops(db, since),
            "reviews": {**reviewstats.summary(db, since), "rows": reviewstats.rows(db, since)},
            "overrides": overrides(p, db, now, blocked_h), "promises": obligations.promise_outcomes(db, since, now),
            "repeated_instructions": correction.repeat_summary(db, since)}
