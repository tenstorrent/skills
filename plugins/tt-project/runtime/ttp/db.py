# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Project state: one SQLite file per project, shared by the daemon, the CLI and the web app."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

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
        self._after_commit: list[Callable[[], Any]] = []
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

    @contextmanager
    def tx(self) -> Iterator[None]:
        """All writes inside commit together or not at all. Nested use joins the outer transaction."""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._after_commit.clear()
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        pending, self._after_commit = self._after_commit, []
        for fn in pending:
            fn()

    def after_commit(self, fn: Callable[[], Any]) -> None:
        """Run fn once the open transaction commits, or now outside one. Slow side effects go here,
        so they never hold the write lock that every other process is waiting on."""
        if self.conn.in_transaction:
            self._after_commit.append(fn)
        else:
            fn()

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

    def unread_for_chat(self, chat: str, after: int, min_severity: str = "normal",
                        upto: int | None = None) -> list[dict]:
        """Outbound messages this chat has not seen: its own replies plus broadcasts at or above
        its severity floor. Replies addressed to another chat are never shown here."""
        floor = SEVERITY_RANK.get(min_severity, 1)
        rows = self.q("SELECT * FROM messages WHERE direction='out' AND id>? AND id<=? AND (chat=? OR chat IS NULL) "
                      "ORDER BY id", (after, upto if upto is not None else 2**62, chat))
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
        return [r for r in rows if all(d in done for d in dependency_ids(r))]

    def dead_dependencies(self) -> list[tuple[dict, Any, str]]:
        """Queued tasks waiting on a dependency that can no longer finish: (task, dependency, why)."""
        rows = self.q("SELECT * FROM tasks WHERE status='queued' AND depends_on NOT IN ('', '[]')")
        if not rows:
            return []
        states = {r["id"]: r["status"] for r in self.q("SELECT id, status FROM tasks")}
        out = []
        for t in rows:
            dead = _first_dead(dependency_ids(t), states)
            if dead:
                out.append((t, *dead))
        return out

    def dead_dependency(self, deps: list) -> tuple[Any, str] | None:
        """The first of `deps` that can no longer finish and why, or None."""
        states = {r["id"]: r["status"] for r in self.q("SELECT id, status FROM tasks")}
        return _first_dead(deps, states)

    def dependency_cycle(self, task_id: int, deps: list) -> bool:
        """True when `task_id` depending on `deps` would close a loop."""
        edges = {r["id"]: dependency_ids(r) for r in self.q("SELECT id, depends_on FROM tasks")}
        seen, todo = set(), list(deps)
        while todo:
            d = todo.pop()
            if d == task_id:
                return True
            if d not in seen:
                seen.add(d)
                todo.extend(edges.get(d, []))
        return False

    # money ---------------------------------------------------------------------------------
    def spend(self, provider: str, usd: float, source: str, account: str = "",
              estimated: bool = False, tokens_in: int = 0, tokens_out: int = 0, ts: float | None = None) -> None:
        self.x("INSERT INTO ledger(ts,provider,account,source,usd,estimated,tokens_in,tokens_out) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (ts or time.time(), provider, account, source, float(usd or 0), int(estimated), tokens_in, tokens_out))

    def spent_since(self, since_ts: float, provider: str | None = None, exclude: Mapping[str, float] | None = None,
                    estimated_only: bool = False) -> float:
        sql, args = "SELECT COALESCE(SUM(usd),0) s FROM ledger WHERE ts>=?", [since_ts]
        if provider:
            sql, args = sql + " AND provider=?", args + [provider]
        # exclude maps provider -> time up to which its rows are left out.
        for prov, until in (exclude or {}).items():
            sql, args = sql + " AND NOT (provider=? AND ts<=?)", args + [prov, until]
        if estimated_only:
            sql += " AND estimated=1"
        r = self.one(sql, args)
        return float(r["s"]) if r else 0.0


SEVERITY_RANK = {"info": 0, "low": 0, "normal": 1, "high": 2, "critical": 3}


def chat_floor(chat_min: str | None, project_min: str | None) -> str:
    """A chat's severity floor: the project floor (notify.chat_min_severity) applies to every chat,
    and a chat can only raise it."""
    return max((chat_min or "normal", project_min or "normal"), key=lambda n: SEVERITY_RANK.get(n, -1))
RESULT_MAX_CHARS = 20000


def dependency_ids(task: dict) -> list:
    """A task's dependencies as ids; an entry that is not an id comes back as None (never done)."""
    try:
        deps = json.loads(task["depends_on"] or "[]")
    except ValueError:
        return [None]
    out = []
    for d in deps if isinstance(deps, list) else [deps]:
        try:
            out.append(int(d))
        except (TypeError, ValueError):
            out.append(None)
    return out


def continues_id(task: dict) -> int | None:
    """The task this one continues (from its `continues:<id>` label), or None."""
    try:
        labels = json.loads(task["labels"] or "[]")
    except ValueError:
        return None
    for lb in labels if isinstance(labels, list) else []:
        if isinstance(lb, str) and lb.startswith("continues:") and lb[10:].isdigit():
            return int(lb[10:])
    return None


def _first_dead(deps: list, states: dict) -> tuple[Any, str] | None:
    for d in deps:
        why = "does not exist" if states.get(d) is None else states[d]
        if why in ("does not exist", "failed", "cancelled"):
            return d, why
    return None


def dump_result(result: dict, limit: int = RESULT_MAX_CHARS) -> str:
    """A hand-off as JSON of at most `limit` chars for tasks.result. Values are shortened, never the
    serialized text, so every reader can parse it; the full hand-off stays in the run directory.
    The summary is what readers show and status/waits drive retries, so those go last: other fields
    shrink first, then the largest of them are dropped, then the summary shrinks."""
    text = json.dumps(result, ensure_ascii=False)
    if len(text) <= limit:
        return text
    summary = str(result.get("summary") or "")
    rest = {k: v for k, v in result.items() if k != "summary"}
    cap, summary_cap = limit, limit // 2
    while True:
        text = json.dumps({"summary": _clip(summary, summary_cap), **_clip(rest, cap), "clipped": True},
                          ensure_ascii=False)
        if len(text) <= limit:
            return text
        droppable = [k for k in rest if k not in ("status", "waits")]
        if cap > 64:
            cap = max(cap // 2, 64)
        elif droppable:
            del rest[max(droppable, key=lambda k: len(json.dumps(_clip(rest[k], cap), ensure_ascii=False)))]
        elif summary_cap > 64:
            summary_cap = max(summary_cap // 2, 64)
        else:
            last = {"summary": summary[:64], "status": str(rest.get("status") or "")[:64], "clipped": True}
            if isinstance(rest.get("waits"), int):
                last["waits"] = rest["waits"]
            return json.dumps(last, ensure_ascii=False)


def _clip(value: Any, cap: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= cap else value[:cap] + "…"
    if isinstance(value, list):
        return [_clip(v, cap) for v in value[:cap]]
    if isinstance(value, dict):
        return {k: _clip(v, cap) for k, v in list(value.items())[:cap]}
    return value


def load_result(text: str | None) -> dict:
    """A task's stored hand-off, {} when there is none or it cannot be read. Rows that older versions
    cut mid-JSON still yield their summary, so one bad row never breaks a reader."""
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        pass
    m = re.match(r'\s*\{\s*"summary"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    if not m:
        return {}
    try:
        return {"summary": json.loads('"' + re.sub(r"\\u[0-9a-fA-F]{0,3}$", "", m.group(1)) + '"')}
    except ValueError:
        return {}
