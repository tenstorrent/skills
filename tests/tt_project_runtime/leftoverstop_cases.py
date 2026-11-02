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


def _ls_spawn(tmp_path, lock, env_extra, name="holder", args=()):
    """A process that takes the flock on `lock` and sleeps, with `env_extra` in its environment."""
    ready = tmp_path / f"{name}.ready"
    env = {k: v for k, v in os.environ.items() if not k.startswith("TTP_")}
    lock.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", _LS_HOLDER, str(lock), str(ready), *args], env={**env, **env_extra})
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


def test_the_daemon_carrying_an_ended_task_s_environment_is_never_stopped(env, tmp_path, capsys, monkeypatch):
    """The live daemon (its daemon.pid) may carry a run's environment (daemon._on_pass_env): neither the
    route nor the sweep stops it. A missing or stale pid file guards nothing extra and does not fail."""
    from ttp import cli, leftoverstop, locks
    p = make(env)
    monkeypatch.chdir(p.root)
    tid, run_dir = _ls_task(p, "done")
    lock = p.state / "locks" / "d.0.lock"
    base = leftoverstop._guarded(p)   # no pid file
    daemon = _ls_spawn(tmp_path, lock, _ls_marks(p, tid, run_dir), "daemon", args=("ttp", "daemon"))
    try:
        (p.state / "daemon.pid").write_text(f"{daemon.pid}\n")
        assert leftoverstop._guarded(p) == base | {daemon.pid}
        assert leftoverstop.sweep(p) == [] and daemon.poll() is None and not locks.any_free([lock])
        with pytest.raises(SystemExit) as e:
            cli.main(["leftover-procs", "--stop", str(tid), "--pid", str(daemon.pid)])
        assert e.value.code == 2 and "or the daemon" in capsys.readouterr().err
        with pytest.raises(SystemExit) as e:
            cli.main(["leftover-procs", "--stop", str(tid)])
        assert f"stopped pid {daemon.pid}" not in capsys.readouterr().out and daemon.poll() is None
    finally:
        _ls_end(daemon)
    (p.state / "daemon.pid").write_text(f"{daemon.pid}\n")   # stale: that pid is gone
    assert leftoverstop._guarded(p) == base
    (p.state / "daemon.pid").write_text("not a pid\n")
    assert leftoverstop._guarded(p) == base


_LS_LEADER = """
import os, subprocess, sys, time
env = dict(os.environ, **dict(a.split("=", 1) for a in sys.argv[2:]))
c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], env=env)
open(sys.argv[1] + ".tmp", "w").write(str(c.pid))
os.rename(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(120)
"""


def _ls_group(tmp_path, env_extra, child_env, name="leader"):
    """A process group: an attributed leader in a session of its own, and its child with `child_env`
    over the leader's environment; (leader Popen, child pid)."""
    ready = tmp_path / f"{name}.child"
    env = {k: v for k, v in os.environ.items() if not k.startswith("TTP_")}
    leader = subprocess.Popen([sys.executable, "-c", _LS_LEADER, str(ready), *(f"{k}={v}" for k, v in child_env.items())],
                              env={**env, **env_extra}, start_new_session=True)
    deadline = time.monotonic() + 20
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists(), "the leader did not start its child"
    return leader, int(ready.read_text())


def _ls_gone(pid, wait=10.0):
    from ttp import leftoverstop
    deadline = time.monotonic() + wait
    while leftoverstop._start(pid) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    return leftoverstop._start(pid) is None


def _ls_kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def test_a_stopped_group_leader_takes_its_group_and_a_check_command_counts_as_its_task_s(env, tmp_path, capsys,
                                                                                        monkeypatch):
    """An ended task's process that leads a group goes with its group: its child, whose TTP_RUN_DIR was
    rewritten (as checkdirs.environment does), ends too. A `ttp checks` command, whose TTP_RUN_DIR is a
    check-run folder whose owner.json names the task's run's checks.pid, is the task's; one whose folder
    names another task's run is refused."""
    from ttp import checkdirs, cli
    p = make(env)
    monkeypatch.chdir(p.root)
    tid, run_dir = _ls_task(p, "failed")
    other, other_dir = _ls_task(p, "done")
    leader, child = _ls_group(tmp_path, _ls_marks(p, tid, run_dir), {"TTP_RUN_DIR": str(tmp_path / "rewritten")})
    folders = {}
    for name, owner in (("own", run_dir), ("other", other_dir)):
        d = p.state / checkdirs.FOLDER / f"cr-{name}"
        d.mkdir(parents=True)
        (d / checkdirs.OWNER).write_text(json.dumps({"marker": str(owner / "checks.pid")}))
        folders[name] = d
    check = _ls_spawn(tmp_path, tmp_path / "c.lock", _ls_marks(p, tid, folders["own"]), "check")
    foreign = _ls_spawn(tmp_path, tmp_path / "f.lock", _ls_marks(p, tid, folders["other"]), "foreign")
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["leftover-procs", "--stop", str(tid), "--dry-run"])
        got = capsys.readouterr()
        assert e.value.code == 2, got
        for pid in (leader.pid, child, check.pid):
            assert f"would stop pid {pid}" in got.out, got.out
        assert f"refused pid {foreign.pid}" in got.err and "not attributable" in got.err
        with pytest.raises(SystemExit) as e:
            cli.main(["leftover-procs", "--stop", str(tid)])
        out = capsys.readouterr().out
        assert f"stopped pid {child}" in out and f"stopped pid {check.pid}" in out, out
        assert leader.wait(timeout=10) is not None and _ls_gone(child) and check.wait(timeout=10) is not None
        assert foreign.poll() is None
    finally:
        _ls_kill(child)
        _ls_end(leader, check, foreign)


def test_a_group_with_a_protected_or_foreign_member_is_not_signalled_only_its_leader_goes(env, tmp_path, capsys,
                                                                                        monkeypatch):
    """A leader whose group holds another task's process, or a guarded one (here the daemon), is stopped
    alone: the member stays, and the output says why the group was not signalled (exit 2)."""
    from ttp import cli, leftoverstop
    p = make(env)
    monkeypatch.chdir(p.root)
    tid, run_dir = _ls_task(p, "cancelled")
    other, _ = _ls_task(p, "running", run_status="running")
    for case in ("task", "guarded"):
        leader, child = _ls_group(tmp_path, _ls_marks(p, tid, run_dir),
                                  {"TTP_TASK": str(other)} if case == "task" else {}, f"leader-{case}")
        if case == "guarded":
            monkeypatch.setattr(leftoverstop, "_daemon_pid", lambda p, child=child: child)
        try:
            with pytest.raises(SystemExit) as e:
                cli.main(["leftover-procs", "--stop", str(tid)])
            got = capsys.readouterr()
            assert e.value.code == 2 and f"stopped pid {leader.pid}" in got.out, (case, got)
            assert f"stopped pid {child}" not in got.out
            assert f"pid {leader.pid}: its process group was not signalled" in got.err, (case, got.err)
            assert {"task": f"task #{other}'s process", "guarded": "or the daemon"}[case] in got.err
            assert leader.wait(timeout=10) is not None
            time.sleep(0.2)
            assert leftoverstop._start(child) is not None, case
        finally:
            _ls_kill(child)
            _ls_end(leader)


def test_an_ended_task_whose_run_row_still_says_running_is_refused(env, tmp_path, capsys, monkeypatch):
    """A done task whose run row still says 'running' (a run that never recorded its end) is not ended
    for this purpose: `--stop` refuses it and stops nothing, and the sweep leaves it."""
    from ttp import cli, leftoverstop, locks
    p = make(env)
    monkeypatch.chdir(p.root)
    tid, run_dir = _ls_task(p, "done", run_status="running")
    lock = p.state / "locks" / "r.0.lock"
    proc = _ls_spawn(tmp_path, lock, _ls_marks(p, tid, run_dir))
    try:
        for argv in ([], ["--pid", str(proc.pid)], ["--dry-run"]):
            with pytest.raises(SystemExit) as e:
                cli.main(["leftover-procs", "--stop", str(tid), *argv])
            got = capsys.readouterr()
            assert e.value.code == 2 and "with a running run" in got.err and "stopped pid" not in got.out, (argv, got)
        assert leftoverstop.sweep(p) == []
        assert proc.poll() is None and not locks.any_free([lock])
        assert not p.db.one("SELECT id FROM events WHERE kind=?", (leftoverstop.EVENT,))
    finally:
        _ls_end(proc)
