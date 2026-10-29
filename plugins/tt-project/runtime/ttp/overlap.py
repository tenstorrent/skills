# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Parallel code tasks that build the same files: a model-free warning, never a refusal.

Two code tasks run side by side and each adds its own copy of one new module: the APIs clash and
a review fails. So when a `code` or `harness` task is added, the repo paths its spec names
(spec_paths: path-like tokens with a known source extension, or whose folder is in the repo) are
compared with (a) the paths other open code tasks' specs name and (b) the files the branches of
other code tasks still to land add or change (`git diff --name-status <base>...<head>`, cached per
head). A branch counts while its task is open, or once done only while it finished within
`overlap.branch_days`, has a head, is not landed (tried.landed_tasks: a push queue landing of it or
of a successor carrying it on, a supersession), not merged into the base nor on the push branch by
patch (landed.py), and its hand-off did not shelve or reject the change. An overlap gives one short
line (warning) in task_add's result and the new task's line in the digest's open tasks, naming the
most recent few tasks (then '+N more') and the shared paths (new files flagged). A code worker's prompt lists the
unlanded branches that touch what its spec names (prompt_block). Common files never count: the
`overlap.ignore` globs (changelogs, changesets, manifests, `__init__.py`) and hubs, tracked files
larger than `overlap.hub_bytes`, which most changes touch in passing, and the files the check
commands (`delivery.push_checks`) name, which specs cite as the required suite. Folders are no
paths. A shared file alone never chains: the tasks run side by side and the conflict-fix path settles
them at landing. task_add chains the new task (chain) only on a real build dependency: its spec names
a file another open or unlanded task adds (not yet on the base branch), or a symbol (`name()` or a
backticked identifier) found only on that task's branch. A `parallel: <why>` spec line still parses
and is recorded, a no-op now. An open task that holds or waits for a device or machine (it may run for
hours) or that waits itself (blocked, deferred, or on unfinished tasks) is exempt (exempt): it stays
in the warning and label, but no chain waits on it. No network, no model.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import gitmemo, settled

KINDS = ("code", "harness")
OPEN = ("queued", "running", "review", "pushing", "waiting", "blocked")
LABEL = "overlap:"           # a task's label: overlap:<ids>:<warning>, shown in the digest while queued
PARALLEL_LABEL = "parallel:"  # a task's label: the why of its spec's `parallel:` line (a no-op kept for old specs)
PARALLEL = re.compile(r"^[ \t]*parallel:[ \t]*(\S.*?)[ \t]*$", re.M)
CACHE = "overlap_diffs.json"  # state/: {head: [[status, path], ...]}, one git diff per head ever
CACHE_MAX = 400
LANDED_CACHE = "overlap_landed.json"   # state/: [head], branch heads found on the push branch by patch
MAX_PATHS = 4                 # shared paths named in one warning
MAX_TASKS = 3                 # other tasks named in one warning, the most recent first
MAX_BRANCHES = 8              # branches a worker's prompt lists, the most recent first
LS_TTL_S = 60
# A slash path without a known extension (bin/tool, skills/tt-x/SKILL): kept when its folder is in the repo.
_PLAIN = re.compile(r"(?<![\w./:@-])((?:[\w.-]+/)+[\w-][\w.-]*)(?![\w/-])")
# A symbol a spec names: `name()` / `mod.name()`, or a backticked identifier with `_` or `.` in it.
_CALL = re.compile(r"(?<![\w.])((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)\(\)")
_TICK = re.compile(r"`((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)`")
MAX_SYMBOLS = 8               # symbols of one spec looked up on the base and the branches
_ls_cache: dict[str, tuple[float, list[str]]] = {}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True,
                          timeout=gitmemo.timeout(20, args), stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def settings(p) -> dict:
    return (p.config().get("overlap") or {}) if p is not None else {}


def tracked(root: Path) -> list[str]:
    """The repo's tracked files (git ls-files), kept LS_TTL_S per root and read once within a gitmemo
    scope, so the TTL cache never decides what a transaction reads; [] outside git."""
    return gitmemo.cached(("overlap.tracked", str(root)), lambda: _tracked(root))


def _tracked(root: Path) -> list[str]:
    key, now = str(root), time.time()
    hit = _ls_cache.get(key)
    if hit and now - hit[0] < LS_TTL_S:
        return hit[1]
    try:
        out = _git(root, "ls-files").stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        out = []
    _ls_cache[key] = (now, out)
    return out


def same(a: str, b: str) -> bool:
    """Two repo paths name one file: equal, or one is the other below a folder (a spec may name
    runtime/x.py for plugins/tool/runtime/x.py)."""
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _dirs(files: list[str]) -> set[str]:
    out = set()
    for f in files:
        parts = f.split("/")[:-1]
        out.update("/".join(parts[i:j]) for i in range(len(parts)) for j in range(i + 1, len(parts) + 1))
    return out


def spec_paths(text: str, root: Path) -> list[str]:
    """The repo paths `text` names: slash paths with a known source extension, or whose folder the
    repo has. Bare file names (named in passing), the repo's folders and URLs are left out."""
    text = re.sub(r"\b[a-z]+://\S+", " ", text or "")
    known = [f for f in settled.files_in(text) if "/" in f]
    plain = [t for t in _PLAIN.findall(text) if t not in known and "." not in t.rsplit("/", 1)[-1]]
    if plain:
        dirs = _dirs(tracked(root))
        plain = [t for t in plain if t.rsplit("/", 1)[0] in dirs and t.rstrip("/") not in dirs]
    return list(dict.fromkeys(f.removeprefix("./") for f in known + plain))


def _ignored(path: str, globs) -> bool:
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, "*/" + g) for g in globs or ())


def _hub(path: str, root: Path, hub_bytes: int) -> bool:
    """A tracked file this path names is larger than hub_bytes: most changes touch it in passing."""
    if hub_bytes <= 0:
        return False
    for f in tracked(root):
        if same(f, path):
            try:
                if (root / f).stat().st_size > hub_bytes:
                    return True
            except OSError:
                pass
    return False


def _check_paths(p) -> list[str]:
    """The repo paths the project's check commands (delivery.push_checks) name: specs cite them as
    the required suite, not as files they change."""
    from . import push
    cmds = push.check_list((p.config().get("delivery") or {}).get("push_checks"))
    return spec_paths(" ".join(cmds), p.root) if cmds else []


def keep(p, paths) -> list[str]:
    """`paths` without ignored files, hubs and the files the check commands name."""
    s = settings(p)
    hub = int(s.get("hub_bytes") or 0)
    checks = _check_paths(p)
    return [f for f in paths if not _ignored(f, s.get("ignore")) and not _hub(f, p.root, hub)
            and not any(same(f, c) for c in checks)]


def _cache_path(p) -> Path:
    return p.state / CACHE


def _load_cache(p) -> dict:
    try:
        got = json.loads(_cache_path(p).read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def diff(p, base: str, head: str, cache: dict) -> list[tuple[str, str]]:
    """[(A|M|D, path)] of `base...head`, from `cache` (keyed by head) or one git diff."""
    if head in cache:
        return [tuple(x) for x in cache[head]]
    r = _git(p.root, "diff", "--name-status", "--no-renames", f"{base}...{head}")
    if r.returncode != 0:
        return []
    out = [(ln[0], ln.split("\t", 1)[1]) for ln in r.stdout.splitlines() if "\t" in ln]
    cache[head] = out
    return out


def _landed_ids(db) -> set[int]:
    """Tasks the records show landed or superseded, themselves or through a successor that carried
    their work on (a review, a fix, a continuation, a rebase): tried.landed_tasks."""
    from .tried import landed_tasks
    return landed_tasks(db)


def _shelved(result) -> bool:
    """The hand-off says its change was shelved or rejected: nothing of it is to land."""
    from .db import load_result
    return str(load_result(result).get("outcome") or "").strip().lower() in ("shelved", "rejected")


def branches(p, skip=()) -> list[dict]:
    """Code tasks' branches still to land, with commits of their own:
    [{id, branch, head, open, files: [(A|M|D, path)]}]. An open task's branch, or a done task's that
    finished (its last run ended) within `overlap.branch_days`, was not shelved or rejected, is not
    landed by the records and not merged into the base; a diff per head is read once and cached in state/.
    Within a gitmemo scope the base and the heads are read once: a later call whose branches are among
    those already read (`skip` is applied after the read) runs no git."""
    from . import worktree
    days = float(settings(p).get("branch_days") or 14)
    since = time.time() - days * 86400
    rows = p.db.q("SELECT id, branch, status, result, updated FROM tasks WHERE kind IN (%s) AND branch IS NOT NULL "
                  "AND branch != '' AND (status IN (%s) OR (status='done' AND updated>?)) ORDER BY id"
                  % (",".join("?" * len(KINDS)), ",".join("?" * len(OPEN))), (*KINDS, *OPEN, since))
    landed = _landed_ids(p.db)
    rows = [r for r in rows if r["id"] not in landed and (r["status"] in OPEN or not _shelved(r["result"]))]
    done = [r["id"] for r in rows if r["status"] not in OPEN]
    if done:   # `updated` moves with any later write: a done task's finish is its last run's end
        ended = {r["task"]: r["e"] for r in p.db.q("SELECT task, MAX(ended) e FROM runs WHERE task IN (%s) "
                                                     "GROUP BY task" % ",".join("?" * len(done)), done)}
        rows = [r for r in rows if r["status"] in OPEN or (ended.get(r["id"]) or r["updated"] or 0) > since]
    if not rows:
        return []
    try:
        base = gitmemo.cached(("overlap.base",), lambda: worktree.resolve_base(p))
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return []
    heads = _heads(p, base, [f"refs/heads/{r['branch']}" for r in rows])
    cache = _load_cache(p)
    size = len(cache)
    out = []
    for r in rows:
        if r["id"] in skip:
            continue
        head = heads.get(r["branch"])
        if head and (files := diff(p, base, head, cache)):
            out.append({"id": r["id"], "branch": r["branch"], "head": head, "open": r["status"] in OPEN,
                        "files": files})
    if len(cache) != size:
        keepers = dict(list(cache.items())[-CACHE_MAX:])
        from .project import durable_write
        try:
            durable_write(_cache_path(p), json.dumps(keepers))
        except OSError:
            pass
    return out


def _heads(p, base: str, refs: list[str]) -> dict[str, str]:
    """{branch: head} of `refs` not merged into `base`; read once per scope for any refs it covers."""
    memo = gitmemo.current()
    got = memo.get(("overlap.heads", base)) if memo is not None else None
    if got and set(refs) <= got[0]:
        return got[1]
    heads = dict(ln.split(" ", 1)[::-1] for ln in _git(p.root, "for-each-ref", "--no-merged", base,
                                                       "--format=%(objectname) %(refname:short)", *refs
                                                       ).stdout.splitlines() if " " in ln)
    if memo is not None:
        memo[("overlap.heads", base)] = (set(refs), heads)
    return heads


def _on_push_branch(p, b: dict, seen: list) -> bool:
    """A done task's branch is on the push branch under other commits (a rebase, a squash, a replay):
    landed.py's patch checks, local refs only. Read only for branches that touch the new task's
    files; a head found there is kept in `seen` (state/LANDED_CACHE), as it stays there."""
    if b["open"]:
        return False
    if b["head"] in seen:
        return True
    from . import landed

    def read() -> bool:
        try:
            return bool(landed.on_push_branch(p, b["branch"]) or landed.equivalent(
                p, b["branch"], (p.config().get("delivery") or {}).get("version_bump")))
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
    hit = gitmemo.cached(("overlap.on_push_branch", b["branch"], b["head"]), read)
    if hit:
        seen.append(b["head"])
    return bool(hit)


def _load_seen(p) -> list:
    try:
        got = json.loads((p.state / LANDED_CACHE).read_text())
        return got if isinstance(got, list) else []
    except (OSError, ValueError):
        return []


def _save_seen(p, seen: list, size: int) -> None:
    if len(seen) != size:
        from .project import durable_write
        try:
            durable_write(p.state / LANDED_CACHE, json.dumps(seen[-CACHE_MAX:]))
        except OSError:
            pass


def find(p, spec: str, skip=()) -> list[dict]:
    """The other tasks whose spec or unlanded branch names a path `spec` names, the most recent first:
    [{id, open, paths: [(path, new)], branch?, head?}], ignored files and hubs left out."""
    if not settings(p).get("enabled", True):
        return []
    mine = keep(p, spec_paths(spec, p.root))
    if not mine:
        return []
    hits: dict[int, dict] = {}
    open_rows = p.db.q("SELECT id, spec FROM tasks WHERE kind IN (%s) AND status IN (%s) ORDER BY id"
                       % (",".join("?" * len(KINDS)), ",".join("?" * len(OPEN))), (*KINDS, *OPEN))
    for r in open_rows:
        if r["id"] in skip:
            continue
        theirs = spec_paths(r["spec"] or "", p.root)
        shared = [m for m in mine if any(same(m, t) for t in theirs)]
        if shared:
            hits[r["id"]] = {"id": r["id"], "open": True, "paths": {m: False for m in shared}}
    seen = _load_seen(p)
    size = len(seen)
    for b in branches(p, skip):
        shared = {}
        for status, f in b["files"]:
            for m in mine:
                if same(m, f):
                    shared[m] = shared.get(m, False) or status == "A"
        if shared and not _on_push_branch(p, b, seen):
            h = hits.setdefault(b["id"], {"id": b["id"], "open": b["open"], "paths": {}})
            for m, new in shared.items():
                h["paths"][m] = h["paths"].get(m, False) or new
            h.update(branch=b["branch"], head=b["head"])
    _save_seen(p, seen, size)
    for h in hits.values():
        if h["open"] and (why := exempt(p, p.db.task(h["id"]))):
            h["exempt"] = why
    return [{**h, "paths": list(h["paths"].items())} for _, h in sorted(hits.items(), reverse=True)]


def exempt(p, task: dict | None, now: float | None = None, _seen: frozenset = frozenset()) -> str:
    """Why an open task a new one overlaps is never waited on (no depends_on, no chain), '' when it
    may be: it holds or waits for a device or machine (a `resources` lock the project's device locks,
    the shared resources or `ttp machines` name, a `-device` lock, or needs_device), so it may run for
    hours, or it waits itself (blocked, a future start_after, a start_when or unfinished depends_on).
    A wait that is only its own overlap chain (on tasks its overlap label names, label_ids) counts
    only when a task it waits on is exempt, so build-dependency chains still form in line. A fault
    reading it is no exemption."""
    if not task or task["id"] in _seen or len(_seen) > 20:
        return ""
    from . import locks, machines, shared
    from .coordinator import task_resources
    from .db import deferral
    from .landed import task_refs
    try:
        cfg = p.config()
        boxes = locks.device_locks(cfg) | shared.names(cfg) | set(machines.load())
        held = sorted(r for r in task_resources(task) if r in boxes or r.endswith("-device"))
        if held or "needs_device" in json.loads(task.get("labels") or "[]"):
            return f"holds or waits for {', '.join(held) or 'a device'}"
        if task.get("status") == "blocked":
            return "is blocked"
        chained, seen = label_ids(task), _seen | {task["id"]}

        def through(ids) -> str:
            for i in ids:
                if why := exempt(p, p.db.task(i), now, seen):
                    return f"waits on #{i}, which {why}"
            return ""
        d = deferral(task)
        if (d.get("after") or 0) > (time.time() if now is None else now):
            return "is deferred"
        when = d.get("when") or ""
        refs = {int(x) for x in task_refs(when) if x.isdigit()}
        if when and not (refs and refs <= chained and not re.sub(r"(?:ttp landed --task|landed:#)\s*\d+|[\s&()]",
                                                                 "", when)):
            return "is deferred"
        unmet = [i for i in p.db.unmet_dependencies([task])[task["id"]] if i is not None]
        if own := [i for i in unmet if i not in chained]:
            return f"waits on {', '.join(f'#{i}' for i in own[:3])}"
        return through(sorted(refs | set(unmet), reverse=True))
    except Exception:
        return ""


def label_ids(task: dict | None) -> set[int]:
    """The task ids a task's overlap label names (the ids its fix named and every #<id> its warning
    lists): the depends_on its own task_add warning may have suggested."""
    try:
        labels = json.loads((task or {}).get("labels") or "[]")
    except (ValueError, TypeError):
        return set()
    out: set[int] = set()
    for lb in labels if isinstance(labels, list) else []:
        if isinstance(lb, str) and lb.startswith(LABEL):
            ids_s, _, text = lb[len(LABEL):].partition(":")
            out |= {int(x) for x in ids_s.split(",") if x.isdigit()}
            out |= {int(x) for x in re.findall(r"#(\d+) \(", text)}
    return out


def warning(hits: list[dict], landing: bool = True) -> str:
    """One line naming the most recent few other tasks and the shared paths (new files flagged): the
    tasks run side by side and conflicts settle at landing. `landing` is kept for callers."""
    if not hits:
        return ""
    parts = []
    for h in hits[:MAX_TASKS]:
        names = [f"{f} (new file)" if new else f for f, new in h["paths"][:MAX_PATHS]]
        more = len(h["paths"]) - MAX_PATHS
        parts.append(f"#{h['id']} ({', '.join(names)}{f', +{more} more' if more > 0 else ''})")
    more = len(hits) - MAX_TASKS
    it = "it" if len(hits) == 1 else "them"
    return (f"overlaps {'; '.join(parts)}{f'; +{more} more tasks' if more > 0 else ''}: runs beside {it}, "
            f"conflicts settle at landing; chain only if it needs {'its' if it == 'it' else 'their'} new file "
            f"or symbol")


def label(hits: list[dict], chained: int | None = None, landing: bool = True) -> str:
    """overlap:<ids>:<warning>: the digest drops it once the task waits on them all: the task task_add
    chained it after (`chained`), else the non-exempt tasks the warning names."""
    ids = [chained] if chained is not None else [h["id"] for h in hits[:MAX_TASKS] if not h.get("exempt")]
    return f"{LABEL}{','.join(map(str, ids))}:{warning(hits, landing)}" if hits else ""


def digest_note(task: dict) -> str:
    """The overlap warning a queued task carries, '' once it waits on the task(s) it names or has started."""
    if task.get("status") != "queued":
        return ""
    from .db import deferral, dependency_ids
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return ""
    for lb in labels if isinstance(labels, list) else []:
        if isinstance(lb, str) and lb.startswith(LABEL):
            ids_s, _, text = lb[len(LABEL):].partition(":")
            ids = {int(x) for x in ids_s.split(",") if x.isdigit()}
            when = deferral(task).get("when") or ""
            waits = set(dependency_ids(task)) | {int(x) for x in re.findall(r"#(\d+)", when)}
            from .landed import task_refs
            waits |= {int(x) for x in task_refs(when) if x.isdigit()}
            return "" if ids and ids <= waits else text
    return ""


def waits_on(db, tid: int, ids: set[int], landed: set[int] | None = None, limit: int = 200) -> bool:
    """Whether task `tid` is one of `ids` or waits on one: its depends_on and start_when task refs
    (landed.task_refs), followed through the tasks they name. A landed task's wait is over."""
    from .db import deferral, dependency_ids
    from .landed import task_refs
    landed = _landed_ids(db) if landed is None else landed
    seen, todo = set(), [tid]
    while todo and len(seen) < limit:
        t = db.task(todo.pop())
        if not t or t["id"] in seen or t["id"] in landed:
            continue
        if t["id"] in ids:
            return True
        seen.add(t["id"])
        todo += list(dependency_ids(t)) + [int(r) for r in task_refs(deferral(t).get("when")) if r.isdigit()]
    return False


def base_files(p, base: str) -> list[str]:
    """The files on `base` (git ls-tree), kept LS_TTL_S per root and base; [] when unreadable. Within a
    gitmemo scope read once, so the TTL cache never decides what a transaction reads."""
    return gitmemo.cached(("overlap.base_files", base), lambda: _base_files(p, base))


def _base_files(p, base: str) -> list[str]:
    key, now = f"{p.root}\0{base}", time.time()
    hit = _ls_cache.get(key)
    if hit and now - hit[0] < LS_TTL_S:
        return hit[1]
    r = _git(p.root, "ls-tree", "-r", "--name-only", base)
    out = r.stdout.splitlines() if r.returncode == 0 else []
    _ls_cache[key] = (now, out)
    return out


def symbols(spec: str) -> list[str]:
    """The symbols `spec` names (`name()`, or a backticked identifier with `_` or `.`), by last part, in
    the order the spec names them."""
    text = re.sub(r"\b[a-z]+://\S+", " ", spec or "")
    found = [(m.start(), m.group(1)) for m in _CALL.finditer(text)]
    found += [(m.start(), m.group(1)) for m in _TICK.finditer(text) if "_" in m.group(1) or "." in m.group(1)]
    names = [n for _, n in sorted(found)]
    out = [n.rsplit(".", 1)[-1] for n in names if not settled.FILE_RE.fullmatch(n)]
    return list(dict.fromkeys(n for n in out if len(n) >= 4))[:MAX_SYMBOLS]


def _has(p, ref: str, sym: str) -> bool | None:
    """Whether the tree at `ref` has `sym` as a word (git grep); None when unreadable. Read once per
    ref and symbol within a gitmemo scope."""
    def read() -> bool | None:
        r = _git(p.root, "grep", "-q", "-w", "-F", "-e", sym, ref, "--")
        return True if r.returncode == 0 else False if r.returncode == 1 else None
    return gitmemo.cached(("overlap.has", ref, sym), read)


def chain(p, hits: list[dict], spec: str = "", now: float | None = None, lineage: set[int] | None = None,
          skip=()) -> tuple[int, str] | None:
    """(id, why) of the newest open or unlanded task the new one needs the work of: a path the new
    spec shares with it (`hits`, find) that is not on the base branch yet and that task adds (its
    branch adds it; with no branch, its folder or top folder is on the base), or a
    symbol the spec names (symbols) that the base lacks and that task's branch has. None otherwise: a
    shared file alone never chains. A task in the new one's `lineage`, or waiting on it (waits_on),
    is passed over: its work lands only with the new task's. An exempt one (exempt) is passed over too,
    and so is everything when the base cannot be read."""
    from . import worktree
    landed = _landed_ids(p.db)

    def ok(tid: int, is_open: bool) -> bool:
        return tid not in landed and tid not in skip and not (lineage and waits_on(p.db, tid, lineage, landed)) \
            and not (is_open and exempt(p, p.db.task(tid), now))
    shown = [h for h in hits if (h.get("open") or h.get("head")) and not h.get("exempt") and ok(h["id"], h.get("open"))]
    syms = symbols(spec)
    if not shown and not syms:
        return None
    try:
        base = gitmemo.cached(("overlap.base",), lambda: worktree.resolve_base(p))
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None
    on_base = base_files(p, base)
    if not on_base:
        return None
    dirs, tops = _dirs(on_base), {x.split("/", 1)[0] for x in on_base if "/" in x}

    def adds(h: dict, f: str, new: bool) -> bool:
        """`h`'s task adds repo file `f`: its branch adds it, or (no branch yet) `f` is missing from the
        base under a folder the base has. A run dir, harness state or another repo's file never counts."""
        if any(same(f, x) for x in on_base):
            return False
        if h.get("head"):
            return new
        return f.rsplit("/", 1)[0] in dirs or f.split("/", 1)[0] in tops
    for h in shown:   # the most recent first
        if f := next((f for f, new in h["paths"] if adds(h, f, new)), None):
            return h["id"], f"it needs {f}, which #{h['id']} adds (not on the base branch yet)"
    syms = [s for s in syms if _has(p, base, s) is False]
    if syms:
        for b in sorted(branches(p, skip), key=lambda b: -b["id"]):
            if ok(b["id"], b["open"]) and (s := next((s for s in syms if _has(p, b["head"], s)), None)) \
                    and not _on_push_branch(p, b, []):
                return b["id"], f"it needs {s}, which only #{b['id']}'s branch has"
    return None


def prompt_block(p, task: dict) -> str:
    """A code worker's block: the other unlanded branches that add or change a path its spec names."""
    if (task.get("kind") or "") not in KINDS:
        return ""
    try:
        mine = keep(p, spec_paths(task.get("spec") or "", p.root)) if settings(p).get("enabled", True) else []
        if not mine:
            return ""
        lines, seen = [], _load_seen(p)
        size = len(seen)
        for b in reversed(branches(p, skip={int(task.get("id") or 0)})):
            shared = [(f, s == "A") for s, f in b["files"] if any(same(m, f) for m in mine)]
            if shared and not _on_push_branch(p, b, seen):
                names = ", ".join(f"{f} (new file)" if new else f for f, new in shared[:MAX_PATHS])
                lines.append(f"- #{b['id']} {b['branch']} at {b['head'][:10]}: {names}")
        _save_seen(p, seen, size)
    except Exception:   # a hint, never a reason to fail a dispatch
        return ""
    if not lines:
        return ""
    more = len(lines) - MAX_BRANCHES
    lines = lines[:MAX_BRANCHES] + ([f"- +{more} more"] if more > 0 else [])
    return ("## Unlanded branches that touch your files\n"
            "These other tasks' branches add or change files your spec names and have not landed yet. Build on "
            "or coordinate with them: reuse their module and API (read it with `git show <branch>:<path>`), "
            "never create a second one beside it.\n" + "\n".join(lines))
