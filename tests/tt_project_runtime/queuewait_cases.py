# Queue-wait escalation tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects these
# tests under its name. Add new queuewait.py tests (detector, options, must-act settle, daily metric) here.
from __future__ import annotations

QW_CFG = {"coordinator": {"queue_wait_escalate_min": 30}, "budget": {"max_parallel_workers": 6}}


def _qw_host(p, now, mine_at, cap=4, queued=20, limit_set=False):
    """A fake host checks state: `cap` running (cap `cap`), `queued` queued, one of them task 7 of `p`."""
    q = [f"other task #{i} (run 1) ttp checks" for i in range(queued - 1)] + [f"{p.name} task #7 (run 3) ttp checks"]
    return {"limit": cap, "running": [f"other task #{90 + i} (run 1)" for i in range(cap)], "queued": q,
            "queued_at": [now - 600] * (queued - 1) + [mine_at], "priority": [], "urgent": [], "parked": [],
            "limit_set": limit_set}


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


@pytest.mark.parametrize("probe,reason,want", [
    ("ttp checks --result /r/1", "", ("checks", "")),
    ("", "next try 04:50 PST", ("checks", "")),
    ("ttp push --result /r/m", "", ("push", "")),
    ("ttp lock --probe dev0", "", ("device", "dev0")),
    ("ttp lock --probe 'build-dir'", "", ("lock", "build-dir")),
    ("ttp pool-wait --check x", "", ("pool", "")),
    ("", "waiting for CPU admission on the host", ("cpu", "")),
    ("ttp detach --check /r/job", "", None),
    ("echo ttp checks", "", None),
    ("landed:#12", "", None),
])
def test_queue_wait_classifies_each_queue(probe, reason, want):
    from ttp import queuewait
    assert queuewait.classify(probe, reason, {"dev0"}) == want


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
    host = {"limit": 4, "running": [], "priority": [], "urgent": [], "parked": [],
            "queued": [f"{p.name} task #{i} (run 1)" for i in range(1, 6)],
            "queued_at": [now - (40 + 10 * i) * 60 for i in range(1, 6)]}
    first = queuewait.scan(p, QW_CFG, now, host)
    assert [fp.split(":")[1] for fp in first] == ["5", "4", "3"]
    assert sorted(fp.split(":")[1] for fp in queuewait.scan(p, QW_CFG, now + 60, host)) == ["1", "2"]
    assert queuewait.scan(p, QW_CFG, now + 120, host) == []
