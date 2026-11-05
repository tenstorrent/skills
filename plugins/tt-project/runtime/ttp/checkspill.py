# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Checks that spill to other machines when this one's check slots are busy: `delivery.checks_spill_hosts`.

A project may list, in order, the ssh aliases of other machines its checks may run on. The account's
pool, `checks.spill_hosts` in the account settings (`ttp config --account checks.spill_hosts '["pool-a"]'`,
which a run may only clear), follows that list in every project on this machine, so each spills
without a list of its own. A project's own empty list (`[]`) keeps its checks here, the pool too;
with neither, checks never leave this machine. A `ttp checks` run that has waited `delivery.checks_spill_after_s`
(default SPILL_AFTER_S) for a slot here (its project's lane, a host-wide checks slot, test-worker
slots) tries the listed hosts in order and runs on the first that is allowed, reachable and has a
free slot, as a check with a check host does (remotecheck: a copy of exactly the commit, its output
streamed back). With none, it keeps its place in the queue here and tries them again after the same
bound. The run says where it went, checks.json records it (`hosts`, `spilled_to`), and `ttp status`
and `ttp checks status` show it while it runs (status_lines).

Allowed: named in the project's charter (a host of the account's pool needs no charter line: the
account setting is the user's word for every project) and in the user's machines list (not retired), and not
paused (a pause of its alias, or a fleet hold of the machine, or a pause of its alias or host name that any
of the user's projects here set, shared or not: paused_elsewhere), and its machine lock (`ttp lock <alias>`)
neither held nor reserved by anyone else. The run holds that lock while it runs, shared where the
resource allows it (several spilled checks share it; an exclusive holder or waiter keeps them off),
exclusively on a device lock. A host that is not allowed, whose lock is busy, that ssh does not reach,
that lacks git or tar, or whose slots are full is skipped, never a failure. So is a host where a device job
runs (device_busy, a lock-based safety gate, never a neighbour or site check): any of its device locks
(`<alias>-device`, the device names of its machines-list `shared` entry, the project's `device.locks` on it)
held, reserved or waited for, here or in the user's shared locks, or a running job row in the status of a
broker this project watches on it (a heal check with preset broker on that machine), or a process on it
that holds a Tenstorrent device node (DEVICE_NODES) open and is no check run of the pool (_device_holders,
in the host's probe: a device test that takes no lock). A broker whose status does not read within
BROKER_TIMEOUT_S, or reads no job rows, never keeps a run off: the locks alone decide. Nor does a holder scan
that cannot run or finish within DEVICE_SCAN_S: the run says so. The scan reads, without sudo, only what the
ssh user may (/proc/<pid>/fd of its own processes, and others' where the host lets it).
A project whose own rules let CPU-only checks run beside device jobs on a host lists it in
`checks.spill_beside_device` (ssh aliases, or "*" for every spill host; default none: beside_device): there
neither the device locks, the broker nor the holder scan keep a run off (its pause and machine lock still
do), and the run and its `ttp status` line say it runs beside a device job.
Runs already on the host go on. The skip shows in one line in `ttp status` and the web app (skip_lines)
while it is fresh (SKIP_SHOW_S). Its slots follow what it has idle now (checklane.host_slots): the runs
its idle cores (cores less the 1-minute load average) and available memory fit, at most the runs its
cores hold less the remote checks running there from any machine (their ttp-check.* folders), not a
small fixed count; a host whose load does not read gets checklane.default_slots less those. This machine also holds one claim file
per slot it uses there (spill/<alias>.<n>.json in the checks slot folder), so its own runs never
take more. A claim whose remote ttp-check.* folder is not made yet (the few seconds of its copy) is
counted by neither, so the host can take one run more than its slots in that gap: a bound, not a guard.

Only a run whose every check may spill spills (check_spills): none with a host of its own (`host`,
delivery.checks_host; `"host": "local"` too unless the check says `"spill": true`), none that says
`"spill": false`, none run in the authoring worktree ("cwd": "authoring"), none behind checks.wrap_cmd
(the project's lock or queue wrapper, which may hold a device or a machine here) unless it has a remote
wrapper (the check's `spill_wrap_cmd`, else checks.spill_wrap_cmd: push.spill_wrap_of) or the project
says its wrappers need no device (`checks.wrap_needs_device: false`; unset or true: the need is unknown
or real, so it stays here), and never a light run (it never waits). On a spill host a check never runs
behind this machine's checks.wrap_cmd: it runs behind its remote wrapper when it has one (push.spill_to),
a wrapper missing there being a host fault (remotecheck), else unwrapped. A run that does not pass on a spill host runs again here (OFF_ENV):
a spill host's own environment never fails a change, only a pass there counts.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from . import remotecheck

KEY = "checks_spill_hosts"
ACCOUNT_KEY = "checks.spill_hosts"   # the account's pool (project.ACCOUNT_KEYS), after a project's own list
BESIDE_KEY = "spill_beside_device"   # checks.spill_beside_device: hosts a device job keeps no spill off (beside_device)
NO_DEVICE_KEY = "wrap_needs_device"   # checks.wrap_needs_device false: a wrapped run needs no device and may spill
AFTER_KEY = "checks_spill_after_s"
SPILL_AFTER_S = 60.0
OFF_ENV = "TTP_CHECKS_NO_SPILL"   # set for the local rerun of a run that did not pass on a spill host
PROBE_TIMEOUT_S = 30
DEVICE_SUFFIX = "-device"
DEVICE_WHY = "a device job runs there: "   # how a device-busy reason starts (device_busy, _device_holders)
BROKER_TIMEOUT_S = 10.0   # one read of a broker's status; past it the device locks alone decide
SPILL_MARK = {"checks_spill": True}   # in the label of a pool check run's hold of a host's lock (_hold)
SKIP_SHOW_S = 900   # a host skipped for a device job shows in `ttp status` and the web app this long after
_JOB_LISTS = ("jobs", "running_jobs", "active_jobs", "running")   # broker status: job rows (a list) or a count
_JOB_ONE = ("current_job", "active_job", "running_job")   # broker status: the one job under way
_RUNNING = {"running", "active", "started", "in_progress", "in-progress", "busy"}   # a `jobs` row's state that runs
RUNNING_MAX_AGE_MIN = 720   # a remote ttp-check.* folder older than this is a leftover, not a running check
_PROBE = ("command -v git >/dev/null 2>&1 && command -v tar >/dev/null 2>&1 || exit 3\n"
          "c=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)\n"
          "m=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null)\n"
          "n=$(find \"${TMPDIR:-/tmp}\" -maxdepth 1 -name 'ttp-check.*' -type d "
          f"-mmin -{RUNNING_MAX_AGE_MIN} 2>/dev/null | wc -l)\n"
          "l=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)\n"
          "echo \"ttp-spill $c ${m:-0} $n ${l:--}\"\n")
DEVICE_NODES = "/dev/tenstorrent*"   # a host's Tenstorrent device nodes (a glob); a process holding one open is a tenant
NODES_ENV = "TTP_TEST_DEVICE_NODES"   # tests: the device node glob instead of DEVICE_NODES
DEVICE_SCAN_S = 15   # the holder scan's share of PROBE_TIMEOUT_S; past it the scan is cut and fails open
DEVICE_HOLDERS_SHOWN = 3   # holders named in a skip reason; the rest are counted
# One line per holder process ("ttp-spill-dev <pid> <device> <command name>") but the pool's own check runs (their
# working folder, or the PWD they inherited, in a ttp-check.* folder of the host's temp folder: $2 as given, $3
# resolved), then "ttp-spill-dev-end". GNU find reads
# each /proc/<pid>/fd the ssh user may read; an unreadable one is skipped.
_SCAN = ("find /proc -maxdepth 0 -lname x -printf '' >/dev/null 2>&1 || { echo 'ttp-spill-dev-end nofind'; exit 0; }\n"
         "find /proc/[0-9]*/fd -maxdepth 1 -lname \"$1\" -printf '%h %l\\n' 2>/dev/null"
         " | awk '{split($1, a, \"/\"); if (!s[a[3]]++) print a[3], $2}' |\n"
         "while read -r p dev; do\n"
         "  case $(readlink /proc/$p/cwd 2>/dev/null) in \"$2\"/ttp-check.*|\"$3\"/ttp-check.*) continue;; esac\n"
         "  case $(tr '\\0' '\\n' </proc/$p/environ 2>/dev/null | sed -n 's/^PWD=//p') in\n"
         "    \"$2\"/ttp-check.*|\"$3\"/ttp-check.*) continue;; esac\n"
         "  echo \"ttp-spill-dev $p $dev $(head -c 40 /proc/$p/comm 2>/dev/null | tr -c 'A-Za-z0-9._+:\\n-' '_')\"\n"
         "done\n"
         "echo ttp-spill-dev-end\n")


def _probe_script() -> str:
    """The host's probe: its slots line (_PROBE), then the device holder scan (_SCAN, bounded by DEVICE_SCAN_S
    with `timeout`; none there: not run) when a DEVICE_NODES node exists, and the scan's exit code."""
    glob = os.environ.get(NODES_ENV) or DEVICE_NODES
    return (_PROBE + f"set -- {glob}\n"
            "t=${TMPDIR:-/tmp}; t=${t%/}\n"
            "if [ ! -e \"$1\" ]; then echo 'ttp-spill-dev-end none'\n"
            "elif ! command -v timeout >/dev/null 2>&1; then echo 'ttp-spill-dev-end notimeout'\n"
            f"else timeout -k 2 {DEVICE_SCAN_S} sh -c {shlex.quote(_SCAN)} _ {shlex.quote(glob)} \"$t\" \"$(cd \"$t\" 2>/dev/null && pwd -P)\" </dev/null; "
            "echo \"ttp-spill-dev-rc $?\"; fi\n")


def _device_holders(out: str) -> tuple[str, str]:
    """From a probe's output: (why the host is device-busy, "") when the scan names a holder, ("", why the scan
    did not read) when it was cut, could not run or printed no end line, else ("", "")."""
    held = re.findall(r"^ttp-spill-dev (\d+) (\S+) ?(.*)$", out, re.M)
    end = re.search(r"^ttp-spill-dev-end ?(\S*)$", out, re.M)
    rc = re.search(r"^ttp-spill-dev-rc (\d+)$", out, re.M)
    if held:
        named = ", ".join(f"pid {p} ({c or '?'}) holds {d}" for p, d, c in held[:DEVICE_HOLDERS_SHOWN])
        more = len(held) - DEVICE_HOLDERS_SHOWN
        return DEVICE_WHY + named + (f" and {more} more process(es)" if more > 0 else ""), ""
    if end and end.group(1) in ("", "none"):
        return "", ""
    if rc and rc.group(1) in ("124", "137"):
        return "", f"its device holder scan timed out after {DEVICE_SCAN_S} s"
    if end and end.group(1) == "nofind":
        return "", "its device holder scan cannot run (no GNU find there)"
    if end and end.group(1) == "notimeout":
        return "", "its device holder scan cannot run (no timeout command there)"
    return "", f"its device holder scan did not finish (exit {rc.group(1) if rc else 'unknown'})"


def hosts_of(v: Any) -> list[str]:
    """A `delivery.checks_spill_hosts` value set by `ttp config`: a list (or a JSON-encoded list) of ssh
    aliases. ValueError for anything else: a scalar or a table is refused, never read as one host."""
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            v = json.loads(v)
        except ValueError:
            pass
    if not isinstance(v, (list, tuple)):
        raise ValueError(f"delivery.{KEY}: {v!r} is not a list of ssh aliases (e.g. [\"check-host-a\"]); "
                         "nothing was changed")
    out = [str(x).strip() if isinstance(x, str) else x for x in v]
    if bad := [why for x in out if (why := _host_problem(x))]:
        raise ValueError(f"delivery.{KEY}: " + "; ".join(bad) + "; nothing was changed")
    return list(dict.fromkeys(out))


def _host_problem(x: Any) -> str:
    if x == remotecheck.LOCAL:
        return f"{x!r} is this machine, not a host to spill to"
    return remotecheck.host_problem(x)


def problems(delivery: dict) -> list[str]:
    """Config check of the spill settings."""
    out = []
    v = (delivery or {}).get(KEY)
    if v is not None:
        try:
            hosts_of(v)
        except ValueError as e:
            out.append(str(e).replace("; nothing was changed", ""))
    a = (delivery or {}).get(AFTER_KEY)
    if a is not None and (isinstance(a, bool) or not isinstance(a, (int, float)) or a < 0):
        out.append(f"delivery.{AFTER_KEY}: {a!r} is not a number of seconds")
    return out


def account_hosts() -> list[str]:
    """The account's pool (checks.spill_hosts in the account settings) in order; [] when unset or malformed."""
    from . import project
    try:
        return hosts_of((project.load_account_settings().get("checks") or {}).get("spill_hosts") or [])
    except ValueError:
        return []


def hosts(cfg: dict | None) -> list[str]:
    """The project's spill hosts in order, then the account's pool; [] when the project's own list is
    empty (it keeps its checks here) or malformed (config_problems names it)."""
    own = ((cfg or {}).get("delivery") or {}).get(KEY)
    try:
        mine = hosts_of(own) if own is not None else []
    except ValueError:
        return []
    if own is not None and not mine:
        return []
    return list(dict.fromkeys([*mine, *account_hosts()]))


def _source(cfg: dict | None, alias: str) -> str:
    """Which setting lists `alias`: the project's own list, else the account's pool."""
    try:
        own = hosts_of(((cfg or {}).get("delivery") or {}).get(KEY) or [])
    except ValueError:
        own = []
    return f"delivery.{KEY}" if alias in own else f"{ACCOUNT_KEY}, the account's pool"


def wrapped_ok(cfg: dict | None) -> bool:
    """Whether the project says the commands behind its wrappers need no device (checks.wrap_needs_device
    false); unset, true or anything else: unknown, so a wrapped run stays here."""
    checks = (cfg or {}).get("checks")
    return isinstance(checks, dict) and checks.get(NO_DEVICE_KEY) is False


def beside_of(v: Any) -> str | list[str]:
    """A `checks.spill_beside_device` value set by `ttp config`: "*" (every spill host) or a list (or a
    JSON-encoded list) of ssh aliases; a list naming "*" is "*". ValueError for anything else."""
    if isinstance(v, str) and v.strip() == "*":
        return "*"
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            v = json.loads(v)
        except ValueError:
            pass
    if not isinstance(v, (list, tuple)):
        raise ValueError(f"checks.{BESIDE_KEY}: {v!r} is not a list of ssh aliases (e.g. [\"check-host-a\"]) "
                         "or \"*\"; nothing was changed")
    out = [str(x).strip() if isinstance(x, str) else x for x in v]
    if "*" in out:
        return "*"
    if bad := [why for x in out if (why := remotecheck.host_problem(x))]:
        raise ValueError(f"checks.{BESIDE_KEY}: " + "; ".join(bad) + "; nothing was changed")
    return list(dict.fromkeys(out))


def beside_device(cfg: dict | None, alias: str) -> bool:
    """Whether the project lets its checks spill to `alias` while a device job runs there
    (checks.spill_beside_device names it, or is "*"); unset or malformed: no."""
    checks = (cfg or {}).get("checks")
    v = checks.get(BESIDE_KEY) if isinstance(checks, dict) else None
    try:
        got = beside_of(v) if v is not None else []
    except ValueError:
        return False
    return got == "*" or alias in got


def wrap_problems(checks: Any) -> list[str]:
    """Config check of checks.wrap_needs_device, checks.spill_wrap_cmd and checks.spill_beside_device."""
    from . import push
    v = checks.get(NO_DEVICE_KEY) if isinstance(checks, dict) else None
    out = [] if v is None or isinstance(v, bool) else [f"checks.{NO_DEVICE_KEY}: {v!r} is not true or false"]
    if (b := checks.get(BESIDE_KEY) if isinstance(checks, dict) else None) is not None:
        try:
            beside_of(b)
        except ValueError as e:
            out.append(str(e).replace("; nothing was changed", ""))
    try:
        push.spill_wrap_of(None, {"checks": checks})
    except ValueError as e:
        out.append(str(e))
    return out


def after_of(v: Any) -> float:
    """A `delivery.checks_spill_after_s` value set by `ttp config`: seconds, at least 0."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        n = -1.0
    if isinstance(v, bool) or not n >= 0:
        raise ValueError(f"delivery.{AFTER_KEY}: {v!r} is not a number of seconds; nothing was changed")
    return n


def after_s(cfg: dict | None) -> float:
    """How long a run waits here before it tries the spill hosts."""
    v = ((cfg or {}).get("delivery") or {}).get(AFTER_KEY)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else SPILL_AFTER_S


def check_spills(c: Any, cfg: dict | None = None) -> bool:
    """Whether check `c` may run on a spill host: it would run on this machine (its host "local" only
    with `"spill": true`), in a clean checkout, does not say `"spill": false`, and is unwrapped unless it
    has a remote wrapper (push.spill_wrap_of; one that does not parse keeps it here) or `cfg` says its
    wrappers need no device (wrapped_ok)."""
    from . import cleantree, push
    opt = getattr(c, "spill", None)
    if opt is False or getattr(c, "keep_here", False) or push.remote_host(c) or cleantree.authoring(c):
        return False
    if getattr(c, "host", "") == remotecheck.LOCAL and opt is not True:
        return False
    try:
        remote_wrap = push.spill_wrap_of(c, cfg)
    except ValueError:
        return False
    return bool(remote_wrap) or wrapped_ok(cfg) or not getattr(c, "wrap", ())


def spillable(cmds: list, cfg: dict | None = None) -> bool:
    """Whether every check of `cmds` may run on a spill host (check_spills)."""
    return bool(cmds) and all(check_spills(c, cfg) for c in cmds)


def _charter_names(p, alias: str) -> bool:
    try:
        text = p.charter_path.read_text()
    except (OSError, AttributeError):
        return False
    return bool(re.search(rf"(?<![\w.@+:-]){re.escape(alias)}(?![\w@+-]|[.:]\w)", text))


def allowed(p, cfg: dict, alias: str) -> str:
    """"" when the project may run checks on `alias`, else why not."""
    from . import lockprobe, machines
    known = machines.load()
    if alias not in known:
        return "not in the machines list (ttp machines add)"
    if machines.retired(known.get(alias)):
        return "retired in the machines list"
    if not _charter_names(p, alias) and alias not in account_hosts():
        return "not named in the charter"
    if (held := lockprobe.pause(p, cfg, alias)) is not None:
        return f"paused{': ' + str(held.get('reason')) if isinstance(held, dict) and held.get('reason') else ''}"
    if got := paused_elsewhere(p, alias, known.get(alias)):
        who, held = got
        return f"paused by {who}{': ' + str(held.get('reason')) if held.get('reason') else ''}"
    return ""


def paused_elsewhere(p, alias: str, machine: Any = None) -> tuple[str, dict] | None:
    """(who, pause) of a pause of `alias` or its machine's host name (`hostname`, short name too) that this project's own lookup
    (lockprobe.pause) does not see: a shared pause file of a name this project does not share, or
    another project's own pause, read-only from each registered project on this machine. None when none;
    an unreadable project is skipped, never a pause."""
    from . import globalcap, project, shared
    from .db import PAUSED_RESOURCES_KEY
    host = str(machine.get("hostname") or "") if isinstance(machine, dict) else ""
    names = [n for n in dict.fromkeys([alias, host, host.split(".")[0]]) if n]
    for n in names:
        try:
            pz = shared.read_pause(n)
        except Exception:
            pz = None
        if isinstance(pz, dict):
            return str(pz.get("project") or "a shared pause"), pz
    for name, e in sorted((project.load_registry().get("projects") or {}).items()):
        if name == p.name or not isinstance(e, dict) or not e.get("dir") \
                or e.get("host") not in (None, project.hostname()):
            continue
        db_path = Path(str(e["dir"])) / project.FOLDER / "state" / "project.db"
        try:
            if not db_path.is_file():
                continue
            conn = globalcap.connect_ro(db_path)
            try:
                r = conn.execute("SELECT value FROM kv WHERE key=?", (PAUSED_RESOURCES_KEY,)).fetchone()
            finally:
                conn.close()
            paused = json.loads(r[0]) if r and r[0] else {}
        except Exception:
            continue
        for n in names:
            if isinstance(paused, dict) and isinstance(pz := paused.get(n), dict):
                return name, pz
    return None


def probe(alias: str, beside_device: bool = False) -> tuple[int, str, str]:
    """(free slots, "", note) on `alias`, or (0, why it cannot take a check now, note): why names a device
    holder the probe's scan found (_device_holders); note says why that scan did not read, else "". With
    `beside_device` a holder found keeps no run off: it is the note instead (starting DEVICE_WHY)."""
    from . import checklane
    try:
        r = subprocess.run(remotecheck._ssh(alias, _probe_script()), stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return 0, f"ssh timed out after {PROBE_TIMEOUT_S} s", ""
    except OSError as e:
        return 0, f"cannot run ssh: {e}", ""
    if r.returncode == 255:
        return 0, f"ssh cannot reach it: {(r.stderr.strip().splitlines() or ['exit 255'])[-1][:200]}", ""
    if r.returncode == 3:
        return 0, "it lacks git or tar", ""
    m = re.search(r"^ttp-spill (\d+) (\d+) (\d+)(?: (\d+(?:\.\d+)?|-))?$", r.stdout, re.M)
    if r.returncode != 0 or not m:
        return 0, f"its probe failed (exit {r.returncode})", ""
    busy, note = _device_holders(r.stdout)
    if busy and not beside_device:
        return 0, busy, ""
    note = busy or note
    cores, mem_kib, running = (int(x) for x in m.groups()[:3])
    load = float(m[4]) if m[4] and m[4] != "-" else None
    n = checklane.host_slots(cores, mem_kib / (1024 * 1024) if mem_kib else None, load, running)
    if n > 0:
        return n, "", note
    return 0, (f"its {running} running check(s) fill it" if load is None
               else f"its idle cores and memory fit no check now (load {load:g} of {cores} cores, "
                    f"{running} check(s) running)"), note


def claim_dir() -> Path:
    from . import checkcap
    return checkcap.slot_dir() / "spill"


def lock_busy(p, cfg: dict, alias: str) -> str:
    """"" when `alias`'s machine lock is free for a shared taker (as `ttp lock --probe --shared` answers),
    else why not: "busy (held by ...; N waiting)"."""
    from . import lockprobe
    try:
        rc, said = lockprobe.answer(p, cfg, alias, shared_mode=True)
    except OSError as e:
        return f"its lock cannot be read: {e}"
    return "" if rc == 0 else str(said).split(": ", 1)[-1]


def _device_names(cfg: dict, alias: str, known: dict | None = None) -> list[str]:
    """`alias`'s device names: `<alias>-device`, the device names its machines-list `shared` entry gives, and
    the config's device.locks on it (the alias, `<alias>-...` or `<alias>:...`)."""
    from . import locks, machines
    known = machines.load() if known is None else known
    dev = locks.device_locks(cfg)
    names = [alias + DEVICE_SUFFIX]
    got = (known.get(alias) or {}).get("shared")
    if isinstance(got, list):
        names += [x for x in got if isinstance(x, str) and x and x != alias and (x.endswith(DEVICE_SUFFIX) or x in dev)]
    names += sorted(d for d in dev if d == alias or d.startswith((alias + "-", alias + ":")))
    return list(dict.fromkeys(names))


def device_locks_of(cfg: dict, alias: str, known: dict | None = None) -> list[str]:
    """The `ttp lock` resources that are `alias`'s device: each of its device names (_device_names) under its
    own name (another project holds it so in the shared locks) and as the lock this project's `ttp lock`
    takes for it (locks.canonical, device.locks' first name)."""
    from . import locks
    return list(dict.fromkeys(r for n in _device_names(cfg, alias, known) for r in (n, locks.canonical(cfg, n))))


def _lock_held(p, cfg: dict, res: str, shared_too: bool = True) -> str:
    """"" when nobody holds, reserves or waits for lock `res`, here (where this project's `ttp lock` takes
    it) or, with `shared_too`, in the user's shared locks (another project that shares it), else the probe's
    line. A lock held only by pool check runs (_spill_only) counts as free."""
    from . import lockprobe, locks, shared
    dirs = list(dict.fromkeys([shared.locks_dir(p, res, cfg), *([shared.root() / res] if shared_too else [])]))
    for d in dirs:
        paths = list(dict.fromkeys([*locks.slot_paths(d, res, shared.slots(p, res, cfg)),
                                    *sorted(Path(d).glob(f"{res}.*.lock"))]))
        if not (locks.probe(d, res, paths, shared=True) and locks.probe(d, res, paths)) \
                and not _spill_only(d, res, paths):
            return lockprobe.line(res, False, d, res, paths, p)
    return ""


def _spill_only(d, res: str, paths: list) -> bool:
    """Whether lock `res` in `d` is held only by pool check runs (_hold's label has SPILL_MARK), with nobody
    waiting for it or reserving it: such a hold is no device job. A taken slot whose label lacks the mark or
    does not read, or no holder found at all, is not."""
    from . import locks
    if locks.reserved_by(locks.reserve_path(d, res)) or locks.queued(d, res):
        return False
    try:
        held = [locks.read_label(x) for x in paths if locks._slot_labels([x])] + locks.shared_holders(paths)
    except (OSError, ValueError):
        return False
    return bool(held) and all(isinstance(h, dict) and all(h.get(k) == v for k, v in SPILL_MARK.items())
                              for h in held)


def _job_name(row: Any) -> str:
    if isinstance(row, dict):
        row = row.get("id") or row.get("job_id") or row.get("name") or row
    return str(row)[:80]


def running_job(data: Any) -> str:
    """The first running job a broker's status JSON reports, in a few words, else "". Job rows are a list
    under jobs, running_jobs, active_jobs or running (a `jobs` row counts only while its state or status
    says it runs; in the others being there is running), a count above 0 under the last three, or one job
    under current_job, active_job or running_job."""
    if not isinstance(data, dict):
        return ""
    for k in _JOB_LISTS:
        v = data.get(k)
        if k != "jobs" and isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return f"{v} running job(s) ({k})"
        for row in v if isinstance(v, list) else []:
            if k == "jobs" and not (isinstance(row, dict)
                                    and str(row.get("state") or row.get("status") or "").lower() in _RUNNING):
                continue
            return f"running job {_job_name(row)}"
    for k in _JOB_ONE:
        if (v := data.get(k)) and not isinstance(v, bool):
            return f"running job {_job_name(v)}"
    return ""


def broker_job(p, alias: str) -> str:
    """A running job a broker on `alias` reports (this project's heal checks with preset broker watching
    that machine), else "". A status that fails, times out (BROKER_TIMEOUT_S) or is no JSON never blocks."""
    from . import heal
    try:
        specs = [s for _, s in heal.checks(p.db) if s.get("preset") == "broker" and heal.machine_of(s) == alias]
    except Exception:
        return ""
    for s in specs:
        try:
            rc, out = heal._shell(s["status_command"], str(p.root),
                                  min(float(s.get("timeout_s") or BROKER_TIMEOUT_S), BROKER_TIMEOUT_S))
            if rc != 0:
                continue
            job = running_job(json.loads(out[out.find("{"):] if "{" in out else out))
        except Exception:
            continue
        if job:
            return f"its broker reports {job}"
    return ""


def device_busy(p, cfg: dict, alias: str) -> str:
    """"" when no device job runs on `alias` as far as this machine can tell, else why: a device lock of it
    held, reserved or waited for (device_locks_of), or a running job its broker reports (broker_job)."""
    from . import machines
    known = machines.load()
    names = _device_names(cfg, alias, known)
    for res in device_locks_of(cfg, alias, known):
        try:   # a canonical lock that is no device name of alias: only where this project's `ttp lock` takes it
            if said := _lock_held(p, cfg, res, shared_too=res in names):
                return f"a device job runs there: {said}"
        except OSError as e:
            return f"its device lock {res} cannot be read: {e}"
    return f"a device job runs there: {job}" if (job := broker_job(p, alias)) else ""


def skip_dir() -> Path:
    from . import checkcap
    return checkcap.slot_dir() / "spill-skips"


def _note_skip(alias: str, why: str, project_name: str, own_only: bool = False) -> None:
    """Record that `alias` took no new checks because of a device job (skip_lines shows it); "" clears it
    (with `own_only`, only a skip `project_name` recorded)."""
    f = skip_dir() / f"{alias}.json"
    try:
        if not why and own_only:
            try:
                if json.loads(f.read_text() or "{}").get("project") != project_name:
                    return
            except (ValueError, AttributeError):
                return
        if not why:
            f.unlink(missing_ok=True)
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        from . import project
        project.durable_write(f, json.dumps({"host": alias, "why": why[:300], "at": time.time(),
                                             "project": project_name}), sync=False)
    except OSError:
        pass


def skip_lines(now: float | None = None) -> list[str]:
    """`ttp status` and web app lines: each spill host the pool skipped for a device job in the last
    SKIP_SHOW_S, one line each, with the reason."""
    now = time.time() if now is None else now
    out = []
    for f in sorted(skip_dir().glob("*.json")) if skip_dir().is_dir() else []:
        try:
            r = json.loads(f.read_text() or "{}")
            at = float(r.get("at") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if r.get("host") and now - at <= SKIP_SHOW_S:
            out.append(f"spill host {r['host']} takes no new checks: {r.get('why') or 'a device job runs there'}"
                       f" (seen {max(0, int((now - at) // 60))} min ago)")
    return out


def _hold(p, cfg: dict, alias: str, what: str):
    """The run's hold of `alias`'s machine lock (shared, exclusive on a device lock), or None if taken."""
    from . import hold, locks, shared
    res = locks.canonical(cfg, alias)
    paths = locks.slot_paths(shared.locks_dir(p, res, cfg), res, shared.slots(p, res, cfg))
    who = shared.holder(p, res, hold.label(hold.who(os.environ.get("TTP_TASK")), os.environ.get("TTP_RUN_ID")), cfg)
    device = res in locks.device_locks(cfg) or res.endswith("-device") or alias.endswith("-device")
    try:
        return (locks.try_take if device else locks.try_take_shared)(paths, who, f"ttp checks (spilled): {what}",
                                                                     extra=SPILL_MARK)
    except OSError:
        return None


class Claim:
    """This machine's hold of one slot on spill host `host`, and the run's hold of its machine lock;
    `close()` frees both."""

    def __init__(self, host: str, f, lock=None):
        self.host = host
        self.f = f
        self.lock = lock

    def close(self) -> None:
        if self.lock is not None:
            self.lock.close()
            self.lock = None
        if self.f is not None:
            try:
                self.f.truncate(0)
            except OSError:
                pass
            self.f.close()
            self.f = None


def _claim(alias: str, free: int, rec: dict) -> Claim | None:
    d = claim_dir()
    d.mkdir(parents=True, exist_ok=True)
    for i in range(max(0, free) + _ours(alias)):
        f = open(d / f"{alias}.{i}.json", "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            continue
        f.seek(0)
        f.truncate(0)
        f.write(json.dumps(rec))
        f.flush()
        return Claim(alias, f)
    return None


def _ours(alias: str) -> int:
    """This machine's live claims on `alias`: they are among the host's running checks already."""
    return sum(1 for r in holders() if r.get("host") == alias)


def pick(p, cfg: dict, tell, what: str = "") -> Claim | None:
    """The first spill host that is allowed, reachable and has a free slot, claimed; None when none is.
    Each host skipped is told with why."""
    rec = {"project": p.name, "task": os.environ.get("TTP_TASK") or "", "run": os.environ.get("TTP_RUN_ID") or "",
           "pid": os.getpid(), "since": time.time(), "what": what[:300]}
    skipped = []
    for alias in hosts(cfg):
        beside = beside_device(cfg, alias)   # the project lets its checks run there beside a device job
        why, dev = allowed(p, cfg, alias) or lock_busy(p, cfg, alias), ""
        if not why and beside:
            dev = device_busy(p, cfg, alias)
            _note_skip(alias, "", p.name, own_only=True)   # this project's earlier skip no longer holds
        elif not why:
            why = device_busy(p, cfg, alias)
            _note_skip(alias, why, p.name)
        free, note = 0, ""
        if not why:
            free, why, note = probe(alias, beside)
            if why.startswith(DEVICE_WHY):
                _note_skip(alias, why, p.name)
            if note.startswith(DEVICE_WHY):
                dev, note = dev or note, ""
        dev = dev[len(DEVICE_WHY):] if dev.startswith(DEVICE_WHY) else dev
        if not why and (got := _claim(alias, free, {**rec, "host": alias,
                                                    **({"beside": dev[:300]} if dev else {})})) is not None:
            got.lock = _hold(p, cfg, alias, what)
            if got.lock is None:
                got.close()
                skipped.append(f"{alias} (busy: its lock was taken just now)")
                continue
            tell(f"spilling to {alias}: the slots here stayed busy for {after_s(cfg):g} s "
                 f"({_source(cfg, alias)}); the checks run there on a copy of the commit"
                 + (f"; {note}, so its device locks alone decided" if note else "")
                 + (f"; beside a device job (checks.{BESIDE_KEY} lists it): {dev}" if dev else "")
                 + (f"; skipped: {'; '.join(skipped)}" if skipped else ""))
            return got
        skipped.append(f"{alias} ({why or 'this machine holds its free slots'}"
                       + (f"; {note}" if note else "") + ")")
    if skipped:
        tell("no spill host could take the checks, waiting here: " + "; ".join(skipped))
    return None


def _alive(pid: Any) -> bool:
    """Not known dead: a claim whose pid this process cannot judge (liveness.UNKNOWN) stays live."""
    from .liveness import pid_state, DEAD
    return pid_state(pid) != DEAD


def holders(project_name: str | None = None) -> list[dict]:
    """The live claims on spill hosts (of `project_name`'s runs when given). Read only."""
    out = []
    for f in sorted(claim_dir().glob("*.json")) if claim_dir().is_dir() else []:
        try:
            r = json.loads(f.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get("host") and _alive(r.get("pid")) \
                and (project_name is None or r.get("project") == project_name):
            out.append(r)
    return out


def status_lines(project_name: str, now: float | None = None) -> list[str]:
    """`ttp status` lines: this project's checks running on a spill host."""
    from .liveness import pid_state, UNKNOWN, FOREIGN
    now = time.time() if now is None else now
    out = []
    for r in holders(project_name):
        who = f"task #{r['task']}" if r.get("task") else f"pid {r.get('pid')}"
        mins = max(0, int((now - float(r.get("since") or now)) // 60))
        out.append(f"checks of {who} running on spill host {r['host']} for {mins} min"
                   + (f": {r['what']}" if r.get("what") else "")
                   + (f" (beside a device job, checks.{BESIDE_KEY}: {r['beside']})" if r.get("beside") else "")
                   + (f" (claim's pid {r.get('pid')}: {FOREIGN})" if pid_state(r.get("pid")) == UNKNOWN else ""))
    return out
