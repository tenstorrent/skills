# Runs on Claude Code's full tool set (user msg #2302): the rules once kept by withholding a tool,
# held by ttp.hook instead (harness tools, search scope, a review's writes, subagents' calls), of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases
# there): helpers, fixtures and imports come from that file. Add new full-harness tests here.
from __future__ import annotations


def _hook_reason(payload):
    from ttp import hook
    out, _ = hook.pre_tool_use(payload)
    return out and out["hookSpecificOutput"].get("permissionDecisionReason")


def _full_harness_run(env, monkeypatch, tmp_path, role="worker"):
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.setenv("TTP_ROLE", role)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TTP_PROJECT", raising=False)
    return run, home


def test_tools_that_step_outside_the_harness_are_refused_by_the_hook(env, monkeypatch, tmp_path):
    from ttp import hook
    _full_harness_run(env, monkeypatch, tmp_path)
    for tool in ("CronCreate", "CronDelete", "ScheduleWakeup", "RemoteTrigger", "EnterWorktree",
                 "ExitWorktree", "PushNotification"):
        reason = _hook_reason({"tool_name": tool, "tool_input": {}, "cwd": str(tmp_path)})
        assert reason and tool in reason, tool
    assert "retry_when" in hook.harness_tool("ScheduleWakeup") and "ttp notify" in hook.harness_tool("PushNotification")
    for tool in ("Agent", "Task", "TaskStop", "Monitor", "Skill", "ToolSearch", "TodoWrite"):
        assert _hook_reason({"tool_name": tool, "tool_input": {}, "cwd": str(tmp_path)}) is None, tool


def test_grep_and_glob_never_search_from_root_or_the_home_folder(env, monkeypatch, tmp_path):
    _, home = _full_harness_run(env, monkeypatch, tmp_path)
    proj = home / "proj"
    refused = [("Grep", {"pattern": "x", "path": "/"}), ("Grep", {"pattern": "x", "path": "~"}),
               ("Grep", {"pattern": "x", "path": str(home)}), ("Grep", {"pattern": "x", "path": str(home.parent)}),
               ("Glob", {"pattern": "/**/*.py"}), ("Glob", {"pattern": "~/**/x"}),
               ("Glob", {"pattern": "*.py", "path": "/"}), ("Grep", {"pattern": "x", "path": ".."})]
    for tool, args in refused:
        reason = _hook_reason({"tool_name": tool, "tool_input": args, "cwd": str(proj)})
        assert reason and "never / or the home folder" in reason, (tool, args)
    allowed = [("Grep", {"pattern": "x"}), ("Grep", {"pattern": "x", "path": str(proj)}),
               ("Glob", {"pattern": "**/*.py"}), ("Glob", {"pattern": f"{proj}/**/*.py"}),
               ("Grep", {"pattern": "x", "path": "/usr/share"}), ("Glob", {"pattern": "/etc/*.conf"})]
    for tool, args in allowed:
        assert _hook_reason({"tool_name": tool, "tool_input": args, "cwd": str(proj)}) is None, (tool, args)


def test_a_review_writes_only_in_its_run_folder_or_while_resolving_a_conflict(env, monkeypatch, tmp_path):
    run, home = _full_harness_run(env, monkeypatch, tmp_path, role="reviewer")
    repo = home / "proj"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for tool, args in (("Edit", {"file_path": str(repo / "a.py")}), ("Write", {"file_path": "b.py"}),
                       ("NotebookEdit", {"notebook_path": str(repo / "n.ipynb")}),
                       ("MultiEdit", {"file_path": str(repo / "c.py")})):
        reason = _hook_reason({"tool_name": tool, "tool_input": args, "cwd": str(repo)})
        assert reason and "never edits the change it judges" in reason, tool
    for path in (run / "notes.md", tempfile.gettempdir() + "/scratch.txt"):
        assert _hook_reason({"tool_name": "Write", "tool_input": {"file_path": str(path)}, "cwd": str(repo)}) is None
    (repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")   # a merge with a conflict to resolve
    assert _hook_reason({"tool_name": "Edit", "tool_input": {"file_path": str(repo / "a.py")}, "cwd": str(repo)}) is None
    # A worker writes in its worktree as before.
    (repo / ".git" / "MERGE_HEAD").unlink()
    monkeypatch.setenv("TTP_ROLE", "worker")
    assert _hook_reason({"tool_name": "Edit", "tool_input": {"file_path": str(repo / "a.py")}, "cwd": str(repo)}) is None


def test_a_subagents_tool_calls_meet_the_same_guards(env, monkeypatch, tmp_path):
    """A subagent's calls carry agent_id and agent_type (checked live with CLI 2.1.296): every guard applies."""
    _, home = _full_harness_run(env, monkeypatch, tmp_path, role="reviewer")
    sub = {"agent_id": "a2b30f4369444fbc0", "agent_type": "general-purpose", "cwd": str(home / "proj")}
    assert "as the user" in _hook_reason({**sub, "tool_name": "Bash", "tool_input": {"command": "ttp say hi"}})
    assert _hook_reason({**sub, "tool_name": "CronCreate", "tool_input": {}})
    assert _hook_reason({**sub, "tool_name": "Grep", "tool_input": {"pattern": "x", "path": "/"}})
    assert _hook_reason({**sub, "tool_name": "Edit", "tool_input": {"file_path": str(home / "proj" / "a.py")}})
    assert _hook_reason({**sub, "tool_name": "Bash", "tool_input": {"command": "git status"}}) is None
