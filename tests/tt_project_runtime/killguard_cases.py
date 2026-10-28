# Worker kill guard tests (killguard.py, through hook.py's PreToolUse) of tests/test_tt_project_runtime.py,
# which runs this file in its own namespace (see _include_cases there): helpers, fixtures and imports come
# from that file, and pytest collects these tests under its name. Add new kill guard tests here.
from __future__ import annotations


@pytest.fixture
def kill_tree(tmp_path, monkeypatch):
    """A run dir whose recorded worker is this test process, and processes to aim at: an own child, an
    own child leading its own group, a pid file of each, and orphans outside the tree (one leading a
    group, one carrying the run's TTP_RUN_DIR). Every process is the test's own and killed at the end."""
    from types import SimpleNamespace
    from ttp import runner
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "child.pid").write_text(f"{os.getpid()}\n{runner.proc_start(os.getpid())}\n")
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    procs = [subprocess.Popen(["sleep", "60"]), subprocess.Popen(["sleep", "60"], start_new_session=True)]
    orphans = []

    def orphan(env, cmd="sleep 60"):
        out = subprocess.run(["sh", "-c", f"{cmd} >/dev/null 2>&1 & echo $!"], capture_output=True, text=True,
                             env={"PATH": os.environ["PATH"], **env}, timeout=30)
        orphans.append(int(out.stdout))
        return orphans[-1]
    t = SimpleNamespace(run_dir=run_dir, child=procs[0].pid, group=procs[1].pid,
                        outside=orphan({}), outside_group=orphan({}, "setsid sleep 60"),
                        by_env=orphan({"TTP_RUN_DIR": str(run_dir)}))
    (run_dir / "job.pid").write_text(f"{t.child}\n")
    (run_dir / "other.pid").write_text(f"{t.outside}\n")
    deadline = time.time() + 10   # setsid execs in the orphan: wait until it leads its own group
    while os.getpgid(t.outside_group) != t.outside_group and time.time() < deadline:
        time.sleep(0.01)
    yield t
    for p in procs:
        p.kill()
        p.wait()
    for pid in orphans:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _kill_refusal(cmd, t):
    from ttp import killguard
    return killguard.refusal(cmd, str(t.run_dir), str(t.run_dir))


def test_kill_guard_refuses_pattern_kills_and_allows_pkill_scoped_to_own_children(kill_tree):
    t = kill_tree
    for cmd in ("pkill -f sleep", "pkill sleep", "pkill -u someone", "pkill -9 -f 'sleep 60'", "/usr/bin/pkill x",
                "killall sleep", "killall -9 -u someone sleep", f"pkill -P {t.outside}", f"pkill -g {t.outside_group}",
                f"pkill -P {t.child},{t.outside} sleep"):
        why = _kill_refusal(cmd, t)
        assert why and "record `$!`" in why and "process group" in why, cmd
    assert "name or pattern" in _kill_refusal("pkill -f sleep", t)
    for cmd in (f"pkill -P {t.child}", f"pkill -TERM -P {t.child} sleep", f"pkill -fP {t.child} sleep",
                f"pkill --parent={t.child}", "pkill -P $$", f"pkill -g {t.group}", "pkill -g 0 sleep"):
        assert _kill_refusal(cmd, t) is None, cmd


def test_kill_guard_signal_zero_probes_pass_and_real_signals_are_refused(kill_tree):
    t = kill_tree
    o = t.outside
    for cmd in (f"kill -0 {o}", f"kill -s 0 {o}", f"kill -n 0 {o}", f"kill -SIG0 {o}", f"kill --signal 0 {o}",
                f"kill --signal=0 {o}", f"sudo kill -0 {o}", f"timeout 5 kill -0 {o}", f"bash -c 'kill -0 {o}'",
                "pkill -0 sleep", "pkill -SIG0 sleep", "pgrep sleep", "pgrep -f sleep"):
        assert _kill_refusal(cmd, t) is None, cmd
    for sig in ("-9", "-KILL", "-SIGKILL", "-s KILL", "-s 9", "-n 9", "--signal KILL", "--signal=KILL", "-TERM", ""):
        for wrap in ("{}", "sudo {}", "env X=1 {}", "timeout 5 {}", "bash -c '{}'", "true; {}", "true && {}", "echo | {}"):
            cmd = wrap.format(f"kill {sig} {o}".replace("  ", " "))
            assert _kill_refusal(cmd, t) is not None, cmd
    # A later -s/-n/--signal replaces signal 0 (bash and /bin/kill then send it), so these are real signals.
    for sig in ("-0 -s 9", "-0 -sKILL", "-s 0 -s 9", "-n 0 -n 9", "-0 --signal KILL", "-0 --signal=9", "-SIG0 -s TERM"):
        assert _kill_refusal(f"kill {sig} {o}", t) is not None, sig
    # Signal 0 given twice still sends nothing.
    assert _kill_refusal(f"kill -0 -s 0 {o}", t) is None
    # pkill's signal-0 exemption scans every word, before and after `--`, with no option parsing: any -<signal>
    # or --s.../--signal value other than 0 is a real signal, whatever an option before it may take as its value.
    for cmd in ("pkill -0 --signal KILL sleep", "pkill -0 --signal=9 -f sleep", "pkill -SIG0 -f x --signal TERM",
                "pkill -0 sleep --sig 9", "pkill -0 --signal 0 -f x --signal=KILL",
                "pkill -0 -q -- --signal KILL sleep", "pkill -0 --queue -- --signal=KILL -f sleep",
                "pkill -0 -F -- --signal 9 x", "pkill -0 -u -- --signal 9 x", "pkill -0 -fq -- --signal 9 x",
                "pkill -0 --pidf -- --signal 9 x", "pkill -0 --r -- --signal TERM -r S -P 1234",
                "pkill -0 --r -- --signal=9 x", "pkill -0 --si=KILL x", "pkill -0 --s 9 x", "pkill -0 -f -9 x",
                "pkill -0 x -- -KILL", "pkill -0 -f -sigterm x", "pkill -0 -f -RTMIN+2 x", "pkill -0 -SIG0 -HUP x",
                # accepted false refusals: a --signal with no value, values only an option table could explain
                "pkill -0 -- --signal", "pkill -0 -q1 -- --signal 9", "pkill -0 --queue=1 -- --signal 9"):
        assert _kill_refusal(cmd, t) is not None, cmd
    for cmd in ("pkill -0 --signal 0 sleep", "pkill -0 sleep", "pkill -0 -f somepattern", "pkill -0 --signal=0 sleep",
                "pkill -0 --signal=SIG0 sleep", "pkill -0 --sig 0 -f x", "pkill -0 -f x -- -0", f"kill -0 {o}"):
        assert _kill_refusal(cmd, t) is None, cmd



def test_kill_guard_kill_signal_zero_scans_every_word_for_signal_words(kill_tree):
    # /bin/kill parses with getopt (abbreviated long options, options after the pid), so kill's signal-0
    # exemption scans every word like pkill's: -<signal>, -s/-n (joined or with the next word) and --s...--signal
    # must all mean 0, and --ti...--timeout (util-linux kill sends a second signal) never passes.
    t = kill_tree
    o = t.outside
    for cmd in (f"/bin/kill -0 --sig KILL {o}", f"/bin/kill -0 {o} -s KILL", f"env kill -0 -q 1 -s KILL {o}",
                f"kill -0 {o} -s", f"kill -0 --sig=KILL {o}", f"kill -0 --s 9 {o}", f"kill -0 {o} -9",
                f"kill -0 {o} -sKILL", f"kill -0 {o} -n9", f"kill -0 {o} --signal", f"sudo /bin/kill -0 {o} -TERM",
                f"timeout 5 /bin/kill -0 {o} --si=TERM", f"kill -s 0 {o} -- -KILL", f"kill -0 --timeout 100 KILL {o}",
                f"kill -0 --time=100 KILL {o}", f"echo {o} | xargs kill -0 -s KILL",
                # accepted false refusal: a negative process group after kill -0 reads as a signal word
                f"kill -0 -- -{t.outside_group}"):
        assert _kill_refusal(cmd, t) is not None, cmd
    for cmd in (f"kill -0 {o}", f"/bin/kill -0 {o}", f"kill -s 0 {o}", f"kill --signal=0 {o}", f"env kill -0 {o}",
                f"/bin/kill -0 {o} -s 0", f"kill -s0 {o}", f"kill -0 --sig=SIG0 {o}", f"env kill -0 -q 1 {o}",
                f"/bin/kill -s KILL {t.child}", f"kill -0 --signal KILL {t.child}",
                f"env kill -0 -s 9 -- -{t.group}", f"kill -0 -sKILL {t.child}"):
        assert _kill_refusal(cmd, t) is None, cmd

def test_kill_guard_allows_kill_of_own_tree_only(kill_tree):
    t = kill_tree
    for cmd in (f"kill {t.child}", f"kill -9 {t.child}", f"kill -s TERM {t.child}", f"kill -- -{t.group}",
                f"kill -KILL -{t.group}", f"kill {t.by_env}", "kill 0", f"kill -0 {t.outside}", f"kill -s 0 {t.outside}",
                "kill -l", "kill -L", "kill 999999999"):
        assert _kill_refusal(cmd, t) is None, cmd
    for cmd, why in ((f"kill {t.outside}", "not this task's own process"),
                     (f"kill -9 {t.child} {t.outside}", f"pid {t.outside} is not"),
                     (f"kill -- -{t.outside_group}", f"process group {t.outside_group} holds"),
                     ("kill 1", "init process"), ("kill -- -1", "every process"), ("kill -9 -1", "every process")):
        assert why in _kill_refusal(cmd, t), cmd
    # Nothing ran: every process is still there.
    for pid in (t.child, t.group, t.outside, t.outside_group, t.by_env):
        os.kill(pid, 0)


def test_kill_guard_resolves_own_expansions_and_refuses_the_rest(kill_tree):
    t = kill_tree
    for cmd in ("sleep 9 & kill $!", "sleep 9 & kill -- -$!", 'sleep 9 & p=$!; sleep 1; kill -9 "$p"', "kill %1",
                "kill -9 %build", "kill $$", "kill ${!}", f"kill $(cat {t.run_dir}/job.pid)",
                "kill $(cat $TTP_RUN_DIR/job.pid)", f"kill `cat {t.run_dir}/job.pid`", "kill $(< job.pid)",
                "kill $(head -n1 job.pid)", "p=$(cat job.pid); kill $p"):
        assert _kill_refusal(cmd, t) is None, cmd
    for cmd, why in (("kill $PID", "cannot be resolved"), ('kill "$pid"', "cannot be resolved"),
                     ("kill $(pgrep sleep)", "name or pattern"), ("kill `pidof sleep`", "name or pattern"),
                     ("kill -9 $(ps aux | grep sleep | awk '{print $2}')", "name or pattern"),
                     ("kill $(cat other.pid)", f"pid {t.outside} is not"), ("p=$(cat other.pid); kill $p", "is not"),
                     ("kill $(cat missing.pid)", "cannot be resolved"), ("kill $(some_tool --pid)", "cannot be resolved"),
                     ("kill $((1 + 2))", "cannot be resolved"), ("p=$!; for p in 1 2; do kill $p; done", "cannot be resolved"),
                     ("while read p; do kill $p; done < pids", "cannot be resolved"),
                     ("pgrep -f sleep | xargs kill", "input the hook cannot see"),
                     ("pgrep sleep | xargs -I {} kill -9 {}", "input the hook cannot see"),
                     ("find /proc -maxdepth 1 -exec kill {} ;", "input the hook cannot see")):
        assert why in (_kill_refusal(cmd, t) or ""), cmd


def test_kill_guard_looks_through_wrappers_shells_and_lists(kill_tree):
    t = kill_tree
    for cmd in (f"sudo kill {t.outside}", f"sudo -u root kill -9 {t.outside}", "env FOO=1 pkill -f sleep",
                "timeout 5 pkill sleep", "nohup killall sleep &", f"setsid kill {t.outside}", "bash -c 'pkill -f sleep'",
                "sh -c \"kill $(pgrep sleep)\"", "eval pkill sleep", f"true; kill {t.outside}", f"make && kill {t.outside}",
                "ls || pkill sleep", f"echo hi | kill {t.outside}", "ttp lock res -- pkill -f sleep",
                "x=$(pkill -f sleep)", "echo \"$(killall sleep)\"", "bash <<'EOF'\npkill -f sleep\nEOF",
                f"flock /tmp/l kill {t.outside}", "if true; then pkill sleep; fi", "{ pkill sleep; }"):
        assert _kill_refusal(cmd, t), cmd
    # Words in data, other tools and options that only look alike pass.
    for cmd in ("git commit -m 'kill guard: pkill -f is refused'", "echo 'pkill -f sleep'", "grep -n pkill notes.md",
                "cat > notes.md <<'EOF'\npkill -f sleep\nEOF", "ttp killscan run.sh", "ttp killscan --shim /tmp/s",
                "timeout --kill-after=5 10 make", "python3 -c 'import os; print(os.kill)'", "sleep 1 # don't kill\ntrue",
                "ttp checks --detach -- 'pytest -q tests/a.py'", "ttp detach job -- make", "ttp lock res -- make"):
        assert _kill_refusal(cmd, t) is None, cmd


def test_kill_guard_parses_sudo_options_before_the_kill(kill_tree):
    # sudo -n is not nice's `-n VALUE`; options with values (-u, -p, -g) and combined flags (-nu) are sudo's own.
    t = kill_tree
    for cmd in (f"sudo -n kill {t.outside}", "sudo -n pkill -f sleep", f"sudo -nu root kill {t.outside}",
                f"sudo -p prompt kill {t.outside}", f"sudo -g grp kill {t.outside}", f"sudo --user=root kill {t.outside}",
                f"sudo --user root -n kill {t.outside}", f"sudo -n -- kill {t.outside}", "doas -u root pkill sleep",
                f"sudo -n FOO=1 kill {t.outside}", f"timeout 5 sudo -n kill {t.outside}", "sudo -n killall sleep"):
        assert _kill_refusal(cmd, t), cmd
    for cmd in ("ssh host 'sudo -n pkill -f x'", "ssh host sudo -n pkill x", "ssh host sudo -nu root killall x"):
        assert "another machine" in (_kill_refusal(cmd, t) or ""), cmd
    for cmd in (f"sudo -n kill {t.child}", f"sudo -nu root kill -9 {t.child}", "sudo -n true", "sudo -u root ls"):
        assert _kill_refusal(cmd, t) is None, cmd


def test_kill_guard_skips_redirection_fd_numbers(kill_tree):
    t = kill_tree
    for cmd in ("sleep 9 & kill $! 2>/dev/null", "kill %1 2>&1", "kill -9 $$ 1>&2", f"kill {t.child} 2>>log",
                f"kill {t.child} 0</dev/null", f"echo '2>x'; kill {t.child} 2>/dev/null"):
        assert _kill_refusal(cmd, t) is None, cmd
    for cmd, why in ((f"kill {t.outside} 2>/dev/null", f"pid {t.outside} is not"),
                     (f"kill 1 2>&1", "init process")):
        assert why in (_kill_refusal(cmd, t) or ""), cmd
    # With a space the digit is an argument again: `kill 1 >/dev/null` signals pid 1.
    assert "init process" in _kill_refusal("kill 1 >/dev/null", t)


def test_kill_guard_inside_ssh_refuses_pattern_kills_and_allows_printed_pids(kill_tree):
    t = kill_tree
    for cmd in ("ssh host 'pkill -f sleep'", "ssh -p 2222 -o BatchMode=yes host killall sleep",
                f"ssh host 'pkill -P {t.child}'", "ssh -t host 'sudo pkill sleep'", "ssh host \"bash -c 'pkill x'\"",
                "ssh host <<'EOF'\npkill -f sleep\nEOF", "ssh host bash -s <<EOF\nkillall sleep\nEOF"):
        assert "another machine" in (_kill_refusal(cmd, t) or ""), cmd
    for cmd in ("ssh host 'kill $(pgrep sleep)'", "ssh host 'kill $(cat /tmp/job.pid)'", "ssh host 'kill -9 -1'",
                "ssh host 'kill -- -1'", "ssh host 'pgrep x | xargs kill'"):
        assert _kill_refusal(cmd, t), cmd
    # The remote tree cannot be checked: a pid or group the worker's own remote job printed passes.
    for cmd in ("ssh host 'kill -- -4242'", "ssh host kill 4242 4243", "ssh host 'kill -0 4242'",
                "ssh host 'sleep 9 & kill $!'", "ssh host <<'EOF'\nkill -TERM -- -4242\nEOF", "ssh host uptime"):
        assert _kill_refusal(cmd, t) is None, cmd


def test_kill_guard_fails_closed_only_for_kill_commands(kill_tree, monkeypatch):
    from ttp import killguard
    t = kill_tree
    for cmd in ('kill "$(cat', "kill $(cat job.pid", "pkill 'sleep"):
        assert "could not parse" in _kill_refusal(cmd, t), cmd
    assert _kill_refusal('echo "unterminated', t) is None and _kill_refusal("ls $(", t) is None
    monkeypatch.setattr(killguard._Guard, "check", lambda *a, **k: 1 / 0)
    assert "could not parse" in _kill_refusal(f"kill {t.child}", t)
    assert _kill_refusal("make test", t) is None


def test_worker_pretooluse_hook_denies_kills_outside_the_task(kill_tree, monkeypatch):
    from ttp import hook
    from ttp.providers import claude
    t = kill_tree
    pre = claude.hook_settings()["hooks"]["PreToolUse"]
    assert pre[0]["matcher"].split("|")[0] == "Bash" and pre[0]["hooks"][0]["command"].endswith("-m ttp.hook PreToolUse")

    def decide(cmd):
        out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(t.run_dir)})
        return (out or {}).get("hookSpecificOutput", {})
    out = decide("pkill -f sleep")
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"].startswith("tt-project: pkill")
    assert decide(f"kill {t.outside}")["permissionDecision"] == "deny"
    assert decide("ssh host 'killall sleep'")["permissionDecision"] == "deny"
    for cmd in (f"kill {t.child}", "sleep 9 & kill $!", "kill $(cat job.pid)", "git status"):
        assert decide(cmd).get("permissionDecision") is None, cmd
    # Outside a worker run (no TTP_RUN_DIR) the hook does nothing.
    monkeypatch.delenv("TTP_RUN_DIR")
    assert hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "pkill -f sleep"}}) == (None, None)
