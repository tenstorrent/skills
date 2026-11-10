# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A throwaway HOME for every check run (`ttp checks`, `ttp push`, the push queue), here and on a check
or spill host, so a project's tests never read or write the account's own home folder.

With checks.scratch_home on (the default), a check runs with HOME in a fresh folder of its run, which
goes with it: here inside the run's managed scratch (checkdirs), on another host inside the run's
folder there (remote_lines), else (no managed scratch) in one of the process's own (own_folder). XDG_CONFIG_HOME, XDG_DATA_HOME and XDG_STATE_HOME are inside it.
The cache in force (XDG_CACHE_HOME where set, else the real HOME's .cache) is linked in as the
scratch HOME's .cache, and XDG_CACHE_HOME names it, so tools that write under $HOME/.cache (pip,
uv, pre-commit, plugin caches) reuse it and runs stay as fast as with the real HOME (cache_link). With
no such cache, or one holding a credential store, nothing is linked and XDG_CACHE_HOME stays where it
was set, else a cache folder shared by every check run (TTP_HOME/checks-cache). Removing the run's
folder only unlinks the link (checkdirs, rm -rf), never what it points to. Toolchains that find themselves under HOME
(KEEP: rustup, cargo, pyenv, go, nvm, the Hugging Face cache) keep their real folder where it exists
and their variable is unset, and TTP_HOME stays the harness's own.

checks.home_files lists files under HOME copied in (default HOME_FILES: .gitconfig, so git has its
identity), from the same host's real HOME (nothing is copied to another host). A credential store
(credguard), anything under ~/.ssh or gh's config, an absolute path or one leaving HOME is refused.
A check that needs the real HOME sets checks.scratch_home false for its project."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil

HOME_FILES = (".gitconfig",)
FILES_ENV = "TTP_CHECKS_HOME_FILES"   # in a check's env when on: the files (JSON), for a check run on another host
FOLDER = "home"
XDG = {"XDG_CONFIG_HOME": ".config", "XDG_DATA_HOME": ".local/share", "XDG_STATE_HOME": ".local/state"}
KEEP = {"RUSTUP_HOME": ".rustup", "CARGO_HOME": ".cargo", "PYENV_ROOT": ".pyenv", "GOPATH": "go",
        "NVM_DIR": ".nvm", "HF_HOME": ".cache/huggingface"}
NEVER = (".ssh", ".config/gh")   # never copied in, credential or not


def settings(cfg: dict | None, home: str | Path | None = None) -> tuple[bool, list[str]]:
    """(on, home_files) from `cfg`'s checks section. ValueError naming a home_files entry that is
    refused (refusal) or a value of the wrong type."""
    c = (cfg or {}).get("checks") or {}
    v = c.get("scratch_home", True)
    on = not (v is False or str(v).strip().lower() in ("0", "false", "off", "no"))
    files = c.get("home_files", list(HOME_FILES))
    if isinstance(files, str):
        files = json.loads(files) if files.strip().startswith("[") else [f for f in files.split() if f]
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ValueError("checks.home_files: a list of paths under HOME")
    for f in files:
        if why := refusal(f, home):
            raise ValueError(f"checks.home_files: {f}: {why}")
    return on, files


def refusal(rel: str, home: str | Path | None = None) -> str | None:
    """Why `rel` may not be copied into a scratch HOME, else None."""
    p = PurePosixPath(rel)
    if not rel or p.is_absolute() or rel.startswith("~") or ".." in p.parts:
        return "not a path under HOME"
    if any(p == PurePosixPath(n) or PurePosixPath(n) in p.parents for n in NEVER):
        return "ssh and gh config are never copied"
    from . import credguard
    home = Path(home or os.path.expanduser("~"))
    if credguard.read_refusal(str(home / p), search=True, home=home):
        return "a credential store or a folder holding one"
    return None


_own: list[str] = []   # this process's folder for an unguarded run (own_folder)


def own_folder() -> str:
    """A folder for the scratch HOME of a run with no managed scratch (a foreground `ttp checks`):
    one per process, under its TMPDIR, removed when the process exits."""
    if not _own:
        import atexit
        import tempfile
        _own.append(tempfile.mkdtemp(prefix="ttp-check-home-"))
        atexit.register(shutil.rmtree, _own[0], True)
    return _own[0]


def cache_dir() -> Path:
    from . import project
    return project.HOME_DIR / "checks-cache"


def cache_link(env: dict, real: Path) -> Path | None:
    """The cache a scratch HOME's .cache links to: `env`'s XDG_CACHE_HOME where set, else `real`'s
    .cache; None where that is no folder, or it is or holds a credential store (credguard)."""
    from . import credguard
    cache = Path(env.get("XDG_CACHE_HOME") or real / ".cache")
    if not cache.is_absolute() or not cache.is_dir() or credguard.read_refusal(str(cache), search=True, home=real):
        return None
    return cache


def apply(env: dict, folder: str | Path, files: list[str]) -> dict:
    """`env` with a HOME made in `folder` (FOLDER inside it), `files` copied in from the real
    HOME (missing ones skipped; a refused one never), and the variables the module docstring names."""
    from . import project
    real = Path(env.get("HOME") or os.path.expanduser("~"))
    home = Path(folder) / FOLDER   # one per run: its checks share it, and a later call keeps what they wrote
    out = {**env, "HOME": str(home), FILES_ENV: json.dumps(files)}
    for var, rel in XDG.items():
        (home / rel).mkdir(parents=True, exist_ok=True)
        out[var] = str(home / rel)
    for f in files:
        src = real / f
        if refusal(f, real) is None and src.is_file():
            (home / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, home / f)
    link = home / ".cache"
    if link.is_symlink():
        link.unlink()   # a later call on the same folder links the cache in force now, or none
    if cache := cache_link(env, real):
        if not link.exists():
            link.symlink_to(cache, target_is_directory=True)
            out["XDG_CACHE_HOME"] = str(cache)
    out.setdefault("XDG_CACHE_HOME", str(cache_dir()))
    for var, rel in KEEP.items():
        if not env.get(var) and (real / rel).exists():
            out[var] = str(real / rel)
    out.setdefault("TTP_HOME", str(project.HOME_DIR))
    return out


def remote_lines(where: str, fail: str = "exit 1") -> str:
    """Shell lines for another host (runnerenv.remote_lines), after its pinned env is made: the same
    scratch HOME there, in a fresh folder inside `where` (the run's folder, removed with it), when this
    check's env has it on (FILES_ENV); "" otherwise. Files come from that host's own HOME. Where no
    folder can be made it prints why and runs `fail`, as a missing pinned env does."""
    if (raw := os.environ.get(FILES_ENV)) is None:
        return ""
    try:
        files = [f for f in json.loads(raw) if isinstance(f, str) and refusal(f, "/nonexistent") is None]
    except ValueError:
        files = []
    w = shlex.quote(where)
    out = (f'ttp_real_home="$HOME"\nttp_home=$(mktemp -d "$PWD"/{w}/{FOLDER}.XXXXXXXX) || '
           f"{{ echo 'ttp: no scratch HOME could be made on this host; the check did not run' >&2; {fail}; }}\n"
           'export TTP_HOME="${TTP_HOME:-$ttp_real_home/.tt-project}"\n'
           # the cache in force linked in as .cache (cache_link), else the shared checks cache
           'if [ -n "${XDG_CACHE_HOME:-}" ] && [ -d "$XDG_CACHE_HOME" ]; then ln -s "$XDG_CACHE_HOME" "$ttp_home"/.cache\n'
           'elif [ -z "${XDG_CACHE_HOME:-}" ] && [ -d "$ttp_real_home"/.cache ]; then '
           'export XDG_CACHE_HOME="$ttp_real_home"/.cache && ln -s "$XDG_CACHE_HOME" "$ttp_home"/.cache\n'
           'else export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$TTP_HOME/checks-cache}"; fi\n')
    for var, rel in KEEP.items():
        out += f'[ -z "${{{var}:-}}" ] && [ -e "$ttp_real_home"/{shlex.quote(rel)} ] && export {var}="$ttp_real_home"/{shlex.quote(rel)}\n'
    for f in files:
        q = shlex.quote(f)
        out += (f'[ -f "$ttp_real_home"/{q} ] && mkdir -p "$(dirname "$ttp_home"/{q})" && '
                f'cp "$ttp_real_home"/{q} "$ttp_home"/{q}\n')
    for var, rel in XDG.items():
        out += f'mkdir -p "$ttp_home"/{rel} && export {var}="$ttp_home"/{rel}\n'
    return out + 'export HOME="$ttp_home"\n'
