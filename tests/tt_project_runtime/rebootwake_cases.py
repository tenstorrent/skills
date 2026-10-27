# Boot-time wake tests of tests/test_tt_project_runtime.py (Daemon.wake_after_reboot on detached-job
# waits), which runs this file in its own namespace (see _include_cases there): helpers, fixtures and
# imports come from that file, and pytest collects these tests under its name.
from __future__ import annotations


def _live_job(p, tid, name="soak"):
    """Task tid waits from before this boot on a plain `ttp detach` job of its own that runs now, on
    this boot (as one relaunched earlier in the boot does when a daemon restart finds it). Returns
    (rc, pid)."""
    from ttp import detach
    run_dir = p.runs / f"live-{tid}"
    run_dir.mkdir(parents=True, exist_ok=True)
    r = _ttp_run(p, "detach", name, "--", "sleep", "60", env={"TTP_RUN_DIR": str(run_dir)})
    assert r.returncode == 0, r.stderr
    rc = (run_dir / f"{name}.rc").resolve()
    deadline = time.time() + 10
    while not (pid := detach.running_pid(rc, lambda b: bool(b))) and time.time() < deadline:
        time.sleep(0.05)
    assert pid, "the job did not start"
    p.db.x("INSERT INTO runs(task,role,provider,started,ended,status,dir,boot_id) VALUES(?,?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 1800, time.time() - 900, "ok", str(run_dir), "an-earlier-boot"))
    return rc, pid


def _wait_on(p, tid, probe, **extra):
    p.db.update_task(tid, status="queued", not_before=time.time() + 3600, result=json.dumps(
        {"status": "waiting", "summary": "job running", "retry_after_s": 3600, "retry_when": probe,
         "waiting_since": time.time() - 900, **extra}))


def test_a_reboot_wake_keeps_a_detached_chain_that_ended_or_still_runs_asleep(env, tmp_path):
    from ttp import detach
    from ttp.daemon import Daemon
    p = make(env)
    live = p.db.add_task("live", "s", kind="work", tier="standard", origin="user")
    rc, pid = _live_job(p, live)
    rows = rc.parent / "rows.txt"
    _wait_on(p, live, f"ttp detach --check {rc} && test -s {rows}")
    ended = p.db.add_task("ended", "s", kind="work", tier="standard", origin="user")
    erc, _ = _resumable_job_cut_by_reboot(p, tmp_path, ended, resumable=False)
    erc.write_text("0\n")
    _wait_on(p, ended, f"ttp detach --check {erc}; test -e {erc.parent}/rows")
    # Something else the reboot ended still wakes it: a process of the earlier boot.
    proc = p.db.add_task("proc", "s", kind="work", tier="standard", origin="user")
    prc, _ = _resumable_job_cut_by_reboot(p, tmp_path, proc, resumable=False)
    prc.write_text("0\n")
    _wait_on(p, proc, f"ttp detach --check {prc} && kill -0 4242")
    try:
        d = Daemon(p.base)   # this boot: a daemon restart within it, as after an upgrade
        d.boot_at = time.time() - 300
        d.wake_after_reboot()
        for tid in (live, ended):
            t = p.db.task(tid)
            assert t["not_before"] and "woke" not in json.loads(t["result"]), (t["title"], dict(t))
        assert _woken(p, proc), "a probe that also checks a process of the earlier boot slept on"
        assert json.loads(p.db.task(proc)["result"])["woke"] == "the host rebooted"
        assert detach.running_pid(rc, d._this_boot) == pid, "the live job was stopped"
    finally:
        detach.stop(rc, pid, "test over")


def test_a_reboot_wake_for_a_lost_detached_chain_runs_at_light(env, tmp_path):
    from ttp import budget as bud, detach
    p = make(env)
    plain = p.db.add_task("plain", "s", kind="work", tier="standard", origin="user")
    rc, _ = _resumable_job_cut_by_reboot(p, tmp_path, plain, resumable=False)
    _wait_on(p, plain, f"ttp detach --check {rc} && test -s {rc.parent}/rows", wake_tier="standard",
             next_step="read the rows, compare them with the baseline and decide whether to rerun")
    capped = p.db.add_task("capped", "s", kind="work", tier="standard", origin="user")
    crc, counter = _resumable_job_cut_by_reboot(p, tmp_path, capped)
    m = json.loads(detach.resume_file(crc).read_text())
    detach.resume_file(crc).write_text(json.dumps({**m, "relaunches": detach.RELAUNCH_CAP}))
    _wait_on(p, capped, f"ttp detach --check {crc}", wake_tier="standard", escalated_wake=True)
    # A wait on no detached job keeps the hand-off's tier and the plain reason.
    tmpw = p.db.add_task("tmp", "s", kind="work", tier="standard", origin="user")
    _wait_on(p, tmpw, "test -e /tmp/soak.done", wake_tier="standard")
    d = _boot_daemon(p)
    for tid, job in ((plain, "job"), (capped, "job")):
        t = p.db.task(tid)
        res = json.loads(t["result"])
        assert _woken(p, tid), dict(t)
        assert res["woke"].startswith(f"the host rebooted and cut short detached job {job} "), res["woke"]
        assert "relaunch each with the same command" in res["woke"] and "same retry_when" in res["woke"]
        assert res["wake_tier"] == "light" and "escalated_wake" not in res, res
        assert bud.wake_tier(t["tier"], res, d.cfg) == "light"
    assert counter.read_text().split() == ["run"], "a capped job was relaunched"
    res = json.loads(p.db.task(tmpw)["result"])
    assert res["woke"] == "the host rebooted" and res["wake_tier"] == "standard", res
    assert bud.wake_tier("standard", res, d.cfg) == "standard"


def test_a_compound_detach_probe_gets_the_model_free_relaunch(env, tmp_path):
    from ttp import rebootwait as rw
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, counter = _resumable_job_cut_by_reboot(p, tmp_path, tid)
    _wait_on(p, tid, f"ttp detach --check {rc} --rows {rc.parent}/rows --after 2 || test -s {rc.parent}/rows")
    _boot_daemon(p)
    assert not _woken(p, tid) and p.db.task(tid)["not_before"], "the task was woken for a model run"
    assert _wait_for_path(rc) and counter.read_text().split() == ["run", "run"]
    assert len(_job_relaunches(p, tid)) == 1
    # A daemon restart on the same boot, the job ended: still asleep, nothing started again.
    _boot_daemon(p)
    assert not _woken(p, tid) and len(_job_relaunches(p, tid)) == 1
    # The parser: local checks only, wherever they stand in the probe; in doubt, none.
    a, b = "/r/a.rc", "/r/b.rc"
    for probe, want in (
            (f"ttp detach --check {a}", [a]),
            (f"ttp detach --check {a} && test -s /r/rows", [a]),
            (f"test -e /r/x || /usr/local/bin/ttp detach --check {a} {b}; true", [a, b]),
            (f"X=1 ttp detach --check {a} --rows=/r/rows --mirror /r && ttp detach --check {a}", [a]),
            (f"ttp detach --check {a} && ttp detach --check --host box d/b.rc", [a]),
            (f"ttp detach --check {a} && ttp detach --check --host=box d/b.rc", [a]),
            (f"ssh box ttp detach --check {a}", []),
            (f"timeout 20 ssh box 'ttp detach --check {a}'", []),
            ("ttp detach --check a.rc", []),
            (f"ttp detach --check {a} --unknown", []),
            (f"ttp detach --check {a} /r/b.log", []),
            (f"echo ttp detach --check {a}", []),
            ("ttp detach --check 'unterminated", []),
            ("", [])):
        assert rw.local_jobs(probe) == [pathlib.Path(x) for x in want], probe
    assert rw.transient_besides_jobs(f"ttp detach --check {a} && test -s /x/state/runs/r1/rows") == ""
    assert rw.transient_besides_jobs(f"ttp detach --check {a} && pgrep soak") == "a process"
    assert rw.transient_besides_jobs(f"ttp detach --check {a}; test -e /tmp/x") == "a path a reboot clears"


def test_a_same_boot_restart_with_no_detached_waits_changes_nothing(env, tmp_path):
    from ttp.daemon import Daemon
    p = make(env)
    timer = p.db.add_task("timer", "s", kind="work", tier="light", origin="user")
    _wait_on(p, timer, 'test "$(date +%s)" -ge 1900000000')
    after = p.db.add_task("after", "s", kind="work", tier="light", origin="user")
    _wait_on(p, after, "ttp detach --check /nowhere/job.rc")
    p.db.update_task(after, result=json.dumps({**json.loads(p.db.task(after)["result"]),
                                               "waiting_since": time.time()}))
    before = {tid: dict(p.db.task(tid)) for tid in (timer, after)}
    d = Daemon(p.base)
    d.boot_at = time.time() - 300
    d.wake_after_reboot()
    assert {tid: dict(p.db.task(tid)) for tid in before} == before
    assert not p.db.q("SELECT id FROM events WHERE kind IN ('job_relaunched','task_blocked')")
