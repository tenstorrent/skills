# Rank inheritance for the dependencies of obligation owners and urgent tasks (rankinherit.py), and a run
# whose supervisor is gone being lost at once: tests of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there).
from __future__ import annotations

import math
import os
import subprocess
import sys
import time


def _rank_row(tid, deps=(), priority=3, spec=""):
    import json
    return {"id": tid, "depends_on": json.dumps(list(deps)), "priority": priority, "spec": spec}


def test_dependencies_of_an_owner_inherit_its_due_through_other_tasks():
    from ttp import rankinherit
    rows = [_rank_row(1, [2]), _rank_row(2, [3]), _rank_row(3), _rank_row(4), _rank_row(5, [3])]
    got = rankinherit.extend(rows, {1: 100.0})
    assert got == {1: 100.0, 2: 100.0, 3: 100.0}   # 4 and 5 are not needed by an owner
    # The earliest due of several owners wins, whichever order they are walked in.
    got = rankinherit.extend(rows, {1: 500.0, 5: 50.0})
    assert got[3] == 50.0 and got[2] == 500.0 and got[1] == 500.0 and got[5] == 50.0


def test_dependencies_of_an_urgent_task_rank_as_owners_with_no_due():
    from ttp import rankinherit
    rows = [_rank_row(1, [2], priority=1), _rank_row(2, [3]), _rank_row(3), _rank_row(4, [3]), _rank_row(5, [], spec="urgent: demo")]
    got = rankinherit.extend(rows, {})
    assert got == {2: math.inf, 3: math.inf}   # the urgent tasks keep their own lane (urgent.first)
    # An owner's own due is kept for what it needs, not widened by an urgent dependent later in the chain.
    got = rankinherit.extend([_rank_row(1, [2], priority=1), _rank_row(2, [3]), _rank_row(3)], {2: 40.0})
    assert got[2] == 40.0 and got[3] == 40.0 and 1 not in got


def test_inheritance_survives_cycles_bad_dependencies_and_tasks_that_are_not_open():
    from ttp import rankinherit
    cyc = [_rank_row(1, [2], priority=1), _rank_row(2, [1])]
    assert rankinherit.extend(cyc, {}) == {2: math.inf, 1: math.inf}   # 1 is needed by 2, which urgent 1 needs
    rows = [_rank_row(1, [99, "x", None, 1], priority=1), {"id": 2, "depends_on": "not json", "priority": 3, "spec": ""},
            {"id": 3, "depends_on": None, "priority": 3, "spec": None}]
    assert rankinherit.extend(rows, {2: 5.0}) == {1: math.inf, 2: 5.0}   # 99 is not open: no entry for it; 1 needs itself
    assert rankinherit.extend([], {7: 1.0}) == {7: 1.0}   # an owner that is not open adds nothing


def test_a_dependency_of_an_owner_starts_before_routine_work_of_a_better_priority(env, monkeypatch):
    p = make(env)
    from ttp import obligations
    p.db.x("UPDATE tasks SET status='done'")
    dep = p.db.add_task("dep", "s", kind="code", tier="light", origin="user", priority=4)
    owner = p.db.add_task("owner", "s", kind="code", tier="light", origin="user", priority=4, depends_on=[dep])
    other = p.db.add_task("other", "s", kind="code", tier="light", origin="user", priority=2)
    monkeypatch.setattr(obligations, "owner_due", lambda db: {owner: time.time() + 600})
    assert [t["id"] for t in p.db.ready_tasks()] == [dep, other]
    monkeypatch.setattr(obligations, "owner_due", lambda db: {})
    assert [t["id"] for t in p.db.ready_tasks()] == [other, dep]
    monkeypatch.setattr(obligations, "owner_due", lambda db: 1 / 0)   # unreadable: only the boost is lost
    assert [t["id"] for t in p.db.ready_tasks()] == [other, dep]


def test_a_run_whose_supervisor_is_gone_is_lost_though_its_lease_is_young(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    run_dir = tmp_path / "lost"
    run_dir.mkdir()
    (run_dir / "lease").touch()   # renewed seconds ago: the old rule waited for it to go stale first
    (run_dir / "runner.pid").write_text(f"{gone.pid}\n123456\n")
    row = {"id": 1, "dir": str(run_dir), "boot_id": d.boot, "pid": gone.pid}
    assert d._run_alive(row) is False
    # Bypasses: a live supervisor with a matching start, no runner record, a finished run. A young lease
    # from another boot is no sign of life; only that run's own supervisor still up is.
    from ttp import runner
    (run_dir / "runner.pid").write_text(f"{os.getpid()}\n{runner.proc_start(os.getpid())}\n")
    assert d._run_alive(dict(row, pid=os.getpid())) is True
    (run_dir / "runner.pid").unlink()
    assert d._run_alive(dict(row, pid=gone.pid)) is False   # a gone recorded process is gone, record or no record
    (run_dir / "runner.pid").write_text(f"{gone.pid}\n123456\n")
    assert d._run_alive(dict(row, boot_id="another-boot")) is False
    (run_dir / "runner.pid").write_text(f"{os.getpid()}\n{runner.proc_start(os.getpid())}\n")
    assert d._run_alive(dict(row, pid=os.getpid(), boot_id="another-boot")) is True
    (run_dir / "runner.pid").write_text(f"{gone.pid}\n123456\n")
    (run_dir / "exit.json").write_text("{}")
    assert d._run_alive(row) is False


def _lost_run(p, d, tmp_path, tid, name):
    """A worker run of task tid whose supervisor died on this boot a moment ago (lease still young, its
    recorded start matching no process), reaped by the daemon's own pass. Returns the run's row."""
    import json
    run_dir = tmp_path / name
    run_dir.mkdir()
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    (run_dir / "runner.pid").write_text(f"{gone.pid}\n123456\n")
    (run_dir / "lease").touch()
    (run_dir / "output.jsonl").write_text(json.dumps({"_session": "s", "_cost": 0.1}))
    (run_dir / "run.json").write_text(json.dumps({"cwd": str(p.root)}))
    p.db.update_task(tid, status="running")
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id,pid,note) VALUES(?,?,?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 90, "running", str(run_dir), d.boot, gone.pid,
            json.dumps({"spec_sha": "x"})))
    d.reap_runs()
    return p.db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))


def test_a_lost_runner_s_task_is_ready_again_on_the_same_pass_that_finds_it(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    p.db.x("UPDATE tasks SET status='done'")
    tid = p.db.add_task("crashed", "s", kind="work", tier="light", origin="user", priority=2)
    t0 = time.time()
    run = _lost_run(p, d, tmp_path, tid, "first")
    assert run["status"] == "lost"
    task = p.db.task(tid)
    # No attempt spent and no retry timer: the first loss in a row is dispatched at the next tick.
    assert task["status"] == "queued" and task["attempts"] == 0 and not task["not_before"], dict(task)
    assert tid in [t["id"] for t in p.db.ready_tasks()]
    assert time.time() - t0 < 120   # the whole find-and-requeue is far inside the 2 min target


def test_a_run_a_reboot_cut_waits_for_its_replacement_from_the_daemon_start_not_through_the_downtime(
        env, tmp_path):
    p = make(env)
    from ttp import replacegap
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    _lost_to_reboot(p, d, tmp_path, tid)
    lost = p.db.one("SELECT id, note FROM runs WHERE task=? AND status='lost'", (tid,))
    assert json.loads(lost["note"])["daemon_at"] == d._started, lost["note"]
    p.db.x("UPDATE runs SET ended=? WHERE id=?", (d._started - 300, lost["id"]))   # 5 min of downtime
    p.db.x("INSERT INTO runs(task,role,provider,started,ended,status) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", d._started + 60, d._started + 90, "ok"))
    got = replacegap.gaps(p.db, 86400.0, d._started + 120)
    assert [round(g["gap_s"]) for g in got] == [60], got   # not 360: the host was down, nobody could start it


def test_replacement_gaps_run_from_the_last_sign_of_life_to_the_next_worker_run(env):
    p = make(env)
    from ttp import replacegap
    now = 1_000_000.0
    tid = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
    other = p.db.add_task("o", "s", kind="work", tier="light", origin="user")
    ins = "INSERT INTO runs(task,role,provider,started,ended,status) VALUES(?,?,?,?,?,?)"
    p.db.x(ins, (tid, "worker", "fake", now - 900, now - 600, "lost"))
    p.db.x(ins, (tid, "worker", "fake", now - 540, now - 100, "ok"))     # replaced 60 s after the loss
    p.db.x(ins, (other, "worker", "fake", now - 500, now - 400, "lost"))  # no next run yet: open until now
    p.db.x(ins, (other, "coordinator", "fake", now - 380, now - 370, "ok"))   # a coordinator run is no replacement
    p.db.x(ins, (tid, "worker", "fake", now - 9e5, now - 8e5, "lost"))   # older than the window
    got = {g["task"]: g for g in replacegap.gaps(p.db, 86400.0, now)}
    assert got[tid]["gap_s"] == 60.0 and not got[tid]["open"]
    assert got[other]["gap_s"] == 400.0 and got[other]["open"]
    s = replacegap.summary(p.db, 86400.0, now)
    assert s["count"] == 2 and s["over_target"] == 1 and s["open"] == 1 and s["p90_s"] == 400.0
    assert replacegap.summary(p.db, 10.0, now)["count"] == 0


def test_a_lost_run_whose_own_hand_off_stood_is_neither_waiting_for_a_replacement_nor_work_lost(env):
    p = make(env)
    from ttp import db as dbm, replacegap
    now = 1_000_000.0
    stood = p.db.add_task("stood", "s", kind="work", tier="light", origin="user")
    waits = p.db.add_task("waits", "s", kind="work", tier="light", origin="user")
    ins = "INSERT INTO runs(task,role,provider,started,ended,status,cost_usd) VALUES(?,?,?,?,?,?,?)"
    a = p.db.x(ins, (stood, "worker", "fake", now - 900, now - 600, "lost", 2.5))
    b = p.db.x(ins, (waits, "worker", "fake", now - 900, now - 600, "lost", 1.0))
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (stood,))   # the daemon accepted its result.json
    got = {g["task"]: g for g in replacegap.gaps(p.db, 86400.0, now)}
    assert stood not in got and got[waits]["open"], got
    assert replacegap.summary(p.db, 86400.0, now)["count"] == 1
    lost = [{"run": a, "task": stood, "usd": 2.5}, {"run": b, "task": waits, "usd": 1.0}]
    p.db.x("INSERT INTO events(ts,source,kind,text,data) VALUES(?,?,?,?,?)",
           (now - 500, "host", "boot", "boot", json.dumps({"lost": lost, "lost_usd": 3.5})))
    rows = p.db.boots(now - 86400)[0]["lost"]
    assert [x["kept"] for x in rows] == [True, False]
    assert dbm.host_line(p.db.boots(now - 86400)).endswith("1 not restarted, $1.00 of work lost")


def test_a_review_of_an_owner_takes_its_due_and_starts_before_a_routine_review(env, monkeypatch):
    from ttp import obligations, rankinherit
    rows = [dict(_rank_row(5), kind="review", labels='["auto_review:1"]'), dict(_rank_row(6), kind="review", title="Review #2: x"),
            dict(_rank_row(7, [8]), kind="review", title="Review #1: again"), _rank_row(8), dict(_rank_row(9), title="Review #1")]
    assert rankinherit.extend(rows, {1: 100.0}) == {1: 100.0, 5: 100.0, 7: 100.0, 8: 100.0}   # 9 is no review; 2 owns nothing
    p = make(env)
    p.db.x("UPDATE tasks SET status='done'")
    owner = p.db.add_task("owner", "s", kind="code", tier="light", origin="user", priority=4)
    other = p.db.add_task("other", "s", kind="code", tier="light", origin="user", priority=4)
    p.db.x("UPDATE tasks SET status='done' WHERE id IN (?, ?)", (owner, other))
    routine = p.db.add_task(f"Review #{other}: other", "s", kind="review", tier="light", origin="daemon", priority=2)
    ordered = p.db.add_task(f"Review #{owner}: owner", "s", kind="review", tier="light", origin="daemon", priority=4)
    monkeypatch.setattr(obligations, "owner_due", lambda db: {owner: time.time() + 600})
    assert [t["id"] for t in p.db.ready_tasks()][:2] == [ordered, routine]
    monkeypatch.setattr(obligations, "owner_due", lambda db: {})
    assert [t["id"] for t in p.db.ready_tasks()][:2] == [routine, ordered]
