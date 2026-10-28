# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Built-in, model-free watchers. Each reports only CHANGES, so an unchanged world costs nothing.

- `prs`: every pull request a task opened — draft/ready, CI result, new review activity, approval,
  merge. Emits events straight to the coordinator (they are already specific and actionable).
  It never puts a PR back in draft and never touches its reviewers: the user may share the
  harness's GitHub account, and their own actions there win. A PR that leaves draft without the
  user's recorded approval (prguard) is checked against the runs' gh log: if a run marked it ready
  or requested reviewers on it, a high alert names the run; otherwise the user did it on GitHub and
  that is recorded as their approval, with one line in the feed. Open PRs' findings
  (failing CI, bot review comments neither fixed nor answered) are recorded for prguard and are
  work: the coordinator hears when a PR has some (to queue a fix task) and when it is clean (to ask
  the user for a review). A bot's re-post of a finding it already made (after each push, say) is one
  finding, not new work, and an empty COMMENTED review that only wraps the runs' own or a bot's inline
  comments is no activity (a person's inline-only review, or one whose comments are unknown, counts; a failed read keeps the last known activity). A move of an open PR's CI alone raises no pr_changed: those reports cover it.
- `logfile`: tails files named in the payload and screens new lines (rules, then Jev if enabled).

Each run has a total deadline (`watchers.deadline_s`, a payload's `deadline_s` wins, up to DEADLINE_MAX_S)
and each gh call a timeout (`watchers.gh_timeout_s`, or the payload's), cut to the time left: a run past its
deadline stops, keeps what it read, and reports one error saying it was cut short. The `prs` watcher reads
`pr_workers` PRs at once, each PR within `pr_timeout_s`, so one slow PR times out alone while the others are
read. Its gh reads are batched (PrCycle): one GraphQL query per repo and poll (PR_BATCH PRs a query) answers
every PR's view and findings, and later asks in the same cycle reuse it, so gh runs do not grow with the PR
count; each gh runs in a process group of its own. It reads the least recently checked PRs first
(PR_TRIED_KEY), so a run cut short is continued by the next and every PR gets its turn; a merged or closed
PR is no longer read. Each run logs every gh call's duration per PR and which PRs it did not check, and why.
A PR is read once per run however many tasks name it (a hand-off's `pr` sets its task's pr_url, so fix,
review and follow-up tasks of one PR all carry it): one row per owner/repo#N, its oldest task's. With no
`deadline_s` in its payload, the prs run's deadline grows with the PR count (PR_READ_EST_S per PR per
worker) up to PR_DEADLINE_AUTO_MAX_S, so a host with many PRs still reads them all in one run.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import contextlib
import contextvars
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import prguard
from . import screen as scr

DAY = 86400.0
GH_LOG_MARGIN_S = 3600.0   # runs' gh calls this long before a PR was last seen in draft still count
GH_TIMEOUT_S = 20.0        # one gh call's default timeout
DEADLINE_S = 90.0          # one builtin run's default total bound, well below the daemon's tick watchdog
# A configured deadline is capped here. Builtin runs go off the daemon's tick (schedrun), which kills one
# past its deadline plus SCHEDULE_GRACE_S, so a schedule's own deadline_s is honoured up to half an hour.
DEADLINE_MAX_S = 1800.0
PR_WORKERS = 4             # PRs the prs watcher reads at once (`watchers.pr_workers`)
PR_WORKERS_MAX = 16
PR_TIMEOUT_S = 60.0        # one PR's reads together (view, then GraphQL; `watchers.pr_timeout_s`)
PR_READ_EST_S = 3.0        # one PR's reads (view, then GraphQL) at a usual pace: sizes the prs run's deadline
PR_DEADLINE_AUTO_MAX_S = 300.0   # the PR count raises the prs run's deadline up to this; a payload's deadline_s wins
PR_TRIED_KEY = "pr_watch_tried"   # kv: {url: number of the run that last checked it}; the least recent go first
DONE_STATES = ("MERGED", "CLOSED")   # a PR last seen so is no longer read

_now = time.monotonic
# (deadline on the _now clock, per-call gh timeout) of the builtin run in progress
_BOUND: contextvars.ContextVar[tuple[float, float] | None] = contextvars.ContextVar("watcher_bound", default=None)
# the schedule run (schedrun.Run) of the builtin run in progress: its timeout or a daemon stop kills gh's groups
_RUN: contextvars.ContextVar = contextvars.ContextVar("watcher_run", default=None)
# one PR's read (_read_pr): _gh appends the timeout of each of its calls that timed out
_TIMED_OUT: contextvars.ContextVar[list | None] = contextvars.ContextVar("watcher_timed_out", default=None)


def _seconds(*values, default: float) -> float:
    """The first of `values` that is a positive number of seconds, else `default`."""
    for v in values:
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if x > 0:
            return x
    return default


def bounds(daemon, payload: dict, kind: str | None = None) -> tuple[float, float]:
    """(deadline_s, gh_timeout_s) of one run: the payload's, else the config's `watchers`, else the
    defaults. The deadline is capped at DEADLINE_MAX_S and a gh call never outlasts it. A `prs` run
    (`kind`, else the payload's builtin) whose payload sets no deadline gets at least the time its PR
    count needs (_sized_deadline)."""
    cfg = (getattr(daemon, "cfg", None) or {}).get("watchers") or {}
    deadline = _seconds(payload.get("deadline_s"), cfg.get("deadline_s"), default=DEADLINE_S)
    if (kind or payload.get("builtin")) == "prs" and not _seconds(payload.get("deadline_s"), default=0):
        deadline = max(deadline, _sized_deadline(daemon, payload))
    deadline = min(deadline, DEADLINE_MAX_S)
    return deadline, min(_seconds(payload.get("gh_timeout_s"), cfg.get("gh_timeout_s"), default=GH_TIMEOUT_S),
                         deadline)


def pr_bounds(daemon, payload: dict) -> tuple[int, float]:
    """(workers, pr_timeout_s) of one prs run: the payload's, else the config's `watchers`, else the defaults."""
    cfg = (getattr(daemon, "cfg", None) or {}).get("watchers") or {}
    workers = _seconds(payload.get("pr_workers"), cfg.get("pr_workers"), default=PR_WORKERS)
    return (max(1, min(int(workers), PR_WORKERS_MAX)),
            _seconds(payload.get("pr_timeout_s"), cfg.get("pr_timeout_s"), default=PR_TIMEOUT_S))


def _pr_rows(db) -> list[dict]:
    """The tasks naming a PR, oldest first (cancelled ones are not watched)."""
    return db.q("SELECT id, title, pr_url, status FROM tasks WHERE pr_url IS NOT NULL AND pr_url!='' "
                "AND status NOT IN ('cancelled') ORDER BY id")


def _one_per_pr(rows: list[dict], seen: dict) -> list[dict]:
    """One row per PR (owner/repo#N, else its URL as written): the oldest task's, so a PR several tasks
    name is read, reported and listed once. A PR last seen merged or closed is left out."""
    out: dict[str, dict] = {}
    for t in rows:
        out.setdefault(prguard.pr_key(t["pr_url"]) or t["pr_url"], t)
    return [t for t in out.values() if (seen.get(t["pr_url"]) or {}).get("state") not in DONE_STATES]


def _sized_deadline(daemon, payload: dict) -> float:
    """The seconds the prs watcher's PRs take at PR_READ_EST_S each, pr_workers at once, capped at
    PR_DEADLINE_AUTO_MAX_S (0 when they cannot be counted)."""
    try:
        db = daemon.p.db
        n = len(_one_per_pr(_pr_rows(db), db.kv("pr_signatures", {}) or {}))
    except Exception:   # noqa: BLE001 - sizing only: the configured deadline stands
        return 0.0
    workers = pr_bounds(daemon, payload)[0]
    return min(-(-n // workers) * PR_READ_EST_S, PR_DEADLINE_AUTO_MAX_S)


def _left() -> float | None:
    """Seconds left before the running builtin's deadline (None: unbounded)."""
    b = _BOUND.get()
    return None if b is None else b[0] - _now()


def _over() -> bool:
    left = _left()
    return left is not None and left <= 0


def run_builtin(daemon, name: str, payload: dict) -> str:
    kind = payload.get("builtin") or name
    deadline, per_call = bounds(daemon, payload, kind)
    source = f"watcher:{name}"
    started = time.time()
    token = _BOUND.set((_now() + deadline, per_call))
    from . import schedrun
    run_token = _RUN.set(schedrun.current())
    try:
        if kind == "prs":
            status = watch_prs(daemon, payload)
        elif kind == "logfile":
            status = watch_logs(daemon, payload)
        else:
            return f"unknown builtin {kind}"
    finally:
        _RUN.reset(run_token)
        _BOUND.reset(token)
    if status.startswith("cut short"):   # one error per stint: its key holds while runs keep being cut
        daemon.observe(scr.error_source(source), f"builtin watcher {name} was cut short ({deadline:g} s "
                       f"deadline): {status}; the next run continues with what it did not check", "normal",
                       lifecycle=scr.ERROR, key=f"{source}:cut-short")
    else:   # a whole run: an earlier cut-short error is over
        scr.settle_receipts(daemon.p.db, source, started, ())
    return status


def _spawn(args: list[str], timeout: float, cwd: str, run=None):
    """gh `args` through boundedchild (its own process group, killed whole at `timeout`), known to the
    schedule run `run` while it lives (schedrun.track), so a daemon stop kills it too."""
    from . import boundedchild, schedrun
    pids: list[int] = []

    def started(pid: int) -> bool:   # stopped while it started: the helper kills it
        pids.append(pid)
        return run is None or schedrun.track(run, pid)

    try:
        return boundedchild.run(["gh", *args], timeout, cwd=cwd, on_start=started)
    finally:
        if run is not None and pids:
            run.groups.pop(pids[0], None)


def _timed(timeout: float) -> None:
    timed = _TIMED_OUT.get()
    if timed is not None:
        timed.append(timeout)


def _gh(args: list[str], cwd: str) -> dict | None:
    """gh's JSON answer, or None. Its timeout is the running builtin's per-call one, cut to the time
    left before its deadline; past the deadline or once its schedule run was stopped it is not called.
    A PR the poll cycle planned (PrCycle) is answered from its repo's one batched read. A call that
    timed out is told to _read_pr (_TIMED_OUT)."""
    left, b, run = _left(), _BOUND.get(), _RUN.get()
    timeout = GH_TIMEOUT_S if b is None else min(b[1], left)
    if timeout <= 0 or (run is not None and run.stopping.is_set()):
        return None
    cycle = _CYCLE.get()
    if cycle is not None:
        hit, answer = cycle.answer(args, cwd, timeout, run)
        if hit:
            return answer
    res = _spawn(args, timeout, cwd, run)
    if res.timed_out:
        _timed(timeout)
        return None
    if res.rc != 0:
        return None
    try:
        return json.loads(res.out)
    except ValueError:
        return None


# One batched read: a GraphQL query per repo and poll for up to PR_BATCH of its PRs, aliased p0, p1...,
# fetching what `gh pr view --json PR_FIELDS` and BOT_QUERY return for each. About 10k nodes a PR, so a
# batch stays far below GitHub's 500k-node cap per query. A connection with more pages than one (over 100
# comments, reviews or checks) is read on, so the answers are whole, as gh's own are.
PR_BATCH = 20
_PAGE = "pageInfo{hasNextPage endCursor} "
_AUTHOR = "author{__typename login} "
# connection: (selection, "%s" taking its `after`; its path from the pullRequest node)
_CONNS = {
    "comments": ("comments(first:100%s){" + _PAGE + "nodes{" + _AUTHOR + "body createdAt url}}", ("comments",)),
    "reviews": ("reviews(first:100%s){" + _PAGE + "nodes{id " + _AUTHOR + "state body submittedAt url "
                "comments(first:50){totalCount nodes{" + _AUTHOR + "body}}}}", ("reviews",)),
    "checks": ("commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100%s){" + _PAGE
               + "nodes{__typename ... on CheckRun{name status conclusion} ... on StatusContext{context state}}}}}}}",
               ("commits", "nodes", -1, "commit", "statusCheckRollup", "contexts")),
}
_PR_SEL = ("state isDraft mergeable reviewDecision url title headRefOid headRefName " + _AUTHOR
           + "".join(sel % "" for sel, _ in _CONNS.values())
           + "reviewThreads(first:100){nodes{isResolved isOutdated path comments(first:50){nodes{" + _AUTHOR
           + "body url}}}}")
# A failed batch whose per-PR asks would fail alike (rate limit, auth): its PRs fail with it, no gh per PR.
_REFUSED_RE = re.compile(r"rate limit|abuse detection|HTTP 40[13]\b|bad credentials|gh auth login", re.I)
_HOST_RE = re.compile(r"https?://([^/\s]+)/", re.I)


def _at(node, path):
    for step in path:
        if isinstance(step, int):
            node = node[step] if isinstance(node, list) and node else None
        else:
            node = node.get(step) if isinstance(node, dict) else None
    return node


def _graphql(res) -> dict | None:
    """The `data` of a GraphQL answer. gh exits 1 when the answer carries errors (one alias naming a PR
    that is gone, say), yet prints it: what did resolve is used."""
    try:
        body = json.loads(res.out) if res is not None and not res.timed_out else None
    except ValueError:
        return None
    return body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else None


def _views(node: dict, nodes: dict) -> tuple[dict, dict]:
    """(`gh pr view --json PR_FIELDS` answer, BOT_QUERY answer) of one PR's batched node, with each
    connection's whole `nodes`. gh gives a null review decision as "" and reviews without their inline
    comments; BOT_QUERY reads the last 100 comments and reviews."""
    reviews = nodes["reviews"]
    view = {"state": node.get("state"), "isDraft": node.get("isDraft"), "mergeable": node.get("mergeable") or "",
            "reviewDecision": node.get("reviewDecision") or "", "statusCheckRollup": nodes["checks"],
            "comments": nodes["comments"], "reviews": [{k: v for k, v in r.items() if k != "comments"} for r in reviews],
            "url": node.get("url"), "title": node.get("title"), "author": node.get("author"),
            "headRefOid": node.get("headRefOid"), "headRefName": node.get("headRefName")}
    data = {"data": {"repository": {"pullRequest": {
        "comments": {"nodes": nodes["comments"][-100:]}, "reviews": {"nodes": reviews[-100:]},
        "reviewThreads": node.get("reviewThreads") or {"nodes": []}}}}}
    return view, data


class PrCycle:
    """One poll cycle's PR reads. The PRs it plans are read one batched GraphQL query per repo (PR_BATCH
    a query) when the first of them is asked for; every later ask in the cycle, from any consumer, is
    answered from that read with no gh call. A read that fails or times out fails each of its PRs, once
    (a hung read cuts its whole batch): the next poll tries again, as with per-PR calls. Asks it cannot
    answer (a PR it did not plan, other fields, an answer without the PR's alias) go to gh as before."""

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self.where: dict[str, dict] = {}   # pr_key: its batch
        self.spawned = 0                   # gh processes the batches started

    def plan(self, urls) -> None:
        import threading
        groups: dict[tuple, list] = {}
        with self._lock:
            for url in urls:
                key, host = prguard.pr_key(url), _HOST_RE.match(url or "")
                if key and host and key not in self.where:
                    owner, rest = key.split("/", 1)
                    name, number = rest.split("#")
                    groups.setdefault((host.group(1).lower(), owner, name), []).append((key, int(number)))
            for (host, owner, name), prs in groups.items():
                for i in range(0, len(prs), PR_BATCH):
                    part = prs[i:i + PR_BATCH]
                    batch = {"host": host, "owner": owner, "name": name, "prs": part, "lock": threading.Lock(),
                             "done": False, "timed_out": False, "refused": False, "got": {}, "unknown": set()}
                    self.where.update({k: batch for k, _ in part})

    @staticmethod
    def _ask(args: list[str]) -> tuple[str | None, object]:
        """(pr_key, the fields asked for, or "data" for BOT_QUERY) of an ask the cycle can answer."""
        if len(args) == 5 and args[:2] == ["pr", "view"] and args[3] == "--json":
            fields = args[4].split(",")
            if set(fields) <= set(PR_FIELDS.split(",")):
                return prguard.pr_key(args[2]) if _HOST_RE.match(args[2]) else None, fields
        if args[:2] == ["api", "graphql"] and f"query={BOT_QUERY}" in args:
            vals = dict(a.split("=", 1) for a in args[2:] if "=" in a and not a.startswith("query="))
            if {"owner", "name", "number"} <= set(vals):
                return prguard.pr_key(f"{vals['owner']}/{vals['name']}#{vals['number']}"), "data"
        return None, None

    def answer(self, args: list[str], cwd: str, timeout: float, run=None) -> tuple[bool, dict | None]:
        """(True, the answer) for an ask about a planned PR, reading its batch first if need be; else (False, None)."""
        key, what = self._ask(args)
        batch = self.where.get(key) if key else None
        if batch is None:
            return False, None
        with batch["lock"]:
            if not batch["done"]:
                self._read(batch, cwd, timeout, run)
        if key in batch["unknown"]:
            return False, None
        if batch["timed_out"]:
            _timed(timeout)
        got = batch["got"].get(key)
        if got is None:
            return True, None
        view, data = json.loads(json.dumps(got))   # each consumer its own copy
        return True, data if what == "data" else {f: view.get(f) for f in what}

    def _call(self, batch: dict, query: str, more: list[str], cwd: str, end: float, run) -> dict | None:
        left = end - _now()
        if left <= 0 or (run is not None and run.stopping.is_set()):
            return None
        host = [] if batch["host"] == "github.com" else ["--hostname", batch["host"]]
        self.spawned += 1
        res = _spawn(["api", "graphql", *host, "-f", f"query={query}", "-f", f"owner={batch['owner']}",
                      "-f", f"name={batch['name']}", *more], left, cwd, run)
        batch["timed_out"] |= bool(res is not None and res.timed_out)
        batch["refused"] |= bool(res is not None and _REFUSED_RE.search(str(res.err or "")))
        return _graphql(res)

    def _read(self, batch: dict, cwd: str, timeout: float, run) -> None:
        batch["done"] = True
        end = _now() + timeout
        query = ("query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
                 + "".join(f"p{i}:pullRequest(number:{n}){{{_PR_SEL}}}" for i, (_, n) in enumerate(batch["prs"]))
                 + "}}")
        got = self._call(batch, query, [], cwd, end, run)
        if got is None and not (batch["timed_out"] or batch["refused"] or _over()
                                or run is not None and run.stopping.is_set()):
            batch["unknown"].update(k for k, _ in batch["prs"])   # gh failed otherwise: each PR asks itself
            return
        repo = (got or {}).get("repository")
        if not isinstance(repo, dict):
            return
        for i, (key, number) in enumerate(batch["prs"]):
            if f"p{i}" not in repo:   # an answer without the alias is not one to this query: ask gh per PR
                batch["unknown"].add(key)
                continue
            node = repo[f"p{i}"]
            if not isinstance(node, dict):
                continue   # gone, or not readable (null): gh pr view would fail too
            nodes = {}
            for conn, (sel, path) in _CONNS.items():
                page = _at(node, path) or {}
                nodes[conn] = list(page.get("nodes") or [])
                while (page.get("pageInfo") or {}).get("hasNextPage"):
                    q = ("query($owner:String!,$name:String!,$number:Int!,$after:String!){repository(owner:$owner,"
                         "name:$name){pullRequest(number:$number){" + sel % ",after:$after" + "}}}")
                    more = self._call(batch, q, ["-F", f"number={number}", "-f",
                                                 f"after={page['pageInfo'].get('endCursor')}"], cwd, end, run)
                    page = _at(more, ("repository", "pullRequest", *path))
                    if not isinstance(page, dict):
                        break
                    nodes[conn] += page.get("nodes") or []
                else:
                    continue
                break   # a page could not be read: this PR is unread, as its gh pr view would be
            else:
                batch["got"][key] = _views(node, nodes)


_CYCLE: contextvars.ContextVar[PrCycle | None] = contextvars.ContextVar("watcher_pr_cycle", default=None)


@contextlib.contextmanager
def pr_cycle():
    """The poll cycle's PR reads (PrCycle): the one in progress, else a new one for this block."""
    cycle = _CYCLE.get()
    if cycle is not None:
        yield cycle
        return
    cycle = PrCycle()
    token = _CYCLE.set(cycle)
    try:
        yield cycle
    finally:
        _CYCLE.reset(token)


TTP_MARKER = "<!-- ttp -->"


def _own(c: dict, pr: dict) -> bool:
    """A comment or review a tt-project run posted: it carries the hidden marker, whatever the author
    (the runs' gh account may not be the one that opened the PR). One the PR's own author wrote is
    the author answering, not someone waiting on an answer. Other unmarked comments count."""
    return TTP_MARKER in str(c.get("body") or "") or _by_author(c, pr)


def _by_author(c: dict, pr: dict) -> bool:
    login = str(((pr.get("author") or {}).get("login")) or "")
    return bool(login) and str((c.get("author") or {}).get("login") or "") == login


def _reply_wrapper(r: dict, pr: dict | None = None) -> bool:
    """An empty COMMENTED review GitHub adds around inline comments that are all the runs' own replies
    (marked) or a bot's: no activity. A review whose inline comments are unknown (gh pr view carries
    none; _with_review_comments adds them) or include a person's unmarked one is a review and counts."""
    if str(r.get("state") or "").upper() != "COMMENTED" or str(r.get("body") or "").strip():
        return False
    cs = r.get("comments")
    nodes = (cs.get("nodes") or []) if isinstance(cs, dict) else []
    if not nodes or int(cs.get("totalCount") or 0) > len(nodes):
        return False
    return all(_is_bot(c.get("author")) or TTP_MARKER in str(c.get("body") or "") or _by_author(c, pr or {})
               for c in nodes)


def _with_review_comments(pr: dict, data: dict | None) -> dict:
    """The PR with each review's inline comments from the GraphQL answer (BOT_QUERY), matched by id."""
    nodes = ((((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}) \
        .get("reviews") or {}
    by_id = {n["id"]: n.get("comments") for n in nodes.get("nodes") or [] if n.get("id")}
    if not by_id:
        return pr
    return {**pr, "reviews": [{**r, "comments": by_id[r["id"]]} if r.get("id") in by_id else r
                              for r in pr.get("reviews") or []]}


def pr_signature(pr: dict) -> dict:
    checks = pr.get("statusCheckRollup") or []
    states = sorted({(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() for c in checks})
    failing = sorted(c.get("name") or c.get("context") or "?" for c in checks
                     if (c.get("conclusion") or c.get("state") or "").upper() in ("FAILURE", "ERROR", "TIMED_OUT",
                                                                                 "CANCELLED", "ACTION_REQUIRED"))
    pending = any((c.get("status") or c.get("state") or "").upper() in ("IN_PROGRESS", "QUEUED", "PENDING")
                  for c in checks)
    reviews = [r for r in pr.get("reviews") or [] if not _reply_wrapper(r, pr)]
    human = [c for c in (pr.get("comments") or []) + reviews
             if not ((c.get("author") or {}).get("login", "").endswith("[bot]")) and not _own(c, pr)]
    activity = hashlib.sha1(json.dumps([(c.get("author") or {}).get("login", "") + str(c.get("body", ""))[:200]
                                        + str(c.get("state", "")) for c in human]).encode()).hexdigest()[:12]
    return {"state": pr.get("state"), "draft": pr.get("isDraft"), "decision": pr.get("reviewDecision"),
            "mergeable": pr.get("mergeable"), "checks": "pending" if pending else ("failing" if failing else
                                                                                  ("passing" if states else "none")),
            "failing": failing[:10], "activity": activity, "n_human": len(human)}


PR_FIELDS = "state,isDraft,mergeable,reviewDecision,statusCheckRollup,comments,reviews,url,title,author,headRefOid,headRefName"


def _read_pr(t: dict, root: str, end: float, run_end: float | None) -> dict:
    """One PR's gh reads on a pool thread: its view, then an open PR's GraphQL, all before `end` on the
    _now clock (its own timeout or the run's deadline, whichever comes first). It only calls gh.
    `cut` says why a read was not made or did not answer in time: "deadline", "timeout" (the PR's) or
    "call" (one gh call's own timeout, `gh_timeout_s`)."""
    b = _BOUND.get()
    _BOUND.set((end, b[1] if b else GH_TIMEOUT_S))
    timed: list = []
    _TIMED_OUT.set(timed)
    out: dict = {"pr": None, "data": None, "calls": [], "cut": None}

    def gh(what: str, args: list[str]) -> dict | None:
        if not _over():
            t0, n = _now(), len(timed)
            r = _gh(args, root)
            late = len(timed) > n
            out["calls"].append(f"{what} {_now() - t0:.1f} s" + ("" if r is not None else
                                                                  " (timed out)" if late else " (no answer)"))
            if r is not None or not _over() and not late:
                return r
            if not _over():
                out["cut"] = "call"
                return None
        out["cut"] = "deadline" if run_end is not None and end >= run_end else "timeout"
        return None

    out["pr"] = gh("view", ["pr", "view", t["pr_url"], "--json", PR_FIELDS])
    key = prguard.pr_key((out["pr"] or {}).get("url") or t["pr_url"])
    if key and out["pr"] is not None and out["pr"].get("state") == "OPEN":   # a closed PR's change is reported
        owner, rest = key.split("/", 1)
        name, number = rest.split("#")
        out["data"] = gh("graphql", ["api", "graphql", "-f", f"query={BOT_QUERY}", "-f", f"owner={owner}",
                                     "-f", f"name={name}", "-F", f"number={number}"])
    return out


def _read_prs(order: list[dict], root: str, workers: int, pr_timeout: float) -> dict[str, dict]:
    """Read the PRs of `order` (in that order), `workers` at once, each within `pr_timeout` and the
    run's deadline; none is started past the deadline. Returns {url: _read_pr's answer} of those
    started. It returns only once every read it started ended, so it leaves no gh process behind; none
    is started once its schedule run was stopped."""
    b, run = _BOUND.get(), _RUN.get()
    run_end = b[0] if b else None
    results: dict[str, dict] = {}
    todo, live = list(order), {}
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pr-watch")
    try:
        while todo or live:
            while todo and len(live) < workers and not _over() and not (run and run.stopping.is_set()):
                t = todo.pop(0)
                end = _now() + pr_timeout if run_end is None else min(run_end, _now() + pr_timeout)
                live[pool.submit(contextvars.copy_context().run, _read_pr, t, root, end, run_end)] = t
            if not live:
                break
            done, _ = wait(live, return_when=FIRST_COMPLETED)
            for f in done:
                results[live.pop(f)["pr_url"]] = f.result()
    finally:
        pool.shutdown(wait=True)
    return results


def _log(daemon, msg: str) -> None:
    try:
        from .daemon import log
        log(daemon.p, msg)
    except (OSError, AttributeError):
        pass


def watch_prs(daemon, payload: dict | None = None) -> str:
    db, root = daemon.p.db, str(daemon.p.root)
    rows = _pr_rows(db)
    seen = db.kv("pr_signatures", {})
    workers, pr_timeout = pr_bounds(daemon, payload or {})
    per_call = (_BOUND.get() or (0, GH_TIMEOUT_S))[1]
    # PRs already out of draft when this check first ran predate the guard and are not flagged.
    before = db.kv(prguard.PREDATES_KEY)
    if before is None:
        before = [u for u, s in seen.items() if s.get("state") == "OPEN" and s.get("draft") is False]
    before = set(before)
    flagged = dict(db.kv(prguard.UNAPPROVED_KEY, {}) or {})   # a PR gh could not read keeps its flag
    findings = dict(db.kv(prguard.FINDINGS_KEY, {}) or {})
    heads = dict(db.kv(prguard.HEADS_KEY, {}) or {})
    drafts = dict(db.kv(prguard.DRAFT_SEEN_KEY, {}) or {})
    changed = 0
    # Each PR once, however many tasks name it; merged and closed PRs are no longer read. The least
    # recently checked go first, so a run cut short is continued by the next and a PR that keeps timing
    # out waits behind the others.
    watch = _one_per_pr(rows, seen)
    tried = {u: n for u, n in (db.kv(PR_TRIED_KEY, {}) or {}).items() if u in {t["pr_url"] for t in watch}}
    stint = max(tried.values(), default=0) + 1
    order = sorted(watch, key=lambda t: tried.get(t["pr_url"], 0))
    t0 = _now()
    with pr_cycle() as cycle:   # one batched read per repo; later asks in the cycle reuse it
        spawned = cycle.spawned
        cycle.plan(t["pr_url"] for t in order)
        results = _read_prs(order, root, workers, pr_timeout)
        spawned = cycle.spawned - spawned
    from . import schedrun
    schedrun.check_stopped()   # a daemon stop (or the timeout's kill) killed its reads: no result to save
    read, missed, timings = 0, [], []
    for t in order:
        r = results.get(t["pr_url"])
        name = prguard.pr_key(t["pr_url"]) or t["pr_url"]
        if r is None:
            missed.append(f"{name} (deadline: not started)")
            continue
        timings.append(f"{name} {', '.join(r['calls']) or 'no call'}")
        if r["cut"]:
            why = {"deadline": "deadline", "call": f"{per_call:g} s gh call timeout"}.get(
                r["cut"], f"{pr_timeout:g} s per-PR timeout")
            missed.append(f"{name} ({why}" + (": findings not read)" if r["pr"] is not None else ")"))
        pr, data = r["pr"], r["data"]
        if pr is None and r["cut"] == "deadline":
            continue   # cut off mid-read by the deadline: it goes first next time
        tried[t["pr_url"]] = stint   # a PR that timed out on its own waits behind the others
        if pr is None:
            read += not r["cut"]   # gh answered with an error: read, nothing to report
            continue
        read += 1
        key = prguard.pr_key(pr.get("url") or t["pr_url"])
        if key and pr.get("headRefOid") and (heads.get(key) or {}).get("sha") != pr["headRefOid"]:
            heads[key] = {"sha": pr["headRefOid"], "seen": time.time()}   # what an approval binds to
        # One GraphQL read serves the findings and which empty reviews only wrap the runs' replies.
        wrappers = any(str(r.get("state") or "").upper() == "COMMENTED" and not str(r.get("body") or "").strip()
                       for r in pr.get("reviews") or [])
        pr = _with_review_comments(pr, data)
        sig = pr_signature(pr)
        _check_unapproved(daemon, t, pr, sig, before, flagged, drafts)
        _check_findings(daemon, t, pr, sig, findings, data)
        old = seen.get(t["pr_url"])
        if old and sig["mergeable"] == "UNKNOWN" and old.get("mergeable") not in (None, "UNKNOWN"):
            sig = {**sig, "mergeable": old["mergeable"]}   # GitHub is recomputing: keep the last known value
        if old and wrappers and data is None:
            # The inline comments could not be read: keep the last known activity, so a failed read and
            # the next good one raise no pr_changed. A first sighting counts the unknown wrappers.
            sig = {**sig, "activity": old.get("activity"), "n_human": old.get("n_human")}
        if sig == old:
            continue
        seen[t["pr_url"]] = sig
        if old is not None and {k for k, v in sig.items() if old.get(k) != v} == {"mergeable"} \
                and "UNKNOWN" in (old.get("mergeable"), sig["mergeable"]):
            continue   # only mergeable moved to or from UNKNOWN: not news
        changed += 1
        if old is None:
            continue   # first sighting records a baseline; nothing new has happened yet
        if sig["state"] == "OPEN" and {k for k, v in sig.items() if old.get(k) != v} <= {"checks", "failing"}:
            continue   # only CI moved on an open PR: _check_findings reports each outcome once
        what =[f"{k}: {old.get(k)} → {v}" for k, v in sig.items() if old.get(k) != v and k != "activity"]
        if sig["activity"] != old.get("activity"):
            what.append(f"new review/comment activity ({sig['n_human']} human items)")
        sev = "high" if sig["state"] == "MERGED" or sig["decision"] == "CHANGES_REQUESTED" else "normal"
        text = f"PR for task #{t['id']} ({pr.get('url')}): " + "; ".join(what)
        # An active 'pr' mute covering it: recorded and counted there, but it does not wake the coordinator.
        status = "muted" if scr.count_muted(db, "pr", text, sev) else "queued"
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (time.time(), "pr", "pr_changed", sev, text, status, t["id"]))
    # A flag whose task is gone (cancelled, say) or whose PR gh has not read for a day is dropped,
    # so its alert clears.
    tasks, now = {t["id"] for t in rows}, time.time()
    flagged = {k: v for k, v in flagged.items()
               if v.get("task") in tasks and now - float(v.get("seen") or v.get("since") or 0) < DAY}
    db.set_kv("pr_signatures", seen)
    db.set_kv(prguard.PREDATES_KEY, sorted(before))
    db.set_kv(prguard.UNAPPROVED_KEY, flagged)
    db.set_kv(prguard.FINDINGS_KEY, findings)
    db.set_kv(prguard.HEADS_KEY, heads)
    keys = {prguard.pr_key(t["pr_url"]) for t in rows}
    db.set_kv(prguard.DRAFT_SEEN_KEY, {k: v for k, v in drafts.items() if k in keys})
    db.set_kv(PR_TRIED_KEY, tried)
    _log(daemon, f"pr-watch: {len(order)} PRs in {_now() - t0:.1f} s ({workers} at once, {spawned} batched "
         f"gh reads); gh calls: "
         + ("; ".join(timings) or "none") + (f"; not checked: {'; '.join(missed)}" if missed else ""))
    unapproved = f", {len(flagged)} out of draft unapproved" if flagged else ""
    if missed:
        shown = "; ".join(missed[:10]) + (f"; {len(missed) - 10} more" if len(missed) > 10 else "")
        return (f"cut short ({read} of {len(order)} PRs read, {changed} changed{unapproved}; "
                f"{len(missed)} not checked: {shown})")
    return f"ok ({len(order)} PRs, {changed} changed{unapproved})"


def _check_unapproved(daemon, t: dict, pr: dict, sig: dict, before: set, flagged: dict, drafts: dict) -> None:
    """Handle a PR that is out of draft without the user's recorded approval. Nothing here changes
    the PR. If the runs' gh log (prguard.GH_LOG) shows a run marking it ready or requesting reviewers
    on it since it was last seen in draft, a restriction is at risk: raise a high alert naming the run
    and, the first time, queue an event for the coordinator. The flag (and the alert) clears once the
    PR is back in draft, closed, merged or approved. Otherwise the user took it out of draft on GitHub:
    record that as their approval and log one feed line."""
    db, url = daemon.p.db, t["pr_url"]
    key = prguard.pr_key(pr.get("url") or url)
    if not key:
        return
    now = time.time()
    if sig["state"] != "OPEN" or sig["draft"] is not False:
        before.discard(url)
        flagged.pop(key, None)
        if sig["draft"]:
            drafts[key] = now
            prguard.drop_spent(db, key)   # back in draft: leaving it again needs a fresh yes
        return
    if url in before or prguard.approved(db, key):
        flagged.pop(key, None)
        return
    shown = pr.get("url") or url
    if "run" not in (flagged.get(key) or {}):   # a flag from before the gh log is judged again
        since = float(drafts.get(key) or now - DAY) - GH_LOG_MARGIN_S
        calls = prguard.worker_calls(daemon.p.state, key, since)
        if not calls:
            flagged.pop(key, None)
            prguard.approve_from_github(db, key, pr.get("headRefOid"), now)
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (now, "pr", "pr_ready_by_user", "low",
                  f"PR for task #{t['id']} ({shown}) left draft on GitHub, not through any run's gh: recorded as "
                  f"the user's own approval. Nothing was changed on the PR.", "handled", t["id"]))
            return
        last = ([c for c in calls if key in (c.get("prs") or [])] or calls)[-1]   # one that names it first
        did = "marked it ready" if last.get("action") == "ready" else "requested reviewers on it"
        how = "the harness's gh refused it" if last.get("refused") else f"gh exited {last.get('rc')}"
        run = f"run {last.get('run')}" + (f" of task #{last['task']}" if last.get("task") else "")
        flagged[key] = {"task": t["id"], "url": shown, "run": last.get("run"), "since": now, "seen": now,
                        "why": f"{run} {did} ({how}): {str(last.get('cmd') or '')[:160]}"}
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "pr", "pr_unapproved_ready", "high",
              f"PR for task #{t['id']} ({shown}) is out of draft without the user's recorded approval, and "
              f"{flagged[key]['why']}. A restriction is at risk: find out how that run got around the guard. "
              f"pr-watch did not put the PR back in draft and never will; leave its draft state and reviewers "
              f"to the user.", "queued", t["id"]))
    flagged[key]["seen"] = now
    daemon.alert(f"{prguard.UNAPPROVED_ALERT}:{key}",
                 f"{shown} (task #{t['id']}) left draft without your recorded approval, and "
                 f"{flagged[key]['why']}. Nothing on the PR was changed; whether it stays ready is your call.",
                 "high", every_s=DAY)


BOT_QUERY = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
             "pullRequest(number:$number){"
             "comments(last:100){nodes{author{__typename login} body createdAt url}}"
             "reviews(last:100){nodes{id author{__typename login} state body submittedAt url "
             "comments(first:50){totalCount nodes{author{__typename login} body}}}}"
             "reviewThreads(first:100){nodes{isResolved isOutdated path "
             "comments(first:50){nodes{author{__typename login} body url}}}}}}}")


def _is_bot(author: dict | None) -> bool:
    a = author or {}
    return a.get("__typename") == "Bot" or str(a.get("login", "")).endswith("[bot]")


_URL_RE = re.compile(r"https?://\S+")
_SHA_RE = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_HIDDEN_RE = re.compile(r"<!--.*?-->", re.S)
_RULE_RE = re.compile(r"\brule[ _-]?(?:id|name)?\s*[:=#]\s*[`'\"]?([\w./-]+)", re.I)


def finding_key(c: dict, path: str | None = None) -> str:
    """What makes two bot findings the same one: the file and the comment's text, with links, commit
    ids and hidden markup (which bots vary per push) left out, plus a rule id the text names. A
    comment without a body is only itself."""
    body = str(c.get("body") or "")
    if not body.strip():
        return "url:" + str(c.get("url") or id(c))
    m = _RULE_RE.search(body)
    text = _SHA_RE.sub(" ", _URL_RE.sub(" ", _HIDDEN_RE.sub(" ", body)).lower())
    text = " ".join(re.sub(r"\W+", " ", text).split())
    return hashlib.sha1(json.dumps([path or c.get("path") or "", m.group(1).lower() if m else "", text])
                        .encode()).hexdigest()[:12]


def bot_findings(data: dict) -> list[tuple[str, str]]:
    """(finding_key, URL) of bot review findings on a PR that are neither fixed nor answered: a review
    thread a bot started that is unresolved, not outdated and has no reply from a person; a bot's
    top-level comment, or review asking for changes, with no comment from a person after it. A bot
    re-posting a finding (same key) is one finding, listed once; once any post of it was answered
    (or its thread resolved), its re-posts are not open work."""
    pr = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    items = []   # (key, url, answered, open)
    for th in (pr.get("reviewThreads") or {}).get("nodes") or []:
        cs = (th.get("comments") or {}).get("nodes") or []
        if cs and _is_bot(cs[0].get("author")):
            answered = bool(th.get("isResolved")) or not all(_is_bot(c.get("author")) for c in cs[1:])
            items.append((finding_key(cs[0], th.get("path")), cs[0].get("url") or "?", answered,
                          not answered and not th.get("isOutdated")))
    comments = (pr.get("comments") or {}).get("nodes") or []
    human_times = sorted(c.get("createdAt") or "" for c in comments if not _is_bot(c.get("author")))
    last_human = human_times[-1] if human_times else ""
    tops = [(c.get("createdAt") or "", c) for c in comments if _is_bot(c.get("author"))]
    tops += [(r.get("submittedAt") or "", r) for r in (pr.get("reviews") or {}).get("nodes") or []
             if _is_bot(r.get("author")) and r.get("state") == "CHANGES_REQUESTED"]
    items += [(finding_key(c), c.get("url") or "?", ts <= last_human, ts > last_human) for ts, c in tops]
    answered = {k for k, _, a, _ in items if a}
    out, seen = [], set()
    for k, u, _, live in items:
        if live and k not in answered and k not in seen:
            seen.add(k)
            out.append((k, u))
    return out


def bot_open(data: dict) -> list[str]:
    """URLs of the PR's open bot findings (bot_findings)."""
    return [u for _, u in bot_findings(data)]


def _check_findings(daemon, t: dict, pr: dict, sig: dict, findings: dict, data: dict | None) -> None:
    """Record an open PR's findings and tell the coordinator when they change: CI failing or bot
    review comments open is work (a fix task); CI green with every bot comment fixed or answered is
    when it may ask the user for a review. `data` is the PR's BOT_QUERY answer (None: unread)."""
    db, url = daemon.p.db, pr.get("url") or t["pr_url"]
    key = prguard.pr_key(url)
    if not key:
        return
    if sig["state"] != "OPEN":
        findings.pop(key, None)
        return
    old = findings.get(key) or {}
    if data is not None:
        found = bot_findings(data)
        bots, bot_keys = [u for _, u in found], sorted(k for k, _ in found)
    else:   # unread: keep what was known
        bots, bot_keys = old.get("bot", []), old.get("bot_keys", sorted(old.get("bot", [])))
    rec = {"task": t["id"], "url": url, "branch": pr.get("headRefName") or old.get("branch"),
           "failing": sig["failing"], "pending": sig["checks"] == "pending",
           "bot_open": len(bots), "bot": bots[:10], "bot_keys": bot_keys,
           "notified": old.get("notified"), "owner": old.get("owner")}
    findings[key] = rec
    if rec["pending"]:
        return
    dirty = bool(rec["failing"] or rec["bot_open"])
    what = "; ".join([*(["CI failing: " + ", ".join(rec["failing"])] if rec["failing"] else []),
                      *([f"{len(bots)} bot review comment(s) neither fixed nor answered: "
                         + ", ".join(bots[:5])] if bots else [])])
    # Open findings always have one owner: an open task on the PR, else a fix task the daemon queues
    # once the task that delivered it is done (Daemon.pr_findings_owner). pr-watch only reports them.
    owner, made = daemon.pr_findings_owner(key, rec, what) if dirty and hasattr(daemon, "pr_findings_owner") \
        else (None, False)
    lost = dirty and not owner and rec["owner"] is not None
    rec["owner"] = owner["id"] if owner else None
    # Keyed by finding, not URL: a bot re-posting the same open finding under a new URL is no news.
    mark = json.dumps([rec["failing"], bot_keys]) if dirty else "clean"
    if dirty and "bot_keys" not in old and rec["notified"] == json.dumps([rec["failing"], sorted(bots)]):
        rec["notified"] = mark   # a record from before keys were kept, already told these findings
    if not made and not lost and (mark == rec["notified"] or (not dirty and rec["notified"] is None)):
        rec["notified"] = mark   # a PR clean when first seen needs no news: its task's hand-off said so
        return
    rec["notified"] = mark
    status = "queued"
    if made:
        text = (f"PR for task #{t['id']} ({url}) has open findings: {what}. No open task owned them, so the daemon "
                f"queued code task #{owner['id']} on the PR's branch to fix or answer each. Queue no other task "
                f"for them. Do not ask the user to review it until pr-watch reports it clean.")
        status = "handled"
    elif owner:
        text = (f"PR for task #{t['id']} ({url}) has open findings: {what}. Open task #{owner['id']} "
                f"({owner['status']}) owns them"
                + (" (a review of its change; the daemon queues their fix once its review chain ends)"
                   if owner["kind"] == "review" else "")
                + ": queue no other task for them. Do not ask the user to review it until pr-watch reports it clean.")
        status = "queued" if owner["status"] == "blocked" else "handled"
    elif dirty:
        text = (f"PR for task #{t['id']} ({url}) has open findings: {what}. This is work"
                + (" and no open task owns them now" if lost else "")
                + ": queue a code task (on the PR's branch) to fix or answer each and get CI green. Do not ask "
                  "the user to review it until pr-watch reports it clean.")
    else:
        text = (f"PR for task #{t['id']} ({url}) is clean: CI green and every bot review comment fixed or "
                f"answered. If its review task passed, ask the user for a draft review now (ask_user, "
                f"blocking review, with its URL).")
    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
         (time.time(), "pr", "pr_findings" if dirty else "pr_clean", "normal", text, status, t["id"]))

def watch_logs(daemon, payload: dict) -> str:
    db = daemon.p.db
    offsets = db.kv("log_offsets", {})
    n, cut = 0, False
    for pattern in payload.get("files", []):
        for path in sorted(Path(daemon.p.root).glob(pattern)) if not os.path.isabs(pattern) else [Path(pattern)]:
            if _over():   # a file not reached keeps its offset: the next run reads it
                cut = True
                break
            if not path.is_file():
                continue
            key = str(path)
            size = path.stat().st_size
            start = offsets.get(key, size)       # first sighting starts at the end: history is not news
            if size < start:
                start = 0                       # rotated or truncated
            if size > start:
                with open(path, "rb") as f:
                    f.seek(start)
                    chunk = f.read(min(size - start, 256 * 1024)).decode(errors="replace")
                offsets[key] = start + len(chunk.encode())
                lines = [ln for ln in chunk.splitlines() if ln.strip()]
                interesting = [ln for ln in lines if daemon_rule(ln) != "info"]
                for ln in interesting[:20]:
                    daemon.observe(f"log:{path.name}", ln[:2000])
                    n += 1
            else:
                offsets[key] = size
    db.set_kv("log_offsets", offsets)
    return f"cut short ({n} lines screened)" if cut else f"ok ({n} lines screened)"


def daemon_rule(line: str) -> str:
    from .screen import rule_severity
    return rule_severity(line)
