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
  lands at that review task's own estimate plus the landing leg.
A blocked, failed or cancelled task on the path, or a leg with no measured sample, gives no
estimate ('no estimate yet', with why): never a guess.

An open promise or obligation is done when the last of its owner tasks lands (obligation_estimate).
The coordinator's STATE (urgent tasks and promises), `ttp status` and the web app's task view show
it as one short field; workers read `ttp eta <task>` before setting a retry or deadline wake.

long_promises() is the self-watch: an open promise or milestone to the user on an urgent goal (an
owner is urgent, urgent.is_urgent) due more than coordinator.course_check.long_promise_h (LONG_H)
after it was made. Each gives one Course check line until it is met, shortened (its due moved, or a
later promise to the same chat with an earlier due) or justified on record (the coordinator's
`obligation` action with `key` and `justify`, kept for that due). It never pages the user."""
from __future__ import annotations

import json
import statistics
import time

WINDOW_S = 14 * 86400
LONG_H = 2.0
ROWS = 5000
CLOSED = ("done", "failed", "cancelled")
PUSH_DONE = ("pushed", "landed")
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


def history(db, now: float | None = None) -> dict:
    """The measured samples of the window: run times by (kind, tier) and kind, first-start waits by
    lock label and overall, review and landing times."""
    now = time.time() if now is None else now
    t0 = now - WINDOW_S
    h = {"run": {}, "kind": {}, "queue": {}, "wait": [], "review": [], "land": []}
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
    except Exception:   # a database from before the push queue
        pass
    return h


class _Est:
    def __init__(self, db, now: float, hist: dict | None = None):
        self.db, self.now = db, now
        self.h = history(db, now) if hist is None else hist
        self.memo: dict[int, dict] = {}
        self._review_since = None
        self._reviews = None

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
            return self._none(tid, f"#{tid} has a dependency cycle")
        # The review depends on this task, which is done: it may start now (task() replaces this memo).
        self.memo[tid] = {"done": self.now, "end": self.now, "path": [tid], "legs": [], "why": None, "overrun": False}
        e = self.task(rv, tuple(x for x in seen if x != tid))
        if e["why"]:
            return {**self._none(tid, e["why"]), "path": [tid] + e["path"]}
        legs, end = list(e["legs"]), e["end"]
        if self.h["land"]:
            legs.append(("landing", _med(self.h["land"])))
            end += legs[-1][1]
        return {"done": self.now, "end": end, "path": [tid] + e["path"], "legs": legs, "why": None,
                "overrun": e["overrun"]}

    def _none(self, tid, why: str) -> dict:
        return {"done": None, "end": None, "path": [tid], "legs": [], "why": why, "overrun": False}

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
            if code:
                try:
                    row = self.db.one("SELECT created, status FROM push_queue WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))
                except Exception:
                    row = None
            if not row and code and st == "done" and (rv := self._open_review(t)):
                return self._after_review(tid, rv, seen)
            if not row or row["status"] in PUSH_FINAL:
                return {"done": now, "end": now, "path": [tid], "legs": [], "why": None, "overrun": False}
            if not self.h["land"]:
                return self._none(tid, "no measured landing yet")
            left, over = self._left(self.h["land"], now - float(row["created"]))
            return {"done": now, "end": now + left, "path": [tid], "legs": [("landing", left)], "why": None,
                    "overrun": over}
        legs, path, over = [], [tid], False
        start = now
        if st == "review":
            if self._review_since is None:
                self._review_since = self.db.review_since()
            since = self._review_since.get(tid, now)
            if self.h["review"]:
                left, over = self._left(self.h["review"], now - since)
                legs.append(("review", left))
            done = now + sum(x for _, x in legs)
        else:
            from .db import dependency_ids
            crit = None
            for d in dependency_ids(t):
                if d is None:
                    return self._none(tid, f"#{tid} names a dependency that is not a task id")
                if d in seen:
                    return self._none(tid, f"#{tid} has a dependency cycle")
                e = self.task(d, seen)
                if e["why"]:
                    return {**self._none(tid, e["why"]), "path": [tid] + e["path"]}
                if crit is None or e["done"] > crit["done"]:
                    crit = e
            if crit and crit["done"] > start:
                start, path = crit["done"], [tid] + crit["path"]
            runs = self._run(t)
            if not runs:
                return {**self._none(tid, f"no measured {t['kind']} run yet"), "path": path}
            if st == "running":
                r = self.db.one("SELECT started FROM runs WHERE task=? AND status='running' AND role!='coordinator' "
                                "ORDER BY id DESC LIMIT 1", (tid,))
                left, over = self._left(runs, now - float(r["started"])) if r and r["started"] else (_med(runs), False)
                legs.append(("run", left))
            else:
                if st == "waiting" or t.get("not_before"):
                    start = max(start, float(t.get("not_before") or 0))
                if st == "queued":
                    locks = _locks(t)
                    waits = [w for lk in locks if (w := _med(self.h["queue"].get(lk, [])) ) is not None]
                    wait = max(waits) if waits else _med(self.h["wait"])
                    if wait is None:
                        return {**self._none(tid, "no measured queue wait yet"), "path": path}
                    legs.append(("queue " + ",".join(locks) if waits else "queue", wait))
                legs.append(("run", _med(runs)))
            done = start + sum(x for _, x in legs)
            if code and self.h["review"]:
                legs.append(("review", _med(self.h["review"])))
        end = start + sum(x for _, x in legs)
        if code and self.h["land"]:
            legs.append(("landing", _med(self.h["land"])))
            end += legs[-1][1]
        return {"done": done, "end": end, "path": path, "legs": legs, "why": None, "overrun": over}


def estimate(db, tid: int, now: float | None = None, hist: dict | None = None) -> dict:
    return _Est(db, time.time() if now is None else now, hist).task(int(tid))


def obligation_estimate(db, o: dict, now: float | None = None, est: _Est | None = None) -> dict:
    """The expected finish of an obligation or promise: the latest landing of its owner tasks."""
    from .obligations import _owner_ids
    est = est or _Est(db, time.time() if now is None else now)
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
    """One short field: `ETA 22:40 PST (critical path #5→#3)` or `ETA: no estimate yet (why)`."""
    from . import timefmt
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
        if pr := o.get("promise"):
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
    est = _Est(db, now)
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


def state_lines(db, where, now: float | None = None) -> list[str]:
    """The STATE section: each urgent open task and each open promise with its expected finish."""
    from . import urgent
    from .obligations import open_promises
    now = time.time() if now is None else now
    hot = [t for t in db.q("SELECT * FROM tasks WHERE status NOT IN ('done','failed','cancelled') "
                           "ORDER BY priority, id LIMIT 60") if urgent.is_urgent(t)]
    proms = open_promises(db)
    if not hot and not proms:
        return []
    est = _Est(db, now)
    out = ["## Expected finish (critical path, measured medians; quote these, never padded worst cases)"]
    out += [f"- #{t['id']} {t['title'][:80]}: {field(est.task(t['id']), where, now)}" for t in hot[:10]]
    out += [f"- promise {o['id']}: {field(obligation_estimate(db, o, now, est), where, now)}" for o in proms[:10]]
    return out
