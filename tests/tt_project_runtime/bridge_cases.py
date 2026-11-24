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
    assert a["git"]["direct"] == [{"name": "git.example.com", "aliases": [], "user": "git",
                                     "target": "", "host_key_alias": ""}]


def test_bridge_direct_host_may_name_its_address_and_known_host_key():
    # cells: direct-hostname
    # a lab host whose older config entry elsewhere names a stale known-hosts alias: the block's own
    # HostName and HostKeyAlias come first, so they win
    import pytest
    from ttp import bridge
    cfg = _bridge_cfg(agents={"auto": {"agent": "env", "direct": [
        {"name": "peer", "aliases": ["peer-long"], "target": "10.0.0.9", "host_key_alias": "10.0.0.9", "user": "u"}]}})
    block = bridge.render_block(cfg, "box-1")
    host = block[block.index("Host peer peer-long"):]
    assert "  HostName 10.0.0.9\n  HostKeyAlias 10.0.0.9\n  User u" in host
    with pytest.raises(bridge.BadConfig):
        bridge.parse({"boxes": {"b": {"agents": {"a": {"agent": "env", "direct": [{"name": "h", "target": "-oX"}]}}}}})


def test_bridge_forwarded_host_turns_off_an_older_proxy_and_only_that():
    # cells: proxy-none
    # an older entry further down (a dead route's proxy, IdentitiesOnly with its own key file) must not take
    # over a name the bridge forwards; `ProxyCommand none` is the one proxy line the block may hold
    import pytest
    from ttp import bridge
    block = bridge.render_block(_bridge_cfg(), "box-1")
    fwd = block[block.index("Host build-a"):block.index("Host build-b")]
    assert "  ProxyCommand none\n  IdentitiesOnly no\n" in fwd
    for bad in ("  ProxyCommand nc %h %p", "  ProxyCommand none extra", "  ProxyJump none"):
        with pytest.raises(bridge.BadConfig):
            bridge.check_block(block.replace("Match all", bad + "\nMatch all"))


def test_bridge_hold_check_keeps_a_held_host_refused():
    # cells: hold-check
    # with a box's hold check set, the proxy goes off only for a forwarded name the check does not hold, so a
    # hold's refusing proxy further down still applies to a held name
    import pytest
    from ttp import bridge
    cfg = _bridge_cfg()
    cfg["boxes"]["box-1"]["hold_check"] = "/opt/hold/ssh-hold-match"
    block = bridge.render_block(cfg, "box-1")
    fwd = block[block.index("Host build-a"):block.index("Host build-b")]
    assert "ProxyCommand" not in fwd
    tail = block[block.index("Match originalhost"):]
    assert tail.startswith('Match originalhost build-a,') and \
        'exec "sh -c \'/opt/hold/ssh-hold-match %n %h; [ $? -eq 1 ]\'"\n  ProxyCommand none\n' in tail
    for bad in ("relative/hold", "/opt/x; rm", '/opt/"x"'):
        with pytest.raises(bridge.BadConfig):
            bridge.parse({"boxes": {"b": {"hold_check": bad, "agents": {"a": {"agent": "env",
                          "forward": [{"name": "h", "target": "h.example.com", "port": 2301}]}}}}})


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
    # 'env': the service's own SSH_AUTH_SOCK, under a name ssh's IdentityAgent does not overwrite
    assert argv[:2] == ["/bin/sh", "-c"] and 'TTP_BRIDGE_AGENT="$SSH_AUTH_SOCK"' in argv[2] and argv[4] == "ssh"
    assert "ForwardAgent=${TTP_BRIDGE_AGENT}" in argv and "ForwardAgent=yes" not in argv
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
    assert "IdentityFile" not in block and "ForwardAgent" not in block and "ProxyJump" not in block
    assert [ln for ln in block.splitlines() if "Proxy" in ln] == ["  ProxyCommand none"] * 2   # off, never a proxy


def test_bridge_block_check_refuses_key_files_key_text_and_proxies():
    import pytest
    from ttp import bridge
    for bad in ("Host x\n  IdentityFile ~/.ssh/id_ed25519\n", "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----\n",
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


def test_bridge_hold_check_lifts_the_refusing_proxy_only_on_a_clear_not_held(tmp_path):
    # cells: hold-check-fail-closed
    # ssh itself reads the block (ssh -G, no connection, a scratch home and config): exit 1 lifts the older
    # entry's refusing proxy; exit 0 (held), a failing check and a missing one all keep it
    import shutil
    import subprocess
    import pytest
    from ttp import bridge
    if not shutil.which("ssh"):
        pytest.skip("no ssh client")
    home = tmp_path / "h"
    home.mkdir()
    check = tmp_path / "hold-check"
    cfg = _bridge_cfg()
    cfg["boxes"]["box-1"]["hold_check"] = str(check)
    conf = tmp_path / "config"
    conf.write_text(bridge.render_block(cfg, "box-1") + "\nHost build-a\n  ProxyCommand false held\n")
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SHELL": "/bin/sh"}

    def proxy() -> str:
        r = subprocess.run(["ssh", "-G", "-F", str(conf), "build-a"], capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0, r.stderr
        return next((ln.split(None, 1)[1] for ln in r.stdout.splitlines() if ln.startswith("proxycommand ")), "")

    for rc, held in ((1, False), (0, True), (2, True), (None, True)):
        if rc is None:
            check.unlink()
        else:
            check.write_text(f"#!/bin/sh\nexit {rc}\n")
            check.chmod(0o755)
        assert (proxy() == "false held") is held, rc


def test_bridge_example_config_parses_with_placeholder_hosts_only():
    import json
    from ttp import bridge
    path = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "tt-project" / "docs" / "access-bridge.example.json"
    cfg = bridge.parse(json.loads(path.read_text()))
    for box in cfg["boxes"]:
        bridge.render_block(cfg, box)
        for h in [h for a in cfg["boxes"][box]["agents"].values() for h in a["forward"]]:
            assert h["target"].endswith(".example.com") or h["target"].startswith("192.0.2.")


def test_bridge_service_runs_the_session_and_restarts_it(tmp_path, monkeypatch):
    from ttp import bridge
    monkeypatch.setenv("HOME", str(tmp_path))
    argv = bridge.ssh_argv(_bridge_cfg(), "box-1", "auto", ssh="ssh")
    pl = bridge.launchd_plist("box-1", "auto", argv)
    assert pl["Label"] == "com.tt-project.bridge.box-1.auto" and pl["KeepAlive"] and pl["ProgramArguments"] == argv
    unit = bridge.systemd_unit("box-1", "auto", argv)
    assert "Restart=always" in unit and "$$SSH_AUTH_SOCK" in unit   # systemd would expand a bare $VAR


def test_bridge_interface_hosts_go_through_the_relay_socket(tmp_path, monkeypatch):
    from ttp import bridge, tunnel
    monkeypatch.setattr(bridge, "HOME_DIR", tmp_path)
    monkeypatch.setattr(bridge, "RELAY", tmp_path / "bridge_relay.py")
    cfg = _bridge_cfg()
    cfg["boxes"]["box-1"]["agents"]["auto"]["forward"][0]["interface"] = "vpn0"
    argv = bridge.ssh_argv(cfg, "box-1", "auto", ssh="ssh")
    sock = tmp_path / "bridge-relay" / "box-1.auto" / "build-a.sock"
    assert argv[argv.index("-R") + 1] == f"127.0.0.1:2301:{sock}"
    svc = bridge.service_argv(cfg, "box-1", "auto", ssh="ssh")
    assert svc[:4] == ["/bin/sh", "-c", tunnel.SPREAD_SH, str(tmp_path / "bridge-box-1-auto.start")]   # the start delay
    assert svc[5:12] == [str(tmp_path / "bridge_relay.py"), "--tag", "box-1/auto", "--link", bridge.link(cfg, "auto"),
                        str(sock.parent), "build-a=build-a.example.com%vpn0"]
    assert svc[svc.index("--") + 1:] == argv
    plain = bridge.service_argv(_bridge_cfg(), "box-1", "auto", ssh="ssh")   # no interface host: still wrapped
    assert plain[5:11] == [str(tmp_path / "bridge_relay.py"), "--tag", "box-1/auto", "--link", bridge.link(cfg, "auto"),
                           str(sock.parent)]
    assert plain[11] == "--" and plain[12] == "/bin/sh"



def test_kept_ssh_services_wait_a_random_start_delay_but_not_on_the_start_that_installs_them(tmp_path, monkeypatch):
    # cells: spread-wait, spread-first-start, spread-bound, spread-env-agent, spread-own-connection
    import plistlib
    from ttp import bridge, hoststate, tunnel
    monkeypatch.delenv("TTP_TEST_NO_BRIDGE_REFRESH", raising=False)
    for mod in (bridge, tunnel):
        monkeypatch.setattr(mod, "HOME_DIR", tmp_path)
    monkeypatch.setattr(bridge, "RELAY", tmp_path / "bridge_relay.py")
    monkeypatch.setattr(hoststate, "unit_dir", lambda: tmp_path / "units")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "sleep").write_text('#!/bin/sh\nprintf "slept %s\\n" "$1"\n')
    (fake / "ssh").write_text(f'#!/bin/sh\nprintf "%s\\0" "${bridge.ENV_AGENT}" "$@"\n')
    for f in fake.iterdir():
        f.chmod(0o755)
    path = {"PATH": f"{fake}:{os.environ['PATH']}"}
    # The plist and the unit both carry the wait; the unit's escaping reads back as the same argv.
    cfg = _bridge_cfg()
    svc = bridge.service_argv(cfg, "box-1", "auto", platform="linux")
    marker = bridge.bridge_marker("box-1", "auto")
    assert svc[:4] == ["/bin/sh", "-c", tunnel.SPREAD_SH, str(marker)] and "/dev/urandom" in tunnel.SPREAD_SH
    assert bridge.launchd_plist("box-1", "auto", svc)["ProgramArguments"] == svc
    (tmp_path / "units").mkdir()
    bridge.service_file("box-1", "auto", "linux").write_text(bridge.systemd_unit("box-1", "auto", svc))
    assert bridge.installed_argv("box-1", "auto", "linux") == svc
    # Its ssh keeps its own connection, and the session after the relay's "--" is the same argv as before.
    assert svc[svc.index("--") + 1:] == bridge.ssh_argv(cfg, "box-1", "auto")
    assert "ControlMaster=no" in svc and "ControlPath=none" in svc
    # A restart without the marker sleeps 0..RECONNECT_SPREAD_S s, then execs the command.
    runs = [subprocess.run(tunnel.spread(["echo", "ran"], marker), env=path, capture_output=True, text=True,
                           timeout=30).stdout.split("\n") for _ in range(40)]
    waits = [int(r[0].split()[1]) for r in runs]
    assert all(r[0].startswith("slept ") and r[1] == "ran" for r in runs), runs[:2]
    assert all(0 <= w <= tunnel.RECONNECT_SPREAD_S for w in waits) and len(set(waits)) > 1, waits
    # The start an install or restart means skips it once, using up the marker.
    tunnel.skip_next_wait(svc)
    assert marker.is_file()
    r = subprocess.run(tunnel.spread(["echo", "ran"], marker), env=path, capture_output=True, text=True, timeout=30)
    assert r.stdout == "ran\n" and not marker.exists()
    # The env-agent form inside it still execs ssh with the same argv (the agent under its own name).
    ssh = bridge.ssh_argv(cfg, "box-1", "auto", ssh=str(fake / "ssh"))
    assert ssh[:2] == ["/bin/sh", "-c"]
    marker.touch()
    r = subprocess.run(tunnel.spread(ssh, marker), env={**path, "SSH_AUTH_SOCK": "/agents/env.sock"},
                       capture_output=True, text=True, timeout=30)
    assert r.stdout.split("\0")[:-1] == ["/agents/env.sock", *ssh[5:]], r
    # bridge start (Linux): enabled, then one restart (which also starts a stopped unit) with the marker there.
    seen = []
    monkeypatch.setattr(bridge, "_run", lambda argv, *a, **k: seen.append((argv[2], marker.is_file()))
                        or subprocess.CompletedProcess(argv, 0, "", ""))
    assert not marker.exists()
    bridge.start("box-1", "auto", svc, "linux")
    assert seen[1:] == [("enable", False), ("restart", True)], seen
    # A kept tunnel: installed with the wait, its first start (and a restart) prompt, its ssh on its own connection.
    calls = []
    monkeypatch.setattr(tunnel, "_run", lambda *argv: calls.append((argv, tunnel.tunnel_marker("demo").is_file()))
                        or subprocess.CompletedProcess(argv, 0, "", ""))
    assert tunnel.keep("demo", "box", 18700, lambda port: port, "linux")[0] == 18800
    have = tunnel.installed("demo", "linux")
    assert have["spread"] and have["own"] and have["host"] == "box" and have["local"] == 18800
    assert any(a[2:4] == ("enable", "--now") and m for a, m in calls), calls
    tunnel.tunnel_marker("demo").unlink()
    calls.clear()
    assert tunnel.restart("demo", "linux") == "restarted" and calls[-1][1]
    # An older kept tunnel without the wait is re-installed by setup/upgrade on its port; a current one is left.
    old = tunnel.ssh_argv("box", 18900, 18750)
    tunnel.service_file("older", "linux").write_text(tunnel.systemd_unit("older", old))
    assert tunnel.refresh_kept("linux") == ["tunnel older: start delay added; replaced the kept tunnel "
                                       f"(systemd user unit, {tunnel.service_file('older', 'linux')})"]
    assert tunnel.installed("older", "linux")["spread"] and tunnel.installed("older", "linux")["local"] == 18900
    assert tunnel.refresh_kept("linux") == []
    plist = tunnel.launchd_plist("older", tunnel.spread(old, tunnel.tunnel_marker("older")))
    assert plistlib.loads(plistlib.dumps(plist))["ProgramArguments"][-1] == "box"   # the host stays argv's last word
    tunnel.unkeep("older", "linux")
    assert not tunnel.tunnel_marker("older").exists()

def test_bridge_relay_carries_one_connection_and_exits_with_its_session(monkeypatch, request):
    import shutil
    import socket
    import tempfile
    import threading
    from ttp import bridge_relay
    # a short folder: macOS's per-user temp path makes a unix socket path too long
    tmp_path = Path(tempfile.mkdtemp(prefix="br", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(tmp_path, ignore_errors=True))
    assert bridge_relay.parse_spec("h=10.0.0.2%vpn0") == ("h", "10.0.0.2", "vpn0")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def echo():
        c, _ = srv.accept()
        c.sendall(b"SSH-2.0-test\r\n" + c.recv(10))
        c.close()

    threading.Thread(target=echo, daemon=True).start()
    seen = []
    monkeypatch.setattr(bridge_relay, "bound_connect",
                        lambda host, iface: seen.append((host, iface)) or socket.create_connection(("127.0.0.1", port)))
    path = str(tmp_path / "h.sock")
    bridge_relay.listen(path, "10.0.0.2", "vpn0")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    c = socket.socket(socket.AF_UNIX)
    c.connect(path)
    c.sendall(b"ping")
    data = b""
    while len(data) < 18:
        chunk = c.recv(64)
        if not chunk:
            break
        data += chunk
    assert data == b"SSH-2.0-test\r\nping" and seen == [("10.0.0.2", "vpn0")]
    rc = bridge_relay.main([str(tmp_path / "d"), "x=10.0.0.3%vpn0", "--", sys.executable, "-c", "raise SystemExit(7)"])
    assert rc == 7 and not (tmp_path / "d" / "x.sock").exists()



def _relay_dir(request):
    import shutil
    import tempfile
    # a short folder: macOS's per-user temp path makes a unix socket path too long
    d = Path(tempfile.mkdtemp(prefix="br", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(d, ignore_errors=True))
    return d


def _relay_echo_upstream(monkeypatch, bridge_relay):
    """bound_connect reaches a local echo server instead of a host; returns the upstream sockets it accepted."""
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    ups = []

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            ups.append(c)

            def echo(c=c):
                try:
                    while True:
                        b = c.recv(64)
                        if not b:
                            return
                        c.sendall(b)
                except OSError:
                    pass

            threading.Thread(target=echo, daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setattr(bridge_relay, "bound_connect",
                        lambda host, iface: socket.create_connection(("127.0.0.1", port)))
    return srv, ups


def _relay_roundtrip(path, payload=b"ping"):
    import socket
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(5)
    c.connect(path)
    c.sendall(payload)
    data = b""
    while len(data) < len(payload):
        chunk = c.recv(64)
        if not chunk:
            break
        data += chunk
    c.close()
    return data


def test_bridge_relay_keeps_accepting_after_running_out_of_descriptors(monkeypatch, request):
    # cells: emfile-retry
    import errno
    import socket
    from ttp import bridge_relay
    d = _relay_dir(request)
    _relay_echo_upstream(monkeypatch, bridge_relay)
    monkeypatch.setattr(bridge_relay, "ACCEPT_BACKOFF_MAX_S", 0.05)
    fails = {"n": 3}

    class Starved(socket.socket):
        def accept(self):
            if fails["n"]:
                fails["n"] -= 1
                raise OSError(errno.EMFILE, "Too many open files")
            return super().accept()

    path = str(d / "h.sock")
    lst = bridge_relay.listen(path, "10.0.0.2", "vpn0", sock=Starved)
    request.addfinalizer(lambda: lst.close(unlink=True))
    assert _relay_roundtrip(path, b"one") == b"one"
    assert fails["n"] == 0 and lst.thread.is_alive() and lst.problem() == ""
    assert _relay_roundtrip(path, b"two") == b"two"   # a later connection is carried too


def test_bridge_relay_closes_both_sockets_when_the_upstream_goes_silent(monkeypatch, request):
    # cells: bounded-close
    import socket
    import threading
    import time
    from ttp import bridge_relay
    monkeypatch.setattr(bridge_relay, "DRAIN_S", 0.3)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    request.addfinalizer(srv.close)
    port = srv.getsockname()[1]
    made = []

    def connect(host, iface):
        made.append(socket.create_connection(("127.0.0.1", port)))
        return made[-1]

    monkeypatch.setattr(bridge_relay, "bound_connect", connect)
    a, b = socket.socketpair()
    t = threading.Thread(target=bridge_relay._handle, args=(b, "10.0.0.2", "vpn0"), daemon=True)
    t.start()
    up, _ = srv.accept()   # a peer that takes the connection and never answers or closes
    request.addfinalizer(up.close)
    a.sendall(b"hello")
    a.close()   # the client is done; the upstream stays silent
    t.join(5)
    assert not t.is_alive()   # without the bound it waits on the silent peer forever
    assert b.fileno() == -1 and made[0].fileno() == -1   # both relay descriptors closed
    up.settimeout(2)
    got = b""
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2:
        try:
            chunk = up.recv(64)
        except OSError:
            break
        if not chunk:
            break
        got += chunk
    assert got == b"hello"


def test_bridge_relay_upstream_gets_keepalive():
    # cells: keepalive
    import socket
    from ttp import bridge_relay
    s = socket.socket()
    try:
        bridge_relay.keepalive(s)
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        if hasattr(socket, "TCP_KEEPIDLE"):
            assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == bridge_relay.KEEPALIVE[0]
        if hasattr(socket, "TCP_KEEPCNT"):
            assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == bridge_relay.KEEPALIVE[2]
    finally:
        s.close()


def test_bridge_relay_supervisor_relistens_a_lost_listener(monkeypatch, request):
    # cells: relisten-file-gone, relisten-file-replaced, relisten-accept-ended
    import socket
    from ttp import bridge_relay
    d = _relay_dir(request)
    _relay_echo_upstream(monkeypatch, bridge_relay)
    path = str(d / "h.sock")
    lst = bridge_relay.listen(path, "10.0.0.2", "vpn0")
    request.addfinalizer(lambda: lst.close(unlink=True))
    fails = {}
    assert bridge_relay.supervise_once([lst], fails) and lst.problem() == ""
    os.unlink(path)   # the socket file is gone: forwards get "No such file"
    assert lst.problem() == "its socket file is gone"
    assert bridge_relay.supervise_once([lst], fails) and lst.problem() == ""
    assert _relay_roundtrip(path) == b"ping"
    os.unlink(path)   # replaced by another socket: forwards reach no relay
    other = socket.socket(socket.AF_UNIX)
    other.bind(path)
    other.close()
    assert lst.problem() == "its socket file was replaced"
    assert bridge_relay.supervise_once([lst], fails) and lst.problem() == ""
    assert _relay_roundtrip(path) == b"ping"
    old = lst.thread
    lst.srv.shutdown(socket.SHUT_RDWR)   # the accept loop ends on an error it does not retry
    old.join(5)
    assert not old.is_alive() and lst.problem() in ("its accept loop ended", "its socket is closed")
    assert bridge_relay.supervise_once([lst], fails) and lst.problem() == "" and lst.thread is not old
    assert _relay_roundtrip(path) == b"ping" and fails == {}


def test_bridge_relay_exits_after_two_failed_relistens(monkeypatch, request):
    # cells: relisten-gives-up, relay-lost-exit
    import socket
    import threading
    from ttp import bridge_relay
    d = _relay_dir(request)
    path = str(d / "h.sock")
    lst = bridge_relay.listen(path, "10.0.0.2", "vpn0")
    request.addfinalizer(lambda: lst.close(unlink=True))

    class NoBind(socket.socket):
        def bind(self, addr):
            raise OSError(98, "Address in use")

    lst.sock = NoBind
    os.unlink(path)
    fails = {}
    assert bridge_relay.supervise_once([lst], fails) and fails == {path: 1}
    assert not bridge_relay.supervise_once([lst], fails)   # the second failure in a row gives up
    lost = []
    lst2 = bridge_relay.Listener(str(d / "g.sock"), "10.0.0.2", "vpn0", sock=NoBind)
    monkeypatch.setattr(bridge_relay, "SUPERVISE_S", 0.01)
    stop = threading.Event()
    bridge_relay.supervise([lst2], stop, lambda: lost.append(1))
    assert lost == [1]
    # main: a listener that cannot be restored ends the session and exits RELAY_LOST for a restart
    real = bridge_relay.listen

    def listen(path, host, iface):
        x = real(path, host, iface)
        x.sock = NoBind
        os.unlink(path)
        return x

    monkeypatch.setattr(bridge_relay, "listen", listen)
    rc = bridge_relay.main([str(d / "m"), "x=10.0.0.3%vpn0", "--", sys.executable, "-c", "import time; time.sleep(30)"])
    assert rc == bridge_relay.RELAY_LOST and not (d / "m" / "x.sock").exists()


def test_bridge_relay_caps_its_carried_connections_and_takes_new_ones_once_they_end(monkeypatch, request, capfd):
    # cells: cap-refuse, cap-reopen, cap-relay-wide, cap-one-log-per-burst
    import socket
    import threading
    import time
    from ttp import bridge_relay
    d = _relay_dir(request)
    monkeypatch.setattr(bridge_relay, "MAX_CARRIED", 2)
    monkeypatch.setattr(bridge_relay, "ALL", bridge_relay.Gate(100))
    release = threading.Event()

    def hang(host, iface):   # an upstream that holds each carried connection until released
        release.wait(10)
        raise OSError("upstream down")

    monkeypatch.setattr(bridge_relay, "bound_connect", hang)
    path = str(d / "h.sock")
    lst = bridge_relay.listen(path, "10.0.0.2", "vpn0")
    request.addfinalizer(lambda: lst.close(unlink=True))

    def connect():
        c = socket.socket(socket.AF_UNIX)
        c.settimeout(5)
        c.connect(path)
        return c

    held = [connect(), connect()]
    t0 = time.monotonic()
    while lst.gate.n < 2 and time.monotonic() - t0 < 5:
        time.sleep(0.01)
    assert lst.gate.n == 2
    over = [connect() for _ in range(3)]
    for c in over:   # over the cap: closed at once, not left waiting for an upstream
        assert c.recv(1) == b""
    assert lst.gate.n == 2 and bridge_relay.ALL.n == 2 and lst.thread.is_alive()
    assert capfd.readouterr().err.count("closing new ones until some end") == 1
    release.set()
    for c in held:
        assert c.recv(1) == b""
        c.close()
    t0 = time.monotonic()
    while lst.gate.n and time.monotonic() - t0 < 5:
        time.sleep(0.01)
    assert lst.gate.n == 0 and bridge_relay.ALL.n == 0
    _relay_echo_upstream(monkeypatch, bridge_relay)
    monkeypatch.setattr(bridge_relay, "DRAIN_S", 0.2)
    assert _relay_roundtrip(path, b"again") == b"again"   # carried again once the earlier ones ended
    t0 = time.monotonic()
    while lst.gate.n and time.monotonic() - t0 < 5:
        time.sleep(0.01)
    monkeypatch.setattr(bridge_relay, "ALL", bridge_relay.Gate(0))   # the relay-wide cap refuses too
    c = connect()
    assert c.recv(1) == b"" and lst.gate.n == 0
    c.close()
    assert bridge_relay.fit_carried(256) == (256 - bridge_relay.FD_RESERVE) // 2
    assert bridge_relay.fit_carried(1 << 20) == bridge_relay.MAX_CARRIED_ALL
    assert bridge_relay.fit_carried(None) == bridge_relay.MAX_CARRIED_ALL   # unknown limit: left as it was


def test_bridge_relay_upstream_connect_has_one_deadline_across_every_address(monkeypatch):
    # cells: deadline-all-addresses, deadline-lookup, deadline-handle-closes
    import socket
    import threading
    import time
    from ttp import bridge_relay
    tries = []

    class Hangs:   # a connect into a dead route: it waits out whatever timeout it is given
        def __init__(self, *a):
            self.timeout = None

        def setsockopt(self, *a):
            pass

        def settimeout(self, t):
            self.timeout = t

        def connect(self, addr):
            tries.append(self.timeout)
            time.sleep(self.timeout)
            raise socket.timeout("timed out")

        def close(self):
            pass

    addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"10.0.0.{i}", 22)) for i in range(2, 6)]
    monkeypatch.setattr(bridge_relay.socket, "getaddrinfo", lambda *a, **k: addrs)
    monkeypatch.setattr(bridge_relay.socket, "socket", Hangs)
    monkeypatch.setattr(bridge_relay.socket, "if_nametoindex", lambda n: 1, raising=False)
    monkeypatch.setattr(bridge_relay, "CONNECT_DEADLINE_S", 0.4)
    t0 = time.monotonic()
    with pytest.raises(OSError):
        bridge_relay.bound_connect("box", "vpn0")
    took = time.monotonic() - t0
    assert took < 0.7 and len(tries) < len(addrs), (took, tries)   # one deadline, not 0.4 s per address

    stuck = threading.Event()
    monkeypatch.setattr(bridge_relay.socket, "getaddrinfo", lambda *a, **k: (stuck.wait(5), addrs)[1])
    t0 = time.monotonic()
    with pytest.raises(OSError, match="lookup timed out"):
        bridge_relay.bound_connect("box", "vpn0")
    assert time.monotonic() - t0 < 0.7   # a lookup stuck on a dead network counts against it too
    stuck.set()

    closed = []

    class Client:
        def shutdown(self, how):
            pass

        def close(self):
            closed.append(time.monotonic())

    t0 = time.monotonic()
    bridge_relay._handle(Client(), "box", "vpn0")
    assert closed and closed[0] - t0 < 0.7   # the client side is closed within the deadline


def test_bridge_relay_raises_its_open_file_limit_before_the_session_starts(monkeypatch, request):
    # cells: nofile-before-ssh
    import sys
    from ttp import bridge_relay
    d = _relay_dir(request)
    order = []
    monkeypatch.setattr(bridge_relay, "raise_nofile", lambda *a, **k: order.append("raise") or 1024)

    def run(ssh, tag, dd, state, link=""):
        order.append("ssh")
        return 0

    monkeypatch.setattr(bridge_relay, "run", run)
    monkeypatch.setattr(bridge_relay, "ALL", bridge_relay.Gate(bridge_relay.MAX_CARRIED_ALL))
    assert bridge_relay.main([str(d / "m"), "--", sys.executable, "-c", "pass"]) == 0
    assert order == ["raise", "ssh"] and bridge_relay.ALL.cap == (1024 - bridge_relay.FD_RESERVE) // 2
    if sys.platform == "win32":
        return
    import resource
    import subprocess
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = soft - 1 if soft > 64 else soft
    monkeypatch.undo()
    resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    request.addfinalizer(lambda: resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard)))
    got = bridge_relay.raise_nofile(want=soft)
    out = subprocess.run([sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])"],
                         capture_output=True, text=True, check=True).stdout
    assert int(out) == got == soft   # a child spawned after the raise inherits the raised limit


def test_bridge_relay_raises_its_open_file_limit_within_the_hard_limit():
    # cells: nofile-raise, nofile-hard-lower, nofile-unlimited, nofile-refused
    from ttp import bridge_relay

    class RL:
        RLIMIT_NOFILE, RLIM_INFINITY = 7, -1

        def __init__(self, soft, hard, refuse=False):
            self.lim, self.refuse, self.set = (soft, hard), refuse, []

        def getrlimit(self, which):
            return self.lim

        def setrlimit(self, which, lim):
            if self.refuse:
                raise ValueError("not allowed")
            self.set.append(lim)

    rl = RL(256, 1 << 20)
    assert bridge_relay.raise_nofile(rl=rl) == 4096 and rl.set == [(4096, 1 << 20)]
    rl = RL(256, 1024)
    assert bridge_relay.raise_nofile(rl=rl) == 1024 and rl.set == [(1024, 1024)]
    rl = RL(1024, 1024)   # the hard limit is not above the soft one: nothing to raise
    assert bridge_relay.raise_nofile(rl=rl) == 1024 and rl.set == []
    rl = RL(256, RL.RLIM_INFINITY)
    assert bridge_relay.raise_nofile(rl=rl) == 4096 and rl.set == [(4096, RL.RLIM_INFINITY)]
    rl = RL(8192, 1 << 20)   # already higher: left alone
    assert bridge_relay.raise_nofile(rl=rl) == 8192 and rl.set == []
    assert bridge_relay.raise_nofile(rl=RL(256, 1024, refuse=True)) == 256   # refused: never fails

# --- offline mode: short-lived certificates (`ttp bridge cert`, bridge_cert.py) ------------------
# A stub ssh-keygen and ssh on PATH: no key is made, nothing is signed for real, no host is reached.
# The stub "box" is a folder used as HOME by `sh -c <script>`; a stub certificate carries its
# fields as base64 JSON, which the stub's -L prints the way ssh-keygen does.

_CERT_STUB_KEYGEN = r'''
import base64, hashlib, json, os, sys, time
a = sys.argv[1:]
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": a, "auth_sock": os.environ.get("SSH_AUTH_SOCK", "")}) + "\n")
def fp(line):
    f = line.split()
    if f[0].endswith("-cert-v01@openssh.com"):
        return json.loads(base64.b64decode(f[1]))["key"]
    return "SHA256:" + hashlib.sha256(f[1].encode()).hexdigest()[:24]
def arg(flag):
    return a[a.index(flag) + 1]
if "-t" in a:   # make a key: two placeholder lines, no key material
    path = arg("-f")
    open(path, "w").write("placeholder box key\n")
    open(path + ".pub", "w").write("ssh-ed25519 Qm94S2V5" + base64.b16encode(os.urandom(4)).decode() + " box\n")
elif "-l" in a:
    print("256 " + fp(open(arg("-f")).read()) + " c (ED25519)")
elif "-L" in a:
    c = json.loads(base64.b64decode(open(arg("-f")).read().split()[1]))
    ts = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))
    print(arg("-f") + ":")
    print("        Type: ssh-ed25519-cert-v01@openssh.com " + ("user" if c.get("user", True) else "host") + " certificate")
    print("        Public key: ED25519-CERT " + c["key"])
    print("        Signing CA: ED25519 " + c["ca"] + " (using ssh-ed25519)")
    print('        Key ID: "' + c["id"] + '"')
    print("        Serial: 0")
    print("        Valid: from " + ts(c["from"]) + " to " + ts(c["to"]))
    print("        Principals: " + ("" if c["principals"] else "(none)"))
    for p in c["principals"]:
        print("                " + p)
    print("        Critical Options: " + ("" if c["options"] else "(none)"))
    for k, v in c["options"].items():
        print("                " + k + " " + v)
    print("        Extensions: " + ("" if c["ext"] else "(none)"))
    for e in c["ext"]:
        print("                " + e)
elif "-s" in a:   # sign: the -V window and -O options as asked (STUB_TO_MIN overrides the end)
    now = int(time.time())
    lo, hi = arg("-V").split(":")
    to = now + int(os.environ.get("STUB_TO_MIN") or hi.strip("+m")) * 60
    opts = [a[i + 1] for i, w in enumerate(a) if w == "-O"]
    c = {"key": fp(open(a[-1]).read()), "ca": fp(open(arg("-s")).read()), "id": arg("-I"),
         "from": now + int(lo.strip("m")) * 60, "to": to, "principals": arg("-n").split(","),
         "options": dict(o.split("=", 1) for o in opts if "=" in o),
         "ext": [o for o in opts if o.startswith("permit-")], "user": "-U" in a}
    out = a[-1][:-4] + "-cert.pub"
    open(out, "w").write("ssh-ed25519-cert-v01@openssh.com " + base64.b64encode(json.dumps(c).encode()).decode() + " c\n")
else:
    sys.exit(9)
'''

_CERT_STUB_SSH = r'''
import os, sys
os.environ["HOME"] = os.environ["STUB_BOX_HOME"]
os.execvp("sh", ["sh", "-c", sys.argv[-1]])
'''


def _cert_env(tmp_path, monkeypatch, **cert):
    """A bridge config with a cert section, stub ssh/ssh-keygen on PATH and an empty stub box."""
    import json
    import sys
    from ttp import bridge
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, body in (("ssh-keygen", _CERT_STUB_KEYGEN), ("ssh", _CERT_STUB_SSH)):
        (bin_ / name).write_text(f"#!{sys.executable}\n{body}")
        (bin_ / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    box_home = tmp_path / "box"
    box_home.mkdir()
    monkeypatch.setenv("STUB_BOX_HOME", str(box_home))
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "keygen.log"))
    monkeypatch.delenv("STUB_TO_MIN", raising=False)
    (tmp_path / "ca.pub").write_text("ssh-ed25519 Q0FrZXlwbGFjZWhvbGRlcg ca\n")
    c = {"signer": {"kind": "agent-ca", "ca_public_key": str(tmp_path / "ca.pub"), "agent": str(tmp_path / "ca.sock")},
         "principals": ["builder", "git"], "boxes": {"box-1": {"source_address": "10.0.0.9", "hosts": [
             {"name": "build-a", "aliases": ["ba"], "target": "build-a.example.com", "user": "builder"}]}}}
    c.update(cert)
    raw = {"boxes": {"box-1": {"agents": {"auto": {"agent": "env", "forward": [
        {"name": "build-a", "aliases": ["ba"], "target": "build-a.example.com", "port": 2301, "user": "builder"}]}}}},
           "cert": c}
    from ttp import bridge
    return bridge.parse(raw), box_home


def _cert_log(tmp_path):
    import json
    return [json.loads(x) for x in (tmp_path / "keygen.log").read_text().splitlines()]


def _cert_dir(box):
    return box / ".config" / "tt-project" / "bridge-cert"


def test_bridge_cert_signs_the_box_key_through_the_agent_ca_and_puts_it_back(tmp_path, monkeypatch):
    # cells: cert-sign-stub
    from ttp import bridge_cert
    cfg, box = _cert_env(tmp_path, monkeypatch)
    lines, rc = bridge_cert.renew(cfg, ["box-1"])
    assert rc == 1 and "--make-key" in lines[0]                      # no box key: says how, makes none
    lines, rc = bridge_cert.renew(cfg, ["box-1"], make=True)
    assert rc == 0 and "renewed, valid until" in lines[0], lines
    d = _cert_dir(box)
    assert {p.name for p in d.iterdir()} == {"id_ed25519", "id_ed25519.pub", "id_ed25519-cert.pub", "valid"}
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    sign = [x for x in _cert_log(tmp_path) if "-s" in x["argv"]]
    assert len(sign) == 1
    argv = sign[0]["argv"]
    assert argv[argv.index("-s") + 1] == str(tmp_path / "ca.pub") and "-U" in argv   # the CA's public half only
    assert sign[0]["auth_sock"] == str(tmp_path / "ca.sock")
    opts = [argv[i + 1] for i, w in enumerate(argv) if w == "-O"]
    assert opts == ["clear", "source-address=10.0.0.9", "permit-pty"]
    assert argv[argv.index("-n") + 1] == "builder,git" and argv[argv.index("-V") + 1] == "-5m:+4315m"
    i = bridge_cert.inspect((d / "id_ed25519-cert.pub").read_text())
    assert i["user"] and i["options"] == {"source-address": "10.0.0.9"} and i["extensions"] == ["permit-pty"]
    (tmp_path / "x").mkdir()
    cfg2, _ = _cert_env(tmp_path / "x", monkeypatch, pty=False)
    assert bridge_cert.renew(cfg2, ["box-1"], make=True)[1] == 0
    assert "permit-pty" not in [x for x in _cert_log(tmp_path / "x") if "-s" in x["argv"]][0]["argv"]


def test_bridge_cert_renews_only_below_half_its_life(tmp_path, monkeypatch):
    # cells: cert-renew-half-life
    from ttp import bridge_cert
    cfg, box = _cert_env(tmp_path, monkeypatch)
    monkeypatch.setenv("STUB_TO_MIN", str(40 * 60))                 # 40 h of 72 left: more than half
    assert bridge_cert.renew(cfg, ["box-1"], make=True)[1] == 0
    lines, rc = bridge_cert.renew(cfg, ["box-1"])
    assert rc == 0 and "current" in lines[0]
    monkeypatch.setenv("STUB_TO_MIN", str(30 * 60))
    assert "renewed" in bridge_cert.renew(cfg, ["box-1"], force=True)[0][0]
    monkeypatch.delenv("STUB_TO_MIN")                                # 30 h left: below half, signs again
    lines, rc = bridge_cert.renew(cfg, ["box-1"])
    assert rc == 0 and "renewed" in lines[0]
    assert "current" in bridge_cert.renew(cfg, ["box-1"])[0][0]
    assert len([x for x in _cert_log(tmp_path) if "-s" in x["argv"]]) == 3


def test_bridge_cert_expired_fails_closed(tmp_path, monkeypatch):
    # cells: cert-expired-fails-closed
    import subprocess
    import time
    import pytest
    from ttp import bridge, bridge_cert
    cfg, box = _cert_env(tmp_path, monkeypatch)
    v = tmp_path / "valid"
    v.write_text(bridge_cert.valid_script(int(time.time()) - 5, "box-1"))
    r = subprocess.run(["sh", str(v)], capture_output=True, text=True)
    assert r.returncode == 1 and "certificate of box-1 ended" in r.stderr and "renews it" in r.stderr
    r = subprocess.run(["sh", str(v), "quiet"], capture_output=True, text=True)
    assert r.returncode == 1 and r.stderr == ""
    v.write_text(bridge_cert.valid_script(int(time.time()) + 600, "box-1"))
    assert subprocess.run(["sh", str(v)]).returncode == 0
    # the block: direct with the box's key only while the link is down and the certificate valid;
    # after that every login method is off for the host, so no other key is tried
    block = bridge.render_block(cfg, "box-1")
    k = "~/.config/tt-project/bridge-cert"
    assert (f'Match originalhost build-a,ba !exec "{bridge_cert.match_quote(bridge_cert.agent_up(".ssh/tt-agent/auto.sock"))}" '
            f'exec "sh {k}/valid"') in block
    assert f'!exec "sh {k}/valid quiet"' in block and "  PubkeyAuthentication no" in block
    assert f"  IdentityFile {k}/id_ed25519\n  CertificateFile {k}/id_ed25519-cert.pub" in block
    assert block.index("Match originalhost") < block.index("Host build-a")   # first match wins
    assert "%" not in block.split("Host build-a")[0].replace("%%", "")       # ssh would expand a lone %
    for bad in ("  IdentityFile ~/keys/other", "  CertificateFile ~/keys/other-cert.pub"):
        with pytest.raises(bridge.BadConfig):
            bridge.check_block(block.replace("Match all", bad + "\nMatch all"), bridge_cert.block_lines(cfg, "box-1")[1])
    # a signer returning an already ended certificate: refused, nothing installed
    monkeypatch.setenv("STUB_TO_MIN", "-10")
    lines, rc = bridge_cert.renew(cfg, ["box-1"], make=True)
    assert rc == 1 and "refusing" in lines[0]
    assert not (_cert_dir(box) / "id_ed25519-cert.pub").exists()


def test_bridge_cert_status_reports_until_source_and_expiry(tmp_path, monkeypatch):
    # cells: cert-status
    import base64
    import json
    import time
    from ttp import bridge_cert
    cfg, box = _cert_env(tmp_path, monkeypatch)
    lines, rc = bridge_cert.status(cfg, ["box-1"])
    assert rc == 1 and "no key" in lines[0]
    bridge_cert.renew(cfg, ["box-1"], make=True)
    lines, rc = bridge_cert.status(cfg, ["box-1"])
    assert rc == 0 and lines[0].startswith("box-1: OK valid until ") and "source 10.0.0.9" in lines[0]
    assert "principals builder,git" in lines[0] and "signer agent-ca" in lines[0]
    cert = _cert_dir(box) / "id_ed25519-cert.pub"
    f, b64, rest = cert.read_text().split()
    c = json.loads(base64.b64decode(b64))
    c["to"] = int(time.time()) - 60
    cert.write_text(f"{f} {base64.b64encode(json.dumps(c).encode()).decode()} {rest}\n")
    lines, rc = bridge_cert.status(cfg, ["box-1"])
    assert rc == 1 and lines[0].startswith("box-1: EXPIRED")
    assert bridge_cert.remove(cfg, ["box-1"]) == ["box-1: key and certificate removed"]
    assert not _cert_dir(box).exists()


def test_bridge_cert_trust_prints_one_restricted_line_and_edits_nothing(tmp_path, monkeypatch, capsys):
    # cells: trust-print
    import json
    import pytest
    from ttp import bridge_cert, cli
    cfg, box = _cert_env(tmp_path, monkeypatch)
    line = bridge_cert.trust_line(cfg)
    assert line == ('cert-authority,principals="builder,git",no-agent-forwarding,no-port-forwarding,'
                    'no-X11-forwarding,no-user-rc ssh-ed25519 Q0FrZXlwbGFjZWhvbGRlcg ttp-bridge-ca')
    for text in ("ssh-ed25519 AAAA a\nssh-ed25519 BBBB b\n", "not a public key\n"):
        (tmp_path / "ca.pub").write_text(text)
        with pytest.raises(RuntimeError):
            bridge_cert.trust_line(cfg)
    conf = tmp_path / "bridge.json"
    conf.write_text(json.dumps({"boxes": {"box-1": {"agents": {"a": {"agent": "env", "direct": [{"name": "h"}]}}}}}))
    with pytest.raises(SystemExit):
        cli.main(["bridge", "cert", "trust", "--print", "--config", str(conf)])
    assert "certificate mode is off" in capsys.readouterr().err


def test_bridge_options_may_follow_or_sit_between_the_words(tmp_path, monkeypatch):
    # cells: options-after-words
    from ttp import cli
    monkeypatch.setenv("HOME", str(tmp_path))
    seen = []
    monkeypatch.setattr(cli, "cmd_bridge", lambda a: seen.append((a.action, a.boxes, a.make_key, a.force,
                                                                  a.config, a.remove)))
    for argv in (["bridge", "cert", "renew", "--make-key", "box-2"], ["bridge", "--make-key", "cert", "renew", "box-2"],
                 ["bridge", "cert", "renew", "box-2", "--make-key"]):
        cli.main(argv)
    cli.main(["bridge", "cert", "--force", "renew", "--config", "c.json", "box-1", "box-2"])
    cli.main(["bridge", "stop", "--remove", "box-1", "--config=d.json"])
    assert seen == [("cert", ["renew", "box-2"], True, False, None, False)] * 3 + [
        ("cert", ["renew", "box-1", "box-2"], False, True, "c.json", False),
        ("stop", ["box-1"], False, False, "d.json", True)]
    assert cli._bridge_argv(["web", "x", "--keep"]) == ["web", "x", "--keep"]


def test_bridge_cert_command_signer_gets_the_request_and_overbroad_certs_are_refused(tmp_path, monkeypatch):
    # cells: signer-command
    import json
    import sys
    from ttp import bridge_cert
    signer = tmp_path / "signer.py"
    signer.write_text(
        "import json, os, subprocess, sys, tempfile\n"
        "pub = sys.stdin.read()\n"
        f"open({str(tmp_path / 'req.json')!r}, 'w').write(json.dumps(dict({{k: v for k, v in os.environ.items() "
        "if k.startswith('TTP_CERT_')}, pub=pub)))\n"
        "d = tempfile.mkdtemp(); open(d + '/k.pub', 'w').write(pub)\n"
        "n = os.environ.get('STUB_PRINCIPALS') or os.environ['TTP_CERT_PRINCIPALS']\n"
        "o = ['-O', 'source-address=' + os.environ['TTP_CERT_SOURCE_ADDRESS']]\n"
        "o += ['-O', os.environ['STUB_EXTRA']] if os.environ.get('STUB_EXTRA') else []\n"
        f"subprocess.run(['ssh-keygen', '-s', {str(tmp_path / 'ca.pub')!r}, '-U', '-I', os.environ['TTP_CERT_KEY_ID'], "
        "'-n', n, '-V', '-5m:+' + os.environ['TTP_CERT_VALIDITY_MINUTES'] + 'm'] + o + [d + '/k.pub'], check=True)\n"
        "sys.stdout.write(open(d + '/k-cert.pub').read())\n")
    cfg, box = _cert_env(tmp_path, monkeypatch, signer={"kind": "command", "ca_public_key": str(tmp_path / "ca.pub"),
                                                        "command": [sys.executable, str(signer)]}, validity_hours=24)
    lines, rc = bridge_cert.renew(cfg, ["box-1"], make=True)
    assert rc == 0 and "renewed" in lines[0], lines
    req = json.loads((tmp_path / "req.json").read_text())
    assert req["pub"].startswith("ssh-ed25519 ") and req["TTP_CERT_PRINCIPALS"] == "builder,git"
    assert req["TTP_CERT_VALIDITY_MINUTES"] == "1440" and req["TTP_CERT_SOURCE_ADDRESS"] == "10.0.0.9"
    assert req["TTP_CERT_KEY_ID"].startswith("ttp-bridge:box-1:") and req["TTP_CERT_PERMIT_PTY"] == "1"
    cert = _cert_dir(box) / "id_ed25519-cert.pub"
    good = cert.read_text()
    for env, value in (("STUB_PRINCIPALS", "root"), ("STUB_EXTRA", "permit-port-forwarding")):
        monkeypatch.setenv(env, value)
        lines, rc = bridge_cert.renew(cfg, ["box-1"], force=True)
        assert rc == 1 and "refusing" in lines[0], lines
        assert cert.read_text() == good                               # nothing broader is installed
        monkeypatch.delenv(env)


def test_bridge_cert_config_is_checked_and_off_by_default(tmp_path, monkeypatch):
    import pytest
    from ttp import bridge, bridge_cert
    cfg = _bridge_cfg()
    assert "cert" not in cfg and bridge_cert.block_lines(cfg, "box-1") == ([], set())
    assert "IdentityFile" not in bridge.render_block(cfg, "box-1")
    good, _ = _cert_env(tmp_path, monkeypatch)
    assert good["cert"]["validity_hours"] == 72 and good["cert"]["pty"] is True
    base = {"signer": {"kind": "agent-ca", "ca_public_key": "~/ca.pub"}, "principals": ["u"],
            "boxes": {"box-1": {"source_address": "10.0.0.9", "hosts": [{"name": "h"}]}}}
    raw = {"boxes": {"box-1": {"agents": {"a": {"agent": "env", "direct": [{"name": "h"}]}}}}}
    bad = [{"signer": {"kind": "agent-ca", "ca_public_key": "~/ca"}},          # not a public key file
           {"signer": {"kind": "command", "ca_public_key": "~/ca.pub"}},       # no command
           {"validity_hours": 24 * 30}, {"principals": []}, {"key_dir": "/tmp/k"},
           {"boxes": {"other": {"source_address": "10.0.0.9", "hosts": [{"name": "h"}]}}},
           {"boxes": {"box-1": {"source_address": "any", "hosts": [{"name": "h"}]}}},
           {"boxes": {"box-1": {"source_address": "10.0.0.9", "hosts": []}}}]
    for b in bad:
        with pytest.raises(bridge.BadConfig):
            bridge.parse({**raw, "cert": {**base, **b}})


def test_bridge_writes_to_a_box_only_when_the_content_changed(tmp_path, monkeypatch):
    # other projects watch the box's ssh folder: a re-run with nothing changed must write nothing there
    import subprocess
    from ttp import bridge
    cfg, box = _cert_env(tmp_path, monkeypatch)
    scratch = tmp_path / "boxtmp"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    sshd, conf = box / ".ssh", box / ".ssh" / "config"
    assert bridge.write_block(cfg, "box-1") == "block written (new config)"
    first = conf.read_text()

    def frozen():
        os.utime(conf, (1, 1))
        os.utime(sshd, (1, 1))
        return sorted(p.name for p in sshd.iterdir())

    names = frozen()
    assert bridge.write_block(cfg, "box-1") == "block already current"
    r = bridge._box("box-1", bridge.replace_config_script("x"), inp=first)   # the box compares too
    assert r.returncode == 0 and r.stdout.strip() == "unchanged"
    assert sorted(p.name for p in sshd.iterdir()) == names and conf.read_text() == first
    assert conf.stat().st_mtime == 1 and sshd.stat().st_mtime == 1 and not list(scratch.iterdir())
    conf.write_text("Host mine\n  User me\n\n" + first)
    assert bridge.write_block(cfg, "box-1").startswith("block written (backup")
    assert conf.read_text().startswith(bridge.BEGIN) and "Host mine" in conf.read_text() and any(".bak-ttp-bridge-" in n for n in os.listdir(sshd))
    assert not [n for n in os.listdir(sshd) if n.endswith(".tmp")] and not list(scratch.iterdir())
    assert bridge.remove_block("box-1") == "block removed" and bridge.BEGIN not in conf.read_text()
    assert bridge.remove_block("box-1") == "no block"
    # the agent link: made once, left alone while it already points at this session's agent
    env = {**os.environ, "HOME": str(box), "SSH_AUTH_SOCK": str(tmp_path / "agent-1")}
    cmd = bridge.remote_command(cfg, "auto")
    assert subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, timeout=20).returncode == 0
    assert cfg["socket_dir"].startswith("~/")
    d = box / cfg["socket_dir"][2:]
    ln = d / "auto.sock"
    assert os.readlink(ln) == env["SSH_AUTH_SOCK"] and oct(d.stat().st_mode & 0o777) == "0o700"
    os.utime(ln, (1, 1), follow_symlinks=False)
    os.utime(d, (1, 1))
    assert subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, timeout=20).returncode == 0
    assert ln.lstat().st_mtime == 1 and d.stat().st_mtime == 1
    env["SSH_AUTH_SOCK"] = str(tmp_path / "agent-2")
    assert subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, timeout=20).returncode == 0
    assert os.readlink(ln) == env["SSH_AUTH_SOCK"]


def _box_sh(home, script, inp=None, **env):
    """A box's shell, faked locally: the script runs with HOME at a scratch folder."""
    return subprocess.run(["sh", "-c", script], input=inp, capture_output=True, text=True, timeout=20,
                          env={"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env})


def test_bridge_box_link_is_touched_only_on_change_and_fails_closed(tmp_path):
    from ttp import bridge
    home = tmp_path / "box-home"
    home.mkdir()
    agent = socket.socket(socket.AF_UNIX)
    agent.bind(str(tmp_path / "a.sock"))
    remote = bridge.remote_command(_bridge_cfg(), "auto")
    assert remote.split("while [")[0].count("ln -sfn") == 1 and 'readlink "$HOME/.ssh/tt-agent/auto.sock"' in remote
    link_part = remote.split("while [")[0] + "true"   # without the stay-up loop
    run = lambda: _box_sh(home, link_part, SSH_AUTH_SOCK=str(tmp_path / "a.sock"))
    d = home / ".ssh" / "tt-agent"
    assert run().returncode == 0
    assert os.readlink(d / "auto.sock") == str(tmp_path / "a.sock") and stat.S_IMODE(os.stat(d).st_mode) == 0o700
    seen = lambda: (os.lstat(d / "auto.sock").st_ino, os.lstat(d / "auto.sock").st_mtime_ns, os.stat(d).st_ctime_ns)
    before = seen()
    assert run().returncode == 0 and seen() == before   # a reconnect to the same agent path changes nothing
    os.chmod(d, 0o755)
    assert run().returncode == 0 and stat.S_IMODE(os.stat(d).st_mode) == 0o700   # a loosened folder is tightened
    shutil.rmtree(d)
    other = tmp_path / "other"
    other.mkdir()
    os.chmod(other, 0o755)   # explicit: a host umask such as 027 would trim mkdir's mode
    os.symlink(other, d)
    r = run()
    assert r.returncode == 4 and "not a private folder" in r.stderr
    assert not (other / "auto.sock").exists() and stat.S_IMODE(os.stat(other).st_mode) == 0o755


def test_bridge_box_config_is_written_only_when_its_content_changes(tmp_path, monkeypatch):
    from ttp import bridge
    home = tmp_path / "box-home"
    home.mkdir()
    reads = []

    def box(name, script, timeout=40, inp=None):
        r = _box_sh(home, script, inp)
        if inp is None and reads:
            r.stdout = reads.pop(0)   # a stale read: the file changed after it was read
        return r

    monkeypatch.setattr(bridge, "_box", box)
    cfg = _bridge_cfg()
    conf = home / ".ssh" / "config"
    assert bridge.write_block(cfg, "box-1") == "block written (new config)"
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600 and bridge.BEGIN in conf.read_text()
    st = conf.stat()
    assert bridge.write_block(cfg, "box-1") == "block already current"
    reads.append("")
    assert bridge.write_block(cfg, "box-1") == "block already current"   # compared on the box as well
    assert (conf.stat().st_ino, conf.stat().st_mtime_ns) == (st.st_ino, st.st_mtime_ns)
    assert sorted(p.name for p in conf.parent.iterdir()) == ["config"]   # no backup, no temp file left
    conf.write_text("Host other\n  User someone\n")
    assert bridge.write_block(cfg, "box-1").startswith("block written (backup")
    assert conf.read_text().startswith(bridge.BEGIN) and conf.read_text().endswith("Host other\n  User someone\n")
    assert len(list(conf.parent.glob("config.bak-ttp-bridge-*"))) == 1
    assert bridge.remove_block("box-1") == "block removed" and conf.read_text() == "Host other\n  User someone\n"


def test_bridge_relay_refuses_a_shared_or_linked_socket_dir_and_installs_on_change(tmp_path, monkeypatch):
    from ttp import bridge, bridge_relay
    d = tmp_path / "relay"
    d.mkdir(mode=0o755)
    assert bridge_relay.private_dir(str(d)) and stat.S_IMODE(d.stat().st_mode) == 0o700   # tightened
    other = tmp_path / "elsewhere"
    other.mkdir(mode=0o700)
    os.symlink(other, tmp_path / "linked")
    assert not bridge_relay.private_dir(str(tmp_path / "linked"))
    (tmp_path / "file").write_text("")
    assert not bridge_relay.private_dir(str(tmp_path / "file"))
    ran = tmp_path / "ran"
    rc = bridge_relay.main([str(tmp_path / "linked"), "x=10.0.0.3%vpn0", "--", "touch", str(ran)])
    assert rc == 4 and not ran.exists() and not (other / "x.sock").exists()   # fails closed, no session
    monkeypatch.setattr(bridge, "RELAY", tmp_path / "copy" / "bridge_relay.py")
    bridge.install_relay()
    st = bridge.RELAY.stat()
    assert bridge.RELAY.read_bytes() == pathlib.Path(bridge.__file__).with_name("bridge_relay.py").read_bytes()
    bridge.install_relay()
    assert (bridge.RELAY.stat().st_ino, bridge.RELAY.stat().st_mtime_ns) == (st.st_ino, st.st_mtime_ns)
    monkeypatch.setenv("HOME", str(tmp_path))
    argv = bridge.ssh_argv(_bridge_cfg(), "box-1", "auto", ssh="ssh")
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "-R"]
    assert binds and all(b.startswith("127.0.0.1:") for b in binds)   # never a non-loopback listener


# --- stale-session recovery (bridge_relay --tag) ----------------------------------------------------
# A fake ssh (a Python script) plays each session from a plan and logs its argv; no host is reached.

_BRIDGE_FAKE_SSH = """
import json, os, sys
d = os.path.dirname(os.path.abspath(sys.argv[0]))
log = os.path.join(d, 'calls.jsonl')
calls = [json.loads(ln) for ln in open(log)] if os.path.exists(log) else []
open(log, 'a').write(json.dumps(sys.argv[1:]) + '\\n')
if 'ttp-bridge-clean' in sys.argv[-1]:
    print('ended 2301 4242 3600')
    print("kept 2302 4343 not this computer's session")
    print('ended rec 4545 7200')
    if sum(1 for c in calls if 'ttp-bridge-clean' in c[-1]) < int(os.environ.get('FAKE_HELD_N', '0')):
        print('held 2301 4242')   # this many cleanups in a row leave the port held
    sys.exit(0)
step = json.load(open(os.path.join(d, 'plan.json')))[sum(1 for c in calls if 'ttp-bridge-clean' not in c[-1])]
if step == 'fwd':
    sys.stderr.write('Error: remote port forwarding failed for listen port 2301\\n')
    sys.exit(255)
if step == 'agentdead':
    sys.stderr.write('ttp bridge: auto forwarded agent stopped answering; reconnecting\\n')
    sys.exit(5)
if step == 'auth':
    sys.stderr.write('user@box-a: Permission denied (publickey).\\n')
    sys.exit(255)
if step == 'linger':   # the remote command ended, but open agent channels keep ssh up; it ignores SIGTERM
    import signal, subprocess, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    kid = subprocess.Popen(['sleep', '120'])   # a helper in ssh's process group (ProxyCommand-like)
    open(os.path.join(d, 'kid.pid'), 'w').write(str(kid.pid))
    sys.stderr.write('ttp-bridge-end 5\\n'); sys.stderr.flush()
    time.sleep(120)
if step == 'hold':     # a healthy session that stays up a few seconds
    import time
    time.sleep(float(os.environ.get('FAKE_HOLD_S', '3')))
sys.exit(0)
"""


def _fake_relay_ssh(d, plan, agent="/agents/a.sock"):
    import json
    import sys
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(json.dumps(plan))
    ssh = d / "fake-ssh"
    ssh.write_text(f"#!{sys.executable}\n" + _BRIDGE_FAKE_SSH)
    ssh.chmod(0o755)
    return [str(ssh), "-T", "-n", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=20",
            "-o", "IdentitiesOnly=yes", "-o", "IdentityFile=/keys/login.pub", "-o", f"ForwardAgent={agent}",
            "-R", "127.0.0.1:2301:build-a.example.com:22", "-R", "127.0.0.1:2302:build-b.example.com:22",
            "--", "box-a", "echo linked"]


def _calls(d):
    import json
    return [json.loads(ln) for ln in (d / "calls.jsonl").read_text().splitlines()]


def test_bridge_forward_failure_cleans_once_and_retries(tmp_path, capfd):
    # cells: forward-fail-retry
    from ttp import bridge_relay
    argv = _fake_relay_ssh(tmp_path / "s", ["fwd", "ok"])
    rc = bridge_relay.main(["--tag", "box-a/auto", str(tmp_path / "relay" / "box-a.auto"), "--", *argv])
    calls = _calls(tmp_path / "s")
    assert rc == 0 and len(calls) == 3
    cid = (tmp_path / "relay" / "computer-id").read_text().strip()
    first, clean, retry = calls
    tag = f"ttp-bridge-tag={cid}/box-a/auto/"
    assert first[-1].startswith(": " + tag) and retry[-1].startswith(": " + tag) and first[-1] != retry[-1]
    assert "-R" not in clean and "ForwardAgent=/agents/a.sock" not in clean and "ExitOnForwardFailure=yes" not in clean
    assert {"-a", "ForwardAgent=no", "ClearAllForwardings=yes", "ConnectTimeout=10", "BatchMode=yes",
            "IdentityFile=/keys/login.pub"} <= set(clean)
    assert clean[clean.index("--") + 1] == "box-a" and clean[-1].endswith(f" {tag} bridge-{cid}+box-a+auto+ '' 2301 2302")
    err = capfd.readouterr().err
    assert "remote port forwarding failed" in err   # passed through to the log
    assert "box-a: ended a stale session holding port 2301 (pid 4242, 3600 s old)" in err
    assert "box-a: ended a stale session found by its record (pid 4545, 7200 s old)" in err


def test_bridge_second_forward_failure_exits_after_one_cleanup(tmp_path, capfd):
    # cells: forward-fail-twice
    from ttp import bridge_relay
    argv = _fake_relay_ssh(tmp_path / "s", ["fwd", "fwd", "ok"])
    rc = bridge_relay.main(["--tag", "box-a/auto", str(tmp_path / "relay" / "box-a.auto"), "--", *argv])
    calls = _calls(tmp_path / "s")
    assert rc == 255 and len(calls) == 3 and sum("ttp-bridge-clean" in c[-1] for c in calls) == 1
    assert "failed again right after the cleanup" in capfd.readouterr().err


def test_bridge_auth_or_connect_failure_does_no_cleanup(tmp_path):
    # cells: auth-no-cleanup
    from ttp import bridge_relay
    argv = _fake_relay_ssh(tmp_path / "s", ["auth", "ok"])
    rc = bridge_relay.main(["--tag", "box-a/auto", str(tmp_path / "relay" / "box-a.auto"), "--", *argv])
    assert rc == 255 and len(_calls(tmp_path / "s")) == 1
    argv = _fake_relay_ssh(tmp_path / "u", ["fwd"])   # untagged (an older service's command): no recovery either
    assert bridge_relay.main([str(tmp_path / "relay" / "x"), "--", *argv]) == 255
    assert len(_calls(tmp_path / "u")) == 1


def test_bridge_box_cleanup_ends_only_this_computers_tagged_session(tmp_path):
    # cells: box-cleanup-scope
    # Real processes named like sshd sessions; a fake `ss` says which pid listens on which port.
    import os
    import shlex
    import subprocess
    import time
    from ttp import bridge_relay
    tag = "ttp-bridge-tag=aaaaaaaaaaaa/box-a/auto/"

    def session(child):
        return subprocess.Popen(["bash", "-c", f'exec -a "sshd: u@notty" bash -c {shlex.quote(child + " & wait")}'])

    stale = session(f"sh -c ': {tag}0001; sleep 300; :'")
    login = session("sh -c 'sleep 300; :'")                                                  # interactive: no tag
    other = session("sh -c ': ttp-bridge-tag=bbbbbbbbbbbb/box-a/auto/0002; sleep 300; :'")   # another computer
    procs = [stale, login, other]
    try:
        time.sleep(0.5)
        bin_ = tmp_path / "bin"
        bin_.mkdir()
        rows = [(2301, stale.pid), (2302, login.pid), (2303, other.pid), (2304, 1), (2309, stale.pid)]
        (bin_ / "ss").write_text("#!/bin/sh\ncat <<'X'\n" + "".join(
            f'LISTEN 0 128 127.0.0.1:{p} 0.0.0.0:* users:(("sshd",pid={pid},fd=9))\n' for p, pid in rows) + "X\n")
        (bin_ / "ss").chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", XDG_CACHE_HOME=str(tmp_path / "cache"))
        r = subprocess.run(["sh", "-c", bridge_relay.CLEANUP, "x", tag, "bridge-aaaaaaaaaaaa+box-a+auto+", "",
                            "2301", "2302", "2303", "2304"],
                           capture_output=True, text=True, env=env, timeout=30)
        out = r.stdout.splitlines()
        assert [ln.split()[:3] for ln in out if ln.startswith("ended")] == [["ended", "2301", str(stale.pid)]]
        assert f"kept 2302 {login.pid} not this computer's session" in out
        assert f"kept 2303 {other.pid} not this computer's session" in out
        assert "kept 2304 1 another account" in out or os.getuid() == 0
        stale.wait(timeout=10)
        assert login.poll() is None and other.poll() is None
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()


def test_bridge_refresh_reinstalls_a_service_whose_command_changed(tmp_path, monkeypatch):
    # cells: refresh-argv
    import json
    import plistlib
    from ttp import bridge, hoststate
    monkeypatch.delenv("TTP_TEST_NO_BRIDGE_REFRESH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(bridge, "HOME_DIR", tmp_path)
    monkeypatch.setattr(bridge, "RELAY", tmp_path / "bridge_relay.py")
    conf = tmp_path / "bridge.json"
    conf.write_text(json.dumps({"boxes": {"box-1": {"login": {}, "agents": {"auto": {"agent": "env", "forward": [
        {"name": "build-a", "target": "build-a.example.com", "port": 2301}]}}}}}))
    started = []
    monkeypatch.setattr(bridge, "start", lambda box, an, argv, platform=None: started.append(argv) or "restarted")
    from ttp import bridge_watch
    monkeypatch.setattr(bridge_watch, "install", lambda platform=None: "unchanged")
    cfg = bridge.load(conf)
    new = bridge.service_argv(cfg, "box-1", "auto", platform="darwin")
    assert "--keep-awake" in new
    old = bridge.ssh_argv(cfg, "box-1", "auto")   # what an older release installed: the bare session
    f = bridge.service_file("box-1", "auto", "darwin")
    f.parent.mkdir(parents=True)
    f.write_bytes(plistlib.dumps(bridge.launchd_plist("box-1", "auto", old)))
    assert bridge.refresh("darwin", conf) == ["bridge box-1 auto: service command changed; restarted"]
    assert started == [new] and bridge.RELAY.is_file()
    f.write_bytes(plistlib.dumps(bridge.launchd_plist("box-1", "auto", new)))
    assert bridge.launchd_plist("box-1", "auto", new)["ProgramArguments"][6:8] == ["--tag", "box-1/auto"]
    assert bridge.refresh("darwin", conf) == [] and len(started) == 1   # unchanged: left running
    nowake = [a for a in new if a != "--keep-awake"]   # a relay command from before the keep-awake flag
    f.write_bytes(plistlib.dumps(bridge.launchd_plist("box-1", "auto", nowake)))
    assert bridge.refresh("darwin", conf) == ["bridge box-1 auto: service command changed; restarted"]
    assert started[-1] == new   # an upgrade rolls the flag out without a manual reinstall
    started.clear()
    f.write_bytes(plistlib.dumps(bridge.launchd_plist("box-1", "auto", new)))
    monkeypatch.setattr(hoststate, "unit_dir", lambda: tmp_path / "units")
    (tmp_path / "units").mkdir()
    unit = bridge.systemd_unit("box-1", "auto", new)
    assert "--tag box-1/auto" in unit
    bridge.service_file("box-1", "auto", "linux").write_text(unit)
    assert bridge.installed_argv("box-1", "auto", "linux") == new
    monkeypatch.setenv("TTP_TEST_NO_BRIDGE_REFRESH", "1")
    assert bridge.refresh("darwin", conf) == []


def test_bridge_refresh_restarts_a_session_whose_relay_copy_changed_and_the_check_follows_it(tmp_path, monkeypatch):
    # cells: relay_restart_on_change
    import json
    import plistlib
    from ttp import bridge
    monkeypatch.delenv("TTP_TEST_NO_BRIDGE_REFRESH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(bridge, "HOME_DIR", tmp_path)
    monkeypatch.setattr(bridge, "RELAY", tmp_path / "bridge_relay.py")
    conf = tmp_path / "bridge.json"
    conf.write_text(json.dumps({"boxes": {"box-1": {"login": {}, "agents": {"auto": {"agent": "env", "forward": [
        {"name": "build-a", "target": "build-a.example.com", "port": 2301}]}}}}}))
    started = []
    monkeypatch.setattr(bridge, "start", lambda box, an, argv, platform=None: started.append(argv) or "restarted")
    from ttp import bridge_watch
    monkeypatch.setattr(bridge_watch, "install", lambda platform=None: "unchanged")
    argv = bridge.service_argv(bridge.load(conf), "box-1", "auto", platform="darwin")
    f = bridge.service_file("box-1", "auto", "darwin")
    f.parent.mkdir(parents=True)
    f.write_bytes(plistlib.dumps(bridge.launchd_plist("box-1", "auto", argv)))
    bridge.RELAY.write_text("# an older release's relay\n")
    restarted = {}
    assert bridge.refresh("darwin", conf, restarted=restarted) == ["bridge box-1 auto: relay changed; restarted"]
    assert started == [argv] and bridge.RELAY.read_bytes() == \
        pathlib.Path(bridge.__file__).with_name("bridge_relay.py").read_bytes(), "the same command, the new relay"
    assert restarted == {("box-1", "auto"): "restarted"}, "refresh_checked then checks the restarted session"
    assert bridge.refresh("darwin", conf) == [] and len(started) == 1, "relay and command unchanged: left running"
    checked = []
    monkeypatch.setattr(bridge, "status", lambda *a, **k: checked.append(a) or ([], 0), raising=False)
    bridge.RELAY.write_text("# older again\n")
    out, rc = bridge.refresh_checked("darwin", conf, wait_s=0)
    assert out[0] == "bridge box-1 auto: relay changed; restarted" and len(started) == 2


# --- a dead agent behind a live-looking link (box loop, wrapper reconnect, host watch) --------------
# A stub ssh-add answers by marker files next to the socket it is asked about: <sock>.dead (exit 2),
# <sock>.fetch (exit 1, a failed exchange), <sock>.empty (exit 1, no keys). No real agent or key.

_BRIDGE_STUB_SSH_ADD = """#!/bin/sh
t=$SSH_AUTH_SOCK
[ -L "$t" ] && t=$(readlink "$t")
[ -e "$t.hang" ] && exec sleep 60
[ -e "$t.dead" ] && { echo "Could not open a connection to your authentication agent." >&2; exit 2; }
[ -e "$t.fetch" ] && { echo "error fetching identities: communication with agent failed" >&2; exit 1; }
[ -e "$t.empty" ] && { echo "The agent has no identities."; exit 1; }
echo "256 SHA256:stub test (ED25519)"
"""


def _bridge_stub_agents(tmp_path, *names):
    """A stub ssh-add first on PATH, and a listening Unix socket per agent name."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "ssh-add").write_text(_BRIDGE_STUB_SSH_ADD)
    (bin_dir / "ssh-add").chmod(0o755)
    socks = []
    for n in names:
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(tmp_path / f"{n}.sock"))
        s.listen(1)
        socks.append(s)
    return f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}", socks


def _bridge_box_loop(tmp_path, monkeypatch, own, linked):
    """The box side's stay-up loop with a link already pointing at `linked` (its first link step off)."""
    from ttp import bridge
    monkeypatch.setattr(bridge, "CHECK_S", 0.1)
    home = tmp_path / "box-home"
    d = home / ".ssh" / "tt-agent"
    d.mkdir(parents=True)
    d.chmod(0o700)
    os.symlink(tmp_path / f"{linked}.sock", d / "auto.sock")
    remote = bridge.remote_command(_bridge_cfg(), "auto").replace("ln -sfn", "true", 1)
    path, socks = _bridge_stub_agents(tmp_path, own, linked)
    proc = subprocess.Popen(["sh", "-c", remote], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={"HOME": str(home), "PATH": path, "SSH_AUTH_SOCK": str(tmp_path / f"{own}.sock")})
    return proc, d / "auto.sock", socks


def _bridge_wait(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_bridge_box_loop_relinks_a_dead_link_and_exits_when_its_own_agent_dies(tmp_path, monkeypatch):
    # cells: box-relink, box-own-dead
    from ttp import bridge
    (tmp_path / "old.sock.dead").touch()   # an earlier session's link whose agent died
    proc, ln, _socks = _bridge_box_loop(tmp_path, monkeypatch, "mine", "old")
    try:
        assert _bridge_wait(lambda: os.readlink(ln) == str(tmp_path / "mine.sock"))
        (tmp_path / "mine.sock.fetch").touch()   # exit 1 with a failed exchange is a dead agent too
        rc = proc.wait(timeout=15)
    finally:
        proc.kill()
    out, err = proc.communicate()
    assert rc == bridge.AGENT_DEAD and "relinked" in out and "stopped answering; reconnecting" in err


def test_bridge_box_loop_leaves_a_link_whose_agent_answers(tmp_path, monkeypatch):
    # cells: box-live-kept
    (tmp_path / "newer.sock.empty").touch()   # a newer session's live agent, holding no keys
    proc, ln, _socks = _bridge_box_loop(tmp_path, monkeypatch, "mine", "newer")
    try:
        time.sleep(1.0)   # about ten checks
        assert proc.poll() is None and os.readlink(ln) == str(tmp_path / "newer.sock")
    finally:
        proc.kill()
        proc.communicate(timeout=5)


def test_bridge_dead_agent_reconnects_with_bounded_retries(tmp_path, monkeypatch, capfd):
    # cells: relay-reconnect, relay-bounded
    from ttp import bridge_relay
    monkeypatch.setattr(bridge_relay, "AGENT_RETRY_S", (0, 0, 0))
    argv = _fake_relay_ssh(tmp_path / "s", ["agentdead", "ok"])
    rc = bridge_relay.main(["--tag", "box-a/auto", str(tmp_path / "relay" / "box-a.auto"), "--", *argv])
    calls = _calls(tmp_path / "s")
    assert rc == 0 and len(calls) == 2 and not any("ttp-bridge-clean" in c[-1] for c in calls)
    assert "stopped answering; reconnecting" in capfd.readouterr().err
    argv = _fake_relay_ssh(tmp_path / "t", ["agentdead"] * 5)
    rc = bridge_relay.main(["--tag", "box-a/auto", str(tmp_path / "relay" / "box-a.auto"), "--", *argv])
    assert rc == bridge_relay.AGENT_DEAD and len(_calls(tmp_path / "t")) == 4
    assert "died again after each reconnect" in capfd.readouterr().err


def test_bridge_cert_agent_probe_counts_a_hung_agent_as_down(tmp_path):
    # cells: cert-probe-timeout
    # The Match exec probe of the offline route: an answering agent (keys or none) is up; a dead one or
    # one whose ssh-add hangs is down within the probe's time limit, so ssh falls through, never hangs.
    import os
    import subprocess
    import time
    from ttp import bridge_cert
    path, socks = _bridge_stub_agents(tmp_path, "a")
    probe = bridge_cert.agent_up("a.sock")
    env = dict(os.environ, PATH=path, HOME=str(tmp_path))

    def up():
        (tmp_path / "a.sock.up").unlink(missing_ok=True)   # each state asks the agent (no cached answer)
        t0 = time.monotonic()
        rc = subprocess.run(["sh", "-c", probe], env=env, stdin=subprocess.DEVNULL, timeout=30).returncode
        return rc == 0, time.monotonic() - t0
    assert up()[0]
    (tmp_path / "a.sock.empty").write_text("")
    assert up()[0]
    (tmp_path / "a.sock.empty").unlink()
    (tmp_path / "a.sock.dead").write_text("")
    assert not up()[0]
    (tmp_path / "a.sock.dead").unlink()
    (tmp_path / "a.sock.hang").write_text("")
    ok, took = up()
    assert not ok and took < bridge_cert.PROBE_S + 3
    for s in socks:
        s.close()


def test_bridge_cert_agent_probe_reuses_a_healthy_answer_briefly(tmp_path):
    # cells: cert-probe-cache
    # ssh runs the probe at every config parse (twice per bridged host, multiplexed clients too): a burst
    # of 20 ssh calls asks the agent once, not 20 times; a down agent is never cached, a garbled or old
    # cache entry is ignored, and the probe still has no double quotes, backslashes or percent signs.
    import os
    import subprocess
    from ttp import bridge_cert
    path, socks = _bridge_stub_agents(tmp_path, "a")
    log = tmp_path / "asks"
    real = tmp_path / "bin" / "ssh-add"
    (tmp_path / "bin" / "ssh-add.real").write_text(real.read_text())
    (tmp_path / "bin" / "ssh-add.real").chmod(0o755)
    real.write_text(f'#!/bin/sh\necho x >> {log}\nexec {tmp_path}/bin/ssh-add.real "$@"\n')
    probe = bridge_cert.agent_up("a.sock")
    assert not any(c in probe for c in '"\\') and '%%' not in probe   # match_quote doubles % for ssh
    env = dict(os.environ, PATH=path, HOME=str(tmp_path))

    def up():
        return subprocess.run(["sh", "-c", probe], env=env, stdin=subprocess.DEVNULL, timeout=30).returncode == 0

    def asks():
        return len(log.read_text().splitlines()) if log.exists() else 0
    assert all(up() for _ in range(20))
    assert asks() == 1 and (tmp_path / "a.sock.up").is_file()
    (tmp_path / "a.sock.up").write_text("garbage\n")
    assert up() and asks() == 2
    (tmp_path / "a.sock.up").write_text("1\n")   # long expired
    assert up() and asks() == 3
    (tmp_path / "a.sock.up").unlink()
    (tmp_path / "a.sock.dead").write_text("")
    assert not up() and not up() and asks() == 5
    assert not (tmp_path / "a.sock.up").exists()
    assert not list(tmp_path.glob("a.sock.up.*"))
    for s in socks:
        s.close()


def _bridge_alive(pid):
    """A process that still runs (a zombie has ended)."""
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def test_bridge_remote_command_reports_its_end(tmp_path):
    # cells: relay-end-marker
    # tagged()'s EXIT trap writes the remote command's exit code as its last stderr line and keeps that code.
    import os
    import subprocess
    from ttp import bridge_relay
    for tail, rc in (("exit 5", 5), ("true", 0)):
        cmd = bridge_relay.tagged(["ssh", "--", "box-a", tail], "aaaaaaaaaaaa/box-a/auto/0001")[-1]
        r = subprocess.run(["sh", "-c", cmd], env=dict(os.environ, XDG_CACHE_HOME=str(tmp_path)),
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == rc and r.stderr.splitlines()[-1] == f"ttp-bridge-end {rc}"
        assert bridge_relay.END.match(r.stderr.splitlines()[-1])


def test_bridge_ssh_left_up_after_its_remote_command_is_ended_and_reconnects(tmp_path, monkeypatch, capfd):
    # cells: relay-linger-kill
    # The remote loop exited 5, but ssh stays up (and ignores SIGTERM): the relay ends its whole
    # process group (SIGKILL after the grace), then reconnects as for a dead agent.
    import time
    from ttp import bridge_relay
    monkeypatch.setattr(bridge_relay, "AGENT_RETRY_S", (0, 0, 0))
    monkeypatch.setattr(bridge_relay, "END_GRACE_S", 0.3)
    monkeypatch.setattr(bridge_relay, "KILL_GRACE_S", 0.5)
    argv = _fake_relay_ssh(tmp_path / "s", ["linger", "ok"])
    t0 = time.monotonic()
    rc = bridge_relay.main(["--tag", "box-a/auto", str(tmp_path / "relay" / "box-a.auto"), "--", *argv])
    assert rc == 0 and len(_calls(tmp_path / "s")) == 2 and time.monotonic() - t0 < 30
    kid = int((tmp_path / "s" / "kid.pid").read_text())
    assert _bridge_wait(lambda: not _bridge_alive(kid), 10)   # the group went with it
    err = capfd.readouterr().err
    assert "the remote command ended (exit 5) but ssh stayed up" in err and "reconnecting in 0s" in err
    assert "ttp-bridge-end" not in err   # the marker is read, not passed through


def test_bridge_hung_local_agent_ends_the_session_and_reconnects(tmp_path, monkeypatch, capfd):
    # cells: relay-agent-hang
    # The local agent the session forwards stops answering (ssh-add -l hangs past its timeout, twice):
    # the relay ends the still-running ssh, logs why, and reconnects; the second session runs on.
    import time
    from ttp import bridge_relay
    path, socks = _bridge_stub_agents(tmp_path, "a")
    monkeypatch.setenv("PATH", path)
    monkeypatch.setenv("FAKE_HOLD_S", "60")
    for k, v in (("AGENT_RETRY_S", (0, 0, 0)), ("LOCAL_CHECK_S", 0.2), ("LOCAL_TIMEOUT_S", 0.5),
                 ("LOCAL_RECHECK_S", 0.1), ("KILL_GRACE_S", 0.5)):
        monkeypatch.setattr(bridge_relay, k, v)
    sock = tmp_path / "a.sock"
    (tmp_path / "a.sock.hang").write_text("")
    argv = _fake_relay_ssh(tmp_path / "s", ["hold", "ok"], agent=str(sock))
    t0 = time.monotonic()
    rc = bridge_relay.main(["--tag", "box-a/auto", str(tmp_path / "relay" / "box-a.auto"), "--", *argv])
    assert rc == 0 and len(_calls(tmp_path / "s")) == 2 and time.monotonic() - t0 < 30
    err = capfd.readouterr().err
    assert "the local agent this session forwards stopped answering (ssh-add -l got no answer" in err
    assert err.count("stopped answering (") == 1 and "reconnecting in 0s" in err
    for s in socks:
        s.close()


def test_bridge_healthy_local_agent_leaves_the_session_alone(tmp_path, monkeypatch, capfd):
    # cells: relay-agent-healthy
    from ttp import bridge_relay
    path, socks = _bridge_stub_agents(tmp_path, "a")
    monkeypatch.setenv("PATH", path)
    monkeypatch.setenv("FAKE_HOLD_S", "2")
    monkeypatch.setattr(bridge_relay, "LOCAL_CHECK_S", 0.2)
    monkeypatch.setattr(bridge_relay, "LOCAL_TIMEOUT_S", 2.0)
    argv = _fake_relay_ssh(tmp_path / "s", ["hold"], agent=str(tmp_path / "a.sock"))
    rc = bridge_relay.main(["--tag", "box-a/auto", str(tmp_path / "relay" / "box-a.auto"), "--", *argv])
    assert rc == 0 and len(_calls(tmp_path / "s")) == 1
    assert "stopped answering" not in capfd.readouterr().err
    for s in socks:
        s.close()


def test_bridge_relay_reads_the_forwarded_agent_socket(monkeypatch):
    # cells: relay-agent-socket
    from ttp import bridge_relay
    env = {"SSH_AUTH_SOCK": "/run/agent.sock", "OTHER": "/run/other.sock", "HOME": "/home/u"}
    def fa(v):
        return bridge_relay.forwarded_agent(["ssh", "-o", f"ForwardAgent={v}", "--", "box", "true"], env)
    assert fa("/a/b.sock") == "/a/b.sock" and fa("yes") == "/run/agent.sock" and fa("no") == ""
    assert fa("${TTP_BRIDGE_AGENT}") == "/run/agent.sock" and fa("$OTHER") == "/run/other.sock"
    assert bridge_relay.forwarded_agent(["ssh", "--", "box", "-o ForwardAgent=/x"], env) == ""


def _bridge_watch_setup(env, tmp_path, monkeypatch, block=None):
    """A daemon and an ssh config whose bridge block names one stub agent link (`auto`); the link is dead
    while `<tmp>/auto.sock.dead` exists. Returns (project, daemon, config, eps(), chat())."""
    from ttp import bridge
    from ttp.daemon import Daemon
    path, _socks = _bridge_stub_agents(tmp_path, "auto")
    monkeypatch.setenv("PATH", path)
    link = str(tmp_path / "auto.sock")
    cfg = tmp_path / "ssh-config"
    cfg.write_text(block or f"{bridge.BEGIN}\nHost build-a\n  IdentityAgent {link}\n{bridge.END}\n")
    p = make(env)
    d = Daemon(p.base)
    key = "bridge-agent:auto"

    def eps():
        return p.db.q("SELECT * FROM alerts WHERE key=? ORDER BY id", (key,))

    def chat():
        return [m for m in p.db.q("SELECT * FROM messages WHERE ref=? AND direction='out'", (key,))
                if m["channel"] == "chat"]
    return p, d, cfg, eps, chat


def test_bridge_host_watch_shows_a_status_line_at_once_then_alerts_after_an_hour_and_clears(env, tmp_path, monkeypatch):
    # cells: host-status-line, host-alert-after-hour, host-clear-steady, host-quiet
    from ttp import alerts, bridge, bridgewatch
    link = str(tmp_path / "auto.sock")
    block = (f"Host other\n  IdentityAgent {tmp_path / 'elsewhere.sock'}\n\n{bridge.BEGIN}\n\n"
             f"Host build-a ba\n  HostName 127.0.0.1\n  IdentityAgent {link}\n\n"
             f"Host build-b\n  IdentityAgent {link}\n{bridge.END}\n")
    p, d, cfg, eps, chat = _bridge_watch_setup(env, tmp_path, monkeypatch, block)
    assert bridgewatch.links(cfg.read_text()) == {link: ["build-a", "ba", "build-b"]}
    key = "bridge-agent:auto"
    now = time.time()
    assert bridgewatch.check(d, now, cfg) == [] and not p.db.q("SELECT id FROM alerts")   # answers: quiet
    (tmp_path / "auto.sock.dead").touch()
    assert bridgewatch.check(d, now + 60, cfg) == []   # the first failed check: a low status line at once
    assert len(eps()) == 1 and eps()[0]["severity"] == "low" and not chat()
    assert alerts.holds(p.db, key, now + 60, now + 70)   # the line stays up while the link is down
    for t in range(120, 3600, 60):   # down nearly an hour: the one line, no chat post
        assert bridgewatch.check(d, now + t, cfg) == []
    assert len(eps()) == 1 and not chat()
    assert bridgewatch.check(d, now + 60 + 3600, cfg) == [key]   # down an hour: the user is told once, normal
    sent = chat()
    assert len(sent) == 1 and sent[0]["severity"] == "normal" and sent[0]["kind"] == "alert"
    t = sent[0]["text"]
    assert "auto agent" in t and "none" not in t and "build-a, ba, build-b" in t
    assert "workstation is down and reconnects by itself" in t
    assert "127.0.0.1" not in t and "VPN" not in t
    assert bridgewatch.check(d, now + 5400, cfg) == [] and len(chat()) == 1   # once per outage
    (tmp_path / "auto.sock.dead").unlink()
    assert bridgewatch.check(d, now + 5460, cfg) == []   # one good probe: not cleared yet
    assert not any(e["cleared"] for e in eps()) and alerts.holds(p.db, key, now + 60, now + 5470)
    for t in range(5520, 5460 + 15 * 60, 60):
        bridgewatch.check(d, now + t, cfg)
    assert not any(e["cleared"] for e in eps())
    assert bridgewatch.check(d, now + 5460 + 15 * 60, cfg) == []   # answered steadily for 15 min: cleared
    assert all(e["cleared"] for e in eps())
    assert not alerts.holds(p.db, key, now + 121, now + 6500)
    assert not p.db.kv(bridgewatch.STATE_KEY, {})
    assert len([m for m in chat() if m["kind"] == "alert"]) == 1


def test_bridge_host_watch_a_ten_minute_flap_posts_nothing_to_the_chat(env, tmp_path, monkeypatch):
    # cells: host-short-drop, host-flap-one-outage
    from ttp import alerts, bridgewatch
    p, d, cfg, eps, chat = _bridge_watch_setup(env, tmp_path, monkeypatch)
    key = "bridge-agent:auto"
    now = time.time()
    dead = tmp_path / "auto.sock.dead"
    # The 11-12 pattern: down 4 min, back 5, down 1, back 3, down 2 min, then steady.
    for start, end, down in [(0, 240, 1), (240, 540, 0), (540, 600, 1), (600, 780, 0), (780, 900, 1),
                             (900, 1140, 0)]:
        dead.touch() if down else dead.unlink(missing_ok=True)
        for s in range(start, end, 60):
            assert bridgewatch.check(d, now + s, cfg) == []
    assert len(eps()) == 1 and not chat()   # one quiet line for the whole flap
    st = p.db.kv(bridgewatch.STATE_KEY, {})
    rec = next(iter(st.values()))
    assert rec["since"] == now and rec["flaps"] == 2   # the clock was never reset
    assert alerts.holds(p.db, key, now, now + 1000)
    for s in range(1140, 900 + 15 * 60 + 60, 60):   # steady from 15:00 on: cleared quietly at 30:00
        bridgewatch.check(d, now + s, cfg)
    assert all(e["cleared"] for e in eps()) and not chat()
    assert not p.db.q("SELECT id FROM messages WHERE ref=? AND channel='chat'", (key,))


def test_bridge_host_watch_a_flapping_outage_alerts_once_by_its_first_drop(env, tmp_path, monkeypatch):
    # cells: host-flap-keeps-clock, host-threshold-setting
    from ttp import alerts, bridgewatch
    p, d, cfg, eps, chat = _bridge_watch_setup(env, tmp_path, monkeypatch)
    key = "bridge-agent:auto"
    now = time.time()
    dead = tmp_path / "auto.sock.dead"
    # Down 10 min, back 10 min, down again: never down an hour straight, but one outage past an hour.
    t = 0
    while t <= 3600 + 60:
        if (t // 600) % 2 == 0:
            dead.touch()
        else:
            dead.unlink(missing_ok=True)
        bridgewatch.check(d, now + t, cfg)
        t += 60
    assert [m["severity"] for m in chat() if m["kind"] == "alert"] == ["normal"]
    assert "brief return" in chat()[0]["text"]
    assert len(eps()) == 1
    assert bridgewatch.after_s({}) == 3600 and bridgewatch.steady_s({}) == 15 * 60
    assert bridgewatch.after_s({"bridge": {"agent_alert_after_min": "x"}}) == 3600
    assert bridgewatch.after_s({"bridge": {"agent_alert_after_min": 0}}) == bridgewatch.FLOOR_S
    d.cfg = {**d.cfg, "bridge": {"agent_alert_after_min": 5, "agent_steady_min": 3}}
    dead.unlink(missing_ok=True)
    for s in range(3720, 3720 + 4 * 60, 60):   # steady for 3 min (the setting): cleared
        bridgewatch.check(d, now + s, cfg)
    assert all(e["cleared"] for e in eps())
    dead.touch()
    bridgewatch.check(d, now + 4000, cfg)
    assert bridgewatch.check(d, now + 4000 + 301, cfg) == [key]   # a new outage, past the 5 min set here
    assert not alerts.holds(p.db, "bridge-agent:none", now, now + 5000)


def test_bridge_host_watch_reads_an_older_state_record(env, tmp_path, monkeypatch):
    # cells: host-old-record
    from ttp import bridgewatch
    p, d, cfg, eps, chat = _bridge_watch_setup(env, tmp_path, monkeypatch)
    now = time.time()
    link = str(tmp_path / "auto.sock")
    p.db.set_kv(bridgewatch.STATE_KEY, {link: {"since": now - 7200, "stage": 3, "alerted": True}})
    (tmp_path / "auto.sock.dead").touch()
    assert bridgewatch.check(d, now, cfg) == [] and not chat()   # told under the older stages: not again
    assert p.db.kv(bridgewatch.STATE_KEY, {})[link]["stage"] == 2


def test_bridge_host_watch_skips_ssh_identityagent_keywords(env, tmp_path, monkeypatch):
    # cells: host-real-name
    from ttp import bridge, bridgewatch
    from ttp.daemon import Daemon
    path, _socks = _bridge_stub_agents(tmp_path, "auto")
    monkeypatch.setenv("PATH", path)
    link = str(tmp_path / "auto.sock")
    cfg = tmp_path / "ssh-config"
    # The offline-cert fallback writes `IdentityAgent none` inside the block; ssh also takes
    # SSH_AUTH_SOCK and an environment variable. None of them is a bridge link.
    cfg.write_text(f"{bridge.BEGIN}\nHost build-a\n  IdentityAgent {link}\n\nHost cert-a\n  IdentityAgent none\n\n"
                   f"Host cert-b\n  IdentityAgent \"none\"\n\nHost login\n  IdentityAgent SSH_AUTH_SOCK\n\n"
                   f"Host envy\n  IdentityAgent $MY_AGENT\n{bridge.END}\n")
    assert bridgewatch.links(cfg.read_text()) == {link: ["build-a"]}
    p = make(env)
    d = Daemon(p.base)
    now = time.time()
    (tmp_path / "auto.sock.dead").touch()
    bridgewatch.check(d, now, cfg)
    assert bridgewatch.check(d, now + 3600 + 1, cfg) == ["bridge-agent:auto"]
    assert not p.db.q("SELECT id FROM alerts WHERE key LIKE '%none%' OR key LIKE '%SSH_AUTH%'")


def _fake_launchctl(tmp_path, monkeypatch, fails):
    """A `launchctl` on PATH (never the real one) answering like launchd: its first `fails` bootstraps (None:
    all) fail with error 5 as right after a bootout; `print` shows a loaded job running. Returns its call log."""
    bin_ = tmp_path / "fakebin"
    bin_.mkdir()
    log = tmp_path / "launchctl.calls"
    (bin_ / "launchctl").write_text(f"""#!/bin/sh
d={shlex.quote(str(tmp_path))}
echo "$*" >> "$d/launchctl.calls"
case "$1" in
  bootout) rm -f "$d/loaded" ;;
  bootstrap)
    n=$(cat "$d/failed" 2>/dev/null || echo 0)
    if [ {-1 if fails is None else fails} -lt 0 ] || [ "$n" -lt {0 if fails is None else fails} ]; then
      echo $((n+1)) > "$d/failed"; echo "Bootstrap failed: 5: Input/output error" >&2; exit 5
    fi
    touch "$d/loaded" ;;
  print) [ -e "$d/loaded" ] || exit 113; echo "	state = running" ;;
esac
exit 0
""")
    (bin_ / "launchctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    from ttp import service
    monkeypatch.setattr(service, "BOOTSTRAP_RETRY_S", 0)
    return lambda: log.read_text().splitlines() if log.exists() else []


def _restarting_bridge(tmp_path, monkeypatch):
    """A bridge config with box-1's session installed at an older command (so a refresh restarts it) and a
    box that answers the status probe with its link and port up. Returns the config path."""
    import json
    import plistlib
    from ttp import bridge
    monkeypatch.delenv("TTP_TEST_NO_BRIDGE_REFRESH", raising=False)
    monkeypatch.setattr(bridge, "HOME_DIR", tmp_path)
    monkeypatch.setattr(bridge, "RELAY", tmp_path / "bridge_relay.py")
    conf = tmp_path / "bridge.json"
    conf.write_text(json.dumps({"boxes": {"box-1": {"login": {}, "agents": {"auto": {"agent": "env", "forward": [
        {"name": "build-a", "target": "build-a.example.com", "port": 2301}]}}}}}))
    f = tmp_path / "Library" / "LaunchAgents" / f"{bridge.label('box-1', 'auto')}.plist"
    f.parent.mkdir(parents=True)
    f.write_bytes(plistlib.dumps(bridge.launchd_plist("box-1", "auto", ["ssh", "an-older-command"])))
    monkeypatch.setattr(bridge, "_box", lambda box, script, *a, **k: subprocess.CompletedProcess(
        [], 0, "block yes\nagent auto up 1\nport 2301 up\n", ""))
    from ttp import bridge_watch
    monkeypatch.setattr(bridge_watch, "install", lambda platform=None: "unchanged")
    return conf


def test_bridge_restart_retries_launchd_error_5_and_checks_the_links(tmp_path, monkeypatch):
    # cells: bootstrap-retry
    from ttp import bridge
    calls = _fake_launchctl(tmp_path, monkeypatch, fails=1)
    conf = _restarting_bridge(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    out, rc = bridge.refresh_checked("darwin", conf, wait_s=0)
    assert [c.split()[0] for c in calls() if not c.startswith("print")] == ["bootout", "bootstrap", "bootstrap"]
    assert rc == 0 and out[0] == "bridge box-1 auto: service command changed; launchd agent started", out
    assert out[1].startswith("bridge status: box-1 auto: OK (session running; agent link up, 1 key(s)"), out


def test_upgrade_fails_naming_the_links_a_bridge_restart_left_down(env, tmp_path, monkeypatch):
    # cells: upgrade-bridge-down
    import contextlib
    import io
    from ttp import bridge, cli, service
    p = make(env)
    calls = _fake_launchctl(tmp_path, monkeypatch, fails=None)
    conf = _restarting_bridge(tmp_path, monkeypatch)
    real = bridge.refresh_checked

    def checked():
        with monkeypatch.context() as m:   # only the bridge's own files live in the test's HOME
            m.setenv("HOME", str(tmp_path))
            return real("darwin", conf, wait_s=0)
    monkeypatch.setattr(bridge, "refresh_checked", checked)
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    monkeypatch.setattr(cli, "_upgrade", lambda *a, **k: None)   # the template merge is not under test
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), pytest.raises(SystemExit) as e:
        cli.main(["upgrade", p.name])
    assert e.value.code == 1, (out.getvalue(), err.getvalue())
    assert [c for c in calls() if c.startswith("bootstrap")] and all(
        c.split()[0] in ("bootout", "bootstrap", "print") for c in calls())
    assert sum(c.startswith("bootstrap") for c in calls()) == service.BOOTSTRAP_TRIES
    said = out.getvalue()
    assert "bridge status: box-1 auto: DOWN (session stopped" in said, said
    assert ("bridge: still down after its restart: box-1 auto (launchd refused it: Bootstrap failed: 5: "
            "Input/output error)") in said, said
    assert "the access bridge is still down after its restart" in err.getvalue()


def test_bridge_agent_alert_grace_min_is_retired_and_an_upgrade_drops_it(env):
    """0.2.1158 replaced bridge.agent_alert_grace_min with agent_alert_after_min and agent_steady_min: `ttp
    config` names the retirement and `ttp upgrade` removes the key from project.json, keeping the rest."""
    from ttp import cli
    from ttp.project import unknown_key_hint
    hint = unknown_key_hint("bridge.agent_alert_grace_min")
    assert hint and "retired" in hint and "agent_alert_after_min" in hint and "agent_steady_min" in hint
    assert unknown_key_hint("bridge.agent_alert_after_min") is None and unknown_key_hint("bridge.agent_steady_min") is None
    p = make(env)
    raw = p.raw_config()
    raw.setdefault("bridge", {}).update({"agent_alert_grace_min": 60, "agent_steady_min": 20})
    p.config_path.write_text(json.dumps(raw))
    notes = cli._drop_retired_keys(p)
    assert len(notes) == 1 and "bridge.agent_alert_grace_min" in notes[0], notes
    assert p.raw_config()["bridge"] == {"agent_steady_min": 20}
    assert cli._drop_retired_keys(p) == [], "a rerun is a no-op"


def test_bridge_session_record_is_written_private_and_removed_on_exit(tmp_path):
    # cells: session-record
    # The tagged remote command run here by sh, as the box's login shell would, under a temp cache dir.
    import os
    import subprocess
    from ttp import bridge_relay
    cache = tmp_path / "cache"
    seen = tmp_path / "seen"
    for tail, rc in ((f'cat "$ttp_rec" > {seen}; stat -c %a "$ttp_rec" >> {seen}; echo "$$" >> {seen}', 0),
                     ("exit 3", 3)):
        cmd = bridge_relay.tagged(["ssh", "--", "box-a", tail], "aaaaaaaaaaaa/box a/auto/0001")[-1]
        r = subprocess.run(["sh", "-c", cmd], env=dict(os.environ, XDG_CACHE_HOME=str(cache)), timeout=30)
        assert r.returncode == rc
        assert (cache / "tt-project").is_dir() and list((cache / "tt-project").iterdir()) == []   # removed on exit
    sshd_pid, shell_pid, mode, own = seen.read_text().split()
    assert sshd_pid == str(os.getpid()) and shell_pid == own and mode == "600"


def test_bridge_box_cleanup_reads_session_records_and_spares_reused_pids(tmp_path):
    # cells: record-cleanup
    # The box's sshd made the stale session non-dumpable: ss shows no pid for its port (a fake ss says
    # nothing), so only its record finds it. Records naming a reused pid (another program, another
    # account) or a dead one are removed and their processes left alone; another computer's record stays.
    import os
    import shlex
    import subprocess
    import time
    from ttp import bridge_relay
    tag = "ttp-bridge-tag=aaaaaaaaaaaa/box-a/auto/"
    stale = subprocess.Popen(["bash", "-c", 'exec -a "sshd: u@notty" bash -c ' + shlex.quote(
        f"sh -c ': {tag}0001; sleep 300; :' & wait")])
    reused = subprocess.Popen(["sleep", "300"])
    dead = subprocess.Popen(["true"])
    dead.wait()
    try:
        time.sleep(0.5)
        recs = tmp_path / "cache" / "tt-project"
        recs.mkdir(parents=True)
        mine = "bridge-aaaaaaaaaaaa+box-a+auto+"
        for sid, pid in (("0001", stale.pid), ("0002", reused.pid), ("0003", 1), ("0004", dead.pid)):
            (recs / f"{mine}{sid}.pid").write_text(f"{pid} {pid + 1}\n")
        (recs / "bridge-bbbbbbbbbbbb+box-a+auto+0005.pid").write_text(f"{stale.pid} 1\n")
        bin_ = tmp_path / "bin"
        bin_.mkdir()
        (bin_ / "ss").write_text("#!/bin/sh\nexit 0\n")
        (bin_ / "ss").chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", XDG_CACHE_HOME=str(tmp_path / "cache"))
        r = subprocess.run(["sh", "-c", bridge_relay.CLEANUP, "x", tag, mine, "", "2301"],
                           capture_output=True, text=True, env=env, timeout=30)
        out = r.stdout.splitlines()
        assert [ln.split()[:3] for ln in out if ln.startswith("ended")] == [["ended", "rec", str(stale.pid)]]
        assert f"kept rec {reused.pid} not an sshd session" in out
        assert "kept rec 1 another account" in out or os.getuid() == 0
        assert not any(f" {dead.pid} " in f" {ln} " for ln in out)
        assert sorted(f.name for f in recs.iterdir()) == ["bridge-bbbbbbbbbbbb+box-a+auto+0005.pid"]
        stale.wait(timeout=10)
        assert reused.poll() is None
    finally:
        for p in (stale, reused):
            if p.poll() is None:
                p.kill()
                p.wait()


_FAKE_SS = """#!/bin/sh
# Rows of ROWS (port, pid or -, 1 when ss may show the pid) whose pid still runs (a zombie has ended).
while read -r port pid vis; do
  [ "$pid" = - ] || case "$(ps -o stat= -p "$pid" 2>/dev/null)" in ''|*Z*) continue;; esac
  u=; case "$1" in *p*) [ "$vis" = 1 ] && u=" users:((\\"sshd\\",pid=$pid,fd=9))";; esac
  echo "LISTEN 0 128 127.0.0.1:$port 0.0.0.0:*$u"
done < ROWS
"""


def _fake_sshd(ignore_term=False):
    """A process named like an sshd session with no child (its session command already ended)."""
    import shlex
    import subprocess
    import sys
    code = ("import signal, time; " + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else "")
            + "time.sleep(300)")
    return subprocess.Popen(["bash", "-c", f'exec -a "sshd: u@notty" {shlex.quote(sys.executable)} -c {shlex.quote(code)}'])


def _box_cleanup(tmp_path, rows, link_pid=None, agent_rc=2, ports=("2301",)):
    """Run the box cleanup with a fake ss (rows), a fake ssh-add (agent_rc) and, when link_pid is set,
    an agent link under a scratch HOME pointing at a live socket named agent.<link_pid>. Its stdout lines."""
    import os
    import shutil
    import socket
    import subprocess
    import tempfile
    from ttp import bridge_relay
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (tmp_path / "rows").write_text("".join(f"{port} {pid} {vis}\n" for port, pid, vis in rows))
    (bin_ / "ss").write_text(_FAKE_SS.replace("ROWS", str(tmp_path / "rows")))
    (bin_ / "ssh-add").write_text(f"#!/bin/sh\necho agent >&2\nexit {agent_rc}\n")
    for f in ("ss", "ssh-add"):
        (bin_ / f).chmod(0o755)
    home, sockdir, srv = tmp_path / "home", tempfile.mkdtemp(prefix="ttb"), None   # a short path: sockets fit
    (home / "links").mkdir(parents=True, exist_ok=True)
    try:
        if link_pid is not None:
            srv = socket.socket(socket.AF_UNIX)
            srv.bind(os.path.join(sockdir, f"agent.{link_pid}"))
            os.symlink(os.path.join(sockdir, f"agent.{link_pid}"), home / "links" / "auto.sock")
        env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", HOME=str(home), XDG_CACHE_HOME=str(tmp_path / "cache"))
        r = subprocess.run(["sh", "-c", bridge_relay.CLEANUP, "x", "ttp-bridge-tag=aaaaaaaaaaaa/box-a/auto/",
                            "bridge-aaaaaaaaaaaa+box-a+auto+", "~/links/auto.sock", *ports],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        return r.stdout.splitlines()
    finally:
        if srv is not None:
            srv.close()
        shutil.rmtree(sockdir, ignore_errors=True)
        (home / "links" / "auto.sock").unlink(missing_ok=True)


def test_bridge_box_cleanup_ends_the_session_its_stale_agent_link_names(tmp_path):
    # cells: link-cleanup
    # The stale session's sshd is non-dumpable: ss lists its port with no pid, and it left no record.
    # The agent link still points at agent.<its pid>, whose agent no longer answers: that finds it.
    import time
    stale = _fake_sshd()
    try:
        time.sleep(0.5)
        out = _box_cleanup(tmp_path, [(2301, stale.pid, 0)], link_pid=stale.pid, agent_rc=2)
        assert [ln.split()[:3] for ln in out if ln.startswith("ended")] == [["ended", "link", str(stale.pid)]]
        assert not [ln for ln in out if ln.startswith("held")]   # the port is free again
        assert stale.wait(timeout=10) is not None
    finally:
        if stale.poll() is None:
            stale.kill()
            stale.wait()


def test_bridge_box_cleanup_ends_a_childless_port_owner_and_kills_it_while_the_port_stays_held(tmp_path):
    # cells: port-owner-cleanup, held-after-term
    # ss shows the pid on the port; the session's command already ended (no child), and it ignores
    # SIGTERM, so the port is still held after the grace: SIGKILL follows and the port is free.
    import signal
    import time
    stale = _fake_sshd(ignore_term=True)
    try:
        time.sleep(0.5)
        out = _box_cleanup(tmp_path, [(2301, stale.pid, 1)])
        assert [ln.split()[:3] for ln in out if ln.startswith("ended")] == [["ended", "2301", str(stale.pid)]]
        assert not [ln for ln in out if ln.startswith("held")]
        assert stale.wait(timeout=10) == -signal.SIGKILL
    finally:
        if stale.poll() is None:
            stale.kill()
            stale.wait()


def test_bridge_box_cleanup_refuses_live_agents_other_accounts_its_own_session_and_portless_pids(tmp_path):
    # cells: link-refusals, held-reported
    import os
    import subprocess
    import time
    live = _fake_sshd()      # the link's agent answers: another computer's live session, say
    bystander = _fake_sshd()   # an sshd of this account that neither holds a port nor is linked
    try:
        time.sleep(0.5)
        out = _box_cleanup(tmp_path, [(2301, live.pid, 0)], link_pid=live.pid, agent_rc=0)
        assert f"kept link {live.pid} its agent answers" in out and "held 2301 ?" in out
        assert not [ln for ln in out if ln.startswith("ended")] and live.poll() is None
        mine = _box_cleanup(tmp_path, [], link_pid=os.getpid())   # an ancestor of the cleanup
        assert mine == [f"kept link {os.getpid()} the cleanup's own session"]
        if os.getuid() != 0:
            rows = [ln.split() for ln in subprocess.run(["ps", "-eo", "pid=,uid=,ppid="], capture_output=True,
                                                         text=True).stdout.splitlines() if ln.strip()]
            parent, up, p = {int(r[0]): int(r[2]) for r in rows}, set(), os.getpid()
            while p > 1:
                up.add(p)
                p = parent.get(p, 0)
            other = next(int(r[0]) for r in rows if int(r[1]) != os.getuid() and int(r[0]) > 1 and int(r[0]) not in up)
            assert _box_cleanup(tmp_path, [], link_pid=other) == [f"kept link {other} another account"]
        assert live.poll() is None and bystander.poll() is None
    finally:
        for p in (live, bystander):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_bridge_cleanup_that_leaves_a_port_held_is_a_failure_and_runs_again(tmp_path, capfd, monkeypatch):
    # cells: held-is-failure, held-retries
    from ttp import bridge_relay
    monkeypatch.setattr(bridge_relay, "CLEAN_RETRY_S", (0, 0, 0))
    monkeypatch.setenv("FAKE_HELD_N", "2")   # two cleanups leave the port held, the third frees it
    argv = _fake_relay_ssh(tmp_path / "s", ["fwd", "ok"])
    rc = bridge_relay.main(["--tag", "box-a/auto", "--link", "~/links/auto.sock", str(tmp_path / "relay" / "box-a.auto"),
                            "--", *argv])
    calls = _calls(tmp_path / "s")
    assert rc == 0 and [("ttp-bridge-clean" in c[-1]) for c in calls] == [False, True, True, True, False]
    assert calls[1][-1].endswith(" '~/links/auto.sock' 2301 2302")
    err = capfd.readouterr().err
    assert err.count("box-a: port 2301 is still held, by pid 4242") == 2
    assert err.count("cleaning up again in 0s before reconnecting") == 2
    monkeypatch.setenv("FAKE_HELD_N", "9")   # held every time: the relay exits for the service to restart it
    argv = _fake_relay_ssh(tmp_path / "t", ["fwd", "ok"])
    assert bridge_relay.main(["--tag", "box-a/auto", str(tmp_path / "relay" / "box-a.auto"), "--", *argv]) == 255
    assert sum("ttp-bridge-clean" in c[-1] for c in _calls(tmp_path / "t")) == 4
    assert "the forward ports stay held after 4 cleanups; exiting" in capfd.readouterr().err


def test_bridge_status_says_could_not_check_when_the_probe_did_not_run(monkeypatch):
    # cells: status-unknown
    import subprocess
    from ttp import bridge
    cfg = _bridge_cfg()
    monkeypatch.setattr(bridge, "running", lambda *a, **k: True)
    for rc, out, err, want in (
            (255, "", "Connection timed out", "box-1: unreachable from here"),
            (124, "", "timed out after 40s", "box-1: could not check (link down or no answer; exit 124: timed out"),
            (1, "", "", "box-1: could not check (link down or no answer; exit 1)"),
            (0, "block no\nagent auto up 1\nport 2301 up\nport 2302 up\nagent git up 1\n", "",
             "box-1: the ssh config block is missing")):
        monkeypatch.setattr(bridge, "_box", lambda *a, **k: subprocess.CompletedProcess([], rc, out, err))
        lines, code = bridge.status(cfg, ["box-1"])
        assert code == 1 and lines[0].startswith(want), lines
        assert ("missing" in " ".join(lines)) == (out != "")


class _FakeAwake:
    """A fake Popen for the relay's sleep assertion: records argv, and whether it was ended."""
    made: list = []

    def __init__(self, argv, **kw):
        self.argv, self.kw, self.ended = argv, kw, False
        _FakeAwake.made.append(self)

    def terminate(self):
        self.ended = True

    def wait(self, timeout=None):
        return 0


def _relay_run(tmp_path, args, platform, spawn=_FakeAwake):
    import os
    from ttp import bridge_relay
    _FakeAwake.made = []
    argv = _fake_relay_ssh(tmp_path / "s", ["ok"])
    rc = bridge_relay.main([*args, str(tmp_path / "relay" / "box-a.auto"), "--", *argv],
                           platform=platform, awake_spawn=spawn)
    return rc, list(_FakeAwake.made), os.getpid()


def test_bridge_relay_on_macos_holds_exactly_one_charger_only_assertion_and_ends_it(tmp_path):
    # cells: keep-awake-darwin, keep-awake-argv, keep-awake-restart
    rc, made, pid = _relay_run(tmp_path, ["--tag", "box-a/auto", "--keep-awake"], "darwin")
    assert rc == 0 and len(made) == 1
    assert made[0].argv == ["/usr/bin/caffeinate", "-s", "-w", str(pid)]
    assert not {"-d", "-i", "-u", "-m"} & set(made[0].argv)
    assert made[0].ended, "the assertion ends with the relay"
    # a restart is a new relay process: the old one's assertion is already ended, so one is held at a time
    rc2, made2, _ = _relay_run(tmp_path / "again", ["--tag", "box-a/auto", "--keep-awake"], "darwin")
    assert rc2 == 0 and len(made2) == 1 and made2[0].ended


def test_bridge_relay_spawns_no_assertion_when_off_or_not_on_macos(tmp_path):
    # cells: keep-awake-off, keep-awake-other-platform
    rc, made, _ = _relay_run(tmp_path / "a", ["--tag", "box-a/auto"], "darwin")   # the flag is absent: setting off
    assert rc == 0 and made == []
    for plat in ("linux", "win32"):
        rc, made, _ = _relay_run(tmp_path / plat, ["--tag", "box-a/auto", "--keep-awake"], plat)
        assert rc == 0 and made == []


def test_bridge_relay_survives_a_missing_caffeinate(tmp_path, capfd):
    # cells: keep-awake-missing
    def boom(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory")
    rc, made, _ = _relay_run(tmp_path, ["--keep-awake"], "darwin", spawn=boom)
    assert rc == 0 and "could not keep the computer awake" in capfd.readouterr().err


def test_bridge_keep_awake_setting_drives_the_service_command_and_the_status_line(tmp_path, monkeypatch):
    # cells: keep-awake-setting, keep-awake-status
    import pytest
    from ttp import bridge
    box = {"agents": {"auto": {"agent": "env", "forward": [
        {"name": "build-a", "target": "build-a.example.com", "port": 2301, "user": "builder"}]}}}

    def cfg(**top):
        return bridge.parse({"boxes": {"box-1": box}, **top})
    assert cfg()["keep_awake"] is True and cfg(keep_awake="off")["keep_awake"] is False
    assert cfg(keep_awake=False)["keep_awake"] is False and cfg(keep_awake="ON")["keep_awake"] is True
    with pytest.raises(bridge.BadConfig, match="keep_awake"):
        cfg(keep_awake="sometimes")
    assert "--keep-awake" in bridge.service_argv(cfg(), "box-1", "auto", platform="darwin")
    assert "--keep-awake" not in bridge.service_argv(cfg(keep_awake="off"), "box-1", "auto", platform="darwin")
    assert "--keep-awake" not in bridge.service_argv(cfg(), "box-1", "auto", platform="linux")
    # the status line: not on macOS / off by setting / not held yet / held
    assert bridge.keep_awake_line(cfg(), ["box-1"], "linux") == "keep-awake: not on macOS (nothing is held)"
    assert bridge.keep_awake_line(cfg(keep_awake="off"), ["box-1"], "darwin").startswith("keep-awake: off by setting")
    monkeypatch.setattr(bridge, "running", lambda *a, **k: True)
    monkeypatch.setattr(bridge, "installed_argv", lambda *a, **k: ["py", "relay"])
    assert "not held now" in bridge.keep_awake_line(cfg(), ["box-1"], "darwin")
    monkeypatch.setattr(bridge, "installed_argv", lambda *a, **k: ["py", "relay", "--keep-awake"])
    line = bridge.keep_awake_line(cfg(), ["box-1"], "darwin")
    assert line == "keep-awake: held (charger power only; the display may sleep)"
    # status() ends with that one line
    import subprocess
    monkeypatch.setattr(bridge, "_box", lambda *a, **k: subprocess.CompletedProcess(
        [], 0, "block yes\nagent auto up 1\nport 2301 up\n", ""))
    lines, _ = bridge.status(cfg(), ["box-1"], "darwin")
    assert lines[-1] == line and sum(ln.startswith("keep-awake:") for ln in lines) == 1


def test_daemon_keep_awake_never_holds_the_display_or_the_battery():
    # cells: daemon-awake-pin
    from ttp import awake
    a = awake.argv(4242)
    assert a[0] == "caffeinate" and not {"-d", "-u", "-m"} & set(a) and "-s" in a and a[-2:] == ["-w", "4242"]


def _unbounded_ssh_adds(root):
    """Each `ssh-add` the plugin runs or ships with no bound: a subprocess call without `timeout=`, or a
    shell command string (ssh-add after SSH_AUTH_SOCK=, `$(`, a separator or at its start, with shell syntax
    in the string) not run under `timeout` (`$t ssh-add`, `timeout N ssh-add`) or a kill watchdog
    (`ssh-add ... & p=$!`). Docstrings, comments, messages and bare names (deny lists) are text."""
    import ast
    import re
    cmd = re.compile(r"(?:SSH_AUTH_SOCK=\S*\s+|\$\(\s*|(?:^|[;&|(])\s*)(?:\$t\s+|timeout \d+\s+)?ssh-add\b")
    bounded = re.compile(r"(\$t|timeout \d+)\s+ssh-add|ssh-add[^;|]*&\s*p=\$!")
    shell = re.compile(r"[$|>;&]")
    bad = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix not in (".py", ".sh", "") or "__pycache__" in f.parts:
            continue
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if "ssh-add" not in text:
            continue
        if f.suffix != ".py" and not text.startswith("#!"):
            continue
        if f.suffix != ".py":
            for n, ln in enumerate(text.splitlines(), 1):
                code = ln.split("#", 1)[0] if not ln.lstrip().startswith("#") else ""
                if cmd.search(code) and not bounded.search(code):
                    bad.append(f"{f.relative_to(root)}:{n}")
            continue
        tree = ast.parse(text)
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        parts = {id(x) for j in ast.walk(tree) if isinstance(j, ast.JoinedStr) for x in j.values}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                first = n.args[0] if n.args else None
                if isinstance(first, (ast.List, ast.Tuple)) and first.elts and \
                        isinstance(first.elts[0], ast.Constant) and first.elts[0].value == "ssh-add" and \
                        not any(k.arg == "timeout" for k in n.keywords):
                    bad.append(f"{f.relative_to(root)}:{n.lineno}")
            elif isinstance(n, ast.JoinedStr) or isinstance(n, ast.Constant) and isinstance(n.value, str) \
                    and id(n) not in docs and id(n) not in parts:
                v = n.value if isinstance(n, ast.Constant) else "".join(
                    x.value if isinstance(x, ast.Constant) else "{}" for x in n.values)
                if cmd.search(v) and shell.search(v) and not bounded.search(v):
                    bad.append(f"{f.relative_to(root)}:{n.lineno}")
    return bad


def test_every_ssh_add_the_plugin_runs_or_ships_is_bounded_so_a_dead_forward_never_hangs_it(tmp_path):
    # cells: sshadd_bounded, sshadd_guard_test
    from ttp import bridge
    plugin = RUNTIME.parent
    assert _unbounded_ssh_adds(plugin) == [], "bound it: subprocess timeout=, `timeout 10` (with a fallback) or a watchdog"
    script = bridge.probe_script(_bridge_cfg(), "box-1")
    assert 't="timeout 10"' in script and "$t ssh-add -l" in script, "the bridge's box probe is bounded too"
    (tmp_path / "x.py").write_text('import subprocess\nsubprocess.run(["ssh-add", "-l"])\n'
                                   'CMD = "SSH_AUTH_SOCK=$s ssh-add -l | grep -c SHA256"\n'
                                   'OK = "timeout 10 ssh-add -l"\nNOTE = "load keys with `ssh-add`"\n')
    (tmp_path / "y").write_text("#!/bin/sh\nn=$(ssh-add -l)   # count keys\n# ssh-add -l in a comment\n")
    assert _unbounded_ssh_adds(tmp_path) == ["x.py:2", "x.py:3", "y:2"], "the guard catches what it should"


def _port_holder(argv0):
    """A real local process of this test listening on a free 127.0.0.1 port and accepting (then closing) each
    connection, named argv0: (process, port)."""
    import shlex
    import socket
    import subprocess
    import sys
    import time
    code = ("import socket, sys; s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(8); "
            "print(s.getsockname()[1], flush=True)\nwhile True: s.accept()[0].close()")
    p = subprocess.Popen(["bash", "-c", f'exec -a {shlex.quote(argv0)} {shlex.quote(sys.executable)} -c {shlex.quote(code)}'],
                         stdout=subprocess.PIPE, text=True)
    port = int(p.stdout.readline())
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            time.sleep(0.1)
    return p, port


def _hidden_cleanup(tmp_path, port):
    """The box cleanup with ss hiding every listener's pid (as for this account's sshd sessions): its stdout lines."""
    import os
    import shutil
    import subprocess
    from ttp import bridge_relay
    real = shutil.which("ss")
    if not real:
        import pytest
        pytest.skip("no ss here")
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "ss").write_text(f"#!/bin/sh\n{real} \"$@\" | sed 's/ users:.*//'\n")
    (bin_ / "ss").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", HOME=str(tmp_path), XDG_CACHE_HOME=str(tmp_path / "cache"))
    r = subprocess.run(["sh", "-c", bridge_relay.CLEANUP, "x", "ttp-bridge-tag=aaaaaaaaaaaa/watch/",
                        "bridge-aaaaaaaaaaaa+watch+", "~/links/none.sock", str(port)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def test_bridge_cleanup_leaves_every_session_alone_when_ss_hides_the_owner(tmp_path):
    # cells: hidden-owner-untouched, no-signal-to-others
    # This account's sshd sessions are not dumpable, so ss shows no pid for their forwards: the owner cannot be
    # identified, so no session is ended, paused or signalled, and the port is reported held.
    sshd, port = _port_holder("sshd: u@notty")
    bystander, _ = _port_holder("sshd: u@notty")
    try:
        out = _hidden_cleanup(tmp_path, port)
        assert out == [f"held {port} ?"], out
        for p in (sshd, bystander):
            assert p.poll() is None
            with open(f"/proc/{p.pid}/stat") as f:
                assert f.read().split(") ", 1)[1].split()[0] != "T"
    finally:
        for p in (sshd, bystander):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_bridge_cleanup_sends_only_term_to_the_identified_owner(tmp_path):
    # cells: owner-ended, bystander-untouched
    import os
    import subprocess
    from ttp import bridge_relay
    owner, port = _port_holder("sshd: u@notty")
    bystander, _ = _port_holder("sshd: u@notty")
    try:
        env = dict(os.environ, HOME=str(tmp_path), XDG_CACHE_HOME=str(tmp_path / "cache"))
        r = subprocess.run(["sh", "-c", bridge_relay.CLEANUP, "x", "ttp-bridge-tag=aaaaaaaaaaaa/watch/",
                            "bridge-aaaaaaaaaaaa+watch+", "~/links/none.sock", str(port)],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        assert owner.wait(timeout=15) == -15, r.stdout   # killed by SIGTERM, nothing else
        assert bystander.poll() is None
        with open(f"/proc/{bystander.pid}/stat") as f:
            assert f.read().split(") ", 1)[1].split()[0] != "T"
    finally:
        for p in (owner, bystander):
            if p.poll() is None:
                p.kill()
                p.wait()


def _watch_env(tmp_path, monkeypatch, platform, answers, loaded=True, watch=None, agents=True):
    """A watcher with a fake box (answers: the check's stdout per call, None for an unreachable box), a fake
    launchctl/systemctl (loaded: the service is meant to run) and a fake release: the calls it saw."""
    import json
    import plistlib
    import subprocess
    from ttp import bridge, bridge_watch, hoststate
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(bridge, "HOME_DIR", tmp_path / "ttp")
    monkeypatch.setattr(hoststate, "unit_dir", lambda: tmp_path / "units")
    monkeypatch.setattr(bridge_watch, "NET_PATHS", {"darwin": [str(tmp_path / "netconf")]})
    data = {"boxes": {"box-1": {"login": {}, "agents": {"auto": {"agent": "env", "forward": [
        {"name": "build-a", "target": "build-a.example.com", "port": 2301}]}}}}}
    if watch:
        data["watch"] = watch
    cfg = bridge.parse(json.loads(json.dumps(data)))
    if agents:
        f = bridge.service_file("box-1", "auto", platform)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(plistlib.dumps({"Label": "x"}) if platform == "darwin" else b"[Service]\n")
    seen = {"box": [], "run": [], "release": []}
    answers = list(answers)

    def box(b, script, timeout=40, inp=None):
        seen["box"].append(b)
        a = answers.pop(0)
        return subprocess.CompletedProcess([], 255 if a is None else 0, a or "", "" if a else "no route to host")

    def run(argv, timeout=60, inp=None):
        seen["run"].append(" ".join(argv))
        if argv[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(argv, 0 if loaded else 113, "", "")
        if argv[1:3] == ["--user", "is-active"]:
            state = loaded if isinstance(loaded, str) else "active" if loaded else "inactive"
            return subprocess.CompletedProcess(argv, 0 if state == "active" else 3, state + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(bridge, "_box", box)
    monkeypatch.setattr(bridge, "_run", run)
    monkeypatch.setattr(bridge_watch, "dest_answers", lambda d: True)
    monkeypatch.setattr(bridge_watch, "release",
                        lambda cfg, t, ports: seen["release"].append((t["service"], t["agent"], ports)) or True)
    return cfg, seen


def test_bridge_watch_offline_restarts_nothing_and_backs_off(tmp_path, monkeypatch):
    # cells: offline-no-restart, backoff, network-change
    import os
    from ttp import bridge_watch
    cfg, seen = _watch_env(tmp_path, monkeypatch, "darwin", [None, None, None])
    t0 = 1_000_000.0
    out = bridge_watch.run_pass(cfg, "darwin", t0)
    assert seen["release"] == [] and not any("kickstart" in c for c in seen["run"])
    assert out == ["box-1: not reachable; no restart, next check in 30s or on a network change"]
    assert bridge_watch.run_pass(cfg, "darwin", t0 + 20) == [] and len(seen["box"]) == 1   # backing off
    bridge_watch.run_pass(cfg, "darwin", t0 + 31)
    assert len(seen["box"]) == 2 and bridge_watch.run_pass(cfg, "darwin", t0 + 61) == []
    assert len(seen["box"]) == 2   # the second miss waits 60s
    (tmp_path / "netconf").write_text("x")   # a network change checks at once
    os.utime(tmp_path / "netconf", (t0, t0))
    bridge_watch.run_pass(cfg, "darwin", t0 + 62)
    os.utime(tmp_path / "netconf", (t0 + 63, t0 + 63))
    bridge_watch.run_pass(cfg, "darwin", t0 + 63)
    assert len(seen["box"]) == 3 and seen["release"] == []
    assert not any("kickstart" in c or "restart" in c for c in seen["run"])
    assert max(bridge_watch.BACKOFF_S) <= 60   # the pause never passes a minute
    cfg, seen = _watch_env(tmp_path / "c", monkeypatch, "linux", [None] * 6)
    for i in range(6):
        bridge_watch.run_pass(cfg, "linux", t0 + 61 * i)   # every pass lands just after the allowed wait
    assert len(seen["box"]) == 6


def test_bridge_watch_frees_the_stale_port_restarts_and_heals_within_60s(tmp_path, monkeypatch):
    # cells: stale-freed-then-restarted, heal-under-60s, heal-log, stopped-left-alone
    import json
    import os
    from ttp import bridge, bridge_watch
    cfg, seen = _watch_env(tmp_path, monkeypatch, "darwin",
                           ["up 2301\nttp-watch-done\n", "down 2301\nttp-watch-done\n", "up 2301\nttp-watch-done\n"])
    t0 = 1_000_000.0
    assert bridge_watch.run_pass(cfg, "darwin", t0 - 600) == []
    out = bridge_watch.run_pass(cfg, "darwin", t0)   # the first pass after a sleep: the forward is stale
    svc = bridge.label("box-1", "auto")
    assert seen["release"] == [(svc, "auto", [2301])]
    assert seen["run"][-1] == f"launchctl kickstart -k gui/{os.getuid()}/{svc}"
    assert bridge.bridge_marker("box-1", "auto").is_file()   # the restart skips the random start delay
    assert out == [f"{svc}: port 2301 on box-1 did not answer; restarted"]
    out = bridge_watch.run_pass(cfg, "darwin", t0 + bridge_watch.INTERVAL_S)   # the next default pass
    assert out == [f"{svc}: answers again after 30s"]
    rec = json.loads((tmp_path / "ttp" / "bridge-heal.log").read_text())
    assert rec["event"] == "" and rec["restarts"] == 1 and rec["seconds"] <= 60
    lines = bridge_watch.heal_lines("darwin")
    assert lines[0].startswith("self-heal watch: not installed") and " after a " not in lines[1]
    assert "answering again 30s later" in lines[1]
    # a session `ttp bridge stop` stopped is left stopped
    cfg, seen = _watch_env(tmp_path / "b", monkeypatch, "darwin", ["down 2301\nttp-watch-done\n"], loaded=False)
    assert bridge_watch.run_pass(cfg, "darwin", t0) == [] and seen["release"] == []
    assert not any("kickstart" in c for c in seen["run"])


def test_bridge_watch_leaves_a_session_alone_when_a_forwards_destination_is_down(tmp_path, monkeypatch):
    # cells: target-down-no-restart, target-up-heals, no-listener-heals
    from ttp import bridge_watch
    down = "down 2301\nttp-watch-done\n"
    cfg, seen = _watch_env(tmp_path, monkeypatch, "darwin", [down, down])
    answered = []
    monkeypatch.setattr(bridge_watch, "dest_answers", lambda d: answered.append(d) or False)
    t0 = 1_000_000.0
    out = bridge_watch.run_pass(cfg, "darwin", t0)
    assert [h["target"] for h in answered] == ["build-a.example.com"] and seen["release"] == []
    assert not any("kickstart" in c for c in seen["run"])
    assert len(out) == 1 and "destination does not answer" in out[0]
    assert bridge_watch.run_pass(cfg, "darwin", t0 + 300) == [] and seen["release"] == []   # logged once
    cfg, seen = _watch_env(tmp_path / "b", monkeypatch, "darwin", [down])
    monkeypatch.setattr(bridge_watch, "dest_answers", lambda d: True)
    out = bridge_watch.run_pass(cfg, "darwin", t0)
    assert len(seen["release"]) == 1 and any("kickstart" in c for c in seen["run"]) and out[0].endswith("restarted")
    # no listener on the box at all: the session is dead whatever its target does
    cfg, seen = _watch_env(tmp_path / "c", monkeypatch, "darwin", ["none 2301\nttp-watch-done\n"])
    monkeypatch.setattr(bridge_watch, "dest_answers", lambda d: False)
    out = bridge_watch.run_pass(cfg, "darwin", t0)
    assert len(seen["release"]) == 1 and any("kickstart" in c for c in seen["run"]) and out[0].endswith("restarted")


def test_bridge_watch_leaves_a_live_session_alone_when_one_forward_is_slow(tmp_path, monkeypatch):
    # cells: live-session-slow-port-no-restart, all-silent-restarts, coming-up-not-judged-again, live-target-off-logged
    from ttp import bridge, bridge_watch
    cfg, seen = _watch_env(tmp_path, monkeypatch, "darwin", ["up 2301\ndown 2302\nttp-watch-done\n",
                           "down 2301\ndown 2302\nttp-watch-done\n", "none 2301\nnone 2302\nttp-watch-done\n"])
    cfg = bridge.parse({"boxes": {"box-1": {"login": {}, "agents": {"auto": {"agent": "env", "forward": [
        {"name": "build-a", "target": "build-a.example.com", "port": 2301},
        {"name": "build-b", "target": "build-b.example.com", "port": 2302}]}}}}})
    asked = []
    monkeypatch.setattr(bridge_watch, "dest_answers", lambda d: asked.append(d) or True)
    t0, svc = 1_000_000.0, bridge.label("box-1", "auto")
    # one forward answers, so the session is live: the other's banner is only slow, and a restart would drop both
    assert bridge_watch.run_pass(cfg, "darwin", t0) == [] and seen["release"] == []
    assert [h["target"] for h in asked] == ["build-b.example.com"]
    assert not any("kickstart" in c for c in seen["run"])
    out = bridge_watch.run_pass(cfg, "darwin", t0 + 30)   # every forward silent: a stale session
    assert seen["release"] == [(svc, "auto", [2301, 2302])] and out[0].endswith("restarted")
    # still coming up after its restart (no listener yet): not judged again before RESTART_GAP_S
    assert bridge_watch.run_pass(cfg, "darwin", t0 + 60) == [] and len(seen["release"]) == 1
    # a live session's silent port whose target is off is logged once, as for any session
    two = cfg
    cfg, seen = _watch_env(tmp_path / "b", monkeypatch, "darwin", ["up 2301\ndown 2302\nttp-watch-done\n"])
    monkeypatch.setattr(bridge_watch, "dest_answers", lambda d: False)
    out = bridge_watch.run_pass(two, "darwin", t0)
    assert seen["release"] == [] and len(out) == 1 and "port 2302" in out[0] and "destination does not" in out[0]


def test_bridge_watch_spares_a_session_still_logging_in(tmp_path, monkeypatch):
    # cells: young-login-spared, old-login-restarted, unknown-age-restarted, systemd-main-pid, etime-days
    import json
    import subprocess
    from ttp import bridge, bridge_watch
    none = "none 2301\nttp-watch-done\n"
    for platform, show in (("darwin", "\tstate = running\n\tpid = 4242\n"), ("linux", "4242\n")):
        cfg, seen = _watch_env(tmp_path / platform, monkeypatch, platform, [none] * 4)
        fake, ps = bridge._run, {"pgrep": "4250\n", "etime": "00:08\n"}

        def run(argv, timeout=60, inp=None, fake=fake, ps=ps, show=show):
            if argv[:2] == ["launchctl", "print"] or argv[1:4] == ["--user", "show", "-p"]:
                seen["run"].append(" ".join(argv))
                return subprocess.CompletedProcess(argv, 0, show, "")
            if argv[0] == "pgrep":
                assert argv[-2:] == ["4242", "ssh"]   # the service's own ssh child, by exact name
                return subprocess.CompletedProcess(argv, 0 if ps["pgrep"] else 1, ps["pgrep"], "")
            if argv[0] == "ps":
                assert argv[-1] == "4250"
                return subprocess.CompletedProcess(argv, 0, ps["etime"], "")
            return fake(argv, timeout, inp)

        monkeypatch.setattr(bridge, "_run", run)
        t0, svc = 1_000_000.0, bridge.label("box-1", "auto")
        # its service manager started it 8 s ago: no listener yet is a login still under way, not a stale session
        assert bridge_watch.run_pass(cfg, platform, t0 - 30) == [f"{svc}: still logging in (8s); not restarted this pass"]
        assert seen["release"] == [] and not any("kickstart" in c or "restart --" in c for c in seen["run"])
        assert not json.loads((bridge.HOME_DIR / bridge_watch.STATE).read_text())["fail"]   # no failure: no heal later
        ps["etime"] = "10:00\n"   # a session up 10 min with no listener is stale: restarted on the first pass
        out = bridge_watch.run_pass(cfg, platform, t0)
        assert seen["release"] == [(svc, "auto", [2301])] and out[0].endswith("restarted")
        ps["etime"] = "00:08\n"   # the restarted session logging in is spared too, once its restart gap has passed
        t1 = t0 + bridge_watch.RESTART_GAP_S[1]
        assert bridge_watch.run_pass(cfg, platform, t1) == [f"{svc}: still logging in (8s); not restarted this pass"]
        assert len(seen["release"]) == 1
        ps["etime"] = "1-02:03:04\n"
        assert bridge_watch.login_age({"service": svc}, platform) == 93784
        ps["etime"] = "garbage\n"
        assert bridge_watch.login_age({"service": svc}, platform) is None
        # no ssh yet (its random start wait) or an unreadable age: restarted as before, once its gap allows
        ps["pgrep"] = ""
        out = bridge_watch.run_pass(cfg, platform, t1 + 30)
        assert len(seen["release"]) == 2 and out[0].endswith("restarted")


def test_bridge_watch_dest_answers_by_the_route_the_relay_takes(monkeypatch):
    # cells: direct-target, interface-target
    import socket
    from ttp import bridge_relay, bridge_watch
    calls = []

    class Conn:
        def close(self):
            calls.append("closed")

    def direct(addr, timeout):
        calls.append(("direct", addr, timeout))
        return Conn()

    def bound(host, iface, port=22, timeout=None):
        calls.append(("bound", host, iface, port, timeout))
        raise OSError("no route")

    monkeypatch.setattr(socket, "create_connection", direct)
    monkeypatch.setattr(bridge_relay, "bound_connect", bound)
    assert bridge_watch.dest_answers({"target": "h1", "interface": ""})
    assert not bridge_watch.dest_answers({"target": "h2", "interface": "en9"})
    assert bridge_watch.dest_answers({"target": "h3", "to": 8022, "interface": ""})   # a watch entry's target port
    assert calls == [("direct", ("h1", 22), 5), "closed", ("bound", "h2", "en9", 22, 5),
                     ("direct", ("h3", 8022), 5), "closed"]


def test_bridge_watch_check_tells_a_byte_from_silence_from_no_listener(tmp_path):
    # cells: check-up, check-down, check-none, check-parallel
    import shutil
    import socket
    import subprocess
    import threading
    import time
    import pytest
    from ttp import bridge_watch
    if not (shutil.which("bash") and shutil.which("timeout")):
        pytest.skip("no bash or timeout here")
    talk, closed, quiet = socket.socket(), socket.socket(), [socket.socket() for _ in range(10)]
    try:
        for sk in (talk, closed, *quiet):
            sk.bind(("127.0.0.1", 0))
        for sk in (talk, *quiet):
            sk.listen(4)
        up, none, down = talk.getsockname()[1], closed.getsockname()[1], [sk.getsockname()[1] for sk in quiet]
        closed.close()
        threading.Thread(target=lambda: talk.accept()[0].sendall(b"SSH-2.0"), daemon=True).start()
        t = time.monotonic()
        r = subprocess.run(["sh", "-c", bridge_watch.CHECK, "x", *map(str, [up, *down, none])], capture_output=True,
                           text=True, timeout=30)
        took = time.monotonic() - t
        lines = r.stdout.splitlines()
        assert lines[-1] == "ttp-watch-done" and sorted(lines[:-1]) == sorted(
            [f"up {up}", f"none {none}", *(f"down {p}" for p in down)]), r.stdout
        assert took < 15   # ten silent ports take one probe's 6 s, far inside the box login's 40 s, not 60 s
    finally:
        for sk in (talk, closed, *quiet):
            sk.close()


def test_bridge_watch_resets_a_failed_systemd_unit_before_restarting_it(tmp_path, monkeypatch):
    # cells: failed-reset-then-restart
    from ttp import bridge, bridge_watch
    cfg, seen = _watch_env(tmp_path, monkeypatch, "linux", ["down 2301\nttp-watch-done\n"], loaded="failed")
    out = bridge_watch.run_pass(cfg, "linux", 1_000_000.0)
    unit = bridge.label("box-1", "auto") + ".service"
    assert seen["run"][-2:] == [f"systemctl --user reset-failed -- {unit}", f"systemctl --user restart -- {unit}"]
    assert out[0].endswith("restarted")


def test_bridge_watch_heals_a_listed_hand_made_service(tmp_path, monkeypatch):
    # cells: hand-made-healed, restart-paced, watch-config
    import pytest
    from ttp import bridge, bridge_watch
    watch = [{"label": "my-forward", "box": "box-2", "port": 2400}]
    cfg, seen = _watch_env(tmp_path, monkeypatch, "linux", ["down 2400\nttp-watch-done\n"] * 3, watch=watch,
                           agents=False)
    t0 = 1_000_000.0
    out = bridge_watch.run_pass(cfg, "linux", t0)
    assert seen["box"] == ["box-2"] and seen["release"] == [("my-forward", "", [2400])]
    assert seen["run"][-1] == "systemctl --user restart -- my-forward.service" and out[0].endswith("restarted")
    bridge_watch.run_pass(cfg, "linux", t0 + 30)   # still down, but just restarted: no second restart yet
    assert len(seen["release"]) == 1
    for bad in ([{"label": "x y", "box": "b", "port": 2400}], [{"label": "x", "box": "b", "port": 22}],
                [{"label": "x", "box": "b"}], {"x": 1}, [{"label": "x", "box": "b", "port": 2400, "target": "h"}],
                [{"label": "x", "box": "b", "port": 2400, "target": "-h:22"}],
                [{"label": "x", "box": "b", "port": 2400, "target": "h:22", "interface": "e n"}],
                [{"label": "x", "box": "b", "port": 2400, "interface": "en9"}],
                [{"label": "x", "box": "b", "port": 2400, "extra": 1}]):
        with pytest.raises(bridge.BadConfig):
            bridge.parse({"boxes": {"b": {"login": {}, "agents": {"auto": {"agent": "env", "forward": [{"name": "h", "target": "h.example.com", "port": 2301}]}}}}, "watch": bad})
    assert bridge.parse({"boxes": {"b": {"login": {}, "agents": {"auto": {"agent": "env", "forward": [{"name": "h", "target": "h.example.com", "port": 2301}]}}}}})["watch"] == []


def test_bridge_watch_hand_made_service_without_a_target_is_restarted_once_while_it_stays_silent(tmp_path, monkeypatch):
    # cells: no-target-one-heal, logged-once, heals-again-on-change, held-across-sleep
    from ttp import bridge_watch
    watch = [{"label": "my-forward", "box": "box-2", "port": 2400}]
    down, none = "down 2400\nttp-watch-done\n", "none 2400\nttp-watch-done\n"
    cfg, seen = _watch_env(tmp_path, monkeypatch, "linux", [down, down, down, none, down, down, down, down],
                           watch=watch, agents=False)
    t0 = 1_000_000.0
    assert bridge_watch.run_pass(cfg, "linux", t0)[0].endswith("restarted") and len(seen["release"]) == 1
    out = bridge_watch.run_pass(cfg, "linux", t0 + 60)   # still silent: it may be a live forward whose target is off
    assert len(seen["release"]) == 1 and len(out) == 1 and "names no target" in out[0]
    assert bridge_watch.run_pass(cfg, "linux", t0 + 120) == [] and len(seen["release"]) == 1   # logged once
    bridge_watch.run_pass(cfg, "linux", t0 + 180)   # its check changed: no listener now, so it is healed again
    assert len(seen["release"]) == 2
    bridge_watch.run_pass(cfg, "linux", t0 + 240)   # changed back to silent: one more heal, paced, then it waits
    bridge_watch.run_pass(cfg, "linux", t0 + 330)
    bridge_watch.run_pass(cfg, "linux", t0 + 400)
    assert len(seen["release"]) == 3
    bridge_watch.run_pass(cfg, "linux", t0 + 700)   # a gap like a sleep changes nothing: still held
    assert len(seen["release"]) == 3
    assert sum(c == "systemctl --user restart -- my-forward.service" for c in seen["run"]) == 3


def test_bridge_watch_takes_no_decision_from_the_time_between_passes(tmp_path, monkeypatch):
    # cells: slow-pass-keeps-backoff, slow-pass-keeps-hold, sleep-heals-first-pass, network-change-lifts-hold, caps
    import os
    from ttp import bridge_watch
    cfg, seen = _watch_env(tmp_path, monkeypatch, "darwin", [None, None, None])
    t0 = 1_000_000.0
    bridge_watch.run_pass(cfg, "darwin", t0)
    bridge_watch.run_pass(cfg, "darwin", t0 + 31)   # second miss: next check 60s later
    assert len(seen["box"]) == 2
    # a pass 50s later (after a slow one) is still inside the 60s wait: no early check
    assert bridge_watch.run_pass(cfg, "darwin", t0 + 31 + 50) == [] and len(seen["box"]) == 2
    bridge_watch.run_pass(cfg, "darwin", t0 + 31 + 50 + 600)   # the first pass after a sleep asks the box
    assert len(seen["box"]) == 3
    # a hand-made forward with no target stays held across the same gaps; a network change lifts it once
    down = "down 2400\nttp-watch-done\n"
    cfg, seen = _watch_env(tmp_path / "b", monkeypatch, "darwin", [down] * 6,
                           watch=[{"label": "my-forward", "box": "box-2", "port": 2400}], agents=False)
    bridge_watch.run_pass(cfg, "darwin", t0)
    for t in (t0 + 60, t0 + 150, t0 + 245):   # slow passes and long gaps
        bridge_watch.run_pass(cfg, "darwin", t)
    assert len(seen["release"]) == 1
    (tmp_path / "b" / "netconf").write_text("x")
    os.utime(tmp_path / "b" / "netconf", (t0 + 300, t0 + 300))
    bridge_watch.run_pass(cfg, "darwin", t0 + 300)   # the first stamp only records the network state
    assert len(seen["release"]) == 1
    os.utime(tmp_path / "b" / "netconf", (t0 + 330, t0 + 330))
    out = bridge_watch.run_pass(cfg, "darwin", t0 + 330)
    assert len(seen["release"]) == 2 and out[0].endswith("restarted")
    assert max(bridge_watch.BACKOFF_S) <= 60 and not hasattr(bridge_watch, "WAKE_GAP_S")


def test_bridge_watch_hand_made_service_with_a_target_heals_only_when_the_target_answers(tmp_path, monkeypatch):
    # cells: watch-target-down-left-alone, watch-target-up-heals, watch-target-config
    from ttp import bridge, bridge_watch
    watch = [{"label": "my-forward", "box": "box-2", "port": 2400, "target": "host-9.example.com:8022",
              "interface": "en9"}]
    down = "down 2400\nttp-watch-done\n"
    cfg, seen = _watch_env(tmp_path, monkeypatch, "linux", [down, down], watch=watch, agents=False)
    assert cfg["watch"][0] == {"label": "my-forward", "box": "box-2", "port": 2400,
                                    "target": "host-9.example.com", "to": 8022, "interface": "en9"}
    asked = []
    monkeypatch.setattr(bridge_watch, "dest_answers", lambda d: asked.append(d) or False)
    t0 = 1_000_000.0
    out = bridge_watch.run_pass(cfg, "linux", t0)
    assert asked == [{"target": "host-9.example.com", "to": 8022, "interface": "en9"}]
    assert seen["release"] == [] and not any("restart" in c for c in seen["run"])
    assert len(out) == 1 and "destination does not answer" in out[0]
    assert bridge_watch.run_pass(cfg, "linux", t0 + 60) == [] and seen["release"] == []   # logged once
    cfg, seen = _watch_env(tmp_path / "b", monkeypatch, "linux", [down, down], watch=watch, agents=False)
    monkeypatch.setattr(bridge_watch, "dest_answers", lambda d: True)
    assert bridge_watch.run_pass(cfg, "linux", t0)[0].endswith("restarted")
    assert seen["release"] == [("my-forward", "", [2400])]
    bridge_watch.run_pass(cfg, "linux", t0 + 60)   # its target answers, so a silent port is stale: healed again
    assert len(seen["release"]) == 2
    assert bridge.parse({"boxes": {"b": {"login": {}, "agents": {"auto": {"agent": "env", "forward": [{"name": "h", "target": "h.example.com", "port": 2301}]}}}}, "watch": [{"label": "m", "box": "b", "port": 2400, "target": "10.0.0.5:22"}]})[
        "watch"][0]["interface"] == ""


def test_bridge_watch_service_runs_every_interval_and_on_network_change(tmp_path, monkeypatch):
    # cells: launchd-plist, systemd-timer
    import plistlib
    from ttp import bridge_watch
    monkeypatch.setenv("HOME", str(tmp_path))
    plist = plistlib.loads(bridge_watch.contents("darwin")[0])
    assert plist["StartInterval"] <= 30 and plist["WatchPaths"] == ["/Library/Preferences/SystemConfiguration"]
    assert plist["ProgramArguments"][-2:] == ["bridge", "watch"] and "KeepAlive" not in plist
    svc, timer = (c.decode() for c in bridge_watch.contents("linux"))
    assert "Type=oneshot" in svc and "bridge watch" in svc and "OnUnitActiveSec=30" in timer


def test_bridge_watch_release_runs_the_box_cleanup_for_only_the_dead_ports(tmp_path, monkeypatch):
    # cells: release-argv
    import json
    from ttp import bridge, bridge_relay, bridge_watch
    monkeypatch.setattr(bridge, "HOME_DIR", tmp_path)
    got = []
    monkeypatch.setattr(bridge_relay, "clean", lambda ssh, prefix, ports, link="": got.append((ssh, prefix, ports)) or True)
    cfg = bridge.parse(json.loads(json.dumps({"boxes": {"box-1": {"login": {}, "agents": {"auto": {"agent": "env", "forward": [
        {"name": "build-a", "target": "build-a.example.com", "port": 2301}]}}}}})))
    assert bridge_watch.release(cfg, {"service": "s", "box": "box-1", "ports": [2301], "agent": "auto"}, [2301])
    assert bridge_watch.release(cfg, {"service": "m", "box": "box-2", "ports": [2400], "agent": ""}, [2400])
    (ssh1, p1, ports1), (ssh2, p2, ports2) = got
    cid = bridge_relay.computer_id(str(bridge.relay_dir("box-1", "auto")))
    assert p1 == f"{cid}/box-1/auto/" and ports1 == [2301] and "-R" in ssh1   # the session's own login options
    assert p2 == f"{cid}/watch/" and ports2 == [2400] and ssh2[-2:] == ["box-2", "true"] and "-R" not in ssh2
