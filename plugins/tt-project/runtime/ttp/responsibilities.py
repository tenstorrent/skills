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
schedule name, `resource` or `machine` (the alias itself, or `<alias>-...`), or as a word of its command,
check, fix, status command, unit, host or URL. A command watcher without a `heal` block (report-only: it
reports, and two failed runs queue its fix task, but nothing heals) counts too, and the line names what
only such checks cover ('report-only: machine <alias>'). A shared machine this project has no such check for is covered when
its recovery owner (else its fallback) runs an enabled heal check naming it, and the line says
'machine <alias> covered by owner <project>'. Failing that (no owner set, or the owner has no such
check), it is covered when another project on this machine (a co-tenant) has an enabled heal check
naming it that ran healthy within FRESH_INTERVALS of its interval: 'machine <alias> covered by <project>
heal check'. A stale or unhealthy co-tenant check does not cover it. Other projects' databases are only
read, read-only. Failing those, a recovery owner whose database cannot be read here (it runs on another
host) is trusted as declared: 'machine <alias> covered by owner <project> (declared)'; an owner readable
here with no such check stays a gap. Failing all of these, a machine with a known outage recorded
(`ttp machines outage`) counts as covered until that outage ends: 'machine <alias> covered by a known
outage', and one config health.exclude_machines lists ([{alias, reason}]) counts as opted out:
'machine <alias> excluded: <reason>'. A machine is missing only when no project checks it, no owner is
declared and no outage or exclusion covers it. The rest the daemon
already watches by itself (a schedule failing twice queues its fix task, the push queue recovers dead
batches, the PR watcher reads open PRs, a wait probe wakes its task when the remote job ends or its host
is gone), so they are covered by `builtin`; open PRs need the built-in PR watcher schedule to be on.

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

from . import heal, machines, prguard, project

KV = "responsibilities"            # kv: {"at", "items": [{kind, name, covered_by}], "line"}
TOLD_KV = "responsibilities_told"  # kv: the missing list the coordinator's digest last named
CHECK_EVERY_S = 300
FRESH_INTERVALS = 2                # a co-tenant's heal check counts while its last healthy run is this many intervals old
REBUILD_S = 86400
OPEN_TASK = ("queued", "running", "waiting", "needs_review", "blocked", "pushing")
DONE_PR = ("MERGED", "CLOSED")
REMOTE_JOB_RE = re.compile(r"detach\s+--check\s+--host\s+(['\"]?)([^\s'\"]+)\1")
HEAL_TEXT = ("check", "fix", "status_command", "unit", "host", "url")
KINDS = ("machine", "runner", "tunnel", "schedule", "push queue", "pr", "remote job")
NEEDS_CHECK = ("machine", "runner", "tunnel")
OUTAGE_VIA = "a known outage"      # the `via` of a machine only its recorded outage covers


def _word(needle: str, text: str) -> bool:
    return bool(needle) and re.search(rf"(?<![\w.-]){re.escape(needle)}(?![\w-]|\.\w)", text) is not None


PATH_EXT_RE = re.compile(r"\w\.[A-Za-z][A-Za-z0-9]{0,4}$")   # a file name's extension: heal.py, run.sh


def _named(needle: str, text: str) -> bool:
    """A check's text names `needle`: as a word, or as a `_`-delimited part of a path-like token
    (`bin/<alias>_heal.py`, `<alias>_up.sh`). Elsewhere `_` stays part of a word (`MY_<alias>` names nothing)."""
    if _word(needle, text):
        return True
    if not needle or "_" in needle:
        return False
    for tok in re.split(r"[\s'\"=;|&()<>,]+", text):
        if "_" in tok and ("/" in tok or PATH_EXT_RE.search(tok)):
            if _word(needle, re.sub(r"\.[A-Za-z][A-Za-z0-9]{0,4}$", "", tok).replace("_", " ")):
                return True
    return False


def _names(needle: str, name: str) -> bool:
    """A `resource` or `machine` field names `needle`: it is the alias, or a resource on it (`<alias>-x`)."""
    return bool(needle) and (name == needle or name.startswith((needle + "-", needle + ":")))


# What watches -------------------------------------------------------------------------------------
def watchers(db) -> list[dict]:
    """Enabled command schedules (heal checks and command watchers): {name, heal, preset, fields, text}."""
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
            out.append({"name": r["name"], "heal": spec is not None, "preset": str((spec or {}).get("preset") or ""),
                        "fields": [f for f in fields if f], "text": text})
    return out


def covering(needles: list[str], found: list[dict]) -> list[str]:
    """The checks that name one of `needles`."""
    return [w["name"] for w in found
            if any(_names(n, f) for n in needles for f in [w["name"], *w["fields"]])
            or any(_named(n, w["text"]) for n in needles)]


def _mark_report_only(item: dict, found: list[dict]) -> dict:
    """`item` with report_only set when only command watchers without a heal block cover it."""
    heals = {w["name"] for w in found if w["heal"]}
    if item["covered_by"] and not heals & set(item["covered_by"]):
        item["report_only"] = True
    return item


PR_WORD_RE = re.compile(r"\b(?:pulls?|prs?)\b", re.I)


def pr_item(url: str, prs_on: list[str], found: list[dict]) -> dict:
    """The inventory item of the open PR `url`. The built-in PR watcher covers it; else a command watcher
    whose text names the PR (its URL or owner/repo#N), or its repo (owner/repo) beside a PR word (`gh pr
    list -R owner/repo`, `.../repos/owner/repo/pulls`), covers it, report-only: a project that may keep no
    gh login on its host watches its PRs with its own command watcher, and those PRs are not missing."""
    if prs_on:
        return {"kind": "pr", "name": url, "covered_by": [f"builtin: PR watcher {prs_on[0]}"]}
    key = prguard.pr_key(url) or ""
    repo = key.split("#", 1)[0]
    names = [w["name"] for w in found if not w["heal"] and key
             and (key in prguard.pr_keys(w["text"])
                  or (re.search(r"(?<![\w.-])" + re.escape(repo) + r"(?![\w.-]|/(?!pulls?\b))", w["text"], re.I)
                      and PR_WORD_RE.search(w["text"])))]
    return _mark_report_only({"kind": "pr", "name": url, "covered_by": names}, found)


# The inventory ------------------------------------------------------------------------------------
def inventory(p, cfg: dict | None = None, db=None, now: float | None = None) -> list[dict]:
    """[{kind, name, covered_by: [check names or 'builtin: ...']}], in KINDS order."""
    from . import machine_ledger, pushq, tunnel
    now = time.time() if now is None else now
    cfg = p.config() if cfg is None else cfg
    db = p.db if db is None else db
    found = watchers(db)
    items: list[dict] = []
    known = machines.load()
    try:
        down = machine_ledger.outages(now)
    except Exception:
        down = {}
    skip = excluded_machines(cfg)
    for alias in machine_ledger.resource_machines(p):
        needles = [alias] + ([str(known[alias]["hostname"])] if (known.get(alias) or {}).get("hostname") else [])
        item = _mark_report_only({"kind": "machine", "name": alias, "covered_by": covering(needles, found)}, found)
        if not item["covered_by"]:
            via, checks = _owner_checks(p, known.get(alias) or {}, needles)
            if not checks:
                via, checks = _cotenant_checks(p, needles, now)
            if not checks:
                via, checks = _declared_owner(p, known.get(alias) or {})
            if checks:
                item.update(covered_by=checks, via=via)
            elif alias in down:
                item.update(covered_by=["known outage"], via=OUTAGE_VIA)
            elif alias in skip:
                item.update(covered_by=[f"excluded: {skip[alias]}"], excluded=skip[alias])
        items.append(item)
    runners = ((cfg.get("device") or {}).get("runners") or {})
    for name in sorted(runners if isinstance(runners, dict) else {}):
        host = str((runners[name] or {}).get("host") or "") if isinstance(runners[name], dict) else ""
        items.append(_mark_report_only({"kind": "runner", "name": name,
                                        "covered_by": covering([name] + ([host] if host else []), found)}, found))
    try:
        kept = tunnel.installed(p.name)
    except Exception:
        kept = None
    if kept:
        items.append(_mark_report_only({"kind": "tunnel", "name": p.name, "covered_by": covering(
            [tunnel.label(p.name)] + [tunnel.service_file(p.name, plat).name for plat in ("linux", "darwin")], found)},
            found))
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
                   if prguard.is_pr(t["pr_url"])
                   and str((seen.get(t["pr_url"]) or {}).get("state") or "").upper() not in DONE_PR})
    for url in urls:
        items.append(pr_item(url, prs_on, found))
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


def _heal_checks(base: Path, needles: list[str], fresh_at: float | None = None,
                 preset: str | None = None) -> list[str]:
    """The enabled heal checks naming one of `needles` in the project at `base`, read read-only from its
    database; with `fresh_at`, only those whose last check was healthy within FRESH_INTERVALS of their
    interval; with `preset`, only checks of that preset. [] when it cannot be read."""
    from . import globalcap
    db_path = base / "state" / "project.db"
    if not db_path.is_file():
        return []
    try:
        db = ReadOnly(globalcap.connect_ro(db_path))
    except Exception:
        return []
    try:
        names = covering(needles, [w for w in watchers(db) if w["heal"] and preset in (None, w["preset"])])
        if fresh_at is not None:
            every = {r["name"]: float(r["every_s"] or 0) for r in db.q("SELECT name, every_s FROM schedules")}
            names = [n for n in names if _fresh(heal.state(db, n), every.get(n, 0.0), fresh_at)]
        return names
    except Exception:
        return []
    finally:
        db.close()


def _fresh(st: dict, every_s: float, now: float) -> bool:
    """A heal check's last run was healthy and is no older than FRESH_INTERVALS of its interval."""
    try:
        last = float(st.get("last_check") or 0)
    except (TypeError, ValueError):
        return False
    return (st.get("status") == "healthy" and "unknown_since" not in st and every_s > 0 and last > 0
            and now - last <= FRESH_INTERVALS * every_s)


def _owner_checks(p, rec: dict, needles: list[str]) -> tuple[str, list[str]]:
    """('owner <project>', ['<project>/<check>', ...]): the recovery owner's (else fallback's) enabled heal
    checks naming the machine, read read-only from its database; ("", []) when neither runs one here."""
    bases = dict(_projects(project.hostname()))
    for role in ("owner", "fallback"):
        who = str(rec.get(f"recovery_{role}") or "")
        if not who or who == p.name or who not in bases:
            continue
        names = _heal_checks(bases[who], needles)
        if names:
            return f"{role} {who}", [f"{who}/{n}" for n in names]
    return "", []


def _declared_owner(p, rec: dict) -> tuple[str, list[str]]:
    """('owner <project> (declared)', ['declared: recovery owner <project>']) when the machine's recovery owner
    is another project whose database cannot be read here (it runs on another host, or is not registered on
    this one), so its heal checks cannot be looked at; ("", []) otherwise. An owner readable here that runs
    no heal check naming the machine stays a gap."""
    who = str(rec.get("recovery_owner") or "")
    if not who or who == p.name:
        return "", []
    base = dict(_projects(project.hostname())).get(who)
    if base is not None and _readable(base):
        return "", []
    return f"owner {who} (declared)", [f"declared: recovery owner {who}"]


def _readable(base: Path) -> bool:
    """The project database at `base` opens read-only here."""
    from . import globalcap
    db_path = base / "state" / "project.db"
    if not db_path.is_file():
        return False
    try:
        globalcap.connect_ro(db_path).close()
    except Exception:
        return False
    return True


def excluded_machines(cfg: dict) -> dict[str, str]:
    """{alias: reason} from config health.exclude_machines ([{alias, reason}]): machines this project opts out
    of coverage for. An entry with no alias is ignored; one with no reason reads 'no reason given'."""
    rows = (cfg.get("health") or {}).get("exclude_machines") if isinstance(cfg.get("health"), dict) else None
    out: dict[str, str] = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and str(r.get("alias") or "").strip():
            out[str(r["alias"]).strip()] = str(r.get("reason") or "").strip() or "no reason given"
    return out


# Which healthy heal checks rule out each ledger condition: any check that reached the machine rules out
# `down` (the only condition a route-failed or unknown outage check reports); only a broker check, which
# reads the hold field itself, rules out `held`.
VOUCH_PRESET: dict[str, str | None] = {"down": None, "held": "broker"}


def healthy_checks(who: str, alias: str, now: float | None = None, condition: str = "down") -> list[str]:
    """Project `who`'s enabled heal checks naming machine `alias` whose last run was healthy and fresh, and
    that observe `condition` (VOUCH_PRESET; machine_ledger: such a check holds a page about the machine).
    Read-only; [] when `who` is not on this machine or cannot be read, or no check can observe it."""
    if condition not in VOUCH_PRESET:
        return []
    now = time.time() if now is None else now
    base = dict(_projects(project.hostname())).get(who)
    if base is None:
        return []
    rec = machines.load().get(alias) or {}
    needles = [alias] + ([str(rec["hostname"])] if rec.get("hostname") else [])
    return _heal_checks(base, needles, fresh_at=now, preset=VOUCH_PRESET[condition])


def _cotenant_checks(p, needles: list[str], now: float) -> tuple[str, list[str]]:
    """('<project> heal check', ['<project>/<check>', ...]): the first other project on this machine with an
    enabled heal check naming the machine that ran healthy within its interval; ("", []) when none has."""
    for who, base in _projects(project.hostname()):
        if who == p.name:
            continue
        names = _heal_checks(base, needles, fresh_at=now)
        if names:
            return f"{who} heal check", [f"{who}/{n}" for n in names]
    return "", []


def check_cover(items: list[dict]) -> dict[str, list[str]]:
    """{this project's check: ['machine <alias>', ...]}: the inventory items each check covers."""
    out: dict[str, list[str]] = {}
    for i in items:
        if not i.get("via") and "excluded" not in i:
            for c in i["covered_by"]:
                if not c.startswith("builtin:"):
                    out.setdefault(c, []).append(f"{i['kind']} {i['name']}")
    return out


def uncovered_hint(items: list[dict]) -> str:
    """'' or why a check covers nothing while machines, runners or tunnels lack one, and how to name them."""
    gaps = [f"{i['kind']} {i['name']}" for i in items if i["kind"] in NEEDS_CHECK and not i["covered_by"]]
    if not gaps:
        return ""
    return (f"uncovered: {', '.join(gaps[:8])}{', …' if len(gaps) > 8 else ''}; name one's alias or host name as a "
            f"word or file-name part in the check (e.g. `ssh <alias> ...`, `bin/<alias>_heal.py`), or set heal "
            f"`resource` or `machine` to it")


def by_owner(items: list[dict]) -> list[str]:
    """'machine <alias> covered by owner <project>' (or 'by <project> heal check', a co-tenant's) for each item
    only another project's heal check covers."""
    return [f"{i['kind']} {i['name']} covered by {i['via']}" for i in items if i.get("via") and i["covered_by"]]


def missing(items: list[dict]) -> list[str]:
    return [f"{i['kind']} {i['name']}" for i in items if not i["covered_by"]]


def report_only(items: list[dict]) -> list[str]:
    """The items only report-only command watchers (no heal block) cover: counted, and named apart."""
    return [f"{i['kind']} {i['name']}" for i in items if i.get("report_only") and i["covered_by"]]


def line_of(items: list[dict]) -> str:
    """'health coverage: n/m (missing: ...)'; "" with nothing to cover."""
    if not items:
        return ""
    gaps = missing(items)
    plain = report_only(items)
    notes = (([f"missing: {', '.join(gaps[:8])}{', …' if len(gaps) > 8 else ''}"] if gaps else []) + by_owner(items)
             + ([f"report-only: {', '.join(plain[:8])}{', …' if len(plain) > 8 else ''}"] if plain else [])
             + [f"{i['kind']} {i['name']} excluded: {i['excluded']}" for i in items if "excluded" in i])
    return f"health coverage: {len(items) - len(gaps)}/{len(items)}" + (f" ({'; '.join(notes)})" if notes else "")


def refresh(p, cfg: dict | None = None, now: float | None = None) -> dict:
    """Build the inventory; store it when it changed or is a day old. Returns the stored record."""
    now = time.time() if now is None else now
    items = inventory(p, cfg, now=now)
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


def review_advice(db) -> str:
    """The daily review's advice after the coverage line: a heal check for each missing item only; a
    machine another project's heal check covers needs no duplicate of its own."""
    items = (db.kv(KV) or {}).get("items") or []
    out = []
    if missing(items):
        out.append("Give each missing one a heal check naming it (`ttp machines status` shows the shared "
                   "machines).")
    if any(i.get("via") and i["via"] != OUTAGE_VIA and i["covered_by"] for i in items):
        out.append("A machine covered by another project's heal check needs no duplicate check here.")
    if any(i.get("via") == OUTAGE_VIA for i in items):
        out.append("A machine a known outage covers needs a heal check before that outage ends.")
    return "".join(" " + x for x in out)


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


def _project_aliases(harness: Path, known: dict) -> list[str]:
    """The machines of the project whose harness folder is `harness` (machine_ledger.project_machines),
    read without touching its state."""
    from . import machine_ledger
    try:
        cfg = json.loads((harness / "project.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    return machine_ledger.project_machines(cfg if isinstance(cfg, dict) else {}, known)


def machine_status(alias: str | None = None, now: float | None = None) -> list[dict]:
    """The machine view: one record per machine (or just `alias`), read-only from every project."""
    from . import globalcap, machine_ledger, pauseends, shared
    now = time.time() if now is None else now
    known = machines.load()
    aliases = [alias] if alias else sorted(known)
    view = {a: {"alias": a, "known": a in known, "projects": [], "owner": (known.get(a) or {}).get("recovery_owner"),
                "fallback": (known.get(a) or {}).get("recovery_fallback"), "pauses": [], "changes": [],
                "conditions": [], "outage": None, "checks": [], "problems": [], "unread": []} for a in aliases}
    for name, base in _projects(project.hostname()):
        try:
            mine = _project_aliases(base / "harness", known)
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
    outs = machine_ledger.outages(now)
    for a, v in view.items():
        for res in sorted({res for res in _shared_resources(a)}):
            pz = shared.read_pause(res)
            if pz and not any(x["resource"] == res for x in v["pauses"]):
                v["pauses"].append({"resource": res, "project": pz.get("project") or "shared",
                                    "reason": pz.get("reason") or "", "by": pz.get("by") or "",
                                    "end": pauseends.describe(pz)})
        v["changes"] = [machine_ledger.change_line(c, now) for c in machine_ledger.changes({a})]
        v["conditions"] = [machine_ledger.condition_line(c, now)
                           for k, c in machine_ledger.conditions().items() if c.get("alias") == a]
        v["outage"] = outs.get(a)
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
                                    "by": pz.get("by") or "",
                                    "end": pauseends.describe(pz, None, pauseends.passed_at(db, res, pz), now)})
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
    if rec.get("outage"):
        from .machine_ledger import outage_line
        out.append("  " + outage_line(a, rec["outage"]))
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
