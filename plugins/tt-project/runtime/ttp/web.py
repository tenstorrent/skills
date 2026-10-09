# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The project's web app: a JSON API plus a static single-page UI, served by the daemon.
Bound to localhost by default and guarded by a per-project token; reach it from another machine
through an SSH local forward (see the `tt-project` skill's tunnels notes)."""
from __future__ import annotations

import json
import secrets
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import alerts, awake
from . import budget as bud
from . import globalcap as gcap
from . import coordinator as coord
from . import pushq, release
from . import schedule as sched
from . import upstream
from .daemon import (AUTH_PROBE_S, HEARTBEAT_STALE_S, KV_LOCAL_ONLY, KV_WORKTREES_DIRTY, LOGGED_OUT_NOTE, NET_HELD_NOTE,
                     WAIT_KEYS, WATCHDOG_S, heartbeat, idle_wake)
from .alerts import cleared  # noqa: F401  (readers import it from here)
from .db import (DB, SEVERITY_RANK, chat_floor, continues_id, deferral, dependency_ids, dump_result, host_line,
                 load_result, task_outcome)
from .project import Project, durable_write
from .providers import get_provider
from .runner import stop_runs
from .service import down_note, installed

STATIC = Path(__file__).resolve().parent / "web"
TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript", ".css": "text/css",
         ".svg": "image/svg+xml", ".json": "application/json"}


def token(p: Project) -> str:
    f = p.state / "web.token"
    try:
        have = f.read_text().strip()
    except OSError:
        have = ""
    if not have:   # missing, or emptied by a cut write: an empty token must never let anyone in
        have = secrets.token_hex(16)
        durable_write(f, have, mode=0o600)
    return have


def free_port(start: int = 18700) -> int:
    for port in range(start, start + 500):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


DAY, WEEK = 86400, 7 * 86400
RELAY_LATE_S = 600   # an open question no chat has read for this long: the relay is not delivering


def fix_for(provider: str, note: str) -> str:
    if note == "logged out":
        try:
            hint = get_provider(provider).login_hint
        except KeyError:
            hint = "log in to the agent CLI there"
        return f"log in once on the project's machine: {hint}"
    return "resumes by itself when the limit resets"


def at(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "—"
    return time.strftime("%H:%M" if abs(ts - (now or time.time())) < 20 * 3600 else "%a %H:%M", time.localtime(ts))


def since(ts: float, now: float | None = None) -> str:
    s = max((now or time.time()) - ts, 0)
    return f"{int(s // 60)} min" if s < 7200 else f"{s / 3600:.0f} h" if s < 2 * DAY else f"{s / DAY:.0f} days"


def gate_detail(g: dict, now: float | None = None) -> str:
    n = g.get("numbers") or {}
    if g.get("regime") == "windows":
        wins = n.get("plan") or [n]
        parts = []
        for w in wins:
            s = f"{w.get('window')} {w.get('utilization')}%"
            if w.get("resets_at"):
                s += f", resets {at(w['resets_at'], now)}"
            parts.append(s)
        # Plan-billed dollars are bounded by the windows, so the dollar caps do not apply to them.
        return (f"account use: {'; '.join(parts)}. The project stops at {n.get('limit')}% "
                f"(plan-billed, so the dollar caps do not apply)")
    est = f" (~${n['estimated_24h']:.2f} estimated)" if n.get("estimated_24h") else ""
    day = (f"${n['spent_today']:.2f} of ${n.get('daily_cap', 0):.0f} today" if "spent_today" in n
           else f"${n.get('spent_24h', 0):.2f} of ${n.get('daily_cap', 0):.0f} per 24h")
    out = f"{day}{est}, ${n.get('spent_7d', 0):.2f} of ${n.get('weekly_cap', 0):.0f} per 7d"
    if "global_today" in n:
        out += (f"; global {gcap.money(n['global_today'])} of ${float(n.get('global_cap') or 0):.0f} today: "
                f"{n.get('global_includes') or 'this account'}")
    return out


def offline_help(name: str) -> str:
    """What the web page says once it cannot reach the daemon. Only the viewer's computer can reopen
    a tunnel, so the command is given as information; the daemon's own service restarts it."""
    return (f"If {name} runs on another machine, the SSH tunnel from this computer is down: "
            f"`ttp web {name} --tunnel` reopens it, and `ttp web {name} --tunnel --keep` keeps it up across "
            f"reboots and network drops. If it runs on this computer, its daemon is down and its service "
            f"restarts it within a few minutes, unless `ttp stop` stopped it.")


FIVE_HOUR, SEVEN_DAY = ("five_hour", "5h"), ("seven_day", "7d")


def window_peaks(db: DB, provider: str, names: tuple[str, ...], now: float, span_s: float,
                 account: str | None = None) -> list[float]:
    """The peak reading of each completed period of a plan window with readings in the last span_s,
    only `account`'s when given. A period is told apart by its reset: readings of one period jitter
    by seconds, periods are a window's length apart."""
    rows = db.q(f"SELECT utilization, resets_at FROM snapshots WHERE provider=? AND window IN "
                f"({','.join('?' * len(names))}) AND resets_at IS NOT NULL AND resets_at<=? AND ts>=? "
                f"AND (? IS NULL OR COALESCE(account,'')=?) ORDER BY resets_at",
                (provider, *names, now, now - span_s, account, account))
    gap = bud.WINDOW_HOURS.get(names[0], 168.0) * 3600 / 2
    peaks: list[float] = []
    last = None
    for r in rows:
        reset, util = float(r["resets_at"]), float(r["utilization"] or 0)
        if last is None or reset - last > gap:
            peaks.append(util)
        else:
            peaks[-1] = max(peaks[-1], util)
        last = reset
    return peaks


def budget_line(db: DB, now: float | None = None, core: str = "claude", gate: dict | None = None,
                spent_24h: float | None = None) -> str:
    """The budget in one line, for the web app's header and `ttp status`:
    '5h 4% - resets in 3.9 h, 7d 21% - resets in 6.0 d, 24h $0.17 virtual, 5h avg 31%, 7d avg 72%'.
    The windows and averages are the current account's, from its plan readings (none once the
    provider moved to another account until that one reports); an average is the mean of
    each completed period's peak, 5-hour windows over 7 days and weekly ones over 3 weeks. The
    dollars are this project's last 24 h: 'virtual' (list-price equivalent) on a plan, 'actual' when
    billed by use, which counts only spend whose account was billed by use when it was spent
    (billing.py), not an earlier plan account's. An item without data is left out. Pacing, targets and caps are in the Budget tab."""
    now = now or time.time()
    provs = [r["provider"] for r in db.q("SELECT DISTINCT provider FROM snapshots WHERE ts>=? ORDER BY provider",
                                         (now - 3 * WEEK,))]
    prov = core if core in provs else provs[0] if provs else ""
    wins = {w.window: w for w in bud.plan_windows(db, now) if w.provider == prov}
    parts = []
    for label, names, unit, secs in (("5h", FIVE_HOUR, "h", 3600), ("7d", SEVEN_DAY, "d", DAY)):
        w = next((wins[n] for n in names if n in wins), None)
        if w:
            parts.append(f"{label} {w.utilization:.0f}%" +
                         (f" - resets in {max(w.resets_at - now, 0) / secs:.1f} {unit}" if w.resets_at else ""))
    plan = gate["regime"] == "windows" if (gate or {}).get("regime") else bool(wins)
    n = (gate or {}).get("numbers") or {}
    if not plan and ("spent_today" in n or "global_today" in n):
        return usage_line(n, now)
    spent = (db.spent_since(now - DAY) if spent_24h is None else spent_24h) if plan else \
        db.spent_since(now - DAY, billed=True)
    parts.append(f"24h ${spent:.2f} {'virtual' if plan else 'actual'}")
    account = next(iter(wins.values())).account if wins else None
    for label, names, span in (("5h avg", FIVE_HOUR, WEEK), ("7d avg", SEVEN_DAY, 3 * WEEK)):
        peaks = window_peaks(db, prov, names, now, span, account) if account is not None else []
        if peaks:
            parts.append(f"{label} {sum(peaks) / len(peaks):.0f}%")
    return ", ".join(parts)


def usage_line(n: dict, now: float) -> str:
    """The one budget line of a usage-billed account, from its gate's numbers:
    'today $1.20 this project, $85 of $200 global - resets in 6.5 h (1 machine stale)'.
    'Today' is the budget day (budget.day_start in budget.timezone), else the last 24 h; the global
    part is left out when budget.global_daily_usd is 0."""
    mine = n["spent_today"] if "spent_today" in n else n.get("spent_24h", 0)
    line = f"{'today' if 'spent_today' in n else '24h'} {gcap.money(mine)} this project"
    if "global_today" in n:
        line += f", {gcap.money(n['global_today'])} of ${float(n.get('global_cap') or 0):.0f} global"
    end = n.get("day_end") or n.get("global_resets_at")
    if end:
        line += f" - resets in {max(end - now, 0) / 3600:.1f} h"
    stale = len(n.get("global_stale") or []) if "global_today" in n else 0
    if stale:
        line += f" ({stale} machine{'s' if stale != 1 else ''} stale)"
    return line


def last_note(run_dir: str | None) -> str:
    """The run's latest `ttp note`: what a worker says it is doing, without opening its log."""
    f = Path(run_dir) / "progress.md" if run_dir else None
    if not f or not f.is_file():
        return ""
    with open(f, "rb") as fh:
        fh.seek(max(f.stat().st_size - 2048, 0))
        lines = [ln for ln in fh.read().decode(errors="replace").splitlines() if ln.strip()]
    return lines[-1].strip()[:200] if lines else ""


def health(p: Project, db: DB, alive: bool = True, now: float | None = None) -> dict:
    """Spend, coordinator health, waiting work and why nothing runs: what `ttp status` and the web
    app's header show, so both answer "is it working, what is it costing, what is it waiting for"."""
    now = now or time.time()
    cfg = p.config()
    core, notify = cfg.get("core_provider", "claude"), cfg["notify"]
    gates = db.kv("gates", {})
    last_turn = float(db.kv("last_coordinator_turn", 0))
    backoff = float(db.kv("coordinator_backoff_until", 0))
    last_run = db.one("SELECT status, ended FROM runs WHERE role='coordinator' AND status!='running' "
                      "ORDER BY id DESC LIMIT 1")
    paused_providers = []
    for r in db.q("SELECT key, value FROM kv WHERE key LIKE 'limited:%'"):
        v = json.loads(r["value"] or "{}")
        if float(v.get("until") or 0) > now:
            note = str(v.get("note") or "limit reached")
            paused_providers.append({"provider": r["key"].split(":", 1)[1], "note": note, "until": v["until"],
                                     "fix": fix_for(r["key"].split(":", 1)[1], note)})
    paused_resources = [{"resource": k, **v} for k, v in sorted(db.paused_resources().items())]
    # A deferred task waits to start (start_after / start_when): a plan, not a problem or a retry.
    deferred = [{"id": t["id"], "title": t["title"], "starts": coord.starts_text(t, now)}
                for t in db.q("SELECT * FROM tasks WHERE status='queued' AND labels LIKE '%\"start_%' "
                              "ORDER BY COALESCE(not_before, 0), id")]
    deferred = [t for t in deferred if t["starts"]]
    later = {t["id"] for t in deferred}
    waiting = [t for t in db.q("SELECT id, title, not_before, blocked_reason FROM tasks WHERE status='queued' "
                               "AND not_before>? ORDER BY not_before", (now,)) if t["id"] not in later]
    queued = [t for t in db.q("SELECT id, depends_on FROM tasks WHERE status='queued' AND (not_before IS NULL OR "
                              "not_before<=?)", (now,)) if t["id"] not in later]
    # A task on a paused resource is held, not ready: it is listed under its resource instead.
    from .coordinator import task_resources
    due = db.ready_tasks()
    # A task on a logged-out provider is held too, until its breaker closes.
    breakers = breaker_lines(db, now)
    out = {b["provider"] for b in breakers}
    logged_out = [t for t in due if (t["blocked_reason"] or "").startswith(LOGGED_OUT_NOTE)
                  and (t["provider"] or core) in out]
    # A task whose provider's API host does not resolve is held until it does (the daemon clears the note).
    net_held = [t for t in due if (t["blocked_reason"] or "").startswith(NET_HELD_NOTE)]
    held_ids = {t["id"] for t in logged_out + net_held}
    ready = sum(1 for t in due if not task_resources(t) & {r["resource"] for r in paused_resources}
                and t["id"] not in held_ids)
    blocked = db.one("SELECT COUNT(*) n FROM tasks WHERE status='blocked'")["n"]
    running = db.one("SELECT COUNT(*) n FROM runs WHERE status='running'")["n"]
    asks = db.q("SELECT id, ts, text FROM messages WHERE kind='ask' AND handled=0 ORDER BY id DESC LIMIT 5")
    top = db.one("SELECT source, ROUND(SUM(usd),2) usd FROM ledger WHERE ts>=? GROUP BY source ORDER BY SUM(usd) DESC "
                 "LIMIT 1", (now - WEEK,))
    # A relay that stopped reading: questions the user has not seen, so nothing can be decided.
    # A chat that read past an ask below its severity floor skipped it, so that does not count.
    readers = [(int(r["last_read"] or 0),
                SEVERITY_RANK.get(chat_floor(r["min_severity"], notify.get("chat_min_severity")), 1))
               for r in db.q("SELECT last_read, min_severity FROM chats WHERE id!='web'")]
    if notify.get("slack"):
        slack_floor = SEVERITY_RANK.get(notify.get("slack_min_severity", "high"), 2)
        readers.append((int(db.kv("slack_last_out", 0)), slack_floor))
    undelivered = None
    if readers:
        late = [a for a in db.q("SELECT id, ts, severity FROM messages WHERE kind='ask' AND handled=0 AND chat IS NULL "
                                "AND ts>?", (now - 14 * DAY,))
                if not any(seen >= a["id"] and SEVERITY_RANK.get(a["severity"], 1) >= floor for seen, floor in readers)]
        since = min((a["ts"] for a in late), default=now)
        if late and since < now - RELAY_LATE_S:
            lowest = min(floor for _, floor in readers)
            undelivered = {"asks": len(late), "since": since,
                           "below_floor": sum(SEVERITY_RANK.get(a["severity"], 1) < lowest for a in late)}
    wake = idle_wake(p, cfg, gates, now, db=db) if last_turn else {"at": None, "held": None}
    next_wake = max(wake["at"], backoff, now) if wake["at"] else None

    # What keeps ready tasks from starting; shown even while other runs work.
    stops = []
    if not alive:
        stops.append(f"the daemon is not running: {down_note(p)}")
    if db.kv("paused", False):
        stops.append(f"the project is paused (`ttp resume {p.name}` or the web app)")
    for pp in paused_providers:
        stops.append(f"{pp['provider']} is paused until {at(pp['until'], now)}: {pp['note']}")
    for prov, pg in sorted(gates.items(), key=lambda kv: kv[0] != core):
        if pg.get("level") == "red":
            stops.append(("budget is red: " if prov == core else f"budget for {prov} is red: ")
                         + "; ".join(pg.get("reasons") or []))
    g = gates.get(core) or {}
    if g.get("regime") == "windows" and g.get("level") == "orange" and (g.get("numbers") or {}).get("starts") is False:
        stops.append("no new starts near the plan line: " + "; ".join(g.get("reasons") or []))
    settle = float(db.kv("settle_until", 0) or 0)
    if settle > now and alive:
        stops.append(f"the host just woke from sleep; new work starts at {at(settle, now)} if it stays awake")
    disk = db.kv("disk_low")
    if disk:
        stops.append(f"disk is low ({disk['free_gb']} GB free), so only questions and plans start")
    you = ("waiting on you: " + ", ".join(x for x in (f"{blocked} blocked task(s)" if blocked else "",
                                                     f"{len(asks)} open question(s)" if asks else "") if x)
           if blocked or asks else "")
    why = list(stops)
    if backoff > now:
        why.append(f"the coordinator is backing off after failed turns, next try {at(backoff, now)}")
    if ready:
        why.append(f"{ready} task(s) ready to start")
    for pr in paused_resources:
        why.append(f"{pr['resource']} is paused" + (f" ({pr['reason']})" if pr.get("reason") else "")
                   + f": its tasks wait (`ttp resume {p.name} --resource {pr['resource']}`)")
    if logged_out:
        provs = sorted({t["provider"] or core for t in logged_out})
        why.append(f"{len(logged_out)} task(s) held: logged out ({', '.join(provs)}); they start once a login "
                   f"check passes")
    if net_held:
        provs = sorted({t["provider"] or core for t in net_held})
        why.append(f"{len(net_held)} task(s) held: network ({', '.join(provs)}); they start once the API host "
                   f"resolves")
    if waiting:
        why.append(f"{len(waiting)} task(s) waiting, next try {at(waiting[0]['not_before'], now)}")
    if deferred:
        why.append(f"{len(deferred)} task(s) deferred, first {deferred[0]['starts']}")
    retry = db.kv(coord.RETRY_WAKE_KEY) or {}
    if retry.get("at"):
        review = bool(retry.get("review"))
        what = "review tasks" if review else "new tasks"
        if retry["at"] - now >= 365 * 86400 or coord.task_cap(cfg, review) == 0:
            why.append(f"the cap on {what} is 0: none are added until it is raised")
        else:
            why.append(f"the 24 h cap on {what} is full; the coordinator adds held work at {at(retry['at'], now)}")
    if len(queued) > len(due):
        why.append(f"{len(queued) - len(due)} queued task(s) wait on other tasks")
    if you:
        why.append(you)
    if not why and next_wake:
        why.append(f"nothing queued; the coordinator checks in at {at(next_wake, now)}")
    elif not why and wake["held"]:
        why.append(f"nothing queued; the coordinator's idle check is {wake['held']}")
    held = ""
    if running and ready and stops:
        held = f"{ready} ready task(s) not starting: " + "; ".join(stops + ([you] if you else []))
    working = db.q("SELECT r.id run, r.task, r.role, r.provider, r.model, r.effort, r.started, r.cost_usd, r.dir, "
                   "r.note run_note, t.title FROM runs r "
                   "LEFT JOIN tasks t ON t.id=r.task WHERE r.status='running' ORDER BY r.id")
    for w in working:
        w["note"] = last_note(w.pop("dir"))
        w["wake"] = run_wake(w.pop("run_note"))
    spend = {"spent_24h": round(db.spent_since(now - DAY), 2), "spent_7d": round(db.spent_since(now - WEEK), 2),
             "top_7d": top if top and top["usd"] else None, "in_flight": round(bud.in_flight(db), 2)}
    spend["headline"] = budget_line(db, now, core, g, spend["spent_24h"])
    spend["detail"] = gate_detail(g, now) if g else ""
    return {
        "spend": spend,
        "coordinator": {"last_turn": last_turn or None, "last_status": last_run["status"] if last_run else None,
                        "failures": int(db.kv("coordinator_failures", 0)),
                        "backoff_until": backoff if backoff > now else None,
                        "summary": (db.kv("last_coordinator_summary", {}) or {}).get("summary", ""),
                        "idle_wake": next_wake, "idle_held": wake["held"]},
        "providers_paused": paused_providers, "resources_paused": paused_resources, "waiting": waiting,
        "deferred": deferred,
        "logged_out": [{k: t[k] for k in ("id", "title", "provider")} for t in logged_out],
        "net_held": [{k: t[k] for k in ("id", "title", "provider")} for t in net_held],
        "breakers": breakers,
        "asks": asks, "running": running, "working": working,
        "undelivered": undelivered,
        "why_idle": "; ".join(why) if not running else "", "held": held,
        "host": host_line(db.boots(now - DAY)),
        "idle_sleep": awake.line(db.kv(awake.KV), (db.kv("daemon", {}) or {}).get("pid")) if alive else "",
        "release": release.line(p, db, cfg),
        "schedules_broken": sched.broken_line(db),
        "schedules_waiting": sched.waiting_line(db),
        "local_only": local_only_line(db),
        "uncommitted": uncommitted_line(db),
        "upstream": upstream.status_line(db, cfg),
    }


def breaker_lines(db: DB, now: float) -> list[dict]:
    """Each open auth breaker (Daemon.check_logins) in one plain line: since when, and when the
    harness checks the login again. The fix is a login only the user can do, named by the alert."""
    out = []
    for r in db.q("SELECT key FROM kv WHERE key LIKE ? ORDER BY key", (alerts.BREAKER + "%",)):
        prov = r["key"][len(alerts.BREAKER):]
        b = alerts.breaker(db, prov)
        if not b:
            continue
        nxt = (f"one run checks the login every {AUTH_PROBE_S // 60} min" if b.get("probe")
               else f"next login check {at(max(float(b.get('next_check') or now), now), now)}")
        out.append({"provider": prov, "opened": b.get("opened"), "next_check": b.get("next_check"),
                    "checks": int(b.get("checks") or 0),
                    "line": f"{prov} is logged out since {at(b.get('opened'), now)}: no runs start on it; {nxt}, "
                            f"and work resumes by itself once it passes"})
    return out


def local_only_line(db: DB) -> str:
    """Done code tasks whose branch exists only on this machine (Daemon.check_local_only), in one
    line; "" when there are none. A task no longer done (cancelled) drops out at once."""
    flagged = db.kv(KV_LOCAL_ONLY) or {}
    if not flagged:
        return ""
    done = {str(r["id"]) for r in db.q("SELECT id FROM tasks WHERE status='done' AND id IN (%s)"
                                       % ",".join("?" * len(flagged)), [int(k) for k in flagged])}
    items = [(k, flagged[k]) for k in sorted(flagged, key=int) if k in done]
    if not items:
        return ""
    shown = ", ".join(f"#{k} {v['branch']} ({v['ahead']} commit{'' if v['ahead'] == 1 else 's'})" for k, v in items[:5])
    more = f" and {len(items) - 5} more" if len(items) > 5 else ""
    return (f"{len(items)} done task{'' if len(items) == 1 else 's'} with work only on this machine "
            f"(branch not on any remote): {shown}{more}")


def uncommitted_line(db: DB) -> str:
    """Finished tasks' worktrees kept for modified tracked files (Daemon._uncommitted), in one line;
    "" when there are none."""
    kept = db.kv(KV_WORKTREES_DIRTY) or {}
    if not kept:
        return ""
    ids = sorted(kept, key=int)
    shown = ", ".join(f"#{k}" for k in ids[:8]) + (f" and {len(ids) - 8} more" if len(ids) > 8 else "")
    return (f"{len(ids)} finished task worktree{'' if len(ids) == 1 else 's'} kept with uncommitted changes "
            f"(raised to the coordinator): {shown}")


def uncommitted_feed(db: DB) -> list[dict]:
    """Feed rows (not the top section) for the worktrees uncommitted_line counts."""
    return [{"id": None, "ts": v.get("since") or 0, "kind": "worktree", "severity": "normal", "state": "fyi",
             "text": f"#{k} ({v.get('status')}) left uncommitted changes in {v.get('count')} tracked file(s), "
                     f"e.g. {', '.join(v.get('paths') or [])}; its worktree is kept"}
            for k, v in (db.kv(KV_WORKTREES_DIRTY) or {}).items()]


def run_wake(note: str | None) -> str | None:
    """The tier of a run that wakes a waiting task, from its run note; None for any other run."""
    try:
        wake = json.loads(note or "{}").get("wake")
    except (ValueError, AttributeError):
        return None
    return str(wake.get("tier")) if isinstance(wake, dict) and wake.get("tier") else None


def attention(db: DB, now: float) -> list[dict]:
    """The top section: open asks and alerts about problems active now (see alerts.needs_you)."""
    return alerts.needs_you(db, now)


def state_payload(p: Project, db: DB) -> dict:
    now = time.time()
    tasks = db.q("SELECT id,title,kind,status,priority,tier,provider,budget_usd,spent_usd,attempts,max_attempts,"
                 "origin,branch,pr_url,blocked_reason,not_before,created,updated,result,labels,depends_on FROM tasks WHERE status NOT IN ('done','failed',"
                 "'cancelled') OR updated>? ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'blocked' THEN 1 "
                 "WHEN 'review' THEN 2 WHEN 'pushing' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END, priority, id DESC LIMIT 200",
                 (now - 7 * 86400,))
    in_review = db.review_since()
    # The dependency graph and start/retry conditions, so outside tools need not infer them.
    unmet = db.unmet_dependencies(db.q("SELECT * FROM tasks WHERE status='queued'"))
    for t in tasks:
        t["review_since"] = in_review.get(t["id"])
        t["outcome"] = task_outcome(t)   # changes_needed for a review that asked for changes
        result = load_result(t["result"])
        t["result"] = str(result.get("summary") or "")[:600]
        pushed = result.get("pushed")
        t["pushed"] = [{k: x.get(k) for k in ("branch", "sha", "version", "status")} for x in pushed
                       if isinstance(x, dict)] or None if isinstance(pushed, list) else None   # what the push queue pushed for a review
        t["starts"] = coord.starts_text(t, now) if t["status"] == "queued" else ""
        d = deferral(t)
        t.update(depends_on=dependency_ids(t), waits_on=unmet.get(t["id"], []), continues=continues_id(t),
                 start_after=d.get("after"), start_when=d.get("when"),
                 retry={**{k: result[k] for k in WAIT_KEYS if k in result}, "next_try": t["not_before"]}
                 if t["status"] == "queued" and result.get("status") == "waiting" else None)
        del t["labels"]
    runs = db.q("SELECT id,task,role,provider,model,effort,status,started,ended,cost_usd FROM runs "
                "ORDER BY id DESC LIMIT 40")
    return {
        "project": {"name": p.name, "root": str(p.root), "config": p.config()},
        "daemon": db.kv("daemon", {}), "paused": db.kv("paused", False),
        "gates": {k: {**g, "detail": gate_detail(g, now)} for k, g in db.kv("gates", {}).items()},
        "heartbeat": heartbeat(p), "heartbeat_stale_s": HEARTBEAT_STALE_S, "watchdog_s": WATCHDOG_S,
        "service": installed(p), "disk_low": db.kv("disk_low"),
        "disk": db.kv("disk"), "worktrees_kept": db.kv("worktrees_kept"),
        "tasks": tasks, "task_counts": db.status_counts(), "runs": runs,
        "issues": db.q("SELECT id,source,title,severity,status,count,first_seen,last_seen,task FROM issues "
                       "WHERE status IN ('open','tracking') ORDER BY last_seen DESC LIMIT 100"),
        "schedules": sched.with_costs(db),
        "attention": attention(db, now),
        "feed": sorted(alerts.feed(db, now) + uncommitted_feed(db), key=lambda m: -float(m["ts"] or 0)),
        "offline_help": offline_help(p.name),
        "budget": bud.history(db),
        "coordinator": db.kv("last_coordinator_summary", {}),
        "health": health(p, db, now=now),
        "push_queue": pushq.web(p, db, now),
        "accounts": db.q("SELECT provider, account, MAX(started) last FROM runs WHERE account IS NOT NULL "
                         "GROUP BY provider, account ORDER BY last DESC"),
        "now": now,
    }


class Handler(BaseHTTPRequestHandler):
    daemon_ref = None
    server_version = "tt-project"

    def log_message(self, *args):  # keep the daemon log quiet
        pass

    def _proj(self) -> Project:
        return self.daemon_ref.p

    def _auth(self) -> bool:
        want = token(self._proj())
        got = self.headers.get("X-TTP-Token") or ""
        if not got:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "ttp_token":
                    got = v
        return secrets.compare_digest(got, want)

    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            f = (STATIC / name).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._send(404, {"error": "not found"})
            return self._send(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))
        if not self._auth():
            return self._send(401, {"error": "token required"})
        db = DB(self._proj().state / "project.db")
        try:
            if url.path == "/api/state":
                return self._send(200, state_payload(self._proj(), db))
            if url.path == "/api/overview":
                from . import overview
                data = overview.overview()
                return self._send(200, {**data, "footer": overview.footer(data["global"]),
                                        "spend": {r["name"]: overview.spend(r) for r in data["projects"] if r["ok"]}})
            if url.path == "/api/messages":
                q = parse_qs(url.query)
                after = int(q.get("after", ["0"])[0])
                rows = db.q("SELECT m.id,m.ts,m.direction,m.chat,m.channel,m.kind,m.severity,m.text,"
                            "c.label chat_label FROM messages m LEFT JOIN chats c ON c.id=m.chat "
                            "WHERE m.id>? ORDER BY m.id LIMIT 300", (after,))
                return self._send(200, rows)
            if url.path.startswith("/api/run/"):
                run_id = int(url.path.rsplit("/", 1)[-1])
                r = db.one("SELECT dir FROM runs WHERE id=?", (run_id,))
                if not r:
                    return self._send(404, {"error": "no run"})
                d = Path(r["dir"])
                tail = lambda n: (d / n).read_text(errors="replace")[-8000:] if (d / n).exists() else ""  # noqa: E731
                return self._send(200, {"progress": tail("progress.md"), "result": tail("result.json"),
                                        "stderr": tail("stderr.log")})
            return self._send(404, {"error": "unknown endpoint"})
        finally:
            db.close()

    def do_POST(self):
        url = urlparse(self.path)
        if not self._auth():
            return self._send(401, {"error": "token required"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        p = self._proj()
        db = DB(p.state / "project.db")
        try:
            if url.path == "/api/say":
                text = (body.get("text") or "").strip()
                if not text:
                    return self._send(400, {"error": "empty"})
                mid = db.post("in", text, chat="web", channel="web", kind="user", provenance="web")
                db.x("INSERT OR IGNORE INTO chats(id,created,label,last_active) VALUES('web',?, 'web app', ?)",
                     (time.time(), time.time()))
                return self._send(200, {"id": mid})
            if url.path.startswith("/api/schedule/"):
                name = url.path.rsplit("/", 1)[-1]
                row = db.one("SELECT * FROM schedules WHERE name=?", (name,))
                if not row:
                    return self._send(404, {"error": "no schedule"})
                own = Project(p.base)   # this thread's connection, not the daemon's
                own._db = db
                try:
                    sched.before_change(own)
                except ValueError as e:
                    return self._send(409, {"error": str(e)})
                if "enabled" in body:
                    db.x("UPDATE schedules SET enabled=? WHERE name=?", (int(bool(body["enabled"])), name))
                if "budget_usd_day" in body:
                    v = body["budget_usd_day"]
                    db.x("UPDATE schedules SET budget_usd_day=? WHERE name=?", (None if v in (None, "") else float(v), name))
                sched.write_file(own, f"schedule {name}: changed in the web app")
                return self._send(200, {"ok": True})
            if url.path.startswith("/api/task/"):
                tid = int(url.path.rsplit("/", 1)[-1])
                t = db.task(tid)
                if not t:
                    return self._send(404, {"error": "no task"})
                if body.get("status") == "queued":
                    # One statement, so dispatch cannot start the task between a check and the write.
                    if not db.conn.execute("UPDATE tasks SET status='queued', blocked_reason=NULL, updated=? "
                                           "WHERE id=? AND status!='running'", (time.time(), tid)).rowcount:
                        return self._send(409, {"error": f"task #{tid} is running: cancel it first"})
                    prev = load_result(t["result"])
                    if t["status"] != "queued" and "waiting_since" in prev:
                        # A requeue is a decision to run it, not to sleep on its probe.
                        prev.pop("waiting_since")
                        db.x("UPDATE tasks SET result=? WHERE id=? AND result=?",
                             (dump_result(prev), tid, t["result"]))
                elif body.get("status") == "cancelled":
                    db.update_task(tid, status="cancelled")
                    stop_runs(db, p.runs, tid)
                if body.get("priority"):
                    db.update_task(tid, priority=int(body["priority"]))
                return self._send(200, {"ok": True})
            if url.path == "/api/pause":
                if body.get("resource"):
                    from .coordinator import pause_resource
                    try:
                        pause_resource(p, str(body["resource"]), bool(body.get("paused")),
                                       reason=str(body.get("reason") or ""), by="user", db=db)
                    except ValueError as e:
                        return self._send(400, {"error": str(e)})
                    return self._send(200, {"ok": True})
                db.set_kv("paused", bool(body.get("paused")))
                return self._send(200, {"ok": True})
            if url.path == "/api/config":
                from .coordinator import USER_SETTABLE
                key = body.get("key")
                if key not in USER_SETTABLE:
                    return self._send(400, {"error": f"{key} not settable here"})
                try:
                    p.set_config(key, USER_SETTABLE[key](body.get("value")))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except RuntimeError as e:   # project.json unreadable with no last good copy
                    return self._send(409, {"error": str(e)})
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "unknown endpoint"})
        finally:
            db.close()


def bind(daemon, port: int | None = None) -> ThreadingHTTPServer:
    """Bind the web app's server: on the project's port (picked and saved if unset), or on `port`
    as given (0: one the OS picks, held from now on, so nothing else can take it before serve)."""
    p = daemon.p
    cfg = p.config()
    if port is None:
        port = int(cfg.get("web", {}).get("port") or 0) or free_port()
        if not cfg.get("web", {}).get("port"):
            try:
                p.set_config("web.port", port)
            except RuntimeError:
                pass   # project.json unreadable with no last good copy: serve on this port without saving it
    token(p)
    handler = type("Handler", (Handler,), {"daemon_ref": daemon})   # this server's own project, not the last one bound
    httpd = ThreadingHTTPServer((cfg.get("web", {}).get("bind", "127.0.0.1"), port), handler)
    httpd.daemon_threads = True
    return httpd


def serve(daemon, httpd: ThreadingHTTPServer | None = None) -> None:
    p = daemon.p
    httpd = httpd or bind(daemon)
    port = httpd.server_address[1]
    db = DB(p.state / "project.db")          # this thread's own connection; SQLite objects are per-thread
    db.set_kv("web", {"port": port, "bind": p.config().get("web", {}).get("bind", "127.0.0.1")})
    db.close()
    httpd.serve_forever()
