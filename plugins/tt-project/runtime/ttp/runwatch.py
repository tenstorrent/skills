# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A model-free live record of each running worker and reviewer run (not the coordinator), kept in
state/runwatch.json by a daemon step beside meter_running, so the coordinator, `ttp runs live` and
the web app see what a run is doing while it runs.

Per run: its last `ttp note` and `checks:` lines (progress.md); its last tool call and how long it
has been pending, and the failed calls among its last 20 (the provider's `activity`, read from a
stored byte offset of output.jsonl; a provider without one leaves these empty); the files its
worktree changed (git: workers often edit through shell heredocs that no tool call names), cached by
HEAD and the index mtime for at most GIT_TTL_S; spend against the task budget and the share of its
wall limit used; its attempt; and whether it read the last update written to its steer.md.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import gitmemo
from .project import Project, write_json
from .providers import get_provider

FILE = "runwatch.json"
RECENT = 20          # tool calls kept per run, for the failed count
GIT_TTL_S = 300      # a cached file list is redone after this, even with HEAD and the index unchanged
MAX_FILES = 50
TAIL_BYTES = 16384   # the end of progress.md and steer.md read for their last lines


def path(p: Project) -> Path:
    return p.state / FILE


def load(p: Project) -> dict:
    try:
        data = json.loads(path(p).read_text())
    except (OSError, ValueError):
        return {"updated": None, "runs": {}}
    return data if isinstance(data, dict) and isinstance(data.get("runs"), dict) else {"updated": None, "runs": {}}


def tick(p: Project, now: float | None = None, write: bool = True) -> dict:
    """Refresh every running worker and reviewer run's entry; drop the ones that ended."""
    now = time.time() if now is None else now
    old = load(p)["runs"]
    rows = p.db.q("SELECT r.id, r.task, r.role, r.provider, r.started, r.dir, r.cost_usd, t.title, t.budget_usd, "
                  "t.spent_usd, t.attempts, t.max_attempts FROM runs r LEFT JOIN tasks t ON t.id=r.task "
                  "WHERE r.status='running' AND r.role!='coordinator' ORDER BY r.id")
    base: list[str] = []   # the project's base ref, found once per tick and only when a worktree needs it
    runs = {}
    for r in rows:
        key = str(r["id"])
        try:
            runs[key] = _entry(p, r, old.get(key) or {}, now, base)
        except Exception as e:   # one bad run never costs the others their entries
            runs[key] = {**(old.get(key) or {}), "run": r["id"], "task": r["task"], "error": str(e)[:200]}
    data = {"updated": now, "runs": runs}
    if write:
        write_json(path(p), data)
    return data


def _entry(p: Project, r: dict, old: dict, now: float, base: list[str]) -> dict:
    run_dir = Path(r["dir"]) if r["dir"] else p.runs / str(r["id"])
    started = float(r["started"] or now)
    e = {"run": r["id"], "task": r["task"], "role": r["role"], "provider": r["provider"], "title": r["title"],
         "attempt": r["attempts"], "max_attempts": r["max_attempts"], "started": started, "updated": now}
    if not run_dir.is_dir():
        return {**e, "gone": True}
    try:
        spec = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        spec = {}
    spec = spec if isinstance(spec, dict) else {}
    e["note"], e["checks"] = _progress(run_dir / "progress.md")
    e.update(_tools(r["provider"], run_dir / "output.jsonl", old, now))
    e.update(_files(p, str(spec.get("cwd") or ""), old, now, base))
    cost, budget = float(r["cost_usd"] or 0), r["budget_usd"]
    task_usd = float(r["spent_usd"] or 0) + cost
    e["spend"] = {"run_usd": round(cost, 4), "task_usd": round(task_usd, 4), "budget_usd": budget,
                  "share": round(task_usd / budget, 3) if budget else None}
    limit = spec.get("timeout_s")
    e["wall"] = {"elapsed_s": round(now - started), "limit_s": limit,
                 "share": round((now - started) / float(limit), 3) if limit else None}
    e["steer"] = _steer(run_dir, r["provider"])
    return e


def _tail_lines(f: Path) -> list[str]:
    try:
        with open(f, "rb") as fh:
            fh.seek(max(f.stat().st_size - TAIL_BYTES, 0))
            return [ln.strip() for ln in fh.read().decode(errors="replace").splitlines() if ln.strip()]
    except OSError:
        return []


def _progress(f: Path) -> tuple[str, str]:
    """The last note line and the last `checks:` line ("" when none)."""
    lines = _tail_lines(f)
    checks = next((ln for ln in reversed(lines) if " checks: " in ln), "")
    note = next((ln for ln in reversed(lines) if " checks: " not in ln), "")
    return note[:200], checks[:200]


def _tools(provider: str, out: Path, old: dict, now: float) -> dict:
    offset, recent, last = int(old.get("offset") or 0), list(old.get("recent") or []), old.get("last_tool")
    try:
        size = out.stat().st_size
    except OSError:
        size = 0
    if size < offset:   # the stream was replaced (a restarted run): read it afresh
        offset, recent, last = 0, [], None
    try:
        events, offset = get_provider(provider).activity(out, offset)
    except Exception:
        events = []
    for ev in events:
        ts = ev.get("ts") or now
        known = next((c for c in reversed(recent) if ev["id"] and c["id"] == ev["id"]), None)
        if ev["kind"] == "call" or known is None:
            known = {"id": ev["id"], "tool": ev["tool"], "error": None}
            recent.append(known)
            if ev["kind"] == "call" or ev["tool"]:   # a bare result of a call never seen names no tool
                last = {"id": ev["id"], "tool": ev["tool"], "input_head": ev["input_head"], "since": ts,
                        "done": False}
        if ev["kind"] == "result":
            known["error"] = bool(ev["is_error"])
            if last and last["id"] == ev["id"]:
                last.update(done=True, is_error=bool(ev["is_error"]), ended=ts)
    recent = recent[-RECENT:]
    return {"offset": offset, "recent": recent, "last_tool": last,
            "pending_s": round(now - last["since"]) if last and not last["done"] else None,
            "failed_last_20": sum(1 for c in recent if c.get("error"))}


def _git(cwd: str, *args: str) -> str | None:
    # GIT_OPTIONAL_LOCKS=0: a plain status refreshes and rewrites a worker's index under index.lock,
    # which fails the worker's own add/commit running at the same moment; the observer only reads.
    try:
        r = gitmemo.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=20, read=True,
                        stdin=subprocess.DEVNULL, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _mtime(f: Path) -> int:
    try:
        return f.stat().st_mtime_ns
    except OSError:
        return 0


def _files(p: Project, cwd: str, old: dict, now: float, base: list[str]) -> dict:
    """The worktree's uncommitted changes plus what its commits changed since the base. Only for a
    run in its own checkout: the project root's status is everyone's."""
    if not cwd or not Path(cwd).is_dir() or Path(cwd).resolve() == Path(p.root).resolve():
        return {"files": [], "files_n": 0, "git": None}
    got = (_git(cwd, "rev-parse", "HEAD", "--absolute-git-dir") or "").split()
    if len(got) != 2:
        return {"files": old.get("files") or [], "files_n": old.get("files_n") or 0, "git": old.get("git")}
    head, gitdir = got
    cached = old.get("git") or {}
    if cached.get("key") == [head, _mtime(Path(gitdir) / "index")] and now - float(cached.get("ts") or 0) < GIT_TTL_S:
        return {"files": old.get("files") or [], "files_n": old.get("files_n") or 0, "git": cached}
    status = _git(cwd, "status", "--porcelain", "--untracked-files=all") or ""
    index = _mtime(Path(gitdir) / "index")   # after the status, which may refresh the index
    dirty = {ln[3:].split(" -> ")[-1].strip('"') for ln in status.splitlines() if len(ln) > 3}
    committed = cached.get("committed") if cached.get("head") == head else None
    if committed is None:
        if not base:
            from .worktree import base_ref
            try:
                base.append(base_ref(p) or "")
            except Exception:   # no base to diff against: the uncommitted changes still show
                base.append("")
        diff = _git(cwd, "diff", "--name-only", f"{base[0]}...HEAD") if base[0] else None
        committed = sorted((diff or "").split())
    files = sorted(dirty | set(committed))
    return {"files": files[:MAX_FILES], "files_n": len(files),
            "git": {"key": [head, index], "ts": now, "head": head, "committed": committed}}


def _steer(run_dir: Path, provider: str) -> dict | None:
    """The last update written to steer.md and whether the run read it: up to steer.offset with the
    update hook; a provider without one reads steer.md itself, so its read state is unknown (None)."""
    from .hook import OFFSET_FILE, STEER_FILE
    f = run_dir / STEER_FILE
    try:
        size = f.stat().st_size
    except OSError:
        return None
    try:
        seen = int((run_dir / OFFSET_FILE).read_text())
    except (OSError, ValueError):
        seen = 0
    lines = _tail_lines(f)
    at = max((i for i, ln in enumerate(lines) if ln.startswith("## Update")), default=None)
    try:
        hooked = bool(getattr(get_provider(provider), "steer_hook", False))
    except Exception:
        hooked = False
    return {"last": lines[at][3:] if at is not None else "",
            "text": " ".join(lines[at + 1:at + 3])[:160] if at is not None else "",
            "size": size, "offset": seen, "read": (seen >= size) if hooked else None}


def doing(e: dict) -> str:
    """One line on what a run is doing now: its pending tool call, or the last one."""
    t = e.get("last_tool")
    if not t:
        return ""
    if not t.get("done"):
        return f"{t['tool']}: {t['input_head']} (running {_dur(e.get('pending_s') or 0)})"
    return f"last {t['tool']}: {t['input_head']}" + (" (failed)" if t.get("is_error") else "")


def _dur(s: float) -> str:
    return f"{int(s)}s" if s < 120 else f"{int(s // 60)}m" if s < 7200 else f"{s / 3600:.1f}h"


def _pct(v) -> str:
    return f" ({v:.0%})" if v is not None else ""


def text(data: dict, task: int | None = None) -> str:
    out = []
    for e in data.get("runs", {}).values():
        if task is not None and e.get("task") != task:
            continue
        head = f"run {e['run']} #{e.get('task')} {e.get('role')} {e.get('provider')}: {e.get('title') or ''}".rstrip()
        if e.get("gone"):
            out.append(f"{head}\n  run dir gone")
            continue
        sp, wall, st = e.get("spend") or {}, e.get("wall") or {}, e.get("steer")
        budget = f" of ${sp['budget_usd']:.2f}" if sp.get("budget_usd") else ""
        lines = [head, f"  attempt {e.get('attempt')}/{e.get('max_attempts')} · ${sp.get('task_usd', 0):.2f}"
                       f"{budget}{_pct(sp.get('share'))} · {_dur(wall.get('elapsed_s') or 0)} of wall"
                       f"{_pct(wall.get('share'))} · {e.get('failed_last_20', 0)} failed of last "
                       f"{len(e.get('recent') or [])} calls"]
        for label, val in (("note", e.get("note")), ("doing", doing(e)), ("checks", e.get("checks"))):
            if val:
                lines.append(f"  {label}: {val}")
        if e.get("files_n"):
            lines.append(f"  files ({e['files_n']}): {', '.join(e['files'][:8])}{' ...' if e['files_n'] > 8 else ''}")
        if st and st.get("last"):
            state = {True: "read", False: "UNREAD", None: "read state unknown"}[st.get("read")]
            lines.append(f"  steer: {st['last']} ({state})")
        out.append("\n".join(lines))
    return "\n".join(out) if out else "no running worker or reviewer runs"
