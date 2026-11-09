# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The wait-wake gate: before a waiting task's wake starts a model turn, decide whether one is needed
now, so a wake that would only hand the same wait back costs no model turn.

The gate looks at a task that handed off `waiting` and is due again (its timer ran out, its probe
passed, or another wake path made it due). In this order:

1. Must-wake events always start the turn: an escalated wake, any wake reason other than a plain
   timer or "probe passed" (failed checks, a broken probe, a reboot, a resumed resource, ...), a spec
   that changed since the last turn, a probe whose exit or output changed since the gate's last skip,
   a probe that answered anything but "not yet" (its pass or failed job keeps its model-free path),
   and the cap (`max_skips` skips in a row, or `max_skip_h` hours since the last real turn).
2. Model-free rules re-defer what is mechanically still pending: a local detached job (`ttp detach
   --check`) still running with no exit code, a lock (`ttp lock --probe`) still held by a live
   holder, `ttp checks --result` / `ttp push --result` still pending, and a probe that passed with the
   same answer the last turn already saw and handed the same wait back on (no new evidence).
3. Jev, where its `wake_gate` use is allowed (jevuse) and it is reachable, judges the rest from the
   wait, the probe's last answer, the task's last note and its next step. It skips only when it is
   confident no turn is needed.
4. Anything else wakes the model as before: Jev off, unreachable, erroring or unsure.

Each skip puts the task back to sleep with a bounded backoff and keeps the evidence it saw; the
probe loop wakes it at once when that evidence changes. Every decision is logged in `wake_gate` and
settled by the next real turn (`settle_run`): a wake is a miss when that turn only handed the wait
back; a skip is a miss when the next turn came with nothing new (a timer, the cap, a Jev or fallback
wake) and still found work. Jev's calls are logged in jev_calls as the `wake_gate` use and resolved
the same way, so jevuse.review switches the Jev step off when it does not pay. `waiting.wake_gate.
enabled` false turns the whole gate off.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import budget as bud
from . import jevuse, locks, probematch
from .db import DB

USE = "wake_gate"
DEFAULTS = {"enabled": True,
            "max_skips": 4,          # skips in a row before the task wakes anyway
            "max_skip_h": 4.0,       # hours since the last real turn before it wakes anyway
            "backoff_s": 600,        # the first re-defer when the hand-off asked for less; doubles per skip
            "max_backoff_s": 3600,   # the longest one re-defer lasts
            "jev_skip_below": 0.25}  # Jev skips only when its "a turn would find work" score is below this
SOFT_WOKE = ("", "probe passed")     # wake reasons that are no event on their own
JEV_TIMEOUT_S = 10.0
EVIDENCE_CHARS = 4000
# Clock times, dates and durations change on every probe run without saying anything new.
_VOLATILE = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d)?(\.\d+)?Z?|\b\d{1,2}:\d\d(:\d\d)?\b"
                       r"|\b\d+(\.\d+)?\s*(ms|s|sec|secs|seconds?|m|min|mins|minutes?|h|hours?)\b", re.I)


def settings(cfg: dict) -> dict:
    raw = (cfg.get("waiting") or {}).get("wake_gate")
    if raw is False:
        return {**DEFAULTS, "enabled": False}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k == "enabled":
                out[k] = v is not False and str(v).lower() not in ("off", "false", "0", "no")
            elif k in DEFAULTS:
                try:
                    out[k] = max(0.0, float(v))
                except (TypeError, ValueError):
                    pass
    return out


def evidence(rc: Any, out: str | None) -> str:
    """A probe answer's fingerprint: its exit and output, clock times and durations masked."""
    text = _VOLATILE.sub("<t>", " ".join(str(out or "")[-EVIDENCE_CHARS:].split()))
    return hashlib.sha256(f"{rc}\x00{text}".encode()).hexdigest()[:16]


def wait_key(result: dict) -> str:
    """What a waiting hand-off waits for (its waiting_for and retry_when), as the daemon compares it."""
    text = "\x00".join(" ".join(str(result.get(k) or "").split()).lower() for k in ("waiting_for", "retry_when"))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass
class Decision:
    wake: bool
    by: str                    # event, cap, rule, babysit (the chat watcher), jev, fallback
    reason: str
    delay_s: float = 0.0
    seen: str = ""
    log_id: int | None = None
    jev_call: int | None = None
    extra: dict = field(default_factory=dict)

    def note(self, prev: dict, skips: list) -> dict:
        """What the dispatched run keeps (runs.note wake_gate) for settle_run and the next decision."""
        out = {"log": self.log_id, "by": self.by, "skips": skips, "seen": self.seen, "key": wait_key(prev)}
        out.update({k: self.extra[k] for k in ("tier", "flag", "model") if self.extra.get(k)})
        return out


def pending(p, cfg: dict, probe: Any) -> str | None:
    """Why what a wait probe waits on is mechanically still pending, or None when no rule can tell."""
    if not isinstance(probe, str) or not probe.strip():
        return None
    got = bud.result_wait(probe)
    if got and got[0] == "checks":
        from .cli import CHECKS_RC, _checks_alive, _read_checks_pid
        from . import detach
        d = Path(got[1])
        rc = d / CHECKS_RC
        if rc.exists():
            return None
        if detach.pending(rc):
            return "its checks are queued for the daemon"
        return "its checks still run" if _checks_alive(_read_checks_pid(d)) else None
    if got and got[0] == "push":
        from . import push
        m = push._read(Path(got[1])) or {}
        if m.get("status") == "queued":
            return "its push is queued"
        if m.get("status") == "running" and push._pid_alive(m.get("pid")):
            return "its push still runs"
        return None
    from .daemon import _waited_jobs
    rcs = _waited_jobs(probe)
    if rcs:
        from . import detach
        states = [detach.state(rc)[0] for rc in rcs]
        if all(s in ("running", "queued") for s in states):
            return f"its detached job{'s' if len(rcs) > 1 else ''} still run{'' if len(rcs) > 1 else 's'} with no exit code"
        return None
    found, rest = locks.lock_probe_parts(probe)
    if len(found) == 1 and not rest:
        from . import lockprobe
        res, shared_mode = found[0]
        rc, said = lockprobe.answer(p, cfg, res, shared_mode)
        return f"the lock is still held ({said})" if rc == 75 else None
    return None


def own_checks(p, cfg: dict, result: dict) -> str | None:
    """Why a waiting hand-off's own `ttp checks --result` / `ttp push --result` wait is still live
    (pending: queued for the daemon, its checks or push process alive, a host checks slot wait
    included), else None: not such a wait, or its run is dead or lost, which is a real fault."""
    if result.get("status") != "waiting":
        return None
    probe = result.get("retry_when")
    got = bud.result_wait(probe)
    return pending(p, cfg, probe) if got and got[0] in ("checks", "push") else None


def log(db: DB, task: int, d: Decision, avoided_usd: float = 0.0, now: float | None = None) -> int:
    return db.x("INSERT INTO wake_gate(ts,task,decision,by,reason,jev_call,avoided_usd) VALUES(?,?,?,?,?,?,?)",
                (time.time() if now is None else now, task, "wake" if d.wake else "skip", d.by, d.reason[:300],
                 d.jev_call, float(avoided_usd or 0)))


def wake_cost(db: DB, cfg: dict, now: float | None = None) -> float:
    """What one wait wake's model turn costs: the mean of the gated wakes in the window, else of any
    wake, else jevuse's fallback price."""
    now = time.time() if now is None else now
    for like in ('%"wake_gate"%', '%"wake"%'):
        row = db.one("SELECT AVG(cost_usd) c FROM runs WHERE role!='coordinator' AND status!='running' "
                     "AND cost_usd>0 AND started>=? AND note LIKE ?", (now - jevuse.window_s(cfg), like))
        if row and row["c"]:
            return float(row["c"])
    return jevuse.WAKE_COST_FALLBACK_USD


def _last_note(run: dict | None) -> str:
    try:
        lines = [x for x in Path(run["dir"], "progress.md").read_text(errors="replace").splitlines() if x.strip()]
    except (OSError, TypeError, KeyError):
        return ""
    return lines[-1][-500:] if lines else ""


QUESTION = ("A task handed off `waiting` and is due to be woken. Would a model turn now find work to do, "
            "rather than only finding the wait still on and handing it back? Wake it when the wait looks over, "
            "something changed or failed, or the next step can be done now (the tier: light to check or report, "
            "standard for real work). Skip when nothing is new: the thing waited on is plainly still in progress "
            "or pending; a skip may name when to look next. Flag what looks wrong.")


def _ask_watcher(db: DB, cfg: dict, jev, task: dict, state: str, now: float) -> Decision | None:
    """The chat watcher's verb, acted on as given (babysit.Babysitter.decision); None when it did not
    answer (it logged and counted the fallback): Jev's score decides then."""
    ask = getattr(jev, "decision", None)
    if ask is None:
        return None
    d = ask(state, QUESTION, target=str(task["id"]), purpose=USE)
    if not d:
        return None
    skip, model = d["verb"] == "skip", str(d.get("model") or "")
    what = d["verb"] + (f" {d['tier']}" if d["verb"] == "wake" else "") + (f": {d['reason']}" if d.get("reason") else "")
    call = jevuse.record(db, USE, {"task": task["id"], "by": "babysit", "model": model, "verb": d["verb"],
                                   "tier": d.get("tier") or "", "skip": skip}, 0.0,
                         avoided_usd=wake_cost(db, cfg, now) if skip else 0.0, ref=f"task:{task['id']}",
                         changed=skip, now=now)
    extra = {"model": model}
    if d["verb"] == "wake" and d.get("parsed"):
        extra["tier"] = d["tier"]
    if d["verb"] == "flag":
        extra["flag"] = d["reason"]
    if skip and d.get("delay_s"):
        extra["delay_s"] = float(d["delay_s"])
    return Decision(not skip, "babysit", f"babysit {model}: {what}"[:300], jev_call=call, extra=extra)


def _ask_jev(db: DB, cfg: dict, s: dict, jev, task: dict, prev: dict, probe: tuple | None,
             last_run: dict | None, now: float) -> Decision | None:
    if jev is None:
        return None
    rc, out = probe if probe else (None, "")
    since = prev.get("waiting_since")
    waited = f"{(now - float(since)) / 3600:.1f} h" if isinstance(since, (int, float)) else "unknown"
    state = "\n".join([
        f"task: #{task['id']} {task['title']}",
        f"waiting for: {str(prev.get('waiting_for') or '')[:500]}",
        f"its probe (retry_when): {str(prev.get('retry_when') or '(none: a timer only)')[:500]}",
        f"the probe's last answer: exit {rc}, output: {str(out or '')[-1500:]}" if probe else
        "the probe's last answer: none recorded",
        f"next step it named: {str(prev.get('next_step') or '')[:300]}",
        f"hand-off summary: {str(prev.get('summary') or '')[:800]}",
        f"its last note: {_last_note(last_run)}",
        f"waiting since: {waited}; asked to be retried after {prev.get('retry_after_s')} s",
    ])
    try:   # the chat watcher first: it costs nothing, so jevuse's pay check does not hold it
        if got := _ask_watcher(db, cfg, jev, task, state, now):
            return got
    except Exception as e:   # the gate never loses a wake: an error wakes the model
        return Decision(True, "fallback", f"watcher unavailable: {type(e).__name__}")
    if hasattr(jev, "decision"):   # the watcher logged why it did not answer: Jev itself decides, never a re-ask
        jev = jev.jev
        if jev is None or not jev.enabled():
            return Decision(True, "fallback", "the watcher gave no answer and Jev is off")
    if not jev.enabled() or not jevuse.allowed(db, cfg, USE):
        return None
    try:
        ans = jev.decide(state=state, questions={"needed": {
            "type": "noul",
            "instructions": "A task handed off `waiting` and is due to be woken. Would a model turn now find "
                            "work to do, rather than only finding the wait still on and handing it back?",
            "criteria": {"true": "The wait looks over, something changed or failed, or the next step can be "
                                 "done now.",
                         "false": "Nothing new: the thing waited on is plainly still in progress or pending."}},
        }, purpose=USE, timeout=JEV_TIMEOUT_S)
    except Exception as e:   # the gate never loses a wake: an error wakes the model
        return Decision(True, "fallback", f"jev unavailable: {type(e).__name__}")
    if not ans:
        return Decision(True, "fallback", "jev gave no answer")
    try:
        score = float(ans.get("needed", {}).get("noul", 0.5))
    except (TypeError, ValueError, AttributeError):
        score = 0.5
    skip = score < float(s["jev_skip_below"])
    cost = getattr(jev, "last_cost", 0.0)
    call = jevuse.record(db, USE, {"task": task["id"], "needed": round(score, 2), "skip": skip}, cost,
                         avoided_usd=wake_cost(db, cfg, now) if skip else 0.0, ref=f"task:{task['id']}",
                         changed=skip, now=now)
    return Decision(not skip, "jev", f"jev needed={score:.2f}", jev_call=call)


def decide(p, cfg: dict, task: dict, prev: dict, now: float, probe: tuple | None = None,
           last_run: dict | None = None, jev=None, spec_sha: str | None = None,
           match_out: str | None = None) -> Decision | None:
    """Whether the due wake of waiting task `task` (its hand-off `prev`) starts a model turn now; None
    when the gate does not apply (it is off, or this is no daemon-tracked wait: a coordinator that ends
    a wait drops its waiting_since). `probe` is the probe's
    latest (exit, output), `last_run` the task's last run row, `spec_sha` the spec's digest now,
    `match_out` the raw output tail its probe_match reads (default: the probe's output). The
    decision is logged; a skip carries its re-defer."""
    s = settings(cfg)
    if not s["enabled"] or not isinstance(prev, dict) or prev.get("status") != "waiting" \
            or not isinstance(prev.get("waiting_since"), (int, float)):
        return None
    db = p.db
    gate = prev.get("wake_gate") if isinstance(prev.get("wake_gate"), dict) else {}
    skips = list(gate.get("log") or [])
    seen = evidence(*probe) if probe else ""
    try:
        last = json.loads(last_run["note"] or "{}") if last_run else {}
    except (ValueError, TypeError):
        last = {}
    woke = str(prev.get("woke") or "").strip().rstrip(".")
    ended = float(last_run["ended"] or last_run["started"] or now) if last_run else float(prev["waiting_since"])
    # Its probe_match reads the probe's "not yet" output as a wait: a changed progress line is no event.
    matchwait = bool(probe and probe[0] in _not_yet() and probematch.verdict(prev, probe[1] if match_out is None else match_out) == "wait")
    d = None
    if prev.get("escalated_wake"):
        d = Decision(True, "event", "an escalated wake")
    elif woke not in SOFT_WOKE:
        d = Decision(True, "event", f"woken because: {woke[:200]}")
    elif spec_sha and last.get("spec_sha") and last["spec_sha"] != spec_sha:
        d = Decision(True, "event", "its spec changed since the last turn")
    elif gate.get("seen") and seen and gate["seen"] != seen and not matchwait:
        d = Decision(True, "event", "its probe's answer changed since the gate's last skip")
    elif len(skips) >= int(s["max_skips"]) or now - ended >= float(s["max_skip_h"]) * 3600:
        d = Decision(True, "cap", f"{len(skips)} skip(s), {(now - ended) / 3600:.1f} h since its last turn")
    elif woke == "probe passed":
        lg = last.get("wake_gate") if isinstance(last.get("wake_gate"), dict) else {}
        if seen and lg.get("seen") == seen and lg.get("key") == wait_key(prev):
            d = Decision(False, "rule", "its probe passed with the answer its last turn already saw and waited on")
        else:
            d = Decision(True, "event", "its probe passed")
    elif probe and probe[0] != "timeout" and probe[0] not in _not_yet():
        # The probe answered (a pass, a failed job, a broken probe): never skipped, so a skip's
        # evidence is always a "not yet" and its pass or failure still takes its model-free path.
        d = Decision(True, "event", f"its probe answered exit {probe[0]}")
    else:
        try:
            why = ("its probe's output still matches its probe_match wait" if matchwait
                   else pending(p, cfg, prev.get("retry_when")))
        except Exception:   # a rule that cannot tell decides nothing
            why = None
        if why:
            d = Decision(False, "rule", why)
        else:
            d = _ask_jev(db, cfg, s, jev, task, prev, probe, last_run, now) or Decision(True, "fallback", "no rule or Jev decided")
    d.seen = seen
    if not d.wake:
        base = min(max(bud_retry_s(prev), float(s["backoff_s"])), float(s["max_backoff_s"]) or 3600)
        delay = min(base * 2 ** len(skips), float(s["max_backoff_s"]) or base)
        cap_at = ended + float(s["max_skip_h"]) * 3600
        d.delay_s = max(60.0, min(float(d.extra.get("delay_s") or delay), cap_at - now))
    d.log_id = log(db, task["id"], d, wake_cost(db, cfg, now) if not d.wake else 0.0, now)
    d.extra = {**d.extra, "skips": skips}
    return d


def steer(prev: dict, note: dict) -> dict:
    """The hand-off a watcher's wake runs with: its tier as the wake's tier (budget.wake_tier still caps
    it at the task's own) and its flag as why it woke, so the run reads it."""
    out = dict(prev)
    if note.get("tier"):
        out["wake_tier"] = note["tier"]
    if note.get("flag"):
        out["woke"] = f"the watcher ({note.get('model') or 'chat model'}) flagged: {note['flag']}"[:500]
    return out


def _not_yet() -> tuple:
    from .daemon import NOT_YET_RCS
    return NOT_YET_RCS


def bud_retry_s(prev: dict) -> float:
    try:
        return max(0.0, float(prev.get("retry_after_s") or 0))
    except (TypeError, ValueError):
        return 0.0


def skipped(prev: dict, d: Decision, now: float) -> dict:
    """The hand-off a skip leaves on the task: the same wait, no wake reason (the probe loop treats it
    as asleep again), and the gate's memory: its skips' log ids and the evidence it saw."""
    gate = prev.get("wake_gate") if isinstance(prev.get("wake_gate"), dict) else {}
    out = {k: v for k, v in prev.items() if k not in ("woke", "wake_reason")}
    out["wake_gate"] = {"log": list(gate.get("log") or []) + [d.log_id], "seen": d.seen,
                        "since": gate.get("since") or now, "by": d.by, "reason": d.reason[:200]}
    return out


def holds(prev: dict, rc: Any, out: str | None) -> str | None:
    """For a task asleep after a gate skip, what a new probe answer means: 'same' (keep asleep: the
    gate already judged it), 'changed' (the probe loop's usual paths, else a wake), or None (no skip
    to compare with)."""
    gate = prev.get("wake_gate") if isinstance(prev, dict) else None
    if not isinstance(gate, dict) or not gate.get("seen") or not gate.get("log"):
        return None
    return "same" if evidence(rc, out) == gate["seen"] else "changed"


def _resolve(db: DB, row_id: int, outcome: str, note: str, now: float) -> None:
    db.x("UPDATE wake_gate SET outcome=?, outcome_ts=?, note=? WHERE id=? AND outcome IS NULL",
         (outcome, now, note[:300], row_id))


def settle_run(db: DB, note: dict, prev: dict, result: dict, waiting: bool, now: float | None = None) -> int:
    """Settle the gate decisions behind a finished run (its runs.note `wake_gate`): the wake that
    started it is a miss when the run only handed the same wait back; the skips before it are misses
    when that wake came with nothing new (cap, timer, Jev or fallback) and the run found work. Jev's
    calls among them are resolved in jev_calls too. Returns how many it settled."""
    g = note.get("wake_gate") if isinstance(note, dict) else None
    if not isinstance(g, dict):
        return 0
    now = time.time() if now is None else now
    back = bool(waiting and isinstance(result, dict) and wait_key(result) == wait_key(prev))
    n = 0
    rows = {r["id"]: r for r in db.q(
        f"SELECT * FROM wake_gate WHERE outcome IS NULL AND id IN ({','.join('?' * (1 + len(g.get('skips') or [])))})",
        tuple([g.get("log")] + list(g.get("skips") or [])))}
    w = rows.get(g.get("log"))
    if w:
        _resolve(db, w["id"], "miss" if back else "right",
                 "the turn only handed the wait back" if back else "the turn did work", now)
        if w["jev_call"]:
            jevuse.resolve(db, int(w["jev_call"]), not back, "handed the wait back" if back else "found work", now=now)
        n += 1
    soft = g.get("by") in ("cap", "babysit", "jev", "fallback")
    for sid in g.get("skips") or []:
        r = rows.get(sid)
        if not r:
            continue
        miss = soft and not back
        _resolve(db, r["id"], "miss" if miss else "right",
                 "the next turn came with nothing new and found work" if miss else
                 "the next turn handed the wait back" if back else "the next turn came on a new event", now)
        if r["jev_call"]:
            jevuse.resolve(db, int(r["jev_call"]), not miss, "skip " + ("missed work" if miss else "held"), now=now)
        n += 1
    return n


def stats(db: DB, cfg: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    since = now - jevuse.window_s(cfg)
    r = db.one("SELECT COUNT(*) n, SUM(decision='skip') skips, SUM(decision='skip' AND by='rule') rule_skips, "
               "SUM(decision='skip' AND by='jev') jev_skips, SUM(decision='skip' AND by='babysit') babysit_skips, SUM(decision='wake') wakes, "
               "SUM(decision='skip' AND outcome='miss') skip_miss, SUM(decision='wake' AND outcome='miss') wake_miss, "
               "SUM(CASE WHEN decision='skip' AND COALESCE(outcome,'')!='miss' THEN avoided_usd ELSE 0 END) saved "
               "FROM wake_gate WHERE ts>=?", (since,))
    j = jevuse.stats(db, cfg, now).get(USE) or {}
    return {k: (float(r[k] or 0) if k == "saved" else int(r[k] or 0)) for k in
            ("n", "skips", "rule_skips", "jev_skips", "babysit_skips", "wakes", "skip_miss", "wake_miss", "saved")} | {
        "jev_calls": int(j.get("calls", 0)), "jev_cost": float(j.get("cost", 0.0))}


def line(db: DB, cfg: dict, now: float | None = None) -> str:
    """The daily review's line: gate decisions, skips (rules, Jev), misses, Jev's cost and the saving."""
    s = stats(db, cfg, now)
    if not s["n"]:
        return ""
    days = jevuse.window_s(cfg) / 86400
    net = s["saved"] - s["jev_cost"]
    per = f"{_usd(s['jev_cost'] / s['jev_calls'])} a Jev call" if s["jev_calls"] else "no Jev calls"
    off = USE in (db.kv(jevuse.OFF_KEY, {}) or {}) or jevuse.mode(cfg, USE) == "off"
    return (f"Wait-wake gate over the last {days:g} d: {s['n']} decisions, {s['skips']} skipped "
            f"({s['rule_skips']} by rules, {s['babysit_skips']} by the watcher, {s['jev_skips']} by Jev), {s['wakes']} woke; misses: "
            f"{s['skip_miss']} skips, {s['wake_miss']} wakes; Jev {s['jev_calls']} calls {_usd(s['jev_cost'])} "
            f"({per}){' (Jev step off)' if off else ''}; estimated saving {_usd(s['saved'])}, net {_usd(net)}"
            + (f", {_usd(net / s['skips'])} a skip" if s["skips"] else ""))


def _usd(v: float) -> str:
    return f"-${-v:.2f}" if v < 0 else f"${v:.2f}"
