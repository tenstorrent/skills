# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Every project on this machine at a glance (`ttp overview`, `ttp list --status`, the web app's
Projects tab, /api/overview).

One line per registered project: its daemon (running, stopped, or stale when its process is up
but its heartbeat stopped), its harness runtime version against the installed release, open asks,
tasks waiting on the user, running workers and today's spend against its cap. A footer gives the
account's global daily cap.

Strictly read-only towards other projects: each database is opened read-only (globalcap.connect_ro), the
settings are read from project.json directly (Project.read_config keeps a last-good copy, a
write) and no lock is taken. Projects on other machines are listed as not checked.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from . import globalcap as gcap
from . import identity, project
from .release import installed, is_newer, runtime_version

GATE_FRESH_S = 900   # a daemon's last global count older than this is not shown as current


def _settings(base: Path) -> dict:
    try:
        raw = json.loads((base / "harness" / "project.json").read_text())
    except (OSError, ValueError):
        raw = {}
    return project.layered(raw if isinstance(raw, dict) else {})


def _daemon(conn: sqlite3.Connection, state: Path, here: str) -> tuple[str, int]:
    """("running", "stopped", "unknown (...)" or "stale (...)", the running daemon's pid or 0), by
    identity.probe: the same answer `ttp status` gives, a fresh heartbeat standing in for a pid probe
    a sandboxed worker cannot make."""
    r = conn.execute("SELECT value FROM kv WHERE key='daemon'").fetchone()
    try:
        d = json.loads(r[0]) if r else {}
    except ValueError:
        d = {}
    s = identity.probe(state, d, here)
    if s["state"] == "stale":
        return f"stale (no tick for {int(s['age'] // 60)} min)", s["pid"]
    if s["state"] == "unknown":
        return f"unknown (heartbeat {int(s['age'])}s old, pid probe failed in this sandbox)", 0
    return s["state"], s["pid"]


def _gate_global(conn: sqlite3.Connection) -> tuple[float, dict]:
    """(when, {provider: gate numbers}) of the daemon's last global count."""
    r = conn.execute("SELECT value, ts FROM kv WHERE key='gates'").fetchone()
    try:
        gates = json.loads(r[0]) if r else {}
    except ValueError:
        gates = {}
    out = {prov: g["numbers"] for prov, g in (gates.items() if isinstance(gates, dict) else [])
           if isinstance(g, dict) and isinstance(g.get("numbers"), dict) and "global_today" in g["numbers"]}
    return (float(r[1] or 0) if r else 0.0), out


def _gate_slots(conn: sqlite3.Connection, configured: int) -> list[str]:
    """budget.slot_lines from the daemon's last gates: each provider whose budget gate allows fewer
    worker slots than budget.max_parallel_workers, with its running workers."""
    from . import budget as bud
    try:
        r = conn.execute("SELECT value FROM kv WHERE key='gates'").fetchone()
        gates = json.loads(r[0]) if r else {}
        running = dict(conn.execute("SELECT provider, COUNT(*) FROM runs WHERE status='running' "
                                    "AND role!='coordinator' GROUP BY provider").fetchall())
    except (sqlite3.Error, ValueError):   # an older schema or a torn value: no slot line
        return []
    return bud.slot_lines(gates if isinstance(gates, dict) else {}, configured, running)


class _ReadOnly:
    """What waits.blocked reads of a project (q, paused_resources) over a read-only connection, so
    the overview counts 'waiting on you' as `ttp status` and the web app do. Shared pauses are the
    ones the project last acted on (its kv): reading the share list would write its settings copy."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        cur = self.conn.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def paused_resources(self) -> dict[str, dict]:
        from .db import PAUSED_RESOURCES_KEY, SHARED_SEEN_KEY
        out = {}
        for key in (SHARED_SEEN_KEY, PAUSED_RESOURCES_KEY):   # the project's own pause wins
            r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            try:
                v = json.loads(r[0]) if r else {}
            except (TypeError, ValueError):
                v = {}
            out.update(v if isinstance(v, dict) else {})
        return out


def project_status(name: str, entry: dict, now: float, here: str, lib_v: str, window: tuple[float, float]) -> dict:
    """One project's line as data. Never writes to the project."""
    row = {"name": name, "host": entry.get("host"), "dir": entry.get("dir"), "remote": False, "ok": False}
    url = entry.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        row["url"] = url
    if entry.get("host") not in (None, here):
        row.update(remote=True, note="remote, not checked")
        return row
    base = Path(str(entry.get("dir") or "")) / project.FOLDER
    db = base / "state" / "project.db"
    if not entry.get("dir") or not db.is_file():
        row["note"] = "no project database at its folder"
        return row
    harness_v = runtime_version(base / "harness" / "runtime")
    row.update(version=harness_v, lib=lib_v,
               lag=bool(lib_v != "unknown" and is_newer(lib_v, harness_v)),
               ahead=bool(harness_v != "unknown" and is_newer(harness_v, lib_v)))
    b = _settings(base)["budget"]
    day = gcap.day_bounds(b, now)
    start, label = (day[0], "today") if day else (now - gcap.DAY, "24h")
    try:
        conn = gcap.connect_ro(db, timeout=2)
    except sqlite3.Error as e:
        row["note"] = f"database unreadable: {e}"
        return row
    try:
        row["daemon"], pid = _daemon(conn, base / "state", here)
        # what the daemon process loaded, against the files a restart would load
        rt = identity.summary(base / "state", pid, base / "harness" / "runtime", _settings(base), now)
        row.update({k: rt[k] for k in ("running_version", "running_commit", "instance", "tick_done",
                                       "files_version", "restart_pending")})
        row["asks"] = conn.execute("SELECT COUNT(*) FROM messages WHERE kind='ask' AND handled=0").fetchone()[0]
        from . import waits
        w = waits.on_user(_ReadOnly(conn), now, ages=False)
        row["waiting_on_user"] = w["count"]
        row["wait_asks"] = len(w["asks"])
        row["wait_tasks"] = len(w["tasks"]) + len(w["gated"])
        row["running"] = conn.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0]
        from . import billing
        from .db import counted_spend
        billed = sum(billing.billed_by_account(conn, start, running_at=now).values())
        where, args = counted_spend(start)
        spent = float(conn.execute(f"SELECT COALESCE(SUM(usd),0) FROM ledger WHERE {where}", args).fetchone()[0])
        row.update(spend_label=label, billed_usd=round(billed, 2), plan_usd=round(max(spent - billed, 0.0), 2),
                   daily_cap=float(b.get("daily_usd") or 0))
        row["global_rows"] = gcap._rows(conn, *window)
        row["gate_ts"], row["gate_global"] = _gate_global(conn)
        row["slots"] = _gate_slots(conn, int(b.get("max_parallel_workers", 6) or 0))
        row["ok"] = True
    except sqlite3.Error as e:
        row["note"] = f"database unreadable: {e}"
    finally:
        conn.close()
    return row


def overview(now: float | None = None) -> dict:
    """{"projects": [...], "global": {...}, "lib": version}: every registered project, and the
    account's global daily cap."""
    now = now or time.time()
    here = project.hostname()
    lib_v = runtime_version(installed() / "runtime")
    account = project.layered({})["budget"]
    start, end, rolling = gcap.window(account, now)
    rows = [project_status(n, e if isinstance(e, dict) else {}, now, here, lib_v, (start, end))
            for n, e in sorted((project.load_registry().get("projects") or {}).items())]
    local: dict[str, float] = {}
    for r in rows:
        for x in r.pop("global_rows", None) or []:
            local[x["provider"]] = local.get(x["provider"], 0.0) + x["usd"]
    # The daemons count the global total with the other machines' answers; the freshest count wins.
    counted = max(((r["gate_ts"], r["name"], r["gate_global"]) for r in rows if r.get("gate_global")),
                  default=None)
    for r in rows:
        r.pop("gate_ts", None)
        r.pop("gate_global", None)
    g = {"cap": float(account.get("global_daily_usd") or 0), "target": float(account.get("daily_target_usd") or 0),
         "label": "per 24h" if rolling else "today",
         "local": {k: round(v, 2) for k, v in local.items() if round(v, 2)}, "local_projects": sum(1 for r in rows if r["ok"])}
    if counted and now - counted[0] <= GATE_FRESH_S:
        g["counted"] = {"by": counted[1], "age_s": round(now - counted[0]),
                        "providers": {p: {"usd": n.get("global_today"), "stale": n.get("global_stale") or []}
                                      for p, n in counted[2].items()}}
    return {"projects": rows, "global": g, "lib": lib_v, "host": here, "now": now}


def _ago(s: float) -> str:
    return f"{int(s // 60)} min" if s >= 60 else f"{int(s)} s"


def spend(r: dict) -> str:
    cap = f" of ${r['daily_cap']:.0f} cap" if r["daily_cap"] else " billed, no cap"
    spend = f"{r['spend_label']} {gcap.money(r['billed_usd'])}{cap}"
    if r["plan_usd"]:
        spend += f", +{gcap.money(r['plan_usd'])} plan-billed"
    return spend


def line(r: dict) -> str:
    if not r["ok"]:
        return f"{r['name']}\t{r.get('note')}" + (f"\t{r['host']}:{r['dir']}" if r.get("remote") else "")
    v = r["version"] + (f" (behind installed {r['lib']})" if r["lag"] else
                        f" (ahead of installed {r['lib']})" if r["ahead"] else "")
    if r.get("restart_pending"):
        v += f" (restart pending: running {r['running_version']})"
    elif r.get("running_version") == "unverified":
        v += " (running version unverified)"
    return (f"{r['name']}\tdaemon {r['daemon']} · v{v}"
            f" · waiting on you: {r['waiting_on_user']} ({r['wait_asks']} ask{'s' if r['wait_asks'] != 1 else ''},"
            f" {r['wait_tasks']} task{'s' if r['wait_tasks'] != 1 else ''}) · {r['running']} running · {spend(r)}"
            + "".join(f" · {x}" for x in r.get("slots") or []))


def target_line(g: dict) -> str:
    """Today's Claude spend against budget.daily_target_usd ('' when unset): every machine's count when
    a daemon counted it lately, else this machine's."""
    t = float(g.get("target") or 0)
    if t <= 0:
        return ""
    c = (g.get("counted") or {}).get("providers") or {}
    usd = float((c.get("claude") or {}).get("usd") or 0) if "claude" in c else float((g.get("local") or {}).get("claude") or 0)
    where = "every machine" if "claude" in c else "this machine"
    diff = f"{gcap.money(usd - t)} over" if usd > t else f"{gcap.money(t - usd)} under"
    return f"daily target ${t:.0f}: Claude {gcap.money(usd)} {g['label']} ({where}), {diff}"


def footer(g: dict) -> str:
    if (tl := target_line(g)):
        return _footer(g) + "\n" + tl
    return _footer(g)


def _footer(g: dict) -> str:
    if not g["cap"]:
        return "global daily cap: off (budget.global_daily_usd is 0)"
    c = g.get("counted")
    if c:
        parts = [f"{p} {gcap.money(float(x['usd'] or 0))}" + (f" ({len(x['stale'])} machine{'s' if len(x['stale']) != 1 else ''} stale)"
                                                               if x["stale"] else "")
                 for p, x in sorted(c["providers"].items())]
        return (f"global daily cap ${g['cap']:.0f} {g['label']}: {', '.join(parts)}, every machine counted "
                f"(by {c['by']}'s daemon {_ago(c['age_s'])} ago)")
    local = ", ".join(f"{p} {gcap.money(u)}" for p, u in sorted(g["local"].items())) or "$0.00"
    return (f"global daily cap ${g['cap']:.0f} {g['label']}: {local} billed by this machine's "
            f"{g['local_projects']} project{'s' if g['local_projects'] != 1 else ''}; other machines not counted "
            f"(no daemon here counted them in the last {GATE_FRESH_S // 60} min)")


def text(ov: dict) -> str:
    if not ov["projects"]:
        return "no projects registered on this machine"
    return "\n".join([line(r) for r in ov["projects"]] + [footer(ov["global"])])


# `ttp deployed`: a deploy is accepted only from what each daemon reports it runs ---------------------
DEPLOY_FRESH_S = 300    # the receipt's instance must have completed a tick this recently
UPGRADE_RUN_S = 900     # an automatic upgrade of the release started this recently still runs
OK, PENDING, CANNOT = 0, 1, 2


class _RO:
    """The one-row query release.task_activity needs, on a read-only connection."""
    def __init__(self, conn: sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        self.conn = conn

    def one(self, sql: str, args=()) -> dict | None:
        r = self.conn.execute(sql, tuple(args)).fetchone()
        return dict(r) if r else None

    def kv(self, key: str) -> tuple[dict, float]:
        r = self.one("SELECT value, ts FROM kv WHERE key=?", (key,))
        try:
            v = json.loads(r["value"]) if r else {}
        except ValueError:
            v = {}
        return (v if isinstance(v, dict) else {}), float((r or {}).get("ts") or 0)


def _same_commit(a: str | None, b: str | None) -> bool:
    a, b = (a or "").strip(), (b or "").strip()
    return bool(a and b and len(min(a, b, key=len)) >= 7 and (a.startswith(b) or b.startswith(a)))


def _holder(db: _RO, state: Path) -> int | None:
    """The open finish task holding the template merge (release.finish_holder, read-only)."""
    from .release import UPGRADE_RESOURCE, UPGRADE_TASK_TITLE
    try:
        rec = json.loads((state / "locks" / f"{UPGRADE_RESOURCE}.task").read_text() or "{}")
    except (OSError, ValueError):
        rec = {}
    open_ = "status NOT IN ('done','failed','cancelled')"
    if isinstance(rec, dict) and rec.get("task"):
        if db.one(f"SELECT id FROM tasks WHERE id=? AND {open_}", (int(rec["task"]),)):
            return int(rec["task"])
    t = db.one(f"SELECT id FROM tasks WHERE kind='harness' AND title=? AND {open_} ORDER BY id LIMIT 1",
               (UPGRADE_TASK_TITLE,))
    return int(t["id"]) if t else None


def _paused(db: _RO) -> bool:
    r = db.one("SELECT value FROM kv WHERE key='paused'")
    return bool(r and r["value"] in ("true", "1"))


def _merge_head(harness: Path) -> tuple[str, float] | None:
    """(sha, mtime) of the MERGE_HEAD an unfinished merge left in the live harness checkout, else None."""
    import subprocess
    if not (harness / ".git").exists():     # not its own checkout: never read an enclosing repository's
        return None
    try:
        r = subprocess.run(["git", "-C", str(harness), "rev-parse", "--git-path", "MERGE_HEAD"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    rel = r.stdout.strip()
    if r.returncode != 0 or not rel:
        return None
    path = Path(rel) if Path(rel).is_absolute() else harness / rel
    try:
        return path.read_text().split()[0], path.stat().st_mtime
    except (OSError, IndexError):
        return None


def _age(s: float) -> str:
    return _ago(s) if s < 3600 else f"{int(s // 3600)} h" if s < 86400 else f"{int(s // 86400)} d"


def _next_check(db: _RO, now: float) -> float | None:
    """When the project's daemon next compares the installed release with its harness: its last check
    (release.KV_CHECKED) plus release.CHECK_S, in the past when that check is overdue. A daemon that
    records no check time (an older release): the last drift change, a check too, while that bounds
    a time still ahead; else None (unknown, but a ticking daemon checks within CHECK_S of now)."""
    from .release import CHECK_S, KV_CHECKED, KV_RELEASE
    _, checked = db.kv(KV_CHECKED)
    if checked:
        return checked + CHECK_S
    _, seen = db.kv(KV_RELEASE)
    return seen + CHECK_S if seen and seen + CHECK_S >= now else None


def _behind(name: str, db: _RO, state: Path, files: str, now: float, where, target: str,
            commit: str | None) -> tuple[str, str, str]:
    """(reason, owner, resumes) for a project whose harness files are not at the `target` (`commit`) yet.
    The owner is one that can act on that project: its daemon, task or coordinator, never the deploy's."""
    from . import timefmt
    from .release import KV_AUTO, STALE_S, task_activity
    at = lambda ts: timefmt.short(max(ts, now), where, now)  # noqa: E731
    due = _next_check(db, now)
    next_check = ("at its next release check (hourly)" if due is None else
                  f"at {at(due)} (its next release check)" if due >= now else
                  f"at its release check, overdue since {timefmt.short(due, where, now)}")
    tid = _holder(db, state)
    if tid:
        t, last = task_activity(db, tid)
        status = t["status"] if t else "gone"
        if _paused(db):     # its task cannot run: the pause holds it, not a deadline that would slide
            return (f"files at {files}: paused; merge held by harness task #{tid} ({status})",
                    f"harness task #{tid}", f"when {name} is resumed")
        return (f"files at {files}: merge held by harness task #{tid} ({status})", f"harness task #{tid}",
                f"when it ends, or at {at(now if status == 'blocked' else last + STALE_S)} when a host-side "
                f"`ttp upgrade {name}` may take it over")
    if _paused(db):         # nothing upgrades it by itself: the deploy's host-side takeover merges its files
        return (f"files at {files}: paused; not upgraded yet", "the host-side takeover (project paused)",
                f"at the deploy's host-side `ttp upgrade {name}`, or when {name} is resumed")
    rec, _ = db.kv(KV_AUTO)
    if not _concerns(rec, target, commit):
        rec = {}        # an older release's record is history: it does not explain this deploy
    if rec.get("outcome") == "running":     # its own try briefly leaves MERGE_HEAD in the live checkout
        return f"files at {files}: upgrading now", "this project's daemon", "when the upgrade ends"
    harness = state.parent / "harness"
    mh = _merge_head(harness)
    if mh:              # an unfinished merge in the live checkout holds every release, whatever failed before
        from types import SimpleNamespace
        from . import harnesstree, upjournal
        why = f"files at {files}: harness mid-merge (MERGE_HEAD {mh[0][:12]}, {_age(max(now - mh[1], 0))} old)"
        if harnesstree.landing_merge(harness):      # its own landing: the daemon's sweep aborts it
            return why, "this project's daemon", "when its harness worktree landing ends"
        journal = upjournal.read(SimpleNamespace(state=state))
        if journal is not None:     # a `ttp upgrade` mid-merge: it ends, or the daemon rolls a dead one back
            return why, "this project's daemon", next_check if upjournal.dead(journal) else "when the upgrade ends"
        # anyone else's merge: every upgrade refuses it until someone aborts it there
        return why, f"{name}'s coordinator", "once `git merge --abort` runs in its harness"
    if rec.get("outcome") == "deferred":
        why = " ".join(str(rec.get("why") or "").split())[:160]
        return (f"files at {files}: merge deferred" + (f" ({why})" if why else ""), "this project's daemon",
                f"at {at(float(rec.get('until') or 0))}")
    if rec.get("outcome") == "failed":
        why = " ".join(str(rec.get("why") or "see logs/upgrade.log").split())[:160]
        return (f"files at {files}: the last upgrade failed ({why})", f"{name}'s coordinator",
                "once the failure is fixed")
    return f"files at {files}: not upgraded yet", "this project's daemon", next_check


def _concerns(auto: dict, target: str, commit: str | None) -> bool:
    """An automatic upgrade record (its key is "version commit") concerns the `target` deploy: a try of
    that release (the same commit, when one is given) or of a newer one. An older release's failure, or
    another commit of the same version, is history; a record without a key cannot be placed, so counts."""
    v, _, c = str(auto.get("key") or "").partition(" ")
    if not v:
        return True
    if v != target:
        return is_newer(v, target)
    return not commit or not c or _same_commit(c, commit)


def _contains(commit: str | None, running: str | None) -> bool | None:
    """The running release's commit has the target `commit` in its history, by git in the caller's
    checkout; None when git cannot tell there (no commit, not a repository, a commit it does not have)."""
    import subprocess
    if not commit or not running or running == "unknown":
        return None
    try:
        r = subprocess.run(["git", "merge-base", "--is-ancestor", commit, running], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(r.returncode)


def _merge_unlanded(db: _RO, target: str, auto: dict) -> dict | None:
    """The newest template-merge task when it ended (failed, done or cancelled by the project) without
    landing `target`: {"task", "status", "files"}, the conflicting files (else the merge's problem), or None."""
    from .release import FENCED_PREFIX, KV_STUCK, UPGRADE_TASK_TITLE
    t = db.one("SELECT id, status, blocked_reason, spec FROM tasks WHERE kind='harness' AND title=? "
               "ORDER BY id DESC LIMIT 1", (UPGRADE_TASK_TITLE,))
    if (not t or t["status"] not in ("done", "failed", "cancelled")
            or str(t["blocked_reason"] or "").startswith(FENCED_PREFIX)
            or not re.search(rf"^Target release: tt-project {re.escape(target)} ", t["spec"] or "", re.M)):
        return None
    why = (db.kv(KV_STUCK)[0] or {}).get("why") or auto.get("why") or "unknown (see its logs/upgrade.log)"
    m = re.match(r"the merge conflicts in (.+)", why)
    return {"task": int(t["id"]), "status": t["status"], "files": f"conflicting files: {m[1].strip()}" if m else why}


def deploy_status(name: str, entry: dict, target: str, commit: str | None, now: float, here: str,
                  fresh_s: float = DEPLOY_FRESH_S, allow_newer: bool = False) -> dict:
    """Whether project `name` runs `target` (and `commit`, when given): {"name", "state": OK, PENDING or
    CANNOT, "line", ...}. Read only, like project_status: the database read-only, no lock taken."""
    from .release import KV_AUTO, runtime_commit
    row = {"name": name, "state": CANNOT, "reason": "", "owner": None, "resumes": None}

    def done(state: int, reason: str, owner: str | None = None, resumes: str | None = None) -> dict:
        row.update(state=state, reason=reason, owner=owner, resumes=resumes)
        tail = "".join([f", resumes {resumes}" if resumes else "", f", owner: {owner}" if owner else ""])
        row["line"] = f"{name}\t{('ok', 'pending', 'cannot tell')[state]}: {reason}{tail}"
        return row

    if entry.get("host") not in (None, here):
        return done(CANNOT, f"registered on {entry.get('host')}, not this machine")
    base = Path(str(entry.get("dir") or "")) / project.FOLDER
    state, db_path = base / "state", base / "state" / "project.db"
    if not entry.get("dir") or not db_path.is_file():
        return done(CANNOT, "no project database at its folder")
    where = _settings(base)
    files = runtime_version(base / "harness" / "runtime")
    files_c = runtime_commit(base / "harness" / "runtime")
    at_target = files == target and (not commit or _same_commit(files_c, commit))
    try:
        conn = gcap.connect_ro(db_path, timeout=2)
    except sqlite3.Error as e:
        return done(CANNOT, f"database unreadable: {e}")
    try:
        db = _RO(conn)
        daemon, pid = _daemon(conn, state, here)
        row["daemon_alive"] = bool(pid) and daemon == "running"
        row["daemon_state"] = daemon
        tid = _holder(db, state)
        row["merge_pending"] = bool(tid and not at_target)
        row["paused"] = _paused(db)
        auto, _ = db.kv(KV_AUTO)
        cur = auto if _concerns(auto, target, commit) else {}   # an older release's record is history
        if cur.get("outcome") == "failed":
            row["upgrade_error"] = auto.get("why") or "the last upgrade failed"
        # Its own upgrade restarts the daemon: down meanwhile is a step, not a fault (as release.line).
        row["upgrading"] = (auto.get("outcome") == "running" and bool(auto.get("key")) and
                            _concerns(auto, target, commit) and now - float(auto.get("ts") or 0) < UPGRADE_RUN_S)
        if not tid and not at_target:
            row["merge_unlanded"] = _merge_unlanded(db, target, cur)
        if tid:
            from .release import STALE_S, task_activity
            task, last = task_activity(db, tid)
            row["merge_owner"] = f"harness task #{tid}"
            if task and last:   # a paused project's task cannot run: its merge is taken over (deploy.py) now
                row["takeover_at"] = now if task["status"] == "blocked" or row["paused"] else last + STALE_S
        elif row["paused"] and not at_target:
            # Nothing upgrades a paused project by itself: the host-side takeover merges its files now.
            row.update(merge_pending=True, merge_owner="the host-side takeover (project paused)", takeover_at=now)
        elif row["upgrading"] and not at_target and not daemon.startswith("stale"):
            # Its own upgrade of the release runs now (a frozen daemon owns nothing): owned by its daemon
            # until that try's window ends.
            row.update(merge_pending=True, merge_owner="this project's daemon",
                       takeover_at=float(auto["ts"]) + UPGRADE_RUN_S)
        elif (not at_target and row["daemon_alive"] and (where.get("upgrade") or {}).get("auto", True)
              and cur.get("outcome") not in ("running", "failed")):
            # Its live daemon merges the release by itself: at its next release check, or when a deferred
            # merge's wait ends. Past that deadline deploy.assess reports it missed.
            from .release import CHECK_S
            if cur.get("outcome") == "deferred":
                at = float(cur.get("until") or 0) or now + CHECK_S
            else:   # its real next check; unknown: a ticking daemon checks within CHECK_S of now
                due = _next_check(db, now)
                at = now + CHECK_S if due is None else due
            row.update(merge_pending=True, merge_owner="this project's daemon", takeover_at=at)
        if not pid:
            if not at_target:
                return done(PENDING, *_behind(name, db, state, files, now, where, target, commit))
            return done(PENDING, f"files at {files}, daemon {daemon}", "this project's service", "when its daemon starts")
        r = identity.running(state, pid)
        if not r:
            return done(CANNOT, f"daemon {pid} reports no runtime receipt (running version unverified)")
        if r.get("provenance") != "verified":
            return done(CANNOT, f"receipt unverified: {r.get('why') or 'unknown provenance'}")
        rv, rc = str(r.get("version") or "?"), r.get("commit")
        row.update(running_version=rv, running_commit=rc, instance=r.get("instance"),
                   daemon_started=float(r.get("started") or 0))
        # A newer release passes when it holds the target commit: by git where it can tell, else by version.
        newer = allow_newer and is_newer(rv, target)
        if newer and _contains(commit, rc) is False:
            return done(PENDING, f"daemon runs {rv} ({(rc or '?')[:12]}), which does not contain {commit[:12]}",
                        "the next release that contains it", "when it is deployed")
        if not newer and (rv != target or (commit and not _same_commit(rc, commit))):
            running = f"{rv} ({(rc or '?')[:12]})"
            if not at_target:
                reason, owner, resumes = _behind(name, db, state, files, now, where, target, commit)
                return done(PENDING, f"{reason}; daemon runs {running}", owner, resumes)
            row["settling"] = True   # its own daemon finishes it: deploy.assess gives it a bounded deadline
            return done(PENDING, f"files at {files}, daemon still runs {running}: restart pending",
                        "this project's daemon", "when it restarts onto the new files")
        tick = identity.tick_done(state, r)
        rt = Path(r["runtime"]) if r.get("runtime") else base / "harness" / "runtime"
        try:
            installed_at = max((rt / "ttp" / f).stat().st_mtime for f in ("__init__.py", identity.SOURCE_FILE)
                               if (rt / "ttp" / f).exists())
        except ValueError:
            installed_at = 0.0
        row["tick_done"] = tick
        row["settling"] = not tick or tick < installed_at
        if not tick:
            return done(PENDING, f"daemon runs {rv} but has not completed a tick yet", "this project's daemon",
                        "at its first completed tick")
        if tick < installed_at:
            return done(PENDING, f"daemon runs {rv} but has not completed a tick since the files were installed",
                        "this project's daemon", "at its next completed tick")
        if now - tick > fresh_s:
            return done(PENDING, f"daemon runs {rv} but its last completed tick was {_ago(now - tick)} ago "
                                 f"(over {_ago(fresh_s)}; daemon {daemon})", "this project's daemon",
                        "at its next completed tick")
        ago = _ago(max(now - tick, 0))
        if newer:
            return done(OK, f"runs {rv} ({(rc or '?')[:12]}), which contains {target}, last tick {ago} ago")
        return done(OK, f"running {target} ({(rc or '?')[:12]}), last tick {ago} ago")
    except sqlite3.Error as e:
        return done(CANNOT, f"database unreadable: {e}")
    finally:
        conn.close()


def deployed(target: str | None = None, commit: str | None = None, names: list[str] | None = None,
             fresh_s: float = DEPLOY_FRESH_S, now: float | None = None, allow_newer: bool = False) -> dict:
    """{"rc", "target", "commit", "projects": [...], "text"} for `ttp deployed`. The registry is read now,
    so a project registered after the deploy started is checked too; projects on other machines are
    not (unless named). rc: 0 every project passes, 1 some are pending, 2 some cannot be told."""
    from .release import runtime_commit
    now = now or time.time()
    here = project.hostname()
    lib = installed() / "runtime"
    if not target:
        target, commit = runtime_version(lib), commit or runtime_commit(lib)
    elif not commit and runtime_version(lib) == target:
        commit = runtime_commit(lib)
    commit = None if commit in (None, "", "unknown") else commit
    out = {"target": target, "commit": commit, "projects": []}
    if not target or target == "unknown":
        return {**out, "rc": CANNOT, "text": "cannot tell: no version given and no release installed (`ttp setup`)"}
    reg = project.load_registry().get("projects") or {}
    pick = sorted(reg) if not names else names
    rows = []
    for n in pick:
        e = reg.get(n)
        if not isinstance(e, dict):
            rows.append({"name": n, "state": CANNOT, "reason": "not registered on this machine",
                         "line": f"{n}\tcannot tell: not registered on this machine"})
        elif names or e.get("host") in (None, here):
            rows.append(deploy_status(n, e, target, commit, now, here, fresh_s, allow_newer))
    rc = max((r["state"] for r in rows), default=OK)
    head = f"tt-project {target}" + (f" ({commit[:12]})" if commit else "")
    counts = {s: sum(1 for r in rows if r["state"] == s) for s in (OK, PENDING, CANNOT)}
    first = (f"{head}: deployed to all {len(rows)} project{'s' if len(rows) != 1 else ''} on this machine"
             if rc == OK else f"{head}: {counts[OK]} of {len(rows)} running it, {counts[PENDING]} pending"
             + (f", {counts[CANNOT]} cannot tell" if counts[CANNOT] else ""))
    if not rows:
        first = f"{head}: no projects registered on this machine"
    lines = [first] + [r["line"] for r in sorted(rows, key=lambda r: -r["state"])]   # what holds it back first
    return {**out, "rc": rc, "projects": rows, "text": "\n".join(lines)}
