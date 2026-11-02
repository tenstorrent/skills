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
    assert env_file({}) == {"CLAUDE_ENV_FILE": str(SCRIPT)}
    assert env_file({"CLAUDE_ENV_FILE": "/x/env.sh"}) == {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: "/x/env.sh"}
    assert env_file({"CLAUDE_ENV_FILE": str(SCRIPT)}) == {"CLAUDE_ENV_FILE": str(SCRIPT)}


def test_loopproxy_claude_worker_env_names_the_script(monkeypatch):
    from ttp.loopproxy import SCRIPT
    from ttp.providers.claude import Claude
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    monkeypatch.setattr(Claude, "supports", lambda self, flag: False)
    monkeypatch.setattr(Claude, "hook_rewrites", lambda self: False)
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
