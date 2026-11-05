# `ttp ssh-preflight` tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new ssh preflight tests here, not there.
from __future__ import annotations


def _nnp_fake_ssh(tmp_path):
    """A fake ssh: `-G <host>` prints the host's ProxyCommand (a sudo one for nsbox and nskey, a plain
    one for plainbox); a connection fails as a sudo proxy that died does, or with a host-key error."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "ssh").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = -G ]; then\n"
        "  echo \"hostname $2\"\n"
        "  case \"$2\" in\n"
        "    nsbox|nskey) echo 'proxycommand /usr/bin/sudo -n ip netns exec mgmt nc %h %p';;\n"
        "    plainbox) echo 'proxycommand nc -X connect -x proxy:3128 %h %p';;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "for a; do [ \"$a\" = true ] && break; h=$a; done\n"
        "case \"$h\" in\n"
        "  nskey) echo 'Host key verification failed.' >&2; exit 255;;\n"
        "  *) echo 'kex_exchange_identification: Connection closed by remote host' >&2;"
        " echo 'Connection closed by UNKNOWN port 65535' >&2; exit 255;;\n"
        "esac\n")
    (bindir / "ssh").chmod(0o755)
    return f"{bindir}:{os.environ['PATH']}"


def test_ssh_preflight_names_a_sudo_proxycommand_that_no_new_privs_breaks(env, tmp_path):
    """With no_new_privs set and a ProxyCommand running sudo, ssh's kex_exchange_identification is no
    host refusal: the preflight exits 13 (wakes a waiting task) and says the cause and the fix."""
    sys.path.insert(0, str(RUNTIME))
    from ttp import daemon, hostmap, privlock
    if privlock.fenced() is None or privlock._prctl() is None:
        pytest.skip("needs Linux no_new_privs")
    p = make(env)
    path = _nnp_fake_ssh(tmp_path)
    e = {k: v for k, v in os.environ.items() if k not in ("TTP_RUN_DIR", "TTP_LOCKS_HELD", "TTP_DETACHED")}
    e.update({"TTP_PROJECT": str(p.base), "PATH": path})

    def run(*args):
        return subprocess.run([sys.executable, str(TTP), "ssh-preflight", *args], env=e, cwd=str(p.root),
                              capture_output=True, text=True, timeout=30,
                              preexec_fn=privlock.no_new_privs_preexec())

    r = run("nsbox")
    assert r.returncode == hostmap.NO_NEW_PRIVS_RC == 13, (r.returncode, r.stderr)
    assert "NoNewPrivs: 1" in r.stderr and "ProxyCommand runs sudo" in r.stderr, r.stderr
    assert "unsandboxed" in r.stderr and "ControlMaster" in r.stderr and "kex_exchange_identification" in r.stderr
    assert "13" not in map(str, daemon.NOT_YET_RCS), "13 wakes a waiting task: waiting cannot fix it"
    assert run("nsbox", "--classified").returncode == 13
    p.set_config("device", {"hosts": {"dev": "nsbox"}})
    assert run("@dev").returncode == 13
    # Not this case: a ProxyCommand without sudo, or another ssh failure first.
    assert run("plainbox").returncode == 255
    assert run("nskey").returncode == hostmap.HOST_KEY_RC
    assert run("other").returncode == 255


def test_ssh_preflight_keeps_255_where_sudo_can_run(monkeypatch, tmp_path):
    """Without no_new_privs the same kex failure is the host's (or its proxy's) and stays 255, the
    outage code that keeps a waiting task asleep."""
    sys.path.insert(0, str(RUNTIME))
    from ttp import hostmap
    monkeypatch.setenv("PATH", _nnp_fake_ssh(tmp_path))
    monkeypatch.setattr(hostmap, "no_new_privs_here", lambda: False)
    assert hostmap.preflight("nsbox")[0] == 255
    monkeypatch.setattr(hostmap, "no_new_privs_here", lambda: True)
    rc, out = hostmap.preflight("nsbox")
    assert rc == 13 and out.startswith(hostmap.NO_NEW_PRIVS_WHY)
    assert hostmap.preflight("nsbox", classified=True)[0] == 13


@pytest.mark.parametrize("proxy, sudo", [
    ("sudo -n ip netns exec mgmt nc %h %p", True),
    ("/usr/bin/sudo -n nc %h %p", True),
    ("sh -c 'sudo nc %h %p'", True),
    ("env X=1;doas nc %h %p", True),
    ("pkexec nc %h %p", True),
    ("nc %h %p|sudo tee /dev/null", True),
    ("pseudo-proxy %h %p", False),
    ("sudoku-proxy %h %p", False),
    ("nc -X connect -x proxy:3128 %h %p", False),
    ("none", False)])
def test_ssh_preflight_tells_a_sudo_proxycommand_by_its_words(monkeypatch, proxy, sudo):
    sys.path.insert(0, str(RUNTIME))
    from ttp import hostmap
    monkeypatch.setattr(hostmap.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=f"hostname box\nproxycommand {proxy}\nport 22\n", stderr=""))
    assert hostmap.sudo_proxy("box") is sudo


def test_ssh_preflight_skips_the_proxycommand_check_when_ssh_g_fails(monkeypatch):
    sys.path.insert(0, str(RUNTIME))
    from ttp import hostmap

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 10)
    monkeypatch.setattr(hostmap.subprocess, "run", boom)
    assert hostmap.sudo_proxy("box") is False


@pytest.mark.parametrize("sandboxed, hinted", [("1", True), ("", False), ("0", False)])
def test_a_kex_failure_in_a_sandboxed_run_names_the_sudo_proxycommand_cause(monkeypatch, sandboxed, hinted):
    from ttp import hook
    monkeypatch.delenv("TTP_RUN_DIR", raising=False)
    monkeypatch.setenv("TTP_SANDBOXED", sandboxed)
    error = "Exit code 255\nkex_exchange_identification: Connection closed by remote host"
    out, _ = hook.post_tool_use_failure({"tool_name": "Bash", "error": error})
    if not hinted:
        assert out is None, "unsandboxed, sudo works: the host really closed the connection"
        return
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "ProxyCommand" in text and "ttp ssh-preflight" in text and "references/ssh-preflight.md" in text
    # Another failure in a sandboxed run gets no such hint.
    assert hook.post_tool_use_failure({"tool_name": "Bash", "error": "Exit code 1\nNo such file"})[0] is None
