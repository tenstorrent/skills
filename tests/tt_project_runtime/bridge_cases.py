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
    assert svc[1:4] == [str(tmp_path / "bridge_relay.py"), str(sock.parent), "build-a=build-a.example.com%vpn0"]
    assert svc[svc.index("--") + 1:] == argv
    assert bridge.service_argv(_bridge_cfg(), "box-1", "auto", ssh="ssh")[0] == "/bin/sh"   # no relay without one


def test_bridge_relay_carries_one_connection_and_exits_with_its_session(tmp_path, monkeypatch):
    import socket
    import threading
    from ttp import bridge_relay
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
