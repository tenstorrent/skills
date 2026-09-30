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

from . import alerts
from . import budget as bud
from . import schedule as sched
from .daemon import HEARTBEAT_STALE_S, heartbeat
from .alerts import cleared  # noqa: F401  (readers import it from here)
from .db import DB, SEVERITY_RANK, chat_floor, dump_result, host_line, load_result
from .project import Project
from .providers import get_provider
from .runner import stop_runs

STATIC = Path(__file__).resolve().parent / "web"
TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript", ".css": "text/css",
         ".svg": "image/svg+xml", ".json": "application/json"}


def token(p: Project) -> str:
    f = p.state / "web.token"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_hex(16))
        f.chmod(0o600)
    return f.read_text().strip()


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
        wins = n.get("pace") or [n]
        parts = []
        for w in wins:
            s = f"{w.get('window')} {w.get('utilization')}%"
            if w.get("projected") is not None:
                s += f", on pace for {w['projected']:.0f}%" + (f" by the {at(w['resets_at'], now)} reset"
                                                                if w.get("resets_at") else "")
            elif w.get("resets_at"):
                s += f", resets {at(w['resets_at'], now)}"
            parts.append(s)
        # Plan-billed dollars are bounded by the windows, so the dollar caps do not apply to them.
        return (f"account use: {'; '.join(parts)}. The project stops at {n.get('limit')}% "
                f"(plan-billed, so the dollar caps do not apply)")
    est = f" (~${n['estimated_24h']:.2f} estimated)" if n.get("estimated_24h") else ""
    return (f"${n.get('spent_24h', 0):.2f} of ${n.get('daily_cap', 0):.0f} per 24h{est}, "
            f"${n.get('spent_7d', 0):.2f} of ${n.get('weekly_cap', 0):.0f} per 7d")


def paced_line(g: dict, now: float) -> str:
    """'paced: next start ~14:20 (seven_day on pace for 250%)' while a pace hold spaces out new
    starts (budget._pace), else ''."""
    hold = (g.get("numbers") or {}).get("paced") or {}
    if float(hold.get("until") or 0) <= now:
        return ""
    over = f"{hold.get('window')} on pace for {hold['projected']:.0f}%" if hold.get("projected") is not None \
        else f"{hold.get('window')} over pace"
    return f"paced: next start ~{at(hold['until'], now)} ({over})"


def spend_headline(spend: dict, g: dict) -> str:
    """The header's one-line answer to "how does spend compare with the limit that binds"."""
    n = g.get("numbers") or {}
    if g.get("regime") == "windows" and n.get("window"):
        return f"${spend['spent_24h']:.2f} 24h · {n['window']} {n['utilization']:.0f}% of {n['limit']:.0f}%"
    if n.get("daily_cap"):
        return (f"${n.get('spent_24h', 0):.2f} of ${n['daily_cap']:.0f} 24h · "
                f"${n.get('spent_7d', 0):.2f} of ${n.get('weekly_cap', 0):.0f} 7d")
    return f"${spend['spent_24h']:.2f} 24h · ${spend['spent_7d']:.2f} 7d"


def offline_help(name: str) -> str:
    """What the web page says once it cannot reach the daemon. Only the viewer's computer can reopen
    a tunnel, so the command is given as information; the daemon's own service restarts it."""
    return (f"If {name} runs on another machine, the SSH tunnel from this computer is down: "
            f"`ttp web {name} --tunnel` reopens it, and `ttp web {name} --tunnel --keep` keeps it up across "
            f"reboots and network drops. If it runs on this computer, its daemon is down: its service "
            f"restarts it, and `ttp restart {name}` does so now.")


WINDOW_LABELS = {"five_hour": "5-hour", "5h": "5-hour", "seven_day": "Weekly", "7d": "Weekly",
                 "seven_day_opus": "Weekly (Opus)", "seven_day_sonnet": "Weekly (Sonnet)"}


def until(ts: float, now: float) -> str:
    """'3h 40m', '6d 20h', '12m': time left until ts."""
    m = max(int((ts - now) // 60), 0)
    if m < 60:
        return f"{m}m"
    if m < 24 * 60:
        return f"{m // 60}h {m % 60}m"
    return f"{m // 1440}d {m % 1440 // 60}h"


def window_history(db: DB, provider: str, window: str, now: float, days: int = 14) -> str:
    """A plan window's recent history from the stored readings, '' without any: the daily peak for
    windows of a day or less, the final reading of the last two completed periods for longer ones."""
    hours = bud.WINDOW_HOURS.get(window, 168.0)
    if hours <= 24:
        rows = db.q("SELECT date(ts,'unixepoch','localtime') d, MAX(utilization) peak FROM snapshots "
                    "WHERE provider=? AND window=? AND ts>=? GROUP BY d ORDER BY d", (provider, window, now - days * DAY))
        return f"peaks last {days} days: " + " ".join(f"{float(r['peak']):.0f}" for r in rows) if rows else ""
    rows = db.q("SELECT ts, utilization, resets_at FROM snapshots WHERE provider=? AND window=? AND resets_at IS NOT NULL "
                "AND resets_at<=? AND ts>=? ORDER BY ts", (provider, window, now, now - 3 * hours * 3600 - DAY))
    finals: dict[int, float] = {}
    for r in rows:   # a period is keyed by its reset, to the hour: readings of one period jitter by seconds
        finals[round(float(r["resets_at"]) / 3600)] = float(r["utilization"] or 0)
    last = [finals[k] for k in sorted(finals)][-2:]
    if not last:
        return ""
    unit = "week" if hours == 168 else "period"
    label = f"last two {unit}s: " if len(last) == 2 else f"last {unit}: "
    return label + ", ".join(f"{v:.0f}%" for v in last)


def budget_lines(db: DB, now: float | None = None) -> list[str]:
    """The budget in a few plain lines, for the top of the web app and `ttp status`: one line per plan
    window (used, time to reset, history) and one for the dollar caps. Nothing where there is no data.
    Pacing, gate reasons and top spenders are in the Budget tab."""
    now = now or time.time()
    lines: list[str] = []
    wins = bud.plan_windows(db, now)
    for prov in sorted({w.provider for w in wins}):
        lines.append(f"{prov.capitalize()} plan")
        for w in sorted((w for w in wins if w.provider == prov),
                        key=lambda w: (bud.WINDOW_HOURS.get(w.window, 168.0), w.window)):
            parts = [f"{w.utilization:.0f}% used"]
            if w.resets_at:
                parts.append(f"resets in {until(w.resets_at, now)}")
            hist = window_history(db, prov, w.window, now)
            if hist:
                parts.append(hist)
            lines.append(f"  {WINDOW_LABELS.get(w.window, w.window)}: " + ", ".join(parts))
    caps = [g.get("numbers") or {} for g in (db.kv("gates", {}) or {}).values() if g.get("regime") != "windows"]
    n = next((c for c in caps if c.get("daily_cap") or c.get("weekly_cap")), None)
    if n:
        lines.append(f"${n.get('spent_24h', 0):.2f} of ${n.get('daily_cap', 0):.0f} last 24h, "
                     f"${n.get('spent_7d', 0):.2f} of ${n.get('weekly_cap', 0):.0f} last 7 days")
    return lines


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
    c, core, notify = cfg["coordinator"], cfg.get("core_provider", "claude"), cfg["notify"]
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
    waiting = db.q("SELECT id, title, not_before, blocked_reason FROM tasks WHERE status='queued' AND not_before>? "
                   "ORDER BY not_before", (now,))
    queued = db.q("SELECT id, depends_on FROM tasks WHERE status='queued' AND (not_before IS NULL OR not_before<=?)",
                  (now,))
    # A task on a paused resource is held, not ready: it is listed under its resource instead.
    from .coordinator import task_resources
    due = db.ready_tasks()
    ready = sum(1 for t in due if not task_resources(t) & {r["resource"] for r in paused_resources})
    blocked = db.one("SELECT COUNT(*) n FROM tasks WHERE status='blocked'")["n"]
    running = db.one("SELECT COUNT(*) n FROM runs WHERE status='running'")["n"]
    asks = db.q("SELECT id, ts, text FROM messages WHERE kind='ask' AND handled=0 AND ts>? ORDER BY id DESC LIMIT 5",
                (now - 14 * DAY,))
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
    idle_wake = None
    if last_turn and not waiting and not queued and not db.one("SELECT id FROM tasks WHERE status='running'"):
        idle_wake = max(last_turn + float(c.get("idle_wake_s", 1800)), backoff, now)

    # What keeps ready tasks from starting; shown even while other runs work.
    stops = []
    if not alive:
        stops.append(f"the daemon is not running (`ttp restart {p.name}`)")
    if db.kv("paused", False):
        stops.append(f"the project is paused (`ttp resume {p.name}` or the web app)")
    for pp in paused_providers:
        stops.append(f"{pp['provider']} is paused until {at(pp['until'], now)}: {pp['note']}")
    for prov, pg in sorted(gates.items(), key=lambda kv: kv[0] != core):
        if pg.get("level") == "red":
            stops.append(("budget is red: " if prov == core else f"budget for {prov} is red: ")
                         + "; ".join(pg.get("reasons") or []))
    g = gates.get(core) or {}
    paced = paced_line(g, now)
    if paced:
        stops.append(paced)
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
    if waiting:
        why.append(f"{len(waiting)} task(s) waiting, next try {at(waiting[0]['not_before'], now)}")
    if len(queued) > len(due):
        why.append(f"{len(queued) - len(due)} queued task(s) wait on other tasks")
    if you:
        why.append(you)
    if not why and idle_wake:
        why.append(f"nothing queued; the coordinator checks in at {at(idle_wake, now)}")
    held = ""
    if running and ready and stops:
        held = f"{ready} ready task(s) not starting: " + "; ".join(stops + ([you] if you else []))
    working = db.q("SELECT r.id run, r.task, r.role, r.provider, r.model, r.effort, r.started, r.cost_usd, r.dir, t.title "
                   "FROM runs r "
                   "LEFT JOIN tasks t ON t.id=r.task WHERE r.status='running' ORDER BY r.id")
    for w in working:
        w["note"] = last_note(w.pop("dir"))
    spend = {"spent_24h": round(db.spent_since(now - DAY), 2), "spent_7d": round(db.spent_since(now - WEEK), 2),
             "top_7d": top if top and top["usd"] else None, "in_flight": round(bud.in_flight(db), 2)}
    spend["headline"] = spend_headline(spend, g)
    spend["detail"] = gate_detail(g, now) if g else ""
    return {
        "spend": spend,
        "coordinator": {"last_turn": last_turn or None, "last_status": last_run["status"] if last_run else None,
                        "failures": int(db.kv("coordinator_failures", 0)),
                        "backoff_until": backoff if backoff > now else None,
                        "summary": (db.kv("last_coordinator_summary", {}) or {}).get("summary", ""),
                        "idle_wake": idle_wake},
        "providers_paused": paused_providers, "resources_paused": paused_resources, "waiting": waiting, "asks": asks, "running": running, "working": working,
        "undelivered": undelivered,
        "why_idle": "; ".join(why) if not running else "", "held": held,
        "host": host_line(db.boots(now - DAY)),
        "budget_lines": budget_lines(db, now),
    }


def attention(db: DB, now: float) -> list[dict]:
    """The top section: open asks and alerts about problems active now (see alerts.needs_you)."""
    return alerts.needs_you(db, now)


def state_payload(p: Project, db: DB) -> dict:
    now = time.time()
    tasks = db.q("SELECT id,title,kind,status,priority,tier,provider,budget_usd,spent_usd,attempts,origin,branch,"
                 "pr_url,blocked_reason,not_before,created,updated,result FROM tasks WHERE status NOT IN ('done','failed',"
                 "'cancelled') OR updated>? ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'blocked' THEN 1 "
                 "WHEN 'review' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END, priority, id DESC LIMIT 200",
                 (now - 7 * 86400,))
    for t in tasks:
        t["result"] = str(load_result(t["result"]).get("summary") or "")[:600]
    runs = db.q("SELECT id,task,role,provider,model,effort,status,started,ended,cost_usd FROM runs "
                "ORDER BY id DESC LIMIT 40")
    return {
        "project": {"name": p.name, "root": str(p.root), "config": p.config()},
        "daemon": db.kv("daemon", {}), "paused": db.kv("paused", False),
        "gates": {k: {**g, "detail": gate_detail(g, now)} for k, g in db.kv("gates", {}).items()},
        "heartbeat": heartbeat(p), "heartbeat_stale_s": HEARTBEAT_STALE_S, "disk_low": db.kv("disk_low"),
        "disk": db.kv("disk"), "worktrees_kept": db.kv("worktrees_kept"),
        "tasks": tasks, "runs": runs,
        "issues": db.q("SELECT id,source,title,severity,status,count,first_seen,last_seen,task FROM issues "
                       "WHERE status IN ('open','tracking') ORDER BY last_seen DESC LIMIT 100"),
        "schedules": sched.with_costs(db),
        "attention": attention(db, now),
        "feed": alerts.feed(db, now),
        "offline_help": offline_help(p.name),
        "budget": bud.history(db),
        "coordinator": db.kv("last_coordinator_summary", {}),
        "health": health(p, db, now=now),
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
                mid = db.post("in", text, chat="web", channel="web", kind="user")
                db.x("INSERT OR IGNORE INTO chats(id,created,label,last_active) VALUES('web',?, 'web app', ?)",
                     (time.time(), time.time()))
                return self._send(200, {"id": mid})
            if url.path.startswith("/api/schedule/"):
                name = url.path.rsplit("/", 1)[-1]
                row = db.one("SELECT * FROM schedules WHERE name=?", (name,))
                if not row:
                    return self._send(404, {"error": "no schedule"})
                if "enabled" in body:
                    db.x("UPDATE schedules SET enabled=? WHERE name=?", (int(bool(body["enabled"])), name))
                if "budget_usd_day" in body:
                    v = body["budget_usd_day"]
                    db.x("UPDATE schedules SET budget_usd_day=? WHERE name=?", (None if v in (None, "") else float(v), name))
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
                p.set_config(key, USER_SETTABLE[key](body.get("value")))
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "unknown endpoint"})
        finally:
            db.close()


def serve(daemon) -> None:
    p = daemon.p
    cfg = p.config()
    port = int(cfg.get("web", {}).get("port") or 0) or free_port()
    if not cfg.get("web", {}).get("port"):
        p.set_config("web.port", port)
    Handler.daemon_ref = daemon
    token(p)
    httpd = ThreadingHTTPServer((cfg.get("web", {}).get("bind", "127.0.0.1"), port), Handler)
    httpd.daemon_threads = True
    db = DB(p.state / "project.db")          # this thread's own connection; SQLite objects are per-thread
    db.set_kv("web", {"port": port, "bind": cfg.get("web", {}).get("bind", "127.0.0.1")})
    db.close()
    httpd.serve_forever()
