# Queue-wait escalation tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects these
# tests under its name. Add new queuewait.py tests (detector, options, must-act settle, daily metric) here.
from __future__ import annotations

QW_CFG = {"coordinator": {"queue_wait_escalate_min": 30}, "budget": {"max_parallel_workers": 6}}


def _qw_host(p, now, mine_at, cap=4, queued=20, limit_set=False):
    """A fake host checks state: `cap` running (cap `cap`), `queued` queued, one of them task 7 of `p` (made
    here: a label of a task that is gone raises nothing)."""
    if not p.db.one("SELECT id FROM tasks WHERE id=7"):
        _qw_task7(p)
    q = [f"other task #{i} (run 1) ttp checks" for i in range(queued - 1)] + [f"{p.name} task #7 (run 3) ttp checks"]
    return {"limit": cap, "running": [f"other task #{90 + i} (run 1)" for i in range(cap)], "queued": q,
            "queued_at": [now - 600] * (queued - 1) + [mine_at], "priority": [], "urgent": [], "parked": [],
            "limit_set": limit_set}


def _qw_task7(p):
    while (tid := p.db.add_task(f"t{len(p.db.q('SELECT id FROM tasks'))}", "s", kind="code")) < 7:
        pass
    return tid


def _qw_events(p, status=None):
    return p.db.q("SELECT * FROM events WHERE kind='queue_wait'" + (f" AND status='{status}'" if status else "")
                  + " ORDER BY id")


def test_queue_wait_fires_on_a_full_checks_queue_with_options_then_clears(env):
    """Criterion: '4 running (cap 4), 20 queued' with a task of this project queued 40 min raises one must-act
    item naming the queue, cap, occupancy, holders and options; the same mark never raises twice; an end
    of the wait settles it and the daily metric counts it."""
    from ttp import mustact, pagesev, queuewait
    p = make(env)
    now = time.time()
    host = _qw_host(p, now, now - 40 * 60)
    assert queuewait.scan(p, QW_CFG, now - 15 * 60, host) == []   # 25 min: under the threshold
    fps = queuewait.scan(p, QW_CFG, now, host, cores=64)
    assert len(fps) == 1 and queuewait.scan(p, QW_CFG, now + 60, host, cores=64) == []
    ev = _qw_events(p)[0]
    assert ev["kind"] in mustact.KINDS and ev["task"] == 7
    assert ev["severity"] == "normal" and pagesev.rides_along(ev)   # every host slot taken: host-held
    t = ev["text"]
    assert "qwait-7-checks" in t and "41 min" in t and "4 running (cap 4), 20 queued" in t, t   # as of the rescan
    assert "other task #90" in t and "raise the host-wide checks cap from 4 to 6 (host limit 8)" in t, t
    assert "spill" not in t   # no check host is listed: no spill route
    assert "qwait-7-checks" in mustact.block(p.db, [ev["id"]])
    assert queuewait.scan(p, QW_CFG, now + 120, {}) == []
    assert _qw_events(p, "queued") == []   # the wait ended: its open item is settled
    line = queuewait.daily_line(p.db, QW_CFG, now + 120)
    assert line.startswith("queue_wait_over: 1 queue wait(s) over 30 min") and "checks 1" in line
    assert "no unblock action" in line


def test_queue_wait_options_leave_out_forbidden_routes(env):
    """A cap someone set is never offered for raising; a device the user paused leaves no option and
    the user is told once; a user's own task is never offered for cancelling."""
    from ttp import queuewait
    from ttp.db import PAUSED_RESOURCES_KEY
    p = make(env)
    now = time.time()
    queuewait.scan(p, QW_CFG, now, _qw_host(p, now, now - 40 * 60, limit_set=True), cores=64)
    assert "raise the host-wide checks cap" not in _qw_events(p)[0]["text"]
    tid = p.db.add_task("dev run", "s", kind="work", origin="user")
    p.db.update_task(tid, not_before=now + 600, result=json.dumps(
        {"status": "waiting", "retry_when": "ttp lock --probe dev0", "waiting_since": now - 50 * 60}))
    p.db.set_kv(PAUSED_RESOURCES_KEY, {"dev0": {"by": "user", "reason": "hold", "since": now}})
    w = {"task": tid, "queue": "device", "res": "dev0", "since": now - 3000}
    assert queuewait.options(p.db, w, QW_CFG, p.db.paused_resources(), {"dev0", "dev1"}) == []
    p.db.set_kv(PAUSED_RESOURCES_KEY, {})
    opts = queuewait.options(p.db, w, QW_CFG, {}, {"dev0", "dev1"})
    assert any("dev1" in o for o in opts) and not any("cancel" in o for o in opts), opts
    p.db.set_kv(PAUSED_RESOURCES_KEY, {"dev0": {"by": "user", "reason": "hold", "since": now}})
    told = lambda: p.db.q("SELECT id FROM messages WHERE direction='out' AND text LIKE '%only you can clear%'")  # noqa: E731
    queuewait.scan(p, QW_CFG, now, {}, {"dev0", "dev1"})
    queuewait.scan(p, QW_CFG, now + 31 * 60, {}, {"dev0", "dev1"})
    assert len(told()) == 1
    assert any(f"qwait-{tid}-device" in e["text"] for e in _qw_events(p))


def test_queue_wait_settle_records_an_act_or_raises_again_once_the_wait_doubles(env):
    """No acting action (a plain reply to every chat counts not): no immediate re-wake, nor at the next
    30-minute mark; the wait doubling since the raise (40 to 80 min) raises it again. A config_set or a reply
    naming the key settles it for this wait."""
    from ttp import mustact, queuewait
    p = make(env)
    now = time.time()
    host = _qw_host(p, now, now - 40 * 60)
    queuewait.scan(p, QW_CFG, now, host)
    ev = _qw_events(p)[0]
    p.db.x("UPDATE events SET status='handled' WHERE id=?", (ev["id"],))
    mustact.settle(p, [ev["id"]], [{"type": "reply", "chat": "all", "text": "ok"}], [0], now)
    assert not p.db.q("SELECT id FROM events WHERE kind='must_act_rewake'")
    assert queuewait.scan(p, QW_CFG, now + 5 * 60, host) == []
    assert queuewait.scan(p, QW_CFG, now + 31 * 60, host) == []   # the next mark: same cause, not doubled
    assert len(queuewait.scan(p, QW_CFG, now + 40 * 60, host)) == 1   # 80 min: doubled
    assert queuewait.scan(p, QW_CFG, now + 41 * 60, host) == []
    ev2 = _qw_events(p)[-1]
    p.db.x("UPDATE events SET status='handled' WHERE id=?", (ev2["id"],))
    act = {"type": "reply", "chat": "all", "text": "qwait-7-checks: held by the host checks cap, a safety cap"}
    mustact.settle(p, [ev2["id"]], [act], [0], now + 42 * 60)
    assert queuewait.scan(p, QW_CFG, now + 200 * 60, host) == []
    line = queuewait.daily_line(p.db, QW_CFG, now + 200 * 60)
    assert "median 52" in line and "0 with none" in line, line
    it = {"key": "qwait-7-checks", "chat": "all", "tasks": [7], "kind": mustact.QUEUE_KIND}
    assert mustact.acted(it, [{"type": "config_set", "key": "x", "value": 1}], [0]) == "config_set"
    assert mustact.acted(it, [{"type": "notify", "text": "busy"}], [0]) is None
    assert mustact.acted(it, [{"type": "task_update", "id": 7, "priority": 1}], [0])


def test_queue_wait_covers_worker_slots_and_polled_checks(env):
    """A ready task never started while every worker slot is taken, and a worker polling its own detached
    checks, each raise one item; a task whose checks hold a running slot does not."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    busy = p.db.add_task("busy", "s", kind="code")
    p.db.update_task(busy, status="running")
    p.db.x("INSERT INTO runs(task,role,started,status) VALUES(?,?,?,'running')", (busy, "worker", now - 3600))
    ready = p.db.add_task("ready", "s", kind="code")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 45 * 60, ready))
    poll = p.db.add_task("poll", "s", kind="code")
    p.db.update_task(poll, not_before=now + 300, blocked_reason="next try 12:00", result=json.dumps(
        {"status": "waiting", "retry_when": f"ttp checks --result {_qw_slot_wait(p)}", "waiting_since": now - 35 * 60}))
    cfg = {**QW_CFG, "budget": {"max_parallel_workers": 1}}
    full = {"limit": 4, "running": [f"other task #{i} (run 1)" for i in range(4)], "queued": ["other task #9 (run 1)"]}
    got = {(w["task"], w["queue"]) for w in queuewait.collect(p, cfg, now, full)}
    assert (ready, "workers") in got and (poll, "checks") in got, got
    host = {"running": [f"{p.name} task #{poll} (run 2) ttp checks"], "limit": 4, "queued": ["other task #9 (run 1)"]}
    assert (poll, "checks") not in {(w["task"], w["queue"]) for w in queuewait.collect(p, cfg, now, host)}
    assert len(queuewait.scan(p, cfg, now, full)) == 2


def _qw_slot_wait(p, rc=False, slot=True, pid=None):
    """A run dir whose detached checks wait for a host-wide slot: checks.pid naming a live process (this
    one, or `pid`), checks-slot.wait, and checks.rc only when `rc` (its checks finished)."""
    from ttp import cli
    from ttp.runner import proc_start
    d = p.state / "runs" / f"qw-{len(list((p.state / 'runs').glob('qw-*'))) if (p.state / 'runs').exists() else 0}"
    d.mkdir(parents=True, exist_ok=True)
    pid = os.getpid() if pid is None else pid
    (d / cli.CHECKS_PID).write_text(json.dumps({"pid": pid, "started": proc_start(pid) or "gone"}))
    if slot:
        (d / cli.CHECKS_SLOT_WAIT).write_text("{}")
    if rc:
        (d / cli.CHECKS_RC).write_text("0\n")
    return d


def test_queue_wait_checks_needs_an_open_task_with_a_live_slot_wait_and_a_nonempty_queue(env):
    """A false '#N has waited 99 min on the checks queue: 3 running (cap 4), 0 queued' (#7815: done, its
    checks long finished, the holders another project's). A checks wait is raised only for an open task
    with a live wait record (its checks queued or waiting for a slot) while the host queue has someone
    waiting: a done task, a run whose checks finished, ran or died, and a queue with 0 queued raise nothing;
    a real wait still raises one."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    full = {"limit": 4, "running": [f"other task #{i} (run 1)" for i in range(3)], "queued": ["other task #9 (run 1)"]}
    empty = {**full, "queued": []}

    def wait(name, run_dir, status=None):
        tid = p.db.add_task(name, "s", kind="code")
        p.db.update_task(tid, not_before=now + 300, blocked_reason="waiting for checks; its probe says not yet" + NT,
                         result=json.dumps({"status": "waiting", "retry_when": f"ttp checks --result {run_dir}",
                                            "waiting_since": now - 99 * 60}))
        if status:
            p.db.update_task(tid, status=status)
        return tid

    def got(host):
        return {w["task"] for w in queuewait.collect(p, QW_CFG, now, host) if w["queue"] == "checks"}

    real = wait("real", _qw_slot_wait(p))
    done = wait("done", _qw_slot_wait(p), "done")
    gone = wait("cancelled", _qw_slot_wait(p), "cancelled")
    finished = wait("finished", _qw_slot_wait(p, rc=True))
    running = wait("running", _qw_slot_wait(p, slot=False))
    dead = wait("dead", _qw_slot_wait(p, pid=2 ** 22 + 7))
    nodir = wait("nodir", p.state / "runs" / "absent")
    assert got(full) == {real}, got(full)
    assert got(empty) == set(), "0 queued on the host: no checks slot to wait for"
    queuewait.scan(p, QW_CFG, now, empty)
    assert _qw_events(p) == []
    queuewait.scan(p, QW_CFG, now, full)
    assert [e["text"].split(":", 1)[0] for e in _qw_events(p)] == [f"qwait-{real}-checks"], _qw_events(p)
    assert {done, gone, finished, running, dead, nodir}.isdisjoint(got(full))


NT = "; next try 07:10 PST"   # the tail the daemon puts on every waiting task's reason (daemon.py, sweepskip.NEXT_TRY)


@pytest.mark.parametrize("probe,reason,want", [
    ("ttp checks --result /r/1", "waiting for checks (ttp checks: queued for a host-wide checks slot (pid 9))" + NT,
     ("checks", "")),
    ("ttp checks --result /r/1", "waiting for checks; its probe says not yet" + NT, ("checks", "")),
    ("", "waiting for ttp checks: queued for a host-wide checks slot" + NT, ("checks", "")),
    ("ttp checks --result /r/1", "waiting for checks (ttp checks: running (pid 9))" + NT, None),
    ("ttp push --result /r/m", "waiting for the push; next try Fri 07:10 PST", ("push", "")),
    ("ttp lock --probe dev0", "waiting for dev0 held by #5" + NT, ("device", "dev0")),
    ("ttp lock --probe 'build-dir'", "waiting for build-dir" + NT, ("lock", "build-dir")),
    ("ttp devq status 3", "waiting for the device run" + NT, ("device", "")),
    ("ttp pool-wait --check x", "waiting for a Slurm node" + NT, ("pool", "")),
    ("", "waiting for CPU admission on the host" + NT, ("cpu", "")),
    ("ttp detach --check /r/job", "waiting for the build" + NT, None),
    ("echo ttp checks", "waiting for checks" + NT, None),
    ("landed:#12", "waiting for landed:#12" + NT, None),
    ("ttp ci --repo o/r --branch b", "waiting for ttp ci on o/r" + NT, None),
    ("ttp ack-wait probe --from quad --hash ab12", "waiting for quad's reply" + NT, None),
    ("ttp notify --check true", "waiting for the user's sign-in" + NT, None),
    ("", "waiting for the user" + NT, None),
])
def test_queue_wait_classifies_each_queue(probe, reason, want):
    """The daemon's real reasons: its '; next try' tail never makes a wait a checks wait; the probe decides
    first; a worker's running checks are no queue wait."""
    from ttp import queuewait
    assert queuewait.classify(probe, reason, {"dev0"}) == want


def test_queue_wait_skips_running_checks_and_finished_tasks(env):
    """A worker whose own checks run (cap 0: the host view lists none) waits on no queue; a done or
    cancelled task whose label is still in the host queue raises nothing, so no option names it."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    run = p.db.add_task("run", "s", kind="code")
    p.db.update_task(run, not_before=now + 300, blocked_reason="waiting for checks (ttp checks: running (pid 9))" + NT,
                     result=json.dumps({"status": "waiting", "retry_when": "ttp checks --result /tmp/x",
                                        "waiting_since": now - 40 * 60}))
    assert queuewait.collect(p, QW_CFG, now, {"limit": 0, "running": [], "queued": []}) == []
    gone = p.db.add_task("gone", "s", kind="code")
    p.db.update_task(gone, status="done")
    host = {"limit": 4, "running": [], "queued": [f"{p.name} task #{gone} (run 1)"], "queued_at": [now - 3600]}
    assert queuewait.collect(p, QW_CFG, now, host) == [] and queuewait.scan(p, QW_CFG, now, host) == []


def test_queue_wait_folds_into_an_active_mute_with_an_open_owner(env):
    """A throughput-class mute of the host-wide checks slot whose owner task is open raises no item for
    any checks wait; once the owner is done, or the mute ends, the item is raised."""
    from ttp import queuewait, screen
    p = make(env)
    now = time.time()
    assert _qw_task7(p) == 7
    fix = p.db.add_task("fix the checks slot", "s", kind="code")
    p.db.set_kv(screen.MUTES_KEY, [{"source": "wait_stale", "match": "host-wide checks slot", "below": "high",
                                    "why": f"fix queued as #{fix}", "since": now, "until": now + 3600, "count": 0,
                                    "throughput": {"owner": {"task": fix}, "max_h": 12}}])
    host = _qw_host(p, now, now - 40 * 60)
    assert queuewait.scan(p, QW_CFG, now, host, cores=64) == []
    p.db.update_task(fix, status="done")
    assert len(queuewait.scan(p, QW_CFG, now + 60, host, cores=64)) == 1
    p.db.x("UPDATE events SET status='handled' WHERE kind='queue_wait'")   # the turn handled it
    f2 = p.db.add_task("fix again", "s", kind="code")   # the wait doubling, with the mute ending instead of its owner
    p.db.set_kv(screen.MUTES_KEY, [{"source": "wait_stale", "match": "host-wide checks slot", "below": "high",
                                    "why": f"#{f2}", "since": now, "until": now + 35 * 60, "count": 0,
                                    "throughput": {"owner": {"task": f2}, "max_h": 12}}])
    assert queuewait.scan(p, QW_CFG, now + 34 * 60, host) == []   # 74 min, the mute holds
    assert queuewait.scan(p, QW_CFG, now + 36 * 60, host) == []   # mute over, 76 min: not doubled
    assert len(queuewait.scan(p, QW_CFG, now + 42 * 60, host)) == 1


def test_queue_wait_mute_matches_only_the_queue_and_the_task_itself(env):
    """A mute folds a wait only by the queue's own words or the waiting task's reason without task ids: a
    task-keyed mute of a holder ('#91' in the item's Holding list, or '#9' inside '#91') or of a task the
    reason names raises the item; a task-keyed mute of the waiting task itself folds only its own wait."""
    from ttp import queuewait, screen
    p = make(env)
    now = time.time()
    assert _qw_task7(p) == 7
    fix = p.db.add_task("fix", "s", kind="code")
    p.db.update_task(7, blocked_reason=f"checks queued behind #91 and #{fix}; next try 08:10 PST")

    def mute(match, source="progress"):
        p.db.set_kv(screen.MUTES_KEY, [{"source": source, "match": match, "below": "high", "why": f"#{fix}",
                                        "since": now, "until": now + 3600, "count": 0,
                                        "throughput": {"owner": {"task": fix}, "max_h": 12}}])
        p.db.x("DELETE FROM events WHERE kind='queue_wait'")
        p.db.set_kv(queuewait.KV, {})

    host = _qw_host(p, now, now - 40 * 60)
    assert "#91" in queuewait.text({**queuewait.collect(p, QW_CFG, now, host)[0]}, [], now)
    for m in ("#91", "#9", "probe:#91", f"#{fix}", "next try"):
        mute(m)
        assert len(queuewait.scan(p, QW_CFG, now, host, cores=64)) == 1, m
    mute("#7")
    assert queuewait.scan(p, QW_CFG, now, host, cores=64) == []
    mute("host-wide checks slot", "wait_stale")
    assert queuewait.scan(p, QW_CFG, now, host, cores=64) == []
    mute("#7")   # another task's wait on the same queue still raises under #7's mute
    b = p.db.add_task("b", "s", kind="code")
    host2 = {**host, "queued": host["queued"] + [f"{p.name} task #{b} (run 1)"],
             "queued_at": host["queued_at"] + [now - 41 * 60]}
    assert [fp.split(":")[1] for fp in queuewait.scan(p, QW_CFG, now, host2, cores=64)] == [str(b)]


def test_queue_wait_fix_owner_is_never_another_waiter_on_the_queue(env):
    """A task_update naming the key on another task waiting on the same queue (a priority bump of the queue
    head) owns no cause; on a task not waiting there it does."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    a, b, c = (p.db.add_task(x, "s", kind="code") for x in "abc")
    host = {"limit": 4, "running": [], "priority": [], "urgent": [], "parked": [],
            "queued": [f"{p.name} task #{a} (run 1)", f"{p.name} task #{b} (run 1)"], "queued_at": [now - 40 * 60] * 2}
    queuewait.scan(p, QW_CFG, now, host)
    ev = json.loads(_qw_events(p)[0]["data"])["must_act"]
    it = {"key": ev["key"], "tasks": ev["tasks"], "episode": ev["episode"]}
    other = b if ev["tasks"] == [a] else a
    assert queuewait.fix_owner(p.db, it, [{"type": "task_update", "id": other, "priority": 1,
                                           "note": it["key"]}], [0]) is None
    assert queuewait.fix_owner(p.db, it, [{"type": "task_update", "id": c, "note": it["key"]}], [0]) == c


def test_queue_wait_one_act_per_queue_cause(env):
    """An item acted on with a task_add naming its key makes that task the cause's owner: other tasks'
    waits on the same queue raise nothing while it is open, and again once it is done. A priority bump of
    the waiting task itself owns no cause."""
    from ttp import mustact, queuewait
    p = make(env)
    now = time.time()
    a, b = p.db.add_task("a", "s", kind="code"), p.db.add_task("b", "s", kind="code")
    host = {"limit": 4, "running": [], "priority": [], "urgent": [], "parked": [],
            "queued": [f"{p.name} task #{a} (run 1)"], "queued_at": [now - 40 * 60]}
    queuewait.scan(p, QW_CFG, now, host)
    ev = _qw_events(p)[0]
    k = f"qwait-{a}-checks"
    acts = [{"type": "task_add", "title": "spill checks", "spec": f"for {k}: spill them", "kind": "code"}]
    fix = p.db.add_task("spill checks", f"for {k}: spill them", kind="code")
    p.db.x("UPDATE events SET status='handled' WHERE id=?", (ev["id"],))
    mustact.settle(p, [ev["id"]], acts, [0], now)
    host2 = {**host, "queued": host["queued"] + [f"{p.name} task #{b} (run 1)"], "queued_at": [now - 40 * 60] * 2}
    assert queuewait.scan(p, QW_CFG, now + 60, host2) == []
    p.db.update_task(fix, status="done")
    assert [fp.split(":")[1] for fp in queuewait.scan(p, QW_CFG, now + 120, host2)] == [str(b)]
    it = {"key": k, "tasks": [a]}
    assert queuewait.fix_owner(p.db, it, [{"type": "task_update", "id": a, "priority": 1, "note": k}], [0]) is None


def test_queue_wait_rule_is_in_the_role_prompt_every_provider_reads():
    """The rule lives in coordinator.md, which the per-wake and the session coordinator of every provider read."""
    text = (RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text()
    assert "qwait-<task>-<queue>" in text and "safety rule that holds it" in text


def test_queue_wait_raises_at_most_three_a_scan_longest_first(env):
    """Five tasks of the project queued 40-80 min: the first scan raises the three longest waits, the next
    the other two; none twice."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    ids = [p.db.add_task(f"t{i}", "s", kind="code") for i in range(1, 6)]
    host = {"limit": 4, "running": [], "priority": [], "urgent": [], "parked": [],
            "queued": [f"{p.name} task #{i} (run 1)" for i in ids],
            "queued_at": [now - (40 + 10 * i) * 60 for i in range(1, 6)]}
    first = queuewait.scan(p, QW_CFG, now, host)
    assert [fp.split(":")[1] for fp in first] == [str(i) for i in ids[:1:-1]]
    assert sorted(fp.split(":")[1] for fp in queuewait.scan(p, QW_CFG, now + 60, host)) == sorted(map(str, ids[:2]))
    assert queuewait.scan(p, QW_CFG, now + 120, host) == []


def test_queue_wait_offers_a_spill_host_only_when_it_can_take_checks_now(env):
    """#6883: a checks wait's item lists a spill host as an option only when it can take checks now (allowed,
    not paused, its lock not refused); with every spill host paused it lists none and names the pause."""
    from ttp import db as dbm
    from ttp import machines, queuewait
    p = make(env)
    now = time.time()
    hosts = ["check-host-a", "check-host-b"]
    p.charter_path.write_text("# Charter\n## Resources\n" + "".join(f"- {h}: a check host\n" for h in hosts))
    for h in hosts:
        machines.add(h)
    cfg = {**QW_CFG, "delivery": {"checks_spill_hosts": hosts}}
    host = _qw_host(p, now, now - 40 * 60)

    def item():
        p.db.x("DELETE FROM events WHERE kind='queue_wait'")
        p.db.set_kv(queuewait.KV, {})
        assert len(queuewait.scan(p, cfg, now, host, cores=64)) == 1
        return _qw_events(p)[0]["text"]

    t = item()
    assert "spill to an allowed check host (check-host-a, check-host-b)" in t and "No spill host" not in t, t
    p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"check-host-a": {"reason": "user hold", "by": "user", "since": now}})
    t = item()
    assert "spill to an allowed check host (check-host-b)" in t, "a paused host is never offered"
    p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {h: {"reason": "user hold", "by": "user", "since": now} for h in hosts})
    t = item()
    assert "spill to" not in t, "no spill host can take checks: none is offered"
    assert "No spill host can take checks now (check-host-a: paused: user hold; check-host-b: paused: user hold)" in t, t
    assert "Options: (1)" in t, "the other options stay"
    p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {})
    w = {"task": 7, "queue": "checks", "since": now - 2400}
    assert not any("spill to" in o for o in queuewait.options(p.db, w, cfg, {})), "never offered unchecked"
    assert any("spill to" in o for o in queuewait.options(p.db, w, cfg, {}, spill=queuewait.spill_ready(p, cfg)))


def test_queue_wait_workers_counts_dispatch_live_slots_and_names_the_precheck_gate(env, monkeypatch):
    """#6939's false item: tasks still 'running' while parked on their own checks and another provider's live
    runs outnumber the cap, yet dispatch has a free slot on the waiting task's provider: no workers item. A
    review held by its daemon pre-checks waiting for a host slot raises a checks item, never a workers one.
    Only once the provider's own live runs fill its slots does the head of the line raise a workers item,
    naming those runs alone and no 'raise max_parallel_workers' when the cap is a budget gate's cut."""
    from ttp import daemon as dm, queuewait
    from ttp.cli import CHECKS_SLOT_WAIT
    p = make(env)
    p.set_config("budget.max_parallel_workers", 2)
    d = dm.Daemon(p.base)
    d.cfg = p.config()
    core, now = d.cfg.get("core_provider", "claude"), time.time()
    other = "codex" if core != "codex" else "claude"
    for i in range(4):   # parked on their own checks: status running, no live worker run
        p.db.update_task(p.db.add_task(f"parked {i}", "s", kind="code"), status="running")
    for i in range(3):   # another provider's live runs: its own slots
        t = p.db.add_task(f"other {i}", "s", kind="code", provider=other)
        p.db.update_task(t, status="running")
        p.db.x("INSERT INTO runs(task,role,started,status,provider) VALUES(?,?,?,'running',?)",
               (t, "worker", now - 3600, other))
    live = p.db.add_task("live", "s", kind="code")
    p.db.update_task(live, status="running")
    p.db.x("INSERT INTO runs(task,role,started,status,provider) VALUES(?,?,?,'running',?)",
           (live, "worker", now - 3600, core))
    code = p.db.add_task("c", "s", kind="code")
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (code,))
    out = p.state / dm.PRECHECKS_DIR / f"t{code}-ab12cd34ef56"
    out.mkdir(parents=True)
    (out / CHECKS_SLOT_WAIT).write_text(json.dumps({"since": now - 50 * 60, "pid": 1}))
    rev = p.db.add_task("review", "r", kind="review", priority=1, labels=[dm.PRECHECK_LABEL + out.name])
    ready = p.db.add_task("ready", "s", kind="code")
    p.db.x("UPDATE tasks SET created=? WHERE id IN (?,?)", (now - 50 * 60, rev, ready))
    monkeypatch.setattr(d, "_precheck_holds", lambda t, now: t["id"] == rev)
    cfg = {**QW_CFG, "budget": {"max_parallel_workers": 2}}
    slot = {"limit": 4, "running": [f"other task #{i} (run 1)" for i in range(4)], "queued": [f"{p.name} {out.name}"]}
    assert queuewait.collect(p, cfg, now, {}, hold=d._queue_hold(now)) == [], "0 queued on the host: no slot wait"
    got = {(w["task"], w["queue"]) for w in queuewait.collect(p, cfg, now, slot, hold=d._queue_hold(now))}
    assert got == {(rev, "checks")}, got   # 1 live run of 2 on its provider: dispatch starts `ready`
    assert {(w["task"], w["queue"]) for w in queuewait.collect(p, cfg, now, {})} == {(rev, "workers")}, \
        "the plain count (4 runs on two providers against 2) is the false item the hold removes"
    full = p.db.add_task("full", "s", kind="code")
    p.db.update_task(full, status="running")
    p.db.x("INSERT INTO runs(task,role,started,status,provider) VALUES(?,?,?,'running',?)",
           (full, "worker", now - 60, core))
    w = {x["queue"]: x for x in queuewait.collect(p, cfg, now, slot, hold=d._queue_hold(now))}
    assert set(w) == {"checks", "workers"} and w["workers"]["task"] == ready and w["workers"]["cap"] == 2, w
    assert sorted(w["workers"]["running"]) == sorted(f"{p.name} task #{t} (priority 3)" for t in (live, full)), w
    assert queuewait.options(p.db, {**w["workers"], "cap_why": "4 configured; the budget gate allows 2"},
                             {"budget": {"max_parallel_workers": 4}}, {}, cores=64)[0].startswith("cut the stale")
    opts = queuewait.options(p.db, w["workers"], cfg, {}, cores=64)
    assert "raise budget.max_parallel_workers from 2 to 3" in opts[0], opts
    import dataclasses
    from ttp import budget as bud
    gate = bud.evaluate(p.db, d.cfg, core, bud.plan_windows(p.db))
    monkeypatch.setitem(d.gates, core, dataclasses.replace(gate, allow_new_work=False))
    got = {(x["task"], x["queue"]) for x in queuewait.collect(p, cfg, now, slot, hold=d._queue_hold(now))}
    assert got == {(rev, "checks")}, f"the spend gate holds it, not the slots: {got}"
    monkeypatch.setitem(d.gates, core, gate)
    from ttp import checkcap
    monkeypatch.setattr(checkcap, "state", lambda: {"limit": 4, "running": [], "queued": slot["queued"], "parked": []})
    d.cfg.setdefault("coordinator", {})["queue_wait_escalate_min"] = 30
    d.check_queue_waits(every_s=0)   # the daemon's own scan passes its dispatch hold
    assert {(e["task"], json.loads(e["data"])["must_act"]["key"]) for e in _qw_events(p)} == \
        {(rev, f"qwait-{rev}-checks"), (ready, f"qwait-{ready}-workers")}


def test_queue_wait_item_shows_the_holders_when_read_not_when_raised(env):
    """#6957: an item raised before a slot changes hands is rewritten while unread, so a later read names the
    new holder and occupancy, not the one that let go; an acted item's wait ending still settles it."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    host = _qw_host(p, now, now - 40 * 60)
    assert len(queuewait.scan(p, QW_CFG, now, host, cores=64)) == 1
    assert "other task #90" in _qw_events(p, "queued")[0]["text"]
    later = {**host, "running": ["other task #500 (run 9)"]}
    assert queuewait.scan(p, QW_CFG, now + 120, later, cores=64) == []   # no new item: the open one is rewritten
    evs = _qw_events(p)
    assert len(evs) == 1 and evs[0]["status"] == "queued", evs
    t = evs[0]["text"]
    assert "other task #500" in t and "other task #90" not in t and "1 running (cap 4)" in t and "42 min" in t, t
    f = json.loads(evs[0]["data"])[queuewait.FOLD]   # NEW EVENTS folds by this data: it is rewritten too
    assert "other task #500" in f["ctx"] and "other task #90" not in f["ctx"] and "42 min" in f["item"], f


def test_queue_wait_start_flip_keeps_one_episode_and_a_short_gap_does_not_end_it(env):
    """The checks queue's queued_at and the hand-off's waiting_since name the same wait 60 s apart: a scan
    seeing only one of them never reopens the episode or raises again. A gap under GRACE_S parks the unread
    item and revives it when the wait shows again; a gap past it ends the episode and settles the item."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    tid = p.db.add_task("t", "s", kind="code")
    p.db.update_task(tid, not_before=now + 600)
    hand = {"status": "waiting", "retry_when": "ttp checks --result /x", "waiting_since": now - 40 * 60}
    q = {"limit": 4, "running": [], "priority": [], "urgent": [], "parked": [],
         "queued": [f"{p.name} task #{tid} (run 1)"], "queued_at": [now - 39 * 60]}
    p.db.update_task(tid, result=json.dumps(hand))
    assert len(queuewait.scan(p, QW_CFG, now, {})) == 1   # the hand-off's start
    p.db.update_task(tid, result=json.dumps({"status": "waiting"}))
    for i in range(1, 6):   # only the queue's start, 60 s later, then the hand-off's again
        p.db.update_task(tid, result=json.dumps(hand if i % 2 == 0 else {"status": "waiting"}))
        assert queuewait.scan(p, QW_CFG, now + i * 60, q) == []
    st = p.db.kv(queuewait.KV)
    assert list(st["open"]) == [f"{tid}:checks"] and st["open"][f"{tid}:checks"]["since"] == now - 40 * 60
    assert len(_qw_events(p, "queued")) == 1 and not st.get("hist")
    p.db.update_task(tid, result=json.dumps({"status": "waiting"}))
    queuewait.scan(p, QW_CFG, now + 11 * 60, {})   # gone for a while: its unread item is parked
    assert _qw_events(p, "queued") == [] and f"{tid}:checks" in p.db.kv(queuewait.KV)["open"]
    assert queuewait.scan(p, QW_CFG, now + 20 * 60, q) == []   # back within the grace: no new raise
    assert len(_qw_events(p, "queued")) == 1 and len(_qw_events(p)) == 1
    queuewait.scan(p, QW_CFG, now + 21 * 60, {})
    queuewait.scan(p, QW_CFG, now + 20 * 60 + queuewait.GRACE_S + 1, {})   # past the grace: ended
    st = p.db.kv(queuewait.KV)
    assert st["open"] == {} and len(st["hist"]) == 1 and _qw_events(p, "queued") == []


def test_queue_wait_a_later_episode_of_the_same_task_and_queue_is_raised(env):
    """An episode that ended past GRACE_S leaves its fingerprints in events: a later wait of the same task
    on the same queue opens a new episode and raises under its own fingerprint, not the ended one's."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    tid = p.db.add_task("dev run", "s", kind="work", origin="user")
    wait = lambda t: p.db.update_task(tid, not_before=t + 600, result=json.dumps(   # noqa: E731
        {"status": "waiting", "retry_when": "ttp lock --probe dev0", "waiting_since": t - 40 * 60}))
    wait(now)
    first = queuewait.scan(p, QW_CFG, now, {}, {"dev0"})
    assert len(first) == 1
    p.db.update_task(tid, result=json.dumps({"status": "waiting"}))
    queuewait.scan(p, QW_CFG, now + 60, {}, {"dev0"})
    later = now + 60 + queuewait.GRACE_S + 1
    queuewait.scan(p, QW_CFG, later, {}, {"dev0"})   # past the grace: the first episode ended
    assert p.db.kv(queuewait.KV)["open"] == {}
    wait(later + 3600)
    second = queuewait.scan(p, QW_CFG, later + 3600, {}, {"dev0"})
    assert len(second) == 1 and second != first
    (rec,) = p.db.kv(queuewait.KV)["open"].values()
    assert rec["raise"]["n"] == 1 and len(_qw_events(p)) == 2


def test_queue_wait_raises_again_only_on_a_changed_cause_or_a_doubled_wait(env):
    """Same cause: no raise at the next marks; the options running out (a user pause) is a new cause; a wait
    that doubles since its last raise is raised again."""
    from ttp import queuewait
    from ttp.db import PAUSED_RESOURCES_KEY
    p = make(env)
    now = time.time()
    tid = p.db.add_task("dev run", "s", kind="work", origin="user")
    p.db.update_task(tid, priority=1, not_before=now + 600, result=json.dumps(
        {"status": "waiting", "retry_when": "ttp lock --probe dev0", "waiting_since": now - 40 * 60}))
    seen = lambda t: [e for e in _qw_events(p) if e["ts"] == t]   # noqa: E731
    handle = lambda: p.db.x("UPDATE events SET status='handled' WHERE kind='queue_wait'")   # noqa: E731
    assert len(queuewait.scan(p, QW_CFG, now, {}, {"dev0", "dev1"})) == 1
    assert seen(now)[0]["severity"] == "high"   # not a checks wait: no ride-along
    handle()
    assert queuewait.scan(p, QW_CFG, now + 31 * 60, {}, {"dev0", "dev1"}) == []
    p.db.set_kv(PAUSED_RESOURCES_KEY, {"dev0": {"by": "user", "reason": "hold", "since": now}})
    assert len(queuewait.scan(p, QW_CFG, now + 32 * 60, {}, {"dev0", "dev1"})) == 1   # no option left now
    handle()
    assert queuewait.scan(p, QW_CFG, now + 60 * 60, {}, {"dev0", "dev1"}) == []   # 100 min: under 2 x 72
    fps = queuewait.scan(p, QW_CFG, now + 105 * 60, {}, {"dev0", "dev1"})   # 145 min: doubled
    assert [fp.rsplit(":", 1)[1] for fp in fps] == ["3"]


def test_queue_wait_checks_wait_with_a_free_slot_is_not_host_held(env):
    """A checks wait while a host slot is free is no host hold: it is raised high and starts a turn; with
    every slot taken it is raised at normal and rides along."""
    from ttp import pagesev, queuewait
    p = make(env)
    now = time.time()
    tid = p.db.add_task("t", "s", kind="code")
    lab = [f"{p.name} task #{tid} (run 1)"]
    free = {"limit": 4, "running": ["other task #1 (run 1)"], "priority": [], "urgent": [], "parked": [],
            "queued": lab, "queued_at": [now - 40 * 60]}
    queuewait.scan(p, QW_CFG, now, free)
    ev = _qw_events(p)[0]
    assert ev["severity"] == "high" and not pagesev.rides_along(ev)
    assert not queuewait.host_held({"queue": "checks", "cap": 4, "running": ["a"]})
    assert queuewait.host_held({"queue": "checks", "cap": 4, "running": ["a"] * 4})
    assert queuewait.host_held({"queue": "checks", "cap": None, "running": []})
    assert not queuewait.host_held({"queue": "push", "cap": 1, "running": ["a"]})


def test_queue_wait_old_episode_key_carries_over_without_a_raise(env):
    """An open episode recorded under the old `<task>:<queue>:<since>` key merges into the new key and adopts
    the current cause without a raise; an act recorded on the old key still lands."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    host = _qw_host(p, now, now - 40 * 60)
    old = f"7:checks:{now - 40 * 60:.0f}"
    p.db.set_kv(queuewait.KV, {"open": {old: {"task": 7, "queue": "checks", "res": "", "since": now - 40 * 60,
                                              "acted": None}}})
    assert queuewait.scan(p, QW_CFG, now, host) == []
    assert list(p.db.kv(queuewait.KV)["open"]) == ["7:checks"]
    queuewait.record_act(p.db, old, now + 60)
    assert p.db.kv(queuewait.KV)["open"]["7:checks"]["acted"] == now + 60

def test_queue_wait_raise_carries_fold_data(env):
    """Each raise's data carries the parts NEW EVENTS folds by queue (coordinator.fold_events): the key, the
    options and the queue's state; the event text is the item, the queue, the state and the numbered options."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    host = _qw_host(p, now, now - 40 * 60)
    assert len(queuewait.scan(p, QW_CFG, now, host, cores=64)) == 1
    first = _qw_events(p)[0]
    f = json.loads(first["data"])[queuewait.FOLD]
    assert f["group"] == "the checks queue" and f["item"] == "qwait-7-checks: #7 has waited 40 min", f
    assert f["ctx"].startswith("4 running (cap 4), 20 queued. Holding: other task #90"), f
    assert any("raise the host-wide checks cap from 4 to 6" in o for o in f["opts"]), f
    assert first["text"].startswith(f"{f['item']} on the checks queue: {f['ctx']} Options: (1) "), first["text"]


def test_queue_wait_counts_a_detached_push_lock_holder_and_raises_nothing_while_it_lives(env):
    """A `ttp push --detach` never writes push_queue: it holds the branch push lock and runs a marker.
    The push wait counts the lock's live holder (step, minutes held) and the other running markers, and
    raises nothing while the holder lives within its limit; a dead holder (lock free) or an overdue one
    still raises, naming who holds it."""
    from ttp import locks, push, queuewait
    assert queuewait.PUSH_HOLD_LIMIT_S == push.CHECK_LIMIT_FLOOR_S
    p = make(env)
    now = time.time()
    tid = p.db.add_task("review", "s", kind="review")
    p.db.update_task(tid, not_before=now + 600, result=json.dumps(
        {"status": "waiting", "retry_when": "ttp push --result /r/m", "waiting_since": now - 40 * 60}))
    assert p.db.q("SELECT id FROM push_queue") == []
    paths = push.lock_paths(p, "origin", "team/feature")
    held = []
    try:
        f = locks.try_take(paths, "task #41 (run 9)", "ttp push")   # as push.take labels it
        held.append(f)
        push.Steps(f).go("checks")
        folder = p.state / push.DETACHED
        folder.mkdir(parents=True, exist_ok=True)
        for rid, task in (("m-holder", 41), ("m-wait", 42)):
            marker = folder / f"{rid}.json"
            log = marker.with_suffix(".log")
            log.write_text(f"ttp push: waiting up to 1831 s for push:origin/team/feature (held by task #41)\n"
                           if task == 42 else "ttp push: running the checks\n")
            marker.write_text(json.dumps({"id": rid, "status": "running", "task": task, "log": str(log),
                                          "target": "origin/team/feature",
                                          "pid": os.getpid() if task == 41 else 999999}))
            held.append(locks.try_take([push._run_lock(marker)], f"task #{task}", "ttp push --detach"))
        info = queuewait._holders(p.db, p, "push", "", QW_CFG, now)
        assert info.pop("live") is True and len(info["running"]) == 1, info
        assert "task #41" in info["running"][0] and "step checks" in info["running"][0], info
        assert len(info["queued"]) == 1 and "task #42" in info["queued"][0] \
            and "waiting up to 1831 s" in info["queued"][0], info
        assert [w for w in queuewait.collect(p, QW_CFG, now) if w["queue"] == "push"] == []
        queuewait.scan(p, QW_CFG, now)
        assert _qw_events(p) == [], "no wait raised behind a live push in its turn"
        # Held past its limit: overdue, raised, and the text names the holder.
        later = now + queuewait.PUSH_HOLD_LIMIT_S + 60
        queuewait.scan(p, QW_CFG, later)
        ev = _qw_events(p)
        assert len(ev) == 1 and "1 running (cap 1)" in ev[0]["text"] and "task #41" in ev[0]["text"] \
            and "Holding: none seen" not in ev[0]["text"], ev
        locks.relabel(f, {"since": "garbled"})
        info = queuewait._holders(p.db, p, "push", "", QW_CFG, now)
        assert not info.get("live") and len(info["running"]) == 1, "an unreadable start is never within its limit"
    finally:
        for f in held:
            f.close()
    # Its pid gone, the lock is free: nothing holds it, so the wait is raised as before.
    info = queuewait._holders(p.db, p, "push", "", QW_CFG, now)
    assert not info.get("live") and info["running"] == [] and info["queued"] == [], info
    assert [w["task"] for w in queuewait.collect(p, QW_CFG, now) if w["queue"] == "push"] == [tid]


def test_a_push_batch_holding_its_branch_and_run_locks_is_a_holder_not_a_waiter(env):
    from ttp import locks, push, queuewait
    p = make(env)
    now = time.time()
    bid = "batch-20261111-080135-1"
    held = []
    try:
        held.append(locks.try_take(push.lock_paths(p, "origin", "team/feature"), f"push batch {bid}", "the push queue"))
        folder = p.state / push.DETACHED
        folder.mkdir(parents=True, exist_ok=True)
        marker = folder / f"{bid}.json"
        log = marker.with_suffix(".log")
        log.write_text("ttp push: running the checks\n")
        marker.write_text(json.dumps({"id": bid, "kind": "batch", "status": "running", "log": str(log),
                                      "target": "origin/team/feature", "pid": None}))
        held.append(locks.try_take([push._run_lock(marker)], f"push batch {bid}", "the push queue"))
        info = queuewait._holders(p.db, p, "push", "", QW_CFG, now)
        assert len(info["running"]) == 1 and info["queued"] == [], info
    finally:
        for f in held:
            f.close()


def _qw_reset(p):
    from ttp import queuewait
    p.db.x("DELETE FROM events WHERE kind='queue_wait'")
    p.db.set_kv(queuewait.KV, {})


def _qw_answered(p, now, cap=4, depth=20):
    """Task a's routine checks wait raised and answered with a task_update naming its key (no fix owner);
    returns (a, b, host_of(tasks, cap, depth)): b is a second routine task."""
    from ttp import mustact, queuewait
    a, b = p.db.add_task("a", "s", kind="code"), p.db.add_task("b", "s", kind="code")

    def host_of(tasks, cap=cap, depth=depth, at=None):
        q = [f"other task #{900 + i} (run 1)" for i in range(depth - len(tasks))] + \
            [f"{p.name} task #{t} (run 1)" for t in tasks]
        return {"limit": cap, "running": [f"other task #{90 + i} (run 1)" for i in range(cap)], "priority": [],
                "urgent": [], "parked": [], "queued": q,
                "queued_at": [now - 600] * (depth - len(tasks)) + [at or now - 40 * 60] * len(tasks)}
    assert len(queuewait.scan(p, QW_CFG, now, host_of([a]))) == 1
    ev = _qw_events(p)[0]
    p.db.x("UPDATE events SET status='handled' WHERE id=?", (ev["id"],))
    mustact.settle(p, [ev["id"]], [{"type": "task_update", "id": a, "spec": f"qwait-{a}-checks: held by the cap"}],
                   [0], now)
    return a, b, host_of


def test_queue_wait_answer_covers_routine_waits_under_the_same_cause(env):
    """#7961: once the coordinator answered one routine wait's item, a second routine task waiting on the same
    queue under the same cause raises nothing; it is counted, and the queue's line names the count."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    a, b, host_of = _qw_answered(p, now)
    assert queuewait.scan(p, QW_CFG, now + 60, host_of([a, b])) == []
    rec = next(r for r in p.db.kv(queuewait.KV)["open"].values() if r["task"] == b)
    assert rec.get("covered") == f"qwait-{a}-checks"
    assert queuewait._covered_note(p.db.kv(queuewait.KV), {"task": a, "queue": "checks", "res": ""}).startswith(
        f"1 more wait(s) on this queue are covered by the answer to qwait-{a}-checks")
    queuewait.scan(p, QW_CFG, now + 120, {})
    assert "1 covered by an earlier answer" in queuewait.daily_line(p.db, QW_CFG, now + 120)


def test_queue_wait_answer_ends_when_its_cause_changes(env):
    """A cap change, a halved queue or a wait past three times the answered one raises the routine wait."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    a, b, host_of = _qw_answered(p, now)
    assert [fp.split(":")[1] for fp in queuewait.scan(p, QW_CFG, now + 60, host_of([a, b], cap=6))] == [str(b)]
    _qw_reset(p)
    a, b, host_of = _qw_answered(p, now)
    assert [fp.split(":")[1] for fp in queuewait.scan(p, QW_CFG, now + 60, host_of([a, b], depth=10))] == [str(b)]
    _qw_reset(p)
    a, b, host_of = _qw_answered(p, now)
    host = host_of([a, b])
    host["queued_at"][-1] = now - 130 * 60   # b has waited over 3x a's answered 40 min
    assert [fp.split(":")[1] for fp in queuewait.scan(p, QW_CFG, now + 60, host)] == [str(b)]


def test_queue_wait_answer_ends_when_a_spill_host_changes(env):
    """A spill host whose admission changed (here paused) is a new cause: the routine wait raises."""
    from ttp import db as dbm
    from ttp import machines, mustact, queuewait
    p = make(env)
    now = time.time()
    hosts = ["check-host-a", "check-host-b"]
    p.charter_path.write_text("# Charter\n## Resources\n" + "".join(f"- {h}: a check host\n" for h in hosts))
    for h in hosts:
        machines.add(h)
    cfg = {**QW_CFG, "delivery": {"checks_spill_hosts": hosts}}
    a, b = p.db.add_task("a", "s", kind="code"), p.db.add_task("b", "s", kind="code")
    host = {"limit": 4, "running": [f"other task #{90 + i} (run 1)" for i in range(4)], "priority": [], "urgent": [],
            "parked": [], "queued": [f"{p.name} task #{a} (run 1)", f"{p.name} task #{b} (run 1)"],
            "queued_at": [now - 40 * 60] * 2}
    assert len(queuewait.scan(p, cfg, now, {**host, "queued": host["queued"][:1], "queued_at": [now - 40 * 60]})) == 1
    ev = _qw_events(p)[0]
    p.db.x("UPDATE events SET status='handled' WHERE id=?", (ev["id"],))
    mustact.settle(p, [ev["id"]], [{"type": "reply", "chat": "all", "text": f"qwait-{a}-checks: held by the cap"}],
                   [0], now)
    assert queuewait.scan(p, cfg, now + 30, host) == []   # same spill hosts: covered
    p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"check-host-a": {"reason": "user hold", "by": "user", "since": now}})
    assert [fp.split(":")[1] for fp in queuewait.scan(p, cfg, now + 60, host)] == [str(b)]


def test_queue_wait_answer_never_covers_an_urgent_or_owed_wait(env):
    """An urgent (priority 1 or an `urgent:` line) or user-ordered task keeps its own item under an answer."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    a, b, host_of = _qw_answered(p, now)
    p.db.update_task(b, priority=1)
    assert [fp.split(":")[1] for fp in queuewait.scan(p, QW_CFG, now + 60, host_of([a, b]))] == [str(b)]
    _qw_reset(p)
    a, b, host_of = _qw_answered(p, now)
    p.db.update_task(b, spec="urgent: the user is waiting")
    assert [fp.split(":")[1] for fp in queuewait.scan(p, QW_CFG, now + 60, host_of([a, b]))] == [str(b)]
    _qw_reset(p)
    a, b, host_of = _qw_answered(p, now)
    p.db.update_task(b, reply_chat="c1")
    assert [fp.split(":")[1] for fp in queuewait.scan(p, QW_CFG, now + 60, host_of([a, b]))] == [str(b)]


def test_queue_wait_answered_key_stays_settled_across_a_new_episode(env):
    """#7961 (a key answered with a task_update and raised again 7 min later): the same task's wait coming back
    after its episode ended (GRACE_S with no sign of it) is a new episode, still covered by the answer while the
    cause holds; turned urgent, it raises."""
    from ttp import queuewait
    p = make(env)
    now = time.time()
    a, b, host_of = _qw_answered(p, now)
    gap = now + queuewait.GRACE_S + 60   # b keeps the queue held while a's wait is gone: a's episode ends
    assert queuewait.scan(p, QW_CFG, gap, host_of([b], at=now - 40 * 60)) == []
    assert not any(r["task"] == a for r in p.db.kv(queuewait.KV)["open"].values())
    both = host_of([a, b], at=now - 40 * 60 + 47)
    assert queuewait.scan(p, QW_CFG, gap + 60, both) == []
    p.db.update_task(a, priority=1)
    assert [fp.split(":")[1] for fp in queuewait.scan(p, QW_CFG, gap + 120, both)] == [str(a)]