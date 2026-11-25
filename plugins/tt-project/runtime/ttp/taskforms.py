# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""task_add and task_update forms with one clear reading (actionforms), applied as meant instead of
refused (#9824): each returns (rewritten action, note) or None.

task_add: a missing title is the spec's first line; a depends_on id below 1 is dropped; a resource
name is sanitised to the allowed characters; a continue of a running or queued task is a spec update
of it; an open task of the same title takes the spec as its update (unless it continues another); a start_when waiting on the task
it continues (it could never pass) drops that wait. task_update: `waits_on ask:new` with no ask_user
in the batch is dropped; a branch or deferral change on a started task is a new task that continues it
(after it ends, for a running one)."""
from __future__ import annotations

import re

from .db import TERMINAL_TASK_STATES

STEER = ("running", "queued")   # a task_add continuing one is a spec update of it
DEFER = ("start_after", "start_when")


def _int(v):
    try:
        return None if isinstance(v, bool) else int(v)
    except (TypeError, ValueError):
        return None


def _resources(a: dict) -> tuple[dict, str] | None:
    from .coordinator import RESOURCE_RE, push_lock_name
    raw = a.get("resources")
    if not isinstance(raw, list):
        return None
    fixed = [r if not isinstance(r, str) or RESOURCE_RE.fullmatch(r) or r.startswith("push:") or push_lock_name(r)
             else re.sub(r"[^A-Za-z0-9_.@+-]+", "-", r).lstrip("_.@+-")[:80] for r in raw]
    return ({**a, "resources": fixed}, f"resources={fixed} (was: {raw})") if fixed != raw else None


def _open_titled(db, title: str):
    return db.one(f"SELECT id, status FROM tasks WHERE title=? AND status NOT IN "
                  f"({','.join('?' * len(TERMINAL_TASK_STATES))}) ORDER BY id DESC", (title, *TERMINAL_TASK_STATES))


def _add(p, a: dict) -> tuple[dict, list[str]] | tuple[dict, str]:
    from . import chaincap
    db, was = p.db, []
    spec = str(a.get("spec") or "").strip()
    if not str(a.get("title") or "").strip() and spec:
        a["title"] = next(x for x in spec.splitlines() if x.strip()).strip().lstrip("#-* ").strip()[:120]
        was.append(f"title={a['title']!r}, the spec's first line (was: no title)")
    if isinstance(a.get("depends_on"), list):
        keep = [d for d in a["depends_on"] if (_int(d) or 0) > 0 or _int(d) is None]
        if keep != a["depends_on"]:
            was.append(f"depends_on={keep} (was: {a['depends_on']}: no task id is below 1)")
            a["depends_on"] = keep
    if got := _resources(a):
        a = got[0]
        was.append(got[1])
    prev = _int(a.get("continues"))
    old = db.task(prev) if prev is not None else None
    if old and old["status"] in STEER and spec:
        return {"type": "task_update", "id": old["id"], "spec": spec}, \
            [*was, f"task_update #{old['id']} with its spec (was: a task_add continuing #{old['id']}, which is " \
                   f"{old['status']}: it takes the new spec as its update)"]
    dup = _open_titled(db, str(a.get("title") or "").strip())
    if dup and prev is None and spec:   # with `continues` the meaning is the continue, not this title
        return {"type": "task_update", "id": dup["id"], "spec": spec}, \
            [*was, f"task_update #{dup['id']} with its spec (was: a task_add duplicating open task #{dup['id']})"]
    if old:
        a = _self_wait(p, a, [t["id"] for t in chaincap.chain(db, old["id"])] or [old["id"]], was)
    return a, was


def _self_wait(p, a: dict, lineage: list[int], was: list[str]) -> dict:
    """`a` without the start_when parts waiting on its own continue chain (chaincap.own_wait: they
    could never pass); all of it when they are or-ed."""
    from . import chaincap
    when = a.get("start_when")
    if not (isinstance(when, str) and when.strip() and chaincap.own_wait(p, lineage, when)):
        return a
    parts = [] if "||" in when else re.split(r"\s*&&\s*", when.strip())
    kept = " && ".join(x for x in parts if not chaincap.own_wait(p, lineage, x))
    was.append(f"start_when={kept or None!r} (was: {when.strip()[:120]!r}, a wait on its own continue "
               f"chain that could never pass)")
    return {k: v for k, v in a.items() if k != "start_when"} | ({"start_when": kept} if kept else {})


def _update(p, a: dict, batch: list) -> tuple[dict, list[str]]:
    db, was = p.db, []
    if got := _resources(a):
        a = got[0]
        was.append(got[1])
    wait = a.get("waits_on")
    if isinstance(wait, str) and wait.strip().lower().replace(" ", "") == "ask:new" \
            and not any(isinstance(x, dict) and x.get("type") == "ask_user" for x in batch):
        drop = {"waits_on"} | ({"status"} if a.get("status") == "blocked" else set())
        a = {k: v for k, v in a.items() if k not in drop}
        was.append(f"without {' and '.join(sorted(drop))} (was: waits_on ask:new, but the batch sends no ask_user)")
    t = db.task(_int(a.get("id"))) if _int(a.get("id")) is not None else None
    if t and t["status"] not in ("running", *TERMINAL_TASK_STATES):
        from . import chaincap
        a = _self_wait(p, a, [x["id"] for x in chaincap.chain(db, t["id"])[:-1]], was)
    moved = [k for k in ("pr_branch", *DEFER) if a.get(k) is not None]
    if t and moved and t["status"] in ("running", *TERMINAL_TASK_STATES) and a.get("status") is None:
        title = f"continue #{t['id']}: {t['title']}"[:120]
        if (dup := _open_titled(db, title)) and dup["status"] not in ("running", *TERMINAL_TASK_STATES):
            return {"type": "task_update", "id": dup["id"], **{k: a[k] for k in moved}}, \
                [*was, f"task_update #{dup['id']} with {', '.join(moved)} (was: a task_update of #{t['id']}, which is "
                       f"{t['status']}: #{dup['id']} is the open task carrying its changes)"]
        spec = str(a.get("spec") or "").strip() or f"Continue #{t['id']} ({t['title']}) as its spec says."
        if str(a.get("pr_branch") or "").strip():   # a task_add takes its branch from a spec line
            spec += f"\npr_branch: `{str(a['pr_branch']).strip()}`"
        new = {"type": "task_add", "title": title, "kind": t["kind"], "spec": spec,
               **{k: a[k] for k in moved if k != "pr_branch" or a[k] == ""}}
        new |= {"depends_on": [t["id"]]} if t["status"] == "running" else {"continues": t["id"]}
        how = "after it ends (depends_on)" if t["status"] == "running" else "continuing it"
        return new, [*was, f"a task_add of #{t['id']}'s work {how} with {', '.join(moved)} (was: a task_update of "
                           f"#{t['id']}, which is {t['status']}: those change only before a task starts)"]
    return a, was


def normal_form(p, a: dict, batch: list, _home: str, _now: float):
    got, was = (_add(p, a) if a.get("type") == "task_add" else _update(p, a, batch)
                if a.get("type") == "task_update" else (a, []))
    return (got, "; ".join(was)) if was else None
