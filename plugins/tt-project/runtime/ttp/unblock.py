# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Unblocking quality, for the daily review: how long stuck work stays stuck, how many asks the user
handed back ("decide yourself"), and how the coordinator's effort was split.

Stuck time: an episode starts when a task is handed off blocked, waiting or for review (a queued
task behind a dead dependency is set blocked too) and ends when the task next moves forward: a run
of it starts (a blocked or review task only; a waiting task's wakes keep waiting), it is done,
failed, requeued or pushed, or its status left the stuck state (cancelled, re-pointed). Consecutive
stuck hand-offs are one episode. An episode still open counts with its age so far.

Waits are self or external (classify_wait). A self-wait is the task's own progress: its detached
`ttp checks` or `ttp detach` jobs, the push queue, or a time window it planned itself. It neither
starts a stuck episode nor counts toward the coordinator's 'repeated waits' trigger or a resource's
waits, and it ends a waiting episode (the task moved on). The daily review lists self-waits apart.

A wait whose retry_when only probes `ttp detach --check` jobs or `ttp lock --probe` resources is a wait
on live work (live_wait). While the job runs (or the resource is not paused) such a wait is routine for
the 'repeated waits' trigger, external or not, until the same jobs or locks were waited on more than
coordinator.live_waits_max times in 24 h or, for jobs, their logs stopped growing since the last wait.

Handed-back asks: a user message answers an ask when it names it (`#<id>`) after it was sent, or
replies in its chat thread. The answer's part about that ask is matched against HANDBACK, a short
phrase list of answers that give the decision back to the project.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any

from .db import DB

WINDOWS = (("24 h", 86400), ("7 d", 7 * 86400))
STUCK = {"task_blocked": "blocked", "task_waiting": "waiting", "task_review": "review"}
MOVED = frozenset({"task_done", "task_failed", "task_queued", "task_requeued", "push_queued"})
ENDED = frozenset({"done", "failed", "cancelled", "pushing"})   # a status that ends any episode
ANSWER_MAX_AGE_S = 14 * 86400   # a mention of an ask id later than this is not its answer

# What a waiting hand-off's `retry_when` probes, when it is the task's own work: by the `ttp` subcommand.
SELF_PROBES = (("checks", re.compile(r"\bchecks\s+--result\b")),
               ("detached jobs", re.compile(r"\bdetach\s+--check\b")),
               ("push queue", re.compile(r"\bpush\s+--(result|free)\b")))
EXTERNAL_PROBE = re.compile(r"\block\s+--probe\b")   # a shared resource: someone else holds it
# A time window the task planned itself (a measurement or soak period), in its `waiting_for`.
PLANNED_WINDOW = re.compile(
    r"\b(planned|scheduled|own|measurement|measuring|observation|observing|monitoring|soak|burn-in|bake|"
    r"settling|cool-?down|data[- ]collection)\s+(time\s+)?(window|period|interval|run)\b"
    r"|\b(window|period|delay)\s+(it|I|the task|this task|we)\s+(set|planned|chose)\b"
    r"|\bstart_(after|when)\b", re.I)
WAIT_KINDS = ("self", "external")
LIVE_WAITS_MAX = 6   # coordinator.live_waits_max: waits on the same live jobs or locks in 24 h that stay routine
_SHELL_OPS = frozenset({"&&", "||", ";", "|", "&"})
# A cheap wake that found work and runs again at once: the same wait going on, never a new one.
ESCALATED_WAKE = re.compile(r"\bwoke at \w+ and found work; runs again now\b")
# Tokens that change from one wait to the next without the reason changing: the next-try time,
# timestamps, times, shas and run ids, then any other number.
REASON_NOISE = (re.compile(r";\s*next try\b.*$", re.I),
                re.compile(r"\b\d{4}-\d{2}-\d{2}([T ]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?"),
                re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b"),
                re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,64}\b", re.I),
                re.compile(r"\d+"))

# Answers that hand the decision back: the ask should not have been sent.
HANDBACK = tuple(re.compile(p, re.I) for p in (
    r"\bdecide (it |that |this )?(for )?yourself\b",
    r"\bdecide (it |that |this )?on your own\b",
    r"\byou (can |should |may )?decide\b",
    r"\b(it'?s |that'?s )?your (call|choice|decision)\b",
    r"\bup to you\b",
    r"\b(just|simply) (do|fix|handle) it\b",
    r"\buse your (own |best )?(judge?ment|discretion)\b",
    r"\bwhatever you (think|prefer|decide)\b",
    r"\b(do|go with) what(ever)? you think\b",
    r"\bno need to ask\b",
    r"\b(don'?t|do not|never|shouldn'?t|should not) (need to )?ask( me)?\b",
    r"\b(stop|quit) asking\b",
    r"\bwhy (are you |did you |do you )?ask(ing)?\b",
    r"\b(don'?t|do not|never) (get|stay|be) (blocked|stuck)\b",
    r"\bunblock (yourself|itself|efficiently)\b",
    r"\b(you )?(don'?t|do not) need my (ok|okay|approval|answer|input|permission)\b",
    r"\bhandle it yourself\b",
    r"\bi trust (you|your)\b",
))
_MENTION = re.compile(r"(?<![\w/])#(\d+)\b")
_NOT_ASK = re.compile(r"\b(task|pr|issue|run|pull request)\s*$", re.I)


def handback(text: str) -> str | None:
    """The phrase in an answer that hands the decision back, or None."""
    text = (text or "").replace("’", "'")
    for rx in HANDBACK:
        m = rx.search(text)
        if m:
            return m.group(0)
    return None


def classify_wait(result: dict | None) -> tuple[str, str]:
    """(kind, why) of a waiting hand-off: kind 'self' when the task waits on its own progress (its
    detached checks or jobs, the push queue, a time window or start_after/start_when it set), else
    'external' (a busy resource or lock, another task, a human, a review). The hand-off's own
    `wait_kind` wins; an unknown wait counts as external, so a real one still raises effort."""
    r = result if isinstance(result, dict) else {}
    own = str(r.get("wait_kind") or "").strip().lower()
    if own in WAIT_KINDS:
        return own, "hand-off"
    probe = str(r.get("retry_when") or "")
    if EXTERNAL_PROBE.search(probe):
        return "external", "lock"
    for why, rx in SELF_PROBES:
        if rx.search(probe):
            return "self", why
    if PLANNED_WINDOW.search(str(r.get("waiting_for") or "")):
        return "self", "planned window"
    return "external", "other"


def wait_data(raw: Any) -> dict:
    """A task_waiting event's `data`: {"wait": self|external, "why": ...}; {} for older events."""
    d = _note(raw)
    return d if d.get("wait") in WAIT_KINDS else {}


def is_self_wait(raw: Any) -> bool:
    """Whether a task_waiting event's `data` marks it a self-wait (older events count as external)."""
    return wait_data(raw).get("wait") == "self"


def counted_wait(ev: Any, older: Any = (), live_max: int = LIVE_WAITS_MAX) -> bool:
    """Whether a task_waiting event (a row with `data` and `text`) counts toward 'repeated waits':
    not a cheap wake that found work, and not routine live work (live_routine, given the task's
    `older` waits, newest first); else an external wait counts and a self-wait does not (older
    events lack the data and count as external)."""
    if ESCALATED_WAKE.search(ev["text"] or ""):
        return False
    routine = live_routine(ev, older, live_max)
    if routine is not None:
        return not routine
    return not is_self_wait(ev["data"])


def wait_probes(retry_when: Any) -> tuple[list[str], list[str]] | None:
    """The rc paths of `ttp detach --check` and the resources of `ttp lock --probe` in a retry_when,
    when every command in it is one of these two; None otherwise."""
    try:
        toks = shlex.split(str(retry_when or ""))
    except ValueError:
        return None
    rcs: list[str] = []
    res: list[str] = []
    cmd: list[str] = []
    for tok in toks + [";"]:
        if tok not in _SHELL_OPS:
            cmd.append(tok)
            continue
        at = next((i for i, t in enumerate(cmd[1:], 1) if os.path.basename(cmd[i - 1]) == "ttp"
                   and t in ("detach", "lock")), None)   # `ttp`, a path to it or `python3 -m ttp`
        args = cmd[at:] if at else []
        if len(args) >= 3 and args[:2] == ["detach", "--check"] and not any(a.startswith("-") for a in args[2:]):
            rcs += args[2:]
        elif len(args) == 3 and args[:2] == ["lock", "--probe"]:
            res.append(args[2])
        elif cmd:
            return None
        cmd = []
    return (rcs, res) if rcs or res else None


def live_wait(p: Any, result: dict | None) -> dict | None:
    """What a waiting hand-off's live work is, for the task_waiting event's `data.live`: {"key",
    "alive", "log"}; None when its retry_when probes anything else (wait_probes). `key` names the
    jobs and locks; `alive` is whether a job still runs or no resource is paused; `log` is the jobs'
    log bytes now (None without jobs). Read here, at the hand-off: later the job may be gone."""
    probes = wait_probes((result or {}).get("retry_when") if isinstance(result, dict) else None)
    if not probes:
        return None
    from . import locks as lk
    rcs, res = probes
    cfg = p.config()
    paths = [Path(r) if os.path.isabs(r) else Path(p.base) / r for r in rcs]
    locked = sorted({lk.canonical(cfg, r) for r in res})
    alive = True
    if paths:
        alive = not all(lk.job_ended(x) for x in paths)
    if locked:
        paused = {lk.canonical(cfg, k) for k in p.db.paused_resources()}
        alive = alive and not paused & set(locked)
    log = None
    if paths:
        log = 0
        for x in paths:
            try:
                log += x.with_suffix(".log").stat().st_size
            except OSError:
                pass
    key = " ".join([f"job:{x}" for x in sorted(map(str, paths))] + [f"lock:{r}" for r in locked])
    return {"key": key[:500], "alive": alive, "log": log}


def live_routine(ev: Any, older: Any = (), live_max: int = LIVE_WAITS_MAX) -> bool | None:
    """For a wait on live work that was alive at its hand-off: True while it is routine, False once
    the same work was waited on more than `live_max` times in the 24 h before it (`older`: the
    task's earlier events, newest first; 0 makes none routine) or its jobs' log did not grow since
    the previous wait on them. None for any other wait."""
    live = _note(ev["data"]).get("live")
    if not isinstance(live, dict) or not live.get("alive") or not live.get("key"):
        return None
    if not live_max:
        return False
    same = [_note(o["data"]).get("live") or {} for o in older
            if o["kind"] == "task_waiting" and o["ts"] > ev["ts"] - 86400]
    same = [o for o in same if isinstance(o, dict) and o.get("key") == live["key"]]
    if len(same) + 1 > live_max:
        return False
    before, now = (same[0].get("log") if same else None), live.get("log")
    return not (isinstance(before, int) and isinstance(now, int) and now <= before)


def wait_reason(ev: Any) -> str:
    """What a task_waiting event waits for, normalized so waits differing only in times, shas, run
    ids or counts compare equal: its `data.for`, else its text after "#id title: "."""
    raw = str(_note(ev["data"]).get("for") or (ev["text"] or "").split(": ", 1)[-1])
    raw = re.sub(r"^waiting for\s+", "", raw.strip(), flags=re.I)
    for rx in REASON_NOISE:
        raw = rx.sub("0" if rx.pattern == r"\d+" else "", raw)
    return " ".join(raw.lower().split())[:200]


def answer_part(text: str, ask_id: int, ask_ids: set[int]) -> str | None:
    """The part of a message about ask `ask_id`: from its `#id` to the next ask id it names, or
    None when it does not name it. A `#id` right after "task" or "PR" names something else."""
    found = [m for m in _MENTION.finditer(text or "") if int(m.group(1)) in ask_ids
             and not _NOT_ASK.search(text[:m.start()])]
    for i, m in enumerate(found):
        if int(m.group(1)) == ask_id:
            end = next((n.start() for n in found[i + 1:] if int(n.group(1)) != ask_id), len(text))
            return text[m.start():end]
    return None


def _pct(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    k = max(0, min(len(sorted_vals) - 1, -(-int(q * 100) * len(sorted_vals) // 100) - 1))
    return sorted_vals[k]


def episodes(db: DB, since: float, now: float | None = None) -> list[dict]:
    """Stuck episodes that started at or after `since`: {task, title, kind, start, end, open, s}."""
    now = time.time() if now is None else now
    kinds = tuple(STUCK) + tuple(MOVED)
    ids = [r["task"] for r in db.q(
        f"SELECT DISTINCT task FROM events WHERE task IS NOT NULL AND ts>=? AND kind IN "
        f"({','.join('?' * len(STUCK))})", (since, *STUCK))]
    out: list[dict] = []
    for tid in ids:
        task = db.task(tid)
        if not task:
            continue
        # A self-wait is the task moving on: it ends a waiting episode and starts none.
        marks = [(e["ts"], "self_wait" if e["kind"] == "task_waiting" and is_self_wait(e["data"]) else e["kind"])
                 for e in db.q(f"SELECT ts, kind, data FROM events WHERE task=? AND kind IN "
                               f"({','.join('?' * len(kinds))})", (tid, *kinds))]
        marks += [(r["started"], "run") for r in db.q(
            "SELECT started FROM runs WHERE task=? AND role!='coordinator' AND started IS NOT NULL", (tid,))]
        marks.sort()
        ep: dict | None = None
        for ts, kind in marks:
            if kind in STUCK:
                if ep is None:
                    ep = {"task": tid, "title": task["title"], "kind": STUCK[kind], "start": ts, "mode": STUCK[kind]}
                ep["mode"] = STUCK[kind]
            elif ep and (kind in MOVED or (kind == "self_wait" and ep["mode"] == "waiting")
                         or (kind == "run" and ep["mode"] != "waiting")):
                ep["end"] = ts
                out.append(ep)
                ep = None
        if ep:
            status, upd = task["status"], float(task["updated"] or 0)
            left = status in ENDED or (ep["mode"] != "waiting" and status not in ("blocked", "review"))
            if left and upd >= ep["start"]:
                ep["end"] = upd
            out.append(ep)
    for ep in out:
        ep["open"] = "end" not in ep
        ep["s"] = max(0.0, (ep["end"] if not ep["open"] else now) - ep["start"])
        ep.pop("mode", None)
    return [ep for ep in out if ep["start"] >= since]


def self_waits(db: DB, since: float) -> list[dict]:
    """Self-waits handed off since `since`: {task, why}."""
    return [{"task": e["task"], "why": wait_data(e["data"]).get("why") or "other"}
            for e in db.q("SELECT task, data FROM events WHERE kind='task_waiting' AND ts>=? AND task IS NOT NULL",
                          (since,)) if is_self_wait(e["data"])]


def self_waits_line(rows: list[dict]) -> str:
    """How many self-waits, by how many tasks, and on what."""
    if not rows:
        return "none"
    per: dict[str, int] = {}
    for r in rows:
        per[r["why"]] = per.get(r["why"], 0) + 1
    return (f"{len(rows)} by {len({r['task'] for r in rows})} tasks (not counted as stuck): "
            + ", ".join(f"{k} {n}" for k, n in sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))))


def _dur(s: float) -> str:
    return f"{s / 60:.0f} min" if s < 3600 else f"{s / 3600:.1f} h" if s < 4 * 86400 else f"{s / 86400:.1f} d"


def stuck_line(eps: list[dict]) -> str:
    """count, still open, median, p90 and the longest of some episodes."""
    if not eps:
        return "none"
    vals = sorted(e["s"] for e in eps)
    top = max(eps, key=lambda e: e["s"])
    opened = sum(e["open"] for e in eps)
    return (f"{len(eps)} ({opened} still open), median {_dur(_pct(vals, 0.5))}, p90 {_dur(_pct(vals, 0.9))}, "
            f"longest #{top['task']} {top['title'][:70]} {_dur(top['s'])}{' (still open)' if top['open'] else ''}")


def asks(db: DB, since: float, now: float | None = None) -> list[dict]:
    """Asks sent since `since`: {id, blocking, answered, handback (the phrase or None)}."""
    now = time.time() if now is None else now
    sent = db.q("SELECT id, ts, ref, ext_id FROM messages WHERE direction='out' AND kind='ask' AND ts>=? ORDER BY id",
                (since,))
    if not sent:
        return []
    every = {r["id"] for r in db.q("SELECT id FROM messages WHERE direction='out' AND kind='ask'")}
    replies = db.q("SELECT ts, text, ref FROM messages WHERE direction='in' AND kind='user' AND ts>=? ORDER BY id",
                   (sent[0]["ts"],))
    out = []
    for a in sent:
        ref = a["ref"] or ""
        row = {"id": a["id"], "blocking": ref.split(":", 1)[1] if ref.startswith("blocking:") else "unset",
               "answered": False, "handback": None}
        for m in replies:
            if not a["ts"] < m["ts"] <= a["ts"] + ANSWER_MAX_AGE_S:
                continue
            part = answer_part(m["text"], a["id"], every)
            if part is None and a["ext_id"] and m["ref"] == a["ext_id"]:
                part = m["text"]   # a reply in the ask's chat thread
            if part is None:
                continue
            row["answered"] = True
            row["handback"] = row["handback"] or handback(part)
        out.append(row)
    return out


def asks_line(rows: list[dict]) -> str:
    if not rows:
        return "no asks sent"
    back = [r for r in rows if r["handback"]]
    answered = sum(r["answered"] for r in rows)
    line = (f"{len(rows)} sent, {answered} with a linked answer, {len(back)} handed back "
            f"({100 * len(back) / len(rows):.0f}% of asks)")
    if back:
        line += ": " + ", ".join(f"#{r['id']} ({r['blocking']}; \"{r['handback']}\")" for r in back)
    return line


def turns_line(db: DB, since: float) -> str:
    """The coordinator's high/low turn split and, where its turns log them, escalations and triggers.
    A note's `triggers` list counts each trigger on its own; an "escalated: <why>" entry counts
    under "escalated". Notes without that list fall back to `trigger` or `unblock`."""
    rows = db.q("SELECT effort, note FROM runs WHERE role='coordinator' AND started>=?", (since,))
    if not rows:
        return "no coordinator turns"
    split: dict[str, int] = {}
    escalated, logged, why = 0, False, {}
    for r in rows:
        split[r["effort"] or "default"] = split.get(r["effort"] or "default", 0) + 1
        note = _note(r["note"])
        keys = [k for k in note if "escalat" in str(k)]
        # releases that can escalate write a `triggers` list on every turn, so it marks the log
        logged = logged or bool(keys) or isinstance(note.get("triggers"), list)
        escalated += any(bool(note[k]) for k in keys)
        for t in _triggers(note):
            why[t] = why.get(t, 0) + 1
    line = f"{len(rows)} turns: " + ", ".join(f"{n} {k}" for k, n in sorted(split.items(), key=lambda kv: -kv[1]))
    line += f"; {escalated} escalated low to high" if logged else "; escalations not logged"
    if why:
        line += "; high-effort triggers: " + ", ".join(f"{k} {n}" for k, n in sorted(why.items(), key=lambda kv: -kv[1])[:5])
    return line


def _note(raw: Any) -> dict:
    try:
        note: Any = json.loads(raw or "{}")
    except ValueError:
        return {}
    return note if isinstance(note, dict) else {}


def _triggers(note: dict) -> list[str]:
    """A turn's trigger labels: each entry's text before the first ':' or '(', cut to 40 chars,
    so "jev: needs thought (stuck 0.80)" counts as "jev" and "escalated: <why>" as "escalated"."""
    trigs = note.get("triggers")
    if not isinstance(trigs, list):
        trig = note.get("trigger") or note.get("unblock")
        trigs = [trig] if trig else []
    labels = (re.split(r"[:(]", str(t), maxsplit=1)[0].strip()[:40] for t in trigs if t)
    return [lab for lab in labels if lab]


def triggers_line(db: DB, since: float) -> str:
    """Coordinator turns per trigger label, the raised vs routine share with their cost, and the
    `escalations` counts (routine turns escalated, escalations refused)."""
    rows = db.q("SELECT note, cost_usd FROM runs WHERE role='coordinator' AND started>=?", (since,))
    if not rows:
        return "no coordinator turns"
    per: dict[str, int] = {}
    raised = [0, 0.0]
    routine = [0, 0.0]
    for r in rows:
        trigs = _triggers(_note(r["note"]))
        side = raised if trigs else routine
        side[0] += 1
        side[1] += float(r["cost_usd"] or 0)
        for t in trigs:
            per[t] = per.get(t, 0) + 1
    n = len(rows)
    line = (f"raised {raised[0]} ({100 * raised[0] / n:.0f}%, ${raised[1]:.2f}), "
            f"routine {routine[0]} ({100 * routine[0] / n:.0f}%, ${routine[1]:.2f})")
    if per:
        line += "; per trigger: " + ", ".join(f"{k} {v}" for k, v in sorted(per.items(), key=lambda kv: (-kv[1], kv[0])))
    from .coordinator import ESCALATIONS_KEY
    esc = db.kv(ESCALATIONS_KEY, {}) or {}
    line += f"; escalations: {int(esc.get('n', 0))} (refused {int(esc.get('refused', 0))})"
    return line


def lines(db: DB, now: float | None = None) -> list[str]:
    """The daily review's 'Unblocking quality' lines."""
    now = time.time() if now is None else now
    out = []
    for label, span in WINDOWS:
        eps = episodes(db, now - span, now)
        for kind in STUCK.values():
            out.append(f"stuck {kind}, {label}: {stuck_line([e for e in eps if e['kind'] == kind])}")
        out.append(f"self-waits, {label}: {self_waits_line(self_waits(db, now - span))}")
    for label, span in WINDOWS:
        out.append(f"asks, {label}: {asks_line(asks(db, now - span, now))}")
    out.append(f"coordinator, 24 h: {turns_line(db, now - 86400)}")
    out.append(f"coordinator triggers, 24 h: {triggers_line(db, now - 86400)}")
    return out
