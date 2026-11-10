# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Checks that spill to other machines when this one's check slots are busy: `delivery.checks_spill_hosts`.

A project may list, in order, the ssh aliases of other machines its checks may run on. The account's
pool, `checks.spill_hosts` in the account settings (`ttp config --account checks.spill_hosts '["pool-a"]'`,
which a run may only clear), follows that list in every project on this machine, so each spills
without a list of its own. No machines-list tag adds a host. A project's own empty list (`[]`)
keeps its checks here, the pool too; with neither, checks never leave this machine. A `ttp checks` run that may spill tries the listed hosts in
order before it takes a place here (`delivery.checks_spill_first`, default true: first), so this machine keeps
only the checks that cannot spill, and runs on the first that is allowed, reachable and has a free slot, as a
check with a check host does (remotecheck: a copy of exactly the commit, its output streamed back), with the
test workers that host's idle cores give it (workers: cores less the 1-minute load, never a fixed count). With
none, it queues here and, while it waits (its project's lane, a host-wide checks slot, the load guard,
test-worker slots), tries them again after each `delivery.checks_spill_after_s` (default SPILL_AFTER_S). With
checks_spill_first false it tries them only after that first wait. The run says where it went, checks.json records it (`hosts`, `spilled_to`), and `ttp status`
and `ttp checks status` show it while it runs (status_lines).

Allowed: listed in one of those structured settings (the project's own list or the account's pool),
never by charter wording or a machines-list tag, and in the user's machines list (not retired), and not
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
A project may also set its own pre-run hook, `checks.spill_pre_cmd` (a command; default none, so nothing
ships here: no neighbour, serving-state or site check): pick runs it here, read-only, before a run spills to a
host that passed the gates above, with the host as its last argument (pre_check). Exit 0: go; PRE_SKIP_RC (75):
skip the host for now; any other exit, a timeout or a hook that cannot run: an error, told, and the host is
skipped. It runs on hosts `checks.spill_beside_device` lists too, so there the project's hook decides about
device jobs. A machine with several hosts lists one of them as `alias:host` (target): the machine alias decides
whether it is allowed (machines list, charter or pool, pauses, its machine lock, which the run holds), the host
is what ssh reaches, what its own pause, lock and device checks name, and where the slots are claimed.
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
or real, so it stays here: `ttp checks` and the queue-wait item say so and name both settings, wrap_note), and
never a light run (it never waits). On a spill host a check never runs
behind this machine's checks.wrap_cmd: it runs behind its remote wrapper when it has one (push.spill_to),
a wrapper missing there being a host fault (remotecheck), else unwrapped. A run that does not pass on a spill host runs again
(OFF_ENV): a spill host's own environment never fails a change, only a pass there counts, unless the tests gate proved
its failures new there (they pass on the base on that host: gatejudge.known_new), which fails it. The rerun waits for a slot here
as any run does and may spill to another listed host, never back to one its checks failed on (spill_off_add): a host fault
there (exit 75: the host paused, its lock or a tool gone mid-run) leaves that host one more try, once pick finds it free.
With none left it runs here, whose result counts.

A check that runs an agent CLI (AGENT_CLIS, the claude CLI among them, in command position after its
wrappers: needs_cli), which a spill host neither has nor is logged in to, never spills unless it says
`"spill": true`. Nor does one that reads commits with git (needs_history: `git diff --check <base>`, `git log`),
which the host's one-commit copy lacks. Such a check, and one with `"host": "local"`, does not keep the rest of its run here: the
run splits (stays_here). Its other checks go to the spill host and it runs on this machine in the same run,
whose one verdict needs every part to pass; a failure here counts at once (it never reruns elsewhere). Only
a check that is no test suite or build (checklane.run_class), unwrapped and in a clean checkout splits off
this way; any other check that may not spill keeps the whole run here. So a spill host needs only what the
checks it gets run (git and tar, and python3 with the checks environment for pytest), never an agent CLI or
its sign-in.

A check whose command names a path of this machine (host_path: an absolute, `~` or `$HOME` path outside
the system folders, such as a project's own harness scripts) cannot run on a spill host or a Slurm node, which
get a copy of the commit only, so it never spills either; it splits off to run here the same way.
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
PRE_KEY = "spill_pre_cmd"   # checks.spill_pre_cmd: the project's own go/skip command before a spill to a host (pre_check)
PRE_TIMEOUT_S = 30
PRE_SKIP_RC = 75   # the pre-run hook's "not now": the host is skipped this time, no error
AFTER_KEY = "checks_spill_after_s"
FIRST_KEY = "checks_spill_first"   # delivery.checks_spill_first: a spillable run tries the spill hosts before a place here (first)
SPILL_AFTER_S = 60.0
OFF_ENV = "TTP_CHECKS_NO_SPILL"   # "1": these checks never spill; else the hosts a rerun keeps off (spill_off_add)
HOST_FAULT_TRIES = 2   # a spill host whose own fault (exit 75) ended a run this often is kept off its rerun
ENV_FAULT_HOSTS = 2   # the checks failed at import this often on spill hosts: one fault of the spill env config
ENV_RETRY_S = 900   # after which (or once the checks config changes) a run tries the spill hosts again
PROBE_TIMEOUT_S = 30
DEVICE_SUFFIX = "-device"
DEVICE_WHY = "a device job runs there: "   # how a device-busy reason starts (device_busy, _device_holders)
BROKER_TIMEOUT_S = 10.0   # one read of a broker's status; past it the device locks alone decide
SPILL_MARK = {"checks_spill": True}   # in the label of a pool check run's hold of a host's lock (_hold)
SKIP_SHOW_S = 900   # a host skipped for a device job shows in `ttp status` and the web app this long after
_JOB_LISTS = ("jobs", "running_jobs", "active_jobs", "running")   # broker status: job rows (a list) or a count
_JOB_ONE = ("current_job", "active_job", "running_job")   # broker status: the one job under way
_RUNNING = {"running", "active", "started", "in_progress", "in-progress", "busy"}   # a `jobs` row's state that runs
AGENT_CLIS = ("claude", "codex", "cursor-agent")   # agent CLIs a spill host lacks: a check running one stays here
_SHELLS = ("sh", "bash", "dash", "zsh")   # needs_cli reads their -c script too
# Absolute paths every host has: a check naming only these may still spill (host_path).
_SYSTEM_DIRS = ("/usr/", "/bin/", "/sbin/", "/lib/", "/lib64/", "/lib32/", "/etc/", "/dev/", "/proc/", "/sys/")
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
    if (f := (delivery or {}).get(FIRST_KEY)) is not None:
        try:
            first_of(f)
        except ValueError as e:
            out.append(str(e).replace("; nothing was changed", ""))
    return out


def account_hosts() -> list[str]:
    """The account's pool (checks.spill_hosts in the account settings) in order; [] when unset or malformed."""
    from . import project
    try:
        return hosts_of((project.load_account_settings().get("checks") or {}).get("spill_hosts") or [])
    except ValueError:
        return []


def hosts(cfg: dict | None) -> list[str]:
    """The project's spill hosts in order, then the account's pool;
    [] when the project's own list is empty (it keeps its checks here) or malformed (config_problems names it)."""
    own = ((cfg or {}).get("delivery") or {}).get(KEY)
    try:
        mine = hosts_of(own) if own is not None else []
    except ValueError:
        return []
    if own is not None and not mine:
        return []
    return list(dict.fromkeys([*mine, *account_hosts()]))


def _own(cfg: dict | None) -> list[str]:
    try:
        return hosts_of(((cfg or {}).get("delivery") or {}).get(KEY) or [])
    except ValueError:
        return []


def _source(cfg: dict | None, alias: str) -> str:
    """Which setting lists `alias`: the project's own list, else the account's pool."""
    if alias in _own(cfg):
        return f"delivery.{KEY}"
    return f"{ACCOUNT_KEY}, the account's pool"


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
    return got == "*" or alias in got or target(alias)[1] in got


def pre_cmd_of(v: Any) -> tuple[str, ...]:
    """A `checks.spill_pre_cmd` value as argv words; () when unset or empty. ValueError when it is not a
    string or does not parse."""
    from . import push
    return push._words(v, f"checks.{PRE_KEY}")


def pre_check(p, cfg: dict | None, alias: str) -> str:
    """"" when the project's own pre-run hook (checks.spill_pre_cmd; none set: always "") lets a run spill to
    `alias`, else why not. The hook runs here, in the project's folder, with the host as its last argument and
    TTP_SPILL_HOST, TTP_SPILL_MACHINE and TTP_PROJECT set; it must only read. Exit 0: go; PRE_SKIP_RC: not now;
    any other exit, a timeout (PRE_TIMEOUT_S) or a hook that cannot run is an error, and the host is skipped."""
    checks = (cfg or {}).get("checks")
    try:
        words = pre_cmd_of(checks.get(PRE_KEY) if isinstance(checks, dict) else None)
    except ValueError as e:
        return f"its pre-run hook error: {e}"
    if not words:
        return ""
    machine, host = target(alias)
    env = {**os.environ, "TTP_SPILL_HOST": host, "TTP_SPILL_MACHINE": machine, "TTP_PROJECT": str(p.name)}
    try:
        r = subprocess.run([*words, host], cwd=str(p.root), env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=PRE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"its pre-run hook error: checks.{PRE_KEY} timed out after {PRE_TIMEOUT_S} s"
    except OSError as e:
        return f"its pre-run hook error: checks.{PRE_KEY} cannot run: {e}"
    said = ((r.stdout or "").strip() or (r.stderr or "").strip()).splitlines()
    said = f": {said[-1][:200]}" if said else ""
    if r.returncode == 0:
        return ""
    if r.returncode == PRE_SKIP_RC:
        return f"its pre-run hook says not now (checks.{PRE_KEY}){said}"
    return f"its pre-run hook error: checks.{PRE_KEY} exited {r.returncode}{said}"


WRAP_NOTE = (f"checks behind checks.wrap_cmd never spill while neither checks.spill_wrap_cmd (their wrapper on a "
             f"spill host) is set nor checks.{NO_DEVICE_KEY} is false (the wrapper needs no device); set one to let "
             "them spill")


def wrap_note(cfg: dict | None) -> str:
    """WRAP_NOTE when the project's checks.wrap_cmd keeps the commands behind it here (no spill wrapper, and
    wrapped_ok not set), else ""."""
    from . import push
    try:
        if not push.wrap_of(cfg) or push.spill_wrap_of(None, cfg) or wrapped_ok(cfg):
            return ""
    except ValueError:
        return ""
    return WRAP_NOTE


def wrap_keeps_here(cmds: list, cfg: dict | None) -> str:
    """WRAP_NOTE when a check of `cmds` stays here only because it runs behind checks.wrap_cmd (check_spills
    with wrapped_ok would let it go), else ""."""
    checks = (cfg or {}).get("checks")
    ok = {**(cfg or {}), "checks": {**(checks if isinstance(checks, dict) else {}), NO_DEVICE_KEY: False}}
    return WRAP_NOTE if any(getattr(c, "wrap", ()) and not check_spills(c, cfg) and check_spills(c, ok)
                            for c in cmds) else ""


def wrap_problems(checks: Any) -> list[str]:
    """Config check of checks.wrap_needs_device, checks.spill_wrap_cmd, checks.spill_beside_device and
    checks.spill_pre_cmd."""
    from . import push
    v = checks.get(NO_DEVICE_KEY) if isinstance(checks, dict) else None
    out = [] if v is None or isinstance(v, bool) else [f"checks.{NO_DEVICE_KEY}: {v!r} is not true or false"]
    if (b := checks.get(BESIDE_KEY) if isinstance(checks, dict) else None) is not None:
        try:
            beside_of(b)
        except ValueError as e:
            out.append(str(e).replace("; nothing was changed", ""))
    try:
        pre_cmd_of(checks.get(PRE_KEY) if isinstance(checks, dict) else None)
    except ValueError as e:
        out.append(str(e))
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


def first_of(v: Any) -> bool:
    """A `delivery.checks_spill_first` value set by `ttp config`: true or false."""
    if isinstance(v, bool):
        return v
    t = str(v).strip().lower()
    if t in ("1", "true", "yes", "on"):
        return True
    if t in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"delivery.{FIRST_KEY}: {v!r} is not true or false; nothing was changed")


def first(cfg: dict | None) -> bool:
    """Whether a run that may spill tries the spill hosts before it takes a place here (delivery.checks_spill_first,
    default true): this machine keeps only the checks that cannot spill. False: it spills only after waiting
    after_s() here."""
    v = ((cfg or {}).get("delivery") or {}).get(FIRST_KEY)
    try:
        return True if v is None else first_of(v)
    except ValueError:
        return True


def workers(cores: int, load: float | None, others: int = 0, cap: int = 0) -> int:
    """The test workers a run spilled to a host of `cores` takes: the cores its 1-minute `load` leaves idle,
    less one run's cores (checklane.run_cores) for each of this machine's `others` claims there whose load
    may not show yet, at most `cap` (one run's limit, testslots.sizes) and its cores, at least 1; 0 when the
    load did not read (the run then sizes itself)."""
    from . import checklane
    if load is None or cores <= 0:
        return 0
    idle = int(cores - load) - others * checklane.run_cores(None, cores)
    return max(1, min(cap or cores, cores, idle))


def workers_env(n: int) -> dict:
    """The grant a spilled run's commands see for `n` workers (testslots: what xdist's `-n auto` and a held
    grant read); {} for 0."""
    from . import testslots
    return {testslots.SLOTS_ENV: str(n), testslots.HELD_ENV: str(n), testslots.XDIST_ENV: str(n)} if n > 0 else {}


_HISTORY_GIT = ("diff", "log", "rev-list", "merge-base", "show", "range-diff", "cherry", "describe", "shortlog",
                "whatchanged", "blame")


def needs_history(c: Any) -> bool:
    """Whether a simple command of check `c` runs a git command that reads commits (_HISTORY_GIT: `git diff
    --check <base>`, `git log`), after its wrappers or in a shell's -c script, which a spill host's copy (one commit,
    remotecheck) lacks; a line that does not parse counts."""
    from . import testslots
    try:
        segs = testslots._segments(str(c))
    except (ValueError, IndexError):
        return True
    for seg in segs:
        words = [w for _, _, w in seg]
        i = 0
        while i < len(words) and (re.match(r"[A-Za-z_]\w*=|-|\d", words[i])
                                  or testslots._base(words[i]) in testslots._WRAPPERS):
            i += 1
        if i < len(words) and testslots._base(words[i]) in _SHELLS:   # a shell's -c script, as needs_cli
            opt = next((j for j in range(i + 1, len(words)) if re.fullmatch(r"-[a-z]*c[a-z]*", words[j])), None)
            if opt is not None and opt + 1 < len(words) and needs_history(words[opt + 1]):
                return True
        if i >= len(words) or testslots._base(words[i]) != "git":
            continue
        i += 1
        while i < len(words) and words[i].startswith("-"):   # git's own options; -C and -c take a value
            i += 2 if words[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
        if i < len(words) and words[i] in _HISTORY_GIT:
            return True
    return False


def check_spills(c: Any, cfg: dict | None = None) -> bool:
    """Whether check `c` may run on a spill host: it would run on this machine (its host "local", or an agent
    CLI it runs (needs_cli), only with `"spill": true`), in a clean checkout, does not say `"spill": false`, and is unwrapped unless it
    has a remote wrapper (push.spill_wrap_of; one that does not parse keeps it here) or `cfg` says its
    wrappers need no device (wrapped_ok)."""
    from . import cleantree, push
    opt = getattr(c, "spill", None)
    if opt is False or getattr(c, "keep_here", False) or push.remote_host(c) or cleantree.authoring(c):
        return False
    if (getattr(c, "host", "") == remotecheck.LOCAL or needs_cli(c) or needs_history(c)) and opt is not True:
        return False
    if host_path(c):   # `"spill": true` cannot make a path of this machine exist there
        return False
    try:
        remote_wrap = push.spill_wrap_of(c, cfg)
    except ValueError:
        return False
    return bool(remote_wrap) or wrapped_ok(cfg) or not getattr(c, "wrap", ())


def needs_cli(c: Any) -> bool:
    """Whether a simple command of check `c` runs an agent CLI (AGENT_CLIS) after its wrappers, or in a
    shell's -c script; a line that does not parse counts (it stays here). A script file that runs one is
    not seen: such a check says `"host": "local"`."""
    from . import testslots
    try:
        segs = testslots._segments(str(c))
    except (ValueError, IndexError):
        return True
    for seg in segs:
        words = [w for _, _, w in seg]
        i = 0
        while i < len(words) and (re.match(r"[A-Za-z_]\w*=|-|\d", words[i])
                                  or testslots._base(words[i]) in testslots._WRAPPERS):
            i += 1
        if i < len(words) and testslots._base(words[i]) in AGENT_CLIS:
            return True
        # A shell's -c script (`sh -c "claude ..."`, `bash -lc ...`) is read the same way.
        if i < len(words) and testslots._base(words[i]) in _SHELLS:
            opt = next((j for j in range(i + 1, len(words)) if re.fullmatch(r"-[a-z]*c[a-z]*", words[j])), None)
            if opt is not None and opt + 1 < len(words) and needs_cli(words[opt + 1]):
                return True
    return False


def _host_word(w: str) -> bool:
    """Whether word `w` (or the value of a `VAR=` / `--opt=` word) is a path of this machine: absolute,
    `~` or `$HOME`, outside _SYSTEM_DIRS."""
    for v in (w, w.split("=", 1)[1] if "=" in w else ""):
        if v.startswith(("~", "$HOME", "${HOME}")):
            return True
        if v.startswith("/") and not v.startswith("//") and not (v + "/").startswith(_SYSTEM_DIRS) \
                and v not in ("/", "/dev/null"):
            return True
    return False


def host_path(c: Any) -> bool:
    """Whether check `c` names a path of this machine (_host_word) in any simple command or a shell's -c
    script, which a spill host or a Slurm node, given a copy of the commit only, lacks; a line that does not
    parse counts (it stays here). A path inside the checkout counts too: the copy there sits elsewhere."""
    from . import testslots
    try:
        segs = testslots._segments(str(c))
    except (ValueError, IndexError):
        return True
    for seg in segs:
        words = [w for _, _, w in seg]
        for j, w in enumerate(words):
            if re.match(r"\d*>", w):   # an output redirection writes there: any host can
                continue
            if _host_word(w[1:] if w.startswith("<") else w):
                return True
            if testslots._base(w) in _SHELLS:   # its -c script (`bash -lc '...'`) is read the same way
                opt = next((k for k in range(j + 1, len(words)) if re.fullmatch(r"-[a-z]*c[a-z]*", words[k])), None)
                if opt is not None and opt + 1 < len(words) and host_path(words[opt + 1]):
                    return True
    return False


def stays_here(c: Any, cfg: dict | None = None) -> bool:
    """Whether check `c` runs on this machine while the rest of its run spills: it may not spill only because
    it says `"host": "local"`, runs an agent CLI (needs_cli), reads commits with git (needs_history) or names a path of
    this machine (host_path), and it is cheap enough to run beside the
    spilled part without a checks slot here (no test suite or build: checklane.run_class), unwrapped and in a
    clean checkout."""
    from . import checklane, cleantree, push
    if getattr(c, "spill", None) is not None or getattr(c, "keep_here", False) or push.remote_host(c) \
            or cleantree.authoring(c) or getattr(c, "wrap", ()):
        return False
    if getattr(c, "host", "") != remotecheck.LOCAL and not needs_cli(c) and not needs_history(c) \
            and not host_path(c):
        return False
    return checklane.run_class([c], cfg) != checklane.SUITE


def spillable(cmds: list, cfg: dict | None = None) -> bool:
    """Whether a run of `cmds` may spill: one check at least may (check_spills) and each other one runs
    here beside it (stays_here)."""
    spills = [check_spills(c, cfg) for c in cmds]
    return any(spills) and all(ok or stays_here(c, cfg) for ok, c in zip(spills, cmds))


def target(spec: str, known: dict | None = None) -> tuple[str, str]:
    """(machine alias, ssh host) of spill host `spec`: an alias of the machines list is both; `alias:host`
    whose alias is in the list (and `spec` itself is not) is one host of that machine, which has several."""
    from . import machines
    known = machines.load() if known is None else known
    m, sep, h = spec.partition(":")
    if spec not in known and sep and h and m in known:
        return m, h
    return spec, spec


def allowed(p, cfg: dict, alias: str) -> str:
    """"" when the project may run checks on spill host `alias`, else why not: its machine (target) must be
    allowed, and neither the machine nor its host paused. Only structured settings list a host (the
    project's own list, the account's pool), never charter wording or a machines-list tag; the user's
    pauses and holds still keep a listed host off."""
    from . import lockprobe, machines
    known = machines.load()
    machine, host = target(alias, known)
    if machine not in known:
        return "not in the machines list (ttp machines add)"
    if machines.retired(known.get(machine)):
        return "retired in the machines list"
    if alias not in _own(cfg) and alias not in account_hosts() and machine not in account_hosts():
        name = getattr(p, "name", "") or "<project>"
        return (f"not a spill host of this project (add it with `ttp config {name} delivery.{KEY} "
                f"'{json.dumps([*_own(cfg), alias])}'`)")
    for n in dict.fromkeys([machine, host]):
        if (held := lockprobe.pause(p, cfg, n)) is not None:
            return f"paused{': ' + str(held.get('reason')) if isinstance(held, dict) and held.get('reason') else ''}"
        if got := paused_elsewhere(p, n, known.get(n)):
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
        from . import sshagent
        if sshagent.locked(r.stderr or ""):
            return 0, f"ssh cannot reach it: {sshagent.WHY}", ""
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
    _READ[alias] = (cores, load)
    n = checklane.host_slots(cores, mem_kib / (1024 * 1024) if mem_kib else None, load, running)
    if n > 0:
        return n, "", note
    return 0, (f"its {running} running check(s) fill it" if load is None
               else f"its idle cores and memory fit no check now (load {load:g} of {cores} cores, "
                    f"{running} check(s) running)"), note


_READ: dict[str, tuple[int, float | None]] = {}   # the last probe's (cores, load) per host: pick sizes a run's workers from it


def claim_dir() -> Path:
    from . import checkcap
    return checkcap.slot_dir() / "spill"


def lock_busy(p, cfg: dict, alias: str) -> str:
    """"" when the machine lock of spill host `alias` (and its host's, for one host of a machine: target) is
    free for a shared taker (as `ttp lock --probe --shared` answers), else why not: "busy (held by ...; N waiting)"."""
    from . import lockprobe
    for res in dict.fromkeys(target(alias)):
        try:
            rc, said = lockprobe.answer(p, cfg, res, shared_mode=True)
        except OSError as e:
            return f"its lock cannot be read: {e}"
        if rc != 0:
            return str(said).split(": ", 1)[-1]
    return ""


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
    """Record that `alias` took no new checks because of a device job or the project's pre-run hook (skip_lines
    shows it); "" clears it
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
    """`ttp status` and web app lines: each spill host the pool skipped for a device job or a pre-run hook in the last
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
        self.workers = 0   # the test workers the run takes there (workers); 0: it sizes itself

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


def all_off(off: str) -> bool:
    """Whether OFF_ENV's value `off` keeps the checks off every spill host ("1"; "" or a host list: no)."""
    return off == "1"


def spill_off_add(off: str, host: str, fault: bool, env_cfg: dict | None = None) -> str:
    """OFF_ENV's value after the checks did not pass on spill host `host`: `fault` when the host's own fault
    ended them (exit 75), else they failed or timed out there. `env_cfg`: they failed at import for want of a
    package (push.env_problem), a fault of the spill env config `env_cfg` (env_off), never of the host."""
    mark = (f"env.{_env_key(env_cfg)}.{int(time.time())}" if env_cfg is not None
            else "fault" if fault else "fail")
    return ",".join([*(e for e in off.split(",") if e and e != "1"), f"{host}:{mark}"])


def kept_off(off: str) -> set[str]:
    """The spill hosts OFF_ENV's value `off` keeps a rerun off: each its checks failed on, and each whose own
    fault ended them HOST_FAULT_TRIES times. An import failure keeps no host off (env_off)."""
    seen = [e.rpartition(":") for e in off.split(",") if e and e != "1"]
    return {h for h, _, why in seen if not why.startswith("env.") and (why != "fault"
            or sum(1 for h2, _, w2 in seen if h2 == h and w2 == "fault") >= HOST_FAULT_TRIES)}


def _env_key(cfg: dict | None) -> str:
    import hashlib
    checks = (cfg or {}).get("checks")
    return hashlib.sha1(json.dumps(checks if isinstance(checks, dict) else {}, sort_keys=True,
                                   default=str).encode()).hexdigest()[:10]


def env_off(off: str, cfg: dict, now: float | None = None) -> str:
    """Why OFF_ENV's value `off` keeps the checks off every spill host: they failed at import on
    ENV_FAULT_HOSTS times under the checks config `cfg` still has, in the last ENV_RETRY_S. One fault of
    the spill env config, told once; "" once that config changes or the time passes, when all are tried again."""
    now = time.time() if now is None else now
    key, hit = _env_key(cfg), []
    for h, _, why in (e.rpartition(":") for e in off.split(",") if e and e != "1"):
        _, k, at = (why.split(".") + ["", ""])[:3] if why.startswith("env.") else ("", "", "")
        if k == key and at.isdigit() and now - int(at) <= ENV_RETRY_S:
            hit.append(h)
    if len(hit) < ENV_FAULT_HOSTS:
        return ""
    return (f"the checks failed at import on spill hosts {', '.join(dict.fromkeys(hit))}: one fault of the spill hosts' checks "
            f"env (checks.spill_wrap_cmd or the pinned checks env), not of a host or the change; every spill "
            f"host is tried again once the checks config changes or in {ENV_RETRY_S // 60} min")


def env_event(db, off: str, cfg: dict) -> bool:
    """Raise one coordinator event when OFF_ENV's value `off` shows the spill env config fault (env_off): a
    checks config change that breaks every spilled run within minutes is told at once, once per config."""
    if not (why := env_off(off, cfg)):
        return False
    import sqlite3
    fp = f"checkspill:env:{_env_key(cfg)}"
    try:
        if db.q("SELECT 1 FROM events WHERE fingerprint=? LIMIT 1", (fp,)):
            return False
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (time.time(), "daemon", "observation", fp, "high",
              f"Spilled checks are failing at import: {why}. Check what last changed the checks config "
              f"(checks.spill_wrap_cmd, the pinned checks env) and restore a working value.", "queued"))
    except (OSError, sqlite3.Error):   # an event is a courtesy: never a reason for the checks run to fail
        return False
    return True


def pick(p, cfg: dict, tell, what: str = "", off: str = "", why_here: str = "") -> Claim | None:
    """The first spill host that is allowed, reachable and has a free slot, and that the project's pre-run hook
    (pre_check) lets the run go to, claimed; None when none is. One host of a machine with several (target) is
    probed, claimed and held by its own name, its machine's lock held too. Each host skipped is told with why.
    `off`: OFF_ENV's value, whose hosts (kept_off) are skipped. `why_here`: why the run tries them now (default:
    the slots here stayed busy). The claim's `workers` are sized to the host's idle cores at its probe (workers)."""
    rec = {"project": p.name, "task": os.environ.get("TTP_TASK") or "", "run": os.environ.get("TTP_RUN_ID") or "",
           "pid": os.getpid(), "since": time.time(), "what": what[:300]}
    skipped, keep = [], kept_off(off)
    if env := env_off(off, cfg):
        tell(f"no spill host could take the checks, waiting here: {env}")
        _note_pick(p.name, "", [f"{alias} ({env})" for alias in hosts(cfg)])
        return None
    mine = [r.get("host") for r in holders()]
    tried = [e.rpartition(":")[0] for e in off.split(",")]
    # Spread: hosts these checks did not fail on yet, then those running fewer of this machine's checks, first.
    for alias in sorted(hosts(cfg), key=lambda h: (h in tried, mine.count(h))):
        machine, host = target(alias)
        if alias in keep or host in keep:
            skipped.append(f"{alias} (these checks did not pass there in this run)")
            continue
        beside = beside_device(cfg, alias)   # the project lets its checks run there beside a device job
        why, dev = allowed(p, cfg, alias) or lock_busy(p, cfg, alias), ""
        if not why and beside:
            dev = device_busy(p, cfg, host)
            _note_skip(host, "", p.name, own_only=True)   # this project's earlier skip no longer holds
        elif not why:
            why = device_busy(p, cfg, host)
            _note_skip(host, why, p.name)
        if not why and (why := pre_check(p, cfg, alias)):
            _note_skip(host, why, p.name)
        free, note = 0, ""
        if not why:
            free, why, note = probe(host, beside)
            if why.startswith(DEVICE_WHY):
                _note_skip(host, why, p.name)
            if note.startswith(DEVICE_WHY):
                dev, note = dev or note, ""
        dev = dev[len(DEVICE_WHY):] if dev.startswith(DEVICE_WHY) else dev
        if not why and (got := _claim(host, free, {**rec, "host": host,
                                                   **({"beside": dev[:300]} if dev else {})})) is not None:
            got.lock = _hold(p, cfg, machine, what)
            if got.lock is None:
                got.close()
                skipped.append(f"{alias} (busy: its lock was taken just now)")
                continue
            cores, load = _READ.pop(host, (0, None))
            from . import testslots
            top = testslots.sizes(cfg, cores) if cores > 0 else None   # one run's limit, sized to that host
            got.workers = workers(cores, load, max(0, _ours(host) - 1), top[1] if top else 0)
            note_here(p.name, "")   # this run is on a spill host now: no reason to show it here
            tell(f"spilling to {alias}: "
                 + (why_here or f"the slots here stayed busy for {after_s(cfg):g} s")
                 + f" ({_source(cfg, alias)}); the checks run there on a copy of the commit"
                 + (f" with {got.workers} test worker(s), its idle cores (load {load:g} of {cores})"
                    if got.workers else "")
                 + (f"; {note}, so its device locks alone decided" if note else "")
                 + (f"; beside a device job (checks.{BESIDE_KEY} lists it): {dev}" if dev else "")
                 + (f"; skipped: {'; '.join(skipped)}" if skipped else ""))
            _note_pick(p.name, alias, skipped)
            return got
        skipped.append(f"{alias} ({why or 'this machine holds its free slots'}"
                       + (f"; {note}" if note else "") + ")")
    if skipped:
        tell("no spill host could take the checks, waiting here: " + "; ".join(skipped))
        note_here(p.name, "no spill host could take them: " + "; ".join(skipped), what)
    _note_pick(p.name, "", skipped)
    return None


def _pick_file(project_name: str) -> Path:
    return claim_dir().parent / "spill-picks" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', project_name)}.json"


def _note_pick(project_name: str, took: str, skipped: list[str]) -> None:
    """Record the project's last pick: the host it took ("" none) and each host skipped with why (pick_lines)."""
    f = _pick_file(project_name)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        from . import project
        project.durable_write(f, json.dumps({"at": time.time(), "took": took,
                                             "skipped": [w[:300] for w in skipped]}), sync=False)
    except OSError:
        pass


def pick_lines(project_name: str, now: float | None = None) -> list[str]:
    """`ttp checks status` lines: the project's last spill pick in the last SKIP_SHOW_S, and each host it
    skipped with why."""
    now = time.time() if now is None else now
    try:
        r = json.loads(_pick_file(project_name).read_text() or "{}")
        at = float(r.get("at") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return []
    if now - at > SKIP_SHOW_S:
        return []
    ago = f"{max(0, int((now - at) // 60))} min ago"
    return ([f"last spill pick ({ago}) took {r['took']}" if r.get("took")
             else f"last spill pick ({ago}) found no spill host with room"]
            + [f"  spill host {w.partition(' (')[0]} took no run: {w.partition(' (')[2].removesuffix(')')}"
               for w in r.get("skipped") or []])


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


def here_dir() -> Path:
    from . import checkcap
    return checkcap.slot_dir() / "spill-here"


def note_here(project_name: str, why: str, what: str = "") -> None:
    """Record why this process's checks run on the project host, not a spill host (here_lines shows it while
    the process lives); "" clears it. Records of dead processes go at each write. Never raises."""
    d = here_dir()
    try:
        for g in d.glob("*.json") if d.is_dir() else []:
            if g.stem != str(os.getpid()) and g.stem.isdigit() and not _alive(int(g.stem)):
                g.unlink(missing_ok=True)
        f = d / f"{os.getpid()}.json"
        if not why:
            f.unlink(missing_ok=True)
            return
        d.mkdir(parents=True, exist_ok=True)
        from . import project
        project.durable_write(f, json.dumps({"project": project_name, "task": os.environ.get("TTP_TASK") or "",
                                             "pid": os.getpid(), "why": why[:300], "what": what[:200],
                                             "at": time.time()}), sync=False)
    except OSError:
        pass


def here_records(project_name: str) -> list[dict]:
    """The live note_here records of `project_name`'s runs. Read only."""
    out = []
    for f in sorted(here_dir().glob("*.json")) if here_dir().is_dir() else []:
        try:
            r = json.loads(f.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get("project") == project_name and r.get("why") and _alive(r.get("pid")):
            out.append(r)
    return out


def here_lines(project_name: str) -> list[str]:
    """`ttp checks status` lines: each of this project's runs that stays on the project host, and why."""
    out = []
    for r in here_records(project_name):
        who = f"task #{r['task']}" if r.get("task") else f"pid {r.get('pid')}"
        out.append(f"checks of {who} run on the project host: {r['why']}" + (f" ({r['what']})" if r.get("what") else ""))
    return out


def spill_run(p, cfg: dict, cmds: list, tell, what: str = "", off: str = "", why_here: str = "") -> tuple[Any, list]:
    """For a run that takes no `ttp checks` slot here (the push batch's suite, an after-landing suite):
    (claim, `cmds`) with each check that may spill sent to the spill host checkspill.pick claimed, sized to its
    worker grant (cli._spill_to, as `ttp checks` spills), the rest here; (None, `cmds`) when it stays here,
    with why recorded (note_here). Tried only with spill hosts set and spill first on (first); `off` as pick's.
    The caller closes the claim once the run ends."""
    if not hosts(cfg):
        return None, cmds
    why = ("spill first is off (delivery." + FIRST_KEY + ")" if not first(cfg) else
           "kept off every spill host" if all_off(off) else
           "" if spillable(cmds, cfg) else "its checks cannot spill (an agent CLI, \"host\": \"local\" or a wrapper)")
    if why:
        note_here(p.name, why, what)
        return None, cmds
    claim = pick(p, cfg, tell, what, off, why_here or f"spill first (delivery.{FIRST_KEY}): the project host "
                                                       "keeps only checks that cannot spill")
    if claim is None:
        return None, cmds
    from .cli import _spill_to
    return claim, [_unspilled(c, was) for c, was in zip(_spill_to(cmds, claim.host, cfg, claim.workers), cmds)]


def _unspilled(c: Any, was: Any) -> Any:
    """`c` (spilled from `was`) remembering `was`, so a spill host's fault reruns the check elsewhere."""
    if getattr(c, "spilled", False):
        c.was = was
    return c
