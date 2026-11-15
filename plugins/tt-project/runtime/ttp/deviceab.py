"""Device A/B (`route.device_ab`): two Claude models on device tasks, off by default.

Setting (a project and an account key; the project's own value wins):
  {"projects": [...], "arms": {"sonnet": {"model": "sonnet", "effort": "high"},
                               "opus": {"model": "opus", "effort": "high"}},
   "share": 0.5, "days": 3, "max_tasks": 60, "seed": N}
The first arm listed is the test arm (it gets `share` of the tasks); the second is the control arm.

Scope: tasks created after the A/B's start (START_KV, recorded the first time the daemon sees the setting) in a
listed project, of kind work, question or code, that use a device (tierfloor.device_run: a needs_device label,
a device lock or a device machine in resources). Excluded and counted (a device_ab_excluded:<why> label): urgent,
pinned (provider, model or effort), deep or user_deep, plan, review, a first run that is a tier-up retry, and
tasks after the end (`days` or `max_tasks`) or after the test arm stopped. Device tasks never run on the chat
provider (route.eligible keeps them on core); the A/B only acts on the Claude core provider.

Flip: a stable hash of (seed, project, task id) picks the arm, recorded on the task as device_ab:<arm> before
its first run and kept on every later run. A tier-up retry runs on the control arm and is marked on its run.

Stop: a device incident (a hang, reset, recovery, power cycle or hold named in an event that names the task or
one of its resources) during a test-arm run or within INCIDENT_AFTER_S after it stops the test arm at once
(STOP_KV): its open tasks run on the control arm, new tasks are excluded, and the daemon raises one high event to
the project's coordinator.

Review: each finished A/B task gets one masked review task (Opus at high effort, REVIEW_LABEL) that scores
answered-what-was-asked and conclusions-supported-by-logs, 0-4 each; the spec never names the arm or model.
`ttp stats <project> --device-ab [--json]` (or --all) reports per arm and kind (device work, device code).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable

KEY = "device_ab"
LABEL = "device_ab:"                    # device_ab:<arm>, on the task before its first run
EXCLUDED = "device_ab_excluded:"        # device_ab_excluded:<why>, counted in the report
REVIEW_LABEL = "device_ab_review:"      # on the masked review task: device_ab_review:<task id>
START_KV = "device_ab_start"            # {"ts", "sig"}
STOP_KV = "device_ab_stop"              # {"ts", "arm", "task", "event", "why"}: the test arm is stopped
KINDS = ("work", "question", "code")
DEFAULT_ARMS = {"sonnet": {"model": "sonnet", "effort": "high"}, "opus": {"model": "opus", "effort": "high"}}
INCIDENT_AFTER_S = 1800.0
INCIDENT = re.compile(r"(?i)\b(hang|hangs|hung|reset|resets|recover(?:y|ed|ing|ies)?|power[- ]?cycl\w*|hold)\b")
REVIEW_MODEL = {"model": "opus", "effort": "high"}
SCORE_KEYS = ("answered", "supported")
ANSWER = re.compile(r'"device_ab_scores"\s*:\s*\{[^{}]*\}')


def _labels(task: dict) -> list[str]:
    try:
        return [str(x) for x in json.loads(task.get("labels") or "[]")]
    except (TypeError, ValueError):
        return []


def _num(v: Any, lo: float, hi: float, default: float) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi else default


def settings(cfg: dict) -> dict | None:
    """The A/B's settings, or None when it is off (unset, not an object, no projects or not two arms)."""
    got = (cfg.get("route") or {}).get(KEY)
    if not isinstance(got, dict):
        return None
    projects = [str(x).strip() for x in got.get("projects") or [] if str(x).strip()] \
        if isinstance(got.get("projects"), list) else []
    raw = got.get("arms") if isinstance(got.get("arms"), dict) else DEFAULT_ARMS
    arms = {str(k): {"model": v["model"], "effort": str(v.get("effort") or "")} for k, v in raw.items()
            if isinstance(v, dict) and isinstance(v.get("model"), str) and v["model"]}
    if not projects or len(arms) != 2:
        return None
    seed = got.get("seed")
    seed = int(seed) if isinstance(seed, int) and not isinstance(seed, bool) else 0
    names = list(arms)
    sig = hashlib.sha256(json.dumps([sorted(p.lower() for p in projects), arms, seed]).encode()).hexdigest()[:12]
    return {"projects": projects, "arms": arms, "test": names[0], "control": names[1],
            "share": _num(got.get("share"), 0, 1, 0.5), "days": _num(got.get("days"), 0.01, 365, 3.0),
            "max_tasks": int(_num(got.get("max_tasks"), 1, 100000, 60)), "seed": seed, "sig": sig}


def _obj(v: Any) -> dict:
    """A kv value as an object: db.kv decodes it already, a raw read-only row is its JSON text."""
    if isinstance(v, str):
        try:
            v = json.loads(v or "{}")
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _kv(db, key: str) -> dict:
    return _obj(db.kv(key, {}))


def start(db, s: dict, now: float | None = None) -> dict:
    """The A/B's start {"ts", "sig"}: recorded the first time this setting is seen; a new setting (other
    projects, arms or seed) starts a new A/B and lifts an old stop."""
    got = _kv(db, START_KV)
    if got.get("sig") == s["sig"] and isinstance(got.get("ts"), (int, float)):
        return got
    got = {"ts": float(now if now is not None else time.time()), "sig": s["sig"]}
    db.set_kv(START_KV, got)
    db.set_kv(STOP_KV, {})
    return got


def stopped(db) -> dict:
    """The test arm's stop {"ts", "arm", "task", "why"}, or {} while it runs."""
    return _kv(db, STOP_KV)


def in_project(s: dict, project: str) -> bool:
    return str(project).lower() in {p.lower() for p in s["projects"]}


def device(task: dict, cfg: dict | None) -> bool:
    from . import tierfloor
    return tierfloor.device_run(task, cfg)


def exclusion(task: dict, picked: dict | None = None) -> str:
    """Why an in-scope device task stays out of the A/B ("" when it may take part)."""
    from . import urgent
    kind = str(task.get("kind") or "work")
    if kind in ("plan", "review"):
        return kind
    if kind not in KINDS:
        return "kind"
    labels = _labels(task)
    if str(task.get("tier") or "") == "deep" or str(task.get("assigned_tier") or "") == "deep" \
            or "user_deep" in labels or "needs_deep" in labels:
        return "deep"
    if any(str(task.get(k) or "") for k in ("provider", "model", "effort")):
        return "pin"
    if urgent.is_urgent(task):
        return "urgent"
    if (picked or {}).get("by") == "retry":
        return "retry"
    return ""


def arm_of(s: dict, project: str, task_id: Any) -> str:
    """The stable flip: the test arm for `share` of hash(seed, project, task id), else the control arm."""
    h = int(hashlib.sha256(f"{s['seed']}:{str(project).lower()}:{task_id}".encode()).hexdigest()[:8], 16)
    return s["test"] if h % 10000 < s["share"] * 10000 else s["control"]


def enrolled(db, since: float) -> int:
    return int(db.q("SELECT COUNT(*) AS n FROM tasks WHERE created>=? AND labels LIKE ?",
                    (since, f'%"{LABEL}%'))[0]["n"])


def _label(db, task: dict, label: str) -> None:
    labels = _labels(task)
    if label not in labels:
        db.update_task(int(task["id"]), labels=[*labels, label])
        task["labels"] = json.dumps([*labels, label])


def assign(db, cfg: dict, project: str, task: dict, provider: str, cls: str = "hard",
           picked: dict | None = None, now: float | None = None) -> dict | None:
    """The run's model when the task is in the A/B: {"provider", "model", "effort", "class", "why", KEY: {"arm",
    "ran", "kind", "retry"?, "stopped"?}} (a runclass cheap note the start reads), else None. A new task's arm
    or exclusion is recorded on it before its first run."""
    s = settings(cfg)
    if not s or not in_project(s, project) or provider != "claude" or (cfg.get("core_provider") or "claude") != provider:
        return None
    labels = _labels(task)
    arm = next((lb[len(LABEL):] for lb in labels if lb.startswith(LABEL)), "")
    stop = stopped(db)
    if not arm:
        if any(lb.startswith(EXCLUDED) for lb in labels) or not device(task, cfg):
            return None
        now = float(now if now is not None else time.time())
        st = start(db, s, now)
        if float(task.get("created") or 0) < st["ts"]:
            return None
        why = exclusion(task, picked)
        if not why and now - st["ts"] > s["days"] * 86400:
            why = "ended"
        if not why and enrolled(db, st["ts"]) >= s["max_tasks"]:
            why = "full"
        pick = arm_of(s, project, task["id"])
        if not why and pick == s["test"] and stop:
            why = "test_arm_stopped"
        if why:
            _label(db, task, EXCLUDED + why)
            return None
        arm = pick
        _label(db, task, LABEL + arm)
    if arm not in s["arms"]:
        return None   # an arm of an older setting: its tier's model
    ran, extra = arm, {}
    if (picked or {}).get("by") == "retry":
        ran, extra = s["control"], {"retry": True}
    elif arm == s["test"] and stop:
        ran, extra = s["control"], {"stopped": True}
    m = s["arms"][ran]
    kind = "code" if task.get("kind") == "code" else "work"
    return {"provider": provider, "model": m["model"], "effort": m["effort"], "class": cls,
            "why": f"device A/B arm {arm}" + (f", ran {ran}" if ran != arm else ""),
            KEY: {"arm": arm, "ran": ran, "kind": kind, **extra}}


def _note(run: dict) -> dict:
    try:
        v = json.loads(run.get("note") or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def find_incident(db, cfg: dict, s: dict, since: float, now: float) -> dict | None:
    """A device incident on a test-arm run that started after `since`: {"task", "run", "event", "text"}, else None."""
    from . import effort
    runs = db.q("SELECT id, task, started, ended, note FROM runs WHERE started>=? AND note LIKE ? ORDER BY id",
                (since, f'%"{KEY}"%'))
    for r in runs:
        ab = _note(r).get(KEY) or {}
        if ab.get("ran") != s["test"]:
            continue
        task = db.task(int(r["task"])) if r.get("task") else None
        names = [n for n in effort._resource_names(task or {}) if n]
        until = (float(r["ended"]) if r.get("ended") else now) + INCIDENT_AFTER_S
        for e in db.q("SELECT id, kind, text, task FROM events WHERE ts>=? AND ts<=? AND kind NOT LIKE 'device_ab%'",
                      (float(r["started"] or 0), until)):
            text = str(e.get("text") or "")
            if not INCIDENT.search(text):
                continue
            if e.get("task") == r["task"] or f"#{r['task']}" in text or any(n in text for n in names):
                return {"task": r["task"], "run": r["id"], "event": e["id"], "text": text[:300]}
    return None


def review_spec(task: dict, run_dir: str) -> str:
    """A masked review's spec: the task and its outcome, never the arm or model that ran it."""
    return "\n".join([
        f"Score the finished device task #{task['id']} ({task.get('kind')}) on two criteria, 0-4 each. You do not "
        "know which model ran it, and must not try to find out (never read the run's model, provider or effort).",
        "", "1. answered: did the result answer what the task asked, fully and directly? (0 none, 4 fully)",
        "2. supported: are its conclusions supported by the logs, commands and outputs it cites? Check the cited "
        "logs where you can read them. (0 unsupported, 4 every conclusion backed)",
        "", f"Task title: {task.get('title')}", "Task spec:", str(task.get("spec") or "")[:6000], "",
        f"Its final status: {task.get('status')}.",
        f"Its hand-off (result.json) and logs: {run_dir or '(no run folder recorded)'}; the task's result in "
        f"`ttp task show {task['id']}`.",
        "", "Read only: change nothing, run no device or machine command.",
        'Return in result.json: "device_ab_scores": {"answered": <0-4>, "supported": <0-4>, "why": "<one line>"}.',
        "conclusion: the two scores with a one-line reason.",
    ])


def queue_reviews(db, s: dict, since: float, limit: int = 10) -> list[int]:
    """One masked review per finished A/B task that has none yet; returns the new task ids."""
    done = db.q("SELECT * FROM tasks WHERE created>=? AND labels LIKE ? AND status IN ('done','failed') "
                "ORDER BY id", (since, f'%"{LABEL}%'))
    have = {lb for r in db.q("SELECT labels FROM tasks WHERE labels LIKE ?", (f'%"{REVIEW_LABEL}%',))
            for lb in _labels(r) if lb.startswith(REVIEW_LABEL)}
    out = []
    for t in done:
        if f"{REVIEW_LABEL}{t['id']}" in have or len(out) >= limit:
            continue
        run = db.q("SELECT dir FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (t["id"],))
        out.append(db.add_task(f"Device A/B review of #{t['id']}", review_spec(t, (run[0]["dir"] if run else "") or ""),
                               kind="question", tier="standard", priority=4, origin="daemon",
                               model=REVIEW_MODEL["model"], effort=REVIEW_MODEL["effort"],
                               labels=[f"{REVIEW_LABEL}{t['id']}"]))
    return out


def tick(db, cfg: dict, now: float | None = None) -> dict:
    """The daemon's periodic step: start, the test arm's incident stop and the masked reviews. Returns
    {"stop": <new stop or None>, "reviews": [ids]}; {} when the A/B is off."""
    s = settings(cfg)
    if not s:
        return {}
    now = float(now if now is not None else time.time())
    st = start(db, s, now)
    new_stop = None
    if not stopped(db) and (hit := find_incident(db, cfg, s, st["ts"], now)):
        new_stop = {"ts": now, "arm": s["test"], **hit}
        db.set_kv(STOP_KV, new_stop)
    return {"stop": new_stop, "reviews": queue_reviews(db, s, st["ts"])}


def stop_text(stop: dict) -> str:
    return (f"Device A/B: the {stop['arm']} arm is stopped after a device incident on task #{stop['task']} "
            f"(run {stop['run']}, event {stop['event']}): {stop['text']}. Its open tasks now run on the other arm "
            "and new device tasks stay out; check the device and the task. A new route.device_ab setting restarts it.")


def _scores(result: str | None) -> dict | None:
    try:
        got = (json.loads(result or "{}") or {}).get("device_ab_scores")
    except (TypeError, ValueError, AttributeError):
        m = ANSWER.search(result or "")
        try:
            got = json.loads("{" + m.group(0) + "}")["device_ab_scores"] if m else None
        except ValueError:
            got = None
    if not isinstance(got, dict):
        return None
    vals = {k: got.get(k) for k in SCORE_KEYS}
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 4 for v in vals.values()):
        return None
    return {k: float(v) for k, v in vals.items()}


def report(q: Callable[..., list[dict]], kv: Callable[[str], Any], cfg: dict | None = None) -> dict:
    """Per arm and kind (device work, device code): tasks, done, failed, retried, follow-up fixes, incidents,
    blind scores, $ and wall minutes per task; the excluded count by reason. `q(sql, args)` reads the project's
    db; `kv(key)` its kv table."""
    st, stop = _obj(kv(START_KV)), _obj(kv(STOP_KV))
    since = float(st.get("ts") or 0)
    s = settings(cfg or {}) if cfg else None
    out: dict = {"on": bool(s), "start": st.get("ts"), "stopped": stop or None, "arms": {}, "excluded": {}}
    if s:
        out.update(days=s["days"], max_tasks=s["max_tasks"], share=s["share"], test=s["test"], control=s["control"],
                   models={k: f"{v['model']}:{v['effort']}" for k, v in s["arms"].items()})
    if not since:
        return out
    tasks = q("SELECT id, kind, status, labels FROM tasks WHERE created>=? AND labels LIKE ?",
              (since, '%"device_ab%'))
    reviews: dict[int, dict] = {}
    for r in q("SELECT labels, result, status FROM tasks WHERE labels LIKE ?", (f'%"{REVIEW_LABEL}%',)):
        for lb in _labels(r):
            if lb.startswith(REVIEW_LABEL) and lb[len(REVIEW_LABEL):].isdigit() and (sc := _scores(r.get("result"))):
                reviews[int(lb[len(REVIEW_LABEL):])] = sc
    fixes: dict[int, int] = {}
    for r in q("SELECT labels FROM tasks WHERE created>=? AND (labels LIKE '%\"continues:%' OR labels LIKE "
               "'%\"review_fix:%')", (since,)):
        for lb in _labels(r):
            head, _, rest = lb.partition(":")
            if head in ("continues", "review_fix") and rest.isdigit():
                fixes[int(rest)] = fixes.get(int(rest), 0) + 1
    for t in tasks:
        labels = _labels(t)
        ex = next((lb[len(EXCLUDED):] for lb in labels if lb.startswith(EXCLUDED)), "")
        if ex:
            out["excluded"][ex] = out["excluded"].get(ex, 0) + 1
            continue
        arm = next((lb[len(LABEL):] for lb in labels if lb.startswith(LABEL)), "")
        if not arm:
            continue
        kind = "device code" if t["kind"] == "code" else "device work"
        a = out["arms"].setdefault(arm, {}).setdefault(kind, {
            "tasks": 0, "done": 0, "failed": 0, "retried": 0, "fixes": 0, "incidents": 0, "scored": 0,
            "answered": 0.0, "supported": 0.0, "usd": 0.0, "minutes": 0.0})
        a["tasks"] += 1
        a["done"] += t["status"] == "done"
        a["failed"] += t["status"] == "failed"
        runs = q("SELECT started, ended, cost_usd, note FROM runs WHERE task=?", (t["id"],))
        a["retried"] += any((_note(r).get(KEY) or {}).get("retry") for r in runs)
        a["fixes"] += fixes.get(int(t["id"]), 0)
        a["incidents"] += int(str(stop.get("task")) == str(t["id"]))
        a["usd"] += sum(float(r.get("cost_usd") or 0) for r in runs)
        a["minutes"] += sum(max(float(r["ended"]) - float(r["started"]), 0) for r in runs
                            if r.get("ended") and r.get("started")) / 60
        if sc := reviews.get(int(t["id"])):
            a["scored"] += 1
            a["answered"] += sc["answered"]
            a["supported"] += sc["supported"]
    for kinds in out["arms"].values():
        for a in kinds.values():
            n, m = a["tasks"], a["scored"]
            a["usd_per_task"] = round(a.pop("usd") / n, 2) if n else None
            a["minutes_per_task"] = round(a.pop("minutes") / n, 1) if n else None
            a["answered"] = round(a["answered"] / m, 2) if m else None
            a["supported"] = round(a["supported"] / m, 2) if m else None
    out["excluded_total"] = sum(out["excluded"].values())
    return out


def report_text(r: dict, name: str = "") -> str:
    head = f"Device A/B{f' ({name})' if name else ''}: " + ("on" if r.get("on") else "off")
    if r.get("models"):
        head += ", " + ", ".join(f"{k} = {v}" for k, v in r["models"].items())
    lines = [head]
    if r.get("stopped"):
        lines.append(f"  test arm stopped after an incident on task #{r['stopped'].get('task')}")
    if not r.get("start"):
        return "\n".join([*lines, "  not started"])
    for arm, kinds in sorted(r["arms"].items()):
        for kind, a in sorted(kinds.items()):
            sc = "-" if a["answered"] is None else f"{a['answered']}/{a['supported']} (n={a['scored']})"
            lines.append(f"  {arm:8} {kind:12} tasks {a['tasks']}, done {a['done']}, failed {a['failed']}, "
                         f"retried {a['retried']}, fixes {a['fixes']}, incidents {a['incidents']}, "
                         f"answered/supported {sc}, ${a['usd_per_task']}/task, {a['minutes_per_task']} min/task")
    ex = ", ".join(f"{k} {v}" for k, v in sorted(r["excluded"].items())) or "none"
    lines.append(f"  excluded: {r.get('excluded_total', 0)} ({ex})")
    return "\n".join(lines)
