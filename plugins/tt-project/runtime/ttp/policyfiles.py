# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A project's policy files: the code that decides what needs an audit record, a review, an approval, a budget or
a push. Judgment about them belongs to the strongest model, so the code arm keeps them off the cheaper one.

Each project keeps its own list in its harness, `harness/policy_files.txt`: one glob per line, relative to the
code repo root; `#` starts a comment; `**` crosses directories, `*` and `?` stay inside one; a pattern with no
`/` matches that name at any depth. The plugin ships no list, only this format. A project with no list (missing or
empty) cannot run the arm live (codearm.refusal): the share is 0 until it has one.

Two model-free uses (codearm): before the coin, a task is policy-class when its title, spec or `Where:` line names
a listed file or uses a policy term (TERMS); after a run on the arm, a branch that changes a listed file is refused
(bounce_diff) and the task goes to Opus.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

FILE = "policy_files.txt"
EVIDENCE_REF = "refs/ttp/policy-bounce/t"   # + task id: the branch tip a refused hand-off was saved at
# Policy terms: a task about these decides what the project audits, reviews, approves, budgets or pushes.
# Plain words like "push" or "approved" are not here: every spec says "land with ttp push".
TERMS = re.compile(
    r"(?i)\baudit (?:records?|logs?|trails?)\b|\breview (?:gates?|polic(?:y|ies)|requirements?|rules?)\b"
    r"|\bapproval (?:gates?|rules?|checks?|flow|polic(?:y|ies))\b|\b(?:budget|spend)[ -](?:caps?|gates?|limits?|guards?)\b"
    r"|\bpush[ -](?:gates?|checks?|queue|rules?|polic(?:y|ies)|guards?)\b|\bask[ -]gates?\b"
    r"|\b(?:restriction|charter)[ -](?:gates?|checks?|guards?|rules?)\b|\bdelivery[ -](?:gates?|checks?|rules?|polic(?:y|ies))\b"
    r"|\brequired checks?\b|\bwhat needs (?:an? )?(?:audit|review|approval)\b")
PATH_TOKEN = re.compile(r"[\w./*-]*[\w*]\.\w+|[\w.*-]+/[\w./*-]+")
MODULE_TOKEN = re.compile(r"\b([A-Za-z_]\w*)\.(_?[A-Za-z_]\w*)\b")


def load(harness_dir) -> list[str]:
    """The project's globs ([] when the file is missing, unreadable or empty)."""
    try:
        text = (Path(harness_dir) / FILE).read_text()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.lstrip("/"))
    return out


def _regex(glob: str) -> re.Pattern:
    out, i = "", 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif c == "*":
            out, i = out + "[^/]*", i + 1
        elif c == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(c), i + 1
    return re.compile(out + r"\Z")


def matches(patterns: list[str], path: str) -> str:
    """The first glob of `patterns` that names repo-relative `path`, '' when none does."""
    path = path.strip()
    if path.startswith("./"):
        path = path[2:]
    for g in patterns:
        if _regex(g if "/" in g else f"**/{g}").match(path):
            return g
    return ""


def hits(patterns: list[str], paths) -> list[str]:
    return sorted(p for p in paths if matches(patterns, p))


def _concrete(patterns: list[str]) -> list[str]:
    return [g for g in patterns if not any(ch in g for ch in "*?")]


def task_reason(patterns: list[str], task: dict) -> str:
    """Why `task` is policy-class ('' when it is not): a listed file named in its title or spec (a full path, a
    path tail such as `ttp/daemon.py`, a bare file name, or a dotted module name such as `coordfix._python_reads`
    whose module is a listed file's) or a policy term."""
    text = f"{task.get('title') or ''}\n{task.get('spec') or ''}"
    concrete = _concrete(patterns)
    stems = {Path(g).stem: g for g in concrete if g.endswith(".py")}
    for tok in PATH_TOKEN.findall(text):
        tok = tok.strip(".,;:)(`'\"")
        if not tok:
            continue
        if g := matches(patterns, tok):
            return f"names {tok} (policy file {g})"
        if "." in tok and (g := next((c for c in concrete if c == tok or c.endswith("/" + tok)), "")):
            return f"names {tok} (policy file {g})"
    for mod, ident in MODULE_TOKEN.findall(text):
        if mod in stems and not mod[0].isupper():
            return f"names {mod}.{ident} (policy file {stems[mod]})"
    if m := TERMS.search(text):
        return f"policy term: {m.group(0).lower()}"
    return ""


def _git(cwd, *args: str) -> str:
    got = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120)
    return got.stdout.strip() if got.returncode == 0 else ""


def bounce_diff(p, task: dict, patterns: list[str], changed: dict | None) -> str:
    """After a run on the arm: the reason to bounce its task to Opus ('' when its branch touches no policy file).
    `changed` is the branch's paths against its base (worktree.diff_lines). The branch tip is saved first
    (EVIDENCE_REF) and the task's branch goes back to its fork point, so the Opus run starts clean."""
    found = hits(patterns, changed or {})
    if not found:
        return ""
    from . import worktree
    tid, branch = task["id"], task.get("branch") or ""
    path = p.worktrees / f"t{tid}"
    root = path if path.is_dir() else p.root
    try:
        tip = _git(root, "rev-parse", "--verify", "--quiet", branch or "HEAD")
        if tip:
            _git(p.root, "update-ref", f"{EVIDENCE_REF}{tid}", tip)
        fork = worktree.fork_point(p.root, tid)
        if tip and fork and fork != tip:
            if path.is_dir() and worktree.worktree_branch(p, task):
                _git(path, "reset", "--hard", fork)
            elif branch:
                _git(p.root, "update-ref", f"refs/heads/{branch}", fork)
    except Exception:   # the evidence and reset are best effort: the refusal stands
        pass
    return "policy files touched: " + ", ".join(found)[:240]
