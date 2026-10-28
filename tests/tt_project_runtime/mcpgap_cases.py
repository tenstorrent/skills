# MCP servers connected on the host that isolated Claude workers do not get (ttp/mcpgap.py).
from __future__ import annotations

MCP_LIST = """Checking MCP server health…

claude.ai Gmail: https://example.invalid/mcp - ✔ Connected
claude.ai Example Notes: https://example.invalid/notes - ✔ Connected
claude.ai Example Calendar: https://example.invalid/cal - ! Needs authentication
claude.ai Example Drive: https://example.invalid/drive - ✗ Failed to connect
plugin:tools:helper: npx helper - ✔ Connected
search: /usr/local/bin/search-mcp  - ✔ Connected
tickets: https://example.invalid/tickets (HTTP) - ✔ Connected
docs: npx docs-mcp - ✗ Failed to connect
wiki: npx wiki-mcp - ✗ Not connected
chat: https://example.invalid/chat - ! Needs authentication
"""


def _fake_claude(tmp_path, body: str) -> str:
    exe = tmp_path / "fake-claude"
    exe.write_text("#!/bin/sh\n" + body + "\n")
    exe.chmod(0o755)
    return str(exe)


def _listing(tmp_path) -> str:
    (tmp_path / "mcp_list.txt").write_text(MCP_LIST)
    return _fake_claude(tmp_path, f'cat "{tmp_path / "mcp_list.txt"}"')


def _isolated(env, **claude):
    p = make(env)
    p.set_config("providers.claude.worker_isolation", True)
    for k, v in claude.items():
        p.set_config(f"providers.claude.{k}", v)
    return p


def test_mcp_list_parse_keeps_only_connected_servers():
    from ttp import mcpgap
    assert mcpgap.parse(MCP_LIST) == ["claude.ai Gmail", "claude.ai Example Notes", "plugin:tools:helper",
                                      "search", "tickets"]
    assert mcpgap.parse("No MCP servers configured. Use `claude mcp add` to add a server.\n") == []


def test_mcp_gap_none_missing_shows_no_line(env, tmp_path):
    from ttp import mcpgap
    from ttp.cli import status_text
    from ttp.web import health
    p = _isolated(env, mcp_servers=["search", "tickets"], mcp_withheld=["claude.ai Gmail", "claude.ai Example Notes"])
    e = mcpgap.refresh(p.state, _listing(tmp_path), str(p.root))
    assert e["ok"] and e["connected"]
    g = mcpgap.gap(p.config()["providers"]["claude"], mcpgap.read(p.state))
    assert g == {"state": "none", "missing": [], "connectors": [], "line": ""}
    assert "MCP servers connected" not in status_text(p)
    assert health(p, p.db, alive=False)["mcp_gap"] == ""


def test_mcp_gap_some_missing_is_one_status_and_feed_line_never_the_top(env, tmp_path):
    from ttp import mcpgap
    from ttp.cli import status_text
    from ttp.web import attention, health
    p = _isolated(env, mcp_servers=["search"], mcp_withheld=["claude.ai Gmail", "claude.ai Example Notes"])
    mcpgap.refresh(p.state, _listing(tmp_path), str(p.root))
    want = ("MCP servers connected on this host but not given to workers: tickets (add to "
            "providers.claude.mcp_servers, or to providers.claude.mcp_withheld to keep them out)")
    # Connectors, plugin servers and servers that are not connected are never named.
    assert [ln for ln in status_text(p).splitlines() if "MCP servers connected" in ln] == [want]
    assert health(p, p.db, alive=False)["mcp_gap"] == want
    assert attention(p.db, time.time()) == []   # information, not something the user must act on
    # With isolation off workers load every server: nothing is missing, whatever the cache holds.
    p.set_config("providers.claude.worker_isolation", False)
    assert mcpgap.line(p.state, p.config()) == ""


def test_mcp_gap_withheld_servers_are_not_named(env, tmp_path):
    from ttp import mcpgap
    from ttp.cli import status_text
    p = _isolated(env)
    mcpgap.refresh(p.state, _listing(tmp_path), str(p.root))
    assert "search, tickets (add to" in mcpgap.line(p.state, p.config())
    p.set_config("providers.claude.mcp_withheld", "tickets")
    assert ": search (add to" in mcpgap.line(p.state, p.config())
    p.set_config("providers.claude.mcp_withheld", ["search", "tickets", "claude.ai Gmail"])
    assert mcpgap.line(p.state, p.config()).startswith("Connected claude.ai connectors (claude.ai Example Notes)")
    p.set_config("providers.claude.mcp_withheld", ["search", "tickets", "claude.ai Gmail", "claude.ai Example Notes"])
    assert mcpgap.line(p.state, p.config()) == ""
    assert "MCP servers connected" not in status_text(p)


def test_mcp_gap_probe_timeout_or_failure_is_unknown_never_a_warning(env, tmp_path, capsys, monkeypatch):
    from ttp import mcpgap
    from ttp.cli import status_text
    p = _isolated(env)
    slow = _fake_claude(tmp_path, "sleep 30")
    t0 = time.time()
    e = mcpgap.refresh(p.state, slow, str(p.root), timeout=0.5)
    assert time.time() - t0 < 10 and not e["ok"] and "timed out" in e["error"]
    g = mcpgap.gap(p.config()["providers"]["claude"], mcpgap.read(p.state))
    assert g["state"] == "unknown" and g["line"] == "" and "timed out" in g["error"]
    assert "MCP servers connected" not in status_text(p)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    _ttp_main("doctor", p.name)
    assert "mcp servers on this host: unknown (timed out" in capsys.readouterr().out
    # A failing CLI and a cache never written are unknown too; a failure is asked again within RETRY_S.
    e = mcpgap.refresh(p.state, _fake_claude(tmp_path, "echo not logged in >&2; exit 1"), str(p.root))
    assert not e["ok"] and "exit 1" in e["error"]
    assert mcpgap.gap(p.config()["providers"]["claude"], mcpgap.read(p.state))["state"] == "unknown"
    assert mcpgap.due(e, e["at"] + mcpgap.RETRY_S) and not mcpgap.due(e, e["at"] + 60)
    (p.state / mcpgap.CACHE).write_text("not json")
    assert mcpgap.gap(p.config()["providers"]["claude"], mcpgap.read(p.state))["state"] == "unknown"


def test_mcp_gap_daemon_probes_in_the_background_and_only_when_due(env, tmp_path, monkeypatch):
    import ttp.daemon as dm
    from ttp import mcpgap
    p = _isolated(env)
    d = dm.Daemon(p.base)
    slow = _fake_claude(tmp_path, f'sleep 2; cat "{tmp_path / "mcp_list.txt"}"')
    (tmp_path / "mcp_list.txt").write_text(MCP_LIST)

    class Claude:
        def binary(self):
            return slow
    monkeypatch.setattr(dm, "get_provider", lambda name: Claude())
    d.probe_mcp()
    assert d._mcp_probe is None   # the test switch: no real `claude mcp list` under test
    monkeypatch.delenv("TTP_TEST_NO_MCP_PROBE")
    t0 = time.time()
    d.probe_mcp()
    assert time.time() - t0 < 1.5, "the tick never waits for the probe"
    assert mcpgap.read(p.state) is None
    d._mcp_probe.join(30)
    assert mcpgap.read(p.state)["connected"]
    first = d._mcp_probe
    d._mcp_tried = 0.0
    d.probe_mcp()
    assert d._mcp_probe is first, "a fresh answer is not asked again"
    # Isolation off: no probe at all.
    (p.state / mcpgap.CACHE).unlink()
    p.set_config("providers.claude.worker_isolation", False)
    d.cfg = p.config()
    d.probe_mcp()
    assert d._mcp_probe is first and mcpgap.read(p.state) is None


def test_mcp_list_split_keeps_connected_connectors_apart_from_servers():
    from ttp import mcpgap
    names = mcpgap.parse(MCP_LIST + "claude_ai_Example_Wiki: https://example.invalid/w - ✔ Connected\n")
    servers, connectors = mcpgap.split(names)
    assert servers == ["search", "tickets"]   # plugin servers in neither list
    # Only connectors that are connected: not those needing authentication or failing to connect.
    assert connectors == ["claude.ai Gmail", "claude.ai Example Notes", "claude_ai_Example_Wiki"]


def test_mcp_gap_connected_connectors_get_their_own_line_only_with_isolation(env, tmp_path):
    from ttp import mcpgap
    from ttp.cli import status_text
    from ttp.web import health
    p = _isolated(env, mcp_servers=["search", "tickets"])
    mcpgap.refresh(p.state, _listing(tmp_path), str(p.root))
    want = ("Connected claude.ai connectors (claude.ai Gmail, claude.ai Example Notes) are not given to isolated "
            "workers. List one in providers.claude.mcp_servers to give it to them (every other server stays out), "
            "or add them to providers.claude.mcp_withheld to keep them out")
    g = mcpgap.gap(p.config()["providers"]["claude"], mcpgap.read(p.state))
    assert g["state"] == "missing" and g["missing"] == [] and g["line"] == want
    assert g["connectors"] == ["claude.ai Gmail", "claude.ai Example Notes"]
    assert "Example Calendar" not in g["line"] and "Example Drive" not in g["line"] and "plugin:" not in g["line"]
    assert [ln for ln in status_text(p).splitlines() if "claude.ai connectors" in ln] == [want]
    assert health(p, p.db, alive=False)["mcp_gap"] == want
    # A server gap and a connector gap: two lines, servers first.
    p.set_config("providers.claude.mcp_servers", ["search"])
    assert mcpgap.line(p.state, p.config()).split("\n") == [
        "MCP servers connected on this host but not given to workers: tickets (add to "
        "providers.claude.mcp_servers, or to providers.claude.mcp_withheld to keep them out)", want]
    # Isolation off: connectors load in workers, no line.
    p.set_config("providers.claude.worker_isolation", False)
    assert mcpgap.line(p.state, p.config()) == ""
    assert "claude.ai connectors" not in status_text(p)


def test_mcp_gap_connectors_needing_authentication_or_a_failed_probe_show_no_line(env, tmp_path):
    from ttp import mcpgap
    from ttp.cli import status_text
    p = _isolated(env)
    listing = tmp_path / "auth.txt"
    listing.write_text("claude.ai Example Calendar: https://example.invalid/cal - ! Needs authentication\n"
                       "claude.ai Example Drive: https://example.invalid/drive - ✗ Failed to connect\n"
                       "plugin:tools:helper: npx helper - ✔ Connected\n")
    mcpgap.refresh(p.state, _fake_claude(tmp_path, f'cat "{listing}"'), str(p.root))
    g = mcpgap.gap(p.config()["providers"]["claude"], mcpgap.read(p.state))
    assert g["state"] == "none" and g["connectors"] == []
    # A probe that fails after printing connected connectors stays unknown: no line, no false warning.
    (tmp_path / "mcp_list.txt").write_text(MCP_LIST)
    mcpgap.refresh(p.state, _fake_claude(tmp_path, f'cat "{tmp_path / "mcp_list.txt"}"; exit 1'), str(p.root))
    g = mcpgap.gap(p.config()["providers"]["claude"], mcpgap.read(p.state))
    assert g["state"] == "unknown" and g["line"] == "" and g["connectors"] == []
    assert "claude.ai connectors" not in status_text(p)


def test_mcp_gap_connector_listed_in_mcp_servers_stays_named_only_withheld_drops_it():
    from ttp import mcpgap
    probe = {"ok": True, "connected": ["claude.ai Gmail", "claude.ai Example Notes", "search"]}
    # A listed connector reaches isolated workers only with a host list to trust (mcpgap.plan); this
    # probe has none (no "names"), so the listed one is named with why, the other as not given.
    g = mcpgap.gap({"worker_isolation": True, "mcp_servers": ["claude.ai Gmail", "search"]}, probe)
    assert g["state"] == "missing" and g["missing"] == []
    assert g["connectors"] == ["claude.ai Gmail", "claude.ai Example Notes"]
    assert g["line"].split("\n")[0].startswith("Connected claude.ai connectors (claude.ai Example Notes) are not given")
    assert g["line"].split("\n")[1] == (
        "claude.ai connectors listed in providers.claude.mcp_servers (claude.ai Gmail) do not reach isolated "
        "workers: no list of this host's MCP servers yet; they run with --strict-mcp-config until that clears")
    trusted = {**probe, "at": time.time(), "names": probe["connected"]}
    g = mcpgap.gap({"worker_isolation": True, "mcp_servers": ["claude.ai Gmail", "search"]}, trusted)
    assert g["connectors"] == ["claude.ai Example Notes"] and "Gmail" not in g["line"]
    g = mcpgap.gap({"worker_isolation": True, "mcp_servers": ["claude.ai Gmail"]},
                   {"ok": True, "connected": ["claude.ai Gmail"]})
    assert g["state"] == "missing" and g["connectors"] == ["claude.ai Gmail"] and "claude.ai Gmail" in g["line"]
    # Only mcp_withheld drops a connector, listed in mcp_servers or not.
    for cfg in ({"mcp_servers": ["claude.ai Gmail"], "mcp_withheld": ["claude.ai Gmail"]},
                {"mcp_withheld": ["claude.ai Gmail"]}):
        g = mcpgap.gap({"worker_isolation": True, **cfg}, {"ok": True, "connected": ["claude.ai Gmail"]})
        assert g["state"] == "none" and g["line"] == "" and g["connectors"] == []
    # Servers keep both filters: listed or withheld, not named.
    g = mcpgap.gap({"worker_isolation": True, "mcp_servers": ["search"], "mcp_withheld": ["tickets"]},
                   {"ok": True, "connected": ["search", "tickets"]})
    assert g["state"] == "none"


def test_mcp_list_states_names_connected_needs_auth_and_failed():
    from ttp import mcpgap
    st = mcpgap.states(MCP_LIST)
    assert st["claude.ai Gmail"] == "connected" and st["claude.ai Example Calendar"] == "needs_auth"
    assert st["claude.ai Example Drive"] == "failed" and st["wiki"] == "failed" and st["chat"] == "needs_auth"
    assert "Checking MCP server health…" not in st and mcpgap.states("") == {}
    assert mcpgap.parse(MCP_LIST) == [n for n, s in st.items() if s == "connected"]


# Isolated workers that keep the claude.ai connectors a project lists (mcpgap.plan, Claude.keep_connectors).
HOST_NAMES = ["claude.ai Gmail", "claude.ai Example Notes", "claude.ai Example Calendar", "plugin:tools:helper",
              "docs", "other", "device-srv"]


def _host_list(p, at=None, **extra):
    from ttp import mcpgap
    from ttp.project import write_json
    entry = {"at": time.time() if at is None else at, "ok": True, "connected": HOST_NAMES[:2], "names": HOST_NAMES}
    write_json(p.state / mcpgap.CACHE, {**entry, **extra})


def _iso_run(p, monkeypatch, **kw):
    argv, run_dir, _ = _dispatch_claude_worker(p, monkeypatch, {}, **kw)
    # Ended, with its task, so the next dispatch is not held by the parallel-run cap.
    p.db.x("UPDATE runs SET status='ok', ended=? WHERE status='running'", (time.time(),))
    p.db.x("UPDATE tasks SET status='done' WHERE status!='done'")
    return argv, json.loads((run_dir / "run.json").read_text())["env"]


def _shape(argv):
    """argv without the per-run values (session id, temp files) for comparing two runs' flags."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("--session-id", "--mcp-config", "--append-system-prompt-file", "--system-prompt-file"):
            skip = True
        out.append(a)
    return out


def test_isolated_worker_without_a_listed_connector_keeps_todays_strict_args(env, monkeypatch, tmp_path):
    from ttp.providers import get_provider
    assert get_provider("claude").isolation_args() == ["--strict-mcp-config", "--setting-sources", "project,local"]
    p = make(env)
    _claude_mcp_config(tmp_path, monkeypatch, p)
    _host_list(p)   # a trusted host list changes nothing while no connector is listed
    p.set_config("providers.claude.mcp_servers", ["docs"])
    argv, run_env = _iso_run(p, monkeypatch)
    i = argv.index("--strict-mcp-config")
    assert argv[i - 2] == "--mcp-config" and argv[i:i + 3] == ["--strict-mcp-config", "--setting-sources",
                                                               "project,local"]
    assert "deniedMcpServers" not in argv[argv.index("--settings") + 1]
    assert not any("mcp__" in a for a in argv) and "ENABLE_CLAUDEAI_MCP_SERVERS" not in run_env
    assert p.db.one("SELECT 1 FROM messages WHERE ref LIKE 'mcp_connectors_strict:%'") is None
    # A run that lists a connector but cannot keep it (no host list) gets exactly the same flags.
    (p.state / "mcp_list.json").unlink()
    p.set_config("providers.claude.mcp_servers", ["docs", "claude.ai Gmail"])
    again, again_env = _iso_run(p, monkeypatch)
    per_run = ("TTP_TASK", "TTP_TASK_ID", "TTP_RUN_ID", "TTP_RUN_DIR")
    assert _shape(again) == _shape(argv)
    assert {k: v for k, v in again_env.items() if k not in per_run} == {k: v for k, v in run_env.items()
                                                                         if k not in per_run}


def test_isolated_worker_with_a_listed_connector_keeps_it_and_keeps_every_other_server_out(env, monkeypatch,
                                                                                           tmp_path):
    from ttp import mcpgap
    p = make(env)
    _claude_mcp_config(tmp_path, monkeypatch, p)   # config scopes: docs, other (user), shared (.mcp.json)
    _host_list(p)
    # However the connector is written, it is the same one; the file server still goes in --mcp-config.
    for listed in ("claude.ai Gmail", "claude_ai_Gmail", "mcp__claude_ai_Gmail"):
        p.set_config("providers.claude.mcp_servers", ["docs", listed])
        argv, run_env = _iso_run(p, monkeypatch)
        assert "--strict-mcp-config" not in argv, listed
        assert "ENABLE_CLAUDEAI_MCP_SERVERS" not in run_env, "connectors stay on"
        i = argv.index("--setting-sources")
        assert argv[i + 1] == "project,local"
        denied = argv[argv.index("--disallowedTools", argv.index("--settings")) + 1].split(",")
        assert sorted(denied) == sorted(["mcp__claude_ai_Example_Notes", "mcp__claude_ai_Example_Calendar",
                                         "mcp__plugin_tools_helper", "mcp__other", "mcp__device-srv",
                                         "mcp__shared"]), listed
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert sorted(d["serverName"] for d in settings["deniedMcpServers"]) == sorted(
            ["claude.ai Example Notes", "claude.ai Example Calendar", "plugin:tools:helper", "other",
             "device-srv", "shared"])
        assert "hooks" in settings, "the run's own settings stay"
        j = argv.index("--mcp-config")
        assert argv[j + 2].startswith("--"), "--mcp-config takes several values: a flag must follow it"
        assert list(json.loads(pathlib.Path(argv[j + 1]).read_text())["mcpServers"]) == ["docs"]
        pathlib.Path(argv[j + 1]).unlink()
    assert p.db.one("SELECT 1 FROM messages WHERE ref LIKE 'mcp_servers_unknown:%'") is None, \
        "a connector is not a missing file server"
    # The feed names only connectors the project does not list.
    g = mcpgap.gap(p.config()["providers"]["claude"], mcpgap.read(p.state))
    assert g["connectors"] == ["claude.ai Example Notes"] and "claude.ai Gmail" not in g["line"]
    assert "List one in providers.claude.mcp_servers" in g["line"]


def test_isolated_worker_with_a_connector_stays_strict_without_a_trusted_host_list(env, monkeypatch, tmp_path,
                                                                                capsys):
    from ttp import cli, mcpgap
    p = make(env)
    _claude_mcp_config(tmp_path, monkeypatch, p)
    p.set_config("providers.claude.mcp_servers", ["claude.ai Gmail"])
    plugin = tmp_path / "plug"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "plug", "mcpServers": {"x": {}}}))
    cases = [("no list", lambda: None, "no list of this host's MCP servers yet"),
             ("unreadable", lambda: (p.state / mcpgap.CACHE).write_text("{not json"), "no list"),
             ("too old", lambda: _host_list(p, at=time.time() - mcpgap.TRUST_S - 60), "h old"),
             ("from the future", lambda: _host_list(p, at=time.time() + 3600), "h old"),
             ("before names", lambda: (_host_list(p), mcpgap.write_json(
                 p.state / mcpgap.CACHE, {k: v for k, v in mcpgap.read(p.state).items() if k != "names"})), "no list"),
             ("failed probe", lambda: mcpgap.write_json(p.state / mcpgap.CACHE, {"at": time.time(), "ok": False,
                                                                                  "error": "timed out"}), "timed out"),
             ("not on the host", lambda: _host_list(p, names=["docs"]), "not on this host's list"),
             ("plugin servers", lambda: (_host_list(p), p.set_config("providers.claude.plugin_dirs", [str(plugin)])),
              "plugin dirs bring MCP servers")]
    for name, setup, why in cases:
        (p.state / mcpgap.CACHE).unlink(missing_ok=True)
        setup()
        argv, run_env = _iso_run(p, monkeypatch)
        assert "--strict-mcp-config" in argv and not any("mcp__" in a for a in argv), name
        assert "deniedMcpServers" not in argv[argv.index("--settings") + 1], name
        if name == "no list":   # one low alert a day, whatever the reason
            alert = p.db.one("SELECT text, severity FROM messages WHERE ref='mcp_connectors_strict:claude'")
            assert alert and why in alert["text"] and alert["severity"] == "low", alert and alert["text"]
        monkeypatch.setattr(cli, "need", lambda *a: p)
        cli.cmd_doctor(types.SimpleNamespace(name="demo"))
        out = capsys.readouterr().out
        assert "claude.ai connectors for isolated workers: none, workers stay strict" in out and why in out, name
    # A failed probe keeps the last good list while it is fresh: the run keeps its connector.
    p.set_config("providers.claude.plugin_dirs", [])
    _host_list(p)
    mcpgap.refresh(p.state, _fake_claude(tmp_path, "exit 3"), str(p.root))
    e = mcpgap.read(p.state)
    assert not e["ok"] and e["last_ok"]["names"] == HOST_NAMES
    argv, _ = _iso_run(p, monkeypatch)
    assert "--strict-mcp-config" not in argv
    mcpgap.refresh(p.state, _fake_claude(tmp_path, "exit 3"), str(p.root))   # and again: still the same list
    assert mcpgap.read(p.state)["last_ok"]["names"] == HOST_NAMES
    # The feed names a listed connector that cannot be kept, with why.
    _host_list(p, at=time.time() - mcpgap.TRUST_S - 60)
    g = mcpgap.gap(p.config()["providers"]["claude"], mcpgap.read(p.state))
    assert "listed in providers.claude.mcp_servers (claude.ai Gmail) do not reach isolated workers" in g["line"]
    assert "h old" in g["line"]


def test_mcp_list_names_every_server_and_an_old_cache_is_probed_again(tmp_path):
    from ttp import mcpgap
    assert mcpgap.listed(MCP_LIST)[:4] == ["claude.ai Gmail", "claude.ai Example Notes",
                                           "claude.ai Example Calendar", "claude.ai Example Drive"]
    assert "docs" in mcpgap.listed(MCP_LIST) and "wiki" in mcpgap.listed(MCP_LIST)
    assert len(mcpgap.listed(MCP_LIST)) == 10
    now = time.time()
    assert mcpgap.due({"at": now, "ok": True, "connected": []}, now), "a list from before names: ask again"
    assert not mcpgap.due({"at": now, "ok": True, "connected": [], "names": []}, now)
    e = mcpgap.probe(_listing(tmp_path), str(tmp_path))
    assert e["names"] == mcpgap.listed(MCP_LIST)
    assert mcpgap.tool_name("claude.ai Google Drive") == "claude_ai_Google_Drive"
    assert mcpgap.tool_name("claude.ai  Example (beta)") == "claude_ai_Example_beta"
    assert mcpgap.tool_name("plugin:tools:helper") == "plugin_tools_helper"
    assert mcpgap.tool_name("my.server-1") == "my_server-1"


def test_only_claude_keeps_connectors_codex_and_cursor_are_unchanged(env, monkeypatch, tmp_path):
    from ttp.providers import get_provider
    argv = ["agent", "-p", "--settings", "{}"]
    for name in ("codex", "cursor"):
        prov = get_provider(name)
        assert prov.keep_connectors(list(argv), ["x"]) is None, name
        assert not prov.is_connector("claude.ai Gmail") and prov.mcp_server_names(["/"]) == [], name
    p = make(env)
    _host_list(p)
    p.set_config("providers.codex.mcp_servers", ["claude.ai Gmail"])
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    assert d._connector_deny(get_provider("codex"), "codex", str(p.root), []) is None
    assert d._connector_deny(get_provider("claude"), "claude", str(p.root), []) is None, "claude lists none"
    # The Claude args: a trailing "-" stays last, a settings value that is not JSON keeps the run strict.
    claude = get_provider("claude")
    assert claude.keep_connectors(["claude", "--settings", "{}", "-"], ["a b"])[-3:] == [
        "--setting-sources", "project,local", "-"]
    assert claude.keep_connectors(["claude", "--settings", "/tmp/s.json"], ["a"]) is None
    assert claude.keep_connectors(["claude"], []) == ["claude", "--settings", "{}", "--setting-sources",
                                                      "project,local"]
