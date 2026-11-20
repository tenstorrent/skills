# Codex workers' MCP servers from providers.codex.mcp_servers (ttp/providers/codexmcp.py).
from __future__ import annotations

CODEX_MCP_JSON = {"mcpServers": {
    "docs": {"type": "stdio", "command": "docs mcp", "args": ["--root", "a dir/with \"quotes\"", "back\\slash"],
             "env": {"DOCS_TOKEN": "literal-docs-secret", "DOCS_HOME": "${HOME}"}},
    "my server.v2": {"command": "srv", "args": []},
    "tickets": {"type": "http", "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer literal-bearer-secret", "X-Api-Key": "${TICKETS_KEY}"}},
    "events": {"type": "sse", "url": "https://example.invalid/sse"},
    "needs-var": {"command": "x", "env": {"A": "${TTP_TEST_UNSET_VAR}"}},
    "arg-var": {"command": "x", "args": ["--key", "${TICKETS_KEY}"]},
    "url-var": {"type": "http", "url": "https://example.invalid/?k=${TICKETS_KEY:-d}"},
    "cmd-var": {"command": "${HOME}/bin/x"},
}}
CODEX_USER_TOML = """model = "m"
[mcp_servers.mine]
command = "mine-mcp"
[mcp_servers."with.dot"]
url = "https://example.invalid/u"
[mcp_servers.docs]
command = "user-docs"
"""
# `codex mcp list --json` as codex-cli 0.160 prints it (trimmed).
CODEX_MCP_LIST = json.dumps([
    {"name": "docs", "enabled": True, "disabled_reason": None,
     "transport": {"type": "stdio", "command": "docs mcp", "args": [], "env": None, "env_vars": ["DOCS_TOKEN"]},
     "auth_status": "unsupported"},
    {"name": "mine", "enabled": False, "disabled_reason": None,
     "transport": {"type": "stdio", "command": "mine-mcp", "args": []}, "auth_status": "unsupported"},
    {"name": "stray", "enabled": True, "transport": {"type": "streamable_http", "url": "https://example.invalid/s",
                                                     "bearer_token_env_var": None}},
])


def _codex_mcp_table(args):
    """The table in the single `-c mcp_servers=...` argument, read with a TOML parser."""
    assert args[0] == "-c" and args[1].startswith("mcp_servers=") and len(args) == 2, args
    text = args[1][len("mcp_servers="):]
    try:
        import tomllib
        return tomllib.loads(args[1])["mcp_servers"]
    except ImportError:   # Python before 3.11: the subset codexmcp writes (strings, bools, arrays, tables)
        value, end = _codex_toml_value(text, 0)
        assert end == len(text), text[end:]
        return value


def _codex_toml_value(s, i):
    if s[i] == '"':
        m = re.compile(r'"((?:[^"\\]|\\.)*)"').match(s, i)
        return json.loads('"' + m.group(1) + '"'), m.end()
    if s[i] in "[{":
        close, out = ("]", []) if s[i] == "[" else ("}", {})
        i += 1
        while s[i] != close:
            if close == "}":
                key, i = _codex_toml_value(s, i)
                assert s[i] == "=", s[i:]
                out[key], i = _codex_toml_value(s, i + 1)
            else:
                item, i = _codex_toml_value(s, i)
                out.append(item)
            i += 2 if s.startswith(", ", i) else 0
        return out, i + 1
    m = re.compile(r"true|false").match(s, i)
    return m.group(0) == "true", m.end()


def test_codex_listed_servers_become_one_c_table_whose_toml_quoting_survives(tmp_path):
    from ttp.providers import codexmcp
    (tmp_path / ".mcp.json").write_text(json.dumps(CODEX_MCP_JSON))
    environ = {"HOME": "/home/u", "TICKETS_KEY": "k"}
    args, secrets, unknown = codexmcp.run_args(
        ["docs", "my server.v2", "tickets", "events", "needs-var", "arg-var", "url-var", "cmd-var", "nowhere"],
        [str(tmp_path / "x"), str(tmp_path)], [], True, environ)
    assert unknown == ["events (type sse: Codex runs stdio and streamable HTTP servers only)",
                       "needs-var (${TTP_TEST_UNSET_VAR} is not set)",
                       "arg-var (its args names ${TICKETS_KEY}, whose value would go on the command line)",
                       "url-var (its url names ${TICKETS_KEY}, whose value would go on the command line)",
                       "cmd-var (its command names ${HOME}, whose value would go on the command line)", "nowhere"]
    assert "k" not in [x for a in args for x in a.split('"')], "a variable's value never reaches the argument"
    # The exact text, so the escaping is pinned even where no TOML parser is installed.
    assert '"docs"={"command"="docs mcp", "args"=["--root", "a dir/with \\"quotes\\"", "back\\\\slash"]' in args[1]
    table = _codex_mcp_table(args)
    assert table == {
        "docs": {"command": "docs mcp", "args": ["--root", 'a dir/with "quotes"', "back\\slash"],
                 "env_vars": ["DOCS_TOKEN", "DOCS_HOME"], "enabled": True},
        "my server.v2": {"command": "srv", "args": [], "enabled": True},
        "tickets": {"url": "https://example.invalid/mcp", "bearer_token_env_var": "TTP_MCP_TICKETS_TOKEN",
                    "env_http_headers": {"X-Api-Key": "TICKETS_KEY"}, "enabled": True}}
    assert secrets == {"DOCS_TOKEN": "literal-docs-secret", "DOCS_HOME": "/home/u",
                       "TTP_MCP_TICKETS_TOKEN": "literal-bearer-secret"}
    assert codexmcp.run_args([], [str(tmp_path)], ["mine"], False, environ) == ([], {}, []), \
        "no list: the user's servers load as before"
    # Two listed servers that need different values in one variable: the second is not given.
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "a": {"command": "a", "env": {"K": "1"}}, "b": {"command": "b", "env": {"K": "2"}}}}))
    args, secrets, unknown = codexmcp.run_args(["a", "b"], [str(tmp_path)], [], True, {})
    assert list(_codex_mcp_table(args)) == ["a"] and secrets == {"K": "1"}
    assert unknown == ["b (its K differs from another listed server's)"]


def test_codex_listed_servers_load_in_the_real_cli(tmp_path, monkeypatch):
    """codex-cli itself reads the argument (offline, no sign-in), when it is installed."""
    import shutil
    exe = shutil.which("codex")
    if not exe:
        pytest.skip("codex is not installed")
    from ttp.providers import codexmcp
    (tmp_path / ".mcp.json").write_text(json.dumps(CODEX_MCP_JSON))
    home = tmp_path / "codexhome"
    home.mkdir()
    (home / "config.toml").write_text(CODEX_USER_TOML)
    args, _, _ = codexmcp.run_args(["docs", "my server.v2", "tickets"], [str(tmp_path)],
                                   ["mine", "with.dot", "docs"], False, {"HOME": "/h", "TICKETS_KEY": "k"})
    r = subprocess.run([exe, *args, "mcp", "list", "--json"], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "CODEX_HOME": str(home)}, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    rows = {s["name"]: s for s in json.loads(r.stdout)}
    assert {n: s["enabled"] for n, s in rows.items()} == {
        "docs": True, "my server.v2": True, "tickets": True, "mine": False, "with.dot": False}
    assert rows["docs"]["transport"]["command"] == "user-docs", "a listed user server stays as defined"
    assert rows["my server.v2"]["transport"]["command"] == "srv"


def test_codex_turns_off_unlisted_user_servers_only_when_it_reads_the_user_config(tmp_path, monkeypatch):
    from ttp.providers import codexmcp, get_provider
    from ttp.providers.codex import IGNORE_USER_CONFIG
    (tmp_path / ".mcp.json").write_text(json.dumps(CODEX_MCP_JSON))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codexhome"))
    (tmp_path / "codexhome").mkdir()
    (tmp_path / "codexhome" / "config.toml").write_text(CODEX_USER_TOML)
    prov = get_provider("codex")
    args, _, unknown = prov.mcp_args(["docs", "my server.v2"], [str(tmp_path)], ["codex", "exec", "-"])
    assert _codex_mcp_table(args) == {"docs": {"enabled": True}, "my server.v2": {"command": "srv", "args": [],
                                                                               "enabled": True},
                                      "mine": {"enabled": False}, "with.dot": {"enabled": False}}
    assert unknown == []
    args, _, _ = prov.mcp_args(["docs"], [str(tmp_path)], ["codex", "exec", IGNORE_USER_CONFIG, "-"])
    assert set(_codex_mcp_table(args)) == {"docs"}, "an isolated worker never reads config.toml"
    assert _codex_mcp_table(args)["docs"]["command"] == "docs mcp", "isolated: the .mcp.json entry"
    # The names, with and without tomllib (Python before 3.11 has none), and the inline forms.
    toml = CODEX_USER_TOML + "[mcp_servers]\ninline = { command = \"i\" }\n'lit.q'.command = \"l\"\n[other]\nx = 1\n"
    want = ["mine", "with.dot", "docs", "inline", "lit.q"]
    assert sorted(codexmcp.user_server_names(toml)) == sorted(want)
    monkeypatch.setitem(sys.modules, "tomllib", None)
    assert codexmcp.user_server_names(toml) == want
    assert codexmcp.user_server_names("mcp_servers.top.command = \"t\"\n") == ["top"]
    assert codexmcp.user_server_names("") == []


def test_codex_worker_mcp_secrets_stay_out_of_argv_and_run_json(env, monkeypatch, tmp_path):
    p = make(env)
    (p.root / ".mcp.json").write_text(json.dumps(CODEX_MCP_JSON))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codexhome"))
    monkeypatch.setenv("TICKETS_KEY", "env-ticket-secret")
    (tmp_path / "codexhome").mkdir()
    (tmp_path / "codexhome" / "config.toml").write_text(CODEX_USER_TOML)
    p.set_config("providers.codex.mcp_servers", ["my server.v2", "tickets", "docs", "events"])
    from ttp.providers import codex as codex_provider
    from ttp.daemon import Daemon
    monkeypatch.setattr(codex_provider.Codex, "binary", lambda self: "/usr/bin/true")   # never a real agent
    d = Daemon(p.base)
    tid = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
    rid = d.start_run("worker", "go", "codex", "light", str(p.root), task=p.db.task(tid))
    run_dir = p.runs / str(rid)
    (run_dir / "STOP").touch()
    text = (run_dir / "run.json").read_text()
    spec = json.loads(text)
    for secret in ("literal-docs-secret", "literal-bearer-secret", "env-ticket-secret"):
        assert secret not in text, secret
    assert spec["argv"][-1] == "-"
    i = spec["argv"].index(next(a for a in spec["argv"] if a.startswith("mcp_servers=")))
    table = _codex_mcp_table(spec["argv"][i - 1:i + 1])
    assert table["mine"] == {"enabled": False} and table["docs"] == {"enabled": True}, "not isolated"
    assert table["tickets"]["bearer_token_env_var"] == "TTP_MCP_TICKETS_TOKEN"
    [path] = spec["env_files"]
    assert path in spec["private_files"] and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert p.root not in pathlib.Path(path).parents and run_dir not in pathlib.Path(path).parents
    from ttp import runner
    assert runner._env_files(spec) == {"TTP_MCP_TICKETS_TOKEN": "literal-bearer-secret"}
    alert = p.db.one("SELECT text, severity FROM messages WHERE ref='mcp_servers_unknown:codex'")
    assert alert and "events (type sse" in alert["text"] and alert["severity"] == "low"
    runner.remove_private(run_dir)
    assert not os.path.exists(path) and runner._env_files(spec) == {}
    # Isolated: config.toml's servers are not named at all.
    p.set_config("providers.codex.worker_isolation", True)
    monkeypatch.setattr(codex_provider.Codex, "_ignore_user_config", lambda self: [codex_provider.IGNORE_USER_CONFIG])
    rid = Daemon(p.base).start_run("worker", "go", "codex", "light", str(p.root), task=p.db.task(tid))
    (p.runs / str(rid) / "STOP").touch()
    spec = json.loads((p.runs / str(rid) / "run.json").read_text())
    arg = next(a for a in spec["argv"] if a.startswith("mcp_servers="))
    assert "mine" not in arg and "literal-docs-secret" not in json.dumps(spec)
    runner.remove_private(p.runs / str(rid))


def test_doctor_checks_codex_workers_mcp_servers_with_codex_mcp_list(env, monkeypatch, tmp_path, capsys):
    from ttp.providers import codexmcp
    assert codexmcp.parse_list(CODEX_MCP_LIST) == {"docs": True, "mine": False, "stray": True}
    assert codexmcp.parse_list("not json") == {} and codexmcp.parse_list('{"a": 1}') == {}
    p = make(env)
    (p.root / ".mcp.json").write_text(json.dumps(CODEX_MCP_JSON))
    (tmp_path / "list.json").write_text(CODEX_MCP_LIST)
    exe = tmp_path / "fake-codex"
    exe.write_text(f'#!/bin/sh\nif [ "$1" = exec ]; then echo "--ignore-user-config"; exit 0; fi\n'
                   f'printf "%s\\n" "$@" > "{tmp_path}/argv"; echo "CODEX_HOME=$CODEX_HOME" >> "{tmp_path}/argv"\n'
                   f'cat "{tmp_path}/list.json"\n')
    exe.chmod(0o755)
    from ttp.providers import codex as codex_provider
    monkeypatch.setattr(codex_provider.Codex, "binary", lambda self: str(exe))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codexhome"))
    p.set_config("providers.codex.mcp_servers", ["docs", "events"])
    p.set_config("providers.codex.worker_isolation", True)
    from ttp import cli
    monkeypatch.setattr(cli, "need", lambda *a: p)
    cli.cmd_doctor(types.SimpleNamespace(name="demo"))
    out = capsys.readouterr().out
    assert "codex mcp servers for workers: docs, events" in out
    assert "not given (define them in the project's .mcp.json): events (type sse" in out
    assert "`codex mcp list` with the workers' arguments: docs, stray enabled · not listed but on: stray" in out
    argv = (tmp_path / "argv").read_text()
    assert argv.startswith("-c\nmcp_servers=") and "\nmcp\nlist\n--json\n" in argv
    assert str(tmp_path / "codexhome") not in argv, "an isolated worker's view is read without config.toml"
    assert "literal-docs-secret" not in out + argv
    (tmp_path / "list.json").write_text("Error: failed to load")
    cli.cmd_doctor(types.SimpleNamespace(name="demo"))
    assert "`codex mcp list --json` did not answer" in capsys.readouterr().out


def test_providers_doc_names_codex_per_run_mcp_servers():
    doc = (pathlib.Path(__file__).resolve().parents[1] / "plugins/tt-project/docs/providers.md").read_text()
    row = next(line for line in doc.splitlines() if line.startswith("| Isolation from user config"))
    codex = row.split("|")[3]
    assert "No per-run MCP list" not in codex and "mcp_servers" in codex and "enabled=false" in codex


def test_codex_mcp_server_env_never_changes_the_workers_environment(env, monkeypatch, tmp_path):
    """A listed server whose env would change a variable the worker already has (PATH, the device
    mask, TTP_*) is not given, and the runner's secret files never override the daemon's env."""
    from ttp.providers import codexmcp
    from ttp import machines
    mask = next(iter(machines.DEVICE_MASK))
    servers = {"path": {"command": "x", "env": {"PATH": "/x"}},
               "mask": {"command": "x", "env": {mask: "0"}},
               "nodev": {"command": "x", "env": {"TTP_NO_DEVICE": "0"}},
               "pypath": {"command": "x", "env": {"PYTHONPATH": "/evil"}},
               "same": {"command": "x", "env": {"PATH": "/bin", "OWN": "v"}},
               "fine": {"command": "x", "env": {"OWN": "v"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    _, secrets, unknown = codexmcp.run_args(list(servers), [str(tmp_path)], [], True,
                                            {"PATH": "/bin"}, {"PATH": "/bin", "TTP_NO_DEVICE": "1"})
    assert sorted(u.split(" ")[0] for u in unknown) == ["mask", "nodev", "path", "pypath"], unknown
    assert "its env PATH would change the worker's own" in unknown[0]
    assert secrets == {"PATH": "/bin", "OWN": "v"}, "an identical value is fine"
    # The daemon: the server is not given, and the alert names it.
    p = make(env)
    (p.root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codexhome"))
    (tmp_path / "codexhome").mkdir()
    p.set_config("providers.codex.mcp_servers", ["path", "fine"])
    from ttp.providers import codex as codex_provider
    from ttp.daemon import Daemon
    monkeypatch.setattr(codex_provider.Codex, "binary", lambda self: "/usr/bin/true")
    tid = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
    rid = Daemon(p.base).start_run("worker", "go", "codex", "light", str(p.root), task=p.db.task(tid))
    run_dir = p.runs / str(rid)
    (run_dir / "STOP").touch()
    spec = json.loads((run_dir / "run.json").read_text())
    from ttp import runner
    assert runner._env_files(spec) == {"OWN": "v"}
    arg = next(a for a in spec["argv"] if a.startswith("mcp_servers="))
    assert '"path"' not in arg and "fine" in arg
    alert = p.db.one("SELECT text FROM messages WHERE ref='mcp_servers_unknown:codex'")
    assert alert and "path (its env PATH would change" in alert["text"]
    runner.remove_private(run_dir)
    # The runner: an env file's value never overrides the daemon's environment.
    out, secret = tmp_path / "env.json", tmp_path / "secret.json"
    secret.write_text(json.dumps({"PATH": "/evil", "TTP_NO_DEVICE": "0", "OWN": "v"}))
    child = tmp_path / "child.py"
    child.write_text(f"import json, os; open({str(out)!r}, 'w').write(json.dumps(dict(os.environ)))\n")
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "prompt.md").write_text("x")
    (rd / "run.json").write_text(json.dumps({
        "argv": [sys.executable, str(child)], "cwd": str(tmp_path), "provider": "codex", "timeout_s": 60,
        "env": {"PATH": os.environ["PATH"], "TTP_NO_DEVICE": "1"}, "env_files": [str(secret)]}))
    runner.supervise(rd)
    got = json.loads(out.read_text())
    assert (got["PATH"], got["TTP_NO_DEVICE"], got["OWN"]) == (os.environ["PATH"], "1", "v")
