# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Checks that spill to other machines when this one's check slots are busy: `delivery.checks_spill_hosts`.

A project may list, in order, the ssh aliases of other machines its checks may run on. The account's
pool, `checks.spill_hosts` in the account settings (`ttp config --account checks.spill_hosts '["pool-a"]'`,
which a run may only clear), follows that list in every project on this machine, so each spills
without a list of its own. No machines-list tag adds a host. The account's ban list, `checks.no_spill_hosts`
(`ttp config --account checks.no_spill_hosts '["host-x"]'`, ssh aliases or host names, which a run may only add
to), keeps a host out of every project's spill on this machine: an alias, its machine or ssh host, or the
machines list's `hostname` for it (full or short name) matches (banned), a project or pool list naming one is
refused at config time, and `ttp checks status` shows the list. `delivery.checks_spill_exclude` (ssh aliases or
host names, default none) keeps named hosts out of this project's routes, the pool and checks.spill_beside_device
included: an alias or the machines list's `hostname` for it matches (excluded), and the pick says it skipped them.
A project's own empty list (`[]`)
keeps its checks here, the pool too; with neither, checks never leave this machine. A `ttp checks` run that may spill tries the listed hosts in
order before it takes a place here (`delivery.checks_spill_first`, default true: first), so this machine keeps
only the checks that cannot spill, and runs on the first that is allowed, reachable and has a free slot, as a
check with a check host does (remotecheck: a copy of exactly the commit, its output streamed back), with the
test workers that host's idle cores give it (workers: cores less the 1-minute load, never a fixed count). With
none, it queues here and, while it waits (its project's lane, a host-wide checks slot, the load guard,
test-worker slots), tries them again after each `delivery.checks_spill_after_s` (default SPILL_AFTER_S). With
checks_spill_first false it tries them only after that first wait. On a workstation (a laptop or desktop: netwait.workstation;
`delivery.checks_remote_only`, default auto, true or false overrides it) a heavy run whose spill hosts are all busy
waits for one, holding nothing here, rather than run here (remote_only): it falls back here only when none is busy,
every one out of reach or unusable, and then runs niced in this machine's few test slots. Light checks run here. The run says where it went, checks.json records it (`hosts`, `spilled_to`), and `ttp status`
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
cores hold less the remote checks running there from any machine (their ttp-check.* folders a live process
works in once its run started, whose run script lives, or just made: _PROBE; a stalled copy's or a cut run's
leftover counts for none and is removed), not a
small fixed count; a host whose load does not read gets checklane.default_slots less those. This machine also holds one claim file
per slot it uses there (spill/<alias>.<n>.json in the checks slot folder), so its own runs never
take more. A claim whose remote ttp-check.* folder is not made yet (the few seconds of its copy) is
counted by neither, so the host can take one run more than its slots in that gap: a bound, not a guard.
A run's folder that no process uses and whose run script is gone (its host or link died before it could
clean up) does not count, and is removed once stale (_PROBE).
An entry of either list may be a table instead of an alias, `{"host": "<alias>", "max_runs": 1, "jobs": 16}`
(limits; both optional): max_runs caps this machine's checks running there at once (its claim files, which every
project here shares) below what its idle cores allow, and jobs gives each run there that many test workers
(the xdist -n its grant sets) instead of sizing them from the host's idle cores; a smaller box, or one whose
tests time out under load, gets a lower jobs.
A host whose pinned checks env cannot be made (remotecheck's NO_ENV, as with no package index there) is
down for every project here (down_add): pick skips it, and `ttp status` says why (skip_lines), until a
run there passes its env step (down_clear) or, after DOWN_RETRY_S, the one run that tries it again.

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
there (exit 75: the host paused, its lock or a tool gone mid-run) leaves that host one more try, once pick finds it free,
and a host its own fault kept off is tried again once the checks config changes (a new checks.spill_wrap_cmd) or
after FAULT_RETRY_S. Each bound of its wait here tries them all again, so a waiting run moves to the first that can take it.
With none left it runs here, whose result counts. Reruns are bounded (FAILS_FILE, kept across runs): an unchanged
head (same commit and check commands) never runs again on a spill host its checks failed on; a failure at import
(push.env_problem) reruns once per head in all, then the result counts; and one where tests also failed for a reason
of their own (push.assertion_failures) is the change's failure at once, its missing package only a note.

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
import hashlib
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
EXCLUDE_KEY = "checks_spill_exclude"   # delivery.checks_spill_exclude: hosts the project keeps out of every route (excluded)
ACCOUNT_KEY = "checks.spill_hosts"   # the account's pool (project.ACCOUNT_KEYS), after a project's own list
NO_SPILL_KEY = "checks.no_spill_hosts"   # the account's ban list: hosts no project's checks spill to (banned)
BESIDE_KEY = "spill_beside_device"   # checks.spill_beside_device: hosts a device job keeps no spill off (beside_device)
NO_DEVICE_KEY = "wrap_needs_device"   # checks.wrap_needs_device false: a wrapped run needs no device and may spill
PRE_KEY = "spill_pre_cmd"   # checks.spill_pre_cmd: the project's own go/skip command before a spill to a host (pre_check)
PRE_TIMEOUT_S = 30
PRE_SKIP_RC = 75   # the pre-run hook's "not now": the host is skipped this time, no error
AFTER_KEY = "checks_spill_after_s"
FIRST_KEY = "checks_spill_first"   # delivery.checks_spill_first: a spillable run tries the spill hosts before a place here (first)
REMOTE_ONLY_KEY = "checks_remote_only"   # delivery.checks_remote_only: on a workstation a busy pool is waited for (remote_only)
REMOTE_ONLY_ENV = "TTP_TEST_REMOTE_ONLY"   # tests: "1"/"0" instead of this machine's kind when the setting is unset
SPILL_AFTER_S = 60.0
OFF_ENV = "TTP_CHECKS_NO_SPILL"   # "1": these checks never spill; else the hosts a rerun keeps off (spill_off_add)
HOST_FAULT_TRIES = 2   # a spill host whose own fault (exit 75) ended a run this often is kept off its rerun
FAULT_RETRY_S = 600   # for this long, or until the checks config changes; then it is tried again
FAIL_VERDICT_HOSTS = 2   # on a workstation (remote_only), checks that failed on this many spill hosts failed: no rerun
HANG_VERDICT_HOSTS = 2   # one check command stopped by its silence limit on this many spill hosts hangs: no rerun
HANG = "hang."   # spill_off_add's mark of a host where a check command hung (hang_mark): kept off, as a failure
SLOW = "slow"   # spill_off_add's mark of a host whose tree copy timed out (remotecheck.COPY_TIMEOUT): kept off at once
BUSY = "busy"   # spill_off_add's mark of a host whose spill wrapper declined the run (a busy host): kept off at once
WRAP_ENV = "TTP_CHECKS_WRAP_REFUSED"   # this run's spill hosts whose wrapper refused it, each with why (wrap_refused)
WRAP_SAME_HOSTS = 3   # this many spill hosts (or every one listed) refusing a run for one reason: the wrapper's setting
ENV_FAULT_HOSTS = 2   # the checks failed at import this often on spill hosts: one fault of the spill env config
ENV_RETRY_S = 900   # after which (or once the checks config changes) a run tries the spill hosts again
FAILS_FILE = "checks-spill-fails.json"   # in the project state: where each head's checks failed on a spill host (fails)
FAILS_KEEP_S = 2 * 86400
PROBE_TIMEOUT_S = 30
LOAD_NEAR = 0.8   # no check starts on a spill host whose 1-minute load (with this machine's runs not yet in it) is this x its cores
LOAD_LAG_S = 90.0   # a claim this recent may not show in its host's 1-minute load yet: its cores count as load
BACKOFF_S, BACKOFF_MAX_S = 60.0, 900.0   # an unreachable host is not probed again for this long, doubling per failure
DEVICE_SUFFIX = "-device"
DEVICE_WHY = "a device job runs there: "   # how a device-busy reason starts (device_busy, _device_holders)
BROKER_TIMEOUT_S = 10.0   # one read of a broker's status; past it the device locks alone decide
SPILL_MARK = {"checks_spill": True}   # in the label of a pool check run's hold of a host's lock (_hold)
BUSY_COOL_S = 300   # a host whose spill wrapper declined a run as busy (busy_add) is skipped this long
DOWN_RETRY_S = 1800   # a host marked down (down_add) takes one run again after this, which marks it again or clears it
SKIP_SHOW_S = 900   # a host skipped for a device job shows in `ttp status` and the web app this long after
_JOB_LISTS = ("jobs", "running_jobs", "active_jobs", "running")   # broker status: job rows (a list) or a count
_JOB_ONE = ("current_job", "active_job", "running_job")   # broker status: the one job under way
_RUNNING = {"running", "active", "started", "in_progress", "in-progress", "busy"}   # a `jobs` row's state that runs
AGENT_CLIS = ("claude", "codex", "cursor-agent")   # agent CLIs a spill host lacks: a check running one stays here
_SHELLS = ("sh", "bash", "dash", "zsh")   # needs_cli reads their -c script too
# Absolute paths every host has: a check naming only these may still spill (host_path).
_SYSTEM_DIRS = ("/usr/", "/bin/", "/sbin/", "/lib/", "/lib64/", "/lib32/", "/etc/", "/dev/", "/proc/", "/sys/")
RUNNING_MAX_AGE_MIN = 720   # a remote ttp-check.* folder older than this is a leftover, not a running check
FRESH_MIN = 5   # a folder of the ssh user this new is counted even with no process in it: a run's copy is under way
REAP_AFTER_MIN = 120   # a folder of the ssh user with no process in it, untouched this long, is removed
# The host's slots line: cores, available memory (KiB), running checks and the 1-minute load. A running check
# is a ttp-check.* folder of the temp folder some process of the ssh user works in (its /proc/<pid>/cwd,
# read with GNU find: a run's shell sits in its folder until the run ends) and whose run started (RUN_PID is there:
# a copy that stalled with a process in its folder is no running check), or one of the ssh user's made
# under FRESH_MIN ago, or one whose run script is still alive (its pid in remotecheck.RUN_PID; a run whose
# processes all work outside its folder): those are never removed. One of the ssh user's with none of these
# and older than REAP_AFTER_MIN is a run that died with its host or its link (no EXIT trap ran) and is
# removed. Another user's folder, whose processes cannot be read, and every folder on a host without /proc
# or GNU find, count while under RUNNING_MAX_AGE_MIN old.
_PROBE = ("command -v git >/dev/null 2>&1 && command -v tar >/dev/null 2>&1 || exit 3\n"
          "c=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)\n"
          "m=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null)\n"
          "t=${TMPDIR:-/tmp}; t=${t%/}; r=$(cd \"$t\" 2>/dev/null && pwd -P)\n"
          "if [ -n \"$r\" ] && find /proc/self/cwd -maxdepth 0 -lname '/*' -printf '' >/dev/null 2>&1; then\n"
          "  u=$(id -u); n=0\n"
          "  live=\" $(find /proc/[0-9]*/cwd -maxdepth 0 -lname \"$r/ttp-check.*\" -printf '%l\\n' 2>/dev/null"
          " | awk -v p=\"$r/ttp-check.\" 'index($0, p) == 1 {s = substr($0, length(p) + 1); sub(/\\/.*/, \"\", s);"
          " print s}' | sort -u | tr '\\n' ' ')\"\n"
          "  for d in \"$t\"/ttp-check.*; do\n"
          "    [ -d \"$d\" ] || continue\n"
          "    if [ -z \"$(find \"$d\" -maxdepth 0 -user \"$u\" 2>/dev/null)\" ]; then\n"
          f"      [ -n \"$(find \"$d\" -maxdepth 0 -mmin -{RUNNING_MAX_AGE_MIN} 2>/dev/null)\" ] && n=$((n+1)); continue\n"
          "    fi\n"
          f"    p=$(head -c 20 \"$d/{remotecheck.RUN_PID}\" 2>/dev/null | tr -cd 0-9)\n"
          "    case \"$live\" in *\" ${d##*/ttp-check.} \"*) [ -n \"$p\" ] && { n=$((n+1)); continue; };; esac\n"
          "    if [ -n \"$p\" ] && kill -0 \"$p\" 2>/dev/null; then n=$((n+1)); continue; fi\n"
          f"    if [ -n \"$(find \"$d\" -maxdepth 0 -mmin -{FRESH_MIN} 2>/dev/null)\" ]; then n=$((n+1))\n"
          f"    elif [ -n \"$(find \"$d\" -maxdepth 0 -mmin +{REAP_AFTER_MIN} 2>/dev/null)\" ]; then rm -rf \"$d\" 2>/dev/null; fi\n"
          "  done\n"
          "else\n"
          "  n=$(find \"$t\" -maxdepth 1 -name 'ttp-check.*' -type d "
          f"-mmin -{RUNNING_MAX_AGE_MIN} 2>/dev/null | wc -l | tr -d ' ')\n"
          "fi\n"
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


LIMIT_KEYS = ("max_runs", "jobs")   # a host entry's own limits (entries_of): runs at once, test workers per run


def entries_of(v: Any) -> list:
    """A `delivery.checks_spill_hosts` (or checks.spill_hosts) value set by `ttp config`, normalized: a list (or
    a JSON-encoded list) of ssh aliases, each either a string or a table `{"host": "<alias>", "max_runs": N,
    "jobs": N}` giving that host its own limits (both optional, positive integers; limits). ValueError for
    anything else: a scalar or a table is refused, never read as one host; so is an unknown key in an entry."""
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            v = json.loads(v)
        except ValueError:
            pass
    if not isinstance(v, (list, tuple)):
        raise ValueError(f"delivery.{KEY}: {v!r} is not a list of ssh aliases (e.g. [\"check-host-a\"]); "
                         "nothing was changed")
    out, bad, seen = [], [], set()
    for x in v:
        if isinstance(x, dict):
            alias = x.get("host").strip() if isinstance(x.get("host"), str) else x.get("host")
            extra = sorted(set(x) - {"host", *LIMIT_KEYS})
            nums = [k for k in LIMIT_KEYS if k in x
                    and (isinstance(x[k], bool) or not isinstance(x[k], int) or x[k] < 1)]
            if why := _host_problem(alias):
                bad.append(why)
            elif extra:
                bad.append(f"{alias!r}: unknown key(s) {', '.join(map(repr, extra))} (allowed: host, "
                           + ", ".join(LIMIT_KEYS) + ")")
            elif nums:
                bad.append(f"{alias!r}: {', '.join(nums)} must be a positive whole number")
            entry = {"host": alias, **{k: x[k] for k in LIMIT_KEYS if k in x}}
            entry = alias if len(entry) == 1 else entry
        else:
            alias = entry = x.strip() if isinstance(x, str) else x
            if why := _host_problem(alias):
                bad.append(why)
        if not bad and alias not in seen:
            seen.add(alias)
            out.append(entry)
    if bad:
        raise ValueError(f"delivery.{KEY}: " + "; ".join(bad) + "; nothing was changed")
    return out


def hosts_of(v: Any) -> list[str]:
    """The ssh aliases of a spill-host list (entries_of), in order: the limits of a table entry left out."""
    return [e["host"] if isinstance(e, dict) else e for e in entries_of(v)]


def limits(cfg: dict | None, alias: str) -> dict:
    """A spill host's own limits, `{"max_runs": N, "jobs": N}` or part of it, from the first entry naming it in
    the project's list, else the account's pool; {} for a plain alias or a malformed list. max_runs: this
    machine's checks running there at once, at most (pick: its claims, which every project here shares);
    jobs: a run's test workers there, instead of the host's idle cores (workers)."""
    from . import project
    lists = [((cfg or {}).get("delivery") or {}).get(KEY) or [],
             (project.load_account_settings().get("checks") or {}).get("spill_hosts") or []]
    for v in lists:
        try:
            for e in entries_of(v):
                if isinstance(e, dict) and e["host"] == alias:
                    return {k: e[k] for k in LIMIT_KEYS if k in e}
                if e == alias:
                    return {}
        except ValueError:
            continue
    return {}


def excludes_of(v: Any) -> list[str]:
    """A `delivery.checks_spill_exclude` value set by `ttp config`: a list (or a JSON-encoded list) of ssh aliases
    or host names. ValueError for anything else."""
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            v = json.loads(v)
        except ValueError:
            pass
    if not isinstance(v, (list, tuple)):
        raise ValueError(f"delivery.{EXCLUDE_KEY}: {v!r} is not a list of ssh aliases or host names "
                         "(e.g. [\"check-host-a\"]); nothing was changed")
    out = [str(x).strip() if isinstance(x, str) else x for x in v]
    if bad := [why for x in out if (why := remotecheck.host_problem(x))]:
        raise ValueError(f"delivery.{EXCLUDE_KEY}: " + "; ".join(bad) + "; nothing was changed")
    return list(dict.fromkeys(out))


def _excludes(cfg: dict | None) -> set[str]:
    """The project's delivery.checks_spill_exclude, lower-cased; empty when unset or malformed (problems names it)."""
    try:
        return {x.lower() for x in excludes_of(((cfg or {}).get("delivery") or {}).get(EXCLUDE_KEY) or [])}
    except ValueError:
        return set()


def excluded(cfg: dict | None, alias: str, known: dict | None = None) -> bool:
    """Whether delivery.checks_spill_exclude keeps spill host `alias` out: it names the alias, its machine or
    ssh host (target), or the machines list's `hostname` for either (full or short name)."""
    out = _excludes(cfg)
    if not out:
        return False
    from . import machines
    known = machines.load() if known is None else known
    machine, host = target(alias, known)
    names = {alias, machine, host}
    for n in (machine, host):
        if h := str((known.get(n) or {}).get("hostname") or "").strip():
            names |= {h, h.split(".")[0]}
    return bool({n.lower() for n in names} & out)


def excluded_hosts(cfg: dict | None) -> list[str]:
    """The listed spill hosts (the project's own, then the pool) that delivery.checks_spill_exclude keeps out."""
    if not _excludes(cfg):
        return []
    from . import machines
    known = machines.load()
    return [h for h in _listed(cfg) if excluded(cfg, h, known)]


def exclude_lines(cfg: dict | None) -> list[str]:
    """`ttp checks status` line: the listed spill hosts delivery.checks_spill_exclude keeps out."""
    got = excluded_hosts(cfg)
    return [f"spill hosts excluded by the project (delivery.{EXCLUDE_KEY}): {', '.join(got)}"] if got else []


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
        if bad := banned_in(v):
            out.append(f"delivery.{KEY}: {', '.join(bad)} is banned by the account's {NO_SPILL_KEY}; "
                       "its checks never spill there")
    if (x := (delivery or {}).get(EXCLUDE_KEY)) is not None:
        try:
            excludes_of(x)
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
    try:
        remote_only_of((delivery or {}).get(REMOTE_ONLY_KEY))
    except ValueError as e:
        out.append(str(e).replace("; nothing was changed", ""))
    return out


def no_spill_of(v: Any) -> list[str]:
    """A `checks.no_spill_hosts` value set by `ttp config --account`: a list (or a JSON-encoded list) of ssh
    aliases or host names. ValueError for anything else."""
    try:
        return hosts_of(v)
    except ValueError as e:
        raise ValueError(str(e).replace(f"delivery.{KEY}", NO_SPILL_KEY)) from None


def _names(x: str) -> set[str]:
    """A host name and its short form (first label), lower-cased; an address keeps no short form."""
    x = str(x).strip().lower()
    short = x.split(".")[0]
    return set() if not x else {x} if short.isdigit() else {x, short}


def no_spill() -> list[str]:
    """The account's ban list (checks.no_spill_hosts) in order; [] when unset or malformed."""
    from . import project
    try:
        return no_spill_of((project.load_account_settings().get("checks") or {}).get("no_spill_hosts") or [])
    except ValueError:
        return []


def banned(alias: str, known: dict | None = None, ban: list[str] | None = None) -> str:
    """The checks.no_spill_hosts entry that keeps spill host `alias` out, else "": it names the alias, its
    machine or ssh host (target), or the machines list's `hostname` for either, by full or short name."""
    ban = no_spill() if ban is None else ban
    if not ban:
        return ""
    from . import machines
    known = machines.load() if known is None else known
    machine, host = target(alias, known)
    names = set().union(*(_names(n) for n in (alias, machine, host)))
    for n in (machine, host):
        names |= _names((known.get(n) or {}).get("hostname") or "")
    return next((b for b in ban if _names(b) & names), "")


def banned_in(v: Any, known: dict | None = None) -> list[str]:
    """The hosts of spill list `v` (a project's delivery.checks_spill_hosts or the account's pool) that
    checks.no_spill_hosts bans; [] when none is or `v` is malformed."""
    try:
        got = hosts_of(v)
    except ValueError:
        return []
    ban = no_spill()
    return [h for h in got if banned(h, known, ban)] if ban else []


def ban_refusal(key: str, v: Any) -> str:
    """Why setting spill list `key` to `v` is refused: it names a host checks.no_spill_hosts bans; else ""."""
    if bad := banned_in(v):
        return (f"{key}: {', '.join(bad)} is banned from every project's checks by the account's "
                f"{NO_SPILL_KEY} ({json.dumps(no_spill())}); nothing was changed")
    return ""


def banned_listed(cfg: dict | None) -> list[str]:
    """The hosts in the project's spill list or the account's pool that checks.no_spill_hosts keeps out."""
    if not no_spill():
        return []
    own = ((cfg or {}).get("delivery") or {}).get(KEY)
    try:
        mine = hosts_of(own) if own is not None else []
    except ValueError:
        return []
    if own is not None and not mine:
        return []
    return banned_in(list(dict.fromkeys([*mine, *account_hosts()])))


def settable_hosts(v: Any) -> list:
    """entries_of(v) for a config_set of delivery.checks_spill_hosts (aliases and tables with limits); ValueError
    also when it names a host the account's checks.no_spill_hosts bans."""
    got = entries_of(v)
    if why := ban_refusal(f"delivery.{KEY}", [e["host"] if isinstance(e, dict) else e for e in got]):
        raise ValueError(why)
    return got


def no_spill_lines() -> list[str]:
    """`ttp checks status` line: the account's ban list."""
    return [f"spill hosts banned for every project ({NO_SPILL_KEY}): {', '.join(b)}"] if (b := no_spill()) else []


def account_hosts() -> list[str]:
    """The account's pool (checks.spill_hosts in the account settings) in order; [] when unset or malformed."""
    from . import project
    try:
        return hosts_of((project.load_account_settings().get("checks") or {}).get("spill_hosts") or [])
    except ValueError:
        return []


def hosts(cfg: dict | None) -> list[str]:
    """The project's spill hosts in order, then the account's pool, less those checks.no_spill_hosts bans and
    those delivery.checks_spill_exclude keeps out (excluded); [] when the project's own list is empty (it keeps
    its checks here) or malformed (config_problems names it)."""
    listed = _listed(cfg)
    if not listed or not _excludes(cfg):
        return listed
    from . import machines
    known = machines.load()
    return [h for h in listed if not excluded(cfg, h, known)]


def _listed(cfg: dict | None) -> list[str]:
    """hosts() before delivery.checks_spill_exclude."""
    own = ((cfg or {}).get("delivery") or {}).get(KEY)
    try:
        mine = hosts_of(own) if own is not None else []
    except ValueError:
        return []
    if own is not None and not mine:
        return []
    listed = list(dict.fromkeys([*mine, *account_hosts()]))
    if not (ban := no_spill()):
        return listed
    from . import machines
    known = machines.load()
    return [h for h in listed if not banned(h, known, ban)]


def off_list(project_dir: str, alias: str) -> str:
    """Why spill host `alias` may no longer take a check of the project at `project_dir`, or "": hosts() of
    its settings as they are now no longer lists it (taken off its list or the account's pool, or banned).
    A folder that is no project, or settings that cannot be read, say nothing (the pick checked them)."""
    from .project import Project
    try:
        p = Project(project_dir)
        if not p.exists():
            return ""
        cfg = p.config()
    except Exception:
        return ""
    listed = hosts(cfg)
    if alias in listed:
        return ""
    try:   # a host of a listed machine (machine:host) is listed with it, as allowed() reads it; the run
        from . import machines   # names a listed `machine:host` entry by its ssh host (remotecheck --host)
        known = machines.load()
        if target(alias, known)[0] in listed or any(target(h, known)[1] == alias for h in listed):
            return ""
    except Exception:
        return ""
    return (f"{alias} is no longer one of the project's spill hosts (delivery.{KEY}, the account's pool, less "
            f"{NO_SPILL_KEY}); no check starts there")


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


def remote_only_of(v: Any) -> bool | None:
    """A `delivery.checks_remote_only` value set by `ttp config`: true, false, or auto (None: this machine's kind)."""
    if v is None or isinstance(v, bool):
        return v
    t = str(v).strip().lower()
    if t == "auto":
        return None
    try:
        return first_of(v)
    except ValueError:
        raise ValueError(f"delivery.{REMOTE_ONLY_KEY}: {v!r} is not true, false or auto; nothing was changed") from None


def remote_only(cfg: dict | None) -> bool:
    """Whether a heavy run that may spill waits for a busy spill host instead of taking a place here
    (delivery.checks_remote_only; default auto: on a workstation, a laptop or desktop where the user works,
    netwait.workstation, and off on a server). Only with spill hosts set and spill first on (first): the run
    falls back here only once no host is busy, every one out of reach or unusable (LAST_BUSY empty). Light
    checks (checklane) never wait for it: they run here at once."""
    if not hosts(cfg) or not first(cfg):
        return False
    try:
        v = remote_only_of(((cfg or {}).get("delivery") or {}).get(REMOTE_ONLY_KEY))
    except ValueError:
        v = None
    if v is not None:
        return v
    if (t := os.environ.get(REMOTE_ONLY_ENV)) in ("0", "1"):
        return t == "1"
    from . import netwait
    return netwait.workstation(cfg)


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


def workers_why(cores: int, load: float | None, others: int = 0, cap: int = 0) -> tuple[int, str]:
    """workers(), and what sized it, for the spill line and status (no silent throttling): the host's idle
    cores, those kept for this machine's `others` runs there, one run's limit `cap` (checks.test_run_max), and
    why a run gets a single worker."""
    n = workers(cores, load, others, cap)
    if not n:
        return 0, "test workers it sizes itself (the host's load did not read)"
    from . import checklane
    raw = int(cores - load)
    kept = others * checklane.run_cores(None, cores)
    why = f"{n} test worker(s), its idle cores (load {load:g} of {cores})"
    if others:
        why += f", less {kept} kept for {others} other run(s) of this machine there"
    if cap and n == cap and cap < min(cores, raw - kept):
        why += f", at most {cap} per run (limit checks.test_run_max)"
    if raw - kept < 1:
        why += (f"; only 1 because {max(0, raw)} core(s) are idle"
                + (f" and {kept} are kept for this machine's other runs there" if others else ""))
    return n, why


def _size(cfg: dict, host: str) -> tuple[int, str]:
    """workers_why for a run to `host` from its last probe (_READ), before this run's claim there."""
    from . import testslots
    cores, load = _READ.get(host, (0, None))
    top = testslots.sizes(cfg, cores) if cores > 0 else None   # one run's limit, sized to that host
    return workers_why(cores, load, _ours(host), top[1] if top else 0)


def _probe_all(items: list[tuple[str, bool]]) -> dict:
    """probe() of each (host, beside_device), at once: a pick compares them all, so none waits on another's ssh."""
    items = list(dict.fromkeys(items))
    if len(items) <= 1:
        return {h: probe(h, b) for h, b in items}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as ex:
        got = list(ex.map(lambda hb: probe(*hb), items))
    return {h: r for (h, _), r in zip(items, got)}


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
    """Whether check `c` may run on a spill host: it names no host of its own and has no spill host yet (a
    project's delivery.checks_host, its default_host, does not keep it there; host "local", or an agent
    CLI it runs (needs_cli), only with `"spill": true`), in a clean checkout, does not say `"spill": false`, and is unwrapped unless it
    has a remote wrapper (push.spill_wrap_of; one that does not parse keeps it here) or `cfg` says its
    wrappers need no device (wrapped_ok)."""
    from . import cleantree, push
    opt = getattr(c, "spill", None)
    own = getattr(c, "spill_host", "") or getattr(c, "host", "")
    if opt is False or getattr(c, "keep_here", False) or own not in ("", remotecheck.LOCAL) or cleantree.authoring(c):
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


def why_here(c: Any, cfg: dict | None = None) -> str:
    """Why check `c` may not spill (check_spills false), for the run's log and `ttp checks status`; "" when
    it may. Model-free, read in check_spills' order."""
    from . import cleantree, push
    opt = getattr(c, "spill", None)
    if opt is False or getattr(c, "keep_here", False):
        return 'it says "spill": false'
    if push.remote_host(c):
        return f"it runs on its own host {push.remote_host(c)}"
    if cleantree.authoring(c):
        return 'it runs in the authoring worktree ("cwd": "authoring")'
    if opt is not True:
        if getattr(c, "host", "") == remotecheck.LOCAL:
            return 'it says "host": "local"'
        if needs_cli(c):
            return "it runs an agent CLI, which spill hosts lack"
        if needs_history(c):
            return "it reads commits with git, which the one-commit copy there lacks"
    if host_path(c):
        return ("it names a path of this machine (outside the system folders), which a spill host or Slurm "
                "node, given a copy of the commit only, lacks")
    if check_spills(c, cfg):
        return ""
    return "it runs behind a wrapper that has no spill wrapper (checks.spill_wrap_cmd) for spill hosts"


def here_why(cmds: list, cfg: dict | None = None) -> str:
    """Why a run of `cmds` stays on this machine (spillable false): each check that may not spill and its
    reason (why_here), else that none may; "" when it may spill."""
    if spillable(cmds, cfg):
        return ""
    whys = [f"{str(c)[:80]}: {w}" for c in cmds if (w := why_here(c, cfg))]
    return "its checks cannot spill (" + ("; ".join(whys[:3]) if whys else "none may") + ")"


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
        target = False
        for j, w in enumerate(words):
            if target or re.match(r"&?\d*>", w):   # an output redirection writes there (`>f`, `> f`): any host can
                target = not target and bool(re.fullmatch(r"&?\d*>[>|&]?", w))
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
    on its own host ("host": push.remote_host, which keeps it there) or here beside it (stays_here). A
    worker's extra local pytest beside the project's host-pinned suite so spills too, never waiting for a
    checks slot here."""
    from . import push
    spills = [check_spills(c, cfg) for c in cmds]
    return any(spills) and all(ok or push.remote_host(c) or stays_here(c, cfg) for ok, c in zip(spills, cmds))


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
    if ban := banned(alias, known):
        return f"banned for every project by the account's {NO_SPILL_KEY} ({ban})"
    if excluded(cfg, alias, known):
        return f"excluded by the project (delivery.{EXCLUDE_KEY})"
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
        return _failed(alias, f"ssh timed out after {PROBE_TIMEOUT_S} s")
    except OSError as e:
        return _failed(alias, f"cannot run ssh: {e}")
    if r.returncode == 255:
        from . import sshagent
        if sshagent.locked(r.stderr or ""):
            return 0, f"ssh cannot reach it: {sshagent.WHY}", ""   # this machine's agent, not the host
        return _failed(alias, f"ssh cannot reach it: {(r.stderr.strip().splitlines() or ['exit 255'])[-1][:200]}")
    if r.returncode == 3:
        return 0, "it lacks git or tar", ""
    m = re.search(r"^ttp-spill (\d+) (\d+) (\d+)(?: (\d+(?:\.\d+)?|-))?$", r.stdout, re.M)
    if r.returncode != 0 or not m:
        return _failed(alias, f"its probe failed (exit {r.returncode})")
    busy, note = _device_holders(r.stdout)
    if busy and not beside_device:
        return 0, busy, ""
    note = busy or note
    cores, mem_kib, running = (int(x) for x in m.groups()[:3])
    load = float(m[4]) if m[4] and m[4] != "-" else None
    _READ[alias] = (cores, load)
    _note_host(alias, cores=cores, load=load)
    _CHECKS_RUNNING[alias] = running
    # This machine's runs started there in the last LOAD_LAG_S count as load already: many picks at once
    # each see the same 1-minute load, which shows none of them yet.
    seen = None if load is None else load + _pending_cores(alias, cores)
    if seen is not None and cores > 0 and seen >= LOAD_NEAR * cores:
        return 0, (f"its load is near its cores (load {load:g}"
                   + (f", {seen - load:g} more for this machine's runs just started" if seen > load else "")
                   + f", of {cores} cores; no check starts at {LOAD_NEAR:g} x or above, "
                   f"{running} check(s) running)"), note
    n = checklane.host_slots(cores, mem_kib / (1024 * 1024) if mem_kib else None, seen, running)
    if n > 0:
        return n, "", note
    return 0, (f"its {running} running check(s) fill it" if load is None
               else f"its idle cores and memory fit no check now (load {load:g} of {cores} cores, "
                    f"{running} check(s) running)"), note


def _failed(alias: str, why: str) -> tuple[int, str, str]:
    """probe's answer for a host that did not answer: backed off (_note_host)."""
    _note_host(alias, failed=why)
    return 0, why, ""


_OUT_OF_REACH = ("ssh ", "cannot run ssh", "it lacks", "its probe failed", "its pre-run hook error")   # probe and hook
LAST_BUSY: list[str] = []   # the hosts the last pick skipped only as busy (remote_only waits for them)
_READ: dict[str, tuple[int, float | None]] = {}   # the last probe's (cores, load) per host: pick sizes a run's workers from it
_CHECKS_RUNNING: dict[str, int] = {}   # the last probe's running checks per host: _claim counts in-flight claims beyond them
PINNED_POLL_S = 15.0   # how often a run pinned to a full host probes it again (admit_pinned)


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


def _down_file(host: str) -> Path:
    from . import checkcap
    return checkcap.slot_dir() / "spill-down" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', host)}.json"


def down_add(host: str, why: str, now: float | None = None) -> None:
    """Mark `host` down for every project on this machine (remotecheck: its pinned checks env cannot be made
    there, as on a host with no package index): pick skips it (down_why) until a run there passes its env
    step (down_clear) or DOWN_RETRY_S lets one run try again."""
    from . import project
    f = _down_file(host)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        project.durable_write(f, json.dumps({"host": host, "why": why[:300], "at": time.time() if now is None else now,
                                             "project": Path(os.environ.get("TTP_PROJECT") or "").name}), sync=False)
    except OSError:
        pass


def down_clear(host: str) -> None:
    """`host` is usable again (a run there passed its env step, or a health probe of it passed)."""
    try:
        _down_file(host).unlink(missing_ok=True)
    except OSError:
        pass


def down_why(host: str, now: float | None = None, move: bool = True) -> str:
    """Why pick skips `host` for every project (down_add), or "". Once DOWN_RETRY_S has passed, the first
    caller gets "" and (with `move`; a status read never) moves the mark's time on, so only its run tries
    the host again."""
    now = time.time() if now is None else now
    try:
        r = json.loads(_down_file(host).read_text() or "{}")
        at = float(r.get("at") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return ""
    if now - at >= DOWN_RETRY_S:
        if move:
            down_add(host, str(r.get("why") or ""), now)
        return ""
    return (f"down for every project here since {max(0, int((now - at) // 60))} min: {r.get('why') or 'a host fault'}; "
            f"one run tries it again {max(1, int((DOWN_RETRY_S - (now - at)) // 60))} min from now")


def busy_add(host: str, why: str, now: float | None = None) -> None:
    """Mark `host` busy for every project on this machine: its spill wrapper declined a run (its own workload,
    remotecheck.wrap_busy), so pick skips it (busy_why) for BUSY_COOL_S. Neither the host's fault nor the change's."""
    from . import checkcap, project
    f = checkcap.slot_dir() / "spill-busy" / _down_file(host).name
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        project.durable_write(f, json.dumps({"host": host, "why": why[:300], "at": time.time() if now is None else now}),
                              sync=False)
    except OSError:
        pass


def busy_why(host: str, now: float | None = None) -> str:
    """Why pick skips `host` as busy (busy_add, within BUSY_COOL_S), or ""."""
    from . import checkcap
    now = time.time() if now is None else now
    try:
        r = json.loads((checkcap.slot_dir() / "spill-busy" / _down_file(host).name).read_text() or "{}")
        at = float(r.get("at") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return ""
    if now - at >= BUSY_COOL_S:
        return ""
    return (f"busy: its spill wrapper declined a run {max(0, int(now - at))} s ago ({r.get('why') or 'its own workload'}); "
            f"tried again {max(1, int(BUSY_COOL_S - (now - at)))} s from now")


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
    d = _down_file("x").parent
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            host = json.loads(f.read_text() or "{}").get("host") or ""
        except (OSError, ValueError, AttributeError):
            continue
        if host and (why := down_why(host, now, move=False)):
            out.append(f"spill host {host} takes no checks: {why}")
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

    def note(self, rec: dict) -> None:
        """Rewrite its claim file's record as `rec`."""
        if self.f is not None:
            try:
                self.f.seek(0)
                self.f.truncate(0)
                self.f.write(json.dumps(rec))
                self.f.flush()
            except OSError:
                pass

    def close(self) -> None:
        note_limit("", "")   # off the host: its limit line leaves status
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
    # This machine's claims are among the host's running checks only once their runs started there: one
    # beyond the probe's count is still in flight and takes a free slot too, so concurrent pickers in any
    # project here never claim more than the host fits.
    ours = _ours(alias)
    if (running := _CHECKS_RUNNING.get(alias)) is not None:
        ours = min(ours, running)
    for i in range(max(0, free) + ours):
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


def admit_pinned(p, hosts: list[str], tell, what: str = "", bound: float | None = None,
                 sleep=time.sleep, clock=time.monotonic) -> list[Claim] | None:
    """A claim on each host checks are pinned to (delivery.checks_host or a check's own "host"), taken as a
    spill pick takes one (probe's free slots, _claim counting this machine's in-flight claims), so the runs of
    every project here never start more checks there than it fits. While a host is full the run waits, told
    once per reason with its numbers and shown in status (note_limit), at most `bound` seconds (None: until
    all are claimed): None then, holding nothing. A host the probe cannot read holds no run (its checks then
    fail or pass on their own), as remoteload.held never does."""
    rec = {"project": getattr(p, "name", ""), "task": os.environ.get("TTP_TASK") or "",
           "run": os.environ.get("TTP_RUN_ID") or "", "pid": os.getpid(), "since": time.time(),
           "what": what[:300], "pinned": True}
    got: dict[str, Claim] = {}
    start, said = clock(), {}
    while True:
        for host in hosts:
            if host in got:
                continue
            free, why, _ = probe(host, True)
            if why.startswith(_OUT_OF_REACH):
                got[host] = Claim(host, None)   # nothing to count it by: no hold
                continue
            if not why and (c := _claim(host, free, {**rec, "host": host})) is not None:
                got[host] = c
                continue
            why = why or (f"its {free} free check slot(s) are claimed by this machine's "
                          f"{_ours(host)} run(s) there")
            if said.get(host) != why:
                line = f"checks pinned to {host} wait for a check slot there: {why}"
                tell(line + (f" (at most {bound:.0f}s)" if bound is not None else ""))
                if p is not None:
                    note_limit(p.name, line, what)
                said[host] = why
        if len(got) == len(hosts):
            if said and p is not None:
                note_limit(p.name, "")
            return [c for c in got.values() if c.f is not None]
        if bound is not None and clock() - start >= bound:
            for c in got.values():
                c.close()
            return None
        sleep(PINNED_POLL_S)


def _ours(alias: str) -> int:
    """This machine's live claims on `alias`: they are among the host's running checks already."""
    return sum(1 for r in holders() if r.get("host") == alias)


def _state_file(host: str) -> Path:
    return claim_dir() / f"{host}.state"   # not *.json: holders reads those as claims


def host_state(host: str) -> dict:
    """What this machine last learned of spill host `host` (_note_host): its cores and load at `at`, and
    while it failed to answer, `fails` in a row and `until` (no probe before)."""
    try:
        r = json.loads(_state_file(host).read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return r if isinstance(r, dict) else {}


def _note_host(host: str, cores: int = 0, load: float | None = None, failed: str = "",
               now: float | None = None) -> None:
    """Record a probe of `host`: its cores and load (which ends a back-off), or `failed` (why it did not
    answer), which backs it off BACKOFF_S, doubling per failure in a row up to BACKOFF_MAX_S. Shared by
    every project of this machine, so one unreachable host costs one probe timeout per back-off, not one
    per pick."""
    now = time.time() if now is None else now
    st = host_state(host)
    if failed:
        n = int(st.get("fails") or 0) + 1
        st.update(fails=n, why=failed[:200], until=now + min(BACKOFF_MAX_S, BACKOFF_S * 2 ** (n - 1)))
    else:
        st = {"cores": cores, "load": load, "at": now}
    try:
        claim_dir().mkdir(parents=True, exist_ok=True)
        from . import project
        project.durable_write(_state_file(host), json.dumps(st), sync=False)
    except OSError:
        pass


def backed_off(host: str, now: float | None = None) -> str:
    """Why `host` is not probed now (its last probes failed: _note_host), else ""."""
    now = time.time() if now is None else now
    st = host_state(host)
    if not st.get("fails") or float(st.get("until") or 0) <= now:
        return ""
    from . import timefmt
    return (f"{st.get('why') or 'ssh cannot reach it'}; backing off after {st['fails']} failed probe(s), "
            f"next try at {timefmt.clock(now=float(st['until']))}")


def _run_cores(r: dict, cores: int) -> int:
    """The cores claim `r` takes on a host of `cores`: its test workers, else one run's (checklane.run_cores)."""
    from . import checklane
    return int(r.get("workers") or 0) or checklane.run_cores(None, cores or None)


def _pending_cores(host: str, cores: int, now: float | None = None) -> int:
    """The cores of this machine's claims on `host` from the last LOAD_LAG_S: not in its 1-minute load yet."""
    now = time.time() if now is None else now
    return sum(_run_cores(r, cores) for r in holders()
               if r.get("host") == host and now - float(r.get("since") or 0) < LOAD_LAG_S)


def used_share(host: str, recs: list | None = None, now: float | None = None) -> float:
    """How full `host` looks from here, 0 to 1 or more: its load at the last probe (if recent) plus the cores
    of this machine's claims there since, over its cores. pick tries the emptiest host first, so a share of
    runs follows each host's free cores and no one host takes them all. Unknown cores: its claims count."""
    now = time.time() if now is None else now
    recs = holders() if recs is None else recs
    st = host_state(host)
    cores = int(st.get("cores") or 0)
    ours = [r for r in recs if r.get("host") == host]
    if cores <= 0:
        return float(len(ours))
    at = float(st.get("at") or 0)
    fresh = now - at < 300 and st.get("load") is not None
    since = at - LOAD_LAG_S if fresh else 0.0   # runs just before the probe may not show in its load yet
    load = float(st["load"]) if fresh else 0.0
    return (load + sum(_run_cores(r, cores) for r in ours if float(r.get("since") or 0) >= since)) / cores


def _inflight(alias: str) -> int:
    """This machine's claims on `alias` its last probe did not see running yet (_CHECKS_RUNNING): they take
    free slots there too."""
    ours = _ours(alias)
    running = _CHECKS_RUNNING.get(alias)
    return ours if running is None else max(0, ours - running)


def all_off(off: str) -> bool:
    """Whether OFF_ENV's value `off` keeps the checks off every spill host ("1"; "" or a host list: no)."""
    return off == "1"


def spill_off_add(off: str, host: str, fault: bool, env_cfg: dict | None = None, cfg: dict | None = None,
                  slow: bool = False, hang: str = "", busy: bool = False) -> str:
    """OFF_ENV's value after the checks did not pass on spill host `host`: `fault` when the host's own fault
    ended them (exit 75), else they failed or timed out there. `env_cfg`: they failed at import for want of a
    package (push.env_problem), a fault of the spill env config `env_cfg` (env_off), never of the host.
    A fault is marked with its time and the checks config `cfg` it ran under, so it expires (kept_off).
    `slow`: the copy of the tree there timed out (remotecheck.COPY_TIMEOUT), a fault kept off at once (kept_off):
    another full copy timeout on the same link would only add to the wait; the rerun goes elsewhere or here.
    `hang`: check command `hang` was stopped there by its silence limit (checkwall.Silent): marked per command
    (hang_mark), so the same command hanging on HANG_VERDICT_HOSTS hosts is the change's (hang_hosts).
    `busy`: its spill wrapper declined the run as busy (remotecheck.wrap_busy): kept off at once, never a fault."""
    mark = (f"env.{_env_key(env_cfg)}.{int(time.time())}" if env_cfg is not None
            else BUSY if busy else SLOW if slow else hang_mark(hang) if hang else f"fault.{_env_key(cfg) if cfg is not None else ''}.{int(time.time())}" if fault else "fail")
    return ",".join([*(e for e in off.split(",") if e and e != "1"), f"{host}:{mark}"])


def fail_hosts(off: str) -> list[str]:
    """The spill hosts OFF_ENV's value `off` records the checks as failing on (a ":fail" mark: neither the
    host's own fault nor its env), in order, each once."""
    seen: list[str] = []
    for h, _, why in (e.rpartition(":") for e in off.split(",") if e and e != "1"):
        if (why == "fail" or why.startswith(HANG)) and h not in seen:
            seen.append(h)
    return seen


def hang_mark(check: str) -> str:
    """spill_off_add's mark of a host where check command `check` hung: HANG and a short hash of the command."""
    return HANG + hashlib.sha1(str(check).encode()).hexdigest()[:12]


def hang_hosts(off: str, check: str) -> list[str]:
    """The spill hosts OFF_ENV's value `off` records check command `check` as hung on (hang_mark), in order,
    each once: HANG_VERDICT_HOSTS of them make the hang the change's, never a host's."""
    mark, seen = hang_mark(check), []
    for h, _, why in (e.rpartition(":") for e in off.split(",") if e and e != "1"):
        if why == mark and h not in seen:
            seen.append(h)
    return seen


def _fault_counts(why: str, key: str | None, now: float) -> bool:
    """Whether the fault mark `why` still counts: under the checks config whose key is `key` (None: any; a
    mark with none recorded: any) and within FAULT_RETRY_S. A plain "fault" (an older run's mark) always does."""
    if why == "fault":
        return True
    if not why.startswith("fault."):
        return False
    _, k, at = (why.split(".") + ["", ""])[:3]
    return (key is None or not k or k == key) and at.isdigit() and now - int(at) <= FAULT_RETRY_S


def kept_off(off: str, cfg: dict | None = None, now: float | None = None) -> set[str]:
    """The spill hosts OFF_ENV's value `off` keeps a rerun off: each its checks failed on, and each whose own
    fault ended them HOST_FAULT_TRIES times under the checks config `cfg` has now (None: any) within the last
    FAULT_RETRY_S (once that config changes, a fixed checks.spill_wrap_cmd, or the time passes, it is tried
    again), and each its tree copy timed out on once (SLOW) or whose wrapper declined the run as busy (BUSY). An
    import failure keeps no host off (env_off)."""
    now = time.time() if now is None else now
    key = None if cfg is None else _env_key(cfg)
    seen = [e.rpartition(":") for e in off.split(",") if e and e != "1"]
    return {h for h, _, why in seen if not why.startswith("env.") and (
            why in ("fail", SLOW, BUSY) or why.startswith(HANG) or sum(1 for h2, _, w2 in seen if h2 == h and _fault_counts(w2, key, now)) >= HOST_FAULT_TRIES)}


def fails(state: Path, head: str, key: str) -> list[dict]:
    """The spill failures FAILS_FILE in project state `state` records for head `head` under the check commands
    whose hash is `key` (cli._commands_hash): {host, sig, env (the checks config key of an import failure, else
    ""), ts} each."""
    try:
        recs = json.loads((Path(state) / FAILS_FILE).read_text())
    except (OSError, ValueError):
        return []
    return [r for r in recs if isinstance(r, dict) and r.get("head") == head and r.get("key") == key] \
        if isinstance(recs, list) else []


def fail_add(state: Path, head: str, key: str, host: str, sig: str, env_cfg: dict | None = None) -> None:
    """Record in FAILS_FILE that head `head`'s checks (`key`) failed on spill host `host` with `sig` (its failing
    ids or missing import); `env_cfg`: it failed at import under that checks config. Entries past FAILS_KEEP_S go."""
    path, now = Path(state) / FAILS_FILE, time.time()
    try:
        with open(path.with_name(path.name + ".lock"), "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                recs = json.loads(path.read_text())
            except (OSError, ValueError):
                recs = []
            recs = [r for r in (recs if isinstance(recs, list) else [])
                    if isinstance(r, dict) and now - float(r.get("ts") or 0) <= FAILS_KEEP_S]
            recs.append({"head": head, "key": key, "host": host, "sig": sig,
                         "env": _env_key(env_cfg) if env_cfg is not None else "", "ts": now})
            from . import project
            project.durable_write(path, json.dumps(recs[-500:]))
    except OSError:   # a record is a bound on reruns: never a reason for the checks run to fail
        pass


def fails_off(off: str, recs: list[dict], cfg: dict) -> str:
    """OFF_ENV's value `off` keeping a run off each spill host `recs` (fails) shows its head's checks failed on:
    an import failure only while the checks config `cfg` is the one it failed under (a fixed env is tried again)."""
    hosts = [r["host"] for r in recs if r.get("host") and (not r.get("env") or r["env"] == _env_key(cfg))]
    return ",".join([*(e for e in off.split(",") if e and e != "1"), *(f"{h}:fail" for h in dict.fromkeys(hosts))])


def env_rerun_spent(recs: list[dict], cfg: dict) -> bool:
    """Whether a head's checks already failed at import on a spill host under the checks config `cfg` (fails):
    an import failure reruns once per head, so the next one counts."""
    return any(r.get("env") == _env_key(cfg) for r in recs)


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


def wrap_refused(p, host: str, why: str, what: str = "", cfg: dict | None = None) -> bool:
    """Record that `host`'s spill wrapper refused this run, or is missing there, for `why` (remotecheck.wrap_fault),
    in WRAP_ENV. Once every host that refused it gave the one reason and WRAP_SAME_HOSTS of them (or every
    spill host listed, two or more) did, it is the wrapper's setting, not the hosts: one limit line and one coordinator event
    (once per checks config and reason) name that reason and checks.spill_wrap_cmd. Else the host's own limit
    line. True when it is the setting. The run still goes on elsewhere or here, as for any host fault."""
    try:
        seen = json.loads(os.environ.get(WRAP_ENV) or "{}")
        seen = seen if isinstance(seen, dict) else {}
    except ValueError:
        seen = {}
    seen[host] = why
    os.environ[WRAP_ENV] = json.dumps(seen)
    listed = set(hosts(cfg)) if cfg is not None else set()
    if len(set(seen.values())) != 1 or len(seen) < 2 or not (len(seen) >= WRAP_SAME_HOSTS or listed <= set(seen)):
        note_limit(p.name, f"spill host {host}: {why}; its checks run again on another host or here", what)
        return False
    said = (f"the spill wrapper refused these checks on every spill host they went to ({len(seen)}: "
            f"{', '.join(seen)}) for one reason: {why}. That is the wrapper's setting (checks.spill_wrap_cmd), "
            "not the hosts")
    note_limit(p.name, f"{said}; the checks run here or on another host", what)
    import hashlib
    import sqlite3
    fp = f"checkspill:wrap:{_env_key(cfg)}:{hashlib.sha1(why.encode()).hexdigest()[:8]}"
    try:
        if not p.db.q("SELECT 1 FROM events WHERE fingerprint=? LIMIT 1", (fp,)):
            p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                   (time.time(), "daemon", "observation", fp, "high",
                    f"Spilled checks never run: {said}. Check how checks.spill_wrap_cmd (or a check's own "
                    "spill_wrap_cmd) finds its wrapper on those hosts and fix the setting or the hosts.", "queued"))
    except (OSError, sqlite3.Error, AttributeError):   # an event is a courtesy: never a reason for the run to fail
        pass
    return True


def pick(p, cfg: dict, tell, what: str = "", off: str = "", why_here: str = "") -> Claim | None:
    """The first spill host that is allowed, reachable and has a free slot, and that the project's pre-run hook
    (pre_check) lets the run go to, claimed; None when none is. One host of a machine with several (target) is
    probed, claimed and held by its own name, its machine's lock held too. Each host skipped is told with why.
    `off`: OFF_ENV's value, whose hosts (kept_off) are skipped. `why_here`: why the run tries them now (default:
    the slots here stayed busy). The claim's `workers` are sized to the host's idle cores at its probe (workers).
    LAST_BUSY lists after it the hosts skipped only as busy (a lock, a device job, the hook's "not now", full
    slots), never one not allowed, paused, kept off or out of reach (_OUT_OF_REACH)."""
    LAST_BUSY.clear()
    rec = {"project": p.name, "task": os.environ.get("TTP_TASK") or "", "run": os.environ.get("TTP_RUN_ID") or "",
           "pid": os.getpid(), "since": time.time(), "what": what[:300]}
    skipped, keep = [*(f"{h} (banned by the account's {NO_SPILL_KEY})" for h in banned_listed(cfg)),
                     *(f"{h} (excluded by the project: delivery.{EXCLUDE_KEY})" for h in excluded_hosts(cfg))], kept_off(off, cfg)
    if env := env_off(off, cfg):
        tell(f"no spill host could take the checks, waiting here: {env}")
        _note_pick(p.name, "", [f"{alias} ({env})" for alias in hosts(cfg)])
        return None
    mine, now = holders(), time.time()
    tried = [e.rpartition(":")[0] for e in off.split(",")]
    # Spread: hosts these checks did not fail on yet, then those not backed off, then the emptiest (its load
    # and this machine's runs there over its cores: used_share) first, so runs follow free cores.
    gated = []   # (alias, machine, host, why before the probe, usable, dev, beside)
    for alias in sorted(hosts(cfg), key=lambda h: (h in tried, bool(backed_off(target(h)[1], now)),
                                                  used_share(target(h)[1], mine, now))):
        machine, host = target(alias)
        if alias in keep or host in keep:
            skipped.append(f"{alias} (these checks did not pass there in this run)")
            continue
        if why := quarantined(p, alias):
            skipped.append(f"{alias} ({why})")
            continue
        if down := down_why(host):
            skipped.append(f"{alias} ({down})")
            continue
        beside = beside_device(cfg, alias)   # the project lets its checks run there beside a device job
        why = allowed(p, cfg, alias)
        usable = not why   # past `allowed`, a skip is the host being busy unless it is out of reach
        why, dev = why or backed_off(host, now) or busy_why(host, now) or lock_busy(p, cfg, alias), ""
        if not why and beside:
            dev = device_busy(p, cfg, host)
            _note_skip(host, "", p.name, own_only=True)   # this project's earlier skip no longer holds
        elif not why:
            why = device_busy(p, cfg, host)
            _note_skip(host, why, p.name)
        if not why and (why := pre_check(p, cfg, alias)):
            _note_skip(host, why, p.name)
        gated.append((alias, machine, host, why, usable, dev, beside))
    # Every host left is probed (at once), and the run goes to the one with the most free capacity: the
    # most free slots less this machine's claims not yet running there (_inflight), then the most test
    # workers it would get there (workers_why); ties keep the spread order above.
    probed = _probe_all([(host, beside) for _, _, host, why, _, _, beside in gated if not why])
    cands = []
    for i, (alias, machine, host, why, usable, dev, beside) in enumerate(gated):
        free, note, size, lim = 0, "", (0, ""), limits(cfg, alias)
        if not why:
            free, why, note = probed[host]
            if not why and "max_runs" in lim and (free := min(free, lim["max_runs"] - _ours(host))) <= 0:
                why = f"its max_runs {lim['max_runs']} of this machine's checks run there already"
            if why.startswith(DEVICE_WHY):
                _note_skip(host, why, p.name)
            if note.startswith(DEVICE_WHY):
                dev, note = dev or note, ""
            if not why:
                size = _size(cfg, host)
        dev = dev[len(DEVICE_WHY):] if dev.startswith(DEVICE_WHY) else dev
        cands.append((alias, machine, host, why, usable, dev, free, note, size, i))
    first = [c for c in cands if not c[3]]
    # The hosts that cannot take it go first, only to be told as skipped; then the free ones, most free first.
    order = [c for c in cands if c[3]] + sorted(first, key=lambda c: (-(c[6] - _inflight(c[2])), -c[8][0], c[9]))
    for alias, machine, host, why, usable, dev, free, note, size, _ in order:
        if not why and (got := _claim(host, free, {**rec, "host": host,
                                                   **({"beside": dev[:300]} if dev else {})})) is not None:
            got.lock = _hold(p, cfg, machine, what)
            if got.lock is None:
                got.close()
                skipped.append(f"{alias} (busy: its lock was taken just now)")
                LAST_BUSY.append(alias)
                continue
            got.workers, sized = size
            if jobs := limits(cfg, alias).get("jobs"):   # the host entry's own test workers, instead of its idle cores
                got.workers, sized = jobs, f"{jobs} test worker(s), its jobs setting"
            got.note({**rec, "host": host, **({"beside": dev[:300]} if dev else {}),
                      **({"workers": got.workers} if got.workers else {})})   # its cores, for used_share
            note_here(p.name, "")   # this run is on a spill host now: no reason to show it here
            more = [c[0] for c in first if c[0] != alias]
            tell(f"spilling to {alias}: "
                 + (why_here or f"the slots here stayed busy for {after_s(cfg):g} s")
                 + f" ({_source(cfg, alias)}); the checks run there on a copy of the commit"
                 + (f" with {sized}" if sized else "")
                 + (f"; the most free of {len(more) + 1} usable spill hosts" if more else "")
                 + (f"; {note}, so its device locks alone decided" if note else "")
                 + (f"; beside a device job (checks.{BESIDE_KEY} lists it): {dev}" if dev else "")
                 + (f"; skipped: {'; '.join(skipped)}" if skipped else ""))
            note_limit(p.name, f"spill host {alias}: {sized}", what)
            _note_pick(p.name, alias, skipped)
            return got
        if usable and not why.startswith(_OUT_OF_REACH):
            LAST_BUSY.append(alias)
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


def _held(path: Path) -> bool:
    """Whether a run still holds claim file `path` (its flock, _claim). A record a killed run left (a crash or a
    reboot: its flock went with it) is no claim, even when its pid was reused or cannot be judged; such
    records counted as this machine's runs there shrank a spilled run to one test worker (workers)."""
    try:
        with open(path) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def holders(project_name: str | None = None) -> list[dict]:
    """The live claims on spill hosts (of `project_name`'s runs when given): a held claim file (_held) whose pid
    is not known dead. Read only."""
    out = []
    for f in sorted(claim_dir().glob("*.json")) if claim_dir().is_dir() else []:
        try:
            r = json.loads(f.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get("host") and _alive(r.get("pid")) and _held(f) \
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


def limit_dir() -> Path:
    from . import checkcap
    return checkcap.slot_dir() / "limits"


def note_limit(project_name: str, line: str, what: str = "") -> None:
    """Record the limit that slows or holds this process's checks run now (its name, value and setting, as its
    checks log says it): limit_lines shows it in `ttp status` and the web app's checks view while the process
    lives; "" clears it. Records of dead processes go at each write. Never raises."""
    d = limit_dir()
    try:
        for g in d.glob("*.json") if d.is_dir() else []:
            if g.stem != str(os.getpid()) and g.stem.isdigit() and not _alive(int(g.stem)):
                g.unlink(missing_ok=True)
        f = d / f"{os.getpid()}.json"
        if not line:
            f.unlink(missing_ok=True)
            return
        d.mkdir(parents=True, exist_ok=True)
        from . import project
        project.durable_write(f, json.dumps({"project": project_name, "task": os.environ.get("TTP_TASK") or "",
                                             "pid": os.getpid(), "why": line[:400], "what": what[:200],
                                             "at": time.time()}), sync=False)
    except OSError:
        pass


def limit_lines(project_name: str) -> list[str]:
    """`ttp status` and `ttp checks status` lines: the limit each live checks run of `project_name` is under."""
    out = []
    for f in sorted(limit_dir().glob("*.json")) if limit_dir().is_dir() else []:
        try:
            r = json.loads(f.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get("project") == project_name and r.get("why") and _alive(r.get("pid")):
            who = f"task #{r['task']}" if r.get("task") else f"pid {r.get('pid')}"
            out.append(f"checks of {who} limited: {r['why']}")
    return out


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
           here_why(cmds, cfg))
    if why:
        note_here(p.name, why, what)
        return None, cmds
    claim = pick(p, cfg, tell, what, off, why_here or f"spill first (delivery.{FIRST_KEY}): the project host "
                                                       "keeps only checks that cannot spill")
    if claim is None:
        return None, cmds
    from .cli import _spill_to
    got = [_unspilled(c, was) for c, was in zip(_spill_to(cmds, claim.host, cfg, claim.workers), cmds)]
    for c in got:   # each check's start checks the host is still on the project's list (off_list)
        if getattr(c, "spill_host", "") == claim.host:
            c.spill_project = str(p.base)
    return claim, got


def _unspilled(c: Any, was: Any) -> Any:
    """`c` (spilled from `was`) remembering `was`, so a spill host's fault reruns the check elsewhere."""
    if getattr(c, "spilled", False):
        c.was = was
    return c


# Quarantine of a spill host whose own environment fails runs (#2226): model-free, per project.
STRIKES_KV = "checks_spill_strikes"   # project state: each spill host's strikes, recent spilled failures, quarantines
QUARANTINE_KEY = "spill_quarantine_after"   # checks.spill_quarantine_after: strikes in a row that take a host off
QUARANTINE_AFTER = 3
QUARANTINE_MAX_S = 7 * 86400   # a quarantine's backstop end, if no known-good run (known_good) has passed by then
KNOWN_GOOD_DIR = "spill-known-good"   # under the project's state: each quarantined host's known-good run
KNOWN_GOOD_RETRY_S = 3600   # a known-good run that failed there starts again after this; a refused one (exit 75) at once
CONFIG_WINDOW_S = 3600   # one failure on CONFIG_FAULT_HOSTS hosts within this, no spilled pass between: a config fault
CONFIG_FAULT_HOSTS = 2
FAILS_KEPT = 100


def quarantine_after_of(v: Any) -> int:
    """checks.spill_quarantine_after as set: a whole number from 1, else ValueError."""
    try:
        n = int(str(v).strip())
    except ValueError:
        raise ValueError("checks.spill_quarantine_after is a whole number of strikes, 1 or more") from None
    if n < 1 or isinstance(v, bool) or isinstance(v, float) and v != n:
        raise ValueError("checks.spill_quarantine_after is a whole number of strikes, 1 or more")
    return n


def quarantine_after(cfg: dict | None) -> int:
    """checks.spill_quarantine_after; unset or malformed: QUARANTINE_AFTER."""
    checks = (cfg or {}).get("checks")
    v = checks.get(QUARANTINE_KEY) if isinstance(checks, dict) else None
    try:
        return quarantine_after_of(v) if v is not None else QUARANTINE_AFTER
    except ValueError:
        return QUARANTINE_AFTER


def _strikes(db) -> dict:
    doc = db.kv(STRIKES_KV, {}) or {}
    doc = doc if isinstance(doc, dict) else {}
    doc["hosts"] = {k: v for k, v in (doc.get("hosts") or {}).items() if isinstance(v, dict)} \
        if isinstance(doc.get("hosts"), dict) else {}
    return doc


def _event(db, now: float, fp: str, severity: str, text: str) -> None:
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
         (now, "checks", "observation", fp, severity, text, "queued"))


def strike_fail(p, host: str, head: str, sig: str, now: float | None = None) -> None:
    """A spilled run of `head` failed on `host` (no host fault, no timeout) with failure signature `sig`
    (remotecheck.fail_sig): kept until `head` passes elsewhere (strike_settle). Never raises."""
    now = time.time() if now is None else now
    try:
        with p.db.tx():
            doc = _strikes(p.db)
            doc["fails"] = [*(f for f in doc.get("fails") or [] if isinstance(f, dict)),
                            {"host": host, "head": head, "sig": (sig or "")[:200], "at": now}][-FAILS_KEPT:]
            p.db.set_kv(STRIKES_KV, doc)
    except Exception:   # a strike never stops the rerun
        pass


def strike_settle(p, cfg: dict | None, head: str, passed_on: str = "", now: float | None = None) -> list[str]:
    """`head` passed (on spill host `passed_on`, else here). `passed_on`'s strikes reset. Each spill host whose
    run of `head` failed (strike_fail) takes a strike, unless that failure's signature hit CONFIG_FAULT_HOSTS
    hosts within CONFIG_WINDOW_S with no spilled pass between: a project-wide config fault (one incident, no
    strike, those hosts' strikes reset, and a host quarantined earlier for that signature lifted); a bare
    `exit <rc>` signature never matches (remotecheck.shared_sig). A host at quarantine_after strikes in a row is
    quarantined (pick skips it), with one event and a machine ledger entry whose until-probe (known_good) lifts it
    once a known-good run passes there, QUARANTINE_MAX_S at the latest (its auto-undo).
    Returns the hosts quarantined now. Never raises."""
    now = time.time() if now is None else now
    took, after, freed = [], quarantine_after(cfg), set()
    try:
        with p.db.tx():
            doc = _strikes(p.db)
            hosts = doc.setdefault("hosts", {})
            if passed_on:
                hosts.setdefault(passed_on, {})["n"] = 0
                doc["pass_at"] = now
            fails = [f for f in doc.get("fails") or [] if isinstance(f, dict) and f.get("host")]
            since = max(now - CONFIG_WINDOW_S, float(doc.get("pass_at") or 0))
            seen = {k: v for k, v in (doc.get("incidents") or {}).items() if now - float(v or 0) < CONFIG_WINDOW_S}
            for f in fails:
                if f.get("head") != head or f.get("settled"):
                    continue
                f["settled"] = now
                if f["host"] == passed_on:   # failed, then passed on the same host: no strike either way
                    continue
                sig = str(f.get("sig") or "")
                same = {g["host"] for g in fails if g.get("sig") == sig and float(g.get("at") or 0) >= since}
                if remotecheck.shared_sig(sig) and len(same) >= CONFIG_FAULT_HOSTS:
                    for h in same:
                        hosts.setdefault(h, {})["n"] = 0
                    # A host quarantined earlier for this failure was never at fault: it comes back.
                    freed.update(k for k, v in hosts.items() if (v.get("off") or {}).get("sig") == sig)
                    # One incident, one event: env_event's fingerprint (a spill env fault already told for this
                    # checks config is not told again).
                    fp = f"checkspill:env:{_env_key(cfg)}"
                    if sig not in seen and not p.db.q("SELECT 1 FROM events WHERE fingerprint=? LIMIT 1", (fp,)):
                        seen[sig] = now
                        _event(p.db, now, fp, "high",
                               f"Spill config fault: spilled checks failed the same way on {len(same)} spill hosts "
                               f"({', '.join(sorted(same))}: {sig or 'no error line'}) and passed on this machine, "
                               "so the fault is the project's spill setup (checks.spill_wrap_cmd, its env), not the "
                               "hosts: no host is quarantined. Fix the setting; the reruns here keep the verdicts right.")
                    continue
                h = hosts.setdefault(f["host"], {})
                h["n"] = int(h.get("n") or 0) + 1
                if h["n"] >= after and float((h.get("off") or {}).get("until") or 0) <= now:
                    h["off"] = {"since": now, "until": now + QUARANTINE_MAX_S, "n": h["n"], "sig": sig}
                    took.append(f["host"])
                    _event(p.db, now, f"spill-quarantine:{f['host']}:{now:.0f}", "normal",
                           f"Spill host {f['host']} quarantined: {h['n']} spilled runs in a row failed there and "
                           f"passed elsewhere (last: {sig or 'no error line'}); its checks run elsewhere until a "
                           f"known-good run of the base passes there (`ttp checks --spill-probe {f['host']}`, "
                           f"its machine ledger entry's until-probe) or `ttp checks --spill-back {f['host']}`.")
            doc.update(fails=fails[-FAILS_KEPT:], incidents=seen)
            p.db.set_kv(STRIKES_KV, doc)
    except Exception:
        return []
    for host in sorted(freed - set(took)):
        try:
            lift(p, host, now)
        except Exception:
            pass
    from . import machine_ledger
    for host in took:   # the ledger's file lock, outside the db transaction
        try:
            c = machine_ledger.add_change(
                host, f"spill host quarantined for {p.name}'s checks: its spilled runs fail where they pass "
                      "elsewhere", f"ttp checks --spill-back {host}", p.name, expires=now + QUARANTINE_MAX_S,
                until_probe=f"ttp checks --spill-probe {shlex.quote(host)} || exit 1", auto_undo=True,
                queue=p.state, extra={"kind": "spill_quarantine"})
            with p.db.tx():
                doc = _strikes(p.db)
                if (off := doc["hosts"].get(host, {}).get("off")) is not None:
                    off["change"] = c.get("id")
                    p.db.set_kv(STRIKES_KV, doc)
        except Exception:   # not in the machines list, the ledger unwritable: known_good and the end still hold
            pass
    return took


def quarantined(p, alias: str, now: float | None = None) -> str:
    """Why `alias` is quarantined for project `p`'s checks ("" when it is not). A quarantine past its end
    ends here as lift ends one (its event, its strikes reset), ledger entry or not."""
    from . import timefmt
    now = time.time() if now is None else now
    try:
        off = _strikes(p.db)["hosts"].get(alias, {}).get("off")
        if not off:
            return ""
        if float(off.get("until") or 0) <= now:
            lift(p, alias, now)
            return ""
    except Exception:
        return ""
    if off.get("kind") == "health":   # spillhealth: back once its health check passes
        return f"quarantined: its health check failed ({off.get('why')}); back once it passes, or with `ttp checks --spill-back {alias}`"
    return (f"quarantined: {off.get('n')} spilled runs in a row failed there and passed elsewhere; back once a "
            f"known-good run of the base passes there (`ttp checks --spill-probe {alias}`) or with "
            f"`ttp checks --spill-back {alias}`, at the latest {timefmt.clock(now=float(off['until']))}")


def lift(p, alias: str, now: float | None = None) -> bool:
    """`ttp checks --spill-back <alias>`: end `alias`'s quarantine (one event, its ledger entry closed) and its
    strikes. True when it was quarantined. Safe to run twice."""
    now = time.time() if now is None else now
    with p.db.tx():
        doc = _strikes(p.db)
        h = doc["hosts"].get(alias)
        off = h.pop("off", None) if isinstance(h, dict) else None
        if not isinstance(h, dict):
            return False
        off = off if isinstance(off, dict) else None
        h["n"] = 0
        p.db.set_kv(STRIKES_KV, doc)
        if off:
            _event(p.db, now, f"spill-back:{alias}:{now:.0f}", "normal",
                   f"Spill host {alias} is back in {p.name}'s spill pool (its quarantine ended).")
    if off and off.get("change"):
        from . import machine_ledger
        try:
            machine_ledger.close_change(int(off["change"]), "spill host back", p.name, now, queue=p.state)
        except (ValueError, OSError):   # closed already (by hand, or by the auto-undo running this)
            pass
    return bool(off)


def _known_good_rc(p, alias: str) -> Path:
    return Path(p.state) / KNOWN_GOOD_DIR / re.sub(r"[^A-Za-z0-9_.-]", "_", alias) / "known-good.rc"


def known_good(p, cfg: dict | None, alias: str, now: float | None = None) -> tuple[int, str]:
    """`ttp checks --spill-probe <alias>`, the until-probe of a quarantine's ledger entry, run by the daemon
    on its probe timer: (exit, what it found). The known-good run is the push branch tip's spillable checks,
    run detached on `alias` behind its spill wrapper (the probe's own time limit is far shorter than a
    suite). Passed: the host is lifted (lift), exit 0. Running: exit 1. Refused there (exit 75, a busy host
    or a host fault): started again now, no strike. Failed: started again after KNOWN_GOOD_RETRY_S.
    Not quarantined: exit 0."""
    from . import detach
    now = time.time() if now is None else now
    off = _strikes(p.db)["hosts"].get(alias, {}).get("off")
    if not isinstance(off, dict):
        return 0, "not quarantined"
    rc = _known_good_rc(p, alias)
    if off.get("probe"):
        st, code = detach.state(rc)
        if st in ("running", "queued"):
            return 1, f"its known-good run is {st} ({rc.with_suffix('.log')})"
        if st == "ended" and code == "0":
            lift(p, alias, now)
            return 0, f"its known-good run of {off['probe']} passed: back in the spill pool"
        ended = rc.stat().st_mtime if rc.exists() else 0.0
        if code != str(remotecheck.FAULT_RC) and now - ended < KNOWN_GOOD_RETRY_S:
            return 1, (f"its known-good run of {off['probe']} failed ({code or st}); again after "
                       f"{KNOWN_GOOD_RETRY_S // 60} min")
    base, why = _known_good_start(p, cfg, alias, rc)
    if not base:
        return 1, f"no known-good run started: {why}"
    with p.db.tx():
        doc = _strikes(p.db)
        if isinstance(o := doc["hosts"].get(alias, {}).get("off"), dict):
            o["probe"] = base
            p.db.set_kv(STRIKES_KV, doc)
    return 1, f"a known-good run of {base} started there ({rc.with_suffix('.log')})"


def _known_good_start(p, cfg: dict | None, alias: str, rc: Path) -> tuple[str, str]:
    """Start the known-good run (known_good): (the base commit, "") or ("", why not)."""
    import json
    from . import detach, gatejudge, locks, project, push
    from .runner import boot_id
    repo, base = gatejudge._push_tip(p)
    if not base:
        return "", "the push branch tip is unreadable"
    configured, _ = push.repo_checks((cfg or {}).get("delivery") or {}, repo, p)
    cmds = [c for c in push.check_list(configured) if check_spills(c, cfg)]
    if not cmds:
        return "", "no configured check may spill"
    script = " && ".join(shlex.join(push.check_argv(c, commit=base)) for c in push.spill_to(cmds, alias, cfg))
    rc.parent.mkdir(parents=True, exist_ok=True)
    lock = locks.try_take([locks.job_lock(rc)], f"spill known-good run on {alias}", script)
    if lock is None:
        return "", "an earlier known-good run still runs"
    try:
        for f in (rc, rc.with_suffix(".log"), detach.lost(rc), detach.started(rc)):
            f.unlink(missing_ok=True)
        env = {k: v for k, v in project.command_env(p).items() if k not in ("TTP_LOCKS_HELD", "TTP_LOCKS_SHARED")}
        pid = detach.start(rc, rc.with_suffix(".log"), ["sh", "-c", script], str(repo), env, lock)
    finally:
        lock.close()
    project.durable_write(rc.parent / "detached.json", json.dumps([{"rc": str(rc), "pid": pid, "boot": boot_id(),
                                                                    "start": time.time()}]))
    return base, ""
