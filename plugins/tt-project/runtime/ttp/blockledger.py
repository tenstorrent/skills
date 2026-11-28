"""Blocked-time ledger: which rule held how much of the project's work, so the top blockers can be
retired or rewritten the same day.

`rows(db, cfg, since, until)` lists {rule, task, kind, start, end, minutes, safety, source[, n]} from four
sources: the stuck intervals of tasks (unblock.intervals), guard refusals in each run's
`refusals.jsonl` / `glm_refusals.jsonl` (minutes: until the task next ran, when that run handed off
blocked or waiting), queue-wait episodes (queuewait) and `state/blocklog.jsonl`, which `ttp block-log`
appends to. `safety` names the safety gate that holds the time, else "convenience". `window_share` is the
blocked share of ordered work's wall time (TARGET 5%): overlapping rows of one task count once.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import obligations, queuetrim, queuewait, rulecite, selfblock, unblock
from .db import DB

LOG = "blocklog.jsonl"
TARGET = 0.05
REFUSALS = ("refusals.jsonl", "glm_refusals.jsonl")
GUARD_KINDS = frozenset({"no_device", "worker_role", "provenance", "user_site"})   # hook refusals of a safety guard
# Safety gates beyond selfblock.GATES: a held resource, a check, a review or push, a user message, a rule id.
SAFE = (("held resource", re.compile(r"\bresource:\S+|\block\b", re.I)), ("check", re.compile(r"\bcheck:\S+")),
        ("review or push", re.compile(r"\b(?:review|pushing)\b", re.I)), ("user message", rulecite._MSG),
        ("rule", re.compile(r"\bR-[0-9a-f]{4}")))


def rule_of(reason: str) -> str:
    """The first rule id the reason cites, else its first six words with digits and ids stripped."""
    m = rulecite._ID.search(reason)
    if m:
        return m.group(0)
    words = re.sub(r"#?\d+|\b[0-9a-f]{7,}\b", " ", re.split(r"(?:blocked|waiting)[: ]+", reason.lower(), 1)[-1])
    return " ".join(re.findall(r"[a-z]+", words)[:6]) or "unknown"


def safety_of(reason: str, kind: str = "") -> str:
    if kind == "review":
        return "review or push"
    for name, rx in (*selfblock.GATES, *SAFE):
        if rx.search(reason):
            return name
    return "convenience"


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
    out = []
    for r in db.q("SELECT id, task, started, ended FROM runs WHERE task IS NOT NULL AND started<=? "
                  "AND COALESCE(ended, ?)>=?", (until, until, since)):
        end = _handed_next(db, r, until)
        for name in REFUSALS:
            f = Path(db.path).parent / "runs" / str(r["id"]) / name
            for line in (f.read_text(errors="replace").splitlines() if f.is_file() else ()):
                try:
                    rec = json.loads(line)
                    ts = float(rec["ts"])
                except (ValueError, TypeError, KeyError):
                    continue
                if not isinstance(rec, dict) or not since <= ts <= until:
                    continue
                kind = str(rec.get("kind") or rec.get("rule") or "refusal")
                row = _row(kind, r["task"], "refusal", ts, max(ts, min(end or ts, until)), "", "refusal", n=int(rec.get("n") or 1))
                if rec.get("class") == "harmful" or rec.get("kind") in GUARD_KINDS:
                    row["safety"] = "guard"   # a safety guard's refusal is no rule to retire
                out.append(row)
    return out


def rows(db: DB, cfg: dict | None, since: float, until: float | None = None) -> list[dict]:
    """The ledger's rows whose time overlaps [since, until], longest first."""
    until = time.time() if until is None else until
    out = []
    for seg in unblock.intervals(db, since, until):
        start, end = seg["start"], min(seg.get("end") or until, until)
        ev = db.one("SELECT text FROM events WHERE task=? AND kind=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                    (seg["task"], f"task_{seg['kind']}", start))
        task = db.task(seg["task"]) or {}
        reason = (ev and ev["text"]) or task.get("blocked_reason") or seg["title"]
        out.append(_row(rule_of(reason), seg["task"], seg["kind"], start, end, reason, "task"))
    out += _refusals(db, since, until)
    st = db.kv(queuewait.KV) or {}
    for e in [*(st.get("hist") or []), *(st.get("open") or {}).values()]:
        end = float(e.get("end") or until)
        if end >= since and float(e["since"]) <= until:
            out.append(_row(f"queue:{e['queue']}", e.get("task"), "queue", float(e["since"]), end, e["queue"], "queue"))
    log = Path(db.path).parent / LOG
    for line in (log.read_text(errors="replace").splitlines() if log.is_file() else ()):
        try:
            rec = json.loads(line)
            ts, secs = float(rec["ts"]), float(rec.get("seconds") or 0)
        except (ValueError, TypeError, KeyError):
            continue
        if isinstance(rec, dict) and since <= ts <= until + secs:
            out.append(_row(str(rec["rule"]), rec.get("task"), "log", ts - secs, ts, str(rec.get("why") or ""),
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


def window_share(db: DB, cfg: dict | None, start: float, end: float) -> dict:
    """{share, blocked_min, ordered_min} over [start, end]: blocked minutes of ordered tasks over their wall minutes."""
    ordered = _ordered(db)
    wall = sum(max(0.0, min(float(t["updated"] if t["status"] in unblock.ENDED else end), end)
                    - max(float(t["created"] or 0), start))
               for t in db.q("SELECT * FROM tasks") if t["id"] in ordered) / 60
    by: dict = {}
    for r in rows(db, cfg, start, end):
        if r["task"] in ordered and r["minutes"]:
            by.setdefault(r["task"], []).append((max(r["start"], start), min(r["end"], end)))
    blocked = sum(_union_min(v) for v in by.values())
    return {"share": round(blocked / wall, 4) if wall else 0.0, "blocked_min": round(blocked, 1),
            "ordered_min": round(wall, 1)}


def top(rs: list[dict], n: int = 10) -> list[dict]:
    """The n rules with the most minutes: {rule, minutes, count, safety, tasks}."""
    by: dict = {}
    for r in rs:
        e = by.setdefault(r["rule"], {"rule": r["rule"], "minutes": 0.0, "count": 0, "safety": r["safety"], "tasks": set()})
        e["minutes"] += r["minutes"]
        e["count"] += r.get("n", 1)
        e["tasks"] |= {r["task"]} if r["task"] is not None else set()
    return [{**e, "minutes": round(e["minutes"], 1), "tasks": sorted(e["tasks"])}
            for e in sorted(by.values(), key=lambda e: -e["minutes"])[:n]]


def daily_line(db: DB, cfg: dict | None, now: float | None = None) -> str:
    now = time.time() if now is None else now
    s = window_share(db, cfg, now - 86400, now)
    t = top([r for r in rows(db, cfg, now - 86400, now) if r["safety"] == "convenience"], 3)
    return (f"Blocked time: {s['share'] * 100:.1f}% of ordered work (target {TARGET * 100:g}%); top: "
            + (", ".join(f"{e['rule']} {e['minutes']:g}m" for e in t) or "none"))
