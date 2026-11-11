# Area cases for test_tt_project_runtime.py: a test run never changes the host's real crontab,
# systemd user units or power state (hoststate, hostshim).


def _fake_host(tmp_path, monkeypatch, tab: str | None = None):
    """A stand-in for the host's real crontab (a script by absolute path, backed by a file) and
    systemd user unit folders; returns (crontab script, crontab file, unit folder); the
    ~/.local/share/systemd/user stand-in is hoststate._share_dir()."""
    from ttp import hoststate
    tabf, units, real = tmp_path / "realtab", tmp_path / "units", tmp_path / "realbin"
    units.mkdir()
    (tmp_path / "run").mkdir()
    real.mkdir()
    cron = real / "crontab"
    cron.write_text(f"#!/bin/sh\nT={shlex.quote(str(tabf))}\n"
                    'case "$1" in -l) [ -f "$T" ] && exec cat "$T"; echo "no crontab for x" >&2; exit 1;; '
                    '-r) rm -f "$T";; *) cat > "$T";; esac\n')
    cron.chmod(0o755)
    if tab is not None:
        tabf.write_text(tab)
    # Its own fake store: the session's is shared by every test worker, whose crontab -r would race this one
    monkeypatch.delenv(hoststate.MARKER, raising=False)
    monkeypatch.delenv("TTP_RUN_DIR", raising=False)
    # Its own reap token: the outer run's is on every test worker, so a check's reap would end them all
    from ttp import checkreap
    monkeypatch.setenv(checkreap.MARKER, f"hoststate-test-{os.getpid()}-{tmp_path.name}")
    monkeypatch.setattr(hoststate, "_real_dir", lambda: units)
    (tmp_path / "share_units").mkdir()   # the other user unit folder (~/.local/share/systemd/user)
    monkeypatch.setattr(hoststate, "_share_dir", lambda: tmp_path / "share_units")
    monkeypatch.setattr(hoststate, "_real", lambda name: str(cron) if name == "crontab" else None)
    return cron, tabf, units


def _append(cron, line, scratch):
    """A shell command adding `line` (shell words expand) to the crontab at `cron` by absolute path,
    through a copy in `scratch`: the stand-in's `cat >` would truncate it while `-l` still reads."""
    c = shlex.quote(str(cron))
    return f'{c} -l > "{scratch}/tab"; echo "{line}" >> "{scratch}/tab"; {c} - < "{scratch}/tab"'


CHECK_TIMEOUT_S = 120   # far below ttp checks' silence limit (checkwall.SILENT_S, 10 min)


def _run_check_ended(argv, timeout=CHECK_TIMEOUT_S, **kw):
    """cli._run_check in its own group, ended (KILL) on the way out on pass, failure and timeout alike.
    Its own session and reap token keep it out of the outer run's group stop and reap, so a hung check
    once outlived a stopped test worker: its stand-ins spun for days on the test box."""
    from ttp import cli
    groups = []

    class _Groups:
        def group(self, pgid):
            groups.append(pgid)
    try:
        return cli._run_check(argv, True, record=_Groups(), timeout=timeout, **kw)
    finally:
        for pgid in groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass


def _ledger(monkeypatch):
    from ttp import machine_ledger, machines
    got = []
    monkeypatch.setattr(machines, "here", lambda: ("box1", {}))
    monkeypatch.setattr(machine_ledger, "add_change",
                        lambda *a, **kw: got.append((a, kw)) or {"id": 7})
    return got


def test_shimmed_crontab_systemctl_and_reboot_under_ttp_checks_leave_the_host_untouched_and_are_reported(
        tmp_path, monkeypatch):
    from ttp import hoststate, push
    _, tabf, units = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n")
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    assert env.get(hoststate.MARKER) and env["PATH"].split(os.pathsep)[0].endswith("bin")
    assert env[hoststate.MARKER].startswith(str(tmp_path)), "its own store, not the session's shared one"
    log = tmp_path / "checks.log"
    script = ("echo '@reboot evil' | crontab - && crontab -l | grep -q evil && "
              "systemctl --user enable evil.service && systemctl --user is-enabled evil.service && "
              "reboot && at now < /dev/null")
    with open(log, "w") as out:
        rc = _run_check_ended(["sh", "-c", script], cwd=tmp_path, stdout=out, stderr=subprocess.STDOUT, env=env)
    text = log.read_text()
    assert rc == 0, text
    assert tabf.read_text() == "0 1 * * * keep\n" and not list(units.iterdir())
    said = text[text.index("host guard: these calls went to the run's fake store"):]
    for call in ("crontab -", "systemctl --user enable evil.service", "reboot", "at now"):
        assert call in said, said
    assert "FAILED" not in text


def test_a_host_change_the_stand_ins_miss_fails_ttp_checks_and_is_restored_and_recorded(tmp_path, monkeypatch):
    from ttp import hoststate, push
    cron, tabf, units = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n")
    (units / "a.service").write_text("[Service]\nExecStart=/bin/true\n")
    got = _ledger(monkeypatch)
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    log = tmp_path / "checks.log"
    # Each leak names the run's own folder (its run dir): proof the run made it
    run = env["TTP_RUN_DIR"]
    script = (f"printf '0 1 * * * keep\\n@reboot {run}/evil\\n# tt-project: own\\n' | {shlex.quote(str(cron))} - && "
              f"echo ExecStart={shlex.quote(run)}/x > {shlex.quote(str(units / 'evil.service'))} && "
              f"rm {shlex.quote(str(units / 'a.service'))} && "
              f"mkdir {shlex.quote(str(units / 'default.target.wants'))} && "
              f"ln -s {shlex.quote(run)}/evil.service {shlex.quote(str(units / 'default.target.wants' / 'evil.service'))}")
    with open(log, "w") as out:
        rc = _run_check_ended(["sh", "-c", script], cwd=tmp_path, stdout=out, stderr=subprocess.STDOUT, env=env)
    text = log.read_text()
    assert rc == 1, text
    assert "host guard: FAILED" in text and "/evil'" in text and "evil.service" in text, text
    assert "recorded in the machine change ledger (box1, change 7)" in text
    # Put back: the added line gone, the harness's own line kept, the run's unit files gone. The removed
    # a.service is not provably the run's (another process may have removed it): reported, never re-made
    assert tabf.read_text() == "0 1 * * * keep\n# tt-project: own\n"
    assert sorted(p.name for p in units.iterdir()) == []
    assert "left in place, not proved to be this run's" in text and "a.service" in text, text
    (alias, what, undo, _by), kw = got[0]
    assert alias == "box1" and "/evil'" in what and undo.startswith("none:") and kw["test"] is True


def test_a_failing_check_keeps_its_exit_code_when_it_also_changed_the_host(tmp_path, monkeypatch):
    from ttp import push
    cron, tabf, _ = _fake_host(tmp_path, monkeypatch)
    _ledger(monkeypatch)
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    with open(tmp_path / "log", "w") as out:
        rc = _run_check_ended(["sh", "-c", f"echo \"@reboot $TTP_RUN_DIR/x\" | {shlex.quote(str(cron))} - ; exit 3"],
                              cwd=tmp_path, stdout=out, stderr=subprocess.STDOUT, env=env)
    assert rc == 3 and not tabf.exists()   # there was no crontab before: removed again


def test_ttp_sandbox_fails_a_command_that_changed_the_host_and_restores_it(tmp_path, monkeypatch, capsys):
    from ttp import sandbox
    cron, tabf, _ = _fake_host(tmp_path, monkeypatch, "a\n")
    _ledger(monkeypatch)
    monkeypatch.setattr(sandbox, "wrap", lambda argv, *a, **kw: list(argv))
    monkeypatch.setattr(sandbox, "notice", lambda *a: "")
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        assert sandbox.run(["sh", "-c", "crontab -r && reboot"]) == 0   # stand-ins: the real one untouched
        assert tabf.read_text() == "a\n"
        assert "went to the run's fake store" in capsys.readouterr().err
        assert sandbox.run(["sh", "-c", _append(cron, "b $TTP_HOSTSTATE_FAKE", "$TTP_HOSTSTATE_FAKE")]) == 1
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    assert tabf.read_text() == "a\n"
    assert "host guard: FAILED" in capsys.readouterr().err


def test_sudo_running_a_host_write_under_ttp_checks_is_refused_and_reported_and_other_sudo_calls_pass(
        tmp_path, monkeypatch, leaked_tmp_processes):
    # sudo resets PATH (secure_path): the stand-ins never see what it runs, so the sudo stand-in refuses
    from ttp import push
    _fake_host(tmp_path, monkeypatch)
    real, seen = tmp_path / "realsudo", tmp_path / "sudo-ran"
    real.mkdir()
    (real / "sudo").write_text(f"#!/bin/sh\necho \"$*\" >> {shlex.quote(str(seen))}\n")
    (real / "sudo").chmod(0o755)
    monkeypatch.setenv("PATH", f"{real}{os.pathsep}{os.environ['PATH']}")
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    refused = ["sudo -n systemctl enable evil.service", "sudo -n -u root -- env A=1 reboot",
               "sudo -nE crontab -", "sudo install -m 644 x /etc/systemd/system/evil.service",
               "sudo sh -c 'true && systemctl --user daemon-reload'", "sudo -n timeout 5 shutdown -h now",
               "sudo -n loginctl enable-linger", "sudo tt-smi -glx_reset", "sudo /usr/sbin/poweroff",
               "sudo tee /var/spool/cron/crontabs/x", "sudo sudo -n halt",
               "sudo -u root bash -lc 'systemctl enable x'", "sudo env -i PATH=/x systemctl enable x"]
    allowed = ["sudo -n systemctl status x", "sudo -n crontab -l", "sudo -n true", "sudo -l"]
    script = "; ".join(f"{c} < /dev/null; echo \"rc=$?\"" for c in refused + allowed)
    log = tmp_path / "checks.log"
    with open(log, "w") as out:
        rc = _run_check_ended(["sh", "-c", script], cwd=tmp_path, stdout=out, stderr=subprocess.STDOUT, env=env)
    text = log.read_text()
    assert rc == 0, text
    assert text.count("rc=1") == len(refused) and text.count("sudo: refused in a test run") == len(refused), text
    assert seen.read_text().splitlines() == [c[len("sudo "):] for c in allowed], "only the reads reached sudo"
    said = text[text.index("host guard: these calls went to the run's fake store"):]
    assert "sudo -n systemctl enable evil.service" in said and "sudo -n crontab -l" not in said, said
    assert not leaked_tmp_processes(tmp_path), "no stand-in or sudo outlives the check"


def test_a_hung_sudo_check_times_out_and_leaves_no_stand_in_or_sudo_running(tmp_path, monkeypatch,
                                                                            leaked_tmp_processes):
    # A read sudo passes on that never ends (the real sudo spinning): the check ends at its timeout with
    # its whole group, so nothing naming the test's folder runs on once the test is over
    from ttp import push
    _fake_host(tmp_path, monkeypatch)
    real = tmp_path / "realsudo"
    real.mkdir()
    (real / "sudo").write_text("#!/bin/sh\nwhile :; do :; done\n")
    (real / "sudo").chmod(0o755)
    monkeypatch.setenv("PATH", f"{real}{os.pathsep}{os.environ['PATH']}")
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    with open(tmp_path / "checks.log", "w") as out, pytest.raises(subprocess.TimeoutExpired):
        _run_check_ended(["sh", "-c", "sudo -n true"], timeout=2, cwd=tmp_path, stdout=out,
                         stderr=subprocess.STDOUT, env=env)
    assert not leaked_tmp_processes(tmp_path)


def test_a_host_restore_keeps_a_concurrent_unrelated_crontab_line(tmp_path, monkeypatch):
    from ttp import hoststate
    _, tabf, _ = _fake_host(tmp_path, monkeypatch, "a\nevil\nother\nevil\n")
    before = {"crontab": "a\nevil\n", "units": {}}
    assert hoststate.restore(before, {"crontab": (["evil"], [])}) == []
    assert tabf.read_text() == "a\nevil\nother\n"   # the last copy of the added line went
    assert hoststate.diff({"crontab": "a\n", "units": {}}, {"crontab": "a\n# tt-project: x\n", "units": {}}) == {}


def test_a_run_wiping_the_real_crontab_by_path_fails_and_every_line_comes_back(tmp_path, monkeypatch):
    """`crontab -r` by absolute path removes every project's lines, which no write log or folder name
    proves: the whole crontab going from lines to none, with no open machine change naming it, is the
    run's. It fails and each line comes back (the harness's own included), next to a line written since."""
    from ttp import hoststate, machine_ledger, push
    tab = "0 1 * * * keep\n@reboot bash ~/.x/resume.sh # series-resume:a\n0 3 * * * job # tt-project: d\n"
    cron, tabf, _ = _fake_host(tmp_path, monkeypatch, tab)
    got = _ledger(monkeypatch)
    monkeypatch.setattr(machine_ledger, "changes", lambda *a, **kw: [
        {"what": "a test run changed the real crontab -r", "undo": "none: restored", "test": True}])
    guard = hoststate.Guard(push.check_env("checks", run_dir=str(tmp_path / "run")))
    subprocess.run([str(cron), "-r"], check=True)
    assert not tabf.exists()
    changed, said = guard.end()
    assert changed and any("host guard: FAILED" in s and "real crontab removed (3 lines" in s for s in said), said
    assert tabf.read_text() == tab
    assert got and got[0][1]["test"] is True
    assert not any("left in place" in s for s in said), said
    # A line another process writes between the wipe and the restore stays, after the old ones
    guard = hoststate.Guard(push.check_env("checks", run_dir=str(tmp_path / "run")))
    tabf.unlink()
    monkeypatch.setattr(hoststate, "snapshot", lambda: {"crontab": None, "units": {}, hoststate.SHARE_KEY: {}})
    real = hoststate._crontab
    monkeypatch.setattr(hoststate, "_crontab", lambda: (tabf.write_text("0 4 * * * new\n"), real())[1])
    assert guard.end()[0]
    assert tabf.read_text() == tab + "0 4 * * * new\n"


def test_a_crontab_removal_a_machine_change_names_or_one_line_going_is_left_in_place(tmp_path, monkeypatch):
    """Not a wipe: a crontab removed with an open machine change naming it, an empty crontab, and lines
    their owner or an overlapping run took out or added (a series' own @reboot resume line) are reported,
    never put back or deleted, and pass the run."""
    from ttp import hoststate, machine_ledger, push
    _, tabf, _ = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n")
    got = _ledger(monkeypatch)
    ledger = [{"what": "cleared the box: crontab -r", "undo": "none", "test": False}]
    monkeypatch.setattr(machine_ledger, "changes", lambda *a, **kw: ledger)
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    guard = hoststate.Guard(env)
    tabf.unlink()
    changed, said = guard.end()
    assert not changed and not got and not tabf.exists(), said
    assert any("left in place" in s and "keep" in s for s in said), said
    ledger.clear()
    tabf.write_text("")   # an empty crontab removed: nothing to wipe
    guard = hoststate.Guard(env)
    tabf.unlink()
    assert guard.end() == (False, []) and not tabf.exists()
    resume = "@reboot bash ~/.x/{0}/resume.sh # series-resume:{0}"
    tabf.write_text("0 1 * * * keep\n" + resume.format("a") + "\n" + resume.format("b") + "\n")
    guard = hoststate.Guard(env)
    tabf.write_text("0 1 * * * keep\n" + resume.format("b") + "\n" + resume.format("c") + "\n")
    changed, said = guard.end()
    assert not changed and not got, said
    assert tabf.read_text() == "0 1 * * * keep\n" + resume.format("b") + "\n" + resume.format("c") + "\n"
    [line] = [s for s in said if "left in place" in s]
    assert "series-resume:a" in line and "series-resume:c" in line, line
    assert hoststate.wiped({"crontab": "x\n"}, {"crontab": None}, ["deleted the user crontab on purpose"]) is False
    assert hoststate.wiped({"crontab": "x\n"}, {"crontab": None}, ["edited crontab line y"]) is True
    assert hoststate.wiped({"crontab": "x\n"}, {"crontab": False}, []) is False   # unreadable: no claim


def test_a_unit_another_process_installs_during_an_overlapping_run_survives_and_is_reported(tmp_path, monkeypatch):
    """Runs overlap: another project installs its unit (with a machine change) while this run's guard is
    open. It is not the run's: reported once, naming the paths and the run, and never deleted."""
    from ttp import hoststate, machine_ledger, push
    _, tabf, units = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n")
    got = _ledger(monkeypatch)
    monkeypatch.setattr(machine_ledger, "changes", lambda *a, **kw: [
        {"what": "installed other.service and enabled it", "undo": "systemctl --user disable --now other.service",
         "test": False},
        {"what": "a test run changed the real crontab line added: 'x'", "undo": "none: restored", "test": True}])
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    env["TTP_TASK"] = "42"
    guard = hoststate.Guard(env)
    # The other process: a ledger-named unit (even one naming this run's folder), its enable link, an
    # unrecorded unit, a crontab line, and a pre-existing file's edit
    (units / "other.service").write_text(f"ExecStart={env['TTP_RUN_DIR']}/x\n")
    (units / "default.target.wants").mkdir()
    (units / "default.target.wants" / "other.service").symlink_to("../other.service")
    (units / "plain.timer").write_text("[Timer]\nOnCalendar=daily\n")
    tabf.write_text("0 1 * * * keep\n0 2 * * * other-job\n")
    changed, said = guard.end()
    assert not changed and not got, said
    assert sorted(p.name for p in units.rglob("*")) == ["default.target.wants", "other.service", "other.service",
                                                         "plain.timer"]
    assert tabf.read_text() == "0 1 * * * keep\n0 2 * * * other-job\n"
    [line] = [s for s in said if "left in place" in s]
    assert "task 42" in line and "other.service" in line and "plain.timer" in line and "other-job" in line, line
    assert not any("FAILED" in s for s in said), said


def test_the_runs_own_unit_leak_is_still_caught_and_restored(tmp_path, monkeypatch):
    """The run's own leak: its write log names the path, the name is a test-only one, or the file names
    the run's folder. Each is put back and fails the run; a whole-folder record proves nothing."""
    from ttp import hoststate, machine_ledger, push
    _, tabf, units = _fake_host(tmp_path, monkeypatch)
    got = _ledger(monkeypatch)
    monkeypatch.setattr(machine_ledger, "changes", lambda *a, **kw: [])
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    (units / "kept.service").write_text("[Service]\n")
    guard = hoststate.Guard(env, tokens=[str(tmp_path / "basetemp")])
    (units / "logged.service").write_text("[Service]\n")
    (units / "pytest-leak.service").write_text("[Service]\n")
    (units / "temp.service").write_text(f"ExecStart={tmp_path / 'basetemp' / 'x'}\n")
    (units / "kept.service").write_text(f"ExecStart={env['TTP_RUN_DIR']}/y\n")   # an edit naming the run
    (units / "other.service").write_text("[Service]\n")   # only the whole folder is recorded: not proof
    tabf.write_text(f"@reboot {env['TTP_RUN_DIR']}/z\n")
    changed, said = guard.end(recorded=[str(units / "logged.service"), str(tmp_path)])
    assert changed and any("host guard: FAILED" in s and "logged.service" in s for s in said), said
    assert sorted(p.name for p in units.iterdir()) == ["kept.service", "other.service"]
    assert (units / "kept.service").read_text() == "[Service]\n"
    assert not tabf.exists()   # there was no crontab before
    assert got and got[0][1]["test"] is True
    assert any("left in place" in s and "other.service" in s for s in said), said


def test_the_host_guard_tests_reach_no_real_unit_folder_crontab_or_systemctl(tmp_path, monkeypatch):
    """These tests touch only their temp stand-ins: the guard's unit folder, crontab and systemctl
    resolve into tmp_path (systemctl to none), and the session fails any test that writes the real
    ~/.config/systemd/user (its write recorder watches it) or calls a real crontab or systemctl by name
    (the stand-ins are first on PATH)."""
    import conftest
    from ttp import hoststate, machine_ledger
    cron, tabf, units = _fake_host(tmp_path, monkeypatch)
    _ledger(monkeypatch)
    monkeypatch.setattr(machine_ledger, "changes", lambda *a, **kw: [])
    calls = []
    run = hoststate._run
    monkeypatch.setattr(hoststate, "_run", lambda cmd, *a, **kw: calls.append(cmd[0]) or run(cmd, *a, **kw))
    guard = hoststate.Guard({}, tokens=[str(tmp_path / "t")])
    (units / "pytest-x.service").write_text("x")
    tabf.write_text(f"@reboot {tmp_path / 't'}\n")
    assert guard.end()[0]
    assert hoststate._real_dir() == units and hoststate._real("systemctl") is None
    assert hoststate._share_dir() == tmp_path / "share_units"
    assert calls and set(calls) == {str(cron)}
    assert ".config/systemd/user" in conftest.HOME_WATCHED and ".local/share/systemd/user" in conftest.HOME_WATCHED


def test_local_share_units_guarded(tmp_path, monkeypatch):
    """~/.local/share/systemd/user gets the same rule as ~/.config/systemd/user: another process's unit,
    its enable link and a ledger-named unit there survive and are reported; the run's own leak there
    (its write log, a test-only name, a file naming its folder) fails the run and is put back."""
    from ttp import hoststate, machine_ledger, push
    _, _, units = _fake_host(tmp_path, monkeypatch)
    share = hoststate._share_dir()
    got = _ledger(monkeypatch)
    monkeypatch.setattr(machine_ledger, "changes", lambda *a, **kw: [
        {"what": "reinstalled named.service", "undo": "systemctl --user disable --now named.service", "test": False}])
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    env["TTP_TASK"] = "42"
    (share / "kept.service").write_text("[Service]\n")
    guard = hoststate.Guard(env)
    # Another process: its unit and enable link, and a ledger-named unit even naming this run's folder
    (share / "other.service").write_text("[Service]\nExecStart=/opt/x\n")
    (share / "default.target.wants").mkdir()
    (share / "default.target.wants" / "other.service").symlink_to("../other.service")
    (share / "named.service").write_text(f"ExecStart={env['TTP_RUN_DIR']}/x\n")
    # The run's own: logged, test-only named, naming its run folder, and its edit of a logged file
    (share / "logged.service").write_text("[Service]\n")
    (share / "pytest-y.service").write_text("[Service]\n")
    (share / "mine.service").write_text(f"ExecStart={env['TTP_RUN_DIR']}/y\n")
    (share / "kept.service").write_text("[Service]\nchanged\n")
    changed, said = guard.end(recorded=[str(share / "logged.service"), str(share / "kept.service"), str(share.parent)])
    assert changed and got, said
    assert sorted(p.name for p in share.rglob("*")) == ["default.target.wants", "kept.service", "named.service",
                                                        "other.service", "other.service"]
    assert (share / "kept.service").read_text() == "[Service]\n"
    [left] = [s for s in said if "left in place" in s]
    assert "task 42" in left and "other.service" in left and "named.service" in left, left
    assert "mine.service" not in left and "logged.service" not in left, left
    [failed] = [s for s in said if "FAILED" in s]
    assert "~/.local/share/systemd/user/mine.service" in failed and "pytest-y.service" in failed, failed
    assert not list(units.iterdir())


FOREIGN = "@reboot bash /opt/demo-series/job-a/resume.sh 600 # ttp-series-resume:job-a"
FOREIGN_GONE = "@reboot bash /opt/demo-series/job-b/resume.sh 600 # ttp-series-resume:job-b"


def test_a_crontab_change_another_process_makes_during_a_check_is_left_alone_and_never_fails_it(
        tmp_path, monkeypatch):
    # A live job outside the run registers and drops its own @reboot lines while a check runs: the
    # guard cannot tie them to the run, so it reports them, keeps them and passes the check.
    from ttp import push
    cron, tabf, units = _fake_host(tmp_path, monkeypatch, f"0 1 * * * keep\n{FOREIGN_GONE}\n")
    (units / "demo.service").write_text("[Service]\nExecStart=/opt/demo-series/job-b/run.sh\n")
    got = _ledger(monkeypatch)
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    script = (f"printf '0 1 * * * keep\\n{FOREIGN}\\n' | {shlex.quote(str(cron))} - && "
              f"rm {shlex.quote(str(units / 'demo.service'))}")
    log = tmp_path / "checks.log"
    with open(log, "w") as out:
        rc = _run_check_ended(["sh", "-c", script], cwd=tmp_path, stdout=out, stderr=subprocess.STDOUT, env=env)
    text = log.read_text()
    assert rc == 0, text
    assert "host guard: left in place" in text and "job-a" in text and "job-b" in text, text
    assert "host guard: FAILED" not in text and got == [], text
    assert tabf.read_text() == f"0 1 * * * keep\n{FOREIGN}\n"   # neither reverted
    assert not (units / "demo.service").exists()


def test_a_check_s_own_crontab_line_in_its_working_tree_fails_it_and_only_its_own_line_goes(
        tmp_path, monkeypatch):
    # The line names the check's working tree (its cwd), not its run folder: still the run's own.
    from ttp import push
    cron, tabf, _ = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n")
    _ledger(monkeypatch)
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    tree = tmp_path / "tree"
    tree.mkdir()
    own = f"@reboot {tree}/made-by-a-test.sh"
    log = tmp_path / "checks.log"
    with open(log, "w") as out:
        rc = _run_check_ended(["sh", "-c", f"printf '0 1 * * * keep\\n{FOREIGN}\\n{own}\\n' | "
                               f"{shlex.quote(str(cron))} -"], cwd=tree, stdout=out,
                              stderr=subprocess.STDOUT, env=env)
    text = log.read_text()
    assert rc == 1 and "host guard: FAILED" in text and "made-by-a-test" in text, text
    assert "left in place" in text and "job-a" in text, text
    assert tabf.read_text() == f"0 1 * * * keep\n{FOREIGN}\n"


def test_a_run_tree_after_a_redirect_or_separator_is_the_run_s_and_an_outside_removal_is_not(tmp_path):
    from ttp import hoststate
    tree = str(tmp_path / "run")
    gone = "0 2 * * * outside-job"
    before = {"crontab": f"0 1 * * * keep\n{gone}\n", "units": {}}
    for line in (f"* * * * * x >>{tree}/log", f"* * * * * x 2>{tree}/err", f"* * * * * x <{tree}/in",
                 f"* * * * * x|{tree}/y", f"* * * * * x;{tree}/y", f"* * * * * x&&{tree}/y",
                 f"* * * * * x a,{tree}/y"):
        after = {"crontab": f"0 1 * * * keep\n{line}\n", "units": {}}   # the outside job dropped its line too
        mine, theirs = hoststate.own(before, after, hoststate.diff(before, after), tokens=[tree])
        assert mine == {"crontab": ([line], [])}, line
        assert theirs == {"crontab": ([], [gone])}, line


def test_a_host_restore_reads_the_crontab_again_and_keeps_a_line_written_after_the_compare(
        tmp_path, monkeypatch):
    from ttp import hoststate
    _, tabf, _ = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n")
    _ledger(monkeypatch)
    g = hoststate.Guard({}, tokens=[str(tmp_path / "run")])
    own = f"@reboot {tmp_path}/run/made-by-a-test.sh"
    tabf.write_text(f"0 1 * * * keep\n{own}\n")
    snap = hoststate.snapshot

    def compare_then_concurrent_write():
        got = snap()
        tabf.write_text(tabf.read_text() + FOREIGN + "\n")   # another process, between compare and restore
        return got
    monkeypatch.setattr(hoststate, "snapshot", compare_then_concurrent_write)
    changed, said = g.end()
    assert changed and any("FAILED" in s for s in said), said
    assert tabf.read_text() == f"0 1 * * * keep\n{FOREIGN}\n"


def test_installers_write_to_the_fake_store_only_under_the_marker_with_the_real_home(tmp_path, monkeypatch):
    from ttp import hoststate
    store = tmp_path / "store"
    monkeypatch.setenv(hoststate.MARKER, str(store))
    monkeypatch.setattr(hoststate, "real_home", lambda: pathlib.Path.home())
    assert hoststate.unit_dir() == store / "systemd" / "user"
    assert hoststate.argv(["crontab", "-"]) == [str(store / "bin" / "crontab"), "-"]
    assert hoststate.argv(["git", "x"]) == ["git", "x"]
    monkeypatch.setattr(hoststate, "real_home", lambda: tmp_path / "elsewhere")   # a test's own temp HOME
    assert hoststate.unit_dir() == pathlib.Path.home() / ".config" / "systemd" / "user"
    assert hoststate.argv(["crontab", "-"]) == ["crontab", "-"]


def test_the_host_guard_ledger_record_says_when_this_host_is_not_listed(monkeypatch):
    from ttp import hoststate, machines
    monkeypatch.setattr(machines, "here", lambda: None)
    assert hoststate.record("x").startswith("not recorded in the machine change ledger")


def test_this_test_session_runs_with_the_host_stand_ins_first_on_path():
    store = os.environ.get("TTP_HOSTSTATE_FAKE")
    assert store and os.environ["PATH"].split(os.pathsep)[0] == os.path.join(store, "bin")
    assert shutil.which("crontab") == os.path.join(store, "bin", "crontab")


def test_the_notifier_and_tunnel_installers_write_their_units_to_the_fake_store_under_the_marker(tmp_path, monkeypatch):
    from ttp import hoststate, notifier, tunnel
    home, store = tmp_path / "home", tmp_path / "store"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(hoststate.MARKER, str(store))
    monkeypatch.setattr(hoststate, "real_home", lambda: home)   # a run with the account's real HOME
    monkeypatch.delenv("TTP_TEST_NO_NOTIFIER", raising=False)
    monkeypatch.setattr(notifier.sys, "platform", "linux")
    notifier.install()
    unit = store / "systemd" / "user" / notifier.UNIT
    assert unit.is_file() and not (home / ".config" / "systemd").exists()
    assert notifier.agent_file() == unit
    calls = [json.loads(ln) for ln in (store / "calls.jsonl").read_text().splitlines()]
    assert any(c.get("cmd") == "systemctl" for c in calls), calls   # the stand-in, never the real one
    assert notifier.uninstall() == "removed" and not unit.exists()
    assert tunnel.service_file("x", "linux") == store / "systemd" / "user" / f"{tunnel.label('x')}.service"
    assert tunnel._run("systemctl", "--user", "daemon-reload").returncode == 0
    assert not (home / ".config" / "systemd").exists()


def _stopped_after_write(tabf):
    """A wait that stops the check (an urgent preemption, run_detached's SIGTERM) once it wrote the crontab."""
    def wait(proc, *a, **kw):
        for _ in range(200):
            if tabf.exists() and "evil" in tabf.read_text():
                break
            time.sleep(0.05)
        raise SystemExit(143)
    return wait


def test_a_push_check_stopped_mid_run_still_restores_and_reports_a_host_change(tmp_path, monkeypatch):
    from ttp import push
    cron, tabf, _ = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n")
    _ledger(monkeypatch)
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    monkeypatch.setattr(push, "_timed_wait", _stopped_after_write(tabf))
    said = []
    with pytest.raises(SystemExit):
        push._run_one(_append(cron, "@reboot $TTP_RUN_DIR/evil", "$TTP_RUN_DIR") + " ; exec sleep 30", tmp_path, env, said.append)
    assert tabf.read_text() == "0 1 * * * keep\n"
    assert any("host guard: FAILED" in s for s in said), said


def test_a_ttp_checks_command_stopped_mid_run_still_restores_and_reports_a_host_change(tmp_path, monkeypatch):
    from ttp import checkwall, cli, push
    cron, tabf, _ = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n")
    _ledger(monkeypatch)
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    monkeypatch.setattr(checkwall, "wait", _stopped_after_write(tabf))
    log = tmp_path / "checks.log"
    with open(log, "w") as out, pytest.raises(SystemExit):
        cli._run_check(["sh", "-c", _append(cron, "@reboot $TTP_RUN_DIR/evil", "$TTP_RUN_DIR") + " ; exec sleep 30"], True,
                       cwd=tmp_path, stdout=out, stderr=subprocess.STDOUT, env=env)
    assert tabf.read_text() == "0 1 * * * keep\n"
    assert "host guard: FAILED" in log.read_text()


def test_a_stopped_guard_reports_once_and_never_masks_the_stop(monkeypatch):
    from ttp import hoststate
    monkeypatch.setattr(hoststate, "snapshot", lambda: {"crontab": None, "units": {}})
    g = hoststate.Guard({})
    g.end()
    monkeypatch.setattr(hoststate, "snapshot", lambda: 1 / 0)
    said = []
    g.stopped(said.append)   # already ended: nothing twice
    g2 = hoststate.Guard.__new__(hoststate.Guard)
    g2.done, g2.store, g2.since, g2.before = False, None, 0, {}
    g2.stopped(said.append)   # its compare fails: swallowed
    assert said == []


def test_a_batch_check_stopped_mid_run_still_restores_and_reports_a_host_change(tmp_path, monkeypatch, capsys):
    from ttp import batch, push
    cron, tabf, _ = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n")
    _ledger(monkeypatch)
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    stop = _stopped_after_write(tabf)

    class Stopped(subprocess.Popen):
        def wait(self, timeout=None):
            if timeout is None or timeout > 60:   # the run's own wait, not the kill's grace
                stop(self)
            return super().wait(timeout)
    monkeypatch.setattr(batch.subprocess, "Popen", Stopped)
    with pytest.raises(SystemExit):
        batch._streamed(_append(cron, "@reboot $TTP_RUN_DIR/evil", "$TTP_RUN_DIR") + " ; exec sleep 30", tmp_path, env, 3600,
                        True, (), None)
    assert tabf.read_text() == "0 1 * * * keep\n"
    assert "host guard: FAILED" in capsys.readouterr().out


def test_a_nested_run_reuses_its_inherited_stand_ins_without_writing_to_their_folder(tmp_path, monkeypatch):
    """A child run under `ttp checks` inherits the store: fake_env makes no folder or file there, so a
    run whose temp files must stay in its own TMPDIR never touches the outer store's folder."""
    from ttp import hoststate
    store = tmp_path / "store"
    env = hoststate.fake_env({"PATH": os.defpath}, store)
    calls = []
    monkeypatch.setattr(os, "mkdir", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(os, "replace", lambda *a, **k: calls.append(a))
    again = hoststate.fake_env(dict(env))
    assert again[hoststate.MARKER] == str(store) and again["PATH"].split(os.pathsep)[0] == str(store / "bin")
    assert calls == []


def _shim_layers(tmp_path, project_shim: str):
    """hoststate's stand-ins plus a project's own stand-in folder (no .ttp-shims marker) ahead of them
    and a real systemctl after them; returns (env, store, project folder, real folder)."""
    from ttp import hoststate
    store, proj, real = tmp_path / "store", tmp_path / "proj", tmp_path / "real"
    proj.mkdir()
    real.mkdir()
    bin_dir = hoststate.make(store)
    (proj / "systemctl").write_text("#!/bin/sh\n" + project_shim)
    (real / "systemctl").write_text(f"#!/bin/sh\necho real >> {shlex.quote(str(tmp_path / 'hops'))}\nexit 3\n")
    for f in (proj / "systemctl", real / "systemctl"):
        f.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("TTP_SHIM_")}
    env["PATH"] = os.pathsep.join([str(proj), str(bin_dir), str(real), "/usr/bin", "/bin"])
    return env, store, proj, real


def test_a_read_through_two_stand_in_layers_without_a_marker_reaches_the_real_command_in_one_hop_each(tmp_path):
    """A project's stand-in folder without the marker first on PATH, passing a read to the next systemctl
    on PATH: each layer runs once and the real command's exit code comes back (it once looped forever)."""
    hops = shlex.quote(str(tmp_path / "hops"))
    env, store, proj, real = _shim_layers(tmp_path, f'echo proj >> {hops}\nself=$(cd "$(dirname "$0")" && pwd)\n'
                                          'IFS=:; for d in $PATH; do [ "$d" = "$self" ] && continue; '
                                          '[ -x "$d/systemctl" ] && exec "$d/systemctl" "$@"; done; exit 127\n')
    r = subprocess.run(["systemctl", "status", "x.service"], env=env, timeout=30, capture_output=True, text=True)
    assert r.returncode == 3, r.stderr
    assert (tmp_path / "hops").read_text().split() == ["proj", "real"]
    assert len((store / "calls.jsonl").read_text().splitlines()) == 1


def test_the_hop_limit_ends_a_deliberate_stand_in_loop_fast(tmp_path):
    """A stand-in that wipes the passed-folder list and calls hoststate's stand-in by path forever still
    stops after MAX_HOPS: the read goes to the system folders' command (or fails with a clear error)."""
    from ttp import hostshim
    hops = shlex.quote(str(tmp_path / "hops"))
    env, store, proj, real = _shim_layers(tmp_path, f'echo proj >> {hops}\n'
                                          f'exec env -u TTP_SHIM_SEEN {shlex.quote(str(tmp_path / "store" / "bin" / "systemctl"))} "$@"\n')
    real.joinpath("systemctl").unlink()
    env["PATH"] = os.pathsep.join([str(store / "bin"), str(proj), "/usr/bin", "/bin"])
    start = time.monotonic()
    r = subprocess.run(["systemctl", "--version"], env=env, timeout=30, capture_output=True, text=True)
    assert time.monotonic() - start < 20
    assert len((tmp_path / "hops").read_text().split()) == hostshim.MAX_HOPS
    assert len((store / "calls.jsonl").read_text().splitlines()) == hostshim.MAX_HOPS + 1
    assert r.returncode == 0 or "passed it on" in r.stderr, r.stderr


def test_a_check_run_on_another_machine_leaves_this_hosts_crontab_unjudged(tmp_path, monkeypatch):
    """A check spilled to another host or a Slurm job runs its tests there: a crontab line another
    project adds or removes here meanwhile is neither put back nor recorded, and end() says why."""
    from ttp import hoststate, push, remotecheck, slurmcheck
    _, tabf, _ = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n@reboot other/resume.sh\n")
    got = _ledger(monkeypatch)
    spill = ["nice", "-n", "10", *remotecheck.argv("pytest -q", "spill-host")]
    assert hoststate.elsewhere(spill) == "spill-host"
    assert hoststate.elsewhere(slurmcheck.argv("pytest -q", {}, minutes=5)) == "a Slurm batch job"
    assert hoststate.elsewhere(["bash", "-c", "pytest -q"]) == "" == hoststate.elsewhere(None)
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    guard = hoststate.Guard(env, argv=spill)
    assert guard.before is None
    tabf.write_text("0 1 * * * keep\n0 2 * * * added\n")
    changed, said = guard.end()
    assert not changed and not got and tabf.read_text() == "0 1 * * * keep\n0 2 * * * added\n"
    assert said and "spill-host" in said[0] and "not compared" in said[0], said
    assert hoststate.Guard(env, argv=["bash", "-c", "true"]).before is not None   # a local check is judged


def test_push_and_ttp_checks_hand_the_check_command_to_the_host_guard(tmp_path, monkeypatch):
    """Both check runners pass the command they run, so a spilled one is not judged against this host."""
    from ttp import hoststate, push
    cron, tabf, _ = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n")
    got = _ledger(monkeypatch)
    seen = []
    monkeypatch.setattr(hoststate, "elsewhere", lambda argv: seen.append(argv) or "spill-host")
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    said = []
    push._run_one(_append(cron, "0 3 * * * meanwhile", "$TTP_RUN_DIR"), tmp_path, env, said.append)
    assert "meanwhile" in tabf.read_text() and not got, said
    assert seen and "meanwhile" in " ".join(map(str, seen[-1])), seen
    tabf.write_text("0 1 * * * keep\n")
    _run_check_ended(["bash", "-c", _append(cron, "0 4 * * * meanwhile", "$TTP_RUN_DIR")], env=env)
    assert "0 4 * * * meanwhile" in tabf.read_text() and not got
