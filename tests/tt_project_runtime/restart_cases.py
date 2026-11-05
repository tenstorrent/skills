# Coordinator restart tests (ttp/preempt.py request_restart: a running task stopped at a safe point and requeued
# with new instructions) of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file, and pytest collects these tests
# under its name. Urgent preemption's own tests are in dispatch_cases.
from __future__ import annotations


def _rs_running(p, tmp_path, title="build it", attempts=1, branch="ttp/t1-build"):
    """A running task with one live run (its dir under tmp_path); returns (task id, run id, run dir)."""
    tid = p.db.add_task(title, "Goal: build the thing.", kind="code", tier="light", origin="user")
    p.db.update_task(tid, status="running", attempts=attempts, branch=branch)
    run_dir = tmp_path / f"run-{tid}-{time.monotonic_ns()}"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text("")
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
                 (tid, "worker", "fake", time.time(), "running", str(run_dir), "x"))
    return tid, rid, run_dir


def _rs_live(monkeypatch):
    """Daemon.reap_runs treats the stand-in runs as alive and not hung until their exit.json appears."""
    from ttp.daemon import Daemon
    monkeypatch.setattr(Daemon, "_run_alive", lambda self, r: True)
    monkeypatch.setattr(Daemon, "_hung", lambda self, r, up: None)


def test_a_restart_line_requeues_the_run_with_the_new_spec_keeping_branch_worktree_and_attempts(
        env, tmp_path, monkeypatch):
    from ttp import coordinator as coord, preempt
    from ttp.daemon import Daemon
    from ttp.prompts import worker_task
    p = make(env)
    _rs_live(monkeypatch)
    tid, rid, run_dir = _rs_running(p, tmp_path, attempts=2)
    # This run woke the task from a wait: the restart must not bring it back as a cheap wake of that wait.
    p.db.update_task(tid, result=json.dumps({"status": "waiting", "summary": "waits on the build",
                                             "retry_when": "test -e /nonexistent", "wake_tier": "light"}))
    worktree_dir = tmp_path / "wt"
    worktree_dir.mkdir()
    (worktree_dir / "kept.txt").write_text("work so far")
    assert coord.apply(p, [{"type": "task_update", "id": tid,
                            "spec": "Build only the CPU target.\nrestart: it is rebuilding the device target"}]) == []
    notes = p.db.kv(coord.NOTES_KEY, [])
    assert any("restarts at a safe point" in n for n in notes), notes
    assert (run_dir / "STOP").read_text() == preempt.RESTART_TEXT, "no lock held: stopped at once"
    assert "Build only the CPU target." in (run_dir / "steer.md").read_text()
    ev = p.db.q("SELECT text FROM events WHERE kind=? AND task=?", (preempt.RESTART_EVENT, tid))
    assert len(ev) == 1 and "rebuilding the device target" in ev[0]["text"], "each restart is logged with its why"
    # The worker had written a hand-off on its old course: the restart drops it.
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "built the device target"}))
    (run_dir / "exit.json").write_text(json.dumps({"rc": 143, "stopped": "shutdown", "ended": time.time()}))
    Daemon(p.base).reap_runs()
    t = p.db.task(tid)
    assert t["status"] == "queued" and t["attempts"] == 2 and t["branch"] == "ttp/t1-build"
    assert t["blocked_reason"] == "restarted by the coordinator: it is rebuilding the device target"
    assert "Build only the CPU target." in t["spec"], "requeued with the new spec"
    from ttp import budget as bud
    prev = json.loads(t["result"])
    assert prev["status"] == "restarted" and "retry_when" not in prev and bud.wake_tier("light", prev) is None
    assert (worktree_dir / "kept.txt").read_text() == "work so far"
    assert not p.db.q("SELECT id FROM events WHERE kind IN ('task_failed','task_done') AND task=?", (tid,)), \
        "a restart is neither a failure nor the old hand-off's end"
    prompt = worker_task(p, t, str(worktree_dir), t["branch"])
    assert prompt.startswith("# RESTARTED BY THE COORDINATOR"), prompt[:200]
    head = prompt.split("\n\n# ")[0]
    assert "rebuilding the device target" in head and "Build only the CPU target." in head
    assert "costs no attempt" in head


def test_a_run_holding_a_lock_stops_only_after_the_release(env, tmp_path, monkeypatch):
    from ttp import coordinator as coord, preempt
    from ttp.daemon import Daemon
    p = make(env)
    _rs_live(monkeypatch)
    tid, rid, run_dir = _rs_running(p, tmp_path)
    held = {(str(rid), "")}
    monkeypatch.setattr(preempt, "lock_holders", lambda dirs: set(held))
    coord.apply(p, [{"type": "task_update", "id": tid, "spec": "restart: wrong board"}])
    assert (run_dir / preempt.RESTART_FILE).exists() and not (run_dir / "STOP").exists(), "stopped mid-lock"
    d = Daemon(p.base)
    d.reap_runs()
    assert not (run_dir / "STOP").exists() and p.db.task(tid)["status"] == "running"
    held.clear()
    held.add(("", str(tid)))   # a detached job of the task holds one now: still waits
    d.reap_runs()
    assert not (run_dir / "STOP").exists()
    held.clear()
    d.reap_runs()
    assert (run_dir / "STOP").read_text() == preempt.RESTART_TEXT, "released: the reap pass stops it"
    (run_dir / "exit.json").write_text(json.dumps({"rc": 143, "stopped": "shutdown", "ended": time.time()}))
    d.reap_runs()
    assert p.db.task(tid)["status"] == "queued"
    assert len(p.db.q("SELECT id FROM events WHERE kind=?", (preempt.RESTART_EVENT,))) == 1


def test_the_cap_refuses_the_fourth_restart_in_the_window_and_is_configurable(env, tmp_path):
    from ttp import coordinator as coord, preempt
    p = make(env)
    tid, rid, run_dir = _rs_running(p, tmp_path)
    for i in range(3):
        if i:   # the last restart's run ended; a new run of the task is going the wrong way again
            p.db.x("UPDATE runs SET status='shutdown' WHERE task=?", (tid,))
            run_dir = tmp_path / f"run-again-{i}"
            run_dir.mkdir()
            p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
                   (tid, "worker", "fake", time.time(), "running", str(run_dir), "x"))
        assert preempt.request_restart(p, p.db, p.config(), p.db.task(tid), f"wrong way {i}") == ""
        if not i:   # a second ask before the stop updates the why and counts once
            assert preempt.request_restart(p, p.db, p.config(), p.db.task(tid), "wrong way 0b") == ""
            assert "wrong way 0b" in (run_dir / preempt.RESTART_FILE).read_text()
    assert len(p.db.q("SELECT id FROM events WHERE kind=?", (preempt.RESTART_EVENT,))) == 3
    p.db.x("UPDATE runs SET status='shutdown' WHERE task=?", (tid,))
    run_dir = tmp_path / "run-fourth"
    run_dir.mkdir()
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "running", str(run_dir), "x"))
    assert coord.apply(p, [{"type": "task_update", "id": tid, "spec": "Do it this way.\nrestart: wrong way 3"}]) == []
    notes = p.db.kv(coord.NOTES_KEY, [])
    assert any("restart refused" in n and "restarted 3 times in the last 6 h" in n for n in notes), notes
    assert not (run_dir / "STOP").exists() and not (run_dir / preempt.RESTART_FILE).exists()
    assert "Do it this way." in (run_dir / "steer.md").read_text(), "the refused restart still steers the run"
    assert p.db.q("SELECT id FROM events WHERE kind='task_restart_refused' AND task=?", (tid,))
    # Outside the window it is allowed again; restart_max is configurable.
    p.db.x("UPDATE events SET ts=ts-7*3600 WHERE kind=?", (preempt.RESTART_EVENT,))
    p.set_config("budget.restart_max", 0)
    assert "restart refused" in preempt.request_restart(p, p.db, p.config(), p.db.task(tid), "again")
    p.set_config("budget.restart_max", 3)
    assert preempt.request_restart(p, p.db, p.config(), p.db.task(tid), "again") == ""
    assert (run_dir / "STOP").read_text() == preempt.RESTART_TEXT


def test_a_plain_spec_update_only_steers_and_a_restart_needs_a_running_task_and_a_line_of_its_own(env, tmp_path):
    from ttp import coordinator as coord, preempt
    p = make(env)
    tid, rid, run_dir = _rs_running(p, tmp_path)
    for spec in ("Use the smaller board.", "The build must not restart: keep going.", "do not restart:\nnow"):
        coord.apply(p, [{"type": "task_update", "id": tid, "spec": spec}])
    assert "Use the smaller board." in (run_dir / "steer.md").read_text()
    assert not (run_dir / "STOP").exists() and not (run_dir / preempt.RESTART_FILE).exists()
    assert not p.db.q("SELECT id FROM events WHERE kind LIKE 'task_restart%'")
    assert preempt.restart_line("x\n- restart: went the wrong way ") == "went the wrong way"
    assert preempt.restart_line("Restart:   ") == ""
    # Cancel and restart in one update: the cancel wins.
    coord.apply(p, [{"type": "task_update", "id": tid, "status": "cancelled", "spec": "restart: no"}])
    assert (run_dir / "STOP").read_text() == "cancel" and not (run_dir / preempt.RESTART_FILE).exists()
    queued = p.db.add_task("not started", "s", kind="work", tier="light", origin="user")
    assert "not running" in preempt.request_restart(p, p.db, p.config(), p.db.task(queued), "why")


def test_ttp_task_restart_stops_a_running_task_and_records_the_why_in_its_spec(env, tmp_path, capsys):
    from ttp import cli, preempt
    p = make(env)
    tid, rid, run_dir = _rs_running(p, tmp_path)
    cli.main(["task", p.name, "restart", str(tid), "use the CPU path"])
    assert "restarts at a safe point" in capsys.readouterr().out
    assert (run_dir / "STOP").read_text() == preempt.RESTART_TEXT
    assert json.loads((run_dir / preempt.RESTART_FILE).read_text())["why"] == "use the CPU path"
    assert p.db.task(tid)["spec"].endswith("restart: use the CPU path")
    for bad in ([str(tid)], ["x", "why"]):
        with pytest.raises(SystemExit):
            cli.main(["task", p.name, "restart", *bad])
    queued = p.db.add_task("not started", "s", kind="work", tier="light", origin="user")
    with pytest.raises(SystemExit):
        cli.main(["task", p.name, "restart", str(queued), "why"])
    assert "restart:" not in p.db.task(queued)["spec"], "a refused restart leaves the spec alone"


def test_a_restart_of_a_run_handed_back_by_an_on_pass_dry_run_starts_fresh_with_the_restart_block(
        env, tmp_path, monkeypatch):
    # The handed-back run continued the old session (onpassdry.KEY): a restart drops that session with its
    # direction, so the next run neither resumes it nor counts as a wake of the dropped wait.
    from ttp import coordinator as coord, onpassdry
    from ttp.daemon import Daemon, _stale_wakes
    from ttp.prompts import worker_task
    p = make(env)
    _rs_live(monkeypatch)
    _sessions(env, monkeypatch, "s1")
    tid, rid, run_dir = _rs_running(p, tmp_path)
    worktree_dir = tmp_path / "wt"
    worktree_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"cwd": str(worktree_dir), "env": {}}))
    p.db.x("UPDATE runs SET provider='fake' WHERE id=?", (rid,))
    p.db.update_task(tid, result=json.dumps({
        "status": "waiting", "summary": "waits on the build", "retry_when": "test -e /nonexistent",
        "on_pass_cmd": "true", onpassdry.NOTE_KEY: "WARNING: on_pass_cmd cannot run as written",
        onpassdry.KEY: {"run": rid, "session": "s1", "text": "the on_pass_cmd cannot run"}}))
    d = Daemon(p.base)
    assert d._resumable(p.db.task(tid), "fake")["cause"] == "dry_run", "the setup resumes before the restart"
    assert coord.apply(p, [{"type": "task_update", "id": tid,
                            "spec": "Build only the CPU target.\nrestart: it is rebuilding the device target"}]) == []
    (run_dir / "exit.json").write_text(json.dumps({"rc": 143, "stopped": "shutdown", "ended": time.time()}))
    d.reap_runs()
    t = p.db.task(tid)
    prev = json.loads(t["result"])
    assert t["status"] == "queued" and prev["status"] == "restarted"
    assert onpassdry.KEY not in prev and onpassdry.NOTE_KEY not in prev, prev
    assert d._resumable(t, "fake") is None, "the session the coordinator dropped is never resumed"
    assert _stale_wakes(prev, {"key": "k", "head": "h"}, False) == 0
    prompt = worker_task(p, t, str(worktree_dir), t["branch"])
    assert prompt.startswith("# RESTARTED BY THE COORDINATOR"), prompt[:200]
    assert "Build only the CPU target." in prompt.split("\n\n# ")[0]
