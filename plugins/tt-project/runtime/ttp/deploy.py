# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Post-push deploy outcomes and durable, model-free takeover checks.

Only the reporting project's database is written. Other projects are read through their daemon
receipts, never changed by verification. Ordinary `ttp deployed` remains a strict read-only probe.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import overview, project, timefmt
from .db import SEVERITY_RANK

CHECKS = "deploy_checks"
ALERTED = "deploy_alerted"   # the last deploy alert record() sent or updated: pushq sends no after_push alert of its own
GRACE_S = 300
RESTART_WAIT_S = 900         # default delivery.deploy_restart_wait_s
PROBE_S = 60
HISTORY = 20
TAKEOVERS = "deploy_takeovers"   # project name -> the host-side takeover attempts of its overdue merge
TAKEOVER_TRIES = 3
TAKEOVER_SETTLE_S = 300          # after an attempt ends, its daemon has this long to report the release
TAKEOVER_TIMEOUT_S = 900         # an attempt running this long is over (stopped, when this daemon started it)
RESUME_GRACE_S = 6 * 3600        # a paused project's upgrade, once it is resumed, is its own again for this long
PAUSED_NOTE = "{name} paused; upgrades when resumed"
_children: dict[str, subprocess.Popen] = {}   # this daemon's own attempts, by project name


def condition_key(name: str, version: str) -> str:
    return f"deploy:{name}:{version}"


def release_key(version: str) -> str:
    """The alert of a release's deploy, listing every project behind, as alerts before behind_key had one."""
    return f"deploy:{version}"


def behind_key(name: str) -> str:
    """The one ongoing alert of a project behind on releases: each new release updates it in place."""
    return f"deploy-behind:{name}"


def restart_wait(p) -> float:
    """delivery.deploy_restart_wait_s of the reporting project: how long a daemon may take to restart onto
    files already at the release before that counts as failed."""
    try:
        return float(project.push_queue_number("deploy_restart_wait_s",
                                               (p.config().get("delivery") or {}).get("deploy_restart_wait_s",
                                                                                      RESTART_WAIT_S)))
    except (ValueError, AttributeError, TypeError):
        return RESTART_WAIT_S


def _probe(rec: dict, now: float) -> dict:
    return overview.deploy_status(rec["name"], rec["entry"], rec["target"], rec.get("commit"), now,
                                  project.hostname(), allow_newer=True)


def _owned(row: dict) -> bool:
    """A step with a known owner: a merge held by a harness task, or a live daemon that restarts onto the
    new files (or completes its first tick on them) by itself."""
    return bool((row.get("merge_pending") and row.get("merge_owner")) or row.get("settling"))


def assess(rec: dict, row: dict, now: float, where=None) -> tuple[str, str, str]:
    """(outcome, kind, reason) of one probe: done; pending (an accepted owned merge before its deadline);
    or failed, kind "down" (a daemon that is down outside an upgrade) or "fault" (a current upgrade error):
    at once, whatever the deadline; "missed" (an accepted step past its deadline: a merge's takeover plus
    GRACE_S, a pending restart's bounded wait) or "unowned" (never accepted).
    Shared by record, tick and holds, so the alert clears exactly when its condition does. Times are
    shown in the zone of `where` (the reporting project)."""
    if row["state"] == overview.OK:
        return "done", "", ""
    reason = row["reason"]
    if row.get("upgrade_error"):
        return "failed", "fault", f"{row['upgrade_error']}; {reason}"
    if not row.get("daemon_alive") and not row.get("upgrading"):
        return "failed", "down", f"daemon {row.get('daemon_state') or 'unverified'}; {reason}"
    takeover = rec.get("takeover_at")
    if (_owned(row) and takeover) or rec.get("accepted"):
        # Accepted once, later steps of the same merge (files landed, a restart) wait for the same deadline.
        if now < rec["due"]:
            return "pending", "", reason
        at = timefmt.long(takeover, where) if takeover else "unknown"
        what = "still behind at its re-check" if rec.get("restart") else "missed takeover"
        return "failed", "missed", f"{what} {at} (owner {rec.get('owner') or 'unknown'}); {reason}"
    if row.get("merge_owner") and not takeover:
        reason += "; no known takeover time"
    return "failed", "unowned", reason


def _failing(db, recs: list, now: float) -> bool:
    takeovers = db.kv(TAKEOVERS) or {}
    return any(rec and not rec.get("closed") and _judge(None, rec, _probe(rec, now), dict(takeovers.get(rec["name"]) or {}),
                                                        now)[0] == "failed"
               for rec in recs)


def holds(db, arg: str, now: float) -> bool:
    """The alert `deploy:<arg>` holds (alerts before behind_key): for a release (`deploy:<version>`) while
    any project's check of it still fails; `deploy:<name>:<version>` (one project) while that does."""
    checks = db.kv(CHECKS) or {}
    recs = [checks.get("deploy:" + arg)] if ":" in arg else [r for r in checks.values() if r.get("target") == arg]
    return _failing(db, recs, now)


def holds_behind(db, name: str, now: float) -> bool:
    """The alert `deploy-behind:<name>` holds while a check of that project still fails: it clears once its
    own daemon reports the release (or its upgrade is owned and pending again)."""
    return _failing(db, [r for r in (db.kv(CHECKS) or {}).values() if r.get("name") == name], now)


def _supersede(checks: dict, now: float) -> None:
    """Close each open check of a project that an open check of a newer release replaces: the newest one
    tracks it (reaching it reaches the older ones too), so a stuck project is one check and one alert."""
    newest: dict[str, str] = {}
    for rec in checks.values():
        if not rec.get("closed") and _vnum(rec["target"]) > _vnum(newest.get(rec["name"], "")):
            newest[rec["name"]] = rec["target"]
    for rec in checks.values():
        if not rec.get("closed") and rec["target"] != newest[rec["name"]]:
            rec.update(closed=now, superseded=newest[rec["name"]])


def _vnum(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def _judge(p, rec: dict, row: dict, t: dict, now: float, start: bool = False) -> tuple[str, str, str]:
    """assess, then _takeover: the outcome of one probe `row` of `rec`."""
    return _takeover(p, rec, *assess(rec, row, now, p), t, now, start, paused=bool(row.get("paused")))


def _owner_task(rec: dict) -> int | None:
    m = re.fullmatch(r"harness task #(\d+)", str(rec.get("owner") or ""))
    return int(m[1]) if m else None


def takeover_log(p, name: str) -> Path:
    """The takeover log of project `name`: its own state/logs/takeover.log, where whoever looks into its
    upgrade looks; this (reporting) project's logs only when its folder is unknown."""
    d = ((project.load_registry().get("projects") or {}).get(name) or {}).get("dir")
    return Path(str(d)) / project.FOLDER / "state" / "logs" / "takeover.log" if d else p.logs / "takeover.log"


def _log(p, name: str, line: str) -> None:
    """One line of what the takeover did, or why it did not, in `name`'s takeover log (best effort)."""
    try:
        path = takeover_log(p, name) if p is not None else None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as log:
                log.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def launch_takeover(p, name: str, task: int | None) -> subprocess.Popen:
    """Start the installed release's host-side `ttp upgrade <name>`, taking the merge over from harness task
    `task` (past its deadline), in a session of its own: a restart of this daemon leaves it running. Its
    output goes to `name`'s own logs/takeover.log (takeover_log)."""
    from .release import installed
    ttp = installed() / "bin" / "ttp"
    if not ttp.is_file():
        raise FileNotFoundError(f"no installed release at {ttp}")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "TTP_PROJECT", "TTP_TASK", "TTP_TASK_ID", "TTP_RUN_DIR")}
    argv = [sys.executable, str(ttp), "upgrade", name] + (["--take-over-from", str(task)] if task else [])
    path = takeover_log(p, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as log:
        log.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} ttp upgrade {' '.join(argv[3:])} (host-side takeover "
                  f"by {p.name})\n")
        log.flush()
        return subprocess.Popen(argv, cwd=str(p.base), env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)


def _reap(name: str, t: dict, now: float, p=None) -> None:
    """Record the end of `name`'s running attempt: its exit code when this daemon started it, else once its
    process is gone (a restart or a reboot in between). An attempt past TAKEOVER_TIMEOUT_S is over. The end
    goes to its takeover log too (exit 75: deferred, nothing changed; 76: applied, restart deferred to the
    daemon; its output above says why)."""
    pid = int(t.get("pid") or 0)
    if not pid:
        return
    child = _children.get(name)
    late = now - float(t.get("started") or 0) > TAKEOVER_TIMEOUT_S
    if child is not None and child.pid == pid:
        rc = child.poll()
        if rc is None and late:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            rc = "timed out"
        if rc is None:
            return
        _children.pop(name, None)
    else:
        from .locks import _alive
        if _alive(pid) and not late:
            return
        rc = "timed out" if late else "unknown (it ended while this daemon was down)"
    t.update(pid=None, ended=now, rc=rc if isinstance(rc, str) else f"exit {rc}")
    _log(p, name, f"takeover ended: {t['rc']}" + {"exit 75": " (deferred: nothing was changed)",
                                                    "exit 76": " (applied; the daemon restarts when it takes the "
                                                               "request)"}.get(t["rc"], ""))


def _takeover(p, rec: dict, outcome: str, kind: str, reason: str, t: dict, now: float,
              start: bool = False, paused: bool = False) -> tuple[str, str, str]:
    """A merge past its takeover deadline is taken over, not only reported: a host-side `ttp upgrade` runs
    (`start`, by the daemon's tick; started from the first probe past the deadline, also after a restart or
    a reboot), up to TAKEOVER_TRIES times, TAKEOVER_SETTLE_S apart. Meanwhile it is pending; it fails (and
    pages) only once every try ended and the project's own daemon still does not report the release.
    A `paused` project's upgrade merges its files and restarts it, still paused; one that defers (exit 75:
    the merge needs its harness task) is parked, not retried: pending ("paused"), one low note, and once
    the project is resumed its own task and daemon get RESUME_GRACE_S before the takeover is due again.
    `t` is the project's attempt record, updated in place."""
    name = rec["name"]
    if t.get("noted") and not paused:       # resumed: the upgrade is the project's own again, for a while
        t.pop("noted", None)
        t.pop("parked", None)
        t["tries"] = 0
        rec["due"] = max(float(rec.get("due") or 0), now + RESUME_GRACE_S)
        if p is not None:
            _log(p, name, f"{name} was resumed: its own merge task and daemon take the upgrade on; the takeover "
                          f"is due again at {timefmt.long(rec['due'], p)} if it is still behind")
        if outcome == "failed" and kind == "missed":
            return "pending", "", f"resumed; its own upgrade runs now; {reason}"
    if outcome != "failed" or kind != "missed" or rec.get("restart"):
        return outcome, kind, reason
    tries = int(t.get("tries") or 0)
    if t.get("pid"):
        return "pending", "", f"host-side takeover running (try {tries} of {TAKEOVER_TRIES}); {reason}"
    if paused and (t.get("parked") or (tries and t.get("rc") == "exit 75")):
        if p is not None and not t.get("parked"):
            t["parked"] = now
            _log(p, name, PAUSED_NOTE.format(name=name) + f": the takeover deferred (exit 75: the merge needs its "
                          f"harness task, which runs only once {name} is resumed); not retried while it is paused")
        if p is not None and not t.get("noted"):
            t["noted"] = now
            p.db.post("out", PAUSED_NOTE.format(name=name) + f" (tt-project {rec['target']}: its template merge "
                      f"needs its own harness task; nothing to do).", kind="info", severity="low")
        return "pending", "paused", f"{PAUSED_NOTE.format(name=name)}; {reason}"
    if tries and now < float(t.get("ended") or 0) + TAKEOVER_SETTLE_S:
        return "pending", "", f"host-side takeover ended ({t.get('rc')}); its daemon is checked again; {reason}"
    if tries >= TAKEOVER_TRIES:
        if p is not None and not t.get("gave_up"):
            t["gave_up"] = now
            _log(p, name, f"takeover not retried: tried {tries} time{'s' if tries > 1 else ''}, last {t.get('rc')}")
        return "failed", kind, (f"{reason}; host-side takeover tried {tries} time{'s' if tries > 1 else ''}, "
                                f"last {t.get('rc')} (see {name}'s logs/takeover.log)")
    if not start:
        return "pending", "", f"host-side takeover due now; {reason}"
    from . import cyclenotice
    if p is not None and (h := cyclenotice.held(cyclenotice.project_paths(p))):   # busy: no try spent
        return "pending", "", f"host-side takeover held: {cyclenotice.line(h, p)}; {reason}"
    try:
        child = launch_takeover(p, rec["name"], _owner_task(rec))
    except OSError as e:
        t.update(tries=TAKEOVER_TRIES, ended=None, rc=f"could not start ({e})")   # a new release tries again
        _log(p, rec["name"], f"takeover could not start: {e}")
        return "failed", kind, f"{reason}; host-side takeover could not start: {e}"
    _children[rec["name"]] = child
    t.update(tries=tries + 1, pid=child.pid, started=now, ended=None, rc=None)
    return "pending", "", f"host-side takeover started (try {tries + 1} of {TAKEOVER_TRIES}); {reason}"


def _mark(rec: dict, kind: str, now: float) -> bool:
    """Whether this failure is new news (deduplicated by the stored kind, which a resolved condition
    resets in `_settle`); records it and its history if so."""
    if rec.get("alerted") and rec.get("alerted_kind") == kind:
        return False
    rec.update(alerted=now, alerted_kind=kind)
    rec["history"] = (rec.get("history") or [])[-(HISTORY - 1):] + [{"at": now, "kind": kind, "commit": rec.get("commit")}]
    return True


def _span(s: float) -> str:
    return f"{s / 86400:.1f} d" if s >= 2 * 86400 else f"{max(s, 0) / 3600:.1f} h"


def _alert(p, rec: dict, kind: str, reason: str, now: float, alert=None) -> str:
    """The one ongoing alert of a project behind (behind_key), naming the newest release it misses, how long
    it has been behind and why. High only when its daemon is down; normal otherwise. An open one is updated
    in place (no new message) unless the severity rises. Returns its key."""
    key = behind_key(rec["name"])
    sev = "high" if kind == "down" else "normal"
    since = float(rec.get("since") or ((rec.get("history") or [{}])[0].get("at")) or now)
    text = (f"Deploy failed: {rec['name']} is not running tt-project {rec['target']} (the newest release; "
            f"behind since {timefmt.long(since, p)}, {_span(now - since)}): {reason}.")
    ep = p.db.one("SELECT id, message, severity FROM alerts WHERE key=? AND cleared IS NULL ORDER BY id DESC LIMIT 1",
                  (key,))
    if ep and SEVERITY_RANK.get(sev, 1) <= SEVERITY_RANK.get(ep["severity"], 1):
        p.db.x("UPDATE alerts SET text=?, severity=?, last=? WHERE id=?", (text[:2000], sev, now, ep["id"]))
        if ep["message"]:
            p.db.x("UPDATE messages SET text=?, severity=? WHERE id=?", (text, sev, ep["message"]))
    elif alert:
        alert(key, text, severity=sev, every_s=0)
    else:
        p.db.post("out", text, kind="alert", severity=sev, ref=key)
    return key


def _settle(rec: dict, outcome: str, now: float) -> None:
    rec["outcome"] = outcome
    if outcome != "failed":
        rec.pop("alerted", None)
        rec.pop("alerted_kind", None)
    if outcome == "pending":
        rec["accepted"] = True
    if outcome == "done":
        rec["closed"] = now


def record(p, report: dict, now: float | None = None) -> dict:
    """Classify done/pending/failed, accepting only live owned merges with a takeover deadline.

    Pending is success for after_push. A repeat of the same release (version and commit) by the same
    owner keeps its original deadline across repeat deploys and daemon restarts; another commit or
    owner is a new merge with its own. The outcome is judged from the probe alone: an earlier alert
    only deduplicates, it never turns a healthy owned merge into a failure.
    """
    now = time.time() if now is None else now
    registry = project.load_registry().get("projects") or {}
    wait = restart_wait(p)
    pending, rows, failed = [], [], []
    with p.db.tx():
        checks = p.db.kv(CHECKS) or {}
        takeovers = p.db.kv(TAKEOVERS) or {}
        for row in report["projects"]:
            row = dict(row)
            key = condition_key(row["name"], report["target"])
            old = checks.get(key) or {}
            owner = row.get("owner") if row.get("settling") else row.get("merge_owner")
            same = (old and not old.get("closed") and old.get("commit") == report.get("commit")
                    and (not owner or old.get("owner") in (None, owner)))
            rec = dict(old) if same else {k: old[k] for k in ("history",) if k in old}
            # Behind since its first open check: a project stuck across releases keeps that time.
            since = [float(c.get("since") or 0) for c in checks.values() if c.get("name") == row["name"]
                     and not c.get("closed") and c.get("since")]
            rec.setdefault("since", min(since + [now]))
            rec.update(name=row["name"], target=report["target"], commit=report.get("commit"),
                       entry=registry.get(row["name"]) or {})
            if not rec.get("takeover_at") and row.get("settling"):
                # Its daemon restarts onto the files (or ticks on them) by itself: re-checked after the wait.
                # A restart still pending (or already failed) for an earlier release keeps that deadline:
                # a daemon that never restarts cannot push its re-check back with each release.
                at = min([now + wait] + [float(c["takeover_at"]) for c in checks.values()
                                         if c.get("name") == row["name"] and not c.get("closed")
                                         and c.get("restart") and c.get("takeover_at")])
                rec.update(owner=owner, takeover_at=at, due=at, restart=True)
            elif not rec.get("takeover_at"):
                # The same owner still holding the merge of an earlier release keeps that release's deadline:
                # a task that holds every release in turn cannot push its takeover back with each one.
                earlier = [float(c["takeover_at"]) for c in checks.values() if c.get("name") == row["name"]
                           and not c.get("closed") and not c.get("restart") and c.get("takeover_at")
                           and owner and c.get("owner") == owner]
                at = row.get("takeover_at")
                rec.update(owner=row.get("merge_owner"), takeover_at=min([at] + earlier) if at else at)
                rec["due"] = rec["takeover_at"] + GRACE_S if rec["takeover_at"] else now
            if not same and not rec.get("restart"):
                t = takeovers.setdefault(row["name"], {})
                t["tries"] = 0     # a new release gets its own tries (a paused project: its own files-only try)
                t.pop("parked", None)
                t.pop("gave_up", None)
            outcome, kind, reason = _judge(p, rec, row, takeovers.setdefault(row["name"], {}), now)
            row["outcome"] = outcome
            if outcome == "done" and not old:
                rows.append(row)
                continue
            was_pending = same and old.get("outcome") == "pending"
            _settle(rec, outcome, now)
            if outcome == "failed":
                rec.pop("closed", None)
                failed.append((rec, kind, reason, _mark(rec, kind, now)))
            elif outcome == "pending" and not was_pending and kind != "paused":
                at = timefmt.long(max(rec["takeover_at"], now), p)
                pending.append(f"{row['name']}: restart onto the new files pending, owner {rec['owner']}, "
                               f"re-checked {at}" if rec.get("restart") else
                               f"{row['name']}: paused (stays paused), owner {rec['owner']}, host-side takeover "
                               f"{at}" if row.get("paused") else
                               f"{row['name']}: owner {rec['owner']}, takeover {at}")
            checks[key] = rec
            rows.append(row)
        _supersede(checks, now)
        p.db.set_kv(CHECKS, checks)   # before the alert: condition readers can verify it at once
        p.db.set_kv(TAKEOVERS, takeovers)
        if failed:   # already alerted (still open), or alerted now: after_push's failure is this alert
            keys = [_alert(p, rec, kind, reason, now) if new and not rec.get("closed") else behind_key(rec["name"])
                    for rec, kind, reason, new in failed]
            p.db.set_kv(ALERTED, {"key": keys[0], "keys": keys, "target": report["target"],
                                  "commit": report.get("commit"), "at": now})
        if pending:
            p.db.post("out", f"tt-project {report['target']} deploy pending: " + "; ".join(pending)
                      + ". Follow-up checks run after takeover with a 5 min grace, and after a pending "
                        "restart's wait.", kind="info", severity="low")
    failed = any(r["outcome"] == "failed" for r in rows) or (not rows and report["rc"] != 0)
    return {**report, "projects": rows, "rc": int(failed)}


def tick(p, alert, now: float | None = None) -> None:
    """Called even while paused, once a minute per check. Close caught-up projects quietly; alert a
    daemon that stops or fails to come up, or a current upgrade error, at the next probe; alert a merge
    that missed its takeover only after its deadline. A resolved condition resets its deduplication,
    and its alert clears through holds_behind. One ongoing alert per project a probe finds newly failing,
    updated in place by the next release it misses."""
    now = time.time() if now is None else now
    with p.db.tx():
        checks = p.db.kv(CHECKS) or {}
        before = {k: bool(r.get("closed")) for k, r in checks.items()}
        _supersede(checks, now)   # checks kept per release by earlier versions
        changed = before != {k: bool(r.get("closed")) for k, r in checks.items()}
        takeovers = p.db.kv(TAKEOVERS) or {}
        for name, t in takeovers.items():
            _reap(name, t, now, p)
        failing: list = []
        for key, rec in checks.items():
            if rec.get("closed") or now < float(rec.get("next_probe") or 0):
                continue
            rec["next_probe"] = now + PROBE_S
            changed = True
            outcome, kind, reason = _judge(p, rec, _probe(rec, now), takeovers.setdefault(rec["name"], {}), now,
                                           start=True)
            _settle(rec, outcome, now)
            if outcome == "failed" and _mark(rec, kind, now):
                failing.append((rec, kind, reason))
        live = {rec["name"] for rec in checks.values() if not rec.get("closed")}
        takeovers = {n: t for n, t in takeovers.items() if n in live or t.get("pid")}
        if changed or takeovers != (p.db.kv(TAKEOVERS) or {}):
            p.db.set_kv(CHECKS, checks)
            p.db.set_kv(TAKEOVERS, takeovers)
        for rec, kind, reason in failing:
            _alert(p, rec, kind, reason, now, alert)
