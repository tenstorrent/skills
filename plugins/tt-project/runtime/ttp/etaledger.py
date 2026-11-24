"""The ETA ledger: every expected finish the project gave, what was delivered, and how far apart they were
(`ttp eta --calibration`, the daily review's weekly line). No model call.

state/eta-ledger.jsonl is append-only: an ETA row per task or promise when it is first given and again when
it moves by 15 min or more after an hour (`ts`, `ref` `t12` or `o3`, `kind`, `src`, `likely` the raw
estimate, `shown` the corrected one, `conservative`, `due`), and a settling row when the work is
delivered (`delivered`) or closed without delivery (`closed`). Obligation promises older than the ledger are
backfilled once from their due and receipt.

The correction: per task kind, the trailing 7-day median of actual / promised time, bounded (CLAMP)
and used only from MIN_SAMPLES deliveries on, multiplies the time left in eta.py's estimates. It is
kept in the project's kv (`eta_factors`) so every estimate reads the same one. Config `eta`: `correction`
(false: off), `min_samples`, `clamp` [low, high], `conservative_x` (the conservative time's multiple of
the time left when too few deliveries give a measured one), `ledger` (false: record nothing),
`row_alarm_h` (missing_rows' window, default 2; 0: off).
Which tasks get a row: eta.state_lines' rule, run at each coordinator digest and by `ttp eta`: each open
urgent task (urgent.is_urgent) whose estimate has an end (one with `why`, such as 'waits on outside
work', gets none), plus each open promise. A non-urgent task gets no task row; its promise's row
carries it. `missing_rows` is the model-free alarm for a task that rule covers yet has no row.
Shadow (rings.eta_history, key eta.history, off by default): with the flag on, a task's ETA row also carries
`hist`, the end the measured history gives on its own (hist_end: the median first-start wait, the
median run time of the task's kind and tier (else of its kind) times its expected runs, and for a code
task the median review and landing). Nothing users see changes. `shadow` and
`shadow_line` give the baseline: ETAs compared, and each method's median and p90 absolute error against
the actual finish.
Target (docs): 80% delivered by the conservative time, median actual / promised between 0.7 and 1.3.
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

NAME = "eta-ledger.jsonl"
KEY = "eta_factors"
CLAMP = (0.7, 2.0)
MIN_SAMPLES = 10
CONSERVATIVE_X = 2.0
WEEK_S = 7 * 86400
MOVE_S = 900
REPEAT_S = 3600
REFRESH_S = 600
ROW_ALARM_H = 2.0
TARGET_SHARE = 0.8
TARGET_RATIO = (0.7, 1.3)


def cfg(where) -> dict:
    try:
        got = (where.config() if hasattr(where, "config") else where or {}).get("eta")
    except Exception:
        got = None
    return got if isinstance(got, dict) else {}


def _path(db) -> Path:
    return Path(db.path).parent / NAME


def read(db) -> list[dict]:
    out = []
    try:
        for ln in _path(db).read_text(errors="replace").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("ref"):
                out.append(r)
    except OSError:
        pass
    return out


def _append(db, rows: list[dict]) -> None:
    if not rows:
        return
    try:
        with open(_path(db), "a") as f:
            f.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    except OSError:
        pass


def conservative_x(db, c: dict | None = None) -> float:
    """The conservative time's multiple of the time left: measured from the project's deliveries when the
    ledger has enough (the 80th percentile of actual / shown), else config `eta.conservative_x`."""
    c = c or {}
    try:
        base = max(1.0, float(c.get("conservative_x", CONSERVATIVE_X)))
    except (TypeError, ValueError):
        base = CONSERVATIVE_X
    got = db.kv(KEY) or {}
    x = got.get("conservative_x") if isinstance(got, dict) else None
    return float(x) if isinstance(x, (int, float)) and x >= 1.0 else base


def measured_x(db) -> bool:
    """Whether the ledger holds a measured conservative multiple (not just the configured default)."""
    got = db.kv(KEY) or {}
    x = got.get("conservative_x") if isinstance(got, dict) else None
    return isinstance(x, (int, float)) and x >= 1.0


def factor(db, kind: str) -> float:
    """The correction of task kind `kind` (1.0: none, off or too few deliveries)."""
    got = db.kv(KEY) or {}
    if not isinstance(got, dict) or not got.get("on"):
        return 1.0
    f = (got.get("factors") or {}).get(kind or "")
    return float(f) if isinstance(f, (int, float)) and f > 0 else 1.0


def shown(db, e: dict, now: float, kind: str = "") -> dict:
    """`e` (an eta.py estimate) with its time left scaled by the kind's factor: `end`, plus `raw_end`,
    `factor`, `kind`, `conservative`, and `p90_end` scaled alike (`raw_p90_end`: before). Neither of the
    conservative times is ever before `end`. An estimate with no time left or none is returned as it is."""
    if e.get("why") or not e.get("end"):
        return e
    f = factor(db, kind)
    raw = e["end"]
    end = now + f * (raw - now) if raw > now else raw
    p90 = e.get("p90_end")
    if end <= now:
        cons = end
    elif isinstance(p90, (int, float)) and p90 > raw:
        # The history's own spread (every leg at its p90, scaled by the same correction); once the ledger
        # has measured its 80th percentile of actual / shown, the larger of the two.
        cons = now + f * (p90 - now)
        if measured_x(db):
            cons = max(cons, now + conservative_x(db) * (end - now))
    else:
        cons = now + conservative_x(db) * (end - now)
    out = {**e, "end": end, "raw_end": raw, "factor": f, "kind": kind, "conservative": max(cons, end)}
    if isinstance(p90, (int, float)):
        out.update(p90_end=max(now + f * (p90 - now) if p90 > now else p90, end), raw_p90_end=p90)
    return out


def kind_of(db, e: dict) -> str:
    """The kind of the task whose legs an estimate carries (the first of its critical path)."""
    try:
        t = db.task(int(e["path"][0])) if e.get("path") else None
    except (TypeError, ValueError, KeyError):
        t = None
    return (t or {}).get("kind") or ""


def _hist_on(where) -> bool:
    """The eta_history shadow flag, only for a project (a bare config dict has no ring)."""
    if not hasattr(where, "raw_config"):
        return False
    from . import costlevers
    return costlevers.lever_on(where, "eta_history")


def hist_end(db, ref: str, now: float, h: dict | None = None) -> float | None:
    """The finish the measured history gives a task ref (`t12`) at `now`: the median first-start wait, plus
    the median run time of its kind and tier (else of its kind) times the mean runs a done task of that
    kind and tier took (retries), plus for a code task the median review and the median landing (the push
    queue's approval-to-push time, else a push batch's); None without samples. A promise ref (`o3`): the
    latest of its live owner tasks' ends (obligations.live_owner_ids, so owners kept only in the promise
    count and a cancelled or superseded one does not), a finished owner counting `now`; None with no owner
    that gives one.
    `h` is eta.history's result, read once per caller."""
    if ref.startswith("o"):
        from . import eta, obligations
        o = obligations.get(db, ref)
        owners = obligations.live_owner_ids(db, o) if o else []
        h = eta.history(db, now) if h is None and owners else h
        ends = []
        for tid in owners:
            t = db.task(tid)
            ends.append(now if t and t["status"] in obligations.CLOSED else hist_end(db, f"t{tid}", now, h))
        ends = [e for e in ends if e is not None]
        return max(ends) if ends else None
    if not (ref.startswith("t") and ref[1:].isdigit()):
        return None
    t = db.task(int(ref[1:]))
    if not t:
        return None
    from . import eta
    h = eta.history(db, now) if h is None else h
    kt = (t["kind"], t.get("tier"))
    runs = h["run"].get(kt) or h["kind"].get(t["kind"])
    if not runs:
        return None
    tries = (h.get("tries") or {}).get(kt) or (h.get("tries_kind") or {}).get(t["kind"]) or [1]
    end = now + (statistics.median(h["wait"]) if h["wait"] else 0.0) + \
        statistics.median(runs) * max(1.0, statistics.mean(tries))
    if t["kind"] == "code":
        land = h.get("land") or h.get("batch")
        end += (statistics.median(h["review"]) if h.get("review") else 0.0) + (statistics.median(land) if land else 0.0)
    return end


def rowable(e: dict, now: float) -> bool:
    """Whether an estimate gets a ledger row: it has an end still ahead and no reason ('why') for none."""
    return not e.get("why") and bool(e.get("end")) and e["end"] > now


def record(db, where, items: list[tuple[str, str, dict]], now: float, src: str = "state",
           due: dict | None = None) -> int:
    """Append the ETAs given: items are (ref, kind, shown estimate); a ref already recorded in the last
    hour, or moved by less than 15 min, is skipped. `due`: {ref: due epoch} of promises. Returns the rows added."""
    if cfg(where).get("ledger") is False:
        return 0
    last: dict[str, dict] = {}
    for r in read(db):
        if "shown" in r:
            last[r["ref"]] = r
    rows = []
    hist = None
    shadow_on = _hist_on(where)
    for ref, kind, e in items:
        if not rowable(e, now):
            continue
        prev = last.get(ref)
        if prev and (now - prev["ts"] < REPEAT_S or abs(prev["shown"] - e["end"]) < MOVE_S):
            if src != "cli":
                continue
        row = {"ts": round(now, 1), "ref": ref, "kind": kind, "src": src, "likely": e.get("raw_end", e["end"]),
               "shown": e["end"], "conservative": max(e.get("conservative") or e["end"], e["end"])}
        if due and due.get(ref):
            row["due"] = due[ref]
        if shadow_on:
            try:
                if hist is None:
                    from . import eta
                    hist = eta.history(db, now)
                if (he := hist_end(db, ref, now, hist)) is not None:
                    row["hist"] = round(he, 1)
            except Exception:   # a shadow never costs the ETA row
                pass
        rows.append(row)
        last[ref] = row
    _append(db, rows)
    return len(rows)


def _rowless(db) -> list[dict]:
    """The tasks the row rule covers (eta.row_tasks) with no task ETA row since they were created."""
    from . import eta
    hot = eta.row_tasks(db)
    if not hot:
        return []
    have: dict[str, float] = {}
    for r in read(db):
        if "shown" in r:
            have[r["ref"]] = max(have.get(r["ref"], 0.0), float(r["ts"]))
    return [t for t in hot if have.get(f"t{t['id']}", 0.0) < float(t["created"] or 0)]


def write_rows(db, where, now: float) -> int:
    """Model-free task ETA rows between coordinator wakes: each task the row rule covers that has no row yet gets
    the one STATE would record (eta.row_item, through record()), so an urgent task estimated and finished between
    two wakes still has its row. A task with no finite estimate still gets none. Returns the rows added."""
    if cfg(where).get("ledger") is False:
        return 0
    todo = _rowless(db)
    if not todo:
        return 0
    from . import eta
    refresh(db, where, now)
    est = eta._Est(db, now, core=eta.core_of(where), delivery=eta.delivery_of(where))
    return record(db, where, [eta.row_item(db, est, t, now) for t in todo], now, src="daemon")


def missing_rows(db, where, now: float) -> list[dict]:
    """The open tasks the row rule covers that have no ETA row: urgent (the first ten, as state_lines takes
    them), first dispatched within `row_alarm_h` hours, seen by a coordinator wake since, with an estimate
    that has an end, and no row since they were created. Read-only; [] with the ledger or the alarm off."""
    c = cfg(where)
    try:
        win = float(c.get("row_alarm_h", ROW_ALARM_H)) * 3600
    except (TypeError, ValueError):
        win = ROW_ALARM_H * 3600
    if c.get("ledger") is False or win <= 0:
        return []
    out, est = [], None
    for t in _rowless(db):
        first = db.one("SELECT MIN(started) s FROM runs WHERE task=? AND role!='coordinator' AND started IS NOT NULL",
                       (t["id"],))
        s = float(first["s"]) if first and first["s"] else None
        if s is None or s < now - win:
            continue
        if not db.one("SELECT 1 x FROM runs WHERE role='coordinator' AND started>? LIMIT 1", (s,)):
            continue   # no digest since its dispatch: the writer has not had its chance yet
        from . import eta
        if est is None:
            est = eta._Est(db, now, core=eta.core_of(where), delivery=eta.delivery_of(where))
        if rowable(eta.row_estimate(db, est, t, now), now):
            out.append({"id": t["id"], "title": t["title"], "dispatched": s})
    return out


def backfill(db, now: float) -> int:
    """Promises recorded before the ledger: their due as the ETA, their receipt as the delivery, once each."""
    from . import obligations
    have = {r["ref"] for r in read(db) if r.get("src") == "backfill"}
    rows = []
    for o in obligations.items(db):
        pr, rc = o.get("promise") or {}, o.get("receipt") or {}
        ref = str(o.get("id") or o.get("key") or "")
        due, made, at = pr.get("due"), pr.get("set_at") or o.get("created"), rc.get("at")
        try:
            due, made, at = float(due), float(made), (float(at) if at else None)
        except (TypeError, ValueError):
            continue
        if not ref or ref in have or due <= made:
            continue
        rows.append({"ts": made, "ref": ref, "kind": "promise", "src": "backfill", "likely": due, "shown": due,
                     "conservative": due, "due": due})
        if at:
            rows.append({"ts": at, "ref": ref, "delivered": at})
    _append(db, rows)
    return len(rows)


def settle(db, now: float) -> int:
    """Delivery rows for the refs whose work landed or closed since: a task by landed.landed_at (a failed or
    cancelled one is `closed`, left out of the statistics), a promise by its obligation's receipt."""
    from . import landed, obligations
    rows = read(db)
    settled = {r["ref"] for r in rows if "delivered" in r or "closed" in r}
    waiting = sorted({r["ref"] for r in rows if "shown" in r} - settled)
    out = []
    owners = {str(o.get("id")): o for o in obligations.items(db)} if any(r.startswith("o") for r in waiting) else {}
    for ref in waiting:
        if ref.startswith("o"):
            at = ((owners.get(ref) or {}).get("receipt") or {}).get("at")
            if at:
                out.append({"ts": round(now, 1), "ref": ref, "delivered": float(at)})
            continue
        try:
            t = db.task(int(ref[1:]))
        except ValueError:
            continue
        if not t:
            continue
        if t["status"] in ("failed", "cancelled"):
            out.append({"ts": round(now, 1), "ref": ref, "closed": t.get("updated") or now, "outcome": t["status"]})
        elif t["status"] == "done":
            try:
                at = landed.landed_at(db, t["id"])
            except Exception:
                at = None
            if at:
                out.append({"ts": round(now, 1), "ref": ref, "delivered": float(at)})
    _append(db, out)
    return len(out)


def pairs(rows: list[dict], since: float) -> list[dict]:
    """One entry per delivered ref whose first ETA is at or after `since`: its `kind`, `promised` and
    `raw` seconds from that ETA, `conservative` seconds, `actual` seconds, the `due` seconds for a promise,
    `early`: it was delivered before that ETA was written (actual 0, nothing was predicted), and `quoted`:
    the ETA is a backfilled promise's own due, not a computed one. A ref's first computed row wins over
    its backfill row."""
    first: dict[str, dict] = {}
    done: dict[str, float] = {}
    for r in rows:
        if "shown" in r and r["ts"] >= since and (r["ref"] not in first or (
                first[r["ref"]].get("src") == "backfill" and r.get("src") != "backfill")):
            first[r["ref"]] = r
        elif "delivered" in r:
            done[r["ref"]] = float(r["delivered"])
    out = []
    for ref, r in first.items():
        if ref not in done:
            continue
        p = r["shown"] - r["ts"]
        if p < 60:
            continue
        out.append({"ref": ref, "kind": r.get("kind") or "", "promised": p, "raw": max(r["likely"] - r["ts"], 60.0),
                    "conservative": (r.get("conservative") or r["shown"]) - r["ts"],
                    "due": (r["due"] - r["ts"]) if r.get("due") else None, "actual": max(done[ref] - r["ts"], 0.0),
                    "hist": max(r["hist"] - r["ts"], 0.0) if r.get("hist") else None, "early": done[ref] < r["ts"],
                    "quoted": r.get("src") == "backfill"})
    return out


def calibration(db, now: float, c: dict | None = None, days: float = 7.0) -> dict:
    """{"count", "median_ratio" (actual / promised), "share_by_conservative" (delivered by the conservative
    time, or by the due for a promise), "by_kind", and apart: "median_ratio_computed"/"count_computed" of
    computed ETAs, "median_ratio_quoted"/"count_quoted" of backfilled promises' own dues} of the last `days`
    days' deliveries. A backfilled promise or order whose due nobody named (the default due) is no ETA anyone
    quoted: it is left out of both and of the headline, its "median_ratio_default"/"count_default" apart."""
    from . import obligations
    ps = pairs(read(db), now - days * 86400)
    untimed = {o["id"] for o in obligations.items(db) if (o.get("promise") or {}).get("timed") is False}
    dflt = [p["actual"] / p["promised"] for p in ps if p.get("quoted") and p["ref"] in untimed]
    ps = [p for p in ps if not (p.get("quoted") and p["ref"] in untimed)]
    apart = {"median_ratio_default": statistics.median(dflt) if dflt else None, "count_default": len(dflt)}
    if not ps:
        return {"count": 0, "days": days, **apart}
    ratios = [p["actual"] / p["promised"] for p in ps]
    ok = [p["actual"] <= (p["due"] if p["due"] else p["conservative"]) for p in ps]
    kinds: dict[str, list[float]] = {}
    for p, r in zip(ps, ratios):
        kinds.setdefault(p["kind"], []).append(r)
    split = {q: [r for p, r in zip(ps, ratios) if p.get("quoted") == q] for q in (False, True)}
    return {"count": len(ps), "days": days, "median_ratio": statistics.median(ratios),
            "median_ratio_computed": statistics.median(split[False]) if split[False] else None,
            "count_computed": len(split[False]),
            "median_ratio_quoted": statistics.median(split[True]) if split[True] else None,
            "count_quoted": len(split[True]), **apart,
            "share_by_conservative": sum(ok) / len(ok),
            "by_kind": {k: {"count": len(v), "median_ratio": statistics.median(v)} for k, v in sorted(kinds.items())}}


def line(cal: dict) -> str:
    """The calibration line: `ETA calibration, 7 d: 31 delivered, median actual/promised 1.4, 58% by the
    conservative time (target 80%, median 0.7-1.3); computed 1.30 (n=225), quoted dues 0.18 (n=297)`, and
    the default dues (named by nobody) on a line of their own."""
    dflt = (f"\nETA default dues (no time named; not in the ratio above): {cal['median_ratio_default']:.2f} "
            f"(n={cal['count_default']})") if cal.get("count_default") else ""
    if not cal.get("count"):
        return f"ETA calibration, {cal.get('days', 7):g} d: no delivered ETA yet" + dflt
    split = ", ".join(f"{name} {cal[f'median_ratio_{k}']:.2f} (n={cal[f'count_{k}']})"
                      for k, name in (("computed", "computed"), ("quoted", "quoted dues"))
                      if cal.get(f"median_ratio_{k}") is not None)
    return (f"ETA calibration, {cal['days']:g} d: {cal['count']} delivered, median actual/promised "
            f"{cal['median_ratio']:.2f}, {cal['share_by_conservative']:.0%} by the conservative time or due "
            f"(target {TARGET_SHARE:.0%}, median {TARGET_RATIO[0]}-{TARGET_RATIO[1]})" + (f"; {split}" if split else "")
            + dflt)


def _p90(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.9 * len(xs)))]


PROMOTE_KINDS = ("code", "review", "work", "promise")
PROMOTE_MIN = 10


def _errors(ps: list[dict]) -> dict:
    cur = [abs(p["actual"] - p["promised"]) for p in ps]
    his = [abs(p["actual"] - p["hist"]) for p in ps]
    return {"count": len(ps), "current": {"median": statistics.median(cur), "p90": _p90(cur)},
            "hist": {"median": statistics.median(his), "p90": _p90(his)}}


def shadow(db, since: float) -> dict:
    """{"count", "current": {"median", "p90"}, "hist": {...}, "by_kind": {kind: the same}, "promotable"} of the
    delivered ETAs since `since` that carry a shadow `hist`: each method's absolute error (seconds) against
    the actual finish, a promise (`o3`) as kind `promise`. A pair delivered before its ETA was written is
    left out. `promotable`: each of PROMOTE_KINDS with PROMOTE_MIN pairs or more (at least one) has a history
    median and p90 no worse than the current one's."""
    ps = [p for p in pairs(read(db), since) if p.get("hist") and not p["early"]]
    if not ps:
        return {"count": 0, "promotable": False}
    kinds: dict[str, list[dict]] = {}
    for p in ps:
        kinds.setdefault("promise" if p["ref"].startswith("o") else p["kind"], []).append(p)
    by_kind = {k: _errors(v) for k, v in sorted(kinds.items())}
    judged = [by_kind[k] for k in PROMOTE_KINDS if k in by_kind and by_kind[k]["count"] >= PROMOTE_MIN]
    ok = bool(judged) and all(e["hist"]["median"] <= e["current"]["median"] and e["hist"]["p90"] <= e["current"]["p90"]
                              for e in judged)
    return {**_errors(ps), "by_kind": by_kind, "promotable": ok}


def shadow_line(sh: dict) -> str:
    """`ETA shadow (eta_history, 7 d): 12 compared; abs error median/p90 current 14m/52m, history 9m/40m;
    code 21m/138m vs 38m/98m (n=86); promotable: no`."""
    if not sh.get("count"):
        return "ETA shadow (eta_history, 7 d): no delivered ETA with a history estimate yet"

    def m(x: dict) -> str:
        return f"{x['median'] / 60:.0f}m/{x['p90'] / 60:.0f}m"
    kinds = "".join(f"; {k} {m(e['current'])} vs {m(e['hist'])} (n={e['count']})"
                    for k, e in (sh.get("by_kind") or {}).items())
    return (f"ETA shadow (eta_history, 7 d): {sh['count']} compared; abs error median/p90 current "
            f"{m(sh['current'])}, history {m(sh['hist'])}{kinds}; promotable: {'yes' if sh.get('promotable') else 'no'}")


def _clamp(c: dict) -> tuple[float, float]:
    try:
        lo, hi = c.get("clamp") or CLAMP
        return max(0.1, float(lo)), max(float(lo), float(hi))
    except (TypeError, ValueError):
        return CLAMP


def factors(db, where, now: float) -> dict:
    """The per-kind correction: the trailing 7-day median of actual / promised, bounded, used from
    `min_samples` deliveries on."""
    c = cfg(where)
    lo, hi = _clamp(c)
    try:
        need = max(1, int(c.get("min_samples", MIN_SAMPLES)))
    except (TypeError, ValueError):
        need = MIN_SAMPLES
    ps = [p for p in pairs(read(db), now - WEEK_S) if p["kind"] != "promise"]
    by: dict[str, list[float]] = {}
    for p in ps:
        by.setdefault(p["kind"], []).append(p["actual"] / p["promised"])
    out = {k: round(min(hi, max(lo, statistics.median(v))), 3) for k, v in by.items() if len(v) >= need}
    cons = None
    if len(ps) >= need:
        rs = sorted(p["actual"] / p["promised"] for p in ps)
        cons = round(min(6.0, max(1.25, rs[min(len(rs) - 1, int(0.8 * len(rs)))])), 3)
    return {"on": c.get("correction") is not False, "factors": out, "conservative_x": cons, "at": now,
            "samples": len(ps)}


def refresh(db, where, now: float | None = None, force: bool = False) -> None:
    """Backfill, settle and recompute the factors, at most every 10 min (a failure never reaches the caller)."""
    now = time.time() if now is None else now
    try:
        got = db.kv(KEY) or {}
        if not force and isinstance(got, dict) and now - (got.get("at") or 0) < REFRESH_S and \
                got.get("on") == (cfg(where).get("correction") is not False):
            return
        if cfg(where).get("ledger") is not False:
            backfill(db, now)
            settle(db, now)
        db.set_kv(KEY, factors(db, where, now))
    except Exception:
        return
