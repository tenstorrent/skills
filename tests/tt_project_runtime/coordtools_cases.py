# Session coordinator tool tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file. Add tests of
# a session coordinator's read-only tools, its tool hook, `ttp hostcheck` and the per-wake budget here.
from __future__ import annotations


def _coord_argv(env, monkeypatch, session=None, budget=1.0):
    """The argv, timeout and note of a coordinator turn started with coordinator.session = `session`
    (left out when None); the agent never runs."""
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    from ttp.providers import claude
    monkeypatch.setattr(claude.Claude, "binary", lambda self: "/usr/bin/true")
    monkeypatch.setattr(claude, "_FLAGS", {claude.INPUT_FORMAT: True, claude.SYSTEM_PROMPT_FILE: True,
                                           claude.TOOLS: True})
    p = make(env)
    d = Daemon(p.base)
    if session is not None:
        d.cfg["coordinator"]["session"] = session
    rid = d.start_run("coordinator", "EVENTS", "claude", "standard", str(p.root), read_only=True,
                      schema=coord.ACTIONS_SCHEMA, system="HEAD", budget_usd=budget)
    run = p.runs / str(rid)
    (run / "STOP").touch()
    spec = json.loads((run / "run.json").read_text())
    note = json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"] or "{}")
    return [a.replace(str(run), "<run>") for a in spec["argv"]], spec["timeout_s"], note, p


def test_mode_off_coordinator_argv_is_unchanged(env, monkeypatch):
    base, timeout, note, _ = _coord_argv(env, monkeypatch)
    i = base.index("--tools")
    assert base[i - 2:i + 2] == ["--safe-mode", "--strict-mcp-config", "--tools", ""]
    for off in ({}, {"mode": "off"}, {"mode": "OFF ", "wake_budget_usd": 9, "wake_budget_s": 5}, "on"):
        argv, t, n, _ = _coord_argv(env, monkeypatch, off)
        assert argv == base and t == timeout, off
        assert "wake_budget" not in n["turn"]
    assert "--allowedTools" not in base and "--settings" not in base


def test_mode_on_coordinator_gets_only_read_only_tools(env, monkeypatch):
    argv, _, _, p = _coord_argv(env, monkeypatch, {"mode": "on"})
    assert "--safe-mode" not in argv, "safe mode turns hooks off"
    assert argv[argv.index("--tools") + 1] == "Read,Bash"
    for flag in ("--restricted", "--strict-mcp-config", "--disallowedTools"):
        assert flag in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--allowedTools") + 30]
    allowed = allowed[:next(i for i, a in enumerate(allowed) if a.startswith("--"))]
    assert f"Read(/{p.root.resolve()}/**)" in allowed
    assert f"Bash(ttp task {p.name} show *)" in allowed and "Bash(ttp hostcheck *)" in allowed
    assert "StructuredOutput" in allowed, "the turn's --json-schema answer goes through it"
    assert all(a.startswith("Bash(ttp ") or a.startswith("Read(") or a == "StructuredOutput" for a in allowed)
    assert not [a for a in allowed if "Edit" in a or "Write" in a or a == "Bash(*)"]
    deny = argv[len(argv) - 1 - argv[::-1].index("--disallowedTools") + 1]
    assert deny.count("Read(//") == 2, "the web token and the credential store"
    settings = json.loads(argv[argv.index("--settings") + 1])
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "*" and cmd.endswith("-m ttp.hook CoordPreToolUse")
    assert len(settings["permissions"]["deny"]) == 2


def test_providers_doc_names_structuredoutput_in_the_read_only_coordinator_row():
    import pathlib
    doc = (RUNTIME.parent / "docs" / "providers.md").read_text()
    row = next(line for line in doc.splitlines() if line.startswith("| Read-only coordinator"))
    claude = row.split("|")[2]
    assert "StructuredOutput" in claude, "the doc must name the --json-schema answer tool"
    assert "--allowedTools" in claude


def test_a_wake_stops_at_its_dollar_and_time_limits(env, monkeypatch):
    argv, timeout, note, _ = _coord_argv(env, monkeypatch, {"mode": "on", "wake_budget_usd": 0.4,
                                                            "wake_budget_s": 300}, budget=1.0)
    assert float(argv[argv.index("--max-budget-usd") + 1]) == 0.4
    assert timeout == 300 and note["turn"]["timeout_s"] == 300
    assert note["turn"]["wake_budget"] == {"usd": 0.4, "s": 300}
    # Unset or bad values: the stateless turn's own budget and timeout.
    off_argv, off_timeout, _, _ = _coord_argv(env, monkeypatch, None, budget=1.0)
    argv, timeout, note, _ = _coord_argv(env, monkeypatch, {"mode": "on", "wake_budget_usd": "x",
                                                            "wake_budget_s": -5}, budget=1.0)
    assert float(argv[argv.index("--max-budget-usd") + 1]) == 1.0 == float(off_argv[off_argv.index("--max-budget-usd") + 1])
    assert timeout == off_timeout
    from ttp import coordtools
    assert coordtools.wake_budget({}, 0.7, 600) == (0.7, 600)
    from ttp.project import DEFAULT_CONFIG
    assert "max_tool_calls" not in json.dumps(DEFAULT_CONFIG), "no tool-call cap anywhere"


def test_the_coordinator_tool_hook_allows_reads_and_denies_the_rest(tmp_path, monkeypatch):
    from ttp import coordtools
    secret = tmp_path / "state" / "web.token"
    secret.parent.mkdir()
    secret.write_text("x")
    monkeypatch.setenv(coordtools.DENIED_ENV, str(secret))

    def decide(tool, **args):
        out, _ = coordtools.pre_tool_use({"tool_name": tool, "tool_input": args})
        return "deny" if out else "allow"
    allowed = ["ttp status", "ttp status demo --json", "ttp task demo show 12", "ttp task other list",
               "ttp said 'device reset' -n 3", "ttp eta 12", "ttp landed abc123", "ttp stats --days 2",
               "ttp machines activity box1", "ttp machines list --json", "ttp hostcheck box1 uptime",
               "ttp hostcheck local git-log /repo 5"]
    denied = ["ttp status; rm -rf x", "ttp status && touch x", "ttp status | sh", "ttp status > out",
              "ttp said $(cat x)", "ttp said `id`", "ttp status\nrm x", "rm -rf x", "git log --output=x",
              "ttp task demo add x", "ttp task demo show 1 & touch x", "ttp push", "ttp machines add box2",
              "ttp lock box -- rm x", "ttp task --x show 1", "bash -c 'ttp status'", "ttp say hi",
              "ttp hostcheck box1 uptime 2>x", "ttp 'status", "ttp status #x", "ttp checks", ""]
    for cmd in allowed:
        assert decide("Bash", command=cmd) == "allow", cmd
    for cmd in denied:
        assert decide("Bash", command=cmd) == "deny", cmd
    for tool in ("Edit", "Write", "NotebookEdit", "WebFetch", "Skill", "Task", "mcp__x__y", "",
                 "structuredoutput", "StructuredOutput ", "mcp__x__StructuredOutput", "StructuredOutputX"):
        assert decide(tool, file_path=str(tmp_path / "a")) == "deny", tool
    # The CLI ends a --json-schema turn only through StructuredOutput: denying it loses the answer.
    assert decide("StructuredOutput", actions=[], summary="ok") == "allow"
    assert decide("Read", file_path=str(tmp_path / "a.md")) == "allow"
    assert decide("Glob", pattern="*.md", path=str(tmp_path / "other")) == "allow"
    assert decide("Read", file_path=str(secret)) == "deny"
    link = tmp_path / "link"
    link.symlink_to(secret)
    assert decide("Read", file_path=str(link)) == "deny", "a link to it is it"
    assert decide("Grep", pattern="x", path=str(tmp_path)) == "deny", "a search would walk into it"
    assert decide("Read", file_path=str(tmp_path / "state" / ".." / "state" / "web.token")) == "deny"
    # A malformed payload, through the hook's own entry point, is denied (fails closed).
    hook = subprocess.run([sys.executable, "-m", "ttp.hook", "CoordPreToolUse"], input="not json",
                          capture_output=True, text=True, cwd=str(RUNTIME), timeout=30)
    assert hook.returncode == 0 and json.loads(hook.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert coordtools.pre_tool_use(None)[0]["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hostcheck_catalogue_builds_fixed_read_only_argv():
    from ttp import hostcheck as hc
    assert hc.build("uptime", []) == ["uptime"]
    assert hc.build("process-alive", ["1234"]) == ["ps", "-o", "pid=,etime=,args=", "-p", "1234"]
    assert hc.build("process-alive", ["ttp-daemon"]) == ["ps", "-o", "pid=,etime=,args=", "-C",
                                                          "ttp-daemon"]
    assert hc.build("unit-active", ["tt-project.service", "user"]) == ["systemctl", "--user", "is-active",
                                                                       "tt-project.service"]
    assert hc.build("tt-smi", []) == ["tt-smi", "-s"]
    assert hc.build("squeue", [])[:3] == ["squeue", "--me", "-h"]
    assert hc.build("git-log", ["/r", "5"])[-2:] == ["-n", "5"]
    cfg = {"coordinator": {"session": {"log_paths": {"daemon": "/var/log/d.log", "rel": "d.log"}}}}
    assert hc.build("log-tail", ["daemon"], cfg) == ["tail", "-n", "50", "--", "/var/log/d.log"]
    for check in hc.CATALOGUE:
        for args in ([], ["/r"], ["/r", "HEAD"], ["daemon"]):
            try:
                argv = hc.build(check, args, cfg)
            except hc.Refused:
                continue
            assert argv[0] in hc.READ_ONLY_PROGRAMS, argv
    bad = [("rm", ["-rf", "/"]), ("shell", ["id"]), ("git-log", ["--output=/tmp/x"]), ("git-log", ["/r", "-p"]),
           ("git-show", ["/r", "--output=/tmp/x"]), ("file-exists", ["a;rm -rf /"]), ("file-exists", ["$(id)"]),
           ("disk-free", ["/ /etc"]), ("unit-active", ["x", "--now"]), ("uptime", ["x"]),
           ("process-alive", ["-9"]), ("log-tail", ["rel"]), ("log-tail", ["/etc/shadow"]), ("log-tail", ["nope"]),
           ("git-log", ["/r", "100000"]), ("broker-job", ["--cancel"])]
    for check, args in bad:
        try:
            hc.build(check, args, cfg)
        except hc.Refused:
            continue
        raise AssertionError(f"not refused: {check} {args}")


def test_hostcheck_own_host_runs_here_and_remote_uses_project_access(tmp_path, monkeypatch):
    from ttp import hostcheck as hc
    away = {"hostname": "host-not-local.invalid", "port": 22, "proxy": False}
    monkeypatch.setattr(hc, "ssh_target", lambda alias, config=None: away)
    known = {"box1": {"tags": []}, "box2": {"tags": []}}
    assert hc.command({}, tmp_path, "local", "uptime", [], known) == (["uptime"], True)
    me = socket.gethostname()
    assert hc.command({}, tmp_path, me, "uptime", [], known) == (["uptime"], True)
    monkeypatch.setattr(hc, "ssh_target", lambda alias, config=None: {**away, "hostname": me})
    assert hc.command({}, tmp_path, "box1", "uptime", [], known) == (["uptime"], True), "resolves to this host"
    monkeypatch.setattr(hc, "ssh_target", lambda alias, config=None: away)
    # Default: no access, refused (never the user's own ssh setup).
    for alias in ("box1", "stranger"):
        try:
            hc.command({}, tmp_path, alias, "uptime", [], known)
            raise AssertionError(alias)
        except hc.Refused:
            pass
    helper = tmp_path / "bin" / "reach"
    helper.parent.mkdir()
    cfg = {"coordinator": {"session": {"host_access": {"box1": ["bin/reach", "--via", "gw"], "*": "ssh.conf"}}}}
    argv, here = hc.command(cfg, tmp_path, "box1", "process-alive", ["ttp-daemon"], known)
    assert not here and argv == [str(helper.resolve()), "--via", "gw", "ps", "-o", "pid=,etime=,args=", "-C",
                                                       "ttp-daemon"]
    argv, here = hc.command(cfg, tmp_path, "box2", "file-exists", ["/a"], known)
    assert argv[:9] == ["ssh", "-F", str((tmp_path / "ssh.conf").resolve()), "-o", "BatchMode=yes", "-o",
                        "ConnectTimeout=5", "--", "box2"]
    assert shlex.split(argv[9])[0] == "stat"
    for alias in ("stranger", "-oProxyCommand=id", "box1;id"):   # not a ttp machines alias, or not a word
        try:
            hc.command(cfg, tmp_path, alias, "uptime", [], known)
            raise AssertionError(alias)
        except hc.Refused:
            pass
    outside = {"coordinator": {"session": {"host_access": {"box1": ["/usr/bin/ssh", "box1"]}}}}
    try:
        hc.command(outside, tmp_path, "box1", "uptime", [], known)
        raise AssertionError("a helper outside the project folder")
    except hc.Refused:
        pass


def test_hostcheck_loopback_tunnel_end_is_remote_never_this_host(tmp_path, monkeypatch):
    from ttp import hostcheck as hc
    known = {"tunnel-box": {"tags": []}, "box1": {"tags": [], "hostname": "127.0.0.1"}}
    cfg = {"coordinator": {"session": {"host_access": {"*": "ssh.conf"}}}}
    seen = []

    def target(hostname, port, proxy=False):
        def fake(alias, config=None):
            seen.append(config)
            return {"hostname": hostname, "port": port, "proxy": proxy}
        return fake
    # A loopback HostName is a tunnel end or a hop (ProxyJump into a container) whatever its port: remote.
    for loop in ("127.0.0.1", "::1", "localhost", "127.0.0.2", "LOCALHOST"):
        for port in (2222, 22, None):
            monkeypatch.setattr(hc, "ssh_target", target(loop, port))
            for alias in ("tunnel-box", "box1", "localhost"):   # an ssh alias, a loopback machines entry, a name
                assert not hc.own_host(alias, known), (loop, port, alias)
            argv, here = hc.command(cfg, tmp_path, "tunnel-box", "uptime", [], known)
            assert not here and argv[0] == "ssh" and argv[-2:] == ["tunnel-box", "uptime"], (loop, port, argv)
    assert seen[-1] == str((tmp_path / "ssh.conf").resolve()), "ssh -G reads the access's own config file"
    # A non-loopback name of this machine counts only on port 22 with no proxy hop; ssh not answering: remote.
    me = socket.gethostname()
    for port, proxy in ((22, True), (2222, False), (None, False)):
        monkeypatch.setattr(hc, "ssh_target", target(me, port, proxy))
        assert not hc.own_host("tunnel-box", known), (port, proxy)
    monkeypatch.setattr(hc, "ssh_target", lambda alias, config=None: None)
    assert not hc.own_host("tunnel-box", known)
    monkeypatch.setattr(hc, "ssh_target", target(me, 22))
    assert hc.command(cfg, tmp_path, "tunnel-box", "uptime", [], known) == (["uptime"], True)
    # `local` and this machine's own name never ask ssh, whatever its config says.
    monkeypatch.setattr(hc, "ssh_target", target("127.0.0.1", 2222, True))
    assert hc.own_host("local", known) and hc.own_host(me, known)
    for loop in ("127.0.0.1", "localhost", "::1"):
        assert not hc._local_address(loop), loop


def test_hostcheck_ssh_target_reads_one_ssh_g(monkeypatch):
    from ttp import hostcheck as hc
    seen = []
    out = "user u\nhostname localhost\nport 22\n"

    def fake(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(hc.subprocess, "run", fake)
    assert hc.ssh_target("inner") == {"hostname": "localhost", "port": 22, "proxy": False}
    assert seen == [["ssh", "-G", "inner"]]
    for extra in ("proxyjump bastion\n", "proxycommand nc %h %p\n", "ProxyJump bastion\n"):
        out = "hostname localhost\nport 22\n" + extra
        assert hc.ssh_target("inner", "/p/ssh.conf")["proxy"], extra
    assert seen[-1] == ["ssh", "-G", "-F", "/p/ssh.conf", "inner"] and len(seen) == 4, "one ssh -G per alias"
    out = "hostname 127.0.0.1\nport 2222\nproxycommand none\n"
    assert hc.ssh_target("tunnel-box") == {"hostname": "127.0.0.1", "port": 2222, "proxy": False}
    for bad in ("-oProxyCommand=id", "a b", "x;id"):
        assert hc.ssh_target(bad) is None
    assert len(seen) == 5
    monkeypatch.setattr(hc.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 255, "", "x"))
    assert hc.ssh_target("tunnel-box") is None


def test_hostcheck_run_times_out_caps_output_and_marks_unreachable(tmp_path, monkeypatch):
    from ttp import hostcheck as hc
    long_text = "x" * 10_000
    assert len(hc.trim(long_text)) < hc.MAX_OUT + 100 and "characters cut" in hc.trim(long_text)
    assert hc.trim("short") == "short"
    calls = []

    def fake(argv, here, behaviour):
        def run(a, **k):
            calls.append((a, k))
            if behaviour == "hang":
                raise subprocess.TimeoutExpired(a, k["timeout"])
            return subprocess.CompletedProcess(a, 0, stdout="y" * 9000, stderr="")
        monkeypatch.setattr(hc, "command", lambda *a, **k: (argv, here))
        monkeypatch.setattr(hc.subprocess, "run", run)
    fake(["uptime"], True, "hang")
    assert hc.run({}, tmp_path, "local", "uptime", [])[0] == hc.TIMED_OUT == 124
    assert calls[-1][1]["timeout"] == hc.TIMEOUT_S == 20 and calls[-1][1]["stdin"] == subprocess.DEVNULL
    fake(["ssh", "box1", "uptime"], False, "hang")
    rc, out = hc.run({}, tmp_path, "box1", "uptime", [])
    assert rc == hc.UNREACHABLE == 255 and "within 20 s" in out
    fake(["uptime"], True, "big")
    rc, out = hc.run({}, tmp_path, "local", "uptime", [])
    assert rc == 0 and len(out) <= hc.MAX_OUT + 100
    monkeypatch.undo()
    assert hc.run({}, tmp_path, "local", "nope", [])[0] == hc.REFUSED


def test_ttp_hostcheck_cli_runs_a_local_check(env):
    p = make(env)
    done = subprocess.run([sys.executable, "-m", "ttp.cli", "hostcheck", "local", "reachable"], cwd=p.root,
                          capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    assert done.returncode == 0 and done.stdout.strip() == "reachable", done.stderr
    done = subprocess.run([sys.executable, "-m", "ttp.cli", "hostcheck", "local", "rm", "-rf", "/"], cwd=p.root,
                          capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    assert done.returncode == 2 and "unknown check" in done.stdout


def test_coordtools_and_hostcheck_are_killscan_clean():
    from ttp import killscan
    for name in ("coordtools.py", "hostcheck.py"):
        assert not killscan.scan((RUNTIME / "ttp" / name).read_text()), name
