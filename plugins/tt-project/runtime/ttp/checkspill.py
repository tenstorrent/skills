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
broker this project watches on it (a heal check with preset broker on that machine). A broker whose status
does not read within BROKER_TIMEOUT_S, or reads no job rows, never keeps a run off: the locks alone decide.
Runs already on the host go on. The skip shows in one line in `ttp status` and the web app (skip_lines)
while it is fresh (SKIP_SHOW_S). Its slots:
the lanes its cores and available memory give (checklane.default_slots) less the remote checks
running there from any machine (their ttp-check.* folders); this machine also holds one claim file
per slot it uses there (spill/<alias>.<n>.json in the checks slot folder), so its own runs never
take more. A claim whose remote ttp-check.* folder is not made yet (the few seconds of its copy) is
counted by neither, so the host can take one run more than its slots in that gap: a bound, not a guard.

Only a run whose every check would run here spills: none with a host of its own (`host`,
delivery.checks_host), none run in the authoring worktree ("cwd": "authoring"), none behind
checks.wrap_cmd (the project's lock or queue wrapper, which may hold a device or a machine here) unless
the project says its wrappers need no device (`checks.wrap_needs_device: false`; unset or true: the
need is unknown or real, so it stays here; on a spill host it runs without the wrapper, which is this
machine's), and never a light run (it never waits). A run that does not pass on a spill host runs again here (OFF_ENV):
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
ACCOUNT_KEY = "checks.spill_hosts"   # the account's pool (project.ACCOUNT_KEYS), after a project's own list
NO_DEVICE_KEY = "wrap_needs_device"   # checks.wrap_needs_device false: a wrapped run needs no device and may spill
AFTER_KEY = "checks_spill_after_s"
SPILL_AFTER_S = 60.0
OFF_ENV = "TTP_CHECKS_NO_SPILL"   # set for the local rerun of a run that did not pass on a spill host
PROBE_TIMEOUT_S = 30
DEVICE_SUFFIX = "-device"
BROKER_TIMEOUT_S = 10.0   # one read of a broker's status; past it the device locks alone decide
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


def wrap_problems(checks: Any) -> list[str]:
    """Config check of checks.wrap_needs_device."""
    v = checks.get(NO_DEVICE_KEY) if isinstance(checks, dict) else None
    return [] if v is None or isinstance(v, bool) else [f"checks.{NO_DEVICE_KEY}: {v!r} is not true or false"]


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


def spillable(cmds: list, cfg: dict | None = None) -> bool:
    """Whether every check of `cmds` would run on this machine, in a clean checkout, and unwrapped unless
    `cfg` says its wrappers need no device (wrapped_ok)."""
    from . import cleantree, push
    wrapped = wrapped_ok(cfg)
    return bool(cmds) and all(not push.remote_host(c) and getattr(c, "host", "") != remotecheck.LOCAL
                              and not cleantree.authoring(c) and (wrapped or not getattr(c, "wrap", ()))
                              for c in cmds)


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


def lock_busy(p, cfg: dict, alias: str) -> str:
    """"" when `alias`'s machine lock is free for a shared taker (as `ttp lock --probe --shared` answers),
    else why not: "busy (held by ...; N waiting)"."""
    from . import lockprobe
    try:
        rc, said = lockprobe.answer(p, cfg, alias, shared_mode=True)
    except OSError as e:
        return f"its lock cannot be read: {e}"
    return "" if rc == 0 else str(said).split(": ", 1)[-1]


def device_locks_of(cfg: dict, alias: str, known: dict | None = None) -> list[str]:
    """The `ttp lock` resources that are `alias`'s device, each as the lock it is held by (locks.canonical):
    `<alias>-device`, the device names its machines-list `shared` entry gives, and the config's
    device.locks on it (the alias, `<alias>-...` or `<alias>:...`)."""
    from . import locks, machines
    known = machines.load() if known is None else known
    dev = locks.device_locks(cfg)
    names = [alias + DEVICE_SUFFIX]
    got = (known.get(alias) or {}).get("shared")
    if isinstance(got, list):
        names += [x for x in got if isinstance(x, str) and x and x != alias and (x.endswith(DEVICE_SUFFIX) or x in dev)]
    names += sorted(d for d in dev if d == alias or d.startswith((alias + "-", alias + ":")))
    return list(dict.fromkeys(locks.canonical(cfg, n) for n in names))


def _lock_held(p, cfg: dict, res: str) -> str:
    """"" when nobody holds, reserves or waits for lock `res`, here (where this project's `ttp lock` takes
    it) or in the user's shared locks (another project that shares it), else the probe's line."""
    from . import lockprobe, locks, shared
    dirs = list(dict.fromkeys([shared.locks_dir(p, res, cfg), shared.root() / res]))
    for d in dirs:
        paths = list(dict.fromkeys([*locks.slot_paths(d, res, shared.slots(p, res, cfg)),
                                    *sorted(Path(d).glob(f"{res}.*.lock"))]))
        if not (locks.probe(d, res, paths, shared=True) and locks.probe(d, res, paths)):
            return lockprobe.line(res, False, d, res, paths, p)
    return ""


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
    for res in device_locks_of(cfg, alias):
        try:
            if said := _lock_held(p, cfg, res):
                return f"a device job runs there: {said}"
        except OSError as e:
            return f"its device lock {res} cannot be read: {e}"
    return f"a device job runs there: {job}" if (job := broker_job(p, alias)) else ""


def skip_dir() -> Path:
    from . import checkcap
    return checkcap.slot_dir() / "spill-skips"


def _note_skip(alias: str, why: str, project_name: str) -> None:
    """Record that `alias` took no new checks because of a device job (skip_lines shows it); "" clears it."""
    f = skip_dir() / f"{alias}.json"
    try:
        if not why:
            f.unlink(missing_ok=True)
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"host": alias, "why": why[:300], "at": time.time(), "project": project_name}))
        os.replace(tmp, f)
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
        return (locks.try_take if device else locks.try_take_shared)(paths, who, f"ttp checks (spilled): {what}")
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
        why = allowed(p, cfg, alias) or lock_busy(p, cfg, alias)
        if not why:
            why = device_busy(p, cfg, alias)
            _note_skip(alias, why, p.name)
        free = 0
        if not why:
            free, why = probe(alias)
        if not why and (got := _claim(alias, free, {**rec, "host": alias})) is not None:
            got.lock = _hold(p, cfg, alias, what)
            if got.lock is None:
                got.close()
                skipped.append(f"{alias} (busy: its lock was taken just now)")
                continue
            tell(f"spilling to {alias}: the slots here stayed busy for {after_s(cfg):g} s "
                 f"({_source(cfg, alias)}); the checks run there on a copy of the commit"
                 + (f"; skipped: {'; '.join(skipped)}" if skipped else ""))
            return got
        skipped.append(f"{alias} ({why or 'this machine holds its free slots'})")
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
                   + (f" (claim's pid {r.get('pid')}: {FOREIGN})" if pid_state(r.get("pid")) == UNKNOWN else ""))
    return out
