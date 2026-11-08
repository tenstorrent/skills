# Deliberate-stop tests (a run its STOP file asked to stop, whose supervisor died before writing exit.json)
# of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file, and pytest collects these tests under its name.
from __future__ import annotations


def _ds_orphan(p, d, tmp_path, tid, stop=None, name="orphan"):
    """A run of task tid whose supervisor died on this boot without an exit.json, reaped (finish_run's
    orphan path). `stop`: the text of its STOP file, None for none. Returns the run's row."""
    from ttp import runner
    run_dir = tmp_path / name
    run_dir.mkdir()
    if stop is not None:
        runner.request_stop(run_dir, stop)
    p.db.update_task(tid, status="running")
    (run_dir / "output.jsonl").write_text(json.dumps({"_session": "s", "_cost": 0.1}))
    (run_dir / "run.json").write_text(json.dumps({"cwd": str(p.root)}))
    (run_dir / "lease").touch()
    os.utime(run_dir / "lease", (time.time() - 300, time.time() - 300))   # its supervisor stopped renewing it
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id,note) VALUES(?,?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 600, "running", str(run_dir), d.boot, json.dumps({"spec_sha": "x"})))
    d.reap_runs()
    return p.db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))


def test_an_orphaned_run_its_stop_file_stopped_on_purpose_is_not_requeued_as_a_crash(env, tmp_path, monkeypatch):
    # A superseded or parked run (STOP "cancel", runner.stop_reason "stopped") whose supervisor died ends as
    # a deliberate stop does with a live supervisor (exit.json stopped="stopped": "killed"), never restarted.
    p = make(env)
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s")
    d = Daemon(p.base)
    for i, why in enumerate(("cancel", "superseded by #9")):
        tid = p.db.add_task(f"stopped {i}", "s", kind="work", tier="light", origin="user")
        run = _ds_orphan(p, d, tmp_path, tid, stop=why, name=f"o{i}")
        note = json.loads(run["note"])
        assert run["status"] == "killed" and not note.get("lost_to_crash"), (why, dict(run))
        task = p.db.task(tid)
        assert task["status"] != "queued" and task["attempts"] == 1, (why, dict(task))
        assert d._resumable(task, "fake") is None, why


def test_an_orphaned_run_without_a_stop_file_or_one_the_project_stop_wrote_is_still_requeued(env, tmp_path,
                                                                                              monkeypatch):
    # No STOP file: lost to a crash, requeued once with no attempt spent and its session resumed, as before.
    p = make(env)
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s")
    d = Daemon(p.base)
    tid = p.db.add_task("crashed", "s", kind="work", tier="light", origin="user")
    run = _ds_orphan(p, d, tmp_path, tid)
    assert run["status"] == "lost" and json.loads(run["note"]).get("lost_to_crash"), (run["status"], run["note"])
    task = p.db.task(tid)
    assert task["status"] == "queued" and task["attempts"] == 0, dict(task)
    assert d._resumable(task, "fake")["cause"] == "crash"
    # A STOP the project stop or a preemption wrote ("shutdown", stop_reason "shutdown") is no deliberate
    # end of the task: this path leaves it as it was.
    tid = p.db.add_task("preempted", "s", kind="work", tier="light", origin="user")
    run = _ds_orphan(p, d, tmp_path, tid, stop="preempt", name="pre")
    assert run["status"] == "lost" and json.loads(run["note"]).get("lost_to_crash"), (run["status"], run["note"])


def test_a_cancelled_task_s_orphaned_run_stays_cancelled_with_or_without_a_stop_file(env, tmp_path, monkeypatch):
    # _finish_worker's cancelled path: the cancel stands whatever the run's end is called.
    p = make(env)
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s")
    d = Daemon(p.base)
    for i, stop in enumerate(("cancel", None)):
        tid = p.db.add_task(f"cancelled {i}", "s", kind="work", tier="light", origin="user")
        p.db.update_task(tid, status="running")
        run_dir = tmp_path / f"c{i}"
        run_dir.mkdir()
        if stop:
            from ttp import runner
            runner.request_stop(run_dir, stop)
        (run_dir / "output.jsonl").write_text(json.dumps({"_session": "s", "_cost": 0.1}))
        (run_dir / "lease").touch()
        os.utime(run_dir / "lease", (time.time() - 300, time.time() - 300))
        p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id,note) VALUES(?,?,?,?,?,?,?,?)",
               (tid, "worker", "fake", time.time() - 600, "running", str(run_dir), d.boot, "{}"))
        p.db.update_task(tid, status="cancelled")
        d.reap_runs()
        task = p.db.task(tid)
        assert task["status"] == "cancelled" and task["attempts"] == 0, (stop, dict(task))
        assert json.loads(task["result"] or "{}").get("status") == "cancelled", (stop, dict(task))
