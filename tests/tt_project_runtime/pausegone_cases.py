# Resource pause removal tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects these
# tests under its name. Every path that drops or replaces a resource pause leaves an event naming the path,
# the actor and the reason (db.PAUSE_REMOVED_KIND / PAUSE_REPLACED_KIND), and an open machine-ledger entry
# whose undo resumes a pause that is gone raises one alert (ttp.pausegone).
from __future__ import annotations


def _pause_gone_events(p, kind="resource_pause_removed"):
    return [json.loads(r["data"]) | {"text": r["text"]}
            for r in p.db.q("SELECT text, data FROM events WHERE kind=? ORDER BY id", (kind,))]


def test_a_pause_resumed_by_the_cli_leaves_a_removed_event(env, monkeypatch):
    from ttp import cli
    p = make(env)
    for var in ("TTP_RUN_ID", "TTP_PROJECT", "TTP_TASK"):
        monkeypatch.delenv(var, raising=False)
    cli.main(["pause", "demo", "--resource", "board", "--reason", "maintenance window", "--until", "2d"])
    assert not _pause_gone_events(p)
    cli.main(["resume", "demo", "--resource", "board"])
    (ev,) = _pause_gone_events(p)
    assert ev["resource"] == "board" and ev["via"] == "ttp resume --resource" and ev["by"] == "user"
    assert ev["was"]["reason"] == "maintenance window" and "removed by user via ttp resume" in ev["text"]


def test_a_pause_lifted_by_the_coordinators_action_names_it_and_its_reason(env):
    from ttp import coordinator as coord
    p = make(env)
    assert coord.apply(p, [{"type": "resource_pause", "resource": "rig", "paused": True, "until": "1d",
                            "reason": "flaky link"}]) == []
    assert coord.apply(p, [{"type": "resource_pause", "resource": "rig", "paused": False,
                            "reason": "link fixed"}]) == []
    (ev,) = _pause_gone_events(p)
    assert (ev["resource"], ev["via"], ev["by"], ev["reason"]) == ("rig", "coordinator resource_pause",
                                                                   "coordinator", "link fixed")


def test_a_repause_leaves_a_replaced_event_and_the_web_path_is_named(env):
    from ttp import coordinator as coord
    p = make(env)
    coord.pause_resource(p, "box-a", True, reason="first", by="user", end={"until": time.time() + 3600})
    coord.pause_resource(p, "box-a", True, reason="second", by="user", end={"until": time.time() + 7200},
                         via="web app")
    (ev,) = _pause_gone_events(p, "resource_pause_replaced")
    assert ev["via"] == "web app" and ev["was"]["reason"] == "first" and "replaced" in ev["text"]
    assert not _pause_gone_events(p), "a replaced pause is not reported as removed"


def test_a_pause_dropped_by_a_bare_kv_write_names_its_caller(env):
    from ttp.db import PAUSED_RESOURCES_KEY
    p = make(env)
    p.db.set_kv(PAUSED_RESOURCES_KEY, {"board": {"reason": "r", "by": "user", "since": 1.0},
                                       "rig": {"reason": "s", "by": "user", "since": 1.0}})
    p.db.set_kv(PAUSED_RESOURCES_KEY, {"rig": {"reason": "s", "by": "user", "since": 1.0}})   # drops board only
    (ev,) = _pause_gone_events(p)
    assert ev["resource"] == "board" and ev["by"] == "unknown"
    assert ev["via"].endswith("test_a_pause_dropped_by_a_bare_kv_write_names_its_caller"), ev["via"]


def test_a_health_ended_pause_leaves_a_removed_event(env):
    from ttp import heal
    p, d, _ = _health_pause(env)
    now = time.time()
    for dt in (0, 900, 1800):
        heal.run(d, "svc", _heal_spec(p), now + dt)
        d.check_pause_ends()
    d.check_pause_ends()
    assert "board" not in p.db.paused_resources()
    (ev,) = _pause_gone_events(p)
    assert ev["via"] == "pause end_when heal end" and ev["by"] == "daemon" and ev["reason"]


def test_a_user_end_pause_leaves_a_removed_event(env):
    from ttp import coordinator as coord, pauseends
    p = make(env)
    msg = p.db.post("in", "Stay off rig for 4 hours")
    assert coord.apply(p, [{"type": "resource_pause", "resource": "rig", "paused": True, "user_asked": True,
                            "user_end": True, "until": "4h", "reason": "the user's"}],
                       user_turn=True, messages=[msg]) == []
    until = float(p.db.paused_resources()["rig"]["until"])
    assert pauseends.PauseEnds(p).tick(until + 1) == ["rig"]
    (ev,) = _pause_gone_events(p)
    assert ev["via"] == "pause user_end" and ev["by"] == "daemon" and "user's own end" in ev["reason"]


def test_the_pause_end_migration_leaves_a_replaced_event(env):
    from ttp import pauseends
    from ttp.db import PAUSED_RESOURCES_KEY
    p = make(env)
    p.db.set_kv(PAUSED_RESOURCES_KEY, {"board": {"reason": "old", "by": "user", "since": time.time()}})
    pauseends.migrate(p, time.time())
    (ev,) = _pause_gone_events(p, "resource_pause_replaced")
    assert ev["via"] == "pause end migration" and ev["by"] == "daemon"


def test_a_quarantined_pause_row_leaves_a_removed_event(env):
    p = make(env)
    p.db.x("INSERT INTO kv(key,value,ts) VALUES('paused_resources','{\"board\": ',?)", (time.time(),))
    assert p.db.paused_resources() == {}
    (ev,) = _pause_gone_events(p)
    assert ev["resource"] == "*" and ev["via"] == "kv quarantine" and ev["by"] == "daemon"


def test_a_resumed_shared_pause_leaves_a_removed_event(env):
    from ttp import coordinator as coord
    p = make(env)
    p.set_config("shared_resources", ["board"])
    coord.pause_resource(p, "board", True, reason="maintenance", by="user", end={"until": time.time() + 3600})
    assert "board" in p.db.paused_resources() and "board" not in p.db.paused_resources(shared=False)
    coord.pause_resource(p, "board", False, by="user", via="web app")
    (ev,) = _pause_gone_events(p)
    assert ev["resource"] == "board" and ev["via"] == "web app (shared)" and ev["was"]["reason"] == "maintenance"


@pytest.mark.parametrize("undo, want", [
    ("ttp resume demo --resource box-a", "box-a"),
    ("ttp resume demo --resource=box-a", "box-a"),
    ("/opt/bin/ttp resume demo --resource 'box-a' && echo ok", "box-a"),
    ("ttp lock rig -- ttp resume demo --resource rig", "rig"),
    ("echo x; ttp resume demo --resource rig", "rig"),
    ("ttp resume other --resource box-a", None),
    ("ttp resume demo", None),
    ("ttp pause demo --resource box-a", None),
    ("ttp resume demo && ttp resume other --resource box-a", None),
    ("ttp resume demo --resource 'box-a", None),
    ("none: kept", None),
])
def test_pausegone_reads_the_resource_an_undo_resumes(undo, want):
    from ttp import pausegone
    assert pausegone.resumed_resource(undo, "demo") == want


def _pause_ledger(env):
    from ttp import coordinator as coord, machines as mm, machine_ledger as ml
    p = make(env)
    mm.add("box-a", tags="device")
    coord.pause_resource(p, "box-a", True, reason="kept off for a test", by="user",
                         end={"until": time.time() + 86400})
    c = ml.add_change("box-a", "paused box-a for this project", "ttp resume demo --resource box-a", "demo",
                      expires="2d")
    return p, ml, c["id"]


def _gone_alerts(p, cid):
    return p.db.q("SELECT * FROM alerts WHERE key=? ORDER BY id", (f"pause-gone:{cid}",))


def test_an_open_pause_entry_with_its_pause_live_raises_no_alert(env):
    from ttp import pausegone
    p, _, cid = _pause_ledger(env)
    sent = []
    assert pausegone.check(p, lambda *a, **k: sent.append(a)) == [] and not sent
    assert not p.db.kv(pausegone.KEY)


def test_an_open_pause_entry_with_no_live_pause_alerts_once_and_clears_when_it_closes(env):
    from ttp import alerts, coordinator as coord, pausegone
    from ttp.daemon import Daemon
    p, ml, cid = _pause_ledger(env)
    d = Daemon(p.base)
    coord.pause_resource(p, "box-a", False, reason="looked done", by="coordinator", via="coordinator resource_pause")
    for _ in range(3):
        assert pausegone.check(p, d.alert) == [cid]
    (ep,) = _gone_alerts(p, cid)
    assert ep["severity"] == "normal" and ep["cleared"] is None
    assert f"Machine change #{cid} on box-a" in ep["text"] and "`box-a`" in ep["text"]
    assert "by coordinator via coordinator resource_pause: looked done" in ep["text"], ep["text"]
    msgs = p.db.q("SELECT id FROM messages WHERE ref=?", (f"pause-gone:{cid}",))
    assert len(msgs) == 1, "one alert across repeated checks"
    assert alerts.holds(p.db, f"pause-gone:{cid}", ep["raised"], time.time())
    ml.close_change(cid, "done", "demo")
    assert pausegone.check(p, d.alert) == []
    (ep,) = _gone_alerts(p, cid)
    assert ep["cleared"] is not None and not p.db.kv(pausegone.KEY)
    assert not alerts.holds(p.db, f"pause-gone:{cid}", ep["raised"], time.time())
    assert pausegone.check(p, d.alert) == [] and len(_gone_alerts(p, cid)) == 1


def test_a_pause_gone_alert_clears_when_the_pause_returns(env):
    from ttp import coordinator as coord, pausegone
    p, _, cid = _pause_ledger(env)
    sent = []
    p.db.set_kv("paused_resources", {})
    assert pausegone.check(p, lambda *a, **k: sent.append((a, k))) == [cid] and len(sent) == 1
    assert "no event records who removed it" not in sent[0][0][1], "the bare write was recorded"
    coord.pause_resource(p, "box-a", True, reason="back", by="user", end={"until": time.time() + 3600})
    assert pausegone.check(p, lambda *a, **k: sent.append((a, k))) == [] and len(sent) == 1
    assert not p.db.kv(pausegone.KEY)


def test_the_daemon_checks_pause_entries_when_it_routes_the_ledger(env, monkeypatch):
    from ttp import pausegone
    from ttp.daemon import Daemon
    p, _, cid = _pause_ledger(env)
    calls = []
    monkeypatch.setattr(pausegone, "check", lambda proj, alert: calls.append(proj.name) or [])
    Daemon(p.base).route_machines(every_s=0)
    assert calls == ["demo"]
