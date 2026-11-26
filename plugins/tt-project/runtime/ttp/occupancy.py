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
with this project's own jobs on the box (activity.py): an overlap posts at once at high severity. An
unknown read holds the box but charges no job: an overlap with it is one low line per source.

New starts wait or move (wait_free, wait_here): `ttp lock`, `ttp detach`, checks, Terminal-Bench trials
and harness A/B cells look again every POLL_S until the box is free, or start on an allowed free host.
Jobs already running on this host while a stream holds it are lowered, never signalled (lower_running).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import boundedchild, project
from .util import flocked, read_dict

SOURCES = "occupancy-sources.json"
READINGS = "occupancy-readings.json"
LOCK = "occupancy.lock"
QUIET_S = 60.0            # default quiet window after a stream's last live reading
TIMEOUT_S = 10.0          # default source timeout
FILE_MAX_AGE_S = 120.0    # a file source older than this is unknown
CACHE_S = 20.0            # a source is read at most this often, across every project on the host
GATE_MARK_ENV = "TTP_GATE_MARK"   # `ttp lock` sets it to a path a start gate touches when it refuses the command
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


def _locked():
    return flocked(_path(LOCK))


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
    """A project's own registrations from its settings: {"sources": {alias: source}, "quiet_s", "alerts"}.
    `alerts` (on by default) is the watchdog's post switch: off, flags are still kept for status and the daily
    review, but none is posted and no event raised; the pre-step check is the same either way."""
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
    return {"sources": out, "quiet_s": _num(sec.get("quiet_s"), QUIET_S), "alerts": alerts_on(sec.get("alerts"))}


def alerts_on(v: Any) -> bool:
    """`occupancy.alerts`: on unless set to false, off, no or 0."""
    return v is None or str(v).strip().lower() not in ("false", "off", "no", "0")


def publish(p, cfg: dict | None = None) -> dict:
    """Write this project's registrations into the shared registry (removed when it has none)."""
    mine = settings(p.config() if cfg is None else cfg)
    with _locked():
        doc = read_dict(_path(SOURCES))
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
    return {k: v for k, v in (read_dict(_path(SOURCES)).get("projects") or {}).items()
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


def _step_spans(spans, on: bool, now: float) -> list:
    """`spans` ([start, end or None]) after one read: an open span while `on`, closed at the first read off."""
    spans = [s for s in spans or [] if s[1] is None or now - s[1] < HISTORY_S]
    if on:
        if not spans or spans[-1][1] is not None:
            spans.append([now, None])
    elif spans and spans[-1][1] is None:
        spans[-1][1] = now
    return spans[-200:]


def _live_spans(rec: dict) -> list:
    """The spans the source read live: an unknown read holds the box (spans) but never charges a job to a
    stream. A record written before live spans were kept counts its spans only when its last hold was live."""
    if "live_spans" in rec:
        return rec.get("live_spans") or []
    return (rec.get("spans") or []) if rec.get("live_state") == "live" else []


def mark_refused() -> None:
    """Tell the enclosing `ttp lock` a start gate refused its command before anything ran, so its span is no job
    of a stream. Generic: any wrapper's own gate may `touch "$TTP_GATE_MARK"` before it exits 75."""
    if path := os.environ.get(GATE_MARK_ENV):
        try:
            Path(path).touch()
        except OSError:
            pass


def _held_until(rec: dict) -> float | None:
    """When the source's hold ended: its last live or unknown read, or a closed span's end (the first read off,
    up to CACHE_S later), so the gate's quiet window ends where the watchdog's does."""
    ends = [float(b) for _a, b in rec.get("spans") or [] if b is not None]
    ends += [float(rec["last_live"])] if rec.get("last_live") is not None else []
    return max(ends) if ends else None


def _reading(proj: str, alias: str, src: dict, now: float, fresh: bool) -> dict:
    """The source's shared reading, read again when older than CACHE_S (or `fresh`); its live spans kept."""
    key = f"{proj}:{alias}"
    with _locked():
        rec = (read_dict(_path(READINGS)).get(key) or {})
    if not fresh and rec and now - float(rec.get("at", 0)) < CACHE_S:
        return rec
    state, detail = read_source(src, now)
    with _locked():
        doc = read_dict(_path(READINGS))
        rec = doc.get(key) or {}
        spans = _step_spans(rec.get("spans"), state in ("live", "unknown"), now)   # unknown holds: never a silent pass
        live = _step_spans(_live_spans(rec), state == "live", now)   # what the watchdog charges jobs to
        rec = {"state": state, "detail": detail, "at": now, "spans": spans, "live_spans": live,
               "real_detail": detail if state == "live" else rec.get("real_detail"),
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
            held = _held_until(rec)
            clear_at = (held + quiet) if held is not None else None
            state = rec["state"]
            if state == "free" and clear_at is not None and now < clear_at:
                state = "quiet"   # ended, but inside its quiet window: still live for every step
            applies = role == "stream" or step in FRONT_STEPS
            rows.append({"project": proj, "alias": alias, "stream": stream, "role": role, "state": state,
                         "read": rec["state"], "detail": rec.get("detail", ""), "at": rec.get("at"),
                         "clear_at": clear_at, "applies": applies,
                         "last_live": last, "last_state": rec.get("live_state")})
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
        quiet = f"quiet until {_clock(r['clear_at'])}"
        ended = (f"unknown (its source could not tell at {_clock(r['last_live'])}), {quiet}"
                 if r.get("last_state") == "unknown" else f"its stream ended, {quiet}")
        what = {"live": "a live realtime stream holds it", "quiet": ended,
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
        for reg in (read_dict(_path(SOURCES)).get("projects") or {}).values():
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


POLL_S = 15.0   # a waiting start looks again about this often (readings are shared, CACHE_S)


def wait_free(targets, step: str = "test", *, moves=(), poll_s: float | None = None, max_wait_s: float | None = None,
              say: Callable[[str], None] | None = None, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.time, on_wait: Callable[[], None] | None = None) -> dict:
    """The one gate every new start on a box asks first (`ttp lock`, `ttp detach`, checks, bench trials).
    While a live stream holds any of `targets` for `step` (its quiet window included; a source that cannot
    tell counts as live), the start moves to the first of `moves` (each an alias or a list of names for one
    other host) that is free, else waits, looking again about every `poll_s`, and says each new reason
    once. Never raises. Returns {"ok": bool, "move": <the free move's first name> or "", "waited_s",
    "why": the last hold}; ok False only after `max_wait_s`. `on_wait` is called once, before the first
    sleep of a real wait (a caller frees what it holds while it waits, such as a shared lock)."""
    from . import poll_s as _poll
    poll_s = _poll(POLL_S) if poll_s is None else poll_s
    targets = [str(t) for t in targets if t]
    t0, said, why = clock(), "", ""
    while True:
        try:
            why = blocks_any(targets, step, clock())
            if not why:
                return {"ok": True, "move": "", "waited_s": clock() - t0, "why": ""}
            for m in moves:
                names = [m] if isinstance(m, str) else [str(x) for x in m if x]
                if names and not blocks_any(names, step, clock()):
                    if say:
                        say(f"moved to {names[0]}: {why}")
                    return {"ok": True, "move": names[0], "waited_s": clock() - t0, "why": why}
        except Exception as e:   # never a silent pass: wait as for a source that cannot tell
            _note_error(e)
            why = why or f"occupancy check failed ({type(e).__name__}): counted as live"
        if max_wait_s is not None and clock() - t0 >= max_wait_s:
            return {"ok": False, "move": "", "waited_s": clock() - t0, "why": why}
        if say and (same := re.sub(r", read \d\d:\d\d[^;]*", "", why)) != said:   # a new reason, not a new read time
            say(f"waiting: {why}; looking again every {poll_s:.0f} s")
            said = same
        if on_wait:
            try:
                on_wait()
            except Exception as e:   # never stops the wait
                _note_error(e)
            on_wait = None
        sleep(poll_s if max_wait_s is None else max(0.0, min(poll_s, max_wait_s - (clock() - t0))))


def wait_here(step: str = "bench", who: str = "", **kw) -> dict:
    """wait_free for a start on this host (a bench trial or cell, whose box is the host it runs on); the
    wait line goes to stderr, prefixed with `who`."""
    from . import project
    kw.setdefault("say", lambda line: print(f"{who or 'ttp'}: {line}", file=sys.stderr, flush=True))
    return wait_free([project.hostname()], step, **kw)


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

def _live_windows(rec: dict, now: float, held: bool = False) -> list[tuple[float, float]]:
    """The source's live spans plus its quiet window; `held`: every span that held the box, unknown too."""
    quiet = float(rec.get("quiet_s", QUIET_S))
    spans = (rec.get("spans") or []) if held else _live_spans(rec)
    return [(float(a), (float(b) if b is not None else now) + quiet) for a, b in spans]


def _open_window(rec: dict, hit: tuple[float, float]) -> bool:
    """The stream of window `hit` has not ended yet (its span has no end): its end is not known."""
    return any(b is None and abs(float(a) - hit[0]) < 1e-6 for a, b in _live_spans(rec))


def _job_step(s: dict) -> str:
    st = str(s.get("step") or "")
    if st in STEPS:
        return st
    return "reset" if s.get("disruptive") else "test"


EVENTS_KEY = "occupancy_events"   # kv: stream events (box plus window start) already posted (watch)
UNKNOWN_KEY = "occupancy_unknown_hits"   # kv: sources whose could-not-tell line was posted (watch), per source
EVENT_LIST = 10                   # jobs an event's post lists; the rest are "+N more"
DEVICE_STEPS = ("reset",)         # a step that touches the box's devices whatever the lock says
REMOTE_DEVICE_STEPS = ("bench", "warmup", "reset")   # a remote job's declared step that runs on the box's devices


def _uses_devices(cfg: dict, alias: str, s: dict, step: str) -> bool:
    """A span uses `alias`'s own devices: a lock on its device resources (checkspill.device_locks_of:
    `<alias>-device`, its shared device names, device.locks) or on the alias itself, a remote job on it that
    declared device use (`ttp detach --remote --devices N` or a bench, warm-up or reset step), or a device or
    reset step with no resource of another machine. Read from the span's structured fields, never its command:
    a CPU check, a test suite, a lock waiter, a log read over ssh or a host process driving other hosts (ssh, a
    Slurm cluster, docker over ssh) is not device use, nor is any process that merely runs on the box's host."""
    from . import checkspill, responsibilities
    if s.get("machine"):
        if s["machine"] != alias:
            return False
        if not s.get("resource"):
            return bool(s.get("disruptive")) or str(s.get("step") or "") in REMOTE_DEVICE_STEPS \
                or (str(s.get("devices") or "0").isdigit() and int(s.get("devices") or 0) > 0)
    res = str(s.get("resource") or "")
    if res:
        if res == alias or res in checkspill.device_locks_of(cfg, alias):
            return True
        return step in DEVICE_STEPS and responsibilities._names(alias, res)
    return step in DEVICE_STEPS or bool(s.get("disruptive"))


def _box(alias: str, known: dict) -> str:
    """The box an alias names: its registered hostname (`ttp machines`), else the alias, lower case. Two
    aliases of one box (an ssh alias and the box's own name) are one box, so a job there is flagged once."""
    return str((known.get(alias) or {}).get("hostname") or alias).lower()


def _local_aliases(aliases, known: dict, here: str) -> list[str]:
    """The covered aliases that are this host (by alias or by registered hostname)."""
    return [a for a in aliases if a == here or str((known.get(a) or {}).get("hostname") or "").lower() == here.lower()]


def watch(p, db, now: float | None = None, post: Callable[..., None] | None = None) -> dict:
    """The daemon's model-free watchdog, one pass: publish this project's sources, read every
    registered source (cached), raise one event per own source that cannot tell, and flag each job of
    this project that used a covered box's own devices (_uses_devices) and started while its stream was
    live or inside its quiet window (a front box: deploys and restarts only). A job that started before
    the stream went live is never flagged, nor is a record whose process is gone (activity.reap closes those
    first, here on every pass); a job whose liveness is unknown (a remote one) counts by its start alone.
    Only a live reading charges a job: a job that started only while the source could not tell (an unknown
    read: a timeout or an odd exit, a fail-safe hold that still keeps new work off the box) is not flagged and
    raises no stay-off alert, only one low line per source (`occupancy-source-unknown:<project>:<stream>`,
    once per HISTORY_S) saying the source could not tell; the live part of a mixed span still counts.
    Per-job flags are kept in kv for status; one alert per stream event (box plus window, a later hit
    overlapping it joins it; kept across restarts) lists the jobs and the source's own reason, at high
    severity. It closes once its jobs ended, or once the window ended and none is known
    to still run there (_close_ended). Aliases of one box (_box) count as one box: a job is flagged and alerted
    once, whichever alias names it. With `occupancy.alerts` off nothing is posted and no event is opened; the
    flags are still kept."""
    from . import activity
    now = time.time() if now is None else now
    publish(p)
    cov = covered()
    out = {"read": 0, "flags": [], "unknown": [], "unknown_hits": []}
    here = project.hostname()
    reaped = activity.reap(p.state, here=here, now=now)   # also with no source: a gone process never reads live
    if reaped:
        out["reaped"] = reaped
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
    known = machines_load()
    cfg = p.config()
    alerting = settings(cfg)["alerts"]
    boxes = {a: _box(a, known) for a in cov}
    # A span that ran on this host counts as on `alias` only while `alias` is this host.
    local = set(_local_aliases(cov, known, here))
    seen = db.kv(SEEN_KEY, {})
    flags = db.kv(FLAGS_KEY, [])
    events = db.kv(EVENTS_KEY, {})
    told = db.kv(UNKNOWN_KEY, {})
    new: dict[str, list[dict]] = {}
    fresh: set[str] = set()   # events whose alert this pass posts
    recs = [r for r in activity.entries(p.state) if now - float(r.get("start") or r.get("end") or now) < HISTORY_S]
    jobs: dict[str, str] = {}   # span id -> running, ended or unknown (a remote job's end is known only there)
    for s in activity.spans(recs, here):
        sid = str(s.get("id"))
        jobs[sid] = ("ended" if s.get("reaped") or s.get("end") is not None
                     else "unknown" if s.get("state") == "unknown" else "running")
        if s.get("reaped") or s.get("gate_refused"):
            continue   # its process is gone, or a gate refused it before anything ran: not a job of the stream
        start = float(s.get("start") or 0)
        # A remote job's end is known only on its host: it is unknown here, so it counts only by its start.
        end = start if s.get("state") == "unknown" else float(s["end"]) if s.get("end") is not None else now
        done: set[tuple[str, str, str]] = set()   # (box, source project, role) this span was counted against
        for alias, regs in sorted(cov.items()):
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
                inside = lambda wins: next(((a, b) for a, b in wins if a <= start <= b and end >= a), None)   # noqa: E731
                hit = inside(_live_windows(rec, now))
                fkey = f"{sid}:{alias}:{proj}:{stream}"
                bkey = f"{sid}:box={boxes[alias]}:{proj}:{role}"   # the same job on another alias of this box
                if hit is None:
                    ukey = f"occupancy-source-unknown:{proj}:{stream}"
                    if ukey not in told and inside(_live_windows(rec, now, held=True)):
                        told[ukey] = now   # held only by a could-not-tell read: no violation, one low line
                        line = {"key": ukey, "alias": alias, "stream": stream, "source": proj, "task": s.get("task"),
                                "detail": str(rec.get("live_detail") or rec.get("detail") or "")}
                        out["unknown_hits"].append(line)
                        if post and alerting:
                            post(ukey, unknown_text(line), "low")
                    continue
                if fkey in seen or bkey in seen or (boxes[alias], proj, role) in done:
                    continue
                done.add((boxes[alias], proj, role))
                seen[fkey] = seen[bkey] = now
                flag = {"key": fkey, "alias": alias, "stream": stream, "role": role, "source": proj,
                        "task": s.get("task"), "kind": s.get("kind"), "step": step,
                        "resource": s.get("resource") or s.get("machine"), "cmd": str(s.get("cmd") or "")[:160],
                        "start": start, "end": s.get("end"), "live_from": hit[0], "quiet_to": hit[1], "at": now,
                        "open": _open_window(rec, hit), "quiet_s": float(rec.get("quiet_s", QUIET_S)),
                        "reason": str(rec.get("real_detail") or rec.get("live_detail") or rec.get("detail") or ""),
                        "reading": "live"}
                flags.append(flag)
                out["flags"].append(flag)
                if not alerting:
                    continue   # alerts off: kept for status and the daily review, no event opened or posted
                ekey = _event_for(events, alias, proj, stream, hit, now, boxes)
                ev = events[ekey]
                ev["jobs"] = sorted(set(ev.get("jobs") or []) | {sid})
                ev["at"] = now
                if ev.pop("closed", None) is not None:
                    fresh.add(ekey)   # a job started in the window after its alert closed: told again
                new.setdefault(ekey, []).append(flag)
    if post and alerting:
        for ekey, fl in new.items():
            if ekey in fresh:
                post(f"occupancy:violation:{ekey}", event_text(fl), "high")
    _close_ended(db, events, jobs, readings, now, set(new))
    db.set_kv(SEEN_KEY, {k: v for k, v in seen.items() if now - float(v) < HISTORY_S})
    db.set_kv(EVENTS_KEY, {k: v for k, v in events.items() if now - _event_at(v) < HISTORY_S})
    db.set_kv(UNKNOWN_KEY, {k: v for k, v in told.items() if now - float(v) < HISTORY_S})
    db.set_kv(FLAGS_KEY, flags[-FLAGS_KEPT:])
    return out


def _event_at(v) -> float:
    return float(v.get("at") or 0) if isinstance(v, dict) else float(v)


def _event_for(events: dict, alias: str, proj: str, stream: str, hit: tuple[float, float], now: float,
               boxes: dict[str, str] | None = None) -> str:
    """The stream event (box and window) a job's hit joins: an event of the same box (any alias of it, _box)
    and source project whose window
    overlaps the hit (a window whose start moved, a stream back live inside its quiet tail), else a new one,
    marked closed so the caller reopens and posts it. A record from before events kept their window (a bare
    time) joins only by its exact key and is not posted again."""
    base = f"{alias}:{proj}:{stream}"
    boxes = boxes or {}
    box = boxes.get(alias, alias)
    for k, v in events.items():
        if isinstance(v, dict) and (v.get("base") == [alias, proj, stream]
                                    or (v.get("box") == box and list(v.get("base") or [])[1:2] == [proj])) \
                and float(v["from"]) <= hit[1] and hit[0] <= float(v["to"]):
            v["from"], v["to"] = min(float(v["from"]), hit[0]), max(float(v["to"]), hit[1])
            return k
    ekey = f"{base}:{int(hit[0])}"
    old = events.get(ekey)
    events[ekey] = {"base": [alias, proj, stream], "box": box, "from": hit[0], "to": hit[1], "jobs": [], "at": now}
    if old is None:
        events[ekey]["closed"] = now   # reopened at once by the caller, so its alert is posted
    return ekey


def _close_ended(db, events: dict, jobs: dict[str, str], readings: dict, now: float, joined: set[str]) -> None:
    """Close each open stay-off alert whose condition is over (alerts clear with their
    condition): every job it flagged ended (or its record is gone), or the stream's window ended and none of
    them is known to still run there (a remote job's end is known only on its host). An event a job joined
    this pass waits for the next one."""
    from . import alerts
    for ekey, ev in events.items():
        if not isinstance(ev, dict) or ev.get("closed") is not None or not ev.get("jobs") or ekey in joined:
            continue
        states = {jobs.get(j, "ended") for j in ev["jobs"]}
        if "running" in states:
            continue
        alias, proj, stream = ev["base"]
        if states != {"ended"}:
            if any(a <= now <= b for a, b in _live_windows(readings.get(f"{proj}:{stream}") or {}, now)):
                continue
            why = "the stream's window ended and no job it flagged is known to run there"
        else:
            why = "every job it flagged ended"
        ev["closed"] = now
        alerts.close_key(db, f"occupancy:violation:{ekey}", why, now,
                         text=f"Cleared: the stay-off alert for {alias} ({stream}): {why}.")


def _job_line(f: dict) -> str:
    end = _clock(f["end"]) if f.get("end") is not None else "still running"
    return (f"task #{f.get('task') or '?'} {f['kind']} ({f['step']}) on {f.get('resource') or f['alias']}, "
            f"{_clock(f['start'])}-{end}")


def _window_words(fl: list[dict]) -> str:
    """When the stream held the box: a stream still running has no known end, so its clear time is never
    given as a clock time ("quiet until" while it was still live was false)."""
    f = fl[0]
    if any(x.get("open") for x in fl):
        q = float(f.get("quiet_s", QUIET_S))
        after = ("once it ends" if q <= 0 else "about 1 minute after it ends" if q <= 90
                 else f"about {round(q / 60)} minutes after it ends")
        return f"the stream is live since {_clock(f['live_from'])} and still running; the box clears {after}"
    return f"the stream was live from {_clock(f['live_from'])} and the box was quiet until {_clock(f['quiet_to'])}"


def event_text(fl: list[dict]) -> str:
    """One alert for a stream event: the box and window, the source's own reason at its last live read, the
    first EVENT_LIST jobs and "+N more"."""
    f = fl[0]
    role = f" (front of {f['stream']})" if f["role"] == "front" else ""
    why = f.get("reason") or "no reason given"
    shown = "; ".join(_job_line(x) for x in fl[:EVENT_LIST])
    more = f"; +{len(fl) - EVENT_LIST} more" if len(fl) > EVENT_LIST else ""
    return (f"{len(fl)} job(s) of this project used {f['alias']}{role} while a live realtime stream held it: "
            f"{_window_words(fl)} (source of {f['source']}, {f.get('reading') or 'live'}: {why}). Jobs: {shown}{more}. "
            f"Every deploy, restart, test, benchmark, warm-up, health check or reset there must ask "
            f"`ttp occupancy {f['alias']}` first and stay off while it says so.")


def unknown_text(line: dict) -> str:
    """The one low line for a job that ran while only a could-not-tell read held the box."""
    return (f"Task #{line.get('task') or '?'} ran on {line['alias']} while the occupancy source of {line['source']} "
            f"for {line['stream']} could not tell whether a live stream held it ({line['detail'] or 'no answer'}). "
            f"The box counted as held, but no live stream was reported: no stay-off alert.")


def recent_flags(db, since_s: float = 86400, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return [f for f in db.kv(FLAGS_KEY, []) if now - float(f.get("at", 0)) < since_s]


def _alerts_here() -> bool:
    """`occupancy.alerts` of the project WHERE names (on when there is none or it cannot be read)."""
    try:
        sec = WHERE.config().get("occupancy") if callable(getattr(WHERE, "config", None)) else None
        return alerts_on(sec.get("alerts") if isinstance(sec, dict) else None)
    except Exception:
        return True


def status_lines(db, now: float | None = None) -> list[str]:
    """`ttp status` and the daily review: the last day's flagged overlaps."""
    fl = recent_flags(db, 86400, now)
    if not fl:
        return []
    paused = "" if _alerts_here() else " (alerts paused: occupancy.alerts is off)"
    return [f"Occupancy: {len(fl)} job(s) here ran on a box while a live stream held it in 24 h{paused}:"] + \
           [f"  {f['alias']}: task #{f.get('task') or '?'} {f['step']} at {_clock(f['start'])} "
            f"(source of {f['source']})" for f in fl[-5:]]



# --- Jobs already running while a stream is live: lowered, never killed ----------------------------------

LOWERED_KEY = "occupancy_lowered"   # kv: this project's job groups lowered, with what restores each
LOW_NICE = 19


def _io_get(pid: int) -> str:
    """The process's I/O class and level as ionice arguments ("2 4", "0 0"), or "" when unknown."""
    r = boundedchild.run(["ionice", "-p", str(pid)], timeout=5)
    out = str(r.out or "").strip() if r.rc == 0 else ""
    m = re.match(r"(none|realtime|best-effort|idle)(?:: prio (\d+))?", out)
    if not m:
        return ""
    return f"{('none', 'realtime', 'best-effort', 'idle').index(m.group(1))} {m.group(2) or 0}"


def _io_set(pgid: int, cls_level: str) -> str:
    """Set the group's I/O class ("3 0" idle); "" when done, else why not."""
    cls, level = (cls_level.split() + ["0"])[:2]
    args = ["ionice", "-c", cls] + (["-n", level] if cls in ("1", "2") else []) + ["-P", str(pgid)]
    r = boundedchild.run(args, timeout=5)
    return "" if r.rc == 0 else (str(r.err or "").strip()[-200:] or r.error or f"ionice exit {r.rc}")


def _autogroup_get(pid: int) -> int | None:
    """The session's scheduler autogroup nice (Linux, autogroup on), or None."""
    try:
        if Path("/proc/sys/kernel/sched_autogroup_enabled").read_text().strip() != "1":
            return None
        m = re.search(r"nice (-?\d+)", Path(f"/proc/{pid}/autogroup").read_text())
        return int(m.group(1)) if m else None
    except OSError:
        return None


def _autogroup_set(pid: int, nice: int) -> str:
    """Set the session's autogroup nice; the kernel allows any non-negative value without privilege, so a
    restore always works (it takes one change per 100 ms across the host: retried). "" when done."""
    err = ""
    for _ in range(10):
        try:
            with open(f"/proc/{pid}/autogroup", "w") as f:
                f.write(str(nice))
            return ""
        except BlockingIOError:
            err = "busy"
            time.sleep(0.15)
        except OSError as e:
            return e.strerror or str(e)
    return err


def _nice_get(pgid: int) -> int:
    return os.getpriority(os.PRIO_PGRP, pgid)


def _nice_set(pgid: int, nice: int) -> str:
    try:
        os.setpriority(os.PRIO_PGRP, pgid, nice)
        return ""
    except OSError as e:
        return e.strerror or str(e)


def _job_group(s: dict) -> int | None:
    """The live process group of a running detached job of this host (its wrapper leads its own session),
    from its run's detached.json; None when unknown, gone or its pid was reused."""
    from . import locks
    rc = str(s.get("rcfile") or "")
    if not rc:
        return None
    try:
        jobs = json.loads((Path(rc).parent / "detached.json").read_text())
    except (OSError, ValueError):
        return None
    for j in jobs if isinstance(jobs, list) else []:
        if not isinstance(j, dict) or str(j.get("rc") or "") != rc or not j.get("pid"):
            continue
        pid = int(j["pid"])
        try:
            if os.getsid(pid) != pid or os.getpgid(pid) != pid:
                return None
        except OSError:
            return None
        if j.get("pid_start") is not None and locks.start_time(pid) != j["pid_start"]:
            return None   # another process now has its pid
        return pid
    return None


def lower_running(p, db, now: float | None = None) -> dict:
    """The daemon's pass for this project's jobs already running on this host: while a live stream holds a
    box on this host (its quiet window included; unknown counts as live), each running detached job's
    process group drops to the lowest priority (autogroup nice 19 where the kernel groups by session, else
    nice 19; I/O class idle), and once the box is free again it goes back to what it had. No signal is ever
    sent: a job is never killed or stopped. Each change and each restore goes to the activity log
    (ev "lowered" / "restored"). A nice the kernel lets only a privileged process raise again stays low,
    and the restore record says so."""
    from . import activity
    now = time.time() if now is None else now
    here = project.hostname()
    lowered = db.kv(LOWERED_KEY, {})
    out: dict[str, list] = {"lowered": [], "restored": []}
    try:
        from .machines import load as machines_load
        known = machines_load()
    except Exception:
        known = {}
    local = _local_aliases(covered(), known, here)
    why = blocks_any(local, "test", now) if local else ""
    running = {}
    if why:
        recs = [r for r in activity.entries(p.state) if r.get("host") == here]
        for s in activity.spans(recs, here):
            if s["kind"] == "job" and s.get("state") == "running" and not s.get("machine"):
                running[str(s["id"])] = s
    for sid, s in running.items():
        if sid in lowered:
            continue
        pgid = _job_group(s)
        if pgid is None:
            continue
        from .locks import start_time
        before = {"pgid": pgid, "nice": _nice_get(pgid), "io": _io_get(pgid), "autogroup": _autogroup_get(pgid),
                  "pid_start": start_time(pgid)}
        errs = [e for e in ((_autogroup_set(pgid, LOW_NICE) if before["autogroup"] is not None
                             else _nice_set(pgid, LOW_NICE)), _io_set(pgid, "3 0")) if e]
        lowered[sid] = {**before, "at": now, "task": s.get("task"), "job": s.get("job")}
        activity.append(p.state, activity.base(p.name, ev="lowered", id=sid, at=now, pgid=pgid, why=why[:300],
                                               before=before, **({"errors": errs} if errs else {})))
        out["lowered"].append(sid)
    for sid in [k for k in lowered if k not in running]:
        # The box is free (or the job ended): what the job had comes back.
        rec = lowered.pop(sid)
        pgid = int(rec["pgid"])
        from .locks import start_time
        try:
            alive = os.getpgid(pgid) == pgid and start_time(pgid) == rec.get("pid_start")
        except OSError:
            alive = False   # ended, or its pid is another process's now: nothing to restore
        errs = []
        if alive:
            if rec.get("autogroup") is not None:
                errs.append(_autogroup_set(pgid, int(rec["autogroup"])))
            else:
                e = _nice_set(pgid, int(rec["nice"]))
                errs.append(f"nice stays {LOW_NICE}: raising it back needs privilege ({e})" if e else "")
            if rec.get("io"):
                errs.append(_io_set(pgid, rec["io"]))
        errs = [e for e in errs if e]
        activity.append(p.state, activity.base(p.name, ev="restored", id=sid, at=now, pgid=pgid,
                                               restored={k: rec.get(k) for k in ("nice", "io", "autogroup")},
                                               **({"errors": errs} if errs else {}), **({} if alive else {"gone": True})))
        out["restored"].append(sid)
    db.set_kv(LOWERED_KEY, lowered)
    return out


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
