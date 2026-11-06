# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Cursor CLI hook endpoint: `python -m ttp.cursorhook <event>`, with Cursor's hook payload on stdin.

It translates Cursor's payloads to the Claude Code ones `ttp.hook` takes (pre_tool_use, post_tool_use,
stop) and their replies back, so a Cursor worker gets the same guards, live updates and Stop hand-off
guard as a Claude one; hook.py's own behaviour is unchanged. providers/cursor.py installs the run's
`.cursor/hooks.json` in the run's cwd (install) and runner.remove_private undoes it (undo).

Evidence (read from the bundled code of Cursor CLI build 2026.10.22-27dcf5f, `agent --help`; not run
live, as no logged-in Cursor account was at hand):
- `--workspace <dir>` (or the cwd) is the project: its `.cursor/hooks.json` is loaded with the
  enterprise, team and user ones (`loadProjectHooks` defaults on, `--trust` skips the trust prompt).
  The file is `{"version": 1, "hooks": {<event>: [{"command", "matcher", "timeout" (seconds)}]}}`.
- A hook command runs in a bash shell in the workspace with the CLI's own environment plus CURSOR_*
  variables (`{...process.env, ...}`), so TTP_RUN_DIR, TTP_TASK and the rest reach it unchanged and
  the file can be the same for every run.
- `preToolUse` runs before every local tool and MCP call: {tool_name, tool_input, tool_use_id, cwd,
  parent_tool_call_id inside a subagent}. Tool names and inputs: Shell {command, cwd}, Read and
  Delete {file_path}, Write {file_path, content}, Grep {pattern, file_path, ...}, an MCP tool
  `MCP:<name>` with its arguments. Reply {permission: allow|deny|ask, user_message, agent_message,
  updated_input, additional_context}; deny refuses the call with user_message. Exit 2 also blocks;
  invalid JSON on stdout blocks a permission step, so the endpoint always prints valid JSON or nothing.
- `postToolUse` / `postToolUseFailure` reply {additional_context}, added to the tool's result.
- `stop` {status, loop_count}; reply {followup_message} sends it as the next user message (skipped
  once loop_count reaches the hook's loop_limit).
- `beforeShellExecution` and `beforeReadFile` exist too, but fire only inside the Shell and Read
  tools, after their preToolUse; so preToolUse alone carries the Bash guards and the denied reads
  (TTP_DENIED_READS: the paths providers/cursor.py passes, joined by os.pathsep; Read and Grep).

Mapping: Shell -> Bash; Write, Delete and Edit -> Write (the harness gh wrapper guard); MCP:<name> ->
mcp__cursor__<name> (aitag); Read and Grep -> the denied-read check. A subagent's call
(parent_tool_call_id) counts as agent_id, so updates wait for the worker's own call. stop: a
loop_count above 0 is stop_hook_active; a run that ended aborted or in error is not held.

Hooks file: merged into a `.cursor/hooks.json` that is there (its own entries kept), with a sidecar
`.cursor/.ttp-hooks.json` holding the original text and the runs using it (a read-only run's scratch
dir is shared). The last run out restores the original text, or removes the file. In a git checkout
the file and the sidecar are listed in info/exclude (the repository's shared one, so the lines
stay); a tracked hooks file is also marked skip-worktree for the run, so `git commit -a` leaves the harness entries out.
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import hook

DENIED_READS_ENV = "TTP_DENIED_READS"
HOOKS_REL = Path(".cursor") / "hooks.json"
SIDECAR_REL = Path(".cursor") / ".ttp-hooks.json"
RUN_STATE = "cursor-hooks.json"   # in the run dir: the cwd whose hooks file this run joined
MARK = "-m ttp.cursorhook "        # in each command the harness wrote
STOP_TIMEOUT_S = 150               # as providers/claude.py STOP_HOOK_TIMEOUT_S
NOT_HELD = ("aborted", "error")    # stop statuses a follow-up would not help
EDIT_TOOLS = {"Write", "Delete", "Edit", "StrReplace", "MultiEdit"}
READ_TOOLS = {"Read", "Grep"}
EVENT = {"preToolUse": "PreToolUse", "postToolUse": "PostToolUse", "postToolUseFailure": "PostToolUseFailure",
         "stop": "Stop"}


def _claude_payload(event: str, p: dict) -> dict:
    name = str(p.get("tool_name") or "")
    inp = p.get("tool_input")
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {"text": inp}
    inp = inp if isinstance(inp, dict) else {}
    out: dict = {"hook_event_name": EVENT[event], "cwd": str(p.get("cwd") or inp.get("cwd") or os.getcwd())}
    if p.get("parent_tool_call_id"):
        out["agent_id"] = str(p["parent_tool_call_id"])
    if name == "Shell":
        name, inp = "Bash", {"command": str(inp.get("command") or "")}
    elif name in EDIT_TOOLS:
        name = "Write"
    elif name.startswith("MCP:"):
        name = "mcp__cursor__" + name[4:]
    out.update(tool_name=name, tool_input=inp)
    if event == "stop":
        out["stop_hook_active"] = int(p.get("loop_count") or 0) > 0
    return out


def denied_read(p: dict) -> str | None:
    """Why a Read or Grep call may not read the path it names (TTP_DENIED_READS), or None."""
    if str(p.get("tool_name") or "") not in READ_TOOLS:
        return None
    inp = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
    name = str(inp.get("file_path") or inp.get("path") or "")
    denied = [d for d in os.environ.get(DENIED_READS_ENV, "").split(os.pathsep) if d]
    if not name or not denied:
        return None
    path = os.path.realpath(os.path.join(str(p.get("cwd") or os.getcwd()), os.path.expanduser(name)))
    if any(path == os.path.realpath(d) for d in denied):
        return "this file holds the user's credentials and a run must not read it"
    return None


def _deny(why: str) -> dict:
    return {"permission": "deny", "user_message": why, "agent_message": why}


def translate(event: str, p: dict) -> tuple[dict | None, object]:
    """Cursor's reply to `event` with payload `p`, and hook's after-reply callback (or None)."""
    if event == "preToolUse":
        if os.environ.get("TTP_RUN_DIR") and (why := denied_read(p)):
            return _deny(f"tt-project: {why}."), None
        out, _ = hook.pre_tool_use(_claude_payload(event, p))
        spec = (out or {}).get("hookSpecificOutput") or {}
        if spec.get("permissionDecision") == "deny":
            return _deny(str(spec.get("permissionDecisionReason") or "tt-project: refused.")), None
        reply: dict = {}
        if spec.get("additionalContext"):
            reply["additional_context"] = spec["additionalContext"]
        if isinstance(spec.get("updatedInput"), dict):
            new = spec["updatedInput"]
            reply.update(permission="allow", updated_input={**(p.get("tool_input") or {}),
                                                            "command": new.get("command")})
        return reply or None, None
    if event in ("postToolUse", "postToolUseFailure"):
        handler = hook.post_tool_use if event == "postToolUse" else hook.post_tool_use_failure
        q = _claude_payload(event, p)
        if event == "postToolUseFailure":
            q["error"] = str(p.get("error_message") or p.get("error") or "")
        out, delivered = handler(q)
        ctx = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext")
        return ({"additional_context": ctx} if ctx else None), delivered
    if event == "stop":
        if str(p.get("status") or "") in NOT_HELD:
            return None, None
        out, after = hook.stop(_claude_payload(event, p))
        if (out or {}).get("decision") == "block":
            return {"followup_message": str(out.get("reason") or "")}, after
        return None, after
    return None, None


def main(argv: list[str]) -> int:
    event = argv[1] if len(argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if event not in EVENT or not isinstance(payload, dict):
        return 0
    if event != "stop" and (run_dir := os.environ.get("TTP_RUN_DIR")):
        try:
            hook.mark_hooked(Path(run_dir))   # `ttp note` / `ttp steer` leave updates to this hook now
        except OSError:
            pass
    try:
        out, after = translate(event, payload)
        if out:
            json.dump(out, sys.stdout)
            sys.stdout.flush()
        if callable(after):
            after()
    except Exception:   # as hook.main: a broken guard never holds the run
        return 0
    return 0


# --- the run's hooks file ----------------------------------------------------------------------------

def hooks_config() -> dict:
    """The harness's entries in Cursor's hooks.json format."""
    runtime = str(Path(__file__).resolve().parents[1])
    cmd = f"env PYTHONPATH={shlex.quote(runtime)} {shlex.quote(sys.executable)} {MARK}"
    guarded = "|".join(sorted(EDIT_TOOLS | READ_TOOLS | {"Shell"})) + "|MCP:.*"   # a regex search on tool_name
    return {"preToolUse": [{"command": cmd + "preToolUse", "matcher": guarded, "timeout": 20}],
            "postToolUse": [{"command": cmd + "postToolUse", "timeout": 20}],
            "postToolUseFailure": [{"command": cmd + "postToolUseFailure", "timeout": 20}],
            "stop": [{"command": cmd + "stop", "timeout": STOP_TIMEOUT_S}]}


def _ours(entry) -> bool:
    return isinstance(entry, dict) and MARK in str(entry.get("command") or "")


def merged(original: str | None) -> str:
    """`original` (a hooks.json text, or None) with the harness's entries added after its own."""
    data = json.loads(original) if original and original.strip() else {}
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError("not a hooks.json object")
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    for event, entries in hooks_config().items():
        kept = [e for e in hooks.get(event) or [] if not _ours(e)]
        hooks[event] = kept + entries
    return json.dumps(data, indent=2) + "\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    from . import gitmemo   # install and undo run under the daemon's launch: its cap applies
    return gitmemo.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=20)


def _top(cwd: Path) -> Path | None:
    try:
        r = _git(cwd, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def _tracked(top: Path, rel: str) -> bool:
    try:
        return _git(top, "ls-files", "--error-unmatch", "--", rel).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _exclude(top: Path, cwd: Path) -> None:
    try:
        r = _git(top, "rev-parse", "--git-path", "info/exclude")
    except (OSError, subprocess.SubprocessError):
        return
    if r.returncode != 0 or not r.stdout.strip():
        return
    path = Path(r.stdout.strip())
    path = path if path.is_absolute() else top / path
    rel = os.path.relpath(cwd, top)
    base = "" if rel == "." else rel.replace(os.sep, "/") + "/"
    want = [f"/{base}{HOOKS_REL.as_posix()}", f"/{base}.cursor/.ttp-hooks.*"]   # the sidecar and its lock
    have = path.read_text(errors="replace").splitlines() if path.exists() else []
    add = [w for w in want if w not in have]
    if add:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(("\n" if have and have[-1] else "") + "# tt-project: Cursor runs' hooks file\n"
                    + "".join(a + "\n" for a in add))


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class _Locked:
    """An exclusive lock on the sidecar's lock file while a run joins or leaves."""

    def __init__(self, cwd: Path):
        (cwd / ".cursor").mkdir(exist_ok=True)
        self.f = open(cwd / SIDECAR_REL.with_suffix(".lock"), "a")

    def __enter__(self):
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def install(cwd: str, run_dir: str) -> str:
    """Join `cwd`'s harness hooks file for this run, writing it if no other run has: "" or why not."""
    c, rd = Path(cwd), Path(run_dir)
    created_dir = not (c / ".cursor").exists()
    try:
        with _Locked(c):
            side = _read_json(c / SIDECAR_REL)
            if not side:
                hooks = c / HOOKS_REL
                original = hooks.read_text() if hooks.exists() else None
                text = merged(original)
                top = _top(c)
                rel = os.path.relpath(c / HOOKS_REL, top) if top else ""
                tracked = bool(top) and _tracked(top, rel)
                side = {"original": original, "written": text, "owners": [], "created_dir": created_dir,
                        "tracked": rel if tracked else ""}
                _write(hooks, text)
                if tracked:
                    _git(top, "update-index", "--skip-worktree", "--", rel)
                if top:
                    _exclude(top, c)
            if str(rd) not in side["owners"]:
                side["owners"].append(str(rd))
            _write(c / SIDECAR_REL, json.dumps(side))
        _write(rd / RUN_STATE, json.dumps({"cwd": str(c)}))
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return f"{type(e).__name__}: {e}"[:300]
    return ""


def undo(run_dir) -> None:
    """Leave the hooks file this run joined; the last run out puts back what was there before."""
    rd = Path(run_dir)
    state = _read_json(rd / RUN_STATE)
    if not state.get("cwd"):
        return
    c = Path(state["cwd"])
    try:
        if (c / SIDECAR_REL).exists():
            with _Locked(c):
                side = _read_json(c / SIDECAR_REL)
                owners = [o for o in side.get("owners") or [] if o != str(rd)]
                if side and owners:
                    _write(c / SIDECAR_REL, json.dumps({**side, "owners": owners}))
                elif side:
                    _restore(c, side)
        (rd / RUN_STATE).unlink()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


def _restore(c: Path, side: dict) -> None:
    hooks = c / HOOKS_REL
    original = side.get("original")
    try:
        now = hooks.read_text() if hooks.exists() else None
    except OSError:
        now = None
    if now is not None and now != side.get("written"):
        # Changed during the run: keep that change, take out only the harness's entries.
        try:
            data = json.loads(now)
            data["hooks"] = {k: [e for e in v if not _ours(e)] for k, v in (data.get("hooks") or {}).items()}
            data["hooks"] = {k: v for k, v in data["hooks"].items() if v}
            original = json.dumps(data, indent=2) + "\n" if data["hooks"] or original is not None else None
        except (ValueError, AttributeError, TypeError):
            original = now
    if original is None:
        hooks.unlink(missing_ok=True)
    else:
        _write(hooks, original)
    if side.get("tracked") and (top := _top(c)):
        _git(top, "update-index", "--no-skip-worktree", "--", side["tracked"])
    (c / SIDECAR_REL).unlink(missing_ok=True)
    (c / SIDECAR_REL.with_suffix(".lock")).unlink(missing_ok=True)
    if side.get("created_dir"):
        try:
            (c / ".cursor").rmdir()
        except OSError:
            pass


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


if __name__ == "__main__":
    sys.exit(main(sys.argv))
