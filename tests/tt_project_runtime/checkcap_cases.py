# Host-wide checks cap tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new checks-slot tests (checkcap) here, not there.
from __future__ import annotations


def test_checks_waiter_serves_a_pass_recorded_while_it_waited_and_runs_nothing(env, tmp_path, monkeypatch):
    # The same checks passed elsewhere while these waited for a slot: once they take it, the
    # recorded pass serves and the slot goes to the next waiter.
    import threading
    from ttp import checkcap, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkcap, "DONE_S", 3600.0)   # only the look after taking the slot can find it
    keys = []
    real_key = cli._checks_key
    monkeypatch.setattr(cli, "_checks_key", lambda *a: keys.append(a) or real_key(*a))
    held = checkcap.acquire("other")
    ran, done = tmp_path / "ran", []
    t = threading.Thread(target=lambda: done.append(cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])))
    t.start()
    try:
        assert _wait_for(lambda: (run / cli.CHECKS_SLOT_WAIT).exists() and keys)
        cli._record_pass(*keys[0], str(run))
    finally:
        held.close()
    t.join(30)
    assert not t.is_alive() and not ran.exists(), "a recorded pass of the same checks runs nothing"
    assert "recorded while these waited for a checks slot" in (run / "checks.log").read_text()
    assert checkcap.line() == "", "the slot it took went back"


def test_checks_waiter_for_the_same_running_checks_waits_outside_the_queue_and_keeps_its_place(env, monkeypatch):
    import threading
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    twin = checkcap.acquire("twin", same="K")
    later = locks.enqueue(checkcap.slot_dir(), checkcap.RESOURCE, "later", since=2000.0, group="B")
    told, got = [], []
    t = threading.Thread(target=lambda: got.append(checkcap.acquire("w", tell=told.append, since=1000.0, project_name="A",
                                                                    same="K", max_wait_s=20)))
    t.start()
    try:
        assert _wait_for(lambda: any("the same checks run now (twin)" in m for m in told))
        assert checkcap.state()["queued"] == ["later"], "it waits for their result outside the queue"
        twin.close()   # they ended without a pass
        t.join(30)
        assert got and got[0].f is not None, "it queued again at its own arrival time, ahead of a later waiter"
        assert locks.queued(checkcap.slot_dir(), checkcap.RESOURCE) == [later]
        got[0].close()
    finally:
        twin.close()
        locks.dequeue(later)


def test_checks_waiter_for_the_same_running_checks_ends_with_no_slot_once_their_pass_is_recorded(env, monkeypatch):
    import threading
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    twin = checkcap.acquire("twin", same="K")
    told, got, passed = [], [], []
    t = threading.Thread(target=lambda: got.append(checkcap.acquire("w", tell=told.append, same="K", max_wait_s=20,
                                                                    done=lambda: bool(passed))))
    t.start()
    try:
        assert _wait_for(lambda: told)
        passed.append(1)
        t.join(30)
        assert got and got[0].f is None and got[0].served and got[0].env() == {}
        assert checkcap.state()["running"] == ["twin"] and checkcap.state()["queued"] == []
    finally:
        twin.close()


def test_checks_slot_is_released_only_after_the_pass_is_recorded(env, tmp_path, monkeypatch):
    # A waiter for the same checks that takes the freed slot must find the pass at once.
    from ttp import checkcap, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    real, running = cli._record_pass, []
    monkeypatch.setattr(cli, "_record_pass", lambda *a, **k: running.append(checkcap.state()["running"]) or real(*a, **k))
    cli.main(["checks", "--", "true"])
    assert running and running[-1], "the slot is still held while the pass is recorded"
    assert checkcap.line() == ""


def test_checks_never_run_without_a_slot_when_the_pass_that_ended_their_wait_cannot_be_read(env, tmp_path, monkeypatch):
    # done() saw a recorded pass, so acquire returned no slot; the pass was gone by the look after it.
    # The checks queued again instead of running uncapped.
    from ttp import checkcap, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkcap, "DONE_S", 0.0)
    held = checkcap.acquire("other")
    served = []

    def recorded(*a, **k):
        if sys._getframe(1).f_code.co_name == "<lambda>":   # done(), asked while waiting
            if not served:
                served.append(1)
                return {"run": "9", "ts": time.time()}
            return None
        if served:
            held.close()   # the pass reads as gone; the slot frees for the next try
        return None

    monkeypatch.setattr(cli, "_recorded_pass", recorded)
    seen = tmp_path / "seen"
    cli.main(["checks", "--", "sh", "-c", f'echo "${checkcap.HELD_ENV}" > {seen}'])
    assert served and seen.read_text().strip() == "1", "the checks ran only once they held a slot"
    assert checkcap.line() == ""


def test_checks_place_carries_only_a_still_waiting_launchs_arrival(tmp_path):
    from ttp import checkcap, cli
    now = time.time()
    old = {"pid": 4242, "arrived": now - 300}
    near = lambda t: abs(t - time.time()) < 5
    assert near(cli._checks_place(tmp_path, old, {})), "no slot wait: they took a slot already"
    (tmp_path / cli.CHECKS_SLOT_WAIT).write_text(json.dumps({"pid": 999}))
    assert near(cli._checks_place(tmp_path, old, {})), "the wait is another process's"
    (tmp_path / cli.CHECKS_SLOT_WAIT).write_text(json.dumps({"pid": 4242}))
    assert cli._checks_place(tmp_path, old, {}) == now - 300
    assert cli._checks_place(tmp_path, old, {checkcap.ARRIVED_ENV: repr(now - 900)}) == now - 900, "the earlier one"
    for bad in (now + 3600, "junk", "nan", -5):
        assert near(cli._checks_place(tmp_path, {"pid": 4242, "arrived": bad}, {})), bad


def test_restarted_detached_checks_keep_the_queue_place_of_the_ones_still_waiting(env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, cli, locks
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_TASK", "7")
    holder = _cap_holder("other")
    arrivals = lambda: [locks._arrival(t) for t in locks.queued(checkcap.slot_dir(), checkcap.RESOURCE)]
    try:
        cli.main(["checks", "--detach", "--", "true"])
        assert _wait_for(lambda: (run / cli.CHECKS_SLOT_WAIT).exists())
        info = json.loads((run / cli.CHECKS_PID).read_text())
        early = round(time.time() - 600, 3)
        (run / cli.CHECKS_PID).write_text(json.dumps({**info, "arrived": early}))
        cli.main(["checks", "--detach", "--", "true"])
        new = json.loads((run / cli.CHECKS_PID).read_text())
        assert new["pid"] != info["pid"] and new["arrived"] == early, "the restart keeps the waiting one's place"
        assert _wait_for(lambda: len(arrivals()) == 1 and abs(arrivals()[0] - early) < 1e-3), arrivals()
    finally:
        holder.kill()
        holder.wait()
    assert _wait_for(lambda: (run / cli.CHECKS_RC).exists())
    assert (run / cli.CHECKS_RC).read_text().strip() == "0"


def test_daemon_asks_only_an_ended_tasks_still_waiting_checks_to_stop_waiting(env, tmp_path):
    from ttp import cli
    from ttp.daemon import REAP_CHECKS_WINDOW_S, Daemon
    from ttp.runner import proc_start
    p = make(env)
    proc = subprocess.Popen(["sleep", "60"])
    try:
        started = proc_start(proc.pid)

        def run_of(status, wait_pid=None, start=None, updated=None, waits=True, task=True, run_status="ok",
                   run_started=None):
            tid = p.db.add_task("t", "s", kind="code")
            p.db.x("UPDATE tasks SET status=?, updated=? WHERE id=?", (status, updated or time.time(), tid))
            run_dir = tmp_path / f"run-{tid}"
            run_dir.mkdir()
            p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
                   (tid if task is True else task, "worker", "fake", run_started or time.time(), run_status,
                    str(run_dir)))
            (run_dir / cli.CHECKS_PID).write_text(json.dumps({"pid": proc.pid, "started": start or started}))
            if waits:
                (run_dir / cli.CHECKS_SLOT_WAIT).write_text(json.dumps({"pid": wait_pid or proc.pid}))
            return tid, run_dir

        stop = {s: run_of(s) for s in ("done", "failed", "cancelled")}
        # a task that ended days ago, a task that is gone and an ended run with no task wait for nobody too
        long_ago, gone, taskless = (run_of("done", updated=time.time() - 2 * 86400), run_of("queued", task=10 ** 6),
                                    run_of("queued", task=None))
        keep = [run_of("done", waits=False), run_of("done", wait_pid=proc.pid + 100000), run_of("done", start="1"),
                run_of("running"), run_of("waiting"), run_of("queued", task=None, run_status="running"),
                run_of("done", run_started=time.time() - REAP_CHECKS_WINDOW_S - 60)]
        d = Daemon(p.base)
        d.reap_waiting_checks(every_s=0)
        for status, (tid, run_dir) in stop.items():
            assert json.loads((run_dir / cli.CHECKS_SLOT_CANCEL).read_text()) == {
                "pid": proc.pid, "started": started, "why": f"task #{tid} ended ({status})"}
        whys = {k: json.loads((r / cli.CHECKS_SLOT_CANCEL).read_text())["why"]
                for k, (_, r) in {"long_ago": long_ago, "gone": gone, "taskless": taskless}.items()}
        rid = p.db.one("SELECT id FROM runs WHERE task IS NULL AND status='ok'")["id"]
        assert whys == {"long_ago": f"task #{long_ago[0]} ended (done)", "gone": f"task #{10 ** 6} is gone",
                        "taskless": f"run {rid} ended and has no task"}, whys
        assert not [r for _, r in keep if (r / cli.CHECKS_SLOT_CANCEL).exists()], \
            "checks with a slot, another process, live tasks, a running taskless run and old runs are left alone"
        path = stop["done"][1] / cli.CHECKS_SLOT_CANCEL
        os.utime(path, (1000, 1000))
        d.reap_waiting_checks(every_s=0)
        assert path.stat().st_mtime == 1000, "the same request is not written again"
    finally:
        proc.kill()
        proc.wait()


def test_daemon_stops_a_live_tasks_earlier_runs_waiting_checks_once_it_moved_to_a_newer_run(env, tmp_path):
    # #7172: a task re-dispatched (or woken again) whose earlier run's detached checks still wait or park
    # holds two checks runs; they are stopped unless a task's wait names them or another run follows them.
    from ttp import checkjoin, cli
    from ttp.daemon import Daemon
    from ttp.runner import proc_start
    p = make(env)
    proc = subprocess.Popen(["sleep", "60"])
    try:
        started = proc_start(proc.pid)
        runs = tmp_path / "runs"

        def task_of(status, result=None):
            tid = p.db.add_task("t", "s", kind="code")
            p.db.x("UPDATE tasks SET status=?, result=?, updated=? WHERE id=?",
                   (status, json.dumps(result) if result else None, time.time(), tid))
            return tid

        def run_of(tid, status="ok", role="worker", waits=True):
            rid = p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(?,?,?,?,?)",
                         (tid, role, "fake", time.time(), status))
            run_dir = runs / str(rid)
            run_dir.mkdir(parents=True)
            p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
            if waits:
                (run_dir / cli.CHECKS_PID).write_text(json.dumps({"pid": proc.pid, "started": started}))
                (run_dir / cli.CHECKS_SLOT_WAIT).write_text(json.dumps({"pid": proc.pid}))
            return rid, run_dir

        def probe(run_dir):
            return f"ttp checks --result {run_dir}"
        # its retry_when moved to the newer run's checks, and a re-dispatched task running its newer run
        moved = task_of("waiting")
        old, old_dir = run_of(moved)
        new, new_dir = run_of(moved)
        p.db.x("UPDATE tasks SET result=? WHERE id=?",
               (json.dumps({"status": "waiting", "retry_when": probe(new_dir)}), moved))
        redone = task_of("running")
        old2, old2_dir = run_of(redone)
        new2, _ = run_of(redone, status="running", waits=False)
        stop = {old_dir: f"task #{moved} moved to run {new}", old2_dir: f"task #{redone} moved to run {new2}"}
        keep = []
        # the task's own hand-off still waits on the earlier run's checks (a later wake reads them)
        own = task_of("waiting")
        _, d = run_of(own)
        run_of(own, waits=False)
        p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps({"status": "waiting", "retry_when": probe(d)}), own))
        keep.append(d)
        # another task waits on them, in a later step of its known sequence, by a path relative to the root
        theirs = task_of("waiting")
        _, d = run_of(theirs)
        run_of(theirs, waits=False)
        task_of("waiting", {"status": "waiting", "retry_when": "true",
                            "then": [{"retry_when": f"ttp checks --result state/{d.parent.name}/{d.name}"}]})
        keep.append(d)
        # another run joined them (checkjoin): it would lose its outcome
        joined = task_of("queued")
        _, d = run_of(joined)
        run_of(joined, waits=False)
        lead = checkjoin.register(p.state, task=str(joined), head="h", commit_ts=0, key="k", group="g", run=d.name,
                                  arrived=time.time(), pid=proc.pid)
        follower = checkjoin.register(p.state, task="99", head="h", commit_ts=0, key="k", group="g2", run="x",
                                      arrived=time.time())
        checkjoin.update(follower, phase=checkjoin.JOINED, follows=checkjoin.read(lead)["id"])
        keep.append(d)
        # the earlier run still runs, the newer run is a coordinator's, or the task has one run only
        for runs_of in (("running", "ok"), ("ok", "coordinator"), ("ok",)):
            t = task_of("queued")
            _, d = run_of(t, status=runs_of[0])
            if len(runs_of) > 1:
                run_of(t, role="worker" if runs_of[1] == "ok" else runs_of[1], waits=False)
            keep.append(d)
        # a name that only starts with the run's (runs/12 vs runs/123) is not a wait on it
        prefix = task_of("waiting")
        _, d = run_of(prefix)
        run_of(prefix, waits=False)
        task_of("waiting", {"status": "waiting", "retry_when": probe(d) + "0"})
        stop[d] = f"task #{prefix} moved to run "
        Daemon(p.base).reap_waiting_checks(every_s=0)
        for run_dir, why in stop.items():
            req = json.loads((run_dir / cli.CHECKS_SLOT_CANCEL).read_text())
            assert req["pid"] == proc.pid and req["started"] == started and req["why"].startswith(why), req
        assert not [d for d in keep if (d / cli.CHECKS_SLOT_CANCEL).exists()], \
            [d.name for d in keep if (d / cli.CHECKS_SLOT_CANCEL).exists()]
        assert not (new_dir / cli.CHECKS_SLOT_CANCEL).exists(), "the task's latest run keeps its checks"
        checkjoin.forget(p.state)   # the follower ended: the earlier run's checks follow nobody's wait now
        Daemon(p.base).reap_waiting_checks(every_s=0)
        assert (keep[2] / cli.CHECKS_SLOT_CANCEL).exists()
    finally:
        proc.kill()
        proc.wait()


def test_a_slot_cancel_request_ends_only_its_own_waiter_and_only_while_it_waits(env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, cli
    from ttp.runner import proc_start
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    me = {"pid": os.getpid(), "started": proc_start(os.getpid()), "why": "task #3 ended (done)"}
    assert cli._checks_cancelled(run) == ""
    for other in ({"pid": os.getpid() + 100000}, {"started": "1"}):
        (run / cli.CHECKS_SLOT_CANCEL).write_text(json.dumps({**me, **other}))
        assert cli._checks_cancelled(run) == "", other
    (run / cli.CHECKS_SLOT_CANCEL).write_text(json.dumps(me))
    assert cli._checks_cancelled(run) == "task #3 ended (done)"
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    free = checkcap.acquire("free", stop=lambda: "ended")
    assert free.f is not None, "a slot free at once is taken: only a wait is cancelled"
    with pytest.raises(checkcap.Cancelled, match="ended"):
        checkcap.acquire("w", stop=lambda: "ended", max_wait_s=20)
    assert checkcap.state()["queued"] == []
    ran = tmp_path / "ran"
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
    finally:
        free.close()
    assert e.value.code == 1 and not ran.exists()
    assert "not run: stopped waiting for a host-wide checks slot: task #3 ended (done)" in (run / "checks.log").read_text()


def test_checks_extra_slots_follow_the_load_and_never_pass_the_cap_plus_extra(env, monkeypatch):
    import threading
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "2")
    monkeypatch.setattr(checkcap, "cores", lambda: 16)
    load = [3.0]
    monkeypatch.setattr(checkcap, "_load", lambda: load[0])
    assert checkcap.extra() == 2
    load[0] = 6.0
    assert checkcap.extra() == 1
    load[0] = 9.0
    assert checkcap.extra() == 0
    load[0] = 1.0
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "1")
    assert checkcap.extra() == 1, "never more than checks.load_extra"
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "2")
    held, go = [], threading.Barrier(6)

    def take(i):
        go.wait()
        s = checkcap.acquire(f"t{i}", project_name=f"P{i}", max_wait_s=3)
        if s is not None:
            held.append(s)

    threads = [threading.Thread(target=take, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(held) == 3 and len(locks.held_labels(checkcap.all_paths())) == 3, "racing takers stop at cap plus extra"
    assert "host checks: 3 running of cap 1 +2 while the load is low" in checkcap.line()
    load[0] = 9.0
    held.pop().close()
    assert checkcap.acquire("late", max_wait_s=1) is None, "no new slot beyond the cap once the load rose"
    assert len(locks.held_labels(checkcap.all_paths())) == 2, "checks on extra slots run on to their end"
    for s in held:
        s.close()
    s = checkcap.acquire("next", max_wait_s=5)
    assert s is not None and s.f is not None
    s.close()


def test_check_commands_run_niced_where_nice_and_ionice_exist_unless_turned_off(env, monkeypatch):
    from ttp import cli, project
    have = {"nice"}
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}" if name in have else None)
    assert cli._low_priority(["pytest"]) == ["nice", "-n", "19", "pytest"]
    have.add("ionice")
    assert cli._low_priority(["pytest"]) == ["nice", "-n", "19", "ionice", "-c3", "pytest"]
    have.clear()
    assert cli._low_priority(["pytest"]) == ["pytest"], "neither found: run as it is"
    have.update({"nice", "ionice"})
    cli.main(["config", "--account", "checks.nice", "false"])
    assert project.load_account_settings()["checks"]["nice"] is False
    assert cli._low_priority(["pytest"]) == ["pytest"]


def test_checks_default_cap_scales_with_load_headroom_between_floor_and_ceiling(env, monkeypatch):
    from ttp import checkcap, project
    monkeypatch.delenv("TTP_CHECKS_HOST_MAX", raising=False)
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    load = [0.0]
    monkeypatch.setattr(checkcap, "_load", lambda: load[0])
    assert checkcap.default_range() == (4, 8)
    assert checkcap.check_load() == 8.0, "one run's cores: its test grant on 64 cores"
    assert checkcap.limit() == 8, "an idle host gets the ceiling"
    load[0] = 14.0
    assert checkcap.limit() == 6, "(64 - 14) / 8 rounds down"
    load[0] = 24.0
    assert checkcap.limit() == 5, "(64 - 24) / 8 checks fit"
    load[0] = 36.0
    assert checkcap.limit() == 4, "never below the floor of one slot per 16 cores"
    from ttp import testslots
    with monkeypatch.context() as m:   # the test pool off: CHECK_LOAD a check
        m.setattr(testslots, "sizes", lambda *a, **k: None)
        load[0] = 14.0
        assert checkcap.limit() == 8, "headroom for more than the ceiling still stops at it"
        load[0] = 43.0
        assert checkcap.limit() == 5, "(64 - 43) / 4 rounds down"
    load[0] = 200.0
    assert checkcap.limit() == 4
    monkeypatch.setattr(checkcap, "_load", lambda: float("inf"))
    assert checkcap.limit() == 4, "an unreadable CPU demand gives the floor"
    monkeypatch.setattr(checkcap, "_load", lambda: load[0])
    monkeypatch.setattr(checkcap, "cores", lambda: 8)
    load[0] = 0.0
    assert checkcap.default_range() == (1, 1) and checkcap.limit() == 1, "a small host runs one at a time"
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    project.set_account_setting("checks.host_max", 2)
    assert checkcap.limit() == 2, "a set cap wins over an idle host's ceiling"
    load[0] = 200.0
    project.set_account_setting("checks.host_max", 6)
    assert checkcap.limit() == 6, "and over a busy host's floor"
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "3")
    assert checkcap.limit() == 3, "the environment wins over the setting"


def test_checks_default_cap_shrinking_with_the_load_counts_checks_above_it(env, monkeypatch):
    from ttp import checkcap, locks
    monkeypatch.delenv("TTP_CHECKS_HOST_MAX", raising=False)
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setattr(checkcap, "cores", lambda: 32)   # floor 2, ceiling 4
    load = [0.0]
    monkeypatch.setattr(checkcap, "_load", lambda: load[0])
    held = [checkcap.acquire(f"t{i}", max_wait_s=1) for i in range(4)]
    assert all(s is not None and s.f for s in held)
    assert checkcap.acquire("over", max_wait_s=1) is None, "the ceiling holds"
    load[0] = 30.0   # the floor now: two checks on slots above it still count
    for s in held[:2]:
        s.close()
    assert len(locks.held_labels(checkcap.all_paths())) == 2
    assert checkcap.acquire("late", max_wait_s=1) is None, "two running fill a cap of two"
    held[2].close()
    s = checkcap.acquire("next", max_wait_s=5)
    assert s is not None and s.f is not None
    s.close()
    held[3].close()


def test_an_open_obligations_owner_waits_first_in_its_project_and_only_while_it_is_open(env, monkeypatch):
    import threading
    from ttp import checkcap
    from ttp import obligations as ob
    p = make(env)
    owner, plain, plain2 = (p.db.add_task(t, "s", kind="code", tier="light", origin="coordinator", priority=1)
                            for t in ("owner", "plain", "plain2"))
    oid = ob.add(p, "the urgent report", [owner])
    prio = {}
    for tid in (owner, plain, plain2):
        monkeypatch.setenv("TTP_TASK", str(tid))
        prio[tid] = checkcap.task_priority(p)
    assert prio == {owner: checkcap.OBLIGATION_PRIORITY, plain: 1, plain2: 1} and checkcap.OBLIGATION_PRIORITY < 1

    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    held = checkcap.acquire("h")
    got, told = [], []

    def take(label, proj, pr):
        s = checkcap.acquire(label, tell=told.append if label == "owner" else None, project_name=proj, priority=pr)
        got.append(label)
        s.close()

    waiters = []
    for i, args in enumerate([("a1", "A", prio[plain]), ("b1", "B", 1), ("a2", "A", prio[plain2]),
                              ("owner", "A", prio[owner])]):
        waiters.append(threading.Thread(target=take, args=args))
        waiters[-1].start()
        assert _wait_for(lambda: len(checkcap.state()["queued"]) == i + 1)
    mark = "owner" + checkcap.OBLIGATION_MARK
    assert checkcap.state()["queued"] == [mark, "b1", "a1", "a2"], \
        "the owner goes ahead of its project's earlier p1 waiters; project B keeps its turn"
    assert mark in checkcap.line()
    assert _wait_for(lambda: any("open obligation's owner" in t for t in told))
    held.close()
    for w in waiters:
        w.join(20)
    assert got == ["owner", "b1", "a1", "a2"]

    monkeypatch.setenv("TTP_TASK", str(owner))
    ob.record(p, oid, "receipt", "report sent", None)
    assert checkcap.task_priority(p) == 1, "the boost ends with the obligation's receipt"
    o2 = ob.add(p, "another urgent report", [owner])
    assert checkcap.task_priority(p) == checkcap.OBLIGATION_PRIORITY
    ob.drop(p, o2, "no longer wanted")
    assert checkcap.task_priority(p) == 1, "and once it is dropped"


def test_push_checks_slot_carries_the_obligation_boost_outside_the_push_priority_slot(env, monkeypatch):
    from ttp import checkcap, push
    from ttp import obligations as ob
    p = make(env)
    owner = p.db.add_task("owner", "s", kind="code", tier="light", origin="coordinator", priority=1)
    ob.add(p, "the urgent report", [owner])
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_TASK", str(owner))
    seen = []
    monkeypatch.setattr(checkcap, "acquire", lambda *a, **k: seen.append(k.get("priority")) or checkcap.Slot())
    monkeypatch.setattr(checkcap, "acquire_priority", lambda *a, **k: seen.append("push slot") or checkcap.Slot())
    push.check_slot("abc", lambda s: None)
    push.check_slot("abc", lambda s: None, priority="branch-key")
    assert seen == [checkcap.OBLIGATION_PRIORITY, "push slot"], \
        "own push checks queue boosted; push-branch checks (batches) keep their slot beyond the cap"


def _serve_turns(d, n, urgent_max):
    """Serve the fair queue's next `n` waiters the way checkcap.acquire does; their labels in order."""
    from ttp import locks
    got = []
    for _ in range(n):
        q = locks.fair_order(d, "r", urgent_max=urgent_max)
        if not q:
            break
        locks.mark_served(d, "r", q[0].read_text()[0].upper())
        locks.mark_turn(d, "r", q[0])
        got.append(q[0].read_text())
        locks.dequeue(q[0])
    return got


def test_checks_queue_serves_priority_1_first_by_project_for_a_bounded_run_of_turns(tmp_path):
    from ttp import locks
    d = tmp_path / "slots"
    spec = [("b1", "B", None), ("b2", "B", 2), ("a1", "A", 1), ("c1", "C", 1), ("a2", "A", 1), ("c2", "C", 1),
            ("a3", "A", 1), ("a4", "A", 3)]
    for n, (label, g, pr) in enumerate(spec):
        locks.enqueue(d, "r", label, since=1000.0 + n, group=g, priority=pr)
    names = lambda m: [x.read_text() for x in locks.fair_order(d, "r", urgent_max=m)]
    assert names(0) == ["b2", "a1", "c1", "b1", "a2", "c2", "a3", "a4"], "0: today's fair order"
    assert names(3) == ["a1", "c1", "a2", "b2", "c2", "a3", "b1", "a4"], \
        "p1 first, one project rotation for both, the next other waiter after 3 p1 turns in a row"
    assert names(9) == ["a1", "c1", "a2", "c2", "a3", "b2", "a4", "b1"]
    assert _serve_turns(d, 2, 3) == ["a1", "c1"]
    assert locks._streak(d, "r") == 2 and names(3)[:2] == ["a2", "b2"], "served p1 turns count toward the run"
    assert _serve_turns(d, 2, 3) == ["a2", "b2"]
    assert locks._streak(d, "r") == 0, "another waiter's turn ends the run"
    assert _serve_turns(d, 4, 3) == ["c2", "a3", "b1", "a4"]
    locks.enqueue(d, "r", "a5", group="A", priority=1)
    b9 = locks.enqueue(d, "r", "b9", group="B")
    assert _serve_turns(d, 1, 3) == ["a5"] and locks._streak(d, "r") == 1
    locks.dequeue(b9)
    locks.enqueue(d, "r", "a6", group="A", priority=1)
    assert _serve_turns(d, 1, 3) == ["a6"] and locks._streak(d, "r") == 0, "no run counts while nobody else waits"


def test_one_project_s_many_priority_1_checks_never_lock_out_other_projects(tmp_path):
    from ttp import locks
    d = tmp_path / "slots"
    for n in range(12):
        locks.enqueue(d, "r", f"a{n}", since=1000.0 + n, group="A", priority=1)
    locks.enqueue(d, "r", "b0", since=1100.0, group="B")
    locks.enqueue(d, "r", "c0", since=1101.0, group="C", priority=4)
    got = _serve_turns(d, 14, 3)
    assert got.index("b0") <= 3 and got.index("c0") <= 7, got
    assert [g for g in got if g[0] == "a"] == [f"a{n}" for n in range(12)]
    locks.mark_turn(d, "r", None)
    assert locks._streak(d, "r") == 0


def test_checks_queue_with_older_runtimes_tickets_keeps_today_s_order(tmp_path):
    # Older runtimes' tickets carry no priority (or no group): never urgent, so without a p1 waiter the
    # order is the fair order, and their waiters still get the other turns once one is.
    import fcntl
    from ttp import locks
    d = tmp_path / "slots"
    q = locks.queue_dir(d, "r")
    q.mkdir(parents=True)
    held = []
    for n, name in enumerate(["1000.000000-11", "1001.000000-12.gabc", "1002.000000-13.gabc"]):
        f = open(q / name, "a+")
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(f"old{n}")
        f.flush()
        held.append(f)
    locks.enqueue(d, "r", "new2", since=1003.0, group="B", priority=2)
    names = lambda m: [x.read_text() for x in locks.fair_order(d, "r", urgent_max=m)]
    assert names(3) == names(0) == ["old0", "old1", "new2", "old2"]
    locks.enqueue(d, "r", "new1", since=1004.0, group="B", priority=1)
    assert names(1) == ["new1", "old0", "old1", "new2", "old2"]
    for f in held:
        f.close()


def test_priority_1_checks_take_a_freed_slot_ahead_of_other_projects_unless_turned_off(env, monkeypatch):
    import threading
    from ttp import checkcap, project
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    assert checkcap.priority_turns() == checkcap.PRIORITY_TURNS
    real = project.load_account_settings
    monkeypatch.setattr(project, "load_account_settings", lambda: {"checks": {"priority_turns": 0}})
    assert checkcap.priority_turns() == 0
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_TURNS", "2")
    assert checkcap.priority_turns() == 2
    monkeypatch.setattr(project, "load_account_settings", real)
    for turns, want in (("0", ["b", "a1"]), ("2", ["a1", "b"])):
        monkeypatch.setenv("TTP_CHECKS_PRIORITY_TURNS", turns)
        held = checkcap.acquire("h")
        got = []

        def take(label, proj, prio):
            s = checkcap.acquire(label, project_name=proj, priority=prio)
            got.append(label)
            s.close()

        waiters = []
        for i, args in enumerate([("b", "B", None), ("a1", "A", 1)]):
            waiters.append(threading.Thread(target=take, args=args))
            waiters[-1].start()
            assert _wait_for(lambda: len(checkcap.state()["queued"]) == i + 1)
        assert checkcap.state()["queued"] == want
        held.close()
        for w in waiters:
            w.join(20)
        assert got == want


def test_each_checks_slot_taken_logs_its_wait_durably_in_the_takers_project_state(env, monkeypatch):
    # One JSON line per slot taken, at once or after queueing: project, task, priority and both times.
    import threading
    from ttp import checkcap, project
    p = make(env)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_TASK", "7")
    synced = []
    real = project.durable_append
    monkeypatch.setattr(project, "durable_append", lambda path, text: synced.append(path) or real(path, text))
    log = p.state / checkcap.WAITS_LOG
    arrived = time.time() - 30
    first = checkcap.acquire("A task #7", since=arrived, project_name=p.name, priority=2, state=p.state,
                             lane_at=arrived + 10)
    got = []
    t = threading.Thread(target=lambda: got.append(checkcap.acquire("A task #7 again", project_name=p.name,
                                                                    priority=None, state=p.state)))
    t.start()
    try:
        assert _wait_for(lambda: checkcap.line() and "queued" in checkcap.line())
        time.sleep(0.3)
    finally:
        first.close()
    t.join(30)
    assert not t.is_alive() and got and got[0]
    got[0].close()
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert [(r["project"], r["task"], r["priority"], r["slot"]) for r in rows] == [(p.name, 7, 2, "queue"), (p.name, 7, None, "queue")]
    assert 29 <= rows[0]["granted_at"] - rows[0]["queued_at"] < 60, "the wait counts from the place in the queue"
    assert rows[0]["lane_at"] == round(arrived + 10, 3) and "lane_at" not in rows[1], "when its lane was granted"
    assert rows[1]["granted_at"] - rows[1]["queued_at"] >= 0.3
    grants = checkcap.grants_dir() / checkcap.GRANTS_LOG
    assert synced == [log, grants, log, grants], "each line is appended with a sync, here and host-wide"
    # No project state (or the cap off): nothing logged; an unwritable state never costs the slot.
    checkcap.acquire("no project").close()
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "0")
    checkcap.acquire("cap off", project_name=p.name, state=p.state).close()
    assert len(log.read_text().splitlines()) == 2
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    checkcap.acquire("gone", project_name=p.name, state=p.state / "missing").close()


def test_push_checks_log_their_slot_wait_in_the_pushing_project_including_the_priority_slot(env, monkeypatch):
    from ttp import checkcap, push
    p = make(env)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.delenv("TTP_PROJECT", raising=False)
    push.check_slot("abc", lambda s: None, proj=p).close()
    push.check_slot("abc", lambda s: None, priority="branch-key", proj=p).close()
    rows = [json.loads(x) for x in (p.state / checkcap.WAITS_LOG).read_text().splitlines()]
    assert [(r["project"], r["slot"]) for r in rows] == [(p.name, "queue"), (p.name, "push")]
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    push.check_slot("abc", lambda s: None).close()
    assert len((p.state / checkcap.WAITS_LOG).read_text().splitlines()) == 3, "default: TTP_PROJECT's project"


def test_status_shows_the_last_24h_checks_slot_wait_median_and_p90_across_rotations(env, monkeypatch):
    from ttp import checkcap, cli
    p = make(env)
    now = time.time()
    assert checkcap.wait_line(p.state, now) == "" and "checks slot waits" not in cli.status_text(p)
    monkeypatch.setattr(checkcap, "WAITS_MAX_BYTES", 400)   # rotates every few lines; one old generation kept
    for wait in (5, 0, 10, 20, 30, 40, 50, 60, 70, 600):
        checkcap.record_wait(p.state, p.name, 1, now - 100 - wait, now - 100)
    assert (p.state / checkcap.WAITS_OLD).exists()
    assert sorted(checkcap.waits(p.state, now))[-1] == 600
    w = checkcap.waits(p.state, now)
    st = checkcap.wait_stats(p.state, now)
    assert st["n"] == len(w) and st["n"] >= 4, "both generations are read"
    monkeypatch.setattr(checkcap, "WAITS_MAX_BYTES", 256 * 1024)
    for name in (checkcap.WAITS_LOG, checkcap.WAITS_OLD):
        (p.state / name).unlink(missing_ok=True)
    for wait in (5, 0, 10, 20, 30, 40, 50, 60, 70, 600):
        checkcap.record_wait(p.state, p.name, 1, now - 100 - wait, now - 100)
    checkcap.record_wait(p.state, p.name, 1, now - 90000 - 9999, now - 90000)   # older than 24 h
    with open(p.state / checkcap.WAITS_LOG, "a") as f:
        f.write("not json\n{\"queued_at\": 1}\n")
    assert checkcap.wait_stats(p.state, now) == {"n": 10, "median_s": 35.0, "p90_s": 70.0}
    line = "checks slot waits (24 h): 10 taken, median 35s, p90 1.2 min"
    assert checkcap.wait_line(p.state, now) == line
    assert line in cli.status_text(p, verbose=True) and line not in cli.status_text(p)   # a --verbose line


def _grants():
    from ttp import checkcap
    log = checkcap.grants_dir() / checkcap.GRANTS_LOG
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def test_a_queued_checks_grant_says_its_priority_and_arrival_and_logs_host_wide(env, monkeypatch):
    import threading
    from ttp import checkcap
    p = make(env)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_TASK", "7")
    held = checkcap.acquire("h")
    arrived = time.time() - 120   # queued two minutes ago
    told, got = [], []
    t = threading.Thread(target=lambda: got.append(checkcap.acquire(
        "w", tell=told.append, since=arrived, project_name=p.name, priority=2, state=p.state)))
    t.start()
    try:
        assert _wait_for(lambda: any(x.startswith("queued for") for x in told))
    finally:
        held.close()
    t.join(30)
    got[0].close()
    at = time.strftime("%H:%M", time.gmtime(arrived))
    line = [x for x in told if "slot taken" in x]
    assert len(line) == 1 and line[0].startswith("host-wide checks slot taken after 2.0 min (priority p2, arrived ")
    assert line[0].endswith(f"arrived {at} UTC)") and "owner of" not in line[0]
    rows = [r for r in _grants() if r["project"] == p.name]
    assert len(rows) == 1
    r = rows[0]
    assert (r["task"], r["priority"], r["obligations"], r["slot"]) == (7, 2, [], "queue")
    assert r["since"] == round(arrived, 3) and 119 <= r["waited_s"] < 180 and r["ts"] >= r["since"]
    assert set(r) >= {"ts", "project", "task", "priority", "obligations", "since", "waited_s"}
    # The project's own wait record is the same grant (record_wait), not a second measurement.
    w = json.loads((p.state / checkcap.WAITS_LOG).read_text().splitlines()[-1])
    assert (w["queued_at"], w["granted_at"]) == (r["since"], r["ts"])


def test_an_obligation_boosted_checks_grant_names_the_obligations_behind_it(env, monkeypatch):
    import threading
    from ttp import checkcap
    from ttp import obligations as ob
    p = make(env)
    tid = p.db.add_task("owner", "s", kind="code", tier="light", origin="coordinator", priority=1)
    monkeypatch.setenv("TTP_TASK", str(tid))
    assert checkcap.task_rank(p) == (1, [])
    o1, o2 = ob.add(p, "the urgent report", [tid]), ob.add(p, "another urgent report", [tid])
    prio, owns = checkcap.task_rank(p)
    assert prio == checkcap.OBLIGATION_PRIORITY and sorted(owns) == sorted([o1, o2])
    assert checkcap.task_rank(None) == (None, [])
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    held = checkcap.acquire("h")
    told, got = [], []
    t = threading.Thread(target=lambda: got.append(checkcap.acquire(
        "owner", tell=told.append, since=time.time() - 60, project_name=p.name, priority=prio, state=p.state,
        owns=owns)))
    t.start()
    try:
        assert _wait_for(lambda: any(x.startswith("queued for") for x in told))
    finally:
        held.close()
    t.join(30)
    got[0].close()
    line = next(x for x in told if "slot taken" in x)
    assert f"(priority p{checkcap.OBLIGATION_PRIORITY}, owner of {','.join(owns)}, arrived " in line
    r = [r for r in _grants() if r["project"] == p.name][-1]
    assert (r["priority"], r["obligations"]) == (checkcap.OBLIGATION_PRIORITY, owns)
    # Push checks of the same task pass the boost and its obligations through.
    from ttp import push
    push.check_slot("abc", lambda s: None, proj=p).close()
    r = [r for r in _grants() if r["project"] == p.name][-1]
    assert (r["priority"], r["obligations"]) == (checkcap.OBLIGATION_PRIORITY, owns)


def test_a_relaunch_taking_a_slot_at_once_still_tells_the_wait_it_inherited(env, monkeypatch):
    # A detached hand-over or a relaunch after a reboot queues at its first arrival (`since`): even when a
    # slot is free at once, the grant line and the host-wide log keep the wait it inherited.
    from ttp import checkcap
    p = make(env)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    told = []
    arrived = time.time() - 600
    checkcap.acquire("relaunch", tell=told.append, since=arrived, project_name=p.name, priority=3, state=p.state).close()
    assert len(told) == 1 and told[0].startswith("host-wide checks slot taken after 10.0 min (priority p3, arrived ")
    assert 599 <= _grants()[-1]["waited_s"] < 700
    # A slot taken at once with no inherited wait says nothing, but every grant is logged host-wide,
    # with no project as well.
    told.clear()
    checkcap.acquire("fresh", tell=told.append, project_name=p.name, priority=None, state=p.state).close()
    checkcap.acquire("no project", tell=told.append).close()
    assert told == []
    rows = _grants()
    assert len(rows) == 3 and rows[1]["priority"] is None and rows[1]["waited_s"] < checkcap.GRANT_TELL_S
    assert rows[2]["project"] is None
    assert "priority none" in checkcap.grant_line(None, [], time.time() - 60)


def test_the_host_wide_grants_log_is_bounded_and_never_costs_a_slot(env, monkeypatch):
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkcap, "GRANTS_MAX_BYTES", 600)
    for _ in range(12):
        checkcap.acquire("x", project_name="P").close()
    d = checkcap.grants_dir()
    assert (d / checkcap.GRANTS_OLD).exists()
    assert (d / checkcap.GRANTS_LOG).stat().st_size <= 600 and (d / checkcap.GRANTS_OLD).stat().st_size <= 600
    assert not list(checkcap.slot_dir().glob("*grants*")), "the slot directory holds only slots and the queue"
    monkeypatch.setattr(checkcap, "grants_dir", lambda: d / checkcap.GRANTS_LOG / "not-a-dir")
    checkcap.acquire("unwritable log").close()


def test_checks_log_records_each_check_commands_wall_clock_seconds_and_exit(env, tmp_path, monkeypatch):
    # Each check command's seconds go to checks.log, a failing one's too, so a slow suite shows there.
    import re
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    p.set_config("delivery.push_checks", ["sleep 0.3", "exit 4"])
    try:
        cli.main(["checks", "--fresh"])
    except SystemExit:
        pass
    log = (run / "checks.log").read_text()
    took = re.search(r"^ttp checks: sleep 0\.3 took (\d+\.\d) s \(exit 0\)$", log, re.M)
    assert took and float(took.group(1)) >= 0.3, log
    assert re.search(r"^ttp checks: exit 4 took \d+\.\d s \(exit 4\)$", log, re.M), log


# Host-wide test-worker slots (testslots) --------------------------------------------------------
_SLOT_HOLDER = """
import json, os, sys, time
from pathlib import Path
from ttp import testslots
g = testslots.acquire("holder " + sys.argv[1])
Path(sys.argv[2]).write_text(str(g.k))
while not Path(sys.argv[3]).exists():
    time.sleep(0.05)
"""


def _slot_holder(env_vars: dict, name: str, base, tmp_path):
    got, stop = tmp_path / f"{name}.k", tmp_path / f"{name}.stop"
    proc = subprocess.Popen([sys.executable, "-c", _SLOT_HOLDER, name, str(got), str(stop)],
                            env={**os.environ, **env_vars, "TTP_PROJECT": str(base),
                                 "PYTHONPATH": str(RUNTIME)})
    assert _wait_for(lambda: got.exists() and got.read_text())
    return proc, int(got.read_text()), stop


def test_test_slots_two_projects_share_one_host_pool_and_a_killed_holder_frees_its_slots(env, tmp_path, monkeypatch):
    from ttp import locks, testslots
    monkeypatch.setenv("TTP_TEST_POOL", "3")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "2")
    a_base, b_base = tmp_path / "proj-a", tmp_path / "proj-b"
    a_base.mkdir()
    b_base.mkdir()
    a, ka, _ = _slot_holder({}, "a", a_base, tmp_path)
    b, kb, b_stop = _slot_holder({}, "b", b_base, tmp_path)
    try:
        assert (ka, kb) == (2, 1), "the second project gets what the first left, never more than the pool"
        assert len(locks.held_labels(testslots.paths(3))) == 3
        assert testslots.acquire("third", wait_s=0.2) is None, "a full pool grants nothing more"
        a.kill()   # SIGKILL: no clean-up runs, the kernel drops its locks
        a.wait(10)
        g = testslots.acquire("after", wait_s=5)
        assert g is not None and g.k == 2, "a killed holder's slots are free again"
        g.close()
    finally:
        b_stop.write_text("")
        for proc in (a, b):
            if proc.poll() is None:
                proc.kill()
            proc.wait(10)


def test_test_slots_default_sizes_follow_the_cores_and_host_settings_override_them(env, monkeypatch):
    from ttp import cli, testslots
    assert testslots.sizes(cores=64) == (32, 8)
    assert testslots.sizes(cores=16) == (8, 4)
    assert testslots.sizes(cores=4) == (2, 1)
    assert testslots.sizes(cores=1) == (2, 1), "at least 2 slots, at least 1 per run"
    monkeypatch.setattr(testslots.checkcap, "cores", lambda: 24)
    assert testslots.sizes() == (12, 6), "the host's usable cores, mocked"
    cli.main(["config", "--account", "checks.test_pool", "20"])
    assert testslots.sizes() == (20, 8)
    cli.main(["config", "--account", "checks.test_run_max", "3"])
    assert testslots.sizes() == (20, 3)
    monkeypatch.setenv("TTP_TEST_POOL", "6")
    assert testslots.sizes() == (6, 3), "the environment wins over the account setting"
    cli.main(["config", "--account", "checks.test_pool", "0"])
    monkeypatch.delenv("TTP_TEST_POOL")
    assert testslots.sizes() is None and testslots.SLOTS_ENV not in testslots.worker_env({}), "test_pool 0 turns the pool off"


def test_test_slots_project_config_can_only_lower_the_host_limits(env, monkeypatch):
    from ttp import testslots
    monkeypatch.setattr(testslots.checkcap, "cores", lambda: 32)
    assert testslots.sizes({"checks": {"test_pool": 100, "test_run_max": 50}}) == (16, 8), "never raised"
    assert testslots.sizes({"checks": {"test_pool": 4}}) == (4, 4)
    assert testslots.sizes({"checks": {"test_pool": 6, "test_run_max": 2}}) == (6, 2)
    assert testslots.sizes({"checks": {"test_pool": 0}}) == (16, 8), "a project cannot turn the host's pool off"
    got = testslots.worker_env({"checks": {"test_run_max": 3}})
    assert {k: got[k] for k in ("PYTEST_XDIST_AUTO_NUM_WORKERS", "TTP_TEST_SLOTS")} == {
        "PYTEST_XDIST_AUTO_NUM_WORKERS": "1", "TTP_TEST_SLOTS": "3"}, "a bare -n auto: one worker; more through clip or sandbox"
    assert got["CMAKE_BUILD_PARALLEL_LEVEL"] == "8", "the build pool keeps its own limits"


def test_test_slots_clamp_only_lowers_pytest_worker_counts_above_the_grant(env):
    from ttp import push, testslots
    c = testslots.clamp
    assert c("pytest -q -n 64 tests", 4) == "pytest -q -n 4 tests"
    assert c("python3 -m pytest -n64 x", 4) == "python3 -m pytest -n4 x"
    assert c("pytest --numprocesses=64 x && pytest --numprocesses 9", 4) == \
        "pytest --numprocesses=4 x && pytest --numprocesses 4"
    assert c("pytest -n 2 x", 4) == "pytest -n 2 x", "below the grant: as it is"
    assert c("pytest -n auto x", 4) == "pytest -n auto x", "auto reads the environment"
    assert c("nice -n 19 pytest -n 16 | head -n 50", 4) == "nice -n 19 pytest -n 4 | head -n 50"
    assert c("make -j 64 && sort -n 64 f", 4) == "make -j 64 && sort -n 64 f", "no pytest: untouched"
    # By the executable's basename, and split only at operators outside quotes.
    assert c(".venv/bin/pytest -n 64", 4) == ".venv/bin/pytest -n 4"
    assert c("/usr/bin/python3 -m pytest -n 64", 4) == "/usr/bin/python3 -m pytest -n 4"
    assert c("pytest -k 'a | b; c' -n 64", 4) == "pytest -k 'a | b; c' -n 4"
    assert c(".venv/bin/pytest -n 3 -k 'a | b; c'", 4) == ".venv/bin/pytest -n 3 -k 'a | b; c'", "within it: as it is"
    assert c('pytest -k "x -n 64" -n 64 2>&1 | tail -n 99', 4) == 'pytest -k "x -n 64" -n 4 2>&1 | tail -n 99'
    assert c("cd tests/pytest && head -n 64 f", 4) == "cd tests/pytest && head -n 64 f", "not pytest's -n"
    assert testslots.over("pytest -k 'a; b' -n 64", 4) == [64]
    argv = push.check_argv("pytest -n 64 tests", None, {**os.environ, testslots.HELD_ENV: "3"}, isolate=False)
    assert argv[-1].endswith("pytest -n 3 tests")
    assert push.check_argv("pytest -n 64 tests", None, {}, isolate=False)[-1].endswith("pytest -n 64 tests")


def test_ttp_checks_passes_its_test_slot_grant_runs_without_nice_and_clamps_n(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, testslots
    import shutil as _shutil
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TEST_POOL", "6")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "3")
    real = _shutil.which
    monkeypatch.setattr(testslots.shutil, "which", lambda n, *a, **k: None if n in ("nice", "ionice") else real(n, *a, **k))
    seen = tmp_path / "seen"
    cli.main(["checks", "--", "sh", "-c",
              f'echo "$PYTEST_XDIST_AUTO_NUM_WORKERS $TTP_TEST_SLOTS" > {seen}; echo pytest -n 64 >> {seen}'])
    assert seen.read_text().split("\n")[0] == "3 3", "-n auto reads the grant"
    log = (run / "checks.log").read_text()
    assert "test slots: 3 granted of a host pool of 6" in log and "s waiting" in log
    assert not locks_held(testslots), "released once the checks end"


def test_test_slots_a_full_run_waits_for_its_minimum_grant_others_start_with_one(env, monkeypatch):
    """A full `ttp checks` run waits first in line until checks.test_run_min slots are free, then starts
    with at least that many; a pre-stage or light run and a push check still start with what is free."""
    import threading
    from ttp import testslots
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    monkeypatch.setenv("TTP_TEST_POOL", "8")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "8")
    monkeypatch.setattr(testslots, "POLL_S", 0.05)
    assert testslots.run_min({}, 8) == max(testslots.WORKERS_FLOOR, 4) == 4, "default: half a run's grant"
    assert testslots.run_min({"checks": {"test_run_min": 3}}, 8) == 3
    assert testslots.run_min({"checks": {"test_run_min": 20}}, 8) == 8, "never above a run's grant"
    other = testslots.acquire("other", cfg={"checks": {"test_run_max": 6}})
    assert other.k == 6
    told, got = [], []
    # Grant.close() frees its slots one at a time: a waiter polling mid-close would rightly start with the
    # 4..6 then free. So the test frees `other` only while the waiter is parked at `gate` between polls
    # (it has entered the gate one more time than it has left it).
    gate, at_gate, real_poll = threading.Lock(), [0, 0], testslots.poll_s

    def poll(s):
        if threading.current_thread() is t:
            at_gate[0] += 1
            with gate:
                at_gate[1] += 1
        return real_poll(s)
    monkeypatch.setattr(testslots, "poll_s", poll)
    t = threading.Thread(target=lambda: got.append(testslots.acquire("full", told.append, cfg={"checks": {"test_run_min": 4,
                                                                                     "test_run_max": 7}}, full=True)))
    t.start()
    try:
        assert _wait_for(lambda: any("at least 4 free (checks.test_run_min); 2 free now" in x for x in told)), told
        assert not got, "no full run on 2 of the pool's slots"
        push = testslots.acquire("push", push_key="remote main", wait_s=0)
        assert push is not None and push.k == 8, "a push check never waits for the minimum"
        push.close()
    finally:
        with gate:
            assert _wait_for(lambda: at_gate[0] > at_gate[1]), "the waiter parks at its next poll"
            other.close()
    t.join(10)
    assert got and got[0].k == 7 and "granted" in told[-1], (got, told)
    light = testslots.acquire("pre-stage", wait_s=1)   # 1 free, fewer than the minimum
    assert light is not None and light.k == 1, "a run that is not full starts with 1"
    for g in (got[0], light):
        g.close()


def test_test_slots_a_full_run_on_a_pool_below_its_minimum_starts_without_deadlock(env, monkeypatch):
    from ttp import testslots
    monkeypatch.setenv("TTP_TEST_POOL", "2")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "8")
    monkeypatch.setenv(testslots.RUN_MIN_ENV, "6")
    g = testslots.acquire("full", cfg={}, full=True, wait_s=2)
    assert g is not None and g.k == 2, "the pool can never give 6: it starts with what it has"
    g.close()


def locks_held(testslots):
    from ttp import locks
    return locks.held_labels(testslots.paths(testslots.sizes()[0]))


def test_test_slots_a_full_pool_waits_with_a_line_then_runs(env, monkeypatch):
    import threading
    from ttp import testslots
    monkeypatch.setenv("TTP_TEST_POOL", "2")
    monkeypatch.setattr(testslots, "POLL_S", 0.05)
    first = testslots.acquire("first")
    assert first.k == 1
    second = testslots.acquire("second")
    told, got = [], []
    t = threading.Thread(target=lambda: got.append(testslots.acquire("waiter", told.append)))
    t.start()
    try:
        assert _wait_for(lambda: any("waiting for a host-wide test slot" in x for x in told))
        assert not got, "no grant while the pool is full, and no failure"
    finally:
        first.close()
    t.join(10)
    assert got and got[0].k == 1 and "granted" in told[-1]
    for g in (got[0], second):
        g.close()


def test_test_slots_off_restores_the_old_behaviour(env, tmp_path, monkeypatch):
    from ttp import cli, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TEST_POOL", "0")
    for var in (testslots.XDIST_ENV, testslots.SLOTS_ENV):
        monkeypatch.delenv(var, raising=False)
    g = testslots.acquire("x")
    assert g.k == 0 and g.env() == {}
    assert testslots.clamp_for("pytest -n 64", {}) == "pytest -n 64"
    seen = tmp_path / "seen"
    cli.main(["checks", "--", "sh", "-c", f'echo "[$PYTEST_XDIST_AUTO_NUM_WORKERS]" > {seen}'])
    assert seen.read_text().strip() == "[]"
    assert "test slots" not in (run / "checks.log").read_text()


def test_test_slots_nested_run_keeps_its_parents_grant(env, monkeypatch):
    from ttp import testslots
    monkeypatch.setenv(testslots.HELD_ENV, "2")
    g = testslots.acquire("nested", wait_s=0)
    assert g.k == 2 and not g.files


_SLOT_WAITER = """
import sys, time
from pathlib import Path
from ttp import testslots
g = testslots.acquire("waiter " + sys.argv[1], project_name=sys.argv[1])
Path(sys.argv[2]).write_text(f"{time.time()} {g.k}")
while not Path(sys.argv[3]).exists():
    time.sleep(0.05)
"""


def _slot_waiter(name: str, tmp_path):
    got, stop = tmp_path / f"{name}.got", tmp_path / f"{name}.stop"
    proc = subprocess.Popen([sys.executable, "-c", _SLOT_WAITER, name, str(got), str(stop)],
                            env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    return proc, got, stop


def _queued_test_waiters(n: int) -> bool:
    from ttp import checkcap, locks, testslots
    return len(locks.queued(checkcap.slot_dir(), testslots.RESOURCE)) >= n


def test_test_slots_waiters_are_served_first_come_and_a_killed_waiter_frees_its_place(env, tmp_path, monkeypatch):
    from ttp import testslots
    monkeypatch.setenv("TTP_TEST_POOL", "2")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "1")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    held = [testslots.acquire("a", project_name="pa"), testslots.acquire("b", project_name="pb")]
    assert [g.k for g in held] == [1, 1]
    procs = []
    try:
        first = _slot_waiter("p1", tmp_path)
        procs.append(first)
        assert _wait_for(lambda: _queued_test_waiters(1))
        dead = _slot_waiter("p2", tmp_path)
        procs.append(dead)
        assert _wait_for(lambda: _queued_test_waiters(2))
        last = _slot_waiter("p3", tmp_path)
        procs.append(last)
        assert _wait_for(lambda: _queued_test_waiters(3))
        dead[0].kill()   # a killed waiter's ticket goes with it
        dead[0].wait(10)
        held.pop().close()
        assert _wait_for(lambda: first[1].exists()), "the earliest waiter gets the freed slot"
        time.sleep(0.3)
        assert not last[1].exists(), "a later waiter never jumps the line"
        held.pop().close()
        assert _wait_for(lambda: last[1].exists()), "the killed waiter's place passes on, nothing leaks"
        assert float(first[1].read_text().split()[0]) < float(last[1].read_text().split()[0])
    finally:
        for proc, _, stop in procs:
            stop.write_text("")
            if proc.poll() is None:
                proc.kill()
            proc.wait(10)
        for g in held:
            g.close()


def test_test_slots_one_project_holds_at_most_its_budget_and_never_blocks_the_others(env, monkeypatch):
    from ttp import testslots
    monkeypatch.setenv("TTP_TEST_POOL", "8")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "2")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    assert testslots.project_max() == 8, "default: the runs the pool fits x a run's grant, within the pool"
    monkeypatch.setenv("TTP_TEST_PROJECT_MAX", "2")
    assert testslots.project_max() == 4, "2 checks at once x a run's grant, within the pool"
    a1 = testslots.acquire("a1", project_name="pa")
    a2 = testslots.acquire("a2", project_name="pa")
    assert (a1.k, a2.k) == (2, 2)
    assert testslots.acquire("a3", project_name="pa", wait_s=0.3) is None, "the project is at its budget"
    import threading
    got = {}
    t = threading.Thread(target=lambda: got.setdefault("a4", testslots.acquire("a4", project_name="pa", wait_s=5)))
    t.start()
    time.sleep(0.2)
    b = testslots.acquire("b", project_name="pb", wait_s=1)
    assert b is not None and b.k == 2, "another project goes ahead of a waiter whose project is at its budget"
    a1.close()
    t.join(10)
    assert got["a4"] is not None and got["a4"].k == 2, "freed budget goes back to its project's waiter"
    monkeypatch.setenv("TTP_TEST_PROJECT_MAX", "3")
    assert testslots.project_max({"checks": {"test_project_max": 1}}) == 2, "never below one run's share"
    for g in (a2, b, got["a4"]):
        g.close()


def test_push_checks_take_a_reserved_test_slot_when_task_checks_fill_the_pool(env, monkeypatch):
    from ttp import locks, testslots
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    monkeypatch.setenv("TTP_TEST_POOL", "4")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "2")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    tasks = [testslots.acquire("t1", project_name="pa"), testslots.acquire("t2", project_name="pb")]
    assert [g.k for g in tasks] == [2, 2]
    t0 = time.time()
    push = testslots.acquire("push", project_name="pa", push_key="remote main", wait_s=0)
    assert push is not None and push.k == 2 and time.time() - t0 < 2, "(a) one run's grant at once on the reserved lane"
    assert locks.held_labels([testslots.reserved_path("remote main")]) == ["push"]
    # (b) the reserved slot never goes to a task check: with the pool full, one waits for the pool.
    assert testslots.acquire("t3", project_name="pc", wait_s=0.3) is None
    other = testslots.acquire("push 2", project_name="pc", push_key="remote other", wait_s=0)
    assert other is not None and other.k == 2, "each push branch has its own reserved lane"
    push.close()
    other.close()
    tasks.pop().close()
    # (c) with free pool slots a push takes them within the per-run and per-project budgets.
    monkeypatch.setenv("TTP_TEST_PROJECT_SLOTS", "2")
    push = testslots.acquire("push", project_name="pb", push_key="remote main", wait_s=0)
    assert push.k == 2 and not locks.held_labels([testslots.reserved_path("remote main")])
    push.close()
    push = testslots.acquire("push", project_name="pa", push_key="remote main", wait_s=0)
    assert push.k == 2 and locks.held_labels([testslots.reserved_path("remote main")]) == ["push"], \
        "its project at budget: the reserved lane, never a pool slot past the budget"
    over = testslots.acquire("t4", project_name="pa", wait_s=0.3)
    assert over is None, "a task check of a project at its budget waits though the pool has room"
    push.close()
    tasks.pop().close()


def test_push_checks_get_a_full_runs_grant_when_task_checks_hold_the_project_budget(env, monkeypatch):
    from ttp import locks, testslots
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    monkeypatch.setenv("TTP_TEST_POOL", "16")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "8")
    monkeypatch.setenv("TTP_TEST_PROJECT_SLOTS", "8")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    task = testslots.acquire("t1", project_name="pa", priority=2)
    assert task.k == 8 and testslots.acquire("t2", project_name="pa", wait_s=0.2) is None, "pa's budget is full"
    # (a) a push check of that project gets one run's grant (was 1: the single reserved slot).
    push = testslots.acquire("push", project_name="pa", push_key="remote main", wait_s=0)
    assert push is not None and push.k == 8 and push.env()["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "8"
    # (b) a second push check of the same branch gets no second lane: it waits for slots.
    assert testslots.acquire("push 2", project_name="pa", push_key="remote main", wait_s=0.2) is None
    # (c) task checks are unchanged: another project's check gets its usual grant, never the lane.
    other = testslots.acquire("t3", project_name="pb", wait_s=0)
    assert other.k == 8 and testslots.acquire("t4", project_name="pc", wait_s=0.2) is None, "pool full"
    push.close()
    assert testslots.acquire("t4", project_name="pc", wait_s=0.2) is None, "a freed lane never goes to a task check"
    # A pool with only part of a run's grant free: the push takes those and the lane makes up the rest.
    other.close()
    monkeypatch.setenv("TTP_TEST_POOL", "10")
    other = testslots.acquire("t3", project_name="pb", wait_s=0)
    assert other.k == 2
    push = testslots.acquire("push", project_name="pb", push_key="remote main", wait_s=0)
    assert push.k == 8 and len(locks.held_labels(testslots.paths(10))) == 10, "its 2 pool slots count in the pool"
    for g in (push, other, task):
        g.close()
    # No push check waiting and slots free: a push takes pool slots only, the lane stays free.
    push = testslots.acquire("push", project_name="pa", push_key="remote main", wait_s=0)
    assert push.k == 8 and not locks.held_labels([testslots.reserved_path("remote main")])
    push.close()


_SUITE_HOLDER = """
import sys, time
from pathlib import Path
from ttp import testslots
g = testslots.suite_hold(sys.argv[1])
Path(sys.argv[2]).write_text("held")
while True:
    time.sleep(1)
"""


def test_one_full_checks_run_per_project_at_a_time_freed_by_a_crash(env, tmp_path, monkeypatch):
    from ttp import checkcap, testslots
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    monkeypatch.delenv(testslots.HELD_ENV, raising=False)
    monkeypatch.delenv(checkcap.HELD_ENV, raising=False)
    got = tmp_path / "held"
    proc = subprocess.Popen([sys.executable, "-c", _SUITE_HOLDER, "pa", str(got)],
                            env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    try:
        assert _wait_for(got.exists)
        lines = []
        assert testslots.suite_hold("pa", lines.append, wait_s=0.3) is None, "a second full run waits"
        assert lines and "one at a time per project" in lines[0]
        other = testslots.suite_hold("pb", wait_s=0)
        assert other is not None and other.files, "another project's full checks never wait for it"
        other.close()
        monkeypatch.setenv(testslots.HELD_ENV, "2")
        assert testslots.suite_hold("pa", wait_s=0).files == [], "a check nested in a run does not take it"
        monkeypatch.delenv(testslots.HELD_ENV)
        proc.kill()   # a crash: the kernel drops the lock, nothing stale stays
        proc.wait(10)
        again = testslots.suite_hold("pa", wait_s=5)
        assert again is not None and again.files
        again.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)


def test_a_timed_wait_whose_first_try_outlasts_it_still_says_what_it_waited_for(env, monkeypatch):
    """A loaded host: the first poll of a suite-lock or test-slot wait takes longer than its `wait_s`. The
    wait still says once why it waited before it gives up (was: None and nothing told). A wait_s=0
    suite-lock try stays silent; a wait_s=0 test-slot try still says what holds its slots."""
    from ttp import project, testslots
    monkeypatch.setenv("TTP_TEST_POOL", "2")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "2")
    real = project.ps_cycle
    monkeypatch.setattr(project, "ps_cycle", lambda: (time.sleep(0.3), real()))
    hold, slots = testslots.suite_hold("pa", wait_s=0), testslots.acquire("t1", project_name="pa", wait_s=0)
    assert hold.files and slots.k == 2
    try:
        lines, waits = [], []
        assert testslots.suite_hold("pa", lines.append, wait_s=0.1, waiting=waits.append) is None
        assert len(lines) == 1 and "one at a time per project" in lines[0] and waits == [True, False], lines
        lines.clear()
        assert testslots.acquire("t2", lines.append, wait_s=0.1, project_name="pb") is None
        assert len(lines) == 1 and "waiting for a host-wide" in lines[0], lines
        lines.clear()
        assert testslots.suite_hold("pa", lines.append, wait_s=0, waiting=waits.append) is None
        assert lines == [] and waits == [True, False], "a suite-lock try that never waits says nothing"
        assert testslots.acquire("t2", lines.append, wait_s=0, project_name="pb") is None
        assert len(lines) == 1 and "waiting for a host-wide" in lines[0], lines
    finally:
        hold.close()
        slots.close()


def test_ttp_checks_waits_for_its_project_s_other_full_checks(env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, cli, locks, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 0.3)
    detached = []
    monkeypatch.setattr(cli, "_checks_detach", lambda *a, **k: detached.append(1))
    hold = testslots.suite_hold(p.name, repo=testslots.repo_key(repo))
    assert hold.files
    seen = tmp_path / "seen"
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--", "sh", "-c", f"echo ran > {seen}"])
    finally:
        hold.close()
    assert e.value.code == 75 and detached == [1], "the foreground wait hands over to detached checks"
    assert not seen.exists(), "nothing ran beside the project's other full checks"
    assert "one at a time per project" in (run / "checks.log").read_text() + capsys.readouterr().out
    cli.main(["checks", "--", "sh", "-c", f"echo ran > {seen}"])
    assert seen.exists() and not locks.held_labels(testslots.lane_paths(p.name, 1, testslots.repo_key(repo))), \
        "with the other run gone it runs, and frees the project's suite lock at its end"


@pytest.mark.parametrize("moved", [False, True])
def test_detached_checks_waiting_for_their_project_s_full_checks_are_queued_and_stop_once_their_task_ended(
        env, tmp_path, monkeypatch, capsys, moved):
    """The per-project suite-lock wait is told like the checks-slot wait: CHECKS_SLOT_WAIT names it, the
    `--result` probe says queued, and the daemon's cancel for an ended task ends it with nothing run;
    `moved` (#7172): so does its cancel for a live task that moved to a newer run, freeing the queue."""
    from ttp import checkcap, cli, locks, testslots
    from ttp.daemon import Daemon
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    monkeypatch.setenv("TTP_TASK", "7")
    hold = testslots.suite_hold(p.name, repo=testslots.repo_key(repo))
    assert hold.files
    ran = tmp_path / "ran"
    try:
        cli.main(["checks", "--detach", "--", "sh", "-c", f"touch {ran}"])
        assert _wait_for(lambda: (run / cli.CHECKS_SLOT_WAIT).exists()), (run / "checks.out").read_text()
        info = json.loads((run / cli.CHECKS_PID).read_text())
        assert json.loads((run / cli.CHECKS_SLOT_WAIT).read_text())["pid"] == info["pid"]
        capsys.readouterr()
        assert _ttp("checks", "--result", str(run)) == 1
        said = capsys.readouterr().out
        assert f"waiting (pid {info['pid']}, not running) for this project's other full checks" in said, said
        assert f"held by {p.name}: full checks (pid {os.getpid()})" in said and "host-wide" not in said, said
        tid = p.db.add_task("t", "s", kind="code")
        p.db.x("UPDATE tasks SET status=?, updated=? WHERE id=?", ("queued" if moved else "done", time.time(), tid))
        p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
               (tid, "worker", "fake", time.time(), "ok", str(run)))
        newer = moved and p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
                                 (tid, "worker", "fake", time.time(), "running", str(tmp_path / "newer")))
        Daemon(p.base).reap_waiting_checks(every_s=0)
        assert _wait_for(lambda: (run / cli.CHECKS_RC).exists()), "the cancel did not end the wait"
        assert not locks.queued(checkcap.slot_dir(), testslots._suite_resource(p.name, testslots.repo_key(repo))), \
            "it left the project's checks queue"
    finally:
        hold.close()
    assert (run / cli.CHECKS_RC).read_text().strip() == "1" and not ran.exists()
    assert not (run / cli.CHECKS_SLOT_WAIT).exists()
    why = f"task #{tid} moved to run {newer}" if moved else f"task #{tid} ended (done)"
    assert f"not run: stopped waiting for this project's other full checks: {why}" in (run / "checks.log").read_text()


_SUITE_RUN = "touch {started}; while [ ! -e {release} ]; do sleep 0.05; done"


def test_two_full_checks_runs_of_one_project_never_overlap_whatever_their_purpose(env, tmp_path, monkeypatch, capsys):
    """A `ttp checks` run of the project holds its suite lock while its suite runs; a second run, for
    the same or another purpose, runs nothing beside it, and its log and progress name the run it waits
    for, in the project's home zone."""
    from ttp import checkcap, cli, testslots, timefmt
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 0.5)
    monkeypatch.setattr(cli, "_checks_detach", lambda *a, **k: None)
    for k in (testslots.HELD_ENV, checkcap.HELD_ENV):
        monkeypatch.delenv(k, raising=False)
    first = tmp_path / "runs" / "51"
    first.mkdir(parents=True)
    started, release = tmp_path / "started", tmp_path / "release"
    proc = subprocess.Popen([sys.executable, "-m", "ttp", "checks", "--purpose", "candidate", "--", "sh", "-c",
                             _SUITE_RUN.format(started=started, release=release)],
                            env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_RUN_DIR": str(first),
                                 "TTP_RUN_ID": "51"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    seen = tmp_path / "seen"
    try:
        assert _wait_for(started.exists), "the first run's suite never started"
        for purpose in ("candidate", "harness"):
            with pytest.raises(SystemExit) as e:
                cli.main(["checks", "--purpose", purpose, "--", "sh", "-c", f"touch {seen}"])
            assert e.value.code == 75 and not seen.exists(), f"a {purpose} run ran beside the first run's suite"
        told = (run / "checks.log").read_text()
        assert told.count(f"held by {p.name}: full checks (run 51, pid {proc.pid})") >= 2, told
        assert f"{timefmt.clock()[-3:]} checks: waiting for this project's other full checks" in \
            (run / "progress.md").read_text()
    finally:
        release.touch()
        proc.wait(30)
    assert proc.returncode == 0
    cli.main(["checks", "--purpose", "harness", "--", "sh", "-c", f"touch {seen}"])
    assert seen.exists(), "with the first run done the second runs"


def test_foreground_checks_waiting_for_the_suite_lock_then_a_slot_hand_over_within_one_foreground_wait(
        env, tmp_path, monkeypatch, capsys):
    """Both waits together end after FOREGROUND_WAIT_S, and the suite wait is recorded as a lock wait
    (outside the run's wall clock)."""
    from ttp import checkcap, cli, locks, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 2.0)
    detached, given = [], []
    monkeypatch.setattr(cli, "_checks_detach", lambda *a, **k: detached.append(1))
    acquire = checkcap.acquire

    def spy(*a, **k):
        given.append(a[4] if len(a) > 4 else k.get("max_wait_s"))
        return acquire(*a, **k)
    monkeypatch.setattr(checkcap, "acquire", spy)
    holder = _cap_holder("other")
    hold = testslots.suite_hold(p.name, repo=testslots.repo_key(repo))
    timer = threading.Timer(1.0, hold.close)   # the other run ends 1 s into the suite wait
    suite_hold = testslots.suite_hold

    def held(project, tell, *a, **k):
        return suite_hold(project, lambda line: (tell(line), timer.ident or timer.start()), *a, **k)
    monkeypatch.setattr(testslots, "suite_hold", held)
    seen = tmp_path / "seen"
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--", "sh", "-c", f"touch {seen}"])
    finally:
        timer.cancel()
        hold.close()
        holder.kill()
        holder.wait()
    assert e.value.code == 75 and detached == [1] and not seen.exists()
    assert len(given) == 1 and given[0] is not None and given[0] <= 1.0, \
        f"the checks slot got {given} of the foreground wait after a 1 s suite wait"
    waits = json.loads((run / locks.WAITS_FILE).read_text())
    assert waits and all(w["end"] is not None and w["end"] - w["start"] >= 1.0 for w in waits.values()), waits

def test_a_pytest_n_above_the_budget_is_lowered_with_a_line_saying_so_and_refused_in_a_worker_shell(env, monkeypatch):
    from ttp import hook, push, testslots
    out = testslots.clamped("cd x && pytest -n 64 tests", 4)
    assert out.splitlines()[0].startswith("echo ") and "pytest -n 64 lowered to -n 4" in out
    assert out.splitlines()[1] == "cd x && pytest -n 4 tests"
    assert testslots.clamped("pytest -n 4 tests", 4) == "pytest -n 4 tests", "within it: as it is, no line"
    argv = push.check_argv("pytest -n 64 tests", None, {**os.environ, testslots.HELD_ENV: "3"}, isolate=False)
    r = subprocess.run(["bash", "-c", argv[-1].replace("pytest -n 3 tests", "true")], capture_output=True, text=True)
    assert "lowered to -n 3" in r.stderr and r.returncode == 0
    monkeypatch.setenv("TTP_TEST_SLOTS", "4")
    assert "budget of 4" in hook.too_many_workers("python3 -m pytest -n 16 tests/a.py")
    assert hook.too_many_workers("ttp clip -- ttp sandbox -- pytest -n 4 tests/a.py") is None
    assert hook.too_many_workers("pytest -n auto tests/a.py") is None, "a bare -n auto starts one worker"
    assert hook.too_many_workers("ttp checks -- 'pytest -n 16 tests/a.py'") is None, "ttp checks lowers it"
    assert hook.too_many_workers("head -n 50 f && sort -n 99 g") is None
    monkeypatch.delenv("TTP_TEST_SLOTS")
    assert hook.too_many_workers("pytest -n 64 tests") is None, "outside a worker run: no budget"


@pytest.mark.parametrize("cmd", ["pytest -n 4 tests/a.py", "cd x && python3 -m pytest -n4 tests",
                                 "ttp detach --remote h t -- pytest --numprocesses=3 tests",
                                 "ttp clip -- true; pytest -n 2 t",
                                 "pytest -n $TTP_TEST_SLOTS tests", 'pytest -n "${TTP_TEST_SLOTS}" t',
                                 "python3 -m pytest -n$(nproc) t", "pytest --numprocesses=$N t",
                                 "ttp clip -- true; pytest -n `nproc` t"])
def test_a_workers_pytest_n_outside_clip_or_sandbox_is_refused_as_it_holds_no_host_test_slots(env, monkeypatch, cmd):
    from ttp import hook, testslots
    monkeypatch.setenv("TTP_TEST_SLOTS", "4")
    assert "holds none of this shared host's test slots" in (hook.too_many_workers(cmd) or "")
    for ok in ("pytest tests/a.py", "pytest -n 1 tests", "pytest -n 0 t", "ttp sandbox -- pytest -n 4 t",
               "ttp clip -- pytest -n 4 t", "ttp detach t -- ttp clip -- pytest -n 4 t",
               "ttp detach t -- pytest --numprocesses=3 tests", "ttp detach --lock a t -- pytest -n 4 t",
               "ttp clip -- ttp sandbox -- pytest -n $TTP_TEST_SLOTS t", "head -n $N f", "nice -n $N pytest t"):
        assert hook.too_many_workers(ok) is None, ok
    monkeypatch.setenv(testslots.HELD_ENV, "4")
    assert hook.too_many_workers(cmd) is None, "inside a run that holds slots (a check) it runs on them"


def test_ttp_detach_runs_a_workers_pytest_n_job_under_ttp_sandbox_so_it_takes_host_test_slots(env, tmp_path,
                                                                                             monkeypatch):
    from ttp import checkcap, cli, detach, push, testslots
    job, ttp = ["python3", "-m", "pytest", "-n", "4", "t"], ["ttp"]
    for k in (testslots.SLOTS_ENV, testslots.HELD_ENV, checkcap.HELD_ENV, "TTP_PROJECT", "TTP_PIDNS"):
        monkeypatch.delenv(k, raising=False)
    assert testslots.detach_argv(job, ttp) == job, "outside a worker run: no budget, as it is"
    monkeypatch.setenv(testslots.SLOTS_ENV, "4")
    assert testslots.detach_argv(job, ttp) == ["ttp", "sandbox", "--", *job]
    assert testslots.detach_argv(["env", "A=1", *job], ttp)[:3] == ["ttp", "sandbox", "--"]
    for same in (["ttp", "clip", "--", *job], ["ttp", "sandbox", "--", *job], ["pytest", "t"],
                 ["pytest", "-n", "auto", "t"], ["make", "-j", "4"]):
        assert testslots.detach_argv(same, ttp) == same, same
    monkeypatch.setenv(testslots.HELD_ENV, "4")
    assert testslots.detach_argv(job, ttp) == job, "a run that holds slots (a check) runs on them"
    monkeypatch.delenv(testslots.HELD_ENV)
    # `ttp detach` itself: the job it starts (and records for a relaunch) runs its pytest under `ttp sandbox`
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.setattr(push, "_boxed", lambda: False)
    monkeypatch.setattr(detach, "foreign_pidns", lambda: False)
    started = {}
    monkeypatch.setattr(detach, "start", lambda rc, log, argv, *a, **k: started.setdefault(Path(rc).stem, argv) and 0)
    monkeypatch.chdir(tmp_path)
    for name, argv in (("tests", job), ("plain", ["sh", "-c", "exit 0"])):
        try:
            cli.main(["detach", name, "--", *argv])
        except SystemExit as e:
            assert not e.code, e
    assert started["tests"][-len(job) - 2:] == ["sandbox", "--", *job], started
    assert Path(started["tests"][-len(job) - 3]).name == "ttp", started
    assert started["plain"] == ["sh", "-c", "exit 0"], "a job with no pytest -n is left as it is"
    rows = {j["name"]: j["argv"] for j in json.loads((run / "detached.json").read_text())}
    assert rows["tests"] == started["tests"], rows


def test_asked_reads_a_pytest_runs_worker_count(env):
    from ttp import testslots
    a = testslots.asked
    assert a("pytest -n 8 t", 4) == 8 and a("python3 -m pytest -n6 t", 4) == 6
    assert a("pytest --numprocesses=3 t", 4) == 3 and a("pytest --numprocesses 5 t", 4) == 5
    assert a("pytest -n auto t", 4) == 4 and a("pytest -n logical t", 4) == 4
    assert a("pytest t", 4) == 1 and a("pytest -n 0 t", 4) == 1
    assert a("nice -n 19 pytest t", 4) == 1, "a -n before the pytest word is not its"
    assert a("pytest t -- -n 9", 4) == 1, "nor one after --"
    assert a("head -n 50 f", 4) is None and a("make -j 8", 4) is None


def _direct_env(monkeypatch, pool=4, share=4):
    monkeypatch.setenv("TTP_TEST_POOL", str(pool))
    monkeypatch.setenv("TTP_TEST_RUN_MAX", str(pool))
    monkeypatch.setenv("TTP_CHECKS_LIVE_CORES", "0")
    monkeypatch.setenv("TTP_TEST_SLOTS", str(share))
    for v in ("TTP_TEST_SLOTS_HELD", "TTP_CHECKS_SLOT", "TTP_PROJECT"):
        monkeypatch.delenv(v, raising=False)


def test_a_workers_own_pytest_takes_host_test_slots_and_shrinks_to_what_is_free(env, monkeypatch):
    from ttp import locks, testslots
    _direct_env(monkeypatch)
    other = testslots.acquire("a ttp checks run", most=3)
    told: list = []
    try:
        assert other.k == 3
        g = testslots.direct("pytest -n 8 tests", "worker pytest", told.append)
        assert g is not None and g.k == 1 and len(g.files) == 1, "one slot left: it starts with one"
        assert g.env()[testslots.XDIST_ENV] == "1" and testslots.clamp("pytest -n 8 tests", g.k) == "pytest -n 1 tests"
        assert all(os.get_inheritable(fd) for fd in g.fds()), "the test process keeps them past an exec"
        assert len(locks.held_labels(testslots.paths(4))) == 4
        g.close()
        assert testslots.direct("pytest tests", "serial", told.append) is None, "a one-process run takes none"
        assert testslots.direct("make -j 4", "build", told.append) is None
        monkeypatch.setenv(testslots.HELD_ENV, "2")
        assert testslots.direct("pytest -n 8 tests", "nested", told.append) is None, "a check's own runs use its grant"
    finally:
        other.close()
    monkeypatch.delenv(testslots.HELD_ENV)
    g = testslots.direct("pytest -n 8 tests", "worker pytest", told.append)
    assert g.k == 4, "at most its share (TTP_TEST_SLOTS), never the -n it asked"
    g.close()
    monkeypatch.delenv("TTP_TEST_SLOTS")
    assert testslots.direct("pytest -n 8 tests", "no worker", told.append) is None, "outside a worker run: as before"


def test_a_workers_own_pytest_waits_for_a_full_pool_then_runs_on_one_worker(env, monkeypatch):
    import threading
    from ttp import testslots
    _direct_env(monkeypatch, pool=2, share=2)
    full = testslots.acquire("a ttp checks run", most=2)
    got, told = [], []
    monkeypatch.setattr(testslots, "DIRECT_WAIT_S", 30.0)
    t = threading.Thread(target=lambda: got.append(testslots.direct("pytest -n 2 t", "waiter", told.append)))
    t.start()
    try:
        assert _wait_for(lambda: any("waiting for a host-wide test slot" in m for m in told)), told
        assert not got, "a full pool: it waits"
    finally:
        full.close()
    t.join(30)
    assert got and got[0].k == 2 and got[0].files, "the freed slots go to the waiter"
    got[0].close()
    full = testslots.acquire("a ttp checks run", most=2)
    monkeypatch.setattr(testslots, "DIRECT_WAIT_S", 0.2)
    try:
        g = testslots.direct("pytest -n 2 t", "waiter", told.append)
        assert g.k == 1 and not g.files and "running on 1 test worker" in told[-1], "bounded: then one worker, no slot"
    finally:
        full.close()


_FAKE_PYTEST = """#!{python}
import json, os, sys, time
from pathlib import Path
out = Path(os.environ["FAKE_OUT"])
Path(f"{{out}}.tmp").write_text(json.dumps([" ".join(sys.argv[1:]), os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS", "")]))
os.replace(f"{{out}}.tmp", out)
while not Path(os.environ["FAKE_STOP"]).exists():
    time.sleep(0.05)
"""


@pytest.mark.parametrize("wrapper", [["sandbox", "--"], ["clip", "--"]])
def test_a_workers_own_pytest_holds_its_slots_until_the_test_process_ends_or_is_killed(env, tmp_path, monkeypatch, wrapper):
    import signal
    from ttp import locks, testslots
    _direct_env(monkeypatch, pool=4, share=3)
    bin_dir, xdist = tmp_path / "bin", tmp_path / "site" / "xdist"
    bin_dir.mkdir()
    xdist.mkdir(parents=True)
    (xdist / "__init__.py").write_text("")   # drop_xdist keeps -n: the fake's interpreter finds xdist
    fake = bin_dir / "pytest"
    fake.write_text(_FAKE_PYTEST.format(python=sys.executable))
    fake.chmod(0o755)
    out, stop = tmp_path / "fake-pytest.txt", tmp_path / "stop"
    run_env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_OUT": str(out), "FAKE_STOP": str(stop),
               "PYTHONPATH": f"{RUNTIME}:{tmp_path / 'site'}", "TTP_RUN_DIR": str(tmp_path)}
    held = lambda: len(locks.held_labels(testslots.paths(4)))  # noqa: E731
    for end in ("exit", "kill"):
        for f in (out, stop):
            f.unlink(missing_ok=True)
        proc = subprocess.Popen([sys.executable, "-m", "ttp", *wrapper, "pytest", "-n", "8", "tests"], env=run_env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            if not _wait_for(lambda: out.exists() or proc.poll() is not None) \
                    or proc.poll() is not None:
                proc.kill()
                raise AssertionError(proc.communicate()[0])
            assert json.loads(out.read_text()) == ["-n 3 tests", "3"], "-n lowered to its grant, -n auto's variable set to it"
            assert held() == 3, "the test process holds its slots"
            if end == "kill":
                os.killpg(proc.pid, signal.SIGKILL)   # the wrapper and the tests die at once: nothing cleans up
            else:
                stop.write_text("")
            proc.wait(30)
            assert _wait_for(lambda: held() == 0, 10), f"{end}: its slots are free again"
        finally:
            stop.write_text("")
            if proc.poll() is None:
                proc.kill()
            proc.wait(10)


def test_config_takes_checks_test_run_min_per_project_and_account(env, monkeypatch):
    from ttp import cli, testslots
    p = make(env)
    monkeypatch.delenv("TTP_TEST_RUN_MIN", raising=False)
    cli.main(["config", "--account", "checks.test_run_min", "3"])
    assert testslots.run_min({}, 8) == 3
    cli.main(["config", p.name, "checks.test_run_min", "5"])
    assert testslots.run_min(p.config(), 8) == 5, "the project's wins"


def _build_share(monkeypatch, k):
    monkeypatch.setenv("CMAKE_BUILD_PARALLEL_LEVEL", str(k))
    monkeypatch.setenv("MAKEFLAGS", f"-j{k}")
    for v in ("TTP_TEST_SLOTS_HELD", "TTP_BUILD_SLOTS_HELD"):
        monkeypatch.delenv(v, raising=False)


@pytest.mark.parametrize("cmd,n", [("make -j 64", 64), ("make -j64", 64), ("gmake --jobs=64", 64),
                                   ("ninja -j 64", 64), ("cmake --build . -j 32", 32),
                                   ("cmake --build . --parallel 32", 32), ("cd build && make -j 64", 64),
                                   ("(cd b; nice -n 5 /usr/bin/make -C x -j 64) 2>&1 | tail", 64),
                                   ("ttp clip -- make -j 2 && make -j 64", 64)])
def test_a_worker_shell_build_above_its_build_share_is_refused_pointing_at_ttp_clip(monkeypatch, cmd, n):
    from ttp import hook
    _build_share(monkeypatch, 4)
    why = hook.too_many_build_jobs(cmd)
    assert why and f"-j {n}" in why and "build share of 4" in why and "ttp clip -- <build>" in why, why


@pytest.mark.parametrize("cmd", ["make -j", "cd b && make -j && echo ok", "ninja", "ninja -C build", "make -j 0"])
def test_a_worker_shell_build_with_unlimited_jobs_is_refused_suggesting_the_share_or_ttp_clip(monkeypatch, cmd):
    from ttp import hook
    _build_share(monkeypatch, 4)
    why = hook.too_many_build_jobs(cmd)
    assert why and "unlimited" in why and "-j 4" in why and "ttp clip" in why, why


# A job count the shell fills in: each form, then what clip makes of it (no leftover expansion word)
_SHELL_JOBS = [("make -j$(nproc)", "make -j 4"), ('make -j"$(nproc)"', "make -j 4"), ("make -j$N", "make -j 4"),
               ("make -j`nproc`", "make -j 4"), ("make -j $(nproc)", "make -j 4"), ('make -j "$N" all', "make -j 4 all"),
               ("make --jobs=$(nproc)", "make -j 4"), ("make --jobs $N", "make -j 4"),
               ("make --jobs=`nproc` V=1", "make -j 4 V=1"), ("cmake --build . --parallel $(nproc)", "cmake --build . -j 4"),
               ("cmake --build b --parallel=$N", "cmake --build b -j 4"), ("ninja -j $N", "ninja -j 4"),
               ("ninja -j$(nproc) -C b", "ninja -j 4 -C b"), ("cd b && make -j$(( $(nproc) / 2 )) && echo ok",
                                                              "cd b && make -j 4 && echo ok")]
# ninja calls that run a tool or only print: no build, so no missing -j
_NINJA_INFO = ["ninja -t clean", "ninja -C b -t compdb", "ninja -tclean", "ninja --version", "ninja -h",
               "ninja --help", "ninja -n", "ninja -C build -n all"]


@pytest.mark.parametrize("cmd,_", _SHELL_JOBS)
def test_a_worker_shell_build_with_a_job_count_the_shell_fills_in_is_refused_naming_the_share(monkeypatch, cmd, _):
    from ttp import hook
    _build_share(monkeypatch, 4)
    why = hook.too_many_build_jobs(cmd)
    assert why and "fills in" in why and "build share of 4" in why and "-j 4" in why and "ttp clip" in why, why


@pytest.mark.parametrize("cmd,capped", _SHELL_JOBS)
def test_clip_replaces_a_job_count_the_shell_fills_in_with_its_grant_flag_and_value(cmd, capped):
    from ttp import testslots
    assert testslots.clamp_build(cmd, 4) == capped


@pytest.mark.parametrize("cmd", _NINJA_INFO)
def test_ninja_running_a_tool_or_only_printing_is_allowed_and_left_as_it_is(monkeypatch, cmd):
    from ttp import hook, testslots
    _build_share(monkeypatch, 4)
    assert hook.too_many_build_jobs(cmd) is None, cmd
    assert testslots.clamp_build(cmd, 4) == cmd


def test_a_build_within_its_share_through_clip_or_checks_or_outside_a_worker_is_allowed(monkeypatch, tmp_path):
    from ttp import hook
    _build_share(monkeypatch, 4)
    for cmd in ("make -j 4", "make -j2", "ninja -j 3", "cmake --build . --parallel 4", "make", "make -C build all",
                "cmake --build .", "ttp clip -- make -j 64", "ttp clip -- ninja",
                "cd b && ttp clip -- cmake --build . -j 32", "ttp checks -- 'make -j 64'", "pip install ninja",
                "git grep 'make -j 64'", "sort -j 99 f"):
        assert hook.too_many_build_jobs(cmd) is None, cmd
    monkeypatch.setenv("TTP_BUILD_SLOTS_HELD", "4")
    assert hook.too_many_build_jobs("make -j 64") is None, "under a held build grant"
    monkeypatch.delenv("TTP_BUILD_SLOTS_HELD")
    monkeypatch.setenv("TTP_TEST_SLOTS_HELD", "3")
    assert hook.too_many_build_jobs("ninja") is None, "inside a check holding its test grant"
    monkeypatch.delenv("TTP_TEST_SLOTS_HELD")
    monkeypatch.setenv("MAKEFLAGS", "-j8")
    assert hook.too_many_build_jobs("make -j 64") is None, "the variables disagree: not a worker's build share"
    for v in ("CMAKE_BUILD_PARALLEL_LEVEL", "MAKEFLAGS"):
        monkeypatch.delenv(v)
    assert hook.too_many_build_jobs("make -j 64") is None and hook.too_many_build_jobs("ninja") is None, "no share"
    _build_share(monkeypatch, 4)
    payload = {"tool_name": "Bash", "tool_input": {"command": "cd build && make -j 64"}, "cwd": str(tmp_path)}
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    out, _ = hook.pre_tool_use(payload)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "ttp clip" in out["hookSpecificOutput"]["permissionDecisionReason"]
    monkeypatch.delenv("TTP_RUN_DIR")
    assert hook.pre_tool_use(payload) == (None, None), "the user's own shells are never refused"


# Host-wide build slots (testslots.BUILDS) ----------------------------------------------------------
_FAKE_MAKE = f"""#!/bin/bash
echo "args=$* MAKEFLAGS=$MAKEFLAGS CMAKE=$CMAKE_BUILD_PARALLEL_LEVEL" > "$MAKE_SEEN"
echo $$ > "$MAKE_SEEN.pid"
while [ -n "$MAKE_STOP" ] && [ ! -e "$MAKE_STOP" ]; do {_nap()}; done
"""


def _fake_make(tmp_path, monkeypatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    (bindir / "make").write_text(_FAKE_MAKE)
    (bindir / "make").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("MAKE_SEEN", str(tmp_path / "make.seen"))
    monkeypatch.setenv("TTP_BUILD_POOL", "6")
    monkeypatch.setenv("TTP_BUILD_RUN_MAX", "3")
    return tmp_path / "make.seen"


def _clip(cmd: list) -> int:
    from ttp import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["clip", "--", *cmd])
    return e.value.code


def test_workers_environment_carries_the_build_cap_of_the_build_pool(env, monkeypatch):
    from ttp import cli, testslots
    p = make(env)
    monkeypatch.setenv("TTP_BUILD_POOL", "10")
    monkeypatch.setenv("TTP_BUILD_RUN_MAX", "5")
    _, run_env = _worker_env(p, monkeypatch)
    assert run_env["MAKEFLAGS"] == "-j5" and run_env["CMAKE_BUILD_PARALLEL_LEVEL"] == "5", run_env
    assert testslots.worker_env({"checks": {"build_run_max": 2}})["MAKEFLAGS"] == "-j2", "a project only lowers it"
    assert testslots.makeflags(5, "-j2 -k") == "-j2 -k", "a lower -j and other flags stay"
    assert testslots.makeflags(5, "-j64 --no-print-directory") == "-j5 --no-print-directory"
    assert testslots.makeflags(5, "-j") == "-j5", "a bare -j (unlimited) is capped"
    monkeypatch.delenv("TTP_BUILD_POOL")
    cli.main(["config", "--account", "checks.build_pool", "0"])
    assert "MAKEFLAGS" not in testslots.worker_env({}), "build_pool 0 turns the build cap off"
    assert testslots.SLOTS_ENV in testslots.worker_env({}), "the test pool stays on"


def test_build_job_counts_above_the_grant_are_clamped_and_others_kept_word_by_word(env):
    from ttp import testslots
    c = testslots.clamp_build
    assert c("make -j 64", 4) == "make -j 4" and c("make -j64 all", 4) == "make -j4 all"
    assert c("cd b && /usr/bin/make --jobs=64 V=1", 4) == "cd b && /usr/bin/make --jobs=4 V=1"
    assert c("make -j", 4) == "make -j 4" and c("make -j all", 4) == "make -j 4 all", "bare -j is unlimited"
    assert c("cmake --build build -j 64 -- -j 99", 4) == "cmake --build build -j 4 -- -j 4"
    assert c("cmake --build build --parallel", 4) == "cmake --build build --parallel 4"
    assert c("ninja -j0 -C b", 4) == "ninja -j4 -C b" and c("ninja -C b", 4) == "ninja -j4 -C b"
    assert c('make "-j" 64', 4) == 'make "-j" 4' and c("make '-j64'", 4) == "make -j4", "quoting is no way around it"
    assert c("echo $(make -j 64) # make -j 99", 4) == "echo $(make -j 4) # make -j 99"
    for kept in ("make -j 4", "make -j2 all", "ninja -j 3", "cmake --build b --parallel 1", "cmake -S . -B b -j 64",
                 "pip install ninja", "git commit -m 'make -j64'", "make -k 'x; -j 64'", "sort -j 64"):
        assert c(kept, 4) == kept, kept
    assert testslots.builds("timeout 600 nice -n 5 make") and testslots.builds("CC=gcc cmake --build b")
    assert not testslots.builds("pip install ninja") and not testslots.builds("echo make; cmake -B b")


def test_ttp_clip_caps_a_build_at_its_build_slot_grant_and_keeps_a_lower_j(env, tmp_path, monkeypatch, capsys):
    from ttp import locks, testslots
    seen = _fake_make(tmp_path, monkeypatch)
    assert _clip(["make", "-j", "64", "all"]) == 0
    assert seen.read_text().strip() == "args=-j 3 all MAKEFLAGS=-j3 CMAKE=3"
    out = capsys.readouterr().out
    assert "build slots: 3 granted of a host pool of 6" in out and "capped at -j 3" in out
    assert not locks.held_labels(testslots.paths(6, testslots.BUILDS)), "released once the build ends"
    assert _clip(["make", "-j", "2"]) == 0
    assert seen.read_text().strip() == "args=-j 2 MAKEFLAGS=-j3 CMAKE=3", "a -j at or below the grant is kept"
    assert "capped" not in capsys.readouterr().out
    assert _clip(["sh", "-c", "make -j 3"]) == 0 and seen.read_text().startswith("args=-j 3 "), "kept"
    monkeypatch.setenv(testslots.HELD_ENV, "2")   # inside a check: its test grant, no build slots taken
    assert _clip(["make", "-j", "64"]) == 0 and seen.read_text().startswith("args=-j 2 MAKEFLAGS=-j2")
    assert not locks.held_labels(testslots.paths(6, testslots.BUILDS))


def test_build_slots_of_a_killed_ttp_clip_are_released(env, tmp_path, monkeypatch):
    import signal
    from ttp import locks, testslots
    seen = _fake_make(tmp_path, monkeypatch)
    stop = tmp_path / "make.stop"
    monkeypatch.setenv("MAKE_STOP", str(stop))
    proc = subprocess.Popen([sys.executable, "-m", "ttp", "clip", "--", "make"],
                            env={**os.environ, "PYTHONPATH": str(RUNTIME)}, stdout=subprocess.DEVNULL)
    pid_file = seen.with_name(seen.name + ".pid")
    try:
        assert _wait_for(lambda: pid_file.exists() and pid_file.read_text().strip())
        assert len(locks.held_labels(testslots.paths(6, testslots.BUILDS))) == 3
        assert "host build slots: 3 of 6 in use (at most 3 per run)" in testslots.use_lines()
        proc.kill()   # SIGKILL: no clean-up runs, the kernel drops its locks
        proc.wait(10)
        assert _wait_for(lambda: not locks.held_labels(testslots.paths(6, testslots.BUILDS)), 10), \
            "a killed holder's build slots are free again, even while its build still runs"
        g = testslots.acquire("after", wait_s=5, kind=testslots.BUILDS)
        assert g is not None and g.k == 3
        g.close()
    finally:
        stop.write_text("")
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)
        if pid_file.exists() and (pid := pid_file.read_text().strip()):
            try:
                os.kill(int(pid), signal.SIGKILL)   # the fake make this test started
            except ProcessLookupError:
                pass


def test_web_feed_shows_the_host_test_and_build_slot_use_ttp_status_prints(env, monkeypatch):
    from ttp import testslots
    from ttp.web import state_payload
    p = make(env)
    for var, n in (("TTP_TEST_POOL", "6"), ("TTP_TEST_RUN_MAX", "3"), ("TTP_BUILD_POOL", "4"), ("TTP_BUILD_RUN_MAX", "2")):
        monkeypatch.setenv(var, n)
    assert not [m for m in state_payload(p, p.db)["feed"] if m["kind"] == "slots"], "idle: no row"
    tests = testslots.acquire("checks", wait_s=0, project_name="")
    builds = testslots.acquire("build", wait_s=0, project_name="", kind=testslots.BUILDS)
    try:
        rows = [m for m in state_payload(p, p.db)["feed"] if m["kind"] == "slots"]
        assert [m["text"] for m in rows] == ["host test slots: 3 of 6 in use (at most 3 per run)",
                                             "host build slots: 2 of 4 in use (at most 2 per run)"]
        assert all(m["state"] == "status" and m["severity"] == "low" for m in rows), "the feed, never the top"
        assert not [a for a in state_payload(p, p.db)["attention"] if "slots" in json.dumps(a)]
    finally:
        tests.close()
        builds.close()


def _fake_proc(root, forks: int, procs: dict):
    """A /proc with `processes forks` in stat and pid -> (cgroup unit or None, cmdline[, parent pid])."""
    import shutil
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    (root / "stat").write_text(f"cpu  1 2 3\nprocesses {forks}\nprocs_running 1\n")
    for pid, (unit, cmd, *pp) in procs.items():
        d = root / str(pid)
        d.mkdir()
        if pp:
            (d / "stat").write_text(f"{pid} (some (odd) name) S {pp[0]} 1 1 0\n")
        (d / "cgroup").write_text(f"0::/user.slice/app.slice/{unit}.service\n" if unit else "0::/init.scope\n")
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode())


def test_churn_watch_names_the_owning_project_only_when_the_fork_rate_stays_high(env, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ttp import churnwatch, daemon, service
    monkeypatch.setenv("TTP_CHURN_FORKS_S", "100")
    monkeypatch.setenv("TTP_CHURN_WINDOW_S", "60")
    root = tmp_path / "proc"
    xdist = "python -c import sys;exec(eval(sys.stdin.readline()))"
    w = churnwatch.Watch(root)
    _fake_proc(root, 1000, {1: (None, "init")})
    assert w.step(0) is None
    _fake_proc(root, 1000 + 30 * 500, {1: (None, "init")})
    assert w.step(30) is None, "a high rate starts an episode, nothing yet"
    _fake_proc(root, 1000 + 60 * 50, {1: (None, "init")})
    assert w.step(60) is None and w.since is None, "a short burst wakes nobody"
    n, pid = 1000 + 60 * 50, 100
    for t in (90, 120, 150, 180):
        n += 30 * 900
        procs = {1: (None, "init")}
        for _ in range(5):
            pid += 1
            procs[pid] = ("tt-project-alpha", xdist)
        procs[pid + 1000] = ("tt-project-beta", "sh -c true")
        _fake_proc(root, n, procs)
        obs = w.step(t)
        if t < 120:
            assert obs is None, "not sustained for the window yet"
        elif t == 120:
            assert obs and obs["key"] == "process-churn:tt-project-alpha" and obs["severity"] == "normal"
            assert obs["forks_s"] == 900 and obs["xdist_workers"] == 5 and "tt-project-alpha.service" in obs["text"]
        else:
            assert obs is None, "one observation per episode"
    assert churnwatch.Watch(tmp_path / "no-proc").step(0) is None, "no /proc (macOS): nothing"
    # The daemon raises it only in the owning project's daemon; the watch never signals anything.
    seen = []
    for name in ("alpha", "beta"):
        d = SimpleNamespace(p=SimpleNamespace(name=name, logs=tmp_path / name), observe=lambda *a, **k: seen.append((a, k)))
        d._churn = SimpleNamespace(step=lambda now: obs_alpha)
        obs_alpha = {"unit": service.unit_name(SimpleNamespace(name="alpha")), "text": "x", "severity": "normal",
                     "key": "process-churn:tt-project-alpha"}
        daemon.Daemon.check_churn(d)
    assert len(seen) == 1 and seen[0][1]["key"] == "process-churn:tt-project-alpha"
    assert (tmp_path / "alpha" / "churn.jsonl").exists() and not (tmp_path / "beta").exists(), "only the owner logs it"
    import pathlib
    src = pathlib.Path(churnwatch.__file__).read_text()
    assert "os.kill(" not in src and "import signal" not in src, "read-only: it signals nothing"


def _churn_episode(root, w, parent_cmd="bash run-tests.sh --token s3cr3t"):
    """Drive w through a churn episode: a baseline, then new processes of alpha and beta under parents
    50 (parent_cmd), 60 (gone before it is read) and 70 (a make). Returns the observation."""
    procs = {1: (None, "init"), 50: (None, parent_cmd), 70: (None, "make -j8 all")}
    _fake_proc(root, 1000, procs)
    w.step(0)
    obs, n, pid = None, 1000, 100
    for t in (30, 60, 90):
        n += 30 * 900
        for _ in range(4):
            pid += 1
            procs[pid] = ("tt-project-alpha", "python -c pass", 50)
        procs[pid + 500] = ("tt-project-beta", "sh -c true", 70)
        procs[pid + 600] = (None, "true", 60)
        _fake_proc(root, n, procs)
        obs = w.step(t) or obs
    return obs


def test_churn_event_logs_rate_threshold_units_and_redacted_top_parents(env, tmp_path, monkeypatch):
    import json
    from ttp import churnwatch
    monkeypatch.setenv("TTP_CHURN_FORKS_S", "100")
    monkeypatch.setenv("TTP_CHURN_WINDOW_S", "60")
    root = tmp_path / "proc"
    obs = _churn_episode(root, churnwatch.Watch(root))
    assert obs and obs["unit"] == "tt-project-alpha"
    assert obs["units"] == {"tt-project-alpha": 4, "tt-project-beta": 1}, "new processes per unit, not only the owner"
    assert obs["parents"] == [{"cmd": "bash run-tests.sh --token ***", "count": 4},
                              {"cmd": "make -j8 all", "count": 1}], "top parents by count; a gone parent is skipped"
    f, cache = tmp_path / "logs" / churnwatch.FILE, {}
    assert churnwatch.record(f, obs, 1_800_000_000, cache, print)
    line = json.loads(f.read_text())
    assert set(line) >= {"ts", "rate", "threshold", "units", "parents"}
    assert line["rate"] == 900 and line["threshold"] == 100 and line["units"] == obs["units"]
    assert line["parents"] == obs["parents"] and "s3cr3t" not in f.read_text()
    assert re.search(r"T\d\d:\d\d:\d\d[+-]\d\d:\d\d$", line["ts"]), "ISO time with its offset"
    s = churnwatch.summary(f, days=7, now=1_800_000_000 + 3600)
    assert s["events"] == 1 and s["units"]["tt-project-alpha"] == 4 and s["parents"][0]["count"] == 4
    assert churnwatch.summary(f, days=7, now=1_800_000_000 + 8 * 86400)["events"] == 0, "older than 7 days"
    assert churnwatch.summary(tmp_path / "none.jsonl")["events"] == 0
    # The top N only, at most TOP_PARENTS.
    many = {"unit": "u", "parents": [{"cmd": f"p{i}", "count": i} for i in range(9)]}
    assert len(churnwatch.entry(many)["parents"]) == churnwatch.TOP_PARENTS


def test_churn_log_redacts_secret_looking_arguments_and_cuts_long_command_lines():
    from ttp import churnwatch
    for raw, gone, kept in (
            ("tool --token abc1 --api-key=abc2 --keep-going -v", ("abc1", "abc2"), "--keep-going -v"),
            ("x --password=abc3 --secret abc4 --key abc5", ("abc3", "abc4", "abc5"), "x --password=***"),
            ("env GITHUB_TOKEN=abc6 my_secret=abc7 DB_PASSWORD=abc8 SSH_KEY=abc9 PATH=/bin run",
             ("abc6", "abc7", "abc8", "abc9"), "PATH=/bin run"),
            ("curl -H Authorization: Bearer abcA https://h/x", ("abcA",), "https://h/x"),
            ("curl -H authorization:token abcB -H bearer abcC", ("abcB", "abcC"), "curl -H"),
            ("git clone https://me:abcD@h.example/r.git", ("abcD",), "https://me:***@h.example/r.git"),
            ("python -m pytest -k key_test", (), "python -m pytest -k key_test")):
        out = churnwatch.clean(raw)
        assert not any(g in out for g in gone) and kept in out, (raw, out)
    long = "x " + "a" * 500 + " --token zzzz"
    assert len(churnwatch.clean(long)) == churnwatch.CMD_MAX and "zzzz" not in churnwatch.clean(long)
    # Redaction runs before the cut: a secret straddling char 200 leaves no prefix of itself.
    edge = "y" * 190 + " --token SECRETVALUE"
    assert "SECR" not in churnwatch.clean(edge)
    assert churnwatch.entry({"parents": [{"cmd": "a --token q " + "b" * 300, "count": 1}]})["parents"][0]["cmd"] \
        == churnwatch.clean("a --token q " + "b" * 300)


def test_churn_log_rotates_past_its_cap_and_keeps_one_old_file(tmp_path, monkeypatch):
    from ttp import churnwatch
    monkeypatch.setattr(churnwatch, "MAX_BYTES", 600)
    f, obs = tmp_path / "churn.jsonl", {"unit": "u", "rate": 900, "threshold": 800, "units": {"u": 3},
                                        "parents": [{"cmd": "c" * 150, "count": 3}]}
    for i in range(12):
        assert churnwatch.record(f, obs, 1_800_000_000 + i, {}, print)
        assert f.stat().st_size <= 600
    assert (tmp_path / "churn.jsonl.1").exists() and sorted(p.name for p in tmp_path.iterdir()) == \
        ["churn.jsonl", "churn.jsonl.1"], "one old generation only"
    kept = sum(len(g.read_text().splitlines()) for g in tmp_path.iterdir())
    assert kept >= 2 and churnwatch.summary(f, now=1_800_000_100)["events"] == kept, "both files count"


def test_churn_log_write_failure_never_raises_and_warns_once(tmp_path):
    from ttp import churnwatch
    blocker = tmp_path / "logs"
    blocker.write_text("a file where the folder should be")
    told, cache, obs = [], {}, {"unit": "u", "rate": 900, "threshold": 800}
    for _ in range(3):
        assert churnwatch.record(blocker / churnwatch.FILE, obs, None, cache, told.append) is False
    assert len(told) == 1 and "churn log" in told[0]
    # A warn that itself fails does not raise either; a success re-arms the warning.
    assert churnwatch.record(blocker / churnwatch.FILE, obs, None, {}, lambda m: 1 / 0) is False
    assert churnwatch.record(tmp_path / "ok" / churnwatch.FILE, obs, None, cache, told.append) and not cache["failed"]
    assert churnwatch.record(blocker / churnwatch.FILE, {"unit": object()}, None, cache, told.append) is False
    assert len(told) == 2


def test_churn_log_has_no_line_without_churn_and_a_line_when_the_observation_is_muted(env, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ttp import churnwatch, daemon, service
    monkeypatch.setenv("TTP_CHURN_FORKS_S", "100")
    monkeypatch.setenv("TTP_CHURN_WINDOW_S", "60")
    root, logs = tmp_path / "proc", tmp_path / "logs"
    raised = []

    def muted_observe(*a, **k):   # a mute or a dedup drops it inside observe
        raised.append(k["key"])
        return None
    d = SimpleNamespace(p=SimpleNamespace(name="alpha", logs=logs), observe=muted_observe)
    d._churn = churnwatch.Watch(root)
    clock = [1000.0]
    monkeypatch.setattr(daemon.time, "time", lambda: clock[0])
    _fake_proc(root, 1000, {1: (None, "init")})
    for t in range(6):   # a calm host: a low fork rate
        clock[0] += 31
        _fake_proc(root, 1000 + t * 10, {1: (None, "init")})
        daemon.Daemon.check_churn(d)
    assert not logs.exists() and not raised, "no churn, no line"
    # Churn owned by alpha: one line, though the observation is muted.
    w = churnwatch.Watch(root)
    obs = _churn_episode(root, w)
    assert obs and obs["unit"] == service.unit_name(d.p)
    d._churn, d._churn_checked = SimpleNamespace(step=lambda now: obs), 0.0
    daemon.Daemon.check_churn(d)
    lines = (logs / churnwatch.FILE).read_text().splitlines()
    assert len(lines) == 1 and raised == ["process-churn:tt-project-alpha"]
    # A churn event owned by another project leaves no line here.
    d._churn, d._churn_checked = SimpleNamespace(step=lambda now: {**obs, "unit": "tt-project-beta"}), 0.0
    daemon.Daemon.check_churn(d)
    assert len((logs / churnwatch.FILE).read_text().splitlines()) == 1


def test_a_workers_build_cap_is_no_core_request_of_its_detached_job_but_a_command_s_own_makeflags_is(env, tmp_path):
    # The daemon gives every worker MAKEFLAGS=-j<k> (testslots.worker_env); its own environment has none.
    # A job a worker detaches inherits the cap: that is no request, else every idle wait reads as under-used.
    from ttp import testslots
    p = make(env)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    daemon = subprocess.Popen(["sleep", "60"], env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    try:
        (p.state / "daemon.pid").write_text(str(daemon.pid))
        cap = testslots.worker_env(p.config())
        assert cap.get("MAKEFLAGS"), cap
        run_env = {"TTP_HOME": str(env["home"]), "TTP_HOST": "testhost", "TTP_RUN_DIR": str(run_dir),
                   "TTP_TASK": "41", "TTP_RUN_ID": "7", **cap}
        for name, cmd, extra, want in (("idle", ["sleep", "0"], {}, {}),
                                       ("own", ["sleep", "0"], {"MAKEFLAGS": "-j16"}, {"cores": 16}),
                                       ("said", ["sh", "-c", "MAKEFLAGS=-j16 make"], {}, {"cores": 16})):
            r = _ttp_run(p, "detach", name, "--", *cmd, env={**run_env, **extra})
            assert r.returncode == 0, r.stderr
            job = [j for j in json.loads((run_dir / "detached.json").read_text()) if j["name"] == name][0]
            assert job["requested"] == want, (name, job["requested"])
    finally:
        daemon.kill()
        daemon.wait()


def _argv_with(monkeypatch, settings=None, runner=None):
    from ttp import project, testslots
    monkeypatch.setattr(testslots.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(testslots, "_cfg", lambda: {"runner": runner} if runner is not None else None)
    for k, v in (settings or {}).items():
        project.set_account_setting(k, v)
    return testslots.low_priority(["pytest"])


def test_check_priority_defaults_are_unchanged_without_settings(env, monkeypatch):
    assert _argv_with(monkeypatch) == ["nice", "-n", "19", "ionice", "-c3", "pytest"]


def test_check_priority_follows_checks_nice_and_ionice_settings(env, monkeypatch):
    got = _argv_with(monkeypatch, {"checks.nice": 19, "checks.ionice": "idle"})
    assert got == ["nice", "-n", "19", "ionice", "-c3", "pytest"]
    from ttp import project, testslots
    project.set_account_setting("checks.ionice", "best-effort:3")
    assert testslots.low_priority(["x"])[4:8] == ["-c2", "-n", "3", "x"]
    project.set_account_setting("checks.nice", 40)
    assert testslots.low_priority(["x"])[2] == "19", "clamped to 0..19"


def test_check_priority_ignores_runner_nice(env, monkeypatch):
    """A worker's runner.nice is not the checks': they stay at the lowest priority unless checks.nice says."""
    assert _argv_with(monkeypatch, runner={"nice": 15})[:3] == ["nice", "-n", "19"]
    assert _argv_with(monkeypatch, {"checks.nice": 15}, runner={"nice": 5})[:3] == ["nice", "-n", "15"]


def test_check_priority_follows_the_project_s_checks_ionice(env, monkeypatch):
    from ttp import testslots
    monkeypatch.setattr(testslots.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(testslots, "_cfg", lambda: {"checks": {"ionice": "idle"}})
    assert testslots.low_priority(["pytest"])[3:] == ["ionice", "-c3", "pytest"]
    from ttp import project
    project.set_account_setting("checks.ionice", "best-effort:3")
    assert testslots.ionice_args({"checks": {"ionice": "best-effort:1"}}) == ["-c2", "-n", "1"], "project first"
    assert testslots.ionice_args({"checks": {"ionice": "bogus"}}) == ["-c2", "-n", "3"], "invalid: the account's"
    assert testslots.ionice_args(None) == ["-c2", "-n", "3"]


def test_check_priority_line_names_the_nice_level_the_ionice_class_and_their_settings(env, monkeypatch):
    """#8107 (nice_line_logged): the low priority checks run at is said with its values and settings, here
    (a tool this machine lacks is said skipped) and on a remote host; checks.nice false says normal priority."""
    from ttp import project, testslots
    line = testslots.priority_line({}, which=lambda t: f"/usr/bin/{t}")
    assert "nice 19 (checks.nice)" in line and "ionice class idle (checks.ionice)" in line and "skipped" not in line
    assert "ionice class idle (checks.ionice), skipped: not on this machine" in testslots.priority_line(
        {}, which=lambda t: "" if t == "ionice" else f"/usr/bin/{t}")
    project.set_account_setting("checks.ionice", "best-effort:3")
    remote = testslots.priority_line({}, "check-host-a")
    assert "on check-host-a" in remote and "best-effort 3 (checks.ionice)" in remote and "where the host has it" in remote
    project.set_account_setting("checks.nice", False)
    assert testslots.priority_line({}) == "check commands run at normal priority (checks.nice false)"


def test_check_priority_invalid_values_fall_back_to_the_defaults(env, monkeypatch):
    got = _argv_with(monkeypatch, {"checks.nice": "soon", "checks.ionice": "bogus:9"}, runner={"nice": "x"})
    assert got == ["nice", "-n", "19", "ionice", "-c3", "pytest"]
    from ttp import project, testslots
    project.set_account_setting("checks.ionice", "best-effort:9")
    assert testslots.ionice_args() == ["-c3"]


def test_every_checks_key_the_runtime_reads_is_an_account_key_and_settable(env):
    import re
    from pathlib import Path
    from ttp import cli, project, testslots
    src = "".join(f.read_text() for f in Path(project.__file__).parent.glob("*.py"))
    read = set(re.findall(r'["\']checks\.([a-z_]+)["\']', src))
    # and the keys read as cfg.get("checks") or {}).get("<key>"), which name no 'checks.<key>' literal
    read |= set(re.findall(r'get\("checks"\) or \{\}\)\.get\("([a-z_]+)"', src))
    for kind in (testslots.TESTS, testslots.BUILDS):
        read |= {kind.setting(s)[1] for s in ("pool", "run_max", "project_max")}
    read -= {"dropped", "json", "log", "out", "pid", "queued", "rc", "running", "stalled", "spill_"}   # file names (checks.json) and the key prefix configwho.SPILL_KEYS, not keys
    # project-only: the project's own lock or queue wrapper (and the one its checks run behind on a spill host),
    # set by the user per project, its fallback lane, the spill hosts its user lets it share with device jobs,
    # the Python lock its checks' pinned env is built from, the strikes that quarantine a spill host for it, and a
    # project's own refusal of sudo in its suites (sudowatch)
    # and its checks' throwaway HOME (scratchhome)
    # and its own notice that its push checks run only a pytest subset (gatetime.subset_flag)
    read -= {"wrap_cmd", "spill_wrap_cmd", "wrap_lane", "fallback_lane", "spill_beside_device", "python_lock",
             "spill_quarantine_after", "refuse_sudo", "scratch_home", "home_files", "subset_flag"}
    # a chat-only alias of checks.slots (coordinator.ALIASES), not a key the runtime reads
    read -= {"suite_concurrency"}
    assert "wrap_cmd" in project._known_keys(["checks"])
    missing = read - project.ACCOUNT_KEYS["checks"]
    assert not missing, f"checks keys read but not account keys: {sorted(missing)}"
    for k in ("test_project_max", "test_pool", "test_run_max", "build_pool", "build_run_max",
              "build_project_max", "nice", "ionice", "starve_s"):
        cli.main(["config", "--account", f"checks.{k}", "idle" if k == "ionice" else "3"])
    assert testslots.project_max({"checks": {"test_project_max": 1}}) >= 1


def _suite_ticket(name: str, prio, since: float, task: str = ""):
    from ttp import checkcap, locks, testslots
    return locks.enqueue(checkcap.slot_dir(), testslots._suite_resource(name),
                         f"task #{task} x" if task else "x", since, priority=prio)


def test_suite_slot_order_is_task_priority_then_arrival_and_a_long_waiter_ages_up(env):
    from ttp import locks, testslots
    now = time.time()
    age = testslots.SUITE_AGE_S
    p3_old = _suite_ticket("pa", 3, now - 0.5 * age)   # arrived first, waited under one aging step
    p1_new = _suite_ticket("pa", 1, now - 60)
    p2_a = _suite_ticket("pa", 2, now - 50)
    p2_b = _suite_ticket("pa", 2, now - 40)
    none = _suite_ticket("pa", None, now - 30)
    other = _suite_ticket("pb", 1, now - 9000)   # another project's waiters never count
    try:
        assert testslots.suite_order("pa", now=now) == [p1_new, p2_a, p2_b, p3_old, none], \
            "p1 before an earlier p3; equal priorities by arrival; no priority ranks as p3, by its arrival"
        # No starvation among routine runs: each SUITE_AGE_S waited is one step more urgent, down to p2,
        # so a p5 waiter waits at most 3 steps plus the p1s and the runs that arrived before it.
        p5_starved = _suite_ticket("pa", 5, now - 5 * age - 1)
        p5_aging = _suite_ticket("pa", 5, now - 2 * age - 1)
        assert testslots.suite_priority(p5_starved, now) == 2 and testslots.suite_priority(p5_aging, now) == 3
        order = testslots.suite_order("pa", now=now)
        assert order[:2] == [p1_new, p5_starved], \
            "a waiter aged to p2 goes before any newer p2 (it arrived first), never before a p1 (#5282)"
        assert order.index(p5_aging) > order.index(p2_b) and order[-3:] == [p3_old, none, p5_aging], \
            "aged to p3, it goes after the p2s and, under the fairness bound, after the native p3s (#4294)"
        assert other not in order
    finally:
        for t in list(locks._TICKETS):
            locks.dequeue(t)


def test_suite_slot_frees_to_the_most_urgent_waiter_not_the_first_arrival(env, monkeypatch):
    import threading
    from ttp import checkcap, locks, testslots
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    monkeypatch.delenv(testslots.HELD_ENV, raising=False)
    monkeypatch.delenv(checkcap.HELD_ENV, raising=False)
    hold = testslots.suite_hold("pa")
    assert hold.files
    got, lock = [], threading.Lock()

    def wait(name, prio, since):
        g = testslots.suite_hold("pa", priority=prio, since=since, wait_s=30)
        with lock:
            got.append(name)
        time.sleep(0.3)   # holds it a moment: the next waiter waits for it, never pre-empts it
        g.close()

    now = time.time()
    ts = [threading.Thread(target=wait, args=a) for a in
          (("p3 early", 3, now - 600), ("p2 later", 2, now - 300), ("p1 last", 1, now - 30), ("p2 last", 2, now - 20))]
    for t in ts:
        t.start()
    q = lambda: locks.queued(checkcap.slot_dir(), testslots._suite_resource("pa"))
    assert _wait_for(lambda: len(q()) == 4)
    assert got == [], "a running holder is never pre-empted"
    hold.close()
    for t in ts:
        t.join(30)
    assert got == ["p1 last", "p2 later", "p2 last", "p3 early"], got


def test_suite_slot_ranks_an_urgent_spec_line_as_p1_ahead_of_earlier_p3s_fifo_within_a_priority(env, monkeypatch):
    # A spec's fresh `urgent:` line (user-ordered: urgent.queue_priority) ranks a task's checks as p0, ahead
    # of plain p1, via the rank the real callers pass (checkcap.task_rank); `urgent: no` does not; a running
    # holder is never stopped.
    import threading
    from ttp import checkcap, checklane, locks, testslots
    p = make(env)
    add = lambda title, spec, prio: p.db.add_task(title, spec, kind="code", tier="light", origin="coordinator",
                                                  priority=prio)
    holder = add("holder", "s", 3)
    early, later = add("p3 early", "s", 3), add("p3 later", "s", 3)
    hot = add("urgent line", "fix it\nurgent: user waits on this\n", 3)
    p1 = add("p1", "s", 1)
    not_hot = add("not urgent", "urgent: no\n", 3)
    rank = {}
    for tid in (holder, early, later, hot, p1, not_hot):
        monkeypatch.setenv("TTP_TASK", str(tid))
        rank[tid] = checkcap.task_rank(p)[0]
    assert rank == {holder: 3, early: 3, later: 3, hot: 0, p1: 1, not_hot: 3}, rank
    monkeypatch.setenv("TTP_TASK", str(add("urgent p5", "urgent: deadline", 5)))
    assert checkcap.task_rank(p) == (0, [])
    monkeypatch.setenv("TTP_TASK", str(add("urgent p1", "urgent: x", 1)))
    assert checkcap.task_rank(p) == (0, []), "a user-ordered line outranks its own priority"

    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    monkeypatch.delenv(testslots.HELD_ENV, raising=False)
    monkeypatch.delenv(checkcap.HELD_ENV, raising=False)
    monkeypatch.setenv("TTP_TASK", str(holder))
    hold = testslots.suite_hold("pa", priority=rank[holder])
    assert hold.files
    got, lock = [], threading.Lock()

    def wait(tid, since):
        g = testslots.suite_hold("pa", priority=rank[tid], since=since, wait_s=30)
        with lock:
            got.append(tid)
        time.sleep(0.3)
        g.close()

    now = time.time()
    order = ((early, now - 600), (later, now - 500), (hot, now - 30), (not_hot, now - 20), (p1, now - 10))
    ts = []
    for tid, since in order:   # each waiter's ticket names its task, as a real run's does
        monkeypatch.setenv("TTP_TASK", str(tid))
        ts.append(threading.Thread(target=wait, args=(tid, since)))
        ts[-1].start()
        assert _wait_for(lambda: len(locks.queued(checkcap.slot_dir(), testslots._suite_resource("pa"))) == len(ts))
    line, = checklane.lane_lines("pa", {}, time.time())
    assert f"#{holder} p3" in line, "the lane-wait line still names the holder"
    assert got == [], "a running holder is never pre-empted"
    hold.close()
    for t in ts:
        t.join(30)
    assert got == [hot, p1, early, later, not_hot], \
        "a user-ordered urgent line, then p1, before earlier p3s; FIFO within a priority"


def test_suite_slot_status_line_shows_the_holder_s_time_against_its_cap_and_the_waiting_order(env, monkeypatch):
    from ttp import checkcap, checklane, locks, testslots
    monkeypatch.delenv(testslots.HELD_ENV, raising=False)
    monkeypatch.delenv(checkcap.HELD_ENV, raising=False)
    assert checklane.lane_lines("pa") == [], "free and nobody waits: no line"
    monkeypatch.setenv("TTP_TASK", "12")
    monkeypatch.setattr(checklane, "children", lambda rec: 2)
    hold = testslots.suite_hold("pa", priority=2)
    now = time.time()
    try:
        assert ("#12 p2 queued 0 min, has a lane 0 min, not running yet: waits on no host queue (starting, or an "
                "older runtime), 2 child processes alive") in checklane.lane_lines("pa", {}, now)[0]
        testslots.suite_running(hold, 7200)
        _suite_ticket("pa", 3, now - 60, "31")
        _suite_ticket("pa", 4, now - 2 * testslots.SUITE_AGE_S + 60, "32")
        line, = checklane.lane_lines("pa", {"checks": {"slots": 1}}, now + 601)
        unkeyed = checklane.group_name("")   # an unkeyed lane (no repository key): no run named it
        assert line.startswith(f"checks lane {unkeyed} (1 at a time per repository; checks.slots raises it): "
                               "#12 p2 queued 10 min, running 10 min of its 2 h 0 min cap, 2 child processes alive"), line
        assert line.endswith(f"held: #32 p4 ranked p2 (aged) 39 min, waits on {unkeyed}, "
                             f"#31 p3 ranked p3 11 min, waits on {unkeyed}"), line
    finally:
        for t in list(locks._TICKETS):
            locks.dequeue(t)
        hold.close()


def test_suite_slot_aging_step_is_checks_priority_age_s_and_never_ranks_past_p2(env, monkeypatch):
    # Aging is configurable (checks.priority_age_s: project, then account, else SUITE_AGE_S), judged on a
    # test clock (`now`), never wall time; it ranks a routine waiter at most up to p2 (a native p1 always
    # goes first), and an obligation owner's 0 stays its own.
    from ttp import checklane, cli, locks, testslots
    assert testslots.age_s() == testslots.SUITE_AGE_S == 900.0, "the documented default"
    for bad in (0, -5, "soon", "", "inf", "nan", None):
        assert checklane.priority_age_s({"checks": {"priority_age_s": bad}}) == testslots.SUITE_AGE_S, bad
    assert cli.main(["config", "--account", "checks.priority_age_s", "120"]) in (0, None)
    assert testslots.age_s() == 120.0, "the account's setting"
    assert testslots.age_s({"checks": {"priority_age_s": 60}}) == 60.0, "the project's wins over the account's"
    now = 1_000_000.0
    p5 = _suite_ticket("pa", 5, now - 600)        # 10 min waited
    p1 = _suite_ticket("pa", 1, now - 60)
    p2 = _suite_ticket("pa", 2, now - 30)
    owner = _suite_ticket("pa", 0, now - 10)      # an obligation owner
    try:
        assert testslots.suite_priority(p5, now, 900) == 5, "under one 15 min step: not aged"
        assert testslots.suite_priority(p5, now, 120) == 2 and testslots.suite_priority(p5, now) == 2, \
            "5 steps of 2 min would reach 0: aging stops at p2"
        assert testslots.suite_priority(p5, now, 240) == 3, "two whole steps of 4 min"
        assert testslots.suite_priority(owner, now + 9000, 60) == 0 and testslots.suite_priority(p1, now + 9000, 60) == 1
        assert testslots.suite_order("pa", now=now, age=900) == [owner, p1, p2, p5]
        assert testslots.suite_order("pa", now=now, age=240) == [owner, p1, p2, p5], "aged to p3: still behind p2"
        assert testslots.suite_order("pa", now=now, age=60) == [owner, p1, p5, p2], \
            "aged to p2, it goes before the newer p2 by arrival, never before a p1 or an obligation owner"
        assert testslots.suite_order("pa", now=now) == [owner, p1, p5, p2], "the account's 120 s step by default"
        assert testslots.rank_text(p5, now, 900) == "p5 ranked p5" and testslots.rank_text(p5, now, 60) == "p5 ranked p2 (aged)"
    finally:
        for t in list(locks._TICKETS):
            locks.dequeue(t)


def test_suite_slot_native_priority_breaks_an_aged_tie_until_the_fairness_bound(env):
    # #4294: among equal ranks the native priority goes first, and a waiter past FAIR_STEPS aging steps
    # (1 h by default) goes by arrival among its rank so a stream of p2s never starves it. #5282: aging
    # stops at p2, so a native p1 goes before every routine waiter however long it waited (the lane
    # turned first-come, first-served once routine waits passed an hour).
    from ttp import locks, testslots
    age = testslots.SUITE_AGE_S
    assert testslots.FAIR_STEPS * age == 3600, "the documented 1 h default"
    now = 1_000_000.0
    aged_p3 = _suite_ticket("pa", 3, now - 38 * 60)       # aged two steps, held at p2
    p1 = _suite_ticket("pa", 1, now - 35 * 60)
    p2 = _suite_ticket("pa", 2, now - 10 * 60)            # under one step: stays p2
    owner = _suite_ticket("pa", 0, now - 60)
    try:
        assert testslots.suite_priority(aged_p3, now, age) == 2 == testslots.suite_priority(p2, now, age)
        assert testslots.suite_order("pa", now=now, age=age) == [owner, p1, p2, aged_p3], \
            "an obligation owner first; then the p1; the native p2 before the earlier p3 aged to p2"
        # Past the bound: arrival decides among its rank, before a newer native p2, never before the p1.
        assert testslots.suite_order("pa", now=now + 23 * 60, age=age) == [owner, p1, aged_p3, p2], \
            "waited 61 min: by arrival among the p2s"
        assert testslots.suite_order("pa", now=now + 21 * 60, age=age) == [owner, p1, p2, aged_p3], "59 min: not yet"
        late_p1 = _suite_ticket("pa", 1, now + 22 * 60)   # a p1 arriving after the aged p3 passed the bound
        assert testslots.suite_order("pa", now=now + 30 * 60, age=age) == [owner, p1, late_p1, aged_p3, p2]
        # The bound scales with checks.priority_age_s: four 5-min steps.
        assert testslots.suite_order("pa", now=now, age=300) == [owner, p1, late_p1, aged_p3, p2], \
            "the p3 past 20 min goes by arrival among the p2s, behind both p1s"
    finally:
        for t in list(locks._TICKETS):
            locks.dequeue(t)


def test_suite_slot_p1_queued_after_hours_of_routine_waiters_starts_first_and_the_lane_line_says_so(env):
    # #5282 (seen live): 19 waiters under a cap of 2, routine runs waiting hours; a p1 queued last sat at
    # position 8 behind p2s, because aging ranked them all p1 and the fairness bound then went by arrival.
    # The order the lanes grant (suite_order) and the position the wait report prints (checklane.waits)
    # are one list: a new p1 is first in both, routine runs keep arrival order within their rank.
    from ttp import checklane, locks, testslots
    now = time.time()
    routine = [_suite_ticket("pa", prio, now - (5 - i) * 3600, str(40 + i))
               for i, prio in enumerate((3, 2, 3, 2, 4))]
    p1 = _suite_ticket("pa", 1, now - 60, "50")
    try:
        order = testslots.suite_order("pa", now=now)
        assert order[0] == p1, order
        assert order[1:] == routine, "every routine waiter is past the fairness bound at p2: by arrival"
        w = checklane.waits("pa", {}, now)
        assert [x["task"] for x in w] == ["50", "40", "41", "42", "43", "44"], w
        assert w[0]["position"] == 1 and w[0]["of"] == 6
    finally:
        for t in list(locks._TICKETS):
            locks.dequeue(t)


def test_suite_slot_check_without_a_task_or_priority_ranks_as_p3_and_a_task_s_record_sets_it(env, monkeypatch):
    from ttp import checkcap, locks, testslots
    p = make(env)
    urgent = p.db.add_task("urgent", "s", kind="code", tier="light", origin="coordinator", priority=1)
    routine = p.db.add_task("routine", "s", kind="code", tier="light", origin="coordinator", priority=4)
    monkeypatch.delenv("TTP_TASK", raising=False)
    assert checkcap.task_rank(p) == (None, []), "no task: no priority"
    monkeypatch.setenv("TTP_TASK", "999999")
    assert checkcap.task_rank(p) == (None, []), "a task the project has no record of: no priority"
    monkeypatch.setenv("TTP_TASK", str(urgent))
    assert checkcap.task_rank(p) == (1, []), "read from the task's record"
    monkeypatch.setenv("TTP_TASK", str(routine))
    assert checkcap.task_rank(p) == (4, [])
    now = 1_000_000.0
    none_early = _suite_ticket("pa", None, now - 300)
    p3 = _suite_ticket("pa", 3, now - 200)
    p2 = _suite_ticket("pa", 2, now - 100)
    p4 = _suite_ticket("pa", 4, now - 400)
    none_late = _suite_ticket("pa", None, now - 50)
    try:
        assert testslots.suite_priority(none_early, now, 900) == testslots.NO_PRIORITY == 3
        assert testslots.suite_order("pa", now=now, age=900) == [p2, none_early, p3, none_late, p4], \
            "no priority ranks as p3: after p2, among the p3s by arrival, before p4"
        assert testslots.rank_text(none_early, now, 900) == "p3 (no task priority) ranked p3"
    finally:
        for t in list(locks._TICKETS):
            locks.dequeue(t)


def test_suite_slot_wait_line_of_ttp_checks_names_this_run_s_priority_and_rank(env, monkeypatch):
    import threading
    from ttp import locks, testslots
    _lane_env(monkeypatch)
    hold = testslots.suite_hold("pa")
    told = []
    try:
        t = threading.Thread(target=lambda: testslots.suite_hold("pa", tell=told.append, priority=4,
                                                                 since=time.time() - 130, wait_s=0.3, age=60))
        t.start()
        t.join(10)
        line = next(x for x in told if x.startswith("waiting for this project's other full checks"))
        assert "most urgent priority first; this run p4 ranked p2 (aged), position 1 of 1" in line, line
    finally:
        hold.close()
        for t in list(locks._TICKETS):
            locks.dequeue(t)


_SUITE_RUNNER = """
import json, os, sys, time
from pathlib import Path
from ttp import checkcap, locks, testslots
if sys.argv[3] == "hold":
    g = testslots.suite_hold(sys.argv[1])
    testslots.suite_running(g, 3600)
else:
    locks.enqueue(checkcap.slot_dir(), testslots._suite_resource(sys.argv[1]), "task #5 w", priority=1)
Path(sys.argv[2]).write_text("ready")
while True:
    time.sleep(1)
"""


def test_suite_slot_of_a_reboot_cut_holder_and_waiter_is_free_at_once(env, tmp_path, monkeypatch):
    # A reboot (here: both processes killed outright) leaves their label and ticket files behind;
    # neither holds anything, so the status line shows none and the next run takes the slot at once.
    from ttp import checkcap, checklane, testslots
    monkeypatch.delenv(testslots.HELD_ENV, raising=False)
    monkeypatch.delenv(checkcap.HELD_ENV, raising=False)
    procs = []
    for role in ("hold", "wait"):
        ready = tmp_path / role
        procs.append(subprocess.Popen([sys.executable, "-c", _SUITE_RUNNER, "pa", str(ready), role],
                                      env={**os.environ, "PYTHONPATH": str(RUNTIME)}))
        assert _wait_for(ready.exists)
    try:
        line = "\n".join(checklane.lane_lines("pa"))
        assert "running 0 min of its 1 h 0 min cap" in line and "#5 p1" in line, line
    finally:
        for proc in procs:
            proc.kill()
            proc.wait(10)
    assert testslots.suite_path("pa").read_text(), "the holder's label stays on disk"
    assert checklane.lane_lines("pa") == [], "a dead holder and waiter show nothing"
    t0 = time.time()
    again = testslots.suite_hold("pa", priority=4, wait_s=5)
    assert again is not None and again.files and time.time() - t0 < 2, "taken at once, not behind the dead p1"
    again.close()


def test_checks_wall_cap_default_follows_recent_runs_within_its_floor_and_ceiling(env, monkeypatch):
    from ttp import checkwall
    monkeypatch.delenv(checkwall.ENV, raising=False)
    p = make(env)
    assert checkwall.cap(p)[0] == checkwall.DEFAULT_S, "on with no setup before any timed run"
    for s in (100, 200, 300):
        checkwall.record_run(p.state, s, True)
    assert checkwall.cap(p)[0] == checkwall.FLOOR_S, "3 x 5 min is under the floor"
    for s in (1800, 2400, 1200):
        checkwall.record_run(p.state, s, False)
    assert checkwall.cap(p)[0] == 3 * 2400, "three times the longest recent run"
    checkwall.record_run(p.state, 4 * 3600, True)
    assert checkwall.cap(p)[0] == checkwall.CEILING_S
    p.set_config("checks.wall_cap_s", 90)
    assert checkwall.cap(p) == (90, "checks.wall_cap_s")
    p.set_config("checks.wall_cap_s", 0)
    assert checkwall.cap(p)[0] == 0, "0 turns it off"


def test_ttp_checks_past_the_wall_cap_is_stopped_releases_the_slot_and_records_a_timeout(env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, checkwall, cli, locks, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TASK", "7")
    p.set_config("checks.wall_cap_s", 2)
    child = tmp_path / "child.pid"
    stuck = ["sh", "-c", f"sleep 300 & echo $! > {child}; wait"]
    t0 = time.time()
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", *stuck])
    assert e.value.code == 1 and time.time() - t0 < 60
    err = capsys.readouterr().err
    assert "stopped" in err and "wall-time cap of 0 min (checks.wall_cap_s): a timeout, not a pass" in err, err
    pid = int(child.read_text())
    def ended():   # gone, a zombie, or its pid reused by another process (a sandbox's PID namespace)
        try:
            stat = open(f"/proc/{pid}/stat").read()
        except OSError:
            return True
        return stat.split()[2] == "Z" or "(sleep)" not in stat
    assert _wait_for(ended, 10), "what the stuck check started is ended"
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["timed_out"]["cap_s"] == 2, rec
    assert json.loads((run / "check_failure.json").read_text())["timed_out"]["how"] == "checks.wall_cap_s"
    assert not locks.held_labels([testslots.suite_path(p.name)]) and checkcap.line() == "", "the slot is free"
    assert checkwall.last_timeout(p.state, "7")["head"] == rec["head"]
    assert not checkwall.recent(p.state), "a timed-out run is no sample for the default cap"
    p.set_config("checks.wall_cap_s", 0)
    cli.main(["checks", "--", "true"])
    assert "this task's last checks run (run 41" in capsys.readouterr().out, "its next try says so"
    assert len(checkwall.recent(p.state)) == 1


def test_a_completed_checks_run_stops_reporting_the_task_s_old_timeout(env, tmp_path, monkeypatch, capsys):
    from ttp import checkwall, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TASK", "7")
    p.set_config("checks.wall_cap_s", 2)
    with pytest.raises(SystemExit):
        cli.main(["checks", "--", "sleep", "300"])   # times out at the cap and records it
    assert checkwall.last_timeout(p.state, "7") is not None, "the timeout is recorded"
    p.set_config("checks.wall_cap_s", 0)
    cli.main(["checks", "--", "true"])   # a later run completes: the TIMEOUTS entry is dropped
    capsys.readouterr()
    cli.main(["checks", "--", "true"])   # a further run prints no old-timeout line
    out = capsys.readouterr().out
    assert "was stopped at the wall-time cap" not in out, out
    assert checkwall.last_timeout(p.state, "7") is None


class _SilenceProc:
    """A stand-in check process for checkwall.wait: each wait() is one poll, writing to `out` when it
    `prints`, exiting 0 at poll `exit_at` (None: never; never with no timeout either)."""
    args = "stand-in"

    def __init__(self, out, prints=False, exit_at=None):
        self.out, self.prints, self.exit_at, self.polls = out, prints, exit_at, 0

    def wait(self, timeout=None):
        while True:   # no timeout: blocks to its exit, as Popen.wait does
            self.polls += 1
            if self.prints:
                self.out.write("line\n")
                self.out.flush()
            if self.exit_at is not None and self.polls >= self.exit_at:
                return 0
            if timeout is not None:
                raise subprocess.TimeoutExpired(self.args, timeout)


def test_checks_watchdog_stops_a_silent_command_at_its_limit_on_a_test_clock(tmp_path, monkeypatch):
    from ttp import checkwall, push
    now = [0.0]
    def clock():   # a test clock: each read is a minute later, so no real 10-minute wait
        now[0] += 60.0
        return now[0]
    monkeypatch.setattr(checkwall, "clock", clock)
    out = open(tmp_path / "checks.out", "a")
    try:
        assert checkwall.silent_limit("pytest -q") == checkwall.SILENT_S == 600, "on by default: 10 min"
        silent = _SilenceProc(out)
        with pytest.raises(checkwall.Silent) as e:
            checkwall.wait(silent, out, None, checkwall.SILENT_S)
        assert e.value.timeout == 600 and 600 <= now[0] - 60 <= 720, ("stopped at the limit, not before", now)
        now[0] = 0.0
        busy = _SilenceProc(out, prints=True, exit_at=50)   # 50 test minutes of steady output
        assert checkwall.wait(busy, out, None, 600) == 0 and busy.polls == 50, "a printing command is never stopped"
        now[0] = 0.0
        late = _SilenceProc(out)   # goes quiet after printing for a while: its silence counts from its last line
        orig = late.wait
        def wait(timeout=None):
            late.prints = late.polls < 20
            return orig(timeout)
        late.wait = wait
        with pytest.raises(checkwall.Silent):
            checkwall.wait(late, out, None, 600)
        assert late.polls >= 30, late.polls
        # The per-check limit, and 0 turns it off.
        own = push._check({"run": "make slow", "silent_s": 1800})
        assert checkwall.silent_limit(own) == 1800 and own.config() == {"run": "make slow", "silent_s": 1800}
        now[0] = 0.0
        with pytest.raises(checkwall.Silent):
            checkwall.wait(_SilenceProc(out), out, None, checkwall.silent_limit(own))
        assert now[0] - 60 >= 1800, "the check's own longer limit holds"
        off = push._check({"run": "make slow", "silent_s": 0})
        assert checkwall.silent_limit(off) == 0
        assert checkwall.wait(_SilenceProc(out, exit_at=100), out, None, 0) == 0, "0: never stopped for silence"
        # The wall-time cap still ends it as before (plain TimeoutExpired, not a silence stop).
        now[0] = 0.0
        with pytest.raises(subprocess.TimeoutExpired) as e:
            checkwall.wait(_SilenceProc(out, prints=True), out, 300, 600)
        assert not isinstance(e.value, checkwall.Silent)
        # No output file to watch: watches nothing (a plain wait).
        assert checkwall.wait(_SilenceProc(None, exit_at=1), None, None, 600) == 0
    finally:
        out.close()
    for bad in (-1, "600", True, float("nan"), float("inf")):
        assert "silent_s must be" in push._form_problem({"run": "true", "silent_s": bad}), bad
    assert push._form_problem({"run": "true", "silent_s": 0}) is None


class _ProgressProc:
    """A stand-in pytest run for checkwall.wait: each wait() is one poll, printing a `[ NN%]` line with
    `pct(poll)` (None: prints nothing), exiting 0 once it prints 100% (or at poll `exit_at`)."""
    args = "stand-in"

    def __init__(self, out, pct, exit_at=None):
        self.out, self.pct, self.exit_at, self.polls = out, pct, exit_at, 0

    def wait(self, timeout=None):
        self.polls += 1
        if (n := self.pct(self.polls)) is not None:
            self.out.write(f"...........  [{int(n):3d}%]\n")
            self.out.flush()
        if (n is not None and n >= 100) or (self.exit_at is not None and self.polls >= self.exit_at):
            return 0
        raise subprocess.TimeoutExpired(self.args, timeout)


def _test_clock(monkeypatch, step=10.0):
    from ttp import checkwall
    now = [0.0]
    def clock():   # each read is `step` seconds later: two reads per poll
        now[0] += step
        return now[0]
    monkeypatch.setattr(checkwall, "clock", clock)
    return now


def test_wall_cap_extends_a_run_still_progressing_and_stops_a_silent_or_stalled_one_never_past_the_ceiling(
        tmp_path, monkeypatch):
    from ttp import checkwall
    now = _test_clock(monkeypatch)
    log = tmp_path / "checks.log"
    with open(log, "a") as out:
        out.write("[100%] of an earlier command in the same log\n")
        out.flush()
        # 1.8% per 20 s poll: at the 1000 s cap it is at 90%, done some 2 min later; extended once, finishes.
        ext = checkwall.Extension(1000)
        assert ext.room == 500
        proc = _ProgressProc(out, lambda n: min(100, 1.8 * n))
        assert checkwall.wait(proc, out, 1000, 600, extend=ext) == 0 and now[0] > 1000
        text = log.read_text()
        assert text.count("wall-time cap extended by") == 1 and "at 90%" in text, text
        assert 0 < ext.granted <= ext.room
        # The same run with no extension room: stopped at the cap, as before.
        now[0] = 0.0
        with pytest.raises(subprocess.TimeoutExpired):
            checkwall.wait(_ProgressProc(out, lambda n: min(100, 1.8 * n)), out, 1000, 600)
        # Silence at the cap (the silence stop is off for this check, 10 min still counts): never extended.
        now[0] = 0.0
        quiet = _ProgressProc(out, lambda n: 2 * n if n <= 10 else None)
        with pytest.raises(subprocess.TimeoutExpired) as e:
            checkwall.wait(quiet, out, 1000, 0, extend=checkwall.Extension(1000))
        assert not isinstance(e.value, checkwall.Silent)
        assert "wall-time cap not extended: no output for 13 min" in log.read_text()
        # The silence stop still ends a run that went quiet after an extension.
        now[0] = 0.0
        stalls = _ProgressProc(out, lambda n: 1.8 * n if n <= 52 else None)
        with pytest.raises(checkwall.Silent):
            checkwall.wait(stalls, out, 1000, 240, extend=checkwall.Extension(1000))
        assert 1040 + 240 <= now[0] <= 1040 + 240 + 40, now
        # Printing but no longer progressing: extended while its window still rises, then stopped; the
        # extensions never pass the room.
        now[0] = 0.0
        ext = checkwall.Extension(1000)
        slow = _ProgressProc(out, lambda n: min(99, 1.8 * n))
        with pytest.raises(subprocess.TimeoutExpired):
            checkwall.wait(slow, out, 1000, 600, extend=ext)
        assert 0 < ext.granted <= ext.room and now[0] <= 1000 + ext.room + 40, (ext.granted, now)
        assert "wall-time cap not extended: at 99%" in log.read_text()
        # Stuck at one percentage over the whole window while printing: no progress, stopped at the cap.
        now[0] = 0.0
        with pytest.raises(subprocess.TimeoutExpired):
            checkwall.wait(_ProgressProc(out, lambda n: 40), out, 1000, 600, extend=checkwall.Extension(1000))
        assert "not extended: no progress over the last 15 min (at 40%)" in log.read_text()
        # Too slow to finish in the room: stopped at the cap, with the reason.
        now[0] = 0.0
        with pytest.raises(subprocess.TimeoutExpired):
            checkwall.wait(_ProgressProc(out, lambda n: 0.5 * n), out, 1000, 600, extend=checkwall.Extension(1000))
        assert "its rate needs about" in log.read_text()
        # No progress line at all: stopped at the cap.
        now[0] = 0.0
        with open(tmp_path / "plain.log", "a") as plain:
            plain_proc = _SilenceProc(plain, prints=True)
            with pytest.raises(subprocess.TimeoutExpired):
                checkwall.wait(plain_proc, plain, 1000, 600, extend=checkwall.Extension(1000))
        assert "no progress line" in (tmp_path / "plain.log").read_text()
    # The ceiling: a cap at it has no room, one near it only up to it, a large one half of itself.
    assert checkwall.Extension(checkwall.CEILING_S).room == 0 == checkwall.Extension(0).room
    near = checkwall.Extension(checkwall.CEILING_S - 600)
    assert near.room == 600
    for _ in range(50):   # steady progress, grant after grant: never past the ceiling
        near.grant([(0.0, 10), (100.0, 99)], 0.0, 600)
    assert near.granted <= near.room and near.cap_s + near.granted <= checkwall.CEILING_S
    assert checkwall.Extension(3600).room == 1800


def test_wall_cap_progress_reads_never_close_the_check_log_the_run_still_writes(tmp_path, monkeypatch):
    # The checks log is opened by descriptor (open_append: its name is a number); reading its progress at
    # the cap, extended or stopped, leaves it open for the lines the run writes after.
    from ttp import checkwall, project
    _test_clock(monkeypatch)
    log = tmp_path / "checks.log"
    with project.open_append_or_null(log) as out:
        assert isinstance(out.name, int)
        assert checkwall.wait(_ProgressProc(out, lambda n: min(100, 1.8 * n)), out, 1000, 600,
                              extend=checkwall.Extension(1000)) == 0
        with pytest.raises(subprocess.TimeoutExpired):
            checkwall.wait(_ProgressProc(out, lambda n: None), out, 1000, 600, extend=checkwall.Extension(1000))
        out.write("after the cap\n")
    text = log.read_text()
    assert "wall-time cap extended by" in text and text.endswith("after the cap\n"), text


def test_a_queue_incident_calls_a_holder_still_printing_slow_with_the_host_load_and_a_silent_one_a_likely_hang(
        tmp_path):
    """A holder past the hang factor that still prints is slow: kept in the incident's "slow", named with
    the host load in the next wake's text, and wakes nobody itself. One whose log stopped growing past its
    stall threshold (checkstall) is a likely hang; one naming no log is neither: nothing shows it working."""
    from ttp import checklane, checkstall
    now = time.time()
    printing, silent = tmp_path / "printing.log", tmp_path / "silent.log"
    printing.write_text("..... [ 80%]\n")
    silent.write_text("..... [ 40%]\n")
    os.utime(silent, (now - checkstall.STALL_MIN_S - 60,) * 2)
    busy = {"task": "21", "pid": 4242, "run_since": now - 5000, "command": "pytest -q", "log": str(printing)}
    hung = {"task": "22", "pid": 4243, "run_since": now - 5000, "command": "pytest -q", "log": str(silent)}
    nolog = {"task": "23", "pid": 4244, "run_since": now - 5000, "command": "make check"}
    cfg = {"checks": {"wait_hang_factor": 3}}
    evs = _queue_events(1, holders=[busy])
    why, inc = checklane.wait_wake(evs, {"start": 1.0}, cfg, now, 300.0)
    assert why == [] and len(inc["slow"]) == 1 and not inc["hang"], (why, inc)
    evs = _queue_events(1, holders=[busy, hung, nolog])
    why, inc = checklane.wait_wake(evs, {"start": 1.0}, cfg, now, 300.0)
    assert why == ["hang"] and len(inc["hang"]) == 1 and len(inc["slow"]) == 1, (why, inc)
    text = checklane.wake_text(why, evs, inc, cfg, now, 300.0, load=lambda: "1-minute load 92.0 on 64 cores")
    assert "its log stopped growing, its CPU idle: a likely hang" in text, text
    assert "1 holder ran over 3x" in text and "slow under load (1-minute load 92.0 on 64 cores), not hung" in text, text
    # Not over the factor: neither.
    assert checklane.wait_wake(evs, {"start": 1.0}, {"checks": {"wait_hang_factor": 100}}, now, 300.0)[0] == []


def test_ttp_checks_stops_a_check_with_no_output_as_a_timeout_and_spares_one_that_prints(env, tmp_path, monkeypatch, capsys):
    from ttp import checkwall, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TASK", "7")
    p.set_config("checks.wall_cap_s", 0)
    monkeypatch.setattr(checkwall, "SILENT_S", 1.0)
    monkeypatch.setattr(checkwall, "POLL_S", 0.05)
    cli.main(["checks", "--", "sh", "-c", "for i in 1 2 3 4 5 6 7 8; do echo $i; sleep 0.4; done"])
    assert json.loads((run / "checks.json").read_text())["passed"], "steady output for 3 s passes a 1 s limit"
    capsys.readouterr()
    t0 = time.time()
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", "sleep", "300"])
    assert e.value.code == 1 and time.time() - t0 < 60
    err = capsys.readouterr().err
    assert "after no output for 1 s (its silence limit, silent_s): a hang, a timeout, not a pass" in err, err
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["timed_out"]["silent_s"] == 1.0, rec
    assert json.loads((run / "check_failure.json").read_text())["timed_out"]["silent_s"] == 1.0
    assert "after no output for 1 s" in (run / "checks.log").read_text()
    assert checkwall.last_timeout(p.state, "7")["silent_s"] == 1.0
    # A check object's silent_s overrides the default; 0 turns the stop off.
    p.set_config("delivery.push_checks", [{"run": "sleep 2", "silent_s": 0}])
    cli.main(["checks"])
    out = capsys.readouterr().out
    assert "was stopped after no output for 1 s: a timeout, not a pass" in out, "the next run says why the last one stopped"
    assert json.loads((run / "checks.json").read_text())["passed"], "silent_s 0: a silent check runs to its end"
    assert "no output for 10 min" in checkwall.said_silent("pytest", 600, "abc")


def test_a_check_waiting_for_a_slot_survives_its_silence_limit_while_it_says_so_and_a_silent_one_is_stopped(
        env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, checklane, checkwall, cli, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TASK", "7")
    p.set_config("checks.wall_cap_s", 0)
    monkeypatch.setattr(checkwall, "SILENT_S", 2.0)
    monkeypatch.setattr(checkwall, "POLL_S", 0.05)
    # A wrapper that queues 6 s (three silence limits) for its slot, saying so every 0.2 s (slack for a
    # loaded host), then runs a quick command.
    wait = "i=0; while [ $i -lt 30 ]; do echo waiting for the suite slot >&2; sleep 0.2; i=$((i+1)); done"
    cli.main(["checks", "--", "sh", "-c", f"{wait}; true"])
    assert json.loads((run / "checks.json").read_text())["passed"], "a wait that says so is not a hang"
    capsys.readouterr()
    # The same wait, then a command that hangs with no output: still stopped at the silence limit.
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", "sh", "-c", f"{wait}; sleep 300"])
    assert e.value.code == 1
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["timed_out"]["silent_s"] == 2.0, rec
    assert "print a line while it waits" in capsys.readouterr().err, "the stop names what a waiting wrapper prints"
    # The plugin's own waits a check can sit in say so more often than the default silence limit.
    for name, every in (("ttp lock", cli.LOCK_TELL_S), ("checks lanes", checklane.TELL_S),
                        ("checks slots", checkcap.TELL_S), ("test-worker slots", testslots.TELL_S)):
        assert 0 < every < 600, (name, every)

# Checks lanes (checklane): light checks never queue, N lanes per project and repository, dead holders
# reclaimed, a cheaper waiter's overflow lane, visible waits, and `ttp checks status`.

_LANE_REPO = "0123456789ab"   # a repository key (testslots.repo_key)

_LANE_HOLDER = """
import json, sys, time
from pathlib import Path
from ttp import checklane, testslots
checklane.HEARTBEAT_S = 3600.0   # no touch of the lane file while a test compares it
g = testslots.suite_hold(sys.argv[1], repo=sys.argv[3], what=sys.argv[4], cost=json.loads(sys.argv[5]),
                         who=sys.argv[6] if len(sys.argv) > 6 else "")
Path(sys.argv[2]).write_text("held")
while True:
    time.sleep(1)
"""


def _lane_holder(tmp_path, name, repo, what="pytest -q", cost=None, task="12", who=""):
    got = tmp_path / f"held-{time.time_ns()}"
    proc = subprocess.Popen([sys.executable, "-c", _LANE_HOLDER, name, str(got), repo, what, json.dumps(cost or {}),
                             who],
                            env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_TASK": task})
    assert _wait_for(got.exists), "the lane holder never took its lane"
    return proc


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(10)


def _lane_env(monkeypatch):
    from ttp import checkcap, testslots
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    monkeypatch.setenv("TTP_TEST_PROJECT_WORKERS", "0")   # lanes' own mechanics, no worker ceiling (testslots.project_workers)
    for k in (testslots.HELD_ENV, checkcap.HELD_ENV):
        monkeypatch.delenv(k, raising=False)


def test_light_checks_run_at_once_while_a_heavy_run_holds_the_lane_and_a_failing_one_fails(
        env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, checkwall, cli, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 0.3)
    detached = []
    monkeypatch.setattr(cli, "_checks_detach", lambda *a, **k: detached.append(1))
    (repo / "ok.py").write_text("x = 1\n")
    (repo / "bad.py").write_text("def (:\n")
    subprocess.run([*git, "add", "ok.py", "bad.py"], check=True)
    subprocess.run([*git, "commit", "-qm", "py"], check=True)
    hold = testslots.suite_hold(p.name, repo=testslots.repo_key(repo))
    seen = tmp_path / "seen"
    try:
        cli.main(["checks", "--", "python3 -m py_compile ok.py && git diff --check"])
        assert "light checks only" in capsys.readouterr().out
        cli.main(["checks", "--light", "--", f"touch {seen}"])
        assert seen.exists(), "--light runs the commands after -- at once"
        p.set_config("checks.light_cmds", ["touch"])
        cli.main(["checks", "--", f"touch {seen}.2"])
        assert (tmp_path / "seen.2").exists(), "checks.light_cmds adds light commands"
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--", "python3 -m py_compile bad.py"])
        assert e.value.code not in (0, None, 75), "a failing light check fails, it never waits or passes"
        runs = [json.loads(x) for x in (p.state / checkwall.RUNS_LOG).read_text().splitlines()]
        assert runs[-1]["passed"] is False and "k" not in runs[-1]
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--", f"sh -c 'touch {seen}.3'"])
        assert e.value.code == 75 and detached == [1] and not (tmp_path / "seen.3").exists(), \
            "a heavy check still waits for the lane"
    finally:
        hold.close()


def test_only_whole_light_commands_are_light(env):
    from ttp import checklane, testslots
    pats = checklane.light_patterns({})
    for cmd in ("python3 -m py_compile a.py", "python3 -m py_compile a.py && git diff --check", "ruff check .",
                "timeout 60 ruff check .", "FOO=1 ruff check .", "black --check .", "git diff --check"):
        assert checklane.is_light(cmd, pats), cmd
    for cmd in ("black . --check", "ruff check . && pytest", "$CMD", "${CMD} -n", "bash script.sh -n",
                "sh -c 'ruff check .'", "ruff check .; rm -rf x", "ruff check . | sh", "eval ruff check .",
                "ruff checkx .", "pytest", "", "python3 -m pytest -q", "`pytest` --check"):
        assert not checklane.is_light(cmd, pats), cmd
    assert checklane.all_light(["make"], {}, ["make"]), "--light declares its own commands light"
    assert not checklane.all_light(["make", "ruff check ."], {}, ["ruff check ."])
    assert checklane.all_light(["mylint src"], {"checks": {"light_cmds": "mylint, other -q"}})
    assert not checklane.all_light([], {}), "nothing to run is not a light run"
    assert checklane.run_class(["ruff check . && pytest"]) == checklane.SUITE
    assert checklane.run_class(["sh build.sh"]) == checklane.OTHER


def test_checks_lanes_default_from_the_machine_and_follow_checks_slots(env, monkeypatch):
    from ttp import checklane, project
    assert checklane.default_slots(1, 100) == 2, "runs are isolated: at least two lanes"
    assert checklane.default_slots(0, 0) == 1, "only too little memory keeps one lane"
    assert checklane.default_slots(64, 1000) == checklane.MAX_LANES
    assert checklane.default_slots(64, 10) == 1, "little free memory keeps one lane"
    assert checklane.default_slots(16, None) == 2
    monkeypatch.delenv(checklane.SLOTS_ENV)
    monkeypatch.setattr(checklane, "default_slots", lambda *a, **k: 3)
    assert checklane.slots({}) == 3
    monkeypatch.setenv(checklane.SLOTS_ENV, "junk")
    assert checklane.slots({}) == 3
    project.set_account_setting("checks.slots", 2)
    assert checklane.slots({}) == 2
    assert checklane.slots({"checks": {"slots": 1}}) == 1, "the project's setting wins"
    assert checklane.slots({"checks": {"slots": 0}}) == 3, "an invalid setting falls back to the machine's"


def test_checks_lanes_follow_the_host_small_keeps_the_old_default_idle_large_gets_more(env, monkeypatch):
    from ttp import checklane, testslots
    monkeypatch.delenv(checklane.SLOTS_ENV)
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "2")
    for c, mem in ((8, 30), (16, 60)):
        cap = checklane.capacity({}, n_cores=c, load=0.0, mem_gib=mem)
        assert cap["n"] == cap["floor"] == checklane.default_slots(c, mem), "a small host keeps the old default"
    old = checklane.default_slots(64, 468)
    cap = checklane.capacity({}, n_cores=64, load=0.0, mem_gib=468)
    assert cap["n"] == cap["top"] == 8 > old, "an idle large host gets more lanes than the old default"
    assert checklane.capacity({}, held=6, n_cores=64, load=50.0, mem_gib=468, used=48)["n"] == 7, \
        "load counts the runs"
    note = checklane.slots_note({}, held=0)
    assert "checks.slots unset" in note and "cores, load" in note and "GiB" in note, note


def test_checks_lanes_follow_the_test_pool_run_count_not_the_cores(env, monkeypatch):
    """A 64-core host whose test pool is busy with other projects' runs opens no lane beyond the runs
    the pool fills at a full grant, so no lane starts on one or two workers."""
    from ttp import checklane, testslots
    monkeypatch.delenv(checklane.SLOTS_ENV)
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "2")
    pool, run_max = testslots.sizes({}, cores=64)
    # Other projects on the host hold 24 of the pool's slots and load the host to 40: the pool a run takes
    # from is 24 + 24 spare = 48, which fills 6 runs at a full grant, though the cores would hold 8.
    cap = checklane.capacity({}, n_cores=64, load=40.0, mem_gib=468, used=24)
    assert 64 // checklane.run_cores({}, 64) == 8 and cap["top"] == 48 // run_max == 6, cap
    assert cap["n"] <= cap["top"] and cap["fit"] <= cap["top"], cap
    # Busier: the pool falls back to its default half (32 slots), 4 runs, the floor.
    cap = checklane.capacity({}, held=4, n_cores=64, load=70.0, mem_gib=468, used=32)
    assert cap["top"] == max(cap["floor"], pool // run_max) == 4 and cap["n"] == 4, cap
    # A pool set below the floor's runs never takes the lanes below the floor or below 1.
    monkeypatch.setenv("TTP_TEST_POOL", "2")
    cap = checklane.capacity({}, n_cores=64, load=0.0, mem_gib=468, used=0)
    assert cap["top"] >= cap["floor"] >= 1 and cap["n"] >= 1, cap
    # The pool only lowers the lanes: never above the cores' runs, and an explicit checks.slots still wins.
    monkeypatch.setenv("TTP_TEST_POOL", "512")
    cap = checklane.capacity({}, n_cores=64, load=0.0, mem_gib=468, used=0)
    assert cap["top"] <= 64 // checklane.run_cores({}, 64), cap
    assert checklane.slots({"checks": {"slots": 2}}, held=0) == 2


def test_checks_lanes_admit_no_new_run_on_high_load_or_low_memory_and_an_explicit_setting_wins(
        env, monkeypatch):
    from ttp import checklane, project
    monkeypatch.delenv(checklane.SLOTS_ENV)
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "2")
    floor = checklane.default_slots(64, 468)
    for held in (floor, floor + 2):
        for load, mem in ((70.0, 468), (63.5, 468), (0.0, 7.0), (float("inf"), 468)):
            # The held runs hold their full grants (8 slots each); an unreadable load keeps the pool's default.
            cap = checklane.capacity({}, held=held, n_cores=64, load=load, mem_gib=mem, used=8 * held)
            assert cap["n"] <= held and cap["fit"] == 0, (held, load, mem, cap)
            assert cap["n"] == held or load == float("inf"), (held, load, mem, cap)
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    assert checklane.capacity({}, n_cores=64, load=0.0, mem_gib=468)["n"] == floor, "load_extra 0 keeps the floor"
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "2")
    monkeypatch.setattr(checklane, "capacity", lambda *a, **k: {"n": 8})
    assert checklane.slots({}, held=3) == 8
    monkeypatch.setenv(checklane.SLOTS_ENV, "1")
    assert checklane.slots({}, held=3) == 1, "TTP_CHECKS_SLOTS wins"
    project.set_account_setting("checks.slots", 2)
    assert checklane.slots({}, held=3) == 2, "the account's setting wins"
    assert checklane.slots({"checks": {"slots": 3}}, held=0) == 3, "the project's setting wins"
    assert "checks.slots raises it" in checklane.slots_note({"checks": {"slots": 3}})


def test_test_pool_grows_only_into_spare_cores_and_a_set_pool_wins(env, monkeypatch):
    from ttp import testslots
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "2")
    assert testslots.pool_now({}, 32, 0, cores=64, load=1.0) == 63
    assert testslots.pool_now({}, 32, 24, cores=64, load=60.0) == 32, "a busy host keeps the half"
    assert testslots.pool_now({}, 32, 40, cores=64, load=60.0) == 44
    assert testslots.pool_now({}, 32, 0, cores=64, load=float("inf")) == 32
    assert testslots.pool_now({"checks": {"test_pool": 16}}, 16, 0, cores=64, load=0.0) == 16
    assert testslots.pool_now({}, 32, 0, cores=64, load=0.0, kind=testslots.BUILDS) == 32
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    assert testslots.pool_now({}, 32, 0, cores=64, load=0.0) == 32


def test_suite_hold_rereads_its_lane_count_at_each_admission(env, monkeypatch):
    from ttp import testslots
    _lane_env(monkeypatch)
    n = [1]
    a = testslots.suite_hold("pa", repo=_LANE_REPO, slots=lambda held: n[0], wait_s=0)
    assert a and a.files
    assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=lambda held: n[0], wait_s=0.2) is None
    n[0] = 2
    b = testslots.suite_hold("pa", repo=_LANE_REPO, slots=lambda held: n[0], wait_s=0)
    assert b and b.files, "a grown count admits the next run"
    n[0] = 1
    assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=lambda held: n[0], wait_s=0.2) is None, \
        "holders past a shrunk count still count"
    a.close(), b.close()


def test_a_stale_free_lane_count_never_admits_past_the_lane_bound(env, monkeypatch):
    # Eight lane files from a larger count, three held past a count of 4 (files 5-7). A waiter reads one
    # free lane, then stalls while a more urgent run takes it and leaves the queue: the waiter's stale
    # count must not give it a fifth lane.
    from ttp import checkcap, locks, testslots
    _lane_env(monkeypatch)
    every = testslots.lane_paths("pa", 8, _LANE_REPO)
    for x in every:
        x.parent.mkdir(parents=True, exist_ok=True)
        x.touch()
    past = [locks.try_take([x], "pa: full checks (other run)", "pytest") for x in every[5:]]
    assert all(past)
    counted, go, got = threading.Event(), threading.Event(), []

    def count(held):
        if not counted.is_set():
            counted.set()
            go.wait(10)
        return 4

    def waiter():
        got.append(testslots.suite_hold("pa", repo=_LANE_REPO, slots=count, wait_s=0.5))
    w = threading.Thread(target=waiter, name="stale")
    w.start()
    assert counted.wait(10)
    urgent = testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, priority=1, wait_s=0)
    assert urgent and urgent.files, "the urgent run takes the one free lane"
    go.set()
    w.join(30)
    held = [x for x in testslots.lane_paths("pa", repo=_LANE_REPO) if locks._slot_labels([x])]
    assert len(held) == 4 and got == [None], f"a stale count admitted past 4 lanes: {[x.name for x in held]}"
    urgent.close()
    for f in past:
        f.close()
    assert checkcap.slot_dir()


def test_two_lanes_let_two_runs_proceed_and_one_lane_keeps_priority_order(env, tmp_path, monkeypatch):
    from ttp import checklane, testslots
    _lane_env(monkeypatch)
    a = testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, wait_s=0)
    b = testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, wait_s=0)
    assert a and a.files and b and b.files, "two lanes: two runs at once"
    assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, wait_s=0.2) is None, "a third waits"
    a.close(), b.close()
    hold = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1)
    order = []

    def wait(prio):
        g = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, priority=prio, wait_s=20)
        order.append(prio)
        time.sleep(0.2)
        g.close()
    late = threading.Thread(target=wait, args=(5,))
    late.start()
    assert _wait_for(lambda: len(checklane._live_tickets(testslots.suite_base("pa", _LANE_REPO))) == 1)
    urgent = threading.Thread(target=wait, args=(1,))
    urgent.start()
    assert _wait_for(lambda: len(checklane._live_tickets(testslots.suite_base("pa", _LANE_REPO))) == 2)
    hold.close()
    late.join(30), urgent.join(30)
    assert order == [1, 5], "one lane: the more urgent waiter goes first"


def test_a_lane_freed_after_a_waiters_queue_read_goes_to_the_more_urgent_waiter(env, tmp_path, monkeypatch):
    # The less urgent waiter stalls right after reading the queue until the holder frees the lane:
    # a free-lane count taken after that stale read must not let it jump the more urgent waiter.
    from ttp import checklane, testslots
    _lane_env(monkeypatch)
    hold = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1)
    stalled, released, order = threading.Event(), threading.Event(), []
    orig = testslots._ordered

    def slow(*a, **k):
        q = orig(*a, **k)
        if threading.current_thread().name == "late" and not stalled.is_set():
            stalled.set()
            released.wait(10)
        return q
    monkeypatch.setattr(testslots, "_ordered", slow)

    def wait(prio):
        g = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, priority=prio, wait_s=20)
        order.append(prio)
        time.sleep(0.2)
        g.close()
    late = threading.Thread(target=wait, args=(5,), name="late")
    late.start()
    assert stalled.wait(10)
    urgent = threading.Thread(target=wait, args=(1,))
    urgent.start()
    assert _wait_for(lambda: len(checklane._live_tickets(testslots.suite_base("pa", _LANE_REPO))) == 2)
    hold.close()
    released.set()
    late.join(30), urgent.join(30)
    assert order == [1, 5], "a lane freed after a stale queue read went to the less urgent waiter"


def test_dead_or_silent_lane_holders_are_reclaimed_and_logged(env, tmp_path, monkeypatch):
    from ttp import checklane, locks, testslots
    _lane_env(monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    gone = subprocess.Popen(["true"])
    gone.wait()
    lane = testslots.lane_paths("pa", 1, _LANE_REPO)
    for label in ({"pid": gone.pid}, {"hb": 1}):
        f = locks.try_take(lane, "pa: full checks (run 7, pid x)", "pytest", extra={"task": "7", "hb": 30})
        locks.relabel(f, label)
        if "hb" in label:
            assert testslots.suite_hold("pa", repo=_LANE_REPO, wait_s=0.3) is None, "a live, beating holder keeps it"
            os.utime(lane[0], (time.time() - 1000, time.time() - 1000))
        told = []
        g = testslots.suite_hold("pa", repo=_LANE_REPO, wait_s=5, state=state, tell=told.append)
        assert g and g.files, f"a lane held by {label} was not reclaimed"
        assert any("reclaimed a checks lane" in x for x in told), told
        g.close()
        f.close()
    log = [json.loads(x) for x in (state / checklane.RECLAIM_LOG).read_text().splitlines()]
    assert ["process is gone" in log[0]["why"], "heartbeat stopped" in log[1]["why"]] == [True, True], log
    assert log[0]["holder"]["task"] == "7"


_ORPHANING_HOLDER = """
import subprocess, sys, time
from pathlib import Path
from ttp import checklane, testslots
checklane.HEARTBEAT_S = 3600.0
g = testslots.suite_hold(sys.argv[1], repo=sys.argv[3])
fd = g.files[0].fileno()
child = subprocess.Popen(["sleep", "60"], pass_fds=[fd], start_new_session=True)   # keeps the lane's lock open
Path(sys.argv[2]).write_text(str(child.pid))
while True:
    time.sleep(1)
"""


def test_a_holder_killed_mid_suite_without_releasing_its_lane_frees_or_reaps_it(env, tmp_path, monkeypatch):
    """A lane holder SIGKILLed mid-suite: its flock dies with it, so the next waiter takes the lane at once;
    if a stray child kept the lock open, the next waiter reaps the lane (its labelled pid is gone)."""
    from ttp import checklane, locks, testslots
    _lane_env(monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    holder = _lane_holder(tmp_path, "pa", _LANE_REPO, task="7")
    try:
        assert testslots.suite_hold("pa", repo=_LANE_REPO, wait_s=0.3) is None, "a live holder keeps its lane"
        holder.kill()   # SIGKILL: no close, no release, no exit handler
        holder.wait(10)
        g = testslots.suite_hold("pa", repo=_LANE_REPO, wait_s=5, state=state)
        assert g and g.files, "the killed holder's lane was never freed"
        g.close()
    finally:
        _stop(holder)
    assert not (state / checklane.RECLAIM_LOG).exists(), "the kernel freed it; nothing needed reaping"
    got = tmp_path / "held-orphan"
    holder = subprocess.Popen([sys.executable, "-c", _ORPHANING_HOLDER, "pa", str(got), _LANE_REPO],
                              env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_TASK": "8"})
    lane = testslots.lane_paths("pa", 1, _LANE_REPO)[0]
    try:
        assert _wait_for(got.exists), "the orphaning holder never took its lane"
        holder.kill()
        holder.wait(10)
        assert locks._slot_labels([lane]), "the stray child still holds the lane's lock"
        told = []
        g = testslots.suite_hold("pa", repo=_LANE_REPO, wait_s=5, state=state, tell=told.append)
        assert g and g.files, "a lane held only by a dead holder's stray child was never reaped"
        g.close()
    finally:
        _stop(holder)
        if got.exists():
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(got.read_text()), signal.SIGKILL)   # the stray child, by its pid
    log = [json.loads(x) for x in (state / checklane.RECLAIM_LOG).read_text().splitlines()]
    assert len(log) == 1 and "process is gone" in log[0]["why"] and log[0]["holder"]["task"] == "8", log
    assert any("reclaimed a checks lane" in x for x in told), told


def test_a_cheaper_waiter_gets_an_overflow_lane_after_max_wait_and_only_then(env, tmp_path, monkeypatch):
    from ttp import checklane, testslots
    _lane_env(monkeypatch)
    holder = _lane_holder(tmp_path, "pa", _LANE_REPO, cost={"cls": checklane.SUITE, "est_s": 600})
    try:
        for cost, max_wait in (({"cls": checklane.SUITE}, 0.3), ({"cls": checklane.OTHER, "est_s": 900}, 0.3),
                               ({"cls": checklane.OTHER}, 0)):
            assert testslots.suite_hold("pa", repo=_LANE_REPO, cost=cost, max_wait=max_wait, wait_s=1) is None, cost
        # Not before max_wait: a cheaper waiter whose wait ends first never gets the overflow lane.
        assert testslots.suite_hold("pa", repo=_LANE_REPO, cost={"cls": checklane.OTHER}, max_wait=60,
                                    wait_s=0.3) is None
        told = []
        # max_wait counts from the ticket's arrival, set inside the call; Grant.waited starts later (after
        # the enqueue), so it can read short of max_wait on a busy host. Time the whole call instead; the
        # slowed step between the two stands in for that host.
        remember = checklane.remember_name
        monkeypatch.setattr(checklane, "remember_name", lambda *a, **k: (time.sleep(0.2), remember(*a, **k))[1])
        t0 = time.time()
        g = testslots.suite_hold("pa", repo=_LANE_REPO, cost={"cls": checklane.OTHER}, max_wait=0.3, wait_s=5,
                                 tell=told.append)
        took = time.time() - t0
        assert g and g.files and "-x." in g.files[0].name and took >= 0.3, took
        assert any("extra checks lane" in x and "#12" in x for x in told), told
        g.close()
    finally:
        _stop(holder)


def test_a_lane_wait_names_the_holder_and_the_queue_position(env, tmp_path, monkeypatch):
    from ttp import testslots
    _lane_env(monkeypatch)
    holder = _lane_holder(tmp_path, "pa", _LANE_REPO, what="pytest -q tests/a.py")
    try:
        told = []
        assert testslots.suite_hold("pa", repo=_LANE_REPO, wait_s=0.3, tell=told.append) is None
        assert "position 1 of 1" in told[0] and ("#12 'pytest -q tests/a.py' queued 0 min, has the lane 0 min, "
                                                 "not running yet, no child process") in told[0], told
    finally:
        _stop(holder)


def test_long_lane_waits_show_in_status_and_the_digest_and_raise_one_event_past_wait_event_s(
        env, tmp_path, monkeypatch):
    from ttp import checkcap, checklane, cli, coordinator, locks, testslots
    from ttp.daemon import Daemon
    p = make(env)
    _lane_env(monkeypatch)
    holder = _lane_holder(tmp_path, p.name, _LANE_REPO)
    base = testslots.suite_base(p.name, _LANE_REPO)
    ticket = locks.enqueue(checkcap.slot_dir(), base, f"task #13 {p.name}: full checks (run 9, pid 1)",
                           time.time() - 800, priority=2)
    try:
        assert checklane.wait_events(p.name, {}) == [], "no event before checks.wait_event_s"
        lines = checklane.long_wait_lines(p.name, p.config())
        assert lines and "#13 has waited 13 min" in lines[0] and "#12" in lines[0], lines
        assert lines[0] in cli.status_text(p)
        gates = {"claude": {"level": "green", "reasons": [], "numbers": {}, "max_tier": "deep", "max_parallel": 6,
                            "allow_optional": True, "allow_new_work": True, "regime": "dollars"}}
        assert lines[0] in dict(coordinator.digest_parts(p, gates, [], [])[0]).get("checks_wait", "")
        d = Daemon(p.base)
        d.check_checks_wait(every_s=0)
        assert not p.db.q("SELECT id FROM events WHERE kind='checks_slot_wait'")
        assert checklane.wait_events(p.name, {"checks": {"wait_event_s": 600}}), "checks.wait_event_s sets it"
        assert checklane.wait_events(p.name, {"checks": {"wait_event_s": 0}}) == [], "0 turns it off"
        locks.dequeue(ticket)
        ticket = locks.enqueue(checkcap.slot_dir(), base, f"task #13 {p.name}: full checks (run 9, pid 1)",
                               time.time() - 1000, priority=2)
        for _ in range(2):
            d.check_checks_wait(every_s=0)
        ev = p.db.q("SELECT task, text, status FROM events WHERE kind='checks_slot_wait' ORDER BY id")
        assert [e["status"] for e in ev] == ["queued", "coalesced"], ev
        assert all(e["task"] == 13 and "#12" in e["text"] for e in ev), ev
    finally:
        locks.dequeue(ticket)
        _stop(holder)


def test_long_wait_lines_order_by_the_project_s_priority_age_s_not_the_account_s(env, monkeypatch):
    # waits()/long_wait_lines() order waiters by the project's checks.priority_age_s, so the shown
    # queue position matches the grant order; a project step that ages a waiter past a newer
    # higher-priority one inverts the account-default order.
    from ttp import checkcap, checklane, locks, testslots
    _lane_env(monkeypatch)
    p = make(env)
    p.set_config("checks.priority_age_s", 60)          # the project's aging step: 1 min
    cfg = p.config()
    assert checklane.priority_age_s(cfg) == 60
    now = time.time()
    # A p3 that waited 5 project steps (300 s) ages to p2 and passes the fairness bound under the
    # project's step, ahead of a newer p2 that arrived 30 s ago; under the account default
    # (SUITE_AGE_S = 900) it would still rank p3, behind it.
    old = _suite_ticket(p.name, 3, now - 5 * 60, "31")
    new = _suite_ticket(p.name, 2, now - 30, "32")
    try:
        w = checklane.waits(p.name, cfg, now)
        assert [(x["task"], x["position"]) for x in w] == [("31", 1), ("32", 2)], w
        # the account/default order (900 s step) keeps the newer p2 first
        assert [(x["task"], x["position"]) for x in checklane.waits(p.name, {"checks": {}}, now)] == \
            [("32", 1), ("31", 2)]
        lines = checklane.long_wait_lines(p.name, cfg, now, over_s=0)
        assert [l.split("#")[1].split()[0] for l in lines] == ["31", "32"], lines
        assert [l.split("#")[1].split()[0] for l in checklane.long_wait_lines(p.name, None, now, 0)] == ["32", "31"]
    finally:
        for t in (old, new):
            locks.dequeue(t)


_LANE_WAITER = """
import sys
from ttp import testslots
testslots.suite_hold(sys.argv[1], repo=sys.argv[2], since=float(sys.argv[3]))
"""


def _lane_waiter(name, since, cwd, env=None, shell=False):
    """A real `ttp checks`-style waiter for the held lane (its own session, so no test-runner parent's
    TTP_TASK is its), queued at `since` once this returns; `shell`: started by a `sh` that keeps `env`'s
    TTP_TASK from it (a parent run's task)."""
    from ttp import testslots
    clean = {k: v for k, v in os.environ.items()
             if k not in ("TTP_TASK", "TTP_TASK_ID", "TTP_RUN_ID", "TTP_RUN_DIR", "TTP_DETACHED")}
    argv = [sys.executable, "-c", _LANE_WAITER, name, _LANE_REPO, repr(since)]
    if shell:
        argv = ["sh", "-c", 'env -u TTP_TASK "$@"; :', "sh", *argv]
    proc = subprocess.Popen(argv, cwd=cwd, env={**clean, "PYTHONPATH": str(RUNTIME), **(env or {})},
                            start_new_session=True)
    assert _wait_for(lambda: testslots.suite_order(name, repo=_LANE_REPO)), "the waiter never queued"
    return proc


def _stop_group(proc):
    try:
        os.killpg(proc.pid, 9)
    except OSError:
        pass
    proc.wait(10)


def test_a_waiter_that_requeues_between_reads_keeps_its_task_in_the_wait_event_and_status(env, tmp_path, monkeypatch):
    # requeued-ticket-label: the daemon read the queue (suite_order), then the ticket's text; a waiter
    # that left the queue to rejoin it at its place in between read as "a run with no task".
    from ttp import checklane, testslots
    _lane_env(monkeypatch)
    holder = _lane_holder(tmp_path, "pa", _LANE_REPO)
    waiter = _lane_waiter("pa", time.time() - 1000, tmp_path, {"TTP_TASK": "13"})
    real = testslots.suite_order

    def requeueing(*a, **k):   # each ticket leaves the queue right after it was read
        q = real(*a, **k)
        for t in q:
            t.unlink()
        return q

    monkeypatch.setattr(testslots, "suite_order", requeueing)
    try:
        (ev,) = checklane.wait_events("pa", {})
        assert ev["task"] == 13 and "Checks lane wait: #13 has waited" in ev["text"], ev
        assert ev["fingerprint"].startswith("checks_slot_wait:13:"), ev
        assert _wait_for(lambda: real("pa", repo=_LANE_REPO)), "the waiter queues again"
        (line,) = checklane.long_wait_lines("pa", {}, over_s=0)
        assert line.startswith("checks lane wait: #13 has waited"), line
    finally:
        _stop_group(waiter)
        _stop(holder)


@pytest.mark.parametrize("how", ["run_dir", "worktree", "parent"])
def test_a_ticket_without_its_task_is_labelled_from_its_run_worktree_or_parent(env, tmp_path, monkeypatch, how):
    # detached-ticket-label: a waiter whose ticket names no task (a detached or re-executed `ttp checks`
    # without TTP_TASK) is still its task's: by the worker run it checks for, its worktree or its parent.
    from ttp import checklane
    _lane_env(monkeypatch)
    holder = _lane_holder(tmp_path, "pa", _LANE_REPO)
    cwd, extra = tmp_path, {}
    if how == "run_dir":
        run = tmp_path / "state" / "runs" / "77"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"env": {"TTP_TASK": "14"}}))
        extra = {"TTP_RUN_DIR": str(run), "TTP_DETACHED": "1"}
    elif how == "worktree":
        cwd = tmp_path / "tt-project" / "worktrees" / "t14"
        cwd.mkdir(parents=True)
    else:
        extra = {"TTP_TASK": "14"}
    waiter = _lane_waiter("pa", time.time() - 1000, cwd, extra, shell=how == "parent")
    try:
        (w,) = checklane.waits("pa", {}, over_s=0)
        assert w["task"] == "14", (how, w)
        assert w["run"] == ("77" if how == "run_dir" else ""), w
        (ev,) = checklane.wait_events("pa", {})
        assert ev["task"] == 14 and "#14 has waited" in ev["text"] and "no task" not in ev["text"], ev
        (lane,) = checklane.lane_status("pa")
        assert [x["task"] for x in lane["waiters"]] == ["14"], lane
    finally:
        _stop_group(waiter)
        _stop(holder)


def test_a_truly_taskless_waiter_is_named_by_pid_command_folder_and_first_queued_time(env, tmp_path, monkeypatch):
    # taskless-names-pid: only a waiter no record links to a task reads "a run with no task", and then
    # with what the coordinator needs to act on it without a hunt.
    from ttp import checklane, timefmt
    _lane_env(monkeypatch)
    holder = _lane_holder(tmp_path, "pa", _LANE_REPO)
    cwd = tmp_path / "plain"
    cwd.mkdir()
    since = time.time() - 1000
    waiter = _lane_waiter("pa", since, cwd)
    cfg = {"home_timezone": "America/Los_Angeles"}
    try:
        (ev,) = checklane.wait_events("pa", cfg)
        assert ev["task"] is None and ev["fingerprint"].startswith("checks_slot_wait:run:"), ev
        # the command is cut to 80 characters, so a long interpreter path may leave out " -c"
        who = f"a run with no task (pid {waiter.pid} '{(sys.executable + ' -c ')[:40]}"
        assert who in ev["text"], ev["text"]
        assert f" in {cwd}, first queued {timefmt.short(since, cfg)})" in ev["text"], ev["text"]
        (line,) = checklane.long_wait_lines("pa", cfg, over_s=0)
        assert line.startswith(f"checks lane wait: a run with no task (pid {waiter.pid} "), line
        assert "#" not in line.split(" has waited")[0], line
    finally:
        _stop_group(waiter)
        _stop(holder)


def test_two_repositories_check_side_by_side_while_one_repository_takes_turns(env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, cli, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 0.3)
    monkeypatch.setattr(cli, "_checks_detach", lambda *a, **k: None)
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    wt = tmp_path / "wt"
    subprocess.run([*git, "worktree", "add", "-q", str(wt)], check=True)
    mine, theirs = testslots.repo_key(repo), testslots.repo_key(other)
    assert mine and theirs and mine != theirs and testslots.repo_key(wt) == mine, "worktrees share their repository's"
    holder = _lane_holder(tmp_path, p.name, theirs)
    seen = tmp_path / "seen"
    try:
        cli.main(["checks", "--", f"sh -c 'touch {seen}'"])
        assert seen.exists(), "another repository's run never holds this one back (slots=1)"
        hold = testslots.suite_hold(p.name, repo=mine)
        try:
            monkeypatch.chdir(wt)
            with pytest.raises(SystemExit) as e:
                cli.main(["checks", "--", f"sh -c 'touch {seen}.2'"])
            assert e.value.code == 75 and not (tmp_path / "seen.2").exists(), "the same repository takes turns"
        finally:
            hold.close()
    finally:
        _stop(holder)


def test_checks_status_reads_the_lanes_and_never_queues_or_writes(env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    assert _ttp("checks", "status") == 0 and "no lane held and nobody waiting" in capsys.readouterr().out
    key = testslots.repo_key(repo)
    holder = _lane_holder(tmp_path, p.name, key)
    monkeypatch.setenv("TTP_TASK", "13")
    # Snapshot only once the waiter is settled: waiting(True) comes after its ticket's owner record and
    # lane name are written (which a snapshot taken as soon as the ticket shows would catch landing).
    # No wait_s: a waiter timing out under load would dequeue between the snapshots; done stops it.
    settled, done = threading.Event(), threading.Event()

    def wait():
        try:
            testslots.suite_hold(p.name, repo=key, priority=3, waiting=lambda on: on and settled.set(),
                                 stop=lambda: "test over" if done.is_set() else "")
        except checkcap.Cancelled:
            pass
    waiter = threading.Thread(target=wait)
    waiter.start()

    def snap():
        d = checkcap.slot_dir()
        return {str(x): (x.stat().st_mtime_ns, x.stat().st_size, x.read_bytes() if x.is_file() else b"")
                for x in sorted(d.rglob("*")) if not x.name.startswith(".")}
    try:
        assert settled.wait(30), "the waiter never queued"
        before = snap()
        assert any(".queue/" in k and not k.rsplit("/", 1)[1].startswith(".") for k in before), before
        assert _ttp("checks", "status") == 0
        out = capsys.readouterr().out
        assert f"pid {holder.pid} for full checks, alive" in out and "#12" in out and "(this one)" in out, out
        assert "waiter 1 (next): #13" in out and "p3 ranked p3" in out, out
        assert _ttp("checks", "--slot-status", "--json") == 0
        got = json.loads(capsys.readouterr().out)
        assert _ttp("checks", "status", "--json") == 0
        assert [r["repo"] for r in json.loads(capsys.readouterr().out)["repos"]] == [r["repo"] for r in got["repos"]]
        lane = got["repos"][0]
        assert got["this_repo"] == key == lane["repo"] and lane["lanes"][0]["pid"] == holder.pid
        assert lane["lanes"][0]["alive"] is True and lane["lanes"][0]["task"] == "12"
        assert [(w["task"], w["next"]) for w in lane["waiters"]] == [("13", True)]
        assert snap() == before, "status queued, took or wrote a lane or queue file"
        assert not (run / "checks.log").exists(), "status started no check"
    finally:
        done.set()
        waiter.join(10)
        _stop(holder)
        assert not waiter.is_alive()


_WRAP_A, _WRAP_B = "aaaaaaaaaaaa", "bbbbbbbbbbbb"   # two wrapper locks (checklane.wrap_key)


def test_lock_free_checks_run_beside_a_run_behind_a_wrapper_lock_which_holds_only_its_lock_lane(
        env, tmp_path, monkeypatch):
    from ttp import checklane, testslots
    _lane_env(monkeypatch)
    locked = testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, lock=_WRAP_A, cap=4, wait_s=0)
    assert locked and locked.files and ".w" + _WRAP_A + "." in locked.files[0].name, "the wrapper lock's own lane"
    a = testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, cap=4, wait_s=0)
    b = testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, cap=4, wait_s=0)
    assert a and a.files and b and b.files, "both repository lanes stay free for lock-free checks"
    assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, cap=4, wait_s=0.2) is None, "a third waits"
    bases = testslots.suite_bases("pa")
    assert any(x.endswith(".w" + _WRAP_A) for x in bases), bases
    assert checklane.wrap_key(("ttp", "lock", "dev", "--")) == checklane.wrap_key(["ttp", "lock", "dev", "--"])
    assert checklane.wrap_key(()) == "" and len(checklane.wrap_key(("flock", "/tmp/x"))) == 12
    for g in (locked, a, b):
        g.close()


def test_runs_behind_one_wrapper_lock_go_one_at_a_time_and_another_lock_does_not_wait(env, tmp_path, monkeypatch):
    from ttp import testslots
    _lane_env(monkeypatch)
    first = testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, lock=_WRAP_A, cap=8, wait_s=0)
    assert first and first.files
    told = []
    assert testslots.suite_hold("pa", repo="ba9876543210", slots=4, lock=_WRAP_A, cap=8, wait_s=0.3,
                                tell=told.append) is None, "the same wrapper lock waits, whatever the repository"
    assert told and "behind this wrapper lock" in told[0], told
    # Overflow lanes never serve a wrapper lock: its runs would only queue on the lock itself.
    assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, lock=_WRAP_A, cap=8, wait_s=0.5, max_wait=0.1,
                                cost={"cls": 0}, priority=1, headroom=lambda: True) is None
    other = testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, lock=_WRAP_B, cap=8, wait_s=0)
    assert other and other.files, "another wrapper lock has its own lane"
    first.close(), other.close()


_FAILING_WRAPS = ("flock -n {f}", "flock --nonblock {f}", "flock -nx {f}", "flock -xn {f}", "flock -x -n {f}",
                  "flock --conflict-exit-code 3 {f}", "flock -E 3 -n {f}", "flock -w 60 {f}", "flock -w60 {f}",
                  "flock --wait 60 {f}", "flock --timeout=60 {f}", "flock -x -w 1 {f}")


@pytest.mark.parametrize("wrap", _FAILING_WRAPS + ("flock {f}", "flock -x {f}"))
def test_a_non_blocking_or_wait_bounded_flock_wrapper_never_runs_two_checks_side_by_side(
        env, tmp_path, monkeypatch, wrap):
    """checks.wrap_cmd `flock -n` / `-w N` (and their long, combined and `-E` forms) fail when the lock is
    busy instead of waiting, so the wrapper never counts as serializing by itself: wrap_key still gives
    it its one lane, and a second run of the project waits there rather than taking a free repository
    lane, an overflow lane or a priority-1 extra lane beside the first (which would fail its lock)."""
    from ttp import checklane, push, testslots
    _lane_env(monkeypatch)
    argv = push.wrap_of({"checks": {"wrap_cmd": wrap.format(f=tmp_path / "dev.lock")}})
    key = checklane.wrap_key(argv)
    assert len(key) == 12, (wrap, argv)
    first = testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, lock=key, cap=8, wait_s=0)
    assert first and first.files and ".w" + key + "." in first.files[0].name
    try:
        assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, lock=key, cap=8, wait_s=0.3, max_wait=0.1,
                                    cost={"cls": 0}, priority=1, headroom=lambda: True) is None, \
            "a second run behind the same wrapper waits for the first"
        free = testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, cap=8, wait_s=0)
        assert free and free.files, "a lock-free check still runs beside it"
        free.close()
    finally:
        first.close()


def test_slot_status_text_names_each_group_by_the_one_naming_rule(env, tmp_path, monkeypatch):
    from ttp import checklane, testslots
    _lane_env(monkeypatch)
    held = [testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, cap=8, wait_s=0),
            testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, lock=_WRAP_A, cap=8, wait_s=0)]
    try:
        groups = [r["group"] for r in checklane.lane_status("pa")]
        assert sorted(groups) == sorted([checklane.group_name(_LANE_REPO), checklane.group_name("w" + _WRAP_A)])
        text = checklane.status_text("pa", {})
        assert all(f"  {g}\n" in text + "\n" or f"  {g} (this one)" in text for g in groups), text
        assert "one at a time" in checklane.group_name("w" + _WRAP_A)
        assert checklane.group_name("").strip() and "not keyed" in checklane.group_name("")
    finally:
        for g in held:
            g.close()


def test_checks_test_project_max_caps_a_projects_checks_at_once_over_all_its_lanes(env, tmp_path, monkeypatch):
    from ttp import checklane, project, testslots
    _lane_env(monkeypatch)
    locked = testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, lock=_WRAP_A, cap=2, wait_s=0)
    a = testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, cap=2, wait_s=0)
    assert locked and a and a.files
    told = []
    assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, cap=2, wait_s=0.3, tell=told.append) is None, \
        "a free repository lane past the project's cap waits"
    assert "at most 2 of the project's checks" in told[0] and "running" in told[0], told
    assert testslots.suite_hold("pb", repo=_LANE_REPO, slots=4, cap=2, wait_s=0), "another project never waits"
    locked.close()
    b = testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, cap=2, wait_s=1)
    assert b and b.files, "a lane under the cap again"
    a.close(), b.close()
    monkeypatch.delenv("TTP_TEST_PROJECT_MAX", raising=False)
    monkeypatch.setenv("TTP_TEST_POOL", "32")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "8")
    assert checklane.project_cap({}) == 4, "unset: the runs the test pool fits side by side"
    assert checklane.project_cap({"checks": {"slots": 6}}) == 6, "at least its lanes per repository"
    monkeypatch.setenv("TTP_TEST_POOL", "0")
    assert checklane.project_cap({}) == 0, "unset with the test pool off: no cap"
    monkeypatch.delenv("TTP_TEST_POOL")
    project.set_account_setting("checks.test_project_max", 3)
    assert checklane.project_cap({}) == 3
    assert checklane.project_cap({"checks": {"test_project_max": 5}}) == 5, "the project's setting wins"
    assert checklane.project_cap({"checks": {"test_project_max": "x"}}) == 3, "an invalid one falls back"


def test_checks_test_project_max_auto_sizes_from_the_host_over_a_fixed_account_number(env, capsys, monkeypatch):
    """A fixed account checks.test_project_max (set when the default was one check at a time) held a project's
    checks under what the host fits, and a project had no way back to the host-sized cap: "auto" is that."""
    import pytest
    from ttp import checkcap, checklane, cli, coordinator, project, testslots
    _lane_env(monkeypatch)
    monkeypatch.delenv("TTP_TEST_PROJECT_MAX", raising=False)
    monkeypatch.setenv("TTP_TEST_POOL", "32")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "8")
    host = checklane.project_cap({})
    assert host == 4 and checklane.cap_note({}) == "", "unset: host-sized, nothing to note"
    project.set_account_setting("checks.test_project_max", 3)
    assert checklane.project_cap({}) == 3 and checklane.fixed_cap({}) == (3, "account")
    for auto in ("auto", " AUTO "):
        assert checklane.project_cap({"checks": {"test_project_max": auto}}) == host, auto
        assert checklane.fixed_cap({"checks": {"test_project_max": auto}}) is None
    assert checklane.project_cap({}, fixed=False) == host, "fixed False: the host's size whatever is set"
    layered = project.layered({})
    assert layered["checks"]["test_project_max"] == 3, "a layered config carries the account's value"
    assert checklane.fixed_cap(layered) == (3, "account"), "and it is named as the account's"
    assert checklane.project_cap(project.layered({"checks": {"test_project_max": "auto"}})) == host, \
        "the project's auto wins over the account's number in a layered config"
    note = checklane.cap_note(layered)
    assert "set in the account settings" in note and f"the host fits {host} now" in note and "'auto'" in note, note
    assert checklane.cap_note({"checks": {"test_project_max": 5}}) == "", "a fixed number above the host's: no note"
    project.set_account_setting("checks.test_project_max", "auto")
    monkeypatch.setenv("TTP_TEST_PROJECT_MAX", "2")
    assert checklane.project_cap({}) == host, "the account's auto keeps the environment's number off"
    project.set_account_setting("checks.test_project_max", None)
    assert checklane.fixed_cap({}) == (2, "environment")
    assert "TTP_TEST_PROJECT_MAX" in checklane.cap_note({})
    # The waiter at the cap says where the fixed number comes from and what the host fits.
    a = testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, cap=1, wait_s=0)
    told = []
    # No wall-clock deadline (wait_s), which a slow first poll under load passes before the first line:
    # the wait ends once it has told (stop), however long that took, or after a minute that never tells.
    began = time.time()
    with pytest.raises(checkcap.Cancelled):
        testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, cap=1, tell=told.append, cap_note=note,
                             stop=lambda: "told" if told else "timeout" if time.time() - began > 60 else "")
    assert told, "the waiter at the cap told nothing within a minute"
    assert "(checks.test_project_max 3, set in the account settings; the host fits" in told[0], told
    a.close()
    # config_set takes auto for the cap, not for the test-worker budget, which is a number of slots.
    conv = coordinator.settable("checks.test_project_max")
    assert conv("auto") == "auto" and conv(" Auto ") == "auto" and conv(3) == 3
    cli.main(["config", "--chat-settable", "checks.test_project_max", "auto"])
    assert 'accepted as "auto"' in capsys.readouterr().out
    with pytest.raises(ValueError, match="whole number of at least 1$"):
        coordinator.settable("checks.test_project_slots")("auto")
    with pytest.raises(ValueError, match='or "auto"'):
        conv("automatic")


@pytest.mark.parametrize("key", ["checks.test_project_max", "checks.test_project_slots"])
def test_checks_test_project_max_is_chat_settable_as_a_whole_number_of_at_least_one(env, capsys, key):
    from ttp import cli, coordinator, project
    for v in ("3", "1"):
        cli.main(["config", "--chat-settable", key, v])
        assert f"accepted as {v}" in capsys.readouterr().out
    conv = coordinator.settable(key)
    assert conv(4) == 4 and conv(" 2 ") == 2
    for bad in ("0", "1.5", "x", '{"slots": 2}', "true", "-1"):
        with pytest.raises(SystemExit) as e:
            cli.main(["config", "--chat-settable", key, bad])
        assert e.value.code == 1 and "refused" in capsys.readouterr().out, bad
    for bad in (0, 1.5, True, {"slots": 2}, [2], None):
        with pytest.raises(ValueError):
            conv(bad)
    with pytest.raises(ValueError):   # the section itself takes no scalar
        coordinator.settable("checks")
    assert project.unknown_key_hint(key) is None, "a project-level checks key"


def test_a_wrapper_lock_or_project_cap_wait_names_its_holder_and_raises_the_lane_wait_event(
        env, tmp_path, monkeypatch):
    from ttp import checkcap, checklane, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setenv("TTP_TASK", "12")
    hold = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, lock=_WRAP_A, what="pytest -q tests/dev")
    monkeypatch.delenv("TTP_TASK")
    tickets = []
    try:
        for base in (testslots.suite_base("pa", "w" + _WRAP_A), testslots.suite_base("pa", _LANE_REPO)):
            tickets.append(locks.enqueue(checkcap.slot_dir(), base, "task #13 pa: full checks (run 9, pid 1)",
                                         time.time() - 1000, priority=2))
            ev = checklane.wait_events("pa", {})
            assert ev and all("#12" in x["text"] and "#13" in x["text"] for x in ev), (base, ev)
            assert all("checks.wrap_cmd" in x["text"] for x in ev)
            locks.dequeue(tickets.pop())
        lines = checklane.status_text("pa", {})
        assert "wrapper lock " + _WRAP_A in lines and "#12" in lines, lines
    finally:
        for t in tickets:
            locks.dequeue(t)
        hold.close()


def test_a_priority_1_check_starts_before_routine_ones_across_the_projects_lanes(env, tmp_path, monkeypatch):
    from ttp import checkcap, locks, testslots
    _lane_env(monkeypatch)
    hold = testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, cap=1, priority=5)
    order = []

    def wait(name, prio, **kw):
        g = testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, cap=1, priority=prio, wait_s=30, **kw)
        order.append(name)
        time.sleep(0.2)
        g.close()

    def queued():
        return sum(len(locks.queued(checkcap.slot_dir(), b)) for b in testslots.suite_bases("pa"))
    threads = []
    for name, prio, kw in (("locked routine", 5, {"lock": _WRAP_A}), ("routine", 5, {}),
                           ("routine 2", 6, {}), ("urgent", 1, {})):
        threads.append(threading.Thread(target=wait, args=(name, prio), kwargs=kw))
        threads[-1].start()
        assert _wait_for(lambda: queued() == len(threads)), name
    hold.close()
    for t in threads:
        t.join(60)
    assert order == ["urgent", "locked routine", "routine", "routine 2"], order


def test_a_priority_1_check_starts_beside_routine_holders_only_with_headroom_and_another_worktree(
        env, tmp_path, monkeypatch):
    from ttp import testslots
    _lane_env(monkeypatch)
    hold = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, priority=4, worktree="/w/one")
    try:
        for why, kw in (("no headroom", {"headroom": lambda: False, "worktree": "/w/two"}),
                        ("same worktree", {"headroom": lambda: True, "worktree": "/w/one"}),
                        ("routine waiter", {"headroom": lambda: True, "worktree": "/w/two", "prio": 3})):
            prio = kw.pop("prio", 1)
            assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, priority=prio, wait_s=0.3, **kw) is None, why
        told = []
        g = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, priority=1, wait_s=5, headroom=lambda: True,
                                 worktree="/w/two", tell=told.append)
        assert g and "-x." in g.files[0].name and any("priority 1" in x for x in told), told
        assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, priority=1, wait_s=0.3, headroom=lambda: True,
                                    worktree="/w/three") is None, "never beside another priority-1 run"
        g.close()
    finally:
        hold.close()


def test_a_p1_check_takes_the_urgent_extra_lane_at_a_busy_load_while_checks_host_max_has_room(env, monkeypatch):
    # #5201: three routine suites filled the lane and kept the load over the cores; the host-wide cap had
    # room, yet the p1 waited 12 min because headroom() asked for a load under half the cores.
    from ttp import checkcap, checklane, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "3")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.LIVE_ENV, "0")
    monkeypatch.setattr(checkcap, "_load", lambda: 90.0)
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: 90.0)
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    monkeypatch.setattr(checklane, "_mem_gib", lambda: 64.0)
    kw = dict(repo=_LANE_REPO, slots=2, wait_s=0.3, headroom=checklane.headroom)
    lanes = [testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, priority=p, worktree=f"/w/r{p}") for p in (2, 3)]
    host = [checkcap.acquire("pa routine", project_name="pa") for _ in range(2)]
    try:
        assert all(lanes) and all(host)
        assert checklane.headroom(), "a free host-wide slot is room, whatever the load average"
        assert testslots.suite_hold("pa", priority=2, worktree="/w/p2", **kw) is None, "a routine check waits"
        told = []
        g = testslots.suite_hold("pa", priority=1, worktree="/w/p1", tell=told.append, **kw)
        assert g and "-x." in g.files[0].name and any("priority 1" in x for x in told), told
        rec = locks.holder_records([Path(g.files[0].name)])[0]
        assert rec.get("urgent") and "urgent extra lane" in checklane.holder_text(rec, time.time())
        assert testslots.suite_hold("pa", priority=1, worktree="/w/p1b", **kw) is None, "one overflow per lane"
        g.close()
        host.append(checkcap.acquire("pa routine", project_name="pa"))   # checks.host_max reached
        assert host[-1] and not checklane.headroom()
        assert testslots.suite_hold("pa", priority=1, worktree="/w/p1", **kw) is None, "host_max full: p1 waits too"
        monkeypatch.setenv(checkcap.LIVE_ENV, "1")
        host.pop().close()
        monkeypatch.setattr(checkcap, "free_cores", lambda: 0.0)
        assert not checklane.headroom(), "no live cores for one more check"
        monkeypatch.setenv(checkcap.LIVE_ENV, "0")
        monkeypatch.setattr(checklane, "_mem_gib", lambda: 1.0)
        assert not checklane.headroom(), "no memory for one more lane"
    finally:
        for x in lanes + host:
            x and x.close()


def test_ttp_checks_behind_checks_wrap_cmd_take_the_wrapper_lane_not_the_repository_lane(
        env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, cli, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 0.3)
    monkeypatch.setattr(cli, "_checks_detach", lambda *a, **k: None)
    p.set_config("checks.slots", 1)
    p.set_config("checks.wrap_cmd", "/usr/bin/env")
    hold = testslots.suite_hold(p.name, repo=testslots.repo_key(repo))
    seen = tmp_path / "seen"
    try:
        cli.main(["checks", "--", f"sh -c 'touch {seen}'"])
        assert seen.exists(), "a run behind the wrapper lock waited behind the repository's lock-free run"
    finally:
        hold.close()


def test_a_quick_device_query_check_runs_beside_a_long_device_check_in_another_lane_and_status_names_both(
        env, tmp_path, monkeypatch, capsys):
    # A project with a tt-smi check and a long device check, behind different wrapper locks or none: they
    # run at once in separate lanes (a quick query once waited hours behind the long one), and
    # `ttp checks --slot-status --json` names each lane's group, never an empty repository.
    import json as _json
    from ttp import cli, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    key = testslots.repo_key(repo)
    long_run = testslots.suite_hold(p.name, repo=key, slots=1, lock=_WRAP_A, cap=4, wait_s=0,
                                    what="pytest -q tests/device_long")
    smi = testslots.suite_hold(p.name, repo=key, slots=1, cap=4, wait_s=0, what="tt-smi -s")
    smi_locked = testslots.suite_hold(p.name, repo=key, slots=1, lock=_WRAP_B, cap=4, wait_s=0, what="tt-smi -s")
    try:
        assert long_run and smi and smi_locked and smi.files and smi_locked.files, "no wait behind the long check"
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--slot-status", "--json"])
        assert e.value.code == 0
        out = _json.loads(capsys.readouterr().out)
        groups = [r["group"] for r in out["repos"]]
        assert len(groups) == 3 and all(g.strip() for g in groups) and all(r["repo"] for r in out["repos"]), out
        # The caller's own repository is named by its directory (lane_labels' known names), the locks by key.
        assert {"wrapper lock " + _WRAP_A, "wrapper lock " + _WRAP_B, "repository " + testslots.repo_name(repo)} == \
            {g.replace(" (checks.wrap_cmd, one at a time)", "") for g in groups}, groups
        assert {r["id"] for r in out["repos"]} == {key, "w" + _WRAP_A, "w" + _WRAP_B}, out
    finally:
        for g in (long_run, smi, smi_locked):
            g and g.close()


_CONCURRENT_CHECK = r'''
set -e
me=$1; other=$2; dir=$3
{ echo "port=$TTP_PORT_BASE"; echo "tmp=$TMPDIR"; echo "out=$TTP_CHECK_OUTPUT_DIR"; echo "pwd=$(pwd)"; } > "$dir/$me.env"
mkdir "$TMPDIR/scratch" && touch "$TTP_CHECK_OUTPUT_DIR/$me.mark"
touch "$dir/$me.up"
i=0; while [ ! -e "$dir/$other.up" ]; do i=$((i+1)); [ $i -lt 600 ] || exit 9; ''' + _nap() + """; done
"""


def test_two_full_checks_of_one_repository_run_at_once_without_sharing_ports_scratch_or_output(
        env, tmp_path, monkeypatch):
    """checks.slots 2: two `ttp checks` runs of one project and repository proceed together (each waits
    for the other inside its check) and get their own port block, TMPDIR, output folder and checkout."""
    from ttp import testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    p.set_config("checks.slots", 2)
    script = tmp_path / "both.sh"
    script.write_text(_CONCURRENT_CHECK)
    rds = []
    for me, other in (("a", "b"), ("b", "a")):
        rd = tmp_path / f"run-{me}"
        rd.mkdir()
        rds.append(rd)
        r = subprocess.run(
            [sys.executable, "-m", "ttp", "checks", "--detach", "--fresh", "--", "sh", str(script), me, other,
             str(tmp_path)], cwd=repo, capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_RUN_DIR": str(rd), "TTP_CHECKS_HOST_MAX": "4",
                 "TTP_RUN_ID": f"5{len(rds)}"})
        assert r.returncode == 0, r.stdout + r.stderr
    rcs = [rd / "checks.rc" for rd in rds]
    assert _wait_for(lambda: all(x.exists() for x in rcs), 120), [(rd / "checks.out").read_text() for rd in rds]
    assert [x.read_text().strip() for x in rcs] == ["0", "0"], [(rd / "checks.out").read_text() for rd in rds]
    seen = [dict(x.split("=", 1) for x in (tmp_path / f"{m}.env").read_text().split()) for m in "ab"]
    for k in ("port", "tmp", "out", "pwd"):
        assert seen[0][k] and seen[0][k] != seen[1][k], (k, seen)
    assert int(seen[0]["port"]) >= testslots.PORT_FIRST and int(seen[1]["port"]) >= testslots.PORT_FIRST
    assert abs(int(seen[0]["port"]) - int(seen[1]["port"])) >= testslots.PORT_SPAN


def test_port_blocks_are_distinct_while_held_and_free_with_their_holder(env, tmp_path, monkeypatch):
    from ttp import checkcap, testslots
    monkeypatch.setattr(checkcap, "slot_dir", lambda: tmp_path / "slots")
    (tmp_path / "slots").mkdir()
    monkeypatch.setattr(testslots, "PORT_BLOCKS", 2)
    a, b = testslots.port_block("run a"), testslots.port_block("run b")
    assert a and b and a[1][testslots.PORT_BASE_ENV] != b[1][testslots.PORT_BASE_ENV]
    assert a[1][testslots.PORT_COUNT_ENV] == str(testslots.PORT_SPAN)
    assert testslots.port_block("run c") is None, "all blocks held: none, never a shared one"
    a[0].close()
    c = testslots.port_block("run c")
    assert c and c[1] == a[1], "a freed block is taken again"
    b[0].close(), c[0].close()


def test_checks_in_the_authoring_worktree_hold_its_one_tree_lane(env, tmp_path, monkeypatch):
    from ttp import testslots
    _lane_env(monkeypatch)
    key = testslots.tree_key(tmp_path)
    assert key and key == testslots.tree_key(str(tmp_path) + "/") and key != testslots.tree_key(tmp_path / "x")
    a = testslots.suite_hold("pa", repo=key, slots=1, wait_s=0)
    assert a and testslots.suite_hold("pa", repo=key, slots=1, wait_s=0.2) is None, "one run per worktree"
    other = testslots.suite_hold("pa", repo=testslots.tree_key(tmp_path / "x"), slots=1, wait_s=0)
    assert other, "another worktree is not held up"
    assert testslots.project_held("pa") == [], "a worktree lane never counts against the project cap"
    a.close(), other.close()


def test_checks_slots_is_chat_settable_as_a_whole_number_only(env, capsys):
    from ttp import cli, coordinator
    assert coordinator.settable("checks.slots")("3") == 3
    for bad in ("2.5", "abc", "0", "65", "true", True, 1.5):
        with pytest.raises(ValueError):
            coordinator.settable("checks.slots")(bad)
    try:
        cli.main(["config", "--chat-settable", "checks.slots", "2"])
    except SystemExit as e:
        assert e.code in (0, None)
    with pytest.raises(SystemExit) as e:
        cli.main(["config", "--chat-settable", "checks.slots", "2.5"])
    assert e.value.code == 1


def test_a_lane_wait_and_the_status_line_name_the_limit_and_checks_slots(env, tmp_path, monkeypatch):
    from ttp import checklane, testslots
    _lane_env(monkeypatch)
    hold = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1)
    try:
        told = []
        assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, wait_s=0.3, tell=told.append) is None
        assert "one at a time per project and repository, checks.slots sets it" in told[0], told
        waiter = threading.Thread(target=testslots.suite_hold, args=("pa",),
                                  kwargs={"repo": _LANE_REPO, "slots": 1, "wait_s": 1.5})
        waiter.start()
        cfg = {"checks": {"slots": 1}}
        assert _wait_for(lambda: "checks.slots raises it" in "".join(checklane.lane_lines("pa", cfg)))
        assert "(1 at a time per repository; checks.slots raises it)" in checklane.lane_lines("pa", cfg)[0]
        waiter.join(10)
    finally:
        hold.close()


@pytest.mark.parametrize("source", ["project", "account", "env", "unset", "unset-bad-values"])
def test_slots_from_names_checks_slots_only_when_it_or_its_env_sets_the_count(env, monkeypatch, source):
    from ttp import checklane, project
    monkeypatch.delenv(checklane.SLOTS_ENV, raising=False)
    account = {}
    cfg = {"checks": {"slots": 2}} if source == "project" else {}
    if source == "account":
        account = {"checks": {"slots": 3}}
    elif source == "env":
        monkeypatch.setenv(checklane.SLOTS_ENV, "2")
    elif source == "unset-bad-values":   # values slots() ignores leave the count to the host's load
        cfg = {"checks": {"slots": "0"}}
        monkeypatch.setenv(checklane.SLOTS_ENV, "abc")
    monkeypatch.setattr(project, "load_account_settings", lambda: account)
    said = checklane.slots_from(cfg)
    if source.startswith("unset"):
        assert said == "sized from the host's load now (checks.slots unset)", said
    else:
        assert said == "", said


def test_a_lane_wait_line_names_the_host_s_load_when_checks_slots_is_unset(env, monkeypatch):
    from ttp import testslots
    _lane_env(monkeypatch)
    hold = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1)
    try:
        told = []
        assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=lambda held: 1, wait_s=0.3, tell=told.append,
                                    slots_from="sized from the host's load now (checks.slots unset)") is None
        assert ("one at a time per project and repository, sized from the host's load now (checks.slots unset)"
                in told[0] and "checks.slots sets it" not in told[0]), told
    finally:
        hold.close()


@pytest.mark.parametrize("set_by", ["checks.slots", "load"])
def test_ttp_checks_wait_record_and_probe_name_where_the_lane_count_comes_from(
        env, tmp_path, monkeypatch, capsys, set_by):
    """The lane count of a waiting run comes from checks.slots (or TTP_CHECKS_SLOTS) or, unset, from the
    host's load (checklane.capacity): its wait line, its CHECKS_SLOT_WAIT record and the `--result` probe
    say which, never 'checks.slots sets it' for a count the load set."""
    from ttp import checkcap, checklane, cli, testslots
    from ttp.runner import proc_start
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 0.3)
    monkeypatch.setattr(cli, "_checks_detach", lambda *a, **k: None)
    if set_by == "load":
        monkeypatch.delenv(checklane.SLOTS_ENV, raising=False)
        monkeypatch.setattr(checklane, "_explicit_slots", lambda cfg: None)   # neither project nor account
        # A loaded host, as a constant: a real host-load read can take longer than the wait window on a
        # busy host, and the wait record would never be written.
        loaded = {"n": 1, "floor": 1, "top": 1, "held": 0, "fit": 1, "cores": 4, "load": 4.0, "mem_gib": 16.0,
                  "run_cores": 4, "run_gib": checklane.GIB_PER_LANE}
        monkeypatch.setattr(checklane, "capacity", lambda *a, **k: dict(loaded))
    records, real = [], cli.write_json
    monkeypatch.setattr(cli, "write_json", lambda path, data, *a, **k: (
        records.append(dict(data)) if Path(path).name == cli.CHECKS_SLOT_WAIT else None, real(path, data, *a, **k))[1])
    hold = testslots.suite_hold(p.name, repo=testslots.repo_key(repo))
    assert hold.files
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--", "true"])
        assert e.value.code == 75
        said = (run / "checks.log").read_text() + capsys.readouterr().out
        rec = next(r for r in records if r.get("suite"))
        # The probe reads the record the wait wrote, of a run still alive (this process).
        cli.write_json(run / cli.CHECKS_PID, {"pid": os.getpid(), "started": proc_start(os.getpid()), "ts": time.time()})
        cli.write_json(run / cli.CHECKS_SLOT_WAIT, {**rec, "pid": os.getpid()})
        capsys.readouterr()
        assert _ttp("checks", "--result", str(run)) == 1
        probe = capsys.readouterr().out
    finally:
        hold.close()
    load = "sized from the host's load now (checks.slots unset)"
    if set_by == "load":
        assert f"one at a time per project and repository, {load}" in said, said
        assert rec.get("limit_from") == load, rec
        assert f"one at a time per project and repository ({load})" in probe and "slots sets it" not in probe, probe
    else:
        assert "one at a time per project and repository, checks.slots sets it" in said, said
        assert "limit_from" not in rec, rec
        assert "one at a time per project and repository (checks.slots sets it)" in probe, probe


def test_checks_suite_concurrency_is_a_chat_settable_alias_of_checks_slots(env, capsys):
    """checks.suite_concurrency (the setting's spec name) validates and stores as checks.slots."""
    from ttp import coordinator
    p = make(env)
    assert coordinator.settable("checks.suite_concurrency") is coordinator.settable("checks.slots")
    assert coordinator.settable("checks.suite_concurrency")("2") == 2
    for bad in ("2.5", "abc", "0", "65", True):
        with pytest.raises(ValueError):
            coordinator.settable("checks.suite_concurrency")(bad)
    # The alias is remapped to the canonical key before its value is validated or stored, so one setting
    # is never read from two places.
    assert coordinator.ALIASES == {"checks.suite_concurrency": "checks.slots"}
    # the config_set action (chat) remaps the alias before storing: one setting, one key
    coordinator.apply(p, [{"type": "config_set", "key": "checks.suite_concurrency", "value": "3"}])
    assert p.config()["checks"]["slots"] == 3 and "checks.suite_concurrency" not in p.config()["checks"]
    with pytest.raises(ValueError):
        coordinator.settable("checks.suite_concurrency")("2.5")   # never a non-integer


def test_slot_status_json_names_every_group_and_never_an_empty_repo(env, tmp_path, monkeypatch):
    """`ttp checks --slot-status --json` names each repository group; an unkeyed (legacy) group is
    named as not keyed, and a project directory with no git remote still has its repo key."""
    from ttp import checkcap, checklane, locks, testslots
    _lane_env(monkeypatch)
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    keyed = testslots.repo_key(repo)
    assert keyed, "repo_key comes from the git common dir (git rev-parse), not a remote"
    hold = hold2 = None
    try:
        # the project's own group (its repository), a foreign repository's group, and the unkeyed
        # (legacy, no repository key) group: slot-status names each, never an empty name
        hold = testslots.suite_hold(p.name, repo=keyed)
        hold2 = testslots.suite_hold(p.name, repo=_LANE_REPO)
        ticket = locks.enqueue(checkcap.slot_dir(), testslots._suite_resource(p.name, _LANE_REPO),
                              "task #77 x", time.time() - 60, priority=2)
        st = checklane.lane_status(p.name)
        repos = {r["repo"]: r for r in st}
        assert keyed in repos and _LANE_REPO in repos and len(st) == 2, st
        assert all(r["group"].strip() and r["group"] == checklane.group_name(r["repo"]) for r in st), st
        assert repos[_LANE_REPO]["waiters"][0]["task"] == "77" and repos[_LANE_REPO]["waiters"][0]["next"], st
    finally:
        locks.dequeue(ticket)
        for t in list(locks._TICKETS):
            locks.dequeue(t)
        if hold:
            hold.close()
        if hold2:
            hold2.close()


def test_two_checks_of_one_project_with_different_or_no_locks_run_together(env, tmp_path, monkeypatch, capsys):
    """A tt-smi-style check and a long device check of one project, each behind its own wrapper lock
    (or none), run at the same time: neither waits behind the other or the project's full checks."""
    from ttp import checkcap, cli, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 0.5)
    monkeypatch.setattr(cli, "_checks_detach", lambda *a, **k: None)
    from ttp import locks
    done1, done2 = tmp_path / "done1", tmp_path / "done2"
    try:
        # The project's one suite lane in its repository is held, so a run that took it would wait: a run
        # behind a wrapper lock takes that lock's own lane (checklane.wrap_key) instead.
        p.set_config("checks.slots", 1)
        hold = testslots.suite_hold(p.name, repo=testslots.repo_key(repo), slots=1)
        p.set_config("checks.wrap_cmd", f"flock {tmp_path / 'device.lock'}")
        cli.main(["checks", "--fresh", "--", "sh", "-c", f"touch {done1}"])
        out = capsys.readouterr().out
        assert done1.exists() and "waiting for this project's other full checks" not in out, out
        p.set_config("checks.wrap_cmd", f"flock {tmp_path / 'smi.lock'}")
        cli.main(["checks", "--fresh", "--", "sh", "-c", f"touch {done2}"])
        out = capsys.readouterr().out
        assert done2.exists() and "waiting for" not in out, "a different lock (or none): never waits behind the other check"
    finally:
        for t in list(locks._TICKETS):
            locks.dequeue(t)
        hold.close()



def _wrap_lane_held(project_name: str, key: str) -> list:
    from ttp import locks, testslots
    return locks.holder_records(testslots.lane_paths(project_name, 1, "w" + key))


def _run_behind_held_resource(env, tmp_path, monkeypatch, lane: str | None, crash: bool = False,
                              slow_start: float = 0) -> dict:
    """`ttp checks` behind checks.wrap_cmd `flock <resource>` while this test holds the resource: what the
    wrapper lock's lane looked like while the run queued on the resource, once the command ran, and after.
    The wrapper marks its start before it queues on the resource; the lane is read once it has.
    `slow_start`: seconds the run spends before its wrapper starts, as on a loaded host."""
    import fcntl
    import signal
    from ttp import checkcap, checklane, cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 5)
    monkeypatch.setattr(cli, "_checks_detach", lambda *a, **k: None)
    resource, started, go = tmp_path / "resource.lock", tmp_path / "started", tmp_path / "go"
    queued = tmp_path / "queued"
    p.set_config("checks.wrap_cmd", f"sh -c 'touch {queued}; exec flock {resource} \"$@\"' sh")
    if slow_start:
        problem = push.wrap_problem
        monkeypatch.setattr(push, "wrap_problem", lambda *a, **k: time.sleep(slow_start) or problem(*a, **k))
    if lane:
        p.set_config("checks.wrap_lane", lane)
    key = checklane.wrap_key(push.wrap_of(p.config()))
    seen: dict = {}
    res = open(resource, "a+")
    fcntl.flock(res, fcntl.LOCK_EX)

    def side():
        try:
            # The run is in its wrapper, which can only queue on the resource: nothing behind it runs yet.
            seen["wrapper_started"] = _wait_for(queued.exists, 60)
            seen["queued"] = _wrap_lane_held(p.name, key)
            seen["ran_early"] = started.exists()
            fcntl.flock(res, fcntl.LOCK_UN)
            seen["started"] = _wait_for(started.exists, 30)
            seen["running"] = _wrap_lane_held(p.name, key)
            if crash and seen["running"] and seen["running"][0]["pid"] != os.getpid():
                os.kill(seen["running"][0]["pid"], signal.SIGKILL)   # the step holding the lane dies
                seen["after_crash"] = _wait_for(lambda: not _wrap_lane_held(p.name, key), 10)
        finally:
            fcntl.flock(res, fcntl.LOCK_UN)
            go.touch()
    t = threading.Thread(target=side)
    t.start()
    try:
        cli.main(["checks", "--fresh", "--", f"sh -c 'touch {started}; while [ ! -e {go} ]; do sleep 0.05; done'"])
    except SystemExit as e:
        seen["exit"] = e.code
    finally:
        t.join(60)
        res.close()
    seen["after"] = _wrap_lane_held(p.name, key)
    seen["pid"] = os.getpid()
    return seen


def test_checks_wrap_lane_before_holds_the_wrapper_lane_while_the_run_queues_on_the_wrappers_lock(
        env, tmp_path, monkeypatch):
    # The default (and the premise of the change): the lane is taken before the wrapper starts, so runs
    # behind a wrapper that fails instead of waiting never go side by side; it is held during the queue.
    seen = _run_behind_held_resource(env, tmp_path, monkeypatch, None)
    assert seen["wrapper_started"] and seen["queued"] and seen["queued"][0]["pid"] == seen["pid"], seen
    assert seen["started"] and not seen["ran_early"] and not seen["after"], seen


def test_checks_wrap_lane_before_reads_the_queued_lane_once_a_slow_run_reaches_its_wrapper(
        env, tmp_path, monkeypatch):
    # A run slow to reach its wrapper (a loaded host) is read once it queues there, not after a fixed time.
    seen = _run_behind_held_resource(env, tmp_path, monkeypatch, None, slow_start=1.5)
    assert seen["wrapper_started"] and seen["queued"] and seen["queued"][0]["pid"] == seen["pid"], seen
    assert seen["started"] and not seen["ran_early"] and not seen["after"], seen


def test_checks_wrap_lane_after_takes_the_lane_only_once_the_wrappers_lock_is_held_and_frees_it_at_the_end(
        env, tmp_path, monkeypatch):
    seen = _run_behind_held_resource(env, tmp_path, monkeypatch, "after")
    assert seen["wrapper_started"] and seen["queued"] == [] and not seen["ran_early"], \
        f"a lane held while queued on the wrapper's lock: {seen}"
    assert seen["started"] and len(seen["running"]) == 1, seen
    holder = seen["running"][0]
    assert holder["pid"] != seen["pid"] and holder["alive"], "the step inside the wrapper holds it"
    assert holder.get("lock") and "full checks" in holder["holder"], holder
    assert seen["after"] == [] and seen.get("exit") in (None, 0), seen


def test_checks_wrap_lane_after_frees_the_lane_when_the_step_holding_it_crashes(env, tmp_path, monkeypatch):
    seen = _run_behind_held_resource(env, tmp_path, monkeypatch, "after", crash=True)
    assert seen["wrapper_started"] and seen["queued"] == [] and seen["started"] and len(seen["running"]) == 1, seen
    assert seen["after_crash"], "a killed step leaves no lane held"
    assert seen["after"] == [] and seen.get("exit") not in (None, 0), "the crash fails the check, never a pass"


def test_checks_wrap_lane_setting_and_where_the_lane_step_goes_in_a_checks_argv(env, tmp_path):
    from ttp import checklane, project, push
    assert push.wrap_lane_of({}) == "before" and push.wrap_lane_of({"checks": {"wrap_lane": ""}}) == "before"
    assert push.wrap_lane_of({"checks": {"wrap_lane": " After "}}) == "after"
    for bad in ("inside", 1, True, ["after"]):
        with pytest.raises(ValueError, match="checks.wrap_lane"):
            push.wrap_lane_of({"checks": {"wrap_lane": bad}})
    assert "wrap_lane" in project._known_keys(["checks"])
    assert any("checks.wrap_lane" in x for x in project.config_problems({"checks": {"wrap_lane": "x"}}, tmp_path))
    lane = checklane.lane_argv("pa", "aaaaaaaaaaaa", "flock dev.lock")
    assert lane[:2] == (sys.executable, "-I") and lane[-1] == "--" and "aaaaaaaaaaaa" in lane
    wrapped = push.with_wrap(["pytest -q", push.Check("make test")], ("flock", "/tmp/dev.lock"), lane)
    argv = push.check_argv(wrapped[0], isolate=False)
    assert argv[:2] == ["flock", "/tmp/dev.lock"] and tuple(argv[2:2 + len(lane)]) == lane, argv
    assert argv[2 + len(lane)].endswith(("bash", "sh")), "the command itself runs after the step"
    plain = push.check_argv(push.with_wrap(["pytest -q"], ())[0], isolate=False)
    assert not any("wraplane" in w for w in plain), "no wrapper: no lane step"
    before = push.check_argv(push.with_wrap(["pytest -q"], ("flock", "/tmp/dev.lock"))[0], isolate=False)
    assert not any("wraplane" in w for w in before), "checks.wrap_lane before: no step inside the wrapper"


def test_the_wrap_lane_step_waits_a_bounded_time_ignores_the_runs_nested_grants_and_keeps_the_exit_code(
        env, tmp_path, monkeypatch):
    from ttp import checkcap, checklane, testslots
    _lane_env(monkeypatch)
    key = "cccccccccccc"
    run = lambda cmd, wait="0.3", **e: subprocess.run(   # noqa: E731
        [*checklane.lane_argv("pa", key, "flock x", float(wait)), *cmd], capture_output=True, text=True,
        env={**os.environ, **e}, timeout=60)
    seen = tmp_path / "seen"
    probe = f"import json, sys; sys.path.insert(0, {str(Path(checklane.__file__).parent.parent)!r}); " \
            f"from ttp import locks, testslots; open({str(seen)!r}, 'w').write(json.dumps(" \
            f"[r['pid'] for r in locks.holder_records(testslots.lane_paths('pa', 1, 'w{key}'))]))"
    # Inside a run that holds its slots (the run's grants in its environment) it still takes the lane.
    r = run([sys.executable, "-c", probe], **{testslots.HELD_ENV: "4", checkcap.HELD_ENV: "1"})
    assert r.returncode == 0, r.stderr
    assert len(json.loads(seen.read_text())) == 1, "the step held the wrapper lock's lane while the command ran"
    assert not _wrap_lane_held("pa", key), "freed once the command ended"
    assert run(["sh", "-c", "exit 7"]).returncode == 7
    assert run(["sh", "-c", "kill -TERM $$"]).returncode == 128 + 15
    held = testslots.suite_hold("pa", lock=key, wait_s=0)
    assert held and held.files
    try:
        t0 = time.time()
        r = run(["sh", "-c", "echo ran"], wait="0.5")
        assert r.returncode == 0 and "ran" in r.stdout and time.time() - t0 < 30, r
        assert "stayed busy" in r.stderr and "running without it" in r.stderr, r.stderr
    finally:
        held.close()
    assert run([]).returncode == 2

def _slot_sizes(monkeypatch, pool: int, run: int, **more):
    from ttp import checkcap, testslots
    monkeypatch.setenv("TTP_TEST_POOL", str(pool))
    monkeypatch.setenv("TTP_TEST_RUN_MAX", str(run))
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.02")
    monkeypatch.setenv("TTP_TEST_PROJECT_WORKERS", "0")   # the budget's own rules, no worker ceiling (project_workers)
    for k in ("TTP_TEST_PROJECT_MAX", "TTP_TEST_PROJECT_SLOTS", testslots.HELD_ENV, checkcap.HELD_ENV):
        monkeypatch.delenv(k, raising=False)
    for k, v in more.items():
        monkeypatch.setenv(k, str(v))


def test_three_checks_at_once_with_a_grant_of_4_each_get_their_slots_inside_a_pool_of_32(env, monkeypatch):
    """checks.test_project_max counts checks, never slots: the slot budget is it times one run's grant,
    so 3 lanes run 3 suites at once instead of 1 holding the slots while the other 2 wait."""
    from ttp import checklane, testslots
    _slot_sizes(monkeypatch, 32, 4, TTP_TEST_PROJECT_MAX=3)
    assert checklane.project_cap({}) == 3 and testslots.project_max() == 12
    lanes = [testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, cap=3, wait_s=0, pending=True) for _ in range(3)]
    grants = [testslots.acquire(f"run {i}", project_name="pa", wait_s=0) for i in range(3)]
    try:
        assert all(x and x.files for x in lanes)
        assert [g and g.k for g in grants] == [4, 4, 4], "3 suites at once, 4 workers each"
        for x in lanes:
            testslots.slots_granted(x)
        assert len(testslots.project_held("pa")) == 3
        told = []
        assert testslots.acquire("run 3", project_name="pa", wait_s=0.5, tell=told.append) is None
        assert "its project holds its 12" in told[-1] and "checks.test_project_slots" in told[-1], told
        assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, cap=3, wait_s=0) is None, "the cap of 3 checks"
        other = testslots.acquire("other project", project_name="pb", wait_s=0)
        assert other and other.k == 4, "the pool's other 20 slots stay for other projects"
        other.close()
    finally:
        for g in grants + lanes:
            g and g.close()


@pytest.mark.parametrize("cores", [64, 16])
def test_the_account_cap_of_3_checks_with_the_default_run_max_splits_the_slot_budget_across_lanes(
        env, monkeypatch, cores):
    """checks.test_project_max 3 with the default checks.test_run_max: one run's grant is its share of the
    project's budget, never the whole of it, so two suites hold slots at the same time."""
    from ttp import checkcap, project, testslots
    _slot_sizes(monkeypatch, 0, 0)
    for k in ("TTP_TEST_POOL", "TTP_TEST_RUN_MAX"):   # the host's defaults: half the cores, min(8, half the pool)
        monkeypatch.delenv(k)
    monkeypatch.setattr(checkcap, "cores", lambda: cores)
    project.set_account_setting("checks.test_project_max", 3)
    pool, run = testslots.sizes()
    budget = testslots.project_max()
    assert budget == min(pool, 3 * run) and run < budget, (pool, run, budget)
    grants = []
    try:
        for i in range(min(3, budget // run)):
            grants.append(testslots.acquire(f"run {i}", project_name="pa", wait_s=0))
        assert len(grants) >= 2 and all(g and g.k == run for g in grants), [g and g.k for g in grants]
        assert sum(g.k for g in grants) <= min(budget, pool)
    finally:
        for g in grants:
            g and g.close()

def test_a_lane_counts_against_the_project_cap_only_once_its_run_has_its_test_slots(env, monkeypatch):
    from ttp import testslots
    _slot_sizes(monkeypatch, 32, 4, TTP_TEST_PROJECT_MAX=1)
    waiting = testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, cap=1, wait_s=0, pending=True)
    try:
        assert waiting and testslots.project_held("pa") == [], "still waiting for its slots: not counted"
        second = testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, cap=1, wait_s=0, pending=True)
        assert second and second.files, "a run waiting for slots never keeps another from its turn"
        second.close()
        testslots.slots_granted(waiting)
        assert len(testslots.project_held("pa")) == 1
        assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=2, cap=1, wait_s=0, pending=True) is None
        old = testslots.suite_hold("pb", repo=_LANE_REPO, slots=2, cap=1, wait_s=0)
        assert len(testslots.project_held("pb")) == 1, "a lane taken without `pending` (an older runtime) counts"
        old.close()
    finally:
        waiting.close()


def test_runs_waiting_for_their_host_grants_hold_neither_the_cap_nor_the_lane_and_running_stays_capped(
        env, monkeypatch):
    """A project cap and a repository's lanes full of runs only waiting in the host gates: the next run
    still takes a lane and queues in those gates, and claim_running keeps every running bound."""
    from ttp import testslots
    _slot_sizes(monkeypatch, 32, 4, TTP_TEST_PROJECT_MAX=2)
    two = lambda _held: 2   # noqa: E731
    hold = lambda **k: testslots.suite_hold("pa", repo=_LANE_REPO, slots=two, cap=2, wait_s=0,   # noqa: E731
                                            pending=True, **k)
    waiting = [hold(running=True), hold(running=True)]
    assert hold() is None, "without `running` the waiting runs keep the lanes (the old behaviour)"
    later = [hold(running=True), hold(running=True)]
    try:
        assert all(g and g.files for g in waiting + later), "runs waiting for their grants keep nobody out"
        assert testslots.project_held("pa") == [] and testslots._numbered_held(
            testslots.suite_base("pa", _LANE_REPO), running=True) == 0
        assert testslots.claim_running(later[1], "pa", two, 2), "the run granted first starts first"
        assert testslots.claim_running(waiting[0], "pa", two, 2)
        assert not testslots.claim_running(later[0], "pa", two, 2), "two running: the bound holds"
        assert not testslots.claim_running(later[0], "pa", two, 2, mark=False)
        assert len(testslots.project_held("pa")) == 2, "a refused claim marks nothing"
        assert testslots._pending(Path(later[0].files[0].name))
        waiting[0].close()
        assert testslots.claim_running(later[0], "pa", two, 2, mark=False), "room once a running suite ends"
        assert testslots.claim_running(later[0], "pa", two, 2)
        assert not testslots.claim_running(waiting[1], "pa", two, 3), "the repository's 2 lanes, under a cap of 3"
        assert len(testslots.project_held("pa")) == 2
    finally:
        for g in waiting + later:
            g.close()
    assert testslots.project_held("pa") == []


def test_claim_running_bounds_only_repository_lanes_and_never_counts_a_worktree_lane_or_its_own_twice(
        env, tmp_path, monkeypatch):
    from ttp import locks, testslots
    _lane_env(monkeypatch)
    one = lambda _held: 1   # noqa: E731
    tree = testslots.suite_hold("pa", repo=testslots.tree_key(tmp_path), wait_s=0, pending=True, running=True)
    suite = testslots.suite_hold("pa", repo=_LANE_REPO, slots=one, cap=1, wait_s=0, pending=True, running=True)
    wrap = testslots.suite_hold("pa", repo=_LANE_REPO, lock="0123456789ab", cap=2, wait_s=0, pending=True,
                                running=True)
    try:
        suite.files += tree.files
        assert testslots.claim_running(suite, "pa", one, 1), "its worktree lane is not a second check on the cap"
        assert len(testslots.project_held("pa")) == 1
        rec = locks.holder_records([Path(wrap.files[0].name)])[0]
        assert "running" not in rec, "a wrapper lock's one lane keeps its own bound"
        assert not testslots.claim_running(wrap, "pa", one, 1), "the project's cap of 1 is full"
        assert testslots.claim_running(wrap, "pa", one, 2), "its lock lane, never the repository's lanes"
    finally:
        for g in (suite, wrap):
            g.close()


def test_ttp_checks_granted_with_its_running_bound_full_hands_its_grants_back_and_runs_once_there_is_room(
        env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, cli, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _slot_sizes(monkeypatch, 6, 3)
    from ttp import locks
    real, calls = testslots.claim_running, []

    def claim(grant, *a, mark=True, **k):
        calls.append((mark, len(locks.held_labels(checkcap.all_paths(8))), len(locks_held(testslots))))
        return real(grant, *a, mark=mark, **k) if len(calls) > 2 else False   # full twice

    monkeypatch.setattr(testslots, "claim_running", claim)
    seen = tmp_path / "seen"
    cli.main(["checks", "--", "sh", "-c", f'echo "$TTP_TEST_SLOTS" > {seen}'])
    assert seen.read_text().strip() == "3", "it ran with its grant once there was room"
    assert [c[0] for c in calls] == [True, False, False, True], calls   # refused, waits, room, claims
    assert calls[0][1:] != (0, 0) and calls[1][1:] == (0, 0), \
        "granted it held its slots; waiting for room it holds no checks or test slot, only its lane"
    assert "handed its grants back" in (run / "checks.log").read_text()
    assert not locks_held(testslots) and not locks.held_labels(checkcap.all_paths(8)), "released at the end"


def test_lane_waits_name_the_project_cap_when_the_repository_s_other_lanes_only_wait_for_their_grants(
        env, monkeypatch):
    """Reader and writer agree: suite_hold with `running` counts only running suites against a
    repository's lanes, so waits(), held_lanes and the slots note count the same, and a waiter in a
    repository with a free running lane is held by the full project cap, not by its lanes."""
    from ttp import checkcap, checklane, locks, testslots
    _slot_sizes(monkeypatch, 32, 4, TTP_TEST_PROJECT_MAX=2)
    p = make(env)
    p.set_config("checks.slots", 3)
    cfg = p.config()
    three = lambda _held: 3   # noqa: E731
    other = "abcdef012345"
    hold = lambda repo, cap=2: testslots.suite_hold(p.name, repo=repo, slots=three, cap=cap, wait_s=0,   # noqa: E731
                                                    pending=True, running=True, who="run 1")
    a = [hold(_LANE_REPO) for _ in range(3)]
    b = hold(other)
    ticket = None
    try:
        assert all(g and g.files for g in a + [b])
        assert testslots.claim_running(a[0], p.name, three, 2) and testslots.claim_running(b, p.name, three, 2)
        assert len(testslots.project_held(p.name)) == 2, "the project cap of 2 is full of running suites"
        assert checklane.held_lanes(p.name, _LANE_REPO) == 1, "two of A's three lanes only wait for grants"
        wait = lambda: locks.enqueue(checkcap.slot_dir(), testslots._suite_resource(p.name, _LANE_REPO),  # noqa: E731
                                     "task #41 x", time.time() - 60, priority=2)
        ticket = wait()
        w = [x for x in checklane.waits(p.name, cfg) if x["task"] == "41"]
        assert len(w) == 1 and w[0]["limit"] == "project_cap", w
        assert w[0]["waits_on"] == checklane.CAP_LABEL
        a[0].close()
        b.close()
        assert checklane.held_lanes(p.name, _LANE_REPO) == 0
        assert testslots.claim_running(a[1], p.name, three, 2) and testslots.claim_running(a[2], p.name, three, 2)
        locks.dequeue(ticket)
        a.append(hold(_LANE_REPO, cap=3))
        ticket = wait()
        assert a[-1] and testslots.claim_running(a[-1], p.name, three, 3), "the third running lane"
        w = [x for x in checklane.waits(p.name, cfg) if x["task"] == "41"]
        assert w[0]["limit"] == "lanes" and len(w[0]["holders"]) == 3, w
        assert all(h.get("granted") is not False for h in w[0]["holders"])
    finally:
        ticket and locks.dequeue(ticket)
        for g in a + [b]:
            g and g.close()



def test_the_cap_label_shows_only_at_the_cap_and_below_it_the_wait_names_the_host_gate_and_its_run(
        env, monkeypatch):
    """A waiter with a free lane waits on the project cap only while the project's running checks fill it
    (testslots.project_held); below it, the wait names the host gate a lane holder of the project records
    (lane_waits, gate_lanes), else nothing. A wait with no task names its run (its ticket's label)."""
    from ttp import checkcap, checklane, locks, testslots
    _lane_env(monkeypatch)
    _slot_sizes(monkeypatch, 32, 4, TTP_TEST_PROJECT_MAX=2)
    p = make(env)
    p.set_config("checks.slots", 3)
    p.set_config("checks.wait_event_s", 1)
    cfg = p.config()
    # the waiter's process reads as gone: this test's own environment and worktree must not name its task
    monkeypatch.setattr(checklane, "_proc", lambda pid: {"command": "", "cwd": "", "ppid": None, "sid": None, "env": None})
    three = lambda _held: 3   # noqa: E731
    hold = lambda: testslots.suite_hold(p.name, repo=_LANE_REPO, slots=three, cap=2, wait_s=0,   # noqa: E731
                                        pending=True, running=True, who="run 1")
    gs = [hold(), hold()]
    ticket = None
    try:
        assert all(g and g.files for g in gs) and testslots.claim_running(gs[0], p.name, three, 2)
        ticket = locks.enqueue(checkcap.slot_dir(), testslots._suite_resource(p.name, _LANE_REPO),
                               f"{p.name}: full checks (run 99, pid 4242)", time.time() - 60, priority=2)
        w = checklane.waits(p.name, cfg)
        assert len(w) == 1 and len(testslots.project_held(p.name)) == 1, w
        assert w[0]["waits_on"] == "nothing (about to start)" and w[0]["limit"] == "", "1 of 2 held: no cap"
        assert w[0]["who"] == "run 99 (no task)" and w[0]["run"] == "99", w
        # the other lane's run waits under the host's load guard: the wait names that gate, never the cap
        testslots.lane_waits(gs[1], testslots.LOAD_WAITS + "1-minute load 70.0 above its 64 cores")
        w = checklane.waits(p.name, cfg)
        assert w[0]["limit"] == "host" and w[0]["waits_on"] == (
            checklane.HOST_GATE + testslots.LOAD_WAITS + "1-minute load 70.0 above its 64 cores"), w
        testslots.gate_lanes(gs[1], "the host test-slot pool is full")   # pool_gate's reason wins
        ev = checklane.wait_events(p.name, cfg)
        assert len(ev) == 1 and ev[0]["task"] is None, ev
        assert "run 99 (no task) has waited" in ev[0]["text"], ev
        assert "on the host's gate: the host test-slot pool is full" in ev[0]["text"], ev
        assert "project cap" not in ev[0]["text"].split("Lanes:")[0] and "held by the host's gate" in ev[0]["text"]
        assert "run 99 (no task) has waited" in checklane.long_wait_lines(p.name, cfg, over_s=1)[0]
        # the project's running checks fill its cap of 2: now, and only now, the cap
        assert testslots.claim_running(gs[1], p.name, three, 2)
        w = checklane.waits(p.name, cfg)
        assert w[0]["waits_on"] == checklane.CAP_LABEL and w[0]["limit"] == "project_cap", w
        assert "held by the project cap (checks.test_project_max)." in checklane.wait_events(p.name, cfg)[0]["text"]
        # a waiter whose label names no run still says whose wait it is
        assert checklane._waiter_name("", f"{p.name}: full checks (pid 7)") == "a run with no task (full checks (pid 7))"
        assert checklane._waiter_name("41", "task #41 x (run 3)") == "#41"
    finally:
        ticket and locks.dequeue(ticket)
        for g in gs:
            g and g.close()


def test_a_long_holder_is_a_likely_hang_only_when_its_log_and_cpu_stalled_else_slow_under_load(monkeypatch):
    """checks.wait_hang_factor x the median alone says a holder is long, not hung: a likely hang needs
    checkstall's stalled view (its log not grown for its threshold) and, where the daemon's CPU reading
    of its tree is known, that CPU idle. A long holder still progressing is named slow under load, with
    the host's load and cores, and wakes nobody."""
    from ttp import checkcap, checklane, checkstall
    monkeypatch.setattr(checkcap, "_load", lambda: 63.0)
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    t0 = 1_000_000.0
    now = t0 + 3600   # 6x the 10 min median: long
    h = {"task": "12", "pid": 4242, "run_since": t0, "log": "/scratch/run/checks.log", "command": "pytest -q"}
    cfg = {"checks": {"wait_hang_factor": 3}}
    evs = _queue_events(1, holders=[h])
    grew = {"at": now - 5}
    stat = lambda path: (grew["at"], 1000)   # noqa: E731 - the log's mtime and size
    moving = {checkstall.key(h): {"cpu": 2000.0, "at": now - 30}}   # its tree used CPU in the last reading
    idle = {checkstall.key(h): {"cpu": 2000.0, "at": t0 + 100}}

    why, inc = checklane.wait_wake(evs, None, cfg, now, 600.0, marks=moving, stat=stat)
    assert why == ["new"] and inc["slow"] and not inc["hang"], (why, inc)
    text = checklane.wake_text(why, evs, inc, cfg, now, 600.0)
    assert "slow under load (CPU demand 63.0 cores on 64 cores), not hung" in text and "likely hang" not in text, text
    assert checklane.wait_wake(evs, inc, cfg, now + 60, 600.0, marks=moving, stat=stat)[0] == [], "slow: no wake"
    # bypasses: its log stalled but its tree busy (left to the wall-time cap); stall watch off; no log named
    grew["at"] = t0 + 100
    assert checklane.wait_wake(evs, {"start": 1.0}, cfg, now, 600.0, marks=moving, stat=stat)[0] == []
    off = {"checks": {"wait_hang_factor": 3, "stall_s": 0}}
    assert checklane.wait_wake(evs, {"start": 1.0}, off, now, 600.0, marks=idle, stat=stat)[0] == []
    nolog = _queue_events(1, holders=[{k: v for k, v in h.items() if k != "log"}])
    assert checklane.wait_wake(nolog, {"start": 1.0}, cfg, now, 600.0, marks=idle, stat=stat)[0] == []
    # stalled: log still for 58 min and the tree idle as long, or the log alone where no reading is known
    for marks in (idle, {}, None):
        why, inc = checklane.wait_wake(evs, {"start": 1.0}, cfg, now, 600.0, marks=marks, stat=stat)
        assert why == ["hang"] and not inc["slow"], (marks, why, inc)
    assert "its log stopped growing, its CPU idle: a likely hang" in checklane.wake_text(why, evs, inc, cfg, now, 600.0)
    # short of the factor: neither hung nor slow, however still its log
    assert checklane.wait_wake(evs, {"start": 1.0}, cfg, t0 + 1700, 600.0, marks=idle, stat=stat)[0] == []


def test_urgent_and_p1_runs_reach_the_host_queue_past_lanes_held_by_runs_only_queued_for_a_host_slot(
        env, monkeypatch):
    """12 runs hold the repository's 12 places: 2 run (the project's cap of 2 is full) and 10, one of them
    a p1, only wait for a host-wide checks slot (none headroom or the over-cap lane lets past). A p1 and an urgent run
    still take a place at once, even with no headroom, and queue for the host-wide slot, where the urgent
    one preempts a routine run by the existing rule; a routine run still waits, and running stays capped."""
    import threading
    from ttp import checkcap, testslots
    _slot_sizes(monkeypatch, 32, 4)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")
    monkeypatch.setattr(checkcap, "POLL_S", 0.1)
    twelve = lambda _held: 12   # noqa: E731
    hold = lambda prio, **k: testslots.suite_hold(   # noqa: E731
        "pa", repo=_LANE_REPO, slots=twelve, cap=2, wait_s=0, pending=True, running=True, priority=prio,
        headroom=lambda: False, **k)
    places = [hold(2 + i % 2) for i in range(11)] + [hold(1)]   # the p1 only waits for a host slot
    host, got, told = [], [], []
    try:
        assert all(g and g.files for g in places)
        assert testslots.claim_running(places[0], "pa", twelve, 2) and testslots.claim_running(places[1], "pa", twelve, 2)
        assert len(testslots.project_held("pa")) == 2 and len(locks_held_lanes("pa")) == 12
        assert hold(2) is None, "a routine run waits for the project's cap of running checks"
        p1, urgent = hold(1, who="p1 run"), hold(0, who="urgent run")
        assert p1 and p1.files and urgent and urgent.files, "never held behind runs only queued for a host slot"
        assert testslots._pending(Path(urgent.files[0].name)), "it holds its place waiting, not running"
        assert len(testslots.project_held("pa")) == 2, "no running bound moved"
        assert not testslots.claim_running(p1, "pa", twelve, 2), "a routine slot keeps the project's cap"
        # In the host queue: every host-wide slot is busy with a routine run, so the urgent run asks it to
        # give up its slot (checks.urgent_slots 0) and takes the slot it frees.
        host.append(checkcap.acquire("routine", priority=3, preemptible=True))
        t = threading.Thread(target=lambda: got.append(checkcap.acquire(
            "urgent run", priority=0, urgent=True, tell=told.append, max_wait_s=30)))
        t.start()
        assert _wait_for(lambda: checkcap.preempt_line("routine") in told), told
        host.pop().close()
        t.join(30)
        assert got and got[0] is not None and got[0].f is not None
    finally:
        checkcap.clear_preempt()
        for g in places + host + got + [locals().get("p1"), locals().get("urgent")]:
            g and g.close()
    assert testslots.project_held("pa") == []


def test_a_p1_ttp_checks_run_keeps_its_over_cap_lane_past_a_full_project_cap_before_the_at_once_place(
        env, monkeypatch):
    """In the ttp checks path (pending, running) the project's cap of 2 is full of running p2 runs. A p1
    run with headroom still takes the one over-cap lane and may run at once, never a numbered place that
    claim_running then holds behind the cap (review #7214: the at-once place is only the fallback)."""
    from ttp import locks, testslots
    _lane_env(monkeypatch)
    twelve = lambda _held: 12   # noqa: E731
    hold = lambda prio, repo, wt: testslots.suite_hold(   # noqa: E731
        "pa", repo=repo, slots=twelve, cap=2, wait_s=0, pending=True, running=True, priority=prio,
        worktree=wt, headroom=lambda: True)
    runs = [hold(2, _LANE_REPO, "/w/a"), hold(2, _REPO_B, "/w/b")]
    p1 = None
    try:
        assert all(g and g.files for g in runs)
        assert all(testslots.claim_running(g, "pa", twelve, 2) for g in runs)
        assert len(testslots.project_held("pa")) == 2
        p1 = hold(1, "c0ffee000000", "/w/c")
        assert p1 and p1.files and "-x." in p1.files[0].name, "a p1 takes the over-cap lane, not a numbered place"
        rec, = locks.holder_records([Path(p1.files[0].name)])
        assert rec.get("over_cap") is True
        assert testslots.claim_running(p1, "pa", twelve, 2), "it runs past the full cap at once"
    finally:
        for g in runs + [p1]:
            g and g.close()


def locks_held_lanes(project_name: str) -> list:
    from ttp import locks, testslots
    return [x for x in testslots.lane_paths(project_name, repo=None) if locks._slot_labels([x])]


def test_a_waiting_ttp_checks_run_takes_a_raised_checks_slots_without_a_restart(env, tmp_path, monkeypatch, capsys):
    """checks.slots raised while a run waits for its repository's one lane: the run reads it at its next
    poll and starts, never only once it restarts."""
    import threading
    from ttp import checkcap, cli, locks, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 20)
    monkeypatch.setattr(cli, "_checks_detach", lambda *a, **k: None)
    p.set_config("checks.slots", 1)
    key = testslots.repo_key(repo)
    hold = testslots.suite_hold(p.name, repo=key, slots=1)   # a running suite holds the one lane
    told = threading.Event()   # raised only once the run polled and waits, so it never starts on the new count
    real_hold = testslots.suite_hold

    def hold_told(project_name, tell=None, *a, **k):
        def tell_(x):
            "waiting for this project's other full checks" in x and told.set()
            tell and tell(x)
        return real_hold(project_name, tell_, *a, **k)
    monkeypatch.setattr(testslots, "suite_hold", hold_told)
    queued = lambda: locks.queued(checkcap.slot_dir(), testslots._suite_resource(p.name, key))   # noqa: E731
    raise_it = threading.Thread(target=lambda: _wait_for(queued) and told.wait(15)
                                and p.set_config("checks.slots", 2))
    raise_it.start()
    seen = tmp_path / "seen"
    try:
        t0 = time.time()
        cli.main(["checks", "--", f"sh -c 'touch {seen}'"])
        assert seen.exists() and time.time() - t0 < 15, "it waited out its bound on the old checks.slots"
        assert "waiting for this project's other full checks" in capsys.readouterr().out
    finally:
        raise_it.join(30)
        hold.close()


_RUNNING_HOLDER = """
import os, signal, sys, time
from pathlib import Path
from ttp import checklane, testslots
checklane.HEARTBEAT_S = 3600.0
g = testslots.suite_hold("pa", repo=sys.argv[1], slots=1, cap=1, wait_s=0, pending=True, running=True)
assert testslots.claim_running(g, "pa", lambda _held: 1, 1)
Path(sys.argv[2]).write_text("running")
while not Path(sys.argv[2] + ".end").exists():
    time.sleep(0.05)
how = Path(sys.argv[2] + ".end").read_text()
if how == "exit":
    g.close()
    sys.exit(0)
if how == "crash":
    raise RuntimeError("the suite crashed")
os.kill(os.getpid(), signal.SIGKILL)
"""


@pytest.mark.parametrize("how", ["exit", "crash", "kill"])
def test_a_run_counts_once_from_its_grant_and_frees_its_lane_on_exit_crash_or_sigkill(
        env, tmp_path, monkeypatch, how):
    from ttp import testslots
    _lane_env(monkeypatch)
    one = lambda _held: 1   # noqa: E731
    mine = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, cap=1, wait_s=0, pending=True, running=True)
    try:
        assert testslots.claim_running(mine, "pa", one, 1) and testslots.claim_running(mine, "pa", one, 1)
        assert len(testslots.project_held("pa")) == 1, "a second claim of the same run never counts twice"
    finally:
        mine.close()
    assert testslots.project_held("pa") == []
    got = tmp_path / "holder"
    proc = subprocess.Popen([sys.executable, "-c", _RUNNING_HOLDER, _LANE_REPO, str(got)],
                            env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_TASK": "12"},
                            stderr=subprocess.DEVNULL)
    nxt = None
    try:
        assert _wait_for(got.exists), "the holder never started running"
        assert len(testslots.project_held("pa")) == 1
        hold = lambda: testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, cap=1, wait_s=0,   # noqa: E731
                                            pending=True, running=True)
        assert hold() is None, "a running holder keeps its lane and the cap"
        (tmp_path / "holder.end").write_text(how)
        proc.wait(10)
        assert proc.returncode == {"exit": 0, "crash": 1, "kill": -9}[how]
        assert testslots.project_held("pa") == [], "the kernel drops its lane with the process"
        nxt = hold()
        assert nxt and testslots.claim_running(nxt, "pa", one, 1)
    finally:
        _stop(proc)
        nxt and nxt.close()


class _SlowClose:
    """A slot file whose close pauses after letting go, so a grant's release spans several waiter polls."""

    def __init__(self, f):
        self.f = f

    def close(self):
        self.f.close()
        time.sleep(0.1)


def _slow_release(grant):
    grant.files = [_SlowClose(f) for f in grant.files]
    return grant


def test_a_waiter_at_its_projects_budget_keeps_its_place_and_is_never_passed_by_a_later_one(env, monkeypatch):
    import threading
    from ttp import checkcap, locks, testslots
    _slot_sizes(monkeypatch, 8, 2, TTP_TEST_PROJECT_SLOTS=2)
    first = testslots.acquire("first", project_name="pa", wait_s=0)
    assert first and first.k == 2 and testslots.project_max() == 2
    line = f"{testslots.OWN}.{locks.group_id('pa')}"
    # (a) An earlier waiter still out of the host's line (at budget when it last looked) goes first:
    # a later one finds budget free but waits for it, deterministically.
    earlier = locks.enqueue(checkcap.slot_dir(), line, "earlier", time.time() - 60)
    first.close()
    told = []
    assert testslots.acquire("later", project_name="pa", wait_s=0.5, tell=told.append) is None
    assert "1 earlier waiter(s) of its project go first" in told[-1], told
    locks.dequeue(earlier)
    g = testslots.acquire("later", project_name="pa", wait_s=0)
    assert g and g.k == 2
    assert locks.queued(checkcap.slot_dir(), line) == [], "a granted waiter leaves its project's line"
    # (b) Real waiters: the earlier arrival is granted first, every time.
    got, order = {}, []

    def wait(name):
        x = testslots.acquire(name, project_name="pa", wait_s=30)
        order.append(name)
        got[name] = x

    def queued(n):
        deadline = time.time() + 20
        while len(locks.queued(checkcap.slot_dir(), line)) < n and time.time() < deadline:
            time.sleep(0.01)
        assert len(locks.queued(checkcap.slot_dir(), line)) == n

    ts = []
    for name, n in (("e", 1), ("l", 2)):
        ts.append(threading.Thread(target=wait, args=(name,)))
        ts[-1].start()
        queued(n)   # "l" arrives only once "e" holds its place
    # A release spans several polls (_slow_release): no waiter takes part of it while a later one takes the rest.
    _slow_release(g).close()
    ts[0].join(30)
    assert order == ["e"] and got["e"].k == 2, "the earlier waiter is granted first, its whole grant"
    _slow_release(got["e"]).close()
    ts[1].join(30)
    assert order == ["e", "l"] and got["l"].k == 2
    got["l"].close()


@pytest.mark.parametrize("pool,run,old", [(2, 1, 2), (4, 2, 4), (8, 4, 8), (16, 8, 16)])
def test_a_project_on_a_small_pool_keeps_its_old_budget_and_a_single_run_its_whole_share(env, monkeypatch, pool, run, old):
    """Unset, the budget was twice a run's share within the pool; on a pool that fits at most two runs
    the runs-it-fits x grant default is the same, and one run still gets its whole share at once."""
    from ttp import testslots
    _slot_sizes(monkeypatch, pool, run)
    assert testslots.project_max() == old
    g = testslots.acquire("one", project_name="pa", wait_s=0)
    assert g and g.k == run
    g.close()


def test_the_project_slot_budget_never_exceeds_the_host_pool(env, monkeypatch):
    from ttp import locks, testslots
    _slot_sizes(monkeypatch, 8, 4, TTP_TEST_PROJECT_MAX=10)
    assert testslots.project_max() == 8, "10 checks x 4 slots, within the pool of 8"
    monkeypatch.setenv("TTP_TEST_PROJECT_SLOTS", "100")
    assert testslots.project_max() == 8, "an explicit slot budget wins, within the pool"
    assert testslots.project_max({"checks": {"test_project_slots": 4}}) == 4, "a project's own only lowers it"
    monkeypatch.delenv("TTP_TEST_PROJECT_SLOTS")
    assert testslots.project_max({"checks": {"test_project_slots": 1}}) == 4, "never below one run's grant"
    held = [testslots.acquire(n, project_name=pr, wait_s=0) for n, pr in (("a", "pa"), ("b", "pa"), ("c", "pb"))]
    try:
        assert [g and g.k for g in held] == [4, 4, None], "a full pool grants nothing more"
        assert len(locks.held_labels(testslots.paths(8))) == 8
    finally:
        for g in held:
            g and g.close()


def test_a_projects_waiters_go_most_urgent_first_then_by_arrival_within_one_priority(env, monkeypatch):
    from ttp import checkcap, locks, testslots
    _slot_sizes(monkeypatch, 8, 2, TTP_TEST_PROJECT_SLOTS=2)
    line = f"{testslots.OWN}.{locks.group_id('pa')}"
    p2 = locks.enqueue(checkcap.slot_dir(), line, "earlier p2", time.time() - 60, priority=2)
    try:
        g = testslots.acquire("later p1", project_name="pa", wait_s=0, priority=1)
        assert g and g.k == 2, "a p1 waiter goes before an earlier p2 one"
        g.close()
        p1 = locks.enqueue(checkcap.slot_dir(), line, "earlier p1", time.time() - 30, priority=1)
        told = []
        assert testslots.acquire("later p2", project_name="pa", wait_s=0.5, priority=2, tell=told.append) is None
        assert "2 earlier waiter(s) of its project go first" in told[-1], told
        locks.dequeue(p1)
        told.clear()
        assert testslots.acquire("later p2", project_name="pa", wait_s=0.5, priority=2, tell=told.append) is None
        assert "1 earlier waiter(s) of its project go first" in told[-1], "within one priority, arrival order"
    finally:
        locks.dequeue(p2)
    g = testslots.acquire("later p2", project_name="pa", wait_s=0, priority=2)
    assert g and g.k == 2
    g.close()


def test_a_p1_check_takes_one_urgent_grant_beyond_its_projects_budget_held_by_less_urgent_runs(env, monkeypatch):
    from ttp import locks, testslots
    _slot_sizes(monkeypatch, 16, 4, TTP_TEST_PROJECT_SLOTS=4)
    long_p2 = testslots.acquire("long p2 check", project_name="pa", wait_s=0, priority=2)
    held = [long_p2]
    try:
        assert long_p2.k == 4
        assert testslots.acquire("p2 waiter", project_name="pa", wait_s=0, priority=2) is None, "at budget"
        assert testslots.acquire("no-task waiter", project_name="pa", wait_s=0) is None
        told = []
        urgent = testslots.acquire("p1 check", project_name="pa", wait_s=0, priority=1, tell=told.append)
        held.append(urgent)
        assert urgent and urgent.k == 4 and "urgent" in told[-1], "beside the p2 run, which keeps running"
        assert testslots.acquire("second p1", project_name="pa", wait_s=0, priority=1) is None, "one extra grant only"
        urgent.close()
        held.remove(urgent)
        top = testslots.acquire("top p1", project_name="pa", wait_s=0, priority=1)
        held.append(top)
        assert top and top.k == 4
        assert testslots.acquire("p1 behind a p1", project_name="pa", wait_s=0, priority=1) is None, \
            "a budget held by an equally urgent run gives no extra grant"
        for g in held:
            g.close()
        held = [testslots.acquire(n, project_name=pr, wait_s=0, priority=2) for n, pr in
                (("p2", "pa"), ("other a", "pb"), ("other b", "pc"), ("other c", "pd"))]
        assert [g and g.k for g in held] == [4, 4, 4, 4]
        assert testslots.acquire("p1 on a full pool", project_name="pa", wait_s=0, priority=1) is None, \
            "the urgent grant only takes free slots of the host's pool"
    finally:
        for g in held:
            g and g.close()


def test_upgraded_names_a_newly_installed_runtime_once_it_settled_and_never_twice(env, tmp_path, monkeypatch):
    import ttp
    from ttp import checkcap
    monkeypatch.setattr(checkcap, "_SEEN", {})
    monkeypatch.setenv(checkcap.REEXEC_ENV, "")
    init = tmp_path / "__init__.py"
    init.write_text(f'"""runtime"""\n__version__ = "{ttp.__version__}"\n')
    assert checkcap.upgraded(init, now=100.0) == "", "the version it runs"
    init.write_text('"""runtime"""\n__version__ = "99.0.0"\n')
    assert checkcap.upgraded(init, now=200.0) == "", "just installed: its other files may still be copying"
    assert checkcap.upgraded(init, now=200.0 + checkcap.UPGRADE_SETTLE_S) == "99.0.0"
    monkeypatch.setenv(checkcap.REEXEC_ENV, "99.0.0")
    assert checkcap.upgraded(init, now=9999.0, settle=0) == "", "re-executed for it once already: never a loop"
    init.write_text("no version here\n")
    assert checkcap.upgraded(init, now=9999.0, settle=0) == ""
    init.unlink()
    assert checkcap.upgraded(init, now=9999.0, settle=0) == "", "an unreadable install is no upgrade"


class _ReExec(BaseException):
    """What the stand-in for os.execv raises: the process would be replaced here."""


def _upgrade_child_checks(monkeypatch, upgrade, handover: bool = False) -> list:
    # cli.main's `ttp checks` as the detached child runs it (own_group: _checks_slot's `detached`), with
    # checkcap.upgraded answering `upgrade()` and os.execv recorded instead of run. With `handover` the
    # kept lane and slot fds are duplicated as an exec keeps them, so a second cli.main in this process
    # can adopt them as the re-executed run does while the old run's file objects close.
    from ttp import checkcap, checkspill, cli, testslots
    real = cli._checks_slot
    monkeypatch.setattr(cli, "_checks_slot",
                        lambda p, run_dir, out, head, detached=False, *a, **k: real(p, run_dir, out, head, True, *a, **k))
    monkeypatch.setattr(checkcap, "upgraded", upgrade)
    keys = (checkcap.ARRIVED_ENV, checkcap.REEXEC_ENV, checkspill.OFF_ENV, testslots.LANE_ENV, checkcap.KEPT_ENV)
    for k in keys:
        monkeypatch.setenv(k, "")   # restored after the test, whatever the re-exec set
    execs = []

    def execv(path, argv):
        # The lanes handed over: each still the open, inheritable file it names, labelled with this pid.
        lanes = [(os.get_inheritable(fd), os.path.samestat(os.fstat(fd), os.stat(name)),
                  json.loads(pathlib.Path(name).read_text()).get("pid"))
                 for fd, name in json.loads(os.environ.get(testslots.LANE_ENV) or "[]")]
        execs.append((path, list(argv), {k: os.environ.get(k) for k in keys}, lanes))
        if handover:
            if spec := os.environ.get(testslots.LANE_ENV):
                os.environ[testslots.LANE_ENV] = json.dumps([[os.dup(fd), name] for fd, name in json.loads(spec)])
            if spec := os.environ.get(checkcap.KEPT_ENV):
                fd, name = json.loads(spec)
                os.environ[checkcap.KEPT_ENV] = json.dumps([os.dup(fd), name])
        raise _ReExec()
    monkeypatch.setattr(os, "execv", execv)
    return execs


@pytest.mark.parametrize("waits_for", ["checks slot", "test slots"])
def test_a_waiting_detached_checks_run_reexecutes_with_an_upgraded_runtime_and_keeps_its_place(
        env, tmp_path, monkeypatch, waits_for):
    # A wrapper queued before a runtime upgrade adopts it while it waits (#4252: lanes held by wrappers
    # enforcing the old slot arithmetic), for the checks slot and, holding its lane, for its test slots.
    import threading
    from ttp import checkcap, checkspill, cli, locks, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    new = []
    execs = _upgrade_child_checks(monkeypatch, lambda: new[0] if new else "")
    arrived = time.time() - 600
    monkeypatch.setenv(checkcap.ARRIVED_ENV, repr(arrived))
    if waits_for == "checks slot":
        monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
        held, marker = [checkcap.acquire("other")], "queued for a host-wide checks slot"
    else:
        monkeypatch.setenv("TTP_TEST_POOL", "2")
        held = [testslots.acquire("other 1", project_name="px"), testslots.acquire("other 2", project_name="px")]
        marker = checkcap.PARKED_LINE   # #5452: it waits for them before it takes a host-wide checks slot
    ran, ended = tmp_path / "ran", []

    def go():
        try:
            cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
        except _ReExec:
            ended.append("exec")
    t = threading.Thread(target=go)
    t.start()
    try:
        assert _wait_for(lambda: marker in ((run / "checks.log").read_text() if (run / "checks.log").exists() else ""))
        assert not execs, "no upgrade installed: it waits on"
        new.append("99.0.0")
        t.join(30)
        assert not t.is_alive() and ended == ["exec"] and not ran.exists()
        (path, argv, envs, lanes), = execs
        assert path == sys.executable and argv == [sys.executable, "-m", "ttp", *sys.argv[1:]]
        assert envs[checkcap.REEXEC_ENV] == "99.0.0" and not envs[checkspill.OFF_ENV], "spill hosts stay allowed"
        log = (run / "checks.log").read_text()
        assert log.count("re-executing with it") == 1, log
        # It keeps its lane for the new process and lets go of its slots and queue places; the new process
        # queues for them at its old arrival.
        assert lanes == [(True, True, os.getpid())], lanes
        assert "keeping its checks lane" in log
        assert not locks.queued(checkcap.slot_dir(), checkcap.RESOURCE)
        assert not locks.queued(checkcap.slot_dir(), testslots.TESTS.resource)
        assert envs[checkcap.ARRIVED_ENV] == repr(arrived) and cli._checks_arrived() == arrived
    finally:
        for g in held:
            g.close()
        t.join(30)


class _SlotAsked(BaseException):
    """What the stand-in for _checks_slot raises once it saw the re-executed run's spill decision."""


@pytest.mark.parametrize("caller_off", ["", "1", "om-x"])
def test_a_reexecuted_checks_run_decides_spill_again_and_keeps_only_the_callers_no_spill(
        env, tmp_path, monkeypatch, caller_off):
    # #7419: a run the old code would not spill set OFF_ENV "1" on its re-exec, so after an upgrade that
    # lets it spill it never tried a spill host (66 waiters stalled up to 10 h). Only the caller's OFF_ENV
    # carries over ("1" always; an upgrade drops the hosts a rerun kept off); the new code decides
    # spillability again.
    import inspect
    import threading
    from ttp import checkcap, checkspill, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    params = inspect.signature(cli._checks_slot).parameters
    new = []
    execs = _upgrade_child_checks(monkeypatch, lambda: new[0] if new else "", handover=True)
    monkeypatch.setenv(checkcap.ARRIVED_ENV, repr(time.time() - 600))
    monkeypatch.setenv(checkspill.OFF_ENV, caller_off)
    verdict = [False]   # the old code's: these checks may not spill
    monkeypatch.setattr(checkspill, "spillable", lambda cmds, cfg=None: verdict[0])
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    held = checkcap.acquire("other")
    ran, ended = tmp_path / "ran", []

    def go():
        try:
            cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
        except _ReExec:
            ended.append("exec")
    t = threading.Thread(target=go)
    t.start()
    try:
        assert _wait_for(lambda: "queued for a host-wide checks slot" in (
            (run / "checks.log").read_text() if (run / "checks.log").exists() else ""))
        new.append("99.0.0")
        t.join(30)
        assert not t.is_alive() and ended == ["exec"] and not ran.exists()
        (_, _, envs, _), = execs
        kept = caller_off if checkspill.all_off(caller_off) else ""   # an upgrade drops the hosts kept off
        assert (envs[checkspill.OFF_ENV] or "") == kept, envs
        # The re-executed run, under code that lets these checks spill, decides again from what it was handed.
        verdict[0] = True
        new.clear()
        asked = []

        def slot(*a, **k):
            args = dict(zip(params, a), **k)
            asked.append((args.get("spill"), args.get("spill_off")))
            raise _SlotAsked()
        monkeypatch.setattr(cli, "_checks_slot", slot)
        with pytest.raises(_SlotAsked):
            cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
        assert asked == [(not checkspill.all_off(caller_off), kept)], asked
    finally:
        held.close()
        t.join(30)


def test_a_detached_checks_run_that_got_its_slots_runs_its_commands_and_is_never_reexecuted(env, tmp_path, monkeypatch):
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    execs = _upgrade_child_checks(monkeypatch, lambda: "99.0.0")   # an upgrade installed all along
    ran = tmp_path / "ran"
    cli.main(["checks", "--", "sh", "-c", f"touch {ran}; sleep 0.5"])
    assert ran.exists() and not execs, "only a waiting run re-executes; a running suite is never cut"
    assert "re-executing" not in (run / "checks.log").read_text()


def test_a_checks_run_waiting_for_test_slots_is_cancelled_and_frees_its_lane(env, tmp_path, monkeypatch):
    # checks-slot.cancel (the task ended; Daemon.reap_waiting_checks) reaches a run waiting for test slots
    # too, not only one waiting for its lane or the checks slot (#4283: such a wrapper held its lane).
    import threading
    from ttp import checkcap, cli, locks, testslots
    from ttp.runner import proc_start
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TEST_POOL", "2")
    held = [testslots.acquire("other 1", project_name="px"), testslots.acquire("other 2", project_name="px")]
    ran, ended = tmp_path / "ran", []

    def go():
        try:
            cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
        except SystemExit as e:
            ended.append(e.code)
    t = threading.Thread(target=go)
    t.start()
    try:
        # #5452: it waits for them before it takes a host-wide checks slot (checkcap.PARKED_LINE)
        assert _wait_for(lambda: checkcap.PARKED_LINE in (run / "checks.log").read_text()
                         if (run / "checks.log").exists() else False)
        assert locks.holder_records(testslots.lane_paths(p.name, None, None)), "it waits holding its lane"
        assert checkcap.state()["running"] == [], "and no host-wide checks slot"
        (run / cli.CHECKS_SLOT_CANCEL).write_text(json.dumps(
            {"pid": os.getpid(), "started": proc_start(os.getpid()), "why": "task #3 ended (done)"}))
        t.join(30)
        assert not t.is_alive() and ended and ended[0] not in (0, None) and not ran.exists()
        assert "task #3 ended (done)" in (run / "checks.log").read_text()
        assert not locks.holder_records(testslots.lane_paths(p.name, None, None)), "its lane is free"
        assert not locks.queued(checkcap.slot_dir(), testslots.TESTS.resource)
    finally:
        for g in held:
            g.close()
        t.join(30)


def test_adopt_lanes_takes_only_the_open_lane_files_it_names(env, tmp_path):
    from ttp import locks, testslots
    a = locks.try_take([tmp_path / "lane-a"], "run 1")
    b = tmp_path / "lane-b"
    b.write_text("{}")
    assert testslots.keep_lanes(testslots.Grant()) == "" and testslots.adopt_lanes("") is None
    assert testslots.adopt_lanes("not json") is None
    spec = testslots.keep_lanes(testslots.Grant(0, [a]))
    fd = a.fileno()
    assert os.get_inheritable(fd) and json.loads(spec) == [[fd, str(tmp_path / "lane-a")]]
    # A spec naming another file than its fd is refused and lets that fd go (no double owner).
    dup = os.dup(fd)
    assert testslots.adopt_lanes(json.dumps([[dup, str(b)]])) is None
    os.close(dup)
    dup = os.dup(fd)
    g = testslots.adopt_lanes(json.dumps([[dup, str(tmp_path / "lane-a")]]))
    assert g is not None and g.k == 0 and not os.get_inheritable(dup) and g.files[0].name == str(tmp_path / "lane-a")
    assert locks.try_take([tmp_path / "lane-a"], "run 2") is None, "still held"
    g.close()
    a.close()
    assert locks.try_take([tmp_path / "lane-a"], "run 2") is not None


def _rival_checks_slot(got: list):
    # Another project's run queued for the host-wide checks slot now: it appends the time it got one.
    import threading
    from ttp import checkcap

    def rival():
        s = checkcap.acquire("rival ttp checks", "rival", max_wait_s=60, project_name="rival")
        got.append(time.time())
        if s is not None:
            s.close()
    t = threading.Thread(target=rival)
    t.start()
    return t


def test_a_checks_run_granted_its_host_wide_slot_keeps_it_across_an_upgrade_reexec(env, tmp_path, monkeypatch):
    # A run granted the host's checks slot that still waits for test slots re-executes for an upgraded
    # runtime (note 44bd0bbfdb6de827: it gave the slot back and queued again at the back, four times): the
    # re-executed run holds the same slot, and a waiter queued meanwhile never gets it until the run ended.
    import threading
    from ttp import checkcap, cli, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    new = []
    execs = _upgrade_child_checks(
        monkeypatch, lambda: new[0] if new and os.environ.get(checkcap.REEXEC_ENV) != new[0] else "", handover=True)
    monkeypatch.setenv(checkcap.ARRIVED_ENV, repr(time.time() - 600))
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_TEST_POOL", "2")
    monkeypatch.setattr(testslots, "pool_room", lambda *a, **k: True)   # the slot is granted, then test slots wait
    held = [testslots.acquire("other 1", project_name="px"), testslots.acquire("other 2", project_name="px")]
    ran, ended, got = tmp_path / "ran", [], []
    log = lambda: (run / "checks.log").read_text() if (run / "checks.log").exists() else ""   # noqa: E731

    def go():
        try:
            cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
        except _ReExec:
            ended.append("exec")
        except SystemExit as e:
            ended.append(e.code)
    t = threading.Thread(target=go)
    t.start()
    rival = None
    try:
        assert _wait_for(lambda: "waiting for a host-wide" in log() and checkcap.state()["running"]), log()
        slot_label = checkcap.state()["running"]
        rival = _rival_checks_slot(got)
        assert _wait_for(lambda: checkcap.state()["queued"])
        new.append("99.0.0")
        t.join(30)
        assert not t.is_alive() and ended == ["exec"] and not ran.exists()
        (_, _, envs, _), = execs
        fd, name = json.loads(envs[checkcap.KEPT_ENV])
        assert json.loads(pathlib.Path(name).read_text()).get("pid") == os.getpid()
        assert "keeping its checks lane and its host-wide checks slot" in log()
        assert checkcap.state()["running"] == slot_label and not got, "held through the exec: the rival waits"
        # The re-executed run adopts its slot and lane and waits only for its test slots.
        t = threading.Thread(target=go)
        t.start()
        assert _wait_for(lambda: log().count("waiting for a host-wide") >= 2), log()
        assert checkcap.state()["running"] == slot_label and not got
        assert len(checkcap.state()["queued"]) == 1, "only the rival queues for a checks slot"
        for g in held:
            g.close()
        t.join(30)
        assert not t.is_alive() and ran.exists() and ended[1:] in ([], [0], [None]), ended
        rival.join(30)
        assert got and got[0] >= ran.stat().st_mtime, "the rival got the slot only once the run had ended"
        assert log().count("host-wide checks slot taken") <= 1, "the kept slot is never granted again"
    finally:
        for g in held:
            g.close()
        t.join(30)
        rival and rival.join(60)


def test_a_waiting_checks_run_reexecuted_for_an_upgrade_queues_again_at_its_first_arrival(env, tmp_path, monkeypatch):
    # A run still waiting for the checks slot when it re-executes queues again at its first arrival and
    # priority, so its ageing counts from then, never from the re-execution.
    import threading
    from ttp import checkcap, cli, locks
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    new = []
    execs = _upgrade_child_checks(
        monkeypatch, lambda: new[0] if new and os.environ.get(checkcap.REEXEC_ENV) != new[0] else "", handover=True)
    arrived = time.time() - 600
    monkeypatch.setenv(checkcap.ARRIVED_ENV, repr(arrived))
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    other = checkcap.acquire("other")
    ran, ended = tmp_path / "ran", []
    log = lambda: (run / "checks.log").read_text() if (run / "checks.log").exists() else ""   # noqa: E731
    tickets = lambda: [t.name for t in locks.queued(checkcap.slot_dir(), checkcap.RESOURCE)]   # noqa: E731

    def go():
        try:
            cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
        except _ReExec:
            ended.append("exec")
    t = threading.Thread(target=go)
    t.start()
    try:
        assert _wait_for(lambda: tickets())
        first, = tickets()
        assert first.startswith(f"{arrived:017.6f}-{os.getpid()}")
        new.append("99.0.0")
        t.join(30)
        assert ended == ["exec"] and not tickets()
        (_, _, envs, _), = execs
        assert not envs[checkcap.KEPT_ENV], "it held no checks slot to keep"
        t = threading.Thread(target=go)
        t.start()
        assert _wait_for(lambda: tickets())
        assert tickets() == [first], "the same place: arrival, pid and priority"
        other.close()
        t.join(30)
        assert not t.is_alive() and ran.exists()
        waited = re.search(r"host-wide checks slot taken after ([0-9.]+) min", log())
        assert waited and float(waited.group(1)) >= 10.0, log()
    finally:
        other.close()
        t.join(30)


def test_a_kept_checks_slot_is_freed_when_the_reexec_fails_or_its_process_dies(env, tmp_path, monkeypatch):
    import threading
    from ttp import checkcap, cli, testslots
    # (1) The exec fails: the run lets its slot go at once and waits on with its code (queued ahead of the
    # rival by its arrival, it takes a slot again: a new grant, never the kept one).
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    new = []
    _upgrade_child_checks(monkeypatch, lambda: new[0] if new and os.environ.get(checkcap.REEXEC_ENV) != new[0] else "")

    def execv(path, argv):
        raise OSError(8, "Exec format error")
    monkeypatch.setattr(os, "execv", execv)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_TEST_POOL", "2")
    monkeypatch.setattr(testslots, "pool_room", lambda *a, **k: True)
    held = [testslots.acquire("other 1", project_name="px"), testslots.acquire("other 2", project_name="px")]
    ran, got = tmp_path / "ran", []
    log = lambda: (run / "checks.log").read_text() if (run / "checks.log").exists() else ""   # noqa: E731
    t = threading.Thread(target=lambda: cli.main(["checks", "--", "sh", "-c", f"touch {ran}"]))
    t.start()
    rival = None
    try:
        assert _wait_for(lambda: "waiting for a host-wide" in log() and checkcap.state()["running"]), log()
        rival = _rival_checks_slot(got)
        assert _wait_for(lambda: checkcap.state()["queued"])
        since = lambda: [json.loads(f.read_text() or "{}").get("since") for f in checkcap.all_paths(1)   # noqa: E731
                         if f.exists()]
        before = since()
        new.append("99.0.0")
        assert _wait_for(lambda: "could not re-execute" in log()), log()
        assert _wait_for(lambda: since() != before), "let go, then taken anew"
        assert log().count("re-executing with it") == 1 and not os.environ.get(checkcap.KEPT_ENV)
    finally:
        for g in held:
            g.close()
        rival and rival.join(30)
        t.join(30)
    assert ran.exists(), "it waited on and ran"
    # (2) A process holding a kept slot dies (here after a real exec that kept it): the slot is free again.
    slot = tmp_path / "slot"
    code = ("import json, os, sys\n"
            "from ttp import checkcap, locks\n"
            "f = locks.try_take([__import__('pathlib').Path(sys.argv[1])], 'holder')\n"
            "os.environ[checkcap.KEPT_ENV] = checkcap.keep_slot(checkcap.Slot(f))\n"
            "os.execv(sys.executable, [sys.executable, '-c', 'import os, sys, time\\n"
            "from ttp import checkcap\\n"
            "s = checkcap.adopt_slot(os.environ[checkcap.KEPT_ENV])\\n"
            "print(\"adopted\" if s else \"lost\", flush=True)\\n"
            "time.sleep(60)'])\n")
    proc = subprocess.Popen([sys.executable, "-c", code, str(slot)], cwd=str(RUNTIME), stdout=subprocess.PIPE,
                            text=True, env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    try:
        assert proc.stdout.readline().strip() == "adopted"
        assert _slot_try(slot) is None, "held by the re-executed process"
    finally:
        proc.kill()
        proc.wait(30)
    f = _slot_try(slot)
    assert f is not None, "its death freed it"
    f.close()
    assert checkcap.adopt_slot("") is None and checkcap.adopt_slot("not json") is None
    assert checkcap.adopt_slot(json.dumps([999, str(slot)])) is None, "an fd that is not open"


def _slot_try(path):
    from ttp import locks
    return locks.try_take([path], "probe")


def test_test_slots_waiter_queues_at_its_arrival_and_a_stop_ends_its_wait_leaving_no_ticket(env, monkeypatch):
    import threading
    from ttp import checkcap, locks, testslots
    monkeypatch.setenv("TTP_TEST_POOL", "2")
    held = [testslots.acquire("h1", project_name="px"), testslots.acquire("h2", project_name="px")]
    stopped, got = [], {}

    def late():
        try:
            got["late"] = testslots.acquire("late", project_name="pb",
                                            stop=lambda: "the task ended" if stopped else "")
        except checkcap.Cancelled as e:
            got["late stopped"] = str(e)
    lt = threading.Thread(target=late)
    lt.start()
    assert _wait_for(lambda: len(locks.queued(checkcap.slot_dir(), testslots.TESTS.resource)) == 1)

    def early():
        got["early"] = testslots.acquire("early", project_name="pc", since=time.time() - 600)
    e = threading.Thread(target=early)
    e.start()
    try:
        assert _wait_for(lambda: len(locks.queued(checkcap.slot_dir(), testslots.TESTS.resource)) == 2)
        held.pop().close()
        e.join(10)
        assert got.get("early") and got["early"].k == 1, "its earlier arrival goes first"
        assert "late" not in got
        stopped.append(1)
        lt.join(10)
        assert not lt.is_alive() and got.get("late stopped") == "the task ended"
        assert not locks.queued(checkcap.slot_dir(), testslots.TESTS.resource)
    finally:
        for g in held + [got.get("early")]:
            if g:
                g.close()
        stopped.append(1)
        for th in (lt, e):
            th.join(10)


def test_a_real_detached_checks_child_reexecutes_in_place_after_an_install_and_still_writes_its_rc(
        env, tmp_path, monkeypatch, capsys):
    # The whole path: a detached child started from an installed runtime waits for the checks slot; a new
    # version is installed there; it re-executes as the same process (checks.pid still names it, so it
    # runs without a new `go`), waits on with the new code and writes checks.rc once its commands ran.
    import signal
    from ttp import checkcap, cli, locks, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    rt = tmp_path / "installed"
    shutil.copytree(pathlib.Path(cli.__file__).parents[1], rt, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    cc = rt / "ttp" / "checkcap.py"
    cc.write_text(cc.read_text().replace("UPGRADE_SETTLE_S = 10.0", "UPGRADE_SETTLE_S = 0.2", 1))
    monkeypatch.setattr(cli, "__file__", str(rt / "ttp" / "cli.py"))   # the child imports the installed copy
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    monkeypatch.delenv(checkcap.REEXEC_ENV, raising=False)
    held = checkcap.acquire("other")
    ran = tmp_path / "ran"
    pid = None
    try:
        fds = tmp_path / "fds"
        pid = _detached_checks(run, capsys, "sh", "-c", f"echo x >> {ran}; ls -l /proc/$$/fd > {fds} 2>&1; true")
        assert _wait_for(lambda: (run / cli.CHECKS_SLOT_WAIT).exists())
        init = rt / "ttp" / "__init__.py"
        init.write_text(re.sub(r'__version__ = "[^"]+"', '__version__ = "99.0.0"', init.read_text()))
        assert _wait_for(lambda: "re-executing with it" in (run / "checks.log").read_text())
        assert _wait_for(lambda: (run / cli.CHECKS_SLOT_WAIT).exists()), "it waits again, with the new code"
        assert json.loads((run / "checks.pid").read_text())["pid"] == pid and not (run / cli.CHECKS_RC).exists()
        assert "keeping its checks lane" in (run / "checks.log").read_text()
        lanes = locks.holder_records(testslots.lane_paths(p.name, None, None))
        assert [r.get("pid") for r in lanes] == [pid], "its lane stayed held by the same process"
        held.close()
        assert _wait_for(lambda: (run / cli.CHECKS_RC).exists())
        assert (run / cli.CHECKS_RC).read_text().strip() == "0" and ran.read_text() == "x\n", {x.name: x.read_text(errors="replace")[-1500:] for x in run.iterdir() if x.is_file() and x.name.startswith("checks")}
        assert (run / "checks.log").read_text().count("re-executing with it") == 1
        assert testslots.SUITE not in fds.read_text(), "the commands do not inherit the lane"
    finally:
        held.close()
        if pid and not _gone_pid(pid):
            os.killpg(pid, signal.SIGKILL)


def _received_rows(run):
    from ttp import cli, detach
    f = detach.signals_file(run / cli.CHECKS_RC)
    return [r for r in map(json.loads, f.read_text().splitlines()) if r.get("received")] if f.exists() else []


def test_a_parked_detached_checks_run_survives_its_probe_and_sweep_and_names_who_stopped_it(
        env, tmp_path, monkeypatch, capsys):
    # Parked for the host-wide checks slot: neither its --result probe nor a sweep of earlier, ended check
    # runs (one recorded its group) ends it. A SIGTERM then stops it with exit 143, and the run names its
    # sender: a job-signals row with the sender's pid, ancestors and the run's folder, and a checks.log line.
    import signal
    from ttp import checkcap, checkreap, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    held = checkcap.acquire("other")
    pid = None
    try:
        pid = _detached_checks(run, capsys, "true")
        assert _wait_for(lambda: (run / cli.CHECKS_SLOT_WAIT).exists())
        with pytest.raises(SystemExit) as probe:
            cli.main(["checks", "--result", str(run)])
        assert probe.value.code == 1, "still running"
        capsys.readouterr()
        for tok, pgids in ((checkreap.token(), [os.getpgid(pid)]), (checkreap.token(), [])):
            d = p.state / checkreap.FOLDER / tok
            d.mkdir(parents=True)
            (d / checkreap.RECORD).write_text(json.dumps({"token": tok, "pid": 1, "started": "1", "boot": "", "pidns": "",
                                                          "tree": "", "top": str(repo), "pgids": pgids, "ts": 0}))
            (d / checkreap.EXIT).write_text(json.dumps({"rc": 0, "ts": 0}))
        checkreap.sweep(p.state, grace_s=0, term_s=0.5)
        time.sleep(0.3)
        assert not _gone_pid(pid) and not (run / cli.CHECKS_RC).exists(), "the probe and the sweep leave it waiting"
        assert not _received_rows(run)
        os.kill(pid, signal.SIGTERM)
        assert _wait_for(lambda: (run / cli.CHECKS_RC).exists())
        assert (run / cli.CHECKS_RC).read_text().strip() == "143"
        rows = _received_rows(run)
        assert len(rows) == 1 and rows[0]["signal"] == "SIGTERM" and rows[0]["pid"] == pid, rows
        assert rows[0]["from_pid"] == os.getpid() and rows[0]["from_uid"] == os.getuid(), rows
        assert rows[0]["from_parents"] and rows[0]["from_parents"][0].startswith(f"{os.getppid()} "), rows
        assert rows[0]["run_dir"] == str(run) and rows[0]["job"] == "checks", rows
        log = (run / "checks.log").read_text()
        assert f"SIGTERM received by pid {pid} (ttp checks --detach) from pid {os.getpid()}" in log, log[-800:]
        assert "its parents: " in log and "stopped by signal 15" in log, log[-800:]
    finally:
        held.close()
        if pid and not _gone_pid(pid):
            os.killpg(pid, signal.SIGKILL)


def test_a_running_check_command_gets_the_stop_signals_unblocked_and_ends_with_its_named_stop(
        env, tmp_path, monkeypatch, capsys):
    # The checks take their stop signals in a thread that names the sender: their check command must still
    # start with SIGTERM, SIGINT and SIGHUP unblocked, so a stop of the checks ends it.
    import signal
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    status, cpid = tmp_path / "status", tmp_path / "cmd.pid"
    pid = None
    try:
        # Not sh: dash clears its blocked signals itself, most commands keep the mask they start with.
        code = ("import os, pathlib, time; pathlib.Path(%r).write_text(str(os.getpid())); "
                "pathlib.Path(%r).write_text(open('/proc/self/status').read().split('SigBlk:')[1].split()[0]); "
                "time.sleep(60)") % (str(cpid), str(status))
        pid = _detached_checks(run, capsys, sys.executable, "-c", code)
        assert _wait_for(lambda: status.exists() and status.read_text().strip() and cpid.exists())
        blocked = int(status.read_text(), 16)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            assert not blocked & (1 << (sig - 1)), (sig, hex(blocked))
        # Where the checks sandbox works (this suite not already in one), its os.getpid() is a namespace pid.
        cmd = _host_pid([sys.executable, "-c", code])
        os.kill(pid, signal.SIGTERM)
        assert _wait_for(lambda: (run / cli.CHECKS_RC).exists())
        assert (run / cli.CHECKS_RC).read_text().strip() == "143"
        assert _wait_for(lambda: not os.path.exists(f"/proc/{cmd}") or
                         pathlib.Path(f"/proc/{cmd}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z")
        assert [r["from_pid"] for r in _received_rows(run)] == [os.getpid()]
    finally:
        if pid and not _gone_pid(pid):
            os.killpg(pid, signal.SIGKILL)


def test_checks_child_in_process_leaves_the_stop_signals_unblocked(env, tmp_path, monkeypatch):
    # Called in this process (as tests do), the sender capture ends with the child: its mask is restored.
    import signal
    from ttp import cli
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"")))
    before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    assert cli._checks_child(types.SimpleNamespace(rc=str(tmp_path / cli.CHECKS_RC))) == 1
    assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == before and not cli._CHECK_NAMER["on"]

def test_the_stop_signal_thread_drops_a_siginfo_naming_no_stop_signal(env, tmp_path, monkeypatch):
    # Python 3.10's sigtimedwait returns an unfilled siginfo when its timeout passes during an EINTR
    # retry (seen on a loaded host: signal -15989136): queued, it crashed the checks' main-thread handler.
    import signal
    import threading
    from ttp import cli
    rc = tmp_path / cli.CHECKS_RC
    bad = [-15989136, 0, int(signal.SIGUSR1), 1 << 30]
    fed, done = list(bad), threading.Event()

    def fake(sigset, timeout):
        if fed:
            return types.SimpleNamespace(si_signo=fed.pop(0), si_pid=1434177248, si_uid=30154)
        done.set()
        return None
    monkeypatch.setattr(signal, "sigtimedwait", fake)
    before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    assert cli._checks_name_senders(rc)
    try:
        assert done.wait(_load_scaled(30)), "the thread never took the fed results"
        assert cli._CHECK_NAMER["queue"] == [], "a non-stop signal went on to the main thread"
    finally:
        cli._checks_names_off()
    assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == before
    assert not (tmp_path / "checks.log").exists(), "a non-stop signal was logged as received"


def _queue_events(n, waited_s=1000.0, holders=None, start=13):
    now = time.time()
    holders = [{"task": "12", "pid": 4242, "run_since": now - 600, "command": "pytest -q"}] if holders is None \
        else holders
    return [{"fingerprint": f"checks_slot_wait:{start + i}:{now - waited_s - i:.0f}", "task": start + i,
             "waited_s": waited_s + (n - i), "holders": holders,
             "text": f"Checks slot wait: #{start + i} has waited a while (position {i + 1} of {n})."}
            for i in range(n)]


def _queue_wakes(p, status="queued"):
    return p.db.q("SELECT task, text FROM events WHERE kind='checks_slot_wait' AND status=? ORDER BY id", (status,))


def test_a_checks_queue_of_21_waiters_wakes_the_coordinator_once_and_records_every_waiter(env, monkeypatch):
    from ttp import checklane, coordinator
    from ttp.daemon import Daemon
    p = make(env)
    evs = _queue_events(21)
    monkeypatch.setattr(checklane, "wait_events", lambda *a, **k: evs)
    d = Daemon(p.base)
    for _ in range(3):
        d.check_checks_wait(every_s=0)
    wakes = _queue_wakes(p)
    assert len(wakes) == 1, wakes
    assert wakes[0]["task"] == 13 and "incident started (21 waiting" in wakes[0]["text"], wakes
    assert len(_queue_wakes(p, "coalesced")) == 21, "every waiter stays recorded, without a wake"
    evs.extend(_queue_events(3, start=40))
    d.check_checks_wait(every_s=0)
    assert len(_queue_wakes(p)) == 1 and len(_queue_wakes(p, "coalesced")) == 24, "a later waiter: no new wake"
    assert p.db.kv(checklane.INCIDENT_KEY)["waiters"] == 24


def test_a_checks_queue_incident_rewakes_once_per_escalation_interval_of_its_longest_wait(env, monkeypatch):
    from ttp import checklane, testslots
    from ttp.daemon import Daemon
    p = make(env)
    evs = _queue_events(5)
    monkeypatch.setattr(checklane, "wait_events", lambda *a, **k: evs)
    d = Daemon(p.base)
    d.check_checks_wait(every_s=0)
    assert len(_queue_wakes(p)) == 1
    evs[:] = _queue_events(5, waited_s=checklane.WAIT_ESCALATE_S + 60)
    for _ in range(2):
        d.check_checks_wait(every_s=0)
    wakes = _queue_wakes(p)
    assert len(wakes) == 2 and "longest wait passed 2 h" in wakes[1]["text"], wakes
    inc = p.db.kv(checklane.INCIDENT_KEY)
    inc["escalated_at"] -= checklane.WAIT_ESCALATE_S   # one escalation interval later
    p.db.set_kv(checklane.INCIDENT_KEY, inc)
    d.check_checks_wait(every_s=0)
    assert len(_queue_wakes(p)) == 3, "once more per escalation interval"
    d.check_checks_wait(every_s=0)
    assert len(_queue_wakes(p)) == 3
    # checks.wait_escalate_s 0: never escalates
    why, _ = checklane.wait_wake(evs, {"start": 1.0}, {"checks": {"wait_escalate_s": 0}}, time.time())
    assert why == []


def test_a_drained_checks_queue_ends_its_incident_and_a_new_queue_wakes_afresh(env, monkeypatch):
    from ttp import checklane, testslots
    from ttp.daemon import Daemon
    p = make(env)
    evs = _queue_events(4)
    monkeypatch.setattr(checklane, "wait_events", lambda *a, **k: evs)
    d = Daemon(p.base)
    d.check_checks_wait(every_s=0)
    evs[:] = []   # drained, or no waiter past checks.wait_event_s
    d.check_checks_wait(every_s=0)
    assert p.db.kv(checklane.INCIDENT_KEY) is None
    assert len(_queue_wakes(p)) == 1
    evs[:] = _queue_events(2, start=60)
    d.check_checks_wait(every_s=0)
    wakes = _queue_wakes(p)
    assert len(wakes) == 2 and wakes[1]["task"] == 60 and "incident started (2 waiting" in wakes[1]["text"], wakes


_RUN_HOLDER = """
import json, os, sys, time
from pathlib import Path
from ttp import checklane, cli
from ttp.project import Project
checklane.HEARTBEAT_S = 3600.0
slot = cli._checks_slot(Project(sys.argv[1]), Path(os.environ["TTP_RUN_DIR"]), sys.stdout, sys.argv[3], True,
                        cmds=["true"], top=sys.argv[1])
Path(sys.argv[2]).write_text("held" if slot else "none")
while True:
    time.sleep(1)
"""


def _run_holder(p, tmp_path, run_env: dict):
    """A `ttp checks` run holding one of the project's checks lanes through cli._checks_slot under `run_env`
    (the variables a worker's run or Daemon._start_checks sets): (its process, its lane's holder record)."""
    from ttp import locks, testslots, worktree
    got = tmp_path / f"held-{time.time_ns()}"
    proc = subprocess.Popen([sys.executable, "-c", _RUN_HOLDER, str(p.base), str(got),
                             worktree._git(p.root, "rev-parse", "HEAD")],
                            env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_PROJECT": str(p.base), **run_env},
                            cwd=p.root, start_new_session=True, stdout=subprocess.DEVNULL)
    assert _wait_for(got.exists, 60) and got.read_text() == "held", "the run never took its lane"
    recs = [r for b in testslots.suite_bases(p.name) for x in testslots._lanes_of(b)
            for r in locks.holder_records([x]) if r.get("pid") == proc.pid]
    assert len(recs) == 1 and recs[0].get("task") == run_env["TTP_TASK"], recs
    return proc, recs[0]


def test_a_checks_queue_incident_rewakes_for_a_likely_hung_holder_and_for_an_ownerless_one(
        env, tmp_path, monkeypatch):
    from ttp import checklane, checkwall, daemon, worktree
    from ttp.daemon import Daemon
    from ttp import cli
    assert checklane.PRECHECK_LABEL == daemon.PRECHECK_LABEL
    p = make(env)
    _lane_env(monkeypatch)
    p.set_config("checks.slots", 3)
    for _ in range(checkwall.MIN_RUNS):   # runs of the holder's own command list (_RUN_HOLDER's): its baseline
        checkwall.record_run(p.state, 600.0, True, key=cli._commands_hash(["true"]))
    work = p.db.add_task("w", "s", kind="code")
    p.db.x("UPDATE tasks SET status='running' WHERE id=?", (work,))
    run = p.state / "runs" / "77"
    run.mkdir(parents=True)
    procs = []
    try:
        a, rec = _run_holder(p, tmp_path, {"TTP_RUN_DIR": str(run), "TTP_RUN_ID": "77", **daemon.task_env(work)})
        procs.append(a)
        log = run / "checks.log"   # its commands' log (testslots.suite_running), last grown 2 min ago
        log.write_text("collected 10 items\n")
        os.utime(log, (time.time() - 120,) * 2)
        rec.update(log=str(log), run_since=time.time() - 120)
        evs = _queue_events(3, holders=[rec])
        monkeypatch.setattr(checklane, "wait_events", lambda *a, **k: evs)
        d = Daemon(p.base)
        d.check_checks_wait(every_s=0)
        assert len(_queue_wakes(p)) == 1, "a holder at the median: no hang"
        d.cfg.setdefault("checks", {})["wait_hang_factor"] = 1e-6   # its run is now far over the median
        d.cfg["checks"]["stall_s"] = 60   # and its log stalled (checkstall.view)
        for _ in range(2):
            d.check_checks_wait(every_s=0)
        wakes = _queue_wakes(p)
        assert len(wakes) == 2 and "likely hang" in wakes[1]["text"], wakes
        now = time.time()
        assert checklane.wait_wake(evs, {"start": 1.0}, {"checks": {"wait_hang_factor": 0}}, now, 600.0)[0] == [], \
            "checks.wait_hang_factor 0: never"
        kept = {"start": 1.0, "hang": [], "ownerless": []}
        assert checklane.wait_wake(evs, kept, {"checks": {"wait_hang_factor": 1e-6, "stall_s": 60}}, now,
                                   600.0)[0] == ["hang"]
        assert kept == {"start": 1.0, "hang": [], "ownerless": []}, "the stored incident is never changed in place"
        d.cfg["checks"]["wait_hang_factor"] = 0
        # the daemon's review precheck of a just-finished code task joins the holders: its review reads it
        code = p.db.add_task("c", "s", kind="code")
        head = worktree._git(p.root, "rev-parse", "HEAD")
        out = p.state / daemon.PRECHECKS_DIR / f"t{code}-{head[:12]}"   # Daemon._precheck's run folder
        out.mkdir(parents=True)
        rev = p.db.add_task("review", "r", kind="review", labels=[daemon.PRECHECK_LABEL + out.name])
        p.db.x("UPDATE tasks SET status='done' WHERE id=?", (code,))
        b, pre = _run_holder(p, tmp_path, {"TTP_RUN_DIR": str(out), "TTP_RUN_ID": daemon.precheck_run_id(out),
                                           **daemon.task_env(code)})   # as Daemon._precheck starts it
        procs.append(b)
        evs[:] = _queue_events(3, holders=[rec, pre])
        for status in ("queued", "running"):
            p.db.x("UPDATE tasks SET status=? WHERE id=?", (status, rev))
            d.check_checks_wait(every_s=0)
        assert len(_queue_wakes(p)) == 2, "a precheck its open review reads is not a finished task's holder"
        # bypasses: a review of another head, or of another task's precheck, keeps no holder owned
        assert not checklane.open_precheck(p.db, str(code), "run t%d-ba9876543210" % code)
        assert not checklane.open_precheck(p.db, str(work), pre["holder"])
        assert not checklane.open_precheck(p.db, str(code), rec["holder"])
        # its review closed: nobody owns its fix, one wake
        p.db.x("UPDATE tasks SET status='done' WHERE id=?", (rev,))
        for _ in range(2):
            d.check_checks_wait(every_s=0)
        wakes = _queue_wakes(p)
        assert len(wakes) == 3 and "nobody owns its fix" in wakes[2]["text"], wakes
        # a worker run's own holder whose task ended: ownerless at once
        p.db.x("UPDATE tasks SET status='failed' WHERE id=?", (work,))
        d.check_checks_wait(every_s=0)
        assert len(_queue_wakes(p)) == 4
    finally:
        for x in procs:
            os.killpg(x.pid, 9)
            x.wait(10)


def test_checks_slot_wait_events_count_against_an_observation_mute_on_daemon_checks_slot_wait(env, monkeypatch):
    from ttp import checklane, screen
    from ttp.daemon import Daemon
    p = make(env)
    screen.mute(p.db, "daemon:checks_slot_wait", "Checks", 2, why="a known long queue")
    monkeypatch.setattr(checklane, "wait_events", lambda *a, **k: _queue_events(3))
    Daemon(p.base).check_checks_wait(every_s=0)
    assert _queue_wakes(p) == [] and len(_queue_wakes(p, "muted")) == 4, "the wake and the waiters: muted"
    assert screen.mutes(p.db)[0]["count"] == 4


@pytest.mark.parametrize("args", [["bogus"], ["status", "--bogus"], ["status", "extra"], ["--bogus"],
                                  ["--slot-status", "--", "true"]])
def test_ttp_checks_refuses_an_unknown_subcommand_or_flag_and_queues_nothing(env, tmp_path, monkeypatch, capsys, args):
    from ttp import checkcap
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    assert _ttp("checks", *args) != 0
    err = capsys.readouterr().err
    assert "ttp checks:" in err or "unrecognized arguments" in err, err
    assert not (run / "checks.log").exists(), "an unknown word ran as a check"
    assert not [x for x in checkcap.slot_dir().rglob("*") if ".queue" in str(x) and x.is_file()], "it queued"


@pytest.mark.parametrize("args", [["status"], ["status", "--json"], ["--slot-status"], ["--slot-status", "--json"],
                                  ["status", "--slot-status"]])
def test_ttp_checks_status_spellings_read_the_lanes(env, tmp_path, monkeypatch, capsys, args):
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    assert _ttp("checks", *args) == 0
    assert "checks lanes" in capsys.readouterr().out or "--json" in args
    assert not (run / "checks.log").exists()


def test_ttp_checks_still_runs_a_real_command(env, tmp_path, monkeypatch):
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    assert cli.main(["checks", "--", "true"]) in (0, None)
    assert (run / "checks.log").exists()


def test_slot_status_names_an_unkeyed_lane_group_by_the_project_folder_never_empty(env, tmp_path, monkeypatch, capsys):
    from ttp import locks, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    f = locks.try_take(testslots.lane_paths(p.name, 1, ""), f"{p.name}: full checks (run 7, pid 1)", "pytest",
                       extra={"task": "7"})
    try:
        assert _ttp("checks", "--slot-status", "--json") == 0
        got = json.loads(capsys.readouterr().out)["repos"]
        assert [(r["repo"], r["name"]) for r in got] == [("", p.root.name)], got
        assert _ttp("checks", "status") == 0
        assert f"repository {p.root.name} (not keyed" in capsys.readouterr().out
    finally:
        f.close()


_DONE_HOLDER = """
import sys, time
from pathlib import Path
from ttp import checklane, testslots
checklane.HEARTBEAT_S = 3600.0
g = testslots.suite_hold(sys.argv[1], repo=sys.argv[3], who=sys.argv[4])
Path(sys.argv[2]).write_text("held")
while True:
    time.sleep(1)
"""


def test_a_lane_held_by_a_finished_task_s_check_is_flagged_then_only_its_own_checks_are_stopped(
        env, tmp_path, monkeypatch, capsys):
    from ttp import checklane, cli
    from ttp.daemon import Daemon
    from ttp.runner import proc_start
    p = make(env)
    _lane_env(monkeypatch)
    tid = p.db.add_task("t", "s", kind="code")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "ok", str(run_dir)))
    rid = p.db.one("SELECT max(id) AS id FROM runs")["id"]
    got = tmp_path / "held"
    proc = subprocess.Popen([sys.executable, "-c", _DONE_HOLDER, p.name, str(got), _LANE_REPO, f"run {rid}"],
                            env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_TASK": str(tid)},
                            start_new_session=True)
    try:
        assert _wait_for(got.exists)
        d = Daemon(p.base)
        ev = lambda: p.db.q("SELECT text FROM events WHERE kind='checks_lane_finished'")
        d.check_checks_owner(every_s=0)
        assert not ev(), "a running task's lane is left alone"
        later = time.time() + 400   # 6 min 40 s after the lane was taken and the task ended: past the 5 min flag,
        p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (time.time(), tid))   # inside the 1 h grace
        d.check_checks_owner(every_s=0, now=later)
        d.check_checks_owner(every_s=0, now=later)
        texts = [r["text"] for r in ev()]
        assert len(texts) == 1 and f"pid {proc.pid} of task #{tid}" in texts[0] and f"run {rid}" in texts[0], texts
        assert proc.poll() is None, "flagged inside the grace, not stopped"
        monkeypatch.setenv("TTP_PROJECT", str(p.base))
        monkeypatch.chdir(p.root)
        assert _ttp("checks", "status") == 0
        out = capsys.readouterr().out
        assert f"#{tid} run {rid} pid {proc.pid} for full checks" in out and "its task is DONE" in out, out
        # past the grace: stopped only when the run's checks.pid is that holder
        h = checklane.finished_holders(p, {"checks": {"done_holder_grace_s": 0}}, later)
        assert h and h[0]["stop"]
        (run_dir / cli.CHECKS_PID).write_text(json.dumps({"pid": os.getpid(), "started": proc_start(os.getpid())}))
        assert checklane.stop_finished(p, h[0]) is None, "another pid is never stopped"
        (run_dir / cli.CHECKS_PID).write_text(json.dumps({"pid": proc.pid, "started": proc_start(proc.pid)}))
        r = checklane.stop_finished(p, {**h[0], "run": str(rid + 1000)})
        assert r is None, "a run that is not the holder's is not used"
        r = checklane.stop_finished(p, h[0])
        assert r and r["pgid"] == proc.pid and proc.wait(30) is not None
    finally:
        _stop(proc)


# A review precheck as `ttp checks --detach` holds its lane: cli._checks_slot under Daemon._start_checks' env.
_PRECHECK_HOLDER = """
import json, os, sys, time
from pathlib import Path
from ttp import checklane, cli
from ttp.project import Project
checklane.HEARTBEAT_S = 3600.0
slot = cli._checks_slot(Project(sys.argv[1]), Path(os.environ["TTP_RUN_DIR"]), sys.stdout, sys.argv[3], True,
                        cmds=["true"], top=sys.argv[1])
Path(sys.argv[2]).write_text("held" if slot else "none")
while True:
    time.sleep(1)
"""


def test_a_daemon_review_precheck_of_a_just_finished_task_is_not_a_finished_task_s_lane_while_its_review_is_open(
        env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, checklane, cli, daemon, locks, testslots, worktree
    from ttp.daemon import Daemon
    from ttp.runner import proc_start
    assert checklane.PRECHECK_LABEL == daemon.PRECHECK_LABEL
    p = make(env)
    _lane_env(monkeypatch)
    tid = p.db.add_task("t", "s", kind="code")
    head = worktree._git(p.root, "rev-parse", "HEAD")
    out = p.state / daemon.PRECHECKS_DIR / f"t{tid}-{head[:12]}"   # Daemon._precheck's run folder
    out.mkdir(parents=True)
    token = out.name
    rev = p.db.add_task("review", "r", kind="review", labels=[checklane.PRECHECK_LABEL + token])
    p.db.add_task("review", "r", kind="review", labels=[f"{checklane.PRECHECK_LABEL}t{tid}-ba9876543210"])
    run_env = {"TTP_RUN_DIR": str(out), "TTP_PROJECT": str(p.base), "TTP_RUN_ID": daemon.precheck_run_id(out),
               **daemon.task_env(tid)}   # as Daemon._start_checks sets them
    got = tmp_path / "held"
    proc = subprocess.Popen([sys.executable, "-c", _PRECHECK_HOLDER, str(p.base), str(got), head],
                            env={**os.environ, "PYTHONPATH": str(RUNTIME), **run_env}, cwd=p.root,
                            start_new_session=True, stdout=subprocess.DEVNULL)
    try:
        assert _wait_for(got.exists, 60) and got.read_text() == "held"
        recs = [r for b in testslots.suite_bases(p.name) for x in testslots._lanes_of(b)
                for r in locks.holder_records([x])]
        assert len(recs) == 1 and str(recs[0].get("task")) == str(tid) and recs[0].get("pid") == proc.pid, recs
        lane = [recs[0]["holder"]]
        for k, v in run_env.items():
            monkeypatch.setenv(k, v)
        slot_label = checkcap.label(p.name)   # its checks and test slots' label
        for k in run_env:
            monkeypatch.delenv(k)
        assert checklane.open_precheck(p.db, str(tid), lane[0]), lane
        assert checklane.open_precheck(p.db, str(tid), slot_label), slot_label
        d = Daemon(p.base)
        ev = lambda: p.db.q("SELECT text FROM events WHERE kind='checks_lane_finished'")
        later = time.time() + 400   # past the 5 min flag: a full suite runs about 9 min
        p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (time.time(), tid))
        d.check_checks_owner(every_s=0, now=later)
        assert not ev(), "the precheck its open review reads is not a finished task's lane"
        monkeypatch.setenv("TTP_PROJECT", str(p.base))
        monkeypatch.chdir(p.root)
        assert _ttp("checks", "status") == 0
        out_text = capsys.readouterr().out
        assert f"#{tid}" in out_text and "its task is DONE" not in out_text, out_text
        # bypasses: another task's precheck token, or a review of another head only, is not exempt
        assert not checklane.open_precheck(p.db, str(tid + 1), lane[0])
        assert not checklane.open_precheck(p.db, str(tid + 1), slot_label)
        assert not checklane.open_precheck(p.db, str(tid), "run 7")
        p.db.x("UPDATE tasks SET status='done' WHERE id=?", (rev,))
        assert not checklane.open_precheck(p.db, str(tid), lane[0]), "the open review is of another head"
        # its review ended: the lane is reported as a finished task's again (never stopped: no worker run)
        d.check_checks_owner(every_s=0, now=later + 60)
        texts = [r["text"] for r in ev()]
        assert len(texts) == 1 and f"pid {proc.pid} of task #{tid}" in texts[0], texts
        h = checklane.finished_holders(p, {"checks": {"done_holder_grace_s": 0}}, later + 60)
        assert h and h[0]["stop"] and h[0]["precheck"] == token
        assert checklane.stop_finished(p, h[0]) is None and proc.poll() is None, "no checks.pid in its folder"
        (out / cli.CHECKS_PID).write_text(json.dumps({"pid": os.getpid(), "started": proc_start(os.getpid())}))
        assert checklane.stop_finished(p, h[0]) is None, "another pid is never stopped"
        (out / cli.CHECKS_PID).write_text(json.dumps({"pid": proc.pid, "started": proc_start(proc.pid)}))
        assert checklane.stop_finished(p, {**h[0], "precheck": f"t{tid + 1}-{head[:12]}"}) is None
        r = checklane.stop_finished(p, h[0])
        assert r and r["pgid"] == proc.pid and proc.wait(30) is not None, "past grace its precheck is stopped"
        assert not [x for b in testslots.suite_bases(p.name) for l in testslots._lanes_of(b)
                    for x in locks.holder_records([l]) if x.get("alive") is not False], "lane freed"
    finally:
        _stop(proc)


def _wrap_lane_step_holding(p, run_dir: Path, tid: int, got: Path, parent: bool = False):
    """The checks.wrap_lane "after" step as `ttp checks` in run folder `run_dir` builds it
    (cli._wrap_lane_step), run for task `tid` holding the wrapper lock's lane until stopped; `parent`: under
    a stand-in for the run's `ttp checks` process, as the wrapper runs it."""
    from ttp import cli
    step = cli._wrap_lane_step(p, run_dir, ("flock", str(run_dir / "dev.lock")))
    argv = [*step, "sh", "-c", f"touch {got}; sleep 600"]
    if parent:
        argv = [sys.executable, "-c", "import subprocess, sys; subprocess.run(sys.argv[1:])", *argv]
    proc = subprocess.Popen(argv, env={**os.environ, "TTP_TASK": str(tid)}, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert _wait_for(got.exists, 60), "the step took the lane and ran its command"
    return proc


def _kill_group(proc):
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait(10)


def test_a_wrap_lane_after_review_precheck_is_not_a_finished_task_s_lane_while_its_review_is_open(
        env, tmp_path, monkeypatch):
    from ttp import checklane, daemon, worktree
    p = make(env)
    _lane_env(monkeypatch)
    tid = p.db.add_task("t", "s", kind="code")
    head = worktree._git(p.root, "rev-parse", "HEAD")
    out = p.state / daemon.PRECHECKS_DIR / f"t{tid}-{head[:12]}"   # Daemon._precheck's run folder
    out.mkdir(parents=True)
    rev = p.db.add_task("review", "r", kind="review", labels=[checklane.PRECHECK_LABEL + out.name])
    proc = _wrap_lane_step_holding(p, out, tid, tmp_path / "got")
    try:
        later = time.time() + 400   # past the 5 min flag
        p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (time.time(), tid))
        assert checklane.finished_holders(p, {}, later) == [], "the precheck its open review reads is exempt"
        p.db.x("UPDATE tasks SET status='done' WHERE id=?", (rev,))
        h = checklane.finished_holders(p, {}, later)
        assert len(h) == 1 and h[0]["task"] == str(tid) and h[0]["pid"] == proc.pid, h   # the step holds it
        assert checklane.stop_finished(p, {**h[0], "stop": True}) is None and proc.poll() is None, "no worker run"
    finally:
        _kill_group(proc)


def test_a_finished_task_s_wrap_lane_after_check_is_found_by_its_run_and_only_it_is_stopped(env, tmp_path, monkeypatch):
    from ttp import checklane, cli
    from ttp.runner import proc_start
    p = make(env)
    _lane_env(monkeypatch)
    tid = p.db.add_task("t", "s", kind="code")
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "ok", ""))
    rid = p.db.one("SELECT max(id) AS id FROM runs")["id"]
    run_dir = tmp_path / "runs" / str(rid)   # a worker run's folder is named by its run id
    run_dir.mkdir(parents=True)
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
    proc = _wrap_lane_step_holding(p, run_dir, tid, tmp_path / "got", parent=True)
    other = subprocess.Popen(["sleep", "600"], start_new_session=True)
    try:
        later = time.time() + 400
        p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (time.time(), tid))
        h = checklane.finished_holders(p, {"checks": {"done_holder_grace_s": 0}}, later)
        assert len(h) == 1 and h[0]["run"] == str(rid) and h[0]["stop"] and h[0]["pid"] != proc.pid, h
        (run_dir / cli.CHECKS_PID).write_text(json.dumps({"pid": other.pid, "started": proc_start(other.pid)}))
        assert checklane.stop_finished(p, h[0]) is None, "a holder not under the run's checks is never stopped"
        assert other.poll() is None and proc.poll() is None
        (run_dir / cli.CHECKS_PID).write_text(json.dumps({"pid": proc.pid, "started": proc_start(proc.pid)}))
        from ttp import jobuse
        under = [x for x in jobuse._tree(proc.pid) if x not in (proc.pid, h[0]["pid"])]
        assert under and checklane.stop_finished(p, {**h[0], "pid": under[0]}) is None, "only the step under it"
        r = checklane.stop_finished(p, h[0])
        assert r and r["pgid"] == proc.pid and proc.wait(30) is not None
        assert other.poll() is None, "only the run's own checks"
    finally:
        _kill_group(proc)
        _kill_group(other)


def test_a_duplicate_and_a_dead_waiter_ahead_of_a_live_one_are_dropped_and_the_live_one_takes_the_lane(
        env, tmp_path, monkeypatch):
    from ttp import checkcap, checklane, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setenv("TTP_TASK", "21")
    monkeypatch.setattr(locks, "_pid_ns", lambda: "pid:[1]")   # a sandbox's /proc may not tell its namespace
    tag = " [pidns pid:[1]]"
    res = testslots._suite_resource("pa", _LANE_REPO)
    gone = subprocess.Popen(["true"])
    gone.wait()
    hold = testslots.suite_hold("pa", repo=_LANE_REPO)
    t0 = time.time() - 600
    # a dead waiter (its pid is gone) and an older run's wait for the same check of task 21, both first in line
    dead = locks.enqueue(checkcap.slot_dir(), res, f"task #30 pa: full checks (run 3, pid {gone.pid}){tag}", t0)
    dead.rename(dead.with_name(f"{t0:.6f}-{gone.pid}"))
    old_text = f"task #21 pa: full checks (run 4, pid 1) [check abc]{tag}"
    told, got = [], []
    w = threading.Thread(target=lambda: got.append(testslots.suite_hold(
        "pa", repo=_LANE_REPO, check="abc", who="run 5", tell=told.append, wait_s=20)))
    old = locks.enqueue(checkcap.slot_dir(), res, old_text, t0 + 1)
    try:
        w.start()
        assert _wait_for(lambda: any("dropped a dead waiter" in x for x in told)), told
        assert not dead.with_name(f"{t0:.6f}-{gone.pid}").exists()
        assert [t.read_text().split(" [")[0] for t in testslots.suite_order("pa", repo=_LANE_REPO)] == \
            ["task #21 pa: full checks (run 5, pid %d)" % os.getpid()], "one entry per task and check"
        kept, arrival, superseded, _ = testslots.waiter_sets(locks.queued(checkcap.slot_dir(), res))
        assert old in superseded and min(arrival.values()) == pytest.approx(t0 + 1, abs=1), \
            "the later run keeps the earliest place"
        hold.close()
        w.join(10)
        assert got and got[0].files, "the live waiter took the freed lane"
        got[0].close()
    finally:
        hold.close()
        locks.dequeue(old)


def test_an_older_run_waiting_for_the_same_check_of_its_task_stops_waiting(env, monkeypatch):
    from ttp import checkcap, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setenv("TTP_TASK", "21")
    hold = testslots.suite_hold("pa", repo=_LANE_REPO)
    res = testslots._suite_resource("pa", _LANE_REPO)
    newer = locks.enqueue(checkcap.slot_dir(), res, "task #21 pa: full checks (run 9, pid 1) [check abc]")
    try:
        with pytest.raises(checkcap.Cancelled, match="superseded"):
            testslots.suite_hold("pa", repo=_LANE_REPO, check="abc", who="run 5", wait_s=5)
    finally:
        locks.dequeue(newer)
        hold.close()


def test_a_waiter_whose_ticket_vanished_queues_again_at_its_place(env, monkeypatch):
    from ttp import checkcap, locks, testslots
    _lane_env(monkeypatch)
    hold = testslots.suite_hold("pa", repo=_LANE_REPO)
    res = testslots._suite_resource("pa", _LANE_REPO)
    got = []
    w = threading.Thread(target=lambda: got.append(testslots.suite_hold("pa", repo=_LANE_REPO, since=1000.0, wait_s=20)))
    w.start()
    try:
        assert _wait_for(lambda: locks.queued(checkcap.slot_dir(), res))
        t = locks.queued(checkcap.slot_dir(), res)[0]
        t.unlink()
        assert _wait_for(lambda: locks.queued(checkcap.slot_dir(), res)), "it queued again"
        assert locks._arrival(locks.queued(checkcap.slot_dir(), res)[0]) == pytest.approx(1000.0)
        assert not got, "a vanished ticket never takes a lane past the line"
    finally:
        hold.close()
        w.join(10)
        got and got[0] and got[0].close()


def test_lane_labels_name_the_repository_directory_and_wrapper_command_and_keep_the_id(env, tmp_path, monkeypatch):
    # A lane is labelled by what a person knows (the repository's directory name, the wrapper command),
    # with a short id only when two repositories share a name; the JSON keeps the id apart.
    from ttp import checklane, testslots
    _lane_env(monkeypatch)
    main = tmp_path / "work" / "toolkit"
    main.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                    "--allow-empty", "-m", "x"], check=True)
    wt = tmp_path / "wt" / "t1"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True)
    bare = tmp_path / "other" / "toolkit.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    assert testslots.repo_name(main) == testslots.repo_name(wt) == "toolkit", "a worktree names its repository"
    assert testslots.repo_name(bare) == "toolkit" and testslots.repo_name(tmp_path) == ""
    assert checklane.wrap_name(("/usr/local/bin/ttp", "lock", "dev-device", "--")) == "ttp lock dev-device"
    assert checklane.wrap_name(()) == "" and len(checklane.wrap_name(("x" * 99,))) == 60
    k1, k2, wk = testslots.repo_key(main), testslots.repo_key(bare), checklane.wrap_key(("ttp", "lock", "dev"))
    assert testslots.repo_key(wt) == k1 != k2
    holds = [testslots.suite_hold("pa", repo=k1, slots=1, name="toolkit"),
             testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, name="other-repo"),
             testslots.suite_hold("pa", repo=k1, slots=1, lock=wk, name="ttp lock dev")]
    try:
        st = {r["id"]: r for r in checklane.lane_status("pa")}
        assert st[k1]["label"] == "repository toolkit" and st[_LANE_REPO]["label"] == "repository other-repo", st
        assert st["w" + wk]["label"] == "wrapper lock ttp lock dev" and st["w" + wk]["kind"] == "wrapper lock", st
        assert all(r["group"] == r["label"] and r["repo"] == r["id"] for r in st.values()), "old fields kept"
        holds.append(testslots.suite_hold("pa", repo=k2, slots=1, name="toolkit"))
        labels = {r["id"]: r["label"] for r in checklane.lane_status("pa")}
        assert labels[k1] == f"repository toolkit ({k1[:6]})" and labels[k2] == f"repository toolkit ({k2[:6]})"
        # a lane no run named (an older runtime): the project's own checks.wrap_cmd names it, else its id
        (checklane._name_path("pa", "w" + wk)).unlink()
        cfg = {"checks": {"wrap_cmd": "ttp lock dev"}}
        assert checklane.lane_labels("pa", ["w" + wk], cfg)["w" + wk] == "wrapper lock ttp lock dev"
        assert checklane.lane_labels("pa", ["w" + wk], {})["w" + wk] == f"wrapper lock {wk} (checks.wrap_cmd, one at a time)"
        assert checklane.lane_labels("pa", [""], {})[""] == checklane.group_name("")
    finally:
        for g in holds:
            g and g.close()


def test_status_lines_say_which_lane_or_cap_each_held_check_waits_on(env, tmp_path, monkeypatch):
    # Two repositories' checks never share a lane: a held check of another repository than the holder
    # shows its real reason (the project cap, or the host-wide checks slot for one holding its lane).
    from ttp import checkcap, checklane, locks, testslots
    _lane_env(monkeypatch)
    cfg = {"checks": {"slots": 1, "test_project_max": 2}}
    monkeypatch.setattr(checklane, "children", lambda rec: 3)
    monkeypatch.setenv("TTP_TASK", "12")
    a = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, priority=2, name="alpha")
    testslots.suite_running(a, 3600)
    monkeypatch.setenv("TTP_TASK", "14")
    b = testslots.suite_hold("pa", repo="ba9876543210", slots=1, priority=2, name="beta")
    monkeypatch.delenv("TTP_TASK")
    now = time.time()
    host = locks.enqueue(checkcap.slot_dir(), checkcap.RESOURCE, "pa b", now)   # #14 waits for the host slot
    tickets = [locks.enqueue(checkcap.slot_dir(), testslots.suite_base("pa", _LANE_REPO), "task #31 pa: x",
                             now - 1500, priority=2),
               locks.enqueue(checkcap.slot_dir(), testslots.suite_base("pa", "cd0123456789"), "task #32 pa: x",
                             now - 1560, priority=2),
               locks.enqueue(checkcap.slot_dir(), testslots.suite_base("pa", "cd0123456789"), "task #33 pa: x",
                             now - 60, priority=2)]
    checklane.remember_name("pa", "cd0123456789", "gamma")
    try:
        lines = checklane.lane_lines("pa", cfg, now)
        by = {ln.split(" (")[0]: ln for ln in lines}
        assert set(by) == {"checks lane repository alpha", "checks lane repository beta",
                           "checks lane repository gamma"}, lines
        assert ("#12 p2 queued 0 min, running 0 min of its 1 h 0 min cap, 3 child processes alive; held: #31 p2 "
                "ranked p2 25 min, waits on repository alpha") \
            in by["checks lane repository alpha"], lines
        assert ("#14 p2 queued 0 min, has a lane 0 min, not running yet: waits on the host-wide checks slot, "
                "3 child processes alive") \
            in by["checks lane repository beta"], lines
        assert by["checks lane repository gamma"].endswith(
            "no holder; held: #32 p2 ranked p2 26 min, waits on project cap (checks.test_project_max), "
            "#33 p2 ranked p2 1 min, waits on project cap (checks.test_project_max)"), "behind a capped waiter: the cap"
        assert not any("full checks slot" in ln for ln in lines)
        long = checklane.long_wait_lines("pa", cfg, now)
        assert any("#32 has waited 2" in ln and " min on project cap (checks.test_project_max) (position 1 of 2 in "
                   "repository gamma), behind #12" in ln for ln in long), long
        ev = {e["task"]: e["text"] for e in checklane.wait_events("pa", cfg, now)}
        assert " min on repository alpha (position 1 of 1 in repository alpha)" in ev[31], ev
        assert "on project cap (checks.test_project_max)" in ev[32] and "#14" in ev[32], ev
        js = {r["label"]: r for r in checklane.lane_status("pa", now, cfg)}
        assert js["repository gamma"]["waiters"][0]["waits_on"] == checklane.CAP_LABEL
        assert js["repository beta"]["lanes"][0]["waits_on"] == "the host-wide checks slot"
        # without the cap the free lane's waiter is about to start, not held by anything
        assert "waits on nothing (about to start)" in "".join(checklane.lane_lines("pa", {"checks": {"slots": 1}}, now)) \
            or checklane.project_cap({"checks": {"slots": 1}}) > 0
    finally:
        for t in tickets + [host]:
            locks.dequeue(t)
        a.close()
        b.close()


def test_a_holder_s_queue_age_run_time_and_children_show_apart(env, tmp_path, monkeypatch):
    # A holder whose check sat queued for hours reads as queued that long and running from its pytest
    # start, never a frozen 'running 0 min'; its live children say it is not a stale holder.
    from ttp import checklane, locks, testslots
    _lane_env(monkeypatch)
    now = time.time()
    rec = {"task": "12", "command": "full checks", "queued": now - 3 * 3600, "since": now - 600,
           "run_since": now - 540, "pid": os.getpid(), "node": os.uname().nodename, "pidns": locks._pid_ns()}
    kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        t = checklane.holder_times(rec, now)
        assert (t["queued_s"], t["held_s"], t["running_s"]) == (10800.0, 600.0, 540.0), t
        assert t["children"] >= 1, t
        text = checklane.holder_text(rec, now)
        assert text.startswith("#12 'full checks' queued 3 h 0 min, running 9 min, ") and " alive" in text, text
    finally:
        _stop(kid)
    # Not running yet: the lane's age, not a run time; an older label without "queued" uses the lane's age.
    old = {"task": "12", "since": now - 120}
    assert checklane.holder_text(old, now) == "#12 queued 2 min, has the lane 2 min, not running yet, child processes unknown"
    bad = {**old, "queued": "x", "run_since": "y", "pid": "z"}
    assert checklane.holder_text(bad, now) == checklane.holder_text(old, now), "a garbled label reads as an old one"
    # Children: none for a live childless pid (said as a hint), unknown on another host or for a gone pid.
    lone = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        mine = {"pid": lone.pid, "node": os.uname().nodename, "pidns": locks._pid_ns(), "run_since": now}
        assert checklane.children(mine) == 0
        assert "no child process (stuck or between commands?)" in checklane.holder_text(mine, now)
        assert checklane.children({**mine, "node": "elsewhere"}) is None
    finally:
        _stop(lone)
    assert checklane.children(mine) is None, "a gone pid: unknown, not 'no children'"
    # The lane record carries the arrival as "queued", and the JSON shows the clocks apart.
    monkeypatch.setenv("TTP_TASK", "12")
    g = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, priority=2, name="alpha")
    try:
        lane, = checklane.lane_status("pa", time.time() + 120)[0]["lanes"]
        assert lane["queued_s"] >= lane["age_s"] >= 120 and lane["running_s"] is None, lane
        assert lane["children"] is None or lane["children"] >= 0, lane
        before = time.time()
        testslots.suite_running(g, 0)
        after = time.time()
        lane, = checklane.lane_status("pa", after + 60)[0]["lanes"]
        assert 60 <= lane["running_s"] <= round(after - before + 60, 1), lane   # run_since stamped between the two
    finally:
        g.close()


def test_a_holder_whose_threads_come_and_go_still_has_its_children_counted(tmp_path, monkeypatch):
    # A thread of the holder that ends while its children are read is skipped, not taken for the process
    # gone: the count was "child processes unknown" whenever a pytest or daemon thread ended mid-scan.
    import threading
    from ttp import jobuse
    proc = tmp_path / "proc"
    for tid, kids in (("100", "200 201 "), ("101", None), ("102", "202")):
        (proc / "100" / "task" / tid).mkdir(parents=True)
        if kids is not None:   # 101: a thread that ended between the listing and the read
            (proc / "100" / "task" / tid / "children").write_text(kids)
    (proc / "300" / "task" / "300").mkdir(parents=True)   # every thread ended: the process is gone
    real = jobuse.Path
    monkeypatch.setattr(jobuse, "Path", lambda x: real(str(x).replace("/proc/", f"{proc}/", 1)))
    assert sorted(jobuse._children(100)) == [200, 201, 202]
    assert jobuse._children(300) is None and jobuse._children(400) is None, "gone: unknown, not 0 children"
    monkeypatch.setattr(jobuse, "Path", real)
    # Live: this process's threads churn while its tree is read; it is never unknown.
    stop = []
    def churn():
        while not stop:
            ts = [threading.Thread(target=lambda: None) for _ in range(10)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
    c = threading.Thread(target=churn)
    c.start()
    try:
        assert all(jobuse._tree(os.getpid()) is not None for _ in range(500))
    finally:
        stop.append(1)
        c.join()


def test_status_and_the_web_feed_show_the_lane_lines(env, monkeypatch):
    from ttp import checklane, cli, testslots, web
    _lane_env(monkeypatch)
    p = make(env)
    g = testslots.suite_hold(p.name, repo=_LANE_REPO, slots=1, priority=3, name="alpha")
    try:
        text = cli.status_text(p, verbose=True)
        assert "checks lane repository alpha (" in text and "full checks slot" not in text, text
        assert "checks lane" not in cli.status_text(p), "a routine lane line shows with --verbose only"
        rows = [r["text"] for r in web.slots_feed(time.time(), p)]
        assert rows == checklane.lane_lines(p.name, p.config()) or any("repository alpha" in r for r in rows), rows
        assert any("checks lane repository alpha" in r for r in rows), rows
    finally:
        g.close()


_REPO_B = "ba9876543210"


def test_a_p1_check_takes_one_lane_past_a_cap_full_of_routine_runs_only_with_headroom(env, monkeypatch):
    # The project's cap of 2 is full of p2 runs (one per repository). A p1 run starts at once beside them
    # past the cap, both when its own repository's lanes are full and when they are free; a second p1 waits
    # while the first over-cap lane is held; no headroom, the same worktree or a p1 holder: no overflow.
    from ttp import locks, testslots
    _lane_env(monkeypatch)
    room = {"ok": True}
    kw = dict(slots=1, cap=2, priority=1, wait_s=0, headroom=lambda: room["ok"])
    hold = [testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, cap=2, priority=2, worktree="/w/a", wait_s=0),
            testslots.suite_hold("pa", repo=_REPO_B, slots=1, cap=2, priority=2, worktree="/w/b", wait_s=0)]
    try:
        assert all(g and g.files for g in hold) and len(testslots.project_held("pa")) == 2
        free_repo = "c0ffee000000"   # a repository whose lanes are free: the cap alone holds it
        room["ok"] = False
        assert testslots.suite_hold("pa", repo=free_repo, worktree="/w/c", **kw) is None, "no headroom"
        room["ok"] = True
        assert testslots.suite_hold("pa", repo=_LANE_REPO, worktree="/w/a", **kw) is None, "same worktree"
        assert testslots.suite_hold("pa", repo=free_repo, **{**kw, "priority": 2}) is None, "a routine run waits"
        told = []
        first = testslots.suite_hold("pa", repo=free_repo, worktree="/w/c", tell=told.append, **kw)
        assert first and "-x." in first.files[0].name, "a p1 starts at once past a cap full of p2 runs"
        rec, = [r for r in locks.holder_records(testslots.lane_paths("pa", repo=None)) if r.get("over_cap")]
        assert rec["overflow"] is True and any("cap" in x for x in told), told
        assert testslots.suite_hold("pa", repo=_LANE_REPO, worktree="/w/d", **kw) is None, \
            "one over-cap lane at a time: a second p1 waits while the first is held"
        first.close()
        second = testslots.suite_hold("pa", repo=_LANE_REPO, worktree="/w/d", **kw)
        assert second and "-x." in second.files[0].name, "its own repository's lanes full: the same overflow"
        second.close()
        p1 = testslots.suite_hold("pa", repo=free_repo, slots=1, cap=3, priority=1, wait_s=0)
        assert p1 and "-x." not in p1.files[0].name, "under the cap: an ordinary lane"
        try:
            assert testslots.suite_hold("pa", repo="d00d00000000", worktree="/w/e", **{**kw, "cap": 3}) is None, \
                "a cap holder as urgent as the waiter: no overflow"
        finally:
            p1.close()
    finally:
        for g in hold:
            g and g.close()


def test_a_landing_check_never_waits_behind_task_checks_on_the_project_cap(env, monkeypatch):
    # The project's cap of 2 is full of task runs and the repository's lane is held. A task-less landing
    # check (a push batch's or another landing one) starts at once on the landing lane, without headroom
    # or priority; a second landing check waits while it is held; a priority-1 run keeps its own over-cap
    # lane beside it; an ordinary run still waits. Only a run under a landing mark or the coordinator's task-less run lands.
    from ttp import cli, locks, testslots
    _lane_env(monkeypatch)
    hold = [testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, cap=2, priority=2, worktree="/w/a", wait_s=0),
            testslots.suite_hold("pa", repo=_REPO_B, slots=1, cap=2, priority=2, worktree="/w/b", wait_s=0)]
    try:
        assert all(g and g.files for g in hold) and len(testslots.project_held("pa")) == 2
        assert testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, cap=2, wait_s=0) is None, "an ordinary run waits"
        told = []
        land = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, cap=2, wait_s=0, landing=True, tell=told.append)
        assert land and "-x." in land.files[0].name, "a landing check starts at once past the lanes and the cap"
        rec, = [r for r in locks.holder_records(testslots.lane_paths("pa", repo=None)) if r.get("landing")]
        assert rec["over_cap"] is True and any("landing lane" in x for x in told), told
        assert testslots.suite_hold("pa", repo="c0ffee000000", slots=1, cap=2, wait_s=0, landing=True) is None, \
            "one landing lane per project at a time"
        p1 = testslots.suite_hold("pa", repo="c0ffee000000", slots=1, cap=2, priority=1, wait_s=0,
                                  worktree="/w/c", headroom=lambda: True)
        assert p1 and "-x." in p1.files[0].name, "a priority-1 run keeps its own over-cap lane beside it"
        p1.close()
        land.close()
        # The coordinator's task-less run holds a landing lane: a run under a landing mark (a push batch's)
        # still starts at once on its own lane, a second coordinator run waits, and so does a second mark run.
        coord = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, cap=2, wait_s=0, landing="coordinator")
        assert coord and "-x." in coord.files[0].name, "the coordinator's run takes a landing lane past the cap"
        try:
            mark = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, cap=2, wait_s=0, landing="mark")
            assert mark and "-x." in mark.files[0].name, \
                "a landing-mark run never waits behind the coordinator's landing lane"
            kinds = sorted(r["landing"] for r in locks.holder_records(testslots.lane_paths("pa", repo=None))
                           if r.get("landing"))
            assert kinds == ["coordinator", "mark"], kinds
            assert testslots.suite_hold("pa", repo="c0ffee000000", slots=1, cap=2, wait_s=0,
                                        landing="coordinator") is None, "one coordinator landing lane at a time"
            assert testslots.suite_hold("pa", repo="c0ffee000000", slots=1, cap=2, wait_s=0,
                                        landing="mark") is None, "one mark landing lane at a time"
            mark.close()
        finally:
            coord.close()
        # A mark run holds the lane: the coordinator's run queues under the cap like a task's.
        mark = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, cap=2, wait_s=0, landing="mark")
        try:
            assert mark and testslots.suite_hold("pa", repo="c0ffee000000", slots=1, cap=2, wait_s=0,
                                                 landing="coordinator") is None
        finally:
            mark and mark.close()
    finally:
        for g in hold:
            g and g.close()
    import types
    proj = types.SimpleNamespace(name="pa")
    from ttp import coordtools, landmark
    monkeypatch.delenv("TTP_TASK", raising=False)
    monkeypatch.delenv(landmark.ENV, raising=False)
    monkeypatch.delenv(coordtools.ROLE_ENV, raising=False)
    assert not cli._landing_check(proj), "a plain task-less run (a user's, a test's) shares the project's lanes"
    monkeypatch.setenv(coordtools.ROLE_ENV, "worker")
    assert not cli._landing_check(proj), "a worker run without a task is no landing check"
    monkeypatch.setenv(coordtools.ROLE_ENV, "coordinator")
    assert cli._landing_check(proj) and not cli._landing_check(None), "the coordinator's own run lands"
    monkeypatch.setenv("TTP_TASK", "12")
    assert not cli._landing_check(proj), "a task's own checks queue under the cap as before"
    monkeypatch.delenv(coordtools.ROLE_ENV)
    monkeypatch.setenv(landmark.ENV, "/nonexistent/forged-mark")
    assert not cli._landing_check(proj), "a forged landing mark path is no landing check"
    monkeypatch.setattr(landmark, "label", lambda env=None: {"pid": 1})
    assert cli._landing_check(proj) == "mark", "a run under a verified landing mark lands, even inside a task"
    monkeypatch.delenv("TTP_TASK")
    monkeypatch.setenv(coordtools.ROLE_ENV, "coordinator")
    assert cli._landing_check(proj) == "mark", "the coordinator's run under a landing mark is a mark run"
    monkeypatch.setattr(landmark, "label", lambda env=None: None)
    assert cli._landing_check(proj) == "coordinator"


def test_checks_slot_take_passes_its_landing_kind_to_the_lane_hold(env, monkeypatch, tmp_path):
    # _checks_slot_take hands _landing_check(p) to the lane hold as `landing`, so a push batch's checks
    # take the mark lane and the coordinator's own run its lane.
    import types
    from ttp import cli, testslots
    _lane_env(monkeypatch)
    proj = types.SimpleNamespace(name="pa", state=tmp_path / "state")
    seen = []
    monkeypatch.setattr(testslots, "suite_hold", lambda *a, **kw: seen.append(kw) or None)
    for kind in ("mark", "coordinator", ""):
        seen.clear()
        monkeypatch.setattr(cli, "_landing_check", lambda p, kind=kind: kind if p is proj else "")
        got = cli._checks_slot_take(proj, tmp_path / "run", None, "a" * 40, None, lambda *a: None, 0.0,
                                    time.time(), "", None, 2, None, {}, _LANE_REPO, ["true"], "checks")
        assert got is None and seen and seen[-1].get("landing") == kind, (kind, seen)


def test_a_fixed_project_cap_holds_urgent_and_priority_1_checks_runs_too(env, monkeypatch, tmp_path):
    # Upstream note 7824b6557438f9ff (a): a fixed checks.test_project_max holds. A p1 run takes no over-cap
    # lane past a fixed cap full of routine runs, and an urgent run granted a reserved urgent slot still waits
    # for room under the fixed cap (claim_running), never running beside the cap's holder; a sized cap keeps
    # both shortcuts.
    import io
    import types
    from ttp import checkcap, cli, testslots
    _lane_env(monkeypatch)
    monkeypatch.delenv("TTP_TEST_PROJECT_MAX", raising=False)
    kw = dict(slots=1, cap=1, priority=1, wait_s=0, worktree="/w/c", headroom=lambda: True)
    routine = testslots.suite_hold("pa", repo=_REPO_B, slots=1, cap=1, priority=2, worktree="/w/b", wait_s=0,
                                   pending=True, running=True)
    try:
        assert routine and testslots.claim_running(routine, "pa", lambda n: 12, 1)
        assert testslots.suite_hold("pa", repo=_LANE_REPO, cap_fixed=True, **kw) is None, \
            "a fixed cap full of routine runs: the p1 run waits"
        p1 = testslots.suite_hold("pa", repo=_LANE_REPO, **kw)
        assert p1 and "-x." in p1.files[0].name, "a sized cap: the p1 run still takes its over-cap lane"
        p1.close()

        closed = []

        class Slot:
            urgent, served, tests, f = True, False, None, None

            def close(self):
                closed.append(True)

        monkeypatch.setattr(checkcap, "acquire", lambda *a, **k: Slot())
        monkeypatch.setattr(checkcap, "urgent_key", lambda s: "urgent:test")
        monkeypatch.setattr(testslots, "acquire", lambda *a, **k: testslots.Grant(0, []))
        cfg = {"checks": {"test_project_max": 1}}
        proj = types.SimpleNamespace(name="pa", state=tmp_path / "state", config=lambda: cfg)
        take = lambda c: cli._checks_slot_take(   # noqa: E731
            proj, tmp_path / "run", io.StringIO(), "a" * 40, None, lambda *a: None, 0.5, time.time(), "", None, 0,
            [], c, _LANE_REPO, ["true"], "checks", urgent=True)
        assert take(cfg) is None and closed, "an urgent run waits for room under a fixed cap, its slot handed back"
        assert len(testslots.project_held("pa")) == 1, "only the cap's holder runs"
        cfg = {}
        got = take(cfg)
        try:
            assert got is not None and got.urgent, "a sized cap: the reserved urgent slot runs past it"
        finally:
            got and got.suite.close()
    finally:
        routine.close()


def test_a_landing_check_is_never_held_by_lanes_whose_runs_wait_for_their_grants(env, monkeypatch, tmp_path):
    # Upstream note 7824b6557438f9ff (b): push checks (a push batch's `ttp checks` under a landing mark) take
    # their lane through _checks_slot_take, which asks suite_hold to count only running suites (pending and
    # running): lanes held by runs still waiting for their host grants never block it, even under a fixed cap.
    import types
    from ttp import cli, testslots
    _lane_env(monkeypatch)
    seen = []
    real = testslots.suite_hold
    monkeypatch.setattr(testslots, "suite_hold", lambda *a, **kw: seen.append(kw) or None)
    proj = types.SimpleNamespace(name="pa", state=tmp_path / "state")
    monkeypatch.setattr(cli, "_landing_check", lambda p: "mark")
    cli._checks_slot_take(proj, tmp_path / "run", None, "a" * 40, None, lambda *a: None, 0.0, time.time(), "",
                          None, 2, None, {"checks": {"test_project_max": 1}}, _LANE_REPO, ["true"], "checks")
    assert seen and seen[-1].get("pending") is True and seen[-1].get("running") is True
    assert seen[-1].get("cap") == 1 and seen[-1].get("cap_fixed") is True, seen[-1]
    monkeypatch.setattr(testslots, "suite_hold", real)
    queued = [real("pa", repo=r, slots=1, cap=1, priority=2, wait_s=0, pending=True, running=True,
                   worktree=f"/w/{r}") for r in (_REPO_B, "c0ffee000000")]
    try:
        assert all(g and g.files for g in queued) and not testslots.project_held("pa"), "none running yet"
        mark = real("pa", repo=_LANE_REPO, slots=1, cap=1, wait_s=0, pending=True, running=True,
                    landing="mark", cap_fixed=True)
        assert mark and "-x." not in mark.files[0].name, "its own lane at once: queued holders do not count"
        assert testslots.claim_running(mark, "pa", lambda n: 1, 1), "and it runs once granted"
        mark.close()
    finally:
        for g in queued:
            g and g.close()


def test_obligation_owners_rank_by_their_earliest_due(env, monkeypatch):
    # Upstream note 7824b6557438f9ff (c): every owner used to rank OBLIGATION_PRIORITY whatever its due. An
    # owner due sooner (obligations.owner_due) ranks before one due later, all before every task priority, and
    # its queue ticket carries the rank, so the checks queue serves the nearest due first.
    from ttp import checkcap, locks
    from ttp import obligations as ob
    p = make(env)
    now = time.time()
    tasks = {k: p.db.add_task(k, "s", kind="code", tier="light", origin="coordinator", priority=1)
             for k in ("none", "day", "soon", "late")}
    for k, due in (("day", now + 20 * 3600), ("soon", now + 1800), ("late", now - 60)):
        ob.add(p, f"report {k}", [tasks[k]], milestones=[{"key": "m1", "due": due, "evidence_required": "a file"}])
    ob.add(p, "report none", [tasks["none"]])
    rank = {}
    for k, tid in tasks.items():
        monkeypatch.setenv("TTP_TASK", str(tid))
        rank[k], owns = checkcap.task_rank(p)
        assert owns, k
    assert rank["none"] == checkcap.OBLIGATION_PRIORITY
    assert rank["late"] <= rank["soon"] < rank["day"] < rank["none"] < 1, rank
    assert checkcap.owed_rank(now + 3 * 86400, now) == checkcap.OBLIGATION_PRIORITY, "a far due: the plain boost"
    tickets = [locks.enqueue(checkcap.slot_dir(), checkcap.RESOURCE, k, now + i, priority=rank[k])
               for i, k in enumerate(("none", "day", "soon"))]
    try:
        assert sorted(tickets, key=locks.ticket_priority) == tickets[::-1], "the ticket carries the due's rank"
        assert checkcap._urgent_rank(["o1"], now, rank["soon"]) < checkcap._urgent_rank(["o2"], now - 9, rank["day"])
    finally:
        for t in tickets:
            locks.dequeue(t)


def test_cap_turn_counts_one_lane_for_the_old_unkeyed_base(env, monkeypatch):
    # The old one-slot base ('test-suite.<gid>', no repository key) has one lane: with it held, its aged
    # waiter cannot start and must not take the cap's last room from a repository waiter with a free lane.
    from ttp import checkcap, locks, testslots
    _lane_env(monkeypatch)
    old = testslots.suite_hold("pa", slots=1, wait_s=0)
    now = time.time()
    aged = _suite_ticket("pa", 4, now - 10 * testslots.SUITE_AGE_S)
    mine = locks.enqueue(checkcap.slot_dir(), testslots.suite_base("pa", _LANE_REPO), "x", now, priority=3)
    try:
        assert old and old.files and len(testslots.project_held("pa")) == 1
        assert testslots.cap_turn("pa", 2, mine, 4, now=now), "the old base's waiter has no free lane"
    finally:
        for t in (aged, mine):
            locks.dequeue(t)
        old.close()


def test_checks_run_times_rows_carry_the_scratch_kind_and_old_rows_without_it_still_read(env, monkeypatch):
    from ttp import checklane, checkwall
    p = make(env)
    log = p.state / checkwall.RUNS_LOG
    # A row written before the scratch key existed.
    log.write_text(json.dumps({"ts": time.time(), "s": 100.0, "passed": True, "k": "abc"}) + "\n")
    checkwall.record_run(p.state, 300, True, "abc", scratch="tmpfs")
    checkwall.record_run(p.state, 500, True, "abc", scratch="disk")
    checkwall.record_run(p.state, 700, False)
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert [r.get("scratch") for r in rows] == [None, "tmpfs", "disk", None], rows
    assert checkwall.recent(p.state) == [100.0, 300.0, 500.0, 700.0]
    assert len(checkwall.run_spans(p.state)) == 4
    assert checklane.median_s(p.state, "abc") == 300.0, "old and new rows of one command list both count"


def test_ttp_checks_records_its_scratch_kind_in_the_run_times_log(env, tmp_path, monkeypatch, capsys):
    from ttp import checkwall, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    cli.main(["checks", "--", "true"])
    row = json.loads((p.state / checkwall.RUNS_LOG).read_text().splitlines()[-1])
    assert row["scratch"] == "disk" and row["passed"] is True, row


def test_suite_wait_line_names_every_holder_past_the_first_three(env):
    from ttp import testslots
    now = time.time()
    recs = [{"task": t, "command": "pytest -q", "ts": now - 60} for t in (11, 12, 13, 14, 15)]
    text = testslots.holders_text(recs, now)
    assert all(f"#{t}" in text for t in (11, 12, 13, 14, 15)), text
    assert text.endswith("; +2 more: #14, #15") and text.count("'pytest -q'") == 3, text
    four = testslots.holders_text(recs[:4], now)
    assert four.endswith("; +1 more: #14"), "at cap 4 the 4th holder is named"
    assert testslots.holders_text(recs[:3], now).count("more") == 0
    assert testslots.holders_text([], now) == "a run that just ended"
    assert testslots.holders_text([{"holder": "pb: full checks (pid 9)"}] * 4, now).endswith(
        "+1 more: pb: full checks (pid 9)"), "a holder without a task is named by its label"


def test_a_runs_second_detached_checks_append_to_checks_out_keeping_the_first_runs_lines(
        env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    out, rc = run / cli.CHECKS_OUT, run / cli.CHECKS_RC
    cli.main(["checks", "--detach", "--fresh", "--", "true"])
    assert _wait_for(rc.exists, 60), out.read_text()
    first = out.read_text()
    assert first.strip(), "the first run wrote its lines"
    cli.main(["checks", "--detach", "--fresh", "--", "sh", "-c", "exit 0"])
    assert _wait_for(rc.exists, 60), out.read_text()
    both = out.read_text()
    assert both.startswith(first) and len(both) > len(first), both


def test_an_overflow_lane_is_taken_only_while_the_test_pool_lets_the_run_start(env, tmp_path, monkeypatch):
    """A run that took an extra lane and then waited ~12 min for host test slots held it with no check
    running: an overflow lane goes only to a run the pool lets start now (testslots.pool_room)."""
    from ttp import checkcap, checklane, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: (2, 1))
    monkeypatch.setattr(testslots, "pool_now", lambda cfg, pool, held, *a, **k: pool)
    assert testslots.pool_room({}, "pa")
    taken = [locks.try_take(testslots.paths(2), "other run", "test slots") for _ in range(2)]
    assert all(taken) and not testslots.pool_room({}, "pa"), "a full pool: no room"
    taken.pop().close()
    assert testslots.pool_room({}, "pa")
    t = locks.enqueue(checkcap.slot_dir(), testslots.TESTS.resource, "a waiter", time.time())
    assert not testslots.pool_room({}, "pa"), "a free slot a queued waiter takes next is no room"
    locks.dequeue(t)
    holder = _lane_holder(tmp_path, "pa", _LANE_REPO, cost={"cls": checklane.SUITE, "est_s": 600})
    try:
        room = {"now": False}
        cheap = {"cls": checklane.OTHER}
        assert testslots.suite_hold("pa", repo=_LANE_REPO, cost=cheap, max_wait=0.1, wait_s=0.6,
                                    startable=lambda: room["now"]) is None, "an extra lane with no test slots"
        room["now"] = True
        g = testslots.suite_hold("pa", repo=_LANE_REPO, cost=cheap, max_wait=0.1, wait_s=5,
                                 startable=lambda: room["now"])
        assert g and g.files and "-x." in g.files[0].name
        g.close()
    finally:
        _stop(holder)
        for f in taken:
            f.close()


def test_a_holder_waiting_for_test_slots_says_so_and_is_no_likely_hang(env, tmp_path, monkeypatch):
    """The lane label says what a held, not yet running lane waits for (testslots.lane_waits), and the
    queue's hang wake times a holder from its grant, never across that wait."""
    from ttp import checklane, locks, testslots
    _lane_env(monkeypatch)
    g = testslots.suite_hold("pa", repo=_LANE_REPO, wait_s=5, pending=True)
    try:
        testslots.lane_waits(g, "host test slots")
        rec = locks.holder_records([Path(f.name) for f in g.files])[0]
        now = float(rec["since"]) + 1200
        assert "not running yet (waiting for host test slots)" in checklane.holder_text(rec, now)
        evs = _queue_events(1, holders=[rec])
        cfg = {"checks": {"wait_hang_factor": 3}}
        assert checklane.wait_wake(evs, {"start": 1.0}, cfg, now, 300.0)[0] == [], "waiting is no hang"
        testslots.slots_granted(g)
        rec = locks.holder_records([Path(f.name) for f in g.files])[0]
        assert not rec.get("waits") and rec.get("granted") is True
        assert "waiting for" not in checklane.holder_text(rec, now)
        evs = _queue_events(1, holders=[rec])
        assert checklane.wait_wake(evs, {"start": 1.0}, cfg, float(rec["granted_at"]) + 60, 300.0)[0] == [], \
            "its clock starts at its grant, not when it took the lane"
        log = tmp_path / "checks.log"   # its commands' log, never grown since its grant: stalled (checkstall)
        log.write_text("")
        os.utime(log, (float(rec["granted_at"]),) * 2)
        evs = _queue_events(1, holders=[{**rec, "log": str(log), "run_since": float(rec["granted_at"])}])
        assert checklane.wait_wake(evs, {"start": 1.0}, cfg, float(rec["granted_at"]) + 1000, 300.0)[0] == ["hang"]
    finally:
        g.close()


def test_the_host_checks_cap_prices_a_check_at_one_runs_measured_cores(monkeypatch):
    """checkcap.default_limit fits the spare cores at one run's cores (checklane.run_cores, as the lanes
    count them), not at a fixed 4; with the test pool off it keeps CHECK_LOAD."""
    from ttp import checkcap, checklane, testslots
    monkeypatch.delenv("TTP_CHECKS_HOST_MAX", raising=False)
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    monkeypatch.setattr(checkcap, "_load", lambda: 16.0)
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: (32, 8))
    assert checklane.run_cores() == 8 and checkcap.check_load() == 8.0
    assert checkcap.default_limit() == 6, "48 spare cores hold 6 runs of 8 cores"
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: (32, 16))
    assert checkcap.check_load() == 16.0 and checkcap.default_limit() == 4, "the floor: 64 // 16"
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: None)
    assert checkcap.check_load() == checkcap.CHECK_LOAD == 4.0
    assert checkcap.default_limit() == 8, "the pool off: 48 // 4, at most the ceiling of 8"


def test_the_host_checks_cap_prices_a_check_at_its_measured_cpu_with_process_churn(env, monkeypatch):
    """A run's measured cost (its children's user and system CPU per wall second: checkcap.record_cost)
    raises the price of a check over one run's granted cores, so a churning suite admits fewer checks."""
    from ttp import checkcap, testslots
    monkeypatch.delenv("TTP_CHECKS_HOST_MAX", raising=False)
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    monkeypatch.setattr(checkcap, "_load", lambda: 0.0)
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: (32, 8))
    assert checkcap.measured_cores() is None and checkcap.check_load() == 8.0
    assert checkcap.default_limit() == 8, "unmeasured: 64 // 8"
    checkcap.record_cost(30.0, 30.0)   # too short a run: no sample
    checkcap.record_cost(0.0, 300.0)   # no CPU read: no sample
    assert checkcap.measured_cores() is None
    for _ in range(checkcap.COST_MIN_SAMPLES - 1):
        checkcap.record_cost(4500.0, 300.0)   # 15 cores, about 40% of it system CPU from process churn
    assert checkcap.measured_cores() is None and checkcap.check_load() == 8.0, "too few runs: the default price"
    checkcap.record_cost(4500.0, 300.0)
    checkcap.record_cost(600.0, 300.0)    # a light run after them does not lower the price
    assert checkcap.measured_cores() == 15.0 and checkcap.check_load() == 15.0
    assert checkcap.default_limit() == 4, "64 // 15 is 4, the floor"
    monkeypatch.setattr(checkcap, "cores", lambda: 12)
    assert checkcap.measured_cores() == 12.0, "never above the host's cores"
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    assert checkcap.measured_cores(now=time.time() + checkcap.COST_KEEP_S + 1) is None, "old samples expire"
    (checkcap.slot_dir() / checkcap.COST_LOG).write_text(
        "{torn\n" + "".join(json.dumps({"ts": time.time(), "cores": 9}) + "\n" for _ in range(5)))
    assert checkcap.measured_cores() == 9.0, "a torn row is skipped"
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: None)
    assert checkcap.check_load() == 9.0, "measured beats CHECK_LOAD with the test pool off"
    (checkcap.slot_dir() / checkcap.COST_LOG).unlink()
    assert checkcap.check_load() == checkcap.CHECK_LOAD


def test_one_heavy_checks_run_among_normal_ones_never_sets_the_check_price(env, monkeypatch):
    """measured_cores prices a check at a high percentile of recent runs, not the costliest, so one
    outlier run (here 40 cores among 6-core runs) neither raises the price nor makes the live-cores gate
    hold every project's checks for 40 free cores; heavy runs that are the norm still raise it."""
    from ttp import checkcap, hostcpu, testslots
    monkeypatch.delenv("TTP_CHECKS_HOST_MAX", raising=False)
    monkeypatch.delenv(checkcap.LIVE_ENV, raising=False)
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: None)

    def runs(*cores):
        (checkcap.slot_dir() / checkcap.COST_LOG).unlink(missing_ok=True)
        for c in cores:
            checkcap.record_cost(c * 300.0, 300.0)
    for normal in (4, 10, 19):   # the fewest runs that price, a half window, a full one
        for at in (0, normal // 2, normal):   # the outlier first, in the middle, last
            got = [6.0] * normal
            got.insert(at, 40.0)
            runs(*got)
            assert checkcap.measured_cores() == 6.0 and checkcap.check_load() == 6.0, (normal, at)
    runs(6, 6, 6, 40)
    assert checkcap.measured_cores() is None and checkcap.check_load() == checkcap.CHECK_LOAD, "too few runs"
    runs(*[6] * 15, *[40] * 5)
    assert checkcap.measured_cores() == 40.0, "a fifth of recent runs heavy: heavy is this host's norm"
    runs(*[6] * 19, 40)
    every = checkcap.all_paths(4)
    monkeypatch.setattr(checkcap, "held_since", lambda paths: [0.0])   # one check running, granted long ago
    monkeypatch.setattr(hostcpu, "busy_cores", lambda: 50.0)
    assert checkcap.cores_short(every) == "", "14 free cores hold one 6-core check"


def test_ttp_checks_records_its_cost_with_its_childrens_system_cpu(env, tmp_path, monkeypatch, capsys):
    """`ttp checks` keeps its commands' user and system CPU (forks and execs included) as the host's
    measured cost of a check."""
    from ttp import checkcap, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(checkcap, "COST_MIN_WALL_S", 0.0)
    churn = "sh -c 'i=0; while [ $i -lt 300 ]; do /bin/true; i=$((i+1)); done'"
    cli.main(["checks", "--", churn])
    rows = [json.loads(x) for x in (checkcap.slot_dir() / checkcap.COST_LOG).read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["cores"] > 0 and rows[0]["s"] >= 0, rows
    monkeypatch.setattr(checkcap, "COST_MIN_SAMPLES", 1)
    assert checkcap.measured_cores() == rows[0]["cores"]


def test_a_review_precheck_waiting_for_test_slots_keeps_its_lane_while_its_review_is_open(env, monkeypatch):
    """A review precheck that holds its lane while it waits for host test slots (no command running yet)
    is never a finished task's holder nor a likely hang while a review of its head is open (sa24); once
    none is, it is a finished task's lane again."""
    from ttp import checklane, daemon, locks, testslots, worktree
    p = make(env)
    _lane_env(monkeypatch)
    tid = p.db.add_task("t", "s", kind="code")
    head = worktree._git(p.root, "rev-parse", "HEAD")
    out = p.state / daemon.PRECHECKS_DIR / f"t{tid}-{head[:12]}"   # Daemon._precheck's run folder
    out.mkdir(parents=True)
    rev = p.db.add_task("review", "r", kind="review", labels=[checklane.PRECHECK_LABEL + out.name])
    for k, v in {"TTP_RUN_DIR": str(out), "TTP_PROJECT": str(p.base), "TTP_RUN_ID": daemon.precheck_run_id(out),
                 **daemon.task_env(tid)}.items():   # as Daemon._start_checks sets them
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(p.root)
    g = testslots.suite_hold(p.name, wait_s=5, pending=True, who=f"run {out.name}")   # as cli._checks_slot's hold
    try:
        assert g and g.files
        testslots.lane_waits(g, "host test slots")
        rec = locks.holder_records([Path(f.name) for f in g.files])[0]
        assert rec.get("waits") == "host test slots" and str(rec.get("task")) == str(tid), rec
        later = time.time() + 400   # past the 5 min flag
        p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (time.time(), tid))
        assert checklane.finished_holders(p, {}, later) == [], "the precheck its open review reads keeps its lane"
        cfg = {"checks": {"wait_hang_factor": 3}}
        assert checklane.wait_wake(_queue_events(1, holders=[rec]), {"start": 1.0}, cfg, later + 1200, 300.0,
                                   ended=lambda h: bool(checklane.finished_holders(p, {}, later)))[0] == []
        p.db.x("UPDATE tasks SET status='done' WHERE id=?", (rev,))
        h = checklane.finished_holders(p, {}, later)
        assert len(h) == 1 and h[0]["task"] == str(tid), h
    finally:
        g.close()


def test_hostcpu_busy_cores_and_checkcap_cores_count_the_same_cores_under_a_restricted_affinity(monkeypatch):
    # busy cores scale by the cores checkcap counts (the CPU affinity, else os.cpu_count()), so a
    # process pinned to 4 of 64 cores never reads 32 busy cores out of 4.
    import os
    from ttp import checkcap, hostcpu
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1, 2, 3}, raising=False)
    monkeypatch.setattr(hostcpu, "_LAST", {})
    stats = ["cpu  1000 0 0 1000 0 0 0 0 0 0\n", "cpu  1050 0 0 1050 0 0 0 0 0 0\n"]
    clock = [100.0]

    def sleep(s):
        clock[0] += s
        stats.pop(0)
    assert checkcap.cores() == hostcpu.cores() == 4
    assert hostcpu.busy_cores(lambda: stats[0], sleep, lambda: clock[0]) == 2.0, "half of the 4 cores"
    monkeypatch.setattr(hostcpu, "busy_cores", lambda: 2.0)
    assert checkcap.free_cores() == 2.0
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)   # macOS: no affinity
    assert checkcap.cores() == hostcpu.cores() == 64


def test_hostcpu_busy_cores_reads_proc_stat_deltas_and_spawns_nothing(monkeypatch):
    # Busy cores come from /proc/stat's aggregate counters over the last few seconds; no process spawns.
    import subprocess
    from ttp import hostcpu
    monkeypatch.setattr(hostcpu, "_LAST", {})
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    clock, stats, slept = [100.0], [], []

    def stat(busy, idle):   # user nice system idle iowait irq softirq steal guest guest_nice
        return f"cpu  {busy} 0 0 {idle} 0 0 0 0 7 7\ncpu0 1 2 3 4\n"

    def read():
        return stats[0]

    def sleep(s):
        slept.append(s)
        clock[0] += s
        stats.pop(0)
    stats[:] = [stat(1000, 1000), stat(1000 + 75, 1000 + 25)]
    assert hostcpu.busy_cores(read, sleep, lambda: clock[0], cpus=64) == 48.0, "75% of 64 cores over a fresh window"
    assert slept == [hostcpu.SAMPLE_S], "no earlier reading: one SAMPLE_S window"
    assert hostcpu.busy_cores(read, sleep, lambda: clock[0], cpus=64) == 48.0, "under SAMPLE_S later: the last value"
    clock[0] += 5.0
    stats[:] = [stat(1075 + 10, 1025 + 90)]
    assert hostcpu.busy_cores(read, sleep, lambda: clock[0], cpus=64) == 6.4, "since the last reading, no sleep"
    assert slept == [hostcpu.SAMPLE_S]
    clock[0] += hostcpu.FRESH_S + 1
    stats[:] = [stat(5000, 5000), stat(5000, 5100)]
    assert hostcpu.busy_cores(read, sleep, lambda: clock[0], cpus=64) == 0.0, "a stale reading starts a fresh window"
    clock[0] += 5.0
    assert hostcpu.busy_cores(lambda: (_ for _ in ()).throw(OSError()), sleep, lambda: clock[0]) is None
    assert hostcpu.jiffies("intr 1 2\n") is None and hostcpu.jiffies("cpu  1 2\n") is None


def test_checks_live_cores_gate_waits_on_a_saturated_host_and_always_admits_one(env, monkeypatch):
    # A check slot starts a suite only while the host's live free cores cover one check's price; with no
    # check running one always starts, so checks never starve.
    from ttp import checkcap, hostcpu
    monkeypatch.setenv("TTP_CHECKS_LIVE_CORES", "1")
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "8")
    monkeypatch.setattr(checkcap, "POLL_S", 0.02)
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    monkeypatch.setattr(checkcap, "check_load", lambda: 8.0)
    busy = [60.0]
    monkeypatch.setattr(hostcpu, "busy_cores", lambda: busy[0])
    first = checkcap.acquire("first")
    try:
        assert first.f is not None, "the floor: none running, so one starts on a saturated host"
        told = []
        assert checkcap.acquire("second", tell=told.append, max_wait_s=0.3) is None, "4 cores free: it waits"
        # 4 free, less 8 for the first check granted within RAMP_S
        assert any("waiting for free cores: 0 of 64 cores free now, one check takes 8" in x for x in told), told
        busy[0] = 10.0
        second = checkcap.acquire("second", max_wait_s=5)
        assert second is not None and second.f is not None, "an idle host admits it"
        busy[0] = 41.0   # 23 free, less 8 for each of the two checks granted within RAMP_S: 7
        assert checkcap.acquire("third", max_wait_s=0.2) is None, "just-granted checks count before they show"
        monkeypatch.setattr(checkcap, "RAMP_S", 0.0)
        third = checkcap.acquire("third", max_wait_s=5)
        assert third is not None and third.f is not None, "once they show in the busy cores, 23 free admit one"
        third.close()
        monkeypatch.setattr(hostcpu, "busy_cores", lambda: None)   # no /proc/stat: the load average
        monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (70.0, 0.0, 0.0))
        assert checkcap.cores_short(checkcap.all_paths(8)) == "0 of 64 cores free now, one check takes 8"
        monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (_ for _ in ()).throw(OSError()))
        assert checkcap.cores_short(checkcap.all_paths(8)) == "", "nothing readable: no gate"
        monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (70.0, 0.0, 0.0))
        monkeypatch.setenv("TTP_CHECKS_LIVE_CORES", "0")
        assert checkcap.cores_short(checkcap.all_paths(8)) == "", "checks.live_cores 0 turns it off"
        second.close()
    finally:
        first.close()


def test_test_slots_grant_follows_the_hosts_live_free_cores(env, monkeypatch):
    # Each run's xdist workers are min(its share, the host's free cores less workers just granted), at least 2.
    from ttp import checkcap, hostcpu, testslots
    monkeypatch.setenv("TTP_CHECKS_LIVE_CORES", "1")
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    busy = [0.0]
    monkeypatch.setattr(hostcpu, "busy_cores", lambda: busy[0])
    assert testslots.sizes() == (32, 8)
    with testslots.acquire("idle") as g:
        assert g.k == 8 and g.env()[testslots.XDIST_ENV] == "8", "an idle host: the full share"
    busy[0] = 58.5
    told = []
    with testslots.acquire("busy", tell=told.append) as g:
        assert g.k == 5 and g.env()[testslots.XDIST_ENV] == "5", "5.5 cores free: 5 workers"
        assert "sized to the host's free cores" in told[-1], told
        busy[0] = 54.0   # 10 free, less the 5 workers granted just now
        with testslots.acquire("next") as h:
            assert h.k == 5 and h.env()[testslots.XDIST_ENV] == "5"
    busy[0] = 64.0
    with testslots.acquire("saturated") as g:
        assert g.k == testslots.WORKERS_FLOOR == 2, "never below the floor"
    monkeypatch.setenv("TTP_CHECKS_LIVE_CORES", "0")
    with testslots.acquire("off") as g:
        assert g.k == 8, "checks.live_cores 0: the share alone"


def test_a_wait_for_a_checks_slot_reads_pids_once_per_cycle_without_proc(env, tmp_path, monkeypatch):
    """Without /proc, the pids checkcap.acquire's wait reads each poll cycle (its stop()/done() callbacks,
    as cli's stop -> yielder -> checkjoin does) come from one `ps` per cycle (ps_snapshot), never one
    `ps` per pid read."""
    import os
    import subprocess
    from ttp import checkcap, project

    monkeypatch.delenv(checkcap.HELD_ENV, raising=False)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "3")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")   # no slots beyond the cap: the waiter waits
    monkeypatch.setattr(project, "_PROC", False)
    monkeypatch.setattr(checkcap, "POLL_S", 0.01)
    held = [checkcap.acquire(f"h{i}") for i in range(3)]   # the cap is full: a waiter polls, not takes
    calls, cycles, pids = _counting_ps(monkeypatch), [], [os.getpid(), os.getppid(), 1]

    def stop_():
        cycles.append(1)
        for pid in pids:   # the callbacks' per-cycle pid reads (checkjoin's holders)
            project.zombie(pid)
        return "stop" if len(cycles) > 2 else ""

    def wait():
        try:
            checkcap.acquire("w", stop=stop_, max_wait_s=30)
        except checkcap.Cancelled:
            pass

    try:
        t = threading.Thread(target=wait)
        t.start()
        assert _wait_for(lambda: len(checkcap.state()["queued"]) == 1), checkcap.state()
        t.join(30)
    finally:
        for s in held:
            s.close()
    assert len(cycles) >= 3, "the waiter polled at least 3 cycles"
    # One `ps` per poll cycle, however many pids the callbacks read: they answer from one snapshot
    # (ps_snapshot, renewed by ps_cycle); without it each pid read is its own `ps` (3 per cycle).
    assert calls, "the callbacks' pid reads ran `ps`"
    assert len(calls) <= len(cycles) + 1, \
        f"{len(calls)} ps calls for {len(cycles)} poll cycles of {len(pids)} pid reads"


# A cancelled task's checks (jobstop.end_cancelled_checks): stopped at the cancel, their lane freed at once.

def _cancel_holder(p, tmp_path, tid: int, name: str, repo: str = _LANE_REPO, project=None):
    """A process of task `tid` holding one of project `p`'s checks lanes in a process group of its own, as an
    attached `ttp checks` in a worker's tool shell does (carrying its project's TTP_PROJECT, or `project`'s)."""
    got = tmp_path / f"held-{name}"
    proc = subprocess.Popen([sys.executable, "-c", _DONE_HOLDER, p.name, str(got), repo, f"run {name}"],
                            env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_TASK": str(tid),
                                 "TTP_PROJECT": str(p.base if project is None else project)},
                            start_new_session=True)
    assert _wait_for(got.exists), "the holder never took its lane"
    return proc


def test_cancelling_a_task_stops_its_attached_and_detached_checks_at_once_and_frees_their_lane(
        env, tmp_path, monkeypatch):
    from ttp import cli, jobstop, locks, testslots
    from ttp.daemon import Daemon
    from ttp.runner import proc_start
    p = make(env)
    _lane_env(monkeypatch)
    tid = p.db.add_task("t", "s", kind="code")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "ok", str(run_dir)))
    holder = _cancel_holder(p, tmp_path, tid, "attached")
    # its detached checks: a group leader named in checks.pid (here waiting, holding no lane)
    detached = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"],
                                env={**os.environ, "TTP_TASK": str(tid)}, start_new_session=True)
    try:
        (run_dir / cli.CHECKS_PID).write_text(json.dumps({"pid": detached.pid, "started": proc_start(detached.pid)}))
        lanes = [x for b in testslots.suite_bases(p.name) for x in testslots._lanes_of(b)
                 if not locks.any_free([x])]
        assert len(lanes) == 1
        d = Daemon(p.base)
        d.stop_cancelled_checks()   # first tick: nothing cancelled, nothing touched
        assert holder.poll() is None and detached.poll() is None
        assert p.db.cancel(tid) == ""
        began = time.monotonic()
        d.stop_cancelled_checks()
        assert time.monotonic() - began < jobstop.CANCEL_TERM_S + 2 * jobstop.FREE_WAIT_S + 5, "the cancel stays fast"
        assert holder.wait(10) is not None and detached.wait(10) is not None
        assert locks.any_free(lanes), "the lane is free at the cancel, not when the check ends by itself"
        texts = [r["text"] for r in p.db.q("SELECT text FROM events WHERE kind=? AND task=?", (jobstop.EVENT, tid))]
        assert len(texts) == 2 and all("cancelled task" in t and "SIGTERM" in t for t in texts), texts
        assert any(f"freed checks lane {lanes[0].name}" in t for t in texts), texts
        d.stop_cancelled_checks()   # nothing new: no second pass, no new event
        assert jobstop.sweep_cancelled_checks(p) == [], "the backstop finds nothing left"
    finally:
        _stop(holder)
        _stop(detached)


def test_cancelling_a_task_never_stops_another_task_s_checks_or_an_open_review_s_precheck(
        env, tmp_path, monkeypatch):
    import shlex
    from ttp import checklane, daemon, jobstop, locks, testslots, worktree
    p = make(env)
    _lane_env(monkeypatch)
    tid = p.db.add_task("t", "s", kind="code")
    other = p.db.add_task("o", "s", kind="code")
    theirs = _cancel_holder(p, tmp_path, other, "theirs")
    # a lane label naming the cancelled task on a process that runs for another one (a reused pid, a hand edit)
    relabelled = _cancel_holder(p, tmp_path, other, "relabelled", "1" * 12)
    lane = next(x for b in testslots.suite_bases(p.name) for x in testslots._lanes_of(b)
                if any(r.get("pid") == relabelled.pid for r in locks.holder_records([x])))
    lane.write_text(json.dumps({**json.loads(lane.read_text()), "task": str(tid)}))
    # a check of the cancelled task whose process group also holds another task's process
    shared = subprocess.Popen(["sh", "-c", f"TTP_TASK={other} sleep 600 & exec {shlex.quote(sys.executable)} -c "
                               f"{shlex.quote(_DONE_HOLDER)} {p.name} {tmp_path / 'held-shared'} {'2' * 12} 'run shared'"],
                              env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_TASK": str(tid)}, start_new_session=True)
    assert _wait_for((tmp_path / "held-shared").exists)
    # the daemon's review precheck of the cancelled task, run as that task, while its review is open
    head = worktree._git(p.root, "rev-parse", "HEAD")
    token = f"t{tid}-{head[:12]}"
    p.db.add_task("review", "r", kind="review", labels=[checklane.PRECHECK_LABEL + token])
    precheck = _cancel_holder(p, tmp_path, tid, token, "3" * 12)
    # another project's checks with the same task id (ids are per project) in a lane of the shared folder
    foreign = _cancel_holder(p, tmp_path, tid, "foreign", "4" * 12, project=tmp_path / "other" / "tt-project")
    anon = _cancel_holder(p, tmp_path, tid, "anon", "5" * 12, project="")   # says no project: never provably ours
    try:
        assert p.db.cancel(tid) == ""
        assert jobstop.cancelled_checks(p, tid) == []
        assert jobstop.end_cancelled_checks(p, tid, "test") == []
        daemon.Daemon(p.base).stop_cancelled_checks()
        assert jobstop.sweep_cancelled_checks(p) == []
        assert all(x.poll() is None for x in (theirs, relabelled, shared, precheck, foreign, anon))
        p.db.cancel(other)   # the other task is cancelled too: only now its own checks stop
        assert [r["pgid"] for r in jobstop.end_cancelled_checks(p, other, "test")] == [theirs.pid]
        assert theirs.wait(10) is not None and precheck.poll() is None and foreign.poll() is None and anon.poll() is None
    finally:
        for x in (theirs, relabelled, precheck, foreign, anon):
            _stop(x)
        _kill_group(shared)


def test_cancelling_a_task_whose_checks_group_already_ended_is_no_error(env, tmp_path, monkeypatch):
    from ttp import cli, jobstop
    from ttp.daemon import Daemon
    from ttp.runner import proc_start
    p = make(env)
    _lane_env(monkeypatch)
    tid = p.db.add_task("t", "s", kind="code")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "ok", str(run_dir)))
    gone = subprocess.Popen(["sleep", "0.3"], start_new_session=True)
    started = proc_start(gone.pid)
    gone.wait(10)
    (run_dir / cli.CHECKS_PID).write_text(json.dumps({"pid": gone.pid, "started": started}))
    assert p.db.cancel(tid) == ""
    Daemon(p.base).stop_cancelled_checks()   # a checks.pid naming a process that ended
    assert jobstop.end_cancelled_checks(p, tid, "test") == []
    # a group found live that ends before its signal: no error, no signal, no wait past the grace
    monkeypatch.setattr(jobstop, "cancelled_checks",
                        lambda *a: [{"pgid": gone.pid, "pid": gone.pid, "lane": None, "rc": None, "since": 1.0}])
    began = time.monotonic()
    r = jobstop.end_cancelled_checks(p, tid, "test")
    assert time.monotonic() - began < jobstop.FREE_WAIT_S + 1
    assert len(r) == 1 and "already ended" in r[0]["text"], r


def test_checks_stall_threshold_scales_with_the_checks_median_never_under_15_min_and_reads_cpu_with_slack():
    from ttp import checkstall
    assert checkstall.stall_s({}, {}) == 900 == checkstall.STALL_MIN_S
    assert checkstall.stall_s({}, {"cost": {"est_s": 300}}) == 900, "a short check: the floor"
    assert checkstall.stall_s({}, {"cost": {"est_s": 1800}}) == 1800, "a long check: one usual run"
    assert checkstall.stall_s({"checks": {"stall_s": 60}}, {"cost": {"est_s": 1800}}) == 60, "checks.stall_s sets it"
    assert checkstall.stall_s({"checks": {"stall_s": 0}}, {}) == 0, "0 turns it off"
    assert checkstall.stall_s({"checks": {"stall_s": "x"}}, {}) == 900
    mark = {"cpu": 10.0, "at": 1000.0}
    assert checkstall.read_cpu(mark, 10.4, 1010.0) is mark, "under the slack: still idle since 1000"
    assert checkstall.read_cpu(mark, 18.0, 1900.0) is mark, "under 1% of one core over 900 s: idle"
    assert checkstall.read_cpu(mark, 20.0, 1900.0) == {"cpu": 20.0, "at": 1900.0}, "it used CPU: moved"
    assert checkstall.read_cpu(mark, 5.0, 1900.0) == {"cpu": 5.0, "at": 1900.0}, "a smaller reading: a new tree"
    assert checkstall.read_cpu(mark, None, 1900.0) == {"cpu": None, "at": 1900.0}, "unreadable: counts as moving"
    assert checkstall.read_cpu(None, 3.0, 50.0) == {"cpu": 3.0, "at": 50.0}


def test_daemon_marks_a_silent_checks_run_stalled_once_and_stops_it_only_when_its_processes_are_idle_too(
        env, tmp_path, monkeypatch, capsys):
    # Injected clock and CPU readings; the lane label comes from the real writers (suite_hold, suite_running).
    from ttp import checklane, checkstall, cli, jobstop, testslots
    from ttp.daemon import Daemon
    from ttp.runner import proc_start
    p = make(env)
    _lane_env(monkeypatch)
    run = tmp_path / "run"
    run.mkdir()
    log = run / "checks.log"
    log.write_text("$ pytest -q\n")
    me = os.getpid()
    (run / cli.CHECKS_PID).write_text(json.dumps({"pid": me, "started": proc_start(me)}))
    monkeypatch.setenv("TTP_TASK", "12")
    g = testslots.suite_hold(p.name, repo=_LANE_REPO, slots=1, priority=2, name="alpha")
    testslots.suite_running(g, 3600, str(log))
    monkeypatch.delenv("TTP_TASK")
    stops, cpu = [], {"v": 10.0}
    monkeypatch.setattr(jobstop, "end_checks", lambda p, rd, why, task: stops.append((rd, why, task)) or {"text": "stopped"})

    def tick(at, d=None):
        (d or daemon).check_checks_stall(every_s=0, now=at, cpu=lambda pid: cpu["v"])

    def events():
        return p.db.q("SELECT status, text FROM events WHERE kind=?", (checkstall.EVENT,))

    try:
        [rec] = checkstall.holders(p.name)
        assert rec["log"] == str(log) and rec["pid"] == me
        t0 = float(rec["run_since"])
        os.utime(log, (t0, t0))
        daemon = Daemon(p.base)
        tick(t0 + 300)
        with log.open("a") as f:
            f.write("tests/a.py ....\n")
        os.utime(log, (t0 + 1000, t0 + 1000))
        tick(t0 + 1800)   # CPU flat for 1500 s, but the log grew 800 s ago: slow, never touched
        assert not events() and not stops and "STALLED" not in checklane.holder_text(rec, t0 + 1800, p.config())

        cpu["v"] = 200.0   # its processes still work
        tick(t0 + 1960)
        cpu["v"] = 400.0
        tick(t0 + 2020)
        [ev] = events()
        assert ev["status"] == "handled" and "Checks run stalled" in ev["text"] and "#12" in ev["text"], ev
        assert "has not grown for 16 min (threshold 15 min, checks.stall_s), its processes still use CPU" in ev["text"]
        assert not stops, "a stalled log with a busy tree is left to the wall-time cap"
        now = t0 + 2020
        assert "STALLED: its log has not grown for 18 min" in checklane.holder_text(rec, now + 60, p.config())
        assert any("STALLED" in ln for ln in checklane.lane_lines(p.name, p.config(), now)), "the lane wait lines"
        assert "STALLED" in checklane.status_text(p.name, p.config(), now=now), "`ttp checks status`"
        assert cli._checks_result(run) == 1
        out = capsys.readouterr().out
        assert "running (pid" in out and "stalled: the log had not grown" in out and "left running" in out, out

        daemon = Daemon(p.base)   # a restart keeps each run's CPU clock (kv)
        assert checkstall.key(rec) in p.db.kv_dict(checkstall.KV)[0]
        tick(t0 + 2020 + 600)
        assert not stops, "idle 600 s of a 900 s threshold"
        (run / cli.CHECKS_PID).write_text(json.dumps({"pid": me + 1, "started": "x"}))
        tick(t0 + 2020 + 900)
        assert not stops, "checks.pid names another run: never stopped"
        (run / cli.CHECKS_PID).write_text(json.dumps({"pid": me, "started": proc_start(me)}))
        tick(t0 + 2020 + 960)
        [(rd, why, task)] = stops
        assert rd == run and task == 12 and why.startswith("stalled: its log had not grown"), why
        assert len(events()) == 1, "one event per stall, not one per tick"
        assert json.loads((run / checkstall.MARKER).read_text())["stopped"] is True
        (run / cli.CHECKS_RC).write_text("143\n")
        assert cli._checks_result(run) == 0
        out = capsys.readouterr().out
        assert "finished with exit code 143" in out and "reason stalled, retryable), never a pass" in out, out
        (run / cli.CHECKS_RC).write_text("0\n")
        cli._checks_result(run)
        assert "stalled" not in capsys.readouterr().out, "a pass that beat the stop stays a pass"
        (run / cli.CHECKS_RC).unlink()

        with log.open("a") as f:   # it moved again: a new episode, a new event
            f.write("tests/b.py ....\n")
        os.utime(log, (t0 + 5000, t0 + 5000))
        cpu["v"] = 900.0
        tick(t0 + 5950)
        assert len(events()) == 2 and len(stops) == 1

        p.set_config("checks.stall_s", 0)
        with log.open("a") as f:
            f.write("tests/c.py ....\n")
        tick(t0 + 99999, Daemon(p.base))
        assert len(events()) == 2 and len(stops) == 1, "checks.stall_s 0 turns it off"
    finally:
        g.close()


def test_check_commands_start_at_nice_19_and_idle_io_by_default_and_skip_a_missing_ionice(env, monkeypatch):
    """Every check command (and so its children) starts at the lowest CPU priority and idle IO; a host
    without ionice (macOS) runs it under nice alone, quietly."""
    import subprocess
    from ttp import testslots
    monkeypatch.setattr(testslots, "_cfg", lambda: None)
    assert testslots.NICE == 19 and testslots.IONICE == ["-c3"]
    argv = testslots.low_priority([sys.executable, "-c", "import os; print(os.nice(0))"])
    if shutil.which("nice"):
        assert argv[:3] == ["nice", "-n", "19"]
        if os.nice(0) <= 19:
            assert subprocess.run(argv, capture_output=True, text=True).stdout.strip() == "19"
    real = shutil.which
    monkeypatch.setattr(testslots.shutil, "which", lambda n, *a, **k: None if n == "ionice" else "/usr/bin/nice")
    assert testslots.low_priority(["pytest"]) == ["nice", "-n", "19", "pytest"]
    monkeypatch.setattr(testslots.shutil, "which", real)



def test_checks_nice_false_in_the_project_or_the_account_runs_check_commands_as_they_are(env, monkeypatch):
    """checks.nice false, the project's or the account's, turns the lowered priority off (priority_args None);
    a number or nothing keeps it on, at nice 19 and idle IO by default."""
    from ttp import project, testslots
    monkeypatch.setattr(testslots.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(testslots, "_cfg", lambda: {"checks": {"nice": False}})
    assert testslots.priority_args({"checks": {"nice": False}}) is None
    assert testslots.low_priority(["pytest"]) == ["pytest"], "the project's off switch"
    monkeypatch.setattr(testslots, "_cfg", lambda: {"checks": {"nice": 0}})
    assert testslots.low_priority(["pytest"]) == ["nice", "-n", "0", "ionice", "-c3", "pytest"], "0 is a level, not off"
    assert testslots.priority_args(None) == (["nice", "-n", "19"], ["ionice", "-c3"])
    project.set_account_setting("checks.nice", False)
    assert testslots.priority_args(None) is None and testslots.low_priority(["pytest"]) == ["pytest"], "the account's"


def test_a_pool_shard_runs_its_command_at_the_checks_low_priority(env, monkeypatch):
    """`ttp checks-shard` (a pool shard, here or on a host over ssh) starts its command behind
    testslots.low_priority, as every other check command."""
    import contextlib
    from ttp import checkcap, cli, testslots
    monkeypatch.setattr(testslots, "low_priority", lambda argv: ["nice", "-n", "19", *argv])
    monkeypatch.setattr(checkcap, "shard", lambda *a, **k: contextlib.nullcontext({}))
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    with pytest.raises(SystemExit) as e:
        cli.cmd_checks_shard(types.SimpleNamespace(cmd=["--", "pytest", "-q"]))
    assert e.value.code == 0 and ran == [["nice", "-n", "19", "pytest", "-q"]]


@pytest.mark.parametrize("cores,want", [(1, 2), (4, 2), (16, 2), (32, 4), (64, 8), (256, 8)])
def test_a_project_s_test_worker_ceiling_defaults_to_min_8_and_cores_over_8_at_least_2(env, monkeypatch, cores, want):
    from ttp import checkcap, project, testslots
    monkeypatch.delenv("TTP_TEST_PROJECT_WORKERS", raising=False)
    monkeypatch.setattr(checkcap, "cores", lambda: cores)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "0")   # no host-wide cap: no admitted-runs floor (admitted_workers)
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: float(cores))   # busy: no spare cores
    assert testslots.project_workers() == want
    project.set_account_setting("checks.test_project_workers", 12)
    assert testslots.project_workers() == 12, "the account sets it"
    assert testslots.project_workers({"checks": {"test_project_workers": 3}}) == 3, "a project's own lowers it"
    assert testslots.project_workers({"checks": {"test_project_workers": 30}}) == 12, "and never raises it"
    monkeypatch.setenv("TTP_TEST_PROJECT_WORKERS", "0")
    assert testslots.project_workers() == 0, "0: no ceiling"


def _spare_load(monkeypatch, cores: int, load1: float):
    from ttp import checkcap, testslots
    monkeypatch.delenv("TTP_TEST_PROJECT_WORKERS", raising=False)
    monkeypatch.delenv("TTP_CHECKS_LOAD_EXTRA", raising=False)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "0")   # no host-wide cap: no admitted-runs floor (admitted_workers)
    monkeypatch.setattr(checkcap, "cores", lambda: cores)
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: load1)


@pytest.mark.parametrize("load,want", [(0, 32), (16, 32), (30, 26), (32, 24), (47, 9), (48, 8), (60, 8)])
def test_a_project_s_test_worker_ceiling_grows_into_spare_cores_up_to_half_the_host(env, monkeypatch, load, want):
    """Unset, the ceiling is the base (min(8, cores // 8)) plus the cores the load leaves under 0.75 x the
    cores, at most half the cores; at or above 0.75 x the cores it is the base."""
    from ttp import testslots
    _spare_load(monkeypatch, 64, load)
    assert testslots.project_workers() == want
    assert 8 <= testslots.project_workers() <= 32


def test_a_project_s_test_worker_ceiling_reads_runnable_cpu_demand_not_the_load_average(env, monkeypatch):
    """The spare term reads the load guard's CPU demand (/proc/stat: busy cores and runnable tasks), so a
    load average of 67 on 64 cores made mostly of tasks waiting on I/O (D state) leaves the idle cores to
    the ceiling; real CPU demand still shrinks it."""
    from ttp import checkcap, hostcpu, testslots
    real = checkcap.cpu_demand
    _spare_load(monkeypatch, 64, 0.0)
    monkeypatch.setattr(checkcap, "cpu_demand", real)
    monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (67.0, 67.0, 67.0))
    monkeypatch.setattr(hostcpu, "demand", lambda: 20.0)
    assert testslots.spare_workers(64) == 28 and testslots.project_workers() == 32, "I/O waiters are not CPU load"
    monkeypatch.setattr(hostcpu, "demand", lambda: 40.0)
    assert testslots.project_workers() == 16, "CPU demand of 40 shrinks it"
    monkeypatch.setattr(hostcpu, "demand", lambda: None)
    assert testslots.project_workers() == 8, "no /proc/stat: the load average of 67 leaves no spare cores"


def test_load_extra_0_or_a_set_test_worker_ceiling_keeps_it_fixed(env, monkeypatch):
    from ttp import project, testslots
    _spare_load(monkeypatch, 64, 0.0)
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    assert testslots.project_workers() == 8, "checks.load_extra 0: today's fixed base"
    monkeypatch.delenv("TTP_CHECKS_LOAD_EXTRA")
    project.set_account_setting("checks.load_extra", 0)
    assert testslots.project_workers() == 8, "the account's checks.load_extra 0 too"
    project.set_account_setting("checks.load_extra", 2)
    assert testslots.project_workers() == 32
    project.set_account_setting("checks.test_project_workers", 12)
    assert testslots.project_workers() == 12, "the account's setting wins over the spare cores"
    monkeypatch.setenv("TTP_TEST_PROJECT_WORKERS", "6")
    assert testslots.project_workers() == 6


def test_a_project_s_own_test_worker_setting_still_only_lowers_the_grown_ceiling(env, monkeypatch):
    from ttp import testslots
    _spare_load(monkeypatch, 64, 0.0)
    assert testslots.project_workers({"checks": {"test_project_workers": 3}}) == 3
    assert testslots.project_workers({"checks": {"test_project_workers": 20}}) == 20
    assert testslots.project_workers({"checks": {"test_project_workers": 50}}) == 32, "never above the grown one"


def test_an_unreadable_load_gives_the_base_test_worker_ceiling(env, monkeypatch):
    from ttp import checkcap, testslots
    _spare_load(monkeypatch, 64, 0.0)
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: float("inf"))   # neither /proc/stat nor a load average
    assert testslots.project_workers() == 8
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: float("nan"))
    assert testslots.project_workers() == 8, "a NaN load never grows it"
    for cores, want in ((1, 2), (4, 2), (16, 2)):
        _spare_load(monkeypatch, cores, float("inf"))
        assert testslots.project_workers() == want, "never below WORKERS_FLOOR"


def test_a_project_s_own_held_suites_never_shrink_its_test_worker_ceiling(env, monkeypatch):
    """The load its own held test slots add (one worker each) is taken out before the spare term: with
    only its own suite loading the host its ceiling stays the idle one, so its next suite still gets the
    rest of it; another project, which holds none of those slots, sees the whole load."""
    from ttp import checkcap, testslots
    _slot_sizes(monkeypatch, 64, 28, TTP_TEST_PROJECT_SLOTS=64, TTP_CHECKS_LIVE_CORES=0)
    load = [0.0]
    _spare_load(monkeypatch, 64, 0.0)
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: load[0])
    assert testslots.project_workers(project_name="pa") == 32 and testslots.project_max(project_name="pa") == 32
    a = testslots.acquire("a", project_name="pa", wait_s=0)
    assert a and a.k == 28
    load[0] = 28.0   # a's own 28 workers only
    assert testslots.project_workers(project_name="pa") == 32, "its own load leaves its ceiling alone"
    assert testslots.project_max(project_name="pa") == 32
    assert testslots.project_workers() == 28 and testslots.project_workers(project_name="pb") == 28, \
        "with no project (or another one) the whole load counts"
    b = testslots.acquire("b", project_name="pa", wait_s=0)
    assert b and b.k == 4, "its own load never holds its next suite below the idle ceiling of 32"
    for g in (a, b):
        g.close()


@pytest.mark.parametrize("others,want", [(0, 32), (30, 26), (40, 16), (48, 8), (60, 8)])
def test_others_load_still_shrinks_a_project_s_test_worker_ceiling_beside_its_own_held_suites(
        env, monkeypatch, others, want):
    """Load beyond its own held slots' is others', and shrinks its ceiling as before (the host's pool
    and load guard still bound the total)."""
    from ttp import testslots
    _slot_sizes(monkeypatch, 64, 8, TTP_TEST_PROJECT_SLOTS=64, TTP_CHECKS_LIVE_CORES=0)
    _spare_load(monkeypatch, 64, 0.0)
    a = testslots.acquire("a", project_name="pa", wait_s=0)
    assert a and a.k == 8
    _spare_load(monkeypatch, 64, float(others + 8))
    assert testslots.project_workers(project_name="pa") == want
    a.close()


def test_grants_follow_the_grown_ceiling_and_holders_past_a_shrunk_one_still_count(env, monkeypatch):
    """project_max() and acquire() bound grants by the ceiling of the moment; once it shrinks, a run
    above it keeps its slots and they still count, so no new grant goes past the new ceiling even when
    the low budget slots themselves are free."""
    from ttp import checkcap, testslots
    _slot_sizes(monkeypatch, 64, 32, TTP_TEST_PROJECT_SLOTS=64, TTP_CHECKS_LIVE_CORES=0)
    load = [48.0]
    _spare_load(monkeypatch, 64, 0.0)
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: load[0])
    assert testslots.project_max() == 8
    a = testslots.acquire("a", project_name="pa", wait_s=0)
    assert a and a.k == 8, "busy host: the base"
    load[0] = 0.0
    assert testslots.project_max() == 32
    b = testslots.acquire("b", project_name="pa", wait_s=0)
    assert b and b.k == 24, "idle host: up to the grown ceiling of 32, a's 8 counted"
    a.close()
    load[0] = 72.0   # others' 48 plus b's own 24 (its own load never shrinks its ceiling)
    told = []
    assert testslots.acquire("c", told.append, project_name="pa", wait_s=0.2) is None, \
        "b's 24 past the shrunk ceiling of 8 still count, though slots 0-7 are free"
    assert any("its project holds its 8" in t for t in told), told
    assert not testslots.pool_room(None, "pa")
    o = testslots.acquire("o", project_name="pb", wait_s=0)
    assert o and o.k == 8, "another project has its own ceiling"
    b.close()
    load[0] = 56.0   # others' 48 plus o's 8
    c = testslots.acquire("c", project_name="pa", wait_s=0)
    assert c and c.k == 8, "once b ends, new grants get the shrunk ceiling"
    for g in (c, o):
        g.close()


def test_a_project_s_concurrent_suites_share_its_worker_ceiling_and_its_push_lane_stays_inside_one_grant(
        env, monkeypatch):
    """The ceiling bounds the project's workers over all its suites and one run's grant, through the
    environment (the check command's text is unchanged); other projects keep theirs. A push check's
    reserved lane makes up only the project's per-run grant, never the account's run_max."""
    from ttp import testslots
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    _slot_sizes(monkeypatch, 32, 8, TTP_TEST_PROJECT_WORKERS=4)
    assert testslots.project_max() == 4
    a = testslots.acquire("a", project_name="pa", wait_s=0)
    assert a and a.k == 4 and a.env()[testslots.XDIST_ENV] == "4" and a.env()[testslots.SLOTS_ENV] == "4"
    told = []
    assert testslots.acquire("a2", told.append, project_name="pa", wait_s=0.2) is None, "at the project's ceiling"
    assert any("checks.test_project_workers" in t for t in told), told
    b = testslots.acquire("b", project_name="pb", wait_s=0)
    assert b and b.k == 4, "another project has its own ceiling"
    push = testslots.acquire("push", project_name="pa", push_key="origin feature", wait_s=0)
    assert push and push.k == 4, "the reserved push lane makes up one per-run grant (4), not run_max (8)"
    own = testslots.worker_env(None)
    assert own[testslots.SLOTS_ENV] == "4", "a worker's own pytest asks at most the ceiling"
    assert own[testslots.XDIST_ENV] == "1", "a bare -n auto in a worker's shell takes no slots, so starts one worker"
    cmd = "python3 -m pytest -q -n auto tests"
    assert testslots.clamp_for(cmd, a.env()) == cmd, "the command text stays: -n auto reads the environment"
    for g in (a, b, push):
        g.close()


def test_a_run_that_gets_no_test_slots_within_its_wait_is_told_why_even_on_its_first_try(env, monkeypatch):
    """acquire() tells why before it gives up: with wait_s 0, or a first try slower than wait_s (a loaded
    host), the caller still learns what holds its slots, not None alone."""
    from ttp import project, testslots
    _slot_sizes(monkeypatch, 32, 8, TTP_TEST_PROJECT_WORKERS=4)
    a = testslots.acquire("a", project_name="pa", wait_s=0)
    assert a and a.k == 4
    try:
        told = []
        assert testslots.acquire("a2", told.append, project_name="pa", wait_s=0) is None
        assert any("checks.test_project_workers" in t for t in told), told
        cycle = project.ps_cycle
        monkeypatch.setattr(project, "ps_cycle", lambda: (time.sleep(0.3), cycle())[1])
        told = []
        assert testslots.acquire("a3", told.append, project_name="pa", wait_s=0.2) is None
        assert any("checks.test_project_workers" in t for t in told), told
        told = []
        b = testslots.acquire("b", told.append, project_name="pb", wait_s=0.2)
        assert b and b.k == 4 and not any(t.startswith("waiting") for t in told), told
        b.close()
    finally:
        a.close()


def test_checks_test_run_min_and_delivery_push_hard_tests_are_known_keys(env, monkeypatch):
    """Both are read (testslots.run_min, batch's hard-test rule): setting either gives no unknown-key
    hint, and checks.test_run_min also takes effect from the account settings."""
    from ttp import project, testslots
    monkeypatch.delenv("TTP_TEST_RUN_MIN", raising=False)
    p = make(env)
    for key, value in (("checks.test_run_min", 3), ("delivery.push_hard_tests", "leak|secret")):
        assert project.unknown_key_hint(key) is None, key
        p.set_config(key, value)
        assert not [x for x in project.config_problems(json.loads(p.config_path.read_text())) if key in x], key
    assert testslots.run_min(p.config(), 8) == 3
    p.set_config("checks.test_run_min", None)
    project.set_account_setting("checks.test_run_min", 5)
    assert testslots.run_min(p.config(), 8) == 5


def test_a_project_can_set_its_own_test_run_max(env):
    from ttp import project, testslots
    p = make(env)
    assert project.unknown_key_hint("checks.test_run_max") is None
    assert project.unknown_key_hint("checks.test_project_workers") is None
    p.set_config("checks.test_run_max", 2)
    assert testslots.sizes(p.config())[1] == 2


def test_no_suite_starts_while_the_host_is_overloaded_and_its_lane_says_why(env, monkeypatch):
    """Above checks.load_guard x the cores no routine run takes test slots, nor a priority-1 or push run
    while others hold every load pass it may take (#5992, #6556); it waits saying why (its lane's wait note too), running
    grants are left alone, and once the load drops it starts. A nested run keeps its parent's grant; 0 or
    an unreadable load never holds one."""
    import threading
    from ttp import checkcap, locks, project, testslots
    _slot_sizes(monkeypatch, 32, 4)
    monkeypatch.delenv("TTP_CHECKS_LOAD_GUARD", raising=False)
    load = [10.0]
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    monkeypatch.setattr(checkcap, "_load", lambda: load[0])
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: load[0])
    running = testslots.acquire("running", project_name="pa", wait_s=0)
    assert running and running.k == 4
    load[0] = 82.0
    told = []
    # Other runs past the guard hold every pass these may take (the shared, urgent and floor ones).
    passes = [locks.try_take([checkcap.slot_dir() / f"{n}.lock"], "a run past the guard", "load-guard pass")
              for n in (testslots.LOAD_PASS, testslots.LOAD_URGENT, testslots.LOAD_FLOOR)]
    for kw in ({}, {"priority": 1}, {"push_key": "origin feature"}):
        assert testslots.acquire("new", told.append, wait_s=0.2, project_name="pb", **kw) is None, kw
    assert told and all("overloaded" in t and "82.0" in t and "64 cores" in t for t in told), told
    assert locks._slot_labels([Path(running.files[0].name)]), "a running suite keeps its slots"
    monkeypatch.setenv(testslots.HELD_ENV, "3")
    assert testslots.acquire("nested", wait_s=0).k == 3, "a nested run uses its parent's grant at once"
    monkeypatch.delenv(testslots.HELD_ENV)
    lane = testslots.suite_hold("pb", repo=_LANE_REPO, slots=1, cap=1, wait_s=0, pending=True)
    notes, got = [], {}
    t = threading.Thread(target=lambda: got.setdefault("g", testslots.acquire(
        "waiter", project_name="pb", wait_s=20, waits=lambda w: (testslots.lane_waits(lane, w), notes.append(w)))))
    t.start()
    try:
        recs = []
        for _ in range(200):  # the lane label is rewritten (truncate, then write): poll until it reads whole
            recs = locks.holder_records([Path(f.name) for f in lane.files]) if notes else []
            if recs and "overloaded" in (recs[0].get("waits") or ""):
                break
            time.sleep(0.02)
        assert notes and notes[0].startswith("host load: ") and "overloaded" in recs[0]["waits"], (notes, recs)
        load[0] = 60.0
        t.join(20)
        assert got["g"] and got["g"].k == 4 and notes[-1] == "host test slots", notes
    finally:
        load[0] = 0.0
        t.join(20)
        for g in (running, got.get("g"), lane, *passes):
            if g is not None:
                g.close()
    load[0] = 100.0
    monkeypatch.setenv("TTP_CHECKS_LOAD_GUARD", "2")
    assert testslots.load_guard() == "", "100 is under 2 x 64"
    monkeypatch.setenv("TTP_CHECKS_LOAD_GUARD", "0")
    assert testslots.load_guard() == "", "0 turns it off"
    monkeypatch.setenv("TTP_CHECKS_LOAD_GUARD", "1")
    assert testslots.load_guard()
    load[0] = float("inf")
    assert testslots.load_guard() == "", "an unreadable load average holds no suite"
    load[0] = 100.0
    monkeypatch.delenv("TTP_CHECKS_LOAD_GUARD")
    project.set_account_setting("checks.load_guard", 2)
    assert testslots.load_guard() == "", "the account setting"


def _host_of_64(monkeypatch, load: float = 0.0):
    """A 64-core host with the default pool and grant (32, 8), a project ceiling of 8 test workers and a
    lane cap of 4 (checks.test_project_max), at a fixed 1-minute `load` (load guard on)."""
    from ttp import checkcap
    for k in ("TTP_TEST_POOL", "TTP_TEST_RUN_MAX", "TTP_TEST_RUN_MIN", "TTP_TEST_PROJECT_SLOTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.02")
    monkeypatch.setenv("TTP_TEST_PROJECT_WORKERS", "8")
    monkeypatch.setenv("TTP_TEST_PROJECT_MAX", "4")
    monkeypatch.setenv("TTP_CHECKS_LOAD_GUARD", "1")
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    monkeypatch.setattr(checkcap, "_load", lambda: load)
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: load)
    monkeypatch.setattr(checkcap, "live_cores_on", lambda: False)
    monkeypatch.setattr(checkcap, "extra_max", lambda: 0)


def test_a_second_full_run_of_a_project_starts_beside_the_first_within_its_worker_ceiling(env, monkeypatch):
    """#5992: with 64 cores, a lane cap of 4 and 8 project test workers, a run that starts while another
    of its project waits takes its share (max(run_min, 8 // 4) = 4), not all 8, so the waiting one starts
    beside it; a run alone still takes the whole ceiling."""
    from ttp import checkcap, locks, testslots
    _host_of_64(monkeypatch)
    assert testslots.sizes({}) == (32, 8) and testslots.project_max({}, project_name="pa") == 8
    alone = testslots.acquire("alone", project_name="pa", wait_s=0, full=True)
    assert alone and alone.k == 8, "a run alone takes the project's whole ceiling"
    alone.close()
    parked = locks.enqueue(checkcap.slot_dir(), checkcap.PARKED, "second", time.time(), group="pa")
    try:
        first = testslots.acquire("first", project_name="pa", wait_s=0, full=True)
        assert first and first.k == 4, "another run of its project waits: its share"
        assert testslots.pool_room({}, "pa", full=True), "the waiting run has room beside it"
        locks.dequeue(parked)
        second = testslots.acquire("second", project_name="pa", wait_s=0, full=True)
        assert second and second.k == 4, "the second full run starts beside the first"
        assert not testslots.pool_room({}, "pa", full=True), "the ceiling of 8 is full now"
        other = locks.enqueue(checkcap.slot_dir(), checkcap.PARKED, "other project", time.time(), group="pb")
        try:
            assert not testslots.project_waits("pa"), "another project's waiter is no waiter of this one"
        finally:
            locks.dequeue(other)
    finally:
        locks.dequeue(parked)
        for g in ("first", "second"):
            (locals().get(g) or testslots.Grant()).close()


@pytest.mark.parametrize("spare", [False, True])
def test_every_run_the_host_wide_cap_admits_gets_run_min_on_a_64_core_host_at_load_67_of_io_waiters(
        env, monkeypatch, spare):
    """#6913: 64 cores, a 1-minute load of 67 made mostly of tasks waiting on I/O, checks.host_max 4 and
    checks.test_run_min 4, no test-worker ceiling set. The project's ceiling was min(8, 64 // 8) plus the
    spare cores under the load average (none): 8, so only 2 of the 4 admitted runs could test. Now it is
    at least host_max x run_min (16) within the pool, its spare term reads the runnable CPU demand (20),
    and all 4 runs queued at once start with run_min or more; with checks.load_extra 0 (no spare term)
    too. A project's own lower value still lowers it, and the pool still bounds it."""
    from ttp import checkcap, hostcpu, locks, testslots
    _host_of_64(monkeypatch)
    monkeypatch.delenv("TTP_TEST_PROJECT_WORKERS")
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setenv("TTP_TEST_RUN_MIN", "4")
    monkeypatch.setattr(checkcap, "_load", lambda: 67.0)
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: hostcpu.demand() or checkcap._load())
    monkeypatch.setattr(hostcpu, "demand", lambda: 20.0)
    monkeypatch.setattr(checkcap, "extra_max", lambda: 2 if spare else 0)
    want = 32 if spare else 16
    assert testslots.project_workers({}, project_name="pa") == want
    assert testslots.project_max({}, project_name="pa") == want
    parked = [locks.enqueue(checkcap.slot_dir(), checkcap.PARKED, f"r{i}", time.time(), group="pa") for i in (1, 2, 3)]
    grants = []
    try:
        for i in range(4):
            g = testslots.acquire(f"r{i}", project_name="pa", wait_s=0, full=True)
            assert g and g.k >= 4, f"run {i + 1} of 4 starts with at least run_min"
            grants.append(g)
            if parked:
                locks.dequeue(parked.pop(0))
        assert sum(g.k for g in grants) == want
    finally:
        for t in parked:
            locks.dequeue(t)
        for g in grants:
            g.close()
    assert testslots.project_workers({"checks": {"test_project_workers": 6}}) == 6, "a project's own still lowers it"
    monkeypatch.setenv("TTP_TEST_POOL", "12")
    assert testslots.admitted_workers({}, 64) == 12 and testslots.project_max({}) == 12, "never above the pool"


def test_over_the_load_guard_a_routine_run_waits_and_one_push_or_urgent_run_starts(env, monkeypatch):
    """#5992: while the host's load is above checks.load_guard, a routine run takes no test slots, a
    push-queue run or an urgent one (priority 1) starts, one at a time (the load pass goes with its
    grant), and pool_gate/load_held agree with acquire. (The floor pass, taken by another run here, is
    the one routine run's: test_under_outside_load_an_idle_host_still_starts_one_check_at_a_time.)"""
    from ttp import checkcap, locks, testslots
    _host_of_64(monkeypatch, load=84.5)
    assert testslots.load_guard()
    floor = _hold_load_floor()
    told = []
    assert testslots.acquire("routine", told.append, wait_s=0.2, project_name="pa", full=True) is None
    assert told and "overloaded" in told[-1]
    assert testslots.pool_gate({}, "pa", full=True).startswith("host load: ")
    assert not testslots.pool_gate({}, "pa", full=True, priority=1), "an urgent run passes the guard"
    push = testslots.acquire("push queue", told.append, wait_s=0.2, project_name="pa", push_key="origin feature")
    held = locks.try_take([checkcap.slot_dir() / f"{testslots.LOAD_URGENT}.lock"], "another urgent run")
    try:
        assert push and push.k == 8 and any(f.name == str(testslots.load_pass_path()) for f in push.files)
        assert testslots.acquire("urgent", wait_s=0.2, project_name="pb", priority=1, full=True) is None, \
            "one per pass: its urgent pass and the shared one are held"
        assert testslots.pool_gate({}, "pb", full=True, priority=1).startswith("host load: ")
        assert testslots.load_held(push=True), "a second push run waits too"
    finally:
        push and push.close()
        held.close()
    urgent = testslots.acquire("urgent", wait_s=0.2, project_name="pb", priority=1, full=True)
    try:
        assert urgent and urgent.k == 8, "the pass freed with the push run's grant: the urgent run starts"
    finally:
        urgent and urgent.close()
    assert not testslots.load_held(1), "every grant let the pass go"
    floor.close()


def test_two_urgent_runs_of_different_projects_both_start_past_the_load_guard_and_a_routine_one_waits(
        env, monkeypatch):
    """#6556: over the load guard two urgent runs (priority 1) of different projects never serialize behind
    one load pass: the first takes the urgent pass, the second the shared one, while a routine run holds
    the floor. The count stays bounded: a third urgent run, a push run and a routine run wait."""
    from ttp import testslots
    _host_of_64(monkeypatch, load=84.5)
    floor = testslots.acquire("routine", wait_s=0.2, project_name="pa", priority=3, full=True)
    first = second = None
    try:
        assert floor and Path(floor.files[-1].name).stem == testslots.LOAD_FLOOR
        told = []
        first = testslots.acquire("urgent a", told.append, wait_s=0.2, project_name="pb", priority=1, full=True)
        second = testslots.acquire("urgent b", wait_s=0.2, project_name="pc", priority=1, full=True)
        assert first and first.k == 8 and "pass for urgent runs" in " ".join(told), told
        assert second and second.k == 8, "the second urgent run takes the shared load pass"
        assert {Path(g.files[-1].name).stem for g in (first, second)} == {testslots.LOAD_URGENT, testslots.LOAD_PASS}
        assert testslots.acquire("urgent c", wait_s=0.2, project_name="pd", priority=1, full=True) is None, \
            "bounded: no third urgent pass"
        assert testslots.acquire("push", wait_s=0.2, project_name="pd", push_key="origin feature") is None
        assert testslots.acquire("routine 2", wait_s=0.2, project_name="pd", priority=3, full=True) is None, \
            "a routine run still waits for the one floor pass"
        assert testslots.pool_gate({}, "pd", full=True, priority=3).startswith(testslots.LOAD_GATE)
    finally:
        for g in (floor, first, second):
            g and g.close()
    assert not testslots.load_held(1) and not testslots.load_held(3), "every grant let its pass go"


def test_a_run_owed_to_the_user_passes_the_load_guard_while_less_urgent_runs_hold_its_passes(env, monkeypatch):
    """#6490 (2): over the load guard a priority-0 run (owed to the user) has a pass of its own, so a
    priority-1 runs holding the urgent and shared load passes (#6556) and a routine run on the floor pass
    never hold it; only another priority-0 run does. pool_gate agrees with acquire."""
    from ttp import testslots
    _host_of_64(monkeypatch, load=84.5)
    urgent = testslots.acquire("urgent p1", wait_s=0.2, project_name="pa", priority=1, full=True)
    urgent2 = testslots.acquire("urgent p1 b", wait_s=0.2, project_name="pe", priority=1, full=True)
    floor = testslots.acquire("routine", wait_s=0.2, project_name="pb", priority=3, full=True)
    owed = None
    try:
        assert urgent and urgent2 and floor, "the urgent runs take the urgent and load passes, the routine one the floor"
        assert testslots.pool_gate({}, "pc", full=True, priority=1).startswith(testslots.LOAD_GATE)
        assert testslots.pool_gate({}, "pc", full=True, priority=2).startswith(testslots.LOAD_GATE)
        assert not testslots.pool_gate({}, "pc", full=True, priority=0), "a run owed to the user passes"
        told = []
        owed = testslots.acquire("owed p0", told.append, wait_s=0.2, project_name="pc", priority=0, full=True)
        assert owed and owed.k == 8 and "owed to the user" in " ".join(told), told
        assert testslots.acquire("second p0", wait_s=0.2, project_name="pd", priority=0, full=True) is None, \
            "one priority-0 run at a time on its own pass"
    finally:
        for g in (urgent, urgent2, floor, owed):
            g and g.close()


def test_under_outside_load_an_idle_host_still_starts_one_check_at_a_time(env, monkeypatch):
    """#6490 (3): a host whose load comes from other work (no check runs) still starts one check, a
    routine one included, on the floor pass at once; the next waits until it ends, then starts."""
    from ttp import testslots
    _host_of_64(monkeypatch, load=84.5)
    assert not testslots.pool_gate({}, "pa", full=True, priority=3), "nothing runs: the floor is free"
    told = []
    first = testslots.acquire("routine 1", told.append, wait_s=0.2, project_name="pa", priority=3, full=True)
    try:
        assert first and first.k == 8 and "floor pass" in " ".join(told), told
        assert testslots.pool_gate({}, "pb", full=True, priority=3).startswith(testslots.LOAD_GATE)
        assert testslots.acquire("routine 2", wait_s=0.2, project_name="pb", priority=3, full=True) is None
    finally:
        first and first.close()
    second = testslots.acquire("routine 2", wait_s=0.2, project_name="pb", priority=3, full=True)
    assert second, "the floor freed with the first run's grant"
    second.close()


def test_a_parked_run_the_load_guard_does_not_hold_keeps_no_place_for_the_floor_pass(env, monkeypatch):
    """#6490 (1): the floor pass goes to the most urgent, oldest run parked under the load guard; a run
    parked ahead for another reason (its lane's gate names its project's budget) holds no place, so it
    never blocks the runs behind it. One the load guard holds goes first."""
    from ttp import checkcap, locks, testslots
    _host_of_64(monkeypatch, load=84.5)
    d, now = checkcap.slot_dir(), time.time()
    ahead = locks.enqueue(d, checkcap.PARKED, "older p2", now - 60, group="pa", priority=2)
    lane = locks.try_take([d / f"{testslots.SUITE}.pa.repo.0.lock"], "older p2", "ttp checks",
                          extra={"gate": "its project's test-worker budget: 0 of 8 free, a run needs 4"})
    try:
        assert testslots.floor_ahead(3, now) is False
        assert not testslots.pool_gate({}, "pb", full=True, priority=3, since=now), "not held by the run ahead"
        locks.relabel(lane, {"gate": testslots.LOAD_GATE + "the host is overloaded"})
        assert testslots.floor_ahead(3, now) is True
        assert testslots.pool_gate({}, "pb", full=True, priority=3, since=now).startswith(testslots.LOAD_GATE)
        assert testslots.floor_ahead(1, now) is False, "a more urgent run goes first"
        assert testslots.floor_ahead(2, now - 120) is False, "as urgent and older goes first"
    finally:
        lane.close()
        locks.dequeue(ahead)


def test_slot_status_names_the_gate_that_holds_a_parked_run(env, tmp_path, monkeypatch):
    """#5992: a run parked for its test slots (cli._checks_slot_take) records the gate pool_gate found on
    its lane, and status prints it (checklane._parked): the project's budget with its holder, or the
    host's load; once it starts the gate is cleared."""
    import io
    import threading
    from ttp import checkcap, checklane, cli, locks, testslots
    p = make(env)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.delenv("TTP_DETACHED", raising=False)
    monkeypatch.delenv(testslots.HELD_ENV, raising=False)
    monkeypatch.delenv(checkcap.HELD_ENV, raising=False)
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 30.0)
    _budget_of_one(monkeypatch)
    full = testslots.acquire("the budget holder", project_name=p.name)
    got = []
    t = threading.Thread(target=lambda: got.append(
        cli._checks_slot(p, run_dir, io.StringIO(), "f" * 40, False, cmds=["true"], top=str(p.base))))

    def waits_on():
        return [h.get("waits_on") for lane in checklane.lane_status(p.name) for h in lane["lanes"]]
    t.start()
    try:
        assert _wait_for(lambda: any("held by its project's test-worker budget: 0 of 1 free" in str(w)
                                     for w in waits_on())), waits_on()
        assert "the budget holder" in str(waits_on()), "the gate names the holder"
        assert "test-worker budget: 0 of 1 free" in " ".join(checklane.lane_lines(p.name))
        full.close()
        t.join(30)
        assert got and got[0] is not None and got[0].f is not None
        recs = [r for b in testslots.suite_bases(p.name) for r in locks.holder_records(testslots._lanes_of(b))]
        assert recs and all(not r.get("gate") for r in recs), "started: no gate left on its lane"
    finally:
        full.close()
        t.join(30)
        got and got[0] and got[0].close()
    rec = {"waits": testslots.PARKED_WAITS, "gate": "host load: the host is overloaded"}
    assert checklane._parked(rec).endswith("; held by host load: the host is overloaded")


def _budget_of_one(monkeypatch):
    """A test pool of 4 where each project's budget is one run's single slot (testslots.project_max)."""
    monkeypatch.setenv("TTP_TEST_POOL", "4")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "1")
    monkeypatch.setenv("TTP_TEST_PROJECT_SLOTS", "1")
    monkeypatch.setenv("TTP_TEST_RUN_MIN", "1")


def test_a_run_whose_projects_test_budget_is_full_takes_no_host_slot_and_keeps_its_arrival(env, monkeypatch):
    """#5452: a run of project A whose test-worker budget is full waits outside the host queue
    (testslots.pool_room), so project B's later waiter takes the free host slot; once A's budget frees,
    A's run queues again at its own arrival and goes ahead of a later waiter of project C."""
    import threading
    from ttp import checkcap, locks, testslots
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    _budget_of_one(monkeypatch)
    a1_host, a1_tests = checkcap.acquire("a1"), testslots.acquire("a1", project_name="A")
    assert a1_tests.k == 1 and not testslots.pool_room({}, "A") and testslots.pool_room({}, "B")
    told, got, flips = [], {}, []

    def wait(name, project, since):
        got[name] = checkcap.acquire(name, tell=told.append, since=since, project_name=project, max_wait_s=30,
                                     startable=lambda: testslots.pool_room({}, project),
                                     parks=lambda on: flips.append((name, on)))
    a2 = threading.Thread(target=wait, args=("a2", "A", 1000.0))
    a2.start()
    held, threads = [], [a2]
    try:
        assert _wait_for(lambda: checkcap.PARKED_LINE in told)
        assert checkcap.state()["queued"] == [] and checkcap.state()["running"] == ["a1"], "A's run holds nothing"
        b = threading.Thread(target=wait, args=("b", "B", 2000.0))
        threads.append(b)
        b.start()
        b.join(30)
        assert got.get("b") and got["b"].f is not None, "the other project's later waiter takes the host slot"
        held.append(got["b"])
        c = threading.Thread(target=wait, args=("c", "C", 3000.0))
        threads.append(c)
        c.start()
        assert _wait_for(lambda: checkcap.state()["queued"] == ["c"]), "a waiter with room queues as before"
        a1_tests.close()   # A's budget frees while every host slot is held: A's run queues again
        assert _wait_for(lambda: len(locks.queued(checkcap.slot_dir(), checkcap.RESOURCE)) == 2)
        got["b"].close()
        a2.join(30)
        assert got.get("a2") and got["a2"].f is not None, "A's run goes first: it kept its arrival"
        assert flips == [("a2", True), ("a2", False)], "#5463: told once it parks and once it queues again"
        held.append(got["a2"])
        assert "c" not in got and checkcap.state()["queued"] == ["c"]
        a1_host.close()
        c.join(30)
        held.append(got.get("c"))
        assert got["c"].f is not None
    finally:
        a1_tests.close()
        a1_host.close()
        for th in threads:
            th.join(30)
        for s in held + list(got.values()):
            s and s.close()


def test_a_checks_run_parked_for_its_test_budget_reads_so_in_status_until_it_starts(env, tmp_path, monkeypatch):
    """#5463: while `ttp checks` (cli._checks_slot_take) waits outside the host queue for its project's
    test-worker budget, its lane reads testslots.PARKED_WAITS through the real writer (checkcap.acquire's
    `parks`), never 'a host-wide checks slot' or 'no host queue'; once it starts the label is cleared."""
    import io
    import threading
    from ttp import checkcap, checklane, cli, locks, testslots
    p = make(env)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.delenv("TTP_DETACHED", raising=False)
    monkeypatch.delenv(testslots.HELD_ENV, raising=False)
    monkeypatch.delenv(checkcap.HELD_ENV, raising=False)
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 30.0)
    _budget_of_one(monkeypatch)
    full = testslots.acquire("running", project_name=p.name)
    got = []
    t = threading.Thread(target=lambda: got.append(
        cli._checks_slot(p, run_dir, io.StringIO(), "f" * 40, False, cmds=["true"], top=str(p.base))))

    def holders():
        return [h for lane in checklane.lane_status(p.name) for h in lane["lanes"]]
    t.start()
    try:
        assert _wait_for(lambda: [str(h.get("waits_on")).split("; held by")[0] for h in holders()]
                         == [testslots.PARKED_WAITS + ", outside the host queue"])
        assert checkcap.state()["running"] == [] and checkcap.state()["queued"] == []
        line = " ".join(checklane.lane_lines(p.name))
        assert f"not running yet: waits on {testslots.PARKED_WAITS}, outside the host queue" in line
        assert "a host-wide checks slot" not in line
        assert "no host queue" not in line
        full.close()
        t.join(30)
        assert got and got[0] is not None and got[0].f is not None, "it starts once its budget frees"
        recs = [r for b in testslots.suite_bases(p.name) for r in locks.holder_records(testslots._lanes_of(b))]
        assert recs and all(r.get("granted") and not r.get("waits") for r in recs), "started: its lane waits for nothing"
        assert not any(testslots.PARKED_WAITS in str(h.get("waits_on")) for h in holders())
    finally:
        full.close()
        t.join(30)
        got and got[0] and got[0].close()


def test_slot_status_lists_a_run_parked_for_its_test_slots_as_a_waiter_never_a_holder(env, tmp_path, monkeypatch):
    """[note 0781a4c0bc1d23dc]: `ttp checks --slot-status` (checklane.status_text) lists a run parked outside
    the host queue (labelled through the real writer, cli._checks_slot) under the waiters, never on a
    'holder:' line, and never says 'waiters: none' while it is parked."""
    import io
    import threading
    from ttp import checkcap, checklane, cli, testslots
    p = make(env)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.delenv("TTP_DETACHED", raising=False)
    monkeypatch.delenv(testslots.HELD_ENV, raising=False)
    monkeypatch.delenv(checkcap.HELD_ENV, raising=False)
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 30.0)
    _budget_of_one(monkeypatch)
    full = testslots.acquire("running", project_name=p.name)
    got = []
    t = threading.Thread(target=lambda: got.append(
        cli._checks_slot(p, run_dir, io.StringIO(), "f" * 40, False, cmds=["true"], top=str(p.base))))
    t.start()
    try:
        assert _wait_for(lambda: any(h.get("parked") for lane in checklane.lane_status(p.name) for h in lane["lanes"]))
        text = checklane.status_text(p.name, p.config())
        assert "waiter (parked): " in text and f"waits on {testslots.PARKED_WAITS}, outside the host queue" in text
        holders = [ln for ln in text.splitlines() if ln.strip().startswith("holder")]
        assert holders == ["    holder: none (the parked waiters below keep their lanes while they wait)"], text
        assert "waiters: none" not in text
        full.close()
        t.join(30)
        assert got and got[0] is not None and got[0].f is not None
        assert "waiter (parked)" not in checklane.status_text(p.name, p.config())
    finally:
        full.close()
        t.join(30)
        got and got[0] and got[0].close()


def test_checks_and_push_checks_wait_for_their_projects_test_budget_without_a_host_slot(env, tmp_path, monkeypatch):
    """#5452: `ttp checks` (cli._checks_slot) and push checks (push.check_slot) of a project whose
    test-worker budget is full take no host-wide checks slot while they wait: no grant is logged and
    the foreground wait hands over with nothing held; push checks take their slot once the budget frees."""
    import io
    import threading
    from ttp import checkcap, cli, push, testslots
    p = make(env)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.delenv("TTP_DETACHED", raising=False)
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 1.0)
    _budget_of_one(monkeypatch)
    full = testslots.acquire("running", project_name=p.name)
    before = len(checkcap.grant_records())
    out = io.StringIO()
    try:
        assert cli._checks_slot(p, run_dir, out, "f" * 40, False, cmds=["true"], top=str(p.base)) is None
        assert checkcap.PARKED_LINE in out.getvalue() and checkcap.state()["running"] == []
        assert len(checkcap.grant_records()) == before, "no host slot was granted and handed back"
        told, got = [], []
        t = threading.Thread(target=lambda: got.append(push.check_slot("abc", told.append, proj=p)))
        t.start()
        assert _wait_for(lambda: checkcap.PARKED_LINE in told) and checkcap.state()["running"] == []
        full.close()
        t.join(30)
        assert got and got[0].f is not None and got[0].tests.k == 1
        got[0].close()
    finally:
        full.close()


def _spill_rounds(env, tmp_path, monkeypatch, rounds: int = 2):
    """cli._checks_slot of a detached, spillable full run whose project spills after 0.3 s, with
    checkspill.pick finding no host for `rounds` bounded waits and then a claim: the pick count and the
    host-wide checks slots granted meanwhile (checkcap.grant_records)."""
    import io
    from ttp import checkcap, checkspill, cli
    p = make(env)
    p.set_config("delivery.checks_spill_hosts", ["check-host-a"])
    p.set_config("delivery.checks_spill_after_s", 0.3)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    picks = []

    def pick(*_a, **_k):
        picks.append(time.time())
        return None if len(picks) < rounds + 1 else {"host": "check-host-a"}
    monkeypatch.setattr(checkspill, "pick", pick)
    before = len(checkcap.grant_records())
    slot = cli._checks_slot(p, run_dir, io.StringIO(), "f" * 40, True, cmds=["true"], top=str(p.base), spill=True)
    assert slot is not None and slot.spill == {"host": "check-host-a"} and slot.f is None
    return len(picks), len(checkcap.grant_records()) - before


def _hold_load_floor():
    """Take the load guard's floor pass (#6490) as another run, so a routine run waits under the guard."""
    from ttp import checkcap, locks, testslots
    return locks.try_take([checkcap.slot_dir() / f"{testslots.LOAD_FLOOR}.lock"], "the floor run")


def test_a_spill_bounded_checks_wait_under_the_load_guard_takes_no_host_slot(env, tmp_path, monkeypatch):
    """#5460: while the host's load is above checks.load_guard no suite may start (testslots.load_guard),
    so a run whose wait is bounded by checkspill.after_s takes no host-wide checks slot only to hand it
    back at each bound: its spill rounds log no grant (once each round re-granted it)."""
    from ttp import checkcap, testslots
    monkeypatch.delenv("TTP_CHECKS_LOAD_GUARD", raising=False)
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    monkeypatch.setattr(checkcap, "_load", lambda: 900.0)
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: 900.0)
    monkeypatch.setenv("TTP_TEST_POOL", "4")
    floor = _hold_load_floor()   # another run holds the one routine pass
    try:
        assert testslots.load_guard() and testslots.pool_room({}, "px") and not testslots.pool_room({}, "px", full=True)
        picks, grants = _spill_rounds(env, tmp_path, monkeypatch)
    finally:
        floor.close()
    assert picks == 3 and grants == 0, (picks, grants)


def test_a_spill_bounded_checks_wait_with_the_floor_pass_free_starts_on_it_with_its_test_slots(
        env, tmp_path, monkeypatch):
    """#5460 with the floor pass free (review #6596): the run takes the floor at its gate and its one
    host-wide checks slot comes with its test slots, never a slot handed back at a spill bound."""
    import io
    from ttp import checkcap, checkspill, cli, testslots
    monkeypatch.delenv("TTP_CHECKS_LOAD_GUARD", raising=False)
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    monkeypatch.setattr(checkcap, "_load", lambda: 900.0)
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: 900.0)
    monkeypatch.setenv("TTP_TEST_POOL", "4")
    assert testslots.load_guard() and testslots.pool_room({}, "px", full=True), "the floor is free"
    p = make(env)
    p.set_config("delivery.checks_spill_hosts", ["check-host-a"])
    p.set_config("delivery.checks_spill_after_s", 0.3)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.setattr(checkspill, "pick", lambda *_a, **_k: None)
    before = len(checkcap.grant_records())
    slot = cli._checks_slot(p, run_dir, io.StringIO(), "f" * 40, True, cmds=["true"], top=str(p.base), spill=True)
    try:
        assert slot is not None and slot.f is not None and slot.tests is not None and slot.tests.k
        assert any(Path(f.name).stem == testslots.LOAD_FLOOR for f in slot.tests.files), "it holds the floor"
        assert len(checkcap.grant_records()) - before == 1 and testslots.reserved_pass() is None
        assert testslots.pool_gate({}, "py", full=True, priority=3).startswith(testslots.LOAD_GATE)
    finally:
        slot and slot.close()


def test_parked_routine_runs_leave_the_park_one_per_freed_floor_pass(env, monkeypatch):
    """Review #6596: several routine runs parked under the load guard while another holds the floor. Once
    it frees, the first whose gate passes takes the floor at once (testslots.full_startable), so the
    others stay parked even with a gap before its test-slot wait (cli's lane_waits, ps_cycle, turn wait):
    no run holds a host-wide checks slot without test slots."""
    import threading
    from ttp import checkcap, locks, testslots
    _host_of_64(monkeypatch, load=84.5)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setattr(checkcap, "POLL_S", 0.05)
    d, now = checkcap.slot_dir(), time.time()
    floor = _hold_load_floor()
    res, threads, done = {}, [], threading.Event()

    class Lanes:
        def __init__(self, f):
            self.files = [f]

    def run(i):
        proj, arrived = f"p{i}", now - 100 + i
        lane = Lanes(locks.try_take([d / f"{testslots.SUITE}.{proj}.repo.0.lock"], f"run {i}", "ttp checks"))
        slot = g = None
        try:
            slot = checkcap.acquire(f"run {i}", "x", max_wait_s=3, since=arrived, project_name=proj, priority=2,
                                    startable=lambda: testslots.full_startable(lane, {}, proj, f"run {i}",
                                                                               priority=2, since=arrived))
            time.sleep(0.3)   # the gap between the checks slot and the test-slot wait
            if slot is not None:
                g = testslots.acquire(f"run {i}", wait_s=1.0, project_name=proj, priority=2, full=True,
                                      since=arrived)
            res[i] = (slot is not None, g is not None)
            done.wait(6)
        finally:
            testslots.release_load_pass()
            g and g.close()
            slot and slot.close()
            lane.files[0].close()

    for i in range(3):
        threads.append(threading.Thread(target=run, args=(i,)))
        threads[-1].start()
    time.sleep(0.8)
    floor.close()   # the floor frees
    for _ in range(200):
        if len(res) == 3:
            break
        time.sleep(0.05)
    done.set()
    for t in threads:
        t.join(15)
    assert len(res) == 3, res
    assert not [i for i, (s, g) in res.items() if s and not g], f"a host slot held without test slots: {res}"
    assert sum(g for _s, g in res.values()) == 1, f"one run leaves the park on the floor pass: {res}"


def test_a_spill_bounded_full_run_waiting_for_run_min_takes_no_host_slot(env, tmp_path, monkeypatch):
    """#5460: a full run starts only with checks.test_run_min slots free; with fewer free (pool_room's one
    free slot is not enough) a run bounded by checkspill.after_s waits outside the host queue, so its spill
    rounds log no grant; once enough are free it may start (pool_room full=True)."""
    from ttp import testslots
    monkeypatch.setenv("TTP_TEST_POOL", "4")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "1")   # two other runs of one slot each: two of four free
    others = [testslots.acquire(f"other {i}", project_name="px", wait_s=0) for i in (1, 2)]
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "4")
    monkeypatch.setenv("TTP_TEST_RUN_MIN", "3")
    try:
        assert [g.k for g in others] == [1, 1]
        assert testslots.pool_room({}, "py") and not testslots.pool_room({}, "py", full=True)
        picks, grants = _spill_rounds(env, tmp_path, monkeypatch)
        assert picks == 3 and grants == 0, (picks, grants)
    finally:
        others[0].close()
    assert testslots.pool_room({}, "py", full=True), "with run_min free it may start"
    others[1].close()


def test_a_full_runs_room_counts_a_priority_1_runs_urgent_slots_as_acquire_takes_them(env, monkeypatch):
    """#5460: pool_room(full=True) asks what acquire would start: a priority-1 run whose project's budget
    is held only by less urgent runs has room in its urgent slots (urgent_paths); a less urgent run, or
    one behind a priority-1 holder, does not."""
    from ttp import testslots
    _budget_of_one(monkeypatch)
    held = testslots.acquire("routine", project_name="A", priority=3)
    try:
        assert held.k == 1 and not testslots.pool_room({}, "A", full=True)
        assert not testslots.pool_room({}, "A", full=True, priority=3)
        assert testslots.pool_room({}, "A", full=True, priority=1), "its urgent slots are free"
        urgent = testslots.acquire("urgent", project_name="A", priority=1, wait_s=0, full=True)
        assert urgent and urgent.k == 1, "acquire agrees"
        urgent.close()
    finally:
        held.close()
    held = testslots.acquire("urgent holder", project_name="A", priority=1)
    try:
        assert not testslots.pool_room({}, "A", full=True, priority=1), "no urgent slots past a priority-1 holder"
    finally:
        held.close()


def test_an_owed_priority_0_run_takes_the_urgent_grant_past_its_projects_budget_held_by_priority_1_runs(
        env, monkeypatch):
    """An owed run (priority 0) whose project's budget is held only by priority-1 runs takes the urgent
    grant, as a priority-1 run does past routine holders (an owed run waited 19 min behind two p1 suites
    while a p1 pre-check got the urgent slot). A priority-1 run behind priority-1 holders still waits, and
    nothing passes a priority-0 holder."""
    from ttp import testslots
    _budget_of_one(monkeypatch)
    held = testslots.acquire("p1 suite", project_name="A", priority=1)
    try:
        assert not testslots.pool_room({}, "A", full=True, priority=1), "as urgent as its holder: it waits"
        assert testslots.pool_room({}, "A", full=True, priority=0), "more urgent than every holder"
        owed = testslots.acquire("owed", project_name="A", priority=0, wait_s=0, full=True)
        assert owed and owed.k == 1, "acquire agrees"
        owed.close()
    finally:
        held.close()
    held = testslots.acquire("owed holder", project_name="A", priority=0)
    try:
        assert not testslots.pool_room({}, "A", full=True, priority=0), "no urgent slots past a priority-0 holder"
    finally:
        held.close()


def test_a_run_waiting_for_its_test_slots_keeps_its_project_s_next_run_off_the_host_checks_slots(env, monkeypatch):
    """A run that holds a host-wide checks slot and still waits for its test slots (its lane labelled by
    the real writers: lane_waits(SLOT_WAITS), acquire's load-guard label; or its ticket in its project's
    line) goes first: pool_room(full=True) gives no other run of its project a checks slot only to queue
    behind it (one project's waiters filled every host-wide slot). Another project, a more urgent run
    and a non-full ask still have room; a lane still waiting for its checks slot, or running, blocks none."""
    from ttp import checkcap, checklane, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setenv("TTP_TEST_POOL", "4")
    monkeypatch.setenv("TTP_TEST_RUN_MIN", "1")
    monkeypatch.setattr(testslots, "load_guard", lambda: "")
    monkeypatch.setenv("TTP_TASK", "12")
    lane = testslots.suite_hold("pa", repo=_LANE_REPO, slots=4, wait_s=0, pending=True, running=True, priority=2)
    monkeypatch.delenv("TTP_TASK")
    line = None
    try:
        assert lane and lane.files and testslots.pool_room({}, "pa", full=True, priority=2)
        testslots.lane_waits(lane, testslots.SLOT_WAITS)   # cli's label once it holds its checks slot
        assert not testslots.pool_room({}, "pa", full=True, priority=2)
        assert not testslots.pool_room({}, "pa", full=True), "a run without a priority ranks last"
        assert testslots.pool_room({}, "pa", full=True, priority=1), "a more urgent run is not behind it"
        assert testslots.pool_room({}, "pa") and testslots.pool_room({}, "pb", full=True)
        testslots.lane_waits(lane, "a host-wide checks slot")
        assert testslots.pool_room({}, "pa", full=True, priority=2), "still waiting for its own checks slot"
        monkeypatch.setattr(testslots, "load_guard", lambda: "load 99 over 64 cores")
        floor = _hold_load_floor()   # another run holds the floor pass, so this routine run waits
        try:
            assert floor and testslots.acquire("run 12", project_name="pa", wait_s=0.3, priority=2,
                                               waits=lambda w: testslots.lane_waits(lane, w)) is None
        finally:
            floor and floor.close()
        monkeypatch.setattr(testslots, "load_guard", lambda: "")
        rec = locks.holder_records([Path(f.name) for f in lane.files])[0]
        assert str(rec.get("waits")).startswith(testslots.LOAD_WAITS), rec
        assert not testslots.pool_room({}, "pa", full=True, priority=2), "the load just dropped: it goes first"
        testslots.lane_waits(lane, "a host-wide checks slot")
        line = locks.enqueue(checkcap.slot_dir(), f"{testslots.TESTS.own}.{locks.group_id('pa')}", "run 12",
                             priority=2)   # acquire's ticket in its project's line
        assert not testslots.pool_room({}, "pa", full=True, priority=2)
        assert testslots.pool_room({}, "pa", full=True, priority=1)
        on = checklane.lane_status("pa", time.time(), {})[0]["lanes"][0]["waits_on"]
        assert on.startswith("its project's test-worker budget (checks.test_project_slots)"), on
        locks.dequeue(line)
        line = None
        testslots.lane_waits(lane, testslots.SLOT_WAITS)
        testslots.slots_granted(lane)
        assert testslots.pool_room({}, "pa", full=True, priority=2), "a running suite counts only by its slots"
    finally:
        line and locks.dequeue(line)
        lane.close()


def test_a_lane_whose_run_waits_for_test_slots_or_the_load_guard_holds_no_place_under_the_cap(env, monkeypatch):
    """Lanes whose runs still wait (for test slots under their project's budget, parked, or under the load
    guard) count against neither the project's cap nor its repository's running lanes: a third run
    starts under a cap of 1 and then holds it."""
    from ttp import testslots
    _lane_env(monkeypatch)
    waiting = [testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, cap=1, wait_s=0, pending=True, running=True)
               for _ in range(3)]
    try:
        assert all(g and g.files for g in waiting)
        for g, what in zip(waiting, (testslots.SLOT_WAITS, testslots.PARKED_WAITS, testslots.LOAD_WAITS + "load 99")):
            testslots.lane_waits(g, what)
        assert testslots.project_held("pa") == []
        third = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, cap=1, wait_s=0, pending=True, running=True)
        assert third and third.files, "the waiting runs hold no place under the cap"
        try:
            assert testslots.claim_running(third, "pa", lambda n: 1, cap=1)
            assert len(testslots.project_held("pa")) == 1
            assert not testslots.claim_running(waiting[0], "pa", lambda n: 1, cap=1), "the cap of 1 is now full"
        finally:
            third.close()
    finally:
        for g in waiting:
            g and g.close()


def test_a_never_started_lane_whose_process_is_gone_is_released(env, tmp_path, monkeypatch):
    """A lane whose run never started (still waiting for its test slots) and whose labelled process is gone,
    its lock kept open by a stray child, is no place under the cap and the next waiter reclaims it."""
    from ttp import locks, testslots
    _lane_env(monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    gone = subprocess.Popen(["true"])
    gone.wait()
    g = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, wait_s=0, pending=True)
    try:
        testslots.lane_waits(g, testslots.SLOT_WAITS)
        for f in g.files:
            locks.relabel(f, {"pid": gone.pid})
        assert testslots.project_held("pa") == []
        told = []
        h = testslots.suite_hold("pa", repo=_LANE_REPO, slots=1, wait_s=5, state=state, tell=told.append)
        assert h and h.files and any("reclaimed a checks lane" in x for x in told), told
        h.close()
    finally:
        g.close()


def test_push_checks_wait_for_the_load_guard_without_a_host_slot(env, monkeypatch):
    """#5476: push checks (push.check_slot) waiting under testslots.load_guard() take no host-wide checks
    slot, so other runs keep it; once the guard clears they take one, then their test slots."""
    import threading
    from ttp import checkcap, push, testslots
    p = make(env)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    _budget_of_one(monkeypatch)
    guard = ["the host is overloaded"]
    monkeypatch.setattr(testslots, "load_guard", lambda: guard[0])
    floor = _hold_load_floor()   # another run holds the one routine pass
    before = len(checkcap.grant_records())
    told, got = [], []
    t = threading.Thread(target=lambda: got.append(push.check_slot("abc", told.append, proj=p)))
    t.start()
    try:
        assert _wait_for(lambda: checkcap.PARKED_LINE in told), told
        time.sleep(0.5)
        assert checkcap.state()["running"] == [] and len(checkcap.grant_records()) == before, \
            "no host slot is held or granted while the load guard holds"
        other = checkcap.acquire("other project", project_name="other")
        assert other is not None and other.f is not None, "another run takes the free host slot"
        other.close()
        guard[0] = ""
        t.join(30)
        assert got and got[0].f is not None and got[0].tests.k == 1
    finally:
        guard[0] = ""
        t.join(30)
        floor.close()
        for s in got:
            s.close()


def test_checks_waiter_past_the_starve_bound_goes_before_a_later_priority_one_arrival_only_on_its_turn(env, monkeypatch):
    # A routine run that waited past checks.starve_s keeps later urgent (p1) arrivals ahead of it until N urgent
    # turns went while it waited (a set checks.priority_turns N, else checkcap.AGED_TURNS under the default
    # locks.URGENT_FIRST); then it takes the next free slot (#6033: before, it outranked every later arrival).
    from ttp import checkcap, locks, project
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    held = checkcap.acquire("h")
    now = time.time()
    old = locks.enqueue(checkcap.slot_dir(), checkcap.RESOURCE, "old p2", since=now - 120, group="A", priority=2)
    p1 = locks.enqueue(checkcap.slot_dir(), checkcap.RESOURCE, "new p1", since=now - 1, group="B", priority=1)
    try:
        monkeypatch.setenv(checkcap.STARVE_ENV, "60")
        for turns, n in (("3", 3), ("5", 5), (None, checkcap.AGED_TURNS)):
            if turns:
                monkeypatch.setenv("TTP_CHECKS_PRIORITY_TURNS", turns)
            else:
                monkeypatch.delenv("TTP_CHECKS_PRIORITY_TURNS")
            assert checkcap.aged_turns() == n
            locks.mark_turn(checkcap.slot_dir(), checkcap.RESOURCE, None)   # no urgent turns in a row yet
            for _ in range(n):
                assert checkcap.state()["queued"] == ["new p1", "old p2"], (turns, "the aged run's turn has not come")
                locks.mark_turn(checkcap.slot_dir(), checkcap.RESOURCE, p1)   # an urgent run took a slot meanwhile
            assert checkcap.state()["queued"] == ["old p2", "new p1"], (turns, "its turn after N urgent ones")
        monkeypatch.setenv(checkcap.STARVE_ENV, "600")
        assert checkcap.state()["queued"] == ["new p1", "old p2"], "under the bound: urgent first, as before"
        monkeypatch.setenv(checkcap.STARVE_ENV, "60")
        held.close()
        every = checkcap.all_paths(1)
        assert checkcap._take_in_turn(1, p1, checkcap.paths(1), every, "new p1", "", "") is None, \
            "the p1 waits behind the aged run on its turn"
        got = checkcap._take_in_turn(1, old, checkcap.paths(1), every, "old p2", "", "")
        assert got is not None, "the aged run takes the next free slot"
        got.close()
        monkeypatch.setenv("TTP_CHECKS_PRIORITY_TURNS", "0")
        assert checkcap.aged_turns() == 0, "priority_turns 0 (no urgent precedence): an aged run goes first"
        monkeypatch.delenv("TTP_CHECKS_PRIORITY_TURNS")
        for bad in ("0", "-5", "inf", "nan", "x"):   # never off: the bound keeps routine runs from starving
            monkeypatch.setenv(checkcap.STARVE_ENV, bad)
            assert checkcap.starve_s() == checkcap.STARVE_S
        monkeypatch.delenv(checkcap.STARVE_ENV)
        project.set_account_setting("checks.starve_s", 900)   # the writer behind `ttp config --account`
        assert checkcap.starve_s() == 900.0, "the account's checks.starve_s"
        monkeypatch.setenv(checkcap.STARVE_ENV, "300")
        assert checkcap.starve_s() == 300.0, "the env wins over the account setting"
        monkeypatch.delenv(checkcap.STARVE_ENV)
        project.set_account_setting("checks.starve_s", "x")
        assert checkcap.starve_s() == checkcap.STARVE_S, "an unreadable account value is the default"
    finally:
        held.close()
        locks.dequeue(old)
        locks.dequeue(p1)


def test_push_checks_hold_no_shared_test_slots_while_another_run_waits_for_them(env, monkeypatch):
    # Back-to-back push checks took every test slot of their project's budget while a full run waited
    # outside the checks queue for them. With any run waiting for test slots a push runs on its branch's
    # reserved lane alone; with nobody waiting it takes pool slots as before.
    from ttp import checkcap, locks, testslots
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    monkeypatch.setenv("TTP_TEST_POOL", "8")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "4")
    push = testslots.acquire("push", project_name="pa", push_key="remote main", wait_s=0)
    assert push.k == 4 and not locks.held_labels([testslots.reserved_path("remote main")]), "nobody waits: pool slots"
    push.close()
    for res in (checkcap.PARKED, testslots.TESTS.resource, f"{testslots.TESTS.own}.{locks.group_id('pa')}"):
        waiter = locks.enqueue(checkcap.slot_dir(), res, "pa task #1 waiting", since=time.time() - 30, group="pa")
        try:
            assert testslots.others_wait("pa")
            push = testslots.acquire("push", project_name="pa", push_key="remote main", wait_s=0)
            assert push.k == 4 and push.files and \
                locks.held_labels([testslots.reserved_path("remote main")]) == ["push"], res
            pool = testslots.paths(8) + testslots.own_paths("pa", 8)
            assert locks.held_labels(pool) == [], f"{res}: no pool or budget slot taken while a run waits"
            push.close()
        finally:
            locks.dequeue(waiter)
    assert not testslots.others_wait("pa")
    other = locks.enqueue(checkcap.slot_dir(), f"{testslots.TESTS.own}.{locks.group_id('pb')}", "pb waiter", group="pb")
    try:
        assert not testslots.others_wait("pa"), "another project's budget line is not the push's to free"
    finally:
        locks.dequeue(other)


def test_a_run_parked_past_the_starve_bound_gets_its_projects_next_test_slots(env, monkeypatch):
    # A full run parked outside the queue (its test slots could not be taken) holds a PARKED ticket; once it
    # has waited past checks.starve_s, later runs of its project find no room, it still does, other projects
    # are not held, and the status line shows how long it has waited.
    import threading
    from ttp import checkcap, testslots
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setenv(checkcap.STARVE_ENV, "60")
    monkeypatch.setenv("TTP_TEST_POOL", "8")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "4")
    monkeypatch.setattr(testslots, "load_guard", lambda: "")
    room, got = [False], []
    since = time.time() - 120
    rooms = []

    def startable():
        rooms.append(testslots.pool_room({}, "pa", since=since))   # what its own check reads
        return room[0]

    t = threading.Thread(target=lambda: got.append(checkcap.acquire(
        "pa task #1 (run 9)", project_name="pa", since=since, startable=startable, max_wait_s=30)))
    t.start()
    try:
        assert _wait_for(lambda: checkcap.state()["parked"])
        assert [h for h, _ in checkcap.state()["parked"]] == ["pa task #1 (run 9)"]
        assert "1 waiting outside the queue for test slots: pa task #1 (run 9) (waited 2.0 min)" in checkcap.line()
        assert not testslots.pool_room({}, "pa"), "a later run of its project leaves it the next slots"
        assert not testslots.pool_room({}, "pa", since=time.time() - 30)
        assert testslots.pool_room({}, "pa", since=since - 1), "one that arrived before it is not held"
        assert testslots.pool_room({}, "pb"), "another project is not held"
        assert _wait_for(lambda: rooms) and rooms[-1], "the parked run itself is never held by its own ticket"
        room[0] = True
        t.join(30)
        assert got and got[0].f is not None and not checkcap.state()["parked"], "it took a slot and left the line"
        assert testslots.pool_room({}, "pa")
    finally:
        room[0] = True
        t.join(30)
        for s in got:
            s and s.close()


def test_aged_parked_routine_runs_never_hold_a_later_urgent_run_of_their_project(env, monkeypatch):
    # #6033: three p2 runs parked past checks.starve_s and an aged p1 run that arrived after them: the p1 run
    # goes first (starved_ahead holds only later arrivals at the aged run's priority or lower). #6907: however
    # many p1 runs of the project took a slot since the p2s aged, an owed or urgent run is never held by them
    # (only checks.priority_turns 0 lets them go first). Push checks (no priority) are as before.
    from ttp import checkcap, locks, testslots
    monkeypatch.setenv(checkcap.STARVE_ENV, "3600")
    monkeypatch.setenv("TTP_TEST_POOL", "8")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "4")
    monkeypatch.setattr(testslots, "load_guard", lambda: "")
    now = time.time()
    d, P = checkcap.slot_dir(), checkcap.PARKED
    p2s = [locks.enqueue(d, P, f"pa task #{i} (run {i})", since=now - 9000 + i, group="pa", priority=2)
           for i in range(3)]
    late = now - 5000   # the p1 run: aged too, arrived after every p2
    p1 = locks.enqueue(d, P, "pa task #9 (run 9)", since=late, group="pa", priority=1)
    late = locks._arrival(p1)   # as its ticket records it
    try:
        assert checkcap.starved_ahead("pa", late, now, priority=1) == [], "the urgent run is not held"
        assert testslots.pool_room({}, "pa", priority=1, since=late), "so it has room"
        assert checkcap.starved_ahead("pa", now, now, priority=2) == p2s + [p1], "a later p2 still is, by arrival"
        assert not testslots.pool_room({}, "pa", priority=2)
        assert checkcap.starved_ahead("pa", now, now) == p2s + [p1], "push checks (no priority): as before"
        assert checkcap.starved_ahead("pa", now, now, priority=0) == [], "priority 0: never held by a less urgent"
        aged_at = now - 9000 + 3600
        checkcap.record_wait(None, "pa", 1, aged_at - 600, aged_at - 300)   # before they aged: not a pass
        checkcap.record_wait(None, "pb", 1, aged_at, aged_at + 60)   # another project's: not this one's turn
        checkcap.record_wait(None, "pa", None, aged_at, aged_at + 60, "push")   # push checks: not a turn
        for k in range(3 * checkcap.AGED_TURNS):
            checkcap.record_wait(None, "pa", 1, aged_at + k, aged_at + 100 + k)
            assert checkcap.starved_ahead("pa", late, now, priority=1) == [], f"{k + 1} urgent starts: still not held"
        assert testslots.pool_room({}, "pa", priority=1, since=late)
        assert checkcap.starved_ahead("pa", late, now, priority=0) == [], "priority 0 keeps its precedence"
        monkeypatch.setenv("TTP_CHECKS_PRIORITY_TURNS", "0")   # no urgent precedence: aged ones go first
        assert checkcap.starved_ahead("pa", late, now, priority=1) == p2s
    finally:
        for t in p2s + [p1]:
            locks.dequeue(t)


def test_a_parked_owed_run_takes_its_projects_next_freed_slot_before_older_aged_lower_priority_ones(env, monkeypatch):
    # #7318: with the pool full, two p2 runs parked past checks.starve_s and a newer p0 (owed) run of the same
    # project wait. The freed slot goes to the p0 run: the aged p2s leave it (checkcap.owed_ahead) instead of
    # taking it by polling first. Aging among the p2s, push checks, a landing and
    # other projects are unchanged.
    from ttp import checkcap, locks, testslots
    monkeypatch.setenv(checkcap.STARVE_ENV, "3600")
    monkeypatch.setenv("TTP_TEST_POOL", "8")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "4")
    monkeypatch.setattr(testslots, "load_guard", lambda: "")
    used = [8]
    monkeypatch.setattr(testslots, "_held", lambda paths: min(used[0], len(paths)))
    now = time.time()
    d, P = checkcap.slot_dir(), checkcap.PARKED
    p2s = [locks.enqueue(d, P, f"pa task #{i} (run {i})", since=now - 9000 + i, group="pa", priority=2)
           for i in range(2)]
    p0 = locks.enqueue(d, P, "pa task #9 (run 9)", since=now - 60, group="pa", priority=0)
    at = [locks._arrival(t) for t in p2s + [p0]]
    try:
        assert not any(testslots.pool_room({}, "pa", priority=p, since=a) for p, a in zip((2, 2, 0), at))
        used[0] = 4   # a run ended: its slots are free
        assert testslots.pool_room({}, "pa", priority=0, since=at[2]), "the owed run takes the freed slot"
        for a in at[:2]:
            assert not testslots.pool_room({}, "pa", priority=2, since=a), "older aged p2s leave it to it"
        assert testslots.pool_gate({}, "pa", priority=2, since=at[0]).endswith(testslots.OWED_GATE), "status says why"
        assert checkcap.owed_ahead("pa", 2) == [p0] and checkcap.owed_ahead("pa", 1) == [p0]
        assert checkcap.owed_ahead("pa", 0) == [], "same priority: by arrival and aging as before"
        assert checkcap.owed_ahead("pa") == [], "push checks (no priority): not held"
        assert checkcap.owed_ahead("pb", 2) == [] and testslots.pool_room({}, "pb", priority=2), "other projects"
        assert checkcap.starved_ahead("pa", at[1], now, priority=2) == p2s[:1], "aging among p2s: unchanged"
        assert checkcap.starved_ahead("pa", now, now, priority=2) == p2s, "a later p2 still waits on aged p2s"
        locks.dequeue(p0)
        assert testslots.pool_room({}, "pa", priority=2, since=at[0]), "the oldest p2 next once the p0 started"
        assert not testslots.pool_room({}, "pa", priority=2, since=at[1]), "the next p2 after it, as before"
        p0s = [locks.enqueue(d, P, f"pa task #{i} (run {i})", since=now - 8000 + i, group="pa", priority=0)
               for i in (20, 21)]
        assert checkcap.starved_ahead("pa", locks._arrival(p0s[1]), now, priority=0) == p0s[:1], \
            "same-priority aging among p0 runs: unchanged"
        for t in p0s:
            locks.dequeue(t)
        landing = locks.enqueue(d, P, "pa task #30 (run 30)", since=now - 30, group="pa", priority=2, landing=True)
        late = locks.enqueue(d, P, "pa task #31 (run 31)", since=now - 20, group="pa", priority=0)
        checkcap._PARKS.park = landing
        try:
            assert checkcap.owed_ahead("pa", 2) == [], "a landing's own ticket is not held"
        finally:
            checkcap._PARKS.park = None
        locks.dequeue(landing)
        locks.dequeue(late)
    finally:
        for t in p2s + [p0]:
            locks.dequeue(t)


def test_an_urgent_run_is_never_held_by_aged_routine_runs_whose_own_turn_came(env, monkeypatch):
    # #6999: a priority-1 run behind 11 aged p2 runs and 4 aged p3 runs of its project. Routine (p2) starts
    # made the p3s' own turn come (more urgent than p3), which used to hold the p1 run behind all of them.
    # #6907: an urgent run is not held by them however many urgent starts went since the project's last
    # routine start; fairness among p2+ runs is unchanged.
    from ttp import checkcap, locks, testslots
    monkeypatch.setenv(checkcap.STARVE_ENV, "3600")
    monkeypatch.setenv("TTP_TEST_POOL", "8")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "4")
    monkeypatch.setattr(testslots, "load_guard", lambda: "")
    now = time.time()
    d, P = checkcap.slot_dir(), checkcap.PARKED
    p3s = [locks.enqueue(d, P, f"pa task #{i} (run {i})", since=now - 20000 + i, group="pa", priority=3)
           for i in range(4)]
    p2s = [locks.enqueue(d, P, f"pa task #{10 + i} (run {10 + i})", since=now - 15000 + i, group="pa",
                         priority=2) for i in range(11)]
    p1 = locks.enqueue(d, P, "pa task #99 (run 99)", since=now - 60, group="pa", priority=1)
    late = locks._arrival(p1)
    try:
        before = checkcap.starved_ahead("pa", now, now, priority=2)
        for k in range(checkcap.AGED_TURNS + 1):   # routine starts: the p3s' own turn comes (p2 is more urgent)
            checkcap.record_wait(None, "pa", 2, now - 15000, now - 3000 + k)
        assert checkcap._aged_turn("pa", p3s, now) == p3s, "the p3s' turn came for a p2 run"
        assert checkcap.starved_ahead("pa", late, now, priority=1) == [], "but the p1 run is not held by any"
        assert testslots.pool_room({}, "pa", priority=1, since=late), "so it gets the next free slots"
        assert checkcap.starved_ahead("pa", now, now, priority=2) == p2s + p3s, "p2 fairness: p2s, then p3s' turn"
        assert before == p2s, "before the routine starts: the older p2s only"
        for k in range(2 * checkcap.AGED_TURNS):   # past the old bound: N urgent starts since the last routine one
            checkcap.record_wait(None, "pa", 0 if k % 2 else 1, late, now - 2000 + k)
        assert checkcap.starved_ahead("pa", late, now, priority=1) == [], "no routine turn ahead of an urgent run"
        assert testslots.pool_room({}, "pa", priority=1, since=late)
        assert checkcap.starved_ahead("pa", late, now, priority=0) == [], "priority 0 is never held"
        monkeypatch.setenv("TTP_CHECKS_PRIORITY_TURNS", "0")   # no urgent precedence: aged ones go first
        assert checkcap.starved_ahead("pa", late, now, priority=1) == p3s + p2s
    finally:
        for t in p3s + p2s + [p1]:
            locks.dequeue(t)


def test_a_short_targeted_run_goes_ahead_of_aged_long_full_runs_but_never_of_urgent_or_landing_ones(
        env, tmp_path, monkeypatch):
    # #7109: a targeted run (testpick.narrow) parked for its test slots is not held by aged parked full runs
    # of its priority or less urgent whose lane label's cost is checklane.CHEAP_FACTOR times its own; a more
    # urgent one, a landing's and one of unknown or near cost still go first (checkcap.starved_ahead).
    from ttp import checkcap, checklane, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setenv(checkcap.STARVE_ENV, "3600")
    monkeypatch.setenv("TTP_TEST_POOL", "8")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "4")
    monkeypatch.setattr(testslots, "load_guard", lambda: "")
    full = {"cls": checklane.SUITE, "est_s": 1260.0}
    holders = [_lane_holder(tmp_path, "pa", r, cost=full, task=str(t), who=f"run {t}")   # one lane per repository
               for t, r in ((41, _LANE_REPO), (42, "ba9876543210"))]
    now = time.time()
    d, P = checkcap.slot_dir(), checkcap.PARKED
    p2 = locks.enqueue(d, P, "pa task #41 (run 41)", since=now - 9000, group="pa", priority=2)
    try:
        short = {"cls": checklane.SUITE, "est_s": 140.0, "tests": 87, "of": 1960}
        assert checkcap.starved_ahead("pa", now, now, priority=2) == [p2], "no cost given: as before"
        assert checkcap.starved_ahead("pa", now, now, priority=2, cost=short) == [], "much cheaper: it goes first"
        assert testslots.pool_room({}, "pa", priority=2, cost=short)
        assert not testslots.pool_room({}, "pa", priority=2)
        assert checkcap.starved_ahead("pa", now, now, priority=1, cost=short) == [], "a more urgent one too"
        near = {"cls": checklane.SUITE, "est_s": 600.0, "tests": 900, "of": 1960}
        assert checkcap.starved_ahead("pa", now, now, priority=2, cost=near) == [p2], "not 4x cheaper: held"
        assert checkcap.starved_ahead("pa", now, now, cost=short) == [p2], "push checks (no priority): as before"
        p1 = locks.enqueue(d, P, "pa task #42 (run 42)", since=now - 8000, group="pa", priority=1)
        assert checkcap.starved_ahead("pa", now, now, priority=2, cost=short) == [p1], "a more urgent one goes first"
        locks.dequeue(p1)
        unknown = locks.enqueue(d, P, "pa task #43 (run 43)", since=now - 8000, group="pa", priority=2)
        assert checkcap.starved_ahead("pa", now, now, priority=2, cost=near) == [p2, unknown], "no lane label: held"
        assert checkcap.starved_ahead("pa", now, now, priority=2, cost=short) == [], "short (#7217): not held"
        locks.dequeue(unknown)
        landing = locks.enqueue(d, P, "pa task #42 (run 42)", since=now - 8000, group="pa", priority=2, landing=True)
        assert checkcap.starved_ahead("pa", now, now, priority=2, cost=short) == [landing], "a landing goes first"
        locks.dequeue(landing)
        # With no medians known, a targeted run of at most a quarter of its tests is much cheaper than a full one.
        assert checklane.much_cheaper({"cls": checklane.SUITE, "tests": 87, "of": 1960}, {"cls": checklane.SUITE})
        assert not checklane.much_cheaper({"cls": checklane.SUITE, "tests": 900, "of": 1960}, {"cls": checklane.SUITE})
        assert not checklane.much_cheaper({"cls": checklane.SUITE}, {"cls": checklane.SUITE})
        assert not checklane.much_cheaper(short, None) and not checklane.much_cheaper(None, full)
        # and cheaper() (the overflow lane past checks.max_wait_s) prefers it over a full one, never the reverse.
        assert checklane.cheaper({"cls": checklane.SUITE, "tests": 87, "of": 1960}, {"cls": checklane.SUITE})
        assert not checklane.cheaper({"cls": checklane.SUITE}, {"cls": checklane.SUITE, "tests": 87, "of": 1960})
        assert checklane.cheaper(short, full) and not checklane.cheaper(full, short)
    finally:
        locks.dequeue(p2)
        for h in holders:
            _stop(h)



def test_a_short_targeted_run_is_not_held_by_parked_full_runs_and_queues_first_within_the_host_cap(
        env, monkeypatch):
    # #7217: the 13-test targeted run 14440 waited 50 min behind 13 parked full runs. A targeted run of fewer
    # than checks.small_tests tests (checkcap.is_small) is held by no parked longer run of its priority or a
    # less urgent one (starved_ahead), whatever their costs; an older short one, a landing's and a more urgent
    # one still hold it. In the host queue it goes first among its project's waiters at its priority
    # (locks.SMALL_TICKET, fair_order), and the host cap (checks.host_max) still holds it.
    import threading
    from ttp import checkcap, locks, project
    monkeypatch.setenv(checkcap.STARVE_ENV, "3600")
    tiny, full = {"cls": 2, "tests": 13, "of": 9000}, {"cls": 2, "tests": 6000, "of": 9000}
    assert checkcap.small_tests() == checkcap.SMALL_TESTS == 500
    assert checkcap.is_small(tiny) and not checkcap.is_small(full) and not checkcap.is_small({"cls": 2})
    assert not checkcap.is_small(None) and not checkcap.is_small({"tests": True})
    monkeypatch.setattr(project, "load_account_settings", lambda: {"checks": {"small_tests": 20}})
    assert checkcap.small_tests() == 20 and checkcap.is_small(tiny)
    monkeypatch.setenv(checkcap.SMALL_ENV, "10")
    assert checkcap.small_tests() == 10 and not checkcap.is_small(tiny), "the env wins"
    monkeypatch.setenv(checkcap.SMALL_ENV, "-3")
    assert checkcap.small_tests() == 20, "a negative env value is ignored"
    monkeypatch.delenv(checkcap.SMALL_ENV)
    monkeypatch.setattr(project, "load_account_settings", lambda: {"checks": {"small_tests": "x"}})
    assert checkcap.small_tests() == 500, "unreadable: the default"
    monkeypatch.setattr(project, "load_account_settings", lambda: {})
    now = time.time()
    d, P = checkcap.slot_dir(), checkcap.PARKED
    parked = [locks.enqueue(d, P, f"pa task #{60 + i} (run {60 + i})", since=now - 9000 + i, group="pa", priority=2)
              for i in range(13)]
    extra = []
    try:
        assert len(checkcap.starved_ahead("pa", now, now, priority=2, cost=full)) == 13, "a full run: as before"
        assert checkcap.starved_ahead("pa", now, now, priority=2, cost=tiny) == [], "13 tests: not held"
        monkeypatch.setenv(checkcap.SMALL_ENV, "0")   # off: the cost rule alone, and unknown costs hold it
        assert len(checkcap.starved_ahead("pa", now, now, priority=2, cost=tiny)) == 13
        monkeypatch.delenv(checkcap.SMALL_ENV)
        extra.append(older := locks.enqueue(d, P, "pa task #80 (run 80)", since=now - 9500, group="pa",
                                            priority=2, small=True))
        assert locks.is_small(older) and not locks.is_small(parked[0])
        assert checkcap.starved_ahead("pa", now, now, priority=2, cost=tiny) == [older], "short ones: first come"
        locks.dequeue(extra.pop())
        extra.append(landing := locks.enqueue(d, P, "pa task #81 (run 81)", since=now - 9500, group="pa",
                                              priority=2, landing=True))
        assert checkcap.starved_ahead("pa", now, now, priority=2, cost=tiny) == [landing], "a landing holds it"
        locks.dequeue(extra.pop())
        extra.append(urgent := locks.enqueue(d, P, "pa task #82 (run 82)", since=now - 9500, group="pa", priority=1))
        assert checkcap.starved_ahead("pa", now, now, priority=2, cost=tiny) == [urgent], "a more urgent one too"
    finally:
        for t in parked + extra:
            locks.dequeue(t)
    # The host queue: a full run then a 13-test run, same project and priority, behind the one slot of the cap.
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    for small, want in ((False, ["full", "tiny"]), (True, ["tiny", "full"])):
        held = checkcap.acquire("h")
        got, slots = [], []

        def take(label, is_small):
            s = checkcap.acquire(label, project_name="pa", priority=2, small=is_small)
            got.append((label, len(checkcap.state()["running"])))
            slots.append(s)
            s.close()

        waiters = []
        for i, args in enumerate([("full", False), ("tiny", small)]):
            waiters.append(threading.Thread(target=take, args=args))
            waiters[-1].start()
            assert _wait_for(lambda: len(checkcap.state()["queued"]) == i + 1)
        assert checkcap.state()["queued"] == want, "the 13-test run ranks first only as a short one"
        time.sleep(0.3)
        assert got == [] and checkcap.state()["running"] == ["h"], "the cap holds both while its slot is held"
        held.close()
        for w in waiters:
            w.join(20)
        assert [g[0] for g in got] == want and all(n == 1 for _, n in got), "one at a time: the cap holds"

def test_a_targeted_run_beside_an_unnarrowed_full_suite_is_not_short_and_waits_behind_aged_full_runs(
        env, monkeypatch):
    # #7329: checkcap.is_small read only the targeted commands' "tests", so a 13-test targeted check run with a
    # full pytest after `--` was short: it queued first and skipped aged parked full runs at a full run's cost.
    # checklane.cost marks such a run "whole": never short (is_small) and never targeted-only (much_cheaper).
    from ttp import checkcap, checklane, locks, push
    monkeypatch.setenv(checkcap.STARVE_ENV, "3600")
    own = push.check_list(["python3 -m pytest -q tests/test_a.py"])
    c = push.Check(str(own[0]))   # as testpick.narrow writes a targeted check
    c.__dict__.update(own[0].__dict__)
    c.targeted, c.scope, c.full = True, {"tests": 13, "of": 9000}, own[0]
    extra = push.check_list(["python3 -m pytest -q tests/test_b.py"])
    tiny = checklane.cost(None, "k1", [c])
    mixed = checklane.cost(None, "k2", [c] + list(extra))
    assert tiny == {"cls": checklane.SUITE, "tests": 13, "of": 9000} and checkcap.is_small(tiny)
    assert mixed == tiny | {"whole": True} and not checkcap.is_small(mixed)
    assert checklane.cost(None, "k3", [c] + list(push.check_list(["true"]))) == tiny, "a light command: still short"
    full = {"cls": checklane.SUITE}
    assert checklane.much_cheaper(tiny, full) and not checklane.much_cheaper(mixed, full)
    assert not checklane.cheaper(mixed, full), "no medians: by class, as a full run"
    now = time.time()
    d, P = checkcap.slot_dir(), checkcap.PARKED
    parked = [locks.enqueue(d, P, f"pa task #{60 + i} (run {60 + i})", since=now - 9000 + i, group="pa", priority=2)
              for i in range(3)]
    try:
        assert checkcap.starved_ahead("pa", now, now, priority=2, cost=tiny) == []
        assert checkcap.starved_ahead("pa", now, now, priority=2, cost=mixed) == parked, "held as a full run"
    finally:
        for t in parked:
            locks.dequeue(t)


def test_aged_routine_waiters_take_routine_turns_and_nothing_changes_without_a_backlog(tmp_path):
    # #6033 on the host queue (locks.fair_order): aged routine waiters go ahead of later routine ones only;
    # urgent ones stay first until `aged_turns` urgent turns went in a row (mark_turn's streak). With no
    # waiter past the bound the order is the same as with no bound at all, in every urgent mode.
    from ttp import locks
    d = tmp_path / "slots"
    for n, (label, g, pr) in enumerate([("a2", "A", 2), ("b2", "B", 2), ("c2", "C", 2), ("a1", "A", 1),
                                        ("d3", "D", 3), ("b1", "B", 1)]):
        locks.enqueue(d, "r", label, since=1000.0 + n, group=g, priority=pr)
    names = lambda m, **k: [x.read_text() for x in locks.fair_order(d, "r", urgent_max=m, **k)]
    aged = {"aged_before": 1003.5, "aged_turns": 3}   # the three p2s and a1 waited past the bound
    for m in (locks.URGENT_FIRST, 3):
        assert names(m, **aged)[:2] == ["a1", "b1"], (m, "the p1s first, the aged one and the later one")
        assert names(m, **aged)[2:5] == ["a2", "b2", "c2"], (m, "then the aged p2s by arrival")
    for m in (locks.URGENT_FIRST, 3, 0):
        assert names(m, aged_before=999.0, aged_turns=3) == names(m), (m, "no backlog: unchanged")
    locks.streak_path(d, "r").write_text("3")   # three urgent turns went while routine ones waited
    assert names(locks.URGENT_FIRST, **aged)[:3] == ["a2", "a1", "b1"], "the oldest aged p2's turn"
    assert names(3, **aged)[:3] == ["a2", "a1", "b1"], "under priority_turns 3 too"
    assert names(locks.URGENT_FIRST, aged_before=1003.5)[:2] == ["a1", "b1"], "no aged_turns: no such turn"
    assert names(0, **aged)[:4] == ["a2", "b2", "c2", "a1"], "priority_turns 0: every aged one first, as before"


def test_checks_status_names_the_push_priority_holder_and_each_waiters_wait(env, monkeypatch):
    import json
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    held = checkcap.acquire("h")
    key = "git@example:org/repo#main"
    push = checkcap.acquire_priority(key, "proj push batch 5", "push checks")
    path = checkcap.priority_path(key)
    rec = json.loads(path.read_text())
    path.write_text(json.dumps(rec | {"since": rec["since"] - 600}))   # it took the slot 10 min ago
    w = locks.enqueue(checkcap.slot_dir(), checkcap.RESOURCE, "proj2 task #8 (run 3)", since=time.time() - 300,
                      group="proj2", priority=2)
    fresh = locks.enqueue(checkcap.slot_dir(), checkcap.RESOURCE, "proj3 task #9 (run 4)", group="proj3")
    try:
        line = checkcap.line()
        assert "push priority slot: proj push batch 5 (held 10.0 min)" in line, line
        assert "2 queued: proj2 task #8 (run 3) (waited 5.0 min); proj3 task #9 (run 4) (running: h)" in line, line
    finally:
        push.close()
        held.close()
        locks.dequeue(w)
        locks.dequeue(fresh)


def test_a_push_priority_slot_held_by_a_stuck_push_bounds_the_next_pushs_wait_then_it_takes_an_ordinary_turn(
        env, monkeypatch):
    """Another push holding the branch's priority slot (stuck, or itself waiting) holds the next push only
    checks.push_priority_wait_s: past it the waiter says who held it and takes an ordinary host-wide turn,
    on the pool's test slots rather than the reserved lane. A free slot is still taken at once, a caller
    without a fallback keeps its old wait, and a bad bound keeps the default."""
    import threading
    from ttp import checkcap, push, testslots
    p = make(env)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    monkeypatch.setenv(checkcap.PRIORITY_WAIT_ENV, "0.3")
    key = "git@example:org/repo#main"
    stuck = checkcap.acquire_priority(key, "proj2 push batch 1", "push checks")
    try:
        told, fell = [], []
        start = time.time()
        got = checkcap.acquire_priority(key, "proj push batch 2", "push checks", told.append,
                                        fallback=lambda: fell.append(1) or checkcap.acquire("proj push batch 2"))
        try:
            assert got.fell_back and fell and got.f is not None, "an ordinary host-wide slot"
            assert time.time() - start < 5, "bounded"
            assert any("stayed held" in t and "proj2 push batch 1" in t and "ordinary host-wide" in t
                       for t in told), told
        finally:
            got.close()
        # push.check_slot: the fallback turn takes test slots from the pool, never the reserved lane.
        keys = []
        monkeypatch.setattr(testslots, "acquire", lambda *a, **k: keys.append(k.get("push_key")))
        push.check_slot("abc", told.append, priority=key, proj=p).close()
        assert keys == [""], keys
        # No fallback: the old wait (only another push to the branch), unbounded.
        done = []
        t = threading.Thread(target=lambda: done.append(checkcap.acquire_priority(key, "proj push 3")), daemon=True)
        t.start()
        t.join(1.0)
        assert not done, "a caller without a fallback waits for the holder"
    finally:
        stuck.close()
    t.join(5)
    assert done and not done[0].fell_back
    done[0].close()
    # A free priority slot is taken at once, no fallback.
    free = checkcap.acquire_priority(key, "proj push 4", fallback=lambda: pytest.fail("no fallback"))
    assert not free.fell_back and free.f is not None
    free.close()
    for bad in ("0", "-5", "inf", "x"):
        monkeypatch.setenv(checkcap.PRIORITY_WAIT_ENV, bad)
        assert checkcap.priority_wait_s() == checkcap.PRIORITY_WAIT_S, bad


def test_detached_checks_waiting_for_a_slot_keep_their_place_after_their_worker_run_hands_off(
        env, tmp_path, monkeypatch, capsys):
    # A real `ttp checks --detach` parks for the host-wide slot, then its worker run ends (exit.json): another
    # reader of the queue keeps its ticket (it outlives the run by design), and it gets the slot in turn.
    import signal
    from ttp import checkcap, cli, locks
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    held = checkcap.acquire("other")
    pid = None
    try:
        pid = _detached_checks(run, capsys, "true")
        assert _wait_for(lambda: (run / cli.CHECKS_SLOT_WAIT).exists())
        waiting = locks.queued(checkcap.slot_dir(), checkcap.RESOURCE)
        assert len(waiting) == 1 and json.loads(locks._owner_file(waiting[0]).read_text())["pid"] == pid
        (run / "exit.json").write_text("{}")   # the worker handed off `waiting` on these checks
        assert locks.queued(checkcap.slot_dir(), checkcap.RESOURCE) == waiting
        assert checkcap.state()["queued"] and not (run / cli.CHECKS_RC).exists()
        held.close()
        assert _wait_precheck(run) == "0"
    finally:
        held.close()
        if pid and not _gone_pid(pid):
            os.killpg(pid, signal.SIGKILL)


def test_own_push_checks_take_the_own_branch_lane_at_once_while_a_long_run_holds_the_budget(env, monkeypatch, tmp_path):
    """`ttp push --own` (push.publish) takes its checks slot through its remote's own-branch push lane
    (push.own_lane_key) by default: with the host cap and its project's test budget held by a long
    `ttp checks` run, it runs its checks at once. delivery.own_push_reserved_lane false restores the
    ordinary turn: it waits until that run ends."""
    import threading
    from ttp import checkcap, push, testslots
    p, repo, origin, other = _push_setup(env, monkeypatch, [])
    (tmp_path / "run").mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv(checkcap.BEYOND_CAP_ENV, "1")  # the opt-in overflow; the default counts it in the cap
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    monkeypatch.setattr(testslots, "load_guard", lambda: "")
    _budget_of_one(monkeypatch)
    _git_out(repo, "checkout", "-q", "-b", "own")
    key = push.own_lane_key(repo, "origin", p.config())
    assert key.endswith("#" + push.OWN_LANE) and push.own_lane_key(repo, "origin", None) == key, "on by default"
    long_slot = checkcap.acquire("long checks", project_name=p.name)
    long_tests = testslots.acquire("long checks", project_name=p.name)
    assert long_slot.f is not None and long_tests.k == 1
    seen = tmp_path / "slots"
    checks = push.check_list([f"echo $TTP_TEST_SLOTS >> {seen}"])
    try:
        for n, name in enumerate(("a.txt", "b.txt")):
            _commit(repo, name, "x\n")
            said, rc = [], []
            t = threading.Thread(target=lambda: rc.append(push.publish(repo, "origin", "own", checks, say=said.append,
                                                                       cfg=p.config())))
            t0 = time.time()
            t.start()
            t.join(30)
            assert rc == [0] and time.time() - t0 < 30, said
            assert seen.read_text().split() == ["1"] * (n + 1), "its test slots came from the reserved lane"
            assert any("push priority slot" in m for m in said), said
        # Off: an ordinary turn behind the long run, as before.
        p.set_config("delivery.own_push_reserved_lane", False)
        assert push.own_lane_key(repo, "origin", p.config()) == ""
        _commit(repo, "c.txt", "x\n")
        said, rc = [], []
        t = threading.Thread(target=lambda: rc.append(push.publish(repo, "origin", "own", checks, say=said.append,
                                                                   cfg=p.config())))
        t.start()
        t.join(1.5)
        assert t.is_alive() and not rc, "it waits for the long run's slots"
        long_tests.close()
        long_slot.close()
        t.join(30)
        assert rc == [0], said
    finally:
        long_tests.close()
        long_slot.close()


def test_a_push_lanes_load_guard_wait_ends_at_its_bound_on_the_reserved_lane_alone(env, monkeypatch):
    """Push checks (testslots.acquire's push_key) wait under load_guard at most checks.push_load_wait_s
    (default 300 s), then start on their branch's reserved lane alone, one per lane at a time. Task
    checks, priority-1 ones included, keep waiting; "off" restores the unbounded wait. That bound is for
    while another urgent or push run holds the one load pass (load_passes, #5992)."""
    from ttp import locks, project, testslots
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    _slot_sizes(monkeypatch, 8, 2)
    monkeypatch.delenv(testslots.PUSH_LOAD_WAIT_ENV, raising=False)
    monkeypatch.setattr(testslots, "load_guard", lambda: "the host is overloaded")
    monkeypatch.setattr(testslots, "load_passes", lambda *a, **k: False)   # another run holds the pass
    assert testslots.push_load_wait_s({}) == testslots.PUSH_LOAD_WAIT_S == 300.0, "bounded by default"
    assert testslots.acquire("push", project_name="pa", push_key="remote b", wait_s=0.3) is None, \
        "under the default bound it still waits"
    monkeypatch.setenv(testslots.PUSH_LOAD_WAIT_ENV, "0.3")
    told = []
    t0 = time.time()
    push = testslots.acquire("push", told.append, project_name="pa", push_key="remote b", wait_s=20)
    try:
        assert push is not None and push.k == 2 and 0.3 <= time.time() - t0 < 10, told
        assert locks.held_labels([testslots.reserved_path("remote b")]) == ["push"]
        assert not locks._slot_labels(testslots.paths(8)), "no pool slot is taken"
        assert any("reserved lane alone" in m and "push_load_wait_s" in m for m in told), told
        assert testslots.acquire("push 2", project_name="pb", push_key="remote b", wait_s=0.8) is None, \
            "one push check per lane at a time"
        other = testslots.acquire("push 3", project_name="pb", push_key="remote c", wait_s=20)
        assert other is not None, "another branch's lane is its own"
        other.close()
        for kw in ({}, {"priority": 1}, {"full": True}):
            assert testslots.acquire("task", project_name="pc", wait_s=0.8, **kw) is None, kw
    finally:
        push and push.close()
    for off in ("off", False):
        assert testslots.push_load_wait_s({"checks": {"push_load_wait_s": off}}) is None
    assert testslots.acquire("push", project_name="pa", push_key="remote b", wait_s=0.8,
                             cfg={"checks": {"push_load_wait_s": "off"}}) is None, "off: the unbounded wait"
    monkeypatch.delenv(testslots.PUSH_LOAD_WAIT_ENV)
    for bad, want in (("x", 300.0), (-1, 300.0), ("inf", 300.0), (0, 0.0), ("45", 45.0)):
        assert testslots.push_load_wait_s({"checks": {"push_load_wait_s": bad}}) == want, bad
    project.set_account_setting("checks.push_load_wait_s", 60)
    assert testslots.push_load_wait_s({}) == 60.0, "the account's setting"
    assert testslots.push_load_wait_s({"checks": {"push_load_wait_s": 30}}) == 30.0, "the project's wins"


def _fake_disk(monkeypatch, tmp_path, dirty_kb=0, full60=None, dirty_max=None, io_max=None):
    """Fake /proc/meminfo and /proc/pressure/io for diskgate (full60 None: a kernel without PSI)."""
    from ttp import diskgate
    mem, psi = tmp_path / "meminfo", tmp_path / "pressure-io"
    mem.write_text(f"MemTotal:       65536000 kB\nDirty:          {dirty_kb} kB\nWriteback:           0 kB\n")
    if full60 is None:
        psi.unlink(missing_ok=True)
    else:
        psi.write_text(f"some avg10=1.00 avg60=2.00 avg300=1.00 total=1\nfull avg10=0.50 avg60={full60} avg300=0.10 total=1\n")
    monkeypatch.setattr(diskgate, "MEMINFO", mem)
    monkeypatch.setattr(diskgate, "PSI_IO", psi)
    for key, v in (("TTP_CHECKS_DIRTY_MAX_MB", dirty_max), ("TTP_CHECKS_IO_FULL_MAX", io_max)):
        if v is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(v))


def _fake_cpu(monkeypatch, tmp_path, busy, running, blocked, load=67.0, cores=64):
    """Fake /proc/stat for hostcpu after a window of SAMPLE_S+ (no sleep): `busy` of `cores` cores busy,
    `running` tasks running or waiting to run and `blocked` in D state at both readings; a fixed load average."""
    from ttp import checkcap, hostcpu
    stat = tmp_path / "stat"
    idle = (cores - busy) * 100
    stat.write_text(f"cpu  {busy * 50} 0 {busy * 50} {idle} 0 0 0 0 0 0\ncpu0 1 0 1 1 0 0 0 0 0 0\n"
                    f"intr 1\nprocs_running {running}\nprocs_blocked {blocked}\n")
    monkeypatch.setattr(hostcpu, "STAT", stat)
    monkeypatch.setattr(hostcpu, "_LAST", {"at": (time.monotonic() - 2 * hostcpu.SAMPLE_S, 0, 0, running)})
    monkeypatch.setattr(hostcpu, "cores", lambda: cores)
    monkeypatch.setattr(checkcap, "cores", lambda: cores)
    monkeypatch.setattr(os, "cpu_count", lambda: cores)
    monkeypatch.setattr(os, "getloadavg", lambda: (load, load, load))
    monkeypatch.setenv("TTP_CHECKS_LOAD_GUARD", "1")


def test_the_load_guard_judges_cpu_by_runnable_demand_and_io_pressure_by_its_own_limit(env, tmp_path, monkeypatch):
    """#6836: a load average over the cores made of tasks in D state (waiting on a contended disk) while
    half the cores sit idle holds nothing; runnable demand over the cores holds new suites, named as the
    CPU limit (checks.load_guard); io PSI over checks.io_full_max holds new runs, named as I/O pressure."""
    from ttp import checkcap, diskgate, hostcpu, hostload, testslots
    # (a) load 67 on 64 cores from 35 D-state waiters, 34 cores busy, io PSI under its limit: not held.
    _fake_cpu(monkeypatch, tmp_path, busy=34, running=35, blocked=35)
    _fake_disk(monkeypatch, tmp_path, full60="2.00")
    assert hostcpu.demand() == 34.0 and checkcap.cpu_demand() == 34.0, "the reader itself is not demand"
    assert testslots.load_guard() == "" and diskgate.saturated() == ""
    s = hostload.snapshot()
    assert s["load"][1] == 67.0 and s["demand"] == 34.0 and not hostload.overloaded({**s, "running": 4, "cap": 4})
    assert "CPU demand 34.0 cores" in hostload.lines(s=s)[0] and "over its limit" not in hostload.lines(s=s)[0]
    # A measured host state: load 61.3 on 64 cores, user+system about 57% (idle 42%), 25 tasks in D state
    # on filesystem locks: I/O-bound, not CPU-overloaded.
    _fake_cpu(monkeypatch, tmp_path, busy=37, running=38, blocked=25, load=61.3)
    assert checkcap.cpu_demand() == 37.0 and testslots.load_guard() == ""
    assert not hostload.overloaded({**hostload.snapshot(), "running": 4, "cap": 4})
    # (b) runnable demand over the cores (all busy, 90 tasks wanting a core): held as the CPU limit.
    _fake_cpu(monkeypatch, tmp_path, busy=64, running=91, blocked=0, load=60.0)
    why = testslots.load_guard()
    assert why.startswith("the host's CPU is overloaded: its runnable demand of 90.0 cores is above its 64 "
                          "cores (CPU limit checks.load_guard)"), why
    assert diskgate.saturated() == ""
    s = hostload.snapshot()
    assert hostload.overloaded({**s, "running": 4, "cap": 4}), "the watch judges the demand, not the load of 60"
    assert hostload.lines(s={**s, "running": 4, "cap": 4})[0].endswith(", overloaded: CPU over its limit")
    monkeypatch.setenv("TTP_CHECKS_LOAD_GUARD", "2")
    assert testslots.load_guard() == "", "90 is under 2 x 64"
    monkeypatch.setenv("TTP_CHECKS_LOAD_GUARD", "0")
    assert testslots.load_guard() == "", "0 turns the CPU limit off"
    # (c) io PSI over its limit with idle cores: held as I/O pressure, by its own key; the CPU limit holds nothing.
    _fake_cpu(monkeypatch, tmp_path, busy=10, running=11, blocked=40)
    _fake_disk(monkeypatch, tmp_path, full60="35.00")
    assert testslots.load_guard() == ""
    assert diskgate.saturated() == "io pressure full avg60 35.0% over 10% (checks.io_full_max)"
    assert "disk saturated: new local check runs held" in diskgate.line()
    # Neither /proc/stat nor macOS's CPU ticks: the 1-minute load average stands in.
    monkeypatch.setattr(hostcpu, "STAT", tmp_path / "missing")
    monkeypatch.setattr(hostcpu, "MACH", False)
    monkeypatch.setattr(hostcpu, "_LAST", {})
    monkeypatch.setenv("TTP_CHECKS_LOAD_GUARD", "1")
    assert hostcpu.demand() is None and checkcap.cpu_demand() == 67.0 and testslots.load_guard()


def _fake_mac(monkeypatch, tmp_path, idle, mem, load, cores=14):
    """A Mac for hostcpu: no /proc/stat, the kernel's CPU ticks `idle` (share) idle over a window of SAMPLE_S+
    (no sleep), its memory level `mem` (share free) and a fixed load average."""
    from ttp import checkcap, hostcpu
    monkeypatch.setattr(hostcpu, "STAT", tmp_path / "missing")
    monkeypatch.setattr(hostcpu, "MACH", True)
    monkeypatch.setattr(hostcpu, "mach_ticks", lambda: (round((1 - idle) * 1000), 1000))
    monkeypatch.setattr(hostcpu, "_LAST", {"at": (time.monotonic() - 2 * hostcpu.SAMPLE_S, 0, 0, None)})
    monkeypatch.setattr(hostcpu, "mach_mem_free", lambda: mem)
    monkeypatch.setattr(hostcpu, "cores", lambda: cores)
    monkeypatch.setattr(checkcap, "cores", lambda: cores)
    monkeypatch.setattr(os, "cpu_count", lambda: cores)
    monkeypatch.setattr(os, "getloadavg", lambda: (load, load, load))
    monkeypatch.setenv("TTP_CHECKS_LOAD_GUARD", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "2")


def test_a_mac_load_of_40_with_two_thirds_of_its_cpu_idle_and_free_memory_holds_nothing(env, tmp_path, monkeypatch):
    """Owner's agent msg #164: a Mac whose load average reads 40 on 14 cores (threads blocked on I/O) while
    67% of its CPU sits idle and 45% of its memory is free paces on the measured CPU and memory: the load
    guard, the checks host watch, extra check slots and the daily review's worker slots hold nothing."""
    from ttp import checkcap, hostcpu, hostload, testslots, throughput
    _fake_mac(monkeypatch, tmp_path, idle=0.67, mem=0.45, load=40.0)
    assert round(hostcpu.demand(), 2) == 4.62 and round(checkcap.cpu_demand(), 2) == 4.62, "33% of 14 cores"
    assert hostcpu.mem_free() == 0.45 and hostcpu.mem_short() == ""
    assert testslots.load_guard() == ""
    s = hostload.snapshot()
    assert s["load"][0] == 40.0 and s["mem_free"] == 0.45
    assert not hostload.overloaded({**s, "running": 4, "cap": 4, "spilled": {}})
    assert throughput.cpu_free() == (True, "")
    assert checkcap.extra(1) == 1, "4.6 cores busy is under half of 14"
    assert checkcap.free_cores() == 14 - hostcpu.busy_cores()


def test_a_mac_with_little_cpu_idle_or_short_memory_is_held(env, tmp_path, monkeypatch):
    """The same pacing holds on measured trouble: CPU idle under 5% (the load average then counts tasks
    waiting for a core, as the ticks cannot) or memory under MEM_FREE_MIN free."""
    from ttp import checkcap, hostcpu, hostload, testslots, throughput
    _fake_mac(monkeypatch, tmp_path, idle=0.02, mem=0.45, load=40.0)
    assert checkcap.cpu_demand() == 40.0, "a saturated CPU: the load average stands in for its run queue"
    assert testslots.load_guard().startswith("the host's CPU is overloaded: its runnable demand of 40.0 cores")
    assert hostload.overloaded({**hostload.snapshot(), "running": 4, "cap": 4, "spilled": {}})
    ok, why = throughput.cpu_free()
    assert not ok and why == "host CPU demand 40.0 cores on 14 cores leaves no CPU free", why
    assert checkcap.extra(1) == 0
    _fake_mac(monkeypatch, tmp_path, idle=0.20, mem=0.45, load=40.0)
    assert round(checkcap.cpu_demand(), 1) == 11.2 and testslots.load_guard() == "", "80% busy: under its cores"
    ok, why = throughput.cpu_free()
    assert not ok and why == "host CPU demand 11.2 cores on 14 cores leaves no CPU free", "over 0.75 x 14"
    # Memory short, CPU idle: held by memory, named so.
    _fake_mac(monkeypatch, tmp_path, idle=0.67, mem=0.05, load=40.0)
    assert hostcpu.mem_short() == "memory short: 5% free, under 15%"
    assert testslots.load_guard() == ("the host is memory short: 5% free, under 15%; no new suite starts "
                                      "until memory frees, running ones go on")
    s = {**hostload.snapshot(), "running": 4, "cap": 4, "spilled": {}}
    assert hostload.overloaded(s) and "has run memory short: 5% free" in hostload.text(s, 900.0)
    assert hostload.lines(s=s)[0].endswith(", overloaded: memory short: 5% free, under 15%")
    assert throughput.cpu_free() == (False, "the host is memory short: 5% free, under 15%")
    assert checkcap.extra(1) == 0
    monkeypatch.setenv("TTP_CHECKS_LOAD_GUARD", "0")
    assert testslots.load_guard() == "", "0 turns the guard off, memory too"


def test_hostcpu_reads_memory_free_from_meminfo_and_unknown_memory_holds_nothing(env, tmp_path, monkeypatch):
    from ttp import hostcpu
    meminfo = "MemTotal:       64000000 kB\nMemFree:  1000 kB\nMemAvailable:   8000000 kB\n"
    assert hostcpu.mem_free(lambda: meminfo) == 0.125
    assert hostcpu.mem_short(0.125) == "memory short: 12% free, under 15%" and hostcpu.mem_short(0.5) == ""
    assert hostcpu.mem_free(lambda: "MemTotal: 64000000 kB\n") is None, "no MemAvailable"
    monkeypatch.setattr(hostcpu, "MACH", False)
    assert hostcpu.mem_free() is None and hostcpu.mem_short() == "", "unreadable: nothing held"
    monkeypatch.setattr(hostcpu, "MACH", True)
    monkeypatch.setattr(hostcpu, "mach_mem_free", lambda: 0.3)
    assert hostcpu.mem_free() == 0.3, "no /proc/meminfo on macOS: the kernel's memory level"
    meminfo_file = tmp_path / "meminfo"
    meminfo_file.write_text(meminfo)
    monkeypatch.setattr(hostcpu, "MEMINFO", meminfo_file)
    assert hostcpu.mem_free() == 0.125, "/proc/meminfo first"


def test_disk_gate_reads_dirty_and_io_pressure_against_their_limits(env, tmp_path, monkeypatch):
    """diskgate: Dirty over checks.dirty_max_mb (default 2048) or io full avg60 over checks.io_full_max
    (default 10) holds new local runs, named with the measured values; no PSI skips only that signal;
    0 turns a signal off; the environment wins over the account setting; a bad value is the default."""
    from ttp import diskgate, project
    _fake_disk(monkeypatch, tmp_path, dirty_kb=100 * 1024, full60="0.40")
    r = diskgate.reading()
    assert r["held"] == "" and r["dirty_max_mb"] == 2048.0 and r["io_full_max"] == 10.0, r
    assert diskgate.line(r) == "disk: Dirty 0.1 GB (limit 2.0 GB), io full avg60 0.4% (limit 10%)"
    _fake_disk(monkeypatch, tmp_path, dirty_kb=int(7.6 * 1024 * 1024), full60="0.40")
    why = diskgate.saturated()
    assert why == "Dirty 7.6 GB over the 2.0 GB limit (checks.dirty_max_mb)", why
    assert diskgate.line().startswith("disk saturated: new local check runs held"), diskgate.line()
    _fake_disk(monkeypatch, tmp_path, dirty_kb=0, full60="24.50")
    assert diskgate.saturated() == "io pressure full avg60 24.5% over 10% (checks.io_full_max)"
    _fake_disk(monkeypatch, tmp_path, dirty_kb=int(7.6 * 1024 * 1024), full60=None)
    assert "Dirty 7.6 GB" in diskgate.saturated() and "io pressure" not in diskgate.saturated()
    assert diskgate.io_full_avg60() is None and "io full avg60 no PSI" in diskgate.line(), "no PSI: only it skipped"
    _fake_disk(monkeypatch, tmp_path, dirty_kb=0, full60=None)
    assert diskgate.saturated() == "", "no PSI and a clean page cache: nothing held"
    monkeypatch.setattr(diskgate, "MEMINFO", tmp_path / "none")
    assert diskgate.dirty_mb() is None and diskgate.saturated() == "", "nothing readable: no gate"
    _fake_disk(monkeypatch, tmp_path, dirty_kb=int(7.6 * 1024 * 1024), full60="50", dirty_max=0, io_max=0)
    assert diskgate.saturated() == "" and "limit off" in diskgate.line(), "0 turns each signal off"
    _fake_disk(monkeypatch, tmp_path, dirty_kb=3000 * 1024, full60="5")
    project.set_account_setting("checks.dirty_max_mb", 4096)
    project.set_account_setting("checks.io_full_max", 4)
    assert diskgate.saturated() == "io pressure full avg60 5.0% over 4% (checks.io_full_max)", "the account's limits"
    monkeypatch.setenv("TTP_CHECKS_DIRTY_MAX_MB", "1024")
    assert "Dirty 2.9 GB over the 1.0 GB limit" in diskgate.saturated(), "the environment wins"
    for bad in ("x", "-1", "inf", "nan", ""):
        monkeypatch.setenv("TTP_CHECKS_DIRTY_MAX_MB", bad)
        assert diskgate._limit(diskgate.DIRTY_KEY, 2048.0) == 4096.0, bad
    assert "checks.dirty_max_mb" in project.HOST_WIDE_KEYS and "io_full_max" in project.ACCOUNT_KEYS["checks"]


def test_a_saturated_disk_holds_new_check_slots_but_never_running_ones_or_push_priority(env, tmp_path, monkeypatch):
    """While the disk is saturated no new checks slot is taken, even with none running (unlike the cores
    floor); the wait says why and ends with a slot once the disk clears; a slot already held is kept,
    and the push branch's priority slot is not held."""
    import threading
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setattr(checkcap, "POLL_S", 0.02)
    _fake_disk(monkeypatch, tmp_path, dirty_kb=0, full60="0.1")
    running = checkcap.acquire("running")
    try:
        assert running.f is not None
        _fake_disk(monkeypatch, tmp_path, dirty_kb=0, full60="30")
        told = []
        assert checkcap.acquire("new", tell=told.append, max_wait_s=0.3) is None, "held while saturated"
        assert any("waiting for the disk: io pressure full avg60 30.0% over 10%" in x for x in told), told
        assert "running" in " ".join(checkcap.locks.held_labels(checkcap.all_paths(4))), "the running one keeps its slot"
        running.close()
        assert checkcap.acquire("new", max_wait_s=0.3) is None, "none running: still held"
        push = checkcap.acquire_priority("remote:branch", "push")
        assert push.f is not None, "push priority checks are not held"
        push.close()
        threading.Timer(0.3, lambda: _fake_disk(monkeypatch, tmp_path, dirty_kb=0, full60="0.1")).start()
        late = checkcap.acquire("new", max_wait_s=10)
        assert late is not None and late.f is not None, "admitted once the disk clears, no failure"
        late.close()
    finally:
        running.close()


def test_slot_status_shows_the_disk_gate_with_its_values(env, tmp_path, monkeypatch, capsys):
    import json as _json
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _fake_disk(monkeypatch, tmp_path, dirty_kb=3 * 1024 * 1024, full60=None)
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--slot-status"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert ("disk saturated: new local check runs held (spill hosts first, else they wait): Dirty 3.0 GB over "
            "the 2.0 GB limit (checks.dirty_max_mb); Dirty 3.0 GB (limit 2.0 GB), io full avg60 no PSI (limit 10%)") in out, out
    with pytest.raises(SystemExit):
        cli.main(["checks", "--slot-status", "--json"])
    disk = _json.loads(capsys.readouterr().out)["disk"]
    assert disk["dirty_mb"] == 3072.0 and disk["io_full_avg60"] is None and "Dirty 3.0 GB" in disk["held"], disk


def _waiting_precheck_lane(p, tid, head, queued, monkeypatch):
    """A lane record of the daemon's review precheck of task `tid`, waiting for a slot (no run_since)."""
    from ttp import checklane, testslots
    lane = Path("lane0")
    monkeypatch.setattr(testslots, "suite_bases", lambda name: ["b"])
    monkeypatch.setattr(testslots, "_lanes_of", lambda base: [lane])
    rec = {"pid": os.getpid(), "task": str(tid), "holder": f"run t{tid}-{head}", "since": time.time(), "queued": queued}
    return checklane, lane, rec


def test_a_waiting_precheck_of_a_finished_task_is_flagged_by_its_arrival_not_its_refreshed_since(env, monkeypatch):
    p = make(env)
    tid = p.db.add_task("t", "s", kind="code")
    head = "ab12cd34ef56"
    now = time.time()
    p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (now - 7200, tid))
    checklane, lane, rec = _waiting_precheck_lane(p, tid, head, now - 7000, monkeypatch)
    monkeypatch.setattr(checklane.locks, "holder_records", lambda paths: [rec])
    h = checklane.finished_holders(p, {}, now)
    assert h and h[0]["ended_s"] >= 6900 and h[0]["stop"], h   # waiting, not running: stopped without grace


def test_a_precheck_of_a_done_task_whose_review_is_open_is_kept(env, monkeypatch):
    p = make(env)
    tid = p.db.add_task("t", "s", kind="code")
    head = "ab12cd34ef56"
    now = time.time()
    p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (now - 7200, tid))
    p.db.add_task("review", "r", kind="review", labels=[f"precheck:t{tid}-{head}"])
    checklane, lane, rec = _waiting_precheck_lane(p, tid, head, now - 7000, monkeypatch)
    monkeypatch.setattr(checklane.locks, "holder_records", lambda paths: [rec])
    assert checklane.finished_holders(p, {}, now) == []


def test_urgent_first_puts_every_urgent_waiter_of_any_project_ahead_by_priority_then_arrival(tmp_path, monkeypatch):
    """#5893: under locks.URGENT_FIRST (the default checks.priority_turns) urgent checks of another project
    go ahead of older routine and aged waiters, the most urgent priority first, then by project turns."""
    from ttp import checkcap, locks, project
    d = tmp_path / "slots"
    for n, (label, g, pr) in enumerate([("b3", "B", 3), ("a2", "A", 2), ("c1", "C", 1), ("b0", "B", 0),
                                        ("a1", "A", 1), ("c4", "C", None)]):
        locks.enqueue(d, "r", label, since=1000.0 + n, group=g, priority=pr)
    names = lambda **k: [x.read_text() for x in locks.fair_order(d, "r", urgent_max=locks.URGENT_FIRST, **k)]
    assert names()[:3] == ["b0", "a1", "c1"], "priority 0 first, then the priority-1 waiters by project turn"
    assert names(aged_before=1001.5)[:3] == ["b0", "c1", "a1"], \
        "aged routine waiters stay behind urgent ones (project turns by each one's oldest waiter left: c1 now)"
    assert names(aged_before=1001.5)[3:5] == ["b3", "a2"], "then the aged ones by arrival"
    assert sorted(names()[3:]) == ["a2", "b3", "c4"]
    monkeypatch.delenv("TTP_CHECKS_PRIORITY_TURNS", raising=False)
    monkeypatch.setattr(project, "load_account_settings", lambda: {})
    assert checkcap.priority_turns() == checkcap.PRIORITY_TURNS == locks.URGENT_FIRST


def test_the_test_pool_host_line_serves_a_later_p1_of_another_project_before_an_earlier_p3(env, monkeypatch):
    """#5893: testslots.acquire's host line is locks.urgent_order: a priority-1 run that arrived later takes
    a free slot ahead of an earlier priority-3 waiter of another project; a later priority-3 run waits."""
    from ttp import checkcap, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: (2, 1))
    monkeypatch.setattr(testslots, "pool_now", lambda cfg, pool, held, *a, **k: pool)
    early = locks.enqueue(checkcap.slot_dir(), testslots.TESTS.resource, "c p3", time.time() - 600, group="C",
                          priority=3)
    try:
        assert testslots.acquire("b p3", project_name="B", priority=3, wait_s=0) is None, "behind the earlier p3"
        g = testslots.acquire("b p1", project_name="B", priority=1, wait_s=0)
        assert g is not None and g.k == 1, "the later p1 goes first"
        g.close()
    finally:
        locks.dequeue(early)


def test_pool_room_keeps_a_slot_for_an_urgent_run_of_another_project_parked_for_test_slots(env, monkeypatch):
    """#5893: testslots.pool_room leaves the pool's free slot to an urgent PARKED run of another project
    (urgent_parked): no room for a routine run or a later urgent one, room for a more urgent or older one."""
    from ttp import checkcap, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: (1, 1))
    monkeypatch.setattr(testslots, "pool_now", lambda cfg, pool, held, *a, **k: pool)
    assert testslots.pool_room({}, "A") and testslots.urgent_parked("A") == 0
    t0 = time.time() - 60
    parked = locks.enqueue(checkcap.slot_dir(), checkcap.PARKED, "B task #9 ttp checks", t0, group="B", priority=1)
    try:
        assert testslots.urgent_parked("A") == 1 and testslots.urgent_parked("B") == 0, "only other projects'"
        assert not testslots.pool_room({}, "A"), "a routine run leaves the slot to the parked urgent one"
        assert not testslots.pool_room({}, "A", priority=3)
        assert not testslots.pool_room({}, "A", priority=1), "a later run as urgent waits behind it"
        assert testslots.pool_room({}, "A", priority=1, since=t0 - 10), "an older urgent run goes first"
        assert testslots.pool_room({}, "A", priority=0), "a more urgent run goes first"
        assert testslots.pool_room({}, "B"), "that urgent run itself has room"
    finally:
        locks.dequeue(parked)
    routine = locks.enqueue(checkcap.slot_dir(), checkcap.PARKED, "B routine", t0, group="B", priority=3)
    try:
        assert testslots.pool_room({}, "A"), "a routine parked run keeps nothing"
    finally:
        locks.dequeue(routine)


def _parked_urgent_with_gate(gate: str, n: int = 3):
    """`n` priority-1 runs of project "pa" parked for their test slots, their lane's gate `gate` (gate_lanes)."""
    from ttp import checkcap, locks, testslots
    d, t0 = checkcap.slot_dir(), time.time() - 300
    tickets = [locks.enqueue(d, checkcap.PARKED, f"pa task #{i} ttp checks", t0 + i, group="pa", priority=1)
               for i in range(n)]
    lane = locks.try_take([d / f"{testslots.SUITE}.pa.repo.0.lock"], "pa task #0 ttp checks", "ttp checks",
                          extra={"gate": gate})
    return tickets, lane


def test_an_urgent_parked_run_its_own_gate_holds_keeps_no_test_pool_slots(env, monkeypatch):
    """#7029: pool_gate keeps pool slots only for urgent parked runs of other projects that could start: one
    its project's aged turn, budget or the load guard holds keeps none, so another project's p1 run starts
    on the free slots (8 of 16 free, a run needs 4; 12 were kept for 3 such runs before)."""
    from ttp import locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: (16, 8))
    monkeypatch.setattr(testslots, "pool_now", lambda cfg, pool, held, *a, **k: pool)
    busy = [locks.try_take(testslots.paths(16), f"pc task #{i} ttp checks", "pytest") for i in range(8)]
    for gate in ("46" + testslots.STARVED_GATE,
                 testslots.BUDGET_GATE + "0 of 8 free, a run needs 4 (held by pa task #9)",
                 testslots.LOAD_GATE + "the host is overloaded"):
        tickets, lane = _parked_urgent_with_gate(gate)
        try:
            assert testslots.urgent_parked("pb", 1) == 0, gate
            assert testslots.pool_gate({}, "pb", full=True, priority=1) == "", gate
            assert testslots.pool_gate({}, "pb", full=True, priority=3) == "", "nor for a routine run"
        finally:
            lane.close()
            for t in tickets:
                locks.dequeue(t)
    for f in busy:
        f.close()


def test_an_urgent_parked_run_that_could_start_still_gets_the_freed_slot_ahead_of_a_routine_run(env, monkeypatch):
    """#7029: a parked urgent run held only by the pool (or naming no reason yet) still counts toward the
    reserve: another project's routine run leaves the free slot to it."""
    from ttp import locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: (16, 8))
    monkeypatch.setattr(testslots, "pool_now", lambda cfg, pool, held, *a, **k: pool)
    busy = [locks.try_take(testslots.paths(16), f"pc task #{i} ttp checks", "pytest") for i in range(12)]
    for gate in ("the host's test pool: 4 of 16 free, a run needs 4, 4 kept for 1 urgent run(s) of other projects"
                 " parked ahead", ""):
        tickets, lane = _parked_urgent_with_gate(gate, n=1)
        try:
            assert testslots.urgent_parked("pb") == 1, gate
            why = testslots.pool_gate({}, "pb", full=True, priority=3)
            assert "4 kept for 1 urgent run(s)" in why, why
            assert testslots.pool_gate({}, "pa", full=True, priority=1) == "", "the urgent run itself has room"
        finally:
            lane.close()
            for t in tickets:
                locks.dequeue(t)
    for f in busy:
        f.close()

def test_an_urgent_parked_run_whose_project_budget_is_full_keeps_no_test_pool_slots(env, monkeypatch):
    """#7029: whatever its lane's gate names, a project counts toward the pool reserve at most the urgent runs
    its free test-worker budget could start now: a full budget keeps no pool slots, so another project's p1
    run starts on the free ones; urgent slots open while only less urgent runs hold it count as pool_gate's."""
    from ttp import checkcap, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: (16, 8))
    monkeypatch.setattr(testslots, "pool_now", lambda cfg, pool, held, *a, **k: pool)
    cfg = {"checks": {"test_project_slots": 8}}
    busy = [locks.try_take(testslots.paths(16), f"pc task #{i} ttp checks", "pytest") for i in range(8)]
    own = testslots.own_paths("pa", 8)
    for rank, held, counted in ((1, 8, 0), (1, 4, 1), (3, 8, 2), (1, 0, 2)):
        holders = [locks.try_take(own, f"pa task #{90 + i} ttp checks", "pytest", extra={"priority": rank})
                   for i in range(held)]
        tickets, lane = _parked_urgent_with_gate("", n=3)
        try:
            assert testslots.urgent_parked("pb", 1, budget=8, need=4, run_max=8) == counted, (rank, held)
            assert testslots.urgent_parked("pb", 1) == 3, "without a budget every one counts"
            why = testslots.pool_gate(cfg, "pb", full=True, priority=1)
            assert (why == "") == (counted <= 1), (rank, held, why)
        finally:
            lane.close()
            for t in tickets:
                locks.dequeue(t)
            for f in holders:
                f.close()
    for f in busy:
        f.close()


def test_an_urgent_parked_run_that_leaves_free_pool_room_untaken_keeps_no_slots_after_untaken_s(env, monkeypatch):
    """#7029: a parked urgent run of another project the pool's free room fits, still parked UNTAKEN_S later
    (some hold its lane names no reason for keeps it off), keeps no pool slots, so a routine run starts on
    them; one the room does not fit, or once the room is gone, starts its count again."""
    from ttp import locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setattr(testslots, "_ROOM_SINCE", {})
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: (16, 8))
    monkeypatch.setattr(testslots, "pool_now", lambda cfg, pool, held, *a, **k: pool)
    clock = [1000.0]
    monkeypatch.setattr(testslots.time, "time", lambda: clock[0])
    busy = [locks.try_take(testslots.paths(16), f"pc task #{i} ttp checks", "pytest") for i in range(12)]
    tickets, lane = _parked_urgent_with_gate("", n=2)
    try:
        assert testslots.urgent_parked("pb", need=4, room=4) == 2
        assert "8 kept for 2 urgent run(s)" in testslots.pool_gate({}, "pb", full=True, priority=3)
        clock[0] += testslots.UNTAKEN_S
        assert testslots.urgent_parked("pb", need=4, room=4) == 1, "the oldest one the room fits: dropped"
        assert testslots.urgent_parked("pb", need=4, room=3) == 2, "no room: every count starts again"
        assert testslots.urgent_parked("pb", need=4, room=4) == 2
        clock[0] += testslots.UNTAKEN_S - 1
        assert testslots.urgent_parked("pb", need=4, room=4) == 2, "not yet"
        clock[0] += 1
        assert testslots.urgent_parked("pb", need=4, room=8) == 1, "the room fits the second only from now"
        clock[0] += testslots.UNTAKEN_S
        assert testslots.urgent_parked("pb", need=4, room=8) == 0
    finally:
        lane.close()
        for t in tickets:
            locks.dequeue(t)
        for f in busy:
            f.close()


def test_upgraded_names_a_runtime_tree_changed_on_disk_without_a_version_bump(env, tmp_path, monkeypatch):
    """#7029: a waiting detached `ttp checks` re-executes once its runtime's code changes on disk (a fix
    landing in place, version unchanged), once settled, and never again for the same tree."""
    import ttp
    from ttp import checkcap
    monkeypatch.setattr(checkcap, "_SEEN", {})
    monkeypatch.setattr(checkcap, "_TREE0", {})
    monkeypatch.setattr(checkcap, "_TREE_AT", {})
    monkeypatch.setenv(checkcap.REEXEC_ENV, "")
    init, mod = tmp_path / "__init__.py", tmp_path / "gate.py"
    init.write_text(f'__version__ = "{ttp.__version__}"\n')
    mod.write_text("A = 1\n")
    assert checkcap.upgraded(init, now=100.0) == "", "the tree it imported"
    mod.write_text("A = 22\n")
    os.utime(mod, ns=(1, 1))
    assert checkcap.upgraded(init, now=200.0) == "", "just changed: other files may still be copying"
    new = checkcap.upgraded(init, now=200.0 + checkcap.UPGRADE_SETTLE_S)
    assert new == f"{ttp.__version__}+{checkcap._tree(tmp_path)}", new
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "gate.py").write_text("compiled\n")
    assert checkcap.upgraded(init, now=300.0) == new, "byte code is not code"
    monkeypatch.setenv(checkcap.REEXEC_ENV, new)
    assert checkcap.upgraded(init, now=9999.0, settle=0) == "", "re-executed for it once already: never a loop"


def test_upgraded_reads_the_runtime_tree_at_most_once_per_poll_interval(env, tmp_path, monkeypatch):
    """#7116: a waiter asks upgraded() every second or less; the runtime tree on disk is read (one stat per
    file) only once TREE_POLL_S passed since its last read, and a change is still seen at the next read."""
    import ttp
    from ttp import checkcap
    monkeypatch.setattr(checkcap, "_SEEN", {})
    monkeypatch.setattr(checkcap, "_TREE0", {})
    monkeypatch.setattr(checkcap, "_TREE_AT", {})
    monkeypatch.setenv(checkcap.REEXEC_ENV, "")
    init, mod = tmp_path / "__init__.py", tmp_path / "gate.py"
    init.write_text(f'__version__ = "{ttp.__version__}"\n')
    mod.write_text("A = 1\n")
    real, reads = checkcap._tree, []
    monkeypatch.setattr(checkcap, "_tree", lambda pkg: reads.append(pkg) or real(pkg))
    for i in range(20):   # 20 polls in under one interval: the import tree and one read of the disk
        assert checkcap.upgraded(init, now=100.0 + i * 0.5, settle=0) == ""
    assert len(reads) == 2, reads
    mod.write_text("A = 22\n")
    os.utime(mod, ns=(1, 1))
    assert checkcap.upgraded(init, now=100.0 + checkcap.TREE_POLL_S - 1, settle=0) == "", "not read again yet"
    assert len(reads) == 2
    new = checkcap.upgraded(init, now=100.0 + checkcap.TREE_POLL_S, settle=0)
    assert new == f"{ttp.__version__}+{real(tmp_path)}" and len(reads) == 3, (new, reads)


def test_a_waiting_detached_checks_run_reexecutes_for_a_runtime_tree_change_and_keeps_its_place(
        env, tmp_path, monkeypatch):
    """#7116: a detached `ttp checks` parked for a host-wide slot stays put while its runtime tree is
    unchanged, and re-executes once a runtime file changes with the version unchanged (a project-local fix),
    keeping its lane and its arrival time, so it queues again at its old place."""
    import threading
    import ttp
    from ttp import checkcap, cli, locks
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (init := pkg / "__init__.py").write_text(f'__version__ = "{ttp.__version__}"\n')
    (mod := pkg / "gate.py").write_text("A = 1\n")
    for k, v in (("_SEEN", {}), ("_TREE0", {}), ("_TREE_AT", {}), ("TREE_POLL_S", 0.0)):
        monkeypatch.setattr(checkcap, k, v)
    real, asked = checkcap.upgraded, []
    execs = _upgrade_child_checks(monkeypatch, lambda: asked.append(1) or real(init, settle=0))
    arrived = time.time() - 600
    monkeypatch.setenv(checkcap.ARRIVED_ENV, repr(arrived))
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    held = checkcap.acquire("other")
    ran, ended = tmp_path / "ran", []

    def go():
        try:
            cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
        except _ReExec:
            ended.append("exec")
    t = threading.Thread(target=go)
    t.start()
    try:
        marker = "queued for a host-wide checks slot"
        assert _wait_for(lambda: marker in ((run / "checks.log").read_text() if (run / "checks.log").exists() else ""))
        n = len(asked)
        assert _wait_for(lambda: len(asked) >= n + 3), "it keeps asking while it waits"
        assert not execs and t.is_alive(), "an unchanged runtime tree: no re-exec"
        mod.write_text("A = 22\n")
        os.utime(mod, ns=(1, 1))
        t.join(30)
        assert not t.is_alive() and ended == ["exec"] and not ran.exists()
        (path, argv, envs, lanes), = execs
        assert envs[checkcap.REEXEC_ENV] == f"{ttp.__version__}+{checkcap._tree(pkg)}", envs
        assert lanes == [(True, True, os.getpid())], "its lane is kept for the new process"
        assert not locks.queued(checkcap.slot_dir(), checkcap.RESOURCE), "its queue ticket goes with it"
        assert envs[checkcap.ARRIVED_ENV] == repr(arrived) and cli._checks_arrived() == arrived, "its place"
    finally:
        held.close()
        t.join(30)


def test_a_routine_run_leaves_freed_test_slots_to_a_parked_urgent_run_of_its_own_project(env, monkeypatch):
    """#7124 (a, c): a routine run leaves a run's worth of free test slots to a priority-1 run of its own
    project parked for them (own_urgent_parked; a priority-0 one holds it through checkcap.owed_ahead), though
    that run arrived later and is under starve_s; its pool_gate names the reserve and the urgent run takes the
    slots. With no urgent run of its project parked, or only one that left the room untaken UNTAKEN_S, the
    routine run is not held."""
    from ttp import checkcap, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: (1, 1))   # one run's worth free
    monkeypatch.setattr(testslots, "pool_now", lambda cfg, pool, held, *a, **k: pool)
    monkeypatch.setattr(testslots, "_ROOM_SINCE", {})
    monkeypatch.setattr(testslots, "_OWN_ROOM_SINCE", {})
    now = time.time()
    assert checkcap.starve_s() > 120
    routine = locks.enqueue(checkcap.slot_dir(), checkcap.PARKED, "A task #1 ttp checks", now - 120, group="A",
                            priority=3)
    try:
        assert testslots.pool_gate({}, "A", priority=3, since=now - 120) == "", "(c) no urgent run of A parked"
        urgent = locks.enqueue(checkcap.slot_dir(), checkcap.PARKED, "A task #2 ttp checks", now - 60, group="A",
                               priority=1)
        try:
            why = testslots.pool_gate({}, "A", priority=3, since=now - 120)
            assert testslots.OWN_RESERVE in why, why
            assert testslots.OWN_RESERVE not in testslots.pool_gate({}, "B", priority=3), "only its own project's"
            assert testslots.pool_room({}, "A", priority=1, since=now - 60), "the urgent run has room"
            g = testslots.acquire("A task #2 ttp checks", project_name="A", priority=1, wait_s=0)
            assert g is not None and g.k >= 1, "the urgent run takes the freed slots"
            g.close()
            monkeypatch.setattr(testslots, "UNTAKEN_S", 0.0)
            assert testslots.pool_gate({}, "A", priority=3, since=now - 120) == "", \
                "one that leaves the room untaken keeps none"
        finally:
            locks.dequeue(urgent)
        assert testslots.pool_gate({}, "A", priority=3, since=now - 120) == ""
    finally:
        locks.dequeue(routine)


def test_own_urgent_parked_and_urgent_parked_keep_separate_untaken_counts(env, monkeypatch):
    """#7124 with #7029: own_urgent_parked (its project's parked urgent runs) and urgent_parked (other
    projects') count UNTAKEN_S in separate records, so one pool_gate poll calling both never restarts the
    other's count: an own urgent run that left the room untaken UNTAKEN_S keeps no slots though an urgent
    run of another project is counted in between."""
    from ttp import checkcap, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setattr(testslots, "_ROOM_SINCE", {})
    monkeypatch.setattr(testslots, "_OWN_ROOM_SINCE", {})
    clock = [1000.0]
    monkeypatch.setattr(testslots.time, "time", lambda: clock[0])
    d = checkcap.slot_dir()
    own = locks.enqueue(d, checkcap.PARKED, "A task #2 ttp checks", clock[0] - 60, group="A", priority=1)
    other = locks.enqueue(d, checkcap.PARKED, "B task #3 ttp checks", clock[0] - 60, group="B", priority=1)
    try:
        assert testslots.own_urgent_parked("A", 3, fits=1) == 1
        clock[0] += testslots.UNTAKEN_S
        assert testslots.urgent_parked("A", 3, need=1, room=1) == 1, "the other project's run: its count starts"
        assert testslots.own_urgent_parked("A", 3, fits=1) == 0, "the own run's count was not restarted"
        assert testslots.urgent_parked("A", 3, need=1, room=1) == 1, "nor the other's by the own count"
    finally:
        locks.dequeue(own)
        locks.dequeue(other)


def test_a_parked_urgent_run_whose_ask_lapsed_asks_again_and_takes_the_routine_holders_slot(env, monkeypatch):
    """#7124 (b): a parked urgent run asked a routine holder that did not stop; once its claim lapsed (no live
    request), it asks again preempt_after_s() later, never while the first request lives, and takes the slot
    when that run lets go. One wasted ask no longer ends preemption for the rest of its wait."""
    import threading
    from ttp import checkcap
    _preempt_cap_of_one(monkeypatch)
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "1")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.1")
    monkeypatch.setattr(checkcap, "CLAIM_S", 0.5)
    monkeypatch.setattr(checkcap, "PARKED_CLAIM_S", 0.5)
    victim = checkcap.acquire("routine 5", priority=5, preemptible=True)
    freed = [False]   # the victim's test slots: free once it stops (testslots counts them so under FREEING)
    told, got, asked_at = [], [], []

    def tell(line):
        told.append(line)
        if line == checkcap.preempt_line("routine 5"):
            asked_at.append(time.time())
    t = threading.Thread(target=lambda: got.append(checkcap.acquire(
        "urgent", priority=0, urgent=True, tell=tell, max_wait_s=30,
        startable=lambda: checkcap.FREEING.get() is not None or freed[0])))
    t.start()
    try:
        assert _wait_for(lambda: len(asked_at) >= 2, 15), told
        assert asked_at[1] - asked_at[0] >= checkcap.PARKED_CLAIM_S + 1.0 - 0.2, \
            "asked again only once the first request lapsed and preempt_after_s passed"
        assert checkcap.preempted(victim.since) == "urgent urgent", "the second request is live"
        freed[0] = True
        victim.close()
        gone = time.time()
        t.join(30)
        assert got and got[0] is not None and got[0].f is not None
        assert time.time() - gone < 3.0
    finally:
        checkcap.clear_preempt()
        victim.close()
        t.join(30)
        got and got[0] and got[0].close()


def test_a_checks_wait_ends_once_its_task_moves_the_branch_off_the_head_and_never_on_a_detached_head(tmp_path):
    """#5893: cli._HeadMoved (asked by _checks_slot_take's stop) is "" while the branch tip is the head,
    the reason once a new commit moves it, and "" for a wait that began on a detached HEAD."""
    from ttp import cli
    repo = tmp_path / "r"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e"]
    subprocess.run(["git", "init", "-q", "-b", "work", str(repo)], check=True)
    (repo / "a").write_text("1\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "a"], check=True)
    rev = lambda: subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    head = rev()
    moved = cli._HeadMoved(str(repo), head)
    assert moved() == ""
    (repo / "a").write_text("2\n")
    subprocess.run([*git, "commit", "-qam", "b"], check=True)
    assert moved() == "", "asked again within HEAD_MOVED_S: git is not read"
    moved.at = 0
    why = moved()
    assert head[:12] in why and rev()[:12] in why and "on work" in why, why
    assert cli._HeadMoved(str(repo), head)() == "", "a wait that began off its head never ends this way"
    subprocess.run([*git, "checkout", "-q", "--detach"], check=True)
    detached = cli._HeadMoved(str(repo), rev())
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "c"], check=True)
    detached.at = 0
    assert detached() == "", "a detached HEAD never ends a wait"


def test_an_urgent_check_waiting_ten_minutes_raises_one_urgent_check_wait_event(env, monkeypatch, tmp_path):
    """#5893: an urgent checks run of this project waiting checkcap.URGENT_WAIT_S in the host-wide checks
    queue raises one urgent_check_wait event naming the queue, and none again on a later tick."""
    from ttp import checkcap, daemon as dm, locks
    p = make(env)
    sd = tmp_path / "check-slots"
    monkeypatch.setattr(checkcap, "slot_dir", lambda: sd)
    t0 = time.time()
    tickets = [locks.enqueue(sd, checkcap.RESOURCE, f"{p.name} task #42 (run 7) ttp checks", t0 - 700,
                             group=p.name, priority=1),
               locks.enqueue(sd, checkcap.RESOURCE, f"{p.name} task #43 ttp checks", t0 - 700, group=p.name,
                             priority=3),
               locks.enqueue(sd, checkcap.RESOURCE, "other task #5 ttp checks", t0 - 700, group="other", priority=1),
               locks.enqueue(sd, checkcap.RESOURCE, f"{p.name} task #44 ttp checks", t0 - 60, group=p.name,
                             priority=1)]
    events = lambda: p.db.q("SELECT text, task, status FROM events WHERE kind=?", (checkcap.URGENT_WAIT,))
    try:
        d = dm.Daemon(p.base)
        d.check_urgent_check_wait()
        got = events()
        assert len(got) == 1 and got[0]["task"] == 42 and got[0]["status"] == "queued", got
        assert "host-wide checks queue" in got[0]["text"] and "12 min" in got[0]["text"], got[0]["text"]
        d._urgent_check_wait_checked = 0
        d.check_urgent_check_wait()
        dm.Daemon(p.base).check_urgent_check_wait()
        assert len(events()) == 1, "one event per wait"
    finally:
        for t in tickets:
            locks.dequeue(t)


def test_a_spilled_pass_is_reused_by_a_local_run_of_the_same_tree_but_another_host_s_is_not(env):
    # A review's pre-started checks queued a full run for a tree whose code-task checks had passed:
    # the pass was spilled to another host, recorded under that host's key, and missed here.
    from ttp import cli, push
    p, tree = make(env), "t" * 40
    local = push.check_list(["pytest -q"])
    spilled = cli._spill_to(local, "spill-a")
    assert push.remote_host(spilled[0]) == "spill-a", "it still runs on the spill host"
    cli._record_pass(p, tree, spilled)
    assert cli._recorded_pass(p, tree, local), "a local run of the same tree did not reuse the spilled pass"
    # A check run on a configured host (its own or delivery.checks_host) stays that host's pass.
    for there in (push.check_list([{"run": "pytest -q", "host": "box-a"}]), push.with_host(local, "box-a")):
        assert cli._command_key(there[0]) != cli._command_key(local[0])
        cli._record_pass(p, "u" * 40, there)
        assert not cli._recorded_pass(p, "u" * 40, local), there


def _carried_setup(env, monkeypatch, upstream: dict):
    """A change branch whose checks passed at H1 (recorded with its carry ref), then `upstream` landed
    on main and the branch was rebased to H2: (project, repo, cmds, H2, tree of H2)."""
    from ttp import cli, push
    for var, val in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                     ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(var, val)
    p = make(env)
    p.set_config("delivery.push_exclude_paths", ["notes/**"])
    repo = env["tmp"] / "carried"
    repo.mkdir()
    _git_out(repo, "init", "-q", "-b", "main")

    def commit(files, msg):
        for path, text in files.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(text)
        _git_out(repo, "add", "-A")
        _git_out(repo, "commit", "-qm", msg)
        return _git_out(repo, "rev-parse", "HEAD")

    commit({"src/a.py": "a = 1\n", "notes/n.md": "n\n"}, "base")
    _git_out(repo, "checkout", "-q", "-b", "change")
    h1 = commit({"src/a.py": "a = 2\n"}, "change")
    cmds = push.check_list(["pytest -q"])
    cli._record_pass(p, _git_out(repo, "rev-parse", "HEAD^{tree}"), cmds, at=cli._carry_ref(repo, h1, "main"))
    _git_out(repo, "checkout", "-q", "main")
    commit(upstream, "upstream")
    _git_out(repo, "checkout", "-q", "change")
    _git_out(repo, "rebase", "-q", "main")
    return p, repo, cmds, _git_out(repo, "rev-parse", "HEAD"), _git_out(repo, "rev-parse", "HEAD^{tree}")


def test_a_pass_carries_to_a_rebased_head_whose_delta_its_checks_do_not_cover(env, monkeypatch):
    from ttp import cli
    p, repo, cmds, h2, tree = _carried_setup(env, monkeypatch, {"notes/n.md": "m\n"})
    assert not cli._recorded_pass(p, tree, cmds), "another tree"
    hit = cli._carried_pass(p, repo, h2, tree, cmds, "main")
    assert hit and hit["carried"].endswith("upstream delta touched only: notes/n.md"), hit
    # Nothing carries to a detached HEAD, a branch other than the pass's, or another purpose.
    assert cli._carried_pass(p, repo, h2, tree, cmds, "main", purpose="final") is None
    _git_out(repo, "checkout", "-q", "--detach")
    assert cli._carried_pass(p, repo, h2, tree, cmds, "main") is None
    _git_out(repo, "checkout", "-q", "-b", "other")
    assert cli._carried_pass(p, repo, h2, tree, cmds, "main") is None


def test_a_rebase_whose_delta_its_checks_cover_runs_them_again(env, monkeypatch):
    from ttp import cli
    p, repo, cmds, h2, tree = _carried_setup(env, monkeypatch, {"src/b.py": "b = 1\n"})
    assert cli._carried_pass(p, repo, h2, tree, cmds, "main") is None


def test_an_urgent_review_held_by_its_pre_checks_names_the_host_checks_slot_wait(env, monkeypatch):
    # The urgent-wait observation said "a disk guard or its review's pre-checks"; it names the real hold.
    from ttp import checkcap, cli
    from ttp.daemon import PRECHECK_LABEL, PRECHECKS_DIR, Daemon, precheck_run_id
    p = make(env)
    d = Daemon(p.base)
    out = p.state / PRECHECKS_DIR / f"t7-{'a' * 12}"
    out.mkdir(parents=True)
    rid = p.db.add_task("review", "s", kind="review", tier="light", origin="user", priority=1,
                        labels=[f"{PRECHECK_LABEL}{out.name}"])
    task = p.db.task(rid)
    label = f"{p.name} task #7 (run {precheck_run_id(out)})"
    monkeypatch.setattr(checkcap, "state", lambda: {"queued": ["x", "y", label], "running": ["r"] * 4,
                                                    "parked": []})
    monkeypatch.setattr(d, "_branch_clash", lambda *a: None)
    monkeypatch.setattr(d, "_disk_holds", lambda t: False)
    monkeypatch.setattr(d, "_precheck_holds", lambda t, now: True)
    (out / cli.CHECKS_SLOT_WAIT).write_text(json.dumps({"since": time.time() - 600, "pid": 1}))
    ctx = {"now": time.time(), "paused": {}, "busy": {}, "committed": None, "logged_out": {}, "live": []}
    assert d._start_hold(task, "claude", ctx) == "held"
    words = d._hold_words(task, "held", ctx)
    assert words.endswith("waiting 10 min for a host checks slot, 2 ahead (4 running)"), words
    assert "disk guard" not in words
    (out / cli.CHECKS_SLOT_WAIT).unlink()
    d._start_hold(task, "claude", ctx)
    assert d._hold_words(task, "held", ctx).endswith(", running")
    monkeypatch.setattr(d, "_disk_holds", lambda t: True)
    d._start_hold(task, "claude", ctx)
    assert d._hold_words(task, "held", ctx).startswith("the disk guard (")


def test_an_urgent_run_waiting_in_two_queues_raises_one_urgent_check_wait_event(env, monkeypatch, tmp_path):
    """#6004: one urgent run parked 10+ minutes, then waiting in the checks queue (its ticket keeps the same
    arrival, group and priority), raises one urgent_check_wait event over both ticks, not one per queue."""
    from ttp import checkcap, daemon as dm, locks
    p = make(env)
    sd = tmp_path / "check-slots"
    monkeypatch.setattr(checkcap, "slot_dir", lambda: sd)
    since = time.time() - 700
    holder = f"{p.name} task #42 (run 7) ttp checks"
    events = lambda: p.db.q("SELECT text, task FROM events WHERE kind=?", (checkcap.URGENT_WAIT,))
    parked = locks.enqueue(sd, checkcap.PARKED, holder, since, group=p.name, priority=1)
    queued = None
    try:
        d = dm.Daemon(p.base)
        d.check_urgent_check_wait()
        got = events()
        assert len(got) == 1 and got[0]["task"] == 42 and f"host-wide {checkcap.PARKED} queue" in got[0]["text"], got
        locks.dequeue(parked)
        parked = None
        queued = locks.enqueue(sd, checkcap.RESOURCE, holder, since, group=p.name, priority=1)
        assert [w["queue"] for w in checkcap.urgent_waits(p.name)] == [checkcap.RESOURCE]
        d._urgent_check_wait_checked = 0
        d.check_urgent_check_wait()
        assert len(events()) == 1, ("one event per urgent run, not one per queue", events())
    finally:
        for t in (parked, queued):
            if t:
                locks.dequeue(t)


def test_an_urgent_wait_under_the_load_guard_names_it_and_its_pass_holders_not_the_pools(env, monkeypatch):
    """#6556: an urgent run parked 10+ minutes while the host load guard holds it (the pool has free slots,
    every load pass it may take is held) is named as held by the load guard, with the load and the passes'
    holders (project, task, run), not the test pool's slot holders; once a pass frees and the guard no
    longer holds it, the pool's holders are named again."""
    from ttp import checkcap, locks, testslots
    _host_of_64(monkeypatch, load=84.5)
    d = checkcap.slot_dir()
    slot = locks.try_take(testslots.paths(32), "proj-x task #9 (run 3) ttp checks", "test slots")
    passes = [locks.try_take([d / f"{n}.lock"], f"proj-{i} task #7{i} (run 5{i}) ttp checks", "load-guard pass")
              for i, n in enumerate((testslots.LOAD_URGENT, testslots.LOAD_PASS, testslots.LOAD_FLOOR))]
    parked = locks.enqueue(d, checkcap.PARKED, "pa task #42 (run 7) ttp checks", time.time() - 700, group="pa",
                           priority=1)
    try:
        [w] = checkcap.urgent_waits("pa")
        assert "84.5" in w["load"] and "64 cores" in w["load"], w
        assert w["held_by"] == [f"proj-{i} task #7{i} (run 5{i}) ttp checks" for i in range(3)], w
        text = checkcap.urgent_wait_text(w)
        assert "the host load guard (" in text and "proj-0 task #70 (run 50)" in text, text
        assert "proj-x task #9" not in text, "free slots: the pool's holders are not the block"
        passes.pop(1).close()   # the shared pass frees: the guard no longer holds it
        [w] = checkcap.urgent_waits("pa")
        assert "load" not in w and w["held_by"] == ["proj-x task #9 (run 3) ttp checks"], w
        assert "load guard" not in checkcap.urgent_wait_text(w)
    finally:
        locks.dequeue(parked)
        for f in (slot, *passes):
            f.close()


def _pool_folder(pool: Path, held: bool) -> Path:
    """A pool run's folder made by remotecheck's own command, run here as its ssh runs it on the host, by a
    run that holds a checks slot at its origin (`held`) or not."""
    from ttp import checkcap, remotecheck
    env = {k: v for k, v in os.environ.items() if k != checkcap.HELD_ENV} | {"TMPDIR": str(pool)}
    env |= {checkcap.HELD_ENV: "1"} if held else {}
    r = subprocess.run(["sh", "-c", remotecheck.mktemp_cmd(env)], env=env, capture_output=True, text=True, check=True)
    made = Path(r.stdout.strip())
    assert made.is_dir() and remotecheck._TMP.fullmatch(str(made)), (r.stdout, "its cleanup's pattern")
    return made


def test_a_pool_run_on_the_host_takes_one_of_its_checks_slots_and_one_holding_a_slot_here_counts_once(env, monkeypatch):
    """A check another `ttp checks` runs on this host (a project's own pool, a spill or check host: a
    ttp-check.* folder) takes a host slot, so it queues with the checks started here; a run whose
    `ttp checks` holds a slot here already (its folder carries this host's tag) is not counted again."""
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    pool = Path(os.environ["TTP_CHECKS_POOL_DIRS"])
    pool.mkdir(parents=True, exist_ok=True)
    _pool_folder(pool, held=True)
    s = checkcap.acquire("own", max_wait_s=2)
    assert s is not None and s.f is not None, "a run already holding a slot here is not counted twice"
    s.close()
    untagged = _pool_folder(pool, held=False)
    assert checkcap.pool_runs() == [untagged] and checkcap.state()["pool"] == 1
    assert checkcap.acquire("blocked", max_wait_s=1) is None, "the pool run holds the host's one slot"
    old = time.time() - checkcap.POOL_MAX_AGE_S - 60
    os.utime(untagged, (old, old))
    assert checkcap.pool_runs() == [], "a leftover folder past the age bound is not a running check"
    s = checkcap.acquire("next", max_wait_s=2)
    assert s is not None and s.f is not None
    s.close()


def test_a_pool_run_whose_script_lives_counts_though_no_process_works_in_its_folder(env, monkeypatch, tmp_path):
    """Past POOL_FRESH_S, a pool folder counts while a process works in it or while the run script whose
    pid it keeps (remotecheck.RUN_PID) lives, also when that run's processes all work elsewhere."""
    from ttp import checkcap, remotecheck
    pool = Path(os.environ["TTP_CHECKS_POOL_DIRS"])
    pool.mkdir(parents=True, exist_ok=True)
    f = _pool_folder(pool, held=False)
    (f / ".git").mkdir(exist_ok=True)
    run = subprocess.Popen(["sleep", "60"], cwd=tmp_path)
    try:
        (f / remotecheck.RUN_PID).write_text(f"{run.pid}\n")
        old = time.time() - checkcap.POOL_FRESH_S - 60
        os.utime(f, (old, old))
        assert checkcap.pool_runs() == [f]
    finally:
        run.kill()
        run.wait()
    if os.path.isdir("/proc/self"):
        assert checkcap.pool_runs() == [], "its script gone and nothing working in it: no running check"


def test_remote_check_folder_names_the_origin_only_when_its_run_holds_a_slot_there(env):
    """remotecheck's folder carries the origin's host tag only for a run that holds a slot there: pool_runs
    counts the untagged folder and not the tagged one."""
    from ttp import checkcap
    pool = Path(os.environ["TTP_CHECKS_POOL_DIRS"])
    pool.mkdir(parents=True, exist_ok=True)
    tagged, untagged = _pool_folder(pool, held=True), _pool_folder(pool, held=False)
    assert tagged.name.startswith(f"ttp-check.{checkcap.host_tag()}")
    assert not untagged.name.startswith(f"ttp-check.{checkcap.host_tag()}")
    assert checkcap.pool_runs() == [untagged]


def test_a_wrapped_pool_shard_takes_a_host_slot_and_one_inside_a_held_slot_counts_once(env, monkeypatch, tmp_path):
    """A shard a checks.wrap_cmd pool (or its local fallback) starts here with `ttp checks-shard` takes the
    host's checks slot; inside a `ttp checks` that holds the slot its first shard runs on that slot (counted
    once) and a further one at the same time counts as a pool run, which other checks then wait behind."""
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    for k in (checkcap.HELD_ENV, checkcap.SLOT_KEY_ENV, checkcap.SHARD_ENV):
        monkeypatch.delenv(k, raising=False)
    seen = tmp_path / "seen"
    probe = (f"{sys.executable} -c 'from ttp import checkcap, locks; "
             f"print(len(locks.held_labels(checkcap.all_paths())), len(checkcap.pool_runs()))' >> {seen}")
    wrapper = tmp_path / "wrap.sh"   # a stub checks.wrap_cmd: its local fallback runs the command as one shard here
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m ttp checks-shard -- "$@"\n')
    wrapper.chmod(0o755)

    def shard(cmd: str, extra: dict | None = None) -> int:
        r = subprocess.run([str(wrapper), "sh", "-c", cmd], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "PYTHONPATH": str(RUNTIME), **(extra or {})})
        return r.returncode

    def last() -> str:
        return seen.read_text().splitlines()[-1]

    assert shard(probe) == 0 and last() == "1 0", "a shard on its own takes the host's slot"
    assert shard("exit 3") == 3, "the shard's exit code passes through"
    assert not locks.held_labels(checkcap.all_paths()), "the shard's slot ends with it"
    s = checkcap.acquire("own", max_wait_s=2)
    assert s is not None and s.f is not None
    assert shard(probe, s.env()) == 0 and last() == "1 0", "the run's first shard runs on its slot: counted once"
    for k, v in s.env().items():
        monkeypatch.setenv(k, v)
    with checkcap.shard("first shard"):
        assert checkcap.pool_runs() == []
        assert shard(probe, s.env()) == 0 and last() == "1 1", "a second shard at once counts as a pool run"
        with checkcap.shard("second shard"):
            assert len(checkcap.pool_runs()) == 1 and checkcap.state()["pool"] == 1
            monkeypatch.delenv(checkcap.HELD_ENV)
            monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
            assert checkcap.acquire("blocked", max_wait_s=1) is None, "the extra shard holds the host's second slot"
            monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
            monkeypatch.setenv(checkcap.HELD_ENV, "1")
    assert checkcap.pool_runs() == [], "an ended shard is no longer counted"
    s.close()


def test_coordinator_sets_the_account_checks_host_cap_logged_with_its_setter_and_undo(env, capsys):
    """config_set of checks.host_max applies account-wide, is chat-settable, and logs who, old, new and
    the undo; the value the user set is then refused to the coordinator and to a worker's queue."""
    import json as _json
    from ttp import accountset, cli, coordinator, project
    p = make(env)
    cli.main(["config", "--chat-settable", "list"])
    assert "checks.host_max" in capsys.readouterr().out.split()
    assert coordinator.apply(p, [{"type": "config_set", "key": "checks.host_max", "value": "3"}]) == []
    assert project.load_account_settings()["checks"]["host_max"] == 3
    notes = p.db.kv(coordinator.NOTES_KEY, [])
    assert any('undo: ttp config --account checks.host_max ""' in n for n in notes), notes
    rec = _json.loads(accountset.log_path().read_text().splitlines()[-1])
    assert (rec["key"], rec["old"], rec["new"], rec["by"]) == ("checks.host_max", None, 3, "coordinator")
    assert rec["undo"] == 'ttp config --account checks.host_max ""'
    coordinator.apply(p, [{"type": "config_set", "key": "checks.host_max", "value": "2"}])
    assert project.load_account_settings()["checks"]["host_max"] == 2, "its own value it may change again"
    # The user sets it at a terminal: theirs from then on.
    cli.main(["config", "--account", "checks.host_max", "5"])
    assert _json.loads(accountset.log_path().read_text().splitlines()[-1])["by"] == "user"
    notes = coordinator.apply(p, [{"type": "config_set", "key": "checks.host_max", "value": "1"}])
    assert project.load_account_settings()["checks"]["host_max"] == 5
    assert any("set by the user" in n for n in notes), notes
    with pytest.raises(ValueError, match="set by the user"):
        accountset.queue(p.state, "checks.host_max", 1, {"by": "worker", "task": "7"})
    # A value no logged change explains (set before the log, or by hand) is the user's too.
    project.set_account_setting("checks.load_extra", 4)
    assert accountset.user_set("checks.load_extra")
    with pytest.raises(ValueError, match="set by the user"):
        accountset.apply("checks.load_extra", 0, {"by": "coordinator"})


def test_a_workers_account_checks_cap_change_is_queued_for_the_daemon_which_applies_it_once(env, monkeypatch, capsys):
    """In a run, `ttp config --account checks.host_max N` writes no account file: it queues a request
    in the project's state, the same command again adds nothing, and the daemon's apply_requests sets
    and logs it with the worker as setter; a refused request is kept with its reason."""
    import json as _json
    from ttp import accountset, cli, project
    p = make(env)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_ID", "r1")
    monkeypatch.setenv("TTP_TASK_ID", "42")
    cli.main(["config", "--account", "checks.host_max", "2"])
    assert "queued for" in capsys.readouterr().out
    cli.main(["config", "--account", "checks.host_max", "2"])
    assert "queued already" in capsys.readouterr().out
    assert (project.load_account_settings().get("checks") or {}).get("host_max") is None, "not written by the run"
    assert len(list((p.state / accountset.REQUESTS).glob("*.json"))) == 1
    with pytest.raises(SystemExit):
        cli.main(["config", "--account", "checks.host_max", "-1"])
    logged = []
    assert len(accountset.apply_requests(p.state, logged.append)) == 1
    assert project.load_account_settings()["checks"]["host_max"] == 2
    rec = _json.loads(accountset.log_path().read_text().splitlines()[-1])
    assert (rec["by"], rec["task"], rec["new"]) == ("worker", 42, 2) and "undo" in rec
    assert not list((p.state / accountset.REQUESTS).glob("*.json")) and logged
    # A request the user's later value refuses is kept with the reason.
    accountset.queue(p.state, "checks.host_max", 1, {"by": "worker", "task": "43"})
    accountset.apply("checks.host_max", 6, {"by": "user"})
    assert accountset.apply_requests(p.state, logged.append) == []
    failed = list((p.state / accountset.REQUESTS).glob("*.failed.json"))
    assert len(failed) == 1 and "set by the user" in _json.loads(failed[0].read_text())["error"]
    assert project.load_account_settings()["checks"]["host_max"] == 6


def test_an_urgent_checks_run_takes_a_free_pool_host_at_once_with_every_slot_busy(env, tmp_path, monkeypatch):
    """#6149 (a): an urgent run (priority 0) with every host-wide slot busy goes to a free host of the
    account's test pool at once, not after checkspill.after_s."""
    import io
    from ttp import checkcap, checkspill, cli
    p = make(env)
    p.set_config("delivery.checks_spill_hosts", ["check-host-a"])
    p.set_config("delivery.checks_spill_after_s", 60)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.setattr(checkcap, "task_rank", lambda _p: (0, []))
    monkeypatch.setattr(checkspill, "pick", lambda *_a, **_k: {"host": "check-host-a"})
    held = checkcap.acquire("routine", priority=3)
    try:
        t0 = time.time()
        slot = cli._checks_slot(p, run_dir, io.StringIO(), "f" * 40, True, cmds=["true"], top=str(p.base), spill=True)
        assert slot is not None and slot.spill == {"host": "check-host-a"} and slot.f is None
        assert time.time() - t0 < 20, "no wait for the 60 s spill bound"
        assert not checkcap.state()["urgent"], "the pool host took it: no urgent slot here"
    finally:
        held.close()


def test_an_urgent_checks_run_takes_the_reserved_urgent_slot_beyond_a_full_cap(env, monkeypatch):
    """#6149 (b): with every slot of the cap held (or the run parked), an urgent run takes the reserved
    urgent slot at once; one only (checks.urgent_slots), never a routine run's, and 0 turns it off."""
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.delenv(checkcap.URGENT_SLOTS_ENV, raising=False)
    assert checkcap.urgent_slots() == checkcap.URGENT_SLOTS == 1
    held = checkcap.acquire("routine", priority=3)
    told = []
    try:
        assert checkcap.acquire("late routine", priority=3, urgent=True, max_wait_s=0.5) is None
        u = checkcap.acquire("urgent", priority=0, urgent=True, tell=told.append, max_wait_s=30)
        assert u is not None and u.urgent and u.f is not None and checkcap.URGENT_LINE in told
        assert checkcap.state()["urgent"] == ["urgent"] and "urgent slot beyond the cap: urgent" in checkcap.line()
        assert checkcap.urgent_key(u).startswith("urgent:"), "its test lane is the reserved one"
        monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")   # with none free a second one does not get one
        assert checkcap.acquire("urgent 2", priority=0, urgent=True, max_wait_s=0.5) is None
        u.close()
        parked = checkcap.acquire("urgent parked", priority=0, urgent=True, startable=lambda: False, max_wait_s=0.5)
        assert parked is None, "turned off: it waits as before"
        monkeypatch.delenv(checkcap.URGENT_SLOTS_ENV)
        parked = checkcap.acquire("urgent parked", priority=0, urgent=True, startable=lambda: False, max_wait_s=30)
        assert parked is not None and parked.urgent, "a parked urgent run takes it too"
        parked.close()
    finally:
        held.close()


def test_an_urgent_checks_run_with_no_urgent_slot_preempts_the_lowest_priority_routine_run(env, monkeypatch):
    """#6149 (c): no urgent slot free, an urgent run asks the lowest-priority preemptible routine run on a
    slot of the cap to give it up (never an urgent one) and takes the slot it frees."""
    import threading
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "3")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")
    monkeypatch.setattr(checkcap, "POLL_S", 0.1)
    hold = [checkcap.acquire("urgent other", priority=0, preemptible=True),
            checkcap.acquire("routine 2", priority=2, preemptible=True),
            checkcap.acquire("routine 5", priority=5, preemptible=True)]
    told, got = [], []
    t = threading.Thread(target=lambda: got.append(checkcap.acquire("urgent", priority=0, urgent=True,
                                                                    tell=told.append, max_wait_s=30)))
    t.start()
    try:
        assert _wait_for(lambda: any(checkcap.preempt_line("routine 5") == m for m in told)), told
        assert checkcap.preempted(hold[2].since) == "urgent urgent", "no task: named by its holder"
        assert checkcap.preempted(time.time() + 60) == "", "a request older than the slot is not for it"
        hold[2].close()
        t.join(30)
        assert got and got[0] is not None and got[0].f is not None and not got[0].urgent
    finally:
        checkcap.clear_preempt()
        for h in hold + got:
            if h is not None:
                h.close()


def _preempt_cap_of_one(monkeypatch):
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")


def test_a_parked_urgent_run_gets_the_slot_its_preempt_frees_at_once_not_at_its_next_poll(env, monkeypatch):
    """[note 0781a4c0bc1d23dc]: an urgent run parked for its test slots asks a routine holder whose stop lets
    it start. Its claim lives on while that run holds (past CLAIM_S: the run is still asked), and once the run
    lets go the asker takes the slot within a second or two, not at its next POLL_S (5 s) poll."""
    import threading
    from ttp import checkcap
    _preempt_cap_of_one(monkeypatch)
    monkeypatch.delenv("TTP_TEST_POLL_S", raising=False)   # the real poll interval: the wake must beat it
    monkeypatch.setattr(checkcap, "CLAIM_S", 1.0)
    assert checkcap.POLL_S >= 5.0
    victim = checkcap.acquire("routine 5", priority=5, preemptible=True)
    freed = [False]   # the victim's test slots: free once it stops (testslots counts them so under FREEING)
    told, got, at = [], [], []
    t = threading.Thread(target=lambda: (got.append(checkcap.acquire(
        "urgent", priority=0, urgent=True, tell=told.append, max_wait_s=30,
        startable=lambda: checkcap.FREEING.get() is not None or freed[0])), at.append(time.time())))
    t.start()
    try:
        assert _wait_for(lambda: checkcap.preempt_line("routine 5") in told, 10), told
        assert checkcap.PARKED_LINE in told, "it asked while parked"
        time.sleep(2.0)   # past CLAIM_S: a parked asker's claim, and with it the request, must not lapse
        assert checkcap.preempted(victim.since) == "urgent urgent", "the asked run is still asked"
        freed[0] = True
        victim.close()
        gone = time.time()
        t.join(30)
        assert got and got[0] is not None and got[0].f is not None
        assert at[0] - gone < 2.0, f"granted {at[0] - gone:.1f} s after the slot freed"
    finally:
        checkcap.clear_preempt()
        victim.close()
        t.join(30)
        got and got[0] and got[0].close()


def test_a_parked_asker_that_still_cannot_start_lets_the_freed_slot_go_to_the_next_waiter(env, monkeypatch):
    """[note 0781a4c0bc1d23dc]: no slot sits idle while a waiter that could use it waits. The parked urgent
    asker still cannot start after the asked run let go (its test slots went elsewhere): HANDOVER_S later it
    drops its claim and the queued routine waiter takes the slot, long before the claim would lapse."""
    import threading
    from ttp import checkcap
    _preempt_cap_of_one(monkeypatch)
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.2")
    assert checkcap.CLAIM_S >= 30 and checkcap.HANDOVER_S < 5
    victim = checkcap.acquire("routine 5", priority=5, preemptible=True)
    stop = [""]
    told, urgent, waiter, at = [], [], [], []

    def asker():
        try:
            urgent.append(checkcap.acquire("urgent", priority=0, urgent=True, tell=told.append, max_wait_s=30,
                                           stop=lambda: stop[0],
                                           startable=lambda: checkcap.FREEING.get() is not None))
        except checkcap.Cancelled:
            urgent.append(None)
    ta = threading.Thread(target=asker)
    ta.start()
    tw = None
    try:
        assert _wait_for(lambda: checkcap.preempt_line("routine 5") in told, 10), told
        tw = threading.Thread(target=lambda: (waiter.append(checkcap.acquire("routine 3", priority=3, max_wait_s=30)),
                                              at.append(time.time())))
        tw.start()
        assert _wait_for(lambda: checkcap.state()["queued"], 10)
        victim.close()
        gone = time.time()
        tw.join(30)
        assert waiter and waiter[0] is not None and waiter[0].f is not None
        assert at[0] - gone < checkcap.HANDOVER_S + 2.0, f"the slot sat free {at[0] - gone:.1f} s"
        assert ta.is_alive() and not urgent, "the asker that cannot start still waits"
    finally:
        stop[0] = "test over"
        checkcap.clear_preempt()
        victim.close()
        ta.join(30)
        tw and tw.join(30)
        for g in urgent + waiter:
            g and g.close()


def test_a_preempted_routine_checks_run_requeues_at_its_old_place_and_runs_again(env, tmp_path, monkeypatch, capsys):
    """#6149 (c): a routine run asked for its slot (checkcap.preempt) ends its command, frees the slot and
    queues again at its first arrival, then runs to its result."""
    from ttp import checkcap, checkwall, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkwall, "POLL_S", 0.1)
    real, arrivals, holders = checkcap.acquire, [], []
    monkeypatch.setattr(checkcap, "acquire",
                        lambda *a, **k: arrivals.append((a[5] if len(a) > 5 else k.get("since"), k.get("preemptible")))
                        or holders.append(a[0]) or real(*a, **k))
    monkeypatch.setenv(checkcap.CUT_ENV, "")
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    mark, req = tmp_path / "ran-once", checkcap.slot_dir() / f"{checkcap.PREEMPT}.{os.getpid()}.json"
    script = (f'if [ -e {mark} ]; then exit 0; fi; touch {mark}; '
              f'printf \'{{"by": "urgent u", "task": "77", "at": %s}}\' "$(($(date +%s) + 1))" > {req}; sleep 60')
    t0 = time.time()
    cli.main(["checks", "--", "sh", "-c", script])
    out = capsys.readouterr().out
    assert time.time() - t0 < 45, "its command ended once asked"
    assert "preempted by urgent #77: an urgent run needed this host-wide checks slot" in out, out
    assert "preempted by urgent #77" in (run / "progress.md").read_text(), "its task is told"
    assert "[requeued after preemption by urgent #77]" in holders[1], "its queue entry says so (label)"
    assert "preempted" not in holders[0] and "[preempted by" not in holders[1], holders
    assert checkcap.PREEMPTED_ENV not in os.environ, "it holds a slot again: the label clears"
    assert "passed" in out.rsplit("preempted by", 1)[1], "its own retry ran to its result"
    assert len(arrivals) == 2 and arrivals[0] == arrivals[1] and arrivals[0][1], "it queued again at its old place"
    assert not req.exists() and checkcap.line() == ""
    assert mark.exists()


def test_a_preempted_checks_run_does_not_rerun_a_check_that_passed_before_the_preemption(env, tmp_path, monkeypatch,
                                                                                         capsys):
    """A check that passed in full before a later check of the same call was preempted keeps its pass
    (a suite pass): the requeued run does not run it again, only the rest."""
    from ttp import checkcap, checkwall, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkwall, "POLL_S", 0.1)
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    count, mark = tmp_path / "count", tmp_path / "ran-once"
    req = checkcap.slot_dir() / f"{checkcap.PREEMPT}.{os.getpid()}.json"
    first = f"echo x >> {count}"
    second = (f'if [ -e {mark} ]; then exit 0; fi; touch {mark}; '
              f'printf \'{{"by": "urgent u", "task": "77", "at": %s}}\' "$(($(date +%s) + 1))" > {req}; sleep 60')
    cli.main(["checks", "--", first, f"sh -c {shlex.quote(second)}"])
    out = capsys.readouterr().out
    assert "preempted by urgent #77" in out, out
    assert count.read_text().count("x") == 1, "the check that passed before the preemption ran again"
    assert "not run again" in out.rsplit("preempted by", 1)[1]
    assert json.loads((run / "checks.json").read_text())["passed"] is True


def test_check_commands_never_see_the_checks_runs_own_preempt_rerank_and_reexec_env(env, tmp_path, monkeypatch, capsys):
    """#6289: what only a `ttp checks` run reads (checkcap.RUN_ONLY_ENV: the label a preemption sets, its
    asked request, its rerank and re-execution marks) stays in the run but never reaches its check commands,
    so a `ttp checks` nested in them is not labelled preempted nor kept from its own rerank or re-execution."""
    from ttp import checkcap, checkwall, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkwall, "POLL_S", 0.1)
    monkeypatch.setenv(checkcap.CUT_ENV, "")
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, '{"since": 1.0, "asked": "x"}')
    monkeypatch.setenv(checkcap.RERANK_ENV, "1")
    monkeypatch.setenv(checkcap.REEXEC_ENV, "0.0.1")
    assert {checkcap.PREEMPTED_ENV, checkcap.PREEMPT_ASKED_ENV, checkcap.RERANK_ENV} <= set(checkcap.RUN_ONLY_ENV)
    mark, dump = tmp_path / "ran-once", tmp_path / "env.txt"
    req = checkcap.slot_dir() / f"{checkcap.PREEMPT}.{os.getpid()}.json"
    script = (f'if [ -e {mark} ]; then env > {dump}; exit 0; fi; touch {mark}; '
              f'printf \'{{"by": "urgent u", "task": "77", "at": %s}}\' "$(($(date +%s) + 1))" > {req}; sleep 60')
    cli.main(["checks", "--", "sh", "-c", script])
    out = capsys.readouterr().out
    assert "preempted by urgent #77" in out and "passed" in out.rsplit("preempted by", 1)[1], out
    assert checkcap.PREEMPTED_ENV not in os.environ, "the run dropped its label once it held a slot again"
    seen = {line.split("=", 1)[0] for line in dump.read_text().splitlines() if "=" in line}
    assert "TTP_RUN_DIR" in seen, "the dump is the check's environment"
    assert not seen & set(checkcap.RUN_ONLY_ENV), sorted(seen & set(checkcap.RUN_ONLY_ENV))
    assert os.environ[checkcap.RERANK_ENV] == "1" and os.environ[checkcap.REEXEC_ENV] == "0.0.1"


def test_an_urgent_checks_run_past_its_grace_preempts_one_routine_run_of_its_own_project_first(env, monkeypatch):
    """#6246 (1): every cap slot and the urgent slot held, an urgent run that has waited past
    checks.preempt_after_s asks exactly one routine run to step back: its own project's first, even over a
    less urgent run of another project, and only once per request (a re-execution at the same arrival too);
    it takes the slot that run frees. The asked run sees `urgent #<task>` and its queue entry says so."""
    import threading
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "1")
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "0.5")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    monkeypatch.setenv("TTP_TASK", "6198")
    monkeypatch.setattr(checkcap, "POLL_S", 0.1)
    other = checkcap.acquire("other p3", project_name="other", priority=3, preemptible=True)
    mine = checkcap.acquire("mine p2", project_name="mine", priority=2, preemptible=True)
    held_urgent = checkcap.acquire("other urgent", project_name="other", priority=1, urgent=True)
    assert held_urgent.urgent, "the urgent slot beyond the cap is held too"
    told, got, since = [], [], time.time()
    t = threading.Thread(target=lambda: got.append(checkcap.acquire(
        "urgent", project_name="mine", priority=1, urgent=True, tell=told.append, since=since, max_wait_s=30)))
    t.start()
    try:
        assert _wait_for(lambda: any(checkcap.preempt_line("mine p2") == m for m in told)), told
        assert time.time() - since >= 0.5, "not before its grace"
        assert checkcap.preempted(mine.since) == "urgent #6198"
        monkeypatch.setenv(checkcap.PREEMPTED_ENV, checkcap.preempted(mine.since))
        assert checkcap.label("mine") == "mine task #6198 (run ?) [requeued after preemption by urgent #6198]"
        mine.close()
        t.join(30)
        assert got and got[0] is not None and got[0].f is not None and not got[0].urgent
        assert sum(m.startswith("urgent checks with every") for m in told) == 1, "one preemption per request"
        got[0].close()
        checkcap.clear_preempt()
        # The same request again (a re-execution keeps its arrival): it does not ask again at once; only
        # preempt_after_s after its earlier ask lapsed may it (#7124).
        mine = checkcap.acquire("mine p2", project_name="mine", priority=2, preemptible=True)
        told.clear()
        assert checkcap.acquire("urgent", project_name="mine", priority=1, urgent=True, tell=told.append,
                                since=since, max_wait_s=0.3) is None
        assert not any(m.startswith("urgent checks with every") for m in told), told
        assert not list(checkcap.slot_dir().glob(f"{checkcap.PREEMPT}.*.json"))
    finally:
        checkcap.clear_preempt()
        for h in (other, mine, held_urgent, *got):
            if h is not None:
                h.close()


def test_an_urgent_run_that_was_preempted_queues_again_and_preempts_nobody(env, monkeypatch):
    """[note 46ef17a6f474ab41]: an urgent run that was itself preempted queues again at its old arrival, so
    its grace has long passed. It asked a third run to step back at once, while the run that preempted it
    still waited for its test slots: two runs lost work and the asker gained nothing. A preempted run asks
    nobody when it queues again, and the run that asked (owed: it may ask an urgent run) gets the slot it
    freed."""
    import subprocess
    import threading
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    monkeypatch.setattr(checkcap, "POLL_S", 0.1)
    third = checkcap.acquire("third p3", project_name="other", priority=3, preemptible=True)
    proc = subprocess.Popen(["sleep", "60"])   # its own process: a request to it is its own file
    locks.relabel(third.f, {**locks._owner(proc.pid), "ns_pid": None})
    first_since = time.time() - 900
    mine = checkcap.acquire("mine p1", project_name="mine", priority=1, preemptible=True, since=first_since)
    freed = threading.Event()   # the asker's test slots: free only once the run it asked let go
    startable = lambda: freed.is_set() or (checkcap.FREEING.get() or {}).get("holder") == "mine p1"  # noqa: E731
    told, got, again = [], [], []
    t = threading.Thread(target=lambda: got.append(checkcap.acquire(
        "asker p0", project_name="mine", priority=0, owns=["o1"], urgent=True, tell=told.append,
        startable=startable, max_wait_s=30)))
    t.start()
    try:
        assert _wait_for(lambda: checkcap.preempt_line("mine p1") in told), told
        monkeypatch.setenv(checkcap.PREEMPTED_ENV, checkcap.preempted(mine.since))
        mine.close()
        # It queues again at its first arrival, urgent, past every grace, while the asker still waits for
        # its test slots: it asks nobody.
        again_told: list = []
        again.append(checkcap.acquire("mine p1", project_name="mine", priority=1, urgent=True, preemptible=True,
                                      since=first_since, tell=again_told.append, max_wait_s=1.5))
        assert again == [None], "the preempted run waits for a slot"
        assert not any(m.startswith("urgent checks with every") for m in again_told), again_told
        assert not checkcap._preempt_path(proc.pid).exists(), "the third run was never asked"
        freed.set()
        t.join(30)
        assert got and got[0] is not None and got[0].f is not None, "the asker took the slot it freed"
    finally:
        freed.set()
        checkcap.clear_preempt()
        checkcap._preempt_path(proc.pid).unlink(missing_ok=True)
        proc.kill()
        proc.wait()
        for h in (third, mine, *got, *again):
            if h is not None:
                h.close()


def test_push_priority_urgent_and_lock_holding_checks_runs_are_never_preempted(env, monkeypatch):
    """#6246 (2): with the cap held by an urgent run and a routine run behind a lock, and a push branch's
    priority slot held, an urgent waiter past its grace asks nobody; preemptible() refuses a wrapper lock and
    commands that take a lock or device queue themselves."""
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")
    monkeypatch.setattr(checkcap, "POLL_S", 0.1)
    assert checkcap.preemptible(["pytest -q tests/test_flock_utils.py"]) and checkcap.preemptible(["ttp locks"])
    assert not checkcap.preemptible(["pytest -q"], lock="abc123"), "behind checks.wrap_cmd's lock"
    for cmd in ("ttp lock board -- pytest -q", "make && ttp devq run x", "(flock /tmp/l make)", "flock -n x y"):
        assert not checkcap.preemptible([cmd]), cmd
    held = [checkcap.acquire("urgent run", priority=1, preemptible=True),
            checkcap.acquire("lock holder", priority=5, preemptible=checkcap.preemptible(["ttp lock board -- t"]))]
    push = checkcap.acquire_priority("origin/branch", "push checks")
    told = []
    try:
        assert checkcap.acquire("urgent", priority=0, urgent=True, tell=told.append, max_wait_s=1.0) is None
        assert not any(m.startswith("urgent checks with every") for m in told), told
        assert not list(checkcap.slot_dir().glob(f"{checkcap.PREEMPT}.*.json")), "nobody was asked"
    finally:
        push.close()
        for h in held:
            h.close()


def _owed_preempt_env(monkeypatch, cap="2"):
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", cap)
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    monkeypatch.setattr(checkcap, "POLL_S", 0.1)
    return checkcap


def test_an_owed_checks_run_preempts_an_urgent_holder_that_owes_nothing_after_every_routine_one(env, monkeypatch):
    """#7057 (a): the slot label records whether the holder's task owns an open obligation; an owed waiter
    asks an urgent holder that owes nothing (the latest granted first), but a routine holder before it, even
    one of another project while the urgent one is of its own."""
    import threading
    checkcap = _owed_preempt_env(monkeypatch, cap="3")
    hold = [checkcap.acquire("owed holder", priority=0, owns=["o1"], preemptible=True),
            checkcap.acquire("urgent early", project_name="mine", priority=0, preemptible=True),
            checkcap.acquire("urgent late", project_name="mine", priority=1, preemptible=True)]
    told, got = [], []
    try:
        labels = [json.loads(h.f.name and Path(h.f.name).read_text()) for h in hold]
        assert [lb["owed"] for lb in labels] == [True, False, False], "the writer records it"
        t = threading.Thread(target=lambda: got.append(checkcap.acquire(
            "owed", project_name="mine", priority=0, owns=["o1"], urgent=True, tell=told.append, max_wait_s=30)))
        t.start()
        assert _wait_for(lambda: checkcap.preempt_line("urgent late") in told), told
        assert checkcap.preempted(hold[2].since) == "urgent owed"
        hold[2].close()
        t.join(30)
        assert got and got[0] is not None and got[0].f is not None
    finally:
        checkcap.clear_preempt()
        for h in hold + got:
            if h is not None:
                h.close()
    routine = checkcap.acquire("routine other", project_name="other", priority=5, preemptible=True)
    urgent = checkcap.acquire("urgent mine", project_name="mine", priority=1, preemptible=True)
    owed = checkcap.acquire("owed holder", priority=0, owns=["o1"], preemptible=True)
    told = []
    try:
        assert checkcap.acquire("owed", project_name="mine", priority=0, owns=["o1"], urgent=True,
                                tell=told.append, max_wait_s=1.0) is None
        assert checkcap.preempt_line("routine other") in told, told
    finally:
        checkcap.clear_preempt()
        for h in (routine, urgent, owed):
            h.close()


def test_an_owed_checks_run_never_preempts_an_owed_holder_or_a_push_check_and_others_never_preempt_urgent(env, monkeypatch):
    """#7057 (b): an owed waiter asks nobody while the cap holds only owed runs, with a push branch's priority
    slot and a push check's turn on the cap held too; a waiter that owes nothing never asks an urgent holder."""
    checkcap = _owed_preempt_env(monkeypatch)
    owed = checkcap.acquire("owed holder", priority=0, owns=["o2"], preemptible=True)
    push_turn = checkcap.acquire("push turn", "push checks on abc", priority=0)   # push.turn(): never preemptible
    push = checkcap.acquire_priority("origin/branch", "push checks")
    told = []
    try:
        assert checkcap.acquire("owed", priority=0, owns=["o1"], urgent=True, tell=told.append, max_wait_s=1.0) is None
        assert not any(m.startswith("urgent checks with every") for m in told), told
        assert not list(checkcap.slot_dir().glob(f"{checkcap.PREEMPT}.*.json")), "nobody was asked"
    finally:
        push.close()
        push_turn.close()
        owed.close()
    urgent = [checkcap.acquire("urgent a", priority=0, preemptible=True),
              checkcap.acquire("urgent b", priority=1, preemptible=True)]
    try:
        assert checkcap.acquire("not owed", priority=0, urgent=True, tell=told.append, max_wait_s=1.0) is None
        assert not list(checkcap.slot_dir().glob(f"{checkcap.PREEMPT}.*.json")), "only an owed waiter asks urgent"
    finally:
        for h in urgent:
            h.close()


def test_a_preempted_urgent_checks_run_that_owes_nothing_requeues_at_its_old_place(env, tmp_path, monkeypatch, capsys):
    """#7057 (a): an urgent run that owes nothing, asked for its cap slot, ends its command, frees the slot
    and queues again at its first arrival, then runs to its result, as a routine one does."""
    from ttp import checkcap, checkwall, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkwall, "POLL_S", 0.1)
    monkeypatch.setattr(checkcap, "task_rank", lambda _p: (1, []))
    real, arrivals = checkcap.acquire, []
    monkeypatch.setattr(checkcap, "acquire",
                        lambda *a, **k: arrivals.append((a[5] if len(a) > 5 else k.get("since"), k.get("urgent")))
                        or real(*a, **k))
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    mark, req = tmp_path / "ran-once", checkcap.slot_dir() / f"{checkcap.PREEMPT}.{os.getpid()}.json"
    script = (f'if [ -e {mark} ]; then exit 0; fi; touch {mark}; '
              f'printf \'{{"by": "urgent u", "task": "78", "at": %s}}\' "$(($(date +%s) + 1))" > {req}; sleep 60')
    t0 = time.time()
    cli.main(["checks", "--", "sh", "-c", script])
    out = capsys.readouterr().out
    assert time.time() - t0 < 45, "its command ended once asked"
    assert "preempted by urgent #78" in out, out
    assert "passed" in out.rsplit("preempted by", 1)[1], "its own retry ran to its result"
    assert len(arrivals) == 2 and arrivals[0] == arrivals[1] and arrivals[0][1], "an urgent run queued again at its old place"


def test_a_freed_reserved_urgent_slot_goes_to_the_parked_owed_waiter_before_an_older_urgent_one(env, monkeypatch):
    """#7057 (3): with the cap and the reserved urgent slot held, an owed waiter parked for its test slots gets
    the urgent slot when it frees, before an urgent waiter that owes nothing and arrived earlier; a newcomer
    that owes nothing takes none while the owed one waits; the older one gets the next."""
    import threading
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "1")
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "3600")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")
    monkeypatch.setattr(checkcap, "POLL_S", 0.1)
    routine = checkcap.acquire("routine", priority=3)
    first = checkcap.acquire("first urgent", priority=1, urgent=True, max_wait_s=5)
    assert first is not None and first.urgent
    got: dict = {}
    older = threading.Thread(target=lambda: got.update(older=checkcap.acquire(
        "older urgent", priority=1, urgent=True, since=time.time() - 600, max_wait_s=30)))
    owed = threading.Thread(target=lambda: got.update(owed=checkcap.acquire(
        "owed parked", priority=0, owns=["o1"], urgent=True, startable=lambda: False, max_wait_s=30)))
    older.start()
    owed.start()
    try:
        assert _wait_for(lambda: len(locks.queued(checkcap.slot_dir(), checkcap.URGENT_WAIT)) == 2)
        first.close()
        owed.join(30)
        assert got.get("owed") is not None and got["owed"].urgent, "the owed waiter took it"
        assert "older" not in got, "the older one still waits"
        late = checkcap.acquire("newcomer", priority=1, urgent=True, max_wait_s=0.5)
        got["owed"].close()
        assert late is None or (late.close() and False), "a newcomer never goes before a waiting one"
        older.join(30)
        assert got.get("older") is not None and got["older"].urgent
    finally:
        for k in ("older", "owed"):
            if got.get(k) is not None:
                got[k].close()
        routine.close()
        first.close()


def test_ttp_checks_preempt_asks_a_run_by_hand_and_refuses_owed_push_and_locked_runs(env, monkeypatch, capsys):
    """#7057 (d): `ttp checks --preempt <run>` writes the request preempt() writes for that run's checks and
    logs who asked; it refuses an owed holder, an urgent one whose label does not say it owes nothing, a push
    check (priority slot or a push turn on the cap), a run behind a lock and an unknown run."""
    from ttp import checkcap, cli, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "5")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv("TTP_CHECKS_PRIORITY_BEYOND_CAP", "1")   # #7068: the old overflow, off by default
    holds = {}
    for run, kw in (("r-owed", {"priority": 0, "owns": ["o1"], "preemptible": True}),
                    ("r-urgent", {"priority": 0, "preemptible": True}),
                    ("r-routine", {"priority": 4, "preemptible": True}),
                    ("r-locked", {"priority": 4, "preemptible": checkcap.preemptible(["ttp lock b -- t"])}),
                    ("r-turn", {"priority": 4})):
        monkeypatch.setenv("TTP_RUN_ID", run)
        holds[run] = checkcap.acquire(f"holder {run}", "push checks on abc" if run == "r-turn" else "", **kw)
    monkeypatch.setenv("TTP_RUN_ID", "r-push")
    holds["r-push"] = checkcap.acquire_priority("origin/branch", "push checks")
    monkeypatch.setenv("TTP_RUN_ID", "r-asker")
    monkeypatch.setenv("TTP_TASK", "91")

    def ask(run):
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--preempt", run])
        return e.value.code, capsys.readouterr().out

    try:
        locks.relabel(holds["r-urgent"].f, {"owed": None})   # an older label: does not say it owes nothing
        for run, why in (("r-owed", "is owed"), ("r-urgent", "is owed"), ("r-push", "push checks"),
                         ("r-turn", "push checks"), ("r-locked", "cannot be asked"), ("r-none", "no checks of run")):
            rc, out = ask(run)
            assert rc == 1 and "not asked" in out and why in out, (run, out)
        assert not list(checkcap.slot_dir().glob(f"{checkcap.PREEMPT}.*.json")), "nobody was asked"
        rc, out = ask("r-routine")
        assert rc == 0 and "asked holder r-routine" in out, out
        assert checkcap.preempted(holds["r-routine"].since) == "urgent #91"
        log = [json.loads(x) for x in (checkcap.slot_dir() / checkcap.PREEMPT_LOG).read_text().splitlines()]
        assert len(log) == 1 and log[0]["run"] == "r-routine" and log[0]["task"] == "91" and log[0]["by"], log
        with pytest.raises(SystemExit):
            cli.main(["checks", "--preempt", "r-routine", "--", "pytest"])
        assert "--preempt runs nothing" in capsys.readouterr().err
    finally:
        checkcap.clear_preempt()
        for h in holds.values():
            h.close()


def test_ttp_checks_preempt_finds_a_relaunched_run_by_the_run_slot_status_names(env, tmp_path, monkeypatch, capsys):
    """#7190 (2): checks the daemon started again after a reboot hold their host-wide slot under the run_id
    relaunch-t<task>, while `ttp checks --slot-status` names them by their run folder (run 13549).
    `ttp checks --preempt 13549` finds that slot through the run's lane holder and asks it; a run no lane
    holder names is still not found, and the run_id still works."""
    from ttp import checkcap, checklane, cli, testslots
    p, repo, _, _ = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "5")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    run_dir = p.runs / "13549"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.setenv("TTP_RUN_ID", "relaunch-t6862")
    monkeypatch.setenv("TTP_TASK", "6862")
    lane = testslots.suite_hold(p.name, wait_s=0, who=f"run {run_dir.name}", priority=2,
                                repo=testslots.repo_key(str(repo)))
    slot = checkcap.acquire(checkcap.label(p.name), "ttp checks on abc", priority=2, project_name=p.name,
                            preemptible=True)
    monkeypatch.setenv("TTP_RUN_ID", "r-asker")
    monkeypatch.setenv("TTP_TASK", "91")

    def ask(run):
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--preempt", run])
        return e.value.code, capsys.readouterr().out

    try:
        assert slot.f is not None and lane is not None
        (row,) = checklane.lane_status(p.name)
        assert [h["run"] for h in row["lanes"]] == ["13549"], row
        rc, out = ask("13550")
        assert rc == 1 and "no checks of run 13550" in out, out
        rc, out = ask("13549")
        assert rc == 0 and "task #6862 (run relaunch-t6862)" in out and "asked" in out, out
        assert checkcap.preempted(slot.since) == "urgent #91"
        checkcap.clear_preempt()
        rc, out = ask("relaunch-t6862")
        assert rc == 0 and checkcap.preempted(slot.since), out
    finally:
        checkcap.clear_preempt()
        slot.close()
        lane.close()


def test_no_preemption_within_the_grace_and_it_counts_from_the_arrival(env, monkeypatch):
    """#6246 (3): an urgent waiter asks nobody while it has waited less than checks.preempt_after_s (default
    PREEMPT_AFTER_S); one that arrived longer ago (a re-execution keeps its arrival) asks at once."""
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.delenv(checkcap.PREEMPT_AFTER_ENV, raising=False)
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")
    monkeypatch.setattr(checkcap, "POLL_S", 0.1)
    assert checkcap.preempt_after_s() == checkcap.PREEMPT_AFTER_S == 120.0
    for bad in ("-1", "x", "inf"):
        monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, bad)
        assert checkcap.preempt_after_s() == 120.0, bad
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "5")
    routine = checkcap.acquire("routine", priority=3, preemptible=True)
    told = []
    try:
        assert checkcap.acquire("urgent", priority=1, urgent=True, tell=told.append, max_wait_s=1.0) is None
        assert not any(m.startswith("urgent checks with every") for m in told), told
        assert not list(checkcap.slot_dir().glob(f"{checkcap.PREEMPT}.*.json"))
        assert checkcap.acquire("urgent", priority=1, urgent=True, tell=told.append, since=time.time() - 6,
                                max_wait_s=1.0) is None
        assert checkcap.preempt_line("routine") in told, told
    finally:
        checkcap.clear_preempt()
        routine.close()


def test_a_waiting_routine_checks_run_whose_task_turns_urgent_re_executes_once_as_urgent(env, monkeypatch):
    """#6246: the checks of a task raised to urgent while they wait read its priority again (reranked, at most
    every RERANK_S) and re-execute once to queue as urgent, keeping their arrival; not while it stays urgent."""
    import io
    from ttp import checkcap, cli
    monkeypatch.setenv(checkcap.RERANK_ENV, "")
    monkeypatch.setenv(checkcap.REEXEC_ENV, "")
    monkeypatch.setattr(checkcap, "_RERANK", {"at": 0.0})
    rank = [(3, [])]
    monkeypatch.setattr(checkcap, "task_rank", lambda p: rank[0])
    p = object()
    assert checkcap.reranked(p, 3, now=1000.0) is None, "still routine"
    rank[0] = (1, [])
    assert checkcap.reranked(p, 3, now=1000.0 + checkcap.RERANK_S / 2) is None, "read again only every RERANK_S"
    assert checkcap.reranked(p, 3, now=1000.0 + checkcap.RERANK_S) == 1
    assert checkcap.reranked(p, 1, now=5000.0) is None and checkcap.reranked(None, 3, now=6000.0) is None
    seen = {}

    def execv(*a):
        seen.update(rerank=os.environ.get(checkcap.RERANK_ENV), reexec=os.environ.get(checkcap.REEXEC_ENV),
                    arrived=os.environ.get(checkcap.ARRIVED_ENV))
        raise OSError("no exec in a test")
    monkeypatch.setattr(os, "execv", execv)
    monkeypatch.setenv(checkcap.ARRIVED_ENV, "")
    told = []
    cli._checks_reexec("1", 1234.5, 3, True, told.append, io.StringIO(), rerank=True)
    assert seen == {"rerank": "1", "reexec": "", "arrived": "1234.5"}, seen
    assert told[0].startswith("its task turned urgent (priority 1) while these checks waited at priority 3"), told
    assert checkcap.reranked(p, 3, now=9000.0) is None, "once per priority"


def test_a_waiting_checks_run_re_ranks_whenever_its_task_priority_changes_so_a_stale_ticket_holds_nobody(
        env, monkeypatch):
    """#6907: a run whose task is lowered while it waits (urgent 1 to routine 2) re-executes to queue at the
    new priority too, and again on a later change; a failed re-execution is not retried at that priority. A
    ticket left at the old priority would keep holding a later urgent waiter of its project as urgent."""
    import io
    from ttp import checkcap, cli, locks
    monkeypatch.setenv(checkcap.RERANK_ENV, "")
    monkeypatch.setattr(checkcap, "_RERANK", {"at": 0.0})
    rank = [(2, [])]
    monkeypatch.setattr(checkcap, "task_rank", lambda p: rank[0])
    p = object()
    assert checkcap.reranked(p, 1, now=1000.0) == 2, "an urgent run lowered to 2"
    monkeypatch.setenv(checkcap.RERANK_ENV, "2")   # re-executed at 2
    assert checkcap.reranked(p, 2, now=2000.0) is None, "queued at its priority"
    rank[0] = (1, [])
    assert checkcap.reranked(p, 2, now=3000.0) == 1, "raised again: re-ranked again"
    monkeypatch.setenv(checkcap.RERANK_ENV, "1")   # that re-execution failed: waits on at 2
    assert checkcap.reranked(p, 2, now=4000.0) is None, "not retried at the same priority"
    rank[0] = (None, [])
    assert checkcap.reranked(p, 2, now=5000.0) is None, "no task: as it queued"
    monkeypatch.setattr(os, "execv", lambda *a: (_ for _ in ()).throw(OSError("no exec in a test")))
    monkeypatch.setenv(checkcap.ARRIVED_ENV, "")
    told = []
    cli._checks_reexec("2", 1234.5, 1, True, told.append, io.StringIO(), rerank=True)
    assert told[0].startswith("its task changed priority (priority 2) while these checks waited at priority 1"), told
    assert "queueing again at their arrival" in told[0] and os.environ[checkcap.RERANK_ENV] == "2", told
    monkeypatch.setenv(checkcap.STARVE_ENV, "3600")
    now = time.time()
    d, P = checkcap.slot_dir(), checkcap.PARKED
    stale = locks.enqueue(d, P, "pa task #1 (run 1)", since=now - 9000, group="pa", priority=1)
    try:
        assert checkcap.starved_ahead("pa", now - 60, now, priority=1) == [stale], "left at 1: holds an urgent run"
    finally:
        locks.dequeue(stale)
    ranked = locks.enqueue(d, P, "pa task #1 (run 1)", since=now - 9000, group="pa", priority=2)
    try:
        assert checkcap.starved_ahead("pa", now - 60, now, priority=1) == [], "re-ranked to 2: holds nobody urgent"
        assert checkcap.starved_ahead("pa", now - 60, now, priority=0) == [], "nor an owed one"
    finally:
        locks.dequeue(ranked)


def _yield_setup(monkeypatch, cap: str = "1"):
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", cap)
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    for k in (checkcap.HELD_ENV, checkcap.YIELD_ENV, checkcap.PREEMPT_ASKED_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(checkcap, "POLL_S", 0.05)


def _yield_reqs(run) -> list:
    stem = Path(run.yield_stem)
    return sorted(stem.parent.glob(stem.name + ".*.req"))


def test_a_check_waiting_on_an_outside_pool_frees_its_slot_and_takes_one_back_in_priority_order(env, monkeypatch):
    """[note d3dbe43fdffc5aab] (1) a run whose command waits on an outside pool gives its host-wide slot up
    (checks-yield) and a queued check starts on it; (2) asked to resume, it queues again in the normal
    order: an urgent run goes before a routine waiter of another project that arrived earlier."""
    import threading
    from ttp import checkcap, locks
    _yield_setup(monkeypatch)
    run = checkcap.acquire("waiter", project_name="A", priority=1)
    assert run.allow_yield() and run.env()[checkcap.YIELD_ENV].startswith(str(checkcap.slot_dir()))
    monkeypatch.setenv(checkcap.YIELD_ENV, run.yield_stem)
    got, asked = {}, {}
    queued = threading.Thread(target=lambda: got.update(b=checkcap.acquire("queued", project_name="B", max_wait_s=30)))
    queued.start()
    ask = threading.Thread(target=lambda: asked.update(y=checkcap.ask_yield("yield", os.getpid(), wait_s=30)))
    ask.start()
    assert _wait_for(lambda: _yield_reqs(run))
    told = []
    checkcap.serve_yield(run, told.append)
    ask.join(30)
    queued.join(30)
    assert asked == {"y": ""} and run.f is None and run.yielded and told, "the slot went at once, told"
    assert got["b"] is not None and got["b"].f is not None, "a queued check started on the freed slot"
    checkcap.serve_yield(run)
    assert run.f is None and len(locks.held_labels(checkcap.all_paths())) == 1, "a request is answered once"
    routine = threading.Thread(target=lambda: got.update(c=checkcap.acquire("routine", project_name="C",
                                                                            priority=5, max_wait_s=30)))
    routine.start()
    assert _wait_for(lambda: len(locks.queued(checkcap.slot_dir(), checkcap.RESOURCE) or []) >= 1)
    ask = threading.Thread(target=lambda: asked.update(r=checkcap.ask_yield("resume", os.getpid())))
    ask.start()
    assert _wait_for(lambda: any('"resume"' in f.read_text() for f in _yield_reqs(run)))
    serve = threading.Thread(target=checkcap.serve_yield, args=(run,))
    serve.start()
    assert _wait_for(lambda: len(locks.queued(checkcap.slot_dir(), checkcap.RESOURCE) or []) >= 2)
    assert serve.is_alive() and "r" not in asked, "it waits for a slot: none is free"
    got["b"].close()
    serve.join(30)
    ask.join(30)
    assert asked["r"] == "" and run.f is not None and not run.yielded, "it took the freed slot back"
    assert routine.is_alive() and "c" not in got, "before the routine waiter that arrived earlier"
    run.close()
    routine.join(30)
    assert got["c"] is not None and got["c"].f is not None
    got["c"].close()
    assert not list(checkcap.slot_dir().glob(f"{checkcap.YIELD}.*")), "its request files went with the run"
    assert checkcap.line() == ""


def test_concurrent_checks_yield_askers_of_one_run_are_all_answered(env, monkeypatch):
    """Review #6362: two processes of one check command that run `ttp checks-yield` (then `--resume`) at
    once are each answered (one request file each, never a lost update): the run gives one slot up, keeps
    it given up while either waiter lives, takes exactly one back for both resumes, and once both waiters
    died after a yield takes one back by itself."""
    from ttp import checkcap, locks
    _yield_setup(monkeypatch)

    def held() -> int:
        return len(locks.held_labels(checkcap.all_paths()))

    run = checkcap.acquire("run", project_name="A")
    assert run.allow_yield()
    monkeypatch.setenv(checkcap.YIELD_ENV, run.yield_stem)
    waiters = [subprocess.Popen(["sleep", "60"]) for _ in range(2)]

    def ask(want: str, pid: int):
        code = (f"import sys; sys.path.insert(0, {str(RUNTIME)!r}); from ttp import checkcap; "
                f"print(repr(checkcap.ask_yield({want!r}, {pid}, wait_s=30)), flush=True)")
        return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                                env=dict(os.environ))

    try:
        for want, slot_held in (("yield", 0), ("resume", 1)):
            askers = [ask(want, w.pid) for w in waiters]
            assert _wait_for(lambda: len(_yield_reqs(run)) == 2), f"both {want} requests stay apart"
            checkcap.serve_yield(run)
            outs = [p.communicate(timeout=30)[0].strip() for p in askers]
            assert outs == ["''", "''"], (want, outs)
            assert held() == slot_held and (run.f is not None) == bool(slot_held), (want, held())
            checkcap.serve_yield(run)
            assert held() == slot_held, f"answered {want}s are not served again"
        askers = [ask("yield", w.pid) for w in waiters]
        assert _wait_for(lambda: len(_yield_reqs(run)) == 2)
        checkcap.serve_yield(run)
        assert [p.communicate(timeout=30)[0].strip() for p in askers] == ["''", "''"]
        assert run.yielded and run.waiters == {w.pid for w in waiters} and held() == 0
        waiters[0].kill()
        waiters[0].wait()
        checkcap.serve_yield(run)
        assert run.yielded and held() == 0, "one waiter still waits: the slot stays given up"
        waiters[1].kill()
        waiters[1].wait()
        checkcap.serve_yield(run)
        assert run.f is not None and not run.yielded and held() == 1, "both died: one slot taken back"
        checkcap.serve_yield(run)
        assert held() == 1
    finally:
        for w in waiters:
            w.kill()
    run.close()
    assert held() == 0 and not list(checkcap.slot_dir().glob(f"{checkcap.YIELD}.*"))


def test_a_killed_pool_waiter_leaks_no_host_wide_checks_slot(env, monkeypatch):
    """[note d3dbe43fdffc5aab] (3) a waiter killed while its run's slot is given up holds nothing and leaves
    the run with exactly one slot again for its further work; a slot freed once is never freed twice; a run
    that ends while it queues to take one back holds none; a run killed while yielded leaves none held."""
    from ttp import checkcap, locks
    _yield_setup(monkeypatch, "2")

    def held() -> int:
        return len(locks.held_labels(checkcap.all_paths()))

    run = checkcap.acquire("waiter", project_name="A")
    assert run.allow_yield()
    waiter = subprocess.Popen(["sleep", "60"])
    Path(run.yield_stem + ".1.req").write_text(json.dumps({"id": "1", "want": "yield", "pid": waiter.pid}))
    checkcap.serve_yield(run)
    assert run.f is None and held() == 0
    checkcap.serve_yield(run)
    assert run.yielded and held() == 0, "its waiter lives: the slot stays given up"
    waiter.kill()
    waiter.wait()
    checkcap.serve_yield(run)
    assert run.f is not None and not run.yielded and held() == 1, "the waiter died: the run holds one slot again"
    other = checkcap.acquire("other", max_wait_s=5)
    run.let_go()
    run.close()
    run.close()
    assert held() == 1 and other.f is not None, "freed once: the other run keeps its slot"
    other.close()

    full = [checkcap.acquire("x"), checkcap.acquire("y")]
    run = checkcap.acquire("ends", max_wait_s=0.2)
    assert run is None, "both slots held"
    for s in full[1:]:
        s.close()
    run = checkcap.acquire("ends")
    run.allow_yield()
    run.f.close()
    run.f, run.yielded, run.waiters = None, True, {os.getpid()}
    full[1] = checkcap.acquire("y")
    assert not run.hold_again(stop=lambda: "its command ended") and run.f is None and held() == 2, \
        "ended while queued: it holds none"
    for s in full:
        s.close()

    code = (f"import os, sys, time; sys.path.insert(0, {str(RUNTIME)!r}); from ttp import checkcap; "
            "s = checkcap.acquire('killed'); s.allow_yield(); print(s.yield_stem, flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=dict(os.environ))
    stem = proc.stdout.readline().strip()
    assert stem and held() == 1
    monkeypatch.setenv(checkcap.YIELD_ENV, stem)
    proc.kill()
    proc.wait()
    assert held() == 0, "a killed run holds nothing"
    assert checkcap.ask_yield("resume", os.getpid()) == "the checks run that held the slot has ended"
    assert checkcap.line() == ""


def test_checks_run_gives_its_slot_up_while_its_command_waits_and_holds_one_for_the_next(env, tmp_path, monkeypatch, capsys):
    """[note d3dbe43fdffc5aab] end to end: a `ttp checks` command runs `ttp checks-yield` before it waits on an
    outside pool; another run takes the freed slot; `ttp checks-yield --resume` returns once the run holds
    one again; a command whose wait never resumed leaves the next command to run on a slot taken again."""
    from ttp import checkcap, checkwall, cli, locks
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _yield_setup(monkeypatch)
    monkeypatch.setattr(checkwall, "POLL_S", 0.05)
    helper = tmp_path / "yield.sh"
    helper.write_text(f'#!/bin/sh\nPYTHONPATH={RUNTIME} exec "{sys.executable}" -m ttp checks-yield --pid $PPID "$@"\n')
    helper.chmod(0o755)
    flag, go, seen = tmp_path / "yielded", tmp_path / "go", tmp_path / "seen"
    probe = (f"PYTHONPATH={RUNTIME} {sys.executable} -c 'from ttp import checkcap, locks; "
             f"print(len(locks.held_labels(checkcap.all_paths())))' >> {seen}")
    wait = f"{helper} && touch {flag} && while [ ! -e {go} ]; do sleep 0.05; done"
    import threading
    rcs = []
    t = threading.Thread(target=lambda: rcs.append(cli.main(
        ["checks", "--", f"{wait} && {helper} --resume && {probe}", f"{helper} && {probe}", probe])))
    t.start()
    try:
        assert _wait_for(flag.exists, 60)
        other = checkcap.acquire("other", max_wait_s=10)
        assert other is not None and other.f is not None, "the waiting run's slot went to another check"
        go.touch()
        time.sleep(0.5)
        assert not seen.exists(), "its CPU work waits for a slot again"
        other.close()
        t.join(60)
    finally:
        go.touch()
    out = capsys.readouterr().out
    assert not t.is_alive() and "passed" in out, out
    assert seen.read_text().split() == ["1", "0", "1"], "one slot for its CPU work; the next command's taken again"
    log = (run / "checks.log").read_text()
    assert log.count("gave up its host-wide checks slot") == 2, log
    assert not list(checkcap.slot_dir().glob(f"{checkcap.YIELD}.*")) and checkcap.line() == ""
    assert not locks.held_labels(checkcap.all_paths())


def test_a_twin_waiter_holds_no_load_pass(env, monkeypatch):
    """Review #6605: a routine run whose same checks already run elsewhere waits for their result and runs
    no tests, so it holds no load pass meanwhile: the floor stays free for another run parked under the guard,
    at its first try and on every poll of the wait."""
    from ttp import checkcap, locks, testslots
    _host_of_64(monkeypatch, load=84.5)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setattr(checkcap, "POLL_S", 0.05)
    d = checkcap.slot_dir()
    twin = checkcap._take(4, checkcap.paths(4), checkcap.all_paths(4), "twin run", "x", "K", label={})
    assert twin
    floor = d / f"{testslots.LOAD_FLOOR}.lock"
    assert locks.any_free([floor])
    seen = []

    class Lanes:
        def __init__(self, f):
            self.files = [f]
    lane = Lanes(locks.try_take([d / f"{testslots.SUITE}.pa.repo.0.lock"], "run a", "ttp checks"))

    def done():
        seen.append(locks.any_free([floor]))
        return False
    try:
        slot = checkcap.acquire("run a", "x", max_wait_s=0.6, project_name="pa", priority=2, same="K", done=done,
                                startable=lambda: testslots.full_startable(lane, {}, "pa", "run a", priority=2))
        assert slot is None
        other = testslots.pool_gate({}, "pb", full=True, priority=2)
    finally:
        testslots.release_load_pass()
        twin.close()
        lane.files[0].close()
    assert seen and all(seen), f"floor held during the twin wait: {seen}"
    assert other == "", other


def test_a_twin_waiter_gives_back_a_pass_it_took_before_the_twin_started(env, monkeypatch):
    """A run whose gate took the floor pass and then queued, whose same checks then started elsewhere, lets
    the pass go while it waits on them (checkcap.acquire)."""
    from ttp import checkcap, locks, testslots
    _host_of_64(monkeypatch, load=84.5)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "4")
    monkeypatch.setattr(checkcap, "POLL_S", 0.05)
    d = checkcap.slot_dir()
    floor = d / f"{testslots.LOAD_FLOOR}.lock"
    held, twin = [], []

    class Lanes:
        def __init__(self, f):
            self.files = [f]
    lane = Lanes(locks.try_take([d / f"{testslots.SUITE}.pa.repo.0.lock"], "run a", "ttp checks"))
    busy = [checkcap._take(4, checkcap.paths(4), checkcap.all_paths(4), f"busy {i}", "x", f"B{i}", label={})
            for i in range(4)]   # every slot busy: the run queues holding the pass its gate took

    def stop():
        if not twin:   # after its first try one busy run ends and its same checks (key K) take that slot
            assert not locks.any_free([floor]), "the gate took no pass"
            busy.pop().close()
            twin.append(checkcap._take(4, checkcap.paths(4), checkcap.all_paths(4), "twin run", "x", "K",
                                       label={}))
        else:
            held.append(not locks.any_free([floor]))
        return ""
    try:
        checkcap.acquire("run a", "x", max_wait_s=0.6, project_name="pa", priority=2, same="K", stop=stop,
                         startable=lambda: testslots.full_startable(lane, {}, "pa", "run a", priority=2))
    finally:
        testslots.release_load_pass()
        for f in busy + twin:
            f and f.close()
        lane.files[0].close()
    assert all(busy) and twin and twin[0], "the busy slots or the twin could not start"
    assert held and not any(held), f"floor held while the twin ran: {held}"


def test_testslots_acquire_keeps_its_ps_snapshot():
    """Review #6605: one `ps` per poll cycle (project.ps_snapshot) wraps testslots.acquire itself, not a helper
    inserted above it."""
    from ttp import testslots
    assert hasattr(testslots.acquire, "__wrapped__")
    assert not hasattr(testslots._pass_told, "__wrapped__")


def test_the_slot_a_preempted_run_frees_goes_to_the_urgent_run_that_asked_not_the_next_waiter(env, monkeypatch):
    """#6842 (1): an urgent run's preemption freed a cap slot that the next waiter in turn took, so the
    preempted run's work was lost and the urgent run still waited. Its claim keeps the freed slot from
    every other waiter; it is dropped once the asker holds a slot."""
    import threading
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")
    monkeypatch.setattr(checkcap, "POLL_S", 0.1)
    routine = checkcap.acquire("routine", project_name="pa", priority=3, preemptible=True)
    told, got, other = [], [], []
    t_other = threading.Thread(target=lambda: other.append(checkcap.acquire(
        "first in line", priority=1, since=time.time() - 600, max_wait_s=4)))
    t_other.start()
    assert _wait_for(lambda: locks_queued(checkcap)), "the other waiter is first in line"
    t = threading.Thread(target=lambda: got.append(checkcap.acquire(
        "urgent", project_name="pa", priority=1, urgent=True, tell=told.append, max_wait_s=30)))
    t.start()
    try:
        assert _wait_for(lambda: checkcap.preempt_line("routine") in told), told
        assert checkcap.claims() == 1 and len(list(checkcap.slot_dir().glob(f"{checkcap.CLAIM}.*.json"))) == 1
        routine.close()
        t.join(30)
        t_other.join(30)
        assert got and got[0] is not None and got[0].f is not None, "the asker took the freed slot"
        assert other == [None], "the waiter ahead in line did not take it"
        assert checkcap.claims() == 0 and not list(checkcap.slot_dir().glob(f"{checkcap.CLAIM}.*.json"))
    finally:
        checkcap.clear_preempt()
        for h in [routine, *got, *other]:
            if h is not None:
                h.close()


def locks_queued(checkcap):
    from ttp import locks
    return bool(locks.queued(checkcap.slot_dir(), checkcap.RESOURCE))


def test_a_claim_lapses_when_its_asker_dies_or_stops_renewing_and_its_request_then_asks_nothing(env, monkeypatch):
    """#6842 (1): a claim lives only while its asker lives and renews it (while the asked run holds its
    slot and the asker is not parked, or asked parked within PARKED_CLAIM_S), for at most CLAIM_MAX_S; a
    request whose claim lapsed or was dropped preempts nobody, and the slot goes back to the normal turn."""
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    victim = checkcap.acquire("victim", priority=3, preemptible=True)
    try:
        assert checkcap.preempt("urgent u", checkcap.all_paths(1), claim="tok1") == "victim"
        assert checkcap.claims() == 1 and checkcap.claims("tok1") == 0, "its own claim leaves its asker room"
        assert checkcap.preempted(victim.since) == "urgent urgent u"
        assert checkcap._room(1, checkcap.all_paths(1)) < checkcap._room(1, checkcap.all_paths(1), "tok1")
        assert checkcap.preempt("urgent v", checkcap.all_paths(1), claim="tok2") == "", "asked already: not twice"
        assert checkcap.claims("tok1") == 0, "and no second claim on its slot"
        path = checkcap._claim_path("tok1")
        rec = json.loads(path.read_text())
        old = time.time() - checkcap.CLAIM_S - 1
        checkcap._write(path, {**rec, "at": old})
        checkcap.renew_claim("tok1", parked=False)
        assert checkcap.claims() == 0, "a lapsed claim is not renewed"
        assert checkcap.preempted(victim.since) == "", "its request asks nothing any more"
        checkcap._write(path, {**rec, "at": old + 1})
        checkcap.renew_claim("tok1", parked=True)
        assert json.loads(path.read_text())["at"] == old + 1, "a parked asker does not renew it"
        checkcap._write(path, {**rec, "at": (stale := time.time() - 1)})
        checkcap.renew_claim("tok1", parked=True)
        assert json.loads(path.read_text())["at"] == stale, "nor a live one it did not ask parked"
        checkcap.renew_claim("tok1", parked=True, asked_parked=True)
        assert json.loads(path.read_text())["at"] > stale, "one that asked parked renews it while the asked run holds"
        checkcap._write(path, {**rec, "at": stale, "made": time.time() - checkcap.PARKED_CLAIM_S - 1})
        checkcap.renew_claim("tok1", parked=True, asked_parked=True)
        assert json.loads(path.read_text())["at"] == stale, "for at most PARKED_CLAIM_S from the ask"
        checkcap._write(path, {**rec, "at": time.time() - 1})
        checkcap.renew_claim("tok1", parked=False)
        assert json.loads(path.read_text())["at"] > time.time() - 1, "renewed while the asked run holds"
        checkcap._write(path, {**rec, "at": time.time(), "made": time.time() - checkcap.CLAIM_MAX_S - 1})
        assert checkcap.claims() == 0, "at most CLAIM_MAX_S"
        dead = subprocess.Popen(["true"])
        dead.wait()
        checkcap._write(path, {**rec, "at": time.time(), **locks._owner(dead.pid)})
        assert checkcap.claims() == 0 and checkcap.preempted(victim.since) == "", "its asker died"
        checkcap._write(path, {**rec, "at": time.time()})
        assert checkcap.claims() == 1
        checkcap.drop_claim("tok1")
        assert checkcap.claims() == 0 and checkcap.preempted(victim.since) == "", "dropped: the asker took another"
        checkcap.clear_preempt()
        assert checkcap.preempt("urgent u", checkcap.all_paths(1)) == "victim"
        assert checkcap.preempted(victim.since) == "urgent urgent u", "a request with no claim asks as before"
        victim.close()
        for holder in ("", "another run"):   # its slot freed, then taken by another run
            checkcap.drop_claim("tok1")
            other = checkcap.acquire(holder, max_wait_s=10) if holder else checkcap.Slot()
            if holder:
                locks.relabel(other.f, {"pid": 4242, "pid_start": "s1", "ns_pid": None})
            try:
                checkcap._write(path, {**rec, "at": (stale := time.time() - 1)})
                checkcap.renew_claim("tok1", parked=False)
                assert json.loads(path.read_text())["at"] == stale, f"not renewed: {holder or 'freed'}"
            finally:
                other.close()
    finally:
        checkcap.clear_preempt()
        victim.close()


def test_a_parked_asker_keeps_its_claim_while_the_asked_run_takes_longer_than_claim_s_to_free_its_slot(env, monkeypatch):
    """#6907 (review #6873): a parked urgent asker did not renew its claim, so a victim slower than CLAIM_S
    to end its command freed its slot after the claim had lapsed, and the waiter first in line took it. A
    parked asker renews it while the asked run still holds its slot (PARKED_CLAIM_S from the ask at most); it
    then takes the slot once unparked."""
    import threading
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")
    monkeypatch.setattr(checkcap, "POLL_S", 0.1)
    monkeypatch.setattr(checkcap, "CLAIM_S", 1.0)
    routine = checkcap.acquire("routine", project_name="pa", priority=3, preemptible=True)
    freed = threading.Event()
    startable = lambda: freed.is_set() or (checkcap.FREEING.get() or {}).get("holder") == "routine"  # noqa: E731
    told, got, other = [], [], []
    t_other = threading.Thread(target=lambda: other.append(checkcap.acquire(
        "first in line", priority=1, since=time.time() - 600, max_wait_s=8)))
    t_other.start()
    assert _wait_for(lambda: locks_queued(checkcap)), "the other waiter is first in line"
    t = threading.Thread(target=lambda: got.append(checkcap.acquire(
        "urgent", project_name="pa", priority=1, urgent=True, tell=told.append, startable=startable,
        max_wait_s=30)))
    t.start()
    try:
        assert _wait_for(lambda: checkcap.preempt_line("routine") in told), told
        assert checkcap.PARKED_LINE in told, "the asker waits for its test slots"
        time.sleep(3 * checkcap.CLAIM_S)   # the asked run is slow to end its command
        assert checkcap.claims() == 1, "renewed while parked and the asked run holds its slot"
        assert checkcap.preempted(routine.since) == "urgent urgent", "so its request still asks"
        routine.close()
        time.sleep(0.3)   # its test slots free a little after its cap slot
        freed.set()
        t.join(30)
        t_other.join(30)
        assert got and got[0] is not None and got[0].f is not None, "the parked asker took the freed slot"
        assert other == [None], "the waiter first in line did not take it"
        assert checkcap.claims() == 0 and not list(checkcap.slot_dir().glob(f"{checkcap.CLAIM}.*.json"))
    finally:
        freed.set()
        checkcap.clear_preempt()
        for h in [routine, *got, *other]:
            if h is not None:
                h.close()


def test_a_parked_urgent_run_preempts_only_a_run_whose_stop_lets_it_start(env, monkeypatch):
    """#6842 (2): an urgent run parked for its test slots preempted a run whose stop still left it parked,
    so the work was lost for nothing. It asks only a run with which its startable() holds once that run's
    test slots count free (FREEING), skipping a better-ranked one that would not; none such: nobody."""
    import threading
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")
    monkeypatch.setattr(checkcap, "POLL_S", 0.1)
    big = checkcap.acquire("big", priority=3, preemptible=True)
    small = checkcap.acquire("small", priority=5, preemptible=True)   # ranked first: the least urgent
    fits = {"big"}
    startable = lambda: (checkcap.FREEING.get() or {}).get("holder") in fits   # noqa: E731
    told = []
    try:
        assert checkcap.acquire("urgent", priority=1, urgent=True, tell=told.append, startable=lambda: False,
                                max_wait_s=1.0) is None
        assert not any(m.startswith("urgent checks with every") for m in told), "no stop lets it start: nobody"
        assert not list(checkcap.slot_dir().glob(f"{checkcap.PREEMPT}.*.json"))
        assert not list(checkcap.slot_dir().glob(f"{checkcap.CLAIM}.*.json")), "no claim left behind"
        got = []
        t = threading.Thread(target=lambda: got.append(checkcap.acquire(
            "urgent", priority=1, urgent=True, tell=told.append, startable=startable, max_wait_s=1.0)))
        t.start()
        t.join(30)
        assert checkcap.preempt_line("big") in told, told
        assert checkcap.preempt_line("small") not in told
        assert checkcap.FREEING.get() is None, "the weighing ends with the call"
    finally:
        checkcap.clear_preempt()
        big.close()
        small.close()


def test_the_test_pool_and_a_projects_budget_count_a_run_being_weighed_for_preemption_as_stopped(env, monkeypatch):
    """#6842 (2): while preempt weighs a run (checkcap.FREEING), the host's test pool and its project's
    budget count that run's test slots free (testslots._held), and only that run's."""
    from ttp import checkcap, locks, testslots
    _lane_env(monkeypatch)
    monkeypatch.setattr(testslots, "sizes", lambda *a, **k: (2, 1))
    monkeypatch.setattr(testslots, "pool_now", lambda cfg, pool, held, *a, **k: pool)
    taken = [locks.try_take(testslots.paths(2), "a run", "test slots") for _ in range(2)]
    own = testslots.own_paths("pa", 2)
    mine = [locks.try_take(own, "a run", "test slots") for _ in range(2)]
    try:
        assert all(taken) and all(mine) and not testslots.pool_room({}, "pa")
        victim = {"pid": 4242, "pid_start": "s1"}
        locks.relabel(taken[0], victim)
        locks.relabel(mine[0], victim)
        assert testslots._own_free("pa", own) == 0
        tok = checkcap.FREEING.set({**victim, "holder": "victim"})
        try:
            assert testslots.pool_room({}, "pa"), "its stop leaves room"
            assert testslots._own_free("pa", own) == 1, "its project's budget slot counts free"
        finally:
            checkcap.FREEING.reset(tok)
        tok = checkcap.FREEING.set({"pid": 4242, "pid_start": "other start"})
        try:
            assert not testslots.pool_room({}, "pa"), "a reused pid is another process"
        finally:
            checkcap.FREEING.reset(tok)
        assert not testslots.pool_room({}, "pa")
    finally:
        for f in taken + mine:
            if f:
                f.close()


def test_a_requeued_run_is_labelled_so_while_it_waits_and_not_once_it_holds_a_slot(env, monkeypatch):
    """#6842 (3): a preempted run that got its slot back still read '[preempted by urgent #N]' as if stuck.
    While it queues again its label says 'requeued after preemption'; the slot it takes is labelled without it."""
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv("TTP_TASK", "31")
    monkeypatch.setenv("TTP_RUN_ID", "7")
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "urgent #12")
    who = checkcap.label("pa", "ttp checks")
    assert who == "pa task #31 (run 7) [requeued after preemption by urgent #12] ttp checks"
    slot = checkcap.acquire(who, project_name="pa", priority=3)
    try:
        assert checkcap.state()["running"] == ["pa task #31 (run 7) ttp checks"], checkcap.state()
        assert "preempt" not in checkcap.line()
    finally:
        slot.close()
    monkeypatch.delenv(checkcap.PREEMPTED_ENV)
    assert checkcap.label("pa") == "pa task #31 (run 7)"


def test_a_preempted_checks_run_starts_ahead_of_runs_that_queued_after_it_at_its_priority(env, monkeypatch):
    """#6883: R1 queued at t0 and was preempted at t1 by an urgent run of its own project; R2..R4 (its project
    and two others) queued between t0 and t1 at its priority. When the slot frees, R1 starts first, though its
    project's turn went to the urgent run; a waiter that arrived before R1 or is more urgent still goes first."""
    import threading
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    monkeypatch.setattr(checkcap, "POLL_S", 0.05)
    t0 = time.time() - 600
    blocker = checkcap.acquire("urgent run", project_name="mine", priority=1)   # took R1's slot at t1
    order, threads = [], []

    def wait(name, project, since, prio=3):
        def run():
            s = checkcap.acquire(name, project_name=project, priority=prio, since=since, max_wait_s=30)
            order.append(name)
            if s is not None:
                s.close()
        threads.append(threading.Thread(target=run))
        threads[-1].start()
        assert _wait_for(lambda: len(locks.queued(checkcap.slot_dir(), checkcap.RESOURCE)) == len(threads))

    wait("R2 mine", "mine", t0 + 10)
    wait("R3 other", "other", t0 + 20)
    wait("R4 third", "third", t0 + 30)
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "urgent #9")
    wait("R1 mine", "mine", t0)
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    try:
        q = locks.fair_order(checkcap.slot_dir(), checkcap.RESOURCE, **checkcap.order())
        names = [t.read_text() for t in q]
        assert names[0] == "R1 mine", names
        assert [locks.is_requeued(t) for t in q].count(True) == 1
        blocker.close()
        for t in threads:
            t.join(30)
        assert order == ["R1 mine", *names[1:]], order
    finally:
        blocker.close()
    # It never passes a waiter that arrived before it or a more urgent one.
    d = checkcap.slot_dir()
    tk = {n: locks.enqueue(d, "place", n, since, group="g", priority=pr, requeued=n == "R")
          for n, since, pr in (("early p4", t0 - 10, 4), ("urgent", t0 + 5, 1), ("later p3", t0 + 6, 3),
                               ("later p5", t0 + 7, 5), ("R", t0, 3))}
    try:
        got = locks.requeued_place([tk[n] for n in ("early p4", "urgent", "later p3", "later p5", "R")])
        assert [t.read_text() for t in got] == ["early p4", "urgent", "R", "later p3", "later p5"]
        assert locks.requeued_place([tk["R"], tk["later p3"]]) == [tk["R"], tk["later p3"]]
    finally:
        for t in tk.values():
            locks.dequeue(t)


def test_preemption_asks_a_run_never_preempted_before_over_one_already_preempted(env, monkeypatch):
    """#6883: with two eligible routine runs on the cap, the one already preempted once (the latest granted, of
    the asker's project) is not asked again: the other one is. Alone, it can still be asked."""
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    fresh = checkcap.acquire("fresh run", project_name="other", priority=2, preemptible=True)
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "urgent #9")
    again = checkcap.acquire(checkcap.label("mine"), project_name="mine", priority=3, preemptible=True)
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    try:
        every = checkcap.all_paths(2)
        assert checkcap.preempt("urgent", every, "mine") == "fresh run"
        checkcap.clear_preempt()
        for f in checkcap.slot_dir().glob(f"{checkcap.PREEMPT}.*.json"):
            f.unlink()
        fresh.close()
        assert checkcap.preempt("urgent", every, "mine") == "mine", "the only one left (its slot: no requeue text)"
    finally:
        for f in checkcap.slot_dir().glob(f"{checkcap.PREEMPT}.*.json"):
            f.unlink()
        fresh.close()
        again.close()


def test_project_max_from_names_the_setting_that_binds_the_test_worker_budget(env, monkeypatch):
    """Task #7176: testslots.project_max_from names what project_max() took its value from, in its order: the
    environment variable over the account's, a project's own only lowering either, then the pool and the
    host's per-project worker ceiling bounding all; the value always matches project_max()."""
    from ttp import project, testslots
    for k in ("PROJECT_SLOTS", "PROJECT_MAX", "PROJECT_WORKERS", "RUN_MIN"):
        monkeypatch.delenv(f"TTP_TEST_{k}", raising=False)
    monkeypatch.setenv("TTP_TEST_POOL", "16")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "8")
    workers = {"n": 64}
    monkeypatch.setattr(testslots, "project_workers", lambda cfg=None, cores=None, project_name=None: workers["n"])
    s = (16, 8)
    own = lambda n: {"checks": {"test_project_slots": n}}   # noqa: E731
    for acct, cases in ((None, [({}, ""), (own(12), "project"), (own(32), "pool")]),
                        (8, [({}, "account"), (own(32), "account"), (own(4), "run")])):
        if acct:
            project.set_account_setting("checks.test_project_slots", acct)
        for cfg, want in cases:
            assert testslots.project_max_from(cfg, s) == want, (cfg, want, testslots.project_max(cfg, s))
    monkeypatch.setenv("TTP_TEST_PROJECT_SLOTS", "12")
    assert testslots.project_max_from({}, s) == "env" and testslots.project_max({}, s) == 12, \
        "the environment variable wins over the account's"
    assert testslots.project_max_from(own(10), s) == "project" and testslots.project_max(own(10), s) == 10
    workers["n"] = 9
    assert testslots.project_max_from({}, s) == "workers" and testslots.project_max({}, s) == 9
    assert testslots.project_max_from({}, (0, 0)) == "", "the pool off: no budget"


# `ttp checks park|cancel|resume|queue` (checksqueue, #7120).

def _no_push_env(monkeypatch):
    """This process (each test's queued run) is no push batch whatever runs the tests: the push queue runs
    them with TTP_PUSH_MODE=target, which checksqueue.push_batch reads from /proc/<pid>/environ, fixed at exec."""
    from ttp import landmark, procowner
    real, me, drop = procowner.environ, os.getpid(), ("TTP_PUSH_MODE", landmark.ENV)
    for k in drop:
        monkeypatch.delenv(k, raising=False)

    def environ(pid):
        env = real(pid)
        return {k: v for k, v in env.items() if k not in drop} if env is not None and pid == me else env
    monkeypatch.setattr(procowner, "environ", environ)


def _queue_run(p, tid, wait=True, own=True):
    """A run folder of task `tid` (a runs row) whose live checks (this process) wait in the queue, or with
    wait=False hold a slot."""
    from ttp import cli
    from ttp.project import write_json
    from ttp.runner import proc_start
    rid = p.db.x("INSERT INTO runs(task,role,status,started) VALUES(?,'worker','running',?)", (tid, time.time()))
    d = p.runs / str(rid)
    d.mkdir(parents=True, exist_ok=True)
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(d), rid))
    if own:
        write_json(d / cli.CHECKS_PID, {"pid": os.getpid(), "started": proc_start(os.getpid())})
    if wait:
        write_json(d / cli.CHECKS_SLOT_WAIT, {"pid": os.getpid(), "since": time.time() - 60, "head": "abc"})
    return rid, d


def test_checks_park_frees_the_queue_place_and_resumes_at_its_first_arrival_once_its_probe_passes(env, tmp_path,
                                                                                                    monkeypatch):
    import threading
    from ttp import checkcap, checksqueue, cli, locks
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _no_push_env(monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checksqueue, "PROBE_S", 0)
    monkeypatch.setattr(checksqueue, "POLL_S", 0.05)
    tickets = lambda: [float(x.name.split("-")[0]) for x in locks.queued(checkcap.slot_dir(), checkcap.RESOURCE)]
    held = checkcap.acquire("other")
    ran, flag, done = tmp_path / "ran", tmp_path / "flag", []

    def go():
        try:
            done.append(cli.main(["checks", "--", "sh", "-c", f"touch {ran}"]))
        except SystemExit as e:
            done.append(e.code)
    t = threading.Thread(target=go)
    t.start()
    try:
        assert _wait_for(lambda: (run / cli.CHECKS_SLOT_WAIT).exists() and len(tickets()) == 1)
        first = tickets()[0]
        code, text = checksqueue.act(p, run, "park", "the coordinator", "user msg #1: let p1 go first",
                                     f"test -e {flag}")
        assert code == 0 and "parked" in text and f"resumes when `test -e {flag}` passes" in text, text
        # Its queue place goes before its parked wait is written (checksqueue.hold): wait for both.
        parked = lambda: "parked" in checksqueue._json(run / cli.CHECKS_SLOT_WAIT)
        assert _wait_for(lambda: not tickets() and parked()), "a parked run holds no queue place"
        wait = json.loads((run / cli.CHECKS_SLOT_WAIT).read_text())
        assert wait["note"].startswith("checks run parked by the coordinator: user msg #1: let p1 go first"), wait
        assert not ran.exists()
        flag.touch()
        assert _wait_for(lambda: len(tickets()) == 1), "the probe passed: it queues again"
        assert tickets()[0] == first, "it queues again at its first arrival, never at the back"
        assert not (run / checksqueue.PARK).exists()
    finally:
        flag.touch()   # a failed assert never leaves the thread parked or queued past teardown
        held.close()
        t.join(30)
    assert not t.is_alive() and ran.exists(), "resumed, it took the slot and ran"
    acts = [json.loads(x)["action"] for x in (run / checksqueue.LOG).read_text().splitlines()]
    assert acts == ["park", "resume"], acts
    assert "resumed from parking: queuing again at its first arrival" in (run / "checks.log").read_text()


def test_a_parked_checks_run_is_no_leader_to_join_until_it_resumes(env, tmp_path, monkeypatch):
    import contextlib
    import threading
    from ttp import checkcap, checkjoin, checksqueue, cli, locks
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _no_push_env(monkeypatch)
    monkeypatch.setenv("TTP_TASK", str(p.db.add_task("t", "s", kind="code")))
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checksqueue, "PROBE_S", 0)
    monkeypatch.setattr(checksqueue, "POLL_S", 0.05)
    tickets = lambda: locks.queued(checkcap.slot_dir(), checkcap.RESOURCE)
    held = checkcap.acquire("other")
    flag, done = tmp_path / "flag", []
    mine = lambda: [r for r in checkjoin.records(p.state, prune=False) if r.get("run") == run.name]

    def go():
        try:
            done.append(cli.main(["checks", "--", "true"]))
        except SystemExit as e:
            done.append(e.code)
    t = threading.Thread(target=go, daemon=True)   # a stuck thread never holds the test worker past its exit
    t.start()
    try:
        # Its queue ticket is taken before its wait is recorded: park only once both show (as the park test does).
        assert _wait_for(lambda: (run / cli.CHECKS_SLOT_WAIT).exists() and len(tickets()) == 1 and len(mine()) == 1)
        a = mine()[0]
        code, text = checksqueue.act(p, run, "park", "the coordinator", "user msg #1: p1 first", f"test -e {flag}")
        assert code == 0, text
        assert _wait_for(lambda: not tickets() and mine()[0].get("phase") == checkjoin.PARKED)
        # Another task's run of the same checks (same key, through the real writer) queues after it.
        b_path = checkjoin.register(p.state, task="999", head=a["head"], commit_ts=a["commit_ts"], key=a["key"],
                                    group="other-group", run="77", arrived=time.time() + 5)
        b = checkjoin.read(b_path)
        assert checkjoin.target(b, checkjoin.records(p.state, prune=False)) is None, "a parked run is never joined"
        flag.touch()
        assert _wait_for(lambda: len(tickets()) == 1 and mine()[0].get("phase") == checkjoin.WAITING)
        assert mine()[0]["arrived"] == a["arrived"], "it waits again at its first arrival"
        got = checkjoin.target(b, checkjoin.records(p.state, prune=False))
        assert got and got[0]["id"] == a["id"] and got[1] == checkjoin.JOINED, got
        b_path.unlink()
    except BaseException:
        with contextlib.suppress(Exception):   # never left queued; the assert's own error still shows
            checksqueue.act(p, run, "cancel", "the test", "a failed assert", "")
        raise
    finally:
        flag.touch()   # a failed assert never leaves the thread parked or queued past teardown
        held.close()
        t.join(30)
    assert not t.is_alive() and done in ([0], [None]), done


def test_a_run_that_joined_one_later_parked_queues_again_at_its_place(env, tmp_path, monkeypatch):
    import io
    from ttp import checkjoin, cli
    p = make(env)
    monkeypatch.setenv("TTP_DETACHED", "1")
    lead = checkjoin.register(p.state, task="1", head="h", commit_ts=0, key="k", group="g1", run="11", arrived=1)
    me = checkjoin.register(p.state, task="2", head="h", commit_ts=0, key="k", group="g2", run="12", arrived=2)
    got = checkjoin.target(checkjoin.read(me), checkjoin.records(p.state, prune=False))
    assert got and got[1] == checkjoin.JOINED
    checkjoin.update(lead, phase=checkjoin.PARKED)   # parked after this run joined it
    run = tmp_path / "run"
    run.mkdir()
    out = io.StringIO()
    assert cli._checks_follow(p, run, out, "h", me, checkjoin.Yield(*got), detached=True) is None
    assert "was parked; queueing again at the same place" in out.getvalue()


def test_checks_resume_lets_a_parked_run_queue_again(env, tmp_path, monkeypatch):
    from ttp import checksqueue
    from ttp.runner import proc_start
    p = make(env)
    _no_push_env(monkeypatch)
    tid = p.db.add_task("t", "s", kind="code")
    rid, d = _queue_run(p, tid)
    monkeypatch.setattr(checksqueue, "POLL_S", 0.01)
    assert checksqueue.act(p, d, "park", "the user", "why", "")[0] == 0
    req = checksqueue.request(d)
    assert req["pid"] == os.getpid() and req["started"] == proc_start(os.getpid())
    assert checksqueue.act(p, d, "resume", "the user", "", "")[0] == 0
    waits = []
    assert checksqueue.hold(p, d, "abc", req, lambda: "", waits.append) == "" and waits[0]["parked"] == req
    assert checksqueue.act(p, d, "resume", "the user", "", "")[0] == 3, "not parked any more"


def test_checks_cancel_drops_a_queued_run_and_logs_who_and_why(env, monkeypatch):
    from ttp import checksqueue, cli
    p = make(env)
    _no_push_env(monkeypatch)
    tid = p.db.add_task("t", "s", kind="code")
    rid, d = _queue_run(p, tid)
    assert checksqueue.act(p, d, "cancel", "the coordinator (run 7)", "user msg #2: routine", "")[0] == 0
    assert cli._checks_cancelled(d) == "cancelled by the coordinator (run 7): user msg #2: routine"
    rec = json.loads((d / checksqueue.LOG).read_text())
    assert (rec["action"], rec["who"], rec["why"], rec["task"]) == ("cancel", "the coordinator (run 7)",
                                                                     "user msg #2: routine", tid)
    ev = p.db.one("SELECT * FROM events WHERE kind=?", (checksqueue.EVENT,))
    assert ev and ev["status"] == "handled" and ev["task"] == tid and "cancel by the coordinator" in ev["text"]
    assert "checks: cancel by the coordinator (run 7)" in (d / "progress.md").read_text()
    assert p.db.task(tid)["status"] == "queued" and p.db.task(tid).get("attempts", 0) == 0, "no attempt spent"
    # A parked run that is then cancelled stops waiting with the cancel's reason.
    rid2, d2 = _queue_run(p, tid)
    assert checksqueue.act(p, d2, "park", "the user", "why", "")[0] == 0
    req = checksqueue.request(d2)
    assert checksqueue.act(p, d2, "cancel", "the user", "drop it", "")[0] == 0
    assert checksqueue.hold(p, d2, "abc", req, lambda: cli._checks_cancelled(d2), lambda w: None) \
        == "cancelled by the user: drop it"


def test_checks_park_and_cancel_refuse_a_slot_holder_a_push_batch_and_another_projects_run(env, tmp_path,
                                                                                         monkeypatch):
    from ttp import checksqueue, cli, procowner
    p = make(env)
    _no_push_env(monkeypatch)
    tid = p.db.add_task("t", "s", kind="code")
    _, held = _queue_run(p, tid, wait=False)
    for action in ("park", "cancel"):
        code, text = checksqueue.act(p, held, action, "the user", "why", "")
        assert code == 3 and "hold a slot" in text, text
    _, d = _queue_run(p, tid)
    monkeypatch.setattr(procowner, "environ", lambda pid: {"TTP_PUSH_MODE": "checks"})
    assert not checksqueue.push_batch({"pid": 1}), "a run inside `ttp checks` (mode checks) is no push batch"
    monkeypatch.setattr(procowner, "environ", lambda pid: {"TTP_PUSH_MODE": "own"})
    assert not checksqueue.push_batch({"pid": 1}), "a task's own-branch push is no push batch"
    monkeypatch.setattr(procowner, "environ", lambda pid: {"TTP_PUSH_MODE": "target"})
    for action in ("park", "cancel"):
        code, text = checksqueue.act(p, d, action, "the user", "why", "")
        assert code == 3 and "push batch" in text, text
    assert not (d / checksqueue.PARK).exists() and not (d / checksqueue.LOG).exists()
    od = tmp_path / "other" / "tt-project" / "state" / "runs" / "5"   # another project's run folder
    od.mkdir(parents=True)
    for f in (cli.CHECKS_PID, cli.CHECKS_SLOT_WAIT):
        (od / f).write_text((d / f).read_text())
    for target in (str(od), str(od.parent.parent / "x")):
        with pytest.raises(ValueError, match="refused: .* is no checks run of project demo"):
            checksqueue.resolve(p, target, "", "park")
    monkeypatch.delenv("TTP_TASK", raising=False)
    monkeypatch.delenv("TTP_RUN_ID", raising=False)
    monkeypatch.setenv("TTP_ROLE", "coordinator")
    assert checksqueue.main(p, ["cancel", str(od), "--reason", "x"]) == 3
    assert not (od / checksqueue.LOG).exists()


def test_checks_park_callers_the_coordinator_and_an_ordered_urgent_worker_only(env, monkeypatch, capsys):
    from ttp import checksqueue, coordtools
    p = make(env)
    monkeypatch.setattr(coordtools, "coordinator_run", lambda: False)
    monkeypatch.delenv("TTP_TASK", raising=False)
    monkeypatch.delenv("TTP_RUN_ID", raising=False)
    monkeypatch.setenv("TTP_ROLE", "coordinator")
    assert checksqueue.caller(p, "park") == ("the coordinator (run ?)", "")
    crun = p.db.x("INSERT INTO runs(role,status,started) VALUES('coordinator','running',?)", (time.time(),))
    monkeypatch.setenv("TTP_RUN_ID", str(crun))
    assert checksqueue.caller(p, "cancel") == (f"the coordinator (run {crun})", "")
    monkeypatch.setenv("TTP_RUN_ID", "999999")
    assert "no coordinator run" in checksqueue.caller(p, "park")[1]
    monkeypatch.setenv("TTP_ROLE", "worker")
    order = "Do it.\n\nOwner's order (user msg #1972): park routine checks runs with `ttp checks park`."
    cases = [(3, order, "not urgent or priority 1"), (1, "plain spec, nothing about the queue", "gives no order"),
             (1, "park them\n\nper user msg #5", "gives no order"), (1, order, ""),
             (0, "Cancel the queued runs, as the coordinator ordered.", ""),
             # Daemon notes about tasks name the coordinator and park/cancel variants: no order to the queue.
             (1, "Coordinator note: the coordinator is cancelling #6862.", "gives no order"),
             (1, "Coordinator note: #6862 was cancelled and its checks run parked (user msg #12).", "gives no order"),
             (1, "Cancel #6862, as the coordinator ordered.", "gives no order"),
             (1, "Park the car.\n\nThe coordinator says checks runs are slow.", "gives no order"),
             (1, "Park the checks runs if they block you.", "gives no order"),
             (1, "Per user msg #7: run `ttp checks cancel 123 --reason r`.", "")]
    for prio, spec, want in cases:
        tid = p.db.add_task("t", spec, kind="code", priority=prio)
        rid = p.db.x("INSERT INTO runs(task,role,status,started) VALUES(?,'worker','running',?)",
                     (tid, time.time()))
        monkeypatch.setenv("TTP_TASK", str(tid))
        monkeypatch.setenv("TTP_RUN_ID", str(rid))
        who, no = checksqueue.caller(p, "park")
        assert (want in no and not who) if want else (who == f"task #{tid} (run {rid})" and not no), (prio, spec, no)
    monkeypatch.setenv("TTP_RUN_ID", "1")   # a run of another task
    assert "no task run of this project" in checksqueue.caller(p, "park")[1]
    # An unordered worker is refused before anything is touched, with exit 3.
    tid = p.db.add_task("t", "plain", kind="code", priority=3)
    rid, d = _queue_run(p, tid)
    monkeypatch.setenv("TTP_TASK", str(tid))
    monkeypatch.setenv("TTP_RUN_ID", str(rid))
    assert checksqueue.main(p, ["park", "--task", str(tid), "--reason", "x"]) == 3
    assert "refused" in capsys.readouterr().err and not (d / checksqueue.PARK).exists()


def test_checks_park_task_parks_its_waiting_runs_and_the_queue_lists_them(env, monkeypatch, capsys):
    from ttp import checksqueue, coordtools
    p = make(env)
    _no_push_env(monkeypatch)
    monkeypatch.setattr(coordtools, "coordinator_run", lambda: False)
    spec = "Urgent.\n\nThe coordinator orders: park the routine checks runs (user msg #1972)."
    me = p.db.add_task("me", spec, kind="code", priority=1)
    mrun = p.db.x("INSERT INTO runs(task,role,status,started) VALUES(?,'worker','running',?)",
                  (me, time.time()))
    tid = p.db.add_task("routine", "s", kind="code", priority=3)
    _, d1 = _queue_run(p, tid)
    _, d2 = _queue_run(p, tid, wait=False)   # holds a slot: --task leaves it alone
    assert checksqueue.main(p, ["queue", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["run"], r["task"], r["priority"], r["kind"], r["owed"]) for r in rows] \
        == [(d1.name, tid, 3, "task", False)], rows
    monkeypatch.setenv("TTP_TASK", str(me))
    monkeypatch.setenv("TTP_RUN_ID", str(mrun))
    assert checksqueue.main(p, ["park", "--task", str(tid), "--reason", "user msg #1972: p1 first"]) == 0
    assert f"run {d1.name}: parked; checks run parked by task #{me}" in capsys.readouterr().out
    assert (d1 / checksqueue.PARK).exists() and not (d2 / checksqueue.PARK).exists()
    assert checksqueue.main(p, ["resume", "--task", str(tid)]) == 0
    assert not (d1 / checksqueue.PARK).exists()
    assert checksqueue.main(p, ["resume", "--task", str(tid)]) == 1, "nothing parked any more"
    assert checksqueue.main(p, ["park", "--task", str(tid)]) == 2, "--reason is required"
    assert checksqueue.main(p, ["cancel", "--task", str(tid), "--reason", "r", "--until", "true"]) == 2


def test_checks_queue_orders_and_ages_rows_by_first_arrival_not_the_latest_wait(env, capsys):
    import subprocess
    from ttp import checkcap, checksqueue, cli
    from ttp.project import write_json
    p = make(env)
    now = time.time()
    # Detached: checks.pid's arrived is an hour ago, though a re-wait reset the wait's since a minute ago.
    _, old = _queue_run(p, p.db.add_task("old", "s", kind="code"))
    write_json(old / cli.CHECKS_PID, {**json.loads((old / cli.CHECKS_PID).read_text()), "arrived": now - 3600})
    write_json(old / cli.CHECKS_SLOT_WAIT, {"pid": os.getpid(), "since": now - 60, "head": "abc"})
    # Neither: its wait's since, 2 minutes ago.
    _, new = _queue_run(p, p.db.add_task("new", "s", kind="code"))
    write_json(new / cli.CHECKS_SLOT_WAIT, {"pid": os.getpid(), "since": now - 120, "head": "abc"})
    # Foreground (no checks.pid): the arrival it was handed in its environment, 30 minutes ago.
    fgp = subprocess.Popen(["sleep", "60"], env={**os.environ, checkcap.ARRIVED_ENV: repr(now - 1800)})
    try:
        _, fg = _queue_run(p, p.db.add_task("fg", "s", kind="code"), own=False, wait=False)
        write_json(fg / cli.CHECKS_SLOT_WAIT, {"pid": fgp.pid, "since": now - 10, "head": "abc"})
        rows = checksqueue.queued(p)
        assert [r["run"] for r in rows] == [old.name, fg.name, new.name], rows
        assert [round(now - r["since"]) for r in rows] == [3600, 1800, 120], rows
        assert checksqueue.main(p, ["queue"]) == 0
        assert re.search(r"waiting 36\d\ds", capsys.readouterr().out.splitlines()[0])
    finally:
        fgp.kill()
        fgp.wait()


def test_a_precheck_stops_counting_as_read_once_its_review_is_pushing_and_may_then_be_stopped(env, monkeypatch):
    from ttp import checklane, checksqueue, jobstop
    p = make(env)
    tid = p.db.add_task("t", "s", kind="code")
    head = "5a2b0978dd8f"
    d = p.state / "prechecks" / f"t{tid}-{head}"
    d.mkdir(parents=True)
    rc = d / "checks.rc"
    p.db.x("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    assert f"task #{tid} is running" in jobstop.may_stop(p, rc, None)[1], "its code task still runs"
    assert "may still be read" in jobstop.precheck_read(p, d)
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    # no review reads it (sa24): `ttp detach --stop` may stop it; `ttp checks cancel` waits for its review's label
    assert jobstop.may_stop(p, rc, None) == (True, f"task #{tid} is done")
    assert "no review names" in jobstop.precheck_read(p, d), "started just before its review is queued"
    rid = p.db.add_task("review", "r", kind="review", labels=[f"precheck:t{tid}-{head}"])
    for status in ("queued", "running"):
        p.db.x("UPDATE tasks SET status=? WHERE id=?", (status, rid))
        assert checklane.open_precheck(p.db, str(tid), f"run t{tid}-{head}")
        ok, why = jobstop.may_stop(p, rc, None)
        assert not ok and "still reads" in why, why
    p.db.x("UPDATE tasks SET status='pushing' WHERE id=?", (rid,))
    assert not checklane.open_precheck(p.db, str(tid), f"run t{tid}-{head}"), "past its verdict: read no more"
    assert not checklane.open_precheck(p.db, str(tid), f"run precheck-t{tid}-{head}")
    assert jobstop.may_stop(p, rc, None)[0]
    assert jobstop.task_of(p, rc)["id"] == tid
    # `ttp checks cancel` resolves the precheck by its run id and refuses while a review still reads it.
    assert checksqueue.resolve(p, f"precheck-t{tid}-{head}", "", "cancel") == [Path(os.path.realpath(d))]
    p.db.x("UPDATE tasks SET status='running' WHERE id=?", (rid,))
    code, text = checksqueue.act(p, Path(os.path.realpath(d)), "cancel", "the user", "why", "")
    assert code == 3 and "still reads" in text, text


def test_checks_queue_commands_take_full_option_names_only(env, capsys):
    import pytest
    from ttp import checksqueue
    p = make(env)
    for cmd in (["park", "5", "--reason", "x", "--unti", "touch /tmp/pwn"], ["park", "5", "--u=touch /tmp/pwn"]):
        with pytest.raises(SystemExit) as e:
            checksqueue.main(p, cmd)
        assert e.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err, cmd


def test_session_coordinator_may_run_queue_commands_with_quoted_reasons_and_read_only_probes():
    from ttp import coordtools
    ok = ["ttp checks queue", "ttp checks park 14111 --reason 'user msg #1972: p1 first'",
          "ttp checks cancel --task 6844 --reason \"routine; owner's order\"",
          "ttp checks park 5 --reason x --until 'ttp checks --result /x/runs/9'",
          "ttp checks park 5 --reason x --until 'test \"$(date +%s)\" -ge 1700000000'",
          "ttp checks park 5 --reason x --until='ttp landed --task 7'", "ttp checks resume 5"]
    for cmd in ok[1:]:
        assert coordtools.queue_refusal(cmd) is None, cmd
    assert coordtools.bash_refusal(ok[0]) is None
    bad = ["ttp checks park 5 --reason x; rm -rf /tmp/x", "ttp checks park 5 --reason \"$(id)\"",
           "ttp checks park 5 --reason x --until 'rm -f /tmp/x'", "ttp checks park 5 --reason x --until 'ttp push'",
           "ttp checks park 5 --reason x --until 'ttp checks --result a; touch b'", "ttp checks --detach",
           "ttp push --detach", "ttp checks park 5 --reason x > /tmp/f",
           # argparse abbreviations of --until and unknown options never slip past (review #7268)
           "ttp checks park 5 --reason x --unti 'touch /tmp/pwn'", "ttp checks park 5 --reason x --u='touch /tmp/pwn'",
           "ttp checks park 5 --reason x --un 'touch /tmp/pwn'", "ttp checks park 5 --reason x --fresh",
           "ttp checks park 5 --reason x -h", "ttp checks park 5 --reason x -- 'touch /tmp/pwn'",
           "ttp checks park 5 --reason --until 'touch /tmp/pwn'"]
    for cmd in bad:
        assert coordtools.queue_refusal(cmd) is not None, cmd
    assert coordtools.queue_refusal("ttp checks park 5 --reason='a b' --task=6 --json") is None
    rules = coordtools._rules("demo")
    assert "Bash(ttp checks park *)" in rules and "Bash(ttp checks queue)" in rules


def test_a_preempted_checks_run_resumes_its_pytest_check_from_the_files_it_passed(env, tmp_path, monkeypatch, capsys):
    """#7218: a routine `ttp checks` run preempted mid-pytest queues again and its rerun skips the test files
    that passed before the preemption on the same tree and env (pushpass.resume_env, ttp_pytest_resume); the
    file cut short and the rest run, and the run passes as a full one."""
    from ttp import checkcap, checkwall, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkwall, "POLL_S", 0.1)
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    monkeypatch.delenv(checkcap.RESUME_ENV, raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    ran, mark = tmp_path / "ran.txt", tmp_path / "preempted-once"
    req = checkcap.slot_dir() / f"{checkcap.PREEMPT}.{os.getpid()}.json"
    suite = repo / "suite"
    suite.mkdir()
    for name in ("a", "b"):
        (suite / f"test_{name}.py").write_text(f"def test_{name}():\n    open({str(ran)!r}, 'a').write('{name}\\n')\n")
    (suite / "test_c.py").write_text(
        "import json, os, time\n"
        "def test_c():\n"
        f"    open({str(ran)!r}, 'a').write('c\\n')\n"
        f"    if os.path.exists({str(mark)!r}):\n        return\n"
        f"    open({str(mark)!r}, 'w').close()\n"
        f"    open({str(req)!r}, 'w').write(json.dumps({{'by': 'urgent u', 'task': '77', 'at': time.time() + 1}}))\n"
        "    time.sleep(60)\n")
    subprocess.run([*git, "add", "suite"], check=True)
    subprocess.run([*git, "commit", "-qm", "suite"], check=True)
    t0 = time.time()
    cli.main(["checks", "--", "python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "suite"])
    out = capsys.readouterr().out
    assert time.time() - t0 < 50, "its command ended once asked"
    assert "preempted by urgent #77" in out, out
    after = out.rsplit("preempted by", 1)[1]
    assert "resuming" in after and "2 test file(s) passed" in after, after
    assert "passed" in after.rsplit("resuming", 1)[1], "the resumed run ran to its pass"
    assert ran.read_text().split() == ["a", "b", "c", "c"], "only the file cut short ran again"
    assert checkcap.RESUME_ENV in checkcap.RUN_ONLY_ENV, "a nested `ttp checks` in a check never resumes"
    assert not req.exists()


def test_a_checks_resume_record_is_kept_only_for_a_preempted_rerun_on_the_same_tree_and_env(env, tmp_path, monkeypatch):
    """#7218: a check's resume record (cli._checks_resume) is keyed on the tree, the command and its env, so a
    changed tree or env skips nothing; any run but a preempted run's rerun starts it empty; a check that cannot
    resume (not one plain local pytest run, or under pytest-xdist) gets none."""
    from ttp import checkcap, cli
    p = make(env)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    top, c = tmp_path, "python3 -m pytest -q suite"
    run_env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTHONPATH")}

    def record(tree, e=run_env):
        vars_, _ = cli._checks_resume(p, top, tree, c, e)
        return Path(vars_["TTP_PYTEST_RESUME"])

    monkeypatch.setenv(checkcap.RESUME_ENV, "1")
    record("t1").write_text("suite/test_a.py\nsuite/test_b.py\n")
    assert cli._checks_resume(p, top, "t1", c, run_env)[1] == 2, "the preempted rerun skips its passed files"
    assert cli._checks_resume(p, top, "t2", c, run_env)[1] == 0, "another tree skips nothing"
    other = {**run_env, "PYTEST_PLUGINS": "some_plugin"}
    assert record("t1", other) != record("t1"), "another env keys another record"
    assert cli._checks_resume(p, top, "t1", c, other)[1] == 0, "another env skips nothing"
    monkeypatch.delenv(checkcap.RESUME_ENV)
    assert cli._checks_resume(p, top, "t1", c, run_env)[1] == 0, "a run not preempted starts afresh"
    monkeypatch.setenv(checkcap.RESUME_ENV, "1")
    assert cli._checks_resume(p, top, "t1", c, run_env)[1] == 0, "its record was emptied"
    for cannot in ("sh -c 'pytest -q'", "pytest -q a.py && pytest -q b.py", f"{c} -n 4", f"{c} -nauto",
                   f"{c} --numprocesses=2", f"{c} --dist load"):
        assert cli._checks_resume(p, top, "t1", cannot, run_env) == ({}, 0), cannot
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n 3")
    assert not cli._checks_resumes(c), "xdist from PYTEST_ADDOPTS turns the resume plugin off"


def test_a_checks_run_that_cannot_resume_is_never_preempted_past_90_percent(env, tmp_path, monkeypatch):
    """#7218: a run whose checks cannot resume from their passed files labels its slot with its log
    (cli._checks_progress); preempt never asks it once the log's last pytest `[ NN%]` is SHIELD_PCT or more,
    and asks the other run instead, or nobody. A run that can resume carries no log and is asked as before."""
    from ttp import checkcap, cli
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    log = tmp_path / "checks.log"
    with open(log, "w") as out:
        assert cli._checks_progress(out, ["sh -c 'pytest -q'", "python3 -m pytest -q"]) == str(log)
        assert cli._checks_progress(out, ["python3 -m pytest -q -n 4"]) == str(log)
        assert cli._checks_progress(out, ["python3 -m pytest -q"]) == "", "it resumes: no shield"
    log.write_text("$ pytest -q\n........ [ 50%]\n........ [ 91%]\n")
    late = checkcap.acquire("late run", project_name="other", priority=5, preemptible=True, progress=str(log))
    early = checkcap.acquire("early run", project_name="other", priority=2, preemptible=True)

    def ask():
        try:
            return checkcap.preempt("urgent", checkcap.all_paths(2), "mine")
        finally:
            for f in checkcap.slot_dir().glob(f"{checkcap.PREEMPT}.*.json"):
                f.unlink()
    try:
        assert ask() == "early run", "the lower-priority run is past 90% and cannot resume: the other is asked"
        early.close()
        assert ask() == "", "nobody else: it is still not asked"
        log.write_text("$ pytest -q\n........ [ 89%]\n")
        assert ask() == "late run", "below 90% it is asked as before"
    finally:
        early.close()
        late.close()


def test_a_rerun_within_one_checks_run_runs_every_test_file_again(env, tmp_path, monkeypatch, capsys):
    """#7218: the resume record a check keeps in a run never thins a rerun the same run makes after a fault
    (a rewritten script, a torn file, a lost worker): that rerun runs every file again."""
    from ttp import checkcap, cli, scriptwatch
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv(checkcap.RESUME_ENV, raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(scriptwatch, "rerun", lambda run_once, *a, **k: (run_once(), run_once())[1])
    ran = tmp_path / "ran.txt"
    suite = repo / "suite"
    suite.mkdir()
    for name in ("a", "b"):
        (suite / f"test_{name}.py").write_text(f"def test_{name}():\n    open({str(ran)!r}, 'a').write('{name}\\n')\n")
    subprocess.run([*git, "add", "suite"], check=True)
    subprocess.run([*git, "commit", "-qm", "suite"], check=True)
    cli.main(["checks", "--", "python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "suite"])
    out = capsys.readouterr().out
    assert "passed" in out, out
    assert sorted(ran.read_text().split()) == ["a", "a", "b", "b"], ran.read_text()


def test_checks_with_no_task_started_under_a_project_run_queue_as_that_run(env, tmp_path):
    # A `ttp checks` whose environment names no task (a tool started it from a temp clone with the run's
    # variables cleared) under a project's run counts as that run: its queue group, label and task are the run's.
    import json
    import os
    import sys
    p = make(env)
    code = ("import json, os; from ttp import checkcap; i = checkcap.adopt_identity(); n = checkcap.queue_name(None); "
            "print(json.dumps({'name': n, 'label': checkcap.label(n, 'w'), 'task': checkcap._task_run()[0], "
            "'other': checkcap.inherited(os.getppid(), base=os.environ['ELSEWHERE'])}))")
    base = {k: v for k, v in os.environ.items() if k != "TTP_TEST_NO_INHERIT"}
    run = lambda e: json.loads(subprocess.run(   # noqa: E731
        ["sh", "-c", 'env -u TTP_TASK -u TTP_RUN_ID -u TTP_PROJECT "$0" -c "$1"; :', sys.executable, code],
        env={**base, "PYTHONPATH": str(RUNTIME), "ELSEWHERE": str(tmp_path / "elsewhere"), **e},
        capture_output=True, text=True, timeout=60, check=True).stdout)
    got = run({"TTP_TASK": "42", "TTP_RUN_ID": "9", "TTP_PROJECT": str(p.base)})
    assert got["name"] == p.name and got["task"] == "42"
    assert got["label"] == f"{p.name} task #42 (run 9) w"
    assert got["other"] == {}, "with its own project named, another project's run is not adopted"
    bare = run({"TTP_RUN_ID": "9", "TTP_PROJECT": str(p.base)})   # no task above it: no project
    assert bare["name"] is None and bare["label"] == "no project w"
    gone = run({"TTP_TASK": "42", "TTP_PROJECT": str(tmp_path / "no-such-project")})
    assert gone["name"] is None, "a run of a project that does not exist is not adopted"


def test_checks_with_no_project_queue_behind_every_project_waiter(env, monkeypatch):
    import time
    from ttp import checkcap, locks
    d = checkcap.slot_dir()
    now = time.time()
    bare = locks.enqueue(d, checkcap.RESOURCE, "no project w", since=now - 7200)   # aged, and first to arrive
    a = locks.enqueue(d, checkcap.RESOURCE, "pa task #1 w", since=now - 60, group="pa", priority=3)
    b = locks.enqueue(d, checkcap.RESOURCE, "pb task #2 w", since=now - 30, group="pb")
    try:
        assert locks.fair_order(d, checkcap.RESOURCE, **checkcap.order()) == [a, b, bare]
        assert checkcap.state()["queued"] == ["pa task #1 w", "pb task #2 w", "no project w"]
        assert not locks.my_turn(d, checkcap.RESOURCE, bare, 2, fair=True, **checkcap.order())
        assert locks.my_turn(d, checkcap.RESOURCE, bare, 3, fair=True, **checkcap.order())
        assert locks.fair_order(d, checkcap.RESOURCE, aged_before=now - 3600)[0] == bare, "only with bare_last"
    finally:
        for t in (bare, a, b):
            locks.dequeue(t)


def test_checks_with_no_project_hold_at_most_bare_slots_at_once(env, monkeypatch):
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "3")
    first = checkcap.acquire(checkcap.label(None, "w"))
    try:
        assert first.f is not None
        alone = checkcap.acquire(checkcap.label(None, "w2"), max_wait_s=5)
        assert alone is not None and alone.f is not None, "with no project's run waiting the cap does not bind"
        alone.close()
        waiter = locks.enqueue(checkcap.slot_dir(), checkcap.RESOURCE, "pa task #1 w", group="pa")
        try:
            assert checkcap.acquire(checkcap.label(None, "w2"), max_wait_s=0.2) is None, \
                "one run with no project at once while a project's run waits"
            monkeypatch.setenv(checkcap.BARE_SLOTS_ENV, "2")
            second = checkcap.acquire(checkcap.label(None, "w2"), max_wait_s=5)
            assert second is not None and second.f is not None, "checks.bare_slots raises the cap"
            second.close()
        finally:
            locks.dequeue(waiter)
    finally:
        first.close()


def test_checks_status_flags_a_slot_holder_running_no_test(env, monkeypatch):
    # Status reads whether each host-wide slot holder's commands use CPU: one held for idle_hold_s() with none
    # running is flagged with `ttp checks-yield`, a busy one is not. Nothing is stopped or asked.
    import os
    import sys
    import time
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "3")
    code = ("import sys; from ttp import checkcap; s = checkcap.acquire(checkcap.label(sys.argv[1], 'w'), "
            "project_name=sys.argv[1]); print('held', flush=True); sys.stdin.read()")
    procs = {}
    try:
        for name in ("pa", "pb"):
            procs[name] = subprocess.Popen([sys.executable, "-c", code, name], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, text=True,
                                           env={**os.environ, "PYTHONPATH": str(RUNTIME)})
            assert procs[name].stdout.readline().strip() == "held"
        busy, idle = procs["pa"].pid, procs["pb"].pid
        reads = {busy: iter([10.0, 11.0, 12.0, 13.0]), idle: iter([5.0, 5.0, 5.0, 5.0])}
        monkeypatch.setattr(checkcap, "tests_cpu_s", lambda pid: next(reads[pid]))
        later = time.time() + checkcap.IDLE_HOLD_S + 60
        got = {a["holder"]: a for a in checkcap.holder_activity(now=later, sample_s=0)}
        assert got["pa w"]["tests"] is True and not got["pa w"]["idle"]
        assert got["pb w"]["tests"] is False and got["pb w"]["idle"]
        assert "ttp checks-yield" in checkcap.activity_text(got["pb w"])
        assert "IDLE" not in checkcap.activity_text(got["pa w"])
        now = {a["holder"]: a for a in checkcap.holder_activity(sample_s=0)}
        assert not now["pb w"]["idle"], "held under idle_hold_s(): not flagged yet"
        reads = {busy: iter([10.0, 11.0]), idle: iter([5.0, 5.0])}
        monkeypatch.setenv(checkcap.IDLE_HOLD_ENV, "0.001")
        line = checkcap.line(activity=True)
        assert "no test running: pb w" in line and "pa w (held" not in line and "ttp checks-yield" in line
        assert "no test running" not in checkcap.line(), "without activity the line reads no process"
        assert all(p.poll() is None for p in procs.values()), "status stops nothing"
    finally:
        for p in procs.values():
            p.kill()
            p.wait()


def test_checks_tests_cpu_counts_what_a_holder_started_not_itself(tmp_path):
    import sys
    import time
    from ttp import checkcap
    burn = f"{sys.executable} -c 'import time\nwhile time.process_time() < 0.6: pass'"
    sh = subprocess.Popen(["sh", "-c", f"{burn}; touch {tmp_path / 'done'}; sleep 30"])
    try:
        deadline = time.time() + 60
        while not (tmp_path / "done").exists() and time.time() < deadline:
            time.sleep(0.05)
        assert checkcap.tests_cpu_s(sh.pid) >= 0.5, "a reaped command's CPU counts (0.6 s, in clock ticks)"
        assert checkcap.tests_cpu_s(2 ** 22 + 7) is None, "a pid that is gone reads None"
    finally:
        sh.kill()
        sh.wait()


def test_detached_checks_keep_the_identity_their_parent_adopted(env, monkeypatch, tmp_path):
    # The background run of a detached `ttp checks` outlives its parent, so it cannot walk up to the run: the
    # parent hands what it adopted over in the environment, honoured only for an existing project.
    from ttp import checkcap
    p = make(env)
    monkeypatch.setattr(checkcap, "_IDENT", {})
    monkeypatch.setattr(checkcap, "inherited", lambda pid, base=None: {})
    assert checkcap.adopted_env() == {}
    checkcap._IDENT.update(task="42", run="9", base=str(p.base), project=p.name)
    monkeypatch.setenv(checkcap.ADOPTED_ENV, checkcap.adopted_env()[checkcap.ADOPTED_ENV])
    monkeypatch.delenv(checkcap.NO_INHERIT_ENV)
    assert checkcap.adopt_identity()["task"] == "42" and checkcap.queue_name(None) == p.name
    assert checkcap.adopt_identity(tmp_path / "other") == {}, "another project's run is not taken"
    monkeypatch.setenv(checkcap.ADOPTED_ENV, '{"task": "42", "base": "%s"}' % (tmp_path / "gone"))
    assert checkcap.adopt_identity() == {}
    monkeypatch.setenv(checkcap.ADOPTED_ENV, "not json")
    assert checkcap.adopt_identity() == {}


def test_a_lane_waiter_is_named_by_the_task_in_its_run_record(env, tmp_path, monkeypatch):
    # A detached candidate run started by a task: its lane ticket's record (locks.waiter_ids) names the task,
    # so the wait event and the lane status name it even when the ticket's text does not; a run with no task
    # in its record still reads "no task". An adopted identity (checkcap.adopt_identity) is recorded too.
    from ttp import checkcap, checklane, locks, testslots
    p = make(env)
    _lane_env(monkeypatch)
    holder = _lane_holder(tmp_path, p.name, _LANE_REPO)
    base = testslots.suite_base(p.name, _LANE_REPO)
    now = time.time()
    # the waiters' process reads as gone: this test's own environment and worktree must not name a task
    monkeypatch.setattr(checklane, "_proc", lambda pid: {"command": "", "cwd": "", "ppid": None, "sid": None, "env": None})
    monkeypatch.setenv("TTP_TASK", "71")
    monkeypatch.setenv("TTP_RUN_ID", "501")
    tasked = locks.enqueue(checkcap.slot_dir(), base, f"{p.name}: full checks (run 501, pid 1)", now - 900, priority=2)
    monkeypatch.delenv("TTP_TASK")
    monkeypatch.delenv("TTP_RUN_ID")
    bare = locks.enqueue(checkcap.slot_dir(), base, f"{p.name}: full checks (pid 2)", now - 800, priority=2)
    monkeypatch.setattr(locks, "IDENTITY", {"task": "73", "run": "502"})
    adopted = locks.enqueue(checkcap.slot_dir(), base, f"{p.name}: full checks (pid 3)", now - 700, priority=2)
    try:
        assert [locks.ticket_task(t) for t in (tasked, bare, adopted)] == ["71", "", "73"]
        texts = [e["text"] for e in checklane.wait_events(p.name, {"checks": {"wait_event_s": 600}}, now)]
        assert any(t.startswith("Checks lane wait: #71 has waited") for t in texts), texts
        assert any(t.startswith("Checks lane wait: #73 has waited") for t in texts), texts
        # a gone taskless waiter is named by its pid, its ticket label's command, and when it first queued
        assert any(t.startswith(f"Checks lane wait: a run with no task (pid {os.getpid()} 'full checks (pid 2)' "
                                f"folder unknown, first queued ") for t in texts), texts
        waiters = [w["task"] for r in checklane.lane_status(p.name, now) for w in r["waiters"]]
        assert sorted(waiters) == ["", "71", "73"], waiters
        assert checklane._who({"task": "", "pid": 2}) == "no task pid 2"
        assert checklane._holder_task({"task_id": "74"}) == "74" and checklane._holder_task({"task_id": "x"}) == ""
    finally:
        for t in (tasked, bare, adopted):
            locks.dequeue(t)
        _stop(holder)


def test_the_queue_wait_report_and_slot_status_count_the_same_host_wide_slot_holders(env, monkeypatch):
    # The queue-wait report once read 5 running against cap 4 while slot status read 2: it added the push
    # priority and urgent slots to the cap's, and a run seen on several slots counted each time. Both now
    # read checkcap.host_holders: every holder once, the same count in both.
    import time
    from ttp import checkcap, hostload, locks, queuewait
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "1")
    p = make(env)
    held = [locks.try_take(checkcap.paths(), "acme task #71 (run 5)"),
            locks.try_take(checkcap.paths(), "acme task #71 (run 5)"),   # the same run seen on a second slot
            locks.try_take(checkcap.urgent_paths(), "acme task #72 (run 6)"),
            locks.try_take([checkcap.priority_path("acme/branch")], "push batch b1")]
    try:
        assert all(held)
        paths = checkcap.paths()
        paths[1].write_text(paths[0].read_text())   # the same label: holder, pid and since
        names = sorted(checkcap.state()["held"])
        assert names == ["acme task #71 (run 5)", "acme task #72 (run 6)", "push batch b1"]
        assert hostload.snapshot()["running"] == 3, "slot status counts what the report counts"
        tid = p.db.add_task("waits", "s", kind="code")
        now = time.time()
        host = {**checkcap.state(), "queued": [f"{p.name} task #{tid} (run 3) ttp checks"], "queued_at": [now - 600]}
        waits = queuewait.collect(p, {"coordinator": {"queue_wait_escalate_min": 5}}, now, host)
        assert [sorted(w["running"]) for w in waits if w["queue"] == "checks"] == [names]
        legacy = {k: v for k, v in host.items() if k != "held"}   # an older daemon's state: the old sum
        assert len([w for w in queuewait.collect(p, {}, now, legacy) if w["queue"] == "checks"][0]["running"]) == 4
    finally:
        for f in held:
            if f is not None:
                f.close()


def _remote_load(monkeypatch, readings: dict):
    """remoteload.reading answers from `readings` ({host: [(demand, cores) or None, ...]}, the last one kept)."""
    from ttp import remoteload
    monkeypatch.setenv("TTP_CHECKS_LOAD_GUARD", "1")
    monkeypatch.setattr(remoteload, "reading",
                        lambda h, **k: readings[h].pop(0) if len(readings[h]) > 1 else readings[h][0])
    monkeypatch.setattr(remoteload, "POLL_S", 0.01)


def test_push_checks_on_a_remote_host_skip_this_hosts_load_guard_checks_slot_and_test_slots(env, monkeypatch):
    """#153: a push whose checks run on another host (a check's "host") starts at once while this host reads
    overloaded: no wait for its load guard, no host-wide checks slot, no test slots; a light local check
    beside it (claude plugin validate) takes none either. The remote host's load is read instead."""
    from ttp import checkcap, push, testslots
    p = make(env)
    monkeypatch.setattr(testslots, "load_guard", lambda: "the host's CPU is overloaded")
    monkeypatch.setattr(push, "check_slot", lambda *a, **k: pytest.fail("no local checks slot"))
    _remote_load(monkeypatch, {"checkhost": [(3.0, 64)]})
    before = len(checkcap.grant_records())
    run = push.check_list([{"run": "pytest -n 16 tests", "host": "checkhost"},
                           "claude plugin validate plugins/tt-project --strict"])
    told, t0 = [], time.time()
    slot = push.run_slot(run, "abc", told.append, priority="branch-key", proj=p)
    assert time.time() - t0 < 5 and told == []
    assert slot.f is None and slot.tests is None and slot.env() == {}
    assert len(checkcap.grant_records()) == before and checkcap.state()["running"] == []
    # delivery.checks_host as the default host counts the same.
    slot = push.run_slot(push.with_host(push.check_list(["pytest -n 16 tests"]), "checkhost"), "abc", told.append, proj=p)
    assert slot.f is None and slot.tests is None


def test_push_checks_on_an_overloaded_remote_host_wait_for_it_and_say_so(env, monkeypatch):
    """#153, #8016: checks run on a host whose runnable demand is near its cores (checks.load_guard, at most
    checkspill.LOAD_NEAR x) wait for it to drop and say which host and why; they never start there anyway,
    however long it stays overloaded, and say so again past checks.push_load_wait_s."""
    from ttp import push, remoteload
    p = make(env)
    monkeypatch.setattr(push, "check_slot", lambda *a, **k: pytest.fail("no local checks slot"))
    _remote_load(monkeypatch, {"checkhost": [(90.0, 64), (90.0, 64), (20.0, 64)]})
    from ttp import checkspill   # its check slots there: a fake probe, no ssh
    monkeypatch.setattr(checkspill, "probe", lambda h, b=False: (0, "ssh cannot reach it: exit 255", ""))
    run = push.check_list([{"run": "pytest -n 16 tests", "host": "checkhost"}])
    told = []
    slot = push.run_slot(run, "abc", told.append, proj=p)
    assert slot.f is None and len(told) == 1, told
    assert "check host checkhost's CPU is overloaded" in told[0] and "90.0 cores" in told[0] and "64 cores" in told[0]
    assert "waiting for it to drop" in told[0] and "300s" in told[0]
    # 55 of 64 cores is past 0.8 x even with checks.load_guard 1: it waits.
    assert remoteload.factor() == 0.8
    _remote_load(monkeypatch, {"checkhost": [(55.0, 64)] * 6 + [(20.0, 64)]})
    told, now = [], [0.0]
    def sleep(_s):
        now[0] += 100
    assert remoteload.wait(["checkhost"], told.append, 0, sleep=sleep, clock=lambda: now[0]) == ""
    assert now[0] >= 500 and not any("anyway" in t for t in told), told
    assert any("still waiting after" in t for t in told), told
    _remote_load(monkeypatch, {"checkhost": [None]})
    assert remoteload.wait(["checkhost"], told.append, None) == "", "an unreadable host never holds it"


def _move_spill(monkeypatch, picked):
    """checkspill with one spill host whose pick hands out a claim on it (None: no free host), each pick's
    `off` and why recorded in `picked`."""
    from ttp import checkspill
    monkeypatch.setattr(checkspill, "hosts", lambda cfg: ["spillhost"])

    def pick(p, cfg, tell, what="", off="", why_here=""):
        picked.append((off, why_here))
        return checkspill.Claim("spillhost", None) if picked[0] != "none" else None
    monkeypatch.setattr(checkspill, "pick", pick)


def test_push_checks_on_an_overloaded_default_check_host_move_to_a_free_spill_host(env, monkeypatch):
    """#8232: push checks that took delivery.checks_host as their default, on a host that stays overloaded
    past checks.push_load_wait_s, go to a spill host with free cores (checkspill.pick, the overloaded host
    kept off) instead of starting anyway; the run holds that host's claim and never starts on the
    overloaded one. A check whose own "host" names it keeps waiting there."""
    from ttp import push
    p = make(env)
    monkeypatch.setattr(push, "check_slot", lambda *a, **k: pytest.fail("no local checks slot"))
    monkeypatch.setenv("TTP_CHECKS_PUSH_LOAD_WAIT_S", "0")
    _remote_load(monkeypatch, {"checkhost": [(90.0, 64)]})
    picked = []
    _move_spill(monkeypatch, picked)
    run = push.with_host(push.check_list(["pytest -n 16 tests"]), "checkhost")
    told = []
    slot = push.run_slot(run, "abc", told.append, proj=p)
    assert slot.spill is not None and slot.spill.host == "spillhost" and slot.f is None
    assert push.remote_host(run[0]) == "spillhost" and str(run[0]) == "pytest -n 16 tests"
    assert picked and picked[0][0] == "checkhost:fail" and "checkhost stayed overloaded" in picked[0][1]
    assert any("moved to spill host spillhost" in t for t in told), told
    assert not any("anyway" in t for t in told), told
    # Its own "host" stays its own: it waits until the load drops, never moving and never starting before.
    _remote_load(monkeypatch, {"checkhost": [(90.0, 64)] * 8 + [(20.0, 64)]})
    picked.clear()
    run = push.check_list([{"run": "pytest -n 16 tests", "host": "checkhost"}])
    slot = push.run_slot(run, "abc", told.append, proj=p)
    assert slot.spill is None and push.remote_host(run[0]) == "checkhost" and picked == []


def test_push_checks_wait_when_no_spill_host_is_free_and_never_start_on_the_overloaded_host(env, monkeypatch):
    """#8232: with no spill host free (pick finds none), checks on an overloaded default check host keep
    waiting there, trying again each wait, and start only once its load drops: never anyway."""
    from ttp import push, remoteload
    p = make(env)
    monkeypatch.setattr(push, "check_slot", lambda *a, **k: pytest.fail("no local checks slot"))
    monkeypatch.setenv("TTP_CHECKS_PUSH_LOAD_WAIT_S", "0")
    reads = [(90.0, 64)] * 10 + [(20.0, 64)]
    _remote_load(monkeypatch, {"checkhost": reads})
    picked = ["none"]
    _move_spill(monkeypatch, picked)
    run = push.with_host(push.check_list(["pytest -n 16 tests"]), "checkhost")
    told = []
    slot = push.run_slot(run, "abc", told.append, proj=p)
    assert slot.spill is None and push.remote_host(run[0]) == "checkhost" and len(picked) > 1
    assert reads == [(20.0, 64)], "it started only once the load dropped"
    assert not any("anyway" in t for t in told), told


def test_ttp_checks_on_an_overloaded_default_check_host_move_to_a_free_spill_host(env, monkeypatch, tmp_path):
    """#8232: `ttp checks` whose checks took an overloaded delivery.checks_host as default move them to a free
    spill host past the wait: the caller's list is rewritten in place and the slot holds the spill claim; a
    light check there that may not spill (git diff --check) runs here instead of waiting for that host."""
    import io
    from ttp import checkcap, cli, push
    p = make(env)
    monkeypatch.setattr(checkcap, "acquire", lambda *a, **k: pytest.fail("no local checks slot"))
    monkeypatch.setenv("TTP_CHECKS_PUSH_LOAD_WAIT_S", "0")
    _remote_load(monkeypatch, {"checkhost": [(90.0, 64)]})
    _move_spill(monkeypatch, [])
    out = io.StringIO()
    cmds = push.with_host(push.check_list(["pytest -n 16 tests", "git diff --check"]), "checkhost")
    slot = cli._checks_slot(p, tmp_path, out, "a" * 40, cmds=cmds, top=str(p.root))
    assert slot.spill is not None and slot.spill.host == "spillhost"
    assert [push.remote_host(c) for c in cmds] == ["spillhost", ""], "a light check that cannot spill runs here"
    assert "moved to spill host spillhost" in out.getvalue() and "running now" not in out.getvalue()


def test_a_timing_failure_on_an_overloaded_check_host_is_a_host_fault_not_the_changes(monkeypatch, capsys):
    """#8232: a remote check that fails with a timing-sensitive test's failure (a timeout, an elapsed-time
    assert) while its host is overloaded is an environment fault of the host (FAULT_RC), so it runs again
    elsewhere or later; any other failure, or one on a host with free cores, stays the change's."""
    from ttp import remotecheck
    _remote_load(monkeypatch, {"busy": [(90.0, 64)], "free": [(10.0, 64)], "gone": [None]})
    assert remotecheck.TIMING.search("E   subprocess.TimeoutExpired: Command 'x' timed out after 5 seconds")
    assert remotecheck.TIMING.search("E   assert elapsed < 2.0")
    assert not remotecheck.TIMING.search("E   assert 1 == 2")
    assert remotecheck.judged("busy", 1, True) == remotecheck.FAULT_RC
    assert "environment fault" in capsys.readouterr().out
    assert remotecheck.judged("busy", 1, False) == 1
    assert remotecheck.judged("free", 1, True) == 1
    assert remotecheck.judged("gone", 1, True) == 1
    assert remotecheck.judged("busy", 0, True) == 0


def test_remote_checks_started_just_now_count_as_load_so_parallel_starts_never_pile_on_one_host(env, monkeypatch):
    """#8016: the 1-minute load shows none of the runs started there in the last minute, so each start from
    this machine is recorded and counts one run's cores (checklane.run_cores) until it shows: a host at 3 of
    64 cores takes a few starts, then holds the next until its earlier ones age out."""
    from ttp import checklane, remoteload
    make(env)
    _remote_load(monkeypatch, {"checkhost": [(3.0, 64)]})
    per = checklane.run_cores(None, 64)
    fits = sum(1 for _ in range(40) if not remoteload.held("checkhost", start=True))
    assert 1 <= fits < 40 and fits == sum(1 for k in range(40) if 3 + k * per < 0.8 * 64), (fits, per)
    why = remoteload.held("checkhost")
    assert why and "just started there from this machine" in why, why
    later = time.time() + remoteload.LAG_S + 1
    assert remoteload.held("checkhost", now=lambda: later) == "", "older starts show in the load by then"


def test_local_push_checks_still_take_a_checks_slot_and_never_read_a_remote_host(env, monkeypatch):
    """#153: a push with a local check that is not light takes check_slot as before (its load guard, the
    host-wide cap and test slots); no remote host is read."""
    from ttp import push, remoteload
    p = make(env)
    monkeypatch.setattr(remoteload, "reading", lambda h: pytest.fail("no remote read"))
    calls = []
    monkeypatch.setattr(push, "check_slot", lambda head, say, priority="", proj=None, repo=None: calls.append(priority) or "slot")
    assert push.run_slot(push.check_list(["pytest tests", {"run": "true", "host": "local"}]), "abc", print,
                         priority="branch-key", proj=p) == "slot"
    assert calls == ["branch-key"]
    assert push.run_slot([], "abc", print, proj=p).f is None and calls == ["branch-key"]


def test_ttp_checks_run_only_on_a_remote_host_take_no_local_checks_slot(env, monkeypatch, tmp_path):
    """#153 (review #147 waited 11 min in this host's checks queue): `ttp checks` whose checks all run on
    another host, light local ones aside, return at once with no host-wide slot or test slots here."""
    import io
    from ttp import checkcap, cli, push, testslots
    p = make(env)
    monkeypatch.setattr(testslots, "load_guard", lambda: "the host's CPU is overloaded")
    monkeypatch.setattr(checkcap, "acquire", lambda *a, **k: pytest.fail("no local checks slot"))
    _remote_load(monkeypatch, {"checkhost": [(3.0, 64)]})
    from ttp import checkspill   # its check slots there: a fake probe, no ssh
    monkeypatch.setattr(checkspill, "probe", lambda h, b=False: (0, "ssh cannot reach it: exit 255", ""))
    out = io.StringIO()
    cmds = push.check_list([{"run": "pytest -n 16 tests", "host": "checkhost"}, "git diff --check"])
    slot = cli._checks_slot(p, tmp_path, out, "a" * 40, cmds=cmds, top=str(p.root))
    assert slot.f is None and slot.tests is None
    assert "checks run on checkhost: running now, outside this machine's checks slot" in out.getvalue()


def test_a_project_whose_checks_run_on_a_remote_host_is_never_held_by_local_load(env, monkeypatch, tmp_path):
    """A project whose checks all run on its checks host (delivery.checks_host) is never held by this machine's
    load: neither its load guard (testslots.load_guard) nor its free cores (checkcap.free_cores) are read, with
    or without a fixed checks.test_project_max, for `ttp checks` and push checks alike."""
    import io
    from ttp import checkcap, cli, push, testslots
    p = make(env)
    monkeypatch.setattr(testslots, "load_guard", lambda: pytest.fail("this host's load guard read"))
    monkeypatch.setattr(checkcap, "free_cores", lambda: pytest.fail("this host's free cores read"))
    monkeypatch.setattr(checkcap, "acquire", lambda *a, **k: pytest.fail("no local checks slot"))
    _remote_load(monkeypatch, {"checkhost": [(3.0, 64)]})
    for cap in ("", "2"):
        monkeypatch.setenv("TTP_TEST_PROJECT_MAX", cap) if cap else monkeypatch.delenv("TTP_TEST_PROJECT_MAX", raising=False)
        cmds = push.with_host(push.check_list(["pytest -n 16 tests"]), "checkhost")
        slot = cli._checks_slot(p, tmp_path, io.StringIO(), "a" * 40, cmds=cmds, top=str(p.root))
        assert slot is not None and slot.f is None
        slot.close()
        slot = push.run_slot(cmds, "abc", print, priority="branch-key", proj=p, repo=p.root)
        assert slot.f is None
        slot.close()


def test_remote_checks_keep_the_project_lane_under_a_fixed_test_project_max(env, monkeypatch, tmp_path):
    """Under a fixed checks.test_project_max, checks that run on a checks host still take one of the project's
    checks lanes (no checks slot or test slots here): two full suites never share the checks host past the cap.
    `ttp checks` waits for the lane; push checks wait at most checks.push_load_wait_s, then go ahead and say so."""
    import io
    from ttp import checkcap, cli, push, testslots
    p = make(env)
    monkeypatch.setattr(checkcap, "acquire", lambda *a, **k: pytest.fail("no local checks slot"))
    _remote_load(monkeypatch, {"checkhost": [(3.0, 64)]})
    monkeypatch.setenv("TTP_TEST_PROJECT_MAX", "1")
    cmds = push.with_host(push.check_list(["pytest -n 16 tests"]), "checkhost")
    out = io.StringIO()
    first = cli._checks_slot(p, tmp_path, out, "a" * 40, cmds=cmds, top=str(p.root))
    try:
        assert first is not None and first.f is None and first.tests is not None
        assert "in one of the project's checks lanes (checks.test_project_max)" in out.getvalue()
        assert len(testslots.project_held(p.name)) == 1
        # A second remote run waits for that lane: its foreground wait ends with no slot.
        monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 0.3)
        assert cli._checks_slot(p, tmp_path / "b", io.StringIO(), "b" * 40, cmds=cmds, top=str(p.root)) is None
        # A push's remote checks wait at most checks.push_load_wait_s for it, then go ahead without one.
        monkeypatch.setenv("TTP_CHECKS_PUSH_LOAD_WAIT_S", "0")
        told = []
        slot = push.run_slot(cmds, "abc", told.append, priority="branch-key", proj=p, repo=p.root)
        assert slot.tests is None and any("stayed full" in t and "going ahead" in t for t in told), told
    finally:
        first.close()
    assert testslots.project_held(p.name) == []
    told = []
    slot = push.run_slot(cmds, "abc", told.append, priority="branch-key", proj=p, repo=p.root)
    assert slot.tests is not None and len(testslots.project_held(p.name)) == 1 and told == []
    slot.close()
    assert testslots.project_held(p.name) == []
    # With no fixed cap they take no lane, as before.
    monkeypatch.delenv("TTP_TEST_PROJECT_MAX")
    slot = push.run_slot(cmds, "abc", told.append, proj=p, repo=p.root)
    assert slot.tests is None


def test_the_push_priority_slot_counts_against_a_fixed_test_project_max(env, monkeypatch):
    """The push priority slot goes past the host-wide cap, never past the project's fixed
    checks.test_project_max: its run holds one of the project's lanes, and waits for one while they are full."""
    from ttp import push, testslots
    p = make(env)
    monkeypatch.setenv("TTP_TEST_PROJECT_MAX", "1")
    slot = push.check_slot("abc", lambda s: None, priority="branch-key", proj=p, repo=p.root)
    try:
        assert len(testslots.project_held(p.name)) == 1
        monkeypatch.setenv("TTP_CHECKS_PUSH_LOAD_WAIT_S", "0")
        told = []
        other = push.check_slot("abd", told.append, priority="other-key", proj=p, repo=p.root)
        other.close()
        assert any("stayed full" in t for t in told), told
    finally:
        slot.close()
    assert testslots.project_held(p.name) == []


def test_remoteload_parses_two_proc_stat_samples():
    from ttp import remoteload
    def stat(busy, idle, running, cores=4):
        return (f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\n" + "".join(f"cpu{i} 1 0 0 1 0 0 0 0 0 0\n" for i in range(cores))
                + f"procs_running {running}\n")
    d, c = remoteload.parse(stat(100, 100, 1) + remoteload.SEP + "\n" + stat(500, 100, 1))
    assert c == 4 and abs(d - 4.0) < 0.01
    d, c = remoteload.parse(stat(100, 100, 9) + remoteload.SEP + "\n" + stat(100, 500, 11))
    assert c == 4 and abs(d - 9.0) < 0.01
    assert remoteload.parse("no sample") is None


def test_a_light_local_check_alone_still_takes_this_hosts_checks_slot(env, monkeypatch, tmp_path):
    """Only a light check beside checks run elsewhere goes without a slot: a run of light local checks
    alone is still bounded by this host's checks cap (checkcap.acquire)."""
    import io
    from ttp import checkcap, cli, push
    p = make(env)
    took = []
    monkeypatch.setattr(checkcap, "acquire", lambda *a, **k: took.append(a) or checkcap.Slot())
    out = io.StringIO()
    cli._checks_slot(p, tmp_path, out, "a" * 40, cmds=push.check_list(["claude plugin validate plugins/x --strict"]),
                     top=str(p.root))
    assert took, out.getvalue()


def test_the_load_guard_holds_on_sustained_demand_or_memory_pressure_but_not_on_a_short_spike(env, monkeypatch):
    """checks.load_sustain_s: the guard holds a local suite only while the CPU demand was over the guard at
    every reading of the window (checkcap.sustained_load, readings shared through check-slots), or while
    memory pressure is at warn or critical (hostcpu.mem_pressure); a spike of short-lived processes or one
    high reading holds nothing, and the live-cores gate leaves cores a spike took free."""
    from ttp import checkcap, hostcpu, testslots
    monkeypatch.setenv("TTP_CHECKS_LOAD_SUSTAIN_S", "60")
    monkeypatch.setenv("TTP_CHECKS_LOAD_GUARD", "1")
    monkeypatch.setattr(checkcap, "cores", lambda: 14)
    demand = [30.0]
    monkeypatch.setattr(checkcap, "cpu_demand", lambda: demand[0])
    log = checkcap.slot_dir() / checkcap.DEMAND_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()

    def seen(*readings):
        log.write_text("".join(json.dumps({"ts": now - age, "d": d}) + "\n" for age, d in readings))
    seen()
    assert testslots.load_guard() == "", "one high reading is no sustained load"
    seen((50, 2.0), (20, 30.0))
    assert testslots.load_guard() == "", "a spike: a low reading 50 s ago"
    seen((55, 30.0), (40, 25.0), (10, 28.0))
    assert "overloaded" in testslots.load_guard(), "high at every reading over the window"
    assert checkcap.sustained_load(now) == 25.0
    seen((90, 30.0), (10, 28.0))
    assert testslots.load_guard() == "", "readings older than the window do not count"
    demand[0] = 2.0
    seen((55, 30.0), (40, 25.0))
    assert testslots.load_guard() == "", "the demand dropped"
    # Memory: pressure at warn or critical holds; normal does not.
    for level, held in ((4, True), (2, True), (1, False), (None, False)):
        monkeypatch.setattr(hostcpu, "mem_pressure", lambda level=level: level)
        assert bool(testslots.load_guard()) is held, level
    monkeypatch.setattr(hostcpu, "mem_pressure", lambda: 4)
    assert "pressure critical" in testslots.load_guard()
    monkeypatch.setattr(hostcpu, "mem_pressure", lambda: None)
    # The live-cores gate: busy cores of a spike leave room; sustained ones do not.
    monkeypatch.setenv("TTP_CHECKS_LIVE_CORES", "1")
    monkeypatch.setattr(checkcap, "free_cores", lambda: 0.0)
    monkeypatch.setattr(checkcap, "held_since", lambda every: [0.0])
    demand[0] = 14.0
    seen((50, 1.0))
    assert checkcap.cores_short([]) == "", "a spike"
    seen((55, 14.0), (30, 14.0))
    assert checkcap.cores_short([]), "sustained"
    monkeypatch.setenv("TTP_CHECKS_LOAD_SUSTAIN_S", "0")
    demand[0] = 30.0
    assert checkcap.cores_short([]) and "overloaded" in testslots.load_guard(), "0 reads the demand now"


def _within_cap_env(monkeypatch, host_max):
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", str(host_max))
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.delenv(checkcap.BEYOND_CAP_ENV, raising=False)
    monkeypatch.setenv(checkcap.PREEMPT_AFTER_ENV, "0")
    monkeypatch.setenv(checkcap.PREEMPT_ASKED_ENV, "")
    monkeypatch.setattr(checkcap, "POLL_S", 0.05)


def test_checks_priority_beyond_cap_is_off_unless_set(env, monkeypatch):
    """#7068: checks.priority_beyond_cap (TTP_CHECKS_PRIORITY_BEYOND_CAP) defaults off; true or 1 turns it on,
    and the environment wins over the account setting."""
    from ttp import checkcap
    monkeypatch.delenv(checkcap.BEYOND_CAP_ENV, raising=False)
    assert checkcap.priority_beyond_cap() is False
    for v, want in (("1", True), ("true", True), ("off", False), ("0", False), ("junk", False)):
        monkeypatch.setenv(checkcap.BEYOND_CAP_ENV, v)
        assert checkcap.priority_beyond_cap() is want, v


def test_a_held_push_priority_slot_counts_toward_the_host_checks_cap(env, monkeypatch):
    """#7068 (a): with host_max 2, a push run on its priority slot plus one run on a cap slot fill the cap:
    a third run does not start, and starts once the push ends."""
    from ttp import checkcap
    _within_cap_env(monkeypatch, 2)
    push = checkcap.acquire_priority("origin/branch", "push checks")
    a = checkcap.acquire("task a", priority=3)
    try:
        assert push.f is not None and a.f is not None
        assert checkcap.acquire("task b", priority=3, max_wait_s=0.5) is None, "push + cap slot fill a cap of 2"
        push.close()
        b = checkcap.acquire("task b", priority=3, max_wait_s=10)
        assert b is not None and b.f is not None
        b.close()
    finally:
        push.close()
        a.close()


def test_an_urgent_waiter_with_the_cap_full_preempts_a_routine_run_and_never_starts_beyond_the_cap(env, monkeypatch):
    """#7068 (b): the cap full of a routine run, an urgent run (an urgent slot free) asks the routine run to
    give its slot up and does not take the urgent slot beyond the cap; it starts once the slot frees."""
    import threading
    from ttp import checkcap, locks
    _within_cap_env(monkeypatch, 1)
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "1")
    routine = checkcap.acquire("routine", priority=5, preemptible=True)
    told, got = [], []
    t = threading.Thread(target=lambda: got.append(checkcap.acquire("urgent", priority=0, urgent=True,
                                                                    tell=told.append, max_wait_s=30)))
    t.start()
    try:
        assert _wait_for(lambda: checkcap.preempt_line("routine") in told), told
        assert not locks.held_labels(checkcap.urgent_paths()) and not got, "no start beyond the cap"
        assert checkcap.state()["total"] == 1
        routine.close()
        t.join(30)
        assert got and got[0] is not None and got[0].f is not None
        assert checkcap.state()["total"] == 1, "the urgent run took the one slot of the cap"
    finally:
        checkcap.clear_preempt()
        for h in [routine] + got:
            if h is not None:
                h.close()


def test_a_waiting_push_and_an_urgent_runs_preempt_claim_never_leave_the_freed_slot_idle(env, monkeypatch):
    """Review #7516: the cap full of routine runs, a push waiting on its priority slot and an urgent run that
    asked a routine run to give its slot up: the slot it frees goes to exactly one of them (the urgent claim
    wins), never past the cap, and the other takes the next slot that frees."""
    import threading
    from ttp import checkcap
    _within_cap_env(monkeypatch, 2)
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "1")
    r1 = checkcap.acquire("routine", priority=5, preemptible=True)
    r2 = checkcap.acquire("routine two", priority=5, preemptible=True)
    pushed, told, got = [], [], []
    p = threading.Thread(target=lambda: pushed.append(checkcap.acquire_priority("origin/b", "push checks")))
    p.start()
    held = [r1, r2]
    try:
        assert _wait_for(lambda: checkcap.state()["priority_waiting"]), checkcap.state()
        u = threading.Thread(target=lambda: got.append(checkcap.acquire("urgent", priority=0, urgent=True,
                                                                        tell=told.append, max_wait_s=8)))
        u.start()
        assert _wait_for(lambda: any(m.startswith("urgent checks with every host-wide checks slot busy: asked ")
                                     for m in told)), told
        asked = next(m for m in told if m.startswith("urgent checks with every host-wide checks slot busy: "))
        assert "beyond the cap" not in asked, "the cap is enforced: no urgent slot beyond it"
        (r1 if "asked routine " in asked else r2).close()
        u.join(5)
        assert got and got[0] is not None and got[0].f is not None, (told, got)
        assert p.is_alive() and not pushed, "the push takes the next slot, not this one"
        assert checkcap.state()["total"] <= 2
        held.append(got[0])
        got[0].close()
        p.join(5)
        assert pushed and pushed[0].f is not None, "the push took the slot the urgent run freed"
        held.append(pushed[0])
        assert checkcap.state()["total"] <= 2
    finally:
        checkcap.clear_preempt()
        for h in held + pushed + got:
            if h is not None:
                h.close()
        p.join(10)


def test_checks_status_line_counts_push_and_urgent_holders_in_the_total(env, monkeypatch):
    """#7068 (c): line() says how many checks runs hold a slot of any kind and how many of them are push and
    urgent runs; state() gives the same total."""
    from ttp import checkcap, locks
    _within_cap_env(monkeypatch, 4)
    monkeypatch.setenv(checkcap.URGENT_SLOTS_ENV, "1")
    a = checkcap.acquire("task a", priority=3)
    push = checkcap.acquire_priority("origin/branch", "push checks")
    urgent = locks.try_take(checkcap.urgent_paths(), "urgent run", "")
    try:
        assert urgent is not None
        s = checkcap.state()
        assert s["total"] == 3 and s["priority"] == ["push checks"] and s["urgent"] == ["urgent run"]
        out = checkcap.line()
        assert out.startswith("host checks: 3 running of cap 4 (incl. push 1, urgent 1)"), out
        assert "beyond the cap" not in out
        monkeypatch.setenv(checkcap.BEYOND_CAP_ENV, "1")
        assert "(incl. push 1, urgent 1 beyond the cap)" in checkcap.line()
    finally:
        urgent.close()
        push.close()
        a.close()


def test_a_push_run_takes_the_next_free_slot_ahead_of_queued_task_checks(env, monkeypatch):
    """#7068 (d): with the defaults and the cap full, a push run that arrives after a queued task run holds
    its priority slot as next in line: the slot that frees is the push run's, and the task run starts only
    once the push run ends."""
    import threading
    from ttp import checkcap
    _within_cap_env(monkeypatch, 1)
    held = checkcap.acquire("running", priority=3)
    task, push = [], []
    tt = threading.Thread(target=lambda: task.append(checkcap.acquire("task", priority=1, max_wait_s=30)))
    tt.start()
    try:
        assert _wait_for(lambda: checkcap.state()["queued"] == ["task"])
        told = []
        tp = threading.Thread(target=lambda: push.append(checkcap.acquire_priority("origin/b", "push checks",
                                                                                   tell=told.append)))
        tp.start()
        assert _wait_for(lambda: checkcap.state()["priority_waiting"] == ["push checks"])
        assert "push checks next for a slot: push checks" in checkcap.line()
        held.close()
        tp.join(30)
        assert push and push[0].f is not None and any("within the host-wide cap of 1" in m for m in told), told
        time.sleep(0.5)
        assert not task, "the task run waits behind the push run"
        assert checkcap.state()["priority"] == ["push checks"] and checkcap.state()["total"] == 1
        push[0].close()
        tt.join(30)
        assert task and task[0] is not None and task[0].f is not None
    finally:
        held.close()
        for h in task + push:
            if h is not None:
                h.close()


def _share_holders(monkeypatch, cap, held):
    """Hold host-wide checks slots under a cap of `cap`: {project: count}, labelled as acquire's callers do."""
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", str(cap))
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setenv("TTP_CHECKS_LIVE_CORES", "0")
    monkeypatch.delenv("TTP_TASK", raising=False)
    return [checkcap.acquire(checkcap.label(p), project_name=p) for p, n in held.items() for _ in range(n)]


def _share_queue(spec, at):
    """Queue tickets (label, project, priority) on the checks queue, one second apart from `at`."""
    from ttp import checkcap, locks
    return [locks.enqueue(checkcap.slot_dir(), checkcap.RESOURCE, label, since=at + i, group=g, priority=pr)
            for i, (label, g, pr) in enumerate(spec)]


def test_checks_fair_share_gives_the_next_slot_to_a_project_below_its_share_over_a_long_backlog(env, monkeypatch):
    # #7088: cap 4, project A holds 3 and has a long backlog; B waits with none: B's waiter goes before
    # A's backlog, though A waited longer since its last turn. A real waiter takes the freed slot.
    import threading
    from ttp import checkcap, locks
    held = _share_holders(monkeypatch, 4, {"A": 4})
    t0 = time.time()
    tk = _share_queue([(f"a{i}", "A", 3) for i in range(6)], t0 - 600) + _share_queue([("b0", "B", 3)], t0 + 60)
    names = lambda **k: [t.read_text() for t in locks.fair_order(checkcap.slot_dir(), checkcap.RESOURCE, **k)]
    try:
        assert names()[0] == "a0", "without shares: A's turn (it was served before B arrived)"
        assert names(**checkcap.order())[:3] == ["b0", "a0", "a1"], "B below its share (2 of 4) goes first"
        assert checkcap.over_share("B") == ["A"], "A holds 4 of a share of 2"
        held.pop().close()   # A holds 3: B (0) still below its share
        assert locks.my_turn(checkcap.slot_dir(), checkcap.RESOURCE, tk[-1], 1, fair=True, **checkcap.order())
        assert not locks.my_turn(checkcap.slot_dir(), checkcap.RESOURCE, tk[0], 1, fair=True, **checkcap.order())
        for t in tk[-1:]:
            locks.dequeue(t)
        got = []
        w = threading.Thread(target=lambda: got.append(checkcap.acquire(checkcap.label("B"), project_name="B",
                                                                       since=time.time() - 30, max_wait_s=30)))
        w.start()
        w.join(30)
        assert got and got[0] is not None, "the free slot went to B over A's six older waiters"
        held.append(got[0])
        rows = checkcap.grant_records()
        assert rows[-1]["project"] == "B" and rows[-1].get("over_share") == ["A"], rows
        rep = __import__("ttp.throughput", fromlist=["x"]).host_shares()
        assert rep["cap"] == 4 and rep["share"] == 2, rep
        assert rep["projects"]["B"]["over_share_waits"] == 1 and rep["projects"]["B"]["over_share_by"] == ["A"]
        assert rep["projects"]["A"]["running"] == 3 and rep["projects"]["B"]["running"] == 1
        assert rep["projects"]["B"]["grants"] == 1 and rep["projects"]["B"]["wait_s"] >= 0
    finally:
        for t in tk:
            locks.dequeue(t)
        for s in held:
            s.close()


def test_checks_fair_share_three_projects_on_a_cap_of_four_each_get_one_before_a_second(env, monkeypatch):
    from ttp import checkcap, locks
    held = _share_holders(monkeypatch, 4, {"A": 3})
    t0 = time.time()
    tk = _share_queue([("a0", "A", 3), ("a1", "A", 3)], t0 - 600) + _share_queue([("b0", "B", 3), ("b1", "B", 3)], t0 + 10) \
        + _share_queue([("c0", "C", 3), ("c1", "C", 3)], t0 + 20)
    try:
        assert locks.share_of(4, 3) == 1
        order = [t.read_text() for t in locks.fair_order(checkcap.slot_dir(), checkcap.RESOURCE, **checkcap.order())]
        assert order[:2] == ["b0", "c0"], "B and C (0 of a share of 1) before A (3) and before their own second"
        assert sorted(order[2:]) == ["a0", "a1", "b1", "c1"], order
        assert checkcap.over_share("C") == ["A"]
    finally:
        for t in tk:
            locks.dequeue(t)
        for s in held:
            s.close()


def test_checks_fair_share_a_lone_waiting_project_may_take_every_free_slot(env, monkeypatch):
    # Work-conserving: B holds 1 and waits for none; A (share 2 of 4) may still take all 3 free slots.
    from ttp import checkcap, locks
    held = _share_holders(monkeypatch, 4, {"B": 1})
    tk = _share_queue([(f"a{i}", "A", 3) for i in range(4)], time.time())
    try:
        turn = [locks.my_turn(checkcap.slot_dir(), checkcap.RESOURCE, t, 3, fair=True, **checkcap.order()) for t in tk]
        assert turn == [True, True, True, False], turn
        assert checkcap.over_share("A") == []
    finally:
        for t in tk:
            locks.dequeue(t)
        for s in held:
            s.close()
    held = _share_holders(monkeypatch, 4, {})
    got = [checkcap.acquire(checkcap.label("A"), project_name="A", max_wait_s=5) for _ in range(4)]
    try:
        assert all(got) and checkcap.running_by_project() == {"A": 4}, "alone, a project uses the whole cap"
    finally:
        for s in got:
            s and s.close()


def test_checks_fair_share_keeps_owed_and_urgent_first_and_a_requeued_run_its_place(env, monkeypatch):
    # A is over its share; its owed (priority 0) and urgent (1) waiters still go first, and its preempted
    # routine run (requeued) keeps the place it has without shares. Shares only order routine turns.
    from ttp import checkcap, locks
    held = _share_holders(monkeypatch, 4, {"A": 4})
    d, r = checkcap.slot_dir(), checkcap.RESOURCE
    t0 = time.time()
    tk = _share_queue([("a-routine", "A", 3), ("a-urgent", "A", 1), ("a-owed", "A", checkcap.OBLIGATION_PRIORITY)],
                      t0 - 600)
    tk += _share_queue([("b-routine", "B", 3), ("b-urgent", "B", 1)], t0 + 10)
    try:
        for m in (locks.URGENT_FIRST, 0, 2):
            with_share = [t.read_text() for t in locks.fair_order(d, r, urgent_max=m, share=checkcap.share())]
            plain = [t.read_text() for t in locks.fair_order(d, r, urgent_max=m)]
            pinned = lambda xs: [(i, x) for i, x in enumerate(xs) if "routine" not in x]
            assert pinned(with_share) == pinned(plain), (m, with_share, plain)
            assert with_share.index("b-routine") < with_share.index("a-routine"), (m, with_share)
        first = [t.read_text() for t in locks.fair_order(d, r, **checkcap.order())][:3]
        assert first[0] == "a-owed" and sorted(first[1:]) == ["a-urgent", "b-urgent"], first
        rq = locks.enqueue(d, r, "a-requeued", since=t0, group="A", priority=3, requeued=True)
        tk.append(rq)
        got = [t.read_text() for t in locks.fair_order(d, r, **checkcap.order())]
        plain = [t.read_text() for t in locks.fair_order(d, r, urgent_max=locks.URGENT_FIRST)]
        assert got.index("a-requeued") == plain.index("a-requeued"), (got, plain)
        locks.dequeue(tk[0])   # with no older routine waiter of A before it, it goes before B's later one
        got = [t.read_text() for t in locks.fair_order(d, r, **checkcap.order())]
        assert got.index("a-requeued") < got.index("b-routine"), got
    finally:
        for t in tk:
            locks.dequeue(t)
        for s in held:
            s.close()


def test_throughput_text_shows_the_host_checks_pool_per_project(env, monkeypatch):
    from ttp import throughput
    rep = {"thresholds_s": {"p1": 60, "other": 60}, "since_s": 86400, "lanes": [],
           "host_checks": {"cap": 4, "share": 2, "projects": {
               "A": {"grants": 9, "running": 3, "wait_s": 7200, "over_share_waits": 0, "over_share_by": []},
               "B": {"grants": 2, "running": 0, "wait_s": 9000, "over_share_waits": 2, "over_share_by": ["A"]}}}}
    out = throughput.text(rep)
    assert "Host checks pool (cap 4, fair share 2 each now), per project:" in out
    assert "- B: 2 grant(s), 0 running, waited 2h30m; 2 wait(s) ended while A held over its fair share" in out
    assert "- A: 9 grant(s), 3 running, waited 2h00m\n" in out + "\n"


def test_checks_fair_share_counts_runs_with_no_project_and_older_runtimes_labels(env, monkeypatch):
    # A run with no project counts as "" (its tickets' group); an older runtime's slot without
    # "checks_project" counts by its holder label; push and urgent slots beyond the cap count for none.
    from ttp import checkcap, locks
    held = _share_holders(monkeypatch, 4, {})
    held.append(checkcap.acquire(checkcap.label(None)))
    old = locks.try_take(checkcap.paths(), "C task #5 (run 9)", "ttp checks")
    pushed = checkcap.acquire_priority("branch-key", checkcap.label("D"), project_name="D")
    try:
        assert checkcap.running_by_project() == {"": 1, "C": 1}, checkcap.running_by_project()
        cap, run = checkcap.share()
        assert cap == 4 and run == {locks.group_id(""): 1, locks.group_id("C"): 1}
    finally:
        for s in held + [pushed]:
            s.close()
        old.close()


def test_a_queue_incident_times_a_holder_against_runs_of_its_own_command_list_and_calls_hang_only_on_silence(
        tmp_path, monkeypatch):
    """The project's recent median mixes cheap targeted runs with full suites. A full suite at 40 min whose
    own command list's median is 35 min is neither slow nor hung, though it is far over the mixed median; past
    the factor of its own median it is a likely hang only once its log stopped growing, else slow. A command
    list with no passing run yet has no baseline: its wall time alone flags nothing."""
    from ttp import checklane, checkstall, checkwall, locks, testslots
    _lane_env(monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    full, quick = ["pytest -q tests/"], ["pytest -q tests/test_a.py::test_one"]
    for _ in range(checkwall.MIN_RUNS):
        checkwall.record_run(state, 35 * 60, True, key="full")
        checkwall.record_run(state, 60, True, key="quick")
        checkwall.record_run(state, 60, True, key="quick")
    import statistics
    mixed = statistics.median(checkwall.recent(state))
    assert mixed == 60, "the project median is the targeted runs'"
    cfg = {"checks": {"wait_hang_factor": 3}}
    log = tmp_path / "full.log"
    log.write_text("..... [ 40%]\n")
    for key, cmds, stalls in (("full", full, False), ("new", quick, True)):
        g = testslots.suite_hold("pa", repo=_LANE_REPO, wait_s=5, cost=checklane.cost(state, key, cmds, cfg))
        try:
            testslots.suite_running(g, 0, str(log))
            rec = locks.holder_records([Path(f.name) for f in g.files])[0]
            start = float(rec["run_since"])
            evs = _queue_events(1, holders=[rec])
            if key == "full":
                assert checklane.scope_median_s(rec, mixed) == 35 * 60
                os.utime(log, (start + 40 * 60,) * 2)
                assert checklane.wait_wake(evs, {"start": 1.0}, cfg, start + 40 * 60, mixed)[0] == [], \
                    "40 min against its own 35 min median: no hang, not slow"
                os.utime(log, (start + 110 * 60,) * 2)
                why, inc = checklane.wait_wake(evs, None, cfg, start + 110 * 60, mixed)
                assert why == ["new"] and inc["slow"] and not inc["hang"], \
                    "over 3x its own median but still printing: slow, never a hang"
                text = checklane.wake_text(why, evs, inc, cfg, start + 110 * 60, mixed, load=lambda: "1 on 8 cores")
                assert "its own command list (35 min)" in text and "likely hang" not in text, text
                silent = start + 110 * 60 + checkstall.stall_s(cfg, rec) + 60
                why, inc = checklane.wait_wake(evs, {"start": 1.0}, cfg, silent, mixed)
                assert why == ["hang"], "no output for the stall window: a likely hang"
                text = checklane.wake_text(why, evs, inc, cfg, silent, mixed)
                assert "its own command list (35 min)" in text and "likely hang" in text, text
            else:
                assert checklane.scope_median_s(rec, mixed) is None
                os.utime(log, (start,) * 2)
                assert checklane.wait_wake(evs, {"start": 1.0}, cfg, start + 600 * 60, mixed)[0] == [], \
                    "no passing run of its command list yet: nothing to time it against"
        finally:
            g.close()


_FLOCK_HOLDER = """
import fcntl, sys, time
from pathlib import Path
f = open(sys.argv[1], "a")
fcntl.flock(f, fcntl.LOCK_EX)
Path(sys.argv[2]).write_text("held")
while not Path(sys.argv[3]).exists():
    time.sleep(0.05)
"""


def _stale_label(path, pid, holder):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"holder": holder, "pid": pid, "since": time.time() - 600}))


def test_slot_status_names_a_held_slots_live_holder_from_its_process_never_the_files_stale_json(
        env, tmp_path, monkeypatch, capsys):
    """#7173: `ttp checks --slot-status` lists a held host-wide checks slot by its live flock holder
    (/proc/locks), its project, task and run read from that process's environment, though the lock file
    still keeps the JSON of an earlier holder that released it."""
    import json as _json
    from ttp import checkcap, cli, locks
    if locks.flock_pids([]) is None:
        pytest.skip("no kernel lock table here")
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setattr(checkcap, "slot_dir", lambda: tmp_path / "slots")
    slot = checkcap.paths(2)[1]
    _stale_label(slot, 999999, "other task #1 (run 1)")
    got, stop = tmp_path / "held", tmp_path / "stop"
    proc = subprocess.Popen([sys.executable, "-c", _FLOCK_HOLDER, str(slot), str(got), str(stop)],
                            env={**os.environ, "TTP_PROJECT": str(p.base), "TTP_TASK": "12", "TTP_RUN_ID": "34"})
    try:
        assert _wait_for(got.exists)
        held = checkcap.slot_holders()
        assert held == [{"slot": slot.name, "kind": "cap", "project": p.name, "task": "12", "run": "34",
                         "pid": proc.pid, "alive": True, "source": "process"}], held
        with pytest.raises(SystemExit):
            cli.main(["checks", "--slot-status"])
        out = capsys.readouterr().out
        assert f"  {slot.name} (cap): {p.name} task #12 (run 34), pid {proc.pid} alive" in out, out
        section = out[out.index("host slots (held now"):]
        assert "other task #1" not in section, section
        with pytest.raises(SystemExit):
            cli.main(["checks", "--slot-status", "--json"])
        assert _json.loads(capsys.readouterr().out)["host_slots"]["held"] == held
    finally:
        stop.touch()
        proc.wait(10)
    assert checkcap.slot_holders() == [], "a released slot is no longer listed"


def test_slot_status_lists_no_released_slot_whose_file_keeps_its_last_holders_json(env, tmp_path, monkeypatch):
    """#7173: a slot file with a released holder's JSON (here our own live pid, so the record looks
    current) is not held: slot status lists it nowhere; with no kernel lock table the label serves,
    marked unchecked."""
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setattr(checkcap, "slot_dir", lambda: tmp_path / "slots")
    for i, path in enumerate(checkcap.paths(2)):
        _stale_label(path, os.getpid(), f"demo task #{i + 5} (run 9)")
    assert checkcap.slot_holders(lambda: []) == []
    import fcntl
    slot = checkcap.paths(2)[0]
    held = open(slot, "r+")
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        locks.write_label(held, {"holder": "demo task #7 (run 8)", "pid": os.getpid(), "since": time.time()})

        def no_table():
            raise OSError("no /proc")
        got = checkcap.slot_holders(no_table)
        assert [(h["slot"], h["task"], h["run"], h["pid"], h["source"]) for h in got] == [
            (slot.name, "7", "8", os.getpid(), "label, unchecked")], got
    finally:
        held.close()
    assert checkcap.slot_holders() == []


def test_flock_pids_matches_by_inode_skips_waiters_and_retries_a_missed_line(tmp_path):
    """#7173: locks.flock_pids reads the holder pid from the kernel's lock table by the file's device and
    inode, skips blocked waiters ("->"), and rereads a table that missed the line."""
    from ttp import locks
    f = tmp_path / "checks.0.lock"
    f.write_text("{}")
    st = os.stat(f)
    where = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    reads = [[], ["1: FLOCK  ADVISORY  WRITE 4242 00:99:1 0 EOF\n",
                  f"2: -> FLOCK  ADVISORY  WRITE 777 {where} 0 EOF\n",
                  f"2: FLOCK  ADVISORY  WRITE 4343 {where} 0 EOF\n"]]
    assert locks.flock_pids([f], lambda: reads.pop(0) if len(reads) > 1 else reads[0]) == {f: [4343]}


def test_slot_status_lists_the_host_parked_queue_in_admission_order_with_owed_and_priority_tags(
        env, tmp_path, monkeypatch):
    """#7173: slot status shows the first parked runs (checks-parked.queue) in the order they are admitted:
    an owed run (OBLIGATION_PRIORITY) ahead of an earlier routine one, each with its tags."""
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setattr(checkcap, "slot_dir", lambda: tmp_path / "slots")
    now = time.time()
    routine = locks.enqueue(checkcap.slot_dir(), checkcap.PARKED, "demo task #3 (run 1)", since=now - 30,
                            group="demo", priority=3)
    owed = locks.enqueue(checkcap.slot_dir(), checkcap.PARKED, "demo task #4 (run 2)" + checkcap.OBLIGATION_MARK,
                         since=now - 10, group="demo", priority=checkcap.OBLIGATION_PRIORITY)
    try:
        got = checkcap.parked_order()
        assert got["total"] == 2
        assert [(t["holder"], t["tags"], t["owed"]) for t in got["parked"]] == [
            ("demo task #4 (run 2)", ["owed", "p0"], True), ("demo task #3 (run 1)", ["p3"], False)], got
        lines = checkcap.slot_lines([], got, now=now)
        assert lines[-3:] == ["host parked queue (admission order):", "  1. demo task #4 (run 2) [owed, p0], parked 10s",
                              "  2. demo task #3 (run 1) [p3], parked 30s"], lines
        assert "  none held" in lines
        assert checkcap.parked_order(first=1)["parked"][0]["holder"] == "demo task #4 (run 2)"
    finally:
        locks.dequeue(owed)
        locks.dequeue(routine)


def test_a_preempted_checks_runs_rerun_logs_its_wait_as_cut_by_the_preempting_project(env, tmp_path, monkeypatch,
                                                                                      capsys):
    """[note 82418fd2fe098005]: a routine run preempted by another project's urgent run queues again at its
    first arrival; its rerun's waits-log record carries cut_at (its old grant) and cut_by (the asker's project,
    checkcap.preempt's request), so throughput books that wait to the preempting project, and its lane wait
    (lane_s, of which pool_s the host's test pool held)."""
    from ttp import checkcap, checkwall, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkwall, "POLL_S", 0.1)
    monkeypatch.setenv(checkcap.PREEMPTED_ENV, "")
    monkeypatch.setenv(checkcap.CUT_ENV, "")
    monkeypatch.setenv("TTP_TASK", "7")
    mark, req = tmp_path / "ran-once", checkcap.slot_dir() / f"{checkcap.PREEMPT}.{os.getpid()}.json"
    script = (f'if [ -e {mark} ]; then exit 0; fi; touch {mark}; '
              f'printf \'{{"by": "urgent u", "task": "77", "project": "other-project", "at": %s}}\' '
              f'"$(($(date +%s) + 1))" > {req}; sleep 60')
    cli.main(["checks", "--", "sh", "-c", script])
    assert "preempted by urgent #77" in capsys.readouterr().out
    rows = checkcap.wait_records(p.state)
    assert len(rows) == 2 and "cut_at" not in rows[0], rows
    assert rows[1]["cut_by"] == "other-project" and rows[0]["granted_at"] - 1 <= rows[1]["cut_at"], rows
    assert all(r["lane_s"] >= 0 and 0 <= r["pool_s"] <= r["lane_s"] for r in rows), rows
    assert checkcap.CUT_ENV in checkcap.RUN_ONLY_ENV and checkcap.LANE_WAIT_ENV in checkcap.RUN_ONLY_ENV


def _adopt_queue(env, tmp_path):
    """A project, a repository whose branch `lane` holds the checks' head, and run_of: a run of a task in a
    status whose detached checks (one live process) wait in the checks queue, or run (waits=False)."""
    from ttp import cli
    from ttp.runner import proc_start
    p = make(env)
    repo = tmp_path / "lanes"
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "lane", str(repo)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "a"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    subprocess.run([*git, "checkout", "-q", "--orphan", "other"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "b"], check=True)
    proc = subprocess.Popen(["sleep", "60"])
    started = proc_start(proc.pid)

    def run_of(status, waits=True, branch="lane"):
        tid = p.db.add_task("t", "s", kind="code")
        p.db.x("UPDATE tasks SET status=?, branch=?, updated=? WHERE id=?", (status, branch, time.time(), tid))
        run_dir = tmp_path / f"run-{tid}"
        run_dir.mkdir()
        p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
               (tid, "worker", "fake", time.time(), "ok", str(run_dir)))
        (run_dir / cli.CHECKS_PID).write_text(json.dumps({"pid": proc.pid, "started": started, "head": head,
                                                           "worktree": str(repo), "branch": branch}))
        if waits:
            (run_dir / cli.CHECKS_SLOT_WAIT).write_text(json.dumps({"pid": proc.pid}))
        return tid, run_dir
    return p, proc, run_of


def _continue(p, tid, status="queued", branch=None):
    """An open task that continues #tid, queued as the coordinator queues one (task_add with continues)."""
    from ttp import coordinator as coord
    title = f"continue: t{tid} {status} {branch}"
    assert not coord.apply(p, [{"type": "task_add", "title": title, "spec": "Go on.", "kind": "code",
                                "continues": tid, "force": True}])
    new = p.db.one("SELECT * FROM tasks WHERE title=?", (title,))
    assert new and f'"continues:{tid}"' in new["labels"]
    p.db.x("UPDATE tasks SET status=?, branch=? WHERE id=?", (status, branch, new["id"]))
    return new["id"]


def test_an_ended_tasks_waiting_checks_leave_the_queue_unless_an_open_continue_adopts_their_head(env, tmp_path):
    # #7885: a failed task's waiting checks held the host-wide queue, and the one that an open continue
    # collected (it continues the failed task on the same head) was killed with the rest.
    from ttp import cli, jobstop
    from ttp.daemon import Daemon
    p, proc, run_of = _adopt_queue(env, tmp_path)
    try:
        failed, cancelled, exhausted = run_of("failed"), run_of("cancelled"), run_of("exhausted")
        moved_off = run_of("failed")   # its continue works on a branch that no longer holds the head
        _continue(p, moved_off[0], branch="other")
        adopted = run_of("failed")      # #7884 continues #7866 and collects its run's checks
        _continue(p, adopted[0])
        chained = run_of("cancelled")   # a continue of a continue adopts them too
        _continue(p, _continue(p, chained[0], status="failed", branch="lane"), status="running", branch="lane")
        running = run_of("failed", waits=False)   # holds its slot and runs its commands
        Daemon(p.base).reap_waiting_checks(every_s=0)
        why = lambda r: json.loads((r[1] / cli.CHECKS_SLOT_CANCEL).read_text())["why"]   # noqa: E731
        for (tid, _), status in ((failed, "failed"), (cancelled, "cancelled"), (exhausted, "exhausted"),
                                 (moved_off, "failed")):
            assert why((tid, _)) == f"task #{tid} ended ({status})"
        for r in (adopted, chained, running):
            assert not (r[1] / cli.CHECKS_SLOT_CANCEL).exists(), r
        assert jobstop.checks_adopter(p, adopted[0], json.loads((adopted[1] / cli.CHECKS_PID).read_text()))
        # The daemon's job sweep leaves adopted checks alone and asks waiting ones to leave the queue rather
        # than signal them mid-wait (a bare exit code 143 with no reason).
        for r in (failed, adopted):
            (r[1] / cli.CHECKS_SLOT_CANCEL).unlink(missing_ok=True)
            assert jobstop._end_ended_checks(p, r[1], r[0], "failed") is None
        assert proc.poll() is None, "no lane was signalled"
        assert why(failed) == f"task #{failed[0]} ended (failed)"
        assert not (adopted[1] / cli.CHECKS_SLOT_CANCEL).exists()
    finally:
        proc.kill()
        proc.wait()


def test_a_failed_tasks_waiting_checks_drop_themselves_and_their_result_says_why(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    from ttp.daemon import Daemon
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    results = []
    for by in ("daemon", "itself"):
        tid = p.db.add_task("t", "s", kind="code")
        monkeypatch.setenv("TTP_TASK", str(tid))
        p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
               (tid, "worker", "fake", time.time(), "ok", str(run)))
        for name in (cli.CHECKS_RC, cli.CHECKS_DROPPED, cli.CHECKS_SLOT_CANCEL, cli.CHECKS_SLOT_WAIT):
            (run / name).unlink(missing_ok=True)
        if by == "itself":   # ended before its checks asked for a slot: it never takes one
            p.db.x("UPDATE tasks SET status='failed' WHERE id=?", (tid,))
        holder = _cap_holder(by)
        try:
            cli.main(["checks", "--detach", "--", "true"])
            if by == "daemon":
                assert _wait_for(lambda: (run / cli.CHECKS_SLOT_WAIT).exists())
                p.db.x("UPDATE tasks SET status='failed' WHERE id=?", (tid,))
                Daemon(p.base).reap_waiting_checks(every_s=0)
            assert _wait_for(lambda: (run / cli.CHECKS_RC).exists()), by
        finally:
            holder.kill()
            holder.wait()
        capsys.readouterr()
        with pytest.raises(SystemExit):
            cli.main(["checks", "--result", str(run)])
        results.append(capsys.readouterr().out)
        assert f"dropped: task #{tid} ended (failed)" in results[-1], (by, results[-1])
        assert "lost" not in results[-1]


# A push batch's checks have no task by design (slot "push", or an ordinary turn after it): every wait
# and holder line names them "push batch <id>"; only a run with neither a task nor a batch has no owner.
# Their labels come from the writers the batch process uses: checkcap.label(name, "push batch <id>")
# (batch.Batch._slot) and testslots.suite_hold, under checkcap.BATCH_ENV as pushq._spawn sets it.

_BATCH_ID = "batch-20260101-000000-4242"   # synthetic


def _push_status(capsys):
    assert _ttp("checks", "--slot-status") == 0
    text = capsys.readouterr().out
    assert _ttp("checks", "--slot-status", "--json") == 0
    return text, json.loads(capsys.readouterr().out)


def test_a_push_batch_waiter_shows_as_push_batch_id_in_the_wait_event_and_slot_status(
        env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, checklane, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    monkeypatch.setenv(checkcap.PRIORITY_WAIT_ENV, "0.3")
    key = testslots.repo_key(repo)
    holder = _lane_holder(tmp_path, p.name, key)   # task #12 holds the lane
    monkeypatch.delenv("TTP_TASK", raising=False)
    monkeypatch.setenv(checkcap.BATCH_ENV, _BATCH_ID)
    seen = {}

    def lane_wait(_line):   # told at once, with its ticket queued for 1000 s
        if not seen:
            seen["events"] = checklane.wait_events(p.name, {})
            seen["lines"] = checklane.long_wait_lines(p.name, {})
            seen["status"] = checklane.status_text(p.name, {})
            seen["lane"] = checklane.lane_status(p.name)
    try:
        assert testslots.suite_hold(p.name, repo=key, wait_s=0.3, since=time.time() - 1000, tell=lane_wait) is None
    finally:
        _stop(holder)
    (ev,) = seen["events"]
    assert ev["text"].startswith(f"Checks lane wait: push batch {_BATCH_ID} has waited 16 min"), ev
    assert ev["task"] is None and "no task" not in ev["text"], ev
    assert f"push batch {_BATCH_ID} has waited" in seen["lines"][0], seen["lines"]
    assert f"waiter 1 (next): push batch {_BATCH_ID}" in seen["status"], seen["status"]
    assert seen["lane"][0]["waiters"][0]["batch"] == _BATCH_ID and seen["lane"][0]["waiters"][0]["task"] == ""
    # The push priority slot: another push holds it, so the batch waits for it and slot-status lists it.
    monkeypatch.setenv(checkcap.BATCH_ENV, "batch-20260101-000000-1")
    other = checkcap.acquire_priority("git@example:org/repo#main", checkcap.label(p.name, "push batch batch-20260101-000000-1"))
    monkeypatch.setenv(checkcap.BATCH_ENV, _BATCH_ID)
    got = {}

    def told(_line):
        if not got and "waiting for the push priority slot" in _line:
            got["status"] = _push_status(capsys)
    try:
        slot = checkcap.acquire_priority("git@example:org/repo#main", checkcap.label(p.name, f"push batch {_BATCH_ID}"),
                                         "push checks", told, fallback=lambda: checkcap.Slot())
        slot.close()
    finally:
        other.close()
    text, js = got["status"]
    assert f"waiter (push slot): push batch {_BATCH_ID} of {p.name}" in text, text
    assert f"holder (push slot): push batch batch-20260101-000000-1 of {p.name}" in text, text
    rows = {r["batch"]: r for r in js["push"]}
    assert rows[_BATCH_ID]["slot"] == "push" and rows[_BATCH_ID]["state"] == "waiting", rows
    assert rows[_BATCH_ID]["project"] == p.name
    assert checkcap.push_rows() == [], "the waiter's ticket goes with its wait"


def test_a_push_batch_holder_shows_as_push_batch_id_in_the_wait_event_and_slot_status(
        env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, checklane, locks, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    key = testslots.repo_key(repo)
    monkeypatch.delenv("TTP_TASK", raising=False)
    monkeypatch.setenv(checkcap.BATCH_ENV, _BATCH_ID)
    push = checkcap.acquire_priority("git@example:org/repo#main", checkcap.label(p.name, f"push batch {_BATCH_ID}"),
                                     "push checks", state=p.state)
    lane = testslots.suite_hold(p.name, repo=key, what="pytest -q")
    monkeypatch.delenv(checkcap.BATCH_ENV)
    ticket = locks.enqueue(checkcap.slot_dir(), testslots.suite_base(p.name, key),
                           f"task #13 {p.name}: full checks (run 9, pid 1)", time.time() - 1000, priority=2)
    try:
        (ev,) = checklane.wait_events(p.name, {})
        assert ev["task"] == 13 and f"behind push batch {_BATCH_ID} 'pytest -q'" in ev["text"], ev
        status = checklane.status_text(p.name, {})
        assert f"holder: push batch {_BATCH_ID}" in status and "no task" not in status, status
        assert any(f"push batch {_BATCH_ID} queued" in x for x in checklane.lane_lines(p.name)), checklane.lane_lines(p.name)
        text, js = _push_status(capsys)
        assert f"holder (push slot): push batch {_BATCH_ID} of {p.name}" in text, text
        (row,) = js["push"]
        assert (row["slot"], row["state"], row["batch"], row["project"]) == ("push", "holding", _BATCH_ID, p.name), row
        assert js["repos"][0]["lanes"][0]["batch"] == _BATCH_ID
        (w,) = [r for r in checkcap.wait_records(p.state) if r["slot"] == "push"]
        assert w["batch"] == _BATCH_ID and w["task"] is None, w
    finally:
        locks.dequeue(ticket)
        lane.close()
        push.close()


def test_a_run_with_neither_a_task_nor_a_push_batch_still_shows_as_having_no_owner(
        env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, checklane, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _lane_env(monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    key = testslots.repo_key(repo)
    holder = _lane_holder(tmp_path, p.name, key)
    for k in ("TTP_TASK", "TTP_TASK_ID", "TTP_RUN_ID", checkcap.BATCH_ENV):   # no run id: "run 41 (no task)" otherwise
        monkeypatch.delenv(k, raising=False)
    # nothing in the runner's processes links the waiter to a task
    monkeypatch.setattr(checklane, "_proc", lambda pid: {"command": "", "cwd": "", "ppid": None, "sid": None, "env": None})
    seen = {}

    def lane_wait(_line):
        if not seen:
            seen["events"] = checklane.wait_events(p.name, {})
            seen["status"] = checklane.status_text(p.name, {})
            seen["lane"] = checklane.lane_status(p.name)
    try:
        assert testslots.suite_hold(p.name, repo=key, wait_s=0.3, since=time.time() - 1000, tell=lane_wait) is None
    finally:
        _stop(holder)
    (ev,) = seen["events"]
    assert ev["text"].startswith("Checks lane wait: a run with no task ("), ev
    assert "push batch" not in ev["text"] and "waiter 1 (next): no task" in seen["status"], seen["status"]
    assert seen["lane"][0]["waiters"][0]["batch"] == ""
    # A plain `ttp push` on the priority slot names no batch.
    push = checkcap.acquire_priority("git@example:org/repo#main", checkcap.label(p.name, "ttp push"), state=p.state)
    try:
        text, js = _push_status(capsys)
        (row,) = js["push"]
        assert row["batch"] is None and row["slot"] == "push" and "push batch" not in text, (row, text)
        assert checkcap.owner(None, "") == "a run with no task" and checkcap.batch_of({"holder": "pa ttp push"}) == ""
        (w,) = [r for r in checkcap.wait_records(p.state) if r["slot"] == "push"]
        assert "batch" not in w, w
    finally:
        push.close()




def _slot_status(cli, capsys, as_json=True):
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--slot-status", *(["--json"] if as_json else [])])
    assert e.value.code == 0
    out = capsys.readouterr().out
    return json.loads(out) if as_json else out


def test_slot_status_lists_a_spilled_run_with_its_host_and_pid_though_it_holds_no_lane(env, tmp_path, monkeypatch,
                                                                                       capsys):
    """cells: slot_status_spilled. A run on a spill host (a push batch or an after-landing run: no lane here) is a
    live run in slot-status's `runs`, with its host and pid, and in the text; with nothing live it says none."""
    from ttp import checkspill, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    assert _slot_status(cli, capsys)["runs"] == []
    assert "live check runs: none running or waiting" in _slot_status(cli, capsys, False)
    claim = checkspill._claim("spill-a", 1, {"project": p.name, "task": "7", "run": "99", "pid": os.getpid(),
                                             "since": time.time(), "what": "pytest -q", "host": "spill-a"})
    try:
        d = _slot_status(cli, capsys)
        assert d["repos"] == [], d["repos"]   # no lane: the old view showed nothing
        assert [(r["state"], r["host"], r["pid"], r["run"], r["task"]) for r in d["runs"]] == [
            ("spilled", "spill-a", os.getpid(), "99", "7")], d["runs"]
        text = _slot_status(cli, capsys, False)
        assert "live check runs: 0 running here, 1 on spill hosts, 0 waiting" in text, text
        assert f"spilled: #7 run 99 pid {os.getpid()} on spill-a" in text, text
    finally:
        claim.close()


def test_slot_status_lists_a_run_waiting_for_a_slot_with_what_holds_it(env, tmp_path, monkeypatch, capsys):
    """cells: slot_status_waiting. A run whose checks wait for a slot here (its CHECKS_SLOT_WAIT, its pid live)
    is in slot-status's `runs` as waiting, with what holds it, though no lane ticket shows it."""
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    tid = p.db.add_task("t", "s", kind="code")
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(?,?,?,?,?)",
                 (tid, "worker", "claude", time.time(), "running"))
    wdir = Path(p.runs) / str(rid)
    wdir.mkdir(parents=True)
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(wdir), rid))
    (wdir / cli.CHECKS_SLOT_WAIT).write_text(json.dumps({"since": time.time() - 60, "pid": os.getpid(), "head": "ab12"}))
    d = _slot_status(cli, capsys)
    assert d["repos"] == [], d["repos"]
    w = [r for r in d["runs"] if r["state"] == "waiting"]
    assert len(w) == 1 and w[0]["task"] == tid and w[0]["pid"] == os.getpid(), d["runs"]
    assert w[0]["holds"].startswith("a checks slot here"), w
    (wdir / cli.CHECKS_SLOT_WAIT).write_text(json.dumps({"since": time.time(), "pid": os.getpid(), "head": "ab12",
                                                         "suite": "full", "limit": 2}))
    text = _slot_status(cli, capsys, False)
    assert "live check runs: 0 running here, 0 on spill hosts, 1 waiting" in text, text
    assert f"waiting: #{tid} run {rid} pid {os.getpid()}, held by the test slots of suite full (limit 2)" in text
