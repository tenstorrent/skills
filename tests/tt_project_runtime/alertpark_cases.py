# Parked-alert cases: an alert the user acknowledged stays quiet for its condition while that condition is
# unchanged, and nothing asks the user for what other providers or a fallback already cover (alerts.py
# condition_acked, userstep.silent, Daemon.logout_alert, Daemon.needs_ranking_key).
# Run from tests/test_tt_project_runtime.py (see _include_cases there): helpers, fixtures and imports
# come from that file, and pytest collects these tests under it.
from __future__ import annotations

PARK_WHY = "the user's own sign-in to the example tool (`example-cli login`) shows as logged in"


def _park_deferred(p, title="measure the example tool", why=PARK_WHY, when="example-cli auth status"):
    from ttp import cli
    cli.main(["task", "demo", "add", title, "--start-when", when, "--why", why])
    return p.db.one("SELECT id FROM tasks WHERE title=?", (title,))["id"]


def _chat_alerts(p, ref):
    from ttp import alerts
    return p.db.q("SELECT id, text, severity, channel FROM messages WHERE direction='out' AND kind='alert' "
                  "AND ref=? AND channel IS NOT ? ORDER BY id", (ref, alerts.QUIET))


def _needs(p, ref):
    from ttp import alerts
    ids = {m["id"] for m in p.db.q("SELECT id FROM messages WHERE ref=?", (ref,))}
    return [a for a in alerts.needs_you(p.db, time.time()) if a["id"] in ids]


def _failover_cfg():
    return {"core_provider": "fake", "providers": {"fake": {}, "other": {}},
            "coordinator": {"provider_failover_to": ["other"], "provider_failover_after_h": 1}}


def test_an_acknowledged_user_step_stays_parked_across_new_episodes_until_its_task_ends(env):
    from ttp import alerts, cli, userstep
    p = make(env)
    tid = _park_deferred(p)
    key = f"user-step:{tid}:sign-in"
    [m] = _chat_alerts(p, key)
    alerts.ack(p.db, m["id"], "parked until the key is back")
    assert not alerts.needs_you(p.db, time.time())
    # Its episode closes (a re-deferral, a sweep) and the step is synced again: no new item, no post.
    with p.db.tx():
        for ep in p.db.q("SELECT * FROM alerts WHERE key=? AND cleared IS NULL", (key,)):
            alerts._close(p.db, ep, time.time(), "re-deferred")
    # Re-deferred with new words (as a coordinator's task_update does): the same user step, still parked.
    from ttp.coordinator import defer_labels
    p.db.update_task(tid, labels=defer_labels(None, "example-cli auth status --parked",
                                              "parked until the user's own sign-in (`example-cli login`) is back"))
    userstep.sync_deferrals(p.db, now=time.time() + 60)
    assert len(_chat_alerts(p, key)) == 1 and not alerts.needs_you(p.db, time.time())
    assert alerts.condition_acked(p.db, key)
    cli.main(["task", "demo", "cancel", str(tid)])
    assert alerts.condition_acked(p.db, key) is None, "an ended task ends its acknowledgement"


def test_a_login_deferred_task_another_provider_can_run_waits_silently(env, monkeypatch):
    from ttp import alerts, failover, userstep
    from ttp.coordinator import defer_labels
    p = make(env)
    cfg = _failover_cfg()
    monkeypatch.setattr(failover, "targets", lambda c: ["other", "fake"])
    p.db.set_kv(alerts.BREAKER + "fake", {"open": True, "opened": time.time()})
    tid = p.db.add_task("measure the example tool", "s", kind="work", origin="coordinator",
                        labels=defer_labels(None, "example-cli auth status", PARK_WHY))
    t = p.db.task(tid)
    assert userstep.deferral_kind(t) == "sign-in"
    assert userstep.silent(p.db, t, "sign-in", cfg) == "another provider runs its work"
    assert userstep.sync_deferrals(p.db, [t], cfg=cfg) == [] and not alerts.needs_you(p.db, time.time())
    # Every other provider logged out too: the user's sign-in is the only way on, so it is an item.
    p.db.set_kv(alerts.BREAKER + "other", {"open": True, "opened": time.time()})
    assert userstep.sync_deferrals(p.db, [t], cfg=cfg) == [f"{tid}:sign-in"]
    # The other provider is back: the open item closes without a word to the user.
    p.db.set_kv(alerts.BREAKER + "other", {"open": False})
    n = p.db.one("SELECT COUNT(*) n FROM messages")["n"]
    userstep.sync_deferrals(p.db, [t], cfg=cfg)
    assert not alerts.needs_you(p.db, time.time()) and p.db.one("SELECT COUNT(*) n FROM messages")["n"] == n
    # A required pin: no other provider may run it.
    monkeypatch.setattr(failover, "required", lambda db, task: True)
    assert userstep.silent(p.db, t, "sign-in", cfg) is None


def test_an_acknowledged_logout_stays_quiet_until_the_login_comes_back_and_lapses_again(env):
    from ttp import alerts
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.open_breaker("fake", "logged out")
    d.alert("auth:fake", "fake is logged out", "high")
    [m] = _chat_alerts(p, "auth:fake")
    alerts.ack(p.db, m["id"], "known; its work runs elsewhere")
    alerts.close_key(p.db, "auth:fake", "episode ended", text="x")   # e.g. an upgrade's sweep
    d.alert("auth:fake", "fake is logged out", "high")
    assert len(_chat_alerts(p, "auth:fake")) == 1, "an acknowledged, unchanged logout paged again"
    assert not _needs(p, "auth:fake")
    # The login comes back, then lapses again: a real change, told once.
    p.db.set_kv(alerts.BREAKER + "fake", {"open": False})
    assert alerts.condition_acked(p.db, "auth:fake") is None
    alerts.close_key(p.db, "auth:fake", "logged in")
    d.open_breaker("fake", "logged out again")
    d.alert("auth:fake", "fake is logged out again", "high")
    assert _chat_alerts(p, "auth:fake")[-1]["text"] == "fake is logged out again"


def test_a_logout_whose_work_fails_over_is_a_feed_line_only(env, monkeypatch):
    from ttp import alerts
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    monkeypatch.setattr(d, "logout_waits", lambda prov, now=None: {"need": False, "to": ["other"]})
    d.logout_alert("fake", "fake is logged out")
    rows = p.db.q("SELECT severity, channel FROM messages WHERE kind='alert' AND ref='auth:fake'")
    assert rows and all(r["severity"] == "low" and r["channel"] == alerts.QUIET for r in rows)


def test_a_missing_ranking_key_with_a_ranking_in_use_is_no_needs_you_item(env):
    from ttp import alerts, rankfresh
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.needs_ranking_key(False)
    assert _needs(p, rankfresh.NEEDS_KEY)
    d.needs_ranking_key(True)   # a ranking stays in use: the open item is lowered to a feed line
    assert not _needs(p, rankfresh.NEEDS_KEY)
    assert len(_chat_alerts(p, rankfresh.NEEDS_KEY)) == 1
