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
provider or model pin wins (route.eligible); a task the rule list keeps on core (route.kept_core: urgent,
needs_deep, user_deep, review_fix, untrusted_input) stays on core, and so does a task the daemon made
from outside input (untrusted) until the untrusted-input hook guards are live. The model is the run tier's chat
model, else the light tier's, else the standard tier's; picking among models is route.py's and the
failover's job, not this module's. A tier whose `providers.chat.tiers.<tier>.route` is false routes
no class.

Report: by_class counts runs per class per model per hour with their real cost (`ttp stats --by-class`),
and the share of runs whose class routes to chat. Alarm: quiet_classes names a class routed to chat
that had at least ALARM_MIN runs in the window and none on chat, and a wake gate that decided ALARM_MIN
times with no chat-provider decision.
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
REPORT = re.compile(r"(?i)\b(status|report|measure(?:d|ment|s)?|count|stats|summar(?:y|ise|ize)|digest|brief)\b")
REBASE = re.compile(r"(?i)\b(rebase|merge[- ]conflict|push[- ]conflict)\b")


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
    class (KEY) wins. Order: coordinator, rebase, review (small or hard), device work (hard), wake,
    light tier, status or report, hard."""
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
    if note.get(conflictfix.KEY) or REBASE.search(title) or any(lb.startswith("rebase") for lb in _labels(task)):
        return "rebase"
    if kind == "review":
        lines = (note.get("review_tier") or {}).get("lines")
        small = small_lines(cfg)
        return "review_small" if isinstance(lines, (int, float)) and lines < small and task.get("tier") != "deep" \
            else "hard"
    if tierfloor.device_run(task, cfg):
        return "hard"
    if note.get("wake"):
        return "worker_wake"
    tier = str((note.get("tier") if isinstance(note.get("tier"), str) else "") or task.get("tier") or "")
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
    if routes(cfg).get(cls) != "chat" or kept_core(task) or untrusted(task) or str(task.get("provider") or "") \
            or str(task.get("model") or "") or (picked or {}).get("by") == "retry":
        return None
    t = ((((cfg.get("providers") or {}).get(TO) or {}).get("tiers") or {}).get(tier) or {})
    if isinstance(t, dict) and t.get("route") is False:
        return None   # the user turned this tier's routing off
    model = chat_model(cfg, tier)
    return {"to": TO, "model": model, "rule": f"class:{cls}"} if model else None


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

    def add(cls: str, model: str, usd: float) -> None:
        r = rows.setdefault(cls, {}).setdefault(model or "?", {"runs": 0, "usd": 0.0})
        r["runs"] += 1
        r["usd"] = round(r["usd"] + usd, 4)
    for r in db.q("SELECT task, role, provider, model, cost_usd, note FROM runs WHERE started>=? AND started<?",
                  (since, until)):
        r = dict(r)
        add(classify(tasks.get(r["task"]), r, cfg), f"{r['provider'] or '?'}:{r['model'] or '?'}",
            float(r["cost_usd"] or 0))
    for r in db.q("SELECT use, decision, cost_usd FROM jev_calls WHERE ts>=? AND ts<?", (since, until)):
        use = str(r["use"] or "")
        cls = "wake_decision" if use in DECISION_USES else "screening" if use in SCREEN_USES else None
        if cls:
            add(cls, decider(r["decision"]), float(r["cost_usd"] or 0))
    rt = routes(cfg)
    total = sum(m["runs"] for c in rows.values() for m in c.values())
    chat = sum(m["runs"] for c, ms in rows.items() if rt.get(c) == "chat" for m in ms.values())
    return {"since": since, "until": until, "hours": round(hours, 3), "routes": rt, "classes": rows,
            "runs": total, "share": round(chat / total, 3) if total else 0.0}


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
    """Classes routed to chat with at least `n` runs in `s` (by_class) and none on chat; the wake gate
    counts its decisions (wake_decision): none by a chat model is the same miss."""
    out = []
    for cls, ms in s["classes"].items():
        if s["routes"].get(cls) != "chat":
            continue
        runs = sum(v["runs"] for v in ms.values())
        chat = sum(v["runs"] for m, v in ms.items() if on_chat(m))
        if runs >= n and not chat:
            out.append({"class": cls, "runs": runs, "chat": 0, "models": sorted(ms)})
    return out


def summary(s: dict) -> str:
    """One line for ttp status and the daily review."""
    chat = sum(v["runs"] for ms in s["classes"].values() for m, v in ms.items() if on_chat(m))
    quiet = ", ".join(q["class"] for q in quiet_classes(s))
    return (f"run classes {s['hours']:g} h: {chat} of {s['runs']} on chat, {s['share']:.0%} in chat-routed classes"
            + (f"; routed but none on chat: {quiet}" if quiet else ""))
