# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The git read memo a finish attempt replays (gitmemo.run, gitmemo.attempt).

Within a finish attempt every git read gitmemo.run makes (a gitmemo.is_read call whose output is captured) is
kept here, process-wide, in a size-capped LRU (MAX_BYTES in all, MAX_ENTRY_BYTES each), and the
same read later is answered from it without spawning git. Key: the repository's dirs, the cwd, argv,
the stdin digest, the GIT_*/locale env and how output is captured. Each entry is classified from its
argv alone, with no per-call-site knowledge:
- immutable: every rev token is a full object id (and the verb reads no worktree or index). Kept
  until evicted, while the history is fixed (gitcache._history_fixed) and the config unchanged.
- ref-dependent: its rev tokens name refs (git's DWIM order, HEAD and @ too). The entry keeps every
  candidate ref's value (a symref's whole chain) and is served only while they are all unchanged.
  The values are read from the files (ref snapshot: loose refs, packed-refs, HEAD and each worktree's
  HEAD, cached by stat), never by running git. A reftable repository makes these volatile.
- volatile: worktree or index reads, no rev token, a token that resolves to nothing, input on stdin,
  options that read every ref, a reflog or the clock. Served only within the attempt that read it,
  for at most VOLATILE_MAX_AGE_S.
- once: an exit code other than 0 and 1, a timeout or an oversize output: served only up to the next
  round of the attempt that read it (gitmemo.strict), then dropped.
Outside an attempt nothing is read from or kept here."""

from __future__ import annotations

import collections
import hashlib
import os
import re
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import gitcache

MAX_BYTES = 256 * 1024 * 1024        # output kept in all
MAX_ENTRY_BYTES = 4 * 1024 * 1024    # one answer larger is kept only for the next round ("once")
VOLATILE_MAX_AGE_S = 30.0
IMM, REF, VOL, ONCE = "immutable", "ref", "volatile", "once"
# subprocess.run arguments a kept answer can stand in for
_KW = frozenset({"capture_output", "stdout", "stderr", "stdin", "input", "text", "universal_newlines", "encoding",
                 "errors", "cwd", "env", "check", "close_fds", "start_new_session"})
_ENV = ("LANG", "LANGUAGE", "HOME", "XDG_CONFIG_HOME", "TZ")
_NO_REPLAY_OPTS = ("--git-dir", "--work-tree", "--namespace", "--bare", "--literal-pathspecs", "--glob-pathspecs")
VOLATILE_VERBS = frozenset({"status", "ls-files", "check-ignore", "grep", "for-each-ref", "show-ref", "name-rev",
                            "describe", "reflog"})
# options that read every ref, a reflog, the index or the clock
VOLATILE_OPTS = ("--all", "--branches", "--tags", "--remotes", "--glob", "--exclude", "--since", "--until",
                 "--after", "--before", "--max-age", "--min-age", "--reflog", "--walk-reflogs", "-g", "--stdin",
                 "--cached", "--staged", "--no-index", "--merge", "--fork-point", "--bisect", "--relative-date",
                 "--date=relative", "--date=human", "--disambiguate", "--indexed-objects", "--alternate-refs")
_CLOCK = ("%ar", "%cr", "%ah", "%ch")
_OBJ_TYPES = frozenset({"blob", "tree", "commit", "tag"})
_NAME = re.compile(r"[\w./+-]+")
_PSEUDO = re.compile(r"[A-Z_]+")

_lock = threading.Lock()
_memo: collections.OrderedDict = collections.OrderedDict()   # key -> Entry, oldest first
_bytes = [0]
_loose: dict[str, tuple[tuple, str | None]] = {}             # ref file -> (stat, contents)
_packed: dict[str, tuple[tuple, dict[str, str]]] = {}        # packed-refs -> (stat, name -> value)
stats = collections.Counter()                                # hits, kept, dropped (for tests and the log)


class _Volatile(Exception):
    pass


class Entry:
    __slots__ = ("kind", "deps", "cstamp", "rc", "out", "err", "size", "att", "born", "until")

    def __init__(self, kind, deps, cstamp, rc, out, err, att, born, until):
        self.kind, self.deps, self.cstamp, self.rc, self.out, self.err = kind, deps, cstamp, rc, out, err
        self.size = len(out or b"") + len(err or b"") + 256
        self.att, self.born, self.until = att, born, until


class Key:
    """One memoizable git call: `k` its key; `dirs` (git dir, common dir); `args` from the verb on."""
    __slots__ = ("k", "dirs", "args", "has_input", "pre")

    def __init__(self, k, dirs, args, has_input):
        self.k, self.dirs, self.args, self.has_input, self.pre = k, dirs, args, has_input, None


def clear() -> None:
    with _lock:
        _memo.clear()
        _bytes[0] = 0
        _loose.clear()
        _packed.clear()
        stats.clear()


def key(argv, kw: dict, is_read, opts_with_value: frozenset) -> Key | None:
    """The memo key of subprocess.run(argv, **kw) when its answer can be kept, else None."""
    if set(kw) - _KW or not argv:
        return None
    out, err = kw.get("stdout"), kw.get("stderr")
    if kw.get("capture_output"):
        out = err = subprocess.PIPE
    if out != subprocess.PIPE or err not in (None, subprocess.PIPE, subprocess.DEVNULL, subprocess.STDOUT):
        return None
    if kw.get("stdin") not in (None, subprocess.DEVNULL):
        return None
    env = os.environ if kw.get("env") is None else kw["env"]
    if any(k in env for k in gitcache.UNSAFE_ENV):
        return None
    argv = tuple(map(str, argv))
    if Path(argv[0]).name != "git":
        return None
    try:
        d = os.fspath(kw.get("cwd") or os.getcwd())
    except (OSError, TypeError):
        return None
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        o = argv[i]
        if o.startswith(_NO_REPLAY_OPTS):
            return None
        if o == "-C" and i + 1 < len(argv):
            d = os.path.join(d, argv[i + 1])
        i += 2 if o in opts_with_value else 1
    args = argv[i:]
    if not is_read(args):
        return None
    dirs = gitcache._dirs(d)
    if not dirs:
        return None
    inp = kw.get("input")
    dig = None if inp is None else hashlib.sha256(inp.encode() if isinstance(inp, str) else inp).hexdigest()
    envk = tuple(sorted((k, v) for k, v in env.items() if k.startswith(("GIT_", "LC_")) or k in _ENV))
    io = (out, err, bool(kw.get("text") or kw.get("universal_newlines")), kw.get("encoding"), kw.get("errors"))
    return Key((dirs, os.path.realpath(d), argv, dig, envk, io), dirs, args, inp is not None)


# --- the ref snapshot: ref values read from the files, never by running git ---

def _file(path: str) -> str | None:
    try:
        s = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(s.st_mode):
        return None
    st = (s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    hit = _loose.get(path)
    if hit and hit[0] == st:
        return hit[1]
    try:
        text = Path(path).read_text(errors="replace").strip()
    except OSError:
        return None
    with _lock:
        _loose[path] = (st, text)
    return text


def _packed_refs(common: str) -> dict[str, str]:
    path = os.path.join(common, "packed-refs")
    st = gitcache._st(path)
    if not st:
        return {}
    hit = _packed.get(path)
    if hit and hit[0] == st:
        return hit[1]
    refs: dict[str, str] = {}
    try:
        for line in Path(path).read_text(errors="replace").splitlines():
            if line and line[0] not in "#^":
                sha, _, name = line.partition(" ")
                refs[name.strip()] = sha
    except OSError:
        return {}
    with _lock:
        _packed[path] = (st, refs)
    return refs


def ref(dirs: tuple[str, str], name: str, depth: int = 0):
    """`name`'s value as git would read it: its raw value, a symref as (its text, the target's value);
    None when absent. Raises _Volatile for a ref this snapshot cannot follow."""
    gitdir, common = dirs
    if name.startswith(("refs/bisect/", "refs/worktree/", "refs/rewritten/")) or depth > 5:
        raise _Volatile(name)
    per = "/" not in name           # HEAD and the pseudorefs live in the worktree's git dir
    raw = _file(os.path.join(gitdir if per else common, name))
    if raw is None and not per:
        raw = _packed_refs(common).get(name)
    if raw is None:
        return None
    if raw.startswith("ref:"):
        return (raw, ref(dirs, raw[4:].strip(), depth + 1))
    return raw


def _candidates(name: str) -> list[str]:
    """git's DWIM order for a short name (refs.c ref_rev_parse_rules)."""
    first = [name] if name.startswith("refs/") or _PSEUDO.fullmatch(name) else []
    return first + [f"refs/{name}", f"refs/tags/{name}", f"refs/heads/{name}", f"refs/remotes/{name}",
                    f"refs/remotes/{name}/HEAD"]


# --- classification from argv alone ---

def _bases(token: str) -> list[str]:
    """The names a rev token starts from ("" for an implied HEAD); raises _Volatile when unsure."""
    if "@{" in token or token.startswith(":"):
        raise _Volatile(token)
    t = token[1:] if token.startswith("^") else token
    for suffix in ("^@", "^!", "^-"):
        if t.endswith(suffix):
            t = t[:-len(suffix)]
    parts = t.split("...") if "..." in t else t.split("..") if ".." in t else [t]
    out = []
    for part in parts:
        part = part.split(":", 1)[0] if ":" in part else part
        base = re.split(r"[\^~]", part, maxsplit=1)[0]
        if base == "" and part:
            raise _Volatile(token)
        out.append("HEAD" if base in ("", "@") else base)
    return out


def classify(k: Key) -> tuple[str, tuple, Any]:
    """(kind, deps, config stamp) of k's answer, read before git runs."""
    verb, rest = k.args[0], k.args[1:]
    common = k.dirs[1]
    cstamp = gitcache._config_stamp(k.dirs)
    if cstamp is None or not gitcache._history_fixed(common):
        return VOL, (), None
    if verb == "patch-id":          # reads only its stdin
        return IMM, (), cstamp
    if verb in VOLATILE_VERBS or k.has_input:
        return VOL, (), None
    revs: list[str] = []
    for a in rest:
        if a == "--":
            break
        if a.startswith("-"):
            if any(a == o or a.startswith(o + "=") for o in VOLATILE_OPTS) or any(c in a for c in _CLOCK):
                return VOL, (), None
            continue
        if any(c in a for c in _CLOCK):
            return VOL, (), None
        if verb == "cat-file" and not revs and a in _OBJ_TYPES:
            continue
        if verb == "ls-tree" and revs:   # its paths follow the one tree-ish
            continue
        revs.append(a)
    if not revs:
        return VOL, (), None
    ends = 0
    deps: list[tuple[str, Any]] = []
    try:
        for tok in revs:
            bases = _bases(tok)
            ends += len(bases)
            for b in bases:
                if gitcache.FULL.fullmatch(b):
                    continue
                if not _NAME.fullmatch(b) or b.endswith(".lock") or ".." in b:
                    return VOL, (), None
                cands = [(c, ref(k.dirs, c)) for c in _candidates(b)]
                if not any(v is not None for _, v in cands):
                    return VOL, (), None    # an abbreviated id, a path or nothing
                deps.extend(cands)
    except _Volatile:
        return VOL, (), None
    if verb == "diff" and ends < 2:     # one side is the worktree
        return VOL, (), None
    if deps and os.path.isdir(os.path.join(common, "reftable")):
        return VOL, (), None
    return (REF if deps else IMM), tuple(deps), cstamp


def _valid(e: Entry, dirs) -> bool:
    if e.kind in (VOL, ONCE):
        return True
    if not gitcache._history_fixed(dirs[1]) or gitcache._config_stamp(dirs) != e.cstamp:
        return False
    try:
        return all(ref(dirs, name) == v for name, v in e.deps)
    except _Volatile:
        return False


def get(k: Key, att_id: int, round_no: int) -> Entry | None:
    """The kept answer for `k` still valid now, else None (and an invalid one is dropped)."""
    with _lock:
        e = _memo.get(k.k)
    if e is None:
        return None
    ok = True
    if e.kind in (VOL, ONCE) and e.att != att_id:
        ok = False
    elif e.kind == VOL and time.monotonic() - e.born > VOLATILE_MAX_AGE_S:
        ok = False
    elif e.kind == ONCE and round_no > e.until:
        ok = False
    elif not _valid(e, k.dirs):
        ok = False
    with _lock:
        if not ok:
            _drop(k.k)
            stats["dropped"] += 1
            return None
        if k.k in _memo:
            _memo.move_to_end(k.k)
        stats["hits"] += 1
    return e


def _drop(kk) -> None:
    e = _memo.pop(kk, None)
    if e is not None:
        _bytes[0] -= e.size


def put(k: Key, r: subprocess.CompletedProcess | None, att_id: int, round_no: int) -> None:
    """Keep git's answer `r` to `k` (None: it timed out), classified by k.pre (read before it ran)."""
    kind, deps, cstamp = k.pre or (VOL, (), None)
    rc = None if r is None else r.returncode
    out, err = (None, None) if r is None else (r.stdout, r.stderr)
    if rc not in (0, 1):
        kind = ONCE
    elif rc and kind == IMM:
        kind = VOL      # e.g. an object not there yet
    e = Entry(kind, deps, cstamp, rc, out, err, att_id, time.monotonic(), round_no + 1)
    if e.size > MAX_ENTRY_BYTES:
        e.kind = ONCE
    if e.kind in (IMM, REF) and not _valid(e, k.dirs):
        return      # a ref moved while git ran: the answer may be of either value
    with _lock:
        _drop(k.k)
        _memo[k.k] = e
        _bytes[0] += e.size
        stats["kept"] += 1
        while _bytes[0] > MAX_BYTES and len(_memo) > 1:
            _drop(next(iter(_memo)))


def end(att_id: int) -> None:
    """The attempt is over: its volatile and once answers go."""
    with _lock:
        for kk in [kk for kk, e in _memo.items() if e.kind in (VOL, ONCE) and e.att == att_id]:
            _drop(kk)


def replay(e: Entry, argv, kw: dict, timeout: float | None) -> subprocess.CompletedProcess:
    if e.rc is None:
        raise subprocess.TimeoutExpired(list(argv), timeout or 0)
    r = subprocess.CompletedProcess(list(argv), e.rc, e.out, e.err)
    if kw.get("check") and e.rc:
        raise subprocess.CalledProcessError(e.rc, list(argv), e.out, e.err)
    return r
