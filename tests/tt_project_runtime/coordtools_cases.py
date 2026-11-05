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
    argv, _, _, p = _coord_argv(env, monkeypatch, {"mode": "on", "tools": "read_only"})
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


def _answer(p, tmp_path, monkeypatch, actions, run="run"):
    """The coordinator hook's decision on a StructuredOutput answer of `actions` in session run `run`:
    (permission decision or None, its reason)."""
    from ttp import coordtools
    (tmp_path / run).mkdir(exist_ok=True)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path / run))
    out, _ = coordtools.pre_tool_use({"tool_name": "StructuredOutput",
                                      "tool_input": {"actions": actions, "summary": "s", "session_note": None}})
    if out is None:
        return None, ""
    o = out["hookSpecificOutput"]
    return o["permissionDecision"], o["permissionDecisionReason"]


def test_a_session_answer_with_a_bad_task_update_comes_back_in_the_same_turn(env, tmp_path, monkeypatch):
    from ttp import coordinator as coord
    p = make(env)
    tid = p.db.add_task("port kernel", "s", origin="user")
    gone = p.db.add_task("old probe", "s", origin="user")
    p.db.update_task(gone, status="failed")
    p.db.update_task(tid, status="blocked")
    reply = {"type": "reply", "chat": None, "text": "on it", "id": None, "status": None}
    bad = [reply, {"type": "task_update", "id": tid + 1000, "status": "queued", "spec": None},
           {"type": "task_update", "id": tid, "status": "queued", "depends_on": [gone], "spec": None}]
    before = p.db.q("SELECT id, status, depends_on FROM tasks ORDER BY id")
    decision, why = _answer(p, tmp_path, monkeypatch, bad)
    assert decision == "deny"
    assert f"actions[1] (task_update): no task #{tid + 1000}" in why
    assert f"actions[2] (task_update): #{tid} rejected: depends on #{gone} which is failed" in why
    assert "actions[0]" not in why and "send the whole answer again" in why
    assert p.db.q("SELECT id, status, depends_on FROM tasks ORDER BY id") == before, "the check writes nothing"
    # The model fixes both in the same turn; the fixed answer goes through and applies.
    fixed = [reply, {"type": "task_update", "id": tid, "status": "queued", "depends_on": [], "spec": None}]
    assert _answer(p, tmp_path, monkeypatch, fixed) == (None, "")
    assert coord.apply(p, [{k: v for k, v in a.items() if v is not None} for a in fixed[1:]]) == []
    assert p.db.task(tid)["status"] == "queued"


def test_a_valid_session_answer_is_not_changed(env, tmp_path, monkeypatch):
    p = make(env)
    tid = p.db.add_task("port kernel", "s", origin="user")
    run = p.db.add_task("bench", "s", origin="user")
    p.db.update_task(run, status="running")
    ok = [{"type": "task_update", "id": tid, "status": "blocked", "text": "needs a box"},
          {"type": "task_update", "id": run, "spec": "also label each number", "status": None},
          {"type": "task_update", "id": run, "status": "cancelled"},
          {"type": "task_add", "title": "new work", "spec": "do it", "kind": "code", "depends_on": [tid]},
          {"type": "reply", "text": "done"}, "not a dict"]
    assert _answer(p, tmp_path, monkeypatch, ok) == (None, "")
    assert not (tmp_path / "run" / "answer-precheck.json").exists()
    assert _answer(p, tmp_path, monkeypatch, []) == (None, "")
    # A batch that requeues a failed dependency, or cancels a duplicate, before the action that needs it:
    # apply runs them in order and accepts both, so the check does too.
    gone = p.db.add_task("old probe", "s", origin="user")
    p.db.update_task(gone, status="failed")
    seq = [{"type": "task_update", "id": gone, "status": "queued"},
           {"type": "task_update", "id": tid, "status": "queued", "depends_on": [gone]},
           {"type": "task_update", "id": run, "status": "cancelled"},
           {"type": "task_add", "title": "bench", "spec": "again", "depends_on": [gone]}]
    assert _answer(p, tmp_path, monkeypatch, seq) == (None, "")
    from ttp import coordinator as coord
    assert coord.apply(p, seq) == []


def test_session_answer_check_refuses_what_apply_refuses_and_never_traps_the_turn(env, tmp_path, monkeypatch):
    from ttp import coordinator as coord, coordprecheck
    p = make(env)
    tid = p.db.add_task("bench", "s", origin="user")
    p.db.update_task(tid, status="running")
    done = p.db.add_task("landed", "s", origin="user")
    p.db.update_task(done, status="done")
    cases = [({"type": "task_update", "id": tid, "status": "queued"}, f"task #{tid} is running"),
             ({"type": "task_update", "id": tid, "resources": ["box"]}, "its resources change only"),
             ({"type": "task_update", "id": done, "start_after": "1h"}, "only a task that has not started"),
             ({"type": "task_update", "id": done, "pr_branch": "x"}, "its branch changes only"),
             ({"type": "task_update", "id": tid, "status": "waiting"}, "status `waiting`"),
             ({"type": "task_update", "id": "abc"}, "needs the id of a task"),
             ({"type": "task_add", "title": "bench", "spec": "s"}, f"duplicate of open task #{tid}"),
             ({"type": "task_add", "title": "x", "spec": " "}, coord.MISSING_FIELDS),
             ({"type": "task_add", "title": "x", "spec": "s", "kind": "nope"}, coord.UNKNOWN_KIND),
             ({"type": "task_add", "title": "x", "spec": "s", "continues": 99999}, "no task #99999"),
             ({"type": "task_add", "title": "x", "spec": "s", "depends_on": [99999]}, "names no task")]
    for i, (a, want) in enumerate(cases):
        got = coordprecheck.problems(p, [a])
        assert len(got) == 1 and want in got[0][1], (a, got)
        assert coord.INFO_MARK not in got[0][1]
        # apply refuses each one too (the check is a subset of apply's)
        assert coord.apply(p, [a]), a
    # A task cap reached: refused, unless a config_set in the batch changes a cap (apply runs it first).
    p.set_config("coordinator.max_new_tasks_per_day", 1)
    add = {"type": "task_add", "title": "fresh", "spec": "s"}
    assert coordprecheck.problems(p, [add]) == []
    assert [i for i, _ in coordprecheck.problems(p, [add, {**add, "title": "fresh 2"}])] == [1]
    raise_cap = {"type": "config_set", "key": "coordinator.max_new_tasks_per_day", "value": "5"}
    assert coordprecheck.problems(p, [raise_cap, add, {**add, "title": "fresh 2"}]) == []
    # An action resent unchanged goes through, and apply refuses it with its side effects (the retry wake).
    two = [add, {**add, "title": "fresh 2"}]
    decision, why = _answer(p, tmp_path, monkeypatch, two, run="cap")
    assert decision == "deny" and "actions[1] (task_add): the 24 h cap" in why
    assert _answer(p, tmp_path, monkeypatch, two, run="cap") == (None, "")
    assert coord.apply(p, two) and p.db.kv(coord.RETRY_WAKE_KEY)
    # At most MAX_DENIALS answers of a run are denied, each with a new problem.
    for n in range(coordprecheck.MAX_DENIALS + 1):
        decision, _ = _answer(p, tmp_path, monkeypatch, [{"type": "task_update", "id": 5000 + n}], run="many")
        assert decision == ("deny" if n < coordprecheck.MAX_DENIALS else None), n
    # No run dir, no project, an unreadable project or a broken check: the answer goes through.
    bad = [{"type": "task_update", "id": 777777}]
    assert _answer(p, tmp_path, monkeypatch, bad, run="fresh")[0] == "deny"
    monkeypatch.delenv("TTP_RUN_DIR")
    from ttp import coordtools
    so = {"tool_name": "StructuredOutput", "tool_input": {"actions": bad}}
    assert coordtools.pre_tool_use(so) == (None, None)
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path / "fresh2"))
    (tmp_path / "fresh2").mkdir()
    monkeypatch.setenv("TTP_PROJECT", str(tmp_path / "nowhere" / "x"))
    assert coordtools.pre_tool_use(so) == (None, None)
    assert not (tmp_path / "nowhere").exists(), "no database is created"
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setattr(coordprecheck, "problems", lambda *a: 1 / 0)
    assert coordtools.pre_tool_use(so) == (None, None)
    for answer in (None, "x", {"actions": "x"}, {"actions": None}):
        assert coordtools.pre_tool_use({"tool_name": "StructuredOutput", "tool_input": answer}) == (None, None)


def test_the_answer_check_runs_through_the_coordinator_hook_entry_point(env, tmp_path):
    p = make(env)
    run = tmp_path / "run"
    run.mkdir()
    payload = json.dumps({"tool_name": "StructuredOutput", "tool_input": {"actions": [
        {"type": "task_update", "id": 424242, "status": "queued"}]}})
    environ = {**os.environ, "TTP_PROJECT": str(p.base), "TTP_RUN_DIR": str(run), "PYTHONPATH": str(RUNTIME)}
    hook = subprocess.run([sys.executable, "-m", "ttp.hook", "CoordPreToolUse"], input=payload, env=environ,
                          capture_output=True, text=True, cwd=str(RUNTIME), timeout=60)
    out = json.loads(hook.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "no task #424242" in out["permissionDecisionReason"]


def _coord_settings(argv):
    return json.loads(argv[argv.index("--settings") + 1])


def test_full_session_coordinator_gets_a_workers_tools_and_guards(env, monkeypatch):
    from ttp import coordsession
    argv, _, _, p = _coord_argv(env, monkeypatch, {"mode": "on"})
    assert argv[argv.index("--tools") + 1] == "Bash,Read,Edit,Write,Skill,ToolSearch,WebFetch,WebSearch"
    assert "--safe-mode" not in argv and "--restricted" in argv
    assert not any(argv[i:i + 3] == ["--disallowedTools", "Edit", "Write"] for i in range(len(argv))), \
        "the read-only turn's Edit/Write deny goes"
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--allowedTools") + 10]
    assert {"StructuredOutput", "Bash", "Edit", "Write"} <= set(allowed)
    s = _coord_settings(argv)
    assert s["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith("CoordPreToolUse")
    assert s["hooks"]["PostToolUse"][0]["hooks"][0]["command"].endswith("CoordPostToolUse")
    assert s["hooks"]["PostToolUseFailure"][0]["hooks"][0]["command"].endswith("CoordPostToolUseFailure")
    deny = s["permissions"]["deny"]
    assert f"Edit(/{p.charter_path})" in deny and f"Edit(/{p.harness / 'memory'}/**)" in deny
    assert f"Edit(/{p.config_path})" in deny and f"Edit(/{p.state / 'project.db'})" in deny
    assert s["env"]["TTP_COORD_FULL_TOOLS"] == "1" and s["env"]["BASH_MAX_TIMEOUT_MS"] == "300000"
    assert str(p.charter_path) in s["env"]["TTP_COORD_PROTECTED"].split(os.pathsep)
    # read_only (or any unknown value) keeps the read-only set; so does a session's stateless fallback.
    for cfg in ({"mode": "on", "tools": "read_only"}, {"mode": "on", "tools": "everything"}):
        argv, _, _, _ = _coord_argv(env, monkeypatch, cfg)
        assert argv[argv.index("--tools") + 1] == "Read,Bash", cfg
        assert "TTP_COORD_FULL_TOOLS" not in _coord_settings(argv)["env"]
    monkeypatch.setattr(coordsession, "decide", lambda *a, **k: ("stateless", "test"))
    argv, _, _, _ = _coord_argv(env, monkeypatch, {"mode": "on"})
    assert argv[argv.index("--tools") + 1] == "Read,Bash"
    # The stateless turn (mode off) still has no tools at all.
    argv, _, _, _ = _coord_argv(env, monkeypatch, None)
    assert argv[argv.index("--tools") + 1] == "" and "--settings" not in argv


def _full_env(tmp_path, monkeypatch, timeout_s="300"):
    from ttp import coordtools
    harness, state, run = tmp_path / "harness", tmp_path / "state", tmp_path / "run"
    for d in (harness / "memory", state, run):
        d.mkdir(parents=True)
    protected = [harness / "CHARTER.md", harness / "MEMORY.md", harness / "memory", harness / "project.json",
                 state / "project.db"]
    monkeypatch.setenv(coordtools.FULL_ENV, "1")
    monkeypatch.setenv(coordtools.PROTECTED_ENV, os.pathsep.join(map(str, protected)))
    monkeypatch.setenv(coordtools.TIMEOUT_ENV, timeout_s)
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.delenv(coordtools.DENIED_ENV, raising=False)
    return harness, state, run


def _decide(tmp_path, tool, **args):
    from ttp import coordtools
    out, _ = coordtools.pre_tool_use({"tool_name": tool, "tool_input": args, "cwd": str(tmp_path)})
    return "deny" if out and out["hookSpecificOutput"].get("permissionDecision") == "deny" else "allow"


def test_full_tools_hook_allows_writes_and_denies_protected_files_and_pushes(tmp_path, monkeypatch):
    harness, state, _ = _full_env(tmp_path, monkeypatch)
    for tool, args in (("Write", {"file_path": str(tmp_path / "a.py"), "content": "x"}),
                       ("Edit", {"file_path": str(tmp_path / "b.md"), "old_string": "a", "new_string": "b"}),
                       ("Read", {"file_path": str(harness / "CHARTER.md")}), ("StructuredOutput", {"actions": []}),
                       ("Skill", {"skill": "x"}), ("WebFetch", {"url": "https://example.com"})):
        assert _decide(tmp_path, tool, **args) == "allow", tool
    allowed = ["echo hi > out.txt && mkdir -p build", "ssh box1 'ls /tmp'", "ttp task demo add 'fix x'",
               "git commit -m 'push the fix'", "git status && ./push.sh", "ttp lock box1 -- make", "ttp status",
               "rm -rf build", "git fetch origin"]
    for cmd in allowed:
        assert _decide(tmp_path, "Bash", command=cmd) == "allow", cmd
    for path in (harness / "CHARTER.md", harness / "CHARTER-notes.md", harness / "MEMORY.md",
                 harness / "memory" / "fact.md", harness / "project.json", state / "project.db",
                 state / "project.db-wal", "harness/CHARTER.md", harness / "memory" / ".." / "project.json"):
        for tool in ("Write", "Edit", "MultiEdit"):
            assert _decide(tmp_path, tool, file_path=str(path)) == "deny", (tool, path)
    denied = ["sed -i s/a/b/ harness/CHARTER.md", f"python3 -c \"open('{harness}/project.json','w')\"",
              "sqlite3 state/project.db 'delete from tasks'", "echo x >> harness/MEMORY.md",
              f"rm {harness}/memory/fact.md", "cp /tmp/x CHARTER.md",
              # a raw git push, every way: plain, by path, wrapped, nested in a shell or ssh, to a tag
              "git push origin HEAD:smartonTT/x", "/usr/bin/git -C repo push --tags", "env A=1 git push",
              "timeout 60 git push origin main", "sh -c 'cd r && git push origin main'",
              "ssh box1 'cd r; git push origin v1'", "bash -c \"bash -c 'git push'\"", "true; git push -f",
              # the worker's own checks still run
              "ttp say hi", "/usr/bin/gh pr ready 1"]
    for cmd in denied:
        assert _decide(tmp_path, "Bash", command=cmd) == "deny", cmd
    # read_only mode: the old list holds
    monkeypatch.delenv("TTP_COORD_FULL_TOOLS")
    assert _decide(tmp_path, "Write", file_path=str(tmp_path / "a.py")) == "deny"
    assert _decide(tmp_path, "Bash", command="echo hi > out.txt") == "deny"


def test_a_long_foreground_coordinator_command_is_cut_at_its_time_limit(tmp_path, monkeypatch):
    from ttp import coordtools
    _full_env(tmp_path, monkeypatch, timeout_s="120")
    assert _decide(tmp_path, "Bash", command="make", timeout=100_000) == "allow"
    assert _decide(tmp_path, "Bash", command="make", timeout=600_000) == "deny"
    assert _decide(tmp_path, "Bash", command="make", run_in_background=True) == "deny"
    out, _ = coordtools.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "make", "timeout": 600_000}})
    assert "ttp detach" in out["hookSpecificOutput"]["permissionDecisionReason"]
    # The CLI's own Bash ceiling is the limit, so a call naming no timeout is cut there too.
    cfg = {"coordinator": {"session": {"mode": "on", "tool_timeout_s": 90}}}
    s = coordtools.hook_settings(full=True, timeout_s=coordtools.tool_timeout_s(cfg))
    assert s["env"]["BASH_MAX_TIMEOUT_MS"] == s["env"]["BASH_DEFAULT_TIMEOUT_MS"] == "90000"
    assert coordtools.tool_timeout_s({}) == 300
    assert coordtools.tool_timeout_s({"coordinator": {"session": {"tool_timeout_s": 5000}}}) == 600
    assert coordtools.tool_timeout_s({"coordinator": {"session": {"tool_timeout_s": "x"}}}) == 300


def test_full_tools_log_each_mutating_call(tmp_path, monkeypatch):
    from ttp import coordtools
    _, _, run = _full_env(tmp_path, monkeypatch)
    calls = [({"tool_name": "Bash", "tool_input": {"command": "make install"}}, False),
             ({"tool_name": "Bash", "tool_input": {"command": "ttp lock b -- false"}, "error": "Exit code 2\nboom"}, True),
             ({"tool_name": "Edit", "tool_input": {"file_path": "/x/y.py"}}, False),
             ({"tool_name": "Bash", "tool_input": {"command": "ttp status"}}, False),   # a read: not logged
             ({"tool_name": "Read", "tool_input": {"file_path": "/x/y.py"}}, False),
             ({"tool_name": "Bash", "tool_input": {"command": "rm x"}, "agent_id": "a1"}, False)]
    for payload, failed in calls:
        assert coordtools.post_tool_use(payload, failed) == (None, None)
    rows = [json.loads(line) for line in (run / coordtools.AUDIT).read_text().splitlines()]
    assert [(r["tool"], r["target"], r["exit"]) for r in rows] == [
        ("Bash", "make install", 0), ("Bash", "ttp lock b -- false", 2), ("Edit", "/x/y.py", 0)]
    progress = (run / "progress.md").read_text()
    assert "coordinator Bash exit 2: ttp lock b -- false" in progress and "ttp status" not in progress
    # Through the hook's entry point too.
    hook = subprocess.run([sys.executable, "-m", "ttp.hook", "CoordPostToolUseFailure"],
                          input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/x/z"}, "error": "no"}),
                          capture_output=True, text=True, cwd=str(RUNTIME), timeout=30,
                          env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    assert hook.returncode == 0
    assert json.loads((run / coordtools.AUDIT).read_text().splitlines()[-1])["exit"] == 1
    monkeypatch.delenv("TTP_COORD_FULL_TOOLS")   # read_only mode logs nothing
    coordtools.post_tool_use({"tool_name": "Bash", "tool_input": {"command": "rm y"}})
    assert len((run / coordtools.AUDIT).read_text().splitlines()) == 4


def test_session_tools_key_loosens_only_on_the_users_word():
    from ttp import coordinator as coord, coordtools
    assert coord.NEEDS_USER["coordinator.session.tools"] == "restriction"
    assert "coordinator.session.tools" in coord.SAFE_WHEN_OFF
    assert coord.OFF_VALUE["coordinator.session.tools"] == "read_only"
    assert coordtools.tools_mode_of(" Read_Only ") == "read_only"
    with pytest.raises(ValueError):
        coordtools.tools_mode_of("all")
    assert coordtools.tools_mode({}) == "full"


def test_full_tools_coordinator_lands_nothing_and_moves_no_tag(tmp_path, monkeypatch):
    # The hook: every landing `ttp push`, also wrapped or nested; only the forms that move nothing pass.
    _full_env(tmp_path, monkeypatch)
    for cmd in ("cd wt && ttp push --detach", "ttp push --move-tag smartonTT/x-dogfood abc123", "ttp push",
                "ttp push --land --detach", "ttp push --own", "ttp clip -- ttp push", "env A=1 ttp push --batch m",
                "sh -c 'cd wt && ttp push'", "ssh box1 'ttp push --detach'", "ttp push --free --move-tag t c",
                "ttp push --queue --detach", "/x/bin/ttp push --marker m"):
        assert _decide(tmp_path, "Bash", command=cmd) == "deny", cmd
    for cmd in ("ttp push --queue", "ttp push --free", "ttp push --result /r/m", "ttp push --result /r/m --pushed",
                "ttp task demo add 'push the fix'"):
        assert _decide(tmp_path, "Bash", command=cmd) == "allow", cmd
    # `ttp push` itself, past the hook's text check: by the full-tools variable or the run's role.
    from ttp import cli, coordtools
    for var, val in ((coordtools.FULL_ENV, "1"), (coordtools.ROLE_ENV, "coordinator")):
        monkeypatch.delenv(coordtools.FULL_ENV, raising=False)
        monkeypatch.setenv(var, val)
        monkeypatch.delenv("TTP_TASK", raising=False)
        for args in (["push"], ["push", "--detach"], ["push", "--land"], ["push", "--own"],
                     ["push", "--move-tag", "t", "c"], ["push", "--free", "--move-tag", "t", "c"],
                     ["push", "--marker", "m"], ["push", "--batch", "m"]):
            with pytest.raises(SystemExit) as e:
                cli.main(args)
            assert e.value.code not in (0, None), (var, args)
        monkeypatch.delenv("TTP_PUSH_LAND", raising=False)
    monkeypatch.setenv(coordtools.ROLE_ENV, "worker")
    monkeypatch.delenv(coordtools.FULL_ENV, raising=False)
    assert not coordtools.coordinator_run()


def test_the_git_wrapper_refuses_a_coordinators_push_however_it_is_built(tmp_path):
    from ttp import coordtools
    wrapper = RUNTIME.parent / "template" / "bin" / "git"
    bare = tmp_path / "bin"
    bare.mkdir()
    shutil.copy(wrapper, bare / "git")   # no runtime beside it: the guard cannot load
    base = {k: v for k, v in os.environ.items() if k not in ("TTP_TASK", coordtools.FULL_ENV, coordtools.ROLE_ENV)}

    def run(bindir, extra, script="g=git; $g -C . push origin HEAD:refs/tags/v1"):
        env = {**base, **extra, "TTP_PYTHON": sys.executable, "PATH": f"{bindir}:{base.get('PATH', '')}"}
        return subprocess.run(["sh", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)

    for extra in ({coordtools.ROLE_ENV: "coordinator"}, {coordtools.FULL_ENV: "1"}):
        for bindir in (wrapper.parent, bare):
            out = run(bindir, extra)
            assert out.returncode == 1 and "coordinator" in out.stderr, (extra, bindir, out.stderr)
        out = run(wrapper.parent, extra, "git --version")
        assert out.returncode == 0 and out.stdout.startswith("git version"), out.stderr
    out = run(wrapper.parent, {coordtools.ROLE_ENV: "worker"})   # no run: the real git (no repo here)
    assert "coordinator" not in out.stderr and "not a git repository" in out.stderr, out.stderr


def test_a_coordinator_run_holds_no_device_and_the_device_rule_binds_it(env, tmp_path, monkeypatch):
    from ttp import coordtools, machines, sandbox
    _, _, _, p = _coord_argv(env, monkeypatch, {"mode": "on"})
    run = max((d for d in p.runs.iterdir() if d.name.isdigit()), key=lambda d: int(d.name))
    run_env = json.loads((run / "run.json").read_text())["env"]
    assert run_env["TTP_NO_DEVICE"] == "1" and run_env[coordtools.ROLE_ENV] == "coordinator"
    assert run_env["TTP_TASK"] == ""
    pid = p.db.one("SELECT pid FROM runs WHERE id=?", (int(run.name),))["pid"]
    for _ in range(200):   # its runner reads STOP and ends: none left behind
        if not pid or subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode:
            break
        time.sleep(0.05)
    _full_env(tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_NO_DEVICE", run_env["TTP_NO_DEVICE"])
    monkeypatch.delenv("TTP_HOOK_REWRITE", raising=False)
    monkeypatch.setattr(machines, "device_host", lambda *a, **k: True)
    monkeypatch.setattr(sandbox, "status", lambda: (["/sb/unshare", "--pid"], ""))
    assert _decide(tmp_path, "Bash", command="pytest tests/x.py") == "deny"
    assert _decide(tmp_path, "Bash", command="ttp clip -- ttp sandbox -- pytest tests/x.py") == "allow"
