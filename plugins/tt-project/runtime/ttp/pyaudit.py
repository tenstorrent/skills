# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The run-time Python judge (rollout flag `pyaudit`, key guards.pyaudit, off by default).

On, the daemon packs pyaudit/sitecustomize.py into the run folder's pyaudit/pyaudit.zip and puts it first on the
run's PYTHONPATH, with the policy (the protected paths and why, the credential files, a GLM run's write roots) as
JSON in TTP_PYAUDIT_POLICY (wire). A Python running inline code (`python3 -c '...'`, `python3 - <<'EOF'`) then
judges its own writes, reads and child commands as they happen, with the same rules and messages as the shell
guards (sitecustomize.py; child commands go through hook.pre_tool_use). The code is judged by what it does as
it runs, never by its words; the shell guards keep reading the command's text as they always did."""
from __future__ import annotations

import json
import os
import re
import time
import zipfile
from pathlib import Path

FLAG = "pyaudit"
POLICY_ENV = "TTP_PYAUDIT_POLICY"
DIR = Path(__file__).resolve().parent.parent / "pyaudit"
TAMPER = ("a run's inline Python may not get around the guard's run-time judge (audit hooks, ctypes, fork_exec, "
          "the gc, a function's code, the judge's own environment or files); write it as a script file instead")
def policy(env, cwd: str, run_dir: str, cfg: dict | None, zp: str) -> dict:
    """What the judge refuses and why: the protected paths (pathguard's, the judge's own files), the credential
    files (credguard's, the project's secrets) and, for a run on the GLM arm, its write roots (glmguard's)."""
    from . import credguard, glmguard, pathguard, project, sidefx
    base = set(pathguard.roots(env, {}))
    roots = {r: (pathguard.WHY if r in base else pathguard.PROJECT_WHY).format(hit=r)
             for r in pathguard.roots(env, cfg)}
    roots.update({p: glmguard.GUARD_WHY for d in (os.path.dirname(zp), str(DIR)) for p in (d, os.path.realpath(d))})
    home = str(env.get("HOME") or os.path.expanduser("~"))
    secrets = {p: credguard.WHY for f in [*credguard.credential_files(home), str(project.secrets_path())]
               for p in (str(f), os.path.realpath(f))}
    pol = {"runtime": str(DIR.parent), "zip": zp,
           "t": time.time(), "tamper": TAMPER, "roots": roots, "secrets": secrets}
    if glmguard.glm_run(env) is not None:
        pol["glm"] = {"run_dir": run_dir, "roots": glmguard._roots(cwd, run_dir, glmguard._scratch(run_dir, env), env),
                      "own": glmguard._own_roots(cwd, run_dir, env), "home": home,
                      "harmless": list(sidefx.HARMLESS), "records": sorted(glmguard.RUN_RECORDS),
                      "db": glmguard.STATE_DB.pattern, "db_why": glmguard.STATE_DB_WHY}
    return pol


def wire(env: dict, argv: list, runtime: str, cwd: str, run_dir: str, cfg: dict | None) -> list:
    """Put the judge in a run's environment; the argv with the hooks' PYTHONPATH (Claude's settings JSON, Codex's
    hook command) the same, so a hook reads the environment the run's commands get."""
    d = Path(run_dir) / "pyaudit"
    d.mkdir(exist_ok=True)
    zp = str(d / "pyaudit.zip")
    with zipfile.ZipFile(zp, "w") as z:   # a zip: no __pycache__ a run could plant code in
        z.write(DIR / "sitecustomize.py", "sitecustomize.py")
    pp = f"{zp}{os.pathsep}{runtime}"
    env[POLICY_ENV] = json.dumps(policy({**os.environ, **env}, cwd, run_dir, cfg, zp))   # as the run sees it
    env["PYTHONPATH"] = pp
    rx = re.compile(r"(PYTHONPATH(?:=|\": \")'?)" + re.escape(runtime) + r"(?=[\"'\s]|$)")
    return [rx.sub(lambda m: m[1] + pp, a) if isinstance(a, str) else a for a in argv]


def guarded(env) -> list[str]:
    """The judge's own folder in the run folder: a protected path while the judge is on (pathguard.roots)."""
    try:
        return [os.path.dirname(json.loads(env.get(POLICY_ENV) or "")["zip"])]
    except (ValueError, KeyError, TypeError, AttributeError):
        return []


def child_refusal(cmd: str, cwd: str, env: dict) -> str | None:
    """Why the hook refuses a child command of the judged Python (its argv as shell text), or None."""
    from . import hook
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd}, stage=FLAG)
    spec = (out or {}).get("hookSpecificOutput") or {}
    if spec.get("permissionDecision") in ("deny", "ask"):
        return str(spec.get("permissionDecisionReason") or "the guard refuses this command")
    return None


def logged(ev: str, args, why: str) -> None:
    """Log one refusal of the judge to the run's refusals.jsonl, as the hook logs its own."""
    from . import hook
    hook.CALL.update(id="", stage=FLAG)
    hook._log_refusal(f"{ev} {args!r}", kind=FLAG, reason=why)


def glm_write(path: str, glm: dict) -> str | None:
    """Why a run on the GLM arm may not write `path` (glmguard's rules: its records, outside its roots)."""
    from . import glmguard
    if glmguard._run_record(path, glm["run_dir"]):
        return f"{glmguard.GUARD_WHY}: {path}"
    if glmguard._outside_roots(path, glm["roots"] + [os.getcwd()], glm["own"], glm["home"]):
        return f"it writes {glmguard._OUTSIDE}: {path}"
    return None
