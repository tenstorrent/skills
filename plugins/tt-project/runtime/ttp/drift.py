# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model-free drift flags for running worker and reviewer runs, read from runwatch's record (each
runwatch tick, Daemon.watch_runs), with a cheap ladder before the coordinator spends a turn on them.

Flags, each with its threshold under coordinator.drift.<name> (0 turns that flag off):
- repeat_fail (3): the same normalised shell command (runwatch.norm_command) failed this many times;
- loop (4): the same tool input (its hash) this many times among the run's last LOOP_WINDOW calls;
- outside_spec (3): this many changed files outside the paths the task spec names and their folders,
  only when it names some; tests, changesets and docs never count;
- quiet_s (900): no tool call or result, note or commit for this long while no `ttp lock`, `ttp checks`
  or detach wait is pending (the runner's stall_s still ends a stalled run later);
- pace (1.5): past PACE_FLOOR of the budget, the run's share of the task budget is over this times the
  share of its wall limit used; or over PACE_NO_COMMIT of the budget is spent with no commit.

Ladder, once per flag per run: 1) an update in the run's steer.md naming the evidence; 2) still flagged
raise_s (600) later, one ride-along `worker_drift` event to the coordinator; 3) only for pace past
WAKE_PACE of the budget or a command failed WAKE_REPEAT_FAIL times, one normal event that wakes it.
Active flags go in the run's runwatch entry as "flags" ({flag: evidence}); "drift" keeps the ladder.
Unread updates are steerack's own event; nothing here repeats it.
"""
from __future__ import annotations

import json
import posixpath
import re
from collections import Counter
from pathlib import Path

from . import pagesev

KIND = "worker_drift"
DEFAULTS = {"repeat_fail": 3, "loop": 4, "outside_spec": 3, "quiet_s": 900, "pace": 1.5, "raise_s": 600}
FLAGS = ("repeat_fail", "loop", "outside_spec", "quiet", "pace")
LOOP_WINDOW = 30
PACE_FLOOR = 0.25       # a run's share of the budget below this never counts as fast
PACE_NO_COMMIT = 0.8
WAKE_PACE = 0.9
WAKE_REPEAT_FAIL = 6
# a pending call that waits on purpose: a lock, the checks, a detached job or another queue
WAIT_RE = re.compile(r"\bttp\s+(?:lock|checks|detach|devq|pool-wait|push|land|ci|ack-wait|host-run)\b")
_PATH_RE = re.compile(r"(?<![\w/:.-])(?:[\w.-]+/)+[\w.-]*|(?<![\w/.-])[\w-]+\.(?:py|md|js|ts|json|sh|toml|ya?ml|txt|cfg)\b")
_FREE_RE = re.compile(r"(?:^|/)(?:tests?|docs?|\.changeset)/|(?:^|/)(?:test_[^/]*|[^/]*_test\.py|README[^/]*|CHANGELOG[^/]*)$")


def settings(cfg: dict) -> dict:
    got = ((cfg or {}).get("coordinator") or {}).get("drift") or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        try:
            out[k] = float(got.get(k, DEFAULTS[k]) or 0)
        except (TypeError, ValueError, AttributeError):
            pass
    return out


def spec_paths(spec: str) -> list[str]:
    """The file and folder paths a task spec names (relative, `./` and a trailing `/` dropped)."""
    out = []
    for m in _PATH_RE.finditer(spec or ""):
        t = m.group(0).strip(".").lstrip("/")
        if t.startswith("./"):
            t = t[2:]
        t = t.rstrip("/")
        if t and t not in out and not t.startswith("http"):
            out.append(t)
    return out


def outside(files: list[str], named: list[str]) -> list[str]:
    """The changed files that are neither a named path, under one, in a named file's folder, nor a test,
    changeset or doc. A named path may be the tail of the repository path."""
    dirs = set()
    for n in named:
        leaf = posixpath.basename(n)
        dirs.add(posixpath.dirname(n) if "." in leaf else n)   # a file's folder, or the folder itself
    out = []
    for f in files:
        full = "/" + f
        if _FREE_RE.search(f) or any(full.endswith("/" + n) for n in named):
            continue
        if any(d and ("/" + d + "/") in full for d in dirs):
            continue
        out.append(f)
    return out


def detect(e: dict, s: dict, named: list[str], waiting: bool, now: float) -> dict[str, str]:
    """{flag: evidence} for runwatch entry `e` under settings `s`; `waiting`: a lock wait is open."""
    flags: dict[str, str] = {}
    if s["repeat_fail"] > 0 and e.get("fails"):
        cmd, n = max(e["fails"].items(), key=lambda kv: kv[1])
        if n >= s["repeat_fail"]:
            flags["repeat_fail"] = f"`{cmd[:100]}` failed {n} times"
    recent = (e.get("recent") or [])[-LOOP_WINDOW:]
    if s["loop"] > 0:
        top = Counter(c.get("h") for c in recent if c.get("h")).most_common(1)
        c = next((c for c in recent if top and c.get("h") == top[0][0]), {})
        failing = s["repeat_fail"] > 0 and (e.get("fails") or {}).get(c.get("cmd") or "", 0) >= s["repeat_fail"]
        if top and top[0][1] >= s["loop"] and not failing:   # a failing repeat is repeat_fail's, told once
            flags["loop"] = f"the same {c.get('tool')} call {top[0][1]} times in the last {len(recent)}" + (
                f": `{c['cmd'][:80]}`" if c.get("cmd") else "")
    if s["outside_spec"] > 0 and named:
        out = outside(e.get("files") or [], named)
        e["outside_n"] = len(out)
        if len(out) >= s["outside_spec"]:
            flags["outside_spec"] = f"{len(out)} changed files outside the spec's paths: {', '.join(out[:5])}"
    last = e.get("last_tool") or {}
    pending_wait = bool(last and not last.get("done") and WAIT_RE.search(last.get("input_head") or ""))
    if s["quiet_s"] > 0 and not waiting and not pending_wait:
        idle = now - max(float(e.get("active_at") or 0), float((e.get("drift") or {}).get("head_at") or 0),
                         float(e.get("started") or now))
        if idle >= s["quiet_s"]:
            flags["quiet"] = f"no tool call, note or commit for {idle / 60:.0f} min"
    sp, wall = e.get("spend") or {}, e.get("wall") or {}
    budget = sp.get("budget_usd")
    if s["pace"] > 0 and budget:
        run_share, task_share = float(sp.get("run_usd") or 0) / budget, float(sp.get("share") or 0)
        wall_share = wall.get("share")
        committed = (e.get("git") or {}).get("committed") if e.get("git") else None
        if wall_share is not None and run_share >= PACE_FLOOR and run_share > s["pace"] * wall_share:
            flags["pace"] = f"{run_share:.0%} of the budget spent in {wall_share:.0%} of the wall limit"
        elif task_share > PACE_NO_COMMIT and committed == []:
            flags["pace"] = f"{task_share:.0%} of the budget spent and nothing committed"
    return flags


def _wakes(flag: str, e: dict) -> bool:
    if flag == "pace":
        return float((e.get("spend") or {}).get("share") or 0) > WAKE_PACE
    return flag == "repeat_fail" and max((e.get("fails") or {}).values(), default=0) >= WAKE_REPEAT_FAIL


def _event(db, e: dict, flag: str, evidence: str, step: int, now: float) -> int | None:
    fp = f"{KIND}:{e['run']}:{flag}:{step}"
    if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        return None
    msg = (f"#{e.get('task')} {(e.get('title') or '')[:120]}: its running {e.get('role') or 'worker'} (run "
           f"{e['run']}) drifts ({flag}): {evidence}. It was told so in its steer.md"
           + (" and is still flagged." if step == 2 else ".")
           + " Decide: correct it with a task update, or cancel and requeue the task with a sharper spec.")
    data = {"run": e["run"], "task": e.get("task"), "flag": flag, "evidence": evidence, "step": step}
    if step == 2:
        data[pagesev.RIDE_ALONG] = True
    return db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (now, "daemon", KIND, fp, "normal", msg, json.dumps(data), "queued", e.get("task")))


def check(p, cfg: dict, data: dict, now: float) -> list[int]:
    """Flag each run in runwatch `data` (in place) and take each flag one ladder step; the new event ids."""
    from .coordinator import _append_update
    from .hook import STEER_FILE
    from .locks import wait_spans
    s = settings(cfg)
    runs = [e for e in (data.get("runs") or {}).values() if not e.get("gone") and not e.get("error")]
    specs = {}
    tasks = sorted({e["task"] for e in runs if e.get("task")})
    if tasks and s["outside_spec"] > 0:
        rows = p.db.q(f"SELECT id, spec FROM tasks WHERE id IN ({','.join('?' * len(tasks))})", tasks)
        specs = {r["id"]: spec_paths(r["spec"] or "") for r in rows}
    ids = [e["run"] for e in runs]
    dirs = {r["id"]: r["dir"] for r in p.db.q(f"SELECT id, dir FROM runs WHERE id IN ({','.join('?' * len(ids))})",
                                               ids)} if ids else {}
    raised = []
    for e in runs:
        st = e.setdefault("drift", {})
        head = (e.get("git") or {}).get("head")
        if head and head != st.get("head"):
            st["head_at"] = now if st.get("head") else float(e.get("started") or now)
            st["head"] = head
        run_dir = Path(dirs.get(e["run"]) or Path(p.runs) / str(e["run"]))
        waiting = any(end is None for _, end in wait_spans(run_dir))
        flags = detect(e, s, specs.get(e.get("task")) or [], waiting, now)
        e["flags"] = flags
        fired = st.setdefault("fired", {})
        for flag in FLAGS:
            rec = fired.get(flag)
            if flag not in flags:
                if rec:
                    rec["since"] = None
                continue
            rec = fired.setdefault(flag, {"steered": None, "raised": None, "woke": None})
            if not rec.get("since"):
                rec["since"] = now
            if not rec["steered"]:
                _append_update(run_dir / STEER_FILE,
                               f"Drift check (harness, model-free), {flag}: {flags[flag]}. Fix the cause, or "
                               f"say with `ttp note` why this is right.", key=f"drift-{e['run']}-{flag}")
                rec["steered"] = now
            if _wakes(flag, e) and not rec["woke"]:
                rec["woke"] = now
                raised.append(_event(p.db, e, flag, flags[flag], 3, now))
            elif not rec["raised"] and not rec["woke"] and s["raise_s"] > 0 and now - rec["since"] >= s["raise_s"]:
                rec["raised"] = now
                raised.append(_event(p.db, e, flag, flags[flag], 2, now))
    return [i for i in raised if i]
