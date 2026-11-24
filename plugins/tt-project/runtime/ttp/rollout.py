# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Release rings: a big change ships dark behind a flag that turns on ring by ring.

Ring 0 is throwaway scratch projects, ring 1 a canary set of real projects, ring 2 every other project; an
excluded project never takes a flag. The state is account settings (project.ACCOUNT_KEYS["rollout"], one file
every project on the host reads) plus an append-only ledger in the account home; `ttp rollout` is its only
writer (the daemon's rollback calls the same functions). Nothing here touches a project's charter, memory,
config or state.
- rollout.flags: {flag: "off" | 0 | 1 | 2}, the widest ring that has the flag on (a missing flag is off).
- rollout.rings: {"0": [names], "1": [names]}: a project listed in neither is ring 2. rollout.excluded: [names].
- Precedence (on): a project's own explicit value for the flag's config key (the user's word) wins, else the
  account's ring rule, else off. A feature calls on() at its entry point and then reads its normal config.
- A rolled-back flag stays off and frozen until `ttp rollout arm`, which puts it back at the level it
  had before the rollback (a level set by hand after the rollback wins; a fix landed).
- Waves (a flag marked `waves`): rollout.waves {flag: [[names], ...]}, the user's ordered project lists, and the
  level `auto`: a project is on when it is in waves 0..k (rollout.wave_at {flag: k}, from 0) and not rolled back
  for that flag alone (rollout.project_rollback {flag: {name: {ts, why}}}). wave_tick, model-free and hourly,
  judges the covered projects: a worse one is rolled back alone, a wave whose projects are all ok opens the next.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

LEDGER = "rollout.jsonl"
OFF = "off"
AUTO = "auto"
LEVELS = (OFF, 0, 1, 2, AUTO)
WAVE_EVERY_S = 3600.0   # the wave gate judges a flag at most once an hour (account-wide, WAVE_STATE)
WAVE_STATE = "rollout-waves.json"   # {flag: when the gate last judged it}, in the account home
WAVE_SEEN_KV = "rollout_wave_seen"   # a project's cursor: the ledger time of the last wave record it reported
OVERRIDE_KV = "rollout_override_told"   # a project's {flag: day} of the last override event
# name: the feature's existing config key it gates, its area, and `code`: the files holding its on() call sites
# and the code reading its key, relative to the repo root. The health watch counts a post-landing revert against a
# flag only when the reverted commit touched one of them (rollout_health); a flag with no wiring yet has none.
# `arm`: a model-route flag's model; such a revert also counts only when the change ran on it or a failing test
# exercises the flag's code (rollout_health._reverts), and its other health signals count only the work its route
# ran (rollout_health._Scope). `runs`: "coordinator" when the arm's runs are coordinator turns, not task runs.
# `shadow` marks a flag that only records beside the real behaviour (it posts nothing, changes no run and makes no
# model call), so no health signal judges it (rollout_health.signals). A flag whose feature already runs for every
# project graduates (leaves this list), never gating it off: push_isolation, the push batch survival (a reliability
# fix).
_RT = "plugins/tt-project/runtime/ttp/"


def _code(*names: str) -> tuple[str, ...]:
    return tuple(_RT + n for n in names)


FLAGS: dict[str, dict[str, Any]] = {
    "glm_code": {"key": "route.code_sonnet.glm_share", "area": "route", "code": _code("codearm.py"), "waves": True,
                 "arm": "glm"},
    # The project's routed work on GLM (glmwork.py); `waves`: it may run at auto, judged by glmshare.judge.
    "glm_work": {"key": "route.glm_work.on", "area": "route", "code": _code("glmwork.py", "codearm.py", "daemon.py"),
                 "waves": True, "arm": "glm"},
    # Device work on GLM under an Opus plan (deviceplan.py, #3728); needs glm_work on too. Judged by
    # deviceplan.judge; one harmful step GLM took on its own rolls the project back at once (stop_project).
    "glm_device": {"key": "route.glm_work.device", "area": "route", "waves": True, "judge": "deviceplan",
                   "code": _code("deviceplan.py", "glmwork.py", "glmassist.py", "hook.py"), "arm": "glm"},
    # Routine coordinator wakes on the chat model (coordroute.py): a ring runs them in shadow only (a read-only
    # lane turn beside the coordinator's own, nothing applied); live mode is the project's own setting.
    "glm_coord": {"key": "coordinator.routine.mode", "area": "coordinator", "code": _code("coordroute.py", "daemon.py"),
                  "arm": "glm", "runs": "coordinator"},
    "eta_history": {"key": "eta.history", "area": "eta", "shadow": True,
                    "code": _code("etaledger.py", "costlevers.py", "cli.py")},
    "deslop": {"key": "deslop.enabled", "area": "review", "code": _code("deslop.py")},
    # A cost lever (costlevers.py): off until a ring turns it on.
    "coord_wake_settle": {"key": "coordinator.wake_settle", "area": "coordinator",
                          "code": _code("costlevers.py", "wakesettle.py", "daemon.py")},
}


def _home():
    from . import project
    return project.HOME_DIR


def settings() -> dict:
    from .project import load_account_settings
    return load_account_settings().get("rollout") or {}


def level(flag: str, cfg: dict | None = None) -> Any:
    v = ((cfg if cfg is not None else settings()).get("flags") or {}).get(flag, OFF)
    if v == AUTO:
        return AUTO
    return v if v in (0, 1, 2) and not isinstance(v, bool) else OFF


def ring_of(project: str, cfg: dict | None = None) -> int | None:
    """0, 1 or 2 (a project in no list); None for an excluded project."""
    cfg = settings() if cfg is None else cfg
    if project in (cfg.get("excluded") or []):
        return None
    rings = cfg.get("rings") or {}
    return next((r for r in (0, 1) if project in (rings.get(str(r)) or [])), 2)


def own_setting(raw: dict | None, key: str) -> bool:
    """Whether the project's own project.json sets the flag's config key (the user's word)."""
    node: Any = raw
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node is not None


def on(flag: str, project: str, raw: dict | None = None, cfg: dict | None = None) -> bool:
    """Whether `flag` is on for `project`. `raw`: its project.json, whose own explicit value for the flag's
    key wins (the feature then reads that value). An unknown flag is off."""
    if flag not in FLAGS:
        return False
    if own_setting(raw, FLAGS[flag]["key"]):
        return True
    cfg = settings() if cfg is None else cfg
    lv, ring = level(flag, cfg), ring_of(project, cfg)
    if ring is None or project in rolled_back(flag, cfg):
        return False
    if lv == AUTO:
        return any(project in w for w in waves(flag, cfg)[:wave_at(flag, cfg) + 1])
    return lv != OFF and ring <= lv


def waves(flag: str, cfg: dict | None = None) -> list[list[str]]:
    """The flag's ordered project lists (`ttp rollout waves`); [] without any."""
    got = ((cfg if cfg is not None else settings()).get("waves") or {}).get(flag)
    return [[str(n) for n in w] for w in got if isinstance(w, list)] if isinstance(got, list) else []


def wave_at(flag: str, cfg: dict | None = None) -> int:
    """The flag's current wave: waves 0..it are covered under auto (0 to start)."""
    cfg = settings() if cfg is None else cfg
    k = (cfg.get("wave_at") or {}).get(flag, 0)
    k = k if isinstance(k, int) and not isinstance(k, bool) else 0
    return max(0, min(k, len(waves(flag, cfg)) - 1))


def rolled_back(flag: str, cfg: dict | None = None) -> dict:
    """{project: {ts, why}}: the projects rolled back for `flag` alone, by the wave gate or the health watch, at
    any level (until `arm FLAG PROJECT`)."""
    got = ((cfg if cfg is not None else settings()).get("project_rollback") or {}).get(flag)
    return dict(got) if isinstance(got, dict) else {}


def stop_project(flag: str, project: str, why: str, by: str = "daemon") -> bool:
    """Roll `flag` back in `project` alone now, outside the hourly wave gate (deviceplan's stop, the daemon's health
    watch); True when it was not yet. It stays off there until `ttp rollout arm FLAG PROJECT`; the other projects
    keep it."""
    _check(flag)
    with _locked():
        back = rolled_back(flag)
        if project in back:
            return False
        back[project] = {"ts": round(time.time(), 3), "why": str(why)[:300]}
        _put("project_rollback", flag, back)
        _op("project_rollback", flag, str(why)[:300], by, project=project)
    return True


WHO = {"user": "the user", "run": "a run"}   # the ledger's `by` values, as the digest words them


DIGEST_HEADER = "## Rollout flags (the state is the signal: never hold flag-gated work for a note or someone's word)"


def state_line(project: str, raw: dict | None = None) -> str:
    """Every flag's state for `project` on one line (its own setting for the flag's key wins)."""
    return ", ".join(f"{f}: {'on' if on(f, project, raw) else 'off'}" for f in FLAGS)


def change_lines(project: str, since: float, raw: dict | None = None, now: float | None = None) -> list[str]:
    """Every ledger change since `since` that moves a flag for `project` (an account-wide one, or its
    own), one line each: the flag, its new state, who and when. Model-free, from the shared ledger."""
    from . import timefmt
    out = []
    for r in ledger():
        ts, kind, flag = r.get("ts") or 0, r.get("kind"), r.get("flag") or ""
        if ts <= since or kind in ("clean", "waves"):
            continue
        at, who = timefmt.short(float(ts), raw, now), WHO.get(r.get("by") or "") or r.get("by") or "the terminal"
        if kind == "project_rollback" and r.get("project") == project:
            out.append(f"{flag} rolled back for this project by {who} at {at} ({r.get('why')})")
        elif kind == "project_arm" and r.get("project") == project:
            out.append(f"{flag} re-armed for this project by {who} at {at} ({r.get('why')})")
        elif kind == "advance" and project in (r.get("projects") or []):
            out.append(f"{flag} opened wave {r.get('wave')}, which holds this project: it is on here now")
        elif kind in ("set", "promote", "rollback", "arm"):
            to = "off (frozen)" if kind == "rollback" \
                else str(r.get("to")) + (", unfrozen" if kind == "arm" else "")
            out.append(f"{flag} set to {to} by {who} at {at}"
                       + (f" ({r.get('reason')})" if r.get("reason") else ""))
    return out


def digest_lines(p, since: float, now: float | None = None) -> list[str]:
    """The coordinator's rollout section: each flag's state for this project, and every ledger change
    since its last wake. Model-free, from the shared ledger; the project's own settings still win."""
    raw = p.raw_config()
    return [DIGEST_HEADER, "- " + state_line(p.name, raw)] \
        + [f"- {line}" for line in change_lines(p.name, since, raw, now)]


def ledger(flag: str | None = None) -> list[dict]:
    out = []
    try:
        with open(_home() / LEDGER, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and (flag is None or r.get("flag") == flag):
                    out.append(r)
    except OSError:
        pass
    return out


def frozen(flag: str) -> dict | None:
    """The ledger's last record for `flag` when it is a rollback (the flag waits for `arm`), else None."""
    recs = [r for r in ledger(flag) if r.get("kind") in ("set", "promote", "rollback", "arm")]
    return recs[-1] if recs and recs[-1].get("kind") == "rollback" else None


@contextmanager
def _locked() -> Iterator[None]:
    """One writer at a time: the daemon's rollback and a terminal's command read-modify-write one file."""
    from .project import HOME_DIR
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(HOME_DIR / "rollout.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _record(kind: str, flag: str, old: Any, new: Any, reason: str, by: str, snapshot: dict | None = None) -> None:
    from .project import durable_append
    rec = {"ts": round(time.time(), 3), "kind": kind, "flag": flag, "from": old, "to": new, "reason": reason,
           "by": by, "snapshot": snapshot or {}}
    durable_append(_home() / LEDGER, json.dumps(rec, sort_keys=True) + "\n")


def _put(section: str, key: str, value: Any) -> None:
    from .project import set_account_setting
    cur = dict(settings().get(section) or {})
    if value is None:
        cur.pop(key, None)
    else:
        cur[key] = value
    set_account_setting(f"rollout.{section}", cur or None)


def _op(op: str, flag: str, why: str, by: str, **extra: Any) -> dict:
    """A wave record in the ledger ({ts, op, flag, why, by, ...}; `kind` too, so ledger readers list it)."""
    from .project import durable_append
    rec = {"ts": round(time.time(), 3), "op": op, "kind": op, "flag": flag, "why": why, "by": by, **extra}
    durable_append(_home() / LEDGER, json.dumps(rec, sort_keys=True, default=str) + "\n")
    return rec


def _check(flag: str) -> None:
    if flag not in FLAGS:
        raise ValueError(f"unknown flag {flag!r}; flags: {', '.join(sorted(FLAGS))}")


def parse_level(text: Any) -> Any:
    t = str(text).strip().lower()
    if t in (OFF, AUTO):
        return t
    if t in ("0", "1", "2"):
        return int(t)
    raise ValueError(f"a flag level is off, 0, 1, 2 or auto, not {text!r}")


def set_level(flag: str, to: Any, reason: str, by: str = "user", kind: str = "set", snapshot: dict | None = None) -> str:
    """Set `flag` to `to` (off, 0, 1, 2), ledgered. ValueError: an unknown flag or a frozen one (arm it first)."""
    _check(flag)
    to = parse_level(to)
    with _locked():
        old = level(flag)
        if kind != "rollback" and to != OFF and frozen(flag):
            raise ValueError(f"{flag} was rolled back ({frozen(flag).get('reason')}) and is frozen: "
                             f"`ttp rollout arm {flag}` once a fix landed")
        if old == to:
            return f"{flag} is {to} already; nothing changed"
        if to == AUTO and not FLAGS[flag].get("waves"):
            raise ValueError(f"{flag} has no wave gate; auto is for {', '.join(f for f in FLAGS if FLAGS[f].get('waves'))}")
        if to == AUTO and not waves(flag):
            raise ValueError(f"{flag} has no waves: set them first (`ttp rollout waves {flag} <p1,p2> <p3> ...`)")
        _put("flags", flag, None if to == OFF else to)
        _record(kind, flag, old, to, reason, by, snapshot)
    return f"{flag}: {old} -> {to}"


def promote(flag: str, reason: str = "", by: str = "user") -> str:
    """One ring wider: off -> 0 -> 1 -> 2."""
    cur = level(flag)
    if cur == AUTO:
        return f"{flag} is at auto: its waves widen it (`ttp rollout status`)"
    nxt = 0 if cur == OFF else cur + 1
    if nxt > 2:
        return f"{flag} is on in every ring already"
    return set_level(flag, nxt, reason or "promote", by, "promote")


def rollback(flag: str, reason: str, by: str = "user", snapshot: dict | None = None) -> str:
    """Turn `flag` off for every project and freeze it until `arm`: `ttp rollout rollback` from the user's
    terminal. A health failure in one project switches the flag off there alone (stop_project)."""
    _check(flag)
    with _locked():
        if by.startswith("daemon") and frozen(flag):   # another project's daemon got there first
            return f"{flag} is rolled back already"
        old = level(flag)
        _put("flags", flag, None)
        _record("rollback", flag, old, OFF, reason or "rollback", by, snapshot)
    return f"{flag}: {old} -> off (frozen until `ttp rollout arm {flag}`)"


def vote(flag: str, project: str, snapshot: dict) -> None:
    """A project's clean health vote (ledgered, `reason` leads with its name); promotion needs one per
    project listed in the flag's ring."""
    with _locked():
        since = max((r.get("ts") or 0 for r in ledger(flag) if r.get("kind") in ("set", "promote", "rollback", "arm")),
                    default=0)   # one vote per project and stretch of the flag
        if not frozen(flag) and not any(r.get("kind") == "clean" and r.get("reason") == f"{project} clean"
                                        and (r.get("ts") or 0) >= since for r in ledger(flag)):
            _record("clean", flag, level(flag), level(flag), f"{project} clean", f"daemon:{project}", snapshot)


def armed_at(project: str, *flags: str) -> float:
    """When the terminal last re-armed `project` for any of `flags` (0: never): the judged window of its outcomes
    starts there, so the outcomes that led to the rollback never count against the fix."""
    return max((float(r.get("ts") or 0) for f in flags for r in ledger(f)
                if r.get("op") == "project_arm" and r.get("project") == project), default=0.0)


def arm(flag: str, reason: str = "", by: str = "user", project: str = "", evidence: dict | None = None) -> str:
    """Unfreeze a rolled-back flag, back at the level it had before the rollback (a level set by hand
    after the rollback wins); with `project`, re-arm that project's own rollback. `evidence`: what a
    run's re-arm checked, written into the ledger record ("" from the user's terminal)."""
    _check(flag)
    if project:
        with _locked():
            back = rolled_back(flag)
            if project not in back:
                return f"{flag} is not rolled back in {project}; nothing changed"
            back.pop(project)
            _put("project_rollback", flag, back or None)
            _op("project_arm", flag, reason or "fix landed", by, project=project, **(evidence or {}))
        p = local_projects().get(project)
        return f"{flag} re-armed in {project}: {'on' if on(flag, project, _raw(p) if p else None) else 'off'} for it again"
    with _locked():
        fz = frozen(flag)
        if not fz:
            return f"{flag} is not frozen; nothing changed"
        restore = fz.get("from")   # the ledger's rollback record: the level the flag had before it
        restore = restore if restore in LEVELS else OFF
        hand = level(flag)   # a non-off level here is one set by hand after the rollback: it wins
        # The level actually left in force: the hand-set one, else the restored one when it applies
        # (auto only while waves exist: with none it turns no project on), else off.
        left = hand if hand != OFF else restore if restore != OFF and (restore != AUTO or waves(flag)) else OFF
        _record("arm", flag, OFF, left, reason or "fix landed", by)
        if left not in (OFF, hand):
            _put("flags", flag, left)
    if left == OFF:
        return f"{flag} re-armed (off); promote it from ring 0"
    return f"{flag} re-armed at {left}, " + ("its level before the rollback" if left == restore
                                              else "the level set by hand after the rollback")


def set_ring(project: str, ring: str, by: str = "user") -> str:
    """ring: 0, 1, 2 (listed in neither: the default), exclude or include."""
    if ring not in ("0", "1", "2", "exclude", "include"):
        raise ValueError(f"a ring is 0, 1, 2, exclude or include, not {ring!r}")
    with _locked():
        before = ring_of(project)
        rings = {k: [n for n in (v or []) if n != project] for k, v in (settings().get("rings") or {}).items()}
        if ring in ("0", "1"):
            rings[ring] = rings.get(ring, []) + [project]
        excl = [n for n in (settings().get("excluded") or []) if n != project]
        if ring == "exclude":
            excl.append(project)
        _put("rings", "0", rings.get("0") or None)
        _put("rings", "1", rings.get("1") or None)
        from .project import set_account_setting
        set_account_setting("rollout.excluded", excl or None)
        _record("ring", "", before, ring_of(project), f"{project} -> {ring}", by)
    return f"{project}: ring {before} -> {ring_of(project)}"


def set_waves(flag: str, args: list[str], by: str = "user") -> str:
    """The flag's waves from `args` (each one wave, comma-separated project names), or `clear`. A project is in
    one wave only. The current wave stays (cut to the last wave); clearing resets it and is refused at auto."""
    _check(flag)
    if not FLAGS[flag].get("waves"):
        raise ValueError(f"{flag} has no wave gate; waves are for {', '.join(f for f in FLAGS if FLAGS[f].get('waves'))}")
    new = [] if [a.strip().lower() for a in args] == ["clear"] else \
        [[n.strip() for n in a.split(",") if n.strip()] for a in args]
    if not args or any(not w for w in new):
        raise ValueError("each wave names one or more projects, comma-separated: <p1,p2> <p3> ... (or clear)")
    names = [n for w in new for n in w]
    if len(names) != len(set(names)):
        raise ValueError(f"a project is in one wave only: {', '.join(sorted({n for n in names if names.count(n) > 1}))}")
    with _locked():
        cfg = settings()
        if not new and level(flag, cfg) == AUTO:
            raise ValueError(f"{flag} is at auto: set it to another level before clearing its waves")
        old, k = waves(flag, cfg), wave_at(flag, cfg)
        if old == new:
            return f"{flag}: waves unchanged"
        _put("waves", flag, new or None)
        _put("wave_at", flag, (min(k, len(new) - 1) or None) if new else None)
        _record("waves", flag, old, new, " | ".join(",".join(w) for w in new) or "cleared", by)
    return f"{flag}: waves " + (" | ".join(",".join(w) for w in new) or "cleared")


def local_projects() -> dict:
    """{name: Project} of the projects `ttp list` shows on this host (the registry); opened, never written."""
    from pathlib import Path
    from .project import Project, hostname, load_registry
    out = {}
    for name, e in sorted((load_registry().get("projects") or {}).items()):
        if isinstance(e, dict) and e.get("host") in (None, hostname()) and e.get("dir") and Path(e["dir"]).is_dir():
            p = Project(e["dir"])
            if p.exists():
                out[name] = p
    return out


def _raw(p) -> dict:
    """A project's own project.json, read only ({} when unreadable)."""
    from . import statcache
    try:
        data = statcache.load_json(p.config_path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _wave_state() -> dict:
    try:
        data = json.loads((_home() / WAVE_STATE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def wave_tick(now: float | None = None, judge=None, projects: dict | None = None, by: str = "daemon") -> list[dict]:
    """The wave gate, model-free: for each flag at auto not judged in the last hour, judge each project of its
    covered waves (`judge(p, cfg, now)`: glmshare.judge, ok | worse | wait). A worse one is rolled back for that
    flag alone (the others stay); when every project left in the current wave is ok (and none went worse), the
    next wave opens. The ledger records each move; returns those records. The hour limit and the lock make a
    call from every project's daemon safe; judging runs outside the lock."""
    from .project import write_json
    now = time.time() if now is None else now
    flags = [f for f in FLAGS if level(f) == AUTO]
    if not flags:
        return []
    with _locked():
        seen = _wave_state()
        due = [f for f in flags if not 0 <= now - float(seen.get(f) or 0) < WAVE_EVERY_S]
        if not due:
            return []
        seen.update({f: now for f in due})
        write_json(_home() / WAVE_STATE, seen, mode=0o600)
    projs = local_projects() if projects is None else projects
    out: list[dict] = []
    for flag in due:
        cfg = settings()
        ws, k, back = waves(flag, cfg), wave_at(flag, cfg), rolled_back(flag, cfg)
        excl = set(cfg.get("excluded") or [])
        verdicts: dict[str, dict] = {}
        try:
            from . import deviceplan, glmshare
            fj = judge or (deviceplan.judge if FLAGS.get(flag, {}).get("judge") == "deviceplan" else glmshare.judge)
        except ImportError:
            continue
        for name in (n for w in ws[:k + 1] for n in w if n not in excl and n not in back):
            p = projs.get(name)
            if p is None:
                verdicts[name] = {"state": "wait", "why": "not a project on this host", "n": 0}
                continue
            try:
                from .project import layered
                v = fj(p, layered(_raw(p)), now)
            except Exception as e:   # a judge that fails waits; it never rolls back or widens
                v = {"state": "wait", "why": f"judge failed: {type(e).__name__}: {e}"[:200], "n": 0}
            verdicts[name] = v if isinstance(v, dict) else {"state": "wait", "why": "no verdict", "n": 0}
        with _locked():
            cfg = settings()
            if level(flag, cfg) != AUTO or waves(flag, cfg) != ws or wave_at(flag, cfg) != k:
                continue   # the user changed it meanwhile: the next hour judges the new state
            back, worse = rolled_back(flag, cfg), []
            for name, v in verdicts.items():
                if v.get("state") == "worse" and name not in back:
                    why = str(v.get("why") or "worse than its own baseline")[:300]
                    back[name] = {"ts": round(now, 3), "why": why}
                    worse.append(name)
                    # the verdict's comparison rides along on the ledger line: both pass rates and the window
                    out.append(_op("project_rollback", flag, why, by, project=name, n=v.get("n"),
                                   **{k: v[k] for k in ("glm_pct", "base_pct", "base_window") if k in v}))
            if worse:
                _put("project_rollback", flag, back)
            left = [n for n in ws[k] if n not in excl and n not in back]
            if not worse and k + 1 < len(ws) and left and all(verdicts.get(n, {}).get("state") == "ok" for n in left):
                _put("wave_at", flag, k + 1)
                why = f"wave {k} no worse: " + "; ".join(f"{n}: {verdicts[n].get('why') or 'ok'}" for n in left)
                out.append(_op("advance", flag, why[:600], by, wave=k + 1,
                               projects=[n for n in ws[k + 1] if n not in excl]))
    return out


def override_since(p, key: str) -> float | None:
    """When the project's own `key` was last set: its config change log (configwho), else project.json's mtime."""
    from . import configwho
    rows = [r for r in configwho.history(p, key) if not r.get("refused") and "new" in r]
    if rows:
        try:
            return float(rows[-1]["ts"])
        except (TypeError, ValueError):
            pass
    try:
        return p.config_path.stat().st_mtime
    except OSError:
        return None


def overrides(projects: dict | None = None, cfg: dict | None = None) -> dict[str, list[dict]]:
    """{flag: [{project, value, since, at}]}: for each flag the user turned on, the projects whose own project.json
    sets its key, which wins over the rollout (e.g. a leftover glm_share 0 under glm_code)."""
    from . import configwho, timefmt
    cfg = settings() if cfg is None else cfg
    on_flags = [f for f in FLAGS if level(f, cfg) != OFF]
    out: dict[str, list[dict]] = {}
    if not on_flags:
        return out
    for name, p in sorted((local_projects() if projects is None else projects).items()):
        raw = _raw(p)
        for f in on_flags:
            key = FLAGS[f]["key"]
            if own_setting(raw, key):
                since = override_since(p, key)
                out.setdefault(f, []).append({"project": name, "value": configwho.at(raw, key), "since": since,
                                              "at": timefmt.long(since, raw) if since else "an unknown time"})
    return out


def project_events(p, now: float | None = None) -> list[str]:
    """This project's rollout events for its coordinator, model-free, from its own daemon (its own db only):
    one per wave record about it not yet reported (WAVE_SEEN_KV), and one a day per flag the user turned on
    that its own setting overrides (OVERRIDE_KV). Returns the event kinds added."""
    from . import configwho, timefmt
    now = time.time() if now is None else now
    db, name, done = p.db, p.name, []

    def add(kind: str, fp: str, text: str) -> None:
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", kind, fp, "normal", text[:1500], "queued"))
        done.append(kind)

    try:
        seen = float(db.kv(WAVE_SEEN_KV, 0) or 0)
    except (TypeError, ValueError):
        seen = 0.0
    last = seen
    for r in ledger():
        ts = r.get("ts") or 0
        if not isinstance(ts, (int, float)) or ts <= seen or r.get("op") not in ("advance", "project_rollback"):
            continue
        last = max(last, ts)
        flag = r.get("flag")
        if r["op"] == "advance" and name in (r.get("projects") or []):
            add("rollout_wave", f"rollout_wave:{flag}:{r.get('wave')}",
                f"Release flag {flag} opened wave {r.get('wave')}, which holds this project: it is on here now "
                f"(its own setting for {FLAGS.get(flag, {}).get('key')} still wins). Why: {r.get('why')}")
        elif r["op"] == "project_rollback" and r.get("project") == name:
            add("rollout_wave", f"rollout_wave_rollback:{flag}",
                f"Release flag {flag} was rolled back for this project alone ({r.get('why')}); the other projects "
                f"keep it. It stays off here until `ttp rollout arm {flag} {name}` once a fix landed.")
    if last > seen:
        db.set_kv(WAVE_SEEN_KV, last)
    told = db.kv(OVERRIDE_KV, {}) or {}
    told = told if isinstance(told, dict) else {}
    raw, cfg = _raw(p), settings()
    day = timefmt.today(now, raw)
    for f, m in FLAGS.items():
        if level(f, cfg) == OFF or not own_setting(raw, m["key"]) or told.get(f) == day:
            continue
        since = override_since(p, m["key"])
        add("rollout_override", f"rollout_override:{f}",
            f"Release flag {f} is on for the account ({level(f, cfg)}), but this project's own {m['key']} = "
            f"{json.dumps(configwho.at(raw, m['key']))} overrides it (set {timefmt.long(since, raw) if since else 'at an unknown time'}). "
            f"Clear it (`ttp config {name} {m['key']} ''`) to follow the rollout, or keep it if it is deliberate.")
        told[f] = day
    if told != (db.kv(OVERRIDE_KV, {}) or {}):
        db.set_kv(OVERRIDE_KV, told)
    return done


SCRATCH_CAP_USD = 25.0
SCRATCH_NOTE = "held: scratch project's daily spend cap,"


def scratch_state(db, name: str, own: dict | None = None, now: float | None = None) -> tuple[float, float] | None:
    """(daily cap, spend in the last 24 h) for a ring-0 scratch project, else None. The cap is the project's own
    `rollout.scratch_cap_usd` (the user's word), else the account's, else $25; 0 turns it off."""
    cfg = settings()
    if name not in ((cfg.get("rings") or {}).get("0") or []):
        return None
    cap = next((float(v) for v in ((own or {}).get("scratch_cap_usd"), cfg.get("scratch_cap_usd"))
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0), SCRATCH_CAP_USD)
    return (cap, db.spent_since((now or time.time()) - 86400)) if cap else None


def scratch_hold(state: tuple[float, float] | None, task: dict, owners=()) -> str | None:
    """Why a scratch project's task waits: it is past its daily cap. A soft cap: an urgent task or a user-ordered
    one (urgent.user_ordered) never waits on it, and the project's dollar caps are untouched."""
    from . import urgent
    if not state or state[1] < state[0] or urgent.user_ordered(task, owners) or urgent.is_urgent(task):
        return None
    return f"{SCRATCH_NOTE} ${state[1]:.2f} of ${state[0]:g} used; it starts when the day's spend drops under it"


def status(projects: dict | None = None) -> dict:
    """The rings, each flag's level and, per flag: its waves, current wave and per-project rollbacks, and the
    projects whose own setting overrides it while it is on (overrides)."""
    cfg = settings()
    over = overrides(projects, cfg)
    return {"rings": cfg.get("rings") or {}, "excluded": cfg.get("excluded") or [],
            "flags": {f: {"level": level(f, cfg), "key": m["key"], "frozen": bool(frozen(f)),
                          "shadow": bool(m.get("shadow")), "waves": waves(f, cfg), "wave": wave_at(f, cfg),
                          "rolled_back": rolled_back(f, cfg), "overrides": over.get(f, []),
                          "armed": next((r for r in reversed(ledger(f))
                                         if r.get("op") == "project_arm" and r.get("run")), None)}
                      for f, m in sorted(FLAGS.items())}}



RUN_REARM_KEY = "run_rearm"   # rollout.*: whether a run may re-arm one project with evidence


def rearm_refusal(a) -> tuple[str, dict]:
    """(why a run may not re-arm, the evidence its re-arm records) for `arm FLAG PROJECT --fix TASK
    --replay RUN`: the setting `rollout.run_rearm` must be on, the fix task's change must be on the
    push branch and deployed to PROJECT, and the replay of the rollback's cause must have passed.
    Unfreezing a whole flag, rings, waves and promotion stay with the user."""
    project = (a.value or "").strip()
    fix, replay = getattr(a, "fix", None), (getattr(a, "replay", "") or "").strip()
    if not project:
        return "a run re-arms one rolled-back project; unfreezing a flag stays with the user", {}
    if not settings().get(RUN_REARM_KEY):
        return f"the account setting rollout.{RUN_REARM_KEY} is off", {}
    if not fix or not replay:
        return "a run's re-arm carries its evidence: --fix TASK and --replay RUN", {}
    from . import landed, project as project_mod, release
    p = project_mod.here()
    if p is None:
        return "no project here to read the fix task's landing from", {}
    if a.target and project not in rolled_back(a.target):
        return f"{project} is not rolled back for {a.target}; nothing to re-arm", {}
    code, why = landed.check(p, p.root, tid=fix, fetch=False)
    if code != 0:
        return f"fix task #{fix} is not on the push branch ({why})", {}
    target = local_projects().get(project)
    if target is None:
        return f"{project} is not registered on this machine", {}
    commit = release.runtime_commit(target.harness / "runtime")
    if commit in (None, "", "unknown"):
        return f"cannot tell what {project} runs (no installed release)", {}
    shas, _ = landed.task_shas(p, fix)
    if not any(landed.on_branch(p.root, sha, commit) for sha in shas):
        return f"{project} runs {commit[:12]}, which does not contain fix task #{fix}", {}
    why = rearm_replay(replay, target, p)
    if why:
        return why, {}
    return "", {"run": os.environ.get("TTP_RUN_ID") or "", "run_project": os.environ.get("TTP_PROJECT") or "",
                "run_task": os.environ.get("TTP_TASK") or "", "fix": fix, "deployed": commit,
                "replay": replay}


def rearm_replay(ref: str, target, p) -> str:
    """Why `--replay REF` is no evidence of a passed replay ("" when it is): REF is a result.json
    path, or a run id read from the re-armed project's runs, else this project's, and the run's
    hand-off must say the replay passed ("done")."""
    from pathlib import Path
    path = Path(ref)
    if not path.is_file() and ref.isdigit():
        path = next((q.runs / ref / "result.json" for q in (target, p)
                     if (q.runs / ref / "result.json").is_file()), path)
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return f"no passing replay: {ref} names neither a result file nor a run here"
    except ValueError:
        return f"no passing replay: {ref} is not a result record"
    if not isinstance(data, dict) or data.get("status") != "done":
        status = data.get("status") if isinstance(data, dict) else "nothing"
        return f"no passing replay: {ref} did not pass (its result says {status})"
    return ""


def run(a) -> int:
    """`ttp rollout ACTION ...`: the terminal's (the user's) edit of the rollout state. A run's own call may
    only read it (`status`, or `on`, a wait's probe), roll a flag back (that only reduces a flag's reach)
    or re-arm one project (rearm_refusal: on the evidence its fix landed, deployed and its replay passed);
    promotion is the watcher's, the wave moves the gate's, and the ring and wave lists are the user's."""
    in_run = bool(os.environ.get("TTP_RUN_DIR"))
    act = a.action
    evidence: dict = {}
    if in_run and act == "arm":   # one project, on evidence; everything else stays the user's
        why, evidence = rearm_refusal(a)
        if why:
            print(f"ttp rollout arm: {why}", flush=True)
            return 2
    elif in_run and act not in ("status", "rollback", "on"):
        print(f"ttp rollout {act}: only the user's terminal changes the rollout state; a run may read it "
              "(status, on), roll a flag back or re-arm one project on evidence", flush=True)
        return 2
    try:
        if act == "on":
            # A wait's probe: 0 when FLAG is on for PROJECT (its own setting wins), 1 when it is not.
            # A task held only for the flag then wakes on its own: the daemon polls its retry_when.
            from . import project as project_mod
            _check(a.target)
            who = a.value or ""
            # PROJECT is a name: its registry entry's folder (the CLI's own NAME convention), or a path.
            p = local_projects().get(who) or (project_mod.Project(who) if who else project_mod.here())
            if who and not p.exists():
                print(f"ttp rollout on: no project {who} to read the flag for", flush=True)
                return 2   # a broken probe: a typo in the name must not read another project's flag
            if p is None:
                print("ttp rollout on: no project here to read the flag for (name it: "
                      "`ttp rollout on FLAG PROJECT`)", flush=True)
                return 2
            raw = p.raw_config()
            got = on(a.target, p.name, raw)
            print(f"{a.target} is {'on' if got else 'off'} for {p.name}", flush=True)
            return 0 if got else 1
        if act == "status":
            st = status()
            if a.json:
                print(json.dumps(st, indent=1))
                return 0
            print("rings: " + (", ".join(f"{k}: {','.join(v)}" for k, v in sorted(st["rings"].items())) or "none")
                  + "; excluded: " + (", ".join(st["excluded"]) or "none"))
            for f, e in st["flags"].items():
                print(f"{f}: {e['level']}{' (frozen)' if e['frozen'] else ''}  gates {e['key']}"
                      + ("  (shadow: the health watch never rolls it back)" if e["shadow"] else ""))
                if e["waves"]:
                    print("  waves: " + " | ".join(("> " if i == e["wave"] else "") + ",".join(w)
                                                    for i, w in enumerate(e["waves"]))
                          + (f"  (auto: waves 0..{e['wave']} on)" if e["level"] == AUTO else ""))
                for n, r in sorted(e["rolled_back"].items()):
                    print(f"  rolled back in {n} alone: {r.get('why')}  (`ttp rollout arm {f} {n}`)")
                if e.get("armed"):
                    rec = e["armed"]
                    print(f"  last re-armed in {rec.get('project')} by a run: fix #{rec.get('fix')}, "
                          f"deployed {str(rec.get('deployed'))[:12]}, replay {rec.get('replay')}")
                for o in e["overrides"]:
                    print(f"  overridden in {o['project']} by its own setting since {o['at']} "
                          f"({e['key']} = {json.dumps(o['value'])})")
            return 0
        reason = (a.reason or "").strip()
        more = [a.value, *(getattr(a, "more", None) or [])] if a.value is not None else []
        if not a.target or (act in ("ring", "set", "waves") and a.value is None):
            print(f"ttp rollout {act}: needs " + {"ring": "PROJECT 0|1|2|exclude|include", "set": "FLAG off|0|1|2|auto",
                                                   "waves": "FLAG <p1,p2> <p3> ... | clear"}
                  .get(act, "FLAG"), flush=True)
            return 2
        if act != "waves" and len(more) > 1:
            print(f"ttp rollout {act}: one value, not {' '.join(more)}", flush=True)
            return 2
        if act == "ring":
            print(set_ring(a.target, a.value))
        elif act == "set":
            print(set_level(a.target, a.value, reason or "set by hand"))
        elif act == "promote":
            print(promote(a.target, reason))
        elif act == "rollback":
            print(rollback(a.target, reason or "rolled back by hand", "run" if in_run else "user"))
        elif act == "arm":
            print(arm(a.target, reason, "run" if in_run else "user", project=a.value or "", evidence=evidence))
        elif act == "waves":
            print(set_waves(a.target, more))
    except ValueError as e:
        print(f"ttp rollout {act}: {e}", flush=True)
        return 2
    return 0
