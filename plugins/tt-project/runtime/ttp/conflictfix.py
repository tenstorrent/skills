"""Push-conflict fixes: when the push queue sends an approved head back with a textual conflict, its
review runs again only to rebase that head onto the push branch's tip. That run is a light one with a
short prompt (prompts.conflict_fix_task) and costs no review attempt. It runs at standard instead when
the conflict is no plain textual one (the batch's checks failed on the merged result, or the rebase
stopped for another reason), when both sides changed the same Python function or test, or after a
light fix that did not end in a new approval: then the daemon queues the standard run at once, with
no coordinator turn.

pushq._settle marks the review's result with `conflict_fix` (mark); the daemon's dispatch picks the
tier (tier_for) and records `conflict_fix` in the run's note; the run's end reads that note
(escalation) to keep the attempt and requeue a failed light fix at standard.

A trim-to-fit fix is the same run for a change whose merged push checks failed only on prompt size
caps (cap_failure): each change fit alone, but together with what landed before it the prompt text
is over a cap. Its mark carries `trim` (the failing tests and each measured overshoot); the run
rebases, trims the change's own prompt text by at least the overshoot, and approves again. A review
gets at most TRIM_MAX of them per change before a cap failure fails it as any other check."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import time

from . import gitmemo

KEY = "conflict_fix"
LIGHT_FAILED = "the light conflict fix ended"
TRIMS = "trim_fixes"   # a review result's count of trim-to-fit fixes the push queue sent it
TRIM_MAX = 2           # ... after which a cap failure fails the review as any other check
CAP_TESTS = r"(?i)prompt\w*_(?:stays_small|under_its_cap|size)$"   # delivery.push_cap_tests: a prompt size cap test's name
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
# An overshoot line batch._stream keeps from a check's whole output: pytest's `assert <size> <= <cap>`.
_OVER = re.compile(r"^E\s+assert (\d[\d_]*) <= (\d[\d_]*)(?:\s+\(in (\S+)\))?\s*$", re.M)


def mark(head: str, branch: str, target: str, onto: str, files: list, textual: bool, n: int,
         trim: dict | None = None) -> dict:
    """The `conflict_fix` entry of a review's result when the push queue sent `head` back. `textual`:
    the rebase stopped on conflicting files and the batch ran no checks on a merge of both sides.
    `trim` (cap_failure) makes it a trim-to-fit fix."""
    out = {"head": head, "branch": branch, "target": target, "onto": onto,
           "files": [str(f) for f in files[:20]], "n": n, "at": time.time()}
    if trim:
        out["trim"] = trim
    elif not textual:
        out["escalate"] = "the conflict is no plain textual one (the rebase stopped or the merged checks failed)"
    return out


def cap_failure(tail: str, pattern: str | None = None) -> dict | None:
    """{"ids", "over"} when a failed check's output shows it failed only on prompt size cap tests:
    pytest's complete list of failing ids (sharedchecks.failing_ids), each named like one (`pattern`,
    default CAP_TESTS). `over` lists each measured overshoot ({"test", "size", "cap", "by"}) the output
    kept, possibly none. None for any other failure."""
    from .sharedchecks import failing_ids
    ids, complete = failing_ids(tail or "")
    try:
        rx = re.compile(pattern or CAP_TESTS)
    except (re.error, TypeError):
        rx = re.compile(CAP_TESTS)
    if not ids or not complete or not all(rx.search(i.rpartition("::")[2]) for i in ids):
        return None
    found: dict = {}   # (size, cap) -> entry; one line batch._stream prepends may repeat inside the tail
    for m in _OVER.finditer(tail):
        size, cap = int(m.group(1).replace("_", "")), int(m.group(2).replace("_", ""))
        if size > cap and (found.get((size, cap)) is None or (m.group(3) and not found[(size, cap)]["test"])):
            found[(size, cap)] = {"test": m.group(3) or "", "size": size, "cap": cap, "by": size - cap}
    over = list(found.values())
    return {"ids": ids[:10], "over": over[:10]}


def over_text(trim: dict) -> str:
    """The overshoots of a cap_failure for a prompt or a feed line."""
    over = trim.get("over") or []
    if not over:
        return "overshoot not in the output; " + ", ".join(str(i) for i in trim.get("ids") or []) + " show it"
    return "; ".join(f"{o.get('test') or 'a cap test'}: {o['size']} > {o['cap']} (over by {o['by']})" for o in over)


def of(task: dict) -> dict | None:
    """The `conflict_fix` mark of a queued review, or None: then its next run is no conflict fix."""
    if task.get("kind") != "review":
        return None
    try:
        res = json.loads(task.get("result") or "{}")
    except (ValueError, TypeError):
        return None
    cf = res.get(KEY) if isinstance(res, dict) else None
    return cf if isinstance(cf, dict) and cf.get("head") else None


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], text=True, capture_output=True, timeout=gitmemo.timeout(60, args),
                          stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _functions(src: str) -> list[tuple[str, int, int]]:
    """(qualified name, first line, last line) of every function in Python source `src`."""
    out: list[tuple[str, int, int]] = []

    def walk(node, prefix: str) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{ch.name}"
                if not isinstance(ch, ast.ClassDef):
                    first = min([ch.lineno, *(d.lineno for d in ch.decorator_list)])
                    out.append((name, first, ch.end_lineno or ch.lineno))
                walk(ch, name + ".")
            else:
                walk(ch, prefix)
    walk(ast.parse(src), "")
    return out


def _touched(diff: str, funcs: list[tuple[str, int, int]]) -> set[str]:
    """The innermost functions of the base file that a `git diff -U0` changes: a changed line inside
    one, or an insertion between two of its lines."""
    out: set[str] = set()

    def inner(lo: int, hi: int) -> str | None:
        hits = [f for f in funcs if f[1] <= lo and hi <= f[2]]
        return max(hits, key=lambda f: f[1])[0] if hits else None
    for line in diff.splitlines():
        m = _HUNK.match(line)
        if not m:
            continue
        start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        if count:
            for ln in range(start, start + count):
                name = inner(ln, ln)
                if name:
                    out.add(name)
        elif (name := inner(start, start + 1)):   # inserted after line `start`, inside a function
            out.add(name)
    return out


def same_body(root: str, head: str, onto: str, files: list) -> str:
    """Why the conflict needs more than a light run: both `head` and `onto` changed the same function
    or test of a conflicting Python file since their merge base (e.g. "both sides changed
    tests/test_a.py:test_b"), or "". Other files, and anything git or the parser cannot read, count
    as textual: a light fix that fails runs again at standard anyway."""
    try:
        base = _git(root, "merge-base", head, onto)
        if base.returncode != 0:
            return ""
        base_sha = base.stdout.strip()
        for f in files:
            f = str(f)
            if not f.endswith(".py"):
                continue
            src = _git(root, "show", f"{base_sha}:{f}")
            if src.returncode != 0:
                continue
            try:
                funcs = _functions(src.stdout)
            except (SyntaxError, ValueError):
                continue
            sides = []
            for side in (head, onto):
                d = _git(root, "diff", "-U0", "--no-color", "--no-ext-diff", base_sha, side, "--", f)
                sides.append(_touched(d.stdout, funcs) if d.returncode == 0 else set())
            both = sorted(sides[0] & sides[1])
            if both:
                return f"both sides changed {f}:{both[0]}"
    except (OSError, subprocess.SubprocessError):
        return ""
    return ""


def tier_for(root: str, cf: dict) -> tuple[str, str]:
    """The tier of a conflict-fix run and why: light for a plain textual conflict, standard when the
    mark says to escalate or both sides changed the same function or test."""
    if cf.get("trim") and not cf.get("escalate"):
        return "light", "prompt size cap overshoot: trim to fit"
    why = str(cf.get("escalate") or "") or same_body(root, str(cf.get("head") or ""), str(cf.get("onto") or ""),
                                                     list(cf.get("files") or []))
    return ("standard", why) if why else ("light", "textual push conflict")


def run_note(note: str | None) -> dict | None:
    """The `conflict_fix` entry of a run's note (set at dispatch), or None: the run was no conflict fix."""
    try:
        cf = json.loads(note or "{}").get(KEY)
    except (ValueError, TypeError, AttributeError):
        return None
    return cf if isinstance(cf, dict) else None


def stats(db, since: float) -> dict:
    """Cost of the runs that fixed push conflicts since `since`: each conflict the queue sent back
    and its task's next run. `light` counts runs dispatched as light conflict fixes, `other` the
    rest (before this rule, or escalated)."""
    out = {"fixes": 0, "usd": 0.0, "light": 0, "light_usd": 0.0, "other": 0, "other_usd": 0.0}
    seen: set[int] = set()
    for row in db.q("SELECT task, MAX(updated) t FROM push_queue WHERE status='conflict' AND updated>=? "
                    "GROUP BY task, created", (since,)):
        r = db.one("SELECT id, cost_usd, note FROM runs WHERE task=? AND role!='coordinator' AND started>=? "
                   "ORDER BY id LIMIT 1", (row["task"], row["t"]))
        if not r or r["id"] in seen:
            continue
        seen.add(r["id"])
        cost = float(r["cost_usd"] or 0)
        cf = run_note(r["note"])
        k = "light" if cf and cf.get("tier") == "light" else "other"
        out[k] += 1
        out[f"{k}_usd"] += cost
        out["fixes"] += 1
        out["usd"] += cost
    for k in ("usd", "light_usd", "other_usd"):
        out[k] = round(out[k], 2)
    out["usd_per_fix"] = round(out["usd"] / out["fixes"], 2) if out["fixes"] else None
    return out
