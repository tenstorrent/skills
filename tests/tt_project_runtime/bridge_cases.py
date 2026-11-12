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
            assert h["target"].endswith(".example.com") or h["target"].startswith("10.0.0.")


def test_bridge_service_runs_the_session_and_restarts_it(tmp_path, monkeypatch):
    from ttp import bridge
    monkeypatch.setenv("HOME", str(tmp_path))
    argv = bridge.ssh_argv(_bridge_cfg(), "box-1", "auto", ssh="ssh")
    pl = bridge.launchd_plist("box-1", "auto", argv)
    assert pl["Label"] == "com.tt-project.bridge.box-1.auto" and pl["KeepAlive"] and pl["ProgramArguments"] == argv
    unit = bridge.systemd_unit("box-1", "auto", argv)
    assert "Restart=always" in unit and "$$SSH_AUTH_SOCK" in unit   # systemd would expand a bare $VAR


def test_bridge_interface_hosts_go_through_the_relay_socket(tmp_path, monkeypatch):
    from ttp import bridge
    monkeypatch.setattr(bridge, "HOME_DIR", tmp_path)
    monkeypatch.setattr(bridge, "RELAY", tmp_path / "bridge_relay.py")
    cfg = _bridge_cfg()
    cfg["boxes"]["box-1"]["agents"]["auto"]["forward"][0]["interface"] = "vpn0"
    argv = bridge.ssh_argv(cfg, "box-1", "auto", ssh="ssh")
    sock = tmp_path / "bridge-relay" / "box-1.auto" / "build-a.sock"
    assert argv[argv.index("-R") + 1] == f"127.0.0.1:2301:{sock}"
    svc = bridge.service_argv(cfg, "box-1", "auto", ssh="ssh")
    assert svc[1:6] == [str(tmp_path / "bridge_relay.py"), "--tag", "box-1/auto", str(sock.parent),
                        "build-a=build-a.example.com%vpn0"]
    assert svc[svc.index("--") + 1:] == argv
    plain = bridge.service_argv(_bridge_cfg(), "box-1", "auto", ssh="ssh")   # no interface host: still wrapped
    assert plain[1:5] == [str(tmp_path / "bridge_relay.py"), "--tag", "box-1/auto", str(sock.parent)]
    assert plain[5] == "--" and plain[6] == "/bin/sh"


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
    assert ('Match originalhost build-a,ba !exec "SSH_AUTH_SOCK=$HOME/.ssh/tt-agent/auto.sock ssh-add -l >/dev/null 2>&1; '
            f'[ $? -lt 2 ]" exec "sh {k}/valid"') in block
    assert f'!exec "sh {k}/valid quiet"' in block and "  PubkeyAuthentication no" in block
    assert f"  IdentityFile {k}/id_ed25519\n  CertificateFile {k}/id_ed25519-cert.pub" in block
    assert block.index("Match originalhost") < block.index("Host build-a")   # first match wins
    assert "%" not in block.split("Host build-a")[0]                         # ssh would expand it
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
sys.exit(0)
"""


def _fake_relay_ssh(d, plan):
    import json
    import sys
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(json.dumps(plan))
    ssh = d / "fake-ssh"
    ssh.write_text(f"#!{sys.executable}\n" + _BRIDGE_FAKE_SSH)
    ssh.chmod(0o755)
    return [str(ssh), "-T", "-n", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=20",
            "-o", "IdentitiesOnly=yes", "-o", "IdentityFile=/keys/login.pub", "-o", "ForwardAgent=/agents/a.sock",
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
    assert clean[clean.index("--") + 1] == "box-a" and clean[-1].endswith(f" {tag} 2301 2302")
    err = capfd.readouterr().err
    assert "remote port forwarding failed" in err   # passed through to the log
    assert "box-a: ended a stale session holding port 2301 (pid 4242, 3600 s old)" in err


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
        env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}")
        r = subprocess.run(["sh", "-c", bridge_relay.CLEANUP, "x", tag, "2301", "2302", "2303", "2304"],
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
    cfg = bridge.load(conf)
    new = bridge.service_argv(cfg, "box-1", "auto")
    old = bridge.ssh_argv(cfg, "box-1", "auto")   # what an older release installed: the bare session
    f = bridge.service_file("box-1", "auto", "darwin")
    f.parent.mkdir(parents=True)
    f.write_bytes(plistlib.dumps(bridge.launchd_plist("box-1", "auto", old)))
    assert bridge.refresh("darwin", conf) == ["bridge box-1 auto: service command changed; restarted"]
    assert started == [new] and bridge.RELAY.is_file()
    f.write_bytes(plistlib.dumps(bridge.launchd_plist("box-1", "auto", new)))
    assert bridge.launchd_plist("box-1", "auto", new)["ProgramArguments"][2:4] == ["--tag", "box-1/auto"]
    assert bridge.refresh("darwin", conf) == [] and len(started) == 1   # unchanged: left running
    monkeypatch.setattr(hoststate, "unit_dir", lambda: tmp_path / "units")
    (tmp_path / "units").mkdir()
    unit = bridge.systemd_unit("box-1", "auto", new)
    assert "--tag box-1/auto" in unit
    bridge.service_file("box-1", "auto", "linux").write_text(unit)
    assert bridge.installed_argv("box-1", "auto", "linux") == new
    monkeypatch.setenv("TTP_TEST_NO_BRIDGE_REFRESH", "1")
    assert bridge.refresh("darwin", conf) == []


# --- a dead agent behind a live-looking link (box loop, wrapper reconnect, host watch) --------------
# A stub ssh-add answers by marker files next to the socket it is asked about: <sock>.dead (exit 2),
# <sock>.fetch (exit 1, a failed exchange), <sock>.empty (exit 1, no keys). No real agent or key.

_BRIDGE_STUB_SSH_ADD = """#!/bin/sh
t=$SSH_AUTH_SOCK
[ -L "$t" ] && t=$(readlink "$t")
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
