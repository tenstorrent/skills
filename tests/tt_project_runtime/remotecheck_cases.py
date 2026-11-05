# Remote check host tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file. A fake
# `ssh` on PATH drops its -o options and the host and runs the last argument with `sh -c` here, so
# no network is needed.
from __future__ import annotations

_RC_FAKE_SSH = r'''#!/bin/sh
# A fake ssh: drops -o options and the host, runs the last argument with sh -c. Host "down" is
# unreachable; FAKE_SSH_FAIL=<text> exits 255 at once on a script holding it (reading nothing);
# FAKE_SSH_DROP=<text> runs such a script, then drops the connection before its exit-code marker.
# FAKE_SSH_LOG=<file> gets one line per call naming the host.
while [ "$1" = "-o" ]; do shift 2; done
host=$1
shift
[ -n "$FAKE_SSH_LOG" ] && echo "$host" >> "$FAKE_SSH_LOG"
if [ "$host" = down ]; then echo "ssh: connect to host down port 22: Connection refused" >&2; exit 255; fi
[ -n "$FAKE_REMOTE_TMP" ] && export TMPDIR="$FAKE_REMOTE_TMP"
case "$1" in *"${FAKE_SSH_FAIL:-@none@}"*) echo "ssh: connection to $host lost" >&2; exit 255;; esac
case "$1" in *"${FAKE_SSH_DROP:-@none@}"*) sh -c "$1" | sed '/ttp-remote-rc:/d'; exit 255;; esac
exec sh -c "$1"
'''


def _rc_fake_ssh(tmp_path):
    """A bin folder holding the fake ssh, and the folder its remote temp dirs go to."""
    bin_, remote = tmp_path / "fakessh", tmp_path / "remote"
    bin_.mkdir(exist_ok=True)
    remote.mkdir(exist_ok=True)
    ssh = bin_ / "ssh"
    ssh.write_text(_RC_FAKE_SSH)
    ssh.chmod(0o755)
    return bin_, remote


def _rc_repo(tmp_path, big=False):
    repo = tmp_path / "work"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("a\n")
    if big:   # far more than a pipe buffer: git archive is still writing when ssh drops
        (repo / "big.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "a"], check=True)
    return repo


def _rc_run(tmp_path, repo, cmd, host="box1", env=None, wait=True, commit="", **fake):
    """remotecheck run as callers run it (push.check_argv's argv) in `repo` against the fake ssh:
    (exit code, output); with wait=False the Popen."""
    from ttp import remotecheck
    bin_, remote = _rc_fake_ssh(tmp_path)
    penv = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_REMOTE_TMP": str(remote), **fake}
    proc = subprocess.Popen(remotecheck.argv(cmd, host, env, commit), cwd=repo, env=penv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    if not wait:
        return proc
    out = proc.communicate(timeout=60)[0]
    return proc.returncode, out


def _rc_left(tmp_path):
    return sorted(p.name for p in (tmp_path / "remote").glob("ttp-check.*"))


def test_a_remote_check_runs_on_a_git_checkout_of_exactly_head_and_cleans_up(tmp_path):
    """The command runs in a git work tree whose tree is HEAD's (not the uncommitted edit, not the
    untracked file), with the check's env exported; its exit code is the check's (75 as 1), and the
    temp dir is gone after a pass and a failure."""
    repo = _rc_repo(tmp_path)
    tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], capture_output=True,
                          text=True).stdout.strip()
    (repo / "a.txt").write_text("uncommitted\n")
    (repo / "untracked.txt").write_text("x\n")
    out = tmp_path / "seen"
    out.mkdir()
    cmd = (f"git rev-parse --is-inside-work-tree > {out}/inside; git rev-parse HEAD^{{tree}} > {out}/tree; "
           f"git status --porcelain > {out}/status; cat a.txt > {out}/a; test ! -e untracked.txt; "
           f"printf %s \"$GREETING\" > {out}/env; pwd > {out}/pwd; echo hello")
    rc, said = _rc_run(tmp_path, repo, cmd, env={"GREETING": 'hi "there"'})
    assert rc == 0, said
    assert (out / "inside").read_text().strip() == "true"
    assert (out / "tree").read_text().strip() == tree, "the exact head, as git archive copies it"
    assert (out / "status").read_text() == "" and (out / "a").read_text() == "a\n"
    assert (out / "env").read_text() == 'hi "there"'
    assert "/ttp-check." in (out / "pwd").read_text() and "hello" in said and "ttp-remote-rc" not in said
    assert _rc_left(tmp_path) == []
    assert _rc_run(tmp_path, repo, "exit 3")[0] == 3
    rc, said = _rc_run(tmp_path, repo, "exit 75")
    assert rc == 1 and "reported as 1" in said
    assert _rc_left(tmp_path) == []


def test_a_remote_check_given_a_commit_copies_that_commit_not_the_moved_head(tmp_path):
    """--commit (the commit a `ttp checks` run records): the host gets that commit though HEAD moved
    since; a commit this checkout lacks, or an option-like value, copies nothing and fails (exit 2)."""
    repo = _rc_repo(tmp_path)
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    first = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "a.txt").write_text("moved\n")
    subprocess.run([*git, "commit", "-qam", "moved"], check=True)
    rc, said = _rc_run(tmp_path, repo, "cat a.txt", commit=first)
    assert rc == 0 and "a\n" in said and "moved" not in said and f"checking {first[:12]}" in said, said
    assert "moved" in _rc_run(tmp_path, repo, "cat a.txt")[1], "no commit given: HEAD, as before"
    for bad in ("0" * 40, "--output=x", "-q"):
        rc, said = _rc_run(tmp_path, repo, "echo ran", commit=bad)
        assert rc == 2 and "ran" not in said.replace("nothing to copy", ""), (bad, said)
    assert _rc_left(tmp_path) == []


def test_ssh_failing_at_any_step_of_a_remote_check_is_an_environment_fault(tmp_path, monkeypatch):
    """ssh exiting 255 when making the temp dir, during the copy (also of a tree bigger than a pipe
    buffer, where git archive dies of SIGPIPE) or mid-run exits 75, never the change's 1 or 2."""
    from ttp import remotecheck
    repo = _rc_repo(tmp_path, big=True)
    for fake in ({"FAKE_SSH_FAIL": "mktemp"}, {"FAKE_SSH_FAIL": "tar -xf"}, {"FAKE_SSH_DROP": "ttp-remote-rc"}):
        rc, said = _rc_run(tmp_path, repo, "true", **fake)
        assert rc == remotecheck.FAULT_RC, (fake, said)
        assert f"{remotecheck.SIG}box1" in said and "git archive" not in said, said
    assert _rc_run(tmp_path, repo, "true", host="down")[0] == remotecheck.FAULT_RC
    assert _rc_left(tmp_path) == []
    bin_, _ = _rc_fake_ssh(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    assert remotecheck.reachable("box1") == ""
    assert "cannot reach down" in remotecheck.reachable("down")


def _rc_gone(pid, within=10.0):
    end = time.time() + within
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if pathlib.Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
                return True
        except (OSError, IndexError):
            pass
        time.sleep(0.05)
    return False


def test_a_stopped_remote_check_stops_its_command_on_the_host(tmp_path):
    """SIGTERM to the local side (a timeout, a `ttp checks` stop, a queue stop) stops the command where
    it runs (ssh without a tty signals nothing) and removes its temp dir."""
    repo = _rc_repo(tmp_path)
    started, marker = tmp_path / "started", tmp_path / "marker"
    proc = _rc_run(tmp_path, repo, f"sleep 30 & echo $! > {started}.tmp; mv {started}.tmp {started}; wait $!; "
                                   f"echo alive > {marker}", wait=False)
    try:
        end = time.time() + 30
        while not started.exists() and time.time() < end:
            time.sleep(0.05)
        assert started.exists(), proc.stdout.read() if proc.poll() is not None else "never started"
        sleeper = int(started.read_text())
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 143
    assert _rc_gone(sleeper), "the command's process group was stopped"
    time.sleep(0.3)
    assert not marker.exists() and _rc_left(tmp_path) == []


def test_a_remote_checks_command_starts_with_ctrl_c_and_quit_at_their_default(tmp_path):
    """A non-interactive shell starts a background job with SIGINT and SIGQUIT ignored, and a shell
    cannot undo a signal ignored at its start: the host's launch resets both, so a command (a test
    sending Ctrl-C) behaves on a check host as it does here."""
    repo = _rc_repo(tmp_path)
    seen = tmp_path / "seen"
    probe = ("import signal; print(*(signal.getsignal(s) is signal.SIG_IGN "
             "for s in (signal.SIGINT, signal.SIGQUIT)))")
    rc, said = _rc_run(tmp_path, repo, f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)} > {seen}")
    assert rc == 0, said
    assert seen.read_text().split() == ["False", "False"], "INT and QUIT not ignored on the host"


def test_ttp_checks_on_a_check_host_names_it_and_waits_when_it_cannot_reach_it(env, tmp_path, monkeypatch):
    """delivery.checks_host: `ttp checks` runs there, and checks.json and checks.log say so. A host ssh
    cannot reach (found by the probe, or mid-run) exits 75 with env_fault env:check-host:<host>, records
    no pass, and counts nothing against the change."""
    from ttp import cli, envfault
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    bin_, remote = _rc_fake_ssh(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    p.set_config("delivery.checks_host", "box1")
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["hosts"] == {cmd: "box1"}, rec
    assert f"$ {cmd} (on box1)" in (run / "checks.log").read_text()
    assert "/ttp-check." in log.read_text() and _rc_left(tmp_path) == []
    tasks = p.db.one("SELECT COUNT(*) n FROM tasks")["n"]
    for host, fake in (("down", {}), ("box1", {"FAKE_SSH_FAIL": "mktemp"})):
        for k, v in fake.items():
            monkeypatch.setenv(k, v)
        p.set_config("delivery.checks_host", host)
        (run / "checks.json").unlink()
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--fresh"])
        assert e.value.code == 75, host
        rec = json.loads((run / "checks.json").read_text())
        assert rec["passed"] is False and rec["env_fault"] == f"env:check-host:{host}", rec
        assert not (run / "check_failure.json").exists()
        assert f"env:check-host:{host}" in envfault.open_faults(p)
    assert p.db.one("SELECT COUNT(*) n FROM tasks")["n"] == tasks, "no review or fix task"
    assert len(log.read_text().splitlines()) == 1, "the unreachable host ran nothing"


def test_ttp_push_runs_its_checks_on_the_check_host_and_waits_when_ssh_fails(env, monkeypatch):
    from ttp import envfault, push
    log = env["tmp"] / "where.log"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"pwd >> {log}"])
    p.set_config("delivery.checks_host", "box1")
    bin_, remote = _rc_fake_ssh(env["tmp"])
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 0
    assert "/ttp-check." in log.read_text()
    _commit(repo, "more.txt", "more\n")
    monkeypatch.setenv("FAKE_SSH_FAIL", "mktemp")   # reachable for the probe, gone for the check
    assert _ttp_push() == push.BUSY
    assert "env:check-host:box1" in envfault.open_faults(p)
    monkeypatch.delenv("FAKE_SSH_FAIL")
    p.set_config("delivery.checks_host", "down")
    assert _ttp_push() == push.BUSY
    assert len(log.read_text().splitlines()) == 1


def test_a_push_batch_runs_on_the_check_host_and_holds_on_its_fault(env, monkeypatch):
    """The push queue's batch runs the remote argv; a check host that drops is envfault.Fault (the
    batch ends env_fault, entries requeued), not check_failed."""
    from ttp import envfault, push
    log = env["tmp"] / "where.log"
    p, repo, origin, other = _bump_setup(env, monkeypatch, [f"pwd >> {log}"])
    p.set_config("delivery.checks_host", "box1")
    bin_, remote = _rc_fake_ssh(env["tmp"])
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e1", {"plugins/p/f1.txt": "1\n"})]), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed", m
    assert "/ttp-check." in log.read_text()
    monkeypatch.setenv("FAKE_SSH_FAIL", "mktemp")
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e2", {"plugins/p/f2.txt": "2\n"})], bid="b2"), monkeypatch)
    assert rc == push.BUSY and m["outcome"] == "env_fault" and _statuses(m) == ["requeued"], m
    assert envfault.open_faults(p)["env:check-host:box1"]["queue"] is True


def test_a_paused_fixed_check_host_is_a_fault_with_no_ssh_and_runs_again_once_the_pause_lifts(
        env, tmp_path, monkeypatch, capsys):
    """delivery.checks_host paused (this project's pause, another project's of its alias or host name, or a
    shared pause file) is env:check-host:<host> with 'paused by <who>: <reason>': `ttp checks` (its probe)
    and the remote check itself (mid-run route) exit 75 and never ssh there, and nothing passes or fails.
    Once the pause lifts the check runs there. A "local" check is unaffected; an unreadable other project
    is no pause."""
    from ttp import cli, db as dbm, envfault, machines, remotecheck, shared
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    bin_, remote = _rc_fake_ssh(tmp_path)
    calls = tmp_path / "ssh.calls"
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    monkeypatch.setenv("FAKE_SSH_LOG", str(calls))
    ssh_calls = lambda: calls.read_text().splitlines() if calls.exists() else []
    machines.add("check-host-a", hostname="check-host-a.example")
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    p.set_config("delivery.checks_host", "check-host-a")
    other = _second_project(env, "other")
    pauses = [
        (lambda on: p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"check-host-a": {"reason": "lent out", "by": "user"}}
                                if on else {}), f"paused by {p.name}: lent out"),
        (lambda on: other.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"check-host-a": {"reason": "lent", "by": "user"}}
                                    if on else {}), "paused by other: lent"),
        (lambda on: other.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"check-host-a.example": {"reason": "repair"}}
                                    if on else {}), "paused by other: repair"),
        (lambda on: shared.update_pause("check-host-a", lambda was: {"reason": "held", "since": time.time(),
                                                                       "project": "third"} if on else None),
         "paused by third: held"),
    ]
    for pause, said in pauses:
        pause(True)
        assert remotecheck.paused("check-host-a", repo) == said
        (run / "checks.json").unlink(missing_ok=True)
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--fresh"])
        assert e.value.code == 75, said
        rec = json.loads((run / "checks.json").read_text())
        assert rec["passed"] is False and rec["env_fault"] == "env:check-host:check-host-a", rec
        assert not (run / "check_failure.json").exists()
        got = capsys.readouterr()
        assert f"check host check-host-a is {said}" in got.out + got.err, got
        rc, out = _rc_run(tmp_path, repo, cmd, host="check-host-a")
        assert rc == remotecheck.FAULT_RC and f"{remotecheck.SIG}check-host-a: {said}." in out, out
        assert out.count("\n") == 1, "one line"
        f = envfault.probe(remotecheck.fault("check-host-a", cmd, str(repo)))
        assert f and f["why"] == said, "the fault stays open while paused"
        assert ssh_calls() == [] and not log.exists(), (said, ssh_calls())
        pause(False)
    assert remotecheck.paused("check-host-a", repo) == ""
    assert envfault.probe(remotecheck.fault("check-host-a", cmd, str(repo))) is None, "the lifted pause clears it"
    (run / "checks.json").unlink()
    cli.main(["checks", "--fresh"])
    assert json.loads((run / "checks.json").read_text())["passed"] is True
    assert "/ttp-check." in log.read_text() and ssh_calls(), "it ran there"
    pauses[0][0](True)
    p.set_config("delivery.push_checks", [{"run": cmd, "host": "local"}])
    n = len(ssh_calls())
    (run / "checks.json").unlink()
    cli.main(["checks", "--fresh"])
    assert json.loads((run / "checks.json").read_text())["passed"] is True and len(ssh_calls()) == n, "local"
    pauses[0][0](False)
    (Path(other.root) / "tt-project" / "state" / "project.db").write_bytes(b"not a database")
    assert remotecheck.paused("check-host-a", repo) == "", "an unreadable other project is no pause"
    rc, out = _rc_run(tmp_path, repo, cmd, host="check-host-a")
    assert rc == 0, out


def test_check_host_settings_are_validated_and_local_keeps_a_check_here():
    from ttp import coordinator, project, push
    assert not project.config_problems({"delivery": {"checks_host": "build-box.example"}})
    assert any("delivery.checks_host" in x for x in project.config_problems({"delivery": {"checks_host": "a b"}}))
    assert any("delivery.checks_host" in x for x in project.config_problems({"delivery": {"checks_host": 3}}))
    assert push._form_problem({"run": "make test", "host": "box1", "env": {"PATH": "$HOME/bin:$PATH"}}) is None
    assert push._form_problem({"run": "make test", "host": "local"}) is None
    for bad in ({"host": "a;b"}, {"host": ""}, {"env": {"1X": "v"}}, {"env": {"X": 1}}, {"env": ["X=1"]}):
        assert push._form_problem({"run": "make test", **bad}), bad
    settable = coordinator.USER_SETTABLE["delivery.checks_host"]
    assert settable("box1") == "box1" and settable("") == ""
    with pytest.raises(ValueError):
        settable("a b")
    assert push.checks_of([{"run": "make test", "host": "box1", "env": {"A": "1"}}]) == [
        {"run": "make test", "host": "box1", "env": {"A": "1"}}]
    here, there = push.with_host(push.check_list([{"run": "make a", "host": "local"}, "make b"]), "box1")
    assert push.remote_host(here) == "" and push.where(here) == ""
    assert "--host" not in push.check_argv(here) and push.check_argv(here)[-1] == "make a"
    argv = push.check_argv(there)
    assert argv[argv.index("--host") + 1] == "box1" and argv[-1] == "make b" and push.where(there) == " (on box1)"
    assert push.with_host(push.check_list(["make c"]), "a b") == ["make c"] and not push.remote_host(
        push.with_host(push.check_list(["make c"]), "a b")[0])


def _spill_setup(env, tmp_path, monkeypatch, hosts, charter=("check-host-a", "check-host-b", "down")):
    """A checks repo whose project allows `charter` (charter text and machines list), spills to `hosts`
    after 0.3 s, has one checks lane, and the fake ssh on PATH."""
    from ttp import machines
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    p.charter_path.write_text("# Charter\n## Resources\n" + "".join(f"- {h}: a check host\n" for h in charter))
    for h in charter:
        machines.add(h)
    p.set_config("checks.slots", 1)
    p.set_config("delivery.checks_spill_hosts", list(hosts))
    p.set_config("delivery.checks_spill_after_s", 0.3)
    bin_, remote = _rc_fake_ssh(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    return p, repo, run, remote


def _spill_lane(p, repo):
    """Hold the project's only checks lane for `repo`, as another run of the project would."""
    from ttp import testslots
    g = testslots.suite_hold(p.name, wait_s=0, slots=1, repo=testslots.repo_key(str(repo)))
    assert g is not None and g.files
    return g


def test_busy_checks_spill_to_the_first_reachable_allowed_host_and_name_it(env, tmp_path, monkeypatch):
    """delivery.checks_spill_hosts: with the lane here held past checks_spill_after_s, the run skips an
    unreachable host (never a failure), runs on the next listed one, and checks.json and checks.log name
    it; its claim on the host shows in ttp status while held and is freed after."""
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["down", "check-host-b", "check-host-a"])
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-b", rec
    assert rec["hosts"] == {cmd: "check-host-b"}, rec
    text = (run / "checks.log").read_text()
    assert "down (ssh cannot reach it" in text and "spilling to check-host-b" in text, text
    assert f"$ {cmd} (on check-host-b)" in text
    assert "/ttp-check." in log.read_text() and _rc_left(tmp_path) == []
    assert checkspill.holders(p.name) == [], "the claim ends with the run"
    claim = checkspill._claim("check-host-a", 1, {"project": p.name, "task": "7", "pid": os.getpid(),
                                                  "since": time.time(), "what": "pytest -q", "host": "check-host-a"})
    try:
        assert checkspill.status_lines(p.name) == [
            "checks of task #7 running on spill host check-host-a for 0 min: pytest -q"]
        assert checkspill._claim("check-host-a", 0, {}) is None, "this machine's claims count against the host"
    finally:
        claim.close()
    assert checkspill.status_lines(p.name) == []


def test_spill_skips_hosts_that_are_not_allowed_busy_or_down_and_keeps_waiting_here(env, tmp_path, monkeypatch):
    """No listed host can take the run (not in the charter, not in the machines list, paused, unreachable,
    every slot busy): the run says why for each, keeps its place and runs here once the lane frees."""
    import threading
    from ttp import checklane, checkspill, cli, db as dbm, machines
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch,
                                        ["outside", "unlisted", "check-host-c", "down", "check-host-a"],
                                        charter=("check-host-a", "check-host-c", "down", "unlisted"))
    machines.add("outside")
    machines.remove("unlisted")
    p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"check-host-c": {"reason": "user hold", "by": "user", "since": 1}})
    for i in range(checklane.MAX_LANES):   # remote checks from other machines fill check-host-a
        (remote / f"ttp-check.busy{i}").mkdir()
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    lane = _spill_lane(p, repo)
    threading.Timer(1.5, lane.close).start()
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and "spilled_to" not in rec and "hosts" not in rec, rec
    text = (run / "checks.log").read_text()
    for why in ("outside (not named in the charter)", "unlisted (not in the machines list",
                "check-host-c (paused: user hold)", "down (ssh cannot reach it", "check-host-a (its "):
        assert why in text, (why, text)
    assert "/ttp-check." not in log.read_text(), "it ran here"
    assert checkspill.allowed(p, p.config(), "check-host-a") == ""


def test_spill_skips_a_host_another_project_paused_by_alias_or_host_name_until_the_pause_lifts(env, tmp_path,
                                                                                                    monkeypatch):
    """A pause this project's own lookup does not see still keeps the spill picker off the host: another
    project's own pause of the alias or of the machine's host name, or a shared pause file of a name this
    project does not share. The run says so on one line and runs here; once the pause lifts the host is
    allowed again. An unreadable other project is no pause."""
    import threading
    from ttp import checkspill, cli, db as dbm, machines, shared
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b", "down"])
    machines.add("check-host-b", hostname="check-host-b.example")
    other = _second_project(env, "other")
    other.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"check-host-a": {"reason": "lent out", "by": "user", "since": 1}})
    assert checkspill.allowed(p, p.config(), "check-host-a") == "paused by other: lent out"
    other.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"check-host-b.example": {"reason": "repair", "by": "user"}})
    assert checkspill.allowed(p, p.config(), "check-host-b") == "paused by other: repair", "its host name"
    assert checkspill.allowed(p, p.config(), "check-host-a") == "", "the lifted pause frees it"
    shared.update_pause("check-host-a", lambda was: {"reason": "held", "since": time.time(), "project": "third"})
    assert checkspill.allowed(p, p.config(), "check-host-a") == "paused by third: held", "a shared pause file"
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    lane = _spill_lane(p, repo)
    threading.Timer(1.5, lane.close).start()
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and "spilled_to" not in rec, rec
    text = (run / "checks.log").read_text()
    assert "check-host-a (paused by third: held); check-host-b (paused by other: repair)" in text, text
    assert "/ttp-check." not in log.read_text(), "it ran here"
    shared.update_pause("check-host-a", lambda was: None)
    other.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {})
    assert checkspill.allowed(p, p.config(), "check-host-a") == checkspill.allowed(p, p.config(), "check-host-b") == ""
    (Path(other.root) / "tt-project" / "state" / "project.db").write_bytes(b"not a database")
    assert checkspill.paused_elsewhere(p, "check-host-a") is None


def test_spill_hosts_set_while_a_run_waits_take_it_at_its_next_bound(env, tmp_path, monkeypatch):
    """delivery.checks_spill_hosts set after a run began waiting here: the run reads the setting again
    at its next bound and spills, without waiting for the lane here to free. It still runs and records
    only the commit it began on, though the worktree took a new commit while it waited."""
    import subprocess
    import threading
    from ttp import cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, [])
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}; cat a.txt >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    lane = _spill_lane(p, repo)

    def mid_wait():
        (repo / "a.txt").write_text("moved\n")
        subprocess.run([*git, "commit", "-qam", "moved"], check=True)
        p.set_config("delivery.checks_spill_hosts", ["check-host-a"])
    threading.Timer(0.8, mid_wait).start()
    late = threading.Timer(20.0, lane.close)
    late.start()
    try:
        cli.main(["checks", "--fresh"])
    finally:
        late.cancel()
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", rec
    assert rec["head"] == head, rec
    assert "spilling to check-host-a" in (run / "checks.log").read_text()
    ran = log.read_text()
    assert "/ttp-check." in ran and "a\n" in ran and "moved" not in ran, ran


def test_a_run_with_no_spill_hosts_keeps_waiting_here_in_bounds(env, tmp_path, monkeypatch):
    """No spill hosts set for the whole wait: the run waits here across several bounds, never tries a
    spill host, and runs here once the lane frees."""
    import threading
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, [])
    picks = []
    monkeypatch.setattr(checkspill, "pick", lambda *a, **k: picks.append(a) or None)
    reads = []
    real = type(p).config
    monkeypatch.setattr(type(p), "config", lambda self: reads.append(1) or real(self))
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    lane = _spill_lane(p, repo)
    threading.Timer(1.5, lane.close).start()
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and "spilled_to" not in rec, rec
    assert picks == [] and "spilling" not in (run / "checks.log").read_text()
    assert "/ttp-check." not in log.read_text(), "it ran here"
    assert len(reads) >= 4, "the spill settings are read again at each bound"


def test_a_run_that_does_not_pass_on_a_spill_host_runs_again_here(env, tmp_path, monkeypatch):
    """A spill host's own environment never fails a change: a fault there (ssh lost mid-run) or a
    failure there reruns the checks here, without spilling again, and only that result counts; no
    envfault is opened for the spill host."""
    import threading
    from ttp import cli, envfault
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    log = tmp_path / "where.log"
    cmd = f'case "$PWD" in */ttp-check.*) pwd >> {log}; exit 1;; esac; pwd >> {log}'
    p.set_config("delivery.push_checks", [cmd])
    for fake in ({}, {"FAKE_SSH_FAIL": "mktemp"}):
        for k, v in fake.items():
            monkeypatch.setenv(k, v)
        log.write_text("")
        lane = _spill_lane(p, repo)
        threading.Timer(2.0, lane.close).start()
        cli.main(["checks", "--fresh"])
        rec = json.loads((run / "checks.json").read_text())
        assert rec["passed"] is True and "spilled_to" not in rec, (fake, rec)
        text = (run / "checks.log").read_text()
        assert "did not pass on spill host check-host-a" in text, text
        where = log.read_text().splitlines()
        assert where and "/ttp-check." not in where[-1], where
        assert not any(k.startswith("env:check-host:") for k in envfault.open_faults(p))


def test_a_spilled_run_copies_the_commit_it_checks_even_when_the_worktree_moved_while_it_waited(
        env, tmp_path, monkeypatch):
    """A run that spills after its wait here copies the commit it started on (the one its pass names),
    never the worktree's HEAD at spill time: a commit made while it waited is not what it checked."""
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    log = tmp_path / "where.log"
    cmd = f"cat a.txt >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    real_pick = checkspill.pick

    def moved_then_pick(*args, **kw):
        (repo / "a.txt").write_text("moved\n")
        subprocess.run([*git, "commit", "-qam", "moved"], check=True)
        return real_pick(*args, **kw)

    monkeypatch.setattr(checkspill, "pick", moved_then_pick)
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["spilled_to"] == "check-host-a" and rec["head"] == head, rec
    assert log.read_text() == "a\n", "the spill host ran the commit the run checks, not the moved HEAD"


def test_a_remote_check_given_a_commit_it_cannot_resolve_copies_nothing(env, tmp_path, monkeypatch, capsys):
    """--commit naming no commit of the checkout (unknown, malformed, a tree, an option): it fails
    closed with 2 before any ssh, never falling back to the worktree's HEAD."""
    from ttp import remotecheck
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], capture_output=True,
                          text=True).stdout.strip()
    monkeypatch.chdir(repo)
    for bad in ("0" * 40, "no such ref", tree, "--all"):
        assert remotecheck.main(["--host", "check-host-a", "--commit", bad, "--", "true"]) == 2, bad
        said = capsys.readouterr()
        assert "nothing to copy" in said.out + said.err and "usage" not in said.out + said.err, said
    assert _rc_left(tmp_path) == []


def _spill_lock_paths(p, alias):
    from ttp import locks, shared
    cfg = p.config()
    res = locks.canonical(cfg, alias)
    return locks.slot_paths(shared.locks_dir(p, res, cfg), res, shared.slots(p, res, cfg))


def test_spill_skips_a_host_whose_lock_is_held_and_holds_the_lock_it_runs_under(env, tmp_path, monkeypatch):
    """A spill host's machine lock (`ttp lock <alias>`) held exclusively by someone else: the run skips
    it, says who holds it, and runs on the next host. The run holds its host's lock shared while it runs:
    a second spilled run shares it, an exclusive taker is kept off, and it is freed with the claim."""
    from ttp import checkspill, cli, lockprobe, locks
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    held = locks.try_take(_spill_lock_paths(p, "check-host-a"), "task #9 (run 1)", "make bench")
    assert held is not None
    try:
        log = tmp_path / "where.log"
        cmd = f"pwd >> {log}"
        p.set_config("delivery.push_checks", [cmd])
        lane = _spill_lane(p, repo)
        try:
            cli.main(["checks", "--fresh"])
        finally:
            lane.close()
        rec = json.loads((run / "checks.json").read_text())
        assert rec["passed"] is True and rec["spilled_to"] == "check-host-b", rec
        text = (run / "checks.log").read_text()
        assert "check-host-a (busy (held by task #9" in text and "spilling to check-host-b" in text, text
        cfg = p.config()
        claims = [checkspill.pick(p, cfg, lambda line: None, "pytest -q") for _ in range(2)]
        try:
            assert [c.host for c in claims] == ["check-host-b"] * 2, "spilled runs share the host's lock"
            assert lockprobe.answer(p, cfg, "check-host-b")[0] == 75, "an exclusive taker waits for them"
            assert locks.try_take(_spill_lock_paths(p, "check-host-b"), "task #8 (run 2)") is None
        finally:
            for c in claims:
                c.close()
        assert lockprobe.answer(p, cfg, "check-host-b")[0] == 0, "freed with the claim"
    finally:
        held.close()
    assert checkspill.lock_busy(p, p.config(), "check-host-a") == ""


def _device_hold(p, res, where=None, who="task #4 (run 2)"):
    """Hold lock `res` exclusively (as `ttp lock <res> -- <device job>` does), in `where` (default: where
    this project's `ttp lock` takes it)."""
    from ttp import locks, shared
    cfg = p.config()
    d = shared.locks_dir(p, res, cfg) if where is None else where
    got = locks.try_take(locks.slot_paths(d, res, shared.slots(p, res, cfg)), who, "device soak")
    assert got is not None
    return got


def test_spill_keeps_new_runs_off_a_host_whose_device_lock_is_held_and_shows_why(env, tmp_path, monkeypatch):
    """A device job holding a pool host's device lock (`<alias>-device`): a new run skips that host and
    takes the next one (none left: it waits here for the project host's own slots), a run already there
    goes on, and `ttp status` and the web app show the skip in one line with the host and the reason.
    Once the lock is free the host takes runs again and the line goes."""
    from ttp import checkspill, cli, machines, web
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    machines.add("check-host-a", shared=["check-host-a", "check-host-a-device"])
    cfg = p.config()
    told: list[str] = []
    before = checkspill.pick(p, cfg, told.append, "pytest -q")   # a run already on the host
    assert before is not None and before.host == "check-host-a"
    dev = _device_hold(p, "check-host-a-device")
    try:
        got = checkspill.pick(p, cfg, told.append, "pytest -q")
        try:
            assert got is not None and got.host == "check-host-b", told
            assert "check-host-a (a device job runs there: check-host-a-device: busy (held by" in told[-1], told
            assert "device soak" not in told[-1] and "task #4" in told[-1]
            assert [r["host"] for r in checkspill.holders(p.name)].count("check-host-a") == 1, "the run there goes on"
            assert before.f is not None and not before.f.closed
            line = "spill host check-host-a takes no new checks: a device job runs there: check-host-a-device: busy"
            assert [x for x in checkspill.skip_lines() if x.startswith(line)], checkspill.skip_lines()
            assert line in cli.status_text(p), "shown without --verbose"
            assert any(line in r["text"] for r in web.slots_feed(time.time(), p))
            assert not checkspill.skip_lines(time.time() + checkspill.SKIP_SHOW_S + 60), "a stale skip is not shown"
            p.set_config("delivery.checks_spill_hosts", ["check-host-a"])
            assert checkspill.pick(p, p.config(), told.append, "pytest -q") is None, "no other host: waits here"
            assert told[-1].startswith("no spill host could take the checks, waiting here: check-host-a (a device job")
        finally:
            if got is not None:
                got.close()
    finally:
        dev.close()
    again = checkspill.pick(p, p.config(), told.append, "pytest -q")
    try:
        assert again is not None and again.host == "check-host-a", told
        assert checkspill.skip_lines() == []
    finally:
        again.close()
        before.close()


def test_spill_device_gate_sees_every_device_lock_of_the_host_and_only_that_host(env, tmp_path, monkeypatch):
    """The gate reads the host's device locks wherever a device job takes them: this project's own
    `<alias>-device`, one another project holds in the user's shared locks, a reservation of it, and a
    device.locks name on the host (its canonical lock). A lock of another host, even one whose alias
    starts with this one's, never keeps a run off."""
    from ttp import checkspill, locks, shared
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    cfg = p.config()
    assert checkspill.device_locks_of(cfg, "check-host-a") == ["check-host-a-device"]
    assert checkspill.device_busy(p, cfg, "check-host-a") == ""
    for where in (None, shared.root() / "check-host-a-device"):   # own, then another project's shared hold
        h = _device_hold(p, "check-host-a-device", where)
        try:
            assert "check-host-a-device: busy (held by" in checkspill.device_busy(p, cfg, "check-host-a")
        finally:
            h.close()
        assert checkspill.device_busy(p, cfg, "check-host-a") == ""
    d = shared.locks_dir(p, "check-host-a-device", cfg)
    d.mkdir(parents=True, exist_ok=True)
    locks.reserve(locks.reserve_path(d, "check-host-a-device"), "task #5 (run 3)")
    try:
        assert "reserved for task #5" in checkspill.device_busy(p, cfg, "check-host-a")
    finally:
        locks.unreserve(locks.reserve_path(d, "check-host-a-device"), "task #5 (run 3)")
    p.set_config("device.locks", ["check-host-a:board", "check-host-a:chip0", "check-host-ab-device"])
    cfg = p.config()
    assert checkspill.device_locks_of(cfg, "check-host-a") == \
        ["check-host-a-device", "check-host-a:board", "check-host-a:chip0"]
    h = _device_hold(p, "check-host-a:board")
    try:
        assert "check-host-a:board: busy" in checkspill.device_busy(p, cfg, "check-host-a")
    finally:
        h.close()
    for other in ("check-host-ab-device", "check-host-b-device"):
        h = _device_hold(p, other)
        try:
            assert checkspill.device_busy(p, cfg, "check-host-a") == "", other
        finally:
            h.close()


def test_spill_device_gate_probes_the_hosts_shared_device_lock_when_device_locks_lists_another_name_first(
        env, tmp_path, monkeypatch):
    """device.locks lists another name first, so this project's `ttp lock <alias>-device` takes that
    name's lock (locks.canonical). That hold keeps the host off the pool, and so does another project's
    hold of `<alias>-device` in the shared locks. Another project's hold of only the other name in the
    shared locks, a name this project does not share, does not."""
    from ttp import checkspill, locks, shared
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    p.set_config("device.locks", ["other-device", "check-host-a-device"])
    cfg = p.config()
    canon = locks.canonical(cfg, "check-host-a-device")
    assert canon == "other-device"
    assert checkspill.device_locks_of(cfg, "check-host-a") == ["check-host-a-device", "other-device"]
    told: list[str] = []
    for res, where, who in (
            (canon, None, "task #4 (run 2)"),   # this project's `ttp lock check-host-a-device -- <job>`
            ("check-host-a-device", shared.root() / "check-host-a-device", "otherproj task #9 (run 1)")):
        h = _device_hold(p, res, where, who)
        try:
            assert f"{res}: busy (held by" in checkspill.device_busy(p, cfg, "check-host-a"), res
            assert checkspill.pick(p, cfg, told.append, "pytest -q") is None, "the only host is skipped"
            assert f"check-host-a (a device job runs there: {res}: busy" in told[-1], told
        finally:
            h.close()
        assert checkspill.device_busy(p, cfg, "check-host-a") == ""
    assert shared.locks_dir(p, "other-device", cfg) != shared.root() / "other-device"
    h = _device_hold(p, "other-device", shared.root() / "other-device", "otherproj task #9 (run 1)")
    try:
        assert checkspill.device_busy(p, cfg, "check-host-a") == ""
        got = checkspill.pick(p, cfg, told.append, "pytest -q")
        assert got is not None and got.host == "check-host-a", told
        got.close()
    finally:
        h.close()


def test_spill_device_gate_reads_a_broker_job_row_and_an_unreadable_broker_never_blocks(env, tmp_path, monkeypatch):
    """A broker heal check (preset broker) on the pool host: a running job row in its status keeps new
    runs off the host; a status that fails, is no JSON, times out or lists only queued or finished jobs
    never does (the lock alone decides), and none fails the pick."""
    from ttp import checkspill, schedule as sched
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    monkeypatch.setattr(checkspill, "BROKER_TIMEOUT_S", 1.0)
    status = tmp_path / "broker.json"
    sched.upsert(p.db, "host-a-broker", "command", "5m", payload={"heal": {
        "preset": "broker", "status_command": f"cat {status}", "machine": "check-host-a"}})
    cfg = p.config()
    status.write_text(json.dumps({"held": False, "jobs": [{"id": "soak-1", "state": "RUNNING"}]}))
    assert checkspill.device_busy(p, cfg, "check-host-a") == \
        "a device job runs there: its broker reports running job soak-1"
    told: list[str] = []
    assert checkspill.pick(p, cfg, told.append, "pytest -q") is None and "running job soak-1" in told[-1]
    for text in ("not json", json.dumps({"held": False, "jobs": [{"id": "q", "state": "queued"},
                                                                 {"id": "d", "status": "done"}]}),
                 json.dumps({"running": True, "running_jobs": 0, "current_job": None})):
        status.write_text(text)
        assert checkspill.device_busy(p, cfg, "check-host-a") == "", text
    for cmd in (f"cat {tmp_path}/missing", "sleep 5"):
        sched.upsert(p.db, "host-a-broker", "command", "5m", payload={"heal": {
            "preset": "broker", "status_command": cmd, "machine": "check-host-a"}})
        t0 = time.monotonic()
        got = checkspill.pick(p, p.config(), told.append, "pytest -q")
        try:
            assert got is not None and got.host == "check-host-a", (cmd, told)
            assert time.monotonic() - t0 < 4, "a stuck broker read is bounded"
        finally:
            got.close()


def test_spill_broker_job_rows_are_read_from_lists_counts_or_one_job():
    from ttp import checkspill
    rj = checkspill.running_job
    assert rj({"running_jobs": [{"job_id": "j7"}]}) == "running job j7"
    assert rj({"active_jobs": 2}) == "2 running job(s) (active_jobs)"
    assert rj({"current_job": {"name": "soak"}}) == "running job soak"
    assert rj({"jobs": [{"id": "a", "status": "in_progress"}]}) == "running job a"
    for quiet in ({}, [], "x", None, {"jobs": 3}, {"jobs": ["a"]}, {"running": False}, {"active_job": True},
                  {"running_jobs": []}, {"jobs": [{"id": "a", "state": "failed"}]}):
        assert rj(quiet) == "", quiet


def test_a_spill_run_past_the_wall_cap_or_on_a_stalled_link_runs_again_here(env, tmp_path, monkeypatch):
    """A spill host slower than the wall-time cap (or an ssh link that stalls there) is the host's
    environment, not the change's: the checks run again here, only that result counts, and no task
    timeout stays recorded. The check-host ssh ends a dropped link through keepalives."""
    import threading
    from ttp import checkwall, cli, remotecheck
    assert "ServerAliveInterval=15" in remotecheck.SSH and "ServerAliveCountMax=4" in remotecheck.SSH
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    monkeypatch.setenv("TTP_TASK", "4212")
    monkeypatch.setenv(checkwall.ENV, "3")
    log = tmp_path / "where.log"
    cmd = f'case "$PWD" in */ttp-check.*) pwd >> {log}; sleep 30;; esac; pwd >> {log}'
    p.set_config("delivery.push_checks", [cmd])
    lane = _spill_lane(p, repo)
    threading.Timer(1.5, lane.close).start()
    t0 = time.time()
    cli.main(["checks", "--fresh"])
    assert time.time() - t0 < 25, "the spill host's check was ended at the cap"
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and "spilled_to" not in rec and "timed_out" not in rec, rec
    text = (run / "checks.log").read_text()
    assert "did not pass on spill host check-host-a (past the 3 s wall-time cap)" in text, text
    where = log.read_text().splitlines()
    assert "/ttp-check." in where[0] and "/ttp-check." not in where[-1], where
    assert checkwall.last_timeout(p.state, "4212") is None


def test_no_spill_hosts_keeps_todays_wait_and_unspillable_checks_never_spill(env, tmp_path, monkeypatch):
    """Unset (the default) or for checks that must run here (own host, authoring tree, wrap_cmd), the
    wait is the plain foreground wait and no spill host is tried."""
    from ttp import checkcap, checkspill, cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    p.set_config("delivery.push_checks", ["true"])
    limits = []

    def take(*args, **kw):
        limits.append(args[6])
        return checkcap.Slot()
    monkeypatch.setattr(cli, "_checks_slot_take", take)
    monkeypatch.setattr(checkspill, "pick", lambda *a, **k: pytest.fail("no spill host is tried"))
    cli.main(["checks", "--fresh"])
    assert json.loads((run / "checks.json").read_text())["passed"] is True
    assert limits and limits[0] > 30, limits
    assert checkspill.hosts(p.config()) == [] and checkspill.after_s(p.config()) == checkspill.SPILL_AFTER_S
    c = push.check_list(["make test", {"run": "make a", "host": "local"}, {"run": "make b", "cwd": "authoring"}])
    assert checkspill.spillable(c[:1]) and not checkspill.spillable(c[1:2]) and not checkspill.spillable(c[2:])
    assert not checkspill.spillable(push.with_host(c[:1], "check-host-a"))
    assert not checkspill.spillable(push.with_wrap(c[:1], ("flock", "/tmp/x")))
    assert not checkspill.spillable([])


def test_the_account_pool_spills_every_project_after_its_own_list_and_an_empty_own_list_keeps_it_here(
        env, tmp_path, monkeypatch):
    """checks.spill_hosts in the account settings: a project with no list of its own spills to the pool,
    which needs no charter line (the account setting is the user's word for every project) but still the
    machines list; a narrower own list is tried first, then the pool; an own [] keeps every run here."""
    from ttp import checkspill, cli, machines, project
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, [], charter=())
    p.raw_config()
    raw = json.loads(p.config_path.read_text())
    raw["delivery"].pop("checks_spill_hosts")
    p.config_path.write_text(json.dumps(raw))
    assert checkspill.hosts(p.config()) == []
    project.set_account_setting(checkspill.ACCOUNT_KEY, ["pool-a", "pool-b"])
    machines.add("pool-b")
    assert checkspill.hosts(p.config()) == ["pool-a", "pool-b"]
    assert checkspill.allowed(p, p.config(), "pool-a").startswith("not in the machines list")
    assert checkspill.allowed(p, p.config(), "pool-b") == "", "the pool needs no charter line"
    log = tmp_path / "where.log"
    p.set_config("delivery.push_checks", [f"pwd >> {log}"])
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "pool-b", rec
    text = (run / "checks.log").read_text()
    assert "pool-a (not in the machines list" in text and "checks.spill_hosts, the account's pool" in text, text
    # A narrower own list goes first and the pool follows it; outside the pool the charter still decides.
    p.set_config("delivery.checks_spill_hosts", ["check-host-a", "pool-b"])
    assert checkspill.hosts(p.config()) == ["check-host-a", "pool-b", "pool-a"]
    machines.add("check-host-a")
    assert checkspill.allowed(p, p.config(), "check-host-a") == "not named in the charter"
    assert checkspill._source(p.config(), "pool-b") == "delivery.checks_spill_hosts"
    # An own empty list opts out of the pool; a malformed own list spills nowhere.
    p.set_config("delivery.checks_spill_hosts", [])
    assert checkspill.hosts(p.config()) == []
    for bad in ("pool-a", ["a b"]):
        assert checkspill.hosts({"delivery": {"checks_spill_hosts": bad}}) == [], bad
    project.set_account_setting(checkspill.ACCOUNT_KEY, "pool-a")   # malformed pool: none
    assert checkspill.account_hosts() == [] and checkspill.hosts({}) == []


def test_ttp_config_account_sets_the_spill_pool_validated_and_a_run_may_only_clear_it(env, capsys, monkeypatch):
    from ttp import checkspill, cli, project
    cli.main(["config", "--account", "checks.spill_hosts", '["pool-a", "pool-b", "pool-a"]'])
    assert checkspill.account_hosts() == ["pool-a", "pool-b"]
    for bad in ("pool-a", '["a b"]', '["local"]', "3"):
        with pytest.raises(SystemExit):
            cli.main(["config", "--account", "checks.spill_hosts", bad])
        assert "checks.spill_hosts" in capsys.readouterr().err, bad
    assert checkspill.account_hosts() == ["pool-a", "pool-b"]
    monkeypatch.setenv("TTP_RUN_ID", "7")
    for value in ('["pool-c"]', "pool-c"):
        with pytest.raises(SystemExit) as e:
            cli.main(["config", "--account", "checks.spill_hosts", value])
        assert e.value.code == 2 and "only the user may change" in capsys.readouterr().err
    assert checkspill.account_hosts() == ["pool-a", "pool-b"]
    cli.main(["config", "--account", "checks.spill_hosts", "[]"])   # clearing is the safe direction
    assert checkspill.account_hosts() == [] and "spill_hosts" not in (project.load_account_settings().get("checks") or {})


def test_a_wrapped_run_spills_only_when_its_project_says_the_wrapper_needs_no_device(env, tmp_path, monkeypatch):
    """checks.wrap_needs_device false: a run behind checks.wrap_cmd may spill and runs there without the
    wrapper (it is this machine's); unset, true or not a bool, the need is unknown and the run stays here."""
    import threading
    from ttp import checklane, checkspill, cli, project, push, testslots
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    mark, log = tmp_path / "wrapped.log", tmp_path / "where.log"
    wrapper = tmp_path / "wrap.sh"
    wrapper.write_text(f'#!/bin/sh\necho wrapped >> {mark}\nexec "$@"\n')
    wrapper.chmod(0o755)
    p.set_config("checks.wrap_cmd", str(wrapper))
    p.set_config("delivery.push_checks", ["true"])
    cmd = f"pwd >> {log}"

    def lane():
        g = testslots.suite_hold(p.name, wait_s=0, slots=1, repo=testslots.repo_key(str(repo)),
                                 lock=checklane.wrap_key(push.wrap_of(p.config())))
        assert g is not None and g.files
        return g
    p.set_config("checks.wrap_needs_device", False)
    held = lane()
    try:
        cli.main(["checks", "--fresh", "--", cmd])
    finally:
        held.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", rec
    assert "/ttp-check." in log.read_text() and not mark.exists(), "it ran there, without the wrapper"
    # Unset (unknown) or true (needs a device): it waits here, then runs here behind the wrapper.
    raw = json.loads(p.config_path.read_text())
    raw["checks"].pop("wrap_needs_device")
    p.config_path.write_text(json.dumps(raw))
    log.unlink()
    held = lane()
    threading.Timer(1.5, held.close).start()
    cli.main(["checks", "--fresh", "--", cmd])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and "spilled_to" not in rec, rec
    assert "/ttp-check." not in log.read_text() and mark.read_text() == "wrapped\n"
    c = push.with_wrap(push.check_list(["make test"]), (str(wrapper),))
    for v in (None, True, "false", 0):
        assert not checkspill.spillable(c, {"checks": {"wrap_needs_device": v}}), v
    assert checkspill.spillable(c, {"checks": {"wrap_needs_device": False}})
    assert not checkspill.spillable(push.with_host(c, "check-host-a"), {"checks": {"wrap_needs_device": False}})
    assert not project.config_problems({"checks": {"wrap_needs_device": False}})
    assert any("checks.wrap_needs_device" in x for x in project.config_problems({"checks": {"wrap_needs_device": "no"}}))


def test_a_wrapped_project_with_a_narrower_own_list_spills_to_the_rest_of_the_account_pool(
        env, tmp_path, monkeypatch):
    """A project whose own list holds one of the account pool's hosts and whose checks run behind a
    no-device wrapper: the pool's other host follows its list, so a busy run spills there."""
    from ttp import checkspill, cli, machines, project
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["down"], charter=("down",))
    project.set_account_setting(checkspill.ACCOUNT_KEY, ["down", "pool-b"])
    machines.add("pool-b")
    assert checkspill.hosts(p.config()) == ["down", "pool-b"]
    wrapper = tmp_path / "wrap.sh"
    wrapper.write_text('#!/bin/sh\nexec "$@"\n')
    wrapper.chmod(0o755)
    p.set_config("checks.wrap_cmd", str(wrapper))
    p.set_config("checks.wrap_needs_device", False)
    p.set_config("delivery.push_checks", ["true"])
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh", "--", "true"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "pool-b", rec


def test_host_wide_settings_show_the_account_value_in_every_project_and_a_project_copy_is_refused(env):
    """checks.host_max (and the other HOST_WIDE_KEYS) is read only from the account settings: a project's
    own copy in project.json never shows, and setting one in a project is refused with the account command."""
    from ttp import project
    p = make(env)
    project.set_account_setting("checks.host_max", 12)
    raw = json.loads(p.config_path.read_text())
    raw.setdefault("checks", {})["host_max"] = 4
    raw["checks"]["spill_hosts"] = ["pool-z"]
    p.config_path.write_text(json.dumps(raw))
    assert p.config()["checks"]["host_max"] == 12
    assert "spill_hosts" not in p.config()["checks"], "no account pool: a project's own copy shows nothing"
    with pytest.raises(ValueError, match="ttp config --account checks.host_max"):
        p.set_config("checks.host_max", 4)
    project.set_account_setting("checks.host_max", None)
    assert "host_max" not in p.config()["checks"]


def test_spill_settings_are_validated_and_a_scalar_is_refused():
    from ttp import coordinator, project
    ok = {"delivery": {"checks_spill_hosts": ["check-host-a", "check-host-b"], "checks_spill_after_s": 30}}
    assert not project.config_problems(ok)
    for bad in ("check-host-a", 3, True, {"check-host-a": 1}, ["a b"], ["local"], [3]):
        assert any("delivery.checks_spill_hosts" in x
                   for x in project.config_problems({"delivery": {"checks_spill_hosts": bad}})), bad
    for bad in (-1, "soon", True):
        assert any("delivery.checks_spill_after_s" in x
                   for x in project.config_problems({"delivery": {"checks_spill_after_s": bad}})), bad
    settable = coordinator.USER_SETTABLE["delivery.checks_spill_hosts"]
    assert settable(["check-host-a"]) == ["check-host-a"] and settable('["check-host-a"]') == ["check-host-a"]
    assert settable([]) == []
    for bad in ("check-host-a", 3, {"a": 1}, ["a;b"]):
        with pytest.raises(ValueError):
            settable(bad)
    assert coordinator.USER_SETTABLE["delivery.checks_spill_after_s"]("45") == 45.0
    with pytest.raises(ValueError):
        coordinator.USER_SETTABLE["delivery.checks_spill_after_s"]("-1")


def test_missing_program_on_a_check_host_runs_command_v_there_with_the_checks_env(env, tmp_path, monkeypatch, capsys):
    # A command for a check host is resolved there (its PATH and the check's env), never here; ssh
    # failing is no answer (precheck probes the host). `ttp checks` refuses it naming the host.
    from ttp import cli, envfault
    bin_, remote = _rc_fake_ssh(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    there = tmp_path / "remote-tools"
    there.mkdir()
    (there / "remote-only-tool").write_text("#!/bin/sh\nexit 0\n")
    (there / "remote-only-tool").chmod(0o755)
    cmd = "timeout 60 remote-only-tool check ."
    assert envfault.missing_program(cmd, tmp_path, {}, "box1") == "remote-only-tool"
    assert envfault.missing_program(cmd, tmp_path, {}, "box1", {"PATH": f"{there}:$PATH"}) == ""
    assert envfault.missing_program(cmd, tmp_path, {}, "down") == ""
    monkeypatch.setenv("FAKE_SSH_FAIL", "command -v")
    assert envfault.missing_program(cmd, tmp_path, {}, "box1") == ""
    monkeypatch.delenv("FAKE_SSH_FAIL")
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    p.set_config("delivery.checks_host", "box1")
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--fresh", "--", cmd])
    err = capsys.readouterr().err
    assert e.value.code == 1 and "'remote-only-tool' is not installed on the check host box1" in err, err
    assert not (run / "checks.json").exists()
