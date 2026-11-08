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
    r = _ttp_run(p, "detach", name, "--", "sleep", "60", env={"TTP_RUN_DIR": str(run_dir)}, timeout=HANG_S)
    assert r.returncode == 0, r.stderr
    rc = (run_dir / f"{name}.rc").resolve()
    deadline = time.monotonic() + HANG_S
    while not (pid := detach.running_pid(rc, lambda b: bool(b))) and time.monotonic() < deadline:
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


def test_any_local_gate_naming_a_jobs_rc_counts_as_waiting_on_that_job():
    import ttp.rebootwait as rw
    a, b = "/r/a.rc", "/r/b.rc"
    for probe, want in (
            (f"test -s {a}", [a]),
            (f"test -e {a} && [ -s {b} ]", [a, b]),
            (f"/x/state/runs/7/probe.sh {a}", [a]),
            (f"/x/bin/gate --rc={a} --timeout 5", [a]),
            (f"grep -q '^0$' {a} || ttp detach --check {b}", [a, b]),
            (f"bash -c 'test -s {a}'", [a]),
            (f"test -s /r/rows", []),
            (f"test -s /r/unrelated.log && test -e /r/out", []),
            (f"echo {a}", []),
            (f"ssh box test -s {a}", []),
            (f"/x/bin/gate --host box {a}", []),
            ("test -s job.rc", []),
            ("test -s $TTP_RUN_DIR/job.rc", []),
            (f"bash -c 'ssh box test -s {a}'", []),
            # doubt in one command that names no countable .rc leaves the others' jobs
            (f"ttp detach --check {a} && test \"$(date +%s)\" -ge 5", [a]),
            (f"ttp detach --check {a} && test -s $TTP_RUN_DIR/x.rc", [a]),
            (f"ttp detach --check {a} || test -s x.rc", [a]),
            (f"ttp detach --check {a} && test -s /r/*.rc", [a]),
            (f"! test -s {a}", [a]),
            # one naming an absolute .rc beside a substitution puts the whole probe in doubt
            (f"ttp detach --check {b} && test -s {a} -a -n \"$(pgrep x)\"", []),
            (f"bash -c 'test -s {a} -a -n \"$(pgrep x)\"'", [])):
        assert rw.local_jobs(probe) == [pathlib.Path(x) for x in want], probe
    assert rw.transient_besides_jobs(f"test -s {a} && test -s /x/state/runs/r1/rows") == ""
    assert rw.transient_besides_jobs(f"test -s {a} && pgrep soak") == "a process"
    assert rw.transient_besides_jobs(f"bash -c 'test -s {a} && pgrep soak'") == "a process"
    assert rw.local_jobs(f"test -s {a} -a -n \"$(pgrep soak)\"") == []


@pytest.mark.parametrize("form", ["test -s", "test -e", "script", "script-detach-check"])
def test_a_reboot_relaunches_a_job_whose_gate_names_its_rc_in_any_form(env, tmp_path, form):
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, counter = _resumable_job_cut_by_reboot(p, tmp_path, tid)
    if form.startswith("script"):
        script = rc.parent / "gate.sh"
        script.write_text('#!/bin/sh\n' + ('ttp detach --check "$1"\n' if form == "script-detach-check"
                                             else 'test -s "$1" && grep -q "^0" "$1"\n'))
        script.chmod(0o755)
        probe = f"{script} {rc}"
    else:
        probe = f"{form} {rc}"
    _wait_on(p, tid, probe)
    _boot_daemon(p)
    assert not _woken(p, tid) and p.db.task(tid)["not_before"], dict(p.db.task(tid))
    assert _wait_for_path(rc) and counter.read_text().split() == ["run", "run"]
    assert len(_job_relaunches(p, tid)) == 1


def test_a_reboot_relaunches_a_detach_check_beside_a_command_substitution(env, tmp_path):
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, counter = _resumable_job_cut_by_reboot(p, tmp_path, tid)
    _wait_on(p, tid, f'ttp detach --check {rc} || test "$(date +%s)" -ge {int(time.time()) + 86400}')
    _boot_daemon(p)
    assert not _woken(p, tid) and p.db.task(tid)["not_before"], dict(p.db.task(tid))
    assert _wait_for_path(rc) and counter.read_text().split() == ["run", "run"]
    assert len(_job_relaunches(p, tid)) == 1


def test_a_reboot_does_not_relaunch_for_a_gate_naming_an_unrelated_path(env, tmp_path):
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, counter = _resumable_job_cut_by_reboot(p, tmp_path, tid)
    other = tmp_path / "elsewhere" / "other.rc"
    _wait_on(p, tid, f"test -s {other}")
    _boot_daemon(p)
    assert counter.read_text().split() == ["run"] and not _job_relaunches(p, tid)


def test_a_run_folder_gate_script_probing_a_process_still_wakes(env, tmp_path):
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, counter = _resumable_job_cut_by_reboot(p, tmp_path, tid)
    script = rc.parent / "gate.sh"
    script.write_text(f'#!/bin/sh\ntest -s "$1" && kill -0 {_dead_pid()}\n')
    _wait_on(p, tid, f"{script} {rc}")
    _boot_daemon(p)
    assert _woken(p, tid), dict(p.db.task(tid))
    assert counter.read_text().split() == ["run"] and not _job_relaunches(p, tid)


def test_a_compound_probe_with_a_reboot_volatile_part_wakes_instead_of_relaunching(env, tmp_path):
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, counter = _resumable_job_cut_by_reboot(p, tmp_path, tid)
    _wait_on(p, tid, f"ttp detach --check {rc} && kill -0 {_dead_pid()}")
    _boot_daemon(p)
    assert _woken(p, tid), dict(p.db.task(tid))
    assert counter.read_text().split() == ["run"] and not _job_relaunches(p, tid)


def test_a_jobs_only_compound_probe_still_relaunches(env, tmp_path):
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, counter = _resumable_job_cut_by_reboot(p, tmp_path, tid)
    _wait_on(p, tid, f"ttp detach --check {rc} && test -s {rc.parent}/rows")
    _boot_daemon(p)
    assert not _woken(p, tid) and len(_job_relaunches(p, tid)) == 1
    assert _wait_for_path(rc)


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


def _over_before(p, tid, how):
    """The task's detached job (from _waiting_from_before_the_boot) ended before this boot: its .lost
    marker or its closed record dates from before the boot; "after" marks it lost only after the boot."""
    (run_dir,) = [pathlib.Path(r["dir"]) for r in p.db.q("SELECT dir FROM runs WHERE task=?", (tid,))]
    rc = run_dir / "soak.rc"
    if how == "closed":
        reg = run_dir / "detached.json"
        reg.write_text(json.dumps([{**j, "status": "died", "ended": time.time() - 3600}
                                   for j in json.loads(reg.read_text())]))
        return rc
    lost = rc.with_suffix(".lost")
    lost.write_text("lost\n")
    at = time.time() - (3600 if how == "lost" else 60)
    os.utime(lost, (at, at))
    return rc


@pytest.mark.parametrize("how, wakes", [("lost", False), ("closed", False), ("after", True)])
def test_a_job_over_before_the_boot_does_not_wake_its_task_after_a_reboot(env, how, wakes):
    p = make(env)
    tid = p.db.add_task("soak test", "s", kind="work", tier="light", origin="user")
    _waiting_from_before_the_boot(p, tid, job=True, retry_when="exit 1")
    _over_before(p, tid, how)
    _boot_daemon(p)
    assert _woken(p, tid) is wakes, dict(p.db.task(tid))


def test_a_bare_timer_wait_whose_job_each_reboot_cuts_is_blocked_by_the_third(env):
    """A bare-timer wait gets no reboot exemption when its own job was cut short: each such reboot
    counts toward max_reboot_losses, so a job that takes the host down is blocked, not relaunched."""
    p = make(env)
    tid = p.db.add_task("soak test", "s", kind="work", tier="light", origin="user")
    for i in range(1, 6):
        _waiting_from_before_the_boot(p, tid, boot=f"boot-{i}", retry_when='test "$(date +%s)" -ge 1900000000')
        _boot_daemon(p, f"boot-{i + 1}")
        if p.db.task(tid)["status"] == "blocked":
            break
    t = p.db.task(tid)
    assert t["status"] == "blocked" and i == 3, (i, dict(t))
    assert t["blocked_reason"] == "lost to a host reboot 3 times; it may be causing them"


_DURABLE_SCRIPT = ("#!/bin/sh\n# done once the remote job wrote its marker\n"
                   "ssh box 'test -e /tmp/soak.done' \\\n  && ttp ci --repo o/r --branch b\n"
                   'test "$(date +%s)" -ge 1900000000\n')


def _probe_script(run_dir, how):
    """A probe script in a run folder, made as `how` says; returns its path."""
    path = run_dir / "probe.sh"
    if how == "durable":
        path.write_text(_DURABLE_SCRIPT)
    elif how == "transient":   # an ssh line does not hide the next line's local process check
        path.write_text(_DURABLE_SCRIPT + "ssh box true\nkill -0 4242\n")
    elif how == "oversize":
        path.write_text(_DURABLE_SCRIPT + "#" * (64 * 1024) + "\n")
    elif how == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        path.write_text(_DURABLE_SCRIPT)
        path.chmod(0)
    elif how == "binary":
        path.write_bytes(b"\x7fELF\xff\xfe" + bytes(range(256)))
    elif how == "fifo":   # opened without blocking, and never read
        os.mkfifo(path)
    return path


@pytest.mark.parametrize("how, why", [
    ("durable", ""),
    ("transient", "a process"),
    ("oversize", None),   # None: judged as before, as a file in its run folder
    ("unreadable", None),
    ("binary", None),
    ("fifo", None),
    ("missing", None),
])
def test_a_run_folder_probe_script_is_judged_by_its_contents(tmp_path, how, why):
    from ttp import rebootwait as rw
    run_dir = tmp_path / "state" / "runs" / "7"
    run_dir.mkdir(parents=True)
    path = _probe_script(run_dir, how)
    runs = (str(tmp_path / "state" / "runs"),)
    if why is None:
        why = rw.transient(f"test -e {path}", runs)
        assert why in ("a file in a run folder", "a path a reboot clears"), why
    for probe in (str(path), f"sh {path}", f"timeout -k 5 30 bash -e {path}", f"X=1 {path} --quiet"):
        assert rw.transient(probe, runs) == why, probe
        assert rw.stays_asleep({"retry_when": probe}, runs) is (why == ""), probe
    # the rest of the probe still counts, and a script read as data is not run
    assert rw.transient(f"{path} && pgrep soak", runs) == why or "a process"
    assert rw.transient(f"test -s {path}", runs) == rw.transient(f"test -e {run_dir}/x", runs) != ""
    assert rw.transient(f"bash -c {path}", runs) == rw.transient(f"test -e {run_dir}/x", runs)


def test_a_run_folder_script_running_another_run_folder_script_wakes(tmp_path):
    from ttp import rebootwait as rw
    run_dir = tmp_path / "state" / "runs" / "7"
    run_dir.mkdir(parents=True)
    inner = _probe_script(run_dir, "durable")
    outer = run_dir / "outer.sh"
    outer.write_text(f"{inner}\n")
    assert rw.transient(str(outer)) == rw.transient(f"test -e {inner}") != "", "one level deep only"


@pytest.mark.parametrize("how, wakes", [("durable", False), ("transient", True), ("missing", True)])
def test_a_reboot_wakes_a_run_folder_script_wait_only_when_its_script_probes_transient_state(env, how, wakes):
    p = make(env)
    tid = p.db.add_task("soak test", "s", kind="work", tier="light", origin="user")
    run_dir = p.runs / f"script-{tid}"
    run_dir.mkdir(parents=True)
    _waiting_from_before_the_boot(p, tid, job=False, retry_when=str(_probe_script(run_dir, how)))
    _boot_daemon(p)
    assert _woken(p, tid) is wakes, dict(p.db.task(tid))


def _precheck_killed_by_reboot(p, tmp_path, tid):
    """A review precheck of code task tid (state/prechecks/t<tid>-<head12>) that the reboot killed, and the
    open review that reads it. Returns (its folder, the review's id)."""
    from ttp import cli
    from ttp.daemon import PRECHECKS_DIR
    wt, run_dir = _checks_killed_by_reboot(p, tmp_path, tid)
    p.db.update_task(tid, not_before=None, result="{}")   # only the precheck runs: the task waits on nothing
    info = json.loads((run_dir / cli.CHECKS_PID).read_text())
    out = p.state / PRECHECKS_DIR / f"t{tid}-{info['head'][:12]}"
    out.mkdir(parents=True)
    (out / cli.CHECKS_PID).write_text(json.dumps({**info, "arrived": info["ts"] - 60}))
    rid = p.db.add_task("review", "r", kind="review", labels=[f"precheck:{out.name}"])
    return out, rid


def _record_starts(monkeypatch, d):
    starts = []
    monkeypatch.setattr(d, "_start_checks", lambda path, out, run_id, tid, extra, arrived=None:
                        starts.append((out, run_id, arrived)) or True)
    return starts


def test_a_review_precheck_a_reboot_killed_restarts_once_at_its_old_queue_place(env, tmp_path, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    tid = p.db.add_task("feature", "s", kind="code", tier="light", origin="user")
    p.db.update_task(tid, status="review")
    out, _ = _precheck_killed_by_reboot(p, tmp_path, tid)
    arrived = json.loads((out / "checks.pid").read_text())["arrived"]
    d = Daemon(p.base)
    d.boot_at = time.time() - 300
    starts = _record_starts(monkeypatch, d)
    d.wake_after_reboot()
    assert starts == [(out, f"precheck-{out.name}", arrived)], starts
    assert len(p.db.q("SELECT id FROM events WHERE fingerprint LIKE ?", (f"precheck_relaunched:{out.name}:%",))) == 1
    again = Daemon(p.base)   # a daemon restart within the same boot starts nothing again
    again.boot_at = d.boot_at
    starts2 = _record_starts(monkeypatch, again)
    again.wake_after_reboot()
    assert starts2 == [], starts2


def test_a_precheck_whose_review_was_cancelled_stays_stopped_after_a_reboot(env, tmp_path, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    tid = p.db.add_task("feature", "s", kind="code", tier="light", origin="user")
    out, rid = _precheck_killed_by_reboot(p, tmp_path, tid)
    p.db.update_task(rid, status="cancelled")
    d = Daemon(p.base)
    d.boot_at = time.time() - 300
    starts = _record_starts(monkeypatch, d)
    d.wake_after_reboot()
    assert starts == [] and not p.db.q("SELECT id FROM events WHERE kind='checks_relaunched'")


def test_relaunched_task_checks_keep_their_place_in_the_host_queue(env, tmp_path, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    tid = p.db.add_task("feature", "s", kind="code", tier="light", origin="user")
    wt, run_dir = _checks_killed_by_reboot(p, tmp_path, tid)
    ts = json.loads((run_dir / "checks.pid").read_text())["ts"]
    d = Daemon(p.base)
    d.boot_at = time.time() - 300
    starts = _record_starts(monkeypatch, d)
    d.wake_after_reboot()
    assert starts == [(run_dir, f"relaunch-t{tid}", ts)], starts
    assert len(_relaunches(p, tid)) == 1


def test_a_run_whose_process_died_restarts_once_free_then_counts(env, tmp_path, monkeypatch):
    # A supervisor that died on this boot (crash, OOM kill): the first loss requeues with no attempt and
    # resumes the session; a second loss in a row counts an attempt, so a crash loop still ends.
    from ttp.daemon import Daemon
    p = make(env)
    _sessions(env, monkeypatch, "s")
    d = Daemon(p.base)
    tid = p.db.add_task("crashy", "s", kind="work", tier="light", origin="user")
    _lost_with_session(p, d, tmp_path, tid, name="a", session="s", boot=None)
    t = p.db.task(tid)
    assert t["attempts"] == 0 and t["status"] == "queued", t
    assert d._resumable(t, "fake")["cause"] == "crash"
    _lost_with_session(p, d, tmp_path, tid, name="b", session="s", boot=None)
    assert p.db.task(tid)["attempts"] == 1


def test_a_deferred_tasks_killed_checks_and_jobs_are_not_relaunched(env, tmp_path, monkeypatch):
    # A task deferred after its hand-off (start_when) waits on its start gate: the checks and resumable
    # jobs the reboot killed are not started for it (no host checks slot taken), and it is not woken.
    from ttp.daemon import Daemon
    p = make(env)
    chk = p.db.add_task("checks", "s", kind="code", tier="light", origin="user")
    _checks_killed_by_reboot(p, tmp_path, chk)
    job = p.db.add_task("job", "s", kind="work", tier="light", origin="user")
    rc, counter = _resumable_job_cut_by_reboot(p, tmp_path, job)
    for tid in (chk, job):
        p.db.update_task(tid, labels=json.dumps(["start_when:test -e /nonexistent/gate"]))
    d = Daemon(p.base)
    d.boot, d.boot_at = "a-later-boot", time.time() - 300
    starts = _record_starts(monkeypatch, d)
    d.wake_after_reboot()
    assert starts == [] and not _relaunches(p, chk)
    assert not _job_relaunches(p, job) and counter.read_text().split() == ["run"]
    for tid in (chk, job):
        assert not _woken(p, tid) and p.db.task(tid)["not_before"], dict(p.db.task(tid))
    assert "deferred" in (p.logs / "daemon.log").read_text()


def test_a_resumable_job_a_probe_marked_lost_to_the_reboot_is_relaunched(env, tmp_path):
    # A probe or status read marked the job .lost with the reboot as its reason before the daemon's boot
    # pass ran (or with a clock that puts it before boot_at): still the reboot's cut, so it relaunches.
    from ttp import detach
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, counter = _resumable_job_cut_by_reboot(p, tmp_path, tid)
    lost = detach.lost(rc)
    lost.write_text("the driver died with the host reboot: the host restarted while it ran (its boot id changed)")
    os.utime(lost, (time.time() - 3600, time.time() - 3600))
    _boot_daemon(p)
    assert not _woken(p, tid) and len(_job_relaunches(p, tid)) == 1
    assert _wait_for_path(rc) and counter.read_text().split() == ["run", "run"]


def test_a_survives_reboot_wait_still_relaunches_its_local_resumable_job(env, tmp_path):
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, counter = _resumable_job_cut_by_reboot(p, tmp_path, tid)
    prev = json.loads(p.db.task(tid)["result"])
    p.db.update_task(tid, result=json.dumps({**prev, "survives_reboot": True}))
    _boot_daemon(p)
    assert not _woken(p, tid) and len(_job_relaunches(p, tid)) == 1
    assert _wait_for_path(rc) and counter.read_text().split() == ["run", "run"]


def test_each_skipped_job_relaunch_says_why_in_the_daemon_log(env, tmp_path):
    p = make(env)
    tid = p.db.add_task("plain", "s", kind="work", tier="light", origin="user")
    _resumable_job_cut_by_reboot(p, tmp_path, tid, resumable=False)
    _boot_daemon(p)
    text = (p.logs / "daemon.log").read_text()
    assert f"task {tid}: detached jobs job not relaunched: detached job job is not resumable" in text, text[-2000:]


def test_a_reboot_never_relaunches_checks_of_a_deferred_or_closed_task(env, tmp_path):
    """#7190 (1): the checks a reboot killed start again (relaunch-t<task>) only for a task still open and not
    deferred. A start_when not yet met, a start_after still to come, or a task cancelled or done by the time
    the daemon looks: nothing starts and the task is not woken (a wake would drop its start_after timer); its
    checks run when it next starts. An open task, or one whose start_after has passed, is relaunched as before."""
    from ttp import cli
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.boot_at = time.time() - 300
    when = p.db.add_task("waits on a probe", "s", kind="code", tier="light", origin="user")
    _checks_killed_by_reboot(p, tmp_path, when)
    p.db.update_task(when, labels=json.dumps(["start_when:false"]))
    later = p.db.add_task("starts later", "s", kind="code", tier="light", origin="user")
    _checks_killed_by_reboot(p, tmp_path, later)
    p.db.update_task(later, labels=json.dumps([f"start_after:{time.time() + 7200}"]))
    racing = {}
    for status in ("cancelled", "done"):
        tid = p.db.add_task(status, "s", kind="code", tier="light", origin="user")
        _, run_dir = _checks_killed_by_reboot(p, tmp_path, tid)
        racing[tid] = (p.db.task(tid), run_dir)
        p.db.update_task(tid, status=status)   # closed after the boot scan read it
    fine = p.db.add_task("open", "s", kind="code", tier="light", origin="user")
    _, fine_dir = _checks_killed_by_reboot(p, tmp_path, fine)
    passed = p.db.add_task("start_after passed", "s", kind="code", tier="light", origin="user")
    _, passed_dir = _checks_killed_by_reboot(p, tmp_path, passed)
    p.db.update_task(passed, labels=json.dumps([f"start_after:{time.time() - 60}"]))
    d.wake_after_reboot()
    for tid in (when, later):
        t = p.db.task(tid)
        assert not _relaunches(p, tid), f"#{tid}: a deferred task's checks were started again"
        assert t["not_before"] and "woke" not in json.loads(t["result"]), f"#{tid} was woken"
        assert not (p.runs / f"checks-{tid}" / cli.CHECKS_RC).exists()
    for tid, (t, run_dir) in racing.items():
        assert d._relaunch_checks(t, json.loads(t["result"])) == (True, 0)
        assert not _relaunches(p, tid) and not (run_dir / cli.CHECKS_RC).exists(), p.db.task(tid)["status"]
        assert "woke" not in json.loads(p.db.task(tid)["result"])
    log_text = (p.logs / "daemon.log").read_text()
    for tid in (when, later):
        # The boot scan skips a task deferred when it reads it; one deferred after that read is caught again
        # where its checks would start (_no_relaunch).
        assert f"task {tid}: deferred (start_when or start_after); the work the reboot cut short" in log_text
        t = p.db.task(tid)
        assert d._relaunch_checks(t, json.loads(t["result"])) == (True, 0) and not _relaunches(p, tid)
    log_text = (p.logs / "daemon.log").read_text()
    assert f"task {when}: the reboot killed its checks, not started again: the task is deferred" in log_text
    assert f"task {later}: the reboot killed its checks, not started again: the task is deferred" in log_text
    for tid, run_dir in ((fine, fine_dir), (passed, passed_dir)):
        assert len(_relaunches(p, tid)) == 1, f"#{tid}: an open task's checks were not started again"
        assert _wait_precheck(run_dir) == "0"


def _crashed_checks(p, tmp_path, d, tid):
    """Task tid waits on its own detached checks, which died on this boot (d.boot) without checks.rc."""
    _, run_dir = _checks_killed_by_reboot(p, tmp_path, tid, boot=d.boot)
    return run_dir


def _crash_relaunches(p, tid):
    return p.db.q("SELECT * FROM events WHERE kind='checks_crash_relaunched' AND task=?", (tid,))


def test_checks_that_died_on_this_boot_restart_once_at_their_queue_place(env, tmp_path, monkeypatch):
    from ttp import cli
    from ttp.daemon import Daemon
    p = make(env)
    tid = p.db.add_task("feature", "s", kind="code", tier="light", origin="user")
    d = Daemon(p.base)
    run_dir = _crashed_checks(p, tmp_path, d, tid)
    ts = json.loads((run_dir / cli.CHECKS_PID).read_text())["ts"]
    starts = _record_starts(monkeypatch, d)
    d.relaunch_dead_checks()
    assert starts == [(run_dir, f"relaunch-t{tid}", ts)], starts
    (ev,) = _crash_relaunches(p, tid)
    assert ev["status"] == "handled" and ev["fingerprint"].endswith(
        json.loads((run_dir / cli.CHECKS_PID).read_text())["head"][:12]), dict(ev)
    assert p.db.task(tid)["not_before"] and "woke" not in json.loads(p.db.task(tid)["result"])
    assert not _relaunches(p, tid), "a same-boot restart is no reboot relaunch"
    # Dead again on the same head: left to the task's probe, which reports them killed as today.
    d.relaunch_dead_checks()
    assert len(starts) == 1 and len(_crash_relaunches(p, tid)) == 1
    assert cli._checks_result(run_dir) == 0 and (run_dir / cli.CHECKS_RC).read_text().strip() == "killed"


def test_checks_alive_finished_or_stopped_on_this_boot_are_not_restarted(env, tmp_path, monkeypatch):
    from ttp import cli
    from ttp.daemon import Daemon
    from ttp.runner import proc_start
    p = make(env)
    d = Daemon(p.base)
    starts = _record_starts(monkeypatch, d)
    alive = p.db.add_task("alive", "s", kind="code", tier="light", origin="user")
    run_dir = _crashed_checks(p, tmp_path, d, alive)
    info = json.loads((run_dir / cli.CHECKS_PID).read_text())
    (run_dir / cli.CHECKS_PID).write_text(json.dumps({**info, "pid": os.getpid(), "started": proc_start(os.getpid())}))
    ended = p.db.add_task("ended", "s", kind="code", tier="light", origin="user")
    (_crashed_checks(p, tmp_path, d, ended) / cli.CHECKS_RC).write_text("1\n")
    stopped = p.db.add_task("stopped", "s", kind="code", tier="light", origin="user")
    run_dir = _crashed_checks(p, tmp_path, d, stopped)
    info = json.loads((run_dir / cli.CHECKS_PID).read_text())
    p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
           (time.time(), "daemon", "checks_stopped", f"checks_stopped:{run_dir}:{info['pid']}:{info['started']}",
            "low", "stopped", "handled", stopped))
    earlier = p.db.add_task("earlier boot", "s", kind="code", tier="light", origin="user")
    _checks_killed_by_reboot(p, tmp_path, earlier)   # the reboot path's, not this one's
    d.relaunch_dead_checks()
    assert starts == [] and not p.db.q("SELECT id FROM events WHERE kind='checks_crash_relaunched'"), starts


def test_dead_checks_of_a_cancelled_done_or_deferred_task_are_not_restarted(env, tmp_path, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    starts = _record_starts(monkeypatch, d)
    for status in ("cancelled", "done"):
        tid = p.db.add_task(status, "s", kind="code", tier="light", origin="user")
        _crashed_checks(p, tmp_path, d, tid)
        p.db.update_task(tid, status=status)
    deferred = p.db.add_task("deferred", "s", kind="code", tier="light", origin="user")
    _crashed_checks(p, tmp_path, d, deferred)
    p.db.update_task(deferred, labels=json.dumps(["start_when:test -e /nonexistent/gate"]))
    d.relaunch_dead_checks()
    assert starts == [] and not p.db.q("SELECT id FROM events WHERE kind='checks_crash_relaunched'"), starts


def test_a_review_precheck_that_died_on_this_boot_restarts_once_while_its_review_is_open(env, tmp_path, monkeypatch):
    from ttp import cli
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    tid = p.db.add_task("feature", "s", kind="code", tier="light", origin="user")
    out, rid = _precheck_killed_by_reboot(p, tmp_path, tid)
    info = json.loads((out / cli.CHECKS_PID).read_text())
    (out / cli.CHECKS_PID).write_text(json.dumps({**info, "boot": d.boot}))
    shut = p.db.add_task("other", "s", kind="code", tier="light", origin="user")
    out2, rid2 = _precheck_killed_by_reboot(p, tmp_path, shut)
    (out2 / cli.CHECKS_PID).write_text(json.dumps({**json.loads((out2 / cli.CHECKS_PID).read_text()), "boot": d.boot}))
    p.db.update_task(rid2, status="cancelled")
    starts = _record_starts(monkeypatch, d)
    d.relaunch_dead_checks()
    d.relaunch_dead_checks()
    assert starts == [(out, f"precheck-{out.name}", info["arrived"])], starts
    assert len(p.db.q("SELECT id FROM events WHERE kind='checks_crash_relaunched'")) == 1
