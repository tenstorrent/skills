# A bench arm's Claude models behind the run's loopback bridge (claudebridge.py): the logged-in claude CLI in
# print mode as the model of an OpenAI-tool-calling agent. A fake `claude` on PATH stands in for the CLI; no
# test reaches the network. Run from tests/test_tt_project_runtime.py (see _include_cases there).
from __future__ import annotations

_CB_BASH_TOOL = {"type": "function", "function": {
    "name": "bash", "description": "Execute a bash command",
    "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "The command"}},
                   "required": ["command"]}}}
_CB_SYS = "You are a helpful assistant that can interact with a computer.\n  Two spaces, and a trailing space \n"


def _cb_fake_claude(tmp_path, reply, cost=0.01, rc=0):
    """A `claude` that logs its argv (one JSON line), stdin and cwd, and answers with a CLI JSON result."""
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    log = tmp_path / "claude.log"
    (d / "claude").write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        "if sys.argv[1:] == ['--version']:\n    print('9.9.9 (Fake)'); sys.exit(0)\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'stdin': sys.stdin.read(), "
        "'cwd': os.getcwd(), 'env': sorted(os.environ)}) + '\\n')\n"
        f"print(json.dumps({{'type': 'system', 'subtype': 'init', 'tools': []}}))\n"
        f"print(json.dumps({{'type': 'result', 'is_error': {rc != 0!r}, 'result': {reply!r}, 'total_cost_usd': {cost}, "
        "'usage': {'input_tokens': 5, 'output_tokens': 7, 'cache_read_input_tokens': 100, "
        "'cache_creation_input_tokens': 20}}))\n"
        f"sys.exit({rc})\n")
    (d / "claude").chmod(0o755)
    return d, log


def test_claude_cli_argv_is_pinned_system_prompt_replaced_and_nothing_else_loaded():
    from ttp import claudebridge
    argv = claudebridge.cli_argv("claude-sonnet-5-5", "high", _CB_SYS)
    assert argv == ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                    "--model", "claude-sonnet-5-5", "--effort", "high",
                    "--system-prompt", _CB_SYS, "--tools", "", "--setting-sources", "", "--strict-mcp-config",
                    "--disable-slash-commands", "--no-session-persistence", "--exclude-dynamic-system-prompt-sections"]
    assert "--append-system-prompt" not in argv and "--bare" not in argv and "--api-key" not in " ".join(argv)
    assert "--effort" not in claudebridge.cli_argv("m", "", "s")


def test_claude_cli_env_drops_enclosing_session_and_login_variables_and_keeps_home():
    from ttp import claudebridge
    got = claudebridge.cli_env({"HOME": "/h", "PATH": "/p", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "x",
                                "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "u"})
    assert got["HOME"] == "/h" and got["PATH"] == "/p"
    assert not [k for k in got if k.startswith(("ANTHROPIC", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"))]
    assert got["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1" and got["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


def test_claude_cli_render_keeps_the_system_prompt_byte_for_byte_and_round_trips_tool_calls():
    from ttp import claudebridge
    msgs = [{"role": "system", "content": _CB_SYS}, {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "ok", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls \\"a b\\" && echo $X"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "a b"}]
    system, blocks = claudebridge.render(msgs, [_CB_BASH_TOOL])
    prompt = "".join(blocks)
    assert system == _CB_SYS
    assert prompt.endswith("\n\n=== assistant ===") and prompt.index("Tool `bash`") < prompt.index("=== user ===")
    assert blocks[0].startswith("You can call tools.") and len(blocks) == 4
    assert '<tool_call name="bash" id="call_1">\nls "a b" && echo $X\n</tool_call>' in prompt   # raw, not JSON-escaped
    assert "=== tool result (id call_1) ===\na b" in prompt
    text, calls = claudebridge.parse_reply('Looking.\n<tool_call name="bash">\ncat "x y" | wc -l\n</tool_call>', [_CB_BASH_TOOL])
    assert text == "Looking." and calls == [{"id": "call_1", "type": "function", "function": {
        "name": "bash", "arguments": json.dumps({"command": 'cat "x y" | wc -l'})}}]
    # a value wrapped in its parameter tag is read as meant; an unknown tool's block stays as text
    _, calls = claudebridge.parse_reply('<tool_call name="bash">\n<command>ls</command>\n</tool_call>', [_CB_BASH_TOOL])
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls"}
    for wrapped in ('<parameter name="command">pwd; ls</parameter>', '<parameter name="command">pwd; ls</parameter>\n</parameter>', '<parameter>pwd; ls</parameter>'):
        _, calls = claudebridge.parse_reply(f'<tool_call name="bash">\n{wrapped}\n</tool_call>', [_CB_BASH_TOOL])
        assert json.loads(calls[0]["function"]["arguments"]) == {"command": "pwd; ls"}
    _, calls = claudebridge.parse_reply('<tool_call name="bash">\necho "<b>x</b>"\n</tool_call>', [_CB_BASH_TOOL])
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": 'echo "<b>x</b>"'}
    text, calls = claudebridge.parse_reply('<tool_call name="other">\nx\n</tool_call>', [_CB_BASH_TOOL])
    assert calls == [] and "other" in text
    # a reply in the XML call form the model is trained on (no tool_call block) is read as its calls, in order;
    # an unknown tool stays text, and a reply that has our blocks is not read twice
    text, calls = claudebridge.parse_reply(
        'Now.\n\n<invoke name="bash">\n<parameter name="command">cd x && ls</parameter>\n</invoke>\n'
        '<invoke name="bash">\n<parameter name="command">pwd</parameter>\n</invoke>', [_CB_BASH_TOOL])
    assert text == "Now." and [json.loads(c["function"]["arguments"]) for c in calls] == [{"command": "cd x && ls"}, {"command": "pwd"}]
    assert [c["id"] for c in calls] == ["call_1", "call_2"]
    text, calls = claudebridge.parse_reply('<invoke name="other">\n<parameter name="a">b</parameter>\n</invoke>', [_CB_BASH_TOOL])
    assert calls == [] and "other" in text
    _, calls = claudebridge.parse_reply('<tool_call name="bash">\nls\n</tool_call>\n'
                                        '<invoke name="bash"><parameter name="command">pwd</parameter></invoke>', [_CB_BASH_TOOL])
    assert [json.loads(c["function"]["arguments"]) for c in calls] == [{"command": "ls"}, {"command": "pwd"}]   # the bridge keeps the first


def test_claude_cli_parse_reply_mends_a_models_slips_in_the_call_form():
    from ttp import claudebridge
    tool = {"type": "function", "function": {"name": "exec_command", "parameters": {"type": "object", "properties": {
        "cmd": {"type": "string"}, "yield_time_ms": {"type": "integer"}}}}}
    want = {"cmd": "ls\nwc", "yield_time_ms": 5}
    body = '{"cmd": "ls\nwc", "yield_time_ms": 5}'   # a raw newline inside the JSON string
    for reply in (f'<tool_call name="exec_command">\n<tool_call name="exec_command">\n{body}\n</tool_call>\n</tool_call>',
                  f'<tool_call name="exec_command">\n<tool_call_args>\n{body}\n</tool_call_args>\n</tool_call>',
                  f'<tool_call name="exec_command">\n<tool_call_args>\n{body}\n</tool_call>',
                  f'<tool_call name="exec_command">\n{body}\n</parameter>\n</tool_call>',
                  f'Doing it.\n<tool_call name="exec_command">\n{body}'):
        text, calls = claudebridge.parse_reply(reply, [tool])
        assert len(calls) == 1 and json.loads(calls[0]["function"]["arguments"]) == want and text in ("", "Doing it."), reply
    cut = '<tool_call name="exec_command">\n{"cmd": "ls", "yield'   # cut off mid-reply: stays text, no call
    text, calls = claudebridge.parse_reply(cut, [tool])
    assert calls == [] and text == cut


def test_claude_cli_render_blocks_only_grow_so_each_call_reuses_the_previous_calls_prompt_cache():
    from ttp import claudebridge
    call = lambda n: {"id": f"c{n}", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": f"x{n}"})}}
    msgs = [{"role": "system", "content": _CB_SYS}, {"role": "user", "content": "go"}]
    seen = []
    for n in range(1, 4):
        seen.append(claudebridge.render(msgs, [_CB_BASH_TOOL])[1])
        msgs += [{"role": "assistant", "content": "t", "tool_calls": [call(n), call(n + 10)]},
                 {"role": "tool", "tool_call_id": f"c{n}", "content": f"r{n}"},
                 {"role": "tool", "tool_call_id": f"c{n + 10}", "content": f"s{n}"}]
    for before, after in zip(seen, seen[1:]):
        assert after[:len(before)] == before and len(after) == len(before) + 2   # an assistant block, the results block
    assert all(b == b.rstrip() for bl in seen for b in bl)   # the CLI trims the last block: none may end in whitespace
    assert seen[0][-1].endswith("=== assistant ===") and "r1" in seen[1][-1] and "s1" in seen[1][-1]


def test_claude_cli_bridge_runs_the_cli_empty_handed_and_answers_in_chat_completions(tmp_path, monkeypatch):
    from ttp import claudebridge
    fake, log = _cb_fake_claude(tmp_path, 'On it.\n<tool_call name="bash">\nls -la\n</tool_call>')
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-never-passed")
    b = claudebridge.ClaudeCliBridge.from_settings({"model": "claude-opus-5-5", "effort": "high"}).start()
    try:
        req = urllib.request.Request(b.url + "/v1/chat/completions", data=json.dumps({
            "model": "x", "tools": [_CB_BASH_TOOL], "messages": [
                {"role": "system", "content": _CB_SYS}, {"role": "user", "content": "go"}]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {b.token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            got = json.loads(r.read())
        stats = b.stats()
        scratch = b.scratch
        cost = b.cost_so_far()
    finally:
        b.stop()
    ch = got["choices"][0]
    assert ch["finish_reason"] == "tool_calls" and ch["message"]["tool_calls"][0]["function"]["name"] == "bash"
    assert json.loads(ch["message"]["tool_calls"][0]["function"]["arguments"]) == {"command": "ls -la"}
    assert got["usage"]["prompt_tokens"] == 125 and got["usage"]["completion_tokens"] == 7
    seen = [json.loads(line) for line in log.read_text().splitlines() if line]
    assert len(seen) == 1
    assert seen[0]["argv"] == claudebridge.cli_argv("claude-opus-5-5", "high", _CB_SYS)[1:]   # the system prompt as is
    sent = json.loads(seen[0]["stdin"])
    assert sent["type"] == "user" and [b["type"] for b in sent["message"]["content"]] == ["text", "text"]
    assert "go" in sent["message"]["content"][1]["text"] and _CB_SYS not in seen[0]["stdin"]
    assert seen[0]["cwd"] == scratch and not os.path.exists(scratch)   # an empty scratch folder, removed at the end
    assert not [k for k in seen[0]["env"] if k.startswith(("ANTHROPIC", "CLAUDECODE"))]
    assert stats["claude_cli"]["version"] == "9.9.9 (Fake)" and stats["claude_cli"]["effort"] == "high"
    assert b.usage["calls"] == 1 and b.usage["cache_write_tokens"] == 20 and cost == 0.01


def test_claude_cli_bridge_reports_a_failed_call_as_an_error_status_and_counts_its_cost(tmp_path, monkeypatch):
    from ttp import claudebridge
    fake, _ = _cb_fake_claude(tmp_path, "Please run /login", rc=1, cost=0.002)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    b = claudebridge.ClaudeCliBridge.from_settings({"model": "m"}, retries=0).start()
    try:
        req = urllib.request.Request(b.url + "/v1/chat/completions", data=json.dumps({
            "messages": [{"role": "user", "content": "go"}]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {b.token}"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=30)
        assert e.value.code == 401 and b.faults.get("auth") == 1
        assert b.usage["calls"] == 0 and b.cost_so_far() == 0.002
    finally:
        b.stop()


def test_bench_claude_arm_on_mini_swe_agent_parses_prices_from_the_cli_and_enforces_its_budget():
    from ttp import benchrun
    name, arm = benchrun.parse_arm("sonnet=claude:claude-sonnet-5-5:high:mini-swe-agent")
    assert name == "sonnet" and arm == {"provider": "claude", "model": "claude-sonnet-5-5", "effort": "high",
                                        "harness": "mini-swe-agent"}
    for bad in ("x=claude:m:e:opencode", "x=cursor:m:e:mini-swe-agent"):
        with pytest.raises(benchrun.BenchError):
            benchrun.parse_arm(bad)
    out = benchrun.harness_spend(None, {"calls": 3, "input_tokens": 10, "output_tokens": 20,
                                        "cache_read_input_tokens": 300, "cache_write_tokens": 40, "cost_usd": 0.1234567})
    assert out["cost_usd"] == 0.123457 and out["cost_estimated"] is False and out["cache_write_tokens"] == 40
    assert "cost_basis" not in out and "claude CLI" in out["cost_source"]


def _cb_ask(b, tools=None):
    req = urllib.request.Request(b.url + "/v1/chat/completions", data=json.dumps({
        "model": "x", "tools": tools or [_CB_BASH_TOOL], "messages": [
            {"role": "system", "content": _CB_SYS}, {"role": "user", "content": "go"}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {b.token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def test_claude_cli_bridge_keeps_only_the_first_command_of_a_reply_and_counts_the_drop(tmp_path, monkeypatch, capsys):
    """The CLI has no tools, so it cannot stop at a command: a reply with three commands runs the first and drops two
    (as a native tool call ends the turn at the first), counted in the exit record's usage and the response."""
    from ttp import claudebridge
    reply = ('Probing.\n<tool_call name="bash">\necho one\n</tool_call>\n'
             '<tool_call name="bash">\necho two\n</tool_call>\n<tool_call name="bash">\necho three\n</tool_call>')
    fake, _ = _cb_fake_claude(tmp_path, reply)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    b = claudebridge.ClaudeCliBridge.from_settings({"model": "claude-opus-5-5", "effort": "high"}).start()
    try:
        got = _cb_ask(b)
        stats = b.stats()
    finally:
        b.stop()
    msg = got["choices"][0]["message"]
    assert [json.loads(c["function"]["arguments"]) for c in msg["tool_calls"]] == [{"command": "echo one"}]
    assert got["choices"][0]["finish_reason"] == "tool_calls" and got["ttp_dropped_tool_calls"] == 2
    assert "echo two" not in json.dumps(got) and "echo three" not in json.dumps(got)
    assert stats["usage"]["dropped_tool_calls"] == 2 and stats["usage"]["calls"] == 1
    assert "dropped 2 more" in capsys.readouterr().err


def test_claude_cli_bridge_leaves_a_one_command_or_command_free_reply_unchanged(tmp_path, monkeypatch):
    from ttp import claudebridge
    for reply, ncalls in (('<tool_call name="bash">\nls\n</tool_call>', 1), ("All done, no command.", 0)):
        fake, _ = _cb_fake_claude(tmp_path, reply)
        monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
        b = claudebridge.ClaudeCliBridge.from_settings({"model": "claude-opus-5-5"}).start()
        try:
            got = _cb_ask(b)
            stats = b.stats()
        finally:
            b.stop()
        assert len(got["choices"][0]["message"].get("tool_calls") or []) == ncalls
        assert "ttp_dropped_tool_calls" not in got and stats["usage"]["dropped_tool_calls"] == 0


def test_bench_second_reviewer_is_a_known_config_key_the_daily_bench_reads(env):
    """`ttp config bench.second_reviewer '{...}'` was refused as an unknown key although benchdaily reads it."""
    from ttp import benchdaily
    from ttp.project import DEFAULT_CONFIG, unknown_key_hint
    assert "second_reviewer" in DEFAULT_CONFIG["bench"]
    assert unknown_key_hint("bench.second_reviewer") is None and unknown_key_hint("bench.second_reviewer.model") is None
    assert unknown_key_hint("bench.second_reviwer")   # a typo is still refused
    p = make(env)
    assert benchdaily.second_reviewer(p.config()) is None   # unset: no second grader
    p.set_config("bench.second_reviewer", {"provider": "chat", "model": "some-model", "effort": "max"})
    # a grader never runs above high effort: a configured max is capped
    assert benchdaily.second_reviewer(p.config()) == {"model": "some-model", "effort": "high", "provider": "chat"}
