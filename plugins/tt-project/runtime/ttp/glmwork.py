"""The GLM work route (`route.glm_work`): on a covered project, every run the model-free keep-core checks
(keeps_core) do not hold goes to the chat provider's work model at max effort, apart from a seeded Claude
holdout that is the project's live quality baseline (glmshare.judge).

Covered: `route.glm_work.on` true, from the project's own project.json or the `glm_work` release flag
(rollout.py: waves under level auto). Code tasks take the guarded GLM code arm (codearm.py: its harness,
policy bounce and switch-back guard); other kinds route here (daemon._glm_route). Every arm is logged per
task (kv `glm_work_arm:<id>`). The holdout is `holdout` while the project is in its wave and `holdout_held`
once its wave gate says its quality holds (kv HELD_KV, set hourly by the daemon from glmshare.judge).
A run the endpoint refuses, is over capacity for or cannot seat in time starts on core with the fallback
model (Sonnet at high effort by default), and its note names that model.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

KEY = "glm_work"                    # a route entry's and a run note's field: the arm this run took
ARM_KV = "glm_work_arm:"            # kv glm_work_arm:<task id>: {"arm": "glm"|"holdout", "why", "share", "ts"}
HELD_KV = "glm_work_held"           # kv: {"state", "why", "ts"}: the wave gate's last verdict for this project
HOUR_KV = "glm_work_hour:"          # kv glm_work_hour:<YYYY-MM-DDTHH>: {"starts", "fallbacks": {why: n}, "wait_s"}
HOUR_KEEP = 48
ARMS = ("glm", "holdout")
KINDS = ("code", "work", "question", "harness")
HOLDOUT = 0.10                      # in its wave
HOLDOUT_HELD = 0.05                 # once its wave gate says its quality holds
FALLBACK = {"provider": "claude", "model": "sonnet", "effort": "high"}
# Reasons a routed run starts on core (the route entry's `fallback`, route.REASONS) that take the fallback model:
# the endpoint refused or is cooling down after a fault (route.FAULTS), or had no room in time. A rerun after the
# task's own routed run failed and an escalation keep their core model.
FALLBACK_ON = ("down", "error", "rate_limit", "auth", "malformed", "model_missing", "busy", "slots",
               "busy_wait_expired")

# A run that talks to a person (user msg #3512: GLM never does): a reply, post or comment to a person or a
# channel, or the commands that post.
PERSON = re.compile(
    r"(?i)\b(?:post|send|reply|respond|comment|message|e-?mail|answer)(?:s|ed|ing)?\b[^\n]{0,40}?"
    r"\b(?:slack|jira|github|gh|e-?mail|the user|the requester|the reviewer|the author|a person|people|"
    r"channel|thread|pr comment|issue comment)\b"
    r"|\bgh\s+(?:pr|issue)\s+(?:comment|review)\b|\bttp\s+(?:notify|urgent-reply)\b")
# Optimization or design work (user msg #3643: Opus keeps it): besides modelpick.HARD and codearm.JUDGMENT.
DESIGN = re.compile(
    r"(?i)\boptimi[sz](?:e|es|ed|ing|ation|ations)\b|\bre-?design\w*|\bdesign\s+(?:a|an|the|how|which)\b"
    r"|\barchitect\w*|\bpropose\b[^\n.]{0,40}\b(?:plan|design|approach)\b")


def _num(v, lo: float, hi: float, default: float) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi else default


def _raw(cfg: dict) -> dict:
    got = (cfg.get("route") or {}).get(KEY)
    return got if isinstance(got, dict) else {}


def covered(cfg: dict) -> bool:
    return _raw(cfg).get("on") is True


def model(cfg: dict) -> str:
    """The work model: `route.glm_work.model`, else the code arm's `route.code_sonnet.glm.model`."""
    got = str(_raw(cfg).get("model") or "").strip()
    if got:
        return got
    cs = (cfg.get("route") or {}).get("code_sonnet")
    arm = cs.get("glm") if isinstance(cs, dict) and isinstance(cs.get("glm"), dict) else {}
    return str(arm.get("model") or "").strip()


def settings(cfg: dict) -> dict | None:
    """The route's settings on a covered project with a work model, else None."""
    g = _raw(cfg)
    if not covered(cfg) or not (m := model(cfg)):
        return None
    fb = g.get("fallback") if isinstance(g.get("fallback"), dict) else {}
    kinds = g.get("kinds")
    return {"provider": str(g.get("provider") or "chat"), "model": m, "effort": str(g.get("effort") or "max"),
            "harness": str(g.get("harness") or "mini-swe-agent"),
            "holdout": _num(g.get("holdout"), 0, 1, HOLDOUT), "holdout_held": _num(g.get("holdout_held"), 0, 1,
                                                                                  HOLDOUT_HELD),
            "seed": str(g.get("seed") or ""),
            "kinds": tuple(str(k) for k in kinds) if isinstance(kinds, list) else KINDS,
            "keep_urgent": g.get("keep_urgent") is True,
            "fallback": {k: str(fb.get(k) or v) for k, v in FALLBACK.items()}}


def ring_gate(cfg: dict, project: str, raw: dict | None) -> dict:
    """Apply the `glm_work` release flag (rollout.py) to `cfg`, in place, and return it. The project's own
    `route.glm_work.on` in its project.json wins as written; else the flag decides (off in doubt)."""
    from . import rollout
    try:
        if rollout.own_setting(raw, rollout.FLAGS[KEY]["key"]):
            return cfg
        on = rollout.on(KEY, project, raw)
    except Exception:
        on = False
    if on == covered(cfg) and "on" in _raw(cfg):
        return cfg
    cfg["route"] = {**(cfg.get("route") or {}), KEY: {**_raw(cfg), "on": on}}
    return cfg


def holdout(db, cfg: dict) -> float:
    """This project's holdout share now: `holdout_held` once its wave gate said ok, else `holdout`."""
    s = settings(cfg) or {"holdout": HOLDOUT, "holdout_held": HOLDOUT_HELD}
    got = db.kv(HELD_KV)
    return s["holdout_held"] if isinstance(got, dict) and got.get("state") == "ok" else s["holdout"]


def mark_held(db, verdict: dict, now: float | None = None) -> bool:
    """Keep the wave gate's verdict (glmshare.judge) for the holdout; True when the state changed."""
    state = str((verdict or {}).get("state") or "")
    got = db.kv(HELD_KV)
    if isinstance(got, dict) and got.get("state") == state:
        return False
    db.set_kv(HELD_KV, {"state": state, "why": str((verdict or {}).get("why") or "")[:300],
                        "ts": time.time() if now is None else now})
    return True


def coin(task_id, share: float, seed: str = "") -> bool:
    """True: the task is in the Claude holdout (`share` of tasks), the same side for the same task and seed."""
    h = hashlib.sha256(f"glm-work:{seed}:{task_id}".encode()).hexdigest()
    return int(h[:8], 16) % 1000 < share * 1000


def recorded(db, task_id) -> dict | None:
    got = db.kv(f"{ARM_KV}{task_id}")
    return got if isinstance(got, dict) and got.get("arm") in ARMS else None


def arm(db, cfg: dict, task: dict, peek: bool = False) -> dict | None:
    """The task's arm: {"arm": "glm" or "holdout", "why", "share", "ts"}, recorded on its first call (unless
    `peek`); a follow-up takes its lineage's arm. None when the route is off."""
    s = settings(cfg)
    if not s or not task or not task.get("id"):
        return None
    tid = int(task["id"])
    if got := recorded(db, tid):
        return got
    from . import chaincap
    try:
        ch = chaincap.chain(db, tid) or [task]
    except Exception:
        ch = [task]
    parent = next((a for t in reversed(ch[:-1]) if (a := recorded(db, t["id"]))), None)
    share = holdout(db, cfg)
    if parent:
        got = {"arm": parent["arm"], "why": f"lineage of #{ch[0]['id']}", "share": share}
    else:
        side = coin(tid, share, s["seed"])
        got = {"arm": "holdout" if side else "glm", "why": f"coin at holdout {share:g}", "share": share}
    got["ts"] = time.time()
    if not peek:
        db.set_kv(f"{ARM_KV}{tid}", got)
    return got


def _labels(task: dict) -> list[str]:
    try:
        got = json.loads(task.get("labels") or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in got] if isinstance(got, list) else []


def keeps_core(db, cfg: dict, task: dict, tier: str, cls: str | None = None, picked: dict | None = None) -> str:
    """Why a run of `task` stays on the core provider on a covered project ('' when GLM may take it). Model-free,
    in order: coordinator, plan and review runs and verdicts (#3205), deep, a provider or model pin, outside
    text (untrusted_input), a tier-up retry, a GLM bounce (needs Opus or deep), a run that talks to a person
    (#3512), optimization or design work, device work (#3324), urgent (only with keep_urgent), a kind off the
    list."""
    from . import codearm, effort, modelpick, tierfloor, untrusted, urgent
    kind = str(task.get("kind") or "")
    text = f"{task.get('title') or ''}\n{task.get('spec') or ''}"
    if cls == "coordinator" or kind in ("plan", "review"):
        return kind if kind in ("plan", "review") else "coordinator"
    labels = _labels(task)
    if "verdict" in tierfloor.reasons(task, cfg):
        return "verdict"
    if tier == "deep" or any(lb in ("user_deep", "needs_deep") for lb in labels) or effort.wants_deep(task.get("result")):
        return "deep"
    if str(task.get("provider") or "") or str(task.get("model") or ""):
        return "pin"
    if untrusted.labelled(task) or modelpick.UNTRUSTED in labels:
        return "untrusted"
    if (picked or {}).get("by") == "retry":
        return "retry"
    if task.get("id") and codearm.bounced(db, task["id"]):
        return "bounced"
    if task.get("reply_chat") or PERSON.search(text):
        return "person"
    if DESIGN.search(text) or modelpick.HARD.search(text) or codearm.JUDGMENT.search(text):
        return "design"
    if tierfloor.device_run(task, cfg) or tierfloor.DEVICE_SPEC.search(text):
        return "device"
    s = settings(cfg) or {}
    if s.get("keep_urgent") and urgent.is_urgent(task):
        return "urgent"
    if kind not in s.get("kinds", KINDS):
        return f"kind {kind or 'none'}"
    return ""


def fallback(cfg: dict, why: str) -> dict | None:
    """The model a glm_work run that cannot start on the endpoint (`why`, the route entry's fallback) takes on
    core: {"provider", "model", "effort"}, or None when `why` is no endpoint refusal (it starts on core as is)."""
    s = settings(cfg)
    return dict(s["fallback"]) if s and why in FALLBACK_ON else None


def log_hour(db, why: str, wait_s: float | None = None, now: float | None = None) -> None:
    """Count one routed start for this hour: `why` its fallback ('none': it ran on the endpoint), `wait_s`
    the seconds it waited for room. Kept HOUR_KEEP hours."""
    now = time.time() if now is None else now
    hour = time.strftime("%Y-%m-%dT%H", time.gmtime(now))
    got = db.kv(f"{HOUR_KV}{hour}")
    got = got if isinstance(got, dict) else {"starts": 0, "fallbacks": {}, "wait_s": 0.0}
    got["starts"] = int(got.get("starts") or 0) + 1
    if why and why != "none":
        fb = got.get("fallbacks") if isinstance(got.get("fallbacks"), dict) else {}
        fb[why] = int(fb.get(why) or 0) + 1
        got["fallbacks"] = fb
    got["wait_s"] = round(float(got.get("wait_s") or 0) + float(wait_s or 0), 1)
    db.set_kv(f"{HOUR_KV}{hour}", got)
    old = time.strftime("%Y-%m-%dT%H", time.gmtime(now - HOUR_KEEP * 3600))
    try:
        db.x("DELETE FROM kv WHERE key LIKE ? AND key < ?", (f"{HOUR_KV}%", f"{HOUR_KV}{old}"))
    except Exception:
        pass


def hours(db, n: int = 24) -> list[dict]:
    """The last `n` hours' counts, newest first: {"hour", "starts", "fallbacks", "wait_s"}."""
    try:
        rows = db.q("SELECT key, value FROM kv WHERE key LIKE ? ORDER BY key DESC LIMIT ?", (f"{HOUR_KV}%", n))
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            v = json.loads(r["value"])
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict):
            out.append({"hour": r["key"][len(HOUR_KV):], **v})
    return out
