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


# The daemon records the harness runtime tree it loaded, shows it against the checkout's, and restarts
# itself when that tree moves on (runtimeload.loaded, identity.summary, runtimeload.tree_moved).

def _rl_harness_daemon(p, monkeypatch):
    """A daemon whose code was imported from the harness checkout's runtime/ (as the service runs it)."""
    from ttp import daemon as dmod
    real = dmod.identity.capture

    monkeypatch.setattr(dmod.identity, "capture",
                        lambda *a, **k: {**real(*a, **k), "runtime": str(p.harness / "runtime")})
    d = dmod.Daemon(p.base)
    monkeypatch.setattr(dmod.identity, "capture", real)   # later daemons run their own runtime again
    return d


def test_the_daemon_start_receipt_records_the_harness_head_and_runtime_tree_it_loaded(env, monkeypatch, tmp_path):
    from ttp import runtimeload
    from ttp.daemon import Daemon, start_marker
    p = make(env)
    tree = _hgit(p.harness, "rev-parse", "HEAD:runtime").strip()
    d = _rl_harness_daemon(p, monkeypatch)
    assert d._loaded == {"harness_head": _hgit(p.harness, "rev-parse", "HEAD").strip(), "runtime_tree": tree}
    d._mark_start()
    rec = start_marker(p)["receipt"]
    assert rec["runtime_tree"] == tree and rec["harness_head"] == d._loaded["harness_head"]
    # No git, or a runtime imported from elsewhere: null, and the daemon still starts.
    (tmp_path / "runtime").mkdir()
    assert runtimeload.loaded(tmp_path, str(tmp_path / "runtime")) == {"harness_head": None, "runtime_tree": None}
    assert runtimeload.loaded(p.harness, str(tmp_path)) == {"harness_head": None, "runtime_tree": None}
    d2 = Daemon(p.base)   # the daemon's own runtime: outside the harness
    assert d2.identity["runtime_tree"] is None and d2.identity["harness_head"] is None


def test_status_shows_the_loaded_and_current_runtime_tree_and_marks_a_moved_one_stale(env, monkeypatch):
    from ttp import identity, web
    from ttp.project import hostname
    p = make(env)
    d = _rl_harness_daemon(p, monkeypatch)
    loaded = d._loaded["runtime_tree"]
    (p.state / "heartbeat").write_text(json.dumps({"pid": os.getpid(), "receipt": {**d.identity, "pid": os.getpid()}}))
    s = identity.summary(p.state, os.getpid(), p.harness / "runtime")
    assert s["loaded_tree"] == s["current_tree"] == loaded and not s["runtime_stale"]
    assert f"runtime tree {loaded[:10]}" in s["line"] and "stale" not in s["line"]
    _commit_in_harness_tree(p.harness, "runtime/ttp/ext_moved.py", "X = 2\n", "a commit by hand")
    s = identity.summary(p.state, os.getpid(), p.harness / "runtime")
    now = _hgit(p.harness, "rev-parse", "HEAD:runtime").strip()
    assert s["runtime_stale"] and s["loaded_tree"] == loaded and s["current_tree"] == now != loaded
    assert f"checkout now {now[:10]}: stale, the daemon restarts itself on it" in s["line"]
    assert "ttp " not in s["line"]                     # nothing for the user to run
    p.db.set_kv("daemon", {"pid": os.getpid(), "host": hostname()})
    js = web.runtime_receipt(p, p.db)                  # what `ttp status --json` carries
    assert js["runtime_stale"] and js["current_tree"] == now


def test_a_moved_runtime_tree_restarts_the_daemon_once_and_waits_while_a_handoff_is_judged(env, monkeypatch):
    from ttp import runtimeload, service
    from ttp.daemon import Daemon
    p = make(env)
    d = _rl_harness_daemon(p, monkeypatch)
    req = p.state / service.RESTART_REQUEST
    d._restart_on_moved_runtime()
    assert not req.exists()                            # nothing moved
    _commit_in_harness_tree(p.harness, "runtime/ttp/ext_up.py", "X = 1\n", "ttp upgrade")
    now = _hgit(p.harness, "rev-parse", "HEAD:runtime").strip()
    # Waits while a run's hand-off waits for its own restart, or a push batch is mid-flight.
    p.db.set_kv(runtimeload.PENDING_KEY, {"7": {"at": 1.0}})
    d._restart_on_moved_runtime()
    assert not req.exists()
    p.db.set_kv(runtimeload.PENDING_KEY, None)
    p.db.x("INSERT INTO push_batches(id,marker,target,started) VALUES('b1','m','t',?)", (time.time(),))
    d._restart_on_moved_runtime()
    assert not req.exists()
    p.db.x("UPDATE push_batches SET finalized=? WHERE id='b1'", (time.time(),))
    d._restart_on_moved_runtime()                      # the same request #3873's restart takes
    info = json.loads(req.read_text())
    assert now[:10] in info["why"] and d._tree_asked == now
    d._restart_on_moved_runtime()
    assert json.loads(req.read_text())["at"] == info["at"]   # asked already: no second request
    req.unlink()                                       # taken; the restart failed and this daemon runs on
    d._restart_on_moved_runtime()
    assert not req.exists()                            # once per tree, never a loop
    _commit_in_harness_tree(p.harness, "runtime/ttp/ext_up.py", "X = 2\n", "another commit")
    d._restart_on_moved_runtime()
    assert req.exists()                                # a new tree asks again
    req.unlink()
    d0 = Daemon(p.base)                                # no recorded tree: never asks
    _commit_in_harness_tree(p.harness, "runtime/ttp/ext_up.py", "X = 3\n", "a third commit")
    d0._restart_on_moved_runtime()
    assert not req.exists()


def test_a_moved_runtime_tree_waits_while_a_harness_run_works_there_or_an_upgrade_runs(env, monkeypatch):
    from ttp import locks, release, service
    p = make(env)
    d = _rl_harness_daemon(p, monkeypatch)
    req = p.state / service.RESTART_REQUEST
    tid = _harness_task(p, "fix a runtime module")
    start = _hgit(p.harness, "rev-parse", "HEAD").strip()
    rid = _rl_run(p, tid, start)
    _commit_in_harness_tree(p.harness, "runtime/ttp/ext_mid.py", "X = 1\n", "a mid-run commit")
    d._restart_on_moved_runtime()
    assert not req.exists() and d._tree_asked is None  # the run still works there: its end restarts
    assert "still works in the harness checkout" in d._tree_wait
    p.db.x("UPDATE runs SET status='done' WHERE id=?", (rid,))
    d._restart_on_moved_runtime()
    assert req.exists()                                # finished: the next tick asks
    req.unlink()
    # `ttp upgrade` holds the harness-upgrade lock while it moves main and restarts the daemon itself.
    held = locks.try_take([release.upgrade_lock(p)], "ttp upgrade", "exclusive")
    try:
        _commit_in_harness_tree(p.harness, "runtime/ttp/ext_mid.py", "X = 2\n", "ttp upgrade")
        d._restart_on_moved_runtime()
        d._restart_on_moved_runtime()
        assert not req.exists() and d._tree_wait == "a harness upgrade is in flight"
    finally:
        held.close()
    d._restart_on_moved_runtime()
    assert req.exists()                                # released: the next tick asks


def test_whether_a_runs_runtime_change_loaded_is_decided_by_the_recorded_tree_not_file_times(env):
    from ttp import runtimeload, service
    from ttp.daemon import Daemon
    p, d, tid, start, rid = _rl_setup(env)
    start_tree = _hgit(p.harness, "rev-parse", f"{start}:runtime").strip()
    end_tree = _hgit(p.harness, "rev-parse", "HEAD:runtime").strip()
    # File times say loaded (imported after the write); the recorded tree says not: the run waits.
    d._loaded_at, d._loaded = time.time() + 1, {"harness_head": start, "runtime_tree": start_tree}
    assert _rl_finish(d, p, rid) == "running"
    assert (p.state / service.RESTART_REQUEST).exists()
    # The restarted daemon loaded the run's tree; file times would say it predates the write: judged.
    (p.state / service.RESTART_REQUEST).unlink()
    d2 = Daemon(p.base)
    d2._loaded_at, d2._loaded = time.time() - 3600, {"harness_head": None, "runtime_tree": end_tree}
    assert _rl_finish(d2, p, rid) == "ok"
    assert p.db.task(tid)["status"] not in ("running", "blocked")
    assert str(rid) not in (p.db.kv(runtimeload.PENDING_KEY) or {})


def test_a_restarted_daemon_whose_recorded_tree_lacks_the_runs_change_holds_its_handoff(env):
    from ttp.daemon import Daemon
    p, d, tid, start, rid = _rl_setup(env)
    start_tree = _hgit(p.harness, "rev-parse", f"{start}:runtime").strip()
    d._loaded = {"harness_head": start, "runtime_tree": start_tree}
    assert _rl_finish(d, p, rid) == "running"
    _hgit(p.harness, "rm", "-q", "runtime/ttp/ext_local.py")
    _hgit(p.harness, "commit", "-q", "-m", f"roll back runtime to {start[:10]}: the daemon did not start with it")
    d2 = Daemon(p.base)
    d2._loaded_at = time.time() + 1
    d2._loaded = {"harness_head": None, "runtime_tree": _hgit(p.harness, "rev-parse", "HEAD:runtime").strip()}
    assert _rl_finish(d2, p, rid) == "ok"
    t = p.db.task(tid)
    assert t["status"] == "blocked"
    assert f"the runtime change did not load, rolled back to {start[:10]}" in t["blocked_reason"]
