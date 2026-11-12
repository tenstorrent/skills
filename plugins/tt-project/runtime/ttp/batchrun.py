# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Batch runs: one worker takes a batch of related small ready code tasks in one run, on one branch.

When dispatch starts a code task (the lead), it adds to the same run up to `dispatch.batch_max` other
ready queued code tasks of the project's repo that are small (pick): light or standard tier, not
exclusive, no device or machine resource, the lead's provider and model pins or none, not user_deep.
Tasks naming files the lead's spec names come first (overlap.spec_paths), then by priority; a task
whose files a running task on another branch holds is left out; a member that depends on another comes
after it. The lead is whatever dispatch picked first (an urgent one leads): it never waits for a batch
to fill. An urgent (priority 1) task never joins another's run: it starts in its own, in parallel. Off with `dispatch.batch` false.

The batch shares the lead's worktree and branch (each member's record carries it), and the worker
prompt lists every member (prompt_block). The worker commits each member separately with a
`Task: #<id>` trailer and hands off one outcome per member (`members` in result.json and on_pass).
The run's budget is the sum of the members' remaining budgets, capped at the deep tier's default; its
cost is booked to each member pro rata to their budgets (book). Members wait as `running` with a
blocked_reason naming the lead, which reconcile_tasks leaves alone (held). Once the lead leaves
running, waiting and review (settle): with the lead done, a member whose outcome is done is done on
the lead's branch, which the lead's one review covers; it counts as landed only once the push queue
lands that branch with its own commit on the push branch (landings), and a member the approved head
left out gets a review of its own; a blocked one is blocked with its
reason; a member whose own `Task: #<id>` commit is on the push branch (landed.trailer_sha) is done as
landed whatever the hand-off says, unless it marks it failed or blocked; a failed or missing one goes
back to the queue alone, on a branch of its own, and dispatch closes it instead of running it once its
commit has landed since (close_landed). A lead asleep on its own wait (a waiting hand-off, stored as
queued) still holds its members (holds). The record is the kv `batchrun:<lead>` (id, lead, members,
branch, run; once settled also `settled`, each member's `outcomes` and `why`, kept 14 days), which
status, the web app, the digest and the review and checks paths read (members_of, batch_of). No model;
git only to read the push branch's log.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .db import dump_result, load_result

KV = "batchrun:"
HOLDING = ("running", "waiting", "review")   # lead statuses that keep its members held
SMALL = ("light", "standard")
NO_START = ("wait", "resume")
KEEP_S = 14 * 86400   # a settled record stays readable (review and checks grouping) this long


def holds(lead: dict | None) -> bool:
    """The lead still holds its members: it runs, waits or is in review, or it is queued asleep on its
    own wait (a waiting hand-off is stored as queued with a `waiting` result), as a detached push is."""
    if not lead:
        return False
    if lead["status"] in HOLDING:
        return True
    return lead["status"] == "queued" and str((load_result(lead.get("result")) or {}).get("status")) == "waiting"


def landed_sha(p, tid: int, since: float | None = None, tip: str = "") -> str:
    """Task `tid`'s own `Task: #<id>` commit on the push branch (landed.trailer_sha; from `tip` when
    given), "" when none or it cannot be read: never raises, so a git fault only leaves the member to
    the queue."""
    from . import landed
    try:
        return landed.trailer_sha(p, tid, since, tip)
    except Exception:
        return ""


def close_landed(p, task: dict) -> str:
    """Dispatch's check of a batch member settle requeued: once its own commit is on the push branch it is
    closed done as landed (not run again) and the reason returned; "" when it was not requeued from a
    batch or has not landed."""
    res = load_result(task.get("result"))
    if not isinstance(res, dict) or res.get("status") != "requeued" or not isinstance(res.get("batch"), dict):
        return ""
    if not (sha := landed_sha(p, task["id"], task.get("created"))):
        return ""
    why = f"its commit {sha[:10]} from batch run {res['batch'].get('id')} is on the push branch: closed as landed, not run again"
    p.db.update_task(task["id"], status="done", blocked_reason=None,
                     result=dump_result({"status": "done", "summary": why, "landed": sha, "batch": res["batch"]}))
    return why


def _members_arg(got):
    """A hand-off's `members`: a list, or a list written as a JSON or Python literal string."""
    if isinstance(got, str):
        import ast
        for parse in (json.loads, ast.literal_eval):
            try:
                got = parse(got)
                break
            except (ValueError, SyntaxError, TypeError):
                continue
    return got if isinstance(got, list) else None


def enabled(cfg: dict) -> bool:
    return bool((cfg.get("dispatch") or {}).get("batch", True))


def batch_max(cfg: dict) -> int:
    try:
        return max(int((cfg.get("dispatch") or {}).get("batch_max", 5)), 0)
    except (TypeError, ValueError):
        return 5


def _labels(t: dict) -> list:
    try:
        v = json.loads(t.get("labels") or "[]")
    except (ValueError, TypeError):
        return []
    return [lb for lb in v if isinstance(lb, str)] if isinstance(v, list) else []


def _deps(t: dict) -> list[int]:
    try:
        return [int(d) for d in json.loads(t.get("depends_on") or "[]")]
    except (ValueError, TypeError):
        return []


def _repo(t: dict) -> str:
    return next((lb[7:] for lb in _labels(t) if lb.startswith("pr_fix:")), "")


def _resourced(t: dict) -> bool:
    return any(lb.split(":", 1)[0] in ("resource", "exclusive") or lb in ("needs_device", "exclusive")
               for lb in _labels(t))


def own_branch(t: dict, recorded: bool = True) -> bool:
    """`t` carries a branch of its own (a `pr_branch:` line or label, a PR fix, a continuation or, with
    `recorded`, a branch on its record): it neither joins a batch nor leads one, so no other task's work
    lands on a PR's or another task's branch. A lead's own recorded ttp/t<id> branch is its batch's."""
    labels = _labels(t)
    return bool((recorded and t.get("branch")) or t.get("pr_url") or "pr_branch:" in (t.get("spec") or "")
                or any(str(x).startswith(("pr_branch:", "pr_fix:", "continues:")) for x in labels))


def eligible(lead: dict, t: dict) -> bool:
    """`t` may join `lead`'s batch: a queued small code task of its repo with the same pins or none."""
    from .effort import USER_DEEP
    if t["id"] == lead["id"] or t.get("kind") != "code" or t.get("status") != "queued":
        return False
    if t.get("tier") not in SMALL or _resourced(t) or USER_DEEP in _labels(t) or _repo(t) != _repo(lead):
        return False
    if own_branch(t):
        return False
    if (t.get("priority") or 3) <= 1:   # urgent work starts in a worker of its own, in parallel
        return False
    if t.get("budget_usd") and float(t.get("spent_usd") or 0) >= float(t["budget_usd"]):
        return False
    return all(not t.get(k) or t.get(k) == lead.get(k) for k in ("provider", "model"))


def _paths(p, t: dict) -> set[str]:
    from . import overlap
    try:
        return set(overlap.keep(p, overlap.spec_paths(t.get("spec") or "", p.root)))
    except Exception:
        return set()


def pick(p, cfg: dict, lead: dict, ready: list[dict], inflight: list[dict] | None = None) -> list[dict]:
    """The members to add to `lead`'s run, in the order the worker takes them: related tasks (sharing
    a path the lead's spec names) first, then by priority and id; a member after any member it depends
    on. `inflight`: running tasks on other branches, whose spec paths keep a candidate out."""
    if not enabled(cfg) or lead.get("kind") != "code" or _resourced(lead) or own_branch(lead, recorded=False):
        return []
    n = batch_max(cfg)
    if n <= 0:
        return []
    mine = _paths(p, lead)
    held: set[str] = set()
    for t in inflight or []:
        if t["id"] != lead["id"] and (t.get("branch") or "") != (lead.get("branch") or ""):
            held |= _paths(p, t)
    cands = []
    for t in ready:
        if not eligible(lead, t):
            continue
        ps = _paths(p, t)
        if ps & held:
            continue
        cands.append((0 if ps & mine else 1, t.get("priority") or 3, t["id"], t))
    chosen = [c[3] for c in sorted(cands, key=lambda c: c[:3])][:n]
    return order(chosen)


def order(members: list[dict]) -> list[dict]:
    """`members` with each after the members it depends on (stable otherwise; a cycle keeps its order)."""
    ids, out, done = {m["id"] for m in members}, [], set()
    todo = list(members)
    while todo:
        for i, m in enumerate(todo):
            if not (set(_deps(m)) & ids) - done:
                break
        else:
            i = 0
        m = todo.pop(i)
        out.append(m)
        done.add(m["id"])
    return out


def budget(cfg: dict, lead: dict, remaining: float, members: list[dict]) -> float:
    """The run's budget: the lead's remaining plus each member's, capped at the deep tier's default."""
    from . import budget as bud
    total = max(remaining, 0.0)
    for m in members:
        b = m.get("budget_usd") or bud.task_default_usd(cfg, m.get("tier"))
        total += max(float(b) - float(m.get("spent_usd") or 0), 0.0)
    return min(total, bud.task_default_usd(cfg, "deep"))


def split_cost(total: float, budgets: dict[int, float]) -> dict[int, float]:
    """`total` booked to each id pro rata to its budget (equal shares when none has one)."""
    if not budgets:
        return {}
    weight = sum(max(b or 0.0, 0.0) for b in budgets.values())
    if weight <= 0:
        return {i: total / len(budgets) for i in budgets}
    return {i: total * max(b or 0.0, 0.0) / weight for i, b in budgets.items()}


def record(db, lead: dict, members: list[dict], branch: str, cfg: dict, run: int | None = None) -> dict:
    from . import budget as bud
    rec = {"id": f"b{lead['id']}", "lead": lead["id"], "members": [m["id"] for m in members],
           "branch": branch, "run": run, "started": time.time(),
           "budgets": {str(t["id"]): float(t.get("budget_usd") or bud.task_default_usd(cfg, t.get("tier")))
                       for t in [lead, *members]}}
    db.set_kv(KV + str(lead["id"]), rec)
    return rec


def batch_of(db, lead_id: int) -> dict | None:
    rec = db.kv(KV + str(lead_id))
    return rec if isinstance(rec, dict) else None


def live(db, settled: bool = False) -> list[dict]:
    out = []
    for r in db.q("SELECT key FROM kv WHERE key LIKE ?", (KV + "%",)):
        if (rec := batch_of(db, int(r["key"][len(KV):]))) is not None and (settled or not rec.get("settled")):
            out.append(rec)
    return out


def view(db, task_id) -> dict | None:
    """{"id": "b<lead>", "members": [ids]} for a run's task that leads a batch run, else None:
    `ttp status`, the web app and the coordinator digest show the batch as one run."""
    try:
        rec = batch_of(db, int(task_id)) if task_id else None
    except (TypeError, ValueError):
        return None
    return {"id": rec["id"], "members": list(rec.get("members", []))} if rec and not rec.get("settled") else None


def words(v: dict | None) -> str:
    """' (batch b<lead>: +#a, #b)' for view(), else ''."""
    return f" (batch {v['id']}: +" + ", ".join(f"#{m}" for m in v["members"]) + ")" if v else ""


def members_of(db, task_id: int, settled: bool = False) -> dict | None:
    """The batch `task_id` leads or belongs to: a live one, or with `settled` also one that ended
    (the review and checks of a batch read its members and their outcomes from it)."""
    rec = batch_of(db, task_id)
    if rec is not None and (settled or not rec.get("settled")):
        return rec
    return next((rec for rec in live(db, settled) if task_id in rec.get("members", [])), None)


def held(db) -> set[int]:
    """Members of batches whose lead still runs, waits or is in review: never requeued as orphans."""
    out: set[int] = set()
    for rec in live(db):
        if holds(db.task(rec["lead"])):
            out |= set(rec.get("members", []))
    return out


def book(db, lead_id: int, cost: float) -> dict[int, float]:
    """Move each member's pro-rata share of `cost`, just booked to the lead, onto the member."""
    rec = batch_of(db, lead_id)
    if not rec or rec.get("settled") or not cost:
        return {}
    budgets = {int(k): v for k, v in (rec.get("budgets") or {}).items()} or \
        {i: 1.0 for i in [rec["lead"], *rec.get("members", [])]}
    shares = split_cost(cost, budgets)
    for tid, share in shares.items():
        if tid != lead_id and share:
            db.x("UPDATE tasks SET spent_usd=COALESCE(spent_usd,0)+? WHERE id=?", (share, tid))
            db.x("UPDATE tasks SET spent_usd=MAX(COALESCE(spent_usd,0)-?,0) WHERE id=?", (share, lead_id))
    rec["spent"] = {str(k): round(float((rec.get("spent") or {}).get(str(k), 0)) + v, 6) for k, v in shares.items()}
    db.set_kv(KV + str(lead_id), rec)
    return shares


def outcomes(p, lead: dict) -> dict[int, dict]:
    """The lead's per-member outcomes: its stored result's `members`, else the newest run's result.json."""
    def rows_of(res):   # a waiting hand-off's outcomes ride in its on_pass
        got = _members_arg(res.get("members")) if isinstance(res, dict) else None
        if got is None and isinstance(res, dict) and isinstance(res.get("on_pass"), dict):
            got = _members_arg(res["on_pass"].get("members"))
        return got
    rows = rows_of(load_result(lead.get("result")))
    if rows is None:
        for r in p.db.q("SELECT id, dir FROM runs WHERE task=? ORDER BY id DESC LIMIT 5", (lead["id"],)):
            f = Path(r["dir"] or p.runs / str(r["id"])) / "result.json"
            try:
                rows = rows_of(json.loads(f.read_text()))
            except (OSError, ValueError):
                continue
            if rows is not None:
                break
    out = {}
    for m in rows or []:
        try:
            out[int(str(m.get("id")).lstrip("#"))] = m
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def settle(p, lead_id: int) -> list[tuple[int, str, str]]:
    """Settle the members of a batch whose lead no longer holds them (holds); drop its record.
    Returns (member, new status, why)."""
    db, rec = p.db, batch_of(p.db, lead_id)
    lead = db.task(lead_id)
    if rec is None or rec.get("settled") or holds(lead):
        return []
    got = outcomes(p, lead) if lead and lead["status"] == "done" else {}
    done_ok = bool(lead) and lead["status"] == "done"
    # Commit dates: a member commits after its run started, and a rebase only moves them later.
    since = float(rec["started"]) - 3600 if rec.get("started") else None
    out = []
    for mid in rec.get("members", []):
        m = db.task(mid)
        if not m or m["status"] != "running":
            continue
        o = got.get(mid) or {}
        st = str(o.get("status") or "")
        info = {"id": rec["id"], "lead": lead_id, "members": rec.get("members", [])}
        sha = "" if (done_ok and st == "done") or st in ("failed", "blocked") else landed_sha(p, mid, since)
        if (done_ok and st == "done") or sha:
            why = (f"its commit {sha[:10]} is on the push branch: closed as landed" if sha and st != "done" else
                   str(o.get("summary") or f"done in batch run of #{lead_id}"))
            db.update_task(mid, status="done", branch=rec.get("branch"), blocked_reason=None,
                           result=dump_result({"status": "done", "summary": why, "batch": info,
                                               **({"landed": sha} if sha else {}),
                                               **({"followups": o["followups"]} if o.get("followups") else {})}))
            st = "done"
        elif done_ok and st == "blocked":
            why = str(o.get("reason") or f"blocked in batch run of #{lead_id}")[:500]
            db.update_task(mid, status="blocked", branch=None, blocked_reason=why,
                           result=dump_result({"status": "blocked", "summary": str(o.get("summary") or ""),
                                               "batch": info}))
        else:
            why = str(o.get("reason") or o.get("summary") or
                      (f"no outcome in batch run of #{lead_id}" if done_ok else
                       f"batch lead #{lead_id} ended {lead['status'] if lead else 'missing'}"))[:300]
            why += f" ({'marked failed' if st == 'failed' else 'its commit is not on the push branch'}): requeued alone"
            st = "queued"
            db.update_task(mid, status="queued", branch=None, blocked_reason=None,
                           result=dump_result({"status": "requeued", "summary": why, "batch": info}))
        out.append((mid, st, why))
    db.set_kv(KV + str(lead_id), {**rec, "settled": time.time(), "outcomes": {str(m): s for m, s, _ in out},
                                  "why": {str(m): w for m, _, w in out}})
    return out


def landings(p, rec: dict) -> list[tuple[int, str, str]]:
    """Judge each done member of settled batch `rec` once the push queue landed its shared branch since
    the run started (the newest such landing): it landed only when its own `Task: #<id>` commit (or a
    rebased copy with that trailer) is on the push branch, recorded as `landed` in its result, which
    landed.recorded reads. A member the approved head left out is recorded `left_out` (not landed):
    the daemon queues its own review (Daemon.reconcile_tasks). Each member is judged once. Returns
    (member, "landed"|"left_out", why)."""
    db, branch = p.db, rec.get("branch")
    if not branch or not rec.get("started"):
        return []
    row = db.one("SELECT id, head, landed_sha, pushed_sha FROM push_queue WHERE branch=? AND status IN "
                 "('pushed','landed') AND created>=? ORDER BY id DESC LIMIT 1", (branch, float(rec["started"])))
    if not row:
        return []
    since, out = float(rec["started"]) - 3600, []
    as_sha = str(row["landed_sha"] or row["pushed_sha"] or "")
    for mid in rec.get("members", []):
        m = db.task(mid)
        res = load_result(m.get("result")) if m else None
        if not m or m["status"] != "done" or not isinstance(res, dict) or res.get("landed") or res.get("left_out") \
                or (res.get("batch") or {}).get("id") != rec["id"]:
            continue
        if sha := landed_sha(p, mid, since) or (landed_sha(p, mid, since, as_sha) if as_sha else ""):
            res["landed"], st = sha, "landed"
            why = f"its own commit {sha[:10]} landed with batch {rec['id']}'s branch {branch}"
        else:
            st = "left_out"
            why = (f"not landed: batch {rec['id']}'s branch {branch} landed at {str(row['head'])[:10]}"
                   f"{f' as {as_sha[:10]}' if as_sha else ''} without its own commit (the approved head left it "
                   "out); its own review is queued")
            res["left_out"] = {"head": row["head"], "push_row": row["id"], "why": why}
        db.update_task(mid, result=dump_result(res))
        out.append((mid, st, why))
    return out


def tend(p) -> list[tuple[int, str, str]]:
    """settle() every batch whose lead no longer holds it, and judge the landing of each settled one
    (landings) (reconcile_tasks, each tick)."""
    out, now = [], time.time()
    for rec in live(p.db, settled=True):
        if rec.get("settled"):
            if now - float(rec["settled"]) > KEEP_S:
                p.db.x("DELETE FROM kv WHERE key=?", (KV + str(rec["lead"]),))
            else:
                out += landings(p, rec)
            continue
        if not holds(p.db.task(rec["lead"])):
            out += settle(p, rec["lead"])
            out += landings(p, batch_of(p.db, rec["lead"]) or rec)
    return out


def prompt_block(p, lead: dict, members: list[dict], branch: str) -> str:
    """The worker prompt's batch section: each member's id, title, spec and context block."""
    from . import ctxblock
    ids = ", ".join(f"#{m['id']}" for m in members)
    lines = ["", "", f"## Batch run b{lead['id']}: you also do {ids}",
             f"This run takes task #{lead['id']} (above) and the {len(members)} small code task(s) below, in "
             f"this order, in this worktree on branch `{branch}`. Commit each task separately, with a "
             "`Task: #<id>` trailer line in its commit message (the lead's too). A task you cannot finish "
             "never fails the others: keep its commits off the branch (or revert them forward) and report it. "
             "Hand off one outcome per task, the lead included, as `members` in result.json and in any "
             "`on_pass`: [{\"id\": <id>, \"status\": \"done\"|\"failed\"|\"blocked\", \"summary\": \"...\", "
             "\"reason\": \"why, if not done\", \"followups\": [...]}]. Your own `status` is the lead's. "
             "One check run and one review cover the whole branch: its head, every member's commit. A member "
             "counts as landed only once its own commit is on the push branch."]
    for m in members:
        try:
            ctx = ctxblock.prompt_block(p, dict(m))
        except Exception:
            ctx = ""
        lines += ["", f"### Task #{m['id']}: {m['title']}", (m.get("spec") or "").strip()]
        if ctx:
            lines += [ctx.strip()]
    return "\n".join(lines) + "\n"
