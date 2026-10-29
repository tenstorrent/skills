# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The check a daemon runs on the first start of a new boot: did a power cut damage the harness?

The harness repository gets a bounded `git fsck --connectivity-only`; its charter, config, memory
entries and memory index are checked for the marks of a cut write (empty, NUL bytes, cut short)
and put back from the harness's last commit (the config from its last good copy). Task worktrees
of unfinished tasks get a `git status`; one that fails is reported, never repaired. The user's own
repositories are never fsck'd. Every kept task worktree's repository must still have its .git
directory (a copied or restored project can bring the folders without it), and a project.json
made on another machine is reported once: its machine-specific settings may not hold here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .project import Project, bad_sections, durable_write, hostname

FSCK_TIMEOUT_S = 20
STATUS_TIMEOUT_S = 10
WORKTREES_BUDGET_S = 30
_POINTER = re.compile(r"\]\(memory/([^)]+\.md)\)")


def _git(cwd: Path, *args: str, timeout: float) -> subprocess.CompletedProcess:
    # No optional locks: a status must never fight a running worker over its index.
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=timeout,
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})


def _head(p: Project, rel: str) -> bytes | None:
    """The file as the harness's last commit has it; None when it is not there or git fails."""
    try:
        r = _git(p.harness, "show", f"HEAD:{rel}", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _text(data: bytes | None) -> str | None:
    """The content as text, or None when it shows a cut write: empty, NUL bytes, not UTF-8."""
    if not data or b"\0" in data:
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        return None


def _entry_ok(text: str | None) -> bool:
    return bool(text) and text.startswith("---\n") and "\n---\n" in text[3:] and bool(text.split("\n---\n", 1)[1].strip())


def _cut_short(data: bytes, head: bytes | None) -> bool:
    # Cut mid-line: a hand edit that drops the last section ends on a newline and is kept.
    return bool(head) and len(data) < len(head) and head.startswith(data) and not data.endswith(b"\n")


def looks_cut(p: Project, rel: str, data: bytes) -> bool:
    """Whether harness file `rel` (CHARTER*, MEMORY.md or under memory/) holding `data` shows the
    marks check_harness repairs, so a sweep never commits a cut write over the good copy."""
    text = _text(data)
    if text is None:
        return True
    if rel.startswith("memory/"):
        return not text.endswith("\n") or (text.startswith("---") and not _entry_ok(text))
    return _cut_short(data, _head(p, rel))


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def check_harness(p: Project) -> dict:
    """Check and repair the harness. Returns {"fsck", "restored", "bad"}: `bad` lists what could not
    be repaired, `restored` what was put back."""
    out: dict = {"fsck": "skipped", "restored": [], "bad": []}
    if not (p.harness / ".git").exists():
        return out
    try:
        r = _git(p.harness, "fsck", "--connectivity-only", "--no-progress", timeout=FSCK_TIMEOUT_S)
        out["fsck"] = "ok" if r.returncode == 0 else "failed"
        if r.returncode:
            msg = (r.stderr or r.stdout).decode(errors="replace").strip().splitlines()[:3]
            out["bad"].append("harness git fsck failed: " + " / ".join(msg)[:300])
    except subprocess.TimeoutExpired:
        out["fsck"] = f"timeout after {FSCK_TIMEOUT_S} s"
    except (OSError, subprocess.SubprocessError) as e:
        out["fsck"] = f"error: {e}"[:200]
    restored: list[Path] = []

    def put_back(path: Path, data: bytes, why: str) -> None:
        old = _read(path)
        if old.strip(b"\0 \n"):   # what is replaced is kept for a human, never lost
            durable_write(p.state / "damaged" / f"{path.name}.{time.strftime('%Y%m%dT%H%M%S')}", old)
        durable_write(path, data)
        restored.append(path)
        out["restored"].append(f"{path.relative_to(p.harness)} ({why})")

    # Memory entries: one fact per file, with front matter (entries from before it have none).
    entries = sorted(p.memory_dir.glob("*.md")) if p.memory_dir.is_dir() else []
    for f in entries:
        rel = str(f.relative_to(p.harness))
        data = _read(f)
        text = _text(data)
        if text is not None and text.endswith("\n") and (_entry_ok(text) or not text.startswith("---")):
            continue
        head = _head(p, rel)
        if head == data:
            continue   # committed like this: not a cut write
        if _text(head) is not None:
            put_back(f, head, "from the last commit")
            continue
        # Never committed: nothing to restore from. Set aside, out of every prompt, for a human.
        aside = p.state / "damaged" / f.name
        aside.parent.mkdir(parents=True, exist_ok=True)
        f.replace(aside)
        out["restored"].append(f"{rel} (damaged, never committed: moved to {aside.relative_to(p.base)})")
    # The memory index: a pointer per live entry, whatever the cut left.
    data, head = _read(p.memory_index), _head(p, "MEMORY.md")
    text, head_text = _text(data), _text(head) or ""
    base = (head_text or "# Memory index\n") if text is None or _cut_short(data, head) else text
    if not base.endswith("\n"):
        base = base[:base.rfind("\n") + 1] or "# Memory index\n"   # a cut last line
    live = [f.name for f in entries if f.exists()]
    old = {m.group(1): x if x.endswith("\n") else x + "\n"
           for x in head_text.splitlines(keepends=True) if (m := _POINTER.search(x))}
    lines, listed = [], set()
    for x in base.splitlines(keepends=True):
        m = _POINTER.search(x)
        if m and (m.group(1) not in live or m.group(1) in listed):
            continue
        if m:
            listed.add(m.group(1))
        lines.append(x)
    for name in live:
        if name not in listed:
            body = _text(_read(p.memory_dir / name)) or ""
            kind = re.search(r"^kind:\s*(\S+)", body, re.M)
            first = (body.split("\n---\n", 1)[-1].strip().splitlines() or [name[:-3]])[0][:80]
            lines.append(old.get(name) or
                         f"- [{first}](memory/{name}) ({kind.group(1) if kind else name.split('-', 1)[0]})\n")
    if "".join(lines) != text:
        put_back(p.memory_index, "".join(lines).encode(), "rebuilt from the last commit and the entries")
    # The charter: appended to and committed each time, so HEAD is at least as long.
    data, head = _read(p.charter_path), _head(p, "CHARTER.md")
    if _text(data) is None or _cut_short(data, head):
        if _text(head) is not None:
            put_back(p.charter_path, head, "from the last commit")
        elif p.charter_path.exists():
            out["bad"].append("CHARTER.md is damaged and has no good committed copy")
    # The config: the last good copy is newer than any commit of it.
    if p.config_path.exists():
        try:
            json.loads(_read(p.config_path))
        except ValueError:
            good = _read(p.state / "project.last-good.json")
            head = _head(p, "project.json")
            for cand, why in ((good, "from its last good copy"), (head, "from the last commit")):
                try:
                    if bad_sections(json.loads(cand or b"")):   # never restore a section saved as one value
                        continue
                except ValueError:
                    continue
                put_back(p.config_path, cand, why)
                break
            else:
                out["bad"].append("project.json is damaged and has no good copy")
    if restored:
        p.commit_harness(restored, "integrity: restored after a reboot")
    return out


def check_worktrees(paths: list[tuple[int, Path]]) -> list[dict]:
    """Task worktrees whose `git status` fails ({"task", "path", "error"}). Bounded per worktree and
    in total; one that is only slow is not reported."""
    bad, deadline = [], time.monotonic() + WORKTREES_BUDGET_S
    for task, path in paths:
        if time.monotonic() > deadline:
            break
        if not path.exists():
            continue
        try:
            r = _git(path, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules",
                     timeout=STATUS_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode:
            err = (r.stderr or r.stdout).decode(errors="replace").strip().splitlines()[:2]
            bad.append({"task": task, "path": str(path), "error": " / ".join(err)[:300]})
    return bad


def check_clones(paths: list[tuple[int, Path]]) -> list[dict]:
    """Repositories of kept task worktrees that have no .git directory ({"repo", "tasks", "empty"}):
    an empty or partly copied clone the worktrees can no longer work from. Reads files only."""
    from .worktree import _repo_of
    bad: dict[str, dict] = {}
    for task, path in paths:
        repo = _repo_of(path) if path.exists() else None
        if repo is None or (repo / ".git").is_dir():
            continue
        try:
            empty = not any(repo.iterdir())
        except OSError:
            empty = True
        bad.setdefault(str(repo), {"repo": str(repo), "tasks": [], "empty": empty})["tasks"].append(task)
    return list(bad.values())


def host_change(p: Project) -> dict | None:
    """{"was", "now"} when project.json names another machine than this one, else None. Another name
    for this machine (its FQDN, a machines-list alias or ssh alias for it: machines.names_here) is
    no move: project.json's host is quietly rewritten to this machine's short name (when the file
    reads cleanly), so later checks compare plainly and skip the ssh lookup."""
    try:
        was = p.raw_config().get("host")
    except Exception:
        return None
    now = hostname()
    if not isinstance(was, str) or not was or was == now:
        return None
    from . import machines
    if not machines.names_here(was):
        return {"was": was, "now": now}
    if p.config_status == "ok":
        try:
            p.set_config("host", now)
        except Exception:
            pass
    return None


def host_text(change: dict) -> str:
    return (f"This project was set up on machine '{change['was']}' but its daemon now runs on "
            f"'{change['now']}': the project folder was moved, copied or restored. Check its machine-specific "
            f"settings (runners, machines, ssh aliases, paths) and its repositories, then set project.json's "
            f"host to '{change['now']}' to confirm the move. A move changes no task labels: re-point open tasks whose "
            f"resource labels name machines no longer in use with task_update `resources` (a running one: cancel it "
            f"and task_add one that `continues` it).")[:2000]


def check(p: Project, worktrees: list[tuple[int, Path]] = (), kept: list[tuple[int, Path]] = ()) -> dict:
    """`worktrees`: unfinished tasks' worktrees, `git status`ed; `kept`: every kept task worktree,
    whose repository must have its .git directory."""
    t0 = time.monotonic()
    from .worktree import dangling_root
    out = check_harness(p)
    if (why := dangling_root(p)):
        out["bad"].append(why + ", so no task worktree can be made")
    out["worktrees"] = check_worktrees(list(worktrees))
    out["clones"] = check_clones(list(kept))
    out["seconds"] = round(time.monotonic() - t0, 3)
    return out


def problem_text(res: dict) -> str | None:
    """The alert for what the check could not repair, or None when nothing is left."""
    parts = list(res.get("bad") or [])
    parts += [f"task #{w['task']}'s worktree fails git status ({w['error']})" for w in res.get("worktrees") or []]
    parts += [f"repository {c['repo']} ({'empty' if c['empty'] else 'not empty'}) has no .git directory, so the "
              f"worktree(s) of task(s) {', '.join(f'#{t}' for t in c['tasks'])} cannot work from it: an empty or "
              f"partly copied clone; clone it again or restore its .git" for c in res.get("clones") or []]
    if not parts:
        return None
    return ("After a reboot the harness check found damage it could not repair; nothing was changed in "
            "your code repositories. " + "; ".join(parts))[:2000]
