# System prompt check tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new tests of whether an agent took the system prompt a run
# passed it (Provider.instructions_recorded, runner.check_instructions, the daemon's block and
# observation) here.
from __future__ import annotations

SYSTEM_TEXT = 'BINDING RESTRICTIONS\n- Push only to "the work branch"; never C:\\main.\r\n\tKeep it generic.\n'


def _rollout_lines(sid, developer=True, answered=True, version="0.160.0", text=SYSTEM_TEXT):
    lines = [{"type": "session_meta", "payload": {"id": sid, "cli_version": version}},
             {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                                   "content": [{"type": "input_text", "text": "<permissions>"}]}}]
    if developer:
        lines.append({"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": text + "\n(more of the prompt)"}]}})
    lines.append({"type": "response_item", "payload": {"type": "message", "role": "user",
                                                       "content": [{"type": "input_text", "text": "the task"}]}})
    if answered:
        lines.append({"type": "event_msg", "payload": {"type": "token_count", "info": None}})
    return "".join(json.dumps(x) + "\n" for x in lines)


def _write_rollout(home, sid, body):
    roll = home / "sessions" / "2026" / "11" / "02" / f"rollout-2026-11-02T10-00-00-{sid}.jsonl"
    roll.parent.mkdir(parents=True, exist_ok=True)
    roll.write_text(body)
    return roll


def _sys_argv():
    from ttp.providers.codex import toml_string
    return ["codex", "exec", "--json", "-c", "developer_instructions=" + toml_string(SYSTEM_TEXT), "-"]


def test_codex_finds_the_runs_developer_instructions_in_its_rollout_head(tmp_path):
    from ttp.providers import get_provider
    from ttp.providers import codex as cx
    prov = get_provider("codex")
    home, sid = tmp_path / "codex", "0199abcd-0001"
    env = {"CODEX_HOME": str(home)}
    out = tmp_path / "output.jsonl"
    argv = _sys_argv()
    look = lambda: prov.instructions_recorded(out, argv, SYSTEM_TEXT, env)
    assert look() == {"state": "no_session"}, "no thread.started yet"
    out.write_text(json.dumps({"type": "thread.started", "thread_id": sid}) + "\n")
    assert look() == {"state": "no_rollout"}
    roll = _write_rollout(home, sid, _rollout_lines(sid, developer=False, answered=False))
    assert look()["state"] == "pending", "the context may still be written: no verdict before the model answers"
    with open(roll, "a") as f:   # an item a turn may write before any model reply decides nothing
        f.write(json.dumps({"type": "response_item", "payload": {"type": "ghost_snapshot"}}) + "\n")
    assert look()["state"] == "pending"
    with open(roll, "a") as f:
        f.write(json.dumps({"type": "response_item", "payload": {"type": "reasoning"}}) + "\n")
    assert look()["state"] == "missing", "a model reply: the context is complete"
    roll.write_text(_rollout_lines(sid))
    assert look() == {"state": "recorded", "cli_version": "0.160.0"}, "TOML-escaped quotes, backslashes, CR LF"
    roll.write_text(_rollout_lines(sid, developer=False))
    assert look() == {"state": "missing", "cli_version": "0.160.0"}
    # Bypasses: another text, only a prefix of ours, our text not at the start, or as a user message.
    for text in ("Another prompt\n", SYSTEM_TEXT[:20], "Preamble. " + SYSTEM_TEXT):
        roll.write_text(_rollout_lines(sid, text=text))
        assert look()["state"] == "missing", text
    roll.write_text(_rollout_lines(sid, developer=False).replace('"the task"', json.dumps(SYSTEM_TEXT)))
    assert look()["state"] == "missing", "a user message with the text is not the developer instructions"
    # A head with no answer yet still decides at ROLLOUT_HEAD_LINES; the message past it is not looked for.
    filler = "".join(json.dumps({"type": "event_msg", "payload": {"type": "x"}}) + "\n"
                     for _ in range(cx.ROLLOUT_HEAD_LINES))
    roll.write_text(_rollout_lines(sid, developer=False, answered=False) + filler + _rollout_lines(sid))
    assert look()["state"] == "missing"
    # Only the first INSTRUCTIONS_PREFIX characters are compared; malformed lines are skipped.
    long_text = "x" * (cx.INSTRUCTIONS_PREFIX + 10)
    roll.write_text("not json\n[1]\n" + _rollout_lines(sid, text=long_text[:cx.INSTRUCTIONS_PREFIX] + "DIFFERENT"))
    assert prov.instructions_recorded(out, argv, long_text, env)["state"] == "recorded"
    # Nothing to check: no developer_instructions argument (the text led the prompt), a resumed
    # session (its rollout holds the first run's text) or no text.
    assert prov.instructions_recorded(out, ["codex", "exec", "-"], SYSTEM_TEXT, env) is None
    assert prov.instructions_recorded(out, argv[:-1] + ["resume", sid, "-"], SYSTEM_TEXT, env) is None
    assert prov.instructions_recorded(out, argv, "", env) is None
    assert get_provider("claude").instructions_recorded(out, argv, SYSTEM_TEXT, env) is None


def _runner_run(tmp_path, monkeypatch, rollout, task="7", run_s=30, until_verdict=None):
    """Supervise a stand-in Codex child that prints its thread id, writes `rollout` (None: none) and
    runs `run_s` seconds; returns exit.json. With `until_verdict` (a ROLLOUT_WAIT_S) the run's awake
    time is a test clock (FileClock) the child moves past that wait once its thread id is out; it then
    runs until the watch thread gave a verdict and looked 3 more times, so no scheduling delay decides
    the outcome. exit.json then also has "looks_after_verdict" (rollout parses after the verdict)."""
    from ttp import runner
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.1")
    monkeypatch.setattr(runner, "INSTRUCTIONS_EVERY_S", 0.1)
    monkeypatch.delenv(runner.TEST_CLOCK_ENV, raising=False)
    home, sid = tmp_path / "codex", "0199abcd-0002"
    child = tmp_path / "child.py"
    write = "" if rollout is None else (
        f"from pathlib import Path\nroll = Path({str(home)!r}) / 'sessions/2026/11/02/rollout-x-{sid}.jsonl'\n"
        f"roll.parent.mkdir(parents=True)\nroll.write_text({rollout!r})\n")
    run = f"time.sleep({run_s})\n"
    seen = {"verdict": None, "ticks": 0, "parses": 0}
    if until_verdict is not None:
        clock, done = tmp_path / "awake-clock", tmp_path / "verdict-seen"
        monkeypatch.setenv(runner.TEST_CLOCK_ENV, str(clock))
        monkeypatch.setattr(runner, "ROLLOUT_WAIT_S", until_verdict)
        # The thread id is in output.jsonl (a file) before the clock moves: the verdict sees the session.
        run = (f"from pathlib import Path\nPath({str(clock)!r}).write_text({str(until_verdict)!r})\n"
               f"end = time.time() + 30\n"
               f"while time.time() < end and not Path({str(done)!r}).exists():\n    time.sleep(0.05)\n")
        real_check, real_stop = runner.check_instructions, runner.stop_reason
        from ttp.providers import codex as cx
        real_parse = cx.Codex.instructions_recorded

        def parse(self, *a, **k):
            seen["parses"] += 1
            return real_parse(self, *a, **k)

        def check(*a, **k):
            got = real_check(*a, **k)
            if got is not None and seen["verdict"] is None:
                seen["verdict"] = seen["parses"]
            return got

        def stop_reason(rd):   # once per watch look
            if seen["verdict"] is not None:
                seen["ticks"] += 1
                if seen["ticks"] >= 3:
                    done.touch()
            return real_stop(rd)
        monkeypatch.setattr(cx.Codex, "instructions_recorded", parse)
        monkeypatch.setattr(runner, "check_instructions", check)
        monkeypatch.setattr(runner, "stop_reason", stop_reason)
    child.write_text("import json, time\n" + write +
                     f"print(json.dumps({{'type': 'thread.started', 'thread_id': {sid!r}}}), flush=True)\n" + run)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    (run_dir / "system.md").write_text(SYSTEM_TEXT)
    from ttp.providers.codex import toml_string
    (run_dir / "run.json").write_text(json.dumps({
        "argv": [sys.executable, str(child), "-c", "developer_instructions=" + toml_string(SYSTEM_TEXT), "-"],
        "cwd": str(tmp_path), "provider": "codex", "model": "m1", "timeout_s": 60,
        "env": {"CODEX_HOME": str(home), "TTP_TASK": task}}))
    started = time.time()
    runner.supervise(run_dir)
    info = json.loads((run_dir / "exit.json").read_text())
    info["took"] = time.time() - started
    if until_verdict is not None:
        info["looks_after_verdict"] = None if seen["verdict"] is None else seen["parses"] - seen["verdict"]
    return info


def test_a_codex_run_whose_rollout_has_its_developer_instructions_goes_on(tmp_path, monkeypatch):
    info = _runner_run(tmp_path, monkeypatch, _rollout_lines("0199abcd-0002"), run_s=1)
    assert info["rc"] == 0 and info["stopped"] is None and "refused" not in info, info
    assert info["instructions"] == {"state": "recorded", "cli_version": "0.160.0"}


def test_a_codex_run_whose_rollout_lacks_its_developer_instructions_is_stopped(tmp_path, monkeypatch):
    info = _runner_run(tmp_path, monkeypatch, _rollout_lines("0199abcd-0002", developer=False, version="9.9.9"))
    assert info["stopped"] == "instructions_missing" and info["took"] < 20, info
    assert info["refused"] == "instructions_missing" and "9.9.9" in info["error"] and "stopped" in info["error"]
    assert info["instructions"] == {"state": "missing", "cli_version": "9.9.9"}


def test_a_quick_codex_run_gets_its_instructions_verdict_before_the_watch_thread_starts(tmp_path, monkeypatch):
    # The child ends before the watch thread has built its provider: the end-of-run look builds its own.
    import threading
    from ttp import providers
    real = providers.get_provider

    def slow(name):
        if threading.current_thread() is not threading.main_thread():
            time.sleep(1.5)
        return real(name)
    monkeypatch.setattr(providers, "get_provider", slow)
    info = _runner_run(tmp_path, monkeypatch, _rollout_lines("0199abcd-0002", developer=False, version="9.9.9"),
                       run_s=0)
    assert info["instructions"] == {"state": "missing", "cli_version": "9.9.9"}, info
    assert info["refused"] == "instructions_missing" and "held" in info["error"], info


def test_a_coordinator_codex_run_lacking_its_instructions_is_only_reported(tmp_path, monkeypatch):
    info = _runner_run(tmp_path, monkeypatch, _rollout_lines("0199abcd-0002", developer=False), task="", run_s=1)
    assert info["stopped"] is None and "refused" not in info, info
    assert info["instructions"]["state"] == "missing", "the daemon still raises the observation"


def test_a_codex_run_whose_rollout_never_appears_is_reported_not_stopped(tmp_path, monkeypatch):
    from ttp import runner
    info = _runner_run(tmp_path, monkeypatch, None, until_verdict=5)
    assert info["rc"] == 0 and info["stopped"] is None and "refused" not in info, info
    assert info["instructions"] == {"state": "no_rollout", "seen": "no_rollout"}
    # A run that ends before the wait is up: no verdict, nothing reported.
    monkeypatch.setattr(runner, "ROLLOUT_WAIT_S", 120)
    import shutil
    shutil.rmtree(tmp_path / "run")
    info = _runner_run(tmp_path, monkeypatch, None, run_s=0.3)
    assert "instructions" not in info and info["stopped"] is None


def test_a_codex_run_whose_rollout_stays_pending_is_reported_unchecked_once(tmp_path, monkeypatch):
    # No model reply and a short head: after ROLLOUT_WAIT_S the run gets a verdict and the rollout
    # is not parsed again for the rest of the run.
    from ttp import runner
    from ttp.providers import codex as cx
    real_check = runner.check_instructions
    info = _runner_run(tmp_path, monkeypatch, _rollout_lines("0199abcd-0002", developer=False, answered=False),
                       until_verdict=5)
    assert info["rc"] == 0 and info["stopped"] is None and "refused" not in info, info
    assert info["instructions"] == {"state": "unchecked", "cli_version": "0.160.0"}
    assert info["looks_after_verdict"] == 0, "the rollout was parsed again after the verdict (3 more watch looks)"
    # Before the wait is up a pending rollout gets no verdict, and the end-of-run look gives none either.
    assert real_check(cx.Codex(), tmp_path / "run", tmp_path / "run" / "output.jsonl",
                      json.loads((tmp_path / "run" / "run.json").read_text())["argv"],
                      {"CODEX_HOME": str(tmp_path / "codex")}, 4.9) is None


def test_the_daemon_blocks_a_task_whose_codex_run_lacked_its_instructions_without_an_attempt(env, monkeypatch):
    home = env["tmp"] / "codexhome"
    monkeypatch.setenv("CODEX_HOME", str(home))
    _write_rollout(home, "th-1", _rollout_lines("th-1", developer=False, version="0.999.0"))
    done = {"status": "done", "summary": "changed the README"}
    p, run, task, argv, _ = _cli_run(env, monkeypatch, "codex", _codex_events(*_codex_turn("all done")), result=done,
                                     append_system=SYSTEM_TEXT)
    assert any(a.startswith("developer_instructions=") for a in argv), "the worker's prompt went by -c"
    exit_info = json.loads((p.runs / str(run["id"]) / "exit.json").read_text())
    assert exit_info["refused"] == "instructions_missing", exit_info
    assert task["status"] == "blocked" and task["attempts"] == 0, "the next run would not take it either"
    assert "system prompt not taken" in task["blocked_reason"] and "0.999.0" in task["blocked_reason"]
    note = json.loads(run["note"])
    assert note["refused"] == "instructions_missing" and note["instructions"] == "missing"
    from ttp import budget
    assert "not_waste" not in note and budget.wasted(run), "it ran and spent: waste the runaway guard counts"
    obs = p.db.q("SELECT * FROM events WHERE kind='observation' AND source='daemon:instructions'")
    assert len(obs) == 1 and "0.999.0" in obs[0]["text"] and "task blocked" in obs[0]["text"], obs
    # The same rollout with the run's instructions: the run's hand-off stands, nothing is raised.
    _write_rollout(home, "th-1", _rollout_lines("th-1", version="0.999.0"))
    p, run, task, argv, _ = _cli_run(env, monkeypatch, "codex", _codex_events(*_codex_turn("all done")), result=done,
                                     append_system=SYSTEM_TEXT)
    assert task["status"] == "done" and json.loads(run["note"]).get("instructions") is None
    assert "refused" not in json.loads((p.runs / str(run["id"]) / "exit.json").read_text())


def test_the_daemon_observation_for_a_missing_instructions_check_is_keyed_once_per_version():
    from ttp.daemon import instructions_observation
    a = instructions_observation("codex", "worker", 1, {"state": "missing", "cli_version": "0.170.0"})
    b = instructions_observation("codex", "reviewer", 2, {"state": "missing", "cli_version": "0.170.0"})
    assert a and b and a[1] == b[1] == "instructions:codex:missing:0.170.0" and "0.170.0" in a[0]
    gone = instructions_observation("codex", "worker", 3, {"state": "no_rollout", "seen": "no_session"})
    assert gone and gone[1] == "instructions:codex:no_rollout:unknown" and "went on" in gone[0]
    stopped = instructions_observation("codex", "worker", 5, {"state": "missing", "cli_version": "1"}, stopped=True)
    assert "stopped and its task blocked" in stopped[0], stopped
    coord = instructions_observation("codex", "coordinator", 6, {"state": "missing", "cli_version": "1"})
    assert "only reported" in coord[0] and "blocked" not in coord[0] and "was stopped" not in coord[0], coord
    late = instructions_observation("codex", "worker", 7, {"state": "unchecked", "cli_version": "0.170.0"})
    assert late and late[1] == "instructions:codex:unchecked:0.170.0" and "no model reply" in late[0]
    for quiet in ({"state": "recorded", "cli_version": "1"}, {"state": "pending"}, None, "missing", {}):
        assert instructions_observation("codex", "worker", 4, quiet) is None
