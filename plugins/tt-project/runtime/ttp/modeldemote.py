# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Live model demotion: a chat model that does worse than the next eligible model in a task class stops
being picked for that class. Model-free; it generalizes modeltrip.py's routed-vs-core tripwire to model vs
model.

A sample is a task that ended done or failed in the last `days` whose first worker run ran on the chat
provider: its model and class ("review" for a review, else the run's tier) are that run's. It is bad by
modeltrip's rules (failed, a run's checks failed, a review of it failed, rework) or when a run of it fell
back on the model's own fault (modelrank.FAULTS_PARK). Per class, the models are ordered by the class's
ranking (providers.chat.class_ranking.<class> where set, else modelrank.ranking). A model's peer is the
next model below it with `min_samples` samples that is not demoted or parked (else the nearest above).
check demotes a model for a class once both have `min_samples` samples and its bad rate exceeds the
peer's by more than `margin_pts` points, or, in a `fast_classes` class, its median wall time is over
`slow_x` times the peer's. It parks the model (modelrank.PARKED: weekly bench recheck only) once its
malformed or no_handoff share passes `fault_share`, or its last bench score is under `floor` of the best
benched. Only samples and bench scores newer than the model's last record count, so a demotion ended by a
bench pass or a promote starts from fresh evidence. Each new action is one low alert and one coordinator
digest line (digest_lines).

A demotion (kv DEMOTED per "<model>|<class>", class "*": every class) makes modelrank treat the model as
down for that class: routed starts run the next ranked model instead, and better() never picks it. Only
a newer passing bench score (modelbench.verdict) ends the detector's demotion. `ttp model
demote|promote|park|unpark` records the setter (configwho); a lower setter (harness < coordinator <
user) never overrides a higher one's record, so the user's word stands until the user changes it, and
a user's promote also holds the detector off. Settings: providers.chat.demote (DEFAULTS).
"""
from __future__ import annotations

import statistics
import time
from fractions import Fraction

from . import modelrank, modeltrip, route

DEMOTED = "model_demoted:"    # kv per "<model>|<class>": {"state": "demoted"|"promoted", "at", "by", "why", ...}
UNPARKED = "model_unparked:"  # kv per model: {"at", "by"} of the last unpark (holds lower setters' parks off)
ANY = "*"
LOG_KEY = "model_demote_log"  # kv: the last actions [{at, model, class, action, why}] (digest_lines)
DEFAULTS = {"min_samples": 8, "margin_pts": 15.0, "days": 7, "slow_x": 2.0, "floor": 0.5, "fault_share": 0.2,
            "fast_classes": ["babysit"]}
FAULTS = ("malformed", "no_handoff")   # a fallback that is the model's own malformed output or missing hand-off
RANK = {"user": 3, "coordinator": 2}   # setters above the detector ("harness", 1); anyone else is 0
KEEP = 50


def settings(cfg: dict) -> dict:
    raw = (((cfg.get("providers") or {}).get(route.TO) or {}).get("demote"))
    out = dict(DEFAULTS)
    for k, v in (raw.items() if isinstance(raw, dict) else ()):
        if k == "fast_classes" and isinstance(v, list) and all(isinstance(c, str) for c in v):
            out[k] = list(v)
        elif k in DEFAULTS and k != "fast_classes" and isinstance(v, (int, float)) and not isinstance(v, bool) \
                and v >= (k == "min_samples"):
            out[k] = v
    prefs = (((cfg.get("providers") or {}).get(route.TO) or {}).get("class_prefs")) or {}
    out["fast_classes"] = sorted({*out["fast_classes"], *(c for c, v in (prefs.items() if isinstance(prefs, dict)
                                                                         else ()) if (v or {}).get("prefer") == "fast")})
    return out


def class_ranking(cfg: dict, cls: str) -> list[str]:
    """The class's own ranking (an account value where the project sets none), else model_ranking."""
    v = ((((cfg.get("providers") or {}).get(route.TO) or {}).get("class_ranking")) or {}).get(cls)
    if v is None:
        from .project import account_value
        acct = account_value(f"providers.{route.TO}.class_ranking")
        v = acct.get(cls) if isinstance(acct, dict) else None
    try:
        return modelrank.ranking_of(v) or modelrank.ranking(cfg)
    except ValueError:
        return modelrank.ranking(cfg)


def _rank(setter: dict | None) -> int:
    r = (setter or {}).get("by")
    return RANK.get(r, 1 if r == "harness" else 0)


def record(db, model: str, cls: str) -> dict:
    return db.kv(f"{DEMOTED}{model}|{cls or ANY}") or {}


def demoted(db, model: str, cls: str, now: float) -> bool:
    """`model` is demoted for `cls` (or every class); a detector demotion ends at a newer passing bench."""
    from . import modelbench
    for c in dict.fromkeys([cls or ANY, ANY]):
        rec = record(db, model, c)
        if rec.get("state") == "demoted" and not (
                _rank(rec.get("by")) <= 1 and modelbench.verdict(model, float(rec.get("at") or 0), now) == "passed"):
            return True
    return False


def set_state(db, model: str, cls: str, action: str, setter: dict, now: float, why: str = "", **stats) -> str:
    """Apply `action` (demote, promote, park, unpark) for `model` by `setter` (configwho.by); '' once done,
    else why it was refused (a higher setter's record stands)."""
    key = f"{DEMOTED}{model}|{cls or ANY}"
    if action in ("park", "unpark"):   # the newer of the model's park and unpark records
        old = max((db.kv(k + model) or {} for k in (modelrank.PARKED, UNPARKED)),
                  key=lambda r: float(r.get("at") or 0))
    else:
        old = db.kv(key)
    if old and _rank(old.get("by")) > _rank(setter):
        who = (old.get("by") or {}).get("by")
        state = ("unparked" if old.get("unpark") else "parked") if action in ("park", "unpark") else old.get("state")
        return f"{model} was {state} by the {who}; " \
               f"only the {who} changes that"
    if action == "park":
        db.set_kv(modelrank.PARKED + model, {"at": now, "why": why or "set", "by": setter, **stats})
    elif action == "unpark":
        db.x("DELETE FROM kv WHERE key=?", (modelrank.PARKED + model,))
        db.set_kv(UNPARKED + model, {"at": now, "by": setter, "unpark": True})
    else:
        db.set_kv(key, {"state": "demoted" if action == "demote" else "promoted", "at": now, "by": setter,
                        "why": why or "set", **stats})
    entry = {"at": now, "model": model, "class": cls or ANY, "action": action, "why": why or "set",
             "by": setter.get("by")}
    db.set_kv(LOG_KEY, [*(db.kv(LOG_KEY) or []), entry][-KEEP:])
    return ""


def samples(p, cfg: dict, since: float, until: float) -> dict[int, dict]:
    """{task id: {model, cls, at, wall, bad, fault}} for tasks whose first worker run ran on chat."""
    import json
    out: dict[int, dict | None] = {}
    for r in p.db.q("SELECT r.id, r.task, r.provider, r.model, r.started, r.note, t.kind, t.status FROM runs r "
                    "JOIN tasks t ON t.id=r.task WHERE r.role='worker' AND r.started>=? AND r.started<? "
                    "AND t.status IN ('done','failed') ORDER BY r.id", (since, until)):
        try:
            ent = json.loads(r["note"] or "{}").get(route.KEY) or {}
        except (TypeError, ValueError, AttributeError):
            ent = {}
        on = r["provider"] == route.TO and bool(r["model"])
        if r["task"] not in out:   # the first run decides the task's model and class
            out[r["task"]] = {"model": r["model"], "cls": "review" if r["kind"] == "review"
                              else str(ent.get("tier") or "standard"), "at": float(r["started"]),
                              "wall": ent.get("wall_s"), "bad": r["status"] == "failed", "fault": False} if on else None
        if (s := out[r["task"]]) is None or not on:
            continue
        fell = str(ent.get("fallback") or "")
        s["fault"] |= fell in FAULTS
        s["bad"] |= fell in modelrank.FAULTS_PARK or modeltrip.checks_failed(p, r["id"])
    return modeltrip.mark_reworked(p.db, {i: s for i, s in out.items() if s}, since)


def _excluded(db, model: str, cls: str, now: float) -> bool:
    return demoted(db, model, cls, now) or modelrank.parked(db, model, now)


def check(p, cfg: dict, now: float | None = None) -> list[dict]:
    """Demote or park each model past its gates; returns the new actions ({model, class, action, why, ...})."""
    from . import configwho, modelbench
    st, now = settings(cfg), time.time() if now is None else now
    db, harness, new = p.db, configwho.by("harness", via="modeldemote"), []
    per: dict[tuple[str, str], list[dict]] = {}
    for s in samples(p, cfg, now - st["days"] * 86400, now).values():
        per.setdefault((s["cls"], s["model"]), []).append(s)
    bench = modelbench.models()
    best = max((float(r.get("score") or 0) for r in bench.values()), default=0.0)
    for m in sorted({m for _, m in per} | {m for m in modelrank.ranking(cfg) if m in bench}):   # parked: a buggy
        # model (its own faults) or a bench under the floor
        last = max(float((db.kv(k + m) or {}).get("at") or 0) for k in (modelrank.PARKED, UNPARKED))
        got = [s for (_, mm), ss in per.items() if mm == m for s in ss if s["at"] > last]   # since its last park
        faults = sum(s["fault"] for s in got)
        rec = bench.get(m) or {}
        score = rec.get("score") if float(rec.get("at") or 0) > last else None
        why = f"malformed or no hand-off in {faults}/{len(got)} tasks" if len(got) >= st["min_samples"] and \
            faults > st["fault_share"] * len(got) else f"bench {score:.2f}, under {st['floor']:g} of the best " \
            f"{best:.2f}" if score is not None and best and float(score) < st["floor"] * best else ""
        if why and not modelrank.parked(db, m, now) and not set_state(db, m, ANY, "park", harness, now, why):
            new.append({"model": m, "class": ANY, "action": "park", "why": why})
    for (cls, m), mine in sorted(per.items()):
        rec = record(db, m, cls)
        mine = [s for s in mine if s["at"] > float(rec.get("at") or 0)]   # an ended demotion or promote resets it
        if len(mine) < st["min_samples"] or _excluded(db, m, cls, now) or _rank(rec.get("by")) > 1:
            continue
        order = [*class_ranking(cfg, cls)]
        order += sorted(x for (c, x) in per if c == cls and x not in order)
        i = order.index(m)
        ok = [x for x in order if x != m and len(per.get((cls, x), ())) >= st["min_samples"]
              and not _excluded(db, x, cls, now)]
        peer = next((x for x in ok if order.index(x) > i), None) or next((x for x in reversed(ok)), None)
        if peer is None:
            continue
        theirs = per[(cls, peer)]
        mb, mn, pb, pn = sum(s["bad"] for s in mine), len(mine), sum(s["bad"] for s in theirs), len(theirs)
        why = ""
        if 100 * (mb * pn - pb * mn) > Fraction(st["margin_pts"]) * mn * pn:   # exact, as in modeltrip
            why = f"went wrong {mb}/{mn} against {peer}'s {pb}/{pn}, over {st['margin_pts']:g} points worse"
        elif cls in st["fast_classes"]:
            mw = [float(s["wall"]) for s in mine if isinstance(s["wall"], (int, float))]
            pw = [float(s["wall"]) for s in theirs if isinstance(s["wall"], (int, float))]
            if mw and pw and statistics.median(mw) > st["slow_x"] * statistics.median(pw):
                why = f"median {statistics.median(mw):.0f}s against {peer}'s {statistics.median(pw):.0f}s, " \
                      f"over {st['slow_x']:g}x slower in a fast class"
        if why and not set_state(db, m, cls, "demote", harness, now, why, peer=peer, bad=[mb, mn], peer_bad=[pb, pn]):
            new.append({"model": m, "class": cls, "action": "demote", "why": why, "peer": peer})
    return new


def text(a: dict) -> str:
    where = "every class" if a["class"] == ANY else f"{a['class']} tasks"
    if a["action"] == "park":
        return (f"Chat model {a['model']} is parked ({a['why']}): no route picks it; the bench rechecks it weekly.")
    return (f"Chat model {a['model']} is demoted for {where}: it {a['why']}. Routed starts there run the next "
            f"ranked model; only a newer passing bench score ends it (`ttp model promote` overrides).")


def digest_lines(db, since: float, now: float | None = None) -> list[str]:
    """One coordinator digest line per demotion or park since `since` (the last coordinator turn)."""
    return [f"- {e['action']} {e['model']} ({'every class' if e['class'] == ANY else e['class']}, by "
            f"{e.get('by') or '?'}): {e['why']}" for e in (db.kv(LOG_KEY) or [])
            if float(e.get("at") or 0) > since][-10:]
