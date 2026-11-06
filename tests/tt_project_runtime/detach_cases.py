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
    r = _ttp_run(p, "detach", "--remote", "box-m", "--dir", str(rdir), name, "--", "bash", "-c", script,
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
        env, tmp_path, f"echo step1; while [ ! -e {go} ]; do {_nap(0.1)}; done; echo step2; exit 5")
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


def test_detach_idle_ok_records_its_reason_and_sets_or_clears_it_on_a_running_job(env, tmp_path):
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    e = _lock_job_env(env, p, run_dir)
    r = _ttp_run(p, "detach", "api", "--idle-ok", "waits on a remote API", "--", *_until(tmp_path / "stop"), env=e)
    assert r.returncode == 0 and "idle by design: waits on a remote API" in r.stdout, r.stderr
    rc = (run_dir / "api.rc").resolve()
    assert _record(rc)["idle_ok"] == "waits on a remote API"
    # Removed on the running job, then set again by its .rc path, with the reason's whitespace folded.
    r = _ttp_run(p, "detach", "--idle-ok", "", "api", env=e)
    assert r.returncode == 0 and "idle_ok" not in _record(rc), r.stderr
    r = _ttp_run(p, "detach", "--idle-ok", "polls  a\ncontainer", str(rc), env=e)
    assert r.returncode == 0 and _record(rc)["idle_ok"] == "polls a container", r.stderr
    # Refused: no reason when starting, a remote job, a job no record names, a job that is over.
    r = _ttp_run(p, "detach", "x", "--idle-ok", " ", "--", "true", env=e)
    assert r.returncode != 0 and "needs the reason" in r.stderr and not (run_dir / "x.rc").exists()
    r = _ttp_run(p, "detach", "--remote", "nohost", "--idle-ok", "w", "y", "--", "true", env=e)
    assert r.returncode != 0 and "--idle-ok is for a local job" in r.stderr
    assert _ttp_run(p, "detach", "--idle-ok", "w", "other", env=e).returncode == 2
    (tmp_path / "stop").touch()
    assert _until_true(rc.exists)
    r = _ttp_run(p, "detach", "--idle-ok", "w", "api", env=e)
    assert r.returncode == 1 and "is over" in r.stderr and "idle_ok" in _record(rc)
    assert _record(rc)["idle_ok"] == "polls a container"
    assert "--idle-ok" in _ttp_run(p, "detach", "--help").stdout


def _leftover_slot(p, rc, res, tid):
    """Slot `res` of the project held by a real leftover process (no run in its environment, in a session
    of its own that the label names as the holder's group), its label naming detached job `rc` of task
    `tid` and a command pid that is gone: the hold an older sweep kept as a live group member's. Returns
    the leftover."""
    import fcntl
    from ttp import locks
    gone = subprocess.Popen(["true"])
    gone.wait()
    path = p.state / "locks" / f"{res}.0.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env = {k: v for k, v in os.environ.items() if not k.startswith("TTP_")}
        left = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], pass_fds=[f.fileno()],
                                start_new_session=True, env=env)
        f.truncate(0)
        f.write(json.dumps({"holder": f"task #{tid} (run 7)", "since": time.time(), "pid": gone.pid, "pid_start": "1",
                            "pgid": left.pid, "via": "command", "node": os.uname().nodename, "pidns": locks._pid_ns(),
                            "run_id": "7", "task_id": str(tid), "project": p.base.name, "job": rc.stem,
                            "job_rc": str(rc)}))
        f.flush()
    return left


def test_detach_reap_and_stop_free_the_slot_a_finished_jobs_leftover_holds_once_its_task_ended(env, tmp_path):
    """A job that wrote its .rc, no process of it left, a leftover still holding a lock slot labelled for it:
    `ttp detach --reap` and `--stop` keep the slot while the job's task is open and free it (stopping the
    leftover) once the task is cancelled."""
    from ttp import locks
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    tid = p.db.add_task("uses the rig", kind="code")
    rc = _detach_job(p, run_dir, env, "job", ["sh", "-c", "exit 0"])
    assert _until_true(lambda: rc.exists() and locks.any_free([locks.job_lock(rc)]))
    e = _lock_job_env(env, p, run_dir)
    lefts = [_leftover_slot(p, rc, "rig-z", tid), _leftover_slot(p, rc, "rig-y", tid)]
    try:
        r = _ttp_run(p, "detach", "--reap", "job", env=e)
        assert r.returncode == 0 and "rig-z.0.lock stays held: its job or its task is not over" in r.stdout, r.stdout
        assert all(x.poll() is None for x in lefts)
        p.db.x("UPDATE tasks SET status='cancelled' WHERE id=?", (tid,))
        r = _ttp_run(p, "detach", "--reap", "job", env=e)
        assert r.returncode == 0 and "freed the lock slots its leftovers held: rig-y.0.lock, rig-z.0.lock" in r.stdout, \
            (r.stdout, r.stderr)
        assert all(x.wait(timeout=20) is not None for x in lefts)
        assert locks.any_free([p.state / "locks" / "rig-z.0.lock"]) and locks.any_free([p.state / "locks" / "rig-y.0.lock"])
        ev = p.db.one("SELECT text FROM events WHERE kind='lock_released' ORDER BY id DESC LIMIT 1")
        assert ev and "is cancelled" in ev["text"], ev
        lefts = [_leftover_slot(p, rc, "rig-x", tid)]
        r = _ttp_run(p, "detach", "--stop", str(rc), env=e)
        assert r.returncode == 0 and "not running" in r.stdout and "rig-x.0.lock" in r.stdout, (r.stdout, r.stderr)
        assert lefts[0].wait(timeout=20) is not None and locks.any_free([p.state / "locks" / "rig-x.0.lock"])
    finally:
        for x in lefts:
            if x.poll() is None:
                x.kill()
                x.wait()


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


def _locked_job(p, tid, broker, name="loop", status="running", lock="builddir"):
    """Task tid's run started `ttp detach --lock builddir <name>`, whose loop runs a broker client;
    returns (rc, pgid) once the client runs."""
    run_dir = p.runs / f"stop-{tid}"
    run_dir.mkdir(parents=True, exist_ok=True)
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 60, "ok", str(run_dir)))
    (broker / "client.pid").unlink(missing_ok=True)
    loop = f"while :; do {shlex.quote(str(broker / 'tt-device-mcp'))} run -- sleep 60; done"
    r = _ttp_run(p, "detach", "--lock", lock, name, "--", "sh", "-c", loop, env={"TTP_RUN_DIR": str(run_dir)})
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


def _signal_rows(p):
    f = p.state / "job-signals.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def test_each_signal_a_stop_sends_a_detached_job_is_logged_in_its_log_and_the_signal_stream(env, tmp_path, monkeypatch):
    """The sweep stops an ended task's job: its SIGSTOP and SIGKILL each add a line to the job's own log
    and a row to state/job-signals.jsonl naming the job, group, signal, code path, why and sender."""
    from ttp import jobstop
    p = make(env)
    broker = _stub_broker(tmp_path)
    monkeypatch.setenv("PATH", f"{broker}:{os.environ['PATH']}")
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="cancelled")
    try:
        (r,) = jobstop.sweep(p)
        log = rc.with_suffix(".log").read_text()
        for sig in ("SIGSTOP", "SIGKILL"):
            assert (f"{sig} to group {pgid} of job loop (task #{tid}) from jobstop pid {os.getpid()}" in log
                    and f"(jobstop.sweep): task #{tid} is cancelled" in log), log
        rows = _signal_rows(p)
        assert [(x["signal"], x["job"], x["pid"], x["group"], x["reason"], x["by"], x["by_pid"], x["task"])
                for x in rows] == [(s, "loop", pgid, True, "jobstop.sweep", "jobstop", os.getpid(), tid)
                                   for s in ("SIGSTOP", "SIGKILL")], rows
        assert all(x["why"] == f"task #{tid} is cancelled" and x["ts"] > 0 for x in rows)
        assert f"SIGSTOP then SIGKILL to group {pgid} from jobstop pid {os.getpid()} (jobstop.sweep)" in r["text"]
    finally:
        _end_group(pgid)


def test_a_daemon_paths_detach_stop_logs_its_code_path_and_sender(env, tmp_path):
    """detach.stop, as heal's fix timeout and the reboot relaunch call it, logs its SIGKILL with the code
    path it names, in the project's signal stream wherever under state its .rc sits (heal's fix folder,
    a subfolder of a run); a job already gone logs nothing."""
    from ttp import detach, heal
    p = make(env)
    fix = heal.fix_dir(p, "disk check")   # as heal.start_fix lays it out
    fix.mkdir(parents=True)
    rc = fix / "fix.rc"
    (fix / "detached.json").write_text(json.dumps([{"rc": str(rc), "pid": 1, "start": time.time()}]))
    nested = p.runs / "r1" / "sub" / "job.rc"
    nested.parent.mkdir(parents=True)
    for other in (nested, p.runs / "r1" / "job.rc"):
        assert detach.signals_file(other) == p.state / "job-signals.jsonl"
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        detach.stop(rc, proc.pid, "ran past fix_timeout_s (5 s)", reason="heal.fix_timeout", by="daemon")
        assert proc.wait(timeout=10) == -signal.SIGKILL
    finally:
        _end_group(proc.pid)
    assert "ran past fix_timeout_s" in rc.with_suffix(".lost").read_text()
    log = rc.with_suffix(".log").read_text()
    assert f"SIGKILL to group {proc.pid} of job fix from daemon pid {os.getpid()}" in log, log
    assert "(heal.fix_timeout): ran past fix_timeout_s (5 s)" in log
    (row,) = _signal_rows(p)
    assert (row["signal"], row["reason"], row["by"], row["pid"], row["rc"]) == (
        "SIGKILL", "heal.fix_timeout", "daemon", proc.pid, str(rc))
    detach.stop(rc, proc.pid, "again", reason="heal.fix_timeout")   # gone: nothing more logged
    assert len(_signal_rows(p)) == 1 and not (fix / "job-signals.jsonl").exists()


def test_a_waiting_tasks_job_outlives_the_run_that_started_it_and_is_never_signalled(env, tmp_path, monkeypatch):
    """By design: the run that started a job has ended, but its task waits on the job, so the daemon's
    sweep leaves it running, sends it nothing and logs no signal."""
    from ttp import jobstop, locks as lk
    p = make(env)
    broker = _stub_broker(tmp_path)
    monkeypatch.setenv("PATH", f"{broker}:{os.environ['PATH']}")
    tid = p.db.add_task("gate", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="queued")
    try:
        p.db.update_task(tid, result=json.dumps({"status": "waiting", "retry_when": f"ttp detach --check {rc}"}))
        assert p.db.one("SELECT status FROM runs WHERE task=?", (tid,))["status"] == "ok"   # its run ended
        assert jobstop.sweep(p) == [] and not lk.any_free([lk.job_lock(rc)])
        assert jobstop.members(pgid) and not rc.with_suffix(".lost").exists() and not rc.exists()
        assert _signal_rows(p) == [] and "SIG" not in rc.with_suffix(".log").read_text()
    finally:
        _end_group(pgid)


def test_newer_detached_checks_log_the_signal_that_replaced_the_older_ones(env, tmp_path, monkeypatch, capsys,
                                                                        reap_tmp_processes):
    """A second `ttp checks --detach` in the same run ends the first: the SIGTERM goes into checks.log
    and the job-signal stream, naming the replacement as the code path."""
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    started = tmp_path / "started"
    first = _detached_checks(run, capsys, "sh", "-c", f"touch {started}; sleep 60")
    second = None
    try:
        assert _wait_for(started.exists)
        second = _detached_checks(run, capsys, "true")
        assert _wait_for(lambda: _gone_pid(first), 20)
        log = (run / "checks.log").read_text()
        assert f"SIGTERM to group {first} of job checks from checks pid {os.getpid()}" in log, log
        assert "(ttp checks --detach (replaced))" in log
        assert [(r["signal"], r["pid"], r["by"]) for r in _signal_rows(p)][:1] == [("SIGTERM", first, "checks")]
    finally:
        for x in (first, second):
            try:
                if x:
                    os.killpg(x, signal.SIGKILL)
            except OSError:
                pass
        reap_tmp_processes(tmp_path)


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


def _later_run(p, tid):
    """A later run of task tid, after the one that started its job."""
    d = p.runs / f"later-{tid}"
    d.mkdir(parents=True, exist_ok=True)
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 10, "ok", str(d)))


def test_handed_off_1_a_done_tasks_live_job_from_an_earlier_run_is_stopped(env, tmp_path):
    from ttp import jobstop, locks as lk
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("done", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="done")
    try:
        _later_run(p, tid)
        p.db.update_task(tid, result=json.dumps({"status": "done", "summary": "finished"}))
        (r,) = jobstop.sweep(p)
        assert lk.any_free([lk.job_lock(rc)]) and not jobstop.members(pgid) and r["pgid"] == pgid
        assert f"task #{tid} handed off done" in rc.with_suffix(".lost").read_text()
        assert jobstop.sweep(p) == [] and len(_stopped_events(p)) == 1
    finally:
        _end_group(pgid)


def test_handed_off_2_a_job_the_done_hand_off_names_is_kept(env, tmp_path):
    from ttp import jobstop, locks as lk
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("done", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="done")
    rc2, pgid2 = _locked_job(p, tid, broker, name="loop2", status="done", lock="builddir2")
    try:
        # loop2 is unnamed (loop is a prefix of it, never a match): the first pass stops only it.
        stopped = []
        for key, val in (("retry_when", f"ttp detach --check {rc.parent}/loop"), ("on_pass_cmd", f"cat {rc}"),
                         ("next_step", f"tail {rc.parent.name}/loop.rc"), ("on_pass", {"summary": f"{rc.parent.name}/loop"})):
            p.db.update_task(tid, result=json.dumps({"status": "done", key: val}))
            stopped += [r["pgid"] for r in jobstop.sweep(p)]
            assert not lk.any_free([lk.job_lock(rc)]), key
        assert stopped == [pgid2] and lk.any_free([lk.job_lock(rc2)])
    finally:
        _end_group(pgid)
        _end_group(pgid2)


def test_handed_off_3_a_done_tasks_job_an_open_task_waits_on_is_kept(env, tmp_path):
    from ttp import jobstop, locks as lk
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("done", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="done")
    try:
        p.db.update_task(tid, result=json.dumps({"status": "done", "summary": "finished"}))
        other = p.db.add_task("waiter", "s", kind="work", tier="light", origin="user")
        p.db.update_task(other, status="queued", result=json.dumps({"status": "waiting", "retry_when": f"test -e {rc}"}))
        assert jobstop.sweep(p) == [] and not lk.any_free([lk.job_lock(rc)])
        p.db.update_task(other, status="cancelled")
        assert len(jobstop.sweep(p)) == 1 and lk.any_free([lk.job_lock(rc)])
    finally:
        _end_group(pgid)


def test_handed_off_4_the_run_slash_name_form_counts_as_a_waiter(env, tmp_path):
    from ttp import jobstop, locks as lk
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("cancelled", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="cancelled")
    try:
        other = p.db.add_task("waiter", "s", kind="work", tier="light", origin="user")
        p.db.update_task(other, status="queued", result=json.dumps(
            {"status": "waiting", "retry_when": f"ttp detach --check {rc.parent.name}/{rc.stem}"}))
        assert jobstop.waited_on(p, rc) == other
        assert jobstop.sweep(p) == [] and not lk.any_free([lk.job_lock(rc)])
        # A longer name, another run of the same name or a log file is not this job.
        for text in (f"{rc.parent.name}/{rc.stem}2", f"x{rc.parent.name}/{rc.stem}", f"{rc.parent.name}/{rc.stem}.log"):
            p.db.update_task(other, result=json.dumps({"status": "waiting", "retry_when": f"ttp detach --check {text}"}))
            assert jobstop.waited_on(p, rc) is None, text
        assert len(jobstop.sweep(p)) == 1 and lk.any_free([lk.job_lock(rc)])
    finally:
        _end_group(pgid)


def test_handed_off_4b_a_file_of_another_kind_counts_only_by_its_full_name():
    """A push marker (<dir>/<name>.json) is named by its full path or <dir>/<name>.json, never its bare stem."""
    from ttp import jobstop
    marker = pathlib.Path("/x/state/pushes/t1-20261029-033950-5.json")
    for text in (f"ttp push --result {marker}", "pushes/t1-20261029-033950-5.json", f'"{marker}"'):
        assert jobstop.names_job({"retry_when": text}, marker), text
    for text in ("pushes/t1-20261029-033950-5", "pushes/t1-20261029-033950-5.json2", "pushes/t1-20261029-033950-50.json",
                 "xpushes/t1-20261029-033950-5.json"):
        assert not jobstop.names_job({"retry_when": text}, marker), text
    rc = pathlib.Path("/x/runs/8605/build.rc")
    for text in ("8605/build.rc", "8605/build", "ttp detach --check /x/runs/8605/build;"):
        assert jobstop.names_job({"on_pass_cmd": text}, rc), text
    for text in ("8605/build.rc2", "8605/build.log", "8605/builder", "18605/build"):
        assert not jobstop.names_job({"on_pass_cmd": text}, rc), text


def _no_signal_and_alive(p, pgid, rc):
    """The sweep sent no signal: no signal row, the group's processes run (not stopped) and hold its lock."""
    from ttp import jobstop, locks as lk
    assert _signal_rows(p) == [] and _stopped_events(p) == []
    alive = jobstop.members(pgid)
    assert alive and all(pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "T"
                         for pid in alive)
    assert not lk.any_free([lk.job_lock(rc)]) and not rc.with_suffix(".lost").exists()


def test_handed_off_5_a_blocked_task_keeps_every_job_whatever_its_hand_off_names(env, tmp_path):
    """Regression: a task that handed off blocked while its retry_when waits on its jobs' .rc files (in a
    form names_job does not read, such as a glob), or that names none of them, still owns live work: the
    sweep sends its jobs no signal, across all its runs."""
    from ttp import jobstop
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("blocked", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="blocked")
    rc2, pgid2 = _locked_job(p, tid, broker, name="other", status="blocked", lock="builddir2")
    try:
        _later_run(p, tid)
        for res in ({"status": "blocked", "question": "which?", "retry_when": f"ls {rc.parent}/*.rc"},
                    {"status": "blocked", "question": "which?", "retry_when": f"ttp detach --check {rc} {rc2}"},
                    {"status": "blocked", "question": "which?"}):
            p.db.update_task(tid, result=json.dumps(res))
            assert jobstop.sweep(p) == [], res
        _no_signal_and_alive(p, pgid, rc)
        _no_signal_and_alive(p, pgid2, rc2)
    finally:
        _end_group(pgid)
        _end_group(pgid2)


def test_handed_off_6_a_blocked_hand_off_with_live_jobs_is_recorded_waiting(env, tmp_path):
    """A blocked hand-off while the task's own detached jobs run is recorded as waiting: it keeps its own
    retry_when, else waits on those jobs (job_probe), wakes at standard, and the sweep leaves the jobs."""
    from ttp import jobstop, locks as lk
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("blocked", "s", kind="work", tier="standard", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="running")
    try:
        d = Daemon(p.base)
        for given in (f"ls {rc.parent}/*.rc", None):
            p.db.update_task(tid, status="running")
            res = {"status": "blocked", "summary": "needs a pick", "question": "which?"}
            if given:
                res["retry_when"] = given
            (rc.parent / "result.json").write_text(json.dumps(res))
            d._finish_worker({"task": tid, "note": None, "dir": str(rc.parent)}, RunUsage(final_text="bye"), "ok",
                             rc.parent)
            t = p.db.task(tid)
            got = json.loads(t["result"])
            assert t["status"] == "queued" and got["status"] == "waiting", (given, t["status"], got)
            assert got["retry_when"] == (given or lk.job_probe([rc], got["retry_when"].split(" detach")[0]))
            assert got["question"] == "which?" and got["wake_tier"] == "standard" and got["blocked_while_jobs_run"]
            assert jobstop.sweep(p) == []
        _no_signal_and_alive(p, pgid, rc)
        # Once its jobs ended, a blocked hand-off is blocked.
        _end_group(pgid)
        assert _wait_for(lambda: not jobstop.running(rc))
        p.db.update_task(tid, status="running")
        (rc.parent / "result.json").write_text(json.dumps({"status": "blocked", "summary": "needs a pick"}))
        d._finish_worker({"task": tid, "note": None, "dir": str(rc.parent)}, RunUsage(final_text="bye"), "ok", rc.parent)
        assert p.db.task(tid)["status"] == "blocked"
    finally:
        _end_group(pgid)


def test_handed_off_7_a_waiting_queued_or_running_task_keeps_its_jobs(env, tmp_path):
    """A task still alive keeps its jobs whatever its stored hand-off text says (even a stale done)."""
    from ttp import jobstop
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("alive", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="queued")
    try:
        _later_run(p, tid)
        for state, res in (("queued", {"status": "waiting", "retry_when": "false"}),
                           ("queued", {"status": "done", "summary": "stale"}),
                           ("running", {"status": "blocked", "question": "which?"}),
                           ("blocked", {"status": "done", "summary": "stale"})):
            p.db.update_task(tid, status=state, result=json.dumps(res))
            assert jobstop.sweep(p) == [], (state, res)
        _no_signal_and_alive(p, pgid, rc)
    finally:
        _end_group(pgid)


def test_handed_off_8_a_cancelled_or_done_tasks_jobs_are_still_reaped(env, tmp_path):
    from ttp import jobstop, locks as lk
    p = make(env)
    broker = _stub_broker(tmp_path)
    for state in ("cancelled", "done"):
        tid = p.db.add_task(state, "s", kind="work", tier="light", origin="user")
        rc, pgid = _locked_job(p, tid, broker, status=state, lock=f"build-{state}")
        try:
            p.db.update_task(tid, result=json.dumps({"status": "done" if state == "done" else "waiting",
                                                     "summary": "x"}))
            (r,) = jobstop.sweep(p)
            assert r["pgid"] == pgid and not jobstop.members(pgid) and lk.any_free([lk.job_lock(rc)]), state
            assert f"task #{tid}" in rc.with_suffix(".lost").read_text()
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


def _add_run(p, tid, name):
    """A run of task tid (its own run dir); its id."""
    d = p.runs / name
    d.mkdir(parents=True, exist_ok=True)
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "running", str(d)))
    return p.db.one("SELECT id FROM runs WHERE dir=?", (str(d),))["id"]


def _as(tid, run):
    return {"TTP_TASK": str(tid), "TTP_RUN_ID": str(run), "TTP_PIDNS": "", "TTP_CGROUP": ""}


def test_detach_stop_lets_the_owning_task_stop_its_own_job_from_an_earlier_run_and_records_why(env, tmp_path):
    """(a) An open task stops the job an earlier run of it started: allowed, and .lost and the stop event name
    who stopped it, the run that started it, the caller's run and the reason (--why)."""
    from ttp import locks as lk
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="running")
    try:
        started = p.db.one("SELECT id FROM runs WHERE dir=?", (str(rc.parent),))["id"]
        now = _add_run(p, tid, "soak-now")
        r = _ttp_run(p, "detach", "--stop", str(rc), "--why", "the driver stalled", env=_as(tid, now))
        assert r.returncode == 0 and "stopped detached job loop" in r.stdout, r.stderr
        assert lk.any_free([lk.job_lock(rc)])
        lost = rc.with_suffix(".lost").read_text()
        assert f"stopped by owning task #{tid} (run {now}) from an earlier run, run {started}" in lost, lost
        assert "the driver stalled" in lost, lost
        (ev,) = _stopped_events(p)
        data = json.loads(ev["data"])
        assert (data["stopped_by"], data["job_run"], data["caller_run"]) == (tid, started, now), data
        assert "the driver stalled" in data["why"] and ev["task"] == tid
        assert p.db.task(tid)["status"] == "running", "the stop changed the task"
    finally:
        _end_group(pgid)
    # No --why: the default reason alone.
    rc2, pgid2 = _locked_job(p, tid, broker, name="again", status="running")
    try:
        started2 = p.db.one("SELECT MAX(id) id FROM runs WHERE dir=?", (str(rc2.parent),))["id"]
        r = _ttp_run(p, "detach", "--stop", str(rc2), env=_as(tid, now))
        assert r.returncode == 0, r.stderr
        assert rc2.with_suffix(".lost").read_text().strip() == \
            f"stopped by owning task #{tid} (run {now}) from an earlier run, run {started2} via `ttp detach --stop`"
    finally:
        _end_group(pgid2)
    r = _ttp_run(p, "detach", "--why", "x", "loop", "--", "true", env={"TTP_RUN_DIR": str(rc.parent)})
    assert r.returncode != 0 and "--why goes with --stop" in r.stderr, r.stderr


def _task_job(p, tid, run_name, name="nap"):
    """Task tid's run `run_name` detached `sleep 60` as a worker would (TTP_TASK, TTP_RUN_ID, TTP_RUN_DIR);
    (rc, pgid, run id)."""
    run = _add_run(p, tid, run_name)
    d = p.runs / run_name
    r = _ttp_run(p, "detach", name, "--", "sleep", "60", env={"TTP_RUN_DIR": str(d), **_as(tid, run)})
    assert r.returncode == 0, r.stderr
    rc = (d / f"{name}.rc").resolve()
    assert _wait_for_path(rc.with_suffix(".started"))
    return rc, int(rc.with_suffix(".started").read_text().split()[0]), run


def test_detach_records_its_owner_and_a_later_run_lists_and_stops_it_by_name(env, tmp_path):
    """A job's registry entry names its project, task and run; a later run of the same task lists it
    (`ttp detach --list`) and stops it by its bare name; another task neither sees it by name nor stops it."""
    p = make(env)
    tid = p.db.add_task("gate", "s", kind="work", tier="light", origin="user")
    other = p.db.add_task("other", "s", kind="work", tier="light", origin="user")
    rc, pgid, first = _task_job(p, tid, "gate-1")
    try:
        (entry,) = json.loads((rc.parent / "detached.json").read_text())
        assert (entry["task"], entry["run"], Path(entry["project"])) == (tid, first, p.base), entry
        later, theirs = _add_run(p, tid, "gate-2"), _add_run(p, other, "other-1")
        r = _ttp_run(p, "detach", "--list", env={"TTP_RUN_DIR": str(p.runs / "gate-2"), **_as(tid, later)})
        assert r.returncode == 0 and f"run {first}  nap  running  pid {pgid}  {rc}" in r.stdout, r.stdout + r.stderr
        r = _ttp_run(p, "detach", "--list", env={"TTP_RUN_DIR": str(p.runs / "other-1"), **_as(other, theirs)})
        assert r.returncode == 0 and str(rc) not in r.stdout, r.stdout
        r = _ttp_run(p, "detach", "--list", env=_as(tid, theirs))
        assert r.returncode == 2, "a run id of another task proves nothing"
        r = _ttp_run(p, "detach", "--stop", "nap", env={"TTP_RUN_DIR": str(p.runs / "other-1"), **_as(other, theirs)})
        assert r.returncode == 2 and os.getpgid(pgid) == pgid, r.stdout + r.stderr
        r = _ttp_run(p, "detach", "--stop", str(rc), env=_as(other, theirs))
        assert r.returncode == 3 and "refused" in r.stderr and os.getpgid(pgid) == pgid, r.stderr
        r = _ttp_run(p, "detach", "--stop", "nap", env={"TTP_RUN_DIR": str(p.runs / "gate-2"), **_as(tid, later)})
        assert r.returncode == 0 and "stopped detached job nap" in r.stdout, r.stdout + r.stderr
        assert f"stopped by owning task #{tid} (run {later}) from an earlier run, run {first}" in \
            rc.with_suffix(".lost").read_text()
    finally:
        _end_group(pgid)


def test_detach_stop_lets_the_owning_task_stop_a_job_of_its_current_run(env, tmp_path):
    """(b) The run that started the job stops it (by name, from $TTP_RUN_DIR)."""
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="running")
    try:
        run = p.db.one("SELECT id FROM runs WHERE dir=?", (str(rc.parent),))["id"]
        r = _ttp_run(p, "detach", "--stop", "loop", env={**_as(tid, run), "TTP_RUN_DIR": str(rc.parent)})
        assert r.returncode == 0 and "stopped detached job loop" in r.stdout, r.stderr
        assert f"(run {run}) from its current run" in rc.with_suffix(".lost").read_text()
    finally:
        _end_group(pgid)


def test_an_owners_stop_writes_the_rc_so_a_reader_of_the_rc_alone_sees_a_stop_not_a_death(env, tmp_path):
    """`ttp detach --stop` kills the wrapper, which so never writes an exit code: the stop writes <name>.rc
    itself (137, an integer every reader parses) beside the .lost, and the readers say stopped on purpose,
    never gone without an exit code. No temp file is left beside it."""
    from ttp import detach
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="running")
    try:
        run = p.db.one("SELECT id FROM runs WHERE dir=?", (str(rc.parent),))["id"]
        r = _ttp_run(p, "detach", "--stop", str(rc), "--why", "superseded", env=_as(tid, run))
        assert r.returncode == 0, r.stderr
        assert int(rc.read_text()) == detach.STOP_RC == 137
        assert "superseded" in rc.with_suffix(".lost").read_text()
        assert not [x for x in rc.parent.iterdir() if x.name.endswith(".tmp")]
        assert detach.state(rc) == ("ended", "137") and "superseded" in detach.stopped(rc)
        c = _ttp_run(p, "detach", "--check", str(rc))
        assert c.returncode == detach.FAILED_RC, c.stdout + c.stderr
        assert "loop: stopped on purpose rc=137" in c.stdout and "superseded" in c.stdout, c.stdout
        assert "gone without an exit code" not in c.stdout, c.stdout
        assert "stopped on purpose" in detach.failed_why(f"ttp detach --check {rc}", 4)
        e = detach.close(rc)
        assert (e["status"], e["exit"], e["stopped"]) == ("ended", 137, True) and "superseded" in e["why"], e
    finally:
        _end_group(pgid)


def test_a_stop_never_overwrites_an_exit_code_the_job_wrote_itself(env, tmp_path):
    """A job that wrote its own .rc before the stop's record keeps it (and gets no .lost); a natural exit
    of 137 with no .lost, or a job not stopped (no .rc yet), never reads as stopped; a path outside the
    project's state and run folders gets no file."""
    from ttp import detach
    p = make(env)
    d = p.runs / "own"
    d.mkdir(parents=True)
    rc = d / "job.rc"
    rc.write_text("0\n")
    assert detach.record_stop(rc, "stopped by owning task #1") is False
    assert rc.read_text() == "0\n" and not detach.lost(rc).exists() and detach.stopped(rc) is None
    nat = d / "nat.rc"
    nat.write_text("137\n")
    assert detach.stopped(nat) is None   # its own SIGKILL exit: no stop's reason beside it
    assert detach.stopped(d / "live.rc") is None and not (d / "live.rc").exists()
    assert detach.record_stop(d / "fresh.rc", "spend cap reached") is True
    assert (d / "fresh.rc").read_text() == "137\n" and detach.stopped(d / "fresh.rc") == "spend cap reached"
    assert detach.record_stop(d / "fresh.rc", "again") is False and detach.stopped(d / "fresh.rc") == "spend cap reached"
    out = tmp_path / "checkout"
    out.mkdir()
    assert detach.record_stop(out / "job.rc", "x") is False and not list(out.iterdir())


def test_detach_stop_still_refuses_another_task_or_a_caller_without_a_task_identity(env, tmp_path):
    """(c) Only the job's own task, with a run the project records for it, may stop a job of an open task:
    another task, no task, a task id without a run, a run of another task, or another task claiming the
    owner's run are refused with the same message as before, and the job runs on."""
    from ttp import jobstop, locks as lk
    p = make(env)
    broker = _stub_broker(tmp_path)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    other = p.db.add_task("other", "s", kind="work", tier="light", origin="user")
    rc, pgid = _locked_job(p, tid, broker, status="running")
    try:
        mine = p.db.one("SELECT id FROM runs WHERE dir=?", (str(rc.parent),))["id"]
        theirs = _add_run(p, other, "other-now")
        refused = (f"refused: its task #{tid} is running; only a job of an ended task, or one holding a resource "
                   "of the calling open priority-1 task, may be stopped")
        for e in (_as(other, theirs), _as("", ""), _as(tid, ""), _as(tid, theirs), _as(other, mine),
                  _as(tid, "999999"), _as(f" {tid}", mine)):
            r = _ttp_run(p, "detach", "--stop", str(rc), env=e)
            assert r.returncode == 3 and r.stderr.strip() == f"ttp detach --stop: {refused}", (e, r.stderr)
        assert not lk.any_free([lk.job_lock(rc)]) and not rc.with_suffix(".lost").exists()
        # The queued path (a sandboxed caller) checks the same identity again.
        jobstop.request(p, rc, str(other), str(mine), "forged")
        assert jobstop.serve_requests(p) == [] and not lk.any_free([lk.job_lock(rc)])
        jobstop.request(p, rc, str(tid), str(mine), "queued by its own run")
        (out,) = jobstop.serve_requests(p)
        assert out["stopped_by"] == tid and out["caller_run"] == mine and lk.any_free([lk.job_lock(rc)])
        assert "queued by its own run" in rc.with_suffix(".lost").read_text()
    finally:
        _end_group(pgid)


def test_a_job_its_own_task_stopped_is_not_relaunched_at_boot(env, tmp_path):
    """(d) A resumable job its task stopped is never started again by the reboot relaunch: the stop wrote
    its .rc, so the reboot found nothing of it to wake for, and the task's own probe reads it ended
    (detach.FAILED_RC, stopped on purpose) and wakes it."""
    from ttp import detach
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    run_dir = p.runs / f"jobs-{tid}"
    run_dir.mkdir(parents=True)
    run = _add_run(p, tid, f"jobs-{tid}")
    counter = tmp_path / "runs"
    r = _ttp_run(p, "detach", "--resumable", "job", "--", "sh", "-c", f"echo run >>{counter}; sleep 60",
                 env={"TTP_RUN_DIR": str(run_dir)})
    assert r.returncode == 0, r.stderr
    rc = (run_dir / "job.rc").resolve()
    assert _wait_for_path(counter)
    pgid = int(rc.with_suffix(".started").read_text().split()[0])
    try:
        p.db.update_task(tid, status="running")
        r = _ttp_run(p, "detach", "--stop", str(rc), env=_as(tid, run))
        assert r.returncode == 0, r.stderr
        assert json.loads(detach.resume_file(rc).read_text())["stopped"]
        assert rc.read_text().strip() == str(detach.STOP_RC)   # the stop's own end record
        m = json.loads(detach.resume_file(rc).read_text())
        detach.resume_file(rc).write_text(json.dumps({**m, "boot": "an-earlier-boot"}))
        reg = run_dir / "detached.json"
        reg.write_text(json.dumps([{**j, "boot": "an-earlier-boot"} for j in json.loads(reg.read_text())]))
        p.db.update_task(tid, status="queued", not_before=time.time() + 3600, result=json.dumps(
            {"status": "waiting", "summary": "job running", "retry_after_s": 3600,
             "retry_when": f"ttp detach --check {rc}", "waiting_since": time.time() - 900}))
        _boot_daemon(p)
        assert not _woken(p, tid) and not _job_relaunches(p, tid)
        c = _ttp_run(p, "detach", "--check", str(rc))
        assert c.returncode == detach.FAILED_RC and "stopped on purpose" in c.stdout, c.stdout
        assert detach.relaunch(rc, "a-later-boot")[0].startswith("it was stopped")
        time.sleep(0.3)
        assert counter.read_text().split() == ["run"], "the stopped job ran again"
    finally:
        _end_group(pgid)


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
        # The SIGTERM is named in the checks' own log and the project's job-signal stream, with its sender.
        log = (run / "checks.log").read_text()
        assert f"SIGTERM to group {pid} of job checks (task #{tid}) from jobstop pid {os.getpid()}" in log, log
        assert "(jobstop.end_checks)" in log
        rows = _signal_rows(p)
        assert [(r["signal"], r["reason"], r["pid"], r["by_pid"]) for r in rows][:1] == [
            ("SIGTERM", "jobstop.end_checks", pid, os.getpid())], rows
        assert f"SIGTERM to group {pid} from jobstop pid {os.getpid()}" in _stopped_events(p)[0]["text"]
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


def _scan_count(ob, monkeypatch):
    seen = []
    real = os.scandir
    monkeypatch.setattr(ob.os, "scandir", lambda d: (seen.append(d), real(d))[1])
    return seen


def test_result_file_walk_skips_folders_a_prefixed_glob_cannot_match(tmp_path, monkeypatch):
    from ttp import obligations as ob
    for i in range(10):
        (tmp_path / f"out{i}").mkdir()
        (tmp_path / f"out{i}" / "res.json").write_text("{}")
    for i in range(30):
        (tmp_path / f"big{i}").mkdir()
        for k in range(1000):
            (tmp_path / f"big{i}" / f"f{k}.txt").write_text("")
    seen = _scan_count(ob, monkeypatch)
    assert [p.name for p in ob._glob_walk(str(tmp_path), "out3/*.json")] == ["res.json"]
    assert seen == [str(tmp_path / "out3")]   # the literal prefix is joined, not scanned for
    seen.clear()
    # A wildcard prefix still prunes: only the out* folders are entered, never big*.
    got = sorted(p.parent.name for p in ob._glob_walk(str(tmp_path), "out*/res.json"))
    assert got == [f"out{i}" for i in range(10)]
    assert not any("big" in d for d in seen), seen


def test_result_file_deep_glob_under_a_literal_folder_never_scans_siblings(tmp_path, monkeypatch):
    from ttp import obligations as ob
    (tmp_path / "out" / "a" / "b").mkdir(parents=True)
    (tmp_path / "out" / "top.json").write_text("1")
    (tmp_path / "out" / "a" / "b" / "deep.json").write_text("1")
    for i in range(20):
        (tmp_path / "build" / f"d{i}").mkdir(parents=True)
        (tmp_path / "build" / f"d{i}" / "x.json").write_text("1")
    seen = _scan_count(ob, monkeypatch)
    rel = sorted(str(p.relative_to(tmp_path)) for p in ob._glob_walk(str(tmp_path), "out/**/*.json"))
    assert rel == ["out/a/b/deep.json", "out/top.json"]
    assert all(d.startswith(str(tmp_path / "out")) for d in seen), seen


def test_result_file_glob_resolves_literal_dotdot_and_whole_paths(tmp_path):
    import time as _t
    from ttp import obligations as ob
    (tmp_path / "res").mkdir()
    (tmp_path / "res" / "r.json").write_text("1")
    (tmp_path / "job").mkdir()
    job = str(tmp_path / "job")
    assert [p.name for p in ob._glob_walk(job, "../res/*.json")] == ["r.json"]
    assert [p.name for p in ob._glob_walk(job, "../res/r.json")] == ["r.json"]
    assert list(ob._glob_walk(job, "../res/none.json")) == []
    assert list(ob._glob_walk(job, "*/../res/r.json")) == []   # '..' after a wildcard never matches
    now = _t.time()
    got = ob._result_file({"results": "../res/*.json", "cwd": job, "started": now - 60}, now + 1)
    assert got[1] == "../res/r.json"


# `ttp detach --spend-cmd/--spend-cap`: the daemon's model-free spend cap (spendcap.py). Every process
# signalled is a job this test started.

def _spend_run(p, tid):
    run_dir = p.runs / f"spend-{tid}"
    run_dir.mkdir(parents=True, exist_ok=True)
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 60, "running", str(run_dir)))
    return run_dir


def _spend_job(p, run_dir, name, *opts):
    """Start `ttp detach [opts] <name> -- sleep 60` in run_dir; its (rc, pgid, stdout)."""
    r = _ttp_run(p, "detach", *opts, name, "--", "sleep", "60", env={"TTP_RUN_DIR": str(run_dir)})
    assert r.returncode == 0, r.stderr
    rc = (run_dir / f"{name}.rc").resolve()
    assert _wait_for_path(rc.with_suffix(".started"))
    return rc, int(rc.with_suffix(".started").read_text().split()[0]), r.stdout


def _spend_events(p):
    return p.db.q("SELECT * FROM events WHERE kind LIKE 'detached_job_spend%' ORDER BY id")


def _spend_state(run_dir, name):
    (j,) = [j for j in json.loads((run_dir / "detached.json").read_text()) if j["name"] == name]
    return j["spend"]


def test_a_spend_cap_reached_stops_only_that_jobs_group_and_raises_one_event(env, tmp_path):
    from ttp import jobstop, locks as lk, progress, spendcap
    from ttp.daemon import Daemon
    p = make(env)
    tid = p.db.add_task("bench", "s", kind="work", tier="light", origin="user")
    run_dir = _spend_run(p, tid)
    rc, pgid, out = _spend_job(p, run_dir, "paid", "--spend-cmd", "echo '$12.50'", "--spend-cap", "10",
                               "--spend-every", "60")
    rc2, pgid2, _ = _spend_job(p, run_dir, "other")
    try:
        assert "spend cap: $10.00, read every 60 s" in out
        Daemon(p.base).tend_pushes()   # the daemon's tick runs the check
        assert lk.any_free([lk.job_lock(rc)]) and not jobstop.members(pgid)
        assert "spend cap" in rc.with_suffix(".lost").read_text()
        assert jobstop.running(rc2) and jobstop.members(pgid2)   # the other job is untouched
        (ev,) = _spend_events(p)
        assert (ev["kind"], ev["severity"], ev["status"], ev["task"]) == (spendcap.CAP_EVENT, "normal", "queued", tid)
        assert "paid" in ev["text"] and "$12.50" in ev["text"] and "$10.00" in ev["text"]
        (rep,) = [r for r in progress.entries(p.state) if r["kind"] == "failure"]
        assert rep["key"] == "paid" and rep["reason"] == "spend cap" and rep["job"] == str(rc)
        assert _spend_state(run_dir, "paid")["capped"] is True
        # Once: a later pass neither reads nor raises again.
        assert spendcap.tend(p, now=time.time() + 3600) == [] and len(_spend_events(p)) == 1
    finally:
        _end_group(pgid)
        _end_group(pgid2)


def test_a_failing_spend_command_never_stops_the_job_and_raises_one_event_per_streak(env, tmp_path, monkeypatch):
    from ttp import jobstop, spendcap
    p = make(env)
    monkeypatch.setattr(spendcap, "TIMEOUT_S", 1)
    tid = p.db.add_task("bench", "s", kind="work", tier="light", origin="user")
    run_dir = _spend_run(p, tid)
    script = tmp_path / "spend.sh"
    script.write_text("exit 3\n")
    rc, pgid, _ = _spend_job(p, run_dir, "paid", "--spend-cmd", f"sh {shlex.quote(str(script))}",
                             "--spend-cap", "5")
    t = time.time()
    try:
        # A non-zero exit, a timeout and output that is not one number each count; the third raises.
        for k, body in enumerate(["exit 3\n", "sleep 5\n", "echo 'about 3 dollars'\n"]):
            script.write_text(body)
            spendcap.tend(p, now=t + (k + 1) * 301)
            assert len(_spend_events(p)) == (1 if k == 2 else 0)
        script.write_text("echo nope\n")
        spendcap.tend(p, now=t + 4 * 301)
        (ev,) = _spend_events(p)
        assert ev["kind"] == spendcap.FAIL_EVENT and ev["severity"] == "normal" and ev["status"] == "queued"
        assert "paid" in ev["text"] and "3 times in a row" in ev["text"] and "about 3 dollars" not in ev["text"]
        assert jobstop.running(rc) and jobstop.members(pgid)
        # Not due yet: the cadence holds (default 300 s).
        script.write_text("echo 1\n")
        spendcap.tend(p, now=t + 4 * 301 + 10)
        assert _spend_state(run_dir, "paid")["fails"] == 4
        # A good read resets the count; a new streak of three raises a new event.
        spendcap.tend(p, now=t + 5 * 301)
        assert _spend_state(run_dir, "paid")["fails"] == 0 and _spend_state(run_dir, "paid")["read"] == 1.0
        script.write_text("exit 1\n")
        for k in range(6, 9):
            spendcap.tend(p, now=t + k * 301)
        assert len(_spend_events(p)) == 2 and jobstop.running(rc)
    finally:
        _end_group(pgid)


def test_a_job_without_spend_options_is_never_checked(env, tmp_path, monkeypatch):
    from ttp import jobstop, spendcap
    p = make(env)
    tid = p.db.add_task("bench", "s", kind="work", tier="light", origin="user")
    run_dir = _spend_run(p, tid)
    rc, pgid, out = _spend_job(p, run_dir, "plain")
    calls = []
    monkeypatch.setattr(spendcap, "read", lambda *a, **k: calls.append(a) or (99.0, ""))
    try:
        assert "spend cap" not in out
        assert "spend" not in json.loads((run_dir / "detached.json").read_text())[0]
        assert spendcap.tend(p, now=time.time() + 3600) == [] and calls == []
        assert jobstop.running(rc) and not _spend_events(p)
    finally:
        _end_group(pgid)


def test_detach_refuses_a_spend_command_without_a_cap_and_the_reverse(env, tmp_path):
    p = make(env)
    run_dir = p.runs / "spend-usage"
    run_dir.mkdir(parents=True)
    bad = [["--spend-cmd", "echo 1"], ["--spend-cap", "5"], ["--spend-every", "300"],
           ["--spend-cmd", "echo 1", "--spend-cap", "0"], ["--spend-cmd", "echo 1", "--spend-cap", "nan"],
           ["--spend-cmd", " ", "--spend-cap", "5"],
           ["--spend-cmd", "echo 1", "--spend-cap", "5", "--spend-every", "5"]]
    for opts in bad:
        r = _ttp_run(p, "detach", *opts, "job", "--", "true", env={"TTP_RUN_DIR": str(run_dir)})
        assert r.returncode != 0 and "--spend" in r.stderr, (opts, r.stderr)
    # Also after the name, where the options are taken back out of the command's words.
    r = _ttp_run(p, "detach", "job", "--spend-cap", "5", "--", "true", env={"TTP_RUN_DIR": str(run_dir)})
    assert r.returncode != 0 and "--spend-cmd and --spend-cap go together" in r.stderr
    r = _ttp_run(p, "detach", "--remote", "box", "--spend-cmd", "echo 1", "--spend-cap", "5", "job", "--", "true",
                 env={"TTP_RUN_DIR": str(run_dir)})
    assert r.returncode != 0 and "local job" in r.stderr
    assert not list(run_dir.iterdir())   # nothing started or recorded


def test_the_spend_check_never_logs_the_commands_output_or_environment(env, tmp_path, monkeypatch):
    from ttp import jobstop, spendcap
    p = make(env)
    secret = "sk-test-not-a-real-key-4126"
    monkeypatch.setenv("PAID_API_KEY", secret)   # in the daemon's environment, as `ttp secret env` would set it
    tid = p.db.add_task("bench", "s", kind="work", tier="light", origin="user")
    run_dir = _spend_run(p, tid)
    script = tmp_path / "spend.sh"
    script.write_text('echo "$PAID_API_KEY"; echo "$PAID_API_KEY" >&2; exit 1\n')
    rc, pgid, _ = _spend_job(p, run_dir, "paid", "--spend-cmd", f"sh {shlex.quote(str(script))}",
                             "--spend-cap", "5")
    t = time.time()
    try:
        for k in range(3):
            spendcap.tend(p, now=t + (k + 1) * 301)
        script.write_text('echo "$PAID_API_KEY"\n')   # the key and no number
        spendcap.tend(p, now=t + 4 * 301)
        script.write_text('echo "$PAID_API_KEY" >&2; echo 7.25\n')
        lines = spendcap.tend(p, now=t + 5 * 301)
        assert lines and secret not in "".join(lines)
        assert [e["kind"] for e in _spend_events(p)] == [spendcap.FAIL_EVENT, spendcap.CAP_EVENT]
        assert not jobstop.running(rc)
        rows = [dict(r) for t in ("events", "messages", "kv") for r in p.db.q(f"SELECT * FROM {t}")]
        assert secret not in json.dumps(rows, default=str)
        for f in p.state.rglob("*"):
            if f.is_file() and not f.name.endswith((".db", ".db-wal", ".db-shm")):
                assert secret.encode() not in f.read_bytes(), f
    finally:
        _end_group(pgid)


# `ttp detach --stop NAME` of a driver a project's own launcher started (launcherstop): NAME.pid in a
# results dir, STOP file beside it. Every process signalled is one this test started.

def _launcher(results, name="drive", obey=True):
    """A launcher stand-in leading its own group: records its pid in results/<name>.pid and, when obey,
    exits once results/STOP appears; returns its Popen once the pid file is written."""
    results.mkdir(parents=True, exist_ok=True)
    pidf, stop = shlex.quote(str(results / f"{name}.pid")), shlex.quote(str(results / "STOP"))
    loop = f"while [ ! -e {stop} ]; do sleep 0.1; done" if obey else "while :; do sleep 0.1; done"
    proc = subprocess.Popen(["sh", "-c", f"echo $$ >{pidf}.tmp; mv {pidf}.tmp {pidf}; {loop}"],
                            start_new_session=True)
    assert _wait_for_path(results / f"{name}.pid")
    return proc


def _launcher_alive(proc):
    return proc.poll() is None


def _end_launcher(proc):
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def test_detach_stop_stops_a_launcher_driver_by_its_stop_file(env, tmp_path):
    """No detached job is recorded for the name: its launcher's pid file in $TTP_RUN_DIR, or in the task's
    t<id> folder in the project root, is found, STOP is written beside it and the launcher exits on it."""
    p = make(env)
    run_dir = p.runs / "launch"
    proc = _launcher(run_dir)
    try:
        r = _ttp_run(p, "detach", "--stop", "drive", env={"TTP_RUN_DIR": str(run_dir), "TTP_TASK": "7"})
        assert r.returncode == 0 and "drive: stopped" in r.stdout and "exited after" in r.stdout, r.stderr
        assert (run_dir / "STOP").exists() and proc.wait(timeout=10) is not None
    finally:
        _end_launcher(proc)
    folder = p.root / "t7"
    proc = _launcher(folder, "soak")
    try:
        r = _ttp_run(p, "detach", "--stop", "soak", env={"TTP_TASK": "7"})
        assert r.returncode == 0 and "soak: stopped" in r.stdout, r.stderr
        assert (folder / "STOP").exists() and proc.wait(timeout=10) is not None
    finally:
        _end_launcher(proc)


def test_launcher_stop_escalates_to_sigterm_when_the_stop_file_is_ignored(env, tmp_path, monkeypatch):
    from ttp import launcherstop
    monkeypatch.setattr(launcherstop, "STOP_WAIT_S", 0.3)
    proc = _launcher(tmp_path / "res", obey=False)
    try:
        f = tmp_path / "res" / "drive.pid"
        state, pid, token = launcherstop.identify(f, None)
        assert (state, pid) == ("live", proc.pid)
        r = launcherstop.stop(f, pid, token, "test")
        assert r["how"] == "sigterm" and not r["left"] and (tmp_path / "res" / "STOP").exists()
        assert proc.wait(timeout=10) == -signal.SIGTERM
    finally:
        _end_launcher(proc)


def test_detach_stop_reports_a_stale_launcher_pid_file_already_stopped(env, tmp_path):
    p = make(env)
    res = tmp_path / "res"
    proc = _launcher(res)
    (res / "STOP").write_text("")
    proc.wait(timeout=10)
    (res / "STOP").unlink()
    r = _ttp_run(p, "detach", "--stop", str(res / "drive"))
    assert r.returncode == 0 and "already stopped" in r.stdout and "gone" in r.stdout, r.stderr
    assert not (res / "STOP").exists()   # a later launch must not stop at once


def test_detach_stop_never_signals_a_reused_launcher_pid(env, tmp_path):
    """The pid file names a live process that started after the file was written, or whose start token
    differs from the one the file pins: a reused pid, reported already stopped and left alone."""
    from ttp.runner import proc_start
    p = make(env)
    res = tmp_path / "res"
    proc = _launcher(res, obey=False)
    try:
        f = res / "drive.pid"
        old = time.time() - 3600
        os.utime(f, (old, old))
        r = _ttp_run(p, "detach", "--stop", str(f))
        assert r.returncode == 0 and "already stopped" in r.stdout and "another process" in r.stdout, r.stderr
        f.write_text(f"{proc.pid} start={int(proc_start(proc.pid)) + 1}\n")
        r = _ttp_run(p, "detach", "--stop", str(f))
        assert r.returncode == 0 and "start token" in r.stdout, r.stderr
        time.sleep(0.3)
        assert _launcher_alive(proc) and not (res / "STOP").exists()
    finally:
        _end_launcher(proc)


def test_detach_stop_refuses_a_launcher_pid_of_another_project(env, tmp_path):
    p = make(env)
    res = tmp_path / "res"
    res.mkdir()
    proc = subprocess.Popen(["sh", "-c", "sleep 30"], start_new_session=True,
                            env={**os.environ, "TTP_PROJECT": str(tmp_path / "other" / "tt-project")})
    try:
        (res / "drive.pid").write_text(f"{proc.pid}\n")
        r = _ttp_run(p, "detach", "--stop", str(res / "drive.pid"))
        assert r.returncode == 3 and "another tt-project project" in r.stderr, r.stderr
        assert _launcher_alive(proc) and not (res / "STOP").exists()
    finally:
        _end_launcher(proc)


def test_detach_stop_of_an_unknown_name_says_where_it_looked(env, tmp_path):
    p = make(env)
    run_dir = p.runs / "none"
    run_dir.mkdir(parents=True)
    r = _ttp_run(p, "detach", "--stop", "ghost", env={"TTP_RUN_DIR": str(run_dir), "TTP_TASK": "9"})
    assert r.returncode == 2 and "ghost.pid" in r.stderr and str(run_dir) in r.stderr, r.stderr
    assert str(p.root / "t9") in r.stderr and "detach.launcher_stops" in r.stderr
    r = _ttp_run(p, "detach", "--stop", str(tmp_path / "x" / "ghost.rc"))
    assert r.returncode == 2 and str(tmp_path / "x") in r.stderr and "detach.launcher_stops" in r.stderr


# `ttp detach --stop <job folder>` of a job a project's launcher laid out its own way (no detached.json
# entry, no pid file): the stop command registered in detach.launcher_stops runs, audited.

def _layout_job(folder, project=None):
    """A launcher's job in its own layout: a process in its own session, with the job folder as its cwd,
    that records its pid in <folder>/job.txt (not a pid file); returns its Popen."""
    folder.mkdir(parents=True, exist_ok=True)
    e = {k: v for k, v in os.environ.items() if k != "TTP_PROJECT"}
    if project:
        e["TTP_PROJECT"] = str(project)
    proc = subprocess.Popen(["sh", "-c", "echo $$ >job.tmp; mv job.tmp job.txt; while :; do sleep 0.1; done"],
                            cwd=folder, env=e, start_new_session=True)
    assert _wait_for_path(folder / "job.txt")
    return proc


def _stop_script(tmp_path, body):
    script = tmp_path / "bin" / "gate-stop"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return script


def test_detach_stop_runs_a_registered_launcher_stop_and_audits_it(env, tmp_path):
    from ttp import detach
    p = make(env)
    folder = p.root / "gates" / "run1"
    proc = _layout_job(folder)
    try:
        script = _stop_script(tmp_path, 'for d; do :; done; echo "$1 $d" >>"$(dirname "$0")/called"; '
                                          'kill "$(cat "$d/job.txt")"')
        p.set_config("detach.launcher_stops", {"gates/*": f"{script} --now"})
        r = _ttp_run(p, "detach", "--stop", str(folder), env={"TTP_TASK": "12"})
        assert r.returncode == 0 and "run1: stopped by its registered stop" in r.stdout, r.stderr
        assert "none is left" in r.stdout and proc.wait(timeout=10) is not None
        assert (script.parent / "called").read_text().strip() == f"--now {folder.resolve()}"
        rows = [json.loads(x) for x in (p.state / detach.SIGNALS).read_text().splitlines()]
        assert any(x["signal"] == "launcher_stop" and x["rc"] == str(folder.resolve()) and x["task"] == 12
                   and x["ok"] and x["exit"] == 0 for x in rows), rows
        ev = p.db.one("SELECT text, data FROM events WHERE kind='detached_job_stopped' ORDER BY id DESC LIMIT 1")
        assert ev and "registered stop" in ev["text"] and json.loads(ev["data"])["command"][-1] == str(folder.resolve())
    finally:
        _end_launcher(proc)


def test_detach_stop_refuses_an_unregistered_launcher_layout_naming_the_key(env, tmp_path):
    p = make(env)
    folder = p.root / "gates" / "run1"
    proc = _layout_job(folder)
    try:
        p.set_config("detach.launcher_stops", {"other/*": "true"})
        r = _ttp_run(p, "detach", "--stop", str(folder))
        assert r.returncode == 2 and "detach.launcher_stops" in r.stderr, r.stderr
        time.sleep(0.2)
        assert _launcher_alive(proc)
    finally:
        _end_launcher(proc)


def test_detach_stop_reports_a_registered_stop_that_fails_or_leaves_processes(env, tmp_path, monkeypatch):
    """A stop command exiting non-zero, or exiting 0 while a process of the job still runs, is reported with
    what is left, never as stopped; both are still audited."""
    from ttp import launcherstop
    p = make(env)
    folder = p.root / "gates" / "run2"
    folder.mkdir(parents=True)
    script = _stop_script(tmp_path, "echo cannot reach the gate >&2; exit 4")
    p.set_config("detach.launcher_stops", {"gates/**": [str(script)]})
    r = _ttp_run(p, "detach", "--stop", str(folder))
    assert r.returncode == 1 and "NOT stopped" in r.stderr and "exit 4" in r.stderr, r.stderr
    assert "cannot reach the gate" in r.stderr
    monkeypatch.setattr(launcherstop, "GONE_WAIT_S", 0.3)
    proc = _layout_job(folder)
    try:
        out = launcherstop.registered_stop(p, folder.resolve(), "gates/**", ["true"], p.root, "test")
        assert not out["ok"] and out["rc"] == 0 and proc.pid in out["left"], out
        assert f"still running: {proc.pid}" in out["text"] and _launcher_alive(proc)
        assert p.db.one("SELECT COUNT(*) AS n FROM events WHERE kind='detached_job_stopped' AND severity='normal'")["n"] == 2
    finally:
        _end_launcher(proc)


def test_detach_stop_never_runs_a_registered_stop_on_another_projects_job(env, tmp_path):
    """A job folder outside the project root, inside another tt-project project's folder or a nested
    project's own root, or with a process of another project is refused; the stop command never runs."""
    p = make(env)
    script = _stop_script(tmp_path, 'touch "$(dirname "$0")/called"')
    outside = tmp_path / "elsewhere" / "run1"
    outside.mkdir(parents=True)
    other = p.root / "nested" / "tt-project"
    (other / "state").mkdir(parents=True)
    (other / "state" / "project.db").write_text("")
    inner = other / "gates" / "run1"
    inner.mkdir(parents=True)
    (p.root / "nested2" / "tt-project" / "state").mkdir(parents=True)
    (p.root / "nested2" / "tt-project" / "state" / "project.db").write_text("")
    beside = p.root / "nested2" / "gates" / "run1"
    beside.mkdir(parents=True)
    p.set_config("detach.launcher_stops", {str(tmp_path / "**"): str(script), "**": str(script)})
    (p.root / "gates").mkdir()
    (p.root / "gates" / "link").symlink_to(outside)
    for folder, why in ((outside, "outside the project root"), (inner, "another tt-project project"),
                        (beside, "another tt-project project"),
                        (p.root / "gates" / "link", "outside the project root"),
                        (p.root / "gates" / "..", "the project's own folder"),
                        (p.runs, "the project's own folder")):
        r = _ttp_run(p, "detach", "--stop", str(folder))
        assert r.returncode == 3 and why in r.stderr, (folder, r.stderr)
    run_dir = p.runs / "9"
    run_dir.mkdir(parents=True)
    r = _ttp_run(p, "detach", "--stop", "..", env={"TTP_RUN_DIR": str(run_dir)})
    assert r.returncode == 2, r.stderr
    mine = p.root / "gates" / "run3"
    proc = _layout_job(mine, project=tmp_path / "third" / "tt-project")
    try:
        r = _ttp_run(p, "detach", "--stop", str(mine))
        assert r.returncode == 3 and "belongs to another tt-project project" in r.stderr, r.stderr
        assert _launcher_alive(proc)
    finally:
        _end_launcher(proc)
    assert not (script.parent / "called").exists()


def test_launcher_stop_globs_match_folder_levels():
    from ttp import launcherstop
    root = Path("/r")
    stops = {"gates/*": "a", "deep/**/job": ["b", "x"], "bad": 3}
    assert launcherstop.registered(stops, root / "gates" / "g1", root) == ("gates/*", ["a"])
    assert launcherstop.registered(stops, root / "gates" / "g1" / "sub", root) is None
    assert launcherstop.registered(stops, root / "deep" / "a" / "b" / "job", root) == ("deep/**/job", ["b", "x"])
    assert launcherstop.registered(stops, root / "deep" / "job", root)[0] == "deep/**/job"
    assert launcherstop.registered(stops, root / "bad", root) is None
    assert launcherstop.registered("not a map", root / "gates" / "g1", root) is None


def test_launcher_stop_never_targets_its_caller_nor_writes_through_a_stop_symlink(env, tmp_path, monkeypatch):
    """A pid file naming this process (or an ancestor) is refused; a STOP planted as a symlink is left as
    it is, its target never written, and the launcher is still stopped by the escalation."""
    from ttp import launcherstop
    res = tmp_path / "res"
    res.mkdir()
    (res / "self.pid").write_text(f"{os.getpid()}\n")
    assert launcherstop.identify(res / "self.pid", None)[0] == "refused"
    monkeypatch.setattr(launcherstop, "STOP_WAIT_S", 0.3)
    target = tmp_path / "elsewhere"
    proc = _launcher(res, obey=False)
    try:
        (res / "STOP").symlink_to(target)
        state, pid, token = launcherstop.identify(res / "drive.pid", None)
        assert state == "live"
        assert launcherstop.stop(res / "drive.pid", pid, token, "test")["how"] == "sigterm"
        assert not target.exists() and (res / "STOP").is_symlink()
    finally:
        _end_launcher(proc)


def _task_proc(p, tid, project=None, argv=("sleep", "60")):
    """A process started as a run of task tid (its TTP_TASK and TTP_PROJECT), in a session of its own."""
    e = {**os.environ, "TTP_TASK": str(tid), "TTP_TASK_ID": str(tid), "TTP_PROJECT": str(project or p.base)}
    return subprocess.Popen(list(argv), env=e, start_new_session=True)


def _pidfile(path, *lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{x}\n" for x in lines))
    return path


def test_stop_pids_stops_only_the_listed_processes_of_the_tasks_own_driver(env, tmp_path):
    """`ttp detach --stop-pids`: a task stops what its driver listed in a pid file under its own results dir
    (SIGTERM; a pid that is gone is skipped), never a process of the same task the file does not list."""
    from ttp import jobstop
    from ttp.runner import proc_start
    p = make(env)
    tid = p.db.add_task("driver", "s", kind="work", tier="light", origin="user")
    a, b, unlisted = _task_proc(p, tid), _task_proc(p, tid), _task_proc(p, tid)
    try:
        time.sleep(0.2)
        f = _pidfile(p.root / "results" / f"t{tid}" / "driver.pids", a.pid, f"{b.pid} start={proc_start(b.pid)}",
                     _dead_pid())
        r = _ttp_run(p, "detach", "--stop-pids", str(f), env={"TTP_TASK": str(tid), "TTP_PIDNS": "", "TTP_CGROUP": ""})
        assert r.returncode == 0 and f"SIGTERM to {a.pid}, {b.pid}" in r.stdout and "already gone" in r.stdout, r
        assert a.wait(5) == -signal.SIGTERM and b.wait(5) == -signal.SIGTERM and unlisted.poll() is None
        assert [e["text"] for e in _stopped_events(p) if f"task #{tid}" in e["text"]]
        r = _ttp_run(p, "detach", "--stop-pids", str(f), env={"TTP_TASK": str(tid), "TTP_PIDNS": "", "TTP_CGROUP": ""})
        assert r.returncode == 0 and "nothing to stop" in r.stdout, r
        # A run dir of the task is one of its results dirs too.
        run = p.runs / "drv"
        run.mkdir(parents=True)
        p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
               (tid, "worker", "fake", time.time() - 60, "ok", str(run)))
        targets, _, why = jobstop.plan_pids(p, _pidfile(run / "pids", unlisted.pid), str(tid))
        assert why is None and [t[0] for t in targets] == [unlisted.pid]
    finally:
        for x in (a, b, unlisted):
            x.kill()
            x.wait()


def test_stop_pids_escalates_to_sigkill_when_sigterm_is_ignored(env, tmp_path, monkeypatch):
    """A listed process that ignores SIGTERM is stopped by the SIGKILL step, and the outcome names its pid
    under the SIGKILL step (PIDS_TERM_S monkeypatched short so the test stays fast). The child says it is
    ready only once SIGTERM is ignored, so a slow start on a busy host cannot let SIGTERM end it."""
    from ttp import jobstop
    p = make(env)
    tid = p.db.add_task("driver", "s", kind="work", tier="light", origin="user")
    ready = tmp_path / "ignoring-sigterm"
    # SIGTERM ignored, then a long sleep: only the SIGKILL step can stop it.
    stub = _task_proc(p, tid, argv=(sys.executable, "-c",
                                    "import pathlib, signal, sys, time\n"
                                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                    "pathlib.Path(sys.argv[1]).touch()\n"
                                    "time.sleep(600)", str(ready)))
    try:
        _wait_for_file(ready, stub, timeout=120)
        monkeypatch.setattr(jobstop, "PIDS_TERM_S", 0.3)
        monkeypatch.setattr(jobstop, "FREE_WAIT_S", 60.0)   # ends as soon as the pid is gone; room for a busy host
        f = _pidfile(p.root / "results" / f"t{tid}" / "driver.pids", stub.pid)
        targets, gone, why = jobstop.plan_pids(p, f, str(tid))
        assert why is None and [t[0] for t in targets] == [stub.pid], why
        out = jobstop.stop_pids(p, f, tid, targets, gone, "stopped by `ttp detach --stop-pids`")
        assert stub.wait(10) == -signal.SIGKILL, "the process ignores SIGTERM; only the SIGKILL step can stop it"
        assert f"SIGKILL after {jobstop.PIDS_TERM_S:g}s to {stub.pid}" in out["text"], out["text"]
        assert f"SIGTERM to {stub.pid}" in out["text"] and out["killed"] == [stub.pid], out["text"]
        assert not out["left"] and out["pids"] == [stub.pid], out
        assert [e for e in _stopped_events(p) if f"task #{tid}" in e["text"] and "SIGKILL" in e["text"]]
    finally:
        stub.kill()
        stub.wait()


def test_stop_pids_refuses_a_file_outside_the_tasks_results_dir_or_reached_by_a_symlink(env, tmp_path):
    from ttp import jobstop
    p = make(env)
    tid = p.db.add_task("driver", "s", kind="work", tier="light", origin="user")
    other = p.db.add_task("other", "s", kind="work", tier="light", origin="user")
    proc = _task_proc(p, tid)
    try:
        time.sleep(0.2)
        outside = _pidfile(tmp_path / "elsewhere" / "pids", proc.pid)
        others = _pidfile(p.root / "results" / f"t{other}" / "pids", proc.pid)
        (p.root / "results" / f"t{tid}").mkdir(parents=True)
        linked_file = p.root / "results" / f"t{tid}" / "pids"
        linked_file.symlink_to(outside)
        linked_dir = p.root / "results" / f"task-{tid}"
        linked_dir.symlink_to(outside.parent, target_is_directory=True)
        for f in (outside, others, linked_file, linked_dir / "pids", tmp_path / "missing"):
            targets, _, why = jobstop.plan_pids(p, f, str(tid))
            assert targets == [] and why and ("not under a results dir" in why or "no pid file" in why), (f, why)
        targets, _, why = jobstop.plan_pids(p, others, "")
        assert why and "only a task" in why
        r = _ttp_run(p, "detach", "--stop-pids", str(linked_file), env={"TTP_TASK": str(tid)})
        assert r.returncode == 3 and "not under a results dir" in r.stderr, r
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_stop_pids_refuses_a_reused_pid_another_tasks_or_projects_process_and_a_foreign_lock_holder(env, tmp_path):
    """Each listed pid must still be the process the file recorded and the task's own; one refusal refuses
    the whole file, so the allowed pid beside it is not stopped either."""
    from ttp import jobstop
    from ttp.runner import proc_start
    p = make(env)
    tid = p.db.add_task("driver", "s", kind="work", tier="light", origin="user")
    other = p.db.add_task("other", "s", kind="work", tier="light", origin="user")
    slot = p.state / "locks" / "device.0.lock"
    slot.parent.mkdir(parents=True, exist_ok=True)
    holder = ("import fcntl, json, os, sys, time\nf = open(sys.argv[1], 'a+')\nfcntl.flock(f, fcntl.LOCK_EX)\n"
              "f.truncate(0)\nf.write(json.dumps({'holder': 'someone else', 'pid': os.getpid()}))\nf.flush()\n"
              "time.sleep(60)\n")
    mine, theirs, foreign = _task_proc(p, tid), _task_proc(p, other), _task_proc(p, tid, project=tmp_path / "other")
    locker = _task_proc(p, tid, argv=(sys.executable, "-c", holder, str(slot)))
    procs = [mine, theirs, foreign, locker]
    try:
        assert _wait_for(lambda: slot.exists() and "someone else" in slot.read_text(), 10)
        lock_pid = json.loads(slot.read_text())["pid"]
        d = p.root / "results" / f"t{tid}"
        cases = {
            "reused": (f"{mine.pid} start=1", "pid was reused"),
            "task": (theirs.pid, f"belongs to task #{other}"),
            "project": (foreign.pid, "not started by this project"),
            "lock": (lock_pid, "holds lock slot device.0.lock"),
            "self": (os.getpid(), "is the caller"),
        }
        for name, (line, want) in cases.items():
            targets, _, why = jobstop.plan_pids(p, _pidfile(d / f"{name}.pids", mine.pid, line), str(tid))
            assert targets == [] and why and "nothing stopped" in why and want in why, (name, why)
        # No start token: a process that started after the file was last written is a reused pid.
        late = _pidfile(d / "late.pids", mine.pid)
        os.utime(late, (time.time() - 3600, time.time() - 3600))
        targets, _, why = jobstop.plan_pids(p, late, str(tid))
        assert why and "started after the pid file was last written" in why
        targets, _, why = jobstop.plan_pids(p, _pidfile(d / "ok.pids", f"{mine.pid} start={proc_start(mine.pid)}"),
                                            str(tid))
        assert why is None and [t[0] for t in targets] == [mine.pid]
        r = _ttp_run(p, "detach", "--stop-pids", str(d / "task.pids"), env={"TTP_TASK": str(tid)})
        assert r.returncode == 3 and "refused" in r.stderr, r
        assert all(x.poll() is None for x in procs)
    finally:
        for x in procs:
            try:
                os.killpg(x.pid, signal.SIGKILL)
            except OSError:
                pass
            x.wait()


def test_a_sandboxed_stop_pids_is_queued_and_the_daemon_checks_and_stops(env, tmp_path):
    from ttp import jobstop
    p = make(env)
    tid = p.db.add_task("driver", "s", kind="work", tier="light", origin="user")
    proc, theirs = _task_proc(p, tid), _task_proc(p, tid + 1)
    try:
        time.sleep(0.2)
        d = p.root / "results" / f"t{tid}"
        f = _pidfile(d / "pids", proc.pid)
        jobstop.request_pids(p, f, str(tid))
        assert [r["pids"] for r in jobstop.serve_requests(p)] == [[proc.pid]]
        assert proc.wait(5) == -signal.SIGTERM and "SIGTERM to" in (d / "pids.stopped").read_text()
        bad = _pidfile(d / "bad.pids", theirs.pid)
        jobstop.request_pids(p, bad, str(tid))
        assert jobstop.serve_requests(p) == [] and theirs.poll() is None
        assert "nothing stopped" in (d / "bad.pids.stopped").read_text()
        assert not list((p.state / jobstop.QUEUE).glob("*.json"))
    finally:
        for x in (proc, theirs):
            x.kill()
            x.wait()


def test_a_detached_jobs_lock_is_kept_by_a_process_it_left_running_until_that_ends(tmp_path, monkeypatch):
    """detach.start passes the job lock on by design: it means "a process of this job lives", even one
    that left its group (jobstop ends those through it). The .rc still says the job ended, the label names
    the wrapper (via "job", which lockreap leaves alone), and the lock is free once the last process ends."""
    from ttp import detach, locks
    monkeypatch.setenv("TTP_RUN_ID", "70")
    rc, left = tmp_path / "run" / "job.rc", tmp_path / "left.pid"
    rc.parent.mkdir()
    stray = (f"{shlex.quote(sys.executable)} -c 'import os, sys, time; os.setsid(); "
             f"open(sys.argv[1], \"w\").write(str(os.getpid())); time.sleep(60)' {left}")
    lock = locks.try_take([locks.job_lock(rc)], "detached job job", "leave one running")
    try:
        pid = detach.start(rc, rc.with_suffix(".log"), ["sh", "-c", f"{stray} & exit 0"],
                           str(tmp_path), dict(os.environ), lock)
    finally:
        lock.close()
    assert _wait_for_path(rc) and rc.read_text().strip() == "0"
    assert _until_true(lambda: left.exists() and left.read_text())
    child = int(left.read_text())
    try:
        label = json.loads(locks.job_lock(rc).read_text())
        assert label["via"] == "job" and label["pid"] == pid and label["pgid"] == pid, label
        assert not locks.any_free([locks.job_lock(rc)]), "a process the job left running keeps its job lock"
        assert detach.state(rc)[0] != "running", "the .rc says it ended"
    finally:
        os.kill(child, signal.SIGKILL)
    assert _until_true(lambda: locks.any_free([locks.job_lock(rc)])), "free once its last process ended"
    detach.reap_children()


_PROXY_URL = "http://user:s3cr3t@127.0.0.1:41234"


def test_sandbox_only_names_loopback_proxies_in_either_case_and_keeps_others():
    from ttp import loopproxy
    env = {"HTTPS_PROXY": _PROXY_URL, "https_proxy": "http://localhost:3128", "HTTP_PROXY": "127.8.9.10:80",
           "http_proxy": "http://[::1]:3128/", "ALL_PROXY": "socks5h://LOCALHOST:1080",
           "all_proxy": "http://[::ffff:127.0.0.1]:8080", "NO_PROXY": "127.0.0.1,localhost"}
    assert sorted(loopproxy.sandbox_only(env)) == sorted(k for k in env if k != "NO_PROXY")
    others = {"HTTPS_PROXY": "http://proxy.example.com:3128", "https_proxy": "http://127.0.0.1.example.com:1",
              "HTTP_PROXY": "http://localhost.example.com:1", "http_proxy": "http://10.0.0.1:3128",
              "ALL_PROXY": "http://user:127.0.0.1@proxy.example.com:1", "all_proxy": "",
              "no_proxy": "localhost", "FTP_PROXY": _PROXY_URL}
    assert loopproxy.sandbox_only(others) == []
    assert loopproxy.sandbox_only({"HTTPS_PROXY": "http://[bad"}) == []


def test_a_queued_job_leaves_loopback_proxies_out_and_never_logs_their_url(env, tmp_path, monkeypatch, capsys):
    """A job queued from a sandboxed command starts at the daemon without the sandbox's loopback proxy
    variables (upper and lower case), with the daemon's own proxy, a non-loopback one and NO_PROXY kept;
    the URL appears in no output and no queue file. A record queued before the fix is cleaned too."""
    from ttp import detach
    p = make(env)
    run_dir = p.runs / "proxy"
    run_dir.mkdir(parents=True)
    job = {"HTTPS_PROXY": _PROXY_URL, "https_proxy": _PROXY_URL, "ALL_PROXY": _PROXY_URL,
           "http_proxy": "http://proxy.example.com:3128", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "x"}
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY",
              "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("all_proxy", "http://daemon-proxy.example.com:1")
    rcs = []
    for name in ("new", "old"):
        rc, out = run_dir / f"{name}.rc", run_dir / f"{name}.env"
        q = detach.enqueue(p.state, rc, run_dir / f"{name}.log", ["sh", "-c", f"env >{out}"], str(tmp_path), job)
        if name == "old":   # as queued before loopback proxies were dropped
            m = json.loads(q.read_text())
            m["env"] = {**m["env"], **job}
            q.write_text(json.dumps(m))
        else:
            assert _PROXY_URL not in q.read_text()
        rcs.append((rc, out))
    err = capsys.readouterr().err
    assert "HTTPS_PROXY" in err and "https_proxy" in err and "ALL_PROXY" in err
    assert "127.0.0.1" not in err and "s3cr3t" not in err and "proxy.example.com" not in err
    detach.tend(p)
    for rc, out in rcs:
        assert _wait_for_path(rc) and rc.read_text().strip() == "0"
        got = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
        assert not {"HTTPS_PROXY", "https_proxy", "ALL_PROXY"} & set(got)
        assert got["http_proxy"] == "http://proxy.example.com:3128"
        assert got["all_proxy"] == "http://daemon-proxy.example.com:1"
        assert got["NO_PROXY"] == "127.0.0.1,localhost" and got["no_proxy"] == "x"
    out = capsys.readouterr()
    assert "s3cr3t" not in out.out + out.err


def test_a_resumable_job_relaunched_by_the_daemon_leaves_loopback_proxies_out(env, tmp_path):
    from ttp import detach
    p = make(env)
    run_dir = p.runs / "proxyresume"
    run_dir.mkdir(parents=True)
    rc, out = run_dir / "job.rc", run_dir / "job.env"
    detach.save_resume(rc, run_dir / "job.log", ["sh", "-c", f"env >{out}"], str(tmp_path),
                       {"HTTPS_PROXY": _PROXY_URL, "http_proxy": "http://proxy.example.com:3128"}, "boot-a")
    assert _PROXY_URL not in detach.resume_file(rc).read_text()
    why, pid = detach.relaunch(rc, "boot-b")
    assert not why and pid > 1 and _wait_for_path(rc)
    got = out.read_text()
    assert "s3cr3t" not in got and "http_proxy=http://proxy.example.com:3128" in got


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="no_new_privs is Linux only")
def test_a_detached_job_runs_with_no_new_privs_so_sudo_cannot_gain_privileges(tmp_path, monkeypatch):
    """Every start path (a worker's direct start, the daemon's queued, relaunched and heal fix starts)
    goes through detach.start, which fences the job of a run the daemon sandboxed. A sandboxed test
    runner has the flag already, so the spy shows the start asks for the fence itself."""
    from ttp import detach, locks, privlock
    asked, real = [], privlock.no_new_privs_preexec
    monkeypatch.setattr(privlock, "no_new_privs_preexec", lambda: asked.append(1) or real())
    monkeypatch.setenv("TTP_RUN_ID", "71")
    monkeypatch.setenv(privlock.SANDBOXED_ENV, "1")   # a run the daemon sandboxed (write fence on)
    rc = tmp_path / "run" / "job.rc"
    rc.parent.mkdir()
    lock = locks.try_take([locks.job_lock(rc)], "detached job job", "read its own flag")
    try:
        detach.start(rc, rc.with_suffix(".log"), ["sh", "-c", "grep NoNewPrivs /proc/self/status"],
                     str(tmp_path), dict(os.environ), lock)
    finally:
        lock.close()
    assert _wait_for_path(rc) and rc.read_text().strip() == "0"
    assert re.search(r"NoNewPrivs:\s*1", rc.with_suffix(".log").read_text())
    assert asked == [1]
    detach.reap_children()


def test_a_detached_job_of_an_unsandboxed_run_keeps_its_privileges(tmp_path, monkeypatch):
    """By default runs are unsandboxed (no write fence): detach.start sets no no_new_privs, so sudo works."""
    from ttp import detach, locks, privlock
    asked, seen = [], {}
    monkeypatch.setattr(privlock, "no_new_privs_preexec", lambda: asked.append(1))
    monkeypatch.delenv(privlock.SANDBOXED_ENV, raising=False)
    real_popen = detach.subprocess.Popen

    def popen(*a, **k):
        seen["preexec_fn"] = k.get("preexec_fn")
        return real_popen(*a, **k)
    monkeypatch.setattr(detach.subprocess, "Popen", popen)
    rc = tmp_path / "run" / "job.rc"
    rc.parent.mkdir()
    lock = locks.try_take([locks.job_lock(rc)], "detached job job", "read its own flag")
    try:
        detach.start(rc, rc.with_suffix(".log"), ["true"], str(tmp_path), dict(os.environ), lock)
    finally:
        lock.close()
    assert _wait_for_path(rc) and rc.read_text().strip() == "0"
    assert asked == [] and seen == {"preexec_fn": None}
    detach.reap_children()


def test_the_wrapper_writes_its_started_file_by_rename_so_no_reader_sees_it_empty(tmp_path):
    """<name>.started was truncated, then written: a reader in between (running_pid, jobstop, jobuse)
    read no pid, and a daemon that died before restamping the registry left the relaunched job running.
    The wrapper renames a full file over it instead; the old file's inode (a reader's view) is untouched."""
    from ttp import detach, locks
    rc = tmp_path / "run" / "job.rc"
    rc.parent.mkdir()
    detach.started(rc).write_text("stale\n")
    view = tmp_path / "view"
    os.link(detach.started(rc), view)   # what a reader that opened the old file sees
    lock = locks.try_take([locks.job_lock(rc)], "detached job job", "write its pid")
    try:
        pid = detach.start(rc, rc.with_suffix(".log"), ["true"], str(tmp_path), dict(os.environ), lock)
    finally:
        lock.close()
    assert _wait_for_path(rc) and rc.read_text().strip() == "0"
    assert detach.started(rc).read_text().split() == [str(pid)]
    assert view.read_text() == "stale\n", "the wrapper truncated .started in place"
    assert not [f.name for f in rc.parent.iterdir() if f.name.endswith(".tmp")], "a temp file was left"
    detach.reap_children()


def test_the_no_new_privs_preexec_is_none_off_linux(monkeypatch):
    from ttp import privlock
    monkeypatch.setattr(privlock.os, "uname", lambda: os.uname_result(("Darwin", "h", "r", "v", "m")))
    assert privlock.no_new_privs_preexec() is None and privlock.no_new_privs() is False


def test_detach_check_host_on_the_own_machine_reads_it_here_with_no_ssh(env, tmp_path, monkeypatch):
    """A task waiting on a job on the daemon's own host never waits on ssh to itself: its
    `ttp detach --check --host <alias>` reads the job here, also when ssh to itself has no route."""
    from ttp import cli
    from ttp import machines as mm
    make(env)
    mm.add("box-self", hostname="testhost")   # TTP_HOST: this machine
    b = tmp_path / "bin"
    b.mkdir()
    (b / "ssh").write_text(f'#!/bin/sh\necho "$*" >> "{tmp_path / "ssh-called"}"\n'
                           'echo "ssh: Could not resolve hostname box-self" >&2; exit 255\n')
    (b / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    rdir = tmp_path / "jobs"
    rdir.mkdir()
    (rdir / "done.rc").write_text("0\n")
    (rdir / "bad.rc").write_text("3\n")
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert cli._detach_check_remote("box-self", [str(rdir / "done")]) == 0, out.getvalue()
        assert cli._detach_check_remote("box-self", [str(rdir / "bad")]) == 4, out.getvalue()
    assert not (tmp_path / "ssh-called").exists(), "the own host was asked over ssh"
    with contextlib.redirect_stdout(io.StringIO()):   # a remote host still goes over ssh
        assert cli._detach_check_remote("box-far", [str(rdir / "done")]) == 255
    assert "box-far" in (tmp_path / "ssh-called").read_text()


def test_detach_check_host_on_the_own_machine_reads_a_relative_prefix_from_home(env, tmp_path, monkeypatch):
    """On the daemon's own host the check runs where ssh would, in HOME: a prefix relative to HOME, as
    `ttp detach --remote <own alias> --dir <relative dir>` keeps, names the same job from any folder."""
    from ttp import cli, detach
    from ttp import machines as mm
    make(env)
    mm.add("box-self", hostname="testhost")   # TTP_HOST: this machine
    b = tmp_path / "bin"
    b.mkdir()
    (b / "ssh").write_text(f'#!/bin/sh\necho "$*" >> "{tmp_path / "ssh-called"}"\nexit 255\n')
    (b / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    home, elsewhere = tmp_path / "home", tmp_path / "elsewhere"
    (home / "jobs").mkdir(parents=True)
    elsewhere.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(elsewhere)
    (home / "jobs" / "live.pid").write_text(f"{os.getpid()}\n")
    (home / "jobs" / "done.rc").write_text("0\n")
    (home / "jobs" / "mark").write_text("")
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert cli._detach_check_remote("box-self", ["jobs/live"]) == 1, out.getvalue()
        assert cli._detach_check_remote("box-self", ["jobs/done"]) == 0, out.getvalue()
    seen = detach._remote_sh("box-self", "jobs/live.pid", "jobs/mark")
    assert seen and seen["marker"] and seen.get("alive") == str(os.getpid()), seen
    assert not (tmp_path / "ssh-called").exists(), "the own host was asked over ssh"


def _json_checks_run(p, tid, name, env=None):
    """A run of task tid whose detached `ttp checks` (a sleeping group leader, TTP_TASK tid) left a JSON
    checks.pid as `ttp checks --detach` writes it: (run dir, run id, process)."""
    from ttp.runner import proc_start
    rid = _add_run(p, tid, name)
    d = p.runs / name
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True,
                            env={**os.environ, "TTP_TASK": str(tid), "TTP_PROJECT": str(p.base), **(env or {})})
    (d / "checks.pid").write_text(json.dumps({"pid": proc.pid, "started": proc_start(proc.pid), "ts": time.time()}))
    return d, rid, proc


def _stop_checks_group(proc):
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)


def test_detach_stop_stops_a_runs_detached_checks_named_by_their_json_pid_file(env, tmp_path):
    """`ttp detach --stop <run>/checks.rc` (no detach record; checks.pid is JSON) applies may_stop and stops
    the checks through end_checks; it never falls through to the launcher's plain-pid reader."""
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    d, rid, proc = _json_checks_run(p, tid, "checks-open")
    try:
        r = _ttp_run(p, "detach", "--stop", str(d / "checks.rc"))
        assert r.returncode == 3 and "refused" in r.stderr and "names no pid" not in r.stderr, r.stderr
        r = _ttp_run(p, "detach", "--stop", str(d / "checks.rc"), env={"TTP_TASK": str(tid + 1)})
        assert r.returncode == 3 and proc.poll() is None, r.stderr
        r = _ttp_run(p, "detach", "--stop", "checks", "--why", "stale", env={**_as(tid, rid), "TTP_RUN_DIR": str(d)})
        assert r.returncode == 0 and "stopped the detached checks of run checks-open" in r.stdout, (r.stdout, r.stderr)
        assert proc.wait(timeout=10) == -signal.SIGTERM
        (ev,) = _stopped_events(p)
        data = json.loads(ev["data"])
        assert (data["job"], data["stopped_by"], data["caller_run"], ev["task"]) == ("checks", tid, rid, tid), data
        assert "stale" in data["why"]
        r = _ttp_run(p, "detach", "--stop", str(d / "checks.rc"), env=_as(tid, rid))
        assert r.returncode == 0 and "no longer run" in r.stdout and len(_stopped_events(p)) == 1, r.stdout
    finally:
        _stop_checks_group(proc)
    done = p.db.add_task("done", "s", kind="work", tier="light", origin="user")
    p.db.update_task(done, status="done")
    d2, _, proc2 = _json_checks_run(p, done, "checks-done")
    try:
        r = _ttp_run(p, "detach", "--stop", str(d2 / "checks.rc"))
        assert r.returncode == 0 and f"task #{done} is done" in r.stdout and proc2.wait(timeout=10), r.stderr
    finally:
        _stop_checks_group(proc2)


def test_detach_stop_of_checks_refuses_a_group_of_another_project_or_task(env, tmp_path):
    """may_stop allows a done task's checks, but a group carrying another project's TTP_PROJECT or another
    task's TTP_TASK is never signalled (exit 3), from the CLI or the daemon's queued request."""
    from ttp import jobstop
    p = make(env)
    tid = p.db.add_task("done", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="done")
    other = tmp_path / "other-project"
    (other / "state").mkdir(parents=True)
    for i, extra in enumerate(({"TTP_PROJECT": str(other)}, {"TTP_TASK": str(tid + 7)})):
        d, _, proc = _json_checks_run(p, tid, f"checks-foreign{i}", env=extra)
        try:
            r = _ttp_run(p, "detach", "--stop", str(d / "checks.rc"))
            assert r.returncode == 3 and "nothing was signalled" in r.stderr and proc.poll() is None, r.stderr
            jobstop.request(p, d / "checks.rc", "", "")
            assert jobstop.serve_requests(p) == [] and proc.poll() is None
            assert jobstop.sweep_done_checks(p, time.time() + 3600) == [] and proc.poll() is None
        finally:
            _stop_checks_group(proc)


def _precheck_checks(p, code, head, env=None):
    """The daemon's review precheck of task `code` on `head` (state/prechecks/t<code>-<head>, no runs row)
    whose detached `ttp checks` (a sleeping group leader, TTP_TASK code) left a JSON checks.pid: (dir, process)."""
    from ttp.runner import proc_start
    d = p.state / "prechecks" / f"t{code}-{head}"
    d.mkdir(parents=True)
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True,
                            env={**os.environ, "TTP_TASK": str(code), "TTP_PROJECT": str(p.base), **(env or {})})
    (d / "checks.pid").write_text(json.dumps({"pid": proc.pid, "started": proc_start(proc.pid), "ts": time.time()}))
    return d, proc


def test_detach_stop_maps_a_review_precheck_folder_to_its_code_task(env, tmp_path):
    """`ttp detach --stop state/prechecks/t<id>-<head>/checks.rc` is not refused as outside a run: the folder
    maps to code task <id> (jobstop.task_of) and may_stop applies: refused while that task is open, stopped
    once it is done. A folder of another name, or a prechecks folder outside a project, is never stopped."""
    from ttp import jobstop
    p = make(env)
    code = p.db.add_task("c", "s", kind="code", tier="light", origin="user")
    p.db.update_task(code, status="running")
    d, proc = _precheck_checks(p, code, "ab12cd34ef56")
    odd = p.state / "prechecks" / "x-ab12cd34ef56"
    odd.mkdir()
    (odd / "checks.pid").write_text((d / "checks.pid").read_text())
    loose = tmp_path / "elsewhere" / "state" / "prechecks" / f"t{code}-ab12cd34ef56"
    loose.mkdir(parents=True)
    (loose / "checks.pid").write_text((d / "checks.pid").read_text())
    try:
        assert jobstop.task_of(p, d / "checks.rc")["id"] == code and jobstop.task_of(p, odd / "checks.rc") is None
        r = _ttp_run(p, "detach", "--stop", str(d / "checks.rc"))
        assert r.returncode == 3 and f"task #{code} is running" in r.stderr and proc.poll() is None, r.stderr
        r = _ttp_run(p, "detach", "--stop", str(odd / "checks.rc"))
        assert r.returncode == 3 and "no task of this project" in r.stderr and proc.poll() is None, r.stderr
        r = _ttp_run(p, "detach", "--stop", str(loose / "checks.rc"))
        assert r.returncode == 3 and proc.poll() is None, r.stderr
        p.db.update_task(code, status="done")
        r = _ttp_run(p, "detach", "--stop", str(d / "checks.rc"))
        assert r.returncode == 0 and f"task #{code} is done" in r.stdout, (r.stdout, r.stderr)
        assert "not the checks of a tt-project run" not in r.stderr and proc.wait(timeout=10)
        (ev,) = _stopped_events(p)
        assert ev["task"] == code
    finally:
        _stop_checks_group(proc)


def test_the_sweep_ends_a_review_precheck_once_no_review_reads_it(env, tmp_path):
    """sweep_prechecks (in the daemon's jobstop.sweep) stops a review precheck's live checks once no review
    labelled precheck:<name> is queued, running or waiting (one in the push queue, done, or none at all),
    past the grace; it keeps those inside the grace, those an open task's hand-off waits on and those whose
    checks.rc is written. open_precheck agrees: a review in the push queue no longer owns its lane."""
    from ttp import checklane, jobstop
    p = make(env)
    later = time.time() + 3600

    def task(kind, status, **kw):
        t = p.db.add_task(kind, "s", kind=kind, tier="light", origin="user", **kw)
        p.db.update_task(t, status=status)
        return t

    procs = []
    try:
        code = task("code", "done")
        d_read, pr_read = _precheck_checks(p, code, "aaaaaaaaaaaa")
        rev = task("review", "queued", labels=["precheck:" + d_read.name])
        orphan = task("code", "done")
        d_orphan, pr_orphan = _precheck_checks(p, orphan, "bbbbbbbbbbbb")
        named = task("code", "done")
        d_named, pr_named = _precheck_checks(p, named, "cccccccccccc")
        w = task("work", "waiting")
        p.db.update_task(w, result=json.dumps({"status": "waiting", "retry_when": f"ttp checks --result {d_named}"}))
        ending = task("code", "done")
        d_ending, pr_ending = _precheck_checks(p, ending, "dddddddddddd")
        (d_ending / "checks.rc").write_text("0\n")
        procs = [pr_read, pr_orphan, pr_named, pr_ending]
        assert jobstop.sweep_prechecks(p) == [] and all(x.poll() is None for x in procs), "stopped inside the grace"
        (r,) = jobstop.sweep_prechecks(p, later)
        assert r["task"] == orphan and d_orphan.name in r["text"] and pr_orphan.wait(timeout=10)
        holder = f"run {d_read.name}"
        for status in checklane.PRECHECK_READERS:
            p.db.update_task(rev, status=status)
            assert checklane.open_precheck(p.db, str(code), holder), status
            assert jobstop.sweep_prechecks(p, later) == [] and pr_read.poll() is None, status
        p.db.update_task(rev, status="pushing")
        assert not checklane.open_precheck(p.db, str(code), holder), "a review in the push queue reads it no more"
        rs = [x for x in jobstop.sweep(p, later) if x.get("job") == "checks"]
        assert [x["task"] for x in rs] == [code] and pr_read.wait(timeout=10), rs
        assert pr_named.poll() is None and pr_ending.poll() is None
        assert jobstop.sweep_prechecks(p, later) == [] and len(_stopped_events(p)) == 2
    finally:
        for x in procs:
            _stop_checks_group(x)


def test_the_sweep_ends_a_done_tasks_detached_checks_only_when_nothing_waits_on_them(env, tmp_path):
    """sweep_done_checks stops a done task's live detached checks past the grace, and keeps those an open
    task's `ttp checks --result` wait, the task's own hand-off or a joined checks run still consume, those
    of a live task, those inside the grace and those whose checks.rc is written."""
    from ttp import checkjoin, jobstop
    p = make(env)
    later = time.time() + 3600

    def task(status, result=None):
        t = p.db.add_task(status, "s", kind="work", tier="light", origin="user")
        p.db.update_task(t, status=status, **({"result": json.dumps(result)} if result else {}))
        return t

    procs = []
    try:
        stale = task("done")
        d_stale, _, pr_stale = _json_checks_run(p, stale, "c-stale")
        waited = task("done")
        d_waited, _, pr_waited = _json_checks_run(p, waited, "c-waited")
        task("waiting", {"status": "waiting", "retry_when": f"ttp checks --result {d_waited}"})
        later_wait = task("done")
        d_then, _, pr_then = _json_checks_run(p, later_wait, "c-then")
        task("waiting", {"status": "waiting", "retry_when": "landed:#1",
                         "then": [{"retry_when": f"ttp checks --result {d_then}/"}]})
        own = task("done")
        d_own, _, pr_own = _json_checks_run(p, own, "c-own")
        p.db.update_task(own, result=json.dumps({"status": "done", "on_pass_cmd": f"ttp checks --settle {d_own} --head x"}))
        joined = task("done")
        d_joined, _, pr_joined = _json_checks_run(p, joined, "c-joined")
        lead = checkjoin.register(p.state, task=str(joined), head="h", commit_ts=0, key="k", group="g",
                                  run=d_joined.name, arrived=0, pid=pr_joined.pid)
        follower = checkjoin.register(p.state, task="99", head="h", commit_ts=0, key="k", group="g2", run="r",
                                      arrived=0, pid=os.getpid())
        checkjoin.update(follower, phase=checkjoin.JOINED, follows=checkjoin.read(lead)["id"])
        live = task("waiting")
        d_live, _, pr_live = _json_checks_run(p, live, "c-live")
        ending = task("done")
        d_ending, _, pr_ending = _json_checks_run(p, ending, "c-ending")
        (d_ending / "checks.rc").write_text("0\n")
        procs = [pr_stale, pr_waited, pr_then, pr_own, pr_joined, pr_live, pr_ending]
        assert jobstop.sweep_done_checks(p) == [] and all(x.poll() is None for x in procs), "stopped inside the grace"
        (r,) = jobstop.sweep_done_checks(p, later)
        assert r["task"] == stale and "nothing waits on its checks" in r["text"] and pr_stale.wait(timeout=10)
        assert all(x.poll() is None for x in procs[1:]), [x.poll() for x in procs]
        checkjoin.update(follower, outcome={"passed": True})   # the follower has its result: nothing waits now
        (r,) = jobstop.sweep_done_checks(p, later)
        assert r["task"] == joined and pr_joined.wait(timeout=10)
        assert jobstop.sweep_done_checks(p, later) == [] and len(_stopped_events(p)) == 2
    finally:
        for x in procs:
            _stop_checks_group(x)


def _requeued_proc(p, project=None):
    """A process a detached job re-spawned in a session of its own: (proc, its start ticks)."""
    from ttp.runner import proc_start
    e = {**os.environ, "TTP_PROJECT": str(project or p.base)}
    proc = subprocess.Popen(["sleep", "300"], env=e, start_new_session=True)
    for _ in range(50):
        if (tok := proc_start(proc.pid)) is not None:
            return proc, tok
        time.sleep(0.05)
    raise AssertionError("no start ticks")


def _reap(*procs):
    for x in procs:
        x.kill()
        x.wait()


def test_detach_stop_also_stops_the_groups_a_job_lists_in_its_requeue_pid_file(env, tmp_path):
    """`<rc>.requeue-pid` (`<pid> <start ticks>` lines): `ttp detach --stop` of the job also kills each listed
    process's group while its start ticks match; a reused pid (other ticks), another project's process, an
    exited pid and garbled lines are skipped without a word."""
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    run_dir = p.runs / f"rq-{tid}"
    run_dir.mkdir(parents=True)
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 60, "ok", str(run_dir)))
    run = p.db.one("SELECT id FROM runs WHERE dir=?", (str(run_dir),))["id"]
    r = _ttp_run(p, "detach", "job", "--", "sleep", "300", env={"TTP_RUN_DIR": str(run_dir)})
    assert r.returncode == 0, r.stderr
    rc = (run_dir / "job.rc").resolve()
    pgid = int(rc.with_suffix(".started").read_text().split()[0])
    (mine, tok), (reused, tok2) = _requeued_proc(p), _requeued_proc(p)
    foreign, tok3 = _requeued_proc(p, project=tmp_path / "other" / "tt-project")
    try:
        Path(str(rc) + ".requeue-pid").write_text(
            f"{mine.pid} {tok}\n{reused.pid} {int(tok2) + 1}\n{foreign.pid} {tok3}\n{_dead_pid()} 5\n"
            f"garbage\n{mine.pid}\n-3 x\n\n")
        r = _ttp_run(p, "detach", "--stop", "job", env={**_as(tid, run), "TTP_RUN_DIR": str(run_dir)})
        assert r.returncode == 0 and "stopped detached job job" in r.stdout, r.stderr
        assert f"requeued groups stopped: {mine.pid}" in r.stdout, r.stdout
        assert mine.wait(5) == -signal.SIGKILL
        assert reused.poll() is None and foreign.poll() is None
        ev = json.loads(_stopped_events(p)[-1]["data"])
        assert ev["requeued"] == [mine.pid]
    finally:
        _end_group(pgid)
        _reap(mine, reused, foreign)


def test_a_missing_or_garbled_requeue_pid_file_stops_nothing_extra(env, tmp_path):
    from ttp import jobstop
    p = make(env)
    rc = p.runs / "rq" / "job.rc"
    rc.parent.mkdir(parents=True)
    assert jobstop.requeued(p, rc) == [] and jobstop.end_requeued(p, rc, "test") == []
    proc, tok = _requeued_proc(p)
    try:
        side = Path(str(rc) + ".requeue-pid")
        for text in (f"{proc.pid}\n", f"{proc.pid} {tok} extra\n", f"x{proc.pid} {tok}\n", f"{proc.pid} t{tok}\n",
                     "\0\xff\n", f"{proc.pid} {int(tok) - 1}\n", f"1 {tok}\n"):
            side.write_text(text)
            assert jobstop.requeued(p, rc) == [], text
        side.write_text(f"{proc.pid} {tok}\n")
        assert jobstop.requeued(p, rc) == [(proc.pid, proc.pid)]
        side.unlink()
        side.mkdir()   # unreadable as a file
        assert jobstop.requeued(p, rc) == []
        assert proc.poll() is None
    finally:
        _reap(proc)


def test_stop_pids_also_stops_the_requeued_groups_of_the_listed_processes_job(env, tmp_path):
    """`ttp detach --stop-pids`: a listed process that runs in a detached job (TTP_DETACH_RC) takes the
    groups that job's `<rc>.requeue-pid` lists with it, only those whose start ticks match."""
    p = make(env)
    tid = p.db.add_task("driver", "s", kind="work", tier="light", origin="user")
    res = p.root / "results" / f"t{tid}"
    res.mkdir(parents=True)
    rc = res / "drive.rc"
    e = {**os.environ, "TTP_TASK": str(tid), "TTP_TASK_ID": str(tid), "TTP_PROJECT": str(p.base),
         "TTP_DETACH_RC": str(rc)}
    driver = subprocess.Popen(["sleep", "300"], env=e, start_new_session=True)
    (child, tok), (reused, tok2) = _requeued_proc(p), _requeued_proc(p)
    try:
        time.sleep(0.2)
        Path(str(rc) + ".requeue-pid").write_text(f"{child.pid} {tok}\n{reused.pid} {int(tok2) + 7}\n")
        f = _pidfile(res / "driver.pids", driver.pid)
        r = _ttp_run(p, "detach", "--stop-pids", str(f), env={"TTP_TASK": str(tid), "TTP_PIDNS": "", "TTP_CGROUP": ""})
        assert r.returncode == 0 and f"requeued groups stopped: {child.pid}" in r.stdout, r
        assert driver.wait(5) == -signal.SIGTERM and child.wait(5) == -signal.SIGKILL
        assert reused.poll() is None
    finally:
        _reap(driver, child, reused)


def _check_report(p, marker=None):
    """A check's report folder (checkdirs.reports, its TTP_RUN_DIR) whose allocation's owner.json names
    `marker`, as a push or `ttp checks` run leaves them."""
    from ttp import checkdirs
    name = secrets.token_hex(16)
    alloc = p.state / checkdirs.FOLDER / name
    alloc.mkdir(parents=True)
    (alloc / checkdirs.OWNER).write_text(json.dumps({"marker": str(marker) if marker else None}))
    report = p.state / "check-reports" / name
    report.mkdir(parents=True)
    return report


def _sandbox_verdict(sandbox, tries=5):
    """sandbox.status() once its probe answered: on a loaded host a probe can time out, which is no
    verdict (status probes again on the next call) and makes sandbox.wrap run the command unsandboxed."""
    for _ in range(tries):
        pre, why = sandbox.status()
        if pre or sandbox.TIMED_OUT not in why:
            return pre, why
    pytest.fail(f"the sandbox probe timed out {tries} times: {why}")


def test_sandbox_verdict_asks_again_after_a_probe_that_timed_out(monkeypatch):
    """A timed-out probe makes sandbox.wrap run a command as it is (the flake of the sandboxed checks test:
    its check ran unsandboxed, so `ttp detach` started the job itself); _sandbox_verdict waits for an answer."""
    from ttp import sandbox
    answers = iter([(None, f"unshare: {sandbox.TIMED_OUT}"), (None, f"bwrap: {sandbox.TIMED_OUT}"), (["/sb"], "")])
    monkeypatch.setattr(sandbox, "_STATE", {})
    monkeypatch.setattr(sandbox, "_probe", lambda: next(answers))
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    on = {sandbox.ENV: "1", "TTP_NO_DEVICE": ""}
    assert sandbox.wrap(["true"], True, on) == ["true"]
    assert _sandbox_verdict(sandbox) == (["/sb"], "")
    assert sandbox.wrap(["true"], True, on)[0] == "/sb"
    monkeypatch.setattr(sandbox, "_STATE", {})
    monkeypatch.setattr(sandbox, "_probe", lambda: (None, "bwrap: exit 1"))
    assert _sandbox_verdict(sandbox) == (None, "bwrap: exit 1")   # a real verdict: no sandbox here


def test_a_sandboxed_checks_detached_job_is_started_by_the_daemon_and_outlives_the_check(env, tmp_path, monkeypatch):
    """A push check run in the process sandbox (TTP_SANDBOX_CHECKS=1) starts `ttp detach gate -- ...` and
    exits 75. The job is queued (the sandbox would end it with the check), the daemon starts it outside,
    and it runs to its end and writes its .rc. It keeps the check's device masking, never the check's
    own fd and reaper variables."""
    from ttp import detach, sandbox
    monkeypatch.setenv("TTP_DEVICE_NODES", "")
    monkeypatch.setenv("TTP_CHECKS_TMP_FD", "8")      # the daemon's own, as one started from a check has:
    monkeypatch.setenv("TTP_CHECK_SANDBOX", "outer")  # never passed on to the job
    p = make(env)
    marker = p.state / "pushes" / "m1.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"status": "running"}))
    report = _check_report(p, marker)
    hb, go, seen = tmp_path / "hb", tmp_path / "go", tmp_path / "env"
    job = f"env >{shlex.quote(str(seen))}; while [ ! -e {shlex.quote(str(go))} ]; do echo beat >>{shlex.quote(str(hb))}; sleep 0.05; done"
    check = f"{shlex.quote(sys.executable)} {shlex.quote(str(TTP))} detach gate -- sh -c {shlex.quote(job)}; exit 75"
    e = {**os.environ, "TTP_PROJECT": str(p.base), "TTP_RUN_DIR": str(report), "TTP_SANDBOX_CHECKS": "1",
         "TTP_NO_DEVICE": "1", "TT_VISIBLE_DEVICES": "", "TTP_CHECKS_TMP_FD": "9", "TTP_CHECK_SANDBOX": "tok"}
    for k in ("TTP_PIDNS", "TTP_CGROUP", "TTP_DETACHED"):
        e.pop(k, None)
    if not sandbox.inside():   # else the check runs in this one, which `ttp detach` sees
        pre, why = _sandbox_verdict(sandbox)
        if pre is None:
            pytest.skip(f"no process sandbox works on this host: {why}")
    cmd = sandbox.wrap(["sh", "-c", check], True, e)
    assert sandbox.inside() or cmd[:len(pre)] == pre, "the check must run in the sandbox"
    r = subprocess.run(cmd, env=e, cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    assert r.returncode == 75 and "queued" in r.stdout, r
    rc = report / "gate.rc"
    detach.tend(p)   # the daemon's tick
    assert _wait_for_path(hb), "the job never ran outside the check"
    go.touch()
    assert _wait_for_path(rc) and rc.read_text().strip() == "0", (r.stdout, r.stderr)
    got = dict(line.split("=", 1) for line in seen.read_text().splitlines() if "=" in line)
    assert got.get("TTP_NO_DEVICE") == "1" and got.get("TT_VISIBLE_DEVICES") == ""
    assert "TTP_CHECKS_TMP_FD" not in got and "TTP_CHECK_SANDBOX" not in got
    assert _ttp_run(p, "detach", "--check", str(rc)).returncode == 0
    detach.tend(p)   # the next tick
    assert not list((p.state / detach.BOUND).glob("*.json")), "an ended job's bound record goes"


def test_an_unsandboxed_checks_detached_job_is_queued_so_the_checks_reaper_never_ends_it(env, tmp_path, monkeypatch):
    """A push check or `ttp checks` command run with no process sandbox (none works on the host, or none is
    asked for) starts `ttp detach gate -- ...`. Started there, the job would carry the check's reaper token
    and die once the check ended (checkreap): it is queued instead, the daemon starts it without the token,
    and a reap of the check's token leaves it running to its end."""
    from ttp import checkreap, cli, detach, push, sandbox
    monkeypatch.setenv("TTP_DEVICE_NODES", "")
    p = make(env)
    report = _check_report(p)
    hb, go = tmp_path / "hb", tmp_path / "go"
    tok = checkreap.token()
    for k, v in {"TTP_PROJECT": str(p.base), "TTP_RUN_DIR": str(report), checkreap.MARKER: tok}.items():
        monkeypatch.setenv(k, v)
    for k in ("TTP_PIDNS", "TTP_CGROUP", "TTP_DETACHED", "TTP_LOCKS_HELD", "TTP_SANDBOX_CHECKS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(push, "_boxed", lambda: False)
    monkeypatch.setattr(detach, "foreign_pidns", lambda: False)
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    monkeypatch.chdir(tmp_path)
    rc = report / "gate.rc"
    try:
        try:
            cli.main(["detach", "gate", "--", "sh", "-c",
                      f"while [ ! -e {shlex.quote(str(go))} ]; do echo beat >>{shlex.quote(str(hb))}; sleep 0.05; done"])
        except SystemExit as e:
            assert not e.code, e
        assert detach.queued(rc).exists() and not detach.started(rc).exists(), "started inside the check"
        detach.tend(p)
        assert _wait_for_path(hb), "the daemon never started it"
        assert checkreap.reap(tok, grace_s=1) == (0, 0), "the job carries the check's reaper token"
    finally:
        go.touch()
    assert _wait_for_path(rc) and rc.read_text().strip() == "0"


def test_a_checks_queued_job_ends_at_its_bound_or_once_its_owner_marker_is_gone(env, tmp_path, monkeypatch):
    """No task waits on a job a check queued: the daemon ends it at its bound (CHECK_JOB_S from its queueing)
    or once the push marker or checks run that owns it is gone, whichever comes first, with the reason in its
    .lost. Before either it keeps running through every tick."""
    from ttp import detach, locks
    monkeypatch.setenv("TTP_DEVICE_NODES", "")
    p = make(env)
    loop = ["sh", "-c", "while :; do sleep 0.05; done"]
    jobs = {}
    for name in ("gone", "late"):
        marker = tmp_path / f"{name}.json"
        marker.write_text("{}")
        report = _check_report(p, marker)
        rc = report / f"{name}.rc"
        detach.enqueue(p.state, rc, report / f"{name}.log", loop, str(tmp_path), dict(os.environ),
                       bound=detach.check_bound(str(marker)))
        jobs[name] = (rc, marker)
    detach.tend(p)
    for rc, _ in jobs.values():
        assert _wait_for_path(detach.started(rc))
    detach.tend(p)
    assert all(detach.state(rc)[0] == "running" for rc, _ in jobs.values())
    jobs["gone"][1].unlink()
    detach.tend(p)
    rc = jobs["gone"][0]
    assert detach.state(rc)[0] == "lost" and "is gone" in detach.lost(rc).read_text()
    rc = jobs["late"][0]
    assert detach.state(rc)[0] == "running"
    detach.bound(p, now=time.time() + detach.CHECK_JOB_S + 1)
    assert detach.state(rc)[0] == "lost" and "bound" in detach.lost(rc).read_text()
    for rc, _ in jobs.values():
        deadline = time.time() + 20
        while time.time() < deadline and not locks.any_free([locks.job_lock(rc)]):
            time.sleep(0.05)
        assert locks.any_free([locks.job_lock(rc)]), "the stopped job's processes are gone"
    assert not list((p.state / detach.BOUND).glob("*.json"))


def test_a_cpu_only_checks_queued_job_starts_in_no_namespace(env, tmp_path, monkeypatch):
    """A check that holds no device (TTP_NO_DEVICE=1) queues a job: the daemon starts it as it is, in no
    namespace (it sees what the host sees), with the check's TTP_NO_DEVICE and device mask kept."""
    from ttp import detach, locks, sandbox
    dev = tmp_path / "dev-tt"
    dev.mkdir()
    (dev / "0").write_text("")
    monkeypatch.setenv("TTP_DEVICE_NODES", str(dev))
    p = make(env)
    report = _check_report(p)
    out, rc = tmp_path / "seen", report / "plain.rc"
    job = f"ls -A {shlex.quote(str(dev))} >{shlex.quote(str(out))}; echo \"$TTP_NO_DEVICE:$TT_VISIBLE_DEVICES:\" >>{shlex.quote(str(out))}"
    e = {k: v for k, v in os.environ.items() if k != sandbox.ENV}
    detach.enqueue(p.state, rc, report / "plain.log", ["sh", "-c", job], str(tmp_path),
                   {**e, "TTP_NO_DEVICE": "1", "TT_VISIBLE_DEVICES": ""}, bound=detach.check_bound(None))
    detach.tend(p)
    # A start on a loaded host can take longer than a fixed 20 s: wait while the job holds its lock (the
    # wrapper writes the rc before it lets go), bounded only against a hang.
    assert _wait_for(lambda: rc.exists() or locks.any_free([locks.job_lock(rc)]), timeout=300)
    assert rc.exists() and rc.read_text().strip() == "0", (report / "plain.log").read_text()
    assert out.read_text().splitlines() == ["0", "1::"]


def test_a_detach_in_a_private_pid_namespace_is_queued_and_records_the_host_pid(env, tmp_path):
    """`ttp detach` run in a PID namespace of its own that no TTP_PIDNS marks (a check that started it with
    setsid would record $$, the namespace's pid) queues the job: the daemon starts it in the host's
    namespace, it outlives the namespace, and its record holds the wrapper's host pid and start time."""
    from ttp import detach, locks
    unshare = ["unshare", "--user", "--map-root-user", "--pid", "--fork"]
    if shutil.which("unshare") is None or subprocess.run([*unshare, "true"], capture_output=True).returncode:
        pytest.skip("no private PID namespace on this host")
    p = make(env)
    rd = p.state / "runs" / "1"
    rd.mkdir(parents=True)
    hb, go = tmp_path / "hb", tmp_path / "go"
    job = f"while [ ! -e {shlex.quote(str(go))} ]; do echo beat >>{shlex.quote(str(hb))}; sleep 0.05; done"
    e = {**os.environ, "TTP_PROJECT": str(p.base), "TTP_RUN_DIR": str(rd)}
    for k in ("TTP_PIDNS", "TTP_CGROUP", "TTP_DETACHED", "TTP_LOCKS_HELD"):
        e.pop(k, None)
    r = subprocess.run([*unshare, sys.executable, str(TTP), "detach", "gate", "--", "sh", "-c", job], env=e,
                       cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "queued" in r.stdout, r
    rc = rd / "gate.rc"
    detach.tend(p)
    assert _wait_for_path(hb), "the job never ran outside the namespace"
    pid = detach._entry(rc)["pid"]
    assert pid == int(detach.started(rc).read_text().split()[0])
    assert detach._entry(rc)["pid_start"] == locks.start_time(pid) and detach.job_pid(rc) == pid
    go.touch()
    assert _wait_for_path(rc) and rc.read_text().strip() == "0"


def test_a_jobs_recycled_pid_counts_as_gone(env, tmp_path, monkeypatch):
    """A registry pid that now names another process (its start time differs from the recorded one, or with
    none recorded its arguments do not name the job) is no pid of the job: the bound and a remote verdict
    never signal it, and a stale wrapper pid in <name>.started is treated the same."""
    from ttp import detach, locks
    p = make(env)
    report = _check_report(p, tmp_path / "gone.json")
    rc = report / "gate.rc"
    other = subprocess.Popen(["sleep", "60"])
    try:
        born = locks.start_time(other.pid)
        for entry, ok in (({"pid_start": born}, True), ({"pid_start": str(int(born) + 1)}, False), ({}, False)):
            (report / "detached.json").write_text(json.dumps([{"rc": str(rc), "pid": other.pid, **entry}]))
            assert detach.job_pid(rc) == (other.pid if ok else 0), entry
        detach.started(rc).write_text(f"{other.pid}\n")
        (report / "detached.json").write_text(json.dumps([{"rc": str(rc), "pid": None}]))
        assert detach.job_pid(rc) == 0, "a .started pid whose process is not the job's wrapper"
        killed = []
        monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append(pid))
        lock = locks.try_take([locks.job_lock(rc)], "test", "held")   # the job looks alive
        try:
            detach.end_remote(rc, "its remote side is gone")
            (p.state / detach.BOUND).mkdir(parents=True, exist_ok=True)
            (p.state / detach.BOUND / "q.json").write_text(json.dumps({**detach.check_bound(None), "rc": str(rc)}))
            detach.bound(p, now=time.time() + detach.CHECK_JOB_S + 1)
        finally:
            lock.close()
        assert killed == [], "a recycled pid was signalled"
        assert other.poll() is None
    finally:
        _reap(other)


def test_a_checks_detached_job_keeps_its_files_after_the_push_exits_busy_and_its_scratch_goes(env, tmp_path,
                                                                                               monkeypatch):
    """A push check (cwd and TMPDIR in the push's check scratch) runs `ttp detach gate -- ...` and the push
    exits 75; its check scratch goes before the daemon starts the job. The job still starts, in its own
    work dir under the project's state ($TTP_JOB_DIR, its cwd once the check's is gone), with a TMPDIR of its
    own outside the check's: it writes files there and finds them later. Both go once it ends."""
    from ttp import detach, sandbox
    monkeypatch.setenv("TTP_DEVICE_NODES", "")
    # The check must run sandboxed, so its `ttp detach` queues the job: a probe slowed by a loaded host
    # is waited out, never read as "no sandbox here" (the job would then start at once, in the scratch).
    monkeypatch.setattr(sandbox, "PROBE_S", 600)
    monkeypatch.setattr(sandbox, "_STATE", {})
    if not (sandbox.inside() or sandbox.status()[0]):
        pytest.skip(f"no PID-namespace sandbox here: {sandbox.status()[1]}")
    p = make(env)
    marker = p.state / "pushes" / "m1.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"status": "running"}))
    report = _check_report(p, marker)
    check_tmp = tmp_path / "check-scratch"
    head = check_tmp / "head"
    head.mkdir(parents=True)
    seen = tmp_path / "seen"
    # As a gate does: stage a head dir under $TMPDIR and copies under $TTP_JOB_DIR, then enter the head dir.
    job = ('set -e; s=$PWD; d=$(mktemp -d "$TMPDIR/x.XXXXXX"); mkdir "$d/head"; echo staged >"$d/head/f"; '
           'mkdir -p "$TTP_JOB_DIR/w"; cp "$d/head/f" "$TTP_JOB_DIR/w/f"; cd "$d/head"; cat "$TTP_JOB_DIR/w/f" f >'
           + shlex.quote(str(seen)) + '; printf "%s\\n%s\\n%s\\n" "$s" "$TTP_JOB_DIR" "$TMPDIR" >>'
           + shlex.quote(str(seen)))
    check = f"{shlex.quote(sys.executable)} {shlex.quote(str(TTP))} detach gate -- sh -c {shlex.quote(job)}; exit 75"
    e = {**os.environ, "TTP_PROJECT": str(p.base), "TTP_RUN_DIR": str(report), "TTP_SANDBOX_CHECKS": "1",
         "TTP_NO_DEVICE": "1", "TT_VISIBLE_DEVICES": "", "TTP_CHECK_SANDBOX": "tok",
         **dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(check_tmp))}
    for k in ("TTP_PIDNS", "TTP_CGROUP", "TTP_DETACHED"):
        e.pop(k, None)
    r = subprocess.run(sandbox.wrap(["sh", "-c", check], True, e), env=e, cwd=str(head),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 75 and "queued" in r.stdout, r
    shutil.rmtree(check_tmp)   # the push that exited busy removes its check scratch
    rc = report / "gate.rc"
    detach.tend(p)
    assert _wait_for_path(rc, 300), detach.lost(rc).read_text() if detach.lost(rc).exists() else r
    log = (report / "gate.log").read_text()
    assert rc.read_text().strip() == "0", log
    staged, staged2, cwd, work, tmp = seen.read_text().splitlines()
    assert staged == staged2 == "staged"
    assert Path(work) == detach.scratch(rc) / detach.WORK and Path(cwd).resolve() == Path(work).resolve()
    assert Path(work).is_relative_to(p.state) and not Path(tmp).is_relative_to(check_tmp)
    assert "started in its work dir" in log
    assert not Path(work).exists() and not Path(tmp).exists(), "removed once the job ended"


def test_a_job_detached_from_a_ttp_checks_command_keeps_a_temp_dir_after_the_checks_run_cleans_up(
        env, tmp_path, monkeypatch):
    """A `ttp checks` command starts `ttp detach gate -- ...` and ends; the checks run exits and removes its
    scratch, TMPDIR included. The job, still running, can make temp dirs under its own TMPDIR and use its
    work dir ($TTP_JOB_DIR), neither of them inside the checks run's TMPDIR."""
    from ttp import cli, detach
    p, repo, run, git, tmp = _clean_repo(env, tmp_path, monkeypatch)
    go, seen, outer = tmp_path / "go", tmp_path / "seen", tmp_path / "outer"
    job = (f'while [ ! -e {shlex.quote(str(go))} ]; do sleep 0.05; done; d=$(mktemp -d "$TMPDIR/x.XXXXXX") && '
           f'touch "$d/f" "$TTP_JOB_DIR/f" && printf "%s\\n%s\\n%s\\n" "$d" "$TMPDIR" "$TTP_JOB_DIR" >{shlex.quote(str(seen))}')
    cmd = (f'echo "$TMPDIR" >{shlex.quote(str(outer))}; TTP_PROJECT={shlex.quote(str(p.base))} '
           f'{shlex.quote(sys.executable)} {shlex.quote(str(TTP))} detach gate -- sh -c {shlex.quote(job)}')
    cli.main(["checks", "--detach", "--fresh", "--", cmd])
    assert _wait_precheck(run) == "0", (run / cli.CHECKS_OUT).read_text()
    check_tmp = Path(outer.read_text().strip())
    found = [f for f in tmp_path.rglob("gate.*") if f.suffix in (".log", ".queued")]
    assert found, "the job was neither started nor queued"
    rc = found[0].with_suffix(".rc")
    _wait_for(lambda: not check_tmp.exists(), 300)
    assert not check_tmp.exists(), "the checks run removed its TMPDIR"
    detach.tend(p)   # starts it, were it queued
    go.touch()
    assert _wait_for_path(rc, 300), (detach.lost(rc).read_text() if detach.lost(rc).exists() else
                                     rc.with_suffix(".log").read_text() if rc.with_suffix(".log").exists() else "")
    assert rc.read_text().strip() == "0", rc.with_suffix(".log").read_text()
    made, job_tmp, work = seen.read_text().splitlines()
    assert Path(made).parent == Path(job_tmp) and not Path(job_tmp).is_relative_to(check_tmp)
    assert Path(work).is_relative_to(p.state) and not Path(work).exists() and not Path(job_tmp).exists()


def test_the_sweep_removes_the_work_dir_of_a_checks_job_gone_without_its_wrapper_cleaning_up(env, tmp_path):
    """A check's job killed with its wrapper (or a host restart) leaves its record under detach-tmp: tend's sweep
    removes it, and its temp dir, once the job is gone; a running job's (its lock held) stays."""
    from ttp import detach, locks
    p = make(env)
    report = _check_report(p)
    gone, live = report / "gone.rc", report / "live.rc"
    for rc in (gone, live):
        (detach.scratch(rc) / detach.WORK).mkdir(parents=True)
    detach.lost(gone).write_text("killed\n")
    held = locks.try_take([locks.job_lock(live)], "test", "test")
    try:
        detach.sweep(p.state)
        assert not detach.scratch(gone).exists() and detach.scratch(live).is_dir()
    finally:
        held.close()
    detach.sweep(p.state)
    assert not detach.scratch(live).exists()
