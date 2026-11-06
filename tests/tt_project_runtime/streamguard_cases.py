# Stream guard tests (ttp/streamguard.py: the runner checks the tool calls of a run the harness hook does
# not reach, Codex without its hook and Cursor, and stops it at one the hook refuses) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file.
from __future__ import annotations

SG_DENIED = "cat state/web.token"


def _sg_codex_line(cmd: str, typ: str = "item.started") -> str:
    return json.dumps({"type": typ, "item": {"id": "item_1", "type": "command_execution", "command": cmd,
                                              "status": "in_progress"}})


def _sg_cursor_line(name: str, args: dict, sub: str = "started", result=None) -> str:
    call = {"args": args, **({"result": result} if result is not None else {})}
    return json.dumps({"type": "tool_call", "subtype": sub, "call_id": "c1", "tool_call": {name: call},
                       "timestamp_ms": 1700000000000})


def test_codex_activity_gives_hook_shaped_payloads_with_the_shell_wrapper_unwrapped(tmp_path):
    from ttp.providers import get_provider
    out = tmp_path / "output.jsonl"
    out.write_text("\n".join([
        _sg_codex_line("/bin/bash -lc 'cat state/web.token && ls'"),
        _sg_codex_line("git status"),
        json.dumps({"type": "item.started", "item": {"id": "i2", "type": "file_change",
                                                     "changes": [{"path": "a.py"}, {"path": "b.py"}]}}),
        json.dumps({"type": "item.started", "item": {"id": "i3", "type": "mcp_tool_call", "server": "slack",
                                                     "tool": "post", "arguments": {"text": "hi"}}}),
        _sg_codex_line("/bin/bash -lc 'cat state/web.token && ls'", "item.completed"),
    ]) + "\n")
    events, _ = get_provider("codex").activity(out)
    calls = [e for e in events if e["kind"] == "call"]
    assert calls[0]["payloads"] == [{"tool_name": "Bash", "tool_input": {"command": "cat state/web.token && ls"}}]
    assert calls[1]["payloads"] == [{"tool_name": "Bash", "tool_input": {"command": "git status"}}]
    assert [p["tool_input"]["file_path"] for p in calls[2]["payloads"]] == ["a.py", "b.py"]
    assert calls[3]["payloads"] == [{"tool_name": "mcp__slack__post", "tool_input": {"text": "hi"}}]
    assert "payloads" not in events[-1] and events[-1]["kind"] == "result", "a result is never checked again"


def test_cursor_activity_reads_stream_json_tool_calls(tmp_path):
    from ttp.providers import get_provider
    out = tmp_path / "output.jsonl"
    out.write_text("\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        _sg_cursor_line("shellToolCall", {"command": "ls -la"}),
        _sg_cursor_line("shellToolCall", {"command": "ls -la"}, "completed", {"success": {"exitCode": 2}}),
        _sg_cursor_line("writeToolCall", {"path": "x.py", "fileText": "x"}),
        _sg_cursor_line("readToolCall", {"path": "y.py"}, "completed", {"success": {}}),
    ]) + "\n" + '{"type": "tool_call", "subtype": "started"')   # a line still being written
    events, offset = get_provider("cursor").activity(out)
    assert [(e["kind"], e["tool"]) for e in events] == [("call", "shell"), ("result", "shell"), ("call", "write"),
                                                        ("result", "read")]
    assert events[0]["command"] == "ls -la" and events[0]["ts"] == 1700000000.0
    assert events[0]["payloads"] == [{"tool_name": "Bash", "tool_input": {"command": "ls -la"}}]
    assert events[1]["is_error"] is True and events[3]["is_error"] is False
    assert events[2]["payloads"] == [{"tool_name": "Write", "tool_input": {"file_path": "x.py"}}]
    assert offset == len(out.read_bytes()) - len('{"type": "tool_call", "subtype": "started"'), "whole lines only"


def test_harness_hook_is_known_per_provider_and_per_codex_argv():
    from ttp.providers import get_provider
    from ttp.providers.codex import hook_config_args
    assert get_provider("claude").harness_hook([]) is True
    assert get_provider("codex").harness_hook(["codex", "exec", *hook_config_args(), "-"]) is True
    assert get_provider("codex").harness_hook(["codex", "exec", "-c", "model=x", "-"]) is False
    assert get_provider("cursor").harness_hook(["agent", "-p"]) is False


def test_refusal_asks_the_hooks_own_checks_with_the_runs_environment(tmp_path):
    from ttp import streamguard
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env = {**os.environ, "TTP_RUN_DIR": str(run_dir), "TTP_TASK": "7"}
    why = streamguard.refusal({"tool_name": "Bash", "tool_input": {"command": SG_DENIED}}, env, str(tmp_path))
    assert why and "web token" in why
    assert streamguard.refusal({"tool_name": "Bash", "tool_input": {"command": "ls"}}, env, str(tmp_path)) is None
    no_run = {k: v for k, v in env.items() if k != "TTP_RUN_DIR"}
    assert streamguard.refusal({"tool_name": "Bash", "tool_input": {"command": SG_DENIED}}, no_run,
                               str(tmp_path)) is None, "outside a run the hook allows everything"
    assert not (run_dir / "steer.hooked").exists(), "the check never marks the run as hooked"


def _sg_run(tmp_path, monkeypatch, provider, lines, extra_argv=(), hooked=False, task="7", exec_lines=()):
    """Supervise a stand-in child that prints `lines` (runs those at `exec_lines` as code instead) and
    sleeps; returns (exit.json, run dir)."""
    from ttp import runner
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.1")
    monkeypatch.setattr(runner, "KILL_AFTER_S", 2)
    child = tmp_path / "child.py"
    child.write_text("import time\n" + "".join(f"{line}\n" if i in exec_lines else f"print({line!r}, flush=True)\n"
                                                for i, line in enumerate(lines)) + "time.sleep(4)\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    if hooked:
        (run_dir / "steer.hooked").touch()
    (run_dir / "run.json").write_text(json.dumps({
        "argv": [sys.executable, str(child), *extra_argv], "cwd": str(tmp_path), "provider": provider,
        "model": "", "timeout_s": 60, "env": {"TTP_TASK": task, "TTP_RUN_DIR": str(run_dir)}}))
    runner.supervise(run_dir)
    return json.loads((run_dir / "exit.json").read_text()), run_dir


def test_a_hookless_codex_run_is_stopped_at_a_refused_command(tmp_path, monkeypatch):
    from ttp import streamguard
    ex, run_dir = _sg_run(tmp_path, monkeypatch, "codex", [_sg_codex_line("ls"),
                                                           _sg_codex_line(f"bash -lc '{SG_DENIED}'")])
    assert ex["stopped"] == "guard"
    got = streamguard.take(run_dir)
    assert got["tool"] == "Bash" and "web token" in got["why"] and "web.token" not in json.dumps(got).replace(
        "web token", ""), "the guard names the reason, not the command"


def test_a_hook_marker_the_agent_writes_mid_run_does_not_turn_the_guard_off(tmp_path, monkeypatch):
    from ttp import streamguard
    run_dir = tmp_path / "run"
    touch = json.dumps({"type": "noise"})
    ex, run_dir = _sg_run(tmp_path, monkeypatch, "cursor", [
        touch, f"__import__('pathlib').Path({str(run_dir / 'steer.hooked')!r}).touch()",
        _sg_cursor_line("shellToolCall", {"command": SG_DENIED})], exec_lines=(1,))
    assert (run_dir / "steer.hooked").exists() and ex["stopped"] == "guard" and streamguard.take(run_dir)


def test_a_cursor_run_is_stopped_at_a_refused_shell_call(tmp_path, monkeypatch):
    from ttp import streamguard
    ex, run_dir = _sg_run(tmp_path, monkeypatch, "cursor", [_sg_cursor_line("shellToolCall", {"command": SG_DENIED})])
    assert ex["stopped"] == "guard" and streamguard.take(run_dir)


def test_allowed_calls_hooked_runs_and_runs_without_a_task_are_left_alone(tmp_path, monkeypatch):
    from ttp import streamguard
    from ttp.providers.codex import hook_config_args
    cases = [
        ("allowed", "codex", [_sg_codex_line("ls")], (), False, "7"),
        ("hook in argv", "codex", [_sg_codex_line(SG_DENIED)], tuple(hook_config_args()), False, "7"),
        ("hook ran", "cursor", [_sg_cursor_line("shellToolCall", {"command": SG_DENIED})], (), True, "7"),
        ("no task", "cursor", [_sg_cursor_line("shellToolCall", {"command": SG_DENIED})], (), False, ""),
    ]
    for name, prov, lines, argv, hooked, task in cases:
        d = tmp_path / name.replace(" ", "_")
        d.mkdir()
        ex, run_dir = _sg_run(d, monkeypatch, prov, lines, argv, hooked, task)
        assert ex["stopped"] is None and ex["rc"] == 0, (name, ex)
        assert streamguard.take(run_dir) is None, name


def test_a_guard_stop_requeues_once_with_the_reason_then_blocks(env, tmp_path, monkeypatch):
    from ttp import streamguard
    from ttp.daemon import Daemon
    from ttp.prompts import worker_task
    p = make(env)
    _rs_live(monkeypatch)
    tid, rid, run_dir = _rs_running(p, tmp_path, attempts=1)
    streamguard.record(run_dir, "tt-project: the web token is the user's credential.", "Bash")
    (run_dir / "exit.json").write_text(json.dumps({"rc": 143, "stopped": "guard", "ended": time.time()}))
    Daemon(p.base).reap_runs()
    t = p.db.task(tid)
    assert t["status"] == "queued" and t["attempts"] == 1, "no attempt spent"
    ev = p.db.q("SELECT text FROM events WHERE kind='guard_stop' AND task=?", (tid,))
    assert len(ev) == 1 and "web token" in ev[0]["text"]
    prompt = worker_task(p, t, str(tmp_path), t["branch"])
    assert prompt.startswith("# STOPPED BY A HARNESS GUARD") and "web token" in prompt.split("\n\n# ")[0]
    # The next run trips it again: blocked for the coordinator, still no attempt spent.
    p.db.update_task(tid, status="running")
    run2 = tmp_path / "run2"
    run2.mkdir()
    (run2 / "output.jsonl").write_text("")
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "running", str(run2), "x"))
    streamguard.record(run2, "tt-project: the web token is the user's credential.", "Bash")
    (run2 / "exit.json").write_text(json.dumps({"rc": 143, "stopped": "guard", "ended": time.time()}))
    Daemon(p.base).reap_runs()
    t = p.db.task(tid)
    assert t["status"] == "blocked" and t["attempts"] == 1 and "twice" in t["blocked_reason"]


def test_a_run_without_a_guard_stop_gets_no_guard_block(env, tmp_path):
    from ttp.prompts import worker_task
    p = make(env)
    tid = p.db.add_task("build it", "Goal: build.", kind="code", tier="light", origin="user")
    p.db.update_task(tid, result=json.dumps({"status": "failed", "summary": "x"}))
    assert "STOPPED BY A HARNESS GUARD" not in worker_task(p, p.db.task(tid), str(tmp_path), None)
