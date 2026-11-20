# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Time to recover (TTR) per machine: model-free incident records and their report.

An incident starts when a machine this project watches goes down and ends when nothing says so any
more. A project watches the machines its charter's Resources name, those it is the recovery owner or
fallback of, and those its own heal checks name. A machine is down while any of these says so:
- one of this project's heal checks reports it to the machine ledger (`machine_reported`: down or
  held, past the check's own rules for unknown results, findings and known faults; heal.py), from
  the check's first unhealthy run (`since`);
- a co-tenant's condition on it in the shared routes file (machine_ledger.report) seen within
  machine_ledger.CONDITION_GONE_S, from its `first` report;
- a known outage recorded for it (`ttp machines outage`), from when it was recorded.
The earliest of these is the incident's start, and its kind (down, held or outage) and cause. Its end
is the first daemon tick (every OBSERVE_S) that finds none of them; a later tick that finds one again
starts a new incident, never before the last one ended. When the open incident's source recovers but
another of a different kind and cause (say a known outage after a heal check) keeps the machine down,
the incident hands over: it ends when the new source began (no earlier than it started) and a new one
opens from there with the new kind and cause, so each source's incident keeps its own start and
cause. A source changing only its kind (a heal check's down to held) or only its cause keeps it.

Records live in this project's database (ttr_incidents): rows are only added, and each row's end is
written once. A unique index allows one open incident per machine and every step is one transaction,
so a daemon restart or host reboot mid-incident neither loses nor duplicates one: the next tick finds
the open row and keeps it, or ends it once the machine is healthy. While the daemon is stopped nothing
is observed: a recovery in that time is recorded at its first tick after. While the project is paused
heal checks do not run, so their reports freeze: a heal report whose last check is older than the
check's period plus HEAL_STALE_MARGIN_S is ignored then, and an incident only it kept open ends at that
tick, at the latest its last unhealthy sample plus the check's period (below);
co-tenant conditions and known outages still count.

A heal check samples only every period, so a short fault would last until the next healthy sample: while a
heal check keeps an incident open, its row records the check's last unhealthy sample (`last_bad`, the
check's last run) and period (`period_s`), and the incident ends no later than that sample plus the
period (no earlier than it started), even when the recovering sample or the tick that sees it is later.
Another source keeping it open clears both, and rows from before they were recorded keep their end.

`ttp ttr [<project>] [<alias>] [--days N] [--json]` reports per machine the incidents in the window
(those still open, or that ended within it), the median, p90 and max TTR of the ended ones, the open
ones and the last one; the daily review carries one line per machine with an incident in REVIEW_DAYS.
An incident that starts inside the window of a test change (`ttp machines change add --test`) by the same
project on the same machine (from the entry's `since`, a backdated start, else its `added`, until it was
closed, or now while it is open) is a test: it is labelled at report time from the entry's flag as it is
then, so rows recorded before the entry or before `ttp machines change edit <id> --test` flagged it are too,
and `--no-test` gives them their own kind back. A test incident has kind 'test', stays out of the
counts and median, p90 and max, and is listed apart (count, then up to TEST_LISTED of them). Another
project's entry or another machine's never relabels one. Closed entries are kept
machine_ledger.CLOSED_KEPT_S, as long as the `ttp ttr` default window.
"""
from __future__ import annotations

import math
import statistics
import time

from . import timefmt
from .db import DB

OBSERVE_S = 60          # how often the daemon records
DAYS = 30               # `ttp ttr` window by default
REVIEW_DAYS = 7         # the daily review's window
CAUSE_CHARS = 200
HEAL_STALE_MARGIN_S = 300   # while paused, a heal report older than its check's period plus this is ignored
KINDS = ("down", "held", "outage")
TEST_LISTED = 3         # test incidents listed per machine line (all in --json)


def watched(p) -> set[str]:
    """The machine aliases `p` records incidents for (module doc)."""
    from . import heal, machine_ledger, machines
    known = machines.load()
    mine = set(machine_ledger.resource_machines(p))
    mine |= {a for a, m in known.items() if p.name in (m.get("recovery_owner"), m.get("recovery_fallback"))}
    for _, spec in heal.checks(p.db):
        alias = heal.machine_of(spec, known)
        if alias:
            mine.add(alias)
    return mine


def down_now(p, now: float, aliases: set[str] | None = None) -> dict[str, dict]:
    """{alias: {"since", "kind", "cause"}} of the watched machines that are down now, by the earliest source."""
    from . import heal, machine_ledger
    aliases = watched(p) if aliases is None else aliases
    seen: dict[str, list[dict]] = {}
    paused = bool(p.db.kv("paused", False))
    for row, _ in heal.checks(p.db):
        st = heal.state(p.db, row["name"])
        rep = st.get("machine_reported")
        # Heal checks do not run while the project is paused: a report no newer than the check's
        # period plus a margin is frozen, not evidence the machine is still down.
        if paused and now - float(st.get("last_check") or 0) > float(row["every_s"] or 0) + HEAL_STALE_MARGIN_S:
            continue
        if isinstance(rep, list) and len(rep) == 2 and rep[0] in aliases:
            seen.setdefault(rep[0], []).append({"since": float(st.get("since") or st.get("unknown_since") or now),
                                                "kind": rep[1], "cause": f"heal check {row['name']}",
                                                "last_bad": float(st["last_check"]) if st.get("last_check") else None,
                                                "period_s": float(row["every_s"]) if row["every_s"] else None})
    for c in machine_ledger.conditions().values():
        alias = c.get("alias")
        if alias in aliases and now - float(c.get("last") or 0) < machine_ledger.CONDITION_GONE_S:
            who = ", ".join(c.get("seen_by") or []) or "?"
            seen.setdefault(alias, []).append({"since": float(c.get("first") or now), "kind": c.get("condition"),
                                               "cause": f"reported by {who}: {c.get('text') or ''}".rstrip(": ")})
    for alias, o in machine_ledger.outages(now).items():
        if alias in aliases:
            seen.setdefault(alias, []).append({"since": float(o.get("since") or now), "kind": "outage",
                                               "cause": f"known outage: {o.get('why') or ''}"})
    out = {}
    for alias, recs in seen.items():
        first = min(recs, key=lambda r: r["since"])
        out[alias] = {**first, "since": min(first["since"], now),
                      "cause": " ".join(str(first["cause"]).split())[:CAUSE_CHARS]}
    return out


def _capped(r, end: float) -> float:
    """`end` of open row `r`, no later than its last unhealthy heal sample plus the check's period (module doc)."""
    keys = r.keys()
    bad = r["last_bad"] if "last_bad" in keys else None
    period = r["period_s"] if "period_s" in keys else None
    if bad is None or not period:
        return end
    return min(end, max(float(bad) + float(period), float(r["started"])))


def observe(p, now: float | None = None) -> tuple[list[str], list[str]]:
    """One model-free step: open an incident for each watched machine newly down, end each open one
    whose machine is healthy again. Returns (opened, ended) aliases."""
    now = time.time() if now is None else now
    db: DB = p.db
    aliases = watched(p)
    was_open = {r["alias"] for r in db.q("SELECT alias FROM ttr_incidents WHERE ended IS NULL")}
    down = down_now(p, now, aliases | was_open)
    opened, ended = [], []
    with db.tx():
        open_rows = {r["alias"]: r for r in db.q("SELECT * FROM ttr_incidents WHERE ended IS NULL")}
        for alias, r in list(open_rows.items()):
            d = down.get(alias)
            if d is None:
                end = now
            elif d["kind"] != r["kind"] and d["cause"] != r["cause"]:
                end = min(max(d["since"], float(r["started"])), now)   # handover to another source
            else:
                sample = (d.get("last_bad"), d.get("period_s"))
                if sample != (r["last_bad"], r["period_s"]):
                    db.x("UPDATE ttr_incidents SET last_bad=?, period_s=? WHERE id=? AND ended IS NULL",
                         (*sample, r["id"]))
                continue
            if db.conn.execute("UPDATE ttr_incidents SET ended=? WHERE id=? AND ended IS NULL",
                               (_capped(r, end), r["id"])).rowcount:
                ended.append(alias)
                if d is not None:
                    del open_rows[alias]
        for alias, d in sorted(down.items()):
            if alias in open_rows:
                continue
            last = db.one("SELECT MAX(ended) AS e FROM ttr_incidents WHERE alias=?", (alias,))
            start = max(d["since"], float(last["e"] or 0) if last else 0.0)
            if db.conn.execute("INSERT OR IGNORE INTO ttr_incidents(alias, kind, cause, started, last_bad, period_s) "
                               "VALUES(?,?,?,?,?,?)", (alias, d["kind"], d["cause"], start, d.get("last_bad"),
                                                       d.get("period_s"))).rowcount:
                opened.append(alias)
    return opened, ended


# report -------------------------------------------------------------------------------------------

def _p90(xs: list[float]) -> float:
    """Nearest-rank 90th percentile of a non-empty list."""
    s = sorted(xs)
    return s[max(0, math.ceil(0.9 * len(s)) - 1)]


def test_windows(project: str | None, now: float) -> dict[str, list[tuple[float, float, int]]]:
    """{alias: [(from, to, change id)]} of project `project`'s changes flagged as tests now (`ttp machines change
    add --test`, or `edit <id> --test` later): from each one's start (its backdated `since`, else when it was
    added) until it was closed, or `now` while it is open."""
    if not project:
        return {}
    from . import machine_ledger
    out: dict[str, list[tuple[float, float, int]]] = {}
    for c in machine_ledger.changes(open_only=False):
        if c.get("test") and c.get("by") == project and c.get("alias") and c.get("added") is not None:
            start = c["since"] if isinstance(c.get("since"), (int, float)) else c["added"]
            out.setdefault(c["alias"], []).append((float(start), float(c.get("closed") or now), c.get("id")))
    return out


def report(db: DB, alias: str | None = None, days: float = DAYS, now: float | None = None,
           project: str | None = None) -> dict[str, dict]:
    """{alias: stats} of the incidents open now or ended within `days` (module doc). TTRs in seconds.
    `project`: the project whose test changes label incidents as tests (none without it)."""
    now = time.time() if now is None else now
    since = now - days * 86400
    rows = db.q("SELECT * FROM ttr_incidents WHERE (ended IS NULL OR ended>=?)" + (" AND alias=?" if alias else "")
                + " ORDER BY started, id", (since, alias) if alias else (since,))
    windows = test_windows(project, now)
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r["alias"], []).append(r)
    stats = {}
    for a, all_rs in sorted(out.items()):
        change = {r["id"]: next((cid for lo, hi, cid in windows.get(a, ()) if lo <= float(r["started"]) <= hi), None)
                  for r in all_rs}
        rs = [r for r in all_rs if change[r["id"]] is None]
        ttrs = [float(r["ended"]) - float(r["started"]) for r in rs if r["ended"] is not None]
        stats[a] = {
            "incidents": len(rs),
            "recovered": len(ttrs),
            "median_s": statistics.median(ttrs) if ttrs else None,
            "p90_s": _p90(ttrs) if ttrs else None,
            "max_s": max(ttrs) if ttrs else None,
            "open": [{"id": r["id"], "kind": r["kind"], "cause": r["cause"], "started": r["started"],
                      "open_s": now - float(r["started"])} for r in rs if r["ended"] is None],
            "last": {k: rs[-1][k] for k in ("id", "kind", "cause", "started", "ended")} if rs else None,
            "tests": [{"id": r["id"], "kind": "test", "source_kind": r["kind"], "cause": r["cause"],
                       "started": r["started"], "ended": r["ended"], "change": change[r["id"]],
                       "ttr_s": None if r["ended"] is None else float(r["ended"]) - float(r["started"])}
                      for r in all_rs if change[r["id"]] is not None],
        }
    return stats


def _dur(s: float) -> str:
    if s >= 86400:
        return f"{s / 86400:.1f} d"
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min"


def line(alias: str, s: dict, where, days: float) -> str:
    """One machine's stats as one line, times in the zone of `where` (a Project, settings or zone name)."""
    text = f"{alias}: {s['incidents']} incident{'s' if s['incidents'] != 1 else ''} in {days:g} d"
    if s["recovered"]:
        text += f"; TTR median {_dur(s['median_s'])}, p90 {_dur(s['p90_s'])}, max {_dur(s['max_s'])}"
    if s["open"]:
        text += "; open: " + ", ".join(f"{o['kind']} since {timefmt.long(o['started'], where)} "
                                       f"({_dur(o['open_s'])})" for o in s["open"])
    last = s["last"]
    if last and last["ended"] is not None:
        text += (f"; last: {last['kind']} {timefmt.long(last['started'], where)}, recovered in "
                 f"{_dur(float(last['ended']) - float(last['started']))}")
    text += f" ({last['cause']})" if last and last.get("cause") else ""
    tests = s.get("tests") or []
    if tests:
        shown = [f"{t['source_kind']} {timefmt.long(t['started'], where)}, "
                 + (f"{_dur(t['ttr_s'])}" if t["ttr_s"] is not None else "open")
                 + f", change #{t['change']}" for t in tests[-TEST_LISTED:]]
        more = len(tests) - len(shown)
        text += (f"; tests (not in TTR): {len(tests)}: " + "; ".join(shown)
                 + (f"; {more} earlier" if more else ""))
    return text


def lines(db: DB, where, alias: str | None = None, days: float = DAYS, now: float | None = None,
          project: str | None = None) -> list[str]:
    return [line(a, s, where, days) for a, s in report(db, alias, days, now, project).items()]


def review_lines(p, now: float | None = None) -> list[str]:
    """For the daily review: one line per machine with an incident in the last REVIEW_DAYS, test ones apart."""
    return lines(p.db, timefmt.home(p), days=REVIEW_DAYS, now=now, project=p.name)
