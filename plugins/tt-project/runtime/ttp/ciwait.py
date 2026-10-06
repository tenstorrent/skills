# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A waiting task's CI probe: `ttp ci --branch <b> [--repo <o/r>] [--sha <s>] [--workflow <w>] [--run <id>]`.

It exits 0 once the GitHub Actions runs of the commit completed, whatever their result, or once one
of their jobs has been in progress more than `factor` times the median duration of that job in the
branch's recent successful runs (never under `floor_min`; `default_min` when neither the branch nor
the repo has a finished run of it). Without that, a hung job keeps the task asleep until GitHub's
6 h job limit. 1 while the runs go on normally or none exists yet; 75 when gh cannot answer (a
network blip: "not yet"). One line per run says why, so the woken task runs it again and reports a
`hung` line. `--no-hang` waits for completion only (for a task that saw the hang and still waits).
"""
from __future__ import annotations

import json
import statistics
import subprocess
import time
from datetime import datetime

FACTOR = 3.0
FLOOR_MIN = 10.0     # a job is never called hung before this, however short it usually is
DEFAULT_MIN = 90.0   # the limit when no finished run of the workflow is known
HISTORY_RUNS = 3     # successful runs per workflow whose jobs make the median
RUN_FIELDS = "databaseId,status,conclusion,headSha,workflowName,url,createdAt"


def _gh(args: list[str]) -> list | dict | None:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def _ts(s) -> float | None:
    if not s or str(s).startswith("0001-"):
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _mins(s: float) -> str:
    return f"{s / 60:.0f} min"


def medians(history: list[dict]) -> tuple[dict, dict]:
    """Median seconds per (workflow, job name) and per workflow, from finished runs' jobs."""
    per_job: dict = {}
    per_wf: dict = {}
    for run in history:
        wf = run.get("workflowName") or ""
        for j in run.get("jobs") or []:
            start, end = _ts(j.get("startedAt")), _ts(j.get("completedAt"))
            if start is None or end is None or end < start or (j.get("conclusion") or "") != "success":
                continue
            per_job.setdefault((wf, j.get("name") or ""), []).append(end - start)
            per_wf.setdefault(wf, []).append(end - start)
    return ({k: statistics.median(v) for k, v in per_job.items()},
            {k: statistics.median(v) for k, v in per_wf.items()})


def limit_s(wf: str, job: str, meds: tuple[dict, dict], factor: float = FACTOR, floor_min: float = FLOOR_MIN,
            default_min: float = DEFAULT_MIN) -> tuple[float, str]:
    """How long a job may stay in progress before it counts as hung, and what that limit rests on."""
    per_job, per_wf = meds
    med = per_job.get((wf, job))
    basis = "this job's" if med is not None else "the workflow's jobs'"
    if med is None:
        med = per_wf.get(wf)
    if med is None:
        return default_min * 60, "no finished run known"
    lim = max(factor * med, floor_min * 60)
    return lim, f"{factor:g}x {basis} median {_mins(med)}" + (f", floor {_mins(floor_min * 60)}"
                                                                if lim > factor * med else "")


def verdict(runs: list[dict], meds: tuple[dict, dict], now: float, hang: bool = True, **kw) -> tuple[int, list[str]]:
    """(exit code, lines) for the target runs (each with `jobs` when not completed)."""
    if not runs:
        return 1, ["waiting: no run yet"]
    lines, done, hung = [], 0, False
    for r in runs:
        wf, url = r.get("workflowName") or "?", r.get("url") or ""
        if (r.get("status") or "") == "completed":
            done += 1
            lines.append(f"done: {wf} {r.get('conclusion') or '?'} {url}")
            continue
        worst = None
        for j in r.get("jobs") or []:
            start = _ts(j.get("startedAt"))
            if (j.get("status") or "") != "in_progress" or start is None:
                continue
            lim, why = limit_s(wf, j.get("name") or "", meds, **kw)
            over = (now - start) - lim
            if worst is None or over > worst[0]:
                worst = (over, j, now - start, lim, why)
        if hang and worst and worst[0] > 0:
            hung = True
            _, j, ran, lim, why = worst
            lines.append(f"hung: {wf} job '{j.get('name')}' in progress {_mins(ran)}, limit {_mins(lim)} "
                         f"({why}) {j.get('url') or url}")
        elif worst:
            _, j, ran, lim, why = worst
            lines.append(f"running: {wf} job '{j.get('name')}' {_mins(ran)} of limit {_mins(lim)} ({why}) {url}")
        else:
            lines.append(f"running: {wf} {r.get('status') or '?'} {url}")
    return (0 if hung or done == len(runs) else 1), lines


def _repo(repo: str | None) -> list[str]:
    return ["-R", repo] if repo else []


def targets(runs: list[dict], sha: str | None) -> list[dict]:
    """The newest run per workflow of `sha` (else of the newest run's commit)."""
    runs = sorted(runs, key=lambda r: _ts(r.get("createdAt")) or 0, reverse=True)
    if not runs:
        return []
    sha = sha or runs[0].get("headSha") or ""
    out: dict = {}
    for r in runs:
        if str(r.get("headSha") or "").startswith(sha) and r.get("workflowName") not in out:
            out[r.get("workflowName")] = r
    return list(out.values())


def history(repo: str | None, branch: str | None, workflows: set, skip: set) -> list[dict] | None:
    """Recent successful runs (with jobs) of these workflows: the branch's, else the repo's."""
    for scope in ([["--branch", branch]] if branch else []) + [[]]:
        listed = _gh(["run", "list", *_repo(repo), *scope, "--status", "success", "--limit", "20",
                      "--json", RUN_FIELDS])
        if listed is None:
            return None
        picked, count = [], {}
        for r in listed:
            wf = r.get("workflowName")
            if wf in workflows and r.get("databaseId") not in skip and count.get(wf, 0) < HISTORY_RUNS:
                count[wf] = count.get(wf, 0) + 1
                picked.append(r)
        if picked:
            for r in picked:
                view = _gh(["run", "view", str(r["databaseId"]), *_repo(repo), "--json", "jobs"])
                r["jobs"] = (view or {}).get("jobs") or []
            return picked
    return []


def probe(repo: str | None = None, branch: str | None = None, sha: str | None = None, workflow: str | None = None,
          run: str | None = None, hang: bool = True, **kw) -> tuple[int, list[str]]:
    if run:
        one = _gh(["run", "view", str(run), *_repo(repo), "--json", RUN_FIELDS + ",headBranch"])
        if one is None:
            return 75, [f"not yet: gh could not read run {run}"]
        runs, branch = [one], branch or one.get("headBranch")
    else:
        args = ["run", "list", *_repo(repo), "--limit", "30", "--json", RUN_FIELDS]
        args += ["--branch", branch] if branch else []
        args += ["--workflow", workflow] if workflow else []
        listed = _gh(args)
        if listed is None:
            return 75, ["not yet: gh could not list runs"]
        runs = targets(listed, sha)
    meds: tuple[dict, dict] = ({}, {})
    live = [r for r in runs if (r.get("status") or "") != "completed"]
    if live and hang:
        for r in live:
            view = _gh(["run", "view", str(r["databaseId"]), *_repo(repo), "--json", "jobs"])
            if view is None:
                return 75, [f"not yet: gh could not read the jobs of run {r['databaseId']}"]
            r["jobs"] = view.get("jobs") or []
        past = history(repo, branch, {r.get("workflowName") for r in live}, {r.get("databaseId") for r in runs})
        if past is None:
            return 75, ["not yet: gh could not list past runs"]
        meds = medians(past)
    return verdict(runs, meds, time.time(), hang=hang, **kw)
