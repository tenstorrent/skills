# The ssh access bridge (`ttp bridge`, bridge.py), of tests/test_tt_project_runtime.py, which runs this file
# in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
# No test reaches the network or starts a service.
from __future__ import annotations


def _bridge_cfg(**box):
    b = {"login": {"agent": "~/agents/login.sock", "identity": "~/keys/login.pub"},
         "agents": {"auto": {"agent": "env", "forward": [
             {"name": "build-a", "aliases": ["ba"], "target": "build-a.example.com", "port": 2301, "user": "builder"},
             {"name": "build-b", "target": "10.0.0.2", "port": 2302, "adopt": True, "host_key_alias": "build-b"}]},
             "git": {"agent": "~/agents/with space/git.sock", "direct": [{"name": "git.example.com", "user": "git"}]}}}
    b.update(box)
    from ttp import bridge
    return bridge.parse({"boxes": {"box-1": b}})


def test_bridge_config_parses_hosts_ports_and_agents():
    cfg = _bridge_cfg()
    assert cfg["socket_dir"] == "~/.ssh/tt-agent"
    a = cfg["boxes"]["box-1"]["agents"]
    assert [h["port"] for h in a["auto"]["forward"]] == [2301, 2302]
    assert a["auto"]["forward"][0]["host_key_alias"] == "build-a.example.com"   # defaults to the target
    assert a["auto"]["forward"][1]["adopt"] is True
    assert a["git"]["direct"] == [{"name": "git.example.com", "aliases": [], "user": "git"}]


def test_bridge_config_refuses_unsafe_or_ambiguous_entries():
    import pytest
    from ttp import bridge
    host = {"name": "h", "target": "h.example.com", "port": 2301}
    bad = [
        {"boxes": {"-oProxyCommand=x": {"agents": {"a": {"agent": "env", "forward": [host]}}}}},   # option as a box
        {"boxes": {"b": {"login": {"identity": "~/keys/login"}, "agents": {"a": {"agent": "env", "forward": [host]}}}}},
        {"boxes": {"b": {"agents": {"a": {"agent": "env", "forward": [{**host, "port": 22}]}}}}},       # privileged port
        {"boxes": {"b": {"agents": {"a": {"agent": "env", "forward": [{**host, "target": "h.example.com:443"}]}}}}},
        {"boxes": {"b": {"agents": {"a": {"agent": "env", "forward": [host, {**host, "name": "h2"}]}}}}},   # port twice
        {"boxes": {"b": {"agents": {"a": {"agent": "env", "forward": [host, {**host, "port": 2302}]}}}}},   # name twice
        {"boxes": {"b": {"agents": {"a": {"agent": "env"}}}}},                                             # no hosts
        {"socket_dir": "/tmp/x", "boxes": {"b": {"agents": {"a": {"agent": "env", "forward": [host]}}}}},
    ]
    for data in bad:
        with pytest.raises(bridge.BadConfig):
            bridge.parse(data)


def test_bridge_session_forwards_one_agent_and_only_ssh_ports_on_its_own_connection(tmp_path, monkeypatch):
    from ttp import bridge
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _bridge_cfg()
    argv = bridge.ssh_argv(cfg, "box-1", "auto", ssh="ssh")
    line = " ".join(argv)
    assert "ControlMaster=no" in line and "ControlPath=none" in line and "ExitOnForwardFailure=yes" in line
    assert "ForwardAgent=yes" in argv   # 'env': this computer's SSH_AUTH_SOCK
    assert argv[argv.index("-R") + 1] == "127.0.0.1:2301:build-a.example.com:22"
    assert argv.count("-R") == 1   # the adopted port is someone else's forward
    assert "-D" not in argv and "-L" not in argv and "ProxyJump" not in line   # no route for other traffic
    assert f"IdentityFile={tmp_path}/keys/login.pub" in argv and "IdentitiesOnly=yes" in argv
    assert argv[-2] == "box-1"
    remote = argv[-1]
    assert 'ln -sfn "$SSH_AUTH_SOCK" "$HOME/.ssh/tt-agent/auto.sock"' in remote
    assert 'while [ -S "$SSH_AUTH_SOCK" ]' in remote and "exit 3" in remote
    git = bridge.ssh_argv(cfg, "box-1", "git", ssh="ssh")
    assert f'ForwardAgent="{tmp_path}/agents/with space/git.sock"' in git and "-R" not in git


def test_bridge_box_block_names_agent_links_and_no_key_file():
    from ttp import bridge
    block = bridge.render_block(_bridge_cfg(), "box-1")
    assert block.startswith(bridge.BEGIN) and block.rstrip().endswith(bridge.END)
    assert "Host build-a ba\n  HostName 127.0.0.1\n  Port 2301\n  HostKeyAlias build-a.example.com" in block
    assert "Host build-b\n  HostName 127.0.0.1\n  Port 2302" in block
    assert block.count("IdentityAgent ~/.ssh/tt-agent/auto.sock") == 2
    assert "Host git.example.com\n  IdentityAgent ~/.ssh/tt-agent/git.sock\n  User git" in block
    assert "\nMatch all\n" in block   # what follows the block is not scoped to its last Host
    assert "IdentityFile" not in block and "ForwardAgent" not in block and "Proxy" not in block


def test_bridge_block_check_refuses_key_files_key_text_and_proxies():
    import pytest
    from ttp import bridge
    for bad in ("Host x\n  IdentityFile ~/.ssh/id_ed25519\n", "-----BEGIN OPENSSH PRIVATE KEY-----\n",
                "Host x\n  ProxyJump ws\n", "Host x\n  DynamicForward 1080\n", "Host x\n  ForwardAgent yes\n"):
        with pytest.raises(bridge.BadConfig):
            bridge.check_block(bridge.BEGIN + "\n" + bad + bridge.END + "\n")


def test_bridge_merge_puts_block_first_replaces_an_old_one_and_keeps_the_rest():
    from ttp import bridge
    block = bridge.render_block(_bridge_cfg(), "box-1")
    old = "Include other/config\nHost legacy\n  HostName 127.0.0.1\n  Port 2301\n"
    new = bridge.merge(old, block)
    assert new.startswith(block) and new.endswith(old)
    assert bridge.merge(new, block) == new   # idempotent
    stale = "Host first\n  User a\n\n" + bridge.BEGIN + "\nHost gone\n" + bridge.END + "\nHost last\n"
    again = bridge.merge(stale, block)
    assert "Host gone" not in again and again.count(bridge.BEGIN) == 1
    assert again.startswith(block) and "Host first\n  User a\n" in again and again.endswith("Host last\n")
    assert bridge.strip(again) == "Host first\n  User a\n\nHost last\n"
    assert bridge.merge("", block) == block


def test_bridge_probe_reads_agents_ports_and_stray_files():
    from ttp import bridge
    p = bridge.parse_probe("block yes\nagent auto up 1\nagent git down 0\nport 2301 up\nport 2302 down\n"
                           "file sock auto.sock\nfile other id_ed25519\n")
    assert p == {"block": True, "agents": {"auto": {"up": True, "keys": 1}, "git": {"up": False, "keys": 0}},
                 "ports": {2301: True, 2302: False}, "other_files": ["id_ed25519"]}
    script = bridge.probe_script(_bridge_cfg(), "box-1")
    assert script.startswith("bash -c ") and "/dev/tcp/127.0.0.1/2301" in script and "ssh-add -l" in script


def test_bridge_example_config_parses_with_placeholder_hosts_only():
    import json
    from ttp import bridge
    path = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "tt-project" / "docs" / "access-bridge.example.json"
    cfg = bridge.parse(json.loads(path.read_text()))
    for box in cfg["boxes"]:
        bridge.render_block(cfg, box)
        for h in [h for a in cfg["boxes"][box]["agents"].values() for h in a["forward"]]:
            assert h["target"].endswith(".example.com") or h["target"].startswith("10.0.0.")


def test_bridge_service_runs_the_session_and_restarts_it(tmp_path, monkeypatch):
    from ttp import bridge
    monkeypatch.setenv("HOME", str(tmp_path))
    argv = bridge.ssh_argv(_bridge_cfg(), "box-1", "auto", ssh="ssh")
    pl = bridge.launchd_plist("box-1", "auto", argv)
    assert pl["Label"] == "com.tt-project.bridge.box-1.auto" and pl["KeepAlive"] and pl["ProgramArguments"] == argv
    unit = bridge.systemd_unit("box-1", "auto", argv)
    assert "Restart=always" in unit and "$$SSH_AUTH_SOCK" in unit   # systemd would expand a bare $VAR
