# Page severity tests of tests/test_tt_project_runtime.py (pagesev.py: continue_unqueued, task_blocked and
# machine_change_overdue), which runs this file in its own namespace (see _include_cases there): helpers,
# fixtures and imports come from that file, and pytest collects these tests under its name.
from __future__ import annotations


def _sev_task(p, how):
    """A task that is plain, priority 1, has an `urgent:` line, owns an open obligation or waits on an ask."""
    from ttp import anchors, obligations
    spec = "urgent: the release waits on it\ndo it" if how == "urgent_line" else "do it"
    tid = p.db.add_task(f"task {how}", spec, priority=1 if how == "priority1" else 3, status="blocked",
                        labels=[anchors.label("ask", "7")] if how == "ask" else [])   # an anchor lives while blocked
    if how == "owner":
        obligations.add(p, "a promised report", owners=[tid])
    return p.db.task(tid)


def test_pagesev_continue_unqueued_is_normal_unless_the_task_is_urgent(env):
    from ttp import pagesev
    p = make(env)
    want = {"plain": "normal", "priority1": "high", "urgent_line": "high", "owner": "high",
            "ask": "normal"}   # a waits:ask hold makes a block high, not a forgotten follow-up
    assert {how: pagesev.continue_severity(p.db, _sev_task(p, how)) for how in want} == want
    assert pagesev.urgent(p.db, None) is False


def test_pagesev_rides_along_reads_only_the_data_flag_of_a_routine_event():
    from ttp import pagesev
    flag = json.dumps({pagesev.RIDE_ALONG: True})
    cases = [({"severity": "normal", "data": flag}, True), ({"severity": "low", "data": flag}, True),
             ({"severity": "normal", "data": {pagesev.RIDE_ALONG: True}}, True),
             ({"severity": "high", "data": flag}, False), ({"severity": "normal", "data": None}, False),
             ({"severity": "normal", "data": "not json"}, False), ({"severity": "normal", "data": "[1]"}, False),
             ({"severity": "normal", "data": json.dumps({pagesev.RIDE_ALONG: "yes"})}, False)]
    assert [pagesev.rides_along(e) for e, _ in cases] == [want for _, want in cases]


def _ride(p, ts, kind="continue_unqueued"):
    from ttp import pagesev
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
           (ts, "task:1", kind, "normal", f"{kind} happened", json.dumps({pagesev.RIDE_ALONG: True}), "queued", 1))


def test_pagesev_a_ride_along_event_starts_no_turn_alone_before_its_hour(env, monkeypatch):
    from ttp import pagesev
    from ttp.daemon import Daemon
    p, d, clock, starts = _batch_setup(env, monkeypatch, busy=0)
    p.set_config("coordinator.idle_wake_s", 6 * 3600)
    p.set_config("coordinator.starve_wake_s", 6 * 3600)
    d.cfg = p.config()
    p.db.set_kv("last_coordinator_turn", clock[0])
    _ride(p, clock[0])
    assert not _turn_within(d, clock, starts, pagesev.RIDE_ALONG_MAX_S - 120, step=60), "it woke a turn by itself"
    # A daemon restart keeps it queued and keeps its clock: the event's own time, not the new daemon's.
    d = Daemon(p.base)
    d.cfg = p.config()
    d.update_gates()
    monkeypatch.setattr(d, "start_run", lambda *a, **k: starts.append(clock[0]))
    assert p.db.one("SELECT status FROM events WHERE kind='continue_unqueued'")["status"] == "queued"
    assert _turn_within(d, clock, starts, 600, step=30), "it waited past RIDE_ALONG_MAX_S"
    assert len(starts) == 1


def test_pagesev_a_ride_along_event_joins_the_turn_another_event_starts(env, monkeypatch):
    p, d, clock, starts = _batch_setup(env, monkeypatch, busy=0)
    p.set_config("coordinator.idle_wake_s", 6 * 3600)
    p.set_config("coordinator.starve_wake_s", 6 * 3600)
    d.cfg = p.config()
    p.db.set_kv("last_coordinator_turn", clock[0])
    _ride(p, clock[0])
    assert not _turn_within(d, clock, starts, 600, step=30)
    _event(p, clock, "task_failed", "high")
    assert _turn_within(d, clock, starts, 300), "a high event did not start its turn"
    assert len(starts) == 1


def test_pagesev_raise_blocked_counts_repeats_and_keeps_urgent_blocks_high(env):
    from ttp import pagesev
    p = make(env)
    t = _sev_task(p, "plain")
    now = time.time()

    def rows():
        return p.db.q("SELECT id, severity, status, text, data FROM events WHERE kind='task_blocked' AND task=? "
                      "ORDER BY id", (t["id"],))
    assert pagesev.raise_blocked(p.db, t, "push refused: 3 checks failed", "#1 x → blocked", now) == "normal"
    # Again within 6 h while the first is queued (numbers in the reason do not make it new): counted on it.
    assert pagesev.raise_blocked(p.db, t, "push refused: 5 checks failed", "#1 x → blocked", now + 600) == "counted"
    assert pagesev.raise_blocked(p.db, t, "push refused: 7 checks failed", "#1 x → blocked", now + 1200) == "counted"
    (ev,) = rows()
    assert ev["severity"] == "normal" and "blocked again 2x" in ev["text"] and json.loads(ev["data"])["repeats"] == 2
    # Once a turn read it, a repeat is one ride-along event, itself counted on while queued.
    p.db.x("UPDATE events SET status='handled' WHERE id=?", (ev["id"],))
    assert pagesev.raise_blocked(p.db, t, "push refused: 1 checks failed", "#1 x → blocked", now + 1800) == "ride"
    assert pagesev.raise_blocked(p.db, t, "push refused: 2 checks failed", "#1 x → blocked", now + 2400) == "counted"
    first, ride = rows()
    assert pagesev.rides_along(ride) and ride["status"] == "queued" and json.loads(ride["data"])["repeats"] == 2
    # Another reason, or the same one after 6 h, is new.
    assert pagesev.raise_blocked(p.db, t, "rebooted mid-job", "#1 x → blocked", now + 2400) == "normal"
    p.db.x("UPDATE events SET status='handled' WHERE kind='task_blocked'")
    assert pagesev.raise_blocked(p.db, t, "push refused: 1 checks failed", "#1 x → blocked",
                                 now + 2400 + pagesev.REPEAT_WINDOW_S + 1) == "normal"
    # Urgent tasks and blocks on an ask are high every time, never folded.
    for how in ("priority1", "urgent_line", "owner", "ask"):
        u = _sev_task(p, how)
        assert [pagesev.raise_blocked(p.db, u, "x", "y", now + i) for i in range(2)] == ["high", "high"], how


def test_pagesev_push_refused_block_uses_raise_blocked(env):
    """pushq's refused branch and the daemon's boot block both go through pagesev.raise_blocked."""
    import inspect
    from ttp import daemon, pushq
    assert "pagesev.raise_blocked(" in inspect.getsource(pushq._settle)
    assert "pagesev.raise_blocked(" in inspect.getsource(daemon.Daemon._block_after_reboot)
    assert '"task_blocked", "high"' not in inspect.getsource(daemon.Daemon._block_after_reboot)


def test_pagesev_change_severity_is_high_only_for_a_live_machine_past_its_end_unconfirmed():
    from ttp import pagesev
    now = 1_000_000.0
    past, future = now - 1, now + 3600
    cases = [  # (change, serving, want)
        ({"expires": past}, True, "high"),
        ({"expires": past}, False, "normal"),
        ({"expires": future}, True, "normal"),
        ({"expires": future, "task_ended": True}, True, "high"),
        ({"expires": past, "probe": "test -e /x", "probe_passed": True}, True, "normal"),
        ({"expires": past, "probe": "test -e /x"}, True, "high"),
        ({"expires": past, "probe": "test -e /x", "probe_passed": True, "probe_job_failed": True}, True, "high"),
        ({}, True, "normal"),
    ]
    assert [pagesev.change_severity(c, now, s) for c, s, _ in cases] == [w for _, _, w in cases]
    tagged = [({"tags": ["device", "serving"]}, True), ({"tags": "device, live"}, True), ({"tags": "Server"}, True),
              ({"tags": ["device"]}, False), ({"tags": ["preserving"]}, False), ({}, False), (None, False)]
    assert [pagesev.serving_machine(e) for e, _ in tagged] == [w for _, w in tagged]
    assert pagesev.change_fingerprint("machine-change:3", 12.7) == "machine-change:3@12"


def test_pagesev_overdue_change_pages_carry_severity_and_one_fingerprint_per_episode(env):
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import pagesev
    from ttp.daemon import Daemon
    alpha = make(env, "alpha")
    mm.add("box-a", tags="device")
    mm.add("box-b", tags="device,serving")
    now = time.time()
    ml.add_change("box-a", "drop-in", "rm it", "alpha", expires="1h", now=now - 7200)
    ml.add_change("box-b", "drop-in", "rm it", "alpha", expires="1h", now=now - 7200)
    pages = sorted(ml.route(now, able=lambda n: (True, ""), claimer="alpha"), key=lambda x: x["alias"])
    assert [(x["alias"], x["severity"]) for x in pages] == [("box-a", "normal"), ("box-b", "high")]
    assert [x["fp"] for x in pages] == [pagesev.change_fingerprint(f"machine-change:{i}", now - 7200) for i in (1, 2)]
    d = Daemon(alpha.base)
    for x in pages + pages:   # a resend of the same page, or of a later step's, files nothing new
        assert d._machine_page(x, now)
    assert d._machine_page({**pages[0], "id": pages[0]["id"] + "-step2", "stage": 1}, now)
    rows = alpha.db.q("SELECT fingerprint, severity, status FROM events WHERE kind='machine_change_overdue' ORDER BY id")
    assert [(r["fingerprint"], r["severity"], r["status"]) for r in rows] == \
        [(pages[0]["fp"], "normal", "queued"), (pages[1]["fp"], "high", "queued")]


def test_pagesev_a_relayed_copy_of_a_page_this_project_has_is_filed_handled(env):
    from ttp import upstream
    p = make(env)
    p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
           (time.time(), "daemon", "machine_change_overdue", "machine-change:4@100", "normal", "change 4 overdue", "queued"))
    for fp in ("machine-change:4@100", "machine-change:5@100"):
        assert upstream.send("peer", None, p.name, f"Machine change overdue ({fp})", "high", sender="daemon",
                             extra={"page_fp": fp}) == "sent"
    # A worker's note cannot claim a page fingerprint: send keeps it for daemon pages only.
    assert upstream.send("peer", 1, p.name, "a worker note", "high", extra={"page_fp": "machine-change:4@100"}) == "sent"
    upstream.ingest(p, p.config())
    rows = {r["text"]: r for r in p.db.q("SELECT fingerprint, severity, status, text FROM events WHERE source='upstream'")}
    dup = next(r for t, r in rows.items() if "machine-change:4@100" in t)
    new = next(r for t, r in rows.items() if "machine-change:5@100" in t)
    worker = next(r for t, r in rows.items() if "a worker note" in t)
    assert (dup["severity"], dup["status"], dup["fingerprint"]) == ("low", "handled", None)
    assert new["status"] == "queued" and new["fingerprint"] == "machine-change:5@100" and new["severity"] != "low"
    assert worker["status"] == "queued" and worker["fingerprint"] is None
    # A second relayed copy of the new one is then a duplicate too.
    assert upstream.send("other", None, p.name, "Machine change overdue (machine-change:5@100) again", "high",
                         sender="daemon", extra={"page_fp": "machine-change:5@100"}) == "sent"
    upstream.ingest(p, p.config())
    again = p.db.one("SELECT severity, status FROM events WHERE source='upstream' AND text LIKE '%again%'")
    assert (again["severity"], again["status"]) == ("low", "handled")
