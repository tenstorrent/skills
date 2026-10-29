# Remote check host tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file. A fake
# `ssh` on PATH drops its -o options and the host and runs the last argument with `sh -c` here, so
# no network is needed.
from __future__ import annotations

_RC_FAKE_SSH = r'''#!/bin/sh
# A fake ssh: drops -o options and the host, runs the last argument with sh -c. Host "down" is
# unreachable; FAKE_SSH_FAIL=<text> exits 255 at once on a script holding it (reading nothing);
# FAKE_SSH_DROP=<text> runs such a script, then drops the connection before its exit-code marker.
while [ "$1" = "-o" ]; do shift 2; done
host=$1
shift
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


def _rc_run(tmp_path, repo, cmd, host="box1", env=None, wait=True, **fake):
    """remotecheck run as callers run it (push.check_argv's argv) in `repo` against the fake ssh:
    (exit code, output); with wait=False the Popen."""
    from ttp import remotecheck
    bin_, remote = _rc_fake_ssh(tmp_path)
    penv = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_REMOTE_TMP": str(remote), **fake}
    proc = subprocess.Popen(remotecheck.argv(cmd, host, env), cwd=repo, env=penv, stdout=subprocess.PIPE,
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

    def take(*args):
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
