# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Project state: one SQLite file per project, shared by the daemon, the CLI and the web app."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from bisect import bisect_right
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from . import billing

SCHEMA_VERSION = 1
STUCK_STATUSES = "('blocked','waiting','review')"   # task statuses a stuck episode spans (tasks.stuck_since)
# Why a task's tier is what it is (tasks.tier_source), shown by `ttp task show` for cost reviews.
TIER_SOURCES = {
    "task_add": "given when it was queued", "default": "queued without one (standard)",
    "user_deep": "the user asked for deep", "parent_copy": "its parent's queued tier (a continue hop)",
    "hop_followup": "lowered by its parent's follow-up", "fix_review_default": "the default for a review fix",
    "review_needs_deep": "the failed review marked the fix as needing deep",
    "retry_tier_up": "raised one tier after a failed try", "effort_pick": "picked from its spec at its first start",
    "review_diff": "sized by the reviewed diff", "failover": "moved to another provider",
}
PAUSED_RESOURCES_KEY = "paused_resources"   # kv: see DB.paused_resources
PAUSE_BOOT_KEY = "pause_until_boot"   # kv: {"boot", "since"} of a `ttp pause --until-reboot`; see DB.set_paused
SHARED_SEEN_KEY = "shared_pauses_seen"   # kv: {resource: pause} of the shared pauses this project acted on
WATCHER_ISSUES_MIGRATION = "watcher_issues_per_condition"   # meta: set once DB._migrate has run
PROVENANCE_MIGRATION = "message_provenance"   # meta: set once inbound messages have their provenance
PUSH_QUEUE_MIGRATION = "push_queue"   # meta: set once the push queue's tables exist (see pushq.py)
LEDGER_ACCOUNT_MIGRATION = "ledger_account"   # meta: set once old ledger rows got their run's account
WATCH_KEY_SINCE = "issue_watch_key_since"   # meta: when issues got watch_key (keyless issues before it predate keys)

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
  ext_id TEXT,                     -- its id in its channel: a Slack message's ts (outbound: once posted)
  context TEXT,                    -- inbound: a relayer's own text sent with the user's (`ttp say --context`):
                                   -- data, never the user's word; `text` keeps only the user's verbatim words
  broadcast TEXT);                 -- inbound: the broadcast id when the user sent it to every project (broadcast.py)
CREATE INDEX IF NOT EXISTS messages_unhandled ON messages(direction, handled);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, updated REAL,
  title TEXT NOT NULL, spec TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'work',
  status TEXT NOT NULL DEFAULT 'queued', priority INTEGER NOT NULL DEFAULT 3,
  tier TEXT NOT NULL DEFAULT 'standard', provider TEXT, budget_usd REAL, spent_usd REAL DEFAULT 0,
  attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3, parent INTEGER, reply_chat TEXT,
  origin TEXT NOT NULL DEFAULT 'coordinator', branch TEXT, pr_url TEXT, result TEXT,
  blocked_reason TEXT, depends_on TEXT NOT NULL DEFAULT '[]', labels TEXT NOT NULL DEFAULT '[]',
  not_before REAL,
  model TEXT, effort TEXT, pin_verify TEXT, -- NULL pins follow the tier; NULL verification follows the project
  assigned_tier TEXT, tier_source TEXT); -- the tier it was queued at, and why its tier is what it is (TIER_SOURCES)
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, priority);
-- A hold's anchor (anchors.py, the label `waits:<kind>:<value>`) lives only while the task is blocked.
-- Every write that leaves it on a task not blocked, raw SQL included, drops it: else a later block
-- for another reason (a budget, a dead dependency, a worker) would be released by the old anchor.
CREATE TRIGGER IF NOT EXISTS tasks_anchor_update AFTER UPDATE OF status, labels ON tasks
WHEN NEW.status != 'blocked' AND NEW.labels LIKE '%"waits:%' AND json_valid(NEW.labels)
  AND json_type(NEW.labels) = 'array'
BEGIN
  UPDATE tasks SET labels = (SELECT json_group_array(value) FROM (SELECT value FROM json_each(NEW.labels)
    WHERE type != 'text' OR substr(value, 1, 6) != 'waits:' ORDER BY key)) WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS tasks_anchor_insert AFTER INSERT ON tasks
WHEN NEW.status != 'blocked' AND NEW.labels LIKE '%"waits:%' AND json_valid(NEW.labels)
  AND json_type(NEW.labels) = 'array'
BEGIN
  UPDATE tasks SET labels = (SELECT json_group_array(value) FROM (SELECT value FROM json_each(NEW.labels)
    WHERE type != 'text' OR substr(value, 1, 6) != 'waits:' ORDER BY key)) WHERE id = NEW.id;
END;

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
  closed REAL, cleared_why TEXT, lifecycle TEXT, subject TEXT, opened REAL, watch_key TEXT, reopens TEXT,
  episode TEXT, history TEXT, last_woken REAL);

CREATE TABLE IF NOT EXISTS schedules (
  name TEXT PRIMARY KEY, kind TEXT NOT NULL, every_s INTEGER NOT NULL, at TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, budget_usd_day REAL, description TEXT,
  payload TEXT NOT NULL DEFAULT '{}', last_run REAL, next_run REAL, last_status TEXT, tz TEXT);

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

-- Successful detached pushes, retained after their markers and task worktrees are pruned.
CREATE TABLE IF NOT EXISTS task_landings (
  marker TEXT PRIMARY KEY, task INTEGER NOT NULL, sha TEXT NOT NULL, target TEXT NOT NULL, ts REAL);
CREATE INDEX IF NOT EXISTS task_landings_task ON task_landings(task);

-- A task's work that landed on the push branch as another commit (a re-land after a push conflict):
-- its branch head when recorded and the commit that carries it (`ttp task superseded`, landed.py).
CREATE TABLE IF NOT EXISTS task_superseded (
  task INTEGER NOT NULL, head TEXT NOT NULL, sha TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY(task, head, sha));

-- A done code or harness task whose work will never land, with why and when (`ttp task abandon`, landed.py).
CREATE TABLE IF NOT EXISTS task_abandoned (task INTEGER PRIMARY KEY, why TEXT NOT NULL, ts REAL NOT NULL);

-- One row per Jev call: its use, decision, cost, estimated cost avoided, whether it changed the rules'
-- decision (NULL: not known) and later outcome (jevuse.py).
CREATE TABLE IF NOT EXISTS jev_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, use TEXT NOT NULL, ref TEXT, decision TEXT,
  cost_usd REAL NOT NULL DEFAULT 0, avoided_usd REAL NOT NULL DEFAULT 0, settle_at REAL,
  outcome TEXT, outcome_ts REAL, note TEXT, changed INTEGER);
CREATE INDEX IF NOT EXISTS jev_calls_use ON jev_calls(ts, use);

-- One row per wait-wake gate decision (waitgate.py): skip or wake, by which step, why, its Jev call,
-- the model turn a skip is estimated to have saved, and the outcome the next real turn settled.
CREATE TABLE IF NOT EXISTS wake_gate (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, task INTEGER NOT NULL, decision TEXT NOT NULL,
  by TEXT NOT NULL, reason TEXT, jev_call INTEGER, avoided_usd REAL NOT NULL DEFAULT 0,
  outcome TEXT, outcome_ts REAL, note TEXT);
CREATE INDEX IF NOT EXISTS wake_gate_ts ON wake_gate(ts, task);

-- One row per episode of a high alert with a condition key; cleared is set, never deleted.
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, raised REAL NOT NULL, last REAL,
  message INTEGER, severity TEXT, text TEXT, cleared REAL, cleared_why TEXT);
CREATE INDEX IF NOT EXISTS alerts_key ON alerts(key, cleared);

-- Time to recover (ttr.py): one row per machine incident; rows are only added, each end written once.
-- last_bad/period_s: while a heal check keeps it open, the check's last unhealthy sample and its period.
CREATE TABLE IF NOT EXISTS ttr_incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, alias TEXT NOT NULL, kind TEXT NOT NULL, cause TEXT,
  started REAL NOT NULL, ended REAL, last_bad REAL, period_s REAL);
CREATE UNIQUE INDEX IF NOT EXISTS ttr_open ON ttr_incidents(alias) WHERE ended IS NULL;
CREATE INDEX IF NOT EXISTS ttr_alias ON ttr_incidents(alias, started);

-- One row per finished review (reviewstats.py): reviewed head, changed lines against its merge-base,
-- areas touched, tier, re-review flag, outcome, cost and whether the reviewed hand-off carried a
-- `self_check` line (1/0; NULL: no reviewed task found). A later verdict of the same review replaces it.
CREATE TABLE IF NOT EXISTS review_stats (
  task INTEGER PRIMARY KEY, ts REAL NOT NULL, head TEXT, lines INTEGER, areas TEXT, tier TEXT,
  rereview INTEGER NOT NULL DEFAULT 0, outcome TEXT, cost_usd REAL NOT NULL DEFAULT 0, self_check INTEGER);
CREATE INDEX IF NOT EXISTS review_stats_ts ON review_stats(ts);
"""

TERMINAL_TASK_STATES = ("done", "failed", "cancelled")
NOT_ABANDONED_SQL = "id NOT IN (SELECT task FROM task_abandoned)"   # landed.abandon: left out of landing checks
WORKSPACE_TRIES_KEY = "workspace_tries"   # kv: task id -> failed workspace attempts in a row (Daemon._workspace_failed)
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


# Every connection waits this long for another's write lock before SQLITE_BUSY ("database is locked").
BUSY_TIMEOUT_S = 30
# Then a write is tried again this many more times, after these pauses, before the error is raised.
BUSY_RETRY_PAUSES_S = (0.5, 2.0)
# A write transaction held longer than this is logged with its caller (logs/slow-tx.log next to the
# database): it makes every other process wait, and past BUSY_TIMEOUT_S their writes fail.
SLOW_TX_S = 5.0
# git commands that ask a remote: never run while a write transaction is open (it holds the lock for
# as long as the network takes). Ask before the transaction, or read a cached answer inside it.
GIT_NETWORK = frozenset({"ls-remote", "fetch", "push", "pull", "clone"})
_GIT_OPTS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
# sys.addaudithook cannot remove a hook, and every hook runs on every audit event (id() raises one,
# so deepcopy pays per object). One hook per process however often ttp is imported afresh: it, the
# per-thread state and TxNetworkError live in sys.modules under a name outside ttp; it calls the
# _audit of the latest import to open a transaction.
_SHARED = "_ttp_db_audit"
_shared = sys.modules.get(_SHARED)
if _shared is None:
    _shared = sys.modules[_SHARED] = type(sys)(_SHARED)
    _shared.local = threading.local()
    _shared.audit = None
_tx_local = _shared.local   # .spawns: [(seconds into the transaction, argv)] while one is open


class TxNetworkError(RuntimeError):
    """A git network command started inside a write transaction while the guard is on (TTP_TX_GUARD=1)."""


TxNetworkError = _shared.__dict__.setdefault("TxNetworkError", TxNetworkError)   # one class for every import


def git_network(argv: Any) -> str:
    """The git subcommand when `argv` runs one that asks a remote (GIT_NETWORK), else ""."""
    if not isinstance(argv, (list, tuple)) or not argv or Path(str(argv[0])).name != "git":
        return ""
    skip = False
    for a in map(str, argv[1:]):
        if skip:
            skip = False
        elif a in _GIT_OPTS_WITH_VALUE:
            skip = True
        elif not a.startswith("-"):
            return a if a in GIT_NETWORK else ""
    return ""


def _audit(event: str, args: tuple) -> None:
    """Records each subprocess started inside an open write transaction (for the slow-tx log) and,
    with TTP_TX_GUARD=1 (the tests), refuses a git network command there."""
    if event != "subprocess.Popen":
        return
    spawns = getattr(_tx_local, "spawns", None)
    if spawns is None:
        return
    argv = args[1] if len(args) > 1 else None
    spawns.append((time.monotonic(), argv))
    if os.environ.get("TTP_TX_GUARD") == "1" and (sub := git_network(argv)):
        raise TxNetworkError(f"git {sub} started inside a write transaction (db.tx): ask the remote before "
                             f"the transaction opens, or read a cached answer: {_argv_text(argv)}")


def _argv_text(argv: Any) -> str:
    return (" ".join(map(str, argv)) if isinstance(argv, (list, tuple)) else str(argv))[:200]


def _hook() -> None:
    first = _shared.audit is None
    _shared.audit = _audit
    if first:
        shared = _shared

        def hook(event: str, args: tuple) -> None:
            if event == "subprocess.Popen":
                shared.audit(event, args)
        sys.addaudithook(hook)


def _slowest(spawns: list, ended: float) -> str:
    """The slow-tx log's hint: how many subprocesses the transaction started and the one followed by
    the longest gap (until the next one started, or the commit), which is at most its run time."""
    if not spawns:
        return "; no subprocess"
    gaps = [((spawns[i + 1][0] if i + 1 < len(spawns) else ended) - t, argv) for i, (t, argv) in enumerate(spawns)]
    gap, argv = max(gaps, key=lambda g: g[0])
    return f"; {len(spawns)} subprocess(es), slowest step {gap:.1f} s after: {_argv_text(argv)}"


def is_busy(e: BaseException) -> bool:
    """True for SQLite's busy or locked error: another connection holds the lock (transient)."""
    if not isinstance(e, sqlite3.OperationalError):
        return False
    code = getattr(e, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (5, 6)   # SQLITE_BUSY, SQLITE_LOCKED and their extended codes
    msg = str(e).lower()
    return "database is locked" in msg or "database table is locked" in msg or "database is busy" in msg


def retry_busy(fn: Callable[[], Any], pauses: Iterable[float] | None = None) -> Any:
    """fn(), tried again after each pause while it fails only because the database is busy."""
    for pause in (*(BUSY_RETRY_PAUSES_S if pauses is None else pauses), None):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if pause is None or not is_busy(e):
                raise
            time.sleep(pause)


class DB:
    """Thin wrapper: short transactions, WAL, rows as dicts. Safe to open from many processes."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        self._after_commit: list[Callable[[], Any]] = []
        self._on_rollback: list[Callable[[], Any]] = []
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
        retry_busy(lambda: self.conn.execute("PRAGMA journal_mode=WAL"))
        self.conn.execute("PRAGMA foreign_keys=ON")
        retry_busy(lambda: self.conn.executescript(SCHEMA))
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
        self._migrate_landing_ts()
        self._migrate_issue_opened()
        self._migrate_issue_episode()
        self._migrate_issue_history()
        self._migrate_issue_last_woken()
        self._migrate_schedule_tz()
        self._migrate_task_pin()
        self._migrate_message_context()
        self._migrate_issue_watch_key()
        self._migrate_stuck_since()
        self._migrate_message_broadcast()
        self._migrate_ttr_sample()
        self._migrate_review_self_check()
        self._migrate_tier_source()

    def _migrate_tier_source(self) -> None:
        # The tier a task was queued at and why its current tier is what it is (TIER_SOURCES). Older
        # tasks keep NULL: hops then copy their tier, and show prints no source.
        if {"assigned_tier", "tier_source"} <= {r["name"] for r in self.q("PRAGMA table_info(tasks)")}:
            return
        with self.tx():
            have = {r["name"] for r in self.q("PRAGMA table_info(tasks)")}
            for col in ("assigned_tier", "tier_source"):
                if col not in have:
                    self.x(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")

    def _migrate_review_self_check(self) -> None:
        # Whether the reviewed hand-off carried a `self_check` line. Older rows have none (NULL).
        if "self_check" in {r["name"] for r in self.q("PRAGMA table_info(review_stats)")}:
            return
        with self.tx():
            if "self_check" not in {r["name"] for r in self.q("PRAGMA table_info(review_stats)")}:
                self.x("ALTER TABLE review_stats ADD COLUMN self_check INTEGER")

    def _migrate_ttr_sample(self) -> None:
        # A heal-sourced incident's last unhealthy sample and check period (ttr.observe caps its end by
        # them). Rows from before have neither and keep their recorded end.
        if "last_bad" in {r["name"] for r in self.q("PRAGMA table_info(ttr_incidents)")}:
            return
        with self.tx():
            cols = {r["name"] for r in self.q("PRAGMA table_info(ttr_incidents)")}
            for col in ("last_bad", "period_s"):
                if col not in cols:
                    self.x(f"ALTER TABLE ttr_incidents ADD COLUMN {col} REAL")

    def _migrate_message_context(self) -> None:
        # A relayer's context, kept apart from the user's verbatim words. Older messages have none.
        if "context" in {r["name"] for r in self.q("PRAGMA table_info(messages)")}:
            return
        with self.tx():
            if "context" not in {r["name"] for r in self.q("PRAGMA table_info(messages)")}:
                self.x("ALTER TABLE messages ADD COLUMN context TEXT")

    def _migrate_message_broadcast(self) -> None:
        # The broadcast id of a message the user sent to every project (`ttp say --all`). Older have none.
        if "broadcast" in {r["name"] for r in self.q("PRAGMA table_info(messages)")}:
            return
        with self.tx():
            if "broadcast" not in {r["name"] for r in self.q("PRAGMA table_info(messages)")}:
                self.x("ALTER TABLE messages ADD COLUMN broadcast TEXT")

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

    def _migrate_schedule_tz(self) -> None:
        # The zone a schedule's `at` is read in (schedule.zone_of). Rows from before home zones were
        # read in the daemon's own zone, so they are stamped with it and keep firing at the same
        # instants; rows added or given a new `at` later leave it empty and follow the home zone.
        if "tz" in {r["name"] for r in self.q("PRAGMA table_info(schedules)")}:
            return
        from .timefmt import detect_local
        with self.tx():
            if "tz" not in {r["name"] for r in self.q("PRAGMA table_info(schedules)")}:
                self.x("ALTER TABLE schedules ADD COLUMN tz TEXT")
                self.x("UPDATE schedules SET tz=? WHERE at IS NOT NULL AND at!=''", (detect_local(),))

    def _migrate_task_pin(self) -> None:
        # Explicit task pins and their verification override (see pin_of). Older tasks keep NULL.
        if {"model", "effort", "pin_verify"} <= {r["name"] for r in self.q("PRAGMA table_info(tasks)")}:
            return
        with self.tx():
            have = {r["name"] for r in self.q("PRAGMA table_info(tasks)")}
            for col in ("model", "effort", "pin_verify"):
                if col not in have:
                    self.x(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")

    def _migrate_stuck_since(self) -> None:
        # When a task last entered a stuck status (blocked, waiting, review) from any other, by a trigger
        # on every status write, raw SQL included; NULL while not stuck. A write that keeps it stuck (a new
        # reason, a hold, a spec addendum) never moves it. Older stuck tasks keep NULL: their entry is unknown.
        if "stuck_since" not in {r["name"] for r in self.q("PRAGMA table_info(tasks)")}:
            with self.tx():
                if "stuck_since" not in {r["name"] for r in self.q("PRAGMA table_info(tasks)")}:
                    self.x("ALTER TABLE tasks ADD COLUMN stuck_since REAL")
        self.x(f"""CREATE TRIGGER IF NOT EXISTS tasks_stuck_since AFTER UPDATE OF status ON tasks
WHEN (NEW.status IN {STUCK_STATUSES}) != (OLD.status IN {STUCK_STATUSES})
BEGIN
  UPDATE tasks SET stuck_since = CASE WHEN NEW.status IN {STUCK_STATUSES}
    THEN COALESCE(NULLIF(NEW.updated, OLD.updated), (julianday('now') - 2440587.5) * 86400.0) END
  WHERE id = NEW.id;
END""")

    def _migrate_issue_opened(self) -> None:
        # When each issue's current open stretch began (screen._issue sets it on insert and on each
        # reopen). Older issues start theirs at first_seen.
        if "opened" in {r["name"] for r in self.q("PRAGMA table_info(issues)")}:
            return
        with self.tx():
            if "opened" not in {r["name"] for r in self.q("PRAGMA table_info(issues)")}:
                self.x("ALTER TABLE issues ADD COLUMN opened REAL")
                self.x("UPDATE issues SET opened=first_seen")

    def _migrate_issue_watch_key(self) -> None:
        # The stable key a watcher JSON line gave its issue (screen.py), and the times a keyed issue
        # reopened lately (JSON list): watchdup groups issues by the key's kind and counts each reopen.
        # WATCH_KEY_SINCE records when the column came: watchdup ignores keyless issues opened before it.
        want = ("watch_key", "reopens")
        if set(want) <= {r["name"] for r in self.q("PRAGMA table_info(issues)")} and self.meta(WATCH_KEY_SINCE):
            return
        with self.tx():
            have = {r["name"] for r in self.q("PRAGMA table_info(issues)")}
            if "watch_key" not in have:
                since = time.time()
            elif self.meta(WATCH_KEY_SINCE):
                since = None
            else:   # added before this was recorded: from the first keyed issue, else now; 0 with no issues
                r = self.one("SELECT MIN(first_seen) AS t, COUNT(*) AS n FROM issues WHERE watch_key IS NOT NULL")
                any_issue = self.one("SELECT 1 AS x FROM issues LIMIT 1")
                since = r["t"] if r["n"] else (time.time() if any_issue else 0)
            for col in want:
                if col not in have:
                    self.x(f"ALTER TABLE issues ADD COLUMN {col} TEXT")
            if since is not None:
                self.set_meta(WATCH_KEY_SINCE, repr(float(since)))

    def _migrate_issue_episode(self) -> None:
        # The episode a keyed watcher line named (screen._supersede). Older issues have none.
        if "episode" in {r["name"] for r in self.q("PRAGMA table_info(issues)")}:
            return
        with self.tx():
            if "episode" not in {r["name"] for r in self.q("PRAGMA table_info(issues)")}:
                self.x("ALTER TABLE issues ADD COLUMN episode TEXT")

    def _migrate_issue_history(self) -> None:
        # Closes at someone's word (screen.close_issue: when, by whom, why). Older issues have none.
        if "history" in {r["name"] for r in self.q("PRAGMA table_info(issues)")}:
            return
        with self.tx():
            if "history" not in {r["name"] for r in self.q("PRAGMA table_info(issues)")}:
                self.x("ALTER TABLE issues ADD COLUMN history TEXT")

    def _migrate_issue_last_woken(self) -> None:
        # When each issue last woke the coordinator (screen.screen: rewake_after_s counts from it).
        # Older issues start theirs at last_seen, the time the old rule counted from: no re-wake burst.
        if "last_woken" in {r["name"] for r in self.q("PRAGMA table_info(issues)")}:
            return
        with self.tx():
            if "last_woken" not in {r["name"] for r in self.q("PRAGMA table_info(issues)")}:
                self.x("ALTER TABLE issues ADD COLUMN last_woken REAL")
                self.x("UPDATE issues SET last_woken=last_seen")

    def _migrate_push_landed(self) -> None:
        # Each pushed or landed row's own commit on the branch (pushq._apply; `ttp landed`). Older
        # rows keep NULL: their pushed_sha, the batch's head, contains them.
        if "landed_sha" in {r["name"] for r in self.q("PRAGMA table_info(push_queue)")}:
            return
        with self.tx():
            if "landed_sha" not in {r["name"] for r in self.q("PRAGMA table_info(push_queue)")}:
                self.x("ALTER TABLE push_queue ADD COLUMN landed_sha TEXT")

    def _migrate_landing_ts(self) -> None:
        # When each landing was pushed (tried.landing_times). Older rows keep NULL: the ledger shows
        # their task's finish time.
        if "ts" in {r["name"] for r in self.q("PRAGMA table_info(task_landings)")}:
            return
        with self.tx():
            if "ts" not in {r["name"] for r in self.q("PRAGMA table_info(task_landings)")}:
                self.x("ALTER TABLE task_landings ADD COLUMN ts REAL")

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
        args = tuple(args)
        if self.conn.in_transaction:   # the transaction holds the write lock (BEGIN IMMEDIATE)
            cur = self.conn.execute(sql, args)
        else:                          # a write on its own: it commits or fails whole, so it is safe to retry
            cur = retry_busy(lambda: self.conn.execute(sql, args))
        return cur.lastrowid or cur.rowcount

    @contextmanager
    def tx(self) -> Iterator[None]:
        """All writes inside commit together or not at all. Nested use joins the outer transaction."""
        if self.conn.in_transaction:
            yield
            return
        retry_busy(lambda: self.conn.execute("BEGIN IMMEDIATE"))
        _hook()
        began = time.monotonic()
        outer = getattr(_tx_local, "spawns", None)   # another connection's transaction on this thread
        spawns = _tx_local.spawns = []
        try:
            yield
        except BaseException:
            _tx_local.spawns = outer
            self._rolled_back()
            raise
        _tx_local.spawns = outer
        try:
            self.conn.execute("COMMIT")
        except BaseException:
            self._rolled_back()
            raise
        self._on_rollback.clear()
        ended = time.monotonic()
        held = ended - began
        if held > SLOW_TX_S:
            self._log_slow_tx(held, _slowest(spawns, ended))
        pending, self._after_commit = self._after_commit, []
        failed = None
        for fn in pending:   # each runs: one that raises never drops the commits queued after it
            try:
                fn()
            except Exception as e:
                failed = failed or e
        if failed:
            raise failed

    def _rolled_back(self) -> None:
        """The open transaction failed: roll it back, drop what waited for its commit and undo,
        newest first, the file changes made inside it (on_rollback), so files and rows agree."""
        self._after_commit.clear()
        undo, self._on_rollback = self._on_rollback, []
        try:
            if self.conn.in_transaction:   # SQLite may have rolled back already (SQLITE_FULL, IOERR)
                self.conn.execute("ROLLBACK")
        finally:
            for fn in reversed(undo):
                try:
                    fn()
                except Exception as e:   # the next undo still runs; what is left is the harness sweep's
                    self._log_undo_failure(fn, e)

    def _log_undo_failure(self, fn: Callable[[], Any], e: Exception) -> None:
        """A line in the daemon's log for an undo of a rolled-back transaction that raised. Never raises."""
        try:
            log = Path(self.path).parent / "logs" / "daemon.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} rollback undo "
                         f"{getattr(fn, '__qualname__', fn)!s} failed: {e!r}\n")
        except Exception:
            pass

    def _log_slow_tx(self, held: float, slowest: str = "") -> None:
        """One line per slow write transaction, with the code that held it: slow work (git, a subprocess,
        the network) belongs before the transaction or in after_commit. A report only: never raises."""
        try:
            import traceback
            where = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                for f in reversed(traceback.extract_stack()[:-3][-8:]))
            log = Path(self.path).parent / "logs" / "slow-tx.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} write lock held {held:.1f} s: {where}{slowest}\n")
        except Exception:
            pass

    def after_commit(self, fn: Callable[[], Any]) -> None:
        """Run fn once the open transaction commits, or now outside one. Slow side effects go here,
        so they never hold the write lock that every other process is waiting on."""
        if self.conn.in_transaction:
            self._after_commit.append(fn)
        else:
            fn()

    def on_rollback(self, fn: Callable[[], Any]) -> None:
        """Run fn if the open transaction rolls back (none open: never). A file change made inside a
        transaction registers its undo here, so a rollback never leaves it on disk uncommitted."""
        if self.conn.in_transaction:
            self._on_rollback.append(fn)

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

    def set_paused(self, on: bool, boot: str | None = None, by: str = "unknown", reason: str = "",
                   via: str = "", end: str = "") -> None:
        """Pause or resume the whole project, recording who (`by`: user, coordinator, daemon, a run or
        another project), through what (`via`) and why (projpause: required unless by the user). With
        boot (this host's boot id) the pause lasts until the daemon first ticks on another boot
        (Daemon.lift_boot_pause); any other pause or resume ends that."""
        from . import projpause
        if on:
            projpause.pause(self, by, reason, via=via, end=end or ("until reboot" if boot else ""))
        else:
            projpause.resume(self, by, via=via, reason=reason)
        self.set_kv("paused", bool(on))
        if on and boot:
            self.set_kv(PAUSE_BOOT_KEY, {"boot": boot, "since": time.time()})
        else:
            self.x("DELETE FROM kv WHERE key=?", (PAUSE_BOOT_KEY,))

    def paused_resources(self, shared: bool = True) -> dict[str, dict]:
        """Resources paused by name: {"reason", "since", "by"}. Kept in the database, so a pause
        outlives daemon restarts and reboots until someone lifts it. With shared, pauses of the
        project's shared resources (see shared.py) are in it too, marked "shared" and naming the
        project that set them, and the resources a machine-wide fleet hold covers (fleethold.py),
        marked "fleet"."""
        v = self.kv(PAUSED_RESOURCES_KEY, {})
        v = v if isinstance(v, dict) else {}
        if shared:
            from . import shared as sh
            got = sh.paused_for_db(self.path, self.kv(SHARED_SEEN_KEY, {}), db=self)
            # A fleet hold (fleethold.py) covers what the project has no pause of its own for.
            v = {**{k: x for k, x in got.items() if x.get("fleet")}, **v,
                 **{k: x for k, x in got.items() if not x.get("fleet")}}
        return v

    def boots(self, since: float) -> list[dict]:
        """Reboots the daemon recorded since `since`, oldest first: ts (the boot time where known)
        plus the boot event's data (runs lost, resources held at the last heartbeat before it)."""
        rows = self.q("SELECT ts, data FROM events WHERE source='host' AND kind='boot' AND ts>? ORDER BY ts, id",
                      (since,))
        out = [{**json.loads(r["data"] or "{}"), "ts": r["ts"]} for r in rows]
        ids = [int(x["run"]) for b in out for x in b.get("lost") or [] if isinstance(x, dict) and "run" in x]
        kept = self.resumed_run_ids(min(ids) - 1) if ids else set()
        for b in out:
            for x in b.get("lost") or []:
                if isinstance(x, dict) and "run" in x:
                    x["kept"] = int(x["run"]) in kept
        return out

    def resumed_run_ids(self, after_id: int) -> set[int]:
        """Ids of earlier runs that a later run (id > after_id) resumed with their session, so their work
        was kept. A resume that never started kept nothing."""
        kept: set[int] = set()
        for r in self.q("SELECT note FROM runs WHERE id>? AND role!='coordinator' AND note LIKE '%\"resumes\"%'",
                        (after_id,)):
            try:
                note = json.loads(r["note"] or "{}")
            except ValueError:
                continue
            res = note.get("resumes") if isinstance(note, dict) else None
            back = res.get("run") if isinstance(res, dict) else None
            if back is not None and note.get("not_waste") != "resume":
                kept.add(int(back))
        return kept

    # messages ------------------------------------------------------------------------------
    def post(self, direction: str, text: str, chat: str | None = None, channel: str = "chat",
             kind: str = "user", severity: str = "normal", ref: str | None = None,
             handled: bool = False, provenance: str | None = None, ext_id: str | None = None,
             context: str | None = None, broadcast: str | None = None) -> int:
        """`provenance` (inbound): how the message came in, set by the code that received it, never
        taken from the sender. Only some count as the user's word for a PR approval (prguard).
        `context` (inbound): what a relayer added to the user's words; `text` holds only those words.
        `broadcast` (inbound): the id of the broadcast it came with (`ttp say --all`, broadcast.py)."""
        from . import alerts
        ts = time.time()
        with self.tx():
            mid = self.x("INSERT INTO messages(ts,direction,chat,channel,kind,severity,text,ref,handled,provenance,"
                         "ext_id,context,broadcast) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (ts, direction, chat, channel, kind, severity, text, ref, int(handled), provenance, ext_id,
                          context or None, broadcast or None))
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
    def _continues_failed_review(self, labels: Any) -> bool:
        """Whether labels mark a fix of a failed review: review_fix:<id>, or continues:<id> of a failed review."""
        for lb in labels or []:
            if str(lb).startswith("review_fix:"):
                return True
            if str(lb).startswith("continues:") and str(lb)[10:].isdigit():
                t = self.task(int(str(lb)[10:]))
                if t and t["kind"] == "review" and t["status"] == "failed":
                    return True
        return False

    def add_task(self, title: str, spec: str = "", **kw: Any) -> int:
        now = time.time()
        cols = {"created": now, "updated": now, "title": title.strip()[:200], "spec": spec}
        for k in ("kind", "status", "priority", "tier", "provider", "budget_usd", "max_attempts",
                  "parent", "reply_chat", "origin", "branch", "not_before", "model", "effort", "pin_verify",
                  "tier_source"):
            if kw.get(k) is not None:
                cols[k] = kw[k]
        for k in ("depends_on", "labels"):
            if kw.get(k) is not None:
                cols[k] = json.dumps(list(kw[k]))
        if cols.get("tier") == "light" and kw.get("kind", "code") == "code" and self._continues_failed_review(kw.get("labels")):
            cols["tier"] = "standard"   # a fix of a failed review runs at least standard
        # The tier it is queued at: a continue hop copies this, never a tier the daemon raised for a retry.
        cols["assigned_tier"] = cols.get("tier") or "standard"
        cols.setdefault("tier_source", "task_add" if kw.get("tier") else "default")
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

    def requeue(self, task_id: int) -> str:
        """Put a task that is not running back in the queue (the user's decision to run it: the web app,
        `ttp task requeue`): its blocked reason, its waiting state and earlier unchanged wakes, and its
        failed workspace tries are cleared. Returns "" when done, else why not (no such task, running)."""
        with self.tx():
            t = self.task(task_id)
            if not t:
                return f"no task #{task_id}"
            # One statement, so dispatch cannot start the task between a check and the write.
            if not self.conn.execute("UPDATE tasks SET status='queued', blocked_reason=NULL, updated=? "
                                     "WHERE id=? AND status!='running'", (time.time(), task_id)).rowcount:
                return f"task #{task_id} is running: cancel it first"
            prev = load_result(t["result"])
            if t["status"] != "queued" and ("waiting_since" in prev or "stale_wakes" in prev):
                # A requeue is a decision to run it, not to sleep on its probe, and its earlier
                # unchanged wakes no longer count.
                prev.pop("waiting_since", None)
                prev.pop("stale_wakes", None)
                self.x("UPDATE tasks SET result=? WHERE id=?", (dump_result(prev), task_id))
            tries = self.kv(WORKSPACE_TRIES_KEY, {}) or {}
            if str(task_id) in tries:
                tries.pop(str(task_id))
                self.set_kv(WORKSPACE_TRIES_KEY, tries)
        return ""

    def cancel(self, task_id: int) -> str:
        """Cancel a task that has not finished (`ttp task cancel`, the web app). Returns "" when done,
        else why not; an unknown or finished (done, failed, cancelled) task is left as it is."""
        with self.tx():
            t = self.task(task_id)
            if not t:
                return f"no task #{task_id}"
            # One statement, so a run finishing between a check and the write keeps its outcome.
            if not self.conn.execute("UPDATE tasks SET status='cancelled', updated=? WHERE id=? "
                                     "AND status NOT IN ('done','failed','cancelled')",
                                     (time.time(), task_id)).rowcount:
                return f"task #{task_id} is already {t['status']}"
        return ""

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

    def ready_tasks(self, later: bool = False) -> list[dict]:
        """Queued tasks whose dependencies are all finished, in dispatch's pick order: required owners
        of an open obligation first (its earliest due milestone first, obligations.owner_due), then best
        priority, then oldest. Read afresh on every call: no order outlives a dispatch pass or a daemon
        restart. A task deferred with `start_when` is not ready until its probe passes (see deferral).
        With `later`, also the ones whose retry timer (not_before) has not come yet, in the same order."""
        rows = self.q("SELECT * FROM tasks WHERE status='queued' ORDER BY priority, id") if later else \
            self.q("SELECT * FROM tasks WHERE status='queued' AND (not_before IS NULL OR not_before<=?) "
                   "ORDER BY priority, id", (time.time(),))
        if rows:
            from . import obligations
            try:
                due = obligations.owner_due(self)
            except Exception:   # unreadable obligations only lose the boost, never the queue
                due = {}
            if due:
                rows.sort(key=lambda r: (r["id"] not in due, due.get(r["id"], 0.0)))   # stable: priority, id within
        unmet = self.unmet_dependencies(rows)
        return [r for r in rows if not unmet[r["id"]] and "when" not in deferral(r)]

    def unmet_dependencies(self, tasks: list[dict]) -> dict[int, list]:
        """Task id -> the dependencies it still waits on. Only 'done' satisfies a dependency, with
        two exceptions: a review's dependency on the task it reviews, while that task waits in
        'review' (only the review can move it on, so waiting for 'done' would stall both; any other
        prerequisite of the review still has to finish), and a harness task in 'review' whose commit
        the daemon found merged into the harness (HARNESS_MERGED_KEY): its work is live already."""
        states = {r["id"]: r for r in self.q("SELECT id, status, kind, branch FROM tasks "
                                             "WHERE status IN ('done','review')")}
        merged = (self.kv(HARNESS_MERGED_KEY, {}) or {}) if any(
            r["status"] == "review" and r["kind"] == "harness" for r in states.values()) else {}
        out: dict[int, list] = {}
        for t in tasks:
            out[t["id"]] = [d for d in dependency_ids(t) if d not in states or (
                states[d]["status"] != "done" and not (t.get("kind") == "review" and reviews_task(t, states[d]))
                and not (states[d]["kind"] == "harness" and str(d) in merged))]
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


def unplanned_abrupt(boot: dict) -> bool:
    """Whether a reboot, as DB.boots returns it, was abrupt and nothing planned it: only these say the
    host may be unstable (an orderly one was a shutdown or an update, a planned one was chosen)."""
    return boot.get("shutdown") != "orderly" and not boot.get("planned")


def host_line(boots: list[dict], where=None) -> str:
    """'host: N reboots in 24 h (K planned: by <project>) (last HH:MM <zone>), M runs cut short: L lost
    ($A), K resumed with work kept ($B)' for the boots of the last day, as DB.boots returns them, in
    the home zone `where`; '' when there were none. A run a later run resumed kept its work."""
    if not boots:
        return ""
    runs = [x for b in boots for x in b.get("lost") or [] if isinstance(x, dict)]
    kept = [x for x in runs if x.get("kept")]
    gone = [x for x in runs if not x.get("kept")]
    kept_usd = sum(float(x.get("usd") or 0) for x in kept)
    lost_usd = max(0.0, sum(float(b.get("lost_usd") or 0) for b in boots) - kept_usd)
    planned = [b for b in boots if b.get("planned")]
    by = sorted({str(b["planned"]["project"]) for b in planned
                 if isinstance(b["planned"], dict) and re.fullmatch(r"[\w.-]+", str(b["planned"].get("project") or ""))})
    from .timefmt import short
    last = short(float(boots[-1]["ts"]), where)
    n = len(runs)
    return (f"host: {len(boots)} reboot{'' if len(boots) == 1 else 's'} in 24 h"
            + (f" ({len(planned)} planned" + (f": by {', '.join(by)}" if by else "") + ")" if planned else "")
            + f" (last {last}), {n} run{'' if n == 1 else 's'} cut short"
            + (f": {len(gone)} lost (${lost_usd:.2f}), {len(kept)} resumed with work kept (${kept_usd:.2f})" if n else ""))


def chat_floor(chat_min: str | None, project_min: str | None) -> str:
    """A chat's severity floor: the project floor (notify.chat_min_severity) applies to every chat,
    and a chat can only raise it."""
    return max((chat_min or "normal", project_min or "normal"), key=lambda n: SEVERITY_RANK.get(n, -1))
RESULT_MAX_CHARS = 20000


HARNESS_MERGED_KEY = "harness_merged"   # kv: harness task id in 'review' -> its commit merged into the harness


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


def assigned_tier(task: dict | None) -> str:
    """The tier `task` was queued at (task_add, the coordinator, the daemon's hop or fix), never one the
    daemon raised for a retry; its current tier for a task older than the record."""
    t = task or {}
    return str(t.get("assigned_tier") or t.get("tier") or "standard")


def pin_of(task: dict | None) -> dict:
    """The task's explicit model/effort pin and verification override, as keywords a review, re-review or
    continuation of it takes over: with its provider, so the model is never handed to another
    agent. {} when it pins neither (a derived task then runs as it always did)."""
    if not task or not (task.get("model") or task.get("effort")):
        return {}
    return {k: task.get(k) or None for k in ("provider", "model", "effort", "pin_verify")}


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
    return names_branch(review.get("spec") or "", task.get("branch"))


_INNER_EDGE = re.compile(r"[^\w/-]")  # a character a branch named inside a word can end before
_START_AFTER = re.compile(r"[^\w/.-]")  # a character a branch named inside a word can start after


class BranchIndex:
    """Tasks by branch, to find the ones a text names (see names_branch) in one scan of the text rather
    than one search per task: a named branch has no whitespace, so it is a whole word of the text or a
    part of one cut at its punctuation. Branches with whitespace are searched one by one."""

    def __init__(self, tasks: list[dict]):
        self.tasks = tasks
        self.ids = {t["id"] for t in tasks}
        self.by_branch: dict[str, list[int]] = {}
        self.spaced: list[dict] = []
        for t in tasks:
            if b := t.get("branch"):
                self.by_branch.setdefault(b, []).append(t["id"])
                if any(c.isspace() for c in b):
                    self.spaced.append(t)
        self.firsts = {b[0] for b in self.by_branch}
        self.maxlen = max(map(len, self.by_branch), default=0)

    def named(self, spec: str) -> set[int]:
        out = {t["id"] for t in self.spaced if names_branch(spec, t["branch"])}
        get, firsts, maxlen = self.by_branch.get, self.firsts, self.maxlen
        for w in spec.split():
            if ids := get(w):
                out.update(ids)
            if not _INNER_EDGE.search(w):
                continue
            starts = [i for i in [0] + [m.end() for m in _START_AFTER.finditer(w)] if w[i:i + 1] in firsts]
            ends = [m.start() for m in _INNER_EDGE.finditer(w)] + [len(w)]
            for i in starts:   # ends is sorted: visit only the ends no longer than the longest branch
                for j in ends[bisect_right(ends, i):bisect_right(ends, i + maxlen)]:
                    if (i, j) != (0, len(w)) and (ids := get(w[i:j])):
                        out.update(ids)
        return out


def reviewed_ids(review: dict, tasks: list[dict] | BranchIndex) -> set[int]:
    """The ids of `tasks` that `review` reviews (see reviews_task), in one pass that reads its subject
    once. Pass a BranchIndex when checking many reviews against the same tasks."""
    index = tasks if isinstance(tasks, BranchIndex) else BranchIndex(tasks)
    subject = review_subject(review)
    if subject is not None:
        return {subject} & index.ids
    return index.named(review.get("spec") or "")


def names_branch(spec: str, branch: str | None) -> bool:
    """Whether `spec` names `branch` on its own: no word character, '/', '.' or '-' right before it, no
    word character, '/' or '-' right after. Plain string search, no regex: a pattern per task branch
    would thrash re's cache of 512 compiled patterns."""
    if not branch:
        return False
    i = spec.find(branch)
    while i >= 0:
        before, after = spec[i - 1:i], spec[i + len(branch):i + len(branch) + 1]
        if not (before and (before.isalnum() or before in "_/.-")) and \
                not (after and (after.isalnum() or after in "_/-")):
            return True
        i = spec.find(branch, i + 1)
    return False


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
        if key == "start_when" and val.strip() and val.strip().lower() != "now":   # `now` is no probe
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
