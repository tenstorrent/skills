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

# A child subreaper standing in for `systemd --user`, itself detached (not under the test): it starts the
# launcher, so the detached stand-in is reparented here when the launcher exits (inside a PID namespace or
# not), writes its pid to LC_PID, then waits on its orphans. It keeps the test's TTP_* variables (checkreap's
# marker among them; the launcher gets its environment without the LC_DROP names) and ends once it has no
# child left, or after LC_REAPER_S whatever happens.
_LC_REAPER = """
import ctypes, os, signal, subprocess, sys
signal.alarm(int(os.environ.get("LC_REAPER_S", "150")))
ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)   # PR_SET_CHILD_SUBREAPER
drop = set(os.environ.get("LC_DROP", "").split(os.pathsep))
env = {k: v for k, v in os.environ.items() if k not in drop}
pid = subprocess.run(sys.argv[1:], capture_output=True, text=True, check=True, env=env).stdout.strip()
open(os.environ["LC_PID"] + ".tmp", "w").write(pid)
os.replace(os.environ["LC_PID"] + ".tmp", os.environ["LC_PID"])
while True:
    try:
        os.wait()
    except ChildProcessError:
        break
"""


@pytest.fixture
def lc_spawn(tmp_path, monkeypatch):
    """_lc_spawn for this test; every stand-in it started is stopped at teardown, whether the test passed
    or failed."""
    started = []
    yield lambda *a, **k: _lc_spawn(tmp_path, monkeypatch, *a, started=started, **k)
    _lc_end(*started)


def _lc_spawn(tmp_path, monkeypatch, env_extra, name="run", cwd=None, started=None, reaper_s=150):
    """A process whose command line is `<dir>/ttp checks -- pytest`, holding a checks slot and a ticket
    under the test's checks slot folder; (proc, slot folder)."""
    from ttp import checkcap, checkreap, jobstop, project
    home = tmp_path / "ttp-home"
    monkeypatch.setattr(project, "HOME_DIR", home)
    slots = checkcap.slot_dir()
    slots.mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / f"{name}-bin"
    bindir.mkdir()
    (bindir / "ttp").write_text(_LC_FAKE)
    ready = tmp_path / f"{name}.ready"
    pidfile = tmp_path / f"{name}.pid"
    work = cwd or tmp_path / f"{name}-work"
    work.mkdir(parents=True, exist_ok=True)
    extra = {**env_extra, "LC_SLOTS": str(slots), "LC_READY": str(ready), "LC_PID": str(pidfile),
             "LC_SLOT": str(len(list(slots.glob("checks.*.lock")))), "LC_REAPER_S": str(reaper_s)}
    # the stand-in carries only its own identity (and checkreap's marker); the subreaper keeps the test's
    drop = [k for k in os.environ if k.startswith("TTP_") and k != checkreap.MARKER and k not in extra]
    # Started detached, as a nohup'd run is: its launcher exits, so it lands on the subreaper stand-in, which
    # the caller's guard holds as an ancestor (as `systemd --user` is of the daemon and a CLI caller).
    detach = ("import subprocess,sys\nprint(subprocess.Popen(sys.argv[1:], start_new_session=True, "
              "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)")
    launch = subprocess.run([sys.executable, "-c", detach, sys.executable, "-c", _LC_REAPER, sys.executable, "-c",
                             detach, sys.executable, str(bindir / "ttp"), "checks", "--", "pytest"],
                            cwd=work, capture_output=True, text=True, check=True,
                            env={**os.environ, **extra, "LC_DROP": os.pathsep.join(drop)})
    reaper = _LcProc(int(launch.stdout))
    if started is not None:
        started.append(reaper)
    monkeypatch.setattr(jobstop, "_guarded", lambda p, guarded=jobstop._guarded: guarded(p) | {reaper.pid})
    deadline = time.monotonic() + 20
    while not (ready.exists() and pidfile.exists()) and time.monotonic() < deadline:
        time.sleep(0.02)
    if not (ready.exists() and pidfile.exists()):
        _lc_end(reaper)
        raise AssertionError("the stand-in did not take its slot")
    proc = _LcProc(int(pidfile.read_text()), reaper)
    if started is not None:
        started.append(proc)
    return proc, slots


class _LcProc:
    """A detached stand-in, known by pid and start time (not this test's child, so never waited on)."""

    def __init__(self, pid, reaper=None):
        from ttp import locks
        self.pid, self.start, self.reaper = pid, locks.start_time(pid), reaper

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
        if proc.reaper:
            _lc_end(proc.reaper)


def test_a_leftover_checks_run_with_a_deleted_folder_and_ended_run_is_reaped_and_its_slot_freed(env, tmp_path, monkeypatch, lc_spawn):
    """The bench-cell case: no project or task identity, its working folder and TTP_RUN_DIR deleted. The
    dry run lists it and stops nothing; the daemon's pass stops it, frees its slot, drops its ticket and
    writes the audit line to the project log and the checks waits log."""
    from ttp import checkcap, leftoverchecks, locks
    from ttp.daemon import Daemon
    p = make(env)
    cell = tmp_path / "cell"
    run_dir = cell / "run"
    run_dir.mkdir(parents=True)
    proc, slots = lc_spawn({"TTP_RUN_DIR": str(run_dir)}, cwd=cell / "work")
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


def test_a_checks_run_whose_run_ended_is_reaped_but_one_that_outlives_its_run_by_design_is_not(env, tmp_path, monkeypatch, lc_spawn):
    """An attached checks run still waiting after its run wrote exit.json is a leftover (past GRACE_S); a
    detached one (locks.OUTLIVES_RUN_ENV) whose folder exists is not."""
    from ttp import leftoverchecks, leftoverstop, locks
    monkeypatch.setattr(leftoverstop, "GRACE_S", 0.0)
    p = make(env)
    ended, kept = tmp_path / "ended-run", tmp_path / "kept-run"
    ended.mkdir()
    kept.mkdir()
    proc, _ = lc_spawn({"TTP_RUN_DIR": str(ended)}, name="a")
    keep, _ = lc_spawn({"TTP_RUN_DIR": str(kept), locks.OUTLIVES_RUN_ENV: "1"}, name="b")
    try:
        time.sleep(0.05)
        for d in (ended, kept):
            (d / "exit.json").write_text("{}")
        found = leftoverchecks.find(p)
        assert [(f["pid"], f["why"]) for f in found] == [(proc.pid, "its run has ended")], found
    finally:
        _lc_end(proc, keep)


def test_live_identityless_and_other_users_checks_runs_are_never_reaped(env, tmp_path, monkeypatch, lc_spawn):
    """Untouched: a running task's checks run (even with its run folder gone); one with no project or task
    identity whose working folder exists and whose run folder is live; one holding a `ttp lock` slot; and
    any process of another OS user."""
    from ttp import leftoverchecks
    p = make(env)
    tid = p.db.add_task("t", "s", kind="code")
    p.db.x("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    live_run = tmp_path / "live-run"
    live_run.mkdir()
    running, _ = lc_spawn({"TTP_PROJECT": str(p.base), "TTP_TASK": str(tid),
                                                   "TTP_RUN_DIR": str(tmp_path / "gone")}, name="a")
    bare, _ = lc_spawn({"TTP_RUN_DIR": str(live_run)}, name="b")
    locked_cwd = tmp_path / "c-work"
    locker, _ = lc_spawn({}, name="c", cwd=locked_cwd)
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


def test_a_slot_holder_with_its_own_lifetime_locks_is_reaped_but_one_holding_a_ttp_lock_slot_is_not(env, tmp_path, monkeypatch, lc_spawn):
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
    a, _ = lc_spawn({"LC_EXTRA": os.pathsep.join(map(str, own))}, name="a", cwd=a_cwd)
    b, _ = lc_spawn({"LC_EXTRA": str(p.state / "locks" / "suite.0.lock")}, name="b", cwd=b_cwd)
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


class _LcClock:
    """The daemon module's `time`, with a monotonic clock the test moves (the step budget reads it)."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def __getattr__(self, name):
        return getattr(time, name)


def test_the_leftover_checks_step_keeps_its_budget_and_finishes_many_leftovers_over_several_ticks(env, tmp_path, monkeypatch, lc_spawn):
    """Five leftovers whose stops take 25 s each (on the step's clock): no tick spends more than the step
    budget plus the one stop it started in budget, the step resumes on the next tick (not a minute later),
    and every leftover is stopped."""
    from ttp import daemon as dmod
    from ttp import leftoverchecks
    p = make(env)
    procs = []
    try:
        for n in range(5):
            cell = tmp_path / f"cell{n}"
            (cell / "run").mkdir(parents=True)
            proc, _ = lc_spawn({"TTP_RUN_DIR": str(cell / "run")}, name=f"r{n}", cwd=cell / "work")
            procs.append(proc)
            shutil.rmtree(cell)
        clock = _LcClock()
        monkeypatch.setattr(dmod, "time", clock)
        real_stop = leftoverchecks.stop

        def slow_stop(*a, **k):
            clock.t += 25
            return real_stop(*a, **k)
        monkeypatch.setattr(leftoverchecks, "stop", slow_stop)
        d = dmod.Daemon(p.base)
        spent = []
        for _ in range(4):
            before = clock.t
            d._run_step(d.reap_leftover_checks, "reap_leftover_checks")
            spent.append(clock.t - before)
        assert all(proc.gone_within(15) for proc in procs)
        assert max(spent) <= dmod.STEP_BUDGET_S + 25, spent
        assert len([s for s in spent if s]) >= 2, f"the stops spread over ticks: {spent}"
        text = (p.logs / "daemon.log").read_text()
        assert all(text.count(f"stopped leftover ttp checks pid {proc.pid} ") == 1 for proc in procs), text[-2000:]
    finally:
        _lc_end(*procs)


@pytest.mark.parametrize("where", ["read", "stop"])
def test_a_leftover_checks_read_or_stop_that_hangs_times_out_and_does_not_hold_the_step(env, tmp_path, monkeypatch, lc_spawn, where):
    """A /proc read (or a stop's signalling) that hangs on one process: the step leaves it after
    leftoverchecks.PROBE_S, stops the other leftover, logs the hung one once (not again while it is still
    stuck) and handles it on a later tick once it answers."""
    from ttp import daemon as dmod
    from ttp import leftoverchecks, leftoverstop
    p = make(env)
    monkeypatch.setattr(leftoverchecks, "PROBE_S", 0.5, raising=False)
    monkeypatch.setattr(leftoverstop, "TERM_S", 0.2)
    procs = []
    release = threading.Event()
    try:
        for n in range(2):
            cell = tmp_path / f"cell{n}"
            (cell / "run").mkdir(parents=True)
            proc, _ = lc_spawn({"TTP_RUN_DIR": str(cell / "run")}, name=f"h{n}", cwd=cell / "work")
            procs.append(proc)
            shutil.rmtree(cell)
        hung, other = procs
        if where == "read":
            real_argv = leftoverchecks._argv

            def argv(pid):
                if pid == hung.pid:
                    release.wait(30)
                return real_argv(pid)
            monkeypatch.setattr(leftoverchecks, "_argv", argv)
        else:
            real_end = leftoverstop.end

            def end(targets, *a, **k):
                if any(t["pid"] == hung.pid for t in targets):
                    release.wait(30)
                return real_end(targets, *a, **k)
            monkeypatch.setattr(leftoverstop, "end", end)
        d = dmod.Daemon(p.base)

        def tick():   # in time: well before the hang (30 s) ends
            d._reap_checks_at = 0.0
            began = time.monotonic()
            d.reap_leftover_checks()
            return time.monotonic() - began < 12
        log = p.logs / "daemon.log"
        assert tick(), "the hung process holds the step"
        assert other.gone_within(15) and hung.alive()
        said = [x for x in log.read_text().splitlines() if f" {hung.pid} " in x and "left " in x]
        assert len(said) == 1, log.read_text()[-2000:]
        assert tick(), "the hung process holds the step"
        assert [x for x in log.read_text().splitlines() if f" {hung.pid} " in x and "left " in x] == said, \
            "logged once while it stays stuck"
        release.set()
        leftoverchecks._stuck[hung.pid].join(15)
        assert tick() and hung.gone_within(15)
    finally:
        release.set()
        _lc_end(*procs)


def test_reading_who_joined_a_done_tasks_checks_times_out_and_does_not_hold_the_judge(env, tmp_path, monkeypatch):
    """The checks-join records (and the /proc reads of their pids) are read in a bounded thread: a hang
    there gives "timeout" within PROBE_S, then "stuck" at once while it lasts, and the verdict once it ends."""
    from ttp import checkjoin, leftoverchecks, leftoverstop
    p = make(env)
    monkeypatch.setattr(leftoverchecks, "PROBE_S", 0.5, raising=False)
    monkeypatch.setattr(leftoverchecks, "_stuck", {}, raising=False)
    monkeypatch.setattr(leftoverstop, "GRACE_S", 1e6)   # its run ended too recently to count; its task long ago
    tid = p.db.add_task("t", "s", kind="code")
    p.db.x("UPDATE tasks SET status='done', updated=0 WHERE id=?", (tid,))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "exit.json").write_text("{}")
    env_ = {"TTP_PROJECT": str(p.base), "TTP_TASK": str(tid), "TTP_RUN_ID": "7", "TTP_RUN_DIR": str(run_dir)}
    release = threading.Event()
    real_records = checkjoin.records

    def records(*a, **k):
        release.wait(30)
        return real_records(*a, **k)
    monkeypatch.setattr(checkjoin, "records", records)
    slots = tmp_path / "slots"
    slots.mkdir()
    try:
        began = time.monotonic()
        verdict, why = leftoverchecks.judge(p, os.getpid(), slots, set(), env_)
        assert time.monotonic() - began < 12 and verdict == "timeout", (verdict, why)
        assert leftoverchecks.judge(p, os.getpid(), slots, set(), env_)[0] == "stuck"
        release.set()
        leftoverchecks._stuck[os.getpid()].join(15)
        assert leftoverchecks.judge(p, os.getpid(), slots, set(), env_) == ("leftover", f"its task #{tid} is done")
    finally:
        release.set()


def test_only_the_caller_a_running_worker_or_the_daemon_claims_a_checks_run_not_the_callers_other_ancestors(env, tmp_path, monkeypatch):
    """An orphan lands on the nearest child subreaper, outside a PID namespace `systemd --user`, which is also
    an ancestor of the daemon and of a CLI caller: a caller's ancestor in its chain claims nothing, while the
    caller itself, a running worker run and the live daemon still do."""
    from ttp import jobstop, leftoverchecks, leftoverstop
    p = make(env)
    # A stand-in ancestor the caller's guard holds (stop's group guard keeps it): the real parent chain
    # depends on the host (in a PID namespace the caller's parent is pid 1, which the guard skips).
    up = 999998
    monkeypatch.setattr(jobstop, "_guarded", lambda p, guarded=jobstop._guarded: guarded(p) | {up})
    guarded = leftoverstop._guarded(p)
    assert up in guarded
    facts = {"untouchable": None, "held_lock": False, "cwd_gone": True, "run_dir_is": False, "ended": None, "began": None}
    judge = lambda chain: leftoverchecks.judge(p, chain[0], tmp_path, guarded, {}, {**facts, "chain": chain})
    assert judge([999999, up]) == ("leftover", "its working folder is deleted")
    assert judge([999999, os.getpid(), up]) == ("keep", f"a live worker run or the caller (pid {os.getpid()}) claims it")
    monkeypatch.setattr(leftoverstop, "_daemon_pid", lambda p: up)
    assert judge([999999, up]) == ("keep", f"a live worker run or the caller (pid {up}) claims it")
    monkeypatch.setattr(leftoverstop, "_daemon_pid", lambda p: None)
    p.db.x("INSERT INTO runs(role,status,pid) VALUES('worker','running',?)", (up,))
    assert judge([999999, up]) == ("keep", f"a live worker run or the caller (pid {up}) claims it")


def test_the_subreaper_stand_in_keeps_the_tests_ttp_variables_and_ends_with_its_stand_in_or_on_its_own(env, tmp_path, monkeypatch, lc_spawn):
    """The _lc_spawn helper's subreaper keeps the test's TTP_* (checkreap's marker among them), which the
    stand-in gets only as its own identity and the marker; the subreaper ends once its stand-in ends, and
    after its time limit while the stand-in still runs."""
    from ttp import checkreap, procowner
    monkeypatch.setenv(checkreap.MARKER, "lc-token")
    monkeypatch.setenv("TTP_PROJECT", str(tmp_path / "elsewhere"))
    proc, _ = lc_spawn({"TTP_RUN_DIR": str(tmp_path)}, name="a")
    reaper_env, own = procowner.environ(proc.reaper.pid), procowner.environ(proc.pid)
    assert reaper_env[checkreap.MARKER] == "lc-token" and reaper_env["TTP_PROJECT"] == str(tmp_path / "elsewhere")
    assert own[checkreap.MARKER] == "lc-token" and own["TTP_RUN_DIR"] == str(tmp_path) and "TTP_PROJECT" not in own
    os.kill(proc.pid, signal.SIGKILL)
    assert proc.gone_within(5) and proc.reaper.gone_within(5), "the subreaper outlived its stand-in"
    late, _ = lc_spawn({}, name="b", reaper_s=4)   # long enough for a loaded host to start the stand-in under it
    assert late.reaper.gone_within(_load_scaled(20)) and late.alive(), "the subreaper outlived its time limit"


def test_a_test_s_process_tree_orphaned_in_its_pytest_temp_folder_is_stopped_and_nothing_else(env, tmp_path, monkeypatch):
    """`ttp checks --reap`'s second pass (test_orphans): an orphaned tree whose top has its cwd in a
    pytest-of-<user>/pytest-<n> folder is listed by the dry run and stopped with its child; an orphan
    outside such a folder, and one too young, are left alone."""
    import getpass
    from ttp import leftoverchecks, leftoverstop
    if not leftoverstop.PROC.exists():
        pytest.skip("needs /proc")
    p = make(env)
    inside = tmp_path / f"pytest-of-{getpass.getuser()}" / "pytest-7" / "test_x0"
    inside.mkdir(parents=True)
    tree = subprocess.Popen(["sh", "-c", "sleep 60 & wait"], cwd=inside, start_new_session=True)
    other = subprocess.Popen(["sleep", "60"], cwd="/", start_new_session=True)
    tops = {tree.pid, other.pid}
    real_ppid = leftoverchecks._ppid
    monkeypatch.setattr(leftoverchecks, "_ppid", lambda n: 1 if n in tops else real_ppid(n))
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not [n for n in leftoverchecks.pids() if real_ppid(n) == tree.pid]:
            time.sleep(0.05)
        (child,) = [n for n in leftoverchecks.pids() if real_ppid(n) == tree.pid]
        mine = lambda rs: [r for r in rs if r["pid"] in tops]
        assert mine(leftoverchecks.test_orphans(p, set(), dry_run=True, min_age=3600)) == []   # too young
        (f,) = mine(leftoverchecks.test_orphans(p, set(), dry_run=True, min_age=0))
        assert f["pid"] == tree.pid and sorted(f["pids"]) == sorted([tree.pid, child]) and "would stop" in f["text"]
        assert tree.poll() is None, "the dry run stops nothing"
        (f,) = mine(leftoverchecks.test_orphans(p, set(), min_age=0))
        assert tree.wait(timeout=15) is not None
        deadline = time.time() + 10
        while time.time() < deadline and leftoverstop._start(child):
            time.sleep(0.05)
        assert not leftoverstop._start(child), "its child is stopped too"
        assert other.poll() is None, "an orphan outside a pytest temp folder is left alone"
    finally:
        for proc in (tree, other):
            if proc.poll() is None:
                proc.kill()
                proc.wait()
