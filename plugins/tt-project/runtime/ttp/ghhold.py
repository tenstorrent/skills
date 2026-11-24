# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""gh writes stay behind the run's gh wrapper (bin/gh, prguard.main) on every provider.

A run's agent may reach the real gh without the wrapper: an absolute path, a login shell that puts the
system's bin folders back in front (Cursor's `$SHELL -ilc`, macOS path_helper), or a PATH it set
itself. Four layers, model-free:

1. hold(), in the runner, on every provider: the agent's GH_CONFIG_DIR is an empty folder the run owns,
   and the gh token variables (TOKENS) and the real GH_CONFIG_DIR move to HELD-prefixed names. Only
   the wrapper puts them back (restore(), first thing in prguard.main) before its checks and the
   real gh. A real gh reached around the wrapper finds no token in the environment or hosts.yml.
   Not covered: a token in the system keyring. gh falls back to it with no hosts.yml at all (cli/cli
   internal/config/config.go ActiveToken -> TokenFromKeyring), and on macOS go-keyring runs
   /usr/bin/security on the default keychain, which no variable redirects; layers 2-4 cover that case.
   git's https credential helper for github.com goes through the wrapper (GIT_CONFIG_* entries), since
   `gh auth setup-git` writes the real gh's absolute path.
2. hook.pre_tool_use checks a `gh pr create|edit` or `gh api` PR body before the command runs, with
   prguard.form_problem (the same rule the wrapper applies).
3. ZDOTDIR: zsh reads the run's own startup files (zdotdir()), which source the user's and then put
   the wrapper's folder (BIN_ENV) first on PATH again; loopproxy.sh does the same in every Claude Code
   Bash command.
4. first_gh(): the first gh on the agent's PATH is checked at launch; when it is not the wrapper the
   run gets MARK, and the hook refuses its gh writes (marked_refusal).
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path

TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
HELD = "TTP_GH_HELD_"          # TTP_GH_HELD_GH_TOKEN: the value the wrapper restores
HOLD_ENV = "TTP_GH_HOLD"        # the empty GH_CONFIG_DIR the run's agent sees; set while held
BIN_ENV = "TTP_RUN_BIN"         # the wrapper's folder, put first on PATH again (loopproxy.sh, zdotdir)
ZDOT_ORIG_ENV = "TTP_ZDOTDIR_ORIG"
EMPTY = "gh-empty"
ZDOT = "zdotdir"
MARK = "gh-unguarded"           # in the run dir: the first gh on PATH was not the wrapper
SHIM_TEXT = b"ttp.prguard import main"
ZSH_FILES = (".zshenv", ".zprofile", ".zshrc", ".zlogin")
MARK_WHY = ("refused: the first gh on this run's PATH was not the harness gh wrapper, so its gh writes are "
            f"refused; reads still run. Put ${BIN_ENV} first on PATH again")


def is_shim(path: str) -> bool:
    """`path` is the harness gh wrapper (template/bin/gh)."""
    try:
        with open(path, "rb") as f:
            return SHIM_TEXT in f.read(4096)
    except OSError:
        return False


def _ghs(path: str):
    for d in path.split(os.pathsep):
        c = os.path.join(d, "gh") if d else ""
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            yield c


def shim_on(path: str) -> str | None:
    """The first harness gh wrapper on `path`, if any."""
    return next((c for c in _ghs(path) if is_shim(c)), None)


def real_on(path: str) -> str | None:
    """The first gh on `path` that is not the harness wrapper (the binary it forwards to)."""
    return next((c for c in _ghs(path) if not is_shim(c)), None)


def first_gh(path: str) -> str | None:
    """The first gh on `path` (the one a bare `gh` runs)."""
    return next(_ghs(path), None)


def zdotdir(run_dir: Path, orig: str) -> Path:
    """The run's zsh startup folder: each file sources the user's own (from `orig`, with ZDOTDIR set back
    to it while it runs), then puts $TTP_RUN_BIN first on PATH again."""
    d = run_dir / ZDOT
    d.mkdir(mode=0o700, exist_ok=True)
    for name in ZSH_FILES:
        (d / name).write_text(
            f"# tt-project: the user's {name}, then the harness bin folder first on PATH (ghhold.py)\n"
            f'_ttp_z=$ZDOTDIR; ZDOTDIR=${{{ZDOT_ORIG_ENV}:-$HOME}}\n'
            f'[ -r "$ZDOTDIR/{name}" ] && . "$ZDOTDIR/{name}"\n'
            f"ZDOTDIR=$_ttp_z; unset _ttp_z\n"
            f'[ -n "${{{BIN_ENV}-}}" ] && case ":$PATH" in ":${BIN_ENV}:"*) ;; *) PATH="${BIN_ENV}:$PATH"; export PATH ;; esac\n')
    return d


def hold(env: dict, run_dir: Path) -> dict:
    """`env` with the gh credentials held for the wrapper, the run's ZDOTDIR and BIN_ENV set, and MARK
    written when the first gh on its PATH is not the wrapper. Unchanged when no wrapper is on PATH
    (nothing could restore them) or the hold is already on."""
    shim = shim_on(env.get("PATH", ""))
    if not shim or env.get(HOLD_ENV):
        return env
    out = dict(env)
    empty = run_dir / EMPTY
    empty.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in (*TOKENS, "GH_CONFIG_DIR"):
        value = out.pop(name, None)
        if value:
            out[HELD + name] = value
    out["GH_CONFIG_DIR"] = out[HOLD_ENV] = str(empty)
    out[BIN_ENV] = os.path.dirname(shim)
    out[ZDOT_ORIG_ENV] = env.get("ZDOTDIR") or env.get("HOME") or ""
    out["ZDOTDIR"] = str(zdotdir(run_dir, out[ZDOT_ORIG_ENV]))
    out.update(_git_helper(out, shim))
    first = first_gh(out.get("PATH", ""))
    if first != shim:
        mark(run_dir, f"first gh on PATH: {first}")
    return out


def _git_helper(env: dict, shim: str) -> dict:
    """GIT_CONFIG_* entries after any already set: github.com's https credential helper is the wrapper's
    `gh auth git-credential` (an empty value first drops the helpers configured before it)."""
    try:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        return {}
    hosts = ["github.com", *([h] if (h := env.get("GH_HOST", "")) and h != "github.com" else [])]
    out: dict[str, str] = {}
    for h in hosts:
        for value in ("", f"!{shlex.quote(shim)} auth git-credential"):
            out[f"GIT_CONFIG_KEY_{n}"], out[f"GIT_CONFIG_VALUE_{n}"] = f"credential.https://{h}.helper", value
            n += 1
    return {**out, "GIT_CONFIG_COUNT": str(n)}


def restore(env) -> None:
    """Put the held gh credentials back into `env` (os.environ in the wrapper), in place."""
    if not env.get(HOLD_ENV):
        return
    if env.get("GH_CONFIG_DIR") == env.get(HOLD_ENV):
        env.pop("GH_CONFIG_DIR", None)
    for name in (*TOKENS, "GH_CONFIG_DIR"):
        value = env.pop(HELD + name, None)
        if value:
            env[name] = value
    env.pop(HOLD_ENV, None)


def mark(run_dir: Path, why: str) -> None:
    try:
        (run_dir / MARK).write_text(why + "\n")
    except OSError:
        pass


def marked(run_dir: str | None) -> bool:
    return bool(run_dir) and os.path.exists(os.path.join(run_dir, MARK))


def marked_refusal(segments: list[list[str]]) -> str | None:
    """Layer 4: in a run whose first gh was not the wrapper, a gh write refused (reads run)."""
    if not marked(os.environ.get("TTP_RUN_DIR")):
        return None
    from . import untrusted
    for s in segments:
        if s and os.path.basename(s[0]) == "gh" and untrusted.gh_write(s[1:]):
            return MARK_WHY
    return None
