# Test-pool shard tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new `ttp pool-shard` tests here.
from __future__ import annotations


def _pool_home(monkeypatch, tmp_path):
    """Its own home for stall marks and the measured seconds per file; no push check, no settings."""
    from ttp import checkwait, poolshard, project
    monkeypatch.setattr(project, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(project, "load_account_settings", lambda: {})
    for name in ("TTP_POOL_SHARD_STALL_S", "TTP_POOL_SHARD_STALL_FLOOR_S", checkwait.ENV):
        monkeypatch.delenv(name, raising=False)
    return poolshard


def _fake_pool(tmp_path):
    """A pool command: on pool-stuck its job never leaves the queue (a 'waiting' line every 0.1 s, no file
    done, as the stalled shards printed), unless `fixed` exists; elsewhere it runs its 3 files."""
    script = tmp_path / "pool-run"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$1 $$" >> {tmp_path}/started\n'
        f'if [ "$1" = pool-stuck ] && [ ! -e {tmp_path}/fixed ]; then\n'
        '  while :; do echo "pool: waiting for a pool slot"; sleep 0.1; done\n'
        "fi\n"
        'for f in a b c; do echo "PASS 0.1 s  tests/test_$f.py on $1"; done\n')
    script.chmod(0o755)
    return [str(script), "{pool}"]


def _shard_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        st = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:   # gone between the kill probe and the read
        return False
    return st.rsplit(")", 1)[1].split()[0] != "Z"


def test_pool_shard_with_no_file_progress_is_ended_its_job_cancelled_and_its_files_rerun_on_the_next_pool(
        tmp_path, monkeypatch, capsys):
    """The stall of a push check's head run: 2 shards on a pool printed 'waiting' lines for 20 min with 0 files
    done, so an output watcher never saw a stall. Here a shard with no file done in its window is ended (no
    process left), its batch job cancelled, the stall logged with shard id and pool alias, and its files run
    on the next pool, where they pass: the shard exits 0 through `ttp pool-shard`."""
    import signal as signal_
    ps = _pool_home(monkeypatch, tmp_path)
    monkeypatch.setattr(signal_, "signal", lambda *a: None)   # the CLI's TERM handler stays out of pytest
    cmd = _fake_pool(tmp_path)
    cancel = f'echo "$TTP_POOL $TTP_POOL_SHARD" >> {tmp_path}/cancelled'
    monkeypatch.setattr(ps, "GRACE_S", 1.0)
    rc = _ttp("pool-shard", "--shard", "head.1", "--pool", "pool-stuck", "--pool", "pool-ok", "--window", "1",
              "--cancel", cancel, "--", *cmd)
    out = capsys.readouterr()
    assert rc == 0, out
    assert "PASS 0.1 s  tests/test_c.py on pool-ok" in out.out and "on pool-stuck" not in out.out, out.out
    assert "shard head.1 on pool-stuck STALLED: no file done in 1 s (none started); ended" in out.err, out.err
    assert "re-running its files on pool-ok after a stall on pool-stuck" in out.err, out.err
    assert (tmp_path / "cancelled").read_text() == "pool-stuck head.1\n"
    stuck_pid = int((tmp_path / "started").read_text().split("\n")[0].split()[1])
    assert not _shard_alive(stuck_pid), "the stalled shard's process is gone"
    log = (ps._dir() / ps.LOG).read_text()
    assert "\tstall\tpool-stuck\thead.1\tno file done in 1 s" in log, log
    assert ps.marked("pool-stuck") and not ps.marked("pool-ok")
    # every pool stalls: no verdict, STALLED, never an endless rerun
    rc = ps.run(cmd, ["pool-stuck"], "head.2", win=0.5, tell=lambda _: None, out=io.StringIO(), grace=1.0)
    assert rc == ps.STALLED


def test_pool_shard_stall_mark_skips_the_pool_on_later_gates_until_it_shows_progress(tmp_path, monkeypatch):
    """A stalled pool sits out the next gates (its command never starts there) while its mark is fresh;
    after MARK_TTL_S it gets a try again, and its first file done clears the mark. A pool list that is all
    freshly marked still runs, on the pool marked longest ago."""
    ps = _pool_home(monkeypatch, tmp_path)
    cmd, told = _fake_pool(tmp_path), []
    ps.mark("pool-stuck", "head.1", "no file done in 600 s (none started)", now=time.time() - 60)
    started = tmp_path / "started"
    assert ps.run(cmd, ["pool-stuck", "pool-ok"], "head.1", win=5, tell=told.append, out=io.StringIO()) == 0
    assert started.read_text().split()[0] == "pool-ok", "the marked pool never got the shard"
    assert any(t.startswith("pool-stuck skipped: stalled (no file done in 600 s") for t in told), told
    # all marked: the one marked longest ago runs it
    ps.mark("pool-ok", "head.9", "no file done in 600 s (none started)")
    assert ps.order(["pool-ok", "pool-stuck"]) == (["pool-stuck"], ["pool-ok"])
    ps.clear("pool-ok")
    # the mark ages out: pool-stuck is tried again; fixed now, its first file clears the mark
    (tmp_path / "fixed").touch()
    m = json.loads(ps._mark_path("pool-stuck").read_text())
    ps.project.durable_write(ps._mark_path("pool-stuck"), json.dumps({**m, "since": time.time() - ps.MARK_TTL_S - 1}))
    started.unlink()
    told.clear()
    out = io.StringIO()
    assert ps.run(cmd, ["pool-stuck", "pool-ok"], "head.2", win=5, tell=told.append, out=out) == 0
    assert started.read_text().split()[0] == "pool-stuck" and "on pool-stuck" in out.getvalue()
    assert "pool-stuck made progress again; its stall mark is cleared" in told, told
    assert not ps._mark_path("pool-stuck").exists() and "\tclear\tpool-stuck\t" in (ps._dir() / ps.LOG).read_text()


def test_pool_shard_time_before_its_first_file_is_a_test_pool_wait_counted_against_the_push_check_limit(
        tmp_path, monkeypatch):
    """Time before a shard's first file is a test-pool wait of its push check: the push log shows it live and
    once the check ends, and it counts against the check's time limit (delivery.push_checks_timeout_s counts
    wall time, test-pool waits included). Under the limit the shard passes; a shard whose job queues past the
    limit is ended at it, the log saying it was waiting for a test-pool slot."""
    from ttp import checkwait, push
    _pool_home(monkeypatch, tmp_path)
    monkeypatch.setattr(push, "check_queue_s", 60.0)
    monkeypatch.setattr(push, "pool_wait_s", 4 * 3600.0)
    monkeypatch.setattr(push, "CHECK_POLL_S", 0.05)
    monkeypatch.setattr(push, "POOL_SAY_S", 0.5)

    def check(name, queue_s, limit_s):
        monkeypatch.setattr(push, "check_limit_s", limit_s)
        shard = ["sh", "-c", f"sleep {queue_s}; for f in a b; do echo \"PASS 0.1 s  tests/test_$f.py\"; done"]
        waits = tmp_path / name
        waits.mkdir()
        proc = subprocess.Popen([sys.executable, "-m", "ttp", "pool-shard", "--pool", "pool-a", "--window", "30",
                                 "--", *shard], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_HOME": str(tmp_path / "home"),
                                     checkwait.ENV: str(waits)})
        said = []
        return push._timed_wait(proc, waits, name, said.append), "\n".join(said)

    rc, said = check("under", 2, 30.0)
    assert rc == 0, said
    assert re.search(r"check waiting for a test-pool slot: \d+ s so far, counted against its time limit", said), said
    assert re.search(r"check waited [12] s for a test-pool slot, counted against its time limit", said), said
    rc, said = check("past", 4, 2.0)
    assert rc == 124, said
    assert "check ran past its limit of 2 s and was ended" in said and "waiting for a test-pool slot:" in said, said
    assert re.search(r"\(\d+ s counted, [12] s of it waiting for a test-pool slot", said), said


def test_pool_shard_stall_window_comes_from_the_measured_seconds_per_file_with_a_floor(tmp_path, monkeypatch):
    """No history: the floor (600 s). Then 4x the median seconds per file of earlier shards, never under the
    floor; checks.pool_shard_stall_floor_s moves the floor and checks.pool_shard_stall_s fixes the window.
    Time before a shard's first file never enters the median."""
    ps = _pool_home(monkeypatch, tmp_path)
    assert ps.window("cpu") == ps.STALL_FLOOR_S == 600.0
    ps.record([300.0, 200.0, 250.0], "cpu")
    assert ps.window("cpu") == 1000.0 and ps.window("other") == 600.0
    monkeypatch.setenv("TTP_POOL_SHARD_STALL_FLOOR_S", "1200")
    assert ps.window("cpu") == 1200.0
    monkeypatch.setattr(ps.project, "load_account_settings", lambda: {"checks": {"pool_shard_stall_s": "90"}})
    assert ps.window("cpu") == 90.0
    for bad in ("soon", "-5", "0", "nan"):
        monkeypatch.setenv("TTP_POOL_SHARD_STALL_S", bad)
        assert ps.window("cpu") == 90.0, bad   # a bad value is ignored, the next source counts
    monkeypatch.delenv("TTP_POOL_SHARD_STALL_S")
    monkeypatch.setattr(ps.project, "load_account_settings", lambda: {})
    monkeypatch.delenv("TTP_POOL_SHARD_STALL_FLOOR_S")
    script = tmp_path / "slow-first"
    script.write_text("#!/bin/sh\nsleep 1\necho 'PASS a'\necho 'PASS b'\n")
    script.chmod(0o755)
    assert ps.run([str(script)], ["pool-a"], "s", what="q", win=10, tell=lambda _: None, out=io.StringIO()) == 0
    assert ps.per_file("q") < 0.5, "the 1 s before the first file is no per-file time"


def test_pool_shard_stall_guard_holds_when_the_shard_closes_its_output_or_leaves_a_process_holding_it(
        tmp_path, monkeypatch):
    """Bypasses of the no-progress window: a shard that closes its output and keeps running is still ended at
    its window; one that prints its files and exits while a leftover holds its output open finishes at once
    (no stall, no mark) and the leftover is ended; one interrupted mid-run leaves no process behind."""
    ps = _pool_home(monkeypatch, tmp_path)
    quiet = ["sh", "-c", f"echo $$ > {tmp_path}/quiet; exec >/dev/null 2>&1; exec sleep 600"]
    t0 = time.monotonic()
    assert ps.run(quiet, ["pool-a"], "q", win=1, tell=lambda _: None, out=io.StringIO(), grace=1.0) == ps.STALLED
    assert time.monotonic() - t0 < 10 and not _shard_alive(int((tmp_path / "quiet").read_text()))
    ps.clear("pool-a")
    left = ["sh", "-c", f"sleep 600 & echo $! > {tmp_path}/left; echo 'PASS a'; echo 'PASS b'; exit 3"]
    t0 = time.monotonic()
    assert ps.run(left, ["pool-a"], "l", win=30, tell=lambda _: None, out=io.StringIO()) == 3
    assert time.monotonic() - t0 < 10 and not ps.marked("pool-a")
    assert _wait_for(lambda: not _shard_alive(int((tmp_path / "left").read_text())))
    import threading
    hung = ["sh", "-c", f"echo $$ > {tmp_path}/hung; echo 'PASS a'; exec sleep 600"]
    done = []

    def stop_soon():
        _wait_for(lambda: (tmp_path / "hung").exists())
        time.sleep(0.3)
        done.append(True)
    threading.Thread(target=stop_soon, daemon=True).start()
    real_get = ps.queue.Queue.get

    def get(self, *a, **kw):   # the push's TERM, as the CLI's handler raises it, while it waits
        if done:
            raise SystemExit(143)
        return real_get(self, *a, **kw)
    monkeypatch.setattr(ps.queue.Queue, "get", get)
    with pytest.raises(SystemExit):
        ps.run(hung, ["pool-a"], "h", win=60, tell=lambda _: None, out=io.StringIO(), grace=1.0)
    assert not _shard_alive(int((tmp_path / "hung").read_text()))
