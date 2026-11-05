# Claude Code worker write fence tests (providers/claude.py fence_args, daemon.start_run) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file, and pytest collects these tests under its name. Add
# new write fence tests here.
from __future__ import annotations


def _claude_worker_argv(cwd="/w"):
    from ttp.providers import claude
    prov = claude.Claude()
    argv, _ = prov.build(role="worker", model="opus", effort="high", cwd=cwd, budget_usd=1.0, read_only=False,
                         schema=None, restrictions={})
    return prov, argv + ["-"]


def _settings(argv):
    assert argv.count("--settings") == 1, "a second --settings flag replaces the first (measured)"
    return json.loads(argv[argv.index("--settings") + 1])


@pytest.mark.parametrize("platform,missing,fenced", [
    ("linux", (), True), ("linux", ("bwrap",), False), ("linux", ("socat",), False),
    ("linux", ("bwrap", "socat"), False), ("darwin", ("bwrap", "socat"), True), ("win32", (), False)])
def test_claude_worker_settings_carry_the_sandbox_where_it_can_start(monkeypatch, tmp_path, platform, missing,
                                                                     fenced):
    from ttp.providers import claude
    monkeypatch.setattr(claude, "find_binary", lambda b: None if b in missing else f"/usr/bin/{b}")
    monkeypatch.setattr(claude.sys, "platform", platform)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(claude.Path, "home", lambda: tmp_path / "nohome")
    prov, argv = _claude_worker_argv()
    out, got = prov.fence_args(argv, ["/state", "/repo/.git"], cwd="/wt")
    assert got is fenced and out[-1] == "-", "the prompt on stdin stays last"
    settings = _settings(out)
    assert {k: v for k, v in settings.items() if k != "sandbox"} == _settings(argv), "the rest is kept"
    assert (prov.write_fence() == "") is fenced
    if not fenced:
        assert "sandbox" not in settings and out == argv
        assert prov.write_fence().startswith("bypassPermissions without the Bash sandbox, which needs ")
        return
    box = settings["sandbox"]
    assert box["enabled"] is True and box["autoAllowBashIfSandboxed"] is True
    assert box["allowUnsandboxedCommands"] is False, "no dangerouslyDisableSandbox retry"
    assert box["filesystem"]["allowWrite"] == ["/wt", "/state", "/repo/.git", str(tmp_path / "cache")]
    assert box["network"] == {"allowAllUnixSockets": True}, "the network is left open; the harness's sockets too"
    assert {"ssh *", "ttp lock *", "ttp devq *"} <= set(box["excludedCommands"]), "commands that need the network"
    assert "failIfUnavailable" not in box, "a sandbox that cannot start runs the worker as before, never fails it"


def test_claude_fence_off_by_config_or_without_worker_settings(monkeypatch):
    from ttp.providers import claude
    monkeypatch.setattr(claude, "find_binary", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(claude.sys, "platform", "linux")
    prov, argv = _claude_worker_argv()
    assert prov.fence_args(argv, ["/s"], on=False) == (argv, False), "providers.claude.write_fence false"
    bare = ["claude", "-p", "-"]
    assert prov.fence_args(bare, ["/s"]) == (bare, False), "a run with no worker settings (read-only) stays as is"
    broken = ["claude", "--settings", "/a/file.json", "-"]
    assert prov.fence_args(broken, ["/s"]) == (broken, False), "a settings file is left alone"
    assert prov.fence_optional and not claude.Provider.fence_optional


def test_claude_fence_survives_connector_isolation(monkeypatch):
    from ttp.providers import claude
    monkeypatch.setattr(claude, "find_binary", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(claude.sys, "platform", "linux")
    prov, argv = _claude_worker_argv()
    fenced, _ = prov.fence_args(argv, ["/s"], cwd="/wt")
    kept = prov.keep_connectors(fenced, ["some-server"])
    s = _settings(kept)
    assert s["sandbox"]["enabled"] and s["deniedMcpServers"] == [{"serverName": "some-server"}]


def _claude_launch(env, p, cwd, d=None):
    from ttp.providers import claude
    from ttp.daemon import Daemon
    d = d or Daemon(p.base)
    tid = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
    rid = d.start_run("worker", "go", "claude", "light", str(cwd), task=p.db.task(tid))
    (p.runs / str(rid) / "STOP").touch()
    spec = json.loads((p.runs / str(rid) / "run.json").read_text())
    pid = int((p.runs / str(rid) / "runner.pid").read_text().split()[0])
    try:   # the launch is what is checked: stop the runner this test started (its own session)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)
    return spec


def test_daemon_fences_claude_workers_and_observes_a_missing_dependency_once(env, monkeypatch):
    from ttp.providers import claude
    monkeypatch.setattr(claude.Claude, "binary", lambda self: "/usr/bin/true")   # never a real agent
    monkeypatch.setattr(claude.sys, "platform", "linux")
    have = {"bwrap", "socat"}
    monkeypatch.setattr(claude, "find_binary", lambda b: f"/usr/bin/{b}" if b in have else None)
    p = make(env)
    wt = env["tmp"] / "wt"
    subprocess.run(["git", "-C", str(env["repo"]), "worktree", "add", "-q", str(wt)], check=True)
    spec = _claude_launch(env, p, wt)
    allow = _settings(spec["argv"])["sandbox"]["filesystem"]["allowWrite"]
    assert allow[0] == str(wt) and str(p.state) in allow, "the worktree and the state (run dir, notes, queues)"
    assert str((env["repo"] / ".git").resolve()) in allow, "commits in a worktree write the main repo's git dir"
    assert spec["env"].get("TMPDIR"), "a fenced worker gets a temp dir of its own"
    assert spec["env"].get("TTP_FENCE_BY_NAME") == "1", "the hook refuses ssh inside a compound command"

    def observed():
        return p.db.q("SELECT title, count FROM issues WHERE source='daemon:write-fence'")
    assert not observed()
    have.discard("bwrap")
    for _ in range(2):
        spec = _claude_launch(env, p, wt)
        assert "sandbox" not in _settings(spec["argv"]), "a host without bwrap runs the worker as before"
    rows = observed()
    assert len(rows) == 1 and "needs bwrap" in rows[0]["title"] and "write_fence false" in rows[0]["title"], rows
    p.set_config("providers", {**p.config()["providers"], "claude": {**p.config()["providers"].get("claude", {}),
                                                                      "write_fence": False}})
    have.add("bwrap")
    spec = _claude_launch(env, p, wt)
    assert "sandbox" not in _settings(spec["argv"]) and not spec["env"].get("TMPDIR"), "turned off by config"
    assert not spec["env"].get("TTP_FENCE_BY_NAME"), "an unfenced worker's ssh runs anywhere"


def test_doctor_names_a_claude_fence_turned_off(env, monkeypatch, capsys):
    p = make(env)
    from ttp import cli
    from ttp.providers import base, claude
    monkeypatch.setattr(cli, "need", lambda *a: p)
    monkeypatch.setattr(base.Provider, "available", lambda self: True)
    monkeypatch.setattr(base.Provider, "binary", lambda self: "/bin/" + self.name)
    monkeypatch.setattr(claude.sys, "platform", "linux")
    monkeypatch.setattr(claude, "find_binary", lambda b: "/usr/bin/" + b)
    cli.cmd_doctor(types.SimpleNamespace(name="demo"))
    out = capsys.readouterr().out
    assert "claude (" not in out, "a host with bwrap and socat fences Claude workers"
    p.set_config("providers", {**p.config()["providers"], "claude": {"write_fence": False}})
    cli.cmd_doctor(types.SimpleNamespace(name="demo"))
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("unfenced providers"))
    assert "claude (turned off: providers.claude.write_fence is false)" in line


@pytest.mark.parametrize("cmd", [
    "cd /w && ssh host 'make'", "ssh host 'cat log' | tail -5", "ssh host true; echo ok", "ssh host a\nssh host b",
    "timeout 60 /usr/bin/ssh host x | grep y", "FOO=1 scp a host:b && echo done", "ls | xargs rsync -a h:/x",
    "ttp lock box -- ssh host x 2>&1 | tail", "echo hi && sftp host",
    # Forms the fence keeps sandboxed though only one command runs: a redirect to or from a file,
    # a substitution anywhere in the line, ssh inside a shell not run by `ttp lock ... --`.
    "ssh -o BatchMode=yes host 'bash -s' < /dev/null", "ttp lock res -- ssh host 'bash -s' < remote.sh",
    "ssh host 'bash -s' <<'EOF'\nhostname\nEOF", "ssh host 'cat log' > out.txt", "ssh host true 2>/dev/null",
    "timeout 60 rsync -a host:x . &>/dev/null", "ssh host 'echo $(hostname)'", 'ssh host "$(cat cmd.txt)"',
    "ssh host 'echo `hostname`'", "ttp lock res -- bash -c 'ssh host \"$(cat f)\"'", "bash -c 'ssh host hostname'",
    "sh -ec 'timeout 9 ssh host true'", "ttp clip -- bash -lc 'ssh host true'", "bash -c \"bash -c 'scp a host:b'\"",
    "eval 'ssh host true'",
    "ttp lock res -- ssh host true | tail", "ttp lock res -- bash -c 'ssh host true' | tail",
])
def test_fenced_hook_refuses_ssh_beside_other_commands(env, monkeypatch, cmd):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(env["tmp"])})
    why = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "on its own" in why, why
    monkeypatch.delenv("TTP_FENCE_BY_NAME")
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(env["tmp"])})
    assert not out or out["hookSpecificOutput"].get("permissionDecision") != "deny", "unfenced: ssh runs anywhere"


@pytest.mark.parametrize("cmd", [
    "ssh host 'cat log | tail -5'", "timeout 60 ssh host 'a && b'", "ttp lock box -- ssh host 'make 2>&1 | tail'",
    "ssh host cmd 2>&1", "git log --grep ssh | head -3", "ssh-keygen -l -f k.pub && echo ok",
    "git commit -m 'ssh && scp; rsync'", "cat > notes.md <<'X'\nssh a && ssh b\nX",
    "ssh host hostname", "ttp lock res -- bash -c 'ssh host hostname'", "ttp lock res -- bash wrapper.sh",
    "ttp lock res -- bash -c 'ssh host \"bash -s\" < remote.sh > out.txt'", "ssh host 'cat < f > g'",
    "ssh host x >&2", "bash -c 'echo $(hostname)' > out.txt", "ssh host bash -c 'hostname'", "bash wrapper.sh",
])
def test_fenced_hook_lets_ssh_on_its_own_and_ssh_as_data_run(env, monkeypatch, cmd):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(env["tmp"])})
    assert not out or out["hookSpecificOutput"].get("permissionDecision") != "deny", out


@pytest.mark.parametrize("cmd", ["ssh host 'bash -s' < /dev/null", "bash -c 'ssh host true'",
                                 "ttp lock res -- ssh host true | tail"])
def test_fenced_ssh_refusal_names_the_cause_and_the_wrapper_script(env, monkeypatch, cmd):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    why = _decision(hook, cmd, env["tmp"])
    assert "simple command" in why and "own loopback" in why, why
    assert "ttp lock <res> -- bash wrapper.sh" in why and "plain `bash wrapper.sh` stays fenced" in why, why
    assert "is fine)" not in why.split("(`2>&1` is fine)")[0], "ttp lock alone does not free any line"


@pytest.mark.parametrize("cmd,name", [
    ("FOO=x ssh host cmd", "ssh"), ("SSH_AUTH_SOCK=/a/b scp a host:b", "scp"), ("A=1 B=2 timeout 9 rsync -a h:x .", "rsync"),
    ("FOO=x ttp lock res -- ssh host true", "ssh"), ("FOO= /usr/bin/ssh host 'a | b'", "ssh"), ("x+=1 sftp host", "sftp"),
    ("FOO=x my-ssh host cmd", "my-ssh"), ("MY_SSH_OPTS=-v timeout 5 my-ssh host cmd", "my-ssh"),
])
def test_fenced_hook_refuses_an_assignment_prefixed_freed_command_with_advice_that_works(env, monkeypatch, cmd, name):
    """No line that starts with `VAR=value` is freed (CLI 2.1.296): it runs fenced, where hosts do not resolve."""
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    monkeypatch.setenv("TTP_FENCE_FREED", "my-ssh")
    why = _decision(hook, cmd, env["tmp"])
    assert why and f"{name} cannot reach another host from this line" in why, why
    assert "Drop the prefix" in why and "option of the command" in why and "`ssh host 'FOO=x cmd'`" in why, why
    assert "/a/b" not in why and "-v" not in why, "the refusal names the variable, never its value"
    unprefixed = " ".join(w for w in cmd.split(" ") if not re.match(r"\w+\+?=", w))
    assert not _decision(hook, unprefixed, env["tmp"]), f"unprefixed, {unprefixed!r} passes as before"
    monkeypatch.delenv("TTP_FENCE_BY_NAME")
    assert not _decision(hook, cmd, env["tmp"]), "unfenced: it runs anywhere"


@pytest.mark.parametrize("cmd", [
    "ssh host 'FOO=x cmd'", "ssh host FOO=x cmd", "timeout 9 ssh host 'A=1 B=2 make'", "ssh -o SendEnv=FOO host cmd",
    "ttp lock res -- ssh host 'FOO=x cmd | tail'", "FOO=x make", "FOO=x git status", "my-ssh host 'FOO=x cmd'",
    "git commit -m 'FOO=x ssh host'",
])
def test_fenced_hook_lets_an_assignment_in_the_remote_command_or_before_a_local_one_run(env, monkeypatch, cmd):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    monkeypatch.setenv("TTP_FENCE_FREED", "my-ssh")
    assert not _decision(hook, cmd, env["tmp"])


@pytest.mark.parametrize("cmd", ["FOO=1 scp a host:b && echo done", "FOO=x ssh host cmd | tail", "FOO=x bash -c 'ssh h x'"])
def test_a_compound_line_refusal_also_names_its_assignment_prefix(env, monkeypatch, cmd):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    why = _decision(hook, cmd, env["tmp"])
    assert "on its own" in why and "A leading `FOO=...`" in why and "Drop the prefix" in why, why


def test_fence_frees_git_network_commands_by_name():
    from ttp.providers import claude
    excluded = claude.sandbox_settings(["/wt"])["excludedCommands"]
    for cmd in ("git fetch *", "git pull *", "git push *", "git ls-remote *", "git clone *", "git remote update *"):
        assert cmd in excluded, cmd


def _repo(tmp, name, url):
    import subprocess
    d = tmp / name
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "remote", "add", "origin", url], check=True)
    return d


def _decision(hook, cmd, cwd):
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)})
    return out and out["hookSpecificOutput"].get("permissionDecision") == "deny" and \
        out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("cmd", [
    "git fetch && git rebase origin/x", "git fetch origin 2>&1 | tail -3", "git -C {ssh} fetch",
    "cd {ssh} && git pull --rebase", "git push origin HEAD:main; echo ok", "git remote update && git status",
    "timeout 60 git ls-remote origin | head", "git submodule update --init && make",
    "cd {https} && git clone git@example.com:org/r.git x && ls", "git ls-remote ssh://example.com/r.git | wc -l",
])
def test_fenced_hook_refuses_git_over_ssh_beside_other_commands(env, monkeypatch, cmd):
    from ttp import hook
    ssh, https = _repo(env["tmp"], "s", "git@example.com:org/r.git"), _repo(env["tmp"], "h", "https://example.com/r.git")
    cmd = cmd.format(ssh=ssh, https=https)
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    why = _decision(hook, cmd, ssh)
    assert why and "on its own" in why and "git " in why, why
    monkeypatch.delenv("TTP_FENCE_BY_NAME")
    assert not _decision(hook, cmd, ssh), "unfenced: git reaches any remote"


@pytest.mark.parametrize("cmd,where", [
    ("git fetch", "ssh"), ("timeout 60 git push origin HEAD:x", "ssh"), ("git fetch && git rebase origin/x", "https"),
    ("git -C {https} fetch", "ssh"), ("cd {https} && git pull", "ssh"), ("git status && git log -1", "ssh"),
    ("git commit -m 'git fetch && git push' && echo ok", "ssh"), ("git push origin HEAD:main; echo ok", "https"),
    ("git clone https://example.com/r.git x && ls x", "ssh"),
])
def test_fenced_hook_lets_git_on_its_own_or_over_https_run(env, monkeypatch, cmd, where):
    from ttp import hook
    ssh, https = _repo(env["tmp"], "s", "git@example.com:org/r.git"), _repo(env["tmp"], "h", "https://example.com/r.git")
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    assert not _decision(hook, cmd.format(ssh=ssh, https=https), ssh if where == "ssh" else https)


def _scripts_dir(tmp):
    d = tmp / "bin"
    d.mkdir(exist_ok=True)
    (d / "remote.sh").write_text("#!/bin/bash\nset -e\nttp lock res -- ssh host 'make 2>&1 | tail'\n")
    (d / "copy.sh").write_text("for h in a b; do timeout 9 rsync -a out/ $h:out/; done\n")
    (d / "wrap.sh").write_text("bash -c 'scp f host:f'\n")
    (d / "pull.py").write_text("import subprocess\nsubprocess.run(['ssh', host, 'cat log'])\n")
    (d / "local.sh").write_text("# ssh host would go here\nssh-keygen -l -f k.pub\necho 'use ssh later'\n"
                                "git log --grep ssh | head\n")
    (d / "notes.sh").write_text("cat > n.md <<'X'\nssh a && scp b\nX\n")
    return d


def _hint(hook, cmd, cwd):
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)})
    assert not out or out["hookSpecificOutput"].get("permissionDecision") != "deny", out
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


@pytest.mark.parametrize("cmd,name", [
    ("cd bin && ./remote.sh", "remote.sh"), ("bash bin/remote.sh | tail -5", "remote.sh"),
    ("bash bin/remote.sh; echo done", "remote.sh"), ("(bash bin/copy.sh)", "copy.sh"),
    ("bash bin/copy.sh > out.txt", "copy.sh"), ("bash bin/wrap.sh 2>&1 | tail", "wrap.sh"),
    ("python3 bin/pull.py && echo ok", "pull.py"), ("ttp lock res -- bash bin/remote.sh | tail", "remote.sh"),
    ("ttp lock res -- bash bin/remote.sh > log", "remote.sh"), ("bash bin/remote.sh", "remote.sh"),
    ("timeout 60 bash {d}/copy.sh", "copy.sh"), ("echo $(bash bin/remote.sh)", "remote.sh"),
])
def test_fenced_hook_hints_a_script_that_reaches_a_host_from_a_fenced_line(env, monkeypatch, cmd, name):
    from ttp import hook
    d = _scripts_dir(env["tmp"])
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    hint = _hint(hook, cmd.format(d=d), env["tmp"])
    assert hint.startswith(name) and "host names do not resolve" in hint, hint
    run = f"python3 {d / name}" if name.endswith(".py") else f"{d / name}" if "./" in cmd else f"bash {d / name}"
    assert f"`ttp lock <res> -- {run}`" in hint, hint
    monkeypatch.delenv("TTP_FENCE_BY_NAME")
    assert "write fence" not in _hint(hook, cmd.format(d=d), env["tmp"]), "unfenced: a script reaches any host"


@pytest.mark.parametrize("cmd", [
    "ttp lock res -- bash {d}/remote.sh", "ttp lock res -- python3 bin/pull.py", "timeout 9 ttp lock res -- ./bin/copy.sh",
    "ttp lock res -- bash bin/remote.sh 2>&1", "bash bin/local.sh | tail", "cd bin && bash notes.sh",
    "ttp detach --remote host job -- bash bin/remote.sh", "ttp detach job -- bash bin/remote.sh",
    "cat bin/remote.sh | head", "bash missing.sh && echo ok", "git commit -m 'bash bin/remote.sh && x'",
])
def test_fenced_hook_gives_no_script_hint_to_a_freed_line_or_a_local_script(env, monkeypatch, cmd):
    from ttp import hook
    d = _scripts_dir(env["tmp"])
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    assert "write fence" not in _hint(hook, cmd.format(d=d), env["tmp"])


RACE = ("bwrap: Can't get type of source path /r/.git/worktrees/tree/commondir: No such file or directory")


@pytest.mark.parametrize("error,hinted", [
    (RACE, True), ("bwrap: Can't get type of source path /r/.git/config.lock: No such file or directory", True),
    ("Exit code 1\nbwrap: Can't get type of source /r/.git/config.lock: No such file or directory", True),
    ("bwrap: Can't bind mount /oldroot/r/.git/worktrees/t1 on /newroot/r/.git/worktrees/t1: No such file or "
     "directory", True),
    ("Exit code 1\nls: cannot access 'x': No such file or directory", False),
    ("bwrap: Can't get type of source path /r/x: Permission denied", False), ("", False)])
def test_failed_tool_call_gets_the_mount_race_hint_only_on_that_error(monkeypatch, tmp_path, error, hinted):
    from ttp import hook
    monkeypatch.delenv("TTP_RUN_DIR", raising=False)
    out, _ = hook.post_tool_use_failure({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                                         "error": error})
    if not hinted:
        assert out is None
        return
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert "re-run the same command unchanged" in out["hookSpecificOutput"]["additionalContext"]


def test_failed_tool_call_still_hands_over_the_coordinator_update(monkeypatch, tmp_path):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    (tmp_path / "steer.md").write_text("push to the other branch\n")
    out, delivered = hook.post_tool_use_failure({"tool_name": "Bash", "error": RACE})
    text = out["hookSpecificOutput"]["additionalContext"]
    assert text.startswith("tt-project: transient sandbox mount race") and "push to the other branch" in text
    delivered()
    out, _ = hook.post_tool_use_failure({"tool_name": "Bash", "error": "Exit code 2"})
    assert out is None, "the update is handed over once"
    assert hook.HANDLERS["PostToolUseFailure"] is hook.post_tool_use_failure


def test_claude_hooks_register_the_failure_event():
    from ttp.providers import claude
    hooks = claude.hook_settings()["hooks"]
    assert hooks["PostToolUseFailure"][0]["hooks"][0]["command"].endswith("ttp.hook PostToolUseFailure")
    assert hooks["PostToolUse"][0]["hooks"][0]["command"].endswith("ttp.hook PostToolUse")


def test_fence_grants_only_the_roots_it_is_given(monkeypatch, tmp_path):
    """The mount race fix opened no write: the sandbox still writes only the given roots and the tool
    caches, never runs a command outside it, and has no rule that would lift a deny of Claude Code's."""
    from ttp.providers import claude
    monkeypatch.setattr(claude, "tool_caches", lambda: ["/c"])
    s = claude.sandbox_settings(["/wt", "/repo/.git"])
    assert s["filesystem"] == {"allowWrite": ["/wt", "/repo/.git", "/c"]}
    assert s["enabled"] is True and s["allowUnsandboxedCommands"] is False
    assert set(s) == {"enabled", "autoAllowBashIfSandboxed", "allowUnsandboxedCommands", "excludedCommands",
                      "filesystem", "network"}


def test_audit_counts_claude_runs_that_hit_the_mount_race(env, tmp_path):
    """Only a failing tool_result counts: a run that quotes the message (assistant text, a cat of a file,
    the prompt) did not hit the race."""
    from ttp import audit
    p = make(env)
    now = time.time()

    def result(content, is_error):
        return json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": content, "is_error": is_error}]},
            "tool_use_result": "Error: " + RACE})

    real = "Exit code 1\nbwrap: Can't get type of source /r/.git/config.lock: No such file or directory"
    outputs = [
        ("claude", result(real, True)),
        ("claude", "not json " + RACE + "\n" + result([{"type": "text", "text": RACE}], True)),
        ("claude", json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": RACE}]}})),
        ("claude", result("1  RACE = (\"" + RACE + "\")", False)),
        ("claude", json.dumps({"type": "user", "message": {"content": "fix the metric: " + RACE}})),
        ("claude", result("Exit code 1\nls: x: No such file or directory", True)),
        ("codex", result(RACE, True)), ("claude", None)]
    for i, (prov, text) in enumerate(outputs):
        d = tmp_path / f"r{i}"
        d.mkdir()
        if text is not None:
            (d / "output.jsonl").write_text(text + "\n")
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,dir) VALUES(?,?,?,?,?,?)",
               ("worker", prov, now - 60, now - 30, "ok", str(d)))
    ids = [r["id"] for r in p.db.q("SELECT id FROM runs ORDER BY id")]
    got = audit.mount_races(p.db, now - 3600)
    assert (got["runs"], got["hit"], got["ids"]) == (7, 2, ids[:2])
    assert any(ln.startswith("sandbox mount races: 2 of 7 Claude runs (29%)") for ln in audit.lines(p.db, now, p=p))
    assert audit.report(p.db, now, p=p)["mount_races"]["hit"] == 2

def test_write_dirs_grants_only_directories_inside_the_project_root(tmp_path):
    from ttp.project import write_dirs
    root = tmp_path / "proj"
    base = root / "tt-project"
    for d in ("notes", "data/out", "tt-project/harness/memory", "tt-project/state"):
        (root / d).mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (root / "link-out").symlink_to(tmp_path / "elsewhere")
    (root / "link-in").symlink_to(root / "notes")
    (root / "link-state").symlink_to(base / "state")
    (root / "file.txt").write_text("x")
    assert write_dirs(root, base, None) == ([], [], []), "unset: no extra roots"
    granted, refused, missing = write_dirs(root, base, [
        "notes", str(root / "data/out"), "link-in", "link-out", "../elsewhere", str(tmp_path / "elsewhere"), ".",
        str(root), "data/..", "tt-project", "tt-project/state", "tt-project/harness/memory", "link-state",
        "absent", "file.txt", 3])
    assert granted == [str((root / "notes").resolve()), str((root / "data/out").resolve())], "a link grants its target"
    why = {r.split(": ", 1)[0]: r.split(": ", 1)[1] for r in refused}
    for e in ("link-out", "../elsewhere", str(tmp_path / "elsewhere")):
        assert "outside the project root" in why[e], (e, why)
    for e in (".", str(root), "data/.."):
        assert "project root itself" in why[e], (e, why)
    for e in ("tt-project", "tt-project/state", "tt-project/harness/memory", "link-state"):
        assert "charter, memory, config or state" in why[e], (e, why)
    assert "3" in why and len(refused) == 11
    assert [m.split(":")[0] for m in missing] == ["absent", "file.txt"]
    assert not (root / "absent").exists(), "a missing directory is never created"
    assert write_dirs(root, base, "notes")[1] == ['"notes": not a list of paths']


def test_write_dirs_reach_the_fenced_worker_and_refusals_alert(env, monkeypatch):
    from ttp.providers import claude
    monkeypatch.setattr(claude.Claude, "binary", lambda self: "/usr/bin/true")   # never a real agent
    monkeypatch.setattr(claude.sys, "platform", "linux")
    monkeypatch.setattr(claude, "find_binary", lambda b: f"/usr/bin/{b}")
    from ttp.daemon import Daemon
    alerts: dict = {}
    monkeypatch.setattr(Daemon, "alert", lambda self, key, text, **kw: alerts.__setitem__(key, (text, kw)))
    p = make(env)
    wt = env["tmp"] / "wt"
    subprocess.run(["git", "-C", str(env["repo"]), "worktree", "add", "-q", str(wt)], check=True)
    before = _settings(_claude_launch(env, p, wt)["argv"])["sandbox"]["filesystem"]["allowWrite"]
    assert not alerts, "unset: nothing to say"
    (p.root / "notes").mkdir()
    p.set_config("providers", {**p.config()["providers"], "claude": {
        **p.config()["providers"].get("claude", {}), "write_dirs": ["notes", "..", "tt-project/harness", "gone"]}})
    spec = _claude_launch(env, p, wt)
    sandbox = _settings(spec["argv"])["sandbox"]
    allow = sandbox["filesystem"]["allowWrite"]
    assert [a for a in allow if a not in before] == [str((p.root / "notes").resolve())], "only the allowed dir is added"
    assert set(before) <= set(allow)
    assert sandbox.get("enabled", True) is not False and spec["env"].get("TMPDIR"), "the fence stays on"
    alerts = {k: v[0] for k, v in alerts.items() if k.startswith("write_dirs")}
    assert set(alerts) == {"write_dirs_refused:claude", "write_dirs_missing:claude"}, alerts
    assert "..: " in alerts["write_dirs_refused:claude"] and "tt-project/harness: covers" in alerts["write_dirs_refused:claude"]
    assert "gone:" in alerts["write_dirs_missing:claude"] and not (p.root / "gone").exists()
    from ttp.project import config_problems
    assert not [x for x in config_problems(p.raw_config()) if "write_dirs" in x], "a known key"


def test_a_providers_fenced_answer_matches_the_fence_its_launch_gets(monkeypatch):
    """Provider.fenced (what `ttp upgrade` reads before queueing a harness merge task) says what fence_args
    gives the daemon's launch, for every provider, fence setting and sandbox dependency state."""
    from ttp.providers import all_providers, claude
    missing = []
    monkeypatch.setattr(claude, "fence_missing", lambda: list(missing))
    for deps in ([], ["bwrap"]):
        missing[:] = deps
        for prov in all_providers():
            for on in (True, False):
                for rules in ({}, {"no_internet": True}):
                    _, sandboxed = prov.fence_args(["agent", "--settings", "{}", "-"], ["/tmp/x"], cwd="/tmp/w",
                                                   on=on, restrictions=rules)
                    assert prov.fenced(on, rules) == sandboxed, (prov.name, on, rules, deps)


def _harness_bin_tool(base, name, mode=0o755):
    tool = Path(base) / "harness" / "bin" / name
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text("#!/bin/sh\nexec ssh \"$@\"\n")
    tool.chmod(mode)
    return tool


def test_fence_freed_commands_take_only_executables_in_the_harness_bin(tmp_path):
    """sandbox.fence_freed_commands: a name counts only as an executable file in the harness's bin/;
    a path, a plain file, a missing one, a shell or wrapper and the harness's own commands are left
    out with why, and the hook's shells and wrappers stay in the refused set."""
    from ttp import fencefree, hook
    _harness_bin_tool(tmp_path, "my-ssh")
    _harness_bin_tool(tmp_path, "plain", 0o644)
    for name in ("git", "bash", "ttp"):
        _harness_bin_tool(tmp_path, name)
    (tmp_path / "harness" / "bin" / "dir").mkdir()
    (tmp_path / "elsewhere").write_text("#!/bin/sh\n")
    (tmp_path / "elsewhere").chmod(0o755)
    freed, refused = fencefree.commands(tmp_path, [
        "my-ssh", {"command": "my-ssh", "env": ["MY_SSH_OPTS"]}, "plain", "gone", "dir", "git", "bash", "ttp",
        "../elsewhere", str(tmp_path / "elsewhere"), "-x", "", 5, {"env": ["A"]}])
    assert freed == ["my-ssh"], freed
    assert len(refused) == 13 and "ignored, the Claude sandbox" in refused[0], refused
    assert any(r.startswith("plain: no executable") for r in refused)
    assert any(r.startswith("gone: no executable") for r in refused)
    assert sum("frees all it runs" in r for r in refused) == 3
    assert sum("not a command name" in r for r in refused) == 6, refused
    assert fencefree.commands(tmp_path, "my-ssh") == ([], ['"my-ssh": not a list of command names'])
    assert fencefree.commands(tmp_path, None) == ([], [])
    assert hook.SHELLS | hook.WRAPPERS | {"eval"} <= fencefree.REFUSED
    assert fencefree.patterns(freed) == ["my-ssh *"]


def test_fence_freed_commands_ignore_an_env_part_with_one_line_per_entry(tmp_path):
    """sandbox.fence_freed_commands: the Claude sandbox never frees a line that starts with an assignment
    (CLI 2.1.296), so an entry's env part, well formed or not, adds no `VAR=* name *` pattern: the command
    alone stays freed and each such entry gets one line saying so. An empty env says nothing."""
    from ttp import fencefree
    _harness_bin_tool(tmp_path, "my-ssh")
    _harness_bin_tool(tmp_path, "other")
    freed, said = fencefree.commands(tmp_path, [
        {"command": "my-ssh", "env": ["MY_SSH_OPTS", "LD_PRELOAD"]}, {"command": "my-ssh", "env": "BASH_ENV"},
        {"command": "other", "env": ["BAD VAR"]}, {"command": "other", "env": []}, {"command": "gone", "env": ["A"]}])
    assert freed == ["my-ssh", "other"], freed
    assert fencefree.patterns(freed) == ["my-ssh *", "other *"]
    assert not any("=" in p for p in fencefree.patterns(freed))
    env_lines = [r for r in said if "never frees a command line that starts with an assignment" in r]
    assert len(env_lines) == 3 and [r.split()[0] for r in env_lines] == ["my-ssh", "my-ssh", "other"], said
    assert len(said) == 4 and any(r.startswith("gone: no executable") for r in said), "a missing one: why, no env line"


def test_fence_freed_commands_reach_the_sandbox_and_the_hook_of_a_fenced_worker(env, monkeypatch):
    from ttp.providers import claude
    monkeypatch.setattr(claude.Claude, "binary", lambda self: "/usr/bin/true")   # never a real agent
    monkeypatch.setattr(claude.sys, "platform", "linux")
    monkeypatch.setattr(claude, "find_binary", lambda b: f"/usr/bin/{b}")
    p = make(env)
    wt = env["tmp"] / "wt"
    subprocess.run(["git", "-C", str(env["repo"]), "worktree", "add", "-q", str(wt)], check=True)
    spec = _claude_launch(env, p, wt)
    assert _settings(spec["argv"])["sandbox"]["excludedCommands"] == list(claude.FENCE_EXCLUDED), "unset: built-ins"
    assert "TTP_FENCE_FREED" not in spec["env"]
    _harness_bin_tool(p.base, "my-ssh")
    p.set_config("sandbox", {"fence_freed_commands": ["my-ssh", "gone", {"command": "my-ssh", "env": ["OPTS"]}]})
    from ttp.project import config_problems
    assert not [x for x in config_problems(p.raw_config()) if "sandbox" in x], "a known key"
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    for _ in range(2):
        spec = _claude_launch(env, p, wt, d)
        excluded = _settings(spec["argv"])["sandbox"]["excludedCommands"]
        assert excluded == [*claude.FENCE_EXCLUDED, "my-ssh *"], excluded
        assert spec["env"].get("TTP_FENCE_FREED") == "my-ssh", "the hook checks it as it checks ssh"
    lines = [l for l in (p.logs / "daemon.log").read_text().splitlines() if "fence_freed_commands" in l]
    assert len(lines) == 2 and "left out gone: no executable" in lines[0], "one line per daemon, not per run"
    assert "my-ssh env [\"OPTS\"]: ignored" in lines[1] and "starts with an assignment" in lines[1], lines
    p.set_config("providers", {**p.config()["providers"], "claude": {**p.config()["providers"].get("claude", {}),
                                                                      "write_fence": False}})
    spec = _claude_launch(env, p, wt)
    assert "sandbox" not in _settings(spec["argv"]) and "TTP_FENCE_FREED" not in spec["env"], "unfenced"


@pytest.mark.parametrize("cmd,refused", [
    ("my-ssh host cmd", False), ("timeout 5 my-ssh host cmd", False), ("my-ssh host 'a && b | tail'", False),
    ("ttp lock res -- my-ssh host cmd", False), ("my-ssh host cmd 2>&1", False),
    ("my-ssh host x && y", True), ("my-ssh host x | tail -5", True), ("y; my-ssh host x", True),
    ("my-ssh host x > out.txt", True), ("my-ssh host \"$(cat cmd)\"", True), ("bash -c 'my-ssh host x'", True),
    ("timeout 5 /abs/harness/bin/my-ssh host x && y", True), ("eval 'my-ssh host x'", True),
])
def test_fenced_hook_checks_a_projects_freed_command_as_it_checks_ssh(env, monkeypatch, cmd, refused):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    monkeypatch.setenv("TTP_FENCE_FREED", "my-ssh")
    why = _decision(hook, cmd, env["tmp"])
    assert bool(why) == refused and (not why or "my-ssh reaches another host only as a simple command" in why), why
    monkeypatch.setenv("TTP_FENCE_FREED", "")
    assert not _decision(hook, cmd, env["tmp"]), "a name the project did not free is not checked"
    monkeypatch.setenv("TTP_FENCE_FREED", "my-ssh")
    monkeypatch.delenv("TTP_FENCE_BY_NAME")
    assert not _decision(hook, cmd, env["tmp"]), "unfenced: it runs anywhere"


@pytest.mark.parametrize("cmd", ["my-ssh host cat {tok}", "timeout 5 my-ssh host cat {tok}",
                                 "my-ssh host 'cat {tok}' && y", "ttp lock res -- my-ssh host cat {tok}"])
def test_a_freed_command_naming_the_web_token_is_still_refused(env, monkeypatch, cmd):
    """sa1: freeing a command from the write fence never exempts it from the web token check."""
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    monkeypatch.setenv("TTP_FENCE_FREED", "my-ssh")
    why = _decision(hook, cmd.format(tok="tt-project/state/" + "web" + ".token"), env["tmp"])
    assert why and ("web token" in why or "simple command" in why), why
    if "&&" not in cmd:
        assert "web token" in why, why


def test_an_upgrade_merges_cleanly_where_the_project_frees_its_command_by_config(env, monkeypatch, capsys):
    """A project frees its own ssh wrapper in project.json, not in hook.py or providers/claude.py, and the
    next template changes the FENCE_FREED and FENCE_EXCLUDED lines: the upgrade applies with no merge task
    and keeps the project's key."""
    p = make(env)
    from ttp import cli, service
    restarts = []
    monkeypatch.setattr(service, "restart", lambda p: restarts.append(1) or "restarted")
    lib = env["home"] / "lib" / "current" / "runtime" / "ttp"
    h = p.harness
    _install_template(env)
    cli.main(["upgrade", "demo"])
    _harness_bin_tool(p.base, "my-ssh")
    p.set_config("sandbox", {"fence_freed_commands": ["my-ssh"]})
    _git_out(h, "add", "-A")
    _git_out(h, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "local: free my-ssh")
    _install_template(env)
    for rel, old, new in (("hook.py", 'FENCE_FREED = {"ssh", "scp", "sftp", "rsync"}',
                           'FENCE_FREED = {"ssh", "scp", "sftp", "rsync", "mosh"}'),
                          ("providers/claude.py", 'FENCE_EXCLUDED = ("ssh *", ', 'FENCE_EXCLUDED = ("ssh *", "mosh *", ')):
        text = (lib / rel).read_text()
        assert old in text, rel
        (lib / rel).write_text(text.replace(old, new))
    _next_release(env)
    restarts.clear()
    capsys.readouterr()
    cli.main(["upgrade", "demo", "--auto"])
    assert '"mosh"}' in (h / "runtime" / "ttp" / "hook.py").read_text()
    assert '"mosh *"' in (h / "runtime" / "ttp" / "providers" / "claude.py").read_text()
    assert p.config()["sandbox"]["fence_freed_commands"] == ["my-ssh"]
    assert restarts == [1] and not p.db.q("SELECT id FROM tasks WHERE kind='harness'")
    assert p.db.kv("upgrade_auto")["outcome"] == "applied"


def test_the_upgrade_task_names_the_config_key_for_freed_commands():
    from ttp import cli
    text = cli._UPGRADE_TASK.format(problem="x", name="demo", target="1.2.3", state="/s")
    assert "sandbox.fence_freed_commands" in text and "FENCE_FREED" in text


@pytest.mark.parametrize("argv_tail", [
    ["providers.claude.write_fence", "true"],                         # turns the sandbox back on
    ["providers.claude.write_fence", "false"],                        # or off: the user's, both ways
    ["providers.codex.write_fence", "true"],
    ["providers.claude", json.dumps({"tiers": {}})],                   # drops the user's false: back on
    ["providers", json.dumps({})],                                     # through the whole section
])
def test_a_run_cannot_change_the_worker_sandbox_without_the_user(env, capsys, monkeypatch, argv_tail):
    """The worker sandbox (providers.<name>.write_fence) is a user-only setting: a run's `ttp config`
    refuses to turn it back on (or off), directly or through a parent key, as it refuses a spend cap;
    reading works, and outside a run (the user's own terminal) it sets."""
    from ttp import cli
    from ttp.providers import fence_on
    p = make(env)
    p.set_config("providers.claude.write_fence", False)
    before = p.raw_config()
    monkeypatch.setenv("TTP_RUN_ID", "7")
    if argv_tail[-1] == "false":
        p.set_config("providers.claude.write_fence", True)
        before = p.raw_config()
    with pytest.raises(SystemExit) as e:
        cli.main(["config", "demo", *argv_tail])
    assert e.value.code == 2 and "only the user may change" in capsys.readouterr().err
    assert p.raw_config() == before
    cli.main(["config", "demo", "providers.claude.write_fence"])      # reading works
    capsys.readouterr()
    monkeypatch.delenv("TTP_RUN_ID")
    cli.main(["config", "demo", "providers.claude.write_fence", "true"])
    assert fence_on(p.config(), "claude")


def test_the_coordinator_changes_the_worker_sandbox_only_in_a_turn_carrying_the_users_word(env, capsys):
    """config_set of providers.<name>.write_fence is refused in a turn without the user's message (an
    event or hand-off wake: its own judgement), and applied in the turn carrying it."""
    from ttp import cli, coordinator as coord
    from ttp.providers import fence_on
    p = make(env)
    p.set_config("providers.claude.write_fence", False)
    for value in (True, "on", False):
        out = coord.apply(p, [{"type": "config_set", "key": "providers.claude.write_fence", "value": value}])
        assert any("needs the user's approval" in x for x in out), (value, out)
        assert not fence_on(p.config(), "claude")
    assert "providers.claude.write_fence" not in coord.HELD_SETTING
    assert coord.apply(p, [{"type": "config_set", "key": "providers.claude.write_fence", "value": True}],
                       user_turn=True) == []
    assert fence_on(p.config(), "claude")
    assert coord.apply(p, [{"type": "config_set", "key": "providers.claude.write_fence", "value": "false"}],
                       user_turn=True) == []
    assert p.raw_config()["providers"]["claude"]["write_fence"] is False
    cli.main(["config", "--chat-settable", "providers.claude.write_fence", "true"])
    assert "providers.claude.write_fence: accepted as true" in capsys.readouterr().out
