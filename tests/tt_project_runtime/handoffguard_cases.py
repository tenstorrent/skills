# The hand-off guard in the worker hook (runtime/ttp/hook.py): result.json checked after each change,
# and a Stop hook that refuses to end a task run without a valid hand-off. Run from
# test_tt_project_runtime.py.


def _guard_run(monkeypatch, tmp_path, guard=True, task="7"):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.setenv("TTP_TASK", task)
    if guard:
        monkeypatch.setenv("TTP_HANDOFF_GUARD", "1")
    else:
        monkeypatch.delenv("TTP_HANDOFF_GUARD", raising=False)
    return run_dir


def _hook_call(monkeypatch, event, payload=None):
    from ttp import hook
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload or {})))
    monkeypatch.setattr(sys, "stdout", out)
    assert hook.main(["hook", event]) == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def _write_result(run_dir, text):
    path = run_dir / "result.json"
    path.write_text(text)
    st = path.stat()   # a rewrite within the same mtime tick still counts as a new version
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_an_invalid_result_json_write_hands_the_worker_the_parse_error_once(monkeypatch, tmp_path):
    run_dir = _guard_run(monkeypatch, tmp_path)
    write = {"tool_name": "Write", "tool_input": {"file_path": str(run_dir / "result.json")}}
    assert _hook_call(monkeypatch, "PostToolUse", write) is None, "no result.json yet: nothing to say"
    _write_result(run_dir, '{"status": "done",\n "summary": "a "quoted" word"}')
    ctx = _hook_call(monkeypatch, "PostToolUse", write)["hookSpecificOutput"]
    assert ctx["hookEventName"] == "PostToolUse"
    assert "not valid JSON" in ctx["additionalContext"] and "line 2 column" in ctx["additionalContext"]
    assert _hook_call(monkeypatch, "PostToolUse", {"tool_name": "Bash"}) is None, "said once per version"
    # Any tool that changes it is checked (a Bash heredoc, a script): the file's version, not the tool.
    _write_result(run_dir, '{"status": "finished", "summary": "x"}')
    ctx = _hook_call(monkeypatch, "PostToolUse", {"tool_name": "Bash"})["hookSpecificOutput"]
    assert "'finished' is not a hand-off state" in ctx["additionalContext"]
    _write_result(run_dir, '["done"]')
    assert "one JSON object" in _hook_call(monkeypatch, "PostToolUse", {})["hookSpecificOutput"]["additionalContext"]
    _write_result(run_dir, '{"status": "waiting", "summary": "x", "retry_when": "true"}')
    assert _hook_call(monkeypatch, "PostToolUse", write) is None, "a valid hand-off is silent"
    # A subagent's call says nothing and leaves the version for the worker's own next call.
    _write_result(run_dir, '{"status": ')
    assert _hook_call(monkeypatch, "PostToolUse", {"agent_id": "a1"}) is None
    assert "not valid JSON" in _hook_call(monkeypatch, "PostToolUse", {})["hookSpecificOutput"]["additionalContext"]


def test_the_result_check_and_a_coordinator_update_reach_the_worker_together(monkeypatch, tmp_path):
    from ttp import hook
    run_dir = _guard_run(monkeypatch, tmp_path)
    (run_dir / "steer.md").write_text("\n## Update\nuse any free board\n")
    _write_result(run_dir, "{")
    text = _hook_call(monkeypatch, "PostToolUse", {})["hookSpecificOutput"]["additionalContext"]
    assert "use any free board" in text and "not valid JSON" in text
    assert _hook_call(monkeypatch, "PostToolUse", {}) is None
    # The result check failing never costs the update.
    (run_dir / "steer.md").write_text("\n## Update\nuse any free board\n\n## Update\nstop at 5\n")
    _write_result(run_dir, '{"status": "done"}')
    monkeypatch.setattr(hook, "handoff_problem", lambda path: 1 / 0)
    assert "stop at 5" in _hook_call(monkeypatch, "PostToolUse", {})["hookSpecificOutput"]["additionalContext"]


def test_stop_with_no_or_an_invalid_hand_off_is_refused_once_with_the_reason(monkeypatch, tmp_path):
    run_dir = _guard_run(monkeypatch, tmp_path)
    out = _hook_call(monkeypatch, "Stop", {"hook_event_name": "Stop", "stop_hook_active": False})
    assert out["decision"] == "block" and "no result.json" in out["reason"] and "\n" not in out["reason"]
    assert "$TTP_RUN_DIR/result.json" in out["reason"]
    _write_result(run_dir, '{"status": "done", "summary": "x",}')
    out = _hook_call(monkeypatch, "Stop", {"stop_hook_active": False})
    assert out["decision"] == "block" and "not valid JSON" in out["reason"] and "column" in out["reason"]
    # A valid hand-off ends the run, and one more Stop with a fixed file stays silent.
    _write_result(run_dir, '{"status": "needs_review", "summary": "x"}')
    assert _hook_call(monkeypatch, "Stop", {}) is None


def test_stop_hook_active_or_a_third_stop_lets_the_run_end(monkeypatch, tmp_path):
    run_dir = _guard_run(monkeypatch, tmp_path)
    assert _hook_call(monkeypatch, "Stop", {"stop_hook_active": True}) is None
    assert _hook_call(monkeypatch, "Stop", {})["decision"] == "block"
    assert _hook_call(monkeypatch, "Stop", {})["decision"] == "block"
    assert _hook_call(monkeypatch, "Stop", {}) is None, "capped at two refusals per run: it never loops"
    assert (run_dir / "stop.blocks").read_text() == "2"
    # A count the run cannot read starts over, still capped.
    (run_dir / "stop.blocks").write_text("x")
    assert _hook_call(monkeypatch, "Stop", {})["decision"] == "block"


def test_subagent_and_coordinator_stops_are_never_refused(monkeypatch, tmp_path):
    _guard_run(monkeypatch, tmp_path)
    assert _hook_call(monkeypatch, "Stop", {"agent_id": "a1", "agent_type": "Explore"}) is None
    _guard_run(monkeypatch, tmp_path, task="")   # a coordinator turn: no task
    assert _hook_call(monkeypatch, "Stop", {}) is None
    _guard_run(monkeypatch, tmp_path, guard=False)   # a run the daemon expects no hand-off from
    assert _hook_call(monkeypatch, "Stop", {}) is None
    monkeypatch.delenv("TTP_RUN_DIR")
    monkeypatch.setenv("TTP_HANDOFF_GUARD", "1")
    assert _hook_call(monkeypatch, "Stop", {}) is None


def test_a_hand_off_guard_crash_prints_nothing(monkeypatch, tmp_path):
    from ttp import hook
    run_dir = _guard_run(monkeypatch, tmp_path)
    monkeypatch.setattr(hook, "handoff_problem", lambda path: 1 / 0)
    assert _hook_call(monkeypatch, "Stop", {}) is None
    _write_result(run_dir, "{")
    assert _hook_call(monkeypatch, "PostToolUse", {}) is None
    monkeypatch.undo()
    _guard_run(monkeypatch, tmp_path)
    (run_dir / "stop.blocks").mkdir()   # the count cannot be written: the run is not refused forever
    monkeypatch.setattr(hook, "MAX_STOP_BLOCKS", 2)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    assert hook.main(["hook", "Stop"]) == 0 and out.getvalue() == ""


def test_the_stop_hook_runs_as_claude_code_runs_it(monkeypatch, tmp_path):
    # The command hook_settings registers, run as a shell command with the payload on stdin.
    from ttp.providers.claude import hook_settings
    s = hook_settings()
    stop = s["hooks"]["Stop"][0]["hooks"][0]
    assert stop["command"].endswith("-m ttp.hook Stop") and stop["timeout"] <= 30
    run_dir = _guard_run(monkeypatch, tmp_path)
    env = {**os.environ, **s["env"]}
    r = subprocess.run(stop["command"], shell=True, input=json.dumps({"hook_event_name": "Stop"}), text=True,
                       capture_output=True, env=env, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout)["decision"] == "block"
    (run_dir / "result.json").write_text('{"status": "done", "summary": "x"}')
    r = subprocess.run(stop["command"], shell=True, input="{}", text=True, capture_output=True, env=env, timeout=60)
    assert r.returncode == 0 and r.stdout == ""


def test_the_hook_takes_the_same_hand_off_states_as_the_daemon():
    from ttp import daemon, hook
    assert set(hook.HANDOFF_STATES) == set(daemon.HANDOFF_STATES)


def test_task_runs_get_the_hand_off_guard_and_other_runs_do_not(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    from ttp.daemon import Daemon

    class Proc:
        pid = 4242
    real = subprocess.Popen
    # Only the runner is not launched; git still runs.
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    d = Daemon(p.base)
    tid = p.db.add_task("build it", "s", kind="code", tier="light", origin="user")
    cwd, _ = d._workdir_for(p.db.task(tid))

    def run(role="worker", where=cwd, task=True, **kw):
        rid = d.start_run(role, "go", "claude", "light", where, task=p.db.task(tid) if task else None,
                          read_only=role == "coordinator", **kw)
        return json.loads((p.runs / str(rid) / "run.json").read_text())["env"]

    assert run()["TTP_HANDOFF_GUARD"] == "1"
    assert run("reviewer", str(p.root))["TTP_HANDOFF_GUARD"] == "1", "a review ends with a result.json too"
    assert "TTP_HANDOFF_GUARD" not in run("coordinator", str(p.base), task=False)
    assert "TTP_HANDOFF_GUARD" not in run(handoff_guard=False), "the live check's resume asks for one word"


def test_a_branchless_code_hand_off_naming_no_reviewable_change_is_warned_with_the_fields_to_set(monkeypatch, tmp_path):
    """A code task with no branch on record (the daemon sets TTP_NO_BRANCH_ON_RECORD) that hands off with
    its branch and sha only in the summary is told which fields let the daemon queue its review; a
    `published` entry, top-level branch + head, a PR or a branch on record keep it silent."""
    run_dir = _guard_run(monkeypatch, tmp_path)
    monkeypatch.setenv("TTP_NO_BRANCH_ON_RECORD", "1")
    head = "ab" * 20

    def said(result):
        _write_result(run_dir, json.dumps(result))
        out = _hook_call(monkeypatch, "PostToolUse", {})
        return out["hookSpecificOutput"]["additionalContext"] if out else ""

    text = said({"status": "done", "summary": f"Pushed {head} on branch fix/x."})
    assert "no branch on record" in text and "`\"published\"" in text and "`\"branch\"` + `\"head\"`" in text
    assert "summary is not read" in text
    assert "7-40 digit" in said({"status": "needs_review", "summary": "s", "branch": "fix/x", "head": "nope"})
    assert "no head" in said({"status": "done", "summary": "s", "branch": "fix/x"})
    assert "`published` ignored" in said({"status": "done", "summary": "s", "published": {"branch": "fix/x"}})
    for quiet in ({"status": "done", "summary": "s", "branch": "fix/x", "head": head},
                  {"status": "done", "summary": "s", "branch": "fix/x", "head": head[:7], "repo": "acme/w"},
                  {"status": "done", "summary": "s", "published": {"repo": "acme/w", "branch": "fix/x", "head": head}},
                  {"status": "done", "summary": "s", "pr": "https://github.com/acme/w/pull/3"},
                  {"status": "waiting", "summary": "s", "retry_when": "true"},
                  {"status": "blocked", "summary": "s", "question": "q"}):
        assert said(quiet) == "", quiet
    # A task with its branch on record (no env) is never warned, whatever its summary says.
    monkeypatch.delenv("TTP_NO_BRANCH_ON_RECORD")
    assert said({"status": "done", "summary": f"Pushed {head} on branch fix/x."}) == ""


def test_only_a_branchless_code_worker_run_gets_the_no_branch_flag(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    from ttp.daemon import Daemon

    class Proc:
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    monkeypatch.setenv("TTP_NO_BRANCH_ON_RECORD", "1")   # never inherited
    d = Daemon(p.base)
    tid = p.db.add_task("build it", "s", kind="code", tier="light", origin="user")
    rid_task = p.db.add_task("look", "s", kind="research", tier="light", origin="user")

    def run(task_id, role="worker"):
        rid = d.start_run(role, "go", "claude", "light", str(p.root), task=p.db.task(task_id), read_only=False)
        return json.loads((p.runs / str(rid) / "run.json").read_text())["env"]

    assert run(tid)["TTP_NO_BRANCH_ON_RECORD"] == "1"
    assert "TTP_NO_BRANCH_ON_RECORD" not in run(rid_task)
    assert "TTP_NO_BRANCH_ON_RECORD" not in run(tid, "reviewer")
    p.db.update_task(tid, branch="ttp/t1-build-it")
    assert "TTP_NO_BRANCH_ON_RECORD" not in run(tid)
