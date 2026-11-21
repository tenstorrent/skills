# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Run classes: which runs need the core model and which may run on the chat provider (route.py).

Every run gets one class, model-free (classify): coordinator; wake_decision (wake gate, wake tier and
babysit decisions: jev_calls rows, DECISION_USES); screening (watcher, log and alert triage: SCREEN_USES);
worker_wake (a waiting task's resume turn); status_report (a question, or a work task whose deliverable
is a status, measurement or report); rebase (a push-conflict fix or a rebase task); review_small (a
review whose diff is under `route.review_small_lines`, SMALL_LINES); light (light tier); routine (a work,
question or harness task); hard (the rest: code, plan, deep, device work, every other review). The dispatch records it on the run note (KEY); a run
without one is classified the same way at read time from its note.

Routing: `route.classes` {class: "chat" | "core"} (account-level, then the project's value over it)
over DEFAULT: when the chat provider has a model (chat_model), every class in CHAT goes to chat and
coordinator, review_small, routine and hard stay on core; the coordinator and every review never move,
whatever the map says, and a review on Claude runs on Opus at high effort or more (review_model). A task's own provider or model pin wins (route.eligible); a task the rule list keeps on core (route.kept_core: needs_deep,
user_deep, review_fix, untrusted_input) stays on core; urgency is no such reason here (it orders the run and
nothing else: an urgent task's cheap-class run takes the class's cheap route), and so does a task the daemon made
from outside input (untrusted) until the untrusted-input hook guards are live. The model is the run tier's chat
model, else the light tier's, else the standard tier's; picking among models is route.py's and the
failover's job, not this module's. A tier whose `providers.chat.tiers.<tier>.route` is false routes
no class.

Cheap core fallback (core_cheap): a run of a CHAT class that starts on the Claude core provider anyway (no
chat model configured, the chat endpoint cooling, logged out or its key refused, its slots full) runs on a
cheap Claude model at low effort (CORE_CHEAP, under `route.core_cheap` {class: {"model", "effort"} | false},
false for all) instead of its tier's model, so only the coordinator, reviews and hard work run on Opus. It applies at
the standard tier (light runs keep the light tier's own cheap model; deep is hard), never to a review, a task class
routing could not move (routable), a model pick, or a class `route.classes` keeps on core. Once the chat
endpoint answers with a working key, the route's fallback is none and the run goes to the tt model again
with no config edit. The run note records it (CHEAP_KEY).

Routine (ROUTINE): standard-tier work, question and harness runs that no cheaper class takes run on Sonnet at high effort on the Claude core provider (CORE_CHEAP), never on the chat
provider unless `route.classes` sends them there. Opus keeps code, plan, device, deep and user_deep runs,
needs_deep, urgent and review_fix tasks, tier-up retries and pinned runs (provider, model or effort).
On by default (ROUTINE_MODEL; `route.core_cheap.routine` {"model", "effort"} picks another); off with
`route.classes.routine` "core" or `route.core_cheap.routine` false, when routine runs keep their tier's model. Quality alarm
(routine_quality): when routine's failed and needs_review share over QUALITY_WINDOW_S beats hard's by more
than QUALITY_GAP, with ALARM_MIN runs in each, and the difference is significant (worse_p below ALARM_P), the
daemon raises one run_class_quality event per window.

Code on Sonnet (CODE_KEY): standard-tier code tasks of a CODE_KINDS kind ("new"; "continue": one that
continues another task; "review_fix": one that fixes a failed review, review_fix:<id>) run on Sonnet at high effort (CODE_MODEL) on the Claude core provider when their
kind's quality allows it (code_quality, by the daemon each hour into the kv CODE_STATE_KV): "full" once Sonnet
had CODE_FULL_MIN tasks with a known outcome over CODE_WINDOW_S and its bad share (failed, needs_review, or its
first review asked for changes) is within QUALITY_GAP of Opus's; "off" once it is worse by more, with
ALARM_MIN tasks each, and the difference is significant: worse_p (a one-sided exact binomial test of Sonnet's bad
count against Opus's smoothed bad share) below ALARM_P (`route.code_sonnet.alarm_p`; 0 never moves a kind off), so a
few bad tasks never move a kind (the daemon raises one run_class_quality event when a kind moves back to Opus; the
next hourly recompute brings it back once the difference is no longer significant); else
"canary": a stable CODE_CANARY share of its tasks (by task id; `route.code_sonnet.share`). Each kind has its
own state and alarm. Review fixes take this route though route.kept_core keeps them off every other one, except
a fix after REVIEW_ROUND_MAX failed reviews where the round is known (review_round). Urgent code takes it too
(no Jev pick or daemon-set effort keeps a run off it); needs_deep, user_deep, untrusted_input, pinned (provider,
model or effort: the user's pin), tier-up retries, PR fixes, plan and device runs (tierfloor.device_run, not
device words in the spec) stay on Opus. Each code run's note records the decision (CODE_ROUTE_KEY,
code_decision). Off with `route.code_sonnet` false or `route.core_cheap` false;
`route.code_sonnet.kinds` narrows the kinds. Both `route.code_sonnet` and `route.core_cheap` are project and
account keys (`ttp config <project> route.code_sonnet '{"share": 1.0}'`, `ttp config --account ...`): the
account's value first, the project's own over it. The shipped share stays CODE_CANARY.

Daily guard (guard_run, by the daemon next to the hourly code_quality): once a day while the code route is on,
the code tasks finished in the last day (guard_day). Each reviewed one is attributed to the model and effort of
the code worker run that made the commit the first review (auto_review:<id>) reviewed (the run folder's
result.json `commit` against review_stats.head), else the task's costliest worker run before it; a wake, poll or other
light hop is never picked while another run exists. It trips only on a same-window
comparison: Sonnet at high effort against Opus at high effort, each with at least GUARD_MIN_REVIEWS first reviews
that day, Sonnet's review-fail rate (failed or asked for changes) above Opus's by more than
`route.code_sonnet.guard_pts` points (GUARD_PTS). Fewer on either side: "not enough reviews to compare", never
a trip. Sonnet at any other effort, cheap-class (light) hops and reviews that trace to no worker run stay out of
the comparison and are reported on their own line; the prior GUARD_PRIOR_DAYS days are context only. The row
also has rework hops (review fixes and continuations) per landed change and $ per landed change by worker model
and by role (code worker runs, reviews, review fixes and continuations, device A/B runs, benches and shadow
cells, other). Each day's row is kept in the kv GUARD_DAYS_KV (`ttp stats --code-guard`). A trip sends every
code kind back to Opus (guard_held, over the hourly state) until a person changes `route.code_sonnet`, and the
daemon raises one high-severity event with the
numbers. `route.code_sonnet.guard` false turns the guard off.

Report: by_class counts runs per class per model per hour with their real cost (`ttp stats --by-class`),
and the share of runs whose class routes to chat. Alarm: quiet_classes names a class meant for cheap
models (routed to chat, or with a cheap core model) that had at least ALARM_MIN runs in the window and none
on chat or a cheap core model, and a wake gate that decided ALARM_MIN times with no chat-provider decision.
tt_unavailable names the classes that ran cheap on Claude because their route failed on the chat endpoint,
with none on chat: the daemon tells it once per outage, never as a repeating alarm.
"""
from __future__ import annotations

import json
import math
import os
import re
import time

from .util import task_labels

KEY = "run_class"
CLASSES = ("coordinator", "wake_decision", "screening", "worker_wake", "status_report", "rebase",
           "review_small", "light", "routine", "hard")
# Reviews (review_small and hard) judge work: route.eligible keeps every review on core
# before any class route returns, whatever route.classes says, and core_cheap never moves one (review_model).
CHAT = ("wake_decision", "screening", "worker_wake", "status_report", "rebase", "light")
# The cheap classes an urgent task does not lift to core (urgency sets order only, decision 2026-11-08 23:55
# PST); a light-tier first run is not one of them: an urgent one starts on core.
ORDER_ONLY = tuple(c for c in CHAT if c != "light")
DECISION_USES = ("wake_gate", "wake_tier", "babysit")
SCREEN_USES = ("screen",)
SMALL_LINES = 300
ROUTINE = "routine"
ROUTINE_KINDS = ("work", "question", "harness")
QUALITY_GAP = 0.10
QUALITY_WINDOW_S = 86400.0
ALARM_MIN = 5
ALARM_P = 0.05     # a quality alarm needs the bad-share difference significant at this level (worse_p)
CHEAP_KEY = "core_cheap"
KEPT_CORE = "kept on core"   # core_cheap's why when no route was asked (a keep-core rule, a code arm, no chat model)
# Claude models for cheap classes on the core provider while no tt model serves them: decisions and
# screening on Haiku, work that writes or judges code on Sonnet; all at low effort. Light runs keep the
# light tier's own model.
CORE_CHEAP = {"wake_decision": {"model": "haiku", "effort": "low"},
              "screening": {"model": "haiku", "effort": "low"},
              "worker_wake": {"model": "sonnet", "effort": "low"},
              "status_report": {"model": "sonnet", "effort": "low"},
              "rebase": {"model": "sonnet", "effort": "low"}}
# Routine's model, on by default (`route.core_cheap.routine` false or route.classes.routine "core" turns it off).
ROUTINE_MODEL = {"model": "sonnet", "effort": "high"}
CORE_CHEAP[ROUTINE] = dict(ROUTINE_MODEL)
CHEAP_PROVIDER = "claude"
CODE_KEY = "code"                 # on a cheap note: the code kind that took CODE_MODEL
CODE_KINDS = ("new", "continue", "review_fix")
REVIEW_ROUND_MAX = 2   # a review fix after this many failed reviews of its change stays on Opus
ROUND_LABEL = "review_round:"   # the daemon's fix task: review_round:<failed reviews of the stack so far>
CODE_MODEL = {"model": "sonnet", "effort": "high"}
CODE_EFFORT_MIN = "high"          # a code-kind run on Sonnet never runs below it (code_effort)
CODE_CANARY = 0.2
CODE_FULL_MIN = 30
CODE_WINDOW_S = 7 * 86400.0
CODE_STATE_KV = "code_sonnet_state"
CODE_ROUTE_KEY = "code_route"    # a code run's note: {"to", "why"} (code_decision)
# Why a code run stays on Opus (code_route): the stats and the share alarm count these.
CODE_STAY = ("tier", "device_run", "route_off", "kind_off", "user_pin", "review_round", "pr_fix", "untrusted_input",
             "deep", "retry", "quality_hold", "canary_share", "provider", "core_provider", "device_ab",
             "needs_opus")
# Stays the route keeps by design (the user's pin, untrusted text, device runs, late review rounds, PR fixes, the
# device A/B, a run on another provider): the share alarm leaves them out of its base, so it counts only runs the
# route should have taken. A class-routed run (code_decision's "run_class:<cls>", e.g. a light chat run) is kept too.
CODE_KEEP = ("user_pin", "untrusted_input", "device_run", "device_ab", "review_round", "pr_fix", "provider",
             "needs_opus")


def code_kept(why: str) -> bool:
    """A code-route reason the share alarm leaves out of its base (CODE_KEEP, or a class-routed run)."""
    return why in CODE_KEEP or why.startswith("run_class:")


def _routed_elsewhere(why: dict) -> str:
    """The provider and run_class:<cls> counts, reported on their own line: runs another provider or a lighter
    run class took, which the Sonnet code route could never have taken."""
    got = sorted(((k, v) for k, v in why.items() if k == "provider" or k.startswith("run_class:")),
                 key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{k} {v}" for k, v in got)
SHARE_MIN = 10                     # the share alarm needs this many runs in its window
SHARE_WINDOW_S = 86400.0
SHARE_KV = "route_share_alarm"     # {route: ts of its last event}: one event per route per day
GUARD_KV = "code_sonnet_guard"        # the guard's trip: {"ts", "setting", "row"}; holds every code kind on Opus
GUARD_DAYS_KV = "code_sonnet_guard_days"   # the guard's daily rows, newest last (GUARD_KEEP of them)
GUARD_PTS = 5.0                       # `route.code_sonnet.guard_pts`: the review-fail rise in points that trips it
GUARD_DAY_S = 86400.0
GUARD_PRIOR_DAYS = 7
GUARD_EVERY_S = 23 * 3600.0           # once a day, a little early so the hourly loop's drift never skips a day
GUARD_KEEP = 90
GUARD_MIN_REVIEWS = 15                # first reviews each of Sonnet high and Opus high needs before the guard compares them
_HEX = re.compile(r"[0-9a-f]{7,40}")      # a commit id, as a hand-off or the review records it
GUARD_COMPARED = ("sonnet_high", "opus_high")
GUARD_BUCKETS = (*GUARD_COMPARED, "sonnet_other", "light", "untraced", "other")
GUARD_LABELS = {"sonnet_other": "Sonnet below high", "light": "light hops", "untraced": "untraced (no worker run)",
                "other": "other models or efforts"}
GUARD_ROLES = {"code_worker": "code worker runs", "reviews": "reviews", "rework": "review fixes and continuations",
               "device_ab": "device A/B runs", "bench_shadow": "benches and shadow cells", "other": "other"}
REPORT = re.compile(r"(?i)\b(status|report|measure(?:d|ment|s)?|count|stats|summar(?:y|ise|ize)|digest|brief)\b")


def _note(run: dict | None) -> dict:
    v = (run or {}).get("note")
    try:
        v = json.loads(v) if isinstance(v, str) else v
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


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
    if note.get(conflictfix.KEY) or any(lb.startswith("rebase") for lb in task_labels(task)):
        return "rebase"
    if kind == "review":
        lines = (note.get("review_tier") or {}).get("lines")
        return "review_small" if isinstance(lines, (int, float)) and lines < small_lines(cfg) else "hard"
    if note.get("wake"):
        return "worker_wake"
    if tier == "light":
        return "light"
    if kind == "question" or (kind == "work" and REPORT.search(title)):
        return "status_report"
    return ROUTINE if kind in ROUTINE_KINDS else "hard"


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
    if routes(cfg).get(cls) != "chat" or kept_core(task, urgency=cls not in ORDER_ONLY) or untrusted(task) or str(task.get("provider") or "") \
            or str(task.get("model") or "") or (picked or {}).get("by") == "retry":
        return None
    t = ((((cfg.get("providers") or {}).get(TO) or {}).get("tiers") or {}).get(tier) or {})
    if isinstance(t, dict) and t.get("route") is False:
        return None   # the user turned this tier's routing off
    model = chat_model(cfg, tier)
    return {"to": TO, "model": model, "rule": f"class:{cls}"} if model else None


def cheap_models(cfg: dict) -> dict[str, dict]:
    """{class: {"model", "effort"}}: CORE_CHEAP under `route.core_cheap` (a class set to false, or the whole
    key false, has none; routine true keeps ROUTINE_MODEL)."""
    got = (cfg.get("route") or {}).get("core_cheap")
    if got is False:
        return {}
    out = {c: dict(v) for c, v in CORE_CHEAP.items()}
    if isinstance(got, dict):
        for c, v in got.items():
            if c not in (*CHAT, ROUTINE):
                continue
            if v is False:
                out.pop(c, None)
            elif v is True and c == ROUTINE:
                out[c] = dict(ROUTINE_MODEL)
            elif isinstance(v, dict) and isinstance(v.get("model"), str) and v["model"]:
                out[c] = {"model": v["model"], "effort": str(v.get("effort") or "")}
    return out


def core_cheap(cfg: dict, task: dict | None, cls: str, tier: str, provider: str, routed: dict | None = None,
               picked: dict | None = None, code_state: dict | None = None) -> dict | None:
    """The cheap Claude model a run of class `cls` takes when it starts on `provider` (the core one) at
    `tier`: {"provider", "model", "effort", "class", "why"}, or None (its tier's model): not the Claude core
    provider, a review, not a CHAT class, not the standard tier, the run goes to the chat provider (`routed` with no
    fallback), the task is one routing could not move, or `route.classes` keeps the class on core."""
    if provider != CHEAP_PROVIDER or (cfg.get("core_provider") or "claude") != provider or tier != "standard" \
            or (task or {}).get("kind") == "review":
        return None
    if cls == "hard":
        return code_sonnet(cfg, task, picked, code_state)
    if cls not in (*CHAT, ROUTINE):
        return None
    from .route import kept_core
    if cls == ROUTINE and task and (str(task.get("effort") or "") or kept_core(task)):
        return None   # urgent, needs_deep, user_deep, review_fix or an effort pin: Opus at its tier
    if routed and routed.get("to") != provider and routed.get("fallback") == "none":
        return None   # it runs on the chat provider's tt model
    if not routable(task, {"pick": picked or {}}):
        return None
    if ((cfg.get("route") or {}).get("classes") or {}).get(cls) == "core":
        return None
    m = cheap_models(cfg).get(cls)
    if not m:
        return None
    why = "routine work" if cls == ROUTINE else str((routed or {}).get("fallback") or "")
    return {"provider": provider, "model": m["model"], "effort": code_effort(task, m["model"], m["effort"]),
            "class": cls, "why": why or KEPT_CORE}


def code_effort(task: dict | None, model: str | None, effort: str | None) -> str:
    """The effort a run of `task` on `model` takes: a code-kind task's run on a Sonnet model never runs below
    CODE_EFFORT_MIN (a wake, light-tier, rebase, status, fallback, Jev, continue or review-fix route
    included), else `effort` as given. The cheap classes keep their low effort for every other kind."""
    eff = (effort or "").strip().lower()
    if not task or task.get("kind") != "code" or "sonnet" not in str(model or "").lower():
        return effort or ""
    from .tierfloor import _rank
    return eff if _rank(eff) >= _rank(CODE_EFFORT_MIN) else CODE_EFFORT_MIN


def review_model(model: str | None, effort: str | None) -> tuple[str, str]:
    """A review run on Claude: Opus (or above) at high effort or more (deviceab.REVIEW_MODEL), whatever a
    tier map, a review's sizing, a route or a cheap class picked."""
    from .deviceab import REVIEW_MODEL
    from .tierfloor import _rank
    if not any(f in str(model or "").lower() for f in ("opus", "fable")):
        model = REVIEW_MODEL["model"]
    if _rank(effort) < _rank(REVIEW_MODEL["effort"]):
        effort = REVIEW_MODEL["effort"]
    return str(model), str(effort)


def code_kind(task: dict | None, cfg: dict | None = None) -> str:
    """A standard-tier code task's kind for the Sonnet route: "review_fix", "continue" or "new"; "" for
    anything else (other kinds, other tiers, device work). Device work is a device run (tierfloor.device_run:
    a needs_device label, a device lock or machine in its resources), never device words in its spec: code
    specs name devices in passing ("no device jobs", restated rules, the code under change)."""
    if not task or task.get("kind") != "code" or str(task.get("tier") or "standard") != "standard":
        return ""
    from . import tierfloor
    if tierfloor.device_run(task, cfg):
        return ""
    labels = task_labels(task)
    if any(lb.startswith("review_fix") for lb in labels):
        return "review_fix"
    return "continue" if any(lb.startswith("continues:") for lb in labels) else "new"


def review_round(task: dict | None) -> int:
    """How many failed reviews a review fix answers: its ROUND_LABEL, else one per review_fix:<id> label it
    carries (a fix hop that took over a later round's findings has both); 0 when unknown or not a fix."""
    labels = task_labels(task or {})
    rounds = [int(lb[len(ROUND_LABEL):]) for lb in labels if lb.startswith(ROUND_LABEL) and lb[len(ROUND_LABEL):].isdigit()]
    return max([*rounds, sum(1 for lb in labels if lb.startswith("review_fix:"))])


def code_settings(cfg: dict) -> dict | None:
    """{"share", "kinds"} for the code route, or None when it is off (`route.code_sonnet` or
    `route.core_cheap` false)."""
    r = cfg.get("route") or {}
    got = r.get("code_sonnet")
    if got is False or r.get("core_cheap") is False:
        return None
    got = got if isinstance(got, dict) else {}
    share = got.get("share")
    share = float(share) if isinstance(share, (int, float)) and not isinstance(share, bool) and 0 <= share <= 1 \
        else CODE_CANARY
    kinds = got.get("kinds")
    kinds = tuple(k for k in kinds if k in CODE_KINDS) if isinstance(kinds, list) else CODE_KINDS
    return {"share": share, "kinds": kinds}


def canary(task_id, share: float) -> bool:
    """A stable `share` of task ids: the same task gets the same answer on every run."""
    import hashlib
    return int(hashlib.sha256(f"code-canary:{task_id}".encode()).hexdigest()[:8], 16) % 1000 < share * 1000


def code_sonnet(cfg: dict, task: dict | None, picked: dict | None = None, state: dict | None = None) -> dict | None:
    """The cheap note for a code run that takes CODE_MODEL, else None (Opus): see the module docstring.
    `state`: {kind: "full" | "canary" | "off"} (the daemon's CODE_STATE_KV; a kind missing is canary)."""
    return code_route(cfg, task, picked, state)[0]


def code_route(cfg: dict, task: dict | None, picked: dict | None = None,
               state: dict | None = None) -> tuple[dict | None, str]:
    """(the cheap note, "") for a code run that takes CODE_MODEL, else (None, why it stays on Opus: a
    CODE_STAY reason). Urgency never keeps code on Opus (user-ordered urgent code runs on Sonnet too); only
    a user's pin (the task's provider, model or effort), outside input, deep asks, tier-up retries, late
    review rounds, PR fixes, device runs and the quality state do. An effort the daemon or Jev picks for the
    run (`picked`) is never a pin."""
    task = task or {}
    if task.get("kind") != "code":
        return None, "not_code"
    if str(task.get("tier") or "standard") != "standard":
        return None, "tier"
    kind = code_kind(task, cfg)
    if not kind:
        return None, "device_run"
    on = code_settings(cfg)
    if not on:
        return None, "route_off"
    if kind not in on["kinds"]:
        return None, "kind_off"
    if any(str(task.get(k) or "") for k in ("provider", "model", "effort")):
        return None, "user_pin"
    if (state or {}).get("arm") == "opus":   # a code arm run asked for Opus (codearm.py): the rest stay there
        return None, "needs_opus"
    if kind == "review_fix":   # its review_fix label keeps it off other routes, not this one
        if review_round(task) >= REVIEW_ROUND_MAX:
            return None, "review_round"   # a late round stays on Opus
        if any(lb.startswith("pr_fix:") for lb in task_labels(task)):
            return None, "pr_fix"   # a PR fix reads PR comments
        # the daemon made it from a review of this project's own diff, which untrusted() trusts; an
        # untrusted_input label the fixed work passed on still keeps it on Opus
        task = {**task, "origin": "review", "labels": [lb for lb in task_labels(task) if not lb.startswith("review_fix")]}
    from . import effort
    from .route import _has
    if _has(task_labels(task), ("untrusted_input",)) or untrusted(task):
        return None, "untrusted_input"
    if _has(task_labels(task), ("needs_deep", "user_deep")) or effort.wants_deep(task.get("result")):
        return None, "deep"
    if (picked or {}).get("by") == "retry":
        return None, "retry"
    mode = (state or {}).get(kind) or "canary"
    if mode == "off":
        return None, "quality_hold"
    if mode != "full" and not canary(task.get("id"), on["share"]):
        return None, "canary_share"
    return ({"provider": CHEAP_PROVIDER, **CODE_MODEL, "class": "hard", CODE_KEY: kind,
             "why": f"code {kind} on Sonnet ({'within quality of Opus' if mode == 'full' else 'canary'})"}, "")


def code_decision(cfg: dict, task: dict | None, cls: str, tier: str, provider: str, picked: dict | None = None,
                  state: dict | None = None, cheap: dict | None = None, device_ab: dict | None = None) -> dict | None:
    """What a code-kind worker run's note records under CODE_ROUTE_KEY: {"to": "sonnet"} when it takes
    CODE_MODEL, else {"to": "opus" (or the provider it runs on), "why": a CODE_STAY reason}; None for a run of
    another kind. `cheap` is the cheap note the daemon set (core_cheap), `device_ab` the device A/B arm."""
    if not task or task.get("kind") != "code":
        return None
    if device_ab:
        return {"to": str((cheap or {}).get("model") or "opus"), "why": "device_ab"}
    if cheap and cheap.get(CODE_KEY):
        return {"to": "sonnet", "kind": cheap[CODE_KEY]}
    if provider != CHEAP_PROVIDER:
        return {"to": provider, "why": "provider"}
    if cls != "hard":
        return {"to": "opus" if not cheap else str(cheap.get("model") or "opus"), "why": f"run_class:{cls}"}
    if tier != "standard" and str(task.get("tier") or "standard") == "standard":
        return {"to": "opus", "why": "tier"}
    why = code_route(cfg, task, picked, state)[1]
    if why == "" and (cfg.get("core_provider") or "claude") != CHEAP_PROVIDER:
        why = "core_provider"
    return {"to": "opus", "why": why or "unknown"}


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
    return not (kept_core(task, urgency=(note or {}).get(KEY) not in (None, *ORDER_ONLY)) or untrusted(task) or str(task.get("provider") or "") or str(task.get("model") or "")
                or ((note or {}).get("pick") or {}).get("by") == "retry")


def alarm_text(q: dict, hours: float) -> str:
    return (f"Run class {q['class']} is routed to the chat provider but none of its {q['eligible']} eligible runs "
            f"({q['runs']} in all) in the last {hours:g} h ran there; they ran on {', '.join(q['models'])}. "
            f"Last fallback: {q['last_error'] or 'none recorded'}.")


def worse_p(bad: int, tasks: int, base_bad: int, base_tasks: int) -> float:
    """One-sided exact binomial test: the chance of `bad` or more bad out of `tasks` at the baseline's bad
    share. The baseline share is smoothed by one good and one bad task ((base_bad + 1) / (base_tasks + 2)), so a
    clean small baseline never makes one bad task significant. Pure Python; 1.0 with no tasks."""
    if tasks <= 0 or bad <= 0:
        return 1.0
    p0 = (max(base_bad, 0) + 1) / (max(base_tasks, 0) + 2)
    return min(1.0, sum(math.comb(tasks, i) * p0 ** i * (1 - p0) ** (tasks - i) for i in range(bad, tasks + 1)))


def alarm_p(cfg: dict | None) -> float:
    """The significance level the code route's quality alarm needs (`route.code_sonnet.alarm_p`, 0..1; 0 never
    moves a kind off), else ALARM_P."""
    got = ((cfg or {}).get("route") or {}).get("code_sonnet")
    got = got.get("alarm_p") if isinstance(got, dict) else None
    return float(got) if isinstance(got, (int, float)) and not isinstance(got, bool) and 0 <= got <= 1 else ALARM_P


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
    cheap_down: dict = {}   # those whose route failed on the endpoint (routed_fell)
    last_error: dict = {}
    from .route import KEY as ROUTE_KEY, routed_fell

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
        rn = note.get(ROUTE_KEY)
        fell = str(rn.get("fallback") or "") if isinstance(rn, dict) else ""
        if (cn := cheap_run(note, str(r["provider"] or ""))) and not cn.get(CODE_KEY):
            cheap[cls] = cheap.get(cls, 0) + 1
            cheap_down[cls] = cheap_down.get(cls, 0) + int(routed_fell(fell))
        if routed_fell(fell):
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
            "eligible": eligible, "cheap": cheap, "cheap_down": cheap_down, "meant": sorted(meant), "last_error": last_error, "runs": total, "share": round(chat / total, 3) if total else 0.0}


def routine_quality(db, cfg: dict, since: float, until: float | None = None) -> dict:
    """Routine vs hard outcomes in [since, until), per task by its last worker run there: {"routine", "hard":
    {"tasks", "bad", "share"}, "gap", "alarm"}. Routine counts runs that took the routine cheap model; hard,
    runs of class hard on their tier's model. Bad: the run failed, or the task ended failed or with a
    needs_review hand-off. `alarm`: ALARM_MIN tasks in each, routine's bad share above hard's by more
    than QUALITY_GAP, and that significant (`p`, worse_p, below ALARM_P)."""
    until = time.time() if until is None else until
    last: dict = {}
    for r in db.q("SELECT task, role, provider, status, note FROM runs WHERE role='worker' AND task IS NOT NULL "
                  "AND started>=? AND started<? AND status!='running' ORDER BY started", (since, until)):
        last[r["task"]] = dict(r)
    tasks = {t["id"]: dict(t) for t in db.q(
        f"SELECT * FROM tasks WHERE id IN ({','.join('?' * len(last))})", tuple(last))} if last else {}
    out = {c: {"tasks": 0, "bad": 0, "share": 0.0} for c in (ROUTINE, "hard")}
    for tid, r in last.items():
        task, note = tasks.get(tid), _note(r)
        cheap = cheap_run(note, str(r["provider"] or ""))
        cls = ROUTINE if (cheap or {}).get("class") == ROUTINE else "hard" if not cheap and \
            classify(task, r, cfg) == "hard" else None
        if not cls or not task:
            continue
        try:
            got = json.loads(task.get("result") or "{}")
        except (TypeError, ValueError):
            got = {}
        bad = r["status"] == "failed" or task.get("status") == "failed" or \
            (isinstance(got, dict) and got.get("status") in ("failed", "needs_review"))
        out[cls]["tasks"] += 1
        out[cls]["bad"] += int(bad)
    for v in out.values():
        v["share"] = round(v["bad"] / v["tasks"], 3) if v["tasks"] else 0.0
    gap = round(out[ROUTINE]["share"] - out["hard"]["share"], 3)
    p = worse_p(out[ROUTINE]["bad"], out[ROUTINE]["tasks"], out["hard"]["bad"], out["hard"]["tasks"])
    return {**out, "gap": gap, "p": round(p, 4), "alarm": min(out[ROUTINE]["tasks"], out["hard"]["tasks"]) >= ALARM_MIN
            and gap > QUALITY_GAP and p < ALARM_P}


def quality_text(q: dict, hours: float) -> str:
    r, h = q[ROUTINE], q["hard"]
    return (f"Routine runs (Sonnet, high effort) went wrong more often than hard ones (Opus) in the last {hours:g} h: "
            f"{r['bad']}/{r['tasks']} tasks failed or needs_review ({r['share']:.0%}) vs {h['bad']}/{h['tasks']} "
            f"({h['share']:.0%}; p={q.get('p', 1.0):.3g}, one-sided exact binomial). Check them; "
            f"`route.classes.routine` \"core\" moves routine work back to Opus.")


def code_quality(db, cfg: dict, since: float, until: float | None = None) -> dict:
    """Code tasks on Sonnet (CODE_KEY) vs on Opus per kind in [since, until), each task by its first worker run
    there (the model it started on, so a failed try retried a tier up stays on the side that failed) and counted
    once its outcome is known: {kind: {"sonnet", "opus": {"tasks", "bad", "share"}, "gap", "state"}}. Bad: that
    run or the task failed, a retry raised its tier, a needs_review or failed hand-off, or its first review
    (auto_review:<id>) asked for changes. "p": worse_p of Sonnet's bad count against Opus's share. "state": "off"
    (ALARM_MIN each, Sonnet worse by more than QUALITY_GAP and p below alarm_p(cfg)), "full" (CODE_FULL_MIN Sonnet
    tasks, ALARM_MIN Opus ones, within QUALITY_GAP) or "canary". Recomputed from the window alone, so a kind is
    "off" only while its window's difference stays significant."""
    from .effort import RAISED
    until = time.time() if until is None else until
    level = alarm_p(cfg)
    last: dict = {}
    for r in db.q("SELECT task, provider, model, status, note FROM runs WHERE role='worker' AND task IS NOT NULL "
                  "AND started>=? AND started<? AND status!='running' ORDER BY started", (since, until)):
        last.setdefault(r["task"], dict(r))   # its first run
    tasks = {t["id"]: dict(t) for t in db.q(
        f"SELECT * FROM tasks WHERE kind='code' AND id IN ({','.join('?' * len(last))})", tuple(last))} if last else {}
    first: dict = {}
    if tasks:
        for r in db.q("SELECT id, status, labels FROM tasks WHERE kind='review' AND status IN ('done','failed') "
                      "AND created>=? ORDER BY id", (since,)):
            for lb in task_labels(dict(r)):
                if lb.startswith("auto_review:") and lb[12:].isdigit():
                    first.setdefault(int(lb[12:]), r["status"])
    out = {k: {m: {"tasks": 0, "bad": 0, "share": 0.0} for m in ("sonnet", "opus")} for k in CODE_KINDS}
    for tid, task in tasks.items():
        r = last[tid]
        raised = task.get("tier_source") == "retry_tier_up"
        if raised:   # judged at the tier it started at, not the one its retry raised it to
            task = {**task, "tier": {"deep": "standard", "standard": "light"}.get(str(task.get("tier")), "")}
        cheap = cheap_run(_note(r), str(r["provider"] or ""))
        side = "sonnet" if (cheap or {}).get(CODE_KEY) else "opus" if not cheap and \
            "opus" in str(r["model"] or "") else None
        kind = cheap[CODE_KEY] if side == "sonnet" else code_kind(task, cfg)
        if not side or kind not in out:
            continue
        try:
            got = json.loads(task.get("result") or "{}")
        except (TypeError, ValueError):
            got = {}
        got = got if isinstance(got, dict) else {}
        bad = r["status"] == "failed" or task.get("status") == "failed" or raised or bool(got.get(RAISED)) \
            or got.get("status") in ("failed", "needs_review") or first.get(tid) == "failed"
        if not bad and tid not in first:
            continue   # outcome not known yet: no review verdict
        out[kind][side]["tasks"] += 1
        out[kind][side]["bad"] += int(bad)
    for k, v in out.items():
        for m in ("sonnet", "opus"):
            v[m]["share"] = round(v[m]["bad"] / v[m]["tasks"], 3) if v[m]["tasks"] else 0.0
        v["gap"] = round(v["sonnet"]["share"] - v["opus"]["share"], 3)
        p = worse_p(v["sonnet"]["bad"], v["sonnet"]["tasks"], v["opus"]["bad"], v["opus"]["tasks"])
        v["p"] = round(p, 4)
        enough = v["opus"]["tasks"] >= ALARM_MIN
        v["state"] = "off" if enough and v["sonnet"]["tasks"] >= ALARM_MIN and v["gap"] > QUALITY_GAP \
            and p < level else \
            "full" if enough and v["sonnet"]["tasks"] >= CODE_FULL_MIN else "canary"
    return out


def code_quality_text(kind: str, v: dict, hours: float) -> str:
    s, o = v["sonnet"], v["opus"]
    return (f"Code tasks ({kind}) on Sonnet went wrong more often than on Opus in the last {hours:g} h: "
            f"{s['bad']}/{s['tasks']} failed, needs_review or changes asked at first review ({s['share']:.0%}) vs "
            f"{o['bad']}/{o['tasks']} ({o['share']:.0%}); p={v.get('p', 1.0):.3g} (one-sided exact binomial, "
            f"significant). New {kind} code tasks run on Opus again; they go back to Sonnet by themselves once the "
            f"difference stops being significant, or at the next hourly check with \"alarm_p\": 0 added to "
            f"`route.code_sonnet`; `route.code_sonnet` false turns the Sonnet route off.")


def guard_settings(cfg: dict) -> dict | None:
    """{"pts"} for the daily code-route guard, or None when it is off (the code route off, or
    `route.code_sonnet.guard` false)."""
    if not code_settings(cfg):
        return None
    got = (cfg.get("route") or {}).get("code_sonnet")
    got = got if isinstance(got, dict) else {}
    if got.get("guard") is False:
        return None
    pts = got.get("guard_pts")
    pts = float(pts) if isinstance(pts, (int, float)) and not isinstance(pts, bool) and pts >= 0 else GUARD_PTS
    return {"pts": pts}


def setting_mark(cfg: dict) -> str:
    """The effective `route.code_sonnet` value as a stable string: a trip holds while it is unchanged."""
    return json.dumps((cfg.get("route") or {}).get("code_sonnet"), sort_keys=True, default=str)


def guard_held(db, cfg: dict) -> bool:
    """Whether a guard trip holds every code kind on Opus: one is recorded and `route.code_sonnet` is
    unchanged since (a person's change ends it)."""
    got = db.kv(GUARD_KV)
    return isinstance(got, dict) and bool(got.get("ts")) and got.get("setting") == setting_mark(cfg)


def _family(model: str | None) -> str:
    m = str(model or "").lower()
    return "sonnet" if "sonnet" in m else "opus" if "opus" in m else "other"


def _hex_in(v) -> str:
    """The commit id a hand-off value names, lowercase: the value itself, or the leading word of a line such as
    `abc1234 fix the thing`; '' when it names none."""
    w = str(v or "").strip().lower().split(None, 1)
    return w[0] if w and _HEX.fullmatch(w[0]) else ""


def _run_commit(run: dict) -> str:
    """The commit the run left, lowercase: the head its task branch pointed at as the run ended (`runs.head`,
    recorded by the daemon), else what its own hand-off (`result.json` in its run folder) names: `commit`, `head`,
    `sha` or `work_commit`, else the last of `commits` (a list of ids or lines led by one); '' when none."""
    c = _hex_in(run.get("head"))
    if c:
        return c
    d = str(run.get("dir") or "")
    if not d:
        return ""
    try:
        with open(os.path.join(d, "result.json"), encoding="utf-8") as f:
            res = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(res, dict):
        return ""
    for k in ("commit", "head", "sha", "work_commit"):
        if c := _hex_in(res.get(k)):
            return c
    cs = res.get("commits")
    for v in reversed(cs if isinstance(cs, list) else []):
        if c := _hex_in(v):
            return c
    return ""


def _attribute(runs: list[dict], task: dict, head: str = "") -> tuple[dict | None, str]:
    """The worker run a first review counts under, and how it was found: (run, "head") the earliest run whose
    hand-off names the commit the review reviewed (`head`, review_stats.head: a prefix of at least 7 hex chars
    on either side matches); else (run, "costliest") the run with the highest cost; (None, "") for no run.
    `runs`: the task's worker runs started before the review. Cheap-class runs (wake, poll and other light
    hops: classify in CHAT) are never picked while any other run exists; they are chosen among only when
    they are all there is."""
    if not runs:
        return None, ""
    pool = [r for r in runs if classify(task, r) not in CHAT] or runs
    head = (head or "").strip().lower()
    if _HEX.fullmatch(head):
        for r in sorted(pool, key=lambda r: (float(r.get("started") or 0), r.get("id") or 0)):
            c = _run_commit(r)
            if c and (c.startswith(head) or head.startswith(c)):
                return r, "head"
    return max(pool, key=lambda r: (float(r.get("cost_usd") or 0), float(r.get("started") or 0), r.get("id") or 0)), "costliest"


def _bucket(runs: list[dict], task: dict, head: str = "") -> tuple[str, str]:
    """(bucket, how) where a reviewed code task's first review counts: the model and effort of the code worker
    run _attribute picks, as GUARD_BUCKETS name them; how is "head", "costliest" or "" (no run: untraced).
    Sonnet at another effort than high comes before the light-hop check, so a Sonnet low run shows as such."""
    last, how = _attribute(runs, task, head)
    if last is None:
        return "untraced", ""
    fam, eff = _family(last.get("model")), str(last.get("effort") or "").strip().lower()
    if fam == "sonnet" and eff != "high":
        return "sonnet_other", how
    if classify(task, last) in CHAT:
        return "light", how
    return ({"sonnet": "sonnet_high", "opus": "opus_high"}[fam] if fam != "other" and eff == "high" else "other"), how


def _split(tally: dict) -> dict:
    n, f = tally["reviewed"], tally["review_fails"]
    return {"reviewed": n, "review_fails": f, "fail_rate": round(f / n, 4) if n else None}


def _per(d: dict, n: int) -> dict:
    return {k: round(v / n, 2) for k, v in sorted(d.items())} if n else {}


def guard_day(db, since: float, until: float) -> dict:
    """Code tasks finished (done or failed) in [since, until): {"tasks", "reviewed", "review_fails", "fail_rate",
    "landed", "hops", "hops_per_landed", "usd", "usd_per_landed", "models", "excluded", "attributed",
    "usd_by_role", "usd_by_model"}. Reviewed: a first review (the lowest auto_review:<id> review, done or failed)
    has its verdict; failed means it asked for changes. `models` counts those first reviews by the code worker
    run that made the reviewed commit, else the costliest one (_bucket; `attributed` counts each way): sonnet_high and opus_high, the only ones the guard compares, each {"reviewed",
    "review_fails", "fail_rate"}; `excluded` has the rest, never compared: sonnet_other (Sonnet at any effort
    but high), light (the chosen run was a cheap-class hop, the only run there was), untraced (no worker run before the review) and
    other (another model or effort). Landed changes are the lineages (chaincap.chain, root first) of these
    tasks whose root landed (landed.recorded, which follows the hops that carry it on); hops are a lineage's
    review fixes and continuations. Its $ is every run of its tasks and of the reviews, device A/B and shadow
    tasks that name them, split `usd_by_role` (GUARD_ROLES) and `usd_by_model` (model:effort), both per landed
    change."""
    from . import chaincap, landed
    tasks = [dict(t) for t in db.q("SELECT * FROM tasks WHERE kind='code' AND status IN ('done','failed') "
                                   "AND updated>=? AND updated<? ORDER BY id", (since, until))]
    by_id = {t["id"]: t for t in tasks}
    first: dict = {}
    first_at: dict = {}
    first_rid: dict = {}
    linked: dict = {}   # review-kind task id -> (the code task it names, its GUARD_ROLES role)
    if by_id:
        for r in db.q("SELECT id, status, labels, created FROM tasks WHERE kind='review' ORDER BY id"):
            for lb in task_labels(dict(r)):
                pre, _, rest = lb.partition(":")
                num = rest.split(":")[0]
                if not num.isdigit():
                    continue
                role = {"auto_review": "reviews", "device_ab_review": "device_ab", "device_ab_review2": "device_ab",
                        "device_shadow_review": "bench_shadow", "device_shadow_review2": "bench_shadow"}.get(pre)
                if role:
                    linked.setdefault(r["id"], (int(num), role))
                if pre == "auto_review" and int(num) in by_id and r["status"] in ("done", "failed") \
                        and int(num) not in first:
                    first[int(num)] = r["status"]
                    first_at[int(num)] = float(r["created"] or "inf")
                    first_rid[int(num)] = r["id"]
    buckets = {b: {"reviewed": 0, "review_fails": 0} for b in GUARD_BUCKETS}
    how_n = {"head": 0, "costliest": 0}
    for tid, verdict in first.items():
        runs = [dict(r) for r in db.q("SELECT id, model, effort, note, dir, started, cost_usd, head FROM runs WHERE task=? "
                                      "AND role='worker' AND started<? ORDER BY started DESC, id DESC",
                                      (tid, first_at[tid]))]
        rs = db.one("SELECT head FROM review_stats WHERE task=?", (first_rid[tid],))
        reviewed_head = (rs["head"] if rs else "") or ""
        if not reviewed_head:
            q = db.one("SELECT head FROM push_queue WHERE task=? AND head IS NOT NULL AND head != '' "
                       "ORDER BY id DESC LIMIT 1", (first_rid[tid],))
            reviewed_head = (q["head"] if q else "") or ""
        name, how = _bucket(runs, by_id[tid], reviewed_head)
        b = buckets[name]
        if how:
            how_n[how] += 1
        b["reviewed"] += 1
        b["review_fails"] += verdict == "failed"
    lines: dict = {}
    for t in tasks:
        ch = chaincap.chain(db, t["id"]) or [t]
        root = ch[0]["id"]
        if len(ch) >= len(lines.get(root) or []):
            lines[root] = ch
    landed_lines = [ch for root, ch in lines.items() if landed.recorded(db, root)[0]]
    role_usd: dict = {}
    model_usd: dict = {}

    def add(task_id, role, spent_usd, own_role=None):
        runs = [dict(r) for r in db.q("SELECT role, model, effort, cost_usd FROM runs WHERE task=?", (task_id,))]
        for r in runs or [{"role": own_role, "model": "", "effort": "", "cost_usd": spent_usd}]:
            usd = float(r["cost_usd"] or 0)
            r_role = role if own_role is None or r["role"] in (own_role, None) else "other"
            role_usd[r_role] = role_usd.get(r_role, 0.0) + usd
            key = f"{_family(r['model'])}:{str(r['effort'] or '').lower()}" if r["model"] else "unrecorded"
            model_usd[key] = model_usd.get(key, 0.0) + usd

    for ch in landed_lines:
        ids = {t["id"] for t in ch}
        for i, t in enumerate(ch):
            ab = any(lb.startswith("device_ab:") for lb in task_labels(t))
            add(t["id"], "device_ab" if ab else "code_worker" if i == 0 else "rework", t.get("spent_usd"), "worker")
        for rid, (tid, role) in linked.items():
            if tid in ids:
                t = db.task(rid) or {}
                add(rid, role, t.get("spent_usd"))
    usd = sum(role_usd.values())
    hops = sum(len(ch) - 1 for ch in landed_lines)
    n = len(landed_lines)
    reviewed = sum(b["reviewed"] for b in buckets.values())
    fails = sum(b["review_fails"] for b in buckets.values())
    return {"tasks": len(tasks), "reviewed": reviewed, "review_fails": fails,
            "fail_rate": round(fails / reviewed, 4) if reviewed else None,
            "attributed": how_n,
            "models": {b: _split(buckets[b]) for b in GUARD_COMPARED},
            "excluded": {b: _split(v) for b, v in buckets.items() if b not in GUARD_COMPARED},
            "landed": n, "hops": hops, "hops_per_landed": round(hops / n, 2) if n else None,
            "usd": round(usd, 2), "usd_per_landed": round(usd / n, 2) if n else None,
            "usd_by_role": _per(role_usd, n), "usd_by_model": _per(model_usd, n)}


def guard_check(db, cfg: dict, now: float | None = None) -> dict:
    """The guard's row for the day ending at `now`: {"ts", "day", "prior", "pts", "min_reviews", "enough",
    "gap_pts", "comparison", "tripped"}. It compares Sonnet high with Opus high over the same day (their first
    reviews' fail rates, by the worker's model: guard_day) and trips when each has GUARD_MIN_REVIEWS of them and
    Sonnet's rate is above Opus's by more than `pts` points. Fewer on either side: comparison says "not enough
    reviews to compare" and it never trips. The prior days are context, never the comparator."""
    now = time.time() if now is None else now
    pts = (guard_settings(cfg) or {}).get("pts", GUARD_PTS)
    trip = db.kv(GUARD_KV)   # the reviews that tripped it never count again once the setting changes (re-armed)
    day = guard_day(db, max(now - GUARD_DAY_S, float(trip.get("ts") or 0) if isinstance(trip, dict) else 0), now)
    prior = guard_day(db, now - (1 + GUARD_PRIOR_DAYS) * GUARD_DAY_S, now - GUARD_DAY_S)
    s, o = day["models"]["sonnet_high"], day["models"]["opus_high"]
    enough = min(s["reviewed"], o["reviewed"]) >= GUARD_MIN_REVIEWS
    gap = round((s["fail_rate"] - o["fail_rate"]) * 100, 1) if enough else None
    return {"ts": now, "day": day, "prior": prior, "pts": pts, "min_reviews": GUARD_MIN_REVIEWS, "enough": enough,
            "gap_pts": gap, "comparison": "compared" if enough else "not enough reviews to compare",
            "tripped": bool(enough and gap > pts)}


def guard_run(db, cfg: dict, now: float | None = None) -> dict | None:
    """The daemon's daily guard: None when it is off or ran under GUARD_EVERY_S ago; else today's row, kept in
    GUARD_DAYS_KV. A trip while none holds records GUARD_KV with the current `route.code_sonnet` and comes back
    with "new_trip" true: the caller raises the one alert."""
    now = time.time() if now is None else now
    if not guard_settings(cfg):
        return None
    rows = db.kv(GUARD_DAYS_KV)
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if rows and now - float(rows[-1].get("ts") or 0) < GUARD_EVERY_S:
        return None
    row = guard_check(db, cfg, now)
    row["held"] = guard_held(db, cfg)
    db.set_kv(GUARD_DAYS_KV, (rows + [row])[-GUARD_KEEP:])
    if row["tripped"] and not row["held"]:
        db.set_kv(GUARD_KV, {"ts": now, "setting": setting_mark(cfg), "row": row})
        return {**row, "new_trip": True}
    return row


def _rate(v: dict | None) -> str:
    v = v or {}
    n = v.get("reviewed") or 0
    return f"{v.get('review_fails') or 0}/{n} ({v['fail_rate']:.0%})" if n else "0/0 (n/a)"


def _usd(d: dict | None) -> str:
    return ", ".join(f"{k} {v:.2f}" for k, v in (d or {}).items() if v) or "n/a"


def guard_lines(row: dict) -> list[str]:
    """One guard row as short lines: the same-window Sonnet high / Opus high comparison, what it left out, the
    prior days as context, rework hops and $ per landed code task by worker model and by role. A row kept by
    an older guard (no `models`) shows its own review-fail numbers."""
    d, p = row["day"], row.get("prior") or {}
    if "models" not in d:
        return [f"review fails {d['review_fails']}/{d['reviewed']} vs prior {p.get('review_fails')}/{p.get('reviewed')} "
                f"(older guard: all models together, not compared by model)"]
    m, x = d["models"], d.get("excluded") or {}
    gap = row.get("gap_pts")
    head = (f"Sonnet high review fails {_rate(m['sonnet_high'])} vs Opus high {_rate(m['opus_high'])}, gap "
            f"{gap:g} pts (limit {row['pts']:g})" if row.get("enough") else
            f"Sonnet high review fails {_rate(m['sonnet_high'])}, Opus high {_rate(m['opus_high'])}: "
            f"not enough reviews to compare (needs {row.get('min_reviews', GUARD_MIN_REVIEWS)} on each side)")
    out = [head]
    out.append("left out of the comparison: " + ", ".join(
        f"{label} {v['reviewed']} reviewed, {v['review_fails']} failed"
        for label, v in ((GUARD_LABELS[k], x.get(k) or {"reviewed": 0, "review_fails": 0}) for k in x)))
    pm = (p.get("models") or {})
    if pm:
        out.append(f"prior {GUARD_PRIOR_DAYS} days (context only): Sonnet high {_rate(pm.get('sonnet_high'))}, "
                   f"Opus high {_rate(pm.get('opus_high'))}")
    hops = d.get("hops_per_landed")
    out.append(f"rework hops per landed change {'n/a' if hops is None else format(hops, 'g')}; $ per landed code task "
               f"{'n/a' if d.get('usd_per_landed') is None else format(d['usd_per_landed'], '.2f')} "
               f"({d['landed']} landed)")
    if d.get("attributed") is not None:
        a = d["attributed"]
        out.append(f"worker run found by head commit for {a.get('head', 0)} reviews, by costliest run for "
                   f"{a.get('costliest', 0)}")
    out.append("$ per landed by worker model: " + _usd(d.get("usd_by_model")))
    out.append("$ per landed by role: " + _usd({GUARD_ROLES.get(k, k): v for k, v in (d.get("usd_by_role") or {}).items()}))
    return out


def guard_line(row: dict) -> str:
    return "; ".join(guard_lines(row))


def guard_text(row: dict) -> str:
    """The trip's alert."""
    return (f"Code-route guard tripped: in the same day, Sonnet at high effort failed its first review more often than "
            f"Opus at high effort by more than {row['pts']:g} points, with at least {row['min_reviews']} reviews each. "
            f"{guard_line(row)}. Every code kind runs on Opus again and stays there "
            f"until `route.code_sonnet` is changed (`ttp stats --code-guard` shows the daily numbers).")


def guard_report(db, cfg: dict, days: int = 14) -> dict:
    """`ttp stats --code-guard`: the guard's settings, its hold and its last `days` daily rows."""
    rows = db.kv(GUARD_DAYS_KV)
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    trip = db.kv(GUARD_KV)
    return {"guard": guard_settings(cfg), "share": (code_settings(cfg) or {}).get("share"),
            "held": guard_held(db, cfg), "trip": trip if isinstance(trip, dict) else None,
            "rows": rows[-max(1, days):]}


def guard_report_text(s: dict, name: str, tz=None) -> str:
    from datetime import datetime
    head = (f"{name}: code-route guard " + (f"on (trips above {s['guard']['pts']:g} points)" if s["guard"] else "off")
            + (f", Sonnet share {s['share']:g}" if s["share"] is not None else ", code route off")
            + ("; HOLDING every code kind on Opus until route.code_sonnet changes" if s["held"] else ""))
    out = [head]
    for r in s["rows"]:
        when = datetime.fromtimestamp(float(r.get("ts") or 0), tz).strftime("%Y-%m-%d %H:%M %Z").strip()
        lines = guard_lines(r)
        out.append(f"  {when}: {lines[0]}" + (" TRIPPED" if r.get("tripped") else ""))
        out.extend(f"      {ln}" for ln in lines[1:])
    if not s["rows"]:
        out.append("  no daily rows yet")
    return "\n".join(out)


def code_routes(db, since: float, until: float | None = None) -> dict:
    """Code worker runs in [since, until) per model and per code-route decision (the run note's CODE_ROUTE_KEY;
    "unrecorded" for runs from before it): {"runs", "models": {model: n}, "sonnet", "why": {why: n}, "base"
    (runs with a recorded decision the route should take: all but code_kept ones), "base_sonnet"}."""
    until = time.time() if until is None else until
    out: dict = {"runs": 0, "models": {}, "sonnet": 0, "why": {}, "base": 0, "base_sonnet": 0}
    for r in db.q("SELECT r.model, r.note FROM runs r JOIN tasks t ON t.id=r.task WHERE r.role='worker' AND "
                  "t.kind='code' AND r.started>=? AND r.started<?", (since, until)):
        note = _note(r)
        cr = note.get(CODE_ROUTE_KEY)
        cr = cr if isinstance(cr, dict) else {}
        c = note.get(CHEAP_KEY)
        model = str(r.get("model") or "?")
        on = cr.get("to") == "sonnet" or (not cr and isinstance(c, dict) and bool(c.get(CODE_KEY)))
        why = "sonnet" if on else str(cr.get("why") or "unrecorded")
        out["runs"] += 1
        out["models"][model] = out["models"].get(model, 0) + 1
        out["sonnet"] += on
        out["why"][why] = out["why"].get(why, 0) + 1
        if why != "unrecorded" and not code_kept(why):   # a run from before the decision was recorded never counts
            out["base"] += 1
            out["base_sonnet"] += on
    return out


def code_routes_text(s: dict, name: str = "", hours: float = 24) -> str:
    out = [f"{name + ': ' if name else ''}code worker runs, last {hours:g} h: {s['runs']}, "
           f"{s['sonnet']} on the Sonnet code route"]
    out.append("  per model: " + (", ".join(f"{k} {v}" for k, v in sorted(s["models"].items())) or "none"))
    out.append("  per decision: " + (", ".join(f"{k} {v}" for k, v in sorted(
        s["why"].items(), key=lambda kv: -kv[1])) or "none"))
    if other := _routed_elsewhere(s["why"]):
        out.append(f"  on another provider or run class (not counted): {other}")
    out.append(f"  the route should take {s['base']} of them (all recorded but {', '.join(CODE_KEEP)}, "
               f"run_class:<cls>); it took {s['base_sonnet']}")
    return "\n".join(out)


def top_reasons(why: dict, skip: tuple = (), n: int = 3) -> str:
    got = sorted(((k, v) for k, v in why.items() if k not in skip), key=lambda kv: (-kv[1], kv[0]))[:n]
    return ", ".join(f"{k} {v}" for k, v in got) or "none recorded"


def share_alarms(db, cfg: dict, now: float | None = None) -> list[dict]:
    """Model-free: each configured route (the code route, route.code_sonnet; the device A/B, route.device_ab)
    that took under half its configured share of at least SHARE_MIN runs or tasks in the last SHARE_WINDOW_S:
    [{"route", "share", "got", "n", "text"}], naming the top reasons it was not taken."""
    now = time.time() if now is None else now
    out = []
    cs = code_settings(cfg)
    if cs and cs["share"] > 0:
        s = code_routes(db, now - SHARE_WINDOW_S, now)
        n = s["base"]
        if n >= SHARE_MIN and s["base_sonnet"] / n < cs["share"] / 2:
            got = s["base_sonnet"] / n
            other = _routed_elsewhere(s["why"])
            out.append({"route": "code_sonnet", "share": cs["share"], "got": round(got, 3), "n": n, "text": (
                f"The Sonnet code route took {s['base_sonnet']} of {n} code runs it should take in the last "
                f"{SHARE_WINDOW_S / 3600:g} h ({got:.0%}; configured share {cs['share']:.0%}). Top reasons: "
                f"{top_reasons({k: v for k, v in s['why'].items() if not code_kept(k)}, ('sonnet',))}."
                + (f"\nNot counted (on another provider or run class): {other}." if other else "")
                + " Details: ttp stats --code-routes.")})
    try:
        from . import deviceab
        d = deviceab.share_check(db, cfg, now)
    except Exception:   # an unreadable A/B never stops the code route's check
        d = None
    if d and d["n"] >= SHARE_MIN and d["got"] < d["share"] / 2:
        out.append({"route": "device_ab", **d, "text": (
            f"The device A/B flipped {d['test_n']} of {d['n']} device tasks to its test arm ({d['test']}) since "
            f"it started ({d['got']:.0%}; configured share {d['share']:.0%}). Top reasons: "
            f"{top_reasons(d['why'])}. Details: ttp stats --device-ab.")})
    return out


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
    """Classes whose cheap core runs in `s` (by_class) include one whose route failed on the endpoint, with no
    run on the chat provider: no tt model could serve them. Runs a keep-core rule held raise nothing."""
    return sorted(c for c, k in (s.get("cheap_down") or {}).items()
                  if k and not any(on_chat(m) for m in (s["classes"].get(c) or {})))


def tt_unavailable_text(s: dict, classes: list[str]) -> str:
    models = sorted({m for c in classes for m in (s["classes"].get(c) or {}) if not on_chat(m)})
    errs = sorted({e for c in classes if (e := (s.get("last_error") or {}).get(c))})
    return (f"tt models unavailable: run classes {', '.join(classes)} ran on cheap Claude models "
            f"({', '.join(models)}) at low effort, not on a tt model; they move back by themselves once a tt "
            f"endpoint answers with a working key. Last fallback: {'; '.join(errs)}.")


def summary(s: dict) -> str:
    """One line for ttp status and the daily review."""
    chat = sum(v["runs"] for ms in s["classes"].values() for m, v in ms.items() if on_chat(m))
    quiet = ", ".join(q["class"] for q in quiet_classes(s))
    return (f"run classes {s['hours']:g} h: {chat} of {s['runs']} on chat, {s['share']:.0%} in chat-routed classes"
            + (f"; routed but none on chat: {quiet}" if quiet else ""))
