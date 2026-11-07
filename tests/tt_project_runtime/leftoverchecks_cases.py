# Leftover `ttp checks` run tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new leftoverchecks tests here.
from __future__ import annotations

# A stand-in `ttp checks` run: holds a checks slot and a queue ticket under the slot folder, and each
# LC_EXTRA path ("{pid}" filled in), then sleeps.
_LC_FAKE = """
import fcntl, os, sys, time
slots = os.environ["LC_SLOTS"]
os.makedirs(os.path.join(slots, "checks.queue"), exist_ok=True)
held = []
extra = [x.replace("{pid}", str(os.getpid())) for x in os.environ.get("LC_EXTRA", "").split(os.pathsep) if x]
for name in ("checks.%s.lock" % os.environ["LC_SLOT"], os.path.join("checks.queue", "%017.6f-%d" % (time.time(), os.getpid())), *extra):
    os.makedirs(os.path.dirname(os.path.join(slots, name)), exist_ok=True)
    f = open(os.path.join(slots, name), "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    held.append(f)
open(os.environ["LC_READY"], "w").write("held")
time.sleep(120)
"""


def _lc_spawn(tmp_path, monkeypatch, env_extra, name="run", cwd=None):
    """A process whose command line is `<dir>/ttp checks -- pytest`, holding a checks slot and a ticket
    under the test's checks slot folder; (proc, slot folder)."""
    from ttp import checkcap, project
    home = tmp_path / "ttp-home"
    monkeypatch.setattr(project, "HOME_DIR", home)
    slots = checkcap.slot_dir()
    slots.mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / f"{name}-bin"
    bindir.mkdir()
    (bindir / "ttp").write_text(_LC_FAKE)
    ready = tmp_path / f"{name}.ready"
    work = cwd or tmp_path / f"{name}-work"
    work.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("TTP_")}
    # Started detached, as a nohup'd run is: its launcher exits, so the test (a guarded ancestor) is not its parent.
    launch = subprocess.run([sys.executable, "-c", "import subprocess,sys\nprint(subprocess.Popen(sys.argv[1:], "
                             "start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                             "stderr=subprocess.DEVNULL).pid)", sys.executable, str(bindir / "ttp"), "checks", "--",
                             "pytest"], cwd=work, capture_output=True, text=True, check=True,
                            env={**env, **env_extra, "LC_SLOTS": str(slots), "LC_READY": str(ready),
                                 "LC_SLOT": str(len(list(slots.glob("checks.*.lock"))))})
    proc = _LcProc(int(launch.stdout))
    deadline = time.monotonic() + 20
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists(), "the stand-in did not take its slot"
    return proc, slots


class _LcProc:
    """A detached stand-in, known by pid and start time (not this test's child, so never waited on)."""

    def __init__(self, pid):
        from ttp import locks
        self.pid, self.start = pid, locks.start_time(pid)

    def alive(self):
        from ttp import locks
        try:
            state = pathlib.Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        return state not in ("Z", "X") and locks.start_time(self.pid) == self.start

    def gone_within(self, s):
        deadline = time.monotonic() + s
        while self.alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self.alive()


def _lc_end(*procs):
    for proc in procs:
        if proc.alive():
            os.kill(proc.pid, signal.SIGKILL)


def test_a_leftover_checks_run_with_a_deleted_folder_and_ended_run_is_reaped_and_its_slot_freed(env, tmp_path, monkeypatch):
    """The bench-cell case: no project or task identity, its working folder and TTP_RUN_DIR deleted. The
    dry run lists it and stops nothing; the daemon's pass stops it, frees its slot, drops its ticket and
    writes the audit line to the project log and the checks waits log."""
    from ttp import checkcap, leftoverchecks, locks
    from ttp.daemon import Daemon
    p = make(env)
    cell = tmp_path / "cell"
    run_dir = cell / "run"
    run_dir.mkdir(parents=True)
    proc, slots = _lc_spawn(tmp_path, monkeypatch, {"TTP_RUN_DIR": str(run_dir)}, cwd=cell / "work")
    try:
        shutil.rmtree(cell)
        assert not locks.any_free([slots / "checks.0.lock"])
        (f,) = leftoverchecks.reap(p, dry_run=True)
        assert f["pid"] == proc.pid and "would stop" in f["text"] and f["why"] == "its working folder is deleted"
        time.sleep(0.2)
        assert proc.alive(), "the dry run stops nothing"
        d = Daemon(p.base)
        d.reap_leftover_checks()
        assert proc.gone_within(15)
        assert locks.any_free([slots / "checks.0.lock"])
        assert not [t for t in (slots / "checks.queue").iterdir() if not t.name.startswith(".")]
        assert f"stopped leftover ttp checks pid {proc.pid}" in (p.logs / "daemon.log").read_text()
        recs = [json.loads(x) for x in (p.state / checkcap.WAITS_LOG).read_text().splitlines()]
        assert [r["reaped"] for r in recs] == [proc.pid] and recs[0]["run_dir"] == str(run_dir), recs
        d._reap_checks_at = 0.0
        assert leftoverchecks.reap(p) == []   # nothing left
    finally:
        _lc_end(proc)


def test_a_checks_run_whose_run_ended_is_reaped_but_one_that_outlives_its_run_by_design_is_not(env, tmp_path, monkeypatch):
    """An attached checks run still waiting after its run wrote exit.json is a leftover (past GRACE_S); a
    detached one (locks.OUTLIVES_RUN_ENV) whose folder exists is not."""
    from ttp import leftoverchecks, leftoverstop, locks
    monkeypatch.setattr(leftoverstop, "GRACE_S", 0.0)
    p = make(env)
    ended, kept = tmp_path / "ended-run", tmp_path / "kept-run"
    ended.mkdir()
    kept.mkdir()
    proc, _ = _lc_spawn(tmp_path, monkeypatch, {"TTP_RUN_DIR": str(ended)}, name="a")
    keep, _ = _lc_spawn(tmp_path, monkeypatch, {"TTP_RUN_DIR": str(kept), locks.OUTLIVES_RUN_ENV: "1"}, name="b")
    try:
        time.sleep(0.05)
        for d in (ended, kept):
            (d / "exit.json").write_text("{}")
        found = leftoverchecks.find(p)
        assert [(f["pid"], f["why"]) for f in found] == [(proc.pid, "its run has ended")], found
    finally:
        _lc_end(proc, keep)


def test_live_identityless_and_other_users_checks_runs_are_never_reaped(env, tmp_path, monkeypatch):
    """Untouched: a running task's checks run (even with its run folder gone); one with no project or task
    identity whose working folder exists and whose run folder is live; one holding a `ttp lock` slot; and
    any process of another OS user."""
    from ttp import leftoverchecks
    p = make(env)
    tid = p.db.add_task("t", "s", kind="code")
    p.db.x("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    live_run = tmp_path / "live-run"
    live_run.mkdir()
    running, _ = _lc_spawn(tmp_path, monkeypatch, {"TTP_PROJECT": str(p.base), "TTP_TASK": str(tid),
                                                   "TTP_RUN_DIR": str(tmp_path / "gone")}, name="a")
    bare, _ = _lc_spawn(tmp_path, monkeypatch, {"TTP_RUN_DIR": str(live_run)}, name="b")
    locked_cwd = tmp_path / "c-work"
    locker, _ = _lc_spawn(tmp_path, monkeypatch, {}, name="c", cwd=locked_cwd)
    lock = p.state / "locks" / "suite.0.lock"
    try:
        shutil.rmtree(locked_cwd)
        found = {f["pid"] for f in leftoverchecks.find(p)}
        assert running.pid not in found and bare.pid not in found and locker.pid in found
        # the same leftover, also holding a `ttp lock` slot outside the checks slot folder
        monkeypatch.setattr(leftoverchecks, "_fds", lambda pid, real=leftoverchecks._fds:
                            real(pid) + ([str(lock)] if pid == locker.pid else []))
        assert locker.pid not in {f["pid"] for f in leftoverchecks.find(p)}, "it holds a ttp lock"
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
        assert leftoverchecks.find(p) == [], "another user's processes are never read"
    finally:
        _lc_end(running, bare, locker)


def test_a_slot_holder_with_its_own_lifetime_locks_is_reaped_but_one_holding_a_ttp_lock_slot_is_not(env, tmp_path, monkeypatch):
    """A real slot-holding `ttp checks` also holds its clean checkout's lock (cleantree.PREFIX/LOCK), its
    detached supervisor's check-runs lock (checkdirs.LOCK) and its log's .lock: none of them is a `ttp
    lock` slot, so with its folder deleted it is reaped. One holding a file in the project's lock folder
    is kept."""
    from ttp import checkdirs, cleantree, leftoverchecks
    p = make(env)
    own = [tmp_path / "shm" / f"{cleantree.PREFIX}{{pid}}-x" / cleantree.LOCK,
           p.state / checkdirs.FOLDER / checkdirs.LOCK,
           p.state / checkdirs.FOLDER / "abc" / checkdirs.LOCK,
           tmp_path / "runlogs" / "checks.log.lock"]
    a_cwd, b_cwd = tmp_path / "a-work", tmp_path / "b-work"
    a, _ = _lc_spawn(tmp_path, monkeypatch, {"LC_EXTRA": os.pathsep.join(map(str, own))}, name="a", cwd=a_cwd)
    b, _ = _lc_spawn(tmp_path, monkeypatch, {"LC_EXTRA": str(p.state / "locks" / "suite.0.lock")}, name="b", cwd=b_cwd)
    try:
        held = leftoverchecks._fds(a.pid)
        assert any(f.endswith(f"{cleantree.PREFIX}{a.pid}-x/{cleantree.LOCK}") for f in held), held
        shutil.rmtree(a_cwd)
        shutil.rmtree(b_cwd)
        found = {f["pid"]: f["why"] for f in leftoverchecks.find(p)}
        assert found == {a.pid: "its working folder is deleted"}, found
    finally:
        _lc_end(a, b)


def test_ttp_checks_reap_logs_each_stop_to_the_project_log_but_a_dry_run_does_not(env, monkeypatch, capsys):
    from ttp import cli, leftoverchecks
    p = make(env)
    monkeypatch.setattr(cli, "here", lambda: p)
    monkeypatch.setattr(leftoverchecks, "reap", lambda p, dry_run=False: [{"text": f"reap line dry={dry_run}"}])
    for args in (["checks", "--reap", "--dry-run"], ["checks", "--reap"]):
        with pytest.raises(SystemExit) as e:
            cli.main(args)
        assert e.value.code == 0
    assert capsys.readouterr().out.splitlines() == ["reap line dry=True", "reap line dry=False"]
    text = (p.logs / "daemon.log").read_text() if (p.logs / "daemon.log").exists() else ""
    assert "reap line dry=False" in text and "dry=True" not in text
