# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp express <task|head> --why <text>`: one small, urgent, reviewed change lands alone, now.

The short path for a small urgent change: it does not wait for the push queue's next batch, or behind
one. eligible() decides it, model-free, and refuses with every reason that fails:

- delivery.express is not off (default on);
- an independent review approved this head (an `approved` push_queue row);
- the change is urgent: its review or the reviewed task has priority 1 or an `urgent:` line, answers a
  user's chat or owns an open user order (urgent.covers_urgent, urgent.user_ordered);
- its diff from the target is small: at most delivery.express_max_lines changed lines (MAX_LINES) in at
  most delivery.express_max_files files (MAX_FILES).

start() hands that one row to pushq.start as a one-entry batch marked "express", so it goes through the
same push path as every batch (the push branch, never --force, the leak scan, one version bump and
changeset). The batch (batch.Batch) checks beside a running batch rather than behind it, runs the smoke
push checks with each pytest check narrowed to the tests the diff reaches (testpick.narrow), judges a
failure against the unchanged tip as every batch does (one the tip shares does not block), and rebases
onto the moved tip before it pushes. After it lands, postland runs the full push checks and the
post-landing checks on it; a failure there reverts that one commit forward.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import push, pushq, urgent
from .project import Project

KEY, LINES_KEY, FILES_KEY = "express", "express_max_lines", "express_max_files"
MAX_LINES, MAX_FILES = 200, 8
PREFIX = "express-"   # the batch id of an express landing (postland runs its full checks)


def settings(d: dict) -> tuple[bool, int, int]:
    """(on, max changed lines, max files) from the delivery settings `d`."""
    on = str(d.get(KEY, True)).strip().lower() not in ("0", "false", "no", "off", "none")

    def num(k: str, dflt: int) -> int:
        try:
            return max(0, int(d.get(k, dflt)))
        except (TypeError, ValueError):
            return dflt
    return on, num(LINES_KEY, MAX_LINES), num(FILES_KEY, MAX_FILES)


def find(db, ref: str) -> dict | None:
    """The approved queue row `ref` names: a task id (the review, the reviewed task it depends on, or the
    task whose branch it is) or a head sha (7+ hex digits); the newest first."""
    from .db import dependency_ids
    ref = str(ref).strip().lstrip("#")
    for r in db.q("SELECT * FROM push_queue WHERE status='approved' ORDER BY id DESC"):
        if ref.isdigit():
            task = db.task(int(r["task"])) or {"depends_on": None}
            if int(ref) == r["task"] or str(r["branch"]).startswith(f"ttp/t{ref}-") \
                    or int(ref) in dependency_ids(task):
                return r
        elif len(ref) >= 7 and str(r["head"]).startswith(ref.lower()):
            return r
    return None


def _urgent(p: Project, row: dict) -> bool:
    from . import obligations
    from .db import dependency_ids
    db = p.db
    owners = set(obligations.owner_due(db))
    review = db.task(int(row["task"]))
    tasks = [review] + [db.task(d) for d in dependency_ids(review or {"depends_on": None}) if isinstance(d, int)]
    return urgent.covers_urgent(db, review) or any(urgent.user_ordered(t, owners) for t in tasks if t)


def size(repo: Path, tip: str, head: str) -> tuple[int, int] | None:
    """(changed lines, files) of `head` against its merge base with `tip`; None when git cannot tell."""
    r = push._git(repo, "diff", "--numstat", "--no-renames", f"{tip}...{head}")
    if r.returncode != 0:
        return None
    lines = files = 0
    for ln in r.stdout.splitlines():
        a, _, rest = ln.partition("\t")
        d, _, _path = rest.partition("\t")
        files += 1
        lines += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    return lines, files


def eligible(p: Project, ref: str, row: dict | None = None) -> tuple[dict | None, list[str]]:
    """(the approved row `ref` names, or `row` itself, the reasons it may not go express: empty when it may)."""
    d = p.config().get("delivery") or {}
    on, max_lines, max_files = settings(d)
    why = [] if on else [f"delivery.{KEY} is off"]
    row = row or find(p.db, ref)
    if row is None:
        return None, why + [f"no approved head for {ref}: an independent review must pass it first"]
    if not _urgent(p, row):
        why.append(f"task #{row['task']} is not urgent (priority 1, an `urgent:` line or a user's order)")
    remote, _, branch = str(row["target"]).partition("/")
    tip = push._fetch(p.root, remote, branch) if remote and branch else ""
    got = size(p.root, tip, row["head"]) if tip else None
    if got is None:
        why.append(f"cannot read the diff of {str(row['head'])[:10]} from {row['target']}")
    else:
        lines, files = got
        if lines > max_lines or files > max_files:
            why.append(f"{lines} changed lines in {files} files is over delivery.{LINES_KEY} {max_lines} "
                       f"or delivery.{FILES_KEY} {max_files}")
    return row, why


def start(p: Project, row: dict, why: str, now: float | None = None) -> str | None:
    """The express batch's id once it runs, or None when its row was taken meanwhile."""
    return pushq.start(p, [row], str(row["target"]), now, extra={KEY: {"why": why, "row": row["id"]}},
                       prefix=PREFIX)


def live(p: Project) -> bool:
    """Whether an express landing's process runs now: one at a time per project."""
    return any(pushq.alive(m)[0] for m in (p.state / push.DETACHED).glob(f"{PREFIX}*.json"))


AUTO_KV, AUTO_TRIES = "express_auto", 2


def auto(p: Project, name: str, log=lambda m: None, now: float | None = None) -> str | None:
    """The push queue's tick (pushq.schedule), before it starts a regular batch: an urgent approval for
    target `name` that may go express (eligible) starts alone at once, whether or not a regular batch
    runs; that batch is not waited for (its own worktree, no push lock, its own checks slot) and
    rebases onto the landed tip before it pushes. A head refused or tried AUTO_TRIES times is not looked
    at again (the kv AUTO_KV), so a refused one costs one fetch, not one per tick."""
    db = p.db
    if not settings(p.config().get("delivery") or {})[0]:
        return None
    hot = urgent.approvals_waiting(db, name)
    if not hot or live(p):
        return None
    seen = db.kv(AUTO_KV) or {}
    for qid in hot:
        row = db.one("SELECT * FROM push_queue WHERE id=?", (qid,))
        key = f"{qid}:{row['head']}"
        if not row or seen.get(key, 0) >= AUTO_TRIES:
            continue
        _, why = eligible(p, str(qid), row)
        seen[key] = AUTO_TRIES if why else seen.get(key, 0) + 1
        db.set_kv(AUTO_KV, dict(list(seen.items())[-50:]))
        if why:
            log(f"push queue: urgent approval {qid} goes with the next batch, not express: " + "; ".join(why))
            continue
        bid = start(p, row, f"urgent approval {qid}, started by the push queue", now)
        if bid:
            log(f"push queue: express {bid} lands urgent approval {qid} ({str(row['head'])[:10]}) alone, "
                "ahead of any running or queued batch")
            return bid
    return None


def wait(p: Project, bid: str, timeout_s: float, poll_s: float = 1.0) -> dict:
    """The batch's marker once its process ended (or `timeout_s` passed), applied to the queue."""
    marker = p.state / push.DETACHED / f"{bid}.json"
    end = time.time() + timeout_s
    live, m = pushq.alive(marker)
    while live and time.time() < end:
        time.sleep(poll_s)
        live, m = pushq.alive(marker)
    if not live:
        pushq.finalize(p)
    return m


def is_express(batch_id: str) -> bool:
    return str(batch_id or "").startswith(PREFIX)


def main(p: Project, ref: str, why: str, dry_run: bool = False, wait_s: float = 0.0, say=print) -> int:
    """The `ttp express` command: 0 started (or landed with a wait), 2 refused, 1 failed."""
    if not str(why or "").strip():
        say("ttp express: --why is required (what makes it urgent)")
        return 2
    row, refused = eligible(p, ref)
    if refused:
        say("ttp express: refused: " + "; ".join(refused))
        return 2
    if dry_run:
        say(f"ttp express: queue row {row['id']} ({str(row['head'])[:10]}, task #{row['task']}) may land express")
        return 0
    bid = start(p, row, why)
    if not bid:
        say(f"ttp express: queue row {row['id']} was taken by a batch meanwhile")
        return 1
    say(f"ttp express: {bid} lands {str(row['head'])[:10]} alone on {row['target']}; the full checks run after it")
    if wait_s <= 0:
        return 0
    m = wait(p, bid, wait_s)
    out = str(m.get("outcome") or "running")
    say(f"ttp express: {bid}: {out}" + (f" {m['pushed_sha'][:10]}" if out == "pushed" and m.get("pushed_sha") else "")
        + (f": {m['message']}" if m.get("message") else ""))
    return 0 if out == "pushed" else 1
