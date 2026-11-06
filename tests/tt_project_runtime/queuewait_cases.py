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
    from ttp import mustact, queuewait
    p = make(env)
    now = time.time()
    host = _qw_host(p, now, now - 40 * 60)
    assert queuewait.scan(p, QW_CFG, now - 15 * 60, host) == []   # 25 min: under the threshold
    fps = queuewait.scan(p, QW_CFG, now, host, cores=64)
    assert len(fps) == 1 and queuewait.scan(p, QW_CFG, now + 60, host, cores=64) == []
    ev = _qw_events(p)[0]
    assert ev["kind"] in mustact.KINDS and ev["severity"] == "high" and ev["task"] == 7
    t = ev["text"]
    assert "qwait-7-checks" in t and "40 min" in t and "4 running (cap 4), 20 queued" in t, t
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


def test_queue_wait_settle_records_an_act_or_raises_again_at_the_next_mark(env):
    """No acting action (a plain reply to every chat counts not): no immediate re-wake, the next 30-minute
    mark raises it again. A config_set or a reply naming the key settles it for this wait."""
    from ttp import mustact, queuewait
    p = make(env)
    now = time.time()
    host = _qw_host(p, now, now - 40 * 60)
    queuewait.scan(p, QW_CFG, now, host)
    ev = _qw_events(p)[0]
    p.db.x("UPDATE events SET status='handled' WHERE id=?", (ev["id"],))
    mustact.settle(p, [ev["id"]], [{"type": "reply", "chat": "all", "text": "ok"}], [0], now)
    assert not p.db.q("SELECT id FROM events WHERE kind='must_act_rewake'")
    assert queuewait.scan(p, QW_CFG, now + 5 * 60, host) == []   # the same mark
    assert len(queuewait.scan(p, QW_CFG, now + 31 * 60, host)) == 1   # the next mark
    ev2 = _qw_events(p)[-1]
    p.db.x("UPDATE events SET status='handled' WHERE id=?", (ev2["id"],))
    act = {"type": "reply", "chat": "all", "text": "qwait-7-checks: held by the host checks cap, a safety cap"}
    mustact.settle(p, [ev2["id"]], [act], [0], now + 32 * 60)
    assert queuewait.scan(p, QW_CFG, now + 70 * 60, host) == []
    line = queuewait.daily_line(p.db, QW_CFG, now + 70 * 60)
    assert "median 42" in line and "0 with none" in line, line
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
        {"status": "waiting", "retry_when": "ttp checks --result /tmp/x", "waiting_since": now - 35 * 60}))
    cfg = {**QW_CFG, "budget": {"max_parallel_workers": 1}}
    got = {(w["task"], w["queue"]) for w in queuewait.collect(p, cfg, now, {})}
    assert (ready, "workers") in got and (poll, "checks") in got, got
    host = {"running": [f"{p.name} task #{poll} (run 2) ttp checks"], "limit": 4}
    assert (poll, "checks") not in {(w["task"], w["queue"]) for w in queuewait.collect(p, cfg, now, host)}
    assert len(queuewait.scan(p, cfg, now, {})) == 2


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
    f2 = p.db.add_task("fix again", "s", kind="code")   # the next mark, with the mute ending instead of its owner
    p.db.set_kv(screen.MUTES_KEY, [{"source": "wait_stale", "match": "host-wide checks slot", "below": "high",
                                    "why": f"#{f2}", "since": now, "until": now + 35 * 60, "count": 0,
                                    "throughput": {"owner": {"task": f2}, "max_h": 12}}])
    assert queuewait.scan(p, QW_CFG, now + 31 * 60, host) == []
    assert len(queuewait.scan(p, QW_CFG, now + 36 * 60, host)) == 1


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
