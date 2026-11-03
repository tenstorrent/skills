# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lock-order deadlocks among `ttp lock` holders and waiters, model-free.

A wait-for graph from what `ttp lock` records on this host: each held slot's label (locks.try_take),
each shared holder's label (locks.try_take_shared)
and each live queue ticket (locks.enqueue), in the project's own lock dir and every shared resource's
dir (shared.root(), all projects). A node is a party: a task (project and task id: every run, keeper
and detached job of it) or, outside any task, a process tree (a holder's command, its `ttp lock`, and
any waiter running below them). An edge runs from a waiter to each holder of the lock it waits for,
when every slot of that lock is taken (a shared waiter: by an exclusive holder). Every strongly
connected set of two or more parties is a cycle: none of them can go on until one lets go.

The daemon (Daemon.reap_locks) raises one critical alert per cycle at once, keyed on its member set
(lock-cycle:<hash>), and clears it when the cycle breaks; the per-lock lock-wait alerts of its locks
fold into it. A cycle must show in two graphs read one after the other, so a release and a new wait
read between two files never look like one.

The documented lock order (references/resource-locks.md): narrower locks (one host, device or
machine) before wider ones (site- or cluster-wide: a power-cycle or maintenance lock); never wait for
a narrower lock while holding a wider one. A lock is wide when config `locks.wide` lists it or a word
of its name is in WIDE_WORDS; the alert names each wait that breaks that order.

Only locks taken and waited for through `ttp lock` are seen: a wrapper with its own lock files, or one
that retries `ttp lock --timeout` in a loop (no ticket while it sleeps), is invisible here.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import locks
from .lockreap import _held, _ticket_text, cmdline, label

KEY = "lock-cycle"
WIDE_WORDS = frozenset({"site", "cluster", "global", "fleet", "power", "cycle", "maintenance", "wide"})
_PROJECT_RE = re.compile(r"(?P<project>[A-Za-z0-9][A-Za-z0-9._-]*) task #")


def _ppid(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{int(pid)}/stat").read_text().rpartition(")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def ancestors(pid: int, limit: int = 64) -> list[int]:
    """`pid` and its parents up to (not including) pid 1."""
    out = []
    while pid and pid > 1 and len(out) < limit and pid not in out:
        out.append(pid)
        pid = _ppid(pid)
    return out


def _ticket_pid(t: Path) -> int | None:
    m = re.match(r"[\d.]+-(\d+)", t.name)
    return int(m[1]) if m else None


def width(name: str, cfg: dict | None = None) -> str:
    """"wide" for a site- or cluster-wide lock (config locks.wide, or a word of its name in WIDE_WORDS),
    else "narrow"."""
    listed = ((cfg or {}).get("locks") or {}).get("wide")
    if isinstance(listed, list) and name in listed:
        return "wide"
    return "wide" if set(re.split(r"[^a-z0-9]+", name.lower())) & WIDE_WORDS else "narrow"


def _task_of(text: str, h: dict, project: str | None) -> dict | None:
    """The task a holder label or ticket text names, as {"project", "task", "run"}; None outside a task."""
    from . import hold
    got = hold.parse(text)
    if got:
        return {"project": got["project"] or project, "task": got["task"], "run": got["run"]}
    if str(h.get("task_id") or "").isdigit():
        return {"project": h.get("project") or project, "task": int(h["task_id"]), "run": h.get("run_id")}
    return None


def _project_of(text: str, h: dict, project: str | None) -> str | None:
    m = _PROJECT_RE.match(text or "")
    return (m and m["project"]) or h.get("project") or project


def graph(dirs: list[tuple[Path, str | None]]) -> dict:
    """The wait-for graph over `dirs` ((lock dir, the project it belongs to or None for a shared one)):
    {"parties": {id: {...}}, "edges": [(waiter id, holder id, lock)]}."""
    parties: dict[str, dict] = {}
    procs: list[dict] = []        # holders outside a task: their pids, to group process trees
    locks_seen: list[tuple] = []  # (lock, [(holder record, exclusive)], [(ticket, shared)], blocked)

    def party(pid_: str, **info) -> dict:
        got = parties.setdefault(pid_, {"id": pid_, "holds": set(), "waits": set(), **info})
        got.update({k: v for k, v in info.items() if v and not got.get(k)})   # a holder's label names more
        return got

    for d, proj in dirs:
        try:
            queues = sorted(Path(d).glob("*.queue"))
        except OSError:
            continue
        for q in queues:
            res = q.name[:-len(".queue")]
            tickets = locks.queued(Path(d), res)
            if not tickets:
                continue
            slots = sorted(Path(d).glob(f"{res}.*.lock"))
            states = [(s, _held(s)) for s in slots]
            if not states or any(st in ("free", "") for _, st in states):
                continue   # a free slot: its waiters wait on the queue, not on a holder
            # An exclusive holder labels its slot; a shared one labels itself under <res>.shared (the
            # slot keeps the last exclusive holder's label, which is stale).
            labels = [(label(s), True) for s, st in states if st == "held"]
            shared = [s for s, st in states if st == "shared"]
            if shared:
                labels += [(h, False) for h in locks.shared_holders(shared)]
            held = []
            for h, excl in labels:
                text = str(h.get("holder") or "")
                if not excl and text.endswith(locks.SHARED_MARK):
                    text = text[:-len(locks.SHARED_MARK)]
                rec = {"lock": res, "h": h, "text": text, "project": _project_of(text, h, proj),
                       "task": _task_of(text, h, proj)}
                if not rec["task"]:
                    rec["pids"] = {int(x) for x in (h.get("pid"), h.get("taker_pid")) if str(x or "").isdigit()}
                    procs.append(rec)
                held.append((rec, excl))
            locks_seen.append((res, held, [(t, locks.is_shared(t), proj) for t in tickets]))
    # Process holders below another's pid (a nested `ttp lock` in its command) are one party.
    for rec in procs:
        up = set()
        for pid in rec["pids"]:
            up.update(ancestors(pid))
        rec["up"] = up
    for rec in procs:
        top = [r for r in procs if r["pids"] & rec["up"]]
        # the outermost holder (the fewest ancestors) names the tree
        out = min(top, key=lambda r: (len(r["up"]), min(r["pids"]))) if top else None
        rec["id"] = f"pid {min(out['pids'])}" if out else f"label {rec['lock']}"
    edges = []

    def holder_id(rec: dict) -> str:
        if rec["task"]:
            t = rec["task"]
            p = party(f"{t['project']} task #{t['task']}", kind="task", project=t["project"], task=t["task"],
                      run=t["run"], pid=rec["h"].get("pid"), command=str(rec["h"].get("command") or ""))
        else:
            p = party(rec["id"], kind="process", project=rec["project"], pid=rec["h"].get("pid"),
                      command=str(rec["h"].get("command") or ""))
        p["holds"].add(rec["lock"])
        return p["id"]

    hids = {res: [(holder_id(rec), excl) for rec, excl in held] for res, held, _ in locks_seen}   # holders first:
    for res, _, tickets in locks_seen:                                                         # their labels name a party
        for t, shared_wait, proj in tickets:
            text = _ticket_text(t)
            task = _task_of(text, {}, proj)
            if task:
                w = party(f"{task['project']} task #{task['task']}", kind="task", project=task["project"],
                          task=task["task"], run=task["run"], pid=None, command="")
            else:
                pid = _ticket_pid(t)
                up = set(ancestors(pid)) if pid else set()
                mine = [r for r in procs if r["pids"] & up]
                wid = mine[0]["id"] if mine else f"pid {pid}"
                w = party(wid, kind="process", project=_project_of(text, {}, proj), pid=pid,
                          command=cmdline(pid)[:200] if pid else "")
            w["waits"].add(res)
            for hid, excl in hids[res]:
                if hid != w["id"] and (excl or not shared_wait):
                    edges.append((w["id"], hid, res))
    return {"parties": parties, "edges": edges}


def _sccs(nodes, edges) -> list[set]:
    """Strongly connected sets of two or more nodes (Tarjan, iterative)."""
    out_edges: dict[str, list] = {n: [] for n in nodes}
    for a, b, _ in edges:
        out_edges.setdefault(a, []).append(b)
    index, low, on, stack, found, n_ = {}, {}, set(), [], [], [0]
    for root in sorted(out_edges):
        if root in index:
            continue
        work = [(root, iter(out_edges[root]))]
        index[root] = low[root] = n_[0]
        n_[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            nxt = next(it, None)
            if nxt is not None:
                if nxt not in index:
                    index[nxt] = low[nxt] = n_[0]
                    n_[0] += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(out_edges.get(nxt, []))))
                elif nxt in on:
                    low[v] = min(low[v], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = set()
                while True:
                    x = stack.pop()
                    on.discard(x)
                    comp.add(x)
                    if x == v:
                        break
                if len(comp) > 1:
                    found.append(comp)
    return found


def cycles(g: dict) -> list[dict]:
    """Each cycle in graph `g`: {"members": [party, ...], "edges": [(waiter, holder, lock)], "locks"}."""
    out = []
    for comp in _sccs(g["parties"], g["edges"]):
        es = sorted({e for e in g["edges"] if e[0] in comp and e[1] in comp})
        out.append({"members": [g["parties"][m] for m in sorted(comp)], "edges": es,
                    "locks": sorted({e[2] for e in es}),
                    "projects": sorted({str(g["parties"][m].get("project")) for m in comp
                                        if g["parties"][m].get("project")})})
    return out


def find(dirs: list[tuple[Path, str | None]]) -> list[dict]:
    """The cycles seen in two graphs read one after the other (the same member set in both)."""
    first = {key(c): c for c in cycles(graph(dirs))}
    if not first:
        return []
    return [c for c in cycles(graph(dirs)) if key(c) in first]


def key(c: dict) -> str:
    ids = "\n".join(sorted(m["id"] for m in c["members"]))
    return f"{KEY}:{hashlib.sha256(ids.encode()).hexdigest()[:12]}"


def who(m: dict) -> str:
    if m["kind"] == "task":
        run = f"run {m['run']}, " if m.get("run") not in (None, "", "?") else ""
        return (f"project {m['project'] or '?'} task #{m['task']} ({run}pid {m.get('pid') or '?'}"
                + (f", `{m['command'][:80]}`" if m.get("command") else "") + ")")
    return (f"a command outside any task (pid {m.get('pid') or '?'}, `{(m.get('command') or '?')[:80]}`"
            + (f", project {m['project']}" if m.get("project") else "") + ")")


def breaks(c: dict, cfg: dict | None = None) -> list[str]:
    """Each wait of the cycle that breaks the lock order: for a narrower lock while holding a wider one."""
    out = []
    held_in = {e[2] for e in c["edges"]}
    for m in c["members"]:
        wide = sorted(x for x in m["holds"] & held_in if width(x, cfg) == "wide")
        for w_, _h, lk in c["edges"]:
            if w_ == m["id"] and wide and width(lk, cfg) == "narrow":
                out.append(f"{who(m)} waits for narrower lock {lk} while holding wider lock {', '.join(wide)}")
    return sorted(set(out))


def text(c: dict, cfg: dict | None = None) -> str:
    """The critical alert: every party with what it holds and waits for, every lock, and the waits that
    break the documented lock order."""
    names = {m["id"]: m for m in c["members"]}
    lines = []
    for m in c["members"]:
        waits = [f"{lk} (held by {who(names[h])})" for w_, h, lk in c["edges"] if w_ == m["id"]]
        held = sorted(m["holds"] & {e[2] for e in c["edges"]})
        lines.append(f"{who(m)[0].upper()}{who(m)[1:]} holds {', '.join(held)} and waits for {'; '.join(waits)}")
    bad = breaks(c, cfg)
    order = ("Breaks the lock order (narrower locks before wider ones; never wait for a narrower lock "
             "while holding a wider one): " + "; ".join(bad) + "." if bad else
             "Every lock in it is of one width, so the documented order does not rank them: two parties "
             "took them in opposite orders; take them in one fixed order (e.g. by name), or as one `ttp lock A,B`.")
    return (f"Lock-order deadlock: {len(c['members'])} parties wait on each other over locks "
            f"{', '.join(c['locks'])}; none can go on until one lets go. " + ". ".join(lines) + ". " + order
            + " Break it by ending the party that breaks the order (or any one of them); a script that needs "
              "both takes the narrower lock first, or releases the wider one before it waits.")[:3000]
