# Bench cell isolation tests (cellns.py, benchrun.isolate, bench.scrub_spec, the suite's --models/--class) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers (_bench_built, _bench_opts, _BENCH_FIX, _suite_home), fixtures and imports come from that file
# and from bench_cases.py. Add new cell isolation tests here.
from __future__ import annotations


def _namespace(name):
    # The backend `name` builds a namespace on this host, or the test skips: only a host with neither bwrap
    # nor unshare working (or user namespaces off) cannot run these.
    from ttp import cellns
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cellns-probe-"))
    got = None
    try:
        if shutil.which(name) and sys.platform.startswith("linux"):
            got = subprocess.run([*cellns.render(name, [*cellns.system_plan(), ("tmpfs", "/tmp")], tmp), "true"],
                                 capture_output=True, timeout=30, env={**os.environ, "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError):
        got = None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if got is None or got.returncode:
        pytest.skip(f"{name} cannot build a mount namespace on this host")
    return name


def _any_namespace():
    for name in ("bwrap", "unshare"):
        try:
            return _namespace(name)
        except pytest.skip.Exception:
            continue
    pytest.skip("neither bwrap nor unshare can build a mount namespace on this host")


_CELL_PROBE = ('{ cat "$PROBE_SRC/mod.py" 2>&1; cat "$PROBE_RT/ttp/__init__.py" 2>&1; echo "remote=[$(git remote)]"; '
          'echo "commits=$(git rev-list --all --count)"; echo "refs=$(git for-each-ref | wc -l)"; gh api repos/x 2>&1; '
          'echo "gh_rc=$?"; git fetch origin 2>&1; git clone "$PROBE_SRC" cl 2>&1; ls "$PROBE_HOME" 2>&1; '
          'echo "home=$HOME"; echo "token=[$GH_TOKEN$GITHUB_TOKEN$SSH_AUTH_SOCK]"; } > probe.txt 2>&1; ')


@pytest.mark.parametrize("backend", ["bwrap", "unshare"])
def test_cell_agent_sees_no_source_checkout_runtime_remote_or_gh(env, tmp_path, monkeypatch, backend):
    # What the agent runs finds a one-commit workspace: the source checkout, the runtime and the real home
    # do not exist for it, there is no remote and no ref but its own, `gh` fails, and no forge credential is set.
    from ttp import benchrun
    _namespace(backend)
    monkeypatch.setattr(benchrun, "ISOLATE", True)
    monkeypatch.setenv("TTP_CELL_BACKEND", backend)
    p, tid, task = _bench_built(env, tmp_path)
    repo = pathlib.Path(json.loads((tmp_path / "bench" / "manifest.json").read_text())["repo"])
    runtime = pathlib.Path(benchrun.__file__).resolve().parent.parent
    real_home = tmp_path / "real-home"
    (real_home / ".ssh").mkdir(parents=True)
    (real_home / ".ssh" / "id_x").write_text("SECRET-KEY")
    for k, v in (("PROBE_SRC", repo), ("PROBE_RT", runtime), ("PROBE_HOME", real_home)):
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("TTP_FAKE_SHELL", _CELL_PROBE + _BENCH_FIX)
    arms = {"a": {"provider": "claude", "model": "m-one", "effort": "medium"},
            "b": {"provider": "chat", "model": "m-two", "effort": "high"}}
    out = tmp_path / "iso"
    rows = benchrun.run(tmp_path / "bench", out, arms, 1, 1,
                        _bench_opts(tmp_path, repo=repo, state=p.state, isolate=True), p.config())
    assert len(rows) == 2 and all(r["isolated"] == backend and r.get("why") is None for r in rows), rows
    for arm in arms:
        probe = (out / "cells" / f"{tid}-r1-{arm}" / "diff.patch").read_text()
        _cell_probe_clean(probe)


def _cell_probe_clean(probe):
    assert "No such file" in probe and "+def old" not in probe, "the source checkout is not there to read"
    assert probe.count("No such file") >= 3, "nor the runtime, nor the real home folder"
    assert "remote=[]" in probe and "commits=1" in probe and "refs=1" in probe
    assert "gh_rc=1" in probe and "GitHub is not available" in probe, "gh is a stand-in that fails"
    assert "token=[]" in probe and "SECRET-KEY" not in probe and "id_x" not in probe
    assert f"home={pathlib.Path.home()}" not in probe, "HOME is the cell's own"
    assert "does not appear to be a git repository" in probe, "git has no origin to fetch"


@pytest.mark.parametrize("backend", ["bwrap", "unshare"])
def test_cell_self_test_names_what_is_visible_a_remote_or_a_second_commit(env, tmp_path, monkeypatch, backend):
    from ttp import cellns
    _namespace(backend)
    ws = tmp_path / "cell" / "work"
    ws.mkdir(parents=True)
    git = ["git", "-C", str(ws), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(ws)], check=True)
    (ws / "a").write_text("1")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    cell = cellns.build(ws.parent, ws, [], python=sys.executable, name=backend)
    assert cellns.selftest(cell, ws, [tmp_path / "nowhere"]) == [], "a clean one-commit workspace passes"
    assert any("visible inside the cell: /usr" in x for x in cellns.selftest(cell, ws, [pathlib.Path("/usr")]))
    subprocess.run([*git, "remote", "add", "origin", str(tmp_path / "elsewhere")], check=True)
    (ws / "a").write_text("2")
    subprocess.run([*git, "commit", "-qam", "second"], check=True)
    got = cellns.selftest(cell, ws, [])
    assert any("remote is not empty" in x for x in got) and any("not 1" in x for x in got), got


@pytest.mark.parametrize("backend", ["bwrap", "unshare"])
def test_cell_gh_standin_logs_and_fails_and_covers_a_system_gh(env, tmp_path, monkeypatch, backend):
    from ttp import cellns
    _namespace(backend)
    ws = tmp_path / "cell" / "work"
    ws.mkdir(parents=True)
    cell = cellns.build(ws.parent, ws, [], python=sys.executable, name=backend)

    def run(cmd):
        return subprocess.run(cell.wrap(["sh", "-c", cmd]), capture_output=True, text=True, cwd=ws)
    r = run("gh auth status")
    assert r.returncode == 1 and "not available" in r.stderr, r
    assert run("command -v gh").stdout.strip() == str(ws.parent / "bin" / "gh"), "the stand-in is first on PATH"
    if os.path.exists("/usr/bin/gh"):
        r = run("/usr/bin/gh pr list")
        assert r.returncode == 1 and "not available" in r.stderr, "a system gh is covered by the stand-in"
    assert "gh auth status" in (ws.parent / "home" / ".gh-calls").read_text()


def test_cell_runs_not_isolated_without_a_backend_and_is_never_scored(env, tmp_path, monkeypatch):
    from ttp import benchrun, cellns, modelsuite
    monkeypatch.setattr(benchrun, "ISOLATE", True)
    monkeypatch.setattr(cellns, "backend", lambda: None)
    p, tid, task = _bench_built(env, tmp_path)
    monkeypatch.setenv("TTP_FAKE_SHELL", _BENCH_FIX)
    arm = {"provider": "chat", "model": "org/m-one", "effort": "high"}
    row = benchrun.run_cell(tmp_path / "bench", tmp_path / "o", {**task, "repo": None},
                            {"task": tid, "rep": 1, "arm": "a"}, arm, _bench_opts(tmp_path, isolate=True), p.config())
    assert row["outcome"] == "not_isolated" and row["passed"] is False and "not isolated" in row["why"][0]
    assert not (tmp_path / "o" / "cells").exists(), "the agent never ran"
    got = modelsuite.standard("org/m-one", arm, "2026-11-10", tmp_path / "bench", None,
                              _bench_opts(tmp_path, isolate=True), {}, tmp_path / "o2")
    assert got["pass_rate"] is None and got["scored"] == 0 and got["not_isolated"] >= 1 and got["faults"] == 0
    assert modelsuite.verdict([{"model": "org/m-one", "class": "standard", **got}], "org/m-one", "daily") is None, \
        "a model whose standard class could not be isolated is never ranked"
    monkeypatch.setattr(benchrun, "ISOLATE", False)
    ok = benchrun.run_cell(tmp_path / "bench", tmp_path / "o3", {**task, "repo": None},
                           {"task": tid, "rep": 1, "arm": "a"}, arm, _bench_opts(tmp_path), p.config())
    assert ok["outcome"] == "pass", "only a test turns isolation off"


def test_cell_that_fails_its_self_test_is_an_error_and_never_runs(env, tmp_path, monkeypatch):
    from ttp import benchrun, cellns
    _any_namespace()
    monkeypatch.setattr(benchrun, "ISOLATE", True)
    p, tid, task = _bench_built(env, tmp_path)
    monkeypatch.setattr(cellns, "selftest", lambda *a, **k: ["visible inside the cell: /srv/checkout"])
    ran = []
    monkeypatch.setattr(benchrun, "launch", lambda *a, **k: ran.append(1))
    row = benchrun.run_cell(tmp_path / "bench", tmp_path / "o", {**task, "repo": None},
                            {"task": tid, "rep": 1, "arm": "a"}, {"provider": "chat", "model": "m", "effort": "high"},
                            _bench_opts(tmp_path, isolate=True), p.config())
    assert row["outcome"] == "error" and row["passed"] is False and not ran
    assert row["why"] == ["isolation self-test: visible inside the cell: /srv/checkout"]


def test_cell_prompt_scrub_rewrites_the_checkout_root_and_withholds_state_and_home_paths(tmp_path):
    from ttp import bench
    root, state, home = tmp_path / "src", tmp_path / "src" / "proj" / "state", tmp_path / "home"
    spec = (f"Edit {root}/plugins/x/a.py and run `cd {root} && pytest {root}/tests/t.py`. Last run: "
            f"{state}/runs/12/result.json and {state}/worktrees/t9/a.py; also {home}/.claude/plugins/c.py, ~/.ssh/id "
            f"and {root}-other/keep.py ({root}).")
    got = bench.scrub_spec(spec, [root], [state], home)
    assert "Edit plugins/x/a.py and run `cd . && pytest tests/t.py`." in got
    assert got.count("[path withheld]") == 4 and str(root) not in got.replace(f"{root}-other", "")
    assert f"{root}-other/keep.py" in got, "a sibling folder that only shares the prefix is not the checkout"
    assert "(.)." in got and str(state) not in got and str(home) not in got and "~/" not in got
    assert bench.scrub_spec("no paths here", [root], [state], home) == "no paths here"


def test_cell_agent_is_given_the_scrubbed_prompt_and_prep_scrubs_the_checkout_root(env, tmp_path, monkeypatch):
    from ttp import benchrun
    monkeypatch.setattr(benchrun, "ISOLATE", True)
    _any_namespace()
    p, tid, task = _bench_built(env, tmp_path)
    repo = pathlib.Path(json.loads((tmp_path / "bench" / "manifest.json").read_text())["repo"])
    seen = []
    real = benchrun.launch
    monkeypatch.setattr(benchrun, "launch", lambda run, ws, prompt, *a, **k: (seen.append((prompt, a[-1])),
                                                                              real(run, ws, prompt, *a, **k))[1])
    monkeypatch.setenv("TTP_FAKE_SHELL", _BENCH_FIX)
    cell_task = {**task, "repo": str(repo), "spec": f"Do it in {repo}/mod.py (notes: {p.state}/runs/1/result.json)."}
    row = benchrun.run_cell(tmp_path / "bench", tmp_path / "o", cell_task, {"task": tid, "rep": 1, "arm": "a"},
                            {"provider": "claude", "model": "m", "effort": "high"},
                            _bench_opts(tmp_path, isolate=True, state=p.state, repo=repo), p.config())
    prompt, cell = seen[0]
    assert "Do it in mod.py (notes: [path withheld])." in prompt
    assert str(repo) not in prompt and str(p.state) not in prompt
    assert row["isolated"] == cell.backend and row["outcome"] in ("pass", "flagged"), row
    assert str(repo) not in task["spec"], "prep wrote the spec with the checkout root already rewritten"


def test_in_cell_argv_drops_the_hooks_and_names_the_resolved_executable(tmp_path):
    from ttp import benchrun, cellns
    exe = tmp_path / "real-claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir()
    link.symlink_to(exe)
    cell = cellns.Cell("bwrap", ["bwrap", "--"], tmp_path / "home", cli=str(exe))
    settings = json.dumps({"hooks": {"Stop": [{"hooks": []}]}, "sandbox": {"enabled": False}})
    got = benchrun._in_cell([str(link), "-p", "--settings", settings, "-"], cell)
    assert got[:3] == ["bwrap", "--", str(exe)] and "-p" in got and got[-1] == "-"
    assert json.loads(got[got.index("--settings") + 1]) == {"sandbox": {"enabled": False}}, "no hooks, the rest stays"
    assert benchrun._in_cell(["other", "x"], cell)[2] == "other"


def test_cell_tool_binds_keep_a_folder_that_holds_a_hidden_path_out(tmp_path, monkeypatch):
    from ttp import cellns
    exe = tmp_path / "opt" / "cli" / "claude"
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    assert cellns.tool_binds(str(exe), []) == [exe.parent], "its own install folder"
    assert cellns.tool_binds(str(exe), [exe.parent / "secret"]) == [exe], "a folder with a hidden path: only the file"
    monkeypatch.setenv("HOME", str(exe.parent))
    assert cellns.tool_binds(str(exe), []) == [exe], "never the real home folder itself"
    assert cellns.tool_binds("/bin/sh", []) == [], "system folders are bound anyway"


def test_model_suite_runs_named_models_and_one_class_side_by_side(env, tmp_path, monkeypatch):
    from ttp import modelsuite
    p = make(env)
    _suite_home(env, monkeypatch)
    seen = []
    monkeypatch.setattr(modelsuite, "standard", lambda model, *a, **k: seen.append(model) or
                        {"pass_rate": 1.0, "injection_pass": None, "bar": False, "cell_rows": []})
    monkeypatch.setattr(modelsuite, "babysit", lambda *a, **k: pytest.fail("babysit is not this run's class"))
    cfg = {"providers": {"chat": {"model_ranking": ["m-a", "m-b", "m-c"]}}}
    rows = modelsuite.run(p.db, cfg, ["m-a", "m-b", "m-c"], {}, 1e7, date="2026-11-10", opts={}, has_cli=lambda: True,
                          models=["m-c", "m-a"], classes=("standard",))
    assert sorted(seen) == ["m-a", "m-c"] and [(r["model"], r["class"]) for r in rows] == [("m-c", "standard"),
                                                                                           ("m-a", "standard")]
    with pytest.raises(modelsuite.SuiteError, match="not a served model: m-z"):
        modelsuite.run(p.db, cfg, ["m-a"], {}, 1e7, date="2026-11-10", opts={}, has_cli=lambda: True, models=["m-z"])
    with pytest.raises(modelsuite.SuiteError, match="unknown class"):
        modelsuite.run(p.db, cfg, ["m-a"], {}, 1e7, date="2026-11-10", opts={}, has_cli=lambda: True, classes=("x",))


def test_model_bench_suite_cli_takes_models_and_class_and_passes_them_to_a_detached_run(env, tmp_path, monkeypatch):
    from ttp import cli, modelbench
    p = make(env)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setattr(modelbench, "home", lambda: tmp_path / "bench-home")
    seen = []

    class Child:
        pid = 4242

    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kw: seen.append(argv) or Child())
    cli.main(["model-bench", "suite", "--detach", "--models", "org/a,org/b", "--class", "standard"])
    argv = seen[0][seen[0].index("model-bench") + 1:]
    assert argv[argv.index("--models") + 1] == "org/a,org/b" and argv[argv.index("--class") + 1] == "standard"
    cli.main(["model-bench", "suite", "--detach", "--model", "org/a"])   # --model is --models
    assert "--models" in seen[1] and "--class" not in seen[1]
