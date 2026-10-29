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
    assert cli._low_priority(["pytest"]) == ["nice", "-n", "10", "ionice", "-c3", "pytest"]
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
        "PYTEST_XDIST_AUTO_NUM_WORKERS": "3", "TTP_TEST_SLOTS": "3"}
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
    assert testslots.project_max() == 4, "default: twice a run's share, within the pool"
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
    monkeypatch.setenv("TTP_TEST_POOL", "4")
    monkeypatch.setenv("TTP_TEST_RUN_MAX", "2")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    tasks = [testslots.acquire("t1", project_name="pa"), testslots.acquire("t2", project_name="pb")]
    assert [g.k for g in tasks] == [2, 2]
    t0 = time.time()
    push = testslots.acquire("push", project_name="pa", push_key="remote main", wait_s=0)
    assert push is not None and push.k == 1 and time.time() - t0 < 2, "(a) granted at once on the reserved slot"
    assert locks.held_labels([testslots.reserved_path("remote main")]) == ["push"]
    # (b) the reserved slot never goes to a task check: with the pool full, one waits for the pool.
    assert testslots.acquire("t3", project_name="pc", wait_s=0.3) is None
    other = testslots.acquire("push 2", project_name="pc", push_key="remote other", wait_s=0)
    assert other is not None and other.k == 1, "each push branch has its own reserved slot"
    push.close()
    other.close()
    tasks.pop().close()
    # (c) with free pool slots a push takes them within the per-run and per-project budgets.
    monkeypatch.setenv("TTP_TEST_PROJECT_MAX", "2")
    push = testslots.acquire("push", project_name="pb", push_key="remote main", wait_s=0)
    assert push.k == 2 and not locks.held_labels([testslots.reserved_path("remote main")])
    push.close()
    push = testslots.acquire("push", project_name="pa", push_key="remote main", wait_s=0)
    assert push.k == 1 and locks.held_labels([testslots.reserved_path("remote main")]) == ["push"], \
        "its project at budget: the reserved slot, never a pool slot past the budget"
    over = testslots.acquire("t4", project_name="pa", wait_s=0.3)
    assert over is None, "a task check of a project at its budget waits though the pool has room"
    push.close()
    tasks.pop().close()


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


def test_detached_checks_waiting_for_their_project_s_full_checks_are_queued_and_stop_once_their_task_ended(
        env, tmp_path, monkeypatch, capsys):
    """The per-project suite-lock wait is told like the checks-slot wait: CHECKS_SLOT_WAIT names it, the
    `--result` probe says queued, and the daemon's cancel for an ended task ends it with nothing run."""
    from ttp import cli, testslots
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
        p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (time.time(), tid))
        p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
               (tid, "worker", "fake", time.time(), "ok", str(run)))
        Daemon(p.base).reap_waiting_checks(every_s=0)
        assert _wait_for(lambda: (run / cli.CHECKS_RC).exists()), "the cancel did not end the wait"
    finally:
        hold.close()
    assert (run / cli.CHECKS_RC).read_text().strip() == "1" and not ran.exists()
    assert not (run / cli.CHECKS_SLOT_WAIT).exists()
    assert f"not run: stopped waiting for this project's other full checks: task #{tid} ended (done)" in \
        (run / "checks.log").read_text()


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

    def held(name, tell, *a, **k):
        return suite_hold(name, lambda line: (tell(line), timer.ident or timer.start()), *a, **k)
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
    assert hook.too_many_workers("pytest -n 4 tests/a.py") is None
    assert hook.too_many_workers("pytest -n auto tests/a.py") is None
    assert hook.too_many_workers("ttp checks -- 'pytest -n 16 tests/a.py'") is None, "ttp checks lowers it"
    assert hook.too_many_workers("head -n 50 f && sort -n 99 g") is None
    monkeypatch.delenv("TTP_TEST_SLOTS")
    assert hook.too_many_workers("pytest -n 64 tests") is None, "outside a worker run: no budget"


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
_FAKE_MAKE = """#!/bin/sh
echo "args=$* MAKEFLAGS=$MAKEFLAGS CMAKE=$CMAKE_BUILD_PARALLEL_LEVEL" > "$MAKE_SEEN"
echo $$ > "$MAKE_SEEN.pid"
while [ -n "$MAKE_STOP" ] && [ ! -e "$MAKE_STOP" ]; do sleep 0.05; done
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
    """A /proc with `processes forks` in stat and pid -> (cgroup unit or None, cmdline)."""
    import shutil
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    (root / "stat").write_text(f"cpu  1 2 3\nprocesses {forks}\nprocs_running 1\n")
    for pid, (unit, cmd) in procs.items():
        d = root / str(pid)
        d.mkdir()
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
        d = SimpleNamespace(p=SimpleNamespace(name=name), observe=lambda *a, **k: seen.append((a, k)))
        d._churn = SimpleNamespace(step=lambda now: obs_alpha)
        obs_alpha = {"unit": service.unit_name(SimpleNamespace(name="alpha")), "text": "x", "severity": "normal",
                     "key": "process-churn:tt-project-alpha"}
        daemon.Daemon.check_churn(d)
    assert len(seen) == 1 and seen[0][1]["key"] == "process-churn:tt-project-alpha"
    import pathlib
    src = pathlib.Path(churnwatch.__file__).read_text()
    assert "os.kill(" not in src and "import signal" not in src, "read-only: it signals nothing"


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
    assert _argv_with(monkeypatch) == ["nice", "-n", "10", "ionice", "-c3", "pytest"]


def test_check_priority_follows_checks_nice_and_ionice_settings(env, monkeypatch):
    got = _argv_with(monkeypatch, {"checks.nice": 19, "checks.ionice": "idle"})
    assert got == ["nice", "-n", "19", "ionice", "-c3", "pytest"]
    from ttp import project, testslots
    project.set_account_setting("checks.ionice", "best-effort:3")
    assert testslots.low_priority(["x"])[4:8] == ["-c2", "-n", "3", "x"]
    project.set_account_setting("checks.nice", 40)
    assert testslots.low_priority(["x"])[2] == "19", "clamped to 0..19"


def test_check_priority_falls_back_to_runner_nice(env, monkeypatch):
    assert _argv_with(monkeypatch, runner={"nice": 15})[:3] == ["nice", "-n", "15"]


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


def test_check_priority_invalid_values_fall_back_to_the_defaults(env, monkeypatch):
    got = _argv_with(monkeypatch, {"checks.nice": "soon", "checks.ionice": "bogus:9"}, runner={"nice": "x"})
    assert got == ["nice", "-n", "10", "ionice", "-c3", "pytest"]
    from ttp import project, testslots
    project.set_account_setting("checks.ionice", "best-effort:9")
    assert testslots.ionice_args() == ["-c3"]


def test_every_checks_key_the_runtime_reads_is_an_account_key_and_settable(env):
    import re
    from pathlib import Path
    from ttp import cli, project, testslots
    src = "".join(f.read_text() for f in Path(project.__file__).parent.glob("*.py"))
    read = set(re.findall(r'["\']checks\.([a-z_]+)["\']', src))
    for kind in (testslots.TESTS, testslots.BUILDS):
        read |= {kind.setting(s)[1] for s in ("pool", "run_max", "project_max")}
    read -= {"json", "log", "out", "pid", "queued", "rc"}   # file names (checks.json), not keys
    # project-only: the project's own lock or queue wrapper, set by the user per project
    read -= {"wrap_cmd"}
    assert "wrap_cmd" in project._known_keys(["checks"])
    missing = read - project.ACCOUNT_KEYS["checks"]
    assert not missing, f"checks keys read but not account keys: {sorted(missing)}"
    for k in ("test_project_max", "test_pool", "test_run_max", "build_pool", "build_run_max",
              "build_project_max", "nice", "ionice"):
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
            "p1 before an earlier p3; equal priorities by arrival; no priority last"
        # No starvation: each SUITE_AGE_S waited is one step more urgent, down to 0, so a p5 waiter
        # waits at most 5 steps plus the runs of those that arrived before it.
        p5_starved = _suite_ticket("pa", 5, now - 5 * age - 1)
        p5_aging = _suite_ticket("pa", 5, now - 2 * age - 1)
        assert testslots.suite_priority(p5_starved, now) == 0 and testslots.suite_priority(p5_aging, now) == 3
        order = testslots.suite_order("pa", now=now)
        assert order[0] == p5_starved, "a waiter aged to 0 goes before any newer p1"
        assert order.index(p5_aging) > order.index(p2_b) and order.index(p5_aging) < order.index(p3_old), \
            "aged to p3, it goes after the p2s and, arrived earlier, before the p3 that waits"
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


def test_suite_slot_status_line_shows_the_holder_s_time_against_its_cap_and_the_waiting_order(env, monkeypatch):
    from ttp import checkcap, locks, testslots
    monkeypatch.delenv(testslots.HELD_ENV, raising=False)
    monkeypatch.delenv(checkcap.HELD_ENV, raising=False)
    assert testslots.suite_line("pa") == "", "free and nobody waits: no line"
    monkeypatch.setenv("TTP_TASK", "12")
    hold = testslots.suite_hold("pa", priority=2)
    now = time.time()
    try:
        assert "#12 p2 held 0 min, not running yet" in testslots.suite_line("pa", now)
        testslots.suite_running(hold, 7200)
        _suite_ticket("pa", 3, now - 60, "31")
        _suite_ticket("pa", 4, now - 2 * testslots.SUITE_AGE_S + 60, "32")
        line = testslots.suite_line("pa", now + 601)
        assert line.startswith("full checks slot: #12 p2 running 10 min of its 2 h 0 min cap"), line
        assert line.endswith("waiting in order: #32 p4 (aged to p2) waited 39 min, #31 p3 waited 11 min"), line
    finally:
        for t in list(locks._TICKETS):
            locks.dequeue(t)
        hold.close()


_SUITE_RUNNER = """
import os, sys, time
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
    from ttp import checkcap, testslots
    monkeypatch.delenv(testslots.HELD_ENV, raising=False)
    monkeypatch.delenv(checkcap.HELD_ENV, raising=False)
    procs = []
    for role in ("hold", "wait"):
        ready = tmp_path / role
        procs.append(subprocess.Popen([sys.executable, "-c", _SUITE_RUNNER, "pa", str(ready), role],
                                      env={**os.environ, "PYTHONPATH": str(RUNTIME)}))
        assert _wait_for(ready.exists)
    try:
        line = testslots.suite_line("pa")
        assert "running 0 min of its 1 h 0 min cap" in line and "#5 p1" in line, line
    finally:
        for proc in procs:
            proc.kill()
            proc.wait(10)
    assert testslots.suite_path("pa").read_text(), "the holder's label stays on disk"
    assert testslots.suite_line("pa") == "", "a dead holder and waiter show nothing"
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


# Checks lanes (checklane): light checks never queue, N lanes per project and repository, dead holders
# reclaimed, a cheaper waiter's overflow lane, visible waits, and `ttp checks status`.

_LANE_REPO = "0123456789ab"   # a repository key (testslots.repo_key)

_LANE_HOLDER = """
import json, sys, time
from pathlib import Path
from ttp import checklane, testslots
checklane.HEARTBEAT_S = 3600.0   # no touch of the lane file while a test compares it
g = testslots.suite_hold(sys.argv[1], repo=sys.argv[3], what=sys.argv[4], cost=json.loads(sys.argv[5]))
Path(sys.argv[2]).write_text("held")
while True:
    time.sleep(1)
"""


def _lane_holder(tmp_path, name, repo, what="pytest -q", cost=None, task="12"):
    got = tmp_path / f"held-{time.time_ns()}"
    proc = subprocess.Popen([sys.executable, "-c", _LANE_HOLDER, name, str(got), repo, what, json.dumps(cost or {})],
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
    from ttp import checklane
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
    assert checklane.default_slots(1, 100) == 1 and checklane.default_slots(0, 0) == 1
    assert checklane.default_slots(64, 1000) == checklane.MAX_LANES
    assert checklane.default_slots(64, 10) == 1, "little free memory keeps one lane"
    assert checklane.default_slots(16, None) == 2
    monkeypatch.delenv(checklane.SLOTS_ENV)
    monkeypatch.setattr(checklane, "default_slots", lambda: 3)
    assert checklane.slots({}) == 3
    monkeypatch.setenv(checklane.SLOTS_ENV, "junk")
    assert checklane.slots({}) == 3
    project.set_account_setting("checks.slots", 2)
    assert checklane.slots({}) == 2
    assert checklane.slots({"checks": {"slots": 1}}) == 1, "the project's setting wins"
    assert checklane.slots({"checks": {"slots": 0}}) == 3, "an invalid setting falls back to the machine's"


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
    orig = testslots.suite_order

    def slow(*a, **k):
        q = orig(*a, **k)
        if threading.current_thread().name == "late" and not stalled.is_set():
            stalled.set()
            released.wait(10)
        return q
    monkeypatch.setattr(testslots, "suite_order", slow)

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


def test_a_cheaper_waiter_gets_an_overflow_lane_after_max_wait_and_only_then(env, tmp_path, monkeypatch):
    from ttp import checklane, testslots
    _lane_env(monkeypatch)
    holder = _lane_holder(tmp_path, "pa", _LANE_REPO, cost={"cls": checklane.SUITE, "est_s": 600})
    try:
        for cost, max_wait in (({"cls": checklane.SUITE}, 0.3), ({"cls": checklane.OTHER, "est_s": 900}, 0.3),
                               ({"cls": checklane.OTHER}, 0)):
            assert testslots.suite_hold("pa", repo=_LANE_REPO, cost=cost, max_wait=max_wait, wait_s=1) is None, cost
        told = []
        g = testslots.suite_hold("pa", repo=_LANE_REPO, cost={"cls": checklane.OTHER}, max_wait=0.3, wait_s=5,
                                 tell=told.append)
        assert g and g.files and "-x." in g.files[0].name and g.waited >= 0.3
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
        assert "position 1 of 1" in told[0] and "#12 running 'pytest -q tests/a.py' for 0 min" in told[0], told
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
        lines = checklane.long_wait_lines(p.name)
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
        ev = p.db.q("SELECT task, text FROM events WHERE kind='checks_slot_wait'")
        assert len(ev) == 1 and ev[0]["task"] == 13 and "#12" in ev[0]["text"], ev
    finally:
        locks.dequeue(ticket)
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
    waiter = threading.Thread(target=lambda: testslots.suite_hold(p.name, repo=key, priority=3, wait_s=3))
    waiter.start()

    def snap():
        d = checkcap.slot_dir()
        return {str(x): (x.stat().st_mtime_ns, x.stat().st_size, x.read_bytes() if x.is_file() else b"")
                for x in sorted(d.rglob("*")) if not x.name.startswith(".")}
    try:
        assert _wait_for(lambda: any(".queue/" in k and not k.rsplit("/", 1)[1].startswith(".") for k in snap()))
        before = snap()
        assert _ttp("checks", "status") == 0
        out = capsys.readouterr().out
        assert f"pid {holder.pid}, alive" in out and "#12" in out and "(this one)" in out, out
        assert "waiter 1 (next): #13" in out and "priority 3" in out, out
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
        _stop(holder)
        waiter.join(10)
