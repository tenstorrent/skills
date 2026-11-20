# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model-pick tripwire: a stratum whose routed runs go wrong more often than its core runs goes back to
the core model. Model-free; inactive while the model dispatcher (modelpick.py) is off.

A stratum is a task's kind, the tier it was picked at and one of its labels ('*': any label; a label
naming a task id, such as `continues:<id>`, is no stratum). Each task that was open to routing (its first
run note's modelpick.KEY entry is a Jev pick, or a rules pick of why `jev_unavailable` or `tripwire`;
other rule-forced core starts, such as hard, device or urgent tasks, are riskier by design and count on
neither side) and ended done or failed in the last `days` is one sample: routed when its pick is an
option on the routed-to provider, else core. A sample is bad when the task failed, a run's last
`ttp checks` failed, a review of it failed, or it got rework (a review fix or a continuation of it).

`check` trips a stratum once both sides have `min_samples` samples and the routed bad rate exceeds
core's by more than `margin_pts` points: `modelpick.rules` then sends its tasks to core (why
`tripwire`), a decision memory records it and the caller sends one low notice. Only a newer passing
head-to-head result for that stratum (`head_to_head`, `ttp model-trip --h2h`) re-enables it; samples
then count from that result on (a worse result moves no window). Settings:
`providers.chat.dispatch.tripwire` (DEFAULTS).
"""
from __future__ import annotations

import json
import re
import time
from fractions import Fraction

from . import chaincap, modelpick
from .db import DB, review_subject

TRIPS = "model_trip"      # kv: {stratum: {at, routed: [bad, n], core: [bad, n], margin_pts}}
H2H = "model_h2h"         # kv: {stratum: {at, ok, ref}}
ANY = "*"
DEFAULTS = {"min_samples": 10, "margin_pts": 10.0, "days": 14}
ID_LABEL = re.compile(r":\d+$")
OPEN_WHY = ("jev_unavailable", "tripwire")   # rules picks of a task open to routing (Jev failed, or held)


def settings(cfg: dict) -> dict:
    raw = modelpick.conf(cfg).get("tripwire")
    out = dict(DEFAULTS)
    for k, v in (raw.items() if isinstance(raw, dict) else ()):
        if k in DEFAULTS and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= (k == "min_samples"):
            out[k] = v
    return out


def strata(task: dict, tier: str) -> list[str]:
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (TypeError, ValueError):
        labels = []
    own = sorted({lb for lb in labels if isinstance(lb, str) and lb and not ID_LABEL.search(lb)}) \
        if isinstance(labels, list) else []
    return [f"{task.get('kind')}/{tier}/{lb}" for lb in [ANY, *own]]


def _rearmed(trip: dict | None, h2h: dict | None) -> bool:
    return bool(h2h and h2h.get("ok") and float(h2h.get("at") or 0) > float((trip or {}).get("at") or 0))


def tripped(db: DB, task: dict, tier: str) -> str | None:
    """The first of the task's strata the tripwire holds on core, or None."""
    trips, h2h = db.kv(TRIPS) or {}, db.kv(H2H) or {}
    return next((s for s in strata(task, tier) if s in trips and not _rearmed(trips[s], h2h.get(s))), None)


def samples(p, cfg: dict, since: float, until: float) -> dict[int, dict]:
    """{task id: {side, strata, at, bad}} for the picked tasks that ended, first run started in the window."""
    from .route import TO
    db, opts = p.db, modelpick.options(cfg)
    out: dict[int, dict | None] = {}   # None: the task's first pick was forced to core by the rules
    for r in db.q("SELECT r.id, r.task, r.started, r.note, t.kind, t.labels, t.status FROM runs r JOIN tasks t "
                  "ON t.id=r.task WHERE r.role='worker' AND r.started>=? AND r.started<? AND r.note LIKE ? "
                  "AND t.status IN ('done','failed') ORDER BY r.id", (since, until, f'%"{modelpick.KEY}"%')):
        try:
            dp = json.loads(r["note"] or "{}").get(modelpick.KEY)
        except (TypeError, ValueError, AttributeError):
            dp = None
        if not isinstance(dp, dict):
            continue
        if r["task"] not in out:   # the first run's pick decides; a later retry start's rules pick does not
            if dp.get("by") == "jev" or (dp.get("by") == "rules" and dp.get("why") in OPEN_WHY):
                side = "routed" if (opts.get(str(dp.get("pick") or "")) or {}).get("provider") == TO else "core"
                out[r["task"]] = {"side": side, "strata": strata(dict(r), str(dp.get("tier") or "standard")),
                                  "at": float(r["started"]), "bad": r["status"] == "failed"}
            else:
                out[r["task"]] = None
        if (s := out[r["task"]]) is None:
            continue
        s["bad"] |= checks_failed(p, r["id"])
    return mark_reworked(db, {i: s for i, s in out.items() if s}, since)


def checks_failed(p, run_id: int) -> bool:
    """The run's last `ttp checks` failed."""
    from .prguard import CHECKS_FILE
    try:
        rec = json.loads((p.runs / str(run_id) / CHECKS_FILE).read_text())
    except (OSError, ValueError):
        rec = None
    return isinstance(rec, dict) and rec.get("passed") is False


def mark_reworked(db: DB, out: dict[int, dict], since: float) -> dict[int, dict]:
    """Mark bad each sample {task id: {"bad", ...}} a review since `since` failed (review_failed) or that got
    rework (a review fix or a continuation of it: rework)."""
    from .effortab import FAILED_REVIEWS
    if not out:
        return out
    for rv in db.q("SELECT t.labels, t.title, s.outcome FROM review_stats s JOIN tasks t ON t.id=s.task WHERE s.ts>=?",
                   (since,)):
        if rv["outcome"] in FAILED_REVIEWS and (i := review_subject(dict(rv))) in out:
            out[i]["bad"] = out[i]["review_failed"] = True
    for t in db.q("SELECT id, kind, title, labels, parent FROM tasks WHERE created>=? AND kind!='review'", (since,)):
        t = dict(t)
        i = chaincap.prev_of(t)
        if i is None and chaincap.is_review_fix(t):
            fix = next((lb for lb in json.loads(t.get("labels") or "[]") if str(lb).startswith("review_fix:")), "")
            rv = db.task(int(fix[11:])) if fix[11:].isdigit() else None
            i = review_subject(rv) if rv else None
        if i in out and i != t["id"]:
            out[i]["bad"] = out[i]["rework"] = True
    return out


def check(p, cfg: dict, now: float | None = None) -> list[dict]:
    """Trip each stratum past the margin; returns the new trips ({stratum, at, routed, core, margin_pts})."""
    if not modelpick.enabled(cfg):
        return []
    st = settings(cfg)
    now = time.time() if now is None else now
    db = p.db
    trips, h2h = dict(db.kv(TRIPS) or {}), db.kv(H2H) or {}
    counts: dict[str, dict[str, list[int]]] = {}
    for s in samples(p, cfg, now - st["days"] * 86400, now).values():
        for k in s["strata"]:
            if (h := h2h.get(k) or {}).get("ok") and s["at"] <= float(h.get("at") or 0):
                continue   # before the passing head-to-head that (re-)armed it
            c = counts.setdefault(k, {"routed": [0, 0], "core": [0, 0]})[s["side"]]
            c[0] += s["bad"]
            c[1] += 1
    new = []
    for k, c in sorted(counts.items()):
        if k in trips and not _rearmed(trips[k], h2h.get(k)):
            continue
        (rb, rn), (cb, cn) = c["routed"], c["core"]
        # exact: 100 * (rb/rn - cb/cn) > margin_pts, in integers and fractions (no float boundary error)
        if min(rn, cn) < st["min_samples"] or 100 * (rb * cn - cb * rn) <= Fraction(st["margin_pts"]) * rn * cn:
            continue
        trips[k] = {"at": now, "routed": [rb, rn], "core": [cb, cn], "margin_pts": st["margin_pts"]}
        new.append({"stratum": k, **trips[k]})
    if new:
        db.set_kv(TRIPS, trips)
        for t in new:
            try:   # the trip holds and its notice goes out even when the memory cannot be written
                p.add_memory(text(t), kind="decision", title=f"Model routing tripped back to core: {t['stratum']}",
                             key=f"model-trip:{t['stratum']}:{int(t['at'])}")
            except Exception as e:
                t["memory_error"] = f"{type(e).__name__}: {e}"
    return new


def text(t: dict) -> str:
    (rb, rn), (cb, cn) = t["routed"], t["core"]
    return (f"Model routing for {t['stratum']} (kind/tier/label) is back on the core model: routed tasks went wrong "
            f"{rb}/{rn} ({100 * rb / rn:.0f}%) against core's {cb}/{cn} ({100 * cb / cn:.0f}%), more than "
            f"{t['margin_pts']:g} points worse. Only a new passing head-to-head result for it turns routing back on "
            f"(`ttp model-trip --h2h`).")


def head_to_head(db: DB, stratum: str, ok: bool, ref: str, now: float | None = None) -> dict:
    """Record a head-to-head result for `stratum`: a passing one newer than its trip re-enables it."""
    rec = {"at": time.time() if now is None else now, "ok": bool(ok), "ref": ref}
    db.set_kv(H2H, {**(db.kv(H2H) or {}), stratum: rec})
    return rec
