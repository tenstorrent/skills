"""Coordinator wake decisions: counted, and (behind rings.coord_wake_settle) routine batches settled by rule.

Counter (always on: it changes no behaviour). Every pre-screen decision on a coordinator wake is one row
in the project DB's wake_decisions table: the gate (`triage` for waketriage, `quiet` for quietgate,
`turn` for turngate, `rule` for this module), its verb (`skip` or `wake`), the
event kinds and ids, and the dollars a skip avoided at the median coordinator wake cost of the last 7
days. `summary` and `audit_line` read it for `ttp status` and the daily review.

Rule settle (flag `coord_wake_settle`, config coordinator.wake_settle, off by default). A batch whose
every event is one of these is settled without a model run, the events marked handled:
- a plain task_done (no follow-up proposed, nothing depends on it, no waiting parent; the daemon's
  _plain_done) of a code, work or harness task with no reply chat, no obligation, no failure text, and a
  result that names no `continue:` or `partial:`;
- an upstream_note at info to normal severity: the daemon files it itself;
- a re-check: an observation, idle_capacity, steer_unread, ready_not_started, machine_change_overdue,
  charter_retry or stale_holds event whose kind, task and fingerprint (else text) a coordinator turn
  already met within coordinator.wake_settle_recheck_s (default 6 h), and that was rule-settled fewer
  than RECHECK_MAX times since, so a condition that persists still reaches a turn.
Never settled: a user or chat source, high or critical severity, a must-act item, promise or obligation
text, a review, failure, restriction or outage (never_reason, kinds aside). One event that
does not qualify sends the whole batch to the turn, as it is. Urgent user messages never pass here.

Probe-pass spec edit (same flag, gate `probe`): a waiting task whose start_when or retry_when probe passed and
whose spec changed since its hand-off normally wakes at its own tier with the change (the daemon's
_probe_passed). When the change is only text recording that pass (`pass_record_only`: `## Update` blocks
whose every line says a wait, probe, gate, start_when or retry_when passed, nothing else), the task goes
through the normal probe-pass path instead (on_pass recorded model-free where clear). Anything else about
the edit still wakes it. Counted in wake_decisions with no dollars claimed: the saving is the escalated wake.

Reopened: a rule-settled event whose task (else kind and fingerprint) has a later event, within
REOPEN_S, that a coordinator turn handled rather than a rule. The target is under 2% of rule settles.
"""
from __future__ import annotations

import json
import re
import statistics
import time

from .eventhold import SEVERITIES


# A routine batch the rules may settle; never_reason keeps out what a turn must see.
ROUTINE_KINDS = ("task_done", "upstream_note", "observation")
_FAIL = re.compile(r"\bfail(?:s|ed|ure|ures|ing)?\b|\berror(?:s|ed)?\b|\bcrash(?:ed|es)?\b", re.I)
_RESTRICT = re.compile(r"\brestrict", re.I)
_PROMISE = re.compile(r"\bpromis(?:e|es|ed)\b|\bobligation|\bmust[- ]act", re.I)
_REVIEW = re.compile(r"\bverdict\b|\breview (?:result|passed|failed|finding)", re.I)
_OUTAGE = re.compile(r"\boutage|\boffline\b|\bunreachable\b|\bunhealthy\b|\bis down\b|\bwent down\b|\breboot", re.I)
_MACHINE_SRC = re.compile(r"machine|host|outage|health|broker|device|disk|ssh|reboot|net", re.I)
_MACHINE = re.compile(r"\b(?:machine|host|node|device|broker|disk)s?\b", re.I)


def never_reason(e: dict, task: dict | None, owners) -> str | None:
    """Why the runtime keeps event `e` (with its text) from being settled, or None."""
    text = str(e.get("text") or "")
    source = str(e.get("source") or "")
    if e.get("kind") not in ROUTINE_KINDS:
        return "not a routine kind"
    if e.get("severity") not in SEVERITIES:
        return "high or critical severity"
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


TABLE = "wake_decisions"
MEDIAN_KEY = "wake_settle_median"   # kv: {"at", "usd"}: the median coordinator wake cost, cached an hour
MEDIAN_TTL_S = 3600.0
MEDIAN_WINDOW_S = 7 * 86400.0
DEFAULT_USD = 0.58   # used until the project has coordinator runs of its own
RECHECK_S = 6 * 3600.0
RECHECK_MAX = 3
REOPEN_S = 86400.0
TASK_KINDS = ("code", "work", "harness")
RECHECK_KINDS = ("observation", "idle_capacity", "steer_unread", "ready_not_started", "machine_change_overdue",
                 "charter_retry", "stale_holds")
_OPEN_RESULT = re.compile(r"\bcontinue:|\bpartial:|\bcells unreported\b", re.I)



def on(p) -> bool:
    """Whether rule settling is on for project `p` (ring flag rings.coord_wake_settle; off by default)."""
    from . import costlevers
    return costlevers.lever_on(p, "coord_wake_settle")


def median_usd(db, now: float) -> float:
    """The median cost of the project's coordinator wakes over the last 7 days (cached an hour)."""
    seen = db.kv(MEDIAN_KEY, {}) or {}
    if now - float(seen.get("at") or 0) < MEDIAN_TTL_S and seen.get("usd") is not None:
        return float(seen["usd"])
    costs = [float(r["cost_usd"] or 0) for r in db.q(
        "SELECT cost_usd FROM runs WHERE role='coordinator' AND started>? AND cost_usd>0", (now - MEDIAN_WINDOW_S,))]
    usd = round(statistics.median(costs), 4) if costs else DEFAULT_USD
    db.set_kv(MEDIAN_KEY, {"at": now, "usd": usd})
    return usd


def record(db, gate: str, verb: str, evs: list[dict], now: float | None = None, usd: float | None = None,
           why: str = "", kinds: list[str] | None = None) -> None:
    """Count one pre-screen decision. A skip avoids one wake at the median cost unless `usd` says else."""
    try:
        now = time.time() if now is None else now
        if usd is None:
            usd = median_usd(db, now) if verb == "skip" else 0.0
        kinds = kinds or (sorted({str(e.get("kind")) for e in evs}) if evs else ["wake_due"])
        db.x(f"INSERT INTO {TABLE}(ts, gate, verb, kinds, events, usd, why) VALUES(?,?,?,?,?,?,?)",
             (now, gate, verb, json.dumps(kinds), json.dumps(sorted(int(e["id"]) for e in evs)), float(usd or 0),
              why[:300]))
    except Exception:   # counting never stops a wake decision
        pass


_UPDATE_SPLIT = re.compile(r"^(?=## Update\b)", re.M)
_UPDATE_HEAD = re.compile(r"## Update(?: \d{4}-\d{2}-\d{2} \d{2}:\d{2} [A-Za-z0-9+-]{1,8})?(?: \(turn [\w.-]{1,24}\))?[ \t]*")
_PASS_LINE = re.compile(r"^(?:update\s*:\s*)?(?:the |its |this task's |your )?(?:start_when|retry_when|wait|probe|gate)"
                        r"(?:\s+\w+){0,2}\s+(?:passed|has passed|is met|was met|now holds|holds now|is true|exited 0)"
                        r"(?:\s*\([^)\n]{0,40}\))?[\s.!]*$", re.I)
_NOT_PASS = re.compile(r"\b(?:user msg|correction|never|must|do not|don't|instead|cancel|priority|restrict|freeze|"
                       r"start from|do only|proceed|ack|where:|no_push|premise|urgent)\b|#\d+ msg|\bmsg #", re.I)


def pass_record_only(spec_base: str, seen: str, seen_len, title: str = "") -> bool:
    """Whether a waiting task's spec changed since its hand-off only by text recording a passed probe.
    `spec_base` is the current prompts.spec_base, `seen` the digest the hand-off slept on and `seen_len`
    the length of its spec base then (the daemon stores it): the current base must start with exactly that
    spec (its digest equals `seen`), and what follows must be `## Update` blocks (or nothing), each with a
    bare header (only the harness's time and turn suffix) and at least one line, every line on the positive list. Anything unproven, longer than 3 lines a block, or not on the list
    is not a pass record."""
    from . import prompts
    if isinstance(seen_len, bool) or not isinstance(seen_len, int) or not 0 <= seen_len <= len(spec_base):
        return False
    if prompts.digest_of(spec_base[:seen_len], title) != seen:
        return False
    added = spec_base[seen_len:].strip()
    if not added:
        return True
    if not added.startswith("## Update"):
        return False
    for block in _UPDATE_SPLIT.split(added):
        if not block.strip():
            continue
        head, _, body = block.partition("\n")
        lines = [x.strip() for x in body.splitlines() if x.strip()]
        if not _UPDATE_HEAD.fullmatch(head) or not 0 < len(lines) <= 3 \
                or any(not _PASS_LINE.match(x) or _NOT_PASS.search(x) for x in lines):
            return False
    return True


def _key(e: dict) -> tuple:
    return (e.get("kind"), e.get("task"), e.get("fingerprint") or str(e.get("text") or "")[:300])


def _rule_ids(db, since: float) -> set[int]:
    out: set[int] = set()
    for r in db.q(f"SELECT events FROM {TABLE} WHERE gate='rule' AND verb='skip' AND ts>?", (since,)):
        out.update(json.loads(r["events"] or "[]"))
    return out


def _recheck(db, e: dict, now: float, window: float, ruled: set[int]) -> str | None:
    """Why re-check event `e` may be settled (a turn already met it), or None."""
    kind, task, fp = _key(e)
    where = "kind=? AND id<? AND ts>? AND status='handled' AND " + ("task=?" if task is not None else "task IS NULL")
    args = [kind, int(e["id"]), now - window] + ([task] if task is not None else [])
    prior = [r for r in db.q(f"SELECT id, fingerprint, text FROM events WHERE {where} ORDER BY id DESC LIMIT 50", args)
             if (r["fingerprint"] or str(r["text"] or "")[:300]) == fp]
    met = [r for r in prior if r["id"] not in ruled]
    if not met:
        return None
    if len([r for r in prior if r["id"] in ruled and r["id"] > met[0]["id"]]) >= RECHECK_MAX:
        return None
    return f"repeats event {met[0]['id']} a turn met"


def settle_reason(db, e: dict, task: dict | None, owners, plain_done, now: float, window: float,
                  ruled: set[int]) -> str | None:
    """Why event `e` is settled by rule, or None when a turn must see it."""
    from . import mustact
    if e.get("kind") in mustact.KINDS:
        return None
    kind = e.get("kind")
    base = dict(e, kind="observation") if kind in RECHECK_KINDS else e
    if kind in ("charter_retry", "machine_change_overdue", "stale_holds", "steer_unread", "ready_not_started",
                "idle_capacity"):
        # their own text names restrictions, machines or failures by design: only the hard guards apply
        if e.get("severity") not in SEVERITIES or str(e.get("source") or "").startswith(("user", "chat")):
            return None
        if e.get("task") is not None and int(e["task"]) in owners:
            return None
        try:
            if (json.loads(e.get("data") or "{}") or {}).get("must_act"):
                return None
        except (TypeError, ValueError, AttributeError):
            pass
    elif never_reason(base if kind == "observation" else e, task, owners):
        return None
    if kind in RECHECK_KINDS:
        return _recheck(db, e, now, window, ruled)
    if kind == "upstream_note":
        return "upstream note: the daemon files it"
    if kind == "task_done":
        if not task or task.get("kind") not in TASK_KINDS or task.get("reply_chat"):
            return None
        if _OPEN_RESULT.search(str(task.get("result") or "")) or not plain_done(e):
            return None
        return "plain task_done: no follow-up, nothing depends on it"
    return None


def settle(db, cfg: dict, evs: list[dict], now: float, plain_done) -> tuple[list[int], str]:
    """Settle the batch by rule when every event qualifies: (settled ids, log line), else ([], "")."""
    from . import obligations, alerts
    own = [e for e in evs if not alerts.rides_along(e)]
    if not own:
        return [], ""
    ids = [int(e["id"]) for e in own]
    rows = {r["id"]: r for r in db.q(f"SELECT id, ts, kind, severity, source, text, data, task, fingerprint "
                                     f"FROM events WHERE id IN ({','.join('?' * len(ids))})", ids)}
    try:
        owners = set(obligations.owner_due(db))
        window = float((cfg.get("coordinator") or {}).get("wake_settle_recheck_s", RECHECK_S) or RECHECK_S)
    except Exception:   # owners unknown: nothing is settled
        return [], ""
    ruled = _rule_ids(db, now - window)
    whys = {}
    for i in ids:
        e = rows.get(i)
        if e is None:
            return [], ""
        why = settle_reason(db, e, db.task(e["task"]) if e.get("task") is not None else None, owners,
                            plain_done, now, window, ruled)
        if not why:
            return [], ""
        whys[i] = why
    db.x(f"UPDATE events SET status='handled' WHERE status='queued' AND id IN ({','.join('?' * len(ids))})", ids)
    record(db, "rule", "skip", [rows[i] for i in ids], now, why="; ".join(sorted(set(whys.values())))[:300])
    return ids, (f"coordinator wake settled by rule: events {ids} ("
                 + "; ".join(sorted(set(whys.values())))[:300] + f"), ~${median_usd(db, now):.2f} saved")


def reopened(db, since: float, until: float | None = None) -> tuple[int, int]:
    """(rule-settled events since `since`, of them reopened): a later event on the same task (else same
    kind and fingerprint) within REOPEN_S that a turn handled rather than a rule."""
    until = time.time() if until is None else until
    ruled = _rule_ids(db, since - REOPEN_S)
    mine = [i for i in _rule_ids(db, since) if i in ruled]
    if not mine:
        return 0, 0
    rows = db.q(f"SELECT id, ts, kind, task, fingerprint, text FROM events WHERE id IN ({','.join('?' * len(mine))})",
                mine)
    back = 0
    for r in rows:
        if r["task"] is not None:
            later = db.q("SELECT id FROM events WHERE task=? AND id>? AND ts<? AND status='handled'",
                         (r["task"], r["id"], min(float(r["ts"]) + REOPEN_S, until)))
        else:
            later = [x for x in db.q("SELECT id, fingerprint, text FROM events WHERE kind=? AND task IS NULL AND id>? "
                                     "AND ts<? AND status='handled'", (r["kind"], r["id"], min(float(r["ts"]) + REOPEN_S, until)))
                     if (x["fingerprint"] or str(x["text"] or "")[:300]) == (r["fingerprint"] or str(r["text"] or "")[:300])]
        if any(x["id"] not in ruled for x in later):
            back += 1
    return len(rows), back


def summary(db, since: float, until: float | None = None) -> dict:
    """Decisions since `since`: per gate {skip, wake, usd}, totals, and the rule settles' reopen rate."""
    until = time.time() if until is None else until
    out: dict = {"gates": {}, "skips": 0, "wakes": 0, "usd": 0.0}
    for r in db.q(f"SELECT gate, verb, COUNT(*) n, SUM(usd) usd FROM {TABLE} WHERE ts>? AND ts<=? GROUP BY gate, verb",
                  (since, until)):
        g = out["gates"].setdefault(r["gate"], {"skip": 0, "wake": 0, "usd": 0.0})
        g[r["verb"]] = g.get(r["verb"], 0) + int(r["n"])
        g["usd"] = round(g["usd"] + float(r["usd"] or 0), 2)
        out["skips" if r["verb"] == "skip" else "wakes"] += int(r["n"])
        out["usd"] = round(out["usd"] + float(r["usd"] or 0), 2)
    out["wake_runs"] = int(db.one("SELECT COUNT(*) n FROM runs WHERE role='coordinator' AND started>? AND started<=?",
                                  (since, until))["n"])
    out["rule_settled"], out["rule_reopened"] = reopened(db, since, until)
    return out


def line(db, since: float, until: float | None = None) -> str:
    """One line: wakes run, skips by gate with $ avoided, and the rule settles' reopen rate."""
    s = summary(db, since, until)
    if not s["skips"] and not s["wakes"] and not s["wake_runs"]:
        return ""
    gates = ", ".join(f"{g} {v['skip']}" for g, v in sorted(s["gates"].items()) if v.get("skip"))
    rate = (f"; rule settles reopened {s['rule_reopened']}/{s['rule_settled']}"
            f" ({100.0 * s['rule_reopened'] / s['rule_settled']:.1f}%, target under 2%)") if s["rule_settled"] else ""
    return (f"coordinator wakes: {s['wake_runs']} ran, {s['skips']} settled without a model"
            + (f" ({gates})" if gates else "") + f", ~${s['usd']:.2f} avoided" + rate)


def audit_line(db, since: float) -> str:
    return line(db, since)
