# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A wait on an outside listing or resource already shown absent is decided now, model-free.

A waiting task's retry_when, or a queued task's start_when, that queries something outside (curl, wget, a
URL, `gh api`, a package index) and keeps answering not yet with the same absent result (an HTTP 404, not
found, not listed, withdrawn, unavailable) `waiting.absent_close_n` times in a row (default 3, 0: off) is
closed: the task moves to done with outcome `unavailable` and a summary quoting the evidence, and its
deadline is no longer waited for. So is one whose hand-off (or start_why) already cites settled evidence
(repeated 404s, 'not listed', 'no longer served') once its probe has said not yet. Daemon.check_progress,
the stall detector, runs it with the progress check's per-task counts (progress.note_probe's `same`).

Exempt, left to the deadline and the normal stall path: a credential, funds or quota-reset wait (budget,
spend, quota, rate limit, a daily reset, login, 401/403/429; such a wait ends by time), a time-only probe,
the task's own job or reply waits (progress.OWN_WAIT_RE, ack-wait, a user pause), a user step, a busy lock
or unreachable host (exit 75, 255), a 'not yet'/'pending' answer that expects arrival, and code and review
tasks and any task with its own worktree (worktree.own_worktree), whose branch holds work a close would
drop. A wait is a waiting hand-off (db.waiting_handoff: status queued, last result waiting) or a start_when
deferral; the daemon never writes a task status 'waiting'.

Each close registers one reopen watch: the same probe, re-run model-free every REOPEN_EVERY_S off the tick
(Daemon._probe_now, the probe re-check runner) for REOPEN_FOR_S. The first pass queues one follow-up task
(`reopens:<id>`) carrying the closed task's spec, and ends the watch; a watch that never passes lapses.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from .db import deferral, dump_result, load_result, waiting_handoff, without_deferral

KEY = "absent_waits"     # kv: {"closed": {tid: rec}, "watches": {tid: watch}, "reopened": {tid: follow-up id}}
CLOSE_N = 3              # default of waiting.absent_close_n
REOPEN_EVERY_S = 6 * 3600
REOPEN_FOR_S = 14 * 86400
KEEP_S = 30 * 86400      # closed and reopened records are kept this long after their watch ends
OUTCOME = "unavailable"
LABEL = "reopens"

OUTSIDE_RE = re.compile(r"\b(?:curl|wget|https?://|gh\s+api|pip3?\s+(?:index|download)|npm\s+view|"
                        r"docker\s+(?:manifest|pull)|skopeo|huggingface|hf\s+\w+)", re.I)
ABSENT_RE = re.compile(r"\b404\b|\bnot[ _-]?found\b|\bnot listed\b|\bunlisted\b|\bno longer (?:listed|served|"
                       r"offered|available)\b|\bwithdrawn\b|\bdoes not exist\b|\bno such (?:model|resource|"
                       r"package|image|repo\w*|tag|release)\b|\bunavailable\b|\bnot (?:available|served|offered)\b",
                       re.I)
# A not-yet that expects arrival is no absence.
PENDING_RE = re.compile(r"\bnot yet\b|\buntil\b|\bpending\b|\bin progress\b|\bqueued\b|\bbuilding\b|\bwaiting\b",
                        re.I)
SETTLED_RE = re.compile(r"\b(?:\d+|two|three|four|five|several|many|repeated(?:ly)?)\s*(?:x\s*)?(?:HTTP\s*)?"
                        r"404s?\b|\b404s\b|\bmodel_not_found\b|\b(?:not|never|no longer) (?:been )?listed\b|"
                        r"\b(?:does|did) not list\b|\bno longer (?:served|offered|available)\b|\bwithdrawn\b|"
                        r"\bnot served\b", re.I)
EXEMPT_RE = re.compile(r"\b(?:budget|spend|spent|quota|credits?|funds?|billing|rate[ -]?limit\w*|resets?|daily|"
                       r"log(?:ged)?[ -]?(?:in|out)|sign[ -]?in|credentials?|auth\w*|unauthori[sz]ed|forbidden|"
                       r"expired|401|402|403|429)\b|\bttp\s+(?:hours|budget|secret\s+check)\b|date\s+\+%s", re.I)
SKIP_KINDS = ("code", "review")


def close_n(cfg: dict | None) -> int:
    try:
        return max(0, int(((cfg or {}).get("waiting") or {}).get("absent_close_n", CLOSE_N) or 0))
    except (TypeError, ValueError):
        return CLOSE_N


def absent(reason: Any) -> bool:
    """Whether a probe's answer says the thing is absent (not merely not there yet)."""
    s = str(reason or "")
    return bool(ABSENT_RE.search(s)) and not PENDING_RE.search(s)


def settled(text: Any) -> bool:
    """Whether a hand-off already cites settled evidence of absence."""
    return bool(SETTLED_RE.search(str(text or "")))


def load(db) -> dict:
    st = db.kv(KEY) or {}
    if not isinstance(st, dict):
        st = {}
    for k in ("closed", "watches", "reopened"):
        if not isinstance(st.get(k), dict):
            st[k] = {}
    return st


def _clip(text: Any, n: int = 200) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _exempt(db, cfg: dict, t: dict, probe: str, res: dict, rec: dict) -> str:
    """Why the wait is left to its deadline, or ''."""
    from . import ackwait, progress, userpause, worktree
    if t["kind"] in SKIP_KINDS or worktree.own_worktree(t):
        return f"a {t['kind']} task whose branch may hold work"
    if not OUTSIDE_RE.search(probe):
        return "its probe queries nothing outside"
    if progress.OWN_WAIT_RE.search(probe) or ackwait.is_probe(probe, cfg) or userpause.is_probe(probe, cfg):
        return "its own job, a reply or a user pause"
    if str(res.get("wait_kind") or "").strip().lower() in ("self", "user", "user-action"):
        return f"wait_kind {res.get('wait_kind')}"
    if rec.get("rc") in (75, 255) or isinstance(rec.get("rc"), str):
        return "a busy lock, an unreachable host or a timeout"
    said = " ".join(str(x or "") for x in (probe, res.get("waiting_for"), deferral(t).get("why"), rec.get("reason")))
    if m := EXEMPT_RE.search(said):
        return f"a credential, funds or quota-reset wait ({m.group(0)}): it ends by time"
    return ""


def due(db, cfg: dict, now: float, current_probe: Callable[[dict], str | None]) -> list[dict]:
    """The waits to close now: [{"task", "probe", "n", "reason", "why"}]."""
    from . import progress
    n = close_n(cfg)
    if not n:
        return []
    st, out = load(db), []
    for k, rec in (progress.load(db).get("probes") or {}).items():
        if k in st["watches"] or k in st["closed"] or not isinstance(rec, dict):
            continue
        try:
            t = db.task(int(k))
        except ValueError:
            continue
        res = waiting_handoff(t) if t else {}
        if not t or not (res or t["status"] == "queued" and deferral(t).get("when")):
            continue   # neither a waiting hand-off nor a start_when deferral
        probe = current_probe(t)
        if not probe or probe != rec.get("probe"):
            continue
        if _exempt(db, cfg, t, probe, res, rec):
            continue
        same, reason = int(rec.get("same") or 0), str(rec.get("reason") or "")
        cites = " ".join(str(x or "") for x in (res.get("summary"), res.get("waiting_for"), deferral(t).get("why")))
        if absent(reason) and same >= n:
            why = f"its probe gave the same absent answer {same} times in a row"
        elif settled(cites) and int(rec.get("n") or 0) >= 1:
            why = f"its hand-off already cites settled evidence ({_clip(SETTLED_RE.search(cites).group(0), 60)})"
        else:
            continue
        out.append({"task": t["id"], "probe": probe, "n": int(rec.get("n") or 0), "same": same,
                    "reason": _clip(reason), "why": why, "first": rec.get("first")})
    return out


def close(db, c: dict, now: float, where=None, every_s: float = REOPEN_EVERY_S, for_s: float = REOPEN_FOR_S) -> bool:
    """Close task c["task"] as unavailable and register its one reopen watch. False when it was
    already closed, or is no longer the open wait `due` saw."""
    from . import progress, startgate, timefmt
    tid, key = int(c["task"]), str(c["task"])
    with db.tx():
        st, t = load(db), db.task(tid)
        if key in st["watches"] or key in st["closed"] or not t or t["status"] != "queued":
            return False
        at = timefmt.short(now, where) if where is not None else time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        summary = (f"Closed as {OUTCOME} by the daemon at {at}, without waiting for its deadline: {c['why']} "
                   f"(last answer: {c['reason'] or 'not yet'}; {c['n']} not-yet answers). A reopen watch re-runs "
                   f"the probe every {every_s / 3600:g} h for {for_s / 86400:g} days and queues a follow-up if it "
                   f"passes. Probe: {_clip(c['probe'], 300)}")
        prev = load_result(t["result"])
        handoff = {"status": "done", "outcome": OUTCOME, "summary": summary, "closed_by": "daemon:absentwait",
                   "absent": {"probe": c["probe"], "reason": c["reason"], "n": c["n"], "same": c["same"],
                              "why": c["why"]},
                   **({"waited_for": prev["waiting_for"]} if prev.get("waiting_for") else {})}
        db.update_task(tid, status="done", result=dump_result(handoff), blocked_reason=None, not_before=None,
                       labels=without_deferral(json.loads(t["labels"] or "[]")))
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, f"task:{tid}", "task_done", "normal", f"#{tid} {t['title']} → done ({OUTCOME}): {summary}"[:4000],
              "queued", tid))
        st["closed"][key] = {"at": now, "probe": c["probe"], "reason": c["reason"], "why": c["why"]}
        st["watches"][key] = {"probe": c["probe"], "since": now, "until": now + for_s, "next": now + every_s,
                              "every_s": every_s}
        db.set_kv(KEY, st)
    progress.forget_probe(db, tid)
    startgate.forget(db, tid=tid)
    return True


def _reopen(db, tid: int, w: dict, now: float) -> int | None:
    t = db.task(tid)
    if not t:
        return None
    closed = (load(db)["closed"].get(str(tid)) or {})
    spec = (f"Reopened model-free: task #{tid} was closed {OUTCOME} ({_clip(closed.get('why'), 160)}; last answer: "
            f"{_clip(closed.get('reason'), 160)}). Its reopen watch's probe now passes: `{_clip(w['probe'], 300)}`. "
            f"Do what #{tid} could not; its spec follows.\n\n{t['spec'] or ''}")
    labels = [lb for lb in json.loads(t["labels"] or "[]") if isinstance(lb, str)
              and lb.partition(":")[0] in ("resource", "exclusive", "needs_device", "untrusted_input")]
    return db.add_task(f"reopen: {t['title']}", spec, kind=t["kind"], tier=t["tier"], priority=t["priority"],
                       provider=t["provider"], origin="daemon", labels=[*labels, f"{LABEL}:{tid}"])


def run_watches(db, now: float, run_probe: Callable[[str], Any]) -> list[dict]:
    """Re-run each due reopen watch's probe (run_probe: its exit code, None while it has none yet, judged
    on a later pass). A pass queues the follow-up once and ends the watch; one past its period lapses."""
    st, out, changed = load(db), [], False
    for key, w in sorted(st["watches"].items()):
        if not isinstance(w, dict) or not w.get("probe") or now >= float(w.get("until") or 0):
            st["watches"].pop(key)
            changed = True
            out.append({"task": key, "event": "lapsed"})
            continue
        if now < float(w.get("next") or 0):
            continue
        rc = run_probe(w["probe"])
        if rc is None:
            continue
        changed = True
        w.update(last_rc=rc, last_at=now)
        if rc != 0:
            w["next"] = now + float(w.get("every_s") or REOPEN_EVERY_S)
            continue
        with db.tx():
            cur = load(db)
            if key not in cur["watches"]:
                continue   # another pass took it
            fid = _reopen(db, int(key), w, now)
            cur["watches"].pop(key)
            if fid:
                cur["reopened"][key] = fid
            db.set_kv(KEY, cur)
        st = {**cur, "watches": {**cur["watches"], **{k: v for k, v in st["watches"].items()
                                                         if k in cur["watches"] and k != key}}}
        out.append({"task": key, "event": "reopened", "followup": fid})
    if changed:
        for part in ("closed", "reopened"):   # a record whose watch ended long ago is dropped
            for k in [k for k in st[part] if k not in st["watches"]
                      and now - float((st["closed"].get(k) or {}).get("at") or 0) > KEEP_S]:
                st[part].pop(k, None)
        db.set_kv(KEY, st)
    return out
