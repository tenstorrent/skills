# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Each standard worker task's model, picked once at its first start: by rules first, by Jev (its use
`model`, see jevuse) only for the cases the rules leave open.

Off unless project.json sets `providers.chat.dispatch.enabled: true`. `dispatch.options` maps each
option key to a `provider`, `model` and `effort` (for example `routed-top` on the chat provider and
`core` on the core provider); an option on the chat provider routes the run there (route.py), any
other runs on the core provider at its tier. Nothing is hardcoded: with no option on the core provider
the core pick is the tier's own route-free start.

Rules (model-free) send to core: any kind but code, work and harness; a deep task; a retry or a task
started before; a device task; a conclusion run (tierfloor.reasons); an urgent task; a task labelled
`untrusted_input`; a spec naming optimization, performance, architecture or hard-debugging work. A light
task follows the light tier's own route, and a task pinning a provider or model keeps its pin (no pick).
The rest are open: Jev answers a `choice` over the option keys from a redacted state capped near
STATE_CHARS (kind, tier, labels, title, the spec's head, the paths it names and their count, a
continuation's parent diff stat, the lineage's earlier route outcomes and the kind's routed success rate
over SUCCESS_DAYS). A routed pick wins only above `dispatch.min_p`; Jev unavailable, off or unsure: core.

The pick is kept per task (kv PICK) so later starts of the task at the same tier reuse it (another tier's
start or a floor run starts as with dispatch off); a routed option needs the chat provider allowed
(providers_allowed) and not the core provider, as route.target. Each pick is written to each run's note
(KEY: by, pick, p) and, for a Jev call, to jev_calls with the cost it avoided (a routed pick: the
stratum's mean core cost per worker run over AVOID_DAYS). `settle` marks a routed pick right when its
task ends done on its first try with no fallback, wrong when it escalates (a fallback, a retry or a
failure); a core pick changed nothing (jevuse.NO_CHANGE).
"""
from __future__ import annotations

import json
import re
import time

from . import jevuse
from .db import DB

JEV_USE = "model"
KEY = "dispatch"          # the run note's entry
PICK = "model_pick:"      # kv per task: the pick its starts reuse
OPEN_KINDS = ("code", "work", "harness")
UNTRUSTED = "untrusted_input"
MIN_P = 0.6
STATE_CHARS = 2000
SPEC_HEAD = 700
SUCCESS_DAYS = 14
AVOID_DAYS = 7
HARD = re.compile(r"(?i)\b(optimi[sz]\w*|performance|perf|throughput|latency|architect\w*|"
                  r"hard[- ]debug\w*|deadlock|race condition|root[- ]cause|bisect\w*)\b")
PATHS = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.\w+|[\w-]+\.(?:py|md|json|sh|ya?ml|toml|cpp|h|c|rs|js|ts))\b")


def conf(cfg: dict) -> dict:
    d = (((cfg.get("providers") or {}).get("chat") or {}).get("dispatch")) or {}
    return d if isinstance(d, dict) else {}


def options(cfg: dict) -> dict[str, dict]:
    """The configured option keys, each with its provider, model and effort."""
    raw = conf(cfg).get("options") or {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict) and str(v.get("provider") or "")} \
        if isinstance(raw, dict) else {}


def enabled(cfg: dict) -> bool:
    return conf(cfg).get("enabled") is True and any(routed(cfg, k) for k in options(cfg))


def min_p(cfg: dict) -> float:
    try:
        return float(conf(cfg).get("min_p", MIN_P))
    except (TypeError, ValueError):
        return MIN_P


def routed(cfg: dict, key: str) -> bool:
    """Whether option `key` runs on the routed-to (chat) provider, which the project allows and which
    is not its core provider (as route.target)."""
    from .provallow import allows
    from .route import TO
    o = options(cfg).get(key) or {}
    return o.get("provider") == TO and bool(str(o.get("model") or "")) \
        and (cfg.get("core_provider") or "claude") != TO and allows(cfg, TO)


def core_key(cfg: dict) -> str:
    core = cfg.get("core_provider") or "claude"
    return next((k for k, o in options(cfg).items() if o["provider"] == core), "core")


def target(cfg: dict, pick: dict | None) -> dict | None:
    """{provider, model, effort} a routed pick runs on, or None (core)."""
    key = (pick or {}).get("pick") or ""
    if not routed(cfg, key):
        return None
    o = options(cfg)[key]
    return {"provider": o["provider"], "model": str(o["model"]), "effort": str(o.get("effort") or "")}


def run_model(note: dict | None, provider: str, model: str, effort: str) -> tuple[str, str]:
    """(model, effort) a run on `provider` starts with: its routed pick's option when the run note's
    route entry is one (route.KEY with model_pick) to that provider, else the ones given."""
    from .route import KEY as ROUTE
    rt = (note or {}).get(ROUTE)
    if isinstance(rt, dict) and rt.get("model_pick") and rt.get("to") == provider:
        return str(rt.get("model") or model), str(rt.get("pick_effort") or effort)
    return model, effort


def _labels(task: dict) -> list[str]:
    try:
        return [str(x) for x in json.loads(task.get("labels") or "[]")]
    except (TypeError, ValueError):
        return []


def rules(db: DB, cfg: dict, task: dict, tier: str) -> str | None:
    """Why the rules send this start to core, '' when they leave it open, or None when no pick applies
    (a light tier follows its own route; a pin keeps its provider or model)."""
    from . import effort, tierfloor, urgent
    if tier == "light" or str(task.get("provider") or "") or str(task.get("model") or ""):
        return None
    if task.get("kind") not in OPEN_KINDS:
        return "kind"
    if tier != "standard":
        return "tier"
    if int(task.get("attempts") or 0) or db.one("SELECT 1 FROM runs WHERE task=? AND role!='coordinator' LIMIT 1",
                                                (task["id"],)):
        return "retry"
    if effort.device_task(task):
        return "device"
    if urgent.is_urgent(task):
        return "urgent"
    if UNTRUSTED in _labels(task):
        return "untrusted"
    if HARD.search(f"{task.get('title') or ''}\n{task.get('spec') or ''}"):
        return "hard"
    try:
        if tierfloor.reasons(task, cfg):
            return "conclusion"
    except Exception:   # an unreadable floor keeps the task on core
        return "conclusion"
    return ""


def _parents(db: DB, task: dict) -> list[dict]:
    out = []
    for label in _labels(task):
        if label.startswith("continues:") and label[10:].isdigit() and (t := db.task(int(label[10:]))):
            out.append(t)
    return out


def _route_outcomes(db: DB, parents: list[dict]) -> list[str]:
    from . import route
    out = []
    for t in parents:
        for r in db.q("SELECT id, note FROM runs WHERE task=? AND role='worker' ORDER BY id", (t["id"],)):
            e = route.of(r["note"])
            if e:
                out.append(f"#{t['id']} run {r['id']}: {e.get('model')} fallback {e.get('fallback')}, task {t['status']}")
    return out[-6:]


def success_rate(db: DB, kind: str, now: float) -> tuple[int, int]:
    """(tasks of `kind` with a routed run in the last SUCCESS_DAYS that ended done with no fallback,
    such tasks that ended)."""
    from . import route
    seen: dict[int, bool] = {}
    for r in db.q("SELECT r.task, r.note, t.status FROM runs r JOIN tasks t ON t.id=r.task WHERE r.role='worker' "
                  "AND t.kind=? AND r.started>=? AND t.status IN ('done','failed') AND r.note LIKE '%\"route\"%'",
                  (kind, now - SUCCESS_DAYS * 86400)):
        e = route.of(r["note"])
        if e:
            seen[r["task"]] = seen.get(r["task"], True) and e.get("fallback") == "none" and r["status"] == "done"
    return sum(seen.values()), len(seen)


def state(db: DB, task: dict, tier: str, now: float, parent_diff=None) -> str:
    """The redacted text Jev decides on, capped near STATE_CHARS."""
    from .redact import scrub
    spec = task.get("spec") or ""
    paths = list(dict.fromkeys(PATHS.findall(spec)))
    ok, n = success_rate(db, task.get("kind") or "", now)
    parents = _parents(db, task)
    lines = [f"kind: {task.get('kind')}", f"tier: {tier}", f"labels: {', '.join(_labels(task)) or '-'}",
             f"title: {task.get('title') or ''}",
             f"routed success ({SUCCESS_DAYS} d, this kind): {ok}/{n}" if n else "routed success: no data",
             f"paths named: {len(paths)}: {', '.join(paths[:8])}"]
    for t in parents[:2]:
        stat = None
        if parent_diff is not None:
            try:
                stat = parent_diff(t)
            except Exception:   # a diff that cannot be read is left out
                stat = None
        if stat:
            lines.append(f"continues #{t['id']} ({t['status']}): {stat}")
    lines += [f"earlier route: {x}" for x in _route_outcomes(db, parents)]
    head = "\n".join(lines)
    text = f"{head}\nspec:\n{spec[:max(200, min(SPEC_HEAD, STATE_CHARS - len(head) - 7))]}"
    return str(scrub(text))[:STATE_CHARS]


def avoided(db: DB, cfg: dict, task: dict, tier: str, now: float) -> float:
    """The stratum's (kind, tier's model and effort on the core provider) mean core cost per worker run
    over the last AVOID_DAYS."""
    core = cfg.get("core_provider") or "claude"
    t = ((cfg.get("providers") or {}).get(core) or {}).get("tiers", {}).get(tier) or {}
    row = db.one("SELECT AVG(r.cost_usd) c, COUNT(*) n FROM runs r JOIN tasks t ON t.id=r.task WHERE r.role='worker' "
                 "AND r.provider=? AND COALESCE(r.model,'')=? AND t.kind=? AND r.status!='running' AND r.cost_usd>0 "
                 "AND r.started>=?", (core, str(t.get("model") or ""), task.get("kind"), now - AVOID_DAYS * 86400))
    return float(row["c"]) if row and row["n"] else 0.0


def _ask(db: DB, cfg: dict, task: dict, tier: str, jev, now: float, parent_diff) -> dict | None:
    if jev is None or not jev.enabled() or not jevuse.allowed(db, cfg, JEV_USE):
        return None
    opts = options(cfg)
    ans = jev.decide(state(db, task, tier, now, parent_diff), {"model": {
        # The live API takes a choice's options as `criteria` (key: description) and answers `choice` (one key)
        # with `probabilities` per key; a question without `criteria` is refused (HTTP 422).
        "type": "choice", "criteria": {k: f"{o['provider']} {o.get('model') or ''} {o.get('effort') or ''}".strip()
                                       for k, o in opts.items()},
        "instructions": "Which model should run this task? Pick a routed (cheaper) option only when the task is "
                        "routine enough for it to succeed on its first try."}}, purpose="model", timeout=10.0)
    a = (ans or {}).get("model") or {}
    key = str(a.get("choice") or a.get("option") or "")
    if key not in opts:
        return None
    probs = a.get("probabilities") or {}
    try:
        p = float(probs.get(key) if isinstance(probs, dict) and key in probs else a.get("p", a.get("probability")))
    except (TypeError, ValueError):
        p = None
    win = routed(cfg, key) and p is not None and p > min_p(cfg)
    pick = key if win else core_key(cfg)
    cid = jevuse.record(db, JEV_USE, {"pick": pick, "answer": key, "p": p, "routed": win, "kind": task.get("kind"),
                                      "tier": tier}, getattr(jev, "last_cost", 0.0),
                        avoided_usd=avoided(db, cfg, task, tier, now) if win else 0.0,
                        ref=f"task:{task['id']}", changed=win, now=now)
    return {"by": "jev", "pick": pick, "p": p, "jev_call": cid}


def pick(db: DB, cfg: dict, task: dict, tier: str, jev=None, now: float | None = None, parent_diff=None,
         peek: bool = False) -> dict | None:
    """This start's pick ({by, pick, p, ...}), or None when dispatch is off or no pick applies (the
    tier's own route then decides). The first pick is kept for the task's later starts at the tier it
    was picked at (another tier's start gets None); `peek` only reads a kept one and never calls Jev."""
    if not enabled(cfg) or task.get("kind") == "review" or not task.get("id"):
        return None
    kept = db.kv(f"{PICK}{task['id']}")
    if isinstance(kept, dict) and kept.get("tier", "standard") != tier:
        return None   # a light wake or a deep start: its tier's own route decides; the kept pick still settles
    if isinstance(kept, dict):   # a retry runs on core; its kept pick still settles at the end
        return kept if not int(task.get("attempts") or 0) else {"by": "rules", "pick": core_key(cfg), "p": None,
                                                                 "why": "retry"}
    if peek:
        return None
    now = time.time() if now is None else now
    why = rules(db, cfg, task, tier)
    if why is None:
        return None
    got = None
    if not why:
        try:
            got = _ask(db, cfg, task, tier, jev, now, parent_diff)
        except Exception as e:   # picking must never hold a task back: core
            got = {"by": "rules", "pick": core_key(cfg), "p": None, "why": "jev_unavailable", "jev_error": type(e).__name__}
    got = {**(got or {"by": "rules", "pick": core_key(cfg), "p": None, "why": why or "jev_unavailable"}), "tier": tier}
    db.set_kv(f"{PICK}{task['id']}", got)
    return got


def escalated(db: DB, task_id: int) -> int | None:
    """The task's first worker run that did not run on its routed pick (a fallback of any reason), or None."""
    from . import route
    for r in db.q("SELECT id, note FROM runs WHERE task=? AND role='worker' ORDER BY id", (task_id,)):
        e = route.of(r["note"])
        if e and e.get("fallback") != "none":
            return int(r["id"])
    return None


def settle(db: DB, task_id: int, status: str, attempts: int) -> None:
    """Mark the task's Jev model pick once it ends done or failed: a routed pick right when done on its
    first try with no fallback (escalated), wrong otherwise; a core pick changed nothing."""
    if status not in ("done", "failed"):
        return
    for r in db.q("SELECT id, decision FROM jev_calls WHERE use=? AND ref=? AND outcome IS NULL",
                  (JEV_USE, f"task:{task_id}")):
        try:
            win = bool(json.loads(r["decision"] or "{}").get("routed"))
        except (TypeError, ValueError):
            win = False
        if not win:
            jevuse.resolve(db, r["id"], jevuse.NO_CHANGE, "core pick")
            continue
        fell = escalated(db, task_id)
        right = status == "done" and attempts <= 1 and not fell
        jevuse.resolve(db, r["id"], right, f"task {status} after {attempts} attempt(s)"
                                           + (f", fell back at run {fell}" if fell else ""))
    db.x("DELETE FROM kv WHERE key=?", (f"{PICK}{task_id}",))
