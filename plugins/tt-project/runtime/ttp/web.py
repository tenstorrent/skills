# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The project's web app: a JSON API plus a static single-page UI, served by the daemon.
Bound to localhost by default and guarded by a per-project token; reach it from another machine
through an SSH local forward (see the `project` skill's tunnels notes)."""
from __future__ import annotations

import json
import secrets
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import budget as bud
from . import schedule as sched
from .db import DB
from .project import Project

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


def state_payload(p: Project, db: DB) -> dict:
    now = time.time()
    tasks = db.q("SELECT id,title,kind,status,priority,tier,provider,budget_usd,spent_usd,attempts,origin,branch,"
                 "pr_url,blocked_reason,created,updated,result FROM tasks WHERE status NOT IN ('done','failed',"
                 "'cancelled') OR updated>? ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'blocked' THEN 1 "
                 "WHEN 'review' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END, priority, id DESC LIMIT 200",
                 (now - 7 * 86400,))
    for t in tasks:
        try:
            t["result"] = json.loads(t["result"]).get("summary", "")[:600] if t["result"] else ""
        except ValueError:
            t["result"] = ""
    runs = db.q("SELECT id,task,role,provider,model,effort,status,started,ended,cost_usd FROM runs "
                "ORDER BY id DESC LIMIT 40")
    return {
        "project": {"name": p.name, "root": str(p.root), "config": p.config()},
        "daemon": db.kv("daemon", {}), "paused": db.kv("paused", False), "gates": db.kv("gates", {}),
        "tasks": tasks, "runs": runs,
        "issues": db.q("SELECT id,source,title,severity,status,count,first_seen,last_seen,task FROM issues "
                       "WHERE status IN ('open','tracking') ORDER BY last_seen DESC LIMIT 100"),
        "schedules": sched.with_costs(db),
        "attention": db.q("SELECT id,ts,kind,severity,text FROM messages WHERE direction='out' AND chat IS NULL "
                          "AND ((kind='ask' AND handled=0) OR (kind='alert' AND ts>?)) "
                          "AND severity IN ('high','critical') ORDER BY id DESC LIMIT 20", (now - 86400,)),
        "budget": bud.history(db),
        "coordinator": db.kv("last_coordinator_summary", {}),
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
                rows = db.q("SELECT id,ts,direction,chat,channel,kind,severity,text FROM messages WHERE id>? AND "
                            "(chat IS NULL OR chat='web' OR channel='web') ORDER BY id LIMIT 300", (after,))
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
                if body.get("status") in ("cancelled", "queued"):
                    db.update_task(tid, status=body["status"])
                    if body["status"] == "cancelled":
                        for r in db.q("SELECT dir FROM runs WHERE task=? AND status='running'", (tid,)):
                            Path(r["dir"], "STOP").touch()
                if body.get("priority"):
                    db.update_task(tid, priority=int(body["priority"]))
                return self._send(200, {"ok": True})
            if url.path == "/api/pause":
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
