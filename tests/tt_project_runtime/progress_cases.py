# Driver progress report tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new `ttp progress report` and progress and stall check
# (progress.check) tests here, not there.
from __future__ import annotations


def _report(*args):
    """`ttp progress report ...` in process: its exit code (0 when it returns)."""
    from ttp import cli
    try:
        cli.main(["progress", "report", *args])
    except SystemExit as e:
        return e.code or 0
    return 0


def _lines(p):
    return [json.loads(ln) for ln in (p.state / "progress.jsonl").read_text().splitlines()]


def _driver_reports(p, pid, kinds, key="filler", t0=None):
    from ttp import progress
    from ttp.runner import boot_id
    t0 = time.time() - 600 if t0 is None else t0
    for i, kind in enumerate(kinds):
        progress.append(p.state, {"ts": t0 + i, "key": key, "kind": kind, "reason": f"lock busy (exit 75) #{i}",
                                  "source": f"pid {pid}", "pid": pid, "boot": boot_id(), "job": None})


def test_a_progress_report_appends_one_well_formed_line(env, monkeypatch):
    p = make(env)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_TASK", "12")
    assert _report("refusal", "--key", "filler", "lock busy\n  (exit 75)") == 0
    assert _report("result", "--key", "filler", "batch 3 done") == 0
    first, second = _lines(p)
    assert {"ts", "key", "kind", "reason", "source"} <= set(first), first
    assert (first["key"], first["kind"], first["reason"]) == ("filler", "refusal", "lock busy (exit 75)")
    assert first["pid"] == os.getppid() and "task 12" in first["source"] and first["job"] is None, first
    assert second["kind"] == "result" and second["ts"] >= first["ts"]
    # A bad kind or no key is a usage error that writes nothing; inside a `ttp detach` job the key
    # defaults to its task and name.
    assert _report("progress", "--key", "filler", "x") == 2
    assert _report("refusal", "no key given") == 2
    monkeypatch.setenv("TTP_DETACH_RC", str(env["tmp"] / "run" / "nightly.rc"))
    assert _report("failure", "step 3 exit 4") == 0
    last = _lines(p)[-1]
    assert len(_lines(p)) == 3 and last["key"] == "12/nightly" and last["job"].endswith("nightly.rc"), last
    assert "job nightly" in last["source"], last


def test_a_progress_report_needs_no_daemon_and_exits_0_even_when_it_cannot_write(env, monkeypatch, capsys):
    p = make(env)
    from ttp import progress
    from ttp.cli import daemon_state
    assert daemon_state(p).startswith("NOT RUNNING")
    run_env = {**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_PROJECT": str(p.base)}
    r = subprocess.run([sys.executable, "-m", "ttp", "progress", "report", "failure", "--key", "recheck",
                        "probe exit 4"], env=run_env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert [(x["key"], x["kind"]) for x in _lines(p)] == [("recheck", "failure")]
    # A state folder that cannot take the line: said on stderr, still exit 0, so a `set -e` driver goes on.
    monkeypatch.setenv("TTP_PROJECT", str(p.base))

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(progress, "append", full)
    assert _report("refusal", "--key", "recheck", "busy") == 0
    assert "not recorded" in capsys.readouterr().err


def test_concurrent_progress_reports_never_interleave(env):
    p = make(env)
    script = ("import sys; sys.path.insert(0, sys.argv[1]); from ttp import progress\n"
              "for i in range(40):\n"
              "    progress.append(sys.argv[2], {'ts': i, 'key': sys.argv[3], 'kind': 'refusal', 'reason': 'x' * 500})\n")
    procs = [subprocess.Popen([sys.executable, "-c", script, str(RUNTIME), str(p.state), f"k{n}"]) for n in range(6)]
    assert [pr.wait(timeout=120) for pr in procs] == [0] * 6
    from ttp import progress
    raw = (p.state / "progress.jsonl").read_text().splitlines()
    assert all(json.loads(ln)["reason"] == "x" * 500 for ln in raw)   # every line whole
    got = progress.entries(p.state)
    assert len(raw) == len(got) == 240
    assert sorted({r["key"] for r in got}) == [f"k{n}" for n in range(6)]


def test_the_progress_file_rotates_and_keeps_one_old_generation(env):
    p = make(env)
    from ttp import progress
    for i in range(30):
        progress.append(p.state, {"ts": i, "key": "k", "kind": "failure", "reason": "r" * 100}, max_bytes=1000)
    log, old = p.state / progress.LOG, p.state / progress.OLD
    assert old.exists() and log.stat().st_size <= 1000 and old.stat().st_size <= 1000
    assert not list(p.state.glob("progress.2*"))
    kept = progress.entries(p.state)
    assert 0 < len(kept) < 30 and [r["ts"] for r in kept] == sorted(r["ts"] for r in kept)
    assert kept[-1]["ts"] == 29


def test_status_shows_a_running_driver_with_only_refusals_as_stalled(env):
    p = make(env)
    from ttp.cli import status_text
    from ttp.web import health
    driver = subprocess.Popen(["sleep", "60"])
    try:
        _driver_reports(p, driver.pid, ["result"] + ["refusal"] * 4)
        assert "stalled" not in status_text(p)   # 4 of 5: not yet
        _driver_reports(p, driver.pid, ["failure"], t0=time.time() - 60)
        line = [ln for ln in status_text(p).splitlines() if "driver filler stalled" in ln]
        assert len(line) == 1 and "4 refusals, 1 failure in a row, no result" in line[0], status_text(p)
        assert f"pid {driver.pid}" in line[0]
        assert health(p, p.db)["drivers_stalled"] == [line[0].strip()]
        _driver_reports(p, driver.pid, ["result"], t0=time.time() - 30)
        assert "stalled" not in status_text(p)   # a result ends the streak
        _driver_reports(p, driver.pid, ["refusal"] * 5, t0=time.time() - 20)
        assert "driver filler stalled" in status_text(p)
    finally:
        driver.kill()
        driver.wait()
    assert "stalled" not in status_text(p)   # its process ended: the daemon's observation covers it


def test_the_daemon_raises_one_keyed_observation_per_stalled_streak(env):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)

    def raised():
        return [e for e in p.db.q("SELECT * FROM events WHERE source='daemon:progress' ORDER BY id")]
    _driver_reports(p, 99999, ["refusal"] * 4, key="recheck")
    d.watch_progress(every_s=0)
    assert raised() == []
    _driver_reports(p, 99999, ["refusal"], key="recheck", t0=time.time() - 100)
    d.watch_progress(every_s=0)
    d.watch_progress(every_s=0)   # the same streak: no second observation
    ev = raised()
    assert len(ev) == 1 and "Driver recheck made no progress: 5 refusals in a row" in ev[0]["text"], ev
    assert "lock busy (exit 75) #0" in ev[0]["text"]
    _driver_reports(p, 99999, ["result"] + ["failure"] * 5, key="recheck", t0=time.time() - 50)
    d.watch_progress(every_s=0)
    ev = raised()
    assert len(ev) == 2 and "5 failures in a row" in ev[1]["text"], ev


def _observations(p):
    return [e["text"] for e in p.db.q("SELECT text FROM events WHERE source='daemon:progress' ORDER BY id")]


def test_a_refusal_s_explicit_holder_is_recorded_and_named_by_describe_and_the_stall_observation(env, monkeypatch):
    p = make(env)
    from ttp import progress
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    for _ in range(5):
        assert _report("refusal", "--key", "filler", "--resource", "bench", "--holder", "external ci-runner (live)",
                       "device wanted (75)") == 0
    last = _lines(p)[-1]
    assert (last["resource"], last["holder"]) == ("bench", "external ci-runner (live)"), last
    line = progress.describe(progress.streaks(progress.entries(p.state))["filler"])
    assert line == "5 refusals in a row, no result: device wanted (75) (bench held by external ci-runner (live))"
    # The flags may also come before the kind.
    assert _report("--holder", "task #4 (run 9)", "--resource", "bench", "refusal", "--key", "filler", "busy") == 0
    assert _lines(p)[-1]["holder"] == "task #4 (run 9)"
    d = Daemon(p.base)
    d.watch_progress(every_s=0)
    ev = _observations(p)
    assert len(ev) == 1 and "busy (bench held by task #4 (run 9))" in ev[0], ev


def test_a_refusal_naming_a_held_resource_records_its_holder_live_or_stale_by_default(env, monkeypatch):
    p = make(env)
    from ttp import locks
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    d = Daemon(p.base)
    held = _held_slot(d, "bench")
    try:
        assert _report("refusal", "--key", "filler", "--resource", "bench", "device wanted (75)") == 0
    finally:
        held.close()
    assert _lines(p)[-1]["holder"] == f"task #99 (run 7), pid {os.getpid()} (live)", _lines(p)[-1]
    gone = _exited_holder_pid()
    held = _held_slot(d, "bench", pid=gone)
    try:
        assert _report("refusal", "--key", "filler", "--resource", "bench", "device wanted (75)") == 0
    finally:
        held.close()
    assert _lines(p)[-1]["holder"] == f"task #99 (run 7), pid {gone} (stale)", _lines(p)[-1]
    # A hold taken outside any task is an external label.
    held = locks.try_take(d._slot_paths("bench"), "nightly-ci", "exclusive")
    try:
        assert _report("refusal", "--key", "filler", "--resource", "bench", "busy") == 0
    finally:
        held.close()
    assert _lines(p)[-1]["holder"] == f"external nightly-ci, pid {os.getpid()} (live)", _lines(p)[-1]
    # Free now: said so; a result or a refusal with no resource records no holder.
    assert _report("refusal", "--key", "filler", "--resource", "bench", "busy") == 0
    assert _lines(p)[-1]["holder"] == "nobody (free)"
    assert _report("refusal", "--key", "filler", "busy") == 0
    assert _report("result", "--key", "filler", "--resource", "bench", "done") == 0
    assert all("holder" not in x for x in _lines(p)[-2:]), _lines(p)[-2:]


def test_a_refusal_whose_lock_state_cannot_be_read_is_still_recorded_with_holder_unknown(env, monkeypatch):
    p = make(env)
    from ttp import lockprobe
    monkeypatch.setenv("TTP_PROJECT", str(p.base))

    def broken(*a, **k):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(lockprobe, "answer", broken)
    assert _report("refusal", "--key", "filler", "--resource", "bench", "device wanted (75)") == 0
    last = _lines(p)[-1]
    assert (last["kind"], last["resource"], last["holder"]) == ("refusal", "bench", "unknown"), last
    monkeypatch.setattr(lockprobe, "answer", lambda *a, **k: (75, "not a dict"))
    assert _report("refusal", "--key", "filler", "--resource", "bench", "busy") == 0
    assert _lines(p)[-1]["holder"] == "unknown"


def test_old_reports_without_a_holder_load_and_describe_beside_new_ones(env):
    p = make(env)
    from ttp import progress
    from ttp.daemon import Daemon
    _driver_reports(p, 99999, ["refusal"] * 5, key="recheck")   # written before holders existed
    k = progress.streaks(progress.entries(p.state))["recheck"]
    assert progress.describe(k) == "5 refusals in a row, no result: lock busy (exit 75) #4"
    # The streak's last report decides: a new one with a holder names it, an old one after it does not.
    progress.append(p.state, {**k["last"], "ts": time.time() - 5, "holder": "task #3 (run 8), pid 42 (stale)",
                              "resource": "bench", "reason": "busy"})
    k = progress.streaks(progress.entries(p.state))["recheck"]
    assert progress.describe(k).endswith("busy (bench held by task #3 (run 8), pid 42 (stale))")
    d = Daemon(p.base)
    d.watch_progress(every_s=0)
    assert "(bench held by task #3 (run 8), pid 42 (stale))" in _observations(p)[0]
    _driver_reports(p, 99999, ["refusal"], key="recheck", t0=time.time() - 1)
    assert "held by" not in progress.describe(progress.streaks(progress.entries(p.state))["recheck"])

# ---- the progress and stall check (progress.check, Daemon.check_progress) ----

def _prog_events(p, kind=None):
    rows = p.db.q("SELECT * FROM events WHERE source='daemon:progress' ORDER BY id")
    return [r for r in rows if kind is None or r["kind"] == kind]


def _prog_waiting(p, title, probe, labels=()):
    tid = p.db.add_task(title, "spec", labels=list(labels))
    p.db.update_task(tid, status="waiting", result=json.dumps({"status": "waiting", "retry_when": probe}))
    return tid


def _prog_ran(p, tid, started):
    """A task with a worker run running since `started` (work that has run: its no_progress clock)."""
    p.db.update_task(tid, status="running")
    p.db.x("INSERT INTO runs(role,task,started,status) VALUES('worker',?,?,'running')", (tid, started))
    return p.db.one("SELECT MAX(id) id FROM runs")["id"]


def _task_keys(got, tid):
    return [k for e in got for k in e["keys"] if k == f"task:#{tid}"]


def _prog_clear(p, cfg, now, current_probe=None):
    """Three healthy checks spanning half an hour: the default hysteresis lets a key clear."""
    from ttp import progress
    for dt in (0, 60, 1900):
        assert progress.check(p.db, cfg, p.state, now + dt, current_probe=current_probe) == []


def test_progress_no_progress_fires_past_threshold_only_while_work_runs(env):
    from ttp import progress
    p = make(env)
    now = time.time()
    tid = p.db.add_task("Build the thing", "spec")
    run = _prog_ran(p, tid, now - 3600)
    assert progress.check(p.db, p.config(), p.state, now) == []          # 1 h: under the 2 h threshold
    p.db.x("UPDATE runs SET started=? WHERE id=?", (now - 3 * 3600, run))
    got = progress.check(p.db, p.config(), p.state, now)
    assert [e["kind"] for e in got] == ["no_progress"]
    assert set(got[0]["keys"]) == {f"task:#{tid}", "project"}
    ev = _prog_events(p, "no_progress")[0]
    assert "run running, no result yet" in ev["text"] and f"task #{tid}" in ev["text"] and ev["status"] == "queued"
    assert progress.check(p.db, p.config(), p.state, now + 60) == []     # unchanged: no second wake
    lines, ident = progress.digest_lines(p.db, now + 60)
    assert lines[0].startswith("## Progress") and f"task:#{tid}" in ident
    # Nothing running: the clock stops, the keys clear into the history once healthy long enough.
    p.db.update_task(tid, status="cancelled")
    p.db.x("UPDATE runs SET status='cancelled', ended=? WHERE id=?", (now + 100, run))
    _prog_clear(p, p.config(), now + 120)
    assert progress.digest_lines(p.db, now + 2100) == ([], "")
    assert "2 cleared" in progress.audit_line(p.db, now - 60, now + 2100)
    # A counted result resets the clock: a running task with a recent result is not stuck.
    t2 = p.db.add_task("Another", "spec")
    _prog_ran(p, t2, now - 5 * 3600)
    progress.append(p.state, progress.record("result", "filler", "step done", env={"TTP_TASK": str(t2)}, now=now + 2000))
    assert progress.check(p.db, p.config(), p.state, now + 2200) == []
    # Thresholds are configurable; 0 turns one off.
    cfg = {"coordinator": {"progress_check": {"task_h": 0, "project_h": 0.01}}}
    got = progress.check(p.db, cfg, p.state, now + 2300)
    assert got and got[0]["keys"] == ["project"]


def test_progress_a_key_that_drops_out_for_a_check_neither_clears_nor_wakes_again(env):
    """Review #3485 finding 1: a key that drops out of a check (its run briefly not seen running) woke
    the coordinator again as new each time it came back (3 wakes in 6 checks)."""
    from ttp import progress
    p = make(env)
    cfg = {"coordinator": {"progress_check": {"project_h": 0}}}
    now = time.time()
    tid = p.db.add_task("Stuck work", "spec")
    run = _prog_ran(p, tid, now - 3 * 3600)
    key = f"no_progress:task:#{tid}"

    def busy(on):                          # on: the run is gone for this check (its task not judged)
        p.db.x("UPDATE runs SET status=? WHERE id=?", ("failed" if on else "running", run))

    wakes = 0
    for i in range(6):                     # the run drops out every other check
        busy(i % 2 == 1)
        wakes += sum(f"task:#{tid}" in e["keys"] for e in progress.check(p.db, cfg, p.state, now + 60 * i))
        assert key in progress.load(p.db)["raised"]
    assert wakes == 1
    # Healthy for 3 checks but only a few minutes: still raised, shown as not cleared yet.
    busy(True)
    for dt in (360, 420):
        progress.check(p.db, cfg, p.state, now + dt)
    assert progress.load(p.db)["raised"][key]["ok_n"] == 3
    assert "not cleared yet" in "\n".join(progress.digest_lines(p.db, now + 420)[0])
    # Healthy for 3 checks spanning 30 min: cleared, with the time it went healthy.
    progress.check(p.db, cfg, p.state, now + 300 + 1800)
    st = progress.load(p.db)
    assert key not in st["raised"] and st["history"][-1]["key"] == key and st["history"][-1]["cleared"] == now + 300
    # Stuck again after it cleared: a new raise, one wake.
    busy(False)
    got = progress.check(p.db, cfg, p.state, now + 2200)
    assert got and f"task:#{tid}" in got[0]["keys"]
    # clear_n and clear_min are settings: 1 check and 0 min clears at once.
    busy(True)
    progress.check(p.db, {"coordinator": {"progress_check": {"project_h": 0, "clear_n": 1, "clear_min": 0}}}, p.state,
                   now + 2260)
    assert key not in progress.load(p.db)["raised"]


def test_progress_a_task_released_by_its_dependency_gets_no_task_key_before_it_runs(env):
    """Review #3562: a task that waited 2 days behind a dependency was reported stuck for 48 h on the
    first check after the dependency finished. Never started, it is Daemon.check_stall's."""
    from ttp import progress
    p = make(env)
    cfg = {"coordinator": {"progress_check": {"resource_h": 0, "project_h": 0}}}
    now = time.time()
    a = p.db.add_task("Dep", "spec")
    b = p.db.add_task("Waits", "spec", depends_on=[a])
    p.db.x("UPDATE tasks SET created=? WHERE id IN (?,?)", (now - 2 * 86400, a, b))
    progress.check(p.db, cfg, p.state, now)
    p.db.update_task(a, status="done")
    keys = [k for dt in range(60, 3 * 3600 + 61, 600) for k in _task_keys(progress.check(p.db, cfg, p.state, now + dt), b)]
    assert keys == [] and "ready" not in progress.load(p.db)
    # Once it runs, its clock starts at that run's start.
    _prog_ran(p, b, now + 4 * 3600)
    assert _task_keys(progress.check(p.db, cfg, p.state, now + 5 * 3600), b) == []
    assert _task_keys(progress.check(p.db, cfg, p.state, now + 6 * 3600), b) == [f"task:#{b}"]


def test_progress_a_task_released_by_start_after_or_a_lifted_pause_gets_no_key_before_it_runs(env):
    from ttp import progress
    p = make(env)
    cfg = {"coordinator": {"progress_check": {"project_h": 0}}}
    now = time.time()
    t1 = p.db.add_task("Later", "spec", labels=[f"start_after:{now + 3600}"])
    p.db.x("UPDATE tasks SET created=?, not_before=? WHERE id=?", (now - 86400, now + 3600, t1))
    t2 = p.db.add_task("On box-a", "spec", labels=["resource:box-a"])
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 86400, t2))
    p.db.set_kv("paused_resources", {"box-a": {"reason": "maintenance", "since": now - 86400, "by": "test"}})
    assert progress.check(p.db, cfg, p.state, now) == []
    # The start_after time passes and the pause is lifted: both ready, never started, never judged here.
    p.db.x("UPDATE tasks SET not_before=? WHERE id=?", (now - 1, t1))
    p.db.set_kv("paused_resources", {})
    got = [e for dt in range(60, 5 * 3600, 600) for e in progress.check(p.db, cfg, p.state, now + dt)]
    assert got == [] and progress.load(p.db)["raised"] == {}


def test_progress_a_dispatch_flap_before_any_run_gets_no_task_key(env):
    """A task dispatch picks and queues again before its run starts (lost the race for its resource)
    has never run: no key however long it flaps."""
    from ttp import progress
    p = make(env)
    now = time.time()
    tid = p.db.add_task("Flaky", "spec")
    got = []
    for i, status in enumerate(("queued", "running", "queued", "running", "running", "queued") * 3):
        p.db.update_task(tid, status=status)
        got += progress.check(p.db, p.config(), p.state, now + 600 * i)
    assert got == [] and progress.load(p.db)["raised"] == {}


def test_progress_a_never_started_task_behind_a_full_slot_gets_no_task_key(env):
    """Review #3564: a backlog task that waited 3 h behind the only worker slot was reported 'no progress
    since 3.0 h ago; why: queued, never started' on the first check after the reaped run freed the slot,
    before dispatch could start it. Never-started work is Daemon.check_stall's, not progress.py's."""
    from ttp import progress
    p = make(env)
    cfg = {"budget": {"max_parallel_workers": 1}, "coordinator": {"progress_check": {"resource_h": 0, "project_h": 0}}}
    now = time.time()
    a = p.db.add_task("Running", "spec")
    run = _prog_ran(p, a, now)
    b = p.db.add_task("Backlog", "spec")
    keys = []
    for dt in range(0, 3 * 3600 + 1, 600):
        keys += _task_keys(progress.check(p.db, cfg, p.state, now + dt), b)
    p.db.x("UPDATE runs SET status='failed', ended=? WHERE id=?", (now + 3 * 3600, run))
    p.db.update_task(a, status="failed")
    keys += _task_keys(progress.check(p.db, cfg, p.state, now + 3 * 3600 + 1), b)
    assert keys == [] and f"no_progress:task:#{b}" not in progress.load(p.db)["raised"]
    assert f"no_progress:task:#{a}" in progress.load(p.db)["raised"]      # A ran 3 h with no result: A's key


def test_progress_a_never_started_task_gets_no_task_key_after_a_long_gap_between_checks(env):
    from ttp import progress
    p = make(env)
    cfg = {"budget": {"max_parallel_workers": 1}, "coordinator": {"progress_check": {"resource_h": 0, "project_h": 0}}}
    now = time.time()
    a = p.db.add_task("Running", "spec")
    run = _prog_ran(p, a, now)
    b = p.db.add_task("Backlog", "spec")
    assert _task_keys(progress.check(p.db, cfg, p.state, now), b) == []
    p.db.x("UPDATE runs SET status='failed', ended=? WHERE id=?", (now + 3 * 3600, run))
    p.db.update_task(a, status="failed")
    assert _task_keys(progress.check(p.db, cfg, p.state, now + 3 * 3600 + 1), b) == []    # one gap > task_h
    assert _task_keys(progress.check(p.db, cfg, p.state, now + 9 * 3600), b) == []
    assert f"no_progress:task:#{b}" not in progress.load(p.db)["raised"]


def test_progress_a_task_that_ran_with_no_result_gets_one_task_key_that_clears_with_hysteresis(env):
    from ttp import progress
    p = make(env)
    cfg = {"coordinator": {"progress_check": {"resource_h": 0, "project_h": 0}}}
    now = time.time()
    t = p.db.add_task("Long build", "spec")
    run = _prog_ran(p, t, now)
    keys = []
    for dt in range(0, 3 * 3600 + 1, 600):
        keys += _task_keys(progress.check(p.db, cfg, p.state, now + dt), t)
    assert keys == [f"task:#{t}"]                                       # past task_h from its run's start: one wake
    r = progress.load(p.db)["raised"][f"no_progress:task:#{t}"]
    assert r["since"] == now and "no result yet" in r["reason"]
    # A driver's result inside the run resets the clock: the key goes healthy, but clears only after
    # clear_n checks spanning clear_min minutes.
    progress.append(p.state, progress.record("result", "filler", "chunk done", env={"TTP_TASK": str(t)},
                                             now=now + 3 * 3600 + 100))
    t1 = now + 3 * 3600 + 200
    for dt in (0, 60, 120):
        assert progress.check(p.db, cfg, p.state, t1 + dt) == []
    assert progress.load(p.db)["raised"][f"no_progress:task:#{t}"]["ok_n"] == 3
    assert progress.check(p.db, cfg, p.state, t1 + 1800) == []
    st = progress.load(p.db)
    assert f"no_progress:task:#{t}" not in st["raised"] and st["history"][-1]["key"] == f"no_progress:task:#{t}"
    # Its run ends with no result and the task is queued again: not judged here until a run starts.
    p.db.x("UPDATE runs SET status='failed', ended=? WHERE id=?", (t1 + 1900, run))
    p.db.update_task(t, status="queued")
    assert _task_keys(progress.check(p.db, cfg, p.state, t1 + 9 * 3600), t) == []


def test_progress_the_resource_key_ignores_ready_but_unstarted_tasks(env):
    from ttp import progress
    p = make(env)
    cfg = {"coordinator": {"progress_check": {"task_h": 0, "project_h": 0}}}
    now = time.time()
    b = p.db.add_task("On box-a", "spec", labels=["resource:box-a"])
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 86400, b))
    got = []
    for dt in range(0, 3 * 3600 + 1, 600):
        got += progress.check(p.db, cfg, p.state, now + dt)
    assert "resource:box-a" not in str(got) and progress.load(p.db)["raised"] == {}
    # A run that holds box-a counts: no result on it for resource_h raises the key.
    c = p.db.add_task("Sweep on box-a", "spec", labels=["resource:box-a"])
    _prog_ran(p, c, now + 3 * 3600)
    assert "resource:box-a" not in str(progress.check(p.db, cfg, p.state, now + 4 * 3600))
    got = progress.check(p.db, cfg, p.state, now + 5 * 3600 + 60)
    assert got and got[0]["keys"] == ["resource:box-a"]


def test_progress_probe_stall_wakes_once_then_only_at_rewake_and_clears(env):
    """A probe stall's count grows with time alone, so it never escalates: one wake, then one per rewake_h."""
    from ttp import progress
    p = make(env)
    probe = "ssh box-a test -e /tmp/ready"
    tid = _prog_waiting(p, "Run the sweep", probe)
    now = time.time()
    cur = lambda t: probe if t["id"] == tid else None
    for i in range(10):
        progress.note_probe(p.db, tid, probe, 1, "no such file /tmp/ready", now - 4000 + i * 180)
    progress.note_probe(p.db, tid, probe, 1, "dup verdict", now - 4000 + 9 * 180)   # the same verdict: not counted
    got = progress.check(p.db, {}, p.state, now, current_probe=cur)
    assert [(e["kind"], e["keys"]) for e in got] == [("stall", [f"probe:#{tid}"])]
    ev = _prog_events(p, "stall")[0]
    assert "not yet 10 times" in ev["text"] and "no such file /tmp/ready" in ev["text"] and ev["task"] == tid
    assert json.loads(ev["data"])[0]["n"] == 10
    for i in range(5):                               # still failing, below double: no new wake
        progress.note_probe(p.db, tid, probe, 1, "no such file /tmp/ready", now + 60 + i)
    assert progress.check(p.db, {}, p.state, now + 120, current_probe=cur) == []
    for i in range(5):                               # 20: twice the count, still no new wake
        progress.note_probe(p.db, tid, probe, 1, "no such file /tmp/ready", now + 200 + i)
    assert progress.check(p.db, {}, p.state, now + 300, current_probe=cur) == []
    assert progress.check(p.db, {}, p.state, now + 13 * 3600, current_probe=cur)[0]["text"].endswith("(still)")
    progress.forget_probe(p.db, tid)                 # the probe passed
    _prog_clear(p, {}, now + 14 * 3600, cur)
    assert progress.digest_lines(p.db, now + 15 * 3600) == ([], "")
    hist = progress.load(p.db)["history"]
    assert hist[-1]["key"] == f"stall:probe:#{tid}" and hist[-1]["wakes"] == 2
    # A wait on the task's own job is expected to be long: never counted as a stall.
    own = _prog_waiting(p, "Wait for checks", "ttp checks --result abc")
    for i in range(30):
        progress.note_probe(p.db, own, "ttp checks --result abc", 1, "running", now - 7200 + i * 180)
    assert progress.check(p.db, {}, p.state, now + 15 * 3600, current_probe=lambda t: "ttp checks --result abc") == []


def test_progress_flags_a_wait_on_what_it_fixes(env):
    from ttp import progress
    p = make(env)
    p.db.set_kv("paused_resources", {"box-a": {"reason": "broker down", "since": time.time(), "by": "test"}})
    probe = "ttp lock --probe box-a"
    tid = _prog_waiting(p, "Fix the box-a broker", probe, labels=["resource:box-a"])
    now = time.time()
    for i in range(12):
        progress.note_probe(p.db, tid, probe, 75, "box-a is paused", now - 5000 + i * 300)
    got = progress.check(p.db, {}, p.state, now, current_probe=lambda t: probe)
    assert got and "waits on what it fixes: box-a" in got[0]["text"] and "box-a is paused" in got[0]["text"]
    assert "waits on what it fixes: box-a" in "\n".join(progress.digest_lines(p.db, now)[0])
    # Its holder waiting on the task counts the same, whatever the task's title says.
    p.db.update_task(tid, status="cancelled")
    p.db.set_kv(progress.STATE_KEY, {})
    p.db.set_kv("paused_resources", {"box-b": {"reason": "hold", "since": now, "by": "test"},
                                     "box-c": {"reason": "hold", "since": now, "by": "test"}})
    t = _prog_waiting(p, "Look into logs", "ttp lock --probe box-b")
    holder = p.db.add_task("Benchmark", "spec", labels=["resource:box-b"], depends_on=[t])
    for i in range(12):
        progress.note_probe(p.db, t, "ttp lock --probe box-b", 75, "busy", now - 5000 + i * 300)
    got = progress.check(p.db, {}, p.state, now, current_probe=lambda x: "ttp lock --probe box-b")
    assert got and f"its holder #{holder} waits on this task" in got[0]["text"]
    # A plain wait on a paused resource it does not fix is a stall, not flagged.
    for x in (t, holder):
        p.db.update_task(x, status="cancelled")
    p.db.set_kv(progress.STATE_KEY, {})
    t3 = _prog_waiting(p, "Run the model", "ttp lock --probe box-c")
    for i in range(12):
        progress.note_probe(p.db, t3, "ttp lock --probe box-c", 75, "busy", now - 5000 + i * 300)
    got = progress.check(p.db, {}, p.state, now, current_probe=lambda x: "ttp lock --probe box-c")
    assert got and "fixes" not in got[0]["text"]


def test_progress_one_reader_and_one_streak_detector_for_driver_reports(env):
    """Review #3485 finding 2: the check had its own report file, writer, offset reader and refusal
    streak detector beside `ttp progress report`'s. One refusal streak woke the coordinator twice and
    the offset reader lost lines rotated into progress.1.jsonl."""
    from ttp import progress
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()
    assert progress.check(p.db, {}, p.state, now) == []                     # no report file: fine
    # #3467's API is the only one: append(state, rec), KINDS the report kinds, settings() its own.
    assert progress.KINDS == ("result", "refusal", "stall", "failure")
    assert progress.settings({})["stall_after"] == 5 and "stall_after" not in progress.check_settings({})
    assert not hasattr(progress, "read_reports") and not hasattr(progress, "report_stalls")
    # A refusal streak: watch_progress's observation is its one wake; the check adds no stall for it.
    _driver_reports(p, os.getpid(), ["refusal"] * 8, key="lock:box-a", t0=now - 600)
    with open(p.state / progress.LOG, "a") as f:
        f.write("not json\n[1,2]\n" + json.dumps({"kind": "bogus", "key": "x"}) + "\n" + '{"kind": "refusal", "key": "lock:bo\n')
    for dt in (0, 60, 120):
        assert progress.check(p.db, {}, p.state, now + dt) == []
    d = Daemon(p.base)
    d.watch_progress(every_s=0)
    d.watch_progress(every_s=0)
    assert [e["kind"] for e in _prog_events(p)] == ["observation"]
    assert progress.load(p.db)["raised"] == {}
    # Its last refusal still names why a resource that work waits on is stuck.
    tid = p.db.add_task("Benchmark on box-a", "spec", labels=["resource:box-a"])
    _prog_ran(p, tid, now - 5 * 3600)
    got = progress.check(p.db, {"coordinator": {"progress_check": {"task_h": 0, "project_h": 0}}}, p.state, now + 180)
    assert got and got[0]["keys"] == ["resource:box-a"] and "refusal: lock busy (exit 75) #7" in got[0]["text"]
    # A result made inside a task's run resets that task's clock, also once rotated into progress.1.jsonl.
    t2 = p.db.add_task("Running work", "spec")
    _prog_ran(p, t2, now - 5 * 3600)
    rec = progress.record("result", "filler", "checkpoint saved", env={"TTP_TASK": str(t2)}, now=now + 200)
    progress.append(p.state, rec)
    progress.append(p.state, {**rec, "kind": "refusal", "ts": now + 201}, max_bytes=1)   # rotates
    assert rec["task"] == str(t2) and "checkpoint saved" in (p.state / progress.OLD).read_text()
    cfg = {"coordinator": {"progress_check": {"resource_h": 0, "project_h": 0}}}
    assert f"task:#{t2}" not in str(progress.check(p.db, cfg, p.state, now + 300))
    assert float(progress.load(p.db)["results"][f"task:{t2}"]) == rec["ts"]


def test_progress_repeated_run_failures_and_schedule_failures_stall(env):
    from ttp import progress
    p = make(env)
    now = time.time()
    tid = p.db.add_task("Flaky build", "spec")
    for i in range(3):
        p.db.x("INSERT INTO runs(role,task,started,ended,status,exit_code,note) VALUES('worker',?,?,?,'failed',2,?)",
               (tid, now - 900 + i * 200, now - 800 + i * 200, json.dumps({"error": f"link error in obj {i}"})))
    cfg = {"coordinator": {"progress_check": {"task_h": 0, "project_h": 0}}}
    got = progress.check(p.db, cfg, p.state, now)
    assert got and got[0]["keys"] == [f"runs:#{tid}"] and "3 runs in a row" in got[0]["text"]
    assert "link error in obj" in got[0]["text"]
    p.db.x("INSERT INTO runs(role,task,started,ended,status) VALUES('worker',?,?,?,'ok')", (tid, now, now + 10))
    _prog_clear(p, cfg, now + 60)
    assert not any(k.startswith("stall:runs:") for k in progress.load(p.db)["raised"])
    # A schedule failing the same way repeat_n times.
    p.db.x("INSERT INTO schedules(name,kind,every_s) VALUES('nightly','command',3600)")
    for i in range(4):
        progress.note_schedule(p.db, "nightly", f"error: exit {i + 1}", now + i)
    progress.note_schedule(p.db, "nightly", "skipped: budget low", now + 4)   # an expected skip: neutral
    assert not any(e["kind"] == "stall" for e in progress.check(p.db, cfg, p.state, now + 2005))
    progress.note_schedule(p.db, "nightly", "error: exit 9", now + 6)
    got = progress.check(p.db, cfg, p.state, now + 2007)
    assert got and got[0]["keys"] == ["schedule:nightly"] and "error: exit 9" in got[0]["text"]
    progress.note_schedule(p.db, "nightly", "ok", now + 8)                    # a real run ends it
    _prog_clear(p, cfg, now + 2100)
    assert "stall:schedule:nightly" not in progress.load(p.db)["raised"]


def test_progress_leaves_own_daily_cap_skips_to_the_schedule_budget_observation(env):
    """Review #3485 finding 3: skips on a schedule's own daily cap were re-detected from status strings
    and reported a second time beside the daemon's schedule-budget observation; and the daemon's
    schedule-ran step must still record the skip history and count failures."""
    from ttp import progress
    from ttp import schedule as sched
    from ttp.daemon import SCHEDULE_BUDGET_SOURCE, Daemon
    p = make(env)
    d = Daemon(p.base)
    now = time.time()
    p.db.x("INSERT INTO schedules(name,kind,every_s,budget_usd_day) VALUES('summary2','llm',3600,1)")
    row = p.db.one("SELECT * FROM schedules WHERE name='summary2'")
    for _ in range(7):
        d._schedule_ran(row, "skipped: daily budget used", None)
    cfg = {"coordinator": {"progress_check": {"task_h": 0, "resource_h": 0, "project_h": 0}}}
    assert progress.check(p.db, cfg, p.state, now + 1) == []
    assert "summary2" not in progress.load(p.db)["scheds"]
    obs = p.db.q("SELECT * FROM events WHERE source=?", (SCHEDULE_BUDGET_SOURCE,))
    assert len(obs) == 1 and len(sched.skips(p.db, "summary2")) == 7
    # Failures through the same step are counted.
    for i in range(5):
        d._schedule_ran(row, "error: exit 2", None)
    got = progress.check(p.db, cfg, p.state, time.time() + 2)
    assert got and got[0]["keys"] == ["schedule:summary2"]


def test_progress_mute_keeps_a_key_quiet_and_events_raise_a_stuck_turn(env):
    from ttp import coordinator, progress, screen
    p = make(env)
    now = time.time()
    tid = p.db.add_task("Held work", "spec")
    _prog_ran(p, tid, now - 3 * 3600)
    screen.mute(p.db, progress.SOURCE, "no progress since", 24, why="known")
    assert progress.check(p.db, {}, p.state, now) == []
    assert progress.digest_lines(p.db, now)[0]          # still listed in the digest
    assert coordinator.EFFORT_EVENT_TRIGGERS["no_progress"] == coordinator.EFFORT_EVENT_TRIGGERS["stall"] == "stuck"
    assert "progress_check" in __import__("ttp.project", fromlist=["x"]).EXTRA_KEYS["coordinator"]


def test_progress_daemon_tick_runs_the_check(env):
    from ttp import progress
    from ttp.daemon import Daemon
    p = make(env)
    tid = p.db.add_task("Long run", "spec")
    _prog_ran(p, tid, time.time() - 3 * 3600)
    d = Daemon(p.base)
    d.check_progress()
    assert [e["kind"] for e in _prog_events(p)] == ["no_progress"]
    d.check_progress()                                  # throttled, and unchanged anyway
    assert len(_prog_events(p)) == 1
    assert f"no_progress:task:#{tid}" in progress.load(p.db)["raised"]


# Ready tasks dispatch holds (Daemon.check_stall's stall message names dispatch's recorded hold) and
# machine changes whose until-probe passed while their task sleeps -----------------------------------

def _ready_task(p, title="Ready", labels=()):
    return p.db.add_task(title, "s", kind="work", tier="light", origin="coordinator", labels=list(labels))


def _stall_text(p, check, now):
    """Raise check_stall's stall with one free ready task beside the test's own: ready at `now`, stuck 3 h
    later with no worker run since; the observation's text."""
    _ready_task(p, "Free to start")
    check(now)
    ev = check(now + 3 * 3600)
    assert len(ev) == 1, ev
    return ev[0]["text"]


def _held_slot(d, res, pid=None):
    """A `ttp lock` slot of `res` taken right now, its label naming `pid` (default: this process)."""
    from ttp import locks
    f = locks.try_take(d._slot_paths(res), "task #99 (run 7)", "exclusive")
    if pid is not None:
        lbl = json.loads(pathlib.Path(f.name).read_text())
        f.seek(0)
        f.truncate()
        f.write(json.dumps({**lbl, "pid": pid}))
        f.flush()
    return f


def _exited_holder_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_stall_names_a_live_lock_holder_and_its_pid_from_the_recorded_hold(env, monkeypatch):
    p, d, now, check = _stall_setup(env, monkeypatch)
    tid = _ready_task(p, "Needs the bench", ["exclusive:bench"])
    held = _held_slot(d, "bench")
    try:
        text = _stall_text(p, check, now)
        assert "Ready but held by a busy slot or lock" in text, text
        assert f"#{tid} Needs the bench: bench lock held by task #99 (run 7) (pid {os.getpid()} alive)" in text, text
        ctx = {"now": now, "paused": {}, "busy": {}, "committed": None, "logged_out": {}}
        assert d._start_hold(p.db.task(tid), "claude", ctx) == "resource"
        rec = ctx["holds"][tid]
        assert rec["kind"] == "lock" and rec["resource"] == "bench", rec
        assert [(h["pid"], h["alive"]) for h in rec["holders"]] == [(os.getpid(), True)], rec
    finally:
        held.close()


def test_stall_never_names_a_dead_lock_holder_as_live(env, monkeypatch):
    p, d, now, check = _stall_setup(env, monkeypatch)
    tid = _ready_task(p, "Needs the bench", ["exclusive:bench"])
    dead = _exited_holder_pid()
    held = _held_slot(d, "bench", pid=dead)
    try:
        text = _stall_text(p, check, now)
        assert "lock held by" not in text and "alive" not in text, text
        assert f"#{tid} Needs the bench: bench lock taken, no live holder: task #99 (run 7) (pid {dead} gone)" \
            in text, text
    finally:
        held.close()


def test_stall_names_the_running_tasks_filling_a_busy_slot(env, monkeypatch):
    p, d, now, check = _stall_setup(env, monkeypatch)
    holder = _ready_task(p, "Holder", ["exclusive:rig"])
    p.db.update_task(holder, status="running")
    tid = _ready_task(p, "Needs the rig", ["exclusive:rig"])
    text = _stall_text(p, check, now)
    assert f"#{tid} Needs the rig: rig held by running task(s) #{holder}" in text, text


def test_a_task_paused_through_a_device_lock_alias_is_never_a_lock_wait(env, monkeypatch):
    """Dispatch holds a task whose device name is paused under another device.locks name; it records a
    pause, so the stall message never lists it nor names a holder (review #3580)."""
    p, d, now, check = _stall_setup(env, monkeypatch)
    d.cfg["device"] = {**(d.cfg.get("device") or {}), "locks": ["rig-device", "rig"]}
    tid = _ready_task(p, "Needs the rig", ["exclusive:rig"])
    p.db.set_kv("paused_resources", {"rig-device": {"reason": "maintenance", "since": now}})
    held = _held_slot(d, "rig")   # even with its lock taken: the pause is what holds it
    try:
        ctx = {"now": now, "paused": p.db.paused_resources(), "busy": {}, "committed": None, "logged_out": {}}
        assert d._start_hold(p.db.task(tid), "claude", ctx) == "resource"
        assert ctx["holds"][tid] == {"kind": "pause", "resource": "rig"}
        text = _stall_text(p, check, now)
        assert f"#{tid}" not in text and "lock" not in text and "Ready but held" not in text, text
    finally:
        held.close()


def test_a_task_paused_directly_records_a_pause_and_is_never_a_lock_wait(env, monkeypatch):
    p, d, now, check = _stall_setup(env, monkeypatch)
    tid = _ready_task(p, "Needs the rig", ["exclusive:rig"])
    p.db.set_kv("paused_resources", {"rig": {"reason": "maintenance", "since": now}})
    held = _held_slot(d, "rig")
    try:
        ctx = {"now": now, "paused": p.db.paused_resources(), "busy": {}, "committed": None, "logged_out": {}}
        assert d._start_hold(p.db.task(tid), "claude", ctx) == "paused"
        assert ctx["holds"][tid] == {"kind": "pause", "resource": "rig"}
        text = _stall_text(p, check, now)
        assert f"#{tid}" not in text and "lock" not in text and "Ready but held" not in text, text
    finally:
        held.close()


def test_progress_check_says_nothing_of_a_never_run_task_a_lock_holds(env, monkeypatch):
    """A queued task that never ran is Daemon.check_stall's: progress.py raises no key for it, even while
    a lock holds it for hours, and works out no hold of its own (no lock, pause or alias resolution)."""
    from ttp import progress
    p, d, now, check = _stall_setup(env, monkeypatch)
    tid = _ready_task(p, "Needs the bench", ["exclusive:bench"])
    held = _held_slot(d, "bench")
    try:
        for h in range(0, 6):
            progress.check(p.db, d.cfg, p.state, now + h * 3600, where=p, current_probe=lambda t: None, changes=[])
        assert _prog_events(p) == [], _prog_events(p)
        assert not [k for k in progress.load(p.db)["raised"] if f"#{tid}" in k]
    finally:
        held.close()
    import inspect
    src = inspect.getsource(progress)
    for name in ("hold_reason", "_resource_hold(", "_start_hold(", "canonical(", "device_locks(", "holders_now(",
                 "holder_records(", "slot_paths(", "held_labels(", "pid_alive(", "overdue:#", "multilock"):
        assert name not in src, name


def test_dispatch_logs_a_resource_wait_once(env, monkeypatch):
    p, d, now, check = _stall_setup(env, monkeypatch)
    holder = _ready_task(p, "Holder", ["exclusive:rig"])
    p.db.update_task(holder, status="running")
    tid = _ready_task(p, "Needs the rig", ["exclusive:rig"])
    log = lambda: (p.logs / "daemon.log").read_text() if (p.logs / "daemon.log").exists() else ""
    rec = d._resource_hold(p.db.task(tid))
    assert rec == {"kind": "busy", "resource": "rig", "tasks": [holder]}, rec
    for _ in range(3):
        d._log_resource_wait(p.db.task(tid), rec)
    line = f"task {tid}: ready, not started: rig held by running task(s) #{holder}"
    assert log().count(line) == 1, log()
    check(now)                                    # still held: remembered
    d._log_resource_wait(p.db.task(tid), rec)
    assert log().count(line) == 1
    p.db.update_task(holder, status="done")
    check(now + 60)                               # free: forgotten, a new wait logs again
    p.db.update_task(holder, status="running")
    d._log_resource_wait(p.db.task(tid), rec)
    assert log().count(line) == 2


def test_change_probe_fires_once_and_clears_when_the_change_closes_or_the_task_wakes(env):
    from ttp import progress
    p = make(env)
    now = time.time()
    cfg = {"coordinator": {"progress_check": {"task_h": 0, "resource_h": 0, "project_h": 0}}}
    tid = _prog_waiting(p, "Waits on its drop-in", "test -f /nonexistent/done")
    ch = {"id": 5, "alias": "box-a", "what": "a test drop-in", "undo": "rm -f /etc/drop-in", "by": p.name,
          "task": tid, "probe": "test -f /nonexistent/done"}
    assert progress.check(p.db, cfg, p.state, now, changes=[ch]) == [], "probe not passed"
    ch["probe_passed"] = now
    assert progress.check(p.db, cfg, p.state, now + 600, changes=[ch]) == [], "under change_probe_m (15 min)"
    got = progress.check(p.db, cfg, p.state, now + 16 * 60, changes=[ch])
    assert [(e["kind"], e["keys"]) for e in got] == [("change_probe", ["change-probe:5"])], got
    ev = _prog_events(p, "change_probe")[0]
    assert f"task #{tid}" in ev["text"] and "rm -f /etc/drop-in" in ev["text"], ev["text"]
    for dt in (17, 30, 90, 600):
        assert progress.check(p.db, cfg, p.state, now + dt * 60, changes=[ch]) == [], "fires once"
    assert progress.check(p.db, cfg, p.state, now + 601 * 60, changes=None) == [], "ledger unread: left as is"
    assert "change-probe:5" in progress.load(p.db)["raised"]
    # The change closes: the key clears at once.
    assert progress.check(p.db, cfg, p.state, now + 602 * 60, changes=[]) == []
    assert "change-probe:5" not in progress.load(p.db)["raised"]
    # A second change whose task wakes (runs): cleared at once too.
    ch2 = {**ch, "id": 6, "probe_passed": now + 602 * 60}
    got = progress.check(p.db, cfg, p.state, now + 620 * 60, changes=[ch2])
    assert [e["keys"] for e in got] == [["change-probe:6"]]
    p.db.update_task(tid, status="running")
    assert progress.check(p.db, cfg, p.state, now + 621 * 60, changes=[ch2]) == []
    st = progress.load(p.db)
    assert "change-probe:6" not in st["raised"] and {"change-probe:5", "change-probe:6"} <= {h["key"] for h in st["history"]}


def test_change_probe_wakes_once_across_a_flap_of_its_probe(env):
    from ttp import progress
    p = make(env)
    now = time.time()
    cfg = {"coordinator": {"progress_check": {"task_h": 0, "resource_h": 0, "project_h": 0}}}
    tid = _prog_waiting(p, "Waits", "test -f /nonexistent/done")
    ch = {"id": 7, "alias": "box-a", "what": "x", "undo": "y", "by": p.name, "task": tid, "probe_passed": now}
    assert len(progress.check(p.db, cfg, p.state, now + 16 * 60, changes=[ch])) == 1
    gone = {k: v for k, v in ch.items() if k != "probe_passed"}    # the probe said not yet once
    assert progress.check(p.db, cfg, p.state, now + 17 * 60, changes=[gone]) == []
    ch["probe_passed"] = now + 18 * 60
    for m in (18, 40, 60):
        assert progress.check(p.db, cfg, p.state, now + m * 60, changes=[ch]) == []
    assert "change-probe:7" in progress.load(p.db)["raised"]


def test_check_progress_passes_only_this_projects_machine_changes(env, monkeypatch):
    from ttp import machine_ledger, progress
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    got = {}
    mine = {"id": 1, "by": p.name}
    monkeypatch.setattr(machine_ledger, "changes", lambda: [mine, {"id": 2, "by": "another"}])
    monkeypatch.setattr(progress, "check", lambda *a, **k: got.update(k) or [])
    d.check_progress()
    assert got["changes"] == [mine]
    d._progress_checked = 0

    def broken():
        raise OSError("unreadable")
    monkeypatch.setattr(machine_ledger, "changes", broken)
    d.check_progress()
    assert got["changes"] is None


_HOLDS = ["test -e state/user-example || exit 1",                                  # (a) a user pause
          "test -e state/user-example || test -e state/other-ok || exit 1",        # (b) or another marker file
          "[ -f state/user-example ] && test -e state/user-other"]                 # user markers only
_LIVE = ["test -e state/user-example && ssh somehost true || exit 1",              # (c) and a live check
         "test -e state/user-example || ssh somehost test -e ready || exit 1",     # or a live check
         "test -e state/user-example && test -e state/other-ok || exit 1",         # and a marker the project writes
         "test -e state/out-ready || exit 1"]                                      # a plain live probe


@pytest.mark.parametrize("probe", _HOLDS + _LIVE)
def test_a_user_pause_probe_raises_no_probe_stall_and_the_sweep_and_wait_stale_agree(env, probe):
    """A start_when or retry_when that waits only on user markers (or an `||` of one with other marker
    files) is a user pause: the progress check raises no probe stall for it, however often it says not
    yet. The blocker sweep, wait_stale and the progress check classify every probe the same way, through
    userpause.is_probe; anything else stays a live probe the stall check watches."""
    from ttp import coordinator as coord, progress, sweep, userpause
    from ttp.daemon import Daemon
    p = make(env)
    d, hold = Daemon(p.base), probe in _HOLDS
    assert userpause.is_probe(probe, p.config()) == hold
    now = time.time()
    tid = p.db.add_task("starts on a marker", "s", kind="work", tier="light", origin="user",
                        labels=coord.defer_labels(None, probe))
    for i in range(21):                              # 21 times not yet in an hour and more
        d._start_verdict(p.db.task(tid), 1, now - 4000 + i * 180)
    got = progress.check(p.db, p.config(), p.state, now)
    assert [(e["kind"], e["keys"]) for e in got] == ([] if hold else [("stall", [f"probe:#{tid}"])]), got
    # The waiting check past its hold cap: a wait_stale event only for a live probe.
    wid = _due_waiting_task(p, probe, since_ago=7 * 24 * 3600)
    _settle_probe(d, wid)
    p.db.update_task(wid, not_before=time.time() - 1)
    d.probe_waiting()
    stale = p.db.q("SELECT id FROM events WHERE task=? AND kind='wait_stale'", (wid,))
    assert bool(stale) != hold, stale
    # The blocker sweep carries the deferred task as a user pause, or not; its user-pause reader agrees
    # on the waiting one.
    kinds = {i["task"]: i["kind"] for i in sweep.items(p.db, now + 30 * 3600, p.config()) if i.get("task")}
    assert (kinds.get(tid) == "user-pause", bool(userpause.probe_of(p.db.task(wid), p.config()))) == (hold, hold)


_LIVE_START = "test -e state/out-ready || exit 1"


def _deferred(p, probe, since, after=None):
    """A queued task deferred on start_when `probe` since `since`, with an optional start_after."""
    labels = ([f"start_after:{after:.0f}"] if after else []) + [f"start_when:{probe}", f"deferred_since:{since:.0f}"]
    tid = p.db.add_task("starts on a file", "s", kind="work", tier="light", origin="user", labels=labels)
    p.db.update_task(tid, not_before=after)
    return tid


def _broken_events(p, tid):
    return p.db.q("SELECT * FROM events WHERE kind='deferral_probe_broken' AND task=? AND status='queued'", (tid,))


def test_moving_start_after_into_the_future_drops_the_probe_count_and_its_stall(env):
    """A start_when that said not yet 21 times in an hour raised a probe stall; the coordinator then
    moved its start_after hours ahead. The count, the raised stall and the unhandled broken-probe
    finding are dropped at once: no '(still)' re-raise follows while the probe does not run, and a
    broken probe is reported again once it runs again."""
    from ttp import coordinator as coord, progress
    from ttp.daemon import Daemon
    p = make(env)
    d, now = Daemon(p.base), time.time()
    tid = _deferred(p, _LIVE_START, now - 5000)
    for i in range(21):
        d._start_verdict(p.db.task(tid), 1, now - 3800 + i * 180)
    d._start_verdict(p.db.task(tid), 2, now - 10)       # one broken answer, not yet handled
    assert len(_broken_events(p, tid)) == 1
    got = progress.check(p.db, p.config(), p.state, now)
    assert [(e["kind"], e["keys"]) for e in got] == [("stall", [f"probe:#{tid}"])]
    assert coord.apply(p, [{"type": "task_update", "id": tid, "start_after": "6h"}]) == []
    st = progress.load(p.db)
    assert str(tid) not in st["probes"] and f"stall:probe:#{tid}" not in st["raised"]
    assert st["history"][-1]["key"] == f"stall:probe:#{tid}" and _broken_events(p, tid) == []
    for dt in (60, 3600, 13 * 3600, 26 * 3600):         # well past rewake_h: nothing, not even '(still)'
        assert progress.check(p.db, p.config(), p.state, now + dt) == []
    assert [e for e in _prog_events(p, "stall") if "(still)" in e["text"]] == []
    # Its start_after passes and the probe runs again, broken: reported again.
    assert coord.apply(p, [{"type": "task_update", "id": tid, "start_after": "now"}]) == []
    d._start_verdict(p.db.task(tid), 2, time.time())
    assert len(_broken_events(p, tid)) == 1


def test_no_probe_stall_while_start_after_is_ahead(env):
    """However many not-yet answers a task's record holds, a task whose start_after is still ahead (set
    by any path) raises no probe stall, and a stall raised before clears at once."""
    from ttp import progress
    p = make(env)
    now = time.time()
    tid = _deferred(p, _LIVE_START, now - 5000)
    cur = lambda t: _LIVE_START
    for i in range(21):
        progress.note_probe(p.db, tid, _LIVE_START, 1, "not yet", now - 3800 + i * 180)
    assert progress.check(p.db, {}, p.state, now, current_probe=cur)
    p.db.update_task(tid, labels=[f"start_after:{now + 6 * 3600:.0f}", f"start_when:{_LIVE_START}",
                                  f"deferred_since:{now:.0f}"], not_before=now + 6 * 3600)
    progress.note_probe(p.db, tid, _LIVE_START, 1, "not yet", now + 30)   # a stray late answer
    assert progress.check(p.db, {}, p.state, now + 60, current_probe=cur) == []
    st = progress.load(p.db)
    assert st["probes"] == {} and st["raised"] == {}
    assert progress.check(p.db, {}, p.state, now + 13 * 3600 - 60, current_probe=cur) == []   # quiet, still ahead


def test_after_start_after_passes_only_new_probe_answers_count(env):
    """Answers from before start_after last changed never count toward a stall; once it has passed,
    a probe that keeps saying not yet still raises the stall after probe_n answers and probe_h hours."""
    from ttp import progress
    p = make(env)
    now = time.time()
    tid = _deferred(p, _LIVE_START, now - 5000)
    cur = lambda t: _LIVE_START
    for i in range(21):                                   # 21 old answers
        progress.note_probe(p.db, tid, _LIVE_START, 1, "not yet", now - 3800 + i * 180)
    start = now + 3600
    p.db.update_task(tid, labels=[f"start_after:{start:.0f}", f"start_when:{_LIVE_START}",
                                  f"deferred_since:{now:.0f}"], not_before=start)
    start = float(f"{start:.0f}")
    for i in range(10):                                   # 10 new answers in under an hour: no stall yet
        progress.note_probe(p.db, tid, _LIVE_START, 1, "not yet", start + 60 + i * 180)
    assert progress.check(p.db, {}, p.state, start + 60 + 9 * 180 + 30, current_probe=cur) == []
    assert progress.load(p.db)["probes"][str(tid)]["n"] == 10
    for i in range(10, 22):                               # still not yet past probe_h: the stall is raised
        progress.note_probe(p.db, tid, _LIVE_START, 1, "not yet", start + 60 + i * 180)
    got = progress.check(p.db, {}, p.state, start + 60 + 21 * 180 + 30, current_probe=cur)
    assert [(e["kind"], e["keys"]) for e in got] == [("stall", [f"probe:#{tid}"])]
    assert json.loads(_prog_events(p, "stall")[-1]["data"])[0]["n"] == 22   # the new answers only


def test_a_start_when_probe_that_stopped_answering_is_not_judged(env):
    """A start_when probe with no answer for two probe intervals and its timeout is not running: its count is dropped
    (its next answer starts afresh). A waiting task's retry_when, probed at its own retry, keeps its count."""
    from ttp import daemon, progress
    from ttp import startgate
    assert progress.QUIET_S == 2 * startgate.MAX_EVERY_S + daemon.PROBE_TIMEOUT_S
    p = make(env)
    now = time.time()
    tid = _deferred(p, _LIVE_START, now - 9000)
    for i in range(21):
        progress.note_probe(p.db, tid, _LIVE_START, 1, "not yet", now - 8000 + i * 180)
    assert progress.check(p.db, {}, p.state, now - 8000 + 20 * 180 + 30, current_probe=lambda t: _LIVE_START)
    assert progress.check(p.db, {}, p.state, now, current_probe=lambda t: _LIVE_START) == []
    assert progress.load(p.db)["probes"] == {} and progress.load(p.db)["raised"] == {}


def _wrapper_probe(p, frm="peer-a"):
    """A retry_when on the project's own ack-wait wrapper (ackwait.is_probe), which says not yet."""
    wrapper = p.harness / "bin" / "ack_wait.py"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("import sys\nsys.exit(1)\n")
    return f"python3 {wrapper} probe --from {frm} --hash 0123abcd"


def test_a_reply_wait_on_the_projects_own_ack_wait_wrapper_raises_no_probe_stall(env):
    """A wait on another project's reply through the project's own ack-wait wrapper is a long wait, as a
    `ttp ack-wait probe` is: no probe stall past probe_n and probe_h, nor at rewake_h. A wrapper outside
    the harness dir (is_probe rejects it) is still judged as before."""
    from ttp import ackwait, progress
    p = make(env)
    own, other = _wrapper_probe(p), "python3 other/harness/bin/ack_wait.py probe --from peer-a --hash 0123abcd"
    assert ackwait.is_probe(own, p.config(), p.harness) and not ackwait.is_probe(other, p.config(), p.harness)
    now, probes = time.time(), {}
    for probe in (own, "ttp ack-wait probe --from peer-a --hash 0123abcd", other):
        tid = _due_waiting_task(p, probe, since_ago=5000)
        probes[tid] = probe
        for i in range(21):
            progress.note_probe(p.db, tid, probe, 1, "no reply yet", now - 4000 + i * 180)
    cur = lambda t: probes.get(t["id"])
    flagged = next(k for k, v in probes.items() if v == other)
    got = progress.check(p.db, p.config(), p.state, now, where=p, current_probe=cur)
    assert [k for e in got for k in e["keys"]] == [f"probe:#{flagged}"], got
    for dt in (13 * 3600, 26 * 3600):            # past rewake_h: only the rejected probe wakes again
        got = progress.check(p.db, p.config(), p.state, now + dt, where=p, current_probe=cur)
        assert {k for e in got for k in e["keys"]} <= {f"probe:#{flagged}"}, got
    assert set(progress.load(p.db)["raised"]) == {f"stall:probe:#{flagged}"}


def test_a_wait_on_the_users_step_raises_no_probe_stall_and_self_or_external_waits_still_do(env):
    """A wait only the user's step ends (wait_kind `user` or `user-action`, or an obligation owner whose
    waiting_for or blocked_reason names the user) raises no probe stall: no worker can speed it up. The
    probe still counts. A self or external wait, or a user-naming wait with no open obligation, is
    judged as before."""
    from ttp import obligations, progress, unblock
    p = make(env)
    now, probes = time.time(), {}
    probe = "test -e out/signed-in.flag"
    cases = {"user": {"wait_kind": "user"}, "user-action": {"wait_kind": "User-Action"},
             "owed": {"waiting_for": "the user to sign in on the box"},
             "owed-blocked": {"waiting_for": "a board"},
             "external": {"wait_kind": "external"},
             "unowed": {"waiting_for": "the user to sign in on the box"},
             "owed-not-user": {"waiting_for": "a board"}}
    ids = {}
    for name, extra in cases.items():
        tid = ids[name] = _due_waiting_task(p, probe, since_ago=5000, **extra)
        probes[tid] = probe
        for i in range(21):
            progress.note_probe(p.db, tid, probe, 1, "not yet", now - 4000 + i * 180)
    p.db.update_task(ids["owed-blocked"], status="blocked", blocked_reason="waits for the user's sign-in")
    for name in ("owed", "owed-blocked", "owed-not-user"):
        obligations.add(p, f"deliverable of #{ids[name]}", owners=[ids[name]])
    cur = lambda t: probes.get(t["id"])
    got = progress.check(p.db, p.config(), p.state, now, where=p, current_probe=cur)
    raised = {k for e in got for k in e["keys"]}
    assert raised == {f"probe:#{ids[n]}" for n in ("external", "unowed", "owed-not-user")}, (raised, ids)
    assert int(progress.load(p.db)["probes"][str(ids["user"])]["n"]) == 21   # the probe still counts
    # unblock: a user wait is its own kind, never a self-wait a worker could end.
    assert unblock.classify_wait({"wait_kind": "user"}) == ("user", "hand-off")
    assert unblock.classify_wait({"wait_kind": "user-action", "retry_when": "ttp detach --check x"})[0] == "user"
    assert unblock.classify_wait({"wait_kind": "bogus"})[0] == "external"
    assert unblock.wait_data(json.dumps({"wait": "user", "why": "hand-off"})) \
        and not unblock.is_self_wait(json.dumps({"wait": "user"}))


def test_a_reply_wait_stays_quiet_while_its_peer_recovers_and_re_arms_once_it_is_ready(env, monkeypatch):
    """While the project a reply wait waits on cannot act (read-only from its own state through
    machine_ledger.can_act), the wait raises no wait_stale and its note says the peer is recovering. Once
    the peer can act again, the same wait raises wait_stale and the note drops the state. An unknown peer,
    or one whose state cannot be read, never silences the checks."""
    from ttp import ackwait, machine_ledger
    from ttp.daemon import Daemon
    p = make(env)
    state = {"peer-a": (False, "its daemon is stopped")}
    monkeypatch.setattr(machine_ledger, "can_act", lambda name, here=None: state.get(name, (False, "not a project of this user")))
    probe = _wrapper_probe(p)
    assert ackwait.peer_recovering(probe, p.config(), p.harness) == ("peer-a", "its daemon is stopped")
    for rw in (_wrapper_probe(p, "peer-b"), "ttp lock --probe box-a", "python3 other/harness/bin/ack_wait.py probe --from peer-a"):
        assert ackwait.peer_recovering(rw, p.config(), p.harness) == ("", ""), rw
    state["peer-c"] = (False, "its state cannot be read")
    assert ackwait.peer_recovering(_wrapper_probe(p, "peer-c"), p.config(), p.harness) == ("", "")
    assert ackwait.peer_recovering("ttp ack-wait probe --from=peer-a --hash 0123abcd")[0] == "peer-a"
    d = Daemon(p.base)
    tid = _due_waiting_task(p, probe, since_ago=7 * 24 * 3600)

    def verdict():
        _settle_probe(d, tid)
        p.db.update_task(tid, not_before=time.time() - 1)
        d.probe_waiting()
        return p.db.q("SELECT id FROM events WHERE task=? AND kind='wait_stale'", (tid,)), p.db.task(tid)["blocked_reason"]

    stale, why = verdict()
    assert stale == [] and "(peer peer-a recovering: its daemon is stopped)" in why, why
    state["peer-a"] = (True, "")
    stale, why = verdict()
    assert len(stale) == 1 and "recovering" not in why, why


# ---- probe stalls the blocker sweep settles model-free (sweep.dispose_probe_stalls) ----

def _stalled(p, d, probe, deps=(), now=None, ago=7200):
    """A deferred task whose start_when said not yet 21 times, with its probe stall raised `ago` s back."""
    from ttp import progress
    now = time.time() if now is None else now
    tid = _deferred(p, probe, now - ago - 4000)
    if deps:
        p.db.update_task(tid, depends_on=json.dumps(list(deps)))
    for i in range(21):
        d._start_verdict(p.db.task(tid), 1, now - ago - 3800 + i * 180)
    progress.check(p.db, p.config(), p.state, now - ago)
    assert f"stall:probe:#{tid}" in progress.load(p.db)["raised"]
    return tid


def _done_task(p):
    tid = p.db.add_task("finished work", "s")
    p.db.update_task(tid, status="done")
    return tid


def _dispose(p, d, now, rcs=None, cfg=None):
    from ttp import sweep
    from ttp.daemon import _current_probe
    ran = []

    def run(probe):
        ran.append(probe)
        return (rcs or {}).get(probe, 1)
    got = sweep.dispose_probe_stalls(p.db, cfg or p.config(), now, _current_probe, run, d._probe_stall_act)
    return {r["task"]: r["disposition"] for r in got}, ran


def test_a_probe_stall_on_a_done_dependency_releases_the_task_once_with_a_note(env):
    """A deferred task whose start_when has said not yet for hours while the task it depends on is done:
    the blocker sweep re-runs the probe model-free; still not yet, so the task is released to start
    once at its own tier with a note to check its probe. The same probe text never releases it again."""
    from ttp import progress, sweep
    from ttp.daemon import Daemon
    from ttp.db import deferral
    p = make(env)
    d, now = Daemon(p.base), time.time()
    dep = _done_task(p)
    tid = _stalled(p, d, _LIVE_START, deps=[dep], now=now)
    d.blocker_sweep()                     # runs the real probe in the project root: still not yet
    t = p.db.task(tid)
    assert not deferral(t).get("when") and t["not_before"] is None and t["tier"] == "light"
    assert "your probe has said not yet for" in t["spec"] and "check it still tests what you wait for" in t["spec"]
    rec = p.db.kv(sweep.PROBE_STALL_KEY)[str(tid)]
    assert rec["disposition"] == "released-stale"
    assert str(tid) not in progress.load(p.db)["probes"]
    assert any(f"probe stall #{tid}" in line and "released-stale" in line for line in sweep.probe_stall_lines(p.db, now))
    # Deferred again on the same probe and stalled again: never released a second time.
    p.db.update_task(tid, labels=[f"start_when:{_LIVE_START}", f"deferred_since:{now:.0f}"])
    for i in range(21):
        d._start_verdict(p.db.task(tid), 1, now - 7000 + i * 180)
    progress.check(p.db, p.config(), p.state, now - 3700)
    got, ran = _dispose(p, d, now + 60)
    assert got == {} and ran == [] and deferral(p.db.task(tid)).get("when") == _LIVE_START
    assert p.db.task(tid)["spec"].count("your probe has said not yet") == 1


def test_a_probe_stall_whose_probe_passes_on_the_recheck_lifts_its_hold(env):
    from ttp.daemon import Daemon
    from ttp.db import deferral
    p = make(env)
    d, now = Daemon(p.base), time.time()
    tid = _stalled(p, d, _LIVE_START, deps=[_done_task(p)], now=now)
    got, ran = _dispose(p, d, now, rcs={_LIVE_START: 0})
    t = p.db.task(tid)
    assert got == {tid: "rechecked-passed"} and ran == [_LIVE_START]
    assert not deferral(t).get("when") and "your probe has said" not in t["spec"]


def test_a_probe_stall_on_a_literal_sha_is_flagged_and_every_old_stall_gets_a_disposition(env):
    """A probe that greps a literal commit sha is flagged without running anything; a stall naming no
    finished task is listed; none is judged before coordinator.probe_stall_dispose_h, and 0 turns the
    pass off."""
    from ttp import sweep
    from ttp.daemon import Daemon
    from ttp.db import deferral
    p = make(env)
    d, now = Daemon(p.base), time.time()
    sha_probe = "grep -q 3f9a1c27b04e state/deployed.txt || exit 1"
    flagged = _stalled(p, d, sha_probe, now=now)
    plain = _stalled(p, d, "test -e state/other-ready || exit 1", deps=[p.db.add_task("still going", "s")], now=now)
    for h in (0, 3):     # off, and a threshold past the stalls' 2 h
        cfg = {**p.config(), "coordinator": {**p.config()["coordinator"], "probe_stall_dispose_h": h}}
        assert _dispose(p, d, now, cfg=cfg) == ({}, [])
    got, ran = _dispose(p, d, now)
    assert got == {flagged: "literal-sha-flagged", plain: "listed"} and ran == []
    why = p.db.kv(sweep.PROBE_STALL_KEY)[str(flagged)]["why"]
    assert "probe greps a literal sha; likely stale after a rebase or redeploy" in why
    assert "landed:#<id>" in why and "ttp landed <sha>" in why
    assert deferral(p.db.task(flagged)).get("when") == sha_probe
    lines = sweep.probe_stall_lines(p.db, now)
    assert any(f"#{flagged}" in x and "literal-sha-flagged" in x for x in lines)


def test_user_gated_probe_stalls_are_left_without_a_recheck_release_or_wake(env):
    """A probe that tests a user marker or a saved secret, or a task an open ask holds, is never
    re-run, released or woken, even when the task it depends on is done."""
    from ttp.daemon import Daemon
    from ttp.db import deferral
    p = make(env)
    d, now = Daemon(p.base), time.time()
    dep = _done_task(p)
    marker = _stalled(p, d, "test -e state/user-go && test -e state/out-ready || exit 1", deps=[dep], now=now)
    secret = _stalled(p, d, "ttp secret get api-token >/dev/null || exit 1", deps=[dep], now=now)
    asked = _stalled(p, d, _LIVE_START, deps=[dep], now=now)
    p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,'out','ask',?,0)",
           (now, f"May #{asked} go ahead?"))
    got, ran = _dispose(p, d, now, rcs={_LIVE_START: 0})
    assert got == {marker: "user-gated-left", secret: "user-gated-left", asked: "user-gated-left"} and ran == []
    for tid in (marker, secret, asked):
        t = p.db.task(tid)
        assert deferral(t).get("when") and "your probe has said" not in t["spec"]
        assert "woke" not in json.loads(t["result"] or "{}")


def _repointed(p, d, probe, deps=(), now=None):
    """A stalled deferred task whose start_when was re-pointed to `probe` since its stall was raised:
    progress.py raises none on a `ttp landed` wait itself (OWN_WAIT_RE), so the sweep meets one so."""
    from ttp.db import deferral
    tid = _stalled(p, d, _LIVE_START, deps=deps, now=now)
    since = deferral(p.db.task(tid)).get("since") or now
    p.db.update_task(tid, labels=[f"start_when:{probe}", f"deferred_since:{since:.0f}"])
    return tid


def test_a_probe_stall_with_a_task_ref_that_is_no_id_gets_a_disposition_and_the_pass_goes_on(env):
    """A `ttp landed --task` ref as written that is no task id ('$T', 'abc', '0') names a task that is
    never done: no re-run, no release, and the stalls sorted after it are still judged."""
    from ttp.daemon import Daemon
    from ttp.db import deferral
    p = make(env)
    d, now = Daemon(p.base), time.time()
    dep = _done_task(p)
    bad = [_repointed(p, d, f"ttp landed --task {ref} && test -e state/out-ready || exit 1", deps=[dep], now=now)
           for ref in ('"$T"', "abc", "0")]
    flagged = _stalled(p, d, "grep -q 3f9a1c27b04e state/deployed.txt || exit 1", now=now)
    got, ran = _dispose(p, d, now, rcs={_LIVE_START: 0})
    assert got == {**{tid: "listed" for tid in bad}, flagged: "literal-sha-flagged"} and ran == []
    assert all(deferral(p.db.task(tid)).get("when") for tid in bad)


def test_a_probe_stall_on_a_landed_task_ref_rechecks_the_landing_before_releasing(env):
    """Criterion 1's landed path: the probe waits on `ttp landed --task N` for a done task N. While
    `landed:#N` says not yet the probe is not re-run and the task stays held; once N landed and the
    probe still says not yet, the task is released once with the note."""
    from ttp import sweep
    from ttp.daemon import Daemon
    from ttp.db import deferral
    p = make(env)
    d, now = Daemon(p.base), time.time()
    dep = _done_task(p)
    probe = f"ttp landed --task {dep} && test -e state/out-ready || exit 1"
    tid = _repointed(p, d, probe, now=now)
    got, ran = _dispose(p, d, now, rcs={f"landed:#{dep}": 1})
    assert got == {tid: "listed"} and ran == [f"landed:#{dep}"]
    assert deferral(p.db.task(tid)).get("when") == probe and "your probe has said" not in p.db.task(tid)["spec"]
    later = now + sweep.PROBE_STALL_RECHECK_S + 1
    got, ran = _dispose(p, d, later, rcs={f"landed:#{dep}": 0})
    t = p.db.task(tid)
    assert got == {tid: "released-stale"} and ran == [f"landed:#{dep}", probe]
    assert not deferral(t).get("when") and t["spec"].count("your probe has said not yet") == 1
    assert f"#{dep} done" in p.db.kv(sweep.PROBE_STALL_KEY)[str(tid)]["why"]
    assert _dispose(p, d, later + sweep.PROBE_STALL_RECHECK_S + 1, rcs={f"landed:#{dep}": 0}) == ({}, [])


def _stalled_waiting(p, d, tmp_path, probe, deps, now, ago=7200):
    """A task that handed off waiting on `probe` through the daemon's hand-off path (status queued,
    last result waiting), with its probe stall raised `ago` s back."""
    from ttp import progress
    from ttp.daemon import _current_probe
    from ttp.providers.base import RunUsage
    tid = p.db.add_task("waits on a file", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, depends_on=json.dumps(list(deps)), status="running")
    run_dir = tmp_path / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "waiting", "summary": "waits on its output",
                                                     "retry_after_s": 1800, "retry_when": probe}))
    d._finish_worker({"task": tid, "note": "{}"}, RunUsage(cost_usd=0.1), "ok", run_dir)
    t = p.db.task(tid)
    assert t["status"] == "queued" and json.loads(t["result"])["status"] == "waiting" and t["not_before"] > now
    for i in range(21):
        progress.note_probe(p.db, tid, probe, 1, "no such file", now - ago - 3800 + i * 180)
    progress.check(p.db, p.config(), p.state, now - ago, current_probe=_current_probe)
    assert f"stall:probe:#{tid}" in progress.load(p.db)["raised"]
    return tid


def test_a_waiting_tasks_probe_stall_lifts_through_probe_passed_or_wakes_once_with_the_note(env, tmp_path):
    """A waiting hand-off whose retry_when stalled while the task it depends on is done: a pass on the
    re-run wakes it as a passed probe does (_probe_passed); a not yet wakes it once (_wake_waiting)
    with the spec note."""
    from ttp.daemon import Daemon
    p = make(env)
    d, now = Daemon(p.base), time.time()
    dep = _done_task(p)
    passes = _stalled_waiting(p, d, tmp_path, "test -e state/a-ready || exit 1", [dep], now)
    stale = _stalled_waiting(p, d, tmp_path, "test -e state/b-ready || exit 1", [dep], now)
    got, ran = _dispose(p, d, now, rcs={"test -e state/a-ready || exit 1": 0})
    assert got == {passes: "rechecked-passed", stale: "released-stale"}
    a, b = p.db.task(passes), p.db.task(stale)
    assert json.loads(a["result"])["woke"].startswith("probe passed on a probe-stall re-check")
    assert a["not_before"] <= time.time() and "your probe has said" not in a["spec"]
    assert json.loads(b["result"])["woke"] == "probe stale: released once by the blocker sweep"
    assert b["not_before"] <= time.time() and b["spec"].count("your probe has said not yet") == 1
    assert _dispose(p, d, now + 60) == ({}, [])


def test_stall_check_stays_quiet_for_a_task_held_by_a_live_task_s_branch_or_its_waits_end_label(env, monkeypatch):
    """Dispatch holds a task while another live task uses its branch (Daemon._branch_clash, inside
    _start_hold): the stall and slot-wait checks see the same hold, so no false 'Stall' (review #3783)."""
    from ttp import budget as bud
    p, d, now, check = _stall_setup(env, monkeypatch)
    holder = _ready_task(p, "Holder", ["pr_branch:feat/x"])
    p.db.update_task(holder, status="running", kind="code")
    held = p.db.add_task("Same branch", "s", kind="code", tier="light", origin="coordinator",
                         labels=["pr_branch:feat/x"])
    assert check(now) == [] and check(now + 2 * 3600) == [] and check(now + 4 * 3600) == [], \
        "a task held behind a running task's branch is no stall"
    # A waiting holder the new task was chained after (waits_end label), and no live claim at all.
    p.db.update_task(holder, status="waiting")
    p.db.update_task(held, labels=json.dumps(["pr_branch:feat/x", f"waits_end:{holder}"]))
    assert check(now + 5 * 3600) == [] and check(now + 7 * 3600) == [] and check(now + 9 * 3600) == []
    # Every slot taken: still the branch hold, never 'waits only for a slot' (check_slot_wait).
    d.gates = {"claude": bud.Gate(provider="claude", regime="windows", max_parallel=0)}
    ctx = {"now": now, "paused": {}, "busy": {}, "committed": None, "logged_out": {}}
    assert d._start_hold(p.db.task(held), "claude", ctx) == "held" and held not in ctx["full"]
    assert ctx["branch"][held][0] == holder, ctx["branch"]
    # The holder ends: it is startable again, so a long wait with nothing starting is a stall.
    d.gates = {}
    p.db.update_task(holder, status="done")
    assert check(now + 10 * 3600) == []
    assert len(check(now + 14 * 3600)) == 1, "the hold outlived its holder"
