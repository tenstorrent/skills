# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Project state: one SQLite file per project, shared by the daemon, the CLI and the web app."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY, created REAL, label TEXT, host TEXT,
  last_read INTEGER DEFAULT 0, last_active REAL, min_severity TEXT DEFAULT 'normal');

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
  chat TEXT,                       -- NULL on an outbound message = broadcast to every chat
  channel TEXT NOT NULL DEFAULT 'chat',
  kind TEXT NOT NULL DEFAULT 'user',
  severity TEXT NOT NULL DEFAULT 'normal',
  text TEXT NOT NULL, ref TEXT, handled INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS messages_unhandled ON messages(direction, handled);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, updated REAL,
  title TEXT NOT NULL, spec TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'work',
  status TEXT NOT NULL DEFAULT 'queued', priority INTEGER NOT NULL DEFAULT 3,
  tier TEXT NOT NULL DEFAULT 'standard', provider TEXT, budget_usd REAL, spent_usd REAL DEFAULT 0,
  attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3, parent INTEGER, reply_chat TEXT,
  origin TEXT NOT NULL DEFAULT 'coordinator', branch TEXT, pr_url TEXT, result TEXT,
  blocked_reason TEXT, depends_on TEXT NOT NULL DEFAULT '[]', labels TEXT NOT NULL DEFAULT '[]',
  not_before REAL);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, priority);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task INTEGER, role TEXT NOT NULL,
  provider TEXT, model TEXT, effort TEXT, account TEXT,
  started REAL, ended REAL, pid INTEGER, boot_id TEXT, dir TEXT,
  status TEXT NOT NULL DEFAULT 'running', exit_code INTEGER, progress_ts REAL,
  cost_usd REAL DEFAULT 0, cost_estimated INTEGER DEFAULT 0,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0, note TEXT);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, source TEXT NOT NULL,
  kind TEXT NOT NULL, fingerprint TEXT, severity TEXT NOT NULL DEFAULT 'normal',
  text TEXT NOT NULL, data TEXT, status TEXT NOT NULL DEFAULT 'new', task INTEGER);
CREATE INDEX IF NOT EXISTS events_status ON events(status);

CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT UNIQUE, source TEXT,
  first_seen REAL, last_seen REAL, count INTEGER DEFAULT 1, title TEXT,
  severity TEXT DEFAULT 'normal', status TEXT DEFAULT 'open', task INTEGER, screen TEXT);

CREATE TABLE IF NOT EXISTS schedules (
  name TEXT PRIMARY KEY, kind TEXT NOT NULL, every_s INTEGER NOT NULL, at TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, budget_usd_day REAL, description TEXT,
  payload TEXT NOT NULL DEFAULT '{}', last_run REAL, next_run REAL, last_status TEXT);

CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, provider TEXT, account TEXT,
  source TEXT, usd REAL NOT NULL DEFAULT 0, estimated INTEGER DEFAULT 0,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ledger_ts ON ledger(ts);

CREATE TABLE IF NOT EXISTS snapshots (
  ts REAL NOT NULL, provider TEXT, account TEXT, window TEXT,
  utilization REAL, resets_at REAL);
CREATE INDEX IF NOT EXISTS snapshots_ts ON snapshots(ts);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, ts REAL);
"""

TERMINAL_TASK_STATES = ("done", "failed", "cancelled")


class DB:
    """Thin wrapper: short transactions, WAL, rows as dicts. Safe to open from many processes."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        if self.meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        self.conn.close()

    # generic -------------------------------------------------------------------------------
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def one(self, sql: str, args: Iterable[Any] = ()) -> dict | None:
        r = self.conn.execute(sql, tuple(args)).fetchone()
        return dict(r) if r else None

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        cur = self.conn.execute(sql, tuple(args))
        return cur.lastrowid or cur.rowcount

    def meta(self, key: str) -> str | None:
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))

    def kv(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_kv(self, key: str, value: Any) -> None:
        self.x("INSERT INTO kv(key,value,ts) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET "
               "value=excluded.value, ts=excluded.ts", (key, json.dumps(value), time.time()))

    # messages ------------------------------------------------------------------------------
    def post(self, direction: str, text: str, chat: str | None = None, channel: str = "chat",
             kind: str = "user", severity: str = "normal", ref: str | None = None,
             handled: bool = False) -> int:
        return self.x("INSERT INTO messages(ts,direction,chat,channel,kind,severity,text,ref,handled) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (time.time(), direction, chat, channel, kind, severity, text, ref, int(handled)))

    def unread_for_chat(self, chat: str, after: int, min_severity: str = "normal") -> list[dict]:
        """Outbound messages this chat has not seen: its own replies plus broadcasts at or above
        its severity floor. Replies addressed to another chat are never shown here."""
        floor = SEVERITY_RANK.get(min_severity, 1)
        rows = self.q("SELECT * FROM messages WHERE direction='out' AND id>? AND (chat=? OR chat IS NULL) "
                      "ORDER BY id", (after, chat))
        return [r for r in rows if r["chat"] == chat or SEVERITY_RANK.get(r["severity"], 1) >= floor]

    # tasks ---------------------------------------------------------------------------------
    def add_task(self, title: str, spec: str = "", **kw: Any) -> int:
        now = time.time()
        cols = {"created": now, "updated": now, "title": title.strip()[:200], "spec": spec}
        for k in ("kind", "status", "priority", "tier", "provider", "budget_usd", "max_attempts",
                  "parent", "reply_chat", "origin", "branch", "not_before"):
            if kw.get(k) is not None:
                cols[k] = kw[k]
        for k in ("depends_on", "labels"):
            if kw.get(k) is not None:
                cols[k] = json.dumps(list(kw[k]))
        keys = ",".join(cols)
        return self.x(f"INSERT INTO tasks({keys}) VALUES({','.join('?' * len(cols))})", cols.values())

    def update_task(self, task_id: int, **kw: Any) -> None:
        if not kw:
            return
        for k in ("depends_on", "labels"):
            if k in kw and not isinstance(kw[k], str):
                kw[k] = json.dumps(list(kw[k]))
        kw["updated"] = time.time()
        sets = ",".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE tasks SET {sets} WHERE id=?", [*kw.values(), task_id])

    def task(self, task_id: int) -> dict | None:
        return self.one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def ready_tasks(self) -> list[dict]:
        """Queued tasks whose dependencies are all finished, best priority first."""
        now = time.time()
        rows = self.q("SELECT * FROM tasks WHERE status='queued' AND (not_before IS NULL OR not_before<=?) "
                      "ORDER BY priority, id", (now,))
        done = {r["id"] for r in self.q("SELECT id FROM tasks WHERE status='done'")}
        return [r for r in rows if all(d in done for d in json.loads(r["depends_on"] or "[]"))]

    # money ---------------------------------------------------------------------------------
    def spend(self, provider: str, usd: float, source: str, account: str = "",
              estimated: bool = False, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.x("INSERT INTO ledger(ts,provider,account,source,usd,estimated,tokens_in,tokens_out) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (time.time(), provider, account, source, float(usd or 0), int(estimated), tokens_in, tokens_out))

    def spent_since(self, since_ts: float, provider: str | None = None) -> float:
        if provider:
            r = self.one("SELECT COALESCE(SUM(usd),0) s FROM ledger WHERE ts>=? AND provider=?",
                         (since_ts, provider))
        else:
            r = self.one("SELECT COALESCE(SUM(usd),0) s FROM ledger WHERE ts>=?", (since_ts,))
        return float(r["s"]) if r else 0.0


SEVERITY_RANK = {"info": 0, "low": 0, "normal": 1, "high": 2, "critical": 3}
