# Area cases for test_tt_project_runtime.py: a test run never changes the host's real crontab,
# systemd user units or power state (hoststate, hostshim).


def _fake_host(tmp_path, monkeypatch, tab: str | None = None):
    """A stand-in for the host's real crontab (a script by absolute path, backed by a file) and
    systemd user unit folder; returns (crontab script, crontab file, unit folder)."""
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
    monkeypatch.setattr(hoststate, "_real_dir", lambda: units)
    monkeypatch.setattr(hoststate, "_real", lambda name: str(cron) if name == "crontab" else None)
    return cron, tabf, units


def _ledger(monkeypatch):
    from ttp import machine_ledger, machines
    got = []
    monkeypatch.setattr(machines, "here", lambda: ("box1", {}))
    monkeypatch.setattr(machine_ledger, "add_change",
                        lambda *a, **kw: got.append((a, kw)) or {"id": 7})
    return got


def test_shimmed_crontab_systemctl_and_reboot_under_ttp_checks_leave_the_host_untouched_and_are_reported(
        tmp_path, monkeypatch):
    from ttp import cli, hoststate, push
    _, tabf, units = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n")
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    assert env.get(hoststate.MARKER) and env["PATH"].split(os.pathsep)[0].endswith("bin")
    assert env[hoststate.MARKER].startswith(str(tmp_path)), "its own store, not the session's shared one"
    log = tmp_path / "checks.log"
    script = ("echo '@reboot evil' | crontab - && crontab -l | grep -q evil && "
              "systemctl --user enable evil.service && systemctl --user is-enabled evil.service && "
              "reboot && at now < /dev/null")
    with open(log, "w") as out:
        rc = cli._run_check(["sh", "-c", script], True, cwd=tmp_path, stdout=out, stderr=subprocess.STDOUT, env=env)
    text = log.read_text()
    assert rc == 0, text
    assert tabf.read_text() == "0 1 * * * keep\n" and not list(units.iterdir())
    said = text[text.index("host guard: these calls went to the run's fake store"):]
    for call in ("crontab -", "systemctl --user enable evil.service", "reboot", "at now"):
        assert call in said, said
    assert "FAILED" not in text


def test_a_host_change_the_stand_ins_miss_fails_ttp_checks_and_is_restored_and_recorded(tmp_path, monkeypatch):
    from ttp import cli, hoststate, push
    cron, tabf, units = _fake_host(tmp_path, monkeypatch, "0 1 * * * keep\n")
    (units / "a.service").write_text("[Service]\nExecStart=/bin/true\n")
    got = _ledger(monkeypatch)
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    log = tmp_path / "checks.log"
    script = (f"printf '0 1 * * * keep\\n@reboot evil\\n# tt-project: own\\n' | {shlex.quote(str(cron))} - && "
              f"echo x > {shlex.quote(str(units / 'evil.service'))} && rm {shlex.quote(str(units / 'a.service'))} && "
              f"mkdir {shlex.quote(str(units / 'default.target.wants'))} && "
              f"ln -s ../evil.service {shlex.quote(str(units / 'default.target.wants' / 'evil.service'))}")
    with open(log, "w") as out:
        rc = cli._run_check(["sh", "-c", script], True, cwd=tmp_path, stdout=out, stderr=subprocess.STDOUT, env=env)
    text = log.read_text()
    assert rc == 1, text
    assert "host guard: FAILED" in text and "@reboot evil" in text and "evil.service" in text, text
    assert "recorded in the machine change ledger (box1, change 7)" in text
    # Put back: the added line gone, the harness's own line kept, the unit files as they were
    assert tabf.read_text() == "0 1 * * * keep\n# tt-project: own\n"
    assert sorted(p.name for p in units.iterdir()) == ["a.service"]
    assert (units / "a.service").read_text() == "[Service]\nExecStart=/bin/true\n"
    (alias, what, undo, _by), kw = got[0]
    assert alias == "box1" and "@reboot evil" in what and undo.startswith("none:") and kw["test"] is True


def test_a_failing_check_keeps_its_exit_code_when_it_also_changed_the_host(tmp_path, monkeypatch):
    from ttp import cli, push
    cron, tabf, _ = _fake_host(tmp_path, monkeypatch)
    _ledger(monkeypatch)
    env = push.check_env("checks", run_dir=str(tmp_path / "run"))
    with open(tmp_path / "log", "w") as out:
        rc = cli._run_check(["sh", "-c", f"echo '@reboot x' | {shlex.quote(str(cron))} - ; exit 3"], True,
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
        assert sandbox.run(["sh", "-c", f"echo b | {shlex.quote(str(cron))} -"]) == 1
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    assert tabf.read_text() == "a\n"
    assert "host guard: FAILED" in capsys.readouterr().err


def test_sudo_running_a_host_write_under_ttp_checks_is_refused_and_reported_and_other_sudo_calls_pass(
        tmp_path, monkeypatch):
    # sudo resets PATH (secure_path): the stand-ins never see what it runs, so the sudo stand-in refuses
    from ttp import cli, push
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
        rc = cli._run_check(["sh", "-c", script], True, cwd=tmp_path, stdout=out, stderr=subprocess.STDOUT, env=env)
    text = log.read_text()
    assert rc == 0, text
    assert text.count("rc=1") == len(refused) and text.count("sudo: refused in a test run") == len(refused), text
    assert seen.read_text().splitlines() == [c[len("sudo "):] for c in allowed], "only the reads reached sudo"
    said = text[text.index("host guard: these calls went to the run's fake store"):]
    assert "sudo -n systemctl enable evil.service" in said and "sudo -n crontab -l" not in said, said


def test_a_host_restore_keeps_a_concurrent_unrelated_crontab_line(tmp_path, monkeypatch):
    from ttp import hoststate
    _, tabf, _ = _fake_host(tmp_path, monkeypatch, "a\nevil\nother\nevil\n")
    before = {"crontab": "a\nevil\n", "units": {}}
    assert hoststate.restore(before, {"crontab": (["evil"], [])}) == []
    assert tabf.read_text() == "a\nevil\nother\n"   # the last copy of the added line went
    assert hoststate.diff({"crontab": "a\n", "units": {}}, {"crontab": "a\n# tt-project: x\n", "units": {}}) == {}


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
