# The host-wide chat in-flight cap (inflight.py), its refusal backoff, the bridge's bad-output counters
# and `ttp bench toolprobe` (toolprobe.py). A mock endpoint on loopback; no test reaches the network.
# Run from tests/test_tt_project_runtime.py (see _include_cases there): helpers, fixtures and imports
# come from that file (and chatbridge_cases.py's mock_chat, _post and _CB_KEY).
from __future__ import annotations

import subprocess as _sp
import sys as _sys
import textwrap as _tw

_HOLD = _tw.dedent("""
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    from ttp import inflight
    got = [inflight.acquire(0.0) for _ in range(int(sys.argv[2]))]
    print(sum(1 for s in got if s is not None and s.index >= 0), flush=True)
    time.sleep(120)
""")


def _cap(n):
    from ttp import project
    project.set_account_setting("chat.max_inflight", n)


def _holder(n):
    """A second process holding `n` slots; returns it once it printed how many it got."""
    p = _sp.Popen([_sys.executable, "-c", _HOLD, str(RUNTIME), str(n)], stdout=_sp.PIPE, text=True,
                  env={**os.environ})
    got = int(p.stdout.readline().strip() or 0)
    return p, got


def test_inflight_no_cap_by_default_never_waits(env):
    from ttp import inflight
    assert inflight.cap() == 0 and inflight.allowed() == 0
    s = inflight.acquire(0.0)
    assert s is not None and s.index == -1 and s.fd is None
    assert inflight.refused() == 0 and not (inflight.folder() / "level.json").exists()
    s.release()


def test_inflight_cap_holds_across_two_processes_and_a_dead_holder_frees_its_slot(env):
    from ttp import inflight
    _cap(2)
    p, got = _holder(3)
    try:
        assert got == 2, "the other process gets only the cap"
        assert inflight.held() == 2
        slept = []
        assert inflight.acquire(0.5, sleep=slept.append, clock=iter([0.0, 0.1, 0.6]).__next__) is None
        assert slept, "a call with no free slot waits"
    finally:
        p.kill()
        p.wait()
    s = inflight.acquire(0.0)   # the kernel dropped the dead holder's locks
    assert s is not None and s.index in (0, 1) and inflight.held() == 1
    s2 = inflight.acquire(0.0)
    assert s2 is not None and inflight.acquire(0.0) is None
    s.release()
    s.release()   # at most once
    assert inflight.held() == 1
    s2.release()


def test_inflight_refusal_halves_the_level_then_it_recovers_by_one_per_quiet_minute(env):
    from ttp import inflight
    _cap(8)
    t = 1_000_000.0
    assert inflight.allowed(t) == 8
    assert inflight.refused(t) == 4 and inflight.allowed(t) == 4
    assert inflight.refused(t + 1) == 2 and inflight.refused(t + 2) == 1
    assert inflight.refused(t + 3) == 1, "never below one"
    assert inflight.allowed(t + 3 + 59) == 1 and inflight.allowed(t + 3 + 60) == 2
    assert inflight.allowed(t + 3 + 600) == 8, "never above the cap"
    # A lower level takes only the slots below it.
    assert inflight.refused() == 4 and inflight.refused() == 2 and inflight.refused() == 1
    a = inflight.acquire(0.0)
    assert a is not None and a.index == 0 and inflight.acquire(0.0) is None
    a.release()


def test_inflight_waits_export_for_the_exit_record():
    from ttp import inflight
    w = inflight.Waits()
    assert not w and w.export()["calls"] == 0
    for x in (0.0, 0.0, 1.5, 3.0):
        w.add(x)
    w.timeouts, w.refusals = 1, 2
    assert w.export() == {"calls": 4, "waits": 2, "waited_s": 4.5, "p90_s": 3.0, "timeouts": 1, "refusals": 2}


def test_inflight_cap_is_raised_or_removed_only_by_the_user(env, tmp_path):
    from ttp import accountset, inflight
    assert "chat.max_inflight" in accountset.KEYS
    by = {"by": "coordinator", "project": "p"}
    accountset.apply("chat.max_inflight", 8, by)   # setting one where none was set lowers it
    accountset.apply("chat.max_inflight", 6, by)   # anyone lowers it
    assert inflight.cap() == 6
    for v in (9, 0, None):
        with pytest.raises(ValueError, match="user's word"):
            accountset.apply("chat.max_inflight", v, by)
        with pytest.raises(ValueError, match="user's word"):
            accountset.queue(tmp_path, "chat.max_inflight", v, by)
    accountset.apply("chat.max_inflight", 8, {"by": "user"})
    assert inflight.cap() == 8
    # a coordinator lowers the user's value, noted as theirs (#9824); raising it stays the user's
    assert "the user's value" in accountset.apply("chat.max_inflight", 4, by) and inflight.cap() == 4
    accountset.apply("chat.max_inflight", 8, {"by": "user"})
    assert not accountset.raises("chat.max_inflight", 8, 4) and accountset.raises("chat.max_inflight", 8, 9)
    assert not accountset.raises("chat.max_inflight", None, 8), "setting one where none was lowers it"


def test_chatbridge_holds_a_slot_per_call_and_backs_off_on_a_refusal(env, mock_chat):
    from ttp import inflight
    from ttp.chatbridge import Bridge
    _cap(4)
    ok = (200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    up = mock_chat(ok, (429, {"error": {"message": "slow down"}}), ok)
    hi = {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    with Bridge(up.url, _CB_KEY, "example/model") as b:
        b._sleep, b._jitter = (lambda s: None), (lambda w: w)
        assert _post(b, hi)[0] == 200
        assert inflight.held() == 0, "the slot is freed once the answer is read"
        assert _post(b, hi)[0] == 200   # one refusal, waited out, then sent again
        st = b.stats()
    assert st["inflight"]["calls"] == 3 and st["inflight"]["refusals"] == 1
    assert inflight.allowed() == 2 and inflight.held() == 0


def test_chatbridge_answers_overloaded_when_the_cap_stays_full(env, mock_chat):
    from ttp import inflight
    from ttp.chatbridge import Bridge
    _cap(1)
    up = mock_chat()
    hold = inflight.acquire(0.0)
    try:
        with Bridge(up.url, _CB_KEY, "example/model", busy_retries=0) as b:
            b.timeout = 0.0
            code, text = _post(b, {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
            st = b.stats()
    finally:
        hold.release()
    assert code == 529 and "chat.max_inflight" in text and not up.seen, "never sent upstream"
    assert st["inflight"]["timeouts"] >= 1


def test_chatbridge_counts_tool_input_errors_once_and_junk_per_answer(mock_chat):
    from ttp.chatbridge import Bridge
    bad = "<tool_use_error>InputValidationError: Edit failed due to the following issue: The required parameter `old_string` is missing</tool_use_error>"
    turn = {"max_tokens": 5, "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {}},
                                          {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "x"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": bad, "is_error": True},
            {"type": "tool_result", "tool_use_id": "t2", "content": "exit 1: no such file", "is_error": True}]}]}
    junk = (200, {"choices": [{"message": {"content": "fine", "reasoning_content": "AMEAMEAME",
                                           "tool_calls": [{"id": "c", "type": "function", "function": {
                                               "name": "Bash", "arguments": "{\"command\": \"getById\"}"}}]},
                               "finish_reason": "tool_calls"}]})
    clean = (200, {"choices": [{"message": {"content": "( ( ( ( fine"}, "finish_reason": "stop"}]})
    stream = (200, [{"choices": [{"delta": {"content": "x �"}}]},
                    {"choices": [{"delta": {"content": "( ( ( "}}]},
                    {"choices": [{"delta": {"content": "( ( done"}, "finish_reason": "stop"}]}])
    broken = (200, {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "d", "type": "function", "function": {"name": "Bash", "arguments": "{\"command\": \"ls"}}]},
        "finish_reason": "tool_calls"}]})
    thought_only = (200, {"choices": [{"message": {"content": "", "reasoning_content": "I will run ls."},
                                       "finish_reason": "stop"}]})
    up = mock_chat(junk, junk, clean, stream, broken, thought_only)
    with Bridge(up.url, _CB_KEY, "example/model") as b:
        assert _post(b, turn)[0] == 200
        assert _post(b, turn)[0] == 200, "the same rejected call, sent again with the history"
        assert _post(b, {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})[0] == 200
        assert "bad_output" in b.stats()
        assert _post(b, {**turn, "stream": True})[0] == 200
        assert _post(b, {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})[0] == 200
        assert _post(b, {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})[0] == 200
        st, scanned = b.stats(), b.usage["scanned"]
    # t1 counted once (Bash's own failure is no input error); "getById" is no standalone ById.
    assert st["bad_output"]["tool_input_errors"] == 1
    assert st["bad_output"]["junk_responses"] == 3
    assert st["bad_output"]["junk_hits"] == {"AMEAME": 2, "U+FFFD": 1, "paren_run": 1}
    assert st["bad_output"]["args_unparsed"] == 1 and st["bad_output"]["empty_content"] == 1
    assert scanned == 6, "every answer scanned, so a clean run reads as measured"
    from ttp import chatbridge
    assert chatbridge.args_unparsed("[1]") and not chatbridge.args_unparsed("")
    assert not chatbridge.args_unparsed('{"a": 1}{"b": 2}'), "two objects back to back: the first is sent"


def test_chatbridge_emission_guard_mends_unparsed_tool_arguments_once_and_never_a_cut_string(mock_chat):
    from ttp import chatbridge
    from ttp.chatbridge import Bridge
    r = chatbridge.repair_args
    assert r('{"a": 1,}') == {"a": 1} and r('```json\n{"a": [1, 2,]}\n```') == {"a": [1, 2]}
    assert r('{"a": {"b": 1}') == {"a": {"b": 1}}, "closers cut off after a whole value"
    assert r('{"command": "rm -rf /tm') is None, "a string cut short may be a cut-off command: never mended"
    assert r('{"timeout": 12') is None and r('{"a": [1, 2') is None, "nor a number or word cut short"
    assert r('{"a": [1, 2],') == {"a": [1, 2]}, "closers after a whole value"
    assert r('{"s": "x,}"') == {"s": "x,}"} and r('{"q": "a\\"",}') == {"q": 'a"'}, "string text kept as sent"
    assert r("[1]") is None and r("") is None
    call = lambda args: (200, {"choices": [{"message": {"content": "", "tool_calls": [   # noqa: E731
        {"id": "c", "type": "function", "function": {"name": "Bash", "arguments": args}}]},
        "finish_reason": "tool_calls"}]})
    streamed = (200, [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "s", "type": "function", "function": {
        "name": "Bash", "arguments": '{"command": "ls",'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}])
    hi = {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    up = mock_chat(call('{"command": "ls",}'), call('{"command": "l'), streamed, call('{"command": "ls",}'))
    with Bridge(up.url, _CB_KEY, "example/model") as b:
        assert json.loads(_post(b, hi)[1])["content"][0]["input"] == {"command": "ls"}
        assert json.loads(_post(b, hi)[1])["content"][0]["input"] == {}, "unmendable: {} goes on, Claude Code makes the model resend"
        assert '{\\"command\\": \\"ls\\"}' in _post(b, {**hi, "stream": True})[1], "the stream's call mended too"
        bad = b.stats()["bad_output"]
    assert (bad["args_unparsed"], bad["args_repaired"]) == (3, 2)
    with Bridge(up.url, _CB_KEY, "example/model", emission_guard=False) as b:
        assert json.loads(_post(b, hi)[1])["content"][0]["input"] == {}, "the switch off: the old way"
        assert b.stats()["bad_output"]["args_repaired"] == 0
    cfg = {"providers": {"chat": {"emission_guard": False}}}
    assert chatbridge.settings(cfg, "standard", "m")["emission_guard"] is False
    assert "emission_guard" not in chatbridge.settings({"providers": {"chat": {}}}, "standard", "m")
    assert any("emission_guard" in x for x in chatbridge.config_problems({"providers": {"chat": {"emission_guard": "no"}}}))


def test_chatbridge_loop_guard_nudges_once_then_stops_a_failing_loop_for_a_bounce_and_the_switch_restores_it(mock_chat):
    import inspect
    from ttp import chatbridge, daemon, glmshare
    from ttp.chatbridge import Bridge

    def turn(n, inp=None, ok_last=False):
        msgs = [{"role": "user", "content": "go"}]
        for i in range(n):
            msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash",
                                                        "input": inp or {"command": "make"}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "exit 2",
                                                   "is_error": not (ok_last and i == n - 1)}]}]
        return {"max_tokens": 5, "messages": msgs}
    assert chatbridge.failing_streak(turn(3)) == ('Bash {"command": "make"}', 3)
    assert chatbridge.failing_streak(turn(3, ok_last=True))[1] == 0, "the last call worked"
    mixed = turn(2)
    mixed["messages"] += turn(1, {"command": "make -j2"})["messages"][1:]
    assert chatbridge.failing_streak(mixed)[1] == 1, "a changed input starts a new streak"
    ok = (200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    up = mock_chat(ok, ok, ok, ok)
    with Bridge(up.url, _CB_KEY, "example/model") as b:
        assert _post(b, turn(2))[0] == 200
        assert _post(b, turn(3))[0] == 200
        assert _post(b, turn(3))[0] == 200, "the same history sent again (a retry, a side call) is no new failure"
        code, text = _post(b, turn(4))
        st = b.stats()
    sent = [json.dumps(x) for x in up.seen]
    assert "[harness]" not in sent[0] and "failed 3 times in a row" in sent[1], "one nudge, on the third failure"
    assert "[harness]" not in sent[2], "the nudge goes once"
    assert code == 400 and "loop guard" in text and len(up.seen) == 3, "the repeat after the nudge never goes upstream"
    assert st["loop"]["nudges"] == 1 and st["loop"]["stops"] == 1 and st["loop"]["bounce"].startswith("loop:")
    with Bridge(up.url, _CB_KEY, "example/model", loop_guard=False) as b:
        assert _post(b, turn(4))[0] == 200 and "loop" not in b.stats(), "the switch off: the old way"
    assert chatbridge.settings({"providers": {"chat": {"loop_guard": False}}}, "standard", "m")["loop_guard"] is False
    src = inspect.getsource(daemon.Daemon.finish_run)
    assert '.get("loop")' in src and "glmwork.bounce_on(self.cfg)" in src, "a loop stop on a GLM route bounces to Opus"
    line = glmshare.bad_lines([{"day": "d", "runs": 1, "answers": 2, "runs_hit": 1, "loop_nudges": 1, "loop_stops": 1,
                                **{k: 0 for k in glmshare.BAD_KEYS}, "pct": {k: 0 for k in glmshare.BAD_KEYS}}])[0]
    assert "loop nudges 1, loop stops 1" in line


def test_chatbridge_junk_patterns_are_configurable_and_checked():
    from ttp import chatbridge
    assert chatbridge.re.compile(chatbridge.JUNK_PATTERNS["ById"]).search("x ById y")
    assert not chatbridge.re.compile(chatbridge.JUNK_PATTERNS["ById"]).search("findById")
    cfg = {"providers": {"chat": {"endpoint": "e", "junk_patterns": {"bad": "("},
                                  "tiers": {"standard": {"model": "m"}}}}}
    assert any("junk_patterns.bad" in p for p in chatbridge.config_problems(cfg))
    cfg["providers"]["chat"]["junk_patterns"] = {"lorem": "lorem ipsum"}
    assert chatbridge.settings(cfg, "standard", "m")["junk_patterns"] == {"lorem": "lorem ipsum"}
    with chatbridge.Bridge("http://127.0.0.1:9/v1", _CB_KEY, "m", junk_patterns={"lorem": "lorem ipsum"}) as b:
        b._scan([("content", "lorem "), ("content", "ipsum")])
        assert b.stats()["bad_output"]["junk_hits"] == {"lorem": 1}


def _probe_answer(name, args, extra=""):
    return {"choices": [{"message": {"content": extra, "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def test_toolprobe_validates_counts_refusals_and_junk_one_call_at_a_time(env):
    import urllib.error
    from ttp import inflight, toolprobe
    _cap(3)
    answers = [
        _probe_answer("read_file", {"path": "a.py", "start_line": 40, "limit": 20}),
        _probe_answer("run_command", {"command": "make test", "timeout_s": 600}),   # cwd missing
        urllib.error.HTTPError("u", 429, "busy", {}, None),
        _probe_answer("set_status", {"task": 4, "status": "broken", "notes": []}, "AMEAME"),
        {"choices": [{"message": {"content": "no tools today"}}]},
        _probe_answer("edit_file", {"path": "x", "old_text": "a", "new_text": "b", "replace_all": True, "x": 1}),
    ]
    sent, inside = [], []

    def post(body):
        sent.append(body)
        inside.append(inflight.held())
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    slept = []
    out = toolprobe.probe(5, post, "example/model", sleep=slept.append, python="")
    assert inside == [1] * 6, "one call in flight, under the cap"
    assert all(b["tool_choice"] == "auto" and b["tools"][0]["function"]["strict"] for b in sent)
    assert "Use the read_file tool" in sent[0]["messages"][-1]["content"]
    assert out["validator"] == "builtin"
    assert (out["valid"], out["invalid"], out["no_tool_call"], out["refused"]) == (1, 3, 1, 0)
    assert out["wrong_tool"] == 2, "set_status asked for edit_file, edit_file for read_file"
    assert out["junk_responses"] == 1 and out["junk_hits"] == {"AMEAME": 1}
    assert out["inflight"]["refusals"] == 1 and slept and inflight.allowed() == 1
    assert inflight.held() == 0


def test_toolprobe_gives_up_after_its_retries_and_drops_strict_once_on_a_400(env):
    import urllib.error
    from ttp import toolprobe
    calls = []

    def busy(body):
        calls.append(body)
        raise urllib.error.HTTPError("u", 503, "busy", {}, None)
    out = toolprobe.probe(1, busy, "m", sleep=lambda s: None, python="")
    assert out["refused"] == 1 and len(calls) == toolprobe.RETRIES + 1

    seen = []

    def no_strict(body):
        seen.append(body)
        if body["tools"][0]["function"].get("strict"):
            raise urllib.error.HTTPError("u", 400, "bad", {}, None)
        return _probe_answer("read_file", {"path": "a", "start_line": 1, "limit": 2})
    out = toolprobe.probe(2, no_strict, "m", sleep=lambda s: None, python="")
    assert out["valid"] == 2 and len(seen) == 3 and any("strict" in p for p in out["problems"])


def test_toolprobe_builtin_check_matches_jsonschema_where_a_venv_has_it():
    from ttp import toolprobe
    s = toolprobe.schemas()
    items = [[{"path": "a", "start_line": 0, "limit": 1}, s["read_file"]],
             [{"task": True, "status": "done", "notes": ["x"]}, s["set_status"]],
             [{"task": 2, "status": "done", "notes": ["x"]}, s["set_status"]],
             [{"path": "a", "old_text": "b", "new_text": "c"}, s["edit_file"]]]
    want = [bool(e) for e in (toolprobe.builtin_errors(a, sc) for a, sc in items)]
    assert want == [True, True, False, True]
    py = toolprobe._venv_python()
    if not py:
        pytest.skip("no virtual environment with jsonschema here")
    got, how = toolprobe.validate(items, py)
    if how != "jsonschema":
        pytest.skip("this venv has no jsonschema")
    assert [bool(e) for e in got] == want


def test_toolprobe_main_needs_an_endpoint_and_marks_itself_low_priority(env, monkeypatch):
    from ttp import toolprobe
    with pytest.raises(ValueError, match="endpoint"):
        toolprobe.main({}, 1)
    monkeypatch.delenv("TTP_CHAT_PRIORITY", raising=False)
    monkeypatch.setattr(toolprobe, "endpoint_post", lambda alias, url="": (lambda body: _probe_answer(
        "read_file", {"path": "a", "start_line": 1, "limit": 1})))
    cfg = {"providers": {"chat": {"endpoint": "e", "tiers": {"standard": {"model": "m"}}}}}
    out = toolprobe.main(cfg, 2)
    assert out["valid"] == 2 and out["model"] == "m" and os.environ.get("TTP_CHAT_PRIORITY") == "low"


def test_babysit_and_modelbench_calls_take_a_slot_and_back_off(env, monkeypatch):
    import urllib.error
    from ttp import babysit, endpoint, inflight, modelbench
    _cap(2)
    monkeypatch.setattr(endpoint, "credentials", lambda alias: ("k", "http://127.0.0.1:9/v1"))
    seen = []

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices": [{"message": {"content": "ok"}}]}'

    class _Opener:
        def __init__(self, fail=None):
            self.fail = fail

        def open(self, req, timeout=None):
            seen.append(inflight.held())
            if self.fail:
                raise urllib.error.HTTPError("u", self.fail, "busy", {}, None)
            return _R()
    monkeypatch.setattr(babysit.urllib.request, "build_opener", lambda *a: _Opener())
    assert babysit._post({"alias": "e", "base_url": "", "model": "m"}, [], 5)["choices"]
    monkeypatch.setattr(babysit.urllib.request, "build_opener", lambda *a: _Opener(529))
    with pytest.raises(babysit.Unavailable):
        babysit._post({"alias": "e", "base_url": "", "model": "m"}, [], 5)
    assert seen == [1, 1] and inflight.held() == 0 and inflight.allowed() == 1
    hold = inflight.acquire(0.0)
    try:
        monkeypatch.setattr(inflight, "acquire", lambda t, **k: None)
        with pytest.raises(babysit.Unavailable, match="in-flight"):
            babysit._post({"alias": "e", "base_url": "", "model": "m"}, [], 5)
    finally:
        hold.release()
    assert modelbench.JITTER > 0


def test_inflight_low_priority_calls_wait_while_a_normal_call_waits(env):
    from ttp import inflight
    _cap(1)
    assert inflight.low_priority({"TTP_CHAT_PRIORITY": "low"}) and not inflight.low_priority({})
    held = inflight.acquire(1, low=False)
    ticks = iter(range(100))
    seen = []
    got = inflight.acquire(2, low=False, sleep=lambda s: seen.append(inflight.waiting("normal")),
                           clock=lambda: next(ticks))
    assert got is None and seen and all(seen), "a normal call marks itself while it waits"
    assert not inflight.waiting("normal"), "and unmarks once it stops waiting"
    mark = inflight._mark_waiting(inflight.folder())   # a normal call waiting elsewhere
    held.release()
    assert inflight.acquire(0, low=True) is None, "a low call yields the free slot"
    with inflight.acquire(0, low=False) as s:
        assert s.index == 0
    inflight._unmark(mark)
    with inflight.acquire(0, low=True) as s:
        assert s.index == 0, "no normal call waits: the low call goes"
    (inflight.folder() / "wait.999999.dead").write_text("")   # a waiter that died: no flock on it
    assert not inflight.waiting("normal") and not (inflight.folder() / "wait.999999.dead").exists()


def test_inflight_a_mid_call_waits_behind_a_waiting_normal_call_and_ahead_of_a_waiting_low_call(env):
    from ttp import inflight
    _cap(1)
    assert [inflight.priority({"TTP_CHAT_PRIORITY": v}) for v in ("mid", " LOW ", "high", "")] == \
        ["mid", "low", "normal", "normal"]
    d = inflight.folder()
    d.mkdir(parents=True, exist_ok=True)
    normal = inflight._mark_waiting(d, "normal")
    assert inflight.acquire(0, cls="mid") is None, "a mid call yields to a waiting normal call"
    assert inflight.acquire(0, cls="low") is None, "and so does a low call"
    inflight._unmark(normal)
    mid = inflight._mark_waiting(d, "mid")
    assert inflight.waiting("mid") and not inflight.waiting("normal")
    assert inflight.acquire(0, cls="low") is None, "a low call yields to a waiting mid call"
    with inflight.acquire(0, cls="normal") as s:
        assert s.index == 0, "a normal call goes ahead of a waiting mid call"
    with inflight.acquire(0, cls="mid") as s:
        assert s.index == 0
    inflight._unmark(mid)
    ticks, seen = iter(range(100)), []
    held = inflight.acquire(0, cls="mid")
    assert inflight.acquire(2, cls="mid", sleep=lambda s: seen.append(inflight.waiting("mid")),
                            clock=lambda: next(ticks)) is None and seen and all(seen), "a mid call marks itself while it waits"
    assert not inflight.waiting("mid")
    held.release()
    with inflight.acquire(0, cls="low") as s:
        assert s.index == 0, "nothing waits: the low call goes"


def test_inflight_call_outcomes_are_counted_by_class_and_project_over_the_trailing_window(env):
    from ttp import inflight
    now = 100_000.0
    assert [inflight.outcome(c) for c in (None, 200, 429, 503, 529, 500, 0)] == \
        ["ok", "ok", "refusal", "refusal", "refusal", "error", "error"]
    for i, (st, cls, proj) in enumerate([(200, "normal", "a"), (429, "normal", "a"), (200, "mid", "b"),
                                         (529, "low", None), (500, "low", None)]):
        assert inflight.record_call(st, proj, cls, now=now - 60 * i)
    assert inflight.record_call(200, "a", "normal", now=now - 3600)   # outside a 30-minute window
    got = inflight.load(30, now=now)
    assert (got["calls"], got["refusals"], got["errors"], got["refusal_pct"]) == (5, 2, 1, 40.0)
    assert got["by_class"]["normal"] == {"calls": 2, "refusals": 1, "errors": 0, "refusal_pct": 50.0}
    assert got["by_class"]["low"]["refusals"] == 1 and got["by_class"]["mid"]["calls"] == 1
    assert set(got["by_project"]) == {"a", "b", ""} and got["by_project"][""]["calls"] == 2
    assert inflight.load(120, now=now)["calls"] == 6


def test_inflight_call_outcomes_older_than_two_hours_are_pruned(env):
    from ttp import inflight
    now = 100_000.0
    inflight.record("ok", "normal", "a", now=now - inflight.KEEP_S - 5)
    inflight.record("refusal", "normal", "a", now=now - 10)
    assert inflight.prune(now) == 1
    rows = inflight._rows(inflight.folder())
    assert [r["outcome"] for r in rows] == ["refusal"]
    inflight.record("ok", "normal", "a", now=now + 1)   # pruned just now: no second prune yet
    assert len(inflight._rows(inflight.folder())) == 2


def test_inflight_concurrent_outcome_records_keep_every_line(env):
    from ttp import inflight
    script = _tw.dedent("""
        import sys
        sys.path.insert(0, sys.argv[1])
        from ttp import inflight
        for _ in range(int(sys.argv[2])):
            assert inflight.record("ok", "low", "p")
    """)
    procs = [_sp.Popen([_sys.executable, "-c", script, str(RUNTIME), "60"], env={**os.environ}) for _ in range(6)]
    assert all(p.wait(60) == 0 for p in procs)
    assert inflight.load(60)["calls"] == 360


def test_chatbridge_records_each_call_outcome_with_its_class(env, mock_chat, monkeypatch):
    from ttp import inflight
    from ttp.chatbridge import Bridge
    monkeypatch.setenv("TTP_CHAT_PRIORITY", "mid")
    ok = (200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    up = mock_chat(ok, (429, {"error": {"message": "slow down"}}), ok)
    hi = {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    with Bridge(up.url, _CB_KEY, "example/model") as b:
        b._sleep, b._jitter = (lambda s: None), (lambda w: w)
        assert _post(b, hi)[0] == 200 and _post(b, hi)[0] == 200
    got = inflight.load(5)
    assert (got["calls"], got["refusals"]) == (3, 1) and got["by_class"]["mid"]["calls"] == 3


def test_chat_load_cli_prints_the_window_counts_as_json(env):
    from ttp import inflight
    inflight.record("refusal", "normal", "a")
    inflight.record("ok", "normal", "a")
    r = _sp.run([_sys.executable, "-m", "ttp", "chat-load", "--minutes", "10", "--json"], cwd=str(RUNTIME),
                env={**os.environ}, capture_output=True, text=True, timeout=60)
    got = json.loads(r.stdout)
    assert r.returncode == 0 and got["minutes"] == 10 and (got["calls"], got["refusal_pct"]) == (2, 50.0)
    assert set(got) >= {"since", "refusals", "errors", "by_class", "by_project"}
