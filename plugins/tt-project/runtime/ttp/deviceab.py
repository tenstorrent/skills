"""Device A/B (`route.device_ab`): two Claude models on device tasks, off by default.

Setting (a project and an account key; the project's own value wins):
  {"projects": [...], "arms": {"sonnet": {"model": "sonnet", "effort": "high"},
                               "opus": {"model": "opus", "effort": "high"}},
   "share": 0.5, "days": 3, "max_tasks": 60, "seed": N, "test": "sonnet", "start": "<ISO time or epoch>"}
The test arm (it gets `share` of the tasks and stops on an incident) is `test`; without it, "sonnet" when the arms
are the default names (a stored setting may list its keys sorted), else the first arm listed. The other is the
control arm. `start` re-arms the A/B: a new value starts a new window there (days and max_tasks count from it)
and lifts an old stop; without it the window starts when the daemon first sees the setting.

Scope: tasks created after the A/B's start (START_KV, recorded the first time the daemon sees the setting) in a
listed project, of kind work, question or code, that use a device (tierfloor.device_run: a needs_device label,
a device lock or a device machine in resources). Urgent device tasks take part. Excluded and counted (a
device_ab_excluded:<why> label): pinned (provider, model or effort, or a deep tier or user_deep: the user's pin),
live-outage work (outage_work: an `outage:` spec line or outage task label, a resource machine with an open
outage or machine condition when it is first seen, or a high outage event naming the task), plan, review, and
tasks after the end (`days` or `max_tasks`) or after the test arm stopped. A first run that is a tier-up retry
keeps its arm and runs on the control arm. Device tasks never run on the chat
provider (route.eligible keeps them on core); the A/B only acts on the Claude core provider.

Flip: a stable hash of (seed, project, task id) picks the arm, recorded on the task as device_ab:<arm> before
its first run and kept on every later run. A tier-up retry runs on the control arm and is marked on its run.

Stop: a real device incident on a device a test-arm run changed (an event of an incident kind naming it, or a
watcher observation naming a hang, reset, recovery, power cycle or hold on it or citing the run; never a recovery
or serving-again report, an event about another device, a coordinator action, audit, flag or note) during the run
or within INCIDENT_AFTER_S after it stops the test arm at once
(STOP_KV; each tick re-tests a recorded stop against this rule and lifts one that no longer qualifies, with a
device_ab event): its open tasks run on the control arm, new tasks are excluded, and the daemon raises one high event to
the project's coordinator. A run that only read state (a question or report task, `read:` resources, or no
state-changing command in its own log: changed_state) is never charged, nor is one for an incident inside another
task's machine-change ledger entry on that machine (a planned reboot, deploy or power cycle: others_change). A
re-armed start skips events from before the re-arm (at or before the stop it cleared); a run started earlier is
still charged for a later event.

Review: each finished A/B task gets one masked review task (Opus at high effort, REVIEW_LABEL) that scores
answered-what-was-asked and conclusions-supported-by-logs, 0-4 each; the spec never names the arm or model.
`ttp stats <project> --device-ab [--json]` (or --all) reports per arm and kind (device work, device code).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import blindreview, provallow
from .util import num_in, task_labels

KEY = "device_ab"
LABEL = "device_ab:"                    # device_ab:<arm>, on the task before its first run
EXCLUDED = "device_ab_excluded:"        # device_ab_excluded:<why>, counted in the report
REVIEW_LABEL = "device_ab_review:"      # on the masked review task: device_ab_review:<task id>
REVIEW2_LABEL = "device_ab_review2:"    # on the second reviewer's task (another model family): device_ab_review2:<task id>
SKIP2_LABEL = "second_reviewer_skipped:"  # on the first reviewer's task when the second one's provider is not allowed
START_KV = "device_ab_start"            # {"ts", "sig"}
STOP_KV = "device_ab_stop"              # {"ts", "arm", "task", "event", "why"}: the test arm is stopped
KINDS = ("work", "question", "code")
DEFAULT_ARMS = {"sonnet": {"model": "sonnet", "effort": "high"}, "opus": {"model": "opus", "effort": "high"}}
INCIDENT_AFTER_S = 1800.0
# Event kinds of real device or machine incidents (the harness's own): never coordinator actions, audit,
# course-check, flag, digest, note or chat events.
INCIDENT_KINDS = frozenset({"machine_condition", "machine_condition_held", "machine_outage_overdue", "machine_reach",
                            "resource_trouble", "heal_failed"})
INCIDENT_KIND = re.compile(r"^(?:device|broker)_(?:hang|hung|fault|reset|timeout|incident|down|error|failed|power\w*)$")
INCIDENT = re.compile(r"(?i)\b(hang|hangs|hung|reset|resets|recover(?:y|ed|ing|ies)?|power[- ]?cycl\w*|hold)\b")
# A health report that a device is back: not an incident unless the same line names a fault.
RECOVERED = re.compile(r"(?i)\b(serv(?:es|ing|ed)\s+again|back\s+(?:up|online|in\s+service)|healthy\s+again|"
                       r"recovered|after\s+recovery)\b")
FAULT = re.compile(r"(?i)\b(hang|hangs|hung|stuck|fault\w*|fail\w*|error\w*|timeout|timed\s+out|down|crash\w*)\b")
# Tasks that only read state: never charged with an incident.
READ_ONLY_TITLE = re.compile(r"(?i)^\s*(?:\W*\w+:\s*)?(?:report|status)\b")
REVIEW_MODEL = {"model": "opus", "effort": "high"}
SCORE_KEYS = ("answered", "supported")
ANSWER = re.compile(r'"device_ab_scores"\s*:\s*\{[^{}]*\}')


def second_reviewer(cfg: dict | None) -> dict | None:
    """The second blind reviewer (`route.device_ab_shadow.second_reviewer`: model, effort, provider (default
    chat)), another model family than the first, its effort at most high (blindreview.cap_effort); None when unset. It applies to the live A/B's reviews and the
    shadow's, with the same masked inputs and rubric."""
    got = (((cfg or {}).get("route") or {}).get("device_ab_shadow") or {})
    got = got.get("second_reviewer") if isinstance(got, dict) else None
    if not isinstance(got, dict) or not isinstance(got.get("model"), str) or not got["model"].strip():
        return None
    out = {"model": got["model"].strip(), "effort": blindreview.cap_effort(got.get("effort")),
           "provider": str(got.get("provider") or "chat")}
    if not provallow.allows(cfg or {}, out["provider"]):
        # A twin pinned outside providers_allowed could never start (and would raise ready_not_started per
        # review): queue_reviews makes none and labels the first review with why. The pin never moves.
        out["skip"] = f"its provider is not in {provallow.KEY}"  # no provider name: the label sits on a blind review
    return out


def reviewers(first: dict, label1: str, label2: str, second: dict | None) -> list[tuple[str, dict, str | None]]:
    """(label, model, provider pin) per reviewer to queue: the first, and the second unless it is skipped."""
    return [(label1, first, None)] + ([(label2, second, second["provider"])] if second and not second.get("skip") else [])


def skip_labels(second: dict | None) -> list[str]:
    """The first review's extra label recording why the second reviewer was not queued (none when it was)."""
    return [f"{SKIP2_LABEL}{second['skip']}"] if second and second.get("skip") else []


def settings(cfg: dict) -> dict | None:
    """The A/B's settings, or None when it is off (unset, not an object, enabled false, no projects or not two
    arms)."""
    got = (cfg.get("route") or {}).get(KEY)
    if not isinstance(got, dict) or got.get("enabled") is False:
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
    test = str(got.get("test") or "")
    test = test if test in arms else "sonnet" if set(arms) == set(DEFAULT_ARMS) else names[0]
    begin = start_ts(got.get("start"))
    parts = [sorted(p.lower() for p in projects), arms, seed]
    if got.get("test") in arms or begin is not None:   # only set keys re-arm: an older setting keeps its start
        parts.append({"test": test, "start": begin})
    sig = hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:12]
    return {"projects": projects, "arms": arms, "test": test, "control": next(n for n in names if n != test),
            "start": begin,
            "share": num_in(got.get("share"), 0, 1, 0.5), "days": num_in(got.get("days"), 0.01, 365, 3.0),
            "max_tasks": int(num_in(got.get("max_tasks"), 1, 100000, 60)), "seed": seed, "sig": sig}


def start_ts(v: Any) -> float | None:
    """`start` as an epoch: a number, or an ISO time with its offset (none: UTC); None when unset or bad."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v)
    if isinstance(v, str) and v.strip():
        from datetime import datetime, timezone
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    return None


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
    projects, arms or seed) re-arms it: a new A/B that lifts an old stop; events from before the re-arm ("armed", never
    before the stop it cleared, "after") stay spent and never stop the new one; a run started earlier is still charged for a later
    event."""
    got = _kv(db, START_KV)
    if got.get("sig") == s["sig"] and isinstance(got.get("ts"), (int, float)):
        return got
    now = float(now if now is not None else time.time())
    old = stopped(db).get("ts")
    got = {"ts": float(s["start"] if s.get("start") is not None else now), "sig": s["sig"],
           **({"armed": now} if got.get("sig") else {}),
           **({"after": float(old)} if isinstance(old, (int, float)) else {})}
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


OUTAGE_LINE = re.compile(r"(?im)^\s*outage\s*:")


def outage_work(db, task: dict) -> bool:
    """Live-outage work, model-free: an `outage:` spec line or an outage task's label (outagetask.LABEL); a
    resource machine with an open known outage or machine condition (machine_ledger); or a high-severity
    outage event (kind or key naming an outage, outagemsg.OUTAGE_KEYS) whose text names the task (#<id>)."""
    from . import effort
    if OUTAGE_LINE.search(str(task.get("spec") or "")) or any(lb.startswith("outage:") for lb in task_labels(task)):
        return True
    names = set(effort._resource_names(task))
    if names:
        try:
            from . import machine_ledger
            if names & (set(machine_ledger.outages()) | set(machine_ledger.conditions())):
                return True
        except Exception:   # an unreadable ledger never marks work as outage work
            pass
    if db is not None and task.get("id") is not None:
        from .outagemsg import OUTAGE_KEYS
        tag = re.compile(rf"#{int(task['id'])}\b")
        for e in db.q("SELECT kind, fingerprint, text FROM events WHERE severity IN ('high','critical') AND ts>=? "
                      "AND (kind LIKE '%outage%' OR " + " OR ".join("fingerprint LIKE ?" for _ in OUTAGE_KEYS) + ")",
                      (float(task.get("created") or 0) - 86400, *(f"{k}%" for k in OUTAGE_KEYS))):
            if tag.search(str(e.get("text") or "")):
                return True
    return False


def exclusion(task: dict, picked: dict | None = None, db=None) -> str:
    """Why an in-scope device task stays out of the A/B ("" when it may take part): a user's pin or live-outage
    work. Urgency never excludes it; a tier-up retry keeps its arm and runs on the control arm."""
    kind = str(task.get("kind") or "work")
    if kind in ("plan", "review"):
        return kind
    if kind not in KINDS:
        return "kind"
    labels = task_labels(task)
    if str(task.get("tier") or "") == "deep" or str(task.get("assigned_tier") or "") == "deep" \
            or "user_deep" in labels or "needs_deep" in labels:
        return "deep"
    if any(str(task.get(k) or "") for k in ("provider", "model", "effort")):
        return "pin"
    if outage_work(db, task):
        return "outage"
    return ""


def arm_of(s: dict, project: str, task_id: Any) -> str:
    """The stable flip: the test arm for `share` of hash(seed, project, task id), else the control arm."""
    h = int(hashlib.sha256(f"{s['seed']}:{str(project).lower()}:{task_id}".encode()).hexdigest()[:8], 16)
    return s["test"] if h % 10000 < s["share"] * 10000 else s["control"]


def enrolled(db, since: float) -> int:
    return int(db.q("SELECT COUNT(*) AS n FROM tasks WHERE created>=? AND labels LIKE ?",
                    (since, f'%"{LABEL}%'))[0]["n"])


def _label(db, task: dict, label: str) -> None:
    labels = task_labels(task)
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
    labels = task_labels(task)
    arm = next((lb[len(LABEL):] for lb in labels if lb.startswith(LABEL)), "")
    stop = stopped(db)
    if not arm:
        if any(lb.startswith(EXCLUDED) for lb in labels) or not device(task, cfg):
            return None
        now = float(now if now is not None else time.time())
        st = start(db, s, now)
        if float(task.get("created") or 0) < st["ts"]:
            return None
        why = exclusion(task, picked, db)
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


def device_names(task: dict, cfg: dict | None) -> list[str]:
    """The devices the task may change: its write resources (never `read:` ones) that are a `*-device` lock, a
    configured device lock or a machine tagged `device`. A resource every task names (a project host with no device
    tag) is not one; a task that only reads state (a question, a report or status task) changes none."""
    from . import effort
    if task.get("kind") == "question" or READ_ONLY_TITLE.match(str(task.get("title") or "")):
        return []
    return sorted(set(effort.device_resources(effort._resource_names(task), cfg)))


WATCHER = re.compile(r"(?i)(device|machine|broker|hardware|power|chip|board|box|health)")
TASK_REF = re.compile(r"#(\d+)")
RUN_REF = re.compile(r"(?i)\b(?:run\s+#?|runs/)(\d+)\b")
# Where a report names a device only as a bystander: a reporter that cannot reach another box, a paused, healthy or
# idle box, or one entry of a checked/paused/healthy list. `{n}` is the escaped device name.
BYSTANDER = (r"(?<![\w.-]){n}(?!\w)\s+(?:cannot|can't|can not|could not)\s+(?:reach|see|check)\b",
             r"(?<![\w.-]){n}(?!\w)\s+(?:is\s+|was\s+)?(?:paused|healthy|ok|fine|idle|up)\b",
             r"\b(?:checked|paused|healthy|ok|skipped)\b:?(?:\s*[\w.-]+,)*\s*(?<![\w.-]){n}(?!\w)")


def names_affected(text: str, name: str) -> bool:
    """Whether report `text` names device `name` as affected: one of its lines names it, outside any bystander
    mention, beside a hang, reset, recovery, power-cycle or hold word."""
    n = re.escape(name)
    whole = re.compile(rf"(?<![\w.-]){n}(?!\w)")
    for line in text.splitlines():
        if not (whole.search(line) and INCIDENT.search(line)) or (RECOVERED.search(line) and not FAULT.search(line)):
            continue
        for pat in BYSTANDER:
            line = re.sub(pat.format(n=n), " ", line, flags=re.I)
        if whole.search(line):
            return True
    return False


def incident_event(e: dict, run_task: Any, names: list[str], run_id: Any = None) -> bool:
    """Whether event `e` is a real device or machine incident of run `run_id` of task `run_task` that changed the
    devices `names` (device_names). A run that changed none is never charged, nor is a recovery or serving-again
    report. Two shapes count, never a coordinator action, audit, flag, digest, note or chat event and never a
    daemon-written observation:
    - an event of an incident kind naming a device the run changed (its task id alone is not enough: trouble on
      another host lists the tasks it holds up);
    - an observation from a device or machine watcher naming a hang, reset, recovery, power cycle or hold that
      cites the run's task or run id, or else cites no other task and names a device resource the run held as
      affected (names_affected): a fleet report listing the device as paused, healthy, checked or as a box that
      cannot reach another is not about it, and the event's task column alone is not enough.
    A host name every task mentions is not enough. A notice about another task (a deferral, a retest, a review) is never one."""
    kind, text = str(e.get("kind") or ""), str(e.get("text") or "")
    hay = text + " " + str(e.get("data") or "")
    if not names or (RECOVERED.search(text) and not FAULT.search(text)):
        return False
    if kind in INCIDENT_KINDS or INCIDENT_KIND.match(kind):
        return any(re.search(rf"(?<![\w.-]){re.escape(n)}(?!\w)", hay) for n in names)
    if not (kind == "observation" and INCIDENT.search(text)):
        return False
    source = str(e.get("source") or "")
    if not (source.startswith("watcher:") and WATCHER.search(source[len("watcher:"):])):
        return False
    if run_id is not None and any(int(m) == int(run_id) for m in RUN_REF.findall(text)):
        return True
    cited = {int(m) for m in TASK_REF.findall(RUN_REF.sub(" ", text))}
    if run_task is not None and int(run_task) in cited:
        return True
    if cited:
        return False
    report = text + "\n" + str(e.get("data") or "")
    return any(names_affected(report, n) for n in names)


# A run is charged an incident only when its own commands changed a host's state: a reset, power cycle, flash or
# reboot (deviceplan.risky), a service start or stop (glmguard.SERVICE_RE) or a recorded machine change.
MACHINE_CHANGE = re.compile(r"\bttp\s+machines\s+change\s+add\b")


def changed_state(run_dir) -> bool:
    """Whether the run's own recorded commands (sidefx.items) include one that changed a host's state; one a guard
    refused never ran. A run with no readable log is not charged: False."""
    from . import deviceplan, glmguard, sidefx
    try:
        calls = sidefx.items(Path(run_dir), own_only=True) if run_dir else []
        return any(it.get("kind") == "shell" and it.get("status") != "refused"
                   and (deviceplan.risky(w := str(it.get("what") or "")) or glmguard.SERVICE_RE.search(w)
                        or MACHINE_CHANGE.search(w)) for it in calls)
    except Exception:   # noqa: BLE001 - an unreadable log charges nothing
        return False


def others_change(cfg: dict, names: list[str], run_task, ts) -> dict | None:
    """The shared machine-change ledger's entry, open or recently closed, on a machine of `names` (a `<alias>-device`
    lock names machine <alias>) whose window covers `ts`, made by another task: another project's, or this
    project's for a task other than `run_task`. A planned reboot, deploy or power cycle there is not the run's."""
    from . import machine_ledger
    aliases = set(names) | {n[:-len("-device")] for n in names if n.endswith("-device")}
    try:
        entries = machine_ledger.changes(aliases, open_only=False)
    except Exception:   # noqa: BLE001 - an unreadable ledger excuses nothing
        return None
    for c in entries:
        if c.get("by") == cfg.get("name") and str(c.get("task")) == str(run_task):
            continue
        if c.get("kind") == machine_ledger.POWER_CYCLE:
            start, end = machine_ledger.cycle_window(c)
        else:
            start = float(c.get("since") or c.get("added") or 0)
            end = float(c.get("closed") or c.get("expires") or 0)
        if start <= float(ts or 0) <= end:
            return c
    return None


def find_incident(db, cfg: dict, s: dict, since: float, now: float, after: float = 0.0) -> dict | None:
    """A device incident on a test-arm run that started after `since`: {"task", "run", "event", "text"}, else None.
    Only real incident events after `after` (a cleared stop) tied to a device the run changed count
    (incident_event), and only on a run whose own commands changed a host's state (changed_state), outside another
    task's machine change there (others_change)."""
    runs = db.q("SELECT id, task, started, ended, note, dir FROM runs WHERE started>=? AND note LIKE ? ORDER BY id",
                (since, f'%"{KEY}"%'))
    for r in runs:
        ab = _note(r).get(KEY) or {}
        if ab.get("ran") != s["test"]:
            continue
        task = db.task(int(r["task"])) if r.get("task") else None
        names = device_names(task or {}, cfg)
        until = (float(r["ended"]) if r.get("ended") else now) + INCIDENT_AFTER_S
        if not names or not changed_state(r.get("dir")):
            continue
        for e in db.q("SELECT id, ts, source, kind, text, data, task FROM events WHERE ts>=? AND ts>? AND ts<=? "
                      "AND kind NOT LIKE 'device_ab%'", (float(r["started"] or 0), after, until)):
            if incident_event(e, r["task"], names, r["id"]) and not others_change(cfg, names, r["task"], e["ts"]):
                return {"task": r["task"], "run": r["id"], "event": e["id"], "text": str(e.get("text") or "")[:300]}
    return None


def recheck_stop(db, cfg: dict, s: dict, stop: dict, now: float) -> str:
    """Re-test a recorded stop against the current rule. When its event no longer qualifies, lift the stop, log a
    device_ab event saying why and return that text; "" when the stop stands (or cannot be re-read)."""
    try:
        e = db.q("SELECT id, ts, source, kind, text, data, task FROM events WHERE id=?", (int(stop["event"]),))
        run = db.q("SELECT id, task, dir FROM runs WHERE id=?", (int(stop["run"]),))
    except (KeyError, TypeError, ValueError):
        return ""
    if not e or not run:
        return ""
    r = run[0]
    task = db.task(int(r["task"])) if r.get("task") else None
    names = device_names(task or {}, cfg)
    if float(e[0].get("ts") or 0) <= float(_kv(db, START_KV).get("armed") or 0):
        cause = "the incident came before the A/B was re-armed"
    elif not incident_event(e[0], r["task"], names, r["id"]):
        cause = "it is not a device incident tied to the run's own device"
    elif not changed_state(r.get("dir")):
        cause = "the run recorded no command that changed a host's state"
    elif c := others_change(cfg, names, r["task"], e[0]["ts"]):
        cause = f"it fell in another task's recorded change on {c.get('alias')} ({c.get('what')})"
    else:
        return ""
    why = (f"Device A/B: lifted the {stop.get('arm') or s['test']} arm's stop (task #{stop.get('task')}, event "
           f"{stop.get('event')}, kind {e[0].get('kind')}): {cause}, so it must not stop the test.")
    db.set_kv(STOP_KV, {})
    db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
         (now, "daemon", "device_ab", "low", why, "handled"))
    return why


def review_spec(task: dict, run_dir: str) -> str:
    """A masked review's spec: the task and its outcome, never the arm or model that ran it."""
    return "\n".join([
        f"Score the finished device task #{task['id']} ({task.get('kind')}) on two criteria, 0-4 each. You do not "
        "know which model ran it, and must not try to find out (never read the run's model, provider or effort).",
        "", "1. answered: did the result answer what the task asked, fully and directly? (0 none, 4 fully)",
        "2. supported: are its conclusions supported by the logs, commands and outputs it cites? Check the cited "
        "facts within the read budget below. (0 unsupported, 4 every conclusion backed)",
        "", f"Task title: {blindreview.scrub(task.get('title'))}", "Task spec:",
        blindreview.scrub(str(task.get("spec") or "")[:6000]), "",
        f"Its final status: {task.get('status')}.",
        f"Its hand-off (result.json), progress and transcript, with model names masked: "
        f"{run_dir or '(no run folder recorded)'} (a task that ran more than once has one folder per run there, "
        f"oldest first, listed in runs.md); the task's result in `ttp task show {task['id']}`. Read the "
        "run only there: never its run folder or the project's state.",
        blindreview.READ_BUDGET,
        "", "Read only: change nothing, run no device or machine command.",
        'Return in result.json: "device_ab_scores": {"answered": <0-4>, "supported": <0-4>, "why": "<one line>"}.',
        "conclusion: the two scores with a one-line reason.",
    ])


def queue_reviews(db, s: dict, since: float, limit: int = 10, second: dict | None = None) -> list[int]:
    """One masked review per finished A/B task that has none yet, and with a `second` reviewer one more by it
    (same inputs, same rubric); returns the new task ids. A second reviewer marked `skip` (its provider is not in
    providers_allowed) gets no task; the first review's label says why. None while the A/B is off (`s` None)."""
    if not s:
        return []
    done = db.q("SELECT * FROM tasks WHERE created>=? AND labels LIKE ? AND status IN ('done','failed') "
                "ORDER BY id", (since, f'%"{LABEL}%'))
    have = {lb for r in db.q("SELECT labels FROM tasks WHERE labels LIKE ? OR labels LIKE ?",
                             (f'%"{REVIEW_LABEL}%', f'%"{REVIEW2_LABEL}%')) for lb in task_labels(r)}
    queue = reviewers(REVIEW_MODEL, REVIEW_LABEL, REVIEW2_LABEL, second)
    out = []
    for t in done:
        runs = blindreview.run_dirs(db, t["id"])   # every run of the task, oldest first, not only its last
        for label, who, prov in queue:
            if f"{label}{t['id']}" in have or len(out) >= limit:
                continue
            # Blind: the label masks `ttp` for the reviewer; it reads a masked copy of the run, never its folder.
            got = blindreview.packet(Path(db.path).parent, f"ab-{t['id']}", runs)
            out.append(db.add_task(f"Device A/B review of #{t['id']}" + (" (second reviewer)" if prov else ""),
                                   review_spec(t, got), kind="question",
                                   tier="standard", priority=4, origin="daemon", model=who["model"],
                                   effort=who["effort"], provider=prov,
                                   labels=[f"{label}{t['id']}", blindreview.BLIND_LABEL]
                                   + (skip_labels(second) if label == REVIEW_LABEL else [])))
    return out


def tick(db, cfg: dict, now: float | None = None) -> dict:
    """The daemon's periodic step: start, the test arm's incident stop and the masked reviews. Returns
    {"stop": <new stop or None>, "reviews": [ids]}; {} when the A/B is off."""
    s = settings(cfg)
    if not s:
        return {}
    now = float(now if now is not None else time.time())
    st = start(db, s, now)
    new_stop = lifted = None
    if stop := stopped(db):
        lifted = recheck_stop(db, cfg, s, stop, now) or None
    if not stopped(db) and (hit := find_incident(db, cfg, s, st["ts"], now,
                                                 max(float(st.get("after") or 0), float(st.get("armed") or 0)))):
        new_stop = {"ts": now, "arm": s["test"], **hit}
        db.set_kv(STOP_KV, new_stop)
    return {"stop": new_stop, "lifted": lifted, "reviews": queue_reviews(db, s, st["ts"], second=second_reviewer(cfg))}


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


KEPT_OUT = ("pin", "deep", "outage", "plan", "review", "kind", "ended", "full", "test_arm_stopped")


def share_check(db, cfg: dict, now: float | None = None) -> dict | None:
    """The A/B's fill for the daemon's share alarm (runclass.share_alarms): device tasks since its start, less
    those KEPT_OUT by design, and how many went to the test arm: {"share", "test", "test_n", "n", "got", "why":
    {exclusion: n}}; None when it is off, not started, stopped or over its days."""
    s = settings(cfg)
    st = _kv(db, START_KV)
    since = float(st.get("ts") or 0)
    now = time.time() if now is None else now
    if not s or not since or st.get("sig") != s["sig"] or stopped(db) or now - since > s["days"] * 86400:
        return None
    n = test_n = 0
    why: dict = {}
    for t in db.q("SELECT labels FROM tasks WHERE created>=? AND labels LIKE ?", (since, '%"device_ab%')):
        labels = task_labels(t)
        ex = next((lb[len(EXCLUDED):] for lb in labels if lb.startswith(EXCLUDED)), "")
        arm = next((lb[len(LABEL):] for lb in labels if lb.startswith(LABEL)), "")
        if ex:
            why[ex] = why.get(ex, 0) + 1
        if (ex and ex in KEPT_OUT) or not (ex or arm):
            continue
        n += 1
        test_n += arm == s["test"]
    return {"share": s["share"], "test": s["test"], "test_n": test_n, "n": n,
            "got": round(test_n / n, 3) if n else 0.0, "why": why}


def report(q: Callable[..., list[dict]], kv: Callable[[str], Any], cfg: dict | None = None) -> dict:
    """Per arm and kind (device work, device code): tasks, done, failed, retried, follow-up fixes, incidents,
    blind scores, $ and wall minutes per task; the excluded count by reason. `q(sql, args)` reads the project's
    db; `kv(key)` its kv table."""
    st, stop = _obj(kv(START_KV)), _obj(kv(STOP_KV))
    since = float(st.get("ts") or 0)
    s = settings(cfg or {}) if cfg else None
    out: dict = {"on": bool(s), "start": st.get("ts"), "stopped": stop or None, "test": (s or {}).get("test"),
                 "arms": {}, "excluded": {}}
    if s:
        out.update(days=s["days"], max_tasks=s["max_tasks"], share=s["share"], test=s["test"], control=s["control"],
                   models={k: f"{v['model']}:{v['effort']}" for k, v in s["arms"].items()})
    from . import deviceshadow   # read only shadow arms: their own section, never in the counts
    out["shadow"] = deviceshadow.report(q)
    if not since:
        return out
    tasks = q("SELECT id, kind, status, labels FROM tasks WHERE created>=? AND labels LIKE ?",
              (since, '%"device_ab%'))
    reviews: dict[int, dict] = {}
    reviews2: dict[int, dict] = {}
    for r in q("SELECT labels, result, status FROM tasks WHERE labels LIKE ? OR labels LIKE ?",
               (f'%"{REVIEW_LABEL}%', f'%"{REVIEW2_LABEL}%')):
        for lb in task_labels(r):
            for label, into in ((REVIEW_LABEL, reviews), (REVIEW2_LABEL, reviews2)):
                if lb.startswith(label) and lb[len(label):].isdigit() and (sc := _scores(r.get("result"))):
                    into[int(lb[len(label):])] = sc
    fixes: dict[int, int] = {}
    for r in q("SELECT labels FROM tasks WHERE created>=? AND (labels LIKE '%\"continues:%' OR labels LIKE "
               "'%\"review_fix:%')", (since,)):
        for lb in task_labels(r):
            head, _, rest = lb.partition(":")
            if head in ("continues", "review_fix") and rest.isdigit():
                fixes[int(rest)] = fixes.get(int(rest), 0) + 1
    for t in tasks:
        labels = task_labels(t)
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
            "answered": 0.0, "supported": 0.0, "scored2": 0, "answered2": 0.0, "supported2": 0.0, "usd": 0.0,
            "minutes": 0.0})
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
        if sc := reviews2.get(int(t["id"])):
            a["scored2"] += 1
            a["answered2"] += sc["answered"]
            a["supported2"] += sc["supported"]
    for kinds in out["arms"].values():
        for a in kinds.values():
            n, m, m2 = a["tasks"], a["scored"], a["scored2"]
            a["usd_per_task"] = round(a.pop("usd") / n, 2) if n else None
            a["minutes_per_task"] = round(a.pop("minutes") / n, 1) if n else None
            a["answered"] = round(a["answered"] / m, 2) if m else None
            a["supported"] = round(a["supported"] / m, 2) if m else None
            a["answered2"] = round(a["answered2"] / m2, 2) if m2 else None
            a["supported2"] = round(a["supported2"] / m2, 2) if m2 else None
    out["excluded_total"] = sum(out["excluded"].values())
    out["flipped"] = {arm: sum(a["tasks"] for a in kinds.values()) for arm, kinds in out["arms"].items()}
    out["eligible"] = sum(out["flipped"].values())
    return out


def report_text(r: dict, name: str = "") -> str:
    head = f"Device A/B{f' ({name})' if name else ''}: " + ("on" if r.get("on") else "off")
    if r.get("models"):
        head += ", " + ", ".join(f"{k} = {v}" for k, v in r["models"].items())
        if r.get("test"):
            head += f"; test arm {r['test']}"
    lines = [head]
    if r.get("stopped"):
        lines.append(f"  test arm stopped after an incident on task #{r['stopped'].get('task')}")
    from . import deviceshadow
    if not r.get("start"):
        return "\n".join([*lines, "  not started", *deviceshadow.report_text(r.get("shadow"))])
    for arm, kinds in sorted(r["arms"].items()):
        for kind, a in sorted(kinds.items()):
            sc = "-" if a["answered"] is None else f"{a['answered']}/{a['supported']} (n={a['scored']})"
            sc2 = "" if a.get("scored2") is None or not a["scored2"] else \
                f", second reviewer {a['answered2']}/{a['supported2']} (n={a['scored2']})"
            lines.append(f"  {arm:8} {kind:12} tasks {a['tasks']}, done {a['done']}, failed {a['failed']}, "
                         f"retried {a['retried']}, fixes {a['fixes']}, incidents {a['incidents']}, "
                         f"answered/supported {sc}{sc2}, ${a['usd_per_task']}/task, {a['minutes_per_task']} min/task")
    ex = ", ".join(f"{k} {v}" for k, v in sorted(r["excluded"].items())) or "none"
    flip = ", ".join(f"{k} {v}" for k, v in sorted((r.get("flipped") or {}).items())) or "none"
    lines.append(f"  eligible: {r.get('eligible', 0)} (flipped: {flip})")
    lines.append(f"  excluded: {r.get('excluded_total', 0)} ({ex})")
    return "\n".join([*lines, *deviceshadow.report_text(r.get("shadow"))])
