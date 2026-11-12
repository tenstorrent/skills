# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""After each landing, the project's slow tests (delivery.post_land_checks), model-free.

The push checks are the smoke suite that gates a landing; the slower tests run here, after it, never
holding the queue. Each daemon pass (tend()):

- With no run in flight and a landing since the last one checked (a push queue batch, an express landing
  or a task's own `ttp push` to the push branch, task_landings), it starts one detached driver
  (`python -m ttp.postland <job>`, niced like the workers) on the newest landed sha. Every landing since
  the last check is in its range, by position on the branch: a batch's entries' landed shas after the tip
  before them, and each own push as one point from the landing before it (its base..sha is what a revert
  takes out).
- The driver (drive()) runs each check in a clean checkout of that sha, by push._run_one (its check host
  and lock rules); its first full run goes to a spill host the project's settings allow (checkspill.spill_run,
  as the push batch's first runs do), else here. Failing pytest ids rerun once there, always here: those that
  pass are flaky (quarantined, never reverted). Those failing again rerun at the range's start: failing there too, they were not this
  landing's. The rest bisect over the entries' landed shas to the first that fails them: the guilty change.
- Guilty: a revert of that entry's commits on the current tip (never a force push), checked to pass the
  failing ids, goes in as ttp/postland-revert-<sha>. The daemon approves it into the push queue (it lands
  with the next batch, after the smoke checks) and queues one code task to fix the change on its failing
  tests alone. Anything it cannot tell or attribute goes to the coordinator as one observation.
- An express landing (express.py, batch id express-*) checked only its smoke and touched tests: its job
  runs the full push checks and the post-landing checks, also where post_land_checks is unset.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import gitmemo
from .project import Project, durable_write

KEY = "post_land"
FOLDER = "post-land"
EVENT = "post_land"
BUSY_PASSES = 3   # passes a landing waits on a busy test lock (each after gatejudge's backoff) before it is unattributed


def driver_argv(job: Path) -> list[str]:
    """The driver process; tests put a stub here."""
    return [sys.executable, "-m", "ttp.postland", str(job)]


def _git(repo, *args) -> subprocess.CompletedProcess:
    """git in `repo` through gitmemo.run (its cap per verb); one past the cap reads as failed (rc 124)."""
    argv = ["git", "-C", str(repo), *args]
    try:
        return gitmemo.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "git timed out")


def _event(db, text: str, kind: str = "observation", queued: bool = True) -> None:
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
         (time.time(), "daemon", kind, None, "normal", text[:6000], "queued" if queued else "handled"))


def tend(p: Project, cfg: dict, now: float | None = None) -> str:
    """One daemon pass: settle a finished run, else start one for what landed since the last. Returns
    what it did ('' when nothing)."""
    from . import express, push
    now = time.time() if now is None else now
    d = cfg.get("delivery") or {}
    plc = push.check_list(d.get("post_land_checks"))
    db = p.db
    st = db.kv(KEY, {}) or {}
    job = st.get("job")
    # An express landing ran only its smoke and touched tests: its full push checks run here even in a
    # project with no post-landing checks of its own.
    only = "" if plc else f" AND id LIKE '{express.PREFIX}%'"
    if not plc and not job and not db.one(f"SELECT 1 FROM push_batches WHERE outcome='pushed' AND finalized IS NOT "
                                          f"NULL AND ended>?{only}", (st.get("since") or 0,)):
        return ""
    if job:
        res = Path(job["path"]).with_suffix(".result.json")
        if res.exists():
            try:
                got = json.loads(res.read_text())
            except ValueError:
                got = {"outcome": "error", "why": f"{res} is not JSON"}
        elif _alive(job.get("pid")):
            return ""
        else:
            got = {"outcome": "error", "why": "the driver ended without a result (a reboot, a kill or a crash)"}
        busy = int(st.get("busy") or 0) + 1 if got.get("outcome") == "busy" else 0
        if busy and busy < BUSY_PASSES:   # the test lock admitted nothing: rerun this very job (same range)
            again = retry(p, job, busy)
            db.set_kv(KEY, {**st, "job": again, "busy": busy})
            return f"post-landing checks on {job['sha'][:10]}: busy, retried"
        if busy:
            got = {"outcome": "unattributed", "why": f"the test lock stayed busy for {busy} passes"}
        db.set_kv(KEY, {**st, "job": None, "busy": 0, "since": job["ended"], "last": {**got, "sha": job["sha"]}})
        return apply(p, job, got)
    batches = db.q(f"SELECT * FROM push_batches WHERE outcome='pushed' AND finalized IS NOT NULL AND ended>?{only} "
                   "ORDER BY ended, started", (st.get("since") or 0,))
    own = own_landings(p, st.get("since") or 0) if plc else []
    if not batches and not own:
        return ""
    if not plc:
        batches = batches[:1]    # express landings only: one at a time, its own range
    units = [("batch", b["pushed_sha"], b["ended"], b) for b in batches] + \
        [("own", r["sha"], r["ts"] or 0, r) for r in own]
    ended = max(u[2] for u in units)
    line = _line(p.root, units) if own else []
    if own:   # by position on the branch: a batch's ended is when it finished, not where it landed
        at = {c: i for i, c in enumerate(reversed(line))}
        units = sorted((u for u in units if u[1] in at), key=lambda u: at[u[1]])
        if not units:
            db.set_kv(KEY, {**st, "since": ended})   # none of them is on the branch any longer
            return ""
    if plc and "since" not in st:
        units = units[-1:]   # first run: this landing only, not the project's history
    points, prev = [], ""
    for kind, sha, _, b in units:
        if kind == "own":   # a task's own `ttp push`: one point, from the landing before it
            t = db.task(int(b["task"])) or {}
            points.append({"sha": sha, "base": prev or _before(p, line, sha), "row": None, "task": b["task"],
                           "branch": t.get("branch") or ""})
            prev = sha
            continue
        line_b = _git(p.root, "rev-list", "--reverse", "--first-parent",
                      f"{b['tip']}..{b['pushed_sha']}" if b["tip"] else b["pushed_sha"]).stdout.split()
        at_b = {c: i for i, c in enumerate(line_b)}
        rows = [r for r in db.q("SELECT id, task, branch, landed_sha FROM push_queue WHERE batch=? AND landed_sha "
                                "IS NOT NULL AND status IN ('pushed','landed')", (b["id"],)) if r["landed_sha"] in at_b]
        base = b["tip"] or ""
        for r in sorted(rows, key=lambda r: at_b[r["landed_sha"]]):
            points.append({"sha": r["landed_sha"], "base": base, "row": r["id"], "task": r["task"],
                           "branch": r["branch"]})
            base = r["landed_sha"]
        if b["pushed_sha"] != base:
            points.append({"sha": b["pushed_sha"], "base": base, "row": None})
        prev = b["pushed_sha"]
    kind, sha, _, last = units[-1]
    name = Path(last["marker"]).stem if kind == "own" else last["id"]
    folder = p.state / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    good = (units[0][3]["tip"] or "") if units[0][0] == "batch" else points[0]["base"]
    job = {"path": str(path), "batch": name, "sha": sha, "good": good,
           "points": points, "target": last["target"], "ended": ended, "repo": str(p.root)}
    batches = [u[3] for u in units if u[0] == "batch"]
    if any(express.is_express(b["id"]) for b in batches):
        full = push.check_list(push.repo_checks(d, p.root, p)[0])
        job["checks"] = [c.config() for c in full + plc]
    durable_write(path, json.dumps(job))
    job["pid"] = spawn(p, path)
    db.set_kv(KEY, {**st, "job": job, "since": st.get("since", 0)})
    return f"post-landing checks started on {job['sha'][:10]}"


LINE = 2000   # commits of the push branch's first-parent line read back from its newest landing


def own_landings(p: Project, since: float) -> list[dict]:
    """Landings tasks made themselves on the push branch with `ttp push` (task_landings, recorded from their
    detached push markers) since `since`, oldest first; [] when no push branch is set."""
    from . import push
    try:
        name = "/".join(push.target(p, p.root))
    except ValueError:
        return []
    return p.db.q("SELECT marker, task, sha, target, ts FROM task_landings WHERE ts>? AND target=? AND sha NOT IN "
                  "(SELECT pushed_sha FROM push_batches WHERE pushed_sha IS NOT NULL) AND sha NOT IN "
                  "(SELECT landed_sha FROM push_queue WHERE landed_sha IS NOT NULL) ORDER BY ts", (since, name))


def _line(repo, units: list) -> list[str]:
    """The push branch's first-parent line, newest first, from the newest of `units`' landed shas."""
    head = units[0][1]
    for u in units[1:]:
        if u[1] != head and _git(repo, "merge-base", "--is-ancestor", head, u[1]).returncode == 0:
            head = u[1]
    return _git(repo, "rev-list", "--first-parent", f"--max-count={LINE}", head).stdout.split()


def _before(p: Project, line: list[str], sha: str) -> str:
    """The landing before `sha` on the branch (`line`, newest first): the nearest older commit that a batch, a
    queued entry, a task's own push or the last post-land run landed or checked, else its parent."""
    db = p.db
    known = {r["s"] for r in db.q("SELECT sha s FROM task_landings UNION SELECT pushed_sha FROM push_batches "
                                  "UNION SELECT tip FROM push_batches UNION SELECT landed_sha FROM push_queue")}
    known.add(((db.kv(KEY, {}) or {}).get("last") or {}).get("sha"))
    older = line[line.index(sha) + 1:] if sha in line else []
    return next((c for c in older if c in known), older[0] if older else "")


def retry(p: Project, job: dict, attempt: int) -> dict:
    """The same job (its sha, range and points) again under its own attempt's file, so its result, log and
    driver never meet an earlier attempt's: the result the next pass reads is this attempt's own."""
    base = Path(job["path"])
    path = base.parent / f"{job.get('batch') or base.stem.split('.')[0]}.{attempt}.json"
    again = {k: v for k, v in job.items() if k != "pid"} | {"path": str(path)}
    durable_write(path, json.dumps(again))
    again["pid"] = spawn(p, path)
    return again


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def spawn(p: Project, job: Path) -> int:
    from .project import nice_level, renice
    from .pushq import _child_env
    with open(job.with_suffix(".log"), "ab") as out:
        # own session: the post-land driver outlives this tick; tend() follows it by its pid and job file
        child = subprocess.Popen(driver_argv(job), cwd=str(p.root), env=_child_env(p, str(job.parent)),
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    renice(child.pid, nice_level(p.config().get("runner"))[0])
    return child.pid


def apply(p: Project, job: dict, got: dict) -> str:
    """Act on the driver's result: a guilty change's revert approved into the push queue and one fix task."""
    from . import pushq
    db, sha, out = p.db, job["sha"][:10], got.get("outcome")
    if out == "pass":
        return f"post-landing checks passed on {sha}"
    ids = " ".join(got.get("ids") or [])
    if out == "flaky":
        _event(db, f"Post-landing checks on {sha}: failed, then passed on a rerun (flaky, quarantined, not "
                   f"reverted): {ids}", kind=EVENT, queued=False)
        return "flaky"
    if out != "guilty":   # not this landing's (pre_existing) is only recorded; the rest is the coordinator's
        _event(db, f"Post-landing checks on {sha}: {out}: {got.get('why') or ''} {ids}".strip(),
               kind=EVENT if out == "pre_existing" else "observation", queued=out != "pre_existing")
        return str(out)
    e = got["entry"]
    who = f"#{e['task']} {e.get('branch') or '?'}" if e.get("task") else f"commit {e['sha'][:10]}"
    span = f"{str(e.get('base') or '')[:10]}..{e['sha'][:10]}"
    tail = (f"Its failing tests, the only ones to rerun: {ids}\nCommand: {got.get('cmd')}\n"
            f"Output tail:\n{str(got.get('tail') or '')[-2000:]}")
    if got.get("revert_head"):   # the revert first; the fix starts once it has landed, on top of it
        with db.tx():
            tid = db.add_task(f"Revert {who}: post-landing tests fail", kind="review", status="pushing",
                              spec=f"Forward revert of {span} by the post-landing checks.")
            pushq.approve(p, tid, None, {"entries": [{"branch": got["revert_branch"], "head": got["revert_head"]}],
                                         "target": job["target"]})
        fix = db.add_task(f"Fix {who}: post-landing tests fail", kind="code", labels=[
            f"start_when:landed:#{tid}", f"start_why:revert #{tid} lands", f"deferred_since:{time.time():.0f}"],
            spec=(f"Post-landing checks (delivery.post_land_checks) failed after {who} landed as {e['sha'][:10]}. "
                  f"Revert #{tid} (commit {got['revert_head'][:10]}, branch {got['revert_branch']}) takes {span} "
                  f"off {job['target']}; this starts once it has landed. Re-apply {span} on top of the revert "
                  f"with its fix. {tail}"))
        text = (f"Post-landing checks on {sha}: {who} fails {ids}; revert #{tid} ({got['revert_head'][:10]}) "
                f"approved for {job['target']}; fix task #{fix} starts once it lands")
    else:
        fix = db.add_task(f"Fix {who}: post-landing tests fail", kind="code", spec=(
            f"Post-landing checks (delivery.post_land_checks) failed after {who} landed as {e['sha'][:10]} "
            f"({span}). It is not reverted ({got.get('why')}): the change is still on {job['target']}; fix it "
            f"forward. {tail}"))
        text = f"Post-landing checks on {sha}: {who} fails {ids}; fix task #{fix}; not reverted: {got.get('why')}"
    _event(db, text)
    return "guilty"


def drive(p: Project, job: dict, say=print) -> dict:
    """Run the checks on job['sha'] and, on a new failure, find and revert its change (see the module)."""
    from . import checkcap, cleantree, gatejudge, push, sharedchecks, worktree
    repo = Path(job["repo"])
    base = str(Path(job["path"]).parent)
    checks = push.check_list(job["checks"] if "checks" in job else
                             (p.config().get("delivery") or {}).get("post_land_checks"))
    try:
        env = push.check_env("target", job.get("good") or "", base, cfg=p.config())
    except Exception:
        env = dict(os.environ)

    busy = gatejudge.settings(p.config())

    def run(at: str, cmd, sel: list[str] | None = None) -> tuple[int | None, str]:
        """One run, again after gatejudge's bounded backoff while the test lock is busy (it ran nothing)."""
        got = gatejudge.rerun(lambda a, s: run_once(a, cmd, s), at, sel, busy["busy_tries"], busy["busy_wait_s"])
        if got[0] == gatejudge.BUSY:
            raise Busy(f"`{cmd}` at {at[:10]}")
        return got

    def run_once(at: str, cmd, sel: list[str] | None = None) -> tuple[int | None, str]:
        tree = cleantree.Checkout(repo, at, base)
        try:
            worktree.prepare(p, tree.path)
            got: dict = {}
            sel_env = gatejudge.select_env(sel, env) if sel else {}
            claim, sent = (None, cmd) if sel else _spill(p, cmd, at, say)
            try:
                _, rc = push._run_or_here(sent, tree.path, {**env, "PWD": str(tree.path), **sel_env}, say, got,
                                          checkcap.Slot)
            finally:
                if claim is not None:
                    claim.close()
            return (gatejudge.BUSY if rc == gatejudge.BUSY else got.get("rc")), got.get("text") or ""
        finally:
            tree.close()

    try:
        return _drive(p, job, checks, run)
    except Busy as ex:
        return {"outcome": "busy", "why": f"the test lock stayed busy: {ex}"}


def _spill(p: Project, cmd, sha: str, say) -> tuple:
    """(claim, check) for a check's first full run on the landed sha: sent to a spill host checkspill.spill_run
    picks (the project's own spill hosts, as the push batch's first full runs go), else (None, `cmd`) here. Every
    rerun of its failing tests runs here on `cmd` itself, never spilled (as the push batch's reruns). The spill
    hosts are a speed-up: an error trying them, or a host that cannot run it (push._run_or_here), keeps it here."""
    from . import checkspill
    try:
        claim, out = checkspill.spill_run(p, p.config(), [cmd], say, f"post-landing checks on {sha[:10]}")
    except Exception as e:   # noqa: BLE001
        say(f"spill hosts not tried ({e}): the checks run here")
        return None, cmd
    return claim, (out[0] if claim is not None and out else cmd)


class Busy(Exception):
    """A run the test lock admitted nowhere after its backoff: nothing ran, nothing is known."""


def _drive(p: Project, job: dict, checks: list, run) -> dict:
    from . import gatejudge, sharedchecks
    sha, points = job["sha"], job["points"]
    flaky: list[str] = []
    for cmd in checks:
        rc, text = run(sha, cmd)
        if rc == 0:
            continue
        ids, complete = sharedchecks.failing_ids(text)
        if not ids or not complete:
            return {"outcome": "unattributed", "why": f"`{cmd}` exited {rc} without a complete list of failing tests",
                    "cmd": str(cmd), "tail": text[-3000:]}
        again = gatejudge.failed_of(*run(sha, cmd, ids), ids, at="head")
        if again is None:
            return {"outcome": "undecided", "why": "the rerun could not tell", "ids": ids, "cmd": str(cmd)}
        flaky += [i for i in ids if i not in again]
        if not again:
            continue
        ids = sorted(again)
        pre = gatejudge.failed_of(*run(job["good"], cmd, ids), ids) if job.get("good") else None
        if pre is None or pre == set(ids):
            return {"outcome": "pre_existing" if pre else "undecided", "ids": ids, "cmd": str(cmd),
                    "why": "they fail before this landing too" if pre else
                    f"the rerun before this landing ({str(job.get('good'))[:10] or 'none'}) could not tell"}
        ids = sorted(set(ids) - pre)
        lo, hi = -1, len(points) - 1   # lo passes (the range's start), hi fails
        while hi - lo > 1:
            mid = (lo + hi) // 2
            f = gatejudge.failed_of(*run(points[mid]["sha"], cmd, ids), ids)
            if f is None:
                return {"outcome": "undecided", "why": f"the rerun at {points[mid]['sha'][:10]} could not tell",
                        "ids": ids, "cmd": str(cmd)}
            lo, hi = (lo, mid) if f else (mid, hi)
        e = points[hi]
        out = {"outcome": "guilty", "ids": ids, "cmd": str(cmd), "entry": e, "tail": text[-3000:]}
        out.update(revert(p, Path(job["repo"]), job, e, cmd, ids, run))
        return out
    if flaky:
        s = gatejudge.settings(p.config())
        gatejudge.quarantine(p.state, flaky, {"head": sha, "check": "post_land_checks", "host": gatejudge.host()},
                             time.time(), s["days"])
        return {"outcome": "flaky", "ids": flaky}
    return {"outcome": "pass"}


def revert(p: Project, repo: Path, job: dict, e: dict, cmd, ids: list[str], run) -> dict:
    """A commit reverting the entry's own commits (e['base']..e['sha']) on the target's current tip, as a local branch, once it passes `ids`."""
    from . import cleantree, push
    remote, _, branch = str(job["target"]).partition("/")
    try:
        tip = push._fetch(repo, remote, branch)
    except Exception as ex:
        return {"why": f"could not read {job['target']}: {ex}"}
    if not e.get("row") and not e.get("task"):   # a queue entry or a task's own push, never a batch's bump
        return {"why": "the first failing sha is the version bump, not an entry"}
    start = e.get("base") or ""
    if not start or start == e["sha"]:
        return {"why": f"no commits of its own to revert ({start[:10] or 'no base'}..{e['sha'][:10]})"}
    tree = cleantree.Checkout(repo, tip, str(Path(job["path"]).parent))
    try:
        g = lambda *a: _git(tree.path, "-c", "user.name=ttp", "-c", "user.email=ttp@localhost", *a)
        r = g("revert", "--no-edit", "--no-commit", f"{start}..{e['sha']}")
        if r.returncode != 0:
            return {"why": f"the revert does not apply on {tip[:10]}: {(r.stderr or r.stdout).strip()[:300]}"}
        r = g("commit", "-q", "-m", f"Revert {e.get('branch') or e['sha'][:10]}: post-landing tests fail\n\n"
                                    f"Reverts {start[:10]}..{e['sha'][:10]}; failing: {' '.join(ids)}")
        head = _git(tree.path, "rev-parse", "HEAD").stdout.strip()
        name = f"ttp/postland-revert-{e['sha'][:10]}"
        if r.returncode or _git(repo, "fetch", "-q", str(tree.path), f"+HEAD:refs/heads/{name}").returncode:
            return {"why": "the revert commit could not be made"}
    finally:
        tree.close()
    from . import gatejudge
    still = gatejudge.failed_of(*run(head, cmd, ids), ids)
    if still is None or still:
        return {"why": f"the revert {head[:10]} does not pass {' '.join(sorted(still or ids))}"}
    return {"revert_branch": name, "revert_head": head}


def main(argv: list[str]) -> int:
    job_path = Path(argv[0])
    job = json.loads(job_path.read_text())
    p = Project(Path(os.environ["TTP_PROJECT"]))
    try:
        got = drive(p, job)
    except Exception as ex:
        got = {"outcome": "error", "why": f"{type(ex).__name__}: {ex}"[:1000]}
    durable_write(job_path.with_suffix(".result.json"), json.dumps(got))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
