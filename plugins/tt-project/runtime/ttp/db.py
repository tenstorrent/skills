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

from . import billing

SCHEMA_VERSION = 1
PAUSED_RESOURCES_KEY = "paused_resources"   # kv: see DB.paused_resources
SHARED_SEEN_KEY = "shared_pauses_seen"   # kv: {resource: pause} of the shared pauses this project acted on
WATCHER_ISSUES_MIGRATION = "watcher_issues_per_condition"   # meta: set once DB._migrate has run
PROVENANCE_MIGRATION = "message_provenance"   # meta: set once inbound messages have their provenance
PUSH_QUEUE_MIGRATION = "push_queue"   # meta: set once the push queue's tables exist (see pushq.py)
LEDGER_ACCOUNT_MIGRATION = "ledger_account"   # meta: set once old ledger rows got their run's account

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
  text TEXT NOT NULL, ref TEXT, handled INTEGER NOT NULL DEFAULT 0,
  provenance TEXT,                 -- inbound: the way it came in, set by the writer (prguard.PROVENANCES)
  ext_id TEXT);                    -- its id in its channel: a Slack message's ts (outbound: once posted)
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
  cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0, note TEXT, session_id TEXT);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, source TEXT NOT NULL,
  kind TEXT NOT NULL, fingerprint TEXT, severity TEXT NOT NULL DEFAULT 'normal',
  text TEXT NOT NULL, data TEXT, status TEXT NOT NULL DEFAULT 'new', task INTEGER);
CREATE INDEX IF NOT EXISTS events_status ON events(status);

CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT UNIQUE, source TEXT,
  first_seen REAL, last_seen REAL, count INTEGER DEFAULT 1, title TEXT,
  severity TEXT DEFAULT 'normal', status TEXT DEFAULT 'open', task INTEGER, screen TEXT,
  closed REAL, cleared_why TEXT, lifecycle TEXT, subject TEXT);

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

-- One row per Jev call: its use, decision, cost, estimated cost avoided, whether it changed the rules'
-- decision (NULL: not known) and later outcome (jevuse.py).
CREATE TABLE IF NOT EXISTS jev_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, use TEXT NOT NULL, ref TEXT, decision TEXT,
  cost_usd REAL NOT NULL DEFAULT 0, avoided_usd REAL NOT NULL DEFAULT 0, settle_at REAL,
  outcome TEXT, outcome_ts REAL, note TEXT, changed INTEGER);
CREATE INDEX IF NOT EXISTS jev_calls_use ON jev_calls(ts, use);

-- One row per episode of a high alert with a condition key; cleared is set, never deleted.
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, raised REAL NOT NULL, last REAL,
  message INTEGER, severity TEXT, text TEXT, cleared REAL, cleared_why TEXT);
CREATE INDEX IF NOT EXISTS alerts_key ON alerts(key, cleared);
"""

TERMINAL_TASK_STATES = ("done", "failed", "cancelled")
# A review that asked for changes is a review that worked. It is stored 'failed', so the change it
# reviewed stays gated (its fix and re-review follow), and its hand-off status says changes_needed:
# readers show and count it on its own, never as a failure (task_outcome, DB.status_counts).
CHANGES_NEEDED = "changes_needed"
# The push queue (pushq.py). A review whose approval waits in it has the task status 'pushing'.
PUSH_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS push_queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task INTEGER NOT NULL, run INTEGER, branch TEXT NOT NULL, head TEXT NOT NULL, target TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'approved',
  batch TEXT, tries INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL, updated REAL NOT NULL,
  pushed_sha TEXT, version TEXT, detail TEXT, landed_sha TEXT);
CREATE INDEX IF NOT EXISTS push_queue_status ON push_queue(status, id);
CREATE TABLE IF NOT EXISTS push_batches(
  id TEXT PRIMARY KEY, marker TEXT NOT NULL, target TEXT NOT NULL,
  started REAL NOT NULL, ended REAL,
  outcome TEXT, pushed_sha TEXT, version TEXT, tip TEXT, check_runs INTEGER, check_s REAL,
  after_push TEXT, after_tries INTEGER NOT NULL DEFAULT 0, finalized REAL, after_finalized REAL);
"""
# Open asks older than this no longer hold back idle-slot wakes. They are still shown until answered.
OPEN_ASK_MAX_AGE_S = 14 * 86400


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
        self._migrate()

    def _migrate(self) -> None:
        """Columns added after a table was first created, and one-off fixes of old rows."""
        self._migrate_issues()
        self._migrate_provenance()
        self._migrate_push_queue()
        self._migrate_ledger_account()
        self._migrate_run_session()
        self._migrate_jev_changed()
        self._migrate_issue_lifecycle()
        self._migrate_push_landed()

    def _migrate_ledger_account(self) -> None:
        """Ledger rows written without an account take the account of the run they booked: the run
        of the same provider that ended when the row was written (spend is booked at the run's end).
        Rows no run explains keep none; billing.py decides them by the provider's plan readings."""
        if self.meta(LEDGER_ACCOUNT_MIGRATION) is not None:
            return
        with self.tx():
            if self.meta(LEDGER_ACCOUNT_MIGRATION) is not None:
                return
            for row in self.q("SELECT id, provider, ts FROM ledger WHERE COALESCE(account,'')=''"):
                run = self.one("SELECT account FROM runs WHERE provider=? AND COALESCE(account,'')!='' AND "
                               "ended BETWEEN ? AND ? ORDER BY ABS(ended-?) LIMIT 1",
                               (row["provider"], row["ts"] - 2, row["ts"] + 2, row["ts"]))
                if run:
                    self.x("UPDATE ledger SET account=? WHERE id=?", (run["account"], row["id"]))
            self.set_meta(LEDGER_ACCOUNT_MIGRATION, str(time.time()))

    def _migrate_run_session(self) -> None:
        # The agent's session id of each run (localspend.py tells tt-project's sessions from the
        # user's own by it). Older runs keep NULL; their output.jsonl still names it.
        if "session_id" in {r["name"] for r in self.q("PRAGMA table_info(runs)")}:
            return
        with self.tx():
            if "session_id" not in {r["name"] for r in self.q("PRAGMA table_info(runs)")}:
                self.x("ALTER TABLE runs ADD COLUMN session_id TEXT")

    def _migrate_jev_changed(self) -> None:
        # Whether a Jev call changed the rules' decision (jevuse.review). Older calls keep NULL: not known.
        if "changed" in {r["name"] for r in self.q("PRAGMA table_info(jev_calls)")}:
            return
        with self.tx():
            if "changed" not in {r["name"] for r in self.q("PRAGMA table_info(jev_calls)")}:
                self.x("ALTER TABLE jev_calls ADD COLUMN changed INTEGER")

    def _migrate_issue_lifecycle(self) -> None:
        # Receipt sources (a command schedule with issue_lifecycle explicit_clear) mark each issue a
        # receipt or an error, with the subject it was reported under (see screen.py).
        if "lifecycle" in {r["name"] for r in self.q("PRAGMA table_info(issues)")}:
            return
        with self.tx():
            have = {r["name"] for r in self.q("PRAGMA table_info(issues)")}
            for col in ("lifecycle", "subject"):
                if col not in have:
                    self.x(f"ALTER TABLE issues ADD COLUMN {col} TEXT")

    def _migrate_push_landed(self) -> None:
        # Each pushed or landed row's own commit on the branch (pushq._apply; `ttp landed`). Older
        # rows keep NULL: their pushed_sha, the batch's head, contains them.
        if "landed_sha" in {r["name"] for r in self.q("PRAGMA table_info(push_queue)")}:
            return
        with self.tx():
            if "landed_sha" not in {r["name"] for r in self.q("PRAGMA table_info(push_queue)")}:
                self.x("ALTER TABLE push_queue ADD COLUMN landed_sha TEXT")

    def _migrate_push_queue(self) -> None:
        if self.meta(PUSH_QUEUE_MIGRATION) is not None:
            return
        with self.tx():
            if self.meta(PUSH_QUEUE_MIGRATION) is not None:
                return
            # One statement at a time: executescript would commit the open transaction.
            for stmt in PUSH_QUEUE_SCHEMA.split(";"):
                if stmt.strip():
                    self.x(stmt)
            self.set_meta(PUSH_QUEUE_MIGRATION, str(time.time()))

    def _migrate_provenance(self) -> None:
        if self.meta(PROVENANCE_MIGRATION) is not None:
            return
        with self.tx():
            if self.meta(PROVENANCE_MIGRATION) is not None:
                return
            have = {r["name"] for r in self.q("PRAGMA table_info(messages)")}
            for col in ("provenance", "ext_id"):
                if col not in have:
                    self.x(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
            # Older inbound messages by the channel they came in on. A Slack message's ref was its ts
            # (or its thread's); pr_approve checks it against Slack, so a wrong one is only refused.
            self.x("UPDATE messages SET provenance=CASE channel WHEN 'slack' THEN 'slack' WHEN 'web' THEN 'web' "
                   "WHEN 'system' THEN 'system' ELSE 'cli-legacy' END, "
                   "ext_id=CASE channel WHEN 'slack' THEN ref END WHERE direction='in' AND provenance IS NULL")
            self.set_meta(PROVENANCE_MIGRATION, str(time.time()))

    def _migrate_issues(self) -> None:
        if self.meta(WATCHER_ISSUES_MIGRATION) is not None:
            return
        with self.tx():
            if self.meta(WATCHER_ISSUES_MIGRATION) is not None:
                return
            have = {r["name"] for r in self.q("PRAGMA table_info(issues)")}
            for col, typ in (("closed", "REAL"), ("cleared_why", "TEXT")):
                if col not in have:
                    self.x(f"ALTER TABLE issues ADD COLUMN {col} {typ}")
            # Watcher issues used to be keyed by their whole text, so each run's counts made a new one
            # that nothing ever closed. They are now kept one per condition (see screen.py).
            self.x("UPDATE issues SET status='fixed', closed=?, cleared_why=? WHERE status='open' AND "
                   "source LIKE 'watcher:%'", (time.time(), "closed by the one-off migration to one issue "
                                                            "per watcher condition"))
            self.set_meta(WATCHER_ISSUES_MIGRATION, str(time.time()))

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

    def paused_resources(self, shared: bool = True) -> dict[str, dict]:
        """Resources paused by name: {"reason", "since", "by"}. Kept in the database, so a pause
        outlives daemon restarts and reboots until someone lifts it. With shared, pauses of the
        project's shared resources (see shared.py) are in it too, marked "shared" and naming the
        project that set them."""
        v = self.kv(PAUSED_RESOURCES_KEY, {})
        v = v if isinstance(v, dict) else {}
        if shared:
            from . import shared as sh
            v = {**v, **sh.paused_for_db(self.path, self.kv(SHARED_SEEN_KEY, {}))}
        return v

    def boots(self, since: float) -> list[dict]:
        """Reboots the daemon recorded since `since`, oldest first: ts (the boot time where known)
        plus the boot event's data (runs lost, resources held at the last heartbeat before it)."""
        rows = self.q("SELECT ts, data FROM events WHERE source='host' AND kind='boot' AND ts>? ORDER BY ts, id",
                      (since,))
        return [{**json.loads(r["data"] or "{}"), "ts": r["ts"]} for r in rows]

    # messages ------------------------------------------------------------------------------
    def post(self, direction: str, text: str, chat: str | None = None, channel: str = "chat",
             kind: str = "user", severity: str = "normal", ref: str | None = None,
             handled: bool = False, provenance: str | None = None, ext_id: str | None = None) -> int:
        """`provenance` (inbound): how the message came in, set by the code that received it, never
        taken from the sender. Only some count as the user's word for a PR approval (prguard)."""
        from . import alerts
        ts = time.time()
        with self.tx():
            mid = self.x("INSERT INTO messages(ts,direction,chat,channel,kind,severity,text,ref,handled,provenance,"
                         "ext_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (ts, direction, chat, channel, kind, severity, text, ref, int(handled), provenance, ext_id))
            if direction == "out" and chat is None and alerts.tracked(kind, severity, ref):
                alerts.open_episode(self, ref, ts, mid, severity, text)
        return mid

    def unread_for_chat(self, chat: str, after: int, min_severity: str = "normal",
                        upto: int | None = None) -> list[dict]:
        """Outbound messages this chat has not seen: its own replies plus broadcasts at or above
        its severity floor. Replies addressed to another chat are never shown here, nor quiet
        broadcasts (a machine-wide alert another project already sent)."""
        floor = SEVERITY_RANK.get(min_severity, 1)
        rows = self.q("SELECT * FROM messages WHERE direction='out' AND id>? AND id<=? AND (chat=? OR chat IS NULL) "
                      "ORDER BY id", (after, upto if upto is not None else 2**62, chat))
        return [r for r in rows if r["chat"] == chat
                or (SEVERITY_RANK.get(r["severity"], 1) >= floor and r["channel"] != "quiet")]

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

    def status_counts(self) -> dict[str, int]:
        """Tasks by status, with reviews that asked for changes counted as changes_needed, not failed."""
        counts = {r["status"]: r["n"] for r in self.q("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
        n = sum(task_outcome(t) == CHANGES_NEEDED for t in self.q(
            "SELECT status, kind, result FROM tasks WHERE status='failed' AND kind='review' AND result LIKE ?",
            (f"%{CHANGES_NEEDED}%",)))
        if n:
            counts["failed"] -= n
            if not counts["failed"]:
                del counts["failed"]
            counts[CHANGES_NEEDED] = n
        return counts

    def ready_tasks(self) -> list[dict]:
        """Queued tasks whose dependencies are all finished, best priority first. A task deferred
        with `start_when` is not ready until its probe passes (see deferral)."""
        now = time.time()
        rows = self.q("SELECT * FROM tasks WHERE status='queued' AND (not_before IS NULL OR not_before<=?) "
                      "ORDER BY priority, id", (now,))
        unmet = self.unmet_dependencies(rows)
        return [r for r in rows if not unmet[r["id"]] and "when" not in deferral(r)]

    def unmet_dependencies(self, tasks: list[dict]) -> dict[int, list]:
        """Task id -> the dependencies it still waits on. Only 'done' satisfies a dependency, with
        one exception: a review's dependency on the task it reviews, while that task waits in
        'review'. Only the review can move it on, so waiting for 'done' would stall both; any
        other prerequisite of the review still has to finish."""
        states = {r["id"]: r for r in self.q("SELECT id, status, branch FROM tasks WHERE status IN ('done','review')")}
        out: dict[int, list] = {}
        for t in tasks:
            out[t["id"]] = [d for d in dependency_ids(t) if d not in states or (states[d]["status"] != "done"
                            and not (t.get("kind") == "review" and reviews_task(t, states[d])))]
        return out

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

    def review_since(self) -> dict[int, float]:
        """When each task now in 'review' entered it: its latest hand-off to review, else its last update."""
        rows = self.q("SELECT t.id, t.updated, (SELECT MAX(e.ts) FROM events e WHERE e.task=t.id "
                      "AND e.kind='task_review') AS since FROM tasks t WHERE t.status='review'")
        return {r["id"]: r["since"] or r["updated"] for r in rows}

    def stalled_reviews(self, stall_s: float, now: float) -> list[tuple[dict, float, list[dict]]]:
        """Review tasks that queued tasks depend on, in review longer than `stall_s`:
        (review task, since, its queued dependents). Nothing when `stall_s` is 0."""
        if stall_s <= 0:
            return []
        since = {k: v for k, v in self.review_since().items() if now - v >= stall_s}
        if not since:
            return []
        waiting: dict[int, list[dict]] = {}
        rows = self.q("SELECT * FROM tasks WHERE status='queued' AND depends_on NOT IN ('', '[]') ORDER BY id")
        unmet = self.unmet_dependencies(rows)
        for t in rows:
            for d in set(unmet[t["id"]]):
                if d in since:
                    waiting.setdefault(d, []).append(t)
        return [(self.task(d), since[d], ts) for d, ts in sorted(waiting.items())]

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
                    estimated_only: bool = False, billed: bool = False) -> float:
        """Spend since `since_ts`. With `billed`, only spend whose account was billed by use when it
        was spent (billing.py): what the dollar caps, the global cap and 'actual' count."""
        if billed:
            return sum(billing.billed_by_account(self.conn, since_ts, provider=provider, exclude=exclude,
                                                 estimated_only=estimated_only).values())
        where, args = counted_spend(since_ts, provider=provider, exclude=exclude, estimated_only=estimated_only)
        r = self.one(f"SELECT COALESCE(SUM(usd),0) s FROM ledger WHERE {where}", args)
        return float(r["s"]) if r else 0.0


def counted_spend(since_ts: float, until_ts: float | None = None, provider: str | None = None,
                  exclude: Mapping[str, float] | None = None, estimated_only: bool = False) -> tuple[str, list]:
    """The WHERE clause and its arguments for the ledger rows that count as spend in
    [since_ts, until_ts): the one rule both the project caps (DB.spent_since) and the global daily
    total (globalcap) sum by. `exclude` maps provider -> time up to which its rows are left out."""
    sql, args = "ts>=?", [since_ts]
    if until_ts is not None:
        sql, args = sql + " AND ts<?", args + [until_ts]
    if provider:
        sql, args = sql + " AND provider=?", args + [provider]
    for prov, until in (exclude or {}).items():
        sql, args = sql + " AND NOT (provider=? AND ts<=?)", args + [prov, until]
    if estimated_only:
        sql += " AND estimated=1"
    return sql, args


SEVERITY_RANK = {"info": 0, "low": 0, "normal": 1, "high": 2, "critical": 3}


def host_line(boots: list[dict]) -> str:
    """'host: N reboots in 24 h (last HH:MM), M runs lost ($X)' for the boots of the last day, as
    DB.boots returns them; '' when there were none."""
    if not boots:
        return ""
    lost = sum(len(b.get("lost") or []) for b in boots)
    usd = sum(float(b.get("lost_usd") or 0) for b in boots)
    last = time.strftime("%H:%M", time.localtime(boots[-1]["ts"]))
    return (f"host: {len(boots)} reboot{'' if len(boots) == 1 else 's'} in 24 h (last {last}), "
            f"{lost} run{'' if lost == 1 else 's'} lost (${usd:.2f})")


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


REVIEW_TITLE = re.compile(r"\s*review\s+(?:#|t)(\d+)(?!\d)", re.I)


def review_subject(review: dict) -> int | None:
    """The id of the task a review reviews, from its `auto_review:<id>` label or a title that
    starts with `Review #<id>` (or `Review t<id>`), the form the daemon and coordinator use."""
    try:
        labels = json.loads(review.get("labels") or "[]")
    except (ValueError, TypeError):
        labels = []
    for lb in labels if isinstance(labels, list) else []:
        if isinstance(lb, str) and lb.startswith("auto_review:") and lb[12:].isdigit():
            return int(lb[12:])
    m = REVIEW_TITLE.match(review.get("title") or "")
    return int(m.group(1)) if m else None


def reviews_task(review: dict, task: dict) -> bool:
    """True when `review` reviews `task`: its subject (see review_subject) is the task or, only when
    it names no subject that way, its spec names the task's branch. Ids mentioned anywhere else
    (a stacked base, a plan step, a copied spec) never count."""
    subject = review_subject(review)
    if subject is not None:
        return subject == task["id"]
    branch = task.get("branch")
    return bool(branch and re.search(rf"(?<![\w/.-]){re.escape(branch)}(?![\w/-])", review.get("spec") or ""))


DEFER_LABELS = ("start_after", "start_when", "start_why", "deferred_since")


def deferral(task: dict) -> dict:
    """A deferred task's start condition, from its labels: `after` (epoch seconds, kept in
    `not_before` too), `when` (a shell probe that must exit 0 first), `why` (plain words for the
    probe) and `since` (when it was deferred). Empty when the task is not deferred."""
    try:
        labels = json.loads(task["labels"] or "[]")
    except (ValueError, KeyError, TypeError):
        return {}
    out: dict = {}
    for lb in labels if isinstance(labels, list) else []:
        key, _, val = lb.partition(":") if isinstance(lb, str) else ("", "", "")
        if key == "start_when" and val.strip():
            out["when"] = val
        elif key == "start_why" and val.strip():
            out["why"] = val
        elif key in ("start_after", "deferred_since"):
            try:
                out["after" if key == "start_after" else "since"] = float(val)
            except ValueError:
                pass
    return out


def without_deferral(labels: list) -> list:
    return [lb for lb in labels if not (isinstance(lb, str) and lb.partition(":")[0] in DEFER_LABELS)]


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


def task_outcome(task: dict) -> str:
    """The task's status as readers show it: changes_needed for a review that asked for changes."""
    if task.get("status") == "failed" and task.get("kind") == "review" \
            and load_result(task.get("result")).get("status") == CHANGES_NEEDED:
        return CHANGES_NEEDED
    return task.get("status") or ""


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
