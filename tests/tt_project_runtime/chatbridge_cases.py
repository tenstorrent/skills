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
        mock = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

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
                data = json.dumps(reply).encode()
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
    time.sleep(float(sys.argv[2]))
'''


def _chat_run(tmp_path, mock, extra=(), timeout_s=60, alias="cloud"):
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
        "bridge": {"endpoint": "cloud", "base_url": mock.url, "model": "up-model", "max_tokens": 64}}))
    env = {**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_HOME": str(home)}
    env.pop("ANTHROPIC_BASE_URL", None)
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
