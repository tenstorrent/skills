# Project-local tool guard tests (localguard.py: project.json guards.local, asked through hook.py's
# PreToolUse and coordtools.py) of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations

NO_WIPE = "def refusal(cmd):\n    return 'wiping scratch is not allowed here' if 'wipe-scratch' in cmd else None\n"
NO_SECRET_EDIT = ("def refusal(cmd):\n    return None\n\n\ndef tool_refusal(tool, tool_input):\n"
                  "    return 'no edits to secret.txt' if str(tool_input.get('file_path', '')).endswith('secret.txt') else None\n")


def _guarded(env, monkeypatch, tmp_path, files: dict[str, str], listed: list[str]):
    """A project whose harness holds guard `files` {relative path: source}, `listed` in guards.local,
    and a worker run of it."""
    p = make(env)
    for rel, src in files.items():
        (p.harness / rel).parent.mkdir(parents=True, exist_ok=True)
        (p.harness / rel).write_text(src)
    p.set_config("guards.local", listed)
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.delenv("TTP_TASK", raising=False)
    return p, run


def _worker(tool, cwd, **args):
    from ttp import hook
    out, _ = hook.pre_tool_use({"tool_name": tool, "tool_input": args, "cwd": str(cwd)})
    o = (out or {}).get("hookSpecificOutput", {})
    return o.get("permissionDecisionReason", "") if o.get("permissionDecision") == "deny" else None


def _refusals(run):
    f = run / hook_refusals_file()
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def hook_refusals_file():
    from ttp import hook
    return hook.REFUSALS_FILE


def test_a_listed_local_guard_refuses_and_logs_and_an_unlisted_one_is_ignored(env, monkeypatch, tmp_path):
    _, run = _guarded(env, monkeypatch, tmp_path, {"guards/no_wipe.py": NO_WIPE, "guards/edits.py": NO_SECRET_EDIT,
                                                   "guards/unlisted.py": "def refusal(cmd):\n    return 'never'\n"},
                      ["guards/no_wipe.py", "guards/edits.py"])
    why = _worker("Bash", tmp_path, command="ls && ./wipe-scratch --all")
    assert "wiping scratch is not allowed here" in why and "no_wipe.py" in why
    assert _worker("Bash", tmp_path, command="ls -la") is None   # unlisted.py would refuse everything
    assert "no edits to secret.txt" in _worker("Edit", tmp_path, file_path=str(tmp_path / "secret.txt"),
                                               old_string="a", new_string="b")
    assert _worker("Write", tmp_path, file_path=str(tmp_path / "ok.txt"), content="x") is None
    kinds = [r["kind"] for r in _refusals(run)]
    assert kinds == ["local:no_wipe.py", "local:edits.py"]
    # The built-in guards come first: their refusal, not the local one, is what the call gets.
    assert "wiping" not in _worker("Bash", tmp_path, command="ttp say 'wipe-scratch'")


def test_a_broken_local_guard_refuses_and_names_itself(env, monkeypatch, tmp_path):
    cases = {"guards/syntax.py": "def refusal(cmd)\n", "guards/raises.py": "def refusal(cmd):\n    raise ValueError('bad rule')\n",
             "guards/norefusal.py": "X = 1\n"}
    for rel, src in [*cases.items(), ("guards/missing.py", None)]:
        files = {rel: src} if src else {}
        _, run = _guarded(env, monkeypatch, tmp_path, files, [rel])
        why = _worker("Bash", tmp_path, command="ls")
        name = rel.split("/")[1]
        assert why and name in why and "failed" in why, (rel, why)
        assert _refusals(run)[-1]["kind"] == f"local:{name}"
        # A module that does not load has no known tool check: file edits still go (to fix it).
        if rel != "guards/raises.py":
            assert _worker("Write", tmp_path, file_path=str(tmp_path / "x.txt"), content="x") is None
    # A setting that is not a list of paths refuses every shell command rather than skip the guards.
    from ttp import localguard
    assert localguard.refusal({"guards": {"local": "guards/a.py"}}, "Bash", {"command": "ls"}, tmp_path)
    assert localguard.refusal({"guards": ["x"]}, "Bash", {"command": "ls"}, tmp_path)
    assert localguard.refusal({"guards": {"local": []}}, "Bash", {"command": "ls"}, tmp_path) is None


def test_a_run_cannot_edit_or_name_a_listed_guard_outside_a_harness_task(env, monkeypatch, tmp_path):
    p, _ = _guarded(env, monkeypatch, tmp_path, {"guards/no_wipe.py": NO_WIPE}, ["guards/no_wipe.py"])
    target = p.harness / "guards" / "no_wipe.py"
    assert "harness task" in _worker("Write", tmp_path, file_path=str(target), content="def refusal(c): pass\n")
    assert "harness task" in _worker("Edit", p.harness, file_path="guards/./no_wipe.py", old_string="a", new_string="b")
    for cmd in (f"sed -i 's/wipe/x/' {target}", "rm guards/no_wipe.py", f"mv {target} /tmp/off.py"):
        assert "harness task" in _worker("Bash", p.harness, command=cmd), cmd
    t = p.db.add_task("tune the local guard", kind="harness")
    monkeypatch.setenv("TTP_TASK", str(t))
    assert _worker("Write", tmp_path, file_path=str(target), content=NO_WIPE) is None


def test_a_codex_apply_patch_cannot_touch_a_listed_guard_outside_a_harness_task(env, monkeypatch, tmp_path):
    """The same rule for a Codex apply_patch: each add, update, delete and move target it names is checked,
    whatever shape its payload takes (the patch text itself, or its command, input or patch)."""
    p, _ = _guarded(env, monkeypatch, tmp_path, {"guards/no_wipe.py": NO_WIPE}, ["guards/no_wipe.py"])
    target = p.harness / "guards" / "no_wipe.py"

    def patch(cwd, body, key="input"):
        from ttp import hook
        text = f"*** Begin Patch\n{body}\n*** End Patch"
        out, _ = hook.pre_tool_use({"tool_name": "apply_patch", "tool_input": text if key is None else {key: text},
                                    "cwd": str(cwd)})
        o = (out or {}).get("hookSpecificOutput", {})
        return o.get("permissionDecisionReason", "") if o.get("permissionDecision") == "deny" else None

    touching = [(tmp_path, f"*** Update File: {target}\n@@\n-a\n+b"),
                (p.harness, "*** Delete File: guards/./no_wipe.py"),
                (p.harness, "*** Add File: notes.txt\n+x\n*** update file:guards/no_wipe.py\n@@\n-a\n+b"),
                (tmp_path, f"*** Update File: {tmp_path / 'scratch.py'}\n*** Move to: {target}\n@@\n-a\n+b")]
    for (cwd, body), key in zip(touching, ("input", "command", None, "patch")):
        why = patch(cwd, body, key)
        assert why and "harness task" in why and "no_wipe.py" in why, (body, why)
    unlisted = (f"*** Add File: {p.harness / 'guards' / 'other.py'}\n+x\n"
                f"*** Update File: {tmp_path / 'a.py'}\n*** Move to: {tmp_path / 'b.py'}\n@@\n-a\n+b")
    assert patch(tmp_path, unlisted) is None
    t = p.db.add_task("tune the local guard", kind="harness")
    monkeypatch.setenv("TTP_TASK", str(t))
    for cwd, body in touching:
        assert patch(cwd, body) is None, body


def test_an_upgrade_leaves_local_guards_and_their_setting_alone(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import cli, service
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    (p.harness / "guards").mkdir()
    (p.harness / "guards" / "no_wipe.py").write_text(NO_WIPE)
    p.set_config("guards.local", ["guards/no_wipe.py"])
    _git_out(p.harness, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git_out(p.harness, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "local guard")
    worker = (RUNTIME.parent / "template" / "prompts" / "worker.md").read_text()
    _install_template(env, {"worker.md": worker + "\nupstream line\n"})
    cli.main(["upgrade", "demo"])
    assert "upstream line" in (p.harness / "prompts" / "worker.md").read_text()
    assert (p.harness / "guards" / "no_wipe.py").read_text() == NO_WIPE
    assert p.raw_config()["guards"]["local"] == ["guards/no_wipe.py"]


def test_a_hook_py_upgrade_conflict_names_the_local_guard_setting(env, monkeypatch, capsys):
    p = make(env)
    from ttp import cli, service
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    hook_py = p.harness / "runtime" / "ttp" / "hook.py"
    old = 'f"tt-project: {why}."'
    hook_py.write_text(hook_py.read_text().replace(old, 'f"tt-project (project): {why}."', 1))
    _git_out(p.harness, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "local hook edit")
    _install_template(env)
    up = env["home"] / "lib" / "current" / "runtime" / "ttp" / "hook.py"
    up.write_text(up.read_text().replace(old, 'f"tt-project (upstream): {why}!"', 1))
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "demo"])
    out = capsys.readouterr()
    task = p.db.one("SELECT * FROM tasks WHERE kind='harness'")
    assert "guards.local" in out.out + out.err + (task["spec"] if task else "")


def test_local_guards_bind_every_provider_path_alike(env, monkeypatch, tmp_path):
    """The Claude and Codex workers run `python -m ttp.hook PreToolUse`; a session coordinator's calls go
    through coordtools.pre_tool_use, read-only and full tools: each refuses the same call."""
    from ttp import coordtools
    p, run = _guarded(env, monkeypatch, tmp_path, {"guards/no_wipe.py": NO_WIPE, "guards/edits.py": NO_SECRET_EDIT},
                      ["guards/no_wipe.py", "guards/edits.py"])
    bash = {"tool_name": "Bash", "tool_input": {"command": "ttp status demo; wipe-scratch"}, "cwd": str(tmp_path)}
    edit = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "secret.txt"), "content": "x"},
            "cwd": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "ttp.hook", "PreToolUse"], input=json.dumps(bash), capture_output=True,
                       text=True, timeout=_load_scaled(60), env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    assert "wiping scratch" in r.stdout and '"deny"' in r.stdout
    monkeypatch.setenv(coordtools.FULL_ENV, "1")
    for payload, text in ((bash, "wiping scratch"), (edit, "secret.txt"),
                          ({"tool_name": "WebFetch", "tool_input": {"url": "https://example.com/secret.txt",
                                                                    "file_path": "secret.txt"}}, "secret.txt")):
        out, _ = coordtools.pre_tool_use(payload)
        assert text in out["hookSpecificOutput"]["permissionDecisionReason"], payload["tool_name"]
    monkeypatch.delenv(coordtools.FULL_ENV)
    ro = {**bash, "tool_input": {"command": "ttp status demo"}}
    monkeypatch.setattr(coordtools, "bash_refusal", lambda cmd: None)   # as if listed read-only
    with open(p.harness / "guards" / "no_wipe.py", "a") as f:
        f.write("\n\ndef refusal(cmd):\n    return 'no status here' if 'status' in cmd else None\n")
    out, _ = coordtools.pre_tool_use(ro)
    assert "no status here" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_doctor_reports_local_guards(env, tmp_path):
    from ttp import localguard
    p = make(env)
    (p.harness / "guards").mkdir()
    (p.harness / "guards" / "ok.py").write_text(NO_WIPE)
    (p.harness / "guards" / "bad.py").write_text("import nosuchmodule_xyz\n")
    lines = localguard.report({"guards": {"local": ["guards/ok.py", "guards/bad.py", "runtime/x.py"]}}, p.harness)
    assert lines[0].endswith("ok.py: loads") and "problem" in lines[1] and "ModuleNotFoundError" in lines[1]
    assert "problem" in lines[2]


NO_PATCH = ("def refusal(cmd):\n    return None\n\n\ndef tool_refusal(tool, tool_input):\n"
            "    text = json.dumps(tool_input)\n"
            "    return f'no {tool} on secret.txt' if 'secret.txt' in text else None\n\n\nimport json\n")


def test_local_guards_are_asked_on_the_codex_cursor_and_runner_side_paths(env, monkeypatch, tmp_path):
    """Codex's hook (apply_patch, mcp__ calls), Cursor's per-run hook (cursorhook.translate) and the
    runner-side guard of runs without a hook (streamguard) each reach the listed guards."""
    from ttp import cursorhook, streamguard
    p, run = _guarded(env, monkeypatch, tmp_path, {"guards/no_wipe.py": NO_WIPE, "guards/edits.py": NO_PATCH},
                      ["guards/no_wipe.py", "guards/edits.py"])
    patch = "*** Begin Patch\n*** Update File: secret.txt\n@@\n-a\n+b\n*** End Patch\n"
    assert "no apply_patch on secret.txt" in _worker("apply_patch", tmp_path, command=patch)
    assert "no mcp__jira__create on secret.txt" in _worker("mcp__jira__create", tmp_path, body="see secret.txt")
    assert _worker("apply_patch", tmp_path, command=patch.replace("secret.txt", "ok.txt")) is None
    # Cursor: its own tool names, translated to Claude's.
    for name, inp, text in (("Shell", {"command": "wipe-scratch"}, "wiping scratch"),
                            ("StrReplace", {"file_path": str(tmp_path / "secret.txt")}, "no Write on secret.txt"),
                            ("MCP:create", {"body": "secret.txt"}, "no mcp__cursor__create on secret.txt")):
        out, _ = cursorhook.translate("preToolUse", {"tool_name": name, "tool_input": inp, "cwd": str(tmp_path)})
        assert out and out["permission"] == "deny" and text in out["agent_message"], name
    assert cursorhook.translate("preToolUse", {"tool_name": "Shell", "tool_input": {"command": "ls"},
                                               "cwd": str(tmp_path)})[0] is None
    # The runner-side guard, in a child with the run's environment.
    child = {**os.environ, "TTP_TASK": "7"}
    assert "wiping scratch" in streamguard.refusal({"tool_name": "Bash", "tool_input": {"command": "wipe-scratch"}},
                                                    child, str(tmp_path))
    assert "no Edit on secret.txt" in streamguard.refusal(
        {"tool_name": "Edit", "tool_input": {"file_path": "secret.txt"}}, child, str(tmp_path))
    assert streamguard.refusal({"tool_name": "Bash", "tool_input": {"command": "ls"}}, child, str(tmp_path)) is None
    assert [r["kind"] for r in _refusals(run)].count("local:edits.py") == 5


def test_a_session_coordinators_file_reads_ask_the_local_guards_as_a_workers_do(env, monkeypatch, tmp_path):
    from ttp import coordtools
    _guarded(env, monkeypatch, tmp_path, {"guards/edits.py": NO_PATCH}, ["guards/edits.py"])
    monkeypatch.delenv(coordtools.DENIED_ENV, raising=False)
    assert "no Read on secret.txt" in _worker("Read", tmp_path, file_path="secret.txt")
    for full in (False, True):
        if full:
            monkeypatch.setenv(coordtools.FULL_ENV, "1")
        for tool, args in (("Read", {"file_path": "secret.txt"}), ("Grep", {"pattern": "x", "path": "secret.txt"}),
                           ("Glob", {"pattern": "secret.txt"})):
            out, _ = coordtools.pre_tool_use({"tool_name": tool, "tool_input": args, "cwd": str(tmp_path)})
            assert f"no {tool} on secret.txt" in out["hookSpecificOutput"]["permissionDecisionReason"], (full, tool)
            out, _ = coordtools.pre_tool_use({"tool_name": tool, "tool_input": {"file_path": "ok.txt", "pattern": "x"},
                                              "cwd": str(tmp_path)})
            assert out is None, (full, tool)


def test_a_full_tools_coordinator_edits_a_listed_guard_only_through_a_harness_task(env, monkeypatch, tmp_path):
    from ttp import coordtools
    p, _ = _guarded(env, monkeypatch, tmp_path, {"guards/no_wipe.py": NO_WIPE}, ["guards/no_wipe.py"])
    monkeypatch.setenv(coordtools.FULL_ENV, "1")
    target = p.harness / "guards" / "no_wipe.py"
    for tool, args in (("Write", {"file_path": str(target), "content": "def refusal(c): pass\n"}),
                       ("Edit", {"file_path": str(target), "old_string": "a", "new_string": "b"}),
                       ("Bash", {"command": f"cp /dev/null {target}"})):
        out, _ = coordtools.pre_tool_use({"tool_name": tool, "tool_input": args, "cwd": str(p.harness)})
        assert "harness task" in out["hookSpecificOutput"]["permissionDecisionReason"], tool
    out, _ = coordtools.pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": str(target)}, "cwd": str(p.harness)})
    assert out is None


def test_a_codex_apply_patch_edits_a_listed_guard_only_through_a_harness_task(env, monkeypatch, tmp_path):
    """Codex's apply_patch naming a listed guard (Update, Delete, Add or Move to; absolute or relative to
    cwd; any input key) is refused outside a harness task, in a worker run and a full-tools coordinator."""
    from ttp import coordtools
    p, _ = _guarded(env, monkeypatch, tmp_path, {"guards/no_wipe.py": NO_WIPE}, ["guards/no_wipe.py"])
    target = p.harness / "guards" / "no_wipe.py"
    patches = [(f"*** Begin Patch\n*** {op}: {name}\n*** End Patch\n", cwd)
               for op in ("Update File", "Delete File", "Add File") for name, cwd in
               ((str(target), tmp_path), ("guards/no_wipe.py", p.harness), ("./no_wipe.py", p.harness / "guards"))]
    patches.append((f"*** Begin Patch\n*** Update File: {tmp_path}/x.py\n*** Move to: {target}\n*** End Patch\n", tmp_path))
    for patch, cwd in patches:
        assert "harness task" in (_worker("apply_patch", cwd, command=patch) or ""), patch
    assert "harness task" in (_worker("apply_patch", p.harness, input=patches[1][0]) or "")
    from ttp import hook
    out, _ = hook.pre_tool_use({"tool_name": "apply_patch", "tool_input": patches[0][0], "cwd": str(tmp_path)})
    assert "harness task" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert _worker("apply_patch", p.harness, command=patches[1][0].replace("no_wipe.py", "other.py")) is None
    monkeypatch.setenv(coordtools.FULL_ENV, "1")
    for patch, cwd in (patches[0], patches[4]):
        out, _ = coordtools.pre_tool_use({"tool_name": "apply_patch", "tool_input": {"command": patch}, "cwd": str(cwd)})
        assert "harness task" in out["hookSpecificOutput"]["permissionDecisionReason"], patch
    monkeypatch.delenv(coordtools.FULL_ENV)
    monkeypatch.setenv("TTP_TASK", str(p.db.add_task("tune the local guard", kind="harness")))
    for patch, cwd in patches:
        assert _worker("apply_patch", cwd, command=patch) is None, patch


def test_a_shell_command_names_a_listed_guard_only_by_whole_path_components(env, monkeypatch, tmp_path):
    p, _ = _guarded(env, monkeypatch, tmp_path, {"a.py": NO_WIPE, "guards/b.py": NO_WIPE}, ["a.py", "guards/b.py"])
    for cmd in ("rm a.py", "rm ./a.py", "mv 'a.py' x", f"rm {p.harness / 'a.py'}", "echo >a.py", "cat x;rm a.py",
                "rm guards/b.py", "rm ./guards/b.py", "cp x guards/b.py"):
        assert "harness task" in (_worker("Bash", p.harness, command=cmd) or ""), cmd
    for cmd in ("python3 data.py", "rm a.py.bak", "rm xa.py", "rm a.pyc", "rm old_guards/b.py", "rm guards/bb.py",
                "rm guards/b.py2", "cat ~a.py"):
        assert _worker("Bash", p.harness, command=cmd) is None, cmd


def test_a_local_guard_that_calls_sys_exit_refuses_and_names_itself(env, monkeypatch, tmp_path):
    from ttp import localguard
    cases = {"guards/exit_call.py": "import sys\n\n\ndef refusal(cmd):\n    sys.exit(3)\n\n\n"
                                    "def tool_refusal(tool, tool_input):\n    raise SystemExit('stop')\n",
             "guards/exit_import.py": "raise SystemExit(0)\n"}
    for rel, src in cases.items():
        p, run = _guarded(env, monkeypatch, tmp_path, {rel: src}, [rel])
        why = _worker("Bash", tmp_path, command="ls")
        name = rel.split("/")[1]
        assert why and name in why and "SystemExit" in why, (rel, why)
        assert _refusals(run)[-1]["kind"] == f"local:{name}"
        edit = _worker("Write", tmp_path, file_path=str(tmp_path / "x.txt"), content="x")
        if rel.endswith("exit_call.py"):
            assert edit and name in edit and "SystemExit" in edit
        else:   # not loaded: its tool check is unknown, so edits still go (to fix it)
            assert edit is None
            assert localguard.report({"guards": {"local": [rel]}}, p.harness)[0].startswith("problem")
    _guarded(env, monkeypatch, tmp_path, {"guards/ki.py": "def refusal(cmd):\n    raise KeyboardInterrupt\n"},
             ["guards/ki.py"])
    with pytest.raises(KeyboardInterrupt):   # an interrupt is never read as the guard's refusal
        _worker("Bash", tmp_path, command="ls")


def test_an_unreadable_project_json_with_no_last_good_copy_reports_its_unasked_local_guards(env, monkeypatch, tmp_path):
    """Decided (review #6659): not a refusal of every call, but a daemon alert and a doctor problem."""
    from ttp import localguard
    from ttp.daemon import Daemon
    from ttp.project import Project
    p, _ = _guarded(env, monkeypatch, tmp_path, {"guards/no_wipe.py": NO_WIPE}, ["guards/no_wipe.py"])
    assert _worker("Bash", tmp_path, command="wipe-scratch")
    p.config_path.write_text("{ broken")
    (p.state / "project.last-good.json").unlink(missing_ok=True)
    assert _worker("Bash", tmp_path, command="wipe-scratch") is None   # no guard list to ask
    d = Daemon(p.base)
    assert d.cfg_status == "unavailable"
    msgs = " ".join(str(dict(r)) for r in p.db.q("SELECT * FROM messages WHERE ref='config'"))
    assert "guards.local" in msgs and "not asked" in msgs
    q = Project(p.base)
    lines = localguard.report(q.config(), q.harness, q.config_status)
    assert lines == [f"problem: {localguard.UNREAD}"]
    assert localguard.report({}, q.harness, "ok") == []
