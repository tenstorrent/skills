# Foreground-wait tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. A worker waiting on a remote run or a slow poll runs it detached and
# hands off `waiting`, freeing its slot (owner's agent msg #223); ttp.fgwait reminds and steers it.
from __future__ import annotations


def _fg_run_env(monkeypatch, run_dir, **extra):
    for k in ("TTP_DETACHED", "TTP_ROLE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.setenv("TTP_TASK", "41")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


_FG_REMOTE = ("ttp lock box-a -- ssh box-a 'cd /tmp/x && pytest -q'",
              "cd x && ttp lock rig-a,rig-b -- ./run_tests.sh",
              "ssh box-a 'cd /dev/shm/t && python -m pytest tests -x'",
              "ssh -o BatchMode=yes box make test", "ssh box \"a; ./run.sh\"",
              "true; ssh box ./bench.sh")
_FG_POLL = ("while ! test -e done; do sleep 30; done", "sleep 600", "tail -f run.log")
_FG_FINE = ("ttp lock --probe rig-a", "ttp lock --probe --json rig-a", "ssh box-a uptime",
            "ttp detach t1 --lock rig-a -- ./run_tests.sh", "ttp checks --detach -- pytest -q",
            "ttp push --detach", "nohup ssh box ./bench.sh > log 2>&1 &", "ssh box uptime; pytest -q",
            "pytest -q tests/x.py", "sleep 2", "git status")


def test_a_foreground_remote_run_or_poll_in_a_worker_run_gets_the_detach_reminder(env, tmp_path, monkeypatch):
    from ttp import fgwait, hook
    _fg_run_env(monkeypatch, tmp_path)
    for cmd in _FG_REMOTE + _FG_POLL:
        said = fgwait.reminder(cmd)
        assert said and "holds your worker slot" in said and "ttp detach <name> [--lock <box>] --" in said, cmd
        assert "hand off `waiting`" in said and "the command still runs" in said, cmd
        assert ("remote test run" in said) == (cmd in _FG_REMOTE), cmd
        out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}})
        hso = out["hookSpecificOutput"]
        assert said in hso["additionalContext"], cmd
        assert hso.get("permissionDecision") != "deny", f"a reminder, never a refusal: {cmd}"
    for cmd in _FG_FINE:
        assert fgwait.reminder(cmd) is None, cmd
        out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}})
        assert out is None or "holds your worker slot" not in str(out), cmd


def test_the_detach_reminder_is_only_for_a_worker_run_that_holds_a_slot(env, tmp_path, monkeypatch):
    from ttp import fgwait
    cmd = _FG_REMOTE[0]
    _fg_run_env(monkeypatch, tmp_path)
    assert fgwait.reminder(cmd)
    for k, v in (("TTP_ROLE", "coordinator"), ("TTP_DETACHED", "1")):   # the coordinator; a detached job
        monkeypatch.setenv(k, v)
        assert fgwait.reminder(cmd) is None, k
        monkeypatch.delenv(k)
    for k in ("TTP_RUN_DIR", "TTP_TASK"):   # outside a task's run (a user's shell)
        _fg_run_env(monkeypatch, tmp_path)
        monkeypatch.delenv(k)
        assert fgwait.reminder(cmd) is None, k
        assert fgwait.watch(tmp_path, ["rig-a"], ["true"], after=0) is None, k


def test_a_long_foreground_ttp_lock_in_a_worker_run_steers_it_to_detach_once(env, tmp_path):
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    go = tmp_path / "go"
    run_env = {**_lock_job_env(env, p, run_dir), "TTP_FG_WAIT_S": "1"}
    proc = subprocess.Popen([sys.executable, str(TTP), "lock", "rig-a", "--", *_until(go)],
                            env={**os.environ, "TTP_PROJECT": str(p.base), **run_env},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    steer = run_dir / "steer.md"
    try:
        assert _until_true(lambda: "holding your worker slot" in (steer.read_text() if steer.exists() else ""))
    finally:
        go.touch()
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    text = steer.read_text()
    assert text.count("## Update ") == 1, "one note per foreground command"
    assert "`ttp lock rig-a` has run its command in the foreground" in text
    assert f"`ttp detach bash --lock rig-a -- {shlex.join(_until(go))}`" in text, text
    assert "hand off `waiting` with the `retry_when` it prints" in text
    assert "ttp lock rig-a: foreground command running" in (run_dir / "progress.md").read_text()
    # A command that ends before the threshold leaves no note.
    steer.unlink()
    r = _ttp_run(p, "lock", "rig-a", "--", "true", env={**run_env, "TTP_FG_WAIT_S": "30"})
    assert r.returncode == 0, r.stderr
    time.sleep(0.5)
    assert not steer.exists()


def test_the_suggested_detach_hands_off_waiting_and_its_check_resumes_the_task_once_the_job_ends(env, tmp_path):
    """The detach-and-hand-off path: the steer note's `ttp detach` frees the slot (the worker hands off
    `waiting` with the printed retry_when) and that check passes (the task resumes) once the job ends,
    its log and exit code kept in the run folder."""
    from ttp import fgwait
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    go = tmp_path / "go"
    cmd = ["sh", "-c", f"echo started; {shlex.join(_until(go))}; echo finished"]
    line = fgwait.detach_line(["rig-a"], cmd)
    assert line.startswith("ttp detach sh --lock rig-a -- sh -c ")
    args = shlex.split(line)[1:]
    assert args[args.index("--") + 1:] == cmd
    r = _ttp_run(p, *args, env=_lock_job_env(env, p, run_dir))
    assert r.returncode == 0, r.stderr
    rc = (run_dir / "sh.rc").resolve()
    retry_when = f"{TTP} detach --check {rc}"
    assert f"hand off: status waiting, retry_when \"{retry_when}\"" in r.stdout, r.stdout
    check = shlex.split(retry_when)[1:]
    try:
        assert _until_true(lambda: _held_now(p, "rig-a"))
        assert _ttp_run(p, *check).returncode == 1, "still running: the task keeps waiting"
    finally:
        go.touch()
    assert _until_true(lambda: _ttp_run(p, *check).returncode == 0), "the job ended: the task resumes"
    assert rc.read_text().strip() == "0"
    log = rc.with_suffix(".log").read_text()
    assert "started" in log and "finished" in log, log
    assert _until_true(lambda: not _held_now(p, "rig-a"))


def test_every_worker_prompt_tells_a_waiting_worker_to_detach_and_free_its_slot(env, monkeypatch):
    from ttp.coordinator import TASK_KINDS
    from ttp.prompts import worker_system, worker_task
    p = make(env)
    rule = ("a remote test (`ttp lock <box> --`,\n  ssh) or a slow poll detached (`ttp detach`), hand off "
            "`waiting` with its `retry_when`: that frees your slot.")
    tasks = []
    for kind in TASK_KINDS:
        tid = p.db.add_task(f"a {kind}", f"SPEC-{kind}", kind=kind, tier="standard", origin="coordinator")
        tasks.append(p.db.task(tid))
        assert rule in worker_system(p, tasks[-1]), kind
    task = next(t for t in tasks if t["kind"] == "code")
    system, prompt = worker_system(p, task), worker_task(p, task, str(p.root), None)
    for provider in ("claude", "codex"):   # Codex takes the same text as its developer_instructions
        assert rule in _worker_input(p, monkeypatch, provider, prompt, system), provider
