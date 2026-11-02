# Ended tasks' leftover process tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new leftoverstop tests here.
from __future__ import annotations

_LS_HOLDER = """
import fcntl, sys, time
f = open(sys.argv[1], "a+")
fcntl.flock(f, fcntl.LOCK_EX)
open(sys.argv[2], "w").write("held")
time.sleep(120)
"""


def _ls_task(p, status="done", ended_ago=1000.0, run_status="ok"):
    """A task of `status` with one recorded run that ended `ended_ago` seconds ago; (task id, run dir)."""
    tid = p.db.add_task("leftovers", "s", kind="work", tier="light", origin="user")
    run_dir = p.runs / f"ls-{tid}"
    run_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    p.db.x("INSERT INTO runs(task,role,provider,started,ended,status,dir) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", now - ended_ago - 60, None if run_status == "running" else now - ended_ago,
            run_status, str(run_dir)))
    p.db.x("UPDATE tasks SET status=?, updated=? WHERE id=?", (status, now - ended_ago, tid))
    return tid, run_dir


def _ls_spawn(tmp_path, lock, env_extra, name="holder"):
    """A process that takes the flock on `lock` and sleeps, with `env_extra` in its environment."""
    ready = tmp_path / f"{name}.ready"
    env = {k: v for k, v in os.environ.items() if not k.startswith("TTP_")}
    lock.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", _LS_HOLDER, str(lock), str(ready)], env={**env, **env_extra})
    deadline = time.monotonic() + 20
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists(), "the holder did not take its lock"
    return proc


def _ls_marks(p, tid, run_dir):
    return {"TTP_PROJECT": str(p.base), "TTP_TASK": str(tid), "TTP_TASK_ID": str(tid), "TTP_RUN_DIR": str(run_dir)}


def _ls_end(*procs):
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_an_ended_task_s_leftover_holding_a_lane_is_reaped_by_the_sweep_and_the_lane_freed(env, tmp_path):
    """A done task's test script still holding a lock lane is the lane's only opener: the sweep ends it,
    the lane is free again and one receipt (task, pid, command, lock) goes to the feed."""
    from ttp import leftoverstop, locks
    p = make(env)
    tid, run_dir = _ls_task(p, "done")
    lock = p.state / "locks" / "suite-gate.0.lock"
    proc = _ls_spawn(tmp_path, lock, _ls_marks(p, tid, run_dir))
    try:
        assert not locks.any_free([lock])
        (r,) = leftoverstop.sweep(p)
        assert proc.wait(timeout=10) is not None
        assert locks.any_free([lock])
        assert r["task"] == tid and r["pids"] == [proc.pid] and r["freed"] == ["suite-gate.0.lock"], r
        (ev,) = p.db.q("SELECT * FROM events WHERE kind=?", (leftoverstop.EVENT,))
        assert ev["task"] == tid and f"pid {proc.pid}" in ev["text"] and "suite-gate.0.lock" in ev["text"]
        assert leftoverstop.sweep(p) == []   # nothing left: no second receipt
    finally:
        _ls_end(proc)


def test_the_sweep_leaves_a_running_a_just_ended_or_a_shared_holder_alone(env, tmp_path):
    """No reap for a running task's process, for a task that ended under GRACE_S ago, for a lock that
    an unattributable process also has open (the lock is not held only by the ended task's own), or for a
    process the task's hand-off lists in leftover_procs with how and when it stops."""
    from ttp import leftoverstop, locks
    p = make(env)
    running, run_r = _ls_task(p, "running", run_status="running")
    fresh, run_f = _ls_task(p, "done", ended_ago=5)
    done, run_d = _ls_task(p, "failed")
    kept, run_k = _ls_task(p, "done")
    lr, lf, ld, lk = (p.state / "locks" / f"gate-{x}.0.lock" for x in ("r", "f", "d", "k"))
    procs = [_ls_spawn(tmp_path, lr, _ls_marks(p, running, run_r), "r"),
             _ls_spawn(tmp_path, lf, _ls_marks(p, fresh, run_f), "f"),
             _ls_spawn(tmp_path, ld, _ls_marks(p, done, run_d), "d"),
             _ls_spawn(tmp_path, lk, _ls_marks(p, kept, run_k), "k")]
    p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps({"status": "done", "leftover_procs": [
        {"pid": procs[3].pid, "stop": "its own timeout", "ends": "in an hour"}]}), kept))
    other = open(ld, "a")   # this test process: no task marker, it also has the failed task's lock open
    try:
        assert leftoverstop.sweep(p) == []
        assert all(x.poll() is None for x in procs)
        assert not any(locks.any_free([x]) for x in (lr, lf, ld, lk))
    finally:
        other.close()
        _ls_end(*procs)


def test_stop_route_ends_an_ended_task_s_leftovers_and_refuses_what_it_cannot_attribute(env, tmp_path, capsys,
                                                                                        monkeypatch):
    """`ttp leftover-procs --stop <task>` stops a cancelled task's own lane holder and prints it; named pids
    of a running task, another task, another project, another user, a run dir the task never had or no
    marker at all are each refused with the reason, and stay alive."""
    from ttp import cli, leftoverstop, locks
    p = make(env)
    monkeypatch.chdir(p.root)
    tid, run_dir = _ls_task(p, "cancelled")
    live, live_dir = _ls_task(p, "running", run_status="running")
    other, other_dir = _ls_task(p, "done")
    lock = p.state / "locks" / "lane.0.lock"
    own = _ls_spawn(tmp_path, lock, _ls_marks(p, tid, run_dir), "own")
    keep = {
        "running": _ls_spawn(tmp_path, p.state / "locks" / "k1.lock", _ls_marks(p, live, live_dir), "k1"),
        "task": _ls_spawn(tmp_path, p.state / "locks" / "k2.lock", _ls_marks(p, other, other_dir), "k2"),
        "project": _ls_spawn(tmp_path, tmp_path / "k3.lock", {**_ls_marks(p, tid, run_dir),
                                                              "TTP_PROJECT": str(tmp_path / "elsewhere" / "tt-project")}, "k3"),
        "rundir": _ls_spawn(tmp_path, tmp_path / "k4.lock", {**_ls_marks(p, tid, run_dir),
                                                             "TTP_RUN_DIR": str(tmp_path / "not-a-run")}, "k4"),
        "none": _ls_spawn(tmp_path, tmp_path / "k5.lock", {}, "k5"),
    }
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["leftover-procs", "--stop", str(tid), "--dry-run"])
        got = capsys.readouterr()   # the task-marked process with a run dir the task never had is refused
        assert e.value.code == 2 and f"would stop pid {own.pid}" in got.out and f"would stop pid {keep['rundir'].pid}" not in got.out
        assert f"refused pid {keep['rundir'].pid}" in got.err, got.err
        assert own.poll() is None
        for name, proc in keep.items():
            with pytest.raises(SystemExit) as e:
                cli.main(["leftover-procs", "--stop", str(tid), "--pid", str(proc.pid)])
            err = capsys.readouterr().err
            assert e.value.code == 2 and f"refused pid {proc.pid}" in err, (name, err)
            assert {"running": f"task #{live}'s process", "task": f"task #{other}'s process",
                    "project": "another project's", "rundir": "not attributable", "none": "no task marker"}[name] in err
        real_uid = leftoverstop._uid
        monkeypatch.setattr(leftoverstop, "_uid", lambda pid: -1 if pid == own.pid else real_uid(pid))
        with pytest.raises(SystemExit) as e:
            cli.main(["leftover-procs", "--stop", str(tid), "--pid", str(own.pid)])
        assert e.value.code == 2 and "another user's process" in capsys.readouterr().err
        monkeypatch.setattr(leftoverstop, "_uid", real_uid)
        with pytest.raises(SystemExit) as e:
            cli.main(["leftover-procs", "--stop", str(live)])
        assert e.value.code == 2 and "a running task's processes are its own" in capsys.readouterr().err
        assert all(x.poll() is None for x in [own, *keep.values()])
        with pytest.raises(SystemExit) as e:
            cli.main(["leftover-procs", "--stop", str(tid)])
        out = capsys.readouterr().out
        assert e.value.code == 2 and f"stopped pid {own.pid}" in out and "locks freed: lane.0.lock" in out, out
        assert own.wait(timeout=10) is not None and locks.any_free([lock])
        assert all(x.poll() is None for x in keep.values())
        assert p.db.one("SELECT task FROM events WHERE kind=?", (leftoverstop.EVENT,))["task"] == tid
    finally:
        _ls_end(own, *keep.values())


def test_a_sandboxed_stop_request_is_served_by_the_daemon_s_pass(env, tmp_path, monkeypatch):
    """A caller in another PID namespace queues the stop; the jobstop pass checks again and stops it."""
    from ttp import jobstop, leftoverstop, push
    p = make(env)
    tid, run_dir = _ls_task(p, "exhausted")
    proc = _ls_spawn(tmp_path, p.state / "locks" / "q.0.lock", _ls_marks(p, tid, run_dir))
    try:
        leftoverstop.request(p, tid, None, "7")
        (r,) = jobstop.serve_requests(p)
        assert r["pids"] == [proc.pid] and proc.wait(timeout=10) is not None
        assert not list((p.state / jobstop.QUEUE).glob("leftovers-*"))
    finally:
        _ls_end(proc)
