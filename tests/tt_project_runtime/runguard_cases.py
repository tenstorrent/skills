# Run credential and proxy guard tests (runguard.py, through hook.py's PreToolUse) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file. Add new run credential and proxy guard tests here.
from __future__ import annotations

PROXY_PORT = "40295"


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    """A worker run's environment as the runner sets it (token, proxy port), in a scratch home."""
    for k in ("HOME", "XDG_CONFIG_HOME", "GH_CONFIG_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "home"))
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ttp-run-token-0123456789")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{PROXY_PORT}")
    monkeypatch.setenv("TTP_RUN_PROXY_PORT", PROXY_PORT)
    return tmp_path


def _run_guard_decision(cmd):
    from ttp import hook
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}})
    out = (out or {}).get("hookSpecificOutput", {})
    return out.get("permissionDecision"), out.get("permissionDecisionReason", "")


HARBOR_CMD = ("harbor run -d terminal-bench@2.0 -a claude-code --ae ANTHROPIC_AUTH_TOKEN=$ANTHROPIC_AUTH_TOKEN "
              "--ae ANTHROPIC_BASE_URL=http://172.17.0.1:40300")
IPTABLES_CMD = f"sudo -n iptables -A INPUT -p tcp --dport {PROXY_PORT} -d 172.17.0.1 -j ACCEPT"
SOCAT_CMD = (f"nohup socat TCP-LISTEN:40300,bind=172.17.0.1,fork,reuseaddr TCP:127.0.0.1:{PROXY_PORT} "
             ">/tmp/socat.log 2>&1 &")


def test_run_guard_refuses_the_three_commands_of_the_proxy_incident(run_env):
    """The harbor agent env, the firewall rule and the socat listener a run started to expose its own proxy."""
    for cmd in (HARBOR_CMD, IPTABLES_CMD, SOCAT_CMD):
        decision, why = _run_guard_decision(cmd)
        assert decision == "deny", cmd
        assert "ttp secret env <name> -- <command>" in why
    assert "model credentials" in _run_guard_decision(HARBOR_CMD)[1]
    assert f"model proxy (port {PROXY_PORT}" in _run_guard_decision(SOCAT_CMD)[1]
    assert f"model proxy (port {PROXY_PORT}" in _run_guard_decision(IPTABLES_CMD)[1]


@pytest.mark.parametrize("cmd", [
    "docker run -e ANTHROPIC_AUTH_TOKEN=$ANTHROPIC_AUTH_TOKEN img",
    'docker run -e "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" img',
    "docker run --env ANTHROPIC_AUTH_TOKEN img",
    "docker run --env=CLAUDE_CODE_OAUTH_TOKEN img",
    "docker exec -e ANTHROPIC_API_KEY c claude -p hi",
    "podman run -e ANTHROPIC_AUTH_TOKEN img",
    "docker compose run -e ANTHROPIC_AUTH_TOKEN=$ANTHROPIC_AUTH_TOKEN app",
    "harbor run --agent-env ANTHROPIC_AUTH_TOKEN=$ANTHROPIC_AUTH_TOKEN",
    "harbor run --ae ANTHROPIC_API_KEY",
    "docker run -e ANTHROPIC_AUTH_TOKEN=ttp-run-token-0123456789 img",
    "sudo docker run -e ANTHROPIC_AUTH_TOKEN=$ANTHROPIC_AUTH_TOKEN img",
    "bash -c 'docker run -e ANTHROPIC_AUTH_TOKEN=$ANTHROPIC_AUTH_TOKEN img'",
    "docker run -eANTHROPIC_AUTH_TOKEN img",
    "podman run -eANTHROPIC_API_KEY img",
    "env > .env && docker run --env-file .env img",
    "docker run --env-file <(env) img",
    "printenv > run.env; docker run --env-file=run.env img",
    "export -p | tee agent.env && harbor run --env-file agent.env",
    "docker run --env-file <(set) img",
    "echo ANTHROPIC_AUTH_TOKEN=$ANTHROPIC_AUTH_TOKEN > agent.env",
    "echo \"ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY\" | tee .env.agent",
    "printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\\n' \"$CLAUDE_CODE_OAUTH_TOKEN\" >> docker.env",
])
def test_run_guard_refuses_handing_the_runs_credentials_to_a_container_or_agent(run_env, cmd):
    decision, why = _run_guard_decision(cmd)
    assert decision == "deny" and "ttp secret env" in why


@pytest.mark.parametrize("cmd", [
    "socat TCP-LISTEN:40300,fork TCP:127.0.0.1:40295",
    "socat TCP4-LISTEN:40300,bind=0.0.0.0,fork TCP:localhost:40295",
    "socat TCP-LISTEN:40300,bind=172.17.0.1,fork TCP:127.0.0.1:40295",
    "socat TCP6-LISTEN:40300,bind=[::],fork TCP:127.0.0.1:40295",
    "sudo socat TCP-LISTEN:40300,bind=10.1.2.3 TCP:127.0.0.1:40295",
    "ssh -N -L 0.0.0.0:40300:127.0.0.1:40295 host",
    "ssh -N -L *:40300:127.0.0.1:40295 host",
    "ssh -N -R 0.0.0.0:40300:127.0.0.1:40295 host",
    "ssh -N -L 172.17.0.1:40300:127.0.0.1:40295 host",
    "ssh -g -N -L 40300:127.0.0.1:40295 host",
    "ssh -gN -L 40300:127.0.0.1:40295 host",
    "ssh -o GatewayPorts=yes -N -L 40300:127.0.0.1:40295 host",
    "ssh -o GatewayPorts=clientspecified -N -L 40300:127.0.0.1:40295 host",
    "ssh -oGatewayPorts=yes -N -L 40300:127.0.0.1:40295 host",
    "ssh -o 'GatewayPorts yes' -N -L 40300:127.0.0.1:40295 host",
    "autossh -M 0 -g -N -L 40300:127.0.0.1:40295 host",
    "nc -l 0.0.0.0 40300 | nc 127.0.0.1 40295",
    "ncat -lk 40300 -c 'ncat 127.0.0.1 40295'",
    "sudo iptables -A INPUT -p tcp --dport 40295 -j ACCEPT",
    "sudo ip6tables -I INPUT -p tcp --dport 40295 -j ACCEPT",
    "sudo ufw allow 40295/tcp",
    "sudo nft add rule inet filter input tcp dport 40295 accept",
    "bash -c 'sudo iptables -A INPUT -p tcp --dport 40295 -j ACCEPT'",
    "python3 -c \"import socketserver; socketserver.TCPServer(('0.0.0.0', 40300), H); print(40295)\"",
])
def test_run_guard_refuses_exposing_the_runs_proxy(run_env, cmd):
    decision, why = _run_guard_decision(cmd)
    assert decision == "deny" and f"port {PROXY_PORT}" in why, cmd


@pytest.mark.parametrize("cmd", [
    # a stored secret goes in by the safe route, a literal value is no credential
    "ttp secret env anthropic -- docker run -e ANTHROPIC_API_KEY img",
    "ttp secret env --secret agent_key=ANTHROPIC_API_KEY -- harbor run --ae ANTHROPIC_API_KEY",
    "docker run -e FOO=bar img",
    "docker run -eFOO=bar img",
    "docker run -eANTHROPIC_AUTH_TOKEN=fake-for-a-mock img",
    "docker run --env-file config.env img",
    "env | sort > /tmp/envlist.txt",
    "env FOO=bar docker run img",
    "ttp secret env anthropic -- docker run -eANTHROPIC_API_KEY img",
    "docker run -e ANTHROPIC_AUTH_TOKEN=fake-for-a-mock img",
    "docker run -e OTHER=$HOME img",
    "docker compose up -d",
    "harbor run -d terminal-bench@2.0 -a claude-code",
    "echo FOO=bar > agent.env",
    "echo ANTHROPIC_AUTH_TOKEN=$ANTHROPIC_AUTH_TOKEN",
    "echo $ANTHROPIC_AUTH_TOKEN | wc -c",
    # forwarders on loopback only, other ports, other firewall changes
    "socat TCP-LISTEN:40300,bind=127.0.0.1,fork TCP:127.0.0.1:40295",
    "socat TCP-LISTEN:40300,bind=localhost,fork TCP:127.0.0.1:40295",
    "ssh -N -L 127.0.0.1:40300:127.0.0.1:40295 host",
    "ssh -N -L 40300:127.0.0.1:40295 host",
    "ssh -o GatewayPorts=no -N -L 40300:127.0.0.1:40295 host",
    "ssh -g -N -L 40300:127.0.0.1:8080 host",
    "ssh -N -L 0.0.0.0:40300:127.0.0.1:8080 host",
    "nc -l 127.0.0.1 40300",
    "nc -l 0.0.0.0 40300",
    "socat TCP-LISTEN:40300,fork TCP:127.0.0.1:8080",
    "sudo -n iptables -A INPUT -p tcp --dport 8080 -j ACCEPT",
    "sudo iptables -A INPUT -p tcp --dport 40295 -j DROP",
    "sudo iptables -D INPUT -p tcp --dport 40295 -j ACCEPT",
    "sudo ufw allow 8080/tcp",
    "sudo systemctl restart docker",
    "curl -s http://127.0.0.1:40295/health",
])
def test_run_guard_allows_the_safe_routes_and_everything_else(run_env, cmd):
    assert _run_guard_decision(cmd)[0] is None, cmd


def test_run_guard_knows_the_proxy_port_from_the_runner_or_the_base_url(run_env, monkeypatch):
    from ttp import runguard
    monkeypatch.delenv("TTP_RUN_PROXY_PORT")
    assert runguard.proxy_ports() == {PROXY_PORT}   # the loopback base url alone, any provider
    monkeypatch.setenv("TTP_RUN_PROXY_PORT", "41000, 41001,x")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:42000/v1")
    monkeypatch.setenv("OTHER_BASE_URL", "https://api.example.com:443/v1")   # not loopback: not ours
    assert runguard.proxy_ports() == {"41000", "41001", "42000", PROXY_PORT}
    assert _run_guard_decision("socat TCP-LISTEN:5000,fork TCP:127.0.0.1:41001")[0] == "deny"
    assert _run_guard_decision("socat TCP-LISTEN:5000,fork TCP:127.0.0.1:42000")[0] == "deny"
    assert _run_guard_decision("socat TCP-LISTEN:5000,fork TCP:127.0.0.1:43000")[0] is None
    monkeypatch.delenv("ANTHROPIC_BASE_URL")
    monkeypatch.delenv("TTP_RUN_PROXY_PORT")
    monkeypatch.delenv("OPENAI_BASE_URL")
    assert runguard.proxy_ports() == set()
    assert _run_guard_decision(SOCAT_CMD)[0] is None   # no known port: nothing to protect


def test_run_guard_is_no_sudo_block_or_sandbox_and_does_nothing_outside_a_run(run_env, monkeypatch):
    for cmd in ("sudo -n true", "sudo apt-get install -y socat", "sudo iptables -L -n", "docker ps",
                "docker run --rm -p 127.0.0.1:8080:80 nginx"):
        assert _run_guard_decision(cmd)[0] is None, cmd
    monkeypatch.delenv("TTP_RUN_DIR")   # outside a worker run the hook does nothing
    from ttp import hook
    assert hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": HARBOR_CMD}}) == (None, None)


def test_run_guard_judges_the_same_on_every_provider(run_env, monkeypatch):
    """Cursor and Codex without hooks reach the hook through the stream guard, which runs it with the run's env."""
    from ttp import hook, streamguard
    import inspect
    assert "hook" in inspect.getsource(streamguard)
    assert hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": SOCAT_CMD}})[0]["hookSpecificOutput"][
        "permissionDecision"] == "deny"


def test_run_guard_never_raises_and_passes_what_it_cannot_judge(run_env, monkeypatch):
    from ttp import runguard
    assert runguard.refusal("", []) is None
    assert runguard.refusal("socat 'unterminated", [["socat", "'unterminated"]]) is None
    monkeypatch.setattr(runguard, "cred_refusal", lambda *a, **k: 1 / 0)
    assert runguard.refusal(HARBOR_CMD, [["harbor"]]) is None


def test_runner_exports_the_proxy_port_for_the_guard():
    src = (pathlib.Path(__file__).resolve().parents[1] / "plugins/tt-project/runtime/ttp/runner.py").read_text()
    assert 'env["TTP_RUN_PROXY_PORT"] = bridge.url.rsplit(":", 1)[-1]' in src
