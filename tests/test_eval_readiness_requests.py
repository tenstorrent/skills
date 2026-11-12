"""P2 (harness requests) and P3 (chat conformance) probes against a fake OpenAI server."""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins/tt-model-bringup"
RUNTIME = PLUGIN / "runtime"
sys.path.insert(0, str(RUNTIME))

from readiness_check import eval_request_profiles as profiles_mod  # noqa: E402
from readiness_check import run_chat_conformance as p3  # noqa: E402
from readiness_check import run_harness_requests as p2  # noqa: E402
from readiness_check import served_api  # noqa: E402

GEN_DEFAULTS = {"temperature": 0.6, "top_p": 0.95, "top_k": 20}
FAST_S, SLOW_S = 0.003, 0.02


class FakeServer:
    """Enough of vLLM's OpenAI API for the probes. ``cfg`` switches behaviours."""

    def __init__(self, **cfg):
        self.cfg = {"cap": 32, "reject_above_cap": False, "require_history_reasoning": False,
                    "leak_tags": False, "tool_args_object": False, "tokens": 6, **cfg}
        self.seen: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/v1/models":
                    return self._send(200, {"data": [{"id": "org/model", "max_model_len": 65536}]})
                if self.path == "/metrics":
                    return self._send(200, b"# none\n", "text/plain")
                return self._send(200 if self.path == "/health" else 404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append(body)
                if self.path == "/tokenize":
                    return self._send(404, {"error": {"message": "not found"}})
                code, resp = outer.respond(self.path, body)
                if code != 200:
                    return self._send(code, {"error": {"message": resp}})
                if body.get("stream"):
                    return self._stream(body, resp)
                return self._send(200, resp)

            def _stream(self, body, resp):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                delay = outer.step_delay(body)
                chat = "messages" in body
                msg = resp["choices"][0].get("message") or {}
                pieces = [("reasoning_content", msg.get("reasoning_content") or "")] if chat else []
                n = min(int(body.get("max_tokens") or outer.cfg["tokens"]), outer.cfg["tokens"])
                text = msg.get("content") if chat else resp["choices"][0]["text"]
                parts = [("content" if chat else "text", text)] if not body.get("ignore_eos") else [
                    ("content" if chat else "text", f"t{i} ") for i in range(n)]
                count = 0
                for key, val in pieces + parts:
                    if not val:
                        continue
                    time.sleep(delay)
                    count += 1
                    choice = {"index": 0, "delta": {key: val}} if chat else {"index": 0, "text": val}
                    self.wfile.write(f"data: {json.dumps({'choices': [choice]})}\n\n".encode())
                    self.wfile.flush()
                usage = {"choices": [], "usage": {"completion_tokens": count}}
                self.wfile.write(f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n".encode())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()

    def effective(self, body):
        return {**GEN_DEFAULTS, **{k: v for k, v in body.items() if k in ("temperature", "top_k", "seed")}}

    def host_route(self, body):
        eff = self.effective(body)
        return eff["temperature"] != 0 and not (1 <= eff["top_k"] <= self.cfg["cap"])

    def step_delay(self, body):
        return SLOW_S if self.host_route(body) else FAST_S

    def respond(self, path, body):
        if self.cfg["reject_above_cap"] and self.effective(body)["top_k"] > self.cfg["cap"]:
            return 400, f"top_k {body.get('top_k')} exceeds the device sampler cap"
        if path == "/v1/completions":
            return 200, {"choices": [{"index": 0, "text": "ok", "finish_reason": "length"}]}
        messages = body["messages"]
        for m in messages:
            if self.cfg["require_history_reasoning"] and m["role"] == "assistant" and "reasoning_content" not in m:
                return 400, "Assistant message is missing a thinking field."
        last = messages[-1]
        text = json.dumps(last.get("content"))
        thinking = (body.get("chat_template_kwargs") or {}).get("enable_thinking") is True
        reasoning = "Let me think. 17 + 25 = 42." if thinking else None
        msg = {"role": "assistant", "content": "ok", "reasoning_content": reasoning}
        finish = "stop"
        if body.get("tools") and last["role"] == "user" and "write_file" in text:
            content_arg = {"name": "demo", "values": [1, 2, 3]}
            args = {"path": "config.json",
                    "content": content_arg if self.cfg["tool_args_object"] else json.dumps(content_arg),
                    "options": {"mode": "w", "overwrite": True, "tags": ["a", "b"]}}
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps(args)}}]}
        elif last["role"] == "tool":
            msg["content"] = "It is 18 C and sunny in Paris."
        elif "capital" in text:
            msg["content"] = "Paris"
        elif "17 + 25" in text:
            msg["content"] = "42"
        elif "my name" in text:
            msg["content"] = "Ada"
        elif "code word" in text:
            msg["content"] = "PELICAN-4471"
        elif "Count" in text:
            msg["content"] = "1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, "
        elif "JSON" in text:
            msg["content"] = '{"city": "Paris", "country": "France"}'
        elif last["role"] == "assistant":
            msg["content"] = " vast and blue."
        if self.cfg["leak_tags"] and thinking:
            msg["content"] = "<think>17 + 25</think>42"
        return 200, {"choices": [{"index": 0, "message": msg, "finish_reason": finish}]}


@pytest.fixture
def model(tmp_path):
    model_dir = tmp_path / "models/autoports/org_model"
    (model_dir / "tt").mkdir(parents=True)
    (model_dir / "tt/generator_vllm.py").write_text('model_capabilities = {"max_device_top_k": 32}\n')
    hf = tmp_path / "hf"
    hf.mkdir()
    (hf / "generation_config.json").write_text(json.dumps({**GEN_DEFAULTS, "bos_token_id": 1}))
    (hf / "chat_template.jinja").write_text("{% if enable_thinking %}<think>{% endif %}{% if tools %}{% endif %}")
    return model_dir, hf


def result(model_dir, probe):
    body = json.loads((model_dir / "doc/eval_readiness" / f"{probe}.json").read_text())
    return {c["id"]: c for c in body["checks"]}, body


def p2_args(server, model_dir, hf, *extra):
    return ["--server-url", server.url, "--model-dir", str(model_dir), "--hf-model", str(hf),
            "--users", "1,4", "--load-users", "4", "--output-tokens", "6", *extra]


def test_profiles_file_is_valid_and_has_required_profiles():
    names = {p["name"] for p in profiles_mod.load_profiles()}
    assert {"plain", "lm_eval_reasoning", "lm_eval_non_reasoning", "model_card", "agentic"} <= names
    gpqa = profiles_mod.get_profile("lm_eval_reasoning")
    assert gpqa["request"]["top_k"] == 40 and gpqa["request"]["seed"] == 42
    assert all(p["source"] for p in profiles_mod.load_profiles())


def test_model_card_profile_reads_generation_config(model):
    _, hf = model
    card = profiles_mod.get_profile("model_card", hf_model=str(hf))
    assert card["request"] == GEN_DEFAULTS and "unresolved" not in card
    (hf / "generation_config.json").write_text(json.dumps({"bos_token_id": 0, "eos_token_id": [1]}))
    assert "no sampling defaults" in profiles_mod.get_profile("model_card", hf_model=str(hf))["unresolved"]


def test_route_prediction_mirrors_plugin_rules():
    kw = dict(max_device_top_k=32, seeded_policy="auto", device_penalties=True)
    assert p2.predict_route({"temperature": 1.0, "top_k": 40}, {}, **kw)[0] == "host"
    assert p2.predict_route({"temperature": 1.0, "top_k": 20, "seed": 1}, {}, **kw)[0] == "device"
    assert p2.predict_route({"temperature": 1.0}, {"top_k": 20}, **kw)[0] == "device"
    assert p2.predict_route({"temperature": 1.0}, {}, **kw)[0] == "host"
    assert p2.predict_route({"temperature": 0.0, "top_k": 100}, {}, **kw)[0] == "device"
    assert p2.predict_route({"temperature": 1.0, "top_k": 20, "seed": 1}, {},
                            max_device_top_k=32, seeded_policy="host", device_penalties=True)[0] == "host"
    assert p2.predict_route({"temperature": 1.0, "top_k": 20, "repetition_penalty": 1.1}, {},
                            max_device_top_k=32, seeded_policy="auto", device_penalties=False)[0] == "host"


def test_p2_fails_when_harness_top_k_exceeds_device_cap(model, capsys):
    model_dir, hf = model
    server = FakeServer(cap=32)
    try:
        code = p2.main(p2_args(server, model_dir, hf))
    finally:
        server.close()
    checks, body = result(model_dir, "harness_requests")
    assert code == 2
    assert checks["ratio_lm_eval_reasoning_4u"]["status"] == "fail"
    assert checks["ratio_lm_eval_reasoning_4u"]["severity"] == "critical"
    assert "top_k 40 > max_device_top_k 32" in checks["ratio_lm_eval_reasoning_4u"]["detail"]
    assert checks["ratio_lm_eval_reasoning_1u"]["severity"] == "advisory"
    assert checks["ratio_agentic_4u"]["status"] == "pass"
    assert checks["accepted_lm_eval_reasoning"]["status"] == "pass"
    assert body["data"]["profiles"]["lm_eval_reasoning"]["predicted_route"] == "host"
    assert len(capsys.readouterr().out.splitlines()) <= 40


def test_p2_passes_with_wider_device_top_k(model):
    model_dir, hf = model
    (model_dir / "tt/generator_vllm.py").write_text('model_capabilities = {"max_device_top_k": 64}\n')
    server = FakeServer(cap=64)
    try:
        code = p2.main(p2_args(server, model_dir, hf))
    finally:
        server.close()
    checks, body = result(model_dir, "harness_requests")
    assert code == 0, [c for c in checks.values() if c["status"] == "fail"]
    assert body["data"]["max_device_top_k"] == 64


def test_p2_refused_reasoning_profile_is_not_waivable(model):
    model_dir, hf = model
    server = FakeServer(cap=32, reject_above_cap=True)
    try:
        code = p2.main(p2_args(server, model_dir, hf, "--profiles", "lm_eval_reasoning,agentic"))
    finally:
        server.close()
    checks, _ = result(model_dir, "harness_requests")
    assert code == 2
    row = checks["accepted_lm_eval_reasoning"]
    assert row["status"] == "fail" and row["waivable"] is False and "HTTP 400" in row["detail"]
    assert checks["ratio_lm_eval_reasoning_4u"]["status"] == "skip"


def test_p2_counts_host_sampling_log_lines(model, tmp_path):
    model_dir, hf = model
    log = tmp_path / "server.log"
    log.write_text("'seeded_sampling_policy': 'host'\n")
    server = FakeServer(cap=64)
    try:
        p2.main(p2_args(server, model_dir, hf, "--profiles", "lm_eval_reasoning", "--server-log", str(log)))
    finally:
        server.close()
    _, body = result(model_dir, "harness_requests")
    assert body["data"]["seeded_sampling_policy"] == "host"
    assert body["data"]["profiles"]["lm_eval_reasoning"]["predicted_route"] == "host"


def p3_args(server, model_dir, hf, *extra):
    return ["--server-url", server.url, "--model-dir", str(model_dir), "--hf-model", str(hf), *extra]


def test_p3_passes_on_conforming_server(model):
    model_dir, hf = model
    server = FakeServer()
    try:
        code = p3.main(p3_args(server, model_dir, hf))
    finally:
        server.close()
    checks, body = result(model_dir, "chat_conformance")
    assert code == 0, [c for c in checks.values() if c["status"] == "fail"]
    assert checks["c02_thinking_split"]["status"] == "pass"
    assert checks["c11_thinking_stream"]["status"] == "pass"
    assert checks["c04_tool_call_types"]["status"] == "pass"
    assert checks["render_c01"]["status"] == "skip"
    assert body["data"]["thinks"] is True


def test_p3_history_without_reasoning_is_critical_and_waivable(model):
    model_dir, hf = model
    server = FakeServer(require_history_reasoning=True)
    try:
        code = p3.main(p3_args(server, model_dir, hf))
    finally:
        server.close()
    checks, _ = result(model_dir, "chat_conformance")
    assert code == 2
    row = checks["c06_history_without_reasoning"]
    assert row["status"] == "fail" and row["waivable"] is True and "thinking field" in row["detail"]
    assert checks["c07_history_with_reasoning"]["status"] == "pass"
    assert checks["c05_tool_result_turn"]["status"] == "pass"


def test_p3_flags_tag_leak_and_wrong_tool_argument_type(model):
    model_dir, hf = model
    server = FakeServer(leak_tags=True, tool_args_object=True)
    try:
        code = p3.main(p3_args(server, model_dir, hf))
    finally:
        server.close()
    checks, _ = result(model_dir, "chat_conformance")
    assert code == 2
    assert "thinking tags" in checks["c02_thinking_split"]["detail"]
    assert "content is dict" in checks["c04_tool_call_types"]["detail"]


def test_parse_server_settings_from_command_and_log():
    cmd = ("vllm serve m --reasoning-parser k2_horizon --enable-auto-tool-choice --tool-call-parser hermes "
           "--chat-template-content-format string")
    s = served_api.parse_server_settings(cmd)
    assert s["reasoning_parser"] == "k2_horizon" and s["tool_call_parser"] == "hermes"
    assert s["enable_auto_tool_choice"] is True and s["chat_template_content_format"] == "string"
    log = "non-default args: {'reasoning_parser': 'qwen3', 'enable_auto_tool_choice': True}"
    s = served_api.parse_server_settings(log)
    assert s["reasoning_parser"] == "qwen3" and s["enable_auto_tool_choice"] is True


class BrokenStreamServer:
    """Raw socket server: a chunked SSE stream cut mid-chunk (IncompleteRead), or a non-object SSE payload."""

    def __init__(self, mode):
        import socket
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.mode = mode
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(65536)
                head_raw, _, rest = data.partition(b"\r\n\r\n")
                length = int([h.split(b":")[1] for h in head_raw.split(b"\r\n") if h.lower().startswith(b"content-length")][0])
                while len(rest) < length:
                    rest += conn.recv(65536)
                head = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
                if self.mode == "cut":
                    line = b'data: {"choices": [{"index": 0, "delta": {"content": "a"}}]}\n\n'
                    conn.sendall(head + b"%x\r\n" % len(line) + line + b"\r\n" + b"40\r\ndata: {")
                else:
                    line = b"data: [1, 2]\n\ndata: [DONE]\n\n"
                    conn.sendall(head + b"%x\r\n" % len(line) + line + b"\r\n0\r\n\r\n")

    def close(self):
        self.sock.close()


@pytest.mark.parametrize("mode, expected", [("cut", "IncompleteRead"), ("list", "non-object SSE payload")])
def test_streams_report_incomplete_read_and_non_object_payloads(mode, expected):
    from readiness_check import served_probe as sp
    server = BrokenStreamServer(mode)
    try:
        rec = sp.stream_request(server.url + "/v1/chat/completions", {"messages": []}, timeout=10)
        out = served_api.stream_post(server.url + "/v1/chat/completions", {"messages": []}, timeout=10)
    finally:
        server.close()
    assert expected in rec["error"]
    assert expected in out["error"]


def test_p3_request_crash_writes_a_failing_result(model, monkeypatch):
    model_dir, hf = model
    server = FakeServer()

    def boom(ctx, case):
        raise ValueError("unexpected response shape")

    monkeypatch.setattr(p3, "_send", boom)
    try:
        code = p3.main(p3_args(server, model_dir, hf, "--cases", "c01"))
    finally:
        server.close()
    checks, _ = result(model_dir, "chat_conformance")
    assert code == 2
    assert "probe error: ValueError" in checks["c01_system_list_content"]["detail"]


def test_served_probe_profiles_resolve_model_card_and_respect_thinking_none(model):
    from readiness_check import served_probe as sp
    _, hf = model
    profile = sp.load_profile("model_card", hf_model=str(hf))
    assert profile["request"]["temperature"] == GEN_DEFAULTS["temperature"]
    with pytest.raises(sp.ProbeError, match="cannot be resolved"):
        sp.load_profile("model_card")
    _, body = sp.build_request("http://s", "m", sp.load_profile("plain"), content="q", max_tokens=5)
    assert "chat_template_kwargs" not in body
    _, body = sp.build_request("http://s", "m", sp.load_profile("lm_eval_reasoning"), content="q", max_tokens=5)
    assert body["chat_template_kwargs"] == {"enable_thinking": True}


def test_p2_declared_top_k_resolves_module_constant(tmp_path):
    from readiness_check import run_harness_requests as p2_mod
    tt = tmp_path / "tt"
    tt.mkdir()
    (tt / "generator.py").write_text('MAX_DEVICE_TOP_K = 64\ncaps = {"max_device_top_k": MAX_DEVICE_TOP_K}\n')
    assert p2_mod._declared_capability(tmp_path, "max_device_top_k") == "64"
    (tt / "generator.py").write_text('caps = {"max_device_top_k": 32}\n')
    assert p2_mod._declared_capability(tmp_path, "max_device_top_k") == "32"
