# Detached job tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new `ttp detach` liveness tests here, not there.
from __future__ import annotations


def _ssh_on_path(tmp_path, monkeypatch):
    """An `ssh` on PATH that runs the remote command here with sh, or fails as an unreachable host
    (exit 255) while the returned flag file exists."""
    bindir, down = tmp_path / "sshbin", tmp_path / "ssh-down"
    bindir.mkdir()
    shim = bindir / "ssh"
    shim.write_text(f'#!/bin/sh\n[ -e {shlex.quote(str(down))} ] && exit 255\nfor last; do :; done\nexec sh -c "$last"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return down


def _remote_job(tmp_path, **remote):
    """The .rc path of a local job whose detached.json entry carries this remote contract."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    rc = run_dir / "job.rc"
    (run_dir / "detached.json").write_text(json.dumps([{"name": "job", "rc": str(rc), "remote": remote}]))
    return rc


def _dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_a_remote_hosts_changed_boot_id_is_a_reset_verdict(env, tmp_path, monkeypatch):
    from ttp import detach
    from ttp.runner import boot_id
    _ssh_on_path(tmp_path, monkeypatch)
    assert detach.remote_start("box") == boot_id()
    rc = _remote_job(tmp_path, host="box", boot="a-boot-before-the-reset", pid=str(os.getpid()), marker="")
    why = detach.remote_verdict(rc, {})
    assert why and why.startswith("remote reset:") and "box" in why
    rc = _remote_job(tmp_path, host="box", boot=boot_id(), pid=str(os.getpid()), marker="")
    assert detach.remote_verdict(rc, {}) is None, "a live remote pid on the same boot was called dead"


def test_a_remote_pid_gone_without_its_marker_is_a_verdict_only_on_the_second_probe(env, tmp_path, monkeypatch):
    from ttp import detach
    from ttp.runner import boot_id
    _ssh_on_path(tmp_path, monkeypatch)
    pidfile = tmp_path / "driver.pid"
    rc = _remote_job(tmp_path, host="box", boot=boot_id(), pid=str(pidfile), marker=str(tmp_path / "DONE"))
    memo: dict = {}
    assert detach.remote_verdict(rc, memo) is None, "a pid file not written yet is no dead driver"
    pidfile.write_text(f"{_dead_pid()}\n")
    assert detach.remote_verdict(rc, memo) is None, "one probe that reads the pid dead ended the job"
    why = detach.remote_verdict(rc, memo)
    assert why and why.startswith("remote driver gone:") and "DONE is missing" in why
    pidfile.write_text(f"{os.getpid()}\n")   # alive again (a restarted driver): the count starts over
    assert detach.remote_verdict(rc, memo) is None and "dead" not in memo


def test_a_remote_completion_marker_is_never_a_dead_verdict(env, tmp_path, monkeypatch):
    from ttp import detach
    from ttp.runner import boot_id
    _ssh_on_path(tmp_path, monkeypatch)
    (tmp_path / "DONE").write_text("0\n")
    rc = _remote_job(tmp_path, host="box", boot=boot_id(), pid=str(_dead_pid()), marker=str(tmp_path / "DONE"))
    memo: dict = {}
    assert [detach.remote_verdict(rc, memo) for _ in range(4)] == [None] * 4


def test_an_unreachable_remote_host_is_no_proof_of_death(env, tmp_path, monkeypatch):
    from ttp import detach
    down = _ssh_on_path(tmp_path, monkeypatch)
    down.touch()
    assert detach.remote_start("box") == ""
    rc = _remote_job(tmp_path, host="box", boot="a-boot-before-the-reset", pid=str(_dead_pid()), marker="")
    memo: dict = {}
    assert [detach.remote_verdict(rc, memo) for _ in range(4)] == [None] * 4
    # Unreachable at start: the first answer names the boot, and a later change of it is a reset.
    down.unlink()
    rc = _remote_job(tmp_path, host="box", boot="", pid="", marker="")
    memo = {}
    assert detach.remote_verdict(rc, memo) is None and memo["boot"]
    memo["boot"] = "a-boot-before-the-reset"
    assert (detach.remote_verdict(rc, memo) or "").startswith("remote reset:")


def test_remote_pid_and_marker_need_remote_host(env, tmp_path):
    r = subprocess.run([sys.executable, str(TTP), "detach", "job", "--remote-pid", "5", "--", "true"],
                       capture_output=True, text=True, env=dict(os.environ, TTP_RUN_DIR=str(tmp_path)))
    assert r.returncode != 0 and "go with --remote-host" in r.stderr


def test_stop_records_its_reason_before_a_reader_can_see_the_job_gone(tmp_path, monkeypatch):
    from ttp import detach
    rc = _remote_job(tmp_path)
    seen = []
    # A reader that looks the moment the kill lands (its lock is free) must find the stop's reason.
    monkeypatch.setattr(detach.os, "killpg", lambda pid, sig: seen.append(detach.state(rc)))
    detach.stop(rc, 12345, "remote driver gone: test")
    assert seen == [("lost", "remote driver gone: test")]
    assert detach.state(rc) == ("lost", "remote driver gone: test")


def test_stop_records_its_reason_before_the_job_ends(env, tmp_path, monkeypatch):
    """The keeper's loop reads the job's state while its probe thread stops it: a look between the kill
    and the record took the job for one killed with no reason and wrote that, over the remote verdict."""
    from ttp import detach, locks
    from ttp.runner import boot_id
    rc = _remote_job(tmp_path, host="box", boot=boot_id(), pid="", marker="")
    detach.started(rc).write_text("4242\n")
    killed, seen = [], []
    monkeypatch.setattr(locks, "any_free", lambda paths: bool(killed))
    monkeypatch.setattr(detach.os, "killpg", lambda pid, sig: killed.append(pid) or seen.append(detach.state(rc)))
    assert detach.state(rc) == ("running", "")
    why = "remote driver gone: pid 7 on box is dead"
    detach.stop(rc, 4242, why)
    assert killed == [4242] and seen == [("lost", why)], seen
    assert detach.state(rc) == ("lost", why)


def test_a_keeper_ends_a_job_whose_remote_driver_died_and_lets_its_hold_go(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp import detach, hold, locks
    from ttp.daemon import Daemon
    from ttp.runner import boot_id
    down = _ssh_on_path(tmp_path, monkeypatch)
    mine = _board_task(p, "flash")
    run_dir, release, pidfile = p.runs / "51", tmp_path / "release", tmp_path / "driver.pid"
    driver = subprocess.Popen(["sleep", "600"])   # stands in for the driver on the remote host
    pidfile.write_text(f"{driver.pid}\n")
    paths = Daemon(p.base)._slot_paths("board")
    try:
        _exclusive_run_detaching(p, mine["id"], 51, run_dir, _until(release),
                                 opts=("--remote-host", "box", "--remote-pid", str(pidfile),
                                       "--remote-marker", str(tmp_path / "DONE")))
        entry = json.loads((run_dir / "detached.json").read_text())[0]
        assert entry["remote"] == {"host": "box", "boot": boot_id(), "pid": str(pidfile),
                                   "marker": str(tmp_path / "DONE")}
        rc = pathlib.Path(entry["rc"])
        assert hold.read(run_dir)["state"] == "holding" and not locks.any_free(paths)
        # The host stops answering and its driver dies: unreachable proves nothing, the hold stays.
        down.touch()
        driver.kill()
        driver.wait()
        time.sleep(1)
        assert not locks.any_free(paths) and not locks.job_ended(rc), "an unreachable host ended the job"
        down.unlink()
        assert _until_true(lambda: locks.any_free(paths)), "a dead remote driver's hold was kept"
        st, why = detach.state(rc)
        assert st == "lost" and why.startswith("remote driver gone:")
        rec = hold.read(run_dir)
        assert rec["state"] == "released" and "every detached job of the run ended" in rec["why"]
        assert "remote driver gone" in rec["why"]
        assert not rc.exists(), "the local driver was not ended"
    finally:
        release.touch()
        if driver.poll() is None:
            driver.kill()


def test_a_stopped_job_reads_its_verdict_the_moment_it_dies(env, tmp_path, monkeypatch):
    # The keeper reads its jobs' state while its probe thread ends one: a job seen dead before its
    # verdict was written got a generic "killed" reason, and the keeper then exited with the thread.
    from ttp import detach, locks
    rc = _remote_job(tmp_path, host="box", boot="", pid="", marker="")
    job = locks.try_take([locks.job_lock(rc)], "job")
    seen = []

    def killpg(pid, sig):
        job.close()
        seen.append(detach.state(rc))
    monkeypatch.setattr(detach.os, "killpg", killpg)
    detach.stop(rc, 12345, "remote driver gone: pid 7 on box is dead")
    assert seen == [("lost", "remote driver gone: pid 7 on box is dead")]
    assert detach.state(rc) == seen[0]


def _remote_mirror_job(env, tmp_path, script, name="drv"):
    """A `ttp detach --remote` job started from run 61 through the ssh stub: (project, run dir, remote
    folder, probe argv, ssh env)."""
    p = _device_project(env, device=False)
    run_dir = p.runs / "61"
    run_dir.mkdir(parents=True)
    e = _ssh_stub(tmp_path)
    rdir = tmp_path / "remote"
    r = _ttp_run(p, "detach", "--remote", "box-m", "--dir", str(rdir), name, "--", "sh", "-c", script,
                 env={**e, "TTP_RUN_DIR": str(run_dir)})
    assert r.returncode == 0, r.stderr
    return p, run_dir, rdir, _remote_probe(r.stdout), e


def test_stop_notes_its_verdict_before_the_kill_so_a_concurrent_look_never_reports_a_plain_crash(tmp_path, monkeypatch):
    from ttp import detach
    rc = _remote_job(tmp_path)
    detach.started(rc).write_text("4242\n")
    seen = []
    # Another reader (the daemon, a check) looks at the job the instant its process group dies.
    monkeypatch.setattr(detach.os, "killpg", lambda pid, sig: seen.append(detach.state(rc)))
    detach.stop(rc, 4242, "remote driver gone: pid 7 on box is dead")
    assert seen == [("lost", "remote driver gone: pid 7 on box is dead")]
    assert detach.state(rc) == ("lost", "remote driver gone: pid 7 on box is dead")


def test_a_late_crash_guess_never_overwrites_a_recorded_verdict(tmp_path):
    from ttp import detach
    rc = _remote_job(tmp_path)
    detach._mark_lost(rc, "remote reset: the host restarted")
    assert detach._mark_lost_first(rc, "it was killed after it started") == "remote reset: the host restarted"
    assert detach.lost(rc).read_text().strip() == "remote reset: the host restarted"
    (tmp_path / "b").mkdir()
    other = _remote_job(tmp_path / "b")
    assert detach._mark_lost_first(other, "it was killed after it started") == "it was killed after it started"
    assert detach.lost(other).read_text().strip() == "it was killed after it started"


def test_detach_remote_check_mirrors_the_rc_and_reads_it_once_the_host_is_unreachable(env, tmp_path):
    """The reported failure: a remote job's .log and .rc stayed on a host that then reset, so its probe
    returned 255 forever though the job had finished. The probe now mirrors both into the run folder,
    and with ssh failing it reads the mirrored end: 4 for a failure, 0 for a pass, never 255."""
    p, run_dir, rdir, probe, e = _remote_mirror_job(env, tmp_path, "printf 'out\\377\\n'; exit 3")
    assert probe[probe.index("--mirror") + 1] == str(run_dir.resolve()), probe
    meta = json.loads((run_dir / "drv.remote.json").read_text())
    assert meta["host"] == "box-m" and meta["path"] == str(rdir / "drv") and "end" not in meta
    assert _wait_for_path(rdir / "drv.rc")
    first = _ttp_run(p, *probe[1:], env=e)   # the daemon's probe env carries no TTP_RUN_DIR
    assert first.returncode == 4 and "drv: job finished rc=3" in first.stdout, first.stdout
    assert (run_dir / "drv.remote.rc").read_text().strip() == "3"
    assert (run_dir / "drv.remote.log").read_bytes() == b"out\xff\n", "the log tail is mirrored byte for byte"
    assert json.loads((run_dir / "drv.remote.json").read_text())["end"] == {"rc": "3"}
    down = _ttp_run(p, *probe[1:], env={**e, "SSH_STUB_FAIL": "1"})
    assert down.returncode == 4, down.stdout
    assert "job finished rc=3, as mirrored before box-m stopped answering" in down.stdout, down.stdout
    # The check alone with the env's TTP_RUN_DIR mirrors there too; a mirror is never another job's.
    (run_dir / "drv.remote.json").write_text(json.dumps({"host": "other", "path": str(rdir / "drv"), "end": {"rc": "0"}}))
    assert _ttp_run(p, *probe[1:], env={**e, "SSH_STUB_FAIL": "1"}).returncode == 255
    assert _ttp_run(p, "detach", "--check", str(rdir / "drv.rc"), "--mirror", str(run_dir)).returncode == 2
    r = _ttp_run(p, "detach", "--remote", "box-m", "--dir", str(rdir), "ok", "--", "echo", "fine",
                 env={**e, "TTP_RUN_DIR": str(run_dir)})
    assert r.returncode == 0 and _wait_for_path(rdir / "ok.rc"), r.stderr
    ok = _remote_probe(r.stdout)
    assert _ttp_run(p, *ok[1:], env=e).returncode == 0
    assert _ttp_run(p, *ok[1:], env={**e, "SSH_STUB_FAIL": "1"}).returncode == 0


def test_detach_remote_mirror_keeps_a_bounded_log_tail_sent_only_when_it_grew(env, tmp_path, monkeypatch):
    from ttp import cli, detach
    big = detach.MIRROR_TAIL_BYTES * 3
    p, run_dir, rdir, probe, e = _remote_mirror_job(
        env, tmp_path, f"head -c {big} /dev/zero | tr '\\0' a; echo END; exit 0")
    assert _wait_for_path(rdir / "drv.rc")
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    sent = []
    real = cli._ssh_sh
    monkeypatch.setattr(cli, "_ssh_sh", lambda *a, **k: sent.append(real(*a, **k)) or sent[-1])
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli._detach_check_remote("box-m", [str(rdir / "drv")], mirror=str(run_dir)) == 0
    tail = (run_dir / "drv.remote.log").read_bytes()
    assert len(tail) == detach.MIRROR_TAIL_BYTES and tail == (rdir / "drv.log").read_bytes()[-len(tail):]
    assert tail.endswith(b"END\n")
    assert len(sent[-1].stdout) < detach.MIRROR_TAIL_BYTES * 2, "more than the tail crossed ssh"
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli._detach_check_remote("box-m", [str(rdir / "drv")], mirror=str(run_dir)) == 0
    assert "tail same" in sent[-1].stdout and len(sent[-1].stdout) < 200, "an unchanged log was sent again"
    assert (run_dir / "drv.remote.log").read_bytes() == tail


def test_detach_remote_mirror_unreachable_before_the_end_stays_255_and_the_keeper_refreshes(env, tmp_path, monkeypatch):
    """A host unreachable before any end was mirrored proves nothing: 255 (asleep), naming the log tail
    kept so far. The hold keeper's probe round refreshes the run's mirrors, so the end is caught while
    the host still answers even with no check running."""
    from ttp import detach, hold
    go = tmp_path / "go"
    p, run_dir, rdir, probe, e = _remote_mirror_job(
        env, tmp_path, f"echo step1; while [ ! -e {go} ]; do sleep 0.1; done; echo step2; exit 5")
    pid = int((rdir / "drv.pid").read_text())
    try:
        assert _wait_for_path(rdir / "drv.log") and _until_true(lambda: (rdir / "drv.log").read_text().strip())
        assert _ttp_run(p, *probe[1:], env=e).returncode == 1
        assert (run_dir / "drv.remote.log").read_text() == "step1\n"
        down = _ttp_run(p, *probe[1:], env={**e, "SSH_STUB_FAIL": "1"})
        assert down.returncode == 255 and "ssh to box-m failed" in down.stdout, down.stdout
        assert "last log tail mirrored here" in down.stdout and not (run_dir / "drv.remote.rc").exists()
        go.touch()
        assert _wait_for_path(rdir / "drv.rc")
    finally:
        _kill_driver(pid)
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    hold._remote_round([], {}, [], run_dir)
    assert (run_dir / "drv.remote.rc").read_text().strip() == "5"
    assert (run_dir / "drv.remote.log").read_text() == "step1\nstep2\n"
    monkeypatch.setenv("SSH_STUB_FAIL", "1")
    detach.mirror_refresh(run_dir)   # an ended mirror is not asked again; an unreachable host changes nothing
    down = _ttp_run(p, *probe[1:], env={**e, "SSH_STUB_FAIL": "1"})
    assert down.returncode == 4 and "job finished rc=5, as mirrored" in down.stdout, down.stdout


def test_detach_mirror_probe_reads_the_verdict_before_the_tail(env, tmp_path, monkeypatch):
    """A job that writes its last lines after the verdict was read must still have them in the mirrored
    tail, and the verdict stays the answer's last line."""
    from ttp import cli, detach
    e = _ssh_stub(tmp_path)
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    job = tmp_path / "job"
    (tmp_path / "job.log").write_text("early\n")
    # The fake check reports the job running, then the job writes its last line (as if between steps).
    monkeypatch.setattr(cli, "_REMOTE_CHECK", f'echo "running 1"\necho late >>{tmp_path}/job.log\n')
    d = tmp_path / "mirror"
    d.mkdir()
    r, lines = detach.mirror_probe("box-m", str(job), d)
    assert r.returncode == 0 and lines[-1] == "running 1", lines
    assert (d / "job.remote.log").read_text() == "early\nlate\n", "the tail was taken before the verdict"


def test_a_relative_rc_never_leaves_a_lost_marker_in_the_callers_checkout(env, tmp_path, monkeypatch):
    """A probe run from a checkout names a path no job wrote, relative to its cwd: it still reads as gone
    (4), but no <name>.lost lands in that checkout, from the CLI or from detach.state."""
    from ttp import detach
    p = make(env)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    r = subprocess.run([sys.executable, str(TTP), "detach", "--check", "job.rc"], cwd=checkout,
                       capture_output=True, text=True, env=dict(os.environ, TTP_PROJECT=str(p.root)))
    assert r.returncode == 4 and "gone" in r.stdout, (r.stdout, r.stderr)
    monkeypatch.chdir(checkout)
    assert detach.state("job.rc")[0] == "lost"
    assert detach._mark_lost_first("other.rc", "x") == "x"
    assert not list(checkout.iterdir()), "a marker landed in the caller's checkout"


def test_lost_markers_go_only_into_state_and_run_folders(env, tmp_path, monkeypatch):
    from ttp import detach
    p = make(env)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setattr(detach.os, "killpg", lambda pid, sig: None)
    detach._mark_lost(outside / "a.rc", "why")
    detach.stop(outside / "b.rc", 4242, "why")
    detach.end_remote(outside / "c.rc", "why")
    assert detach.state(outside / "d.rc")[0] == "lost"
    assert not list(outside.iterdir()), "a marker was written outside the state and run folders"
    run_dir, fix = p.runs / "7", p.state / "heal-fix" / "x"   # a run's folder, the daemon's own jobs
    for d in (run_dir, fix):
        d.mkdir(parents=True)
        detach._mark_lost(d / "job.rc", "killed")
        assert detach.lost(d / "job.rc").read_text().strip() == "killed"
    own = tmp_path / "own-run"   # the calling run's folder, wherever it is
    own.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(own))
    monkeypatch.chdir(own)
    assert detach._mark_lost_first("job.rc", "it never started") == "it never started"
    assert detach.lost(own / "job.rc").read_text().strip() == "it never started"


# `ttp detach --lock <resource>`: the job takes its locks all or nothing through `ttp lock` (cmd_lock)
# before its command starts and holds them for the job's life.

def _lock_job_env(env, p, run_dir, task="41", run="7"):
    return {"TTP_HOME": str(env["home"]), "TTP_HOST": "testhost", "TTP_RUN_DIR": str(run_dir),
            "TTP_TASK": task, "TTP_RUN_ID": run}


def _held_now(p, res):
    from ttp import shared
    return [x for x in shared.held(p) if x.startswith(f"{res}: ")]


def _probe_json(p, run_env, res):
    r = subprocess.run([sys.executable, str(TTP), "lock", "--probe", "--json", res],
                       env={**os.environ, "TTP_PROJECT": str(p.base), **run_env}, capture_output=True, text=True, timeout=60)
    return r.returncode, json.loads(r.stdout)


def test_detach_without_lock_prints_and_records_exactly_as_before(env, tmp_path):
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    r = _ttp_run(p, "detach", "job1", "--", "true", env=_lock_job_env(env, p, run_dir))
    assert r.returncode == 0, r.stderr
    rc = (run_dir / "job1.rc").resolve()
    job = json.loads((run_dir / "detached.json").read_text())[0]
    lines = r.stdout.splitlines()
    assert lines == [f"detached job1 (pid {job['pid']})", f"log: {rc.with_suffix('.log')}", f"rc:  {rc}",
                     f"hand off: status waiting, retry_when \"{TTP} detach --check {rc}\""], lines
    assert job["command"] == "true" and "locks" not in job
    assert _wait_for_path(rc) and rc.read_text().strip() == "0"


def test_detach_lock_holds_one_or_two_locks_for_the_jobs_life_and_lets_go_once_its_rc_is_written(env, tmp_path):
    p = make(env)
    for n, names in enumerate((["rig-a"], ["rig-a", "rig-b"])):
        run_dir = p.runs / str(7 + n)
        run_dir.mkdir(parents=True)
        go, ran = tmp_path / f"go{n}", tmp_path / f"ran{n}"
        flags = [w for x in names for w in ("--lock", x)]
        cmd = ["sh", "-c", f"touch {shlex.quote(str(ran))}; {shlex.join(_until(go))}"]
        run_env = _lock_job_env(env, p, run_dir, run=str(7 + n))
        r = _ttp_run(p, "detach", "job", *flags, "--", *cmd, env=run_env)
        assert r.returncode == 0, r.stderr
        assert f"locks: {','.join(names)} (taken all or nothing" in r.stdout, r.stdout
        rc = (run_dir / "job.rc").resolve()
        job = json.loads((run_dir / "detached.json").read_text())[0]
        assert job["locks"] == names
        try:
            assert _until_true(ran.exists)
            for res in names:   # held by this task's run and job while it runs
                assert _held_now(p, res), res
                code, said = _probe_json(p, run_env, res)
                h = said["holders"][0]
                assert code == 75 and h["holder"] == f"task #41 (run {7 + n})" and h["alive"] is True, said
                assert h["job"] == "job" and h["job_pid"] == job["pid"] and h["job_rc"] == str(rc), h
                assert f"(detached job job, job pid {job['pid']})" in h["note"], h["note"]
            assert _ttp_run(p, "detach", "--check", str(rc)).returncode == 1
        finally:
            go.touch()
        assert _wait_for_path(rc) and rc.read_text().strip() == "0"
        for res in names:
            assert _until_true(lambda: not _held_now(p, res)), res
            assert _probe_json(p, run_env, res)[0] == 0


def test_detach_lock_lets_go_once_the_job_is_lost(env, tmp_path):
    from ttp import detach
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    ran = tmp_path / "ran"
    cmd = ["sh", "-c", f"touch {shlex.quote(str(ran))}; {shlex.join(_until(tmp_path / 'never'))}"]
    r = _ttp_run(p, "detach", "job", "--lock", "rig-a,rig-b", "--", *cmd, env=_lock_job_env(env, p, run_dir))
    assert r.returncode == 0, r.stderr
    rc = (run_dir / "job.rc").resolve()
    pid = json.loads((run_dir / "detached.json").read_text())[0]["pid"]
    assert _until_true(ran.exists) and _held_now(p, "rig-a") and _held_now(p, "rig-b")
    os.killpg(pid, signal.SIGKILL)   # a kill or a reboot: the job is gone without an .rc
    assert _until_true(lambda: detach.state(rc)[0] == "lost"), detach.state(rc)
    assert not rc.exists() and not _held_now(p, "rig-a") and not _held_now(p, "rig-b")


def test_detach_lock_holds_nothing_while_another_task_holds_one_of_them(env, tmp_path):
    from ttp import detach
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    go, ran = tmp_path / "go", tmp_path / "ran"
    other = _lock(_lock_env(env, p, TTP_TASK="99", TTP_RUN_ID="3"), "rig-b", cmd=_until(go))
    try:
        assert _until_true(lambda: bool(_held_now(p, "rig-b")))
        r = _ttp_run(p, "detach", "job", "--lock", "rig-a", "--lock", "rig-b", "--", "touch", str(ran),
                     env=_lock_job_env(env, p, run_dir))
        assert r.returncode == 0, r.stderr
        rc = (run_dir / "job.rc").resolve()
        assert _until_true(lambda: _queue_len(p, "rig-a") == 1 and _queue_len(p, "rig-b") == 1)
        time.sleep(0.5)   # several polls: rig-a is free, yet the job keeps it free while it waits for rig-b
        assert not _held_now(p, "rig-a") and not ran.exists() and detach.state(rc)[0] == "running"
    finally:
        go.touch()
    assert other.wait(timeout=60) == 0
    assert _wait_for_path(rc) and rc.read_text().strip() == "0" and ran.exists()
    assert not _held_now(p, "rig-a") and not _held_now(p, "rig-b")


def test_detach_lock_on_a_paused_resource_exits_75_and_starts_nothing(env, tmp_path):
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    p.db.set_kv("paused_resources", {"rig-b": {"reason": "maintenance", "since": time.time()}})
    r = _ttp_run(p, "detach", "job", "--lock", "rig-a,rig-b", "--", "touch", str(tmp_path / "ran"),
                 env=_lock_job_env(env, p, run_dir))
    assert r.returncode == 75 and "rig-b is paused (maintenance)" in r.stderr, (r.returncode, r.stderr)
    assert "retry_when `ttp lock --probe rig-a,rig-b`" in r.stderr
    assert not (run_dir / "detached.json").exists() and not (run_dir / "job.log").exists()
    assert _queue_len(p, "rig-a") == 0 and not (tmp_path / "ran").exists()
    r = _ttp_run(p, "detach", "--remote", "box", "job", "--lock", "rig-a", "--", "true", env=_lock_job_env(env, p, run_dir))
    assert r.returncode != 0 and "--lock is for a local job" in r.stderr, r.stderr


# A job's detached.json record closes once the job is over (detach.close, the daemon's close_records,
# `ttp detach --reap`). Each was red before the record had a status (no close existed) and is green now.
def _detach_job(p, run_dir, env, name, cmd, *lock):
    r = _ttp_run(p, "detach", name, *[x for res in lock for x in ("--lock", res)], "--", *cmd,
                 env=_lock_job_env(env, p, run_dir))
    assert r.returncode == 0, r.stderr
    return (run_dir / f"{name}.rc").resolve()


def _record(rc):
    return next(j for j in json.loads((rc.parent / "detached.json").read_text()) if j["rc"] == str(rc))


def test_detach_record_of_a_job_that_wrote_its_rc_closes_as_ended_in_one_sweep(env, tmp_path):
    from ttp import detach
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    rc = _detach_job(p, run_dir, env, "job", ["sh", "-c", "exit 3"])
    assert _record(rc)["status"] == "running"
    assert _until_true(lambda: rc.exists() and detach.state(rc)[0] == "ended")
    assert detach.close_records(p) == 1
    e = _record(rc)
    assert (e["status"], e["exit"], e["ended"]) == ("ended", 3, rc.stat().st_mtime)
    assert detach.close_records(p, now=time.time() + 120) == 0 and _record(rc) == e   # closed once


def test_detach_record_of_a_killed_driver_closes_as_died_with_no_exit_code(env, tmp_path):
    from ttp import detach
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    rc = _detach_job(p, run_dir, env, "job", _until(tmp_path / "never"))
    assert _until_true(lambda: detach.started(rc).exists())
    os.killpg(_record(rc)["pid"], signal.SIGKILL)
    assert _until_true(lambda: detach.close(rc) is not None)
    e = _record(rc)
    assert (e["status"], e["exit"]) == ("died", None) and "killed" in e["why"] and not rc.exists()


def test_detach_record_whose_pid_another_process_reuses_closes_and_a_live_one_stays_open(env, tmp_path):
    from ttp import detach
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    gone = _detach_job(p, run_dir, env, "gone", _until(tmp_path / "never"))
    live = _detach_job(p, run_dir, env, "live", _until(tmp_path / "stop"))
    assert _until_true(lambda: detach.started(gone).exists() and detach.started(live).exists())
    os.killpg(_record(gone)["pid"], signal.SIGKILL)
    assert _until_true(lambda: detach.locks.any_free([detach.locks.job_lock(gone)]))
    with detach.registry(run_dir) as jobs:   # its pid now names a live process that is not the job
        next(j for j in jobs if j["name"] == "gone")["pid"] = os.getpid()
    assert detach.close_records(p) == 1
    assert _record(gone)["status"] == "died" and _record(live)["status"] == "running"
    (tmp_path / "stop").touch()
    assert _until_true(lambda: detach.close(live) is not None) and _record(live)["status"] == "ended"


def test_detach_reap_closes_an_ended_job_refuses_a_live_one_and_is_idempotent(env, tmp_path):
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    rc = _detach_job(p, run_dir, env, "job", _until(tmp_path / "stop"))
    e = _lock_job_env(env, p, run_dir)
    r = _ttp_run(p, "detach", "--reap", "job", env=e)
    assert r.returncode == 1 and "still running" in r.stderr and _record(rc)["status"] == "running"
    (tmp_path / "stop").touch()
    assert _until_true(rc.exists)
    first = _ttp_run(p, "detach", "--reap", "job", env=e)
    assert first.returncode == 0 and "job: ended rc=0" in first.stdout, first.stderr
    closed = _record(rc)
    again = _ttp_run(p, "detach", "--reap", str(rc), env=e)
    assert (again.returncode, again.stdout) == (0, first.stdout) and _record(rc) == closed
    assert _ttp_run(p, "detach", "--reap", "other", env=e).returncode == 2
    assert "--reap" in _ttp_run(p, "detach", "--help").stdout


def test_detach_lock_job_closed_as_died_leaves_no_lock_held_or_shown_held(env, tmp_path):
    from ttp import activity, detach
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    rc = _detach_job(p, run_dir, env, "job", _until(tmp_path / "never"), "rig-a,rig-b")
    assert _until_true(lambda: _held_now(p, "rig-a") and _held_now(p, "rig-b"))
    # `ttp lock` logs its takes just after it gets the locks: kill it only once they are logged.
    assert _until_true(lambda: {r["resource"] for r in activity.entries(p.state) if r.get("ev") == "take"}
                       == {"rig-a", "rig-b"})
    os.killpg(_record(rc)["pid"], signal.SIGKILL)   # its `ttp lock` dies too and logs no release
    assert _until_true(lambda: detach.close(rc) is not None) and _record(rc)["status"] == "died"
    assert not _held_now(p, "rig-a") and not _held_now(p, "rig-b")
    recs = activity.entries(p.state)
    takes = [r for r in recs if r.get("ev") == "take"]
    assert {r["resource"] for r in takes} == {"rig-a", "rig-b"} and all(r["job_rc"] == str(rc) for r in takes)
    for r in takes:   # the lock's pid reused by a live process never shows the lock as held
        r["pid"] = os.getpid()
    assert [s["state"] for s in activity.spans(recs, "testhost") if s["kind"] == "lock"] == ["unknown", "unknown"]


# Stopping the detached jobs of a task that ended without success (jobstop), and `ttp detach --stop`.
# The broker is a stub on PATH; every process signalled is one this test started.

def _stub_broker(tmp_path, status=None, fail=False):
    """A `tt-device-mcp` stub: `run` records its pid and sleeps (a client), `status -j` prints the
    jobs given (or exits 1), `kill <id>` records the id and the client's process state."""
    d = tmp_path / "brokerbin"
    d.mkdir(exist_ok=True)
    (d / "status.json").write_text(json.dumps({"jobs": status or []}))
    stub = d / "tt-device-mcp"
    stub.write_text(f'''#!/bin/sh
d={shlex.quote(str(d))}
case "$1" in
  run) echo $$ >"$d/client.pid"; sleep 60; exit 0 ;;
  status) {"exit 1" if fail else 'cat "$d/status.json"'} ;;
  kill) echo "$2 $(cut -d' ' -f3 /proc/$(cat "$d/client.pid")/stat)" >>"$d/kills" ;;
esac
''')
    stub.chmod(0o755)
    return d


def _broker_jobs(me):
    return [{"job_id": 11, "owner": me, "command": "sleep 60", "status": "running"},
            {"job_id": 12, "owner": "another-user", "command": "sleep 60", "status": "running"},
            {"job_id": 13, "owner": me, "command": "sleep 61", "status": "running"},
            {"job_id": 14, "owner": me, "command": "sleep 60", "status": "done", "finished_at": 1}]


def _locked_job(p, tid, broker, name="loop", status="running"):
    """Task tid's run started `ttp detach --lock builddir <name>`, whose loop runs a broker client;
    returns (rc, pgid) once the client runs."""
    run_dir = p.runs / f"stop-{tid}"
    run_dir.mkdir(parents=True, exist_ok=True)
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 60, "ok", str(run_dir)))
    (broker / "client.pid").unlink(missing_ok=True)
    loop = f"while :; do {shlex.quote(str(broker / 'tt-device-mcp'))} run -- sleep 60; done"
    r = _ttp_run(p, "detach", "--lock", "builddir", name, "--", "sh", "-c", loop, env={"TTP_RUN_DIR": str(run_dir)})
    assert r.returncode == 0, r.stderr
    rc = (run_dir / f"{name}.rc").resolve()
    assert _wait_for_path(broker / "client.pid")
    pgid = int(rc.with_suffix(".started").read_text().split()[0])
    p.db.update_task(tid, status=status)
    return rc, pgid


def _end_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _stopped_events(p):
    return p.db.q("SELECT * FROM events WHERE kind='detached_job_stopped'")


def test_a_cancelled_tasks_job_is_frozen_its_matching_broker_job_ended_and_its_locks_freed(env, tmp_path, monkeypatch):
    import pwd
    from ttp import jobstop, locks as lk
    from ttp.daemon import Daemon
    p = make(env)
    me = pwd.getpwuid(os.getuid()).pw_name
    broker = _stub_broker(tmp_path, _broker_jobs(me))
    monkeypatch.setenv("PATH", f"{broker}:{os.environ['PATH']}")
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="cancelled")
    try:
        slot = p.state / "locks" / "builddir.0.lock"
        assert not lk.any_free([slot]) and json.loads(slot.read_text())["job_rc"] == str(rc)
        Daemon(p.base).tend_pushes()   # the daemon's tick runs the sweep
        # Only the job whose owner and full command match the client: ended once, while the group was stopped.
        assert (broker / "kills").read_text().split("\n")[0] == "11 T", (broker / "kills").read_text()
        assert len((broker / "kills").read_text().splitlines()) == 1
        assert f"task #{tid} is cancelled" in rc.with_suffix(".lost").read_text()
        assert lk.any_free([slot]) and lk.any_free([lk.job_lock(rc)])
        assert not jobstop.members(pgid)
        (ev,) = _stopped_events(p)
        assert ev["task"] == tid and "loop" in ev["text"] and "11" in ev["text"] and "builddir.0.lock" in ev["text"]
        assert str(pgid) in ev["text"]
        # Idempotent: a second pass does nothing and logs nothing.
        assert jobstop.sweep(p) == [] and len(_stopped_events(p)) == 1
        assert len((broker / "kills").read_text().splitlines()) == 1
    finally:
        _end_group(pgid)


def test_a_failing_or_missing_broker_cli_never_blocks_the_stop(env, tmp_path, monkeypatch):
    import pwd
    from ttp import jobstop, locks as lk
    p = make(env)
    me = pwd.getpwuid(os.getuid()).pw_name
    broker = _stub_broker(tmp_path, _broker_jobs(me), fail=True)
    monkeypatch.setenv("PATH", f"{broker}:{os.environ['PATH']}")
    failed = p.db.add_task("failed", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, failed, broker, status="failed")
    try:
        (r,) = jobstop.sweep(p)
        assert "status -j` failed" in r["note"] and not (broker / "kills").exists()
        assert lk.any_free([lk.job_lock(rc)]) and not jobstop.members(pgid)
    finally:
        _end_group(pgid)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")   # no broker CLI at all
    gone = p.db.add_task("gone", "s", kind="work", tier="light", origin="user")
    rc2, pgid2 = _locked_job(p, gone, broker, name="loop2", status="failed")
    try:
        (r,) = jobstop.sweep(p)
        assert "not on PATH" in r["note"] and not (broker / "kills").exists()
        assert lk.any_free([lk.job_lock(rc2)]) and "is failed" in rc2.with_suffix(".lost").read_text()
    finally:
        _end_group(pgid2)


def test_a_broker_job_is_ended_only_on_an_exact_owner_and_command_match(env, tmp_path):
    from ttp import jobstop
    me = "builder"
    d = _stub_broker(tmp_path, [{"job_id": 1, "owner": me, "command": "sleep 60"},
                                {"job_id": 2, "owner": "other", "command": "sleep 60"},
                                {"job_id": 3, "owner": me, "command": "sleep 600"},
                                {"job_id": 4, "owner": me, "command": "sleep"}])
    proc = subprocess.Popen([str(d / "tt-device-mcp"), "run", "--owner", me, "--", "sleep", "60"], start_new_session=True)
    try:
        assert _wait_for_path(d / "client.pid")
        os.environ["PATH"], old = f"{d}:{os.environ['PATH']}", os.environ["PATH"]
        try:
            ids, note = jobstop.end_broker_jobs(jobstop.members(proc.pid))
        finally:
            os.environ["PATH"] = old
        assert ids == ["1"] and not note
        assert jobstop.end_broker_jobs([os.getpid()]) == ([], "")   # no client among them: nothing asked
    finally:
        _end_group(proc.pid)
        proc.wait()


def test_a_job_an_open_task_still_waits_on_is_not_stopped(env, tmp_path):
    from ttp import jobstop, locks as lk
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("cancelled", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="cancelled")
    try:
        other = p.db.add_task("waiter", "s", kind="work", tier="light", origin="user")
        p.db.update_task(other, status="queued", result=json.dumps(
            {"status": "waiting", "retry_when": f"ttp detach --check {rc}"}))
        assert jobstop.sweep(p) == [] and not lk.any_free([lk.job_lock(rc)])
        assert not rc.with_suffix(".lost").exists() and not _stopped_events(p)
        p.db.update_task(other, result=json.dumps({"status": "waiting", "retry_when": "true",
                                                   "on_pass": {"status": "done", "summary": f"see {rc}"}}))
        assert jobstop.sweep(p) == []
        p.db.update_task(other, status="done")
        assert len(jobstop.sweep(p)) == 1 and lk.any_free([lk.job_lock(rc)])
    finally:
        _end_group(pgid)


def test_a_restart_mid_stop_finishes_it_on_the_next_pass(env, tmp_path):
    from ttp import jobstop, locks as lk
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("cancelled", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="cancelled")
    try:
        os.killpg(pgid, signal.SIGSTOP)   # a daemon that froze the group and wrote why, then died
        rc.with_suffix(".lost").write_text(f"task #{tid} is cancelled\n")
        (r,) = jobstop.sweep(p)
        assert lk.any_free([lk.job_lock(rc)]) and not jobstop.members(pgid) and r["pgid"] == pgid
        assert jobstop.sweep(p) == [] and len(_stopped_events(p)) == 1
    finally:
        _end_group(pgid)


def test_detach_stop_is_allowed_for_ended_tasks_and_priority_one_resource_holders_only(env, tmp_path):
    from ttp import locks as lk
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("open", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="waiting")
    try:
        r = _ttp_run(p, "detach", "--stop", str(rc))
        assert r.returncode == 3 and r.stderr.count("\n") == 1 and "refused" in r.stderr, r.stderr
        low = p.db.add_task("low", "s", kind="work", tier="light", origin="user", labels=["resource:builddir"])
        r = _ttp_run(p, "detach", "--stop", str(rc), env={"TTP_TASK": str(low)})
        assert r.returncode == 3, r.stderr   # priority 3, not 1
        urgent = p.db.add_task("recover", "s", kind="work", tier="light", origin="user", priority=1,
                               labels=["resource:another"])
        r = _ttp_run(p, "detach", "--stop", str(rc), env={"TTP_TASK": str(urgent)})
        assert r.returncode == 3, r.stderr   # none of its resources is held by the job
        assert not lk.any_free([lk.job_lock(rc)])
        p.db.update_task(urgent, labels=json.dumps(["exclusive:builddir"]))
        r = _ttp_run(p, "detach", "--stop", str(rc), env={"TTP_TASK": str(urgent)})
        assert r.returncode == 0 and "stopped detached job loop" in r.stdout, r.stderr
        assert lk.any_free([lk.job_lock(rc)]) and f"priority-1 task #{urgent}" in rc.with_suffix(".lost").read_text()
        r = _ttp_run(p, "detach", "--stop", str(rc), env={"TTP_TASK": str(urgent)})
        assert r.returncode == 0 and "not running" in r.stdout and len(_stopped_events(p)) == 1
    finally:
        _end_group(pgid)
    done = p.db.add_task("done", "s", kind="work", tier="light", origin="user")
    rc2, pgid2 = _locked_job(p, done, broker, name="after", status="done")
    try:
        r = _ttp_run(p, "detach", "--stop", str(rc2))
        assert r.returncode == 0 and lk.any_free([lk.job_lock(rc2)]), r.stderr
    finally:
        _end_group(pgid2)


def test_the_daemon_reaps_the_wrappers_relaunch_started(env, tmp_path):
    from ttp import detach
    p = make(env)
    run_dir = p.runs / "reap"
    run_dir.mkdir(parents=True)
    rc = run_dir / "quick.rc"
    detach.save_resume(rc, run_dir / "quick.log", ["true"], str(tmp_path), {}, "boot-a")
    why, pid = detach.relaunch(rc, "boot-b")
    assert not why and pid > 1
    assert _wait_for_path(rc)
    stat = pathlib.Path(f"/proc/{pid}/stat")
    deadline = time.time() + 10
    while time.time() < deadline and stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] != "Z":
        time.sleep(0.05)
    assert stat.exists(), "the wrapper was reaped before the daemon's tick"
    detach.tend(p)
    assert not stat.exists(), "the relaunched wrapper stayed a zombie of the daemon"


def test_a_cancelled_tasks_detached_checks_from_an_earlier_run_are_stopped(env, tmp_path, monkeypatch, capsys,
                                                                         reap_tmp_processes):
    """An earlier run of a task started `ttp checks --detach`; once the task is cancelled the daemon's pass
    ends their group, they write checks.rc 143, one event says so, and a second pass does nothing."""
    from ttp import jobstop
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    tid = p.db.add_task("soak", "s", kind="code", tier="light", origin="user")
    later = p.runs / "later"
    later.mkdir(parents=True)
    for d in (run, later):
        p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
               (tid, "worker", "fake", time.time() - 60, "ok", str(d)))
    started = tmp_path / "started"
    pid = _detached_checks(run, capsys, "sh", "-c", f"touch {started}; sleep 60")
    try:
        assert _wait_for(started.exists)
        assert jobstop.sweep(p) == [], "the checks of an open task were stopped"
        p.db.x("UPDATE tasks SET status='cancelled' WHERE id=?", (tid,))
        stopped = jobstop.sweep(p)
        assert [r["job"] for r in stopped] == ["checks"] and _wait_for(lambda: _gone_pid(pid), 10)
        assert (run / "checks.rc").read_text().strip() == "143"
        assert len(_stopped_events(p)) == 1 and f"task #{tid}" in _stopped_events(p)[0]["text"]
        assert jobstop.sweep(p) == [] and len(_stopped_events(p)) == 1
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        reap_tmp_processes(tmp_path)


def test_a_failed_job_s_wake_carries_its_log_tail_and_first_error_line_bounded(env, monkeypatch, tmp_path):
    """A failed detached job wakes its task (no on_pass_cmd) with its log's last 20 lines and its
    first line matching Traceback/*Error/error/FAIL, cut to about 4,000 chars, control characters
    stripped; a job with no log gives one short line and no exception."""
    p = make(env)
    from ttp import daemon as dmod, detach
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    probe = _ended_jobs(tmp_path, bench=1, gone=3, fine=0)
    jobs = tmp_path / "jobs"
    lines = [f"step {i} ok \x1b[32mgreen\x1b[0m\x07" for i in range(200)]
    lines[150:150] = ["Traceback (most recent call last):", '  File "bench.py", line 3, in <module>',
                      "ModuleNotFoundError: No module named 'torch'"]
    lines += ["x" * 5000 for _ in range(30)]
    (jobs / "bench.log").write_text("\n".join(lines) + "\n")
    (jobs / "fine.log").write_text("all good\n")
    tid = _due_waiting_task(p, probe)
    p.db.update_task(tid, not_before=time.time() + 3600)
    _settle_probe(d, tid)
    res = json.loads(p.db.task(tid)["result"])
    woke = res["woke"]
    assert _ready(p, tid) and woke.startswith("detached job failed") and "bench: job finished rc=1" in woke, woke
    assert "bench.log (" in woke and "last lines:" in woke and "x" * 100 in woke and "x" * 400 not in woke, woke
    assert "first error line (line 151): Traceback (most recent call last):" in woke, woke
    assert f"gone.log: no log at {jobs / 'gone.log'}" in woke, woke
    assert "fine.log" not in woke and "\x1b" not in woke and "\x07" not in woke
    excerpt = detach.failed_logs(probe, p.root)
    assert len(excerpt) <= detach.LOG_EXCERPT_CHARS and woke.endswith(excerpt), woke
    # No line matches: it says which patterns were looked for.
    (jobs / "bench.log").write_text("step 1\nstep 2 aborted\n")
    assert "no line matches Traceback, *Error, error, ERROR or FAIL" in detach.failed_logs(probe, p.root)
    assert detach.first_error(jobs / "bench.log") is None
    # A probe with no local job, or one that only finished ok, adds nothing.
    assert detach.failed_logs("ttp detach --check --host box-a /r/a.rc") == ""
    assert detach.failed_logs(_ended_jobs(tmp_path, fine=0)) == ""


def test_result_file_walk_is_bounded_on_a_big_tree(tmp_path, monkeypatch):
    from ttp import obligations as ob
    for i in range(30):
        (tmp_path / f"d{i}").mkdir()
        for k in range(10):
            (tmp_path / f"d{i}" / f"f{k}.txt").write_text("x")
    (tmp_path / "d3" / "out.json").write_text("{}")
    monkeypatch.setattr(ob, "RESULT_FILES_MAX", 50)
    seen = []
    real = os.scandir
    monkeypatch.setattr(ob.os, "scandir", lambda d: (seen.append(d), real(d))[1])
    list(ob._glob_walk(str(tmp_path), "**/out.json"))
    assert len(seen) < 10   # stopped after about 50 entries, not all 30 folders


def test_result_file_patterns_still_find_files(tmp_path):
    import time as _t
    from ttp import obligations as ob
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "top.json").write_text("1")
    (tmp_path / "a" / "b" / "deep.json").write_text("1")
    (tmp_path / "a" / "skip.txt").write_text("1")
    rel = lambda pat: sorted(str(p.relative_to(tmp_path)) for p in ob._glob_walk(str(tmp_path), pat))
    assert rel("*.json") == ["top.json"]
    assert rel("a/*.txt") == ["a/skip.txt"]
    assert rel("**/*.json") == ["a/b/deep.json", "top.json"]
    now = _t.time()
    job = {"results": "**/deep.json", "cwd": str(tmp_path), "started": now - 60}
    assert ob._result_file(job, now + 1)[1] == "a/b/deep.json"
