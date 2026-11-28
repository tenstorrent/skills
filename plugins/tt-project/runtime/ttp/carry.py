# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Carry tasks: one owner task carries a user's order from first commit to live, its reviews inline.

A code task carries when `enabled` says so: config `review.inline` is `ordered` (the default: the task
owns an open order, was ordered by a user message, or has an `urgent:` line), `all` (every code task) or
`off`, and the rollout flag `inline_review` is on for the project. Its phase lives in the `carry_state`
table (db.py) and every change of it is one transaction with one `carry_phase` event, the source of the
chain-time measurements. `reconcile` brings phases forward to the push queue's rows after a restart.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

from .db import dump_result, load_result

CHANGE, REVIEW, FIX, QUEUED, LANDED, DEPLOY, DONE = "change", "review", "fix", "queued", "landed", "deploy", "done"
REBASE, CHECKFIX, CAPPED = "rebase", "checkfix", "capped"   # a push conflict, failed push checks, over the rounds cap
PLAIN = "plain"   # a pass with no valid push approval: the standard review flow takes the task (pass_ends)
PHASES = (CHANGE, REVIEW, FIX, QUEUED, LANDED, DEPLOY, DONE, REBASE, CHECKFIX, CAPPED, PLAIN)
EVENT = "carry_phase"
MODES = ("ordered", "all", "off")
FIELDS = ("round", "head", "review_run", "push_row", "batch", "note")
UPDATE = "## Update (review round"   # heads each block of findings appended to the owner's spec
ROLE = "reviewer"   # runs.role of an inline review: the role of every independent review, with its guards (no edits)
# How far along the chain a phase is: reconcile only moves a task to a later one.
_RANK = {CHANGE: 0, REVIEW: 1, FIX: 1, REBASE: 1, CHECKFIX: 1, PLAIN: 1, QUEUED: 2, LANDED: 3, DEPLOY: 4, DONE: 5}


def mode(cfg: dict | None) -> str:
    got = ((cfg or {}).get("review") or {}).get("inline", "ordered")
    return got if got in MODES else "ordered"


def ordered(task: dict, db) -> bool:
    """A task urgent or user-ordered by the shared rules (urgent), of an open user order, or one a user message ordered on resources (userorder)."""
    from . import obligations, urgent, userorder
    owners = obligations.order_tasks(db)
    return (urgent.is_urgent(task) or urgent.user_ordered(task, owners)
            or task.get("id") in {o.get("task") for o in userorder.active(db).values()})


def selected(cfg: dict | None, task: dict | None, db) -> bool:
    """The config's choice alone: a code task, under `review.inline`'s mode."""
    m = mode(cfg)
    return bool(task) and task.get("kind") == "code" and m != "off" and (m == "all" or ordered(task, db))


def enabled(cfg: dict | None, task: dict | None, db, p=None) -> bool:
    """Whether `task` carries: selected, and `inline_review` on for project `p` (off without one), and not
    handed to the standard review flow (phase `plain`)."""
    from . import costlevers
    return (selected(cfg, task, db) and p is not None and costlevers.lever_on(p, "inline_review")
            and (state(db, task["id"]) or {}).get("phase") != PLAIN)


def state(db, task_id: int) -> dict | None:
    return db.one("SELECT * FROM carry_state WHERE task=?", (int(task_id),))


def advance(db, task: dict | int, phase: str, **fields) -> dict:
    """Put `task` in `phase`, with any of FIELDS, in one transaction that also writes the one `carry_phase`
    event (task, from, to, ts), a record that wakes nobody. A task with no state starts from None; the same phase with no field is no change."""
    if phase not in PHASES:
        raise ValueError(f"unknown carry phase {phase!r}")
    if bad := set(fields) - set(FIELDS):
        raise ValueError(f"unknown carry field(s): {', '.join(sorted(bad))}")
    tid = int(task["id"] if isinstance(task, dict) else task)
    with db.tx():
        old = state(db, tid)
        before = old["phase"] if old else None
        if old and before == phase and not fields:
            return old
        now = time.time()
        row = {"round": 0, **{k: (old or {}).get(k) for k in FIELDS if old and old.get(k) is not None}, **fields}
        db.x("INSERT INTO carry_state(task, phase, round, head, review_run, push_row, batch, updated, note) "
             "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(task) DO UPDATE SET phase=excluded.phase, round=excluded.round, "
             "head=excluded.head, review_run=excluded.review_run, push_row=excluded.push_row, batch=excluded.batch, "
             "updated=excluded.updated, note=excluded.note",
             (tid, phase, row["round"], row.get("head"), row.get("review_run"), row.get("push_row"),
              row.get("batch"), now, row.get("note")))
        if before != phase:
            data = {"task": tid, "from": before, "to": phase, "ts": now}
            db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, "daemon", EVENT, "low", f"carry task #{tid}: {before or 'start'} -> {phase}",
                  json.dumps(data), "handled", tid))
    return state(db, tid)


def pin_ref(task_id: int, rnd: int) -> str:
    return f"refs/ttp/review/t{task_id}/r{rnd}"


def begin_review(p, task: dict, head: str, note: str | None = None) -> None:
    """Pin `head` for round n+1 at its ref and put `task` in `review` with no run yet: the dispatcher starts
    that run next. The ref keeps the reviewed commit whatever the branch does meanwhile."""
    from . import worktree
    old = state(p.db, task["id"])
    rnd = int(old["round"] or 0) + 1 if old else 1
    worktree._git(p.root, "update-ref", pin_ref(task["id"], rnd), head)
    advance(p.db, task, REVIEW, round=rnd, head=head, review_run=None, note=note)


def review_due(db, task: dict) -> dict | None:
    """The state of a queued carry task whose next run is its review (a run that ended with no verdict is run again), else None."""
    s = state(db, task["id"])
    return s if s and s["phase"] == REVIEW and s["head"] and task.get("status") == "queued" else None


def review_tree(p, task: dict, s: dict) -> str:
    """A detached worktree of the review's own at the pinned ref (made afresh), never the author's."""
    from . import worktree
    path = p.worktrees / f"t{task['id']}-review"
    drop_tree(p, task)
    worktree._git(p.root, "worktree", "add", "--detach", str(path), pin_ref(task["id"], int(s["round"])))
    return str(path)


def drop_tree(p, task: dict) -> None:
    from . import worktree
    if (path := p.worktrees / f"t{task['id']}-review").exists():
        worktree.remove_path(p.root, path, force=True)


def inline_review(daemon, task: dict, summary: str) -> bool:
    """Put `task` in `review` for a review run of its own, in place of a `Review #N` task: its prompt, from the builder
    every review uses (daemon._auto_review), is kept in the carry state and its head pinned. False when there is nothing
    to review (the old flow's no-review cases)."""
    got = daemon._auto_review(task, summary, spec_only=True)
    if not isinstance(got, dict):
        return False
    earlier = re.findall(r"^" + re.escape(UPDATE) + r".*?(?=^## |\Z)", task.get("spec") or "", re.M | re.S)
    if earlier:   # a re-review: the reviewer checks each earlier finding
        got["spec"] += "\n\nEarlier findings of this change; check that each is fixed:\n" + "\n".join(x.strip() for x in earlier)
    begin_review(daemon.p, task, got["head"], note=json.dumps({k: got[k] for k in ("title", "spec", "tier", "labels")}))
    return True


def review_view(task: dict, s: dict) -> dict:
    """`task` as its review run sees it: a review task with the stored prompt, so the review's routing, tier, prompt and
    role come out of the ordinary dispatch. Only its run is new: the row, id, branch and budget stay the task's."""
    got = json.loads(s["note"] or "{}")
    return dict(task, kind="review", spec=got.get("spec", ""), tier=got.get("tier") or "standard", title=got.get("title") or task["title"],
                labels=json.dumps(got.get("labels") or []), attempts=0)


def _authors(db, tid: int) -> list[dict]:
    """The task's own runs, newest first: every one that is not an inline review (or a coordinator's)."""
    return db.q("SELECT * FROM runs WHERE task=? AND role NOT IN (?, 'coordinator') ORDER BY id DESC", (tid, ROLE))


def session_of(run: dict) -> str:
    return str(run.get("session_id") or json.loads(run.get("note") or "{}").get("session_id") or "")


def check_approval(daemon, task: dict, session: str, entries, cfg: dict) -> dict:
    """pushq.check_approval for the push list of carry `task`'s inline review (run in `session`), once the review
    is independent: its session is none of the author's runs' and the branch is still at the pinned head (the
    reviewer committed nothing, nobody else did). {"invalid": why} otherwise: the review runs again."""
    from . import pushq
    s = state(daemon.p.db, task["id"])
    if not s or s["phase"] != REVIEW:
        return {"invalid": "its task is no longer in review"}
    if not session or session in {session_of(a) for a in _authors(daemon.p.db, task["id"])}:
        return {"invalid": "its review ran in the author's own session (or has none), not an independent one"}
    if (now := daemon._branch_head(task)) != s["head"]:
        return {"invalid": f"the branch is at {str(now)[:12] or 'no commit'}, not the reviewed {str(s['head'])[:12]}: "
                           "it moved during the review"}
    # What the review's spec says it reviewed ("branch at head", as a Review task's does) is what pushq may approve.
    named = dict(task, depends_on="[]", spec=f"{task.get('spec') or ''}\nReviewed: {task.get('branch')} at {s['head']}")
    return pushq.check_approval(daemon.p, named, entries, cfg=cfg)


def _findings(result: dict) -> str:
    fups = [f for f in result.get("followups") or [] if isinstance(f, dict)]
    return "\n".join([str(result.get("summary") or "")] + [f"- {f.get('title')}: {f.get('spec')}" for f in fups])[:6000]


def pass_ends(daemon, task: dict, why: str = "") -> str:
    """An inline review passed with no valid push approval. The carry ends (phase done, True) only when the change
    needs no landing: no push branch, or a change kept off it ("done"). Else it fails closed: phase `plain`, one
    `carry_fallback` event, and the task's new status is "review": the daemon's Review #N backs it."""
    from . import landed, push, pushq, worktree
    p, tid = daemon.p, task["id"]
    d = daemon.cfg.get("delivery") or {}
    if not landed.has_push_branch(p) or push.kept_off(task, worktree.diff_lines(p, [task["branch"]]), d, p):
        advance(p.db, tid, DONE)
        return "done"
    advance(p.db, tid, PLAIN)
    pushq._event(p.db, task, "carry_fallback", f"#{tid} {task['title']}: its inline review passed with no valid push "
                 f"approval ({why or 'none'}); it goes to the standard review", queued=False, severity="low")
    return "review"


def failed(daemon, task: dict, result: dict, run_dir) -> tuple[str, dict]:
    """Carry `task`'s inline review failed or asked for changes. Within `review.auto_fix_rounds` + 1 review runs: phase
    fix, the findings and this run's result.json appended to the spec, the owner woken (its author session resumed
    where the provider allows, else a fresh run from the spec): (queued, result fields). Past them: phase capped, a
    `carry_capped` event to the coordinator and the task in `review`. Only the approval's form failed (nothing for the
    author to fix): phase plain and the task in `review`, for the standard review. A real failed review is counted by
    reviewcap (a `task_changes_needed` event); a form-only failure is not."""
    from . import pushq
    from .daemon import AUTO_FIX_ROUNDS, HANDOFF_RESUME_KEY
    db, tid = daemon.p.db, task["id"]
    s = state(db, tid)
    if not s or s["phase"] != REVIEW:
        return "failed", {}
    path, text = Path(run_dir) / "result.json", _findings(result)
    cap = int((daemon.cfg.get("review") or {}).get("auto_fix_rounds", AUTO_FIX_ROUNDS)) + 1
    if result.get("review_failure") in ("approval", "marker"):   # nothing for the author to fix: the standard review takes it
        advance(db, tid, PLAIN, note=str(path))
        pushq._event(db, task, "carry_fallback", f"#{tid} {task['title']}: its inline review's push approval failed "
                     f"({text.splitlines()[0] if text else ''}); it goes to the standard review", queued=False, severity="low")
        return "review", {}
    pushq._event(db, task, "task_changes_needed", f"#{tid} {task['title']}: inline review round {s['round']} failed: "
                 f"{text.splitlines()[0] if text else ''}", queued=False, severity="low")
    if int(s["round"]) >= cap:
        advance(db, tid, CAPPED, note=str(path))
        pushq._event(db, task, "carry_capped", f"#{tid} {task['title']}: its inline review failed {s['round']} time(s); "
                     f"findings in {path}. Decide: scope it down, redesign it or accept the gaps", queued=True, severity="high")
        return "review", {}
    woke = f"its review (round {s['round']}) failed; findings in the last Update block of your spec and in {path}. Fix them on your own branch, then hand off done"
    db.update_task(tid, spec=f"{task['spec'] or ''}\n\n{UPDATE} {s['round']}: findings)\n{text}\nReview result: {path}")
    advance(db, tid, FIX)
    extra = {"woke": woke}
    if (last := next(iter(_authors(db, tid)), None)) and last.get("dir") and (
            back := daemon._handoff_resume(last, task, SimpleNamespace(session_id=session_of(last)), Path(last["dir"]), woke)):
        extra[HANDOFF_RESUME_KEY] = back
    return "queued", extra


def reconcile(db) -> list[tuple[int, str, str]]:
    """On daemon start: move each carry task's phase forward to its newest push row (approved or batched:
    queued; pushed or landed: landed) and, once that row's batch ended its deploy step well (ok or skipped), to done. Returns
    (task, from, to) for each move. A task in `capped` is the coordinator's and stays; one in `plain` is the standard review's."""
    from .pushq import LANDED_OUTCOMES, OPEN_ROWS
    moved = []
    for s in db.q("SELECT task, phase FROM carry_state WHERE phase NOT IN (?,?,?)", (DONE, CAPPED, PLAIN)):
        row = db.one("SELECT q.id, q.status, q.batch, q.head, b.after_push FROM push_queue q "
                     "LEFT JOIN push_batches b ON b.id=q.batch WHERE q.task=? ORDER BY q.id DESC LIMIT 1", (s["task"],))
        if not row:
            continue
        landed = row["status"] in LANDED_OUTCOMES
        want = (DONE if landed and row["after_push"] in ("ok", "skipped") else LANDED if landed
                else QUEUED if row["status"] in OPEN_ROWS and row["status"] != "held" else None)
        if want and _RANK[want] > _RANK[s["phase"]]:
            advance(db, s["task"], want, push_row=row["id"], batch=row["batch"], head=row["head"])
            moved.append((s["task"], s["phase"], want))
    return moved


def settle_kind(states: set[str], trim) -> str | None:
    """How one approval's row `states` settle a carry task: checkfix, rebase or landed; None leaves it to
    the review flow's own handling (a trim, a refusal, rows still waiting)."""
    from .pushq import LANDED_OUTCOMES
    if trim or "refused" in states:
        return None
    return (CHECKFIX if "check_failed" in states else REBASE if "conflict" in states
            else LANDED if states <= set(LANDED_OUTCOMES) else None)


def _approvals(db, tid: int) -> tuple[list[dict], list[dict]]:
    """A task's push rows and, of them, its newest approval's (the rows sharing one `created`)."""
    rows = db.q("SELECT * FROM push_queue WHERE task=? ORDER BY id", (tid,))
    return rows, [r for r in rows if r["created"] == rows[-1]["created"]]


def _land_done(p, task: dict, b: dict, m: dict, now: float) -> None:
    """The change is live: done as a landed review is (its hand-off, events and reply), phase `done`."""
    from . import pushq
    rows, current = _approvals(p.db, task["id"])
    if task["status"] != "pushing" or not any(r["status"] in pushq.LANDED_OUTCOMES for r in current):
        return
    prev = load_result(task["result"])
    with p.db.tx():
        pushq._settle_landed(p, task, b, m, prev, str(prev.get("summary") or ""), b["target"], rows, current, now)
        advance(p.db, task, DONE, batch=b["id"])


def _owner_progress(p, tid: int, evidence: str) -> None:
    """`evidence` as progress on each open obligation task `tid` owns (no receipt: its owner or the coordinator closes it)."""
    from . import obligations
    for key in obligations.owned_open(p.db, tid):
        obligations.record(p, key, "progress", evidence, tid)


def on_settle(p, task: dict, kind: str, b: dict, m: dict, now: float, deploy: bool) -> None:
    """The push queue settled carry task `task` (`settle_kind`'s `kind`) in batch `b`. landed: phase `landed`,
    an owned obligation gets the sha as progress, then `deploy` (the batch's after_push follows) or done.
    rebase and checkfix: the owner is woken to fix it in place (the log's path and tail with it); past its
    attempts it goes `capped` and failed, to the coordinator."""
    from . import pushq
    db, tid, target = p.db, task["id"], b["target"]
    rows, current = _approvals(db, tid)
    hit = pushq.LANDED_OUTCOMES if kind == LANDED else ("conflict",) if kind == REBASE else ("check_failed",)
    row = next(r for r in reversed(current) if r["status"] in hit)
    at = {"push_row": row["id"], "batch": b["id"], "head": row["head"]}
    if kind == LANDED:
        _owner_progress(p, tid, f"landed {pushq._short(row['pushed_sha'])} on {target} (batch {b['id']})")
        advance(db, tid, LANDED, **at)
        if deploy:
            advance(db, tid, DEPLOY)
        else:
            _land_done(p, task, b, m, now)
        return
    sent = pushq._approvals_sent(db, tid, "conflict" if kind == REBASE else "check_failed")
    if kind == REBASE:
        woke = pushq._conflict_woke(m, target, row, current) + "; rebase it in place on its own branch"
    else:
        d = pushq._detail(row)
        woke = (f"the push checks failed on {row['branch'] or '?'} at {pushq._short(row['head'])} in batch {b['id']}: "
                f"`{d.get('cmd') or 'the push checks'}`; log: {m.get('log') or '?'}\n{pushq._tail_of(m, d)}\n"
                "fix it in place; its delta is reviewed again")
    if sent > int(task["max_attempts"] or pushq.DEFAULT_MAX_ATTEMPTS):
        text = f"{woke}. It keeps failing its push after {sent - 1} fixes"
        db.update_task(tid, status="failed", blocked_reason=None,
                       result=dump_result({**load_result(task["result"]), "status": "failed", "summary": text[:1500]}))
        advance(db, tid, CAPPED, **at, note=kind)
        pushq._event(db, task, "task_failed", f"#{tid} {task['title']} -> failed: {text}", queued=True)
        return pushq._reply(db, task, "failed", text)
    advance(db, tid, kind, **at)
    pushq._requeue(db, task, woke)
    pushq._event(db, task, "task_requeued", f"#{tid} {task['title']}: {woke.splitlines()[0]}", queued=False)


def on_after(p, b: dict, m: dict, status: str, line: str, tail: str, now: float) -> None:
    """The batch's after_push ended with `status`, `line` in words and `tail` the end of its log. Each carry
    task that landed in the batch: ok, skipped or superseded finishes it, `line` the obligation's progress;
    a failure raises `carry_deploy_failed` to the coordinator and wakes the owner only if the log names a
    file its landed commit touched, else blocks the task for the coordinator."""
    from . import pushq
    db = p.db
    for s in db.q("SELECT task FROM carry_state WHERE batch=? AND phase IN (?,?)", (b["id"], LANDED, DEPLOY)):
        task = db.task(s["task"])
        if not task or task["status"] != "pushing":
            continue
        if status in ("ok", "skipped", "superseded"):
            _owner_progress(p, task["id"], line)
            _land_done(p, task, b, m, now)
            continue
        head = next((r["head"] for r in _approvals(db, task["id"])[1] if r["batch"] == b["id"]), None)
        named = [f for f in sorted(pushq._changed(p, m["tip"], head) or ()) if f in tail] if head and m.get("tip") else []
        pushq._event(db, task, "carry_deploy_failed", f"#{task['id']} {task['title']}: {line}"
                     + (f"; the log names {', '.join(named[:10])} from its change" if named else ""), queued=True)
        if named:
            pushq._requeue(db, task, f"{line}; the log names {', '.join(named[:10])} from your change:\n{tail}")
        else:   # no longer a push in flight (drain, host cycles and sweeps count `pushing`): the coordinator's
            db.update_task(task["id"], status="blocked", blocked_reason=line[:500])


def show_line(db, task_id: int) -> str:
    """`carry: <phase> round <n>` for `ttp task show`, "" for a task with no state."""
    s = state(db, task_id)
    return f"carry: {s['phase']} round {s['round']}" if s else ""


def _sched_minutes(p, since: float) -> dict[str, float]:
    """Minutes queued tasks waited per held reason since `since`: each scheduler.jsonl line's count x the
    gap to the next line (capped at 15 min). The log is not split by ordered work."""
    from . import schedlog
    rows = []
    for f in (schedlog.path(p).with_name(schedlog.OLD), schedlog.path(p)):
        try:
            rows += [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
        except (OSError, ValueError):
            pass
    ts = [dt.datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp() for r in rows]
    out = {"depends_on": 0.0, "carry": 0.0}
    for r, t, nxt in zip(rows, ts, ts[1:] + [time.time()]):
        if t >= since:
            for k in out:
                out[k] += (r.get("held") or {}).get(k, 0) * min(max(nxt - t, 0), 900) / 60
    return out


def report(p, days: float = 1) -> list[str]:
    """`ttp carry report`: model-free lines for the last `days`. Chain mode: minutes tasks waited on an
    unfinished depends_on. Carry mode: minutes carry tasks waited, and for each order that reached done
    the lead time (first carry_phase to done) and the minutes in the push queue, landed and deploy
    phases, where no run is alive."""
    since = time.time() - days * 86400
    held = _sched_minutes(p, since)
    first, last, wait = {}, {}, {}
    for e in p.db.q("SELECT data FROM events WHERE kind=? AND ts>=? ORDER BY ts", (EVENT, since - 30 * 86400)):
        d = json.loads(e["data"])
        t = d["task"]
        if t in last and last[t][1] in (QUEUED, LANDED, DEPLOY):
            wait[t] = wait.get(t, 0) + d["ts"] - last[t][0]
        first.setdefault(t, d["ts"])
        last[t] = (d["ts"], d["to"])
    done = [t for t, (ts, to) in last.items() if to == DONE and ts >= since]
    lead = [(last[t][0] - first[t]) / 60 for t in done]
    med = lambda xs: f"{statistics.median(xs):.0f}" if xs else "-"
    return [f"last {days:g} d: tasks waited {held['depends_on']:.0f} min on depends_on (all work: the log does not "
            f"split ordered work) and {held['carry']:.0f} min as carry tasks",
            f"carry orders done: {len(done)}, median lead time {med(lead)} min, median push-queue/landed/deploy "
            f"wait {med([wait.get(t, 0) / 60 for t in done])} min"]
