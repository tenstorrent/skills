# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A tripwire on the account's credential files: a run that changed one is named at once.

Runs are unsandboxed, so a tool call can write any file in HOME, a login file too (a check that fed a
patch for ~/.config/gh/ to a hook on the live HOME overwrote gh's hosts file and logged gh out on the
host). The runner (runner.supervise) takes snapshot() as an agent starts and again once it ended, and
writes changes() of the two to the run's exit.json; the daemon (observe) then queues one high
observation per changed path for the coordinator, naming the run. Only stat is read (size, mtime,
inode), never a file's contents.

Two kinds of file (files()):
- stores: gh's hosts file, git's and netrc's credential files and the ssh private keys. Nothing a run
  does rewrites them: created, removed or any change of size, mtime or inode counts.
- logins: the providers' own login files (Provider.credential_files). Their CLIs rewrite them on every
  token refresh, so only a removal, or a rewrite to under half the size it had, counts.

No automatic restore. Without reading contents the harness cannot tell damage from a real change (a
new `gh auth login`, a refreshed token, a rotated key, made by the user or by another run at the same
time); a restore of the start's copy would undo those, and a copy kept beside the file during the run
is one more readable credential while an unsandboxed agent runs. The observation says what to
re-login instead.

store_paths() is the one list of credential stores here, for a credential write guard to adopt.
"""
from __future__ import annotations

import os
from pathlib import Path

# ~/.ssh files that hold no secret
SSH_PUBLIC = {"config", "known_hosts", "known_hosts.old", "authorized_keys", "authorized_keys2", "rc", "environment"}
SHRINK = 0.5   # a login file rewritten below this share of its size counts as damaged


def ssh_private(name: str) -> bool:
    """A file name in ~/.ssh that may hold a private key."""
    return not (name.endswith(".pub") or name in SSH_PUBLIC or name.startswith(("known_hosts", "cm-")) or "%" in name)


def store_paths(home: str | Path | None = None, env=None) -> list[str]:
    """The credential stores of the account whose home is `home` (missing ones too, and the ~/.ssh
    files that exist and may be private keys)."""
    env = os.environ if env is None else env
    home = Path(home or env.get("HOME") or os.path.expanduser("~"))
    gh = Path(env.get("GH_CONFIG_DIR") or Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "gh")
    out = [gh / "hosts.yml", home / ".git-credentials", home / ".netrc"]
    try:
        out += sorted(p for p in (home / ".ssh").iterdir() if ssh_private(p.name) and p.is_file())
    except OSError:
        pass
    return [str(p) for p in out]



def login_paths() -> list[str]:
    """Every provider's login files, as the providers name them."""
    from .providers import all_providers
    out: list[str] = []
    for prov in all_providers():
        try:
            out += [f for f in prov.credential_files() if f not in out]
        except Exception:
            continue
    return out


def files() -> dict[str, str]:
    """{path: "store" | "login"} of this account."""
    out = {p: "login" for p in login_paths()}
    out.update({p: "store" for p in store_paths()})
    return out


def mark(path: str) -> list[int] | None:
    """[size, mtime_ns, inode] of `path`, None when it is missing (stat only, never contents)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns, st.st_ino]


def snapshot(watched: dict[str, str] | None = None) -> dict:
    """{"kinds": {path: kind}, "marks": {path: mark}} of the watched files."""
    watched = files() if watched is None else watched
    return {"kinds": dict(watched), "marks": {p: mark(p) for p in watched}}


def changes(before: dict, after: dict) -> list[list[str]]:
    """[[path, how], ...]: what changed between two snapshots (how: created, removed, changed, shrunk).
    A file only `after` lists (a new ssh key) counts as created when it is a store."""
    out = []
    was, now, kinds = before.get("marks") or {}, after.get("marks") or {}, {**after.get("kinds", {}), **before.get("kinds", {})}
    for path in sorted(set(was) | set(now)):
        a, b = was.get(path), now.get(path)
        if a == b:
            continue
        login = kinds.get(path) == "login"
        if b is None:
            out.append([path, "removed"])
        elif a is None:
            if not login:
                out.append([path, "created"])
        elif not login:
            out.append([path, "changed"])
        elif b[0] < a[0] * SHRINK:
            out.append([path, "shrunk"])
    return out


def observe(db, run: dict, changed: list, now: float) -> int:
    """One high observation per changed path of `run` (a runs row): the run, the path and what to do.
    A change several overlapping runs saw is told once (the fingerprint holds the path and when it was
    seen to change, to the minute). Returns how many it queued."""
    run, told = dict(run), 0
    for path, how in changed:
        fp = f"credwatch:{path}:{int(now // 60)}"
        if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            continue
        who = f"run {run.get('id')}" + (f" of task #{run['task']}" if run.get("task") else f" ({run.get('role')})")
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", "observation", fp, "high",
              f"Credential file {how} during {who}: {path}. A run, or something else on the host while it ran, "
              f"wrote the account's credential file (only its size, time and inode were compared). Find which "
              f"command did it in the run's transcript and stop it recurring; if the login no longer works, "
              f"ask the user to log in again (for gh: `gh auth login`). Nothing was restored.", "queued"))
        told += 1
    return told
