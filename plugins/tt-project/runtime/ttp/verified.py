"""Verified-through markers: how far a task's lineage is already verified, so a continuation checks only
what came after.

A hop's `ttp checks` pass records `checks` (the head, its tree and the commands that passed there) for its
task; a passing review records `review` (the head it accepted) for the code tasks it covers. Both live in
the kv table under `verified_through:<task>`. A task that continues another (`continues:<id>`, starting
from its branch) inherits the nearest marker of its lineage: the tasks it continues and, for a review, the
tasks it reviews. While that marker is an ancestor of HEAD, the worker's instructions, the review spec and
`ttp checks` (its `if_changed` checks) verify only the commits after it. When it is not (a rebase or any
other rewrite), they fall back to full verification and say so.

The marker only skips per-hop re-verification. `ttp push`, `ttp push --own` and the push queue never read
it: they run the full checks on the exact commit they push."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from . import gitmemo
from .db import DB, continues_id, dependency_ids

KEY = "verified_through:"
KINDS = ("checks", "review")
MAX_HOPS = 20   # a lineage walk stops here (and at a cycle)


def record(db: DB, task: int | str, kind: str, sha: str, **extra) -> None:
    """Store `kind`'s marker for `task` at `sha` (with `extra`, e.g. tree and commands), keeping the other kind."""
    if kind not in KINDS or not str(task).isdigit() or not sha:
        return
    cur = db.kv(f"{KEY}{int(task)}", {})
    cur = cur if isinstance(cur, dict) else {}
    cur[kind] = {"sha": sha, "ts": time.time(), **extra}
    db.set_kv(f"{KEY}{int(task)}", cur)


def own(db: DB, task: int | str, kind: str | None = None) -> dict | None:
    """`task`'s own marker of `kind` (either kind, the newer, when None), with `task` and `kind` added."""
    cur = db.kv(f"{KEY}{int(task)}", {}) if str(task).isdigit() else {}
    found = [{**cur[k], "task": int(task), "kind": k} for k in ((kind,) if kind else KINDS)
             if isinstance(cur, dict) and isinstance(cur.get(k), dict) and cur[k].get("sha")]
    return max(found, key=lambda m: float(m.get("ts") or 0)) if found else None


def _reviewed_ids(t: dict) -> list[int]:
    try:
        labels = json.loads(t.get("labels") or "[]")
    except ValueError:
        labels = []
    ids = [i for i in dependency_ids(t) if i is not None] if t.get("depends_on") else []
    ids += [int(x[12:]) for x in labels if isinstance(x, str) and x.startswith("auto_review:") and x[12:].isdigit()]
    return list(dict.fromkeys(ids))


def _next(t: dict) -> list[int]:
    return (_reviewed_ids(t) if t.get("kind") == "review" else []) + \
        ([c] if (c := continues_id({"labels": t.get("labels")})) is not None else [])


def lineage(db: DB, task: dict, include_self: bool = False) -> list[int]:
    """The tasks `task` builds on, nearest first: the ones it continues (`continues:`), and for a review
    among them, the tasks it reviews. `include_self` puts `task` itself (and what it reviews) first."""
    me = int(task["id"]) if str(task.get("id") or "").isdigit() else None
    out: list[int] = []
    queue = [me] if include_self and me is not None else _next(task)
    while queue and len(out) < MAX_HOPS:
        tid = queue.pop(0)
        if tid in out or (tid == me and not include_self):
            continue
        out.append(tid)
        t = task if tid == me else db.task(tid)
        if t:
            queue += [n for n in _next(t) if n not in out]
    return out


def inherited(db: DB, task: dict, kind: str | None = None, include_self: bool = False) -> dict | None:
    """The nearest marker of `kind` (either when None) in `task`'s lineage, or None."""
    for tid in lineage(db, task, include_self):
        if m := own(db, tid, kind):
            return m
    return None


def is_ancestor(repo: str | Path, sha: str, head: str) -> bool:
    try:
        return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, head],
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def scope(db: DB, task: dict, repo: str | Path, head: str, kind: str | None = None,
          include_self: bool = False) -> dict | None:
    """The inherited marker as it applies to `head` in `repo`: the marker plus `ok` (still an ancestor
    of `head`, so only the commits after it need verifying) and `line` (what to tell the reader). None
    when the lineage has none."""
    m = inherited(db, task, kind, include_self)
    if not m or not head:
        return None
    sha, who = str(m["sha"]), f"#{m['task']}'s {'review' if m['kind'] == 'review' else 'checks'}"
    if sha == head or is_ancestor(repo, sha, head):
        return {**m, "ok": True, "line": f"verified through {sha[:12]} ({who})"}
    return {**m, "ok": False, "line": f"{who} verified {sha[:12]}, which is no longer an ancestor of {head[:12]} "
                                      f"(history was rewritten): full verification"}


def changed_since(repo: str | Path, sha: str, head: str) -> list[str] | None:
    """The files the commits after `sha` up to `head` change, or None when git cannot tell."""
    try:
        r = subprocess.run(["git", "-C", str(repo), "-c", "core.quotePath=false", "diff", "--name-only",
                            "--no-renames", f"{sha}..{head}"], capture_output=True, text=True,
                           timeout=gitmemo.timeout(120))
    except (OSError, subprocess.SubprocessError):
        return None
    return [f for f in r.stdout.splitlines() if f] if r.returncode == 0 else None


def handoff_line(result: dict) -> str:
    """The hand-off's marker for a digest line: ` · verified through <sha12>`, or ""."""
    sha = str((result or {}).get("verified_through") or "")
    return f" · verified through {sha[:12]}" if sha else ""
