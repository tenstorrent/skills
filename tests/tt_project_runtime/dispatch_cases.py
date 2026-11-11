# Dispatch pick-order and worker-slot tests of tests/test_tt_project_runtime.py, which runs this file in
# its own namespace (see _include_cases there): helpers, fixtures and imports come from that file, and
# pytest collects these tests under its name. Add new tests of dispatch's pick order, its worker slots
# and Daemon.check_slot_wait here.
from __future__ import annotations


def _slot_setup(env, monkeypatch, tmp_path, slots):
    """A project with `slots` worker slots whose worker runs stay running until drain() (the
    stand-in agent waits for a file), and a daemon. Returns (p, d, drain, settle)."""
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    p.set_config("budget.max_parallel_workers", slots)
    gate = tmp_path / "release-runs"
    monkeypatch.setenv("TTP_FAKE_SHELL", f'while [ ! -e "{gate}" ]; do sleep 0.05; done')
    d = Daemon(p.base)
    d.cfg = p.config()
    d.update_gates()

    def drain(daemon):
        """Release every run and reap it with `daemon` until no worker run is running. Safe in a
        failed test's cleanup: it asserts nothing and its deadline ignores a patched time.time."""
        gate.touch()
        deadline = time.monotonic() + 30
        while _slot_tasks(p) and time.monotonic() < deadline:
            daemon.reap_runs()
            time.sleep(0.05)

    def settle(daemon):
        """Release every run and reap it with `daemon`, then hold new runs again."""
        drain(daemon)
        assert not _slot_tasks(p), "a stand-in run did not end"
        gate.unlink()
    return p, d, drain, settle


def _slot_tasks(p):
    return [r["task"] for r in p.db.q("SELECT task FROM runs WHERE role!='coordinator' AND status='running' "
                                      "ORDER BY id")]


def _owe(p, tid, due):
    from ttp import obligations
    return obligations.add(p, f"deliverable of #{tid}", owners=[tid],
                           milestones=[{"key": "m1", "due": due, "evidence_required": "a result"}])


def test_dispatch_starts_obligation_owners_then_urgent_first_and_never_past_max_parallel(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    try:
        now = time.time()
        old_p3 = p.db.add_task("older routine work", "s", kind="work", tier="light", origin="user", priority=3)
        plain_p1 = p.db.add_task("urgent work", "s", kind="work", tier="light", origin="user", priority=1)
        late_owner = p.db.add_task("owes a later deliverable", "s", kind="work", tier="light", origin="user",
                                   priority=3)
        early_owner = p.db.add_task("owes the next deliverable", "s", kind="work", tier="light", origin="user",
                                    priority=3)
        _owe(p, late_owner, now + 7200)
        _owe(p, early_owner, now + 3600)
        assert "## Obligation o" in p.db.task(early_owner)["spec"], "attached through the real record path"
        assert [t["id"] for t in p.db.ready_tasks()] == [early_owner, late_owner, plain_p1, old_p3]
        d.dispatch()
        assert _slot_tasks(p) == [early_owner, late_owner], "obligation owners by due, before priority 1 alone"
        d.dispatch()
        assert len(_slot_tasks(p)) == 2, "a second pass started past max_parallel"
        settle(d)
        d.dispatch()
        assert _slot_tasks(p) == [plain_p1, old_p3], "then priority 1 before the older priority 3"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_pick_order_after_a_daemon_restart_comes_from_the_task_records(env, monkeypatch, tmp_path,
                                                                       reap_tmp_processes):
    from ttp.daemon import Daemon
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    try:
        busy = p.db.add_task("holds the only slot", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        assert _slot_tasks(p) == [busy]
        first = p.db.add_task("first in line before the restart", "s", kind="work", tier="light", origin="user",
                              priority=2)   # not urgent: priority 1 would outrank an obligation owner
        later = p.db.add_task("queued later", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        assert _slot_tasks(p) == [busy] and [t["id"] for t in p.db.ready_tasks()] == [first, later]
        # While the daemon is down, the later task becomes an obligation's owner.
        _owe(p, later, time.time() + 3600)
        settle(d)
        restarted = Daemon(p.base)
        restarted.cfg = p.config()
        restarted.update_gates()
        restarted.dispatch()
        assert _slot_tasks(p) == [later], "the restarted daemon kept a stale pick order"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def _slot_wait_events(p):
    return p.db.q("SELECT * FROM events WHERE kind='observation' AND fingerprint LIKE 'slot_wait:%' ORDER BY id")


def test_slot_wait_fires_once_per_urgent_task_only_while_every_slot_is_taken(env, monkeypatch, tmp_path,
                                                                             reap_tmp_processes):
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    d.cfg["budget"]["urgent_extra_slots"] = 0   # the urgent task stays held by the one full slot
    try:
        holder = p.db.add_task("routine work holding the slot", "s", kind="work", tier="light", origin="user",
                               priority=3)
        d.dispatch()
        assert _slot_tasks(p) == [holder]
        owner = p.db.add_task("owes a deliverable", "s", kind="work", tier="light", origin="user", priority=2)
        oid = _owe(p, owner, time.time() + 3600)
        urgent = p.db.add_task("urgent work", "s", kind="work", tier="light", origin="user", priority=1)
        routine = p.db.add_task("more routine work", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        assert _slot_tasks(p) == [holder], "a full slot let a task start"
        start = time.time()

        def check(minutes):
            d._slot_wait_checked = start + minutes * 60 - 60
            monkeypatch.setattr(time, "time", lambda: start + minutes * 60)
            d.check_slot_wait()
            return _slot_wait_events(p)

        assert check(0) == [] and check(5) == [] and check(9) == []
        events = check(10)
        assert sorted(e["task"] for e in events) == [owner, urgent], "only obligation owners and priority 1"
        by_task = {e["task"]: e for e in events}
        assert by_task[owner]["severity"] == "high" and by_task[urgent]["severity"] == "normal"
        text = by_task[owner]["text"]
        assert f"#{owner} " in text and oid in text and "all 1 are taken" in text
        assert f"#{holder} routine work holding the slot (p3, run " in text, "the slot holders with priorities"
        assert routine not in [e["task"] for e in events]
        assert len(check(20)) == 2 and len(check(60)) == 2, "raised again for the same wait"
        monkeypatch.undo()
        # A free slot: dispatch would start them, so nothing is raised however long they stay queued.
        settle(d)
        for t in (owner, urgent):
            p.db.update_task(t, status="queued")
        p.db.x("DELETE FROM events WHERE fingerprint LIKE 'slot_wait:%'")
        start = time.time()
        assert all(check(m) == [] for m in (0, 5, 10, 15, 20, 30)), "raised while a slot was free"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def _gate_cut_wait(env, monkeypatch, tmp_path, level, allowed, cause, extra):
    """Two routine runs hold slots under a green gate with 4 configured; then the budget gate drops to
    `level` allowing `allowed` (cause `cause`) and a priority-1 task waits ten minutes with
    budget.urgent_extra_slots = `extra`. Returns (p, d, drain, the slot-wait event's text)."""
    import dataclasses
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=4)
    d.cfg["budget"]["urgent_extra_slots"] = extra
    core = d.cfg.get("core_provider", "claude")
    for i in range(2):
        p.db.add_task(f"routine work {i}", "s", kind="work", tier="light", origin="user", priority=3)
    d.dispatch()
    assert len(_slot_tasks(p)) == 2
    d.gates[core] = dataclasses.replace(d.gates[core], level=level, regime="caps", max_parallel=allowed,
                                        cause=cause, reasons=["billed by use since 01:00 PST", cause])
    hot = p.db.add_task("urgent fix", "s\nurgent: a user deadline", kind="work", tier="light", origin="user",
                        priority=1)
    d.dispatch()
    assert len(_slot_tasks(p)) == 2 and hot not in _slot_tasks(p), "the gate's cut let the urgent task start"
    start = time.time()
    d._slot_wait_checked = start + 9 * 60
    monkeypatch.setattr(time, "time", lambda: start + 10 * 60)
    d.check_slot_wait()
    monkeypatch.undo()
    (ev,) = _slot_wait_events(p)
    assert ev["task"] == hot
    return p, d, drain, ev["text"]


def test_slot_wait_names_the_budget_gates_halving_at_yellow_under_the_caps(env, monkeypatch, tmp_path,
                                                                           reap_tmp_processes):
    """Yellow under the dollar caps halves budget.max_parallel_workers (budget.evaluate): the slot wait
    names that cut and its cause, not only 'all 2 are taken', and `ttp status` and `ttp overview`
    show the same slots line from the saved gates."""
    from ttp import overview
    from ttp.cli import status_text
    p, d, drain, text = _gate_cut_wait(env, monkeypatch, tmp_path, "yellow", 2, "63% of cap used", 0)
    try:
        assert "all 2 are taken (4 configured; the budget gate allows 2 at yellow: 63% of cap used)." in text
        core = d.cfg.get("core_provider", "claude")
        p.db.set_kv("gates", {core: d.gates[core].as_dict()})
        want = (f"{core} worker slots: 2 running; 4 configured; the budget gate allows 2 at yellow: "
                "63% of cap used")
        assert want in status_text(p).splitlines()
        with sqlite3.connect(p.state / "project.db") as conn:
            assert overview._gate_slots(conn, 4) == [want]
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_slot_wait_names_the_budget_gates_one_slot_at_orange_and_the_urgent_extra(env, monkeypatch, tmp_path,
                                                                                  reap_tmp_processes):
    p, d, drain, text = _gate_cut_wait(env, monkeypatch, tmp_path, "orange", 1, "90% of cap used", 1)
    try:
        assert ("all 2 are taken (4 configured; the budget gate allows 1 at orange: 90% of cap used; "
                "+1 urgent).") in text
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_slot_wait_words_name_the_slot_held_back_for_urgent_work(env, monkeypatch, tmp_path):
    """Routine work leaves budget.urgent_reserve_slots to a ready urgent task: its slot count names that
    reserve; with no cut and no reserve nothing is added. An obligation owner keeps none (it ranks ahead)."""
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    core = d.cfg.get("core_provider", "claude")
    owner = p.db.task(p.db.add_task("routine work", "s", kind="work", tier="light", origin="user",
                                        priority=2))
    debtor = p.db.task(p.db.add_task("owes a deliverable", "s", kind="work", tier="light", origin="user",
                                     priority=2))
    _owe(p, debtor["id"], time.time() + 3600)
    ctx = {"now": time.time(), "paused": {}, "committed": None, "busy": {core: 1}, "logged_out": {}}
    assert d._start_hold(owner, core, ctx) is None, "a free slot with no urgent task due"
    p.db.add_task("urgent fix", "s", kind="work", tier="light", origin="user", priority=1)
    ctx = {"now": time.time(), "paused": {}, "committed": None, "busy": {core: 1}, "logged_out": {}}
    assert d._start_hold(owner, core, ctx) == "gate"
    assert ctx["full"][owner["id"]] == 1
    assert ctx["full_why"][owner["id"]] == "2 configured; 1 held back for urgent work"
    assert d._hold_words(owner, "gate", ctx) == f"every {core} slot taken (1; 2 configured; 1 held back for urgent work)"
    ctx = {"now": time.time(), "paused": {}, "committed": None, "busy": {core: 1}, "logged_out": {}}
    assert d._start_hold(p.db.task(debtor["id"]), core, ctx) is None, "an obligation owner keeps no reserve"


def test_budget_slot_words_name_the_gate_cut_only_when_it_lowers_the_configured_slots():
    from ttp import budget as bud
    g = bud.Gate(provider="claude", max_parallel=6)
    assert bud.gate_slots(g, 6) == "" and bud.slot_words(g, 6) == "" and bud.slot_lines({"claude": g}, 6) == []
    bud._raise(g, "yellow", "5h window at 80%, 10.0 points under the 90% line: room for 3 workers")
    bud._raise(g, "yellow", "a later yellow reason")
    g.max_parallel = 3
    assert bud.slot_words(g, 6, extra=1) == ("6 configured; the budget gate allows 3 at yellow: 5h window at 80%, "
                                             "10.0 points under the 90% line: room for 3 workers; +1 urgent")
    bud._raise(g, "red", "cap reached: $2000.00/today of $2000")
    g.allow_new_work = False
    assert bud.gate_slots(g.as_dict(), 6) == "the budget gate allows none at red: cap reached: $2000.00/today of $2000"
    old = {k: v for k, v in g.as_dict().items() if k != "cause"}   # saved by a version without `cause`
    assert bud.gate_slots(old, 6).startswith("the budget gate allows none at red: 5h window at 80%")
    assert bud.slot_lines({"claude": old}, 6, {"claude": 4})[0].startswith("claude worker slots: 4 running; 6 configured; ")


def test_slot_wait_is_off_at_zero(env, monkeypatch, tmp_path, reap_tmp_processes):
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    try:
        p.db.add_task("routine", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        p.db.add_task("urgent work", "s", kind="work", tier="light", origin="user", priority=1)
        d.cfg["coordinator"]["urgent_slot_wait_min"] = 0
        start = time.time()
        for minutes in (0, 5, 10, 15, 20):
            d._slot_wait_checked = start + minutes * 60 - 60
            monkeypatch.setattr(time, "time", lambda: start + minutes * 60)
            d.check_slot_wait()
        assert _slot_wait_events(p) == []
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_task_waiting_on_an_ended_detached_driver_holds_no_worker_slot(env, monkeypatch, tmp_path,
                                                                         reap_tmp_processes):
    """Only live model runs count against max_parallel: a task whose run handed off waiting on its
    detached driver, which then ended, is queued and frees its slot at once (no detach-record change
    needed)."""
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    try:
        rc = tmp_path / "drv.rc"
        rc.write_text("0\n")   # the driver ended
        monkeypatch.delenv("TTP_FAKE_SHELL")
        monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({"status": "waiting", "summary": "driver runs",
                                                          "retry_when": f"ttp detach --check {rc}",
                                                          "retry_after_s": 3600}))
        waiter = p.db.add_task("waits on its driver", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        assert _slot_tasks(p) == [waiter]
        deadline = time.time() + 30
        while _slot_tasks(p) and time.time() < deadline:
            d.reap_runs()
            time.sleep(0.05)
        t = p.db.task(waiter)
        assert t["status"] == "queued" and json.loads(t["result"])["status"] == "waiting"
        monkeypatch.delenv("TTP_FAKE_RESULT")
        monkeypatch.setenv("TTP_FAKE_SHELL", f'while [ ! -e "{tmp_path / "release-runs"}" ]; do sleep 0.05; done')
        nxt = p.db.add_task("next in line", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        assert _slot_tasks(p) == [nxt], "the waiting task still held the only slot"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def _board_setup(env, monkeypatch, tmp_path, board):
    """_slot_setup with three worker slots and `board` slots of resource board. Each stand-in run hands
    off $G/result-<task>.json when the test wrote one, else runs until $G/go-<task> or drain(). Returns
    (p, d, drain, go): go(tid) ends that task's run and reaps it."""
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=3)
    g = shlex.quote(str(tmp_path))
    monkeypatch.setenv("TTP_FAKE_SHELL", f'G={g}; if [ -e "$G/result-$TTP_TASK.json" ]; then '
                                         f'cp "$G/result-$TTP_TASK.json" "$TTP_RUN_DIR/result.json"; exit 0; fi; '
                                         f'while [ ! -e "$G/go-$TTP_TASK" ] && [ ! -e "$G/release-runs" ]; '
                                         f'do sleep 0.05; done')
    _board_slots(p, d, board)

    def go(tid):
        (tmp_path / f"go-{tid}").touch()
        assert _until_true(lambda: d.reap_runs() or tid not in _slot_tasks(p)), f"the run of #{tid} did not end"
    return p, d, drain, go


def _board_slots(p, d, n, **more):
    p.set_config("resources", {"board": n, **more})
    d.cfg = p.config()


def _board_add(p, title, priority, resources=("board",)):
    from ttp import coordinator as coord
    assert coord.apply(p, [{"type": "task_add", "title": title, "spec": "s", "kind": "work", "tier": "light",
                            "resources": list(resources), "exclusive": True, "priority": priority}]) == []
    return p.db.one("SELECT id FROM tasks WHERE title=?", (title,))["id"]


def _board_waiting(p, d, tmp_path, tid, probe="ttp lock --probe board", **more):
    """Start task tid (the test leaves a slot of each of its resources free), let its run hand off
    waiting on `probe` (and the hand-off fields `more`) and reap it; the next run of the task stays
    running until go(tid)."""
    res = tmp_path / f"result-{tid}.json"
    res.write_text(json.dumps({"status": "waiting", "summary": "board busy", "retry_when": probe,
                               "retry_after_s": 3600, **more}))
    d.dispatch()
    assert _until_true(lambda: d.reap_runs() or p.db.task(tid)["status"] == "queued" and tid not in _slot_tasks(p))
    res.unlink()
    t = p.db.task(tid)
    # a priority-1 task's hour-long timer is capped at waiting.urgent_retry_cap_s: still far off for the test
    assert json.loads(t["result"])["status"] == "waiting" and t["not_before"] > time.time() + 240, t


def test_a_freed_exclusive_slot_goes_to_the_higher_priority_task_waiting_for_it(env, monkeypatch, tmp_path,
                                                                                reap_tmp_processes):
    """The reported case: a priority-3 run holds board, a priority-1 task waits on `ttp lock --probe board`
    (retry timer an hour off) and a newer priority-3 task is ready. Once board frees, dispatch starts the
    waiting priority-1 task, not the ready one."""
    p, d, drain, go = _board_setup(env, monkeypatch, tmp_path, board=2)
    try:
        a = _board_add(p, "A holds board", 3)
        p1 = _board_add(p, "P1 urgent", 1)
        _board_waiting(p, d, tmp_path, p1)
        assert _slot_tasks(p) == [a]
        _board_slots(p, d, 1)
        c = _board_add(p, "C routine", 3)
        d.dispatch()
        assert _slot_tasks(p) == [a], "a task started on a full board"
        go(a)
        d.dispatch()
        assert _slot_tasks(p) == [p1], "the freed slot went to the lower-priority ready task"
        assert p.db.task(c)["status"] == "queued"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_freed_exclusive_slot_goes_to_the_higher_priority_of_two_ready_tasks(env, monkeypatch, tmp_path,
                                                                               reap_tmp_processes):
    p, d, drain, go = _board_setup(env, monkeypatch, tmp_path, board=1)
    try:
        a = _board_add(p, "A holds board", 3)
        d.dispatch()
        assert _slot_tasks(p) == [a]
        c = _board_add(p, "C routine, older", 3)
        p1 = _board_add(p, "P1 urgent", 1)
        d.dispatch()
        assert _slot_tasks(p) == [a]
        go(a)
        d.dispatch()
        assert _slot_tasks(p) == [p1] and p.db.task(c)["status"] == "queued"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_waiting_higher_priority_task_reserves_the_slot_against_ttp_lock_and_lower_tasks(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """board has two slots: run A holds one, a test `ttp lock` the other, P1 waits. Once the lock ends,
    the free slot is reserved for P1: another `ttp lock` waits (75, naming P1) and dispatch starts P1,
    not the lower-priority ready C."""
    from ttp import locks
    p, d, drain, go = _board_setup(env, monkeypatch, tmp_path, board=2)
    flag, marks = tmp_path / "unlock", tmp_path / "marks"
    holder = None
    try:
        a = _board_add(p, "A holds board", 3)
        p1 = _board_add(p, "P1 urgent", 1)
        _board_waiting(p, d, tmp_path, p1)
        holder = _lock(_lock_env(env, p, TTP_TASK="900"), "board", cmd=_mark(marks, "H", flag))
        assert _until_true(lambda: marks.exists() and "start H" in marks.read_text())
        c = _board_add(p, "C routine", 3)
        d.dispatch()
        assert _slot_tasks(p) == [a]
        assert locks.reserved_by(d._reserve_path("board")) == f"task #{p1}"
        flag.touch()
        assert _ended([holder]) == [0]
        r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "2", "board", "--", "true"],
                           env=_lock_env(env, p, TTP_TASK="901"), capture_output=True, text=True, timeout=60)
        assert r.returncode == 75 and f"reserved for task #{p1}" in r.stderr, (r.returncode, r.stderr)
        d.dispatch()
        assert _slot_tasks(p) == [a, p1] and p.db.task(c)["status"] == "queued"
    finally:
        flag.touch()
        if holder:
            _ended([holder])
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_reservation_that_keeps_a_free_slot_unused_lapses_after_exclusive_reserve_s(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """P1 needs board and cable; a test `ttp lock` holds cable. P1 reserves the free board, so the lower C
    does not start on it, until the reservation lapses after budget.exclusive_reserve_s (1 s here)."""
    from ttp import locks
    p, d, drain, go = _board_setup(env, monkeypatch, tmp_path, board=1)
    flag, marks = tmp_path / "unlock", tmp_path / "marks"
    holder = None
    clock = [time.time()]   # the reservation pass's clock: a slow host never lapses the reservation early
    real_pass = d._slot_reservations
    monkeypatch.setattr(d, "_slot_reservations", lambda now: real_pass(clock[0]))
    try:
        p.set_config("budget.exclusive_reserve_s", 1)
        _board_slots(p, d, 1, cable=1)
        p1 = _board_add(p, "P1 urgent", 1, resources=("board", "cable"))
        _board_waiting(p, d, tmp_path, p1, probe="ttp lock --probe board,cable")
        holder = _lock(_lock_env(env, p, TTP_TASK="900"), "cable", cmd=_mark(marks, "H", flag))
        assert _until_true(lambda: marks.exists() and "start H" in marks.read_text())
        c = _board_add(p, "C routine", 3)
        d.dispatch()
        d.dispatch()
        assert _slot_tasks(p) == [], "C started on a board reserved for P1"
        assert locks.reserved_by(d._reserve_path("board")) == f"task #{p1}"
        clock[0] += 1.2
        d.dispatch()
        assert _slot_tasks(p) == [c], "the reservation never lapsed"
        assert locks.reserved_by(d._reserve_path("board")) != f"task #{p1}"
        assert p.db.task(p1)["status"] == "queued"
    finally:
        flag.touch()
        if holder:
            _ended([holder])
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_equal_priority_reserves_nothing_and_the_older_waiting_task_takes_the_freed_slot(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    from ttp import locks
    p, d, drain, go = _board_setup(env, monkeypatch, tmp_path, board=2)
    try:
        a = _board_add(p, "A holds board", 3)
        w = _board_add(p, "W waits, older", 3)
        _board_waiting(p, d, tmp_path, w)
        _board_slots(p, d, 1)
        c = _board_add(p, "C ready, newer", 3)
        d.dispatch()
        assert _slot_tasks(p) == [a]
        assert locks.reserved_by(d._reserve_path("board")) is None, "an equal rank took a reservation"
        go(a)
        d.dispatch()
        assert _slot_tasks(p) == [w] and p.db.task(c)["status"] == "queued"
        assert locks.reserved_by(d._reserve_path("board")) is None
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_paused_exclusive_resource_is_reserved_for_no_one_and_nothing_starts_on_it(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    from ttp import coordinator as coord, locks
    p, d, drain, go = _board_setup(env, monkeypatch, tmp_path, board=1)
    try:
        p1 = _board_add(p, "P1 urgent", 1)
        _board_waiting(p, d, tmp_path, p1)
        before = p.db.task(p1)["not_before"]
        coord.pause_resource(p, "board", True, reason="maintenance", by="user")
        _board_add(p, "C routine", 3)
        d.dispatch()
        assert _slot_tasks(p) == []
        assert locks.reserved_by(d._reserve_path("board")) is None, "a paused resource was reserved"
        assert p.db.task(p1)["not_before"] == before, "a task on a paused resource was woken"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_reservation_keeps_one_slot_so_lower_tasks_take_the_spare_slots_and_its_start_serves_it(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """board has two slots and P1 (board and cable) reserves it while cable is held: the lower C takes the
    spare slot, the lower D not the one kept for P1. Once P1 starts, its reservation is served."""
    from ttp import locks
    p, d, drain, go = _board_setup(env, monkeypatch, tmp_path, board=2)
    flag, marks = tmp_path / "unlock", tmp_path / "marks"
    holder = None
    try:
        _board_slots(p, d, 2, cable=1)
        p1 = _board_add(p, "P1 urgent", 1, resources=("board", "cable"))
        _board_waiting(p, d, tmp_path, p1, probe="ttp lock --probe board,cable")
        holder = _lock(_lock_env(env, p, TTP_TASK="900"), "cable", cmd=_mark(marks, "H", flag))
        assert _until_true(lambda: marks.exists() and "start H" in marks.read_text())
        c = _board_add(p, "C routine", 3)
        dd = _board_add(p, "D routine", 3)
        d.dispatch()
        assert _slot_tasks(p) == [c], "the spare slot stayed idle, or the kept one was taken"
        assert locks.reserved_by(d._reserve_path("board")) == f"task #{p1}"
        go(c)
        flag.touch()
        assert _ended([holder]) == [0]
        d.dispatch()
        assert _slot_tasks(p) == [p1, dd], "P1's start did not serve its reservation"
    finally:
        flag.touch()
        if holder:
            _ended([holder])
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_command_user_still_on_its_retry_timer_does_not_stop_a_slot_reservation(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """P1 (exclusive) waits for board; N, as urgent, uses board per command but is deferred hours off.
    N is not due, so it takes no part in the rank: P1 reserves the slot. Once N is due it ties P1 again
    and nothing is reserved."""
    from ttp import coordinator as coord, locks
    p, d, drain, go = _board_setup(env, monkeypatch, tmp_path, board=2)
    try:
        a = _board_add(p, "A holds board", 3)
        p1 = _board_add(p, "P1 urgent", 1)
        _board_waiting(p, d, tmp_path, p1)
        assert _slot_tasks(p) == [a]
        _board_slots(p, d, 1)
        assert coord.apply(p, [{"type": "task_add", "title": "N uses board later", "spec": "s", "kind": "work",
                                "tier": "light", "resources": ["board"], "priority": 1, "start_after": "6h",
                                "why": "the board's maintenance window"}]) == []
        n = p.db.one("SELECT id FROM tasks WHERE title=?", ("N uses board later",))["id"]
        assert p.db.task(n)["not_before"] > time.time() + 3600
        d.dispatch()
        assert _slot_tasks(p) == [a]
        assert list(d._slot_reservers(time.time()).values()) == [p1], "a deferred command user outranked P1"
        assert locks.reserved_by(d._reserve_path("board")) == f"task #{p1}"
        assert coord.apply(p, [{"type": "task_update", "id": n, "start_after": "now"}]) == []
        assert (p.db.task(n)["not_before"] or 0) <= time.time()
        assert d._slot_reservers(time.time()) == {}, "a due command user of equal rank no longer counted"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


@pytest.mark.parametrize("on_pass_cmd", [True, False])
def test_a_slot_wake_runs_the_hand_offs_on_pass_cmd_model_free(env, monkeypatch, tmp_path, reap_tmp_processes,
                                                               on_pass_cmd):
    """P1 waits only on `ttp lock --probe board`, its timer an hour off. Once board frees, its slot wake
    runs the hand-off's on_pass_cmd model-free and records its result, with no model run. A hand-off
    without one is woken and started as before."""
    p, d, drain, go = _board_setup(env, monkeypatch, tmp_path, board=2)
    try:
        a = _board_add(p, "A holds board", 3)
        p1 = _board_add(p, "P1 urgent", 1)
        cmd = """printf '{"status": "done", "summary": "board came free; published"}' > "$TTP_RESULT\""""
        _board_waiting(p, d, tmp_path, p1, **({"on_pass_cmd": cmd} if on_pass_cmd else {}))
        _board_slots(p, d, 1)
        runs = p.db.one("SELECT COUNT(*) AS n FROM runs WHERE task=?", (p1,))["n"]
        go(a)
        d.dispatch()
        if not on_pass_cmd:
            assert _slot_tasks(p) == [p1], "the hand-off was not woken and started"
            assert "board came free" in json.loads(p.db.task(p1)["result"])["woke"]
            return
        assert _slot_tasks(p) == [], "the slot wake started a model run"
        assert p1 in d._on_pass, "its on_pass_cmd did not start"
        assert p.db.task(p1)["not_before"] > time.time() + 60, "left due for a model run meanwhile"
        assert _until_true(lambda: d._poll_on_pass(time.time()) or p.db.task(p1)["status"] == "done"), \
            p.db.task(p1)
        assert json.loads(p.db.task(p1)["result"])["summary"].startswith("board came free; published")
        assert p.db.one("SELECT COUNT(*) AS n FROM runs WHERE task=?", (p1,))["n"] == runs, "a model run started"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def _claude_tools(argv):
    return argv[argv.index("--tools") + 1].split(",") if "--tools" in argv else None


def _dispatch_one(p, monkeypatch, flags, **kw):
    """_dispatch_claude_worker, the earlier stand-in runs ended first so the next task starts."""
    p.db.x("UPDATE runs SET status='done' WHERE status='running'")
    p.db.x("UPDATE tasks SET status='done' WHERE status='running'")
    return _dispatch_claude_worker(p, monkeypatch, flags, **kw)


def test_claude_workers_and_reviewers_get_the_full_tool_set(env, monkeypatch, tmp_path):
    """Workers and reviewers on Claude run on the full Claude Code harness (user msg #2302): no --tools
    allowlist, Agent included; ttp.hook holds the rules. Only the web tools under no_internet and the
    project's own denylist are withheld."""
    from ttp.providers import claude
    p = make(env)
    for kind in ("code", "review", "question", "plan"):
        argv, _, _ = _dispatch_one(p, monkeypatch, {}, kind=kind)
        assert _claude_tools(argv) is None and "--disallowedTools" not in argv, kind
        hooks = json.loads(argv[argv.index("--settings") + 1])["hooks"]["PreToolUse"]
        assert [h["matcher"] for h in hooks] == ["*"], "every tool, subagents' too, meets the hook"
    # The no_internet restriction withholds the web tools only.
    p.set_config("restrictions.no_internet", True)
    argv, _, _ = _dispatch_one(p, monkeypatch, {}, kind="review")
    assert argv[argv.index("--disallowedTools") + 1:argv.index("--settings")] == list(claude.WEB_TOOLS)
    p.set_config("restrictions.no_internet", False)
    # The project's own denylist stays; its MCP servers come through --mcp-config.
    _claude_mcp_config(tmp_path, monkeypatch, p)
    p.set_config("providers.claude.worker_isolation", True)
    p.set_config("providers.claude.mcp_servers", ["docs"])
    p.set_config("providers.claude.disallowed_tools", ["mcp__docs__write_page"])
    argv, _, _ = _dispatch_one(p, monkeypatch, {}, kind="work")
    assert _claude_tools(argv) is None
    assert "--strict-mcp-config" in argv and "--mcp-config" in argv
    assert argv[argv.index("--disallowedTools") + 1:argv.index("--settings")] == ["mcp__docs__write_page"]
    pathlib.Path(argv[argv.index("--mcp-config") + 1]).unlink()


def test_the_tool_set_change_leaves_coordinator_turns_codex_and_cursor_alone(env, monkeypatch):
    from ttp.providers import claude, get_provider
    monkeypatch.setattr(claude, "_FLAGS", {claude.TOOLS: True})
    kw = dict(model="opus", effort="low", cwd=".", budget_usd=1.0, schema=None, restrictions={})
    turn, _ = get_provider("claude").build(role="coordinator", read_only=True, web=False, **kw)
    assert "--tools" not in turn and turn[turn.index("--disallowedTools") + 1:] == ["Edit", "Write", "NotebookEdit"]
    for name in ("codex", "cursor"):
        for role, read_only in (("worker", False), ("reviewer", False), ("coordinator", True)):
            with_web = get_provider(name).build(role=role, read_only=read_only, web=True, **kw)
            assert get_provider(name).build(role=role, read_only=read_only, web=False, **kw) == with_web, name


def _stub_runner(monkeypatch):
    """Dispatch starts runs without launching their runner."""
    from ttp import daemon as dm

    class Proc:
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dm.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))


def _run_cwd(p, tid):
    run = p.db.one("SELECT dir FROM runs WHERE task=? ORDER BY id DESC", (tid,))
    return json.loads((pathlib.Path(run["dir"]) / "run.json").read_text())["cwd"] if run else None


def _end(p, tid):
    p.db.x("UPDATE runs SET status='done', ended=? WHERE task=? AND status='running'", (time.time(), tid))
    p.db.update_task(tid, status="done")


def test_a_second_task_on_a_live_task_s_branch_is_held_until_it_ends_then_gets_its_own_worktree(env, monkeypatch):
    # Two code tasks carry one branch: the later one never starts beside the running one. It waits with
    # a note naming the holder and the branch, starts once the holder ended, in a worktree of its own
    # from that branch's current head (the holder's commit included); the holder's worktree is untouched.
    from ttp import daemon as dm
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    p.set_config("core_provider", "claude")
    _git_out(p.root, "branch", "feat/shared")
    first = p.db.add_task("first on it", "s", kind="code", tier="light", origin="user", labels=["pr_branch:feat/shared"])
    second = p.db.add_task("second on it", "s", kind="code", tier="light", origin="user",
                           labels=["pr_branch:feat/shared"])
    _stub_runner(monkeypatch)
    dm.Daemon(p.base).dispatch()
    assert _run_cwd(p, first) == str(p.worktrees / f"t{first}"), p.db.task(first)["blocked_reason"]
    held = p.db.task(second)
    assert held["status"] == "queued" and _run_cwd(p, second) is None
    assert held["blocked_reason"].startswith(dm.BRANCH_HELD_NOTE) and f"#{first}" in held["blocked_reason"]
    assert "branch feat/shared" in held["blocked_reason"], held["blocked_reason"]
    # The holder commits on the branch; the next tick still holds the second task.
    wt = p.worktrees / f"t{first}"
    (wt / "a.txt").write_text("work")
    _git_out(wt, "add", "a.txt")
    _git_out(wt, "commit", "-qm", "work of the first")
    tip = _git_out(wt, "rev-parse", "HEAD")
    dm.Daemon(p.base).dispatch()
    assert _run_cwd(p, second) is None
    _end(p, first)
    dm.Daemon(p.base).dispatch()
    cwd = _run_cwd(p, second)
    assert cwd == str(p.worktrees / f"t{second}") and cwd != str(wt), cwd
    assert _git_out(cwd, "rev-parse", "HEAD") == tip, "it did not start from the branch's current head"
    assert p.db.task(second)["status"] == "running" and not p.db.task(second)["blocked_reason"]
    assert (wt / "a.txt").read_text() == "work"


def test_code_tasks_on_different_branches_still_start_side_by_side(env, monkeypatch):
    from ttp import daemon as dm
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    p.set_config("core_provider", "claude")
    _git_out(p.root, "branch", "feat/one")
    _git_out(p.root, "branch", "feat/two")
    one = p.db.add_task("on one", "s", kind="code", tier="light", origin="user", labels=["pr_branch:feat/one"])
    two = p.db.add_task("on two", "s", kind="code", tier="light", origin="user", labels=["pr_branch:feat/two"])
    plain = p.db.add_task("plain", "s", kind="code", tier="light", origin="user")
    _stub_runner(monkeypatch)
    dm.Daemon(p.base).dispatch()
    assert [p.db.task(t)["status"] for t in (one, two, plain)] == ["running"] * 3, \
        [p.db.task(t)["blocked_reason"] for t in (one, two, plain)]
    assert len({_run_cwd(p, t) for t in (one, two, plain)}) == 3


def test_a_task_continuing_a_running_task_s_lineage_is_held_by_worktree_path_too(env):
    # A project root that is no git repo records no branch: the continued task's worktree path still
    # ties two continuations of it, and a review never holds or is held.
    from ttp import daemon as dm
    p = make(env)
    old = p.db.add_task("failed work", "s", kind="code", tier="light", origin="user")
    p.db.update_task(old, status="failed", attempts=1)
    a = p.db.add_task("redo a", "s", kind="code", tier="light", origin="user", labels=[f"continues:{old}"])
    b = p.db.add_task("redo b", "s", kind="code", tier="light", origin="user", labels=[f"continues:{old}"])
    review = p.db.add_task("review it", "s", kind="review", tier="light", origin="user", labels=[f"continues:{old}"])
    p.db.update_task(a, status="running")
    d = dm.Daemon(p.base)
    live = d._live_claims()
    assert d._branch_clash(p.db.task(b), live) == (a, f"worktree {p.worktrees / f't{old}'}")
    assert d._branch_clash(p.db.task(review), live) is None
    # A task cancelled before it ran has no worktree or branch: two redos of it do not tie.
    never = p.db.add_task("never ran", "s", kind="code", tier="light", origin="user")
    p.db.update_task(never, status="cancelled")
    c = p.db.add_task("redo c", "s", kind="code", tier="light", origin="user", labels=[f"continues:{never}"])
    e = p.db.add_task("redo e", "s", kind="code", tier="light", origin="user", labels=[f"continues:{never}"])
    p.db.update_task(c, status="running")
    assert d._branch_clash(p.db.task(e), d._live_claims()) is None


def test_code_prompt_tells_a_task_in_a_root_that_is_no_git_repo_to_make_its_own_worktree(env):
    from ttp.prompts import _read, worker_task
    p = make(env)
    text = _read(p, "kind-code.md")
    assert "You are in a dedicated git worktree" not in text
    assert "(none in your header: make one as it says)" in text
    old = p.db.add_task("failed work", "s", kind="code", tier="light", origin="user")
    p.db.update_task(old, status="failed")
    tid = p.db.add_task("redo", "s", kind="code", tier="light", origin="user", labels=[f"continues:{old}"])
    prompt = worker_task(p, p.db.task(tid), str(p.root), None)
    assert f"make your own, t{tid} on branch ttp/t{tid}-<topic>" in prompt
    assert f"from #{old}'s branch; never edit in its worktree" in prompt
    assert "No worktree was made" not in worker_task(p, p.db.task(tid), str(p.worktrees / f"t{tid}"), "ttp/x")


def _sched_lines(p):
    from ttp import schedlog
    f = schedlog.path(p)
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def _sched_held(p, title, **cols):
    tid = p.db.add_task(title, "s", kind="work", tier="light", origin="user", priority=2)
    if cols:
        p.db.x(f"UPDATE tasks SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), tid))
    return tid


def test_a_slot_fill_logs_runnable_tasks_in_rank_order_held_counts_by_reason_and_the_pick(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """One free slot; runnable p1 and p3, one task held by start_when, one by a `ttp lock` on its resource
    (ranked between the two, so it is reached only after the slot filled): one line, the p1 task picked."""
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    _board_slots(p, d, 1)
    flag, marks = tmp_path / "unlock", tmp_path / "marks"
    holder = None
    try:
        holder = _lock(_lock_env(env, p, TTP_TASK="900"), "board", cmd=_mark(marks, "H", flag))
        assert _until_true(lambda: marks.exists() and "start H" in marks.read_text())
        p3 = p.db.add_task("routine", "s", kind="work", tier="light", origin="user", priority=3)
        p1 = p.db.add_task("urgent", "s", kind="work", tier="light", origin="user", priority=1)
        _sched_held(p, "deferred", labels=json.dumps(["start_when:false"]))
        locked = _board_add(p, "needs the board", 2)
        d.dispatch()
        assert _slot_tasks(p) == [p1]
        lines = _sched_lines(p)
        assert len(lines) == 1, lines
        line = lines[0]
        assert line["free_slots"] == 1 and line["picked"] == [p1] and line["ts"].endswith("Z")
        assert line["runnable"] == [{"id": p1, "priority": 1, "owns_obligation": False},
                                    {"id": p3, "priority": 3, "owns_obligation": False}], (line, locked)
        assert line["runnable_total"] == 2
        assert {k: v for k, v in line["held"].items() if v} == {"start_when": 1, "lock": 1}, line
    finally:
        flag.touch()
        if holder:
            _ended([holder])
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_slot_fill_flags_an_obligation_owner_and_lists_it_where_dispatch_ranks_it(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    try:
        plain = p.db.add_task("urgent", "s", kind="work", tier="light", origin="user", priority=1)
        owner = p.db.add_task("owes a deliverable", "s", kind="work", tier="light", origin="user", priority=3)
        _owe(p, owner, time.time() + 3600)
        d.dispatch()
        assert _slot_tasks(p) == [owner], "an obligation owner ranks before priority 1 alone"
        line, = _sched_lines(p)
        assert line["runnable"] == [{"id": owner, "priority": 3, "owns_obligation": True},
                                    {"id": plain, "priority": 1, "owns_obligation": False}], line
        assert line["picked"] == [owner]
        assert line["slots"] == {"running": 1, "by_kind": {"work": 1}, "unreaped": 0, "untracked": 0}, line
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_idle_slot_fills_with_nothing_changed_add_no_scheduler_line_even_after_a_restart(env, monkeypatch, tmp_path):
    from ttp.daemon import Daemon
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    _sched_held(p, "deferred", labels=json.dumps(["start_when:false"]))
    _sched_held(p, "later", not_before=time.time() + 3600)
    for _ in range(3):
        d.dispatch()
    assert len(_sched_lines(p)) == 1
    d2 = Daemon(p.base)   # a restart reads the last line back
    d2.cfg = d.cfg
    d2.update_gates()
    d2.dispatch()
    assert len(_sched_lines(p)) == 1
    _sched_held(p, "after it", depends_on=json.dumps([99999]))
    d2.dispatch()
    lines = _sched_lines(p)
    assert len(lines) == 2 and lines[0]["picked"] == lines[1]["picked"] == [] and lines[1]["runnable"] == []
    assert {k: v for k, v in lines[1]["held"].items() if v} == {"start_when": 1, "start_after": 1, "depends_on": 1}


def test_a_slot_fill_files_a_retry_when_wait_under_retry_when_not_start_after(env, monkeypatch, tmp_path):
    """The held counts tell a task's own retry_when timer from a planned start: a waiting hand-off's
    not_before is retry_when; a start_when task whose not_before is only its probe's backoff (its
    start_after over) is start_when; a start_after still ahead is start_after, start_when or not, and
    so is a bare start timer that is no retry_when wait."""
    from ttp.db import dump_result
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    start = time.time()
    monkeypatch.setattr(time, "time", lambda: start + 600)
    wait = dump_result({"status": "waiting", "retry_when": "false", "waiting_since": start})
    try:
        _sched_held(p, "own wait", not_before=start + 3600, result=wait)
        _sched_held(p, "own wait, timer up", not_before=start + 900, result=wait)
        _sched_held(p, "probe backoff", not_before=start + 900,
                    labels=json.dumps(["start_when:false", f"start_after:{start:.0f}"]))
        _sched_held(p, "planned", not_before=start + 7200, labels=json.dumps([f"start_after:{start + 7200:.0f}"]))
        _sched_held(p, "planned, then probe", not_before=start + 7200,
                    labels=json.dumps(["start_when:false", f"start_after:{start + 7200:.0f}"]))
        _sched_held(p, "start timer", not_before=start + 3600)
        d.dispatch()
        line, = _sched_lines(p)
        assert line["picked"] == [] and line["runnable"] == []
        assert {k: v for k, v in line["held"].items() if v} == {"retry_when": 2, "start_when": 1, "start_after": 3}, line
    finally:
        drain(d)


def test_the_scheduler_log_rotates_under_its_cap_and_keeps_one_old_copy(tmp_path, monkeypatch):
    from ttp import schedlog
    monkeypatch.setattr(schedlog, "MAX_BYTES", 2000)
    f, cache, warned = tmp_path / "state" / schedlog.FILE, {}, []
    for i in range(200):
        assert schedlog.record(f, schedlog.entry(1, [{"id": i, "priority": 3, "owns_obligation": False}], {}, []),
                               cache, warned.append)
        assert f.stat().st_size <= 2000
    old = f.with_name(schedlog.OLD)
    assert old.exists() and old.stat().st_size <= 2000 and not warned
    assert sorted(x.name for x in f.parent.iterdir()) == [schedlog.OLD, schedlog.FILE]
    assert json.loads(f.read_text().splitlines()[-1])["runnable"][0]["id"] == 199


def test_an_unwritable_scheduler_log_never_stops_dispatch_and_is_reported_once(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    from ttp import schedlog
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    monkeypatch.setattr(schedlog, "path", lambda _p: blocker / schedlog.FILE)   # its parent is a file
    try:
        a = p.db.add_task("first", "s", kind="work", tier="light", origin="user", priority=1)
        d.dispatch()
        assert _slot_tasks(p) == [a]
        b = p.db.add_task("second", "s", kind="work", tier="light", origin="user", priority=1)
        d.dispatch()
        assert _slot_tasks(p) == [a, b]
        text = (p.logs / "daemon.log").read_text()
        assert text.count("scheduler log: write to") == 1 and "scheduler log failed" not in text
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_config_section_saved_as_a_string_still_lets_runs_start(env, monkeypatch, tmp_path, reap_tmp_processes):
    """`ttp config checks host_max` once saved project.json's "checks" as the string "host_max": every
    run start then failed with "AttributeError: 'str' object has no attribute 'get'". A section that is
    not an object reads as absent, with one warning naming the key, and the run starts."""
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    try:
        raw = json.loads(p.config_path.read_text())
        raw["checks"] = "host_max"
        p.config_path.write_text(json.dumps(raw))
        d._load_config()
        d._load_config()   # warned once per distinct set of bad sections, not on every reload
        tid = p.db.add_task("work", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        assert _slot_tasks(p) == [tid], p.db.task(tid)["blocked_reason"]
        warned = [ln for ln in (p.logs / "daemon.log").read_text().splitlines() if 'checks = "host_max"' in ln]
        assert len(warned) == 1 and "read as absent" in warned[0], warned
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_failed_start_names_its_line_logs_the_traceback_and_never_stops_the_next_task(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """One task's start failing leaves it queued with a note naming the runtime file and line the error
    came from, logs the full traceback with the task id, and the next task in line still starts. The
    run-start alert stays up while the failed task is queued for its retry, though another run started;
    deferred past that retry, it clears."""
    from ttp import alerts, prompts
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    try:
        bad = p.db.add_task("breaks at start", "s", kind="work", tier="light", origin="user", priority=1)
        good = p.db.add_task("starts fine", "s", kind="work", tier="light", origin="user", priority=3)
        real = prompts.worker_system

        def system(proj, task):
            if task["id"] == bad:
                return {"checks": "host_max"}["checks"].get("pool")   # the AttributeError of a str section
            return real(proj, task)
        monkeypatch.setattr(prompts, "worker_system", system)
        d._start_failures = 2   # the third failure in a row raises the alert
        d.dispatch()
        assert _slot_tasks(p) == [good], "the failed start kept the next task from starting"
        why = p.db.task(bad)["blocked_reason"]
        assert p.db.task(bad)["status"] == "queued"
        assert re.fullmatch(r"could not start a run: AttributeError: 'str' object has no attribute 'get' "
                            r"at daemon\.py:\d+", why), why   # the last runtime frame; the stand-in is no runtime file
        log_text = (p.logs / "daemon.log").read_text()
        assert f"task {bad} could not start a run" in log_text and "Traceback (most recent call last)" in log_text
        assert "'str' object has no attribute 'get'" in log_text.split("Traceback", 1)[1]
        now = time.time()
        assert alerts.holds(p.db, "run-start", now - 5, now), "the failed task is still queued: the alert must hold"
        p.db.update_task(bad, not_before=now + 7200)   # deferred by the coordinator
        assert not alerts.holds(p.db, "run-start", now - 5, now)
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_one_cause_failing_three_tasks_starts_opens_the_breaker_holds_dispatch_and_a_good_retry_resumes_it(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """The same error at the same line failing the starts of three tasks within 10 minutes opens the
    run-start breaker: one alert naming that line, no further starts, no attempts spent, and the
    coordinator's repeated-failures trigger leaves the never-started runs to that alert. One start
    retries every few minutes; the first that succeeds closes it and the queue starts."""
    from ttp import alerts, startbreaker, testslots
    from ttp import coordinator as coord
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=5)
    try:
        ids = [p.db.add_task(f"t{i}", "s", kind="work", tier="light", origin="user", priority=3) for i in range(4)]
        real = testslots.worker_env
        monkeypatch.setattr(testslots, "worker_env", lambda cfg: "host_max".get("pool"))   # the str-section crash
        p.db.set_kv("last_coordinator_turn", time.time() - 3600)
        d.dispatch()
        st = startbreaker.state(p.db)
        assert st.get("open") and re.fullmatch(r"AttributeError at daemon\.py:\d+", st["sig"]), st
        assert st["tasks"] == ids[:3]
        assert _slot_tasks(p) == []
        held = p.db.task(ids[3])
        assert held["status"] == "queued" and held["blocked_reason"].startswith(startbreaker.HELD_NOTE), held
        failed = p.db.q("SELECT task, note FROM runs WHERE role!='coordinator' ORDER BY id")
        assert [r["task"] for r in failed] == ids[:3] and all(json.loads(r["note"])["never_started"] for r in failed)
        sent = [m["text"] for m in p.db.q("SELECT text FROM messages WHERE direction='out' AND kind='alert'")
                if "Runs cannot start" in (m["text"] or "")]
        assert len(sent) == 1 and st["sig"].split(" at ")[1] in sent[0], sent
        now = time.time()
        assert alerts.holds(p.db, "run-start", now - 5, now)
        p.db.x("UPDATE tasks SET status='cancelled' WHERE id IN (%s)" % ",".join(map(str, ids)))
        assert not alerts.holds(p.db, "run-start", now - 5, now), "nothing queued: the alert has nothing to hold"
        p.db.x("UPDATE tasks SET status='queued' WHERE id IN (%s)" % ",".join(map(str, ids)))
        # Every task due again: all held, nothing started, and no retry before RETRY_S.
        p.db.x("UPDATE tasks SET not_before=NULL")
        d.dispatch()
        assert p.db.one("SELECT COUNT(*) n FROM runs WHERE role!='coordinator'")["n"] == 3
        # The retry: one start only, on the task the cause has not failed yet, which fails at the same
        # line; the breaker stays open. The next goes to the failed task tried least recently.
        for _ in range(2):
            p.db.x("UPDATE tasks SET not_before=NULL")
            p.db.set_kv(startbreaker.KEY, dict(startbreaker.state(p.db), tried=time.time() - startbreaker.RETRY_S - 1))
            d.dispatch()
        assert [r["task"] for r in p.db.q("SELECT task FROM runs WHERE role!='coordinator' ORDER BY id")] == \
            ids[:3] + [ids[3], ids[0]]
        assert startbreaker.is_open(p.db) and startbreaker.state(p.db)["tasks"] == ids
        assert all(int(p.db.task(t)["attempts"] or 0) == 0 for t in ids)
        # Task 0 failed twice, but both runs never started: they fold into the breaker's one alert.
        trig = coord.effort_triggers(p.db, d.cfg, [], None)[0]
        assert "repeated failures" not in trig, trig
        # A good retry closes it, clears the alert's condition and every task starts in the same pass.
        monkeypatch.setattr(testslots, "worker_env", real)
        p.db.x("UPDATE tasks SET not_before=NULL")
        p.db.set_kv(startbreaker.KEY, dict(startbreaker.state(p.db), tried=time.time() - startbreaker.RETRY_S - 1))
        d.dispatch()
        assert not startbreaker.is_open(p.db)
        assert sorted(_slot_tasks(p)) == ids
        assert all(int(p.db.task(t)["attempts"] or 0) == 0 and not p.db.task(t)["blocked_reason"] for t in ids)
        now = time.time()
        assert not alerts.holds(p.db, "run-start", now - 5, now)
        # Closed, the never-started runs count again.
        assert "repeated failures" in coord.effort_triggers(p.db, d.cfg, [], None)[0]
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_cause_failing_only_some_tasks_never_holds_a_healthy_task_past_one_retry(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """Three urgent tasks fail at one shared line (their kind's prompt), which opens the breaker. The
    retry goes to a queued task the cause has not failed, ahead of the failed ones, so the healthy task
    starts on the first retry and closes the breaker, rather than each retry re-failing a bad task."""
    from ttp import prompts, startbreaker
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=5)
    try:
        bad = [p.db.add_task(f"bad{i}", "s", kind="work", tier="light", origin="user", priority=1) for i in range(3)]
        real = prompts.worker_system

        def system(proj, task):
            if task["id"] in bad:
                raise KeyError("its kind's prompt")
            return real(proj, task)
        monkeypatch.setattr(prompts, "worker_system", system)
        d.dispatch()
        assert startbreaker.is_open(p.db) and startbreaker.state(p.db)["tasks"] == bad
        good = p.db.add_task("healthy", "s", kind="work", tier="light", origin="user", priority=3)
        p.db.x("UPDATE tasks SET not_before=NULL")
        d.dispatch()
        assert _slot_tasks(p) == [] and p.db.task(good)["blocked_reason"].startswith(startbreaker.HELD_NOTE)
        p.db.set_kv(startbreaker.KEY, dict(startbreaker.state(p.db), tried=time.time() - startbreaker.RETRY_S - 1))
        d.dispatch()
        assert _slot_tasks(p) == [good] and not p.db.task(good)["blocked_reason"]
        # Closed by that start, the bad tasks tried again in the same pass and reopened it: the cause is live.
        assert startbreaker.is_open(p.db) and startbreaker.state(p.db)["tasks"] == bad
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_good_start_forgets_the_failures_before_it_and_a_retry_failing_elsewhere_holds_nothing(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """Failures on either side of a good start never add up to an open breaker; a retry that fails at
    another line closes it, and the rest of that pass starts with no held note."""
    from ttp import prompts, startbreaker
    now = time.time()
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=5)
    try:
        for t in (1, 2):
            startbreaker.record(p.db, "E at a.py:1", "E", 9000 + t, now, d._breaker_stamp())
        good = p.db.add_task("starts fine", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        assert _slot_tasks(p) == [good] and not startbreaker.state(p.db).get("fails")
        assert startbreaker.record(p.db, "E at a.py:1", "E", 9003, now, d._breaker_stamp())[0] is None
        # Open on one line; the retry fails at another, and the next task starts in the same pass.
        for t in (4, 5):
            startbreaker.record(p.db, "E at a.py:1", "E", 9000 + t, now, d._breaker_stamp())
        assert startbreaker.is_open(p.db)
        first = p.db.add_task("fails elsewhere", "s", kind="work", tier="light", origin="user", priority=1)
        second = p.db.add_task("starts", "s", kind="work", tier="light", origin="user", priority=2)
        real = prompts.worker_system

        def system(proj, task):
            if task["id"] == first:
                raise ValueError("elsewhere")
            return real(proj, task)
        monkeypatch.setattr(prompts, "worker_system", system)
        p.db.set_kv(startbreaker.KEY, dict(startbreaker.state(p.db), tried=time.time() - startbreaker.RETRY_S - 1))
        d.dispatch()
        assert not startbreaker.is_open(p.db)
        assert _slot_tasks(p) == [good, second] and not p.db.task(second)["blocked_reason"]
        assert "None" not in (p.db.task(first)["blocked_reason"] or "")
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_one_tasks_own_start_failures_never_open_the_run_start_breaker(env, monkeypatch, tmp_path, reap_tmp_processes):
    """Three failed starts of one task, or the same type on three tasks at different lines, are no
    systemic cause: the breaker stays shut and the other tasks start."""
    from ttp import prompts, startbreaker
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=3)
    try:
        bad = p.db.add_task("breaks at start", "s", kind="work", tier="light", origin="user", priority=1)
        real = prompts.worker_system

        def system(proj, task):
            if task["id"] == bad:
                raise ValueError("its own spec")
            return real(proj, task)
        monkeypatch.setattr(prompts, "worker_system", system)
        for _ in range(3):
            p.db.update_task(bad, not_before=None)
            d.dispatch()
        assert not startbreaker.is_open(p.db)
        assert len(startbreaker.state(p.db)["fails"]) == 3
        good = p.db.add_task("starts fine", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        assert _slot_tasks(p) == [good]
        # Same type, different lines on three tasks.
        now = time.time()
        for t, line in ((1, 10), (2, 20), (3, 30)):
            st, _ = startbreaker.record(p.db, f"KeyError at x.py:{line}", "KeyError", 9000 + t, now, "s")
            assert st is None
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_the_run_start_breaker_closes_on_a_config_or_runtime_change_and_on_a_retry_failing_elsewhere(env):
    """Opened under one config, a changed config (or runtime) closes it at the next gate; a retry that
    fails at another line closes it too, and failures older than its window are forgotten."""
    from ttp import startbreaker
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.cfg = p.config()
    now = time.time()
    stamp = d._breaker_stamp()
    assert startbreaker.record(p.db, "E at a.py:1", "E", 3, now - startbreaker.WINDOW_S - 1, stamp)[0] is None
    for t in (1, 2):
        assert startbreaker.record(p.db, "E at a.py:1", "E", t, now, stamp)[0] is None
    assert startbreaker.record(p.db, "E at a.py:1", "E", 2, now, stamp)[0] is None   # the same task twice
    opened, said = startbreaker.record(p.db, "E at a.py:1", "E: boom", 4, now, stamp)
    assert opened and opened["tasks"] == [1, 2, 4] and "opened" in said
    assert startbreaker.gate(p.db, now, lambda: stamp) == ("held", "")
    assert startbreaker.gate(p.db, now + startbreaker.RETRY_S, lambda: stamp) == ("probe", "")
    d.cfg = {**d.cfg, "checks": {}}
    assert d._breaker_stamp() != stamp
    brk, said = startbreaker.gate(p.db, now, d._breaker_stamp)
    assert brk == "" and "changed" in said and not startbreaker.is_open(p.db)
    for t in (1, 2, 3):
        startbreaker.record(p.db, "E at a.py:1", "E", t, now, stamp)
    assert startbreaker.is_open(p.db)
    st, said = startbreaker.record(p.db, "F at b.py:9", "F", 1, now, stamp)
    assert st is None and "elsewhere" in said and not startbreaker.is_open(p.db)


def test_a_scalar_config_section_is_refused_never_kept_as_last_good_and_a_missing_snapshot_is_no_error(
        env, capsys, monkeypatch):
    """`ttp config checks host_max` must be refused with the account-level form, as must config_set;
    config_problems names such a section, sizes() reads it as absent, no last-good copy keeps it and a
    restore from an older copy that holds one drops it. A missing last-good file is only "no snapshot
    yet": a valid config reads "ok" and writes a fresh one."""
    from ttp import cli, integrity, testslots
    from ttp.coordinator import apply
    from ttp.project import Project, config_problems
    p = make(env)
    good = p.state / "project.last-good.json"
    before = p.raw_config()
    for key, val in (("checks", "host_max"), ("budget", 5), ("providers.fake", "x")):
        with pytest.raises(ValueError, match=f"{re.escape(key)} is a section of settings"):
            p.set_config(key, val)
    with pytest.raises(SystemExit):
        cli.main(["config", "demo", "checks", "host_max"])
    assert "ttp config --account checks.host_max N" in capsys.readouterr().err
    probs = apply(p, [{"type": "config_set", "key": "checks", "value": "host_max"}])
    assert any(x.startswith("config_set: checks") for x in probs), probs   # refused, never saved
    assert p.raw_config() == before
    p.set_config("checks.test_pool", 4)   # a key inside the section is fine
    assert p.raw_config()["checks"] == {"test_pool": 4}

    # A dotted path through a plain value is refused and writes nothing; only a scalar section is replaced.
    raw = p.raw_config()
    raw.update(core_provider="claude", delivery={"push_checks": ["pytest -q"]}, checks="host_max")
    p.config_path.write_text(json.dumps(raw))
    for key, val, prefix in (("delivery.push_checks.0", "x", "delivery.push_checks"),
                             ("core_provider.x", 1, "core_provider")):
        with pytest.raises(ValueError, match=f"{re.escape(prefix)} is one value, not a section"):
            p.set_config(key, val)
        assert p.raw_config() == raw
    probs = apply(p, [{"type": "config_set", "key": "core_provider.x", "value": 1}])
    assert any(x.startswith("config_set: core_provider.x") for x in probs), probs
    assert p.raw_config() == raw
    p.set_config("checks.test_pool", 4)
    assert p.raw_config()["checks"] == {"test_pool": 4}

    good.unlink(missing_ok=True)   # no snapshot yet: not an error, and the valid config becomes one
    fresh = Project(p.base)
    assert fresh.config()["checks"]["test_pool"] == 4 and fresh.config_status == "ok"
    assert json.loads(good.read_text())["checks"] == {"test_pool": 4}

    raw = p.raw_config()
    raw["checks"] = "host_max"
    p.config_path.write_text(json.dumps(raw))
    good.unlink()
    fresh = Project(p.base)
    assert "checks" not in fresh.config() or isinstance(fresh.config()["checks"], dict)
    assert fresh.config_status == "ok" and not good.exists(), "a scalar section became the last good copy"
    assert any(x.startswith("checks: a section of settings holds") for x in config_problems(raw))
    assert testslots.sizes(raw, cores=16) == (8, 4), "a scalar checks section crashed sizes()"
    assert testslots.project_max(raw) >= 1

    # An older copy that kept one: the fallback read drops it, the reboot integrity check never restores it.
    good.write_text(json.dumps(raw))
    p.config_path.write_text("{ not json")
    fresh = Project(p.base)
    assert "host_max" not in json.dumps(fresh.config().get("checks")) and fresh.config_status == "fallback"
    monkeypatch.setattr(integrity, "_head", lambda proj, name: b"")
    out = integrity.check_harness(p)
    assert p.config_path.read_text() == "{ not json" and "project.json is damaged and has no good copy" in out["bad"]


def test_an_urgent_task_starts_first_and_takes_one_extra_slot_past_full_routine_work(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """The urgent lane (ttp/urgent.py): with every slot taken by routine work, one urgent task starts in
    budget.urgent_extra_slots beyond max_parallel_workers; a second waits; 0 turns it off."""
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    try:
        r1 = p.db.add_task("routine one", "s", kind="work", tier="light", origin="user", priority=3)
        r2 = p.db.add_task("routine two", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        assert _slot_tasks(p) == [r1, r2]
        r3 = p.db.add_task("routine three", "s", kind="work", tier="light", origin="user", priority=2)
        hot = p.db.add_task("time-sensitive fix", "Fix it.\nurgent: the user is waiting on it", kind="work",
                            tier="light", origin="user", priority=3)
        hot2 = p.db.add_task("another urgent fix", "s", kind="work", tier="light", origin="user", priority=1)
        assert [t["id"] for t in p.db.ready_tasks()] == [hot, hot2, r3], "a stated urgent line first, then p1"
        d.cfg["budget"]["urgent_preempt"] = False   # the extra slots alone (preemption: its own test)
        d.cfg["budget"]["urgent_extra_slots"] = 0
        d.dispatch()
        assert _slot_tasks(p) == [r1, r2], "0 turns the extra slot off"
        d.cfg["budget"]["urgent_extra_slots"] = 1
        d.dispatch()
        assert _slot_tasks(p) == [r1, r2, hot], "one extra slot, for the task with an urgent line"
        d.dispatch()
        assert _slot_tasks(p) == [r1, r2, hot], "priority 1 alone never takes the extra slot"
        settle(d)
        d.dispatch()
        assert _slot_tasks(p) == [hot2, r3], "priority 1 before routine priority 2"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_routine_work_leaves_one_slot_for_queued_or_soon_due_urgent_work_and_dispatch_starts_it_model_free(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """budget.urgent_reserve_slots (default 1): while a priority 1 task is queued or waiting with its retry
    due within the hour, routine work fills all slots but one, so the urgent task starts in the same
    dispatch pass it turns ready, with no coordinator turn. No urgent work open, or 0: routine fills all."""
    from ttp import urgent
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=3)
    try:
        hot = p.db.add_task("urgent fix", "s", kind="work", tier="light", origin="user", priority=1)
        p.db.update_task(hot, status="waiting", not_before=time.time() + 300)
        routine = [p.db.add_task(f"routine {i}", "s", kind="work", tier="light", origin="user", priority=2)
                   for i in range(3)]
        d.dispatch()
        assert _slot_tasks(p) == routine[:2], "one slot kept for the urgent task due in 5 min"
        p.db.update_task(hot, status="queued", not_before=None)   # its probe passed
        d.dispatch()
        assert _slot_tasks(p) == routine[:2] + [hot], "it starts at once, in a regular slot"
        assert not p.db.q("SELECT 1 FROM runs WHERE role='coordinator'"), "dispatch is model-free"
        settle(d)
        far = p.db.add_task("urgent, waiting on a user", "s", kind="work", tier="light", origin="user", priority=1)
        p.db.update_task(far, status="waiting", not_before=time.time() + 6 * 3600)
        for i in range(3):
            p.db.add_task(f"routine more {i}", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        assert len(_slot_tasks(p)) == 3, "a wait due hours away keeps no slot"
        p.db.update_task(far, status="queued", not_before=None)
        assert urgent.reserve_slots(d.cfg, p.db, time.time(), 3) == 1
        assert urgent.reserve_slots(d.cfg, p.db, time.time(), 1) == 0, "one slot always stays open to routine"
        d.cfg["budget"]["urgent_reserve_slots"] = 0
        assert urgent.reserve_slots(d.cfg, p.db, time.time(), 3) == 0, "0 turns it off"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_the_urgent_reserve_keeps_no_slot_for_priority_1_work_that_could_not_start(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """The reserve counts only a priority 1 task dispatch could start now or within the hour, for the
    provider whose slot is decided: one deferred hours away, waiting on a running task, on a paused
    resource, queued for another provider or a review held by its own pre-review checks keeps none."""
    from ttp import coordinator as coord, urgent
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=3)
    core = d.cfg.get("core_provider", "claude")
    other = "codex" if core != "codex" else "claude"
    later = time.time() + 6 * 3600
    blocker = p.db.add_task("long run", "s", kind="work", tier="light", origin="user", priority=3)
    p.db.update_task(blocker, status="running")
    coord.pause_resource(p, "board", True, reason="maintenance", by="user")
    review_of = p.db.add_task("urgent code", "s", kind="code", tier="light", origin="user", priority=1)
    p.db.update_task(review_of, status="review")
    cases = {
        "start_after 6 h": dict(not_before=later, labels=[f"start_after:{later}"]),
        "depends on a running task": dict(depends_on=[blocker]),
        "a paused resource": dict(labels=["resource:board"]),
        "another provider": dict(provider=other),
        "a review behind its pre-review checks": dict(kind="review", depends_on=[review_of],
                                                      labels=[f"auto_review:{review_of}"]),
    }
    import dataclasses
    d.gates[other] = dataclasses.replace(d.gates[core], max_parallel=0)   # it stays queued, not started
    held = set()
    monkeypatch.setattr(d, "_precheck_holds", lambda t, now: t["id"] in held)
    monkeypatch.setattr(d, "_precheck_failed", lambda t: False)
    try:
        for i, (case, kw) in enumerate(cases.items()):
            hot = p.db.add_task(f"urgent: {case}", "s", **{"kind": "work", "tier": "light", "origin": "user",
                                                          "priority": 1, **kw})
            if kw.get("kind") == "review":
                assert urgent.reserve_slots(d.cfg, p.db, time.time(), 3, provider=core) == 1, "ready unless held"
                held.add(hot)
            else:
                assert urgent.reserve_slots(d.cfg, p.db, time.time(), 3, provider=core) == 0, case
            routine = [p.db.add_task(f"routine {i}.{j}", "s", kind="work", tier="light", origin="user", priority=2)
                       for j in range(3)]
            d.dispatch()
            assert _slot_tasks(p) == routine, f"a slot stayed idle for a p1 task that cannot start: {case}"
            settle(d)
            p.db.update_task(hot, status="cancelled")
        ready = p.db.add_task("urgent, ready", "s", kind="work", tier="light", origin="user", priority=1,
                              provider=other)
        assert urgent.reserve_slots(d.cfg, p.db, time.time(), 3, provider=other) == 1, "its own provider keeps one"
        assert urgent.reserve_slots(d.cfg, p.db, time.time(), 3, provider=core) == 0
        p.db.update_task(ready, status="running")
        assert urgent.reserve_slots(d.cfg, p.db, time.time(), 3, provider=other,
                                    due=[p.db.task(ready)]) == 0, "one dispatch started since keeps none"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_unstarted_events_ride_with_the_next_turn_and_never_wake_alone():
    from ttp import coordinator, unstarted
    assert coordinator.batchable(unstarted.READY_KIND, "normal")
    assert coordinator.batchable(unstarted.ANNOUNCED_KIND, "normal")


def test_the_spend_gate_still_holds_an_urgent_task(env, monkeypatch, tmp_path, reap_tmp_processes):
    """allow_new_work False, or a gate of no slots, holds an urgent task like any other: no extra slot."""
    import dataclasses
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    try:
        core = d.cfg.get("core_provider", "claude")
        hot = p.db.add_task("urgent fix", "s", kind="work", tier="light", origin="user", priority=1)
        gate = d.gates[core]
        d.gates[core] = dataclasses.replace(gate, allow_new_work=False)
        d.dispatch()
        assert _slot_tasks(p) == [], "the spend gate let an urgent task start"
        d.gates[core] = dataclasses.replace(gate, max_parallel=0)
        d.dispatch()
        assert _slot_tasks(p) == [], "a gate of no slots gave an urgent task the extra one"
        d.gates[core] = gate
        d.dispatch()
        assert _slot_tasks(p) == [hot]
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_an_urgent_task_held_by_anything_raises_one_slot_wait_naming_the_hold_at_ten_minutes(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """coordinator.urgent_slot_wait_min (default 10): an urgent task held by a paused resource (not the
    slots) raises one event naming that hold, counted from when it was queued; a routine one none."""
    from ttp import coordinator as coord
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    try:
        assert d.cfg["coordinator"]["urgent_slot_wait_min"] == 10
        coord.pause_resource(p, "board", True, reason="maintenance", by="user")
        hot = p.db.add_task("urgent fix", "Fix it.\nurgent: a demo in an hour", kind="work", tier="light",
                            origin="user", priority=3, labels=["resource:board"])
        p.db.add_task("routine", "s", kind="work", tier="light", origin="user", priority=3,
                      labels=["resource:board"])
        start = time.time()

        def check(minutes):
            d._slot_wait_checked = start + minutes * 60 - 60
            monkeypatch.setattr(time, "time", lambda: start + minutes * 60)
            d.check_slot_wait()
            return _slot_wait_events(p)

        assert check(1) == [] and check(9) == []
        (ev,) = check(10)
        assert ev["task"] == hot and ev["severity"] == "normal" and ev["status"] == "queued"
        assert "urgent: a demo in an hour" in ev["text"] and "pause on resource board" in ev["text"]
        assert len(check(11)) == 1 and len(check(30)) == 1, "raised again for the same wait"
    finally:
        monkeypatch.undo()
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def _held_by_guard(d, core, now, reasons):
    import dataclasses
    from ttp import budget as bud
    g = dataclasses.replace(d.gates[core], level="red", allow_new_work=False, max_parallel=0,
                            reasons=list(reasons), cause=reasons[0])
    assert bud.red_class(reasons[0]) is not None or reasons[0].startswith("provider limit")
    d.gates[core] = g


def test_an_urgent_task_held_only_by_the_runaway_guard_wakes_nobody_and_is_listed_in_the_digest(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """The runaway guard (or any spend limit only time or the user clears) holding a ready urgent task
    raises no urgent-wait event, which would be a self-started coordinator turn adding to the guard:
    the next digest lists it, one line per task, with when the guard is expected to clear. Once a hold
    the coordinator can clear takes over (a pause), the event is raised as before."""
    from ttp import coordinator as coord, urgent
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    core = d.cfg.get("core_provider", "claude")
    try:
        start = time.time()
        # A self-started coordinator turn that ended 20 minutes ago is the newest run the guard counts.
        p.db.x("INSERT INTO runs(task,role,provider,status,started,ended,cost_usd) VALUES(NULL,'coordinator',?,"
               "'done',?,?,4.09)", (core, start - 1500, start - 1200))
        _held_by_guard(d, core, start, ["runaway guard: coordinator spent $4.09 on self-started turns in the "
                                        "last hour (limit $4; turns for user messages not counted)"])
        hot = p.db.add_task("urgent fix", "Fix it.\nurgent: a demo in an hour", kind="work", tier="light",
                            origin="user", priority=3)
        other = p.db.add_task("urgent too", "s", kind="work", tier="light", origin="user", priority=1)
        p.db.add_task("routine", "s", kind="work", tier="light", origin="user", priority=3)

        def check(minutes):
            d._slot_wait_checked = start + minutes * 60 - 60
            monkeypatch.setattr(time, "time", lambda: start + minutes * 60)
            d.check_slot_wait()
            return _slot_wait_events(p)

        assert check(9) == [] and not p.db.kv(urgent.SPEND_HELD_KEY), "listed before the wait threshold"
        assert check(10) == [] and check(30) == [], "the runaway guard alone woke the coordinator"
        held = p.db.kv(urgent.SPEND_HELD_KEY)
        assert sorted(held) == sorted([str(hot), str(other)])
        assert held[str(hot)]["clears_at"] == start - 1200 + 3600, "an hour after the newest counted run ended"
        assert "runaway guard: coordinator spent $4.09" in held[str(hot)]["held"]
        lines = urgent.spend_held_lines(p, p.db, start + 30 * 60)
        assert lines[0].startswith("## Urgent tasks held only by a spend limit") and len(lines) == 3
        line = next(x for x in lines if x.startswith(f"- #{hot} "))
        assert "urgent: a demo in an hour" in line and "waited 30 min" in line and "expected to clear by" in line
        assert "runaway guard's hour rolls over" in line
        parts, _ = coord.digest_parts(p, {k: g.as_dict() for k, g in d.gates.items()}, [], [])
        assert any(k == "urgent_spend_held" and f"- #{hot} " in body for k, body in parts)

        # A task that left the queue drops out of the digest; a cap only the user raises has no clear time.
        p.db.update_task(other, status="done")
        _held_by_guard(d, core, start, ["cap reached: daily $50"])
        assert check(31) == []
        held = p.db.kv(urgent.SPEND_HELD_KEY)
        assert list(held) == [str(hot)] and held[str(hot)]["clears_at"] is None
        assert "no clear time known (the cap's period rolls over or the user raises it)" in \
            urgent.spend_held_lines(p, p.db, start + 31 * 60)[1]

        # The gate lifts but the run does not fit the dollar caps: also a limit only time or the user clears.
        p.db.x("DELETE FROM runs WHERE role='coordinator'")
        d.update_gates()
        assert d.gates[core].allow_new_work and d.gates[core].regime == "caps"
        monkeypatch.setattr(type(d), "_fits_caps", staticmethod(lambda gate, usd: False))
        assert check(32) == []
        assert "the user raises a cap" in p.db.kv(urgent.SPEND_HELD_KEY)[str(hot)]["clears"]
        monkeypatch.undo()
        monkeypatch.setattr(time, "time", lambda: start + 32 * 60)

        # A pause holds it instead: a hold the coordinator can act on wakes it.
        coord.pause_resource(p, "board", True, reason="maintenance", by="user")
        p.db.update_task(hot, labels=["resource:board"])
        (ev,) = check(33)
        assert ev["task"] == hot and "pause on resource board" in ev["text"]
        assert p.db.kv(urgent.SPEND_HELD_KEY) == {}
    finally:
        monkeypatch.undo()
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_an_urgent_task_held_by_a_busy_resource_still_wakes_the_coordinator(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """A resource another task holds (the lock-style hold) is one the coordinator can act on: the
    urgent-wait event is raised as before and nothing is listed as spend-held."""
    from ttp import urgent
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    _board_slots(p, d, 1)
    try:
        holder = _board_add(p, "holds the board", 3)
        d.dispatch()
        assert _slot_tasks(p) == [holder]
        hot = _board_add(p, "urgent fix", 1)
        start = time.time()
        d._slot_wait_checked = start + 600 - 60
        monkeypatch.setattr(time, "time", lambda: start + 600)
        d.check_slot_wait()
        (ev,) = _slot_wait_events(p)
        assert ev["task"] == hot and "resource board" in ev["text"] and f"#{holder}" in ev["text"]
        assert p.db.kv(urgent.SPEND_HELD_KEY) in (None, {})
    finally:
        monkeypatch.undo()
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_an_urgent_task_reserves_an_exclusive_slot_over_an_obligation_owner(env, monkeypatch, tmp_path):
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    owner = p.db.add_task("owes a deliverable", "s", kind="work", tier="light", origin="user", priority=2)
    _owe(p, owner, time.time() + 3600)
    hot = p.db.add_task("urgent fix", "s\nurgent: the user asked twice", kind="work", tier="light", origin="user",
                        priority=3)
    rows = {t["id"]: t for t in p.db.ready_tasks()}
    monkeypatch.setattr(d, "_slot_contenders", lambda now: ({"board": [rows[hot], rows[owner]]}, {}))
    assert d._slot_reservers(time.time()) == {"board": hot}
    monkeypatch.setattr(d, "_slot_contenders", lambda now: ({"board": [rows[owner], rows[hot]]}, {}))
    assert d._slot_reservers(time.time()) == {}, "an owner first outranks no urgent contender"


def _resume_setup(env, monkeypatch, tmp_path, reap_tmp_processes, **more):
    """One worker slot. An older backlog task (p3, queued before the waiter), a waiter (p3) that handed
    off waiting on a file probe (and the hand-off fields `more`) and a holder (p3) running in the only
    slot. Returns (p, d, drain, go, backlog, waiter, holder, flag): touching flag passes the probe."""
    p, d, drain, go = _board_setup(env, monkeypatch, tmp_path, board=1)
    backlog = p.db.add_task("older backlog, never started", "s", kind="work", tier="light", origin="user", priority=3)
    p.db.update_task(backlog, status="blocked")   # out of the way while the waiter runs
    waiter = p.db.add_task("waits on its checks", "s", kind="work", tier="light", origin="user", priority=3)
    flag = tmp_path / "checks-passed"
    _board_waiting(p, d, tmp_path, waiter, probe=f"test -e {shlex.quote(str(flag))}", **more)
    p.set_config("budget.max_parallel_workers", 1)
    d.cfg = p.config()
    d.update_gates()
    holder = p.db.add_task("routine work holding the slot", "s", kind="work", tier="light", origin="user",
                           priority=3)
    d.dispatch()
    assert _slot_tasks(p) == [holder]
    p.db.update_task(backlog, status="queued")
    return p, d, drain, go, backlog, waiter, holder, flag


def _probe_until(p, d, tid, done):
    """Run probe_waiting until done(task) holds (its probe runs in the background)."""
    assert _until_true(lambda: d.probe_waiting() or done(p.db.task(tid))), p.db.task(tid)


def test_a_passed_wait_outranks_older_new_work_of_its_priority_under_a_full_worker_cap(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """The reported case: every worker slot is taken; a task's checks wait passes while an older,
    never-started task of the same priority is queued. The woken task is first in line, and takes
    the slot when it frees."""
    p, d, drain, go, backlog, waiter, holder, flag = _resume_setup(env, monkeypatch, tmp_path, reap_tmp_processes)
    try:
        assert [t["id"] for t in p.db.ready_tasks()] == [backlog]
        flag.touch()
        _probe_until(p, d, waiter, lambda t: json.loads(t["result"]).get("woke"))
        assert [t["id"] for t in p.db.ready_tasks()] == [waiter, backlog], "the passed wait queued behind new work"
        d.dispatch()
        assert _slot_tasks(p) == [holder], "the full cap let a task start"
        go(holder)
        d.dispatch()
        assert _slot_tasks(p) == [waiter], "the freed slot went to new work before the passed wait"
        assert p.db.task(backlog)["status"] == "queued"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_ready_to_resume_order_keeps_priority_levels_and_puts_the_longest_ready_first(env):
    """Within a priority the woken waits go first, the one ready longest first; across levels priority
    (and urgency) still wins: a p2 new task beats a p3 resume, a p1 beats everything."""
    p = make(env)
    p.db.x("UPDATE tasks SET status='done'")
    now = time.time()

    def add(title, prio, woke_at=None):
        tid = p.db.add_task(title, "s", kind="work", tier="light", origin="user", priority=prio)
        if woke_at is not None:
            p.db.update_task(tid, not_before=woke_at, result=json.dumps(
                {"status": "waiting", "summary": "checks", "retry_when": "true", "woke": "probe passed"}))
        return tid

    new3 = add("new p3", 3)
    new2 = add("new p2", 2)
    late3 = add("p3 woke a minute ago", 3, now - 60)
    early3 = add("p3 woke an hour ago", 3, now - 3600)
    res4 = add("p4 woke long ago", 4, now - 7200)
    new4 = add("new p4", 4)
    urgent = add("new p1", 1)
    asleep = add("p3 still waiting", 3)
    p.db.update_task(asleep, not_before=now - 30, result=json.dumps(
        {"status": "waiting", "summary": "checks", "retry_when": "true"}))   # its timer ran out, no wake
    assert [t["id"] for t in p.db.ready_tasks()] == [urgent, new2, early3, late3, new3, asleep, res4, new4]


def test_a_passed_wait_with_an_on_pass_cmd_settles_model_free_while_every_slot_is_taken(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """The worker cap is full; the waiter's probe passes and its hand-off names an on_pass_cmd. The
    daemon runs that command itself and records done: no worker slot, no model run."""
    cmd = """printf '{"status": "done", "summary": "checks passed; settled"}' > "$TTP_RESULT\""""
    p, d, drain, go, backlog, waiter, holder, flag = _resume_setup(env, monkeypatch, tmp_path, reap_tmp_processes,
                                                                   on_pass_cmd=cmd)
    try:
        runs = p.db.one("SELECT COUNT(*) AS n FROM runs WHERE task=?", (waiter,))["n"]
        flag.touch()
        _probe_until(p, d, waiter, lambda t: waiter in d._on_pass or t["status"] == "done")
        assert _slot_tasks(p) == [holder], "the settle step took a worker slot"
        assert _until_true(lambda: d._poll_on_pass(time.time()) or p.db.task(waiter)["status"] == "done"), \
            p.db.task(waiter)
        assert json.loads(p.db.task(waiter)["result"])["summary"].startswith("checks passed; settled")
        assert p.db.one("SELECT COUNT(*) AS n FROM runs WHERE task=?", (waiter,))["n"] == runs, "a model run started"
        assert _slot_tasks(p) == [holder]
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_resume_wait_fires_once_for_a_passed_wait_not_started_in_15_min_naming_its_hold(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    p, d, drain, go, backlog, waiter, holder, flag = _resume_setup(env, monkeypatch, tmp_path, reap_tmp_processes)
    try:
        flag.touch()
        _probe_until(p, d, waiter, lambda t: json.loads(t["result"]).get("woke"))
        start = p.db.task(waiter)["not_before"]

        def check(minutes):
            d._resume_wait_checked = 0.0
            monkeypatch.setattr(time, "time", lambda: start + minutes * 60)
            d.check_resume_wait()
            monkeypatch.undo()
            return p.db.q("SELECT * FROM events WHERE kind='observation' AND fingerprint LIKE 'resume_wait:%'")

        assert check(0) == [] and check(14) == []
        events = check(15)
        assert [e["task"] for e in events] == [waiter], "only the passed wait, not the backlog task"
        assert f"#{waiter} waits on its checks (p3)" in events[0]["text"] and "slot taken" in events[0]["text"]
        assert len(check(30)) == 1 and len(check(120)) == 1, "raised again for the same wait"
        p.set_config("coordinator.resume_wait_min", 0)
        d.cfg = p.config()
        p.db.x("DELETE FROM events WHERE fingerprint LIKE 'resume_wait:%'")
        assert check(60) == [], "resume_wait_min 0 did not turn it off"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"



def test_a_queued_resume_wait_names_the_hold_true_now_not_a_spend_gate_that_cleared(env, monkeypatch):
    """The reported case: a resume wait raised while the global daily cap held new work is still queued
    when the cap is raised. The next check names what holds it now, not the cleared spend gate; once the
    task starts, the queued observation is retired."""
    from ttp import coordinator as coord, globalcap as gcap
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    core = p.config().get("core_provider", "claude")
    monkeypatch.setattr(gcap, "total", lambda *a, **k: {"usd": 2017.0, "stale": [], "includes": []})
    p.set_config("budget.global_daily_usd", 2000)
    d = Daemon(p.base)
    d.cfg = p.config()
    d.update_gates()
    assert not d.gates[core].allow_new_work and "global daily cap reached" in d.gates[core].cause, d.gates[core]
    now = time.time()
    tid, paused = (p.db.add_task(title, "s", kind="work", tier="light", origin="user", priority=3, **kw)
                   for title, kw in (("woken wait", {}), ("woken wait on a board", {"labels": ["resource:board"]})))
    for t in (tid, paused):
        p.db.update_task(t, result=json.dumps({"status": "waiting", "woke": True}), not_before=now - 20 * 60)

    def check():
        d._resume_wait_checked = 0.0
        d.check_resume_wait()
        return p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'resume_wait:%'")

    first = check()
    assert [e["task"] for e in first] == [tid, paused]
    assert "held by the spend gate (no new work at red: global daily cap" in first[0]["text"]
    p.set_config("budget.global_daily_usd", 3000)   # the cap is raised before a turn read the observation
    d.cfg = p.config()
    d.update_gates()
    assert d.gates[core].allow_new_work, d.gates[core]
    coord.pause_resource(p, "board", True, reason="maintenance", by="user")   # a hold that says so itself
    again = check()
    assert [(e["id"], e["status"]) for e in again] == [(first[0]["id"], "queued"), (first[1]["id"], "handled")]
    assert json.loads(again[1]["data"])["retired"] == "held: paused"
    assert "spend gate" not in again[0]["text"], again[0]["text"]
    assert f"#{tid} woken wait" in again[0]["text"]
    p.db.update_task(tid, status="running")   # it resumed before any turn read it
    gone = check()
    assert gone[0]["status"] == "handled" and json.loads(gone[0]["data"])["retired"] == "resumed or left the queue"


def test_a_resume_wait_retired_for_a_passing_net_hold_is_raised_again_while_the_spend_gate_holds(env, monkeypatch):
    """A one-check network blip retires the unread observation (the network hold reports itself). Once it
    clears and the global cap still holds the task, the next check queues the observation again."""
    from ttp import globalcap as gcap
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    core = p.config().get("core_provider", "claude")
    monkeypatch.setattr(gcap, "total", lambda *a, **k: {"usd": 2017.0, "stale": [], "includes": []})
    p.set_config("budget.global_daily_usd", 2000)
    d = Daemon(p.base)
    d.cfg = p.config()
    d.update_gates()
    assert not d.gates[core].allow_new_work, d.gates[core]
    tid = p.db.add_task("woken wait", "s", kind="work", tier="light", origin="user", priority=3)
    p.db.update_task(tid, result=json.dumps({"status": "waiting", "woke": True}), not_before=time.time() - 20 * 60)

    def check():
        d._resume_wait_checked = 0.0
        d.check_resume_wait()
        return p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'resume_wait:%'")

    first = check()
    assert [(e["task"], e["status"]) for e in first] == [(tid, "queued")]
    real = Daemon._start_hold
    monkeypatch.setattr(Daemon, "_start_hold", lambda self, *a: "net")
    blip = check()
    assert [e["status"] for e in blip] == ["handled"]
    monkeypatch.setattr(Daemon, "_start_hold", real)
    back = check()
    assert [(e["id"], e["status"]) for e in back] == [(first[0]["id"], "queued")]
    assert "held by the spend gate (no new work at red: global daily cap" in back[0]["text"], back[0]["text"]
    assert check() == back   # stays one queued row, unchanged


def _merge_task(p, **kw):
    """The harness task `ttp upgrade` queues to finish a template merge, as it queues it (cli.upgrade)."""
    from ttp import release
    return p.db.add_task(release.UPGRADE_TASK_TITLE, "merge upstream", kind="harness", tier="standard",
                         priority=2, origin="user", **kw)


def test_a_template_merge_task_starts_ahead_of_queued_p1_work_and_the_gates_still_hold_it(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """With every slot taken by priority-1 work, the queued template-merge task starts in the urgent extra
    slot before an older queued priority-1 task (urgent.leads), and leads db.ready_tasks; one at a time holds
    the merge, so routine work is not starved. A paused resource it names or a spend gate allowing no new
    work still holds it."""
    import dataclasses
    from ttp import coordinator as coord, urgent
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    core = d.cfg.get("core_provider", "claude")
    try:
        running = [p.db.add_task(f"p1 work {i}", "s", kind="work", tier="light", origin="user", priority=1)
                   for i in range(2)]
        d.dispatch()
        assert _slot_tasks(p) == running
        older_p1 = p.db.add_task("older queued p1", "s", kind="work", tier="light", origin="user", priority=1)
        merge = _merge_task(p)
        assert urgent.is_urgent(p.db.task(merge)) and urgent.why(p.db.task(merge)) == "finishes a template upgrade"
        assert [t["id"] for t in p.db.ready_tasks()] == [merge, older_p1], "the merge task leads the queue"
        d.dispatch()
        assert _slot_tasks(p) == running + [merge], "the merge task waited behind p1 work for a slot"
        settle(d)
        p.db.update_task(merge, status="done")
        d.dispatch()
        assert older_p1 in _slot_tasks(p), "the p1 task starts once the merge is under way"
        settle(d)
        p.db.x("UPDATE tasks SET status='done' WHERE status='queued'")

        coord.pause_resource(p, "board", True, reason="maintenance", by="user")
        held = _merge_task(p, labels=["resource:board"])
        d.dispatch()
        assert _slot_tasks(p) == [], "a paused resource let the merge task start"
        coord.pause_resource(p, "board", False, by="user")
        gate = d.gates[core]
        d.gates[core] = dataclasses.replace(gate, allow_new_work=False)
        d.dispatch()
        assert _slot_tasks(p) == [], "the spend gate let the merge task start"
        d.gates[core] = gate
        d.dispatch()
        assert _slot_tasks(p) == [held]
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_deadline_task_with_an_urgent_line_starts_before_routine_priority_1_work_and_takes_the_extra_slot(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """The stall: a project running its routine work at priority 1 (older tasks, woken waits) ranked a fresh
    `urgent:` deadline task behind all of it, and routine priority 1 took the one extra slot. Now the stated
    line ranks first and only it may start past full slots; a stale line (over urgent.STALE_S) ranks as
    priority alone."""
    from ttp import urgent
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    try:
        routine = [p.db.add_task(f"routine p1 {i}", "s", kind="work", tier="light", origin="coordinator",
                                 priority=1) for i in range(3)]
        d.dispatch()
        assert _slot_tasks(p) == routine[:1], "routine priority 1 fills the only slot"
        stale = p.db.add_task("old deadline", "s\nurgent: last week's deadline", kind="work", tier="light",
                              origin="user", priority=1)
        p.db.x("UPDATE tasks SET created=? WHERE id=?", (time.time() - urgent.STALE_S - 60, stale))
        hot = p.db.add_task("user deadline", "s\nurgent: the user waits on it tonight", kind="work",
                            tier="light", origin="user", priority=1)
        assert [t["id"] for t in p.db.ready_tasks()] == [hot] + routine[1:] + [stale]
        d.dispatch()
        assert _slot_tasks(p) == [routine[0], hot], "the deadline task takes the extra slot at once"
        d.dispatch()
        assert _slot_tasks(p) == [routine[0], hot], "routine and stale priority 1 never take an extra slot"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_schedlog_slot_users_name_what_fills_the_worker_slots(env):
    from ttp import schedlog
    p = make(env)
    p.db.x("UPDATE tasks SET status='done'")
    code = p.db.add_task("a change", "s", kind="code", tier="light", origin="user")
    rev = p.db.add_task("its review", "s", kind="review", tier="light", origin="user")
    gone = p.db.add_task("handed off", "s", kind="work", tier="light", origin="user")
    p.db.update_task(code, status="running")
    p.db.update_task(rev, status="running")
    p.db.update_task(gone, status="done")
    for tid, role in ((code, "worker"), (rev, "reviewer"), (gone, "worker"), (None, "worker"), (None, "coordinator")):
        p.db.x("INSERT INTO runs(task, role, provider, status, started) VALUES(?,?,?,?,?)",
               (tid, role, "claude", "running", time.time()))
    users = schedlog.slot_users(p.db, "claude")
    assert users == {"running": 4, "by_kind": {"code": 1, "review": 1, "work": 1, "worker": 1},
                     "unreaped": 1, "untracked": 1}, users
    assert schedlog.slot_users(p.db, "codex")["running"] == 0
    words = schedlog.slot_words(users)
    assert words.startswith("4 runs (") and "1 review" in words and "1 of them for a task no longer running" in words
    assert schedlog.entry(1, [], {}, [], 0, users)["slots"] == users and "slots" not in schedlog.entry(1, [], {}, [], 0)


def test_dispatch_starts_work_when_obligations_are_unreadable(env, monkeypatch, tmp_path, reap_tmp_processes):
    """An unreadable obligation (e.g. an item missing a field) only loses the owners' boost, never the queue."""
    from ttp import obligations
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    try:
        plain = p.db.add_task("routine work", "s", kind="work", tier="light", origin="user", priority=2)

        def broken(db, *a, **k):
            raise KeyError("receipt")
        monkeypatch.setattr(obligations, "owner_due", broken)
        d.dispatch()
        assert _slot_tasks(p) == [plain]
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_an_obligation_owner_takes_the_extra_slot_past_full_slots_and_routine_priority_1_does_not(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    try:
        routine = [p.db.add_task(f"routine p1 {i}", "s", kind="work", tier="light", origin="coordinator",
                                 priority=1) for i in range(2)]
        d.dispatch()
        assert _slot_tasks(p) == routine[:1], "routine priority 1 fills the only slot"
        owner = p.db.add_task("owes a deliverable", "s", kind="work", tier="light", origin="user", priority=3)
        _owe(p, owner, time.time() + 1800)
        d.dispatch()
        assert _slot_tasks(p) == [routine[0], owner], "the owner of a deliverable due within the hour takes it"
        d.dispatch()
        assert _slot_tasks(p) == [routine[0], owner], "routine priority 1 never takes an extra slot"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def _guard_turns(p, now, n=7, cost=0.60):
    """`n` self-started coordinator turns in the last hour on the core provider costing `cost` each:
    past the old $4/h limit (the retired budget.hourly_coordinator_usd) at 7."""
    core = p.config().get("core_provider", "claude")
    for i in range(n):
        p.db.x("INSERT INTO runs(role,provider,effort,status,started,ended,cost_usd,note) "
               "VALUES('coordinator',?,'high','ok',?,?,?,?)",
               (core, now - 120 - i * 300, now - 60 - i * 300, cost, json.dumps({"messages": [], "events": [i]})))
        p.db.spend(core, cost, "coordinator")


def test_coordinator_spend_past_the_old_hourly_limit_holds_no_worker_task_and_a_dollar_cap_holds_them(
        env, monkeypatch, tmp_path):
    """Self-started coordinator turns past the old $4/h limit hold no worker task: routine, scheduled,
    urgent and user-ordered tasks all start under the normal caps, and `ttp status` says nothing is
    spaced out. A dollar cap reached holds every one."""
    from ttp.cli import status_text
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=4)
    try:
        p.set_config("budget.global_daily_usd", 0)
        d.cfg = p.config()
        now = time.time()
        _guard_turns(p, now)
        d.update_gates()
        core = d.cfg.get("core_provider", "claude")
        g = d.gates[core]
        assert not g.coord_hold and g.level != "red" and g.numbers["coordinator_1h"] == 4.2, g.as_dict()
        add = lambda title, spec="s", **kw: p.db.task(p.db.add_task(title, spec, kind="work", tier="standard",
                                                                     **{"priority": 3, **kw}))
        routine = add("routine work")
        scheduled = add("scheduled sweep", origin="schedule")
        prio1 = add("priority-1 work", priority=1)
        line = add("urgent line", "urgent: the release waits on it\ndo it", priority=2)
        chat = add("answers the user", reply_chat="c1")
        owner = add("owes a deliverable")
        _owe(p, owner["id"], now + 3600)

        def ctx():
            return {"now": time.time(), "paused": {}, "committed": None, "busy": {core: 0}, "logged_out": {}}
        for t in (routine, scheduled, prio1, line, chat, owner):
            c = ctx()
            assert d._start_hold(t, core, c) is None, t["title"]
            assert c["gate"].allow_new_work and c["gate"].max_parallel > 0, t["title"]
        p.db.set_kv("gates", {core: d.gates[core].as_dict()})
        assert "spaced out" not in status_text(p)
        # A dollar cap reached: nothing new starts, urgent or user-ordered included.
        p.set_config("budget.daily_usd", 1)
        d.cfg = p.config()
        d.update_gates()
        for t in (prio1, line, chat, owner, routine, scheduled):
            assert d._start_hold(t, core, ctx()) == "gate", t["title"]
    finally:
        drain(d)


def test_a_coordinator_past_the_old_hourly_limit_still_gets_its_next_self_started_wake(env, monkeypatch):
    """Self-started coordinator spend past the old $4/h limit never defers the session's next wake."""
    p, d, clock, starts = _batch_setup(env, monkeypatch, busy=0)
    p.set_config("budget.global_daily_usd", 0)
    d.cfg = p.config()
    _guard_turns(p, clock[0])
    d.update_gates()
    core = d.cfg.get("core_provider", "claude")
    assert not d.gates[core].coord_hold and d.gates[core].allow_new_work, d.gates[core].as_dict()
    _event(p, clock, "task_done")
    assert _turn_within(d, clock, starts, 600, step=30), "the coordinator's next wake was deferred"
    # A dollar cap reached still stops its self-started turns.
    p.db.x("UPDATE events SET status='handled'")
    p.set_config("budget.daily_usd", 1)
    d.cfg = p.config()
    d.update_gates()
    assert d.gates[core].level == "red", d.gates[core].as_dict()
    _event(p, clock, "task_done")
    assert not _turn_within(d, clock, starts, 600, step=30), "a self-started turn ran past the dollar cap"


def test_a_tripped_coordinator_turn_guard_spaces_out_self_started_turns_into_one_wake(env, monkeypatch):
    """While a coordinator turn signal is tripped (on a plan: too many self-started turns, or turns on the
    same evidence), self-started turns wait for its cool-down, and then one turn takes every event queued
    meanwhile. A user message or a must-act item still wakes the coordinator at once."""
    from ttp import obligations
    p, d, clock, starts = _batch_setup(env, monkeypatch, busy=0)
    d.update_gates()
    core = d.cfg.get("core_provider", "claude")
    hold = {"reasons": ["runaway guard: 4 self-started coordinator turns in the last hour met the same evidence"],
            "until": clock[0] + 1800}
    d.gates[core].coord_hold = hold
    assert d.gates[core].allow_new_work, d.gates[core].as_dict()
    _event(p, clock, "task_done")
    assert not _turn_within(d, clock, starts, 900, step=30), "a self-started turn ran inside the cool-down"
    _event(p, clock, "followup_proposed")
    assert not _turn_within(d, clock, starts, hold["until"] - clock[0] - 60, step=30)
    queued = [e["id"] for e in p.db.q("SELECT id FROM events WHERE status='queued'")]
    assert len(queued) == 2
    assert _turn_within(d, clock, starts, 300, step=30), "no turn once the cool-down ended"
    assert len(starts) == 1 and clock[0] >= hold["until"]
    # A user message wakes it inside a cool-down.
    clock[0] = hold["until"] - 1500
    p.db.x("UPDATE events SET status='handled'")
    n = len(starts)
    p.db.post("in", "what is the status?", chat="c1")
    assert _turn_within(d, clock, starts, 120, step=30) and len(starts) == n + 1, "the user's message got no turn"
    # So does a must-act item (an overdue promise).
    clock[0] += 120
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    _event(p, clock, obligations.PROMISE_KIND)
    assert clock[0] < hold["until"] and _turn_within(d, clock, starts, 120, step=30), "a must-act item got no turn"


def test_user_ordered_urgent_work_starts_in_burst_slots_in_one_tick_and_then_preempts_the_lowest_routine_run(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """The device-action stall: with every slot taken, a user-ordered urgent task (fresh `urgent:` line) starts
    in one of budget.urgent_extra_slots (2) burst slots at the same dispatch pass; with those full too, the
    lowest-priority routine run is stopped with STOP 'preempt' and requeued with no attempt spent."""
    from ttp import preempt
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    try:
        hi = p.db.add_task("routine p2", "s", kind="work", tier="light", origin="user", priority=2)
        lo = p.db.add_task("routine p3", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        assert _slot_tasks(p) == [hi, lo]
        hot = [p.db.add_task(f"device action {i}", "s\nurgent: the user ordered it now", kind="work", tier="light",
                             origin="user") for i in range(3)]
        d.dispatch()
        assert _slot_tasks(p) == [hi, lo] + hot[:2], "both burst slots taken in one pass"
        before = p.db.task(lo)["attempts"]
        d.dispatch()
        lo_run = p.db.one("SELECT * FROM runs WHERE task=? AND status='running'", (lo,))
        assert (pathlib.Path(lo_run["dir"]) / "STOP").read_text() == preempt.STOP_TEXT, "the lowest-priority run is stopped"
        assert not (pathlib.Path(p.db.one("SELECT dir FROM runs WHERE task=?", (hi,))["dir"]) / "STOP").exists()
        assert all(not (pathlib.Path(r["dir"]) / "STOP").exists() for r in p.db.q(
            "SELECT dir FROM runs WHERE task IN (?,?)", tuple(hot[:2]))), "urgent runs are never preempted"
        ev = p.db.q("SELECT * FROM events WHERE kind='task_preempted'")
        assert [e["task"] for e in ev] == [lo] and f"#{hot[2]}" in ev[0]["text"]
        d.dispatch()
        assert len(p.db.q("SELECT id FROM events WHERE kind='task_preempted'")) == 1, "a stop under way counts as freeing"
        deadline = time.monotonic() + 30
        while p.db.task(lo)["status"] == "running" and time.monotonic() < deadline:
            d.reap_runs()
            time.sleep(0.05)
        t = p.db.task(lo)
        assert t["status"] == "queued" and t["attempts"] == before, "requeued with no attempt spent"
        assert t["blocked_reason"] == "preempted for user-ordered urgent work; resumes"
        d.dispatch()
        assert sorted(_slot_tasks(p)) == sorted([hi] + hot), "the urgent task takes the freed slot, not the preempted one"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_the_urgent_burst_opens_at_an_orange_zero_slot_gate_but_never_at_red_and_preemption_can_be_turned_off(env):
    """_start_hold: a user-ordered urgent task takes the burst slots at an orange plan gate of 0 slots
    (critical work); red holds it; routine priority 1 never bursts. budget.urgent_preempt false: no stop."""
    import dataclasses
    from ttp import preempt
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    d = Daemon(p.base)
    d.cfg = p.config()
    d.update_gates()
    core = d.cfg.get("core_provider", "claude")
    hot = p.db.task(p.db.add_task("device action", "s\nurgent: the user ordered it", kind="work", tier="light",
                                  origin="user"))
    plain = p.db.task(p.db.add_task("routine p1", "s", kind="work", tier="light", origin="coordinator", priority=1))

    def hold(task, level, allow=True):
        d.gates[core] = dataclasses.replace(d.gates[core], level=level, max_parallel=0, allow_new_work=allow)
        return d._start_hold(task, core, {"now": time.time(), "paused": {}, "committed": None, "busy": {core: 0},
                                          "logged_out": {core: False}})
    assert hold(hot, "orange") is None, "orange 0-slot gate: the burst is open"
    assert hold(plain, "orange") == "gate", "priority 1 alone never bursts"
    assert hold(hot, "red") == "gate" and hold(hot, "red", allow=False) == "gate", "red holds it"
    assert preempt.enabled(d.cfg) and not preempt.enabled({"budget": {"urgent_preempt": False}})
    assert not preempt.enabled({"budget": {"urgent_preempt": "off"}})


def test_preemption_never_picks_urgent_work_a_review_a_harness_task_a_pushing_task_an_owner_or_a_lock_holder(env):
    from ttp import preempt
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")

    def running(title, kind="work", priority=3, status="running", spec="s"):
        tid = p.db.add_task(title, spec, kind=kind, tier="light", origin="user", priority=priority)
        p.db.update_task(tid, status=status)
        p.db.x("INSERT INTO runs(task, role, provider, status, started) VALUES(?,?,?,?,?)",
               (tid, "worker", "claude", "running", time.time() + tid))
        return tid, p.db.one("SELECT id FROM runs WHERE task=?", (tid,))["id"]
    spared = [running("urgent", spec="s\nurgent: ordered"), running("p1", priority=1), running("review", "review"),
              running("harness", "harness"), running("pushing", status="pushing")]
    owner = running("owner", priority=4)
    holder = running("lock holder", priority=5)
    p2 = running("routine p2", priority=2)
    p3 = running("routine p3", priority=3)
    held = {(str(holder[1]), "")}
    got = preempt.pick(p.db, "claude", {owner[0]: 0}, held)
    assert got["task"]["id"] == p3[0], "the lowest-priority eligible run"
    p.db.update_task(p3[0], status="done")
    assert preempt.pick(p.db, "claude", {owner[0]: 0}, held)["task"]["id"] == p2[0]
    assert preempt.pick(p.db, "claude", {owner[0]: 0}, held | {("", str(p2[0]))}) is None, \
        "a lock labelled with its task spares it; nothing else is eligible"
    assert preempt.pick(p.db, "claude", {}, set())["task"]["id"] == holder[0], "only its lock spared the p5 run"
    p.db.x("UPDATE tasks SET status='done' WHERE id IN (?,?,?)", (holder[0], owner[0], p2[0]))
    assert preempt.pick(p.db, "claude", {}, set()) is None, f"never one of {spared}"


def _closed_done(p, title, closes):
    """A task that handed off done `closes` times (task_done events), then put back in the queue by an
    unlogged write, as a stale row written back would."""
    tid = p.db.add_task(title, "s", kind="plan", tier="light", origin="user", priority=2)
    p.db.update_task(tid, status="done", attempts=closes, result=json.dumps({"status": "done", "summary": "done"}))
    for _ in range(closes):
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
               (time.time(), f"task:{tid}", "task_done", "normal", "done", "handled", tid))
    p.db.x("UPDATE tasks SET status='queued' WHERE id=?", (tid,))
    return tid


def test_a_done_task_queued_again_without_a_requeue_is_not_dispatched_past_its_attempt_limit(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """Regression (upstream fp 6c6d0d95639c46a6): a plan closed done again and again was put back in the
    queue by an unlogged write and redispatched each time (attempt 14 of 3). Dispatch refuses it, puts it
    back to done and says why; an explicit requeue runs it again."""
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    try:
        again = _closed_done(p, "a plan done three times", 3)
        once = _closed_done(p, "a plan done once", 1)   # under its limit: a rework re-queue still runs
        d.dispatch()
        assert _slot_tasks(p) == [once], "a task closed done as often as its attempt limit was started again"
        t = p.db.task(again)
        assert t["status"] == "done" and t["attempts"] == 3
        ev = p.db.one("SELECT text FROM events WHERE task=? AND kind='dispatch_refused'", (again,))
        assert ev and "closed done 3 times" in ev["text"] and "attempts 3 of 3" in ev["text"]
        assert f"task {again} not dispatched: it was already closed done 3 times" in \
            (p.logs / "daemon.log").read_text()
        settle(d)
        assert p.db.requeue(again, force=True) == ""   # the user's decision: its done count starts again
        assert p.db.one("SELECT id FROM events WHERE task=? AND kind='task_requeued'", (again,))
        d.dispatch()
        assert again in _slot_tasks(p), "an explicit requeue did not run the task again"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_task_cancelled_after_the_ready_read_is_not_started(env, monkeypatch, tmp_path, reap_tmp_processes):
    """Dispatch reads the ready list once per tick; a task cancelled or closed done after that read
    (a stale queued row in the list) is not started."""
    from ttp import db as dbmod
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    try:
        gone = p.db.add_task("cancelled after the read", "s", kind="work", tier="light", origin="user")
        done = p.db.add_task("done after the read", "s", kind="work", tier="light", origin="user")
        stale = [p.db.task(gone), p.db.task(done)]
        p.db.update_task(gone, status="cancelled")
        p.db.update_task(done, status="done")
        monkeypatch.setattr(dbmod.DB, "ready_tasks", lambda self, later=False: [dict(t) for t in stale])
        d.dispatch()
        assert _slot_tasks(p) == []
        assert (p.db.task(gone)["status"], p.db.task(done)["status"]) == ("cancelled", "done")
        log_text = (p.logs / "daemon.log").read_text()
        assert f"task {gone} not dispatched: it is cancelled now" in log_text
        assert f"task {done} not dispatched: it is done now" in log_text
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_recorded_requeue_restarts_the_done_count(env):
    """A recorded explicit requeue resets dispatch's done count: the guard then lets the task run."""
    from ttp import dispatchguard
    p = make(env)
    tid = _closed_done(p, "a chained code task", 3)
    assert dispatchguard.refusal(p.db, p.db.task(tid))
    dispatchguard.record_requeue(p.db, tid, "done", "continue")
    assert dispatchguard.done_closes(p.db, tid) == 0 and dispatchguard.refusal(p.db, p.db.task(tid)) == ""


def _requeues(p, tid):
    return p.db.q("SELECT source FROM events WHERE task=? AND kind='task_requeued'", (tid,))


def test_a_rejected_coordinator_requeue_records_nothing_and_an_accepted_one_does(env):
    """Review #7495: the coordinator's status queued logs its requeue only once the update is written. A
    rejected update (a start_when on a done task) leaves no task_requeued event, so the guard still
    refuses the task after an unlogged re-queue; an accepted status queued records one and the task runs."""
    from ttp import coordinator as coord, dispatchguard
    p = make(env)
    tid = _closed_done(p, "a plan done three times", 3)
    p.db.update_task(tid, status="done")
    assert coord.apply(p, [{"type": "task_update", "id": tid, "status": "queued", "start_when": "true"}])
    assert p.db.task(tid)["status"] == "done" and _requeues(p, tid) == []
    p.db.x("UPDATE tasks SET status='queued' WHERE id=?", (tid,))   # an unlogged write
    assert "closed done 3 times" in dispatchguard.refusal(p.db, p.db.task(tid))
    p.db.update_task(tid, status="done")
    assert coord.apply(p, [{"type": "task_update", "id": tid, "status": "queued"}]) == []
    assert p.db.task(tid)["status"] == "queued" and len(_requeues(p, tid)) == 1
    assert dispatchguard.refusal(p.db, p.db.task(tid)) == ""


def test_a_chain_continue_records_a_requeue(env):
    """chaincap.requeue (a continue hop) records task_requeued, so dispatch's done count restarts after it.
    The push queue's re-runs record theirs with their own text (push_cases)."""
    from ttp import chaincap, dispatchguard
    p = make(env)
    tid = _closed_done(p, "a chained code task", 3)
    p.db.update_task(tid, status="done", kind="code", branch="b")
    p.set_config("chains.requeue", True)
    assert chaincap.requeue(p, p.config(), p.db.task(tid), {"title": "continue: more", "spec": "x"}, "progress", 0)
    assert [r["source"] for r in _requeues(p, tid)] == ["continue"]
    assert dispatchguard.refusal(p.db, p.db.task(tid)) == ""


def test_a_plain_requeue_never_undoes_a_cancel_or_a_done_hand_off_or_passes_the_attempt_cap(env, capsys):
    """Regression (upstream fp af9f443ce59b93ba): a project's model-free watcher script ran
    `ttp task <project> requeue <id>` on a task its coordinator had cancelled, and the requeue counted as
    the user's decision, so the task ran again at attempt 37 of 3. A plain requeue (CLI or db) now leaves
    a cancelled or done task, or one that failed its last allowed attempt, as it is; `--force` (a
    deliberate decision) and a web app click run it again, on record with who did it."""
    from ttp import cli, dispatchguard, web
    p = make(env)

    def task(status, attempts=0):
        tid = p.db.add_task(f"{status} at {attempts}", "s", kind="work", tier="light", origin="user")
        p.db.x("UPDATE tasks SET status=?, attempts=? WHERE id=?", (status, attempts, tid))
        return tid

    cancelled, done, spent = task("cancelled", 37), task("done", 2), task("failed", 3)
    for tid in (cancelled, done, spent):   # what a watcher script runs: refused, the task left as it is
        before = p.db.task(tid)["status"]
        with pytest.raises(SystemExit):
            cli.main(["task", "demo", "requeue", str(tid)])
        assert p.db.task(tid)["status"] == before and not _requeues(p, tid), tid
        assert "--force" in capsys.readouterr().err
        assert "--force" in p.db.requeue(tid), "the db path the CLI and web app share refuses it too"
        assert dispatchguard.refusal(p.db, p.db.task(tid)), "never dispatched"
    # Work a script may still requeue: failed under its cap, blocked, waiting.
    for tid in (task("failed", 1), task("blocked", 5), task("waiting", 5)):
        cli.main(["task", "demo", "requeue", str(tid)])
        assert p.db.task(tid)["status"] == "queued", tid
        assert [r["source"] for r in _requeues(p, tid)] == ["requeue"]
    # A deliberate requeue: it runs again, and the event says it was forced.
    cli.main(["task", "demo", "requeue", str(cancelled), "--force"])
    assert p.db.task(cancelled)["status"] == "queued" and dispatchguard.refusal(p.db, p.db.task(cancelled)) == ""
    assert [r["source"] for r in _requeues(p, cancelled)] == ["requeue --force"]
    # The web app's Requeue is a click: deliberate.
    port = _start_web(p)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/task/{done}", method="POST",
                                 data=json.dumps({"status": "queued"}).encode(),
                                 headers={"X-TTP-Token": web.token(p), "Content-Type": "application/json"})
    for _ in range(50):
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                assert r.status == 200
            break
        except urllib.error.HTTPError:
            raise
        except OSError:
            time.sleep(0.1)
    assert p.db.task(done)["status"] == "queued"
    assert [r["source"] for r in _requeues(p, done)] == ["web --force"]


def test_a_task_whose_last_run_failed_at_its_attempt_cap_is_not_dispatched_until_a_requeue(env):
    """Regression (upstream fp af9f443ce59b93ba): the attempt cap holds at dispatch. A task whose last run
    failed at its attempt limit and that some write queued again with no explicit requeue is refused and
    put back to failed; under its cap, or after an explicit requeue, it may run."""
    from ttp import dispatchguard
    p = make(env)
    tid = p.db.add_task("flaky", "s", kind="work", tier="light", origin="user")
    now = time.time()
    p.db.x("INSERT INTO runs(task, role, provider, status, started, ended) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "claude", "failed", now - 60, now - 30))
    for attempts, refused in ((2, False), (3, True), (37, True)):
        p.db.x("UPDATE tasks SET status='queued', attempts=? WHERE id=?", (attempts, tid))
        why = dispatchguard.refusal(p.db, p.db.task(tid))
        assert bool(why) == refused and (not refused or f"attempts {attempts} of 3" in why), (attempts, why)
    dispatchguard.refuse(p.db, tid, dispatchguard.refusal(p.db, p.db.task(tid)))
    assert p.db.task(tid)["status"] == "failed"
    assert "requeue --force" in p.db.one("SELECT text FROM events WHERE task=? AND kind='dispatch_refused'", (tid,))["text"]
    assert p.db.requeue(tid, force=True) == ""
    assert dispatchguard.refusal(p.db, p.db.task(tid)) == "", "the deliberate requeue grants a run"


def test_a_cancelled_task_queued_again_without_a_requeue_is_not_dispatched(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """Regression (upstream fp aed18d8ef1b1ecc7): a task cancelled for good (`ttp task cancel`, or the
    coordinator's status cancelled) was put back in the queue by an unlogged write and dispatched at
    attempt 35 of 3. Dispatch refuses it and puts it back to cancelled; an explicit requeue runs it."""
    from ttp import coordinator as coord
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=3)
    try:
        by_cli = p.db.add_task("cancelled by the user", "s", kind="work", tier="light", origin="user")
        by_coord = p.db.add_task("cancelled by the coordinator", "s", kind="work", tier="light", origin="user")
        live = p.db.add_task("still wanted", "s", kind="work", tier="light", origin="user")
        assert p.db.cancel(by_cli) == ""
        assert coord.apply(p, [{"type": "task_update", "id": by_coord, "status": "cancelled"}]) == []
        for tid in (by_cli, by_coord):   # an unlogged write puts it back, far past its limit
            p.db.x("UPDATE tasks SET status='queued', attempts=35 WHERE id=?", (tid,))
        d.dispatch()
        assert _slot_tasks(p) == [live], "a cancelled task was started again"
        for tid in (by_cli, by_coord):
            assert p.db.task(tid)["status"] == "cancelled"
            ev = p.db.one("SELECT text FROM events WHERE task=? AND kind='dispatch_refused'", (tid,))
            assert ev and "cancelled and queued again" in ev["text"] and "attempts 35 of 3" in ev["text"]
        settle(d)
        assert p.db.requeue(by_cli, force=True) == ""   # the user's decision: it runs again
        d.dispatch()
        assert by_cli in _slot_tasks(p) and by_coord not in _slot_tasks(p)
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_done_task_with_a_start_when_probe_past_its_attempt_limit_is_not_redispatched(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """Regression (upstream fp ad88895e4aa49dda): a plan closed done (once by its last run, other closes
    leaving no task_done event) kept a start_when probe from an old machine hold and was queued again
    without a requeue, attempt 9 of 3. Its probe passing only makes it ready: dispatch refuses it, puts it
    back to done and says why; an explicit requeue runs it again."""
    from ttp.db import deferral
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    try:
        held = _closed_done(p, "a plan held on a machine probe", 1)
        now = time.time()
        p.db.x("INSERT INTO runs(task,role,started,ended,status) VALUES(?,?,?,?,?)",
               (held, "worker", now - 60, now - 1, "ok"))
        p.db.update_task(held, attempts=9, labels=["start_when:true", f"deferred_since:{now:.0f}"])
        fresh = _closed_done(p, "a plan done once, under its limit", 1)   # a rework re-queue still runs
        d._start_verdict(p.db.task(held), 0, time.time())   # its start_when passed
        assert not deferral(p.db.task(held))
        d.dispatch()
        assert _slot_tasks(p) == [fresh], "a done task past its attempt limit started again on its probe"
        t = p.db.task(held)
        assert (t["status"], t["attempts"]) == ("done", 9)
        ev = p.db.one("SELECT text FROM events WHERE task=? AND kind='dispatch_refused'", (held,))
        assert ev and "attempts 9 of 3" in ev["text"]
        settle(d)
        assert p.db.requeue(held, force=True) == ""   # the user's decision: it runs again
        d.dispatch()
        assert held in _slot_tasks(p), "an explicit requeue did not run the task again"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_run_that_queued_its_task_again_itself_is_not_refused_past_its_attempt_limit(env):
    """The attempts branch needs a done end: a last run whose end queued the task again with no task_done
    event after it (a push rework, a busy resource), or a done close followed by an explicit requeue, is
    left to run."""
    from ttp import dispatchguard
    p = make(env)
    tid = _closed_done(p, "a review re-run by the push queue", 1)
    p.db.update_task(tid, attempts=5)
    now = time.time()
    p.db.x("INSERT INTO runs(task,role,started,ended,status) VALUES(?,?,?,?,?)", (tid, "worker", now, now + 600, "ok"))
    assert dispatchguard.refusal(p.db, p.db.task(tid)) == "", "a later run's own requeue was refused"
    p.db.x("UPDATE runs SET ended=? WHERE task=?", (now - 600, tid))
    assert "attempts 5 of 3" in dispatchguard.refusal(p.db, p.db.task(tid))
    dispatchguard.record_requeue(p.db, tid, "done", "requeue")
    assert dispatchguard.refusal(p.db, p.db.task(tid)) == ""


def _branch_calls(env, n):
    """A project with `n` finished code-task branches, each committing pkg/lib.py, and a queued code task
    whose spec names that file: (project, queued task id)."""
    return _branch_calls_named(env, n, "demo")


def _branch_calls_named(env, n, name):
    p = make(env, name) if name == "demo" else _second_project(env, name)
    (p.root / "pkg").mkdir(exist_ok=True)
    (p.root / "pkg/lib.py").write_text("x = 0\n")
    _git_out(p.root, "add", "pkg/lib.py")
    _git_out(p.root, "commit", "-qm", "lib")
    for i in range(n):
        _tid, path, _branch = _code_task(p, f"done {i}")
        (path / "pkg/lib.py").write_text(f"x = {i + 1}\n")
        _git_out(path, "add", "pkg/lib.py")
        _git_out(path, "commit", "-qm", f"lib {i}")
    return p, p.db.add_task("Change lib", "Edit pkg/lib.py so it reads y.", kind="code", tier="light",
                            origin="user")


def test_a_workers_start_reads_no_git_per_unlanded_branch(env, monkeypatch):
    """The start-time branch check (overlap.prompt_block) once ran two or more git calls per finished
    branch at every worker start (~230 branches on one project): heads found not on the push branch are
    kept for its tip, so a later start runs a fixed few git calls however many branches there are."""
    from ttp import gitmemo, landed, overlap
    p, q = _branch_calls(env, 12)
    p.set_config("delivery.push_branch", "t79-push")
    push_ref = landed._push_ref(p, p.root)
    _git_out(p.root, "update-ref", push_ref, "HEAD")   # the push branch at the base, as on a project that pushed
    calls = []
    real = gitmemo.run
    monkeypatch.setattr(gitmemo, "run", lambda argv, *a, **k: calls.append(argv) or real(argv, *a, **k))
    first = overlap.prompt_block(p, p.db.task(q))
    cold = len(calls)
    calls.clear()
    again = overlap.prompt_block(p, p.db.task(q))
    assert again == first and again.count("\n- #") == overlap.MAX_BRANCHES, again
    assert cold >= 24 and len(calls) <= 8, (cold, [" ".join(c[3:]) for c in calls])
    # A new commit on the push branch's tip touching none of their files: the heads found off stay off,
    # three batched reads for all of them instead of each branch's landed reads.
    (p.root / "other.txt").write_text("o\n")
    _git_out(p.root, "add", "other.txt")
    _git_out(p.root, "commit", "-qm", "other")
    _git_out(p.root, "update-ref", push_ref, "HEAD")
    calls.clear()
    assert overlap.prompt_block(p, p.db.task(q)) == first
    assert len(calls) <= 11, [" ".join(c[3:]) for c in calls]
    calls.clear()
    overlap.prompt_block(p, p.db.task(q))
    assert len(calls) <= 8, calls
    # One branch lands as a rebased copy (same author, date and subject, same file): every head touching
    # that file is read again, and that one is found on the push branch.
    sha = next(ln.split()[0] for ln in _git_out(p.root, "log", "--all", "--format=%H %s").splitlines()
               if ln.endswith(" lib 0"))
    branch = _git_out(p.root, "branch", "--contains", sha, "--format=%(refname:short)").split()[0]
    _git_out(p.root, "cherry-pick", sha)
    _git_out(p.root, "update-ref", push_ref, "HEAD")
    off = overlap._load_off(p)
    assert off["prev"] and overlap._carried(p, off) == set()
    assert overlap._on_push_branch(p, {"open": False, "head": sha, "branch": branch}, [], off)


def test_the_first_start_after_a_tip_move_reads_the_same_git_for_4_or_12_branches(env, monkeypatch):
    """Review #246: the carried heads were cached only inside a gitmemo scope, so a start outside one
    (the worker-start prompt runs under a dispatch pass) reread them for every branch: the git count
    after a tip move grew with the branch count. It is now read once per prompt build."""
    from ttp import gitmemo, landed, overlap
    counts = []
    for n in (4, 12):
        p, q = _branch_calls(env, n) if n == 4 else _branch_calls_named(env, n, f"demo{n}")
        p.set_config("delivery.push_branch", "t79-push")
        push_ref = landed._push_ref(p, p.root)
        _git_out(p.root, "update-ref", push_ref, "HEAD")
        overlap.prompt_block(p, p.db.task(q))
        (p.root / f"other{n}.txt").write_text("o\n")
        _git_out(p.root, "add", f"other{n}.txt")
        _git_out(p.root, "commit", "-qm", "other")
        _git_out(p.root, "update-ref", push_ref, "HEAD")
        calls = []
        real = gitmemo.run
        monkeypatch.setattr(gitmemo, "run", lambda argv, *a, **k: calls.append(argv) or real(argv, *a, **k))
        overlap.prompt_block(p, p.db.task(q))
        monkeypatch.setattr(gitmemo, "run", real)
        counts.append(len(calls))
    assert counts[0] == counts[1] and counts[1] <= 11, counts


def test_slow_git_dispatch_starts_every_ready_task_in_one_step(env, monkeypatch, tmp_path, reap_tmp_processes):
    """On a loaded machine each git call is slow; each start re-read the root checkout's head, branch and
    status and fetched origin, so a dispatch step's budget ran out after a start or two and the rest
    waited a tick each. Within one dispatch pass those reads and the fetch run once: 4 ready tasks and
    4 free places all start within one step budget, the fetch and root reads run once."""
    from ttp import stepclock
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=4)
    try:
        for i in range(4):
            p.db.add_task(f"urgent code {i}", "s", kind="code", tier="light", origin="user", priority=1)
        calls = _slow_git(tmp_path, monkeypatch, 0.4)
        monkeypatch.setattr(d, "_step_budget", lambda: 36.0)
        stepclock.install()
        try:
            d._run_step(d.dispatch)
        finally:
            stepclock.uninstall()
        assert sorted(_slot_tasks(p)) == [1, 2, 3, 4], "every ready task starts within one step"
        said = [" ".join(line.split()[1:]) for line in calls.read_text().splitlines()]
        root = f"-C {p.root} "
        for read in ("fetch --quiet origin", "status --porcelain=v1", "symbolic-ref --quiet --short refs/remotes/origin/HEAD"):
            assert sum(c.startswith(root + read) for c in said) == 1, (read, said)
    finally:
        drain(d)


def test_slow_diff_reads_never_hold_ready_reviews_past_one_step(env, monkeypatch, tmp_path, reap_tmp_processes):
    """The 2026-11-10 stall: each ready review's tier pick read its branch's `diff --numstat`, and on a
    loaded machine one took most of the step budget, so the step stopped with urgent reviews and free
    places left. A diff is now measured only while half the budget is left; later reviews start at their
    given tier. Counted, not timed: one numstat runs and all four start (before: the step stopped first)."""
    from ttp import stepclock, worktree
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=4)
    try:
        g = lambda *a: subprocess.run(["git", "-C", str(p.root), *a], check=True, capture_output=True,  # noqa: E731
                                      text=True).stdout.strip()
        start = g("rev-parse", "HEAD")
        for i in range(4):
            g("checkout", "-q", "-b", f"ttp/t{90 + i}-work", start)
            (p.root / f"w{i}.py").write_text("x = 1\n" * (i + 1))
            g("add", f"w{i}.py")
            g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"work {i}")
            sha = g("rev-parse", "HEAD")
            g("checkout", "-q", "-")
            p.db.add_task(f"Review #{90 + i}", f"Review branch ttp/t{90 + i}-work, head {sha}.", kind="review",
                          tier="standard", origin="user", priority=1)
        getattr(worktree, "_NUMSTAT", {}).clear()
        calls = _slow_git(tmp_path, monkeypatch, 0.05, slow="*diff\\ --numstat*", slow_s=24)
        monkeypatch.setattr(d, "_step_budget", lambda: 40.0)
        stepclock.install()
        try:
            d._run_step(d.dispatch)
        finally:
            stepclock.uninstall()
        said = calls.read_text()
        assert said.count("diff --numstat") == 1, said
        assert len(_slot_tasks(p)) == 4, "every ready review starts within one step"
    finally:
        drain(d)


def test_diff_lines_reads_a_head_pair_once(env, monkeypatch, tmp_path):
    """worktree.diff_lines keeps each (base commit, ref commit) numstat across calls: a review retried or
    picked again on a later pass pays a slow diff once per head; a new head is read afresh."""
    from ttp import worktree
    p = make(env)
    g = lambda *a: subprocess.run(["git", "-C", str(p.root), *a], check=True, capture_output=True,  # noqa: E731
                                  text=True).stdout.strip()
    base = g("rev-parse", "HEAD")
    g("checkout", "-q", "-b", "feat")
    (p.root / "f.py").write_text("a\n")
    g("add", "f.py")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "f")
    g("checkout", "-q", "-")
    worktree._NUMSTAT.clear()
    calls = _slow_git(tmp_path, monkeypatch, 0)
    n = lambda: calls.read_text().count("diff --numstat")  # noqa: E731
    assert worktree.diff_lines(p, ["feat"], base=base) == {"f.py": 1}
    assert worktree.diff_lines(p, ["feat"], base=base) == {"f.py": 1} and n() == 1
    g("checkout", "-q", "feat")
    (p.root / "f.py").write_text("a\nb\n")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "g")
    g("checkout", "-q", "-")
    assert worktree.diff_lines(p, ["feat"], base=base) == {"f.py": 2} and n() == 2


def test_dispatch_pass_replays_a_failed_fetch(tmp_path, monkeypatch):
    """A second fetch in a pass gets the first one's outcome: a failure's returncode (check=True raises)
    and a timeout's TimeoutExpired, never a made-up success."""
    from ttp import gitmemo
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    calls = _slow_git(tmp_path, monkeypatch, 0)
    fetch = ["git", "-C", str(repo), "fetch", "--quiet", "origin"]
    with gitmemo.dispatch_pass():
        first = gitmemo.run(fetch, capture_output=True, text=True)
        assert first.returncode != 0
        assert gitmemo.run(fetch, capture_output=True, text=True).returncode == first.returncode
        with pytest.raises(subprocess.CalledProcessError):
            gitmemo.run(fetch, capture_output=True, text=True, check=True)
    assert calls.read_text().count("fetch --quiet origin") == 1
    real = gitmemo._run
    monkeypatch.setattr(gitmemo, "_run", lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 1)))
    with gitmemo.dispatch_pass():
        for _ in range(2):
            with pytest.raises(subprocess.TimeoutExpired):
                gitmemo.run(fetch, capture_output=True)
        monkeypatch.setattr(gitmemo, "_run", real)
        with pytest.raises(subprocess.TimeoutExpired):
            gitmemo.run(fetch, capture_output=True)


def test_dispatch_pass_memo_keeps_root_reads_until_a_move(tmp_path, monkeypatch):
    """gitmemo.dispatch_pass(): a root read and the fetch run once; a checkout move drops the kept reads;
    a kept failing read still raises under check=True; outside a pass nothing is kept."""
    from ttp import gitmemo
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "a"], check=True)
    calls = _slow_git(tmp_path, monkeypatch, 0)
    head = ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"]
    n = lambda s: sum(s in line for line in calls.read_text().splitlines()) if calls.exists() else 0
    with gitmemo.dispatch_pass():
        for _ in range(2):
            gitmemo.run(["git", "-C", str(repo), "fetch", "--quiet", "origin"], capture_output=True)
        assert n("fetch --quiet origin") == 1, "a failed fetch is not retried within the pass either"
        assert gitmemo.run(head, capture_output=True, text=True).stdout.strip() == "main"
        assert gitmemo.run(head, capture_output=True, text=True).stdout.strip() == "main"
        assert n("symbolic-ref --quiet --short HEAD") == 1
        gitmemo.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"], capture_output=True)
        gitmemo.run(["git", "-C", str(repo), "update-ref", "refs/ttp/fork-point/t1", "HEAD"], check=True)
        gitmemo.run(head, capture_output=True, text=True)
        assert n("symbolic-ref --quiet --short HEAD") == 1, "a list or a harness ref leaves the root's head"
        gitmemo.run(["git", "-C", str(repo), "checkout", "-q", "-b", "other"], check=True)
        assert gitmemo.run(head, capture_output=True, text=True).stdout.strip() == "other"
        assert n("symbolic-ref --quiet --short HEAD") == 2
        gitmemo.run(["git", "-C", str(repo), "checkout", "-q", "--detach"], check=True)
        for _ in range(2):
            with pytest.raises(subprocess.CalledProcessError):
                gitmemo.run(head, capture_output=True, text=True, check=True)
        assert n("symbolic-ref --quiet --short HEAD") == 3
    gitmemo.run(head, capture_output=True, text=True)
    gitmemo.run(head, capture_output=True, text=True)
    assert n("symbolic-ref --quiet --short HEAD") == 5, "outside a pass every read runs"


def test_dispatch_holds_a_duplicate_of_an_open_or_recently_done_task_once(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """User msg #2495 (waste): one project ran 27 runs on duplicate same-title tasks. Just before dispatch, a
    task repeating an earlier open task, or one done in the last 14 days (same title, or task_add's
    looks_same), is blocked with one dispatch_held event and nothing runs; the original starts. Its own
    continue lineage, a `differs from #<id>` spec and an explicit requeue after the hold all run."""
    from ttp import supersede
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=4)
    try:
        first = p.db.add_task("deploy the relay fix", "roll it out", kind="work", tier="light")
        dup = p.db.add_task("Deploy the relay fix", "roll it out again", kind="work", tier="light")
        old = p.db.add_task("rebuild the wheel cache", "s", kind="work", tier="light", status="done")
        hop = p.db.add_task("rebuild the wheel cache", "s", kind="work", tier="light", labels=[f"continues:{old}"])
        old2 = p.db.add_task("prune the docs index", "s", kind="work", tier="light", status="done")
        forced = p.db.add_task("prune the docs index", f"differs from #{old2}: other target", kind="work",
                               tier="light")
        redo = p.db.add_task("prune the docs index again", "s", kind="work", tier="light")
        d.dispatch()
        assert sorted(_slot_tasks(p)) == sorted([first, hop, forced])
        for tid, orig in ((dup, first), (redo, old2)):
            t = p.db.task(tid)
            assert t["status"] == "blocked" and f"duplicates #{orig}" in t["blocked_reason"]
            (ev,) = p.db.q("SELECT * FROM events WHERE task=? AND kind=?", (tid, supersede.HELD))
            assert ev["status"] == "queued" and "Nothing ran" in ev["text"]
        settle(d)
        assert p.db.requeue(redo) == ""   # the coordinator looked: it runs, and no second event
        d.dispatch()
        assert redo in _slot_tasks(p)
        assert len(p.db.q("SELECT 1 FROM events WHERE task=? AND kind=?", (redo, supersede.HELD))) == 1
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_dispatch_holds_daemon_reviews_of_one_unchanged_subject_but_not_a_re_review(env):
    """A second review of the same code task is a duplicate while its subject is unchanged since the first;
    a re-review (other numbers in its title) and a review of another subject are not."""
    from ttp import supersede
    p = make(env)
    code = p.db.add_task("land the gate", "s", kind="code", status="done")
    other = p.db.add_task("land the other gate", "s", kind="code", status="done")
    r1 = p.db.add_task(f"Review #{code}: land the gate", "s", kind="review", labels=[f"auto_review:{code}"])
    r2 = p.db.add_task(f"Review #{code}: land the gate", "s", kind="review", labels=[f"auto_review:{code}"])
    rr = p.db.add_task(f"Re-review #{r1}: Review #{code}: land the gate", "s", kind="review",
                       labels=[f"auto_review:{code}", f"continues:{r1}"])
    ro = p.db.add_task(f"Review #{other}: land the other gate", "s", kind="review", labels=[f"auto_review:{other}"])
    assert f"duplicates #{r1}" in supersede.dispatch_hold(p.db, p.db.task(r2))
    assert supersede.dispatch_hold(p.db, p.db.task(rr)) == ""
    assert supersede.dispatch_hold(p.db, p.db.task(ro)) == ""
    p.db.update_task(r1, status="done")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() + 5, code))   # the subject changed since
    supersede._clear.clear()
    assert supersede.dispatch_hold(p.db, p.db.task(r2)) == ""


def test_dispatch_never_holds_owners_of_different_obligations_as_duplicates(env):
    """The owner note the harness appends to each owner's spec reads alike for any two obligations, so two owners
    of different obligations looked like one task and the second was held. Owning other obligations is other
    work; a same-title task owing the same obligation, or none, is still held."""
    from ttp import supersede
    p = make(env)
    note = "\n\n## Obligation {} (urgent, owed to the user)\nship the deliverable\nRecord progress every 2 h.\n"
    a = p.db.add_task("ship the relay deliverable", "build it" + note.format("o1"), kind="work")
    b = p.db.add_task("ship the relay deliverable", "build it" + note.format("o2"), kind="work")
    c = p.db.add_task("ship the relay deliverable", "build it" + note.format("o1"), kind="work")
    d = p.db.add_task("ship the relay deliverable", "build it", kind="work")
    assert supersede.dispatch_hold(p.db, p.db.task(b)) == ""
    assert f"duplicates #{a}" in supersede.dispatch_hold(p.db, p.db.task(c))
    assert "duplicates #" in supersede.dispatch_hold(p.db, p.db.task(d))


def test_dispatch_holds_an_open_review_only_of_the_same_subject_and_target(env):
    """Two open reviews of one code task are duplicates only while their specs name the same other-repository
    heads and packets (path + sha256); a done one's exact repeat is left to verdict reuse at start."""
    from ttp import supersede
    p = make(env)
    code = p.db.add_task("land the gate", "s", kind="code", status="done")
    sums = ("ab" * 32, "cd" * 32)

    def review(spec):
        supersede._clear.clear()
        return p.db.add_task(f"Review #{code}: land the gate", spec, kind="review", labels=[f"auto_review:{code}"])
    first = review(f"Review the packet reviews/p.md sha256 {sums[0]}.")
    assert supersede.dispatch_hold(p.db, p.db.task(review(f"Review the packet reviews/p.md sha256 {sums[1]}."))) == ""
    assert supersede.dispatch_hold(p.db, p.db.task(review("Review repo other/b, head " + "e" * 40 + "."))) == ""
    same = review(f"Review the packet reviews/p.md sha256 {sums[0]} again.")
    assert f"duplicates #{first}" in supersede.dispatch_hold(p.db, p.db.task(same))
    p.db.update_task(first, status="done")
    p.db.update_task(same, status="cancelled")
    assert supersede.dispatch_hold(p.db, p.db.task(review(f"Review reviews/p.md sha256 {sums[0]}."))) == ""


def test_dispatch_holds_a_task_whose_step_a_newer_user_order_superseded(env):
    """An obligation's `supersedes` steps are kept with the order: a queued task created before it whose spec
    still carries such a step is held at dispatch; one re-specified after the order, or created after it, runs."""
    from ttp import coordinator as coord, supersede
    p = make(env)
    stale = p.db.add_task("tune the cache", "Step 1: restart the relay service nightly.\nStep 2: report.",
                          kind="work", tier="light")
    respecced = p.db.add_task("tune the store", "Restart the relay service nightly, then report.",
                              kind="work", tier="light")
    p.db.x("UPDATE tasks SET created=created-60 WHERE id IN (?,?)", (stale, respecced))
    mid = p.db.x("INSERT INTO messages(ts,chat,direction,text) VALUES(?,?,?,?)",
                 (time.time() - 30, "c", "in", "stop the nightly restarts"))
    errs = coord.apply(p, [{"type": "obligation", "order": mid, "text": "no nightly restarts", "action_class": "change",
                            "supersedes": ["restart the relay service nightly"]}])
    assert errs == [], errs
    coord.apply(p, [{"type": "task_update", "id": respecced, "spec": "and keep it short"}])
    later = p.db.add_task("tune the index", "restart the relay service nightly", kind="work", tier="light")
    why = supersede.dispatch_hold(p.db, p.db.task(stale))
    assert f"user msg #{mid} superseded" in why
    assert supersede.dispatch_hold(p.db, p.db.task(respecced)) == ""
    assert supersede.dispatch_hold(p.db, p.db.task(later)) == ""


def test_a_running_task_is_stopped_once_superseded_and_its_work_kept(env, tmp_path):
    """A running task is cancelled and its run asked to stop (STOP) within one pass once its change landed
    through another task, a newer task continues it, or every obligation it owns was dropped; one
    superseded_stopped event says why, and its run dir stays. A running task with none of these keeps running."""
    from ttp import daemon as dm, obligations, supersede
    p = make(env)
    d = dm.Daemon(p.base)
    now = time.time()
    ids = {}
    for name in ("landed", "continued", "dropped", "kept"):
        tid = p.db.add_task(f"work {name}", "s", kind="code", status="running")
        run_dir = tmp_path / f"run-{name}"
        run_dir.mkdir()
        p.db.x("INSERT INTO runs(task,role,started,status,dir) VALUES(?,?,?,?,?)",
               (tid, "worker", now - 100, "running", str(run_dir)))
        ids[name] = (tid, run_dir)
    p.db.x("INSERT INTO task_superseded(task,head,sha,ts) VALUES(?,?,?,?)",   # `ttp task superseded`
           (ids["landed"][0], "abc123", "def4567890", now))
    p.db.add_task("continue: work continued", "s", kind="code", labels=[f"continues:{ids['continued'][0]}"])
    st = obligations._load(p.db)
    st["items"]["o1"] = {"id": "o1", "text": "x", "owners": [str(ids["dropped"][0])], "receipt": None,
                         "dropped": {"at": now, "by": "coordinator", "why": "no longer wanted"}}
    p.db.set_kv(obligations.KV, st)
    d.stop_superseded()
    for name in ("landed", "continued", "dropped"):
        tid, run_dir = ids[name]
        assert p.db.task(tid)["status"] == "cancelled", name
        assert (run_dir / "STOP").exists() and run_dir.is_dir(), name
        (ev,) = p.db.q("SELECT * FROM events WHERE task=? AND kind=?", (tid, supersede.STOPPED))
        assert ev["status"] == "queued" and "worktree and branch are kept" in ev["text"]
    assert "landed through another commit" in p.db.one("SELECT text FROM events WHERE task=? AND kind=?",
                                                       (ids["landed"][0], supersede.STOPPED))["text"]
    tid, run_dir = ids["kept"]
    assert p.db.task(tid)["status"] == "running" and not (run_dir / "STOP").exists()


def test_dispatch_never_holds_a_recurring_schedule_run_as_a_duplicate_of_its_last(env):
    """Each run of a schedule repeats its last one's title by design: the next run, after a done or beside an
    open one, is never held, and a coordinator task is not held for matching a schedule run. A second daemon
    fix of one review is still held."""
    from ttp import supersede
    p = make(env)
    for first in ("done", "running"):
        a = p.db.add_task("[daily-review] daily review", "evidence 1", kind="work", status=first, origin="schedule")
        b = p.db.add_task("[daily-review] daily review", "evidence 2", kind="work", origin="schedule")
        assert supersede.dispatch_hold(p.db, p.db.task(b)) == "", first
        c = p.db.add_task("[daily-review] daily review", "evidence 3", kind="work")
        assert supersede.dispatch_hold(p.db, p.db.task(c)) == "", first
        for t in (a, b, c):
            p.db.x("UPDATE tasks SET status='cancelled' WHERE id=?", (t,))
    f1 = p.db.add_task("Fix review #5: code: tidy the relay", "fix the findings", kind="code", origin="daemon")
    f2 = p.db.add_task("Fix review #5: code: tidy the relay", "fix the findings", kind="code", origin="daemon")
    assert f"duplicates #{f1}" in supersede.dispatch_hold(p.db, p.db.task(f2))


def test_a_landing_record_from_before_a_run_began_never_stops_it(env, tmp_path):
    """A done task with a `ttp task superseded` record, requeued afterwards, keeps its new run."""
    from ttp import daemon as dm, supersede
    p = make(env)
    d = dm.Daemon(p.base)
    now = time.time()
    tid = p.db.add_task("work requeued", "s", kind="code", status="running")
    p.db.x("INSERT INTO task_superseded(task,head,sha,ts) VALUES(?,?,?,?)", (tid, "abc123", "def4567890", now - 500))
    p.db.x("INSERT INTO runs(task,role,started,status,dir) VALUES(?,?,?,?,?)",
           (tid, "worker", now - 100, "running", str(tmp_path)))
    d.stop_superseded()
    assert p.db.task(tid)["status"] == "running" and not (tmp_path / "STOP").exists()
    assert not p.db.q("SELECT 1 FROM events WHERE task=? AND kind=?", (tid, supersede.STOPPED))


def _ended_runs(p, tid, n, cost=0.5, status="error", handoff=None):
    """`n` ended worker runs of task `tid`, each spending `cost`, with `handoff` (a result.json) or none."""
    for i in range(n):
        run_dir = p.base / f"run-{tid}-{time.time_ns()}"
        run_dir.mkdir()
        if handoff:
            (run_dir / "result.json").write_text(json.dumps(handoff))
        p.db.x("INSERT INTO runs(task,role,provider,effort,status,started,ended,cost_usd,dir,note) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)", (tid, "worker", "claude", "high", status, time.time(),
                                               time.time() + 1, cost, str(run_dir), json.dumps({"error": f"boom {i}"})))


def test_a_task_at_its_run_cap_is_blocked_and_the_coordinator_told_once(env, monkeypatch):
    """Upstream user msg #2495 (2a): a task the daemon re-ran 4 times (tier-up retries, restarts) is not
    dispatched a fifth time. It is blocked, with one run_cap event carrying a model-free summary of each
    run and the last error; an explicit requeue starts the count again, and chains.max_task_runs 0 turns
    the cap off. Runs that never reached the model do not count."""
    from ttp import dispatchguard, runcap
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    tid = p.db.add_task("loops forever", "s", kind="work", tier="light", origin="user", provider="claude")
    _ended_runs(p, tid, 3, handoff={"status": "waiting", "summary": "tried a flag"})
    _ended_runs(p, tid, 2, cost=0, status="limit")   # free ends: not runs here
    d, started = _reuse_daemon(p, monkeypatch)
    d.dispatch()
    assert started == [tid], "3 runs: under the cap"
    p.db.update_task(tid, status="queued")
    _ended_runs(p, tid, 1, handoff={"status": "waiting", "summary": "tried a flag again"})
    d.dispatch()
    d.dispatch()
    t = p.db.task(tid)
    assert started == [tid] and t["status"] == "blocked" and "max_task_runs 4" in t["blocked_reason"]
    ev = p.db.q("SELECT * FROM events WHERE kind=?", (runcap.RUN_CAP_EVENT,))
    assert len(ev) == 1 and ev[0]["task"] == tid
    assert "4 runs, $2.00" in ev[0]["text"] and "tried a flag again" in ev[0]["text"]
    assert "no same-approach retry" in ev[0]["text"]
    dispatchguard.record_requeue(p.db, tid, "blocked", "coordinator")
    p.db.update_task(tid, status="queued")
    d.dispatch()
    assert started == [tid, tid], "an explicit requeue starts the count again"
    p.db.update_task(tid, status="queued")
    time.sleep(0.01)
    _ended_runs(p, tid, 4, cost=0.1, handoff={"status": "waiting"})
    assert runcap.check(p, p.config(), p.db.task(tid))
    p.db.update_task(tid, status="queued")
    p.set_config("chains.max_task_runs", 0)
    assert runcap.check(p, p.config(), p.db.task(tid)) == "", "0 turns the cap off"


def test_the_daemons_and_push_queues_own_requeues_never_restart_the_run_count(env):
    """Review #8098: only an explicit requeue (the coordinator, the user, a chain continue) restarts the
    run count. The daemon's requeue of a lost run and the push queue's rework requeue write the same
    event kind; a crash or rework loop that reset the count on them would never reach the cap."""
    from ttp import dispatchguard, pushq, runcap
    p = make(env)
    tid = p.db.add_task("crash loop", "s", kind="work", tier="light", origin="user")
    _ended_runs(p, tid, 4, cost=0.1, handoff={"status": "waiting"})
    time.sleep(0.01)
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
           (time.time(), "daemon", "task_requeued", "low", f"#{tid} was marked running with no live run",
            "handled", tid))
    pushq._event(p.db, p.db.task(tid), "task_requeued", f"#{tid}: its review runs again", queued=False)
    assert "max_task_runs 4" in runcap.check(p, p.config(), p.db.task(tid)), "automatic requeues count on"
    time.sleep(0.01)
    dispatchguard.record_requeue(p.db, tid, "blocked", "coordinator")
    p.db.update_task(tid, status="queued")
    assert runcap.check(p, p.config(), p.db.task(tid)) == "", "the coordinator's requeue starts it again"


def test_a_task_whose_last_runs_spent_money_with_no_progress_is_blocked(env):
    """Upstream user msg #2495 (2a): two runs in a row that each spent money and handed off neither done
    nor waiting (and, for code, committed nothing) stop the task under the run cap; a waiting hand-off is
    progress, and chains.max_idle_runs 0 turns the check off."""
    from ttp import runcap
    p = make(env)
    p.set_config("chains.max_task_runs", 0)   # the idle check alone
    tid = p.db.add_task("spins", "s", kind="work", tier="light", origin="user")
    _ended_runs(p, tid, 1)
    _ended_runs(p, tid, 1, handoff={"status": "waiting", "summary": "checks detached"})
    assert runcap.check(p, p.config(), p.db.task(tid)) == "", "the last run made progress"
    _ended_runs(p, tid, 1)
    assert runcap.check(p, p.config(), p.db.task(tid)) == "", "one idle run is not a loop"
    _ended_runs(p, tid, 1, handoff={"status": "failed", "summary": "same error"})
    p.set_config("chains.max_idle_runs", 0)
    assert runcap.check(p, p.config(), p.db.task(tid)) == ""
    p.set_config("chains.max_idle_runs", 2)
    why = runcap.check(p, p.config(), p.db.task(tid))
    assert "last 2 runs spent $1.00 with no hand-off" in why and p.db.task(tid)["status"] == "blocked"


@pytest.mark.parametrize("at", ["before_spawn", "after_record"])
def test_a_step_stop_in_a_worker_start_never_leaves_its_task_running_with_no_run(
        env, monkeypatch, tmp_path, reap_tmp_processes, at):
    """The step's stop (StepBudgetSpent, no Exception) landing in start_run before the runner spawned puts
    the task back to queued; one landing after the run was recorded keeps the task running with its run."""
    from ttp import stepclock
    from ttp.daemon import Daemon
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    tid = p.db.add_task("one", "s", kind="work", tier="light", origin="user")
    real = Daemon.start_run

    def start_run(self, *a, **k):
        if at == "before_spawn":
            stepclock._on_alarm(signal.SIGALRM, None)
        rid = real(self, *a, **k)
        stepclock._on_alarm(signal.SIGALRM, None)
        return rid
    monkeypatch.setattr(Daemon, "start_run", start_run)
    monkeypatch.setattr(stepclock, "_arm", lambda t: None)   # no real SIGALRM: the test places the stop
    try:
        stepclock.begin("dispatch", 600, 5)
        try:
            with pytest.raises(stepclock.StepBudgetSpent):
                d.dispatch()
        finally:
            stepclock.end()
        status = p.db.task(tid)["status"]
        if at == "before_spawn":
            assert status == "queued" and not _slot_tasks(p), status
        else:
            assert status == "running" and _slot_tasks(p) == [tid], status
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"
