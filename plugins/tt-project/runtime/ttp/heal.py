# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model-free heal checks: a declared health check, a safe fix, and escalation.

A command schedule may carry a `heal` block in its payload (harness/schedules.json or schedule_set):

    "heal": {"check": "<probe>", "fix": "<safe command>", "resource": "<lock>", "grace_s": 300,
             "settle_s": 60, "max_fixes": 3, "window_h": 1, "timeout_s": 120}

`check` exits 0 healthy, 1 unhealthy, 75 or 255 unknown (the codes of start_when); any other exit,
or a timeout, is unknown too and reported as an error of the schedule. Each run of the schedule:
- healthy: the check's state clears (and an open outage alert with it);
- unknown: nothing changes and nothing is fixed;
- unhealthy within `grace_s` of the first unhealthy run: tolerated (automatic recovery may still
  happen); the check runs again when the grace ends;
- unhealthy past grace: the fix runs (under `ttp lock <resource>` when set; a busy or paused lock
  defers it uncounted, but a lock that stays busy, not paused, past `window_h` escalates as below;
  a fix that itself exits 75 counts like any other) and the check runs again `settle_s` later. Healthy then: one low feed line and a digest
  record ("fixed X, n today"), no coordinator wake.
- the fix failed, did not help, `max_fixes` per `window_h` is used up, or there is no fix: one
  priority-1 self-fix task is queued (label `heal:<name>`, so one is open per check), carrying the
  check's and the fix's output. Only once that task fails, blocks, or ends while the check still
  fails does a keyed high alert `heal:<name>` go out; it clears itself once the check is healthy.

Known faults and outages: `known_fault` (why repair is not ours) keeps a single fault quiet: no fix,
no task, no alert. A check with `outage: true` watches a whole box or resource; once it has served
nothing for 60 min (or past its grace_s, if shorter) it escalates as above whatever `known_fault`
says. Its check staying unknown (75, 255 or a broken check: an unreachable box looks like that) for as
long escalates the same way, without running the fix; the next healthy result clears it. Observation
mutes never reach these checks: their escalation does not go through observations.

Daemon time: `timeout_s` is capped at MAX_TIMEOUT_S (the daemon's command-watcher cap), a timeout
kills the check's or fix's whole process group, and the daemon tells its watchdog it still moves
between the check and the fix; after a fix the schedule's own command waits for the recheck, so one
step never outlasts the watchdog.

Presets fill in `check` (and a default `fix`) from config, so no host or unit is named in code:
- `{"preset": "systemd", "unit": "x.service", "user": false, "host": null}`: active and not
  crash-looping (NRestarts rising since the last check); fix `systemctl restart` (over ssh to `host`).
- `{"preset": "http", "url": "...", "expect": [200]}`: the URL answers with an expected status
  (any 2xx/3xx by default). No default fix.
- `{"preset": "broker", "status_command": "...", "hold_field": "held", "auto_fields": [...]}`: the
  command prints JSON; unhealthy while `hold_field` is true or any of `auto_fields` (automatic
  recovery: power cycle, reboot) is false. No default fix.

State lives in the database (kv `heal:<name>`), so a daemon restart, even in the middle of a fix,
picks up where it was: a fix that started counts toward the cap, and the next run is its recheck,
no earlier than `settle_s` after the fix started.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .db import DB

STATE_KEY = "heal:"             # kv per check: its state (see run)
FIXED_KEY = "heal_fixed"        # kv: [{"at", "name"}] successful fixes, last 7 days
LABEL = "heal:"                 # task label of a check's self-fix task
UNKNOWN_RCS = (75, 255)
OUTAGE_S = 3600                 # a whole box or resource serving nothing this long is never kept quiet
LOCK_WAIT_S = 30                # how long a fix waits for its resource lock before it is deferred
MAX_TIMEOUT_S = 240             # timeout_s cap: daemon.WATCHER_MAX_S, so check, then fix (+ lock wait) stay
                                # each well below the daemon's watchdog (WATCHDOG_S)
FIX_OWN_75 = 176                # a fix under a lock that itself exits 75 exits this, apart from the lock's 75
DEFAULTS = {"grace_s": 300, "settle_s": 60, "max_fixes": 3, "window_h": 1.0, "timeout_s": 120}
OPEN = ("queued", "running", "waiting", "needs_review")
_COMMON = {"check", "fix", "resource", "preset", "outage", "known_fault", *DEFAULTS}
PRESETS: dict[str, set[str]] = {
    "systemd": {"unit", "user", "host"},
    "http": {"url", "expect"},
    "broker": {"status_command", "hold_field", "auto_fields"},
}
BROKER_HOLD = "held"
BROKER_AUTO = ("auto_power_cycle", "auto_reboot")


# The block ----------------------------------------------------------------------------------------
def validate(block: Any, where: str = "heal") -> dict:
    """The heal block checked and with its defaults filled in. Raises ValueError saying what is wrong."""
    if not isinstance(block, dict):
        raise ValueError(f"{where} is an object")
    preset = block.get("preset")
    if preset is not None and preset not in PRESETS:
        raise ValueError(f"{where}: `preset` must be one of {', '.join(PRESETS)}")
    allowed = _COMMON | PRESETS.get(preset, set())
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}; use {', '.join(sorted(allowed))}")
    out = {**DEFAULTS, **block}
    for k in ("grace_s", "settle_s", "max_fixes", "window_h", "timeout_s"):
        v = out[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"{where}: `{k}` is a number, 0 or more")
    if out["timeout_s"] <= 0 or out["window_h"] <= 0:
        raise ValueError(f"{where}: `timeout_s` and `window_h` must be positive")
    out["timeout_s"] = min(out["timeout_s"], MAX_TIMEOUT_S)   # longer would hold the daemon's tick past its watchdog
    for k in ("check", "fix", "resource", "known_fault"):
        if out.get(k) is not None and not isinstance(out[k], str):
            raise ValueError(f"{where}: `{k}` is text")
    if not isinstance(out.get("outage", False), bool):
        raise ValueError(f"{where}: `outage` is true or false")
    if preset == "systemd":
        if not str(out.get("unit") or "").strip():
            raise ValueError(f"{where}: preset systemd needs `unit`")
        if not isinstance(out.get("user", False), bool):
            raise ValueError(f"{where}: `user` is true or false")
        if out.get("fix") is None:
            out["fix"] = _systemctl(out, "restart")
    elif preset == "http":
        if not str(out.get("url") or "").startswith(("http://", "https://")):
            raise ValueError(f"{where}: preset http needs `url` (http:// or https://)")
        exp = out.get("expect")
        if exp is not None and not (isinstance(exp, list) and all(isinstance(c, int) for c in exp)):
            raise ValueError(f"{where}: `expect` is a list of HTTP status codes")
    elif preset == "broker":
        if not str(out.get("status_command") or "").strip():
            raise ValueError(f"{where}: preset broker needs `status_command`, a command printing JSON")
        auto = out.get("auto_fields")
        if auto is not None and not (isinstance(auto, list) and all(isinstance(f, str) for f in auto)):
            raise ValueError(f"{where}: `auto_fields` is a list of field names")
    elif not str(out.get("check") or "").strip():
        raise ValueError(f"{where} needs `check` (a shell probe: 0 healthy, 1 unhealthy, 75/255 unknown) "
                         f"or a `preset`")
    return out


def of(payload: dict | None) -> dict | None:
    """A schedule payload's heal block, validated; None when it has none or it does not check out."""
    block = (payload or {}).get("heal")
    if not block:
        return None
    try:
        return validate(block)
    except ValueError:
        return None


# Checks -------------------------------------------------------------------------------------------
def _env() -> dict:
    from .providers.base import service_path
    return {**os.environ, "PATH": service_path()}


def _exec(args: str | list[str], cwd: str, timeout: float, env: dict | None = None) -> tuple[int, str]:
    """Run in a session of its own; a timeout kills the whole group (a hung ssh, a fix's children)."""
    try:
        proc = subprocess.Popen(args, shell=isinstance(args, str), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=cwd, env=env or _env(), start_new_session=True)
    except OSError as e:
        return 127, str(e)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:   # a child that left the group still holds the pipes
            pass
        return 124, f"timed out after {timeout:.0f} s"
    return proc.returncode, ((out or "") + (err or "")).strip()[-2000:]


def _shell(cmd: str, cwd: str, timeout: float) -> tuple[int, str]:
    return _exec(cmd, cwd, timeout)


def _systemctl(spec: dict, *args: str) -> str:
    cmd = "systemctl " + ("--user " if spec.get("user") else "") + " ".join(shlex.quote(a) for a in args) \
        + " " + shlex.quote(spec["unit"])
    return f"ssh -o BatchMode=yes {shlex.quote(spec['host'])} {shlex.quote(cmd)}" if spec.get("host") else cmd


def _check_systemd(spec: dict, state: dict, cwd: str) -> tuple[int, str]:
    rc, out = _shell(_systemctl(spec, "show", "-p", "ActiveState", "-p", "NRestarts"), cwd, spec["timeout_s"])
    if rc == 255 and spec.get("host"):
        return 255, f"ssh failed: {out[-300:]}"
    if rc != 0:
        return 75, f"systemctl show failed (rc {rc}): {out[-300:]}"
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    active = props.get("ActiveState", "")
    try:
        restarts = int(props.get("NRestarts", "0"))
    except ValueError:
        restarts = 0
    before = state.get("nrestarts")
    state["nrestarts"] = restarts
    if active != "active":
        return 1, f"{spec['unit']} is {active or 'unknown'}"
    if before is not None and restarts > int(before):
        return 1, f"{spec['unit']} is crash-looping: NRestarts {before} -> {restarts}"
    return 0, f"{spec['unit']} is active (NRestarts {restarts})"


def _check_http(spec: dict, state: dict, cwd: str) -> tuple[int, str]:
    expect = spec.get("expect")
    try:
        with urllib.request.urlopen(spec["url"], timeout=min(spec["timeout_s"], 30)) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 1, f"{spec['url']} does not answer: {getattr(e, 'reason', e)}"
    ok = code in expect if expect else 200 <= code < 400
    return (0 if ok else 1), f"{spec['url']} answered {code}"


def _field(data: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _check_broker(spec: dict, state: dict, cwd: str) -> tuple[int, str]:
    rc, out = _shell(spec["status_command"], cwd, spec["timeout_s"])
    if rc in UNKNOWN_RCS:
        return rc, f"status command could not tell (rc {rc}): {out[-300:]}"
    if rc != 0:
        return 1, f"status command failed (rc {rc}): {out[-300:]}"
    try:
        data = json.loads(out[out.find("{"):] if "{" in out else out)
    except ValueError:
        return 75, f"status command printed no JSON: {out[-300:]}"
    problems = []
    if _field(data, spec.get("hold_field") or BROKER_HOLD):
        problems.append(f"{spec.get('hold_field') or BROKER_HOLD} is set")
    missing = []
    for f in spec.get("auto_fields") or BROKER_AUTO:
        v = _field(data, f)
        if v is None:
            missing.append(f)
        elif not v:
            problems.append(f"{f} is off")
    if problems:
        return 1, "; ".join(problems)
    if missing:
        return 75, f"status does not report {', '.join(missing)}"
    return 0, "no hold; automatic recovery on"


_PRESET_CHECKS: dict[str, Callable[[dict, dict, str], tuple[int, str]]] = {
    "systemd": _check_systemd, "http": _check_http, "broker": _check_broker}


def check(spec: dict, state: dict, cwd: str) -> tuple[int, str]:
    """Run the check: (0 healthy | 1 unhealthy | other: unknown, its output). May update `state`."""
    fn = _PRESET_CHECKS.get(spec.get("preset") or "")
    if fn and not spec.get("check"):
        return fn(spec, state, cwd)
    return _shell(spec["check"], cwd, spec["timeout_s"])


def run_fix(spec: dict, cwd: str, project_base: str) -> tuple[int, str]:
    """Run the fix, under `ttp lock <resource>` when it names one. 75 only from the lock (busy or
    paused): `ttp lock` passes its command's exit code on, so a fix's own 75 comes back as FIX_OWN_75."""
    if not spec.get("resource"):
        return _shell(spec["fix"], cwd, spec["timeout_s"])
    wrap = f'sh -c "$1"; rc=$?; [ "$rc" -eq 75 ] && exit {FIX_OWN_75}; exit "$rc"'
    argv = [sys.executable, "-m", "ttp", "lock", "--timeout", str(LOCK_WAIT_S), spec["resource"],
            "--", "sh", "-c", wrap, "heal-fix", spec["fix"]]
    env = {**_env(), "TTP_PROJECT": project_base, "PYTHONPATH": os.pathsep.join(
        [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.environ.get("PYTHONPATH", "")]).rstrip(
        os.pathsep)}
    return _exec(argv, cwd, spec["timeout_s"] + LOCK_WAIT_S + 10, env)


# State --------------------------------------------------------------------------------------------
def state(db: DB, name: str) -> dict:
    return dict(db.kv(STATE_KEY + name) or {})


def _save(db: DB, name: str, st: dict) -> None:
    db.set_kv(STATE_KEY + name, st)


def fixed_today(db: DB, name: str | None = None, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return sum(1 for x in db.kv(FIXED_KEY, []) or []
               if float(x.get("at") or 0) > now - 86400 and (name is None or x.get("name") == name))


def _record_fixed(db: DB, name: str, now: float) -> int:
    kept = [x for x in db.kv(FIXED_KEY, []) or [] if float(x.get("at") or 0) > now - 7 * 86400]
    db.set_kv(FIXED_KEY, kept + [{"at": now, "name": name}])
    return fixed_today(db, name, now)


def open_task(db: DB, name: str, label: str | None = None) -> dict | None:
    """The check's open self-fix task (its fingerprint is the label heal:<name>, or `label`). The
    labels are compared exactly (instr, then the parsed list): a LIKE pattern would read `_` and `%` in a name as wildcards."""
    want = LABEL + name if label is None else label
    for t in db.q(f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(OPEN))}) AND instr(labels, ?) > 0 "
                  f"ORDER BY id DESC", (*OPEN, json.dumps(want))):
        try:
            labels = json.loads(t["labels"] or "[]")
        except ValueError:
            continue
        if isinstance(labels, list) and want in labels:
            return t
    return None


def has_open_task(db: DB, name: str) -> bool:
    """A self-fix task of the check is open (its own, or the schedule_fix task it adopted)."""
    if open_task(db, name):
        return True
    tid = state(db, name).get("task")
    t = db.task(int(tid)) if tid else None
    return bool(t and t["status"] in OPEN)


# The flow -----------------------------------------------------------------------------------------
def run(host: Any, name: str, spec: dict, now: float | None = None) -> tuple[str, float | None]:
    """One run of the check for the daemon `host` (it has `p` and `alert`, and may have `_progress`,
    the watchdog ping, called between the check and the fix). Returns (schedule status, when to run
    again if sooner than its period)."""
    p, db = host.p, host.p.db
    now = time.time() if now is None else now
    st = state(db, name)
    cwd = str(p.root)
    if st.get("phase") == "fixing" and now < float(st.get("fix_started") or 0) + spec["settle_s"]:
        # A restart cut the fix short: its recheck still waits for settle_s.
        return "unhealthy (fix interrupted; rechecking)", float(st["fix_started"]) + spec["settle_s"]
    rc, out = check(spec, st, cwd)
    ping = getattr(host, "_progress", None)
    if callable(ping):
        ping()
    st.update(last_check=now, last_rc=rc, last_out=out[-1000:])
    pending = st.get("phase") in ("fixing", "settling")   # a fix ran (or a restart cut it short)
    if rc == 0:
        was = st.get("status")
        nxt = {k: st[k] for k in ("fixes", "nrestarts", "last_check", "last_rc", "last_out") if k in st}
        _save(db, name, {**nxt, "status": "healthy"})
        t = db.task(int(st["task"])) if st.get("task") else None
        if t is not None and t["status"] == "queued":   # a running one is left to finish
            db.update_task(t["id"], status="cancelled", blocked_reason="check recovered before the fix ran")
        if pending and was == "unhealthy":
            n = _record_fixed(db, name, now)
            db.post("out", f"Self-healed {name}: its fix worked ({n} today). {out[:200]}".strip(), chat=None,
                    kind="info", severity="low", ref=STATE_KEY + name)
            return f"fixed ({n} today)", None
        return "ok (healthy)", None
    if rc != 1:
        st["unknown_since"] = st.get("unknown_since") or now
        again = None
        if spec.get("outage"):
            # A whole box whose check cannot even tell (ssh fails: it looks like this when it is down)
            # serves nothing either: escalated as unhealthy past the outage window, never fixed blind.
            window = min(spec["grace_s"] or OUTAGE_S, OUTAGE_S)
            dark = now - float(st["unknown_since"])
            if dark >= window:
                st["status"] = "unhealthy"
                st.setdefault("since", st["unknown_since"])
                if st.get("task") or st.get("escalated"):
                    return _escalated(host, name, spec, st, now)
                return _escalate(host, name, spec, st, now, f"its check could not tell for {dark / 60:.0f} min "
                                                            f"(exit {rc}): the box or resource may be unreachable")
            again = float(st["unknown_since"]) + window
        _save(db, name, st)
        if rc in UNKNOWN_RCS:
            return f"ok (unknown, rc {rc})", again
        return f"error: heal check exited {rc}: {out[-150:]}", again
    st.pop("unknown_since", None)
    st["status"] = "unhealthy"
    since = st.setdefault("since", now)
    down = now - float(since)
    outage = bool(spec.get("outage")) and down >= min(OUTAGE_S, spec["grace_s"] or OUTAGE_S)
    if st.get("task") or st.get("escalated"):
        return _escalated(host, name, spec, st, now)
    if pending:
        st["phase"] = None
        return _escalate(host, name, spec, st, now, f"the fix ran but the check still fails {down / 60:.0f} min on")
    if spec.get("known_fault") and not outage:
        _save(db, name, st)
        return "unhealthy (known fault: kept quiet)", None
    grace = min(spec["grace_s"], OUTAGE_S) if spec.get("outage") else spec["grace_s"]   # a whole box waits no longer
    if down < grace:
        _save(db, name, st)
        return f"unhealthy ({down:.0f} s, grace {grace:.0f} s)", float(since) + grace
    if not spec.get("fix"):
        return _escalate(host, name, spec, st, now, "it has no fix")
    window = spec["window_h"] * 3600
    fixes = [t for t in st.get("fixes", []) if float(t) > now - window]
    if len(fixes) >= spec["max_fixes"]:
        st["fixes"] = fixes
        return _escalate(host, name, spec, st, now,
                         f"{len(fixes)} fixes in {spec['window_h']:g} h used up its cap ({spec['max_fixes']})")
    # Recorded before the fix runs: a restart in the middle still counts it, and rechecks next.
    deferred = st.pop("deferred_since", None)
    st.update(fixes=fixes + [now], phase="fixing", fix_started=now)
    _save(db, name, st)
    frc, fout = run_fix(spec, cwd, str(p.base))
    st.update(fix_rc=frc, fix_out=fout[-1000:])
    if frc == 75 and spec.get("resource"):
        # Only the lock exits 75 here (run_fix): the fix never started. Not counted; try again next run.
        st.update(fixes=fixes, phase=None)
        res = spec["resource"]
        if "is paused" in fout:   # a pause is the user's; it waits however long it lasts
            _save(db, name, st)
            return f"unhealthy (fix deferred: {res} paused)", None
        st["deferred_since"] = deferred or now
        if now - float(st["deferred_since"]) >= window:
            return _escalate(host, name, spec, st, now, f"its fix could not get the lock {res} for "
                                                        f"{(now - float(st['deferred_since'])) / 3600:.1f} h: it stayed busy")
        _save(db, name, st)
        return f"unhealthy (fix deferred: {res} busy)", None
    if frc == FIX_OWN_75:
        frc = st["fix_rc"] = 75   # the fix's own 75 (run_fix), counted like any failed fix
    if frc != 0:
        st["phase"] = None
        return _escalate(host, name, spec, st, now, f"the fix exited {frc}")
    st["phase"] = "settling"
    _save(db, name, st)
    return "unhealthy (fixed; rechecking)", now + spec["settle_s"]


def _escalate(host: Any, name: str, spec: dict, st: dict, now: float, why: str) -> tuple[str, float | None]:
    """Queue the check's one self-fix task (or keep the open one)."""
    db = host.p.db
    # The daemon may have queued a schedule_fix task for the same failure (a check exiting an error
    # code): adopt it, one task per failing check.
    t = open_task(db, name) or open_task(db, name, f"schedule_fix:{name}")
    if t is None:
        fix = spec.get("fix") or "(none)"
        spec_text = (
            f"Heal check `{name}` is unhealthy and its automatic fix cannot repair it: {why}.\n\n"
            f"Check: {spec.get('check') or 'preset ' + str(spec.get('preset'))}\n"
            f"Last check (exit {st.get('last_rc')}): {st.get('last_out') or '(no output)'}\n"
            f"Fix: {fix}\n"
            + (f"Last fix (exit {st.get('fix_rc')}): {st.get('fix_out') or '(no output)'}\n"
               if st.get("fix_rc") is not None else "")
            + f"Unhealthy since {time.strftime('%Y-%m-%d %H:%M', time.localtime(float(st['since'])))}.\n\n"
            "Find the cause and repair it within the charter's restrictions, then make sure the check "
            f"passes (`ttp heal test {name}`). If the fix command itself is wrong, correct the heal block "
            "in harness/schedules.json. Report what you changed. Hand off blocked only for a missing "
            "credential or a step the charter forbids.")
        tid = db.add_task(f"Self-fix: heal check {name} failing", spec_text, kind="work", priority=1,
                          origin="daemon", labels=[LABEL + name])
    else:
        tid = t["id"]
    st.update(task=tid, escalated=now, why=why)
    _save(db, name, st)
    return f"unhealthy (self-fix task #{tid}: {why})", None


def _escalated(host: Any, name: str, spec: dict, st: dict, now: float) -> tuple[str, float | None]:
    """Unhealthy with its self-fix task out: alert once that task failed, blocked or ended without
    making the check pass."""
    db = host.p.db
    tid = st.get("task")
    t = db.task(int(tid)) if tid else None
    if t is not None and t["status"] in OPEN:
        _save(db, name, st)
        return f"unhealthy (self-fix task #{tid} open)", None
    how = f"self-fix task #{tid} {t['status']}" if t else f"self-fix task #{tid} is gone"
    if not st.get("alerted"):
        st["alerted"] = now
        host.alert(STATE_KEY + name,
                   f"Outage: heal check {name} still fails and its {how}. "
                   f"Last check: {(st.get('last_out') or '')[:300]}", "high")
    _save(db, name, st)
    return f"unhealthy ({how}; alerted)", None


def holds(db: DB, name: str) -> bool:
    """The outage alert heal:<name> stays while the check has not passed since (alerts.holds)."""
    row = db.one("SELECT payload FROM schedules WHERE name=? AND enabled=1", (name,))
    if not row or not of(json.loads(row["payload"] or "{}")):
        return False
    return state(db, name).get("status") != "healthy"


# Views --------------------------------------------------------------------------------------------
def checks(db: DB) -> list[tuple[dict, dict]]:
    """Enabled schedules with a heal block: (schedule row, heal spec)."""
    out = []
    for r in db.q("SELECT * FROM schedules WHERE enabled=1 AND kind='command' ORDER BY name"):
        spec = of(json.loads(r["payload"] or "{}"))
        if spec:
            out.append((r, spec))
    return out


def summary(db: DB, now: float | None = None) -> dict:
    n_ok = n_bad = n_unknown = 0
    for r, _ in checks(db):
        s = state(db, r["name"])
        if s.get("status") == "unhealthy" and s.get("escalated"):   # an unreachable box escalated too
            n_bad += 1
        elif "unknown_since" in s:   # the latest reading tells nothing either way
            n_unknown += 1
        elif s.get("status") == "unhealthy":
            n_bad += 1
        elif s.get("status") == "healthy":
            n_ok += 1
        else:
            n_unknown += 1
    return {"ok": n_ok, "fixed": fixed_today(db, now=now), "failing": n_bad, "unknown": n_unknown}


def line(db: DB, now: float | None = None) -> str:
    """'health: N ok, M fixed today, K failing' for ttp status and the web app; "" without checks."""
    if not checks(db):
        return ""
    s = summary(db, now)
    return (f"health: {s['ok']} ok, {s['fixed']} fixed today, {s['failing']} failing"
            + (f", {s['unknown']} unknown" if s["unknown"] else ""))


def digest_lines(db: DB, since: float, now: float | None = None) -> list[str]:
    """The digest's record of checks fixed since the last turn: nothing to do, for the record."""
    now = time.time() if now is None else now
    done = [x for x in db.kv(FIXED_KEY, []) or [] if float(x.get("at") or 0) > since]
    if not done:
        return []
    names = sorted({x["name"] for x in done})
    return ["## Self-healed (nothing to do): " + "; ".join(
        f"fixed {n}, {fixed_today(db, n, now)} today" for n in names)]


def describe(r: dict, spec: dict, db: DB, now: float | None = None) -> str:
    now = time.time() if now is None else now
    s = state(db, r["name"])
    what = spec.get("check") or f"preset {spec.get('preset')}"
    status = s.get("status") or "not checked yet"
    if "unknown_since" in s:
        status = f"unknown (exit {s.get('last_rc')})"
    elif status == "unhealthy" and s.get("since"):
        status += f" for {(now - float(s['since'])) / 60:.0f} min"
    extra = f"; self-fix task #{s['task']}" if s.get("task") else ""
    return (f"{r['name']}: {status}{extra}; {fixed_today(db, r['name'], now)} fixed today; check: {what}; "
            f"fix: {spec.get('fix') or '(none)'}" + (f" under lock {spec['resource']}" if spec.get("resource") else ""))
