# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Alerts that clear themselves, and the split between what needs the user now and everything else.

A high alert posted with a condition key (Daemon.alert, the budget gate) opens an episode in the
`alerts` table. The daemon's sweep closes it once its condition no longer holds and stores when
and why; nothing is deleted. Readers (web app, `ttp status`, chat relay, desktop notifier, Slack)
ask `active()`, which also checks the condition live, so a cleared alert disappears at once.

A condition only the user can clear (USER_BLOCKERS: a lost VPN or network sign-in, netwait.py) is
alerted at normal severity yet tracked like a high alert: an episode, the top section, and a clearing
told as one low feed line. Posted low (an outage of a tunnel that reconnects by itself), it is live
status in the feed until it clears or is raised to normal under the same key.

A provider's budget gate or limit pause that lifts by itself (STATUS_CONDITIONS) is status, not a
problem for the user: posted below high, it is tracked as an episode and listed in the feed as live
status until it clears, and never reaches the top section. Only a case only the user can fix (an
account out of credits or funds) is posted high.

A host reboot is information only: it is posted as kind `info` and never opens an episode.

One condition is one broadcast: while its episode is open a repeat only updates `last`, with one
reminder once a day has passed. A condition every project on the machine sees (a logged-out CLI, a
full shared disk) is broadcast by one of them: the first takes a claim in the per-user tt-project
folder, and the others post theirs on the `quiet` channel, shown in the web app and `ttp status`
but never sent to a chat, Slack or the desktop.

A logout is the exception to the one daily reminder: every project waiting on the logged-out CLI
reports itself into the claim (machine_round), and the claimer reminds the chats at LOGOUT_REMIND_S
with how many projects, queued tasks and high events wait, whichever project holds the claim then.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from . import prguard
from . import startbreaker
from .db import DB, SEVERITY_RANK
from .schedule import failing

DAY = 86400.0
REMIND_S = DAY          # an open episode is broadcast again once, this long after it was raised
QUIET = "quiet"         # channel of a broadcast that stays out of chats (another project has the claim)
CLAIM_TTL = DAY + 3600  # a claim its holder stopped renewing (gone, or no reminder) passes on after this
FEED_DAYS = 7
START_FAILED = "could not start a run: "   # a queued task's note after a failed run start (Daemon._start_failed)
START_RETRY_S = 600.0   # a start failure retries within a minute; a task not due this soon was deferred
LOGOUT_REMIND_S = (3600.0, 4 * 3600.0, 12 * 3600.0)   # a logout is reminded this long after it began, then daily
WAITER_TTL = 1800.0     # a project's report into a logout claim counts this long (it renews every few minutes)
# What the chat hears once an episode clears.
CLEARED_TEXT = {
    "budget": "Budget for {arg} is out of red; new work starts again.",
    "auth": "{arg} is logged in again; its queued work starts.",
    "limit": "{arg} accepts work again.",
    "coordinator": "The coordinator is working again: a turn succeeded.",
    "disk": "Disk space is back above the guard; held tasks start again.",
    "run-start": "Runs start again.",
    "schedule": "Schedule {arg} runs again.",
    "release-older": "The installed tt-project is no longer older than this harness.",
    "integrity": "The harness, the task worktrees and their repositories check out again.",
    "integrity-host": "project.json names this machine as the project's host again.",
    "pr-ready": "{arg} is back in draft, closed or approved.",
    "config": "project.json reads again; new work starts again.",
    "push_rejected": "Pushes go through again: a push batch pushed.",
    "after_push_failed": "after_push no longer fails: the last one succeeded, or after_push is off.",
    "push_queue_dying": "Push batches finish again.",
    "push_not_landing": "A push batch landed on the push branch again.",
    "push_scope": "The changes held for the token scope `{arg}` pushed or left the push queue.",
    "push_tip_failing": "The push branch tip passes its checks again, or it moved: push batches go on as usual.",
    "heal": "Heal check {arg} passes again.",
    "heal-hold": "The device hold heal check {arg} reported has a tenant again or ended.",
    "deploy-behind": "The deploy to {arg} no longer fails: it runs the release, or its upgrade is owned and pending again.",
    "machine": "Machine condition {arg} is over: no longer reported.",
    "machine-change": "Machine change #{arg} is closed or no longer overdue.",
    "network": "The hosts the queued work waits on answer again; it resumes by itself.",
    "daemon-hang": "The daemon completes its ticks again after its watchdog restart.",
    "paused": "The project is no longer paused; new runs start again.",
    "coord-budget": "No coordinator item is parked over its per-turn budget any more.",
    "linger-off": "Linger is on again: the daemon starts at boot without a login.",
    "notifier": "Desktop notifications deliver again.",
    "tick-step": "Daemon tick step {arg} runs again.",
    "account-mismatch": "{arg}'s current account has plan readings of its own again, is known to be billed by use, or the old readings aged out.",
}
# Conditions only the user can clear, alerted below high: tracked and shown in the top section like a
# high alert; their clearing is a low feed line, not a new notification (netwait.py).
# linger-off: the systemd service would not start at boot without a login (downtime.py).
# notifier: this machine's desktop notifications fail (notifier.py; a system setting or a missing tool).
USER_BLOCKERS = ("network", "paused", "coord-budget", "linger-off", "notifier")   # paused: a long project pause (projpause)
CLEARED_SEVERITY = {"network": "low", "paused": "low", "coord-budget": "low", "account-mismatch": "low",
                    "linger-off": "low", "notifier": "low"}
# Conditions that lift by themselves: below high they are a self-clearing feed line, never "needs you".
# account-mismatch: the provider's login has no plan readings of its own yet (budget.evaluate); its next
# run reports them, or it turns out billed by use.
# tick-step: a daemon tick step that keeps failing (Daemon._run_step); its next success clears it.
STATUS_CONDITIONS = ("budget", "limit", "deploy", "deploy-behind", "daemon-hang", "account-mismatch", "tick-step")


def _urgent(severity: str | None) -> bool:
    return SEVERITY_RANK.get(severity or "normal", 1) >= SEVERITY_RANK["high"]


def _top(severity: str | None, ref: str | None) -> bool:
    """An alert for the top section: high or above, or a keyed condition only the user can clear
    (posted low, it is live status instead: a tunnel that reconnects by itself, netwait.py)."""
    return _urgent(severity) or (bool(ref) and str(ref).partition(":")[0] in USER_BLOCKERS
                                 and severity != "low")


def _status(ref: str | None, severity: str | None = None) -> bool:
    kind = str(ref or "").partition(":")[0]
    return bool(ref) and (kind in STATUS_CONDITIONS or (kind in USER_BLOCKERS and severity == "low"))


def tracked(kind: str, severity: str | None, ref: str | None) -> bool:
    """Whether a broadcast opens an episode: a top-section alert or a status condition with a
    condition key, never a reboot."""
    return (kind == "alert" and bool(ref) and (_top(severity, ref) or _status(ref, severity))
            and not str(ref).startswith("reboot:"))


def open_episode(db: DB, key: str, ts: float, message: int, severity: str, text: str) -> None:
    """Called by DB.post in the message's own transaction: a repeat joins the open episode."""
    row = db.one("SELECT id FROM alerts WHERE key=? AND cleared IS NULL ORDER BY id DESC LIMIT 1", (key,))
    if row:
        db.x("UPDATE alerts SET last=?, message=?, severity=?, text=? WHERE id=?",
             (ts, message, severity, text[:2000], row["id"]))
    else:
        db.x("INSERT INTO alerts(key,raised,last,message,severity,text) VALUES(?,?,?,?,?,?)",
             (key, ts, ts, message, severity, text[:2000]))


BREAKER = "auth_breaker:"   # kv per provider: its auth breaker (Daemon.open_breaker, check_logins)


def breaker(db: DB, provider: str) -> dict | None:
    """The provider's open auth breaker: no run starts on it until a login check passes."""
    rec = db.kv(BREAKER + provider) or {}
    return rec if rec.get("open") else None


def switch_text(switched: dict) -> str:
    """A login's change of account in words: its kinds only, never an email address."""
    old, new = switched.get("from") or "", switched.get("to") or ""
    if old and new and old != new:
        return f"{old} before, {new} now"
    if new:
        return f"{new}, as before" if old == new else f"{new} now"
    return "account kind unknown"


def login_proven(db: DB, provider: str, since: float) -> bool:
    """Whether a run on `provider` started at or after `since` got past the login: it succeeded, or
    it is still running and spends or streams tokens (Daemon.meter_running). Age alone is no proof:
    a CLI that hangs or retries while logged out would release the whole queue."""
    return bool(db.one("SELECT id FROM runs WHERE provider=? AND started>=? AND (status='ok' OR status='running' "
                       "AND (cost_usd>0 OR input_tokens>0 OR output_tokens>0)) LIMIT 1", (provider, since)))


def holds(db: DB, key: str, since: float, now: float) -> bool:
    """Whether the condition behind an alert key is still true. `since` is when it was raised.
    Unknown keys cannot be checked and hold for a day after their last report."""
    kind, _, arg = key.partition(":")
    if kind == "auth":
        # A logout holds while the provider's auth breaker is open; its closing ends the alert.
        rec = db.kv(BREAKER + arg) or {}
        if rec.get("open"):
            return True
        if float(rec.get("closed") or 0) >= since:
            return False
        return not login_proven(db, arg, since)   # an alert from before the breaker
    if kind == "limit":
        # The next successful run on the provider ends it, as does the end of its pause.
        if db.one("SELECT id FROM runs WHERE provider=? AND status='ok' AND started>=? LIMIT 1", (arg, since)):
            return False
        return float((db.kv(f"limited:{arg}") or {}).get("until") or 0) > now
    if kind == "budget":
        if (db.kv("gates", {}).get(arg) or {}).get("level") == "red":
            return True
        # A dip inside an open red episode (Daemon._red_episodes) is not out of red: one flap, one alert.
        return any(isinstance(e, dict) and e.get("open")
                   for e in ((db.kv("budget_red_episodes", {}) or {}).get(arg) or {}).values())
    if kind == "account-mismatch":   # the gate keeps the flag while the login has no plan readings of its own
        return bool(((db.kv("gates", {}).get(arg) or {}).get("numbers") or {}).get("account_mismatch"))
    if kind == "relayed":   # conditions told to the chat while the coordinator could not run: while one is open
        ids = [int(x) for x in arg.split(",") if x.isdigit()]
        # One reopened after the relay is a new stretch, told by a relay of its own.
        return bool(ids) and bool(db.one(f"SELECT id FROM issues WHERE status='open' AND COALESCE(opened,first_seen)<=? "
                                         f"AND id IN ({','.join('?' * len(ids))}) LIMIT 1", [since, *ids]))
    if key == "disk":
        return bool(db.kv("disk_low"))
    if key == "coordinator":
        return int(db.kv("coordinator_failures", 0)) > 0
    if key == "config":   # the daemon keeps the flag while project.json and its last good copy are unreadable
        return bool(db.kv("config_unreadable"))
    if key == "release-older":
        return bool(db.kv("release_older"))
    if key == "integrity":
        last = db.kv("integrity") or {}
        return bool(last.get("bad") or last.get("worktrees") or last.get("clones"))
    if key == "integrity-host":   # the daemon keeps the change while project.json names another machine
        return bool(db.kv("integrity_host"))
    if kind == "schedule":
        # The next run that does not fail ends it, as does disabling or removing the schedule.
        row = db.one("SELECT last_status FROM schedules WHERE name=? AND enabled=1", (arg,))
        return bool(row) and failing(row["last_status"])
    if kind == "pr-ready":   # the pr-watch watcher keeps the flag while the PR is out of draft unapproved
        return arg in (db.kv(prguard.UNAPPROVED_KEY, {}) or {}) and not prguard.approved(db, arg)
    if key == "run-start":
        if startbreaker.is_open(db):   # one cause fails every start: dispatch holds the queue (startbreaker)
            return bool(db.one("SELECT id FROM tasks WHERE status='queued' LIMIT 1"))
        # Until every task whose start failed has started, been cancelled or deferred past its one-minute
        # retry (START_FAILED): another task's run starting proves nothing about the cause.
        return bool(db.one("SELECT id FROM tasks WHERE status='queued' AND blocked_reason LIKE ? "
                           "AND COALESCE(not_before,0)<=? LIMIT 1", (START_FAILED + "%", now + START_RETRY_S)))
    if kind == "deploy":
        from . import deploy
        return deploy.holds(db, arg, now)
    if kind == "deploy-behind":
        from . import deploy
        return deploy.holds_behind(db, arg, now)
    if kind == "heal":   # healthy, coordinator-owned and known-fault checks no longer need the user
        from . import heal
        return heal.holds(db, arg, now)
    if kind == "heal-hold":   # an untenanted hold's page: the heal alert's lifecycle (heal.cleared_why)
        from . import heal
        return heal.cleared_why(db, arg, now, hold=True) is None
    # The push queue (pushq.py): each lasts until a batch finalized after it was raised shows otherwise.
    if key == "push_rejected":
        return not db.one("SELECT id FROM push_batches WHERE outcome='pushed' AND finalized>=? LIMIT 1", (since,))
    if key == "after_push_failed":   # also over once no after_push will run (pushq.AFTER_PUSH_OFF)
        if db.kv("after_push_off") or float(db.kv("after_push_ok_at") or 0) >= since:
            return False
        return not db.one("SELECT id FROM push_batches WHERE after_push='ok' AND after_finalized>=? LIMIT 1",
                          (since,))
    # Shared machines (machine_ledger): an outage page to the user lasts while its condition is still
    # reported, or while its change is open and overdue.
    if kind == "machine":
        from . import machine_ledger
        return machine_ledger.condition_open(arg, now)
    if kind == "machine-change":
        from . import machine_ledger
        return arg.isdigit() and machine_ledger.change_overdue(int(arg), now)
    if kind == "network":   # Daemon.check_unreachable keeps the outage until reachability is back
        return bool((db.kv("network_outage") or {}).get("alerted"))
    if kind == "push_scope":   # pushq.tend_scope_holds keeps the scope while a change held for it waits
        return arg in (db.kv("push_scope_held") or {})
    if key == "push_tip_failing":   # pushq keeps the failing tip until it passes or the branch moves
        return bool(((db.kv("push_queue") or {}).get("bad_tip") or {}).get("alerted"))
    if kind == "paused":   # the same project pause still lasts (projpause)
        from . import projpause
        return projpause.holds(db, arg)
    if key == "coord-budget":   # an item it named is still parked over its per-turn budget (turnsize.py)
        from . import turnsize
        return turnsize.holds(db)
    if key == "linger-off":   # Daemon.check_linger keeps the flag while linger is off
        return bool(db.kv("linger_off"))
    if key == "notifier":   # until a later desktop delivery on this machine succeeds
        from . import notifier
        return bool(notifier.failing())
    if kind == "tick-step":   # Daemon._run_step keeps the step while it keeps failing
        return arg in (db.kv("tick_steps_failing", {}) or {})
    if key == "daemon-hang":   # ttp.watchdog's incident lasts until a daemon completes a tick after it
        return bool(db.kv("daemon_hang"))
    if key == "push_queue_dying":   # a batch a reboot cut short did not finish either
        return not db.one("SELECT id FROM push_batches WHERE outcome IS NOT NULL AND outcome NOT IN "
                          "('died','error','rebooted') AND finalized>=? LIMIT 1", (since,))
    if key == "push_not_landing":   # pushq.check_landing: until a batch lands
        if not db.one("SELECT id FROM push_queue WHERE status IN ('approved','batched') LIMIT 1"):
            return False   # every waiting approval was withdrawn or cancelled
        return not db.one("SELECT id FROM push_batches WHERE outcome IN ('pushed','landed') AND finalized>=? LIMIT 1",
                          (since,))
    return now - since < DAY


def episode(db: DB, key: str, ts: float) -> dict | None:
    """The stored episode a message about `key` posted at `ts` belongs to (None before this table)."""
    return db.one("SELECT * FROM alerts WHERE key=? AND raised<=? ORDER BY id DESC LIMIT 1", (key, ts))


def active(db: DB, key: str, ts: float, now: float) -> bool:
    """A message about `key` posted at `ts` still describes a live problem."""
    ep = episode(db, key, ts)
    if ep and ep["cleared"]:
        return False
    return holds(db, key, _since(ep) if ep else ts, now)


def _checkable(key: str) -> bool:
    return key.partition(":")[0] in ("auth", "limit", "budget", "relayed", "disk", "coordinator", "run-start",
                                         "schedule", "release-older", "integrity", "integrity-host", "pr-ready", "config",
                                         "push_rejected", "after_push_failed", "push_queue_dying", "push_not_landing", "push_tip_failing", "push_scope", "heal",
                                         "heal-hold", "machine", "machine-change", "network", "deploy", "deploy-behind", "daemon-hang", "paused",
                                         "coord-budget", "account-mismatch", "linger-off", "tick-step", "notifier")


def _since(ep: dict) -> float:
    """What a condition is checked from: when the episode began, or for a condition the harness
    cannot check, when it was last reported."""
    return ep["raised"] if _checkable(ep["key"]) else ep["last"]


def cleared(db: DB, m: dict, now: float) -> bool:
    """A tracked alert (high, or a status condition) whose condition no longer holds: not worth
    delivering late. Other lower-severity messages sharing the key are news whatever the state."""
    return (tracked(m.get("kind") or "", m.get("severity"), m.get("ref"))
            and not active(db, m["ref"], m["ts"], now))


def _close(db: DB, ep: dict, now: float, why: str, text: str | None = None,
           severity: str | None = None) -> dict:
    """Close an episode within the caller's transaction, retaining its messages and reason."""
    acks = db.kv(ACKS, {}) or {}
    acked = acks.pop(str(ep["id"]), None)
    if acked:   # the acknowledgement ends with its episode; its reason stays in the history
        db.set_kv(ACKS, acks)
        if acked.get("why") and acked["why"] != why:
            why = f"{why} (acknowledged: {acked['why']})"
    db.x("UPDATE alerts SET cleared=?, cleared_why=? WHERE id=?", (now, why, ep["id"]))
    sent = db.kv("alerts_sent", {}) or {}
    if ep["key"] in sent:
        sent.pop(ep["key"])
        db.set_kv("alerts_sent", sent)
    if text:
        first = db.one("SELECT channel FROM messages WHERE id=?", (ep["message"],)) if ep["message"] else None
        db.post("out", text, chat=None, kind="resolved", severity=severity or "low", ref=ep["key"],
                channel=QUIET if first and first["channel"] == QUIET else "chat")
    return {**ep, "cleared": now, "cleared_why": why}


ACKS = "alert_acks"   # kv: open episode id -> its acknowledgement {why, ts, message, severity} (ack)
# Outages: acknowledging one never hides it while it holds, nor skips the release of its machine-wide claim.
OUTAGE_KINDS = ("auth", "network", "disk", "machine")


def acked(db: DB, ep: dict | None) -> dict | None:
    """The acknowledgement of an open episode: it leaves the top section for the feed and its
    repeats stay quiet (Daemon.alert) until the condition clears. It covers only the severity it
    acknowledged: once the episode is raised above it, the alert is back in the top section."""
    rec = (db.kv(ACKS, {}) or {}).get(str(ep["id"])) if ep and not ep["cleared"] else None
    if rec and SEVERITY_RANK.get(ep["severity"], 1) > SEVERITY_RANK.get(rec.get("severity"), 1):
        return None
    return rec


def _outage(db: DB, key: str, now: float) -> bool:
    """A live outage: a logout, unreachable hosts, a full disk, a machine condition, or an outage
    heal check that still needs the user."""
    kind, _, arg = key.partition(":")
    if kind == "heal":
        from . import heal
        return heal.cleared_why(db, arg, now) is None and heal.is_outage(db, arg)
    return kind in OUTAGE_KINDS


def ack(db: DB, message: int, why: str, now: float | None = None) -> dict:
    """Acknowledge the episode of a message id printed by `ttp alerts`; retries preserve the receipt.
    A condition that still holds stays open, acknowledged: it moves to the feed, its repeats page
    nobody, and the sweep closes it once it clears, so a new occurrence alerts again. A live outage
    is refused. Anything else closes now; the caller releases a machine-wide claim (`cleared` set)."""
    if not why or not why.strip():
        raise ValueError("acknowledging an alert requires a non-empty --why")
    now = time.time() if now is None else now
    why = why.strip()
    with db.tx():
        m = db.one("SELECT * FROM messages WHERE id=? AND direction='out' AND chat IS NULL", (message,))
        ep = episode(db, m["ref"], m["ts"]) if m and tracked(m["kind"], m["severity"], m["ref"]) else None
        if not ep:
            raise ValueError(f"no keyed alert with message id #{message}")
        if ep["cleared"] is not None:
            return ep
        if prior := acked(db, ep):
            return {**ep, "acked": prior}
        kind, _, arg = ep["key"].partition(":")
        if not _checkable(ep["key"]):
            live = False   # nothing to check: closing is safe, a new report alerts again
        elif kind == "heal":
            from . import heal
            live = heal.cleared_why(db, arg, now) is None
        else:
            live = holds(db, ep["key"], _since(ep), now)
        if live and _outage(db, ep["key"], now):
            raise ValueError(f"alert #{message} is a live outage ({ep['key']}): it clears itself once the "
                             f"condition is over, and cannot be acknowledged away")
        if not live:
            return _close(db, ep, now, why, f"Acknowledged alert #{message}: {why}")
        rec = {"why": why, "ts": now, "message": message, "severity": ep["severity"]}
        db.set_kv(ACKS, {**(db.kv(ACKS, {}) or {}), str(ep["id"]): rec})
        db.post("out", f"Acknowledged alert #{message} ({ep['key']}): {why}. It stays in the feed until "
                f"the condition clears.", chat=None, kind="info", severity="low", ref=ep["key"])
        return {**ep, "acked": rec}


def close_key(db: DB, key: str, why: str, now: float | None = None, text: str | None = None) -> list[dict]:
    """Retire open episodes when a check no longer requires user escalation. With `text`, the
    clearing is told as that, as quietly as sweep tells a status condition's."""
    now = time.time() if now is None else now
    with db.tx():
        return [_close(db, ep, now, why, text or f"Cleared: {key} user alert: {why}.",
                       ("normal" if _urgent(ep["severity"]) else "low") if text else None)
                for ep in db.q("SELECT * FROM alerts WHERE key=? AND cleared IS NULL ORDER BY id", (key,))]


def clear_kind(db: DB, kind: str, why: str, now: float | None = None) -> list[dict]:
    """Close the open episodes of condition `kind` at once (the project resumed), told as their
    CLEARED_TEXT, rather than on the daemon's next sweep."""
    now = time.time() if now is None else now
    if not db.one("SELECT id FROM alerts WHERE key LIKE ? AND cleared IS NULL LIMIT 1", (f"{kind}:%",)):
        return []
    text = "Cleared: " + CLEARED_TEXT[kind].format(arg="") if kind in CLEARED_TEXT else None
    with db.tx():
        return [_close(db, ep, now, why, text, CLEARED_SEVERITY.get(kind, "low"))
                for ep in db.q("SELECT * FROM alerts WHERE key LIKE ? AND cleared IS NULL ORDER BY id", (f"{kind}:%",))]


def sweep(db: DB, now: float | None = None) -> list[dict]:
    """Close every open episode whose condition cleared: store when and why, let the same condition
    alert again at once, and tell the chats once. Returns the episodes closed."""
    now = now or time.time()
    closed = []
    if not db.one("SELECT id FROM alerts WHERE cleared IS NULL LIMIT 1"):
        return closed   # the usual tick: no write lock taken
    with db.tx():
        for ep in db.q("SELECT * FROM alerts WHERE cleared IS NULL ORDER BY id"):
            kind, _, arg = ep["key"].partition(":")
            why = "no longer reported" if not _checkable(ep["key"]) else "condition cleared"
            if kind in ("heal", "heal-hold"):
                from . import heal
                why = heal.cleared_why(db, arg, now, hold=kind == "heal-hold")
                if why is None:
                    continue
            elif holds(db, ep["key"], _since(ep), now):
                continue
            text, sev = None, None
            if kind in CLEARED_TEXT:
                # A status line clears as quietly as it was raised.
                sev = CLEARED_SEVERITY.get(kind) or ("normal" if _urgent(ep["severity"]) else "low")
                text = "Cleared: " + CLEARED_TEXT[kind].format(arg=arg)
                if kind == "auth" and (sw := (db.kv(BREAKER + arg) or {}).get("switched")):
                    text += f" It is on another account than before the logout ({switch_text(sw)})."
            if kind in ("heal", "heal-hold") and why != "condition cleared":
                text, sev = f"Cleared: {ep['key']} user alert: {why}.", "low"
            closed.append(_close(db, ep, now, why, text, sev))
    return closed


def claims_path() -> Path:
    from .project import HOME_DIR
    return HOME_DIR / "alert-claims.json"


def claim(claim_key: str, key: str, owner: str, now: float, cleared: float = 0.0) -> bool:
    """Take or renew this user's claim on broadcasting a machine-wide condition. False while another
    project holds a live claim. A claim is stale, and taken over, once it is older than the caller's
    last cleared episode of the condition (it belongs to an earlier one) or its owner's daemon is
    gone. A claim file that cannot be read or written never silences anyone."""
    path = claims_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(path.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)   # several daemons of this user share the file
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        data = _live_claims(data, now)
        took = _take(data, claim_key, key, owner, now, cleared)
        from .project import write_json
        write_json(path, data, 0o600)
        return took
    except OSError:
        return True
    finally:
        os.close(fd)


def _live_claims(data, now: float) -> dict:
    return {k: v for k, v in (data if isinstance(data, dict) else {}).items()
            if isinstance(v, dict) and now - float(v.get("ts") or 0) < CLAIM_TTL}


def _take(data: dict, claim_key: str, key: str, owner: str, now: float, cleared: float) -> bool:
    """Take or renew the claim in `data`; False (and `data` unchanged) while another live project holds
    it. A renewal or a take-over keeps what the claim knows of the condition (since, reminders, waiting
    projects); a claim from an earlier episode starts afresh."""
    held = data.get(claim_key)
    fresh = bool(held) and float(held.get("ts") or 0) > cleared
    if fresh and held.get("owner") != owner and owner_alive(str(held.get("owner"))):
        return False
    keep = {k: held[k] for k in ("since", "reminded", "waiting") if k in held} if fresh else {}
    data[claim_key] = {**keep, "owner": owner, "alert": key, "ts": now}
    return True


def due_reminders(since: float, now: float) -> int:
    """How many logout reminders are due `now` for a logout that began at `since`: one at each
    LOGOUT_REMIND_S, then one a day after the last."""
    age = now - since
    n = sum(1 for s in LOGOUT_REMIND_S if age >= s)
    if age >= LOGOUT_REMIND_S[-1] + DAY:
        n += int((age - LOGOUT_REMIND_S[-1]) // DAY)
    return n


def machine_round(claim_key: str, key: str, owner: str, now: float, cleared: float, raised: float,
                  info: dict) -> dict | None:
    """One project's turn on a machine-wide logout: report what waits on it here (`info`: name,
    queued, events) into the shared claim, and if this project holds or may take the claim and a
    reminder is due, mark it sent and return what the reminder says: {since, reminder, waiting}
    (the live projects' reports). None otherwise, also when the claim file cannot be used: a
    reminder is extra, the first alert already went out."""
    path = claims_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(path.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        data = _live_claims(data, now)
        held = data.get(claim_key)
        if held and float(held.get("ts") or 0) <= cleared:
            held = None   # an earlier episode's: _take starts afresh
        waiting = {o: w for o, w in ((held or {}).get("waiting") or {}).items()
                   if isinstance(w, dict) and now - float(w.get("ts") or 0) < WAITER_TTL
                   and (o == owner or owner_alive(o))}
        waiting[owner] = {**info, "ts": now}
        since = min(float((held or {}).get("since") or raised), raised)
        out = None
        if held and held.get("owner") != owner and owner_alive(str(held.get("owner"))):
            data[claim_key] = {**held, "waiting": waiting, "since": since}
        else:
            _take(data, claim_key, key, owner, now, cleared)
            rec = data[claim_key]
            rec.update(waiting=waiting, since=since)
            due = due_reminders(since, now)
            if due > int(rec.get("reminded") or 0):
                rec["reminded"] = due
                out = {"since": since, "reminder": due, "waiting": list(waiting.values())}
        from .project import write_json
        write_json(path, data, 0o600)
        return out
    except OSError:
        return None
    finally:
        os.close(fd)


def release(claim_keys: list[str], cleared: float) -> None:
    """Drop every claim on these machine-wide conditions taken before an episode of theirs cleared,
    whoever holds it: the condition went away for all projects alike, so the next project that sees
    it again broadcasts it at once, even while the old claimer sits idle or is stopped."""
    path = claims_path()
    if not claim_keys or not path.exists():
        return
    try:
        fd = os.open(str(path.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        keep = {k: v for k, v in data.items()
                if not (k in claim_keys and isinstance(v, dict) and float(v.get("ts") or 0) <= cleared)}
        if keep != data:
            from .project import write_json
            write_json(path, keep, 0o600)
    except OSError:
        pass
    finally:
        os.close(fd)


def owner_alive(owner: str) -> bool:
    """The project at `owner` has a running daemon: its pid file (removed when the daemon stops)
    names a live one."""
    from .daemon import _is_daemon, _read_pid
    pid = _read_pid(Path(owner) / "state" / "daemon.pid")
    return bool(pid) and _is_daemon(pid)


KEYLESS_TTL = 3600   # seconds an alert without a condition key stays in the top section


def needs_you(db: DB, now: float, limit: int = 20) -> list[dict]:
    """The top section: open asks first, then top alerts (_top) about problems that are active now, each
    newest first. A newer alert on the same condition replaces the older one. Open asks are kept
    whatever their age and are never cut by `limit`, which only caps the alerts after them."""
    asks = db.q("SELECT id,ts,kind,severity,text,ref FROM messages WHERE direction='out' AND chat IS NULL "
                "AND kind='ask' AND handled=0 ORDER BY id DESC")
    rows = db.q("SELECT id,ts,kind,severity,text,ref FROM messages WHERE direction='out' AND chat IS NULL "
                "AND kind='alert' AND ts>? ORDER BY id DESC LIMIT 300", (now - 14 * DAY,))
    for m in asks:
        m.pop("ref")
    out, seen = [], set()
    for m in rows:
        if len(out) >= limit - len(asks):
            break
        ref = m.pop("ref")
        if not _top(m["severity"], ref):
            continue
        if ref:
            if ref in seen:
                continue
            seen.add(ref)
            if not active(db, ref, m["ts"], now) or acked(db, episode(db, ref, m["ts"])):
                continue   # over, or acknowledged (in the feed)
        elif now - m["ts"] >= KEYLESS_TTL:   # an alert with no condition to check stays an hour
            continue
        out.append(m)
    return asks + out


def feed(db: DB, now: float, limit: int = 30) -> list[dict]:
    """Everything below the top section, newest first: FYI notes and decisions, milestones, reboots,
    live status conditions (state 'status'), acknowledged live alerts (state 'acked', with why),
    cleared alerts (with when they cleared). Open asks and
    live alerts are in the top section."""
    rows = db.q("SELECT id,ts,kind,severity,text,ref FROM messages WHERE direction='out' AND chat IS NULL "
                "AND kind!='ask' AND ts>? ORDER BY id DESC LIMIT ?", (now - FEED_DAYS * DAY, limit * 3))
    out = []
    for m in rows:
        ref = m.pop("ref")
        m["state"] = "info" if m["kind"] in ("info", "resolved") else "fyi"
        if m["kind"] == "alert" and _top(m["severity"], ref):
            ep = episode(db, ref, m["ts"]) if ref else None
            if ref and active(db, ref, m["ts"], now) and (ack := acked(db, ep)):
                m["state"], m["acked_why"] = "acked", ack.get("why")   # live, but the user knows
            elif (ref and active(db, ref, m["ts"], now)) or (not ref and now - m["ts"] < KEYLESS_TTL):
                continue   # in the top section
            else:
                m["state"] = "cleared"
                m["cleared_at"] = ep["cleared"] if ep else None
        elif m["kind"] == "alert" and _status(ref, m["severity"]):   # a gate or pause that lifts by itself
            if active(db, ref, m["ts"], now):
                ep = episode(db, ref, m["ts"])
                if ep and _top(ep["severity"], ref):
                    continue   # raised since: the condition is in the top section
                m["state"] = "status"
            else:
                m["state"] = "cleared"
                ep = episode(db, ref, m["ts"])
                m["cleared_at"] = ep["cleared"] if ep else None
        out.append(m)
        if len(out) >= limit:
            break
    return out
