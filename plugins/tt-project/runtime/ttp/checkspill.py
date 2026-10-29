# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Checks that spill to other machines when this one's check slots are busy: `delivery.checks_spill_hosts`.

A project may list, in order, the ssh aliases of other machines its checks may run on. Unset or
empty, checks never leave this machine. A `ttp checks` run that has waited `delivery.checks_spill_after_s`
(default SPILL_AFTER_S) for a slot here (its project's lane, a host-wide checks slot, test-worker
slots) tries the listed hosts in order and runs on the first that is allowed, reachable and has a
free slot, as a check with a check host does (remotecheck: a copy of exactly the commit, its output
streamed back). With none, it keeps its place in the queue here and tries them again after the same
bound. The run says where it went, checks.json records it (`hosts`, `spilled_to`), and `ttp status`
and `ttp checks status` show it while it runs (status_lines).

Allowed: named in the project's charter and in the user's machines list (not retired), and not
paused (a pause of its alias, or a fleet hold of the machine). A host that is not allowed, that ssh
does not reach, that lacks git or tar, or whose slots are full is skipped, never a failure. Its slots:
the lanes its cores and available memory give (checklane.default_slots) less the remote checks
running there from any machine (their ttp-check.* folders); this machine also holds one claim file
per slot it uses there (spill/<alias>.<n>.json in the checks slot folder), so its own runs never
take more.

Only a run whose every check would run here spills: none with a host of its own (`host`,
delivery.checks_host), none run in the authoring worktree ("cwd": "authoring"), none behind
checks.wrap_cmd (the project's lock or queue wrapper, which may hold a device or a machine here), and
never a light run (it never waits). A run that does not pass on a spill host runs again here (OFF_ENV):
a spill host's own environment never fails a change, only a pass there counts.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from . import remotecheck

KEY = "checks_spill_hosts"
AFTER_KEY = "checks_spill_after_s"
SPILL_AFTER_S = 60.0
OFF_ENV = "TTP_CHECKS_NO_SPILL"   # set for the local rerun of a run that did not pass on a spill host
PROBE_TIMEOUT_S = 30
RUNNING_MAX_AGE_MIN = 720   # a remote ttp-check.* folder older than this is a leftover, not a running check
_PROBE = ("command -v git >/dev/null 2>&1 && command -v tar >/dev/null 2>&1 || exit 3\n"
          "c=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)\n"
          "m=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null)\n"
          "n=$(find \"${TMPDIR:-/tmp}\" -maxdepth 1 -name 'ttp-check.*' -type d "
          f"-mmin -{RUNNING_MAX_AGE_MIN} 2>/dev/null | wc -l)\n"
          "echo \"ttp-spill $c ${m:-0} $n\"\n")


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


def hosts(cfg: dict | None) -> list[str]:
    """The project's spill hosts in order; [] when unset or malformed (config_problems names it)."""
    try:
        return hosts_of(((cfg or {}).get("delivery") or {}).get(KEY) or [])
    except ValueError:
        return []


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


def spillable(cmds: list) -> bool:
    """Whether every check of `cmds` would run on this machine, unwrapped, in a clean checkout."""
    from . import cleantree, push
    return bool(cmds) and all(not push.remote_host(c) and getattr(c, "host", "") != remotecheck.LOCAL
                              and not cleantree.authoring(c) and not getattr(c, "wrap", ()) for c in cmds)


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
    if not _charter_names(p, alias):
        return "not named in the charter"
    if (held := lockprobe.pause(p, cfg, alias)) is not None:
        return f"paused{': ' + str(held.get('reason')) if isinstance(held, dict) and held.get('reason') else ''}"
    return ""


def probe(alias: str) -> tuple[int, str]:
    """(free slots, "") on `alias`, or (0, why it cannot take a check now)."""
    from . import checklane
    try:
        r = subprocess.run(remotecheck._ssh(alias, _PROBE), stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return 0, f"ssh timed out after {PROBE_TIMEOUT_S} s"
    except OSError as e:
        return 0, f"cannot run ssh: {e}"
    if r.returncode == 255:
        return 0, f"ssh cannot reach it: {(r.stderr.strip().splitlines() or ['exit 255'])[-1][:200]}"
    if r.returncode == 3:
        return 0, "it lacks git or tar"
    m = re.search(r"^ttp-spill (\d+) (\d+) (\d+)$", r.stdout, re.M)
    if r.returncode != 0 or not m:
        return 0, f"its probe failed (exit {r.returncode})"
    cores, mem_kib, running = (int(x) for x in m.groups())
    n = checklane.default_slots(cores, mem_kib / (1024 * 1024) if mem_kib else None)
    return (n - running, "") if running < n else (0, f"its {n} check slot(s) are busy")


def claim_dir() -> Path:
    from . import checkcap
    return checkcap.slot_dir() / "spill"


class Claim:
    """This machine's hold of one slot on spill host `host`; `close()` frees it."""

    def __init__(self, host: str, f):
        self.host = host
        self.f = f

    def close(self) -> None:
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
        why = allowed(p, cfg, alias)
        free = 0
        if not why:
            free, why = probe(alias)
        if not why and (got := _claim(alias, free, {**rec, "host": alias})) is not None:
            tell(f"spilling to {alias}: the slots here stayed busy for {after_s(cfg):g} s "
                 f"(delivery.{KEY}); the checks run there on a copy of the commit"
                 + (f"; skipped: {'; '.join(skipped)}" if skipped else ""))
            return got
        skipped.append(f"{alias} ({why or 'this machine holds its free slots'})")
    if skipped:
        tell("no spill host could take the checks, waiting here: " + "; ".join(skipped))
    return None


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


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
    now = time.time() if now is None else now
    out = []
    for r in holders(project_name):
        who = f"task #{r['task']}" if r.get("task") else f"pid {r.get('pid')}"
        mins = max(0, int((now - float(r.get("since") or now)) // 60))
        out.append(f"checks of {who} running on spill host {r['host']} for {mins} min"
                   + (f": {r['what']}" if r.get("what") else ""))
    return out
