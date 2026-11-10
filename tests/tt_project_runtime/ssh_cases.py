# ssh command tests (sshcmd.py and its callers) of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that
# file, and pytest collects these tests under its name. Add new ssh command tests here.
from __future__ import annotations

_EVIL_DIR = "/srv/p;touch /tmp/pwned"


def _host_and_cmd(argv: list) -> tuple[str, str]:
    """(the host, the remote command) of an ssh argv; asserts '--' comes right before the host."""
    i = argv.index("--")
    return argv[i + 1], argv[i + 2]


def test_a_remote_folder_with_shell_characters_is_quoted_in_every_remote_command(env, monkeypatch, capsys):
    import shlex
    from types import SimpleNamespace
    from ttp import cli, notifier, sshcmd, tunnel, weblink
    entry = {"host": "host-a", "dir": _EVIL_DIR}
    want = shlex.quote(f"{_EVIL_DIR}/tt-project/harness/bin/ttp")
    # cli: every command forwarded to the project there
    host, cmd = _host_and_cmd(cli._ssh_cmd(entry, ["status", "demo"]))
    assert host == "host-a" and cmd.startswith(want + " ") and shlex.split(cmd)[0] == f"{_EVIL_DIR}/tt-project/harness/bin/ttp"
    # notifier: the minute alert poll
    seen = []
    monkeypatch.setattr(notifier.subprocess, "run", lambda argv, **k: seen.append(argv)
                        or SimpleNamespace(returncode=0, stdout="[]", stderr=""))
    assert notifier._remote_alerts(entry, "demo", 0, "high") == ([], True, None)
    host, cmd = _host_and_cmd(seen[-1])
    assert host == "host-a" and cmd.startswith(want + " ") and len(shlex.split(cmd.split(" --health")[0])) == 8, cmd
    # weblink: the daemon restart when the kept tunnel's link does not answer
    seen.clear()
    monkeypatch.setattr(weblink, "verify", lambda url, name: ("down", "no answer"))
    monkeypatch.setattr(tunnel, "installed", lambda name: {"host": "host-a"})
    monkeypatch.setattr(tunnel, "keep", lambda *a, **k: (18800, "kept"))
    monkeypatch.setattr(tunnel, "restart", lambda name: "restarted")
    monkeypatch.setattr(weblink.subprocess, "run", lambda argv, **k: seen.append(argv)
                        or SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    weblink.remote("demo", entry, 18700, "tok")
    assert [_host_and_cmd(a)[0] for a in seen] == ["host-a", "host-a"], seen   # the ssh check, then the restart
    host, cmd = _host_and_cmd(seen[-1])
    assert cmd == f"{want} restart demo", cmd
    # cli web: asking the daemon there for its link
    seen.clear()
    monkeypatch.setattr(cli, "resolve", lambda name: (None, entry))
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k: seen.append(argv)
                        or subprocess.CompletedProcess(argv, 0, "web app: http://127.0.0.1:18700/#token=abc123\n", ""))
    monkeypatch.setattr(tunnel, "installed", lambda name: None)
    cli.main(["web", "demo"])
    host, cmd = _host_and_cmd(seen[0])
    assert cmd == f"{want} web demo", cmd
    assert sshcmd.remote_cmd(entry, ["x"]) == f"{want} x"


def test_an_ssh_host_starting_with_a_dash_is_refused_everywhere(env, monkeypatch, capsys):
    from ttp import cli, notifier, sshcmd, tunnel, weblink
    entry = {"host": "-oProxyCommand=evil", "dir": "/srv/p"}
    for bad in ("-oFoo", "", "-"):
        with pytest.raises(sshcmd.BadHost):
            sshcmd.check_host(bad)
    assert sshcmd.check_host("host-a") == "host-a"
    with pytest.raises(sshcmd.BadHost):
        sshcmd.host({"host": "host-a", "ssh": "-oFoo"})   # the ssh alias wins, and is checked too
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail(f"ssh ran: {a}"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail(f"ssh ran: {a}"))
    with pytest.raises(SystemExit) as e:
        cli._ssh_cmd(entry, ["status"])
    assert e.value.code == 2 and "refusing ssh host" in capsys.readouterr().err
    with pytest.raises(sshcmd.BadHost):
        tunnel.ssh_argv("-oFoo", 18800, 18700)
    monkeypatch.setattr(tunnel, "keep", lambda *a, **k: pytest.fail("no tunnel to a refused host"))
    line, rc = weblink.remote("demo", entry, 18700, "tok")
    assert rc == weblink.UNVERIFIED and "refusing ssh host" in line, line
    assert notifier._remote_alerts(entry, "demo", 0, "high") == (None, False, None)
    monkeypatch.setattr(cli, "resolve", lambda name: (None, entry))
    with pytest.raises(SystemExit):
        cli.main(["web", "demo"])
    assert "refusing ssh host" in capsys.readouterr().err


def test_chat_log_markers_with_shell_characters_or_a_dash_host_are_not_matched(env, monkeypatch, tmp_path, capsys):
    from ttp import cli
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    logs = tmp_path / "userhome" / ".claude" / "projects" / "x"
    logs.mkdir(parents=True)
    (logs / "a.jsonl").write_text('{"text": "tt-project://demo@host-a:/srv/p;touch /tmp/x"}\n'
                                  '{"text": "tt-project://demo@-oFoo:/srv/q"}\n'
                                  '{"text": "tt-project://demo@host-b:~/w/demo-1.x/sub_dir"}\n'
                                  '{"text": "tt-project://demo@host-c:/srv/$(id)"}\n')
    hits = cli.search_transcripts("demo")
    # '/srv/p;touch' stops at ';' and '/srv/$(id)' at '$': only the safe prefix is a folder
    assert {(h["host"], h["dir"]) for h in hits} == {("host-a", "/srv/p"), ("host-c", "/srv/")}, hits
    # a '~' folder is not absolute, so it was never a marker
    assert all(";" not in h["dir"] and "$" not in h["dir"] for h in hits)


def test_resolve_says_which_host_and_folder_a_chat_log_hit_registers(env, monkeypatch, capsys):
    from ttp import cli
    seen = "/logs/session.jsonl"
    monkeypatch.setattr(cli, "local_project", lambda name: None)
    monkeypatch.setattr(cli, "remote_entry", lambda name: None)
    monkeypatch.setattr(cli, "search_transcripts", lambda name, cap_s=None: [{"host": "host-a", "dir": "/srv/p", "seen_in": seen, "mtime": 1}])
    registered = []
    monkeypatch.setattr(cli, "register", lambda name, entry: registered.append((name, entry)))
    p, entry = cli.resolve("demo")
    err = capsys.readouterr().err
    assert p is None and entry == {"host": "host-a", "dir": "/srv/p"} and registered == [("demo", entry)]
    assert "registered demo at host-a:/srv/p" in err and seen in err, err


def test_a_kept_tunnel_unit_keeps_percent_and_dollar_literal(env, tmp_path, monkeypatch):
    from ttp import tunnel
    argv = tunnel.ssh_argv("host-a", 18800, 18700, ssh="/opt/ssh%i/$HOME/ssh")
    unit = tunnel.systemd_unit("demo", argv)
    line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert "/opt/ssh%%i/$$HOME/ssh" in line and "%i" not in line.replace("%%", ""), line
    assert "$HOME" not in line.replace("$$", ""), line
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    f = tunnel.service_file("demo", "linux")
    f.parent.mkdir(parents=True)
    f.write_text(unit)
    got = tunnel.installed("demo", "linux")
    assert got["host"] == "host-a" and got["local"] == 18800 and got["remote"] == 18700 and got["own"], got


def _chat_logs_at(tmp_path, monkeypatch, n: int) -> list:
    """`n` Claude Code chat logs in a fake home, file i modified at time i (so the last is newest)."""
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    logs = tmp_path / "userhome" / ".claude" / "projects" / "x"
    logs.mkdir(parents=True)
    files = []
    for i in range(n):
        f = logs / f"s{i}.jsonl"
        f.write_text('{"text": "hello"}\n')
        os.utime(f, (1000 + i, 1000 + i))
        files.append(f)
    return files


def test_a_mistyped_name_stops_the_chat_log_search_at_its_cap_newest_logs_first(env, monkeypatch, tmp_path, capsys):
    import subprocess as sp
    from ttp import cli
    files = _chat_logs_at(tmp_path, monkeypatch, 5)
    clock = [0.0]
    monkeypatch.setattr(cli, "_mono", lambda: clock[0])
    monkeypatch.setattr(cli, "_GREP_BATCH", 1)
    monkeypatch.setenv("TTP_CHATLOG_SEARCH_S", "10")
    real_run, searched = sp.run, []

    def slow_grep(cmd, *a, **kw):   # each log takes 4 s of the fake clock
        searched.append(cmd[-1])
        clock[0] += 4
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(cli.subprocess, "run", slow_grep)
    with pytest.raises(SystemExit):
        cli.need("demo-typo", ["status"])
    # 10 s of budget: three 4 s logs at most, the newest first
    assert searched == [str(f) for f in files[::-1][:3]], searched
    err = capsys.readouterr().err
    assert "chat-log search cut after 10s" in err and "ttp find demo-typo" in err, err


def test_a_second_miss_for_the_same_name_skips_the_search_until_the_cache_expires(env, monkeypatch, tmp_path):
    import stat
    from ttp import cli
    _chat_logs_at(tmp_path, monkeypatch, 2)
    now = [5000.0]
    monkeypatch.setattr(cli, "_now", lambda: now[0])
    calls = []
    real = cli.search_transcripts
    monkeypatch.setattr(cli, "search_transcripts", lambda name, cap_s=None: calls.append(cap_s) or real(name, cap_s=cap_s))
    assert cli.resolve("demo-typo") == (None, None) and calls == [10.0]
    cache = cli._miss_cache_path()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    now[0] += 599
    assert cli.resolve("demo-typo") == (None, None) and calls == [10.0], "a cached miss searches again"
    assert cli.resolve("other-typo") == (None, None) and calls == [10.0, 10.0], "the cache is per name"
    now[0] += 2
    assert cli.resolve("demo-typo") == (None, None) and calls == [10.0, 10.0, 10.0], "an expired miss is searched"


def test_ttp_find_searches_every_chat_log_even_after_a_cached_miss_and_a_hit_registers(env, monkeypatch, tmp_path, capsys):
    from ttp import cli
    from ttp.project import load_registry
    files = _chat_logs_at(tmp_path, monkeypatch, 3)
    calls = []
    real = cli.search_transcripts
    monkeypatch.setattr(cli, "search_transcripts", lambda name, cap_s=None: calls.append(cap_s) or real(name, cap_s=cap_s))
    assert cli.resolve("demo") == (None, None)
    files[0].write_text('{"text": "tt-project://demo@host-a:/srv/demo"}\n')   # the oldest log
    assert cli.resolve("demo") == (None, None) and calls == [10.0]
    with pytest.raises(SystemExit):   # a miss is never cached for ttp find
        cli.main(["find", "nosuch"])
    cli.main(["find", "demo"])
    assert calls == [10.0, None, None], calls
    out, err = capsys.readouterr()
    assert "tt-project://demo@host-a:/srv/demo" in out and "registered demo at host-a:/srv/demo" in err
    assert load_registry()["projects"]["demo"].items() >= {"host": "host-a", "dir": "/srv/demo"}.items()


def test_a_chat_log_hit_still_registers_through_the_capped_search(env, monkeypatch, tmp_path, capsys):
    from ttp import cli
    from ttp.project import load_registry
    files = _chat_logs_at(tmp_path, monkeypatch, 3)
    files[1].write_text('{"text": "tt-project://demo@host-a:/srv/demo"}\n')
    assert cli.resolve("demo") == (None, {"host": "host-a", "dir": "/srv/demo"})
    assert load_registry()["projects"]["demo"].items() >= {"host": "host-a", "dir": "/srv/demo"}.items()
    assert not cli._miss_cache_path().exists()


_FAKE_SSH_G = """#!/bin/sh
# Stands in for ssh in tests: only `ssh -G <alias>`, from the made-up config below; never connects.
[ "$1" = -G ] || { echo "fake ssh: only -G" >&2; exit 2; }
case "$2" in
  box-a) printf 'host box-a\\nhostname box-a.example.test\\nproxyjump none\\nproxycommand none\\n' ;;
  box-j) printf 'host box-j\\nhostname box-j\\nproxyjump gw-a\\n' ;;
  box-bad) echo "/fake/config line 3: Bad configuration option: hostnme" >&2; exit 255 ;;
  *) printf 'host %s\\nhostname %s\\nproxyjump none\\n' "$2" "$2" ;;
esac
"""


def _fake_ssh_resolve(tmp_path, monkeypatch):
    bin_ = tmp_path / "fake-ssh-bin"
    bin_.mkdir()
    (bin_ / "ssh").write_text(_FAKE_SSH_G)
    (bin_ / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("TTP_TEST_NO_SSH_CHECK", raising=False)


def test_machines_add_refuses_an_ssh_alias_that_resolves_nowhere_and_saves_a_defined_one(env, tmp_path, monkeypatch, capsys):
    """`ttp machines add|set` asks `ssh -G` (read-only, a fake ssh here) and refuses an alias with no
    Host entry that is no host name either, naming the fix; a Host entry, a jump host, a bare name
    that resolves, this machine's own name and --no-ssh-check are saved; ssh it cannot run says nothing."""
    import socket
    from ttp import cli
    from ttp import machines as mm
    _fake_ssh_resolve(tmp_path, monkeypatch)
    real = socket.getaddrinfo
    looked: list[str] = []

    def lookup(name, *a, **kw):   # no network: only box-dns exists
        looked.append(name)
        if name == "box-dns":
            return real("127.0.0.1", None)
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    with pytest.raises(SystemExit):
        cli.main(["machines", "add", "box-b", "--tags", "device"])
    err = capsys.readouterr().err
    assert "ssh alias 'box-b' does not resolve" in err and "Host box-b" in err and "--no-ssh-check" in err, err
    assert "box-b" not in mm.load(), "a refused alias is not saved"
    with pytest.raises(SystemExit):
        cli.main(["machines", "set", "box-bad"])
    assert "Bad configuration option" in capsys.readouterr().err
    for alias in ("box-a", "box-j", "box-dns", "testhost"):
        cli.main(["machines", "add", alias, "--tags", "device"])
        assert alias in mm.load(), alias
    assert "box-a" not in looked and "box-j" not in looked, "a Host entry is enough: no lookup"
    cli.main(["machines", "add", "box-b", "--no-ssh-check"])
    assert "box-b" in mm.load()
    # A known machine: only a new host name asks again (its own name here is no ssh route needed).
    cli.main(["machines", "set", "box-b", "--note", "edited"])
    with pytest.raises(SystemExit):
        cli.main(["machines", "set", "box-b", "--hostname", "box-b-01"])
    capsys.readouterr()
    cli.main(["machines", "set", "box-b", "--hostname", "testhost"])
    assert mm.load()["box-b"]["hostname"] == "testhost"
    # ssh missing or slow, or a resolver that does not answer: cannot say, so saved.
    assert mm.route_problem("box-c") and mm.route_problem("box-c", timeout=0.0) == ""
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **kw: (_ for _ in ()).throw(socket.gaierror(socket.EAI_AGAIN, "Temporary failure")))
    assert mm.route_problem("box-c") == ""
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    assert mm.route_problem("box-b") == ""


@pytest.mark.parametrize("err, gone", [
    ("ssh: Could not resolve hostname box-a: Name or service not known", "box-a"),
    ("ssh: Could not resolve hostname box-a.example.test: No address associated with hostname", "box-a.example.test"),
    ("ssh: Could not resolve hostname box-a: Temporary failure in name resolution", ""),
    ("ssh: Could not resolve hostname box-a: nodename nor servname provided, or not known", ""),
    ("ssh: connect to host box-a port 22: Connection refused", ""),
])
def test_an_ssh_name_that_does_not_exist_is_a_broken_route_never_an_unreachable_wait(err, gone):
    """sshfail.unresolved picks out the resolver's 'no such name' answer; netwait.confirm then reads the
    probe's 255 as broken (it wakes its task for a fix) without asking any host, never as unreachable."""
    from ttp import netwait, sshfail
    assert sshfail.unresolved(err) == gone
    asked: list[str] = []
    why = netwait.confirm("ssh box-a true", err, answered=lambda h: asked.append(h) or False)
    if gone:
        assert f"ssh cannot resolve {gone}" in why and "not an outage" in why and not asked, why
    else:
        assert why == "" and asked == ["box-a"], "still confirmed against the host"


def test_ttp_new_on_another_host_quotes_every_forwarded_word_so_empty_or_spaced_ones_survive(env, monkeypatch, tmp_path):
    import shlex
    import subprocess as sp
    from types import SimpleNamespace
    from ttp import cli, coordinator, machines, timefmt
    words = ["~/my project/root", "", "a b", "x;touch pwned", "$HOME", "~/", "plain"]
    remote = cli.remote_words(words)
    assert shlex.split(remote) == [("$HOME/" + w[2:]) if w.startswith("~/") else w for w in words], remote
    # the remote shell sees the same words, with only the leading ~/ expanded to its home
    out = sp.run(["bash", "-c", f"printf '<%s>\\n' {remote}"], env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
                 capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == [f"<{tmp_path}/my project/root>", "<>", "<a b>", "<x;touch pwned>", "<$HOME>",
                                f"<{tmp_path}/>", "<plain>"], out
    # new_remote forwards --dir, --name and --provider through it
    seen = []
    monkeypatch.setattr(cli, "ship_runtime", lambda host: "~/stage dir/bin/ttp")
    monkeypatch.setattr(cli, "load_secrets", lambda: {})
    monkeypatch.setattr(machines, "push", lambda host: "machines: pushed")
    monkeypatch.setattr(timefmt, "in_ssh_login", lambda: True)
    monkeypatch.setattr(cli, "register", lambda name, entry: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k: seen.append(argv)
                        or SimpleNamespace(returncode=0, stdout="", stderr=""))
    a = SimpleNamespace(dir="~/my project/root", host="host-a", no_secrets=False, name="demo", provider="",
                        no_service=True, coordinator_effort=coordinator.DEFAULT_EFFORT)
    monkeypatch.setattr(cli, "detect_provider", lambda: "")
    cli.new_remote(a, "brief")
    assert shlex.split(seen[-1][-1]) == ["$HOME/stage dir/bin/ttp", "new", "demo", "--dir", "$HOME/my project/root",
                                         "--provider", "", "--no-service", "--describe-file", "-"], seen[-1]


# ssh connection reuse (template/bin/ssh, sshmux.py). A fake "real ssh" further down PATH records its
# argv; the wrapper is a copy of the template's.
_FAKE_SSH = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
case " $* " in *" -O stop "*) exit 0 ;; esac
[ -n "$FAKE_NONSOCK" ] && echo "ControlSocket $FAKE_NONSOCK already exists, disabling multiplexing" >&2
case " $* " in *"ControlMaster=auto"*) [ -n "$FAKE_MUX_FAIL" ] && { echo "mux_client_hello_exchange: write packet: Broken pipe" >&2; exit 255; } ;; esac
exit "${FAKE_RC:-0}"
"""


def _run_wrapper(tmp_path, args, **extra):
    import shutil
    import subprocess
    wbin, rbin, home = tmp_path / "wbin", tmp_path / "rbin", tmp_path / "home"
    for d in (wbin, rbin, home):
        d.mkdir(exist_ok=True)
    shutil.copy(RUNTIME.parent / "template" / "bin" / "ssh", wbin / "ssh")
    (wbin / "ssh").chmod(0o755)
    (rbin / "ssh").write_text(_FAKE_SSH)
    (rbin / "ssh").chmod(0o755)
    log = tmp_path / "log"
    log.unlink(missing_ok=True)
    e = {"PATH": f"{wbin}:{rbin}:/usr/bin:/bin", "HOME": str(home), "FAKE_LOG": str(log), "TMPDIR": str(tmp_path),
         **{k: str(v) for k, v in extra.items()}}
    r = subprocess.run([str(wbin / "ssh"), *args], env=e, capture_output=True, text=True, timeout=30)
    return r, (log.read_text().splitlines() if log.exists() else [])


def test_the_ssh_wrapper_adds_connection_reuse_in_a_private_folder(tmp_path):
    import shutil
    import stat
    import tempfile
    home = pathlib.Path(tempfile.mkdtemp(prefix="h", dir="/tmp"))   # short: a pytest tmp_path may be over 45
    try:
        r, log = _run_wrapper(tmp_path, ["-F", "cfg", "-J", "jump", "-o", "BatchMode=yes", "host-a", "true"],
                              HOME=home)
        cm = home / ".tt-project" / "cm"
        mode = stat.S_IMODE(cm.stat().st_mode)
    finally:
        shutil.rmtree(home)
    assert r.returncode == 0 and len(log) == 1
    for opt in ("ControlMaster=auto", f"ControlPath={cm}/%C", "ControlPersist=600",
                "ServerAliveInterval=15", "ServerAliveCountMax=3"):
        assert f"-o {opt}" in log[0]
    assert log[0].endswith("-F cfg -J jump -o BatchMode=yes host-a true")   # -F, ProxyJump, BatchMode kept
    assert mode == 0o700
    assert "/.ssh" not in log[0]
    assert not list(tmp_path.glob("ttp-ssh.*"))                              # its stderr copy is removed


@pytest.mark.parametrize("args,extra", [
    (["host-a", "true"], {"TTP_SSH_REUSE": "0"}),
    (["-S", "/x/sock", "host-a", "true"], {}),
    (["-o", "ControlMaster=no", "host-a", "true"], {}),
    (["-oControlPath=none", "host-a", "true"], {}),
    (["-o", "controlpath=/x/%C", "host-a", "true"], {}),
    (["-O", "check", "host-a"], {}),
    (["-L", "8080:localhost:80", "host-a"], {}),
    (["-N", "host-a"], {}),
    (["-G", "host-a"], {}),
    (["-p", "22", "-fN", "host-a"], {}),
    (["-v", "host-a", "true"], {}),
    (["-o", "BatchMode=yes", "-vvv", "host-a", "true"], {}),
])
def test_the_ssh_wrapper_leaves_calls_that_set_their_own_sharing_or_forward(tmp_path, args, extra):
    r, log = _run_wrapper(tmp_path, args, **extra)
    assert r.returncode == 0 and log == [" ".join(args)]


def test_the_ssh_wrapper_reads_only_options_before_the_host(tmp_path):
    """A remote command's words (-Rf) and an option's value (-i -N) are not read as ssh options."""
    r, log = _run_wrapper(tmp_path, ["-i", "-N", "host-a", "rm", "-Rf", "/tmp/x"])
    assert r.returncode == 0 and "ControlMaster=auto" in log[0]
    assert log[0].endswith("-i -N host-a rm -Rf /tmp/x")


def test_the_ssh_wrapper_passes_the_remote_exit_code_without_a_retry(tmp_path):
    r, log = _run_wrapper(tmp_path, ["host-a", "exit 3"], FAKE_RC=3)
    assert r.returncode == 3 and len(log) == 1
    r, log = _run_wrapper(tmp_path, ["host-a", "false"], FAKE_RC=255)   # 255 without a sharing error
    assert r.returncode == 255 and len(log) == 1


def test_a_broken_shared_connection_falls_back_once_to_a_plain_one(tmp_path):
    r, log = _run_wrapper(tmp_path, ["-o", "BatchMode=yes", "host-a", "exit 4"], FAKE_MUX_FAIL=1, FAKE_RC=4)
    assert r.returncode == 4, r.stderr
    assert len(log) == 3
    assert "-O stop" in log[1] and "ControlMaster=auto" in log[1]   # the bad socket is removed
    assert log[2] == "-o ControlMaster=no -o ControlPath=none -o BatchMode=yes host-a exit 4"
    assert "mux_client_hello" in r.stderr                              # the first error still shows


def test_the_ssh_wrapper_keeps_its_socket_path_under_the_unix_limit(tmp_path):
    import os
    r, log = _run_wrapper(tmp_path, ["host-a", "true"], TTP_SSH_CM_DIR=tmp_path / ("d" * 70))
    want = f"/tmp/ttp-cm-{os.getuid()}"
    assert r.returncode == 0 and f"ControlPath={want}/%C" in log[0]
    # %C is 40 hex characters and ssh appends a 17-character suffix while it binds: a folder of at most
    # 45 characters (the wrapper's limit) keeps the path and its NUL within macOS's 104-byte limit.
    assert 45 + 1 + 40 + 17 + 1 <= 104 and len(want) <= 45
    r, log = _run_wrapper(tmp_path, ["host-a", "true"], TTP_SSH_CM_DIR="/tmp/" + "d" * 40)   # 45: kept
    assert f"ControlPath=/tmp/{'d' * 40}/%C" in log[0]
    os.rmdir("/tmp/" + "d" * 40)


def test_sshmux_routes_bare_ssh_to_the_wrapper_unless_reuse_is_off():
    from ttp import sshmux
    assert sshmux.enabled(None) and sshmux.enabled({"ssh": {"reuse": True}})
    assert not sshmux.enabled({"ssh": {"reuse": False}}) and not sshmux.enabled({"ssh": {"reuse": "off"}})
    assert sshmux.env({"ssh": {"reuse": False}}) == {"TTP_SSH_REUSE": "0"}
    w = sshmux.wrapper()
    assert w and w.endswith("/bin/ssh")
    assert sshmux.argv(["ssh", "-o", "BatchMode=yes", "h", "true"], {}) == [w, "-o", "BatchMode=yes", "h", "true"]
    assert sshmux.argv(["ssh", "h"], {"TTP_SSH_REUSE": "0"}) == ["ssh", "h"]
    assert sshmux.argv(["/usr/bin/ssh", "h"], {}) == ["/usr/bin/ssh", "h"]
    assert sshmux.argv("ssh h", {}) == "ssh h"


def test_stepclock_sends_the_daemons_ssh_through_the_wrapper(monkeypatch):
    from ttp import sshmux, stepclock
    seen = []
    monkeypatch.setitem(stepclock._orig, "init", lambda self, args, *a, **kw: seen.append(args))
    stepclock._init(None, ["ssh", "h", "true"], env={"TTP_SSH_REUSE": "1"})
    stepclock._init(None, ["ssh", "h", "true"], env={"TTP_SSH_REUSE": "0"})
    assert seen[0][0] == sshmux.wrapper() and seen[1][0] == "ssh"


def test_project_commands_carry_the_ssh_reuse_switch(env):
    from ttp.project import command_env
    p = _device_project(env, device=False)
    assert command_env(p).get("TTP_SSH_REUSE") == "1"
    p.set_config("ssh.reuse", False)
    assert command_env(p).get("TTP_SSH_REUSE") == "0"


def test_the_ssh_wrapper_removes_a_non_socket_left_at_its_socket_path(tmp_path):
    """ssh runs unshared past a plain file at the socket path; the wrapper removes it so the next call shares
    again, but never a path outside its own folder."""
    import shutil
    import tempfile
    cm = pathlib.Path(tempfile.mkdtemp(prefix="c", dir="/tmp"))   # short: the wrapper's folder limit is 45
    try:
        bad, outside = cm / ("a" * 40), tmp_path / "keep"
        bad.write_text("")
        outside.write_text("")
        r, log = _run_wrapper(tmp_path, ["host-a", "true"], TTP_SSH_CM_DIR=cm, FAKE_NONSOCK=bad)
        assert r.returncode == 0 and len(log) == 1 and not bad.exists()
        r, _ = _run_wrapper(tmp_path, ["host-a", "true"], TTP_SSH_CM_DIR=cm, FAKE_NONSOCK=outside)
        assert r.returncode == 0 and outside.exists()
    finally:
        shutil.rmtree(cm)
