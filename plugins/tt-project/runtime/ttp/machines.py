# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The user's machines, and resources that keep failing.

~/.tt-project/machines.json lists the machines this user has, by alias, with tags (what each one
offers, e.g. `device`) and a short note. It is per user, so every project of the user sees the same
list; which of them a project may use is the charter's Resources section. A task that runs on a
machine names its alias in `resources`, so failures can be counted per machine.

A resource is in trouble when the tasks using it keep failing in the last 24 h: runs that crashed,
stalled, timed out or were lost, hand-offs that failed or blocked, host reboots while it was held.
The coordinator routes around it: the tasks move to a machine the charter allows that shares its
tags. Many waits alone also show up, but only as a hint: a busy resource is not a broken one.

An entry may also carry `min_free_gb`, the disk guard's threshold on that machine's filesystem: it
overrides the project's `disk.min_free_gb` there (0 turns the guard off on it), so a shared disk that
other services keep near full by design does not hold every project. The entry for the machine a
daemon runs on is the one whose alias, or `hostname`, is this machine's short host name.

A project created with --host runs its daemon on that machine, which reads its own copy of the list.
`push` copies the list there, merged alias by alias with what is there: the newest change wins, a
removal included, so a list edited on that machine is never overwritten by an older one. A change
(`ttp machines add|set|remove`) copies it in the background, never waiting for a host that is down;
hosts whose machine has an open known outage or the `decommissioned` tag are skipped.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from . import project, statcache
from .devq import DEVICE_MASK, MASK_RECORD, unmask_devices  # noqa: F401  (re-exported)

ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,79}")   # an alias is also a resource name
TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,39}")
NOTE_CHARS = 200
DIGEST_MACHINES = 20        # machines the digest lists at most
TROUBLE_AT = 2              # failures in 24 h that put a resource in trouble
WAITS_AT = 6                # or this many waits on it (it may be down, not busy)
BAD_RUNS = ("failed", "lost", "timeout", "stalled")
# The device driver's nodes: `ttp sandbox` hides them from a run that holds no device (sandbox.py).
DEVICE_NODES = ("/dev/tenstorrent",)


def path() -> Path:
    return project.HOME_DIR / "machines.json"


def _doc() -> dict:
    try:
        data = statcache.load_json(path())
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _part(doc: dict, key: str) -> dict:
    got = doc.get(key)
    return got if isinstance(got, dict) else {}


def load() -> dict[str, dict]:
    """{alias: {"tags": [...], "note": str, "added": ts, "updated": ts}}; {} when there is no list yet."""
    return {k: v for k, v in _part(_doc(), "machines").items() if isinstance(v, dict)}


def _save(machines: dict[str, dict], removed: dict[str, float]) -> None:
    """`removed` keeps when each alias was removed, so copies elsewhere learn of the removal."""
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    project.write_json(path(), {"machines": machines, **({"removed": removed} if removed else {})})
    os.chmod(path(), 0o600)


def tag_list(v: Any) -> list[str]:
    """Tags from "device,x86" or a list; rejects anything that is not a plain word."""
    items = v if isinstance(v, list) else str(v or "").replace(",", " ").split()
    tags = []
    for t in items:
        t = str(t).strip().lower()
        if not TAG_RE.fullmatch(t):
            raise ValueError(f"not a tag: {t!r}")
        if t not in tags:
            tags.append(t)
    return tags


def gb_value(v: Any) -> float | None:
    """A disk threshold in GB from text or a number; "" or "none" means none (the project's own)."""
    if v is None or str(v).strip().lower() in ("", "none", "default"):
        return None
    try:
        gb = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"not a size in GB: {v!r}") from None
    if not gb >= 0 or gb == float("inf"):
        raise ValueError(f"not a size in GB: {v!r}")
    return gb


def tag_names(v: Any) -> list[str]:
    """Resource names from "a,b" or a list, in order, each once."""
    items = v if isinstance(v, list) else str(v or "").replace(",", " ").split()
    out = []
    for n in items:
        n = str(n).strip()
        if not ALIAS_RE.fullmatch(n):
            raise ValueError(f"not a resource name: {n!r}")
        if n not in out:
            out.append(n)
    return out


def add(alias: str, tags: Any = None, note: str | None = None, min_free_gb: Any = ...,
        hostname: str | None = None, shared: Any = None, until: str | None = None, where=None) -> dict:
    """Add a machine, or update its tags, note, disk threshold or host name (one left out keeps the
    old value; a threshold or host name of "" removes it). `shared` names the resources on it that
    all of the user's projects share (shared.py): a list or "a,b"; "" the alias itself; False none.
    Left out, it keeps the old ones. `until` (a delay such as 3d or an ISO time) is when the note
    stops being true; past it the digest marks the note stale (a zone-less time is read in the home zone of
    `where`, ends.parse_expires). A new note without one has none; "" removes it."""
    alias = (alias or "").strip()
    if not ALIAS_RE.fullmatch(alias):
        raise ValueError(f"not a machine alias: {alias!r} (letters, digits and _.@+- only)")
    doc = _doc()
    machines, removed = load(), dict(_part(doc, "removed"))
    old = machines.get(alias) or {}
    now = time.time()
    entry = {"tags": tag_list(tags) if tags is not None else old.get("tags", []),
             "note": " ".join(str(note).split())[:NOTE_CHARS] if note is not None else old.get("note", ""),
             "added": old.get("added") or now, "updated": now}
    if until:
        from .ends import parse_expires
        entry["note_until"] = parse_expires(until, now, what="until", where=where)
    elif until is None and note is None and old.get("note_until"):
        entry["note_until"] = old["note_until"]
    gb = old.get("min_free_gb") if min_free_gb is ... else gb_value(min_free_gb)
    if gb is not None:
        entry["min_free_gb"] = gb
    host = old.get("hostname") if hostname is None else hostname.strip()
    if host:
        if not ALIAS_RE.fullmatch(host):
            raise ValueError(f"not a host name: {host!r}")
        entry["hostname"] = host
    if shared is None:
        shared = old.get("shared") or False
    elif shared is not False:
        shared = [alias] if shared == "" else tag_names(shared)
    from . import shared as sh
    sh.check_unshare(old.get("shared"), shared, alias)
    if shared:
        entry["shared"] = shared
    for k in RECOVERY:
        if old.get(k):
            entry[k] = old[k]
    machines[alias] = entry
    removed.pop(alias, None)
    _save(machines, removed)
    return entry


def remove(alias: str) -> bool:
    alias = (alias or "").strip()
    machines, removed = load(), dict(_part(_doc(), "removed"))
    if alias not in machines:
        return False
    from . import shared as sh
    sh.check_unshare(machines[alias].get("shared"), False, alias)
    machines.pop(alias)
    removed[alias] = time.time()
    _save(machines, removed)
    return True


def _when(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def merge(mine: dict, theirs: dict) -> dict:
    """Two copies of the list as one, alias by alias: the newest change wins, a removal included.
    On a tie a machine beats a removal (nothing is lost) and `mine` beats `theirs`."""
    machines, removed = {}, {}
    ours = (_part(mine, "machines"), _part(mine, "removed"))
    other = (_part(theirs, "machines"), _part(theirs, "removed"))
    for alias in set().union(*ours, *other):
        best = None
        for rank, (ms, rs) in ((1, ours), (0, other)):
            m = ms.get(alias)
            if isinstance(m, dict):
                best = max(best or (), (_when(m.get("updated") or m.get("added")), 1, rank, m))
            if alias in rs:
                best = max(best or (), (_when(rs[alias]), 0, rank, rs[alias]))
        if best and best[1]:
            machines[alias] = best[3]
        elif best:
            removed[alias] = best[0]
    return {"machines": machines, **({"removed": removed} if removed else {})}


SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
# ssh's own words for a key it offered being refused (auth_failed), as opposed to a route failure.
AUTH_FAILED_RE = re.compile(r"permission denied \(|too many authentication failures|"
                            r"no more authentication methods", re.I)
REBOOT_RECENT_S = 6 * 3600   # a boot this close before an auth failure makes the reboot its likely cause


def auth_failed_hint(stderr: str | None, booted_at: float | None = None, now: float | None = None) -> str:
    """A hint for an ssh auth_failed (empty for any other failure). One possible cause: on some hosts
    (e.g. a container on local disk) a reboot brings the account's home back empty or owned by another
    user, so its authorized_keys is gone. `booted_at` (the host's last known boot, e.g. from `uptime -s`) narrows it."""
    if not AUTH_FAILED_RE.search(stderr or ""):
        return ""
    now = time.time() if now is None else now
    if booted_at and 0 <= now - booted_at <= REBOOT_RECENT_S:
        lead = f"The host rebooted {(now - booted_at) / 3600:.1f} h ago. One possible cause: its"
    else:
        lead = "If the host rebooted shortly before this, one possible cause: its"
    return (f"{lead} account home may have come back empty or owned by another user, so authorized_keys "
            "is missing. Restore the home's ownership and authorized_keys through the provider's own "
            "console or connect path.")
REMOTE = "~/.tt-project/machines.json"
# Replaces the remote list only if it is still the one read (sha256 of its text), mode 0600.
# A disk error there (a read-only or failing /home) exits 1 with one line, never a traceback.
_WRITE = ("import hashlib,json,os,sys\ntry:\n"
          " h=os.path.expanduser('~/.tt-project');os.makedirs(h,mode=0o700,exist_ok=True)\n"
          " p=os.path.join(h,'machines.json');d=json.load(sys.stdin)\n"
          " try: cur=open(p,'rb').read()\n except FileNotFoundError: cur=b''\n"
          " if hashlib.sha256(cur).hexdigest()!=d['expect']: sys.exit(3)\n"
          " fd=os.open(p+'.tmp',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)\n"
          " os.write(fd,(json.dumps(d['doc'],indent=2,sort_keys=True)+'\\n').encode());os.close(fd)\n"
          " os.chmod(p+'.tmp',0o600);os.replace(p+'.tmp',p)\n"
          "except OSError as e: sys.exit(str(e))")


# A copy gives up on a host that is down within seconds, whatever the user's own ssh retry settings say.
PUSH_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]
PUSH_TIMEOUT_S = 60         # one ssh call of a copy, at most
DECOMMISSIONED = "decommissioned"   # the tag of a machine that is gone for good: nothing is copied to it
PUSH_LOGS = "machines-push"         # in ~/.tt-project: one log per background copy, so copies never mix
PUSH_KEEP = 5                       # the newest copies' logs kept
PUSH_STALE_S = 86400                # an older log with no done line (its copy died) goes after this
PUSH_REPORTED = "reported"          # in PUSH_LOGS: the logs whose failures a change already reported
PUSH_DONE = "finished"              # the background copy's last line


RETIRED_TAGS = (DECOMMISSIONED, "retired")
# A bare "Do not use" (one that goes on with a qualifier, "do not use for builds", only limits how it is used).
DO_NOT_USE_RE = re.compile(r"\bdo(?:n't| not)\s+use\b", re.I)
BARE_USE_RE = re.compile(r"(?:\s+(?:it|this(?:\s+(?:machine|box|host|node))?))?\s*(?:[.!;,)\u2013\u2014-]|$)", re.I)
# A note that retires its machine: "Decommissioned", "retired", or a bare "Do not use".
RETIRED_NOTE_RE = re.compile(r"\b(?:decommissioned|retired)\b|" + DO_NOT_USE_RE.pattern + BARE_USE_RE.pattern, re.I)
# Words right before "retired"/"decommissioned" that make it negated or still to come: "not yet decommissioned",
# "will be decommissioned next month", "to be retired", "never retired".
NOT_YET_RE = re.compile(r"\b(?:not|never|yet|be|being|soon)\s+$", re.I)


def retired(m: dict | None) -> bool:
    """Whether a machines-list entry is gone for good: a decommissioned or retired tag, or a note that
    says decommissioned, retired or "Do not use"."""
    m = m if isinstance(m, dict) else {}
    note = str(m.get("note") or "")
    return (bool(set(RETIRED_TAGS) & set(tag_names(m.get("tags"))))
            or any(not NOT_YET_RE.search(note[:x.start()]) for x in RETIRED_NOTE_RE.finditer(note)))


def decommissioned_user_pause(name: str, row: dict | None, known: dict[str, dict] | None = None) -> bool:
    """Whether the pause `row` of resource `name` is the user's and its machine's list entry is retired
    (retired()): the machine is gone, so the pause is no stuck work and its end needs no prompt. It stays
    as set; only the user's word lifts it. The machine is the row's `machine` (a fleet hold), else the
    alias `name` is or starts with ("box-a", "box-a-device", "box-a:0")."""
    if not isinstance(row, dict) or row.get("by") != "user":
        return False
    known = load() if known is None else known
    alias = row.get("machine") or next((a for a in sorted(known, key=len, reverse=True)
                                        if name == a or name.startswith((a + "-", a + ":"))), None)
    return bool(alias) and retired(known.get(alias))


def push_skips(hosts: list[str], known: dict[str, dict] | None = None,
               outages: dict[str, dict] | None = None) -> dict[str, str]:
    """{host: why} of the hosts a copy of the list skips: its machine has an open known outage or the
    decommissioned tag. A host is matched to a machine by alias or `hostname`, short names included."""
    from . import machine_ledger as ml
    from . import timefmt
    known = load() if known is None else known
    outages = ml.outages() if outages is None else outages
    out = {}
    for host in hosts:
        bare = host.rsplit("@", 1)[-1]     # an ssh target may name the user
        names = {host, bare, bare.split(".")[0]}
        aliases = {a for a, m in known.items() if a in names or {(m.get("hostname") or ""), (m.get("hostname") or "").split(".")[0]} & names - {""}} | (names & set(outages))
        for alias in sorted(aliases):
            if DECOMMISSIONED in tag_names((known.get(alias) or {}).get("tags")):
                out[host] = f"{alias} is tagged {DECOMMISSIONED}"
            elif alias in outages:
                until = float(outages[alias].get("until") or 0)
                out[host] = f"{alias} is in a known outage" + (f" until {timefmt.long(until, timefmt.ambient())}" if until else "")
    return out


def push_log() -> Path:
    """The newest background copy's log (a name in PUSH_LOGS that may not exist yet)."""
    logs = _push_logs()
    return logs[-1] if logs else project.HOME_DIR / PUSH_LOGS / "none.log"


def _push_logs() -> list[Path]:
    """The background copies' logs, oldest first (each name starts with its start time in ns)."""
    return sorted((project.HOME_DIR / PUSH_LOGS).glob("*.log"))


def _push_done(log: Path) -> bool:
    try:
        return PUSH_DONE in log.read_text().splitlines()
    except OSError:
        return False


def prune_push_logs() -> None:
    """Keep the newest PUSH_KEEP logs; an older one goes once its copy is done or died long ago."""
    now = time.time()
    for log in _push_logs()[:-PUSH_KEEP]:
        try:
            if _push_done(log) or now - log.stat().st_mtime > PUSH_STALE_S:
                log.unlink()
        except OSError:
            pass


def push_background(hosts: list[str]) -> Path:
    """Start `ttp machines push --log` for `hosts` in its own session and return its log; never waits.
    Each copy writes its own log, so a copy started while another still runs loses neither's results."""
    import sys
    prune_push_logs()
    log = project.HOME_DIR / PUSH_LOGS / f"{time.time_ns():019d}-{os.getpid()}.log"
    project.durable_write(log, f"copying the machines list to {', '.join(hosts)}\n")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        x for x in (str(Path(__file__).resolve().parent.parent), os.environ.get("PYTHONPATH")) if x)}
    with open(log, "a") as f:
        subprocess.Popen([sys.executable, "-m", "ttp", "machines", "push", "--log", *[f"--to={h}" for h in hosts]],
                         stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, env=env,
                         start_new_session=True)
    return log


def push_failures(mark: bool = False) -> list[str]:
    """The failed copies of the finished background copies not yet reported, oldest first; `mark`
    records those logs as reported, so the next change does not repeat them."""
    seen_file = project.HOME_DIR / PUSH_LOGS / PUSH_REPORTED
    try:
        seen = set(json.loads(seen_file.read_text()))
    except (OSError, ValueError, TypeError):
        seen = set()
    logs = _push_logs()
    out, done = [], []
    for log in logs:
        if log.name in seen or not _push_done(log):
            continue
        done.append(log.name)
        try:
            out += [ln for ln in log.read_text().splitlines() if ln.startswith(("could not copy", "left the machines list"))]
        except OSError:
            pass
    if mark and done:
        names = {p.name for p in logs}
        project.durable_write(seen_file, json.dumps(sorted((seen & names) | set(done))) + "\n")
    return out


def _ssh(argv: list[str], **kw) -> subprocess.CompletedProcess:
    """One ssh call of a copy, bounded by PUSH_TIMEOUT_S; a timeout is exit 124."""
    try:
        return subprocess.run(argv, timeout=PUSH_TIMEOUT_S, **kw)
    except subprocess.TimeoutExpired:
        err = f"timed out after {PUSH_TIMEOUT_S} s"
        return subprocess.CompletedProcess(argv, 124, "", err if kw.get("text") else err.encode())


def _short(err: str) -> str:
    """The last non-empty line of an error output, at most 200 characters: one line, never a traceback."""
    lines = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
    return lines[-1][-200:] if lines else "no error output"


def push(host: str, tries: int = 3) -> str:
    """Copy this user's machines list to another machine (same user), merged with the list there.
    A list there that cannot be read is left alone. Whatever fails, the result is one line."""
    try:
        return _push(host, tries)
    except Exception as e:      # one bad host never stops the copies to the others
        return f"could not copy the machines list to {host}: {type(e).__name__}: {_short(str(e))}"


def _push(host: str, tries: int) -> str:
    mine = _doc()
    if not _part(mine, "machines") and not _part(mine, "removed"):
        return "no machines list to copy"
    for _ in range(tries):
        r = _ssh([*PUSH_SSH, host, f"cat {REMOTE} 2>/dev/null || true"], capture_output=True)
        if r.returncode != 0:
            return f"could not copy the machines list to {host}: {_short(r.stderr.decode(errors='replace'))}"
        try:
            theirs = json.loads(r.stdout) if r.stdout.strip() else {}
        except ValueError:
            theirs = None
        if not isinstance(theirs, dict):
            return f"left the machines list on {host} alone: {REMOTE} there is not a valid list"
        doc = merge(mine, theirs)
        if doc == merge(theirs, {}):
            return f"machines list on {host} is up to date ({len(doc['machines'])} machines)"
        w = _ssh([*PUSH_SSH, host, f"python3 -c {shlex.quote(_WRITE)}"], text=True, capture_output=True,
                 input=json.dumps({"expect": hashlib.sha256(r.stdout).hexdigest(), "doc": doc}))
        if w.returncode == 0:
            return f"copied the machines list ({len(doc['machines'])} machines) to {host}"
        if w.returncode != 3:
            return f"could not copy the machines list to {host}: {_short(w.stderr)}"
    return f"could not copy the machines list to {host}: it kept changing there; try `ttp machines push` again"


def here(known: dict[str, dict] | None = None) -> tuple[str, dict] | None:
    """(alias, entry) for the machine this runs on: its alias or `hostname` is this machine's short
    host name (any case); None when the list has no such entry."""
    me = project.hostname().lower()
    known = load() if known is None else known
    for alias in sorted(known):
        m = known[alias]
        if me in (alias.lower(), str(m.get("hostname") or "").lower()):
            return alias, m
    return None


def local_names() -> set[str]:
    """The names this machine answers to, lower case: its short name, full host name and FQDN, and
    loopback. With TTP_HOST set, that name stands in for the host's own names."""
    names = {project.hostname().lower(), "localhost", "127.0.0.1", "::1"}
    if not os.environ.get("TTP_HOST"):
        import socket
        for get in (socket.gethostname, socket.getfqdn):
            try:
                names.add(get().lower())
            except OSError:
                pass
    return names - {""}


def ssh_hostname(alias: str) -> str:
    """The host `ssh -G` resolves `alias` to (no connection; `alias` itself when it cannot say)."""
    from .lease import ssh_hostname as resolve
    return resolve(alias)


def names_here(value: str, known: dict[str, dict] | None = None) -> bool:
    """Whether `value` names the machine this runs on: one of local_names, a machines-list alias whose
    `hostname` is one of them, or an ssh alias that `ssh -G` resolves to one. The ssh lookup is best
    effort: an error or timeout leaves the plain compare."""
    v = str(value or "").strip().lower()
    if not v:
        return False
    mine = local_names()
    if v in mine:
        return True
    known = load() if known is None else known
    if any(a.lower() == v and str(m.get("hostname") or "").strip().lower() in mine for a, m in known.items()):
        return True
    try:
        real = ssh_hostname(v).strip().lower()
    except Exception:
        return False
    return real != v and real in mine


def device_host(known: dict[str, dict] | None = None) -> bool:
    """Whether this machine's entry is tagged `device`."""
    hit = here(known)
    return bool(hit) and "device" in tag_list(hit[1].get("tags"))


def device_nodes() -> list[str]:
    """DEVICE_NODES, or the paths in env TTP_DEVICE_NODES (separated by `:` or `,`; tests use it)."""
    raw = os.environ.get("TTP_DEVICE_NODES")
    if raw is None:
        return list(DEVICE_NODES)
    return [p for p in re.split(r"[:,]", raw) if p.strip()]


def mask_devices(env: dict, known: dict[str, dict] | None = None) -> dict:
    """`env` with devices hidden from direct opens on a machine tagged `device`, else as it is.
    Masking an already masked environment keeps the first record of the originals."""
    if not device_host(known):
        return env
    out = dict(env)
    if MASK_RECORD not in out:
        out[MASK_RECORD] = json.dumps({k: env.get(k) for k in DEVICE_MASK}, sort_keys=True)
    out.update(DEVICE_MASK)
    return out


def disk_min_free_gb(known: dict[str, dict] | None = None) -> tuple[str, float] | None:
    """(alias, GB) when this machine's entry sets its own disk guard threshold, else None."""
    hit = here(known)
    if not hit:
        return None
    try:
        gb = gb_value(hit[1].get("min_free_gb"))
    except ValueError:
        return None
    return (hit[0], gb) if gb is not None else None


RECOVERY = ("recovery_owner", "recovery_fallback")
PROJECT_RE = re.compile(r"[\w.-]{1,100}")


def set_recovery(alias: str, owner: str | None = None, fallback: str | None = None) -> dict:
    """Name the project that recovers machine `alias` when it stays down or held, and the one that
    takes over when the owner cannot act (machine_ledger.py). None keeps a value, "" removes it."""
    doc = _doc()
    machines, removed = load(), dict(_part(doc, "removed"))
    if alias not in machines:
        raise ValueError(f"no machine {alias!r} in `ttp machines list`")
    entry = dict(machines[alias])
    for key, name in zip(RECOVERY, (owner, fallback)):
        if name is None:
            continue
        name = name.strip()
        if name and not PROJECT_RE.fullmatch(name):
            raise ValueError(f"not a project name: {name!r}")
        if name:
            entry[key] = name
        else:
            entry.pop(key, None)
    if entry.get("recovery_fallback") and not entry.get("recovery_owner"):
        raise ValueError("a fallback needs an owner: give --owner too")
    entry["updated"] = time.time()
    machines[alias] = entry
    _save(machines, removed)
    return entry


def line(alias: str, m: dict) -> str:
    tags = f" [{', '.join(m.get('tags') or [])}]" if m.get("tags") else ""
    host = f" (host {m['hostname']})" if m.get("hostname") else ""
    disk = f" (disk guard {m['min_free_gb']:g} GB)" if isinstance(m.get("min_free_gb"), (int, float)) else ""
    note = f": {m['note']}" if m.get("note") else ""
    if note and isinstance(m.get("note_until"), (int, float)):
        note += f" (until {_day(m['note_until'])})"
    shared = m.get("shared")
    shared = f" (shared by all projects: {', '.join(shared)})" if isinstance(shared, list) and shared else ""
    owner = (f" (recovery owner {m['recovery_owner']}" + (f", fallback {m['recovery_fallback']}"
             if m.get("recovery_fallback") else "") + ")") if m.get("recovery_owner") else ""
    return f"{alias}{tags}{host}{disk}{shared}{owner}{note}"


def alternatives(alias: str, machines: dict[str, dict], avoid: set[str]) -> list[str]:
    """Other machines sharing a tag with `alias`, most shared tags first, none in `avoid`."""
    tags = set((machines.get(alias) or {}).get("tags") or [])
    if not tags:
        return []
    scored = [(-len(tags & set(m.get("tags") or [])), a) for a, m in machines.items()
              if a != alias and a not in avoid and tags & set(m.get("tags") or [])]
    return [a for _, a in sorted(scored)]


def _labels(t: dict) -> set[str]:
    from .coordinator import task_resources
    return task_resources(t)


def stats(db, now: float | None = None) -> dict[str, dict]:
    """Every resource with a failure or wait in the last 24 h: {name: counts}."""
    now = now or time.time()
    since = now - 86400
    stats: dict[str, dict] = {}

    def bump(names, key):
        for n in names:
            s = stats.setdefault(n, {"runs": 0, "handoffs": 0, "reboots": 0, "waits": 0})
            s[key] += 1
    for r in db.q("SELECT r.status, r.note, t.labels FROM runs r JOIN tasks t ON t.id=r.task "
                  f"WHERE r.role!='coordinator' AND r.ended>? AND r.status IN ({','.join('?' * len(BAD_RUNS))})",
                  (since, *BAD_RUNS)):
        if r["status"] == "lost" and "lost_to_reboot" in (r["note"] or ""):
            continue   # counted once, as the reboot below
        bump(_labels(r), "runs")
    # Only what the task's own runs reported: a block the daemon set on a dead dependency says
    # nothing about the resource.
    # A self-wait (its own checks, jobs, push or planned window) says nothing about the resource either.
    from .unblock import is_self_wait
    for e in db.q("SELECT e.kind, e.data, t.labels FROM events e JOIN tasks t ON t.id=e.task "
                  "WHERE e.ts>? AND e.source LIKE 'task:%' "
                  "AND e.kind IN ('task_failed','task_blocked','task_waiting')", (since,)):
        if e["kind"] == "task_waiting" and is_self_wait(e["data"]):
            continue
        bump(_labels(e), "waits" if e["kind"] == "task_waiting" else "handoffs")
    # Reboots count only against machines on the list (a lock such as push:<branch> is not a
    # machine), and only when most of the host's reboots happened while it was held: a host that
    # reboots on its own says nothing about what it held at the time. A planned reboot counts against nothing.
    boots = [b for b in db.boots(since) if not b.get("planned")]
    known = set(load())
    held: dict[str, int] = {}
    for b in boots:
        for n in {str(h).split(":", 1)[0] for h in b.get("held") or [] if ":" in str(h)} & known:
            held[n] = held.get(n, 0) + 1
    bump([n for n, k in held.items() for _ in range(k) if 2 * k > len(boots)], "reboots")
    for s in stats.values():
        s["failures"] = s["runs"] + s["handoffs"] + s["reboots"]
    return stats


def trouble(db, now: float | None = None, seen: dict[str, dict] | None = None) -> dict[str, dict]:
    """Resources whose tasks keep failing in the last 24 h: {name: counts, open task ids}.
    `seen` is stats() already read for the same moment."""
    seen = stats(db, now) if seen is None else seen
    out = {name: dict(s) for name, s in seen.items() if s["failures"] >= TROUBLE_AT or s["waits"] >= WAITS_AT}
    if out:
        for t in db.q("SELECT id, labels FROM tasks WHERE status NOT IN ('done','failed','cancelled') ORDER BY id"):
            for name in _labels(t) & set(out):
                out[name].setdefault("tasks", []).append(t["id"])
    return out


def trouble_line(name: str, s: dict, machines: dict[str, dict], avoid: set[str]) -> str:
    parts = [f"{s[k]} {label}" for k, label in (("runs", "runs crashed, stalled or lost"),
                                                ("handoffs", "hand-offs failed or blocked"),
                                                ("reboots", "host reboots while held"),
                                                ("waits", "waits")) if s.get(k)]
    tasks = s.get("tasks") or []
    on = f"; open tasks on it: {', '.join(f'#{t}' for t in tasks[:10])}" if tasks else "; no open tasks on it"
    if name not in machines:
        alt = "; not in the machines list, so no alternative is known"
    else:
        alts = alternatives(name, machines, avoid)
        alt = (f"; machines sharing its tags: {', '.join(alts)}" if alts
               else "; no other machine shares its tags")
    hint = "; waits only: it may be busy, not down, so do not pause it for this" if not s.get("failures") else ""
    return f"{name}: {', '.join(parts)}{on}{alt}{hint}"


def list_lines() -> list[str]:
    """The Machines section of the coordinator's cached prompt; [] when the list is empty."""
    machines, lines = load(), []
    if machines:
        lines.append("## Machines (the user's list; use only those the charter's Resources allows; "
                     "a task on one names its alias in `resources`)")
        for alias in sorted(machines)[:DIGEST_MACHINES]:
            lines.append(f"- {line(alias, machines[alias])}")
        if len(machines) > DIGEST_MACHINES:
            lines.append(f"- … and {len(machines) - DIGEST_MACHINES} more (`ttp machines list`)")
    return lines


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def stale_notes(machines: dict[str, dict], now: float) -> dict[str, float]:
    """{alias: note_until} for the notes whose `until` passed."""
    return {a: float(m["note_until"]) for a, m in machines.items()
            if m.get("note") and isinstance(m.get("note_until"), (int, float)) and m["note_until"] <= now}


def digest_lines(db, paused: dict | None = None, now: float | None = None) -> list[str]:
    """The Resource trouble section of the coordinator's digest, and machine notes past their
    `until`; [] when there is neither."""
    machines, lines = load(), []
    bad = trouble(db, now)
    if bad:
        avoid = set(bad) | set(paused or {})
        lines.append("## Resource trouble (last 24 h; move its tasks to an allowed healthy alternative)")
        for name in sorted(bad):
            lines.append(f"- {trouble_line(name, bad[name], machines, avoid)}")
    stale = stale_notes(machines, time.time() if now is None else now)
    if stale:
        lines.append("## Machine notes past their end (may no longer hold; do not act on them as current)")
        for alias in sorted(stale)[:DIGEST_MACHINES]:
            lines.append(f"- {alias}: {machines[alias]['note']} (stale since {_day(stale[alias])})")
    return lines
