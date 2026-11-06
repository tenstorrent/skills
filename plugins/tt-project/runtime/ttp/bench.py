# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Replay bench, model-free part (`ttp bench prep`): pick landed tasks a later run can replay to
compare models, build a clean workspace at each one's base commit and hold out its tests.

A task qualifies when it is a done code or work task created on or after `since` (the later of the
compared models' release dates, so neither saw the change), landed through one push queue row, and
its landed change adds or changes tests (addedtests.added). Its base is the commit before its
landed commits on the push branch. Excluded, with the reason: a spec that quotes the fix (QUOTE_LINES
added lines verbatim) and held-out tests that use a repository symbol the base lacks and the spec does
not name. Each kept task gets <out>/<id>/workspace, `git archive` of the base in a fresh
repository with one commit and no remotes, tags or reflog, and <out>/<id>/heldout, the head's copy
of each held-out test file. manifest.json names them with their sha256; manifest.sha256 pins it.
The bench names no model: what it compares is the runner's choice."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import addedtests, gitmemo
from .project import durable_write

SMALL = 150        # changed non-test lines at most for the small stratum
QUOTE_LINES = 3    # added lines of the fix quoted verbatim in the spec that exclude it
QUOTE_LEN = 30     # an added line counts as quotable from this many characters
_EPOCH = {"GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000"}


def _git(repo, *args: str, **kw) -> subprocess.CompletedProcess:
    return gitmemo.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=None, **kw)


def _out(repo, *args: str) -> str:
    r = _git(repo, *args)
    return r.stdout.strip() if r.returncode == 0 else ""


def landing(repo: Path, rows: list[dict]) -> tuple[str, str, str]:
    """(base, head, "") of a change the push queue landed in `rows` (its rows with a landed commit),
    or ("", "", why) when the range on the push branch is not clear."""
    if len(rows) != 1:
        return "", "", f"landed in {len(rows)} pushes" if rows else "no landed commit recorded"
    land, head = rows[0]["landed_sha"], rows[0]["head"] or ""
    fork = _out(repo, "merge-base", head, land) if head else ""
    if not fork or not _out(repo, "rev-parse", "--verify", "-q", f"{land}^{{commit}}"):
        return "", "", "its landed commit or branch head is not in the repository"
    if fork == _out(repo, "rev-parse", head):
        return "", "", "it landed as a fast-forward, so where its commits start is unknown"
    n = int(_out(repo, "rev-list", "--count", "--no-merges", f"{fork}..{head}") or 0)
    base = _out(repo, "rev-parse", "--verify", "-q", f"{land}~{n}") if n else ""
    same = base and _out(repo, "rev-list", "--count", "--merges", f"{base}..{land}") == "0" and (
        _out(repo, "diff", "--name-only", fork, head) == _out(repo, "diff", "--name-only", base, land))
    return (base, land, "") if same else ("", "", "its landed commits do not match its branch")


def _changed(repo: Path, base: str, head: str) -> dict[str, list[str]]:
    """{path: added lines} of the non-test files the change touches; "": lines changed in them."""
    out, lines = {}, 0
    for row in _out(repo, "diff", "--numstat", base, head).splitlines():
        add, rem, path = (row.split("\t", 2) + ["", ""])[:3]
        if path and not addedtests._test_file(path):
            out[path] = []
            lines += int(add) + int(rem) if add.isdigit() and rem.isdigit() else 0
    for path in out:
        diff = _out(repo, "diff", "-U0", base, head, "--", path).splitlines()
        out[path] = [x[1:].strip() for x in diff if x.startswith("+") and not x.startswith("+++")]
    return {**out, "": [str(lines)]}


def quotes(spec: str, added: dict[str, list[str]]) -> int:
    """How many distinct added non-test lines of QUOTE_LEN characters or more `spec` quotes."""
    body = " ".join(spec.split())
    return len({x for p, xs in added.items() if p for x in xs if len(x) >= QUOTE_LEN and " ".join(x.split()) in body})


def unknown_symbols(repo: Path, base: str, head: str, files: list[str], spec: str) -> list[str]:
    """The repository modules and their attributes the held-out test `files` use (at `head`) that
    neither exist at `base` nor appear in `spec`: a hidden interface no candidate could guess."""
    mods: dict[str, list[str]] = {}
    for path in _out(repo, "ls-tree", "-r", "--name-only", head).splitlines():
        p = Path(path)
        stem = p.parent.name if p.name == "__init__.py" else p.stem
        if p.suffix == ".py":
            mods.setdefault(stem, []).append(path)
    words = set(re.findall(r"\w+", spec))
    known: dict[str, str] = {}

    def at_base(mod: str) -> str:
        if mod not in known:
            known[mod] = "\n".join(addedtests._show(repo, base, f) for f in mods[mod])
        return known[mod]
    used: set[tuple[str, str]] = set()
    for path in files:
        try:
            tree = ast.parse(addedtests._show(repo, head, path))
        except (SyntaxError, ValueError):
            continue
        bound: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                parent = node.module.split(".")[-1]
                for a in node.names:
                    if a.name in mods:
                        bound[a.asname or a.name] = a.name
                        used.add((a.name, ""))
                    elif parent in mods:
                        used.add((parent, a.name))
            elif isinstance(node, ast.Import):
                for a in node.names:
                    last = a.name.split(".")[-1]
                    if last in mods and (a.asname or "." not in a.name):
                        bound[a.asname or a.name] = last
                        used.add((last, ""))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in bound:
                used.add((bound[node.value.id], node.attr))
    out = []
    for mod, attr in sorted(used):
        name = attr or mod
        have = at_base(mod) and (not attr or re.search(rf"\b{re.escape(attr)}\b", at_base(mod)))
        if not have and name not in words:
            out.append(f"{mod}.{attr}" if attr else mod)
    return out


def _archive(repo: Path, rev: str, dest: Path) -> None:
    dest.mkdir(parents=True)
    tar = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", rev], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=tar.stdout, check=True)


def workspace(repo: Path, base: str, dest: Path) -> str:
    """A fresh repository at `dest` holding `base`'s tracked files in one commit, with no remotes,
    tags, reflog or other history; returns its tree id (equal to `base`'s tree)."""
    _archive(repo, base, dest)
    return seal(dest)


def seal(dest: Path) -> str:
    """Make the files in `dest` a fresh repository with one commit and no remotes, tags or reflog;
    returns its tree id."""
    env = {**os.environ, **_EPOCH}
    git = ["git", "-C", str(dest), "-c", "user.name=bench", "-c", "user.email=bench@localhost",
           "-c", "core.logAllRefUpdates=false", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(dest)], check=True, capture_output=True)
    subprocess.run([*git, "add", "-A", "-f"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "--no-verify", "-m", "base"], check=True, capture_output=True, env=env)
    shutil.rmtree(dest / ".git" / "logs", ignore_errors=True)
    return _out(dest, "rev-parse", "HEAD^{tree}")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prep(db, repo: Path, out: Path, since: float, limit: int = 0) -> dict:
    """Write the bench to `out` (which must not exist yet) and return its manifest."""
    from . import landed
    out.mkdir(parents=True)
    tasks = db.q("SELECT id, kind, title, spec, created FROM tasks WHERE status='done' AND kind IN ('code','work') "
                 "AND created >= ? ORDER BY id", (since,))
    rows = landed.Rows(db, [t["id"] for t in tasks])
    pushed = db.q("SELECT task, branch, head, landed_sha FROM push_queue WHERE status IN ('pushed','landed') "
                  "AND landed_sha IS NOT NULL AND landed_sha != '' ORDER BY id")
    kept, excluded = [], []
    for t in tasks:
        if limit and len(kept) >= limit:
            break
        carry = landed.carriers(db, t["id"], rows)
        branches = {v[0] for v in carry.values() if v[0]}
        base, head, why = landing(repo, [r for r in pushed if r["task"] in carry or r["branch"] in branches])
        tests = addedtests.added(repo, base, head) if base else []
        changed = _changed(repo, base, head) if base else {}
        if not why and not tests:
            why = "its landed change adds or changes no test"
        if not why and (n := quotes(t["spec"], changed)) >= QUOTE_LINES:
            why = f"its spec quotes {n} lines of the fix"
        if not why and (hidden := unknown_symbols(repo, base, head, sorted({p for p, _ in tests}), t["spec"])):
            why = f"its held-out tests use symbols the base lacks and the spec does not name: {', '.join(hidden)}"
        if why:
            excluded.append({"task": t["id"], "why": why})
            continue
        lines = int(changed[""][0])
        here = out / str(t["id"])
        tree = workspace(repo, base, here / "workspace")
        held = {}
        for path in sorted({p for p, _ in tests}):
            dest = here / "heldout" / path
            durable_write(dest, addedtests._show(repo, head, path), sync=False)
            held[path] = _sha(dest)
        kept.append({"task": t["id"], "kind": t["kind"], "title": t["title"], "spec": t["spec"],
                     "created": t["created"], "base": base, "head": head, "tree": tree,
                     "stratum": "small" if lines <= SMALL else "large", "changed_lines": lines,
                     "tests": [f"{p}::{n}" for p, n in tests], "heldout": held})
    manifest = {"since": since, "made": time.time(), "repo": str(repo.resolve()), "tasks": kept,
                "excluded": excluded, "counts": {s: sum(k["stratum"] == s for k in kept) for s in ("small", "large")}}
    durable_write(out / "manifest.json", json.dumps(manifest, indent=1) + "\n")
    durable_write(out / "manifest.sha256", f"{_sha(out / 'manifest.json')}  manifest.json\n")
    return manifest
