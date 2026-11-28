# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The code route's second arm: new code tasks on a chat model (GLM-style) or Sonnet by a seeded coin, off by default.

Setting, under the code route it builds on (`route.code_sonnet`; off when that route is off):
  {"glm_share": 0.5,                                   # 0..1 of eligible new code tasks; 0 or unset: off
   "glm": {"provider": "chat", "model": "<chat model>", "effort": "max", "harness": "claude-code"},
   "glm_seed": "",                                     # the coin's seed: a new seed re-deals new tasks only
   "glm_cap": {"start": 4, "min": 1, "max": 8},        # concurrent runs on the arm, moved by refusals
   "glm_guard_pts": 10, "glm_guard_min": 15}           # the switch-back guard
The arm has the shape of a `route.device_ab_shadow` arm. A live arm run needs a guarded harness (HARNESSES: one whose
command layer enforces glmguard's side-effect guard); on any other harness `glm_share` is treated as 0 and tasks
keep their normal route (refusal logs why once). Claude Code is not one: GLM there is untested live.

Who takes the coin (choose): a new standard-tier code task that the Sonnet route could take (runclass.code_route:
no pin, no outside input, no deep ask, not a retry), with no resource of any kind (device, machine, lock, read),
no protected class (route.protected: urgent, device, optimization or performance, conclusion), no `conclusion:`
line, not user-ordered and no `code_arm: claude` line (the coordinator's force). Anything else, or anything in
doubt, is the `claude` arm: the code route as before. A task the user ordered onto the arm, marked by the
coordinator's `code_arm: user msg #<id>` line (user_arm), skips the user-order and policy-class checks and the coin
and runs on the arm while it is live; its other stays hold, and reviews stay on Opus. The coin is sha256 of the seed and task id, recorded once per
task (kv `code_arm:<id>`) with its seed and why, so every run of the task reads the same arm. A follow-up (a
continue or a review fix) takes its lineage's arm (chaincap.chain), never a new coin.

Each run of a `glm` task starts on the arm's provider and model (the route entry's `code_arm`) unless a check
holds it now: a `needs Opus` bounce or a fallback before (its later runs start on Claude), the switch-back
guard, a protected class or pin added since. Opus keeps every judgment run: reviews, plans, questions,
conclusions and coordinators never take this route (route.eligible keeps reviews off it; kinds other than
code never reach it). A `sonnet` task runs the Sonnet code route without its canary share.

Policy files (harness/policy_files.txt): outside the GLM work route a spec naming a listed file stays on Claude and a
branch that changes one is bounced to Opus. A project the work route covers has neither: GLM writes those files, Opus's
review (with the ponytail lane) checks them, and its notes name the listed files the diff touched.

Bounce: a run on the arm that cannot do the task (deep thinking, design, a root cause, people) hands off
`{"status": "needs_opus", "needs_opus": "<reason>"}`, pushes nothing and publishes no branch. The daemon requeues
it on Claude at Opus (no attempt spent, no fallback counted against the provider) and logs the reason.

Daily: a per-arm table (day_table) for the daily review and a model-free switch-back guard (guard_run): when GLM
lands within one fix round more than `glm_guard_pts` points less often than Sonnet (each with `glm_guard_min`
decided tasks), or has more reverts within 3 days, the arm's share is held at 0 until the setting changes, the
reason is recorded and one normal event goes out. The cap (cap_tick) starts at `glm_cap.start` concurrent runs;
an hour with no refusals and low server waits raises it by one, an hour with refusals lowers it by one.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .util import num_in, task_labels

KEY = "code_arm"                    # a route entry's and a run note's field: the arm the run took
ARM_KV = "code_arm:"                # kv code_arm:<task id>: {"arm", "why", "seed", "ts"}
OFF_KV = "code_arm_off"             # the guard's trip: {"ts", "setting", "why", "row"}
DAYS_KV = "code_arm_days"           # the guard's daily rows, newest last
CAP_KV = "code_arm_cap"             # {"cap", "ts", "why"}
ARMS = ("glm", "sonnet", "claude")
BOUNCE = "needs_opus"               # the route entry's fallback for a bounce (route.BOUNCE)
# The harnesses a live worker run can take: only a harness whose command layer enforces the arm's side-effect
# guard (glmguard): mini-swe-agent (glmharness, glmenv). Claude Code's GLM runs are untested live and landed 0 of 8 in the
# benchmark, so it is not one; Codex joins once its command layer is guarded.
HARNESSES: tuple = ("mini-swe-agent",)
KNOWN_HARNESSES = ("claude-code", "mini-swe-agent", "codex")   # named in a setting; only HARNESSES run live
REFUSED_KV = "code_arm_refused"     # {"mark", "why", "ts"}: the setting a live refusal was logged for
FORCE = re.compile(r"(?mi)^\s*code_arm:\s*(claude|opus|sonnet)\b")
USER_ARM = re.compile(r"(?mi)^\s*code_arm:\s*user msg #(\d+)\b")   # the coordinator's mark of a user's order
CONCLUSION = re.compile(r"(?mi)^\s*conclusion:")
# A spec that leaves a design or policy choice open (weigh options, a trade-off, no data settles it) is Opus's
# deep thinking (user's rollout rule): kept off the arm model-free, since a dry-run GLM run decided one itself.
JUDGMENT = re.compile(r"(?i)\bweigh(?:s|ing)?\b|\btrade-?offs?\b|\bno (?:measurements?|data|evidence)\b[^.\n]{0,40}"
                      r"\bsettles?\b|\b(?:decide|choose)\s+(?:between|which design|the (?:design|policy|default))\b")
REVERT_TITLE = re.compile(r"^Revert #(\d+)\b")
REVERT_S = 3 * 86400.0
WINDOW_S = 7 * 86400.0              # the table's and the guard's window
GUARD_PTS = 10.0
GUARD_MIN = 15
GUARD_EVERY_S = 23 * 3600.0
GUARD_KEEP = 90
CAP = {"start": 4, "min": 1, "max": 16}   # glm_cap {"start", "min", "max"} sets them (1-64)
CAP_EVERY_S = 3600.0
WAIT_LOW_S = 10.0                   # mean server wait per call (chatbridge per_call wait_s) that still counts as low


def settings(cfg: dict, policy: list | None = None) -> dict | None:
    """The arm's settings, or None when it is off: the code route off (runclass.code_settings), `glm_share` unset
    or 0, no `glm.model`, or an empty policy list (`policy` is the project's policyfiles.load; None: not checked)."""
    from . import glmwork, runclass
    got = (cfg.get("route") or {}).get("code_sonnet")
    got = got if isinstance(got, dict) else {}
    if w := glmwork.settings(cfg):   # a project the GLM work route covers: its settings drive the arm
        if w["harness"] not in HARNESSES:   # the policy list does not gate a covered project: GLM writes those files too
            return None
        share, arm, model = 1 - w["holdout"], {**w, "harness": w["harness"]}, w["model"]
    else:
        if not runclass.code_settings(cfg) or policy is not None and not policy:
            return None
        share = num_in(got.get("glm_share"), 0, 1, 0.0)
        arm = got.get("glm") if isinstance(got.get("glm"), dict) else {}
        model = str(arm.get("model") or "").strip()
    if share <= 0 or not model or str(arm.get("harness") or "claude-code") not in HARNESSES:
        return None   # a share on a harness that is not guarded is 0 for a live run (refusal says why)
    c = got.get("glm_cap") if isinstance(got.get("glm_cap"), dict) else {}
    lo = int(num_in(c.get("min"), 1, 64, CAP["min"]))
    hi = max(lo, int(num_in(c.get("max"), 1, 64, CAP["max"])))
    return {"share": share, "provider": str(arm.get("provider") or "chat"), "model": model,
            "effort": str(arm.get("effort") or "max"), "harness": str(arm.get("harness") or "claude-code"),
            "seed": str(arm.get("seed") or got.get("glm_seed") or ""), "glm_work": bool(glmwork.settings(cfg)),
            "cap": {"start": min(hi, max(lo, int(num_in(c.get("start"), 1, 64, CAP["start"])))), "min": lo, "max": hi},
            "pts": num_in(got.get("glm_guard_pts"), 0, 100, GUARD_PTS),
            "min_tasks": int(num_in(got.get("glm_guard_min"), 1, 10000, GUARD_MIN))}


RING_SHARE = 0.5   # the coin flip's share where the glm_code ring flag turns it on and the project sets none


def ring_gate(cfg: dict, project: str, raw: dict | None) -> dict:
    """Apply the `glm_code` release-ring flag (rollout.py) to `cfg`'s code route, in place, and return `cfg`.
    The project's own `route.code_sonnet.glm_share` in its project.json wins as written. Without one, the
    flag on gives a share from the account settings, else RING_SHARE; the flag off makes it 0, so no coin
    flip runs. Claude review and the switch-back guard are unchanged; the policy-file bounce applies outside the GLM work route."""
    got = (cfg.get("route") or {}).get("code_sonnet")
    if not isinstance(got, dict):
        return cfg
    from . import rollout
    try:
        if rollout.own_setting(raw, rollout.FLAGS["glm_code"]["key"]):
            return cfg
        if not str(((got.get("glm") if isinstance(got.get("glm"), dict) else {}) or {}).get("model") or "").strip():
            return cfg   # no arm model: no coin flip either way (settings)
        share = num_in(got.get("glm_share"), 0, 1, 0.0)
        on = rollout.on("glm_code", project, raw)
    except Exception:   # in doubt, no coin flip
        on, share = False, 0.0
    want = (share if share > 0 else RING_SHARE) if on else 0
    if want == share and "glm_share" in got:
        return cfg
    got = dict(got)
    got["glm_share"] = want
    cfg["route"] = {**cfg["route"], "code_sonnet": got}
    return cfg


def harness(cfg: dict) -> str:
    """The harness the arm's live runs use (`route.code_sonnet.glm.harness`, default claude-code)."""
    got = (cfg.get("route") or {}).get("code_sonnet")
    arm = got.get("glm") if isinstance(got, dict) and isinstance(got.get("glm"), dict) else {}
    return str(arm.get("harness") or "claude-code")


def refusal(cfg: dict, policy: list | None = None) -> str:
    """Why a live `glm_share` > 0 is treated as 0, '' when it is not: its `glm.harness` is not a guarded harness
    (HARNESSES), or the project has no policy list (`policy` empty: fail closed), so no run takes the arm and
    every code task keeps its normal route."""
    from . import glmwork, policyfiles, runclass
    if w := glmwork.settings(cfg):
        harness = w["harness"]
    else:
        if not runclass.code_settings(cfg):
            return ""
        got = (cfg.get("route") or {}).get("code_sonnet")
        got = got if isinstance(got, dict) else {}
        arm = got.get("glm") if isinstance(got.get("glm"), dict) else {}
        if num_in(got.get("glm_share"), 0, 1, 0.0) <= 0 or not str(arm.get("model") or "").strip():
            return ""
        harness = str(arm.get("harness") or "claude-code")
    if harness in HARNESSES:
        if policy is not None and not policy and not w:   # a covered project needs no list
            return (f"route.code_sonnet glm_share is treated as 0: this project has no policy file list "
                    f"(harness/{policyfiles.FILE}); the arm never takes a task before the project names the "
                    f"files that decide its audit, review, approval, budget and push rules")
        return ""
    return (f"route.code_sonnet glm_share is treated as 0: glm.harness {harness!r} is not a guarded harness "
            f"({', '.join(HARNESSES) or 'none yet'}); a live arm run needs the side-effect guard in its command layer")


def log_refusal(db, cfg: dict, log=None, policy: list | None = None) -> str:
    """The refusal's reason, logged once per setting (kv REFUSED_KV keeps the setting it was logged for)."""
    why = refusal(cfg, policy)
    if not why:
        return ""
    mark = setting_mark(cfg) + ("|no-policy" if "policy file list" in why else "")
    got = db.kv(REFUSED_KV)
    if not (isinstance(got, dict) and got.get("mark") == mark):
        db.set_kv(REFUSED_KV, {"mark": mark, "why": why, "ts": time.time()})
        if log:
            log(why)
    return why


def setting_mark(cfg: dict) -> str:
    r = cfg.get("route") or {}
    return json.dumps(r.get("code_sonnet"), sort_keys=True, default=str) + (
        f"|glm_work:{r['glm_work'].get('model')}" if isinstance(r.get("glm_work"), dict) and r["glm_work"].get("on") is True else "")


def held(db, cfg: dict) -> bool:
    """A switch-back trip holds the share at 0 while `route.code_sonnet` is unchanged since."""
    got = db.kv(OFF_KV)
    return isinstance(got, dict) and bool(got.get("ts")) and got.get("setting") == setting_mark(cfg)


PROBE_ARMS = ("treatment", "control")   # the adversarial-probe A/B: an even task id is the treatment


def probe_arm(task_id) -> str:
    return PROBE_ARMS[int(task_id) % 2]


def coin(task_id, share: float, seed: str = "") -> bool:
    """The seeded coin: the same task and seed get the same side every time."""
    h = hashlib.sha256(f"code-arm:{seed}:{task_id}".encode()).hexdigest()
    return int(h[:8], 16) % 1000 < share * 1000


def recorded(db, task_id) -> dict | None:
    got = db.kv(f"{ARM_KV}{task_id}")
    return got if isinstance(got, dict) and got.get("arm") in ARMS else None


def show_line(arm: dict) -> str:
    """`ttp task show`'s line: 'code route arm: glm (coin at share 0.5)'; a policy-class task reads 'opus (policy: ...)'."""
    why = str(arm.get("why") or "")
    name = "opus" if arm.get("arm") == "claude" and why.startswith("policy:") else arm.get("arm")
    return f"code route arm: {name}" + (f" ({why})" if why else "")


def bounced(db, task_id) -> str:
    """The reason of the task's last `needs Opus` bounce, '' when it had none."""
    from .route import KEY as ROUTE, of
    for r in db.q("SELECT note FROM runs WHERE task=? AND role='worker' AND note LIKE ? ORDER BY id DESC",
                  (task_id, f'%"{BOUNCE}"%')):
        e = of(r["note"]) or {}
        if e.get("fallback") == BOUNCE:
            return str(e.get("bounce") or "needs Opus")
    return ""


def user_arm(db, task: dict) -> tuple[int, str]:
    """(message id, '') when `task` carries the line `code_arm: user msg #<id>` from the coordinator's own task_add
    and #id is a user message here: the user ordered it onto the arm, so neither the policy-class check nor the
    holdout coin keeps it off. (0, why it is ignored) for any other task with the line; (0, '') without one. A
    daemon task or a worker follow-up (chaincap.hop_spec drops the line from a hand-off's text) never counts."""
    m = USER_ARM.search(str(task.get("spec") or ""))
    if not m:
        return 0, ""
    mid = int(m.group(1))
    if task.get("origin") != "coordinator":
        return 0, f"code_arm line ignored: task made by {task.get('origin') or 'unknown'}, not the coordinator"
    if not db.one("SELECT 1 FROM messages WHERE id=? AND direction='in' AND kind='user'", (mid,)):
        return 0, f"code_arm line ignored: #{mid} is no user message"
    return mid, ""


def stays(db, cfg: dict, task: dict, tier: str, picked: dict | None = None, policy: list | None = None) -> str:
    """Why `task` stays on Claude now ('' when the arm may take it): the Sonnet route's own stays
    (runclass.code_route), a resource (not on device work the glm_device route takes), a protected class (a `conclusion:` line alone is
    none), the force line, a user order,
    a policy-class spec (policyfiles.task_reason against the project's list `policy`, model-free), an unsupported
    harness. A task the user ordered onto the arm (user_arm) skips the user-order and policy-class checks. A project
    the GLM work route covers has no policy-class stay: GLM writes those files too and the review checks them."""
    from . import deviceplan, glmwork, policyfiles, route, runclass, tierfloor, urgent
    ordered = user_arm(db, task)[0]
    if (task.get("kind") or "") != "code":
        return "not_code"
    if glmwork.settings(cfg):   # covered: the GLM work route's keep-core checks, not the Sonnet route's stays
        if why := glmwork.keeps_core(db, cfg, task, tier, None, picked):
            return why
        if str(tier or task.get("tier") or "standard") not in ("standard", "light"):
            return "tier"
        if any(lb.startswith(("resource:", "exclusive:", "read:")) for lb in task_labels(task)) and not (
                deviceplan.device(task, cfg) and deviceplan.on(cfg)):
            return "resources"   # device work takes the GLM device route whatever its kind (deviceplan.py)
        if FORCE.search(str(task.get("spec") or "")):
            return "forced_claude"
        return ""   # no policy-file stay here: GLM writes those files too, and Opus reviews them (review notes name them)
    if str(tier or task.get("tier") or "standard") != "standard":
        return "tier"
    if any(lb.startswith(("resource:", "exclusive:", "read:")) for lb in task_labels(task)):
        return "resources"
    _, why = runclass.code_route(cfg, task, picked, {k: "full" for k in runclass.CODE_KINDS})
    if why:
        return why
    if FORCE.search(str(task.get("spec") or "")):
        return "forced_claude"
    if JUDGMENT.search(str(task.get("spec") or "")):
        return "judgment"
    if not ordered and (task.get("origin") == "user" or urgent.user_ordered(task)):
        return "user_ordered"
    try:
        if (why := route.protected(db, cfg, task, tier)) and not (
                why == "conclusion" and tierfloor.reasons(task, cfg) == [tierfloor.CONCLUSION_LINE]):
            return why   # a `conclusion:` line alone keeps nothing: the coordinator reads the hand-off, Opus reviews it
    except Exception:   # in doubt, Claude
        return "unchecked"
    if policy and not ordered and (why := policyfiles.task_reason(policy, task)):
        return f"policy: {why}"
    s = settings(cfg) or {}
    if s.get("harness") not in HARNESSES:
        return "harness_unsupported"
    return ""


def lineage_why(db, root: int, parent: dict | None) -> str:
    """A follow-up's recorded why: the rule that put its lineage on its arm (its nearest recorded
    parent's why, else the root's), "(follows #root)" after it, so the share report names that rule and never a lineage miss;
    "lineage of #root" only when no task in it recorded a why."""
    got = str((parent or {}).get("why") or (recorded(db, root) or {}).get("why") or "")
    got = re.sub(r"\s*\(follows #\d+\)$", "", got)
    if not got or got.startswith("lineage of #"):
        return f"lineage of #{root}" + ("" if parent else " (no arm)")
    return f"{got} (follows #{root})"[:300]


def choose(db, cfg: dict, task: dict | None, tier: str, picked: dict | None = None, peek: bool = False,
           policy: list | None = None) -> dict | None:
    """This run's arm: {"arm" (recorded, stable), "run" ("glm", "sonnet", "opus" or "claude": what this run
    takes), "why"}, or None when the arm is off or the task is not code. The first call for a task records its
    arm (unless `peek`); a follow-up takes its lineage's recorded arm (on a covered project a Claude-arm lineage
    other than the holdout's is decided afresh)."""
    s = settings(cfg, policy)
    if not s or not task or task.get("kind") != "code" or not task.get("id"):
        return None
    tid = int(task["id"])
    got = recorded(db, tid)
    if not got:
        from . import chaincap
        ch = chaincap.chain(db, tid) or [task]
        parent = next((a for t in reversed(ch[:-1]) if (a := recorded(db, t["id"]))), None)
        mid, ignored = user_arm(db, task)
        if mid and not stays(db, cfg, task, tier, picked, policy):   # the user's order: no coin, no lineage
            got = {"arm": "glm", "why": f"user msg #{mid}", "seed": s["seed"], "ts": time.time()}
        elif len(ch) > 1 and not (s.get("glm_work") and parent and parent["arm"] == "claude" and not str(
                (recorded(db, ch[0]["id"]) or {}).get("why") or "").startswith("holdout")):
            # a covered project's follow-up of a Claude-arm change is decided afresh (its own stays, then the coin):
            # only the Claude holdout keeps a whole lineage on Claude
            got = {"arm": parent["arm"] if parent else "claude", "why": lineage_why(db, ch[0]["id"], parent),
                   "seed": s["seed"], "ts": time.time()}
        elif s.get("glm_work"):   # covered: a kept task stays on Claude; else the work route's holdout coin
            from . import glmwork
            why = stays(db, cfg, task, tier, picked, policy)
            w = None if why else glmwork.arm(db, cfg, task, peek=peek)
            arm = "glm" if w and w["arm"] == "glm" else "claude"
            got = {"arm": arm, "why": why or ("holdout: " if arm == "claude" else "") + str(w["why"]),
                   "seed": s["seed"], "ts": time.time()}
        else:
            why = stays(db, cfg, task, tier, picked, policy)
            arm = "claude" if why else "glm" if coin(tid, s["share"], s["seed"]) else "sonnet"
            got = {"arm": arm, "why": why or f"coin at share {s['share']:g}", "seed": s["seed"], "ts": time.time()}
        if ignored:
            got["why"] = f"{got['why']}; {ignored}"
        if not peek:
            db.set_kv(f"{ARM_KV}{tid}", got)
    arm, run, why = got["arm"], got["arm"], ""
    if arm in ("glm", "sonnet"):
        if b := bounced(db, tid):
            run, why = "opus", f"needs Opus: {b}"[:200]
        elif arm == "glm" and held(db, cfg):
            run, why = "claude", "switched back"
        elif arm == "glm" and (w := stays(db, cfg, task, "standard" if tier == "light" else tier, picked, policy)):
            # a light wake is no stay (the arm owns all of a task's runs); a tier-up retry still is
            run, why = "claude", w   # a pin, protected class, late review round or force line added since
    return {"arm": arm, "run": run, "why": why or str(got.get("why") or "")}


def code_state(state: dict, arm: dict | None) -> dict:
    """The Sonnet route's state for a run of `arm` (choose): a sonnet arm skips the canary share (the coin
    decided it) but keeps a quality hold; a bounce keeps it on Opus (runclass "needs_opus")."""
    if not arm:
        return state
    from .runclass import CODE_KINDS
    if arm["run"] == "opus":
        return {**state, "arm": "opus"}
    if arm["run"] == "sonnet":
        return {**{k: "full" for k in CODE_KINDS}, **{k: v for k, v in state.items() if v == "off"}}
    return state


def contract(reason_hint: str = "") -> str:
    """The prompt block a run on the arm gets: when and how to bounce."""
    return ("\n\n## Needs Opus (your exit)\n"
            "You run on the cheaper model arm. Opus does planning and deep thinking. Before your first edit, ask: does "
            "the spec leave a choice for me to make (which design, policy or default, between options it names or "
            "not, with no measurement or rule in the spec that settles it)? Must I find a root cause the code and "
            "logs do not show? Does it involve a person (a reply, a post, a review comment, someone's decision)? "
            "If any answer is yes, hand the task back to Opus at once and change nothing: do not decide it yourself, "
            "even if you could. Build only what the spec already decided. To hand it back, write result.json with "
            '`{"status": "blocked", "needs_opus": "<one-line reason>", "summary": "needs Opus: <reason>"}` '
            "and stop (the `needs_opus` field makes it a hand-back, not a block). On that exit never run `ttp push`, `git push` or publish a branch; commits you made stay local. "
            "It spends no attempt and does not count as a failure."
            + (f"\n{reason_hint}" if reason_hint else ""))


def bounce_reason(res: dict | None) -> str:
    """The reason a hand-off asks for Opus, '' when it does not."""
    if not isinstance(res, dict) or str(res.get("status") or "") not in ("blocked", BOUNCE):
        return ""
    r = str(res.get("needs_opus") or "").strip() or (
        str(res.get("summary") or "").strip() if str(res.get("status")) == BOUNCE
        or str(res.get("summary") or "").lower().startswith("needs opus:") else "")
    if not r:
        return ""
    return (r[len("needs Opus:"):].strip() if r.lower().startswith("needs opus:") else r)[:300] or "no reason given"


def handed_back(run_dir) -> str:
    """The reason the run in `run_dir` handed its task back to Opus (its result.json carries `needs_opus`), ''
    when it did not or the file is unreadable. Read where a hand-back must publish nothing (`ttp push`)."""
    try:
        data = json.loads((Path(run_dir) / "result.json").read_text())
    except (OSError, ValueError, TypeError):
        return ""
    return bounce_reason(data if isinstance(data, dict) else None)


def task_handed_back(p, task_id) -> str:
    """The hand-back reason of the task's latest worker run, '' when that run did not hand back (a task
    requeued on Claude after a bounce has a newer run that did the work). The push queue skips such a head."""
    try:
        row = p.db.q("SELECT id, dir FROM runs WHERE task=? AND role='worker' ORDER BY id DESC LIMIT 1", (int(task_id),))
    except (TypeError, ValueError):
        return ""
    return handed_back(row[0]["dir"] or p.runs / str(row[0]["id"])) if row else ""


def run_summary(stats: dict | None) -> dict | None:
    """What a run note keeps of the chat bridge's stats for the per-arm table: tokens (uncached in, cached in,
    out), calls, refusals (429/503 given up, rate-limit faults), busy waits and server wait seconds."""
    if not isinstance(stats, dict):
        return None
    u = stats.get("usage") if isinstance(stats.get("usage"), dict) else {}
    busy = stats.get("busy") if isinstance(stats.get("busy"), dict) else {}
    faults = stats.get("faults") if isinstance(stats.get("faults"), dict) else {}
    srv = stats.get("server") if isinstance(stats.get("server"), dict) else {}
    calls = srv.get("per_call") if isinstance(srv.get("per_call"), list) else []

    def n(d, k):
        v = d.get(k)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    return {"calls": n(u, "calls"), "in": n(u, "input_tokens"), "cached": n(u, "cache_read_input_tokens"),
            "out": n(u, "output_tokens"), "waits": n(busy, "waits"), "refusals": n(busy, "gave_up") + n(faults, "rate_limit"),
            "wait_s": round(sum(float(c.get("wait_s") or 0) for c in calls if isinstance(c, dict))
                            + float(busy.get("waited_s") or 0), 1)}


def cost(cfg: dict, provider: str, model: str, toks: dict) -> float | None:
    """$ for tokens {"in" (uncached), "cached", "out"} at `pricing.<provider>` ([$/M input, cached input, output]
    for the model, else its "default" row): cached tokens at the cache-read rate. None when no rate is set."""
    rows = (cfg.get("pricing") or {}).get(provider) or {}
    row = rows.get(model) if isinstance(rows, dict) else None
    if not (isinstance(row, (list, tuple)) and len(row) == 3):
        row = rows.get("default") if isinstance(rows, dict) else None
    if not (isinstance(row, (list, tuple)) and len(row) == 3):
        return None
    i, c, o = (float(x) for x in row)
    return round((int(toks.get("in") or 0) * i + int(toks.get("cached") or 0) * c + int(toks.get("out") or 0) * o) / 1e6, 6)


def _note(raw) -> dict:
    try:
        got = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def day_table(db, cfg: dict, since: float, until: float) -> dict:
    """Per arm, over new code tasks given an arm in [since, until): tasks, first-try review fails, rounds to land,
    time to land, bounces, reverts within 3 days, server waits and refusals, tokens (uncached in, cached in, out),
    $ and the switch-back rate (decided tasks that landed within one fix round)."""
    from . import chaincap, landed
    out = {a: {"tasks": 0, "reviewed": 0, "review_fails": 0, "landed": 0, "rounds": 0, "land_s": [], "bounces": 0,
               "reverts": 0, "decided": 0, "one_round": 0, "waits": 0, "refusals": 0, "wait_s": 0.0, "in": 0,
               "cached": 0, "out": 0, "usd": 0.0} for a in ARMS}
    reverts: dict[int, float] = {}
    for r in db.q("SELECT title, created FROM tasks WHERE title LIKE 'Revert #%' AND created>=?", (since,)):
        if m := REVERT_TITLE.match(str(r["title"] or "")):
            reverts.setdefault(int(m.group(1)), float(r["created"] or 0))
    roots: dict[int, str] = {}
    for r in db.q("SELECT key, value FROM kv WHERE key LIKE ? AND ts>=? AND ts<?", (f"{ARM_KV}%", since, until)):
        tail = str(r["key"])[len(ARM_KV):]
        try:
            rec = json.loads(r["value"]) if isinstance(r["value"], str) else r["value"]
        except ValueError:
            continue
        if tail.isdigit() and isinstance(rec, dict) and rec.get("arm") in ARMS:
            roots[int(tail)] = rec["arm"]
    lines: dict[int, list[dict]] = {}
    for k in db.q("SELECT id FROM tasks WHERE kind='code' AND created>=?", (since,)):
        c = chaincap.chain(db, int(k["id"]))
        if c and c[0]["id"] in roots and len(c) > len(lines.get(c[0]["id"]) or []):
            lines[c[0]["id"]] = c
    for rid, arm in roots.items():
        ch = lines.get(rid)
        if not ch or ch[0]["id"] != rid or chaincap.prev_of(dict(ch[0])) is not None:
            continue   # a follow-up is counted in its root's lineage
        a = out[arm]
        a["tasks"] += 1
        root, ids = ch[0], [c["id"] for c in ch]
        fr = db.one("SELECT status FROM tasks WHERE kind='review' AND status IN ('done','failed') AND labels LIKE ? "
                    "ORDER BY id LIMIT 1", (f'%"auto_review:{rid}"%',))
        if fr:
            a["reviewed"] += 1
            a["review_fails"] += fr["status"] == "failed"
        fixes = sum(1 for c in ch[1:] if chaincap.is_review_fix(c))
        got = any(landed.recorded(db, i)[0] for i in ids)
        settled = all(str(c.get("status") or "") in ("done", "failed", "cancelled") for c in ch)
        land_t = None
        if got:
            a["landed"] += 1
            a["rounds"] += fixes
            q = db.one(f"SELECT MIN(updated) AS t FROM push_queue WHERE task IN ({','.join('?' * len(ids))}) "
                       "AND status IN ('pushed','landed')", tuple(ids))
            if q and q["t"]:
                land_t = float(q["t"])
                a["land_s"].append(land_t - float(root.get("created") or land_t))
        if got or settled:
            a["decided"] += 1
            a["one_round"] += bool(got) and fixes <= 1
        base = land_t or float(root.get("created") or 0)
        a["reverts"] += sum(1 for i in ids if i in reverts and reverts[i] - base <= REVERT_S)
        for run in db.q(f"SELECT cost_usd, note FROM runs WHERE role='worker' AND task IN "
                        f"({','.join('?' * len(ids))})", tuple(ids)):
            n = _note(run["note"])
            rt = n.get("route") if isinstance(n.get("route"), dict) else {}
            a["bounces"] += rt.get("fallback") == BOUNCE
            g = n.get(KEY) if isinstance(n.get(KEY), dict) else {}
            s = g.get("bridge") if isinstance(g.get("bridge"), dict) else {}
            for k in ("waits", "refusals", "in", "cached", "out"):
                a[k] += int(s.get(k) or 0)
            a["wait_s"] = round(a["wait_s"] + float(s.get("wait_s") or 0), 1)
            a["usd"] = round(a["usd"] + float(run["cost_usd"] or 0), 4)
    for a in out.values():
        ls = sorted(a.pop("land_s"))
        a["land_h_median"] = round(ls[len(ls) // 2] / 3600, 2) if ls else None
        a["rounds_per_landed"] = round(a["rounds"] / a["landed"], 2) if a["landed"] else None
        a["fail_rate"] = round(a["review_fails"] / a["reviewed"], 4) if a["reviewed"] else None
        a["one_round_rate"] = round(a["one_round"] / a["decided"], 4) if a["decided"] else None
    return {"since": since, "until": until, "arms": out}


def guard_check(db, cfg: dict, now: float | None = None) -> dict:
    """The switch-back row: trips when GLM's landed-within-one-fix-round rate is more than `pts` points below
    Sonnet's (each with `min_tasks` decided tasks), or GLM has more reverts within 3 days."""
    now = time.time() if now is None else now
    s = settings(cfg) or {"pts": GUARD_PTS, "min_tasks": GUARD_MIN}
    off = db.kv(OFF_KV)   # the tasks that tripped it never count again once the setting changes (re-armed)
    tab = day_table(db, cfg, max(now - WINDOW_S, float(off.get("ts") or 0) if isinstance(off, dict) else 0), now)
    g, so = tab["arms"]["glm"], tab["arms"]["sonnet"]
    enough = min(g["decided"], so["decided"]) >= s["min_tasks"]
    gap = round((so["one_round_rate"] - g["one_round_rate"]) * 100, 1) if enough else None
    why = ""
    if enough and gap > s["pts"]:
        why = (f"GLM landed within one fix round {g['one_round_rate']:.0%} of {g['decided']} tasks against Sonnet's "
               f"{so['one_round_rate']:.0%} of {so['decided']}: {gap:g} points below (limit {s['pts']:g})")
    elif g["reverts"] > so["reverts"]:
        why = f"GLM had {g['reverts']} reverts within 3 days against Sonnet's {so['reverts']}"
    return {"ts": now, "table": tab, "enough": enough, "gap_pts": gap, "pts": s["pts"], "min_tasks": s["min_tasks"],
            "tripped": bool(why), "why": why}


def guard_run(db, cfg: dict, now: float | None = None) -> dict | None:
    """Daily: None when the arm is off or the guard ran under GUARD_EVERY_S ago; else the row, kept in DAYS_KV. A
    new trip records OFF_KV with the current setting and comes back with "new_trip": the caller raises the event."""
    now = time.time() if now is None else now
    if not settings(cfg):
        return None
    rows = db.kv(DAYS_KV)
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if rows and now - float(rows[-1].get("ts") or 0) < GUARD_EVERY_S:
        return None
    row = guard_check(db, cfg, now)
    row["held"] = held(db, cfg)
    db.set_kv(DAYS_KV, (rows + [row])[-GUARD_KEEP:])
    if row["tripped"] and not row["held"]:
        db.set_kv(OFF_KV, {"ts": now, "setting": setting_mark(cfg), "why": row["why"]})
        return {**row, "new_trip": True}
    return row


def guard_text(row: dict) -> str:
    return (f"Code route GLM arm switched back: {row.get('why')}. Its share is held at 0 (new code tasks take Sonnet "
            "or Opus as before) until route.code_sonnet changes.\n" + "\n".join(guard_report_lines(row)))


def guard_report_lines(row: dict) -> list[str]:
    """The switch-back verdict beside what it does not weigh, per arm over its window: first-review fail rate,
    bounces and $ per task, and a warning for each compared arm under `min_tasks` decided tasks."""
    gap = row.get("gap_pts")
    out = [f"- switch-back guard, 7 days to {time.strftime('%m-%d %H:%M', time.localtime(row['ts']))}: "
           + (f"tripped: {row['why']}" if row.get("tripped") else "not tripped")
           + (f" (GLM {abs(gap):g} points {'below' if gap >= 0 else 'above'} Sonnet on landing within one fix round, "
              f"limit {row['pts']:g})" if gap is not None and not row.get("tripped") else "")]
    arms = (row.get("table") or {}).get("arms") or {}
    for name, a in arms.items():
        if a.get("tasks"):
            fr = f"{a['fail_rate']:.0%} of {a['reviewed']}" if a.get("fail_rate") is not None else "n/a"
            out.append(f"  - {name}: first-review fails {fr}, {a['bounces'] / a['tasks']:.2g} bounces per task, "
                       f"${a['usd'] / a['tasks']:.2f} per task")
    for name in ("glm", "sonnet"):   # the arms the guard compares
        if (n := (arms.get(name) or {}).get("decided", 0)) < row["min_tasks"]:
            out.append(f"  - small sample: {name} has {n} decided tasks, under the guard's {row['min_tasks']}, so "
                       "their landing rates are not compared")
    return out


def table_lines(tab: dict, held_why: str = "") -> list[str]:
    """The per-arm table for the daily review, one line per arm with tasks."""
    out = []
    for name, a in (tab.get("arms") or {}).items():
        if not a.get("tasks"):
            continue
        fr = f"{a['review_fails']}/{a['reviewed']}" if a["reviewed"] else "0/0"
        land = f"{a['land_h_median']:g} h" if a.get("land_h_median") is not None else "n/a"
        rounds = f"{a['rounds_per_landed']:g}" if a.get("rounds_per_landed") is not None else "n/a"
        out.append(f"- {name}: {a['tasks']} tasks, first-try review fails {fr}, landed {a['landed']} "
                   f"({rounds} fix rounds, median {land} to land), bounces {a['bounces']}, reverts {a['reverts']}, "
                   f"server waits {a['waits']} ({a['wait_s']:g} s), refusals {a['refusals']}, tokens in "
                   f"{a['in']:,} uncached + {a['cached']:,} cached, out {a['out']:,}, ${a['usd']:.2f}")
    if held_why:
        out.append(f"- switched back: {held_why}")
    return out


def review_lines(db, cfg: dict, now: float | None = None) -> list[str]:
    """The daily review's per-arm block over the last day; [] while the arm is off and never tripped."""
    now = time.time() if now is None else now
    if not settings(cfg) and not held(db, cfg):
        return []
    off = db.kv(OFF_KV) if held(db, cfg) else None
    rows = db.kv(DAYS_KV)
    last = rows[-1] if isinstance(rows, list) and rows and isinstance(rows[-1], dict) else None
    return (table_lines(day_table(db, cfg, now - 86400, now), str((off or {}).get("why") or ""))
            + (guard_report_lines(last) if last else []))


def running(db) -> int:
    """Runs on the arm now (worker runs not ended whose route entry carries KEY)."""
    from .route import of
    return sum(1 for r in db.q("SELECT note FROM runs WHERE role='worker' AND ended IS NULL AND note LIKE ?",
                               (f'%"{KEY}"%',)) if (of(r["note"]) or {}).get(KEY) == "glm")


def cap(db, cfg: dict) -> int:
    s = settings(cfg)
    if not s:
        return 0
    got = db.kv(CAP_KV)
    c = int(got.get("cap") or 0) if isinstance(got, dict) else 0
    return min(s["cap"]["max"], max(s["cap"]["min"], c)) if c else s["cap"]["start"]


def full(db, cfg: dict) -> bool:
    """Whether the arm's concurrency cap is taken."""
    return running(db) >= cap(db, cfg)


def cap_tick(db, cfg: dict, now: float | None = None) -> dict | None:
    """Hourly: the cap moves by the last hour's arm runs: refusals lower it by one; none, with low server waits
    (mean under WAIT_LOW_S per call), raise it by one. None when the arm is off, no run ended or it is too soon."""
    now = time.time() if now is None else now
    s = settings(cfg)
    if not s:
        return None
    got = db.kv(CAP_KV)
    got = got if isinstance(got, dict) else {}
    if now - float(got.get("ts") or 0) < CAP_EVERY_S:
        return None
    runs = calls = refusals = 0
    wait_s = 0.0
    for r in db.q("SELECT note FROM runs WHERE role='worker' AND ended>=? AND note LIKE ?",
                  (now - CAP_EVERY_S, f'%"{KEY}"%')):
        g = _note(r["note"]).get(KEY)
        b = g.get("bridge") if isinstance(g, dict) and isinstance(g.get("bridge"), dict) else None
        if not b:
            continue
        runs += 1
        calls += int(b.get("calls") or 0)
        refusals += int(b.get("refusals") or 0)
        wait_s += float(b.get("wait_s") or 0)
    old = cap(db, cfg)
    new, why = old, "no change"
    if refusals:
        new, why = max(s["cap"]["min"], old - 1), f"{refusals} refusals in the last hour"
    elif runs and wait_s / max(calls, 1) < WAIT_LOW_S:
        new, why = min(s["cap"]["max"], old + 1), f"{runs} runs, no refusals, mean server wait {wait_s / max(calls, 1):.1f} s"
    elif not runs:
        why = "no runs ended"
    db.set_kv(CAP_KV, {"cap": new, "ts": now, "why": why})
    return {"cap": new, "was": old, "why": why}
