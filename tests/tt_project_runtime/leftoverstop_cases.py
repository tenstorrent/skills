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
    rewritten (as checkdirs.environment does), ends too. A `ttp checks` command, whose TTP_RUN_DIR is its
    allocation's report folder (checkdirs.new + environment + push.check_env, as `ttp checks` builds it)
    and whose owner.json names the task's run's checks.pid, is the task's; one whose allocation names
    another task's run is refused."""
    from ttp import checkdirs, cli, push
    p = make(env)
    monkeypatch.chdir(p.root)
    tid, run_dir = _ls_task(p, "failed")
    other, other_dir = _ls_task(p, "done")
    leader, child = _ls_group(tmp_path, _ls_marks(p, tid, run_dir), {"TTP_RUN_DIR": str(tmp_path / "rewritten")})
    folders = {}
    for name, owner in (("own", run_dir), ("other", other_dir)):
        alloc = checkdirs.new(p.state, owner / "checks.pid")
        output = checkdirs.environment(alloc, {})["TTP_CHECK_OUTPUT_DIR"]
        folders[name] = push.check_env("checks", run_dir=output)["TTP_RUN_DIR"]
        assert Path(folders[name]).parent.name == "check-reports", folders[name]
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


# Sandbox trees of ended runs (sandboxreap): a bwrap root (here a shell script named bwrap, so its comm
# is "bwrap") with one child standing in for its proxy listener.
_SR_BWRAP = """#!/bin/sh
sleep 300 </dev/null >/dev/null 2>&1 &
echo $! > "$SR_READY.tmp" && mv "$SR_READY.tmp" "$SR_READY"
wait
"""


def _sr_bin(tmp_path):
    exe = tmp_path / "srbin" / "bwrap"
    if not exe.exists():
        exe.parent.mkdir(parents=True)
        exe.write_text(_SR_BWRAP)
        exe.chmod(0o755)
    return exe


def _sr_ready(ready, wait=20.0):
    deadline = time.monotonic() + wait
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists(), "the fake sandbox did not start"
    return int(ready.read_text())


def _sr_spawn(tmp_path, name, env_extra, wrap=False):
    """A fake sandbox tree in its own session with `env_extra`: (the process started, the bwrap root's pid,
    its child's pid). `wrap`: started by a shell of the same environment (the tree is not orphaned)."""
    ready = tmp_path / f"{name}.ready"
    env = {k: v for k, v in os.environ.items() if not k.startswith("TTP_")}
    env = {**env, **env_extra, "SR_READY": str(ready)}
    exe = str(_sr_bin(tmp_path))
    argv = ["sh", "-c", f'"{exe}" & echo $! > "$SR_READY.root"; wait'] if wrap else [exe]
    proc = subprocess.Popen(argv, env=env, start_new_session=True)
    child = _sr_ready(ready)
    root = int((tmp_path / f"{name}.ready.root").read_text()) if wrap else proc.pid
    return proc, root, child


def _sr_alive(pid):
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _sr_marks(p, run):
    return {"TTP_PROJECT": str(p.base), "TTP_RUN_DIR": str(p.runs / run)}


@pytest.mark.skipif(not pathlib.Path("/proc/self/environ").exists(), reason="Linux /proc only")
def test_sandbox_reap_ends_only_an_ended_run_s_orphaned_own_tree(env, tmp_path):
    """reap() ends an ended run's orphaned bwrap tree whole (root and its listener); a live run's tree, a
    `ttp detach` job's, another project's, one whose run dir is outside the runs folder and one a process
    of the same run still owns are not touched; a tree with a guarded member is kept whole and reported."""
    from ttp import sandboxreap
    p = make(env)
    for r in ("ended", "live", "guarded", "owned"):
        (p.runs / r).mkdir(parents=True, exist_ok=True)
    ended = lambda d: pathlib.Path(d).name != "live"
    procs = {}
    try:
        procs["ended"] = _sr_spawn(tmp_path, "ended", _sr_marks(p, "ended"))
        procs["live"] = _sr_spawn(tmp_path, "live", _sr_marks(p, "live"))
        procs["detached"] = _sr_spawn(tmp_path, "detached", {**_sr_marks(p, "ended"), "TTP_DETACHED": "1"})
        procs["project"] = _sr_spawn(tmp_path, "project", {**_sr_marks(p, "ended"),
                                                           "TTP_PROJECT": str(tmp_path / "other" / "tt-project")})
        procs["outside"] = _sr_spawn(tmp_path, "outside", {**_sr_marks(p, "ended"), "TTP_RUN_DIR": str(tmp_path / "x")})
        procs["owned"] = _sr_spawn(tmp_path, "owned", _sr_marks(p, "owned"), wrap=True)
        procs["guarded"] = _sr_spawn(tmp_path, "guarded", _sr_marks(p, "guarded"))
        guarded = {procs["guarded"][2]}
        found, kept = sandboxreap.reap(p.base, ended, guarded, dry=True)
        assert {t["root"] for t in found} == {procs["ended"][1]}, found
        assert {m["pid"] for m in found[0]["members"]} == set(procs["ended"][1:])
        assert [k["root"] for k in kept] == [procs["guarded"][1]], kept
        assert f"member pid {procs['guarded'][2]}" in kept[0]["why"]
        assert all(_sr_alive(pid) for _, *pids in procs.values() for pid in pids), "a dry run signals nothing"
        found, kept = sandboxreap.reap(p.base, ended, guarded, term_s=2)
        procs["ended"][0].wait(timeout=10)
        assert not any(_sr_alive(pid) for pid in procs["ended"][1:])
        for name, (_, *pids) in procs.items():
            if name != "ended":
                assert all(_sr_alive(pid) for pid in pids), name
        assert "1 sandbox tree of ended runs (2 processes; runs ended)" in sandboxreap.summary(found)
    finally:
        for proc, root, child in procs.values():
            for pid in (child, root):
                _ls_kill(pid)
            proc.wait(timeout=10)


@pytest.mark.skipif(not pathlib.Path("/proc/self/environ").exists(), reason="Linux /proc only")
def test_sandbox_trees_of_ended_runs_go_by_the_daemon_sweep_and_the_leftover_procs_route(env, tmp_path, capsys,
                                                                                        monkeypatch):
    """The daemon's sweep ends a tree whose run has its exit.json and writes one receipt; one whose run row
    still says running stays. `ttp leftover-procs --sandboxes --dry-run` names a tree and signals nothing,
    then without --dry-run ends it; exit 0 when nothing was kept."""
    from ttp import cli, sandboxreap
    p = make(env)
    monkeypatch.chdir(p.root)
    done, live = p.runs / "sr-done", p.runs / "sr-live"
    for d in (done, live):
        d.mkdir(parents=True)
    (done / "exit.json").write_text("{}")
    tid = p.db.add_task("sr", "s", kind="work", tier="light", origin="user")
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "running", str(live)))
    procs = []
    try:
        procs.append(_sr_spawn(tmp_path, "done", _sr_marks(p, "sr-done")))
        procs.append(_sr_spawn(tmp_path, "live", _sr_marks(p, "sr-live")))
        (t,) = sandboxreap.sweep(p)
        assert t["root"] == procs[0][1]
        procs[0][0].wait(timeout=10)
        (ev,) = p.db.q("SELECT * FROM events WHERE kind=?", (sandboxreap.EVENT,))
        assert "runs sr-done" in ev["text"] and str(procs[0][1]) in ev["data"]
        assert sandboxreap.sweep(p) == [] and _sr_alive(procs[1][1])
        p.db.x("UPDATE runs SET status='ok' WHERE dir=?", (str(live),))
        with pytest.raises(SystemExit) as e:
            cli.main(["leftover-procs", "--sandboxes", "--dry-run"])
        out = capsys.readouterr().out
        assert e.value.code == 0 and f"would end sandbox tree {procs[1][1]} of run sr-live: 2 processes" in out, out
        assert _sr_alive(procs[1][1])
        with pytest.raises(SystemExit) as e:
            cli.main(["leftover-procs", "--sandboxes"])
        assert e.value.code == 0 and f"ended sandbox tree {procs[1][1]}" in capsys.readouterr().out
        procs[1][0].wait(timeout=10)
        assert not _sr_alive(procs[1][2])
    finally:
        for proc, root, child in procs:
            for pid in (child, root):
                _ls_kill(pid)
            proc.wait(timeout=10)


@pytest.mark.skipif(not pathlib.Path("/proc/self/environ").exists(), reason="Linux /proc only")
def test_the_runner_ends_its_agent_s_group_and_its_own_orphaned_sandbox_tree_at_the_end(env, tmp_path, monkeypatch):
    """When the agent exits, the runner ends what it left in its process group and the run's own bwrap
    tree, which runs in its own session and was orphaned when the agent went."""
    from ttp import runner
    p = make(env)
    monkeypatch.delenv("TTP_DETACHED", raising=False)
    run_dir = p.runs / "sr-runner"
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.md").write_text("x")
    ready, grp = tmp_path / "rn.ready", tmp_path / "grp.pid"
    script = (f'sleep 300 </dev/null >/dev/null 2>&1 & echo $! > {shlex.quote(str(grp))}; '
              f'setsid -f {shlex.quote(str(_sr_bin(tmp_path)))} </dev/null >/dev/null 2>&1; '
              f'while [ ! -f {shlex.quote(str(ready))} ]; do sleep 0.05; done')
    (run_dir / "run.json").write_text(json.dumps({
        "argv": ["sh", "-c", script], "cwd": str(tmp_path), "timeout_s": 60, "provider": "fake",
        "env": {**_sr_marks(p, "sr-runner"), "SR_READY": str(ready)}}))
    pids = []
    try:
        assert runner.supervise(run_dir) == 0
        pids = [int(grp.read_text()), int(ready.read_text())]
        for pid in pids:
            _ls_gone(pid)
            assert not _sr_alive(pid), pid
    finally:
        for pid in pids:
            _ls_kill(pid)


def _sr_cleanup(procs):
    for proc, root, child in procs:
        for pid in (child, root):
            _ls_kill(pid)
        proc.wait(timeout=10)


@pytest.mark.skipif(not pathlib.Path("/proc/self/environ").exists(), reason="Linux /proc only")
def test_the_daemon_sandbox_sweep_keeps_its_step_budget_and_ends_the_rest_on_later_ticks(env, tmp_path, monkeypatch):
    """Many ended runs' trees, each slow to end, never hold one tick step past STEP_BUDGET_S: the sweep stops
    between trees, and later ticks (inside jobstop's sweep interval) resume after the last tree done until
    each tree is ended exactly once."""
    from ttp import daemon as dm
    from ttp import leftoverstop as ls
    p = make(env)
    names = [f"sr-many-{n}" for n in range(4)]
    for n in names:
        (p.runs / n).mkdir(parents=True)
        (p.runs / n / "exit.json").write_text("{}")
    real_end, ends = ls.end, []

    def slow_end(targets, term_s=ls.TERM_S, group_ok=None):
        time.sleep(0.3)   # one tree's end on a loaded host
        ends.append(targets[0]["pid"])
        return real_end(targets, term_s, group_ok)
    monkeypatch.setattr(ls, "end", slow_end)
    monkeypatch.setattr(dm, "STEP_BUDGET_S", 0.2)
    procs = []
    try:
        procs = [_sr_spawn(tmp_path, n, _sr_marks(p, n)) for n in names]
        roots = sorted(root for _, root, _ in procs)
        d = dm.Daemon(p.base)
        for tick in range(4 * len(roots)):   # other sandboxes on the host are items too, one per tick here
            before, t0 = len(ends), time.monotonic()
            d._run_step(d.sweep_sandbox_trees)
            assert time.monotonic() - t0 < 4.0, "the step ran past its budget by more than one tree's end"
            assert len(ends) - before <= 1, "the step went on past its budget"
            if sorted(ends) == roots:
                break
            assert "sandbox_sweep" in d._step_cursor, "the rest are not resumed on the next tick"
        assert tick >= len(roots) - 1 and sorted(ends) == roots and len(set(ends)) == len(ends), ends
        for proc, *_ in procs:
            proc.wait(timeout=10)
        assert not any(_sr_alive(pid) for _, *pids in procs for pid in pids)
    finally:
        _sr_cleanup(procs)


@pytest.mark.skipif(not pathlib.Path("/proc/self/environ").exists(), reason="Linux /proc only")
def test_a_hung_proc_read_never_holds_the_sandbox_sweep_the_runner_end_or_the_scan(env, tmp_path, monkeypatch):
    """A /proc read of a hung process blocks with no timeout: the sweep leaves that tree after ITEM_S, logs it
    once, ends the other tree, skips it while that read still hangs and ends it once the read returns. A
    hung scan of the host's processes ends by WALK_S; the runner's run-end cleanup by END_LEFTOVERS_S."""
    import threading
    from ttp import leftoverstop as ls
    from ttp import runner, sandboxreap
    from ttp.project import Project
    p = make(env)
    for n in ("sr-hung", "sr-fine"):
        (p.runs / n).mkdir(parents=True)
        (p.runs / n / "exit.json").write_text("{}")
    monkeypatch.setattr(sandboxreap, "ITEM_S", 0.5)
    monkeypatch.setattr(sandboxreap, "WALK_S", 0.5)
    release, hung = threading.Event(), set()
    real_env = ls._read_env

    def read_env(pid):
        if pid in hung:
            release.wait(30)
        return real_env(pid)
    monkeypatch.setattr(ls, "_read_env", read_env)

    def bounded(fn, wait=10.0):   # fn runs in a thread of its own: it opens its own Project (sqlite)
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("v", fn()), daemon=True)
        t0 = time.monotonic()
        t.start()
        t.join(wait)
        assert not t.is_alive(), "a hung /proc read held the pass"
        return box["v"], time.monotonic() - t0
    procs, said = [], []
    try:
        procs = [_sr_spawn(tmp_path, n, _sr_marks(p, n)) for n in ("sr-hung", "sr-fine")]
        (_, hroot, hchild), (_, froot, _) = procs
        hung.add(hroot)
        found, took = bounded(lambda: sandboxreap.sweep(Project(p.base), say=said.append))
        assert [t["root"] for t in found] == [froot] and took < 5.0, (found, took)
        assert len(said) == 1 and f"sandbox tree {hroot}" in said[0] and "left for a later pass" in said[0]
        found, took = bounded(lambda: sandboxreap.sweep(Project(p.base), say=said.append))
        assert found == [] and took < 2.0 and len(said) == 1, "a walk still hung is skipped and logged once"
        assert _sr_alive(hroot) and _sr_alive(hchild)
        release.set()
        hung.clear()
        _ls_wait = time.monotonic() + 10
        while sandboxreap._late.get(f"tree {hroot}") and sandboxreap._late[f"tree {hroot}"].is_alive() \
                and time.monotonic() < _ls_wait:
            time.sleep(0.02)
        found, _ = bounded(lambda: sandboxreap.sweep(Project(p.base), say=said.append))
        assert [t["root"] for t in found] == [hroot] and len(said) == 1
        procs[0][0].wait(timeout=10)

        scan_free = threading.Event()
        monkeypatch.setattr(ls, "_uid_pids", lambda: scan_free.wait(30) and [])
        try:
            (found, kept), took = bounded(lambda: sandboxreap.reap(p.base, lambda d: True))
            assert found == [] and [k["late"] for k in kept] == ["scan"] and took < 3.0
        finally:
            scan_free.set()

        gate = threading.Event()
        monkeypatch.setattr(runner, "end_leftovers", lambda *a: gate.wait(30))
        monkeypatch.setattr(runner, "END_LEFTOVERS_S", 0.3)
        try:
            ok, took = bounded(lambda: runner.end_leftovers_within(0, tmp_path, str(p.base)))
            assert ok is False and took < 3.0
        finally:
            gate.set()
    finally:
        release.set()
        _sr_cleanup(procs)


def test_no_shipped_default_starts_a_sandbox(env, monkeypatch):
    """Every run is unsandboxed by default: a Claude worker's settings turn the Bash sandbox off (even one the
    user's own settings turn on) and its launch records no sandbox; the coordinator session's settings do
    the same; `ttp sandbox` runs only when a project turned it on; the project default leaves it off."""
    from ttp import coordtools, project, sandbox
    from ttp.providers.claude import Claude
    monkeypatch.setattr(Claude, "supports", lambda self, flag: True)
    monkeypatch.setattr(Claude, "hook_rewrites", lambda self: False)
    for read_only in (False, True):
        argv, _ = Claude().build(role="worker", model="m", effort="high", cwd="/tmp", budget_usd=1.0,
                                 read_only=read_only, schema=None, restrictions={})
        if "--settings" in argv:
            assert json.loads(argv[argv.index("--settings") + 1])["sandbox"] == {"enabled": False}
        assert Claude().launch_sandbox(argv) == ""
    assert coordtools.hook_settings()["sandbox"] == {"enabled": False}
    assert coordtools.hook_settings(full=True)["sandbox"] == {"enabled": False}
    monkeypatch.delenv(sandbox.ENV, raising=False)
    assert not sandbox.opted_in() and not sandbox.opted_in({})
    assert project.DEFAULT_CONFIG["sandbox"]["checks"] is False
