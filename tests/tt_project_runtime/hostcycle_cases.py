# Planned host cycle tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new host-cycle hold tests here, not there.
from __future__ import annotations


def _hold_setup(env, monkeypatch):
    """Two projects on this host (testhost, listed in `ttp machines`): `demo` drains for a reboot,
    `other` is a co-tenant."""
    from ttp import machines as mm
    p, q = make(env), _second_project(env)
    mm.add("testhost")
    for k in ("TTP_PROJECT", "TTP_RUN_ID", "TTP_RUN_DIR", "TTP_TASK"):
        monkeypatch.delenv(k, raising=False)
    return p, q


def _hold_daemon(base, calls):
    """A Daemon whose dispatch and start_pushes only record that they ran."""
    from ttp.daemon import Daemon
    d = Daemon(base)
    d.dispatch = lambda: calls.append("dispatch")
    d.start_pushes = lambda: calls.append("push")
    d.maybe_coordinate = lambda: calls.append("coordinate")
    return d


def test_drain_reboot_holds_new_runs_checks_and_pushes_in_every_project_on_the_host(env, monkeypatch, capsys, tmp_path):
    from ttp import cli, hostcycle
    from ttp import coordinator as coord
    from ttp import machine_ledger as ml
    from ttp.runner import boot_id
    p, q = _hold_setup(env, monkeypatch)
    calls: list[str] = []
    d = _hold_daemon(q.base, calls)
    d.tick()
    assert "dispatch" in calls and "push" in calls, "no cycle announced: work starts as usual"
    now = time.time()
    cli.main(["drain", "demo", "--reboot", "firmware update"])
    out = capsys.readouterr().out
    (c,) = ml.changes(["testhost"])
    assert c["kind"] == ml.POWER_CYCLE and c["by"] == "demo" and c["boot"] == boot_id() \
        and c["reason"] == "firmware update" and c["at"] == pytest.approx(now + 1, abs=5)
    assert f"machine change #{c['id']}" in out and "planned host cycle recorded" in out
    (f,) = (p.state / hostcycle.FOLDER).glob("*/record.json")
    assert json.loads(f.read_text())["intent"] == c["id"]
    cli.main(["drain", "demo", "--reboot", "firmware update"])
    assert "already announced by demo" in capsys.readouterr().out and len(ml.changes()) == 1, "one intent per cycle"
    # The co-tenant's daemon starts no worker run or push; its coordinator still runs.
    calls.clear()
    d.tick()
    assert "coordinate" in calls and "dispatch" not in calls and "push" not in calls, calls
    # Status and digest say so.
    line = hostcycle.hold_line(c, q)
    assert line.startswith("holding new runs: planned host cycle by demo at ") and line.endswith("(firmware update)")
    assert line in cli.status_text(q) and line in coord.digest(q, {}, [], [])
    # `ttp checks` starts nothing new.
    monkeypatch.setenv("TTP_PROJECT", str(q.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", "true"])
    assert e.value.code == 75 and "ttp checks: not started, holding new runs" in capsys.readouterr().err


@pytest.mark.parametrize("lift", ["timeout", "closed", "new_boot", "no_reboot"])
def test_the_host_cycle_hold_lifts(env, monkeypatch, capsys, lift):
    """The hold ends once the intent closes, a new boot is seen, its record says no reboot (the daemon
    closes it), or POWER_CYCLE_LEAD_S after its time; that last adds one low feed line."""
    from ttp import cli, hostcycle, runner
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    cli.main(["drain", "demo", "--reboot", "bus reset", "--wait", "0"])
    capsys.readouterr()
    (c,) = ml.changes()
    calls: list[str] = []
    d = _hold_daemon(q.base, calls)
    now = c["at"]
    assert d.host_cycle_held(now)["id"] == c["id"]
    assert hostcycle.hold(q, c["at"] - ml.POWER_CYCLE_LEAD_S - 1) is None, "not due yet"
    if lift == "timeout":
        now = c["at"] + ml.POWER_CYCLE_LEAD_S
    elif lift == "closed":
        ml.close_change(c["id"], "done")
    elif lift == "new_boot":
        monkeypatch.setattr(runner, "boot_ids", lambda: ("a-later-boot", "a-later-boot"))
    else:
        (f,) = (p.state / hostcycle.FOLDER).glob("*/record.json")
        f.write_text(json.dumps({**json.loads(f.read_text()), "rebooted": False}))
    feed = "SELECT text FROM messages WHERE kind='info' AND severity='low' AND text LIKE '%did not come%'"
    assert d.host_cycle_held(now) is None and hostcycle.hold(q, now) is None
    assert d.host_cycle_held(now) is None
    assert len(q.db.q(feed)) == (1 if lift == "timeout" else 0), "one low feed line, only on a timeout lift"
    if lift == "no_reboot":
        assert not ml.changes() and "no reboot" in ml.changes(open_only=False)[0]["closed_why"]
    if lift != "timeout":   # the tick reads the real clock, still inside the timeout's window
        calls.clear()
        d.tick()
        assert "dispatch" in calls and "push" in calls


def test_drain_reboot_waits_for_and_lists_other_projects_runs_with_its_own_exit_code(env, monkeypatch, capsys):
    from ttp import cli
    from ttp.project import Project
    p, q = _hold_setup(env, monkeypatch)
    rid = q.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(7,'worker','claude',?,'running')",
                 (time.time(),))
    t0 = time.time()
    cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "0.3"])   # exit 0: its own runs decide
    out = capsys.readouterr().out
    assert time.time() - t0 >= 0.3, "the wait lasts while another project's run is in flight"
    assert "other projects on this host: 1 run(s) or push(es) in flight" in out and f"other run {rid} (worker, task #7)" in out
    # The other project's run ends during the wait: drain returns early.
    p.db.set_paused(False)
    threading.Timer(0.2, lambda: Project(q.base).db.x("UPDATE runs SET status='done', ended=? WHERE id=?",
                                                      (time.time(), rid))).start()
    t0 = time.time()
    cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "10"])
    assert time.time() - t0 < 8 and "other projects on this host" not in capsys.readouterr().out
    # Its own run still decides the exit code: 1 at the timeout.
    p.db.x("INSERT INTO runs(role,provider,started,status) VALUES('worker','claude',?,'running')", (time.time(),))
    with pytest.raises(SystemExit) as e:
        cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "0.1"])
    assert e.value.code == 1


def test_drain_reboot_on_an_unlisted_host_records_the_cycle_and_says_others_are_not_held(env, monkeypatch, capsys):
    from ttp import cli, hostcycle
    from ttp import machine_ledger as ml
    p = make(env)
    cli.main(["drain", "demo", "--reboot", "firmware update"])
    cap = capsys.readouterr()
    assert "no entry in `ttp machines list`" in cap.err and "planned host cycle recorded" in cap.out
    assert not ml.changes() and hostcycle.hold(p) is None


def test_a_drain_intent_holds_and_steers_from_its_start_and_after_an_early_return(env, monkeypatch, capsys, tmp_path):
    """--wait longer than POWER_CYCLE_LEAD_S: the co-tenant is held and its workers told from the drain's
    start, not only POWER_CYCLE_LEAD_S before the drain's end; a drain that returns early moves the
    cycle to then, so the hold covers it."""
    from ttp import cli, hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    t0 = time.time()
    c, wrote, _ = hostcycle.open_intent(p, "firmware update", 1200, now=t0)
    assert wrote and c["at"] == t0 + 1200 and c["hold_from"] == t0
    assert ml.cycle_window(c)[0] == t0
    for t in (t0, t0 + 1, t0 + 599):
        assert hostcycle.hold(q, t)["id"] == c["id"], t
    assert hostcycle.hold(q, t0 - 1) is None, "not before the drain began"
    tid = q.db.add_task("co-tenant work", "s", kind="code", origin="user")
    (tmp_path / "w").mkdir()
    rid = q.db.x("INSERT INTO runs(task,role,started,status,dir) VALUES(?,?,?,?,?)",
                 (tid, "worker", t0, "running", str(tmp_path / "w")))
    _hold_daemon(q.base, []).power_cycle_notices(t0 + 1)
    assert f"machine change #{c['id']}" in (tmp_path / "w" / "steer.md").read_text(), "told at the drain's start"
    ml.close_change(c["id"], "test")
    q.db.x("UPDATE runs SET status='done', ended=? WHERE id=?", (time.time(), rid))
    # The CLI: an idle host drains at once, and the cycle is due then.
    t1 = time.time()
    cli.main(["drain", "demo", "--reboot", "firmware update", "--wait", "1200"])
    capsys.readouterr()
    (c,) = ml.changes()
    assert c["at"] == pytest.approx(t1, abs=5), "the cycle is recorded now, not at the end of the wait"
    assert hostcycle.hold(q)["id"] == c["id"] and not hostcycle.lapsed()
    assert hostcycle.hold(q, c["at"] + ml.POWER_CYCLE_LEAD_S) is None


def test_a_drain_does_not_reuse_an_intent_planned_for_later(env, monkeypatch, capsys):
    from ttp import cli, hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    later = ml.add_power_cycle("testhost", "2d", "other", "planned maintenance")
    cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "0"])
    assert "already announced" not in capsys.readouterr().out
    mine = [c for c in ml.changes() if c["id"] != later["id"]]
    assert len(mine) == 1 and mine[0]["by"] == "demo"
    assert hostcycle.hold(q)["id"] == mine[0]["id"], "held now, by the drain's own intent"
    assert next(c for c in ml.changes() if c["id"] == later["id"])["at"] == later["at"], "the later one is untouched"


def test_a_retried_drain_does_not_lapse_before_its_record(env, monkeypatch):
    """The recipe retries a drain that timed out: each retry reuses the due intent and pushes it on, and
    the record brings it to the cycle itself, so the hold never lapses mid-drain."""
    from ttp import hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    t0 = time.time()
    c, wrote, _ = hostcycle.open_intent(p, "bus reset", 1200, now=t0)
    hostcycle.cycle_now(p, c, now=t0 + 1200)          # timed out: recorded, retried at once
    c2, wrote2, line = hostcycle.open_intent(p, "bus reset", 1200, now=t0 + 1201)
    assert not wrote2 and c2["id"] == c["id"] and c2["at"] == t0 + 2401 and "already announced" in line
    for t in (t0 + 1800, t0 + 2400):   # past the first try's lapse
        assert hostcycle.hold(q, t)["id"] == c["id"] and not hostcycle.lapsed(t), t
    hostcycle.cycle_now(p, c2, now=t0 + 2000)          # drained early this time
    (c3,) = ml.changes()
    assert c3["at"] == t0 + 2000 and c3["expires"] == t0 + 2000 + ml.POWER_CYCLE_AFTER_S
    assert [e["by"] for e in c3["edits"]] == ["demo"] * 3
    assert hostcycle.hold(q, t0 + 2599) and hostcycle.lapsed(t0 + 2600) == [ml.changes()[0]]


def test_resume_withdraws_an_intent_its_own_drain_never_recorded(env, monkeypatch, capsys):
    from ttp import cli, hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    theirs = ml.add_power_cycle("testhost", "2d", "other", "their cycle")
    c, wrote, _ = hostcycle.open_intent(p, "firmware update", 1200)   # the drain was stopped: no record
    assert wrote and c["id"] != theirs["id"]
    cli.main(["resume", "demo"])
    assert f"planned host cycle withdrawn (machine change #{c['id']})" in capsys.readouterr().out
    assert [x["id"] for x in ml.changes()] == [theirs["id"]], "only its own unrecorded intent closes"
    # One its drain recorded stays.
    cli.main(["drain", "demo", "--reboot", "firmware update", "--wait", "0"])
    capsys.readouterr()
    cli.main(["resume", "demo"])
    assert "withdrawn" not in capsys.readouterr().out and len(ml.changes()) == 2


def test_drain_reboot_waits_for_other_projects_pushes_but_not_their_coordinators(env, monkeypatch, capsys):
    from ttp import cli
    p, q = _hold_setup(env, monkeypatch)
    rid = q.db.x("INSERT INTO runs(role,provider,started,status) VALUES('coordinator','claude',?,'running')",
                 (time.time(),))
    t0 = time.time()
    cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "10"])
    out = capsys.readouterr().out
    assert time.time() - t0 < 8, "another project's coordinator run is not waited for"
    assert f"other run {rid} (coordinator)" in out, "but it is listed"
    tid = q.db.add_task("t", "s")
    q.db.x("UPDATE tasks SET status='pushing' WHERE id=?", (tid,))
    t0 = time.time()
    cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "0.3"])
    assert time.time() - t0 >= 0.3 and f"other push of task #{tid}" in capsys.readouterr().out


def test_a_drain_that_takes_up_a_due_intent_keeps_holding(env, monkeypatch):
    """A drain reuses an intent `ttp machines change ... power-cycle` wrote (no hold_from) and pushes it on
    past POWER_CYCLE_LEAD_S: the co-tenant stays held before, during and after the reuse, until the cycle;
    a resume does not withdraw the intent the drain only took up."""
    from ttp import hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    t0 = time.time()
    c = ml.add_power_cycle("testhost", "5m", "demo", "planned maintenance", now=t0)
    assert hostcycle.hold(q, t0)["id"] == c["id"]
    c2, wrote, _ = hostcycle.open_intent(p, "kernel update", 1200, now=t0)
    assert not wrote and c2["id"] == c["id"] and c2["at"] == t0 + 1200
    assert c2["hold_from"] == t0 - ml.POWER_CYCLE_LEAD_S + 300, "no later than its window began"
    for t in (t0, t0 + 1, t0 + 600, t0 + 1199):
        assert hostcycle.hold(q, t)["id"] == c["id"], t
    hostcycle.cycle_now(p, c2, now=t0 + 900)
    assert hostcycle.hold(q, t0 + 900 + ml.POWER_CYCLE_LEAD_S - 1)["id"] == c["id"]
    assert hostcycle.hold(q, t0 + 900 + ml.POWER_CYCLE_LEAD_S + 1) is None
    assert hostcycle.release(p) == [] and ml.changes()[0]["id"] == c["id"]


def test_a_dry_run_drain_puts_back_the_time_of_an_intent_it_took_up(env, monkeypatch, capsys):
    """A drain takes up another project's planned cycle (`ttp machines change ... power-cycle`: no `boot`),
    pushes it on and brings it to the drain's end; its record then says no reboot. The intent keeps its
    own time and window again (once, also seen before the daemon writes it), so the co-tenants are not
    held past that cycle's own window, and it stays open as the planned cycle it is."""
    from ttp import cli, hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)

    def no_reboot():
        for f in (p.state / hostcycle.FOLDER).glob("*/record.json"):
            f.write_text(json.dumps({**json.loads(f.read_text()), "rebooted": False}))

    t0 = time.time()
    c = ml.add_power_cycle("testhost", "5m", "other", "planned maintenance", now=t0)
    orig = {k: c.get(k) for k in ml.RETIMED}
    c2, wrote, _ = hostcycle.open_intent(p, "kernel update", 1200, now=t0)
    assert not wrote and c2["at"] == t0 + 1200
    hostcycle.record(p, "kernel update", [], [], intent=c["id"], intent_was=hostcycle.before(c2, p.name, t0))
    hostcycle.cycle_now(p, c2, now=t0 + 900)          # the drain ended after the intent's own time
    late = t0 + 900 + ml.POWER_CYCLE_LEAD_S - 1
    assert hostcycle.hold(q, late)["id"] == c["id"], "a real cycle holds until its recorded time"
    no_reboot()
    assert hostcycle.hold(q, late) is None and ml.changes()[0]["at"] == t0 + 900, "read only, already not held"
    assert hostcycle.hold(q, late, settle=True) is None
    (c3,) = ml.changes()
    assert {k: c3.get(k) for k in ml.RETIMED} == orig and "hold_from" not in c3
    assert hostcycle.hold(q, t0 + 300, settle=True)["id"] == c["id"], "its own window still holds"
    assert hostcycle.hold(q, t0 + 300 + ml.POWER_CYCLE_LEAD_S) is None
    assert len(ml.changes()[0]["edits"]) == len(c3["edits"]) == 3, "undone once"
    assert hostcycle.lapsed(t0 + 300 + ml.POWER_CYCLE_LEAD_S) == ml.changes()
    # The same through `ttp drain`, whose wait ends at once.
    ml.close_change(c["id"], "done")
    for f in (p.state / hostcycle.FOLDER).glob("*/record.json"):
        f.unlink()
    t1 = time.time()
    c = ml.add_power_cycle("testhost", "5m", "other", "planned maintenance", now=t1)
    orig = {k: c.get(k) for k in ml.RETIMED}
    cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "1200"])
    assert "already announced by other" in capsys.readouterr().out and ml.changes()[0]["at"] < t1 + 300
    no_reboot()
    assert hostcycle.hold(q, settle=True)["id"] == c["id"]
    (c4,) = ml.changes()
    assert {k: c4.get(k) for k in ml.RETIMED} == orig


def _take_up(p, c, since, wait=1200):
    """Project p's drain, started at `since`, takes up intent c (open_intent pushes it on): its intent_was
    and since, as `ttp drain` gives them to record. The record is written later (_record_takeup)."""
    from ttp import hostcycle
    c2, wrote, _ = hostcycle.open_intent(p, "kernel update", wait, now=since)
    assert not wrote and c2["id"] == c["id"]
    return {"intent": c["id"], "intent_was": hostcycle.before(c2, p.name, since), "since": since}


def _record_takeup(p, took, end, rebooted=None):
    """The drain of _take_up ends at `end`: it records the cycle and brings the intent to then; with
    `rebooted` False its record then says no reboot."""
    from ttp import hostcycle
    from ttp import machine_ledger as ml
    f = hostcycle.record(p, "kernel update", [], [], **took)
    hostcycle.cycle_now(p, ml.changes()[0], now=end, since=took["since"])
    if rebooted is False:
        _no_reboot_record(f)
    return f


def _no_reboot_record(f):
    f.write_text(json.dumps({**json.loads(f.read_text()), "rebooted": False}))


def _undos(c):
    return [e for e in c.get("edits") or [] if e.get("undo")]


def test_take_ups_marked_out_of_order_end_at_the_intents_own_time(env, monkeypatch):
    """Drains A then B take up one planned cycle. A says no reboot first: nothing is put back while B
    waits (or has recorded a cycle that is still coming), since that would drop B's time; once B also
    says no reboot, the intent is back at its own fields, not at A's retime, in one undo."""
    from ttp import hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    b = _second_project(env, "third")
    t0 = time.time()
    c = ml.add_power_cycle("testhost", "5m", "other", "planned maintenance", now=t0)
    orig = {k: c.get(k) for k in ml.RETIMED}
    a_took = _take_up(p, c, t0)
    b_took = _take_up(b, c, t0 + 60)
    b_time = {k: ml.changes()[0].get(k) for k in ml.RETIMED}
    assert b_time["at"] == t0 + 60 + 1200
    fa = _record_takeup(p, a_took, t0 + 120)
    b_time = {k: ml.changes()[0].get(k) for k in ml.RETIMED}
    _no_reboot_record(fa)
    assert hostcycle.hold(q, t0 + 130, settle=True)["id"] == c["id"], "B still waits: held"
    assert {k: ml.changes()[0].get(k) for k in ml.RETIMED} == b_time and not _undos(ml.changes()[0])
    fb = _record_takeup(b, b_took, t0 + 180)
    hostcycle.hold(q, t0 + 190, settle=True)
    assert not _undos(ml.changes()[0]), "B's cycle is still coming"
    _no_reboot_record(fb)
    assert hostcycle.hold(q, t0 + 400, settle=False)["at"] == orig["at"], "read only: its own time"
    hostcycle.hold(q, t0 + 400, settle=True)
    hostcycle.hold(b, t0 + 400, settle=True)
    (c2,) = ml.changes()
    assert {k: c2.get(k) for k in ml.RETIMED} == orig and len(_undos(c2)) == 1


def test_take_ups_marked_in_order_are_undone_newest_first(env, monkeypatch):
    """A and B took up one intent and both say no reboot: the edit trail is walked back newest first, so
    the read-only view and the settled ledger both show the intent's own fields, written once."""
    from ttp import hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    b = _second_project(env, "third")
    t0 = time.time()
    c = ml.add_power_cycle("testhost", "5m", "other", "planned maintenance", now=t0)
    orig = {k: c.get(k) for k in ml.RETIMED}
    a_took = _take_up(p, c, t0)
    b_took = _take_up(b, c, t0 + 60)
    _record_takeup(p, a_took, t0 + 120, rebooted=False)
    _record_takeup(b, b_took, t0 + 180, rebooted=False)
    records = hostcycle._no_reboot_records(q, c)
    assert [r["project"] for _, r in records] == ["third", "demo"], "newest first"
    seen = hostcycle._undo_takeups(q, ml.changes()[0], records, False, t0 + 200)
    assert {k: seen.get(k) for k in ml.RETIMED if seen.get(k) is not None} == {k: v for k, v in orig.items() if v is not None}
    hostcycle.hold(q, t0 + 200, settle=True)
    hostcycle.hold(q, t0 + 200, settle=True)
    (c2,) = ml.changes()
    assert {k: c2.get(k) for k in ml.RETIMED} == orig and len(_undos(c2)) == 1


def test_a_later_drain_of_the_same_project_keeps_its_retime(env, monkeypatch):
    """A project's first drain took the intent up and said no reboot; its second drain takes it up again
    and still waits: that retime is the second drain's (its edit's `drain`), so it stands."""
    from ttp import hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    t0 = time.time()
    c = ml.add_power_cycle("testhost", "5m", "other", "planned maintenance", now=t0)
    first = _take_up(p, c, t0)
    _record_takeup(p, first, t0 + 120, rebooted=False)
    _take_up(p, c, t0 + 200)
    waiting = {k: ml.changes()[0].get(k) for k in ml.RETIMED}
    assert hostcycle.hold(q, t0 + 210, settle=True)["id"] == c["id"]
    assert {k: ml.changes()[0].get(k) for k in ml.RETIMED} == waiting and not _undos(ml.changes()[0])


def test_a_take_up_by_a_project_listed_under_another_key_is_undone_once(env, monkeypatch):
    """The drain's project is registered under a key other than its name: its own daemon and a co-tenant's
    both settle, and the intent is put back once."""
    from ttp import hostcycle
    from ttp import machine_ledger as ml
    from ttp.project import load_registry, register, unregister
    p, q = _hold_setup(env, monkeypatch)
    b = _second_project(env, "third")
    entry = load_registry()["projects"]["third"]
    unregister("third")
    register("third-elsewhere", {"host": entry["host"], "dir": entry["dir"]})
    t0 = time.time()
    c = ml.add_power_cycle("testhost", "5m", "other", "planned maintenance", now=t0)
    orig = {k: c.get(k) for k in ml.RETIMED}
    _record_takeup(b, _take_up(b, c, t0), t0 + 120, rebooted=False)
    for proj in (b, q, p, b, q):
        hostcycle.hold(proj, t0 + 200, settle=True)
    (c2,) = ml.changes()
    assert {k: c2.get(k) for k in ml.RETIMED} == orig and len(_undos(c2)) == 1


def test_a_take_up_that_says_no_reboot_does_not_close_the_owners_intent(env, monkeypatch):
    """A co-tenant's drain wrote the intent; this project's drain took it up and says no reboot. The
    intent stays open at its writer's time (that cycle is still coming); the writer's own no-reboot
    record closes it."""
    from ttp import hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    t0 = time.time()
    c, wrote, _ = hostcycle.open_intent(q, "firmware update", 600, now=t0)
    assert wrote
    orig = {k: c.get(k) for k in ml.RETIMED}
    _record_takeup(p, _take_up(p, c, t0 + 60), t0 + 120, rebooted=False)
    assert hostcycle.hold(q, t0 + 130, settle=True)["id"] == c["id"], "the writer's own window holds"
    (c2,) = ml.changes()
    assert not c2.get("closed") and {k: c2.get(k) for k in ml.RETIMED} == orig
    own = hostcycle.record(q, "firmware update", [], [], intent=c["id"], since=t0)
    _no_reboot_record(own)
    assert hostcycle.hold(q, t0 + 140, settle=True) is None and ml.changes() == []


def test_no_take_up_is_undone_after_a_real_reboot(env, monkeypatch):
    from ttp import hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    t0 = time.time()
    c = ml.add_power_cycle("testhost", "5m", "other", "planned maintenance", now=t0)
    _record_takeup(p, _take_up(p, c, t0), t0 + 120, rebooted=False)
    moved = {k: ml.changes()[0].get(k) for k in ml.RETIMED}
    monkeypatch.setattr(hostcycle, "_rebooted_since", lambda c: True)
    assert hostcycle.hold(q, t0 + 130, settle=True) is None
    (c2,) = ml.changes()
    assert {k: c2.get(k) for k in ml.RETIMED} == moved and not _undos(c2)


def test_a_take_up_undo_after_the_intent_closed_is_skipped(env, monkeypatch):
    from ttp import hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    t0 = time.time()
    c = ml.add_power_cycle("testhost", "5m", "other", "planned maintenance", now=t0)
    _record_takeup(p, _take_up(p, c, t0), t0 + 120, rebooted=False)
    stale = ml.changes()[0]
    records = hostcycle._no_reboot_records(q, stale)
    ml.close_change(c["id"], "done")
    assert hostcycle._undo_takeups(q, stale, records, True, t0 + 130) is stale
    (closed,) = ml.changes(open_only=False)
    assert closed["closed"] and not _undos(closed)


def test_a_drain_intent_quiets_outages_only_around_the_cycle(env, monkeypatch):
    """hold_from holds and steers from the drain's start, but expects_now ('down') starts only
    POWER_CYCLE_LEAD_S before the cycle, so a real outage during the wait is not quieted."""
    from ttp import hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    t0 = time.time()
    c, wrote, _ = hostcycle.open_intent(p, "firmware update", 1800, now=t0)
    assert wrote and hostcycle.hold(q, t0)["id"] == c["id"]
    for t in (t0, t0 + 600, t0 + 1800 - ml.POWER_CYCLE_LEAD_S - 1):
        assert ml.expects_now(c, t) == [], t
    assert ml.expects_now(c, t0 + 1800 - ml.POWER_CYCLE_LEAD_S) == ["down"]
    hostcycle.cycle_now(p, c, now=t0 + 300)
    (c,) = ml.changes()
    assert ml.expects_now(c, t0 + 300 - ml.POWER_CYCLE_LEAD_S - 1) == []
    for t in (t0 + 300 - ml.POWER_CYCLE_LEAD_S, t0 + 300, t0 + 600):
        assert ml.expects_now(c, t) == ["down"], t


def _abrupt_reboot_held(p, d, held, shutdown="abrupt", boot="an-earlier-boot"):
    """The record of an abrupt reboot that ended `boot`, with `held` at its last heartbeat."""
    p.db.set_kv("boot_prev", {"boot": d.boot, "prev_boot": boot, "held": held, "shutdown": shutdown})


def test_held_by_task_names_only_the_tasks_own_device_and_machine_locks():
    from ttp.daemon import held_by_task
    held = ["board: task #7 (run 3) since 10:02 PDT", "box-device: demo task #7 (run ?) since 10:02 PDT",
            "push:origin%2Fmain: task #7 (run 3) since 10:00 PDT", "harness-upgrade: task #7 (run 3) since 10:00 PDT",
            "rig: reserved for task #7 (run 3)", "board2: task #70 (run 3) since 10:02 PDT",
            "box: other task #7 (run 3) since 10:02 PDT", "bench: task #7 (run 3) since 10:01 PDT",
            "rack: task #7 (run 3), whole run since 10:03 PDT", "probe: demo task #7 (run 4), detached jobs since 10:04 PDT",
            "rack2: other task #7 (run 3), whole run since 10:03 PDT"]
    assert held_by_task(held, 7, "demo") == ["board", "box-device", "bench", "rack", "probe"]
    assert held_by_task(held, 8, "demo") == [] and held_by_task(None, 7, "demo") == []


def test_every_hold_label_form_round_trips_through_the_one_parser():
    from ttp import hold
    for kind in ("", hold.WHOLE_RUN, hold.LABEL):
        for run in ("3", 41, None):
            lbl = hold.label(hold.who(7), run, kind)
            assert hold.task_of(lbl) == 7 and hold.task_of(lbl, "demo") == 7, lbl
            shared_lbl = hold.label(f"demo {hold.who(7)}", run, kind)
            assert hold.task_of(shared_lbl, "demo") == 7 and hold.task_of(shared_lbl, "other") is None, shared_lbl
            assert hold.own_keeper(lbl, hold.who(7)) is (kind == hold.LABEL)
            assert not hold.own_keeper(lbl, hold.who(70)) and not hold.own_keeper(shared_lbl, hold.who(7))
    # Foreign or malformed labels name no task.
    for lbl in (None, "", "task #7", "task #7 (run 3", "task #? (run 3)", "task #7 (run 3), lunch", "reserved for task #7",
                "keeper of run 3", "no project task #7 (run 1)", "a b task #7 (run 3)", "task #7 (run 3) since 10:02 PDT",
                "xtask #7 (run 3)", "task #7x (run 3)", " task #7 (run 3)"):
        assert hold.task_of(lbl) is None and hold.parse(lbl) is None, lbl
    assert hold.who(None) == "task #?" and hold.task_of(hold.label(hold.who(None), None)) is None


def _heartbeat_held(p):
    """What the daemon's heartbeat records as held right now (shared.held), as JSON from a subprocess."""
    return ["python3", "-c", "import json, sys; from ttp import shared; from ttp.project import Project; "
            "print('HELD=' + json.dumps(shared.held(Project(sys.argv[1]))))", str(p.base)]


def test_a_ttp_lock_hold_is_found_by_held_by_task(env):
    p = make(env)
    from ttp.daemon import held_by_task
    out = subprocess.run([sys.executable, str(TTP), "lock", "board", "--", *_heartbeat_held(p)],
                         env=dict(os.environ, TTP_PROJECT=str(p.base), TTP_TASK="7", TTP_RUN_ID="12",
                                  PYTHONPATH=str(RUNTIME)), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    held = json.loads(out.stdout.split("HELD=", 1)[1])
    assert any(x.startswith("board: task #7 (run 12) since ") for x in held), held
    assert held_by_task(held, 7, p.name) == ["board"] and held_by_task(held, 8, p.name) == []


def test_a_whole_run_hold_is_found_by_held_by_task(env, tmp_path):
    p = make(env)
    from ttp import shared
    from ttp.daemon import held_by_task
    release = tmp_path / "release"
    proc, _ = _hold_exclusive(p, tmp_path, release)
    try:
        held = shared.held(p)
    finally:
        release.touch()
    assert proc.wait(timeout=60) == 0
    assert any(x.startswith("board: task #7 (run ?), whole run since ") for x in held), held
    assert held_by_task(held, 7, p.name) == ["board"] and held_by_task(held, 8, p.name) == []


def test_a_detached_jobs_keepers_hold_is_found_by_held_by_task(env, tmp_path):
    p = make(env)
    from ttp import shared
    from ttp.daemon import held_by_task
    mine = _board_task(p, "flash")
    run_dir, release = p.runs / "41", tmp_path / "release"
    _exclusive_run_detaching(p, mine["id"], 41, run_dir, _until(release))
    try:
        held = shared.held(p)
    finally:
        release.touch()
    assert _until_true(lambda: (run_dir / "job.rc").exists())
    assert any(x.startswith(f"board: task #{mine['id']} (run 41), detached jobs since ") for x in held), held
    assert held_by_task(held, mine["id"], p.name) == ["board"] and held_by_task(held, mine["id"] + 1, p.name) == []


def test_a_run_lost_to_an_abrupt_reboot_while_its_task_held_a_device_is_not_retried(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    dev = p.db.add_task("flash the board", "s", kind="work", tier="light", origin="user")
    calm = p.db.add_task("edit docs", "s", kind="work", tier="light", origin="user")
    push = p.db.add_task("review and push", "s", kind="work", tier="light", origin="user")
    _abrupt_reboot_held(p, d, [f"board: task #{dev} (run 1) since 10:02 PDT",
                               f"push:origin%2Fmain: task #{push} (run 2) since 10:01 PDT",
                               f"rig: reserved for task #{calm} (run 3)"])
    for tid in (dev, calm, push):
        _lost_to_reboot(p, d, tmp_path, tid, name=f"lost{tid}")
    t = p.db.task(dev)
    assert t["status"] == "blocked" and t["attempts"] == 0, dict(t)
    assert t["blocked_reason"].startswith("the host rebooted abruptly while this task held board"), t["blocked_reason"]
    assert json.loads(t["result"])["reboot"]["held"] == ["board"]
    ev = p.db.q("SELECT severity, status FROM events WHERE kind='task_blocked' AND task=?", (dev,))
    assert ev and ev[-1]["severity"] == "high" and ev[-1]["status"] == "queued", ev
    # Nothing held, a push lock or a reservation: today's resume, at once and without an attempt.
    for tid in (calm, push):
        t = p.db.task(tid)
        assert t["status"] == "queued" and t["attempts"] == 0 and tid in [x["id"] for x in p.db.ready_tasks()], dict(t)


@pytest.mark.parametrize("case", ["orderly", "planned", "other machine", "other boot"])
def test_a_hold_at_a_reboot_the_task_did_not_cause_keeps_the_retry(env, tmp_path, monkeypatch, case):
    p = make(env)
    from ttp import machines as mm
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("flash the board", "s", kind="work", tier="light", origin="user")
    res = "board"
    if case == "other machine":
        mm.add("farbox")
        res = "farbox-device"   # held over ssh: a job there cannot reset this host
    if case == "planned":
        monkeypatch.setattr(d, "_planned_cycle", lambda boot: {"reason": "firmware", "path": "x"})
    _abrupt_reboot_held(p, d, [f"{res}: task #{tid} (run 1) since 10:02 PDT"],
                        shutdown="orderly" if case == "orderly" else "abrupt",
                        boot="yet-another-boot" if case == "other boot" else "an-earlier-boot")
    _lost_to_reboot(p, d, tmp_path, tid)
    assert p.db.task(tid)["status"] == "queued", dict(p.db.task(tid))


def test_a_lock_named_after_this_host_still_counts(env, tmp_path):
    p = make(env)
    from ttp import machines as mm
    from ttp.daemon import Daemon
    from ttp.project import hostname
    mm.add(hostname())
    d = Daemon(p.base)
    tid = p.db.add_task("flash the board", "s", kind="work", tier="light", origin="user")
    _abrupt_reboot_held(p, d, [f"{hostname()}-device: task #{tid} (run 1) since 10:02 PDT"])
    _lost_to_reboot(p, d, tmp_path, tid)
    assert p.db.task(tid)["status"] == "blocked"


@pytest.mark.parametrize("survives", [False, True])
def test_a_waiting_task_whose_job_held_a_device_at_an_abrupt_reboot_is_blocked_not_woken(env, monkeypatch, survives):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    tid = p.db.add_task("soak test", "s", kind="work", tier="light", origin="user")
    _waiting_from_before_the_boot(p, tid)
    if survives:
        p.db.update_task(tid, result=json.dumps({**json.loads(p.db.task(tid)["result"]), "survives_reboot": True}))
    d = Daemon(p.base)
    d.boot_at = time.time() - 300
    _abrupt_reboot_held(p, d, [f"board: task #{tid} (run 1) since 10:02 PDT"])
    monkeypatch.setattr(d, "_relaunch_jobs", lambda *a: pytest.fail("a job that held the device was relaunched"))
    d.wake_after_reboot()
    t = p.db.task(tid)
    assert t["status"] == "blocked" and "held board" in t["blocked_reason"], dict(t)
    ev = p.db.q("SELECT severity FROM events WHERE kind='task_blocked' AND task=?", (tid,))
    assert ev and ev[-1]["severity"] == "high"
    # The coordinator's requeue is its word that the attempt was not implicated: the next reboot
    # without a hold wakes it as before.
    assert coord.apply(p, [{"type": "task_update", "id": tid, "status": "queued"}]) == []
    _waiting_from_before_the_boot(p, tid)
    d = Daemon(p.base)
    d.boot_at = time.time() - 300
    _abrupt_reboot_held(p, d, [])
    d.wake_after_reboot()
    t = p.db.task(tid)
    assert t["status"] == "queued" and (survives or json.loads(t["result"]).get("woke") == "the host rebooted"), dict(t)


def _down_since(p, d, age_s, boot=None):
    """The heartbeat a daemon left `age_s` ago on `boot` (this boot by default)."""
    hb = p.state / "heartbeat"
    hb.write_text(json.dumps({"pid": 1, "boot": boot or d.boot, "held": []}))
    os.utime(hb, (time.time() - age_s, time.time() - age_s))


def _down_notices(p):
    return p.db.q("SELECT text, severity, kind FROM messages WHERE direction='out' AND text LIKE '%daemon was down%'")


def _late_watch(p, late_s):
    from ttp import schedule as sched
    sched.upsert(p.db, "watch", "command", "30m", payload={"command": "true", "timeout_s": 30})
    now = time.time()
    p.db.x("UPDATE schedules SET last_run=?, next_run=? WHERE name='watch'", (now - late_s - 1800, now - late_s))


def test_a_daemon_back_after_a_long_stop_posts_one_notice(env, tmp_path):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    _late_watch(p, 3 * 3600)
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    run_dir = tmp_path / "gone"
    run_dir.mkdir()
    (run_dir / "lease").touch()
    os.utime(run_dir / "lease", (time.time() - 4 * 3600, time.time() - 4 * 3600))
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 5 * 3600, "running", str(run_dir), d.boot))
    _down_since(p, d, 3 * 3600 + 300)
    d._last_stop = {"boot": d.boot, "ts": time.time() - 3 * 3600 - 200}
    d._note_down()
    d.reap_runs()
    (msg,) = _down_notices(p)
    assert msg["severity"] == "normal" and msg["kind"] == "info", msg
    assert "down for 3.1 h" in msg["text"] and "the daemon was stopped." in msg["text"], msg["text"]
    assert "6 schedule run(s) were missed; 1 run(s) were cut short" in msg["text"], msg["text"]
    d.reap_runs()
    assert len(_down_notices(p)) == 1, "told twice"


def test_a_long_stop_later_on_the_same_boot_is_no_reboot(env):
    # boot_prev stays for every start on this boot: a later long stop is the daemon's, not a reboot.
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    _abrupt_reboot_held(p, d, [])
    p.db.set_kv("reboot_told", d.boot)
    _down_since(p, d, 3 * 3600)
    d._last_stop = {"boot": d.boot, "ts": time.time() - 3 * 3600 + 60}
    d._note_down()
    d.reap_runs()
    (msg,) = _down_notices(p)
    assert "the daemon was stopped." in msg["text"] and "rebooted" not in msg["text"], msg["text"]


@pytest.mark.parametrize("told", [False, True])
def test_an_old_heartbeat_with_no_boot_id_is_a_reboot_only_until_it_is_told(env, told):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    _abrupt_reboot_held(p, d, [])
    if told:
        p.db.set_kv("reboot_told", d.boot)
    hb = p.state / "heartbeat"
    hb.write_text(json.dumps({"pid": 1, "held": []}))
    os.utime(hb, (time.time() - 2 * 3600, time.time() - 2 * 3600))
    d._note_down()
    d.reap_runs()
    (msg,) = _down_notices(p)
    assert ("the host rebooted (abrupt)" in msg["text"]) != told, msg["text"]


def test_a_short_restart_posts_no_down_notice(env):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    _late_watch(p, 3 * 3600)   # schedule runs missed alone are the schedule-gap check's to tell
    _down_since(p, d, 600)
    d._note_down()
    d.reap_runs()
    assert not _down_notices(p)


def test_a_threshold_from_config_and_a_notice_kept_across_a_second_start(env):
    from ttp import downtime
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.cfg = {**d.cfg, "daemon": {"down_notice_s": 600}}
    _down_since(p, d, 900)
    d._note_down()
    # It died before its first tick; the next start finds a fresh heartbeat but keeps the notice.
    e = Daemon(p.base)
    _down_since(p, e, 5)
    e._note_down()
    assert p.db.kv(downtime.DOWN_KEY)
    e.reap_runs()
    (msg,) = _down_notices(p)
    assert "down for 15 min" in msg["text"] and "without a clean stop" in msg["text"], msg["text"]


def test_a_long_reboot_downtime_joins_the_reboot_notice_in_one_message(env, tmp_path):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    _abrupt_reboot_held(p, d, [])
    _down_since(p, d, 6 * 3600, boot="an-earlier-boot")
    d._note_down()
    _lost_to_reboot(p, d, tmp_path, tid)
    out = p.db.q("SELECT text, severity, ref FROM messages WHERE direction='out' AND kind='info'")
    assert len(out) == 1, out
    text = out[0]["text"]
    assert text.startswith("The daemon was down for 6.0 h") and "the host rebooted (abrupt)" in text, text
    assert "The host rebooted (abrupt; 1st reboot in 24 h); 1 run(s) were cut short" in text, text
    assert out[0]["severity"] == "normal" and out[0]["ref"] == f"reboot:{d.boot}"
    d.reap_runs()
    assert len(p.db.q("SELECT id FROM messages WHERE direction='out' AND kind='info'")) == 1


def test_a_long_reboot_downtime_with_no_run_cut_short_still_posts_the_notice(env):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    _abrupt_reboot_held(p, d, [], shutdown="orderly")
    _down_since(p, d, 2 * 3600, boot="an-earlier-boot")
    d._note_down()
    d.reap_runs()
    (msg,) = _down_notices(p)
    assert "the host rebooted (orderly)" in msg["text"] and "0 run(s) were cut short" in msg["text"], msg["text"]


# Who started the shutdown that ended the previous boot (initiator.py): fixture journal output only,
# never the real journalctl (the env fixture sets TTP_TEST_NO_JOURNAL).
_SUDO_REBOOT = ("Oct 25 21:59:58 testhost sudo[4242]:  someuser : TTY=pts/3 ; PWD=/home/someuser ; USER=root ; "
                "COMMAND=/usr/sbin/reboot\n"
                "Oct 25 21:59:59 testhost systemd-logind[812]: System is rebooting.\n")
_PKEXEC_POWEROFF = ("Oct 25 21:59:58 testhost pkexec[51]: someuser: Executing command [USER=root] [TTY=/dev/pts/0] "
                    "[CWD=/home/someuser] [COMMAND=/usr/bin/systemctl poweroff]\n")
_POWER_KEY = ("Oct 25 21:59:50 testhost systemd-logind[812]: Power key pressed short.\n"
              "Oct 25 21:59:51 testhost systemd-logind[812]: Powering off...\n")
_HIDDEN = (0, "", "Hint: You are currently not seeing messages from other users and the system.\n")


def _initiator_reboot(p, tmp_path, monkeypatch, journal, last_heartbeat_ago=600):
    """A new boot after one that cut a run short, with run_journal answering `journal`; the reboot notice."""
    from ttp import initiator
    from ttp.daemon import Daemon
    calls = []
    monkeypatch.setattr(initiator, "run_journal", lambda boot: calls.append(boot) or journal)
    _boot_as(monkeypatch, "boot-0")
    Daemon(p.base).tick()
    p.db.set_kv("paused", True)
    _lost_deep_runs(p, tmp_path, "boot-0", costs=(1.0,))
    _boot_as(monkeypatch, "boot-1", time.time() - 60)
    d = Daemon(p.base)
    d.tick()
    d.tick()
    assert calls == ["boot-0"], "the journal is read once per boot, for the boot that ended"
    (n,) = _reboot_notices(p)
    ev = p.db.one("SELECT text, data FROM events WHERE source='host' AND kind='boot' ORDER BY id DESC LIMIT 1")
    return n["text"], ev["text"], json.loads(ev["data"])["initiator"]


@pytest.mark.parametrize("journal,said", [
    ((0, _SUDO_REBOOT, ""), "Initiator: someuser ran `reboot` via sudo (journal)."),
    ((0, _PKEXEC_POWEROFF, ""), "Initiator: someuser ran `systemctl poweroff` via pkexec (journal)."),
    ((0, _POWER_KEY, ""), "Initiator: the power key (journal)."),
    ((0, "Oct 25 21:59:59 testhost systemd-logind[812]: System is powering down.\n", ""),
     "Initiator: a request to systemd-logind that names no user (journal: System is powering down.)."),
    ((0, "", ""), "Initiator: unknown."),
    (_HIDDEN, "Initiator: unknown (journal not readable: user not in systemd-journal and no passwordless sudo)."),
    ((1, "", "No journal files were opened due to insufficient permissions.\n"),
     "Initiator: unknown (journal not readable: user not in systemd-journal and no passwordless sudo)."),
    (None, "Initiator: unknown (journal not readable: journalctl did not answer, missing or timed out)."),   # journalctl missing or timed out
])
def test_the_reboot_notice_names_who_started_the_shutdown_from_the_journal(env, tmp_path, monkeypatch, journal, said):
    p = make(env)
    feed, record, got = _initiator_reboot(p, tmp_path, monkeypatch, journal)
    assert said in feed, feed
    assert record.endswith("; " + said[:-1].replace("Initiator", "initiator")), record
    assert got["source"] == ("unknown" if "unknown" in said else "journal"), got


def test_the_reboot_notice_names_a_host_cycle_record_of_another_project(env, tmp_path, monkeypatch):
    from ttp import initiator
    p, q = make(env), _second_project(env)
    rec = q.state / "host-cycle" / "rec-a" / "record.json"
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"id": "rec-a", "boot_id": "boot-0", "started": time.time() - 300,
                               "dry_run": False, "reason": "firmware update", "by": "ttp drain"}))
    got = initiator.find(p, "boot-0", time.time() - 60, time.time() - 600, journal=_HIDDEN)
    assert got["source"] == "host-cycle" and got["project"] == q.name, got
    assert got["text"] == f"`ttp drain --reboot` in project {q.name} (host-cycle record rec-a: firmware update)", got
    # A planned cycle's notice already names its record: the clause is not repeated there.
    feed, record, stored = _initiator_reboot(p, tmp_path, monkeypatch, _HIDDEN)
    assert stored == {**got, "record": str(rec)} and "Initiator:" not in feed and "initiator:" not in record, feed
    assert "It was a planned host cycle (host-cycle record rec-a" in feed, feed
    # A host-cycle record of an earlier boot, or a dry run, names nothing.
    assert initiator.find(p, "boot-9", time.time(), None, journal=_HIDDEN)["source"] == "unknown"


def test_the_reboot_notice_names_a_ledger_entry_recorded_just_before_the_shutdown(env, tmp_path, monkeypatch):
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env)
    mm.add("testhost")
    mm.add("otherhost")
    now = time.time()
    ml.add_change("testhost", "raised the fan curve", "none: kept", "other", expires="2h", now=now - 900)
    ml.add_change("otherhost", "power cycled it via the BMC", "none: done", "other", expires="2h", now=now - 400)
    ml.add_change("testhost", "power cycled it via the BMC", "none: done", "other", expires="2h",
                  now=now - 4 * 3600)   # long before the shutdown
    hit = ml.add_change("testhost", "power-cycled the host via the BMC: chips off the bus", "none: done", "other",
                        expires="2h", now=now - 300)
    feed, record, got = _initiator_reboot(p, tmp_path, monkeypatch, _HIDDEN)
    assert got == {"source": "ledger", "by": "other", "entry": hit["id"],
                   "text": f"machine-change ledger entry #{hit['id']} by project other: "
                           f"power-cycled the host via the BMC: chips off the bus",
                   "unreadable": "user not in systemd-journal and no passwordless sudo"}, got
    assert f"Initiator: machine-change ledger entry #{hit['id']} by project other: power-cycled" in feed \
        and "(abrupt;" in feed, feed


def test_the_initiator_search_never_raises_and_never_waits_long(env, monkeypatch):
    import subprocess as sp
    from ttp import hostcycle, initiator, machines
    p = make(env)
    monkeypatch.delenv("TTP_TEST_NO_JOURNAL")
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw.get("timeout")))
        raise sp.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(initiator.subprocess, "run", run)
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broken"))
    monkeypatch.setattr(hostcycle, "planned", boom)
    monkeypatch.setattr(machines, "here", boom)
    assert initiator.find(p, "0123456789abcdef0123456789abcdef", time.time(), None) == \
        {"source": "unknown", "text": "unknown (journal not readable: journalctl did not answer, missing or timed out)",
         "unreadable": "journalctl did not answer, missing or timed out"}
    (cmd, timeout), = seen
    assert cmd[:3] == ["journalctl", "-b", "0123456789abcdef0123456789abcdef"] and timeout == initiator.JOURNAL_TIMEOUT_S
    monkeypatch.setattr(initiator.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert initiator.run_journal("boot-0") is None
    assert initiator.find(p, "b", None, None, journal=(0, None, None))["text"] == "unknown"   # garbage parses to nothing


def test_journal_lines_that_do_not_stop_the_host_name_no_initiator():
    from ttp import initiator
    for line in ["sudo[1]:  someuser : 3 incorrect password attempts ; TTY=pts/1 ; USER=root ; COMMAND=/usr/sbin/reboot",
                 "sudo[1]:  someuser : TTY=pts/1 ; USER=root ; COMMAND=/usr/bin/vim /etc/reboot.conf",
                 "sudo[1]:  someuser : TTY=pts/1 ; USER=root ; COMMAND=/usr/bin/systemctl status reboot.target",
                 "sudo[1]:  someuser : TTY=pts/1 ; USER=root ; COMMAND=/usr/local/bin/rebootctl",
                 "pkexec[1]: someuser: Error executing command as another user: Not authorized [COMMAND=/usr/sbin/reboot]",
                 "cron[1]: (root) CMD (/usr/sbin/shutdown -r now)",
                 "systemd-logind[1]: New session 4 of user someuser."]:
        assert initiator.parse(f"Oct 25 10:00:00 testhost {line}\n") == (None, None), line
    # The last stop in the boot wins: a hung `shutdown` followed by the power key is the power key.
    two = ("Oct 25 10:00:00 testhost sudo[1]:  someuser : TTY=pts/1 ; USER=root ; COMMAND=/sbin/shutdown -h now\n"
           "Oct 25 10:09:00 testhost systemd-logind[2]: Power key pressed.\n")
    assert initiator.parse(two)[0] == "the power key (journal)"
    assert initiator.parse(two.splitlines()[0])[0] == "someuser ran `shutdown -h now` via sudo (journal)"


# The previous boot's journal as JSON (journalctl -o json), generic names only: a system service that
# power-cycles the host, and how the read falls back to `sudo -n` when this user sees only its own entries.
_T0 = 1_760_000_000


def _j(dt, msg, unit="", ident="", uid="0", about=""):
    e = {"__REALTIME_TIMESTAMP": str(int((_T0 + dt) * 1e6)), "MESSAGE": msg, "_UID": uid}
    for k, v in (("_SYSTEMD_UNIT", unit), ("SYSLOG_IDENTIFIER", ident), ("UNIT", about)):
        if v:
            e[k] = v
    return json.dumps(e)


_BROKER = "example-broker.service"
_CYCLE = "\n".join([
    _j(-900, "power-cycle requested long ago", _BROKER, "example-broker"),   # outside the 10 min window
    _j(-120, "job 41 finished", _BROKER, "example-broker"),
    _j(-30, "Started example-broker-recover.service - Example recovery.", "init.scope", "systemd",
       about="example-broker-recover.service"),
    _j(-5, "power-cycle a hung shutdown: running example-power-cycle --now", "example-broker-recover.service",
       "example-recover"),
    _j(-1, "nothing to do", "systemd-timesyncd.service", "systemd-timesyncd"),
    _j(0, "System is rebooting.", "systemd-logind.service", "systemd-logind"),
    _j(3, "Stopping example-broker.service - Example broker...", "init.scope", "systemd", about=_BROKER),
    _j(9, "Reached target reboot.target - System Reboot.", "init.scope", "systemd"),
]) + "\n"
_OWN_ONLY = (0, _j(-60, "my own user unit line", "user@1000.service", "example-user-app", uid=str(os.getuid())) + "\n",
             "Hint: You are currently not seeing messages from other users and the system.\n")


def test_the_reboot_notice_names_the_unit_that_power_cycled_the_host_from_a_readable_journal(env, tmp_path,
                                                                                               monkeypatch):
    from ttp import initiator
    p = make(env)
    monkeypatch.setattr(initiator, "run_sudo_journal", lambda boot: pytest.fail("the user's read was enough"))
    feed, record, got = _initiator_reboot(p, tmp_path, monkeypatch, (0, _CYCLE, ""))
    said = ("Initiator: example-broker-recover.service (power-cycle a hung shutdown: running example-power-cycle "
            "--now).")
    assert said in feed and got == {"source": "journal", "text": said[len("Initiator: "):-1]}, (feed, got)
    assert initiator.doctor_line(p) == ""
    # Logged text that reads like a journalctl hint is no hint: the read still counts as the system's.
    noisy = _j(-9, "open /dev/example0: permission denied; no journal files were found", _BROKER, "x") + "\n"
    assert initiator.read("boot-0", (0, noisy + _CYCLE, ""), False)[1:] == ("user", "")
    # A 'Reached target reboot.target' line alone is a marker too; with no stop words before it, the unit
    # started nearest before it stands in, and a stop line older than the window names nothing.
    lines = _CYCLE.splitlines()
    quiet = "\n".join(line for line in lines if "hung shutdown" not in line and "System is rebooting" not in line)
    assert initiator.parse(quiet, full=True) == (
        None, "example-broker-recover.service (Started example-broker-recover.service - Example recovery.)")
    assert initiator.parse("\n".join([lines[0], lines[1], lines[5]]), full=True) == \
        (None, f"{_BROKER} (job 41 finished)")


def test_a_permission_limited_journal_is_read_through_sudo_n_never_prompting(env, tmp_path, monkeypatch):
    import subprocess as sp
    from ttp import initiator
    p = make(env)
    monkeypatch.delenv("TTP_TEST_NO_JOURNAL")
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw.get("timeout"), kw.get("stdin")))
        if cmd[0] == "journalctl":
            return sp.CompletedProcess(cmd, *_OWN_ONLY)
        if cmd == ["sudo", "-n", "true"]:
            return sp.CompletedProcess(cmd, 0, "", "")
        return sp.CompletedProcess(cmd, 0, _CYCLE, "")
    monkeypatch.setattr(initiator.subprocess, "run", run)
    got = initiator.find(p, "0123456789abcdef0123456789abcdef", time.time(), None)
    assert got["source"] == "journal" and got["read"] == "sudo" \
        and got["text"].startswith("example-broker-recover.service (power-cycle a hung shutdown"), got
    user, check, sudo = (c[0] for c in calls)
    assert user[:3] == ["journalctl", "-b", "0123456789abcdef0123456789abcdef"] and "json" in user
    assert check == ["sudo", "-n", "true"] and sudo == ["sudo", "-n", *user]
    assert all(t <= 10 and stdin == sp.DEVNULL for _, t, stdin in calls), calls   # bounded, never prompts
    # Only the user's own entries and no hint is limited too; no passwordless sudo: one silent probe, no read.
    calls.clear()
    own = (0, _OWN_ONLY[1], "")
    monkeypatch.setattr(initiator.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or sp.CompletedProcess(
        cmd, *(own if cmd[0] == "journalctl" else (1, "", "sudo: a password is required\n"))))
    got = initiator.find(p, "boot-0", time.time(), None)
    assert [c[:3] for c in calls] == [["journalctl", "-b", "-1"], ["sudo", "-n", "true"]], calls
    assert got["text"] == "unknown (journal not readable: user not in systemd-journal and no passwordless sudo)"
    # sudo works but its read is refused: said so, never retried with a password.
    monkeypatch.setattr(initiator.subprocess, "run", lambda cmd, **kw: sp.CompletedProcess(
        cmd, *(own if cmd[0] == "journalctl" else (0, "", "") if cmd[-1] == "true"
               else (1, "", "sudo: a password is required\n"))))
    assert initiator.find(p, "boot-0", time.time(), None)["unreadable"] == \
        "user not in systemd-journal, and sudo -n journalctl did not read it either"


def test_an_unreadable_journal_gives_its_reason_once_in_doctor_not_a_command_to_run(env, tmp_path, monkeypatch):
    from ttp import initiator
    p = make(env)
    feed, record, got = _initiator_reboot(p, tmp_path, monkeypatch, _OWN_ONLY)
    why = "user not in systemd-journal and no passwordless sudo"
    assert f"Initiator: unknown (journal not readable: {why})." in feed and feed.count("Initiator:") == 1, feed
    assert got == {"source": "unknown", "text": f"unknown (journal not readable: {why})", "unreadable": why}
    line = initiator.doctor_line(p)
    assert line.startswith(f"reboot causes: the last reboot's journal was not readable ({why}).") \
        and "systemd-journal group" in line and "setup-box" in line and "`" not in line, line
    r = subprocess.run([sys.executable, "-m", "ttp", "doctor", p.name], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(RUNTIME)}, timeout=60)
    assert r.stdout.count("reboot causes:") == 1, r.stdout + r.stderr
    # A missing persistent journal is no group problem: sudo is not tried and no group fix is offered.
    stored = initiator.find(p, "boot-0", None, None, journal=(1, "", "Specifying boot ID or boot offset has no "
                                                                      "effect, no persistent journal was found.\n"),
                            sudo=(0, _CYCLE, ""))   # sudo would read it: never asked
    assert stored == {"source": "unknown", "unreadable": "no persistent journal kept for the previous boot",
                      "text": "unknown (journal not readable: no persistent journal kept for the previous boot)"}
    p.db.set_kv("boot_prev", {**p.db.kv("boot_prev"), "initiator": stored})
    assert "(no persistent journal" in initiator.doctor_line(p) and "group" not in initiator.doctor_line(p)


def test_a_boot_with_no_shutdown_marker_says_no_clean_shutdown_and_names_the_last_unit_lines(env, tmp_path,
                                                                                             monkeypatch):
    from ttp import initiator
    p = make(env)
    cut = "\n".join(line for line in _CYCLE.splitlines()[:3]) + "\n" + _j(-2, "kernel line", ident="kernel") + "\n"
    feed, record, got = _initiator_reboot(p, tmp_path, monkeypatch, (0, cut, ""))
    said = (f"Initiator: no clean shutdown logged (power loss, hard reset or watchdog); last unit lines: "
            f"{_BROKER}: power-cycle requested long ago; {_BROKER}: job 41 finished.")
    assert said in feed and got["source"] == "journal", (feed, got)
    # No unit lines at all: the wording alone. A limited read never claims an unclean boot.
    assert initiator.parse(_j(-2, "kernel line", ident="kernel"), full=True) == (None, initiator.NO_SHUTDOWN)
    assert initiator.parse(cut, full=False) == (None, None)
