# Process-liveness tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new liveness.pid_state and caller tests here.
from __future__ import annotations

import types

import pytest


@pytest.fixture
def gone_pid():
    """A pid that ran and was reaped: dead in this namespace."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


@pytest.fixture(params=["foreign", "same"])
def pidns(request, monkeypatch):
    """Runs a test as a worker in a PID namespace other than the daemon's ("foreign": TTP_PIDNS names
    another one, as in a sandbox with its own) and in the daemon's own ("same")."""
    from ttp import liveness
    here = liveness.pid_ns()
    if here is None:
        pytest.skip("no /proc/self/ns/pid here")
    monkeypatch.setenv("TTP_PIDNS", "pid:[1]" if request.param == "foreign" else here)
    return request.param


def test_pid_state_is_unknown_from_a_foreign_pid_namespace_and_judges_in_the_recording_one(gone_pid, pidns):
    from ttp import liveness
    want = liveness.UNKNOWN if pidns == "foreign" else liveness.DEAD
    assert liveness.pid_state(gone_pid) == want
    assert liveness.pid_state(os.getpid()) == (liveness.UNKNOWN if pidns == "foreign" else liveness.ALIVE)
    # a record that keeps its own namespace is judged against that one, not the daemon's
    assert liveness.pid_state(gone_pid, ns=liveness.pid_ns()) == liveness.DEAD
    assert liveness.pid_state(gone_pid, ns="pid:[2]") == liveness.UNKNOWN
    for bad in (None, "", "x", 0, -5):
        assert liveness.pid_state(bad) == liveness.DEAD


def test_pid_state_judges_locally_when_no_namespace_is_known(gone_pid, monkeypatch):
    from ttp import liveness
    monkeypatch.delenv("TTP_PIDNS", raising=False)
    assert liveness.pid_state(gone_pid) == liveness.DEAD
    monkeypatch.setattr(liveness, "pid_ns", lambda: None)   # no /proc: cannot tell, judge as before
    monkeypatch.setenv("TTP_PIDNS", "pid:[1]")
    assert liveness.pid_state(gone_pid) == liveness.DEAD


def test_liveness_callers_never_read_a_foreign_namespace_pid_as_dead(gone_pid, pidns, tmp_path):
    from ttp import activity, checkspill, chatgone, cli, progress, route, upjournal
    from ttp.runner import boot_id
    foreign = pidns == "foreign"
    assert checkspill._alive(gone_pid) is foreign
    assert cli._pid_alive(gone_pid) is foreign
    assert cli._listener_alive(gone_pid, "c1") is foreign
    (tmp_path / "listen-c1.pid").write_text(str(gone_pid))
    assert chatgone.listener_alive(types.SimpleNamespace(state=tmp_path), "c1") is foreign
    assert progress._alive({"pid": gone_pid, "boot": boot_id()}) is foreign
    assert route._held({"pid": gone_pid, "until": time.time() + 60, "start": "1"}, time.time()) is foreign
    assert upjournal.dead({"pid": gone_pid, "boot": boot_id()}) is not foreign
    assert activity._alive(gone_pid) is False   # never "running" on a guess: the state stays "unknown"


def test_checkspill_status_names_a_foreign_namespace_claim_unknown(gone_pid, pidns, monkeypatch):
    from ttp import checkspill
    monkeypatch.setattr(checkspill, "claim_dir", lambda: pathlib.Path("/nonexistent"))
    rec = {"host": "spill1", "pid": gone_pid, "task": "4", "since": time.time()}
    monkeypatch.setattr(checkspill, "holders", lambda project_name=None: [rec])
    line = checkspill.status_lines("x")[0]
    assert ("unknown (other PID namespace)" in line) is (pidns == "foreign")


def test_detach_series_with_a_foreign_namespace_driver_is_running_not_lost(gone_pid, pidns, tmp_path):
    from ttp import detach
    d = tmp_path / "drv"
    d.mkdir()
    (d / "pid").write_text(f"{gone_pid}\n")
    st, why = detach.series(d)
    if pidns == "foreign":
        assert (st, why) == ("running", f"series driver pid {gone_pid}: unknown (other PID namespace)")
    else:
        assert st == "lost" and "is dead" in why


@pytest.fixture
def held_lock(tmp_path):
    """A series driver dir whose <dir>/lock a live process (the driver) holds with flock; yields (dir, release)."""
    import fcntl
    d = tmp_path / "gate"
    d.mkdir()
    fd = os.open(d / "lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    state = {"fd": fd}

    def release():
        if state["fd"] is not None:
            os.close(state["fd"])
            state["fd"] = None
    yield d, release
    release()


def test_detach_series_judges_a_held_lock_never_a_pid_from_another_namespace(held_lock, gone_pid, pidns):
    """A gate started inside a sandboxed check records its pid in that check's own PID namespace: here the
    same number is dead, or another live process (a kernel thread). Its held lock says it runs; once the
    kernel frees the lock with no EXIT it is lost, whatever process now has that pid number."""
    from ttp import detach
    d, release = held_lock
    for other in (gone_pid, 1, os.getpid()):
        (d / "pid").write_text(f"{other}\n")
        assert detach.series(d) == ("running", ""), other
    release()
    for other in (1, os.getpid()):   # a live process here owns the number: still lost, never "running"
        (d / "pid").write_text(f"{other}\n")
        st, why = detach.series(d)
        assert st == "lost" and "lock" in why and "free with no EXIT" in why, (other, why)
    (d / "pid").unlink()   # a lock alone names a driver too
    assert detach.series(d)[0] == "lost"


def test_detach_series_reports_a_yielded_gate_by_its_exit_code_not_as_gone(held_lock, pidns, monkeypatch):
    """A gate that yields (exit 75) writes EXIT before the kernel frees its lock: the reader gets the 75 and
    keeps it queued, never "gone with no rc"."""
    from ttp import detach
    d, release = held_lock
    (d / "pid").write_text("1\n")
    (d / "EXIT").write_text("75\n")
    release()
    assert detach.series(d) == ("ended", "75")
    # EXIT written in the instant between the reader's first look and the lock going free
    (d / "EXIT").unlink()
    real = detach.locks.free_state

    def free_then_exit(path):
        (d / "EXIT").write_text("75\n")
        return real(path)
    monkeypatch.setattr(detach.locks, "free_state", free_then_exit)
    assert detach.series(d) == ("ended", "75")


@pytest.mark.skipif(not shutil.which("flock"), reason="no flock(1) here")
def test_remote_series_check_judges_the_drivers_lock_not_its_pid(held_lock, tmp_path):
    from ttp import cli
    d, release = held_lock

    def check():
        return subprocess.run(["sh", "-c", cli._REMOTE_CHECK, "sh", str(d)], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    (d / "pid").write_text("1\n")   # alive here, but not the driver
    assert check() == "running lock"
    release()
    assert check() == "series"
    (d / "pid").unlink()
    assert check() == "series"
    (d / "EXIT").write_text("75\n")
    assert check() == "rc 75"


def test_recover_setup_never_rolls_back_under_a_foreign_namespace_setup(gone_pid, pidns, tmp_path, monkeypatch):
    from ttp import cli, project
    from ttp.runner import boot_id
    monkeypatch.setattr(project, "HOME_DIR", tmp_path)
    (tmp_path / "lib").mkdir()
    jr = tmp_path / "lib" / cli.SETUP_JOURNAL
    jr.write_text(json.dumps({"pid": gone_pid, "boot": boot_id()}))
    line = cli._recover_setup()
    if pidns == "foreign":
        assert line == "" and jr.exists()
    else:
        assert "rolled back" in line and not jr.exists()


def test_listener_state_never_signals_a_pid_it_cannot_place(pidns):
    """From a foreign namespace a listener pid that names some other local process is UNKNOWN (its number
    may be a host listener's), so a new listener neither signals it nor waits on it; one that is a
    listener for the chat here is this namespace's own and ALIVE. In the daemon's namespace: as before."""
    from ttp import cli, liveness
    other = subprocess.Popen(["sleep", "60"])
    mine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "listen", "c1"])
    try:
        want = liveness.UNKNOWN if pidns == "foreign" else liveness.DEAD
        assert cli._listener_state(other.pid, "c1") == want
        assert cli._listener_alive(other.pid, "c1") is (pidns == "foreign")
        assert cli._listener_state(mine.pid, "c1") == liveness.ALIVE
    finally:
        other.kill()
        mine.kill()
        other.wait()
        mine.wait()


def _ns_join_rec(tmp_path, rid, pid, ns, **extra):
    from ttp import checkjoin
    d = checkjoin.folder(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": rid, "pid": pid, "started": 1.0, "pidns": ns, "task": "7", "head": "a" * 40, "commit_ts": 1.0,
           "key": "k", "group": "g", "run": "1", "arrived": 1.0, "ts": time.time(), "phase": "running", **extra}
    (d / f"{rid}.json").write_text(json.dumps(rec))
    return d / f"{rid}.json"


def test_checkjoin_keeps_and_never_joins_a_record_of_another_pid_namespace(gone_pid, pidns, tmp_path):
    from ttp import checkjoin, liveness
    foreign = pidns == "foreign"
    path = _ns_join_rec(tmp_path, "lead", gone_pid, "pid:[1]" if foreign else liveness.pid_ns())
    recs = checkjoin.records(tmp_path, prune=True)
    assert path.exists() is foreign and len(recs) == int(foreign)
    me = {"id": "me", "task": "8", "key": "k", "group": "g8", "head": "b" * 40, "commit_ts": 2.0,
          "arrived": 2.0, "ts": time.time(), "phase": "waiting"}
    assert checkjoin.target(me, recs) is None
    if foreign:
        assert recs[0]["alive"] is None
        assert checkjoin.resolve("lead", recs) == ("wait", recs[0])   # never lost
        _ns_join_rec(tmp_path, "fol", gone_pid, "pid:[1]", phase="joined", follows="lead")
        assert any(liveness.FOREIGN in line for line in checkjoin.status_lines(tmp_path))


def test_devq_wait_line_never_says_gone_for_a_pid_it_cannot_place(gone_pid, pidns, tmp_path):
    from ttp import devq
    d = tmp_path / "devq"
    for sub in ("queue", "running", "done"):
        (d / sub).mkdir(parents=True)
    (d / "queue" / f"{int(time.time() * 1e9):020d}-jb.json").write_text("{}")
    (d / "running" / "ja.json").write_text("{}")
    (d / "running" / "ja.state.json").write_text(json.dumps({"cur": {"pid": gone_pid}}))
    line = devq.wait_line(d, "jb")
    assert ("gone" in line) is (pidns == "same") and ("unknown" in line) is (pidns == "foreign"), line
    assert devq.parse_wait(line)["holder"]["alive"] is (None if pidns == "foreign" else False)


def test_checkjoin_judges_a_record_without_a_namespace_here(monkeypatch, tmp_path):
    """No /proc (macOS) records no namespace: the run is judged in this process, alive while it runs."""
    from ttp import checkjoin
    from ttp.runner import proc_start
    monkeypatch.delenv("TTP_PIDNS", raising=False)
    _ns_join_rec(tmp_path, "mac", os.getpid(), "", started=proc_start(os.getpid()))
    assert [r["alive"] for r in checkjoin.records(tmp_path)] == [True]


def test_checkjoin_prunes_an_old_foreign_record_without_an_outcome_and_keeps_a_fresh_one(gone_pid, pidns, tmp_path):
    """A sandboxed run killed before its outcome leaves a record no process can judge: past FOREIGN_S it is
    pruned (and no longer listed); a fresh one, a live one and a foreign one with an outcome stay."""
    from ttp import checkjoin, liveness
    from ttp.runner import proc_start
    foreign = pidns == "foreign"
    ns = "pid:[1]" if foreign else liveness.pid_ns()
    now = time.time()
    old = now - checkjoin.FOREIGN_S - 60
    gone = _ns_join_rec(tmp_path, "gone", gone_pid, ns, ts=old, since=old, phase="joined", follows="x")
    fresh = _ns_join_rec(tmp_path, "fresh", gone_pid, ns, ts=old, since=now - 60, phase="joined", follows="x")
    live = _ns_join_rec(tmp_path, "live", os.getpid(), liveness.pid_ns(), ts=old, started=proc_start(os.getpid()))
    done = _ns_join_rec(tmp_path, "done", gone_pid, ns, ts=old, outcome={"passed": True, "ts": now - 60})
    lines = checkjoin.status_lines(tmp_path, now)
    assert len(lines) == int(foreign) and all(liveness.FOREIGN in ln for ln in lines), lines   # not the old one
    recs = {r["id"]: r for r in checkjoin.records(tmp_path, now, prune=True)}
    assert not gone.exists() and "gone" not in recs
    assert fresh.exists() is foreign and live.exists() and done.exists()
    if foreign:
        assert recs["fresh"]["alive"] is None and recs["done"]["alive"] is None
    assert recs["live"]["alive"] is True


def test_devq_waits_names_a_holder_pid_it_cannot_place_as_of_another_pid_namespace(gone_pid, pidns, tmp_path):
    from ttp import devq, liveness, lockreap
    d = tmp_path / "devq"
    for sub in ("queue", "running", "done"):
        (d / sub).mkdir(parents=True)
    (d / "queue" / f"{int(time.time() * 1e9):020d}-jb.json").write_text("{}")
    (d / "running" / "ja.json").write_text("{}")
    (d / "running" / "ja.state.json").write_text(json.dumps({"cur": {"pid": gone_pid}}))
    kv = {}
    db = types.SimpleNamespace(kv=lambda k, default=None: kv.get(k, default), set_kv=kv.__setitem__,
                               task=lambda tid: {"id": tid, "status": "queued"})
    lockreap.note_devq_wait(db, 5, "r1", "ttp devq probe r1 jb", devq.wait_line(d, "jb"), 1)
    [w] = lockreap.devq_waits(db, 60, time.time() + 3600)
    [h] = w["holders"]
    if pidns == "foreign":
        assert (h["state"], h["why"]) == ("unknown", f"its pid {gone_pid} is {liveness.FOREIGN}")
    else:
        assert (h["state"], h["why"]) == ("dead", f"its pid {gone_pid} is gone")
