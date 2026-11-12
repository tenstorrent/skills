# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A release candidate's fresh one-line install check, run model-free: `ttp install-check [<commit>]`.

A release cut must show that the public one-line install works at the candidate commit. Run by the
model as `curl ... | sh`, that download marks the run as reading untrusted input (untrusted.py,
source 'download'), and a marked run may push no tag, so the cut could never move its channel tag
(`ttp push --move-tag`). Here ttp itself does the download and the install, and the model only reads
the recorded pass or fail:

- the installer is the project's own `plugins/tt-project/install.sh` at exactly that commit, from the
  GitHub repository of the push remote (else `origin`): no free URL, so this is no download channel
  for other text;
- it runs with `TT_PROJECT_REF=<commit>` in a fresh temporary HOME (and XDG dirs) under the run
  folder, the user's own config dirs left out, the PATH profile untouched;
- it passes when the installer exits 0 and the installed `ttp --version` names the commit;
- the result (commit, exit code, installed version, URL, log path, time) is written to
  `install-check.json` in the run folder (else the temp dir) and appended to the project's
  state/install_checks.jsonl; only one summary line is printed, never the installer's output.

Every untrusted-input guard stays: a model's own curl or wget still marks its run, and a marked run
still pushes no tag.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

LOG = "install_checks.jsonl"     # under the project's state: one line per check
RESULT = "install-check.json"    # in the run folder (or the check's temp dir)
INSTALLER = "plugins/tt-project/install.sh"
RAW = "https://raw.githubusercontent.com/{slug}/{commit}/" + INSTALLER
FETCH_TIMEOUT_S = 60
INSTALL_TIMEOUT_S = 1800
GITHUB_RE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                       r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")
# left out of the installer's environment: the user's own tool homes and every harness setting
DROP = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CURSOR_CONFIG_DIR", "GH_CONFIG_DIR", "GIT_DIR", "GIT_WORK_TREE",
        "TT_PROJECT_REPO", "TT_PROJECT_REF", "TT_PROJECT_SRC")


def slug(repo: Path, cfg: dict) -> str:
    """owner/repo of the push remote's GitHub URL, else origin's; '' when neither is on GitHub."""
    from .channeltag import _git, push_remote
    prefix = ["git", "-C", str(repo)]
    remote = push_remote(prefix, cfg)[0]
    for r in dict.fromkeys(x for x in (remote, "origin") if x):
        out = _git(prefix, "remote", "get-url", r)
        if out.returncode == 0 and (m := GITHUB_RE.match(out.stdout.strip())):
            return m.group(1)
    return ""


def _fetch(url: str, dest: Path) -> None:
    """Download the installer to `dest` (tests replace this)."""
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as r:
        dest.write_bytes(r.read())


def _env(home: Path, commit: str, repo_url: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in DROP and not k.startswith("TTP_")}
    env.update(HOME=str(home), XDG_DATA_HOME=str(home / ".local/share"), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_CACHE_HOME=str(home / ".cache"), XDG_STATE_HOME=str(home / ".local/state"),
               TT_PROJECT_REF=commit, TT_PROJECT_REPO=repo_url, TT_PROJECT_NO_MODIFY_PATH="1")
    return env


def run(p, repo: Path, commit: str, out_dir: Path | None = None) -> dict:
    """Fetch and run the installer at `commit` in a fresh HOME; return the recorded result."""
    from .channeltag import _commit
    prefix = ["git", "-C", str(repo)]
    sha = _commit(prefix, commit)
    rec: dict = {"commit": sha or commit, "rc": None, "version": "", "passed": False, "time": int(time.time())}
    out_dir = Path(out_dir or tempfile.mkdtemp(prefix="ttp-install-check-"))
    rec["log"] = str(out_dir / "install-check.log")
    s = slug(repo, p.config() if p else {})
    if not sha:
        rec["error"] = f"{commit} is no commit here"
    elif not s:
        rec["error"] = "neither the push remote nor origin is a GitHub repository"
    else:
        rec["url"] = RAW.format(slug=s, commit=sha)
        script = out_dir / "install-check.sh"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            home = Path(tempfile.mkdtemp(prefix="install-check-home-", dir=out_dir))   # fresh each time
            _fetch(rec["url"], script)
            with open(rec["log"], "wb") as log:
                done = subprocess.run(["sh", str(script)], cwd=home, env=_env(home, sha, f"https://github.com/{s}.git"),
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                      timeout=INSTALL_TIMEOUT_S)
            rec["rc"] = done.returncode
            ttp = home / ".local/bin/ttp"
            if ttp.exists():
                v = subprocess.run([str(ttp), "--version"], env=_env(home, sha, ""), capture_output=True,
                                   text=True, timeout=120, stdin=subprocess.DEVNULL)
                rec["version"] = v.stdout.strip().splitlines()[-1] if v.returncode == 0 and v.stdout.strip() else ""
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
        if rec["rc"] == 0 and sha[:12] not in rec["version"]:
            rec["error"] = f"installed ttp --version does not name {sha[:12]}"
        rec["passed"] = rec["rc"] == 0 and not rec.get("error")
    from . import project
    out_dir.mkdir(parents=True, exist_ok=True)
    project.durable_write(out_dir / RESULT, json.dumps(rec, indent=1) + "\n")
    if p is not None:
        try:
            project.durable_append(p.state / LOG, json.dumps(rec) + "\n")
        except OSError:
            pass
    return rec


def line(rec: dict) -> str:
    c = str(rec.get("commit") or "")[:12]
    if rec.get("passed"):
        return f"install check passed at {c}: {rec['version']} (rc 0; log {rec['log']})"
    why = rec.get("error") or f"installer exit {rec.get('rc')}"
    return f"install check FAILED at {c}: {why} (log {rec['log']})"


def command(a) -> None:
    """`ttp install-check [<commit>]`: exit 0 passed, 1 failed, 2 usage."""
    import sys
    from .project import Project
    base = os.environ.get("TTP_PROJECT")
    p = Project(base) if base else next((c for d in [Path.cwd(), *Path.cwd().parents]
                                         if (c := Project(d)).exists()), None)
    if p is not None and not p.exists():
        p = None
    out = a.out or os.environ.get("TTP_RUN_DIR") or None
    rec = run(p, Path.cwd(), a.commit, Path(out) if out else None)
    print(line(rec) + f"; result {Path(rec['log']).parent / RESULT}")
    sys.exit(0 if rec["passed"] else 1)
