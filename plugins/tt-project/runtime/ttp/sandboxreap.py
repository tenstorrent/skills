# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""End the sandbox trees an ended run left behind: a `bwrap` (Claude Code's Bash sandbox, or another
sandbox a run started) and all it started, such as the socat proxy listeners on 1080/3128 and the
loopback-port listeners (loopproxy.sh) inside it. bwrap's --die-with-parent watches only the process
that started it; once that one is gone, systemd --user or init adopts the tree and nothing ends it.

A tree is an ended run's (reap()) only when all of these hold: its root is a process named bwrap of
this user whose environment carries this project's TTP_PROJECT and a TTP_RUN_DIR under the project's
runs folder; that run has ended (`ended(run_dir)`); its parent is not a process of that same run (the
tree is orphaned: a live process of the run still owns it otherwise); and it carries no TTP_DETACHED (a
`ttp detach` job's, which jobstop and `ttp detach --stop` own). A command line or a name alone never
counts. A tree with a member that is guarded (the caller and its ancestors, a running worker, the
daemon), another user's, another project's or run's, a `ttp detach` job's or a device user's is kept
whole and reported. Only local processes are read or signalled; Linux only (/proc).

No caller waits on a hung process: the scan of the host's processes ends by WALK_S, and each tree's
reads and its TERM-then-KILL end by ITEM_S past its TERM wait; one past its time is left for a later
pass (its thread ends on its own; until it does that walk is skipped). A caller's `over(what)` stops the
pass between trees; `order` and `stop` let it resume after the last tree done.

Callers: the runner, for its own run when the agent ended (runner.supervise, within RUNNER_S); the
daemon's own sweep_sandbox_trees step (its first pass at start, then each jobstop.SWEEP_EVERY_S, within
STEP_BUDGET_S); `ttp leftover-procs --sandboxes` (within RUNNER_S)."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import leftoverstop as ls

SANDBOX_NAMES = ("bwrap",)
EVENT = "sandbox_trees_stopped"
WALK_S = 20.0     # the scan of the host's processes (each one's name and parent)
ITEM_S = 10.0     # one tree's reads (its root's and members' environments and fds), past its TERM-then-KILL wait
RUNNER_S = 30.0   # a pass with no step budget of its own (the runner's run end, the CLI) starts no tree past this

_late: dict[str, threading.Thread] = {}   # a walk past its time, by key ("scan", "tree <pid>"), until it ends
_said: set[str] = set()                   # late walks logged by sweep(): once each until it ends


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


def within(key: str, timeout: float, fn, *args) -> tuple[bool, object]:
    """fn(*args) in a thread, waited on for at most `timeout` s: (True, its value), or (False, None) while
    it still runs (a /proc read of a hung process blocks; no timeout can interrupt it) or while an earlier
    pass's walk of the same `key` still does. The thread is left to end on its own; its error is raised."""
    if (old := _late.get(key)) is not None:
        if old.is_alive():
            return False, None
        del _late[key]
        _said.discard(key)   # a later hang of it is logged again
    box: dict = {}

    def go():
        try:
            box["v"] = fn(*args)
        except BaseException as e:   # raised in the caller's thread
            box["e"] = e
    t = threading.Thread(target=go, name=f"sandboxreap {key}", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        _late[key] = t
        return False, None
    if "e" in box:
        raise box["e"]
    return True, box.get("v")


def _scan() -> tuple[dict[int, list[int]], list[int]]:
    """(this user's processes by parent, those named as a sandbox)."""
    kids: dict[int, list[int]] = {}
    roots = []
    for pid in ls._uid_pids():
        if (pp := _ppid(pid)) is not None:
            kids.setdefault(pp, []).append(pid)
        if _name(pid) in SANDBOX_NAMES:
            roots.append(pid)
    return kids, roots


def _root_run(pid: int, mine: str, runs: str) -> str | None:
    """The run dir (resolved) of root `pid` when it is an orphaned sandbox of one of this project's runs."""
    if (env := ls._read_env(pid)) is None:
        return None
    if not env.get("TTP_PROJECT") or ls._real(env["TTP_PROJECT"]) != mine or not env.get("TTP_RUN_DIR"):
        return None
    run = ls._real(env["TTP_RUN_DIR"])
    if Path(run).parent != Path(runs) or env.get("TTP_DETACHED"):
        return None
    parent = _ppid(pid)
    penv = ls._read_env(parent) if parent and parent > 1 else None
    if penv and penv.get("TTP_RUN_DIR") and ls._real(penv["TTP_RUN_DIR"]) == run:
        return None   # not orphaned: a live process of the run still owns it (a nested sandbox, a live job)
    return run


def _tree(pid: int, run: str, kids: dict[int, list[int]], mine: str, guarded: set[int], untouchable,
          term_s: float, dry: bool) -> tuple[bool, dict]:
    """(True, the tree {root, run_dir, members}, ended unless `dry`) or (False, {root, run_dir, why}: kept)."""
    me = os.getuid()
    members, todo, why, seen = [], [pid], None, set()
    while todo:
        m = todo.pop()
        if m in seen or (s := ls._start(m)) is None:
            continue
        seen.add(m)
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
        return False, {"root": pid, "run_dir": run, "why": why}
    if not dry:
        members = ls.end(members, term_s)
    return True, {"root": pid, "run_dir": run, "members": members}


def _untouchable(pid: int, env: dict) -> str | None:
    if env.get("TTP_DETACHED"):
        return "a `ttp detach` job's process: stop it with `ttp detach --stop`"
    if any(f.startswith(ls.DEVICE_PREFIX) for f in ls._fds(pid)):
        return "uses a device: end it through the device's broker"
    return None


def deadline(s: float | None = None):
    """An `over` for a pass with no step budget of its own: spent `s` (default RUNNER_S) seconds from now."""
    end = time.monotonic() + (RUNNER_S if s is None else s)
    return lambda what: time.monotonic() >= end


def _late_entry(key: str, root: int | None, run: str, took: float) -> dict:
    return {"root": root, "run_dir": run, "late": key,
            "why": f"its /proc reads took over {took:.0f} s (a hung process); left for a later pass"}


def reap(base, ended, guarded: set[int] | None = None, untouchable=None, term_s: float = ls.TERM_S,
         dry: bool = False, over=None, order=None, stop=None) -> tuple[list[dict], list[dict]]:
    """End the orphaned sandbox trees of this project's ended runs: SIGTERM to every member, then SIGKILL
    to those left after term_s (a reused pid is never signalled). `base`: the project's tt-project folder;
    `ended`: run dir (resolved) -> whether that run has ended (called in this thread: it may read the
    project's database); `untouchable(pid, env)`: why a member must not go, or None (default: a `ttp
    detach` job's or a device user's). Returns (the trees ended, or that would be with `dry`, as {root,
    run_dir, members: [{pid, start, cmd}]}; those kept whole, as {root, run_dir, why}, a walk past its
    time also with "late").

    The pass's time: `over(what)` (default deadline(RUNNER_S)) is asked before each tree but the first;
    once it says the time is spent the pass stops and `stop(root)` gets the last root it did. `order`
    (rows with "id", the root pid) puts the roots in the order to do them, such as after that root."""
    if not ls.PROC.is_dir():
        return [], []
    over = over or deadline()
    mine, runs = ls._real(str(base)), ls._real(str(Path(base) / "state" / "runs"))
    guarded = ancestors() if guarded is None else guarded
    done, scan = within("scan", WALK_S, _scan)
    if not done:
        return [], [_late_entry("scan", None, "", WALK_S)]
    kids, roots = scan
    rows = [{"id": pid} for pid in roots]
    rows = order(rows) if order else sorted(rows, key=lambda r: r["id"])
    out, kept, prev = [], [], None
    for i, row in enumerate(rows):
        pid = row["id"]
        if over(f"sandbox tree {pid}") and i:
            if stop:
                stop(prev)   # the rest go first next pass
            break
        prev, key = pid, f"tree {pid}"
        done, run = within(key, ITEM_S, _root_run, pid, mine, runs)
        if not done:
            kept.append(_late_entry(key, pid, "", ITEM_S))
            continue
        if not run or not ended(run):
            continue
        done, res = within(key, ITEM_S + term_s + 2.0, _tree, pid, run, kids, mine, guarded, untouchable,
                           term_s, dry)
        if not done:
            kept.append(_late_entry(key, pid, run, ITEM_S + term_s + 2.0))
        elif res[0]:
            out.append(res[1])
        else:
            kept.append(res[1])
    return out, kept


def summary(found: list[dict], verb: str = "stopped") -> str:
    n = sum(len(t["members"]) for t in found)
    runs = sorted({Path(t["run_dir"]).name for t in found}, key=lambda r: (len(r), r))
    return (f"{verb} {len(found)} sandbox tree{'s' if len(found) != 1 else ''} of ended runs ({n} "
            f"process{'es' if n != 1 else ''}; runs {', '.join(runs) or 'none'})")


def sweep(p, over=None, order=None, stop=None, say=None) -> list[dict]:
    """The daemon's pass for project `p`: end its own orphaned sandbox trees of ended runs and write one
    receipt event when it ended any. `over`, `order`, `stop`: as reap() takes them (the daemon's step
    budget); `say(text)` hears of each walk past its time once, until that walk ends."""
    found, kept = reap(p.base, run_ended(p), ls._guarded(p), lambda pid, env: ls._untouchable(p, pid, env),
                       over=over, order=order, stop=stop)
    for k in kept:
        if (key := k.get("late")) and key not in _said:
            _said.add(key)
            if say:
                where = f"sandbox tree {k['root']}" if k["root"] else "the scan of the host's processes"
                say(f"sandboxreap: {where}: {k['why']}")
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
