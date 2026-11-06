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
    t = p.db.add_task("harness", "tune the local guard")
    monkeypatch.setenv("TTP_TASK", str(t))
    assert _worker("Write", tmp_path, file_path=str(target), content=NO_WIPE) is None


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
