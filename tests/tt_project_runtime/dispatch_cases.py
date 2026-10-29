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


def test_dispatch_starts_urgent_then_obligation_owners_first_and_never_past_max_parallel(
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
        assert [t["id"] for t in p.db.ready_tasks()] == [plain_p1, early_owner, late_owner, old_p3]
        d.dispatch()
        assert _slot_tasks(p) == [plain_p1, early_owner], "urgent first, then the earliest due obligation owner"
        d.dispatch()
        assert len(_slot_tasks(p)) == 2, "a second pass started past max_parallel"
        settle(d)
        d.dispatch()
        assert _slot_tasks(p) == [late_owner, old_p3], "then the later owner before the older priority 3"
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
    assert json.loads(t["result"])["status"] == "waiting" and t["not_before"] > time.time() + 3000, t


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


def test_claude_workers_get_a_tool_allowlist_with_web_tools_only_where_the_task_needs_them(env, monkeypatch, tmp_path):
    """Workers and reviewers on Claude run with --tools (the built-in tools workers use, Skill for the
    plugin skills, ToolSearch so MCP tools stay deferred) instead of the long denylist. The web tools
    come with every kind but a review; a review gets them only when its spec links a page off GitHub."""
    from ttp.providers import claude
    lean = ["Bash", "Read", "Edit", "Write", "Skill", "ToolSearch"]
    p = make(env)
    # Default worker (a code task): the allowlist with the web tools, and no built-in denylist.
    argv, _, _ = _dispatch_one(p, monkeypatch, {}, kind="code")
    assert _claude_tools(argv) == [*lean, "WebFetch", "WebSearch"]
    assert argv.count("--tools") == 1 and "--disallowedTools" not in argv, "nothing left to deny"
    assert argv[argv.index("--tools") + 2].startswith("--"), "--tools takes several values: a flag must follow"
    # Review: no web tools.
    argv, run_dir, _ = _dispatch_one(p, monkeypatch, {}, kind="review")
    assert p.db.one("SELECT role FROM runs WHERE dir=?", (str(run_dir),))["role"] == "reviewer"
    assert _claude_tools(argv) == lean
    argv, _, _ = _dispatch_one(p, monkeypatch, {}, kind="review",
                                         spec="Check https://github.com/o/r/pull/1 and https://api.github.com/x")
    assert _claude_tools(argv) == lean, "GitHub links are read with gh"
    # A web-needing review: its spec links a page elsewhere.
    argv, _, _ = _dispatch_one(p, monkeypatch, {}, kind="review",
                                         spec="Check the flag against https://docs.example.com/cli")
    assert _claude_tools(argv) == [*lean, "WebFetch", "WebSearch"]
    # The no_internet restriction wins over the task's kind.
    p.set_config("restrictions.no_internet", True)
    argv, _, _ = _dispatch_one(p, monkeypatch, {}, kind="question")
    assert _claude_tools(argv) == lean and "WebFetch" not in argv
    p.set_config("restrictions.no_internet", False)
    # A project with MCP servers for its workers: they come through --mcp-config; --tools only limits
    # built-in tools, and ToolSearch keeps their schemas deferred. The project's own denylist stays.
    _claude_mcp_config(tmp_path, monkeypatch, p)
    p.set_config("providers.claude.worker_isolation", True)
    p.set_config("providers.claude.mcp_servers", ["docs"])
    p.set_config("providers.claude.disallowed_tools", ["mcp__docs__write_page"])
    argv, _, _ = _dispatch_one(p, monkeypatch, {}, kind="work")
    assert _claude_tools(argv) == [*lean, "WebFetch", "WebSearch"]
    assert "--strict-mcp-config" in argv and "--mcp-config" in argv
    assert argv[argv.index("--disallowedTools") + 1:argv.index("--settings")] == ["mcp__docs__write_page"]
    pathlib.Path(argv[argv.index("--mcp-config") + 1]).unlink()
    # A CLI without --tools keeps the old denylist, the web tools on it where the task does not need them.
    argv, _, _ = _dispatch_one(p, monkeypatch, {claude.TOOLS: False}, kind="review")
    assert "--tools" not in argv
    denied = argv[argv.index("--disallowedTools") + 1:argv.index("--settings")]
    assert denied == [*claude.WORKER_DENIED_TOOLS, "WebFetch", "WebSearch", "mcp__docs__write_page"]
    pathlib.Path(argv[argv.index("--mcp-config") + 1]).unlink()


def test_the_tool_allowlist_leaves_coordinator_turns_codex_and_cursor_alone(env, monkeypatch):
    from ttp import daemon as dmod
    from ttp.providers import claude, get_provider
    monkeypatch.setattr(claude, "_FLAGS", {claude.TOOLS: True})
    kw = dict(model="opus", effort="low", cwd=".", budget_usd=1.0, schema=None, restrictions={})
    turn, _ = get_provider("claude").build(role="coordinator", read_only=True, web=False, **kw)
    assert "--tools" not in turn and turn[turn.index("--disallowedTools") + 1:] == ["Edit", "Write", "NotebookEdit"]
    for name in ("codex", "cursor"):
        for role, read_only in (("worker", False), ("reviewer", False), ("coordinator", True)):
            with_web = get_provider(name).build(role=role, read_only=read_only, web=True, **kw)
            assert get_provider(name).build(role=role, read_only=read_only, web=False, **kw) == with_web, name
    assert dmod.web_needed("worker", None) and dmod.web_needed("worker", {"kind": "code", "spec": ""})
    assert not dmod.web_needed("reviewer", {"kind": "review", "title": "review #1", "spec": "diff"})
    assert dmod.web_needed("reviewer", {"kind": "review", "title": "see http://example.org/a", "spec": ""})


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
        assert _slot_tasks(p) == [plain], "an urgent task ranks before an obligation owner"
        line, = _sched_lines(p)
        assert line["runnable"] == [{"id": plain, "priority": 1, "owns_obligation": False},
                                    {"id": owner, "priority": 3, "owns_obligation": True}], line
        assert line["picked"] == [plain]
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
    budget.urgent_extra_slots (default 1) beyond max_parallel_workers; a second waits; 0 turns it off."""
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
        assert [t["id"] for t in p.db.ready_tasks()] == [hot2, hot, r3], "urgent first, by priority within"
        d.cfg["budget"]["urgent_extra_slots"] = 0
        d.dispatch()
        assert _slot_tasks(p) == [r1, r2], "0 turns the extra slot off"
        d.cfg["budget"]["urgent_extra_slots"] = 1
        d.dispatch()
        assert _slot_tasks(p) == [r1, r2, hot2], "one extra slot, for the first urgent task"
        d.dispatch()
        assert _slot_tasks(p) == [r1, r2, hot2], "a second urgent task waits past the extra slot"
        settle(d)
        d.dispatch()
        assert _slot_tasks(p) == [hot, r3], "the spec-line urgent task before routine priority 2"
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
    """coordinator.urgent_slot_wait_min (default 10): an urgent task held by the spend gate (not the
    slots) raises one event naming that hold, counted from when it was queued; a routine one none."""
    import dataclasses
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=1)
    try:
        assert d.cfg["coordinator"]["urgent_slot_wait_min"] == 10
        core = d.cfg.get("core_provider", "claude")
        d.gates[core] = dataclasses.replace(d.gates[core], allow_new_work=False)
        hot = p.db.add_task("urgent fix", "Fix it.\nurgent: a demo in an hour", kind="work", tier="light",
                            origin="user", priority=3)
        p.db.add_task("routine", "s", kind="work", tier="light", origin="user", priority=3)
        start = time.time()

        def check(minutes):
            d._slot_wait_checked = start + minutes * 60 - 60
            monkeypatch.setattr(time, "time", lambda: start + minutes * 60)
            d.check_slot_wait()
            return _slot_wait_events(p)

        assert check(1) == [] and check(9) == []
        (ev,) = check(10)
        assert ev["task"] == hot and ev["severity"] == "normal"
        assert "urgent: a demo in an hour" in ev["text"] and "held by the spend gate (no new work)" in ev["text"]
        assert len(check(11)) == 1 and len(check(30)) == 1, "raised again for the same wait"
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
