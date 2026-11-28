# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The size and lean-code checks never stop a tested landing.

Once `ttp checks` recorded a candidate pass on exactly the head being judged (by any run of the task
or the tasks it reviews, prguard.task_record), the change-size rule
(ponytail.size_problem, the PR size budget) and the ponytail lane's verdict no longer refuse `ttp push`,
the push queue's approval or the review: defer adds one follow-up task per change and kind instead (a size
problem: explain or split it; a lane finding: fix it), started once the change lands, and the change goes
on. Without such a pass both still refuse as before. The correctness review still gates landing."""
from __future__ import annotations

import time

KIND = "landgate_followup"


def _tasks(p, task) -> list[dict]:
    from . import push
    row = task if isinstance(task, dict) else push._task_row(p, task)
    return [row, *push._reviewed_tasks(p, str(row["id"]))] if row else []


def checked(p, tasks: list[dict], head: str) -> dict | None:
    """The candidate `ttp checks` pass on exactly `head` recorded by any run of `tasks`, or None."""
    from . import prguard
    head = head and (prguard._git_out(str(p.root), "rev-parse", "--verify", "-q", f"{head}^{{commit}}") or head)
    return next((r for t in tasks if head and (r := prguard.task_record(p, t["id"], head))
                 and r.get("passed") is True), None)


def defer(p, task, head: str, why: str, land: bool = True, say=None) -> int | None:
    """The follow-up task that replaces refusal `why` of `task`'s head `head` (added once per change and
    kind; with `land`, it starts once the change lands), or None: no checks pass on that head."""
    from . import ponytail
    tasks = _tasks(p, task)
    if not tasks or not checked(p, tasks, head):
        return None
    change = next((t for t in tasks[1:] if t.get("kind") in ("code", "work")), tasks[0])
    cid, size = change["id"], ponytail.FAILED not in why and "lines" in why
    kind = "size" if size else "lean"
    mark = f"{KIND}:{kind}:{cid}"
    old = p.db.one("SELECT id FROM tasks WHERE labels LIKE ? AND status!='cancelled' ORDER BY id DESC LIMIT 1",
                   (f'%"{mark}"%',))
    if old:
        fid = old["id"]
    else:
        title = (f"Explain or split #{cid}: it went on over the size rule" if size
                 else f"Fix the ponytail lane's findings on #{cid}")
        spec = (f"Task #{cid}'s head {head[:12]} passed its checks, so this did not stop it landing: {why}\n"
                + ("Record why it is this size (the `no_split` field of your hand-off), or split what is left of "
                   "it into smaller changes." if size else
                   "Fix each finding that still holds on the landed code; one that a quoted user order in the "
                   "task's spec asks for is in scope: close it as such.") + f"\nContext: follow-up of task #{cid}.")
        labels = [mark] + ([f"start_when:landed:#{cid}", f"start_why:task #{cid} lands",
                            f"deferred_since:{time.time()}"] if land else [])
        fid = p.db.add_task(title, spec, kind="code", tier="standard", priority=3, origin="daemon", labels=labels)
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
           (time.time(), "daemon", KIND, "low", f"#{cid} head {head[:12]} passed its checks: not refused ({why[:300]}); "
            f"follow-up task #{fid}", "handled", cid))
    if say:
        say(f"ttp push: not refused, its checks passed on {head[:12]}: {why[:300]}. Follow-up task #{fid} covers it")
    return fid


def review_kept(p, task: dict, run_dir, result: dict, why: str, fix: list[str]) -> dict | None:
    """Review `task`'s done hand-off kept despite the lane's or size rule's `why`, or None (nothing to fix
    in the change, or no checks pass on the head the lane read)."""
    from . import ponytail
    head = str((ponytail.read(run_dir) or {}).get("head") or "") or next(
        (f"refs/heads/{t['branch']}" for t in _tasks(p, task)[1:] if t.get("branch")), "")
    if not fix or not (fid := defer(p, task, head, why)):
        return None
    return dict(result, ponytail_followup=fid, summary=(f"{result.get('summary') or ''}\nPonytail lane (not blocking, "
                                                        f"checks passed on {head[:12]}): {why}; follow-up task #{fid}")[:4000])
