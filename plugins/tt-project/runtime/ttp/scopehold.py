# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A push the remote refuses because the credential lacks a token scope: GitHub refuses a push that
creates or updates `.github/workflows/*` without the `workflow` scope ("refusing to allow an OAuth App
to create or update workflow `<path>` without `workflow` scope", or the Personal Access Token variant).

The push batch (batch.py) reads the refusal from git push's stderr (classify), holds the entries whose
changes need the scope (touched, needs) and pushes the rest at once. The push queue (pushq.py) keeps
their rows `held` and releases them, model-free, once the scope check passes (check_cmd, probe)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import gitmemo

KV = "push_scope_held"       # kv: {scope: {"rows": [push_queue ids], "probe_at", "holds", "probe"}}
ALERT = "push_scope"         # alert key: "push_scope:<scope>" (alerts.holds: while KV names the scope)
WORKFLOW_DIR = ".github/workflows/"
# `delivery.push_scope_check`: a shell command; it passes (exit 0) once the credential has `{scope}`.
DEFAULT_CHECK = "gh auth status 2>&1 | grep -i 'token scopes:' | grep -q \"'{scope}'\""
DEFAULT_PROBE_S = 600        # `delivery.push_scope_probe_s`: how often a held scope is checked
PROBE_MAX_S = 6 * 3600       # a scope held again after a passing check is checked less often, up to this
CHECK_TIMEOUT_S = 30
SCOPE = re.compile(r"without [`'\"]?([A-Za-z][A-Za-z0-9_:.-]{0,63})[`'\"]? scope")
QUOTED = re.compile(r"`([^`\s]+)`")


def classify(stderr: str) -> list[dict]:
    """The scope refusals in git push's stderr, one per scope in order of appearance:
    [{"scope": "workflow", "files": [".github/workflows/ci.yml"]}]. A file is a back-quoted word on
    a refusing line other than the scope itself. Nothing when no line refuses for a missing scope."""
    out: dict[str, list[str]] = {}
    for line in (stderr or "").splitlines():
        for m in SCOPE.finditer(line):
            files = out.setdefault(m.group(1), [])
            for f in QUOTED.findall(line):
                if f != m.group(1) and f not in files:
                    files.append(f)
    return [{"scope": s, "files": f} for s, f in out.items()]


def _git(repo: Path, *args: str) -> list[str]:
    r = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=gitmemo.timeout(None, args))
    return [x for x in r.stdout.splitlines() if x.strip()] if r.returncode == 0 else []


def touched(repo: Path, tip: str, head: str) -> list[str]:
    """The paths a push of `head` onto `tip` changes: its tree against the merge base (`tip...head`,
    which takes in what its merge commits brought), plus each of its own commits' paths (a file it
    added and removed again is still in a commit the remote checks)."""
    paths = _git(repo, "diff", "--name-only", f"{tip}...{head}")
    for f in _git(repo, "log", "--format=", "--name-only", "--no-merges", f"{tip}..{head}"):
        if f not in paths:
            paths.append(f)
    return paths


def needs(paths: list[str], need: dict) -> list[str]:
    """Which of `paths` need the scope of refusal `need`: the files it names, and for `workflow`
    any file under .github/workflows/."""
    named = set(need.get("files") or [])
    return [f for f in paths if f in named or (need.get("scope") == "workflow" and f.startswith(WORKFLOW_DIR))]


def check_cmd(d: dict, scope: str) -> str:
    """The shell command that passes once the credential has `scope` (`delivery.push_scope_check`,
    `{scope}` filled in; default a `gh auth status` check)."""
    cmd = d.get("push_scope_check")
    cmd = cmd if isinstance(cmd, str) and cmd.strip() else DEFAULT_CHECK
    return cmd.replace("{scope}", scope)


def probe_s(d: dict, holds: int = 1) -> float:
    """Seconds between checks of a held scope: delivery.push_scope_probe_s, doubled for each time the
    scope held changes again after a passing check (the check may not see git's credential)."""
    try:
        base = max(30.0, float(d.get("push_scope_probe_s", DEFAULT_PROBE_S)))
    except (TypeError, ValueError):
        base = DEFAULT_PROBE_S
    return min(PROBE_MAX_S, base * 2 ** max(0, holds - 1))


def probe(cwd: Path, d: dict, scope: str) -> tuple[bool, str]:
    """Run the scope check: (passed, the last line of its output or why it did not run)."""
    if not SCOPE.fullmatch(f"without {scope} scope"):
        return False, f"not a scope name: {scope!r}"
    try:
        r = subprocess.run(check_cmd(d, scope), shell=True, cwd=str(cwd), text=True, capture_output=True,
                           timeout=CHECK_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"[:300]
    tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    return r.returncode == 0, (tail[-1] if tail else f"exit {r.returncode}")[:300]


def grant_hint(scope: str) -> str:
    return f"`gh auth refresh -s {scope}`"
