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
    want = ("Connected claude.ai connectors (claude.ai Gmail, claude.ai Example Notes) are not available to "
            "isolated workers; they cannot be passed on. Turn off providers.claude.worker_isolation to use them "
            "(or add them to providers.claude.mcp_withheld to keep them out)")
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
    # Listing a connector in mcp_servers asks for it, but isolated workers still never get it.
    g = mcpgap.gap({"worker_isolation": True, "mcp_servers": ["claude.ai Gmail", "search"]}, probe)
    assert g["state"] == "missing" and g["missing"] == []
    assert g["connectors"] == ["claude.ai Gmail", "claude.ai Example Notes"]
    assert g["line"].startswith("Connected claude.ai connectors (claude.ai Gmail, claude.ai Example Notes)")
    assert g["line"].endswith(". Listing claude.ai Gmail in providers.claude.mcp_servers does not reach "
                              "isolated workers")
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
