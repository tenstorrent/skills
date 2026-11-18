# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Box occupancy: one shared, model-free check of whether a live realtime stream holds a machine.

Sharing a box with real users through the device broker is fine: agents zipper in with their jobs.
What must never be disturbed is a live stream with realtime guarantees (serving). Before a deploy,
restart, test, benchmark, warm-up, health check or reset on a box, every harness path asks
`blocks(target, step)`; `ttp occupancy <alias>` shows the same answer.

Sources. Nothing ships hardcoded: each project names its own read-only source per machine alias in
its settings, `occupancy.sources`:

    {"box-a": {"cmd": "<read-only command>", "timeout_s": 10, "fronts": ["router-a"]}}
    {"box-b": {"file": "/path/to/status.json", "max_age_s": 120}}

A command exits 0 while a stream is live, 1 while none is, anything else (or a timeout) is unknown.
A file holds `live`/`free` or JSON {"live": true|false}; one older than `max_age_s` is unknown.
`fronts` names the stream's front boxes (a router or proxy): they are covered only for deploys and
restarts, and only while the stream is live. Each project's sources are published to
~/.tt-project/occupancy-sources.json (publish), so every project on the host consults every source
registered for a box, not only its own.

Answer: live, free or unknown, with each source, the time it was read and `clear_at`: the stream's
last live reading plus a quiet window (`occupancy.quiet_s`, default 60 s, the largest any registering
project set). Inside the window the box still counts as live. Unknown on a box with a source counts
as live too (never a silent pass), and the source's own daemon raises one event (watch). With no
source registered for a box the answer is free and nothing changes.

Readings are shared in ~/.tt-project/occupancy-readings.json (each source read at most every CACHE_S),
with each source's live spans for the last HISTORY_S, which the daemon's watchdog (watch) intersects
with this project's own jobs on the box (activity.py): an overlap posts at once at high severity.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import boundedchild, project

SOURCES = "occupancy-sources.json"
READINGS = "occupancy-readings.json"
LOCK = "occupancy.lock"
QUIET_S = 60.0            # default quiet window after a stream's last live reading
TIMEOUT_S = 10.0          # default source timeout
FILE_MAX_AGE_S = 120.0    # a file source older than this is unknown
CACHE_S = 20.0            # a source is read at most this often, across every project on the host
HISTORY_S = 2 * 86400.0   # live spans kept this long, for the watchdog and status
WAIT_S = 60.0             # callers that wait retry about this often
STEPS = ("deploy", "restart", "test", "bench", "warmup", "health", "reset")
FRONT_STEPS = ("deploy", "restart")   # what a front box is covered for
FLAGS_KEY = "occupancy_flags"         # kv: this project's flagged overlaps (watch)
SEEN_KEY = "occupancy_seen"           # kv: span ids already judged (watch)
FLAGS_KEPT = 200
WHERE: Any = None   # the zone times are shown in (a project or zone name; set by callers that know it)


def _path(name: str) -> Path:
    return project.HOME_DIR / name


@contextlib.contextmanager
def _locked():
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(_path(LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _load(name: str) -> dict:
    try:
        doc = json.loads(_path(name).read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _save(name: str, doc: dict) -> None:
    path = _path(name)
    project.durable_write(path, json.dumps(doc, indent=1, sort_keys=True))


def _num(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f > 0 else default


def settings(cfg: dict) -> dict:
    """A project's own registrations from its settings: {"sources": {alias: source}, "quiet_s"}."""
    sec = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
    out = {}
    for alias, src in (sec.get("sources") or {}).items() if isinstance(sec.get("sources"), dict) else ():
        if not isinstance(src, dict) or not (src.get("cmd") or src.get("file")):
            continue
        fronts = src.get("fronts") or []
        out[str(alias)] = {"cmd": str(src.get("cmd") or ""), "file": str(src.get("file") or ""),
                           "timeout_s": _num(src.get("timeout_s"), TIMEOUT_S),
                           "max_age_s": _num(src.get("max_age_s"), FILE_MAX_AGE_S),
                           "fronts": [str(f) for f in fronts] if isinstance(fronts, list) else [str(fronts)]}
    return {"sources": out, "quiet_s": _num(sec.get("quiet_s"), QUIET_S)}


def source_problem(value: Any) -> str | None:
    """Why an `occupancy.sources` value cannot be used, or None."""
    if not isinstance(value, dict):
        return "occupancy.sources is an object of machine alias -> {cmd | file, timeout_s, fronts}"
    for alias, src in value.items():
        if not isinstance(src, dict) or not (src.get("cmd") or src.get("file")):
            return f"occupancy.sources.{alias} needs a read-only `cmd` (exit 0 live, 1 free) or a `file`"
    return None


def publish(p, cfg: dict | None = None) -> dict:
    """Write this project's registrations into the shared registry (removed when it has none)."""
    mine = settings(p.config() if cfg is None else cfg)
    with _locked():
        doc = _load(SOURCES)
        projects = doc.setdefault("projects", {})
        if mine["sources"]:
            projects[p.name] = {**mine, "base": str(p.base), "at": time.time()}
        elif projects.pop(p.name, None) is None:
            return mine
        _save(SOURCES, doc)
    return mine


def registry() -> dict:
    """{project: {"sources", "quiet_s", "base"}} of every project on the host that registered one.
    A registration whose project folder (`base`) is gone is dropped: a deleted project's stale source
    must never hold a box for the projects that remain."""
    return {k: v for k, v in (_load(SOURCES).get("projects") or {}).items()
            if isinstance(v, dict) and not (v.get("base") and not os.path.exists(str(v["base"])))}


def covered() -> dict[str, list[tuple[str, str, dict, str]]]:
    """alias -> [(project, stream alias, source, role)]: role "stream" for a source's own box,
    "front" for a box a source names as its stream's front."""
    out: dict[str, list] = {}
    for proj, reg in sorted(registry().items()):
        for alias, src in (reg.get("sources") or {}).items():
            src = {**src, "quiet_s": reg.get("quiet_s", QUIET_S), "base": reg.get("base", "")}
            out.setdefault(alias, []).append((proj, alias, src, "stream"))
            for f in src.get("fronts") or []:
                out.setdefault(f, []).append((proj, alias, src, "front"))
    return out


def _read_file(src: dict, now: float) -> tuple[str, str]:
    path = Path(os.path.expanduser(src["file"]))
    try:
        age = now - path.stat().st_mtime
        text = path.read_text().strip()
    except OSError as e:
        return "unknown", f"file unreadable: {e.strerror or e}"
    if age > src.get("max_age_s", FILE_MAX_AGE_S):
        return "unknown", f"file {age:.0f} s old"
    try:
        doc = json.loads(text)
    except ValueError:
        doc = text.lower()
    live = doc.get("live") if isinstance(doc, dict) else {"live": True, "free": False, "true": True,
                                                         "false": False}.get(str(doc))
    if live is None:
        return "unknown", "file says neither live nor free"
    return ("live" if live else "free"), "file"


def _read_cmd(src: dict) -> tuple[str, str]:
    cwd = src.get("base") or None
    limit = src.get("timeout_s", TIMEOUT_S)
    # boundedchild: its own process group, killed whole on a timeout, a signal or the return
    res = boundedchild.run(src["cmd"], limit, shell=True, cwd=cwd if cwd and os.path.isdir(cwd) else None)
    if res.timed_out:
        return "unknown", f"timed out after {limit:g} s"
    if res.rc is None:
        return "unknown", f"could not run: {res.error or 'no exit code'}"
    tail = (res.out or res.err or "").strip().splitlines()[-1:] or [""]
    state = {0: "live", 1: "free"}.get(res.rc, "unknown")
    return state, f"exit {res.rc}" + (f": {tail[0][:120]}" if tail[0] else "")


def read_source(src: dict, now: float | None = None) -> tuple[str, str]:
    """One source read now: (live | free | unknown, detail)."""
    now = time.time() if now is None else now
    return _read_file(src, now) if src.get("file") else _read_cmd(src)


def _reading(proj: str, alias: str, src: dict, now: float, fresh: bool) -> dict:
    """The source's shared reading, read again when older than CACHE_S (or `fresh`); its live spans kept."""
    key = f"{proj}:{alias}"
    with _locked():
        rec = (_load(READINGS).get(key) or {})
    if not fresh and rec and now - float(rec.get("at", 0)) < CACHE_S:
        return rec
    state, detail = read_source(src, now)
    with _locked():
        doc = _load(READINGS)
        rec = doc.get(key) or {}
        spans = [s for s in rec.get("spans") or [] if s[1] is None or now - s[1] < HISTORY_S]
        if state in ("live", "unknown"):   # unknown counts as live: never a silent pass
            if not spans or spans[-1][1] is not None:
                spans.append([now, None])
        elif spans and spans[-1][1] is None:
            spans[-1][1] = now
        rec = {"state": state, "detail": detail, "at": now, "spans": spans[-200:],
               "last_live": now if state != "free" else rec.get("last_live"),
               # what the source said at its last live or unknown read (a real clip, or a fail-safe hold)
               "live_state": state if state != "free" else rec.get("live_state"),
               "live_detail": detail if state != "free" else rec.get("live_detail"),
               "unknown_since": (rec.get("unknown_since") or now) if state == "unknown" else None,
               "quiet_s": src.get("quiet_s", QUIET_S)}
        doc[key] = rec
        _save(READINGS, doc)
    return rec


def check(target: str, step: str = "test", now: float | None = None, fresh: bool = False,
          match: Callable[[str, str], bool] | None = None) -> dict:
    """Whether a live realtime stream holds `target` (a machine alias, a resource on it or its host
    name) for `step`. Returns {"target", "step", "state": live|free|unknown, "blocks", "why",
    "clear_at", "at", "sources": [...]}. No source for the box: free, blocks nothing."""
    now = time.time() if now is None else now
    match = match or _matches
    rows = []
    for alias, regs in covered().items():
        if not match(alias, target):
            continue
        for proj, stream, src, role in regs:
            rec = _reading(proj, stream, src, now, fresh)
            quiet = float(src.get("quiet_s", QUIET_S))
            last = rec.get("last_live")
            clear_at = (float(last) + quiet) if last is not None else None
            state = rec["state"]
            if state == "free" and clear_at is not None and now < clear_at:
                state = "quiet"   # ended, but inside its quiet window: still live for every step
            applies = role == "stream" or step in FRONT_STEPS
            rows.append({"project": proj, "alias": alias, "stream": stream, "role": role, "state": state,
                         "read": rec["state"], "detail": rec.get("detail", ""), "at": rec.get("at"),
                         "clear_at": clear_at, "applies": applies})
    holding = [r for r in rows if r["applies"] and r["state"] != "free"]
    out = {"target": target, "step": step, "at": now, "sources": rows, "blocks": bool(holding),
           "state": "free", "why": "", "clear_at": None}
    if not rows:
        out["why"] = "no source registered for this box"
        return out
    if holding:
        out["state"] = "unknown" if all(r["state"] == "unknown" for r in holding) else "live"
        ends = [r["clear_at"] for r in holding if r["state"] == "quiet"]
        out["clear_at"] = max(ends) if ends and len(ends) == len(holding) else None
        out["why"] = why_line(out)
    else:
        out["why"] = "; ".join(f"{r['alias']}: front of {r['stream']}, covered only for deploys and restarts"
                               if not r["applies"] else f"{r['alias']}: no live stream ({r['project']})"
                               for r in rows)
    return out


def _matches(alias: str, target: str) -> bool:
    """`target` is the machine `alias`, a resource on it (`<alias>-x`) or its host name."""
    if not target:
        return False
    if target == alias:
        return True
    try:
        from .machine_ledger import on_machine
        return bool(on_machine(alias)(target))
    except Exception:
        return target.startswith(alias + "-")


def _clock(at: float | None) -> str:
    if at is None:
        return "?"
    from . import timefmt
    try:
        return timefmt.short(float(at), WHERE if WHERE is not None else timefmt.ambient())
    except Exception:
        return time.strftime("%H:%M:%S", time.localtime(at))


def why_line(res: dict) -> str:
    """The one line a refused or waiting step shows (status, exit 75)."""
    parts = []
    for r in res["sources"]:
        if not (r["applies"] and r["state"] != "free"):
            continue
        what = {"live": "a live realtime stream holds it", "quiet": f"its stream ended, quiet until {_clock(r['clear_at'])}",
                "unknown": f"its source could not tell ({r['detail']}): counted as live"}[r["state"]]
        role = f" (front of {r['stream']})" if r["role"] == "front" else ""
        parts.append(f"{r['alias']}{role}: {what}; source of {r['project']}, read {_clock(r['at'])}")
    return (f"stay off for {res['step']}: " + "; ".join(parts)) if parts else ""


def blocks(target: str, step: str = "test", now: float | None = None) -> str:
    """The reason `step` must not run on `target` now, or "" (free, or no source registered).
    Never raises: a broken source counts as live, and an internal error counts as unknown (held) on a
    box that has a registered source; where none is registered it stays free."""
    if not target:
        return ""
    try:
        res = check(target, step, now)
    except Exception as e:   # logged; a box with a source is held, any other box stays free
        _note_error(e)
        return _error_hold(target, step)
    return res["why"] if res["blocks"] else ""


def _error_hold(target: str, step: str) -> str:
    """The unknown-hold reason after an internal error, or "" when no source covers `target` for `step`."""
    try:
        for alias, regs in covered().items():
            if _matches(alias, target) and any(role == "stream" or step in FRONT_STEPS
                                               for _p, _s, _src, role in regs):
                return f"{alias}: occupancy check failed (unknown), stay off for {step} until it reads again"
    except Exception as e:
        _note_error(e)
        return _error_hold_raw(target, step)
    return ""


def _error_hold_raw(target: str, step: str) -> str:
    """Last resort when the registry cannot be parsed by `covered()`: a raw scan of the file."""
    try:
        for reg in (_load(SOURCES).get("projects") or {}).values():
            for alias, src in (reg.get("sources") or {}).items():
                fronts = src.get("fronts") or [] if isinstance(src, dict) else []
                if _matches(alias, target) or (step in FRONT_STEPS and any(_matches(str(f), target) for f in fronts)):
                    return f"{alias}: occupancy check failed (unknown), stay off for {step} until it reads again"
    except Exception:
        pass
    return ""


def blocks_any(targets, step: str = "test", now: float | None = None) -> str:
    for t in targets:
        if why := blocks(t, step, now):
            return why
    return ""


def _note_error(e: Exception) -> None:
    try:
        with open(_path("occupancy-errors.log"), "a") as f:
            f.write(f"{time.time():.0f} {type(e).__name__}: {e}\n")
    except OSError:
        pass


def text(res: dict) -> str:
    """`ttp occupancy` output."""
    head = f"{res['target']} ({res['step']}): {res['state']}" + (" - stay off" if res["blocks"] else " - may run")
    lines = [head]
    if res["why"]:
        lines.append("  " + res["why"])
    for r in res["sources"]:
        clear = f", clear at {_clock(r['clear_at'])}" if r["clear_at"] and r["state"] != "live" else ""
        lines.append(f"  source {r['project']}:{r['stream']} ({r['role']}): {r['read']}, read {_clock(r['at'])}"
                     f"{clear} [{r['detail']}]")
    return "\n".join(lines)


# --- Watchdog -------------------------------------------------------------------------------------

def _live_windows(rec: dict, now: float) -> list[tuple[float, float]]:
    quiet = float(rec.get("quiet_s", QUIET_S))
    return [(float(a), (float(b) if b is not None else now) + quiet) for a, b in rec.get("spans") or []]


def _job_step(s: dict) -> str:
    st = str(s.get("step") or "")
    if st in STEPS:
        return st
    return "reset" if s.get("disruptive") else "test"


EVENTS_KEY = "occupancy_events"   # kv: stream events (box plus window start) already posted (watch)
EVENT_LIST = 10                   # jobs an event's post lists; the rest are "+N more"
DEVICE_STEPS = ("reset",)         # a step that touches the box's devices whatever the lock says


def _uses_devices(cfg: dict, alias: str, s: dict, step: str) -> bool:
    """A span uses `alias`'s own devices: a broker job on it (`machine`), a lock on its device resources
    (checkspill.device_locks_of: `<alias>-device`, its shared device names, device.locks) or on the alias
    itself, or a device or reset step with no resource of another machine. Read from the span's structured
    fields, never its command: a CPU check, a test suite or a host process driving other hosts (ssh, a
    Slurm cluster, docker over ssh) is not device use."""
    from . import checkspill
    if s.get("machine"):
        return s["machine"] == alias
    res = str(s.get("resource") or "")
    if res:
        if res == alias or res in checkspill.device_locks_of(cfg, alias):
            return True
        return step in DEVICE_STEPS and _names_alias(alias, res)
    return step in DEVICE_STEPS or bool(s.get("disruptive"))


def _names_alias(alias: str, res: str) -> bool:
    return res == alias or res.startswith((alias + "-", alias + ":"))


def watch(p, db, now: float | None = None, post: Callable[[str, str], None] | None = None) -> dict:
    """The daemon's model-free watchdog, one pass: publish this project's sources, read every
    registered source (cached), raise one event per own source that cannot tell, and flag each job of
    this project that used a covered box's own devices (_uses_devices) and started while its stream was
    live or inside its quiet window (a front box: deploys and restarts only). A job that started before
    the stream went live is never flagged. Per-job flags are kept in kv for status; one alert per stream
    event (box plus window start) posts at high severity, listing the jobs and the source's own reason."""
    from . import activity
    now = time.time() if now is None else now
    publish(p)
    cov = covered()
    out = {"read": 0, "flags": [], "unknown": []}
    readings: dict[str, dict] = {}
    for alias, regs in cov.items():
        for proj, stream, src, role in regs:
            key = f"{proj}:{stream}"
            if key not in readings:
                readings[key] = _reading(proj, stream, src, now, False)
                out["read"] += 1
                if proj == p.name and readings[key].get("state") == "unknown":
                    out["unknown"].append((stream, readings[key]))
    if not cov:
        return out
    from .machines import load as machines_load
    here = project.hostname()
    known = machines_load()
    cfg = p.config()
    # A span that ran on this host counts as on `alias` only while `alias` is this host.
    local = {a for a in cov if a == here or str((known.get(a) or {}).get("hostname") or "").lower() == here.lower()}
    seen = db.kv(SEEN_KEY, {})
    flags = db.kv(FLAGS_KEY, [])
    events = db.kv(EVENTS_KEY, {})
    new: dict[str, list[dict]] = {}
    recs = [r for r in activity.entries(p.state) if now - float(r.get("start") or r.get("end") or now) < HISTORY_S]
    for s in activity.spans(recs, here):
        sid = str(s.get("id"))
        start = float(s.get("start") or 0)
        end = float(s["end"]) if s.get("end") is not None else now
        for alias, regs in cov.items():
            if not activity.on_machine(alias, s, {here.lower()} if alias in local else set()):
                continue
            step = _job_step(s)
            device = _uses_devices(cfg, alias, s, step)
            for proj, stream, src, role in regs:
                if role == "front":
                    if step not in FRONT_STEPS:
                        continue
                elif not device:
                    continue
                rec = readings.get(f"{proj}:{stream}") or {}
                # Only a job that started while the stream was live (or in its quiet tail) counts.
                hit = next(((a, b) for a, b in _live_windows(rec, now) if a <= start <= b and end >= a), None)
                fkey = f"{sid}:{alias}:{proj}:{stream}"
                if hit is None or fkey in seen:
                    continue
                seen[fkey] = now
                flag = {"key": fkey, "alias": alias, "stream": stream, "role": role, "source": proj,
                        "task": s.get("task"), "kind": s.get("kind"), "step": step,
                        "resource": s.get("resource") or s.get("machine"), "cmd": str(s.get("cmd") or "")[:160],
                        "start": start, "end": s.get("end"), "live_from": hit[0], "quiet_to": hit[1], "at": now,
                        "reason": str(rec.get("live_detail") or rec.get("detail") or ""),
                        "reading": str(rec.get("live_state") or rec.get("state") or "")}
                flags.append(flag)
                out["flags"].append(flag)
                new.setdefault(f"{alias}:{proj}:{stream}:{int(hit[0])}", []).append(flag)
    if post:
        for ekey, fl in new.items():
            if ekey not in events:
                post(f"occupancy:violation:{ekey}", event_text(fl))
    for ekey in new:
        events[ekey] = now
    db.set_kv(SEEN_KEY, {k: v for k, v in seen.items() if now - float(v) < HISTORY_S})
    db.set_kv(EVENTS_KEY, {k: v for k, v in events.items() if now - float(v) < HISTORY_S})
    db.set_kv(FLAGS_KEY, flags[-FLAGS_KEPT:])
    return out


def _job_line(f: dict) -> str:
    end = _clock(f["end"]) if f.get("end") is not None else "still running"
    return (f"task #{f.get('task') or '?'} {f['kind']} ({f['step']}) on {f.get('resource') or f['alias']}, "
            f"{_clock(f['start'])}-{end}")


def event_text(fl: list[dict]) -> str:
    """One alert for a stream event: the box and window, the source's own reason at the hit (a real clip or
    a fail-safe hold with no owner shown), the first EVENT_LIST jobs and "+N more"."""
    f = fl[0]
    role = f" (front of {f['stream']})" if f["role"] == "front" else ""
    why = f.get("reason") or "no reason given"
    shown = "; ".join(_job_line(x) for x in fl[:EVENT_LIST])
    more = f"; +{len(fl) - EVENT_LIST} more" if len(fl) > EVENT_LIST else ""
    return (f"{len(fl)} job(s) of this project used {f['alias']}{role} while a live realtime stream held it: the "
            f"stream was live from {_clock(f['live_from'])} and quiet until {_clock(f['quiet_to'])} (source of "
            f"{f['source']}, {f.get('reading') or 'live'}: {why}). Jobs: {shown}{more}. "
            f"Every deploy, restart, test, benchmark, warm-up, health check or reset there must ask "
            f"`ttp occupancy {f['alias']}` first and stay off while it says so.")


def flag_text(f: dict) -> str:
    """One job's flag as an alert (event_text of a single job)."""
    return event_text([f])


def recent_flags(db, since_s: float = 86400, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return [f for f in db.kv(FLAGS_KEY, []) if now - float(f.get("at", 0)) < since_s]


def status_lines(db, now: float | None = None) -> list[str]:
    """`ttp status` and the daily review: the last day's flagged overlaps."""
    fl = recent_flags(db, 86400, now)
    if not fl:
        return []
    return [f"Occupancy: {len(fl)} job(s) here ran on a box while a live stream held it in 24 h:"] + \
           [f"  {f['alias']}: task #{f.get('task') or '?'} {f['step']} at {_clock(f['start'])} "
            f"(source of {f['source']})" for f in fl[-5:]]


# --- Stop switch for long runners (bench, warm-up, soak): checked before each trial ---------------------

def _stop_name(name: str) -> str:
    name = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
        raise ValueError(f"runner name {name!r}: letters, digits, . _ - only")
    return name


def stop_file(p: Any, name: str) -> Path:
    """The file whose presence asks runner `name` of project `p` to stop before its next trial."""
    return Path(p.state) / "runner-stops" / _stop_name(name)


def request_stop(p: Any, name: str, why: str = "") -> Path:
    f = stop_file(p, name)
    f.parent.mkdir(parents=True, exist_ok=True)
    project.durable_write(f, json.dumps({"at": time.time(), "why": why or "stop requested"}) + "\n")
    return f


def clear_stop(p: Any, name: str) -> bool:
    try:
        stop_file(p, name).unlink()
        return True
    except FileNotFoundError:
        return False


def should_stop(p: Any, name: str, box: str = "", step: str = "bench", now: float | None = None) -> str:
    """Why runner `name` must stop before its next trial: its stop switch is set, or a live realtime stream
    holds `box` for `step` (the occupancy check); "" to go on."""
    f = stop_file(p, name)
    if f.exists():
        try:
            why = json.loads(f.read_text()).get("why") or "stop requested"
        except (OSError, ValueError, AttributeError):
            why = "stop requested"
        return f"stop switch set ({why}); clear it with `ttp occupancy --clear-stop {name}`"
    return blocks(box, step, now) if box else ""
