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
- 'detached job running on <host>' (no grant, but a detached job it started still runs),
- 'stop requested; N jobs still running on <resource>' (a cancelled, failed or exhausted task whose
  detached jobs still run: until the job sweep has ended them). Its `ttp detach --remote` jobs count
  too, from their mirrors in its run dirs (detach.mirror_save): one counts until an answer from its host
  shows it ended. The job sweep asks each host (refresh_remote: one bounded ssh call per job, a host
  that did not answer is not asked again in that sweep) and records the answer in the mirror, so
  reading the line runs no ssh. A host that cannot be reached reads 'still possibly running (host
  unreachable)', never stopped.

guard() checks a coordinator reply or notify before it is posted. Its scope is the host tasks the text
names (#id), the reply's backing tasks (open tasks answering its chat, its `owners`) and host tasks this
batch adds or updates. When one of them has not started on its host (or was stopped while its jobs
still run) and a sentence claims an action is under way or done ('now', '...ing it', 'started',
'stopped', 'only X runs') about it, that sentence is kept but marked planned, not started, and the honest
status of each such task is appended. A claim is about a task only when it names its #id or its resource
or host; any other sentence states a fact about other work and stays as written. A task with a detached
job still live on its host (a queued task's driver too) is under way. Inside a block meant to be posted
as written (segments(): a fenced code block, text between '---' lines, a '>' quote) nothing is inserted:
the marker goes on its own line after the block, quoting the sentence. Nothing else in the message changes
and it is posted at once: a rewrite in place keeps every word (goal 1) and the user never reads the claim as a fact.

A sentence that reports a past event (past tense only, or a stated time at or before the send time) and
one backed by a reply claim whose `expect` probe passes now are left as written: they report what was
observed, not a plan. A start verb ('started', 'began', 'launched', 'kicked off') is never a past report:
it says work runs now. Only a stop not yet confirmed (live jobs) still marks a past 'stopped'. The
appended status line is the harness's, never the coordinator's words: no promise is read from it."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

FILE = "onhost.json"
CLOSED = ("done", "failed", "cancelled", "exhausted")
MARK = "Planned, not started on the host yet:"
STATUS_HEAD = "Status now: "   # the honest status line guard() appends (claimcheck and promises skip it)
CLAIM_PROBE_S = 20       # the reply claims' expect probes of one message, run at send time, all together

_PROG_OBJ = re.compile(r"(?i)\b([a-z]{3,}ing)\s+(?:it|them|this|that|those|these|the|its|their|all|both)\b")
_PROG = re.compile(r"(?i)\b([a-z]{3,}ing)\b")
_NOW = re.compile(r"(?i)\b(?:now|right away|as we speak|at the moment|currently)\b")
_RUNS = re.compile(r"(?i)\b(?:runs|running|under ?way|in progress|happening|going on)\b")
_ONLY = re.compile(r"(?i)\bonly\b[^.!?\n]{0,80}?\b(?:runs|run|is running|are running|remains?|is left)\b")
# A start verb says work began and runs now: never a past report, whatever its tense or stated time.
_START = re.compile(r"(?i)\b(?:started|began|begun|launched|kicked off)\b")
_DONE = re.compile(r"(?i)\b(?:stopped|killed|cancell?ed|halted|terminated|ended|aborted|power[- ]?cycled|"
                   r"rebooted|restarted|reset (?:it|them|the)|(?:is|are|was|were) (?:done|finished|complete)|"
                   r"\w+['’]s (?:done|finished|complete)|finished|completed)\b")
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
    try:   # a stored task's labels are JSON; a pending task_add's (pending()) a list
        got = task.get("labels") if isinstance(task.get("labels"), list) else json.loads(task.get("labels") or "[]")
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


def remote_jobs(run_dir) -> list[dict]:
    """The mirrors of a run's `ttp detach --remote` jobs not known to have ended: {"name", "host", "path",
    "probe"}. A job whose host last answered that no driver is recorded at its path is left out."""
    out = []
    for meta in sorted(Path(run_dir).glob("*.remote.json")):
        try:
            m = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(m, dict) or m.get("end") or not m.get("host") or not m.get("path"):
            continue
        probe = m.get("probe") if isinstance(m.get("probe"), dict) else {}
        if probe.get("state") != "none":
            out.append({"name": meta.name[:-len(".remote.json")], "host": str(m["host"]), "path": str(m["path"]),
                        "probe": probe})
    return out


def live_jobs(p, task: dict) -> tuple[int, list[str], int, list[str]]:
    """(count, resources, unsure count, unsure hosts) of the task's detached jobs that still run, across its
    runs: local ones (jobstop._live_jobs) and remote ones not known to have ended. A remote job whose host
    did not answer the last probe is unsure: still possibly running, never counted as stopped."""
    from . import jobstop, locks
    n, res, u, ures = 0, [], 0, []
    cfg = None
    for _, d in _run_dirs(p, task["id"]):
        for rc in jobstop._live_jobs(d):
            n += 1
            try:
                res += sorted(jobstop.job_resources(p, rc))
            except Exception:
                pass
        for j in remote_jobs(d):
            try:
                cfg = p.config() if cfg is None else cfg
                host = locks.canonical(cfg, j["host"])
            except Exception:
                host = j["host"]
            if j["probe"].get("state") == "unreachable":
                u, ures = u + 1, ures + [host]
            else:
                n, res = n + 1, res + [host]
    return n, list(dict.fromkeys(res)), u, list(dict.fromkeys(ures))


REMOTE_TIMEOUT_S = 10       # one ssh call for a remote job's liveness
REMOTE_EVERY_S = 60         # a remote job seen running is asked about again after this long
REMOTE_UNREACHABLE_S = 300  # one whose host did not answer, after this long
REMOTE_BUDGET_S = 20        # ssh time one sweep spends; the rest wait for the next sweep
OPEN_IDLE = ("queued", "waiting", "blocked")   # open, no worker: refresh_remote asks after their jobs too


def refresh_remote(p, now: float | None = None, budget: float = REMOTE_BUDGET_S) -> int:
    """The job sweep's model-free probe of the `ttp detach --remote` jobs of tasks that ended (jobstop.ENDED)
    or wait with no worker (OPEN_IDLE: their live driver counts as under way, state()), within SWEEP_DAYS:
    one bounded ssh call per job not known to have ended (detach.mirror_probe), whose
    answer goes to its mirror: an end, 'running', 'none' (no driver at its path) or 'unreachable'. A host
    that did not answer is not asked again in this sweep. The number of ssh calls made."""
    from . import detach
    from .jobstop import ENDED, SWEEP_DAYS
    now = time.time() if now is None else now
    marks = ",".join("?" * (len(ENDED) + len(OPEN_IDLE)))
    rows = p.db.q(f"SELECT DISTINCT r.dir FROM runs r JOIN tasks t ON t.id=r.task WHERE t.status IN ({marks}) "
                  "AND r.dir IS NOT NULL AND COALESCE(r.started, 0)>?", (*ENDED, *OPEN_IDLE, now - SWEEP_DAYS * 86400))
    down: set[str] = set()
    calls, t0 = 0, time.monotonic()
    for row in rows:
        for j in remote_jobs(row["dir"]):
            probe, host = j["probe"], j["host"]
            wait = REMOTE_UNREACHABLE_S if probe.get("state") == "unreachable" else REMOTE_EVERY_S
            try:
                if now - float(probe.get("at") or 0) < wait:
                    continue
            except (TypeError, ValueError):
                pass
            end = None
            if host in down:
                state = "unreachable"
            elif time.monotonic() - t0 >= budget:
                return calls
            else:
                calls += 1
                r, lines = detach.mirror_probe(host, j["path"], row["dir"], timeout=REMOTE_TIMEOUT_S)
                word = (lines[-1].split(" ", 1)[0] if lines else "") if r is not None and r.returncode == 0 else ""
                end = detach.mirror_end(lines[-1]) if word else None
                state = "running" if word == "running" else "none" if word == "none" else \
                    "ended" if end else "unreachable"
                if state == "unreachable":
                    down.add(host)
            try:
                detach.mirror_save(row["dir"], j["name"], host, j["path"], end=end, probe={"at": now, "state": state})
            except OSError:
                pass
    return calls


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
        n, res, u, ures = live_jobs(p, task)
        if not n and not u:
            return None
        where = ", ".join(res or named) or "its host"
        parts = [f"{n} job{'s' if n != 1 else ''} still running on {where}"] if n else []
        if u:
            parts.append(f"{u} job{'s' if u != 1 else ''} still possibly running on {', '.join(ures)} (host unreachable)")
        return {"kind": "stop", "started": False, "line": "stop requested; " + "; ".join(parts)}
    if not named:
        return None
    got = started(p, task, cfg)
    if got and st != "queued":
        return {"kind": "on", "started": True, "line": f"on {got[1]} since {timefmt.short(got[0], p, now)}"}
    n, res, _, _ = live_jobs(p, task)
    if n:   # a detached job it started still runs there (a queued task's driver too): under way
        return {"kind": "on", "started": True,
                "line": f"detached job{'s' if n != 1 else ''} running on {', '.join(res or named)}"}
    if st == "running":
        since = _waiting_since(p, task)
        return {"kind": "waiting", "started": False,
                "line": f"worker running, waiting for {', '.join(named)}"
                        + (f" since {timefmt.short(since, p, now)}" if since else "")}
    try:
        e = (est.task(task["id"]) if est is not None else eta.estimate(p.db, task["id"], now, core=eta.core_of(cfg), delivery=eta.delivery_of(cfg)))
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


def _claim(sentence: str) -> str | None:
    """'start' when a sentence says work began (so runs now), 'now' when it says an action is under way,
    'past' when it only reports one done (a past tense verb), None when it claims neither."""
    s = sentence.strip()
    if not s or s.startswith(MARK):
        return None
    if any(not _HEDGE.search(s[:m.start()]) for m in _START.finditer(s)):
        return "start"
    if _NOW.search(s) and (_progressive(s, _PROG) or _RUNS.search(s)):
        return "now"
    if _progressive(s, _PROG_OBJ) or _ONLY.search(s):
        return "now"
    if re.search(r"(?i)\b(?:is|are)\s+(?:now\s+)?(?:running|under ?way|in progress|happening)\b", s):
        return "now"
    return "past" if any(not _HEDGE.search(s[:m.start()]) for m in _DONE.finditer(s)) else None


def claims(sentence: str) -> bool:
    """Whether a sentence says an action is under way or done (high recall; scoped by guard())."""
    return _claim(sentence) is not None


_LATER = re.compile(r"(?i)\b(?:will|shall|going to|tomorrow|tonight|next|later|until|till|ETA|expected|due|"
                    r"by\s+(?:then|noon|tomorrow|tonight|end))\b|\bby\s+\d|'ll\b|’ll\b")


def _said_before(sentence: str, now: float, tz) -> bool:
    """Whether a sentence states a clock time (with minutes or am/pm, its date when named) at or before
    `now`, and says nothing of later: it reports an event already seen."""
    from datetime import datetime, timezone
    from .obligations import CLOCK_RE, DAY_RE, MONTHS
    if _LATER.search(sentence):
        return False
    day = DAY_RE.search(sentence)
    for c in CLOCK_RE.finditer(sentence):
        h, mi, ap = int(c.group("h")), int(c.group("m") or 0), (c.group("ap") or "").lower()
        if not (c.group("m") or ap) or (ap and not 1 <= h <= 12):
            continue
        h = h % 12 + (12 if ap.startswith("p") else 0) if ap else h
        base = datetime.fromtimestamp(now, timezone.utc if c.group("utc") else tz)
        try:
            when = base.replace(hour=h, minute=mi, second=0, microsecond=0)
            if day:
                when = when.replace(month=MONTHS.index(day.group("mon")[:3].lower()) + 1, day=int(day.group("d")))
        except ValueError:
            continue
        if when.timestamp() <= now:
            return True
    return False


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9#]+", s.replace(MARK, "").lower()))


def _backs(sentence: str, claim: str) -> bool:
    """Whether a reply claim's text is this sentence: one holds the other, or most of their words match."""
    def flat(s: str) -> str:
        return " ".join(re.findall(r"[a-z0-9#]+", s.replace(MARK, "").lower()))
    a, b = flat(sentence), flat(claim)
    if not a or not b:
        return False
    if f" {a} " in f" {b} " or f" {b} " in f" {a} ":
        return True
    one, other = _words(sentence), _words(claim)
    return min(len(one), len(other)) >= 3 and len(one & other) >= 0.6 * min(len(one), len(other))


def passes(p, probe: str, timeout: float = CLAIM_PROBE_S) -> bool:
    """Whether a read-only probe exits 0 now, run as the daemon runs a claim's expect probe; never inside
    a database transaction (False there) and never longer than `timeout` (killed, False)."""
    import subprocess
    from . import landed, machines, probespawn
    from .project import command_env
    if getattr(p.db, "conn", None) is not None and p.db.conn.in_transaction:
        return False
    run = probespawn.Pending(landed.probe_command(probe, p), cwd=str(p.root),
                             env={**machines.mask_devices(command_env(p)), landed.DONE_ENV: "1"}, stdout=False)
    try:
        rc = run.wait(timeout)
    except subprocess.TimeoutExpired:
        run.stop()
        rc = None
    if f := run.take_file():
        f.close()
    return rc == 0


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


_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_RULE = re.compile(r"^\s*-{3,}\s*$")


def segments(text: str) -> list[tuple[str, bool]]:
    """`text` cut into (chunk, is_block) pieces that join back to it exactly. A block is text meant to be
    posted as written: a fenced code block (to its closing fence, or the end), the lines between two '---'
    lines (a lone '---' is a rule, not a block) or a run of '>' quote lines. A block chunk leaves out its
    last newline, so a marker line goes right after it (marker_after)."""
    lines = (text or "").splitlines(keepends=True)
    out, plain, i = [], "", 0

    def fence_end(k: int) -> int:
        m = _FENCE.match(lines[k])
        for j in range(k + 1, len(lines)):
            if lines[j].lstrip().startswith(m.group(1)[0] * len(m.group(1))):
                return j
        return len(lines) - 1

    while i < len(lines):
        j = None
        if _FENCE.match(lines[i]):
            j = fence_end(i)
        elif _RULE.match(lines[i]):
            k = i + 1
            while k < len(lines) and not _RULE.match(lines[k]):
                k = fence_end(k) + 1 if _FENCE.match(lines[k]) else k + 1
            j = k if k < len(lines) else None
        elif lines[i].lstrip().startswith(">"):
            j = i
            while j + 1 < len(lines) and lines[j + 1].lstrip().startswith(">"):
                j += 1
        if j is None:
            plain += lines[i]
            i += 1
            continue
        block = "".join(lines[i:j + 1])
        if plain:
            out.append((plain, False))
        tail = block[len(block.rstrip("\r\n")):]
        out.append((block[:len(block) - len(tail)], True))
        plain, i = tail, j + 1
    if plain or not out:
        out.append((plain, False))
    return out


def quoted(sentence: str) -> str:
    """A block's sentence as a marker line after the block quotes it: without its quote mark."""
    return '"' + re.sub(r"^[\s>]+", "", sentence).strip() + '"'


def guard(p, text: str, tasks: list[dict], now: float | None = None, proofs=(), prove=None) -> tuple[str, list[str]]:
    """(text to post, notes for the coordinator). Unchanged unless a covered task has not started on its
    host (or was stopped with jobs still running) and a sentence claims an action is under way or done.
    A past report (past tense only, or a stated time at or before `now`) is marked only for a stop not yet
    confirmed. `proofs`: the reply's (claim text, expect probe) pairs; a sentence one of them backs is left
    as written when its probe passes (`prove`, default passes())."""
    now = time.time() if now is None else now
    if not text or not tasks:
        return text, []
    segs = [(re.split(r"((?<=[.!?;])[ \t]+|\n)", chunk), block) for chunk, block in segments(text)]
    if not any(_claim(parts[k]) for parts, _ in segs for k in range(0, len(parts), 2)):
        return text, []
    from . import eta
    try:
        cfg = p.config()
        est = eta._Est(p.db, now, core=cfg.get("core_provider"), delivery=eta.delivery_of(cfg))
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
            waiting.append((t, f"{who}: {got['line']}", got.get("kind")))
    if not waiting:
        return text, []
    from . import timefmt
    tz = timefmt.tzinfo(p)
    proved: dict[str, bool] = {}
    end = time.monotonic() + CLAIM_PROBE_S   # all of one message's probes: a slow one never holds the post long
    about: set[int] = set()   # indexes in `waiting` of the tasks a marked sentence is about
    marked, out = 0, []
    for parts, block in segs:
        after = []
        for k in range(0, len(parts), 2):
            kind = _claim(parts[k])
            if not kind:
                continue
            # A report of a past event names no plan: only a stop its live jobs contradict still counts. A
            # start claim is never one: only a started task or a passing expect probe clears it.
            past = kind == "past" or (kind != "start" and _said_before(parts[k], now, tz))
            pool = [j for j, w in enumerate(waiting) if w[2] == "stop"] if past else list(range(len(waiting)))
            if not pool:
                continue
            named = {int(x) for x in re.findall(r"#(\d{1,9})\b", parts[k])}
            # A sentence is about an unstarted task only when it names its #id, or (naming no #id) that
            # task's resource or host. Any other claim sentence is left as written: it states a fact about
            # other work, whatever its subject.
            if named:
                hit = {j for j in pool if waiting[j][0].get("id") in named}
            else:
                hit = {j for j in pool if _names(parts[k], waiting[j][0])}
            if not hit:
                continue
            # A reply claim whose expect probe passes now shows it done: posted as written.
            backing = [probe for said, probe in proofs or () if probe and _backs(parts[k], said)]
            for probe in backing:
                if probe not in proved:
                    left = end - time.monotonic()
                    try:
                        proved[probe] = left > 0 and bool((prove or (lambda q: passes(p, q, left)))(probe))
                    except Exception:
                        proved[probe] = False
            if any(proved[q] for q in backing):
                continue
            marked += 1
            about |= hit
            if block:   # text meant to be posted as written: the marker goes on its own line after it
                after.append(f"\n{MARK} {quoted(parts[k])}")
            else:
                parts[k] = f"{MARK} {parts[k].strip()}"
        out.append("".join(parts) + "".join(after))
    if not marked:
        return text, []
    # Only the tasks the message itself claimed: the other unstarted tasks it covers get no forecast line
    honest = [waiting[j][1] for j in sorted(about)]
    footer = ((cfg.get("coordinator") or {}).get("status_footer", True) is not False)
    out = "".join(out).rstrip() + ("\n" + STATUS_HEAD + "; ".join(honest) if footer else "")
    note = (f"onhost: a message said work was under way or done that has not started on its host "
            f"({'; '.join(honest)[:300]}); it was sent marked as planned"
            + (" with that status" if footer else " (coordinator.status_footer is off: no status line)")
            + ". Describe an action as happening only once STATE shows it on the host")
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
