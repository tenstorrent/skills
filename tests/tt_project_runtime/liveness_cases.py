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
