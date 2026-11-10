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
    from ttp import hook
    run, home = _full_harness_run(env, monkeypatch, tmp_path, role="reviewer")
    assert {tempfile.gettempdir(), "/tmp"} <= set(hook._scratch_roots())
    # tmp_path is itself under a temp root (TMPDIR under `ttp checks`): pin the roots so the repo is not.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(hook, "_scratch_roots", lambda: [str(run), str(scratch)])
    repo = home / "proj"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for tool, args in (("Edit", {"file_path": str(repo / "a.py")}), ("Write", {"file_path": "b.py"}),
                       ("NotebookEdit", {"notebook_path": str(repo / "n.ipynb")}),
                       ("MultiEdit", {"file_path": str(repo / "c.py")})):
        reason = _hook_reason({"tool_name": tool, "tool_input": args, "cwd": str(repo)})
        assert reason and "never edits the change it judges" in reason, tool
    for path in (run / "notes.md", scratch / "scratch.txt"):
        assert _hook_reason({"tool_name": "Write", "tool_input": {"file_path": str(path)}, "cwd": str(repo)}) is None
    (repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")   # a merge with a conflict to resolve
    assert _hook_reason({"tool_name": "Edit", "tool_input": {"file_path": str(repo / "a.py")}, "cwd": str(repo)}) is None
    # A worker writes in its worktree as before.
    (repo / ".git" / "MERGE_HEAD").unlink()
    monkeypatch.setenv("TTP_ROLE", "worker")
    assert _hook_reason({"tool_name": "Edit", "tool_input": {"file_path": str(repo / "a.py")}, "cwd": str(repo)}) is None


def test_a_subagents_tool_calls_meet_the_same_guards(env, monkeypatch, tmp_path):
    """A subagent's calls carry agent_id and agent_type (checked live with CLI 2.1.296): every guard applies."""
    from ttp import hook
    run, home = _full_harness_run(env, monkeypatch, tmp_path, role="reviewer")
    monkeypatch.setattr(hook, "_scratch_roots", lambda: [str(run)])
    sub = {"agent_id": "a2b30f4369444fbc0", "agent_type": "general-purpose", "cwd": str(home / "proj")}
    assert "as the user" in _hook_reason({**sub, "tool_name": "Bash", "tool_input": {"command": "ttp say hi"}})
    assert _hook_reason({**sub, "tool_name": "CronCreate", "tool_input": {}})
    assert _hook_reason({**sub, "tool_name": "Grep", "tool_input": {"pattern": "x", "path": "/"}})
    assert _hook_reason({**sub, "tool_name": "Edit", "tool_input": {"file_path": str(home / "proj" / "a.py")}})
    assert _hook_reason({**sub, "tool_name": "Bash", "tool_input": {"command": "git status"}}) is None


def _spawn_stream(*calls):
    """A Claude stream-json run whose assistant turns start the subagents `calls` (subagent_type, or None)."""
    use = lambda i, t: {"type": "tool_use", "id": f"s{i}", "name": "Agent",   # noqa: E731
                        "input": {"description": "look", "prompt": "x", **({"subagent_type": t} if t else {})}}
    lines = [{"type": "system", "subtype": "init", "session_id": "s", "model": "claude-opus-5-5"}]
    for i, t in enumerate(calls):
        lines.append({"type": "assistant", "message": {"id": f"m{i}", "content": [use(i, t)]}})
        lines.append({"type": "assistant", "message": {"id": f"m{i}", "content": [use(i, t)]}})   # streamed again
    # A subagent's own calls (parent_tool_use_id set) and a Bash call are not spawns.
    lines.append({"type": "assistant", "parent_tool_use_id": "s0",
                  "message": {"id": "sub", "content": [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {}}]}})
    lines.append({"type": "result", "subtype": "success", "total_cost_usd": 0.5, "result": "ok", "usage": {}})
    return lines


def test_a_runs_subagent_spawns_are_counted_once_each_by_type(env, tmp_path):
    """Item 3 part 3 (#7774): each Agent call counts once (Task on older CLIs), by subagent_type."""
    from ttp.providers import get_provider
    out = tmp_path / "claude.jsonl"
    out.write_text("".join(json.dumps(x) + "\n" for x in _spawn_stream("Explore", None, "Explore")))
    u = get_provider("claude").parse(out)
    assert u.extra["subagents_spawned"] == 3 and u.extra["tool_calls"] == 4
    assert u.extra["subagent_types"] == {"Explore": 2, "general-purpose": 1}
    out.write_text("".join(json.dumps(x) + "\n" for x in _spawn_stream()))
    u = get_provider("claude").parse(out)
    assert u.extra["subagents_spawned"] == 0 and "subagent_types" not in u.extra


def test_a_finished_run_books_its_subagent_spawns_in_its_note(env):
    from ttp.daemon import Daemon
    d = Daemon(make(env).base)
    db = d.p.db
    rid = db.x("INSERT INTO runs(role,provider,model,started,status,boot_id) VALUES('coordinator','claude','opus',?,"
               "'running',?)", (time.time() - 600, d.boot))
    run_dir = d.p.runs / str(rid)
    run_dir.mkdir(parents=True)
    db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
    (run_dir / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _spawn_stream("Explore", "Plan")))
    (run_dir / "stderr.log").write_text("")
    d.finish_run(db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                 {"rc": 0, "started": time.time() - 600, "ended": time.time(), "stopped": None, "slept_s": 0})
    note = json.loads(db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"])
    assert note["subagents_spawned"] == 2 and note["subagent_types"] == {"Explore": 1, "Plan": 1}, note


def test_ttp_stats_landing_shows_dollars_per_landed_change_and_spawns_over_24h_and_7d(env, tmp_path):
    """Item 3 part 3 (#7774): booked spend over the commits landed on the push branch (merges and version
    bumps left out), per 24 h and 7 d, with the subagents those runs started."""
    from ttp import trainstats
    p = make(env)
    repo = tmp_path / "landrepo"
    repo.mkdir()
    g = lambda *a, ts=None: subprocess.run(   # noqa: E731
        ["git", "-C", str(repo), *a], check=True, capture_output=True,
        env={**os.environ, **({"GIT_COMMITTER_DATE": f"@{int(ts)} +0000", "GIT_AUTHOR_DATE": f"@{int(ts)} +0000"}
                              if ts else {})})
    g("init", "-q", "-b", "land")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    now = time.time()
    for i, (subject, age) in enumerate((("old change", 10 * 86400), ("week change", 3 * 86400),
                                        ("day change", 3600), ("tt-project: 0.2.999 (bump)", 1800),
                                        ("another day change", 600))):
        (repo / f"f{i}").write_text(str(i))
        g("add", ".")
        g("commit", "-qm", subject, ts=now - age)
    for age, cost, note in ((7200, 4.0, {"subagents_spawned": 3}), (2 * 86400, 6.0, {"subagents_spawned": 1}),
                            (9 * 86400, 50.0, {"subagents_spawned": 9}), (1800, 2.0, "not json subagents_spawned")):
        p.db.x("INSERT INTO runs(role, started, ended, cost_usd, note) VALUES('worker',?,?,?,?)",
               (now - age, now - age + 60, cost, note if isinstance(note, str) else json.dumps(note)))
    day, week = trainstats.per_landed(p.db, repo, "refs/heads/land", now=now)
    assert (day["usd"], day["landed_commits"], day["usd_per_landed"], day["subagents_spawned"]) == (6.0, 2, 3.0, 3)
    assert (week["usd"], week["landed_commits"], week["usd_per_landed"], week["subagents_spawned"]) == (
        12.0, 3, 4.0, 4), week
    out = trainstats.text({"days": [], "per_landed": [day, week], "ref": "refs/heads/land"}, p.name)
    assert "24 h: $6.00 over 2 commits landed on refs/heads/land = $3.00 per landed change; 3 subagents" in out, out
    # No such branch: the commit count is unknown, never zero.
    (day, _) = trainstats.per_landed(p.db, repo, "refs/heads/nope", now=now)
    assert day["landed_commits"] is None and day["usd_per_landed"] is None


def test_daily_audit_shows_dollars_per_landed_change_over_24h_and_7d(env, monkeypatch):
    """#7971: the daily self-efficiency audit carries the same $ per landed change view as
    `ttp stats --landing` (trainstats.per_landed), in its lines and in `ttp audit --json`."""
    from ttp import audit, landed, trainstats
    p = make(env)
    now = time.time()
    for age, cost, spawned in ((3600, 6.0, 2), (3 * 86400, 4.0, 1)):
        p.db.x("INSERT INTO runs(role, started, ended, cost_usd, note) VALUES('worker',?,?,?,?)",
               (now - age, now - age + 60, cost, json.dumps({"subagents_spawned": spawned})))
    monkeypatch.setattr(landed, "_push_ref", lambda p, repo: "refs/heads/land")
    counts = {86400: 3, 7 * 86400: 5}
    monkeypatch.setattr(trainstats, "landed_commits", lambda repo, ref, since, until: counts[round(until - since)])
    line = next(x for x in audit.lines(p.db, now=now, p=p) if x.startswith("$ per landed change"))
    assert "24 h: $6.00 over 3 commits landed on refs/heads/land = $2.00 per landed change; 2 subagents" in line, line
    assert "7 d: $10.00 over 5 commits landed on refs/heads/land = $2.00 per landed change; 3 subagents" in line, line
    rep = audit.report(p.db, now=now, p=p)["per_landed"]
    assert rep["ref"] == "refs/heads/land" and [w["usd_per_landed"] for w in rep["windows"]] == [2.0, 2.0], rep
    # The push branch unreadable: the count is unknown, never zero, and the spend still shows.
    monkeypatch.setattr(trainstats, "landed_commits", lambda *a: None)
    line = next(x for x in audit.lines(p.db, now=now, p=p) if x.startswith("$ per landed change"))
    assert "$6.00 over ? commits" in line and "= - per landed change" in line, line
    # No project, or a view that raises (an unreadable database): no line, and the audit still runs.
    assert not any(x.startswith("$ per landed change") for x in audit.lines(p.db, now=now))
    assert audit.report(p.db, now=now)["per_landed"] == {}
    real = trainstats.per_landed
    monkeypatch.setattr(trainstats, "per_landed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("busy")))
    assert not any(x.startswith("$ per landed change") for x in audit.lines(p.db, now=now, p=p))
    monkeypatch.setattr(trainstats, "per_landed", real)
    # Nothing spent and nothing landed: no line.
    p.db.x("DELETE FROM runs")
    monkeypatch.setattr(trainstats, "landed_commits", lambda *a: 0)
    assert not any(x.startswith("$ per landed change") for x in audit.lines(p.db, now=now, p=p))


# The same rules on the other providers (user msg #1828): chat runs Claude Code itself; Codex edits
# through apply_patch (its reads go through its shell, the Bash rules); Cursor's hook translates its tools.
def test_chat_workers_and_reviews_get_claudes_full_tool_set_and_hook(env, monkeypatch):
    from ttp.providers import get_provider
    kw = dict(model="m", effort="low", cwd=".", budget_usd=1.0, schema=None, restrictions={})
    for role in ("worker", "reviewer"):
        argv, _ = get_provider("chat").build(role=role, read_only=False, web=True, **kw)
        ref, _ = get_provider("claude").build(role=role, read_only=False, web=True, **kw)
        assert "--tools" not in argv and "--disallowedTools" not in argv, role
        hooks = json.loads(argv[argv.index("--settings") + 1])["hooks"]
        assert hooks == json.loads(ref[ref.index("--settings") + 1])["hooks"], role
        assert [h["matcher"] for h in hooks["PreToolUse"]] == ["*"], role


def _patch(*heads):
    return {"command": "*** Begin Patch\n" + "".join(f"*** {h}\n+x\n" for h in heads) + "*** End Patch\n"}


def test_a_codex_reviews_apply_patch_writes_only_in_its_run_folder_or_while_resolving_a_conflict(
        env, monkeypatch, tmp_path):
    from ttp.providers import codex
    assert re.fullmatch(codex.PRE_TOOL_MATCHER, "apply_patch"), "Codex routes apply_patch through the hook"
    run, home = _full_harness_run(env, monkeypatch, tmp_path, role="reviewer")
    repo = home / "proj"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    call = lambda inp: _hook_reason({"tool_name": "apply_patch", "tool_input": inp, "cwd": str(repo)})
    for heads in (["Update File: a.py"], ["Add File: " + str(repo / "b.py")], ["Delete File: a.py"],
                  [f"Update File: {run}/n.md", "Move to: c.py"], [f"Add File: {run}/n.md", "Add File: d.py"]):
        reason = call(_patch(*heads))
        assert reason and "never edits the change it judges" in reason, heads
    assert call(_patch(f"Add File: {run}/notes.md", f"Add File: {tempfile.gettempdir()}/s.txt")) is None
    (repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    assert call(_patch("Update File: a.py")) is None
    (repo / ".git" / "MERGE_HEAD").unlink()
    monkeypatch.setenv("TTP_ROLE", "worker")
    assert call(_patch("Update File: a.py")) is None


def test_cursor_tool_calls_meet_the_search_scope_and_review_write_rules(env, monkeypatch, tmp_path):
    from ttp import cursorhook
    run, home = _full_harness_run(env, monkeypatch, tmp_path, role="reviewer")
    repo = home / "proj"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    deny = lambda tool, inp: ((cursorhook.translate("preToolUse", {"tool_name": tool, "tool_input": inp,
                                                                   "cwd": str(repo)})[0] or {}).get("permission")
                              == "deny")
    for inp in ({"pattern": "x", "file_path": "/"}, {"pattern": "x", "path": "~"}, {"pattern": "x", "file_path": ".."}):
        assert deny("Grep", inp), inp
    assert not deny("Grep", {"pattern": "x"}) and not deny("Grep", {"pattern": "x", "file_path": str(repo)})
    for tool in ("Write", "Edit", "StrReplace", "Delete"):
        assert deny(tool, {"file_path": str(repo / "a.py")}), tool
        assert not deny(tool, {"file_path": str(run / "notes.md")}), tool
    monkeypatch.setenv("TTP_ROLE", "worker")
    assert not deny("Write", {"file_path": str(repo / "a.py")})
