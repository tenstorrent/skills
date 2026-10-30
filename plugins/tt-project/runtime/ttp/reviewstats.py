# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One row per finished review (table review_stats), so review routing can be measured and retested:
the reviewed head, its changed lines against its merge-base with the base branch (all lines, and
`code_lines`: those outside tests, docs and changesets), the areas the
change touches (runtime, skills-prompts-config, tests-docs), the review's tier, whether it was a
re-review after a failed one, its outcome, its cost and whether the reviewed hand-off carried a
`self_check` line (kind-code.md's try-to-break-it step). The daemon writes a row each time a review's
run ends in a verdict (a later verdict of the same review replaces it). The daily review's
efficiency audit reads `summary`/`audit_line`; `ttp audit --json` carries the rows.

A re-review leads with the fix delta: `delta_line` names the failed head and the new head, and asks
for the delta checked against the earlier findings first and the full diff only where needed."""
from __future__ import annotations

import json
import time
from typing import Any

from . import budget as bud
from .db import DB, continues_id, load_result, review_subject

AREAS = ("runtime", "skills-prompts-config", "tests-docs")
# Prompt, skill and config content: a part of the path with one of these names, or a config suffix.
PROMPT_DIRS = {"skills", "prompts", "template", "templates", "agents", "commands", "hooks"}
CONFIG_SUFFIXES = (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf")
PROMPT_NAMES = ("SKILL.md", "AGENTS.md", "CLAUDE.md")
OUTCOMES = {"done": "approved", "pushing": "approved", "failed": "failed", "blocked": "blocked",
            "review": "needs_review", "changes_needed": "changes_needed"}


def area(path: str) -> str:
    """The area of one changed path: tests-docs for a test or doc file, skills-prompts-config for
    skill, prompt or config content (SKILL.md under skills/ is a prompt, not a doc), runtime else."""
    parts = path.split("/")
    if bud.is_test_path(path):
        return "tests-docs"
    if parts[-1] in PROMPT_NAMES or PROMPT_DIRS & {x.lower() for x in parts[:-1]} \
            or parts[-1].lower().endswith(CONFIG_SUFFIXES):
        return "skills-prompts-config"
    if parts[-1].lower().endswith(bud.DOC_SUFFIXES) or "docs" in {x.lower() for x in parts[:-1]} \
            or ".changeset" in parts[:-1]:
        return "tests-docs"
    return "runtime"


def areas(changes: dict[str, int | None] | None) -> list[str]:
    """The areas `changes` (path -> changed lines) touch, in AREAS order."""
    hit = {area(path) for path in changes or {}}
    return [a for a in AREAS if a in hit]


def code_lines(changes: dict[str, int | None] | None) -> int | None:
    """Changed lines outside tests, docs and changesets (area tests-docs), None when `changes` is."""
    return None if changes is None else sum(n or 0 for path, n in changes.items() if area(path) != "tests-docs")


def is_rereview(db: DB, review: dict) -> bool:
    """A review after a failed review of the same change: it continues a review, or its subject is
    a fix of a failed review (review_fix:, or continues: of a failed review)."""
    c = continues_id(review)
    if c is not None and (t := db.task(c)) and t["kind"] == "review":
        return True
    subject = review_subject(review)
    t = db.task(subject) if subject is not None else None
    try:
        labels = json.loads(t["labels"] or "[]") if t else []
    except ValueError:
        labels = []
    return bool(t) and db._continues_failed_review(labels if isinstance(labels, list) else [])


def has_self_check(db: DB, review: dict) -> int | None:
    """1 when the hand-off of the task `review` reviews carries a non-empty `self_check` line (or its
    `on_pass` or `metrics` does), 0 when it carries none, None when the review names no task that exists."""
    subject = review_subject(review)
    t = db.task(subject) if subject is not None else None
    if not t:
        return None
    res = load_result(t.get("result"))
    on_pass = res.get("on_pass") if isinstance(res.get("on_pass"), dict) else {}
    for got in (res, on_pass, res.get("metrics"), on_pass.get("metrics")):
        if isinstance(got, dict) and isinstance(got.get("self_check"), str) and got["self_check"].strip():
            return 1
    return 0


def _reviewed(p, review: dict) -> tuple[str, str]:
    """(branch, full head) the review reviewed: the head it approved for the push queue, else the
    tip of its subject's branch now; ('', '') when it names none."""
    from . import worktree
    db = p.db
    q = db.one("SELECT branch, head FROM push_queue WHERE task=? ORDER BY id DESC LIMIT 1", (review["id"],))
    subject = review_subject(review)
    t = db.task(subject) if subject is not None else None
    branch = (t or {}).get("branch") or (q["branch"] if q else "") or ""
    if q and q["head"]:
        return branch, q["head"]
    if not branch:
        return "", ""
    return branch, worktree._git(p.root, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}", check=False)


def record(p, review: dict, outcome: str, now: float | None = None) -> dict | None:
    """Write the review's row: its head, changed lines against the merge-base (all, and code_lines),
    areas, tier, re-review flag, outcome (OUTCOMES), cost so far and self-check flag (has_self_check).
    Git trouble leaves lines and areas empty."""
    from . import worktree
    db = p.db
    if review.get("kind") != "review":
        return None
    head, lines, code, touched = "", None, None, []
    try:
        _, head = _reviewed(p, review)
        changes = worktree.diff_lines(p, [head]) if head else None
        if changes is not None:
            lines = sum(n or 0 for n in changes.values())
            code = code_lines(changes)
            touched = areas(changes)
    except Exception:
        pass
    t = db.task(review["id"]) or review
    row = {"task": review["id"], "ts": time.time() if now is None else now, "head": head[:40] or None,
           "lines": lines, "code_lines": code, "areas": ",".join(touched), "tier": t.get("tier") or "standard",
           "rereview": int(is_rereview(db, t)), "outcome": OUTCOMES.get(outcome, outcome),
           "cost_usd": float(t.get("spent_usd") or 0), "self_check": has_self_check(db, t)}
    db.x("INSERT OR REPLACE INTO review_stats(task,ts,head,lines,code_lines,areas,tier,rereview,outcome,cost_usd,"
         "self_check) VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(row.values()))
    return row


def failed_head(db: DB, review_id: int) -> str:
    """The head a failed review reviewed, as recorded; '' when none was."""
    r = db.one("SELECT head FROM review_stats WHERE task=? AND outcome IN ('failed','changes_needed')", (review_id,))
    return (r["head"] or "") if r else ""


def delta_line(failed: str, ref: str, new: str = "", findings: str = "each finding above") -> str:
    """The re-review's lead: the fix delta `failed..ref` checked against the earlier findings first,
    the full diff only where the delta touches it or the fix could regress. `new` names the new head
    when it is known (else the tip of `ref`)."""
    return (f"Fix delta: failed head {failed[:12]}, new head {new or f'the tip of {ref}'}. Lead with "
            f"`git diff {failed[:12]}..{ref}`: check {findings} against it first. Read the full diff against "
            f"the merge-base only where the delta touches it or where the fix could regress; it stays "
            f"available for that.")


def new_head_line(p, failed: str, branch: str, full: str) -> str:
    """The line a queued re-review gets once its fix finished: the new head and the delta's size."""
    from . import worktree
    n = 0
    for ln in worktree._git(p.root, "diff", "--numstat", "--no-renames", f"{failed}..{full}").splitlines():
        a, d, _ = ln.split("\t", 2)
        n += 0 if a == "-" else int(a) + int(d)
    return (f"New head: {branch} at {full[:12]}. The fix delta {failed[:12]}..{full[:12]} changes {n} lines: "
            f"lead with it, as above.")


def rows(db: DB, since: float) -> list[dict]:
    return db.q("SELECT * FROM review_stats WHERE ts>=? ORDER BY ts", (since,))


def size_bucket(lines: int | None) -> str:
    if lines is None:
        return "unknown"
    return "<50" if lines < 50 else "50-100" if lines <= 100 else "101-200" if lines <= 200 else ">200"


def summary(db: DB, since: float) -> dict[str, Any]:
    """Reviews finished since `since`: counts, cost and the failure rate (failed or changes_needed)
    per size bucket (of all changed lines, and of non-test lines: code_lines), per tier, for first
    reviews against re-reviews and by whether the reviewed hand-off carried a self-check (rows from
    before a field, or with no reviewed task, count as unknown)."""
    out: dict[str, Any] = {"n": 0, "usd": 0.0, "by_size": {}, "by_code_size": {}, "by_tier": {}, "by_kind": {},
                           "by_self_check": {}}
    for r in rows(db, since):
        out["n"] += 1
        out["usd"] += float(r["cost_usd"] or 0)
        bad = r["outcome"] in ("failed", "changes_needed")
        for key, val in (("by_size", size_bucket(r["lines"])), ("by_code_size", size_bucket(r["code_lines"])),
                         ("by_tier", r["tier"]),
                         ("by_kind", "re-review" if r["rereview"] else "first"),
                         ("by_self_check", {1: "self-checked", 0: "not self-checked"}.get(r["self_check"], "unknown"))):
            g = out[key].setdefault(val, {"n": 0, "failed": 0, "usd": 0.0})
            g["n"] += 1
            g["failed"] += bad
            g["usd"] += float(r["cost_usd"] or 0)
    return out


def audit_line(db: DB, since: float) -> str:
    """The efficiency audit's line on review routing, or '' when no review finished."""
    s = summary(db, since)
    if not s["n"]:
        return ""

    def part(groups: dict) -> str:
        return ", ".join(f"{k} {g['n']} ({g['failed']} failed, ${g['usd']:.2f})" for k, g in sorted(groups.items()))
    return (f"reviews finished: {s['n']}, ${s['usd']:.2f}; by diff lines: {part(s['by_size'])}; "
            f"by non-test lines: {part(s['by_code_size'])}; by tier: "
            f"{part(s['by_tier'])}; {part(s['by_kind'])}; {part(s['by_self_check'])}")
