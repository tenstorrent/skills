"""Blocked-time ledger: which rule held how much of the project's work, so the top blockers can be
retired or rewritten the same day.

`rows(db, since, until)` lists {rule, task, kind, start, end, minutes, safety, source[, n]} from four
sources: the stuck intervals of tasks (unblock.intervals), guard refusals in each run's
`refusals.jsonl` / `glm_refusals.jsonl` (minutes: until the task next ran, when that run handed off
blocked or waiting), queue-wait episodes (queuewait) and `state/blocklog.jsonl`, which `ttp block-log`
appends to. `safety` names the safety gate that holds the time, else "convenience". `window_share` is the
blocked share of ordered work's wall time (TARGET 5%): overlapping rows of one task count once, and rows of
WORK (a wait on the task's own detached job, a clock-gated planned window) are no blocked time. A wait on
another task's landing is rule "dependency", a review hold rule "review"; a redirect refusal (the run was
told the allowed way and went on) holds 0 minutes but keeps its count. Refusals are keyed by the guard's name.

`top` lists the day's convenience rules with the most minutes (safety rows are listed apart and never
retire items; `listed` leaves out NOT_TOP and 0-minute rows); `digest_lines` shows each as a
'top blocker' until `ttp blocked --done <key> --how TEXT`
(kv `blockledger:done:<key>`), and 'came back' when a done rule holds time again within BACK_S.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import obligations, queuetrim, queuewait, rulecite, selfblock, unblock, unstarted
from .db import DB
from .util import loads_dict

LOG = "blocklog.jsonl"
TARGET = 0.05
DONE = "blockledger:done:"   # + key_of(rule): {"how"}, stamped by the kv row's ts
BACK_S = 7 * 86400
SHOWN = 5
REFUSALS = ("refusals.jsonl", "glm_refusals.jsonl")
REDIRECT = "redirect"   # glmguard.REDIRECTED: the refusal told the allowed way and the run went on
WORK = frozenset({"own job", "planned window"})   # work in progress or a planned start: no blocked time
NOT_TOP = WORK | {"dependency"}
LANDED = re.compile(r"\blanded:#?(\d+)|\bttp\s+landed\s+--task\s+(\d+)|\blanding of task #(\d+)")
GUARD_KINDS = frozenset({"no_device", "worker_role", "provenance", "user_site", "pyaudit"})   # refusals of a safety guard
# Refusals that show the run the allowed way and let it go on (no class logged): redirects, unless the run was
# handed back (glmguard.HIT in its folder) or the attempt was harmful.
MOVES_ON = frozenset({"worker_role", "provenance", "pyaudit"})
# Safety gates beyond selfblock.GATES: a held resource, a check, a review or push, a user message, a rule id.
SAFE = (("held resource", re.compile(r"\bresource:\S+|\block\b", re.I)), ("check", re.compile(r"\bcheck:\S+")),
        ("review or push", re.compile(r"\b(?:review|pushing)\b", re.I)), ("user message", rulecite._MSG),
        ("rule", re.compile(r"\bR-[0-9a-f]{4}")))


def _why(text: str, tid: int, title: str) -> str:
    """The reason of a task event's text, which opens with '#id title' and then ': ' or ' is blocked: '."""
    rest = text.removeprefix(f"#{tid} {title}")
    if rest == text:
        return re.split(r"(?:blocked|waiting)[: ]+", text, 1, re.I)[-1]
    return re.sub(r"^\s*(?:(?:is|→)?\s*(?:blocked|waiting)\b)?[\s:→]*", "", rest, flags=re.I)


def rule_of(reason: str) -> str:
    """The first rule id the reason cites, else its first six words with digits and ids stripped."""
    m = rulecite._ID.search(reason)
    if m:
        return m.group(0)
    words = re.sub(r"#?\d+|\b[0-9a-f]{7,}\b", " ", re.split(r"(?:blocked|waiting)[: ]+", reason.lower(), 1)[-1])
    return " ".join(re.findall(r"[a-z]+", words)[:6]) or "unknown"


PLUGIN = re.compile(r"\b(?:task|run)[ _-]budget|budget_usd|over (?:its |the )?(?:task )?budget|change size|"
                    r"lines? over|too (?:large|big)|lean[- ]code|ponytail", re.I)
MONEY = selfblock.GATES[2][0]   # its words ("budget", "$") also read in the plugin's own task budget
CAPS = re.compile(r"\b(?:project|account|dollar|spend)[ _-]caps?\b", re.I)   # the dollar caps stay safety gates


def safety_of(reason: str, kind: str = "") -> str:
    if kind == "review":
        return "review or push"
    gates = (*selfblock.GATES, *SAFE)
    if PLUGIN.search(reason) and not CAPS.search(reason):   # the plugin's own task budget, change size and lean-code gates are ours to retire,
        gates = tuple(g for g in gates if g[0] != MONEY)   # unless a pause, a held resource, a user message or the like also holds it
    for name, rx in gates:
        if rx.search(reason):
            return name
    return "convenience"


def _wait_rule(data: dict, reason: str) -> tuple[str, str] | None:
    """(rule, reason) of a wait its task_waiting `data` or reason places: the task's own detached job, another
    task's landing, a planned window (a bare clock-gate probe, or the hand-off's own: unblock.classify_wait)."""
    probe = str(data.get("probe") or "")
    if queuewait.own_job(probe):
        return "own job", reason
    if m := LANDED.search(probe) or LANDED.search(reason):
        return "dependency", f"needs #{next(g for g in m.groups() if g)}"
    window = unstarted.TIME_ONLY.match(probe) or data.get("why") == "planned window"
    return ("planned window", reason) if window else None


def _row(rule, task, kind, start, end, reason, source, **extra) -> dict:
    return {"rule": rule, "task": task, "kind": kind, "start": start, "end": end,
            "minutes": round(max(0.0, end - start) / 60, 1), "safety": safety_of(f"{rule} {reason}", kind),
            "source": source, **extra}


def _handed_next(db: DB, r: dict, until: float) -> float | None:
    """When the task next ran after run `r` handed off blocked or waiting, else None."""
    hand = db.one("SELECT ts FROM events WHERE task=? AND kind IN ('task_blocked','task_waiting') "
                  "AND ts>=? AND ts<=? ORDER BY ts LIMIT 1", (r["task"], r["started"], (r["ended"] or until) + 60))
    nxt = hand and db.one("SELECT started FROM runs WHERE task=? AND role!='coordinator' AND started>? "
                          "ORDER BY started LIMIT 1", (r["task"], hand["ts"]))
    return nxt and nxt["started"]


def _refusals(db: DB, since: float, until: float) -> list[dict]:
    """One row per run and refusal rule: from the first refusal, `n` counting them, until the task next ran."""
    out = []
    for r in db.q("SELECT id, task, started, ended FROM runs WHERE task IS NOT NULL AND started<=? "
                  "AND COALESCE(ended, ?)>=?", (until, until, since)):
        end = _handed_next(db, r, until)
        by: dict = {}
        run = Path(db.path).parent / "runs" / str(r["id"])
        back = (run / "glm_guard.json").is_file()   # glmguard.HIT: the run was handed back to Opus
        for name in REFUSALS:
            f = run / name
            for line in (f.read_text(errors="replace").splitlines() if f.is_file() else ()):
                try:
                    rec = json.loads(line)
                    ts = float(rec["ts"])
                except (ValueError, TypeError, KeyError):
                    continue
                if not isinstance(rec, dict) or not since <= ts <= until:
                    continue
                kind = str(rec.get("guard") or rec.get("kind") or rec.get("rule") or "refusal")
                e = by.setdefault(kind, {"ts": ts, "n": {}, "guard": False, "moved": True})
                e["ts"], e["n"][name] = min(e["ts"], ts), e["n"].get(name, 0) + 1
                e["guard"] |= rec.get("class") == "harmful" or rec.get("kind") in GUARD_KINDS
                e["moved"] &= rec.get("class") == REDIRECT or (
                    not back and rec.get("class") is None and rec.get("kind") in MOVES_ON)
        for kind, e in by.items():   # the hook and glmguard both log a GLM refusal: count it once
            stop = e["ts"] if e["moved"] else max(e["ts"], min(end or e["ts"], until))
            row = _row(kind, r["task"], "refusal", e["ts"], stop, "", "refusal", n=max(e["n"].values()))
            if e["guard"]:
                row["safety"] = "guard"   # a safety guard's refusal is no rule to retire
            out.append(row)
    return out


def rows(db: DB, since: float, until: float | None = None) -> list[dict]:
    """The ledger's rows whose time overlaps [since, until], longest first."""
    until = time.time() if until is None else until
    out = []
    for seg in unblock.overlapping(db, since, until):
        start, end = seg["start"], min(seg.get("end") or until, until)
        ev = db.one("SELECT text, data FROM events WHERE task=? AND kind=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                    (seg["task"], f"task_{seg['kind']}", start))
        task = db.task(seg["task"]) or {}
        reason = _why((ev and ev["text"]) or task.get("blocked_reason") or seg["title"], seg["task"], seg["title"])
        rule, why = ("review", reason) if seg["kind"] == "review" else \
            _wait_rule(loads_dict(ev and ev["data"]), reason) or (rule_of(reason), reason)
        out.append(_row(rule, seg["task"], seg["kind"], max(start, since), end, why, "task"))
    out += _refusals(db, since, until)
    st = db.kv(queuewait.KV) or {}
    for e in [*(st.get("hist") or []), *(st.get("open") or {}).values()]:
        end = min(float(e.get("end") or until), until)
        if end >= since and float(e["since"]) <= until:
            rule, why = f"queue:{e['queue']}", e["queue"]
            if e["queue"] == "own":   # an urgent task's own probe: its detached job, another task's landing, a clock
                rule, why = _wait_rule({"probe": e.get("probe")}, why) or (rule, why)
            out.append(_row(rule, e.get("task"), "queue", max(float(e["since"]), since), end, why, "queue"))
    log = Path(db.path).parent / LOG
    for line in (log.read_text(errors="replace").splitlines() if log.is_file() else ()):
        try:
            rec = json.loads(line)
            ts, secs = float(rec["ts"]), float(rec.get("seconds") or 0)
        except (ValueError, TypeError, KeyError):
            continue
        if isinstance(rec, dict) and since <= ts <= until + secs:
            out.append(_row(str(rec["rule"]), rec.get("task"), "log", max(ts - secs, since), ts, str(rec.get("why") or ""),
                            str(rec.get("source") or "log")))
    return sorted(out, key=lambda r: -r["minutes"])


def append(db: DB, rule: str, task: int | None = None, seconds: float = 0, why: str = "", source: str = "cli") -> None:
    """One `blocklog.jsonl` line: time the harness or a worker lost to `rule` outside a task's blocked state."""
    with open(Path(db.path).parent / LOG, "a") as f:
        f.write(json.dumps({"ts": time.time(), "rule": rule, "task": task, "seconds": seconds, "why": why,
                            "source": source}) + "\n")


def _ordered(db: DB) -> set[int]:
    owned = obligations.order_tasks(db)
    return {t["id"] for t in db.q("SELECT * FROM tasks") if t["id"] in owned or int(t["priority"]) <= 1
            or queuetrim.user_ordered(t)}


def _union_min(spans: list[tuple[float, float]]) -> float:
    total, edge = 0.0, 0.0
    for a, b in sorted(spans):
        total += max(0.0, b - max(a, edge))
        edge = max(edge, b)
    return total / 60


def window_share(db: DB, start: float, end: float, rs: list[dict] | None = None) -> dict:
    """{share, blocked_min, ordered_min} over [start, end] from the ledger's `rs` (rows() of that window): blocked
    minutes of ordered tasks over their wall minutes."""
    ordered = _ordered(db)
    wall = sum(max(0.0, min(float(t["updated"] if t["status"] in unblock.ENDED else end), end)
                    - max(float(t["created"] or 0), start))
               for t in db.q("SELECT * FROM tasks") if t["id"] in ordered) / 60
    by: dict = {}
    for r in rows(db, start, end) if rs is None else rs:
        if r["task"] in ordered and r["minutes"] and r["rule"] not in WORK:
            by.setdefault(r["task"], []).append((max(r["start"], start), min(r["end"], end)))
    blocked = sum(_union_min(v) for v in by.values())
    return {"share": round(blocked / wall, 4) if wall else 0.0, "blocked_min": round(blocked, 1),
            "ordered_min": round(wall, 1)}


def key_of(rule: str) -> str:
    """The one-word key `ttp blocked --done` takes: the rule id, else the rule's words joined by '-'."""
    m = rulecite._ID.search(rule)
    return m.group(0) if m else re.sub(r"[^a-z0-9]+", "-", rule.lower()).strip("-")[:40]


def by_rule(rs: list[dict], n: int = 10) -> list[dict]:
    """The n rules with the most minutes: {rule, key, minutes, count, safety, tasks}."""
    by: dict = {}
    for r in rs:
        e = by.setdefault(r["rule"], {"rule": r["rule"], "key": key_of(r["rule"]), "minutes": 0.0, "count": 0,
                                      "safety": r["safety"], "tasks": set()})
        e["minutes"] += r["minutes"]
        e["count"] += r.get("n", 1)
        e["tasks"] |= {r["task"]} if r["task"] is not None else set()
    return [{**e, "minutes": round(e["minutes"], 1), "tasks": sorted(e["tasks"])}
            for e in sorted(by.values(), key=lambda e: -e["minutes"])[:n]]


def daily_line(db: DB, now: float | None = None) -> str:
    now = time.time() if now is None else now
    rs = rows(db, now - 86400, now)
    s = window_share(db, now - 86400, now, rs)
    t = top(db, now, 3, rs=rs)
    opened, back = status(db, now, rs)
    return (f"Blocked time: {s['share'] * 100:.1f}% of ordered work (target {TARGET * 100:g}%); top: "
            + (", ".join(f"{e['rule']} {e['minutes']:g}m" for e in t) or "none")
            + f"; top blockers open: {len(opened)}, came back: {len(back)}")


def top(db: DB, now: float | None = None, n: int = SHOWN, safety: bool = False, rs: list[dict] | None = None) -> list[dict]:
    """The day's n rules with the most minutes: the convenience ones (retire or rewrite them), or with `safety`
    the safety-gate ones, listed apart and never retire items."""
    now = time.time() if now is None else now
    rs = rows(db, now - 86400, now) if rs is None else rs
    return by_rule([r for r in listed(rs) if (r["safety"] != "convenience") == safety], n)


def listed(rs: list[dict]) -> list[dict]:
    """The rows a top-blockers list names: real blocked minutes, no work, planned window or dependency."""
    return [r for r in rs if r["minutes"] and r["rule"] not in NOT_TOP]


def done(db: DB, key: str, how: str) -> None:
    db.set_kv(DONE + key, {"how": how})


def status(db: DB, now: float, rs: list[dict] | None = None) -> tuple[list[dict], list[tuple[str, str]]]:
    """(the day's top convenience rules not yet done, [(key, how)] of done ones that held time again within BACK_S)."""
    marks = {r["key"][len(DONE):]: (r["ts"], json.loads(r["value"]).get("how", ""))
             for r in db.q("SELECT key, value, ts FROM kv WHERE key LIKE ? AND ts>=?", (DONE + "%", now - BACK_S))}
    opened = top(db, now, 50, rs=rs)
    back = []
    if marks:
        since = min(ts for ts, _ in marks.values())
        for r in listed(rows(db, since, now)):
            k = key_of(r["rule"])
            if k in marks and r["safety"] == "convenience" and r["start"] > marks[k][0]:
                back.append((k, marks[k][1]))
                marks.pop(k)
    gone = {k for k, _ in back} | set(marks)
    return [e for e in opened if e["key"] not in gone], back


def digest_lines(db: DB, now: float | None = None) -> list[str]:
    """Course check lines: each top convenience blocker of the day until it is done, and each done one that came back."""
    now = time.time() if now is None else now
    opened, back = status(db, now)
    return [f"- top blocker {e['key']}: {e['rule']} held {', '.join(f'#{t}' for t in e['tasks'][:4]) or 'work'} for "
            f"{e['minutes']:g} min today; retire it (memory_forget / config_set / code task) or rewrite it, then "
            f"`ttp blocked --done {e['key']} --how \"<what>\"`" for e in opened[:SHOWN]] + \
           [f"- came back: {k} ({how} did not stop it)" for k, how in back[:SHOWN]]
