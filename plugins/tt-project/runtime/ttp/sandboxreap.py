# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""End the sandbox trees an ended run left behind: a `bwrap` (Claude Code's Bash sandbox, or another
sandbox a run started) and all it started, such as the socat proxy listeners on 1080/3128 and the
loopback-port listeners (loopproxy.sh) inside it. bwrap's --die-with-parent watches only the process
that started it; once that one is gone, systemd --user or init adopts the tree and nothing ends it.

A tree is an ended run's (trees()) only when all of these hold: its root is a process named bwrap of
this user whose environment carries this project's TTP_PROJECT and a TTP_RUN_DIR under the project's
runs folder; that run has ended (`ended(run_dir)`); its parent is not a process of that same run (the
tree is orphaned: a live process of the run still owns it otherwise); and it carries no TTP_DETACHED (a
`ttp detach` job's, which jobstop and `ttp detach --stop` own). A command line or a name alone never
counts. A tree with a member that is guarded (the caller and its ancestors, a running worker, the
daemon), another user's, another project's or run's, a `ttp detach` job's or a device user's is kept
whole and reported. Only local processes are read or signalled; Linux only (/proc).

Callers: the runner, for its own run when the agent ended (runner.supervise); the daemon's sweep (its
first pass at start, then each jobstop pass); `ttp leftover-procs --sandboxes`."""
from __future__ import annotations

import os
import time
from pathlib import Path

from . import leftoverstop as ls

SANDBOX_NAMES = ("bwrap",)
EVENT = "sandbox_trees_stopped"


def _name(pid: int) -> str:
    try:
        return (ls.PROC / str(pid) / "comm").read_text().strip()
    except OSError:
        return ""


def _ppid(pid: int) -> int | None:
    try:
        return int((ls.PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def ancestors() -> set[int]:
    out, pid = set(), os.getpid()
    while pid and pid > 1 and pid not in out:
        out.add(pid)
        pid = _ppid(pid)
    return out


def run_ended(p) -> callable:
    """`ended(run_dir)` for project `p`: the run dir has its exit.json (the runner writes it once, at
    the end), or the run's row is no longer running."""
    def ended(run_dir: str) -> bool:
        if (Path(run_dir) / "exit.json").exists():
            return True
        row = p.db.one("SELECT status FROM runs WHERE dir=? OR dir=?", (run_dir, str(Path(run_dir))))
        return bool(row) and row["status"] != "running"
    return ended


def trees(base, ended, guarded: set[int] | None = None, untouchable=None) -> tuple[list[dict], list[dict]]:
    """(the orphaned sandbox trees of this project's ended runs, as {root, run_dir, members: [{pid, start,
    cmd}]}; those kept whole, as {root, run_dir, why}). `base`: the project's tt-project folder; `ended`:
    run dir (resolved) -> whether that run has ended; `untouchable(pid, env)`: why a member must not go,
    or None (default: a `ttp detach` job's or a device user's)."""
    if not ls.PROC.is_dir():
        return [], []
    mine, runs = ls._real(str(base)), ls._real(str(Path(base) / "state" / "runs"))
    guarded = ancestors() if guarded is None else guarded
    me = os.getuid()
    pids = ls._uid_pids()
    kids: dict[int, list[int]] = {}
    for pid in pids:
        if (pp := _ppid(pid)) is not None:
            kids.setdefault(pp, []).append(pid)
    out, kept = [], []
    for pid in pids:
        if _name(pid) not in SANDBOX_NAMES or (env := ls._read_env(pid)) is None:
            continue
        if not env.get("TTP_PROJECT") or ls._real(env["TTP_PROJECT"]) != mine or not env.get("TTP_RUN_DIR"):
            continue
        run = ls._real(env["TTP_RUN_DIR"])
        if Path(run).parent != Path(runs) or env.get("TTP_DETACHED"):
            continue
        parent = _ppid(pid)
        penv = ls._read_env(parent) if parent and parent > 1 else None
        if penv and penv.get("TTP_RUN_DIR") and ls._real(penv["TTP_RUN_DIR"]) == run:
            continue   # not orphaned: a live process of the run still owns it (a nested sandbox, a live job)
        if not ended(run):
            continue
        members, todo, why = [], [pid], None
        while todo:
            m = todo.pop()
            if (s := ls._start(m)) is None:
                continue
            members.append({"pid": m, "start": s, "cmd": ls._cmd(m)[:ls.CMD_CHARS]})
            todo += kids.get(m, [])
            if why:
                continue
            e = ls._read_env(m)
            if m in guarded:
                why = f"member pid {m} is {ls.GUARDED}"
            elif ls._uid(m) != me:
                why = f"member pid {m} is another user's process"
            elif e is None:
                why = f"member pid {m}'s environment cannot be read"
            elif e.get("TTP_PROJECT") and ls._real(e["TTP_PROJECT"]) != mine:
                why = f"member pid {m} is another project's process"
            elif e.get("TTP_RUN_DIR") and ls._real(e["TTP_RUN_DIR"]) != run:
                why = f"member pid {m} is another run's process"
            elif r := (untouchable or _untouchable)(m, e):
                why = f"member pid {m}: {r}"
        if why:
            kept.append({"root": pid, "run_dir": run, "why": why})
        else:
            out.append({"root": pid, "run_dir": run, "members": members})
    return out, kept


def _untouchable(pid: int, env: dict) -> str | None:
    if env.get("TTP_DETACHED"):
        return "a `ttp detach` job's process: stop it with `ttp detach --stop`"
    if any(f.startswith(ls.DEVICE_PREFIX) for f in ls._fds(pid)):
        return "uses a device: end it through the device's broker"
    return None


def reap(base, ended, guarded: set[int] | None = None, untouchable=None, term_s: float = ls.TERM_S,
         dry: bool = False) -> tuple[list[dict], list[dict]]:
    """End each tree trees() finds: SIGTERM to every member, then SIGKILL to those left after term_s
    (a reused pid is never signalled). Returns (the trees ended, or that would be with `dry`; those kept)."""
    found, kept = trees(base, ended, guarded, untouchable)
    if not dry:
        for t in found:
            t["members"] = ls.end(t["members"], term_s)
    return found, kept


def summary(found: list[dict], verb: str = "stopped") -> str:
    n = sum(len(t["members"]) for t in found)
    runs = sorted({Path(t["run_dir"]).name for t in found}, key=lambda r: (len(r), r))
    return (f"{verb} {len(found)} sandbox tree{'s' if len(found) != 1 else ''} of ended runs ({n} "
            f"process{'es' if n != 1 else ''}; runs {', '.join(runs) or 'none'})")


def sweep(p) -> list[dict]:
    """The daemon's pass for project `p`: end its own orphaned sandbox trees of ended runs and write one
    receipt event when it ended any."""
    found, _ = reap(p.base, run_ended(p), ls._guarded(p), lambda pid, env: ls._untouchable(p, pid, env))
    if found:
        import json
        text = summary(found)
        data = {"trees": [{"root": t["root"], "run": Path(t["run_dir"]).name, "pids": [m["pid"] for m in t["members"]]}
                          for t in found]}
        p.db.x("INSERT OR IGNORE INTO events(ts,source,kind,fingerprint,severity,text,data,status) VALUES(?,?,?,?,?,?,?,?)",
               (time.time(), "daemon", EVENT, f"{EVENT}:" + ",".join(f"{t['root']}@{t['members'][0]['start']}"
                                                                     for t in found if t["members"]),
                "low", text, json.dumps(data), "handled"))
    return found
