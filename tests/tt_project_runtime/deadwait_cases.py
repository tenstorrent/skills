# Dead landed-wait tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new deadwait.py tests (landed_wait_dead, review_nofix) here.
from __future__ import annotations


def _dw_producer(p, name, status="done"):
    """A producer task on its own branch, with one queued task held on landed:#<it>."""
    x = p.db.add_task(name, "s", kind="code", origin="user")
    p.db.update_task(x, status=status, branch=f"ttp/t{x}-{name}")
    w = p.db.add_task(f"after {name}", "s", origin="coordinator", labels=[f"start_when:landed:#{x}"])
    return x, w


def _dw_push_row(p, task, branch, status, sha="a" * 40):
    now = time.time()
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated, landed_sha, pushed_sha) "
           "VALUES(?,?,?,?,?,?,?,?,?)", (task, branch, "c" * 40, "origin/work", status, now, now, sha, sha))


def _dw_fixture(p):
    """Held producers of every kind: {name: (producer, held task)}."""
    out = {name: _dw_producer(p, name) for name in
           ("landed", "dead", "squashed", "carried", "queued", "abandoned", "landing", "superseded")}
    out["running"] = _dw_producer(p, "running", status="running")
    branch = lambda name: p.db.task(out[name][0])["branch"]
    _dw_push_row(p, out["landed"][0], branch("landed"), "landed")
    # Squash-carried: a fix continuing the producer landed under another commit on its own branch.
    fix = p.db.add_task("Fix review: squashed", "s", kind="code", labels=[f"continues:{out['squashed'][0]}"])
    p.db.update_task(fix, status="done", branch=f"ttp/t{fix}-fix")
    _dw_push_row(p, fix, f"ttp/t{fix}-fix", "landed", sha="b" * 40)
    p.db.add_task(f"Review #{out['carried'][0]}", "s", kind="review", depends_on=[out["carried"][0]])
    _dw_push_row(p, out["queued"][0], branch("queued"), "approved", sha=None)
    p.db.x("INSERT INTO task_abandoned(task, why, ts) VALUES(?,?,?)", (out["abandoned"][0], "dropped", time.time()))
    p.db.x("INSERT INTO task_landings(marker, task, sha, target, ts) VALUES(?,?,?,?,?)",
           ("m1.json", out["landing"][0], "d" * 40, "origin/work", time.time()))
    p.db.x("INSERT INTO task_superseded(task, head, sha, ts) VALUES(?,?,?,?)",
           (out["superseded"][0], "c" * 40, "e" * 40, time.time()))
    return out


def test_deadwait_judges_every_held_producer_of_a_check_from_one_read_as_from_its_own(env):
    """check() reads the rows once for all held producers: each verdict (path, recorded, carriers) is
    the one a read of its own gives (`ttp landed --task` reads its own), and only the producer
    nothing can land raises landed_wait_dead: landed, squash-carried, carried by an open review, in
    the push queue, abandoned, landed by a detached push, superseded or not done are no dead end."""
    from ttp import deadwait, landed
    p = make(env)
    fx = _dw_fixture(p)
    held_by = {x: [p.db.task(w)] for x, w in fx.values()}
    rows = deadwait.Rows(p.db, held_by)
    for name, (x, w) in fx.items():
        assert deadwait.path(p.db, x, {w}, rows) == deadwait.path(p.db, x, {w}), name
        if p.db.task(x)["status"] == "done":
            assert landed.carriers(p.db, x, rows) == landed.carriers(p.db, x), name
            assert landed.recorded(p.db, x, rows=rows) == landed.recorded(p.db, x), name
    assert deadwait.path(p.db, fx["squashed"][0], set(), rows) == "a landing is recorded"
    assert landed.recorded(p.db, fx["squashed"][0])[0] == ["b" * 40]
    assert deadwait.path(p.db, fx["dead"][0], {fx["dead"][1]}, rows) == ""
    assert deadwait.path(p.db, 999999, set(), rows) == "#999999 is missing"
    deadwait.check(p.db, {"waiting": {"landed_wait_dead_s": 0}}, held_by, time.time())
    (ev,) = p.db.q("SELECT * FROM events WHERE kind=?", (deadwait.DEAD_EVENT,))
    assert ev["task"] == fx["dead"][0] and f"#{fx['dead'][1]} (queued)" in ev["text"]
    assert list(p.db.kv(deadwait.KEY)) == [str(fx["dead"][0])]


def test_deadwait_check_reads_a_fixed_number_of_rows_sets_however_many_producers_wait(env):
    """A full check runs the same few statements for 1 held producer as for 9; a check whose rows,
    held tasks and saved state are unchanged reads only its state and its stamp of the rows."""
    from ttp import deadwait
    p = make(env)
    fx = _dw_fixture(p)
    seen = []
    p.db.conn.set_trace_callback(seen.append)

    def statements(held_by, now):
        seen.clear()
        deadwait.check(p.db, {"waiting": {"landed_wait_dead_s": 0}}, held_by, now)
        return len(seen)
    t0 = time.time()
    one = statements({fx["landed"][0]: [p.db.task(fx["landed"][1])]}, t0)
    p.db.conn.set_trace_callback(None)
    held_by = {x: [p.db.task(w)] for x, w in fx.values()}
    p.db.conn.set_trace_callback(seen.append)
    many = statements(held_by, t0)
    assert one <= 10 and many <= one + 4, (one, many, seen)   # the one raise: its insert and kv write
    assert statements(held_by, t0 + 3) <= 2, seen
    p.db.conn.set_trace_callback(None)


def test_deadwait_skip_never_delays_a_new_dead_wait_past_its_grace(env):
    """The skip of an unchanged check never holds a raise back: a wait dead since t0 is raised on the
    first check at t0 + grace, a path that ends is judged on the next check, and a write that keeps
    every stamp is still judged within RECHECK_S."""
    from ttp import deadwait
    p = make(env)
    x, w = _dw_producer(p, "feature")
    review = p.db.add_task(f"Review #{x}", "s", kind="review", depends_on=[x])
    held_by = lambda: {x: [p.db.task(w)]}
    dead = lambda: p.db.q("SELECT * FROM events WHERE kind=?", (deadwait.DEAD_EVENT,))
    cfg = {"waiting": {"landed_wait_dead_s": 30}}
    t0 = time.time()
    deadwait.check(p.db, cfg, held_by(), t0)
    assert p.db.kv(deadwait.KEY, {}) == {}, "an open review is a path"
    p.db.update_task(review, status="failed")
    deadwait.check(p.db, cfg, held_by(), t0 + 1)
    assert p.db.kv(deadwait.KEY)[str(x)] == {"since": t0 + 1, "event": None}, "judged on the next check"
    for k in range(2, 31):
        deadwait.check(p.db, cfg, held_by(), t0 + k)
        assert bool(dead()) == (k >= 31), k
    deadwait.check(p.db, cfg, held_by(), t0 + 31)
    (ev,) = dead()
    assert ev["status"] == "queued", "raised on the first check at its grace"
    # A path that bypasses every stamp (a raw write keeping tasks.updated): judged within RECHECK_S.
    p.db.x("UPDATE tasks SET status='queued' WHERE id=?", (review,))
    deadwait.check(p.db, cfg, held_by(), t0 + 32)
    deadwait.check(p.db, cfg, held_by(), t0 + 31 + deadwait.RECHECK_S)
    assert p.db.one("SELECT status FROM events WHERE id=?", (ev["id"],))["status"] == "handled"
    # A grace shorter than a tick: raised on the first check at it.
    p.db.update_task(review, status="failed")
    deadwait.check(p.db, {"waiting": {"landed_wait_dead_s": 0}}, held_by(), t0 + 100)
    assert len(dead()) == 2


def test_a_skipped_deadwait_check_never_clears_or_raises_from_probe_verdicts(env):
    """sa7: the skip only decides whether to re-judge. A skipped tick with a passing in-memory probe
    verdict, or after a restart (no verdicts, no memory of the last check), leaves the queued event
    and its saved state as they are; a path in the saved rows clears it on the next tick."""
    p = make(env)
    p.set_config("delivery.push_branch", "origin/work")   # landed:#<id> is checkable here
    from ttp import deadwait
    from ttp.daemon import Daemon
    x, w = _dw_producer(p, "feature")
    rows = lambda: p.db.q("SELECT * FROM tasks WHERE id=?", (w,))
    dead = lambda: p.db.q("SELECT * FROM events WHERE kind=?", (deadwait.DEAD_EVENT,))
    t0 = time.time()
    d = Daemon(p.base)
    d._dead_landed_waits(rows(), t0)
    d._dead_landed_waits(rows(), t0 + deadwait.GRACE_S)
    (ev,) = dead()
    state = p.db.kv(deadwait.KEY)
    d._start_rc[w] = (0, t0 + deadwait.GRACE_S, f"landed:#{x}")   # a passing verdict: not a saved fact
    d._dead_landed_waits(rows(), t0 + deadwait.GRACE_S + 3)
    deadwait._last.clear()                                       # a restart forgets the last check
    d2 = Daemon(p.base)
    d2._dead_landed_waits(rows(), t0 + deadwait.GRACE_S + 6)
    d2._dead_landed_waits(rows(), t0 + deadwait.GRACE_S + 9)
    assert dead() == [ev] and p.db.kv(deadwait.KEY) == state
    p.db.add_task(f"Fix #{x}", "s", kind="code", labels=[f"continues:{x}"])
    d2._dead_landed_waits(rows(), t0 + deadwait.GRACE_S + 12)
    assert p.db.one("SELECT status FROM events WHERE id=?", (ev["id"],))["status"] == "handled"
    assert p.db.kv(deadwait.KEY) == {}
