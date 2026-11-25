# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The run-time Python judge (rollout flag `pyaudit`, key guards.pyaudit, off by default).

On, the daemon packs pyaudit/sitecustomize.py into the run folder's pyaudit/pyaudit.zip and puts it first on the
run's PYTHONPATH, with the policy (the protected paths and why, the credential files, a GLM run's write roots) as
JSON in TTP_PYAUDIT_POLICY (wire). A Python running inline code (`python3 -c '...'`, `python3 - <<'EOF'`) then
judges its own writes, reads and child commands as they happen, with the same rules and messages as the shell
guards (sitecustomize.py; child commands go through hook.pre_tool_use). So the shell guards need not read that
code: relieve() blanks it, but only when the Python is certain to load the judge (every condition below); any
doubt leaves the text as it was, and the shell guards judge it as before."""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

FLAG = "pyaudit"
POLICY_ENV = "TTP_PYAUDIT_POLICY"
DIR = Path(__file__).resolve().parent.parent / "pyaudit"
TAMPER = ("a run's inline Python may not get around the guard's run-time judge (audit hooks, ctypes, fork_exec, "
          "the gc, a function's code, the judge's own environment or files); write it as a script file instead")
# The Python child being judged (child_refusal): its environment decides relief, not the hook's
_CHILD: list[dict] = []

_INTERP = r"(python(?:3(?:\.\d+)?)?)"
_HEAD = (r"(?:^|(?<=[;&|(\n]))[ \t]*(?:(?:then|do|else|elif|if|while|until|!)[ \t]+)*"   # a command start
         r"(?:[A-Za-z_]\w*=\S*[ \t]+)*"   # its assignments
         r"(?:(?:timeout|nice|nohup|time|stdbuf)(?:[ \t]+-{1,2}[\w=.-]+)*(?:[ \t]+\d[\w.]*)?[ \t]+)*"   # wrappers
         + _INTERP + r"(?:[ \t]+-[bBdOqsuvR]+)*")
# double-quoted code may hold plain `$name` expansions: the judge screens the code they give as it starts (screen)
VAR_RE = re.compile(r"\$(?:[A-Za-z_]\w*|\{[A-Za-z_]\w*\})")
C_RE = re.compile(_HEAD + r"[ \t]+-c[ \t]*('[^']*'|\"(?:[^\"$`\\]|" + VAR_RE.pattern + r")*\")(?=[\s;&|()<>]|$)")   # the whole word
STDIN_RE = re.compile(_HEAD + r"(?:[ \t]+-)?[ \t]*$")
# Anything that may change which Python starts, or whether it loads the judge
ESCAPE = re.compile(
    r"(?<![\w.-])env\b[^;&|\n]*[ \t]-(?:[a-zA-Z]*[iu]|-ignore-environment|-unset)"
    r"|(?<![\w.-])(?:unset|export|alias|shopt|source|hash|enable|function|exec|eval|declare|typeset)(?![\w.-])"
    r"|(?:^|[;&|(\n])[ \t]*\.[ \t]|python[\w.]*[ \t]*\([ \t]*\)"
    r"|(?<![\w$])(?:PYTHON\w*|BASH_ENV|TTP_PYAUDIT_\w*|PATH|HOME|LD_\w+)\+?="
    r"|\.pth\b|customize|pyvenv|site-packages")
_MARKS = {"importlib", "ctypes", "_posixsubprocess", "__code__", "__closure__", "cell_contents", "__globals__",
          "f_globals", "f_locals", "f_back", "_getframe", "addaudithook", "__subclasses__", "__self__",
          "__wrapped__", "pickle", "marshal", "CodeType", "FunctionType", "fork_exec", "mknod", "mkfifo",
          "_ttp_pyaudit", "inspect", "dir_fd"}
SITE_FILES = ("*.pth", "usercustomize.py", "sitecustomize.py")


def _marks():
    from . import pathguard
    words = sorted(pathguard.PY_DYNAMIC | pathguard.PY_DYNAMIC_ATTR | _MARKS)
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, words)) + r")(?!\w)|\bgc\s*\.|sys\s*\.\s*modules")


MARK_RE = _marks()


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
    pol = {"runtime": str(DIR.parent), "zip": zp, "sha": hashlib.sha256(Path(zp).read_bytes()).hexdigest(),
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


def first(env) -> None:
    """Put the judge's zip first on `env`'s PYTHONPATH again, when the judge is on (a launcher set its own)."""
    try:
        zp = json.loads(env.get(POLICY_ENV) or "")["zip"]
    except (ValueError, KeyError, TypeError):
        return
    pp = str(env.get("PYTHONPATH") or "")
    if pp.split(os.pathsep)[0] != zp:
        env["PYTHONPATH"] = zp + (os.pathsep + pp if pp else "")


def loaded(env) -> dict | None:
    """The policy, when a Python started with `env` loads the judge: its zip first on PYTHONPATH and unchanged."""
    try:
        pol = json.loads(env.get(POLICY_ENV) or "")
        if str(env.get("PYTHONPATH") or "").split(os.pathsep)[0] == pol["zip"] \
                and hashlib.sha256(Path(pol["zip"]).read_bytes()).hexdigest() == pol["sha"]:
            return pol
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        pass
    return None


def _interpreter(name: str, env, pol: dict) -> int:
    """The minor version of `name` on the environment's PATH when it is a system Python 3.8+ (root's, not
    writable by others) whose site files (a .pth, usercustomize, a venv's pyvenv.cfg) all predate the judge's
    policy; else 0."""
    path = str(env.get("PATH") or "")
    if not path or any(not os.path.isabs(p) for p in path.split(os.pathsep)):
        return 0
    w = shutil.which(name, path=path)
    if not w:
        return 0
    real = os.path.realpath(w)
    m = re.fullmatch(r"python3\.(\d+)", os.path.basename(real))
    try:
        st = os.stat(real)
    except OSError:
        return 0
    if not (m and int(m[1]) >= 8 and st.st_uid == 0 and not st.st_mode & 0o022):
        return 0
    ver, files = f"python3.{m[1]}", []
    sites = [os.path.join(str(env.get("HOME") or ""), ".local", "lib", ver, "site-packages")]
    for v in (os.path.dirname(w), os.path.dirname(os.path.dirname(w))):
        files.append(os.path.join(v, "pyvenv.cfg"))
        sites.append(os.path.join(v, "lib", ver, "site-packages"))
    files += [f for s in sites for g in SITE_FILES for f in glob.glob(os.path.join(glob.escape(s), g))]
    try:
        return int(m[1]) if all(os.lstat(f).st_ctime < pol["t"] for f in files if os.path.lexists(f)) else 0
    except OSError:
        return 0


def _bodies(lines: list[str]) -> list[list]:
    """Each heredoc of the command's lines: [the Python that reads it as its program (`python3 - <<'EOF'`) or '',
    whether its delimiter is quoted, its body's line indexes], as hook._heredocs reads them."""
    from . import hook
    out, pending = [], []
    for i, line in enumerate(lines):
        if pending:
            delim, dash, k = pending[0]
            if (line.lstrip("\t") if dash else line) == delim:
                pending.pop(0)
            else:
                out[k][2].append(i)
            continue
        if "<<" in line:
            flags = hook._quoting(line)
            for m in hook.HEREDOC_RE.finditer(line):
                if not flags[m.start()][0]:
                    h = STDIN_RE.search(line[:m.start()])
                    pending.append((m[3], m[1] == "-", len(out)))
                    out.append([h[1] if h and not flags[h.start(1)][0] else "", bool(m[2]), []])
    return out


def relieve(cmd: str, env) -> str:
    """`cmd` with the inline code of each Python that loads the judge blanked (`-c 'pass'`, a heredoc body
    `pass`), so the shell guards judge only the shell around it. Unchanged unless every condition holds: the
    judge is on and unchanged (loaded), nothing in the command can change the Python or its environment
    (ESCAPE), the Python is a plain name on a trusted PATH (_interpreter) at an unquoted command start, with
    only harmless flags, its code fixed text (single-quoted, or double-quoted with no backtick or backslash
    and no `$` but plain `$name` ones, which the judge screens as it starts; a heredoc's delimiter quoted or
    its body free of `$` and backticks) that names nothing that reaches past the judge (MARK_RE)."""
    env = _CHILD[0] if _CHILD else env
    if "python" not in cmd or "\\\n" in cmd or not (pol := loaded(env)) or ESCAPE.search(cmd):
        return cmd
    from . import hook
    names: dict[str, int] = {}

    def ok(name, code):
        if name not in names:
            names[name] = _interpreter(name, env, pol)
        if not names[name] or MARK_RE.search(code):
            return False
        # an expansion's text shows only as the Python starts: 3.10+ (sys.orig_argv), with its shape on record
        return "$" not in code or names[name] >= 10 and _expects(pol, code[1:-1])

    lines = cmd.split("\n")
    docs = _bodies(lines)
    inbody = {i for d in docs for i in d[2]}
    for py, quoted, body in docs:
        text = "\n".join(lines[i] for i in body)
        if body and py and (quoted or not re.search(r"[$`]", text)) and ok(py, text):
            lines[body[0]] = "pass"
            for i in body[1:]:
                lines[i] = ""
    # quoting as the shell reads it: heredoc bodies are no shell text (2: no `-c` relief there)
    view = "\n".join(" " * len(ln) if i in inbody else ln for i, ln in enumerate(lines))
    flags, pos = [int(q) for q, _ in hook._quoting(view)], 0
    for i, ln in enumerate(lines):
        if i in inbody:
            flags[pos:pos + len(ln)] = [2] * len(ln)
        pos += len(ln) + 1
    text = "\n".join(lines)
    return C_RE.sub(lambda m: m[0][:m.start(2) - m.start()] + "'pass'" if flags[m.start(1)] == 0
                    and 2 not in flags[m.start():m.end()] and ok(m[1], m[2]) else m[0], text)


def _expects(pol: dict, code: str) -> bool:
    """Record the shape of relieved code with `$name` expansions (its fixed parts) in the judge's folder, where
    the judge finds it and screens the code they give (screen). False when it cannot be recorded."""
    parts = VAR_RE.split(code)
    if not "".join(parts).strip():   # code all expansions: a shape that every later code fits
        return False
    line = json.dumps(parts) + "\n"
    f = Path(pol["zip"]).with_name("expanded.jsonl")
    try:
        if line not in (f.read_text().splitlines(True) if f.exists() else ()):
            with open(f, "a") as out:
                out.write(line)
        return True
    except OSError:
        return False


def screen(code: str, pol: dict) -> str | None:
    """Why the judge refuses to start inline `code` whose shape relieve() recorded: what the expansions put in
    it names something that reaches past the judge (MARK_RE), which the shell guards never read."""
    try:
        shapes = Path(pol["zip"]).with_name("expanded.jsonl").read_text().splitlines()
    except OSError:
        return None
    for s in shapes:
        if re.fullmatch(".*".join(map(re.escape, json.loads(s))), code, re.S) and MARK_RE.search(code):
            return pol["tamper"]
    return None


def child_refusal(cmd: str, cwd: str, env: dict) -> str | None:
    """Why the hook refuses a child command of the judged Python (its argv as shell text), or None."""
    from . import hook
    _CHILD[:] = [env]
    try:
        out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd}, stage=FLAG)
    finally:
        _CHILD.clear()
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
