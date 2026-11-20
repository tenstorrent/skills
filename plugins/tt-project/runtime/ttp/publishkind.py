"""A task that publishes a code task's branch runs as kind `code`, whatever kind it was added as.

Only a code task may publish a branch (`ttp push --own`) or update a PR, so a harness, work, plan or
question task whose spec has a `pr_branch: <branch>` line, or a `ttp push --own` step and the branch
of a code task by name (never one inferred from the task it continues), is corrected when it is added (coordinator task_add, the
daemon's continue hops) and again before dispatch (a spec changed later): kind code, a
`pr_branch:<branch>` label and a `continues:<owner>` link to the code task whose branch it is, so the
own-push target check accepts it. An owner that cannot be worked out unambiguously is refused
(MISMATCH), never guessed."""
from __future__ import annotations

import json
import re

from . import worktree
from .db import continues_id

MISMATCH = "cannot publish branch"
SKIP = ("code", "review")   # kinds that publish as they are
OWN_PUSH = re.compile(r"\bttp\s+push\b[^\n`]*?--own\b(?!-)")
OWN_BRANCH = re.compile(r"ttp/t(\d+)-\S+")
NAMED = re.compile(r"(?<![\w/.-])ttp/t(\d+)-[\w./-]*[\w-]")
FENCED = re.compile(r"^[ \t]*```.*?^[ \t]*```", re.M | re.S)   # a quoted block's lines are not its own


def _refuse(kind: str, branch: str, how: str, why: str) -> ValueError:
    return ValueError(f"kind {kind} {MISMATCH} {branch} ({how}){why}; add it as kind code with "
                      f"continues:<owner id>, the code task whose branch it is")


def _own_name(t: dict) -> str:
    return str(t.get("branch") or "") or f"ttp/t{t['id']}-{worktree.slug(str(t.get('title') or ''))}"


def _carriers(db, branch: str) -> list[dict]:
    """The code tasks that carry `branch` (as their row's branch or a pr_branch: label), the latest of
    each continue lineage only (a PR fix that continues the task it fixes is that task's lineage)."""
    rows = db.q("SELECT * FROM tasks WHERE kind='code' AND (branch=? OR labels LIKE ?) ORDER BY id",
                (branch, f'%{json.dumps("pr_branch:" + branch)}%'))
    rows = [r for r in rows if r["branch"] == branch or worktree.recorded_pr_branch(r) == branch]
    earlier: set[int] = set()
    for r in rows:
        c, seen = continues_id(r), {r["id"]}
        while c is not None and c not in seen:
            seen.add(c)
            earlier.add(c)
            t = db.task(c)
            c = continues_id(t) if t else None
    return [r for r in rows if r["id"] not in earlier]


def _owner(db, kind: str, branch: str, how: str) -> dict | None:
    """The code task `branch` belongs to, None for a branch no task carries (a named branch such as a
    user's PR branch publishes without one); raises MISMATCH when it is not one code task."""
    if m := OWN_BRANCH.fullmatch(branch):
        t = db.task(int(m.group(1)))
        if not t:
            raise _refuse(kind, branch, how, f": no task #{m.group(1)} owns it")
        if t["kind"] != "code":
            raise _refuse(kind, branch, how, f": #{t['id']} is kind {t['kind']}, not code")
        return t
    found = _carriers(db, branch)
    if len(found) > 1:
        raise _refuse(kind, branch, how, ": code tasks " + ", ".join(f"#{t['id']}" for t in found)
                      + " carry it")
    return found[0] if found else None


def resolve(db, kind: str, spec: str) -> dict | None:
    """What a task of `kind` whose spec is `spec` publishes: {"branch", "owner" (a code task row or None),
    "how"}, or None when it publishes no branch or its kind may publish (SKIP). Raises ValueError (MISMATCH) when the owner is not unambiguous."""
    if kind in SKIP or not spec:
        return None
    spec = FENCED.sub("", spec)
    lines = sorted({m.group("branch") for m in worktree.SPEC_PR_BRANCH.finditer(spec)})
    if len(lines) > 1:
        raise _refuse(kind, ", ".join(lines), "pr_branch: lines", ": it names several")
    if lines:
        return {"branch": lines[0], "owner": _owner(db, kind, lines[0], "pr_branch:"), "how": "pr_branch:"}
    if not OWN_PUSH.search(spec):
        return None
    # A `ttp push --own` step onto a code task's branch its spec names; a spec naming none publishes
    # nothing (it may only mention the command, e.g. 'do not run it').
    named = {}
    for m in NAMED.finditer(spec):
        t = db.task(int(m.group(1)))
        if t and t["kind"] == "code" and m.group(0) == _own_name(t):
            named[t["id"]] = (t, m.group(0))
    if not named:
        return None
    if len(named) > 1:
        branches = ", ".join(b for _, b in named.values())
        raise _refuse(kind, branches, "ttp push --own", ": it names several code tasks' branches")
    t, branch = next(iter(named.values()))
    return {"branch": branch, "owner": t, "how": "ttp push --own"}


def labels(old: list, pub: dict) -> list:
    """`old` labels with `pub`'s branch (pr_branch:<branch>, replacing any branch label) and its
    owner's continues: link."""
    out = [lb for lb in old if not (isinstance(lb, str) and lb.startswith(("pr_branch:", "rebases:"))
                                    or lb == worktree.NO_PR_BRANCH)] + [f"pr_branch:{pub['branch']}"]
    if pub["owner"] and f"continues:{pub['owner']['id']}" not in out:
        out.append(f"continues:{pub['owner']['id']}")
    return out


def note(task_id, kind: str, pub: dict) -> str:
    own = f" and continues #{pub['owner']['id']}, whose branch it is" if pub["owner"] else ""
    return (f"#{task_id} was kind {kind} but publishes {pub['branch']} ({pub['how']}): it runs as kind code "
            f"with pr_branch:{pub['branch']}{own}")
