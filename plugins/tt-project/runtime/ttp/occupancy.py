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
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import project

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
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True))
    os.replace(tmp, path)


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
    """{project: {"sources", "quiet_s", "base"}} of every project on the host that registered one."""
    return {k: v for k, v in (_load(SOURCES).get("projects") or {}).items() if isinstance(v, dict)}


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
    try:
        # its own process group, so a timeout ends the probe and every child it started
        proc = subprocess.Popen(src["cmd"], shell=True, cwd=cwd if cwd and os.path.isdir(cwd) else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return "unknown", f"could not run: {e}"
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, 9)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        return "unknown", f"timed out after {limit:g} s"
    tail = (out or err or "").strip().splitlines()[-1:] or [""]
    state = {0: "live", 1: "free"}.get(proc.returncode, "unknown")
    return state, f"exit {proc.returncode}" + (f": {tail[0][:120]}" if tail[0] else "")


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
    Never raises: a broken registry blocks nothing it cannot read, a broken source counts as live."""
    if not target:
        return ""
    try:
        res = check(target, step, now)
    except Exception as e:   # a bug here must never hold the whole harness; the watchdog still flags
        _note_error(e)
        return ""
    return res["why"] if res["blocks"] else ""


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


def watch(p, db, now: float | None = None, post: Callable[[str, str], None] | None = None) -> dict:
    """The daemon's model-free watchdog, one pass: publish this project's sources, read every
    registered source (cached), raise one event per own source that cannot tell, and flag each job of
    this project that ran on a covered box while its stream was live or inside its quiet window (a
    front box: deploys and restarts only). A flag posts once, at high severity, and is kept in kv."""
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
    # A span that ran on this host counts as on `alias` only while `alias` is this host.
    local = {a for a in cov if a == here or str((known.get(a) or {}).get("hostname") or "").lower() == here.lower()}
    seen = db.kv(SEEN_KEY, {})
    flags = db.kv(FLAGS_KEY, [])
    recs = [r for r in activity.entries(p.state) if now - float(r.get("start") or r.get("end") or now) < HISTORY_S]
    for s in activity.spans(recs, here):
        sid = str(s.get("id"))
        start = float(s.get("start") or 0)
        end = float(s["end"]) if s.get("end") is not None else now
        for alias, regs in cov.items():
            if not activity.on_machine(alias, s, {here.lower()} if alias in local else set()):
                continue
            step = _job_step(s)
            for proj, stream, src, role in regs:
                if role == "front" and step not in FRONT_STEPS:
                    continue
                rec = readings.get(f"{proj}:{stream}") or {}
                hit = next(((a, b) for a, b in _live_windows(rec, now) if start <= b and end >= a), None)
                fkey = f"{sid}:{alias}:{proj}:{stream}"
                if hit is None or fkey in seen:
                    continue
                seen[fkey] = now
                flag = {"key": fkey, "alias": alias, "stream": stream, "role": role, "source": proj,
                        "task": s.get("task"), "kind": s.get("kind"), "step": step,
                        "resource": s.get("resource") or s.get("machine"), "cmd": str(s.get("cmd") or "")[:160],
                        "start": start, "end": s.get("end"), "live_from": hit[0], "quiet_to": hit[1], "at": now}
                flags.append(flag)
                out["flags"].append(flag)
                if post:
                    post(f"occupancy:violation:{fkey}", flag_text(flag))
    db.set_kv(SEEN_KEY, {k: v for k, v in seen.items() if now - float(v) < HISTORY_S})
    db.set_kv(FLAGS_KEY, flags[-FLAGS_KEPT:])
    return out


def flag_text(f: dict) -> str:
    role = f" (front of {f['stream']})" if f["role"] == "front" else ""
    end = _clock(f["end"]) if f.get("end") is not None else "still running"
    return (f"A job of this project ran on {f['alias']}{role} while a live realtime stream held it: task "
            f"#{f.get('task') or '?'} {f['kind']} ({f['step']}) on {f.get('resource') or f['alias']}, "
            f"{_clock(f['start'])}-{end}; the stream was live from {_clock(f['live_from'])} and quiet until "
            f"{_clock(f['quiet_to'])} (source of {f['source']}). Command: {f['cmd'] or '?'}. "
            f"Every deploy, restart, test, benchmark, warm-up, health check or reset there must ask "
            f"`ttp occupancy {f['alias']}` first and stay off while it says so.")


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
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": time.time(), "why": why or "stop requested"}) + "\n")
    os.replace(tmp, f)
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
