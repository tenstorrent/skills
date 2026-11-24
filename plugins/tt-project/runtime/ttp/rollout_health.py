# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Health of a release-ring flag in one project, model-free (the daemon's rollout watch, every 15 min).

A project is compared with itself: its window starts when the flag reached its ring, its baseline is the 7
days before that. `check` returns the failing signals (rollback), `verdict` also says whether the window has
enough samples to vote "clean". Promotion needs a clean vote from every project listed in the flag's current
ring, after that ring's soak; silence never promotes (too few samples stretch the window to
MAX_WINDOW_H, then a low note says so). Thresholds are settings: rollout.thresholds overrides DEFAULTS.

Rates and costs are compared like for like: work is grouped by task kind and tier, each window sample is set
against the baseline of its own group (groups the baseline lacks are left out), and a check judges only once
its matched sample reaches its minimum. A shadow flag (rollout.FLAGS[flag]["shadow"]) changes no run, so no
signal judges it: the watch never rolls it back; `ttp rollout rollback` by hand still does.

A model-route flag (rollout.FLAGS[flag]["arm"]) is blamed only for work its route ran (_Scope): the window's
failed runs are its runs on the arm, against the baseline's runs off it; a landing or a stuck task counts only
when the task's latest run was on the arm. Work it never ran is left out, and a stuck task left out is listed
under "reported", never "failing". A flag whose arm runs are coordinator turns (FLAGS "runs": "coordinator")
is judged on those turns; no landing or stuck task is its. A flag with no arm changes how every task runs, so
its signals stay project-wide. A run a host reboot or crash cut counts toward no flag (_runs). A task waiting
in a live batch run (batchrun.held) is never stuck: its lead runs for it.
"""
from __future__ import annotations

import re
import statistics
import subprocess
import time
from pathlib import Path

from . import postland, pushq, rollout

DEFAULTS = {"failed_runs_pts": 5.0, "min_runs": 20, "land_time_x": 1.5, "cost_x": 1.3, "min_landings": 20,
            "stuck_h": 2.0, "baseline_days": 7.0, "soak_h_0": 1.0, "soak_h_1": 24.0, "max_window_h": 72.0}
# Alerts a flag is judged on: only those of its own area (rollout.FLAGS[flag]["area"]), each a key prefix, new
# since the baseline. Only a high alert counts: it is the one stored in the alerts table. An area with no alert of
# its own is an empty tuple and is judged on the generic signals alone (failed runs, time to land, cost per
# landing, stuck tasks, forward reverts), which cover the landing path. The prefixes are keys the runtime raises
# and stores there (a test checks each against the source and the real raise path).
ALERT_AREAS = {
    "push": (pushq.LANDING_ALERT, pushq.BAD_TIP_ALERT, "push_queue_dying", "after_push_failed"),
    "route": ("auth:",),
    "eta": (),
    "device": (),
    "review": (),
    "worker": (),
    "coordinator": (),
    "output": (),
}
H = 3600.0


def thresholds(cfg: dict | None = None) -> dict:
    out = dict(DEFAULTS)
    for k, v in ((cfg or {}).get("thresholds") or {}).items():
        if k in out and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[k] = float(v)
    return out


def entered(flag: str, ring: int, recs: list[dict]) -> float | None:
    """When `flag` first reached a level covering `ring` in its current stretch (after the last rollback or
    off), from the ledger; None when it does not cover it."""
    since = None
    for r in recs:
        if r.get("flag") != flag or r.get("kind") not in ("set", "promote", "rollback"):
            continue
        to = r.get("to")
        if r["kind"] == "rollback" or to == "off":
            since = None
        elif isinstance(to, int) and to >= ring and since is None:
            since = r.get("ts")
        elif isinstance(to, int) and to < ring:
            since = None
    return since


def _med(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _group(r) -> str:
    return f"{r['kind'] or '?'}/{r['tier'] or '?'}"


class _Scope:
    """Which work counts against a flag (module doc): `arm` None counts all of it; else `on` picks the arm's
    work (the window) or the rest (the baseline), and `coord` says the arm's runs are coordinator turns."""

    def __init__(self, flag: str | None, on: bool = True):
        meta = rollout.FLAGS.get(flag or "", {})
        self.arm, self.on, self.coord = meta.get("arm"), on, meta.get("runs") == "coordinator"

    def run(self, r) -> bool:
        if not self.arm:
            return True
        from . import glmshare
        return (self.arm == "glm" and glmshare.on_glm(r["provider"], r["model"], r["note"])) == self.on

    def task(self, db, tid: int) -> bool:
        """The task's latest worker run is in scope; a coordinator-turn arm runs no task."""
        if not self.arm:
            return True
        if self.coord:
            return not self.on
        last = db.one("SELECT provider, model, note FROM runs WHERE task=? AND role!='coordinator' "
                      "ORDER BY started DESC, id DESC LIMIT 1", (tid,))
        return self.run(last) if last else not self.on


def _runs(db, a: float, b: float, scope: _Scope | None = None) -> dict[str, list[int]]:
    """{kind/tier: [runs, failed]} of the worker runs that ended in [a, b) in `scope` (a coordinator-turn arm:
    its coordinator turns), login failures and runs a host reboot or crash cut (finish_run's lost_to_reboot,
    lost_to_crash) left out: the host failed them, not the route; their reruns count on their own outcome."""
    scope = scope or _Scope(None)
    role = "r.role='coordinator'" if scope.arm and scope.coord else "r.role!='coordinator'"
    out: dict[str, list[int]] = {}
    for r in db.q("SELECT r.status, r.provider, r.model, r.note, t.kind, COALESCE(t.assigned_tier, t.tier) tier "
                  f"FROM runs r LEFT JOIN tasks t ON t.id=r.task WHERE {role} AND r.ended>=? AND r.ended<? AND "
                  "r.status NOT IN ('running','auth') AND COALESCE(r.note,'') NOT LIKE '%\"lost_to_reboot\"%' "
                  "AND COALESCE(r.note,'') NOT LIKE '%\"lost_to_crash\"%'", (a, b)):
        if not scope.run(r):
            continue
        n = out.setdefault(_group(r), [0, 0])
        n[0] += 1
        n[1] += r["status"] in ("failed", "lost")
    return out


def _landings(db, a: float, b: float, scope: _Scope | None = None) -> list[tuple[float, float, str]]:
    """(seconds from task creation to its first landing, the task's spend, its kind/tier) per task in `scope`
    landed in [a, b)."""
    scope = scope or _Scope(None)
    rows = db.q("SELECT t.id, t.created c, t.spent_usd s, t.kind, COALESCE(t.assigned_tier, t.tier) tier, MIN(l.ts) l "
                "FROM task_landings l JOIN tasks t ON t.id=l.task WHERE t.kind='code' GROUP BY t.id "
                "HAVING MIN(l.ts)>=? AND MIN(l.ts)<?", (a, b))
    return [(r["l"] - (r["c"] or r["l"]), float(r["s"] or 0), _group(r)) for r in rows if scope.task(db, r["id"])]


def _per_kind(window: list[tuple[float, float, str]], base: list[tuple[float, float, str]]) -> dict[str, dict]:
    """{kind/tier: window count, window and baseline mean cost, baseline median time} for the groups both have."""
    out = {}
    for g in sorted({x[2] for x in window} & {x[2] for x in base}):
        w, b = [x for x in window if x[2] == g], [x for x in base if x[2] == g]
        out[g] = {"n": len(w), "base_n": len(b), "cost": sum(x[1] for x in w) / len(w),
                  "base_cost": sum(x[1] for x in b) / len(b), "base_time": _med([x[0] for x in b])}
    return out


def _kinds_text(per: dict[str, dict]) -> str:
    return ", ".join(f"{g} {v['n']} at ${v['cost']:.2f} against ${v['base_cost']:.2f}" for g, v in per.items())


_SPAN = re.compile(r"Forward revert of ([0-9a-f]*)\.\.([0-9a-f]+) by ")


def _reverted_files(repo: str | None, spec: str) -> set[str] | None:
    """The files the reverted span (a revert task's "Forward revert of <base>..<sha>") changed, read from git
    in `repo`; None when the span or the repo cannot be read."""
    m = _SPAN.search(spec or "")
    if not m or not repo:
        return None
    base, sha = m.groups()
    cmd = ["git", "-C", repo, "diff", "--name-only", base, sha] if base else \
        ["git", "-C", repo, "show", "--name-only", "--format=", sha]
    try:
        got = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return set(got.stdout.split()) if got.returncode == 0 else None


def _by_arm(db, title: str, arm: str) -> bool:
    """Whether the change a revert task's title names (`Revert #<review> ttp/t<task>-...`) ran on `arm`: a
    worker run of its review or of the branch's task on that model (only "glm" so far)."""
    from . import glmshare
    ids = {int(x) for x in re.findall(r"#(\d+)", title)} | {int(x) for x in re.findall(r"\bttp/t(\d+)-", title)}
    if arm != "glm" or not ids:
        return False
    rows = db.q(f"SELECT provider, model, note FROM runs WHERE task IN ({','.join('?' * len(ids))})", tuple(ids))
    return any(glmshare.on_glm(r["provider"], r["model"], r["note"]) for r in rows)


def _tests_hit(db, repo: str | None, title: str, meta: dict) -> bool:
    """Whether a test whose failure caused the revert exercises the flag's gated code: it lies in a gated
    module's case file (tests/tt_project_runtime/<module>_cases.py) or its body names the flag's key. The
    failing tests are read from the revert's fix task (postland.failing_tests)."""
    names = {i.split("::")[-1].split("[")[0] for i in postland.failing_tests(db, title)}
    if not names or not repo:
        return False
    cases = {f"{c.rsplit('/', 1)[-1][:-3]}_cases.py" for c in meta.get("code") or ()}
    for f in (Path(repo) / "tests").rglob("*.py"):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for n in names:
            at = text.find(f"def {n}(")
            if at < 0:
                continue
            end = text.find("\ndef ", at + 1)
            if f.name in cases or meta["key"] in text[at:end if end > 0 else None]:
                return True
    return False


def _reverts(db, since: float, now: float, flag: str | None, repo: str | None) -> tuple[list[str], list[str]]:
    """(the revert tasks that count against `flag`, those whose reverted commit cannot be resolved). A revert
    counts only when the commit it reverted changed a file of the flag's gated code (rollout.FLAGS[flag]["code"]);
    an unresolved one counts against no flag. A model-route flag (its `arm`) also needs the change to have run
    on that arm (_by_arm) or a failing test to exercise its gated code (_tests_hit): a change made on another
    arm that only touched its files never counts."""
    meta = rollout.FLAGS.get(flag or "", {})
    code = set(meta.get("code") or ())
    hit, unknown = [], []
    for r in db.q("SELECT id, title, spec FROM tasks WHERE title LIKE ? ESCAPE '\\' AND created>=? AND created<?",
                  ("Revert %" + postland.REVERT_TITLE_TAIL.replace("%", "\\%").replace("_", "\\_"), since, now)):
        files = _reverted_files(repo, r["spec"])
        if files is None:
            unknown.append(f"#{r['id']}")
        elif code and files & code and (not meta.get("arm") or _by_arm(db, r["title"], meta["arm"])
                                         or _tests_hit(db, repo, r["title"], meta)):
            hit.append(f"#{r['id']}")
    return hit, unknown


def signals(db, since: float, now: float, th: dict, area: str | None = None, flag: str | None = None,
            repo: str | None = None) -> dict:
    """The window [since, now) against the baseline before `since`: the measures and the failing signals.
    `area` is the flag's area: a new high alert counts only if its key starts with one of ALERT_AREAS[area].
    `flag` and `repo` (the project's git root) attribute forward reverts: see _reverts; "unattributed" lists
    the reverts whose commit could not be read, shown and never failing. `flag` also scopes the other signals to
    the work its route ran (module doc); "reported" lists the stuck tasks it never ran, shown and never failing.
    A shadow flag's would-be signals are listed under "exempt" and never under "failing"."""
    base_a = since - th["baseline_days"] * 86400
    mine, rest = _Scope(flag), _Scope(flag, on=False)
    rb, rw = _runs(db, base_a, since, rest), _runs(db, since, now, mine)
    nw, fw = sum(v[0] for v in rw.values()), sum(v[1] for v in rw.values())
    nb = sum(v[0] for v in rb.values())
    lb, lw = _landings(db, base_a, since, rest), _landings(db, since, now, mine)
    fail: list[str] = []
    # Failed runs: the window's rate against the baseline's rate re-weighted to the window's mix of kinds.
    both = [g for g in rw if rb.get(g, [0])[0]]
    nm, fm = sum(rw[g][0] for g in both), sum(rw[g][1] for g in both)
    expect = sum(rw[g][0] * rb[g][1] / rb[g][0] for g in both)
    if nm >= th["min_runs"] and 100.0 * (fm - expect) / nm > th["failed_runs_pts"]:
        fail.append(f"failed runs {100 * fm / nm:.0f}% against {100 * expect / nm:.0f}% for the same task kinds "
                    f"({nm} runs)")
    per = _per_kind(lw, lb)
    matched = [x for x in lw if x[2] in per]
    if len(matched) >= th["min_landings"]:
        mw = _med([x[0] for x in matched])
        mb = _med([per[x[2]]["base_time"] for x in matched])
        ratio = _med([x[0] / per[x[2]]["base_time"] for x in matched if per[x[2]]["base_time"]])
        if ratio and ratio > th["land_time_x"]:
            fail.append(f"median time to land {mw / H:.1f} h against {mb / H:.1f} h for the same task kinds "
                        f"({len(matched)} landings: {_kinds_text(per)})")
        cw = sum(x[1] for x in matched) / len(matched)
        cb = sum(per[x[2]]["base_cost"] for x in matched) / len(matched)
        if cb and cw > th["cost_x"] * cb:
            fail.append(f"cost per landing ${cw:.2f} against ${cb:.2f} for the same task kinds "
                        f"({len(matched)} landings: {_kinds_text(per)})")
    prefixes = ALERT_AREAS.get(area or "", ())

    def raised(a: float, b: float) -> set[str]:
        return {r["key"] for r in db.q("SELECT DISTINCT key, severity FROM alerts WHERE raised>=? AND raised<?", (a, b))
                if r["key"].startswith(prefixes) and r["severity"] == "high"}
    seen = raised(base_a, since)
    for key in sorted(raised(since, now) - seen):
        fail.append(f"new alert {key}")
    rev, unattributed = _reverts(db, since, now, flag, repo)
    if rev:
        fail.append(f"{len(rev)} forward revert(s) of a landed change touching this flag's code: {', '.join(rev[:5])}")
    from . import batchrun
    batched = batchrun.held(db)
    stuck, reported = [], []
    for r in db.q("SELECT id FROM tasks WHERE status='running' AND updated<? AND NOT EXISTS "
                  "(SELECT 1 FROM runs WHERE runs.task=tasks.id AND runs.status='running')", (now - th["stuck_h"] * H,)):
        if r["id"] not in batched:
            (stuck if mine.task(db, r["id"]) else reported).append(f"#{r['id']}")
    if stuck:
        fail.append("tasks with no run and no change for " + f"{th['stuck_h']:g} h: " + ", ".join(stuck[:5]))
    enough = nm >= th["min_runs"] or len(matched) >= th["min_landings"]
    out = {"runs": nw, "failed": fw, "landings": len(lw), "base_runs": nb, "base_landings": len(lb),
           "matched_runs": nm, "matched_landings": len(matched), "kinds": per,
           "failing": fail, "enough": enough, "unattributed": unattributed, "reported": reported}
    if rollout.FLAGS.get(flag or "", {}).get("shadow"):
        out.update(failing=[], exempt=fail, shadow=True)
    return out


def verdict(db, flag: str, ring: int, recs: list[dict], cfg: dict, now: float | None = None,
            repo: str | None = None, project: str = "") -> dict | None:
    """None when `flag` does not cover `ring`; else signals() plus 'since' and 'level' (the ring entered, or
    `project`'s last re-arm after a switch-off of its own, so the work that switched it off never counts again)."""
    now = now or time.time()
    since = entered(flag, ring, recs)
    if since is None:
        return None
    if project:
        since = max(since, max((float(r.get("ts") or 0) for r in recs if r.get("flag") == flag
                                and r.get("op") == "project_arm" and r.get("project") == project), default=0.0))
    v = signals(db, since, now, thresholds(cfg), rollout.FLAGS.get(flag, {}).get("area"), flag, repo)
    v["since"] = since
    return v


def ready(flag: str, level: int, project_names: list[str], recs: list[dict], cfg: dict, now: float | None = None) -> bool:
    """Whether `flag` may go from `level` to the next ring: its soak has passed and every project listed in that
    ring has a clean vote (a `clean` ledger record) since the flag reached the level."""
    now = now or time.time()
    th = thresholds(cfg)
    since = entered(flag, level, recs)
    if since is None or level not in (0, 1) or not project_names:
        return False
    if now - since < th[f"soak_h_{level}"] * H:
        return False
    voted = {r.get("reason", "").split(" ")[0] for r in recs
             if r.get("flag") == flag and r.get("kind") == "clean" and (r.get("ts") or 0) >= since}
    return all(n in voted for n in project_names)
