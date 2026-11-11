# Proxy host rewrite tests (loopproxy.py and loopproxy.sh) of tests/test_tt_project_runtime.py, which runs
# this file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that
# file, and pytest collects these tests under its name. Add new proxy rewrite tests here.
from __future__ import annotations

# (value, expected): the credentials are placeholders, never a real proxy's.
LOOPPROXY_CASES = [
    ("http://user:pw@localhost:3128", "http://user:pw@127.0.0.1:3128"),
    ("socks5h://user:pw@localhost:1080", "socks5h://user:pw@127.0.0.1:1080"),
    ("http://localhost:3128", "http://127.0.0.1:3128"),
    ("http://localhost:3128/some/path", "http://127.0.0.1:3128/some/path"),
    ("http://localhost", "http://127.0.0.1"),
    ("localhost:1080", "127.0.0.1:1080"),
    ("user:pw@localhost:1080", "user:pw@127.0.0.1:1080"),
    ("http://us@er:p*w@localhost:3128", "http://us@er:p*w@127.0.0.1:3128"),   # a raw @ and a glob character
    ("http://proxy.example.com:3128", "http://proxy.example.com:3128"),
    ("http://user:pw@proxy.example.com:3128/localhost", "http://user:pw@proxy.example.com:3128/localhost"),
    ("http://localhost.example.com:3128", "http://localhost.example.com:3128"),
    ("http://localhostx:3128", "http://localhostx:3128"),
    ("http://localhost@proxy.example.com:3128", "http://localhost@proxy.example.com:3128"),   # localhost as the user
    ("127.0.0.1:1080", "127.0.0.1:1080"),
    ("", ""),
]


@pytest.mark.parametrize("value,expected", LOOPPROXY_CASES)
def test_loopproxy_rewrites_only_a_localhost_host(value, expected):
    from ttp.loopproxy import loopback
    assert loopback(value) == expected


def test_loopproxy_fixed_covers_upper_and_lower_case_and_leaves_no_proxy():
    from ttp.loopproxy import PROXY_VARS, fixed
    env = {k: "http://user:pw@localhost:3128" for k in PROXY_VARS}
    env.update(NO_PROXY="localhost,127.0.0.1", no_proxy="localhost", RSYNC_PROXY="localhost:1080",
               HTTPS_PROXY="http://proxy.example.com:3128", ftp_proxy="localhost:1080")
    out = fixed(env)
    assert {"https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "FTP_PROXY", "ftp_proxy"} == set(out)
    assert out["https_proxy"] == "http://user:pw@127.0.0.1:3128" and out["ftp_proxy"] == "127.0.0.1:1080"
    assert fixed({"PATH": "/bin", "HTTPS_PROXY": "http://127.0.0.1:3128"}) == {}


def _sourced(shell: str, env: dict, prelude: str = "") -> dict:
    """The proxy variables after `shell` sources loopproxy.sh with `env`, and the script's exit status."""
    from ttp.loopproxy import PROXY_VARS, SCRIPT
    probe = "; ".join(f'printf "%s=%s\\0" {k} "${{{k}-}}"' for k in (*PROXY_VARS, "NO_PROXY", "PREV_RAN"))
    out = subprocess.run([shell, "-c", f'{prelude}. "$1"; echo "rc=$?" >&2; {probe}', "sh", str(SCRIPT)],
                         capture_output=True, text=True, env={"PATH": os.environ["PATH"], **env}, timeout=30)
    assert out.returncode == 0 and "rc=0" in out.stderr, out.stderr
    return dict(kv.split("=", 1) for kv in out.stdout.split("\0") if kv)


@pytest.mark.parametrize("shell", ["bash", "dash"])
def test_loopproxy_script_matches_python_in_each_shell(shell):
    if not shutil.which(shell):
        pytest.skip(f"no {shell}")
    from ttp.loopproxy import PROXY_VARS, loopback
    for value, expected in LOOPPROXY_CASES:
        got = _sourced(shell, {**{k: value for k in PROXY_VARS}, "NO_PROXY": "localhost"})
        assert all(got[k] == expected == loopback(value) for k in PROXY_VARS), (value, got)
        assert got["NO_PROXY"] == "localhost"
    # Unset variables stay unset (empty), and a shell with `set -u` does not stop on them.
    got = _sourced(shell, {}, prelude="set -u; ")
    assert all(got[k] == "" for k in PROXY_VARS)


def test_loopproxy_script_sources_an_inherited_env_file_after_its_rewrite(tmp_path):
    from ttp.loopproxy import PREV_ENV
    prev = tmp_path / "prev.sh"
    prev.write_text('PREV_RAN="$HTTPS_PROXY"\n')
    got = _sourced("bash", {"HTTPS_PROXY": "http://localhost:3128", PREV_ENV: str(prev)})
    assert got["PREV_RAN"] == "http://127.0.0.1:3128"
    got = _sourced("bash", {"HTTPS_PROXY": "http://localhost:3128", PREV_ENV: str(tmp_path / "missing.sh")})
    assert got["HTTPS_PROXY"] == "http://127.0.0.1:3128" and got["PREV_RAN"] == ""


def test_loopproxy_env_file_keeps_an_inherited_one():
    from ttp.loopproxy import PREV_ENV, SCRIPT, env_file
    assert env_file({}) == {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: ""}
    assert env_file({"CLAUDE_ENV_FILE": "/x/env.sh"}) == {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: "/x/env.sh"}
    assert env_file({"CLAUDE_ENV_FILE": str(SCRIPT)}) == {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: ""}


def test_loopproxy_env_file_never_chains_to_another_projects_copy():
    """A daemon started from another project's worker shell inherits that project's CLAUDE_ENV_FILE (its
    own loopproxy.sh) and PREV_ENV naming the same file: chaining to it made every worker Bash call
    source that copy forever (exit 1, no output). Another copy is skipped for the file it chains to;
    the inherited PREV_ENV is always overridden."""
    from ttp.loopproxy import PREV_ENV, SCRIPT, env_file
    other = "/other/project/tt-project/harness/runtime/ttp/loopproxy.sh"
    assert env_file({"CLAUDE_ENV_FILE": other, PREV_ENV: other}) == {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: ""}
    assert env_file({"CLAUDE_ENV_FILE": other, PREV_ENV: "/x/env.sh"}) == \
        {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: "/x/env.sh"}
    assert env_file({PREV_ENV: other}) == {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: ""}


@pytest.mark.parametrize("shell", ["bash", "dash"])
def test_loopproxy_script_never_sources_itself_forever(tmp_path, shell):
    """PREV_ENV naming this script, or another copy whose own PREV_ENV is the same file, ends after one
    pass: the variable is unset while the previous file runs, and restored afterwards."""
    if not shutil.which(shell):
        pytest.skip(f"no {shell}")
    from ttp.loopproxy import PREV_ENV, SCRIPT
    other = tmp_path / "ttp" / "loopproxy.sh"
    other.parent.mkdir()
    other.write_text(SCRIPT.read_text())
    for prev in (str(SCRIPT), str(other)):
        got = _sourced(shell, {"HTTPS_PROXY": "http://localhost:3128", PREV_ENV: prev})
        assert got["HTTPS_PROXY"] == "http://127.0.0.1:3128"
        out = subprocess.run([shell, "-c", f'. "$1"; printf "%s" "${{{PREV_ENV}-}}"', "sh", str(SCRIPT)],
                             capture_output=True, text=True, env={"PATH": os.environ["PATH"], PREV_ENV: prev},
                             timeout=30)
        assert out.returncode == 0 and out.stdout == prev, out.stderr[-500:]


def test_loopproxy_claude_worker_env_names_the_script(monkeypatch):
    from ttp.loopproxy import SCRIPT
    from ttp.providers.claude import Claude
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    monkeypatch.setattr(Claude, "supports", lambda self, flag: False)
    for read_only in (False, True):
        _, env = Claude().build(role="worker", model="m", effort="high", cwd="/tmp", budget_usd=1.0,
                                read_only=read_only, schema=None, restrictions={})
        assert env["CLAUDE_ENV_FILE"] == str(SCRIPT) and SCRIPT.is_file()


def test_loopproxy_runner_starts_the_agent_with_loopback_proxies(env, tmp_path, monkeypatch):
    """The runner's child sees 127.0.0.1; run.json, which the daemon wrote, keeps no proxy value."""
    from ttp import runner
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    dump = tmp_path / "env.txt"
    script = f'printf "%s|%s|%s" "$HTTPS_PROXY" "$all_proxy" "$NO_PROXY" > {shlex.quote(str(dump))}'
    (run_dir / "run.json").write_text(json.dumps({"argv": ["sh", "-c", script], "cwd": str(tmp_path),
                                                  "timeout_s": 60, "env": {}, "provider": "fake"}))
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@localhost:3128")
    monkeypatch.setenv("all_proxy", "socks5h://localhost:1080")
    monkeypatch.setenv("NO_PROXY", "localhost")
    assert runner.supervise(run_dir) == 0
    assert dump.read_text() == "http://user:pw@127.0.0.1:3128|socks5h://127.0.0.1:1080|localhost"
    assert "pw@" not in (run_dir / "run.json").read_text()


# Host loopback ports (loopproxy.ports_env and the forwarders loopproxy.sh starts in the Bash sandbox).
def _proc_net(tmp_path, tcp: list[str], tcp6: list[str]):
    head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    net = tmp_path / "net"
    net.mkdir()
    for name, rows in (("tcp", tcp), ("tcp6", tcp6)):
        (net / name).write_text(head + "".join(f"   {i}: {local} {'0' * (len(local) - 5)}:0000 {st} 0 0 0\n"
                                               for i, (local, st) in enumerate(rows)))
    return net


def test_loopback_listening_reads_loopback_then_wildcard_listeners(tmp_path):
    from ttp.loopproxy import listening
    net = _proc_net(tmp_path,
                    [("00000000:0016", "0A"), ("0100007F:1F90", "0A"), ("0100007F:2328", "01"),   # 01: established
                     ("3500007F:0035", "0A"), ("0A00000A:1388", "0A")],                        # 127.0.0.53, 10.0.0.10
                    [("00000000000000000000000001000000:1389", "0A"), ("0" * 32 + ":1F90", "0A"),
                     ("0000000000000000FFFF00000100007F:138A", "0A")])
    assert listening(net) == [8080, 5001, 5002, 22]
    assert listening(tmp_path / "missing") == []


def test_loopback_ssh_ports_reads_tunnel_ends_from_ssh_config(tmp_path):
    from ttp.loopproxy import ssh_ports
    cfg = tmp_path / "config"
    cfg.write_text("""# a comment
Host tunnel
    HostName localhost
    Port 2222
Host tunnel6
  hostname=::1
  port=2223
Host far
    HostName box.example.com
    Port 2224
    LocalForward 9001 127.0.0.1:80
    LocalForward 127.0.0.1:9002 other:80
    LocalForward [::1]:9003 other:80
    LocalForward *:9004 other:80
    LocalForward 10.1.2.3:9005 other:80
    LocalForward /tmp/sock other:80
Host noport
    HostName 127.0.0.1
Host
""")
    assert ssh_ports(cfg) == [2222, 2223, 9001, 9002, 9003, 9004]
    assert ssh_ports(tmp_path / "missing") == []


def test_loopback_ports_env_by_default_with_extras_first_capped_and_off(tmp_path, monkeypatch):
    from ttp import loopproxy
    net = _proc_net(tmp_path, [("0100007F:1F90", "0A"), ("00000000:2710", "0A")], [])
    cfg = tmp_path / "config"
    cfg.write_text("Host t\n  HostName localhost\n  Port 2222\n")
    got = loopproxy.ports_env(None, net, cfg)
    assert got[loopproxy.PORTS_ENV] == "2222 8080 10000", "on by default: ssh tunnel ends, then listeners"
    assert got[loopproxy.NETNS_ENV] == os.readlink("/proc/self/ns/net")
    assert loopproxy.ports_env([7000, 8080, True, "x"], net, cfg)[loopproxy.PORTS_ENV] == "7000 8080 2222 10000"
    assert loopproxy.ports_env(False, net, cfg) == {}, "false keeps the sandbox's own namespace"
    assert loopproxy.ports_env(None, tmp_path / "none", tmp_path / "none") == {}
    many = list(range(20000, 20000 + loopproxy.MAX_PORTS + 5))
    assert len(loopproxy.ports_env(many, net, cfg)[loopproxy.PORTS_ENV].split()) == loopproxy.MAX_PORTS


@pytest.mark.parametrize("value,expected", [
    ("8080,9000", [8080, 9000]), ("8080 9000 8080", [8080, 9000]), ([5000], [5000]), ("false", False),
    ("off", False), (False, False), ("0", False)])
def test_loopback_ports_setter_accepts(value, expected):
    from ttp.coordinator import _loopback_ports
    assert _loopback_ports("k", value) == expected


@pytest.mark.parametrize("value", ["", "x", "70000", "8080,abc", "-1", True, [True], ["a"]])
def test_loopback_ports_setter_refuses(value):
    from ttp.coordinator import _loopback_ports
    with pytest.raises(ValueError):
        _loopback_ports("k", value)


def test_daemon_gives_fenced_claude_workers_the_host_loopback_ports(env, monkeypatch):
    from ttp import loopproxy
    from ttp.daemon import Daemon
    from ttp.providers import claude
    monkeypatch.setattr(claude.Claude, "binary", lambda self: "/usr/bin/true")   # never a real agent
    monkeypatch.setattr(claude.sys, "platform", "linux")
    have = {"bwrap", "socat"}
    monkeypatch.setattr(claude, "find_binary", lambda b: f"/usr/bin/{b}" if b in have else None)
    monkeypatch.setattr(loopproxy, "listening", lambda net=None: [8080])
    monkeypatch.setattr(loopproxy, "ssh_ports", lambda path=None: [2222])
    p = make(env)

    def launch():
        tid = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
        rid = Daemon(p.base).start_run("worker", "go", "claude", "light", str(env["repo"]), task=p.db.task(tid))
        (p.runs / str(rid) / "STOP").touch()
        return json.loads((p.runs / str(rid) / "run.json").read_text())["env"]

    def set_ports(value):
        p.set_config("providers.claude.loopback_ports", value)
    assert loopproxy.PORTS_ENV not in launch(), "an unfenced worker (the default) shares the host's network"
    _write_fence("claude")(p)
    got = launch()
    assert got[loopproxy.PORTS_ENV] == "2222 8080" and got[loopproxy.NETNS_ENV].startswith("net:"), "on once fenced"
    set_ports([5000])
    assert launch()[loopproxy.PORTS_ENV] == "5000 2222 8080"
    set_ports(False)
    assert loopproxy.PORTS_ENV not in launch(), "false: the sandbox keeps its own namespace"
    set_ports(None)
    have.discard("bwrap")
    assert loopproxy.PORTS_ENV not in launch(), "an unfenced worker shares the host's network already"


def _fake_socat(tmp_path):
    """A socat on PATH that records its arguments and listens on nothing."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "socat.log"
    (bin_ / "socat").write_text(f'#!/bin/sh\necho "$@" >> {shlex.quote(str(log))}\n')
    (bin_ / "socat").chmod(0o755)
    return bin_, log


def test_loopback_script_forwards_only_from_another_namespace(tmp_path):
    """The worker's own namespace (a command outside the sandbox) binds nothing; another one gets one
    listener per unprivileged port, through the proxy with its credentials."""
    from ttp.loopproxy import NETNS_ENV, PORTS_ENV, SCRIPT
    if not Path("/proc/self/ns/net").exists():
        pytest.skip("no network namespaces")
    bin_, log = _fake_socat(tmp_path)
    # The script skips a port something already listens on, and this runs in the host's namespace: two
    # bound, never listening sockets reserve free ports, so no other process on a shared host takes one.
    held = [socket.socket() for _ in range(2)]
    for s in held:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in held]
    base = {"PATH": f"{bin_}:{os.environ['PATH']}", "HTTPS_PROXY": "http://user:pw@localhost:3128",
            PORTS_ENV: f"{ports[0]} 22 x {ports[1]}"}
    for netns, want in ((os.readlink("/proc/self/ns/net"), False), ("net:[1]", True), ("", False)):
        log.unlink(missing_ok=True)
        # The forwarders start in the background: the shell waits for them (the stand-ins log and exit),
        # so every line is in before the log is read, however loaded the host; their order is free.
        out = subprocess.run(["bash", "-c", 'set -u; . "$1"; rc=$?; wait; echo "rc=$rc"', "sh", str(SCRIPT)],
                             capture_output=True, text=True, env={**base, NETNS_ENV: netns}, timeout=120)
        assert out.stdout.strip() == "rc=0", out
        calls = log.read_text().splitlines() if log.exists() else []
        if not want:
            assert calls == [], netns
            continue
        assert sorted(calls) == [f"TCP-LISTEN:{p},bind=127.0.0.1,fork,reuseaddr "
                                 f"PROXY:127.0.0.1:localhost:{p},proxyport=3128,proxyauth=user:pw"
                                 for p in sorted(ports)]
    log.unlink(missing_ok=True)
    out = subprocess.run(["bash", "-c", '. "$1"; rc=$?; wait; exit $rc', "sh", str(SCRIPT)], capture_output=True,
                         text=True, timeout=120,
                         env={**base, NETNS_ENV: "net:[1]", "HTTPS_PROXY": "http://proxy.example.com:3128"})
    assert out.returncode == 0 and not log.exists(), "only the sandbox's own loopback proxy carries them"
    for s in held:
        s.close()


def test_loopback_live_connect_from_a_namespace_without_the_host_network(tmp_path):
    """A host listener on 127.0.0.1 is refused from a fresh network namespace (bwrap --unshare-net, as
    the Bash sandbox runs each command) and answers once loopproxy.sh forwards it through a CONNECT
    proxy bridged in on a Unix socket, as the sandbox bridges its own."""
    import socket
    import threading
    from ttp.loopproxy import NETNS_ENV, PORTS_ENV, SCRIPT
    bwrap, socat = shutil.which("bwrap"), shutil.which("socat")
    if not (bwrap and socat):
        pytest.skip("needs bwrap and socat")
    if subprocess.run([bwrap, "--unshare-net", "--dev-bind", "/", "/", "true"], capture_output=True).returncode:
        pytest.skip("bwrap cannot start a network namespace here")
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]
    sock = tmp_path / "proxy.sock"
    proxy = socket.socket(socket.AF_UNIX)
    proxy.bind(str(sock))
    proxy.listen(8)

    def serve():
        while True:
            try:
                c, _ = server.accept()
            except OSError:
                return
            c.sendall(b"pong\n")
            c.close()

    def pipe(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def connect_proxy():
        while True:
            try:
                c, _ = proxy.accept()
            except OSError:
                return
            head = b""
            while b"\r\n\r\n" not in head and (chunk := c.recv(4096)):
                head += chunk
            target = head.split()[1].decode()
            host, _, tport = target.rpartition(":")
            try:
                # As the sandbox's proxy, which resolves localhost on the host (no resolver needed here).
                host = "127.0.0.1" if host == "localhost" else host.strip("[]")
                up = socket.create_connection((host, int(tport)), timeout=5)
            except OSError:
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                c.close()
                continue
            c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            threading.Thread(target=pipe, args=(c, up), daemon=True).start()
            threading.Thread(target=pipe, args=(up, c), daemon=True).start()

    for fn in (serve, connect_proxy):
        threading.Thread(target=fn, daemon=True).start()
    # The connect runs from a script started inside a compound command: the forwarders serve every
    # process of the sandboxed command, not only a bare top-level one.
    probe = tmp_path / "probe.py"
    probe.write_text(f"import socket\ns = socket.create_connection(('127.0.0.1', {port}), timeout=5)\n"
                     "print(s.makefile().readline().strip())\n")
    script = tmp_path / "connect.sh"
    script.write_text(f"python3 {shlex.quote(str(probe))} 2>/dev/null || echo refused\n")
    connect = f"true && sh {shlex.quote(str(script))} | cat"
    inner = (f'{shlex.quote(socat)} TCP-LISTEN:3128,bind=127.0.0.1,fork,reuseaddr UNIX-CONNECT:{shlex.quote(str(sock))} '
             f'</dev/null >/dev/null 2>&1 & i=0; while [ $i -lt 100 ] && ! grep -q ":0C38 00000000:0000 0A" /proc/net/tcp; '
             f'do sleep 0.02; i=$((i+1)); done; echo "before=$({connect})"; . {shlex.quote(str(SCRIPT))}; '
             f'echo "after=$({connect})"')
    try:
        out = subprocess.run([bwrap, "--unshare-net", "--unshare-pid", "--die-with-parent", "--dev-bind", "/", "/",
                              "--proc", "/proc", "bash", "-c", inner],
                             capture_output=True, text=True, timeout=60,
                             env={"PATH": os.environ["PATH"], "HTTPS_PROXY": "http://localhost:3128",
                                  PORTS_ENV: str(port), NETNS_ENV: os.readlink("/proc/self/ns/net")})
    finally:
        server.close()
        proxy.close()
    assert "before=refused" in out.stdout and "after=pong" in out.stdout, (out.stdout, out.stderr)
