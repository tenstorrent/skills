# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model scoreboard: one account-wide quality and speed per (chat model, task class), and the order each
class runs its models in, model-free.

The account file FILE (~/.tt-project/model-scores.json, merged under LOCK) keeps each project's own live
rows: every project's daemon whose chat provider has an endpoint replaces its own at most every EVERY_S
(record), from its worker runs on the chat provider over DAYS (live_rows). Per (model, class) a row counts
its tasks (n: the ended tasks whose first chat run it was, not counting a run an endpoint fault ended), their
first-try done runs (first_done: that run handed off without a fallback), the bad ones by modeltrip.py's sample rules (failed, checks_failed,
review_failed, rework), the runs' fallback, no_handoff and salvaged ends, the median wall time per task and
the output tokens per second. Rows of a project not seen for DAYS are dropped.

Bench (bench): each conclusive result of the host's daily bench (modelbench.py's day files) and every row of
model-bench/*.jsonl ({"model", "score" 0-1 or 0-100, "suite", "class", "inject_ok": false on a failed
prompt-injection cell}); a model's bench score is the mean of its latest score per suite, per class where a
row names one. A failed injection cell bars the model from babysit (and from untrusted_input work: barred).
Prior: the Artificial Analysis index (rankfresh.py's state), only for a model with no bench score.

Quality (board) = the bench score (else the prior) weighted against the live good share (first-try done and
not bad) by sample count: live weighs n / (n + live_k) (providers.chat.class_prefs.live_k, default LIVE_K);
with neither bench nor prior, live alone. Orders per class (order): smart, highest quality first; fast, the
models within margin_pts of the best and at or above the class floor, fastest first (median wall time, then
tokens/s), then the rest as smart. Unscored models go last, in the account ranking's order.

publish writes providers.chat.class_ranking.<class> (account values: each class's
providers.chat.class_prefs.<class>.prefer, smart, fast or auto; auto is fast for FAST_CLASSES, else smart)
and keeps providers.chat.model_ranking equal to the standard class, so modelrank.py keeps working; each
change is one rankfresh LOG line. The file keeps both orders of every class for a start that wants the other
(an urgent or critical-path task under auto: fast).
"""
from __future__ import annotations

import fcntl
import json
import statistics
from pathlib import Path

from . import route

CLASSES = ("babysit", "light", "standard", "review")
FAST_CLASSES = ("babysit",)          # auto means fast here (latency-bound)
PREFS = ("smart", "fast", "auto")
FILE, LOCK = "model-scores.json", "model-scores.lock"   # under project.HOME_DIR
KEY = "providers.chat.class_ranking"
PREFS_KEY = "providers.chat.class_prefs"
DAYS, EVERY_S, LIVE_K = 14, 3600, 10
MARGIN_PTS, FLOOR = 5.0, 0.0
OWN = ("none", "malformed", "timeout", "failed", "no_handoff")   # a run end that is the model's own (else the endpoint's)
COUNTS = ("n", "first_done", "good", "bad", "failed", "checks_failed", "review_failed", "rework", "runs", "fallback",
          "no_handoff", "salvaged", "out_tokens")


def _home() -> Path:
    from .project import HOME_DIR
    return HOME_DIR


def class_of(kind: str | None, tier: str | None) -> str:
    return "review" if kind == "review" else "light" if tier == "light" else "standard"


def live_rows(p, now: float, days: int = DAYS) -> dict[str, dict[str, dict]]:
    """{model: {class: row}} of this project's chat runs started in the last `days`."""
    from .modeltrip import mark_reworked
    from .prguard import CHECKS_FILE
    since, rows, samples, walls, seen = now - days * 86400, {}, {}, {}, set()
    for r in p.db.q("SELECT r.id, r.task, r.model, r.status, r.started, r.ended, r.output_tokens, r.note, t.kind, "
                    "t.tier, t.status tstatus FROM runs r LEFT JOIN tasks t ON t.id=r.task WHERE r.provider=? AND "
                    "r.started>=? AND r.started<? AND r.status!='running' AND r.model!='' ORDER BY r.id",
                    (route.TO, since, now)):
        e = route.of(r["note"]) or {}
        cls, fell = class_of(r["kind"], e.get("tier") or r["tier"]), e.get("fallback") or "none"
        s = rows.setdefault(r["model"], {}).setdefault(cls, dict.fromkeys(COUNTS, 0) | {"tok_wall_s": 0.0})
        wall = float(r["ended"]) - float(r["started"]) if r["ended"] and r["started"] else 0.0
        s["runs"] += 1
        s["fallback"] += fell != "none"
        s["no_handoff"] += fell == "no_handoff"
        s["salvaged"] += bool(e.get("salvaged"))
        if int(r["output_tokens"] or 0) and wall > 0:
            s["out_tokens"] += int(r["output_tokens"])
            s["tok_wall_s"] += wall
        k = (r["model"], cls, r["task"])
        walls[k] = walls.get(k, 0.0) + wall
        if r["task"] is not None and r["task"] not in seen and r["tstatus"] in ("done", "failed") and fell in OWN:
            seen.add(r["task"])   # the task's first chat run decides its model
            try:
                ck = json.loads((p.runs / str(r["id"]) / CHECKS_FILE).read_text())
            except (OSError, ValueError):
                ck = None
            samples[k] = {"first_done": r["status"] == "ok" and fell == "none", "failed": r["tstatus"] == "failed",
                          "checks_failed": isinstance(ck, dict) and ck.get("passed") is False}
    mark_reworked(p.db, {k[2]: v for k, v in samples.items()}, since)
    for (m, cls, _), v in samples.items():
        s = rows[m][cls]
        s["n"] += 1
        bad = any(v.get(f) for f in ("failed", "checks_failed", "review_failed", "rework"))
        for f in ("first_done", "failed", "checks_failed", "review_failed", "rework"):
            s[f] += bool(v.get(f))
        s["bad"] += bad
        s["good"] += bool(v["first_done"]) and not bad
    for m, by in rows.items():
        for cls, s in by.items():
            w = [t for (wm, wc, _), t in walls.items() if wm == m and wc == cls and t > 0]
            s["wall_med_s"] = round(statistics.median(w), 1) if w else None
    return rows


def _read() -> dict:
    try:
        v = json.loads((_home() / FILE).read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _write(data: dict) -> None:
    from .project import durable_write
    durable_write(_home() / FILE, json.dumps(data, indent=1, sort_keys=True) + "\n")


def _locked(fn):
    _home().mkdir(parents=True, exist_ok=True)
    with open(_home() / LOCK, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        return fn(_read())


def record(project: str, rows: dict, now: float) -> None:
    """Replace `project`'s own live rows in FILE, dropping projects not seen for DAYS."""
    def work(data: dict) -> None:
        projects = {n: v for n, v in (data.get("projects") or {}).items()
                    if isinstance(v, dict) and now - float(v.get("at") or 0) < DAYS * 86400}
        _write({**data, "projects": {**projects, project: {"at": now, "rows": rows}}})
    _locked(work)


def merged(data: dict) -> dict[str, dict[str, dict]]:
    """Every project's rows summed per (model, class); wall_med_s is the n-weighted mean of their medians."""
    out: dict[str, dict[str, dict]] = {}
    for v in (data.get("projects") or {}).values():
        for m, by in ((v or {}).get("rows") or {}).items():
            for cls, s in (by or {}).items():
                t = out.setdefault(m, {}).setdefault(cls, dict.fromkeys(COUNTS, 0) | {"tok_wall_s": 0.0, "_w": [0.0, 0]})
                for f in (*COUNTS, "tok_wall_s"):
                    t[f] += s.get(f) or 0
                if s.get("wall_med_s") is not None:
                    t["_w"][0] += float(s["wall_med_s"]) * max(1, int(s.get("n") or 0))
                    t["_w"][1] += max(1, int(s.get("n") or 0))
    for by in out.values():
        for t in by.values():
            w = t.pop("_w")
            t["wall_med_s"] = round(w[0] / w[1], 1) if w[1] else None
    return out


def bench() -> dict[str, dict]:
    """{model: {"scores": {class or "*": {suite: (at, score 0-100)}}, "injection_failed": bool}}."""
    out: dict[str, dict] = {}

    def add(m, cls, suite, at, score, inject_ok) -> None:
        b = out.setdefault(m, {"scores": {}, "injection_failed": False, "_inj_at": -1.0})
        cur = b["scores"].setdefault(cls or "*", {}).get(suite)
        if score is not None and (cur is None or at >= cur[0]):
            b["scores"][cls or "*"][suite] = (at, score * 100 if score <= 1 else score)
        if inject_ok is not None and at >= b["_inj_at"]:
            b["injection_failed"], b["_inj_at"] = not inject_ok, at
    d = _home() / "model-bench"
    for f in sorted(d.glob("20*.json")) if d.is_dir() else ():
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for m, r in ((rec.get("results") or {}) if isinstance(rec, dict) else {}).items():
            if isinstance(r, dict) and not r.get("inconclusive") and isinstance(r.get("score"), (int, float)):
                cells = r.get("cells") if isinstance(r.get("cells"), dict) else {}
                add(m, None, "cells", float(rec.get("at") or 0), float(r["score"]),
                    cells.get("inject") if "inject" in cells else None)
    for f in sorted(d.glob("*.jsonl")) if d.is_dir() else ():
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(r, dict) or not isinstance(r.get("model"), str):
                continue
            sc = r.get("score") if isinstance(r.get("score"), (int, float)) and not isinstance(r.get("score"), bool) else None
            inj = r.get("inject_ok") if isinstance(r.get("inject_ok"), bool) else None
            add(r["model"], r.get("class") if r.get("class") in CLASSES else None, str(r.get("suite") or f.stem),
                float(r.get("at") or 0), None if sc is None else float(sc), inj)
    for b in out.values():
        b.pop("_inj_at")
    return out


def prefs(cls: str, raw: dict | None = None) -> dict:
    """The class's {"prefer", "margin_pts", "floor"} (the account's providers.chat.class_prefs, else defaults)."""
    if raw is None:
        from .project import account_value
        raw = account_value(PREFS_KEY)
    c = (raw or {}).get(cls) if isinstance(raw, dict) else None
    c = c if isinstance(c, dict) else {}
    num = lambda v, d: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else d
    return {"prefer": c.get("prefer") if c.get("prefer") in PREFS else "auto",
            "margin_pts": num(c.get("margin_pts"), MARGIN_PTS), "floor": num(c.get("floor"), FLOOR)}


def board(live: dict, benched: dict, prior: dict[str, float], models, live_k: float = LIVE_K) -> dict:
    """{model: {class: {"quality", "n", "good", "bench", "prior", "wall_s", "tok_s"}}} for `models`."""
    out = {}
    for m in models:
        b = benched.get(m) or {}
        for cls in CLASSES:
            s = (live.get(m) or {}).get(cls) or {}
            suites = (b.get("scores") or {}).get(cls) or (b.get("scores") or {}).get("*") or {}
            bs = statistics.fmean(v[1] for v in suites.values()) if suites else None
            base = bs if bs is not None else prior.get(m)
            n, good = int(s.get("n") or 0), int(s.get("good") or 0)
            lq = 100.0 * good / n if n else None
            w = n / (n + live_k) if n else 0.0
            q = lq if base is None else base if lq is None else (1 - w) * base + w * lq
            tok = float(s.get("out_tokens") or 0) / float(s["tok_wall_s"]) if s.get("tok_wall_s") else None
            out.setdefault(m, {})[cls] = {"quality": None if q is None else round(q, 2), "n": n, "good": good,
                                          "bench": None if bs is None else round(bs, 1), "prior": prior.get(m),
                                          "wall_s": s.get("wall_med_s"), "tok_s": None if tok is None else round(tok, 1)}
    return out


def order(bd: dict, cls: str, prefer: str, margin: float = MARGIN_PTS, floor: float = FLOOR,
          known: list[str] | None = None, barred=()) -> list[str]:
    """`cls`'s models, best first by `prefer` (smart or fast); unscored ones last in `known`'s order."""
    pos = {m: i for i, m in enumerate(known or [])}
    ms = [m for m in bd if not (cls == "babysit" and m in barred)]
    scored = sorted((m for m in ms if bd[m][cls]["quality"] is not None), key=lambda m: (-bd[m][cls]["quality"], m))
    rest = sorted((m for m in ms if bd[m][cls]["quality"] is None), key=lambda m: (pos.get(m, len(pos)), m))
    if prefer != "fast" or not scored:
        return scored + rest
    best = bd[scored[0]][cls]["quality"]
    elig = [m for m in scored if bd[m][cls]["quality"] >= max(best - margin, floor)]

    def speed(m):
        r = bd[m][cls]
        return (r["wall_s"] is None, r["wall_s"] or 0, -(r["tok_s"] or 0), scored.index(m))
    elig.sort(key=speed)
    return elig + [m for m in scored if m not in elig] + rest


def barred(benched: dict | None = None) -> list[str]:
    """Models whose latest prompt-injection bench cell failed: never babysit or untrusted_input work."""
    return sorted(m for m, b in (bench() if benched is None else benched).items() if b.get("injection_failed"))


def build(served: list[str] | None = None, raw_prefs: dict | None = None) -> dict:
    """{"board", "orders": {class: {"smart", "fast"}}, "ranking": {class: [...]}, "barred"} from FILE, the
    bench, the prior and the account ranking."""
    from . import rankfresh
    from .modelrank import ranking_of
    from .project import account_value
    live, benched = merged(_read()), bench()
    prior = {k: float(v) for k, v in (rankfresh.read_state().get("scores") or {}).items()
             if isinstance(v, (int, float))}
    try:
        known = ranking_of(account_value(rankfresh.KEY))
    except ValueError:
        known = []
    models = dict.fromkeys([*known, *(served or []), *(rankfresh.read_state().get("seen") or []), *live, *benched])
    raw = account_value(PREFS_KEY) if raw_prefs is None else raw_prefs
    lk = (raw or {}).get("live_k") if isinstance(raw, dict) else None
    bd = board(live, benched, prior, models, float(lk) if isinstance(lk, (int, float)) and lk > 0 else LIVE_K)
    bar = barred(benched)
    orders, ranking = {}, {}
    for cls in CLASSES:
        pf = prefs(cls, raw)
        orders[cls] = {k: order(bd, cls, k, pf["margin_pts"], pf["floor"], known, bar) for k in ("smart", "fast")}
        pick = pf["prefer"] if pf["prefer"] != "auto" else "fast" if cls in FAST_CLASSES else "smart"
        ranking[cls] = orders[cls][pick]
    return {"board": bd, "orders": orders, "ranking": ranking, "barred": bar}


def publish(served: list[str] | None, now: float) -> dict:
    """Write the class rankings (KEY) and model_ranking (= standard) as account values when they changed, and
    the orders into FILE. {"changed", "old", "new"}: old and new are the standard class."""
    from . import rankfresh
    from .modelrank import ranking_of
    from .project import account_value, set_account_setting

    def work(data: dict) -> dict:
        got = build(served)
        _write({**data, "at": now, "orders": got["orders"], "barred": got["barred"], "board": got["board"]})
        old_cls = account_value(KEY) or {}
        if got["ranking"] != old_cls and any(got["ranking"].values()):
            set_account_setting(KEY, got["ranking"])
        try:
            old = ranking_of(account_value(rankfresh.KEY))
        except ValueError:
            old = []
        new = got["ranking"]["standard"]
        if new and new != old:
            set_account_setting(rankfresh.KEY, new)
            rankfresh.log_change(old, new, "scores", now, by="model scoreboard")
        return {"changed": bool(new) and new != old, "old": old, "new": new}
    return _locked(work)


def fast_order(cls: str) -> list[str]:
    """`cls`'s fast order from FILE (an urgent or critical-path start under auto), or []."""
    return list(((_read().get("orders") or {}).get(cls) or {}).get("fast") or [])


def text(got: dict) -> str:
    """build()'s class rankings as a table (`ttp model-bench scores`)."""
    lines = []
    for cls in CLASSES:
        lines.append(f"{cls} ({'fast' if got['ranking'][cls] == got['orders'][cls]['fast'] and got['ranking'][cls] != got['orders'][cls]['smart'] else 'smart'}):")
        for i, m in enumerate(got["ranking"][cls], 1):
            r = got["board"][m][cls]
            q = "-" if r["quality"] is None else f"{r['quality']:.1f}"
            lines.append(f"  {i}. {m:<32} quality {q:>5}  live {r['good']}/{r['n']}  bench {r['bench'] if r['bench'] is not None else '-'}"
                         f"  prior {r['prior'] if r['prior'] is not None else '-'}  wall {r['wall_s'] if r['wall_s'] is not None else '-'}s"
                         f"  tok/s {r['tok_s'] if r['tok_s'] is not None else '-'}")
    if got["barred"]:
        lines.append(f"barred from babysit and untrusted_input (failed the injection cell): {', '.join(got['barred'])}")
    return "\n".join(lines)
