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
import sqlite3
import time
from pathlib import Path

from . import globalcap as gcap
from . import project
from .release import installed, is_newer, runtime_version

GATE_FRESH_S = 900   # a daemon's last global count older than this is not shown as current


def _settings(base: Path) -> dict:
    try:
        raw = json.loads((base / "harness" / "project.json").read_text())
    except (OSError, ValueError):
        raw = {}
    return project.layered(raw if isinstance(raw, dict) else {})


def _daemon(conn: sqlite3.Connection, state: Path, here: str) -> str:
    from .daemon import HEARTBEAT_STALE_S, _alive
    r = conn.execute("SELECT value FROM kv WHERE key='daemon'").fetchone()
    try:
        d = json.loads(r[0]) if r else {}
    except ValueError:
        d = {}
    pid = int(d.get("pid") or 0) if isinstance(d, dict) else 0
    if not pid or d.get("host") != here or not _alive(pid):
        return "stopped"
    try:
        hb = json.loads((state / "heartbeat").read_text())
        age = time.time() - (state / "heartbeat").stat().st_mtime
    except (OSError, ValueError):
        return "running"
    if isinstance(hb, dict) and int(hb.get("pid") or 0) == pid and age > HEARTBEAT_STALE_S:
        return f"stale (no tick for {int(age // 60)} min)"
    return "running"


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
        row["daemon"] = _daemon(conn, base / "state", here)
        row["asks"] = conn.execute("SELECT COUNT(*) FROM messages WHERE kind='ask' AND handled=0").fetchone()[0]
        row["waiting_on_user"] = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='blocked'").fetchone()[0]
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
    g = {"cap": float(account.get("global_daily_usd") or 0), "label": "per 24h" if rolling else "today",
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
    return (f"{r['name']}\tdaemon {r['daemon']} · v{v} · {r['asks']} open ask{'s' if r['asks'] != 1 else ''}"
            f" · {r['waiting_on_user']} waiting on you · {r['running']} running · {spend(r)}")


def footer(g: dict) -> str:
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
