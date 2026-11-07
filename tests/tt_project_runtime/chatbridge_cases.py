# The chat provider's loopback bridge (chatbridge.py): Claude Code's Messages API to an upstream
# Chat Completions endpoint. A mock endpoint on 127.0.0.1 stands in for it; no test reaches the network.
# Run from tests/test_tt_project_runtime.py (see _include_cases there): helpers, fixtures and imports
# come from that file.
from __future__ import annotations

import http.server

_CB_KEY = "sk-upstream-placeholder-9f8e7d6c5b4a"


class _MockChat:
    """A Chat Completions endpoint on loopback: records each request, answers from `replies`
    (status, JSON body) or `(200, [chunks])` for a stream."""

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []
        self.models = None   # the ids GET /models lists; None: it answers 404
        mock = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                mock.seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
                data = json.dumps({"data": [{"id": i} for i in mock.models or []]}).encode()
                self.send_response(404 if mock.models is None else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                mock.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                status, reply = mock.replies.pop(0) if mock.replies else (200, {"choices": []})
                if isinstance(reply, list):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for c in reply:
                        self.wfile.write(b"data: " + (c if isinstance(c, bytes) else json.dumps(c).encode()) + b"\n\n")
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def mock_chat():
    made = []

    def make(*replies):
        made.append(_MockChat(replies))
        return made[-1]
    yield make
    for m in made:
        m.close()


def _post(bridge, body, token=None, path="/v1/messages"):
    req = urllib.request.Request(bridge.url + path, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "x-api-key": bridge.token if token is None else token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _cb_events(sse: str) -> list[dict]:
    return [json.loads(line[6:]) for line in sse.splitlines() if line.startswith("data: ")]


def test_chatbridge_maps_a_messages_request_to_chat_completions():
    from ttp import chatbridge
    body = {"model": "claude-x", "max_tokens": 32000, "stream": True, "temperature": 0.2,
            "thinking": {"type": "enabled", "budget_tokens": 1000}, "metadata": {"user_id": "u"},
            "system": [{"type": "text", "text": "You are a worker.", "cache_control": {"type": "ephemeral"}},
                       {"type": "text", "text": "Follow the charter."}],
            "tools": [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}},
                      {"type": "web_search_20250305", "name": "web_search"}],
            "tool_choice": {"type": "any"},
            "messages": [
                {"role": "user", "content": "list files"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "secret plan", "signature": "s"},
                    {"type": "text", "text": "Running two."},
                    {"type": "tool_use", "id": "call_a", "name": "Bash", "input": {"command": "ls"}},
                    {"type": "tool_use", "id": "call_b", "name": "Bash", "input": {"command": "pwd"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call_a", "content": [{"type": "text", "text": "a.py"}]},
                    {"type": "tool_result", "tool_use_id": "call_b", "content": "boom", "is_error": True},
                    {"type": "text", "text": "go on"}]}]}
    req = chatbridge.to_chat(body, "up-model", cap=4096)
    assert req["model"] == "up-model" and req["max_tokens"] == 4096 and req["temperature"] == 0.2
    assert req["stream"] is True and req["stream_options"] == {"include_usage": True}
    assert "thinking" not in req and "metadata" not in req
    assert req["tool_choice"] == "required"
    assert req["tools"] == [{"type": "function", "function": {"name": "Bash", "description": "run",
                                                              "parameters": {"type": "object"}}}]
    m = req["messages"]
    assert m[0] == {"role": "system", "content": "You are a worker.\nFollow the charter."}
    assert m[1] == {"role": "user", "content": "list files"}
    assert m[2]["role"] == "assistant" and m[2]["content"] == "Running two." and "secret plan" not in json.dumps(m)
    assert [(c["id"], json.loads(c["function"]["arguments"])) for c in m[2]["tool_calls"]] == [
        ("call_a", {"command": "ls"}), ("call_b", {"command": "pwd"})]
    # Tool results answer the calls right after them, by id, before the user's own text.
    assert m[3] == {"role": "tool", "tool_call_id": "call_a", "content": "a.py"}
    assert m[4] == {"role": "tool", "tool_call_id": "call_b", "content": "Error: boom"}
    assert m[5] == {"role": "user", "content": "go on"}
    # The cap only lowers: a smaller request keeps its own, no cap keeps the request's.
    assert chatbridge.to_chat({"max_tokens": 100, "messages": []}, "m", cap=4096)["max_tokens"] == 100
    assert chatbridge.to_chat({"max_tokens": 9000, "messages": []}, "m")["max_tokens"] == 9000
    assert chatbridge.to_chat({"tools": body["tools"], "tool_choice": {"type": "tool", "name": "Bash"},
                               "messages": []}, "m")["tool_choice"] == {"type": "function", "function": {"name": "Bash"}}


def test_chatbridge_tool_input_takes_the_first_object_of_concatenated_arguments():
    from ttp.chatbridge import tool_input
    assert tool_input('{"command": "ls"}{"command": "pwd"}') == {"command": "ls"}
    assert tool_input("") == {} and tool_input("not json") == {} and tool_input("[1]") == {}


def test_chatbridge_streams_text_and_interleaved_parallel_tool_calls(mock_chat):
    from ttp.chatbridge import Bridge
    up = mock_chat((200, [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "hidden thoughts"}}]},
        {"choices": [{"index": 0, "delta": {"content": "Checking "}}]},
        {"choices": [{"index": 0, "delta": {"content": "both."}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "type": "function", "function": {"name": "Bash", "arguments": ""}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 1, "id": "call_b", "type": "function", "function": {"name": "Read", "arguments": '{"file_'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"command": '}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 1, "function": {"arguments": 'path": "a.py"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"ls"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 2, "id": "call_c", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "pwd"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 42,
                                  "prompt_tokens_details": {"cached_tokens": 900}}},
    ]))
    with Bridge(up.url, _CB_KEY, "up-model", max_tokens=2048) as b:
        status, text = _post(b, {"model": "claude-x", "max_tokens": 32000, "stream": True,
                                 "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200
    ev = _cb_events(text)
    assert ev[0]["type"] == "message_start" and ev[-1]["type"] == "message_stop"
    deltas = [e["delta"]["text"] for e in ev if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta"]
    assert "".join(deltas) == "Checking both." and "hidden thoughts" not in text
    tools = [e["content_block"] for e in ev if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"]
    args = [json.loads(e["delta"]["partial_json"]) for e in ev
            if e["type"] == "content_block_delta" and e["delta"]["type"] == "input_json_delta"]
    assert [(t["id"], t["name"]) for t in tools] == [("call_a", "Bash"), ("call_b", "Read"), ("call_c", "Bash")]
    assert args == [{"command": "ls"}, {"file_path": "a.py"}, {"command": "pwd"}]
    starts = [e["index"] for e in ev if e["type"] == "content_block_start"]
    stops = [e["index"] for e in ev if e["type"] == "content_block_stop"]
    assert starts == stops == [0, 1, 2, 3]
    final = next(e for e in ev if e["type"] == "message_delta")
    assert final["delta"]["stop_reason"] == "tool_use"
    assert final["usage"] == {"input_tokens": 100, "output_tokens": 42, "cache_read_input_tokens": 900,
                              "cache_creation_input_tokens": 0}
    sent = up.seen[0]
    assert sent["path"] == "/v1/chat/completions" and sent["auth"] == f"Bearer {_CB_KEY}"
    assert sent["body"]["model"] == "up-model" and sent["body"]["max_tokens"] == 2048


def test_chatbridge_non_streamed_reply_drops_reasoning_and_maps_stop_and_usage(mock_chat):
    from ttp.chatbridge import Bridge
    up = mock_chat((200, {"choices": [{"message": {"role": "assistant", "content": "done",
                                                   "reasoning_content": "private chain"},
                                       "finish_reason": "length"}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}}),
                   (200, {"choices": [{"message": {"content": None, "tool_calls": [
                       {"id": "call_x", "type": "function", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}},
                       {"id": "call_y", "type": "function", "function": {"name": "Bash", "arguments": '{"command":"id"}'}}]},
                       "finish_reason": "stop"}]}))
    with Bridge(up.url, _CB_KEY, "up-model") as b:
        _, first = _post(b, {"max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
        _, second = _post(b, {"max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
    first, second = json.loads(first), json.loads(second)
    assert first["content"] == [{"type": "text", "text": "done"}] and "private chain" not in json.dumps(first)
    assert first["stop_reason"] == "max_tokens" and first["usage"]["input_tokens"] == 10
    assert first["usage"]["output_tokens"] == 5
    assert second["stop_reason"] == "tool_use"
    assert [(c["id"], c["input"]) for c in second["content"]] == [("call_x", {"command": "ls"}), ("call_y", {"command": "id"})]
    assert "stream" not in up.seen[0]["body"]


@pytest.mark.parametrize("status,code,kind,cls", [
    (401, 401, "authentication_error", "auth"), (403, 401, "authentication_error", "auth"),
    (402, 429, "rate_limit_error", "funds"), (429, 429, "rate_limit_error", "limit"),
    (503, 529, "overloaded_error", "retry"), (529, 529, "overloaded_error", "retry"),
    (500, 500, "api_error", "retry"), (502, 502, "api_error", "retry"),
    (400, 400, "invalid_request_error", "request")])
def test_chatbridge_maps_upstream_errors_to_messages_errors_the_runner_classifies(mock_chat, status, code, kind, cls):
    from ttp.chatbridge import Bridge
    from ttp.providers.base import AUTH_RE, FUNDS_RE, LIMIT_RE, request_rejected
    up = mock_chat((status, {"error": {"message": f"bad things for key {_CB_KEY}"}}))
    with Bridge(up.url, _CB_KEY, "up-model") as b:
        got, text = _post(b, {"max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
    body = json.loads(text)
    assert got == code and body["type"] == "error" and body["error"]["type"] == kind
    assert _CB_KEY not in text   # an upstream that echoes the key gets it scrubbed
    # What Claude Code prints for it: "API Error: <code> <body>".
    said = f"API Error: {code} {text}"
    assert bool(AUTH_RE.search(said)) == (cls == "auth")
    assert bool(FUNDS_RE.search(said)) == (cls == "funds")
    assert bool(LIMIT_RE.search(said)) == (cls in ("limit", "funds"))
    assert request_rejected(said) == (cls == "request")


def test_chatbridge_unreachable_upstream_and_stream_error_chunk(mock_chat):
    from ttp.chatbridge import Bridge
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}/v1"
    with Bridge(dead, _CB_KEY, "m") as b:
        code, text = _post(b, {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
    assert code == 500 and json.loads(text)["error"]["type"] == "api_error"
    up = mock_chat((200, [{"choices": [{"delta": {"content": "par"}}]},
                          {"error": {"message": "rate limited", "code": 429}}]))
    with Bridge(up.url, _CB_KEY, "m") as b:
        code, text = _post(b, {"max_tokens": 5, "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    ev = _cb_events(text)
    assert ev[-1]["type"] == "error" and ev[-1]["error"]["type"] == "rate_limit_error"


def test_chatbridge_serves_only_the_run_token_on_loopback(mock_chat):
    from ttp.chatbridge import Bridge
    up = mock_chat()
    with Bridge(up.url, _CB_KEY, "m") as b:
        assert b.server.server_address[0] == "127.0.0.1" and b.server.server_address[1] > 0
        for token in ("", "wrong", _CB_KEY):
            code, text = _post(b, {"max_tokens": 5, "messages": []}, token=token)
            assert code == 401 and json.loads(text)["error"]["type"] == "authentication_error"
        code, text = _post(b, {"messages": [{"role": "user", "content": "x" * 400}]}, path="/v1/messages/count_tokens")
        assert code == 200 and json.loads(text)["input_tokens"] > 50
        env = b.env()
        assert env["ANTHROPIC_BASE_URL"] == b.url and env["ANTHROPIC_AUTH_TOKEN"] == b.token
        assert _CB_KEY not in json.dumps(env)
    assert up.seen == []   # nothing unauthorized reached the upstream
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", int(b.url.rsplit(":", 1)[1])), timeout=2).close()


def test_chatbridge_settings_and_config(env, monkeypatch):
    from ttp import chatbridge, project
    cfg = {"providers": {"chat": {"endpoint": "cloud", "base_url": "https://api.example.com/v1", "max_tokens": 8192,
                                  "tiers": {"light": {"model": "small-model", "max_tokens": 4096}}}}}
    assert chatbridge.settings(cfg, "light", "small-model") == {
        "endpoint": "cloud", "base_url": "https://api.example.com/v1", "model": "small-model", "max_tokens": 4096}
    assert chatbridge.settings(cfg, "standard", "m")["max_tokens"] == 8192
    assert chatbridge.config_problems(cfg) == [] and chatbridge.config_problems({}) == []
    bad = chatbridge.config_problems({"providers": {"chat": {"endpoint": "a b", "base_url": "ftp://x",
                                                             "max_tokens": "lots"}}})
    assert len(bad) == 3 and any("providers.chat.endpoint" in b for b in bad)
    for key in ("providers.chat.endpoint", "providers.chat.base_url", "providers.chat.tiers.light.max_tokens"):
        assert project.unknown_key_hint(key) is None
    # Off by default: no chat entry in the defaults.
    assert "chat" not in project.DEFAULT_CONFIG["providers"]
    from ttp.providers import get_provider
    prov = get_provider("chat")
    assert prov.reach_host({}) == "" and prov.login_check() is None and prov.account() == ""


def test_chatbridge_key_is_bound_to_its_saved_url(env, monkeypatch):
    from ttp import chatbridge, endpoint, project
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "secrets-home")
    s = {"endpoint": "cloud", "base_url": "https://api.example.com/v1", "model": "m", "max_tokens": 0}
    with pytest.raises(chatbridge.BridgeError, match="no key saved"):
        chatbridge.Bridge.from_settings(s)
    endpoint.save("cloud", "https://api.example.com/v1", _CB_KEY, True, "HTTP 200")
    assert chatbridge.Bridge.from_settings(s).base_url == "https://api.example.com/v1"
    with pytest.raises(chatbridge.BridgeError, match="only to the URL"):
        chatbridge.Bridge.from_settings({**s, "base_url": "https://other.example.com/v1"})
    with pytest.raises(chatbridge.BridgeError, match="no model"):
        chatbridge.Bridge.from_settings({**s, "model": ""})


_CB_AGENT = r'''
import json, os, sys, time, urllib.request
out = sys.argv[1]
time.sleep(float(os.environ.get("CB_AGENT_SLOW") or 0))   # a loaded host: a slow start-up
url, token = os.environ["ANTHROPIC_BASE_URL"], os.environ["ANTHROPIC_AUTH_TOKEN"]
req = urllib.request.Request(url + "/v1/messages?beta=true", data=json.dumps({
    "model": "claude-x", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
reply = json.loads(urllib.request.urlopen(req, timeout=30).read())
with open(out + ".tmp", "w") as f:
    json.dump({"url": url, "reply": reply, "env": dict(os.environ), "argv": sys.argv}, f)
os.rename(out + ".tmp", out)
print(json.dumps({"type": "result", "result": reply["content"][0]["text"]}), flush=True)
if len(sys.argv) > 2:
    clock = os.environ.get("TTP_TEST_AWAKE_FILE")
    if clock:   # the runner's test clock: its limit counts this work, not start-up delays
        with open(clock + ".tmp", "w") as f:
            f.write(sys.argv[2])
        os.replace(clock + ".tmp", clock)
    time.sleep(float(sys.argv[2]))
'''


def _chat_run(tmp_path, mock, extra=(), timeout_s=60, alias="cloud", bridge=None):
    home = tmp_path / "ttp-home"
    home.mkdir(mode=0o700, exist_ok=True)
    (home / "secrets.json").write_text(json.dumps({f"endpoint/{alias}": {"url": mock.url, "key": _CB_KEY}}))
    agent = tmp_path / "agent.py"
    agent.write_text(_CB_AGENT)
    seen = tmp_path / "agent.json"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    (run_dir / "run.json").write_text(json.dumps({
        "argv": [sys.executable, str(agent), str(seen), *extra], "env": {}, "cwd": str(tmp_path),
        "timeout_s": timeout_s, "provider": "chat",
        "bridge": {"endpoint": "cloud", "base_url": mock.url, "model": "up-model", "max_tokens": 64, **(bridge or {})}}))
    env = {**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_HOME": str(home)}
    env.pop("ANTHROPIC_BASE_URL", None)
    env.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW", None)
    return run_dir, seen, env


def _port_closed(url: str) -> bool:
    try:
        socket.create_connection(("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=2).close()
    except OSError:
        return True
    return False


def test_runner_runs_a_chat_agent_through_its_bridge_and_stops_it_at_the_end(env, tmp_path, mock_chat):
    up = mock_chat((200, {"choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}]}))
    run_dir, seen, run_env = _chat_run(tmp_path, up)
    subprocess.run([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME), env=run_env,
                   timeout=_load_scaled(120))
    info = json.loads((run_dir / "exit.json").read_text())
    assert info["rc"] == 0, (run_dir / "runner.log").read_text() if (run_dir / "runner.log").exists() else info
    got = json.loads(seen.read_text())
    assert got["reply"]["content"] == [{"type": "text", "text": "pong"}]
    assert got["url"].startswith("http://127.0.0.1:") and _port_closed(got["url"])
    assert up.seen[0]["auth"] == f"Bearer {_CB_KEY}" and up.seen[0]["body"]["max_tokens"] == 10
    # The key is in no run file, not in the agent's environment or argv, not in run.json.
    assert _CB_KEY not in json.dumps(got["env"]) and _CB_KEY not in json.dumps(got["argv"])
    for f in run_dir.rglob("*"):
        if f.is_file():
            assert _CB_KEY.encode() not in f.read_bytes(), f


def test_runner_stops_the_chat_bridge_on_timeout_and_when_killed(env, tmp_path, mock_chat):
    up = mock_chat(*[(200, {"choices": [{"message": {"content": "pong"}}]})] * 2)
    run_dir, seen, run_env = _chat_run(tmp_path / "t", up, extra=("120",), timeout_s=1) if (tmp_path / "t").mkdir() is None else None
    # The 1 s limit runs on the runner's test clock, which only the agent's own work advances: a start-up
    # slower than the limit (a loaded host, here 1.5 s) never times the agent out before it has the bridge.
    run_env.update(TTP_TEST_AWAKE_FILE=str(tmp_path / "t" / "awake-clock"), CB_AGENT_SLOW="1.5")
    subprocess.run([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME), env=run_env,
                   timeout=_load_scaled(120))
    assert json.loads((run_dir / "exit.json").read_text())["stopped"] == "timeout"
    assert _port_closed(json.loads(seen.read_text())["url"])
    # A runner killed outright takes its bridge thread with it, and the watchdog ends the agent.
    (tmp_path / "k").mkdir()
    run_dir, seen, run_env = _chat_run(tmp_path / "k", up, extra=("120",))
    proc = subprocess.Popen([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME), env=run_env)
    try:
        assert _wait_for(seen.exists, _load_scaled(60))
        agent = int((run_dir / "child.pid").read_text().split()[0])
        proc.kill()
        proc.wait(timeout=30)
        assert _port_closed(json.loads(seen.read_text())["url"])
        assert _wait_for(lambda: _pid_gone(agent), 30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_runner_refuses_a_chat_run_without_a_saved_key(env, tmp_path, mock_chat):
    up = mock_chat()
    run_dir, seen, run_env = _chat_run(tmp_path, up, alias="other")
    subprocess.run([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME), env=run_env,
                   timeout=_load_scaled(60))
    info = json.loads((run_dir / "exit.json").read_text())
    assert info["launched"] is False and "chat bridge" in info["launch_error"]
    assert "failed to authenticate" in info["launch_error"] and not seen.exists() and up.seen == []


def test_runner_books_a_chat_bridge_without_a_usable_key_for_the_auth_gate(env, tmp_path, mock_chat):
    up = mock_chat()
    for case, change in (("missing", {}), ("other url", {"base_url": "https://other.example.com/v1"}),
                         ("no model", {"model": ""})):
        (tmp_path / case).mkdir()
        run_dir, seen, run_env = _chat_run(tmp_path / case, up, alias="other" if case == "missing" else "cloud")
        spec = json.loads((run_dir / "run.json").read_text())
        spec["bridge"].update(change)
        (run_dir / "run.json").write_text(json.dumps(spec))
        subprocess.run([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME), env=run_env,
                       timeout=_load_scaled(60))
        info = json.loads((run_dir / "exit.json").read_text())
        assert info["launched"] is False and not seen.exists() and up.seen == [], case
        if case == "no model":
            # A bridge that cannot start for another reason stays a launch failure.
            assert "no model" in info["launch_error"] and "launch_auth" not in info
        else:
            assert info["launch_error"].startswith("could not start the agent: chat bridge: failed to authenticate"), \
                info["launch_error"]
            assert info["launch_auth"] is True, case


def _chat_config(p):
    p.set_config("providers.chat", {"endpoint": "cloud", "base_url": "https://api.example.com/v1",
                                    "tiers": {"standard": {"model": "org/Base-Model-2"}}})


def test_a_chat_run_without_a_usable_key_goes_to_the_auth_gate(env, monkeypatch):
    from ttp import alerts
    from ttp.daemon import Daemon
    p = make(env)
    _chat_config(p)
    d = Daemon(p.base)
    seen = []
    monkeypatch.setattr(d, "observe", lambda source, text, *a, **k: seen.append(source))
    why = ("could not start the agent: chat bridge: failed to authenticate: providers.chat.base_url is not the URL "
           "saved with endpoint 'cloud': the key goes only to the URL it was saved for")
    ends = []
    for launch_auth in (True, False):
        tid = p.db.add_task("build", "", kind="work", tier="standard", priority=3, budget_usd=8.0, labels=[],
                            provider="chat")
        p.db.update_task(tid, status="running")
        rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,boot_id) "
                     "VALUES(?,'worker','chat','org/Base-Model-2',?,'running',?)", (tid, time.time() - 5, d.boot))
        rd = p.runs / str(rid)
        rd.mkdir(parents=True)
        p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(rd), rid))
        d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                     {"rc": None, "started": time.time() - 5, "ended": time.time(), "launched": False,
                      "launch_error": why, **({"launch_auth": True} if launch_auth else {})})
        ends.append((p.db.one("SELECT * FROM runs WHERE id=?", (rid,)), p.db.task(tid)))
        if launch_auth:
            # The logged-out pause: the breaker is open, nothing more starts on chat until a check passes.
            assert d._logged_out("chat") and alerts.breaker(p.db, "chat")
            assert not d._may_probe("chat", time.time()), "the first check waits for check_logins"
            assert p.db.q("SELECT id FROM messages WHERE ref='auth:chat'")
            assert "daemon:launch" not in seen, "not reported as a harness launch failure"
            p.db.x("DELETE FROM kv WHERE key=?", (alerts.BREAKER + "chat",))
    (run, task), (plain, _) = ends
    assert run["status"] == "auth" and not run["cost_usd"]
    assert "failed to authenticate" in json.loads(run["note"])["launch_error"]
    assert task["status"] == "queued" and int(task["attempts"] or 0) == 0 and task["tier"] == "standard", \
        "an auth failure costs no attempt and is not retried a tier up"
    # Without the runner's mark the same text stays a launch failure (only the bridge decides it is auth).
    assert plain["status"] == "failed" and "daemon:launch" in seen


# Upstream model ids as chat endpoints name them: '/', '.', ':', '-', '_', '@' and uppercase.
CHAT_MODELS = ("org-name/Model-V2.5-32B-Instruct:free", "accounts/acme/models/model_v3p1@2026-01", "Big.Model:Q4_K_M")


def test_a_chat_model_pin_is_kept_as_given(env):
    from ttp import failover
    from ttp.coordinator import apply
    p = make(env)
    _chat_config(p)
    for model in CHAT_MODELS:
        assert apply(p, [{"type": "task_add", "title": f"on {model}", "spec": "x", "provider": "chat",
                          "model": model, "effort": "high"}]) == []
        t = p.db.one("SELECT * FROM tasks WHERE title=?", (f"on {model}",))
        assert (t["provider"], t["model"], t["effort"]) == ("chat", model, "high"), t
        # An update of the model alone keeps the provider and takes the new id as it is.
        assert apply(p, [{"type": "task_update", "id": t["id"], "model": model.upper()}]) == []
        assert (p.db.task(t["id"])["provider"], p.db.task(t["id"])["model"]) == ("chat", model.upper())
    # pin_required names chat: the mark is set.
    assert apply(p, [{"type": "task_add", "title": "user asked for chat", "spec": "y", "provider": "chat",
                      "model": CHAT_MODELS[0], "pin_required": True}]) == []
    assert failover.required(p.db, p.db.one("SELECT * FROM tasks WHERE title='user asked for chat'"))


def test_failover_off_a_logged_out_chat_drops_its_model_and_keeps_a_required_pin(env):
    from ttp.daemon import Daemon
    p = make(env)
    _chat_config(p)
    d = Daemon(p.base)
    core = d.cfg.get("core_provider") or "claude"
    pref = p.db.add_task("preferred", "x", kind="work", tier="standard", provider="chat",
                         model=CHAT_MODELS[0], effort="high")
    req = p.db.add_task("required", "y", kind="work", tier="standard", provider="chat",
                        model=CHAT_MODELS[1], effort="high", labels=["pin:required"])
    d.open_breaker("chat", "chat bridge: failed to authenticate")
    d.update_gates()
    d.failover_pinned()
    t = p.db.task(pref)
    assert (t["provider"], t["model"]) == (core, None), "the chat model id must not carry to the core provider"
    assert "moved at once from chat" in t["spec"].lower()
    r = p.db.task(req)
    assert (r["provider"], r["model"], r["status"]) == ("chat", CHAT_MODELS[1], "queued"), \
        "a pin the user's words require waits for chat"
    assert not p.db.q("SELECT id FROM events WHERE kind='provider_failover' AND task=?", (req,))
    # It still waits once chat's stop is past the failover threshold.
    d._failover_checked = 0
    d.failover_pinned(every_s=0)
    assert p.db.task(req)["provider"] == "chat"


@pytest.mark.parametrize("model", CHAT_MODELS)
def test_check_pin_verifies_a_chat_model_id(tmp_path, model):
    from ttp import runner
    from ttp.providers import get_provider
    argv, _ = get_provider("chat").build(role="worker", model=model, effort="high", cwd=str(tmp_path), budget_usd=None,
                                         read_only=False, schema=None, restrictions={})
    for mode in ("lenient", "strict"):
        spec = {"provider": "chat", "argv": argv, "cwd": str(tmp_path), "pin": {"model": model, "effort": "high"},
                "pin_verify": mode, "bridge": {"endpoint": "cloud", "model": model}}
        (tmp_path / "run.json").write_text(json.dumps(spec))
        assert runner.check_pin(tmp_path, spec, {}) is None, mode
        session = json.loads((tmp_path / "run.json").read_text())["session"]
        assert session["verification"] == "verified" and session["model"] == model
    # Another model than the pin is still refused before the first turn.
    refused = runner.check_pin(tmp_path, {**spec, "pin": {"model": "org-name/other-model"}}, {})
    assert refused["refused"] == "model_mismatch"


# Prompt-too-long answers as upstreams word them: (status, body, Anthropic message start).
_CB_TOO_LONG = [
    (400, {"error": {"message": "This model's maximum context length is 4096 tokens. However, your messages "
                                "resulted in 5000 tokens. Please reduce the length of the messages.",
                     "type": "invalid_request_error", "code": "context_length_exceeded"}},
     "prompt is too long: 5000 tokens > 4096 maximum"),
    (400, {"object": "error", "message": "This model's maximum context length is 8192 tokens. However, you "
           "requested 9000 tokens (8500 in the messages, 500 in the completion).", "code": 400},
     "prompt is too long: 9000 tokens > 8192 maximum"),
    (400, {"error": {"code": "context_length_exceeded", "message": "Input is rejected."}}, "prompt is too long"),
    (400, {"error": {"code": 400, "type": "exceed_context_size_error",
                     "message": "the request exceeds the available context size, try increasing it"}},
     "prompt is too long"),
    (413, {"error": {"message": "too many tokens in the prompt"}}, "prompt is too long"),
    (422, {"error": "Input validation error: `inputs` tokens + `max_new_tokens` must be <= 4096. Given: 5000 "
                    "`inputs` tokens and 100 `max_new_tokens`"}, "prompt is too long: 5000 tokens > 4096 maximum"),
]


@pytest.mark.parametrize("status,reply,want", _CB_TOO_LONG)
def test_chatbridge_maps_an_upstream_context_length_error_to_prompt_is_too_long(mock_chat, status, reply, want):
    from ttp.chatbridge import Bridge
    from ttp.providers.base import AUTH_RE, FUNDS_RE, LIMIT_RE, request_rejected
    up = mock_chat((status, reply))
    with Bridge(up.url, _CB_KEY, "m") as b:
        code, text = _post(b, {"max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
    err = json.loads(text)["error"]
    # Anthropic's own shape and wording, which Claude Code compacts on (and reads the numbers from).
    assert code == 400 and err["type"] == "invalid_request_error" and err["message"].startswith(want), err
    said = f"API Error: {code} {text}"
    assert request_rejected(said) and not (AUTH_RE.search(said) or LIMIT_RE.search(said) or FUNDS_RE.search(said))


def test_chatbridge_maps_a_context_length_error_in_a_200_body_or_a_stream_chunk(mock_chat):
    from ttp.chatbridge import Bridge
    up = mock_chat((200, {"error": {"code": "context_length_exceeded", "message": "too long"}}),
                   (200, [{"error": {"code": "context_length_exceeded", "message": "too long"}}]),
                   (200, [{"error": {"code": 429, "message": "too many tokens per minute"}}]))
    with Bridge(up.url, _CB_KEY, "m") as b:
        code, text = _post(b, {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
        assert code == 400 and json.loads(text)["error"]["message"].startswith("prompt is too long"), text
        _, text = _post(b, {"max_tokens": 5, "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        err = _cb_events(text)[-1]["error"]
        assert err["type"] == "invalid_request_error" and err["message"].startswith("prompt is too long"), err
        # A status the chunk gives keeps its meaning: a per-minute token limit stays a rate limit.
        _, text = _post(b, {"max_tokens": 5, "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        err = _cb_events(text)[-1]["error"]
        assert err["type"] == "rate_limit_error" and "prompt is too long" not in err["message"], err


@pytest.mark.parametrize("status,reply", [
    (400, {"error": {"message": "max_tokens is too large: 100000. This model supports at most 4096 completion tokens"}}),
    (400, {"error": {"message": "Invalid value for 'tool_choice'", "code": "invalid_value"}}),
    (429, {"error": {"message": "Request too large: too many tokens per minute"}}),
])
def test_chatbridge_other_upstream_errors_are_not_prompt_too_long(mock_chat, status, reply):
    from ttp.chatbridge import Bridge, error_for
    up = mock_chat((status, reply))
    with Bridge(up.url, _CB_KEY, "m") as b:
        code, text = _post(b, {"max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
    # Exactly what the bridge returned before: the upstream body behind the status's own wording.
    assert (code, json.loads(text)) == error_for(status, json.dumps(reply)) and "too long" not in text.lower()


def test_chatbridge_context_window_sets_the_agents_compaction_window(env, tmp_path, mock_chat):
    from ttp import chatbridge, project
    cfg = {"providers": {"chat": {"endpoint": "cloud", "context_window": 65536,
                                  "tiers": {"light": {"model": "s", "context_window": 32768}, "standard": {"model": "m"}}}}}
    assert chatbridge.settings(cfg, "light", "s")["context_window"] == 32768
    assert chatbridge.settings(cfg, "standard", "m")["context_window"] == 65536
    del cfg["providers"]["chat"]["context_window"]
    assert "context_window" not in chatbridge.settings(cfg, "standard", "m"), "unset: the run.json entry is unchanged"
    assert project.unknown_key_hint("providers.chat.context_window") is None
    assert chatbridge.config_problems({"providers": {"chat": {"endpoint": "cloud", "context_window": 0}}}) == []
    bad = chatbridge.config_problems({"providers": {"chat": {"endpoint": "cloud", "tiers": {"light": {"context_window": "big"}}}}})
    assert len(bad) == 1 and "context_window" in bad[0]
    # The launch: the agent gets Claude Code's compaction window with it, and nothing new without it.
    for window in (32768, None):
        up = mock_chat((200, {"choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}]}))
        sub = tmp_path / str(window)
        sub.mkdir()
        run_dir, seen, run_env = _chat_run(sub, up, bridge={"context_window": window} if window else None)
        subprocess.run([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME), env=run_env,
                       timeout=_load_scaled(120))
        got = json.loads(seen.read_text())["env"]
        assert got.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == (str(window) if window else None)


def _cb_stream(path, *, result=True):
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 5000,
             "cache_creation_input_tokens": 0}
    lines = [{"type": "system", "subtype": "init", "session_id": "s-1", "model": "up-model"},
             {"type": "assistant", "message": {"id": "msg_1", "usage": usage, "content": [{"type": "text", "text": "ok"}]}}]
    if result:
        lines.append({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.5,
                      "usage": usage, "result": "ok", "session_id": "s-1"})
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path


_CB_PRICES = {"up-model": [2.0, 0.5, 8.0]}
_CB_PRICED = (1000 * 2.0 + 5000 * 0.5 + 200 * 8.0) / 1e6


def test_chat_spend_is_repriced_from_pricing_chat_and_estimated_without_it(tmp_path):
    from ttp.providers import get_provider
    out = _cb_stream(tmp_path / "output.jsonl")
    u = get_provider("chat").use("up-model", _CB_PRICES).parse(out)
    assert u.cost_usd == pytest.approx(_CB_PRICED) and not u.estimated and u.extra["cost_basis"] == "pricing"
    # A "default" row covers other models; a malformed row prices nothing.
    assert get_provider("chat").use("other", {"default": [2.0, 0.5, 8.0]}).parse(out).cost_usd == pytest.approx(_CB_PRICED)
    for prices in (None, {}, {"up-model": [1, 2]}, {"other": [1, 1, 1]}):
        u = get_provider("chat").use("up-model", prices).parse(out)
        assert u.cost_usd == 0.5 and u.estimated, prices   # Claude Code's fallback-rate figure, marked estimated
    # Mid-run (no result line yet): the runner's budget check reads the repriced tokens so far.
    part = _cb_stream(tmp_path / "partial.jsonl", result=False)
    assert get_provider("chat").use("up-model", _CB_PRICES).cost_so_far(part) == pytest.approx(_CB_PRICED)
    assert get_provider("chat").use("up-model", None).cost_so_far(part) is None
    # Budget: with prices the runner caps on the repriced figure, so Claude Code gets no --max-budget-usd.
    kw = dict(role="worker", model="up-model", effort="high", cwd=str(tmp_path), budget_usd=3.0, read_only=False,
              schema=None, restrictions={})
    priced, plain = get_provider("chat").use("up-model", _CB_PRICES), get_provider("chat").use("up-model", None)
    assert not priced.caps_budget and "--max-budget-usd" not in priced.build(**kw)[0]
    assert plain.caps_budget and "--max-budget-usd" in plain.build(**kw)[0]


def test_chat_declared_plan_or_a_zero_price_row_drops_the_fallback_rate_cap(tmp_path):
    from ttp.providers import get_provider
    out = _cb_stream(tmp_path / "output.jsonl")
    kw = dict(role="worker", model="up-model", effort="high", cwd=str(tmp_path), budget_usd=3.0, read_only=False,
              schema=None, restrictions={})
    # A. billing plan, no price row: no --max-budget-usd, and the runner has no repriced figure to cap on.
    plan = get_provider("chat").use("up-model", None, "plan")
    assert not plan.caps_budget and "--max-budget-usd" not in plan.build(**kw)[0]
    assert plan.cost_so_far(_cb_stream(tmp_path / "partial.jsonl", result=False)) is None
    # B. a zero row (a free endpoint): no cap, $0 priced from tokens, not estimated.
    for prices in ({"up-model": [0, 0, 0]}, {"default": [0.0, 0.0, 0.0]}):
        for billing in ("auto", "usage", "plan"):
            free = get_provider("chat").use("up-model", prices, billing)
            assert not free.caps_budget and "--max-budget-usd" not in free.build(**kw)[0], (prices, billing)
            u = free.parse(out)
            assert u.cost_usd == 0 and not u.estimated and u.extra["cost_basis"] == "pricing", (prices, billing)
    # C. no usable row under auto or usage (or an unknown kind, read as auto): Claude Code's cap stays.
    for billing in ("auto", "usage", ""):
        for prices in (None, {}, {"up-model": [1, 2]}, {"other": [1, 1, 1]}):
            kept = get_provider("chat").use("up-model", prices, billing)
            argv = kept.build(**kw)[0]
            assert kept.caps_budget and argv[argv.index("--max-budget-usd") + 1] == "3.00", (billing, prices)
    # D. Claude's own argv does not depend on the declared billing.
    claude = [get_provider("claude").use("opus", None, b).build(**{**kw, "model": "opus"})[0]
              for b in ("auto", "plan", "usage")]
    assert claude[0] == claude[1] == claude[2] and "--max-budget-usd" in claude[0]
    assert get_provider("claude").use("opus", None, "plan").caps_budget


def test_chat_fallback_cap_is_one_low_observation_a_day_and_a_doctor_line(env, monkeypatch):
    from ttp import daemon as dmod
    from ttp.daemon import Daemon
    from ttp.providers import claude
    from ttp.providers.chat import fallback_cap_line
    monkeypatch.setattr(claude, "fence_missing", lambda: [])   # fenced runs, whatever this host has installed
    p = make(env)
    _chat_config(p)
    seen, clock = [], [time.time()]
    monkeypatch.setattr(dmod.time, "time", lambda: clock[0])

    class Proc:   # never a real runner
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))

    def daemon():
        d = Daemon(p.base)
        monkeypatch.setattr(d, "observe", lambda source, text, hint=None, **k: seen.append((source, hint, k.get("key"))))
        return d

    def start(d):
        tid = p.db.add_task("build it", "s", kind="code", tier="standard", origin="user", provider="chat")
        cwd, _ = d._workdir_for(p.db.task(tid))
        rid = d.start_run("worker", "go", "chat", "standard", cwd, task=p.db.task(tid), budget_usd=4.0)
        return json.loads((p.runs / str(rid) / "run.json").read_text())["argv"]

    d = daemon()
    assert "--max-budget-usd" in start(d) and "--max-budget-usd" in start(d)
    assert seen == [("daemon:chat-fallback-cap", "low", "chat-fallback-cap")], "once a day, not per run"
    start(daemon())   # a daemon restart does not repeat it the same day
    assert len(seen) == 1
    clock[0] += 86400 + 1
    start(d)
    assert len(seen) == 2
    line = fallback_cap_line(p.config())
    assert "fallback rate" in line and "pricing.chat" in line and "billing" in line
    # Declared plan, or a price row (zeros: a free endpoint): no cap, no observation, no doctor line.
    for key, value in (("providers.chat.billing", "plan"), ("pricing", {"chat": {"default": [0, 0, 0]}})):
        if key == "pricing":
            p.set_config("providers.chat.billing", "auto")
        p.set_config(key, value)
        clock[0] += 86400 + 1
        assert "--max-budget-usd" not in start(daemon()), key
        assert len(seen) == 2 and fallback_cap_line(p.config()) == "", key
    assert fallback_cap_line({"providers": {"claude": {}}}) == ""   # no chat provider: no line


def test_claude_codex_and_cursor_spend_is_unchanged_by_pricing_chat(tmp_path):
    from ttp.providers import get_provider
    out = _cb_stream(tmp_path / "output.jsonl")
    u = get_provider("claude").use("up-model", _CB_PRICES).parse(out)
    assert u.cost_usd == 0.5 and not u.estimated and "cost_basis" not in u.extra
    assert get_provider("claude").caps_budget
    for name in ("codex", "cursor", "fake"):
        assert not get_provider(name).caps_budget, name   # the runner keeps capping these, as before


def test_a_chat_run_books_its_repriced_spend(env):
    from ttp.daemon import Daemon
    p = make(env)
    _chat_config(p)
    booked = []
    for prices in (_CB_PRICES["up-model"], None):
        p.set_config("pricing", {"chat": {"org/Base-Model-2": prices}} if prices else {})
        d = Daemon(p.base)
        tid = p.db.add_task("build", "", kind="work", tier="standard", priority=3, budget_usd=8.0, labels=[],
                            provider="chat")
        p.db.update_task(tid, status="running")
        rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,boot_id) "
                     "VALUES(?,'worker','chat','org/Base-Model-2',?,'running',?)", (tid, time.time() - 5, d.boot))
        rd = p.runs / str(rid)
        rd.mkdir(parents=True)
        _cb_stream(rd / "output.jsonl")
        p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(rd), rid))
        d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                     {"rc": 0, "started": time.time() - 5, "ended": time.time(), "launched": True})
        run = p.db.one("SELECT * FROM runs WHERE id=?", (rid,))
        booked.append((run["cost_usd"], run["cost_estimated"]))
    # Budget gates read runs.cost_usd and the ledger: the repriced figure, not Claude Code's 0.5.
    assert booked[0][0] == pytest.approx(_CB_PRICED) and booked[0][1] == 0
    assert booked[1] == (0.5, 1)


# Per-tier routing (route.py): a tier's worker runs to the chat provider, with fallback and a meter.
_RT_URL, _RT_MODEL = "https://example.invalid/v1", "placeholder-model"


def _route_config(p, route=True, **extra):
    p.set_config("providers.chat", {"endpoint": "placeholder", "base_url": _RT_URL,
                                    "tiers": {"light": {"model": _RT_MODEL, **({"route": route} if route is not None else {})}},
                                    **extra})


def _route_dispatch(p, d, monkeypatch, **task):
    """Dispatch one queued task; returns (provider, tier, note) of the start, or None."""
    from ttp import budget as bud
    from ttp import coordinator as coord
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, prompt, provider, tier, cwd, **k: started.append(
        (provider, tier, k.get("note") or {})) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.root), None))
    for prov in (d.cfg.get("core_provider", "claude"), "chat", "codex"):
        d.gates[prov] = bud.Gate(prov, regime="windows", max_parallel=6)
    title = task.pop("title", "t")
    assert coord.apply(p, [{"type": "task_add", "title": title, "spec": "s", "tier": "light", "priority": 3, **task}]) == []
    d.dispatch()
    return started[-1] if started else None


def test_tier_routing_unset_changes_nothing(env, monkeypatch):
    from ttp import route, web
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p, route=None)
    d = Daemon(p.base)
    prov, tier, note = _route_dispatch(p, d, monkeypatch)
    assert (prov, tier) == (d.cfg.get("core_provider", "claude"), "light") and route.KEY not in note
    assert route.target(d.cfg, "light") is None and route.config_problems(p.config()) == []
    assert route.phrase(p.db) == "" and "routed" not in web.budget_line(p.db, time.time(), d.cfg.get("core_provider", "claude"), None, 0.0, {})
    # A run with no route entry is booked as before: the finish path never touches it.
    assert route.of(None) is None and route.of("{}") is None


def test_a_routed_tier_starts_on_chat_unless_pinned_reviewed_or_retried(env, monkeypatch):
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="plain")
    assert (prov, tier) == ("chat", "light")
    assert note[route.KEY] == {"tier": "light", "to": "chat", "model": _RT_MODEL, "fallback": "none"}
    assert d.cfg["providers"]["chat"]["tiers"]["light"]["model"] == _RT_MODEL   # start_run's model for chat/light
    for pin in ({"provider": "codex"}, {"model": "some-model"}):
        prov, _, note = _route_dispatch(p, d, monkeypatch, title=f"pinned {pin}", **pin)
        assert prov == pin.get("provider", d.cfg.get("core_provider", "claude")) and route.KEY not in note, pin
    assert not route.eligible({"kind": "review"}, None)
    assert not route.eligible({"kind": "code"}, {"by": "retry", "tier": "light"})
    assert route.eligible({"kind": "code"}, {"by": "jev", "tier": "light"})
    assert route.target(d.cfg, "standard") is None
    assert route.target({**d.cfg, "core_provider": "chat"}, "light") is None


# Rule routing (providers.chat.routes, route.match): by kind and label, first match wins, per-tier fallback.
_ROUTE_RULES = [{"kinds": ["code", "work"], "tiers": ["standard"], "model": "rule-model-a", "effort": "max",
           "route_timeout_s": 1800},
          {"labels_any": ["bulk"], "labels_none": ["keep"], "model": "rule-model-b"}]


def _route_rules_cfg(rules=_ROUTE_RULES, light_route=True):
    return {"providers": {"chat": {"tiers": {"light": {"model": _RT_MODEL, "route": light_route, "effort": "low"},
                                             "standard": {"effort": "medium", "route_timeout_s": 600}},
                                   "routes": rules}}}


def test_route_rules_first_match_wins_and_the_per_tier_route_is_the_fallback():
    from ttp import route
    cfg = _route_rules_cfg()
    code = {"kind": "code", "labels": "[]", "priority": 2}
    assert route.match(cfg, code, "standard") == {"to": "chat", "model": "rule-model-a", "rule": 0}
    # Both rules match a bulk-labelled code task at standard: the first one wins.
    bulk = {**code, "labels": '["bulk"]'}
    assert route.match(cfg, bulk, "standard")["rule"] == 0 and route.match(cfg, bulk, "deep")["rule"] == 1
    assert route.match(_route_rules_cfg(_ROUTE_RULES[::-1]), bulk, "standard")["rule"] == 0   # order decides, not specificity
    assert route.match(_route_rules_cfg(_ROUTE_RULES[::-1]), bulk, "standard")["model"] == "rule-model-b"
    # No rule matches: the tier's own route, else nowhere.
    assert route.match(cfg, code, "light") == {"to": "chat", "model": _RT_MODEL, "rule": None}
    assert route.match(cfg, {**code, "kind": "question"}, "standard") is None
    assert route.match(cfg, code, "deep") is None
    # A rule's labels_none (also as `label:<id>`) skips it; labels_any needs one of its labels.
    assert route.match(cfg, {**code, "labels": '["bulk", "keep:3"]'}, "deep") is None
    # Effort and time limit: the rule's, else the tier's.
    assert route.run_model(cfg, "standard", {"rule": 0, "model": "rule-model-a"}) == ("rule-model-a", "max")
    assert route.run_model(cfg, "light", {"rule": 1, "model": "rule-model-b"}) == ("rule-model-b", "low")
    assert route.run_model(cfg, "light", {"model": _RT_MODEL}) is None   # the tier's own route: its own model
    assert route.timeout_s(cfg, "standard", 0) == 1800.0 and route.timeout_s(cfg, "standard", 1) == 600.0
    # Off: no list, an empty list, chat as the core provider or not allowed.
    for off in (_route_rules_cfg([]), {**cfg, "core_provider": "chat"}, {**cfg, "providers_allowed": ["claude"]}):
        assert route.match(off, code, "standard") is None, off
    assert route.match(_route_rules_cfg(None), code, "light")["rule"] is None
    # Tier-only readers (gates, /models checks) see every model a tier may route to.
    assert route.target(cfg, "deep") == ("chat", "rule-model-b") and route.models(cfg, "light") == {_RT_MODEL, "rule-model-b"}


@pytest.mark.parametrize("task", [
    {"labels": '["urgent"]'}, {"labels": '["needs_deep"]'}, {"labels": '["user_deep"]'},
    {"labels": '["review_fix:41"]'}, {"labels": '["untrusted_input"]'}, {"labels": ["review_fix:7"]},
    {"priority": 1}, {"spec": "urgent: the user is waiting"}, {"result": '{"needs_deep": "too hard at standard"}'},
])
def test_route_rules_keep_urgent_deep_review_fix_and_untrusted_tasks_on_core(task):
    """With a rule list set these route nowhere, the tier's own route included; with none, as before."""
    from ttp import route
    t = {"kind": "code", "labels": "[]", "priority": 2, **task}
    assert route.kept_core(t)
    assert route.match(_route_rules_cfg([{"model": "any-model"}]), t, "standard") is None
    assert route.match(_route_rules_cfg([{"model": "any-model"}]), t, "light") is None
    assert route.match(_route_rules_cfg(None), t, "light")["rule"] is None   # no rule list: per-tier routing unchanged
    assert not route.kept_core({"kind": "code", "labels": '["urgently"]', "priority": 2})


def test_a_rule_routes_a_standard_code_run_by_kind_and_keeps_every_exclusion(env, monkeypatch):
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("providers.chat", {"endpoint": "placeholder", "base_url": _RT_URL,
                                    "tiers": {"light": {"model": _RT_MODEL, "route": True}}, "routes": _ROUTE_RULES})
    d = Daemon(p.base)
    monkeypatch.setattr(d, "_pick_effort", lambda task, provider: None)
    core = d.cfg.get("core_provider", "claude")

    def start(**kw):
        tid = p.db.add_task(kw.pop("title", "t"), kw.pop("spec", "s"), budget_usd=8.0,
                            **{"kind": "code", "tier": "standard", "priority": 2, "labels": [], **kw})
        return _dispatch_task(p, d, monkeypatch, tid)

    prov, tier, k = start()
    assert (prov, tier, k["timeout_s"]) == ("chat", "standard", 1800.0)
    assert k["note"][route.KEY] == {"tier": "standard", "to": "chat", "model": "rule-model-a", "rule": 0,
                                    "fallback": "none", "timeout_s": 1800.0}
    prov, tier, k = start(tier="light")   # no rule for a light code run: the tier's own route
    assert prov == "chat" and k["note"][route.KEY]["model"] == _RT_MODEL and "rule" not in k["note"][route.KEY]
    for why, kw in (("urgent", {"priority": 1}), ("review fix", {"labels": ["review_fix:9"]}),
                    ("pinned model", {"model": "some-model"}), ("pinned provider", {"provider": core}),
                    ("review", {"kind": "review"}), ("question: conclusion floor", {"kind": "question", "labels": ["bulk"]}),
                    ("device run: floor", {"labels": ["needs_device"]})):
        prov, _, k = start(title=why, **kw)
        assert prov == core and (k["note"].get(route.KEY) or {}).get("fallback") in (None,), why
    # A model_missing hold is per model: a hold on the tier's own model leaves the rule's model routing.
    p.db.set_kv(route.MISSING + "standard", {"model": "rule-model-a", "since": time.time()})
    prov, _, k = start(title="held")
    assert prov == core and k["note"][route.KEY]["fallback"] == "model_missing" and k["note"][route.KEY]["rule"] == 0
    p.db.set_kv(route.MISSING + "standard", {"model": "another-model", "since": time.time()})
    assert start(title="other hold")[0] == "chat"


def test_a_rule_routed_run_starts_with_the_rules_model_and_effort(env, monkeypatch):
    from ttp import route
    from ttp.daemon import Daemon
    from ttp.providers import chat as chat_provider

    class Built(Exception):
        pass

    def build(self, **kw):   # what the run would start with; no agent process starts
        raise Built((kw["model"], kw["effort"]))
    monkeypatch.setattr(chat_provider.Chat, "build", build)
    p = make(env)
    p.set_config("providers.chat", {"endpoint": "placeholder", "base_url": _RT_URL, "routes": _ROUTE_RULES,
                                    "tiers": {"standard": {"model": "tier-model", "effort": "medium"}}})
    d = Daemon(p.base)
    tid = p.db.add_task("t", "s", kind="code", tier="standard", priority=2)

    def launch(entry):
        with pytest.raises(Built) as got:
            d.start_run("worker", "go", "chat", "standard", str(p.root), task=p.db.task(tid), note={route.KEY: entry})
        return got.value.args[0]

    assert launch({"to": "chat", "model": "rule-model-a", "rule": 0, "fallback": "none"}) == ("rule-model-a", "max")
    assert launch({"to": "chat", "model": "rule-model-b", "rule": 1, "fallback": "none"}) == ("rule-model-b", "medium")
    assert launch({"to": "chat", "model": "tier-model", "fallback": "none"}) == ("tier-model", "medium")


def test_route_rules_config_problems_and_chat_setter():
    from ttp import chatset, route
    from ttp.cli import cmd_chat_settable
    bad = [{"model": "m", "tiers": ["huge"]}, {"kinds": "code", "model": "m"}, {"tiers": ["light"]},
           {"model": "m", "extra": 1}, {"model": "m", "route_timeout_s": 0}, "rule"]
    got = route.config_problems({"providers": {"chat": {"routes": bad}}})
    assert len(got) == len(bad) and "[0].tiers: unknown tier 'huge'" in got[0] and all("rule skipped" in g for g in got[1:])
    assert route.config_problems({"providers": {"chat": {"routes": {"model": "m"}}}}) == [
        "providers.chat.routes: give a list of rules; nothing is routed by rule"]
    assert [i for i, _ in route.rules({"providers": {"chat": {"routes": [*bad, {"model": "ok"}]}}})] == [0, len(bad)]
    assert route.config_problems({"providers": {"chat": {"routes": _ROUTE_RULES}}}) == []
    conv = chatset.setter("providers.chat.routes")
    assert conv(_ROUTE_RULES) == _ROUTE_RULES and conv(json.dumps(_ROUTE_RULES)) == _ROUTE_RULES and conv("[]") == []
    for v in ("nope", {"model": "m"}, [{"model": ""}], [{"model": "https://x.invalid/m"}], [{"model": "m", "tiers": ["huge"]}],
              [{"model": "m", "effort": "turbo"}], [{"model": "m", "route_timeout_s": 10 ** 9}],
              [{"model": "m", "labels_any": ["a b"]}], [{"model": "m", "base_url": "x"}], [{"model": "m"}] * 33):
        with pytest.raises(ValueError):
            conv(v)
    assert "providers.chat.routes" in chatset.PATTERNS and chatset.setter("providers.claude.routes") is None
    cmd_chat_settable("providers.chat.routes", None)

def _route_slots(p, d, monkeypatch, n, core_max=1, chat_max=1, chat_open=True, core_running=0, tag="t"):
    """Queue n light routed tasks, one dispatch tick under the given gates; returns [(provider, fallback)]."""
    from ttp import budget as bud
    from ttp import coordinator as coord
    core = d.cfg.get("core_provider", "claude")
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, prompt, provider, tier, cwd, **k: started.append(
        (provider, ((k.get("note") or {}).get("route") or {}).get("fallback"))) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.root), None))
    d.gates[core] = bud.Gate(core, regime="windows", max_parallel=core_max)
    d.gates["chat"] = bud.Gate("chat", regime="windows", max_parallel=chat_max, allow_new_work=chat_open)
    d.cfg["budget"]["urgent_extra_slots"] = 0   # slot limits, not the urgent lane
    for _ in range(core_running):
        p.db.x("INSERT INTO runs(role,provider,started,status,boot_id) VALUES('worker',?,?,'running',?)",
               (core, time.time(), d.boot))
    assert coord.apply(p, [{"type": "task_add", "title": f"{tag}{i}", "spec": f"s {tag}", "tier": "light", "priority": 3}
                           for i in range(n)]) == []
    d.dispatch()
    return started


def test_a_routed_start_takes_its_slot_under_the_chat_gate(env, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    core = d.cfg.get("core_provider", "claude")
    # One slot on each gate, four routed tasks: one runs on chat, the overflow waits for a chat slot
    # (route_wait_s) instead of falling back to the core provider at once.
    assert _route_slots(p, d, monkeypatch, 4) == [("chat", "none")]
    assert len(p.db.q("SELECT id FROM tasks WHERE status='queued'")) == 3
    assert core != "chat" and len(d._route_waits) == 3


def _route_ticks(p, d, monkeypatch, n=0, chat_max=1, chat_running=0, tag="w"):
    """One dispatch tick (n new routed tasks first); returns [(provider, route entry)] of its starts."""
    from ttp import budget as bud
    from ttp import coordinator as coord
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, prompt, provider, tier, cwd, **k: started.append(
        (provider, dict((k.get("note") or {}).get("route") or {}))) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.root), None))
    d.gates[d.cfg.get("core_provider", "claude")] = bud.Gate("core", regime="windows", max_parallel=4)
    d.gates["chat"] = bud.Gate("chat", regime="windows", max_parallel=chat_max)
    d.cfg["budget"]["urgent_extra_slots"] = 0
    p.db.x("UPDATE runs SET status='done' WHERE status='running'")
    for _ in range(chat_running):
        p.db.x("INSERT INTO runs(role,provider,started,status,boot_id) VALUES('worker','chat',?,'running',?)",
               (time.time(), d.boot))
    if n:
        assert coord.apply(p, [{"type": "task_add", "title": f"{tag}{i}", "spec": f"s {tag}", "tier": "light",
                                "priority": 3} for i in range(n)]) == []
    d.dispatch()
    return started


def test_a_routed_start_waits_a_bounded_time_for_a_full_slot_then_falls_back_busy_wait_expired(env, monkeypatch):
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    core = d.cfg.get("core_provider", "claude")
    assert route.wait_s(d.cfg) == route.WAIT_S == 90.0
    # Two routed tasks, one chat slot: the second waits; nothing starts on the core provider.
    first = _route_ticks(p, d, monkeypatch, 2)
    assert [(prov, e["fallback"]) for prov, e in first] == [("chat", "none")] and "waited_s" not in first[0][1]
    (waiting,) = d._route_waits
    # The slot frees within the wait: it starts on chat and says how long it waited.
    (prov, e), = _route_ticks(p, d, monkeypatch)
    assert (prov, e["fallback"]) == ("chat", "none") and 0 <= e["waited_s"] < 90 and not d._route_waits
    # Full for the whole wait: it starts on the core provider, busy_wait_expired with the wait.
    _route_ticks(p, d, monkeypatch, 1, chat_running=1, tag="x")
    (tid,) = d._route_waits
    assert _route_ticks(p, d, monkeypatch, chat_running=1) == []   # still within the wait: held
    d._route_waits[tid] = time.time() - 91
    (prov, e), = _route_ticks(p, d, monkeypatch, chat_running=1)
    assert (prov, e["fallback"]) == (core, "busy_wait_expired") and e["waited_s"] >= 90 and not d._route_waits
    # route_wait_s 0: no wait at all.
    _route_config(p, route_wait_s=0)
    d = Daemon(p.base)
    (prov, e), = _route_ticks(p, d, monkeypatch, 1, chat_running=1, tag="y")
    assert (prov, e["fallback"]) == (core, "busy_wait_expired") and e["waited_s"] == 0
    assert "busy_wait_expired" in route.REASONS and p.config()["providers"]["chat"]["route_wait_s"] == 0


def test_two_projects_share_the_hosts_routed_slots_and_a_finished_run_frees_its_own(env, monkeypatch):
    from ttp import project, route
    from ttp.daemon import Daemon
    monkeypatch.setenv(route.HOST_ENV, "1")
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    # Another project's daemon on this host holds the one routed slot (its lease, as host_take writes it).
    other = f"{env['tmp']}/other-project#7"
    assert route.host_take("chat", other, os.getpid(), time.time() + 600)
    # This project has free chat slots of its own, but the host has none: its routed task waits.
    assert _route_ticks(p, d, monkeypatch, 1, chat_max=4) == [] and len(d._route_waits) == 1
    assert route.host_used("chat") == 1
    # The other project's run ends: this one takes the slot, and the other cannot take it back.
    route.host_release("chat", other)
    (prov, e), = _route_ticks(p, d, monkeypatch, chat_max=4)
    assert (prov, e["fallback"]) == ("chat", "none") and "waited_s" in e and route.host_used("chat") == 1
    assert not route.host_take("chat", other, os.getpid(), time.time() + 600)
    # This project's run ends: finish_run frees its own lease, and the other project gets the slot.
    (tid,) = [t["id"] for t in p.db.q("SELECT id FROM tasks WHERE status='running'")]
    _route_finish(p, d, tid, {"rc": 0})
    assert route.host_used("chat") == 0 and route.host_take("chat", other, os.getpid(), time.time() + 600)
    # The limit is account-wide: route.host_max in the account settings, 0 for none.
    monkeypatch.delenv(route.HOST_ENV)
    assert route.host_limit() == route.HOST_MAX
    project.set_account_setting("route.host_max", "0")
    assert route.host_limit() == 0 and not route.host_full("chat")


def test_a_stale_routed_slot_lease_is_dropped_and_a_bad_lease_file_never_holds_runs(env, monkeypatch):
    import subprocess
    from ttp import route, runner
    monkeypatch.setenv(route.HOST_ENV, "1")
    now = time.time()
    gone = subprocess.Popen(["true"])
    gone.wait()
    path = route._host_file("chat")
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = {"crashed": {"pid": gone.pid, "start": "", "until": now + 600},   # its process is gone
             "expired": {"pid": os.getpid(), "start": runner.proc_start(os.getpid()), "until": now - 1},
             "reused": {"pid": os.getpid(), "start": "not-this-process", "until": now + 600},   # pid reused
             "junk": "x"}
    for key, lease in stale.items():
        path.write_text(json.dumps({key: lease}))
        assert route.host_used("chat", now) == 0, key
        assert route.host_take("chat", "next", os.getpid(), now + 600, now), key
        assert set(json.loads(path.read_text())) == {"next"}, key
        route.host_release("chat", "next")
    assert route.host_take("chat", "a", os.getpid(), now + 600, now)
    assert not route.host_take("chat", "b", os.getpid(), now + 600, now)   # a live holder keeps the slot
    assert route.host_take("chat", "a", os.getpid(), now + 600, now)   # its own lease: kept, not doubled
    path.write_text("{not json")   # unreadable: no lease stands
    assert route.host_used("chat", now) == 0 and route.host_take("chat", "b", os.getpid(), now + 600, now)


def test_status_counts_routed_slot_waits_and_busy_wait_expired(env):
    from ttp import route
    p = make(env)
    now = time.time()
    for prov, e in (("chat", {"fallback": "none", "waited_s": 12.0}), ("chat", {"fallback": "none"}),
                    ("claude", {"fallback": "busy_wait_expired", "waited_s": 90.0})):
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,note) VALUES('worker',?,?,?,'ok',?)",
               (prov, now - 60, now - 30, json.dumps({"route": {"tier": "light", "to": "chat", **e}})))
    assert route.phrase(p.db, now) == "routed 24h: 2 runs to chat, 1 fell back (busy_wait_expired 1), 1 after a slot wait"


def test_a_routed_start_is_not_held_by_full_core_slots(env, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    assert _route_slots(p, d, monkeypatch, 3, chat_max=2, core_running=1) == [("chat", "none")] * 2


def test_a_closed_or_held_chat_gate_sends_routed_work_to_the_core_gate(env, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    core = d.cfg.get("core_provider", "claude")
    assert _route_slots(p, d, monkeypatch, 2, chat_open=False) == [(core, "busy")]
    p.db.x("UPDATE tasks SET status='done'")
    # Chat full and core full: nothing starts.
    assert _route_slots(p, d, monkeypatch, 2, chat_max=0, core_running=1, tag="full") == []
    p.db.x("UPDATE tasks SET status='done'")
    p.db.x("UPDATE runs SET status='done'")
    # Its API host does not resolve: the core provider takes it, reason down.
    monkeypatch.setattr(d, "net_held", lambda prov: prov == "chat")
    assert _route_slots(p, d, monkeypatch, 1, tag="net") == [(core, "down")]
    monkeypatch.setattr(d, "net_held", lambda prov: False)
    p.db.x("UPDATE tasks SET status='done'")
    # A rate-limit pause on chat: no routed start there.
    p.db.set_kv("limited:chat", {"until": time.time() + 600, "note": "rate limited"})
    assert _route_slots(p, d, monkeypatch, 1, tag="paused") == [(core, "busy")]


def test_a_start_whose_tier_leaves_the_route_rechecks_the_core_slots(env, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    core = d.cfg.get("core_provider", "claude")
    # Its slot was checked on chat, but the pick raises it to an unrouted tier: it needs a core slot.
    monkeypatch.setattr(d, "_pick_effort", lambda task, prov: {"tier": "standard", "by": "jev"})
    assert _route_slots(p, d, monkeypatch, 2, chat_max=2, core_running=1) == []
    p.db.x("UPDATE tasks SET status='done'")
    p.db.x("UPDATE runs SET status='done'")
    assert _route_slots(p, d, monkeypatch, 2, chat_max=2, tag="free") == [(core, None)]


def _route_finish(p, d, tid, exit_info, provider="chat", result=None, reply=None, earlier=None, is_error=False,
                  routed=None):
    """`reply`: the run's final reply text (its output.jsonl result line, an error one with `is_error`);
    `earlier`: an earlier turn's text; `routed`: the run's route entry (default: light on _RT_MODEL)."""
    p.db.update_task(tid, status="running")
    note = {"route": routed or {"tier": "light", "to": "chat", "model": _RT_MODEL, "fallback": "none"}}
    rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,boot_id,note) "
                 "VALUES(?,'worker',?,?,?,'running',?,?)",
                 (tid, provider, note["route"]["model"], time.time() - 30, d.boot, json.dumps(note)))
    rd = p.runs / str(rid)
    rd.mkdir(parents=True)
    if result is not None:
        (rd / "result.json").write_text(result if isinstance(result, str) else json.dumps(result))
    if reply is not None:
        turn = lambda text: {"type": "assistant", "message": {"id": f"m{len(text)}", "content": [{"type": "text", "text": text}]}}
        lines = [turn(earlier)] if earlier is not None else []
        lines += [turn(reply), {"type": "result", "subtype": "success", "is_error": is_error, "total_cost_usd": 0.0,
                                "usage": {}, "result": reply, "session_id": "s-1"}]
        (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(rd), rid))
    d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                 {"rc": 1, "started": time.time() - 30, "ended": time.time(), **exit_info})
    return p.db.one("SELECT * FROM runs WHERE id=?", (rid,)), p.db.task(tid)


@pytest.mark.parametrize("fault", ["down", "error", "rate_limit"])
def test_a_routed_run_on_an_endpoint_fault_falls_back_to_the_core_provider(env, monkeypatch, fault):
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p, route_cooldown_s=600)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    run, task = _route_finish(p, d, tid, {"bridge": {"last_fault": fault, "faults": {fault: 2}}})
    assert run["status"] == "fallback", run["status"]
    assert task["status"] == "queued" and int(task["attempts"] or 0) == 0 and task["tier"] == "light"
    e = route.of(run["note"])
    assert e["fallback"] == fault and e["wall_s"] >= 29 and e["priced"] is False and not run["cost_usd"]
    assert route.cooling(p.db, "chat", time.time()) == fault
    assert route.cooling(p.db, "chat", time.time() + 601) == ""
    # The retry starts on the core provider at the same tier, its note naming why.
    p.db.update_task(tid, status="done")
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="next")
    assert (prov, tier) == (d.cfg.get("core_provider", "claude"), "light") and note[route.KEY]["fallback"] == fault
    # Once the cool-down ends the tier routes again.
    p.db.set_kv(route.COOL + "chat", {"until": time.time() - 1, "reason": fault})
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="after")
    assert prov == "chat" and note[route.KEY]["fallback"] == "none"
    assert route.phrase(p.db) == f"routed 24h: 1 run to chat, 1 fell back ({fault} 1)"


def test_chatbridge_stats_keep_the_scrubbed_upstream_status_and_error_code(mock_chat):
    from ttp.chatbridge import Bridge, upstream_of
    hi = {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    gone = {"error": {"message": f"The model 'old-model' does not exist at https://example.invalid/v1 ({_CB_KEY})",
                      "type": "invalid_request_error", "code": "model_not_found"}}
    up = mock_chat((404, gone), (500, {"error": {"message": "boom"}}),
                   (200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}))
    with Bridge(up.url, _CB_KEY, "m") as b:
        _post(b, hi)
        st404 = b.stats()
        _post(b, hi)
        st500 = b.stats()
        _post(b, hi)
        ok = b.stats()
    # A missing model is model_missing (#5194), a 5xx an error; each keeps its upstream status and code.
    assert (st404["last_fault"], st500["last_fault"]) == ("model_missing", "error")
    assert st404["upstream"] == {"status": 404, "code": "model_not_found"}
    assert st500["upstream"] == {"status": 500}
    assert "upstream" not in ok and ok["last_fault"] == ""   # a good answer clears it
    for st in (st404, st500):
        assert _CB_KEY not in json.dumps(st) and "example.invalid" not in json.dumps(st) and "old-model" not in json.dumps(st)
    # Only a plain code word is kept: a key, a token, a URL, a host or free text in the code is dropped
    # (the type stands in when it is one), and no answer has no upstream record.
    for bad in (_CB_KEY, "tok_9f8e7d6c5b4a3921", "https://example.invalid/v1", "api.example.invalid",
                "host:8443", "model not found here", "a" * 60, 404, None, ["model_not_found"]):
        got = upstream_of(404, {"error": {"code": bad, "type": "not_found_error"}}, _CB_KEY)
        assert got == {"status": 404, "code": "not_found_error"}, (bad, got)
        assert upstream_of(404, json.dumps({"error": {"code": bad}}), _CB_KEY) == {"status": 404}, bad
    assert upstream_of(404, "<html>not found</html>") == {"status": 404}
    assert upstream_of(0, json.dumps(gone)) == {}
    assert upstream_of(404, {"error": {"code": "secretword"}}, "secretword") == {"status": 404}
    # An error in a stream chunk keeps its own code; a dead endpoint keeps none.
    up = mock_chat((200, [{"choices": [{"delta": {"content": "par"}}]},
                          {"error": {"message": "slow down", "code": 429, "type": "rate_limit_exceeded"}}]))
    with Bridge(up.url, _CB_KEY, "m") as b:
        _post(b, {**hi, "stream": True})
        assert b.stats()["upstream"] == {"status": 429, "code": "rate_limit_exceeded"}
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}/v1"
    with Bridge(dead, _CB_KEY, "m") as b:
        _post(b, hi)
        assert b.stats()["last_fault"] == "down" and "upstream" not in b.stats()


def test_a_routed_run_that_fell_back_is_one_route_line_in_the_feed_not_a_failed_run(env, monkeypatch):
    from ttp import coordinator as coord
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    said = "There's an issue with the selected model (placeholder-model). It may not exist or you may not have access to it."
    bridge = {"last_fault": "error", "faults": {"error": 1}, "upstream": {"status": 404, "code": "model_not_found"}}
    run, task = _route_finish(p, d, tid, {"bridge": bridge}, reply=said, is_error=True)
    assert run["status"] == "fallback" and task["status"] == "queued" and int(task["attempts"] or 0) == 0
    e = route.of(run["note"])
    assert e["fallback"] == "error" and e["upstream"] == {"status": 404, "code": "model_not_found"}
    summary = json.loads(task["result"])["summary"]
    assert summary == (f"routed run {run['id']} on chat ({_RT_MODEL}) fell back: error (HTTP 404 model_not_found); "
                       "requeued at light on the core provider, no attempt spent"), summary
    ev = p.db.q("SELECT kind, text FROM events WHERE task=?", (tid,))
    assert ev and all("issue with the selected model" not in x["text"] for x in ev), ev
    assert any("fell back: error (HTTP 404 model_not_found)" in x["text"] for x in ev), ev
    digest = coord.digest(p, {}, [x["id"] for x in p.db.q("SELECT id FROM events")], [])
    assert "issue with the selected model" not in digest
    assert sum(1 for line in digest.splitlines() if f"routed run {run['id']} on chat" in line) >= 1
    # A bad upstream record in the exit info is dropped, not copied; a run's own fallback shows the
    # daemon's cause, never its error text; the core provider's run then is the outcome that counts.
    tid2 = p.db.add_task("build2", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    bad = {"last_fault": "error", "upstream": {"status": "404", "code": f"https://example.invalid/{_CB_KEY}"}}
    run2, _ = _route_finish(p, d, tid2, {"bridge": bad})
    assert "upstream" not in route.of(run2["note"])
    assert route.upstream_entry({"upstream": {"status": 404, "code": "a/b"}}) == {"status": 404}
    tid3 = p.db.add_task("build3", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    run3, task3 = _route_finish(p, d, tid3, {"bridge": {"last_fault": "", "faults": {}}}, reply=said, is_error=True)
    e3 = route.of(run3["note"])
    assert run3["status"] == "fallback" and e3["fallback"] == "failed", e3
    s3 = json.loads(task3["result"])["summary"]
    assert s3.startswith(f"routed run {run3['id']} on chat ({_RT_MODEL}) fell back: failed;") and "issue" not in s3, s3
    p.db.update_task(tid, status="running")
    rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,boot_id,note) VALUES(?,'worker',?,?,?,'running',?,?)",
                 (tid, d.cfg.get("core_provider", "claude"), "core", time.time() - 10, d.boot,
                  json.dumps({"route": {"tier": "light", "to": "chat", "model": _RT_MODEL, "fallback": "error"}})))
    rd = p.runs / str(rid)
    rd.mkdir(parents=True)
    (rd / "result.json").write_text(json.dumps({"status": "failed", "summary": "the build broke"}))
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(rd), rid))
    d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)), {"rc": 0, "started": time.time() - 10, "ended": time.time()})
    assert json.loads(p.db.task(tid)["result"])["summary"] == "the build broke"


def test_chatbridge_a_404_for_a_missing_model_is_model_missing(mock_chat):
    """#5194: a withdrawn model's 404 was a plain `error`; its wording, or /models leaving it out, names it."""
    from ttp.chatbridge import Bridge, fault_of
    for detail in ("The model `example-org/model-a` does not exist.", '{"error": {"code": "model_not_found"}}',
                   "Unknown model: example-org/model-a", "model example-org/model-a is not available"):
        assert fault_of(404, detail) == "model_missing", detail
    for status, detail in ((404, "Not Found"), (404, ""), (500, "model does not exist"), (400, "unknown model")):
        assert fault_of(status, detail) != "model_missing", (status, detail)
    hi = {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    # A bare 404: the bridge asks /models once; a list without the model makes it model_missing.
    up = mock_chat((404, {"error": {"message": "Not Found"}}), (404, {"error": {"message": "Not Found"}}))
    up.models = ["example-org/model-b"]
    with Bridge(up.url, _CB_KEY, "example-org/model-a") as b:
        _post(b, hi)
        _post(b, hi)
        assert b.stats()["last_fault"] == "model_missing" and b.stats()["faults"] == {"model_missing": 2}
    assert [s["path"] for s in up.seen].count("/v1/models") == 1
    # Listed, or no list to read: it stays a plain error (the endpoint may be misrouted, not the model gone).
    for models in (["example-org/model-a"], None):
        up = mock_chat((404, {"error": {"message": "Not Found"}}))
        up.models = models
        with Bridge(up.url, _CB_KEY, "example-org/model-a") as b:
            _post(b, hi)
            assert b.stats()["last_fault"] == "error", models


def _model_missing_run(p, d, monkeypatch, mock_chat):
    """One routed light run against a fake endpoint that 404s the configured model: its bridge stats."""
    from ttp.chatbridge import Bridge
    up = mock_chat((404, {"error": {"message": f"The model `{_RT_MODEL}` does not exist."}}))
    with Bridge(up.url, _CB_KEY, _RT_MODEL) as b:
        _post(b, {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
        return b.stats()


def test_a_routed_tier_whose_model_is_gone_falls_back_once_then_holds(env, monkeypatch, mock_chat):
    """#5194: every routed start re-hit the withdrawn model after each 900 s cool-down, with no alert."""
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("providers.chat", {"endpoint": "placeholder", "base_url": _RT_URL, "route_cooldown_s": 600, "tiers": {
        "light": {"model": _RT_MODEL, "route": True}, "standard": {"model": "example-org/model-b", "route": True}}})
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    alerts = []
    monkeypatch.setattr(d, "alert", lambda key, text, severity="high", **k: alerts.append((key, text, severity)))
    stats = _model_missing_run(p, d, monkeypatch, mock_chat)
    assert stats["last_fault"] == "model_missing"
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    run, task = _route_finish(p, d, tid, {"bridge": stats})
    assert run["status"] == "fallback" and route.of(run["note"])["fallback"] == "model_missing"
    assert task["status"] == "queued" and int(task["attempts"] or 0) == 0
    assert route.cooling(p.db, "chat", time.time()) == ""   # the endpoint is fine: no provider-wide cool-down
    assert route.held(p.db, d.cfg, "light") == _RT_MODEL and route.held(p.db, d.cfg, "standard") == ""
    assert len(alerts) == 1 and alerts[0][2] == "normal"
    key, text, _ = alerts[0]
    assert "light" in text and _RT_MODEL in text and "chat" in text and _RT_URL not in text and "http" not in text
    # A second run already in flight hits it too: no second notice.
    tid2 = p.db.add_task("build2", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    _route_finish(p, d, tid2, {"bridge": stats})
    assert len(alerts) == 1
    # No further routed starts for the held tier, past any cool-down; the other routed tier is untouched.
    p.db.update_task(tid, status="done")
    p.db.update_task(tid2, status="done")
    for title in ("next", "later"):
        prov, tier, note = _route_dispatch(p, d, monkeypatch, title=title)
        assert (prov, tier) == (d.cfg.get("core_provider", "claude"), "light") and note[route.KEY]["fallback"] == "model_missing"
    assert d._route({"id": None, "kind": "work"}, "standard", None, time.time())["fallback"] == "none"
    # ttp status shows the hold.
    lines = route.model_lines(p.db, d.cfg, p.state, time.time(), lambda ts: "now")
    assert any(l.startswith("routing: light held since now: chat does not serve " + _RT_MODEL) for l in lines), lines


def test_a_routed_run_refused_its_model_by_claude_code_holds_the_tier_with_one_notice(env, monkeypatch):
    """Note 6f381cadcd4517c0: '[claude-code:unrecognized_model] {"model": ...}' with bridge fault `error`
    only cooled the endpoint 900 s, so every light run re-hit it; it is model_missing, held, one notice."""
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p, route_cooldown_s=600)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    alerts = []
    monkeypatch.setattr(d, "alert", lambda key, text, severity="high", **k: alerts.append((key, severity)))
    refused = '[claude-code:unrecognized_model] {"model":"%s"}' % _RT_MODEL
    for n, last_fault in enumerate(("error", "", "error")):
        tid = p.db.add_task(f"build{n}", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
        p.db.update_task(tid, status="running")
        rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,boot_id,note) "
                     "VALUES(?,'worker','chat',?,?,'running',?,?)", (tid, _RT_MODEL, time.time() - 30, d.boot,
                     json.dumps({"route": {"tier": "light", "to": "chat", "model": _RT_MODEL, "fallback": "none"}})))
        rd = p.runs / str(rid)
        rd.mkdir(parents=True)
        (rd / "output.jsonl").write_text(json.dumps({"type": "result", "subtype": "error_during_execution",
                                                     "is_error": True, "total_cost_usd": 0.0, "usage": {},
                                                     "result": refused, "session_id": "s-1"}) + "\n")
        p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(rd), rid))
        d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                     {"rc": 1, "started": time.time() - 30, "ended": time.time(), "bridge": {"last_fault": last_fault}})
        run = p.db.one("SELECT * FROM runs WHERE id=?", (rid,))
        assert run["status"] == "fallback" and route.of(run["note"])["fallback"] == "model_missing", (n, run["note"])
        p.db.update_task(tid, status="done")
    assert route.held(p.db, d.cfg, "light") == _RT_MODEL and route.cooling(p.db, "chat", time.time()) == ""
    assert alerts == [("route-model-missing:light", "normal")]
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="next")
    assert prov == d.cfg.get("core_provider", "claude") and note[route.KEY]["fallback"] == "model_missing"
    # Other run errors stay as they were: a bridge error cools, a clean bridge leaves no endpoint fault.
    assert route.fell_back("failed", {"bridge": {"last_fault": "error"}}, "error_during_execution: boom") == "error"
    assert route.fell_back("failed", {"bridge": {"last_fault": "down"}}, refused) == "down"
    assert route.fell_back("ok", {"bridge": {"last_fault": "error"}}, refused) == ""
    assert route.fell_back("failed", {}, "the worker read about unrecognized models") == ""


def test_a_refused_model_the_endpoint_still_lists_retries_hourly_with_one_notice(env, monkeypatch, mock_chat):
    """Review #5307: a hold made while /models still lists the model ended at the next hourly check, the
    next routed run was refused again and a new notice went out every hour. One notice per tier and
    model; the tier still tries one routed run per hourly check."""
    from ttp import endpoint, project, route
    from ttp.daemon import Daemon
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "models-home")
    up = mock_chat()
    up.models = [_RT_MODEL]
    endpoint.save("placeholder", up.url, _CB_KEY, True, "HTTP 200")
    p = make(env)
    p.set_config("providers.chat", {"endpoint": "placeholder", "base_url": up.url,
                                    "tiers": {"light": {"model": _RT_MODEL, "route": True}}})
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    alerts = []
    monkeypatch.setattr(d, "alert", lambda key, text, severity="high", **k: alerts.append((key, severity)))
    monkeypatch.delenv("TTP_TEST_NO_MODELS_PROBE", raising=False)
    refused = '[claude-code:unrecognized_model] {"model":"%s"}' % _RT_MODEL

    def refused_run(n):
        tid = p.db.add_task(f"build{n}", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
        p.db.update_task(tid, status="running")
        rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,boot_id,note) "
                     "VALUES(?,'worker','chat',?,?,'running',?,?)", (tid, _RT_MODEL, time.time() - 30, d.boot,
                     json.dumps({"route": {"tier": "light", "to": "chat", "model": _RT_MODEL, "fallback": "none"}})))
        rd = p.runs / str(rid)
        rd.mkdir(parents=True)
        (rd / "output.jsonl").write_text(json.dumps({"type": "result", "subtype": "error_during_execution",
                                                     "is_error": True, "total_cost_usd": 0.0, "usage": {},
                                                     "result": refused, "session_id": "s-1"}) + "\n")
        p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(rd), rid))
        d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                     {"rc": 1, "started": time.time() - 30, "ended": time.time(), "bridge": {"last_fault": "error"}})
        p.db.update_task(tid, status="done")

    def hourly_check():
        rec = route.read_served(p.state).get("chat")
        if rec:   # the last check was an hour ago
            (p.state / route.MODELS_FILE).write_text(json.dumps({"chat": {**rec, "checked": time.time() - route.MODELS_EVERY_S}}))
        d._models_tried = 0.0
        d.check_route_models()
        if d._models_probe:
            d._models_probe.join(10)
        time.sleep(0.01)
        d.check_route_models()

    refused_run(0)
    assert route.held(p.db, d.cfg, "light") == _RT_MODEL
    for n in (1, 2):
        hourly_check()
        assert _RT_MODEL in route.read_served(p.state)["chat"]["ids"]
        assert route.held(p.db, d.cfg, "light") == ""   # one routed run tries it again
        refused_run(n)
        assert route.held(p.db, d.cfg, "light") == _RT_MODEL
    assert alerts == [("route-model-missing:light", "normal")], alerts


def test_a_model_missing_hold_ends_when_the_endpoint_lists_it_or_the_config_changes(env, monkeypatch, mock_chat):
    from ttp import endpoint, project, route
    from ttp.daemon import Daemon
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "models-home")
    up = mock_chat()
    up.models = ["example-org/model-b"]
    endpoint.save("placeholder", up.url, _CB_KEY, True, "HTTP 200")
    p = make(env)
    p.set_config("providers.chat", {"endpoint": "placeholder", "base_url": up.url,
                                    "tiers": {"light": {"model": _RT_MODEL, "route": True}}})
    d = Daemon(p.base)
    monkeypatch.setattr(d, "alert", lambda *a, **k: None)
    monkeypatch.delenv("TTP_TEST_NO_MODELS_PROBE", raising=False)
    t0 = time.time()
    assert route.hold(p.db, "light", _RT_MODEL, 1, t0 - 10) and not route.hold(p.db, "light", _RT_MODEL, 2, t0)

    def check():
        d._models_tried = 0.0
        d.check_route_models()
        if d._models_probe:
            d._models_probe.join(10)
        d.check_route_models()   # the next tick reads what the probe wrote

    # The model-free check records ids only, and the hold stays while the model is not listed.
    check()
    rec = route.read_served(p.state)["chat"]
    assert rec["ok"] and rec["ids"] == ["example-org/model-b"] and rec["checked"] >= t0
    raw = (p.state / route.MODELS_FILE).read_text()
    assert _CB_KEY not in raw and up.url not in raw and "127.0.0.1" not in raw
    assert route.held(p.db, d.cfg, "light") == _RT_MODEL
    assert route.models_due(d.cfg, p.state, time.time()) == ""   # at most hourly
    assert route.models_due(d.cfg, p.state, time.time() + route.MODELS_EVERY_S) == "placeholder"
    lines = route.model_lines(p.db, d.cfg, p.state, time.time(), lambda ts: "now")
    assert "routing: chat served 1 model at now: example-org/model-b" in lines, lines
    # Listed again at the next hourly check: the hold ends and the tier routes again.
    up.models = ["example-org/model-b", _RT_MODEL]
    (p.state / route.MODELS_FILE).write_text(json.dumps({"chat": {**rec, "checked": time.time() - route.MODELS_EVERY_S}}))
    assert route.held(p.db, d.cfg, "light")
    check()
    assert route.held(p.db, d.cfg, "light") == "" and route.holds(p.db) == {}
    assert d._route({"id": None, "kind": "work"}, "light", None, time.time())["fallback"] == "none"
    # A config change ends a hold too, with no check at all.
    monkeypatch.setenv("TTP_TEST_NO_MODELS_PROBE", "1")
    route.hold(p.db, "light", _RT_MODEL, 3, time.time() + 5)   # after the last listing: that does not end it
    d.check_route_models()
    assert route.held(p.db, d.cfg, "light") == _RT_MODEL
    p.set_config("providers.chat.tiers.light.model", "example-org/model-b")
    d._load_config()
    assert route.held(p.db, d.cfg, "light") == ""   # read as no hold at once
    d.check_route_models()
    assert route.holds(p.db) == {}
    # A failed check keeps the last ids, marked failed, and names no URL.
    up.models = None
    route.refresh_served(p.state, "placeholder")
    rec = route.read_served(p.state)["chat"]
    assert not rec["ok"] and rec["ids"] == ["example-org/model-b", _RT_MODEL] and up.url not in json.dumps(rec)


def _ranked(p, d, monkeypatch, ids, ranking=("rank-a", "rank-b")):
    """A routed light tier with a model ranking, the endpoint's last /models check listing `ids`."""
    from ttp import route
    _route_config(p, model_ranking=list(ranking))
    d._load_config()
    route.write_served(p.state, "chat", True, "HTTP 200", list(ids), time.time())
    alerts = []
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    monkeypatch.setattr(d, "alert", lambda key, text, severity="high", **k: alerts.append((key, text, severity)))
    return alerts


def test_a_routed_tier_whose_model_is_down_runs_the_top_ranked_served_model(env, monkeypatch, mock_chat):
    """User's rule: if a model goes down, use the smartest model still served, by the project's ranking."""
    from ttp import modelpick, modelrank, route
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    alerts = _ranked(p, d, monkeypatch, ["rank-b", "rank-a", _RT_MODEL])
    # Up: the tier's own model, nothing ranked.
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="up")
    assert prov == "chat" and note[route.KEY]["model"] == _RT_MODEL and modelrank.KEY not in note[route.KEY]
    # Refused (the model_missing hold): the top-ranked served model, one low alert, the hold's notice skipped.
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    run, _ = _route_finish(p, d, tid, {"bridge": _model_missing_run(p, d, monkeypatch, mock_chat)})
    assert route.of(run["note"])["fallback"] == "model_missing" and route.held(p.db, d.cfg, "light") == _RT_MODEL
    assert alerts == []
    p.db.update_task(tid, status="done")
    for title in ("down", "still down"):
        prov, tier, note = _route_dispatch(p, d, monkeypatch, title=title)
        assert (prov, tier) == ("chat", "light"), note
        assert note[route.KEY] == {"tier": "light", "to": "chat", "model": "rank-a", "fallback": "none",
                                   modelrank.KEY: {"from": _RT_MODEL, "why": "refused"}}
    assert modelpick.run_model(note, "chat", _RT_MODEL, "low") == ("rank-a", "low")   # start_run runs it
    assert modelpick.run_model(note, "claude", "sonnet", "low") == ("sonnet", "low")
    assert len(alerts) == 1 and alerts[0][2] == "low" and "rank-a" in alerts[0][1] and _RT_MODEL in alerts[0][1]
    # The stand-in is refused too: it is skipped, the next ranked one runs, the tier's hold is unchanged.
    tid = p.db.add_task("build2", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    run, task = _route_finish(p, d, tid, {"bridge": {"last_fault": "model_missing"}}, routed=note[route.KEY])
    assert run["status"] == "fallback" and task["status"] == "queued" and int(task["attempts"] or 0) == 0
    assert route.held(p.db, d.cfg, "light") == _RT_MODEL and modelrank.down(p.db, d.cfg, p.state, "rank-a", time.time())
    p.db.update_task(tid, status="done")
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="next ranked")
    assert prov == "chat" and note[route.KEY]["model"] == "rank-b" and len(alerts) == 2
    # Back: the endpoint lists the model again after a check that left it out, so the hold ends and
    # the tier's own model runs again; the switch record ends with it.
    now = time.time()
    route.write_served(p.state, "chat", True, "HTTP 200", ["rank-b"], now + 1)
    route.release(p.db, d.cfg, route.read_served(p.state), now + 1)
    route.write_served(p.state, "chat", True, "HTTP 200", ["rank-b", _RT_MODEL], now + 2)
    assert route.release(p.db, d.cfg, route.read_served(p.state), now + 2)[0][2] == "the endpoint lists it again"
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="back")
    assert prov == "chat" and note[route.KEY] == {"tier": "light", "to": "chat", "model": _RT_MODEL, "fallback": "none"}
    assert p.db.kv(modelrank.SWITCH + "light") is None and "back on " + _RT_MODEL in (p.logs / "daemon.log").read_text()


def test_rule_routes_of_one_tier_keep_their_own_model_ranking_switch(env, monkeypatch):
    """Two models of one tier (its own route and a providers.chat.routes rule) both down: each start
    keeps its own switch record, so alternating starts raise one low alert per switch, not one each."""
    from ttp import modelrank, route
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    alerts = _ranked(p, d, monkeypatch, ["rank-a"])
    _route_config(p, model_ranking=["rank-a"], routes=[{"labels_any": ["bulk"], "model": "rule-model-b"}])
    d._load_config()
    task = {"id": None, "kind": "work"}
    for _ in range(3):
        own = d._route_entry(task, "light", "chat", _RT_MODEL, time.time())
        by_rule = d._route_entry(task, "light", "chat", "rule-model-b", time.time(), 0)
        assert own["model"] == by_rule["model"] == "rank-a" and by_rule["rule"] == 0
        assert by_rule[modelrank.KEY] == {"from": "rule-model-b", "why": "not listed"}
    assert len(alerts) == 2 and {a[2] for a in alerts} == {"low"}
    assert p.db.kv(modelrank.SWITCH + "light")["from"] == _RT_MODEL
    assert p.db.kv(modelrank.SWITCH + "light:rule0")["from"] == "rule-model-b"
    # The rule's model is served again: only its own record ends.
    route.write_served(p.state, "chat", True, "HTTP 200", ["rank-a", "rule-model-b"], time.time())
    assert d._route_entry(task, "light", "chat", "rule-model-b", time.time(), 0)["model"] == "rule-model-b"
    assert p.db.kv(modelrank.SWITCH + "light:rule0") is None and p.db.kv(modelrank.SWITCH + "light")


def test_a_model_left_out_of_the_served_list_is_ranked_and_an_empty_ranking_changes_nothing(env, monkeypatch):
    from ttp import modelrank, route
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    alerts = _ranked(p, d, monkeypatch, ["rank-b"])
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="unlisted")
    assert prov == "chat" and note[route.KEY]["model"] == "rank-b"
    assert note[route.KEY][modelrank.KEY] == {"from": _RT_MODEL, "why": "not listed"}
    # A model pick to another provider is never ranked against the chat endpoint's list.
    assert modelrank.KEY not in d._route_entry({"id": None, "kind": "work"}, "light", "codex", "gpt-x", time.time())
    # Nothing ranked is served: the tier's own model, as before (a refusal then holds it as usual).
    route.write_served(p.state, "chat", True, "HTTP 200", ["other"], time.time())
    assert _route_dispatch(p, d, monkeypatch, title="none served")[2][route.KEY]["model"] == _RT_MODEL
    # A failed /models check proves nothing is served: nothing is swapped.
    route.write_served(p.state, "chat", False, "HTTP 503", None, time.time())
    assert modelrank.pick(p.db, d.cfg, p.state, _RT_MODEL, time.time()) is None
    # A cool-down (the whole endpoint faulted) swaps nothing: the core provider, as before.
    route.write_served(p.state, "chat", True, "HTTP 200", ["rank-b"], time.time())
    route.cool(p.db, d.cfg, "chat", "down", 1, time.time())
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="cooling")
    assert prov != "chat" and note[route.KEY]["fallback"] == "down" and modelrank.KEY not in note[route.KEY]
    p.db.x("DELETE FROM kv WHERE key=?", (route.COOL + "chat",))
    # Empty ranking: today's behaviour, held or unlisted alike.
    for empty in ([], "", None):
        _route_config(p, model_ranking=empty)
        d._load_config()
        assert modelrank.ranking(d.cfg) == [] and modelrank.down(p.db, d.cfg, p.state, _RT_MODEL, time.time()) == ""
        assert _route_dispatch(p, d, monkeypatch, title=f"empty {empty!r}", force=True)[2][route.KEY] == \
            {"tier": "light", "to": "chat", "model": _RT_MODEL, "fallback": "none"}
        route.hold(p.db, "light", _RT_MODEL, 1, time.time())
        prov, _, note = _route_dispatch(p, d, monkeypatch, title=f"held {empty!r}", force=True)
        assert prov != "chat" and note[route.KEY]["fallback"] == "model_missing" and modelrank.KEY not in note[route.KEY]
        p.db.x("DELETE FROM kv WHERE key=?", (route.MISSING + "light",))
    assert len(alerts) == 1
    # The setting is checked: a malformed ranking is named and ranks nothing.
    assert modelrank.ranking_of("a, b,a") == ["a", "b"]
    for bad in (3, {"a": 1}, ["a", 2]):
        assert any("model_ranking" in x for x in route.config_problems({"providers": {"chat": {"model_ranking": bad}}}))
        assert modelrank.ranking({"providers": {"chat": {"model_ranking": bad}}}) == []


def test_a_required_model_pin_that_is_down_waits_and_a_preference_pin_is_swapped(env, monkeypatch):
    from ttp import daemon as dmod
    from ttp import failover, modelrank, route
    p = make(env)
    d = dmod.Daemon(p.base)
    alerts = _ranked(p, d, monkeypatch, ["rank-a"])
    req = p.db.add_task("required", "s", kind="work", tier="light", priority=3, provider="chat", model=_RT_MODEL,
                        labels=[failover.REQUIRED_LABEL])
    pref = p.db.add_task("preferred", "s", kind="work", tier="light", priority=3, provider="chat", model=_RT_MODEL,
                         labels=[])
    ctx = {"now": time.time(), "paused": {}, "logged_out": {}, "busy": {}, "committed": None}
    assert d._start_hold(p.db.task(req), "chat", ctx) == "held"
    assert "required model " + _RT_MODEL + " is down" in ctx["held_by"][req]
    assert d._required_model_down(p.db.task(pref), "chat", time.time()) == ""
    assert d._required_model_down(p.db.task(req), "claude", time.time()) == ""   # no served list there

    class Proc:   # never a real runner
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    rid = d.start_run("worker", "go", "chat", "light", str(p.root), task=p.db.task(pref))
    row = p.db.one("SELECT model, note FROM runs WHERE id=?", (rid,))
    assert row["model"] == "rank-a" and json.loads(row["note"])[modelrank.KEY] == {"from": _RT_MODEL, "why": "not listed"}
    assert [a[2] for a in alerts] == ["low"]
    # Served again: the required pin starts, the preference pin runs its own model again.
    route.write_served(p.state, "chat", True, "HTTP 200", ["rank-a", _RT_MODEL], time.time())
    assert d._required_model_down(p.db.task(req), "chat", time.time()) == ""
    rid = d.start_run("worker", "go", "chat", "light", str(p.root), task=p.db.task(pref))
    assert p.db.one("SELECT model FROM runs WHERE id=?", (rid,))["model"] == _RT_MODEL
    assert p.db.kv(modelrank.SWITCH + "pin:" + _RT_MODEL) is None


def test_a_stale_served_list_holds_and_swaps_nothing_and_a_pinned_chat_task_keeps_it_fresh(env, monkeypatch):
    """A /models check over modelrank.STALE_S old is unknown: no required-pin hold, no swap; with a
    ranking set, a queued pinned chat task makes the check due though no tier routes to chat."""
    from ttp import daemon as dmod
    from ttp import failover, modelrank, route
    p = make(env)
    d = dmod.Daemon(p.base)
    alerts = _ranked(p, d, monkeypatch, ["rank-a"])
    req = p.db.add_task("required", "s", kind="work", tier="light", priority=3, provider="chat", model=_RT_MODEL,
                        labels=[failover.REQUIRED_LABEL])
    now = time.time()
    assert d._required_model_down(p.db.task(req), "chat", now)   # a fresh 'not listed' holds it
    old = now - modelrank.STALE_S - 60
    route.write_served(p.state, "chat", True, "HTTP 200", ["rank-a"], old)
    assert modelrank.served(p.state, now) is None and modelrank.down(p.db, d.cfg, p.state, _RT_MODEL, now) == ""
    assert d._required_model_down(p.db.task(req), "chat", now) == ""
    assert d._rank_pick("tier light", "light", _RT_MODEL, now) is None and alerts == []
    # No tier routes to chat: due only with the ranking set and a pinned chat task queued.
    _route_config(p, route=None, model_ranking=["rank-a"])
    d._load_config()
    assert not any(route.target(d.cfg, t) for t in ("light", "standard", "deep"))
    assert route.models_due(d.cfg, p.state, now) == ""                 # the daemon's db not given
    assert route.models_due(d.cfg, p.state, now, p.db) == "placeholder"
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (req,))
    assert route.models_due(d.cfg, p.state, now, p.db) == ""           # no pinned chat task left
    p.db.x("UPDATE tasks SET status='queued', provider=NULL WHERE id=?", (req,))
    assert route.models_due(d.cfg, p.state, now, p.db) == ""           # an unset provider runs on the core one
    assert route.models_due({**d.cfg, "core_provider": "chat"}, p.state, now, p.db) == "placeholder"
    _route_config(p, route=None)
    d._load_config()
    assert route.models_due(d.cfg, p.state, now, p.db) == ""           # no ranking: as before


def test_a_model_down_with_no_ranked_model_served_keeps_its_switch(env, monkeypatch):
    from ttp import daemon as dmod
    from ttp import modelrank
    p = make(env)
    d = dmod.Daemon(p.base)
    _ranked(p, d, monkeypatch, ["rank-a"])
    now = time.time()
    assert d._rank_pick("tier light", "light", _RT_MODEL, now)[0] == "rank-a"
    modelrank.refused(p.db, "rank-a", None, now)   # the stand-in refused too: none served
    assert d._rank_pick("tier light", "light", _RT_MODEL, now) is None
    assert p.db.kv(modelrank.SWITCH + "light") and "back on" not in (p.logs / "daemon.log").read_text()


def test_a_routed_run_with_a_refused_key_goes_through_the_auth_gate_then_falls_back(env, monkeypatch):
    from ttp import alerts, route
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    run, task = _route_finish(p, d, tid, {"rc": None, "launched": False, "launch_auth": True,
                                          "launch_error": "could not start the agent: chat bridge: failed to authenticate"})
    assert run["status"] == "auth" and route.of(run["note"])["fallback"] == "auth"
    assert d._logged_out("chat") and alerts.breaker(p.db, "chat")
    assert task["status"] == "queued" and int(task["attempts"] or 0) == 0 and task["tier"] == "light"
    p.db.update_task(tid, status="done")
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="next")
    assert prov == d.cfg.get("core_provider", "claude") and note[route.KEY]["fallback"] == "auth"


def test_a_routed_run_that_hands_off_failed_is_no_fallback(env, monkeypatch):
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    p.set_config("pricing.chat", {_RT_MODEL: [1.0, 0.1, 4.0]})
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    # The endpoint answered (an earlier fault cleared by a later reply) and the worker handed off
    # failed: its own verdict, retried as usual (a tier up), not a fallback.
    run, task = _route_finish(p, d, tid, {"rc": 0, "bridge": {"last_fault": "", "faults": {"error": 1}}},
                              result={"status": "failed", "summary": "the approach does not work"})
    assert run["status"] == "ok" and int(task["attempts"] or 0) == 1
    e = route.of(run["note"])
    assert e["fallback"] == "none" and e["priced"] is True and "cause" not in e
    assert route.cooling(p.db, "chat", time.time()) == "" and route.fell_back_before(p.db, tid) is None
    # A run that fell back to the core provider is booked as usual, not at the chat price.
    run, _ = _route_finish(p, d, tid, {"bridge": {"last_fault": "down"}}, provider=d.cfg.get("core_provider", "claude"))
    assert run["status"] == "failed" and "priced" not in route.of(run["note"])


def _dispatch_task(p, d, monkeypatch, tid):
    """Dispatch with only task `tid` queued; returns (provider, tier, kwargs) of its start."""
    from ttp import budget as bud
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, prompt, provider, tier, cwd, **k: started.append(
        (provider, tier, k)) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.root), None))
    for prov in (d.cfg.get("core_provider", "claude"), "chat"):
        d.gates[prov] = bud.Gate(prov, regime="windows", max_parallel=6)
    p.db.x("UPDATE tasks SET status='done' WHERE status='queued' AND id!=?", (tid,))
    d.dispatch()
    return started[-1]


@pytest.mark.parametrize("exit_info,result,cause,why", [
    ({"rc": 1}, None, "failed", "exit 1"),                                    # crashed, no hand-off
    ({"rc": None, "stopped": "timeout"}, None, "timeout", "timed out"),       # its time limit
    ({"rc": None, "stopped": "stalled"}, None, "timeout", "stalled"),         # no output for too long
    ({"rc": 0}, None, "no_handoff", "exited without result.json"),            # a clean exit, nothing handed off
    ({"rc": 0}, "{not json", "no_handoff", "result.json is not a JSON object"),   # malformed output
    ({"rc": 0}, {"status": "finished"}, "no_handoff", "result.json status 'finished' is not a hand-off"),
])
def test_a_routed_run_that_ends_without_a_hand_off_falls_back_once(env, monkeypatch, exit_info, result, cause, why):
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    core = d.cfg.get("core_provider", "claude")
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    run, task = _route_finish(p, d, tid, {**exit_info, "bridge": {"last_fault": "", "faults": {}}}, result=result)
    assert run["status"] == "fallback", run["status"]
    assert task["status"] == "queued" and int(task["attempts"] or 0) == 0 and task["tier"] == "light"
    e = route.of(run["note"])
    assert e["fallback"] == cause and e["cause"] == why, e
    # The endpoint is fine: no cool-down, other tasks still route; this task's next run is on the core
    # provider at the same tier (at most one free fallback per task), naming the run it follows.
    assert route.cooling(p.db, "chat", time.time()) == "" and route.fell_back_before(p.db, tid) == run["id"]
    prov, tier, k = _dispatch_task(p, d, monkeypatch, tid)
    assert (prov, tier) == (core, "light")
    assert k["note"][route.KEY] == {"tier": "light", "to": "chat", "model": _RT_MODEL, "fallback": "rerun",
                                    "after": run["id"]} and k.get("timeout_s") is None
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="other")
    assert prov == "chat" and note[route.KEY]["fallback"] == "none"
    assert f"fell back ({cause} 1)" in route.phrase(p.db)


_SALVAGE_OBJ = {"status": "done", "summary": "removed the claims {and kept a brace} in a string", "artifacts": ["a.md"]}


@pytest.mark.parametrize("reply,kept", [
    (f"Done.\n\n```json\n{json.dumps(_SALVAGE_OBJ, indent=1)}\n```\n", True),        # one fenced object
    (f"All done, hand-off: {json.dumps(_SALVAGE_OBJ)} -- that is all.", True),          # one bare object
    (f"```json\n{json.dumps(_SALVAGE_OBJ)}\n```\nor {json.dumps(_SALVAGE_OBJ)}", False),  # two objects
    ('```json\n{"status": "done", "summary": "x",}\n```', False),                       # does not parse
    ('{"status": "finished", "summary": "x"}', False),                                   # no hand-off status
    ('["done"]', False),                                                                 # not an object
    ('{"status": "done", "summary": "cut off', False),                                    # never closed
    ("I wrote the hand-off.", False),                                                    # none
])
def test_a_routed_run_keeps_one_hand_off_from_its_final_reply(env, monkeypatch, reply, kept):
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    run, task = _route_finish(p, d, tid, {"rc": 0, "bridge": {"last_fault": "", "faults": {}}}, reply=reply)
    e = route.of(run["note"])
    path = Path(run["dir"]) / "result.json"
    if kept:   # written as its result.json and judged as a hand-off: no fallback, the task done
        assert json.loads(path.read_text()) == _SALVAGE_OBJ
        assert run["status"] == "ok" and task["status"] == "done" and e["salvaged"] is True
        assert e["fallback"] == "none" and route.fell_back_before(p.db, tid) is None
        assert route.phrase(p.db) == "routed 24h: 1 run to chat, 1 hand-off salvaged"
        assert "ok (salvaged)" in route.runs_text(route.runs(p.db, "light", time.time() - 3600))
    else:      # exactly as before: no file, a no_handoff fallback
        assert not path.exists() and run["status"] == "fallback" and "salvaged" not in e
        assert e["fallback"] == "no_handoff" and e["cause"] == "exited without result.json"


def test_hand_off_salvage_leaves_written_results_other_providers_failed_runs_and_earlier_turns(env, monkeypatch):
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    ok = {"rc": 0, "bridge": {"last_fault": "", "faults": {}}}
    reply = json.dumps(_SALVAGE_OBJ)
    new = lambda: p.db.add_task(f"build {time.time()}", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    # A result.json the run wrote stays as it is, valid or not, whatever its reply says.
    for written, status in (({"status": "failed", "summary": "own"}, "ok"), ({"status": "finished"}, "fallback")):
        run, _ = _route_finish(p, d, new(), ok, result=written, reply=reply)
        assert json.loads((Path(run["dir"]) / "result.json").read_text()) == written
        assert run["status"] == status and "salvaged" not in route.of(run["note"])
    # A run on the core provider (a fallback start) is never salvaged here.
    core = d.cfg.get("core_provider", "claude")
    run, _ = _route_finish(p, d, new(), {"rc": 0}, provider=core, reply=reply)
    assert not (Path(run["dir"]) / "result.json").exists() and "salvaged" not in route.of(run["note"])
    # A failed or timed-out run has no final reply: no salvage, it falls back.
    for end in ({"rc": 1}, {"rc": None, "stopped": "timeout"}):
        run, _ = _route_finish(p, d, new(), {**end, "bridge": {"last_fault": ""}}, reply=reply)
        assert not (Path(run["dir"]) / "result.json").exists() and run["status"] == "fallback"
    # Only the final reply is read: a hand-off in an earlier turn is not.
    run, _ = _route_finish(p, d, new(), ok, reply="Finished.", earlier=reply)
    assert not (Path(run["dir"]) / "result.json").exists() and route.of(run["note"])["fallback"] == "no_handoff"
    assert route.salvage(reply, ("done",)) == _SALVAGE_OBJ and route.salvage(reply, ("waiting",)) is None


def test_a_routed_run_cut_by_the_budget_or_a_kill_is_no_fallback(env, monkeypatch):
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    for stopped in ("budget", "stopped"):
        tid = p.db.add_task(f"build {stopped}", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
        run, _ = _route_finish(p, d, tid, {"rc": None, "stopped": stopped, "bridge": {"last_fault": ""}})
        assert run["status"] != "fallback" and route.of(run["note"])["fallback"] == "none", run["status"]
        assert route.fell_back_before(p.db, tid) is None
    assert route.own_cause("lost") == route.own_cause("limit") == route.own_cause("auth") == ""


def test_a_routed_run_takes_the_tiers_routed_time_limit(env, monkeypatch):
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("providers.chat", {"endpoint": "placeholder", "base_url": _RT_URL, "tiers": {
        "light": {"model": _RT_MODEL, "route": True, "route_timeout_s": 2700},
        "standard": {"model": _RT_MODEL, "route": True}}})
    d = Daemon(p.base)
    tid = p.db.add_task("a", "s", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    prov, tier, k = _dispatch_task(p, d, monkeypatch, tid)
    assert (prov, tier, k["timeout_s"]) == ("chat", "light", 2700.0) and k["note"][route.KEY]["timeout_s"] == 2700.0
    # Unset for the tier: the tier default (start_run's run_timeout_s), as before.
    tid = p.db.add_task("b", "s", kind="work", tier="standard", priority=3, budget_usd=8.0, labels=[])
    monkeypatch.setattr(d, "_pick_effort", lambda task, provider: None)
    prov, tier, k = _dispatch_task(p, d, monkeypatch, tid)
    assert (prov, tier) == ("chat", "standard") and k.get("timeout_s") is None and "timeout_s" not in k["note"][route.KEY]
    # A start on the core provider (a cool-down) keeps the tier default too.
    p.db.set_kv(route.COOL + "chat", {"until": time.time() + 600, "reason": "down"})
    tid = p.db.add_task("c", "s", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    prov, _, k = _dispatch_task(p, d, monkeypatch, tid)
    assert prov == d.cfg.get("core_provider", "claude") and k.get("timeout_s") is None
    assert route.timeout_s({"providers": {"chat": {"tiers": {"light": {"route_timeout_s": True}}}}}, "light") is None
    assert route.timeout_s({"providers": {"chat": {"tiers": {"light": {"route_timeout_s": 0}}}}}, "light") is None


def test_chatbridge_an_answer_that_is_not_json_is_a_malformed_endpoint_fault(env, monkeypatch, mock_chat):
    from ttp import route
    from ttp.chatbridge import Bridge
    hi = {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    up = mock_chat((200, b"<html>gateway</html>"))
    with Bridge(up.url, _CB_KEY, "m") as b:
        code, _ = _post(b, hi)
        st = b.stats()
    assert code == 502 and st == {"last_fault": "malformed", "faults": {"malformed": 1}}
    # A routed run that ended on it falls back with the endpoint cooling down, as for any endpoint fault.
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    run, _ = _route_finish(p, d, tid, {"bridge": st})
    assert run["status"] == "fallback" and route.of(run["note"])["fallback"] == "malformed"
    assert "cause" not in route.of(run["note"]) and route.cooling(p.db, "chat", time.time()) == "malformed"


_CB_HI_STREAM = {"max_tokens": 5, "stream": True, "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.parametrize("bad", [b"{not json", b"<html>gateway</html>", b"[1, 2]", b"5", b"null",
                                 f'{{"echo": "{_CB_KEY}"'.encode()])
def test_chatbridge_a_streamed_chunk_that_is_not_a_json_object_is_a_malformed_endpoint_fault(mock_chat, bad):
    from ttp.chatbridge import Bridge
    # Mixed with good chunks: the answer ends in an error (a lost piece may be a tool call's arguments),
    # what came before it stays, and nothing after it is sent.
    up = mock_chat((200, [{"choices": [{"delta": {"content": "par"}}]}, bad,
                          {"choices": [{"delta": {"content": "tial"}, "finish_reason": "stop"}]}]))
    with Bridge(up.url, _CB_KEY, "m") as b:
        code, text = _post(b, _CB_HI_STREAM)
        st = b.stats()
    ev = _cb_events(text)
    assert code == 200 and st == {"last_fault": "malformed", "faults": {"malformed": 1}}
    assert ev[-1]["type"] == "error" and ev[-1]["error"]["type"] == "api_error"
    deltas = [e["delta"]["text"] for e in ev if e["type"] == "content_block_delta"]
    assert deltas == ["par"] and not any(e["type"] == "message_stop" for e in ev)
    assert _CB_KEY not in text and _CB_KEY not in json.dumps(st)   # no upstream text in the fault


def test_chatbridge_a_stream_with_blank_data_lines_is_no_fault(mock_chat):
    from ttp.chatbridge import Bridge
    up = mock_chat((200, [b"", {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]))
    with Bridge(up.url, _CB_KEY, "m") as b:
        code, text = _post(b, _CB_HI_STREAM)
        st = b.stats()
    assert st == {"last_fault": "", "faults": {}} and _cb_events(text)[-1]["type"] == "message_stop"


def test_chatbridge_a_stream_of_only_bad_chunks_falls_back_and_cools_the_endpoint(env, monkeypatch, mock_chat):
    from ttp import route
    from ttp.chatbridge import Bridge
    from ttp.daemon import Daemon
    up = mock_chat((200, [b"<html>bad gateway</html>", b"{oops"]))
    with Bridge(up.url, _CB_KEY, "m") as b:
        code, text = _post(b, _CB_HI_STREAM)
        st = b.stats()
    ev = _cb_events(text)
    assert [e["type"] for e in ev] == ["error"] and ev[0]["error"]["type"] == "api_error"
    assert st == {"last_fault": "malformed", "faults": {"malformed": 1}}
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    run, _ = _route_finish(p, d, tid, {"bridge": st})
    assert run["status"] == "fallback" and route.of(run["note"])["fallback"] == "malformed"
    assert route.cooling(p.db, "chat", time.time()) == "malformed"


def test_routed_spend_uses_the_chat_price_or_is_zero_and_unpriced():
    from types import SimpleNamespace
    from ttp import route
    u = SimpleNamespace(input_tokens=1_000_000, cache_write_tokens=0, cache_read_tokens=2_000_000,
                        output_tokens=500_000, cost_usd=9.9)
    assert route.price({}, "chat", _RT_MODEL, u) is False and u.cost_usd == 0
    cfg = {"pricing": {"chat": {"default": [2.0, 0.5, 8.0]}}}
    assert route.price(cfg, "chat", _RT_MODEL, u) is True and u.cost_usd == 2.0 + 1.0 + 4.0
    cfg["pricing"]["chat"][_RT_MODEL] = [1.0, 0.0, 0.0]
    assert route.price(cfg, "chat", _RT_MODEL, u) is True and u.cost_usd == 1.0


def test_routed_runs_show_in_status_web_and_stats_runs(env, monkeypatch, capsys):
    from ttp import cli, route, web
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    _route_finish(p, d, tid, {"rc": 0}, result={"status": "done", "summary": "built"})
    tid = p.db.add_task("build more", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    _route_finish(p, d, tid, {"bridge": {"last_fault": "rate_limit"}})
    want = "routed 24h: 2 runs to chat, 1 fell back (rate_limit 1)"
    assert route.phrase(p.db) == want
    # routing left the one-line budget for the Budget tab and ttp status --verbose
    assert "routed" not in web.budget_line(p.db, time.time(), d.cfg.get("core_provider", "claude"), None, 0.0, {})
    assert want == web.health(p, p.db)["spend"]["route_line"]
    rows = route.runs(p.db, "light", time.time() - 3600)
    assert [(r["provider"], r["model"], r["fallback"], r["priced"]) for r in rows] == \
        [("chat", _RT_MODEL, "none", False), ("chat", _RT_MODEL, "rate_limit", False)]
    assert all(r["wall_s"] >= 29 for r in rows) and route.runs(p.db, "standard", time.time() - 3600) == []
    cli.main(["stats", "demo", "--runs", "--tier", "light"])
    out = capsys.readouterr().out
    assert "fallback (rate_limit)" in out and _RT_MODEL in out and "unpriced" in out
    # ttp status --verbose prints the routing line; plain ttp status does not
    text = cli.status_text(p, verbose=True)
    assert f"routing: {want}" in text and any(ln == f"routing: {want}" for ln in text.splitlines())
    assert "routed 24h" not in cli.status_text(p)


def test_route_config_problems():
    from ttp import route
    assert route.config_problems({}) == []
    raw = {"providers": {"chat": {"tiers": {"light": {"route": True}, "standard": {"model": "m", "route": "yes"}},
                                  "route_cooldown_s": -1},
                         "claude": {"tiers": {"light": {"route": True}}}}}
    got = "\n".join(route.config_problems(raw))
    for want in ("chat.tiers.light.route: set providers.chat.tiers.light.model", "standard.route: give true or false",
                 "claude.tiers.light.route: only providers.chat", "route_cooldown_s: give seconds"):
        assert want in got, got
    assert "core provider" in route.config_problems({"core_provider": "chat", "providers": {"chat": {
        "tiers": {"light": {"model": "m", "route": True}}}}})[0]
    assert route.cooldown_s({"providers": {"chat": {"route_cooldown_s": "x"}}}) == route.COOLDOWN_S
    # A routed run time limit must be positive seconds.
    for bad in (0, -5, "2700", True):
        got = route.config_problems({"providers": {"chat": {"tiers": {"light": {"route_timeout_s": bad}}}}})
        assert got and "light.route_timeout_s: give seconds" in got[0], (bad, got)
    # A routed tier whose requests would carry no reasoning effort is named; one with fields is not.
    tier = {"model": "m", "route": True, "effort": "low"}
    got = route.config_problems({"providers": {"chat": {"tiers": {"light": tier}}}})
    assert got == ["providers.chat.tiers.light: its routed requests carry no reasoning effort; set "
                   "providers.chat.tiers.light.effort and providers.chat.effort_fields.<model>.<effort> "
                   '(such as {"reasoning_effort": "low"})']
    assert route.config_problems({"providers": {"chat": {"tiers": {"light": {**tier, "route_timeout_s": 2700}},
                                                         "effort_fields": {"m": {"low": {"reasoning_effort": "low"}}}}}}) == []


def test_chatbridge_records_endpoint_fault_kinds_without_their_text(mock_chat):
    from ttp.chatbridge import Bridge, fault_of
    assert [fault_of(s) for s in (401, 403, 402, 429, 0, 500, 503, 404, 400)] == \
        ["auth", "auth", "rate_limit", "rate_limit", "down", "error", "error", "error", ""]
    assert fault_of(400, "Too Many Requests, slow down") == "rate_limit"
    hi = {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    up = mock_chat((500, {"error": {"message": f"boom {_CB_KEY}"}}), (429, {"error": {"message": "slow"}}),
                   (200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}))
    with Bridge(up.url, _CB_KEY, "m") as b:
        _post(b, hi)
        assert b.stats()["last_fault"] == "error"
        _post(b, hi)
        assert b.stats()["last_fault"] == "rate_limit"
        _post(b, hi)
        st = b.stats()
    assert st == {"last_fault": "", "faults": {"error": 1, "rate_limit": 1}}
    assert _CB_KEY not in json.dumps(st)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}/v1"
    with Bridge(dead, _CB_KEY, "m") as b:
        _post(b, hi)
        assert b.stats()["last_fault"] == "down"
    up = mock_chat((200, [{"choices": [{"delta": {"content": "par"}}]}, {"error": {"message": "rate limited", "code": 429}}]))
    with Bridge(up.url, _CB_KEY, "m") as b:
        _post(b, {**hi, "stream": True})
        assert b.stats()["last_fault"] == "rate_limit"


def test_a_routed_chat_run_keeps_the_key_out_of_its_files(env, tmp_path, mock_chat):
    up = mock_chat((503, {"error": {"message": f"down for {_CB_KEY}"}}))
    run_dir, seen, run_env = _chat_run(tmp_path, up)
    subprocess.run([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME), env=run_env,
                   timeout=_load_scaled(120))
    info = json.loads((run_dir / "exit.json").read_text())
    assert info["bridge"]["last_fault"] == "error", info
    for f in run_dir.rglob("*"):
        if f.is_file():
            assert _CB_KEY.encode() not in f.read_bytes(), f


# providers.chat.effort_fields: the run's effort to extra Chat Completions request fields, per model.
_EF = {"made-up-model-a": {"low": {"reasoning_effort": "low"}, "high": {"reasoning_effort": "xhigh"},
                           "*": {"reasoning_effort": "medium"}},
       "made-up-model-b": {"low": {"enable_thinking": False}, "max": {"thinking": {"type": "enabled"}}}}
_EF_HI = {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
_EF_OK = (200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})


def test_chatbridge_effort_fields_follow_the_run_effort_with_a_star_fallback():
    from ttp import chatbridge
    cfg = {"providers": {"chat": {"endpoint": "cloud", "effort_fields": _EF}}}
    got = {(m, e): chatbridge.settings(cfg, "light", m, e).get("fields")
           for m in ("made-up-model-a", "made-up-model-b", "made-up-model-c") for e in ("low", "high", "max", "")}
    assert got[("made-up-model-a", "low")] == {"reasoning_effort": "low"}
    assert got[("made-up-model-a", "high")] == {"reasoning_effort": "xhigh"}
    assert got[("made-up-model-a", "max")] == got[("made-up-model-a", "")] == {"reasoning_effort": "medium"}, "* fallback"
    assert got[("made-up-model-b", "low")] == {"enable_thinking": False}
    assert got[("made-up-model-b", "max")] == {"thinking": {"type": "enabled"}}
    assert got[("made-up-model-b", "high")] is None, "no entry and no *: nothing sent"
    assert all(got[("made-up-model-c", e)] is None for e in ("low", "high", "max", "")), "a model without a map"
    assert chatbridge.settings(cfg, "light", "made-up-model-a", "high")["effort"] == "high"
    # Unset: the settings are as before, with no effort or fields.
    plain = chatbridge.settings({"providers": {"chat": {"endpoint": "cloud"}}}, "light", "made-up-model-a", "high")
    assert "fields" not in plain and "effort" not in plain
    # An invalid map sends nothing, not part of it.
    bad = {"providers": {"chat": {"effort_fields": {"made-up-model-a": {"low": {"reasoning_effort": "low"},
                                                                         "huge": {"x": 1}}}}}}
    assert "fields" not in chatbridge.settings(bad, "light", "made-up-model-a", "low")


def test_chatbridge_effort_fields_config_problems():
    from ttp import chatbridge, project
    ok = {"providers": {"chat": {"endpoint": "cloud", "effort_fields": _EF}}}
    assert chatbridge.config_problems(ok) == []
    assert project.unknown_key_hint("providers.chat.effort_fields") is None
    for table, want in (({"m": {"huge": {"x": 1}}}, "effort_fields.m.huge: not an effort"),
                        ({"m": {"low": "low"}}, "effort_fields.m.low: give an object of request fields"),
                        ({"m": {"low": ["reasoning_effort", "low"]}}, "effort_fields.m.low: give an object"),
                        ({"m": "low"}, "effort_fields.m: give an object of efforts"),
                        ("low", "effort_fields: give an object of model names"),
                        ({"m": {"*": {"messages": [], "model": "x"}}}, "messages, model carries the conversation")):
        got = chatbridge.config_problems({"providers": {"chat": {"endpoint": "cloud", "effort_fields": table}}})
        assert len(got) == 1 and want in got[0], (table, got)
    # Through the project's own check, as `ttp config` and load report it.
    assert any("not an effort" in x for x in project.config_problems(
        {"providers": {"chat": {"endpoint": "cloud", "effort_fields": {"m": {"top": {}}}}}}))


def test_chatbridge_sends_the_effort_fields_over_claude_codes_own_and_never_over_the_conversation(mock_chat):
    from ttp.chatbridge import Bridge, CONVERSATION, with_fields
    up = mock_chat(_EF_OK, _EF_OK)
    fields = {"reasoning_effort": "xhigh", "enable_thinking": True}
    tools = [{"name": "Read", "description": "read", "input_schema": {"type": "object"}}]
    body = {**_EF_HI, "tools": tools, "thinking": {"type": "enabled", "budget_tokens": 9000},
            "output_config": {"effort": "low"}, "reasoning_effort": "low"}
    with Bridge(up.url, _CB_KEY, "made-up-model-a", effort="high", fields=fields) as b:
        assert _post(b, body)[0] == 200
        assert _post(b, {**body, "stream": False})[0] == 200
        st = b.stats()
    for seen in up.seen:
        sent = seen["body"]
        assert sent["reasoning_effort"] == "xhigh" and sent["enable_thinking"] is True
        assert "thinking" not in sent and "output_config" not in sent, "the daemon's effort wins"
        assert sent["model"] == "made-up-model-a" and sent["messages"] == [{"role": "user", "content": "hi"}]
        assert sent["tools"][0]["function"]["name"] == "Read"
    assert st["effort"] == "high" and st["fields"] == fields and st["rejected"] is False
    # Conversation fields are never replaced, whatever a map holds.
    req = {"model": "m", "messages": [1], "tools": [2], "stream": True, "max_tokens": 5}
    assert with_fields(req, {k: "x" for k in CONVERSATION} | {"top_k": 3}) == {**req, "top_k": 3}


def test_chatbridge_without_effort_fields_sends_the_request_unchanged(mock_chat):
    from ttp.chatbridge import Bridge, to_chat
    up = mock_chat(_EF_OK)
    with Bridge(up.url, _CB_KEY, "made-up-model-a", effort="high") as b:
        _post(b, _EF_HI)
        st = b.stats()
    assert up.seen[0]["body"] == to_chat(_EF_HI, "made-up-model-a")
    assert st == {"last_fault": "", "faults": {}}, "no effort record without fields"


@pytest.mark.parametrize("status", [400, 422])
def test_chatbridge_retries_once_without_rejected_effort_fields(mock_chat, status):
    from ttp.chatbridge import Bridge
    fields = {"reasoning_effort": "high"}
    no = (status, {"error": {"message": "Unsupported value: 'reasoning_effort' does not support 'high' with this model."}})
    up = mock_chat(no, _EF_OK, _EF_OK, (200, [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]))
    with Bridge(up.url, _CB_KEY, "made-up-model-a", effort="high", fields=fields) as b:
        code, text = _post(b, _EF_HI)
        assert code == 200 and json.loads(text)["content"][0]["text"] == "ok"
        _post(b, _EF_HI)
        code, text = _post(b, {**_EF_HI, "stream": True})
        st = b.stats()
    assert code == 200 and "message_stop" in text
    assert [("reasoning_effort" in s["body"]) for s in up.seen] == [True, False, False, False], \
        "retried without them, and the rest of the run goes without them"
    assert st["rejected"] is True and st["last_fault"] == "" and st["faults"] == {}


def test_chatbridge_other_errors_with_effort_fields_are_not_retried(mock_chat):
    from ttp.chatbridge import Bridge
    fields = {"reasoning_effort": "high"}
    up = mock_chat((400, {"error": {"message": "invalid tool schema"}}),
                   (400, {"error": {"message": "maximum context length is 8192 tokens; reasoning_effort high"}}),
                   (500, {"error": {"message": "reasoning_effort backend crashed"}}))
    with Bridge(up.url, _CB_KEY, "made-up-model-a", effort="high", fields=fields) as b:
        first = _post(b, _EF_HI)
        second = _post(b, _EF_HI)
        third = _post(b, _EF_HI)
        st = b.stats()
    assert len(up.seen) == 3 and all("reasoning_effort" in s["body"] for s in up.seen)
    assert first[0] == 400 and "invalid tool schema" in first[1]
    assert second[0] == 400 and "prompt is too long" in second[1], "a context error is not a rejected field"
    assert third[0] >= 500 and st["rejected"] is False


def test_a_chat_run_launches_its_bridge_with_the_effort_fields_of_its_effort(env):
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("providers.chat", {"endpoint": "cloud", "base_url": "https://api.example.com/v1",
                                    "effort_fields": _EF,
                                    "tiers": {"light": {"model": "made-up-model-a", "effort": "high"}}})
    d = Daemon(p.base)
    sent = {}
    for pin in ("", "low"):
        tid = p.db.add_task("build it", "s", kind="code", tier="light", origin="user", provider="chat",
                            **({"effort": pin} if pin else {}))
        cwd, _ = d._workdir_for(p.db.task(tid))
        rid = d.start_run("worker", "go", "chat", "light", cwd, task=p.db.task(tid))
        (p.runs / str(rid) / "STOP").touch()
        b = json.loads((p.runs / str(rid) / "run.json").read_text())["bridge"]
        sent[pin] = (b.get("effort"), b.get("fields"))
    assert sent == {"": ("high", {"reasoning_effort": "xhigh"}), "low": ("low", {"reasoning_effort": "low"})}


def test_a_routed_run_records_its_effort_fields_logs_a_rejection_once_and_shows_them_in_status(env, monkeypatch):
    from ttp import route, web
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p, effort_fields={_RT_MODEL: {"high": {"reasoning_effort": "xhigh"}}})
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    stats = {"last_fault": "", "faults": {}, "effort": "high", "fields": {"reasoning_effort": "xhigh"}}
    runs = [_route_finish(p, d, tid, {"rc": 0, "bridge": {**stats, "rejected": r}},
                          result={"status": "done", "summary": "built"})[0] for r in (True, True, False)]
    notes = [json.loads(r["note"])["route"]["effort"] for r in runs]
    assert notes[0] == {"effort": "high", "fields": {"reasoning_effort": "xhigh"}, "rejected": True}
    assert [n["rejected"] for n in notes] == [True, True, False]
    assert all(r["status"] == "ok" for r in runs), "a rejected field never fails the run"
    logged = (p.logs / "daemon.log").read_text().count("rejected its effort_fields")
    assert logged == 1, "logged once per model and effort"
    want = "light high: reasoning_effort=xhigh"
    assert route.phrase(p.db).endswith("; " + want)
    assert "routed" not in web.budget_line(p.db, time.time(), d.cfg.get("core_provider", "claude"), None, 0.0, {})
    assert web.health(p, p.db)["spend"]["route_line"].endswith("; " + want)
    _route_finish(p, d, tid, {"rc": 0, "bridge": {**stats, "rejected": True}})
    assert route.phrase(p.db).endswith(want + " (rejected)")
    plain = _route_finish(p, d, tid, {"rc": 0, "bridge": {"last_fault": "", "faults": {}}})[0]
    assert "effort" not in json.loads(plain["note"])["route"]


def test_route_phrase_counts_reruns_apart_from_real_fallbacks(env, monkeypatch):
    from ttp import route
    p = make(env)
    row = lambda fb, prov="chat": {"provider": prov, "to": "chat", "fallback": fb, "tier": "light", "effort": None}
    seen = {}
    monkeypatch.setattr(route, "_rows", lambda *a, **k: seen["rows"])
    seen["rows"] = [row("none"), row("timeout", "claude"), row("rerun", "claude"), row("rerun", "claude")]
    assert route.phrase(p.db) == "routed 24h: 1 run to chat, 1 fell back (timeout 1), 2 reruns on core"
    seen["rows"] = [row("none"), row("none")]
    assert route.phrase(p.db) == "routed 24h: 2 runs to chat"
    seen["rows"] = [row("none"), row("rate_limit", "claude")]
    assert route.phrase(p.db) == "routed 24h: 1 run to chat, 1 fell back (rate_limit 1)"



def _routed_run(p, tid, provider="chat", fallback="none", handoff=None):
    """A finished worker run of task `tid` with a route entry (light tier to chat); `handoff` writes its result.json."""
    d = p.root / "tmp-runs" / f"r{tid}-{time.time_ns()}"
    d.mkdir(parents=True)
    if handoff:
        (d / "result.json").write_text(json.dumps({"status": handoff, "summary": "x"}))
    note = {"route": {"tier": "light", "to": "chat", "model": _RT_MODEL, "fallback": fallback}}
    return p.db.x("INSERT INTO runs(task,role,provider,model,started,ended,status,dir,note) "
                  "VALUES(?,'worker',?,?,?,?,'ok',?,?)", (tid, provider, _RT_MODEL, time.time() - 60, time.time(),
                                                          str(d), json.dumps(note)))


def _escalation_start(p, monkeypatch, tid):
    from ttp import route
    from ttp.daemon import Daemon
    p.set_config("providers.chat.tiers.standard", {"model": _RT_MODEL, "route": True})   # a fix may start a tier up
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    prov, _, k = _dispatch_task(p, d, monkeypatch, tid)
    return prov, k["note"][route.KEY]


@pytest.mark.parametrize("fields,kind", [
    ({"spec": "s\nurgent: the release is blocked"}, "urgent"),   # an `urgent:` line
    ({"priority": 1}, "urgent"),                                   # the urgent lane's priority
    ({"origin": "user"}, "user-ordered"),                          # added by the user
    ({"reply_chat": "c1"}, "user-ordered"),                        # answers a user's chat (the floor keeps it too)
    ({}, ""),                                                      # routine work routes
])
def test_an_urgent_or_user_ordered_task_starts_on_core_as_escalated(env, monkeypatch, fields, kind):
    from ttp import route
    p = make(env)
    _route_config(p)
    f = {"spec": "s", "priority": 3, **fields}
    tid = p.db.add_task("t", f.pop("spec"), kind="code", tier="light", budget_usd=8.0, labels=[], **f)
    if "reply_chat" in fields:   # a chat answer runs on core by the conclusion floor before routing
        assert route.escalation(p.db, p.db.task(tid))["escalated"] == kind
        return
    prov, e = _escalation_start(p, monkeypatch, tid)
    if not kind:
        assert prov == "chat" and e["fallback"] == "none"
        return
    assert prov != "chat" and e["fallback"] == route.ESCALATED and e["escalated"] == kind, e
    assert e["cause"].startswith("urgent: the release" if "spec" in fields else ("urgent" if kind == "urgent" else "ordered"))


@pytest.mark.parametrize("written_on", ["chat", "claude"])
def test_a_review_fix_of_a_change_written_on_the_routed_provider_starts_on_core(env, monkeypatch, written_on):
    from ttp import route
    p = make(env)
    _route_config(p)
    code = p.db.add_task("build", "s", kind="code", tier="light", priority=3, budget_usd=8.0, labels=[])
    _routed_run(p, code, provider=written_on, handoff="done")
    p.db.update_task(code, status="done")
    fix = p.db.add_task(f"Fix review #99: build", "s", kind="code", tier="light", priority=3, budget_usd=8.0,
                        labels=[f"continues:{code}", "review_fix:99"])
    prov, e = _escalation_start(p, monkeypatch, fix)
    if written_on == "chat":
        assert prov != "chat" and e["fallback"] == route.ESCALATED and e["escalated"] == "review fix", e
    else:   # a change written on core: its fix routes as any task
        assert prov == "chat" and e["fallback"] == "none", e


@pytest.mark.parametrize("last_on_chat", [True, False])
def test_a_checks_fix_wake_of_a_routed_change_starts_on_core_and_counts_once_as_sent_back(env, monkeypatch, last_on_chat):
    from ttp import route
    from ttp.chaincap import chain
    p = make(env)
    _route_config(p)
    tid = p.db.add_task("build", "s", kind="code", tier="light", priority=3, budget_usd=8.0, labels=[])
    rid = _routed_run(p, tid, handoff="waiting")
    if not last_on_chat:   # a core run after the routed one wrote the change under check
        rid = _routed_run(p, tid, provider="claude", fallback="busy", handoff="waiting")
    p.db.update_task(tid, result=json.dumps({"status": "waiting", "woke": "its checks failed (exit 1); the end ..."}))
    prov, e = _escalation_start(p, monkeypatch, tid)
    assert prov != "chat" and e["fallback"] == route.ESCALATED and e["escalated"] == "checks fix", e
    assert (e.get("sent_back"), e.get("after")) == (("checks", rid) if last_on_chat else (None, None))
    # Two starts on the same failed checks of one routed change are one miss; without the wake it routes.
    for _ in range(2):
        p.db.x("INSERT INTO runs(task,role,provider,started,status,note) VALUES(?,'worker','claude',?,'ok',?)",
               (tid, time.time(), json.dumps({"route": {"to": "chat", "fallback": "escalated", **e}})))
    assert route.misses(p.db, chain(p.db, tid)) == ([f"task {tid} sent back by checks"] if last_on_chat else [])
    p.db.update_task(tid, result=json.dumps({"status": "waiting"}))
    assert route.escalation(p.db, p.db.task(tid)) == {}


def test_a_lineage_with_two_routed_misses_stays_on_core_and_one_miss_still_routes(env, monkeypatch):
    from ttp import route
    from ttp.chaincap import chain
    p = make(env)
    _route_config(p)
    root = p.db.add_task("build", "s", kind="code", tier="light", priority=3, budget_usd=8.0, labels=[])
    _routed_run(p, root, fallback="timeout")              # fell back on its own: miss 1
    _routed_run(p, root, provider="claude", fallback="rerun", handoff="done")
    _routed_run(p, root, provider="claude", fallback="busy", handoff="failed")   # a core run's failure: no miss
    _routed_run(p, root, fallback="down")                 # an endpoint fault: not the run's own, no miss
    p.db.update_task(root, status="done")
    hop = p.db.add_task("continue: build", "s", kind="code", tier="light", priority=3, budget_usd=8.0,
                        labels=[f"continues:{root}"])
    assert route.misses(p.db, chain(p.db, hop)) == [f"run {p.db.one('SELECT MIN(id) i FROM runs')['i']} fell back"]
    prov, e = _escalation_start(p, monkeypatch, hop)
    assert prov == "chat" and e["fallback"] == "none", e
    _routed_run(p, hop, handoff="failed")                  # handed off failed on chat: miss 2
    p.db.update_task(hop, status="done")
    nxt = p.db.add_task("continue: build again", "s", kind="code", tier="light", priority=3, budget_usd=8.0,
                        labels=[f"continues:{hop}"])
    prov, e = _escalation_start(p, monkeypatch, nxt)
    assert prov != "chat" and e["fallback"] == route.ESCALATED and e["escalated"] == "lineage", e
    assert "2 routed misses" in e["cause"] and "failed" in e["cause"]
    # A review that sends a routed change back is a miss too.
    other = p.db.add_task("other", "s", kind="code", tier="light", priority=3, budget_usd=8.0, labels=[])
    _routed_run(p, other, handoff="done")
    fix = p.db.add_task("Fix review #7: other", "s", kind="code", tier="light", priority=3, budget_usd=8.0,
                        labels=[f"continues:{other}", "review_fix:7"])
    assert route.misses(p.db, chain(p.db, fix)) == [f"task {other} sent back by review"]


def test_escalated_starts_are_counted_apart_in_status_and_stats_runs(env, monkeypatch):
    from ttp import route
    p = make(env)
    tid = p.db.add_task("t", "s", kind="code", tier="light", priority=3, budget_usd=8.0, labels=[])
    for kind in ("urgent", "lineage", "urgent"):
        p.db.x("INSERT INTO runs(task,role,provider,started,ended,status,note) VALUES(?,'worker','claude',?,?,'ok',?)",
               (tid, time.time() - 5, time.time(), json.dumps({"route": {"tier": "light", "to": "chat", "fallback": "escalated",
                                                                          "escalated": kind, "cause": kind}})))
    _routed_run(p, tid)
    assert route.phrase(p.db) == "routed 24h: 1 run to chat, 3 escalated to core"
    rows = route.runs(p.db, "light", time.time() - 3600)
    assert [r.get("escalated") for r in rows] == ["urgent", "lineage", "urgent", None]
    text = route.runs_text(rows)
    assert "ok (escalated)" in text and text.splitlines()[-1] == "escalated to core: 3 (lineage 1, urgent 2)"

# A stand-in for Claude Code's init event: the user's MCP servers load unless --strict-mcp-config (or
# deniedMcpServers names them), the user's plugins unless --setting-sources leaves out "user".
_FAKE_INIT = r'''
import json, sys
a = sys.argv[1:]
def val(flag, default):
    return a[a.index(flag) + 1] if flag in a else default
settings = json.loads(val("--settings", "{}"))
denied = {d.get("serverName") for d in settings.get("deniedMcpServers") or []}
servers = [] if "--strict-mcp-config" in a else [n for n in json.loads(sys.stdin.read()) if n not in denied]
plugins = ["user-plugin"] if "user" in val("--setting-sources", "user,project,local").split(",") else []
print(json.dumps({"type": "system", "subtype": "init", "mcp_servers": [{"name": n} for n in servers],
                  "plugins": [{"name": n} for n in plugins]}))
'''


def _fake_init(argv, user_servers):
    out = subprocess.run([sys.executable, "-c", _FAKE_INIT, *argv[1:]], input=json.dumps(user_servers),
                         capture_output=True, text=True, check=True).stdout
    event = json.loads(out)
    return [s["name"] for s in event["mcp_servers"]], [x["name"] for x in event["plugins"]]


def test_chat_workers_inherit_claude_worker_isolation_and_withheld_connectors(env, monkeypatch, tmp_path):
    """providers.chat with no worker_isolation or mcp_withheld of its own takes claude's (project.CHAT_INHERITS):
    a routed model gets none of the user's MCP servers or plugins that claude workers are kept from."""
    from ttp import daemon as dmod
    from ttp.daemon import Daemon
    p = make(env)
    _chat_config(p)
    _claude_mcp_config(tmp_path, monkeypatch, p)
    user_servers = ["docs", "other", "claude.ai Gmail"]

    class Proc:   # never a real runner
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))

    def start():
        d = Daemon(p.base)
        monkeypatch.setattr(d, "observe", lambda *a, **k: None)
        tid = p.db.add_task("build it", "s", kind="code", tier="standard", origin="user", provider="chat")
        cwd, _ = d._workdir_for(p.db.task(tid))
        rid = d.start_run("worker", "go", "chat", "standard", cwd, task=p.db.task(tid), budget_usd=4.0)
        p.db.x("UPDATE runs SET status='ok', ended=? WHERE status='running'", (time.time(),))
        p.db.x("UPDATE tasks SET status='done' WHERE status!='done'")
        return json.loads((p.runs / str(rid) / "run.json").read_text())["argv"]

    def isolated(argv):
        i = argv.index("--setting-sources") if "--setting-sources" in argv else -1
        return "--strict-mcp-config" in argv and i >= 0 and argv[i + 1] == "project,local"

    def denied(argv):
        return {d["serverName"] for d in json.loads(argv[argv.index("--settings") + 1]).get("deniedMcpServers", [])}

    # Isolation off everywhere: the chat run loads the user's servers and plugins, as before.
    p.set_config("providers.claude.worker_isolation", False)
    argv = start()
    assert not isolated(argv) and _fake_init(argv, user_servers) == (user_servers, ["user-plugin"])
    # Claude isolated, chat's key unset: chat is isolated too, and its init lists nothing of the user's.
    p.set_config("providers.claude.worker_isolation", True)
    assert p.config()["providers"]["chat"]["worker_isolation"] is True
    assert "worker_isolation" not in p.raw_config()["providers"]["chat"], "inherited, never written"
    argv = start()
    assert isolated(argv), argv
    assert _fake_init(argv, user_servers) == ([], [])
    # An explicit chat false wins; an explicit chat true holds with claude off.
    p.set_config("providers.chat.worker_isolation", False)
    assert not isolated(start())
    p.set_config("providers.claude.worker_isolation", False)
    p.set_config("providers.chat.worker_isolation", True)
    assert isolated(start())
    # Connectors: a chat run keeps the ones providers.chat.mcp_servers asks for, minus claude's withheld
    # list (inherited while chat's own is unset); every other server is denied by name.
    _host_list(p)
    p.set_config("providers.chat.mcp_servers", ["claude.ai Gmail", "claude.ai Example Notes"])
    p.set_config("providers.claude.mcp_withheld", ["claude.ai Example Notes"])
    assert p.config()["providers"]["chat"]["mcp_withheld"] == ["claude.ai Example Notes"]
    argv = start()
    assert "--strict-mcp-config" not in argv and argv[argv.index("--setting-sources") + 1] == "project,local"
    assert "claude.ai Example Notes" in denied(argv) and "claude.ai Gmail" not in denied(argv)
    assert _fake_init(argv, ["docs", "claude.ai Gmail", "claude.ai Example Notes"]) == (["claude.ai Gmail"], [])
    # Chat's own list wins over the inherited one.
    p.set_config("providers.chat.mcp_withheld", [])
    argv = start()
    assert not {"claude.ai Example Notes", "claude.ai Gmail"} & denied(argv)
    # Every connector asked for is withheld (however it is written): the run stays strict.
    p.set_config("providers.chat.mcp_withheld", ["claude.ai Gmail", "claude_ai_Example_Notes"])
    assert isolated(start())


def test_chat_inherits_only_into_a_configured_chat_provider():
    from ttp.project import _chat_inherits
    cfg = {"providers": {"claude": {"worker_isolation": True, "mcp_withheld": ["a"]}}}
    _chat_inherits(cfg)
    assert "chat" not in cfg["providers"], "no chat section is made up"
    cfg["providers"]["chat"] = {"endpoint": "x", "mcp_withheld": None}
    _chat_inherits(cfg)
    assert cfg["providers"]["chat"] == {"endpoint": "x", "worker_isolation": True, "mcp_withheld": ["a"]}
    cfg["providers"]["claude"]["mcp_withheld"].append("b")
    assert cfg["providers"]["chat"]["mcp_withheld"] == ["a"], "a copy, never claude's own list"
    for bad in ({"providers": {"chat": "x", "claude": {}}}, {"providers": ["chat"]}, {}):
        _chat_inherits(bad)   # a broken section is left as it is


def test_doctor_prints_worker_isolation_per_provider_and_the_coordinator_may_set_chats(env, monkeypatch, capsys):
    import types
    from ttp import cli
    from ttp import coordinator as coord
    p = make(env)
    monkeypatch.setattr(cli, "need", lambda *a: p)
    p.set_config("providers.claude.worker_isolation", True)

    def line():
        cli.cmd_doctor(types.SimpleNamespace(name="demo"))
        return next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("worker isolation: "))
    assert "chat" not in line(), "no chat line while chat is not configured"
    _chat_config(p)
    assert "claude on" in line() and "chat on (from claude)" in line()
    assert coord.apply(p, [{"type": "config_set", "key": "providers.chat.worker_isolation", "value": "off"}]) == []
    assert p.raw_config()["providers"]["chat"]["worker_isolation"] is False
    assert "chat off" in line() and "(from claude)" not in line()


# The stall limit on chat-route runs: a run that goes silent, or only sleep-polls between quick checks,
# ends at stall_s and falls back once; the route timeout stays the hard cap.
_WAIT_AGENT = r'''
import json, sys, time
mode, length = sys.argv[1], float(sys.argv[2])
def w(**kw):
    sys.stdout.write(json.dumps(kw) + "\n"); sys.stdout.flush()
def call(i, command):
    w(type="assistant", message={"content": [{"type": "tool_use", "id": i, "name": "Bash",
                                              "input": {"command": command, "timeout": 600000}}]})
def done(i):
    w(type="user", message={"content": [{"type": "tool_result", "tool_use_id": i, "content": "ok"}]})
w(type="system", subtype="init")
end, n = time.time() + length, 0
while time.time() < end:
    n += 1
    if mode == "silent":
        time.sleep(length)
    elif mode == "poll":      # a long sleep, then a quick look: what runs 8861, 9369 and 10130 did
        call(f"w{n}", "sleep 590; ttp checks --result run-1")
        time.sleep(0.4)
        done(f"w{n}")
        call(f"q{n}", "ps -p 1")
        done(f"q{n}")
    else:                     # real work that reports regularly
        call(f"b{n}", "python3 -m pytest -q tests/a.py")
        time.sleep(0.2)
        done(f"b{n}")
w(type="result", result="done")
'''


def _wait_run(tmp_path, mode, length, waits_idle=True, stall_s=2, timeout_s=60):
    from ttp import runner
    agent = tmp_path / "agent.py"
    agent.write_text(_WAIT_AGENT)
    run_dir = tmp_path / f"run-{mode}-{waits_idle}"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    (run_dir / "run.json").write_text(json.dumps({
        "argv": [sys.executable, str(agent), mode, str(length)], "cwd": str(tmp_path), "provider": "fake",
        "timeout_s": timeout_s, "stall_s": stall_s, **({"waits_idle": True} if waits_idle else {})}))
    runner.supervise(run_dir)
    return json.loads((run_dir / "exit.json").read_text())


@pytest.mark.parametrize("mode", ["silent", "poll"])
def test_a_chat_run_that_goes_silent_or_only_sleep_polls_ends_at_stall_s_and_falls_back_once(env, tmp_path, monkeypatch, mode):
    from ttp import route
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.1")
    info = _wait_run(tmp_path, mode, 120)
    assert info["stopped"] == "stalled", info
    assert info["ended"] - info["started"] < 20, "ended near stall_s (2 s), not at timeout_s (60 s)"
    # The daemon books it as a fault of the route: one fallback to the core provider, same task, spec,
    # tier and budget, then no more fallbacks for that task.
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    core = d.cfg.get("core_provider", "claude")
    tid = p.db.add_task("build", "the spec", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    run, task = _route_finish(p, d, tid, {"rc": info["rc"], "stopped": info["stopped"],
                                          "bridge": {"last_fault": "", "faults": {}}})
    assert run["status"] == "fallback" and route.of(run["note"])["fallback"] == "timeout"
    assert task["status"] == "queued" and int(task["attempts"] or 0) == 0
    assert (task["title"], task["spec"], task["budget_usd"]) == ("build", "the spec", 8.0)
    prov, tier, k = _dispatch_task(p, d, monkeypatch, tid)
    assert (prov, tier) == (core, "light") and k["note"][route.KEY]["fallback"] == "rerun"
    assert route.fell_back_before(p.db, tid) == run["id"]


@pytest.mark.parametrize("mode,waits_idle", [("busy", True), ("poll", False)])
def test_a_run_that_reports_regularly_or_is_not_a_chat_run_is_not_stalled_by_its_waits(env, tmp_path, monkeypatch,
                                                                                      mode, waits_idle):
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.1")
    info = _wait_run(tmp_path, mode, 5, waits_idle=waits_idle)
    assert info["rc"] == 0 and not info.get("stopped"), info


def test_only_a_chat_run_counts_its_waits_against_the_stall_limit(env, monkeypatch):
    import subprocess as sp
    from ttp.daemon import Daemon
    real, started = sp.Popen, []

    def popen(argv, *a, **k):   # the runner itself is not started: only its run.json is read
        if list(argv[1:3]) == ["-m", "ttp.runner"]:
            argv = [sys.executable, "-c", "pass"]
        started.append(real(argv, *a, **k))
        return started[-1]
    monkeypatch.setattr(sp, "Popen", popen)
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    for prov, want in (("chat", True), ("fake", None)):
        rid = d.start_run("worker", "go", prov, "light", str(p.root))
        spec = json.loads((p.runs / str(rid) / "run.json").read_text())
        assert spec.get("waits_idle") is want and spec["stall_s"] == d.cfg["budget"]["stall_s"]["light"], spec
    for proc in started:
        proc.wait(timeout=30)


@pytest.mark.parametrize("command,wait", [
    ("sleep 590; ttp checks --result /x", True),
    ("sleep 30", True),
    ("sleep 5m && ps", True),
    ("x=1; sleep 1.5h", True),
    ("timeout 550 bash -c 'while true; do ttp checks --result /x && exit 0; sleep 25; done'", True),
    ("for i in 1 2 3; do ps -e | wc -l; sleep 20; done", True),
    ("until test -e /x; do sleep 5; done", True),
    ("python3 -c 'import time; time.sleep(600)'", True),
    ("sleep 5; ls", False),                       # a short pause is no wait
    ("python3 -m pytest -q -k sleep tests/a.py", False),
    ("grep -n sleep_s runner.py; echo sleep", False),
    ("ttp hours --sleep 600", False),             # an option, not the command
    ("ls; nosleep 600", False),
    ("for f in a b; do cat $f; done", False),     # a loop without a sleep
    # Blocking waits that never sleep: they hold the call until its timeout.
    ("tail -f /x/checks.log", True),
    ("timeout 550 tail -n 50 -F /x/log", True),
    ("cd /x && tail --follow=name log", True),
    ("watch -n 30 ttp checks --result /x", True),
    ("gh run watch 123", True),
    ("ps; watch ls", True),
    ("timeout 550 ttp checks --result /x", False),  # returns at once (exit 0 or 1)
    ("tail -n 50 /x/log", False),
    ("grep -n watch runner.py; grep -c 'tail -f' a.md", False),   # words in arguments
    ("tail -n 5 file-f", False),
    ("npm run build:watch", False),
    (None, False), (42, False),
])
def test_is_wait_tells_waits_from_work(command, wait):
    from ttp import runner
    assert runner.is_wait(command) is wait


def test_waited_counts_overlapping_and_open_waits_once_within_the_window(tmp_path):
    from ttp import runner
    out = tmp_path / "output.jsonl"
    ev = lambda **kw: json.dumps(kw) + "\n"
    use = lambda i, c: ev(type="assistant", message={"content": [{"type": "tool_use", "id": i, "input": {"command": c}}]})
    res = lambda i: ev(type="user", message={"content": [{"type": "tool_result", "tool_use_id": i}]})
    out.write_text(use("a", "sleep 300") + use("b", "sleep 400"))   # two waits side by side
    w = runner.ProgressWatch(out, tmp_path)
    w.poll(100.0)
    assert w.waited(150.0, 900) == 50.0, "open waits run until now, counted once"
    with open(out, "a") as f:
        f.write(res("a"))
    w.poll(400.0)
    with open(out, "a") as f:
        f.write(res("b") + use("c", "ls") + res("c"))
    w.poll(500.0)
    assert w.waited(500.0, 900) == 400.0
    assert w.waited(1300.0, 900) == 100.0, "only the part inside the window"
    assert w.waited(1500.0, 900) == 0.0 and w.waits == [], "old waits are dropped"


# A chat-route run is told to hand off `waiting` instead of waiting in a tool call (hook.idle_wait),
# so its waits never end it as stalled and rerun it on another provider.
_HOOK_AGENT = r'''
import json, sys
sys.path[:0] = json.loads(sys.argv[1])
from ttp import hook
out = {}
for c in json.loads(sys.argv[2]):
    got, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": c}, "cwd": "."})
    out[c] = (got or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason")
open(sys.argv[3], "w").write(json.dumps(out))
print(json.dumps({"type": "result", "result": "done"}))
'''
_WAITS = ["sleep 590; ttp checks --result run-1", "sleep 30",
          "timeout 550 bash -c 'while true; do ttp checks --result r && exit 0; sleep 25; done'",
          "until test -e /x; do sleep 5; done", "tail -f /x/log", "watch -n 30 ls"]
_WORK = ["sleep 5; ls", "timeout 550 ttp checks --result run-1", "python3 -m pytest -q tests/a.py",
         "grep -n 'tail -f' a.md"]


@pytest.mark.parametrize("waits_idle", [True, False])
def test_a_chat_run_is_told_to_hand_off_waiting_for_a_wait_call_and_other_runs_may_wait(env, tmp_path, waits_idle):
    from ttp import runner
    agent, got = tmp_path / "agent.py", tmp_path / "got.json"
    agent.write_text(_HOOK_AGENT)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    (run_dir / "run.json").write_text(json.dumps({
        "argv": [sys.executable, str(agent), json.dumps(sys.path), json.dumps(_WAITS + _WORK), str(got)],
        "cwd": str(tmp_path), "provider": "fake", "timeout_s": 60, "stall_s": 30,
        "env": {"TTP_RUN_DIR": str(run_dir)}, **({"waits_idle": True} if waits_idle else {})}))
    runner.supervise(run_dir)
    out = json.loads(got.read_text())
    for c in _WAITS:
        if waits_idle:
            assert "hand off `waiting`" in out[c].lower() and "ttp checks --result <run dir>" in out[c], (c, out[c])
        else:
            assert out[c] is None, f"a run that is not a chat run may wait: {c!r}: {out[c]}"
    for c in _WORK:
        assert out[c] is None, f"no wait: {c!r}: {out[c]}"


def test_a_wait_call_is_refused_only_in_a_run(env, monkeypatch):
    from ttp import hook
    monkeypatch.setenv("TTP_WAITS_IDLE", "1")
    monkeypatch.delenv("TTP_RUN_DIR", raising=False)
    assert hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "sleep 600"}}) == (None, None)
    for val in ("", "0", "yes"):
        monkeypatch.setenv("TTP_WAITS_IDLE", val)
        assert hook.idle_wait("sleep 600") is None, val


# The ranking refresher (rankfresh.py): fake names and a fake index, never the network.
def _fake_index(*entries, version="v-test", more=False):
    """One index page: (slug, score) pairs."""
    return {"tier": "free", "intelligence_index_version": version, "pagination": {"has_more": more},
            "data": [{"id": f"uuid-{i}", "slug": slug, "name": slug.upper(),
                      "evaluations": {"artificial_analysis_intelligence_index": score}}
                     for i, (slug, score) in enumerate(entries)]}


def _rank_home(env, monkeypatch):
    from ttp import project, rankfresh
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "rank-home")
    monkeypatch.delenv(rankfresh.KEY_ENV, raising=False)
    return project.HOME_DIR


def test_the_account_model_ranking_applies_under_a_projects_own(env, monkeypatch):
    """User's order: every project using the chat provider gets the ranking; account-wide, a project's own wins."""
    from ttp import cli, modelrank, project, rankfresh
    home = _rank_home(env, monkeypatch)
    p = make(env)
    _route_config(p)
    cli.main(["config", "--account", "budget.timezone", "UTC"])   # an older account key stays beside it
    cli.main(["config", "--account", rankfresh.KEY, '["rank-a", "rank-b"]'])
    raw = json.loads((home / "settings.json").read_text())
    assert raw["providers"]["chat"]["model_ranking"] == ["rank-a", "rank-b"] and raw["budget"]["timezone"] == "UTC"
    assert "providers" not in project.load_account_settings()   # never merged into a project's settings
    assert "chat" not in (project.layered({}).get("providers") or {})
    assert modelrank.ranking({"providers": {"chat": {"endpoint": "x"}}}) == ["rank-a", "rank-b"]
    assert modelrank.ranking({}) == ["rank-a", "rank-b"]
    assert modelrank.ranking({"providers": {"chat": {"model_ranking": ["own"]}}}) == ["own"]
    assert modelrank.ranking({"providers": {"chat": {"model_ranking": []}}}) == []   # a project may turn it off
    # Another account key set later keeps the ranking; '' removes it; each change is in the ranking log.
    cli.main(["config", "--account", "budget.timezone", "UTC"])
    assert project.account_value(rankfresh.KEY) == ["rank-a", "rank-b"]
    cli.main(["config", "--account", rankfresh.KEY, ""])
    assert project.account_value(rankfresh.KEY) is None and "providers" not in json.loads((home / "settings.json").read_text())
    log = [json.loads(x) for x in (home / rankfresh.LOG).read_text().splitlines()]
    assert [(r["old"], r["new"], r["trigger"]) for r in log] == [([], ["rank-a", "rank-b"], "set"), (["rank-a", "rank-b"], [], "set")]
    with pytest.raises(SystemExit):
        cli.main(["config", "--account", rankfresh.KEY, "3"])
    with pytest.raises(SystemExit):
        cli.main(["config", "--account", rankfresh.EVERY_KEY, "60"])
    # The source and key file decide where the key goes: a run may only clear them.
    monkeypatch.setenv("TTP_RUN_ID", "1")
    for k, v in ((rankfresh.SOURCE_KEY, "https://example.invalid/x"), (rankfresh.KEY_FILE_KEY, "/tmp/k")):
        with pytest.raises(SystemExit):
            cli.main(["config", "--account", k, v])
        assert project.account_value(k) is None


def test_rank_maps_served_ids_to_the_index_by_normalised_name():
    from ttp import rankfresh
    entries, version = rankfresh.fetch("https://example.invalid/models", "k", lambda url, key: _fake_index(
        ("alpha-2-5", 60), ("alpha-2-5-reasoning", 70), ("beta-7b", 50), ("gamma-1", 65), ("unscored", None)))
    assert version == "v-test"
    served = ["Org/Beta-7B-FP8", "zz-unknown", "org/alpha-2.5:latest", "aa-unknown", "Gamma_1"]
    assert rankfresh.rank(served, entries) == (["org/alpha-2.5:latest", "Gamma_1", "Org/Beta-7B-FP8", "aa-unknown",
                                                "zz-unknown"], ["aa-unknown", "zz-unknown"])
    # Pages are followed while the index says there are more.
    pages = {"https://example.invalid/m": _fake_index(("one", 1), more=True),
             "https://example.invalid/m?page=2": _fake_index(("two", 2))}
    assert len(rankfresh.fetch("https://example.invalid/m", "k", lambda url, key: pages[url])[0]) == 2
    for bad in ({"data": "x"}, [], {"data": []}):
        with pytest.raises(ValueError):
            rankfresh.fetch("https://example.invalid/m", "k", lambda url, key, b=bad: b)


def test_the_ranking_refresh_logs_each_change_and_a_failed_fetch_keeps_the_last(env, monkeypatch):
    from ttp import modelrank, project, rankfresh
    home = _rank_home(env, monkeypatch)
    calls, answer = [], [_fake_index(("rank-a", 70), ("rank-b", 60))]

    def get(url, key):
        calls.append((url, key))
        if isinstance(answer[0], Exception):
            raise answer[0]
        return answer[0]
    now = 1_000_000.0
    served = ["rank-b", "rank-a", "odd-one"]
    assert rankfresh.due(served, now) == "weekly"   # never refreshed
    res = rankfresh.refresh(served, "weekly", now, "secret-key", get)
    assert res["changed"] and res["new"] == ["rank-a", "rank-b", "odd-one"] and res["unmapped"] == ["odd-one"]
    assert modelrank.ranking({}) == ["rank-a", "rank-b", "odd-one"] and calls[0][1] == "secret-key"
    assert "odd-one" in rankfresh.change_text(res) and "Artificial Analysis" in rankfresh.change_text(res)
    # Not due again until a week passes; the same answer then changes nothing and logs nothing.
    assert rankfresh.due(served, now + 3600) == "" and rankfresh.refresh(served, "weekly", now + 3600, "k", get) == {}
    week = now + rankfresh.EVERY_S
    assert rankfresh.due(served, week) == "weekly" and not rankfresh.refresh(served, "weekly", week, "k", get)["changed"]
    # A failed fetch, then an answer matching nothing: the ranking stays, one outage, retried hourly.
    for i, fault in enumerate((OSError("down"), _fake_index(("other", 1)))):
        answer[0] = fault
        t = week + rankfresh.EVERY_S + i * rankfresh.RETRY_S
        res = rankfresh.refresh(served, "weekly", t, "k", get)
        assert res["error"] and res["outage_new"] == (i == 0) and res["since"] == week + rankfresh.EVERY_S
        assert modelrank.ranking({}) == ["rank-a", "rank-b", "odd-one"]
        assert rankfresh.read_state()["outage"]["error"] is True
        assert rankfresh.due(served, t + 60) == "" and rankfresh.due(served, t + rankfresh.RETRY_S) == "weekly"
    answer[0] = _fake_index(("rank-a", 70), ("rank-b", 80), version="v-2")
    res = rankfresh.refresh(served, "weekly", t + rankfresh.RETRY_S, "k", get)
    assert res["changed"] and res["ended"] and rankfresh.read_state()["outage"] is None
    log = [json.loads(x) for x in (home / rankfresh.LOG).read_text().splitlines()]
    assert [(r["old"], r["new"], r["trigger"], r["version"]) for r in log] == [
        ([], ["rank-a", "rank-b", "odd-one"], "weekly", "v-test"),
        (["rank-a", "rank-b", "odd-one"], ["rank-b", "rank-a", "odd-one"], "weekly", "v-2")]
    assert all("Artificial Analysis" in r["source"] and r["unmapped"] == ["odd-one"] for r in log)
    for f in ("settings.json", rankfresh.LOG, rankfresh.STATE):   # the key is never stored
        assert "secret-key" not in (home / f).read_text()
    assert project.account_value(rankfresh.KEY) == ["rank-b", "rank-a", "odd-one"]


def test_a_newly_served_model_refreshes_the_ranking_through_the_daemon(env, monkeypatch):
    """The served-models check lists a new model: the daemon refreshes at once; no key, one user-only item."""
    from ttp import alerts, modelrank, rankfresh, route
    from ttp.daemon import Daemon
    home = _rank_home(env, monkeypatch)
    monkeypatch.setenv("TTP_TEST_MODEL_RANKING", "1")
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    d._load_config()
    posted, seen = [], []
    monkeypatch.setattr(d, "alert", lambda key, text, severity="high", **k: posted.append((key, text, severity)))
    monkeypatch.setattr(d, "observe", lambda source, text, **k: seen.append((source, text, k)))
    index = [_fake_index(("rank-a", 70), ("rank-b", 60))]
    monkeypatch.setattr(rankfresh, "_get", lambda url, key: index[0])

    def tick():
        d._rank_tried = 0.0
        d.refresh_model_ranking()
        if d._rank_job:
            d._rank_job.join(10)
        d.refresh_model_ranking()   # posts what the thread found
    route.write_served(p.state, "chat", True, "HTTP 200", ["rank-b"], time.time())
    tick()   # no key: nothing fetched, one user-only item
    assert posted == [(rankfresh.NEEDS_KEY, rankfresh.NEEDS_KEY_TEXT, "normal")] and not (home / rankfresh.STATE).exists()
    assert alerts.holds(p.db, rankfresh.NEEDS_KEY, 0, time.time())
    keyfile = env["tmp"] / "aa-key"
    keyfile.write_text("file-key\n")
    from ttp import project
    project.set_account_setting(rankfresh.KEY_FILE_KEY, str(keyfile))
    assert rankfresh.api_key() == "file-key" and not alerts.holds(p.db, rankfresh.NEEDS_KEY, 0, time.time())
    posted.clear()
    tick()
    assert modelrank.ranking(d.cfg) == ["rank-b"] and len(posted) == 1 and posted[0][2] == "low"
    tick()   # nothing new: no refresh, no line
    assert len(posted) == 1
    route.write_served(p.state, "chat", True, "HTTP 200", ["rank-b", "rank-a"], time.time())
    assert rankfresh.due(modelrank.served(p.state), time.time()) == "new model"
    tick()
    assert modelrank.ranking(d.cfg) == ["rank-a", "rank-b"] and len(posted) == 2
    assert json.loads((home / rankfresh.LOG).read_text().splitlines()[-1])["trigger"] == "new model"
    # The index goes away: one observation, flagged an error; the ranking stays.
    index[0] = {"error": "gone"}
    route.write_served(p.state, "chat", True, "HTTP 200", ["rank-b", "rank-a", "rank-c"], time.time())
    tick()
    d._rank_tried = 0.0
    d.refresh_model_ranking()   # not due again within the hour
    assert modelrank.ranking(d.cfg) == ["rank-a", "rank-b"] and len(posted) == 2
    assert len(seen) == 1 and seen[0][0] == "model-ranking" and seen[0][1].startswith("error:")
    # A project with no chat endpoint never refreshes.
    assert not rankfresh.configured({"providers": {"chat": {"model_ranking": ["x"]}}}) and not rankfresh.configured({})
