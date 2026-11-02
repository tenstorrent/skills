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
gets at most TRIM_MAX of them per change before a cap failure fails it as any other check.

Before a textual conflict costs a fix run, dispatch tries a model-free resolution (model_free): it
rebases the head onto the tip in a scratch worktree and settles each conflicted hunk in which both
sides only added lines (additive), ours then theirs. Only when every hunk qualifies and each Python
file it wrote still parses (with no top-level name defined twice) does it keep the result; the run
is then a light review of that resolution's delta alone (prompts.delta_review_task), and the push
checks run on the head it approves, as on any other. Any doubt falls back to the fix run above.
`record` notes on the conflict rows which way each was settled (RESOLUTIONS)."""
from __future__ import annotations

import ast
import difflib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time

from . import gitmemo

KEY = "conflict_fix"
LIGHT_FAILED = "the light conflict fix ended"
TRIMS = "trim_fixes"   # a review result's count of trim-to-fit fixes the push queue sent it
TRIM_MAX = 2           # ... after which a cap failure fails the review as any other check
CAP_TESTS = r"(?i)prompt\w*_(?:stays_small|under_its_cap|size)$"   # delivery.push_cap_tests: a prompt size cap test's name
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
# An overshoot line batch._stream keeps from a check's whole output: pytest's `assert <size> <= <cap>`.
RESOLUTIONS = ("model_free", "review_rerun")   # push_queue.resolution of a conflict row (record)
RESOLVED_REF = "refs/ttp/resolved/"   # + task id: pins a model-free resolution until its task ends
_TRIED: dict[tuple, dict | None] = {}   # (root, head, onto) -> model_free's answer, this process
DELTA_CHARS = 4000     # the settled hunks a delta review's prompt shows
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
    return gitmemo.run(["git", "-C", root, *args], text=True, capture_output=True, timeout=60,
                       stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"})


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
    rest (before this rule, or escalated). `model_free` and `review_rerun` (and their `_usd`) count
    the conflicts by the resolution dispatch recorded (record), the cost of each one's run included."""
    out = {"fixes": 0, "usd": 0.0, "light": 0, "light_usd": 0.0, "other": 0, "other_usd": 0.0,
           **{k: 0 for k in RESOLUTIONS}, **{f"{k}_usd": 0.0 for k in RESOLUTIONS}}
    seen: set[int] = set()
    for row in db.q("SELECT task, MAX(updated) t, MAX(resolution) how FROM push_queue WHERE status='conflict' "
                    "AND updated>=? GROUP BY task, created", (since,)):
        if row["how"] in RESOLUTIONS:
            out[row["how"]] += 1
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
        if row["how"] in RESOLUTIONS:
            out[f"{row['how']}_usd"] += cost
    for k in ("usd", "light_usd", "other_usd", *(f"{k}_usd" for k in RESOLUTIONS)):
        out[k] = round(out[k], 2)
    out["usd_per_fix"] = round(out["usd"] / out["fixes"], 2) if out["fixes"] else None
    return out


def additive(ours: list[str], base: list[str], theirs: list[str]) -> list[str] | None:
    """One conflict hunk's lines settled without judgment, or None: when each side only added lines
    to the base section (empty, or kept whole by both), the lines both added at one spot go ours
    first, then theirs. Any changed or removed base line is an edit: None."""
    at: list[dict[int, list[str]]] = []
    for side in (ours, theirs):
        ins: dict[int, list[str]] = {}
        for tag, i1, _i2, j1, j2 in difflib.SequenceMatcher(None, base, side, autojunk=False).get_opcodes():
            if tag == "insert":
                ins.setdefault(i1, []).extend(side[j1:j2])
            elif tag != "equal":
                return None
        at.append(ins)
    out: list[str] = []
    for k in range(len(base) + 1):
        out += at[0].get(k, []) + at[1].get(k, [])
        if k < len(base):
            out.append(base[k])
    return out


def merge_additive(ours: str, base: str, theirs: str) -> tuple[str, list[dict]] | None:
    """git's three-way merge of the texts with each conflict hunk settled by `additive`: (merged
    text, the hunks settled as {"ours", "theirs"} line lists), or None when a hunk is no pure
    addition, a side does not end with a newline, or the markers cannot be read. The markers carry a
    random tag, so file content never passes for one."""
    if any(t and not t.endswith("\n") for t in (ours, base, theirs)):
        return None
    tag = secrets.token_hex(8)
    # built, not spelled out: a literal marker line would make this file read as a conflicted one
    start, mid, end = (f"{c * 7} {side}-{tag}" for c, side in (("<", "ours"), ("|", "base"), (">", "theirs")))
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = os.path.join(d, name)
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            paths.append(path)
        r = gitmemo.run(["git", "merge-file", "-p", "--diff3", "-L", f"ours-{tag}", "-L", f"base-{tag}",
                            "-L", f"theirs-{tag}", *paths], capture_output=True, text=True,
                        timeout=60)
    if not 0 <= r.returncode < 127:
        return None
    lines, out, hunks, i = r.stdout.splitlines(keepends=True), [], [], 0
    while i < len(lines):
        if lines[i].rstrip("\r\n") != start:
            out.append(lines[i])
            i += 1
            continue
        parts: list[list[str]] = [[], [], []]
        k, i = 0, i + 1
        while i < len(lines) and lines[i].rstrip("\r\n") != end:
            line = lines[i].rstrip("\r\n")
            if line == mid and k == 0:
                k = 1
            elif line == "=" * 7 and k == 1:
                k = 2
            else:
                parts[k].append(lines[i])
            i += 1
        if i >= len(lines) or k != 2:
            return None
        both = additive(parts[0], parts[1], parts[2])
        if both is None:
            return None
        out += both
        hunks.append({"ours": parts[0], "theirs": parts[2]})
        i += 1
    return "".join(out), hunks


def _py_ok(text: str, ours: str, theirs: str) -> bool:
    """A settled Python file parses and defines no top-level name twice that neither side did."""
    from .batch import _duplicates
    try:
        ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    return not _duplicates(text) - _duplicates(ours) - _duplicates(theirs)


def model_free(root: str, head: str, onto: str, steps: int = 200) -> dict | None:
    """Rebase `head` onto `onto` in a scratch worktree of `root`, settling every conflict by
    merge_additive: {"sha", "onto", "files", "hunks", "delta"} (the new head, the files it settled,
    their hunk count and a bounded text of the settled hunks), or None when any conflict needs
    judgment, a stop is no content conflict, the head carries merges, or git fails. Ours (stage 2)
    is the tip, theirs (stage 3) the change. Nothing outside the scratch worktree changes."""
    if _git(root, "rev-list", "--merges", "-1", head, f"^{onto}").stdout.strip():
        return None
    wt = tempfile.mkdtemp(prefix="ttp-resolve-")
    files: list[str] = []
    settled: list[tuple[str, dict]] = []
    try:
        if _git(root, "worktree", "add", "--detach", "--quiet", wt, onto).returncode != 0:
            return None
        r = _git(wt, "rebase", "--no-keep-empty", onto, head)
        for _ in range(steps):
            if r.returncode == 0:
                sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
                if not sha or not settled:
                    return None   # nothing conflicted here: the batch's own conflict is not this one
                return {"sha": sha, "onto": onto, "files": sorted(set(files)), "hunks": len(settled),
                        "delta": _delta(settled)}
            unmerged = [f for f in _git(wt, "diff", "--name-only", "-z", "--diff-filter=U").stdout.split("\0") if f]
            if not unmerged:
                return None       # the rebase stopped for another reason
            for f in unmerged:
                stages = [_git(wt, "show", f":{n}:{f}") for n in (2, 1, 3)]
                if any(x.returncode != 0 for x in stages):
                    return None   # deleted on one side, or created on both
                o, b, t = (x.stdout for x in stages)
                if "\0" in o + b + t:
                    return None
                got = merge_additive(o, b, t)
                if got is None or (f.endswith(".py") and not _py_ok(got[0], o, t)):
                    return None
                with open(os.path.join(wt, f), "w", encoding="utf-8", newline="") as fh:
                    fh.write(got[0])
                files.append(f)
                settled += [(f, h) for h in got[1]]
            if _git(wt, "add", "--", *unmerged).returncode != 0:
                return None
            r = _git(wt, "rebase", "--continue")
        return None
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError):
        return None
    finally:
        try:
            _git(root, "worktree", "remove", "--force", wt)
        except (OSError, subprocess.SubprocessError):
            pass
        shutil.rmtree(wt, ignore_errors=True)


def _delta(settled: list[tuple[str, dict]]) -> str:
    """The settled hunks as text for a delta review: per hunk, the tip's lines then the change's."""
    out = []
    for f, h in settled:
        out.append(f"--- {f}: kept both, the tip's lines first\n" + "".join(f"+< {x}" for x in h["ours"])
                   + "".join(f"+> {x}" for x in h["theirs"]))
    text = "\n".join(out)
    return text if len(text) <= DELTA_CHARS else text[:DELTA_CHARS] + "\n... (cut: see the range-diff)"


def try_model_free(root: str, cf: dict, task_id: int) -> dict | None:
    """The model-free resolution of a queued conflict fix's head (model_free), pinned under
    RESOLVED_REF, or None: then the fix runs as before. Only a plain textual conflict qualifies: no
    escalation, no trim, conflicting files named. It rebases onto the target's current tip where
    git knows it, else onto the tip the batch saw."""
    head = str(cf.get("head") or "")
    files = [str(f) for f in cf.get("files") or []]
    if cf.get("escalate") or cf.get("trim") or not head or not files or files[0].startswith("(the rebase"):
        return None
    try:
        onto = ""
        for ref in (str(cf.get("target") or ""), str(cf.get("onto") or "")):
            if ref and (r := _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")).returncode == 0:
                onto = r.stdout.strip()
                break
        if not onto or _git(root, "cat-file", "-e", f"{head}^{{commit}}").returncode != 0:
            return None
        key = (root, head, onto)
        if key not in _TRIED:   # a dispatch that did not start tries again next tick: no second rebase
            while len(_TRIED) >= 64:
                _TRIED.pop(next(iter(_TRIED)))
            _TRIED[key] = model_free(root, head, onto)
        mf = _TRIED[key]
        if mf and _git(root, "update-ref", f"{RESOLVED_REF}{int(task_id)}", mf["sha"]).returncode != 0:
            return None
        return mf
    except (OSError, subprocess.SubprocessError):
        return None


def record(db, task_id: int, kind: str) -> None:
    """Note on the review's latest conflict rows how dispatch settles them (RESOLUTIONS); a later
    run of the same fix (an escalation) overwrites it."""
    db.x("UPDATE push_queue SET resolution=? WHERE task=? AND status='conflict' AND "
         "created=(SELECT MAX(created) FROM push_queue WHERE task=?)", (kind, task_id, task_id))


# Conflict markers a commit must never add (stray_markers). Narrow on purpose: only a line that starts
# at column 0 with seven '<', '|' or '>' followed by a space or the line's end (git's own form), or
# that is exactly seven '=' (git's separator). An indented or quoted marker, as tests spell them in
# string literals, never matches. Skipped: paths with a fixture directory (FIXTURE_DIRS), and '='
# lines in docs (DOC_SUFFIXES), where seven '=' underline a heading. Built, not spelled out: a literal
# marker line would make this file read as a conflicted one.
_MARKER = re.compile("^(?:(?:" + "|".join(re.escape(c * 7) for c in "<|>") + ")(?: |$)|" + "=" * 7 + "$)")
_HUNK_NEW = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
FIXTURE_DIRS = frozenset({"fixtures", "fixture", "testdata", "__snapshots__"})
DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
MARKER_LIMIT = 10   # the hits stray_markers reports


def _marker_ok(path: str, line: str) -> bool:
    """Whether an added marker-like `line` of `path` is allowed (see _MARKER)."""
    if FIXTURE_DIRS & set(path.split("/")[:-1]):
        return True
    return line.startswith("=") and path.lower().endswith(DOC_SUFFIXES)


def stray_markers(root: str, *revs: str, timeout: float | None = 60) -> list[str]:
    """Each conflict marker line (_MARKER) that a commit of `revs` (git log arguments, merges aside)
    adds, as "<short sha> <path>:<line>: <marker>", at most MARKER_LIMIT. Every commit counts, not only
    the tip: an intermediate commit that a later one cleans up still differs from its landed copy, so
    the next batch replays it and the replay conflicts. Raises subprocess.TimeoutExpired."""
    args = ("log", "-p", "-U0", "--no-merges", "--no-color", "--no-ext-diff", "--no-renames",
            "--format=%x00%H", *revs)
    r = subprocess.run(["git", "-C", root, *args], capture_output=True, timeout=gitmemo.timeout(timeout, args),
                       stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        return []
    out: list[str] = []
    for chunk in r.stdout.decode("utf-8", "replace").split("\0")[1:]:
        sha, _, diff = chunk.partition("\n")
        path, line_no, header = "", 0, True
        for ln in diff.splitlines():
            if ln.startswith("diff --git "):
                path, header = "", True
            elif header and ln.startswith("+++ "):
                name = ln[4:].strip('"')   # a quoted path: "b/..."
                path = name[2:] if name.startswith("b/") else ""
            elif (m := _HUNK_NEW.match(ln)):
                header, line_no = False, int(m.group(1))
            elif not header and ln.startswith("+"):
                if path and _MARKER.match(ln[1:]) and not _marker_ok(path, ln[1:]):
                    out.append(f"{sha[:10]} {path}:{line_no}: {ln[1:][:40]}")
                    if len(out) >= MARKER_LIMIT:
                        return out
                line_no += 1
    return out


def marker_refusal(hits: list[str]) -> str:
    """Why a head with stray_markers' `hits` is refused."""
    return (f"a commit adds a git conflict marker line ({'; '.join(hits[:3])}"
            + (f"; +{len(hits) - 3} more" if len(hits) > 3 else "") + "). No commit, intermediate ones "
            "included, may carry one: rewrite the commits so none adds it (squash the fix's own commits), "
            "then approve the new head")


MARKER_FIX = "rewrite the commits that add a git conflict marker"   # the follow-up title marker_fix starts with


def marker_fix(review_id: int, why: str, hits: list[str]) -> dict:
    """The fix follow-up for a plain review whose approval was refused for stray_markers' `hits`
    (`why`): its reviewer may not edit, so a fix task rewrites the commits (Daemon._finish_tx)."""
    shas = sorted({h.split(" ", 1)[0] for h in hits})
    return {"title": f"{MARKER_FIX} ({', '.join(shas[:5])})",
            "spec": (f"Review #{review_id} passed the change, but the push queue refused its approval: {why}\n"
                     f"Commits carrying a marker: {', '.join(shas)}. Lines: {'; '.join(hits)}.\n"
                     "Rewrite this branch's own commits so that no commit in the range it adds over the push "
                     "branch adds a conflict marker line: squash each commit that adds one into the commit that "
                     "removes it, or fix it in place, and replay the commits after it. Keep the final tree as "
                     "reviewed and change nothing else. Then check `git log -p` over that range: no added line "
                     "may be a marker. The re-review approves the rewritten head.")}
