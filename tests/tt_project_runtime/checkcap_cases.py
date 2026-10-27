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
    from ttp.daemon import Daemon
    from ttp.runner import proc_start
    p = make(env)
    proc = subprocess.Popen(["sleep", "60"])
    try:
        started = proc_start(proc.pid)

        def run_of(status, wait_pid=None, start=None, updated=None, waits=True):
            tid = p.db.add_task("t", "s", kind="code")
            p.db.x("UPDATE tasks SET status=?, updated=? WHERE id=?", (status, updated or time.time(), tid))
            run_dir = tmp_path / f"run-{tid}"
            run_dir.mkdir()
            p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
                   (tid, "worker", "fake", time.time(), "ok", str(run_dir)))
            (run_dir / cli.CHECKS_PID).write_text(json.dumps({"pid": proc.pid, "started": start or started}))
            if waits:
                (run_dir / cli.CHECKS_SLOT_WAIT).write_text(json.dumps({"pid": wait_pid or proc.pid}))
            return tid, run_dir

        stop = {s: run_of(s) for s in ("done", "failed", "cancelled")}
        keep = [run_of("done", waits=False), run_of("done", wait_pid=proc.pid + 100000), run_of("done", start="1"),
                run_of("running"), run_of("waiting"), run_of("done", updated=time.time() - 2 * 86400)]
        d = Daemon(p.base)
        d.reap_waiting_checks(every_s=0)
        for status, (tid, run_dir) in stop.items():
            assert json.loads((run_dir / cli.CHECKS_SLOT_CANCEL).read_text()) == {
                "pid": proc.pid, "started": started, "why": f"task #{tid} ended ({status})"}
        assert not [r for _, r in keep if (r / cli.CHECKS_SLOT_CANCEL).exists()], \
            "checks with a slot, another process and live tasks are left alone"
        path = stop["done"][1] / cli.CHECKS_SLOT_CANCEL
        os.utime(path, (1000, 1000))
        d.reap_waiting_checks(every_s=0)
        assert path.stat().st_mtime == 1000, "the same request is not written again"
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
    monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (load[0], 0.0, 0.0))
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
    assert "3 running (cap 1 +2 while the load is low)" in checkcap.line()
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
    assert cli._low_priority(["pytest"]) == ["nice", "-n", "10", "pytest"]
    have.add("ionice")
    assert cli._low_priority(["pytest"]) == ["nice", "-n", "10", "ionice", "-c2", "-n7", "pytest"]
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
    monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (load[0], 0.0, 0.0))
    assert checkcap.default_range() == (4, 8)
    assert checkcap.limit() == 8, "an idle host gets the ceiling"
    load[0] = 14.0
    assert checkcap.limit() == 8, "headroom for more than the ceiling still stops at it"
    load[0] = 36.0
    assert checkcap.limit() == 7, "(64 - 36) / 4 checks fit"
    load[0] = 43.0
    assert checkcap.limit() == 5, "(64 - 43) / 4 rounds down"
    load[0] = 50.0
    assert checkcap.limit() == 4, "never below the floor of one slot per 16 cores"
    load[0] = 200.0
    assert checkcap.limit() == 4
    monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (_ for _ in ()).throw(OSError()))
    assert checkcap.limit() == 4, "an unreadable load average gives the floor"
    monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (load[0], 0.0, 0.0))
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
    monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (load[0], 0.0, 0.0))
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
    first = checkcap.acquire("A task #7", since=time.time() - 30, project_name=p.name, priority=2, state=p.state)
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
import os, sys, time
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
    assert testslots.sizes() is None and testslots.worker_env({}) == {}, "test_pool 0 turns the pool off"


def test_test_slots_project_config_can_only_lower_the_host_limits(env, monkeypatch):
    from ttp import testslots
    monkeypatch.setattr(testslots.checkcap, "cores", lambda: 32)
    assert testslots.sizes({"checks": {"test_pool": 100, "test_run_max": 50}}) == (16, 8), "never raised"
    assert testslots.sizes({"checks": {"test_pool": 4}}) == (4, 4)
    assert testslots.sizes({"checks": {"test_pool": 6, "test_run_max": 2}}) == (6, 2)
    assert testslots.sizes({"checks": {"test_pool": 0}}) == (16, 8), "a project cannot turn the host's pool off"
    assert testslots.worker_env({"checks": {"test_run_max": 3}}) == {
        "PYTEST_XDIST_AUTO_NUM_WORKERS": "3", "TTP_TEST_SLOTS": "3"}


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
