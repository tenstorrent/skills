# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Superseded reviews: a review that failed or asked for changes (stored 'failed') whose findings a
later passing review or a reviewed commit covers.

Automatic (`auto`, model-free, at a review's passing hand-off): every failed review earlier in the
passing review's chain is marked superseded by it, with the head it approved. The chain runs back
through `continues:<review>` labels and through the reviewed task's `review_fix:<review>` or
`continues:<review>` labels (a fix of a failed review and its re-review).

By hand (`manual`, `ttp task <project> superseded <id> [<sha>] [--by-review <id>] [--ref <ref>]`):
evidence is a passed review of the same chain (`--by-review`), or a commit reachable from the push
branch or `--ref`. Without evidence it is refused and names both forms.

The record is the hand-off's SUPERSEDED_KEY: the verdict (status, summary, findings) is kept as it
was; task_outcome shows the review as `superseded`, so the daily review, the blocker sweep and the
open changes_needed lists leave it out. Recording twice changes nothing."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .db import SUPERSEDED_KEY, dump_result, load_result, review_subject

PASSED = ("done", "pushing")           # review statuses that approve (pushing: approved, in the push queue)


def record_of(task: dict) -> dict | None:
    """The supersede record of a failed review, None when it has none."""
    rec = load_result(task.get("result")).get(SUPERSEDED_KEY)
    return rec if isinstance(rec, dict) else None


def _labels(task: dict) -> list[str]:
    try:
        vals = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return []
    return [v for v in vals if isinstance(v, str)] if isinstance(vals, list) else []


def _ids(task: dict, prefixes: tuple[str, ...]) -> list[int]:
    return [int(v.partition(":")[2]) for v in _labels(task)
            if v.startswith(prefixes) and v.partition(":")[2].isdigit()]


def earlier(db, review: dict) -> list[int]:
    """Ids of the reviews before `review` in its chain, nearest first (each once)."""
    out, seen, todo = [], {review["id"]}, [review]
    while todo:
        cur = todo.pop(0)
        back = _ids(cur, ("continues:",))
        if (subject := review_subject(cur)) is not None and (fix := db.task(subject)):
            back += _ids(fix, ("review_fix:", "continues:"))
        for rid in back:
            if rid in seen:
                continue
            seen.add(rid)
            prev = db.task(rid)
            if prev and prev.get("kind") == "review":
                out.append(rid)
                todo.append(prev)
    return out


def passed(task: dict | None) -> bool:
    """A review that approved its change."""
    return bool(task) and task.get("kind") == "review" and task.get("status") in PASSED


def _record(db, tid: int, rec: dict) -> bool:
    """Write `rec` on failed review `tid` unless it has one: True when written."""
    with db.tx():
        t = db.task(tid)
        if not t or t.get("kind") != "review" or t.get("status") != "failed" or record_of(t):
            return False
        result = {**load_result(t.get("result")), SUPERSEDED_KEY: rec}
        return bool(db.conn.execute("UPDATE tasks SET result=? WHERE id=? AND status='failed'",
                                    (dump_result(result), tid)).rowcount)


def _head(db, review_id: int) -> str:
    r = db.one("SELECT head FROM review_stats WHERE task=?", (review_id,))
    return (r["head"] or "") if r else ""


def auto(db, review: dict) -> list[int]:
    """Mark each failed review earlier in passing `review`'s chain superseded by it: the ids marked."""
    if not passed(review):
        return []
    sha, now = _head(db, review["id"]), time.time()
    return [rid for rid in earlier(db, review)
            if _record(db, rid, {"by_review": review["id"], "sha": sha, "ref": None, "how": "auto", "ts": now})]


def _reachable(p, sha: str, ref: str | None, repo: str | None) -> tuple[str, str]:
    """(full sha, what check said) when `sha` is on `ref` (default: the push branch); ValueError says why not."""
    from . import landed
    repos = [Path(repo).expanduser()] if repo else []
    top = landed._git(Path.cwd(), "rev-parse", "--show-toplevel")   # the repository `ttp` runs in
    if top.returncode == 0 and top.stdout.strip():
        repos.append(Path(top.stdout.strip()))
    repos = list(dict.fromkeys([*repos, p.root]))
    where = next((r for r in repos if landed._commit(r, sha)), None)
    if where is None:
        raise ValueError(f"not recorded: no commit {sha} in {', '.join(map(str, repos))}")
    full = landed._commit(where, sha)
    rc, said = landed.check(p, where, full, onto=ref)
    if rc != 0:
        raise ValueError(f"not recorded: {said} in {where}; name a commit on "
                         f"{ref or 'the push branch'} (or --ref <ref> that holds it)")
    return full, said


def manual(p, tid: int, sha: str = "", ref: str | None = None, by_review: int | None = None,
           repo: str | None = None) -> str:
    """Record failed review `tid` as superseded, on evidence: `by_review` (a passed review of the same
    chain) or `sha` reachable from `ref` (default: the push branch). The words to print; ValueError
    when refused."""
    db = p.db
    t = db.task(tid)
    if not t or t.get("kind") != "review" or t.get("status") != "failed":
        raise ValueError(f"task #{tid} is not a failed review")
    if rec := record_of(t):
        return f"task #{tid}: already recorded as superseded ({words(rec)})"
    if by_review is None and not sha:
        raise ValueError(f"task #{tid} is a failed review: give evidence that its findings are fixed, either "
                         f"`ttp task {p.name} superseded {tid} --by-review <passed re-review id>` or "
                         f"`ttp task {p.name} superseded {tid} <covering commit> [--ref <ref>]`")
    rec = {"by_review": None, "sha": "", "ref": ref, "how": "manual", "ts": time.time()}
    if by_review is not None:
        other = db.task(by_review)
        if not passed(other):
            raise ValueError(f"not recorded: #{by_review} is not a passed review"
                             + (f" (it is {other['kind']}, {other['status']})" if other else ""))
        if tid not in earlier(db, other):
            raise ValueError(f"not recorded: review #{by_review} is not a later review of #{tid}'s change chain")
        rec.update(by_review=by_review, sha=_head(db, by_review))
    if sha:
        rec["sha"] = _reachable(p, sha, ref, repo)[0]
    if not _record(db, tid, rec):
        t = db.task(tid) or {}
        return f"task #{tid}: already recorded as superseded ({words(record_of(t) or rec)})"
    return f"task #{tid}: review recorded as superseded ({words(rec)}); its verdict is kept"


def words(rec: dict) -> str:
    parts = []
    if rec.get("by_review"):
        parts.append(f"by review #{rec['by_review']}")
    if rec.get("sha"):
        parts.append(f"commit {str(rec['sha'])[:10]}" + (f" on {rec['ref']}" if rec.get("ref") else ""))
    return ", ".join(parts) + (f", {rec.get('how')}" if rec.get("how") else "")


def show_line(task: dict, fmt=None) -> str:
    """`ttp task show`'s line for a superseded review, '' for any other task."""
    rec = record_of(task) if task.get("kind") == "review" and task.get("status") == "failed" else None
    if not rec:
        return ""
    when = fmt(rec["ts"]) if fmt and isinstance(rec.get("ts"), (int, float)) else ""
    return f"review superseded {words(rec)}" + (f", recorded {when}" if when else "")
