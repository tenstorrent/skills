# The Codex worker hook beyond shell calls (runtime/ttp/providers/codex.py hook_config_args, hook.py): apply_patch
# edits, MCP connector calls and Stop, run as Codex runs the hook, with the payloads codex-cli 0.160 sent in a live
# run. Run from test_tt_project_runtime.py.


def _codex_hook_call(cmds, hook_env, cwd, event, **fields):
    """Run the hook command Codex was given for `event`, the payload on stdin; its parsed reply or None."""
    payload = {"session_id": THREAD, "turn_id": "t-1", "transcript_path": "/x/rollout.jsonl", "cwd": str(cwd),
               "hook_event_name": event, "model": "m", "permission_mode": "bypassPermissions", **fields}
    out = subprocess.run(cmds[event], shell=True, input=json.dumps(payload), capture_output=True, text=True,
                         env=hook_env, cwd=str(cwd), timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) if out.stdout.strip() else None


def _codex_hook_setup(env, monkeypatch, tmp_path):
    worker, _, repo = _codex_hooks(monkeypatch, tmp_path)
    _, argv = worker()
    p = make(env)
    run = tmp_path / "run"
    run.mkdir()
    hook_env = {**os.environ, "TTP_RUN_DIR": str(run), "TTP_PROJECT": str(p.base), "TTP_TASK": "42"}
    return argv, _codex_hook_commands(argv), hook_env, repo, p, run


def test_codex_hook_registers_edit_mcp_and_stop_events_as_claude_does(env, monkeypatch, tmp_path):
    from ttp.providers import claude as claude_provider
    argv, cmds, *_ = _codex_hook_setup(env, monkeypatch, tmp_path)
    assert set(cmds) == {"PreToolUse", "PostToolUse", "Stop"}
    assert cmds["Stop"].endswith("-m ttp.hook Stop")
    entries = {a.split("=", 1)[0][len("hooks."):]: a.split("=", 1)[1] for a in argv if a.startswith("hooks.")}
    assert "matcher=" not in entries["Stop"], "Stop takes no tool matcher"
    assert f"timeout={claude_provider.STOP_HOOK_TIMEOUT_S}}}" in entries["Stop"], "the hand-off checks get Claude's allowance"
    pre = {"matcher": json.loads(re.match(r'\[\{matcher=("(?:[^"\\]|\\.)*")', entries["PreToolUse"]).group(1))}
    # Codex reads the matcher as a regex (measured): shell, edits and MCP calls, as Claude's Bash|Edit|...|mcp__.*
    for name in ("Bash", "apply_patch", "mcp__probe__echo", "mcp__claude_ai_Slack__slack_send_message"):
        assert re.fullmatch(pre["matcher"], name), name
    for name in ("view_image", "update_plan", "web_search"):
        assert not re.fullmatch(pre["matcher"], name), name


def test_codex_apply_patch_on_the_harness_gh_is_denied_outside_a_harness_task(env, monkeypatch, tmp_path):
    _, cmds, hook_env, repo, p, _ = _codex_hook_setup(env, monkeypatch, tmp_path)
    gh = p.harness / "bin" / "gh"
    gh.parent.mkdir(parents=True, exist_ok=True)
    gh.write_text("#!/bin/sh\n")
    (gh.parent / "gh.d").mkdir(exist_ok=True)

    def patch(body, cwd=repo):
        out = _codex_hook_call(cmds, hook_env, cwd, "PreToolUse", tool_name="apply_patch",
                               tool_input={"command": f"*** Begin Patch\n{body}\n*** End Patch"}, tool_use_id="call-1")
        return (out or {}).get("hookSpecificOutput")

    denied = (f"*** Update File: {gh}\n@@\n+echo hi",                       # as codex-cli 0.160 sends it
              f"*** Add File: {gh.parent}/gh.d/10-x\n+exit 0",
              f"*** Delete File: {gh}",
              f"*** Update File: {repo}/a.txt\n*** Move to: {gh}\n@@\n-a\n+b",    # moved over the wrapper
              f"*** Add File: {repo}/ok.txt\n+ok\n*** Update File: {gh}\n@@\n+x",  # the second file of a patch
              f"  ***  update file :  {gh}  \r",                                 # loose spacing and case
              f"*** Update File: {repo}/../{os.path.relpath(gh, repo)}\n@@\n+x")  # a relative path
    for body in denied:
        got = patch(body)
        assert got and got["permissionDecision"] == "deny" and "bin/gh" in got["permissionDecisionReason"], body
    got = patch("*** Update File: gh\n@@\n+x", cwd=gh.parent)   # relative to the hook payload's cwd
    assert got and got["permissionDecision"] == "deny"
    allowed = (f"*** Add File: {repo}/notes.md\n+edit harness/bin/gh only in a harness task",   # named in content only
               f"*** Update File: {gh.parent}/ghx\n@@\n+x",
               f"*** Add File: {repo}/gh\n+x")
    for body in allowed:
        assert patch(body) is None, body
    # Claude's edit tools keep their own check; a run outside a task is left alone.
    out = _codex_hook_call(cmds, hook_env, repo, "PreToolUse", tool_name="Edit", tool_input={"file_path": str(gh)})
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    hook_env.pop("TTP_RUN_DIR")
    assert patch(f"*** Delete File: {gh}") is None


def test_codex_apply_patch_payload_shapes_are_read(monkeypatch, tmp_path):
    from ttp import hook
    base = tmp_path / "proj"
    gh = base / "harness" / "bin" / "gh"
    monkeypatch.setenv("TTP_PROJECT", str(base))
    text = f"*** Begin Patch\n*** Delete File: {gh}\n*** End Patch"
    for inp in ({"command": text}, {"input": text}, {"patch": text}, text):
        assert hook.harness_gh_patch({"tool_name": "apply_patch", "tool_input": inp, "cwd": str(tmp_path)}), inp
    for inp in ({}, None, {"command": ["*** Delete File:", str(gh)]}, "no patch"):
        assert hook.harness_gh_patch({"tool_name": "apply_patch", "tool_input": inp, "cwd": str(tmp_path)}) is None
    monkeypatch.setattr(hook, "_harness_task", lambda: True)
    assert hook.harness_gh_patch({"tool_input": {"command": text}, "cwd": str(tmp_path)}) is None, "a harness task may"


def test_codex_mcp_post_without_the_ai_tag_is_refused(env, monkeypatch, tmp_path):
    _, cmds, hook_env, repo, *_ = _codex_hook_setup(env, monkeypatch, tmp_path)
    # Codex names an MCP tool mcp__<server>__<tool> and passes its arguments as tool_input (measured).
    out = _codex_hook_call(cmds, hook_env, repo, "PreToolUse", tool_name="mcp__jira__create_issue",
                           tool_input={"fields": {"summary": "s", "description": "no tag"}}, tool_use_id="call-2")
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "AI-authorship tag" in out["hookSpecificOutput"]["permissionDecisionReason"]
    tagged = {"channel_id": "C1", "message": "Build is green\n\n[authored by AI]"}
    assert _codex_hook_call(cmds, hook_env, repo, "PreToolUse", tool_name="mcp__slack__slack_send_message",
                            tool_input=tagged, tool_use_id="call-3") is None


def test_codex_stop_without_a_hand_off_is_refused(env, monkeypatch, tmp_path):
    _, cmds, hook_env, repo, _, run = _codex_hook_setup(env, monkeypatch, tmp_path)
    hook_env["TTP_HANDOFF_GUARD"] = "1"
    hook_env.pop("TTP_HANDOFF_CHECK_SENDBACKS", None)   # no hand-off checks: the guard alone

    def stop(active=False):
        return _codex_hook_call(cmds, hook_env, repo, "Stop", stop_hook_active=active, last_assistant_message="Done.")

    got = stop()
    assert got and got["decision"] == "block" and "result.json" in got["reason"], got
    assert (run / "stop.blocks").read_text() == "1"
    assert stop(active=True) is None, "Codex's second stop after a refusal is let through"
    (run / "result.json").write_text(json.dumps({"status": "done", "summary": "s"}))
    assert stop() is None
    hook_env.pop("TTP_HANDOFF_GUARD")
    (run / "result.json").unlink()
    assert stop() is None, "only a task run the daemon guards"
