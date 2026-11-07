# System prompt argv tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new tests of how a run's system prompt reaches the agent's command
# line (file flags, the inline fallback and its size guard, launch failures) here.
from __future__ import annotations

ARG_STRLEN = 131_072   # Linux MAX_ARG_STRLEN: one argument longer than this fails the exec (E2BIG)


def _big_text(kib=200):
    line = "Keep every change reviewed before it lands on the shared branch.\n"
    return (line * (kib * 1024 // len(line) + 1))[:kib * 1024]


def _stub_agent(tmp_path):
    """A stand-in agent binary that records how many arguments it got and exits 0."""
    stub = tmp_path / "agent-stub"
    stub.write_text("#!/bin/sh\necho \"$#\" > \"$0.args\"\nexit 0\n")
    stub.chmod(0o755)
    return stub


def _start_coordinator(env, monkeypatch, flags, system, context=None, ttl=""):
    from ttp.daemon import Daemon
    from ttp.providers import claude
    monkeypatch.setattr(claude.Claude, "binary", lambda self: "/usr/bin/true")   # never a real agent
    monkeypatch.setattr(claude, "_FLAGS", {claude.INPUT_FORMAT: True, **flags})
    p = make(env)
    from ttp import coordinator as coord
    rid = Daemon(p.base).start_run("coordinator", "EVENTS", "claude", "standard", str(p.root), read_only=True,
                                   schema=coord.ACTIONS_SCHEMA, system=system, context=context, cache_ttl=ttl)
    run = p.runs / str(rid)
    (run / "STOP").touch()
    return json.loads((run / "run.json").read_text()), run


def test_a_large_coordinator_system_prompt_goes_by_file_and_execs(env, monkeypatch, tmp_path):
    from ttp.providers import claude
    head = _big_text(200)
    spec, run = _start_coordinator(env, monkeypatch, {claude.SYSTEM_PROMPT_FILE: True}, head)
    argv = spec["argv"]
    assert argv[argv.index("--system-prompt-file") + 1] == str(run / "system.md")
    assert "--system-prompt" not in argv and (run / "system.md").read_text() == head
    assert max(len(os.fsencode(a)) for a in argv) < ARG_STRLEN
    assert "EVENTS" == _unmoded((run / "prompt.md").read_text()), "the text is in the file, not the prompt"
    # The same argv execs: no E2BIG before the agent starts.
    stub = _stub_agent(tmp_path)
    done = subprocess.run([str(stub), *argv[1:]], env={**os.environ, **spec["env"]}, timeout=30)
    assert done.returncode == 0 and int(pathlib.Path(f"{stub}.args").read_text()) == len(argv) - 1


def test_an_inline_system_prompt_past_the_argument_limit_leads_the_prompt(env, monkeypatch, tmp_path):
    from ttp.providers import base, claude
    head = _big_text(200)
    spec, run = _start_coordinator(env, monkeypatch, {claude.SYSTEM_PROMPT_FILE: False}, head)
    argv = spec["argv"]
    assert not [a for a in argv if a.startswith("--system-prompt")], "no file flag and too long: not inline"
    assert max(len(os.fsencode(a)) for a in argv) < ARG_STRLEN
    assert _unmoded((run / "prompt.md").read_text()) == head + "\n\nEVENTS", "the text leads what the agent reads"
    stub = _stub_agent(tmp_path)
    assert subprocess.run([str(stub), *argv[1:]], env={**os.environ, **spec["env"]}, timeout=30).returncode == 0
    # Text that fits one argument still goes inline on such a CLI.
    spec, run = _start_coordinator(env, monkeypatch, {claude.SYSTEM_PROMPT_FILE: False}, "SHORT-HEAD")
    assert spec["argv"][spec["argv"].index("--system-prompt") + 1] == "SHORT-HEAD"
    assert _unmoded((run / "prompt.md").read_text()) == "EVENTS"
    # The limit is the one guard: at it inline, one byte past it not.
    path = tmp_path / "edge.md"
    path.write_text("x" * base.MAX_ARG_BYTES)
    assert claude.Claude().system_prompt_args(path) == ["--system-prompt", "x" * base.MAX_ARG_BYTES]
    path.write_text("x" * (base.MAX_ARG_BYTES - 1) + "é")   # bytes, not characters, count
    assert claude.Claude().system_prompt_args(path) == []


def test_a_cached_coordinator_turn_keeps_a_too_long_head_in_its_input(env, monkeypatch):
    from ttp.providers import claude
    head = _big_text(200)
    monkeypatch.setattr(claude.Claude, "own_cache_breakpoints", 3)   # an agent that leaves one free
    spec, run = _start_coordinator(env, monkeypatch, {claude.SYSTEM_PROMPT_FILE: False}, head,
                                   context="STABLE-CONTEXT", ttl="1h")
    assert spec["stdin"] == "input.jsonl" and "--input-format" in spec["argv"]
    assert not [a for a in spec["argv"] if a.startswith("--system-prompt")]
    blocks = json.loads((run / "input.jsonl").read_text())["message"]["content"]
    assert [_unmoded(b["text"]) for b in blocks] == [head, "STABLE-CONTEXT", "EVENTS"]
    assert [("cache_control" in b) for b in blocks] == [False, True, False], "one breakpoint, after both"
    # With the file flag the head stays the system prompt and the input is unchanged.
    spec, run = _start_coordinator(env, monkeypatch, {claude.SYSTEM_PROMPT_FILE: True}, head,
                                   context="STABLE-CONTEXT", ttl="1h")
    assert "--system-prompt-file" in spec["argv"]
    blocks = json.loads((run / "input.jsonl").read_text())["message"]["content"]
    assert [_unmoded(b["text"]) for b in blocks] == ["STABLE-CONTEXT", "EVENTS"]


def _start_worker(p, monkeypatch, flags, system):
    """A worker run started with `system` appended to the agent's system prompt (never launched):
    its argv, prompt and run dir."""
    from ttp import daemon as dmod
    from ttp.providers import claude

    class Proc:
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(claude, "_FLAGS", {claude.EXCLUDE_DYNAMIC: True, claude.TOOLS: True, **flags})
    monkeypatch.setattr(claude.Claude, "binary", lambda self: "/usr/bin/true")   # never a real agent
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    tid = p.db.add_task("x", "x", kind="work", tier="light", origin="user")
    rid = dmod.Daemon(p.base).start_run("worker", "WORK", "claude", "light", str(p.root), task=p.db.task(tid),
                                        append_system=system)
    run = p.runs / str(rid)
    return json.loads((run / "run.json").read_text())["argv"], (run / "prompt.md").read_text(), run


def test_a_large_appended_system_prompt_goes_by_file_or_leads_the_prompt(env, monkeypatch, tmp_path):
    from ttp.providers import claude
    p = make(env)
    big = _big_text(200)
    stub = _stub_agent(tmp_path)
    # With the file flag: the worker's argv names the file and execs.
    argv, prompt, run = _start_worker(p, monkeypatch, {claude.APPEND_SYSTEM_FILE: True,
                                                       claude.APPEND_SYSTEM: True}, big)
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(run / "system.md")
    assert max(len(os.fsencode(a)) for a in argv) < ARG_STRLEN and prompt == "WORK"
    assert subprocess.run([str(stub), *argv[1:]], timeout=30).returncode == 0
    # Only the inline flag: too long for it, so the text leads the prompt.
    argv, prompt, run = _start_worker(p, monkeypatch, {claude.APPEND_SYSTEM_FILE: False,
                                                       claude.APPEND_SYSTEM: True}, big)
    assert not [a for a in argv if a.startswith("--append-system-prompt")]
    assert max(len(os.fsencode(a)) for a in argv) < ARG_STRLEN and prompt == big + "\n\nWORK"
    assert subprocess.run([str(stub), *argv[1:]], timeout=30).returncode == 0
    # Short text still goes inline there.
    argv, prompt, run = _start_worker(p, monkeypatch, {claude.APPEND_SYSTEM_FILE: False,
                                                       claude.APPEND_SYSTEM: True}, "SHORT")
    assert argv[argv.index("--append-system-prompt") + 1] == "SHORT" and prompt == "WORK"


def test_a_failed_agent_exec_is_a_launch_failure_not_a_lost_run(env, tmp_path, monkeypatch):
    from ttp import runner
    from ttp.daemon import Daemon
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    stub = _stub_agent(tmp_path)
    (run_dir / "run.json").write_text(json.dumps({
        "argv": [str(stub), "--system-prompt", _big_text(200)], "env": {}, "cwd": str(tmp_path),
        "timeout_s": 60, "provider": "claude"}))
    assert runner.supervise(run_dir) == 1, "the runner books the failure instead of crashing"
    info = json.loads((run_dir / "exit.json").read_text())
    assert info["launched"] is False and info["rc"] is None
    assert "Argument list too long" in info["launch_error"] and "after --system-prompt" in info["launch_error"]
    assert not (run_dir / "child.pid").exists()
    # A missing binary is a launch failure too, with its own message.
    (run_dir / "run.json").write_text(json.dumps({"argv": [str(tmp_path / "no-such-agent")], "env": {},
                                                  "cwd": str(tmp_path), "timeout_s": 60, "provider": "claude"}))
    assert runner.supervise(run_dir) == 1
    assert "No such file" in json.loads((run_dir / "exit.json").read_text())["launch_error"]
    # The daemon books it failed (never lost) with one observation naming the cause.
    p = make(env)
    d = Daemon(p.base)
    seen = []
    monkeypatch.setattr(d, "observe", lambda source, text, *a, **k: seen.append((source, text, k.get("key"))))
    rid = p.db.x("INSERT INTO runs(role,provider,model,started,status,boot_id) VALUES('coordinator','claude','',?,"
                 "'running',?)", (time.time() - 5, d.boot))
    rd = p.runs / str(rid)
    rd.mkdir(parents=True)
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(rd), rid))
    d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)), info)
    row = p.db.one("SELECT status, cost_usd, note FROM runs WHERE id=?", (rid,))
    assert row["status"] == "failed" and not row["cost_usd"]
    assert "Argument list too long" in json.loads(row["note"])["launch_error"]
    assert len(seen) == 1 and seen[0][0] == "daemon:launch" and "never started" in seen[0][1]
    assert seen[0][2] == "launch:claude:coordinator"
