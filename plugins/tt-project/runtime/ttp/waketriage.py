"""Before an event-only coordinator wake, the watcher model decides whether it needs the coordinator now.

The daemon asks babysit.ask(..., target="coordinator") once per set of queued events. "skip" folds the
batch: no turn starts, the events stay queued on disk and ride the next wake's digest; the same set is
not asked again, and it wakes without asking once its oldest event is coordinator.wake_triage_max_s old
(default 1800). A new event changes the set and asks again. "wake" or "flag" wakes as before; the
coordinator's effort tier never changes here. Any watcher error wakes and logs the model and the error.
Off with coordinator.wake_triage false.

Without a chat provider (babysit.settings is None) the core provider's cheap model decides instead
(cheapdecide.Haiku: Claude Haiku at low effort, no tools), on by default while Haiku is on
(cheapdecide.settings); off with coordinator.wake_triage_fallback false. It only decides whether the
coordinator wakes now; it never answers in the coordinator's place. Its call runs in the background: while the answer
is on its way the batch waits (never past wake_triage_max_s), and a later tick reads it. Its cost is
booked by Haiku's own path under purpose wake_decision. The same exclusions hold for both: the daemon
asks only for an event-only batch (no user message) that may wait (eventhold.can_wait: no high or
critical event, no ask_, charter_, obligation_, pause_, machine_, resource_, provider_ or must-act
kind) and holds no unblock event (coordinator.UNBLOCK_KINDS)."""
from __future__ import annotations

from . import babysit, cheapdecide

KEY = "wake_triage"
MAX_S = 1800.0
# The question without its answer form: the chat provider answers babysit's schema line (skip or wake
# coordinator), Haiku answers ASK's yes or no; a mixed form lets Haiku answer "skip", which ASK cannot read
EVENTS = ("A batch of project events (below) would wake the project's coordinator, an expensive model run. "
          "Routine progress the coordinator can read on its next wake: a task finished as planned, a timer, "
          "a note. Needs a decision or action now: a failure, a blocker, a stuck or idle project, something "
          "that would be lost or late if it waited.")
QUESTION = EVENTS + (" Answer skip when every event is routine progress; answer wake coordinator when any "
                     "event needs a decision or action now.")


def max_s(cfg: dict) -> float:
    try:
        return max(float((cfg.get("coordinator") or {}).get("wake_triage_max_s", MAX_S)), 0.0)
    except (TypeError, ValueError):
        return MAX_S


PURPOSE = "wake_decision"
ASK = {"wake": {"type": "noul",
                "instructions": EVENTS + " Does any event need the coordinator now? Answer yes or no.",
                "criteria": {"true": "an event needs a decision or action now",
                             "false": "every event is routine progress that can wait for the next wake"}}}


def fallback_on(cfg: dict) -> bool:
    """Whether Haiku decides because there is no chat provider."""
    return babysit.settings(cfg) is None and \
        (cfg.get("coordinator") or {}).get("wake_triage_fallback", True) is not False and \
        cheapdecide.settings(cfg) is not None


def on(cfg: dict) -> bool:
    return (cfg.get("coordinator") or {}).get("wake_triage", True) is not False and \
        (babysit.settings(cfg) is not None or fallback_on(cfg))


def _haiku_ask(haiku):
    """babysit.ask's shape from Haiku: {"verb", "model"}; {"verb": "pending"} while the answer is on its way."""
    def ask(cfg, text, question, target="coordinator", db=None, purpose=PURPOSE):
        h = haiku or cheapdecide.Haiku(cfg, db)
        model = (cheapdecide.settings(cfg) or {}).get("model") or "haiku"
        ans = h.decide(text, ASK, purpose=PURPOSE)
        if ans is None:
            if getattr(h, "pending", False):
                return {"verb": "pending", "model": model}
            raise babysit.Unavailable("Haiku gave no answer")
        return {"verb": "wake" if float(ans["wake"].get("noul", 1.0)) >= 0.5 else "skip", "model": model}
    return ask


def state(db, ids: list[int]) -> str:
    rows = db.q(f"SELECT id, kind, severity, task, text FROM events WHERE id IN ({','.join('?' * len(ids))}) "
                "ORDER BY id", tuple(ids))
    lines = [f"event {r['id']} {r['kind']} ({r['severity']})" + (f" task #{r['task']}" if r["task"] else "")
             + f": {' '.join(str(r['text'] or '').split())[:300]}" for r in rows]
    return "\n".join(lines)[:6000]


def fold(db, cfg: dict, evs: list[dict], now: float, ask=None, haiku=None) -> tuple[bool, str]:
    """(True, log line) when this batch folds into the next wake, else (False, log line or "")."""
    fallback = ask is None and fallback_on(cfg)
    ask = ask or (_haiku_ask(haiku) if fallback else babysit.ask)
    ids = sorted(e["id"] for e in evs)
    seen = db.kv(KEY, {}) or {}
    if seen.get("ids") == ids and seen.get("verb") == "skip":
        if now - min(e["ts"] for e in evs) < max_s(cfg):
            return True, ""
        return False, f"coordinator wake: events {ids} waited {int(max_s(cfg))} s after a watcher skip, waking"
    model = ((cheapdecide.settings(cfg) if fallback else babysit.settings(cfg)) or {}).get("model") or "none"
    try:
        d = ask(cfg, state(db, ids), QUESTION, target="coordinator", db=db, purpose="coordinator_wake")
    except Exception as e:   # the watcher only saves money: any error wakes
        return False, f"coordinator wake triage failed, waking ({model}): {type(e).__name__}: {str(e)[:300]}"
    if d["verb"] == "pending":   # Haiku's answer is on its way: wait for it, never past max_s
        if now - min(e["ts"] for e in evs) < max_s(cfg):
            return True, ""
        return False, f"coordinator wake: events {ids} waited {int(max_s(cfg))} s for the watcher, waking"
    db.set_kv(KEY, {"ids": ids, "verb": d["verb"], "at": now, "model": d.get("model") or model})
    if d["verb"] == "skip":
        return True, (f"coordinator wake folded by the watcher ({d.get('model') or model}): events {ids} "
                      f"stay queued for the next wake, at most {int(max_s(cfg))} s")
    return False, ""
