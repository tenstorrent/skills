# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Who paused the whole project, why and since when: a project pause is never silent or anonymous.

Every pause and resume (`ttp pause`/`resume`/`drain`, the web app, the daemon lifting a pause set
until a reboot, a run or another project calling `ttp pause`) records an event and the kv entry
INFO_KEY: {"by", "via", "reason", "since", "end"}. A pause found with no record (one from before
this, or a raw write) reads as paused by unknown since the time the flag was last written.

A pause longer than budget.paused_alert_s (DEFAULT_ALERT_S; 0 = never) raises one normal alert under
the condition key `paused:<since>` (Daemon.alert_long_pause, model-free and run while paused). It is
tracked like a condition only the user can clear (alerts.USER_BLOCKERS): reminded at most once a
day and cleared as soon as the project resumes.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

INFO_KEY = "pause_info"
ALERT_KIND = "paused"
DEFAULT_ALERT_S = 4 * 3600
NO_REASON = "no reason given"
UNKNOWN = "unknown"


def caller(p, via: str = "cli", env: dict | None = None) -> str:
    """Who runs this command for project p: "user", "coordinator", "worker run N", "project <name>"
    (a run of another project) or "unknown"."""
    env = os.environ if env is None else env
    from .project import in_run
    if not in_run(env):
        return "user"
    base = env.get("TTP_PROJECT")
    if base and Path(base).resolve() != Path(p.base).resolve():
        try:
            from .project import Project
            return f"project {Project(base).name}"
        except Exception:
            return f"project {Path(base).name}"
    run = str(env.get("TTP_RUN_ID") or "")
    row = p.db.one("SELECT role FROM runs WHERE id=?", (int(run),)) if run.isdigit() else None
    if row and row["role"] == "coordinator":
        return "coordinator"
    return f"worker run {run}" if run else UNKNOWN


def _event(db, text: str, data: dict, now: float) -> None:
    db.x("INSERT INTO events(ts,source,kind,severity,text,status,data) VALUES(?,?,?,?,?,?,?)",
         (now, "harness", "project_pause", "normal", text[:2000], "handled", json.dumps(data)))


def who(info: dict) -> str:
    """'the user (web app)', 'the coordinator', 'unknown'."""
    by, via = info.get("by") or UNKNOWN, info.get("via")
    name = {"user": "the user", "coordinator": "the coordinator", "daemon": "the daemon"}.get(by, by)
    return name + (f" ({via})" if via and by == "user" else "")


def user_resume(db, resource: str | None = None) -> dict | None:
    """The latest lift of the project's pause (or of resource `resource`'s), {"ts", "via"}, when the user
    lifted it; None when nobody did or someone else did."""
    if resource is None:
        row = db.one("SELECT ts, data FROM events WHERE source='harness' AND kind='project_pause' "
                     "ORDER BY ts DESC, id DESC LIMIT 1")
        d = json.loads(row["data"] or "{}") if row else {}
        ok = d.get("action") == "resume"
    else:
        from .db import PAUSE_REMOVED_KIND
        row = db.one("SELECT ts, data FROM events WHERE kind=? AND json_extract(data, '$.resource')=? "
                     "ORDER BY ts DESC, id DESC LIMIT 1", (PAUSE_REMOVED_KIND, resource))
        d = json.loads(row["data"] or "{}") if row else {}
        ok = bool(row)
    return {"ts": float(row["ts"]), "via": d.get("via") or ""} if ok and d.get("by") == "user" else None


REPAUSE_GUARD_S = 3 * 86400
HOST_CYCLE_VIA = "ttp drain"
HOST_CYCLE_END = "until reboot"


def repause_refusal(db, by: str, user_msg: int | None = None, resource: str | None = None,
                    now: float | None = None, guard_s: float = REPAUSE_GUARD_S) -> str:
    """Why a worker, coordinator or other project may not pause again what the user lifted ("" = it may).
    The user's resume (within the last `guard_s`) is their word that it runs; only a newer user message
    (`user_msg`, an inbound message after that resume) asking for the pause outranks it. A pause in force
    is not re-checked; an older resume no longer binds."""
    if by in ("user", UNKNOWN, "daemon"):
        return ""
    if (db.kv("paused", False) if resource is None else resource in db.paused_resources(shared=False)):
        return ""
    lifted = user_resume(db, resource)
    if not lifted or (time.time() if now is None else now) - lifted["ts"] > guard_s:
        return ""
    if user_msg is not None:
        row = db.one("SELECT ts FROM messages WHERE id=? AND direction='in' AND kind='user'", (int(user_msg),))
        if row and float(row["ts"]) > lifted["ts"]:
            return ""
        return (f"user message #{user_msg} is not a user message newer than the user's resume: "
                f"it cannot outrank that resume")
    what = "the project" if resource is None else f"resource {resource}"
    return (f"the user resumed {what} last ({lifted['via'] or 'CLI'}); {by} may not pause it again. Run the "
            "work it guards on its own terms, or tell the user; pass --user-msg <id> only when a newer user "
            "message asks for this pause")


def pause(db, by: str, reason: str = "", via: str = "", end: str = "", now: float | None = None,
          user_msg: int | None = None) -> dict:
    """Record a pause of the whole project by `by`. A non-user caller must say why; a user's (or an
    unknown caller's) pause without a reason records NO_REASON. A re-pause keeps when the pause began.
    A worker, coordinator or other project never re-applies a pause the user lifted (repause_refusal),
    except a host-cycle pause (`ttp drain`, until reboot) that the run or the daemon lifts itself."""
    now = time.time() if now is None else now
    cycle = via == HOST_CYCLE_VIA or end == HOST_CYCLE_END
    refusal = "" if cycle else repause_refusal(db, by, user_msg, now=now)
    if refusal:
        raise PermissionError(refusal)
    reason = " ".join(str(reason or "").split())
    if not reason:
        if by not in ("user", UNKNOWN):
            raise ValueError(f"a project pause by {by} needs a reason")
        reason = NO_REASON
    prev = current(db) if db.kv("paused", False) else None
    info = {"by": by, "via": via, "reason": reason[:500], "since": prev["since"] if prev else now, "end": end}
    with db.tx():
        db.set_kv(INFO_KEY, info)
        _event(db, f"project paused by {who(info)}: {info['reason']}" + (f" ({end})" if end else ""),
               {**info, "action": "pause"}, now)
    return info


def resume(db, by: str, via: str = "", reason: str = "", now: float | None = None) -> dict | None:
    """Record who resumed the project and clear its long-pause alert. Returns the pause it ended."""
    now = time.time() if now is None else now
    prev = current(db, now)
    with db.tx():
        db.x("DELETE FROM kv WHERE key=?", (INFO_KEY,))
        if prev:
            _event(db, f"project resumed by {who({'by': by, 'via': via})} after {(now - prev['since']) / 3600:.1f} h "
                       f"(paused by {who(prev)}: {prev['reason']})" + (f": {reason}" if reason else ""),
                   {"action": "resume", "by": by, "via": via, "reason": reason, "paused": prev}, now)
    from . import alerts
    alerts.clear_kind(db, ALERT_KIND, f"resumed by {who({'by': by, 'via': via})}", now)
    return prev


def current(db, now: float | None = None) -> dict | None:
    """The project's pause, {"by", "via", "reason", "since", "end"}, or None when it runs. An
    unrecorded pause is by UNKNOWN since its flag was last written."""
    if not db.kv("paused", False):
        return None
    info = db.kv(INFO_KEY)
    if isinstance(info, dict) and info.get("since"):
        return {"by": UNKNOWN, "via": "", "reason": "", "end": "", **info, "since": float(info["since"])}
    row = db.one("SELECT ts FROM kv WHERE key='paused'")
    return {"by": UNKNOWN, "via": "", "reason": "", "end": "",
            "since": float(row["ts"] if row and row["ts"] else (now or time.time()))}


def windows(db, since: float, now: float | None = None) -> list[list[float]]:
    """[start, end] of each project pause that ended after `since` (from its resume event), plus
    the current one up to now: time nothing ran by design, like host sleep (schedule.missed)."""
    now = time.time() if now is None else now
    out = []
    for r in db.q("SELECT ts, data FROM events WHERE source='harness' AND kind='project_pause' AND ts>? "
                  "ORDER BY ts, id", (since,)):
        d = json.loads(r["data"] or "{}")
        start = (d.get("paused") or {}).get("since") if d.get("action") == "resume" else None
        if start is not None:
            out.append([float(start), float(r["ts"])])
    cur = current(db, now)
    if cur:
        out.append([float(cur["since"]), now])
    return out


def adopt_unknown(db, now: float | None = None) -> dict | None:
    """A pause found with no record (daemon start, a raw write) is recorded as by unknown, once."""
    info = current(db, now)
    if not info or info["by"] != UNKNOWN or isinstance(db.kv(INFO_KEY), dict):
        return None
    with db.tx():
        db.set_kv(INFO_KEY, info)
        _event(db, "project found paused with no record of who paused it or why", {**info, "action": "pause"},
               time.time() if now is None else now)
    return info


def describe(info: dict, where, now: float | None = None) -> str:
    """'paused [until reboot] by the user (CLI) since 2026-10-11 15:39 PDT (59.0 h): <reason>'; times in the
    project's home zone (`where`: a Project, its settings or a zone)."""
    from . import timefmt
    now = time.time() if now is None else now
    return (f"paused{' ' + info['end'] if info.get('end') else ''} by {who(info)} since {timefmt.long(info['since'], where)} "
            f"({max(0.0, now - info['since']) / 3600:.1f} h)"
            + (f": {info['reason']}" if info.get("reason") else ""))


def alert_key(info: dict) -> str:
    return f"{ALERT_KIND}:{int(info['since'])}"


def holds(db, arg: str) -> bool:
    """The alert `paused:<since>` holds while that same pause lasts."""
    info = current(db)
    return bool(info) and str(int(info["since"])) == arg


def alert_s(cfg: dict) -> float:
    v = (cfg.get("budget") or {}).get("paused_alert_s", DEFAULT_ALERT_S)
    return float(DEFAULT_ALERT_S if v is None else v)


def alert_due(db, cfg: dict, now: float | None = None) -> tuple[str, dict] | None:
    """(alert key, pause) once the project has been paused longer than budget.paused_alert_s."""
    now = time.time() if now is None else now
    limit = alert_s(cfg)
    info = current(db, now)
    if not info or limit <= 0 or now - info["since"] < limit:
        return None
    return alert_key(info), info


LIVE_WITHIN_S = 6 * 3600


def live(db, within_s: float = LIVE_WITHIN_S, now: float | None = None) -> tuple[bool, str]:
    """Whether the project really works now, for a hand-back or takeover test ("is the other one still
    coordinating?"): a running daemon is not enough. Not live while a provider's login is broken (its
    auth breaker open) or when no model run succeeded, or spent tokens while running, in `within_s`."""
    from .alerts import BREAKER
    now = time.time() if now is None else now
    out = [r["key"][len(BREAKER):] for r in db.q("SELECT key FROM kv WHERE key LIKE ?", (BREAKER + "%",))
           if (db.kv(r["key"]) or {}).get("open")]
    if out:
        return False, f"logged out: {', '.join(sorted(out))} (its login breaker is open)"
    if db.kv("paused", False):
        return False, "paused"
    row = db.one("SELECT MAX(COALESCE(ended, started)) AS ts FROM runs WHERE status='ok' OR status='running' "
                 "AND (cost_usd>0 OR input_tokens>0 OR output_tokens>0)")
    last = float(row["ts"]) if row and row["ts"] else 0.0
    if now - last > within_s:
        return False, ("no successful model run yet" if not last else
                       f"no successful model run in {(now - last) / 3600:.1f} h (limit {within_s / 3600:g} h)")
    return True, f"last successful model run {(now - last) / 60:.0f} min ago"
