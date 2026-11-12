# Parked-alert cases: an alert the user acknowledged stays quiet for its condition while that condition is
# unchanged, and nothing asks the user for what other providers or a fallback already cover (alerts.py
# condition_acked, userstep.silent, Daemon.logout_alert, Daemon.needs_ranking_key; a missing key is a status line).
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


def test_a_missing_ranking_key_is_a_status_line_with_or_without_a_ranking(env):
    # Until the user saves a ranking key, nothing alerts or asks about it (user msg #2681).
    from ttp import alerts, rankfresh
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.alert(rankfresh.NEEDS_KEY, rankfresh.NEEDS_KEY_TEXT, severity="normal")   # an item from before
    assert _needs(p, rankfresh.NEEDS_KEY)
    d.needs_ranking_key(False)   # no ranking at all: the open item is lowered to a feed line too
    assert not _needs(p, rankfresh.NEEDS_KEY)
    d.needs_ranking_key(True)
    assert not _needs(p, rankfresh.NEEDS_KEY) and len(_chat_alerts(p, rankfresh.NEEDS_KEY)) == 1
    open_ = p.db.q("SELECT severity FROM alerts WHERE key=? AND cleared IS NULL", (rankfresh.NEEDS_KEY,))
    assert [a["severity"] for a in open_] == ["low"]


def test_a_missing_chat_key_is_a_status_line_and_a_saved_key_resumes_its_work_unprompted(env, monkeypatch):
    # The chat endpoint's key is not saved yet: its logout is a status line, never a sign-in ask, even
    # for work pinned to it. Saving the key resumes routing on the next check_logins tick, with no
    # coordinator turn, and the pinned task may start (user msg #2681).
    from ttp import alerts, endpoint, project
    from ttp.daemon import Daemon
    from ttp.providers import get_provider
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "key-home")
    p = make(env)
    p.set_config("providers.chat", {"endpoint": "cloud", "base_url": "https://api.example.com/v1",
                                    "tiers": {"standard": {"model": "org/Base-Model-2"}}})
    d = Daemon(p.base)
    turns = []
    monkeypatch.setattr(d, "observe", lambda *a, **k: turns.append(a))
    tid = p.db.add_task("chat work", "", kind="work", tier="standard", priority=3, budget_usd=8.0, labels=[])
    p.db.update_task(tid, provider="chat")   # pinned: nothing else runs it, so a login would be 'needed'
    assert get_provider("chat").key_missing(d.cfg) and not get_provider("fake").key_missing(d.cfg)
    d.open_breaker("chat", "failed to authenticate: no key saved for endpoint 'cloud'")
    d.logout_alert("chat", "chat is logged out: log in")
    assert not _needs(p, "auth:chat") and not _chat_alerts(p, "auth:chat")
    line = p.db.one("SELECT severity, text FROM messages WHERE ref='auth:chat'")
    assert line["severity"] == "low" and "once a key is saved" in line["text"] and "log in" not in line["text"]
    assert d._no_room("chat", {"now": time.time(), "busy": {}}) == "auth"
    monkeypatch.setattr(endpoint, "verify", lambda *a, **k: (True, "HTTP 200"))
    endpoint.save("cloud", "https://api.example.com/v1", "sk-placeholder-0000", True, "HTTP 200")
    assert not get_provider("chat").key_missing(d.cfg)
    d.check_logins()   # the saved key changed the store's stamp: checked at once, not on the backoff
    assert not d._logged_out("chat") and alerts.breaker(p.db, "chat") is None
    assert d._no_room("chat", {"now": time.time(), "busy": {}}) != "auth"
    assert p.db.one("SELECT status FROM tasks WHERE id=?", (tid,))["status"] == "queued"
    assert not turns, "no coordinator turn was needed"



# --- reclassify: an alert asks only for what the user can and will do (alerts.reclassify) ---

def _raw_alert(p, key, text, severity="normal"):
    """An alert as raised before the rule: posted straight, its episode open."""
    if key.startswith("schedule:"):   # its schedule still fails
        p.db.x("INSERT OR REPLACE INTO schedules(name, kind, every_s, enabled, last_status, payload) "
               "VALUES(?, 'command', 3600, 1, ?, '{}')", (key.partition(":")[2], f"error: {text}"))
    return p.db.post("out", text, chat=None, kind="alert", severity=severity, ref=key)


def test_reclassify_closes_pre_rule_alerts_of_every_class_and_never_reraises_them(env, monkeypatch):
    from ttp import alerts, ghauth, projpause
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()
    ghauth._write(None, {"ok": False, "since": now, "checked_at": now, "error": "not logged in"})
    projpause.pause(p.db, "user", "away")
    p.db.set_kv("paused", True)
    since = int(projpause.current(p.db)["since"])
    p.db.set_kv(alerts.BREAKER + "chat", {"open": True, "opened": now})
    p.db.set_kv("gh_auth_down", True)
    tid = _park_deferred(p, "watch the PR", "the user's gh sign-in (`gh auth login`) on this host", "gh auth status")
    from ttp import userstep
    p.db.set_kv(userstep.STEPS, {f"{tid}:sign-in": {"source": "deferral", "kind": "sign-in"}})
    cases = {
        "auth:chat": "chat logged out: save the endpoint's key",
        "model-ranking-key": "No Artificial Analysis key: save one",
        "gh-auth": "gh has no login on a lab host",
        "schedule:pr-watch": "Schedule pr-watch failed twice in a row and does nothing until fixed: gh auth "
                             "status: you are not logged into any GitHub hosts. Run gh auth login",
        f"user-step:{tid}:sign-in": f"#{tid} waits on a sign-in only you can do: gh auth login on the lab host.",
        f"paused:{since}": "Project demo has been paused by the user.",
    }
    ids = {k: _raw_alert(p, k, t, "high" if k.startswith(("schedule", "auth")) else "normal") for k, t in cases.items()}
    assert {m["id"] for m in alerts.needs_you(p.db, now)} >= set(ids.values())
    done = alerts.reclassify(p.db, now)
    assert {d["id"] for d in done} == set(ids.values())
    assert all(d["why"].startswith("reclassified: ") for d in done)
    assert not ({m["id"] for m in alerts.needs_you(p.db, now)} & set(ids.values()))
    feed = " ".join(m["text"] for m in p.db.q("SELECT text FROM messages WHERE kind IN ('info','resolved')"))
    for rule in ("chat-endpoint", "ranking-key", "gh-no-login", "own-pause"):
        assert f"reclassified: {rule}" in feed
    # Raised again for the same condition: a quiet status line, never back under "needs you".
    d = Daemon(p.base)
    for k, t in cases.items():
        d.alert(k, t, "high")
    assert not ({m["id"] for m in alerts.needs_you(p.db, time.time())}
                - set(m["id"] for m in alerts.needs_you(p.db, now)))
    assert not [m for m in alerts.needs_you(p.db, time.time()) if m["text"] in cases.values()]


def test_reclassify_closes_a_hand_back_pause_but_not_a_coordinator_pause(env):
    from ttp import alerts, projpause
    p = make(env)
    projpause.pause(p.db, "project other", "handed back to the other copy")
    p.db.set_kv("paused", True)
    key = f"paused:{int(projpause.current(p.db)['since'])}"
    mid = _raw_alert(p, key, "Project demo has been paused.")
    assert alerts.not_an_ask(p.db, key) == "handback-pause"
    assert [d["id"] for d in alerts.reclassify(p.db, time.time())] == [mid]
    projpause.pause(p.db, "coordinator", "a disk check")
    assert alerts.not_an_ask(p.db, "paused:3") == ""


def test_reclassify_leaves_real_asks_and_decisions_open(env):
    from ttp import alerts
    p = make(env)
    keep = [
        p.db.post("out", "PR https://github.com/o/r/pull/1 is ready for review: take it out of draft?",
                  chat=None, kind="ask", severity="high"),
        p.db.post("out", "The deploy key only you hold has expired; save a new one.", chat=None, kind="ask",
                  severity="high"),
        p.db.post("out", "Keep the old cache or drop it?", chat=None, kind="ask", severity="normal"),
        _raw_alert(p, "schedule:nightly", "Schedule nightly failed twice in a row: its build script is missing.",
                   "high"),
    ]
    login = p.db.post("out", "Please run gh auth login on the lab host so PR checks work.", chat=None,
                      kind="ask", severity="high")
    done = alerts.reclassify(p.db, time.time())
    assert [d["id"] for d in done] == [login]
    left = {m["id"] for m in alerts.needs_you(p.db, time.time())}
    assert set(keep) <= left and login not in left


def test_unset_gh_route_queues_gh_calls_to_the_workstation_while_logged_out(env, monkeypatch):
    from ttp import ghauth, ghqueue, ghroute
    make(env)
    now = time.time()
    ghauth._write(None, {"ok": False, "since": now, "checked_at": now, "error": "not logged in"})
    sent = []
    monkeypatch.setattr(ghqueue, "enqueue", lambda args, input=None, cwd=None: sent.append(args) or 75)
    assert ghroute.known({}) == ghroute.WORKSTATION
    assert ghroute.run("gh", ["pr", "comment", "1", "-b", "x"], cfg={}) == 75 and sent
    none = {"delivery": {"gh_route": "none"}}
    sent.clear()
    assert ghroute.known(none) == "none" and ghroute.route(none) == ""
    assert ghroute.run("gh", ["pr", "comment", "1", "-b", "x"], cfg=none) == ghroute.HELD_RC and not sent
    ghauth._write(None, {"ok": True, "checked_at": now + 1})
    assert ghroute.known({}) == ""


def test_a_task_deferred_on_a_gh_login_is_held_for_the_queue_not_a_sign_in(env):
    from ttp import userstep
    p = make(env)
    tid = _park_deferred(p, "watch the PR", "the user's gh sign-in (`gh auth login`) on this host",
                         "gh auth status")
    t = p.db.task(tid)
    cfg = {"delivery": {"gh_route": "workstation"}}
    assert userstep.silent(p.db, t, "sign-in", cfg) == "held for the workstation gh queue"
    assert "only you" not in userstep.deferral_text(t, "sign-in", cfg)
    assert userstep.gh_held({"delivery": {"gh_route": "none"}}) == "held: no gh route"
    assert not _needs(p, f"user-step:{tid}:sign-in")


def test_a_schedule_failing_only_for_gh_login_is_a_held_status_line_not_a_fix_task(env):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d._schedule_broken({"name": "pr-watch"}, "error: exit 4: To get started with GitHub CLI, please run: gh auth login")
    assert not p.db.one("SELECT id FROM tasks WHERE title LIKE '%pr-watch%'")
    [m] = p.db.q("SELECT text, severity FROM messages WHERE kind='alert' AND ref='schedule:pr-watch'")
    assert m["severity"] == "low" and "is held" in m["text"] and "delivery.gh_route" in m["text"]
    assert not _needs(p, "schedule:pr-watch")
