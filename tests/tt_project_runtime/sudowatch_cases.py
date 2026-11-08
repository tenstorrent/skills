# sudo in test suites (sudowatch): recorded and reported by default, refused with checks.refuse_sudo.
# Every test runs a stub sudo on a scratch PATH that holds no real sudo: none ever runs the real one.


def _sudo_path(tmp_path, stub: bool = True) -> tuple[str, pathlib.Path]:
    """A scratch PATH: a stub sudo (it writes its arguments to calls.txt and exits 3) and links to the
    few tools the wrapper uses. No directory on it holds a real sudo."""
    tools, stub_dir = tmp_path / "tools", tmp_path / "stub"
    tools.mkdir(parents=True)
    stub_dir.mkdir()
    for name in ("tr", "date", "sh", "cat", "true", "timeout"):
        (tools / name).symlink_to(shutil.which(name))
    if stub:
        s = stub_dir / "sudo"
        s.write_text(f"#!/bin/sh\necho \"$*\" >> {shlex.quote(str(tmp_path / 'calls.txt'))}\nexit 3\n")
        s.chmod(0o755)
    return os.pathsep.join([str(stub_dir), str(tools)]), tmp_path / "calls.txt"


def _sudo_run(watch, *args):
    return subprocess.run(["sudo", *args], env=watch.env, capture_output=True, text=True)


def test_sudowatch_records_each_call_then_runs_the_next_sudo_unchanged(tmp_path, monkeypatch):
    from ttp import sudowatch
    path, calls = _sudo_path(tmp_path)
    monkeypatch.delenv("TTP_REFUSE_SUDO", raising=False)
    w = sudowatch.Watch({"PATH": path, "TTP_RUN_ID": "77"}, refuse=False)
    try:
        assert w.env["PATH"].split(os.pathsep)[0].endswith("/ttp-sudowatch") and w.env["TTP_REFUSE_SUDO"] == "0"
        r = _sudo_run(w, "-n", "ipmitool", "sdr", "elist")
        assert r.returncode == 3, r.stderr   # the stub's own exit code: passed through
        assert calls.read_text() == "-n ipmitool sdr elist\n"
        (c,) = w.calls()
        assert c["how"] == "ran" and c["run"] == "77" and c["args"] == "-n ipmitool sdr elist" and c["pid"] > 1
        lines = sudowatch.report_lines(w.calls(), w.started, procs=[])
        assert lines == ["sudo: the suite called sudo 1 time: sudo -n ipmitool sdr elist"]
    finally:
        w.close()
    assert not w.dir.exists()


def test_sudowatch_refuses_under_checks_refuse_sudo_and_never_calls_sudo(tmp_path):
    from ttp import sudowatch
    path, calls = _sudo_path(tmp_path)
    for w in (sudowatch.Watch({"PATH": path}, refuse=True), sudowatch.Watch({"PATH": path, "TTP_REFUSE_SUDO": "1"})):
        try:
            r = _sudo_run(w, "-n", "ipmitool", "sel", "elist")
            assert r.returncode == sudowatch.REFUSED_RC and "checks.refuse_sudo" in r.stderr
            assert not calls.exists(), "a refused sudo ran"
            assert [c["how"] for c in w.calls()] == ["refused"]
            assert "1 refused by checks.refuse_sudo" in sudowatch.report_lines(w.calls(), w.started, procs=[])[0]
        finally:
            w.close()


def test_sudowatch_refuse_setting_comes_from_the_project_key_or_the_daemon_env(monkeypatch):
    from ttp import project, sudowatch
    assert "refuse_sudo" in project._known_keys(["checks"])
    assert sudowatch.refusing(cfg={"checks": {"refuse_sudo": True}})
    assert not sudowatch.refusing(cfg={"checks": {"refuse_sudo": "yes"}}) and not sudowatch.refusing(cfg={})
    assert sudowatch.refusing(env={"TTP_REFUSE_SUDO": "1"}) and not sudowatch.refusing(env={"TTP_REFUSE_SUDO": "0"})
    monkeypatch.setattr(project, "here", lambda: None)
    assert not sudowatch.refusing(env={})   # no setting and no project: recorded, never refused


def test_sudowatch_nested_wrappers_skip_each_other_and_no_sudo_is_127(tmp_path):
    """A suite inside a suite (ttp sandbox under ttp checks) keeps one wrapper first on PATH; a wrapper
    never takes another wrapper for the real sudo; with none on PATH it says so and exits 127."""
    from ttp import sudowatch
    path, calls = _sudo_path(tmp_path)
    outer = sudowatch.Watch({"PATH": path}, refuse=False)
    inner = sudowatch.Watch(outer.env, refuse=False)
    try:
        assert sum(d.endswith("/ttp-sudowatch") for d in inner.env["PATH"].split(os.pathsep)) == 1
        both = {**inner.env, "PATH": os.pathsep.join([str(inner.dir / "ttp-sudowatch"), str(outer.dir / "ttp-sudowatch"), path])}
        assert subprocess.run(["sudo", "true"], env=both, capture_output=True).returncode == 3
        assert calls.read_text() == "true\n"
    finally:
        inner.close()
        outer.close()
    nopath, _ = _sudo_path(tmp_path / "x", stub=False)
    w = sudowatch.Watch({"PATH": nopath}, refuse=False)
    try:
        r = _sudo_run(w, "true")
        assert r.returncode == 127 and "not found" in r.stderr and w.calls() == []
    finally:
        w.close()


def test_sudowatch_command_name_skips_sudo_options_and_assignments():
    from ttp.sudowatch import command_name
    assert command_name("-n ipmitool sdr elist") == "ipmitool"
    assert command_name("-u root -E FOO=1 /usr/bin/ipmitool raw 0x06") == "ipmitool"
    assert command_name("-nu root timeout 5 ipmitool") == "timeout"
    assert command_name("-uroot -n ls") == "ls"
    assert command_name("--user=root -- ls -l") == "ls"
    assert command_name("--user root --preserve-env ls") == "ls"
    assert command_name("-n") == "" and command_name("") == ""


def test_sudowatch_leftovers_are_live_root_processes_started_through_sudo_only():
    """An orphaned root child (reparented away from sudo) is found by its command's name; a root child
    still under the recorded sudo by its ancestry; one in the check's session by it. Older root processes,
    the user's own processes and unrelated root ones are not listed. Calls without leftovers say only the
    calls; no calls say nothing."""
    from ttp import sudowatch
    t0 = 1000.0
    calls = [{"ts": "1001", "pid": 500, "run": "", "how": "ran", "args": "-n ipmitool sdr elist"}]

    def proc(pid, ppid, uid, name, start=t0 + 5, sid=1, cmd=None):
        return {"pid": pid, "ppid": ppid, "sid": sid, "start": start, "uids": [uid, uid], "name": name,
                "cmd": cmd or f"/usr/bin/{name} x"}
    procs = [proc(600, 900, 0, "ipmitool"),                 # orphan: name
             proc(601, 500, 0, "helper"),                   # under the sudo: ancestry
             proc(602, 1, 0, "other", sid=4242),            # in the check's session
             proc(603, 900, 0, "ipmitool", start=t0 - 60),  # older than the suite
             proc(604, 900, 1000, "ipmitool"),              # the user's own
             proc(605, 1, 0, "cron")]                       # unrelated root
    left = sudowatch.leftovers(calls, t0, (4242,), procs)
    assert [p["pid"] for p in left] == [600, 601, 602]
    lines = sudowatch.report_lines(calls, t0, (4242,), "the check", procs)
    assert "3 root processes started through sudo in the check" in lines[1] and "only root can end them" in lines[1]
    assert "pid 600 /usr/bin/ipmitool x" in lines[1] and "checks.refuse_sudo" in lines[2]
    assert sudowatch.report_lines(calls, t0, (), "the check", procs=[]) == lines[:1]
    assert sudowatch.report_lines([], t0, procs=procs) == []
    refused = [{**calls[0], "how": "refused"}]
    assert sudowatch.leftovers(refused, t0, (), procs) == []   # a refused call started nothing


def test_sudowatch_observation_is_queued_once_per_suite(monkeypatch):
    from ttp import project, sudowatch
    rows = []

    class DB:
        def one(self, sql, args):
            return any(r[3] == args[0] for r in rows) or None

        def x(self, sql, args):
            rows.append(args)

    class P:
        db = DB()

        def exists(self):
            return True
    monkeypatch.setattr(project, "here", lambda: P())
    lines = ["sudo: the check called sudo 1 time: sudo x", "sudo: 1 root process started ... only root can end it", "z"]
    assert sudowatch.observe(lines, "k") and sudowatch.observe(lines, "k")
    assert len(rows) == 1 and rows[0][2:5] == ("observation", "sudowatch:k", "normal")
    assert not sudowatch.observe(lines[:1], "k2") and len(rows) == 1   # calls alone: no observation
    monkeypatch.setattr(project, "here", lambda: None)
    assert not sudowatch.observe(lines, "k3")


def test_a_check_gets_the_sudo_wrapper_and_its_report_never_fails_it(tmp_path, monkeypatch):
    """ttp checks (_run_check): the check's sudo goes through the wrapper, its log names the call and an
    orphaned root process with who can end it, the exit code stays the command's, and the runner's own
    environment keeps no wrapper. Under the refusal the stub never runs."""
    from ttp import cli, sudowatch
    path, calls = _sudo_path(tmp_path)
    monkeypatch.setattr(sudowatch, "processes", lambda: [
        {"pid": 4321, "ppid": 1, "sid": 1, "start": time.time() + 30, "uids": [0, 0], "name": "ipmitool",
         "cmd": "ipmitool sdr elist"}])
    monkeypatch.setattr(sudowatch, "observe", lambda lines, key: seen.append(lines) or True)
    monkeypatch.setitem(cli._CHECK_STOP, "stopped", None)
    seen = []
    log = tmp_path / "check.log"
    with open(log, "w") as out:
        rc = cli._run_check(["sh", "-c", "sudo -n ipmitool sdr elist || true"], True, cwd=tmp_path, stdout=out,
                            stderr=subprocess.STDOUT, env={"PATH": path, "TTP_REFUSE_SUDO": "0"})
    text = log.read_text()
    assert rc == 0 and calls.read_text() == "-n ipmitool sdr elist\n"
    assert "ttp checks: sudo: the check called sudo 1 time" in text
    assert "pid 4321 ipmitool sdr elist" in text and "only root can end it" in text
    assert len(seen) == 1 and "ttp-sudowatch" not in os.environ.get("PATH", "")
    calls.unlink()
    with open(log, "w") as out:
        rc = cli._run_check(["sh", "-c", "sudo -n ipmitool sdr elist"], True, cwd=tmp_path, stdout=out,
                            stderr=subprocess.STDOUT, env={"PATH": path, "TTP_REFUSE_SUDO": "1"})
    assert rc == 126 and not calls.exists() and "checks.refuse_sudo is on" in log.read_text()


def test_ttp_sandbox_reports_the_commands_sudo_on_stderr_and_keeps_its_exit_code(tmp_path, monkeypatch, capfd):
    from ttp import sandbox, sudowatch
    path, calls = _sudo_path(tmp_path)
    monkeypatch.setattr(sandbox, "wrap", lambda argv, *a, **k: list(argv))
    monkeypatch.setattr(sandbox, "notice", lambda *a: "")
    monkeypatch.setattr(sudowatch, "processes", lambda: [])
    monkeypatch.setattr(sudowatch, "observe", lambda lines, key: False)
    monkeypatch.setenv("PATH", path)
    monkeypatch.setenv("TTP_REFUSE_SUDO", "0")
    # Under `timeout`: a wrapper and a sudo stand-in that run each other would hang here, not fail.
    assert sandbox.run(["timeout", "20", "sh", "-c", "sudo -n ipmitool sel elist; exit 5"]) == 5
    assert calls.read_text() == "-n ipmitool sel elist\n"
    assert "ttp sandbox: sudo: the command called sudo 1 time" in capfd.readouterr().err
    monkeypatch.setenv("TTP_REFUSE_SUDO", "1")
    calls.unlink()
    assert sandbox.run(["timeout", "20", "sh", "-c", "sudo -n ipmitool sel elist"]) == 126 and not calls.exists()
    assert "checks.refuse_sudo" in capfd.readouterr().err
    assert sandbox.run(["true"]) == 0 and "sudo" not in capfd.readouterr().err   # no call: nothing said


def _with_stand_ins(tmp_path, path, wrapper_first_applied):
    """The sudo wrapper and the host-state stand-ins (hoststate.fake_env) on one PATH, applied in either order."""
    from ttp import hoststate, sudowatch
    store = tmp_path / "store"
    if wrapper_first_applied:
        w = sudowatch.Watch({"PATH": path}, refuse=False)
        w.env = hoststate.fake_env(w.env, store)
    else:
        w = sudowatch.Watch(hoststate.fake_env({"PATH": path}, store), refuse=False)
    return w, store


@pytest.mark.parametrize("wrapper_first_applied", [True, False])
def test_sudowatch_and_the_sudo_stand_in_run_the_real_sudo_once_in_either_order(tmp_path, wrapper_first_applied):
    import sys
    path, calls = _sudo_path(tmp_path)
    (tmp_path / "tools" / "python3").symlink_to(sys.executable)
    w, store = _with_stand_ins(tmp_path, path, wrapper_first_applied)
    try:
        assert w.env["PATH"].split(os.pathsep)[0].endswith("/ttp-sudowatch")   # the wrapper stays first
        # Bounded: the wrapper and the stand-in once took each other for the real sudo and looped forever.
        r = subprocess.run(["sudo", "-n", "echo", "hi"], env=w.env, capture_output=True, text=True, timeout=20)
        assert r.returncode == 3, r.stderr
        assert calls.read_text() == "-n echo hi\n"   # the stub (real) sudo ran exactly once
        assert [c["args"] for c in w.calls()] == ["-n echo hi"]
        # A host write is still refused by the stand-in, and the wrapper still records it.
        r = subprocess.run(["sudo", "-n", "systemctl", "enable", "x"], env=w.env, capture_output=True, text=True,
                           timeout=20)
        assert r.returncode == 1 and "refused" in r.stderr, r.stderr
        assert calls.read_text() == "-n echo hi\n"
        assert [c["args"] for c in w.calls()] == ["-n echo hi", "-n systemctl enable x"]
        logged = [json.loads(ln) for ln in (store / "calls.jsonl").read_text().splitlines()]
        assert [(c["cmd"], c["write"]) for c in logged if c["cmd"] == "sudo"] == [("sudo", False), ("sudo", True)]
    finally:
        w.close()


def test_sudowatch_gives_no_wrapper_when_its_folder_cannot_run(tmp_path, monkeypatch):
    from ttp import sudowatch
    monkeypatch.setattr(sudowatch.os, "access", lambda p, mode: False)   # as on a noexec TMPDIR
    with pytest.raises(OSError, match="cannot run"):
        sudowatch.Watch({"PATH": "/nonexistent", "TMPDIR": str(tmp_path)})
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("ttp-sudo-")]


def test_hoststate_and_hostshim_name_the_sudo_wrapper_folder_as_sudowatch_does():
    from ttp import hostshim, hoststate, sudowatch
    assert hoststate.SUDO_WRAPPER_DIR == sudowatch.DIR_NAME
    assert f'"{sudowatch.DIR_NAME}"' in pathlib.Path(hostshim.__file__).read_text()
