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
    assert coord.apply(p, [{"type": "task_add", "title": title, "spec": "s", "tier": "light", "priority": 1, **task}]) == []
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
    d.cfg["budget"]["urgent_extra_slots"] = 0   # slot limits, not the urgent lane (its tasks are priority 1)
    for _ in range(core_running):
        p.db.x("INSERT INTO runs(role,provider,started,status,boot_id) VALUES('worker',?,?,'running',?)",
               (core, time.time(), d.boot))
    assert coord.apply(p, [{"type": "task_add", "title": f"{tag}{i}", "spec": f"s {tag}", "tier": "light", "priority": 1}
                           for i in range(n)]) == []
    d.dispatch()
    return started


def test_a_routed_start_takes_its_slot_under_the_chat_gate(env, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p)
    d = Daemon(p.base)
    core = d.cfg.get("core_provider", "claude")
    # One slot on each gate, four routed tasks: one runs on chat, the overflow on the core provider.
    assert sorted(_route_slots(p, d, monkeypatch, 4)) == sorted([("chat", "none"), (core, "busy")])
    assert len(p.db.q("SELECT id FROM tasks WHERE status='queued'")) == 2


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


def _route_finish(p, d, tid, exit_info, provider="chat", result=None, reply=None, earlier=None):
    """`reply`: the run's final reply text (its output.jsonl result line); `earlier`: an earlier turn's text."""
    p.db.update_task(tid, status="running")
    note = {"route": {"tier": "light", "to": "chat", "model": _RT_MODEL, "fallback": "none"}}
    rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,boot_id,note) "
                 "VALUES(?,'worker',?,?,?,'running',?,?)",
                 (tid, provider, _RT_MODEL, time.time() - 30, d.boot, json.dumps(note)))
    rd = p.runs / str(rid)
    rd.mkdir(parents=True)
    if result is not None:
        (rd / "result.json").write_text(result if isinstance(result, str) else json.dumps(result))
    if reply is not None:
        turn = lambda text: {"type": "assistant", "message": {"id": f"m{len(text)}", "content": [{"type": "text", "text": text}]}}
        lines = [turn(earlier)] if earlier is not None else []
        lines += [turn(reply), {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0,
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
    tid = p.db.add_task("a", "s", kind="work", tier="light", priority=1, budget_usd=8.0, labels=[])
    prov, tier, k = _dispatch_task(p, d, monkeypatch, tid)
    assert (prov, tier, k["timeout_s"]) == ("chat", "light", 2700.0) and k["note"][route.KEY]["timeout_s"] == 2700.0
    # Unset for the tier: the tier default (start_run's run_timeout_s), as before.
    tid = p.db.add_task("b", "s", kind="work", tier="standard", priority=1, budget_usd=8.0, labels=[])
    monkeypatch.setattr(d, "_pick_effort", lambda task, provider: None)
    prov, tier, k = _dispatch_task(p, d, monkeypatch, tid)
    assert (prov, tier) == ("chat", "standard") and k.get("timeout_s") is None and "timeout_s" not in k["note"][route.KEY]
    # A start on the core provider (a cool-down) keeps the tier default too.
    p.db.set_kv(route.COOL + "chat", {"until": time.time() + 600, "reason": "down"})
    tid = p.db.add_task("c", "s", kind="work", tier="light", priority=1, budget_usd=8.0, labels=[])
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
    assert want in web.budget_line(p.db, time.time(), d.cfg.get("core_provider", "claude"), None, 0.0, {})
    rows = route.runs(p.db, "light", time.time() - 3600)
    assert [(r["provider"], r["model"], r["fallback"], r["priced"]) for r in rows] == \
        [("chat", _RT_MODEL, "none", False), ("chat", _RT_MODEL, "rate_limit", False)]
    assert all(r["wall_s"] >= 29 for r in rows) and route.runs(p.db, "standard", time.time() - 3600) == []
    cli.main(["stats", "demo", "--runs", "--tier", "light"])
    out = capsys.readouterr().out
    assert "fallback (rate_limit)" in out and _RT_MODEL in out and "unpriced" in out


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
    assert want in web.budget_line(p.db, time.time(), d.cfg.get("core_provider", "claude"), None, 0.0, {})
    # The latest run's rejection shows; a run without fields records none.
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
