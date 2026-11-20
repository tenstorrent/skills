# Leftover check process and sandbox tree tests of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that
# file, and pytest collects these tests under its name. Add new checkreap tests here.
from __future__ import annotations


def _reap_live(pid: int) -> bool:
    """`pid` still runs (a zombie has ended)."""
    from ttp import checkreap
    st = checkreap._stat(pid)
    return st is not None and st[0] not in ("Z", "X")


def _reap_wait_file(path, timeout: float = 20) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.02)
    raise AssertionError(f"{path} was not written")


def _reap_tmp(tmp_path, monkeypatch):
    """A temporary folder of this test's own for clean checkouts."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    return tmp


def _reap_kill(*procs) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_check_s_background_child_ends_with_it_and_leaves_no_sandbox_tree(env, tmp_path, monkeypatch, capsys):
    """With no process sandbox to end them, what a check started in the background (still in its
    group) is ended once its exit code is known, the clean tree goes, and the log says so."""
    from ttp import checkreap, cli
    _no_sandbox_tools(monkeypatch)
    tmp = _reap_tmp(tmp_path, monkeypatch)
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    pidf = tmp_path / "bg.pid"
    cli.main(["checks", "--fresh", "--", f"sleep 60 & echo $! > {shlex.quote(str(pidf))}"])
    assert json.loads((run / "checks.json").read_text())["passed"] is True
    pid = int(_reap_wait_file(pidf))
    assert not _reap_live(pid), "the check's background child outlived the run"
    assert not list(tmp.glob("ttp-checks-*")), "the clean tree outlived the run"
    folder = p.state / checkreap.FOLDER
    assert not folder.exists() or not list(folder.iterdir())
    if checkreap.PROC.exists():  # without /proc (macOS) nothing is found to reap, so nothing is counted
        assert "ttp checks: reaped 1 leftover process and 1 sandbox tree" in (run / "checks.log").read_text()


def test_an_orphaned_grandchild_in_the_run_s_own_scratch_ends_with_the_run(env, tmp_path, monkeypatch):
    """A grandchild a check left in a session of its own with an environment of its own (no run token,
    no check group) ends with the run when its working directory or an argument is in the run's own
    scratch; a process outside it is never touched."""
    import shutil
    from ttp import cli
    from ttp import checkreap
    if not checkreap.PROC.exists() or not shutil.which("setsid"):
        pytest.skip("needs /proc and setsid")
    _no_sandbox_tools(monkeypatch)
    _reap_tmp(tmp_path, monkeypatch)
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    own = tmp_path / "scratch"   # what the checks supervisor makes the run's TMPDIR (checkdirs.scratch)
    own.mkdir()
    monkeypatch.setenv("TMPDIR", str(own))
    monkeypatch.setattr(cli, "_run_scratch", lambda: str(own))
    by_cwd, by_arg = tmp_path / "cwd.pid", tmp_path / "arg.pid"
    outside = subprocess.Popen(["sleep", "60"], cwd=tmp_path, start_new_session=True)
    try:
        q = shlex.quote
        cmd = (f"setsid sh -c 'echo $$ > {q(str(by_cwd))}; cd \"$TMPDIR\" && exec env -i sleep 60' & "
               f"setsid sh -c 'echo $$ > {q(str(by_arg))}; cd / && exec env -i sleep 60 \"$1\"' sh \"$TMPDIR/x\" & "
               f"sleep 0.5")
        cli.main(["checks", "--fresh", "--", cmd])
        pids = [int(_reap_wait_file(by_cwd)), int(_reap_wait_file(by_arg))]
        assert not any(_reap_live(pid) for pid in pids), "a grandchild in the run's scratch outlived the run"
        assert outside.poll() is None, "a process outside the run's scratch was ended"
    finally:
        _reap_kill(outside)


def test_the_sweep_reaps_a_stale_sandbox_with_exit_json_or_a_dead_runner(env, tmp_path, monkeypatch):
    """An earlier run's record that has ended (exit.json) or whose runner died: the processes in its
    recorded group with a working directory in its tree are ended, then the tree and record go."""
    from ttp import checkreap
    tmp = _reap_tmp(tmp_path, monkeypatch)
    p = make(env)
    procs = []
    try:
        for ended in (True, False):
            tree = tmp / f"{_reap_prefix()}{ended}"
            tree.mkdir()
            proc = subprocess.Popen(["sleep", "60"], cwd=tree, start_new_session=True)
            procs.append(proc)
            rec = checkreap.Record(p.state, checkreap.token(), str(tree))
            rec.group(proc.pid)
            if ended:
                rec.exit(0)
            else:   # a runner that is gone
                gone = subprocess.Popen(["true"])
                gone.wait()
                rec.data.update(pid=gone.pid, started="1")
                checkreap._write(rec.dir / checkreap.RECORD, rec.data)
            # Within the grace period nothing is touched.
            assert checkreap.sweep(p.state, grace_s=3600) == (0, 0) and proc.poll() is None
            assert checkreap.sweep(p.state, grace_s=0, term_s=10) == (1, 1)
            assert proc.wait(timeout=10) == -signal.SIGTERM
            assert not tree.exists() and not rec.dir.exists()
    finally:
        _reap_kill(*procs)


def _reap_prefix() -> str:
    from ttp import cleantree
    return cleantree.PREFIX


def test_the_sweep_never_signals_a_process_it_cannot_tie_to_the_sandbox(env, tmp_path, monkeypatch):
    """PID reuse: a process whose pid (and group) the record names but whose working directory is
    outside the sandbox and that lacks the run's token is left running."""
    from ttp import checkreap
    tmp = _reap_tmp(tmp_path, monkeypatch)
    p = make(env)
    tree, elsewhere = tmp / f"{_reap_prefix()}reuse", tmp_path / "elsewhere"
    tree.mkdir()
    elsewhere.mkdir()
    other = subprocess.Popen(["sleep", "60"], cwd=elsewhere, start_new_session=True)
    try:
        rec = checkreap.Record(p.state, checkreap.token(), str(tree))
        rec.group(other.pid)
        rec.exit(0)
        assert checkreap.sweep(p.state, grace_s=0, term_s=10) == (0, 1)
        assert other.poll() is None and _reap_live(other.pid)
        assert not tree.exists() and not rec.dir.exists()
    finally:
        _reap_kill(other)


def test_the_sweep_leaves_a_running_sandbox_alone(env, tmp_path, monkeypatch):
    """A runner that still runs and has written no exit.json: its processes, tree and record stay."""
    from ttp import checkreap
    tmp = _reap_tmp(tmp_path, monkeypatch)
    p = make(env)
    tree = tmp / f"{_reap_prefix()}live"
    tree.mkdir()
    tok = checkreap.token()
    rec = checkreap.Record(p.state, tok, str(tree))   # this process is its runner
    proc = subprocess.Popen(["sleep", "60"], cwd=tree, start_new_session=True,
                            env={**os.environ, checkreap.MARKER: tok})
    try:
        rec.group(proc.pid)
        assert checkreap.members(tok) == [proc.pid]
        assert checkreap.sweep(p.state, grace_s=0, term_s=10) == (0, 0)
        assert proc.poll() is None and tree.is_dir() and (rec.dir / checkreap.RECORD).exists()
    finally:
        _reap_kill(proc)


def test_a_stop_or_timeout_of_the_runner_ends_what_the_check_left_even_outside_its_group(tmp_path, monkeypatch):
    """A SIGTERM to the runner (a timeout's, a stop's) while a check runs ends the check and also a
    process it started in a session of its own, found by the run's token."""
    from ttp import checkreap, cli
    tok, pidf, log = checkreap.token(), tmp_path / "escaped.pid", tmp_path / "check.log"
    child = (f"import os, time; os.setsid(); open({str(pidf)!r}, 'w').write(str(os.getpid())); "
             "time.sleep(60)")
    script = (f"{shlex.quote(sys.executable)} -c {shlex.quote(child)} & "
              f"while [ ! -s {shlex.quote(str(pidf))} ]; do sleep 0.02; done; kill -TERM {os.getpid()}; sleep 60")
    monkeypatch.setitem(cli._CHECK_STOP, "stopped", None)   # the stop must not outlast this test
    monkeypatch.setitem(cli._REAPED, "procs", 0)
    old = signal.signal(signal.SIGTERM, cli._checks_stop)
    pid = None
    try:
        with open(log, "w") as out, pytest.raises(SystemExit) as e:
            cli._run_check(["sh", "-c", script], True, cwd=tmp_path, stdout=out,
                           env={**os.environ, checkreap.MARKER: tok})
        pid = int(_reap_wait_file(pidf))
    finally:
        signal.signal(signal.SIGTERM, old)
        if pid and _reap_live(pid):
            os.kill(pid, signal.SIGKILL)
    assert e.value.code == 143
    assert not _reap_live(pid), "a process the stopped check left kept running"
    assert cli._REAPED["procs"] >= 1 and "the check left running" in log.read_text()


def test_a_reaped_process_counts_until_it_is_gone_not_until_it_stops_showing_its_token(monkeypatch):
    """A process that is exiting no longer shows its environment, yet still runs: the reap waits
    for it (and kills it) by its pid and start time, so none is left when it returns."""
    from ttp import checkreap
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        monkeypatch.setattr(checkreap, "members", lambda *a, **k: [proc.pid])
        monkeypatch.setattr(checkreap, "tied", lambda *a, **k: False)
        assert checkreap.reap("tok", grace_s=0.2) == (1, 0)
        assert proc.wait(timeout=10) == -signal.SIGTERM
    finally:
        _reap_kill(proc)


def test_a_sweep_kill_writes_a_job_signals_row_naming_the_process(env, tmp_path, monkeypatch):
    """The daemon's sweep drops its counts, so each signal it sends must leave a job-signals.jsonl row
    (who sent it, why, and the ended process's command line): no reap of a check process is silent."""
    from ttp import checkreap, detach
    tmp = _reap_tmp(tmp_path, monkeypatch)
    p = make(env)
    tree = tmp / f"{_reap_prefix()}rows"
    tree.mkdir()
    proc = subprocess.Popen(["sleep", "61"], cwd=tree, start_new_session=True)
    try:
        rec = checkreap.Record(p.state, checkreap.token(), str(tree))
        rec.group(proc.pid)
        rec.exit(0)
        assert checkreap.sweep(p.state, grace_s=0, term_s=10) == (1, 1)
        assert proc.wait(timeout=10) == -signal.SIGTERM
        rows = [json.loads(x) for x in (p.state / detach.SIGNALS).read_text().splitlines()]
        assert [(r["pid"], r["signal"], r["by"], r["reason"]) for r in rows] == \
            [(proc.pid, "SIGTERM", "checkreap", "checkreap.reap")]
        assert "sleep 61" in rows[0]["target_cmd"] and rec.tok in rows[0]["why"] and "ended" in rows[0]["why"]
    finally:
        _reap_kill(proc)


def test_a_process_inside_a_check_run_never_reaps_that_run_so_sibling_test_workers_live(tmp_path):
    """A test worker of an outer `ttp checks` run carries the run's token from exec; a test in it that
    reaps with that token (a check env copied from os.environ) must not end its sibling workers. The
    runner, which never carries its own token, still ends them."""
    from ttp import checkreap
    tok = checkreap.token()
    env = {**os.environ, checkreap.MARKER: tok, "PYTHONPATH": str(RUNTIME)}
    sibling = subprocess.Popen(["sleep", "60"], env=env, start_new_session=True)
    inner = ("import subprocess, sys; from ttp import checkreap, cli; tok = sys.argv[1]; "
             "print(checkreap.members(tok), checkreap.reap(tok, grace_s=1)); "
             "cli._reap_check(subprocess.Popen(['true']), False, {'env': {checkreap.MARKER: tok}})")
    try:
        out = subprocess.run([sys.executable, "-c", inner, tok], env=env, capture_output=True, text=True,
                             timeout=60)
        assert out.returncode == 0 and out.stdout.split() == ["[]", "(0,", "0)"], (out.stdout, out.stderr)
        assert _reap_live(sibling.pid), "a check reaping its own run ended a sibling"
        assert checkreap.members(tok) == [sibling.pid], "the runner (no token) still finds it"
        assert checkreap.reap(tok, grace_s=5) == (1, 0) and not _reap_live(sibling.pid)
    finally:
        _reap_kill(sibling)
