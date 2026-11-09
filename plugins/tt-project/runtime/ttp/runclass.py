# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Run classes: which runs need the core model and which may run on the chat provider (route.py).

Every run gets one class, model-free (classify): coordinator; wake_decision (wake gate, wake tier and
babysit decisions: jev_calls rows, DECISION_USES); screening (watcher, log and alert triage: SCREEN_USES);
worker_wake (a waiting task's resume turn); status_report (a question, or a work task whose deliverable
is a status, measurement or report); rebase (a push-conflict fix or a rebase task); review_small (a
review whose diff is under `route.review_small_lines`, SMALL_LINES); light (light tier); hard (the rest:
code, plan, deep, device work, large reviews). The dispatch records it on the run note (KEY); a run
without one is classified the same way at read time from its note.

Routing: `route.classes` {class: "chat" | "core"} (account-level, then the project's value over it)
over DEFAULT: when the chat provider has a model (chat_model), every class in CHAT goes to chat and
coordinator and hard stay on core; the coordinator never moves, whatever the map says. A task's own
provider or model pin wins (route.eligible); a task the rule list keeps on core (route.kept_core: needs_deep,
user_deep, review_fix, untrusted_input) stays on core; urgency is no such reason here (it orders the run and
nothing else: an urgent task's cheap-class run takes the class's cheap route), and so does a task the daemon made
from outside input (untrusted) until the untrusted-input hook guards are live. The model is the run tier's chat
model, else the light tier's, else the standard tier's; picking among models is route.py's and the
failover's job, not this module's. A tier whose `providers.chat.tiers.<tier>.route` is false routes
no class.

Cheap core fallback (core_cheap): a run of a CHAT class that starts on the Claude core provider anyway (no
chat model configured, the chat endpoint cooling, logged out or its key refused, its slots full) runs on a
cheap Claude model at low effort (CORE_CHEAP, under `route.core_cheap` {class: {"model", "effort"} | false},
false for all) instead of its tier's model, so only the coordinator and hard work run on Opus. It applies at
the standard tier (light runs keep the light tier's own cheap model; deep is hard), never to a task class
routing could not move (routable), a model pick, or a class `route.classes` keeps on core. Once the chat
endpoint answers with a working key, the route's fallback is none and the run goes to the tt model again
with no config edit. The run note records it (CHEAP_KEY).

Report: by_class counts runs per class per model per hour with their real cost (`ttp stats --by-class`),
and the share of runs whose class routes to chat. Alarm: quiet_classes names a class meant for cheap
models (routed to chat, or with a cheap core model) that had at least ALARM_MIN runs in the window and none
on chat or a cheap core model, and a wake gate that decided ALARM_MIN times with no chat-provider decision.
tt_unavailable names the classes that ran cheap on Claude with none on chat: the daemon tells it once per
outage, never as a repeating alarm.
"""
from __future__ import annotations

import json
import re
import time

KEY = "run_class"
CLASSES = ("coordinator", "wake_decision", "screening", "worker_wake", "status_report", "rebase",
           "review_small", "light", "hard")
CHAT = ("wake_decision", "screening", "worker_wake", "status_report", "rebase", "review_small", "light")
DECISION_USES = ("wake_gate", "wake_tier", "babysit")
SCREEN_USES = ("screen",)
SMALL_LINES = 300
ALARM_MIN = 5
CHEAP_KEY = "core_cheap"
# Claude models for cheap classes on the core provider while no tt model serves them: decisions and
# screening on Haiku, work that writes or judges code on Sonnet; all at low effort. Light runs keep the
# light tier's own model.
CORE_CHEAP = {"wake_decision": {"model": "haiku", "effort": "low"},
              "screening": {"model": "haiku", "effort": "low"},
              "worker_wake": {"model": "sonnet", "effort": "low"},
              "status_report": {"model": "sonnet", "effort": "low"},
              "rebase": {"model": "sonnet", "effort": "low"},
              "review_small": {"model": "sonnet", "effort": "low"}}
CHEAP_PROVIDER = "claude"
REPORT = re.compile(r"(?i)\b(status|report|measure(?:d|ment|s)?|count|stats|summar(?:y|ise|ize)|digest|brief)\b")


def _note(run: dict | None) -> dict:
    v = (run or {}).get("note")
    try:
        v = json.loads(v) if isinstance(v, str) else v
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _labels(task: dict) -> list[str]:
    from .route import _labels as labels
    return labels(task)


def classify(task: dict | None, run: dict | None = None, cfg: dict | None = None) -> str:
    """The run's class (CLASSES). `run` is its row (role, note) or the note being written; a recorded
    class (KEY) wins. Order: coordinator, hard by structure (deep tier, plan, device
    action), rebase (a conflict-fix note or a rebase label), review (small or hard), wake, light tier,
    status or report, hard."""
    note = _note(run)
    if note.get(KEY) in CLASSES:
        return note[KEY]
    if (run or {}).get("role") == "coordinator":
        return "coordinator"
    if not task:
        return "screening" if (run or {}).get("role") in ("watcher", "screen") else "hard"
    from . import conflictfix, tierfloor
    kind = str(task.get("kind") or "")
    title = str(task.get("title") or "")
    tier = str((note.get("tier") if isinstance(note.get("tier"), str) else "") or task.get("tier") or "")
    # Hard by structure, before any cheaper class: deep work, plans and device actions stay on core.
    if "deep" in (tier, task.get("tier")) or kind == "plan" or tierfloor.device_run(task, cfg) \
            or tierfloor.DEVICE_SPEC.search(str(task.get("spec") or "")):
        return "hard"
    # Rebase by structure only (a push-conflict fix, a rebase label), never by title words.
    if note.get(conflictfix.KEY) or any(lb.startswith("rebase") for lb in _labels(task)):
        return "rebase"
    if kind == "review":
        lines = (note.get("review_tier") or {}).get("lines")
        small = small_lines(cfg)
        return "review_small" if isinstance(lines, (int, float)) and lines < small and task.get("tier") != "deep" \
            else "hard"
    if note.get("wake"):
        return "worker_wake"
    if tier == "light":
        return "light"
    if kind == "question" or (kind == "work" and REPORT.search(title)):
        return "status_report"
    return "hard"


def small_lines(cfg: dict | None) -> int:
    v = ((cfg or {}).get("route") or {}).get("review_small_lines")
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and v > 0 else SMALL_LINES


def chat_model(cfg: dict, tier: str) -> str:
    """The chat provider's model for a class-routed run at `tier`: that tier's, else light's, else
    standard's; '' when chat is the core provider, not allowed or has no model."""
    from .provallow import allows
    from .route import TO
    if (cfg.get("core_provider") or "claude") == TO or not allows(cfg, TO):
        return ""
    tiers = (((cfg.get("providers") or {}).get(TO) or {}).get("tiers") or {})
    if not isinstance(tiers, dict):
        return ""
    return next((str(t["model"]) for t in (tiers.get(tier), tiers.get("light"), tiers.get("standard"))
                 if isinstance(t, dict) and isinstance(t.get("model"), str) and t["model"]), "")


def routes(cfg: dict) -> dict[str, str]:
    """{class: "chat" | "core"}: DEFAULT (CHAT to chat when chat_model has one) under route.classes;
    the coordinator always core."""
    on = bool(chat_model(cfg, "light"))
    out = {c: ("chat" if on and c in CHAT else "core") for c in CLASSES}
    got = ((cfg.get("route") or {}).get("classes") or {})
    if isinstance(got, dict):
        out.update({c: v for c, v in got.items() if c in CLASSES and v in ("chat", "core")})
    out["coordinator"] = "core"
    return out


def target(cfg: dict, task: dict, tier: str, cls: str, picked: dict | None = None) -> dict | None:
    """Where a worker run of class `cls` routes by class: {"to", "model", "rule": "class:<cls>"}, or None
    (core): its class maps to core, no chat model, the task pins a provider or model, the start is a
    tier-up retry, or the task stays on core (route.kept_core)."""
    from .route import TO, kept_core
    if routes(cfg).get(cls) != "chat" or kept_core(task, urgency=False) or untrusted(task) or str(task.get("provider") or "") \
            or str(task.get("model") or "") or (picked or {}).get("by") == "retry":
        return None
    t = ((((cfg.get("providers") or {}).get(TO) or {}).get("tiers") or {}).get(tier) or {})
    if isinstance(t, dict) and t.get("route") is False:
        return None   # the user turned this tier's routing off
    model = chat_model(cfg, tier)
    return {"to": TO, "model": model, "rule": f"class:{cls}"} if model else None


def cheap_models(cfg: dict) -> dict[str, dict]:
    """{class: {"model", "effort"}}: CORE_CHEAP under `route.core_cheap` (a class set to false, or the whole
    key false, has none)."""
    got = (cfg.get("route") or {}).get("core_cheap")
    if got is False:
        return {}
    out = {c: dict(v) for c, v in CORE_CHEAP.items()}
    if isinstance(got, dict):
        for c, v in got.items():
            if c not in CHAT:
                continue
            if v is False:
                out.pop(c, None)
            elif isinstance(v, dict) and isinstance(v.get("model"), str) and v["model"]:
                out[c] = {"model": v["model"], "effort": str(v.get("effort") or "")}
    return out


def core_cheap(cfg: dict, task: dict | None, cls: str, tier: str, provider: str, routed: dict | None = None,
               picked: dict | None = None) -> dict | None:
    """The cheap Claude model a run of class `cls` takes when it starts on `provider` (the core one) at
    `tier`: {"provider", "model", "effort", "class", "why"}, or None (its tier's model): not the Claude core
    provider, not a CHAT class, not the standard tier, the run goes to the chat provider (`routed` with no
    fallback), the task is one routing could not move, or `route.classes` keeps the class on core."""
    if provider != CHEAP_PROVIDER or (cfg.get("core_provider") or "claude") != provider or cls not in CHAT \
            or tier != "standard":
        return None
    if routed and routed.get("to") != provider and routed.get("fallback") == "none":
        return None   # it runs on the chat provider's tt model
    if not routable(task, {"pick": picked or {}}):
        return None
    if ((cfg.get("route") or {}).get("classes") or {}).get(cls) == "core":
        return None
    m = cheap_models(cfg).get(cls)
    if not m:
        return None
    why = str((routed or {}).get("fallback") or "") if routed else "no chat model"
    return {"provider": provider, "model": m["model"], "effort": m["effort"], "class": cls, "why": why or "no chat model"}


def cheap_run(note: dict | None, provider: str) -> dict | None:
    """The run note's cheap core model (CHEAP_KEY) when it applies to a start on `provider`, else None."""
    c = (note or {}).get(CHEAP_KEY)
    return c if isinstance(c, dict) and c.get("provider") == provider and c.get("model") else None


def routable(task: dict | None, note: dict | None = None) -> bool:
    """A run class routing could move (target's task-side exclusions): no provider or model pin, not
    kept on core, no outside input, not a tier-up retry. A run with no task (a watcher) counts."""
    if not task:
        return True
    from .route import kept_core
    return not (kept_core(task, urgency=False) or untrusted(task) or str(task.get("provider") or "") or str(task.get("model") or "")
                or ((note or {}).get("pick") or {}).get("by") == "retry")


def alarm_text(q: dict, hours: float) -> str:
    return (f"Run class {q['class']} is routed to the chat provider but none of its {q['eligible']} eligible runs "
            f"({q['runs']} in all) in the last {hours:g} h ran there; they ran on {', '.join(q['models'])}. "
            f"Last fallback: {q['last_error'] or 'none recorded'}.")


def untrusted(task: dict) -> bool:
    """A task whose run may read untrusted text with tools: labelled untrusted_input, or one the daemon
    made from outside input (PR comments, CI, alerts, notes), a review of this project's own diff
    apart. It stays on core until the untrusted-input hook guards are live."""
    return task.get("origin") == "daemon" and task.get("kind") != "review"


def by_class(db, cfg: dict, since: float, until: float | None = None) -> dict:
    """Runs per class per model per hour, with real cost, from runs and the wake and screening
    decisions (jev_calls) in [since, until); `share`: the part of all of them whose class routes to chat."""
    until = time.time() if until is None else until
    hours = max((until - since) / 3600, 1e-9)
    tasks = {r["id"]: dict(r) for r in db.q(
        "SELECT * FROM tasks WHERE id IN (SELECT task FROM runs WHERE started>=? AND started<?)", (since, until))}
    rows: dict = {}

    eligible: dict = {}
    cheap: dict = {}   # runs per class on a cheap core model (CHEAP_KEY)
    last_error: dict = {}
    from .route import KEY as ROUTE_KEY

    def add(cls: str, model: str, usd: float, ok: bool = True) -> None:
        r = rows.setdefault(cls, {}).setdefault(model or "?", {"runs": 0, "usd": 0.0})
        r["runs"] += 1
        r["usd"] = round(r["usd"] + usd, 4)
        eligible[cls] = eligible.get(cls, 0) + int(ok)
    for r in db.q("SELECT task, role, provider, model, cost_usd, note FROM runs WHERE started>=? AND started<? "
                  "ORDER BY started", (since, until)):
        r = dict(r)
        task, note = tasks.get(r["task"]), _note(r)
        cls = classify(task, r, cfg)
        add(cls, f"{r['provider'] or '?'}:{r['model'] or '?'}", float(r["cost_usd"] or 0), routable(task, note))
        if cheap_run(note, str(r["provider"] or "")):
            cheap[cls] = cheap.get(cls, 0) + 1
        rn = note.get(ROUTE_KEY)
        fell = str(rn.get("fallback") or "") if isinstance(rn, dict) else ""
        if fell and fell != "none":
            last_error[cls] = f"route fallback: {fell}"
    for r in db.q("SELECT use, decision, cost_usd FROM jev_calls WHERE ts>=? AND ts<?", (since, until)):
        use = str(r["use"] or "")
        cls = "wake_decision" if use in DECISION_USES else "screening" if use in SCREEN_USES else None
        if cls:
            add(cls, decider(r["decision"]), float(r["cost_usd"] or 0))
    for r in db.q("SELECT source FROM ledger WHERE ts>=? AND ts<? AND source LIKE 'babysit_fallback:%' "
                  "ORDER BY ts", (since, until)):
        last_error["wake_decision"] = f"chat decision fell back ({r['source']})"
    rt = routes(cfg)
    total = sum(m["runs"] for c in rows.values() for m in c.values())
    chat = sum(m["runs"] for c, ms in rows.items() if rt.get(c) == "chat" for m in ms.values())
    meant = {c for c in CHAT if rt.get(c) == "chat" or c in cheap_models(cfg)}
    return {"since": since, "until": until, "hours": round(hours, 3), "routes": rt, "classes": rows,
            "eligible": eligible, "cheap": cheap, "meant": sorted(meant), "last_error": last_error, "runs": total, "share": round(chat / total, 3) if total else 0.0}


def decider(decision) -> str:
    """Who made a recorded wake or screening decision: its `model` (a chat-provider answer) or jev."""
    try:
        d = json.loads(decision) if isinstance(decision, str) else decision
    except ValueError:
        d = None
    m = str((d or {}).get("model") or "") if isinstance(d, dict) else ""
    return f"chat:{m}" if m else "jev"


def on_chat(model_key: str) -> bool:
    from .route import TO
    return model_key.startswith(f"{TO}:")


def text(s: dict) -> str:
    lines = [f"Runs per class per model, {s['hours']:g} h ({s['runs']} runs; per hour in brackets); "
             f"{s['share']:.0%} in classes routed to chat:"]
    for cls in CLASSES:
        ms = s["classes"].get(cls)
        if not ms:
            continue
        parts = [f"{m} {v['runs']} ({v['runs'] / s['hours']:.1f}/h) ${v['usd']:.2f}"
                 for m, v in sorted(ms.items(), key=lambda kv: -kv[1]["runs"])]
        lines.append(f"  {cls} [{s['routes'].get(cls, 'core')}]: " + "; ".join(parts))
    return "\n".join(lines)


def quiet_classes(s: dict, n: int = ALARM_MIN) -> list[dict]:
    """Classes meant for cheap models (routed to chat, or with a cheap core model: `meant`) with at least
    `n` runs in `s` (by_class) and none on chat or a cheap core model: they fell to the tier's model (Opus).
    The wake gate counts its decisions (wake_decision): none by a chat model is the same miss."""
    out = []
    meant = set(s.get("meant") or [c for c in CHAT if s["routes"].get(c) == "chat"])
    for cls, ms in s["classes"].items():
        if cls not in meant:
            continue
        if s["routes"].get(cls) != "chat":   # meant for a cheap core model only: jev's own decisions use none
            ms = {m: v for m, v in ms.items() if m != "jev"}
        runs = sum(v["runs"] for v in ms.values())
        ok = (s.get("eligible") or {}).get(cls, runs)   # runs class routing would have moved
        if s["routes"].get(cls) != "chat":
            ok = min(ok, runs)   # eligible counts jev's decisions too; they are not this class's runs
        chat = sum(v["runs"] for m, v in ms.items() if on_chat(m)) + (s.get("cheap") or {}).get(cls, 0)
        if ok >= n and not chat:
            out.append({"class": cls, "runs": runs, "eligible": ok, "chat": 0, "models": sorted(ms),
                        "last_error": (s.get("last_error") or {}).get(cls, "")})
    return out


def tt_unavailable(s: dict) -> list[str]:
    """Classes that ran on a cheap core model in `s` (by_class) with no run on the chat provider: no tt model
    served them (none configured, its endpoint down or its key refused)."""
    return sorted(c for c, k in (s.get("cheap") or {}).items()
                  if k and not any(on_chat(m) for m in (s["classes"].get(c) or {})))


def tt_unavailable_text(s: dict, classes: list[str]) -> str:
    models = sorted({m for c in classes for m in (s["classes"].get(c) or {}) if not on_chat(m)})
    errs = sorted({e for c in classes if (e := (s.get("last_error") or {}).get(c))})
    return (f"tt models unavailable: run classes {', '.join(classes)} ran on cheap Claude models "
            f"({', '.join(models)}) at low effort, not on a tt model; they move back by themselves once a tt "
            f"endpoint answers with a working key. Last fallback: {'; '.join(errs) or 'no chat model configured'}.")


def summary(s: dict) -> str:
    """One line for ttp status and the daily review."""
    chat = sum(v["runs"] for ms in s["classes"].values() for m, v in ms.items() if on_chat(m))
    quiet = ", ".join(q["class"] for q in quiet_classes(s))
    return (f"run classes {s['hours']:g} h: {chat} of {s['runs']} on chat, {s['share']:.0%} in chat-routed classes"
            + (f"; routed but none on chat: {quiet}" if quiet else ""))
