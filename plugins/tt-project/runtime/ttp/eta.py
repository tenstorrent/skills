# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Expected finish from the critical path (`ttp eta <task|o<id>>`), model-free.

estimate() gives an open task's expected finish as its remaining critical path: its unfinished
dependency chain (the latest-finishing dependency at each step), then its own legs, each the median
of this project's measured history (history(): ended runs, review tasks and push-queue rows of the
last WINDOW_S days), never a worst case or padding:
- queue: a queued task waits the median first-start wait of tasks with the same `resource:` or
  `exclusive:` label (the lock it needs), else of all tasks; a waiting task starts at its next try.
- run: the median run time of its kind and tier (else of its kind). A running task has the median of
  the longer runs left (those past its elapsed time); past every measured run it is `overrunning`
  and due now.
- review and landing: a code task then waits the median review (hand-off to review done) and the
  median push-queue row from approval to pushed or landed, each only when the project did one in the
  window (else the leg is not part of its path). A task in review or with an approved row has only
  what is left of those; a done one whose open review task (db.reviews_task) has not approved it yet
  lands at that review task's own estimate plus the landing leg. An approved row still waiting for a
  batch instead counts the approvals ahead of it in the queue's order (pushq._approved, aging
  included): it lands once the running batch ends and then (ahead // push_batch_max + 1) more batches
  of the median batch time ran (history(): push_batches started to finalized); with no measured
  batch it falls back to the median landing.
- landing wait: a queued or waiting task whose start_when waits only on other tasks' landings (exactly
  `landed:#N`, or `ttp landed --task N` terms joined by `&&`, optionally ending `|| exit 1`) starts no
  earlier than the latest of those tasks' own expected landing (their finish plus their review and
  landing legs); one already landed (landed.recorded) adds nothing. A waiting hand-off's retry_when made
  only of landing waits counts the same, never as its next probe time. A gated task with no live owner
  (done but not landed, with no open review, fix, rebase or push row carrying it on: deadwait.path; or
  failed, cancelled or abandoned), or one whose own estimate has none, gives no clock time: the estimate
  carries `after` ('landed:#N') and field() reads 'ETA: after landed:#N (why)'. Any other probe is not
  judged, except a merge gate (merge_gate: `gh pr view <n>` or `gh api .../pulls/<n>` reading the
  PR's state or merge), which reads 'after PR #<n> merges or closes' unless the hand-off measured an eta.
- outside work: a waiting task whose retry_when watches work outside the harness (outside_wait: any
  probe but a bare clock gate or a landing wait, such as a Slurm job, a detached run or a peer's ack) has
  no measured finish yet, unless its hand-off gives `eta` (epoch, or ISO time with offset, still ahead)
  with `eta_basis` (the measurement behind it), or a `ttp progress report --eta --eta-basis` of the task
  since it began waiting does (the newest valid one wins): then it finishes at that eta, plus the median
  run only when no on_pass_cmd ends it. Its next probe time is never taken for its finish. A wait the
  daemon already woke (`woke` in its hand-off) is due and keeps the usual queue and run legs.
- hold: a queued or waiting task whose provider's spend gate (kv `gates`) allows no new work starts no
  earlier than the gate's known reset (budget.hold_clears: a cap's period end, a plan window's reset,
  the runaway guard's hour, a provider limit's end); with no known reset it has no estimate and says it
  is held by the spend cap. A coordinator turn guard's red saved by an older runtime holds routine work only (budget.for_task).
A blocked, failed or cancelled task on the path, or a leg with no measured sample, gives no
estimate ('no estimate yet', with why): never a guess.

An open promise or obligation is done when the last of its owner tasks lands (obligation_estimate).
The coordinator's STATE (urgent tasks and promises), `ttp status` and the web app's task view show
it as one short field; workers read `ttp eta <task>` before setting a retry or deadline wake. Each
of them reads the project's delivery config (delivery_of: push_batch_max, push_age_s). In the STATE,
urgent tasks with no estimate for the same gate and reason share one line that names the gate.

long_promises() is the self-watch: an open promise or milestone to the user on an urgent goal (an
owner is urgent, urgent.is_urgent) due more than coordinator.course_check.long_promise_h (LONG_H)
after it was made. Each gives one Course check line until it is met, shortened (its due moved, or a
later promise to the same chat with an earlier due) or justified on record (the coordinator's
`obligation` action with `key` and `justify`, kept for that due). It never pages the user."""
from __future__ import annotations

import json
import re
import statistics
import time
from pathlib import Path

WINDOW_S = 14 * 86400
LONG_H = 2.0
ROWS = 5000
CLOSED = ("done", "failed", "cancelled")
PUSH_DONE = ("pushed", "landed")
LANDED_ALL = re.compile(r"\s*ttp\s+landed\s+--task\s+#?\d+(?:\s*&&\s*ttp\s+landed\s+--task\s+#?\d+)*"
                        r"(?:\s*\|\|\s*exit\s+1)?\s*")
MERGE_GATE = re.compile(r"\bgh\s+pr\s+view\s+#?(\d+)\b|\bgh\s+api\s+\S*/pulls/(\d+)\b")
MERGE_WORDS = re.compile(r"state|merged|closed", re.I)
PUSH_FINAL = ("pushed", "landed", "conflict", "check_failed", "refused", "scope_held", "nothing", "dropped")


def _med(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _labels(t: dict) -> list:
    try:
        v = json.loads(t.get("labels") or "[]")
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    except ValueError:
        return []


def _locks(t: dict) -> list[str]:
    return sorted(lb for lb in _labels(t) if lb.startswith(("resource:", "exclusive:")))


def gate_probe(t: dict) -> str:
    """The probe a task waits on: its start_when, else the retry_when of its waiting hand-off (not one
    the daemon already woke); '' for none."""
    from .db import deferral, load_result, waiting_handoff
    if when := deferral(t).get("when"):
        return when
    res = load_result(t.get("result")) if t.get("status") == "waiting" else waiting_handoff(t)
    return str(res.get("retry_when") or "") if res.get("status") == "waiting" and not res.get("woke") else ""


def merge_gate(probe: str) -> int | None:
    """The PR number a merge gate probe waits on (`gh pr view <n>` or `gh api .../pulls/<n>`, reading its
    state or merge), None for any other probe."""
    m = MERGE_GATE.search(probe or "")
    return int(m.group(1) or m.group(2)) if m and MERGE_WORDS.search(probe) else None


def landing_waits(t: dict) -> list[int]:
    """The task ids a start_when (or a waiting hand-off's retry_when, gate_probe) made only of landing
    waits names: `landed:#N`, or `ttp landed --task N` terms joined by `&&` (optionally ending `|| exit 1`);
    [] for any other probe or none."""
    from .landed import LANDED_PROBE
    when = gate_probe(t)
    if m := LANDED_PROBE.fullmatch(when):
        return [int(m.group(1))]
    if LANDED_ALL.fullmatch(when):
        return list(dict.fromkeys(int(x) for x in re.findall(r"--task\s+#?(\d+)", when)))
    return []


def history(db, now: float | None = None) -> dict:
    """The measured samples of the window: run times by (kind, tier) and kind, first-start waits by
    lock label and overall, review and landing times."""
    now = time.time() if now is None else now
    t0 = now - WINDOW_S
    h = {"run": {}, "kind": {}, "queue": {}, "wait": [], "review": [], "land": [], "batch": []}
    for r in db.q("SELECT r.started s, r.ended e, t.kind k, t.tier tr FROM runs r JOIN tasks t ON t.id=r.task "
                  "WHERE r.role!='coordinator' AND r.ended>? AND r.started IS NOT NULL AND r.ended>r.started "
                  "ORDER BY r.id DESC LIMIT ?", (t0, ROWS)):
        d = float(r["e"]) - float(r["s"])
        h["run"].setdefault((r["k"], r["tr"]), []).append(d)
        h["kind"].setdefault(r["k"], []).append(d)
    for r in db.q("SELECT t.created c, t.not_before nb, t.labels labels, t.depends_on deps, MIN(r.started) s "
                  "FROM tasks t JOIN runs r ON r.task=t.id WHERE t.created>? AND r.role!='coordinator' "
                  "GROUP BY t.id ORDER BY t.id DESC LIMIT ?", (t0, ROWS)):
        if (r["deps"] or "[]") not in ("[]", ""):
            continue   # its wait includes its dependencies' work, counted on their own legs
        w = max(0.0, float(r["s"]) - max(float(r["c"]), float(r["nb"] or 0)))
        h["wait"].append(w)
        for lk in _locks(r):
            h["queue"].setdefault(lk, []).append(w)
    for r in db.q("SELECT t.created c, MAX(r.ended) e FROM tasks t JOIN runs r ON r.task=t.id WHERE t.kind='review' "
                  "AND t.status='done' AND t.created>? AND r.ended IS NOT NULL GROUP BY t.id LIMIT ?", (t0, ROWS)):
        h["review"].append(max(0.0, float(r["e"]) - float(r["c"])))
    try:
        for r in db.q("SELECT created c, updated u FROM push_queue WHERE status IN ('pushed','landed') AND created>? "
                      "LIMIT ?", (t0, ROWS)):
            h["land"].append(max(0.0, float(r["u"]) - float(r["c"])))
        for r in db.q("SELECT started s, COALESCE(after_finalized, finalized, ended) e FROM push_batches "
                      "WHERE started>? AND COALESCE(after_finalized, finalized, ended) IS NOT NULL LIMIT ?", (t0, ROWS)):
            h["batch"].append(max(0.0, float(r["e"]) - float(r["s"])))
    except Exception:   # a database from before the push queue
        pass
    return h


def _epoch(v) -> float | None:
    """A hand-off `eta`: epoch seconds, or an ISO time with its UTC offset; None for anything else."""
    from datetime import datetime
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.timestamp() if d.tzinfo else None


def outside_wait(t: dict, now: float, reports=None) -> dict | None:
    """A waiting task whose retry_when watches work outside the harness (a Slurm job, a detached run, a
    peer's ack: any probe but a bare clock gate or a landing wait, `landed:#N` or `ttp landed --task N`
    terms): {"at": the newest measured `eta` (with an `eta_basis`, still ahead) from its hand-off or its
    `ttp progress report` lines since it began waiting (`reports`: progress.entries lines, or a callable
    giving them, read only for such a wait), or None, "follows": an on_pass_cmd records the end, no run}.
    None for any other task, and for one the daemon already woke (its hand-off's `woke`: it is due and
    waits only for dispatch), which keep the usual estimate."""
    from .daemon import time_gate
    from .db import load_result, waiting_handoff
    from .landed import LANDED_PROBE
    res = load_result(t.get("result")) if t.get("status") == "waiting" else waiting_handoff(t)
    probe = str(res.get("retry_when") or "").strip()
    if (res.get("status") != "waiting" or res.get("woke") or not probe or time_gate(probe) is not None
            or LANDED_PROBE.fullmatch(probe) or LANDED_ALL.fullmatch(probe)):
        return None

    def measured(r: dict) -> float | None:
        at = _epoch(r.get("eta")) if str(r.get("eta_basis") or "").strip() else None
        return at if at is not None and now < at < float("inf") else None
    at = measured(res)
    try:
        since = float(res.get("waiting_since"))
    except (TypeError, ValueError):
        since = None   # no start of this wait on record: an older wait's reports are never read
    if since is not None:
        mine = [(ts, r) for r in (reports() if callable(reports) else reports) or []
                if str(r.get("task") or "") == str(t.get("id")) and (ts := _epoch(r.get("ts"))) is not None
                and ts >= since]
        for _, r in sorted(mine, key=lambda x: x[0]):
            at = measured(r) or at   # a later valid report wins; an invalid one keeps the last
    return {"at": at, "follows": bool(str(res.get("on_pass_cmd") or "").strip())}


class _Est:
    def __init__(self, db, now: float, hist: dict | None = None, core: str | None = None,
                 delivery: dict | None = None):
        self.db, self.now = db, now
        self.delivery = delivery or {}   # the project's delivery config: push_batch_max, push_age_s (_push_ahead)
        self.h = history(db, now) if hist is None else hist
        self.memo: dict[int, dict] = {}
        self._review_since = None
        self._reviews = None
        self.core = core or "claude"   # the provider of a task with none (config core_provider)
        self._gates = None
        self._owners = None
        self._reports = None

    def reports(self) -> list[dict]:
        """The project's `ttp progress report` lines (read once per estimate; none when unreadable)."""
        if self._reports is None:
            from . import progress
            try:
                self._reports = progress.entries(Path(self.db.path).parent)
            except Exception:
                self._reports = []
        return self._reports

    def _held(self, t: dict | None) -> tuple[float | None, str] | None:
        """A queued or waiting task the spend gate of its provider holds: (the gate's known reset or
        None, what clears it); None when nothing holds it."""
        from . import budget as bud, urgent
        if not t or t["status"] not in ("queued", "waiting"):
            return None
        if self._gates is None:
            got = self.db.kv("gates", {})
            self._gates = got if isinstance(got, dict) else {}
        prov = t.get("provider") or self.core
        g = self._gates.get(prov)
        if not isinstance(g, dict) or g.get("allow_new_work", True) is not False:
            return None
        if (g.get("lane") or {}).get("max_parallel"):   # the coordinator turn guard: routine work only
            if self._owners is None:
                from .obligations import owner_due
                try:
                    self._owners = set(owner_due(self.db))
                except Exception:
                    self._owners = set()
            if urgent.is_urgent(t) or urgent.user_ordered(t, self._owners):
                return None
        gate = bud.Gate(provider=prov, reasons=[r for r in g.get("reasons") or [] if isinstance(r, str)],
                        numbers=g.get("numbers") if isinstance(g.get("numbers"), dict) else {})
        lim = self.db.kv(f"limited:{prov}")
        return bud.hold_clears(self.db, gate, self.now, lim.get("until") if isinstance(lim, dict) else None)

    def _held_none(self, tid: int, clears: str) -> dict:
        return self._none(tid, f"#{tid} is held by the spend cap until {clears}")

    def _run(self, t: dict) -> list[float]:
        return self.h["run"].get((t["kind"], t["tier"])) or self.h["kind"].get(t["kind"]) or []

    def _left(self, xs: list[float], elapsed: float) -> tuple[float, bool]:
        """Median time left of the samples longer than `elapsed`; past all of them: 0, overrunning."""
        longer = [x - elapsed for x in xs if x > elapsed]
        return (_med(longer), False) if longer else (0.0, True)

    def task(self, tid: int, seen: tuple = ()) -> dict:
        """{"done": when it is done (its dependants may start), "end": when its work has landed, "path":
        the critical path ids, "legs": the measured legs, "why": set when there is no estimate}."""
        if tid in self.memo:
            return self.memo[tid]
        t = self.db.task(tid)
        out = self._task(t, tid, seen + (tid,))
        self.memo[tid] = out
        return out

    def _open_review(self, t: dict) -> int | None:
        """The open review task (db.reviews_task) of a done code task: it is not landed until that review
        approves it and its push-queue row lands."""
        from .db import reviews_task
        if self._reviews is None:
            self._reviews = self.db.q("SELECT * FROM tasks WHERE kind='review' AND status NOT IN (?,?,?) ORDER BY id",
                                      CLOSED)
        return next((r["id"] for r in self._reviews if reviews_task(r, t)), None)

    def _after_review(self, tid: int, rv: int, seen: tuple) -> dict:
        """A done code task awaiting review task `rv`: done now, landed at that review's end plus the median
        landing (when the project has one)."""
        if rv in seen:
            return self._cycle(tid, seen, rv)
        # The review depends on this task, which is done: it may start now (task() replaces this memo).
        self.memo[tid] = {"done": self.now, "end": self.now, "path": [tid], "legs": [], "why": None, "overrun": False}
        e = self.task(rv, tuple(x for x in seen if x != tid))
        if e["why"]:
            return self._from(tid, e)
        legs, end = list(e["legs"]), e["end"]
        if self.h["land"]:
            legs.append(("landing", _med(self.h["land"])))
            end += legs[-1][1]
        return {"done": self.now, "end": end, "path": [tid] + e["path"], "legs": legs, "why": None,
                "overrun": e["overrun"]}

    def _push_ahead(self, tid: int, target: str) -> tuple[int, float] | None:
        """For task `tid`'s approved row on `target`: (the approvals ahead of it in the queue's order,
        seconds until the batch that takes it ends), or None with no measured batch or no such row."""
        from . import pushq
        if not self.h.get("batch"):
            return None
        d = self.delivery
        rows = pushq._approved(self.db, target, self.now, pushq._num(d.get("push_age_s"), pushq.DEFAULT_AGE_S, float, 0))
        ahead = next((i for i, r in enumerate(rows) if r["task"] == tid), None)
        if ahead is None:
            return None
        cap = int(pushq._num(d.get("push_batch_max"), pushq.DEFAULT_BATCH_MAX, int, 1))
        med = _med(self.h["batch"])
        live = self.db.one("SELECT started FROM push_batches WHERE after_finalized IS NULL ORDER BY started LIMIT 1")
        left = max(0.0, med - (self.now - float(live["started"]))) if live else 0.0
        return ahead, left + (ahead // cap + 1) * med

    def _cycle(self, tid, seen: tuple, d: int) -> dict:
        return self._none(tid, "dependency cycle " + "→".join(f"#{i}" for i in seen[seen.index(d):] + (d,)))

    def _none(self, tid, why: str) -> dict:
        return {"done": None, "end": None, "path": [tid], "legs": [], "why": why, "overrun": False}

    def _from(self, tid, e: dict) -> dict:
        """No estimate for `tid` because the task `e` estimates on its path has none: its why and gate."""
        return {**self._none(tid, e["why"]), "path": [tid] + e["path"],
                **({"after": e["after"]} if e.get("after") else {})}

    def _gate_dead(self, n: int, waiter: int) -> str | None:
        """Why gated task `n`, not landed, has no live owner to land it ('' -> None: it has one): failed,
        cancelled, abandoned or gone, or done with nothing open carrying it on (deadwait.path)."""
        from . import deadwait, landed
        t = self.db.task(n)
        if not t:
            return f"#{n} is gone"
        if t["status"] in ("failed", "cancelled"):
            return f"#{n} {t['status']}"
        if landed.abandoned(self.db, n):
            return f"#{n} was abandoned"
        if t["status"] != "done" or deadwait.path(self.db, n, {waiter}):
            return None
        stuck = deadwait.stuck_by(self.db, n)
        return (f"#{n} is done but not landed and nothing open carries it on"
                + (f"; {stuck['what']}" if stuck else ""))

    def _after(self, tid: int, gate: str, why: str, path: list) -> dict:
        return {**self._none(tid, why), "path": path, "after": gate}

    def _task(self, t: dict | None, tid: int, seen: tuple) -> dict:
        now = self.now
        if not t:
            return self._none(tid, f"#{tid} is gone")
        st = t["status"]
        if st in ("failed", "cancelled"):
            return self._none(tid, f"#{tid} {st}")
        if st == "blocked":
            return self._none(tid, f"#{tid} is blocked")
        code = t["kind"] == "code"
        if st == "done" or st == "pushing":
            row = None
            if code or t["kind"] == "review":   # a review owns its approval's push-queue row
                try:
                    row = self.db.one("SELECT created, status, target FROM push_queue WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))
                except Exception:
                    row = None
            if not row and code and st == "done" and (rv := self._open_review(t)):
                return self._after_review(tid, rv, seen)
            if not row or row["status"] in PUSH_FINAL:
                return {"done": now, "end": now, "path": [tid], "legs": [], "why": None, "overrun": False}
            if row["status"] == "approved" and (q := self._push_ahead(tid, row["target"])):
                n, left = q
                return {"done": now, "end": now + left, "path": [tid], "why": None, "overrun": False,
                        "legs": [(f"landing ({n} approval{'' if n == 1 else 's'} ahead)", left)]}
            if not self.h["land"]:
                return self._none(tid, "no measured landing yet")
            left, over = self._left(self.h["land"], now - float(row["created"]))
            return {"done": now, "end": now + left, "path": [tid], "legs": [("landing", left)], "why": None,
                    "overrun": over}
        legs, path, over = [], [tid], False
        start = now
        begin = None
        if st == "review":
            if self._review_since is None:
                self._review_since = self.db.review_since()
            since = self._review_since.get(tid, now)
            if self.h["review"]:
                left, over = self._left(self.h["review"], now - since)
                if (rv := self._open_review(t)) and (hold := self._held(self.db.task(rv))):
                    if hold[0] is None:
                        return self._held_none(tid, hold[1])
                    left, over = max(left, hold[0] - now + _med(self.h["review"])), False
                legs.append(("review", left))
            done = now + sum(x for _, x in legs)
        else:
            from .db import dependency_ids
            crit = None
            for d in dependency_ids(t):
                if d is None:
                    return self._none(tid, f"#{tid} names a dependency that is not a task id")
                if (dt := self.db.task(d)) and dt["status"] in ("done", "pushing"):
                    continue   # finished: its dependants may start now, and the walk ends there
                if d in seen:
                    return self._cycle(tid, seen, d)
                e = self.task(d, seen)
                if e["why"]:
                    return self._from(tid, e)
                if crit is None or e["done"] > crit["done"]:
                    crit = e
            if crit and crit["done"] > start:
                start, path = crit["done"], [tid] + crit["path"]
            for n in landing_waits(t) if st != "running" else []:
                from .landed import recorded
                if recorded(self.db, n)[0]:
                    continue   # landed: the probe passes
                if n in seen:
                    return self._cycle(tid, seen, n)
                # Its gate's own critical path while a live owner can land it; else no clock time, never
                # the probe's next check.
                if dead := self._gate_dead(n, tid):
                    return self._after(tid, f"landed:#{n}", dead, [tid, n])
                e = self.task(n, seen)
                if e["why"]:
                    return self._after(tid, e.get("after") or f"landed:#{n}", e["why"], [tid] + e["path"])
                if e["end"] > start:   # its own finish, review and landing: the probe passes then
                    start, path = e["end"], [tid] + e["path"]
            runs = self._run(t)
            ow = outside_wait(t, now, self.reports) if st != "running" else None
            if st != "running" and (pr := merge_gate(gate_probe(t))) is not None and not (ow and ow["at"]):
                return self._after(tid, f"PR #{pr} merges or closes", "a merge gate has no measured finish", path)
            if ow and ow["at"] is None:
                return {**self._none(tid, f"#{tid} waits on outside work: no measured finish yet"), "path": path}
            if not runs and not (ow and ow["follows"]):
                return {**self._none(tid, f"no measured {t['kind']} run yet"), "path": path}
            if ow:   # its hand-off's measured eta; a median run after it unless an on_pass_cmd ends the task
                legs.append(("outside work", max(0.0, ow["at"] - start)))
                if not ow["follows"]:
                    if hold := self._held(t):
                        if hold[0] is None:
                            return {**self._held_none(tid, hold[1]), "path": path}
                        legs[-1] = ("outside work", max(legs[-1][1], hold[0] - start))
                    begin = start + legs[-1][1]
                    legs.append(("run", _med(runs)))
            elif st == "running":
                r = self.db.one("SELECT started FROM runs WHERE task=? AND status='running' AND role!='coordinator' "
                                "ORDER BY id DESC LIMIT 1", (tid,))
                left, over = self._left(runs, now - float(r["started"])) if r and r["started"] else (_med(runs), False)
                legs.append(("run", left))
            else:
                if st == "waiting" or t.get("not_before"):
                    start = max(start, float(t.get("not_before") or 0))
                if hold := self._held(t):
                    if hold[0] is None:
                        return {**self._held_none(tid, hold[1]), "path": path}
                    start = max(start, hold[0])
                if st == "queued":
                    locks = _locks(t)
                    waits = [w for lk in locks if (w := _med(self.h["queue"].get(lk, [])) ) is not None]
                    wait = max(waits) if waits else _med(self.h["wait"])
                    if wait is None:
                        return {**self._none(tid, "no measured queue wait yet"), "path": path}
                    legs.append(("queue " + ",".join(locks) if waits else "queue", wait))
                begin = start + sum(x for _, x in legs)   # when a worker is expected to start it
                legs.append(("run", _med(runs)))
            done = start + sum(x for _, x in legs)
            if code and self.h["review"]:
                legs.append(("review", _med(self.h["review"])))
        end = start + sum(x for _, x in legs)
        if code and self.h["land"]:
            legs.append(("landing", _med(self.h["land"])))
            end += legs[-1][1]
        return {"done": done, "end": end, "path": path, "legs": legs, "why": None, "overrun": over, "start": begin}


def estimate(db, tid: int, now: float | None = None, hist: dict | None = None, core: str | None = None,
             delivery: dict | None = None) -> dict:
    return _Est(db, time.time() if now is None else now, hist, core, delivery).task(int(tid))


def core_of(where) -> str | None:
    """The core provider of a project or its config (a task with no provider runs there)."""
    try:
        cfg = where.config() if hasattr(where, "config") else where
        return str((cfg or {}).get("core_provider") or "") or None
    except Exception:
        return None


def delivery_of(where) -> dict | None:
    """The delivery config of a project or its config (push_batch_max, push_age_s: _Est._push_ahead)."""
    try:
        cfg = where.config() if hasattr(where, "config") else where
        d = (cfg or {}).get("delivery")
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def obligation_estimate(db, o: dict, now: float | None = None, est: _Est | None = None,
                        core: str | None = None, delivery: dict | None = None) -> dict:
    """The expected finish of an obligation or promise: the latest landing of its owner tasks
    (`core`: the project's core provider, for owner tasks with none, and `delivery` its delivery
    config, when no `est` is given)."""
    from .obligations import _owner_ids
    est = est or _Est(db, time.time() if now is None else now, core=core, delivery=delivery)
    ids = _owner_ids(o)
    if not ids:
        return {"done": None, "end": None, "path": [], "legs": [], "why": "no owner task", "overrun": False}
    worst = None
    for tid in ids:
        e = est.task(tid)
        if e["why"]:
            return e
        if worst is None or e["end"] > worst["end"]:
            worst = e
    return worst


def _dur(s: float) -> str:
    m = round(s / 60)
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m}m"


def field(e: dict, where, now: float | None = None) -> str:
    """One short field: `ETA 22:40 PST (critical path #5→#3)`, `ETA: after landed:#3 (why)` or `ETA: no
    estimate yet (why)`."""
    from . import timefmt
    if e.get("after"):   # a landing or merge gate with no live owner or estimate: no clock time
        return f"ETA: after {e['after']} ({e['why']})"
    if e["why"]:
        return f"ETA: no estimate yet ({e['why']})"
    path = "→".join(f"#{i}" for i in e["path"])
    return (f"ETA {timefmt.short(e['end'], where, now)} (critical path {path}"
            + (", overrunning its median" if e["overrun"] else "") + ")")


def text(e: dict, where, now: float | None = None) -> str:
    """field() with its legs, for `ttp eta`."""
    legs = ", ".join(f"{k} {_dur(v)}" for k, v in e["legs"])
    return field(e, where, now) + (f"; legs of #{e['path'][0]}: {legs}" if legs else "") + (
        "; medians of this project's last 14 days, no padding" if not e["why"] else "")


def wake_at(e: dict) -> float | None:
    """The epoch a deadline wake on this task belongs at: its expected finish (None: no estimate)."""
    return None if e["why"] else e["end"]


def _threshold(cfg: dict | None) -> float:
    from . import course
    got = (((cfg or {}).get("coordinator") or {}).get(course.KEY) or {})
    try:
        return max(0.0, float(got.get("long_promise_h", LONG_H))) if isinstance(got, dict) else LONG_H
    except (TypeError, ValueError):
        return LONG_H


def _urgent_owner(db, o: dict) -> bool:
    from . import urgent
    from .obligations import _owner_ids
    return any(urgent.is_urgent(db.task(t)) for t in _owner_ids(o))


def long_promises(db, cfg: dict | None, now: float | None = None) -> list[dict]:
    """The open promises and milestones on urgent goals due more than the threshold after they were made,
    not shortened, met or justified for that due: {"id", "what", "due", "made", "o"}."""
    from .obligations import open_promises, items, _open
    now = time.time() if now is None else now
    h = _threshold(cfg)
    if h <= 0:
        return []
    out = []
    proms = open_promises(db)
    for o in items(db):
        if not _open(o):
            continue
        cands = []
        if (pr := o.get("promise")) and not pr.get("claim"):   # a claim's due is its check time, no ETA
            cands.append(("promise", float(pr["due"]), float(pr.get("set_at") or o["created"])))
        for m in (o.get("milestones") or {}).values():
            if not m.get("receipt"):
                cands.append((f"milestone {m['key']}", float(m["due"]), float(m.get("added") or o["created"])))
        cands = [c for c in cands if c[1] - c[2] > h * 3600 and c[1] > now]
        if not cands or not _urgent_owner(db, o):
            continue
        for what, due, made in cands:
            j = (o.get("eta_justified") or {}).get(what)
            if j and float(j.get("due") or 0) == due:
                continue
            if what == "promise" and any(
                    q["id"] != o["id"] and q["promise"]["chat"] == o["promise"]["chat"]
                    and float(q["promise"].get("set_at") or q["created"]) > made and float(q["promise"]["due"]) < due
                    for q in proms):
                continue   # a later promise to the same chat moved it earlier
            out.append({"id": o["id"], "what": what, "due": due, "made": made, "o": o})
    return out


def course_lines(db, cfg: dict | None, now: float | None = None) -> tuple[list[str], list[str]]:
    """Course check lines for long_promises (with each one's estimate) and their identities."""
    from . import timefmt
    now = time.time() if now is None else now
    rows = long_promises(db, cfg, now)
    if not rows:
        return [], []
    est = _Est(db, now, core=core_of(cfg), delivery=delivery_of(cfg))
    out, ident = [], []
    for r in rows:
        e = obligation_estimate(db, r["o"], now, est)
        out.append(f"- long promise on urgent work: {r['id']} {r['what']} '{r['o']['text'][:120]}' is due "
                   f"{timefmt.short(r['due'], cfg or {}, now)}, {(r['due'] - r['made']) / 3600:.1f} h after it was made; "
                   f"{field(e, cfg or {}, now)} | shorten it (tell its chat the new time) or justify it: `obligation` "
                   f"with key {r['id']} and `justify` naming the critical path")
        ident.append(f"long_promise:{r['id']}:{r['what']}:{r['due']:.0f}")
    return out, ident


def justify(p, oid: str, why: str, now: float | None = None) -> str:
    """Record the coordinator's reason for a long due on urgent work: it holds for the current dues of the
    promise and its milestones, and a moved due needs a new one."""
    from . import obligations
    why = " ".join(str(why).split())[:obligations.EVIDENCE_MAX]
    if not why:
        raise ValueError("justify needs the critical path that makes the due this long")
    now = time.time() if now is None else now
    with p.db.tx():
        st = obligations._load(p.db)
        o = obligations._need(st, oid)
        j = o.setdefault("eta_justified", {})
        if o.get("promise"):
            j["promise"] = {"why": why, "due": float(o["promise"]["due"]), "ts": now}
        for m in (o.get("milestones") or {}).values():
            j[f"milestone {m['key']}"] = {"why": why, "due": float(m["due"]), "ts": now}
        p.db.set_kv(obligations.KV, st)
    return f"obligation {oid}: long due justified ({why[:120]})"


def state_lines(db, where, now: float | None = None, host=None, skip=()) -> list[str]:
    """The STATE section: each urgent open task and each open promise with its expected finish. `host`:
    a task's started-on-host state (onhost.line), shown after its estimate. `skip`: task ids shown
    elsewhere, left out before tasks with one reason share a line."""
    from . import urgent
    from .obligations import open_promises
    now = time.time() if now is None else now
    hot = [t for t in db.q("SELECT * FROM tasks WHERE status NOT IN ('done','failed','cancelled') "
                           "ORDER BY priority, id LIMIT 60") if urgent.is_urgent(t) and t["id"] not in skip]
    proms = open_promises(db)
    if not hot and not proms:
        return []
    est = _Est(db, now, core=core_of(where), delivery=delivery_of(where))
    out = ["## Expected finish (critical path, measured medians; quote these, never padded worst cases)"]
    # no estimate for one gate and reason: one line (titles in Open tasks); the gate ('after') is kept
    same: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
    for t in hot[:10]:
        on = host(t) if host else ""
        e = est.task(t['id'])
        why = str(e["why"] or "").removeprefix(f"#{t['id']} ")
        if why and not on:
            same.setdefault((str(e.get("after") or ""), why), []).append((t, e))
            continue
        out.append(f"- #{t['id']} {t['title'][:80]}: {field(e, where, now)}" + (f"; {on}" if on else ""))
    for (after, why), got in same.items():
        ids = ", ".join(f"#{t['id']}" for t, _ in got)
        if len(got) == 1:
            out.append(f"- #{got[0][0]['id']} {got[0][0]['title'][:80]}: {field(got[0][1], where, now)}")
        else:
            out.append(f"- {ids}: ETA: after {after} ({why})" if after else f"- {ids}: ETA: no estimate yet ({why})")
    out += [f"- promise {o['id']}: {field(obligation_estimate(db, o, now, est), where, now)}" for o in proms[:10]]
    return out
