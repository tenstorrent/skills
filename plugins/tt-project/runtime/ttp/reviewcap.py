# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Repeated review failures in one area, counted across stacks.

The daemon fixes a failed review at most AUTO_FIX_ROUNDS times per stack (a `continues:` chain), but
a new stack on the same component (`Fix re-review #N`, `#N follow-up`, a task that `continues` one)
started the count again. Here an area is a lineage of code and review tasks linked by `continues:`,
follow-ups (`parent`), a review's `depends_on`, the daemon's `review_fix:`/`auto_review:` labels and
`Review #N` / `Fix review #N` / `#N follow-up` titles; as a fallback, reviews whose diffs have the
same main file (the non-test file with the most changed lines) count as one area too, but only
when their changes there overlap: a shared hunk line range, or a shared function around or changed
by its hunks (an enclosing class alone does not count). A busy file is touched by most changes, so
this holds however many failures the review's own lineage has. Own-lineage and pooled failures are
reported apart. Links run only between code and review tasks, so a plan or daily review that
spawned many tasks joins none.

With review.area_fail_cap (default 3; 0 off) or more reviews of one area failed or asking for
changes within AREA_WINDOW_S, the daemon queues no automatic fix for it and raises a
REVIEW_AREA_EVENT, which makes the coordinator's turn a tricky one (TRIGGER) that re-plans. The
event names each failed review's first finding in one line (first_finding).
A review whose hand-off says `review_failure: check_only` (daemon.review_failure) found no code
problem, only a missing or inapplicable check, so it does not count toward the cap; nor does one
the push queue failed because only its batch checks failed (pushq._settle writes check_only too),
nor one the daemon failed with no reviewer run because its pre-review checks failed
(`review_failure: precheck`, Daemon._precheck_failed): no reviewer judged the design. That one still
counts toward the per-change round cap (daemon.AUTO_FIX_ROUNDS), since it is a round of the stack.
"""
from __future__ import annotations

import json
import re
import time

from . import gitmemo
from .db import DB, continues_id, dependency_ids, load_result

AREA_WINDOW_S = 48 * 3600
LOOKBACK_S = 14 * 86400   # tasks this recent are searched for links back into a lineage
MAX_LINEAGE = 400         # a lineage this big stops growing: the search must stay cheap
DEFAULT_CAP = 3
REVIEW_AREA_EVENT = "review_area_cap"
TRIGGER = "repeated review failures in one area"
FAILED_KINDS = ("task_failed", "task_changes_needed")
TITLE_REF = re.compile(r"(?i)^\s*(?:fix\s+)?(?:re-?)?review\s+#(\d+)|#(\d+)\s+follow-?up\b"
                       r"|\bfollow-?up\s+(?:to|of|on|for)\s+#(\d+)")
LINK_LABELS = ("review_fix:", "auto_review:")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@ ?(.*)$")
DEF = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|fn|func|sub)\s+([A-Za-z_]\w*)")
FINDING_CHARS = 200   # one failed review's first finding in the event
MAX_FINDINGS = 6      # the newest failed reviews whose first finding the event names
TEST_PATH = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*$|_test\.[^/]+$|\.(spec|test)\.[^/]+$")


def cap(cfg: dict) -> int:
    try:
        return max(0, int((cfg.get("review") or {}).get("area_fail_cap", DEFAULT_CAP)))
    except (TypeError, ValueError):
        return DEFAULT_CAP


def links(task: dict) -> set[int]:
    """The ids `task` names as its own lineage: what it continues or follows up, what a review
    depends on, the daemon's fix and re-review labels, and `Review #N`-style titles."""
    out: set[int] = set()
    if (c := continues_id(task)) is not None:
        out.add(c)
    if task.get("parent"):
        out.add(int(task["parent"]))
    try:
        labels = json.loads(task.get("labels") or "[]")
    except ValueError:
        labels = []
    for lb in labels if isinstance(labels, list) else []:
        for pre in LINK_LABELS:
            if isinstance(lb, str) and lb.startswith(pre) and lb[len(pre):].isdigit():
                out.add(int(lb[len(pre):]))
    if task.get("kind") == "review":
        out |= {i for i in dependency_ids(task) if i is not None}
    for m in TITLE_REF.finditer(task.get("title") or ""):
        out.add(int(next(g for g in m.groups() if g)))
    out.discard(task["id"])
    return out


def lineage(db: DB, task_id: int, now: float | None = None) -> set[int]:
    """The code and review tasks linked to `task_id` (itself included), both ways along `links`."""
    now = time.time() if now is None else now
    rows = {t["id"]: t for t in db.q("SELECT * FROM tasks WHERE kind IN ('code','review') AND (created>? OR updated>?)",
                                     (now - LOOKBACK_S, now - LOOKBACK_S))}
    adj: dict[int, set[int]] = {}

    def add(t: dict) -> None:
        for o in links(t):
            adj.setdefault(t["id"], set()).add(o)
            adj.setdefault(o, set()).add(t["id"])
    for t in rows.values():
        add(t)
    seen, todo = set(), [task_id]
    while todo and len(seen) < MAX_LINEAGE:
        i = todo.pop()
        if i in seen:
            continue
        if i not in rows:   # older than the lookback: read it, and follow its own links back
            t = db.task(i)
            if not t or t["kind"] not in ("code", "review"):
                continue
            rows[i] = t
            add(t)
        seen.add(i)
        todo += [o for o in adj.get(i, ()) if o not in seen]
    return seen


def main_file(p, review: dict) -> str | None:
    """The non-test file with the most changed lines in what `review` reviews, or None (kept per
    review within a gitmemo scope)."""
    return gitmemo.cached(("reviewcap.main_file", review["id"]), lambda: _main_file(p, review))


def _main_file(p, review: dict) -> str | None:
    from . import worktree
    try:
        lines = worktree.diff_lines(p, worktree.reviewed_refs(p, review)) or {}
    except Exception:   # a branch that is gone or unreadable has no main file
        return None
    files = [(n, f) for f, n in lines.items() if n and not TEST_PATH.search(f)]
    return max(files, key=lambda x: (x[0], x[1]))[1] if files else None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _scope(base: list[str], at: int, limit: int) -> list[tuple[int, str, bool]]:
    """The functions and classes around base line `at` (1-based, walking up from it) for code at
    indent `limit`, outermost first: (indent, name, is a class)."""
    out: list[tuple[int, str, bool]] = []
    for line in reversed(base[:max(at, 0)]):
        if not line.strip() or line.lstrip().startswith(("#", "//", "/*", "*")) or _indent(line) >= limit:
            continue
        limit = _indent(line)
        if d := DEF.match(line):
            out.append((limit, d[1], d[0].split()[-2] == "class"))
        if not limit:
            break
    return out[::-1]


def touched(p, review: dict, path: str) -> tuple[list[tuple[int, int]], set[str]]:
    """What `review` changes in `path`: the base-side line ranges of its hunks, and the functions
    they sit in or the functions and classes they change, as dotted names (`Core.run`). The function
    around a hunk is read from the base file: git's hunk header names the enclosing top-level line,
    which for a method is its class, and a shared enclosing class alone is no overlap. Kept per
    review and path within a gitmemo scope."""
    return gitmemo.cached(("reviewcap.touched", review["id"], path), lambda: _touched(p, review, path))


def _touched(p, review: dict, path: str) -> tuple[list[tuple[int, int]], set[str]]:
    import subprocess
    from . import worktree
    ranges: list[tuple[int, int]] = []
    names: set[str] = set()
    try:
        base, refs = worktree.resolve_base(p), worktree.reviewed_refs(p, review)
        for ref in refs:
            if not worktree._git(p.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False):
                continue
            mb = worktree._git(p.root, "merge-base", base, ref, check=False)
            old = subprocess.run(["git", "-C", str(p.root), "show", f"{mb}:{path}"], capture_output=True,
                                 text=True, timeout=120) if mb else None
            lines = old.stdout.splitlines() if old is not None and old.returncode == 0 else []
            hunks: list[tuple[int, int, list[str]]] = []
            for line in worktree._git(p.root, "diff", "-U0", "--no-renames", "--no-color", f"{base}...{ref}",
                                      "--", path).splitlines():
                if m := HUNK.match(line):
                    count = int(m[2] if m[2] is not None else 1)
                    hunks.append((int(m[1]), count, []))
                elif hunks and line[:1] in "+-" and not line.startswith(("+++", "---")):
                    hunks[-1][2].append(line[1:])
            for start, count, body in hunks:
                ranges.append((start, start + max(count, 1)))
                # A modified hunk starts at its first changed line; an insertion follows line `start`.
                at = start - 1 if count else start
                code = [b for b in body if b.strip()]
                limit = _indent(code[0]) if code else _indent(lines[start - 1]) if 0 < start <= len(lines) else 0
                around = _scope(lines, at, limit)
                if around and not around[-1][2]:
                    names.add(".".join(n for _, n, _ in around))
                for b in code:
                    if d := DEF.match(b):
                        names.add(".".join([n for i, n, _ in around if i < _indent(b)] + [d[1]]))
    except Exception:   # unreadable: nothing overlaps
        pass
    return ranges, names


def overlaps(a: tuple[list[tuple[int, int]], set[str]], b: tuple[list[tuple[int, int]], set[str]]) -> bool:
    """A shared function, or hunks sharing a base line (half-open ranges: touching ends do not)."""
    return bool(a[1] & b[1]) or any(s1 < e2 and s2 < e1 for s1, e1 in a[0] for s2, e2 in b[0])


def check_only(task: dict) -> bool:
    """Whether review `task`'s latest hand-off says it failed only on a check, with no code finding."""
    return load_result(task.get("result")).get("review_failure") == "check_only"


def unjudged(task: dict) -> bool:
    """Whether review `task` failed with no reviewer judging the code: check-only, or its pre-review
    checks failed (precheck). Neither counts toward area_fail_cap."""
    return load_result(task.get("result")).get("review_failure") in ("check_only", "precheck")


def failed_reviews(db: DB, since: float) -> list[int]:
    """Reviews that failed or asked for changes since `since`, each once, but none no reviewer judged (unjudged)."""
    return [r["task"] for r in db.q(
        "SELECT DISTINCT e.task, t.result FROM events e JOIN tasks t ON t.id=e.task WHERE t.kind='review' AND e.ts>? "
        f"AND e.kind IN ({','.join('?' * len(FAILED_KINDS))}) ORDER BY e.task", (since, *FAILED_KINDS))
        if not unjudged(r)]


def area(p, cfg: dict, review: dict, now: float | None = None) -> dict | None:
    """When `review` (failing now) makes area_fail_cap failed reviews of its area within
    AREA_WINDOW_S: {root, title, reviews, tasks, files}; None below it or with the cap off."""
    n = cap(cfg)
    if not n or unjudged(review):
        return None
    db, now = p.db, time.time() if now is None else now
    fails = set(failed_reviews(db, now - AREA_WINDOW_S)) | {review["id"]}
    if len(fails) < n:
        return None
    line = lineage(db, review["id"], now)
    hit = fails & line
    mains: dict[int, str | None] = {}
    if len(hit) < n and (main := main_file(p, review)):
        # Others on its main file join only where their changes overlap, however many failed in its own lineage.
        mine = touched(p, review, main)
        for i in sorted(fails - hit):
            t = db.task(i)
            if t and mains.setdefault(i, main_file(p, t)) == main and overlaps(mine, touched(p, t, main)):
                hit.add(i)
    if len(hit) < n:
        return None
    files = sorted({f for i in hit if (f := mains[i] if i in mains else main_file(p, db.task(i) or review))})
    root = db.task(min(line)) or review
    return {"root": root["id"], "title": str(root["title"])[:120], "reviews": sorted(hit),
            "own": sorted(hit & line), "pooled": sorted(hit - line), "tasks": sorted(line | hit), "files": files,
            "findings": {str(i): first_finding(review if i == review["id"] else db.task(i))
                         for i in sorted(hit)[-MAX_FINDINGS:]}}


def first_finding(task: dict | None) -> str:
    """Review `task`'s first finding in one line (FINDING_CHARS): its first follow-up (title: spec),
    else its hand-off summary, so the area's re-plan needs no extra reads."""
    r = load_result((task or {}).get("result"))
    fups = [f for f in r.get("followups") or [] if isinstance(f, dict) and (f.get("title") or f.get("spec"))]
    text = ": ".join(str(fups[0].get(k) or "").strip() for k in ("title", "spec") if fups[0].get(k)) if fups \
        else str(r.get("summary") or "")
    text = " ".join(text.split())
    return (text[:FINDING_CHARS - 1] + "…" if len(text) > FINDING_CHARS else text) or "no finding recorded"


def _refs(ids) -> str:
    return ", ".join(f"#{i}" for i in ids)


def _split(a: dict) -> str:
    """The failed reviews, own lineage apart from those pooled on the same main file."""
    if "own" not in a:   # an event from before the split
        return "reviews " + _refs(a.get("reviews") or [])
    own, pooled = a.get("own") or [], a.get("pooled") or []
    return (f"{len(own)} in its own lineage ({_refs(own) or 'none'}), {len(pooled)} pooled from other lineages "
            f"with overlapping changes to the same main file ({_refs(pooled) or 'none'})")


def event_text(a: dict) -> str:
    tasks = ", ".join(f"#{i}" for i in a["tasks"][:30]) + (" ..." if len(a["tasks"]) > 30 else "")
    return (f"{TRIGGER}: {len(a['reviews'])} reviews failed or asked for changes within "
            f"{AREA_WINDOW_S / 3600:g} h in the lineage of #{a['root']} ({a['title']}): "
            + _split(a) + f"; tasks {tasks}; main files: "
            + (", ".join(a["files"]) or "unknown")
            + "." + "".join(f"\n- #{i} first finding: {f}" for i, f in (a.get("findings") or {}).items())
            + ("\n" if a.get("findings") else " ")
            + "The daemon queues no more automatic fixes for it. Re-plan this area: narrow the scope, accept "
              "and document the known gaps, or redesign. Do not add another edge-case fix, and do not raise "
              "the tier to deep for this alone. Record the settled design with `memory_add` `memory_kind: "
              "settled_area` (`files`, `title` as the one-line rule, `text` the decision, `reviews` the failed "
              "ids), not as a plain decision: it goes to the settled-areas register, which reaches only the "
              "tasks that touch those files.")


def daily_line(db: DB, since: float) -> str:
    """The daily review's line: each area that reached the cap since `since`, once."""
    seen: dict[int, dict] = {}
    for r in db.q("SELECT data FROM events WHERE kind=? AND ts>=? ORDER BY ts", (REVIEW_AREA_EVENT, since)):
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("root") is not None:
            seen[int(d["root"])] = d
    if not seen:
        return "none"
    return f"{len(seen)} area(s): " + "; ".join(
        f"#{k} ({len(d.get('reviews') or [])} failed reviews"
        + (f": {len(d.get('own') or [])} own, {len(d.get('pooled') or [])} pooled" if "own" in d else "")
        + (f", {', '.join(d.get('files') or [])}" if d.get("files") else "") + ")" for k, d in seen.items())

