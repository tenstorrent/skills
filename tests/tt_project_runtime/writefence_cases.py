# Worker launch, failed-tool-call hook, write_dirs, no_internet Restriction and coordinator write tests of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file, and pytest collects these tests under its name.
from __future__ import annotations


def _claude_launch(env, p, cwd, d=None):
    from ttp.providers import claude
    from ttp.daemon import Daemon
    d = d or Daemon(p.base)
    tid = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
    held = []

    class Held(subprocess.Popen):
        # start_run drops its runner's Popen: a runner that exited first (a loaded host) was then reaped by
        # Popen.__del__ or subprocess's cleanup, and os.waitpid here found no child. Held, only this test reaps it.
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            held.append(self)
    real = subprocess.Popen
    subprocess.Popen = Held
    try:
        rid = d.start_run("worker", "go", "claude", "light", str(cwd), task=p.db.task(tid))
    finally:
        subprocess.Popen = real
    (p.runs / str(rid) / "STOP").touch()
    spec = json.loads((p.runs / str(rid) / "run.json").read_text())
    pid = int((p.runs / str(rid) / "runner.pid").read_text().split()[0])
    proc = next(x for x in held if x.pid == pid)
    try:   # the launch is what is checked: stop the runner this test started (its own session)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()
    return spec


def test_claude_launch_reaps_a_runner_that_exited_before_start_run_returned(env, monkeypatch):
    from ttp import runner
    from ttp.providers import claude
    monkeypatch.setattr(claude.Claude, "binary", lambda self: "/usr/bin/true")
    gone = []
    real = runner.proc_start

    def after_exit(pid, *a, **k):   # a loaded host's timing: the runner is gone before start_run returns
        for _ in range(1200 if pid != os.getpid() else 0):   # the daemon reads its own start too
            try:
                if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    gone.append(pid)
                    break
            except OSError:
                break
            time.sleep(0.1)
        return real(pid, *a, **k)
    monkeypatch.setattr(runner, "proc_start", after_exit)
    spec = _claude_launch(env, make(env), env["repo"])
    assert gone and spec["argv"], "the helper reaps a runner the daemon's dropped Popen could have reaped"


def test_failed_tool_call_still_hands_over_the_coordinator_update(monkeypatch, tmp_path):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    (tmp_path / "steer.md").write_text("push to the other branch\n")
    out, delivered = hook.post_tool_use_failure({"tool_name": "Bash", "error": "Exit code 1"})
    assert "push to the other branch" in out["hookSpecificOutput"]["additionalContext"]
    delivered()
    out, _ = hook.post_tool_use_failure({"tool_name": "Bash", "error": "Exit code 2"})
    assert out is None, "the update is handed over once"
    assert hook.HANDLERS["PostToolUseFailure"] is hook.post_tool_use_failure


def test_claude_hooks_register_the_failure_event():
    from ttp.providers import claude
    hooks = claude.hook_settings()["hooks"]
    assert hooks["PostToolUseFailure"][0]["hooks"][0]["command"].endswith("ttp.hook PostToolUseFailure")
    assert hooks["PostToolUse"][0]["hooks"][0]["command"].endswith("ttp.hook PostToolUse")


def test_write_dirs_grants_only_directories_inside_the_project_root(tmp_path):
    from ttp.project import write_dirs
    root = tmp_path / "proj"
    base = root / "tt-project"
    for d in ("notes", "data/out", "tt-project/harness/memory", "tt-project/state"):
        (root / d).mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (root / "link-out").symlink_to(tmp_path / "elsewhere")
    (root / "link-in").symlink_to(root / "notes")
    (root / "link-state").symlink_to(base / "state")
    (root / "file.txt").write_text("x")
    assert write_dirs(root, base, None) == ([], [], []), "unset: no extra roots"
    granted, refused, missing = write_dirs(root, base, [
        "notes", str(root / "data/out"), "link-in", "link-out", "../elsewhere", str(tmp_path / "elsewhere"), ".",
        str(root), "data/..", "tt-project", "tt-project/state", "tt-project/harness/memory", "link-state",
        "absent", "file.txt", 3])
    assert granted == [str((root / "notes").resolve()), str((root / "data/out").resolve())], "a link grants its target"
    why = {r.split(": ", 1)[0]: r.split(": ", 1)[1] for r in refused}
    for e in ("link-out", "../elsewhere", str(tmp_path / "elsewhere")):
        assert "outside the project root" in why[e], (e, why)
    for e in (".", str(root), "data/.."):
        assert "project root itself" in why[e], (e, why)
    for e in ("tt-project", "tt-project/state", "tt-project/harness/memory", "link-state"):
        assert "charter, memory, config or state" in why[e], (e, why)
    assert "3" in why and len(refused) == 11
    assert [m.split(":")[0] for m in missing] == ["absent", "file.txt"]
    assert not (root / "absent").exists(), "a missing directory is never created"
    assert write_dirs(root, base, "notes")[1] == ['"notes": not a list of paths']


@pytest.mark.parametrize("var", ["TTP_RUN_ID", "TTP_RUN_DIR", "TTP_NOTE_RUN_DIR"])
@pytest.mark.parametrize("start,argv_tail", [
    (None, ["restrictions.no_internet", "true"]),                      # turns Codex's and Cursor's sandbox on
    (None, ["restrictions", json.dumps({"no_internet": True})]),       # through the parent key
    (True, ["restrictions.no_internet", "false"]),                     # clearing it loosens a Restriction
    (True, ["restrictions.no_internet", ""]),                          # unset, the same
    (True, ["restrictions", json.dumps({})]),                          # dropped through the parent key
])
def test_a_run_cannot_change_restrictions_no_internet(env, capsys, monkeypatch, var, start, argv_tail):
    """restrictions.no_internet turns sandboxing on (Codex workspace-write and read-only, Cursor --sandbox) and is
    a Restriction: a run's `ttp config` refuses it in both directions, directly or through `restrictions`, and
    the coordinator's config_set refuses it; the user's own `ttp config`, outside a run, sets it."""
    from ttp import cli, coordinator as coord
    from ttp.providers import get_provider
    p = make(env)
    if start is not None:
        p.set_config("restrictions.no_internet", start)
    before = p.raw_config()
    monkeypatch.setenv(var, "7")
    with pytest.raises(SystemExit) as e:
        cli.main(["config", "demo", *argv_tail])
    err = capsys.readouterr().err
    assert e.value.code == 2 and "only the user may change" in err and "restrictions.no_internet" in err, err
    assert p.raw_config() == before
    monkeypatch.delenv(var)
    for user_turn in (False, True):
        out = coord.apply(p, [{"type": "config_set", "key": "restrictions.no_internet", "value": not start}],
                          user_turn=user_turn)
        assert out and p.raw_config() == before, out
    cli.main(["config", "demo", "restrictions.no_internet", "true"])  # the user's own terminal
    cfg = p.config()
    assert get_provider("codex").fenced(cfg["restrictions"])


def test_coordinator_write_detector_reads_git_status_or_top_level_mtimes(tmp_path):
    from ttp import coordwrites
    repo, plain, run = tmp_path / "repo", tmp_path / "plain", tmp_path / "run"
    for d in (repo, plain, run):
        d.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (plain / "a").write_text("1")
    coordwrites.save(run, [repo, plain, tmp_path / "gone"])
    assert set(json.loads((run / coordwrites.FILE).read_text())) == {str(repo), str(plain)}
    assert coordwrites.changed(run) == [], "nothing changed"
    (repo / "new.txt").write_text("x")
    assert coordwrites.changed(run) == [str(repo)]
    os.utime(plain / "a", ns=(1, 1))
    assert coordwrites.changed(run) == [str(repo), str(plain)]
    assert coordwrites.changed(tmp_path / "none") == [], "no snapshot: no compare"


def test_daemon_logs_a_coordinator_turn_that_changed_files_without_a_model_call(env, monkeypatch):
    from ttp.daemon import Daemon
    calls = []
    monkeypatch.setattr(Daemon, "alert", lambda self, *a, **kw: calls.append(a))
    p = make(env)
    d = Daemon(p.base)
    run = env["tmp"] / "coord-run"
    run.mkdir()
    d._coord_snapshot(run)
    assert set(json.loads((run / "coord-writes.json").read_text())) >= {str(p.harness)}
    d._coord_writes({"id": 7}, run)
    log = lambda: (p.logs / "daemon.log").read_text() if (p.logs / "daemon.log").exists() else ""
    assert "coordinator turn changed files" not in log()
    (p.harness / "scratch.md").write_text("x")
    d._coord_writes({"id": 7}, run)
    assert f"run 7: warning: coordinator turn changed files in {p.harness}" in log()
    assert not calls, "a log line only: no alert, no model call"


