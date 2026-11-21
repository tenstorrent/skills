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

from .util import num_in, task_labels

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

# A run told to talk to a person (GLM never does): an instruction to post, comment, reply, send or notify (a
# sentence or list item that opens with the verb, `then`/`also`/`please` and the like aside) to a person or a channel,
# or to run the command that posts. A spec that only mentions such words (a task about the comment handler, a
# `gh pr comment` bug) is no instruction: only `reply_chat` or an instruction keeps a run on core.
PERSON = re.compile(
    r"(?im)(?:^|[.,;:!?]\s+)[ \t>*#\d.)-]*(?:(?:then|also|and|finally|next|please|you\s+(?:must|should|will)|must|should)\s+)*"
    r"(?:post|send|reply|respond|comment|message|e-?mail|answer|notify)(?:\s+back)?\b[^\n.]{0,60}?"
    r"\b(?:to|on|in|at)\s+(?:the\s+|a\s+|an\s+)?(?:slack|jira|github|gh|e-?mail|user|requester|reviewer|author|person|"
    r"people|channel|thread|pr|pull\s+request|issue|chat)\b"
    r"|(?i:\b(?:run|use|call|via)\s+`?(?:gh\s+(?:pr|issue)\s+(?:comment|review)|ttp\s+(?:notify|urgent-reply))\b)")
# Optimization, design or hard-debug work (Opus keeps it), besides codearm.JUDGMENT, read in the task's own goal
# (goal_spec). Metric and debug words (latency, throughput, root cause, bisect) are no design: they mostly name fixes,
# deflakes and measurements.
DESIGN = re.compile(
    r"(?i)\boptimi[sz](?:e|es|ed|er|ers|ing|ation|ations)\b|\bre-?design\w*|\bdesign\s+(?:a|an|the|how|which)\b"
    r"|\barchitect\w*|\bpropose\b[^\n.]{0,40}\b(?:plan|design|approach)\b|\bhard[- ]debug\w*|\bdeadlock"
    r"|\brace\s+condition")
# A question or work task that reviews another task (Opus reviews), not a fix of one.
REVIEW_TITLE = re.compile(r"(?i)^(?!\s*fix\b).*?(?:\breview\s+(?:of\s+)?#\d+|\bsecond\s+reviewer\b)")
# A job the goal runs through a box's device broker (tierfloor.DEVICE_SPEC names resets, soaks and device tests).
BROKER_JOB = re.compile(r"(?i)\bthrough\s+(?:its|the|a|each|every|[\w.-]+'s)\s+(?:[\w-]+\s+){0,2}broker\b")
# Where a spec's own goal ends: its first `Context:` or `Restrictions:` line (`Restrictions (binding):` too).
GOAL_END = re.compile(r"(?im)^[ \t#>*-]*(?:context|restrictions)\b(?:\s*\([^)\n]*\))?\**\s*:")


def goal_spec(spec) -> str:
    """The spec above its first Context or Restrictions line (the whole spec without one): quoted user words,
    findings and restated rules below it name no design or device work of the task's own."""
    spec = str(spec or "")
    m = GOAL_END.search(spec)
    return spec[:m.start()] if m else spec


def _raw(cfg: dict) -> dict:
    got = (cfg.get("route") or {}).get(KEY)
    return got if isinstance(got, dict) else {}


def device_only(cfg: dict) -> bool:
    """glm_work is off for the project (rolled back or its flag off) while glm_device covers it: only its device
    tasks keep the GLM route (ring_gate sets `route.glm_work.device_only`)."""
    g = _raw(cfg)
    return g.get("on") is not True and g.get("device_only") is True


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


def settings(cfg: dict, device: bool = False) -> dict | None:
    """The route's settings on a covered project with a work model, else None. `device`: the device path's read,
    which also covers a device_only project."""
    g = _raw(cfg)
    if not (covered(cfg) or device and device_only(cfg)) or not (m := model(cfg)):
        return None
    fb = g.get("fallback") if isinstance(g.get("fallback"), dict) else {}
    kinds = g.get("kinds")
    return {"provider": str(g.get("provider") or "chat"), "model": m, "effort": str(g.get("effort") or "max"),
            "harness": str(g.get("harness") or "mini-swe-agent"),
            "holdout": num_in(g.get("holdout"), 0, 1, HOLDOUT), "holdout_held": num_in(g.get("holdout_held"), 0, 1,
                                                                                  HOLDOUT_HELD),
            "seed": str(g.get("seed") or ""),
            "kinds": tuple(str(k) for k in kinds) if isinstance(kinds, list) else KINDS,
            "keep_urgent": g.get("keep_urgent") is True,
            "fallback": {k: str(fb.get(k) or v) for k, v in FALLBACK.items()}}


def person_writeup(cfg: dict) -> bool:
    """Whether a GLM run of a task that answers a chat leaves the words to Opus (`route.glm_work.person_writeup`,
    default on; false keeps such a task whole on core). It acts only where route.glm_work is on."""
    return _raw(cfg).get("person_writeup") is not False


def drafts_on(cfg: dict) -> bool:
    """Whether a GLM run's outward post becomes a draft for the Opus coordinator (glmguard `redirect`)."""
    from . import glmguard
    return glmguard.refusal_settings(cfg)["redirect"]


def bounce_on(cfg: dict) -> bool:
    """Whether a GLM run's hand-off asking for Opus or deep thinking sends its task back (`route.glm_work.bounce`,
    default on; false keeps the old way: such a hand-off stands as the run's end, and only later runs keep core)."""
    return _raw(cfg).get("bounce") is not False


def bounce_reason(res, cfg: dict) -> str:
    """Why a GLM run's hand-off `res` sends its task back to Opus, '' when it does not: `needs_opus` as on the
    code arm (codearm.bounce_reason), or `needs_deep` on any end but done. Model-free; '' with bounce off."""
    from . import codearm, effort
    if not bounce_on(cfg) or not isinstance(res, dict):
        return ""
    if got := codearm.bounce_reason(res):
        return got
    if str(res.get("status") or "") != "done" and effort.wants_deep(res):
        return f"needs deep thinking: {str(res.get(effort.NEEDS_DEEP)).strip()}"[:300]
    return ""


def contract() -> str:
    """The prompt block a non-code GLM run gets while bounce_on: how to hand its task back (code arm runs get
    codearm.contract)."""
    return ("\n\n## Needs Opus or deep thinking (your exit)\n"
            "You run on the cheaper model. If the task needs a choice its spec does not settle, a root cause you cannot "
            "find, or deeper thinking than you can give, stop and hand it back: write result.json with "
            '`{"status": "blocked", "needs_opus": "<one-line reason>", "summary": "<what you found and did>"}` '
            "(or `needs_deep` in place of `needs_opus`). The task restarts fresh on Opus with your summary as its "
            "previous attempt; no attempt is spent. On that exit never run `ttp push`, `git push` or publish anything.")


def size_check() -> str:
    """The prompt block a GLM run on a code task gets: the size review its change meets before it can land, run
    by the run itself first. Reviews of GLM changes fail mostly on that lane (extra copies, dead code, tests that
    only match text), not on correctness."""
    return ("\n\n## Before you hand off: the size review (your change is failed on these)\n"
            "A separate reviewer asks whether this is the smallest change that solves the task. Reread your own diff "
            "as that reviewer before result.json, and fix what it would flag: a helper, string, rule or table that "
            "exists once already (call it, never copy it); dead code, unused parameters, fields, imports or options; "
            "a layer or fallback the spec did not ask for; comments that narrate history or an incident; a citation "
            "of a spec or id that is not defined where you cite it; a test that passes without your change, asserts "
            "only that some text is present, or repeats a rule another test asserts. Run `ttp slop --diff "
            "<base>..HEAD` and clear what it lists. Keep every fact and behaviour the task needs: cut size, not "
            "correctness. Never widen what a change reads or runs on a device or live service beyond what the spec "
            "names, and bound any new read of one in time.")


def ring_gate(cfg: dict, project: str, raw: dict | None) -> dict:
    """Apply the `glm_work` release flag (rollout.py) to `cfg`, in place, and return it. The project's own
    `route.glm_work.on` in its project.json wins as written; else the flag decides (off in doubt). With the flag
    off for the project, the device route still follows glm_device (device_only)."""
    from . import deviceplan, rollout
    try:
        if rollout.own_setting(raw, rollout.FLAGS[KEY]["key"]):
            return deviceplan.gate(cfg, project, raw)
        on = rollout.on(KEY, project, raw)
    except Exception:
        on = False
    g = {**_raw(cfg), "on": on}
    g.pop("device_only", None)
    cfg["route"] = {**(cfg.get("route") or {}), KEY: g}
    deviceplan.gate(cfg, project, raw)
    if not on and deviceplan.settings(cfg)["on"]:
        cfg["route"][KEY] = {**_raw(cfg), "device_only": True}
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
    s = settings(cfg, device=True)
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


INFRA_CAUSES = ("timeout", "failed")   # route.RUN_CAUSES that are the infrastructure's: a rerun stays on GLM
INFRA_ENDS = ("failed", "timeout", "stalled", "lost", "request_error", "killed")   # a run row's ends with no hand-off


def infra_failed(db, task: dict) -> bool:
    """Whether the task's last worker run ended on the infrastructure (an endpoint error, a crash, a timeout, a
    stall, a kill) with no hand-off: a retry of it is no GLM miss. A run that handed off (even `failed`) or
    ended clean without a hand-off finished its attempt, so its retry is a tier-up on Opus."""
    from .db import load_result
    if not task.get("id") or str(load_result(task.get("result")).get("status") or "") in ("failed", "done", "blocked"):
        return False
    try:
        row = db.one("SELECT status FROM runs WHERE task=? AND role='worker' ORDER BY id DESC LIMIT 1", (task["id"],))
    except Exception:   # in doubt, the old way: core
        return False
    return bool(row) and str(row["status"] or "") in INFRA_ENDS


def keeps_core(db, cfg: dict, task: dict, tier: str, cls: str | None = None, picked: dict | None = None) -> str:
    """Why a run of `task` stays on the core provider on a covered project ('' when GLM may take it). Model-free,
    in order: coordinator, plan and review runs, deep, a provider or model pin, outside text
    (untrusted_input), a tier-up retry after a finished attempt (infra_failed: a crash, timeout or endpoint error
    retries on GLM), a GLM bounce (needs Opus or deep), a run that talks to a person (`reply_chat` with
    person_writeup off, or an instruction to post, comment or notify with the outward-drafts redirect off: with
    them on, Opus writes the words and GLM does the work), a task titled as a review of another, optimization or
    design work, device work (a job through a device broker too; with
    glm_device on it runs on GLM under an Opus plan, deviceplan.py; both read only the title and goal_spec besides
    its resources), urgent (only with keep_urgent), a kind off the list."""
    from . import codearm, deviceplan, effort, modelpick, untrusted, urgent
    kind = str(task.get("kind") or "")
    text = f"{task.get('title') or ''}\n{task.get('spec') or ''}"
    if cls == "coordinator" or kind in ("plan", "review"):
        return kind if kind in ("plan", "review") else "coordinator"
    labels = task_labels(task)
    if tier == "deep" or any(lb in ("user_deep", "needs_deep") for lb in labels) or effort.wants_deep(task.get("result")):
        return "deep"
    if str(task.get("provider") or "") or str(task.get("model") or ""):
        return "pin"
    if untrusted.labelled(task) or modelpick.UNTRUSTED in labels:
        return "untrusted"
    if (picked or {}).get("by") == "retry" and not infra_failed(db, task):
        return "retry"   # GLM finished and the work failed: Opus; a crash, timeout or endpoint error retries on GLM
    if task.get("id") and codearm.bounced(db, task["id"]):
        return "bounced"
    # A `conclusion:` line alone keeps nothing: that hand-off reaches only the Opus coordinator. A chat reply
    # stays on core only with person_writeup off (else Opus writes the post, writeup.py); an instruction to post
    # only with the outward-drafts redirect off (else the post is a draft the Opus coordinator relays, glmguard.py).
    if task.get("reply_chat") and not person_writeup(cfg):
        return "person"
    if PERSON.search(text) and not drafts_on(cfg):
        return "person"
    if kind in ("question", "work") and REVIEW_TITLE.search(str(task.get("title") or "")):
        return "review"
    own = {**task, "spec": goal_spec(task.get("spec"))}
    goal = f"{task.get('title') or ''}\n{own['spec']}"
    if DESIGN.search(goal) or codearm.JUDGMENT.search(goal):
        return "design"
    if (deviceplan.device(own, cfg) or BROKER_JOB.search(goal)) and not deviceplan.on(cfg):
        return "device"   # with glm_device on, a device task runs on GLM under an Opus plan (deviceplan.py)
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
