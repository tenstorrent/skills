# Remote check host tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file. A fake
# `ssh` on PATH drops its -o options and the host and runs the last argument with `sh -c` here, so
# no network is needed.
from __future__ import annotations

_RC_FAKE_SSH = r'''#!/bin/sh
# A fake ssh: drops -o options and the host, runs the last argument with sh -c. Host "down" is
# unreachable; FAKE_SSH_FAIL=<text> exits 255 at once on a script holding it (reading nothing);
# FAKE_SSH_DROP=<text> runs such a script, then drops the connection before its exit-code marker.
# FAKE_SSH_LOG=<file> gets one line per call naming the host; FAKE_SSH_ARGS=<file> one with all its arguments. FAKE_SSH_MUX=<file> holding a count (or
# "always"): on a script holding FAKE_SSH_MUX_ON (default: the run step) the shared connection refuses
# the session while the count lasts, and the link drops (exit 255); <file>.log gets "shared" or "direct".
# The host's tt-project folder (its mirrors) is FAKE_REMOTE_TMP/../host-home. FAKE_SSH_NOGIT=1: git there
# is unusable. FAKE_SSH_SLOW=<text>:<s> sleeps <s> before a script holding <text> (a slow link), only on host
# FAKE_SSH_SLOW_HOST when that is set.
all="$*"
while [ "$1" = "-o" ]; do shift 2; done
host=$1
shift
[ -n "$FAKE_SSH_LOG" ] && echo "$host" >> "$FAKE_SSH_LOG"
[ -n "$FAKE_SSH_ARGS" ] && echo "$all" >> "$FAKE_SSH_ARGS"
if [ "$host" = down ]; then echo "ssh: connect to host down port 22: Connection refused" >&2; exit 255; fi
[ -n "$FAKE_REMOTE_TMP" ] && export TMPDIR="$FAKE_REMOTE_TMP" TTP_HOME="$FAKE_REMOTE_TMP/../host-home"
if [ -n "$FAKE_SSH_NOGIT" ]; then
  mkdir -p "$FAKE_REMOTE_TMP/../nogit" && printf '#!/bin/sh\nexit 127\n' > "$FAKE_REMOTE_TMP/../nogit/git"
  chmod +x "$FAKE_REMOTE_TMP/../nogit/git" && export PATH="$FAKE_REMOTE_TMP/../nogit:$PATH"
fi
if [ -n "$FAKE_SSH_SLOW" ]; then
  [ -z "$FAKE_SSH_SLOW_HOST" ] || [ "$host" = "$FAKE_SSH_SLOW_HOST" ] && case "$1" in *"${FAKE_SSH_SLOW%:*}"*) sleep "${FAKE_SSH_SLOW##*:}";; esac
fi
if [ -n "$FAKE_SSH_MUX" ]; then
  case "$1" in *"${FAKE_SSH_MUX_ON:-ttp-remote-rc}"*)
    case "$all" in *ControlPath=none*) echo direct >> "$FAKE_SSH_MUX.log";; *) echo shared >> "$FAKE_SSH_MUX.log";; esac
    left=$(cat "$FAKE_SSH_MUX")
    if [ "$left" = always ] || [ "$left" -gt 0 ]; then
      [ "$left" = always ] || echo $((left - 1)) > "$FAKE_SSH_MUX"
      echo "mux_client_request_session: session request failed: Session open refused by peer" >&2
      echo "client_loop: send disconnect: Broken pipe" >&2
      exit 255
    fi;;
  esac
fi
# FAKE_SSH_SLOW=<host>: the tree copy to that host never ends (a stalled link).
[ "$host" = "${FAKE_SSH_SLOW:-@none@}" ] && case "$1" in *"tar -xf -"*) exec sleep 5;; esac
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


def _rc_run(tmp_path, repo, cmd, host="box1", env=None, wait=True, commit="", literal=None, **fake):
    """remotecheck run as callers run it (push.check_argv's argv) in `repo` against the fake ssh:
    (exit code, output); with wait=False the Popen."""
    from ttp import remotecheck
    bin_, remote = _rc_fake_ssh(tmp_path)
    penv = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_REMOTE_TMP": str(remote), **fake}
    proc = subprocess.Popen(remotecheck.argv(cmd, host, env, commit, literal=literal), cwd=repo, env=penv,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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



def test_a_remote_check_s_python_output_is_unbuffered_unless_its_env_says_otherwise(tmp_path):
    """Over ssh stdout is a pipe: Python there runs unbuffered, so pytest's progress streams back and a
    passing run longer than the silence limit (checkwall.SILENT_S) is not stopped as a hang (live: about 30
    spilled smoke runs stopped at 610-631 s); a check's own env still sets it otherwise."""
    repo = _rc_repo(tmp_path)
    rc, said = _rc_run(tmp_path, repo, 'echo "unbuf=$PYTHONUNBUFFERED"')
    assert rc == 0 and "unbuf=1" in said, said
    rc, said = _rc_run(tmp_path, repo, 'echo "unbuf=$PYTHONUNBUFFERED"', env={"PYTHONUNBUFFERED": ""})
    assert rc == 0 and "unbuf=" in said and "unbuf=1" not in said, said


def test_a_remote_check_runs_at_nice_19_and_idle_io_on_the_host(tmp_path):
    """The command on the host runs behind its `nice -n 19` and `ionice -c3` (testslots.priority_args), not
    only the local ssh client: its niceness there is 19, its IO class idle where the host has ionice."""
    repo = _rc_repo(tmp_path)
    rc, said = _rc_run(tmp_path, repo, "echo nice=$(ps -o ni= -p $$ | tr -d ' '); command -v ionice >/dev/null && ionice -p $$ || true")
    assert rc == 0 and (f"nice={max(19, os.getpriority(os.PRIO_PROCESS, 0))}" in said), said   # already above 19: stays
    if shutil.which("ionice"):
        assert "idle" in said, said


def test_a_remote_check_s_priority_prefix_skips_a_tool_the_host_lacks_and_none_when_off(tmp_path):
    """Each of nice and ionice goes in front only where the host has it (macOS: no ionice), nice outermost;
    checks.nice false (no priority words) adds nothing."""
    from ttp import remotecheck
    prio = (["nice", "-n", "19"], ["ionice", "-c3"])
    assert remotecheck._priority(None) == "" and "nice" not in remotecheck._run_script("/t", "", "x", "M:")
    only = tmp_path / "only"
    only.mkdir()
    for tool in ("nice", "ionice"):
        (only / tool).write_text("#!/bin/sh\n")
        (only / tool).chmod(0o755)
        script = "set -- bash -c x\n" + remotecheck._priority(prio) + 'echo "$@"\n'
        out = subprocess.run(["/bin/sh", "-c", script], env={"PATH": str(only)}, capture_output=True, text=True)
        assert out.stdout.split() == {"nice": ["nice", "-n", "19", "bash", "-c", "x"],
                                      "ionice": ["nice", "-n", "19", "ionice", "-c3", "bash", "-c", "x"]}[tool], out
    assert remotecheck._priority(prio) in remotecheck._run_script("/t", "", "x", "M:", ("srun",), prio)


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
    for fake in ({"FAKE_SSH_FAIL": "mktemp"}, {"FAKE_SSH_FAIL": "ttp-send"}, {"FAKE_SSH_FAIL": "--shared"},
                 {"FAKE_SSH_FAIL": "tar -xf", "FAKE_SSH_NOGIT": "1", "TTP_REMOTE_TREE_COPY": "1"}, {"FAKE_SSH_DROP": "ttp-remote-rc"}):
        rc, said = _rc_run(tmp_path, repo, "true", **fake)
        assert rc == remotecheck.FAULT_RC, (fake, said)
        assert f"{remotecheck.SIG}box1" in said and "git archive" not in said, said
    assert _rc_run(tmp_path, repo, "true", host="down")[0] == remotecheck.FAULT_RC
    assert _rc_left(tmp_path) == []
    bin_, _ = _rc_fake_ssh(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    assert remotecheck.reachable("box1") == ""
    assert "cannot reach down" in remotecheck.reachable("down")


def test_a_remote_check_tries_again_on_its_own_connection_when_the_shared_one_refuses_a_session(tmp_path):
    """ssh's shared connection refusing a session (MaxSessions) at the run step, the copy or the temp dir
    is not the host's fault: the check runs again from the start there on a connection of its own and
    its result counts; nothing is left on the host."""
    from ttp import remotecheck
    repo = _rc_repo(tmp_path)
    mux = tmp_path / "mux"
    for i, on in enumerate(("ttp-remote-rc", "ttp-send", "mktemp")):
        _rc_commit(repo, f"n{i}.txt")   # a commit the mirror lacks: each check sends a bundle
        mux.write_text("1")
        Path(f"{mux}.log").unlink(missing_ok=True)
        rc, said = _rc_run(tmp_path, repo, "echo ran-here; exit 3", FAKE_SSH_MUX=str(mux), FAKE_SSH_MUX_ON=on)
        assert rc == 3 and "ran-here" in said, (on, said)
        assert remotecheck.MUX_REFUSED in said and "trying again in 2 s on a connection of its own" in said, said
        assert remotecheck.SIG not in said, said
        assert Path(f"{mux}.log").read_text().split() == ["shared", "direct"], on
        assert _rc_left(tmp_path) == []


def test_a_remote_check_whose_shared_ssh_connection_refuses_every_try_is_a_fault_naming_the_refusal(
        tmp_path, monkeypatch):
    """Bounded: refused on every try, the check is today's environment fault after 1 + len(MUX_BACKOFF_S)
    tries, and its message names the refusal; an ssh drop with no refusal is a fault at once."""
    from ttp import remotecheck
    repo = _rc_repo(tmp_path)
    mux = tmp_path / "mux"
    mux.write_text("always")
    rc, said = _rc_run(tmp_path, repo, "echo ran-here", FAKE_SSH_MUX=str(mux))
    assert rc == remotecheck.FAULT_RC and "ran-here" not in said, said
    assert f"{remotecheck.SIG}box1" in said and f"refused a session ({remotecheck.MUX_REFUSED}) on each of 3 tries" in said
    assert Path(f"{mux}.log").read_text().split() == ["shared", "direct", "direct"]
    assert _rc_left(tmp_path) == []
    rc, said = _rc_run(tmp_path, repo, "true", FAKE_SSH_DROP="ttp-remote-rc")
    assert rc == remotecheck.FAULT_RC and "trying again" not in said, said
    # The keepalive outpaces a proxy that cuts a link idle for 15 s; drops still end in about a minute.
    opts = dict(w.split("=", 1) for w in remotecheck.SSH if "=" in w)
    alive = int(opts["ServerAliveInterval"])
    assert alive < 15 and 30 <= alive * int(opts["ServerAliveCountMax"]) <= 90, opts
    direct = remotecheck._ssh("h", "true", direct=True)
    assert "ControlPath=none" in direct and "ControlMaster=no" in direct and direct[-2] == "h"
    assert "ControlPath=none" not in remotecheck._ssh("h", "true")


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


def test_a_remote_check_s_orphan_in_its_run_dir_ends_with_the_run(tmp_path):
    """A process a remote check left in a session of its own, with an environment of its own, ends with
    the run when its working directory is the run's temp dir on the host (remotecheck._END)."""
    import shutil
    if not os.path.exists("/proc/self/status") or not shutil.which("setsid"):
        pytest.skip("needs /proc and setsid")
    repo = _rc_repo(tmp_path)
    pidf = tmp_path / "orphan.pid"
    rc, out = _rc_run(tmp_path, repo, f"setsid sh -c 'echo $$ > {pidf}.tmp; mv {pidf}.tmp {pidf}; exec env -i sleep 60' & "
                                      f"while [ ! -e {pidf} ]; do sleep 0.05; done")
    assert rc == 0, out
    orphan = int(pidf.read_text())
    try:
        assert _rc_gone(orphan), "the orphan in the run's dir outlived the run"
        assert _rc_left(tmp_path) == []
    finally:
        try:
            os.kill(orphan, signal.SIGKILL)
        except OSError:
            pass


def test_a_stopped_remote_check_stops_its_command_on_the_host(tmp_path, tmp_processes_in):
    """SIGTERM to the local side (a timeout, a `ttp checks` stop, a queue stop) stops the command where
    it runs (ssh without a tty signals nothing) and removes its temp dir; the host's stop watcher (its
    TERM-then-KILL grace sleep) ends on its own within that grace, leaving nothing running there."""
    from ttp import remotecheck
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
    end = time.time() + remotecheck.STOP_GRACE_S + 10
    while (left := tmp_processes_in(tmp_path)) and time.time() < end:
        time.sleep(0.1)
    assert not left, left


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


# The fake ssh runs a spill host's probe and checks on this machine. On macOS its device holder scan cannot
# run (no GNU find), and a check script under its temp folder (/private/var/...) is a path of this machine,
# which never spills unless the test counts its temp folder as a system folder (_spill_setup does).
_GNU_FIND = pytest.mark.skipif(sys.platform != "linux", reason="the fake spill host's device scan needs GNU find")
_LINUX_TMP = pytest.mark.skipif(sys.platform != "linux", reason="a check script under the macOS temp folder never spills")

def _idle_probe(cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 8)"):
    """checkspill's slots probe for the fake ssh, run on this machine: an idle host of `cores` cores (no
    load, memory unread) with its running ttp-check.* folders counted, using no /proc or GNU tools, so it
    reads the same on macOS (whose wc pads its count) as on Linux."""
    from ttp import checkspill
    return ("command -v git >/dev/null 2>&1 && command -v tar >/dev/null 2>&1 || exit 3\n"
            f"c={cores}\n"
            "n=$(find \"${TMPDIR:-/tmp}\" -maxdepth 1 -name 'ttp-check.*' -type d "
            f"-mmin -{checkspill.RUNNING_MAX_AGE_MIN} 2>/dev/null | wc -l | tr -d ' ')\n"
            "echo \"ttp-spill $c 0 $n 0\"\n")


def _spill_setup(env, tmp_path, monkeypatch, hosts, charter=("check-host-a", "check-host-b", "down")):
    """A checks repo whose machines list has `charter` (also written as charter text, which no gate reads), spills to `hosts`
    after 0.3 s, has one checks lane, and the fake ssh on PATH reporting an idle host."""
    from ttp import checkspill, machines
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    # The fake ssh runs the probe here: read an idle load, not this machine's (a busy test host has none).
    monkeypatch.setattr(checkspill, "_PROBE", _idle_probe())
    # The fake spill hosts are this machine, so the test's temp folder is there too: a check naming it may spill
    # (checkspill.host_path) wherever the session keeps its temp files. Before, it spilled only under /dev/shm (a
    # system folder), and on a loaded host or under `ttp checks` (temp under /tmp) waited for the lane instead.
    monkeypatch.setattr(checkspill, "_SYSTEM_DIRS", (*checkspill._SYSTEM_DIRS,
                                                     *{f"{d}/" for d in (str(tmp_path), os.path.realpath(tmp_path))}))
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


def _close_when_back(lane, run, said="did not pass on spill host"):
    """Free the held lane once checks.log says the spilled run came back here, not after a fixed time: a slow
    spill under load would otherwise find the lane free first and run here without spilling. A bounded wait
    in a daemon thread keeps a run that never says it from holding the lane past the test."""
    import threading
    log = run / "checks.log"
    start = log.stat().st_size if log.exists() else 0

    def watch():
        end = time.time() + _load_scaled(120)
        while time.time() < end:
            try:
                with open(log, "rb") as f:
                    f.seek(start)
                    if said.encode() in f.read():
                        break
            except OSError:
                pass
            time.sleep(0.05)
        lane.close()
    threading.Thread(target=watch, daemon=True).start()


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
    """No listed host can take the run (not in the machines list, paused, unreachable, every slot busy):
    the run says why for each, keeps its place and runs here once the lane frees."""
    import threading
    from ttp import checklane, checkspill, cli, db as dbm, machines
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch,
                                        ["unlisted", "check-host-c", "down", "check-host-a"],
                                        charter=("check-host-a", "check-host-c", "down", "unlisted"))
    machines.remove("unlisted")
    p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"check-host-c": {"reason": "user hold", "by": "user", "since": 1}})
    # Remote checks from other machines fill check-host-a: as many as its cores hold (checklane.host_slots).
    for i in range(max(checklane.MAX_LANES, os.cpu_count() or 1)):
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
    for why in ("unlisted (not in the machines list",
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


@_GNU_FIND
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
    from ttp import checkspill, cli, envfault
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    log = tmp_path / "where.log"
    cmd = f'case "$PWD" in */ttp-check.*) pwd >> {log}; exit 1;; esac; pwd >> {log}'
    p.set_config("delivery.push_checks", [cmd])
    real_off = checkspill.spill_off_add
    lane = None

    def spill_off_add(*a, **k):   # the spill run ended without passing: free the lane for the rerun here
        off = real_off(*a, **k)
        lane.close()
        return off
    monkeypatch.setattr(checkspill, "spill_off_add", spill_off_add)
    for fake in ({}, {"FAKE_SSH_FAIL": "mktemp"}):
        for k, v in fake.items():
            monkeypatch.setenv(k, v)
        (p.state / checkspill.FAILS_FILE).unlink(missing_ok=True)   # each scenario is its own run of the head
        log.write_text("")
        lane = _spill_lane(p, repo)
        safety = threading.Timer(120, lane.close)   # never hang the suite
        safety.start()
        try:
            cli.main(["checks", "--fresh"])
        finally:
            safety.cancel()
            lane.close()
        rec = json.loads((run / "checks.json").read_text())
        assert rec["passed"] is True and "spilled_to" not in rec, (fake, rec)
        text = (run / "checks.log").read_text()
        assert "did not pass on spill host check-host-a" in text, text
        where = log.read_text().splitlines()
        assert where and "/ttp-check." not in where[-1], where
        assert not any(k.startswith("env:check-host:") for k in envfault.open_faults(p))


def test_a_spilled_run_stays_on_its_spill_host_when_the_shared_ssh_connection_refuses_a_session(
        env, tmp_path, monkeypatch):
    """A spill host whose shared ssh connection refuses one session keeps the run: it passes there and
    counts. Refused on every try, the run comes back here as for any host fault, and the log names it."""
    import threading
    from ttp import cli, envfault, remotecheck
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    log = tmp_path / "where.log"
    p.set_config("delivery.push_checks", [f"pwd >> {log}"])
    mux = tmp_path / "mux"
    monkeypatch.setenv("FAKE_SSH_MUX", str(mux))
    for refusals in ("1", "always"):
        mux.write_text(refusals)
        log.write_text("")
        lane = _spill_lane(p, repo)
        if refusals == "always":   # frees the lane here for the run that comes back
            threading.Timer(2.0, lane.close).start()
        cli.main(["checks", "--fresh"])
        lane.close()
        rec = json.loads((run / "checks.json").read_text())
        text = (run / "checks.log").read_text()
        assert rec["passed"] is True and remotecheck.MUX_REFUSED in text, (refusals, rec)
        where = log.read_text().splitlines()
        if refusals == "1":
            assert rec["spilled_to"] == "check-host-a" and len(where) == 1 and "/ttp-check." in where[0], rec
            assert "did not pass on spill host" not in text, text
        else:
            assert "spilled_to" not in rec and "/ttp-check." not in where[-1], (rec, where)
            assert "did not pass on spill host check-host-a" in text and "on each of 3 tries" in text, text
        assert not any(k.startswith("env:check-host:") for k in envfault.open_faults(p))
        assert _rc_left(tmp_path) == []


def test_a_run_with_the_host_wide_cap_full_spills_to_a_free_host_past_a_paused_one(env, tmp_path, monkeypatch):
    """#6379: the project's lanes are free but the host-wide checks cap is full: past checks_spill_after_s
    the run skips a paused spill host and runs on the next free one, never waiting for the cap here."""
    from ttp import checkcap, cli, db as dbm
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-c", "check-host-a"],
                                        charter=("check-host-a", "check-host-c"))
    p.set_config("checks.slots", 4)
    p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"check-host-c": {"reason": "user hold", "by": "user", "since": 1}})
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkcap, "extra_max", lambda: 0)
    p.set_config("delivery.push_checks", ["true"])
    held = checkcap.acquire("other project", priority=3)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        held.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", rec
    text = (run / "checks.log").read_text()
    assert "check-host-c (paused: user hold)" in text and "spilling to check-host-a" in text, text


def test_a_run_that_fails_tests_on_a_spill_host_reruns_only_those_tests_here_and_their_failing_again_counts(
        env, tmp_path, monkeypatch):
    """A suite that failed some tests on a spill host (exit 1, every failure listed) does not run whole
    again here: the rerun selects only the failed ids (TTP_PYTEST_SELECT_IDS), their pass here passes the
    run, and their failing here too fails it. One that stopped early (-x) runs whole again."""
    import threading
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    log, mark = tmp_path / "where.log", tmp_path / "fail-here"
    fail = ("printf 'FAILED tests/test_x.py::test_a - boom\\nFAILED tests/test_y.py::test_b - boom\\n"
            "%s\\n' \"$TAIL\"; exit 1")
    cmd = (f'echo "$PWD ids=$(printf %s \"$TTP_PYTEST_SELECT_IDS\" | tr \'\\n\' ,)" >> {log}; '
           f'case "$PWD" in */ttp-check.*) {fail};; esac; test -e {mark} && {{ {fail}; }}; exit 0')
    p.set_config("delivery.push_checks", [cmd])
    for tail, fails_here, passed, ids in (
            ("2 failed, 9 passed in 0.10s", False, True, "tests/test_x.py::test_a,tests/test_y.py::test_b"),
            ("2 failed, 9 passed in 0.10s", True, False, "tests/test_x.py::test_a,tests/test_y.py::test_b"),
            ("!!!!!!!!!! stopping after 2 failures !!!!!!!!!!", False, True, "")):
        monkeypatch.setenv("TAIL", tail)
        mark.touch() if fails_here else mark.unlink(missing_ok=True)
        (p.state / checkspill.FAILS_FILE).unlink(missing_ok=True)   # each scenario is its own run of the head
        log.write_text("")
        seen = len((run / "checks.log").read_text()) if (run / "checks.log").exists() else 0
        lane = _spill_lane(p, repo)
        threading.Timer(2.0, lane.close).start()
        try:
            cli.main(["checks", "--fresh"])
        except SystemExit as e:
            assert not passed and e.code == 1, e.code
        rec = json.loads((run / "checks.json").read_text())
        assert rec["passed"] is passed and "spilled_to" not in rec, (tail, fails_here, rec)
        text = (run / "checks.log").read_text()[seen:]   # this run's part of the log
        assert "did not pass on spill host check-host-a (exit 1)" in text, text
        assert ("running only the 2 test(s)" in text) is bool(ids), text
        where = log.read_text().splitlines()
        assert "/ttp-check." in where[0] and "/ttp-check." not in where[-1], where
        assert where[-1].endswith(f"ids={ids}"), where


@_LINUX_TMP
def test_checks_that_fail_on_a_spill_host_run_again_only_here_and_a_host_fault_still_moves_on(
        env, tmp_path, monkeypatch):
    """#8400: checks that fail tests on a spill host (exit 1) run again once, on this machine only, whose result
    counts: never on to the next spill host (before, a change failing everywhere went from host to host). A host's
    own fault (exit 75) still moves them to another free spill host, where a pass counts, never back to that one."""
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    p.set_config("delivery.checks_remote_only", False)   # a workstation takes the hosts' verdict instead
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    mark, log = tmp_path / "refused-once", tmp_path / "where.log"
    (repo / "refuse-once.sh").write_text(f'#!/bin/sh\ntest -e {mark} && exec "$@"\ntouch {mark}\n'
                                         ': > "$TTP_WRAP_REFUSED"\nexit 75\n')   # the host busy once (a refusal)
    (repo / "refuse-once.sh").chmod(0o755)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "wrap"], check=True)
    p.set_config("delivery.push_checks", [{"run": f"pwd >> {log}", "host": "local", "spill": True,
                                           "spill_wrap_cmd": "./refuse-once.sh"}])
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-b", rec
    text = (run / "checks.log").read_text()
    assert "did not pass on spill host check-host-a (exit 75)" in text and "spilling to check-host-b" in text, text
    assert "on this machine only" not in text and mark.exists(), text
    assert all("/ttp-check." in w for w in log.read_text().splitlines()), "both runs were on spill hosts"
    # Failing on a spill host: here next, never host b, whatever is free; failing here too fails the run.
    for fails_here in (False, True):
        log.write_text("")
        (p.state / checkspill.FAILS_FILE).unlink(missing_ok=True)   # each scenario is its own run of the head
        seen = len((run / "checks.log").read_text())
        p.set_config("delivery.push_checks", [f'pwd >> {log}; case "$PWD" in */ttp-check.*) exit 1;; esac; '
                                              + ("exit 1" if fails_here else "exit 0")])
        lane = _spill_lane(p, repo)
        _close_when_back(lane, run, "on this machine only")
        try:
            cli.main(["checks", "--fresh"])
        except SystemExit as e:
            assert fails_here and e.code == 1, e.code
        finally:
            lane.close()
        rec = json.loads((run / "checks.json").read_text())
        assert rec["passed"] is (not fails_here) and "spilled_to" not in rec, (fails_here, rec)
        text = (run / "checks.log").read_text()[seen:]
        assert "did not pass on spill host check-host-a (exit 1)" in text and "on this machine only" in text, text
        assert "spilling to check-host-b" not in text, text
        where = log.read_text().splitlines()
        assert len(where) == 2 and "/ttp-check." in where[0] and "/ttp-check." not in where[1], where
    assert checkspill.holders(p.name) == []


@_LINUX_TMP
def test_the_same_check_hanging_on_two_spill_hosts_fails_the_run_and_a_host_fault_then_one_hang_moves_on(
        env, tmp_path, monkeypatch):
    """#8416: a check command stopped by its silence limit on one spill host and again on a second is a hang of
    the change's: the run fails after the second, never on to a third host or here, and says it hung. A host's
    own fault (exit 75) and one hang still move on once, as any single timeout there does."""
    import threading
    from ttp import checkspill, checkwall, cli
    hosts = ["check-host-a", "check-host-b", "check-host-c"]
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, hosts, charter=hosts)
    p.set_config("delivery.checks_remote_only", False)
    monkeypatch.setattr(checkwall, "POLL_S", 0.1)
    monkeypatch.setenv("TTP_TASK", "8416")
    log = tmp_path / "where.log"
    p.set_config("delivery.push_checks", [{"run": f'pwd >> {log}; case "$PWD" in */ttp-check.*) sleep 30;; esac',
                                           "silent_s": 2}])
    lane = _spill_lane(p, repo)
    threading.Timer(_load_scaled(90), lane.close).start()   # bounded: a run that never fails here frees it
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    assert e.value.code == 1
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False, rec
    text = (run / "checks.log").read_text()
    verdict = [ln for ln in text.splitlines() if "(a hang) on spill hosts" in ln]
    assert len(verdict) == 1 and "check-host-c" not in verdict[0] and "not run again" in verdict[0], text
    assert "spilling to check-host-c" not in text and "running them again" in text, text
    where = log.read_text().splitlines()
    assert len(where) == 2 and all("/ttp-check." in w for w in where), "two spill hosts, then no rerun anywhere"
    assert checkwall.last_timeout(p.state, "8416"), "the hang is the task's timeout"
    # A host's fault, then one hang: on once more, where it passes.
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    mark, count = tmp_path / "refused-once", tmp_path / "count"
    (repo / "refuse-once.sh").write_text(f'#!/bin/sh\ntest -e {mark} && exec "$@"\ntouch {mark}\n'
                                         ': > "$TTP_WRAP_REFUSED"\nexit 75\n')
    (repo / "refuse-once.sh").chmod(0o755)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "wrap"], check=True)
    log.write_text("")
    seen = len((run / "checks.log").read_text())
    p.set_config("delivery.push_checks", [{"run": f"pwd >> {log}; echo x >> {count}; "
                                                  f'test "$(wc -l < {count})" -gt 1 || sleep 30',
                                           "host": "local", "spill": True, "spill_wrap_cmd": "./refuse-once.sh",
                                           "silent_s": 2}])
    lane = _spill_lane(p, repo)
    threading.Timer(_load_scaled(90), lane.close).start()
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    text = (run / "checks.log").read_text()[seen:]
    assert rec["passed"] is True, (rec, text)
    assert "(exit 75)" in text and "(no output for 2 s: a hang, stopped)" in text, text
    assert "(a hang) on spill hosts" not in text, text
    assert len(log.read_text().splitlines()) == 2 and mark.exists()
    assert checkspill.holders(p.name) == []


def test_a_hang_mark_is_per_command_keeps_its_host_off_and_counts_for_the_workstation_verdict():
    """#8416: spill_off_add's hang mark names the command, so only the same command hanging on two hosts is a
    verdict (hang_hosts); a hung host is kept off like a failed one, and fail_hosts counts it as before."""
    from ttp import checkspill
    off = checkspill.spill_off_add("", "host-a", False, hang="make test")
    off = checkspill.spill_off_add(off, "host-b", False, hang="make other")
    assert checkspill.hang_hosts(off, "make test") == ["host-a"] and checkspill.kept_off(off) == {"host-a", "host-b"}
    off = checkspill.spill_off_add(off, "host-c", fault=True)
    off = checkspill.spill_off_add(off, "host-b", False, hang="make test")
    assert checkspill.hang_hosts(off, "make test") == ["host-a", "host-b"]
    assert checkspill.fail_hosts(off) == ["host-a", "host-b"]


def test_spill_off_keeps_a_failed_host_off_and_a_faulted_one_after_its_second_fault():
    """#6379: OFF_ENV's value names the hosts a rerun keeps off: a host the checks failed on at once, one
    whose own fault (exit 75) ended them only after HOST_FAULT_TRIES faults; "1" keeps every host off."""
    from ttp import checkspill
    assert checkspill.all_off("1") and not checkspill.all_off("") and checkspill.kept_off("") == set()
    off = checkspill.spill_off_add("", "host-a", fault=True)
    assert checkspill.kept_off(off) == set(), "a host fault (paused mid-run) leaves it one more try"
    off = checkspill.spill_off_add(off, "host-b", fault=False)
    assert checkspill.kept_off(off) == {"host-b"} and not checkspill.all_off(off)
    off = checkspill.spill_off_add(off, "host-a", fault=True)
    assert checkspill.kept_off(off) == {"host-a", "host-b"}
    assert checkspill.kept_off(checkspill.spill_off_add("1", "user@host-c:22", fault=False)) == {"user@host-c:22"}


def test_a_host_kept_off_by_its_own_faults_is_tried_again_once_the_wrapper_changes_or_the_back_off_passes():
    """#7917: a spill host whose own fault (exit 75) ended the checks HOST_FAULT_TRIES times is kept off under
    the checks config they ran with, never for good: a new checks.spill_wrap_cmd, or FAULT_RETRY_S later, it is
    tried again. A host the checks failed on stays off."""
    from ttp import checkspill
    old = {"checks": {"spill_wrap_cmd": "old-wrap"}}
    off = checkspill.spill_off_add("", "host-a", fault=True, cfg=old)
    off = checkspill.spill_off_add(off, "host-b", fault=False, cfg=old)
    assert checkspill.kept_off(off, old) == {"host-b"}, "one fault leaves it one more try"
    off = checkspill.spill_off_add(off, "host-a", fault=True, cfg=old)
    assert checkspill.kept_off(off, old) == {"host-a", "host-b"}
    assert checkspill.kept_off(off) == {"host-a", "host-b"}, "no config given: the time alone decides"
    new = {"checks": {"spill_wrap_cmd": "new-wrap"}}
    assert checkspill.kept_off(off, new) == {"host-b"}, "a changed wrapper tries the host again"
    later = time.time() + checkspill.FAULT_RETRY_S + 1
    assert checkspill.kept_off(off, old, now=later) == {"host-b"}, "so does the back-off passing"
    assert checkspill.kept_off("host-a:fault,host-a:fault", new) == {"host-a"}, "an older run's plain mark holds"


def test_a_run_waiting_here_moves_to_a_spill_host_once_its_lock_frees(env, tmp_path, monkeypatch):
    """#7917: a run that found its only spill host busy waits here in its place and tries the host again at
    each bound: once the host's lock frees it spills there, the lane here still held."""
    import threading
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    busy = []
    monkeypatch.setattr(checkspill, "lock_busy", lambda _p, _c, _a: (
        busy.append(1) or "busy (held by another run; 1 waiting)") if len(busy) < 3 else "")
    lane = _spill_lane(p, repo)
    safety = threading.Timer(30, lane.close)   # never hang the suite: the lane frees after 30 s at the latest
    safety.start()
    try:
        cli.main(["checks", "--fresh"])
    finally:
        safety.cancel()
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec.get("spilled_to") == "check-host-a", rec
    assert len(busy) == 3, busy
    text = (run / "checks.log").read_text()
    assert "check-host-a (busy (held by another run" in text and "spilling to check-host-a" in text, text
    assert "/ttp-check." in log.read_text()
    assert checkspill.holders(p.name) == []


def test_a_waiting_rerun_moves_to_a_host_its_faults_kept_off_once_the_wrapper_changes(env, tmp_path, monkeypatch):
    """#7917: a rerun whose only spill host faulted twice under the old checks.spill_wrap_cmd waits here; once
    the wrapper changes, the next bound tries that host again and the run moves there."""
    import io
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    p.set_config("checks.spill_wrap_cmd", "old-wrap")
    off = ""
    for _ in range(checkspill.HOST_FAULT_TRIES):
        off = checkspill.spill_off_add(off, "check-host-a", fault=True, cfg=p.config())
    real, results = checkspill.pick, []

    def pick(*a, **k):
        results.append(real(*a, **k))
        if len(results) == 2:   # the wrapper is fixed while the run waits here
            p.set_config("checks.spill_wrap_cmd", "new-wrap")
        return results[-1]
    monkeypatch.setattr(checkspill, "pick", pick)
    lane = _spill_lane(p, repo)
    try:
        slot = cli._checks_slot(p, run, io.StringIO(), "f" * 40, True, cmds=["true"], top=str(repo),
                                spill=True, spill_off=off)
    finally:
        lane.close()
    try:
        assert slot is not None and slot.spill is not None and slot.spill.host == "check-host-a", results
        assert results[:2] == [None, None] and results[-1] is slot.spill, results
    finally:
        slot and slot.close()


def test_a_run_with_no_usable_spill_host_still_waits_here_under_the_load_guard(env, tmp_path, monkeypatch):
    """#7917 (checks.load_guard, a safety rule): a run whose only spill host its faults keep off tries it at
    each bound and finds it off, takes no host-wide checks slot while the guard holds, and starts here only
    once the floor frees."""
    import io
    from ttp import checkcap, checkspill, cli, testslots
    monkeypatch.delenv("TTP_CHECKS_LOAD_GUARD", raising=False)
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    monkeypatch.setattr(checkcap, "_load", lambda: 900.0)
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: 900.0)
    monkeypatch.setenv("TTP_TEST_POOL", "4")
    p = make(env)
    p.set_config("delivery.checks_spill_hosts", ["check-host-a"])
    p.set_config("delivery.checks_spill_after_s", 0.3)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    off = ""
    for _ in range(checkspill.HOST_FAULT_TRIES):
        off = checkspill.spill_off_add(off, "check-host-a", fault=True, cfg=p.config())
    floor = _hold_load_floor()   # another run holds the one routine pass
    real, results, grants = checkspill.pick, [], []
    before = len(checkcap.grant_records())

    def pick(*a, **k):
        results.append(real(*a, **k))
        if len(results) == 3:
            grants.append(len(checkcap.grant_records()) - before)
            floor.close()
        return results[-1]
    monkeypatch.setattr(checkspill, "pick", pick)
    out = io.StringIO()
    try:
        assert testslots.load_guard()
        slot = cli._checks_slot(p, run_dir, out, "f" * 40, True, cmds=["true"], top=str(p.base), spill=True,
                                spill_off=off)
    finally:
        floor.close()
    try:
        assert slot is not None and slot.spill is None and slot.f is not None, "it ran here"
        assert len(results) >= 3 and not any(results) and grants == [0], (results, grants)
        assert "check-host-a (these checks did not pass there in this run)" in out.getvalue(), out.getvalue()
    finally:
        slot.close()


def test_an_urgent_rerun_after_a_spill_host_still_takes_a_free_pool_host_at_once(env, tmp_path, monkeypatch):
    """#6379: an urgent rerun of checks that failed on a spill host still goes first: it asks the pool at
    once (no checkspill.after_s wait), keeping off the host it failed on, and takes no Slurm overflow."""
    import io
    from ttp import checkcap, checkspill, cli, slurmcheck
    p = make(env)
    p.set_config("delivery.checks_spill_hosts", ["check-host-a", "check-host-b"])
    p.set_config("delivery.checks_spill_after_s", 60)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.setattr(checkcap, "task_rank", lambda _p: (0, []))
    asked = []
    monkeypatch.setattr(checkspill, "pick", lambda _p, _c, _t, _w="", off="", _why="": asked.append(off) or (
        {"host": "check-host-b"} if "check-host-b" not in checkspill.kept_off(off) else None))
    monkeypatch.setattr(slurmcheck, "pick", lambda *_a, **_k: pytest.fail("no Slurm overflow for a rerun"))
    held = checkcap.acquire("routine", priority=3)
    try:
        t0 = time.time()
        off = checkspill.spill_off_add("", "check-host-a", fault=False)
        slot = cli._checks_slot(p, run_dir, io.StringIO(), "f" * 40, True, cmds=["true"], top=str(p.base),
                                spill=True, spill_off=off)
        assert slot is not None and slot.spill == {"host": "check-host-b"} and asked == [off]
        assert time.time() - t0 < 20, "no wait for the 60 s spill bound"
    finally:
        held.close()


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
    # One run on each host: pick() spreads runs, so with equal runs it tries them in their listed order.
    on_b = checkspill.pick(p, cfg, told.append, "pytest -q")
    assert on_b is not None and on_b.host == "check-host-b"
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
        on_b.close()


def test_spill_beside_device_takes_a_listed_host_with_a_device_job_but_keeps_pauses_and_lock_holds(
        env, tmp_path, monkeypatch):
    """checks.spill_beside_device: a listed host whose device lock is held, or whose probe finds a device
    holder, still takes the run, and the run and its `ttp status` line say it runs beside a device job; an
    unlisted host is skipped as before; a listed host that is paused or whose machine lock is held is still
    skipped; "*" covers every host; a malformed value is a config problem and lists nothing."""
    from ttp import checkspill, cli, db as dbm, locks, project
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    told: list[str] = []
    devs = [_device_hold(p, "check-host-a-device"), _device_hold(p, "check-host-b-device")]
    try:
        assert checkspill.pick(p, p.config(), told.append, "pytest -q") is None, "unlisted: skipped as today"
        assert "check-host-a (a device job runs there" in told[-1] and "check-host-b (a device job" in told[-1]
        p.set_config("checks.spill_beside_device", ["check-host-b"])
        cfg = p.config()
        assert not checkspill.beside_device(cfg, "check-host-a") and checkspill.beside_device(cfg, "check-host-b")
        got = checkspill.pick(p, cfg, told.append, "pytest -q")
        try:
            assert got is not None and got.host == "check-host-b", told
            assert "check-host-a (a device job runs there" in told[-1], "the unlisted host is still skipped"
            assert "spilling to check-host-b" in told[-1] and \
                "beside a device job (checks.spill_beside_device lists it): check-host-b-device: busy" in told[-1]
            line = [x for x in checkspill.status_lines(p.name) if "check-host-b" in x]
            assert line and "(beside a device job, checks.spill_beside_device: check-host-b-device: busy" in line[0]
            assert "beside a device job" in cli.status_text(p, verbose=True)
            assert not [x for x in checkspill.skip_lines() if "spill host check-host-b " in x], "taken, not skipped"
        finally:
            if got is not None:
                got.close()
        p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"check-host-b": {"reason": "user hold", "by": "user", "since": 1}})
        assert checkspill.pick(p, p.config(), told.append, "pytest -q") is None, "a pause still stops it"
        assert "check-host-b (paused: user hold)" in told[-1], told[-1]
        p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {})
        held = locks.try_take(_spill_lock_paths(p, "check-host-b"), "task #9 (run 1)", "make bench")
        assert held is not None
        try:
            assert checkspill.pick(p, p.config(), told.append, "pytest -q") is None, "a held lock still stops it"
            assert "check-host-b (busy (held by task #9" in told[-1], told[-1]
        finally:
            held.close()
        p.set_config("checks.spill_beside_device", "*")
        got = checkspill.pick(p, p.config(), told.append, "pytest -q")
        try:
            assert got is not None and got.host == "check-host-a" and "beside a device job" in told[-1], told
        finally:
            got.close()
    finally:
        for d in devs:
            d.close()
    got = checkspill.pick(p, p.config(), told.append, "pytest -q")
    try:
        assert got is not None and "beside a device job" not in told[-1], "no device job: nothing to say"
        assert "beside" not in checkspill.status_lines(p.name)[0]
    finally:
        got.close()
    assert checkspill.beside_of('["check-host-a", "check-host-a"]') == ["check-host-a"]
    assert checkspill.beside_of(["check-host-a", "*"]) == "*"
    for bad in ("check-host-a", 3, {"check-host-a": True}, ["-oProxyCommand=x"]):
        try:
            checkspill.beside_of(bad)
        except ValueError as e:
            assert "checks.spill_beside_device" in str(e), e
        else:
            raise AssertionError(bad)
        assert not checkspill.beside_device({"checks": {"spill_beside_device": bad}}, "check-host-a"), bad
        assert [x for x in project.config_problems({"checks": {"spill_beside_device": bad}})
                if "spill_beside_device" in x], bad


@_GNU_FIND
def test_spill_beside_device_lets_a_listed_host_take_a_run_while_its_probe_finds_a_device_holder(
        env, tmp_path, monkeypatch):
    """A device test that takes no lock on a host checks.spill_beside_device lists: the probe's holder scan
    keeps no run off, and the run says it runs beside that holder; unlisted, the host is skipped."""
    from ttp import checkspill
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    nodes = tmp_path / "dev"
    nodes.mkdir()
    (nodes / "tenstorrent0").write_text("")
    monkeypatch.setenv(checkspill.NODES_ENV, str(nodes / "tenstorrent*"))
    outside = _node_holder(nodes / "tenstorrent0", cwd=tmp_path)
    told: list[str] = []
    try:
        assert checkspill.pick(p, p.config(), told.append, "pytest -q") is None
        p.set_config("checks.spill_beside_device", ["check-host-a"])
        got = checkspill.pick(p, p.config(), told.append, "pytest -q")
        try:
            assert got is not None and got.host == "check-host-a", told
            assert f"beside a device job (checks.spill_beside_device lists it): pid {outside.pid}" in told[-1], told
            assert "device locks alone decided" not in told[-1]
        finally:
            got.close()
    finally:
        outside.kill()
        outside.wait()


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


def test_spill_probe_device_holder_scan_names_outside_holders_and_reports_a_scan_that_did_not_read():
    """_device_holders on fake probe output: a holder the scan names makes the host busy with its pid and
    command name (more than DEVICE_HOLDERS_SHOWN are counted, a cut scan's holders still count); a scan that
    ended with none, or a host with no device node, is not busy; a scan cut, unable to run or with no end line
    is not busy either, and says why."""
    from ttp import checkspill
    head = "ttp-spill 8 16000000 0\n"
    busy, note = checkspill._device_holders(
        head + "ttp-spill-dev 4242 /dev/tenstorrent/0 run.sh\nttp-spill-dev-end\nttp-spill-dev-rc 0\n")
    assert (busy, note) == ("a device job runs there: pid 4242 (run.sh) holds /dev/tenstorrent/0", "")
    many = "".join(f"ttp-spill-dev {i} /dev/tenstorrent/{i} t{i}\n" for i in range(1, 6))
    busy, note = checkspill._device_holders(head + many + "ttp-spill-dev-rc 124\n")
    assert busy.startswith("a device job runs there: pid 1 (t1) holds /dev/tenstorrent/1, pid 2 (t2)") and note == ""
    assert busy.endswith("pid 3 (t3) holds /dev/tenstorrent/3 and 2 more process(es)"), busy
    for quiet in ("ttp-spill-dev-end\nttp-spill-dev-rc 0\n", "ttp-spill-dev-end none\n"):
        assert checkspill._device_holders(head + quiet) == ("", ""), quiet
    for out, said in (("ttp-spill-dev-rc 124\n", "timed out after 15 s"), ("ttp-spill-dev-rc 137\n", "timed out"),
                      ("ttp-spill-dev-end nofind\nttp-spill-dev-rc 0\n", "cannot run (no GNU find there)"),
                      ("ttp-spill-dev-end notimeout\n", "cannot run (no timeout command there)"),
                      ("ttp-spill-dev-rc 2\n", "did not finish (exit 2)"), ("", "did not finish (exit unknown)"),
                      ("ttp-spill-dev 12\nttp-spill-dev-rc 0\n", "did not finish (exit 0)")):
        busy, note = checkspill._device_holders(head + out)
        assert busy == "" and said in note and note.startswith("its device holder scan "), (out, note)


def _node_holder(node, cwd=None, env=None):
    """A process holding device node `node` open (in `cwd`, with `env`), once it has it open."""
    hold = "import sys, time; f = open(sys.argv[1], 'rb'); print('open', flush=True); time.sleep(120)"
    proc = subprocess.Popen([sys.executable, "-c", hold, str(node)], stdout=subprocess.PIPE, text=True, cwd=cwd,
                            env=env)
    assert proc.stdout.readline() == "open\n"
    return proc


@_GNU_FIND
def test_spill_skips_a_host_where_a_process_outside_the_pool_holds_a_device_node_open(env, tmp_path, monkeypatch):
    """A device test that takes no lock: the host's probe finds a process holding one of its device nodes
    open, so a new run skips the host, saying which pid and command, and `ttp status` shows it. The pool's
    own check runs holding one (their folder, or the PWD they inherited, a ttp-check.* folder of the host's
    temp folder) do not count, one named so elsewhere does, and once the outside holder ends the host takes runs again."""
    from ttp import checkspill
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    nodes = tmp_path / "dev"
    nodes.mkdir()
    (nodes / "tenstorrent0").write_text("")
    monkeypatch.setenv(checkspill.NODES_ENV, str(nodes / "tenstorrent*"))
    cfg = p.config()
    told: list[str] = []
    own = remote / "ttp-check.own00001"
    own.mkdir()
    procs = [_node_holder(nodes / "tenstorrent0", cwd=own),
             _node_holder(nodes / "tenstorrent0", cwd=tmp_path, env={**os.environ, "PWD": str(own)})]
    try:
        got = checkspill.pick(p, cfg, told.append, "pytest -q")
        assert got is not None and got.host == "check-host-a", told
        assert "device holder scan" not in told[-1], told
        got.close()
        elsewhere = tmp_path / "ttp-check.notours1"   # named like a pool run, but not in the host's temp folder
        elsewhere.mkdir()
        outside = _node_holder(nodes / "tenstorrent0", cwd=elsewhere, env={**os.environ, "PWD": str(elsewhere)})
        procs.append(outside)
        assert checkspill.pick(p, cfg, told.append, "pytest -q") is None
        why = f"check-host-a (a device job runs there: pid {outside.pid} (python"
        assert told[-1].startswith("no spill host could take the checks, waiting here: " + why), told[-1]
        assert f"holds {nodes}/tenstorrent0" in told[-1] and str(procs[0].pid) not in told[-1], told[-1]
        line = f"spill host check-host-a takes no new checks: a device job runs there: pid {outside.pid} (python"
        assert [x for x in checkspill.skip_lines() if x.startswith(line)], checkspill.skip_lines()
        outside.kill()
        outside.wait()
        again = checkspill.pick(p, cfg, told.append, "pytest -q")
        assert again is not None and again.host == "check-host-a", told
        again.close()
        assert checkspill.skip_lines() == []
    finally:
        for x in procs:
            x.kill()
            x.wait()


def test_spill_device_holder_scan_that_times_out_or_cannot_run_never_keeps_a_run_off_and_is_told(
        env, tmp_path, monkeypatch):
    """The holder scan cut at DEVICE_SCAN_S (a fake `timeout` that exits 124), or a host with no `timeout`
    command, keeps no run off the host: the run spills there and says the device locks alone decided."""
    from ttp import checkspill
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    nodes = tmp_path / "dev"
    nodes.mkdir()
    (nodes / "tenstorrent0").write_text("")
    monkeypatch.setenv(checkspill.NODES_ENV, str(nodes / "tenstorrent*"))
    fake = tmp_path / "faketimeout"
    fake.mkdir()
    (fake / "timeout").write_text("#!/bin/sh\nexit 124\n")
    (fake / "timeout").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    told: list[str] = []
    got = checkspill.pick(p, p.config(), told.append, "pytest -q")
    assert got is not None and got.host == "check-host-a", told
    got.close()
    assert ("its device holder scan timed out after 15 s, so its device locks alone decided" in told[-1]), told
    assert checkspill.skip_lines() == []
    script = checkspill._probe_script().replace("command -v timeout", "command -v no-such-timeout-cmd")
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30).stdout
    assert checkspill._device_holders(out) == ("", "its device holder scan cannot run (no timeout command there)")


def test_a_spill_run_past_the_wall_cap_or_on_a_stalled_link_runs_again_here(env, tmp_path, monkeypatch):
    """A spill host slower than the wall-time cap (or an ssh link that stalls there) is the host's
    environment, not the change's: the checks run again here, only that result counts, and no task
    timeout stays recorded. The check-host ssh ends a dropped link through keepalives."""
    import threading
    from ttp import checkwall, cli, remotecheck
    assert "ServerAliveInterval=5" in remotecheck.SSH and "ServerAliveCountMax=12" in remotecheck.SSH
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
    assert checkspill.spillable(push.with_host(c[:1], "check-host-a")), "delivery.checks_host does not keep it there"
    assert not checkspill.spillable(push.check_list([{"run": "make test", "host": "check-host-a"}]))
    assert not checkspill.spillable(push.with_wrap(c[:1], ("flock", "/tmp/x")))
    assert not checkspill.spillable([])


def test_the_account_pool_spills_every_project_after_its_own_list_and_an_empty_own_list_keeps_it_here(
        env, tmp_path, monkeypatch):
    """checks.spill_hosts in the account settings: a project with no list of its own spills to the pool,
    which needs the machines list; a narrower own list is tried first, then the pool; an own [] keeps every run here."""
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
    assert checkspill.allowed(p, p.config(), "pool-b") == "", "the pool alone allows it"
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
    # A narrower own list goes first and the pool follows it; the own list alone allows its hosts.
    p.set_config("delivery.checks_spill_hosts", ["check-host-a", "pool-b"])
    assert checkspill.hosts(p.config()) == ["check-host-a", "pool-b", "pool-a"]
    machines.add("check-host-a")
    assert checkspill.allowed(p, p.config(), "check-host-a") == ""
    assert checkspill._source(p.config(), "pool-b") == "delivery.checks_spill_hosts"
    # An own empty list opts out of the pool; a malformed own list spills nowhere.
    p.set_config("delivery.checks_spill_hosts", [])
    assert checkspill.hosts(p.config()) == []
    for bad in ("pool-a", ["a b"]):
        assert checkspill.hosts({"delivery": {"checks_spill_hosts": bad}}) == [], bad
    project.set_account_setting(checkspill.ACCOUNT_KEY, "pool-a")   # malformed pool: none
    assert checkspill.account_hosts() == [] and checkspill.hosts({}) == []


def test_a_project_excludes_a_pool_host_by_alias_or_machines_list_hostname(env, tmp_path, monkeypatch):
    """delivery.checks_spill_exclude keeps named hosts out of every spill route, the account's pool and
    checks.spill_beside_device included: an alias or the machines list's hostname (full or short) matches, the
    run spills to the next host and its log names the setting; `ttp checks status` lists the excluded hosts."""
    from ttp import checkspill, cli, machines, project
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, [], charter=())
    raw = json.loads(p.config_path.read_text())
    raw["delivery"].pop("checks_spill_hosts")
    p.config_path.write_text(json.dumps(raw))
    project.set_account_setting(checkspill.ACCOUNT_KEY, ["pool-a", "pool-b", "pool-c"])
    machines.add("pool-a", hostname="serve-a.example.test")
    machines.add("pool-b")
    machines.add("pool-c")
    p.set_config("checks.spill_beside_device", "*")
    p.set_config("delivery.checks_spill_exclude", ["SERVE-A", "pool-c"])   # a short host name, any case; an alias
    assert checkspill.hosts(p.config()) == ["pool-b"]
    assert checkspill.excluded_hosts(p.config()) == ["pool-a", "pool-c"]
    for h in ("pool-a", "pool-c"):
        assert checkspill.allowed(p, p.config(), h) == "excluded by the project (delivery.checks_spill_exclude)"
    assert checkspill.exclude_lines(p.config()) == [
        "spill hosts excluded by the project (delivery.checks_spill_exclude): pool-a, pool-c"]
    p.set_config("delivery.checks_spill_exclude", ["serve-a.example.test"])   # the full host name matches too
    assert checkspill.hosts(p.config()) == ["pool-b", "pool-c"]
    p.set_config("delivery.checks_spill_exclude", ["pool-a"])
    log = tmp_path / "where.log"
    p.set_config("delivery.push_checks", [f"pwd >> {log}"])
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "pool-b", rec
    assert "pool-a (excluded by the project: delivery.checks_spill_exclude)" in (run / "checks.log").read_text()
    lines = checkspill.pick_lines(p.name)
    assert any("pool-a took no run: excluded by the project" in x for x in lines), lines


def test_spill_exclude_default_and_settings(env, tmp_path, monkeypatch):
    """The empty default changes no host set; a project's own list is excluded from as the pool is; a malformed
    exclude list is named by the config check and excludes nothing; the coordinator may set it."""
    from ttp import checkspill, coordinator, machines, project
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"], charter=())
    machines.add("check-host-a")
    before = checkspill.hosts(p.config())
    assert before == ["check-host-a", "check-host-b"]
    p.set_config("delivery.checks_spill_exclude", [])
    assert checkspill.hosts(p.config()) == before and checkspill.excluded_hosts(p.config()) == []
    assert checkspill.exclude_lines(p.config()) == [] and checkspill.allowed(p, p.config(), "check-host-a") == ""
    p.set_config("delivery.checks_spill_exclude", ["check-host-b"])
    assert checkspill.hosts(p.config()) == ["check-host-a"]
    for bad in ("check-host-a", 3, ["a b"]):
        assert any("delivery.checks_spill_exclude" in x
                   for x in project.config_problems({"delivery": {"checks_spill_exclude": bad}})), bad
        assert checkspill.hosts({"delivery": {"checks_spill_hosts": ["check-host-a"],
                                              "checks_spill_exclude": bad}}) == ["check-host-a"], bad
    settable = coordinator.settable("delivery.checks_spill_exclude")
    assert settable('["host-a"]') == ["host-a"] and settable([]) == []
    with pytest.raises(ValueError):
        settable("host-a")


def test_spill_gate_reads_only_structured_settings_never_charter_wording(env, tmp_path, monkeypatch):
    """allowed() decides from delivery.checks_spill_hosts and the account's pool, never the charter: a listed
    host with no charter line is allowed, a host the charter names but no setting lists is refused with the
    setting to add it to, and the user's pauses still keep a listed host off."""
    from ttp import checkspill, db as dbm, machines
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"], charter=())
    machines.add("check-host-a")
    machines.add("named-only")
    p.charter_path.write_text("# Charter\n## Resources\n- named-only, NAMED-ONLY: a check host\n")
    assert checkspill.allowed(p, p.config(), "check-host-a") == "", "the setting alone allows it"
    why = checkspill.allowed(p, p.config(), "named-only")
    assert why.startswith("not a spill host of this project") and "charter" not in why, why
    assert f"ttp config {p.name} delivery.checks_spill_hosts '[\"check-host-a\", \"named-only\"]'" in why, why
    assert "named-only" not in checkspill.hosts(p.config())
    # A user hold or pause still keeps a listed host off.
    p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"check-host-a": {"reason": "user hold", "by": "user", "since": 1}})
    assert checkspill.allowed(p, p.config(), "check-host-a") == "paused: user hold"


def test_a_machines_list_checks_tag_admits_no_spill_host(env, tmp_path, monkeypatch):
    """Any run may tag a machine (`ttp machines add <alias> --tags checks`), so no tag widens spill hosts:
    a host tagged `checks` in neither the project's list nor the account's pool is neither tried nor allowed."""
    from ttp import checkspill, machines
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"], charter=())
    machines.add("check-host-a")
    machines.add("tagged-host", tags=["x86", "checks"])
    machines.add("tagged-upper", tags="CHECKS,x86")
    assert checkspill.hosts(p.config()) == ["check-host-a"]
    for host in ("tagged-host", "tagged-upper"):
        why = checkspill.allowed(p, p.config(), host)
        assert why.startswith("not a spill host of this project") and "delivery.checks_spill_hosts" in why, why


def test_spill_hosts_accepted_through_the_charter_before_stay_allowed_without_it(env, tmp_path, monkeypatch):
    """Upgrade: every host the old charter-matching gate accepted was already in the project's own list or
    the account's pool (only hosts() is tried), so with the charter line gone it stays allowed and the
    project's settings are left as they were: no seed is needed."""
    from ttp import checkspill
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    before = p.config()["delivery"]["checks_spill_hosts"]
    p.charter_path.write_text("# Charter\n")
    assert [checkspill.allowed(p, p.config(), h) for h in checkspill.hosts(p.config())] == ["", ""]
    assert p.config()["delivery"]["checks_spill_hosts"] == before


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



def test_the_account_ban_list_keeps_a_spill_host_out_by_alias_or_host_name_and_an_unlisted_host_still_spills(
        env, tmp_path, monkeypatch):
    """checks.no_spill_hosts in the account settings: a host it names, by alias or by the machines list's host
    name (full or short), is never tried or allowed, from the project's own list or the account's pool; a host
    it does not name still spills, and the run's log says why the banned one was skipped."""
    from ttp import checkspill, cli, machines, project
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    machines.add("check-host-b", hostname="serve-b.example")
    project.set_account_setting(checkspill.ACCOUNT_KEY, ["down"])
    project.set_account_setting(checkspill.NO_SPILL_KEY, ["CHECK-HOST-A", "serve-b", "down"])
    assert checkspill.hosts(p.config()) == []
    assert checkspill.banned("check-host-a") == "CHECK-HOST-A", "by alias, any case"
    assert checkspill.banned("check-host-b") == "serve-b", "by the machines list's host name, short form"
    assert "checks.no_spill_hosts" in checkspill.allowed(p, p.config(), "check-host-b")
    assert checkspill.allowed(p, p.config(), "down").startswith("banned for every project"), "the pool too"
    project.set_account_setting(checkspill.NO_SPILL_KEY, ["check-host-a", "serve-b.example"])
    assert checkspill.banned("check-host-b") == "serve-b.example", "by the full host name"
    assert checkspill.hosts(p.config()) == ["down"]
    project.set_account_setting(checkspill.NO_SPILL_KEY, ["check-host-a"])
    assert checkspill.hosts(p.config()) == ["check-host-b", "down"]
    assert checkspill.allowed(p, p.config(), "check-host-b") == "", "an unlisted host still spills"
    log = tmp_path / "where.log"
    p.set_config("delivery.push_checks", [f"pwd >> {log}"])
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-b", rec
    text = (run / "checks.log").read_text()
    assert "check-host-a (banned by the account's checks.no_spill_hosts)" in text, text
    project.set_account_setting(checkspill.NO_SPILL_KEY, "check-host-a")   # malformed: no ban, never a crash
    assert checkspill.no_spill() == [] and "check-host-a" in checkspill.hosts(p.config())


def test_a_spill_list_naming_a_banned_host_is_refused_and_a_run_may_only_add_to_the_ban_list(
        env, tmp_path, capsys, monkeypatch):
    """`ttp config` and config_set refuse a project spill list or account pool naming a host the account bans;
    a list set before the ban is flagged by the config check; `ttp checks status` shows the ban; a run may
    add hosts to the ban list but never remove one, and each change is logged with its undo."""
    from ttp import accountset, checkspill, cli, coordinator, machines, project
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    for k in ("TTP_RUN_ID", "TTP_RUN_DIR"):   # the user at a terminal until the run below
        monkeypatch.delenv(k, raising=False)
    machines.add("check-host-b", hostname="serve-b.example")
    p.set_config("delivery.checks_spill_hosts", ["check-host-b"])
    cli.main(["config", "--account", "checks.no_spill_hosts", '["serve-b.example"]'])
    err = capsys.readouterr().err
    assert checkspill.no_spill() == ["serve-b.example"]
    assert f"{p.name}'s delivery.checks_spill_hosts names check-host-b" in err, err
    assert any("check-host-b is banned" in x for x in project.config_problems(p.raw_config()))
    for argv in (["config", p.name, "delivery.checks_spill_hosts", '["check-host-c", "check-host-b"]'],
                 ["config", "--account", "checks.spill_hosts", '["check-host-b"]']):
        with pytest.raises(SystemExit):
            cli.main(argv)
        assert "banned from every project's checks" in capsys.readouterr().err, argv
    with pytest.raises(ValueError, match="banned"):
        coordinator.USER_SETTABLE["delivery.checks_spill_hosts"](["check-host-b"])
    assert coordinator.USER_SETTABLE["delivery.checks_spill_hosts"](["check-host-c"]) == ["check-host-c"]
    cli.main(["config", p.name, "delivery.checks_spill_hosts", '["check-host-c"]'])
    assert p.config()["delivery"]["checks_spill_hosts"] == ["check-host-c"]
    assert checkspill.no_spill_lines() == [
        "spill hosts banned for every project (checks.no_spill_hosts): serve-b.example"]
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["checks", "status"])
    assert "spill hosts banned for every project (checks.no_spill_hosts): serve-b.example" in capsys.readouterr().out
    monkeypatch.setenv("TTP_RUN_ID", "7")
    for value in ("[]", "", '["check-host-x"]'):
        with pytest.raises(SystemExit):
            cli.main(["config", "--account", "checks.no_spill_hosts", value])
        assert "only the user lifts a ban" in capsys.readouterr().err, value
    cli.main(["config", "--account", "checks.no_spill_hosts", '["serve-b.example", "check-host-x"]'])
    assert checkspill.no_spill() == ["serve-b.example", "check-host-x"]
    log = [json.loads(x) for x in accountset.log_path().read_text().splitlines()]
    assert [r["new"] for r in log if r["key"] == "checks.no_spill_hosts"] == [
        ["serve-b.example"], ["serve-b.example", "check-host-x"]]


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
    assert f"no spill host can take these checks: {checkspill.WRAP_NOTE}" in (run / "checks.log").read_text()
    c = push.with_wrap(push.check_list(["make test"]), (str(wrapper),))
    for v in (None, True, "false", 0):
        assert not checkspill.spillable(c, {"checks": {"wrap_needs_device": v}}), v
    assert checkspill.spillable(c, {"checks": {"wrap_needs_device": False}})
    assert checkspill.spillable(push.with_host(c, "check-host-a"), {"checks": {"wrap_needs_device": False}})
    assert not project.config_problems({"checks": {"wrap_needs_device": False}})
    assert any("checks.wrap_needs_device" in x for x in project.config_problems({"checks": {"wrap_needs_device": "no"}}))


def test_a_wrapped_check_kept_here_says_its_wrapper_keeps_it_and_names_the_settings_not_a_pause(env, tmp_path):
    """Checks behind checks.wrap_cmd with neither checks.spill_wrap_cmd set nor checks.wrap_needs_device false
    never spill: wrap_keeps_here and the queue-wait item's spill note say so and name both settings, ahead of
    any host's pause; a check kept here for another reason, an unwrapped one, or either setting set: no note."""
    from ttp import checkspill, push, queuewait
    wrapped = {"checks": {"wrap_cmd": "./wrap.sh"}}
    c = push.with_wrap(push.check_list(["make test"]), ("./wrap.sh",))
    assert checkspill.wrap_note(wrapped) == checkspill.WRAP_NOTE
    assert checkspill.wrap_keeps_here(c, wrapped) == checkspill.WRAP_NOTE
    for v in ("checks.wrap_cmd", "checks.spill_wrap_cmd", "checks.wrap_needs_device"):
        assert v in checkspill.WRAP_NOTE
    for cfg in ({"checks": {"wrap_cmd": "./wrap.sh", "spill_wrap_cmd": "./remote-wrap.sh"}},
                {"checks": {"wrap_cmd": "./wrap.sh", "wrap_needs_device": False}}):
        assert checkspill.wrap_note(cfg) == "" and checkspill.wrap_keeps_here(c, cfg) == "", cfg
    assert checkspill.wrap_note({}) == "", "no checks.wrap_cmd"
    assert checkspill.wrap_keeps_here(push.check_list(["make test"]), wrapped) == "", "unwrapped"
    kept = push.with_wrap(push.check_list([{"run": "make test", "spill": False}]), ("./wrap.sh",))
    assert checkspill.wrap_keeps_here(kept, wrapped) == "", "kept here by its own spill: false"
    note = queuewait.spill_note(([], ["host-a: paused: lent out"]), wrapped)
    assert note.startswith("No spill host can take checks behind checks.wrap_cmd: checks behind"), note
    assert "checks.spill_wrap_cmd" in note and "Other checks: no spill host can take them now (host-a: paused" in note
    assert "checks.wrap_needs_device" in queuewait.spill_note((["host-a"], []), wrapped), "a free host too"
    assert queuewait.spill_note(([], ["host-a: paused: lent out"]), {}) == (
        "No spill host can take checks now (host-a: paused: lent out), so none is offered.")
    assert queuewait.spill_note((["host-a"], []), {}) == "" and queuewait.spill_note(([], []), wrapped) == ""


def test_busy_checks_spill_to_one_host_of_a_machine_with_several_hosts(env, tmp_path, monkeypatch):
    """A spill host `alias:host` is one host of machine `alias`: it is allowed when listed (the project's
    list or the pool), its machine is in the machines list, and neither is paused, ssh reaches the host, and the run, its claim and
    checks.json name the host. A plain alias reads as before; an unknown machine is not in the list."""
    from ttp import checkspill, cli, db as dbm, machines
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["cluster-a:host-b"], charter=("cluster-a",))
    assert checkspill.target("cluster-a:host-b") == ("cluster-a", "host-b")
    assert checkspill.target("cluster-a") == ("cluster-a", "cluster-a")
    assert checkspill.target("cluster-x:host-b") == ("cluster-x:host-b", "cluster-x:host-b")
    cfg = p.config()
    assert checkspill.allowed(p, cfg, "cluster-a:host-b") == ""
    assert checkspill.allowed(p, cfg, "cluster-x:host-b").startswith("not in the machines list")
    machines.add("cluster-c")
    assert checkspill.allowed(p, cfg, "cluster-c:host-b").startswith("not a spill host of this project")
    for n in ("host-b", "cluster-a"):
        p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {n: {"reason": "held", "by": "user", "since": 1}})
        assert checkspill.allowed(p, cfg, "cluster-a:host-b") == "paused: held", n
    p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {})
    assert checkspill.beside_device({"checks": {"spill_beside_device": ["host-b"]}}, "cluster-a:host-b")
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "host-b" and rec["hosts"] == {cmd: "host-b"}, rec
    assert "spilling to cluster-a:host-b" in (run / "checks.log").read_text()
    assert "/ttp-check." in log.read_text() and checkspill.holders(p.name) == []
    assert checkspill.kept_off(checkspill.spill_off_add("", "host-b", False)) == {"host-b"}


def test_spill_pre_run_hook_decides_per_host_go_skip_or_error(env, tmp_path, monkeypatch):
    """checks.spill_pre_cmd (none by default): run here with the host as its last argument before a run spills
    there. Exit 0 lets the run go; 75 skips the host for now; any other exit is an error, told, and the host is
    skipped; neither skip probes the host. It runs on a host spill_beside_device lists too, and a hook that does
    not parse is a config problem and skips every host."""
    from ttp import checkspill, project
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    probed = []
    monkeypatch.setattr(checkspill, "probe", lambda alias, beside=False: (probed.append(alias), (1, "", ""))[1])
    seen, rc = tmp_path / "hook.seen", tmp_path / "hook.rc"
    hook = tmp_path / "hook.sh"
    hook.write_text(f'#!/bin/sh\necho "$* $TTP_SPILL_HOST $TTP_SPILL_MACHINE" >> {seen}\n'
                    f'echo "said $(cat {rc})"\nexit "$(cat {rc})"\n')
    hook.chmod(0o755)
    assert "spill_pre_cmd" in project._known_keys(["checks"])
    assert checkspill.pre_check(p, p.config(), "check-host-a") == "", "no hook: go"

    def pick():
        told = []
        got = checkspill.pick(p, p.config(), told.append, "pytest -q")
        if got is not None:
            got.close()
        return got, " ".join(told)
    p.set_config("checks.spill_pre_cmd", f"{hook} --check")
    rc.write_text("0")
    got, told = pick()
    assert got is not None and got.host == "check-host-a" and probed == ["check-host-a"], told
    assert seen.read_text() == "--check check-host-a check-host-a check-host-a\n", "the host last"
    probed.clear()
    rc.write_text("75")
    got, told = pick()
    assert got is None and probed == [], told
    assert "check-host-a (its pre-run hook says not now (checks.spill_pre_cmd): said 75)" in told, told
    assert any("check-host-a takes no new checks: its pre-run hook says not now" in x
               for x in checkspill.skip_lines()), checkspill.skip_lines()
    rc.write_text("3")
    got, told = pick()
    assert got is None and probed == [], told
    assert "check-host-a (its pre-run hook error: checks.spill_pre_cmd exited 3: said 3)" in told, told
    p.set_config("checks.spill_beside_device", "*")
    rc.write_text("75")
    got, told = pick()
    assert got is None and "pre-run hook says not now" in told, "the hook decides beside a device job too"
    raw = json.loads(p.config_path.read_text())
    raw["checks"]["spill_pre_cmd"] = 'a "b'
    p.config_path.write_text(json.dumps(raw))
    got, told = pick()
    assert got is None and "its pre-run hook error: checks.spill_pre_cmd" in told, told
    assert any("checks.spill_pre_cmd" in x for x in project.config_problems(raw)), "named as a config problem"
    raw["checks"]["spill_pre_cmd"] = str(tmp_path / "missing-hook")
    p.config_path.write_text(json.dumps(raw))
    got, told = pick()
    assert got is None and "checks.spill_pre_cmd cannot run" in told, told


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


def test_a_saturated_disk_sends_a_new_run_to_a_spill_host_before_any_wait_here(env, tmp_path, monkeypatch):
    """While this host's disk is saturated (diskgate), a run that may spill tries the spill hosts before
    it waits here at all, even with its lane free and a long checks_spill_after_s; with none free it
    waits here and runs here once the disk clears, never failing for it."""
    import threading
    from ttp import checkcap, checkspill, cli, diskgate
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    p.set_config("delivery.checks_spill_after_s", 3600)
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    held = [True]
    picks = []
    after_pick = [0]

    def saturated():
        if picks and held[0]:   # the disk clears only once the run, the spill hosts tried, saw it held here
            after_pick[0] += 1
            held[0] = after_pick[0] < 2
            return "Dirty 7.6 GB over the 2.0 GB limit (checks.dirty_max_mb)"
        return "Dirty 7.6 GB over the 2.0 GB limit (checks.dirty_max_mb)" if held[0] else ""
    monkeypatch.setattr(diskgate, "saturated", saturated)
    real_acquire = checkcap.acquire
    monkeypatch.setattr(checkcap, "acquire", lambda *a, **k: pytest.fail("no wait here before the spill hosts"))
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", rec
    text = (run / "checks.log").read_text()
    assert "Dirty 7.6 GB over the 2.0 GB limit (checks.dirty_max_mb): trying the spill hosts first" in text, text
    # No spill host free: it waits here (the gate in checkcap._take) and runs here once the disk clears.
    monkeypatch.setattr(checkcap, "acquire", real_acquire)
    monkeypatch.setattr(checkcap, "POLL_S", 0.02)
    monkeypatch.setattr(checkspill, "pick", lambda *a, **k: picks.append(1) or None)
    log.write_text("")
    safety = threading.Timer(120, lambda: held.__setitem__(0, False))   # never hang the suite
    safety.start()
    try:
        cli.main(["checks", "--fresh"])
    finally:
        safety.cancel()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and "spilled_to" not in rec, rec
    assert picks == [1], "the spill hosts were tried first, once"
    assert "/ttp-check." not in log.read_text(), "it ran here"


def test_spill_device_gate_never_counts_a_pool_check_runs_hold_on_a_bare_alias_as_a_device_job(
        env, tmp_path, monkeypatch):
    """device.locks lists the bare machine alias, so a pool check run on that host holds the alias's lock
    (_hold). That hold, another project's in the shared locks or this project's own, under the raw or the
    canonical name, never reads as a device job. Any other hold of the alias still does, and so does one
    dressed as a check run without its mark, a check run's hold with a device job waiting or a reservation,
    and a held `<alias>-device`."""
    from ttp import checkspill, locks, shared
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    told: list[str] = []

    def spill_hold(res, where, who, take_shared=False):
        paths = locks.slot_paths(where, res, 1)
        take = locks.try_take_shared if take_shared else locks.try_take
        got = take(paths, who, "ttp checks (spilled): pytest -q", extra=checkspill.SPILL_MARK)
        assert got is not None
        return got

    for dev in (["check-host-a"], ["other-device", "check-host-a"]):
        p.set_config("device.locks", dev)
        cfg = p.config()
        assert "check-host-a" in checkspill.device_locks_of(cfg, "check-host-a")
        canon = locks.canonical(cfg, "check-host-a")
        for res, where, who, sh in (
                ("check-host-a", shared.root() / "check-host-a", "otherproj task #9 (run 1)", True),
                ("check-host-a", shared.root() / "check-host-a", "otherproj task #9 (run 1)", False),
                (canon, shared.locks_dir(p, canon, cfg), "task #4 (run 2)", False)):
            h = spill_hold(res, where, who, sh)
            try:
                assert checkspill.device_busy(p, cfg, "check-host-a") == "", (dev, res, where)
                if res != canon or where != shared.locks_dir(p, canon, cfg):   # its own hold: no free slot here
                    got = checkspill.pick(p, cfg, told.append, "pytest -q")
                    assert got is not None and got.host == "check-host-a", told
                    got.close()
            finally:
                h.close()
    cfg = p.config()
    alias_dir = shared.root() / "check-host-a"
    h = _device_hold(p, "check-host-a", alias_dir, "otherproj task #9 (run 1)")   # not a check run's hold
    try:
        assert "check-host-a: busy (held by" in checkspill.device_busy(p, cfg, "check-host-a")
        assert checkspill.pick(p, cfg, told.append, "pytest -q") is None
    finally:
        h.close()
    f = locks.try_take(locks.slot_paths(alias_dir, "check-host-a", 1), "otherproj task #9 (run 1)",
                       "ttp checks (spilled): pytest -q")   # the command text alone is no mark
    try:
        assert "check-host-a: busy" in checkspill.device_busy(p, cfg, "check-host-a")
    finally:
        f.close()
    h = spill_hold("check-host-a", alias_dir, "otherproj task #9 (run 1)", True)
    try:
        g = locks.try_take_shared(locks.slot_paths(alias_dir, "check-host-a", 1), "otherproj task #8 (run 4)",
                                  "soak")
        try:
            assert "check-host-a: busy" in checkspill.device_busy(p, cfg, "check-host-a"), "a hold beside it"
        finally:
            g.close()
        locks.reserve(locks.reserve_path(alias_dir, "check-host-a"), "otherproj task #5 (run 3)")
        try:
            assert "reserved for otherproj task #5" in checkspill.device_busy(p, cfg, "check-host-a")
        finally:
            locks.unreserve(locks.reserve_path(alias_dir, "check-host-a"), "otherproj task #5 (run 3)")
        assert checkspill.device_busy(p, cfg, "check-host-a") == ""
        d = _device_hold(p, "check-host-a-device", shared.root() / "check-host-a-device", "otherproj task #9 (run 1)")
        try:
            assert "check-host-a-device: busy (held by" in checkspill.device_busy(p, cfg, "check-host-a")
        finally:
            d.close()
    finally:
        h.close()


def test_an_agent_cli_or_host_local_check_splits_off_to_run_here_while_the_rest_spills(env):
    """A check running an agent CLI (the claude CLI, after any wrapper or in any simple command) never spills
    unless it says `spill: true`; it and a host "local" check that is no test suite, unwrapped and in a clean
    checkout stay here while the run's other checks spill. Anything else that may not spill keeps the whole
    run here; a run of such checks alone never spills. Spill admission asks the host for no agent CLI."""
    from ttp import checkspill, push
    for cli_cmd in ("claude plugin validate plugins/x --strict", "env A=1 claude plugin validate .",
                    "timeout 60 /usr/local/bin/claude plugin validate .", "nice -n 19 claude -p hi",
                    "true && claude plugin validate .", "make lint; codex exec x", "cursor-agent --help | cat",
                    'sh -c "claude plugin validate ."', "bash -lc 'cd x && claude -p hi'"):
        assert checkspill.needs_cli(cli_cmd), cli_cmd
        assert not checkspill.check_spills(push.check_list([cli_cmd])[0]), cli_cmd
    for cmd in ("python3 -m pytest -q -k claude tests/", "echo claude", "pytest tests/test_claude.py", "make t",
                'sh -c "echo claude"', "bash tools/claude.sh"):
        assert not checkspill.needs_cli(cmd), cmd
    test, cli = "python3 -m pytest -q tests/", "claude plugin validate plugins/x --strict"
    c = push.check_list([test, cli, {"run": "sh tools/lint.sh", "host": "local"}, {"run": cli, "spill": True}])
    assert checkspill.stays_here(c[1]) and checkspill.stays_here(c[2]) and not checkspill.stays_here(c[0])
    assert checkspill.spillable(c[:3]) and checkspill.spillable([c[1], c[0]]), "a split run spills"
    assert checkspill.check_spills(c[3]) and checkspill.spillable(c[3:]), "spill: true sends it too"
    assert not checkspill.spillable(c[1:3]), "nothing left to spill: it all runs here"
    keep = push.check_list([{"run": "python3 -m pytest -q tests/b", "host": "local"},   # a test suite
                            {"run": cli, "spill": False}, {"run": cli, "cwd": "authoring"}])
    keep.append(push.with_wrap(push.check_list([cli]), ("/opt/devwrap",))[0])
    for k in keep:
        assert not checkspill.stays_here(k) and not checkspill.spillable([c[0], k]), k
    assert not any(x in checkspill._probe_script() for x in checkspill.AGENT_CLIS)


def test_a_check_naming_a_path_of_this_machine_never_spills_and_splits_off_to_run_here(env):
    """A check running a script of this machine by absolute path (a project's harness/bin scanner: exabox jobs
    exited 127 on it, #7451), or naming a `~`/`$HOME` path, an input file or a `VAR=`/`--opt=` path outside the
    system folders, never goes to a spill host or a Slurm node, which get a copy of the commit only, not even
    with `"spill": true`. A cheap one splits off to run here beside the spilled suite; a suite naming one keeps
    its whole run here. System paths, output redirections and URLs still spill."""
    from ttp import checkspill, cli, push
    scan, test = "/srv/proj/harness/bin/public-commit-scan", "python3 -m pytest -q tests/"
    for cmd in (scan, f"timeout 60 {scan} --base x", "~/bin/scan", "$HOME/bin/scan", "cat < /srv/in.txt",
                "PYTHONPATH=/srv/lib sh tools/lint.sh", "sh tools/lint.sh --config=/srv/lint.cfg", f"bash -lc 'cd x && {scan}'",
                f'sh -e -c "{scan}"'):
        assert checkspill.host_path(cmd), cmd
        c = push.check_list([cmd])[0]
        assert not checkspill.check_spills(c) and checkspill.stays_here(c), cmd
        assert checkspill.spillable(push.check_list([test]) + [c]), cmd
        assert not checkspill.check_spills(push.check_list([{"run": cmd, "spill": True}])[0]), cmd
    for cmd in (test, "/usr/bin/python3 -m pytest -q tests/ 2>/dev/null >/tmp/out.log", "sh tools/lint.sh",
                "curl -fsS https://example.invalid/x", "/bin/sh -c 'make lint'", "git diff --quiet",
                "pwd >> /srv/out.log", "make lint 2> /srv/err.log &> /srv/all.log"):
        assert not checkspill.host_path(cmd), cmd
    suite = push.check_list(["python3 -m pytest -q --rootdir=/srv/proj tests/"])[0]
    assert not checkspill.check_spills(suite) and not checkspill.stays_here(suite)
    assert not checkspill.spillable(push.check_list(["make lint"]) + [suite]), "a suite naming one keeps the run here"
    out = cli._spill_to(push.check_list([test, scan]), "check-host-a")
    assert push.remote_host(out[0]) == "check-host-a" and push.remote_host(out[1]) == "" and push.where(out[1]) == ""


def test_a_check_kept_here_for_a_path_of_this_machine_says_why_in_the_run_log_and_status(env, tmp_path, monkeypatch):
    """A check running a script of this machine by absolute path (outside the checkout and the system folders,
    as a project's harness scanner: its Slurm jobs ended 127) runs here while the rest of its run spills, and
    the run's log says so and why; a run that cannot spill names each check that keeps it here and its reason
    for `ttp checks status` (checkspill.here_why)."""
    from ttp import checkspill, cli, push
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    here = (f"{tmp_path}/", f"{os.path.realpath(tmp_path)}/")   # _spill_setup counts the temp folder a system one
    monkeypatch.setattr(checkspill, "_SYSTEM_DIRS", tuple(d for d in checkspill._SYSTEM_DIRS
                                                        if d not in here and not here[0].startswith(d)))   # or tmp under /dev/shm
    scan = tmp_path / "harness-bin" / "scan"
    scan.parent.mkdir()
    scan.write_text("#!/bin/sh\necho scanned\n")
    scan.chmod(0o755)
    log = tmp_path / "where.log"
    test = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [test, str(scan)])
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", rec
    text = (run / "checks.log").read_text()
    assert f"{scan} runs here, not on check-host-a: it names a path of this machine" in text, text
    assert f"{test} runs here" not in text and "/ttp-check." in log.read_text()
    assert checkspill.why_here(push.check_list([str(scan)])[0]).startswith("it names a path of this machine")
    assert checkspill.why_here(push.check_list([test])[0]) == ""
    suite = "python3 -m pytest -q --rootdir=/srv/proj tests/"
    why = checkspill.here_why(push.check_list([suite, "make lint"]))
    assert why.startswith("its checks cannot spill (") and suite in why and "path of this machine" in why, why
    assert "make lint" not in why, "a check that may spill is not named"
    assert checkspill.here_why(push.check_list(["make lint", str(scan)])) == "", "a split run spills"
    for c, said in (("claude plugin validate . --strict", "agent CLI"), ({"run": "make lint", "host": "local"},
                    '"host": "local"'), ({"run": "make lint", "spill": False}, '"spill": false')):
        assert said in checkspill.why_here(push.check_list([c])[0]), c


def test_a_shell_c_script_running_a_test_suite_counts_as_a_test_suite():
    """checklane.run_class reads a shell's -c script as checkspill.needs_cli does: `sh -c 'pytest ... &&
    claude ...'` is a test suite, as the same chain is unwrapped."""
    from ttp import checklane
    for c in ("sh -c 'python3 -m pytest -q tests/a.py && claude plugin validate . --strict'",
              "env A=1 bash -lc 'cd x; pytest -q'", "nice sh -c \"sh -c 'make test'\""):
        assert checklane.run_class([c]) == checklane.SUITE, c
    assert checklane.run_class(["sh -c 'echo hi; ls'"]) == checklane.OTHER


def test_a_check_chaining_tests_and_the_claude_cli_never_sends_either_to_a_spill_host():
    """A single check that runs a test suite and then the claude CLI (`pytest ... && claude plugin validate
    ...`) never spills: it needs the CLI, and as a test suite it may not split off to run here, so its whole
    run stays on this machine and neither part ever runs on a spill host, nor is labelled with one."""
    from ttp import checkspill, cli, push
    test = "python3 -m pytest -q tests/a.py"
    for chain in (f"{test} && claude plugin validate plugins/x --strict",
                  f"{test}; claude plugin validate plugins/x --strict",
                  f"sh -c '{test} && claude plugin validate plugins/x --strict'"):
        for c in (push.check_list([chain])[0], chain):   # configured, or a plain command after `--`
            assert checkspill.needs_cli(c) and not checkspill.check_spills(c) and not checkspill.spillable([c]), chain
            assert not checkspill.stays_here(c) and not checkspill.spillable([test, c]), chain
            assert not checkspill.spillable(push.check_list(["make lint", test]) + [c]), chain
            # In a split run the chain still runs here, unlabelled, beside the spilled test.
            out = cli._spill_to([push.check_list([test])[0], c], "check-host-a")
            assert push.remote_host(out[0]) == "check-host-a" and push.remote_host(out[1]) == "", chain
            assert push.where(out[1]) == "" and str(out[1]) not in push.hosts_of(out), chain


def _split_run(env, tmp_path, monkeypatch, cli_rc: int, extra: bool = False, cli_check="claude plugin validate . --strict",
               sleep_s: int = 0):
    """A spilled run of a spilled check and a claude CLI check (a fake one here, sleeping `sleep_s`, then exiting
    `cli_rc`; given after `--` as a plain command with `extra`) whose spill host drops any script naming the
    claude command, as a host without it would fail it: (checks.json, checks.log, where the spilled check ran,
    where the claude check ran)."""
    import threading
    from ttp import cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    agent = tmp_path / "agentbin"
    agent.mkdir()
    ran, here = tmp_path / "spilled.log", tmp_path / "here.log"
    (agent / "claude").write_text(f"#!/bin/sh\npwd >> {here}\nsleep {sleep_s}\nexit {cli_rc}\n")
    (agent / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{agent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_FAIL", "claude plugin validate")
    p.set_config("delivery.push_checks", [f"pwd >> {ran}"] + ([] if extra else [cli_check]))
    lane = _spill_lane(p, repo)
    late = threading.Timer(20.0, lane.close)
    late.start()
    try:
        cli.main(["checks", "--fresh"] + (["--", cli_check] if extra else []))
    except SystemExit as e:   # a failing run exits 1
        assert e.code == 1 and (cli_rc or sleep_s), e.code
    finally:
        late.cancel()
        lane.close()
    return (json.loads((run / "checks.json").read_text()), (run / "checks.log").read_text(),
            ran.read_text().splitlines(), here.read_text().splitlines())


def test_a_split_run_spills_its_tests_and_runs_its_agent_cli_check_here_for_one_verdict(env, tmp_path, monkeypatch):
    """The lane here busy, the run spills: its plain check runs on the spill host and its claude CLI check
    (which that host lacks) runs on this machine in the same run, both in one passing verdict."""
    rec, text, ran, here = _split_run(env, tmp_path, monkeypatch, 0)
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", (rec, text)
    assert len(ran) == 1 and "/ttp-check." in ran[0], ran
    assert len(here) == 1 and "/ttp-check." not in here[0], here
    assert "did not pass on spill host" not in text, text
    # The log and the record name the host each check really ran on: the claude check, run here, none.
    assert f"$ pwd >> {tmp_path / 'spilled.log'} (on check-host-a)" in text, text
    assert "$ claude plugin validate . --strict\n" in text and "claude plugin validate . --strict (on" not in text, text
    assert list(rec["hosts"].values()) == ["check-host-a"] and not any("claude" in k for k in rec["hosts"]), rec


def test_a_split_runs_failing_agent_cli_check_here_fails_it_without_a_rerun(env, tmp_path, monkeypatch):
    """The check split off to run here fails: that failure is the run's verdict at once, never a spill host
    fault that reruns the checks."""
    rec, text, ran, here = _split_run(env, tmp_path, monkeypatch, 1)
    assert rec["passed"] is False and rec["spilled_to"] == "check-host-a", (rec, text)
    assert "did not pass on spill host" not in text and len(ran) == len(here) == 1, (text, ran, here)


def test_a_split_run_keeps_a_plain_agent_cli_command_after_the_dashes_here(env, tmp_path, monkeypatch):
    """The claude CLI check given after `--` (a plain command, no checks_host or wrap_cmd set) splits off to
    run here like a configured one: one passing verdict, never a crash on the check's spill fields."""
    rec, text, ran, here = _split_run(env, tmp_path, monkeypatch, 0, extra=True)
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", (rec, text)
    assert len(ran) == 1 and len(here) == 1 and "/ttp-check." not in here[0], (ran, here, text)


def test_a_split_off_check_timing_out_here_is_the_verdict_without_a_rerun(env, tmp_path, monkeypatch):
    """A check split off to run here stopped at its silence limit: that timeout is the run's verdict, never a
    spill host fault that reruns the checks (and puts the host off spilling)."""
    from ttp import checkwall
    monkeypatch.setattr(checkwall, "POLL_S", 0.2)
    hang = {"run": "claude plugin validate . --strict", "silent_s": 1}
    rec, text, ran, here = _split_run(env, tmp_path, monkeypatch, 0, cli_check=hang, sleep_s=4)
    assert rec["passed"] is False and rec["spilled_to"] == "check-host-a" and rec.get("timed_out"), (rec, text)
    assert "did not pass on spill host" not in text and len(ran) == len(here) == 1, (text, ran, here)


def test_a_suite_of_a_project_with_a_checks_host_spills_and_runs_on_the_spill_host():
    """delivery.checks_host (push.with_host's default_host) never keeps a check off the spill hosts: only the
    check's own host, or a spill host it already has, does. Spilled, it runs on the spill host, not the
    checks host; a check that names its own host, or one already spilled, stays where it is."""
    from ttp import checkspill, push
    suite = push.with_host(push.check_list(["python3 -m pytest -q tests", {"run": "make lint", "host": "check-host-b"}]),
                           "check-host-a")
    assert suite[0].default_host == "check-host-a" and push.remote_host(suite[0]) == "check-host-a"
    assert checkspill.check_spills(suite[0]) and not checkspill.check_spills(suite[1])
    assert checkspill.spillable(suite[:1]) and checkspill.spillable(suite)   # the host-pinned check runs beside it
    spilled = push.spill_to(suite[:1], "spill-host-a", None)
    assert push.remote_host(spilled[0]) == "spill-host-a" and not checkspill.check_spills(spilled[0])


def test_a_check_opts_in_to_spilling_and_runs_behind_its_remote_wrapper_there(env):
    """`spill: true` lets a check with host "local" spill, `false` keeps any check here; a check behind
    checks.wrap_cmd (a device wrapper) spills only with a remote wrapper (its own spill_wrap_cmd, else
    checks.spill_wrap_cmd), and spill_to runs it there behind that wrapper, never this machine's."""
    from ttp import checkspill, project, push
    c = push.check_list(["make t", {"run": "make a", "host": "local"}, {"run": "make b", "host": "local", "spill": True},
                         {"run": "make c", "spill": False}, {"run": "make d", "spill": True, "spill_wrap_cmd": "./w.sh -x"},
                         {"run": "make e", "spill": True, "cwd": "authoring"}])
    assert [checkspill.spillable(c[i:i + 1]) for i in range(6)] == [True, False, True, False, True, False]
    assert not checkspill.spillable(c) and checkspill.spillable([c[0], c[2], c[4]])
    assert not checkspill.spillable(push.check_list([{"run": "make x", "host": "check-host-a", "spill": True}]))
    assert checkspill.spillable(push.with_host(c[:1], "check-host-a")), "delivery.checks_host does not keep it there"
    assert checkspill.spillable(push.with_host(c[2:3], "check-host-a")), "host local beats checks_host"
    assert push.checks_of([{"run": "make b", "host": "local", "spill": True, "spill_wrap_cmd": "./w.sh -x"}]) == [
        {"run": "make b", "host": "local", "spill": True, "spill_wrap_cmd": "./w.sh -x"}]
    # A device-needing wrapper with no remote wrapper stays here; a remote wrapper lets it spill.
    wrapped = push.with_wrap(push.check_list(["make test"]), ("/opt/devwrap",))
    assert not checkspill.spillable(wrapped) and not checkspill.spillable(wrapped, {"checks": {"wrap_needs_device": True}})
    cfg = {"checks": {"spill_wrap_cmd": "tools/pool-wrap --cpu"}}
    assert checkspill.spillable(wrapped, cfg)
    assert checkspill.spillable(push.with_wrap(c[4:5], ("/opt/devwrap",))), "its own remote wrapper"
    for bad in ("a 'b", 3, ["x"]):   # a remote wrapper that does not parse never lets it spill
        assert not checkspill.spillable(wrapped, {"checks": {"spill_wrap_cmd": bad}}), bad
        assert any("checks.spill_wrap_cmd" in x for x in project.config_problems({"checks": {"spill_wrap_cmd": bad}})), bad
    assert not project.config_problems(cfg)
    for bad in ({"spill": "yes"}, {"spill_wrap_cmd": ""}, {"spill_wrap_cmd": 3}, {"spill_wrap_cmd": "a 'b"}):
        assert push.check_problems([{"run": "make t", **bad}]), bad
        assert not checkspill.spillable(push.check_list([{"run": "make t", "host": "local", **bad}])), bad
    # spill_to: each runs on the spill host behind its own wrapper, else the project's; host local too.
    t, b, d = push.spill_to([c[0], c[2], c[4]], "pool-a", cfg)
    assert [push.remote_host(x) for x in (t, b, d)] == ["pool-a"] * 3
    assert t.remote_wrap == ("tools/pool-wrap", "--cpu") and d.remote_wrap == ("./w.sh", "-x")
    assert b.config() == {"run": "make b", "host": "local", "spill": True}, "the record keeps the configured form"
    argv = push.check_argv(push.spill_to(wrapped, "pool-a", cfg)[0])
    assert json.loads(argv[argv.index("--wrap") + 1]) == ["tools/pool-wrap", "--cpu"] and argv[-1] == "make test"
    assert "/opt/devwrap" not in argv, "never this machine's wrapper there"
    assert "--wrap" not in push.check_argv(push.spill_to(c[:1], "pool-a", {})[0]), "no remote wrapper: none"


def test_a_remote_wrapper_missing_on_the_host_is_a_fault_and_the_command_never_runs_unwrapped(tmp_path):
    """--wrap: the command runs behind the wrapper (a path in the copy of the commit, or a name on the
    host's PATH); one missing or not executable there exits FAULT_RC without starting the command."""
    from ttp import remotecheck
    repo = _rc_repo(tmp_path)
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    (repo / "w.sh").write_text('#!/bin/sh\necho "wrapped by w.sh: $1"\nshift\nexec "$@"\n')
    (repo / "w.sh").chmod(0o755)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "w"], check=True)
    bin_, remote = _rc_fake_ssh(tmp_path)
    penv = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_REMOTE_TMP": str(remote)}
    ran = tmp_path / "ran"

    def go(wrap):
        r = subprocess.run(remotecheck.argv(f"echo ran >> {ran}; exit 3", "box1", {}, "", wrap), cwd=repo, env=penv,
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout
    rc, out = go(("./w.sh", "cpu"))
    assert rc == 3 and "wrapped by w.sh: cpu" in out and ran.read_text() == "ran\n", out
    ran.unlink()
    for wrap in (("./nope.sh",), ("tools/nope",), ("ttp-no-such-wrapper-xyz", "--cpu")):
        rc, out = go(wrap)
        assert rc == remotecheck.FAULT_RC and f"{remotecheck.SIG}box1" in out and "never unwrapped" in out, (wrap, out)
        assert not ran.exists(), wrap
    rc, out = go(("sh", "-c", 'echo "named: $0"; exec "$@"', "x"))
    assert rc == 3 and ran.exists(), out
    assert _rc_left(tmp_path) == []


def test_a_remote_wrapper_that_records_its_refusal_is_a_host_fault_and_a_plain_exit_75_still_fails(tmp_path):
    """A spill wrapper that refuses the run (a busy host) writes the file TTP_WRAP_REFUSED names and exits
    FAULT_RC: a host fault, so the check runs again elsewhere, never a failed check. Exit 75 without that
    file (from the wrapper or the check), the file with any other exit, or a check that writes it unwrapped
    keep today's reading: 75 as 1, other codes as they are; a real failure stays a failure."""
    from ttp import remotecheck
    repo = _rc_repo(tmp_path)
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    (repo / "busy.sh").write_text('#!/bin/sh\n[ -n "$REFUSE" ] || exec "$@"\necho "host busy" >&2\n'
                                  'case "$REFUSE" in mark*) : > "$TTP_WRAP_REFUSED";; esac\n'
                                  'case "$REFUSE" in *75) exit 75;; *) exit 9;; esac\n')
    (repo / "busy.sh").chmod(0o755)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "busy"], check=True)
    bin_, remote = _rc_fake_ssh(tmp_path)
    ran = tmp_path / "ran"

    def go(cmd, refuse="", wrap=("./busy.sh",)):
        penv = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_REMOTE_TMP": str(remote),
                "REFUSE": refuse}
        r = subprocess.run(remotecheck.argv(cmd, "box1", {}, "", wrap), cwd=repo, env=penv, capture_output=True,
                           text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr
    rc, out = go(f"echo ran >> {ran}", refuse="mark75")
    assert rc == remotecheck.FAULT_RC and f"{remotecheck.SIG}box1" in out and "refused" in out, out
    host, why = remotecheck.wrap_fault(out)   # #8107: the limit line status shows names the host and the setting
    assert host == "box1" and "'./busy.sh' refused the run there" in why and "checks.spill_wrap_cmd" in why, why
    assert "on box1" in out and "low priority" in out and out.count("(checks.nice)") == 1, out
    assert "host busy" in out and not ran.exists() and "ttp-remote-rc" not in out, out
    assert _rc_left(tmp_path) == []
    rc, out = go("exit 3")
    assert rc == 3 and remotecheck.SIG not in out, "a real failure behind the wrapper stays a failure"
    rc, out = go("exit 75")
    assert rc == 1 and "reported as 1" in out and remotecheck.SIG not in out, out
    rc, out = go("true", refuse="plain75")
    assert rc == 1 and remotecheck.SIG not in out, "the wrapper's own 75 without the file is today's 1"
    rc, out = go("true", refuse="mark9")
    assert rc == 9 and remotecheck.SIG not in out, "the file with another exit code is no refusal"
    rc, out = go('[ -n "$TTP_WRAP_REFUSED" ] && touch "$TTP_WRAP_REFUSED"; exit 75', wrap=())
    assert rc == 1 and remotecheck.SIG not in out, "unwrapped, there is no wrapper to refuse"
    assert _rc_left(tmp_path) == []


def test_a_host_local_check_that_opts_in_spills_behind_its_remote_wrapper_and_a_missing_one_reruns_here(
        env, tmp_path, monkeypatch):
    """A push check with host "local" and `spill: true` spills when the lane here stays busy and runs
    there behind its spill_wrap_cmd; with that wrapper missing on the host the spill is a host fault,
    never a verdict: the check runs again here and only that pass counts. Without `spill` it never spills."""
    import threading
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    (repo / "pool-wrap.sh").write_text('#!/bin/sh\necho pool-wrapped >> "$WRAP_LOG"\nexec "$@"\n')
    (repo / "pool-wrap.sh").chmod(0o755)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "wrap"], check=True)
    wlog, log = tmp_path / "wrap.log", tmp_path / "where.log"
    monkeypatch.setenv("WRAP_LOG", str(wlog))
    cmd = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [{"run": cmd, "host": "local", "spill": True,
                                           "spill_wrap_cmd": "./pool-wrap.sh"}])
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", rec
    assert "/ttp-check." in log.read_text() and wlog.read_text() == "pool-wrapped\n"
    assert "on check-host-a the checks run behind their spill wrapper ./pool-wrap.sh" in (run / "checks.log").read_text()
    # The remote wrapper missing there: a host fault, the run goes on here and its pass counts.
    log.unlink()
    wlog.unlink()
    p.set_config("delivery.push_checks", [{"run": cmd, "host": "local", "spill": True,
                                           "spill_wrap_cmd": "./missing-wrap.sh"}])
    lane = _spill_lane(p, repo)
    threading.Timer(1.5, lane.close).start()
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and "spilled_to" not in rec, rec
    text = (run / "checks.log").read_text()
    assert "missing-wrap.sh' is missing or not executable there" in text and "did not pass on spill host" in text, text
    # #8107 (wrap_refusal_in_status): status names the host and the wrapper's setting while the run goes on.
    lines = checkspill.limit_lines(p.name)
    assert any("spill host check-host-a: the spill wrapper './missing-wrap.sh' is missing" in x
               and "checks.spill_wrap_cmd" in x for x in lines), lines
    assert "check commands run at low priority, behind" in text and "(checks.nice)" in text, "nice_line_logged here"
    checkspill.note_limit(p.name, "")
    assert "/ttp-check." not in log.read_text().splitlines()[-1] and len(log.read_text().splitlines()) == 1
    assert not wlog.exists()
    # Without the opt-in a host-local check waits here.
    p.set_config("delivery.push_checks", [{"run": cmd, "host": "local", "spill_wrap_cmd": "./pool-wrap.sh"}])
    monkeypatch.setattr(checkspill, "pick", lambda *a, **k: pytest.fail("no spill host is tried"))
    lane = _spill_lane(p, repo)
    threading.Timer(1.0, lane.close).start()
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and "spilled_to" not in rec, rec


def test_spill_host_slots_follow_idle_cores_and_memory_and_the_probe_reads_its_load(monkeypatch):
    """#5893: a spill host's slots follow what it has idle now (checklane.host_slots), and checkspill.probe
    reads both its 3-field line (no load: default_slots less running) and its 4-field line (with load), each
    followed by a device holder scan that found none."""
    from ttp import checklane, checkspill
    assert checklane.host_slots(64, 200, 10, 0) == 6
    assert checklane.host_slots(64, 200, 60, 2) == 0, "a busy host fits no run"
    assert checklane.host_slots(64, 12, 0, 0) == 1, "memory bounds it"
    assert checklane.host_slots(64, 200, None, 1) == checklane.default_slots(64, 200) - 1, "no load read"
    out = {}
    monkeypatch.setattr(checkspill.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, out["line"] + "\nttp-spill-dev-end none\n", ""))
    gib = 1024 * 1024
    out["line"] = f"ttp-spill 64 {200 * gib} 1"
    assert checkspill.probe("h") == (checklane.default_slots(64, 200) - 1, "", "")
    out["line"] = f"ttp-spill 64 {200 * gib} 0 10.5"
    assert checkspill.probe("h") == (checklane.host_slots(64, 200, 10.5, 0), "", "")
    out["line"] = f"ttp-spill 64 {200 * gib} 0 -"
    assert checkspill.probe("h") == (checklane.default_slots(64, 200), "", ""), "an unread load: '-'"
    out["line"] = f"ttp-spill 64 {200 * gib} 2 60"
    n, why, _ = checkspill.probe("h")
    assert n == 0 and "load 60, of 64 cores" in why and "0.8 x" in why, why
    out["line"] = "ttp-spill 64 x 2"
    assert checkspill.probe("h")[0] == 0 and "probe failed" in checkspill.probe("h")[1]



def _spill_hosts_read(monkeypatch, loads: dict, cores=64, mem_gib=200):
    """The spill probe over ssh answers per host from `loads` ({alias: 1-minute load, or None: ssh cannot
    reach it}); every other command runs as before. Returns the probes run per host."""
    from ttp import checkspill, remotecheck
    real, real_ssh, seen = subprocess.run, remotecheck._ssh, {}
    monkeypatch.setattr(remotecheck, "_ssh", lambda alias, script, *a, **k: (
        ["fake-spill-probe", alias] if script == checkspill._probe_script() else real_ssh(alias, script, *a, **k)))

    def run(cmd, *a, **k):
        if not (isinstance(cmd, list) and cmd[:1] == ["fake-spill-probe"]):
            return real(cmd, *a, **k)
        host = cmd[1]
        seen[host] = seen.get(host, 0) + 1
        if loads[host] is None:
            return subprocess.CompletedProcess(cmd, 255, "", f"ssh: connect to host {host}: No route to host\n")
        return subprocess.CompletedProcess(
            cmd, 0, f"ttp-spill {cores} {mem_gib * 1024 * 1024} {checkspill._ours(host)} {loads[host]}\n"
            "ttp-spill-dev-end none\n", "")   # a claim's run has its folder there: it counts as running
    monkeypatch.setattr(checkspill.subprocess, "run", run)
    return seen


def test_spill_never_starts_a_check_on_a_host_whose_load_is_near_its_cores(env, tmp_path, monkeypatch):
    """#8016 (user msg #2469: runs kept choking one box): a host whose 1-minute load is at or above
    checkspill.LOAD_NEAR x its cores gets no new check, whatever its slots; this machine's runs started there
    within LOAD_LAG_S count as load already (the 1-minute load shows them late), so a burst of picks fills a
    host only to the gate and then waits, never piling on."""
    from ttp import checkspill
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    _spill_hosts_read(monkeypatch, {"check-host-a": 52, "check-host-b": 4})
    n, why, _ = checkspill.probe("check-host-a")
    assert n == 0 and "load 52, of 64 cores" in why and "0.8 x" in why, why
    told, got = [], []
    try:
        while len(got) < 20 and (c := checkspill.pick(p, p.config(), told.append, "pytest -q")) is not None:
            got.append(c)
        assert got and {c.host for c in got} == {"check-host-b"}, told
        assert len(got) < 20, "the gate held the burst"
        assert "check-host-a (its load is near its cores (load 52, of 64 cores" in told[-1], told[-1]
        assert checkspill._pending_cores("check-host-b", 64) > 0
        later = time.time() + checkspill.LOAD_LAG_S + 1
        assert checkspill._pending_cores("check-host-b", 64, now=later) == 0, "the load shows them by then"
    finally:
        for c in got:
            c.close()


def test_an_unreachable_spill_host_backs_off_and_its_share_spreads_over_the_others(env, tmp_path, monkeypatch):
    """#8016: a host whose probe fails backs off (60 s, doubling to 900 s; a state file every project here
    reads) and is tried last until then, never probed at each pick; the runs it would take spread over the
    other hosts by their free cores, never all to one."""
    from ttp import checkspill
    names = ["down", "check-host-a", "check-host-b", "check-host-c"]
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, names, charter=names)
    probes = _spill_hosts_read(monkeypatch, {"down": None, "check-host-a": 0, "check-host-b": 0, "check-host-c": 0})
    told, got = [], []
    try:
        for _ in range(6):
            got.append(checkspill.pick(p, p.config(), told.append, "pytest -q"))
        hosts = [c.host for c in got if c is not None]
        assert len(hosts) == 6 and {h: hosts.count(h) for h in set(hosts)} == \
            {"check-host-a": 2, "check-host-b": 2, "check-host-c": 2}, (hosts, told)
        assert probes["down"] == 1, probes
        why = checkspill.backed_off("down")
        assert "ssh cannot reach it" in why and "backing off after 1 failed probe" in why, why
        assert checkspill.probe("down")[0] == 0
        st = checkspill.host_state("down")
        assert st["fails"] == 2 and 100 < st["until"] - time.time() <= 2 * checkspill.BACKOFF_S, st
        assert checkspill.backed_off("down", now=time.time() + checkspill.BACKOFF_MAX_S + 1) == ""
        # A probe that reads again ends the back-off.
        _spill_hosts_read(monkeypatch, {"down": 0})
        assert checkspill.probe("down")[0] > 0 and checkspill.backed_off("down") == ""
    finally:
        for c in got:
            c and c.close()

# The account's Slurm overflow (slurmcheck): fake sbatch, squeue, sacct and scancel run through the fake
# ssh. FAKE_SLURM=<dir> holds their logs and job states; FAKE_SLURM_MODE: run (the job runs at once and
# completes), hold (it stays RUNNING until scancel), refuse (sbatch exits 1), startfail (every job ends
# FAILED, exit code 1:0, without running or output), nodefail1 (the first job
# ends NODE_FAIL without running, later ones run), localdisk (the workdir is on the submit host's own
# disk: the node cannot read it, so the probe exits 3 and a job fails at once), probehide (the node's
# probe answer never shows on the submit host). Probe jobs (--job-name=ttp-probe) log to probe.log
# with ids from 901. FAKE_SLURM_NOACCT=1: sacct answers nothing (no accounting), scontrol does.
# FAKE_SLURM_RC_LAG=<s>: a job's rc file shows that long after the job ended (NFS); lag.pids lists the
# background movers, which a test waits out so none outlives it; "never": it never shows.
_SLURM_FAKES = {
    "sbatch": r'''#!/bin/sh
d=$FAKE_SLURM
case "$*" in *--job-name=ttp-probe*) probe=1; log=probe.log; first=900;; *) probe=; log=sbatch.log; first=100;; esac
echo "$*" >> "$d/$log"
[ "$FAKE_SLURM_MODE" = refuse ] && { echo "sbatch: error: invalid partition specified" >&2; exit 1; }
n=$(( $(cat "$d/next$probe" 2>/dev/null || echo $first) + 1 )); echo $n > "$d/next$probe"
out= ; dir= ; for a in "$@"; do case "$a" in --output=*) out=${a#--output=};; --chdir=*) dir=${a#--chdir=};; esac; script=$a; done
if [ "$FAKE_SLURM_MODE" = localdisk ]; then echo FAILED > "$d/state.$n"; [ -n "$probe" ] && echo 3:0 > "$d/code.$n"
elif [ -n "$probe" ]; then (cd "$dir" && sh "$script" > "$out" 2>&1); echo COMPLETED > "$d/state.$n"
  [ "$FAKE_SLURM_MODE" = probehide ] && rm -f "$(dirname "$script")/probe.out"
elif [ "$FAKE_SLURM_MODE" = hold ]; then echo RUNNING > "$d/state.$n"
elif [ "$FAKE_SLURM_MODE" = startfail ]; then echo FAILED > "$d/state.$n"; echo "1:0" > "$d/code.$n"
elif [ "$FAKE_SLURM_MODE" = nodefail1 ] && [ ! -e "$d/failed" ]; then touch "$d/failed"; echo NODE_FAIL > "$d/state.$n"
else (cd "$dir" && sh "$script" > "$out" 2>&1); echo COMPLETED > "$d/state.$n"
  if [ "$FAKE_SLURM_RC_LAG" = never ] && [ -e "$dir/rc" ]; then
    mv "$dir/rc" "$dir/rc.late"
  elif [ -n "$FAKE_SLURM_RC_LAG" ] && [ -e "$dir/rc" ]; then
    mv "$dir/rc" "$dir/rc.late"; (sleep "$FAKE_SLURM_RC_LAG"; mv "$dir/rc.late" "$dir/rc") </dev/null >/dev/null 2>&1 &
    echo $! >> "$d/lag.pids"
  fi
fi
echo "$n"
''',
    "squeue": r'''#!/bin/sh
for a in "$@"; do [ "$prev" = -j ] && j=$a; prev=$a; done
s=$(cat "$FAKE_SLURM/state.$j" 2>/dev/null)
case "$s" in PENDING|RUNNING) echo "$s";; esac
''',
    "sacct": r'''#!/bin/sh
[ -n "$FAKE_SLURM_NOACCT" ] && { echo "sacct: error: Slurm accounting storage is disabled" >&2; exit 1; }
for a in "$@"; do [ "$prev" = -j ] && j=$a; prev=$a; done
echo "$(cat "$FAKE_SLURM/state.$j" 2>/dev/null)|$(cat "$FAKE_SLURM/code.$j" 2>/dev/null || echo 0:0)|"
''',
    "scontrol": r'''#!/bin/sh
echo "$*" >> "$FAKE_SLURM/scontrol.log"
s=$(cat "$FAKE_SLURM/state.$3" 2>/dev/null) || { echo "slurm_load_jobs error: Invalid job id specified" >&2; exit 1; }
printf 'JobId=%s JobName=ttp-check\n   JobState=%s Reason=None Dependency=(null)\n   ExitCode=%s\n' "$3" "$s" \
  "$(cat "$FAKE_SLURM/code.$3" 2>/dev/null || echo 0:0)"
''',
    "scancel": r'''#!/bin/sh
echo "$1" >> "$FAKE_SLURM/scancel.log"
echo CANCELLED > "$FAKE_SLURM/state.$1"
''',
}


def _wait_out_pids(path, timeout=30):
    """Wait for the fakes' background processes listed in path to end, so none outlives the test."""
    deadline = time.monotonic() + timeout
    for pid in map(int, path.read_text().split() if path.exists() else []):
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"the fake's background process {pid} still runs")


def _slurm_fakes(tmp_path, monkeypatch=None, mode="run"):
    """The fake ssh and Slurm tools on a bin folder: (environment for them, the fakes' state folder)."""
    bin_, remote = _rc_fake_ssh(tmp_path)
    d = tmp_path / "slurm"
    d.mkdir(exist_ok=True)
    for name, text in _SLURM_FAKES.items():
        (bin_ / name).write_text(text)
        (bin_ / name).chmod(0o755)
    fake = {"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_REMOTE_TMP": str(remote),
            "FAKE_SLURM": str(d), "FAKE_SLURM_MODE": mode, "TTP_SLURM_POLL_S": "0.1"}
    for k, v in fake.items():
        if monkeypatch is not None:
            monkeypatch.setenv(k, v)
    return fake, d


_SLURM_SET = {"host": "cluster-login", "partition": "cpu-part", "max_time_s": 1800}


def _slurm_run(tmp_path, repo, cmd, fake, wait=True, minutes=0, **more):
    from ttp import slurmcheck
    s = slurmcheck.settings_of({**_SLURM_SET, "workdir": str(tmp_path / "remote"), **more})
    proc = subprocess.Popen(slurmcheck.argv(cmd, s, {"GREETING": "hi"}, "", minutes), cwd=repo,
                            env={**os.environ, **fake}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not wait:
        return proc
    out = proc.communicate(timeout=60)[0]
    return proc.returncode, out


def test_slurm_overflow_is_off_by_default_validated_and_only_the_user_sets_it(env, capsys, monkeypatch):
    from ttp import cli, project, slurmcheck
    assert slurmcheck.settings() is None and slurmcheck.enabled({}) is None
    assert slurmcheck.pick(None, {}, pytest.fail) is None, "unset: nothing is tried"
    s = slurmcheck.settings_of({"host": "cluster-login", "partition": "cpu"})
    assert s == {"host": "cluster-login", "partition": "cpu", "workdir": slurmcheck.DEFAULT_WORKDIR,
                 "max_jobs": 2, "margin_s": 600, "max_time_s": 7200, "max_pending_s": 1800, "rc_grace_s": 90}
    for bad in ("cluster-login", {"host": "a b", "partition": "cpu"}, {"host": "h", "partition": "c p"},
                {"host": "h", "partition": "cpu", "workdir": "rel/dir"}, {"host": "h", "partition": "cpu", "workdir": "/a/../b"},
                {"host": "h", "partition": "cpu", "max_jobs": 0}, {"host": "h", "partition": "cpu", "gres": "x"}):
        with pytest.raises(ValueError):
            slurmcheck.settings_of(bad)
    # --time: the run's median plus the margin, capped; the cap before any median is known.
    assert slurmcheck.time_min(None, s) == 120 and slurmcheck.time_min(300, s) == 15
    assert slurmcheck.time_min(10 ** 6, s) == 120
    value = '{"host": "cluster-login", "partition": "cpu"}'
    cli.main(["config", "--account", slurmcheck.KEY, value])
    assert slurmcheck.settings()["partition"] == "cpu"
    with pytest.raises(SystemExit):
        cli.main(["config", "--account", slurmcheck.KEY, '{"host": "cluster-login"}'])
    assert "partition" in capsys.readouterr().err
    monkeypatch.setenv("TTP_RUN_ID", "7")
    with pytest.raises(SystemExit) as e:
        cli.main(["config", "--account", slurmcheck.KEY, '{"host": "other", "partition": "cpu"}'])
    assert e.value.code == 2 and "only the user may change" in capsys.readouterr().err
    assert slurmcheck.enabled({"delivery": {"checks_spill_hosts": []}}) is None, "an own [] keeps runs here"
    cli.main(["config", "--account", slurmcheck.KEY, ""])
    assert slurmcheck.settings() is None and "slurm_overflow" not in (project.load_account_settings().get("checks") or {})


def test_a_run_may_clear_the_slurm_overflow_to_off_logged_but_sets_no_user_only_key(env, capsys, monkeypatch):
    """In a run, `ttp config --account checks.slurm_overflow {}` (its off value) is a clear: it only reduces
    reach, so it is taken and logged with the run, task, old and new values; a value that is not off, and
    every other user-only key (spend caps, the write fence), still exit 2. Outside a run nothing changed."""
    from ttp import accountset, cli, project, slurmcheck
    value = {"host": "cluster-login", "partition": "cpu"}
    cli.main(["config", "--account", slurmcheck.KEY, json.dumps(value)])
    assert accountset.last(slurmcheck.KEY)["by"] == "user" and accountset.last(slurmcheck.KEY)["new"] == value
    capsys.readouterr()
    monkeypatch.setenv("TTP_RUN_ID", "7")
    monkeypatch.setenv("TTP_TASK_ID", "42")
    for bad in ('{"host": "other", "partition": "cpu"}', "[]", "null", "0"):
        with pytest.raises(SystemExit) as e:
            cli.main(["config", "--account", slurmcheck.KEY, bad])
        assert e.value.code == 2 and "only the user may change" in capsys.readouterr().err, bad
    for key, v in (("budget.global_daily_usd", ""), ("budget.global_daily_usd", "5"), ("budget.daily_usd", ""),
                   ("providers.claude.write_fence", ""), ("providers.claude.write_fence", "off")):
        with pytest.raises(SystemExit) as e:
            cli.main(["config", "--account", key, v])
        assert e.value.code == 2 and "only the user may change" in capsys.readouterr().err, key
    assert slurmcheck.settings()["host"] == "cluster-login", "nothing a refused call changed"
    cli.main(["config", "--account", slurmcheck.KEY, "{ }"])
    out = capsys.readouterr().out
    assert slurmcheck.settings() is None and "was" in out and "cluster-login" in out and "undo:" in out, out
    rec = accountset.last(slurmcheck.KEY)
    assert rec["new"] == {} and rec["old"] == value and rec["by"] == "worker", rec
    assert str(rec["task"]) == "42" and str(rec["run"]) == "7" and rec["ts"] and "cluster-login" in rec["undo"], rec
    # Outside a run: the user sets and clears it as before.
    monkeypatch.delenv("TTP_RUN_ID")
    monkeypatch.delenv("TTP_TASK_ID")
    cli.main(["config", "--account", slurmcheck.KEY, json.dumps(value)])
    assert slurmcheck.settings()["host"] == "cluster-login" and accountset.last(slurmcheck.KEY)["by"] == "user"
    cli.main(["config", "--account", "budget.global_daily_usd", "5"])
    assert project.load_account_settings()["budget"]["global_daily_usd"] in (5, 5.0, "5")


def test_the_slurm_breaker_trips_after_three_start_deaths_in_a_row_and_only_the_user_re_arms_it(env, capsys, monkeypatch):
    from ttp import cli, project, slurmcheck
    project.set_account_setting(slurmcheck.KEY, dict(_SLURM_SET))
    died = lambda n: {"job": str(n), "state": "FAILED", "code": "1:0", "elapsed_s": 0}
    assert slurmcheck._breaker(died(1)) is None and slurmcheck._breaker(died(2)) is None
    assert slurmcheck._breaker(None) is None, "a job that ran resets the count"
    assert slurmcheck._breaker(died(3)) is None and slurmcheck._breaker(died(4)) is None and not slurmcheck.tripped()
    trip = slurmcheck._breaker(died(5))
    assert trip and trip["count"] == 3 and [j["job"] for j in trip["jobs"]] == ["3", "4", "5"], trip
    assert slurmcheck.tripped()["tripped"] and "job 5 FAILED, exit code 1:0" in slurmcheck.trip_why(trip)
    assert slurmcheck._breaker(None) is None and slurmcheck.tripped(), "a late job's end re-arms nothing"
    told = []
    assert slurmcheck.pick(None, {}, told.append) is None and "paused itself" in told[-1], told
    assert "paused itself" in slurmcheck.paused_line() and "re-arms it" in slurmcheck.paused_line()
    assert slurmcheck.alert_due("a") and slurmcheck.alert_due("a") is None and slurmcheck.alert_due("b") is None
    # The user setting the same value again re-arms it (a new log line, whatever its stamp).
    cli.main(["config", "--account", slurmcheck.KEY, json.dumps(_SLURM_SET)])
    assert slurmcheck.tripped() is None and slurmcheck.paused_line() == ""
    assert slurmcheck._breaker(died(6)) is None and slurmcheck._breaker(died(7)) is None
    assert slurmcheck._breaker(died(8)), "counted afresh"
    # A changed value re-arms it too.
    project.set_account_setting(slurmcheck.KEY, {**_SLURM_SET, "max_jobs": 1})
    assert slurmcheck.tripped() is None and slurmcheck._breaker(died(9)) is None


def test_the_slurm_breaker_tells_a_re_arm_by_the_log_line_not_by_comparing_clocks(env, capsys):
    """The settings log stamps a change rounded to the millisecond, so a breaker record begun within half
    a millisecond after a re-arm could read as older than it and start over on every death (never tripping
    again); and a trip stamped later than the user's setting (a clock step) must still be re-armed by it.
    The record keeps the log line it began under: only a new line for the key re-arms it."""
    from ttp import accountset, cli, project, slurmcheck
    project.set_account_setting(slurmcheck.KEY, dict(_SLURM_SET))
    died = lambda n: {"job": str(n), "state": "FAILED", "code": "1:0", "elapsed_s": 0}
    ahead = time.time() + 3600   # the breaker's clock well past any log stamp
    assert [slurmcheck._breaker(died(n), now=ahead + n) for n in (1, 2)] == [None, None]
    assert slurmcheck._breaker(died(3), now=ahead + 3) and slurmcheck.tripped()
    cli.main(["config", "--account", slurmcheck.KEY, json.dumps(_SLURM_SET)])
    assert slurmcheck.tripped() is None, "the user's setting re-arms a trip stamped after it"
    ts = float(accountset.last(slurmcheck.KEY)["ts"])
    early = ts - 0.0004   # the log rounded its stamp up past these ends
    assert slurmcheck._breaker(died(4), now=early) is None and slurmcheck._breaker(died(5), now=early) is None
    trip = slurmcheck._breaker(died(6), now=early)
    assert trip and [j["job"] for j in trip["jobs"]] == ["4", "5", "6"], "counted, not started over each time"
    # A record kept from before it named its log line: by the line's stamp, as then.
    path = slurmcheck.slurm_dir() / slurmcheck.BREAKER
    old = {k: v for k, v in json.loads(path.read_text()).items() if k != "set"}
    path.write_text(json.dumps({**old, "since": ts + 1}))
    assert slurmcheck.tripped()
    path.write_text(json.dumps({**old, "since": ts - 1}))
    assert slurmcheck.tripped() is None
    capsys.readouterr()


def test_overflow_jobs_dying_at_start_pause_it_with_one_alert_and_the_runs_stay_local_never_failed(
        env, tmp_path, monkeypatch, capsys):
    """startfail: every job ends FAILED with no output. The first run's job and its one resubmit die (2),
    the run's checks run again here and pass; the second run's job is the third death: the overflow trips,
    that run passes here too; the third run submits nothing. One alert, in ttp status, cleared on re-arm."""
    import threading
    from ttp import alerts, cli, project, slurmcheck
    from ttp.daemon import Daemon
    p, repo, run, d, log, cmd = _slurm_overflow_setup(env, tmp_path, monkeypatch, ["down"])
    monkeypatch.setenv("FAKE_SLURM_MODE", "startfail")
    sbatches = []
    for n in range(3):
        lane = _spill_lane(p, repo)
        threading.Timer(1.5, lane.close).start()
        cli.main(["checks", "--fresh"])
        rec = json.loads((run / "checks.json").read_text())
        text = (run / "checks.log").read_text()
        assert rec["passed"] is True and "spilled_to" not in rec, (n, rec, text)
        sbatches.append(len((d / "sbatch.log").read_text().splitlines()))
        if n == 1:
            assert "paused itself" in text and "running them again on this machine" in text, text
        if n == 2:
            assert "paused itself" in text, text
    assert sbatches == [2, 3, 3], sbatches
    trip = slurmcheck.tripped()
    assert trip and trip["count"] == 3 and [j["state"] for j in trip["jobs"]] == ["FAILED"] * 3, trip
    assert all(j["code"] == "1:0" for j in trip["jobs"]) and trip["jobs"][-1]["job"] == "103", trip
    dm = Daemon(p.base)
    for _ in range(3):
        dm.alert_slurm_paused()
    (msg,) = p.db.q("SELECT * FROM messages WHERE ref=?", (slurmcheck.ALERT_KEY,))
    assert msg["severity"] == "normal" and "paused itself" in msg["text"] and "local queue" in msg["text"], msg
    assert alerts.tracked("alert", "normal", slurmcheck.ALERT_KEY) and not alerts.sweep(p.db)
    capsys.readouterr()
    cli.main(["status"])
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "status"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert out.count("Slurm checks overflow paused itself") == 2 and "runs stay on the local queue" in out, out
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "status", "--json"])
    assert e.value.code == 0
    assert json.loads(capsys.readouterr().out)["slurm_paused"]["count"] == 3
    project.set_account_setting(slurmcheck.KEY, {**slurmcheck.settings(), "max_jobs": 1})   # the user's new value
    (ep,) = alerts.sweep(p.db)
    assert ep["key"] == slurmcheck.ALERT_KEY and slurmcheck.tripped() is None
    dm.alert_slurm_paused()
    assert len(p.db.q("SELECT id FROM messages WHERE ref=? AND kind='alert'", (slurmcheck.ALERT_KEY,))) == 1


@pytest.mark.parametrize("run_env", ["TTP_RUN_ID", "TTP_RUN_DIR", "TTP_NOTE_RUN_DIR"])
@pytest.mark.parametrize("task", ["42", ""])   # a worker's run, or a coordinator's (the session's own shell)
@pytest.mark.parametrize("argv_tail", [["sandbox.checks", "true"], ["sandbox.checks", "1"], ["sandbox.checks", "9"],
                                       ["sandbox.checks", "enabled"], ["sandbox", json.dumps({"checks": True})]])
def test_a_run_cannot_turn_the_checks_sandbox_on(env, capsys, monkeypatch, run_env, task, argv_tail):
    """sandbox.checks (the checks' PID namespace) is user-only, like checks.slurm_overflow: a `ttp config`
    inside any run, a worker's or a coordinator's, refuses turning it on, directly or through its parent;
    the message says only the user's own word does."""
    from ttp import cli
    p = make(env)
    monkeypatch.setenv(run_env, "7")
    if task:
        monkeypatch.setenv("TTP_TASK", task)
    before = p.raw_config()
    with pytest.raises(SystemExit) as e:
        cli.main(["config", "demo", *argv_tail])
    err = capsys.readouterr().err
    assert e.value.code == 2 and "only the user may change" in err and "user's own word" in err, err
    assert p.raw_config() == before and p.config()["sandbox"]["checks"] is False


@pytest.mark.parametrize("argv_tail", [["sandbox.checks", "false"], ["sandbox.checks", ""],
                                       ["sandbox", json.dumps({"fence_freed_commands": []})]])
def test_a_run_may_turn_the_checks_sandbox_off_and_the_user_turns_it_on(env, capsys, monkeypatch, argv_tail):
    """The off value (false, unset, dropped through the parent) follows the user's unsandboxed-runs order:
    a run may write it. The user's own `ttp config` (no run) turns it on."""
    from ttp import cli
    p = make(env)
    cli.main(["config", "demo", "sandbox.checks", "true"])
    assert p.config()["sandbox"]["checks"] is True
    monkeypatch.setenv("TTP_RUN_ID", "7")
    monkeypatch.setenv("TTP_TASK", "42")
    cli.main(["config", "demo", *argv_tail])
    assert p.config()["sandbox"]["checks"] is False
    cli.main(["config", "demo", "sandbox.checks", "false"])   # already off: a no-op write stays allowed


def test_the_coordinator_never_turns_the_checks_sandbox_on(env, capsys):
    """A coordinator config_set of sandbox.checks on is refused with the reason, in a turn carrying a user
    message too; false and unset are applied. `ttp config --chat-settable` says the same."""
    from ttp import cli, coordinator as coord
    p = make(env)
    for value in (True, "true", "on", 1):
        for user_turn in (False, True):
            out = coord.apply(p, [{"type": "config_set", "key": "sandbox.checks", "value": value}], user_turn=user_turn)
            assert any("only the user may change" in x and "user's own word" in x for x in out), (value, out)
            assert p.config()["sandbox"]["checks"] is False
    p.set_config("sandbox.checks", True)
    assert coord.apply(p, [{"type": "config_set", "key": "sandbox.checks", "value": False}]) == []
    assert p.raw_config()["sandbox"]["checks"] is False
    p.set_config("sandbox.checks", True)
    assert coord.apply(p, [{"type": "config_set", "key": "sandbox.checks", "value": ""}]) == []
    assert p.config()["sandbox"]["checks"] is False
    for args in (["sandbox.checks"], ["sandbox.checks", "true"]):
        with pytest.raises(SystemExit) as e:
            cli.main(["config", "--chat-settable", *args])
        out = capsys.readouterr().out
        assert e.value.code == 1 and out.startswith("refused: sandbox.checks") and "user's own word" in out, out
    cli.main(["config", "--chat-settable", "sandbox.checks", "false"])
    assert "sandbox.checks: accepted as false" in capsys.readouterr().out
    assert coord.SANDBOX_KEYS >= {"sandbox.checks", *(f"providers.{n}.write_fence" for n in ("claude", "codex", "cursor", "chat"))}


def test_a_slurm_job_runs_the_check_on_a_copy_of_head_as_one_cpu_job_with_its_time_and_is_logged(env, tmp_path):
    from ttp import slurmcheck
    repo = _rc_repo(tmp_path)
    fake, d = _slurm_fakes(tmp_path)
    (repo / "a.txt").write_text("uncommitted\n")
    rc, out = _slurm_run(tmp_path, repo, 'git status --porcelain; cat a.txt; echo "$GREETING from the job"', fake,
                         minutes=17)
    assert rc == 0 and "a\nhi from the job" in out and "as Slurm job 101" in out, out
    sbatch = (d / "sbatch.log").read_text()
    assert "--partition=cpu-part --time=17 --nodes=1 --ntasks=1" in sbatch and "gres" not in sbatch, sbatch
    assert _rc_left(tmp_path) == [] and not (d / "scancel.log").exists()
    rec = slurmcheck.jobs()[-1]
    assert rec["job"] == "101" and rec["state"] == "COMPLETED" and rec["rc"] == 0 and rec["submit"] <= rec["end"], rec
    assert {"project", "task", "check", "head"} <= set(rec)
    assert not list(slurmcheck.slurm_dir().glob("job.*.json")), "the live record goes with the run"
    rc, out = _slurm_run(tmp_path, repo, "exit 3", fake)
    assert rc == 3 and slurmcheck.jobs()[-1]["rc"] == 3, out
    assert "--time=30 " in (d / "sbatch.log").read_text().splitlines()[-1], "max_time_s with no median"


def test_a_slurm_job_logs_the_node_it_ran_on_for_a_cluster_without_accounting(env, tmp_path):
    """jobs.jsonl names the node each job ran on (sacct may be off and scontrol forgets a job), read from the
    `node` file the job writes first."""
    from ttp import slurmcheck
    repo = _rc_repo(tmp_path)
    fake, d = _slurm_fakes(tmp_path)
    fake = {**fake, "SLURMD_NODENAME": "cpu-node-07", "FAKE_SLURM_NOACCT": "1"}
    rc, out = _slurm_run(tmp_path, repo, "true", fake)
    assert rc == 0 and slurmcheck.jobs()[-1].get("node") == "cpu-node-07", (out, slurmcheck.jobs()[-1])
    assert "node" in slurmcheck.job_script("/q", "", "true")


def test_a_slurm_job_the_cluster_ends_runs_again_once_and_a_refused_one_is_a_fault(env, tmp_path):
    from ttp import remotecheck, slurmcheck
    repo = _rc_repo(tmp_path)
    fake, d = _slurm_fakes(tmp_path, mode="nodefail1")
    rc, out = _slurm_run(tmp_path, repo, "echo ran", fake)
    assert rc == 0 and "submitting it again once" in out and "ran" in out, out
    assert len((d / "sbatch.log").read_text().splitlines()) == 2
    assert [j["state"] for j in slurmcheck.jobs()] == ["NODE_FAIL", "COMPLETED"]
    rc, out = _slurm_run(tmp_path, repo, "echo ran", {**fake, "FAKE_SLURM_MODE": "refuse"})
    assert rc == remotecheck.FAULT_RC and "sbatch refused the job" in out and remotecheck.SIG in out, out
    assert _rc_left(tmp_path) == []
    s = slurmcheck.settings_of({**_SLURM_SET, "host": "down"})
    proc = subprocess.run(slurmcheck.argv("echo ran", s), cwd=repo, env={**os.environ, **fake}, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == remotecheck.FAULT_RC and not (d / "scancel.log").exists(), proc.stdout


def test_a_stopped_slurm_check_cancels_its_job_and_leaves_nothing(env, tmp_path):
    from ttp import slurmcheck
    repo = _rc_repo(tmp_path)
    fake, d = _slurm_fakes(tmp_path, mode="hold")
    proc = _slurm_run(tmp_path, repo, "sleep 60", fake, wait=False)
    deadline = time.time() + 30
    while time.time() < deadline and not any(  # past the workdir probe's job: the check's own is sent
            slurmcheck._read(f).get("job") == "101" for f in slurmcheck.slurm_dir().glob("job.*.json")):
        time.sleep(0.1)
    assert (d / "sbatch.log").exists()
    proc.send_signal(signal.SIGTERM)
    out = proc.communicate(timeout=30)[0]
    assert proc.returncode == 143 and "cancelled job 101" in out, out
    assert (d / "scancel.log").read_text().split() == ["101"]
    assert _rc_left(tmp_path) == [] and not list(slurmcheck.slurm_dir().glob("job.*.json"))
    assert slurmcheck.jobs()[-1]["state"] == "CANCELLED"


def test_a_slurm_job_whose_run_died_is_cancelled_by_the_next_pick(env, tmp_path, monkeypatch):
    from ttp import slurmcheck
    fake, d = _slurm_fakes(tmp_path, monkeypatch)
    dead = subprocess.Popen(["true"])
    dead.wait()
    slurmcheck.slurm_dir().mkdir(parents=True, exist_ok=True)
    (slurmcheck.slurm_dir() / f"job.{dead.pid}.json").write_text(json.dumps(
        {"pid": dead.pid, "job": "55", "host": "cluster-login", "project": "x"}))
    assert slurmcheck.reap() == ["55"] and (d / "scancel.log").read_text().split() == ["55"]
    assert slurmcheck.jobs()[-1]["state"] == "ORPHANED"


def test_a_slurm_job_on_a_cluster_without_accounting_reads_scontrol_and_waits_out_a_late_rc_file(env, tmp_path):
    # No accounting (sacct answers nothing): the job's state comes from scontrol, and an rc file that
    # shows late (NFS) is still read within rc_grace_s; only past it is the job a cluster end.
    from ttp import remotecheck
    repo = _rc_repo(tmp_path)
    fake, d = _slurm_fakes(tmp_path)
    fake = {**fake, "FAKE_SLURM_NOACCT": "1", "FAKE_SLURM_RC_LAG": "1"}
    rc, out = _slurm_run(tmp_path, repo, "exit 5", fake, rc_grace_s=30)
    assert rc == 5 and "submitting it again" not in out, out
    assert "show job 101" in (d / "scontrol.log").read_text()
    assert len((d / "sbatch.log").read_text().splitlines()) == 1
    # Past the grace: an rc file that never shows, not a lag a slow poll under load could outwait.
    rc, out = _slurm_run(tmp_path, repo, "echo ran", {**fake, "FAKE_SLURM_RC_LAG": "never"}, rc_grace_s=1)
    assert rc == remotecheck.FAULT_RC and "submitting it again once" in out and "ended COMPLETED" in out, out
    assert len((d / "sbatch.log").read_text().splitlines()) == 3
    _wait_out_pids(d / "lag.pids")
    # A node fault scontrol names is a cluster end at once, never waited out: sent again once, then it runs.
    rc, out = _slurm_run(tmp_path, repo, "echo ran", {**fake, "FAKE_SLURM_MODE": "nodefail1", "FAKE_SLURM_RC_LAG": ""},
                         rc_grace_s=600)
    assert rc == 0 and "(cluster: it ended NODE_FAIL" in out and "ran" in out, out


def _slurm_clock_fakes(monkeypatch, sacct, shows):
    """A fake clock (each sleep moves it on) and a fake ssh answering the poll (squeue never lists the
    job), sacct and scontrol: sacct(t) gives sacct's 'State|ExitCode' line at time t, shows(t) what the
    rc or probe.out file reads on the submit host then ('' while hidden, as NFS can)."""
    import re
    from types import SimpleNamespace
    from ttp import slurmcheck
    clock = {"t": 0.0}
    monkeypatch.setattr(slurmcheck, "time", SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    token = {}

    def ssh(host, script, timeout, data=None):
        t, out = clock["t"], ""
        put = re.search(r"printf '%s\\n' (\w+) > \S+/probe\.in", script)
        mark = re.search(r"\{?(ttp-(?:slurm|probe):[0-9a-f]+)", script)
        if put:
            token["v"] = put.group(1)
        elif script.startswith("sbatch"):
            out = "901\n"
        elif script.startswith("sacct"):
            out = sacct(t) + "\n"
        elif script.startswith("scontrol"):
            out = ""   # sacct answers in these cases
        elif mark:
            shown = shows(t)
            out = f"{mark.group(1)}||{token.get('v', '') if shown == 'TOKEN' else shown}\n"
        return subprocess.CompletedProcess(script, 0, out.encode(), b"")
    monkeypatch.setattr(slurmcheck, "_ssh", ssh)
    monkeypatch.setattr(slurmcheck, "scancel", lambda *a: pytest.fail("nothing is cancelled"))
    return clock


def test_a_slurm_job_slurm_calls_failed_still_waits_out_its_late_rc_file(monkeypatch):
    # The node could not make the env: the script writes `noenv` and exits 1, so Slurm says FAILED, while
    # the rc file shows here 30 s late (NFS). It gets the same rc_grace_s a COMPLETED job gets.
    from ttp import slurmcheck
    s = slurmcheck.settings_of({"host": "h", "partition": "p", "rc_grace_s": 90})
    clock = _slurm_clock_fakes(monkeypatch, lambda t: "FAILED|1:0", lambda t: slurmcheck.NO_ENV if t >= 30 else "")
    rc, state, why, _ = slurmcheck._follow("h", "/w/q", "901", s, 10, 0, lambda **kw: None)
    assert (rc, state) == (None, "FAILED") and "pinned Python env could not be made" in why, why
    assert clock["t"] >= 30
    # A FAILED job whose rc file never shows is a cluster end once the grace passed.
    clock = _slurm_clock_fakes(monkeypatch, lambda t: "FAILED|1:0", lambda t: "")
    rc, state, why, _ = slurmcheck._follow("h", "/w/q", "901", s, 10, 0, lambda **kw: None)
    assert rc is None and why.startswith("cluster: it ended FAILED") and clock["t"] >= 90, why


def test_a_slurm_job_squeue_misses_while_sacct_shows_it_active_gets_the_full_rc_grace_after_its_end(monkeypatch):
    # squeue does not list the job, yet sacct shows it RUNNING until t=100; its rc file shows at t=150.
    # The grace counts from the end sacct shows (t=100), not from squeue's first miss (t=0).
    from ttp import slurmcheck
    s = slurmcheck.settings_of({"host": "h", "partition": "p", "rc_grace_s": 90})
    sacct = lambda t: "RUNNING|" if t < 100 else "COMPLETED|0:0"
    _slurm_clock_fakes(monkeypatch, sacct, lambda t: "0" if t >= 150 else "")
    assert slurmcheck._follow("h", "/w/q", "901", s, 10, 0, lambda **kw: None)[:3] == (0, "COMPLETED", "")
    # The workdir probe job alike: its answer under the workdir shows at t=150.
    _slurm_clock_fakes(monkeypatch, sacct, lambda t: "TOKEN" if t >= 150 else "")
    assert slurmcheck._probe("h", s, "/w/q", 10, lambda **kw: None) == (True, "")


def test_a_workdir_the_nodes_cannot_see_is_found_by_one_probe_job_before_any_check_job(env, tmp_path, monkeypatch, capsys):
    # The workdir on the submit host's own disk: one tiny probe job finds it before any check job is
    # sent; the run goes on here, the overflow is marked unusable with why (status, slot status, pick),
    # and the verdict is kept per (host, partition, workdir): new settings probe again.
    import threading
    from types import SimpleNamespace
    from ttp import cli, remotecheck, slurmcheck
    p, repo, run, d, log, cmd = _slurm_overflow_setup(env, tmp_path, monkeypatch, ["down"])
    monkeypatch.setenv("FAKE_SLURM_MODE", "localdisk")
    lane = _spill_lane(p, repo)
    threading.Timer(1.5, lane.close).start()
    cli.main(["checks", "--fresh"])
    rec, text = json.loads((run / "checks.json").read_text()), (run / "checks.log").read_text()
    assert rec["passed"] is True and "/ttp-check." not in log.read_text(), (rec, text)
    assert "not readable on the compute node" in text and "running them again on this machine" in text, text
    assert len((d / "probe.log").read_text().splitlines()) == 1 and not (d / "sbatch.log").exists()
    assert "--partition=cpu-part --time=1 " in (d / "probe.log").read_text()
    told = []
    assert slurmcheck.pick(SimpleNamespace(name=p.name), {}, told.append) is None
    assert "its workdir probe failed" in told[-1] and "waiting here" in told[-1], told
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["checks", "--slot-status"])
    assert "is unusable: its workdir probe failed" in capsys.readouterr().out
    lines = slurmcheck.probe_lines()
    assert len(lines) == 1 and "is unusable" in lines[0] and "not readable on the compute node" in lines[0]
    s = slurmcheck.settings()
    rc, out = _slurm_run(tmp_path, repo, "echo ran", {"FAKE_SLURM_MODE": "localdisk"}, workdir=s["workdir"])
    assert rc == remotecheck.FAULT_RC and "the Slurm overflow is unusable" in out, out
    assert len((d / "probe.log").read_text().splitlines()) == 1, "a kept failure sends no probe again"
    # A shared workdir (new settings): probed once, then every job goes without a probe.
    shared = s["workdir"] + "/shared"
    for _ in (1, 2):
        rc, out = _slurm_run(tmp_path, repo, "echo ran", {"FAKE_SLURM_MODE": "run"}, workdir=shared)
        assert rc == 0 and "ran" in out, out
    assert len((d / "probe.log").read_text().splitlines()) == 2 and len((d / "sbatch.log").read_text().splitlines()) == 2
    # The node's answer never showing on the submit host fails the probe too, once rc_grace_s passed.
    rc, out = _slurm_run(tmp_path, repo, "echo ran", {"FAKE_SLURM_MODE": "probehide"}, workdir=s["workdir"] + "/hidden",
                         rc_grace_s=1)
    assert rc == remotecheck.FAULT_RC and "did not show on cluster-login" in out, out
    assert len((d / "sbatch.log").read_text().splitlines()) == 2 and _rc_left(tmp_path) == []


def test_a_slurm_job_makes_its_pinned_env_under_the_shared_workdir_and_no_env_is_an_environment_fault(env, tmp_path):
    # The node makes (or reuses, by the lock's hash: runnerenv.ensure) the checks' pinned env under the
    # workdir, so one build serves every job on the shared file system; the check runs with its bin
    # first, the user site off and pip kept to a virtualenv. An env that cannot be made ends the job
    # with its reason: an environment fault (the run goes again here), never sent again as a cluster end.
    from ttp import remotecheck
    repo = _rc_repo(tmp_path)
    fake, d = _slurm_fakes(tmp_path)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python3").write_text('#!/bin/sh\necho "$TTP_HOME" >> "$FAKE_SLURM/maker.log"\n'
                                  'mkdir -p "$TTP_HOME/checks-env/k/bin" && echo "$TTP_HOME/checks-env/k"\n')
    (shim / "python3").chmod(0o755)
    seen, work = tmp_path / "seen", tmp_path / "remote"
    cmd = f'printf "%s|%s|%s" "${{PATH%%:*}}" "$PYTHONNOUSERSITE" "$PIP_REQUIRE_VIRTUALENV" > {seen}'
    rc, out = _slurm_run(tmp_path, repo, cmd, {**fake, "PATH": f"{shim}{os.pathsep}{fake['PATH']}"})
    assert rc == 0, out
    assert seen.read_text() == f"{work}/checks-env/k/bin|1|1"
    assert (d / "maker.log").read_text().split() == [str(work)]
    rc, out = _slurm_run(tmp_path, repo, "echo ran", {**fake, "TTP_TEST_CHECKS_PYTHON_ENV": str(tmp_path / "none")})
    assert rc == remotecheck.FAULT_RC and remotecheck.SIG in out and "submitting it again" not in out, out
    assert "pinned Python env could not be made on the node" in out and "\nran" not in out, out
    assert len((d / "sbatch.log").read_text().splitlines()) == 2 and _rc_left(tmp_path) == []


def _slurm_overflow_setup(env, tmp_path, monkeypatch, pool, host="cluster-login", **more):
    from ttp import project, slurmcheck
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, pool)
    fake, d = _slurm_fakes(tmp_path, monkeypatch)
    project.set_account_setting(slurmcheck.KEY, {**_SLURM_SET, "host": host, "workdir": str(remote), **more})
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    return p, repo, run, d, log, cmd


def test_a_run_no_pool_host_can_take_overflows_to_slurm_and_one_the_pool_takes_does_not(env, tmp_path, monkeypatch):
    from ttp import cli, slurmcheck
    p, repo, run, d, log, cmd = _slurm_overflow_setup(env, tmp_path, monkeypatch, ["down"])
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "cluster-login", rec
    text = (run / "checks.log").read_text()
    assert "overflowing to Slurm cluster cluster-login" in text and "as Slurm job 101" in text, text
    assert "--time=30 " in (d / "sbatch.log").read_text() and "/ttp-check." in log.read_text()
    assert slurmcheck.holders(p.name) == [] and slurmcheck.status_lines(p.name) == []
    assert not list(slurmcheck.slurm_dir().glob("want.*.json")), "the run's place among the waiters goes"
    # A reachable pool host takes the next run: no job.
    (d / "sbatch.log").unlink()
    p.set_config("delivery.checks_spill_hosts", ["check-host-a"])
    from ttp import machines
    machines.add("check-host-a")
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    assert json.loads((run / "checks.json").read_text())["spilled_to"] == "check-host-a"
    assert not (d / "sbatch.log").exists()


@pytest.mark.parametrize("host,mode", [("down", "run"), ("cluster-login", "refuse")])
def test_an_unreachable_or_refusing_cluster_keeps_the_run_here_never_failed(env, tmp_path, monkeypatch, host, mode):
    """A cluster ssh does not reach is skipped and the run waits here in its place; one whose sbatch
    refuses the job is an environment fault, and the run runs again here, whose verdict counts."""
    import threading
    from ttp import cli
    p, repo, run, d, log, cmd = _slurm_overflow_setup(env, tmp_path, monkeypatch, ["down"], host=host)
    monkeypatch.setenv("FAKE_SLURM_MODE", mode)
    lane = _spill_lane(p, repo)
    threading.Timer(1.5, lane.close).start()
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    text = (run / "checks.log").read_text()
    assert rec["passed"] is True and "/ttp-check." not in log.read_text(), (rec, text)
    if host == "down":
        assert "spilled_to" not in rec and "Slurm overflow (down) cannot now" in text, text
        assert not (d / "sbatch.log").exists(), text
    else:
        assert "sbatch refused the job" in text and "running them again on this machine" in text, text


def test_urgent_runs_take_the_slurm_overflow_first(env, tmp_path, monkeypatch):
    from ttp import project, slurmcheck
    _slurm_fakes(tmp_path, monkeypatch)
    project.set_account_setting(slurmcheck.KEY, {**_SLURM_SET, "workdir": str(tmp_path / "remote")})
    other = subprocess.Popen(["sleep", "60"])
    try:
        slurmcheck.slurm_dir().mkdir(parents=True, exist_ok=True)
        (slurmcheck.slurm_dir() / f"want.{other.pid}.json").write_text(json.dumps(
            {"pid": other.pid, "prio": 0, "arrived": time.time(), "until": time.time() + 600}))

        class P:
            name = "proj"
        told = []
        assert slurmcheck.pick(P(), {}, told.append, "x", prio=None, arrived=time.time() - 100) is None
        assert "1 run(s) ahead of this one" in told[-1]
        claim = slurmcheck.pick(P(), {}, told.append, "x", prio=0, arrived=time.time() - 100)
        assert claim is not None and claim.slurm["host"] == "cluster-login", told
        assert slurmcheck.status_lines("proj")[0].startswith("checks of pid ")
        claim.close()
    finally:
        other.kill()
        other.wait()
    assert slurmcheck.ahead(None, time.time()) == 0, "a dead waiter's ticket counts no more"


def test_a_run_behind_a_spill_wrapper_never_overflows_to_slurm(env, tmp_path, monkeypatch):
    """The batch job runs no spill wrapper and a wrapped check never runs unwrapped (remotecheck): a run
    with one waits here in its place instead of overflowing."""
    import threading
    from ttp import cli
    p, repo, run, d, log, cmd = _slurm_overflow_setup(env, tmp_path, monkeypatch, ["down"])
    p.set_config("checks.spill_wrap_cmd", "scripts/wrap.sh")
    assert cli._spill_wrapped(["pytest"], p.config()) and not cli._spill_wrapped(["pytest"], {})
    assert cli._spill_wrapped(["pytest"], {"checks": {"spill_wrap_cmd": "'unclosed"}}), "unparsed counts"
    lane = _spill_lane(p, repo)
    threading.Timer(1.5, lane.close).start()
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and "spilled_to" not in rec, rec
    assert not (d / "sbatch.log").exists() and "/ttp-check." not in log.read_text()


def test_a_remote_check_ships_python_files_out_of_its_tree_and_exports_literals_unexpanded(tmp_path):
    """--py files land on the host's PYTHONPATH in the run folder's .git (the copy's tree and status stay
    the commit's), ahead of a PYTHONPATH the check's env sets; --literal values export as given, never
    expanded or split by the remote shell; a file that cannot be read copies nothing (exit 2)."""
    from ttp import remotecheck
    repo = _rc_repo(tmp_path)
    mod = tmp_path / "ttp_ship_probe.py"
    mod.write_text("WORD = 'shipped'\nEND = 'TTP_PY_x'\n")
    seen = tmp_path / "seen"
    seen.mkdir()
    cmd = (f"python3 -c 'import ttp_ship_probe as m; print(m.WORD)' > {seen}/mod; git status --porcelain > {seen}/status; "
           f"printf %s \"$PYTHONPATH\" > {seen}/pp; printf %s \"$SEL\" > {seen}/sel")
    bin_, remote = _rc_fake_ssh(tmp_path)
    penv = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_REMOTE_TMP": str(remote)}
    odd = "^(?:a|b)$ `x` \"q\" 'z' $HOME\nline2"
    argv = remotecheck.argv(cmd, "box1", {"PYTHONPATH": "/own"}, py=(str(mod),), literal={"SEL": odd})
    r = subprocess.run(argv, cwd=repo, env=penv, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (seen / "mod").read_text().strip() == "shipped"
    assert (seen / "status").read_text() == "", "the shipped file is not in the tree the check sees"
    pp = (seen / "pp").read_text()
    assert pp.endswith(f"/{remotecheck.SHIP_DIR}:/own") and "/ttp-check." in pp, pp
    assert (seen / "sel").read_text() == odd
    assert _rc_left(tmp_path) == []
    gone = remotecheck.argv("true", "box1", py=(str(tmp_path / "missing.py"),))
    r = subprocess.run(gone, cwd=repo, env=penv, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "cannot read" in r.stdout + r.stderr
    bad = remotecheck.argv("true", "box1", literal={"1x": "v"})
    assert subprocess.run(bad, cwd=repo, env=penv, capture_output=True, text=True, timeout=60).returncode == 2


_GATE_PRE = "def test_ok():\n    pass\n\n\ndef test_pre():\n    assert False\n"
_GATE_FAIL = "python3 -m pytest -p no:cacheprovider tests/"


def _spill_gate(env, tmp_path, monkeypatch, base_body, head_body):
    """A spill setup whose base commit has tests/test_gate.py with `base_body` and whose head has `head_body`,
    checked by one plain pytest run: (project, repo, run dir)."""
    from ttp import cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    (repo / "tests").mkdir()
    (repo / "tests" / "test_gate.py").write_text(base_body)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    base = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "tests" / "test_gate.py").write_text(head_body)
    subprocess.run([*git, "commit", "-qam", "head"], check=True)
    monkeypatch.setattr(cli, "_checks_base", lambda *a: base)
    p.set_config("delivery.push_checks", [_GATE_FAIL])
    return p, repo, run


def test_a_spilled_failure_on_base_and_head_is_judged_on_the_spill_host_and_lands_without_a_rerun_here(
        env, tmp_path, monkeypatch):
    """Follow-up of the spill hosts' tests gate: a spilled pytest check whose failing test also fails on the
    base reruns only that test on the spill host (the select plugin shipped there), on the base only (it
    fails there too, so the head does not rerun it), and the change lands (landing only) without running the checks again here; the base result is
    saved under the spill host's name. A failure new on the head still runs again here, as before."""
    import threading
    from ttp import cli, gatejudge
    marks = tmp_path / "where.log"
    # Each pytest run (a rerun included) records where it ran and which tests it kept.
    conftest = (f"import os\n\n\ndef pytest_collection_finish(session):\n"
                f"    with open({str(marks)!r}, 'a') as f:\n"
                f"        f.write(os.getcwd() + ' ' + ','.join(i.name for i in session.items) + '\\n')\n")
    p, repo, run = _spill_gate(env, tmp_path, monkeypatch, _GATE_PRE, _GATE_PRE + "\n\ndef helper():\n    return 1\n")
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    (repo / "conftest.py").write_text(conftest)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "conftest"], check=True)
    base = subprocess.run([*git, "rev-parse", "HEAD~2"], capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(cli, "_checks_base", lambda *a: base)
    lane = _spill_lane(p, repo)
    late = threading.Timer(30.0, lane.close)
    late.start()
    try:
        cli.main(["checks", "--fresh"])
    finally:
        late.cancel()
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    text = (run / "checks.log").read_text()
    assert rec["passed"] and rec.get("landing_only") and rec.get("spilled_to") == "check-host-a", (rec, text)
    assert rec["excused"][0]["pre_existing"] == ["tests/test_gate.py::test_pre"], rec
    assert rec["excused"][0]["host"] == "check-host-a", rec
    assert "did not pass on spill host" not in text, "no rerun here"
    runs = [line.split(" ", 1) for line in marks.read_text().splitlines()]
    assert all("/ttp-check." in where for where, _ in runs), runs
    assert len(runs) == 1, ("failing on the base too: no head rerun", runs, text)
    assert "(rerun on the base" in text and "(rerun on the head" not in text, text
    # The base (no conftest there) ran on the spill host too: its saved result is that host's.
    assert gatejudge.saved_base(p.state, "check-host-a", base, ["tests/test_gate.py::test_pre"], time.time())
    assert not gatejudge.saved_base(p.state, gatejudge.host(), base, ["tests/test_gate.py::test_pre"], time.time())


def test_a_spilled_failure_new_on_the_head_fails_the_change_and_a_rerun_of_its_tree_fails_before_the_queue(
        env, tmp_path, monkeypatch):
    """#6509: the spill host's gate finds a failure new on the head (it failed twice there and passes on the
    base there): the host's environment is ruled out, so the run fails without running the checks again here
    (no slot taken for a run that fails again). A later `ttp checks` of the same tree fails at once, before it
    queues for a checks slot, naming the tests and telling the task to fix them; --fresh runs it again."""
    import threading
    from ttp import cli, gatejudge
    base = "def test_ok():\n    pass\n\n\ndef test_x():\n    pass\n"
    p, repo, run = _spill_gate(env, tmp_path, monkeypatch, base,
                               base.replace("def test_x():\n    pass", "def test_x():\n    assert False"))
    lane = _spill_lane(p, repo)
    threading.Timer(2.0, lane.close).start()
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--fresh"])
    assert e.value.code == 1
    text = (run / "checks.log").read_text()
    assert "rerun at once on check-host-a" in text and "new on this head" in text, text
    assert "did not pass on spill host check-host-a" not in text, "no full rerun here"
    assert "tests the base passes on that host: tests/test_gate.py::test_x" in text, text
    assert not json.loads((run / "checks.json").read_text())["passed"]
    tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True).stdout.strip()
    known = gatejudge.known_new(p.state, tree, _GATE_FAIL)
    assert known and known["new"] == ["tests/test_gate.py::test_x"] and known["host"] == "check-host-a", known
    size = len(text)
    with pytest.raises(SystemExit) as e:
        cli.main(["checks"])
    assert e.value.code == 1
    again = (run / "checks.log").read_text()[size:]
    assert "not queued for a checks slot" in again and "tests/test_gate.py::test_x" in again \
        and "Fix them" in again, again
    assert "rerun at once" not in again and "check-host-a" in again, "it ran nothing: the saved verdict decided"
    assert not json.loads((run / "checks.json").read_text())["passed"]


@pytest.mark.parametrize("got", [(None, ""), (2, "ERROR: usage\n")], ids=["timeout", "exit-2"])
def test_a_spilled_failure_whose_head_rerun_cannot_tell_still_runs_again_here_and_saves_nothing(
        env, tmp_path, monkeypatch, got):
    """Review #6582: the spill host's head rerun times out or exits 2, so nothing is proven new there (the host's
    environment is not ruled out): the checks run again here as before and gate_new.json keeps nothing."""
    import threading
    from ttp import cli, gatejudge
    base = "def test_ok():\n    pass\n\n\ndef test_x():\n    pass\n"
    p, repo, run = _spill_gate(env, tmp_path, monkeypatch, base,
                               base.replace("def test_x():\n    pass", "def test_x():\n    assert False"))
    monkeypatch.setattr(gatejudge, "rerun", lambda *a, **k: got)
    lane = _spill_lane(p, repo)
    _close_when_back(lane, run)
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--fresh"])
    assert e.value.code == 1
    text = (run / "checks.log").read_text()
    assert "rerun at once on check-host-a" in text and "not proven new" in text, text
    assert "did not pass on spill host check-host-a" in text, text
    assert "tests the base passes on that host" not in text, text
    assert not json.loads((run / "checks.json").read_text())["passed"]
    assert not (p.state / gatejudge.NEW_FILE).exists() or not json.loads((p.state / gatejudge.NEW_FILE).read_text())


def _spill_gate_unproven(env, tmp_path, monkeypatch, at):
    """A spilled failure whose gate rerun at `at` ("head" or "base") cannot tell (exit 255, an ssh reset,
    or a timeout) on the spill host: the gate keeps it new but never proven, so the spill host's fault is
    not counted there; the checks run again here (whose own rerun fails the same way: exit 1)."""
    import threading
    from ttp import cli, gatejudge
    base = "def test_ok():\n    pass\n\n\ndef test_x():\n    pass\n"
    p, repo, run = _spill_gate(env, tmp_path, monkeypatch, base,
                               base.replace("def test_x():\n    pass", "def test_x():\n    assert False"))
    real = gatejudge.rerun
    faults = iter([(255, "ssh: connection reset\n"), (None, "")] * 4)

    def rerun(run_, where, ids, *a, **k):
        return next(faults) if where == at else real(run_, where, ids, *a, **k)
    monkeypatch.setattr(gatejudge, "rerun", rerun)
    lane = _spill_lane(p, repo)
    _close_when_back(lane, run)
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--fresh"])
    assert e.value.code == 1
    text = (run / "checks.log").read_text()
    assert "rerun at once on check-host-a" in text, text
    assert "tests the base passes on that host" not in text, ("a fault there is no proof", text)
    assert "did not pass on spill host check-host-a" in text, text
    assert not json.loads((run / "checks.json").read_text())["passed"]


def test_a_spilled_failure_whose_head_rerun_there_cannot_tell_is_not_counted_there(env, tmp_path, monkeypatch):
    _spill_gate_unproven(env, tmp_path, monkeypatch, "head")


def test_a_spilled_failure_whose_base_rerun_there_cannot_tell_is_not_counted_there(env, tmp_path, monkeypatch):
    _spill_gate_unproven(env, tmp_path, monkeypatch, "base")


def test_spill_first_sends_a_run_to_an_idle_spill_host_with_its_lane_here_free_and_off_waits_here_first(
        env, tmp_path, monkeypatch):
    """#2210: delivery.checks_spill_first (default true) tries the spill hosts before a place here, even with the
    lane here free and a long checks_spill_after_s, and the spill line says why; set false, a run with its lane
    free runs here without trying them."""
    from ttp import checkcap, checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    p.set_config("delivery.checks_spill_after_s", 3600)
    log = tmp_path / "where.log"
    p.set_config("delivery.push_checks", [f"pwd >> {log}"])
    real_acquire = checkcap.acquire
    monkeypatch.setattr(checkcap, "acquire", lambda *a, **k: pytest.fail("no place here before the spill hosts"))
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", rec
    assert "/ttp-check." in log.read_text(), "it ran on the spill host's copy"
    text = (run / "checks.log").read_text()
    assert "spilling to check-host-a: spill first (delivery.checks_spill_first)" in text, text
    monkeypatch.setattr(checkcap, "acquire", real_acquire)
    monkeypatch.setattr(checkspill, "pick", lambda *a, **k: pytest.fail("spill first is off and the lane is free"))
    p.set_config("delivery.checks_spill_first", False)
    log.write_text("")
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and "spilled_to" not in rec, rec
    assert log.read_text().strip() and "/ttp-check." not in log.read_text(), "it ran here"


def test_a_run_held_here_by_the_load_guard_or_a_cap_goes_to_a_spill_host_at_its_next_bound(env, tmp_path, monkeypatch):
    """No spill host had room at first; the wait here is held (the load guard or a cap: its bounded take gets
    no place): at the bound the spill hosts are tried again and the run goes to the one with room."""
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    p.set_config("delivery.push_checks", [f"pwd >> {tmp_path / 'where.log'}"])
    real_pick, picks, takes = checkspill.pick, [], []
    monkeypatch.setattr(checkspill, "pick", lambda *a, **k: None if not picks.append(1) and len(picks) == 1
                        else real_pick(*a, **k))
    monkeypatch.setattr(cli, "_checks_slot_take", lambda *a, **k: takes.append(k.get("urgent")) or None)
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", rec
    assert len(picks) == 2 and len(takes) == 1, (picks, takes)


def test_a_spilled_run_gets_test_workers_sized_to_the_hosts_idle_cores_outside_its_pass_key(env, tmp_path, monkeypatch):
    """#2210: -n is sized per run to the spill host's idle cores (cores less its 1-minute load, less a run's
    cores per other claim of ours there, within one run's limit), exported as the test-slot grant the suite's
    conftest reads; the grant is no part of the pass key, so a run here of the same tree reuses the pass."""
    from ttp import checkspill, cli, push, testslots
    assert checkspill.workers(16, 3.5) == 12 and checkspill.workers(16, 3.5, cap=8) == 8
    assert checkspill.workers(16, None) == 0 and checkspill.workers(0, 1.0) == 0
    assert checkspill.workers(4, 9.0) == 1, "an overloaded host still runs one worker"
    assert checkspill.workers(32, 0.0, others=1) < 32
    assert checkspill.workers_env(0) == {}
    assert checkspill.workers_env(3) == {testslots.SLOTS_ENV: "3", testslots.HELD_ENV: "3", testslots.XDIST_ENV: "3"}
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    monkeypatch.setattr(checkspill, "_PROBE", _idle_probe(64))
    seen = tmp_path / "seen"
    p.set_config("delivery.push_checks", [f"printf %s \"${testslots.SLOTS_ENV}\" > {seen}"])
    p.set_config("checks.test_run_max", 4)
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", rec
    assert seen.read_text() == "4", "64 idle cores, but one run's limit (test_run_max 4)"
    assert "with 4 test worker(s), its idle cores (load 0 of 64)" in (run / "checks.log").read_text()
    plain = push.Check("pytest -q")
    granted = cli._spill_to([push.Check("pytest -q")], "check-host-a", p.config(), 5)[0]
    assert dict(granted.literal_env)[testslots.SLOTS_ENV] == "5"
    assert cli._command_key(granted) == cli._command_key(plain)
    own = push.Check("pytest -q")
    own.literal_env = ((testslots.SLOTS_ENV, "2"),)
    assert dict(cli._spill_to([own], "check-host-a", p.config(), 5)[0].literal_env)[testslots.SLOTS_ENV] == "2"


def test_a_queued_run_spills_to_the_most_free_host_with_workers_sized_to_its_free_cores(env, tmp_path, monkeypatch):
    """#7996 (pick_most_free_host, spill_workers_sized): every usable spill host is probed and the run goes to the
    one with the most free capacity, not the first listed; its workers fit that host's idle cores, and the tell
    and the run's limit record (ttp status, the web app) say so."""
    from ttp import checkspill
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    loads = {"check-host-a": 60.0, "check-host-b": 4.0}

    def fake_probe(host, beside=False):
        checkspill._READ[host] = (64, loads[host])
        return 2, "", ""
    monkeypatch.setattr(checkspill, "probe", fake_probe)
    p.set_config("checks.test_run_max", 32)
    told = []
    got = checkspill.pick(p, p.config(), told.append, "task #1 checks")
    try:
        assert got is not None and got.host == "check-host-b", told
        assert 1 < got.workers <= 32, got.workers
        line = told[-1]
        assert "spilling to check-host-b" in line and "the most free of 2 usable spill hosts" in line, line
        assert f"with {got.workers} test worker(s), its idle cores (load 4 of 64)" in line, line
        assert "limit checks.test_run_max" in line or got.workers < 32, line
        assert any("limited: spill host check-host-b:" in x for x in checkspill.limit_lines(p.name))
    finally:
        got and got.close()
    assert not any("check-host-b" in x for x in checkspill.limit_lines(p.name)), "freed with the claim"


def test_the_spill_pick_spreads_counting_claims_not_yet_running_against_free_slots(env, tmp_path, monkeypatch):
    """#7996 (spill_pick_spreads, user msg #2469): the pick goes to the host with the most free slots less this
    machine's claims there that its probe did not see running yet, not the first host with any slot."""
    from ttp import checkspill
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    frees = {"check-host-a": 3, "check-host-b": 2}

    def fake_probe(host, beside=False):
        checkspill._READ[host], checkspill._CHECKS_RUNNING[host] = (64, 4.0), 0
        return frees[host], "", ""
    monkeypatch.setattr(checkspill, "probe", fake_probe)
    monkeypatch.setattr(checkspill, "_ours", lambda h: 2 if h == "check-host-a" else 0)
    p.set_config("checks.test_run_max", 2)   # both hosts size a run alike: only the free slots decide
    told = []
    got = checkspill.pick(p, p.config(), told.append, "task #1 checks")
    try:
        assert got is not None and got.host == "check-host-b", told
    finally:
        got and got.close()
    assert checkspill._inflight("check-host-a") == 2
    monkeypatch.setitem(checkspill._CHECKS_RUNNING, "check-host-a", 2)
    assert checkspill._inflight("check-host-a") == 0, "claims seen running there are in its load already"


def test_a_host_wide_checks_wait_names_its_cap_and_where_the_cap_comes_from(monkeypatch):
    """#7996 (user msg #2469): the host-wide cap in a wait line says its number and source: the setting, or the
    free-core fit while checks.host_max is unset."""
    from ttp import checkcap
    monkeypatch.delenv("TTP_CHECKS_HOST_MAX", raising=False)
    monkeypatch.setattr(checkcap, "_set_limit", lambda: None)
    assert checkcap.cap_text(12) == "cap 12: the free-core fit (checks.host_max unset)"
    monkeypatch.setattr(checkcap, "_set_limit", lambda: 6)
    assert checkcap.cap_text(6) == "cap 6 (checks.host_max)"
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "6")
    assert checkcap.cap_text(6) == "cap 6 (TTP_CHECKS_HOST_MAX)"


def test_a_spilled_run_held_to_one_worker_says_why(env, tmp_path, monkeypatch):
    """#7996 (single_worker_logged): a spill host sized to 1 test worker says which cores are idle and which
    are kept, and a run's per-run cap names its setting; an unread load says the run sizes itself."""
    from ttp import checkspill
    n, why = checkspill.workers_why(4, 9.0)
    assert n == 1 and "only 1 because 0 core(s) are idle" in why, why
    n, why = checkspill.workers_why(10, 4.0, others=1)
    assert n == 1 and "kept for 1 other run(s)" in why and "only 1 because" in why, why
    n, why = checkspill.workers_why(64, 0.0, cap=8)
    assert n == 8 and "at most 8 per run (limit checks.test_run_max)" in why, why
    assert checkspill.workers_why(16, None)[1].startswith("test workers it sizes itself")


def test_a_claim_record_no_live_holder_locks_counts_for_no_run(env, tmp_path, monkeypatch):
    """#7996: a claim record left by a run a reboot killed (its pid since reused by another process) holds no
    lock: it is no holder and keeps no cores off the next run's workers (it once sized a spill to 1 worker)."""
    from ttp import checkspill
    d = checkspill.claim_dir()
    d.mkdir(parents=True, exist_ok=True)
    stale = d / "check-host-a.0.json"
    stale.write_text(json.dumps({"project": "other", "pid": os.getpid(), "host": "check-host-a", "since": 1.0}))
    assert not any(r.get("host") == "check-host-a" for r in checkspill.holders())
    assert checkspill._ours("check-host-a") == 0


def test_a_parked_checks_run_names_the_limit_its_value_and_setting(env, tmp_path, monkeypatch):
    """#7996 (limit_reason_logged, status_shows_limit): a run parked on a limit writes the limit, its value and
    its setting to its log, and the same line shows in ttp status and the web app's checks view until it ends."""
    import contextlib
    import io
    from ttp import checkcap, checkspill, cli, web
    p = make(env)
    why = "the host's test pool: 0 of 8 free, a run needs 2 (limits checks.test_pool, checks.test_run_min)"
    told = []
    checkcap._tell_parked(told.append, p.name, lambda: why)
    assert told == [checkcap.parked_line(why)] and checkcap.PARKED_LINE in told[0] and why in told[0]
    lines = checkspill.limit_lines(p.name)
    assert lines and "limited: waits for its test slots: " + why in lines[0], lines
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main(["status", p.name])
    assert "limited: waits for its test slots:" in out.getvalue()
    import time
    assert any("checks.test_pool" in r["text"] for r in web.slots_feed(time.time(), p)), "web feed"
    checkcap._note_limit("", p.name)
    assert checkspill.limit_lines(p.name) == []


def _full_after(monkeypatch, free=1, running=0):
    """checkspill.probe for fake hosts: `free` check slots, `running` checks there (no ssh)."""
    from ttp import checkspill

    def fake(host, beside=False):
        checkspill._READ[host], checkspill._CHECKS_RUNNING[host] = (64, 0.0), running
        return free, "", ""
    monkeypatch.setattr(checkspill, "probe", fake)


def test_checks_pinned_to_a_host_take_its_check_slots_as_a_spill_pick_does(env, tmp_path, monkeypatch):
    """#7996 (pinned_host_admission, no_overshoot_concurrent_picks): checks pinned to a host claim its check
    slots in the shared claim folder, so runs of any project here (pinned or spilled) never start more there
    than its probe's free slots, claims still in flight (no check running there yet) included; a run that
    waits says why with its numbers, and status shows it."""
    import threading
    from ttp import checkspill
    p = make(env)
    monkeypatch.setitem(checkspill._CHECKS_RUNNING, "check-host-a", 0)   # the last probe: no check running there yet
    got, lock = [], threading.Lock()

    def take():
        c = checkspill._claim("check-host-a", 2, {"project": p.name, "pid": os.getpid(), "host": "check-host-a"})
        with lock:
            got.append(c)
    ts = [threading.Thread(target=take) for _ in range(6)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    held = [c for c in got if c is not None]
    assert len(held) == 2, "6 pickers at once, 2 free slots: no overshoot"
    for c in held:
        c.close()
    _full_after(monkeypatch, free=1)
    first = checkspill.admit_pinned(p, ["check-host-a"], print, "task #1 checks")
    assert first and len(first) == 1
    told = []
    assert checkspill.admit_pinned(p, ["check-host-a"], told.append, "task #2 checks", bound=0,
                                   sleep=lambda s: None) is None
    assert told and "checks pinned to check-host-a wait for a check slot there: its 1 free check slot(s) are " \
        "claimed by this machine's 1 run(s) there" in told[0], told
    assert any("limited: checks pinned to check-host-a" in x for x in checkspill.limit_lines(p.name))
    for c in first:
        c.close()
    again = checkspill.admit_pinned(p, ["check-host-a"], print, "task #2 checks", bound=0)
    assert again and len(again) == 1
    again[0].close()
    monkeypatch.setattr(checkspill, "probe", lambda h, b=False: (0, "ssh cannot reach it: exit 255", ""))
    assert checkspill.admit_pinned(p, ["check-host-a"], print, bound=0) == [], "an unreadable host holds no run"


def test_a_tasks_checks_never_start_anyway_on_an_overloaded_pinned_host(env, tmp_path, monkeypatch):
    """#7996 (no_start_anyway_task_checks): `ttp checks` pinned to an overloaded host never start there past
    the bound: the wait says why with its numbers and hands over (None); push checks keep their bounded start."""
    import io
    from ttp import checkcap, checkspill, cli, push, remoteload
    p = make(env)
    monkeypatch.delenv("TTP_DETACHED", raising=False)
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 0.0)
    monkeypatch.setattr(checkspill, "probe", lambda h, b=False: pytest.fail("no admission while overloaded"))
    why = "the check host check-host-a's CPU is overloaded: its runnable demand of 298.0 cores is above its 64 cores"
    monkeypatch.setattr(remoteload, "held", lambda h, **kw: why)
    cmds = push.check_list([{"run": "pytest -n 16 tests", "host": "check-host-a"}])
    told = []
    # #8107 (load_guard_in_status): while it waits, status names the guard and its setting; cleared after.
    seen = []

    def nap(s):
        seen.append(checkspill.limit_lines(p.name))
        monkeypatch.setattr(remoteload, "held", lambda h, **kw: "")
    note = lambda line: checkspill.note_limit(p.name, line)   # noqa: E731
    assert remoteload.wait(["check-host-a"], told.append, None, sleep=nap, note=note) == ""
    assert any("remote load guard" in x and "checks.load_guard" in x and why in x for x in seen[0]), seen
    assert checkspill.limit_lines(p.name) == []
    monkeypatch.setattr(remoteload, "held", lambda h, **kw: "")
    _full_after(monkeypatch, free=1)
    out = io.StringIO()
    slot = cli._checks_slot(p, tmp_path, out, "a" * 40, cmds=cmds, top=str(p.root))
    assert slot is not None and len(slot.pinned) == 1 and "holding 1 check slot(s) there" in out.getvalue()
    slot.close()
    assert slot.pinned == [] and checkspill._ours("check-host-a") == 0




def test_spill_host_entries_take_their_own_max_runs_and_jobs_and_plain_aliases_still_work(env, capsys):
    """A spill-host list may mix plain aliases with tables `{"host", "max_runs", "jobs"}` (checkspill.entries_of):
    hosts_of reads the aliases as before, limits the table's numbers; a bad table is refused whole."""
    from ttp import checkspill, cli
    v = ["check-host-a", {"host": " check-host-b ", "max_runs": 1, "jobs": 6}, {"host": "check-host-c"},
         "check-host-a"]
    assert checkspill.entries_of(v) == ["check-host-a", {"host": "check-host-b", "max_runs": 1, "jobs": 6},
                                        "check-host-c"]
    assert checkspill.hosts_of(v) == ["check-host-a", "check-host-b", "check-host-c"]
    assert checkspill.hosts_of(json.dumps(v)) == checkspill.hosts_of(v)
    assert checkspill.hosts_of(["check-host-a", "check-host-b"]) == ["check-host-a", "check-host-b"]
    cfg = {"delivery": {"checks_spill_hosts": v}}
    assert checkspill.limits(cfg, "check-host-b") == {"max_runs": 1, "jobs": 6}
    assert checkspill.limits(cfg, "check-host-a") == {} and checkspill.limits(cfg, "nope") == {}
    for bad in ([{"host": "check-host-a", "max_runs": 0}], [{"host": "check-host-a", "jobs": True}],
                [{"host": "check-host-a", "jobs": "4"}], [{"host": "check-host-a", "slots": 2}],
                [{"max_runs": 1}], [{"host": "local"}]):
        with pytest.raises(ValueError):
            checkspill.entries_of(bad)
    cli.main(["config", "--account", "checks.spill_hosts", '[{"host": "pool-a", "max_runs": 1, "jobs": 8}, "pool-b"]'])
    assert checkspill.account_hosts() == ["pool-a", "pool-b"]
    assert checkspill.limits({}, "pool-a") == {"max_runs": 1, "jobs": 8}, "the account's pool keeps its limits"
    with pytest.raises(SystemExit):
        cli.main(["config", "--account", "checks.spill_hosts", '[{"host": "pool-a", "max_runs": -1}]'])
    assert "checks.spill_hosts" in capsys.readouterr().err
    assert checkspill.limits({}, "pool-a") == {"max_runs": 1, "jobs": 8}


def test_a_spill_host_capped_by_max_runs_refuses_a_second_run_and_takes_one_again_once_freed(env, tmp_path, monkeypatch):
    """max_runs 1: the host takes one of this machine's checks at once though its idle cores fit more; a second
    run is told why and waits; freed, the host takes one again."""
    from ttp import checkspill
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, [{"host": "check-host-a", "max_runs": 1}])
    told: list[str] = []
    held = checkspill.pick(p, p.config(), told.append, "pytest -q")
    try:
        assert held is not None and held.host == "check-host-a", told
        assert checkspill.pick(p, p.config(), told.append, "pytest -q") is None
        assert "check-host-a (its max_runs 1 of this machine's checks run there already" in told[-1], told
    finally:
        held.close()
    again = checkspill.pick(p, p.config(), told.append, "pytest -q")
    assert again is not None and again.host == "check-host-a"
    again.close()


def test_a_spill_hosts_jobs_setting_is_the_test_workers_its_runs_get(env, tmp_path, monkeypatch):
    """jobs 6: a run spilled there gets 6 test workers (the grant its xdist -n reads), not the host's idle cores
    nor one run's usual limit, and the run says so."""
    from ttp import checkspill, cli, testslots
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, [{"host": "check-host-a", "jobs": 6}])
    monkeypatch.setattr(checkspill, "_PROBE", checkspill._PROBE.replace("c=$(nproc", "c=$(echo 64 || nproc"))
    seen = tmp_path / "seen"
    p.set_config("delivery.push_checks", [f"printf %s \"${testslots.SLOTS_ENV}\" > {seen}"])
    p.set_config("checks.test_run_max", 4)
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", rec
    assert seen.read_text() == "6", "the host's jobs, not its idle cores or test_run_max"
    assert "with 6 test worker(s), its jobs setting" in (run / "checks.log").read_text()


def test_a_check_reading_git_history_stays_here_and_the_rest_of_its_run_spills(env, tmp_path, monkeypatch):
    """Upstream note 92664c3b34114013: a spill host's copy has one commit, so `git diff --check <base>` exited 128
    there. A check that runs a git command reading commits (any form: options, an env prefix, a shell -c, an
    unclosed quote) stays here unless it says "spill": true; `git status` and pytest still spill."""
    from ttp import checkspill, cli, push
    for cmd in ("git diff --check origin/main", "git -C plugins log -1", "env A=1 git --no-pager diff HEAD~1",
                "true && git merge-base HEAD main", "bash -c 'git rev-list --count HEAD'", "git log 'unclosed"):
        assert checkspill.needs_history(push.Check(cmd)), cmd
        assert not checkspill.check_spills(push.Check(cmd)), cmd
    for cmd in ("git status --porcelain", "pytest -q tests", "python3 -m pytest -n 4", "git -c a=b rev-parse HEAD"):
        assert not checkspill.needs_history(push.Check(cmd)), cmd
    assert checkspill.check_spills(push.check_list([{"run": "git diff --check HEAD", "spill": True}])[0])
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    log = tmp_path / "where.log"
    p.set_config("delivery.push_checks", [f"git diff --check HEAD && pwd >> {log}", f"pwd >> {log}"])
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", rec
    where = log.read_text().splitlines()
    assert len(where) == 2 and sum("/ttp-check." in w for w in where) == 1, where


def test_spill_first_setting_is_validated(env):
    from ttp import checkspill, coordinator, project
    assert checkspill.first({}) is True and checkspill.first({"delivery": {"checks_spill_first": False}}) is False
    assert not project.config_problems({"delivery": {"checks_spill_first": False}})
    assert any("delivery.checks_spill_first" in x for x in project.config_problems({"delivery": {"checks_spill_first": "maybe"}}))
    settable = coordinator.USER_SETTABLE["delivery.checks_spill_first"]
    assert settable("off") is False and settable("true") is True
    with pytest.raises(ValueError, match="nothing was changed"):
        settable("sometimes")


def test_a_spill_wrapper_that_puts_its_own_venv_first_never_hides_the_pinned_checks_env(tmp_path):
    """A host's spill wrapper may put its own Python env first on PATH (one that lacks a package the
    checks import): behind it the command still runs with the pinned checks env's bin first and the user
    site off, so every check there failing at collection never keeps a healthy host off."""
    from ttp import remotecheck
    repo = _rc_repo(tmp_path)
    venv = tmp_path / "wrapper-venv" / "bin"
    venv.mkdir(parents=True)
    wrap = tmp_path / "venv-wrap.sh"
    wrap.write_text(f'#!/bin/sh\nPATH="{venv}:$PATH"; PYTHONNOUSERSITE=; export PATH PYTHONNOUSERSITE\nexec "$@"\n')
    wrap.chmod(0o755)
    bin_, remote = _rc_fake_ssh(tmp_path)
    penv = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_REMOTE_TMP": str(remote)}
    for w in ((str(wrap),), (str(wrap), "env", "X=1")):
        r = subprocess.run(remotecheck.argv('echo "first=${PATH%%:*} nousersite=$PYTHONNOUSERSITE"', "box1", {}, "", w),
                           cwd=repo, env=penv, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        first = r.stdout.split("first=", 1)[1].split()[0]
        assert first != str(venv) and first.endswith("/bin") and "nousersite=1" in r.stdout, (w, r.stdout)


def test_spill_pick_spreads_runs_across_hosts_with_room_and_checks_status_says_why_each_took_none(
        env, tmp_path, monkeypatch, capsys):
    """pick() sends a new run to a host running fewer of this machine's checks first, never piling every run
    on the first listed host while others with room run nothing; its last pick shows in `ttp checks status`
    with each host it skipped and why."""
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["down", "check-host-a", "check-host-b"])
    told = []
    first = checkspill.pick(p, p.config(), told.append, "pytest -q")
    lines = checkspill.pick_lines(p.name)
    assert any(line.startswith("  spill host down took no run: ssh cannot reach it") for line in lines), lines
    second = checkspill.pick(p, p.config(), told.append, "pytest -q")
    try:
        assert first is not None and second is not None
        assert (first.host, second.host) == ("check-host-a", "check-host-b"), told
        lines = checkspill.pick_lines(p.name)
        # The unreachable host backs off (not probed again) and status names it as skipped with why: no silent limit.
        assert lines[0] == "last spill pick (0 min ago) took check-host-b", lines
        assert len(lines) == 2 and lines[1].startswith("  spill host down took no run: ") \
            and "backing off after 1 failed probe(s)" in lines[1], lines
        off = ",".join(f"{h}:fail" for h in ("down", "check-host-a", "check-host-b"))
        assert checkspill.pick(p, p.config(), told.append, "pytest -q", off) is None
        lines = checkspill.pick_lines(p.name)
        assert lines[0] == "last spill pick (0 min ago) found no spill host with room", lines
        assert "  spill host check-host-b took no run: these checks did not pass there in this run" in lines
        with pytest.raises(SystemExit):
            cli.main(["checks", "status"])
        assert "spill host check-host-a took no run: these checks did not pass there" in capsys.readouterr().out
        assert checkspill.pick_lines(p.name, now=time.time() + checkspill.SKIP_SHOW_S + 1) == [], "old picks age out"
    finally:
        first and first.close()
        second and second.close()


@pytest.mark.parametrize("rerank", [False, True])
def test_a_parked_run_re_executed_for_an_upgrade_tries_every_spill_host_again(env, monkeypatch, rerank):
    """A run parked here whose spilled checks failed on every spill host keeps them off only within one
    runtime: re-executed for an upgrade (a fix of what failed there), it tries them all again at its next
    bound; a priority change (same runtime) keeps them off."""
    import io
    from ttp import checkcap, checkspill, cli

    class _Exec(Exception):
        pass

    def fake_exec(*_a):
        raise _Exec(os.environ.get(checkspill.OFF_ENV))
    for k in (checkspill.OFF_ENV, checkcap.REEXEC_ENV, checkcap.RERANK_ENV, checkcap.ARRIVED_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cli.os, "execv", fake_exec)
    off = "host-a:fail,host-b:fail"
    with pytest.raises(_Exec) as e:
        cli._checks_reexec("2" if rerank else "99.0.0", time.time(), 3, True, lambda _l: None, io.StringIO(),
                           rerank=rerank, spill_off=off)
    assert e.value.args[0] == (off if rerank else None)


@_LINUX_TMP
def test_import_only_failure_reruns_once(env, tmp_path, monkeypatch):
    """A package the spill hosts' checks env lacks fails the checks at collection on a spill host: a fault of
    the spill env config, never of the change, so it runs again once elsewhere. Failing the same way on the
    second host, that result counts (an import failure reruns once per head) and the coordinator is told once of
    the config fault; every spill host is tried again once the checks config changes or ENV_RETRY_S passes."""
    import threading
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    # The script is part of the commit, named by a relative path: a path of this machine (tmp_path is one,
    # outside /dev/shm on Linux) would keep the check here (checkspill.host_path), never trying a spill host.
    (repo / "check.sh").write_text('case "$(pwd)" in */ttp-check.*)\n'
                                   "  printf 'ERROR collecting tests/test_x.py\\nE   ModuleNotFoundError: No module named '"
                                   "\\''nosuchpkg'\\''\\n'; exit 2;;\nesac\n")
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*git, "add", "check.sh"], check=True)
    subprocess.run([*git, "commit", "-qm", "check"], check=True)
    cmd = "sh check.sh"
    assert not checkspill.host_path(cmd)
    p.set_config("delivery.push_checks", [cmd])
    lane = _spill_lane(p, repo)
    threading.Timer(6, lane.close).start()
    with pytest.raises(SystemExit):
        cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["spilled_to"] == "check-host-b", rec
    assert "nosuchpkg not installed" in rec["env_problem"] and "check-host-b" in rec["env_problem"], rec
    fail = json.loads((run / "check_failure.json").read_text())
    assert "nosuchpkg" in fail["env_problem"] and "env_note" not in fail, fail   # the env's failure, not the change's
    text = (run / "checks.log").read_text()
    assert text.count("spilling to check-host-a") == 1 and text.count("spilling to check-host-b") == 1, text
    assert "nosuchpkg not installed for the python the check ran with, a fault of the spill env" in text, text
    assert "(once: an import failure runs again once per head)" in text, text
    assert "an import failure runs again once per head, so this result counts; not run again" in text, text
    ev = p.db.q("SELECT severity, text FROM events WHERE fingerprint LIKE 'checkspill:env:%'")
    assert len(ev) == 1 and "failing at import" in ev[0]["text"], ev   # the coordinator is told at once, once
    # The record: both hosts, under this checks config; a fixed config tries them again.
    cfg = p.config()
    recs = checkspill.fails(p.state, rec["head"], cli._commands_hash([cmd]))
    assert [r["host"] for r in recs] == ["check-host-a", "check-host-b"] and checkspill.env_rerun_spent(recs, cfg), recs
    assert checkspill.kept_off(checkspill.fails_off("", recs, cfg)) == {"check-host-a", "check-host-b"}
    p.set_config("checks.spill_wrap_cmd", "env FIXED=1")
    assert not checkspill.env_rerun_spent(recs, p.config()) and checkspill.fails_off("", recs, p.config()) == ""
    # Unit level: the env marks and their release.
    off = checkspill.spill_off_add("", "check-host-a", False, cfg)
    assert checkspill.kept_off(off) == set() and checkspill.env_off(off, cfg) == "", "one host: not yet"
    off = checkspill.spill_off_add(off, "check-host-b", False, cfg)
    assert checkspill.kept_off(off) == set() and "check-host-a, check-host-b" in checkspill.env_off(off, cfg)
    told = []
    assert checkspill.pick(p, cfg, told.append, "pytest -q", off) is None and len(told) == 1, told
    assert any("took no run: the checks failed at import" in x for x in checkspill.pick_lines(p.name))
    assert checkspill.env_off(off, cfg, now=time.time() + checkspill.ENV_RETRY_S + 1) == "", "time passed"
    assert checkspill.env_off(off, p.config()) == "", "the checks config changed"
    got = checkspill.pick(p, p.config(), told.append, "pytest -q", off)
    try:
        assert got is not None
    finally:
        got and got.close()


def _assertion_and_import_log(n=30):
    """pytest output with `n` tests failing on assertions and one failing on an import of 'yaml'."""
    return ("=================================== FAILURES ===================================\n"
            "___________________________________ test_imp ___________________________________\n"
            "    import yaml\nE   ModuleNotFoundError: No module named 'yaml'\n"
            + "".join(f"___________________________________ test_{i} ___________________________________\n"
                      f"    assert 1 == 2\nE   AssertionError: assert 1 == 2\n" for i in range(n))
            + "=========================== short test summary info ============================\n"
            + "".join(f"FAILED tests/test_a.py::test_{i} - AssertionError: assert 1 == 2\n" for i in range(n))
            + "FAILED tests/test_a.py::test_imp - ModuleNotFoundError: No module named 'yaml'\n"
            + f"========================= {n + 1} failed, 100 passed in 1.00s =========================\n")


@_LINUX_TMP
def test_assertion_failures_not_env(env, tmp_path, monkeypatch):
    """30 tests failing on assertions beside one failing on a missing package, on a spill host: the change's
    failure, kept with its failing ids and not run again, never called a fault of the spill env; the missing
    package is one note about that host's checks env."""
    import threading
    from ttp import cli, push
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    out = _assertion_and_import_log()
    top = str(repo)
    assert push.env_problem(out, top) and len(push.assertion_failures(out, top)) == 30, "the reproduction"
    only = out.replace("FAILED tests/test_a.py::test_", "PASSED tests/test_a.py::test_").replace(
        "PASSED tests/test_a.py::test_imp", "FAILED tests/test_a.py::test_imp").replace("31 failed", "1 failed")
    assert push.env_problem(only, top) and push.assertion_failures(only, top) == [], "the import alone: env's"
    (tmp_path / "out.txt").write_text(out)
    script = tmp_path / "check.sh"
    script.write_text(f'case "$(pwd)" in */ttp-check.*) cat {tmp_path / "out.txt"}; exit 1;; esac\n')
    cmd = f"sh {script}"
    p.set_config("delivery.push_checks", [cmd])
    lane = _spill_lane(p, repo)
    threading.Timer(6, lane.close).start()
    with pytest.raises(SystemExit):
        cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["spilled_to"] == "check-host-a", rec
    fail = json.loads((run / "check_failure.json").read_text())
    assert "env_problem" not in fail and "yaml not installed" in fail["env_note"], fail   # a test failure of the change
    assert "test_a.py::test_29" in fail["output"], fail
    text = (run / "checks.log").read_text()
    assert "spilling to check-host-b" not in text and "running them again" not in text, text
    assert "a fault of the spill env" not in text, text
    assert "with tests that failed on their own: tests/test_a.py::test_0" in text and "and 20 more" in text, text
    note = json.loads((p.state / cli.CHECKS_ENV).read_text())
    assert note["also"] is True and "yaml" in note["problem"] and "check-host-a" in note["problem"], note


@_LINUX_TMP
def test_same_failure_same_host_not_rerun(env, tmp_path, monkeypatch):
    """Checks that failed on a spill host run again here (only a pass there counts); a later run of the same
    head and commands is never sent to that host again, where it would only fail the same way."""
    import threading
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    script = tmp_path / "check.sh"
    script.write_text('case "$(pwd)" in */ttp-check.*)\n'
                      "  printf 'FAILED tests/test_a.py::test_x - AssertionError\\n1 failed in 1s\\n'; exit 1;;\nesac\n")
    cmd = f"sh {script}"
    p.set_config("delivery.push_checks", [cmd])
    for n in (1, 2):
        lane = _spill_lane(p, repo)
        threading.Timer(3, lane.close).start()
        cli.main(["checks", "--fresh"])
        rec = json.loads((run / "checks.json").read_text())
        assert rec["passed"] is True and "spilled_to" not in rec, rec
        text = (run / "checks.log").read_text()
        assert text.count("spilling to check-host-a") == 1, (n, text)   # the first run's only, never again
    recs = checkspill.fails(p.state, rec["head"], cli._commands_hash([cmd]))
    assert [(r["host"], r["env"]) for r in recs] == [("check-host-a", "")] and recs[0]["sig"].startswith("ids:"), recs
    assert checkspill.fails(p.state, "0" * 40, cli._commands_hash([cmd])) == [], "another head: free to go there"


def test_a_spilled_checks_fixed_n_is_lowered_to_the_hosts_worker_grant(tmp_path):
    """On a spill host a pytest check's fixed `-n N` above the grant checkspill.workers_env sends is
    lowered to it; with no grant the command runs as written."""
    from ttp import checkspill
    repo = _rc_repo(tmp_path)
    for literal, want in ((checkspill.workers_env(2), "pytest -n 2 -q"), (None, "pytest -n 8 -q")):
        rc, out = _rc_run(tmp_path, repo, "echo pytest -n 8 -q", host="check-host-a", literal=literal)
        assert rc == 0 and want in out.splitlines(), out
        assert ("lowered to -n 2" in out) == bool(literal), out


def test_spill_run_sends_what_may_spill_to_a_spill_host_and_records_why_a_run_stays_here(env, tmp_path,
                                                                                      monkeypatch, capsys):
    """checkspill.spill_run (the push batch's and an after-landing suite's way out): a pytest check
    spills with its worker grant and remembers the check it came from, an agent CLI check stays here;
    spill first off, a host list kept off or no host free keeps the run here, and `ttp checks status`
    says why until the next spill clears it."""
    from ttp import checkspill, cli, push, testslots
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    said = []
    cmds = push.check_list(["pytest -q tests", "claude plugin validate . --strict"])
    claim, out = checkspill.spill_run(p, p.config(), cmds, said.append, "batch 1 checks")
    try:
        assert claim is not None and claim.host == "check-host-a", said
        assert push.remote_host(out[0]) == "check-host-a" and out[0].was is cmds[0]
        assert testslots.HELD_ENV in dict(out[0].literal_env or ()), out[0]
        assert not push.remote_host(out[1]), "an agent CLI check never leaves the project host"
    finally:
        claim.close()
    assert checkspill.here_lines(p.name) == []
    p.set_config("delivery.checks_spill_first", False)
    assert checkspill.spill_run(p, p.config(), cmds, said.append, "batch 2 checks") == (None, cmds)
    assert any("spill first is off" in ln and "(batch 2 checks)" in ln for ln in checkspill.here_lines(p.name))
    p.set_config("delivery.checks_spill_first", True)
    assert checkspill.spill_run(p, p.config(), cmds, said.append, "batch 3", off="1")[0] is None
    assert any("kept off every spill host" in ln for ln in checkspill.here_lines(p.name))
    p.set_config("delivery.checks_spill_hosts", ["down"])
    assert checkspill.spill_run(p, p.config(), cmds, said.append, "batch 4")[0] is None
    lines = checkspill.here_lines(p.name)
    assert len(lines) == 1 and "no spill host could take them" in lines[0], lines
    monkeypatch.chdir(repo)
    capsys.readouterr()
    for args in (["checks", "status"], ["checks", "status", "--json"]):
        with pytest.raises(SystemExit) as e:
            cli.main(args)
        assert e.value.code == 0
    text, js = capsys.readouterr().out.split("\n{", 1)
    assert lines[0] in text, text
    assert "no spill host could take them" in json.loads("{" + js)["here"][0]["why"]
    p.set_config("delivery.checks_spill_hosts", ["check-host-a"])
    claim, _ = checkspill.spill_run(p, p.config(), cmds, said.append, "batch 5")
    claim.close()
    assert checkspill.here_lines(p.name) == [], "a run that spilled has no reason to show here"


def _spill_batch(env, tmp_path, monkeypatch, cmd):
    """A push queue project (_pq) whose batch checks may spill to the one allowed host check-host-a."""
    from ttp import checkspill, machines
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(checkspill, "_PROBE", _idle_probe())
    s.p.charter_path.write_text("# Charter\n## Resources\n- check-host-a: a check host\n")
    machines.add("check-host-a")
    s.p.set_config("delivery.push_checks", [cmd])
    s.p.set_config("delivery.checks_spill_hosts", ["check-host-a"])
    bin_, remote = _rc_fake_ssh(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    return s


@pytest.mark.parametrize("fault", [False, True])
def test_a_push_batch_spills_its_checks_first_and_a_spill_host_fault_reruns_them_here(env, tmp_path, monkeypatch,
                                                                                        fault):
    """delivery.checks_spill_first: the push batch's suite goes to a spill host before the project host,
    under a claim freed after; a spill host's fault (the link drops twice) keeps that host off and the
    check runs here, and the batch still pushes."""
    from ttp import batch, checkspill, push, pushq
    log = tmp_path / "where.log"
    s = _spill_batch(env, tmp_path, monkeypatch, f"pwd >> {log}")
    if fault:
        monkeypatch.setenv("FAKE_SSH_DROP", str(log))
    monkeypatch.setattr(pushq, "batch_argv", lambda marker: [sys.executable, "-c", "pass"])
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    marker = pathlib.Path(s.p.db.one("SELECT marker FROM push_batches WHERE id=?", (bid,))["marker"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("TTP_RUN_DIR", push._read(marker)["check_report_dir"])
    assert batch.run_batch(s.p, marker) == 0
    assert push._read(marker)["outcome"] == "pushed", push._read(marker)
    where = log.read_text().splitlines()
    assert where and "/ttp-check." in where[0], where
    if fault:
        assert len(where) == 3 and "/ttp-check." not in where[-1], where
    else:
        assert len(where) == 1, where
    assert checkspill.holders(s.p.name) == [], "the claim ends with the batch"


def _spill_push(env, tmp_path, monkeypatch, cmd):
    """A _push_setup project whose push checks (`cmd`) may spill to the one allowed host check-host-a."""
    from ttp import checkspill, machines
    p, repo, origin, other = _push_setup(env, monkeypatch, [cmd])
    monkeypatch.setattr(checkspill, "_PROBE", _idle_probe())
    p.charter_path.write_text("# Charter\n## Resources\n- check-host-a: a check host\n")
    machines.add("check-host-a")
    p.set_config("delivery.checks_spill_hosts", ["check-host-a"])
    bin_, remote = _rc_fake_ssh(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    (tmp_path / "run").mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path / "run"))
    _commit(repo, "change.txt", "new\n")
    return p, repo


@pytest.mark.parametrize("mode", ["first", "off", "fault", "pinned"])
def test_a_direct_push_spills_its_checks_first_as_ttp_checks_does(env, tmp_path, monkeypatch, mode):
    """delivery.checks_spill_first: a plain `ttp push` sends its push checks to a spill host before taking the
    local priority slot, under a claim freed after; off, they run here. A spill host's fault (the link drops)
    reruns the check here, and a check pinned to its own host keeps the run off the spill hosts."""
    from ttp import checkspill, push
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p, repo = _spill_push(env, tmp_path, monkeypatch, cmd)
    if mode == "off":
        p.set_config("delivery.checks_spill_first", False)
    if mode == "fault":
        monkeypatch.setenv("FAKE_SSH_DROP", str(log))
    checks = push.check_list([cmd])
    if mode == "pinned":
        checks = push.check_list([{"run": cmd, "host": "local"}, {"run": f"echo pinned >> {log}", "host": "check-host-a"}])
    said: list[str] = []
    assert push.push(repo, "origin", "proj", checks, 1, say=said.append) == 0, said
    assert _git_out(repo, "rev-parse", "origin/proj") == _git_out(repo, "rev-parse", "HEAD")
    where = log.read_text().splitlines()
    if mode == "first":
        assert len(where) == 1 and "/ttp-check." in where[0], where
    elif mode == "fault":
        assert len(where) >= 2 and "/ttp-check." in where[0] and "/ttp-check." not in where[-1], where
        assert any("running it here" in m for m in said), said
    elif mode == "pinned":
        assert "/ttp-check." not in where[0] and "pinned" in where, where
    else:
        assert where == [str(repo.resolve())] or where == [str(repo)], where
    assert checkspill.holders(p.name) == [], "the claim ends with the push"


def _busy_push(env, tmp_path, monkeypatch, always: bool):
    """A _spill_push project with two spill hosts whose push check runs under a repo wrapper that declines the
    run (exit 75 and TTP_WRAP_REFUSED: a busy host) once, or `always`: (p, repo, checks, where-log)."""
    from ttp import machines, push
    log, mark = tmp_path / "where.log", tmp_path / "refused-once"
    p, repo = _spill_push(env, tmp_path, monkeypatch, f"pwd >> {log}")
    machines.add("check-host-b")
    p.set_config("delivery.checks_spill_hosts", ["check-host-a", "check-host-b"])
    (repo / "refuse.sh").write_text("#!/bin/sh\n" + ("" if always else f'test -e {mark} && exec "$@"\ntouch {mark}\n')
                                    + 'echo "spill-wrap: busy: its own soak" > "$TTP_WRAP_REFUSED"\nexit 75\n')
    (repo / "refuse.sh").chmod(0o755)
    _commit(repo, "refuse.sh", (repo / "refuse.sh").read_text())
    checks = push.check_list([{"run": f"pwd >> {log}", "host": "local", "spill": True, "spill_wrap_cmd": "./refuse.sh"}])
    return p, repo, checks, log


def test_a_busy_spill_host_moves_a_push_check_to_the_next_one_with_no_env_fault(env, tmp_path, monkeypatch):
    """#8418: a spill wrapper declining a push check's run (a busy host) is neither the host's fault nor the
    change's: no env:check-host fault and no hand-off to wait, that host cools off (checkspill.busy_why) and the
    check runs on the next spill host, whose pass lands the change."""
    from ttp import checkspill, envfault, push
    p, repo, checks, log = _busy_push(env, tmp_path, monkeypatch, always=False)
    said: list[str] = []
    assert push.push(repo, "origin", "proj", checks, 1, say=said.append) == 0, said
    assert _git_out(repo, "rev-parse", "origin/proj") == _git_out(repo, "rev-parse", "HEAD")
    where = log.read_text().splitlines()
    assert len(where) == 1 and "/ttp-check." in where[0], (where, said)   # ran on the second spill host
    text = "\n".join(said)
    first = next(h for h in ("check-host-a", "check-host-b") if f"spill host {h} is busy" in text)
    assert "its own soak" in text and "not pushing" not in text and "running it here" not in text, text
    assert envfault.open_faults(p) == {}, envfault.open_faults(p)
    assert checkspill.busy_why(first) and checkspill.holders(p.name) == []


def test_every_spill_host_busy_runs_the_push_check_here_ahead_of_the_projects_full_checks(env, tmp_path, monkeypatch):
    """#8418: with every spill host's wrapper declining the run, the push check runs here once, and never waits
    behind the project's other full checks (a fixed checks.test_project_max held by another run): landing
    checks go first. One line says so; no env fault."""
    from ttp import envfault, push
    p, repo, checks, log = _busy_push(env, tmp_path, monkeypatch, always=True)
    monkeypatch.setenv("TTP_TEST_PROJECT_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_PUSH_LOAD_WAIT_S", "60")
    other = push.check_slot("abc", lambda s: None, priority="other-key", proj=p, repo=repo)   # another full suite
    said: list[str] = []
    try:
        t0 = time.time()
        assert push.push(repo, "origin", "proj", checks, 1, say=said.append) == 0, said
        took = time.time() - t0
    finally:
        other.close()
    text = "\n".join(said)
    where = log.read_text().splitlines()
    assert where and "/ttp-check." not in where[-1], (where, text)   # ran here
    assert "no spill host could take" in text and "landing checks go first" in text, text
    assert "stayed full" not in text and "not pushing" not in text and took < 45, (took, text)
    assert envfault.open_faults(p) == {}


def test_a_real_spill_host_fault_on_a_push_check_still_records_the_env_fault(env, tmp_path, monkeypatch):
    """#8418: a real spill host fault (ssh drops) keeps the env-fault path: the fault is recorded, the check
    runs here, and the push says what happens in one line (no "not pushing" before running it anyway)."""
    from ttp import envfault, push
    log = tmp_path / "where.log"
    p, repo = _spill_push(env, tmp_path, monkeypatch, f"pwd >> {log}")
    monkeypatch.setenv("FAKE_SSH_DROP", str(log))
    said: list[str] = []
    assert push.push(repo, "origin", "proj", push.check_list([f"pwd >> {log}"]), 1, say=said.append) == 0, said
    text = "\n".join(said)
    assert any("check-host-a" in k for k in envfault.open_faults(p)), envfault.open_faults(p)
    assert "running it here" in text and "not pushing" not in text and "is busy" not in text, text


def test_a_busy_spill_host_cools_off_briefly_and_is_kept_off_for_the_run():
    """#8418: busy_add marks a host whose wrapper declined a run for checkspill.BUSY_COOL_S; spill_off_add
    with busy keeps it off the rest of the run at once. remotecheck.wrap_busy reads only a refusal, never a
    missing wrapper."""
    from ttp import checkspill, remotecheck
    checkspill.busy_add("check-host-a", "its own soak", now=1000.0)
    assert "its own soak" in checkspill.busy_why("check-host-a", now=1010.0)
    assert checkspill.busy_why("check-host-a", now=1000.0 + checkspill.BUSY_COOL_S + 1) == ""
    assert checkspill.busy_why("check-host-b", now=1010.0) == ""
    off = checkspill.spill_off_add("", "check-host-a", False, busy=True)
    assert "check-host-a" in checkspill.kept_off(off, {})
    refused = (f"environment fault {remotecheck.SIG}h1: the spill wrapper 'sh' refused the run there "
               f"{remotecheck.WRAP_SETTING}: spill-wrap: busy: x (exit 75 and {remotecheck.WRAP_REFUSED_ENV}), so it")
    missing = (f"environment fault {remotecheck.SIG}h1: the spill wrapper 'sh' is missing or not executable there "
               f"{remotecheck.WRAP_SETTING}, so the check did not run")
    assert remotecheck.wrap_busy(refused) == ("h1", "spill-wrap: busy: x")
    assert remotecheck.wrap_busy(missing) == ("", "")


def test_push_own_spills_its_checks_first_too(env, tmp_path, monkeypatch):
    """`ttp push --own` (publish) takes the same spill-first route as a direct push."""
    from ttp import checkspill, push
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p, repo = _spill_push(env, tmp_path, monkeypatch, cmd)
    base = _git_out(repo, "rev-parse", "origin/proj")
    _git_out(repo, "checkout", "-q", "-b", "own")
    _commit(repo, "own.txt", "own\n")
    said: list[str] = []
    assert push.publish(repo, "origin", "own", push.check_list([cmd]), say=said.append, base=base, p=p) == 0, said
    where = log.read_text().splitlines()
    assert len(where) == 1 and "/ttp-check." in where[0], where
    assert checkspill.holders(p.name) == []



@pytest.mark.parametrize("route", ["push", "own"])
@pytest.mark.parametrize("kind", ["pre_existing", "new"])
def test_a_spilled_push_check_that_fails_goes_through_the_tests_gate_as_its_local_original(
        env, tmp_path, monkeypatch, route, kind):
    """A push check spill first sent to a spill host still meets the base-vs-head tests gate (_gate_here), as
    the push batch judges one: its failing tests rerun here as the local check. A test failing on the base
    too is pre-existing and the push (direct or --own) goes; one failing only on the head still fails it."""
    from ttp import checkspill, push
    p, repo = _spill_push(env, tmp_path, monkeypatch, _GATE_FAIL)
    body = _GATE_PRE + "\n\ndef test_x():\n    pass\n"
    (repo / "tests").mkdir(exist_ok=True)
    _commit(repo, "tests/test_gate.py", body)
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    base = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "checkout", "-q", "-b", "own")
    _commit(repo, "tests/test_gate.py", body.replace("def test_x():\n    pass", "def test_x():\n    assert False")
            if kind == "new" else body + "\n\ndef helper():\n    return 1\n")
    head = _git_out(repo, "rev-parse", "HEAD")
    said: list[str] = []
    checks = push.check_list([_GATE_FAIL])
    rc = (push.push(repo, "origin", "proj", checks, 1, say=said.append) if route == "push" else
          push.publish(repo, "origin", "own", checks, say=said.append, base=base, p=p))
    text = "\n".join(said)
    assert "check-host-a" in text, text   # it ran there first
    assert "judging the" in text and "tests/test_gate.py::test_pre" in text, text
    target = "origin/proj" if route == "push" else "origin/own"
    if kind == "pre_existing":
        assert rc == 0, text
        assert "pre-existing on the base" in text and _git_out(repo, "rev-parse", target) == head
    else:
        assert rc == push.CHECKS_FAILED, text
        assert "new on" in text and "tests/test_gate.py::test_x" in text, text
        got = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", target],
                             capture_output=True, text=True).stdout.strip()
        assert got != head, "nothing pushed"
    assert checkspill.holders(p.name) == [], "the claim ends with the push"


@pytest.mark.parametrize("kind", ["pre_existing", "flaky"])
def test_a_spilled_push_batch_suite_is_judged_as_its_local_original(env, tmp_path, monkeypatch, kind):
    """A spilled suite's failing test reruns here at the head, then on the tip, as the local check's do
    (Batch._judged): one failing on the tip too is pre-existing and one passing on its rerun is flaky
    (quarantined); neither blocks the batch."""
    from ttp import batch, push, pushq
    count = tmp_path / "runs.txt"
    body = "assert False" if kind == "pre_existing" else (
        f"import pathlib\n    c = pathlib.Path({str(count)!r})\n    n = len(c.read_text()) if c.exists() else 0\n"
        "    c.write_text('x' * (n + 1))\n    assert n")
    s = _spill_batch(env, tmp_path, monkeypatch, "python3 -m pytest -q -p no:cacheprovider test_g.py")
    (s.other / "test_g.py").write_text(f"def test_g():\n    {body}\n")
    _git_out(s.other, "add", "test_g.py")
    _git_out(s.other, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "g")
    _git_out(s.other, "push", "-q", "origin", "HEAD:proj")
    monkeypatch.setattr(pushq, "batch_argv", lambda marker: [sys.executable, "-c", "pass"])
    _pq_hand_off(env, s)
    marker = pathlib.Path(s.p.db.one("SELECT marker FROM push_batches WHERE id=?", (_pq_batch(s),))["marker"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("TTP_RUN_DIR", push._read(marker)["check_report_dir"])
    assert batch.run_batch(s.p, marker) == 0
    m = push._read(marker)
    assert m["outcome"] == "pushed", m
    [got] = m[{"flaky": "flaky_tests"}.get(kind, kind)]
    assert got["ids"] == ["test_g.py::test_g"], got
    if kind == "flaky":
        assert count.read_text() == "xx", "the spilled run failed it and its one rerun here passed"



def test_a_spilled_push_batch_check_reruns_only_here_with_its_selection(env, tmp_path, monkeypatch):
    """Only a check's first full run spills: every rerun after it (the judge's, the search's split probe:
    _ids_check) runs here, as its local original, with its selection env (_run_ids)."""
    from ttp import batch, push, pushq
    log = tmp_path / "where.log"
    s = _spill_batch(env, tmp_path, monkeypatch, f'echo "$(pwd) ${{TTP_PYTEST_SELECT:-none}}" >> {log}; '
                                                 "python3 -m pytest -q -p no:cacheprovider")
    (s.other / "test_ok.py").write_text("def test_ok():\n    pass\n")
    _git_out(s.other, "add", "test_ok.py")
    _git_out(s.other, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "ok")
    _git_out(s.other, "push", "-q", "origin", "HEAD:proj")
    monkeypatch.setattr(pushq, "batch_argv", lambda marker: [sys.executable, "-c", "pass"])
    _pq_hand_off(env, s)
    code, path, branch = _code_task(s.p, "bad")
    _commit(path, "test_g.py", "def test_g():\n    assert False\n")
    review = s.p.db.add_task("review bad", "r", kind="review", tier="standard", origin="coordinator",
                             depends_on=[code])
    _pq_hand_off(env, s, task=review, push=[{"branch": branch, "head": _git_out(path, "rev-parse", "HEAD")}])
    marker = pathlib.Path(s.p.db.one("SELECT marker FROM push_batches WHERE id=?", (_pq_batch(s),))["marker"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("TTP_RUN_DIR", push._read(marker)["check_report_dir"])
    batch.run_batch(s.p, marker)
    m = push._read(marker)
    assert m["checks"].get("id_probes"), m
    lines = log.read_text().splitlines()
    first = next(n for n, ln in enumerate(lines) if "/ttp-check." in ln)
    spilled = [ln for ln in lines if "/ttp-check." in ln]
    # The one full run only: once the search knows the failing tests, the passing entry runs no second full run.
    assert len(spilled) == 1 and spilled[0].endswith(" none"), lines
    reruns = [ln for ln in lines[first + 1:] if not ln.endswith(" none")]
    # The judge's at the head; the tip and entry 1 do not collect test_g.py, so it is not rerun there.
    assert len(reruns) >= 1, lines
    assert all("/ttp-check." not in ln and "test_g" in ln.split(" ", 1)[1] for ln in reruns), lines


def test_push_hard_tests_is_a_known_delivery_key_the_batch_reads():
    """delivery.push_hard_tests sets which failing tests a batch never excuses as the tip's own."""
    from ttp import batch, project
    assert project.unknown_key_hint("delivery.push_hard_tests") is None
    b = batch.Batch.__new__(batch.Batch)
    b.d = {}
    assert b._hard("tests/x.py::test_no_leak_in_docs") and not b._hard("tests/x.py::test_other")
    b.d = {"push_hard_tests": r"::test_other$"}
    assert b._hard("tests/x.py::test_other") and not b._hard("tests/x.py::test_no_leak_in_docs")


# A fake `tailscale`: `debug prefs` prints $FAKE_TS_PREFS, `status --json` prints $FAKE_TS_STATUS; an
# empty one exits 1, as the real command does when it cannot answer.
_FAKE_TAILSCALE = r'''#!/bin/sh
case "$1" in
  debug) out=$FAKE_TS_PREFS;;
  status) out=$FAKE_TS_STATUS;;
  set) echo set >> "$FAKE_TS_SET_LOG"; exit 0;;
  *) exit 2;;
esac
[ -n "$out" ] || { echo "failed to connect to local tailscaled" >&2; exit 1; }
printf '%s\n' "$out"
'''

_TS_CASES = {
    # (prefs, status): the case the message must name
    "logged_out": ('{"LoggedOut": true, "WantRunning": true, "RouteAll": false}', '{"BackendState": "NeedsLogin"}'),
    "routes_off": ('{"LoggedOut": false, "WantRunning": true, "RouteAll": false}', '{"BackendState": "Running"}'),
    "stopped": ('{"LoggedOut": false, "WantRunning": false, "RouteAll": true}', '{"BackendState": "Stopped"}'),
    "fine": ('{"LoggedOut": false, "WantRunning": true, "RouteAll": true}', '{"BackendState": "Running"}'),
    "broken": ("", ""),
}


def _fake_tailscale(tmp_path, monkeypatch, case=None):
    """A fake tailscale (case: a _TS_CASES key) on PATH ahead of the fake ssh; case None: no tailscale
    on PATH at all. Returns the log a `tailscale set` would write to."""
    bin_, _ = _rc_fake_ssh(tmp_path)
    ts = tmp_path / "ts-bin"
    ts.mkdir(exist_ok=True)
    log = tmp_path / "ts-set.log"
    if case is not None:
        (ts / "tailscale").write_text(_FAKE_TAILSCALE)
        (ts / "tailscale").chmod(0o755)
        monkeypatch.setenv("FAKE_TS_PREFS", _TS_CASES[case][0])
        monkeypatch.setenv("FAKE_TS_STATUS", _TS_CASES[case][1])
    monkeypatch.setenv("FAKE_TS_SET_LOG", str(log))
    monkeypatch.setenv("PATH", f"{ts}{os.pathsep}{bin_}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.delenv("TTP_TEST_NO_TAILSCALE", raising=False)
    return log


@pytest.mark.parametrize("case", ["logged_out", "routes_off", "stopped", "fine", "broken", None])
def test_an_unreachable_host_names_what_is_wrong_with_tailscale_here_and_only_offers_the_route_fix(
        tmp_path, monkeypatch, case):
    """ssh cannot reach a host: with Tailscale here, the message says which part of it is wrong. Logged
    out: sign in. Stopped: turn it on. Running and logged in with RouteAll false: no sign-in, the fix is
    `tailscale set --accept-routes=true`, offered for the user's OK and never run. Fine, failing or not
    installed: the plain ssh text, no Tailscale blame. Never a logout while Tailscale is logged in."""
    from ttp import hostcheck, netwait, remotecheck, tailnet
    log = _fake_tailscale(tmp_path, monkeypatch, case)
    why = remotecheck.reachable("down")
    assert why.startswith("ssh cannot reach down: ") and "Connection refused" in why, why
    said = tailnet.diagnose()
    if case == "logged_out":
        assert said and "logged out" in said and "sign in" in said and tailnet.FIX not in said, said
    elif case == "routes_off":
        assert "running and logged in" in said and "accept-routes is off" in said, said
        assert f"`{tailnet.FIX}`" in said and "No sign-in" in said and "user's OK" in said, said
        assert "logged out" not in said, "never a logout while Tailscale is logged in"
    elif case == "stopped":
        assert "not running" in said and "logged out" not in said and tailnet.FIX not in said, said
    else:
        assert said == "", said
    assert why.endswith(tailnet.suffix()) and (said in why if said else "ailscale" not in why), why
    # The same diagnosis in the network outage alert, replacing the generic VPN advice ...
    alert = netwait.text("UTC", 0.0, 2, ["down"], now=60.0, tailnet=said)
    assert (said in alert and "VPN" not in alert) if said else ("VPN or network sign-in" in alert), alert
    # ... and in `ttp hostcheck` when the host does not answer (ssh's 255).
    monkeypatch.setattr(hostcheck, "command", lambda *a: (["sh", "-c", "echo 'ssh: timed out' >&2; exit 255"], False))
    rc, out = hostcheck.run({}, tmp_path, "down", "uptime", [])
    assert rc == hostcheck.UNREACHABLE and "ssh: timed out" in out and (said in out if said else "ailscale" not in out)
    assert not log.exists(), "the accept-routes fix is offered, never run"


def test_the_route_diagnosis_needs_tailscale_to_say_it_is_logged_in(tmp_path, monkeypatch):
    """RouteAll false with no word on the login (status fails, prefs lack LoggedOut) blames nothing,
    and status alone saying NeedsLogin is a logout even when prefs cannot be read."""
    from ttp import tailnet
    _fake_tailscale(tmp_path, monkeypatch, "fine")
    monkeypatch.setenv("FAKE_TS_PREFS", '{"RouteAll": false}')
    monkeypatch.setenv("FAKE_TS_STATUS", "")
    assert tailnet.diagnose() == ""
    monkeypatch.setenv("FAKE_TS_PREFS", "")
    monkeypatch.setenv("FAKE_TS_STATUS", '{"BackendState": "NeedsLogin"}')
    assert "logged out" in tailnet.diagnose()
    monkeypatch.setenv("FAKE_TS_STATUS", "not json")
    assert tailnet.diagnose() == ""


def test_remote_only_defaults_to_this_machines_kind_and_is_validated(env, monkeypatch):
    """delivery.checks_remote_only: auto (unset) follows the machine's kind (a workstation: on), true or false
    overrides it; it is off without spill hosts or with spill first off; a bad value is a config problem."""
    from ttp import checkspill, coordinator, netwait, project
    monkeypatch.delenv(checkspill.REMOTE_ONLY_ENV, raising=False)
    pool = {"delivery": {"checks_spill_hosts": ["check-host-a"]}}
    for kind in (True, False):
        monkeypatch.setattr(netwait, "workstation", lambda cfg=None, kind=kind: kind)
        assert checkspill.remote_only(pool) is kind
        assert checkspill.remote_only({"delivery": {**pool["delivery"], "checks_remote_only": "auto"}}) is kind
        assert checkspill.remote_only({"delivery": {**pool["delivery"], "checks_remote_only": not kind}}) is (not kind)
    monkeypatch.setattr(netwait, "workstation", lambda cfg=None: True)
    assert not checkspill.remote_only({}), "no spill hosts: nowhere to wait for"
    assert not checkspill.remote_only({"delivery": {**pool["delivery"], "checks_spill_first": False}})
    assert any("delivery.checks_remote_only" in x
               for x in project.config_problems({"delivery": {"checks_remote_only": "maybe"}}))
    assert not project.config_problems({"delivery": {"checks_remote_only": "auto"}})
    settable = coordinator.USER_SETTABLE["delivery.checks_remote_only"]
    assert settable("auto") == "auto" and settable("on") is True and settable("false") is False
    with pytest.raises(ValueError, match="nothing was changed"):
        settable("sometimes")


def test_pick_lists_busy_hosts_apart_from_hosts_out_of_reach(env, tmp_path, monkeypatch):
    """checkspill.LAST_BUSY after a pick names the hosts skipped only as busy (full slots, a device job, the
    hook's not-now), never one ssh does not reach, nor one not allowed."""
    from ttp import checkspill
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["down", "check-host-a", "check-host-b", "nowhere"])
    why = {"check-host-a": "its 3 running check(s) fill it", "check-host-b": checkspill.DEVICE_WHY + "a job",
           "down": "ssh cannot reach it: Connection refused"}
    monkeypatch.setattr(checkspill, "probe", lambda host, beside=False: (0, why.get(host, "its probe failed (exit 1)"), ""))
    assert checkspill.pick(p, p.config(), lambda line: None) is None
    assert sorted(checkspill.LAST_BUSY) == ["check-host-a", "check-host-b"], checkspill.LAST_BUSY
    why = {h: "ssh timed out after 30 s" for h in why}
    assert checkspill.pick(p, p.config(), lambda line: None) is None
    assert checkspill.LAST_BUSY == [], "every host out of reach (or not allowed): nothing to wait for"


def test_on_a_workstation_a_heavy_run_waits_for_a_busy_spill_host_and_never_runs_here(env, tmp_path, monkeypatch):
    """Owner's order (keep heavy test work on the remote boxes): on a workstation a heavy run whose spill hosts
    are busy holds nothing here and tries them again after checks_spill_after_s; it goes to the first that has
    room, never to a place here."""
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    p.set_config("delivery.checks_remote_only", True)
    log = tmp_path / "where.log"
    p.set_config("delivery.push_checks", [f"pwd >> {log}"])
    real_pick, picks = checkspill.pick, []

    def pick(*a, **k):
        picks.append(1)
        if len(picks) == 1:   # the pool is busy at first
            checkspill.LAST_BUSY[:] = ["check-host-a"]
            return None
        return real_pick(*a, **k)
    monkeypatch.setattr(checkspill, "pick", pick)
    monkeypatch.setattr(cli, "_checks_slot_take", lambda *a, **k: pytest.fail("a heavy run took a place here"))
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-a", rec
    assert len(picks) == 2 and "/ttp-check." in log.read_text()
    assert "waiting for a busy spill host (check-host-a), not running here" in (run / "checks.log").read_text()


def test_a_workstations_wait_for_a_busy_spill_host_says_so_and_holds_no_checks_slot(env, tmp_path, monkeypatch,
                                                                                     capsys):
    """Owner's msg #303 (checks waited an hour as "queued for a host-wide checks slot ... 0 running, 0 queued"):
    a workstation's wait for a busy spill host holds no checks slot here, so its `--result` probe says it waits
    for the spill host and the queue report files it under busy spill hosts, never offering a raise of the
    host-wide checks cap."""
    from ttp import checkspill, cli, queuewait
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    p.set_config("delivery.checks_remote_only", True)
    p.set_config("delivery.push_checks", ["true"])
    real_pick, picks, said = checkspill.pick, [], []

    def pick(*a, **k):
        picks.append(1)
        if len(picks) == 1:
            checkspill.LAST_BUSY[:] = ["check-host-a"]
            return None
        return real_pick(*a, **k)
    real_cancelled = cli._checks_cancelled

    def cancelled(run_dir):
        if not said:   # the probe as a detached run's owner reads it, mid-wait
            with monkeypatch.context() as m:
                m.setattr(cli, "_read_checks_pid", lambda rd: {"pid": os.getpid()})
                m.setattr(cli, "_checks_alive", lambda info: True)
                capsys.readouterr()
                cli._checks_result(run_dir)
                said.append(capsys.readouterr().out)
        return real_cancelled(run_dir)
    monkeypatch.setattr(checkspill, "pick", pick)
    monkeypatch.setattr(cli, "_checks_cancelled", cancelled)
    cli.main(["checks", "--fresh"])
    assert said and "for a busy spill host (check-host-a)" in said[0] and "host-wide" not in said[0], said
    assert "holding no checks slot here" in said[0], said
    assert queuewait.classify("ttp checks --result", said[0]) == ("spill", "")
    assert queuewait.classify("ttp checks --result", "ttp checks: queued for a host-wide checks slot (pid 1); x") \
        == ("checks", "")
    w = {"task": 1, "queue": "spill", "res": "", "cap": 3, "running": [], "queued": [], "project": p.name}
    assert not any("host_max" in o for o in queuewait.options(p.db, w, p.config(), {}, cores=64))


@_LINUX_TMP
def test_on_a_workstation_checks_that_fail_on_two_spill_hosts_fail_without_another_run(env, tmp_path, monkeypatch):
    """Owner's msg #303: a workstation never runs heavy checks here while a spill host is busy, so a change that
    fails everywhere went from host to host, then waited for the last one for hours. Failing alike on
    checkspill.FAIL_VERDICT_HOSTS spill hosts is its verdict: it fails at once, never runs here or again."""
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"],
                                        charter=("check-host-a", "check-host-b", "check-host-c"))
    p.set_config("delivery.checks_spill_hosts", ["check-host-a", "check-host-b", "check-host-c"])
    p.set_config("delivery.checks_remote_only", True)
    log = tmp_path / "where.log"
    p.set_config("delivery.push_checks", [f"pwd >> {log}; exit 1"])
    monkeypatch.setattr(cli, "_checks_slot_take", lambda *a, **k: pytest.fail("a heavy run took a place here"))
    try:
        cli.main(["checks", "--fresh"])
    except SystemExit:
        pass
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False, rec
    where = log.read_text().splitlines()
    assert len(where) == 2 and all("/ttp-check." in w for w in where), where
    text = (run / "checks.log").read_text()
    assert "failed on spill hosts check-host-a, check-host-b (exit 1): the change's failure" in text, text
    assert checkspill.fail_hosts("a:fail,b:fault,a:fail,c:env.k.1,d:fail") == ["a", "d"]
    assert checkspill.holders(p.name) == []


def test_on_a_workstation_a_heavy_run_falls_back_here_only_when_every_spill_host_is_out_of_reach(
        env, tmp_path, monkeypatch):
    """No spill host reachable: the heavy run runs here (niced, in this machine's test slots); a light run never
    tries the spill hosts at all."""
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["down"])
    p.set_config("delivery.checks_remote_only", True)
    log = tmp_path / "where.log"
    p.set_config("delivery.push_checks", [f"pwd >> {log}"])
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and "spilled_to" not in rec, rec
    assert checkspill.LAST_BUSY == [] and "/ttp-check." not in log.read_text(), "it ran here"
    assert "waiting for a busy spill host" not in (run / "checks.log").read_text()
    monkeypatch.setattr(checkspill, "pick", lambda *a, **k: pytest.fail("a light run tried the spill hosts"))
    p.set_config("delivery.push_checks", ["sh -n /dev/null"])
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and "spilled_to" not in rec, rec


def test_workers_on_a_remote_only_workstation_are_told_to_run_tests_through_ttp_checks(env, monkeypatch):
    """The code worker prompt names `ttp checks` as the only test route only where checkspill.remote_only holds."""
    from ttp import checkspill, prompts
    on = {"delivery": {"checks_spill_hosts": ["check-host-a"], "checks_remote_only": True}}
    assert "never a plain pytest or build here" in prompts.remote_only_line(on)
    assert prompts.remote_only_line({**on, "delivery": {**on["delivery"], "checks_remote_only": False}}) == ""
    assert prompts.remote_only_line({}) == ""



def _mac_full(monkeypatch):
    """This host reads overloaded (its load guard) and its host-wide checks cap is full: a local heavy
    check could not start here now."""
    from ttp import checkcap, testslots
    monkeypatch.setattr(testslots, "load_guard", lambda: "the host's CPU is overloaded")
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkcap, "extra_max", lambda: 0)
    return checkcap.acquire("other project", priority=3)


def test_a_worker_run_beside_a_host_pinned_suite_spills_at_once_while_this_host_is_full(env, tmp_path, monkeypatch):
    """A worker's `ttp checks -- <its own pytest>` beside the project's suite pinned to a check host
    ("host"): the run spills its own check to a spill host at once, while this host reads overloaded and
    its checks cap is full; the pinned suite runs on its own host. Neither waits for a checks slot here."""
    from ttp import cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-b"])
    p.set_config("delivery.checks_spill_after_s", 600)
    log = tmp_path / "where.log"
    pinned, own = f"echo pinned $(pwd) >> {log}", f"echo own $(pwd) >> {log}"
    p.set_config("delivery.push_checks", [{"run": pinned, "host": "check-host-a"}])
    held = _mac_full(monkeypatch)
    t0 = time.time()
    try:
        cli.main(["checks", "--fresh", "--", own])
    finally:
        held.close()
    assert time.time() - t0 < 60, "no wait for the cap or the spill bound"
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-b", rec
    where = log.read_text().splitlines()
    assert len(where) == 2 and all("/ttp-check." in ln for ln in where), where
    text = (run / "checks.log").read_text()
    assert "(on check-host-a)" in text and "(on check-host-b)" in text, text


def test_a_push_batch_beside_a_host_pinned_suite_spills_and_its_light_check_takes_no_slot_here(
        env, tmp_path, monkeypatch):
    """The push train does the same: its local suite spills beside the suite pinned to a check host, and a
    light local check (claude plugin validate) runs here without the host-wide checks slot, so the batch
    lands while this host reads overloaded and its checks cap is full."""
    from ttp import batch, checkspill, push, pushq
    log = tmp_path / "where.log"
    s = _spill_batch(env, tmp_path, monkeypatch, f"echo own $(pwd) >> {log}")
    s.p.set_config("delivery.push_checks", [{"run": f"echo pinned $(pwd) >> {log}", "host": "check-host-a"},
                                            f"echo own $(pwd) >> {log}", "claude plugin validate plugins/x --strict"])
    s.p.charter_path.write_text("# Charter\n## Resources\n- check-host-a: a check host\n- check-host-b: a check host\n")
    from ttp import machines
    machines.add("check-host-b")
    s.p.set_config("delivery.checks_spill_hosts", ["check-host-b"])
    fake = tmp_path / "fakeclaude"
    fake.mkdir()
    (fake / "claude").write_text(f"#!/bin/sh\necho light $(pwd) >> {log}\n")
    (fake / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    held = _mac_full(monkeypatch)
    monkeypatch.setattr(push, "check_slot", lambda *a, **k: pytest.fail("no host-wide checks slot here"))
    monkeypatch.setattr(pushq, "batch_argv", lambda marker: [sys.executable, "-c", "pass"])
    try:
        _pq_hand_off(env, s)
        marker = pathlib.Path(s.p.db.one("SELECT marker FROM push_batches WHERE id=?", (_pq_batch(s),))["marker"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        monkeypatch.setenv("TTP_RUN_DIR", push._read(marker)["check_report_dir"])
        assert batch.run_batch(s.p, marker) == 0
    finally:
        held.close()
    assert push._read(marker)["outcome"] == "pushed", push._read(marker)
    where = log.read_text().splitlines()
    assert sorted(ln.split()[0] for ln in where) == ["light", "own", "pinned"], where
    assert all("/ttp-check." in ln for ln in where if not ln.startswith("light")), where
    assert not any("/ttp-check." in ln for ln in where if ln.startswith("light")), "the light check ran here"
    assert checkspill.holders(s.p.name) == []


def test_the_tree_copy_to_a_check_host_is_compressed_and_a_stall_is_the_hosts_fault_before_the_silence_limit(tmp_path):
    """The tree goes to the check host over a compressed ssh (a plain tar, about 3x smaller on a slow link),
    and only that copy is: the run's own session is not. A copy that stalls ends as the host's fault
    (SETUP_TIMEOUT_S) before the checks' silence limit could call it a hang of the change."""
    from ttp import checkwall, remotecheck
    repo = _rc_repo(tmp_path)
    args = tmp_path / "ssh.args"
    rc, out = _rc_run(tmp_path, repo, "true", FAKE_SSH_ARGS=str(args), FAKE_SSH_NOGIT="1", TTP_REMOTE_TREE_COPY="1")
    assert rc == 0, out
    calls = args.read_text().splitlines()
    copy = [c for c in calls if "tar -xf -" in c]
    assert len(copy) == 1 and "Compression=yes" in copy[0], calls
    assert not [c for c in calls if "Compression=yes" in c and "tar -xf -" not in c], calls
    assert remotecheck.SETUP_TIMEOUT_S < checkwall.SILENT_S


def _rc_mirror(tmp_path):
    """The fake host's mirrors: {name: [its refs]}."""
    found = {}
    for m in sorted((tmp_path / "host-home" / "check-mirrors").glob("*.git")):
        found[m.name] = subprocess.run(["git", "-C", str(m), "for-each-ref", "--format=%(refname)"],
                                       capture_output=True, text=True, check=True).stdout.split()
    return found


def _rc_sent(said):
    """MB each bundle took to the mirror, as the check printed them."""
    return [float(x) for x in re.findall(r"to box1's mirror: done in \d+ s \(([\d.]+) MB\)", said)]


def _rc_commit(repo, name, data=b"x\n"):
    (repo / name).write_bytes(data)
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", name], check=True)


def test_a_remote_check_sends_only_new_objects_to_the_hosts_mirror_and_leaves_no_ref_or_checkout(tmp_path):
    """The first check seeds the host's bare mirror (beside its checks env, keyed by the repository's root
    commits) with the whole history; a later one sends a bundle of only the new commit. Each runs in a
    fresh checkout of exactly its head; afterwards its checkout, its check ref and the bundle are gone and
    the head stays as a base ref later bundles leave out."""
    repo = _rc_repo(tmp_path, big=True)
    rc, said = _rc_run(tmp_path, repo, "git rev-parse HEAD; test -f big.bin")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert rc == 0 and head in said, said
    first = _rc_sent(said)
    assert len(first) == 1 and first[0] > 1.5, said   # the 2 MB of history, once
    (mirror, refs), = _rc_mirror(tmp_path).items()
    assert re.fullmatch(r"[0-9a-f]{16}\.git", mirror) and refs == [f"refs/heads/base/{head}"], refs
    _rc_commit(repo, "b.txt")
    rc, said = _rc_run(tmp_path, repo, "cat b.txt; test -f big.bin")
    assert rc == 0 and "x" in said, said
    second = _rc_sent(said)
    assert len(second) == 1 and second[0] < 0.01, said   # KBs: only the new commit
    assert not [r for r in _rc_mirror(tmp_path)[mirror] if "/check/" in r]
    assert _rc_left(tmp_path) == [] and not list((tmp_path / "host-home" / "check-mirrors").glob("*.bundle"))
    rc, said = _rc_run(tmp_path, repo, "cat b.txt")   # nothing new: no bundle at all
    assert rc == 0 and _rc_sent(said) == [] and "sending" not in said, said


def test_a_new_mirror_far_behind_is_seeded_in_steps_that_a_cut_short_copy_resumes(tmp_path, monkeypatch):
    """Seeding sends the history in SEED_STEP first-parent steps, each kept as a base ref there: a copy cut
    off after the first step is a host fault, and the next check sends only the rest."""
    from ttp import remotecheck
    repo = _rc_repo(tmp_path)
    for i in range(5):
        _rc_commit(repo, f"c{i}.txt")
    shas = subprocess.run(["git", "-C", str(repo), "rev-list", "--first-parent", "--reverse", "HEAD"],
                          capture_output=True, text=True).stdout.split()
    rc, said = _rc_run(tmp_path, repo, "true", TTP_REMOTE_SEED_STEP="2", FAKE_SSH_FAIL="ttp-send:refs/heads/check")
    assert rc == remotecheck.FAULT_RC
    refs = next(iter(_rc_mirror(tmp_path).values()))
    assert {f"refs/heads/base/{shas[1]}", f"refs/heads/base/{shas[3]}"} <= set(refs), (refs, said)
    rc, said = _rc_run(tmp_path, repo, "true", TTP_REMOTE_SEED_STEP="2")
    assert rc == 0 and len(_rc_sent(said)) == 1, said


def test_two_concurrent_remote_checks_share_one_mirror_and_both_pass(tmp_path):
    repo = _rc_repo(tmp_path)
    assert _rc_run(tmp_path, repo, "true")[0] == 0
    _rc_commit(repo, "b.txt")
    procs = [_rc_run(tmp_path, repo, f"cat b.txt; echo run{i}", wait=False) for i in range(2)]
    outs = [p.communicate(timeout=60)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    assert "run0" in outs[0] and "run1" in outs[1]
    assert not [r for r in next(iter(_rc_mirror(tmp_path).values())) if "/check/" in r]
    assert _rc_left(tmp_path) == []


def test_a_slow_copy_prints_progress_and_an_overrun_is_a_host_fault_not_a_timeout(tmp_path):
    """A copy prints how far it got while it runs (the checks' silence limit never calls it a hang); one
    past its limit is killed and is the host's fault (75, the unreachable-host wait), never the change's
    failure or a timeout's 124."""
    from ttp import push, remotecheck
    repo = _rc_repo(tmp_path)
    rc, said = _rc_run(tmp_path, repo, "echo did-run", FAKE_SSH_SLOW="ttp-send:2", TTP_REMOTE_PROGRESS_S="0.5")
    assert rc == 0 and "did-run" in said, said
    assert len(re.findall(r"sending [0-9a-f]{12} to box1's mirror: .*\(\d+ s\)", said)) >= 2, said
    _rc_commit(repo, "b.txt")
    for fake in ({}, {"FAKE_SSH_NOGIT": "1", "TTP_REMOTE_TREE_COPY": "1"}):
        slow = {"FAKE_SSH_SLOW": ("tar -xf" if fake else "ttp-send") + ":20", "TTP_REMOTE_COPY_LIMIT_S": "1"}
        t0 = time.monotonic()
        rc, said = _rc_run(tmp_path, repo, "echo did-run", **fake, **slow)
        assert rc == remotecheck.FAULT_RC and "did-run" not in said, (fake, said)
        assert "overran its limit" in said and f"{remotecheck.SIG}box1" in said and time.monotonic() - t0 < 15, said
        # the overrun is a staging fault (push.remote_fault reads it), so the host clears only once a test copy works
        assert f"{remotecheck.SIG}box1: {remotecheck.STAGE_TIMED_OUT}" in said, said
        box = type("Box", (str,), {"host": "box1"})("x")
        assert push.remote_fault(box, rc, tmp_path, said).get("staging") is True, said
    assert _rc_left(tmp_path) == []


def test_a_host_without_usable_git_gets_the_tree_over_a_compressed_ssh(tmp_path):
    """No usable git there: no mirror and, by default, no tree sent from here either (a host fault); with
    TTP_REMOTE_TREE_COPY=1 the tree goes as before (git archive over a compressed ssh)."""
    from ttp import remotecheck
    repo = _rc_repo(tmp_path)
    args = tmp_path / "args"
    rc, said = _rc_run(tmp_path, repo, "cat a.txt", FAKE_SSH_NOGIT="1")
    assert rc == remotecheck.FAULT_RC and "tree copies from here are off" in said and "tar" not in said, said
    rc, said = _rc_run(tmp_path, repo, "cat a.txt", FAKE_SSH_NOGIT="1", FAKE_SSH_ARGS=str(args),
                       TTP_REMOTE_TREE_COPY="1")
    assert rc == 0 and "a" in said and _rc_sent(said) == [], said
    assert "tree to box1 (transfer; ssh compresses it): done" in said, said
    assert "git checkout on box1 (setup): done" in said, said  # transfer and setup are timed and reported apart
    assert any("Compression=yes" in ln and "tar -xf" in ln for ln in args.read_text().splitlines())
    assert _rc_mirror(tmp_path) == {} and _rc_left(tmp_path) == []


def test_a_new_mirror_is_seeded_from_the_clone_on_the_host_that_checks_seed_names(tmp_path):
    """`delivery.checks_seed` names a clone already on the host: a new mirror fetches its branches there first
    (the clone is only read), and only the commits after the newest one both sides have are sent from here."""
    repo = _rc_repo(tmp_path)
    for i in range(6):
        _rc_commit(repo, f"s{i}.txt", os.urandom(20000))
    clone = tmp_path / "host-clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    before = subprocess.run(["git", "-C", str(clone), "for-each-ref"], capture_output=True, text=True).stdout
    _rc_commit(repo, "new.txt")
    proj = tmp_path / "proj" / "tt-project" / "harness"
    proj.mkdir(parents=True)
    (proj / "project.json").write_text(json.dumps({"delivery": {"checks_seed": {"box1": str(clone)}}}))
    rc, said = _rc_run(tmp_path, repo, "cat new.txt", TTP_PROJECT=str(tmp_path / "proj"), TTP_REMOTE_SEED_SAMPLE="1")
    assert rc == 0 and "seeded box1's mirror from" in said, said
    assert len(_rc_sent(said)) == 1 and _rc_sent(said)[0] < 0.05, said
    assert subprocess.run(["git", "-C", str(clone), "for-each-ref"], capture_output=True, text=True).stdout == before
    assert _rc_left(tmp_path) == []


def _spill_events(p, text):
    return [r["text"] for r in p.db.q("SELECT text FROM events") if text in r["text"]]


def test_a_spill_host_whose_runs_fail_where_the_head_passes_elsewhere_is_quarantined_until_lifted(
        env, tmp_path, monkeypatch):
    """Strikes: a spilled failure counts against its host only once the same head passes elsewhere; a pass
    there resets them; a head that never passes is no strike. At checks.spill_quarantine_after in a row the
    host is quarantined: pick skips it, one event and one ledger entry (auto-undo `ttp checks --spill-back`);
    lift puts it back with one event and closes the entry."""
    from ttp import checkspill, machine_ledger
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    cfg = p.config()
    sig = "OSError: no space left on device"

    def strike(head, passed_on=""):
        checkspill.strike_fail(p, "check-host-a", head, sig)
        return checkspill.strike_settle(p, p.config(), head, passed_on)

    def n(host="check-host-a"):
        return checkspill._strikes(p.db)["hosts"].get(host, {}).get("n")

    checkspill.strike_fail(p, "check-host-a", "h0", sig)   # the change's own failure: it never passes
    assert checkspill.strike_settle(p, cfg, "other") == [] and n() is None
    assert strike("h1") == [] and strike("h2") == [] and n() == 2
    assert checkspill.strike_settle(p, cfg, "h3", passed_on="check-host-a") == [] and n() == 0, "a pass resets"
    assert strike("h4") == [] and strike("h5") == [] and not checkspill.quarantined(p, "check-host-a")
    assert strike("h6") == ["check-host-a"] and "quarantined: 3 spilled runs" in checkspill.quarantined(p, "check-host-a")
    assert strike("h7") == [], "already off: no second change"
    assert len(_spill_events(p, "Spill host check-host-a quarantined")) == 1
    entry = [c for c in machine_ledger.changes(["check-host-a"]) if c.get("kind") == "spill_quarantine"]
    assert len(entry) == 1 and entry[0]["auto_undo"] and entry[0]["undo"] == "ttp checks --spill-back check-host-a"
    told = []
    got = checkspill.pick(p, cfg, told.append, "pytest -q")
    try:
        assert got is not None and got.host == "check-host-b" and "check-host-a (quarantined" in told[0], told
    finally:
        got.close()
    assert checkspill.lift(p, "check-host-a") is True and not checkspill.quarantined(p, "check-host-a")
    assert checkspill.lift(p, "check-host-a") is False, "safe to run twice"
    assert len(_spill_events(p, "Spill host check-host-a is back")) == 1
    assert not [c for c in machine_ledger.changes(["check-host-a"]) if c.get("kind") == "spill_quarantine"]
    assert n() == 0
    p.set_config("checks.spill_quarantine_after", 1)
    assert strike("h8") == ["check-host-a"], "checks.spill_quarantine_after"
    assert checkspill.quarantine_after({"checks": {"spill_quarantine_after": 0}}) == 3
    with pytest.raises(ValueError):
        checkspill.quarantine_after_of("x")



def _quarantine(p, host="check-host-a", sig="OSError: no space left on device"):
    from ttp import checkspill
    p.set_config("checks.spill_quarantine_after", 1)
    checkspill.strike_fail(p, host, "q1", sig)
    assert checkspill.strike_settle(p, p.config(), "q1") == [host]


def test_a_spill_quarantine_past_its_end_comes_back_with_its_event_and_no_strikes_left(env, tmp_path, monkeypatch):
    """A quarantine that ends by time (no known-good run passed, its ledger entry gone or unrun) ends as a lift
    does: one back event, its strikes reset, its ledger entry closed; never silently with the old strike count.
    The entry's until-probe is the known-good probe."""
    from ttp import checkspill, machine_ledger
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    _quarantine(p)
    entry = [c for c in machine_ledger.changes(["check-host-a"]) if c.get("kind") == "spill_quarantine"]
    assert entry and entry[0]["probe"] == "ttp checks --spill-probe check-host-a || exit 1", entry
    off = checkspill._strikes(p.db)["hosts"]["check-host-a"]["off"]
    assert "known-good run" in checkspill.quarantined(p, "check-host-a", float(off["until"]) - 1)
    assert checkspill.quarantined(p, "check-host-a", float(off["until"]) + 1) == ""
    h = checkspill._strikes(p.db)["hosts"]["check-host-a"]
    assert h["n"] == 0 and "off" not in h, h
    assert len(_spill_events(p, "Spill host check-host-a is back")) == 1
    assert not [c for c in machine_ledger.changes(["check-host-a"]) if c.get("kind") == "spill_quarantine"]


def test_a_spill_quarantine_ledger_entry_is_due_at_its_expiry_while_its_probe_still_says_not_yet(
        env, tmp_path, monkeypatch):
    """The 7-day backstop is real: past its --expires a spill quarantine's ledger entry is due (its auto-undo
    runs, nobody is paged) though its known-good probe never passed. Any other entry with a failing
    until-probe stays not due past its --expires."""
    from ttp import machine_ledger
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    _quarantine(p)
    entry = [c for c in machine_ledger.changes(["check-host-a"]) if c.get("kind") == "spill_quarantine"][0]
    end = float(entry["expires"])
    assert not entry.get("probe_passed")
    assert machine_ledger._due(entry, end - 1) == "" and not machine_ledger.auto_undo_due(entry, end - 1)
    assert machine_ledger._due(entry, end + 1) == "expired"
    assert machine_ledger.auto_undo_due(entry, end + 1) and machine_ledger.auto_undo_waits(entry, end + 1)
    other = {**entry, "kind": None}
    assert machine_ledger._due(other, end + 1) == "", "only the backstop kinds: a failing probe holds others"
    assert machine_ledger._due({**entry, "probe_passed": True}, end - 1), "a passed probe is due as before"


def test_the_known_good_probe_lifts_a_quarantine_only_once_its_detached_run_passes(env, tmp_path, monkeypatch):
    """`ttp checks --spill-probe`: starts the known-good run detached and exits 1 while it runs; a refusal there
    (exit 75) starts it again at once with no strike; a failure waits KNOWN_GOOD_RETRY_S; a pass lifts the host
    (exit 0, one back event)."""
    from ttp import checkspill, detach
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    cfg = p.config()
    assert checkspill.known_good(p, cfg, "check-host-a")[0] == 0, "not quarantined"
    _quarantine(p)
    starts, state = [], [("running", "")]
    rc = checkspill._known_good_rc(p, "check-host-a")
    from ttp import gatejudge
    monkeypatch.setattr(gatejudge, "_push_tip", lambda p_: (repo, "c0ffee1"))
    p.set_config("delivery.push_checks", ["python3 -m pytest -q", "claude plugin validate . --strict"])
    cfg = p.config()
    argv = []
    monkeypatch.setattr(detach, "start", lambda rc_, log, cmd, *a, **k: argv.extend(cmd) or 4242)
    assert checkspill._known_good_start(p, cfg, "check-host-a", rc) == ("c0ffee1", "")
    assert argv[:2] == ["sh", "-c"] and "--host check-host-a" in argv[2] and "--commit c0ffee1" in argv[2], argv
    assert "pytest" in argv[2] and "validate" not in argv[2], "only the checks that may spill"

    def start(p_, cfg_, alias, rc_):
        starts.append(alias)
        rc_.parent.mkdir(parents=True, exist_ok=True)
        rc_.write_text("")
        return "base1", ""
    monkeypatch.setattr(checkspill, "_known_good_start", start)
    monkeypatch.setattr(detach, "state", lambda rc_: state[0])
    now = time.time()
    assert checkspill.known_good(p, cfg, "check-host-a", now)[0] == 1 and starts == ["check-host-a"]
    assert checkspill._strikes(p.db)["hosts"]["check-host-a"]["off"]["probe"] == "base1"
    assert checkspill.known_good(p, cfg, "check-host-a", now)[0] == 1 and len(starts) == 1, "running"
    state[0] = ("ended", "75")
    assert checkspill.known_good(p, cfg, "check-host-a", now)[0] == 1 and len(starts) == 2, "refused: again now"
    assert checkspill._strikes(p.db)["hosts"]["check-host-a"]["n"] == 1, "a refusal is no strike"
    state[0] = ("ended", "1")
    assert checkspill.known_good(p, cfg, "check-host-a", rc.stat().st_mtime + 60)[0] == 1 and len(starts) == 2
    assert checkspill.quarantined(p, "check-host-a", now), "a failed known-good run keeps it out"
    later = rc.stat().st_mtime + checkspill.KNOWN_GOOD_RETRY_S + 1
    assert checkspill.known_good(p, cfg, "check-host-a", later)[0] == 1 and len(starts) == 3, "failed: again later"
    state[0] = ("ended", "0")
    code, said = checkspill.known_good(p, cfg, "check-host-a", now)
    assert code == 0 and "passed" in said and not checkspill.quarantined(p, "check-host-a", now)
    assert checkspill._strikes(p.db)["hosts"]["check-host-a"]["n"] == 0
    assert len(_spill_events(p, "Spill host check-host-a is back")) == 1
    assert checkspill.known_good(p, cfg, "check-host-a", now)[0] == 0 and len(starts) == 3


def test_a_late_config_fault_lifts_the_quarantine_its_failure_caused(env, tmp_path, monkeypatch):
    """One host failed alone and was quarantined; the same failure then shows on a second host: it was the
    project's spill setup all along, so the first host comes back (one back event)."""
    from ttp import checkspill
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    sig = "ModuleNotFoundError: No module named 'yaml'"
    _quarantine(p, sig=sig)
    assert checkspill.quarantined(p, "check-host-a")
    checkspill.strike_fail(p, "check-host-b", "q2", sig)
    assert checkspill.strike_settle(p, p.config(), "q2") == []
    assert not checkspill.quarantined(p, "check-host-a") and not checkspill.quarantined(p, "check-host-b")
    assert len(_spill_events(p, "Spill host check-host-a is back")) == 1

def test_a_failure_every_spill_host_shares_is_one_config_incident_and_quarantines_no_host(env, tmp_path, monkeypatch):
    """The spill setup broke for all hosts (the pinned env lost: the same import error everywhere, passing here):
    one high incident, no strike, no quarantine, however many runs; a second host failing differently, or a
    pass on another spill host in between, is no config fault: strikes as before."""
    from ttp import checkspill
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    cfg = p.config()
    sig = "ModuleNotFoundError: No module named 'yaml'"
    for i in range(5):
        for host in ("check-host-a", "check-host-b"):
            checkspill.strike_fail(p, host, f"h{i}", sig)
        assert checkspill.strike_settle(p, cfg, f"h{i}") == []
    hosts = checkspill._strikes(p.db)["hosts"]
    assert all(not h.get("n") and not h.get("off") for h in hosts.values()), hosts
    assert len(_spill_events(p, "Spill config fault")) == 1 and "No module named 'yaml'" in _spill_events(
        p, "Spill config fault")[0]
    p.set_config("checks.spill_quarantine_after", 1)
    cfg = p.config()
    checkspill.strike_settle(p, cfg, "x", passed_on="check-host-b")   # a spilled pass: the hosts work again
    checkspill.strike_fail(p, "check-host-a", "g1", sig)
    assert checkspill.strike_settle(p, cfg, "g1") == ["check-host-a"], "one host failing now is the host's fault"
    checkspill.lift(p, "check-host-a")
    checkspill.strike_fail(p, "check-host-a", "g2", "OSError: disk full")
    checkspill.strike_fail(p, "check-host-b", "g2", "ImportError: libfoo")
    assert sorted(checkspill.strike_settle(p, cfg, "g2")) == ["check-host-a", "check-host-b"], "different faults"


@_LINUX_TMP
def test_an_import_failure_on_two_spill_hosts_raises_one_high_event_and_no_strikes(
        env, tmp_path, monkeypatch):
    """One spill-env incident, one event: the landed env_event tells it when the import fails on a second host
    (an import failure runs again once per head, so that result counts: the run fails), and neither the hosts
    are struck nor does strike_settle raise its own config event."""
    import threading
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    script = tmp_path / "check.sh"
    script.write_text('case "$(pwd)" in */ttp-check.*)\n'
                      "  printf 'ERROR collecting tests/test_x.py\\nE   ModuleNotFoundError: No module named '"
                      "\\''nosuchpkg'\\''\\n'; exit 2;;\nesac\n")
    p.set_config("delivery.push_checks", [f"sh {script}"])
    lane = _spill_lane(p, repo)
    threading.Timer(6, lane.close).start()
    with pytest.raises(SystemExit):
        cli.main(["checks", "--fresh"])
    assert json.loads((run / "checks.json").read_text())["passed"] is False
    events = p.db.q("SELECT severity, fingerprint FROM events WHERE fingerprint LIKE 'checkspill:%' "
                    "OR fingerprint LIKE 'spill-%'")
    assert len(events) == 1 and events[0]["severity"] == "high" and events[0]["fingerprint"].startswith(
        "checkspill:env:"), [dict(e) for e in events]
    strikes = checkspill._strikes(p.db)
    assert not strikes.get("fails") and all(not h.get("n") for h in strikes["hosts"].values()), strikes
    # A strike_settle that still sees the same failure on two hosts (recorded before env_event told it)
    # adds no second event under another fingerprint, and still strikes nobody.
    cfg, sig = p.config(), "ModuleNotFoundError: No module named 'nosuchpkg'"
    for host in ("check-host-a", "check-host-b"):
        checkspill.strike_fail(p, host, "h1", sig)
    assert checkspill.strike_settle(p, cfg, "h1") == []
    assert len(p.db.q("SELECT 1 FROM events WHERE severity='high' AND (fingerprint LIKE 'checkspill:%' "
                      "OR fingerprint LIKE 'spill-%')")) == 1
    assert all(not h.get("n") for h in checkspill._strikes(p.db)["hosts"].values())


def test_fail_sig_reads_the_last_error_line_without_paths_or_numbers():
    from ttp import remotecheck
    a = "E   ModuleNotFoundError: No module named 'yaml'\nImportError while loading conftest '/tmp/ttp-check.ab12/x.py'.\n" \
        "E   ModuleNotFoundError: No module named 'yaml'\nttp checks: SomeError: ours\n"
    assert remotecheck.fail_sig(a, 4) == "ModuleNotFoundError: No module named 'yaml'"
    assert remotecheck.fail_sig("FAILED t.py::x - AssertionError: 12 != 3 in /h/a/b.py", 1) == \
        remotecheck.fail_sig("FAILED t.py::x - AssertionError: 7 != 3 in /h/c/b.py", 1)
    assert remotecheck.fail_sig("all quiet", 2) == "exit 2"


def test_fail_sig_names_the_failing_test_ids():
    """Two tests failing with the same error text read differently; the same tests on two hosts read the same;
    a whole-suite collection error keeps one signature everywhere; a long id list stays under 200 chars."""
    from ttp import remotecheck
    x = remotecheck.fail_sig("FAILED tests/a.py::x - AssertionError: assert False\n", 1)
    y = remotecheck.fail_sig("FAILED tests/b.py::y - AssertionError: assert False\n", 1)
    assert x != y and "tests/a.py::x" in x and "AssertionError: assert False" in x
    plain = "tests/a.py F\nE       assert 1 == 2\nFAILED tests/a.py::test_x - assert 1 == 2\n1 failed in 0.12s\n"
    assert remotecheck.fail_sig(plain, 1) == remotecheck.fail_sig(plain.replace("0.12", "3.4"), 1) != "exit 1"
    assert "tests/a.py::test_x" in remotecheck.fail_sig(plain, 1)
    coll = ("E   ModuleNotFoundError: No module named 'yaml'\n"
            "ERROR tests/a.py - ModuleNotFoundError: No module named 'yaml'\n"
            "ERROR tests/b.py - ModuleNotFoundError: No module named 'yaml'\n"
            "!!!!!!! Interrupted: 2 errors during collection !!!!!!!\n")
    other = coll.replace("ERROR tests/a.py", "ERROR tests/c.py").replace("tests/b.py", "tests/a.py")
    assert remotecheck.fail_sig(coll, 2) == remotecheck.fail_sig(
        "conftest '/tmp/ttp-check.9f/x.py'\n" + coll.replace("2 errors", "2 errors in 1.5s"), 2)
    assert remotecheck.fail_sig(coll, 2) != remotecheck.fail_sig(other, 2)
    assert remotecheck.fail_sig("ttp checks: FAILED tests/z.py::ours\n", 3) == "exit 3"
    many = "".join(f"FAILED tests/test_{i}_long_module_name.py::test_case_{i} - assert 0\n" for i in range(40))
    assert len(remotecheck.fail_sig(many, 1)) <= 200
    assert remotecheck.fail_sig(many, 1) != remotecheck.fail_sig(many.replace("test_case_7 ", "test_case_x "), 1)
    assert remotecheck.shared_sig(x) and not remotecheck.shared_sig("exit 1") and not remotecheck.shared_sig("")
    assert not remotecheck.shared_sig("exit None")


def test_failures_without_an_error_line_or_of_different_tests_never_read_as_a_config_fault(
        env, tmp_path, monkeypatch):
    """A bare `exit N` signature (no error line, no test id) on two hosts is two strikes, never a config fault;
    two hosts failing different tests with the same assertion text are two strikes too; a host whose
    failures carry no error line reaches the strike limit and is quarantined; the same collection-time
    ImportError on two hosts within the hour is still one config incident."""
    from ttp import checkspill, remotecheck
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    cfg = p.config()

    def n(host):
        return checkspill._strikes(p.db)["hosts"].get(host, {}).get("n")

    bare = remotecheck.fail_sig("1 failed in 0.3s\n", 1)
    assert bare == "exit 1"
    checkspill.strike_fail(p, "check-host-a", "h1", bare)
    checkspill.strike_fail(p, "check-host-b", "h2", bare)
    assert checkspill.strike_settle(p, cfg, "h1") == [] and checkspill.strike_settle(p, cfg, "h2") == []
    assert n("check-host-a") == 1 and n("check-host-b") == 1
    checkspill.strike_fail(p, "check-host-a", "h3", remotecheck.fail_sig(
        "FAILED tests/a.py::x - AssertionError: assert False\n", 1))
    checkspill.strike_fail(p, "check-host-b", "h3", remotecheck.fail_sig(
        "FAILED tests/b.py::y - AssertionError: assert False\n", 1))
    assert checkspill.strike_settle(p, cfg, "h3") == []
    assert n("check-host-a") == 2 and n("check-host-b") == 2
    assert not _spill_events(p, "Spill config fault")
    checkspill.strike_fail(p, "check-host-a", "h4", bare)
    assert checkspill.strike_settle(p, cfg, "h4") == ["check-host-a"], "no error line: still the host's fault"
    assert not _spill_events(p, "Spill config fault")
    coll = ("E   ImportError: libfoo.so: cannot open shared object file\n"
            "ERROR tests/a.py - ImportError: libfoo.so: cannot open shared object file\n")
    checkspill.strike_fail(p, "check-host-b", "h5", remotecheck.fail_sig(coll, 2))
    checkspill.strike_fail(p, "check-host-a", "h6", remotecheck.fail_sig(coll.replace("/x", "/y"), 2))
    assert checkspill.strike_settle(p, cfg, "h5") == [] and checkspill.strike_settle(p, cfg, "h6") == []
    assert len(_spill_events(p, "Spill config fault")) == 1 and n("check-host-b") == 0


def test_a_spilled_failure_strikes_its_host_once_the_run_passes_here_and_a_host_fault_does_not(
        env, tmp_path, monkeypatch):
    """`ttp checks`: a run failing on the spill host and passing here strikes that host (with the error line
    as its signature); one the host's own fault ended (exit 75) strikes nothing (a missing-package import
    failure is the spill env fault: no strike, see the one-event test)."""
    import threading
    from ttp import checkspill, cli
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    cmd = 'case "$PWD" in */ttp-check.*) echo "E   AssertionError: host-only difference"; exit 1;; esac'
    p.set_config("delivery.push_checks", [cmd])
    for fake, want in (({"FAKE_SSH_FAIL": "mktemp"}, None), ({}, 1)):
        for k, v in fake.items():
            monkeypatch.setenv(k, v)
        lane = _spill_lane(p, repo)
        threading.Timer(2.0, lane.close).start()
        cli.main(["checks", "--fresh"])
        for k in fake:
            monkeypatch.delenv(k)
        assert json.loads((run / "checks.json").read_text())["passed"] is True
        doc = checkspill._strikes(p.db)
        assert doc.get("hosts", {}).get("check-host-a", {}).get("n") == want, (fake, doc)
    assert doc["fails"][-1]["sig"] == "AssertionError: host-only difference" and doc["fails"][-1]["settled"]


def test_spill_strikes_read_malformed_state_as_none(env, tmp_path, monkeypatch):
    from ttp import checkspill
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    for bad in ([1], {"hosts": [1]}, {"hosts": {"check-host-a": "x"}}, {"hosts": {"check-host-a": {"off": "x"}}}):
        p.db.set_kv(checkspill.STRIKES_KV, bad)
        assert checkspill.quarantined(p, "check-host-a") == "" and checkspill.lift(p, "check-host-a") is False



@_GNU_FIND
def test_spill_probe_counts_only_check_folders_a_process_uses_and_removes_stale_dead_ones(tmp_path):
    """A host whose origin rebooted mid-run keeps that run's ttp-check.* folder (no EXIT trap ran): the slots
    probe counts a folder some process of the ssh user works in, and one made just now (its copy is under
    way), but not a dead one, and removes a dead one older than REAP_AFTER_MIN, so dead folders never fill
    an idle host."""
    from ttp import checkspill, remotecheck
    t = tmp_path / "remote"
    t.mkdir()
    live, fresh, dead, stale = (t / f"ttp-check.{x}0001" for x in ("live", "fresh", "dead", "stale"))
    for d in (live, fresh, dead, stale):
        d.mkdir()
    (live / "sub").mkdir()
    (live / ".git").mkdir()
    (live / remotecheck.RUN_PID).write_text("999999999")   # started (a pid no process has)
    for d, age_min in ((live, 600), (dead, checkspill.FRESH_MIN + 10), (stale, checkspill.REAP_AFTER_MIN + 10)):
        old = time.time() - age_min * 60
        os.utime(d, (old, old))
    user = subprocess.Popen(["sleep", "60"], cwd=live / "sub")   # a run's process, deeper in its folder
    try:
        out = subprocess.run(["sh", "-c", checkspill._PROBE], env={**os.environ, "TMPDIR": str(t)},
                             capture_output=True, text=True, timeout=30).stdout
        assert out.split()[:1] == ["ttp-spill"] and out.split()[3] == "2", out   # live and fresh
        assert sorted(p.name for p in t.iterdir()) == sorted(d.name for d in (live, fresh, dead)), "stale removed"
    finally:
        user.kill()
        user.wait()
    out = subprocess.run(["sh", "-c", checkspill._PROBE], env={**os.environ, "TMPDIR": str(t)},
                         capture_output=True, text=True, timeout=30).stdout
    assert out.split()[3] == "1", out   # its process gone, only the fresh folder counts


@_GNU_FIND
def test_spill_probe_keeps_a_run_whose_script_lives_though_its_processes_all_work_outside_its_folder(tmp_path):
    """A run's processes can all work outside its ttp-check.* folder (its check cd's elsewhere): the run
    script writes its pid in the folder first (remotecheck.RUN_PID), and while that pid lives the probe
    counts the folder and never removes it, however old; once it is gone, an old folder is removed."""
    from ttp import checkspill, remotecheck
    t = tmp_path / "remote"
    q = t / "ttp-check.away0001"
    (q / ".git").mkdir(parents=True)
    seen = tmp_path / "seen.pid"
    script = remotecheck._run_script(shlex.quote(str(q)), "", f"cp {remotecheck.RUN_PID} {seen}; exit 0",
                                     "ttp-remote-rc:")
    proc = subprocess.Popen(["sh", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        assert proc.wait(timeout=30) == 0   # ssh's stdin left open: closing it stops the command
    finally:
        proc.stdin.close()
    assert seen.read_text().split() == [str(proc.pid)], "the run script writes its own pid first"
    assert not q.exists(), "and removes its folder at the end"
    (q / ".git").mkdir(parents=True)
    run = subprocess.Popen(["sleep", "60"], cwd=tmp_path)   # the run's script, working outside its folder
    (q / remotecheck.RUN_PID).write_text(f"{run.pid}\n")
    old = time.time() - (checkspill.REAP_AFTER_MIN + 10) * 60
    os.utime(q, (old, old))
    probe = lambda: subprocess.run(["sh", "-c", checkspill._PROBE], env={**os.environ, "TMPDIR": str(t)},
                                   capture_output=True, text=True, timeout=30).stdout
    try:
        out = probe()
        assert out.split()[:1] == ["ttp-spill"] and out.split()[3] == "1", out
        assert q.is_dir(), "a live run's folder is kept"
    finally:
        run.kill()
        run.wait()
    out = probe()
    assert out.split()[3] == "0" and not q.exists(), out


@pytest.mark.skipif(not shutil.which("dash") and sys.platform != "linux", reason="needs a POSIX sh")
@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGINT, signal.SIGTERM])
def test_remote_check_script_stops_its_command_and_removes_its_folder_when_a_signal_ends_it(tmp_path, sig):
    """dash runs no EXIT trap when a signal ends the shell (its ssh session hung up, the host stopping it):
    the run script traps hangup, interrupt and TERM, stops its command and removes its folder anyway."""
    from ttp import remotecheck
    q = tmp_path / "ttp-check.sig00001"
    q.mkdir()
    pidf = tmp_path / "cmd.pid"
    script = remotecheck._run_script(shlex.quote(str(q)), "", f"echo $$ > {pidf}.new; mv {pidf}.new {pidf}; exec sleep 60",
                                     "ttp-remote-rc:")
    shell = shutil.which("dash") or "sh"
    proc = subprocess.Popen([shell, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        end = time.time() + 20
        while not pidf.exists() and time.time() < end:
            time.sleep(0.05)
        assert pidf.exists(), "the command started"
        cmd = int(pidf.read_text())
        proc.send_signal(sig)
        assert proc.wait(timeout=20) == 128 + sig
        assert not q.exists(), "the folder is removed"
        end = time.time() + 10
        while time.time() < end:
            try:
                os.kill(cmd, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the command was stopped")
    finally:
        proc.kill()
        proc.wait()
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pidf.read_text()), signal.SIGKILL)


def test_a_tree_copy_timeout_clears_only_once_a_test_copy_to_the_host_finishes_in_time(env, tmp_path, monkeypatch):
    """A check host whose tree copy timed out is a `staging` fault: `ttp checks --env-ok` on it stays 1 while ssh
    answers but a test copy there stalls or fails (else the waiting task wakes into the same timed-out upload),
    and 0 once the test copy lands in time. Other host faults still clear once ssh answers, with no test copy;
    the push queue's and push's callers read the same output."""
    from ttp import cli, envfault, push, remotecheck
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    bin_, remote = _rc_fake_ssh(tmp_path)
    slow = tmp_path / "slowssh"
    slow.mkdir()
    (slow / "ssh").write_text(f'#!/bin/sh\ncase "$*" in *"/sample"*) [ -n "$STAGE_SLOW" ] && exec sleep 5;; esac\n'
                              f'exec {bin_ / "ssh"} "$@"\n')
    (slow / "ssh").chmod(0o755)
    calls = tmp_path / "ssh.args"
    monkeypatch.setenv("PATH", f"{slow}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    monkeypatch.setenv("FAKE_SSH_ARGS", str(calls))
    monkeypatch.setattr(remotecheck, "STAGE_PROBE_S", 2)
    sig = remotecheck.SIG + "box1"

    class Cmd(str):
        host = "box1"
    said = (f"ttp remote check: environment fault {sig}: {remotecheck.STAGE_TIMED_OUT} after "
            f"{remotecheck.SETUP_TIMEOUT_S} s. This is the host, not the change\n")
    f = push.remote_fault(Cmd("pytest -q"), remotecheck.FAULT_RC, repo, said)
    assert f["staging"] is True and remotecheck.STAGE_TIMED_OUT in f["why"], f
    plain = push.remote_fault(Cmd("pytest -q"), remotecheck.FAULT_RC, repo, "ssh: connection lost")
    assert "staging" not in plain, plain
    assert push.remote_fault(Cmd("pytest -q"), 1, repo, said) is None
    envfault.record(p, f, "7")
    assert envfault.open_faults(p)[sig]["staging"] is True, "the record keeps it"

    monkeypatch.setenv("STAGE_SLOW", "1")   # ssh answers, the copy stalls past its limit
    assert remotecheck.reachable("box1") == ""
    assert "did not finish within 2 s" in (envfault.probe(envfault.open_faults(p)[sig]) or {}).get("why", "")
    assert cli._checks_env_ok(p, [sig]) == 1
    monkeypatch.delenv("STAGE_SLOW")
    monkeypatch.setenv("FAKE_SSH_FAIL", "/sample")   # the copy fails at once
    assert cli._checks_env_ok(p, [sig]) == 1
    monkeypatch.delenv("FAKE_SSH_FAIL")
    calls.unlink(missing_ok=True)
    assert cli._checks_env_ok(p, [sig]) == 0, "the test copy lands in time: the fault closes"
    copy = [c for c in calls.read_text().splitlines() if "/sample" in c]
    assert len(copy) == 1 and "Compression=yes" in copy[0] and "ControlPath=none" in copy[0], copy
    assert sig not in envfault.open_faults(p) and _rc_left(tmp_path) == [], "the sample is removed"

    monkeypatch.setenv("STAGE_SLOW", "1")
    calls.unlink()
    envfault.record(p, plain, "7")
    assert cli._checks_env_ok(p, [sig]) == 0, "a fault that was no copy timeout clears once ssh answers"
    assert not [c for c in calls.read_text().splitlines() if "/sample" in c]


def _health_setup(env, tmp_path, monkeypatch, hosts):
    """_spill_setup with the health probe's pinned-env lines left out (the fake host is this machine, whose
    pytest the probe imports) and its folder under the test's temp folder."""
    from ttp import runnerenv
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, hosts)
    monkeypatch.setattr(runnerenv, "remote_lines", lambda *a, **k: "")
    monkeypatch.chdir(tmp_path)
    return p


def test_a_spill_host_health_probe_imports_pytest_through_the_wrapper_and_a_refusal_is_busy(
        env, tmp_path, monkeypatch):
    """spillhealth.check over ssh: `<wrap> python3 -c 'import pytest'` exiting 0 is ok; the wrapper failing,
    missing or exiting 75 without its refusal file fails; exit 75 with the file (TTP_WRAP_REFUSED) is busy;
    an unreachable host fails naming ssh."""
    from ttp import spillhealth
    _health_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    wrap = tmp_path / "wrap.sh"
    wrap.write_text('#!/bin/sh\ncase "$(cat "$WRAP_MODE")" in\n  fail) echo boom; exit 1;;\n'
                    '  refuse) : > "$TTP_WRAP_REFUSED"; exit 75;;\n  bare75) exit 75;;\nesac\nexec "$@"\n')
    wrap.chmod(0o755)
    mode = tmp_path / "mode"
    monkeypatch.setenv("WRAP_MODE", str(mode))
    for m, state in (("ok", "ok"), ("fail", "fail"), ("refuse", "busy"), ("bare75", "fail")):
        mode.write_text(m)
        got = spillhealth.check("check-host-a", [str(wrap)])
        assert got["state"] == state, (m, got)
    mode.write_text("fail")
    assert "boom" in spillhealth.check("check-host-a", [str(wrap)])["why"]
    assert spillhealth.check("check-host-a", [str(tmp_path / "nowrap.sh")])["state"] == "fail"
    down = spillhealth.check("down", [])
    assert down["state"] == "fail" and "ssh" in down["why"], down
    assert spillhealth.check("check-host-a", [])["state"] == "ok"
    assert spillhealth.wrap_words({"checks": {"spill_wrap_cmd": "tools/wrap.sh --low"}}) == [], "a tree path"


def test_a_spill_host_failing_its_health_check_leaves_pick_until_it_passes_with_one_event(
        env, tmp_path, monkeypatch):
    """A host failing the shared health probe is quarantined (pick skips it) with one event however often it
    fails; a busy result changes nothing; a pass lifts it; a host probed by one daemon is not probed again by
    another within EVERY_S; a strikes quarantine is never lifted by a health pass."""
    from ttp import checkspill, spillhealth
    p = _health_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b", "down"])
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    cfg, now = p.config(), time.time()
    spillhealth.probe(spillhealth.targets(cfg), now)
    spillhealth.probe(spillhealth.targets(cfg), now + 1)   # another project's daemon, same hosts: shared result
    assert sorted(log.read_text().split()) == ["check-host-a", "check-host-b", "down"]
    assert spillhealth.apply(p, cfg, now) == ["down"]
    assert "health check failed (ssh failed" in checkspill.quarantined(p, "down")
    told = []
    got = checkspill.pick(p, cfg, told.append, "pytest -q")
    try:
        assert got is None or got.host != "down"
    finally:
        got and got.close()
    assert "down (quarantined: its health check failed" in " ".join(told), told
    spillhealth.probe(spillhealth.targets(cfg), now + spillhealth.EVERY_S + 1)
    assert spillhealth.apply(p, cfg, now + spillhealth.EVERY_S + 1) == []
    assert len(_spill_events(p, "Spill host down failed its health check")) == 1
    # down comes back: a busy result first changes nothing, then a pass lifts it
    res = {"down": {"state": "busy", "why": "refused"}}
    monkeypatch.setattr(spillhealth, "check", lambda h, w: res.get(h, {"state": "ok", "why": ""}))
    t = now + 2 * spillhealth.EVERY_S + 2
    spillhealth.probe(spillhealth.targets(cfg), t)
    spillhealth.apply(p, cfg, t)
    assert checkspill.quarantined(p, "down")
    res.clear()
    t += spillhealth.EVERY_S + 1
    spillhealth.probe(spillhealth.targets(cfg), t)
    assert spillhealth.apply(p, cfg, t) == ["down"] and not checkspill.quarantined(p, "down")
    assert len(_spill_events(p, "Spill host down is back")) == 1
    # a strikes quarantine stays through a passing health check
    p.set_config("checks.spill_quarantine_after", 1)
    checkspill.strike_fail(p, "check-host-a", "h1", "OSError: disk full")
    assert checkspill.strike_settle(p, p.config(), "h1", passed_on="check-host-b") == ["check-host-a"]
    assert spillhealth.apply(p, cfg, t) == [] and "spilled runs in a row" in checkspill.quarantined(p, "check-host-a")


def test_every_spill_host_failing_its_health_check_is_one_config_event_and_takes_none_out(
        env, tmp_path, monkeypatch):
    """The spill setup broken for every host at once (the same import error everywhere): one high event, no
    host quarantined, however often the probe runs."""
    from ttp import checkspill, spillhealth
    p = _health_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    monkeypatch.setattr(spillhealth, "check",
                        lambda h, w: {"state": "fail", "why": "ModuleNotFoundError: No module named 'pytest'"})
    cfg, now = p.config(), time.time()
    for i in range(3):
        t = now + i * (spillhealth.EVERY_S + 1)
        spillhealth.probe(spillhealth.targets(cfg), t)
        assert spillhealth.apply(p, cfg, t) == []
    assert not checkspill.quarantined(p, "check-host-a") and not checkspill.quarantined(p, "check-host-b")
    ev = _spill_events(p, "Every spill host failed its health check")
    assert len(ev) == 1 and "No module named 'pytest'" in ev[0], ev


def test_a_spill_host_dropping_with_the_workstation_tunnel_goes_out_and_back_at_low_and_other_faults_stay_normal(
        env, tmp_path, monkeypatch):
    """cells: spill_tunnel_flap. One spill host reached through a forwarded loopback route failing to connect is
    a tunnel flap the harness heals itself: its out and back events are low and name the ssh alias, never the
    loopback address. An auth failure on a host keeps both at normal."""
    from ttp import checkspill, machines, netwait, spillhealth
    hosts = ["spill-a", "spill-b", "spill-c"]
    p = _health_setup(env, tmp_path, monkeypatch, hosts)
    for h in hosts:
        machines.add(h)
    monkeypatch.setattr(netwait, "_ssh_config", lambda h: {"hostname": "127.0.0.1" if h == "spill-a" else h + ".example"})
    res = {"spill-a": {"state": "fail", "why": "ssh failed: Connection to 127.0.0.1 port 2224 timed out"},
           "spill-b": {"state": "fail", "why": "ssh failed: Permission denied (publickey)."}}
    monkeypatch.setattr(spillhealth, "check", lambda h, w: res.get(h, {"state": "ok", "why": ""}))
    cfg, now = p.config(), time.time()

    def round_(t):
        spillhealth.probe(spillhealth.targets(cfg), t)
        return spillhealth.apply(p, cfg, t)

    def events(kind):
        return {r["fingerprint"].split(":")[1]: (r["severity"], r["text"]) for r in p.db.q(
            "SELECT fingerprint, severity, text FROM events WHERE fingerprint LIKE ?", (kind + ":%",))}

    assert sorted(round_(now)) == ["spill-a", "spill-b"]
    out = events("spill-health")
    assert out["spill-a"][0] == "low" and "spill-a" in out["spill-a"][1] and "tunnel" in out["spill-a"][1]
    assert "127.0.0.1" not in out["spill-a"][1] and "port" not in out["spill-a"][1], out["spill-a"][1]
    assert out["spill-b"][0] == "normal" and "Permission denied" in out["spill-b"][1] and "tunnel" not in out["spill-b"][1]
    res.clear()
    assert sorted(round_(now + spillhealth.EVERY_S + 1)) == ["spill-a", "spill-b"]
    back = events("spill-back")
    assert back["spill-a"][0] == "low" and back["spill-b"][0] == "normal"
    assert not any(checkspill.quarantined(p, h) for h in hosts)


def test_every_spill_host_failing_to_connect_through_the_tunnel_is_one_low_bridge_event_not_a_config_fault(
        env, tmp_path, monkeypatch):
    """cells: spill_bridge_down. Every spill host reached through a forwarded loopback route failing to connect is
    the workstation tunnel being down: one low event naming ssh aliases (no loopback address), none taken out; a
    changed host set raises nothing new; still down after BRIDGE_NORMAL_S, one normal event; a host answering ends
    the outage. An auth or env failure on every host stays the one high config fault."""
    from ttp import checkspill, machines, netwait, spillhealth
    hosts = ["spill-a", "spill-b", "spill-c"]
    p = _health_setup(env, tmp_path, monkeypatch, hosts)
    for h in hosts:
        machines.add(h)
    loop = {"spill-a": "127.0.0.1", "spill-b": "localhost", "spill-c": "spill-c.example"}
    monkeypatch.setattr(netwait, "_ssh_config", lambda h: {"hostname": loop.get(h, h)})
    why = {"spill-a": "ssh failed: Connection to 127.0.0.1 port 2224 timed out",
           "spill-b": "ssh failed: ssh: connect to host localhost port 2223: Connection refused",
           "spill-c": "ssh failed: Connection to 127.0.0.1 port 23112 timed out"}
    res = {h: {"state": "fail", "why": w} for h, w in why.items()}
    monkeypatch.setattr(spillhealth, "check", lambda h, w: res.get(h, {"state": "ok", "why": ""}))
    cfg, now = p.config(), time.time()

    def round_(t):
        spillhealth.probe(spillhealth.targets(cfg), t)
        return spillhealth.apply(p, cfg, t)

    def events():
        return [(r["severity"], r["text"]) for r in p.db.q("SELECT severity, text FROM events WHERE fingerprint LIKE 'spill-health%'")]

    assert round_(now) == []
    assert not any(checkspill.quarantined(p, h) for h in hosts)
    (sev, text), = events()
    assert sev == "low" and "workstation tunnel is down" in text and "reconnects by itself" in text, text
    assert "spill-a, spill-b, spill-c" in text and "127.0.0.1" not in text and "port" not in text
    assert "config fault" not in text and "VPN" not in text
    # the set flaps (one host answers ssh but is busy) while the tunnel is still down: nothing new
    res["spill-b"] = {"state": "busy", "why": "its spill wrapper refused the run"}
    t = now + spillhealth.EVERY_S + 1
    assert round_(t) == [] and len(events()) == 1
    # down past BRIDGE_NORMAL_S: one normal event, once
    for i in range(2):
        t = now + spillhealth.BRIDGE_NORMAL_S + i * (spillhealth.EVERY_S + 1)
        assert round_(t) == []
    assert [s for s, _ in events()] == ["low", "normal"], events()
    assert "127.0.0.1" not in events()[1][1]
    # the hosts answer: the outage is over (no host was out, none comes back)
    res.clear()
    t += spillhealth.EVERY_S + 1
    assert round_(t) == [] and not p.db.kv(spillhealth.BRIDGE_KV)
    # a route that is not loopback, or an auth failure through the tunnel: the high config fault, once
    p.db.x("DELETE FROM events")
    res.update({h: {"state": "fail", "why": "ssh failed: Permission denied (publickey)."} for h in hosts})
    t += spillhealth.EVERY_S + 1
    assert round_(t) == []
    (sev, text), = events()
    assert sev == "high" and "a config fault" in text and "Permission denied" in text
    res.update({h: {"state": "fail", "why": "the pinned checks env could not be made: no venv"} for h in hosts[:2]})
    assert round_(t + spillhealth.EVERY_S + 1) == [] and len(events()) == 1, "a changed set: no new config event"
    assert not any(checkspill.quarantined(p, h) for h in hosts)
    # connection timeouts on a host whose route is direct: not the tunnel
    p.db.x("DELETE FROM events")
    loop["spill-a"] = "spill-a.example"
    res.update({h: {"state": "fail", "why": "ssh failed: ssh: connect to host spill-a.example port 22: Connection timed out"} for h in hosts})
    assert round_(t + 2 * (spillhealth.EVERY_S + 1)) == []
    (sev, text), = events()
    assert sev == "high" and "workstation tunnel" not in text


def test_the_daemon_probes_spill_host_health_in_a_thread_and_acts_on_it(env, tmp_path, monkeypatch):
    """Daemon.check_spill_health: the due probes run in a thread; the step takes a failing host out."""
    from ttp import checkspill, spillhealth
    from ttp import daemon as dm
    p = _health_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b"])
    monkeypatch.setattr(spillhealth, "check",
                        lambda h, w: {"state": "fail" if h == "check-host-a" else "ok", "why": "no python3"})
    monkeypatch.setenv("TTP_TEST_SPILL_HEALTH", "1")
    d = dm.Daemon(p.base)
    d.check_spill_health()
    d._spill_health.join(10)
    d._spill_health_at = 0.0
    d.check_spill_health()
    assert "health check failed (no python3)" in checkspill.quarantined(p, "check-host-a")
    assert not checkspill.quarantined(p, "check-host-b")


def test_a_paused_or_held_spill_host_gets_no_health_probe_quarantine_or_event(env, tmp_path, monkeypatch):
    """A spill host the user paused (the project's own pause or a shared hold) is not probed, and a stored failing
    result for it takes it out with no event; once the pause lifts it is probed and acted on again."""
    from ttp import checkspill, db as dbm, shared, spillhealth
    p = _health_setup(env, tmp_path, monkeypatch, ["check-host-a", "check-host-b", "down"])
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    cfg, now = p.config(), time.time()
    for pause in (lambda on: p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"down": {"reason": "lent", "by": "user"}} if on else {}),
                  lambda on: shared.update_pause("down", lambda was: {"reason": "held", "since": time.time(),
                                                                       "project": "third"} if on else None)):
        pause(True)
        assert [h for h, _ in spillhealth.targets(cfg, p)] == ["check-host-a", "check-host-b"]
        log.unlink(missing_ok=True)
        spillhealth.probe(spillhealth.targets(cfg, p), now)
        assert "down" not in log.read_text().split()
        spillhealth.probe(spillhealth.targets(cfg), now)   # another project's probe left a failing result for it
        assert spillhealth.apply(p, cfg, now) == [] and not checkspill.quarantined(p, "down")
        assert not _spill_events(p, "Spill host down failed its health check")
        pause(False)
        now += spillhealth.EVERY_S + 1
    log.unlink(missing_ok=True)
    spillhealth.probe(spillhealth.targets(cfg, p), now)
    assert "down" in log.read_text().split()
    assert spillhealth.apply(p, cfg, now) == ["down"] and checkspill.quarantined(p, "down")


def test_health_results_for_part_of_the_spill_hosts_neither_take_out_nor_lift_nor_call_a_config_fault(
        env, tmp_path, monkeypatch):
    """Four hosts, fresh results for only the two failing ones: no config event, nothing taken out, an existing
    health quarantine kept; once all four have results, the two failing ones are out with one event each and
    stay out."""
    from ttp import checkspill, machines, spillhealth
    hosts = ["check-host-a", "check-host-b", "check-host-c", "check-host-d"]
    p = _health_setup(env, tmp_path, monkeypatch, hosts)
    for h in hosts[2:]:
        machines.add(h)
    res = {"check-host-a": {"state": "fail", "why": "no python3"}, "check-host-b": {"state": "fail", "why": "no python3"}}
    monkeypatch.setattr(spillhealth, "check", lambda h, w: res.get(h, {"state": "ok", "why": ""}))
    cfg, now = p.config(), time.time()
    spillhealth.probe(spillhealth.targets(cfg, p)[:2], now)
    assert spillhealth.apply(p, cfg, now) == []
    assert not checkspill.quarantined(p, "check-host-a") and not _spill_events(p, "Every spill host failed")
    spillhealth.probe(spillhealth.targets(cfg, p)[2:], now)
    assert spillhealth.apply(p, cfg, now) == ["check-host-a", "check-host-b"]
    # a new round: the first two results are fresh again, the other two are not yet
    t = now + 3 * spillhealth.EVERY_S
    spillhealth.probe(spillhealth.targets(cfg, p)[:2], t)
    assert spillhealth.apply(p, cfg, t) == []
    assert checkspill.quarantined(p, "check-host-a") and checkspill.quarantined(p, "check-host-b")
    assert not _spill_events(p, "Every spill host failed")
    assert len(_spill_events(p, "Spill host check-host-a failed its health check")) == 1


def test_a_spill_host_whose_tree_copy_timed_out_is_kept_off_at_once_and_the_rerun_goes_to_the_next(env, tmp_path,
                                                                                                  monkeypatch):
    """cells: spill_copy_failover. One tree-copy timeout on a spill host (a stalled link) keeps that host off the
    rerun at once (checkspill.SLOW), never a second full timeout there: the checks run on the next free spill
    host, and the log records the cause with what was sent."""
    from ttp import checkspill, cli, remotecheck
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["slow-host", "check-host-b"],
                                        charter=("slow-host", "check-host-b"))
    calls = tmp_path / "ssh.log"
    monkeypatch.setenv("FAKE_SSH_SLOW", "ttp-send:20")
    monkeypatch.setenv("FAKE_SSH_SLOW_HOST", "slow-host")
    monkeypatch.setenv("FAKE_SSH_ARGS", str(calls))
    monkeypatch.setenv(remotecheck.COPY_TIMEOUT_ENV, "1")
    p.set_config("delivery.push_checks", ["true"])
    lane = _spill_lane(p, repo)
    try:
        cli.main(["checks", "--fresh"])
    finally:
        lane.close()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["spilled_to"] == "check-host-b", rec
    text = (run / "checks.log").read_text()
    assert "copying the tree timed out: " in text and "overran its limit after" in text and "MB sent" in text, text
    assert "copying the tree timed out, so that host is kept off this run" in text, text
    copies = [c for c in calls.read_text().splitlines() if "slow-host" in c and "ttp-send" in c and "cat > " in c]
    assert len(copies) == 1, "one copy there, never a second full timeout"


def test_a_copy_timeout_keeps_its_host_off_after_one_try_a_plain_host_fault_after_two(env, tmp_path, monkeypatch):
    """cells: spill_copy_failover. kept_off: a host whose tree copy timed out once (SLOW) is off the rerun, so
    with no other spill host the rerun is admitted here; a plain host fault keeps it off only after
    HOST_FAULT_TRIES."""
    from ttp import checkspill
    slow = checkspill.spill_off_add("", "h1", True, slow=True)
    assert slow == "h1:slow" and checkspill.kept_off(slow) == {"h1"}
    fault = checkspill.spill_off_add("", "h1", True)
    assert checkspill.kept_off(fault) == set()
    assert checkspill.kept_off(checkspill.spill_off_add(fault, "h1", True)) == {"h1"}
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-a"])
    told = []
    assert checkspill.pick(p, p.config(), told.append, "pytest -q", off="check-host-a:slow") is None, told
    claim = checkspill.pick(p, p.config(), told.append, "pytest -q", off="check-host-a:fault")
    assert claim is not None and claim.host == "check-host-a", told
    claim.close()


@_GNU_FIND
def test_the_spill_probe_counts_only_started_folders_a_live_process_works_in(tmp_path):
    """checkspill's slots probe: a ttp-check.* folder counts as a running check only once its run started
    (remotecheck.RUN_PID, which the real run script writes) and a live process works in it. A stalled copy
    (no pid file, its tar still waiting, past RUNNING_GRACE_MIN) and a run that died leaving its folder count as
    none; a copy made within FRESH_MIN still counts (its claim is not on the host yet)."""
    import time
    from ttp import checkspill, remotecheck
    remote = tmp_path / "t"
    remote.mkdir()
    procs = []
    try:
        for name, mark, live in (("ttp-check.run", True, True), ("ttp-check.dead", True, False),
                                 ("ttp-check.copy", False, True)):
            d = remote / name
            (d / ".git").mkdir(parents=True)
            if mark:
                (d / remotecheck.RUN_PID).write_text("999999999")   # a pid no process has: only the live process counts
            if live:
                procs.append(subprocess.Popen(["sleep", "60"], cwd=d))
            if name != "ttp-check.run":   # the stalled copy and the dead run are past the grace
                aged = time.time() - 2 * checkspill.FRESH_MIN * 60
                os.utime(d, (aged, aged))
        out = subprocess.run(["sh", "-c", checkspill._PROBE], env={**os.environ, "TMPDIR": str(remote)},
                             capture_output=True, text=True, timeout=30).stdout.split()
        assert out[0] == "ttp-spill" and out[3] == "1", out
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait()
    repo = _rc_repo(tmp_path)
    rc, said = _rc_run(tmp_path, repo, f"test -f {remotecheck.RUN_PID} && echo started-here")
    assert rc == 0 and "started-here" in said, "the run script marks its folder started once the copy is in"


def test_tree_copies_to_one_host_are_capped_for_every_project_here(env, tmp_path, monkeypatch, capsys):
    """remotecheck.upload_slot: at most UPLOADS_PER_HOST copies to one host run from this machine; the next
    waits, saying so, and is a host fault once UPLOAD_WAIT_S passes; a closed copy frees its slot, and a run
    leaves none held."""
    from ttp import remotecheck
    monkeypatch.setattr(remotecheck, "UPLOAD_TELL_S", 0)
    held = [remotecheck.upload_slot("box1", wait_s=0) for _ in range(remotecheck.UPLOADS_PER_HOST)]
    try:
        assert all(h is not None for h in held)
        assert remotecheck.upload_slot("box1", wait_s=0.1) is None
        assert "waiting for a copy slot to box1" in capsys.readouterr().out
        other = remotecheck.upload_slot("box2", wait_s=0)
        assert other is not None, "another host has its own slots"
        other.close()
        held.pop().close()
        again = remotecheck.upload_slot("box1", wait_s=0)
        assert again is not None
        again.close()
    finally:
        for h in held:
            h.close()
    repo = _rc_repo(tmp_path)
    rc, said = _rc_run(tmp_path, repo, "true")
    assert rc == 0, said
    free = [remotecheck.upload_slot("box1", wait_s=0) for _ in range(remotecheck.UPLOADS_PER_HOST)]
    assert all(h is not None for h in free), "the run's copy slot was freed"
    for h in free:
        h.close()


def test_a_host_that_cannot_make_the_checks_env_is_down_for_every_project_until_a_run_passes(env, tmp_path, monkeypatch):
    """A spill host whose pinned checks env cannot be made (no package index there) fails at once
    (FAULT_RC) and is marked down for every project on this machine: pick skips it and status says so. Once
    DOWN_RETRY_S passes one run tries it again (the mark moves on for the rest), and a run there that passes
    its env step clears it."""
    from ttp import checkspill, remotecheck, runnerenv
    assert "PIP_RETRIES=" in runnerenv.remote_lines(None, "x", "y"), "pip gives up quickly where it has no index"
    assert "PIP_DEFAULT_TIMEOUT=" in runnerenv.remote_lines(None, "x", "y")
    p, repo, run, remote = _spill_setup(env, tmp_path, monkeypatch, ["check-host-b"])
    rc, said = _rc_run(tmp_path, repo, "true", host="check-host-b", TTP_TEST_CHECKS_PYTHON_ENV=str(tmp_path / "none"))
    assert rc == remotecheck.FAULT_RC, said
    assert "pinned Python env could not be made there" in checkspill.down_why("check-host-b")
    told = []
    assert checkspill.pick(p, p.config(), told.append, "pytest -q") is None
    assert any("check-host-b" in t and "down for every project" in t for t in told), told
    assert "check-host-b" not in checkspill.LAST_BUSY, "a down host is no busy one to wait for"
    assert any("spill host check-host-b takes no checks" in ln for ln in checkspill.skip_lines()), checkspill.skip_lines()
    later = time.time() + checkspill.DOWN_RETRY_S + 1
    assert checkspill.down_why("check-host-b", now=later) == "", "one run tries it again"
    assert checkspill.down_why("check-host-b", now=later + 1), "the others still skip it"
    rc, said = _rc_run(tmp_path, repo, "true", host="check-host-b")
    assert rc == 0, said
    assert checkspill.down_why("check-host-b") == "" and not checkspill._down_file("check-host-b").exists()
