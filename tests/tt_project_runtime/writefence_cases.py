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


def _claude_launch(env, p, cwd):
    from ttp.providers import claude
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
    rid = d.start_run("worker", "go", "claude", "light", str(cwd), task=p.db.task(tid))
    (p.runs / str(rid) / "STOP").touch()
    return json.loads((p.runs / str(rid) / "run.json").read_text())


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
])
def test_fenced_hook_lets_ssh_on_its_own_and_ssh_as_data_run(env, monkeypatch, cmd):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    monkeypatch.setenv("TTP_FENCE_BY_NAME", "1")
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(env["tmp"])})
    assert not out or out["hookSpecificOutput"].get("permissionDecision") != "deny", out


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
