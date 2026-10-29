"""Machine activity: who held a lock or ran a detached job on a machine, and when.

Each project appends its `ttp lock` takes and releases and its `ttp detach` starts and ends to
state/activity.jsonl. The log is bounded: past MAX_BYTES it rotates to activity.1.jsonl (one old
generation is kept), so it never holds more than about twice that. A job's end is appended by its
wrapper (detach.WRAPPER) without the log's lock: a rotation in between moves the line to the old
generation, which is read as well.

`ttp machines activity <alias>` and the resume note after a reboot read every project's log on this
machine through the registry, read-only: nothing here writes another project's state, lock files
or queue tickets.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import project
from .project import durable_append

LOG, OLD, LOCK = "activity.jsonl", "activity.1.jsonl", "activity.lock"
MAX_BYTES = 256 * 1024
HEAD = 120   # the command head kept per entry, in characters
ENV_LOG, ENV_ID = "TTP_ACTIVITY_LOG", "TTP_ACTIVITY_ID"   # read (and unset) by detach.WRAPPER
REBOOT_WINDOW_S = 30 * 60
REBOOT_LINES = 5


def head(cmd) -> str:
    """The first HEAD characters of a command, whitespace collapsed."""
    text = " ".join(map(str, cmd)) if isinstance(cmd, (list, tuple)) else str(cmd or "")
    return re.sub(r"\s+", " ", text).strip()[:HEAD]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def log_path(state: Path) -> Path:
    return Path(state) / LOG


def append(state: Path, rec: dict) -> None:
    """Append one entry to the project's log, rotating it first when it would pass MAX_BYTES. Never
    raises: a log that cannot be written must not stop the command it describes."""
    state = Path(state)
    line = json.dumps(rec, sort_keys=True) + "\n"
    try:
        state.mkdir(parents=True, exist_ok=True)
        with open(state / LOCK, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            log = state / LOG
            try:
                size = log.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(line) > MAX_BYTES:
                os.replace(log, state / OLD)
            durable_append(log, line)
    except OSError:
        pass


def base(name: str, **kw) -> dict:
    """The fields every entry of this process carries: project, task, run and host."""
    return {"project": name, "task": os.environ.get("TTP_TASK") or None,
            "run": os.environ.get("TTP_RUN_ID") or None, "host": project.hostname(), **kw}


def entries(state: Path) -> list[dict]:
    """The project's log entries, oldest first; unreadable lines are skipped."""
    out = []
    for name in (OLD, LOG):
        try:
            text = (Path(state) / name).read_text(errors="replace")
        except OSError:
            continue
        for ln in text.splitlines():
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("id"):
                out.append(rec)
    return out


def _held(path: Path) -> bool:
    """Whether some process holds the flock of an existing file; opened read-only, never created."""
    try:
        with open(path) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            return False
    except OSError:
        return False


def _own_pid(s: dict) -> bool:
    """Whether a take's pid means a process to this one: seen in this PID namespace (a take from before
    pidns was logged counts) and not a kernel thread here (a sandboxed taker's own pid 2 or 13 is one on
    the host, alive for the whole boot)."""
    from .locks import _pid_ns, kthread
    return ("pidns" not in s or (s["pidns"] is not None and s["pidns"] == _pid_ns())) and not kthread(s["pid"])


def spans(recs: list[dict], here: str | None = None) -> list[dict]:
    """Take and release (or job start and end) entries joined into one span each: kind lock or job,
    start, end (None while open), rc and state: ended, running or unknown (no end logged: a kill
    or a reboot, or a remote job whose end only its host knows)."""
    here = project.hostname() if here is None else here
    # A quick job's wrapper may log its end before `ttp detach` logs the start: ends join in any order.
    ends = {r["id"]: r for r in recs if r.get("ev") in ("release", "job_end")}
    by: dict[str, dict] = {}
    for r in recs:
        ev = r.get("ev")
        if ev in ("take", "job"):
            e = ends.get(r["id"], {})
            by[r["id"]] = {**r, "kind": "lock" if ev == "take" else "job", "end": e.get("end"), "rc": e.get("rc")}
    out = []
    for s in by.values():
        s.pop("ev", None)
        if s["end"] is not None:
            s["state"] = "ended"
        elif s["kind"] == "job" and s.get("rcfile") and not s.get("machine"):
            rc = Path(s["rcfile"])
            try:
                s.update(end=rc.stat().st_mtime, rc=_rc(rc.read_text()), state="ended")
            except OSError:
                s["state"] = "running" if s.get("host") == here and _held(rc.with_suffix(".lock")) else "unknown"
        elif s["kind"] == "lock" and s.get("job_rc"):   # a `ttp detach --lock` job's: alive while its job lock is
            s["state"] = "running" if s.get("host") == here and _held(Path(s["job_rc"]).with_suffix(".lock")) \
                else "unknown"   # held, never by a pid another process may reuse
        elif s["kind"] == "lock" and s.get("host") == here and s.get("pid") and _same_boot(s.get("boot")) \
                and _own_pid(s) and _alive(int(s["pid"])):
            s["state"] = "running"
        else:
            s["state"] = "unknown"
        out.append(s)
    return sorted(out, key=lambda s: float(s.get("start") or 0))


def _rc(text: str):
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        return text or None


def _same_boot(boot) -> bool:
    from .runner import same_boot
    return bool(boot) and same_boot(boot)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def on_machine(alias: str, s: dict, hostnames: set[str]) -> bool:
    """A span is on machine `alias`: its resource is the alias or one on it (`<alias>-x`), a remote
    job ran there, or it ran on this host while `alias` is this host (hostnames: its names, lower case)."""
    from .responsibilities import _names
    if s.get("machine"):   # a remote job runs there, not on the host that started it
        return s["machine"] == alias
    return _names(alias, str(s.get("resource") or "")) or str(s.get("host") or "").lower() in hostnames


OPEN_BACK_S = 86400   # a span with no end logged counts this far back from a window's end, at most


def in_window(s: dict, since: float, until: float) -> bool:
    """A span overlaps [since, until]. One still running overlaps from its start on; one with no end
    logged (cut off by a kill or a reboot) counts when it started in the window, or within
    OPEN_BACK_S before its end: a lock taken long before a reboot was cut off by it too."""
    start = float(s.get("start") or 0)
    if start > until:
        return False
    if s.get("end") is not None:
        return float(s["end"]) >= since
    return s.get("state") == "running" or start >= min(since, until - OPEN_BACK_S)


def parse_since(raw: str | None, now: float, default_s: float = 86400, where=None) -> float:
    """`--since`: a delay back (`30m`, `6h`, `2d`) or an ISO date or time, read in the home zone of `where`
    (a Project, settings or zone name; None: timefmt.ambient()) unless it names a zone or offset."""
    s = (raw or "").strip()
    if not s:
        return now - default_s
    m = re.fullmatch(r"([0-9]+) ?([smhdw])", s)
    if m:
        return now - int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}[m[2]]
    iso = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", s.replace(" ", "T", 1).replace("Z", "+00:00"))
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(f"--since {raw!r}: use a delay such as 30m or 6h, or an ISO time such as 2026-10-05T09:00")
    from . import timefmt
    return (d if d.tzinfo else d.replace(tzinfo=timefmt.reading_zone(where))).timestamp()


def _alias_hostnames(alias: str, known: dict) -> set[str]:
    """This host's names when `alias` is this host (so its local entries count), else none."""
    me = project.hostname().lower()
    names = {alias.lower(), str((known.get(alias) or {}).get("hostname") or "").lower()} - {""}
    return {me} if me in names else set()


def _lock_dirs(alias: str, projects: list) -> list[tuple[str, Path]]:
    """(owner, dir) of each lock directory with resources on `alias`: the shared ones and each project's own."""
    from . import shared
    from .responsibilities import _names
    out = []
    try:
        out += [("shared", d) for d in sorted(shared.root().iterdir()) if d.is_dir() and _names(alias, d.name)]
    except OSError:
        pass
    return out + [(name, base / "state" / "locks") for name, base in projects]


def _current(alias: str, owner: str, d: Path, view: dict) -> None:
    from . import locks
    from .responsibilities import _names
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return
    for n in names:
        path = d / n
        if n.endswith(".lock") and n.count(".") >= 2:
            res = n.rsplit(".", 2)[0]
            if _names(alias, res) and _held(path):
                try:
                    h = json.loads(path.read_text() or "{}")
                except (OSError, ValueError):
                    h = {}
                h = h if isinstance(h, dict) else {}
                view["holders"].append({"resource": res, "where": owner, "holder": h.get("holder"),
                                        "since": h.get("since"), "command": head(h.get("command") or ""),
                                        "pid": h.get("pid"), "alive": locks.pid_alive(h),
                                        **{k: h[k] for k in ("via", "taker_pid") if k in h}})
        elif n.endswith(".reserved") and _names(alias, n[:-len(".reserved")]):
            who = locks.reserved_by(path)
            if who:
                view["reservations"].append({"resource": n[:-len(".reserved")], "where": owner, "holder": who})
        elif n.endswith(".queue") and _names(alias, n[:-len(".queue")]):
            try:
                tickets = sorted(t for t in os.listdir(path) if not t.startswith("."))
            except OSError:
                continue
            for t in tickets:
                if _held(path / t):
                    try:
                        who = (path / t).read_text().strip()
                    except OSError:
                        who = ""
                    view["waiters"].append({"resource": n[:-len(".queue")], "where": owner, "holder": who,
                                            "since": float(t.split("-", 1)[0]) if t[:1].isdigit() else None})


def _exclusive(alias: str, name: str, base: Path, view: dict) -> None:
    """Running tasks of a project whose `exclusive:<resource>` label is on `alias`, read-only."""
    from . import globalcap
    from .responsibilities import _names
    db = base / "state" / "project.db"
    if not db.is_file():
        return
    try:
        conn = globalcap.connect_ro(db)
    except Exception:
        view["unread"].append(name)
        return
    try:
        rows = conn.execute("SELECT id, title, labels FROM tasks WHERE status='running'").fetchall()
    except Exception:
        view["unread"].append(name)
        rows = []
    finally:
        conn.close()
    for tid, title, labels in rows:
        try:
            got = json.loads(labels or "[]")
        except ValueError:
            continue
        for lb in got if isinstance(got, list) else []:
            if isinstance(lb, str) and lb.startswith("exclusive:") and _names(alias, lb.split(":", 1)[1]):
                view["exclusive"].append({"project": name, "task": tid, "title": str(title)[:80],
                                          "resource": lb.split(":", 1)[1]})


def machine_view(alias: str, since: float, now: float | None = None) -> dict:
    """`ttp machines activity <alias>`: current holders, waiters, reservations, exclusive tasks and
    live jobs on the machine, and the logged spans in [since, now], from every project on this host."""
    from . import machines
    from .responsibilities import _projects
    now = time.time() if now is None else now
    known = machines.load()
    projects = _projects(project.hostname())
    hosts = _alias_hostnames(alias, known)
    view = {"alias": alias, "known": alias in known, "since": since, "now": now, "holders": [], "waiters": [],
            "reservations": [], "exclusive": [], "jobs": [], "activity": [], "unread": []}
    for owner, d in _lock_dirs(alias, projects):
        _current(alias, owner, d, view)
    for name, base in projects:
        _exclusive(alias, name, base, view)
        for s in spans(entries(base / "state")):
            if not on_machine(alias, s, hosts):
                continue
            if s["kind"] == "job" and s["state"] == "running":
                view["jobs"].append(s)
            if in_window(s, since, now):
                view["activity"].append(s)
    view["activity"].sort(key=lambda s: float(s.get("start") or 0))
    return view


def _who(s: dict) -> str:
    task = f"task #{s['task']}" if s.get("task") else "no task"
    return f"{s.get('project') or '?'} {task}" + (f" (run {s['run']})" if s.get("run") else "")


def span_line(s: dict, where=None) -> str:
    from . import timefmt
    what = (f"lock {s.get('resource')}" + (f" ({s['mode']})" if s.get("mode") not in (None, "lock") else "")
            if s["kind"] == "lock" else f"job {s.get('job')}" + (f" on {s['machine']}" if s.get("machine") else ""))
    start = timefmt.short(float(s.get("start") or 0), where)
    end = (f"to {timefmt.short(float(s['end']), where)}" + (f" rc={s['rc']}" if s.get("rc") not in (None, "") else "")
           if s.get("end") is not None else "still running" if s["state"] == "running" else "no end logged")
    flag = "DISRUPTIVE " if s.get("disruptive") else ""
    return f"{flag}{what}: {_who(s)} {start} {end}" + (f": {s['cmd']}" if s.get("cmd") else "")


def lines(view: dict, where=None) -> list[str]:
    """`ttp machines activity` text."""
    from . import timefmt
    out = [f"{view['alias']}" + ("" if view["known"] else " (not in `ttp machines list`)")
           + f": activity since {timefmt.short(view['since'], where)}"]
    for h in view["holders"]:
        out.append(f"  holds {h['resource']}: {h.get('holder') or '?'}"
                   + (f" since {timefmt.short(float(h['since']), where)}" if h.get("since") else "")
                   + (f": {h['command']}" if h.get("command") else ""))
    for w in view["waiters"]:
        out.append(f"  waits for {w['resource']}: {w.get('holder') or '?'}")
    for r in view["reservations"]:
        out.append(f"  reserved {r['resource']}: {r['holder']}")
    for x in view["exclusive"]:
        out.append(f"  exclusive {x['resource']}: {x['project']} task #{x['task']} {x['title']}")
    for j in view["jobs"]:
        out.append(f"  live job: {span_line(j, where)}")
    if not any(view[k] for k in ("holders", "waiters", "reservations", "exclusive", "jobs")):
        out.append("  nothing holds, waits for or runs on it now")
    out += [f"  {span_line(s, where)}" for s in view["activity"]] or ["  no logged activity in the window"]
    if view["unread"]:
        out.append("  could not read: " + ", ".join(view["unread"]))
    return out


def before_boot(at: float, where=None, window_s: float = REBOOT_WINDOW_S, cap: int = REBOOT_LINES) -> list[str]:
    """Up to `cap` lines of what ran on this host in the `window_s` before a boot at `at`, from every
    project's log here (read-only): disruptive entries first, then the ones without a logged end
    (cut off by the reboot), then the latest."""
    from .responsibilities import _projects
    me = project.hostname().lower()
    got = []
    for _name, base in _projects(project.hostname()):
        for s in spans(entries(base / "state"), here=""):   # nothing from before the boot still runs
            if str(s.get("host") or "").lower() == me and not s.get("machine") and in_window(s, at - window_s, at):
                got.append(s)
    got.sort(key=lambda s: (not s.get("disruptive"), s.get("end") is not None, -float(s.get("start") or 0)))
    out = [span_line(s, where) for s in got[:cap]]
    if len(got) > cap:
        out.append(f"... {len(got) - cap} more: `ttp machines activity <alias> --since <time>`")
    return out
