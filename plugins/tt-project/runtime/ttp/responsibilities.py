# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""What a project keeps running, which of it a model-free check watches, and a view of shared machines.

Responsibility inventory. The daemon lists, model-free, what the project is responsible for:
- machines: the user's machines (machines.json) that the charter's Resources section names;
- device runners: `device.runners.<name>` in the config;
- a kept tunnel to the project's web app (`ttp web <name> --tunnel --keep`) installed on this machine;
- command and watcher schedules, the push queue, open PRs and remote detached jobs (a waiting task's
  `retry_when` is `ttp detach --check --host ...`).
A machine, runner or tunnel is covered when an enabled heal check or command watcher names it: in its
`resource` or `machine` (the alias itself, or `<alias>-...`), or as a word of its command, check, fix,
status command, unit, host or URL. The rest the daemon already watches by itself (a schedule failing
twice queues its fix task, the push queue recovers dead batches, the PR watcher reads open PRs, a wait
probe wakes its task when the remote job ends or its host is gone), so they are covered by `builtin`;
open PRs need the built-in PR watcher schedule to be on.

`ttp status` and the web app show 'health coverage: n/m (missing: ...)'. The daemon looks every
CHECK_EVERY_S (a file read and a few queries, no subprocess) and keeps the inventory in kv KV; it
rewrites it when the charter, config, schedules or open work change it, and once a day. The coordinator
hears of it only when the covered set changes: one digest line, no turn of its own (TOLD_KV). The daily
review gets the line too.

Machine view (`ttp machines status [alias]`, the web app's Projects tab): per machine, the projects
whose charter Resources name it (its co-tenants), its recovery owner, the pauses on it with their ends,
open ledger entries and conditions (machine_ledger), the heal checks naming it with their state, and
its problems in the last 24 h (lost runs, failed or blocked hand-offs, host reboots while held). Other
projects are only read: their charter file, and their database opened read-only (globalcap.connect_ro).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import heal, machines, project

KV = "responsibilities"            # kv: {"at", "items": [{kind, name, covered_by}], "line"}
TOLD_KV = "responsibilities_told"  # kv: the missing list the coordinator's digest last named
CHECK_EVERY_S = 300
REBUILD_S = 86400
OPEN_TASK = ("queued", "running", "waiting", "needs_review", "blocked", "pushing")
DONE_PR = ("MERGED", "CLOSED")
REMOTE_JOB_RE = re.compile(r"detach\s+--check\s+--host\s+(['\"]?)([^\s'\"]+)\1")
HEAL_TEXT = ("check", "fix", "status_command", "unit", "host", "url")
KINDS = ("machine", "runner", "tunnel", "schedule", "push queue", "pr", "remote job")
NEEDS_CHECK = ("machine", "runner", "tunnel")


def _word(needle: str, text: str) -> bool:
    return bool(needle) and re.search(rf"(?<![\w.-]){re.escape(needle)}(?![\w-]|\.\w)", text) is not None


def _names(needle: str, name: str) -> bool:
    """A `resource` or `machine` field names `needle`: it is the alias, or a resource on it (`<alias>-x`)."""
    return bool(needle) and (name == needle or name.startswith((needle + "-", needle + ":")))


# What watches -------------------------------------------------------------------------------------
def watchers(db) -> list[dict]:
    """Enabled command schedules (heal checks and command watchers): {name, heal, fields, text}."""
    out = []
    for r in db.q("SELECT name, payload FROM schedules WHERE enabled=1 AND kind='command' ORDER BY name"):
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            continue
        spec = heal.of(payload)
        fields = [str(spec.get(k) or "") for k in ("resource", "machine")] if spec else []
        text = " ".join([str(payload.get("command") or "")]
                        + [str(spec.get(k) or "") for k in HEAL_TEXT if spec])
        if spec or str(payload.get("command") or "").strip():
            out.append({"name": r["name"], "heal": spec is not None, "fields": [f for f in fields if f], "text": text})
    return out


def covering(needles: list[str], found: list[dict]) -> list[str]:
    """The checks that name one of `needles`."""
    return [w["name"] for w in found
            if any(_names(n, f) for n in needles for f in w["fields"]) or any(_word(n, w["text"]) for n in needles)]


# The inventory ------------------------------------------------------------------------------------
def inventory(p, cfg: dict | None = None, db=None) -> list[dict]:
    """[{kind, name, covered_by: [check names or 'builtin: ...']}], in KINDS order."""
    from . import machine_ledger, pushq, tunnel
    cfg = p.config() if cfg is None else cfg
    db = p.db if db is None else db
    found = watchers(db)
    items: list[dict] = []
    known = machines.load()
    for alias in machine_ledger.resource_machines(p):
        needles = [alias] + ([str(known[alias]["hostname"])] if (known.get(alias) or {}).get("hostname") else [])
        items.append({"kind": "machine", "name": alias, "covered_by": covering(needles, found)})
    runners = ((cfg.get("device") or {}).get("runners") or {})
    for name in sorted(runners if isinstance(runners, dict) else {}):
        host = str((runners[name] or {}).get("host") or "") if isinstance(runners[name], dict) else ""
        items.append({"kind": "runner", "name": name, "covered_by": covering([name] + ([host] if host else []), found)})
    try:
        kept = tunnel.installed(p.name)
    except Exception:
        kept = None
    if kept:
        items.append({"kind": "tunnel", "name": p.name, "covered_by": covering(
            [tunnel.label(p.name)] + [tunnel.service_file(p.name, plat).name for plat in ("linux", "darwin")], found)})
    prs_on = []
    for r in db.q("SELECT name, kind, payload FROM schedules WHERE enabled=1 AND kind IN ('command','watcher') "
                  "ORDER BY name"):
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = {}
        if r["kind"] == "watcher" and (payload.get("builtin") or r["name"]) == "prs":
            prs_on.append(r["name"])
        if r["kind"] == "command" and heal.of(payload):
            continue   # a heal check is coverage, not something to cover
        items.append({"kind": "schedule", "name": r["name"],
                      "covered_by": ["builtin: two failed runs queue its fix task"]})
    try:
        on = pushq.enabled(p, cfg)
    except Exception:
        on = False
    if on:
        items.append({"kind": "push queue", "name": "push queue",
                      "covered_by": ["builtin: the daemon recovers dead batches"]})
    seen = db.kv("pr_signatures", {}) or {}
    urls = sorted({t["pr_url"] for t in db.q("SELECT pr_url FROM tasks WHERE pr_url IS NOT NULL AND pr_url!='' "
                                             "AND status!='cancelled'")
                   if str((seen.get(t["pr_url"]) or {}).get("state") or "").upper() not in DONE_PR})
    for url in urls:
        items.append({"kind": "pr", "name": url, "covered_by": [f"builtin: PR watcher {prs_on[0]}"] if prs_on else []})
    for t in db.q("SELECT id, result FROM tasks WHERE status='waiting' ORDER BY id"):
        try:
            probe = str((json.loads(t["result"] or "{}") or {}).get("retry_when") or "")
        except (ValueError, AttributeError):
            continue
        m = REMOTE_JOB_RE.search(probe)
        if m:
            items.append({"kind": "remote job", "name": f"task #{t['id']} on {m.group(2)}",
                          "covered_by": ["builtin: its wait probe wakes the task when the job ends"]})
    return items


def missing(items: list[dict]) -> list[str]:
    return [f"{i['kind']} {i['name']}" for i in items if not i["covered_by"]]


def line_of(items: list[dict]) -> str:
    """'health coverage: n/m (missing: ...)'; "" with nothing to cover."""
    if not items:
        return ""
    gaps = missing(items)
    return (f"health coverage: {len(items) - len(gaps)}/{len(items)}"
            + (f" (missing: {', '.join(gaps[:8])}{', …' if len(gaps) > 8 else ''})" if gaps else ""))


def refresh(p, cfg: dict | None = None, now: float | None = None) -> dict:
    """Build the inventory; store it when it changed or is a day old. Returns the stored record."""
    now = time.time() if now is None else now
    items = inventory(p, cfg)
    old = p.db.kv(KV) or {}
    if old.get("items") != items or now - float(old.get("at") or 0) >= REBUILD_S:
        old = {"at": now, "items": items, "line": line_of(items)}
        p.db.set_kv(KV, old)
    return old


def line(db) -> str:
    """The stored coverage line, for ttp status and the web app (the daemon keeps it current)."""
    return str((db.kv(KV) or {}).get("line") or "")


def digest_lines(db) -> list[str]:
    """One line when coverage changed since the coordinator last heard; [] when it did not."""
    rec = db.kv(KV) or {}
    if not rec.get("items"):
        return []
    gaps = missing(rec["items"])
    told = db.kv(TOLD_KV)
    if told is not None and sorted(told) == sorted(gaps):
        return []
    db.set_kv(TOLD_KV, gaps)
    if told is None and not gaps:
        return []   # first look and all covered: nothing to say
    fixed = sorted(set(told or []) - set(gaps))
    return [f"## Health coverage changed: {rec['line']}"
            + (f"; now covered: {', '.join(fixed)}" if fixed else "")
            + ("; give each missing one a heal check (harness/schedules.json, `heal` block) naming it"
               if gaps else "")]


# Read-only view of other projects ------------------------------------------------------------------
class ReadOnly:
    """The few DB calls heal and machines read through, on another project's database, read-only."""

    def __init__(self, conn: sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        self.conn = conn

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def kv(self, key: str, default: Any = None) -> Any:
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not r or r[0] is None:
            return default
        try:
            return json.loads(r[0])
        except ValueError:
            return default

    def boots(self, since: float) -> list[dict]:
        rows = self.q("SELECT ts, data FROM events WHERE source='host' AND kind='boot' AND ts>? ORDER BY ts, id",
                      (since,))
        return [{**json.loads(r["data"] or "{}"), "ts": r["ts"]} for r in rows]

    def close(self) -> None:
        self.conn.close()


def _projects(here: str) -> list[tuple[str, Path]]:
    """(name, its tt-project folder) of each registered project on this machine."""
    out = []
    for name, e in sorted((project.load_registry().get("projects") or {}).items()):
        if isinstance(e, dict) and e.get("dir") and e.get("host") in (None, here):
            out.append((name, Path(str(e["dir"])) / project.FOLDER))
    return out


def _resource_aliases(charter: str, known: dict) -> list[str]:
    from . import ends
    from .prompts import charter_sections
    text = "\n".join("\n".join(body) for head, body in charter_sections(charter)
                     if ends.section_name(head).lower().startswith("resources"))
    return sorted(a for a in known if _word(a, text))


def machine_status(alias: str | None = None, now: float | None = None) -> list[dict]:
    """The machine view: one record per machine (or just `alias`), read-only from every project."""
    from . import globalcap, machine_ledger, pauseends, shared
    now = time.time() if now is None else now
    known = machines.load()
    aliases = [alias] if alias else sorted(known)
    view = {a: {"alias": a, "known": a in known, "projects": [], "owner": (known.get(a) or {}).get("recovery_owner"),
                "fallback": (known.get(a) or {}).get("recovery_fallback"), "pauses": [], "changes": [],
                "conditions": [], "checks": [], "problems": [], "unread": []} for a in aliases}
    for name, base in _projects(project.hostname()):
        try:
            mine = _resource_aliases((base / "harness" / "CHARTER.md").read_text(), known)
        except OSError:
            mine = []
        for a in mine:
            if a in view:
                view[a]["projects"].append(name)
        db_path = base / "state" / "project.db"
        if not db_path.is_file():
            continue
        try:
            db = ReadOnly(globalcap.connect_ro(db_path))
        except Exception:
            for a in mine:
                if a in view:
                    view[a]["unread"].append(name)
            continue
        try:
            _read_project(name, db, view, now)
        except Exception as e:
            for a in view:
                if a in mine:
                    view[a]["unread"].append(f"{name} ({type(e).__name__})")
        finally:
            db.close()
    for a, v in view.items():
        for res in sorted({res for res in _shared_resources(a)}):
            pz = shared.read_pause(res)
            if pz and not any(x["resource"] == res for x in v["pauses"]):
                v["pauses"].append({"resource": res, "project": pz.get("project") or "shared",
                                    "reason": pz.get("reason") or "", "by": pz.get("by") or "",
                                    "end": pauseends.describe(pz)})
        v["changes"] = [machine_ledger.change_line(c, now) for c in machine_ledger.changes({a})]
        v["conditions"] = [f"{c['condition']} since {machine_ledger.ends.stamp(c['first'])} (seen by "
                           f"{', '.join(c.get('seen_by', []))}): {c.get('text') or 'no detail'}"
                           for k, c in machine_ledger.conditions().items() if c.get("alias") == a]
    return list(view.values())


def _shared_resources(alias: str) -> list[str]:
    from . import shared
    try:
        dirs = [d.name for d in shared.root().iterdir() if d.is_dir()]
    except OSError:
        return []
    return [d for d in dirs if _names(alias, d)]


def _read_project(name: str, db: ReadOnly, view: dict, now: float) -> None:
    from . import pauseends
    from .db import PAUSED_RESOURCES_KEY
    found = watchers(db)
    for a, v in view.items():
        for w in found:
            if not w["heal"] or not covering([a], [w]):
                continue
            st = heal.state(db, w["name"])
            status = st.get("status") or "not checked yet"
            if "unknown_since" in st:
                status = f"unknown (exit {st.get('last_rc')})"
            elif status == "unhealthy" and st.get("since"):
                status += f" for {(now - float(st['since'])) / 60:.0f} min"
            v["checks"].append({"project": name, "check": w["name"], "status": status,
                                "fixed_today": heal.fixed_today(db, w["name"], now)})
    paused = db.kv(PAUSED_RESOURCES_KEY, {}) or {}
    for res, pz in (paused.items() if isinstance(paused, dict) else []):
        for a, v in view.items():
            if _names(a, res) and isinstance(pz, dict):
                v["pauses"].append({"resource": res, "project": name, "reason": pz.get("reason") or "",
                                    "by": pz.get("by") or "", "end": pauseends.describe(pz)})
    stats = machines.stats(db, now)
    for res, s in stats.items():
        for a, v in view.items():
            if _names(a, res):
                parts = [f"{s[k]} {label}" for k, label in (("runs", "lost, crashed or stalled runs"),
                                                            ("handoffs", "failed or blocked hand-offs"),
                                                            ("reboots", "host reboots while held")) if s.get(k)]
                if parts:
                    v["problems"].append(f"{name}: {', '.join(parts)} (24 h, {res})")


def machine_lines(rec: dict) -> list[str]:
    """`ttp machines status` text for one machine."""
    a = rec["alias"]
    out = [a + ("" if rec["known"] else " (not in `ttp machines list`)")]
    out.append("  projects: " + (", ".join(rec["projects"]) or "none name it in their Resources"))
    out.append("  recovery owner: " + (rec["owner"] or "none set")
               + (f", fallback {rec['fallback']}" if rec.get("fallback") else ""))
    for pz in rec["pauses"]:
        out.append(f"  paused: {pz['resource']} by {pz['project']}" + (f" ({pz['reason']})" if pz["reason"] else "")
                   + (f"; {pz['end']}" if pz["end"] else "; no end recorded"))
    for c in rec["conditions"]:
        out.append(f"  condition: {c}")
    for c in rec["changes"]:
        out.append(f"  change: {c}")
    for c in rec["checks"]:
        out.append(f"  heal check {c['project']}/{c['check']}: {c['status']}"
                   + (f", {c['fixed_today']} fixed today" if c["fixed_today"] else ""))
    if not rec["checks"]:
        out.append("  heal checks: none name it")
    for x in rec["problems"]:
        out.append(f"  problem: {x}")
    if rec["unread"]:
        out.append("  could not read: " + ", ".join(rec["unread"]))
    return out
