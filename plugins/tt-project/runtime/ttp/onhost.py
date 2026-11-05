# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Whether a task's work has started on its host, and what the coordinator may say about it. Model-free.

A host task names a machine or device resource (`resource:`, `exclusive:` or `needs_device` labels).
Its record of starting on the host is written in its run dir (FILE): the first time its worker holds
a lock on a resource (`ttp lock` grants, the runner's hold of an `exclusive:` resource) and its first
`ttp detach --remote` on a machine. started() reads it back across the task's runs.

line() is the state the digest rows and Expected finish show for a host task:
- 'queued for a worker, ETA <start>' (queued, or open with no record yet),
- 'worker running, waiting for <resource> since HH:MM' (running, no grant yet),
- 'on <resource> since HH:MM' (a grant recorded),
- 'stop requested; N jobs still running on <resource>' (a cancelled, failed or exhausted task whose
  detached jobs still run: until the job sweep has ended them).

guard() checks a coordinator reply or notify before it is posted. Its scope is the host tasks the text
names (#id), the reply's backing tasks (open tasks answering its chat, its `owners`) and host tasks this
batch adds or updates. When one of them has not started on its host (or was stopped while its jobs
still run) and a sentence claims an action is under way or done ('now', '...ing it', 'started',
'stopped', 'only X runs'), that sentence is kept but marked planned, not started, and the honest status
of each such task is appended. Nothing else in the message changes and it is posted at once: a rewrite
in place keeps every word (goal 1) and the user never reads the claim as a fact."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

FILE = "onhost.json"
CLOSED = ("done", "failed", "cancelled", "exhausted")
MARK = "Planned, not started on the host yet:"

_PROG_OBJ = re.compile(r"(?i)\b([a-z]{3,}ing)\s+(?:it|them|this|that|those|these|the|its|their|all|both)\b")
_PROG = re.compile(r"(?i)\b([a-z]{3,}ing)\b")
_NOW = re.compile(r"(?i)\b(?:now|right away|as we speak|at the moment|currently)\b")
_RUNS = re.compile(r"(?i)\b(?:runs|running|under ?way|in progress|happening|going on)\b")
_ONLY = re.compile(r"(?i)\bonly\b[^.!?\n]{0,80}?\b(?:runs|run|is running|are running|remains?|is left)\b")
_DONE = re.compile(r"(?i)\b(?:started|stopped|killed|cancell?ed|halted|terminated|ended|aborted|power[- ]?cycled|"
                   r"rebooted|restarted|reset (?:it|them|the)|(?:is|are|was|were) (?:done|finished|complete)|"
                   r"finished|completed)\b")
# Words before a done-verb that make it a plan, a condition or a negation, not a claim.
_HEDGE = re.compile(r"(?i)(?:\b(?:will|to be|to get|not|never|once|until|when|after|if|before|unless|gets?|be)|n't)\s+"
                    r"(?:\w+\s+){0,2}$")
# -ing words that say nothing is under way: planning, waiting, queueing, and words that are no verb.
_HONEST = {"queueing", "queuing", "scheduling", "waiting", "planning", "asking", "adding", "filing", "holding",
           "pending", "nothing", "something", "anything", "everything", "thing", "things", "during", "morning",
           "evening", "according", "string", "bring", "spring", "ring", "king", "setting", "warning", "meaning",
           "including", "following", "remaining", "outstanding", "existing", "missing", "upcoming", "incoming",
           "outgoing", "ongoing", "building", "being", "ceiling", "duckling", "sibling", "wing", "swing", "sing",
           "bling", "sling", "thing", "fling", "cling", "sting", "wring", "ping", "tracking", "watching", "checking",
           "reading", "reporting", "proposing", "suggesting", "recommending", "considering", "expecting", "hoping"}


def record(run_dir, resources, kind: str = "lock", now: float | None = None) -> None:
    """Note the first time this run held each resource (or ran a remote command on a machine). Never
    raises: a missing record makes the coordinator say 'queued', never stops the command."""
    if not run_dir or not resources:
        return
    import fcntl
    from .project import durable_write
    run_dir, now = Path(run_dir), time.time() if now is None else now
    try:
        with open(run_dir / f"{FILE}.lock", "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            try:
                got = json.loads((run_dir / FILE).read_text())
            except (OSError, ValueError):
                got = {}
            got = got if isinstance(got, dict) else {}
            new = {str(r): {"ts": now, "kind": kind} for r in resources if r and str(r) not in got}
            if new:
                durable_write(run_dir / FILE, json.dumps({**got, **new}))
    except OSError:
        pass


def _labels(task: dict) -> list[str]:
    try:
        got = json.loads(task.get("labels") or "[]")
    except (TypeError, ValueError):
        return []
    return [x for x in got if isinstance(x, str)] if isinstance(got, list) else []


def resources(task: dict, cfg: dict | None = None) -> list[str]:
    """The machine or device resources a task names, as their canonical locks: `resource:`, `exclusive:`
    and, for `needs_device`, the project's device lock. Empty: not a host task."""
    from . import locks
    cfg = cfg or {}
    labels = _labels(task)
    out = [locks.canonical(cfg, lb.split(":", 1)[1]) for lb in labels
           if lb.split(":", 1)[0] in ("resource", "exclusive") and ":" in lb and lb.split(":", 1)[1]]
    if "needs_device" in labels and (dev := sorted(locks.device_locks(cfg))):
        out.append(locks.canonical(cfg, dev[0]))
    elif "needs_device" in labels and not out:
        out.append("the device")
    return list(dict.fromkeys(out))


def _run_dirs(p, tid: int) -> list[tuple[dict, Path]]:
    rows = p.db.q("SELECT * FROM runs WHERE task=? AND role!='coordinator' ORDER BY id", (tid,))
    return [(r, Path(r["dir"]) if r["dir"] else p.runs / str(r["id"])) for r in rows]


def started(p, task: dict, cfg: dict | None = None) -> tuple[float, str] | None:
    """(when, resource) of the task's first recorded hold of a resource it names (or remote command on a
    machine it names), across its runs; None when it never had one."""
    from . import locks
    cfg = p.config() if cfg is None else cfg
    named = set(resources(task, cfg))
    best = None
    for _, d in _run_dirs(p, task["id"]):
        try:
            got = json.loads((d / FILE).read_text())
        except (OSError, ValueError):
            continue
        for res, rec in (got.items() if isinstance(got, dict) else []):
            try:
                ts = float(rec["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            canon = locks.canonical(cfg, res)
            if (canon in named or res in named) and (best is None or ts < best[0]):
                best = (ts, canon)
    return best


def live_jobs(p, task: dict) -> tuple[int, list[str]]:
    """(count, resources) of the task's detached jobs that still run, across its runs."""
    from . import jobstop
    n, res = 0, []
    for _, d in _run_dirs(p, task["id"]):
        for rc in jobstop._live_jobs(d):
            n += 1
            try:
                res += sorted(jobstop.job_resources(p, rc))
            except Exception:
                pass
    return n, list(dict.fromkeys(res))


def _waiting_since(p, task: dict) -> float | None:
    from . import locks
    run = p.db.one("SELECT * FROM runs WHERE task=? AND status='running' AND role!='coordinator' "
                   "ORDER BY id DESC LIMIT 1", (task["id"],))
    if not run:
        return None
    d = Path(run["dir"]) if run["dir"] else p.runs / str(run["id"])
    opened = [s for s, e in locks.wait_spans(d) if e is None]
    return min(opened) if opened else run["started"]


def queued_line(e: dict | None, where, now: float) -> str:
    """'queued for a worker, ETA <start> to start (finish <end>, critical path ...)' from an eta estimate."""
    from . import eta, timefmt
    if not e:
        return "queued for a worker, ETA: no estimate yet"
    if e.get("why"):
        return f"queued for a worker, {eta.field(e, where, now)}"
    path = "→".join(f"#{i}" for i in e["path"] if i)
    begin = e.get("start")
    head = f"ETA {timefmt.short(begin, where, now)} to start, " if begin else "ETA "
    return (f"queued for a worker, {head}finish {timefmt.short(e['end'], where, now)}"
            + (f" (critical path {path})" if path else ""))


def state(p, task: dict, now: float | None = None, est=None, cfg: dict | None = None) -> dict | None:
    """{"kind": queued|waiting|on|stop, "line": text, "started": bool} for a host task (or a stopped
    task with live jobs); None for other tasks. `started`: its work may be described as happening."""
    from . import eta, timefmt
    now = time.time() if now is None else now
    cfg = p.config() if cfg is None else cfg
    named = resources(task, cfg)
    st = task["status"]
    if st in CLOSED:
        if st == "done":
            return None
        n, res = live_jobs(p, task)
        if not n:
            return None
        where = ", ".join(res or named) or "its host"
        return {"kind": "stop", "started": False,
                "line": f"stop requested; {n} job{'s' if n != 1 else ''} still running on {where}"}
    if not named:
        return None
    got = started(p, task, cfg)
    if got and st != "queued":
        return {"kind": "on", "started": True, "line": f"on {got[1]} since {timefmt.short(got[0], p, now)}"}
    if st == "running":
        since = _waiting_since(p, task)
        return {"kind": "waiting", "started": False,
                "line": f"worker running, waiting for {', '.join(named)}"
                        + (f" since {timefmt.short(since, p, now)}" if since else "")}
    try:
        e = (est.task(task["id"]) if est is not None else eta.estimate(p.db, task["id"], now))
    except Exception:
        e = None
    return {"kind": "queued", "started": False, "line": queued_line(e, p, now)}


def line(p, task: dict, now: float | None = None, est=None) -> str:
    try:
        got = state(p, task, now, est)
    except Exception:
        return ""
    return got["line"] if got else ""


def stop_lines(p, now: float | None = None, limit: int = 20) -> list[str]:
    """The digest section of stopped tasks whose detached jobs still run (one line each)."""
    from .jobstop import ENDED, SWEEP_DAYS
    now = time.time() if now is None else now
    marks = ",".join("?" * len(ENDED))
    rows = p.db.q(f"SELECT * FROM tasks WHERE status IN ({marks}) AND updated>? ORDER BY updated DESC LIMIT ?",
                  (*ENDED, now - SWEEP_DAYS * 86400, 200))
    out = []
    for t in rows:
        got = line(p, t, now)
        if got:
            out.append(f"- #{t['id']} {t['status']}: {got} (say 'stop requested' until this line is gone)")
        if len(out) >= limit:
            break
    return (["## Stops not yet confirmed (the job sweep has not seen these jobs end)"] + out) if out else []


# The check on reply and notify text --------------------------------------------------------------------------

def _progressive(s: str, pat) -> bool:
    return any(m.group(1).lower() not in _HONEST for m in pat.finditer(s))


def claims(sentence: str) -> bool:
    """Whether a sentence says an action is under way or done (high recall; scoped by guard())."""
    s = sentence.strip()
    if not s or s.startswith(MARK):
        return False
    if _NOW.search(s) and (_progressive(s, _PROG) or _RUNS.search(s)):
        return True
    if _progressive(s, _PROG_OBJ) or _ONLY.search(s):
        return True
    if re.search(r"(?i)\b(?:is|are)\s+(?:now\s+)?(?:running|under ?way|in progress|happening)\b", s):
        return True
    return any(not _HEDGE.search(s[:m.start()]) for m in _DONE.finditer(s))


def scope(p, text: str, chat: str | None, owners=(), batch_ids=(), pending: list | None = None) -> list[dict]:
    """The tasks a reply speaks for: named (#id), its owners (`owners`), this batch's (`batch_ids`) and
    `pending` (host task_add actions of this batch not added yet). Open tasks answering `chat` are covered
    too, marked `via_chat`: guard() ties a sentence to one of those only by its #id or its resource."""
    ids = {int(x) for x in re.findall(r"#(\d{1,9})\b", text or "")}
    ids |= {int(x) for x in owners or [] if str(x).isdigit()} | {int(x) for x in batch_ids or []}
    out = [t for i in sorted(ids) if (t := p.db.task(i))]
    if chat:
        out += [dict(t, via_chat=True) for r in p.db.q("SELECT id FROM tasks WHERE reply_chat=? AND status NOT IN "
                                                       "('done','failed','cancelled','exhausted') ORDER BY id", (chat,))
                if r["id"] not in ids and (t := p.db.task(r["id"]))]
    return out + list(pending or [])


def _names(sentence: str, task: dict) -> bool:
    """Whether a sentence names one of a task's resources (or that resource's host part before '-device')."""
    for r in resources(task):
        for n in {r, re.sub(r"(?i)-device$", "", r)}:
            if n and re.search(rf"(?i)(?<![\w-]){re.escape(n)}(?![\w-])", sentence):
                return True
    return False


def guard(p, text: str, tasks: list[dict], now: float | None = None) -> tuple[str, list[str]]:
    """(text to post, notes for the coordinator). Unchanged unless a covered task has not started on its
    host (or was stopped with jobs still running) and a sentence claims an action is under way or done."""
    now = time.time() if now is None else now
    if not text or not tasks:
        return text, []
    parts = re.split(r"((?<=[.!?;])[ \t]+|\n)", text)
    hits = [k for k in range(0, len(parts), 2) if claims(parts[k])]
    if not hits:
        return text, []
    from . import eta
    try:
        cfg = p.config()
        est = eta._Est(p.db, now)
    except Exception:
        return text, []
    waiting = []   # (task, honest status) of each covered task not started on its host
    for t in tasks:
        try:
            if t.get("pending"):
                got = {"started": False, "line": queued_line(_new_estimate(est, t), p, now)}
            else:
                got = state(p, t, now, est, cfg)
        except Exception:
            got = None
        if got and not got["started"]:
            who = f"#{t['id']}" if t.get("id") else f"new task '{str(t.get('title') or '')[:60]}'"
            waiting.append((t, f"{who}: {got['line']}"))
    if not waiting:
        return text, []
    ids = {t["id"] for t, _ in waiting if t.get("id")}
    direct = [t for t, _ in waiting if not t.get("via_chat")]
    marked = 0
    for k in hits:
        named = {int(x) for x in re.findall(r"#(\d{1,9})\b", parts[k])}
        # A sentence is about an unstarted task when it names its #id; naming no #id, when it names that
        # task's resource or the reply speaks for exactly one unstarted task (a chat's other open tasks do
        # not count). Any other claim sentence is left as written: it may be about other, finished work.
        if (named & ids) if named else (len(direct) == 1 or any(_names(parts[k], t) for t, _ in waiting)):
            parts[k] = f"{MARK} {parts[k].strip()}"
            marked += 1
    if not marked:
        return text, []
    honest = [h for _, h in waiting]
    out = "".join(parts).rstrip() + "\nStatus now: " + "; ".join(honest)
    note = (f"onhost: a message said work was under way or done that has not started on its host "
            f"({'; '.join(honest)[:300]}); it was sent marked as planned with that status. Describe an "
            f"action as happening only once STATE shows it on the host")
    return out, [note]


def _new_estimate(est, t: dict) -> dict | None:
    """The estimate of a task this batch adds and that has no id yet (a queued copy, id 0)."""
    fake = {"id": 0, "status": "queued", "kind": t.get("kind") or "work", "tier": t.get("tier") or "standard",
            "labels": json.dumps(t.get("labels") or []), "depends_on": json.dumps(t.get("depends_on") or []),
            "not_before": None, "title": t.get("title") or "", "priority": t.get("priority") or 3}
    try:
        return est._task(fake, 0, (0,))
    except Exception:
        return None


def pending(actions: list, cfg: dict | None) -> list[dict]:
    """This batch's task_add actions for host work (they have no id while a reply before them is posted)."""
    out = []
    for a in actions or []:
        if not isinstance(a, dict) or a.get("type") != "task_add":
            continue
        kind = "exclusive" if a.get("exclusive") else "resource"
        labels = [f"{kind}:{r}" for r in (a.get("resources") or []) if isinstance(r, str) and r]
        if a.get("needs_device"):
            labels.append("needs_device")
        if labels:
            out.append({"pending": True, "id": None, "title": a.get("title"), "kind": a.get("kind"),
                        "tier": a.get("tier"), "labels": labels, "depends_on": a.get("depends_on") or [],
                        "priority": a.get("priority")})
    return out
