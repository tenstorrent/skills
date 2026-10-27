# A harness run that commits runtime changes in the harness checkout is judged after the daemon
# restarted on them (ttp.runtimeload), and held when they did not load.

def _rl_run(p, tid, start, handoff=None):
    """A worker run of task `tid` in the harness checkout that started at harness commit `start` and
    has ended with `handoff` as its result.json."""
    rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,note) VALUES(?,?,?,?,?,?,?)",
                 (tid, "worker", "fake", "", time.time() - 60, "running",
                  json.dumps({"harness_head": start} if start else {})))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True)
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
    (run_dir / "output.jsonl").write_text("")
    (run_dir / "stderr.log").write_text("")
    (run_dir / "result.json").write_text(json.dumps(handoff or {"status": "done", "summary": "fixed the module"}))
    return rid


def _rl_finish(d, p, rid):
    d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                 {"rc": 0, "started": time.time() - 60, "ended": time.time(), "stopped": None})
    return p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"]


def _rl_setup(env, rel="runtime/ttp/ext_local.py"):
    """A harness task whose run committed `rel` in the harness checkout, and a daemon that imported
    its code before that commit."""
    p = make(env)
    from ttp.daemon import Daemon
    tid = _harness_task(p, "fix a runtime module")
    start = _hgit(p.harness, "rev-parse", "HEAD").strip()
    _commit_in_harness_tree(p.harness, rel, "X = 1\n", "fix the module")
    d = Daemon(p.base)
    d._loaded_at = time.time() - 3600
    return p, d, tid, start, _rl_run(p, tid, start)


def test_a_harness_checkout_run_that_changed_runtime_restarts_the_daemon_before_its_handoff_is_judged(env):
    from ttp import runtimeload, service
    from ttp.daemon import Daemon
    p, d, tid, start, rid = _rl_setup(env)
    assert _rl_finish(d, p, rid) == "running"          # left for the restarted daemon
    assert p.db.task(tid)["status"] == "running"
    req = json.loads((p.state / service.RESTART_REQUEST).read_text())
    assert f"run {rid}" in req["why"] and "runtime/ttp/ext_local.py" in req["why"]
    rec = p.db.kv(runtimeload.PENDING_KEY)[str(rid)]
    assert rec["at"] == req["at"] and rec["start"] == start and rec["files"] == ["runtime/ttp/ext_local.py"]
    # Later ticks of the old daemon wait: no second request, nothing judged.
    assert _rl_finish(d, p, rid) == "running"
    assert json.loads((p.state / service.RESTART_REQUEST).read_text())["at"] == req["at"]
    # The restart: a new daemon (a new process; nothing in memory) imported the new code.
    (p.state / service.RESTART_REQUEST).unlink()
    d2 = Daemon(p.base)
    d2._loaded_at = time.time() + 1
    assert _rl_finish(d2, p, rid) == "ok"
    assert p.db.task(tid)["status"] not in ("running", "blocked", "queued")
    assert str(rid) not in (p.db.kv(runtimeload.PENDING_KEY) or {})
    assert not (p.state / service.RESTART_REQUEST).exists()
    # Finished once: the run is no longer running, so no later tick or daemon processes it again.
    spent = p.db.one("SELECT COUNT(*) n FROM ledger")["n"]
    d2.reap_runs()
    Daemon(p.base).reap_runs()
    assert p.db.one("SELECT COUNT(*) n FROM ledger")["n"] == spent


def test_a_pending_runtime_handoff_survives_a_daemon_that_died_mid_restart(env):
    """The old daemon is gone before the restart helper ran: a daemon started after the request
    still runs code older than the run's commit (its file was written later), so it asks again."""
    from ttp import runtimeload, service
    from ttp.daemon import Daemon
    p, d, tid, start, rid = _rl_setup(env)
    assert _rl_finish(d, p, rid) == "running"
    (p.state / service.RESTART_REQUEST).unlink()      # a newer daemon drops an older request
    first = p.db.kv(runtimeload.PENDING_KEY)[str(rid)]["at"]
    d2 = Daemon(p.base)
    d2._loaded_at = time.time() - 1800
    assert _rl_finish(d2, p, rid) == "running"
    again = json.loads((p.state / service.RESTART_REQUEST).read_text())
    assert again["at"] > first and p.db.kv(runtimeload.PENDING_KEY)[str(rid)]["at"] == again["at"]


def test_a_runtime_change_that_rolled_back_holds_the_handoff_for_the_coordinator(env):
    from ttp import runtimeload
    from ttp.daemon import Daemon
    p, d, tid, start, rid = _rl_setup(env)
    assert _rl_finish(d, p, rid) == "running"
    # service.restart's rollback: runtime/ back to the last good commit, as a new commit.
    _hgit(p.harness, "rm", "-q", "runtime/ttp/ext_local.py")
    _hgit(p.harness, "commit", "-q", "-m", f"roll back runtime to {start[:10]}: the daemon did not start with it")
    d2 = Daemon(p.base)
    d2._loaded_at = time.time() + 1
    assert _rl_finish(d2, p, rid) == "ok"
    t = p.db.task(tid)
    assert t["status"] == "blocked" and int(t["attempts"] or 0) == 0
    assert f"the runtime change did not load, rolled back to {start[:10]}" in t["blocked_reason"]
    assert "fixed the module" in t["result"]                       # the hand-off is kept
    ev = p.db.q("SELECT kind, status, text FROM events WHERE task=? AND kind='task_blocked'", (tid,))
    assert len(ev) == 1 and ev[0]["status"] == "queued" and "rolled back to" in ev[0]["text"]
    assert str(rid) not in (p.db.kv(runtimeload.PENDING_KEY) or {})


def test_a_runtime_restart_that_failed_holds_the_handoff_instead_of_judging_it_with_the_old_code(env):
    from ttp import runtimeload, service
    p, d, tid, start, rid = _rl_setup(env)
    assert _rl_finish(d, p, rid) == "running"
    at = p.db.kv(runtimeload.PENDING_KEY)[str(rid)]["at"]
    (p.state / service.RESTART_REQUEST).unlink()
    (p.state / service.RESTART_RESULT).write_text(json.dumps(
        {"at": at, "outcome": "failed", "text": "The restart helper could not start"}))
    assert _rl_finish(d, p, rid) == "ok"
    t = p.db.task(tid)
    assert t["status"] == "blocked" and "the runtime change did not load" in t["blocked_reason"]
    assert "could not start" in t["blocked_reason"]


def test_runs_that_change_no_loaded_runtime_module_finish_without_a_restart(env):
    from ttp import runtimeload, service
    from ttp.daemon import Daemon
    p = make(env)
    for rel, start_known, loaded_after in (("prompts/extra.md", True, False),          # no runtime module
                                           ("runtime/ttp/notes.txt", True, False),     # runtime, not .py
                                           ("runtime/ttp/ext_a.py", False, False),     # a worktree run
                                           ("runtime/ttp/ext_b.py", True, True)):      # already loaded
        tid = _harness_task(p, rel)
        start = _hgit(p.harness, "rev-parse", "HEAD").strip()
        _commit_in_harness_tree(p.harness, rel, "y\n", rel)
        d = Daemon(p.base)
        d._loaded_at = time.time() + 1 if loaded_after else time.time() - 3600
        rid = _rl_run(p, tid, start if start_known else None)
        assert _rl_finish(d, p, rid) == "ok", rel
        assert p.db.task(tid)["status"] not in ("running", "blocked"), rel
        assert not (p.state / service.RESTART_REQUEST).exists(), rel
        assert not p.db.kv(runtimeload.PENDING_KEY), rel


def test_a_run_in_the_harness_checkout_records_its_start_commit_and_a_worktree_run_does_not(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    from ttp.daemon import Daemon

    class Proc:   # never a real runner
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    d = Daemon(p.base)
    head = _hgit(p.harness, "rev-parse", "HEAD").strip()
    tid = _harness_task(p, "a")
    rid = d.start_run("worker", "go", "claude", "light", str(p.harness), task=p.db.task(tid))
    assert json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"])["harness_head"] == head
    tid = p.db.add_task("build it", "s", kind="code", tier="light", origin="user")
    cwd, _ = d._workdir_for(p.db.task(tid))
    rid = d.start_run("worker", "go", "claude", "light", cwd, task=p.db.task(tid))
    assert "harness_head" not in json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"])


def test_a_failing_runtime_reload_check_finishes_the_run_as_before(env, monkeypatch):
    from ttp import runtimeload, service

    def broken(*a, **k):
        raise subprocess.TimeoutExpired("git", 60)
    p, d, tid, start, rid = _rl_setup(env)
    monkeypatch.setattr(runtimeload, "check", broken)
    assert _rl_finish(d, p, rid) == "ok"
    assert not (p.state / service.RESTART_REQUEST).exists()
