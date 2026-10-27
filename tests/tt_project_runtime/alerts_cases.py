# Alerts tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new alerts tests here, not there.
from __future__ import annotations


def test_unreadable_config_starts_no_model_work_and_alerts_once(env, monkeypatch):
    """Neither project.json nor a last good copy: no coordinator turn or worker run starts on the
    defaults; one alert says so and clears once the file reads again."""
    from ttp import alerts
    from ttp.daemon import Daemon
    p = make(env)
    saved = p.config_path.read_text()
    p.config_path.write_text("{ broken")
    (p.state / "project.last-good.json").unlink(missing_ok=True)
    p.db.add_task("user work", "s", kind="work", tier="light", origin="user")
    p.db.post("in", "hello", chat="web")
    d = Daemon(p.base)
    assert d.cfg_status == "unavailable"
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append(a[0]) or 0)
    for _ in range(2):
        d._last_cfg = 0
        d.tick()
    assert started == [], "no turn or run routes on the defaults"
    eps = p.db.q("SELECT * FROM alerts WHERE key='config' AND cleared IS NULL")
    assert len(eps) == 1 and p.db.one("SELECT COUNT(*) n FROM messages WHERE ref='config'")["n"] == 1
    assert alerts.active(p.db, "config", eps[0]["raised"], time.time())
    with pytest.raises(RuntimeError):
        Daemon.start_run(d, "worker", "x", "fake", "light", str(p.root))
    p.config_path.write_text(saved)
    d._load_config()
    d.sweep_alerts()
    assert d.cfg_status == "ok"
    assert p.db.one("SELECT cleared FROM alerts WHERE key='config'")["cleared"]
    assert p.db.one("SELECT id FROM messages WHERE ref='config' AND kind='resolved'")



def test_a_machine_wide_alert_is_broadcast_by_one_project_and_shown_quietly_by_the_others(env, monkeypatch):
    p = make(env)
    from ttp import alerts, notifier
    monkeypatch.setattr(alerts, "owner_alive", lambda owner: True)   # no daemon runs in the test
    from ttp.cli import bootstrap
    from ttp.daemon import Daemon
    from ttp.web import attention
    repo2 = env["tmp"] / "repo2"
    subprocess.run(["git", "clone", "-q", str(env["repo"]), str(repo2)], check=True)
    q = bootstrap(repo2, "second", "Another project.", "fake")
    for x in (p, q):
        x.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
               (time.time(), "t", time.time()))
    Daemon(p.base).alert("auth:fake", "fake is logged out", "high")
    Daemon(q.base).alert("auth:fake", "fake is logged out", "high")
    assert [m["text"] for m in p.db.unread_for_chat("c1", 0)] == ["fake is logged out"]
    assert q.db.unread_for_chat("c1", 0) == [], "the second project told the chat again"
    assert [a["cleared"] for a in notifier.alerts_since(q, 0)] == [True], "the desktop would show it twice"
    assert [m["text"] for m in attention(q.db, time.time())] == ["fake is logged out"], "web and status show it"
    # A condition only one project has (its own schedule) is not machine-wide.
    Daemon(q.base).alert("coordinator", "The coordinator failed", "high")
    q.db.set_kv("coordinator_failures", 1)
    assert [m["text"] for m in q.db.unread_for_chat("c1", 0)] == ["The coordinator failed"]
    # Once the claimer's episode clears, the claim is released: the other project may broadcast.
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time() + 1, time.time() + 1))
    Daemon(p.base).sweep_alerts()
    q.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time() + 1, time.time() + 1))
    Daemon(q.base).sweep_alerts()
    assert [m["text"] for m in q.db.unread_for_chat("c1", 0)] == ["The coordinator failed"], \
        "a quiet episode clears quietly"
    Daemon(q.base).alert("auth:fake", "fake is logged out again", "high")
    Daemon(p.base).alert("auth:fake", "fake is logged out again", "high")
    assert [m["text"] for m in q.db.unread_for_chat("c1", 0)][-1] == "fake is logged out again"
    assert [m["text"] for m in p.db.unread_for_chat("c1", 0)][-1] == "Cleared: fake is logged in again; its queued work starts."



def _two_projects(env):
    from ttp.cli import bootstrap
    p = make(env)
    repo2 = env["tmp"] / "repo2"
    subprocess.run(["git", "clone", "-q", str(env["repo"]), str(repo2)], check=True)
    q = bootstrap(repo2, "second", "Another project.", "fake")
    for x in (p, q):
        x.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
               (time.time(), "t", time.time()))
    return p, q



def test_a_claim_from_an_earlier_logout_does_not_silence_the_next_one(env, monkeypatch):
    # p broadcast the first logout and stays idle (its episode and claim stay open); q posted it
    # quietly. The user logs in again and only q runs, so only q's episode clears.
    from ttp import alerts
    from ttp.daemon import Daemon
    monkeypatch.setattr(alerts, "owner_alive", lambda owner: True)
    p, q = _two_projects(env)
    Daemon(p.base).alert("auth:fake", "fake is logged out", "high")
    Daemon(q.base).alert("auth:fake", "fake is logged out", "high")
    assert q.db.unread_for_chat("c1", 0) == []
    stale = json.loads(alerts.claims_path().read_text())
    q.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time() + 1, time.time() + 1))
    Daemon(q.base).sweep_alerts()
    assert _episodes(p, "auth:fake")[0]["cleared"] is None and _episodes(q, "auth:fake")[0]["cleared"]
    assert json.loads(alerts.claims_path().read_text()) == {}, "the clear drops the idle project's claim"
    Daemon(q.base).alert("auth:fake", "fake is logged out again", "high")
    assert [m["text"] for m in q.db.unread_for_chat("c1", 0)] == ["fake is logged out again"], \
        "a new logout reached no chat"
    # Even a claim the clear could not drop (an older runtime, a failed write) is stale for a
    # project whose own episode cleared after it was taken.
    q.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time() + 2, time.time() + 2))
    Daemon(q.base).sweep_alerts()
    alerts.claims_path().write_text(json.dumps(stale))
    Daemon(q.base).alert("auth:fake", "fake is logged out a third time", "high")
    assert [m["text"] for m in q.db.unread_for_chat("c1", 0)][-1] == "fake is logged out a third time"



def test_a_stopped_claimers_claim_does_not_silence_the_others(env, monkeypatch):
    from ttp import alerts
    from ttp.daemon import Daemon
    p, q = _two_projects(env)
    running = {str(p.base), str(q.base)}
    monkeypatch.setattr(alerts, "owner_alive", lambda owner: owner in running)
    start = time.time()
    clock = {"t": start}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    Daemon(p.base).alert("auth:fake", "fake is logged out", "high")
    dq = Daemon(q.base)
    dq.open_breaker("fake", "logged out")
    dq.alert("auth:fake", "fake is logged out", "high")
    assert q.db.unread_for_chat("c1", 0) == []
    running.discard(str(p.base))   # p's daemon stops for good, still holding the claim
    clock["t"] = start + alerts.REMIND_S + 1800   # before the claim's TTL runs out
    assert clock["t"] - start < alerts.CLAIM_TTL
    dq.remind_logouts()
    assert [m["text"] for m in q.db.unread_for_chat("c1", 0)][0].startswith("fake on testhost is still logged out"), \
        "the reminder was left to a stopped project"
    from ttp.project import hostname
    assert json.loads(alerts.claims_path().read_text())[f"{hostname()}|auth:fake"]["owner"] == str(q.base)



def test_a_logout_is_one_alert_per_machine_reminded_at_1_4_and_12_hours_with_what_waits(env, monkeypatch):
    from ttp import alerts
    from ttp.daemon import Daemon
    p, q = _two_projects(env)
    running = {str(p.base), str(q.base)}
    monkeypatch.setattr(alerts, "owner_alive", lambda owner: owner in running)
    start = time.time()
    clock = {"t": start}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    dp, dq = Daemon(p.base), Daemon(q.base)
    for d in (dp, dq):
        d.open_breaker("fake", "logged out")
        d.alert("auth:fake", "fake is logged out", "high")
    q.db.add_task("queued work", "")
    q.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
           (start, "watch", "obs", "high", "a box is down", "queued"))
    told = lambda x: [m["text"] for m in x.db.unread_for_chat("c1", 0)]
    seen = []
    for hours in (0.5, 1.01, 2, 3.9, 4.01, 8, 11.9, 12.01, 20, 35.9, 36.01):
        clock["t"] = start + hours * 3600
        for d in (dq, dp):   # each project reports every few minutes; here the claimer goes last
            d._logout_round = 0.0
            d.remind_logouts()
        seen.append((hours, len(told(p))))
    assert told(q) == [], "the second project paged the chat too"
    sent = told(p)
    assert sent[0] == "fake is logged out" and len(sent) == 5, sent
    assert [h for (h, n), (_, m) in zip(seen[1:], seen) if n > m] == [1.01, 4.01, 12.01, 36.01]
    assert all("still logged out" in t and "2 project(s) wait on it (demo, second)" in t
               and "1 queued task(s) and 1 high event(s)" in t for t in sent[1:]), sent[1]
    rows = p.db.q("SELECT channel, severity FROM messages WHERE ref='auth:fake'")
    assert {(r["channel"], r["severity"]) for r in rows} == {("chat", "high")}, "a reminder went out quietly"
    assert len(_episodes(p, "auth:fake")) == 1, "a reminder opened a second episode"
    # The claimer stops: the other project keeps the cadence without repeating a reminder.
    running.discard(str(p.base))
    clock["t"] = start + 36.5 * 3600
    dq._logout_round = 0.0
    dq.remind_logouts()
    assert told(q) == []
    clock["t"] = start + 60.01 * 3600
    dq._logout_round = 0.0
    dq.remind_logouts()
    assert len(told(q)) == 1 and "1 project(s) wait on it (second)" in told(q)[0]
    # Logged in again: no more reminders.
    dq._close_breaker("fake", alerts.breaker(q.db, "fake"), "login check passed")
    clock["t"] = start + 90 * 3600
    dq._logout_round = 0.0
    dq.remind_logouts()
    assert len(told(q)) == 1



def test_account_mismatch_then_match_keeps_caps_meanwhile_and_alerts_once(env, monkeypatch):
    """A login on another account than the plan readings, its kind unknown: the dollar caps hold it
    (the stricter side), one self-clearing status line says so (never 'needs you'), and once a run of
    the logged-in account reports its windows, the plan windows gate it again with no manual step and
    the line clears itself."""
    p = make(env)
    from ttp import alerts, budget as bud
    from ttp.daemon import Daemon
    now = time.time()
    _old_plan(p, now)
    from ttp.providers.claude import Claude
    monkeypatch.setattr(Claude, "account", lambda self: "acct-b | plan")
    bud.note_account(p.db, "claude", "acct-b | plan")     # the login switched
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and g.numbers.get("account_mismatch") and any(
        "no plan readings of its own yet" in r for r in g.reasons), g.reasons
    assert not any("check the login" in r or "another account" in r for r in g.reasons), g.reasons
    p.set_config("core_provider", "claude")
    d = Daemon(p.base)
    d.update_gates()
    d.update_gates()                                      # a repeat does not post again
    sent = p.db.q("SELECT * FROM messages WHERE ref='account-mismatch:claude'")
    assert len(sent) == 1 and sent[0]["severity"] == "low", [dict(m) for m in sent]
    assert "log in" not in sent[0]["text"] and "clears by itself" in sent[0]["text"]
    assert alerts.holds(p.db, "account-mismatch:claude", now, now)
    assert not alerts.needs_you(p.db, time.time()), "a status line, never 'needs you'"
    assert any(m["state"] == "status" and "no plan readings" in m["text"] for m in alerts.feed(p.db, time.time()))
    # the next run on the logged-in account reports its windows
    _run(p, "acct-b | plan", now - 120, now - 30, 0.5)
    _reading(p, time.time(), "acct-b | plan", "five_hour", 7.0, now + 3600)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db), time.time())
    assert g.regime == "windows" and not g.numbers.get("account_mismatch"), g.reasons
    d.update_gates()
    assert not alerts.holds(p.db, "account-mismatch:claude", now, time.time())
    alerts.sweep(p.db)
    assert p.db.one("SELECT cleared FROM alerts WHERE key='account-mismatch:claude'")["cleared"]



def _billed_switch(p, account="acct-b | contracted"):
    """The login moved off the plan account to `account`, and the budget fingerprint recorded the
    plan -> billed change for it (budgetkind.apply's event; it reflects the gate, so it is no evidence)."""
    from ttp import budget as bud, budgetkind
    bud.note_account(p.db, "claude", account)
    ts = time.time() - 300
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,data) VALUES(?,?,?,?,?,?,?)",
           (ts, "daemon", budgetkind.KIND, "normal", "claude budget changed", "handled",
            json.dumps({"provider": "claude", "old": {"account": budgetkind.account_hash("acct-a | plan"), "kind": "plan"},
                        "new": {"account": budgetkind.account_hash(account), "kind": "billed"}})))
    return ts



def test_switch_to_usage_billed_account_is_a_neutral_line_not_needs_you(env, monkeypatch):
    """The reported case: plan readings of account A, claude deliberately switched to a usage-billed
    account B, whose paid runs since report no plan windows (the lapse rule). The old readings are history: no 'another
    account' reason and no alert or 'needs you', a neutral 'billed by use since' line in the gate's
    reasons (status, web app, coordinator digest), and the dollar caps still gate it. An alert raised
    for the condition before clears itself."""
    p = make(env)
    from ttp import alerts, budget as bud, timefmt
    from ttp.daemon import Daemon
    from ttp.providers.claude import Claude
    now = time.time()
    _old_plan(p, now)
    monkeypatch.setattr(Claude, "account", lambda self: "acct-b | contracted")
    # an alert raised for the condition by an earlier version (normal, 'needs you' then)
    p.db.post("out", "claude is logged in to another account than its plan readings", chat=None, kind="alert",
              severity="normal", ref="account-mismatch:claude")
    bud.note_account(p.db, "claude", "acct-b | contracted")
    since = now - 500
    _run(p, "acct-b | contracted", since, now - 400, 0.3)
    _run(p, "acct-b | contracted", now - 300, now - 200, 0.3)
    p.set_config("budget.daily_usd", 5)
    p.db.spend("claude", 9.0, "task:2", account="acct-b | contracted", ts=now - 60)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and not g.numbers.get("account_mismatch"), g.reasons
    assert not any("another account" in r or "plan readings" in r or "login" in r for r in g.reasons), g.reasons
    line = f"claude billed by use since {timefmt.short(since, p.config(), now)}"
    assert line in g.reasons and g.numbers["billed_by_use_since"] == since, g.reasons
    assert g.level == "red" and any("cap reached" in r for r in g.reasons), g.reasons   # the dollar cap still applies
    p.set_config("core_provider", "claude")
    Daemon(p.base).update_gates()
    assert len(p.db.q("SELECT * FROM messages WHERE ref='account-mismatch:claude'")) == 1, "no new alert"
    assert not alerts.holds(p.db, "account-mismatch:claude", now, time.time())
    alerts.sweep(p.db)
    assert p.db.one("SELECT cleared FROM alerts WHERE key='account-mismatch:claude'")["cleared"]
    assert not [m for m in alerts.needs_you(p.db, time.time()) if "account" in (m.get("ref") or "")]
    assert line in p.db.kv("gates")["claude"]["reasons"]



@pytest.mark.parametrize("stale", [False, True])
def test_usage_billed_account_on_day_one_is_never_told_to_log_in(env, monkeypatch, stale):
    """A usage-billed account before its first run: no plan readings at all (a new project), or only an
    old plan account's (stale). Neither is a login problem: no gate reason, alert, feed line or
    'needs you' item says log in or names the login."""
    p = make(env)
    from ttp import alerts, budget as bud
    from ttp.daemon import Daemon
    from ttp.providers.claude import Claude
    now = time.time()
    if stale:
        _old_plan(p, now)
    monkeypatch.setattr(Claude, "account", lambda self: "acct-b | contracted")
    bud.note_account(p.db, "claude", "acct-b | contracted")
    p.set_config("core_provider", "claude")
    Daemon(p.base).update_gates()
    said = list(p.db.kv("gates")["claude"]["reasons"]) + [m["text"] for m in p.db.q("SELECT text FROM messages")]
    said += [m["text"] for m in alerts.feed(p.db, time.time())]
    assert not [t for t in said if re.search(r"\blog(?:ged)?[ -]?(?:in|out)\b|\blogin\b", t, re.I)], said
    assert not alerts.needs_you(p.db, time.time())



@pytest.mark.parametrize("text", [
    "claude is logged in to another account than its plan readings. Please log in again to the plan account.",
    "Plan readings for claude are missing since the switch; please check which account claude is logged in as.",
    "claude may be logged out: no plan windows reported for 2 days. Run claude there and use /login.",
])
def test_a_login_ask_or_notify_needs_a_real_auth_failure(env, text):
    """The coordinator cannot tell the user to log an agent CLI in again from plan metering alone:
    with no open auth breaker or auth alert the ask and the notify are refused. A real auth failure
    (a run refused at the login opens the breaker) lets the same ask through, and a login ask about
    something that is not an agent CLI is not this gate's."""
    from ttp import alerts, coordinator as coord
    from ttp.daemon import Daemon
    p = make(env)
    problems, ask = _ask(p, text, blocking="access", recommendation="log in once")
    assert problems and "no agent CLI here is logged out" in problems[0] and ask is None, problems
    assert p.db.one("SELECT 1 FROM events WHERE kind=? AND text LIKE 'false login:%'", (coord.ASK_REFUSED_KIND,))
    problems = coord.apply(p, [{"type": "notify", "text": text, "severity": "normal"}])
    assert problems and "notify rejected" in problems[0], problems
    assert not p.db.one("SELECT 1 FROM messages WHERE kind='alert' AND text=?", (text,))
    other = "The VPN portal session expired; please sign in again to the VPN."
    assert coord._false_login_ask(p.db, other) == ""
    Daemon(p.base).open_breaker("claude", "Failed to authenticate: OAuth session expired")
    assert alerts.breaker(p.db, "claude")
    assert coord._false_login_ask(p.db, text) == ""
    problems, ask = _ask(p, text, blocking="access", recommendation="log in once")
    assert problems == [] and ask is not None, problems



@pytest.mark.parametrize("text", [
    "gh on the device box is logged out, so Claude cannot push. Please run gh auth login again there.",
    "The Slurm account token expired; please re-authenticate with kinit so the claude workers can submit jobs.",
    "The build server's SSO session expired, so codex runs cannot fetch artifacts; please log in again there.",
    "Claude cannot reach the device box: the host is logged out after the reboot. Please log in again on the console.",
    "The fake data server is logged out; please log in again.",
    # review #2354: a provider before a clause break, after the phrase, or generic 'subscription'/'metering'
    "Claude needs the NAS share: please log in again on the file server.",
    "The license portal session expired, so claude cannot use the subscription seat; please log in again there.",
    "Please log in again to the dashboard so claude can read the device metering data.",
    "The license server logged out the claude runs; please log in again on the license portal.",
    "The claude build server is logged out; please log in again on it.",
    "Claude logged out of the NAS share; please log in again on the file server.",
    "The claude session on the jump host ended; please log in again on the jump host.",
    "Codex's login to the artifact store failed; please log in again there.",
])
def test_a_login_ask_about_another_credential_is_never_refused(env, text):
    """Reviews #2338 and #2354: the gate refuses only a login of the agent CLI itself (the provider is
    grammatically what is logged in) or one citing a plan-metering cue. An ask
    about another credential or tool (gh, Slurm, kinit, SSO, a host) that only names a provider in
    passing is a real ask, sent with no auth breaker open. The test-only fake provider is no name to
    match unless the project's config uses it."""
    from ttp import alerts, coordinator as coord
    p = make(env)
    p.set_config("core_provider", "claude")
    assert not any(alerts.breaker(p.db, n) for n in ("claude", "codex", "cursor", "fake"))
    assert coord._false_login_ask(p.db, text, p.config()) == ""
    problems, ask = _ask(p, text, blocking="access", recommendation="log in once")
    assert problems == [] and ask is not None, problems
    problems = coord.apply(p, [{"type": "notify", "text": text, "severity": "normal"}])
    assert problems == [], problems



def test_recorded_billed_change_from_the_mismatch_gate_is_no_evidence(env, monkeypatch):
    """Review #2298: budgetkind classifies the mismatch gate (dollar caps) as 'billed', so a switch
    to an account of unknown kind records a plan -> billed change after the debounce. That change
    reflects the gate itself: the warning stays a low status line until the account's own runs say
    which it is."""
    p = make(env)
    from ttp import alerts, budget as bud, budgetkind
    from ttp.daemon import Daemon
    from ttp.providers.claude import Claude
    now = time.time()
    _old_plan(p, now)
    p.set_config("core_provider", "claude")
    monkeypatch.setattr(Claude, "account", lambda self: "acct-a | plan")
    bud.note_account(p.db, "claude", "acct-a | plan")
    d = Daemon(p.base)
    d.update_gates()                                       # the baseline
    monkeypatch.setattr(Claude, "account", lambda self: "acct-b | plan")
    bud.note_account(p.db, "claude", "acct-b | plan")
    monkeypatch.setattr(budgetkind, "DEBOUNCE_S", 0.0)
    for _ in range(3):
        d.update_gates()
    ev = p.db.q("SELECT data FROM events WHERE kind=?", (budgetkind.KIND,))
    assert ev and json.loads(ev[-1]["data"])["new"]["kind"] == "billed", "the circular event is recorded"
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db), time.time())
    assert g.numbers.get("account_mismatch") and "billed_by_use_since" not in g.numbers, g.reasons
    assert not any("billed by use since" in r for r in g.reasons), g.reasons
    sent = p.db.q("SELECT * FROM messages WHERE ref='account-mismatch:claude'")
    assert len(sent) == 1 and sent[0]["severity"] == "low", [dict(m) for m in sent]
    assert not alerts.needs_you(p.db, time.time())
    # real evidence: two paid runs of its own that reported no plan windows
    _run(p, "acct-b | plan", now - 500, now - 400, 0.3)
    _run(p, "acct-b | plan", now - 300, now - 200, 0.3)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db), time.time())
    assert not g.numbers.get("account_mismatch") and g.numbers["billed_by_use_since"] == now - 500, g.reasons



def test_plan_account_missing_its_readings_still_warns_as_status_line(env):
    """A plan account (it reported plan windows before) whose own readings are missing still warns,
    as a self-clearing status line, even with a plan -> billed change recorded for it."""
    p = make(env)
    from ttp import alerts, budget as bud
    from ttp.daemon import Daemon
    now = time.time()
    _old_plan(p, now)
    _reading(p, now - 10 * 86400, "acct-b | plan", "seven_day", 40.0, now - 9 * 86400)
    _billed_switch(p, "acct-b | plan")
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.numbers.get("account_mismatch") and "billed_by_use_since" not in g.numbers, g.reasons
    p.set_config("core_provider", "claude")
    Daemon(p.base).update_gates()
    sent = p.db.q("SELECT * FROM messages WHERE ref='account-mismatch:claude'")
    assert len(sent) == 1 and sent[0]["severity"] == "low"
    assert alerts.tracked("alert", "low", "account-mismatch:claude") and not alerts._top("low", "account-mismatch:claude")
    assert not alerts.needs_you(p.db, time.time())



def test_a_broken_command_schedule_can_be_turned_off_without_a_command(env):
    p = make(env)
    from ttp import alerts
    from ttp import coordinator as coord
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    # A legacy command schedule that stored an llm-style payload and so has no command.
    sched.upsert(p.db, "probe", "command", "30m", payload={"spec": "python3 check.py", "tier": "light"})
    d = Daemon(p.base)
    d.gates = {}
    for _ in range(3):
        p.db.x("UPDATE schedules SET next_run=? WHERE name='probe'", (time.time() - 1,))
        d.run_schedules()
        d.sweep_alerts()
        if fix := p.db.one("SELECT id FROM tasks WHERE labels LIKE '%schedule_fix:probe%'"):
            p.db.update_task(fix["id"], status="failed")   # its fix task could not fix it: now the alert
    assert [m for m in alerts.needs_you(p.db, time.time()) if "probe" in m["text"]]
    # Enabling it without a command is still rejected.
    out = coord.apply(p, [{"type": "schedule_set", "name": "probe", "enabled": True}])
    assert len(out) == 1 and "needs `command`" in out[0], out
    assert coord.apply(p, [{"type": "schedule_set", "name": "probe", "enabled": False}]) == []
    s = p.db.one("SELECT * FROM schedules WHERE name='probe'")
    assert not s["enabled"] and s["every_s"] == 1800
    d.sweep_alerts()
    assert not [m for m in alerts.needs_you(p.db, time.time()) if "probe" in m["text"]]
    assert p.db.one("SELECT cleared FROM alerts WHERE key='schedule:probe'")["cleared"]
    # Switching it back on still needs the command.
    out = coord.apply(p, [{"type": "schedule_set", "name": "probe", "enabled": True}])
    assert len(out) == 1 and "needs `command`" in out[0], out



def test_a_schedule_failing_twice_raises_one_alert_that_clears_on_an_ok_run(env):
    p = make(env)
    from ttp import alerts
    from ttp import schedule as sched
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    sched.upsert(p.db, "probe", "command", "30m", payload={})
    d = Daemon(p.base)
    d.gates = {}

    def tick():
        p.db.x("UPDATE schedules SET next_run=? WHERE name='probe'", (time.time() - 1,))
        d.run_schedules()
        d.sweep_alerts()

    def live():
        return [m for m in alerts.needs_you(p.db, time.time()) if "probe" in m["text"]]

    tick()
    assert not live()   # one failure may be a fluke
    assert "schedules failing: probe (no command)" in status_text(p)
    tick()
    # Two failures queue one harness task to fix it, with its errors; no alert yet.
    fixes = p.db.q("SELECT * FROM tasks WHERE labels LIKE '%schedule_fix:probe%'")
    assert len(fixes) == 1 and fixes[0]["kind"] == "harness" and "no command" in fixes[0]["spec"]
    assert "harness/schedules.json" in fixes[0]["spec"]
    tick()
    assert not live() and len(p.db.q("SELECT id FROM tasks WHERE labels LIKE '%schedule_fix:probe%'")) == 1
    # Its fix task done while the schedule still fails: now the alert, naming the task.
    p.db.update_task(fixes[0]["id"], status="done")
    tick()
    tick()
    assert len(live()) == 1 and f"#{fixes[0]['id']} ended done" in live()[0]["text"]
    assert p.db.one("SELECT COUNT(*) n FROM messages WHERE ref='schedule:probe' AND kind='alert'")["n"] == 1
    p.db.x("UPDATE schedules SET payload=? WHERE name='probe'", (json.dumps({"command": "true"}),))
    tick()
    assert not live()
    assert p.db.one("SELECT cleared FROM alerts WHERE key='schedule:probe'")["cleared"]
    assert "schedules failing" not in status_text(p)
    # A new failure stint after the ok run queues a new fix task.
    p.db.x("UPDATE schedules SET payload=? WHERE name='probe'", (json.dumps({}),))
    tick()
    tick()
    assert len(p.db.q("SELECT id FROM tasks WHERE labels LIKE '%schedule_fix:probe%'")) == 2 and not live()



_HUNG_TICK = """import sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from ttp.daemon import register_stack_dump
state = Path(sys.argv[1])
assert register_stack_dump(state)
(state / "stub.ready").write_text("1")


def tick():
    while True:
        pass


tick()
"""



def test_a_daemon_hung_inside_a_tick_is_dumped_restarted_alerted_once_and_cleared(env, monkeypatch, tmp_path):
    from ttp import alerts, service, watchdog
    p = make(env)
    stub = tmp_path / "stubs" / "hung_daemon.py"
    stub.parent.mkdir()
    stub.write_text(_HUNG_TICK)
    proc = subprocess.Popen([sys.executable, "-I", str(stub), str(p.state), str(RUNTIME)])
    live = {proc.pid}
    monkeypatch.setattr(watchdog, "_is_daemon", lambda pid: pid in live and _alive_child(pid, proc))
    monkeypatch.setattr(watchdog, "_alive", lambda pid: pid in live and _alive_child(pid, proc))
    monkeypatch.setattr(service, "installed", lambda p: {"kind": "systemd", "watchdog": True})
    calls = []

    def systemctl(*args, timeout):
        calls.append(args)
        if args[0] == "restart":   # systemd ends the hung daemon and starts a new one (this test process)
            proc.kill()
            proc.wait()
            (p.state / "daemon.pid").write_text(str(os.getpid()))
            live.add(os.getpid())
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(watchdog, "_systemctl", systemctl)
    try:
        deadline = time.time() + 30
        while not (p.state / "stub.ready").exists() and time.time() < deadline:
            time.sleep(0.1)
        assert (p.state / "stub.ready").exists(), "the stub daemon never started"
        (p.state / "daemon.pid").write_text(str(proc.pid))
        (p.state / "daemon.start").write_text(json.dumps({"pid": proc.pid, "started": time.time() - 3600,
                                                          "stacks": True}))
        hb = p.state / "heartbeat"
        hb.write_text(json.dumps({"pid": proc.pid, "started": time.time() - 3600}))
        old = time.time() - watchdog.HANG_S - 100
        os.utime(hb, (old, old))
        now = time.time()
        assert watchdog.check(p, now=now) == "stale"
        assert proc.poll() is None and not calls
        assert watchdog.check(p, now=now + watchdog.CONFIRM_S) == "restarted"
    finally:
        proc.kill()
        proc.wait()
    assert calls == [("restart", f"{service.unit_name(p)}.service")]
    rec = p.db.kv("daemon_hang")
    dump = pathlib.Path(rec["dump"])
    assert dump.parent == p.state / "incidents" and rec["restarted"] and rec["pid"] == proc.pid
    text = dump.read_text()
    assert "most recent call first" in text and "in tick" in text and "hung_daemon.py" in text, text
    alerts_sent = p.db.q("SELECT severity, text FROM messages WHERE ref='daemon-hang' AND kind='alert'")
    assert len(alerts_sent) == 1 and alerts_sent[0]["severity"] == "normal"
    assert "demo" in alerts_sent[0]["text"] and str(dump) in alerts_sent[0]["text"]
    assert "restarted it" in alerts_sent[0]["text"]
    assert alerts.holds(p.db, "daemon-hang", now, now)
    # The new daemon is in its first tick: the incident stays open, nothing is sent again.
    (p.state / "daemon.start").write_text(json.dumps({"pid": os.getpid(), "started": time.time()}))
    assert watchdog.check(p) == "ok" and p.db.kv("daemon_hang")
    # It completes one: the incident ends and the alert clears itself.
    (p.state / "heartbeat").write_text(json.dumps({"pid": os.getpid(), "started": time.time()}))
    assert watchdog.check(p) == "ok" and not p.db.kv("daemon_hang")
    closed = alerts.sweep(p.db)
    assert [ep["key"] for ep in closed] == ["daemon-hang"]
    assert len(p.db.q("SELECT id FROM messages WHERE ref='daemon-hang' AND kind='alert'")) == 1



def _alive_child(pid, proc):
    return pid != proc.pid or proc.poll() is None



def test_an_auth_failure_opens_a_breaker_that_survives_a_restart_and_holds_only_its_provider(env, monkeypatch):
    err = "Error: Authentication required. Please run 'agent login' first, or set CURSOR_API_KEY.\n"
    p, run, task, _, _ = _cli_run(env, monkeypatch, "cursor", "", stderr=err, rc=1)
    from ttp import alerts, cli
    from ttp import budget as bud
    from ttp.daemon import LOGGED_OUT_NOTE, Daemon
    from ttp.providers import get_provider
    assert run["status"] == "auth" and alerts.breaker(p.db, "cursor"), "the first auth failure opens the breaker"
    p.db.update_task(task["id"], provider="cursor")
    other = p.db.add_task("other", "s", kind="work", tier="light", origin="user")   # the core provider
    d = Daemon(p.base)   # a restart: the breaker is read back from the project's state
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, *a, **k: started.append(k["task"]["id"]) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda task: (str(p.root), None))
    for prov in ("fake", "cursor"):
        d.gates[prov] = bud.Gate(prov, regime="windows", max_parallel=6)
    monkeypatch.setattr(type(get_provider("cursor")), "login_check", lambda self: False)
    for _ in range(3):
        d.sweep_alerts()
        d.dispatch()
    assert started == [other], "a run started on the logged-out provider, or the other provider stopped"
    held = p.db.task(task["id"])
    assert held["status"] == "queued" and held["attempts"] == 0 and held["blocked_reason"].startswith(LOGGED_OUT_NOTE)
    assert p.db.one("SELECT COUNT(*) n FROM runs")["n"] == 1, "a run checked the login"
    line = [ln for ln in cli.status_text(p).splitlines() if ln.startswith("cursor is logged out")]
    assert len(line) == 1 and "next login check" in line[0] and "`" not in line[0], line



def test_the_login_check_backs_off_and_closes_the_breaker_and_its_alert(env, monkeypatch):
    p = make(env)
    from ttp import alerts
    from ttp.daemon import AUTH_CHECK_S, Daemon
    from ttp.providers import get_provider
    from ttp.web import attention
    d = Daemon(p.base)
    login = env["tmp"] / "logged-in"
    monkeypatch.setenv("TTP_FAKE_LOGIN", str(login))
    d.open_breaker("fake", "401")
    d.alert("auth:fake", "fake is logged out", "high")
    checks = []
    real = type(get_provider("fake")).login_check
    monkeypatch.setattr(type(get_provider("fake")), "login_check", lambda self: checks.append(1) or real(self))
    d.sweep_alerts()
    assert checks == [], "checked before the first backoff step"
    gaps = []
    for _ in range(len(AUTH_CHECK_S) + 1):
        b = alerts.breaker(p.db, "fake")
        p.db.set_kv("auth_breaker:fake", {**b, "next_check": time.time() - 1})
        d.sweep_alerts()
        gaps.append(round(alerts.breaker(p.db, "fake")["next_check"] - time.time()))
    assert gaps == [s for s in AUTH_CHECK_S[1:]] + [AUTH_CHECK_S[-1]] * 2, gaps
    assert len(checks) == len(AUTH_CHECK_S) + 1 and attention(p.db, time.time())
    login.write_text("")
    p.db.set_kv("auth_breaker:fake", {**alerts.breaker(p.db, "fake"), "next_check": time.time() - 1})
    d.sweep_alerts()
    assert alerts.breaker(p.db, "fake") is None and not d._logged_out("fake")
    assert p.db.one("SELECT cleared FROM alerts WHERE key='auth:fake'")["cleared"], "the alert outlived the breaker"
    assert attention(p.db, time.time()) == []
    assert p.db.one("SELECT COUNT(*) n FROM runs")["n"] == 0, "the login was checked with a model run"



@pytest.mark.parametrize("switch", [False, True])
def test_a_login_back_on_another_account_holds_one_check_then_starts_and_says_so(env, monkeypatch, switch):
    """A login that comes back as another account than before the logout may be a user mid-switch:
    nothing starts until one more check; still another account then, work starts, the chat hears
    the change (account kind only) and an event records it. The same account starts at once."""
    p = make(env)
    from ttp import alerts
    from ttp.daemon import ACCOUNT_SWITCH_EVENT, AUTH_CHECK_S, Daemon
    from ttp.providers import get_provider
    cls = type(get_provider("fake"))
    who = {"account": "one@example.com | plan_a", "kind": "plan_a"}
    monkeypatch.setattr(cls, "account", lambda self: who["account"])
    monkeypatch.setattr(cls, "account_kind", lambda self: who["kind"])
    login = env["tmp"] / "logged-in"
    monkeypatch.setenv("TTP_FAKE_LOGIN", str(login))
    p.db.x("INSERT INTO runs(role,provider,account,started,ended,status) VALUES('worker','fake',?,?,?,'ok')",
           (who["account"], time.time() - 60, time.time() - 50))
    d = Daemon(p.base)
    d.open_breaker("fake", "401")
    d.alert("auth:fake", "fake is logged out", "high")
    if switch:
        who.update(account="two@example.com | plan_b", kind="plan_b")
    login.write_text("")
    p.db.set_kv("auth_breaker:fake", {**alerts.breaker(p.db, "fake"), "next_check": time.time() - 1})
    d.sweep_alerts()
    events = lambda: p.db.q("SELECT * FROM events WHERE kind=?", (ACCOUNT_SWITCH_EVENT,))
    cleared = lambda: p.db.q("SELECT text FROM messages WHERE text LIKE 'Cleared: fake is logged in again%'")
    if not switch:
        assert alerts.breaker(p.db, "fake") is None, "the same account waited"
        assert not events() and len(cleared()) == 1 and "another account" not in cleared()[0]["text"]
        return
    b = alerts.breaker(p.db, "fake")
    assert b and b["account_hold"] and d._logged_out("fake"), "another account started at once"
    assert round(b["next_check"] - time.time()) == AUTH_CHECK_S[0] and not events() and not cleared()
    d.sweep_alerts()
    assert alerts.breaker(p.db, "fake"), "the hold ended before its check was due"
    p.db.set_kv("auth_breaker:fake", {**alerts.breaker(p.db, "fake"), "next_check": time.time() - 1})
    d.sweep_alerts()
    assert alerts.breaker(p.db, "fake") is None and not d._logged_out("fake")
    ev = events()
    assert len(ev) == 1 and ev[0]["status"] == "handled" and "plan_a before, plan_b now" in ev[0]["text"]
    text = cleared()[0]["text"]
    assert len(cleared()) == 1 and "another account than before the logout (plan_a before, plan_b now)" in text
    assert "@" not in text + ev[0]["text"] + ev[0]["data"], "an email address was shown or kept"



def test_a_login_back_on_the_old_account_after_its_hold_starts_without_a_note(env, monkeypatch):
    """A user who switches back within the hold's check gets the plain 'logged in again'."""
    p = make(env)
    from ttp import alerts
    from ttp.daemon import ACCOUNT_SWITCH_EVENT, Daemon
    from ttp.providers import get_provider
    cls = type(get_provider("fake"))
    who = {"account": "a | plan_a"}
    monkeypatch.setattr(cls, "account", lambda self: who["account"])
    login = env["tmp"] / "logged-in"
    monkeypatch.setenv("TTP_FAKE_LOGIN", str(login))
    d = Daemon(p.base)
    from ttp import budget as bud
    bud.note_account(p.db, "fake", who["account"])
    d.open_breaker("fake", "401")
    who["account"] = "b | plan_b"
    login.write_text("")
    for _ in range(2):
        p.db.set_kv("auth_breaker:fake", {**alerts.breaker(p.db, "fake"), "next_check": time.time() - 1})
        d.check_logins()
        if alerts.breaker(p.db, "fake"):
            who["account"] = "a | plan_a"   # switched back during the hold
    assert alerts.breaker(p.db, "fake") is None
    assert not p.db.q("SELECT id FROM events WHERE kind=?", (ACCOUNT_SWITCH_EVENT,))
    assert not (p.db.kv("auth_breaker:fake") or {}).get("switched")



def test_a_refusal_right_after_a_passed_login_check_falls_back_to_one_probe_run(env, monkeypatch):
    """A CLI whose status says logged in while the service still refuses it must not release the
    whole queue to fail again after every check."""
    p = make(env)
    from ttp import alerts
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    login = env["tmp"] / "logged-in"
    login.write_text("")
    monkeypatch.setenv("TTP_FAKE_LOGIN", str(login))
    d.open_breaker("fake", "401")
    p.db.set_kv("auth_breaker:fake", {**alerts.breaker(p.db, "fake"), "next_check": time.time() - 1})
    d.sweep_alerts()
    assert alerts.breaker(p.db, "fake") is None
    d.open_breaker("fake", "401 again")   # the next run was refused all the same
    for _ in range(2):
        p.db.set_kv("auth_breaker:fake", {**alerts.breaker(p.db, "fake"), "next_check": time.time() - 1})
        d.sweep_alerts()
    b = alerts.breaker(p.db, "fake")
    assert b and b["distrust"] and b["probe"] and d._may_probe("fake", time.time())
    # A run that gets through ends it, and the check is trusted again next time.
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time(), time.time()))
    d.sweep_alerts()
    assert alerts.breaker(p.db, "fake") is None
    d.open_breaker("fake", "logged out")
    assert not alerts.breaker(p.db, "fake").get("distrust")



def test_a_logged_out_alert_from_before_the_breaker_opens_it(env):
    p = make(env)
    from ttp import alerts
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    d.alert("auth:fake", "fake is logged out", "high")
    assert alerts.breaker(p.db, "fake") is None
    d.sweep_alerts()
    assert alerts.breaker(p.db, "fake"), "an upgrade dropped the logout"
    assert p.db.one("SELECT cleared FROM alerts WHERE key='auth:fake'")["cleared"] is None



def test_only_the_three_push_queue_alerts_reach_the_top_section_and_only_while_active(env):
    from ttp import alerts
    from ttp.daemon import Daemon
    from ttp.web import state_payload
    p, rid = _pq_rows(env)
    d = Daemon(p.base)
    now = time.time()
    for key in ("push_rejected", "after_push_failed", "push_queue_dying"):
        d.alert(key, f"{key} text", severity="high")
    # The rest is news for the coordinator or the feed, never the top section.
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
           (now, "daemon", "push_tip_failed", "high", "the tip fails", "queued"))
    p.db.post("out", "push batch b0 ended nothing", chat=None, kind="info", severity="normal")
    top = lambda: sorted(m["text"] for m in state_payload(p, p.db)["attention"])  # noqa: E731
    assert top() == ["after_push_failed text", "push_queue_dying text", "push_rejected text"]
    later = time.time() + 1
    _pq_batch_row(p, "b1", later, age=0, outcome="pushed", pushed_sha="cd" * 20, ended=later, finalized=later,
                  after_push="ok", after_finalized=later)
    assert top() == [], "a pushed and deployed batch clears all three"
    assert sorted(ep["key"] for ep in alerts.sweep(p.db, later + 1)) == [
        "after_push_failed", "push_queue_dying", "push_rejected"]
    feed = [m for m in state_payload(p, p.db)["feed"] if m["state"] == "cleared"]
    assert sorted(m["text"] for m in feed) == ["after_push_failed text", "push_queue_dying text", "push_rejected text"]



def test_a_long_project_pause_alerts_once_after_the_threshold_and_clears_on_resume(env):
    p = make(env)
    from ttp import alerts, projpause
    from ttp.daemon import Daemon

    def alerts_out():
        return p.db.q("SELECT * FROM messages WHERE direction='out' AND ref LIKE 'paused:%' ORDER BY id")
    p.db.set_paused(True, by="coordinator", reason="board firmware update")
    Daemon(p.base).tick()
    assert alerts_out() == []   # not before the threshold
    since = time.time() - projpause.DEFAULT_ALERT_S - 60
    p.db.set_kv(projpause.INFO_KEY, {**p.db.kv(projpause.INFO_KEY), "since": since})
    Daemon(p.base).tick()
    Daemon(p.base).tick()
    out = alerts_out()
    assert len(out) == 1 and out[0]["kind"] == "alert" and out[0]["severity"] == "normal", out
    assert "demo" in out[0]["text"] and "the coordinator" in out[0]["text"] and "board firmware update" in out[0]["text"]
    assert any(m["id"] == out[0]["id"] for m in alerts.needs_you(p.db, time.time()))
    # Resuming clears it at once, and says so once.
    p.db.set_paused(False, by="user", via="CLI")
    ep = p.db.one("SELECT * FROM alerts WHERE key=?", (out[0]["ref"],))
    assert ep["cleared"] and "resumed by the user" in ep["cleared_why"], ep
    assert not any(m["id"] == out[0]["id"] for m in alerts.needs_you(p.db, time.time()))
    Daemon(p.base).tick()
    assert [m["kind"] for m in alerts_out()] == ["alert", "resolved"]



def test_alerts_ack_of_a_live_condition_keeps_it_quiet_in_the_feed_until_it_clears(env, capsys):
    # Acking a condition that still holds must not close it: callers re-report every tick, and a
    # closed episode would page the user again at once.
    from ttp import alerts, cli
    from ttp.daemon import Daemon
    from ttp.web import attention
    p = make(env)
    d = Daemon(p.base)
    p.db.set_kv("coordinator_failures", 3)
    d.alert("coordinator", "The coordinator keeps failing", "high")
    mid = p.db.one("SELECT id FROM messages WHERE kind='alert'")["id"]
    cli.main(["alerts", "demo", "--ack", str(mid), "--why", "known; fixing the provider"])
    assert "kept in the feed" in capsys.readouterr().out
    ep = _episodes(p, "coordinator")[0]
    assert ep["cleared"] is None and alerts.acked(p.db, ep)["why"] == "known; fixing the provider"
    for every_s in (6 * 3600, 0):
        d.alert("coordinator", "The coordinator keeps failing", "high", every_s=every_s)
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert'")) == 1, "the ack paged the user again"
    assert attention(p.db, time.time()) == []
    feed = [m for m in alerts.feed(p.db, time.time()) if m["id"] == mid]
    assert feed and feed[0]["state"] == "acked" and feed[0]["acked_why"] == "known; fixing the provider"
    cli.main(["alerts", "demo", "--ack", str(mid), "--why", "a retry"])   # keeps the first receipt
    assert alerts.acked(p.db, _episodes(p, "coordinator")[0])["why"] == "known; fixing the provider"
    d.alert("coordinator", "The coordinator is down", "critical")   # a raised severity is new
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert'")) == 2
    p.db.set_kv("coordinator_failures", 0)
    [closed] = alerts.sweep(p.db)
    assert closed["cleared"] and "acknowledged: known; fixing the provider" in closed["cleared_why"]
    assert p.db.kv(alerts.ACKS) == {}
    p.db.set_kv("coordinator_failures", 1)
    d.alert("coordinator", "The coordinator fails again", "high")
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert'")) == 3, "a new occurrence alerts again"
    assert [m["text"] for m in attention(p.db, time.time())] == ["The coordinator fails again"]



def test_alerts_ack_covers_only_its_severity_and_a_raise_surfaces_again(env, monkeypatch):
    # An ack of a high alert must not hide the same episode once it escalates to critical.
    from ttp import alerts, cli
    from ttp.daemon import Daemon
    from ttp.web import attention
    p = make(env)
    d = Daemon(p.base)
    p.db.set_kv("coordinator_failures", 3)
    d.alert("coordinator", "x", "high")
    mid = p.db.one("SELECT id FROM messages WHERE kind='alert'")["id"]
    cli.main(["alerts", "demo", "--ack", str(mid), "--why", "known"])
    assert alerts.acked(p.db, _episodes(p, "coordinator")[0])["severity"] == "high"
    d.alert("coordinator", "DOWN", "critical")
    assert [m["text"] for m in attention(p.db, time.time())] == ["DOWN"]
    assert alerts.acked(p.db, _episodes(p, "coordinator")[0]) is None
    down = p.db.one("SELECT id FROM messages WHERE kind='alert' AND text='DOWN'")["id"]
    assert [m["state"] for m in alerts.feed(p.db, time.time()) if m["id"] == down] != ["acked"]
    d.alert("coordinator", "DOWN", "critical")   # a repeat dedups as usual, nothing quietly dropped
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert'")) == 2
    later = time.time() + alerts.REMIND_S + 60
    monkeypatch.setattr(time, "time", lambda: later)
    d.alert("coordinator", "DOWN", "critical")   # and reminds once REMIND_S has passed
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert'")) == 3
    cli.main(["alerts", "demo", "--ack", str(down), "--why", "on it"])   # a fresh ack covers the raise
    assert alerts.acked(p.db, _episodes(p, "coordinator")[0])["severity"] == "critical"
    assert attention(p.db, later) == []



def test_alerts_ack_of_an_outage_that_is_over_closes_it_and_releases_its_claim(env):
    from ttp import alerts, cli
    from ttp.daemon import Daemon
    from ttp.project import hostname
    p = make(env)
    Daemon(p.base).alert("auth:fake", "fake is logged out", "high")
    claim = f"{hostname()}|auth:fake"
    assert claim in json.loads(alerts.claims_path().read_text())
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time() + 1, time.time() + 1))   # logged in again, before any sweep
    mid = p.db.one("SELECT id FROM messages WHERE kind='alert'")["id"]
    cli.main(["alerts", "demo", "--ack", str(mid), "--why", "logged back in"])
    assert _episodes(p, "auth:fake")[0]["cleared"]
    assert claim not in json.loads(alerts.claims_path().read_text()), "the ack leaked the machine-wide claim"



def test_top_section_keeps_old_open_asks_and_drops_keyless_alerts_after_an_hour(env):
    p = make(env)
    from ttp.alerts import needs_you
    now = time.time()
    old = p.db.post("out", "Old question?", kind="ask", severity="high")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now - 30 * 86400, old))
    for i in range(310):   # many alerts must not push the ask out
        p.db.post("out", f"noise {i}", kind="alert", severity="low")
    stale = p.db.post("out", "Keyless old", kind="alert", severity="high")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now - 2 * 3600, stale))
    p.db.post("out", "Relabelled", kind="alert_old", severity="high")
    p.db.post("out", "Keyless fresh", kind="alert", severity="high")
    texts = [m["text"] for m in needs_you(p.db, now)]
    assert texts == ["Old question?", "Keyless fresh"], texts
    from ttp.web import health
    assert [a["text"] for a in health(p, p.db, now=now)["asks"]] == ["Old question?"]



def test_a_working_logged_out_probe_releases_the_coordinator_and_queue_before_it_ends(env, monkeypatch):
    """The user logged in and the probe runs for real: waiting for it to end would hold every other
    run (and unanswered messages) for up to the probe's whole run timeout."""
    p = make(env)
    from ttp import alerts
    d, started = _logged_out_daemon(p, monkeypatch)
    d.dispatch()
    assert started == ["small"]
    now = time.time()
    p.db.x("UPDATE alerts SET raised=raised-3600 WHERE key='auth:fake'")   # the logout came first
    # A run from before the logout, still spending, says nothing about the login now.
    p.db.x("INSERT INTO runs(role,provider,started,status,cost_usd) VALUES('worker','fake',?,'running',1.0)",
           (now - 7200,))
    p.db.set_kv("auth_probe:fake", now - 30)
    run = p.db.x("INSERT INTO runs(role,provider,started,status,cost_usd) VALUES('worker','fake',?,'running',0)",
                 (now - 30,))
    p.db.post("in", "I logged in, status?", chat="cli")
    p.db.x("UPDATE messages SET ts=ts-60")
    # Seconds in and nothing spent yet: still the one probe.
    d.sweep_alerts(); d.maybe_coordinate(); d.dispatch()
    assert started == ["small"], started
    # It spends tokens: the login works, everything starts on the same tick.
    p.db.x("UPDATE runs SET cost_usd=0.4 WHERE id=?", (run,))
    d.sweep_alerts(); d.maybe_coordinate(); d.dispatch()
    assert started[:2] == ["small", "coordinator"] and sorted(started[2:]) == ["big", "other"], started
    # Output tokens count too, priced or not; age alone does not.
    p.db.x("UPDATE runs SET cost_usd=0, started=? WHERE id=?", (time.time() + 1, run))
    assert alerts.holds(p.db, "auth:fake", time.time(), time.time() + 3600)
    p.db.x("UPDATE runs SET output_tokens=12 WHERE id=?", (run,))
    assert not alerts.holds(p.db, "auth:fake", time.time(), time.time() + 5)



def test_a_hung_logged_out_probe_holds_the_queue_until_it_shows_spend(env, monkeypatch, tmp_path):
    """A provider CLI that hangs or retries while still logged out must not release the queue just
    by running long: every queued run would then fail once on the logout. Through full ticks."""
    p = make(env)
    from ttp import alerts
    d, started = _logged_out_daemon(p, monkeypatch)
    d.dispatch()
    assert started == ["small"]
    now = time.time()
    p.db.x("UPDATE alerts SET raised=raised-3600 WHERE key='auth:fake'")
    p.db.set_kv("auth_breaker:fake", {**p.db.kv("auth_breaker:fake"), "opened": now - 3600})
    p.db.set_kv("auth_probe:fake", now - 600)
    run_dir = tmp_path / "probe"
    run_dir.mkdir()
    (run_dir / "lease").touch()
    (run_dir / "output.jsonl").write_text(json.dumps({"error": "retrying"}))   # output, but nothing spent
    run = p.db.x("INSERT INTO runs(role,provider,started,status,dir,boot_id) VALUES('worker','fake',?,'running',?,?)",
                 (now - 600, str(run_dir), d.boot))
    p.db.post("in", "status?", chat="cli")
    p.db.x("UPDATE messages SET ts=ts-60")
    for _ in range(3):
        d.tick()
    assert started == ["small"], "a probe hung past the old grace released the queue"
    assert alerts.holds(p.db, "auth:fake", now - 3600, time.time() + 3600)
    assert p.db.one("SELECT cleared FROM alerts WHERE key='auth:fake' ORDER BY id DESC")["cleared"] is None
    # It starts spending: the login works and everything starts on the next tick.
    (run_dir / "output.jsonl").write_text(json.dumps({"_cost": 0.3, "_output_tokens": 40}))
    (run_dir / "lease").touch()
    d._metered.clear()
    d.tick()
    row = p.db.one("SELECT cost_usd, output_tokens FROM runs WHERE id=?", (run,))
    assert (row["cost_usd"], row["output_tokens"]) == (0.3, 40), row
    assert p.db.one("SELECT cleared FROM alerts WHERE key='auth:fake' ORDER BY id DESC")["cleared"]
    assert "coordinator" in started and {"big", "other"} <= set(started), started



def test_boot_check_restores_an_empty_memory_file_and_a_truncated_index(env):
    p = make(env)
    from ttp import alerts
    from ttp.daemon import Daemon, KV_INTEGRITY
    a = p.add_memory("The build needs the release toolchain.", kind="fact")
    p.add_memory("Reviews go before every push.", kind="decision")
    good_entry, good_index = a.read_bytes(), p.memory_index.read_bytes()
    a.write_bytes(b"")                                        # what a cut write leaves
    p.memory_index.write_bytes(good_index[:len(good_index) // 2])
    stray = p.memory_dir / "fact-never-committed.md"
    stray.write_bytes(b"\0" * 64)
    d = Daemon(p.base)
    import resource

    def cpu_s() -> float:
        # CPU time of this process and its reaped children (the check's git calls): a slow check costs
        # more of it, a loaded host only stretches the wall clock.
        return sum(r.ru_utime + r.ru_stime for r in (resource.getrusage(resource.RUSAGE_SELF),
                                                     resource.getrusage(resource.RUSAGE_CHILDREN)))
    t0 = cpu_s()
    d.check_integrity(start=True)
    took = cpu_s() - t0
    assert a.read_bytes() == good_entry
    assert p.memory_index.read_bytes() == good_index
    assert not stray.exists() and (p.state / "damaged" / stray.name).exists()
    res = p.db.kv(KV_INTEGRITY)
    assert res["fsck"] == "ok" and not res["bad"] and len(res["restored"]) == 3, res
    assert took < 1, f"boot check used {took:.2f} s of CPU"   # about 0.07 s on an idle host
    assert p.db.one("SELECT id FROM messages WHERE kind='info' AND text LIKE '%repaired%'")
    assert not p.db.one("SELECT id FROM messages WHERE kind='alert'")
    clean = subprocess.run(["git", "-C", str(p.harness), "status", "--porcelain"], capture_output=True, text=True)
    assert clean.stdout == "", clean.stdout
    # The same boot is not checked again; nothing was left broken.
    at = res["at"]
    d.check_integrity(start=True)
    d.check_integrity()
    assert p.db.kv(KV_INTEGRITY)["at"] == at
    assert not alerts.holds(p.db, "integrity", at, time.time())



def test_boot_check_alerts_once_on_a_change_of_machine_and_clears(env, monkeypatch):
    p = make(env)
    from ttp import alerts
    from ttp.daemon import Daemon, KV_INTEGRITY_HOST
    assert p.raw_config().get("host") == "testhost"
    d = Daemon(p.base)
    d.check_integrity(start=True)
    assert not p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='integrity-host'")
    # The project folder now runs on another machine: one alert, not repeated by a restart.
    monkeypatch.setenv("TTP_HOST", "otherhost")
    d.check_integrity(start=True)
    Daemon(p.base).check_integrity(start=True)
    al = p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='integrity-host'")
    assert len(al) == 1 and "'testhost'" in al[0]["text"] and "'otherhost'" in al[0]["text"], al
    # It tells the coordinator a move re-points no task: open tasks' machine labels are its to fix.
    assert "changes no task labels" in al[0]["text"] and "task_update `resources`" in al[0]["text"], al[0]["text"]
    assert "`continues`" in al[0]["text"], al[0]["text"]
    assert p.db.kv(KV_INTEGRITY_HOST)["now"] == "otherhost"
    assert alerts.holds(p.db, "integrity-host", al[0]["ts"], time.time())
    # Confirmed by setting project.json's host: the hourly re-check clears it.
    p.set_config("host", "otherhost")
    d.check_integrity()   # not an hour yet
    assert p.db.kv(KV_INTEGRITY_HOST)
    monkeypatch.setattr(time, "time", lambda real=time.time: real() + 2 * 3600)
    d.check_integrity()
    assert p.db.kv(KV_INTEGRITY_HOST) is None
    assert not alerts.holds(p.db, "integrity-host", al[0]["ts"], time.time())



def _fake_ssh_g(env, monkeypatch, script: str) -> None:
    """An `ssh` on PATH that runs `script` (sh), so `ssh -G` never reads the user's own config."""
    bindir = env["tmp"] / "fakebin"
    bindir.mkdir(exist_ok=True)
    (bindir / "ssh").write_text("#!/bin/sh\n" + script)
    (bindir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")


def _host_alerts(p) -> list:
    return p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='integrity-host'")


def test_host_check_takes_a_machines_list_alias_for_this_machine_as_no_move(env, monkeypatch):
    """project.json names a machines-list alias whose hostname is this machine: no alert, and the
    alias is rewritten to this machine's short name."""
    p = make(env)
    from ttp import machines as mm
    from ttp.daemon import Daemon
    _fake_ssh_g(env, monkeypatch, 'echo "hostname $2"\n')   # no ssh config: an alias resolves to itself
    mm.add("box-a", hostname="testhost")
    p.set_config("host", "box-a")
    Daemon(p.base).check_integrity(start=True)
    assert not _host_alerts(p)
    assert p.raw_config()["host"] == "testhost"
    # An alias of the list for another machine is a move.
    mm.add("box-b", hostname="box-b.example")
    p.set_config("host", "box-b")
    Daemon(p.base).check_integrity(start=True)
    assert len(_host_alerts(p)) == 1 and p.raw_config()["host"] == "box-b"


def test_host_check_takes_the_fqdn_for_the_short_name(env, monkeypatch):
    p = make(env)
    from ttp import integrity
    monkeypatch.delenv("TTP_HOST")
    monkeypatch.setattr(socket, "gethostname", lambda: "box-a")
    monkeypatch.setattr(socket, "getfqdn", lambda *a: "box-a.example")
    _fake_ssh_g(env, monkeypatch, 'echo "hostname $2"\n')
    for stored in ("box-a.example", "BOX-A", "box-a"):
        p.set_config("host", stored)
        assert integrity.host_change(p) is None, stored
        assert p.raw_config()["host"] == "box-a"
    p.set_config("host", "box-b.example")
    assert integrity.host_change(p) == {"was": "box-b.example", "now": "box-a"}


def test_host_check_follows_ssh_aliases_and_falls_back_when_ssh_cannot_say(env, monkeypatch):
    p = make(env)
    from ttp import integrity, machines as mm
    from ttp.daemon import Daemon
    # `ssh -G` resolves the workstation's alias for this box to this machine: no move.
    _fake_ssh_g(env, monkeypatch, '[ "$2" = box-a-alias ] && echo "hostname testhost" || echo "hostname $2"\n')
    p.set_config("host", "box-a-alias")
    assert integrity.host_change(p) is None and p.raw_config()["host"] == "testhost"
    # An ssh alias for another machine still alerts.
    p.set_config("host", "box-b-alias")
    _fake_ssh_g(env, monkeypatch, 'echo "hostname box-b.example"\n')
    Daemon(p.base).check_integrity(start=True)
    assert len(_host_alerts(p)) == 1
    # ssh fails or times out: the plain compare decides (another name alerts, this one does not).
    _fake_ssh_g(env, monkeypatch, 'exit 255\n')
    p.set_config("host", "box-a-alias")
    assert integrity.host_change(p) == {"was": "box-a-alias", "now": "testhost"}

    def timeout(alias):
        raise subprocess.TimeoutExpired(["ssh", "-G", alias], 10)
    monkeypatch.setattr(mm, "ssh_hostname", timeout)
    assert integrity.host_change(p) == {"was": "box-a-alias", "now": "testhost"}
    p.set_config("host", "testhost")
    assert integrity.host_change(p) is None


def test_adopt_stores_this_machines_name_for_a_local_alias(env, monkeypatch):
    from ttp import cli, machines as mm
    from ttp.project import load_registry
    _fake_ssh_g(env, monkeypatch, '[ "$2" = box-a-alias ] && echo "hostname testhost" || echo "hostname $2"\n')
    mm.add("box-a", hostname="testhost")
    for given in ("box-a", "box-a-alias", "testhost"):
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["adopt", "demo", "--host", given, "--dir", "/srv/demo"])
        assert load_registry()["projects"]["demo"]["host"] == "testhost", given
    # Another machine keeps the name it was given.
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["adopt", "demo", "--host", "box-b", "--dir", "/srv/demo"])
    assert load_registry()["projects"]["demo"]["host"] == "box-b"


def test_boot_check_flags_an_empty_clone_under_a_kept_worktree(env):
    p = make(env)
    from ttp import alerts, integrity
    from ttp.daemon import Daemon, KV_INTEGRITY
    repo = env["tmp"] / "code"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@x"})
    done = p.db.add_task("old", "s", kind="code", tier="light", origin="user")
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (done,))
    wt = p.worktrees / f"t{done}"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt)], check=True)
    d = Daemon(p.base)
    d.check_integrity(start=True)
    assert p.db.kv(KV_INTEGRITY)["clones"] == []
    # The clone comes back without its .git (a copy that left it out): a finished task's kept worktree flags it.
    (repo / ".git").rename(env["tmp"] / "saved-git")
    p.db.set_kv(KV_INTEGRITY, {**p.db.kv(KV_INTEGRITY), "boot": "an earlier boot"})   # the start after a reboot
    d.check_integrity(start=True)
    res = p.db.kv(KV_INTEGRITY)
    assert res["clones"] == [{"repo": str(repo.resolve()), "tasks": [done], "empty": True}], res
    al = p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='integrity'")
    assert len(al) == 1 and "no .git directory" in al[0]["text"] and f"#{done}" in al[0]["text"], al
    assert alerts.holds(p.db, "integrity", al[0]["ts"], time.time())
    assert "no .git" in integrity.problem_text(res)
    # Restored: the next start checks again and the alert clears.
    (env["tmp"] / "saved-git").rename(repo / ".git")
    d.check_integrity(start=True)
    assert p.db.kv(KV_INTEGRITY)["clones"] == []
    assert not alerts.holds(p.db, "integrity", al[0]["ts"], time.time())



def test_boot_check_alerts_once_on_what_it_cannot_repair_and_clears(env, monkeypatch):
    p = make(env)
    from ttp import alerts, integrity
    from ttp.daemon import Daemon, KV_INTEGRITY
    p.add_memory("A fact.")
    blob = subprocess.run(["git", "-C", str(p.harness), "rev-parse", "HEAD:MEMORY.md"], capture_output=True,
                          text=True).stdout.strip()
    obj = p.harness / ".git" / "objects" / blob[:2] / blob[2:]
    obj.rename(obj.with_name("gone"))
    tid = p.db.add_task("code", "s", kind="code", tier="light", origin="user")
    wt = p.worktrees / f"t{tid}"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {env['tmp'] / 'nowhere'}\n")
    fscked = []
    real = integrity._git
    monkeypatch.setattr(integrity, "_git", lambda cwd, *a, **kw: (fscked.append(cwd) if a[0] == "fsck" else None)
                        or real(cwd, *a, **kw))
    d = Daemon(p.base)
    d.check_integrity(start=True)
    res = p.db.kv(KV_INTEGRITY)
    assert res["fsck"] == "failed" and res["bad"] and res["worktrees"][0]["task"] == tid, res
    assert fscked == [p.harness], "only the harness is fsck'd, never a code repository"
    al = p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='integrity'")
    assert len(al) == 1 and f"task #{tid}" in al[0]["text"]
    assert alerts.holds(p.db, "integrity", al[0]["ts"], time.time())
    # Repaired by hand: the next start checks again and the alert clears.
    obj.with_name("gone").rename(obj)
    (wt / ".git").unlink()
    wt.rmdir()
    d.check_integrity(start=True)
    assert not alerts.holds(p.db, "integrity", al[0]["ts"], time.time())
    assert len(p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='integrity'")) == 1



def test_pr_watch_records_a_pr_the_user_took_out_of_draft_as_their_approval(env, monkeypatch):
    """The user and the harness share one GitHub account. A PR that leaves draft with no run's gh call
    behind it was the user's own doing: it is recorded as their approval, with one low feed line and
    no high alert, and nothing on the PR is changed."""
    from ttp import alerts, coordinator as coord, prguard, watchers
    from ttp.daemon import Daemon
    p = make(env)
    old, new = "https://github.com/acme/widgets/pull/3", "https://github.com/acme/widgets/pull/7"
    for n, url in ((1, old), (2, new)):
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               (f"t{n}", "code", "done", url, time.time()))
    state = {old: {"isDraft": False}, new: {"isDraft": True}}
    monkeypatch.setattr(watchers, "_gh", lambda args, cwd: None if args[0] == "api" else
                        {"url": args[2], "state": "OPEN", "title": "t", "headRefOid": HEAD, **state[args[2]]})
    # PRs out of draft before this check existed are not flagged.
    p.db.set_kv("pr_signatures", {old: {"state": "OPEN", "draft": False}})
    d = Daemon(p.base)

    def alerting():
        return [m["text"] for m in p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref LIKE 'pr-ready:%'")
                if alerts.active(p.db, m["ref"], m["ts"], time.time())]

    watchers.watch_prs(d)
    assert alerting() == [] and not p.db.q("SELECT id FROM events WHERE kind LIKE 'pr_%ready%'")
    # A run's call on another PR long before #7 was last seen in draft says nothing about #7.
    _run_tried(p, "acme/widgets#8")
    _run_tried(p, "acme/widgets#7", ago=watchers.DAY)
    state[new]["isDraft"] = False      # the user took #7 out of draft on GitHub
    watchers.watch_prs(d)
    watchers.watch_prs(d)
    assert alerting() == [] and p.db.kv(prguard.UNAPPROVED_KEY) == {}
    assert not p.db.q("SELECT id FROM events WHERE kind='pr_unapproved_ready'")
    ev = p.db.q("SELECT text, severity, status FROM events WHERE kind='pr_ready_by_user'")
    assert len(ev) == 1 and new in ev[0]["text"] and ev[0]["severity"] == "low" and ev[0]["status"] == "handled"
    rec = p.db.kv(prguard.APPROVALS_KEY)["acme/widgets#7"]
    assert rec["channel"] == prguard.GITHUB and rec["head"] == HEAD and rec["spent"], rec
    # It is the user's approval for that time only: a run still may not mark it ready with it...
    assert prguard.approved(p.db, "acme/widgets#7") and not prguard.may_ready(p.db, "acme/widgets#7")
    # ...and back in draft it is gone, so leaving draft again is judged afresh.
    state[new]["isDraft"] = True
    watchers.watch_prs(d)
    assert "acme/widgets#7" not in (p.db.kv(prguard.APPROVALS_KEY) or {})
    state[old]["isDraft"] = True
    watchers.watch_prs(d)
    state[old]["isDraft"] = False      # #3 was grandfathered only until it went back to draft
    _run_tried(p, "acme/widgets#3")
    watchers.watch_prs(d)
    assert set(p.db.kv(prguard.UNAPPROVED_KEY)) == {"acme/widgets#3"}



def test_pr_watch_alerts_when_a_run_took_a_pr_out_of_draft(env, monkeypatch):
    """A PR left draft without approval and the runs' gh log shows a run marking it ready (or
    requesting reviewers, or a call whose PR it could not tell): a high alert names the run, the
    coordinator hears it once, and the PR is not put back in draft."""
    from ttp import alerts, coordinator as coord, prguard, watchers
    from ttp.daemon import Daemon
    p = make(env)
    urls = {n: f"https://github.com/acme/widgets/pull/{n}" for n in (7, 8, 9)}
    for url in urls.values():
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               ("t", "code", "done", url, time.time()))
    draft = {url: True for url in urls.values()}
    monkeypatch.setattr(watchers, "_gh", lambda args, cwd: None if args[0] == "api" else
                        {"url": args[2], "state": "OPEN", "title": "t", "headRefOid": HEAD, "isDraft": draft[args[2]]})
    p.db.set_kv("pr_signatures", {})
    d = Daemon(p.base)

    def alerting():
        return sorted((m["ref"], m["text"]) for m in p.db.q("SELECT * FROM messages WHERE kind='alert' "
                                                           "AND ref LIKE 'pr-ready:%'")
                      if alerts.active(p.db, m["ref"], m["ts"], time.time()))

    watchers.watch_prs(d)
    _run_tried(p, "acme/widgets#7", run="41", task="5")                       # refused, then got around it
    _run_tried(p, "acme/widgets#8", action="reviewers", refused=False, run="42", task="6")
    _run_tried(p, None, run="43", task="6")                                   # its PR could not be identified
    for url in urls.values():
        draft[url] = False
    watchers.watch_prs(d)
    watchers.watch_prs(d)
    shown = alerting()
    assert [r for r, _ in shown] == [f"pr-ready:acme/widgets#{n}" for n in (7, 8, 9)]
    assert "run 41 of task #5 marked it ready" in shown[0][1] and "refused" in shown[0][1]
    assert "run 42 of task #6 requested reviewers" in shown[1][1] and "run 43" in shown[2][1]
    assert all("Nothing on the PR was changed" in t for _, t in shown)
    ev = p.db.q("SELECT text, severity, status FROM events WHERE kind='pr_unapproved_ready' ORDER BY id")
    assert len(ev) == 3 and all(e["severity"] == "high" and e["status"] == "queued" for e in ev), "told once each"
    assert "run 41" in ev[0]["text"] and "never will" in ev[0]["text"]
    assert not p.db.q("SELECT id FROM events WHERE kind='pr_ready_by_user'")
    assert not (p.db.kv(prguard.APPROVALS_KEY) or {}), "a run's doing is never the user's approval"
    # The user approves #7 after the fact: its alert clears at once.
    said = p.db.post("in", f"yes, {urls[7]} can stay ready", chat="web", provenance="web-session")
    assert coord.apply(p, [{"type": "pr_approve", "id": said, "text": urls[7], "quote": "can stay ready"}]) == []
    assert [r for r, _ in alerting()] == ["pr-ready:acme/widgets#8", "pr-ready:acme/widgets#9"]
    alerts.sweep(p.db)
    assert p.db.one("SELECT cleared FROM alerts WHERE key='pr-ready:acme/widgets#7'")["cleared"]
    # The user puts #8 back in draft themselves: its flag goes.
    draft[urls[8]] = True
    watchers.watch_prs(d)
    assert set(p.db.kv(prguard.UNAPPROVED_KEY)) == {"acme/widgets#9"}



def test_pr_watch_drops_flags_for_cancelled_tasks_and_prs_it_cannot_read(env, monkeypatch):
    """A flagged PR whose task was cancelled, or that gh has not been able to read for a day, no longer
    holds its high alert."""
    from ttp import alerts, prguard, watchers
    from ttp.daemon import Daemon
    p = make(env)
    a, b = "https://github.com/acme/widgets/pull/7", "https://github.com/acme/widgets/pull/8"
    ids = [p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
                  ("t", "code", "done", url, time.time())) for url in (a, b)]
    readable = {a: True, b: True}
    monkeypatch.setattr(watchers, "_gh", lambda args, cwd: None if args[0] == "api" or not readable[args[2]] else
                        {"url": args[2], "state": "OPEN", "title": "t", "isDraft": False})
    p.db.set_kv("pr_signatures", {})
    _run_tried(p, "acme/widgets#7")
    _run_tried(p, "acme/widgets#8")
    d = Daemon(p.base)

    def alerting():
        return sorted(m["ref"] for m in p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref LIKE 'pr-ready:%'")
                      if alerts.active(p.db, m["ref"], m["ts"], time.time()))

    watchers.watch_prs(d)
    assert alerting() == ["pr-ready:acme/widgets#7", "pr-ready:acme/widgets#8"]
    # #7's task is cancelled: its flag goes on the next pass.
    p.db.x("UPDATE tasks SET status='cancelled' WHERE id=?", (ids[0],))
    # gh cannot read #8: the flag stays for a day, then goes.
    readable[b] = False
    watchers.watch_prs(d)
    assert set(p.db.kv(prguard.UNAPPROVED_KEY)) == {"acme/widgets#8"}
    assert alerting() == ["pr-ready:acme/widgets#8"]
    flags = p.db.kv(prguard.UNAPPROVED_KEY)
    flags["acme/widgets#8"]["seen"] -= watchers.DAY + 1
    p.db.set_kv(prguard.UNAPPROVED_KEY, flags)
    watchers.watch_prs(d)
    assert p.db.kv(prguard.UNAPPROVED_KEY) == {} and alerting() == []



def test_pr_watch_stops_at_its_deadline_keeps_what_it_read_and_resumes_there(env, monkeypatch):
    """Slow gh calls: a builtin run stops at its total deadline, keeps the PRs it read, reports one error
    that it was cut short (one issue however many runs are cut), and the next run starts at the PR it
    did not reach. A whole run closes the error."""
    from ttp import screen as scr, watchers
    from ttp.daemon import Daemon
    p = make(env)
    urls = [f"https://github.com/acme/widgets/pull/{n}" for n in range(1, 6)]
    for url in urls:
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               ("t", "code", "done", url, time.time()))
    clock, step, viewed = [1000.0], [30.0], []
    monkeypatch.setattr(watchers, "_now", lambda: clock[0])

    def slow_gh(args, cwd):
        clock[0] += step[0]   # each call takes `step` seconds
        if args[0] == "api":
            return None
        viewed.append(args[2])
        return {"url": args[2], "state": "OPEN", "title": "t", "isDraft": True, "headRefOid": HEAD}
    monkeypatch.setattr(watchers, "_gh", slow_gh)
    d = Daemon(p.base)
    errors = lambda: p.db.q("SELECT status, count FROM issues WHERE source=?",
                            (scr.error_source("watcher:prs"),))
    one = {"deadline_s": 90, "pr_workers": 1}   # one PR at a time: the fake clock is shared
    status = watchers.run_builtin(d, "prs", one)
    # Two PRs (a view and a GraphQL read each) take 120 s: the third is not started.
    assert status.startswith("cut short (2 of 5 PRs read"), status
    assert viewed == urls[:2] and set(p.db.kv("pr_signatures")) == set(urls[:2])
    assert [e["status"] for e in errors()] == ["open"]
    status = watchers.run_builtin(d, "prs", one)
    assert status.startswith("cut short") and viewed[2:] == urls[2:4]
    assert [e["status"] for e in errors()] == ["open"], "a second cut run is the same error"
    step[0] = 0.0
    assert watchers.run_builtin(d, "prs", one).startswith("ok (5 PRs")
    assert viewed[4:] == [urls[4], *urls[:4]], "it starts at the PR the cut run did not reach"
    assert [e["status"] for e in errors()] == ["fixed"]


def test_a_hanging_gh_call_is_killed_at_the_watchers_deadline(env, monkeypatch, tmp_path):
    """A gh that never answers holds a builtin run no longer than its deadline, whatever its per-call
    timeout, and the run reports itself cut short."""
    from ttp import watchers
    from ttp.daemon import Daemon
    p = make(env)
    for n in (1, 2, 3):
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               ("t", "code", "done", f"https://github.com/acme/widgets/pull/{n}", time.time()))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text("#!/bin/sh\nexec sleep 30\n")
    (bindir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    d = Daemon(p.base)
    t0 = time.monotonic()
    status = watchers.run_builtin(d, "prs", {"deadline_s": 1.5, "gh_timeout_s": 20})
    assert time.monotonic() - t0 < 6, "the tick stays bounded by the deadline"
    assert status.startswith("cut short (0 of 3 PRs read"), status


def test_builtin_watcher_bounds_come_from_the_payload_then_the_config_and_are_capped(env):
    from ttp import watchers
    from ttp.daemon import Daemon
    d = Daemon(make(env).base)
    assert watchers.bounds(d, {}) == (watchers.DEADLINE_S, watchers.GH_TIMEOUT_S)
    d.cfg = {**d.cfg, "watchers": {"deadline_s": 50, "gh_timeout_s": 10}}
    assert watchers.bounds(d, {}) == (50, 10)
    assert watchers.bounds(d, {"deadline_s": 5}) == (5, 5), "a call never outlasts the deadline"
    assert watchers.bounds(d, {"deadline_s": 10_000, "gh_timeout_s": "x"}) == (watchers.DEADLINE_MAX_S, 10)


HANGING_GH = r'''#!/bin/sh
# Fake gh: PR numbers listed in $HANG hang (a child of their own too), the rest answer at once with
# failing CI and no review comments.
for n in $HANG; do
  case "$*" in
    *"pull/$n "*|*"pull/$n") sleep 60 & echo $! >> "$PIDS"; echo $$ >> "$PIDS"; wait; exit 1 ;;
  esac
done
case "$1" in
  api) echo '{"data":{"repository":{"pullRequest":{"comments":{"nodes":[]},"reviews":{"nodes":[]},"reviewThreads":{"nodes":[]}}}}}' ;;
  pr) printf '{"url":"%s","state":"OPEN","isDraft":true,"title":"t","headRefOid":"abc","statusCheckRollup":[{"name":"build","conclusion":"FAILURE","status":"COMPLETED"}]}\n' "$3" ;;
  *) exit 1 ;;
esac
'''


def _fake_gh_prs(env, monkeypatch, tmp_path, n: int, hang: str):
    """n PR tasks on a made-up repo, and the fake gh on PATH; returns (project, urls, pids file)."""
    p = make(env)
    urls = [f"https://github.com/acme/widgets/pull/{i}" for i in range(1, n + 1)]
    for url in urls:
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               ("t", "code", "done", url, time.time()))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(HANGING_GH)
    (bindir / "gh").chmod(0o755)
    pids = tmp_path / "pids"
    pids.write_text("")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HANG", hang)
    monkeypatch.setenv("PIDS", str(pids))
    return p, urls, pids


def _all_gone(pids) -> bool:
    """Every pid the fake gh wrote is gone (a zombie waiting for its reaper counts as gone)."""
    for _ in range(40):
        left = []
        for pid in {int(x) for x in pids.read_text().split()}:
            try:
                with open(f"/proc/{pid}/stat") as f:
                    state = f.read().rsplit(")", 1)[1].split()[0]
            except OSError:
                continue
            if state != "Z":
                left.append(pid)
        if not left:
            return True
        time.sleep(0.05)
    return False


def test_pr_watch_reads_prs_in_parallel_and_a_hung_one_times_out_alone(env, monkeypatch, tmp_path):
    """Each PR's gh reads run in their own process groups, several at once, within a per-PR timeout:
    a hung PR times out, is reported not checked, and leaves no process; the others still report."""
    from ttp import watchers
    from ttp.daemon import Daemon
    p, urls, pids = _fake_gh_prs(env, monkeypatch, tmp_path, 3, "2")
    d = Daemon(p.base)
    t0 = time.monotonic()
    status = watchers.run_builtin(d, "prs", {"deadline_s": 30, "gh_timeout_s": 20, "pr_timeout_s": 1.5,
                                             "pr_workers": 2})
    assert time.monotonic() - t0 < 6, "the hung PR is cut at its own timeout, not the deadline"
    assert "acme/widgets#2 (1.5 s per-PR timeout)" in status and status.startswith("cut short (2 of 3"), status
    found = {r["task"] for r in p.db.q("SELECT task FROM events WHERE kind='pr_findings'")}
    ids = {r["pr_url"]: r["id"] for r in p.db.q("SELECT id, pr_url FROM tasks")}
    assert found == {ids[urls[0]], ids[urls[2]]}
    assert pids.read_text().split(), "the fake gh hung"
    assert _all_gone(pids), "the hung gh and its child were killed with their group"


def test_pr_watch_honours_the_schedules_deadline_up_to_its_ceiling(env):
    from ttp import watchers
    from ttp.daemon import Daemon, SCHEDULE_GRACE_S
    d = Daemon(make(env).base)
    assert watchers.bounds(d, {"deadline_s": 1000}) == (1000, watchers.GH_TIMEOUT_S)
    assert watchers.bounds(d, {"deadline_s": 10_000})[0] == watchers.DEADLINE_MAX_S == 1800
    s = {"name": "prs", "kind": "watcher"}
    assert d._schedule_timeout(s, {"builtin": "prs", "deadline_s": 1000}) == 1000 + SCHEDULE_GRACE_S
    assert watchers.pr_bounds(d, {}) == (watchers.PR_WORKERS, watchers.PR_TIMEOUT_S)
    assert watchers.pr_bounds(d, {"pr_workers": 999, "pr_timeout_s": 5}) == (watchers.PR_WORKERS_MAX, 5)


def test_pr_watch_rotates_so_a_hung_pr_cannot_starve_the_rest(env, monkeypatch):
    """A PR that outlasts the whole deadline does not hold the next runs: it counts as checked (timed
    out) and waits behind the others, so every PR is read within a bounded number of runs, from the
    durable record a new daemon reads too."""
    from ttp import watchers
    from ttp.daemon import Daemon
    p = make(env)
    urls = [f"https://github.com/acme/widgets/pull/{n}" for n in range(1, 7)]
    for url in urls:
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               ("t", "code", "done", url, time.time()))
    clock, viewed = [1000.0], []
    monkeypatch.setattr(watchers, "_now", lambda: clock[0])

    def gh(args, cwd):
        if args[0] == "api":
            clock[0] += 10
            return None
        viewed.append(args[2])
        if args[2] == urls[0]:
            clock[0] += 100   # hangs past its own timeout and the run's deadline
            return None
        clock[0] += 10
        return {"url": args[2], "state": "OPEN", "title": "t", "isDraft": True, "headRefOid": HEAD}
    monkeypatch.setattr(watchers, "_gh", gh)
    one = {"deadline_s": 90, "pr_workers": 1, "pr_timeout_s": 40}
    for _ in range(3):
        watchers.run_builtin(Daemon(p.base), "prs", one)
    assert viewed[0] == urls[0] and viewed[1] != urls[0], "the hung PR does not go first again"
    assert set(viewed[:viewed.index(urls[0], 1)]) == set(urls), "every PR read before the hung one's next turn"
    assert set(p.db.kv(watchers.PR_TRIED_KEY)) == set(urls)


def test_pr_watch_stops_reading_merged_and_closed_prs(env, monkeypatch):
    from ttp import watchers
    from ttp.daemon import Daemon
    p = make(env)
    urls = [f"https://github.com/acme/widgets/pull/{n}" for n in (1, 2, 3)]
    for url in urls:
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               ("t", "code", "done", url, time.time()))
    state = {u: "OPEN" for u in urls}
    calls = []

    def gh(args, cwd):
        calls.append(args[2] if args[0] == "pr" else args[-1])
        if args[0] == "api":
            return None
        return {"url": args[2], "state": state[args[2]], "title": "t", "isDraft": True, "headRefOid": HEAD}
    monkeypatch.setattr(watchers, "_gh", gh)
    d = Daemon(p.base)
    watchers.watch_prs(d)
    state[urls[1]], state[urls[2]] = "MERGED", "CLOSED"
    watchers.watch_prs(d)
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind='pr_changed' AND text LIKE '%MERGED%'")["n"] == 1
    calls.clear()
    assert watchers.watch_prs(d).startswith("ok (1 PRs")
    assert calls == [urls[0], "number=1"], "merged and closed PRs are not read again"


def test_pr_watch_logs_each_gh_calls_duration_and_what_a_cut_run_skipped(env, monkeypatch):
    from ttp import screen as scr, watchers
    from ttp.daemon import Daemon
    p = make(env)
    urls = [f"https://github.com/acme/widgets/pull/{n}" for n in (1, 2, 3)]
    for url in urls:
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               ("t", "code", "done", url, time.time()))
    clock = [1000.0]
    monkeypatch.setattr(watchers, "_now", lambda: clock[0])

    def gh(args, cwd):
        clock[0] += 7 if args[0] == "pr" else 3
        return None if args[0] == "api" else {"url": args[2], "state": "OPEN", "title": "t", "isDraft": True}
    monkeypatch.setattr(watchers, "_gh", gh)
    d = Daemon(p.base)
    status = watchers.run_builtin(d, "prs", {"deadline_s": 15, "pr_workers": 1})
    assert status.startswith("cut short (2 of 3 PRs read"), status
    assert "acme/widgets#2 (deadline: findings not read)" in status
    assert "acme/widgets#3 (deadline: not started)" in status
    line = [x for x in (p.logs / "daemon.log").read_text().splitlines() if "pr-watch:" in x][-1]
    assert "acme/widgets#1 view 7.0 s, graphql 3.0 s (no answer)" in line and "acme/widgets#2 view 7.0 s" in line
    assert "not checked: acme/widgets#2 (deadline: findings not read); acme/widgets#3 (deadline: not started)" in line
    issue = p.db.one("SELECT * FROM issues WHERE source=?", (scr.error_source("watcher:prs"),))
    assert issue is not None and issue["status"] == "open" and "cut short" in issue["title"], "never silent"


def _pr_tasks(p, urls):
    for url in urls:
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               ("t", "code", "done", url, time.time()))


EMPTY_BOT_DATA = {"data": {"repository": {"pullRequest": {"comments": {"nodes": []}, "reviews": {"nodes": []},
                                                          "reviewThreads": {"nodes": []}}}}}


def test_pr_watch_reads_and_lists_a_pr_several_tasks_name_once(env, monkeypatch):
    """A PR named by many tasks (a hand-off's `pr` sets each one's pr_url) is read once per run, and a
    run cut short names it once in its not-checked list, with the count of PRs left unchecked."""
    from ttp import prguard, watchers
    from ttp.daemon import Daemon
    p = make(env)
    base = "https://github.com/example-org/example-repo/pull/"
    _pr_tasks(p, [base + "1", *[base + "2"] * 10, base + "2/files", base + "2#issuecomment-1", base + "3"])
    clock, step, viewed = [1000.0], [10.0], []
    monkeypatch.setattr(watchers, "_now", lambda: clock[0])

    def gh(args, cwd):
        if args[0] == "api":
            return EMPTY_BOT_DATA
        clock[0] += step[0]
        viewed.append(prguard.pr_key(args[2]))
        return {"url": base + args[2].split("/pull/")[1][:1], "state": "OPEN", "title": "t", "isDraft": True,
                "headRefOid": HEAD}
    monkeypatch.setattr(watchers, "_gh", gh)
    one = {"deadline_s": 15, "pr_workers": 1}
    status = watchers.run_builtin(Daemon(p.base), "prs", one)
    assert status.startswith("cut short (2 of 3 PRs read") and "2 not checked:" in status, status
    assert status.count("example-org/example-repo#2 ") == 1 and status.count("example-org/example-repo#3 ") == 1
    assert viewed == ["example-org/example-repo#1", "example-org/example-repo#2"]
    step[0], viewed[:] = 0.0, []
    assert watchers.run_builtin(Daemon(p.base), "prs", one).startswith("ok (3 PRs"), "three PRs, not 13"
    assert sorted(viewed) == [f"example-org/example-repo#{n}" for n in (1, 2, 3)], "each PR fetched once"


def test_pr_watch_two_runs_check_every_pr_when_one_run_cannot(env, monkeypatch):
    """More PRs than one run's deadline fits, one of them named by several tasks: the next run starts
    where the cut one stopped, so two runs together read every PR."""
    from ttp import prguard, watchers
    from ttp.daemon import Daemon
    p = make(env)
    urls = [f"https://github.com/example-org/example-repo/pull/{n}" for n in range(1, 7)]
    _pr_tasks(p, [urls[0]] * 6 + urls[1:])
    clock, viewed = [1000.0], []
    monkeypatch.setattr(watchers, "_now", lambda: clock[0])

    def gh(args, cwd):
        if args[0] == "api":
            return EMPTY_BOT_DATA
        clock[0] += 10
        viewed.append(prguard.pr_key(args[2]))
        return {"url": args[2], "state": "OPEN", "title": "t", "isDraft": True, "headRefOid": HEAD}
    monkeypatch.setattr(watchers, "_gh", gh)
    one = {"deadline_s": 25, "pr_workers": 1}   # three reads a run
    for _ in range(2):
        assert watchers.run_builtin(Daemon(p.base), "prs", one).startswith("cut short (3 of 6")
    assert sorted(viewed) == sorted(prguard.pr_key(u) for u in urls), viewed


MIXED_PRS = {   # number: (view answer extras, BOT_QUERY pullRequest)
    1: ({"isDraft": True, "statusCheckRollup": [{"name": "build", "conclusion": "FAILURE", "status": "COMPLETED"}]},
        {}),
    2: ({"isDraft": False, "statusCheckRollup": [{"name": "build", "conclusion": "SUCCESS", "status": "COMPLETED"}]},
        {"reviewThreads": {"nodes": [{"isResolved": False, "isOutdated": False, "path": "a.py", "comments": {"nodes": [
            {"author": {"login": "lint[bot]"}, "body": "unused import", "url": "u2", "createdAt": "2026-01-01"}]}}]}}),
    3: ({"isDraft": False, "statusCheckRollup": [{"name": "build", "conclusion": "SUCCESS", "status": "COMPLETED"}]},
        {}),
    4: ({"isDraft": True, "statusCheckRollup": [{"name": "build", "conclusion": "SUCCESS", "status": "COMPLETED"}]},
        {"comments": {"nodes": [
            {"author": {"login": "lint[bot]"}, "body": "style nit", "url": "u4", "createdAt": "2026-01-01"},
            {"author": {"login": "someone"}, "body": "done", "url": "u4b", "createdAt": "2026-01-02"}]}}),
}


def test_pr_watch_parallel_reads_find_what_one_by_one_reads_find(env, monkeypatch):
    """A mixed set of PRs (draft or not, CI failing or green, bot comments open or answered), one named by
    two tasks: reading them several at once records the same signatures, findings and events as reading
    them one by one, and each PR is fetched once either way."""
    from ttp import prguard, watchers
    from ttp.daemon import Daemon
    urls = {n: f"https://github.com/example-org/example-repo/pull/{n}" for n in MIXED_PRS}
    views: list = []

    def gh(args, cwd):
        time.sleep(0.01)
        if args[0] == "api":
            n = int(next(a for a in args if a.startswith("number=")).split("=")[1])
            pr = {"comments": {"nodes": []}, "reviews": {"nodes": []}, "reviewThreads": {"nodes": []}}
            return {"data": {"repository": {"pullRequest": {**pr, **MIXED_PRS[n][1]}}}}
        views.append(prguard.pr_key(args[2]))
        n = int(args[2].rsplit("/", 1)[1])
        return {"url": urls[n], "state": "OPEN", "title": "t", "headRefOid": HEAD, **MIXED_PRS[n][0]}
    monkeypatch.setattr(watchers, "_gh", gh)
    p = make(env)
    _pr_tasks(p, [urls[1], urls[2], urls[2], urls[3], urls[4]])
    top = p.db.one("SELECT MAX(id) AS n FROM tasks")["n"]
    keys = ("pr_signatures", prguard.UNAPPROVED_KEY, prguard.FINDINGS_KEY, prguard.HEADS_KEY,
            prguard.DRAFT_SEEN_KEY, watchers.PR_TRIED_KEY)
    got = []
    for workers in (1, 8):
        # The same start for both: no state from the other run, nor the fix tasks it queued.
        p.db.x(f"DELETE FROM kv WHERE key IN ({','.join('?' * len(keys))})", keys)
        p.db.x("DELETE FROM events")
        p.db.x("DELETE FROM tasks WHERE id > ?", (top,))
        p.db.set_kv(prguard.PREDATES_KEY, [urls[2], urls[3]])   # out of draft before the guard: not an approval
        views.clear()
        assert watchers.run_builtin(Daemon(p.base), "prs", {"deadline_s": 60, "pr_workers": workers}) \
            .startswith("ok (4 PRs")
        assert sorted(views) == sorted(prguard.pr_key(u) for u in urls.values()), "each PR fetched once"
        new = lambda m: "#new" if int(m.group(1)) > top else m.group(0)   # a fix task each run queued
        events = sorted((e["kind"], e["task"], re.sub(r"#(\d+)", new, e["text"]))
                        for e in p.db.q("SELECT * FROM events WHERE source='pr'"))
        findings = {k: {**r, "owner": r["owner"] and re.sub(r"#(\d+)", new, f"#{r['owner']}")}
                    for k, r in p.db.kv(prguard.FINDINGS_KEY).items()}
        got.append((p.db.kv("pr_signatures"), findings, events))
    assert got[0] == got[1]
    sigs, findings, events = got[0]
    assert [sigs[urls[n]]["draft"] for n in MIXED_PRS] == [True, False, False, True]
    assert [findings[prguard.pr_key(urls[n])]["bot_open"] for n in MIXED_PRS] == [0, 1, 0, 0], "an answered one is not open"
    assert findings[prguard.pr_key(urls[1])]["failing"] == ["build"]
    assert sorted(t for _, t, _ in events) == [1, 2], events


def test_pr_watch_deadline_grows_with_the_pr_count_up_to_its_cap(env, monkeypatch, tmp_path):
    """With no deadline in its payload, a prs run gets the time its PR count needs, each PR counted once,
    never past PR_DEADLINE_AUTO_MAX_S; the schedule's kill timeout follows it. A payload's deadline_s wins,
    and a run whose reads all stall still ends at its deadline."""
    from ttp import watchers
    from ttp.daemon import Daemon, SCHEDULE_GRACE_S
    p = make(env)
    d = Daemon(p.base)
    floor = d.cfg["watchers"]["deadline_s"]
    _pr_tasks(p, ["https://github.com/example-org/example-repo/pull/1"] * 1000)
    assert watchers.bounds(d, {"builtin": "prs"})[0] == floor, "one PR however many tasks name it"
    _pr_tasks(p, [f"https://github.com/example-org/example-repo/pull/{n}" for n in range(2, 1001)])
    assert watchers.bounds(d, {"builtin": "prs"})[0] == watchers.PR_DEADLINE_AUTO_MAX_S
    assert watchers.bounds(d, {}, "prs")[0] == watchers.PR_DEADLINE_AUTO_MAX_S
    assert watchers.bounds(d, {})[0] == floor, "other builtins keep the configured deadline"
    assert watchers.bounds(d, {"builtin": "prs", "deadline_s": 60})[0] == 60
    assert d._schedule_timeout({"name": "prs", "kind": "watcher"}, {}) \
        == watchers.PR_DEADLINE_AUTO_MAX_S + SCHEDULE_GRACE_S
    n = 200 // watchers.PR_WORKERS * watchers.PR_WORKERS
    keep = [f"https://github.com/example-org/example-repo/pull/{k}" for k in range(1, n + 1)]
    p.db.x(f"DELETE FROM tasks WHERE pr_url NOT IN ({','.join('?' * n)})", tuple(keep))
    assert watchers.bounds(d, {"builtin": "prs"})[0] == max(floor, n / watchers.PR_WORKERS * watchers.PR_READ_EST_S)
    # Every read stalls: the run still ends at its (sized, capped) deadline and leaves no gh behind.
    p.db.x("DELETE FROM tasks")
    q, urls, pids = _fake_gh_prs(env, monkeypatch, tmp_path, 3, "1 2 3")
    monkeypatch.setattr(watchers, "PR_DEADLINE_AUTO_MAX_S", 1.5)
    monkeypatch.setattr(watchers, "PR_READ_EST_S", 100.0)
    dq = Daemon(q.base)
    dq.cfg = {**dq.cfg, "watchers": {**dq.cfg["watchers"], "deadline_s": 0.5}}
    assert watchers.bounds(dq, {"builtin": "prs"})[0] == 1.5
    t0 = time.monotonic()
    status = watchers.run_builtin(dq, "prs", {"builtin": "prs", "pr_timeout_s": 30})
    assert time.monotonic() - t0 < 6 and status.startswith("cut short (0 of 3"), status
    assert _all_gone(pids)


def test_pr_watch_leaves_no_gh_process_behind_at_its_deadline(env, monkeypatch, tmp_path):
    """Every PR hangs past the run's deadline: the run ends near it, reports them not checked, and
    each gh's whole group (its children too) is killed."""
    from ttp import watchers
    from ttp.daemon import Daemon
    p, urls, pids = _fake_gh_prs(env, monkeypatch, tmp_path, 2, "1 2")
    t0 = time.monotonic()
    status = watchers.run_builtin(Daemon(p.base), "prs", {"deadline_s": 1.5, "pr_timeout_s": 30})
    assert time.monotonic() - t0 < 6
    assert status.startswith("cut short (0 of 2 PRs read") and "#1 (deadline)" in status, status
    assert len(pids.read_text().split()) == 4 and _all_gone(pids)


def test_pr_watch_runs_gh_through_boundedchild_and_a_hung_one_hits_its_deadline_unread(env, monkeypatch, tmp_path):
    """Each gh call goes through boundedchild, so a hung gh is cut at the run's deadline with its whole
    group: that PR is reported not checked and nothing of it is recorded as read; the other PR is."""
    from ttp import boundedchild, watchers
    from ttp.daemon import Daemon
    p, urls, pids = _fake_gh_prs(env, monkeypatch, tmp_path, 2, "2")
    calls, real = [], boundedchild.run

    def spy(argv, timeout, **kw):
        res = real(argv, timeout, **kw)
        calls.append((argv[3] if argv[:3] == ["gh", "pr", "view"] else argv[1], res))
        return res
    monkeypatch.setattr(boundedchild, "run", spy)
    t0 = time.monotonic()
    status = watchers.run_builtin(Daemon(p.base), "prs", {"deadline_s": 1.5, "gh_timeout_s": 20,
                                                          "pr_timeout_s": 30, "pr_workers": 2})
    assert time.monotonic() - t0 < 6
    assert status.startswith("cut short (1 of 2") and "acme/widgets#2 (deadline)" in status, status
    hung = [res for what, res in calls if what == urls[1]]
    assert len(hung) == 1 and hung[0].timed_out and hung[0].rc is None, calls
    assert any(what == urls[0] and res.rc == 0 for what, res in calls)
    seen = p.db.kv("pr_signatures", {})
    assert urls[0] in seen and urls[1] not in seen, "the hung PR is unchecked, never read"
    assert _all_gone(pids), "the hung gh and its child were killed with their group"


@pytest.mark.parametrize("where", ["payload", "config"])
def test_pr_watch_reports_a_pr_whose_gh_call_timed_out_before_its_per_pr_timeout(env, monkeypatch, tmp_path, where):
    """The default ratio (a gh call's timeout below the PR's): a hung call times out on its own, the
    PR is reported not checked and goes to the back of the rotation; the others still report."""
    from ttp import watchers
    from ttp.daemon import Daemon
    p, urls, pids = _fake_gh_prs(env, monkeypatch, tmp_path, 3, "2")
    d, payload = Daemon(p.base), {"deadline_s": 30, "pr_workers": 2}
    if where == "payload":
        payload.update(gh_timeout_s=1, pr_timeout_s=3)
    else:   # no overrides: the config's watchers defaults, scaled down
        assert d.cfg["watchers"]["gh_timeout_s"] < d.cfg["watchers"]["pr_timeout_s"]
        d.cfg = {**d.cfg, "watchers": {**d.cfg["watchers"], "gh_timeout_s": 1, "pr_timeout_s": 3}}
    t0 = time.monotonic()
    status = watchers.run_builtin(d, "prs", payload)
    assert time.monotonic() - t0 < 6
    assert status.startswith("cut short (2 of 3") and "acme/widgets#2 (1 s gh call timeout)" in status, status
    found = {r["task"] for r in p.db.q("SELECT task FROM events WHERE kind='pr_findings'")}
    ids = {r["pr_url"]: r["id"] for r in p.db.q("SELECT id, pr_url FROM tasks")}
    assert found == {ids[urls[0]], ids[urls[2]]}
    tried = p.db.kv(watchers.PR_TRIED_KEY)
    assert tried[urls[1]] == max(tried.values()), "the timed-out PR waits behind the others"
    assert _all_gone(pids)


def test_a_daemon_stop_mid_pass_kills_pr_watchs_gh_and_starts_no_more(env, monkeypatch, tmp_path):
    """gh runs in sessions of its own on the pass's pool threads: a daemon stop kills them (the hung
    one and its child), the pass starts no further read and the run unwinds recording nothing."""
    from ttp import daemon as dm, schedrun, schedule as sched
    p, urls, pids = _fake_gh_prs(env, monkeypatch, tmp_path, 3, "1 2 3")
    sched.upsert(p.db, "prs", "watcher", "5m", payload={"builtin": "prs", "deadline_s": 60, "gh_timeout_s": 50,
                                                        "pr_timeout_s": 50, "pr_workers": 1})
    _due(p, "prs")
    d = dm.Daemon(p.base)
    d.run_schedules()
    run = d._sched_runs["prs"]
    assert _wait_for(lambda: len(pids.read_text().split()) == 2, 10), "the first gh hung"
    schedrun.stop(p.db, list(d._sched_runs.values()))
    assert run.done.wait(5) and run.stopped, "the run unwound on the stop"
    time.sleep(0.5)
    assert len(pids.read_text().split()) == 2, "a gh started after the stop"
    assert _all_gone(pids), "the stop killed the hung gh and its child"
    assert p.db.one("SELECT last_run FROM schedules WHERE name='prs'")["last_run"] is None
    assert not p.db.kv("pr_signatures"), "the killed reads saved no result"



def test_the_global_cap_alert_clears_by_itself_at_the_day_reset(env, tmp_path, monkeypatch):
    p, other = _global_cap_setup(env, tmp_path)
    from ttp import alerts
    from ttp import globalcap as gcap
    from ttp.daemon import Daemon
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    d = Daemon(p.base)
    d.update_gates()
    other.spend("fake", 12.0, "task:1")
    clock[0] += gcap.LOCAL_CACHE_S + 1      # the other projects' totals are read again
    d.update_gates()
    alert = p.db.one("SELECT ts, text FROM messages WHERE ref='budget:fake'")
    assert alert and "starts again by itself when the budget day resets" in alert["text"], alert
    assert alerts.holds(p.db, "budget:fake", alert["ts"], clock[0])
    clock[0] = gcap.day_bounds(p.config()["budget"], clock[0])[1] + 60
    d.update_gates()
    assert d.gates["fake"].level != "red"
    assert not alerts.holds(p.db, "budget:fake", alert["ts"], clock[0])



NOISY_STDERR = {"401": "MCP server tools: HTTP 401 Unauthorized\n", "400": "MCP server tools: HTTP 400 Bad Request\n"}



REJECTED_LIMIT_EVENT = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}}



NOISE_CASES = {**{f"{prov}-{code}": (prov, OK_RUNS[prov], line) for prov in OK_RUNS for code, line in NOISY_STDERR.items()},
               "claude-rejected-limit-event": ("claude", [REJECTED_LIMIT_EVENT, *OK_RUNS["claude"]], "")}



@pytest.mark.parametrize("case", list(NOISE_CASES))
def test_a_successful_priced_run_with_noisy_stderr_or_a_rejected_limit_event_stays_ok(env, case, tmp_path):
    # Replays the probe cases of a review that found a shared classifier marking these runs auth,
    # limit or a request error: the parse, then the run's status, the breaker and the limit hold.
    provider, lines, stderr = NOISE_CASES[case]
    u = _parsed(provider, tmp_path, lines, stderr)
    assert not (u.auth_failed or u.limited or u.request_error) and (u.cost_usd or u.output_tokens), u
    p = make(env)
    from ttp import alerts
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    run, _ = _finished_run(d, lines, provider=provider, stderr=stderr, rc=0)
    assert run["status"] == "ok", run["status"]
    assert not alerts.breaker(p.db, provider) and p.db.kv(f"limited:{provider}") is None
    assert not p.db.q("SELECT id FROM messages WHERE ref IN (?, ?)", (f"auth:{provider}", f"limit:{provider}"))



@pytest.mark.parametrize("provider", list(REJECTED_RUNS))
def test_a_failed_run_with_a_typed_schema_400_fails_its_task_at_once_without_a_raised_retry(env, provider):
    p = make(env)
    from ttp import alerts, effort
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    run, task = _finished_run(d, REJECTED_RUNS[provider], provider=provider, tier="light")
    assert run["status"] == "request_error", run["status"]
    assert task["status"] == "failed" and task["attempts"] == 1, "a schema rejection was queued for a retry"
    result = json.loads(task["result"])
    assert result["status"] == "request_error" and "400" in result["summary"], result
    assert effort.retry_tier(p.db, task) is None, "a requeued schema rejection would retry at a raised tier"
    assert not alerts.breaker(p.db, provider) and p.db.kv(f"limited:{provider}") is None



def test_a_machine_outage_alert_holds_while_reported_and_clears_with_its_condition(env):
    from ttp import alerts
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp.daemon import Daemon
    alpha = make(env, "alpha")
    mm.add("box-a", tags="device")
    now = time.time()
    ml.report("gamma", "box-a", "down", now=now)
    d = Daemon(alpha.base)
    d._machine_page({"target": ml.USER, "kind": "machine_condition", "key": "machine:box-a:down",
                     "alias": "box-a", "text": "Machine box-a down."}, now)
    late = now + alerts.DAY + 3600
    ml.report("gamma", "box-a", "down", now=late)
    assert alerts.sweep(alpha.db, late) == [], "still reported past a day: it holds"
    ml.report("gamma", "box-a", "down", cleared=True)
    closed = alerts.sweep(alpha.db, late)
    assert [(c["key"], c["cleared_why"]) for c in closed] == [("machine:box-a:down", "condition cleared")]
    assert alpha.db.one("SELECT text FROM messages WHERE kind='resolved' AND ref='machine:box-a:down'")
    # Nobody reporting it for CONDITION_GONE_S ends it too.
    ml.report("gamma", "box-a", "held", now=now)
    d.alert("machine:box-a:held", "Outage: Machine box-a held.", severity="high")
    assert alerts.sweep(alpha.db, now + 60) == []
    assert [c["key"] for c in alerts.sweep(alpha.db, now + ml.CONDITION_GONE_S)] == ["machine:box-a:held"]
    # An overdue change: holds while it is open and overdue, clears once it is closed.
    c = ml.add_change("box-a", "drop-in", "rm it", "gamma", expires="1h", now=now - 7200)
    key = f"machine-change:{c['id']}"
    d.alert(key, "Outage: Machine change overdue.", severity="high")
    assert alerts.sweep(alpha.db, late) == []
    ml.close_change(c["id"], "undone")
    assert [x["key"] for x in alerts.sweep(alpha.db, late)] == [key]



def test_a_los_angeles_project_on_a_utc_box_shows_pdt_or_pst_in_every_surface(env, utc_box):
    """The process runs on UTC, the project's home zone is America/Los_Angeles: ttp status, the web
    app's JSON and its client-side clock, alert and wait texts and the coordinator's digest (what its
    chat replies are written from) all show Pacific time with its abbreviation, never UTC."""
    import shutil
    from ttp import alerts, anchors, waits, web
    from ttp import coordinator as coord
    from ttp.cli import status_text
    from ttp.db import host_line
    p = make(env)
    p.set_config("home_timezone", "America/Los_Angeles")
    db, now = p.db, time.time()
    pac = re.compile(r"\d\d:\d\d P[DS]T")
    db.set_kv("coordinator_failures", 2)
    db.set_kv("coordinator_backoff_until", now + 600)
    db.set_kv("limited:fake", {"until": now + 900, "note": "logged out"})
    tid = db.add_task("measure on a board", "needs a board", kind="work", tier="light", origin="user",
                      not_before=now + 1200)
    # ttp status: the clock in the head line, retry and pause times.
    out = status_text(p)
    for need in ("retry at", "fake paused until", "next try"):
        line = next(ln for ln in out.splitlines() if need in ln)
        assert pac.search(line.split(need, 1)[1]) and " UTC" not in line, line
    # The web app's JSON: the zone it formats in, and the lines the daemon writes for it.
    h = web.health(p, db, now=now)
    assert h["zone"] == "America/Los_Angeles" and web.state_payload(p, db)["project"]["zone"] == h["zone"]
    assert pac.search(h["why_idle"].split("next try", 1)[1]), h["why_idle"]
    assert pac.search(web.gate_detail({"regime": "windows", "numbers": {"window": "5h", "utilization": 4,
                                       "resets_at": now + 3600, "limit": 90}}, now, p))
    db.set_kv(alerts.BREAKER + "fake", {"open": True, "opened": now - 600, "next_check": now + 600})
    line = web.breaker_lines(db, now, p)[0]["line"]
    assert len(pac.findall(line)) == 2 and " UTC" not in line, line
    assert web.at(now + 60, now).endswith("UTC")    # no zone given: UTC, never the box's or a guess
    assert pac.search(host_line([{"ts": now - 60, "lost": []}], p))
    # Alert and wait texts with clock times.
    w = waits.wait_kind({"id": tid, "status": "blocked", "labels": json.dumps([f"waits:time:{now + 900:.0f}"]),
                         "blocked_reason": "", "updated": now}, [], now, p)
    assert re.search(r"P[DS]T$", w["text"]), w
    assert re.search(r"P[DS]T$", anchors.describe("until", str(now + 900), p))
    db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(coord.defer_labels(now + 3600, None)), tid))
    db.x("UPDATE tasks SET not_before=? WHERE id=?", (now + 3600, tid))
    assert pac.search(coord.starts_text(db.task(tid), now, plain=True, where=p))
    # The coordinator's digest, which its chat replies are written from, is stamped in the home zone.
    head = coord.digest(p, {}, [], []).splitlines()[0]
    assert re.match(r"# STATE at \d{4}-\d\d-\d\d \d\d:\d\d P[DS]T ", head), head
    # The web app formats on the client in the zone the daemon sends, not the browser's.
    js = (RUNTIME / "ttp" / "web" / "app.js").read_text()
    assert "toLocaleTimeString" not in js and "toLocaleString" not in js
    assert "ZONE = (st.project && st.project.zone)" in js
    node = shutil.which("node")
    if node:
        block = js[js.index("let ZONE"):js.index("\n", js.index("const at = "))]
        r = subprocess.run([node, "-e", block + "\nZONE = 'America/Los_Angeles';"
                            f"console.log(at({now + 60}), '|', at({now + 3 * 86400}))"],
                           capture_output=True, text=True, timeout=60, env={**os.environ, "TZ": "UTC"})
        assert r.returncode == 0, r.stderr
        assert re.fullmatch(r"\d\d:\d\d P[DS]T \| \w{3},? \d\d:\d\d P[DS]T\n", r.stdout), r.stdout



def test_network_outage_alerts_once_after_the_grace_in_the_home_zone_and_shows_in_the_top_section(env, monkeypatch):
    from ttp import alerts, timefmt
    from ttp.cli import status_text
    p, d, tids, now, check = _net_setup(env, monkeypatch)
    assert check(now) == [], "the grace starts at the first check that sees the outage"
    assert check(now + 600) == [], "within the grace (15 min by default) nothing is said"
    sent = check(now + 900)
    assert len(sent) == 1, "every task that could start waits on an unreachable host: one alert"
    m = sent[0]
    assert m["severity"] == "normal" and m["chat"] is None and m["channel"] == "chat", m
    assert "jump.example" in m["text"] and "2 queued task(s)" in m["text"], m["text"]
    assert "VPN or network sign-in" in m["text"] and "resumes by itself" in m["text"], m["text"]
    assert timefmt.long(now, "America/Los_Angeles") in m["text"] and ("PDT" in m["text"] or "PST" in m["text"])
    assert "ttp " not in m["text"], "no command the harness could run itself"
    assert [x["id"] for x in alerts.needs_you(p.db, now + 900)] == [m["id"]], "a normal alert, yet in the top section"
    assert "needs you (alert" in status_text(p) and "VPN" in status_text(p)
    for at in (now + 1800, now + 3600, now + 6 * 3600, now + 30 * 3600):
        assert len(check(at)) == 1, "no repeat while the outage lasts, not even a daily reminder"
    assert not p.db.q("SELECT id FROM events WHERE status IN ('new','queued')"), "the coordinator is not woken"
    assert p.db.one("SELECT COUNT(*) n FROM alerts WHERE key='network:unreachable'")["n"] == 1



def test_network_outage_clears_once_hosts_answer_with_a_low_feed_line_and_a_brief_reconnect_stays_one(env, monkeypatch):
    from ttp import alerts
    p, d, tids, now, check = _net_setup(env, monkeypatch)
    check(now)
    assert len(check(now + 900)) == 1
    since = p.db.kv("network_outage")["since"]
    # A brief reconnect: the probes pass, then fail again within the debounce (600 s by default).
    assert len(check(now + 960, rc=None)) == 1
    assert alerts.needs_you(p.db, now + 960), "the alert stays through the debounce"
    assert len(check(now + 1300)) == 1 and p.db.kv("network_outage")["since"] == since, "the same outage"
    # Reachability is back for good: the probes passed and the tasks started.
    t0 = now + 1400
    assert len(check(t0, rc=None)) == 1
    assert len(check(t0 + 300, rc=None)) == 1 and alerts.needs_you(p.db, t0 + 300)
    assert len(check(t0 + 660, rc=None)) == 1, "clearing posts no new alert"
    assert not alerts.needs_you(p.db, t0 + 660), "cleared once the hosts answered for the debounce"
    done = p.db.q("SELECT * FROM messages WHERE kind='resolved' AND ref='network:unreachable'")
    assert len(done) == 1 and done[0]["severity"] == "low", "one low feed line, not a notification"
    assert not p.db.kv("network_outage")
    assert [x for x in alerts.feed(p.db, t0 + 660) if x.get("state") == "cleared"]
    # A later outage is a new one: alerted once more after its own grace.
    t1 = t0 + 3600
    assert len(check(t1)) == 1 and len(check(t1 + 600)) == 1
    assert len(check(t1 + 900)) == 2



TUNNEL_PROBE = "ssh -o ConnectTimeout=5 box-a true"



TUNNEL_OUT = "ssh: connect to host 127.0.0.1 port 2222: Connection refused"



def test_network_outage_on_a_tunnel_route_is_a_low_feed_line_raised_once_after_an_hour_then_clears(env, monkeypatch):
    import re
    from ttp import alerts
    from ttp.cli import status_text
    p, d, tids, now, check = _net_setup(env, monkeypatch, probe=TUNNEL_PROBE, out=TUNNEL_OUT,
                                        ssh_config={"box-a": {"hostname": "127.0.0.1", "port": "2222"}})
    check(now)
    sent = check(now + 900)
    assert len(sent) == 1, "one line per outage, after the same grace"
    m = sent[0]
    assert m["severity"] == "low", "a tunnel that reconnects by itself pages nobody at first"
    assert "box-a" in m["text"] and "tunnel from the user's workstation" in m["text"], m["text"]
    assert "reconnects by itself" in m["text"] and "VPN" not in m["text"] and "sign-in" not in m["text"], m["text"]
    assert not re.search(r"127\.\d+\.\d+\.\d+|::1|localhost", m["text"]), m["text"]
    assert "nothing else can start" not in m["text"] and "the project keeps running" in m["text"], m["text"]
    assert not alerts.needs_you(p.db, now + 900) and "needs you" not in status_text(p)
    assert [x["state"] for x in alerts.feed(p.db, now + 900) if x["id"] == m["id"]] == ["status"]
    assert p.db.one("SELECT severity FROM alerts WHERE key='network:unreachable'")["severity"] == "low"
    assert not p.db.unread_for_chat("c", 0), "no chat page for a low line"
    assert len(check(now + 3000)) == 1, "quiet before the threshold (60 min into the outage)"
    sent = check(now + 3600)
    assert len(sent) == 2, "raised once at the threshold"
    r = sent[1]
    assert r["severity"] == "normal" and "box-a" in r["text"] and "(1.0 h so far)" in r["text"], r["text"]
    assert "VPN" not in r["text"] and not re.search(r"127\.\d+\.\d+\.\d+", r["text"]), r["text"]
    assert [x["id"] for x in alerts.needs_you(p.db, now + 3600)] == [r["id"]]
    assert m["id"] not in [x["id"] for x in alerts.feed(p.db, now + 3600)], "the raised line replaces the low one"
    for at in (now + 7200, now + 6 * 3600):
        assert len(check(at)) == 2, "no repeat after the one raise"
    assert p.db.one("SELECT COUNT(*) n FROM alerts WHERE key='network:unreachable'")["n"] == 1, "one issue"
    # The tunnel is back: the outage clears under the same key.
    t0 = now + 6 * 3600 + 300
    check(t0, rc=None)
    assert len(check(t0 + 660, rc=None)) == 2 and not alerts.needs_you(p.db, t0 + 660)
    done = p.db.q("SELECT * FROM messages WHERE kind='resolved' AND ref='network:unreachable'")
    assert len(done) == 1 and done[0]["severity"] == "low"
    assert not p.db.kv("network_outage")



def test_network_outage_on_a_tunnel_route_that_comes_back_before_the_threshold_never_pages(env, monkeypatch):
    from ttp import alerts
    p, d, tids, now, check = _net_setup(env, monkeypatch, probe=TUNNEL_PROBE, out=TUNNEL_OUT,
                                        ssh_config={"box-a": {"hostname": "127.0.0.1", "port": "2222"}})
    check(now)
    assert [m["severity"] for m in check(now + 900)] == ["low"]
    check(now + 1020, rc=None)   # back 17 min into the outage
    assert [m["severity"] for m in check(now + 1700, rc=None)] == ["low"], "never raised"
    assert not p.db.kv("network_outage") and not alerts.needs_you(p.db, now + 1700)
    assert [x["state"] for x in alerts.feed(p.db, now + 1700) if x["kind"] == "alert"] == ["cleared"]
    # 0: page at once.
    d.cfg = {**d.cfg, "waiting": {**(d.cfg.get("waiting") or {}), "tunnel_outage_page_after_s": 0}}
    t1 = now + 7200
    check(t1)
    sent = check(t1 + 900)
    assert [m["severity"] for m in sent] == ["low", "normal"] and "tunnel" in sent[1]["text"]
    assert len(check(t1 + 4 * 3600)) == 2



def test_network_outage_sees_real_start_when_probe_verdicts_and_clears_once_they_pass(env, monkeypatch):
    """No verdict written by hand: two tasks deferred on a start_when probe that, like ssh to a host
    out of reach, prints ssh's error and exits 255. The daemon's own probe cycles feed the check."""
    from ttp import alerts
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    from ttp.daemon import Daemon
    from ttp.db import deferral
    p = make(env)
    _no_events(p)
    _ssh_255(p)   # the check that the named host is out of reach gets ssh's own 255 too
    stub = p.root / "reach.sh"
    stub.write_text("echo 'ssh: connect to host gw.example port 22: Connection timed out' >&2; exit 255\n")
    probe = f"sh {stub}"
    from ttp import netwait
    monkeypatch.setattr(netwait, "_ssh_config", lambda alias: {})
    d = Daemon(p.base)
    for i in range(2):
        assert coord.apply(p, [{"type": "task_add", "title": f"device job {i}", "spec": "s",
                                "start_when": probe}]) == []
    tids = [t["id"] for t in p.db.q("SELECT id FROM tasks WHERE status='queued' ORDER BY id")]
    clock = {"now": time.time()}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    start = clock["now"]

    def cycle(at):
        clock["now"] = at
        d.probe_waiting()   # starts the probe once its last run is PROBE_EVERY_S old
        for proc, _, _ in list(d._probes.values()):
            proc.wait(timeout=30)
        d.probe_waiting()   # reads its verdict
        d._net_checked = 0.0
        d.check_unreachable()
        alerts.sweep(p.db, at)
        return p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='network:unreachable' ORDER BY id")

    step = dmod.PROBE_EVERY_S + 20
    at = start
    while at < start + 900:
        assert cycle(at) == [], "within the grace nothing is said"
        at += step
    sent = cycle(at)
    assert len(sent) == 1 and "gw.example" in sent[0]["text"] and "2 queued task(s)" in sent[0]["text"], sent
    assert all(p.db.task(t)["status"] == "queued" and deferral(p.db.task(t)).get("when") for t in tids)
    # The network is back: the probe passes and the tasks are free to start.
    stub.write_text("exit 0\n")
    at += step
    assert len(cycle(at)) == 1 and alerts.needs_you(p.db, at), "the alert holds through the debounce"
    assert all(not deferral(p.db.task(t)).get("when") for t in tids), "start_when passed"
    assert not d._start_rc, "a passed start_when leaves no verdict behind"
    for t in tids:
        p.db.update_task(t, status="running")
        p.db.x("INSERT INTO runs(task,role,started,status) VALUES(?,'worker',?,'running')", (t, at))
    at += 660
    assert len(cycle(at)) == 1, "clearing posts no new alert"
    assert not alerts.needs_you(p.db, at) and not p.db.kv("network_outage"), "the outage ended after the debounce"
    assert len(p.db.q("SELECT id FROM messages WHERE kind='resolved' AND ref='network:unreachable'")) == 1



@pytest.mark.parametrize("up", [True, False])
def test_a_probe_255_is_a_network_outage_only_when_its_host_does_not_answer(env, monkeypatch, up):
    """A start_when that exits 255 through its own `|| exit 255` after its remote command failed, while
    the host answers ssh, is a broken probe against its task, never a network outage. With the host
    really out of reach (ssh stubbed to 255) the outage is alerted, naming the alias."""
    from ttp import alerts, netwait
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    p = make(env)
    _no_events(p)
    _ssh_255(p, up=up)
    monkeypatch.setattr(netwait, "_ssh_config", lambda alias: {})
    probe = "m=$(ssh -o BatchMode=yes box-a stat /x) || exit 255"
    d = dmod.Daemon(p.base)
    assert coord.apply(p, [{"type": "task_add", "title": "device job", "spec": "s", "start_when": probe}]) == []
    tid = _added(p, "device job")
    clock = {"now": time.time()}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    at = start = clock["now"]
    while at < start + 1200:
        clock["now"] = at
        d.probe_waiting()
        for proc, _, _ in list(d._probes.values()):
            proc.wait(timeout=30)
        d.probe_waiting()
        d._net_checked = 0.0
        d.check_unreachable()
        alerts.sweep(p.db, at)
        at += dmod.PROBE_EVERY_S + 20
    sent = p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='network:unreachable'")
    broken = _events(p, tid, "deferral_probe_broken")
    if up:
        assert sent == [] and not p.db.kv("network_outage"), "the host answers: no network outage"
        assert len(broken) == 1 and f"#{tid}" in broken[0]["text"] and "ssh reaches box-a" in broken[0]["text"], broken
        assert "task %d probe is broken" % tid in (p.logs / "daemon.log").read_text()
    else:
        assert len(sent) == 1 and "box-a" in sent[0]["text"], sent
        assert p.db.kv("network_outage")["hosts"] and not broken
    assert p.db.task(tid)["status"] == "queued"



def test_failover_moves_off_a_logged_out_provider_only_to_a_usable_target(env):
    p, d, now = _failover_setup(env, util=10.0)
    tid = _codex_task(p)
    d.update_gates()
    d.failover_pinned()
    assert p.db.task(tid)["provider"] == "codex", "moved off a green provider"
    d.open_breaker("codex", "401")
    d.open_breaker("fake", "401")
    d._failover_checked = 0
    d.failover_pinned()
    assert p.db.task(tid)["provider"] == "codex", "moved to a logged-out target"
    from ttp import alerts
    p.db.set_kv(alerts.BREAKER + "fake", {})
    d._failover_checked = 0
    d.failover_pinned()
    t = p.db.task(tid)
    assert (t["provider"], t["model"], t["effort"]) == ("fake", None, "xhigh")
    assert "logged out" in t["spec"] and "no known end" in t["spec"]



def test_a_provider_limit_needs_the_user_only_when_the_account_is_out_of_funds(env, capsys):
    """A usage or rate limit lifts by itself: a low status line in the feed that clears itself, never
    "needs you", a chat alert or the desktop. An account out of credits only the user can fix: high,
    on top, even after a status line for the same provider."""
    p = make(env)
    from types import SimpleNamespace
    from ttp import alerts
    from ttp.cli import _listen_loop, status_text
    from ttp.daemon import Daemon
    from ttp.web import attention, fix_for, state_payload
    d = Daemon(p.base)
    now = time.time()
    p.db.set_kv("limited:fake", {"until": now + 3600, "note": "usage limit reached"})
    d.alert("limit:fake", "fake hit a limit: usage limit reached.", "low")
    assert attention(p.db, now) == [] and "needs you" not in status_text(p)
    feed = [m for m in state_payload(p, p.db)["feed"] if m["text"].startswith("fake hit a limit")]
    assert [m["state"] for m in feed] == ["status"], feed
    assert fix_for("fake", "usage limit reached") == "resumes by itself when the limit resets"
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (now, "t", now))
    _listen_loop(p, p.db, SimpleNamespace(chat="c1", timeout=3, once=True, ack=None), 0, "low")
    assert "fake hit a limit" in capsys.readouterr().out, "a live status line reaches a chat that asked for low"
    # The same provider then refuses for funds: news at once, on top, though its status episode is open.
    d.alert("limit:fake", "fake refused work: out of credits. The account needs more credits.", "high")
    assert [m["text"] for m in attention(p.db, now)] == [
        "fake refused work: out of credits. The account needs more credits."]
    assert fix_for("fake", "out of credits") == "the account needs more credits or a higher spend limit"
    assert len(p.db.q("SELECT id FROM alerts WHERE key='limit:fake'")) == 1
    p.db.set_kv("limited:fake", {"until": now - 1, "note": "out of credits"})
    d.sweep_alerts()
    assert attention(p.db, time.time()) == []
    told = p.db.q("SELECT severity FROM messages WHERE kind='resolved' AND ref='limit:fake'")
    assert told == [{"severity": "normal"}], told   # a cleared high alert is told as before
    # A status line that cleared before a chat read it is not delivered late.
    p.db.set_kv("limited:other", {"until": time.time() - 1, "note": "rate limit"})
    p.db.post("out", "other hit a limit: rate limit.", chat=None, kind="alert", severity="low", ref="limit:other")
    _listen_loop(p, p.db, SimpleNamespace(chat="c1", timeout=3, once=True, ack=None), 0, "low")
    assert "other hit a limit" not in capsys.readouterr().out
    assert alerts.cleared(p.db, {"kind": "alert", "severity": "low", "ref": "limit:other",
                                 "ts": time.time()}, time.time())



@pytest.mark.parametrize('caught_up', [False, '0.5.0', '0.5.1'])
def test_after_push_deploy_followup_after_takeover(env, tmp_path, caught_up):
    from ttp import alerts, deploy, overview, release
    p = make(env)
    p.set_config('home_timezone', 'America/Los_Angeles')
    _deploy_lib('0.5.0')
    base, db = _deploy_project(tmp_path, 'alpha', '0.4.9')
    db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    now = time.time()
    report = overview.deployed(names=['alpha'])
    report['projects'][0]['takeover_at'] = now + 1
    assert deploy.record(p, report, now)['rc'] == 0
    due = now + 1 + deploy.GRACE_S
    pages = []
    def page(key, text, severity, **_):
        pages.append((key, text, severity))
        p.db.post('out', text, kind='alert', ref=key, severity=severity)
    deploy.tick(p, page, due - 1)
    assert not pages
    if caught_up:
        hb = base / 'state' / 'heartbeat'
        data = json.loads(hb.read_text())
        data['receipt'].update(version=caught_up, commit=DEPLOY_C if caught_up == '0.5.0' else '123456789abcd')
        hb.write_text(json.dumps(data))
        os.utime(base / 'state' / 'tick_done', (due - 1, due - 1))
    deploy.tick(p, page, due + deploy.PROBE_S)
    deploy.tick(p, page, due + deploy.PROBE_S * 2)
    rec = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    if caught_up:
        assert rec['closed'] and not pages
    else:
        assert len(pages) == 1 and pages[0][0] == 'deploy-behind:alpha' and pages[0][2] == 'normal'
        assert 'missed takeover' in pages[0][1] and 'PDT' in pages[0][1]
        assert alerts.holds(p.db, pages[0][0], now, due)
        hb = base / 'state' / 'heartbeat'
        data = json.loads(hb.read_text())
        data['receipt']['version'] = '0.5.0'
        hb.write_text(json.dumps(data))
        os.utime(base / 'state' / 'tick_done', (due, due))
        assert [e['key'] for e in alerts.sweep(p.db, now=due)] == ['deploy-behind:alpha']
        assert not alerts.active(p.db, pages[0][0], now, due)



@pytest.mark.parametrize('caught_up', [True, False])
@pytest.mark.parametrize('merge_task', [False, True])
def test_after_push_deploy_restart_pending_is_owned_and_rechecked_after_its_wait(env, tmp_path, caught_up, merge_task):
    """Files at the release, daemon still on the old one: its own daemon restarts onto them, so it is
    pending (one low note) until delivery.deploy_restart_wait_s, then alerts once if still behind."""
    from ttp import alerts, deploy, overview, release
    p = make(env)
    p.set_config('home_timezone', 'America/Los_Angeles')
    p.set_config('delivery.deploy_restart_wait_s', 600)
    _deploy_lib('0.5.0')
    base, db = _deploy_project(tmp_path, 'alpha', '0.5.0', running='0.4.9')
    if merge_task:   # a harness task still open from the merge: the restart is still the daemon's step
        db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    now = time.time()
    result = deploy.record(p, overview.deployed(names=['alpha']), now)
    assert result['rc'] == 0 and result['projects'][0]['outcome'] == 'pending'
    rec = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    assert rec['owner'] == "this project's daemon" and rec['due'] == now + 600
    notes = p.db.q("SELECT severity,text FROM messages WHERE direction='out'")
    assert [n['severity'] for n in notes] == ['low'] and 'alpha: restart onto the new files pending' in notes[0]['text']
    assert not p.db.q('SELECT * FROM alerts')
    pages, page = _deploy_pager(p)
    deploy.tick(p, page, now + 599)
    assert not pages
    if caught_up:
        hb = base / 'state' / 'heartbeat'
        data = json.loads(hb.read_text())
        data['receipt']['version'] = '0.5.0'
        hb.write_text(json.dumps(data))
        os.utime(base / 'state' / 'tick_done', (now + 590, now + 590))
    for t in (now + 601, now + 601 + deploy.PROBE_S, now + 601 + deploy.PROBE_S * 2):
        deploy.tick(p, page, t)
    rec = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    if caught_up:
        assert rec['closed'] and not pages
    else:
        assert len(pages) == 1 and pages[0][0] == 'deploy-behind:alpha' and pages[0][2] == 'normal'
        assert 'still behind at its re-check' in pages[0][1] and 'PDT' in pages[0][1]
        assert alerts.holds(p.db, 'deploy-behind:alpha', now, now + 700)



@pytest.mark.parametrize('fault', ['stopped', 'upgrade_error'])
def test_after_push_deploy_fault_while_pending_alerts_at_next_probe(env, tmp_path, fault):
    from ttp import alerts, deploy, overview, release
    from ttp.project import Project
    p = make(env)
    _deploy_lib('0.5.0')
    _, db = _deploy_project(tmp_path, 'alpha', '0.4.9')
    db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    now = time.time()
    assert deploy.record(p, overview.deployed(names=['alpha']), now)['rc'] == 0
    due = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']['due']
    pages, page = _deploy_pager(p)
    deploy.tick(p, page, now + 1)
    assert not pages
    if fault == 'stopped':
        db.set_kv('daemon', {'pid': 0, 'host': 'testhost'})
    else:
        db.set_kv(release.KV_AUTO, {'key': f'0.5.0 {DEPLOY_C[:12]}', 'outcome': 'failed',
                                    'why': 'runtime import failed'})
    t = now + 1 + deploy.PROBE_S
    p = Project(p.base)                          # the reporting daemon restarted meanwhile
    pages, page = _deploy_pager(p, pages)
    deploy.tick(p, page, t)
    assert t < due and len(pages) == 1 and pages[0][0] == 'deploy-behind:alpha'
    assert ('daemon stopped' if fault == 'stopped' else 'runtime import failed') in pages[0][1]
    p = Project(p.base)
    pages, page = _deploy_pager(p, pages)
    for i in (2, 3):
        deploy.tick(p, page, now + 1 + deploy.PROBE_S * i)
    assert len(pages) == 1, 'one alert per condition, also across restarts'
    assert alerts.holds(p.db, 'deploy-behind:alpha', now, t)
    if fault == 'stopped':
        db.set_kv('daemon', {'pid': os.getpid(), 'host': 'testhost'})
    else:
        db.set_kv(release.KV_AUTO, {})
    t = now + 1 + deploy.PROBE_S * 4
    deploy.tick(p, page, t)
    assert [e['key'] for e in alerts.sweep(p.db, now=t)] == ['deploy-behind:alpha']
    rec = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    assert rec['due'] == due and rec['outcome'] == 'pending' and len(rec['history']) == 1
    deploy.tick(p, page, due + 1)
    assert len(pages) == 2 and 'missed takeover' in pages[1][1], 'the merge deadline is still watched'



def test_a_daemon_that_never_restarts_onto_new_releases_is_one_alert(env, tmp_path):
    """Files reach each release but the daemon keeps running the old code: each release set a fresh restart
    deadline, the failed check was superseded, its alert cleared and a new one opened after the wait. The
    earlier restart deadline is kept, so it is one alert, updated in place, never cleared in between."""
    from ttp import alerts, deploy, overview
    p = make(env)
    base, _ = _deploy_project(tmp_path, 'alpha', '0.5.0', running='0.4.9')
    pages, page = _deploy_pager(p)
    now = time.time()
    versions = [f'0.5.{i}' for i in range(5)]
    for i, v in enumerate(versions):
        t = now + 3600 * i
        (base / 'harness' / 'runtime' / 'ttp' / '__init__.py').write_text(f'__version__ = "{v}"\n')
        _deploy_lib(v)
        deploy.record(p, overview.deployed(names=['alpha']), t)
        alerts.sweep(p.db, now=t)
        deploy.tick(p, page, t + deploy.RESTART_WAIT_S + 400)
        alerts.sweep(p.db, now=t + deploy.RESTART_WAIT_S + 400)
    rows = p.db.q("SELECT key,text,cleared FROM alerts WHERE key='deploy-behind:alpha'")
    assert len(rows) == 1 and rows[0]['cleared'] is None, rows
    assert f'tt-project {versions[-1]} ' in rows[0]['text']
    assert not p.db.q("SELECT text FROM messages WHERE text LIKE '%Cleared%'")
    assert len(pages) == 1



def test_after_push_deploy_new_commit_of_same_version_retargets_the_check(env, tmp_path):
    from ttp import alerts, deploy, overview, release
    from ttp.project import Project
    p = make(env)
    _deploy_lib('0.5.0')
    base, db = _deploy_project(tmp_path, 'alpha', '0.4.9')
    db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    now = time.time()
    assert deploy.record(p, overview.deployed('0.5.0', DEPLOY_C, names=['alpha']), now)['rc'] == 0
    pages, page = _deploy_pager(p)
    db.set_kv('daemon', {'pid': 0, 'host': 'testhost'})
    deploy.tick(p, page, now + 1)
    assert len(pages) == 1
    db.set_kv('daemon', {'pid': os.getpid(), 'host': 'testhost'})
    newer = '123456789abcdef01234'
    again = Project(p.base)
    assert deploy.record(again, overview.deployed('0.5.0', newer, names=['alpha']), now + 10)['rc'] == 0
    rec = again.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    assert rec['commit'] == newer and rec['outcome'] == 'pending' and len(rec['history']) == 1
    assert not alerts.holds(p.db, 'deploy:alpha:0.5.0', now, now + 10)
    deploy.record(again, overview.deployed('0.5.0', newer, names=['alpha']), now + 20)
    assert again.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0'] == rec, 'a true retry keeps its deadline'
    _deploy_catch_up(base, db, '0.5.0', newer, now + 30)
    deploy.tick(Project(p.base), page, now + 30)
    rec = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    assert rec['closed'] and len(pages) == 1
    assert not alerts.holds(p.db, 'deploy:alpha:0.5.0', now, rec['due'] + 1)
    deploy.tick(p, page, rec['due'] + 1)
    assert len(pages) == 1, 'no missed-takeover alert for a release that arrived'



def test_after_push_deploy_recovered_daemon_returns_to_owned_pending(env, tmp_path):
    from ttp import alerts, deploy, overview, release
    from ttp.project import Project
    p = make(env)
    _deploy_lib('0.5.0')
    _, db = _deploy_project(tmp_path, 'alpha', '0.4.9')
    db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    db.set_kv('daemon', {'pid': 0, 'host': 'testhost'})
    now = time.time()
    assert deploy.record(p, overview.deployed(names=['alpha']), now)['rc'] == 1
    due = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']['due']
    assert due > now + 3600
    db.set_kv('daemon', {'pid': os.getpid(), 'host': 'testhost'})
    again = Project(p.base)
    for t in (now + 60, now + 120):         # the coordinator's retry, after a restart
        result = deploy.record(again, overview.deployed(names=['alpha']), t)
        assert result['rc'] == 0 and result['projects'][0]['outcome'] == 'pending'
    assert len(p.db.q("SELECT * FROM messages WHERE kind='alert'")) == 1
    assert [e['key'] for e in alerts.sweep(p.db, now=now + 120)] == ['deploy-behind:alpha']
    rec = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    assert rec['due'] == due and len(rec['history']) == 1 and not rec.get('alerted')
    pages, page = _deploy_pager(again)
    deploy.tick(again, page, now + 180)
    assert not pages
    deploy.tick(again, page, due + 1)
    assert len(pages) == 1 and 'missed takeover' in pages[0][1]



def test_failover_review_lines_say_when_a_logout_ended(env):
    """A logged-out cause whose breaker closed since shows its end and the task's status; an open one keeps 'no known end'."""
    from ttp import alerts, failover
    p = make(env)
    now = time.time()
    tid = p.db.add_task("long job", "Run it.", kind="work", tier="standard", provider="claude")
    line = (f"#{tid} long job: left running on claude, which cannot start new work (logged out) with no known end; "
            "a later run of it moves unless its pin is required")
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
           (now - 5 * 3600, "daemon", failover.KIND, "low", line, "handled", tid))
    p.db.set_kv(alerts.BREAKER + "claude", {"open": True, "opened": now - 6 * 3600})
    assert failover.review_lines(p.db, now, where="America/Los_Angeles") == [line]
    p.db.set_kv(alerts.BREAKER + "claude", {"open": False, "opened": now - 6 * 3600, "closed": now - 3.5 * 3600})
    p.db.update_task(tid, status="done")
    got = failover.review_lines(p.db, now, where="America/Los_Angeles")[0]
    assert "no known end" not in got and "(ended " in got and "lasted 2.5 h)" in got, got
    assert got.endswith(f"#{tid} is now done") and (" PDT, " in got or " PST, " in got), got


def test_headline_label_prefix_and_word_clip_in_chain_audit():
    from ttp import headline
    assert headline.restates("triage: box-a hold", "box-a hold now cleared", False)
    assert headline.join("triage: box-a hold", "box-a hold", 80) == "box-a hold"
    out = headline.clip("alpha beta gamma delta " * 5, 50)
    assert out.endswith("…") and len(out) <= 50 and out[:-1].split()[-1] in {"alpha", "beta", "gamma", "delta"}


def test_a_worker_high_notify_reaches_the_coordinator_while_its_run_lives(env, monkeypatch, capsys):
    """`ttp notify --severity high`: synced to the run's escalation file, which the daemon reads each
    tick while the run lives. Each distinct text becomes one high coordinator observation, never a
    user alert; repeats and rereads add nothing, past ESCALATIONS_PER_RUN texts are counted in the
    hand-off, and a run killed before any tick still delivers its lines when it is finished."""
    from ttp import cli, daemon
    from ttp.daemon import Daemon
    from ttp.db import load_result
    from ttp.providers.base import RunUsage as Usage

    def notify(*args) -> int:
        try:
            cli.main(["notify", *args])
        except SystemExit as e:
            return int(e.code or 0)
        return 0

    def escalations(tid: int) -> list:
        return p.db.q("SELECT text, severity, status, kind FROM events WHERE task=? AND fingerprint LIKE 'escalate:%' "
                      "ORDER BY id", (tid,))
    p = make(env)
    d = Daemon(p.base)
    tid = p.db.add_task("probe", "spec", kind="work", tier="standard", origin="coordinator")
    p.db.update_task(tid, status="running")
    run_dir = _note_run(env, monkeypatch, p, task=str(tid))
    rid = p.db.x("INSERT INTO runs(task,role,provider,status,started,dir) VALUES(?,'worker','claude','running',?,?)",
                 (tid, time.time(), str(run_dir)))
    assert notify("--severity", "critical", "x") == 2, "critical is refused"
    assert not (run_dir / daemon.ESCALATE_FILE).exists()
    assert notify("--severity", "high", "device lost mid-run:\n  results at risk") == 0
    assert "coordinator" in capsys.readouterr().out
    assert "escalate: device lost" in (run_dir / "progress.md").read_text()
    assert not (run_dir / daemon.NOTIFY_FILE).exists(), "high never takes the user's notify path"
    d.relay_escalations()
    got = escalations(tid)
    assert len(got) == 1 and got[0]["severity"] == "high" and got[0]["status"] == "queued", got
    assert got[0]["kind"] == "observation" and "device lost mid-run: results at risk" in got[0]["text"]
    # The same text again, an unchanged file, and a reread from scratch (a daemon restart) add nothing.
    assert notify("--severity", "high", "device lost mid-run: results at risk") == 0
    d.relay_escalations()
    d.relay_escalations()
    d._escalated.clear()
    d.relay_escalations()
    assert len(escalations(tid)) == 1
    # The cap: two distinct texts per run; the rest are counted, never queued.
    for i in range(3):
        assert notify("--severity", "high", f"another finding {i}") == 0
    d.relay_escalations()
    assert len(escalations(tid)) == daemon.ESCALATIONS_PER_RUN == 2
    assert not p.db.one("SELECT id FROM messages WHERE kind='alert'"), "the user is never paged directly"
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "probed"}))
    d._finish_worker(dict(p.db.one("SELECT * FROM runs WHERE id=?", (rid,))), Usage(cost_usd=0.1), "ok", run_dir)
    assert len(escalations(tid)) == 2
    assert load_result(p.db.task(tid)["result"])["escalations_dropped"] == 2

    # A run killed before the daemon ever read its file: its run end (here after a reboot) delivers it.
    t2 = p.db.add_task("probe two", "spec", kind="work", tier="standard", origin="coordinator")
    p.db.update_task(t2, status="running")
    run2 = _note_run(env, monkeypatch, p, task=str(t2))
    r2 = p.db.x("INSERT INTO runs(task,role,provider,status,started,dir) VALUES(?,'worker','claude','running',?,?)",
                (t2, time.time(), str(run2)))
    assert notify("--severity", "high", "found corrupt output") == 0
    (run2 / "result.json").write_text(json.dumps({"status": "failed", "summary": "cut short"}))
    d._finish_worker(dict(p.db.one("SELECT * FROM runs WHERE id=?", (r2,))), Usage(cost_usd=0.1), "ok", run2,
                     rebooted=True)
    assert [e["text"].endswith("found corrupt output") for e in escalations(t2)] == [True]
    assert "escalations_dropped" not in load_result(p.db.task(t2)["result"])


def test_worker_low_and_normal_notify_are_unchanged_by_the_escalation_path(env, monkeypatch, capsys):
    """Low and normal stay user alerts posted when the run ends; no escalation file, no event."""
    from ttp import cli, daemon
    p = make(env)
    tid = p.db.add_task("probe", "spec", kind="work", tier="standard", origin="coordinator")
    run_dir = _note_run(env, monkeypatch, p, task=str(tid))
    for sev in ("low", "normal"):
        cli.main(["notify", "--severity", sev, f"{sev} news"])
    assert "sent when this run ends" in capsys.readouterr().out
    assert not (run_dir / daemon.ESCALATE_FILE).exists()
    assert daemon.relay_worker_escalations(p, p.db.task(tid), 9, run_dir) == (0, 0)
    assert daemon.relay_worker_notifies(p, p.db.task(tid), run_dir) == 2
    assert not p.db.one("SELECT id FROM events WHERE fingerprint LIKE 'escalate:%'")


def _blocked_on(env, p, question, *, target_status="queued", extra="", target=None):
    """A running task hands off `blocked` with `question`; a second task (`target`, made in
    `target_status` unless given) is the one it names as {t}; {me} is the task itself."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    if not (p.config().get("delivery") or {}).get("push_branch"):
        p.set_config("delivery.push_branch", "origin/proj")
    if target is None:
        target = p.db.add_task("the change it needs", "s", origin="coordinator")
        p.db.update_task(target, status=target_status)
    tid = p.db.add_task("needs it", "s", origin="coordinator")
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(
        {"status": "blocked", "summary": "stuck", "question": question.format(t=target, u=target + 50, me=tid)}))
    Daemon(p.base)._finish_worker({"task": tid, "note": None}, Usage(cost_usd=1.0), "ok", run_dir)
    return tid, target


def test_a_hand_off_blocked_only_on_one_task_landing_becomes_a_landed_wait(env):
    p = make(env)
    tid, target = _blocked_on(env, p, "Re-queue me after #{t} lands on the push branch")
    row = p.db.task(tid)
    result = json.loads(row["result"])
    assert row["status"] == "queued" and result["status"] == "waiting"
    assert result["retry_when"] == f"landed:#{target}" and "Re-queue me after" in result["summary"]
    ev = p.db.one("SELECT * FROM events WHERE kind='task_wait_landed' AND task=?", (tid,))
    assert ev["severity"] == "low" and ev["status"] == "handled"
    assert not p.db.q("SELECT * FROM events WHERE task=? AND kind='task_blocked'", (tid,))


def test_a_branch_name_counts_as_the_named_task(env):
    p = make(env)
    tid, target = _blocked_on(env, p, "waiting for ttp/t{t}-fix to be merged")
    assert json.loads(p.db.task(tid)["result"])["retry_when"] == f"landed:#{target}"


@pytest.mark.parametrize("question", ["Is #{t} really landed, or #{u}?", "Need a decision about #{t}",
                                      "Need credentials for the box", "Is the thing landed?",
                                      "Need ssh credentials for the box; also #{t} must land first",
                                      "Blocked until the user merges PR #{t}",
                                      "Ask #{t} must be answered before this can be pushed",
                                      "Should I wait for #{t} to land or revert it?",
                                      "owner/repo#{t} must be merged upstream",
                                      "Which approach do you prefer? Also #{t} must land first.",
                                      "Can you re-queue me once #{t} lands?",
                                      "Re-queue me after #{t} lands, then #{u} too",
                                      "Re-queue me after #{me} lands",
                                      "Re-queue me after #{u} lands"])
def test_a_blocked_hand_off_that_is_not_one_task_landing_stays_blocked(env, question):
    p = make(env)
    tid, _ = _blocked_on(env, p, question)
    assert p.db.task(tid)["status"] == "blocked"
    assert json.loads(p.db.task(tid)["result"])["status"] == "blocked"


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_a_blocked_hand_off_on_a_failed_or_cancelled_task_stays_blocked(env, state):
    p = make(env)
    tid, _ = _blocked_on(env, p, "Re-queue me after #{t} lands", target_status=state)
    assert p.db.task(tid)["status"] == "blocked"


def _no_network(monkeypatch):
    """Every git fetch, ls-remote, pull or push started while handling the result, recorded."""
    calls, real = [], subprocess.run

    def run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and {"fetch", "ls-remote", "pull", "push"} & {str(c) for c in cmd}:
            calls.append(cmd)
        return real(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_a_blocked_hand_off_on_an_already_landed_task_stays_blocked(env, monkeypatch):
    """Its landing is on the local push-branch ref: a wait would pass at once, so no wait is made."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    target, path, branch = _code_task(p, "feature")
    _commit(path, "feature.txt", "feature\n")
    _git_out(path, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    calls = _no_network(monkeypatch)
    tid, _ = _blocked_on(env, p, "Re-queue me after #{t} lands", target=target)
    assert p.db.task(tid)["status"] == "blocked" and not calls, calls


def test_a_blocked_hand_off_stays_blocked_when_the_push_branch_is_unknown(env, monkeypatch):
    """No push branch: its `landed:#<id>` probe could never pass, so no wait is made."""
    from ttp import landed
    monkeypatch.setattr(landed, "_push_ref", lambda p, repo: (_ for _ in ()).throw(ValueError("no target")))
    p = make(env)
    tid, _ = _blocked_on(env, p, "Re-queue me after #{t} lands")
    assert p.db.task(tid)["status"] == "blocked"


def test_the_landed_check_on_a_blocked_hand_off_reads_local_refs_only(env, monkeypatch):
    """A done target not on the local push-branch ref converts with no fetch: the wait's probe fetches."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    target, path, branch = _code_task(p, "feature")
    _commit(path, "feature.txt", "feature\n")
    calls = _no_network(monkeypatch)
    tid, _ = _blocked_on(env, p, "Re-queue me after #{t} lands", target=target)
    assert json.loads(p.db.task(tid)["result"])["retry_when"] == f"landed:#{target}"
    assert not calls, calls


def test_outage_facts_state_the_start_duration_actor_task_and_what_was_tried(env):
    """outagemsg.facts: one wording for every outage message, for the owner acting, the owner paused
    with the fallback holding a tracked task, and nobody acting."""
    from ttp import outagemsg
    t0 = 1_790_000_000.0
    owner = outagemsg.facts(t0, t0 + 4 * 3600, outagemsg.actor("alpha", "owner"), "UTC",
                            tried=["recovery page 1 to alpha"], next_step="alpha recovers it")
    assert owner.startswith(f"Since {time.strftime('%Y-%m-%d %H:%M', time.gmtime(t0))} UTC (4.0 h so far).")
    assert "Acting now: alpha (owner)." in owner and "Tracked task: none." in owner
    assert "Tried: recovery page 1 to alpha." in owner and owner.endswith("Next: alpha recovers it.")
    fallback = outagemsg.facts(t0, t0 + 1500, outagemsg.actor("beta", "fallback", "alpha cannot act: it is paused"),
                               "UTC", task="#42", due=t0 + 3 * 3600)
    assert "(25 min so far)" in fallback and "Acting now: beta (fallback; alpha cannot act: it is paused)." in fallback
    assert f"Tracked task: #42, due {time.strftime('%H:%M', time.gmtime(t0 + 3 * 3600))} UTC." in fallback
    assert "Tried: nothing yet." in fallback
    for acting in (outagemsg.actor(None), outagemsg.actor("the user"), ""):
        nobody = outagemsg.facts(t0, t0 + 2 * 86400, acting, "UTC", task="not a task", due="soon")
        assert "(2.0 d so far)" in nobody and "Acting now: nobody: escalating" in nobody, nobody
        assert "Tracked task: none." in nobody, "a task id or due time that is not one is left out"


def test_a_bare_nothing_was_changed_outage_message_never_goes_out_without_an_actor(env):
    from ttp import outagemsg
    from ttp.daemon import Daemon
    bare = ("Device held for 4 h on box-a. Nothing was changed: the owner is paused and the fallback "
            "was sent a note.")
    assert outagemsg.bare(bare) and outagemsg.bare("box-a down; sent the fallback a note.")
    assert not outagemsg.bare(bare + " Acting now: beta (fallback).")
    assert outagemsg.ensure_actor(bare).endswith("Acting now: nobody: escalating to the user.")
    p = make(env)
    d = Daemon(p.base)
    for key in ("outage:box-a", "machine:box-a:held", "heal:dev", "heal-hold:dev", "network:unreachable"):
        d.alert(key, bare, "high")
        got = p.db.one("SELECT text FROM messages WHERE kind='alert' AND ref=?", (key,))["text"]
        assert got.startswith(bare) and "Acting now: nobody: escalating" in got, (key, got)
    d.alert("box-a-gone", bare, "high", outage=True)
    assert "Acting now:" in p.db.one("SELECT text FROM messages WHERE kind='alert' AND ref='box-a-gone'")["text"]


def test_an_alert_off_the_outage_paths_is_never_tagged_with_an_actor_by_its_words(env):
    """Only outage paths opt in (a key prefix or outage=True): 'held', 'down', 'owner', 'recovery' in a
    task title or a push message do not make an alert about an outage."""
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    texts = {
        "root-checkout:7": ("The project root's checkout (/repo) was left with uncommitted changes to 1 tracked "
                            "path it did not have before: a.py after run 3 of task #7 (Fix held device recovery "
                            "when the owner is down), which worked in the project root. Nothing was changed; this "
                            "clears once the checkout is back and those paths are clean."),
        "push-held": "Push held: nothing was changed.",
        "hold-stale:3": "Run 3 has held box-a for 5.0 h; nothing was changed, as a job may still use it.",
    }
    for key, text in texts.items():
        d.alert(key, text, "low", every_s=0)
        got = p.db.one("SELECT text FROM messages WHERE kind='alert' AND ref=?", (key,))["text"]
        assert got == text, (key, got)
    d.alert("outage:box-b", texts["push-held"], "high", outage=False)
    assert p.db.one("SELECT text FROM messages WHERE kind='alert' AND ref='outage:box-b'")["text"] == texts["push-held"]


def test_machine_pages_and_status_say_how_long_and_who_acts_now(env):
    """A machine condition's page names its start, duration so far and the actor at each step: the owner,
    the fallback once the owner cannot act (naming the outage task queued in it), then nobody."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import responsibilities
    from ttp.daemon import Daemon
    alpha = make(env, "alpha")
    mm.add("box-a", tags="device")
    mm.set_recovery("box-a", owner="alpha", fallback="beta")
    t0 = time.time() - 6 * 3600
    ml.report("gamma", "box-a", "held", "device held, no holder", now=t0)
    able = {"alpha": (False, "it is paused"), "beta": (True, "")}
    first = t0 + ml.GRACE_S

    def recovery(at):   # every page goes out; the recovery pages are returned
        got = ml.route(at, able=able.get, healthy=lambda *a: [])
        for x in got:
            ml.delivered(x)
        return [x for x in got if x["kind"] == "machine_condition"]
    pages = recovery(first)
    assert [x["target"] for x in pages] == ["alpha"]
    text = pages[0]["text"]
    assert "Machine box-a held" in text and "(30 min so far)" in text and "Acting now: alpha (owner)." in text, text
    assert "Tried: nothing yet." in text and "Tracked task: none." in text and "Next: alpha checks" in text, text
    # The owner cannot act: the outage task (outagetask.py) is queued in the fallback; later pages name it.
    ml.report("gamma", "box-a", "held", "device held, no holder", now=first + 60)
    recovery(first + 60)
    later = first + 60 + ml.UNAVAILABLE_S
    pages = recovery(later)
    assert [x["target"] for x in pages] == ["beta"]
    text = pages[0]["text"]
    assert "Acting now: beta (fallback; alpha cannot act: it is paused)." in text, text
    assert f"Tracked task: outage:box-a:held@{int(t0)} in beta, due " in text, text
    assert "Tried: recovery page 1 to alpha." in text and "(1.0 h so far)" in text, text
    line = responsibilities.machine_lines(next(r for r in responsibilities.machine_status("box-a")
                                               if r["alias"] == "box-a"))
    cond = next(x for x in line if x.startswith("  condition: "))
    assert "so far; seen by gamma" in cond and "Acting now: beta (fallback)" in cond, cond
    assert f"Tracked task: outage:box-a:held@{int(t0)} in beta" in cond, cond
    able["beta"] = (False, "its daemon is stopped")
    ml.report("gamma", "box-a", "held", now=later + 60)
    recovery(later + 60)
    pages = recovery(later + 60 + ml.UNAVAILABLE_S)
    assert [x["target"] for x in pages] == [ml.USER]
    text = pages[0]["text"]
    assert "Acting now: nobody: escalating to the user (beta cannot act: its daemon is stopped)." in text, text
    assert "recovery page 1 to alpha; recovery page 2 to beta" in text, text
    Daemon(alpha.base)._machine_page(pages[0], later)
    got = alpha.db.one("SELECT text FROM messages WHERE kind='alert' AND ref=?", (pages[0]["key"],))["text"]
    assert got.startswith("Outage: Machine box-a held") and got.count("Acting now:") == 1, got
    # A page recorded before this wording still names the actor.
    Daemon(alpha.base)._machine_page({**pages[0], "key": "machine:old", "text": "Machine box-a held."}, later)
    old = alpha.db.one("SELECT text FROM messages WHERE kind='alert' AND ref='machine:old'")["text"]
    assert old == "Outage: Machine box-a held. Acting now: nobody: escalating to the user.", old

def _due(p, name):
    p.db.x("UPDATE schedules SET next_run=? WHERE name=?", (time.time() - 1, name))


def _settle(d, name, within=20.0):
    """Tick the schedules until `name`'s run is recorded; its row."""
    end = time.monotonic() + within
    d.run_schedules()
    while name in d._sched_runs and time.monotonic() < end:
        d.run_schedules()
    assert name not in d._sched_runs, f"schedule {name} still running after {within} s"
    return d.p.db.one("SELECT * FROM schedules WHERE name=?", (name,))


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    stat = f"/proc/{pid}/stat"
    return not os.path.exists(stat) or open(stat).read().rsplit(") ", 1)[1][0] != "Z"


def test_a_slow_command_schedule_never_holds_the_tick(env, monkeypatch):
    """A command that runs far longer than a tick goes on off the tick: the tick's later steps (message
    handling, dispatch) still run within seconds, and its result is recorded on a later tick."""
    from ttp import daemon as dm, schedule as sched
    p = make(env)
    sched.upsert(p.db, "slow", "command", "1h", payload={"command": "sleep 4; echo '{\"text\": \"done\"}'",
                                                        "timeout_s": 60})
    _due(p, "slow")
    d = dm.Daemon(p.base)
    later = []
    monkeypatch.setattr(d, "maybe_coordinate", lambda: later.append("coordinate"))
    monkeypatch.setattr(d, "dispatch", lambda: later.append("dispatch"))
    t0 = time.monotonic()
    d.tick()
    assert time.monotonic() - t0 < dm.SCHEDULE_WAIT_S + 3, "the tick waited for the slow schedule"
    assert later == ["coordinate", "dispatch"] and "slow" in d._sched_runs
    assert p.db.one("SELECT last_run FROM schedules WHERE name='slow'")["last_run"] is None
    row = _settle(d, "slow")
    assert row["last_status"] == "ok (1 observations)" and row["last_run"]


def test_a_command_schedule_past_its_timeout_has_its_group_killed_and_counts_as_failed(env, tmp_path):
    """Its whole process group is killed (the shell's background child too) and the run is a failed
    one: an error observation, and two in a row queue the usual fix task."""
    from ttp import daemon as dm, schedrun, schedule as sched, screen as scr
    p = make(env)
    pidfile = tmp_path / "child.pid"
    sched.upsert(p.db, "hangs", "command", "1h", payload={
        "command": f"sleep 60 & echo $! > {pidfile}; wait", "timeout_s": 1})
    d = dm.Daemon(p.base)
    for n in (1, 2):
        _due(p, "hangs")
        row = _settle(d, "hangs")
        assert row["last_status"] == "error: timed out after 1 s", row["last_status"]
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child), "the command's background child outlived its timeout"
    assert p.db.q("SELECT id FROM issues WHERE source=? AND status='open'", (scr.error_source("watcher:hangs"),))
    assert "hangs" in (p.db.kv(d.SCHEDULE_FIXES_KEY) or {}), "two failed runs in a row queue the fix task"
    assert not p.db.kv(schedrun.GROUPS_KEY), "an ended group is no longer kept"


def test_a_schedule_still_running_at_its_next_due_time_is_not_started_twice(env, tmp_path):
    from ttp import daemon as dm, schedule as sched
    p = make(env)
    count = tmp_path / "starts"
    sched.upsert(p.db, "busy", "command", "1m", payload={"command": f"echo x >> {count}; test $(wc -l < {count}) -gt 1 || sleep 5",
                                                        "timeout_s": 60})
    d = dm.Daemon(p.base)
    for _ in range(2):   # 4 s at most: the first run takes 5
        _due(p, "busy")
        d.run_schedules()
    assert count.read_text().count("x") == 1 and "busy" in d._sched_runs
    _settle(d, "busy")
    _due(p, "busy")
    _settle(d, "busy")
    assert count.read_text().count("x") == 2, "once it ended it runs again when due"


def test_replay_a_hanging_gh_in_the_pr_watcher_does_not_stall_the_tick(env, monkeypatch):
    """Replay of a tick stalled in run_schedules: the builtin prs watcher sat in a gh call (subprocess
    communicate) that did not answer. The tick now goes on; a run past its timeout is recorded failed
    and its late result dropped, and no second run starts while the hung one still goes."""
    from ttp import daemon as dm, schedule as sched, watchers
    p = make(env)
    p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
           ("t", "code", "done", "https://github.com/acme/widgets/pull/1", time.time()))
    gate, calls = threading.Event(), []

    def hung_gh(args, cwd):
        calls.append(args[0])
        gate.wait(30)
        return None
    monkeypatch.setattr(watchers, "_gh", hung_gh)
    monkeypatch.setattr(dm, "SCHEDULE_GRACE_S", 0)
    sched.upsert(p.db, "prs", "watcher", "5m", payload={"builtin": "prs", "deadline_s": 1})
    _due(p, "prs")
    d = dm.Daemon(p.base)
    later = []
    monkeypatch.setattr(d, "dispatch", lambda: later.append("dispatch"))
    t0 = time.monotonic()
    d.tick()
    assert time.monotonic() - t0 < dm.SCHEDULE_WAIT_S + 3 and later == ["dispatch"]
    time.sleep(1.1)
    d.run_schedules()
    row = p.db.one("SELECT * FROM schedules WHERE name='prs'")
    assert row["last_status"] == "error: timed out after 1 s", row["last_status"]
    _due(p, "prs")
    d.run_schedules()
    assert len(calls) == 1, "the hung run is not started again while it still goes"
    gate.set()
    _settle(d, "prs")
    assert p.db.one("SELECT last_status FROM schedules WHERE name='prs'")["last_status"].startswith("error: timed out")


def test_a_command_schedule_killed_by_a_daemon_stop_records_nothing_and_stays_due(env):
    """The stop kills the run's group; the command then ends -9, which is not its result: no error
    issue, no event to wake the coordinator, no last_run. The next daemon runs it again."""
    from ttp import daemon as dm, schedrun, schedule as sched, screen as scr
    p = make(env)
    sched.upsert(p.db, "long", "command", "1h", payload={"command": "sleep 60", "timeout_s": 120})
    _due(p, "long")
    d = dm.Daemon(p.base)
    d.run_schedules()
    run = d._sched_runs["long"]
    assert _wait_for(lambda: p.db.kv(schedrun.GROUPS_KEY), 10)
    events = p.db.one("SELECT COUNT(*) n FROM events")["n"]
    assert schedrun.stop(p.db, list(d._sched_runs.values())), "the stop killed its group"
    assert run.done.wait(5)
    time.sleep(1)
    d.run_schedules()
    assert "long" not in d._sched_runs
    assert not p.db.q("SELECT id FROM issues WHERE source=?", (scr.error_source("watcher:long"),))
    assert p.db.one("SELECT COUNT(*) n FROM events")["n"] == events, "the kill queued an event"
    row = p.db.one("SELECT last_run, next_run FROM schedules WHERE name='long'")
    assert row["last_run"] is None and row["next_run"] <= time.time(), "it stays due"


def test_a_heal_fix_killed_by_a_daemon_stop_does_not_escalate_and_rechecks_next_start(env):
    from ttp import daemon as dm, heal, schedrun, schedule as sched
    p = make(env)
    sched.upsert(p.db, "svc", "command", "5m", payload={"heal": {
        "check": "false", "fix": "sleep 60", "grace_s": 0, "settle_s": 600}})
    _due(p, "svc")
    d1 = dm.Daemon(p.base)
    d1.run_schedules()
    run = d1._sched_runs["svc"]
    assert _wait_for(lambda: heal.state(p.db, "svc").get("phase") == "fixing" and p.db.kv(schedrun.GROUPS_KEY), 10)
    assert schedrun.stop(p.db, list(d1._sched_runs.values()))
    assert run.done.wait(5)
    d1.run_schedules()
    st = heal.state(p.db, "svc")
    assert st.get("phase") == "fixing" and "fix_rc" not in st and not st.get("task"), st
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Self-fix%'"), "the killed fix escalated"
    assert p.db.one("SELECT last_run FROM schedules WHERE name='svc'")["last_run"] is None
    row = _settle(dm.Daemon(p.base), "svc")
    assert row["last_status"] == "unhealthy (fix interrupted; rechecking)", row["last_status"]
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Self-fix%'")


def test_a_run_killed_at_the_ticks_backstop_is_recorded_once_as_timed_out(env):
    """The tick's backstop (past the run's own bounds) records the timeout and kills the group; the
    command then ends -9 in its thread, which is not a second failure to record."""
    from ttp import daemon as dm, schedrun, schedule as sched, screen as scr
    p = make(env)
    sched.upsert(p.db, "long", "command", "1h", payload={"command": "sleep 60", "timeout_s": 120})
    _due(p, "long")
    d = dm.Daemon(p.base)
    d.run_schedules()
    run = d._sched_runs["long"]
    assert _wait_for(lambda: p.db.kv(schedrun.GROUPS_KEY), 10)
    run.deadline = time.monotonic()
    d.run_schedules()
    assert run.done.wait(5)
    time.sleep(0.5)
    d.run_schedules()
    assert "long" not in d._sched_runs
    titles = [r["title"] for r in p.db.q("SELECT title FROM issues WHERE source=?", (scr.error_source("watcher:long"),))]
    assert titles and not any("rc=-9" in t for t in titles), titles
    assert p.db.one("SELECT last_status FROM schedules WHERE name='long'")["last_status"].startswith("error: timed out")


def test_a_timed_out_run_whose_thread_never_ends_is_observed_once_more_as_stuck(env, monkeypatch):
    """A builtin watcher stuck in Python ignores the backstop's kill, so its schedule cannot run again:
    once it is still going STUCK_FACTOR x its timeout after the kill, a second, keyed issue says so, once."""
    import threading
    from ttp import daemon as dm, schedrun, schedule as sched, screen as scr, watchers
    p = make(env)
    release, calls = threading.Event(), []

    def stuck(host, name, payload):
        calls.append(name)
        release.wait(30)
        return "ok"
    monkeypatch.setattr(watchers, "run_builtin", stuck)
    sched.upsert(p.db, "spin", "watcher", "1h", payload={"deadline_s": 5})
    _due(p, "spin")
    d = dm.Daemon(p.base)
    try:
        d.run_schedules()
        run = d._sched_runs["spin"]
        assert _wait_for(lambda: calls, 5)
        source = scr.error_source("watcher:spin")
        issues = lambda: p.db.q("SELECT title, count, watch_key FROM issues WHERE source=?", (source,))
        run.deadline = time.monotonic()
        d.run_schedules()
        assert run.timed_out and not run.done.is_set() and len(issues()) == 1, issues()
        d.run_schedules()
        assert len(issues()) == 1 and not run.stuck, "stuck reported before STUCK_FACTOR x its timeout"
        run.killed_at -= schedrun.STUCK_FACTOR * run.timeout
        d.run_schedules()
        d.run_schedules()
        stuck_rows = [r for r in issues() if r["watch_key"]]
        assert run.stuck and len(issues()) == 2 and len(stuck_rows) == 1, issues()
        assert stuck_rows[0]["count"] == 1 and "still going" in stuck_rows[0]["title"], stuck_rows
        _due(p, "spin")
        d.run_schedules()
        assert calls == ["spin"], "a second run started while the stuck one goes on"
    finally:
        release.set()
    assert run.done.wait(5)
    d.run_schedules()
    assert "spin" not in d._sched_runs


def test_a_heal_fix_killed_at_the_ticks_backstop_does_not_escalate_its_kill(env):
    from ttp import daemon as dm, heal, schedrun, schedule as sched
    p = make(env)
    sched.upsert(p.db, "svc", "command", "5m", payload={"heal": {
        "check": "false", "fix": "sleep 60", "grace_s": 0, "settle_s": 600}})
    _due(p, "svc")
    d = dm.Daemon(p.base)
    d.run_schedules()
    run = d._sched_runs["svc"]
    assert _wait_for(lambda: heal.state(p.db, "svc").get("phase") == "fixing" and p.db.kv(schedrun.GROUPS_KEY), 10)
    run.deadline = time.monotonic()
    d.run_schedules()
    assert run.done.wait(5)
    d.run_schedules()
    st = heal.state(p.db, "svc")
    assert "fix_rc" not in st and not st.get("task"), st
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Self-fix%'"), "the backstop's kill escalated"
    assert p.db.one("SELECT last_status FROM schedules WHERE name='svc'")["last_status"].startswith("error: timed out")


def test_a_restarted_daemon_kills_the_groups_its_predecessors_runs_left_and_runs_them_once_more(env, tmp_path):
    from ttp import daemon as dm, schedrun, schedule as sched
    p = make(env)
    first = tmp_path / "first"
    sched.upsert(p.db, "long", "command", "1h", payload={
        "command": f"test -f {first} || {{ touch {first}; exec sleep 60; }}", "timeout_s": 120})
    _due(p, "long")
    d1 = dm.Daemon(p.base)
    d1.run_schedules()
    groups = p.db.kv(schedrun.GROUPS_KEY) or {}
    assert len(groups) == 1 and "long" in d1._sched_runs
    pid = int(next(iter(groups)))
    assert p.db.one("SELECT last_run FROM schedules WHERE name='long'")["last_run"] is None
    d2 = dm.Daemon(p.base)   # d1 is gone: its tick never records that run
    row = _settle(d2, "long")
    assert not _alive(pid), "the earlier daemon's command was killed"
    assert row["last_status"] == "ok (0 observations)", "the schedule ran once more, recorded once"


def test_the_desktop_notifier_alarms_once_when_a_remote_host_stays_unreachable_and_once_on_recovery(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from ttp import notifier, timefmt
    monkeypatch.setattr(notifier, "load_registry", lambda: {"projects": {
        "a1": {"dir": "/w/a1", "host": "host-a"}, "a2": {"dir": "/w/a2", "host": "host-a"},
        "b1": {"dir": "/w/b1", "host": "host-b"}}})
    up = {"host-a": True, "host-b": True}
    monkeypatch.setattr(notifier, "_remote_alerts",
                        lambda entry, name, after, floor, *a: ([], True, None) if up[entry["host"]] else (None, False, None))
    monkeypatch.setattr(timefmt, "ambient", lambda: "Europe/Berlin")
    shown = []
    monkeypatch.setattr(notifier, "show", lambda title, body, url=None, **kw: shown.append(body))
    t0, state = 1_800_000_000, {}

    def poll(t, st=None):
        return notifier.run_once(json.loads(json.dumps(st if st is not None else state)), "high", 1200, now=t0 + t)

    def polls(a, b):   # one poll a minute, as the notifier does
        st = state
        for t in [*range(a, b, 60), b]:
            st = poll(t, st)
        return st

    state = poll(0)
    up["host-a"] = False
    state = polls(60, 60 + 1199)
    assert shown == [], "a failure under the threshold alarmed"
    up["host-a"] = True
    state = poll(1300)
    assert shown == [], "a short blip alarmed or announced a recovery"
    up["host-a"] = False
    state = polls(1400, 1400 + 1200)
    since = datetime.fromtimestamp(t0 + 1400, ZoneInfo("Europe/Berlin")).strftime("%H:%M %Z")
    assert shown == [f"host-a unreachable since {since}; its projects cannot page"], shown
    for t in (2700, 2800, 5000):
        state = poll(t)
    # A restart reloads the state file: still one alarm.
    state = poll(5100, json.loads(json.dumps(state)))
    assert len(shown) == 1, shown
    up["host-a"] = True
    state = poll(5200)
    state = poll(5300)
    assert shown[1:] == ["host-a answers again; its projects can page again"], shown
    # Hosts are tracked apart: host-b going down does not touch host-a, and back.
    up["host-b"] = False
    state = polls(6000, 7200)
    assert len(shown) == 3 and shown[2].startswith("host-b unreachable since"), shown
    assert state["_hosts"]["host-a"].get("alarmed") is None and state["_hosts"]["host-b"]["alarmed"]


def test_the_desktop_notifier_starts_a_host_down_streak_anew_after_a_gap_between_polls(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from ttp import notifier, timefmt
    monkeypatch.setattr(timefmt, "ambient", lambda: "Europe/Berlin")
    shown = []
    monkeypatch.setattr(notifier, "show", lambda title, body, url=None, **kw: shown.append(body))
    t0, st = 1_800_000_000, {}
    notifier._track_hosts(st, {"h": True}, t0, 1200)
    notifier._track_hosts(st, {"h": False}, t0 + 60, 1200)
    # The laptop sleeps for hours; on wake the network is not up yet for one poll.
    wake = t0 + 60 + 15 * 3600
    notifier._track_hosts(st, {"h": False}, wake, 1200)
    assert shown == [], "a failure before and after a long gap alarmed at once"
    notifier._track_hosts(st, {"h": True}, wake + 60, 1200)
    assert shown == [], "a recovery was announced with no alarm before it"
    # Down again after a gap, then failing every minute for the threshold: one alarm, since after the gap.
    notifier._track_hosts(st, {"h": False}, wake + 120, 1200)
    later = wake + 120 + 6 * 3600
    for t in range(later, later + 1201, 60):
        notifier._track_hosts(st, {"h": False}, t, 1200)
    since = datetime.fromtimestamp(later, ZoneInfo("Europe/Berlin")).strftime("%H:%M %Z")
    assert shown == [f"h unreachable since {since}; its projects cannot page"], shown
    # An alarmed host stays alarmed across a gap (no second alarm), and an empty registry read keeps it.
    notifier._track_hosts(st, {"h": False}, later + 9 * 3600, 1200)
    notifier._track_hosts(st, {}, later + 9 * 3600 + 60, 1200)
    notifier._track_hosts(st, {"h": False}, later + 9 * 3600 + 120, 1200)
    assert len(shown) == 1, shown
    notifier._track_hosts(st, {"h": True}, later + 9 * 3600 + 180, 1200)
    assert shown[1:] == ["h answers again; its projects can page again"], shown


def _slow_ssh_notifier(monkeypatch, n_projects=9, ssh_s=40):
    """A notifier whose every remote poll takes `ssh_s` of a fake clock, with `n_projects` on one
    down host: one run takes longer than HOST_POLL_GAP_S. Returns (clock, poll, shown)."""
    from ttp import notifier, timefmt
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(notifier.time, "time", lambda: clock["t"])
    monkeypatch.setattr(notifier, "load_registry", lambda: {"projects": {
        f"p{i}": {"dir": f"/w/p{i}", "host": "host-a"} for i in range(n_projects)}})
    up = {"host-a": True}

    def remote(entry, name, after, floor, *a):
        clock["t"] += ssh_s
        return ([], True, None) if up["host-a"] else (None, False, None)
    monkeypatch.setattr(notifier, "_remote_alerts", remote)
    monkeypatch.setattr(timefmt, "ambient", lambda: "Europe/Berlin")
    shown = []
    monkeypatch.setattr(notifier, "show", lambda title, body, url=None, **kw: shown.append(body))
    state = {}

    def poll(sleep_s=60):   # one run, then the notifier's sleep
        nonlocal state
        state = notifier.run_once(json.loads(json.dumps(state)), "high", 1200)
        clock["t"] += sleep_s
        return state
    poll()
    up["host-a"] = False
    return clock, poll, shown


def test_the_desktop_notifier_alarms_a_down_host_when_slow_ssh_makes_each_poll_outlast_the_gap(monkeypatch):
    from ttp import notifier
    clock, poll, shown = _slow_ssh_notifier(monkeypatch)
    first = clock["t"]
    while clock["t"] - first < 1200 + 2 * 420:
        poll()
    run_s = 9 * 40 + 60
    assert run_s > notifier.HOST_POLL_GAP_S, "the case must have runs land further apart than the gap"
    assert len(shown) == 1 and shown[0].startswith("host-a unreachable since"), shown
    for _ in range(5):
        poll()
    assert len(shown) == 1, shown


def test_the_desktop_notifier_still_restarts_a_down_streak_after_a_sleep_with_slow_ssh(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    clock, poll, shown = _slow_ssh_notifier(monkeypatch)
    poll(sleep_s=6 * 3600)   # one failed run, then the laptop sleeps for hours
    since = clock["t"] + 9 * 40   # the end of the first run after the sleep starts the new streak
    for _ in range(3):
        poll()
    assert shown == [], "the streak counted the sleep"
    while not shown and clock["t"] < since + 3 * 3600:
        poll()
    expect = datetime.fromtimestamp(since, ZoneInfo("Europe/Berlin")).strftime("%H:%M %Z")
    assert shown == [f"host-a unreachable since {expect}; its projects cannot page"], shown


def test_the_desktop_notifier_counts_only_an_ssh_connect_failure_as_a_host_down(monkeypatch):
    from types import SimpleNamespace
    from ttp import notifier
    rc = {"code": 255}
    monkeypatch.setattr(notifier.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=rc["code"], stdout="", stderr=""))
    entry = {"dir": "/w/a1", "host": "host-a"}
    assert notifier._remote_alerts(entry, "a1", 0, "high") == (None, False, None)
    rc["code"] = 1   # the host answered; the remote command failed
    assert notifier._remote_alerts(entry, "a1", 0, "high") == (None, True, None)
    rc["code"] = 0
    assert notifier._remote_alerts(entry, "a1", 0, "high") == ([], True, None)


def _daemon_notifier(monkeypatch, projects):
    """A notifier over remote `projects` {name: host} whose answers come from `up` {host: bool} and
    `hb` {name: health dict or None}. Returns (poll(t, state), up, hb, shown, reg)."""
    from ttp import notifier, timefmt
    reg = {n: {"dir": f"/w/{n}", "host": h} for n, h in projects.items()}
    monkeypatch.setattr(notifier, "load_registry", lambda: {"projects": dict(reg)})
    up = {h: True for h in projects.values()}
    hb = {n: {"heartbeat_age_s": 10.0, "service": "systemd", "paused": False} for n in projects}

    def remote(entry, name, after, floor, *a):
        return ([], True, hb[name]) if up[entry["host"]] else (None, False, None)
    monkeypatch.setattr(notifier, "_remote_alerts", remote)
    monkeypatch.setattr(timefmt, "ambient", lambda: "Europe/Berlin")
    shown = []
    monkeypatch.setattr(notifier, "show", lambda title, body, url=None, **kw: shown.append(body))

    def poll(t, st):   # a round trip through the state file, as the notifier keeps it
        return json.loads(json.dumps(notifier.run_once(st, "high", 1200, now=1_800_000_000 + t,
                                                         daemon_down_after_s=3 * 3600)))
    return poll, up, hb, shown, reg


def _down(hb, name, age):
    hb[name] = {**hb[name], "heartbeat_age_s": float(age)}


def test_the_desktop_notifier_alarms_once_when_a_daemon_stays_down_on_an_answering_host_and_once_on_recovery(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    poll, up, hb, shown, reg = _daemon_notifier(monkeypatch, {"proj-a": "host-a", "proj-b": "host-a"})
    st = poll(0, {})
    _down(hb, "proj-a", 3 * 3600 - 1)
    st = poll(60, st)
    assert shown == [], "a daemon under the threshold alarmed"
    _down(hb, "proj-a", 3 * 3600)
    st = poll(120, st)
    since = datetime.fromtimestamp(1_800_000_000 + 120 - 3 * 3600, ZoneInfo("Europe/Berlin")).strftime("%H:%M %Z")
    assert shown == [f"proj-a daemon down since {since} (no heartbeat for 3.0 h); its host host-a answers. "
                     "`ttp restart proj-a` on that host."], shown
    for t in range(180, 1800, 60):
        _down(hb, "proj-a", 3 * 3600 + t)
        st = poll(t, st)
    assert len(shown) == 1, "repeated polls of a down daemon alarmed again"
    # Projects are tracked apart: proj-b going down alarms once on its own.
    _down(hb, "proj-b", 4 * 3600)
    st = poll(1800, st)
    st = poll(1860, st)
    assert len(shown) == 2 and shown[1].startswith("proj-b daemon down since"), shown
    _down(hb, "proj-a", 5)
    st = poll(1920, st)
    st = poll(1980, st)
    assert shown[2:] == ["proj-a daemon runs again; it can page again"], shown
    assert "proj-a" not in st["_daemons"] and st["_daemons"]["proj-b"]
    # A project that leaves the registry goes from the state with no notice; an empty read keeps it.
    reg.clear()
    st = poll(2040, st)
    assert st["_daemons"].get("proj-b"), "an empty registry read dropped an alarmed project"
    reg["proj-a"] = {"dir": "/w/proj-a", "host": "host-a"}
    st = poll(2100, st)
    assert "proj-b" not in st["_daemons"] and len(shown) == 3, shown


def test_the_desktop_notifier_never_alarms_a_daemon_with_no_service_or_unknown_health(monkeypatch):
    poll, up, hb, shown, reg = _daemon_notifier(monkeypatch, {"proj-a": "host-a", "proj-b": "host-b"})
    st = poll(0, {})
    hb["proj-a"] = {"heartbeat_age_s": 9 * 3600.0, "service": None, "paused": False}   # stopped on purpose
    hb["proj-b"] = None   # an older ttp: health unknown
    for t in range(60, 600, 60):
        st = poll(t, st)
    assert shown == [], shown
    # An alarmed daemon whose health turns unknown stays alarmed; its service going away ends it quietly.
    hb["proj-b"] = {"heartbeat_age_s": 9 * 3600.0, "service": "cron", "paused": False}
    st = poll(660, st)
    assert len(shown) == 1 and shown[0].startswith("proj-b daemon down since"), shown
    hb["proj-b"] = None
    st = poll(720, st)
    assert st["_daemons"].get("proj-b"), "unknown health cleared the alarm"
    hb["proj-b"] = {"heartbeat_age_s": 9 * 3600.0, "service": None, "paused": False}
    st = poll(780, st)
    assert "proj-b" not in st["_daemons"] and len(shown) == 1, shown


def test_the_desktop_notifier_sends_only_the_host_alarm_while_a_host_is_unreachable(monkeypatch):
    poll, up, hb, shown, reg = _daemon_notifier(monkeypatch, {"proj-a": "host-a", "proj-b": "host-a"})
    st = poll(0, {})
    _down(hb, "proj-a", 4 * 3600)   # down before the host goes away: alarmed
    st = poll(60, st)
    assert len(shown) == 1 and shown[0].startswith("proj-a daemon down"), shown
    up["host-a"] = False
    for t in range(120, 120 + 1300, 60):
        st = poll(t, st)
    assert shown[1:] and shown[1].startswith("host-a unreachable since") and len(shown) == 2, shown
    # It answers again: proj-a is fine now (the host's notice covers it), proj-b's daemon is stale.
    up["host-a"] = True
    _down(hb, "proj-a", 5)
    _down(hb, "proj-b", 4 * 3600)
    st = poll(1500, st)
    st = poll(1560, st)
    assert shown[2] == "host-a answers again; its projects can page again", shown
    assert len(shown) == 4 and shown[3].startswith("proj-b daemon down since"), shown
    assert "proj-a" not in st["_daemons"]


def test_the_desktop_notifier_alarms_a_down_local_daemon_from_its_own_health(env, monkeypatch):
    import os
    from ttp import notifier, service, timefmt
    p = make(env)
    monkeypatch.setattr(service, "installed", lambda p: {"kind": "systemd", "watchdog": True})
    monkeypatch.setattr(timefmt, "ambient", lambda: "UTC")
    shown = []
    monkeypatch.setattr(notifier, "show", lambda title, body, url=None, **kw: shown.append(body))
    hb = p.state / "heartbeat"
    hb.write_text("{}")
    now = time.time()
    os.utime(hb, (now - 4 * 3600, now - 4 * 3600))
    h = notifier.health(p, now)
    assert h["service"] == "systemd" and h["paused"] is False and abs(h["heartbeat_age_s"] - 4 * 3600) < 1
    st = notifier.run_once({}, "high", 1200, now=now)
    assert len(shown) == 1 and shown[0].endswith("`ttp restart demo` on this machine."), shown
    st = notifier.run_once(json.loads(json.dumps(st)), "high", 1200, now=now + 60)
    assert len(shown) == 1, shown
    os.utime(hb, None)
    notifier.run_once(st, "high", 1200, now=time.time())
    assert shown[1:] == ["demo daemon runs again; it can page again"], shown


def test_alerts_health_flag_adds_the_daemon_health_and_leaves_the_default_output_alone(env, monkeypatch, capsys):
    import os
    from ttp import cli, service
    from ttp.daemon import Daemon
    p = make(env)
    Daemon(p.base).alert("coordinator", "The coordinator keeps failing", "high")
    monkeypatch.setattr(service, "installed", lambda p: None)
    cli.main(["alerts", "demo", "--json"])
    plain = json.loads(capsys.readouterr().out)
    assert isinstance(plain, list) and plain and plain[0]["text"] == "The coordinator keeps failing"
    hb = p.state / "heartbeat"
    hb.write_text("{}")
    os.utime(hb, (time.time() - 600, time.time() - 600))
    p.db.set_kv("paused", True)
    cli.main(["alerts", "demo", "--json", "--health"])
    got = json.loads(capsys.readouterr().out)
    assert got["alerts"] == plain
    assert got["health"]["service"] is None and got["health"]["paused"] is True
    assert 600 <= got["health"]["heartbeat_age_s"] < 660


def test_the_desktop_notifier_still_gets_alerts_from_an_older_remote_without_health(monkeypatch):
    from types import SimpleNamespace
    from ttp import notifier
    calls = []
    new = {"on": False}

    def run(argv, **k):
        calls.append(argv[-1])
        if "--health" in argv[-1]:
            if new["on"]:
                return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                    {"alerts": [{"id": 7, "text": "x"}], "health": {"heartbeat_age_s": 3.0, "service": "systemd",
                                                                     "paused": False}}))
            return SimpleNamespace(returncode=2, stdout="", stderr="ttp: error: unrecognized arguments: --health")
        return SimpleNamespace(returncode=0, stderr="", stdout='[{"id": 7, "text": "x"}]')
    monkeypatch.setattr(notifier.subprocess, "run", run)
    entry, st, t0 = {"dir": "/w/proj-a", "host": "host-a"}, {}, 1_800_000_000
    assert notifier._remote_alerts(entry, "proj-a", 0, "high", st, t0) == ([{"id": 7, "text": "x"}], True, None)
    assert len(calls) == 2 and "--health" in calls[0] and "--health" not in calls[1]
    assert st["_no_health"] == {"host-a": t0}
    calls.clear()
    assert notifier._remote_alerts(entry, "proj-a", 0, "high", st, t0 + 3599)[0] == [{"id": 7, "text": "x"}]
    assert len(calls) == 1 and "--health" not in calls[0], "the flag was retried within the hour"
    calls.clear()
    new["on"] = True   # upgraded: an hour on, the flag is asked again and the memory goes
    rows, ok, h = notifier._remote_alerts(entry, "proj-a", 0, "high", st, t0 + 3600)
    assert rows == [{"id": 7, "text": "x"}] and ok and h["service"] == "systemd"
    assert len(calls) == 1 and st["_no_health"] == {}


def _linger_daemon(env, monkeypatch, kind="systemd", answer="Linger=no"):
    """A daemon whose service is `kind` and whose loginctl answers `answer`; returns it and the calls."""
    from ttp import downtime, service
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    calls, said = [], {"out": answer}

    def loginctl(*argv):
        calls.append(argv)
        return subprocess.CompletedProcess(list(argv), 0, said["out"] + "\n", "")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service, "installed", lambda _p: {"kind": kind, "watchdog": True} if kind else None)
    monkeypatch.setattr(downtime, "_run", loginctl)
    return p, d, calls, said


def _linger_alerts(p):
    return p.db.q("SELECT text, severity FROM messages WHERE direction='out' AND ref='linger-off' "
                  "AND text NOT LIKE 'Cleared:%'")


def test_linger_off_under_systemd_raises_one_normal_alert_that_clears_once_linger_is_on(env, monkeypatch):
    """The incident: linger was off, so after a reboot the systemd user service waited for a login (about
    6 h down). The daemon looks at start and once a day; one alert names the fix and clears by itself."""
    from ttp import alerts, downtime
    p, d, calls, said = _linger_daemon(env, monkeypatch)
    d.check_linger()
    d.check_linger()   # within the recheck window: not looked at again
    assert len(calls) == 1 and calls[0][:2] == ("loginctl", "show-user")
    for _ in range(2):   # looked at again later, still off: still the one alert
        d._linger_at -= downtime.RECHECK_OFF_S + 1
        d.check_linger()
    assert len(calls) == 3
    (msg,) = _linger_alerts(p)
    assert msg["severity"] == "normal" and "loginctl enable-linger" in msg["text"], msg
    assert "start only at the user's next login" in msg["text"]
    assert alerts.tracked("alert", "normal", downtime.LINGER_KEY)
    assert not alerts.sweep(p.db), "cleared while linger was still off"
    said["out"] = "Linger=yes"
    d._linger_at -= downtime.RECHECK_OFF_S + 1
    d.check_linger()
    (ep,) = alerts.sweep(p.db)
    assert ep["key"] == downtime.LINGER_KEY
    assert not p.db.one("SELECT id FROM alerts WHERE cleared IS NULL")
    assert p.db.one("SELECT severity FROM messages WHERE text LIKE 'Cleared: Linger is on again%'")["severity"] == "low"
    # On, it is looked at once a day only.
    d._linger_at -= downtime.RECHECK_OFF_S + 1
    d.check_linger()
    assert len(calls) == 4
    d._linger_at -= downtime.CHECK_EVERY_S
    d.check_linger()
    assert len(calls) == 5 and len(_linger_alerts(p)) == 1


def test_a_user_neither_logged_in_nor_lingering_counts_as_linger_off_and_an_unclear_answer_changes_nothing(env, monkeypatch):
    from ttp import downtime
    fail = lambda err: (lambda *a: subprocess.CompletedProcess(list(a), 1, "", err))
    assert downtime.lingers("u", fail("Failed to get user: User ID 1000 is not logged in or lingering")) is False
    assert downtime.lingers("u", fail("Failed to connect to bus")) is None
    p, d, calls, said = _linger_daemon(env, monkeypatch, answer="")
    d.check_linger()
    assert calls and not _linger_alerts(p) and not p.db.kv(downtime.LINGER_KV)


@pytest.mark.parametrize("kind", ["launchd", "cron", None])
def test_launchd_cron_and_no_service_are_not_checked_for_linger(env, monkeypatch, kind):
    p, d, calls, _ = _linger_daemon(env, monkeypatch, kind=kind)
    d.check_linger()
    assert calls == [] and not _linger_alerts(p)


def test_macos_is_not_checked_for_linger(env, monkeypatch):
    p, d, calls, _ = _linger_daemon(env, monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    d.check_linger()
    assert calls == [] and not _linger_alerts(p)


def _failing_step_daemon(base, calls):
    """A Daemon whose dispatch and deliver_outbound only record that they ran, and whose
    sweep_alerts (first step loop) and retry_rejected (second loop) raise while `calls` holds 'fail'."""
    from ttp.daemon import Daemon
    d = Daemon(base)
    d.dispatch = lambda: calls.append("dispatch")
    real_deliver = d.deliver_outbound
    d.deliver_outbound = lambda: calls.append("deliver") or real_deliver()
    real_sweep = d.sweep_alerts

    def sweep_alerts():
        if "fail" in calls:
            raise RuntimeError("sweep broke")
        return real_sweep()

    def retry_rejected():
        if "fail" in calls:
            raise ValueError("bad row 7\nsecond line")
    d.sweep_alerts, d.retry_rejected = sweep_alerts, retry_rejected
    return d


def test_a_failing_tick_step_does_not_stop_dispatch_or_delivery(env):
    """A step that raises is logged with its name and the tick goes on: later steps in both loops
    (dispatch, deliver_outbound) still run in that tick."""
    p = make(env)
    calls: list[str] = ["fail"]
    d = _failing_step_daemon(p.base, calls)
    d.tick()
    assert "dispatch" in calls and "deliver" in calls
    logged = (p.logs / "daemon.log").read_text()
    assert "tick step sweep_alerts error:" in logged and "RuntimeError: sweep broke" in logged
    assert "tick step retry_rejected error:" in logged
    assert d._step_fails["sweep_alerts"][0] == 1 and d._step_fails["retry_rejected"][0] == 1
    assert not p.db.one("SELECT id FROM messages WHERE ref LIKE 'tick-step:%'"), "one failure is not alerted"


def test_a_tick_step_failing_three_ticks_in_a_row_is_alerted_then_cleared_by_a_success(env):
    """STEP_FAILS_ALERT failing ticks raise tick-step:<name> at normal severity naming the step and
    the error's first line; past STEP_FAILS_HIGH_S it rises to high; the next success clears it
    and the count."""
    from ttp import alerts
    from ttp import daemon as dm
    p = make(env)
    calls: list[str] = ["fail"]
    d = _failing_step_daemon(p.base, calls)
    for _ in range(dm.STEP_FAILS_ALERT - 1):
        d.tick()
    assert not p.db.one("SELECT id FROM alerts WHERE key='tick-step:retry_rejected'")
    d.tick()
    ep = p.db.one("SELECT * FROM alerts WHERE key='tick-step:retry_rejected' AND cleared IS NULL")
    assert ep and ep["severity"] == "normal"
    assert "retry_rejected" in ep["text"] and "ValueError: bad row 7" in ep["text"] and "second line" not in ep["text"]
    assert alerts.active(p.db, "tick-step:retry_rejected", ep["raised"], time.time())
    n, since = d._step_fails["retry_rejected"]
    d._step_fails["retry_rejected"] = (n, since - dm.STEP_FAILS_HIGH_S)   # failing for half an hour
    d.tick()
    ep = p.db.one("SELECT * FROM alerts WHERE key='tick-step:retry_rejected' AND cleared IS NULL")
    assert ep["severity"] == "high"
    assert p.db.one("SELECT COUNT(*) n FROM messages WHERE kind='alert' AND ref='tick-step:retry_rejected'")["n"] == 2
    calls.remove("fail")
    d.tick()
    assert "retry_rejected" not in d._step_fails and "sweep_alerts" not in d._step_fails
    assert not p.db.one("SELECT id FROM alerts WHERE key LIKE 'tick-step:%' AND cleared IS NULL")
    told = p.db.one("SELECT * FROM messages WHERE kind='resolved' AND ref='tick-step:retry_rejected'")
    assert told and "retry_rejected runs again" in told["text"]
    assert not alerts.holds(p.db, "tick-step:retry_rejected", ep["raised"], time.time())


def _notifier_stubs(monkeypatch, tmp_path, names, rc=0):
    """Stub notifier commands on an otherwise empty PATH; each records its argv NUL-separated."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name in names:
        stub = bindir / name
        stub.write_text(f"#!/bin/sh\nprintf '%s\\0' \"$@\" > \"{tmp_path}/{name}.argv\"\n"
                        f"echo 'stub refused' >&2\nexit {rc}\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    _notifier_files(monkeypatch, tmp_path)
    return lambda name: (tmp_path / f"{name}.argv").read_bytes().split(b"\0")[:-1]


def _notifier_files(monkeypatch, tmp_path):
    """The notifier's delivery record, start record and log in the test folder, never the account's."""
    from ttp import notifier
    for attr in ("DELIVERY", "RUNNING", "LOG"):
        monkeypatch.setattr(notifier, attr, tmp_path / pathlib.Path(getattr(notifier, attr)).name)


def test_the_desktop_notifier_passes_non_ascii_text_to_osascript_byte_for_byte(monkeypatch, tmp_path, capsys):
    from ttp import notifier
    monkeypatch.setattr(notifier.sys, "platform", "darwin")
    argv = _notifier_stubs(monkeypatch, tmp_path, ["osascript"])
    body, title = "build → done, “quoted” — \"plain\" \\ end", "tt-project · proj‑a"
    notifier.show(title, body)
    got = argv("osascript")
    assert got[-2:] == [body.encode(), title.encode()], "text arrives as arguments, unchanged"
    assert not any(body.encode() in a for a in got[:-2]), "the text is never spliced into the script"
    assert b"item 1 of argv" in b" ".join(got) and capsys.readouterr().err == ""


_LOG_LINE = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \S* ?notifier: "


@pytest.mark.parametrize("platform,cmd", [("darwin", "osascript"), ("darwin", "terminal-notifier"),
                                          ("linux", "notify-send")])
def test_the_desktop_notifier_logs_and_records_a_refused_notification(monkeypatch, tmp_path, platform, cmd):
    """A non-zero exit (macOS refusing it, no notification server) is logged with a timestamp and the
    backend, and recorded as failing from the first failure until a delivery succeeds."""
    from ttp import notifier
    monkeypatch.setattr(notifier.sys, "platform", platform)
    monkeypatch.setenv("DISPLAY", ":0")
    argv = _notifier_stubs(monkeypatch, tmp_path, [cmd], rc=3)
    _agent(monkeypatch, tmp_path)
    res = notifier.show("tt-project · a", "alert one")
    assert argv(cmd), "the stub ran"
    assert res["ok"] is False and res["backend"] == cmd and res["error"] == "exited 3: stub refused"
    (line,) = notifier.LOG.read_text().splitlines()
    assert re.match(_LOG_LINE + f"{cmd} delivery failed: exited 3: stub refused$", line), line
    first = notifier.failing()
    assert first and first["failing_since"] == res["ts"]
    notifier.show("tt-project · a", "alert two")
    assert notifier.failing()["failing_since"] == first["failing_since"], "one failing stretch, not one per alert"
    _notifier_stubs(monkeypatch, tmp_path, [cmd], rc=0)
    assert notifier.show("tt-project · a", "alert three")["ok"]
    assert notifier.failing() is None and "failing_since" not in notifier.delivery()
    assert len(notifier.LOG.read_text().splitlines()) == 2, "a success logs nothing"


def test_the_desktop_notifier_logs_a_timeout_and_a_missing_backend(monkeypatch, tmp_path):
    from ttp import notifier
    _notifier_stubs(monkeypatch, tmp_path, ["osascript"])
    monkeypatch.setattr(notifier.sys, "platform", "darwin")

    def hang(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])
    monkeypatch.setattr(notifier.subprocess, "run", hang)
    res = notifier.show("t", "b")
    assert not res["ok"] and res["error"] == f"timed out after {notifier.TIMEOUT_S}s"
    monkeypatch.setattr(notifier.sys, "platform", "linux")
    res = notifier.show("t", "b")   # PATH holds no notify-send
    assert not res["ok"] and res["backend"] == "notify-send" and res["error"] == "notify-send is not installed"
    _notifier_stubs(monkeypatch, tmp_path, ["notify-send"])
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        monkeypatch.delenv(var, raising=False)
    res = notifier.show("t", "b")
    assert not res["ok"] and res["error"].startswith("no desktop session")
    monkeypatch.setattr(notifier.sys, "platform", "win32")
    assert notifier.show("t", "b")["error"] == "no desktop notification backend on win32"
    lines = notifier.LOG.read_text().splitlines()
    assert len(lines) == 4 and all(re.match(_LOG_LINE, ln) for ln in lines)
    assert "osascript delivery failed: timed out" in lines[0] and "notify-send delivery failed: notify-send is not" in lines[1]


def test_every_desktop_notification_plays_a_sound(monkeypatch, tmp_path):
    from ttp import notifier
    monkeypatch.setattr(notifier.sys, "platform", "darwin")
    argv = _notifier_stubs(monkeypatch, tmp_path, ["osascript"])
    notifier.show("t", "b")
    assert any(b'sound name "' in a for a in argv("osascript")), "the macOS command includes the sound clause"
    argv = _notifier_stubs(monkeypatch, tmp_path, ["terminal-notifier"])
    notifier.show("t", "b")
    got = argv("terminal-notifier")
    assert got[got.index(b"-sound") + 1] == b"default"
    monkeypatch.setattr(notifier.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    argv = _notifier_stubs(monkeypatch, tmp_path, ["notify-send"])
    notifier.show("t", "b", severity="normal")
    got = argv("notify-send")
    assert b"string:sound-name:message-new-instant" in got and got[got.index(b"-u") + 1] == b"critical"


@pytest.mark.parametrize("platform,cmd,rc,answer,exit_code,says", [
    ("darwin", "osascript", 0, None, 0, "test notification sent (osascript); macOS does not say whether it was shown"),
    ("darwin", "osascript", 0, "y", 0, "test notification delivered (osascript), seen by you"),
    ("darwin", "osascript", 0, "n", 1, "test notification not shown (osascript)"),
    ("darwin", "osascript", 1, None, 1, "test notification failed (osascript): exited 1: stub refused"),
    ("darwin", "terminal-notifier", 1, None, 1, "test notification failed (terminal-notifier)"),
    ("linux", "notify-send", 0, None, 0, "test notification delivered (notify-send)"),
    ("linux", "notify-send", 1, None, 1, "test notification failed (notify-send)"),
])
def test_ttp_notifier_test_reports_the_backend_the_result_and_the_fix(env, monkeypatch, tmp_path, capsys, platform,
                                                                     cmd, rc, answer, exit_code, says):
    from ttp import cli, notifier
    monkeypatch.setattr(notifier.sys, "platform", platform)
    monkeypatch.setenv("DISPLAY", ":0")
    _notifier_stubs(monkeypatch, tmp_path, [cmd], rc=rc)
    _agent(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: answer is not None, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: answer is not None, raising=False)
    monkeypatch.setattr("builtins.input", lambda q: answer)
    with pytest.raises(SystemExit) as e:
        cli.main(["notifier", "test"])
        raise SystemExit(0)
    out = capsys.readouterr().out
    assert e.value.code == exit_code and says in out, out
    fix = {"osascript": "System Settings > Notifications > Script Editor:", "terminal-notifier":
           "System Settings > Notifications > terminal-notifier", "notify-send": "No notification server answered"}[cmd]
    assert (fix in out) == (exit_code == 1 or answer is None and platform == "darwin"), out
    if cmd == "osascript" and exit_code:
        assert "brew install terminal-notifier" in out
    assert bool(notifier.failing()) == bool(exit_code)


def _agent(monkeypatch, tmp_path, on=True):
    """This user's notifier agent installed (a systemd unit and a launchd plist in a test HOME), enabled
    while `state["on"]` (systemctl is-enabled, launchctl print)."""
    from ttp import notifier
    monkeypatch.delenv("TTP_TEST_NO_NOTIFIER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    for f in (tmp_path / "userhome" / ".config" / "systemd" / "user" / notifier.UNIT,
              tmp_path / "userhome" / "Library" / "LaunchAgents" / f"{notifier.LABEL}.plist"):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("[Unit]\n")
    state = {"on": on}
    monkeypatch.setattr(notifier, "_service_on", lambda: state["on"])
    monkeypatch.setattr(notifier, "_enabled_cache", {})
    monkeypatch.setattr(notifier, "ENABLED_CACHE_S", 0)
    return state


def _fake_agent(monkeypatch, tmp_path, running_code, on=True):
    """An installed notifier agent whose running process reports `running_code` (None: no start record)."""
    from ttp import notifier
    _notifier_files(monkeypatch, tmp_path)
    monkeypatch.setattr(notifier.sys, "platform", "linux")
    _agent(monkeypatch, tmp_path, on)
    if running_code:
        notifier.write_json(notifier.RUNNING, {"pid": os.getpid(), "code": running_code})
    calls = []
    monkeypatch.setattr(notifier, "install", lambda: calls.append("restart") or "installed and started (test)")
    monkeypatch.setattr(notifier, "test", lambda ask=None: calls.append("test") or (True, ["test notification sent (x)"]))
    return calls


@pytest.mark.parametrize("running", ["old", "current", None])
def test_setup_restarts_the_notifier_and_sends_one_test_per_code_version(env, monkeypatch, tmp_path, running):
    from ttp import notifier
    calls = _fake_agent(monkeypatch, tmp_path, {"old": "0.0.1 (abc)", "current": None, None: None}[running])
    want = notifier.installed_code()
    if running == "current":
        notifier.write_json(notifier.RUNNING, {"pid": os.getpid(), "code": want})
    out = notifier.refresh(send_test=True, always=True)   # `ttp setup`
    assert calls == ["restart", "test"] and out[0].startswith("desktop notifier: installed and started"), out
    assert out[1] == "desktop notifier: test notification sent (x)"
    calls.clear()
    out = notifier.refresh(send_test=True, always=True)   # again on the same code: restart, no second test
    assert calls == ["restart"] and "already went out on ttp " + want in out[1]


def test_upgrade_restarts_only_a_notifier_on_other_code_and_auto_sends_no_test(env, monkeypatch, tmp_path):
    from ttp import notifier
    calls = _fake_agent(monkeypatch, tmp_path, "0.0.1 (abc)")
    assert notifier.status_lines() == [f"desktop notifier runs ttp 0.0.1 (abc), not the installed "
                                       f"{notifier.installed_code()}: `ttp notifier restart`"]
    notifier.refresh(send_test=False)   # `ttp upgrade --auto`
    assert calls == ["restart"]
    notifier.write_json(notifier.RUNNING, {"pid": os.getpid(), "code": notifier.installed_code()})
    calls.clear()
    notifier.refresh(send_test=False)
    assert calls == [] and notifier.status_lines() == []
    notifier.refresh(send_test=True)   # `ttp upgrade` by hand: the test once on this code
    notifier.refresh(send_test=True)
    assert calls == ["test"]
    monkeypatch.setenv("TTP_TEST_NO_NOTIFIER", "1")
    assert notifier.refresh(send_test=True, always=True) == [] and calls == ["test"]


@pytest.mark.parametrize("argv,auto", [(["upgrade", "demo"], False), (["upgrade", "demo", "--auto"], True)])
def test_ttp_upgrade_refreshes_the_notifier_and_only_a_hand_upgrade_tests_it(env, monkeypatch, argv, auto):
    from ttp import cli, notifier
    make(env)
    got = []

    class Stop(Exception):
        pass

    def refresh(send_test, always=False):
        got.append((send_test, always))
        raise Stop
    monkeypatch.setattr(notifier, "refresh", refresh)
    with pytest.raises(Stop):
        cli.main(argv)
    assert got == [(not auto, False)]


def test_ttp_setup_restarts_the_installed_notifier(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, notifier
    got = []
    monkeypatch.setattr(notifier, "refresh", lambda send_test, always=False: got.append((send_test, always))
                        or ["desktop notifier: restarted (test)"])
    cli.main(["setup", "--bin-dir", str(tmp_path / "bin"), "--force"])
    assert got == [(True, True)] and "desktop notifier: restarted (test)" in capsys.readouterr().out


def test_the_notifier_unit_runs_the_installed_runtime_path_and_is_restarted(env, monkeypatch, tmp_path):
    """The agent runs from lib/current, not the release folder it resolves to, so a restart picks up new
    code; a running systemd unit is restarted, as `enable --now` would leave it on the old code."""
    from ttp import notifier, release
    _install_template(env)
    _notifier_files(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setattr(notifier.sys, "platform", "linux")
    ran = []
    monkeypatch.setattr(notifier.subprocess, "run", lambda args, **kw: ran.append(args[2:]) or
                        subprocess.CompletedProcess(args, 0, "", ""))
    assert notifier.install() == "installed and started (systemd user unit)"
    unit = (tmp_path / "userhome" / ".config" / "systemd" / "user" / notifier.UNIT).read_text()
    assert f"Environment=PYTHONPATH={release.installed() / 'runtime'}\n" in unit
    assert ran == [["daemon-reload"], ["enable", notifier.UNIT], ["restart", notifier.UNIT]]


def test_the_notifier_logs_its_code_at_start_and_exits_once_the_installed_code_changes(env, monkeypatch, tmp_path):
    from ttp import notifier, release
    _install_template(env)
    _notifier_files(monkeypatch, tmp_path)
    rt = release.installed() / "runtime"
    monkeypatch.setattr(notifier, "own_runtime", lambda: rt)
    monkeypatch.setattr(notifier, "run_once", lambda state, *a, **kw: state)
    monkeypatch.setattr(notifier, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(notifier, "CONF", tmp_path / "conf.json")
    mine = notifier.code(rt)
    sleeps = []

    def sleep(s):   # one minute later, `ttp setup` installed other code
        sleeps.append(s)
        init = rt / "ttp" / "__init__.py"
        init.write_text(init.read_text().replace(f'"{notifier.code(rt).split()[0]}"', '"9.9.9"'))
    monkeypatch.setattr(notifier.time, "sleep", sleep)
    assert notifier.main() == 0 and len(sleeps) == 1
    rec = json.loads(notifier.RUNNING.read_text())
    assert rec["code"] == mine and rec["pid"] == os.getpid() and rec["python"]
    lines = notifier.LOG.read_text().splitlines()
    assert re.match(_LOG_LINE + re.escape(f"started: ttp {mine} under Python "), lines[0]), lines
    assert "installed code is now ttp 9.9.9" in lines[1]
    monkeypatch.setattr(notifier, "own_runtime", lambda: tmp_path)   # run from elsewhere: never exits for it
    assert not notifier.outdated("0.0.1 (abc)")


def test_a_failing_desktop_notifier_raises_one_chat_alert_that_clears_once_a_delivery_succeeds(env, monkeypatch, tmp_path):
    from ttp import alerts, notifier
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.check_notifier()
    assert not p.db.one("SELECT id FROM messages WHERE ref='notifier'"), "nothing failed yet"
    monkeypatch.setattr(notifier.sys, "platform", "darwin")
    _notifier_stubs(monkeypatch, tmp_path, ["osascript"], rc=1)
    _agent(monkeypatch, tmp_path)
    for _ in range(3):   # three alerts fail to show: one chat alert, not three
        notifier.show("tt-project · demo", "an alert")
        d.check_notifier()
    (msg,) = p.db.q("SELECT * FROM messages WHERE ref='notifier'")
    assert msg["severity"] == "normal" and msg["channel"] == "chat" and msg["chat"] is None
    assert "Desktop notifications on" in msg["text"] and "osascript: exited 1: stub refused" in msg["text"]
    assert "`ttp notifier test`" in msg["text"]
    assert alerts.tracked("alert", "normal", notifier.ALERT_KEY) and not alerts.sweep(p.db)
    _notifier_stubs(monkeypatch, tmp_path, ["osascript"], rc=0)
    notifier.show("tt-project · demo", "another alert")
    (ep,) = alerts.sweep(p.db)
    assert ep["key"] == notifier.ALERT_KEY
    told = p.db.one("SELECT * FROM messages WHERE text LIKE 'Cleared: Desktop notifications deliver again%'")
    assert told and told["severity"] == "low"
    d.check_notifier()
    assert len(p.db.q("SELECT id FROM messages WHERE ref='notifier' AND kind='alert'")) == 1


def test_a_failed_notifier_test_alerts_only_while_an_enabled_agent_is_installed(env, monkeypatch, tmp_path):
    """A failed `ttp notifier test` on a box with no agent (headless, no notify-send) raises no alert; an
    enabled agent's failure does, and disabling or removing the agent clears it."""
    from ttp import alerts, notifier
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    monkeypatch.setattr(notifier.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    _notifier_stubs(monkeypatch, tmp_path, [])   # no notify-send
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.delenv("TTP_TEST_NO_NOTIFIER", raising=False)
    ok, lines = notifier.test()
    assert not ok and notifier.delivery()["ok"] is False
    assert notifier.failing() is None and notifier.status_lines() == []
    d.check_notifier()
    assert not p.db.one("SELECT id FROM messages WHERE ref='notifier'"), "no agent here: nothing to alert"
    state = _agent(monkeypatch, tmp_path)
    d.check_notifier()
    assert p.db.one("SELECT id FROM messages WHERE ref='notifier'"), "an enabled agent that cannot deliver alerts"
    state["on"] = False   # `systemctl --user disable --now` or `launchctl bootout` by hand
    assert notifier.failing() is None and notifier.status_lines() == []
    (ep,) = alerts.sweep(p.db)
    assert ep["key"] == notifier.ALERT_KEY
    state["on"] = True
    assert notifier.uninstall() == "removed"
    assert notifier.agent_file() is None and notifier.delivery() == {} and notifier.failing() is None


def test_the_no_desktop_session_fix_points_at_ttp_notifier_uninstall(monkeypatch, tmp_path):
    from ttp import notifier
    steps = " ".join(notifier.fix_steps({"backend": "notify-send", "error": "no desktop session (no DISPLAY)"}))
    assert "`ttp notifier uninstall`" in steps and "systemctl" not in steps


@pytest.mark.parametrize("always", [False, True])
def test_setup_and_upgrade_never_restart_a_disabled_or_removed_notifier(env, monkeypatch, tmp_path, always):
    """refresh() restarts only an agent the user keeps enabled: install() would enable a disabled one."""
    from ttp import notifier
    calls = _fake_agent(monkeypatch, tmp_path, None, on=False)
    assert notifier.refresh(send_test=False, always=always) == [] and calls == []
    assert notifier.status_lines() == []
    assert notifier.refresh(send_test=True, always=always) == [] and calls == []
    notifier.uninstall()
    assert notifier.refresh(send_test=False, always=always) == [] and calls == []


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_ttp_notifier_uninstall_stops_and_removes_the_agent_and_its_records(env, monkeypatch, tmp_path, capsys,
                                                                            platform):
    from ttp import cli, notifier
    argv = _notifier_stubs(monkeypatch, tmp_path, ["systemctl", "launchctl"])
    monkeypatch.setattr(notifier.sys, "platform", platform)
    _agent(monkeypatch, tmp_path)
    notifier.write_json(notifier.DELIVERY, {"ok": False, "backend": "notify-send", "error": "x", "ts": 1})
    notifier.write_json(notifier.RUNNING, {"pid": os.getpid(), "code": "0.0.1 (abc)"})
    assert notifier.status_lines()
    cli.main(["notifier", "uninstall"])
    assert capsys.readouterr().out.strip() == "removed"
    if platform == "linux":
        assert argv("systemctl") == [b"--user", b"daemon-reload"]   # the last call, after `disable --now`
    else:
        assert argv("launchctl")[0] == b"bootout"
    assert notifier.agent_file() is None
    assert not notifier.DELIVERY.exists() and not notifier.RUNNING.exists()
    assert notifier.status_lines() == [] and notifier.failing() is None
    cli.main(["notifier", "uninstall"])
    assert "no desktop notifier installed here" in capsys.readouterr().out


def test_the_notifier_module_stays_python_3_9_syntax():
    """The agent may run under macOS's system Python 3.9."""
    import ast
    src = (RUNTIME / "ttp" / "notifier.py").read_text()
    ast.parse(src, feature_version=(3, 9))
    assert "from __future__ import annotations" in src, "X | None annotations need it before 3.10"


def test_the_desktop_notifier_never_lets_a_later_alert_replace_an_earlier_one(monkeypatch, tmp_path):
    from ttp import notifier
    monkeypatch.setattr(notifier.sys, "platform", "darwin")
    argv = _notifier_stubs(monkeypatch, tmp_path, ["terminal-notifier"])
    notifier.show("tt-project · a", "alert one", url="https://example.invalid/x")
    got = argv("terminal-notifier")
    assert b"-group" not in got and got[got.index(b"-message") + 1] == b"alert one"


@pytest.mark.parametrize("bare", ["_refresh_meters", "update_gates", "coord.expire_asks", "scr.expire_mutes",
                                  "check_mute_ends", "sweep_watcher_issues", "review_jev", "settling"])
def test_a_failing_call_between_the_tick_step_loops_does_not_stop_dispatch_or_delivery(env, monkeypatch, bare):
    """The calls between the two step loops run guarded like the steps: one that raises is logged
    with its name and the second loop (dispatch, deliver_outbound) still runs in that tick."""
    from ttp import daemon as dm
    p = make(env)
    calls: list[str] = []
    d = _failing_step_daemon(p.base, calls)

    def broken(*a, **k):
        raise RuntimeError(f"{bare} broke")
    mod, _, attr = bare.rpartition(".")
    broken.__name__ = attr   # as the daemon's own method would be named
    if mod:
        monkeypatch.setattr(getattr(dm, mod), attr, broken)
    else:
        setattr(d, attr, broken)
    d.tick()
    assert "dispatch" in calls and "deliver" in calls
    logged = (p.logs / "daemon.log").read_text()
    assert f"tick step {attr} error:" in logged
    assert f"RuntimeError: {bare} broke" in logged


@pytest.mark.parametrize("bare", ["net_held", "_net_may_probe", "host_cycle_held"])
def test_a_failing_hold_check_does_not_stop_dispatch_or_delivery(env, bare):
    """net_held, _net_may_probe and host_cycle_held run guarded with a False default: one that raises
    is logged and dispatch and deliver_outbound still run in that tick."""
    p = make(env)
    calls: list[str] = []
    d = _failing_step_daemon(p.base, calls)
    if bare == "_net_may_probe":
        d.net_held = lambda prov: True   # so that the probe check is reached

    def broken(*a, **k):
        raise RuntimeError(f"{bare} broke")
    setattr(d, bare, broken)
    d.tick()
    assert "dispatch" in calls and "deliver" in calls
    assert f"RuntimeError: {bare} broke" in (p.logs / "daemon.log").read_text()


def test_a_failing_update_gates_still_lets_dispatch_and_delivery_run_and_is_alerted(env):
    """update_gates raising every tick: dispatch and deliver_outbound still run each tick, and after
    STEP_FAILS_ALERT ticks it is alerted as tick-step:update_gates like any step."""
    from ttp import daemon as dm
    p = make(env)
    calls: list[str] = []
    d = _failing_step_daemon(p.base, calls)

    def update_gates():
        raise RuntimeError("gates broke")
    d.update_gates = update_gates
    for _ in range(dm.STEP_FAILS_ALERT):
        calls.clear()
        d.tick()
        assert "dispatch" in calls and "deliver" in calls
    assert "tick step update_gates error:" in (p.logs / "daemon.log").read_text()
    assert p.db.one("SELECT id FROM alerts WHERE key='tick-step:update_gates' AND cleared IS NULL")


def test_a_failing_delivery_while_paused_is_logged_not_raised(env):
    """While the project is paused the tick only delivers outbound messages: a delivery that raises
    is logged and counted as the step, not thrown out of the tick."""
    from ttp.daemon import Daemon
    p = make(env)
    p.db.set_kv("paused", True)
    d = Daemon(p.base)

    def deliver_outbound():
        raise RuntimeError("slack down")
    d.deliver_outbound = deliver_outbound
    d.tick()
    assert "tick step deliver_outbound error:" in (p.logs / "daemon.log").read_text()
    assert d._step_fails["deliver_outbound"][0] == 1


def test_a_pinned_watcher_line_keeps_its_severity_through_jev_and_mutes(env, monkeypatch):
    """"pin_severity": true keeps a command watcher line's severity: Jev is not asked (it could only
    lower it) and no mute covers it; an unpinned line is still lowered and muted as before."""
    p = make(env)
    from ttp import daemon as dm
    from ttp import screen as scr
    p.set_config("jev.uses.screen", "on")
    out = {"stdout": ""}
    monkeypatch.setattr(dm.schedrun, "run_group", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out["stdout"], ""))
    d = dm.Daemon(p.base)
    d.jev = quiet = _FakeJev(0.1, 0.0)   # rates everything routine: would lower any line to info

    def run(text, severity, key, pin):
        out["stdout"] = json.dumps({"text": text, "severity": severity, "key": key,
                                    **({"pin_severity": True} if pin else {})})
        d._run_command_watcher({"name": "hw"}, {"command": "x"})

    def wake(key):
        fp = scr.key_fingerprint("watcher:hw", key)
        return p.db.one("SELECT severity FROM events WHERE kind='observation' AND fingerprint=?", (fp,))

    def issue(key):
        return p.db.one("SELECT severity, status, screen FROM issues WHERE fingerprint=?",
                        (scr.key_fingerprint("watcher:hw", key),))

    run("box-a: down for 2.5h", "critical", "box-a down", pin=False)
    assert quiet.calls == 1 and issue("box-a down")["severity"] == "info" and wake("box-a down") is None
    run("box-b: down for 2.5h", "critical", "box-b down", pin=True)
    assert quiet.calls == 1, "a pinned line costs no Jev call"
    i = issue("box-b down")
    assert (i["severity"], i["status"]) == ("critical", "open") and "pinned severity critical" in i["screen"]
    assert wake("box-b down")["severity"] == "critical"
    # A mute that covers the line silences the unpinned one only.
    scr.mute(p.db, "watcher:hw", "link flapping", 4, "critical")
    run("box-c: link flapping", "high", "box-c link", pin=False)
    assert quiet.calls == 2 and wake("box-c link") is None
    d.jev = None
    run("box-d: link flapping", "high", "box-d link", pin=False)
    assert wake("box-d link") is None and issue("box-d link")["severity"] == "high", "muted, not lowered"
    run("box-e: link flapping", "high", "box-e link", pin=True)
    assert wake("box-e link")["severity"] == "high"
    # An issue Jev lowered before the line was pinned is raised back to the pinned severity and wakes.
    d.jev = quiet
    run("box-a: down for 3h", "critical", "box-a down", pin=True)
    assert issue("box-a down")["severity"] == "critical" and wake("box-a down")["severity"] == "critical"
    # pin_severity needs a severity: without one it changes nothing (Jev judges as before).
    calls = quiet.calls
    out["stdout"] = json.dumps({"text": "box-f: odd output", "key": "box-f odd", "pin_severity": True})
    d._run_command_watcher({"name": "hw"}, {"command": "x"})
    assert quiet.calls == calls + 1 and issue("box-f odd")["severity"] == "info"


def test_a_stepped_alert_re_raises_by_severity_or_by_episode_as_documented(env, monkeypatch):
    """A keyed alert that steps up after quiet runs (references/watchers.md, stepped alerts): a raised
    severity reopens and wakes; a step at the same severity wakes only with an episode of its own."""
    p = make(env)
    from ttp import daemon as dm
    from ttp import screen as scr
    out = {"stdout": ""}
    monkeypatch.setattr(dm.schedrun, "run_group", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out["stdout"], ""))
    d = dm.Daemon(p.base)

    def run(text="", severity="high", key="box-a down", episode=None):
        obs = {"text": text, "severity": severity, "key": key, **({"episode": episode} if episode else {})}
        out["stdout"] = json.dumps(obs) if text else ""
        d._run_command_watcher({"name": "hw"}, {"command": "x"})

    def wakes():
        return p.db.one("SELECT COUNT(*) AS n FROM events WHERE kind='observation'")["n"]

    run("box-a: down for 0.5h", "normal")
    assert wakes() == 1
    run()   # a quiet run closes it
    run("box-a: down for 1h", "high")   # a higher step: reopens and wakes
    assert wakes() == 2
    run()
    run("box-a: down for 2h", "high")   # the same severity within the reraise window: counted only
    assert wakes() == 2
    # The documented pattern: one episode per step (here the step's threshold), so each step wakes once.
    for step in ("1h", "2h", "4h"):
        run()
        run(f"box-a: down for over {step}", "high", key="box-b down", episode=f"down>{step}")
        run(f"box-a: down for over {step}", "high", key="box-b down", episode=f"down>{step}")
    assert wakes() == 5
    assert len(p.db.q("SELECT id FROM issues WHERE fingerprint LIKE ?",
                      (scr.key_fingerprint("watcher:hw", "box-b down") + "%",))) == 3


def _mac_app_stubs(monkeypatch, tmp_path, tools=("osacompile", "codesign", "open", "osascript")):
    """macOS on Linux: a temp HOME and stub osacompile (writes an applet's Info.plist, or like the real one a
    script file when the output name does not end in .app), codesign, open and
    osascript, each logging its argv as one JSON line to <tool>.calls."""
    from ttp import notifier
    _notifier_stubs(monkeypatch, tmp_path, [])
    monkeypatch.setattr(notifier.sys, "platform", "darwin")
    monkeypatch.setattr(notifier, "LOG_SETTLE_S", 0.3)   # the usernoted poll after a first post
    monkeypatch.setattr(notifier, "LOG_POLL_S", 0.05)
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setattr(notifier, "CONF", tmp_path / "notifier.json")
    monkeypatch.setattr(notifier, "CLICK_URL", tmp_path / "notifier-click-url")
    for tool in tools:
        stub = tmp_path / "bin" / tool
        stub.write_text(f"#!{sys.executable}\nimport json, plistlib, sys, pathlib\n"
                        f"open({str(tmp_path / (tool + '.calls'))!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                        "if pathlib.Path(sys.argv[0]).name == 'osacompile' and not sys.argv[2].endswith('.app'):\n"
                        "    pathlib.Path(sys.argv[2]).write_bytes(b'compiled script')  # like osacompile: no .app, no applet\n"
                        "elif pathlib.Path(sys.argv[0]).name == 'osacompile':\n"
                        "    c = pathlib.Path(sys.argv[2]) / 'Contents'\n"
                        "    c.mkdir(parents=True)\n"
                        "    (c / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.apple.ScriptEditor.id.x',"
                        " 'CFBundleExecutable': 'applet'}))\n")
        stub.chmod(0o755)
    return lambda tool: [json.loads(ln) for ln in (tmp_path / f"{tool}.calls").read_text().splitlines()] \
        if (tmp_path / f"{tool}.calls").exists() else []


def test_the_macos_notifier_app_asks_for_persistent_alerts_is_ad_hoc_signed_and_built_once(monkeypatch, tmp_path):
    import plistlib
    from ttp import notifier
    calls = _mac_app_stubs(monkeypatch, tmp_path)
    stray = tmp_path / "userhome" / "Applications" / ".tt-project.app.new"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"compiled script")   # what an older build left: a script file, not an app
    app, line = notifier.ensure_app()
    assert app == tmp_path / "userhome" / "Applications" / "tt-project.app" and "persistent alerts requested" in line
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "com.tt-project.notifier" and info["CFBundleName"] == "tt-project"
    assert info["NSUserNotificationAlertStyle"] == "alert", "macOS's default style for the app: stays until closed"
    assert info["LSBackgroundOnly"] is True and "LSUIElement" not in info and info["CFBundleExecutable"] == "applet"
    assert (app / "Contents" / "Resources" / "applet.icns").read_bytes()[:4] == b"icns"
    (compiled,) = calls("osacompile")
    assert compiled[0] == "-o" and compiled[1].endswith("/.new-tt-project.app")
    (signed,) = calls("codesign")
    assert signed[:4] == ["--force", "--deep", "--sign", "-"] and signed[4] == compiled[1], "signed after the plist edit"
    assert notifier.ensure_app() == (app, ""), "a rerun leaves the same app alone"
    assert len(calls("osacompile")) == 1 and len(calls("codesign")) == 1
    assert [p.name for p in app.parent.iterdir()] == ["tt-project.app"], "no build leftovers"
    notifier.CONF.write_text('{"persistent": false}')
    assert "banner alerts requested" in notifier.ensure_app()[1], "the one switch rebuilds it"
    assert plistlib.loads((app / "Contents" / "Info.plist").read_bytes())["NSUserNotificationAlertStyle"] == "banner"


def _ncprefs(tmp_path, flags=None):
    """macOS's Notifications settings as the app reads them; flags None: the app is not listed."""
    import plistlib
    prefs = tmp_path / "userhome" / "Library" / "Preferences" / "com.apple.ncprefs.plist"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    apps = [{"bundle-id": "com.example.other", "flags": 16}]
    if flags is not None:
        apps.append({"bundle-id": "com.tt-project.notifier", "flags": flags})
    prefs.write_bytes(plistlib.dumps({"apps": apps}, fmt=plistlib.FMT_BINARY))
    return prefs


def test_macos_alerts_keep_their_sender_until_macos_lists_the_app_and_with_the_opt_out(monkeypatch, tmp_path):
    from ttp import notifier
    calls = _mac_app_stubs(monkeypatch, tmp_path)
    assert notifier.ensure_app()[0], "the app is built"
    for prefs in (None, "unreadable", "not listed"):   # never proven registered: the sender of today
        if prefs == "not listed":
            _ncprefs(tmp_path)
        elif prefs == "unreadable":
            p = tmp_path / "userhome" / "Library" / "Preferences" / "com.apple.ncprefs.plist"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"not a plist")
        assert notifier.show("t", "b")["backend"] == "osascript" and not calls("open"), prefs
    _ncprefs(tmp_path, 16)
    assert notifier.show("t", "b")["backend"] == "tt-project"
    notifier.CONF.write_text('{"app": false}')
    n = len(calls("open"))
    assert notifier.show("t", "b")["backend"] == "osascript" and len(calls("open")) == n, "opted out: never the app"
    shutil.rmtree(notifier.app_path())
    assert notifier.ensure_app() == (None, "") and not notifier.app_path().exists(), "opted out: never built again"
    assert notifier.register_app() == []


def test_an_app_test_that_shows_nothing_names_the_opt_out_and_the_other_senders(monkeypatch, tmp_path):
    from ttp import notifier
    calls = _mac_app_stubs(monkeypatch, tmp_path)
    notifier.ensure_app()
    _ncprefs(tmp_path, 16)
    ok, lines = notifier.test(ask=lambda q: False)
    assert not ok and lines[0] == "test notification not shown (tt-project): macOS took it but did not show it"
    text = "\n".join(lines)
    assert '"app": false in ~/.tt-project/notifier.json' in text and "terminal-notifier" in text and "osascript" in text
    assert len(calls("open")) == 1, "a listed app gets no first-notification ping"


def test_macos_alerts_go_through_the_tt_project_app_and_a_click_opens_the_link(monkeypatch, tmp_path):
    from ttp import notifier
    calls = _mac_app_stubs(monkeypatch, tmp_path)
    app = notifier.ensure_app()[0]
    _ncprefs(tmp_path, 16)   # macOS lists it: only then do alerts go through it
    res = notifier.show("tt-project · a", "build → done", url="http://127.0.0.1:8765/?t=x")
    assert res["ok"] and res["backend"] == "tt-project" and not res["confirmed"]
    assert calls("open") == [["-g", "-n", "-a", str(app), "--args", "--ttp-notify", "tt-project · a", "build → done",
                              "Glass"]] and not calls("osascript"), "not Script Editor's osascript"
    assert notifier.CLICK_URL.read_text() == "http://127.0.0.1:8765/?t=x"
    script = notifier.APP_SCRIPT
    assert "display notification (item (i + 2) of argv)" in script and "open location u" in script


def test_macos_falls_back_to_osascript_and_names_the_setting_when_the_app_cannot_be_built(monkeypatch, tmp_path):
    from ttp import notifier
    calls = _mac_app_stubs(monkeypatch, tmp_path, tools=("osascript", "open"))   # no osacompile, no codesign
    ok, lines = notifier.test()
    assert ok and calls("osascript") and not calls("open")
    assert lines[0] == "notifier app not built: osacompile and codesign not found"
    assert ("alerts come from Script Editor and may go away by themselves: System Settings > Notifications > "
            "Script Editor > alert style Persistent (older macOS: Alerts)") in lines
    notifier.CONF.write_text('{"persistent": false}')
    assert not [ln for ln in notifier.test()[1] if "alert style Persistent" in ln], "no hint once persistence is off"


@pytest.mark.parametrize("flags,says,hint", [
    (16 | 0x2000000, "alert style: persistent (stays until closed)", False),
    (8, "alert style: banners;", True),
    (0, "alert style: none;", True),
    (None, "The tt-project app is not in System Settings > Notifications yet, so alerts come from osascript", True),
])
def test_ttp_notifier_test_reads_the_macos_alert_style_and_hints_only_when_alerts_go_away(
        monkeypatch, tmp_path, capsys, flags, says, hint):
    import plistlib
    from ttp import cli, notifier
    calls = _mac_app_stubs(monkeypatch, tmp_path)
    prefs = _ncprefs(tmp_path, flags)
    before = prefs.read_bytes()
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    cli.main(["notifier", "test"])
    out = capsys.readouterr().out
    sender = "osascript" if flags is None else "tt-project"
    assert f"test notification sent ({sender})" in out and says in out, out
    if flags is None:   # not listed yet: one ping through the app brings macOS's Allow prompt
        assert "the tt-project app posted its first notification" in out and '"app": false' in out
        assert [c[-4:-1] for c in calls("open")] == [["--ttp-notify", "tt-project",
                                                      "tt-project alerts come from this app once you allow it."]]
    assert ("System Settings > Notifications > tt-project > Allow notifications on, alert style Persistent" in out) == hint
    assert prefs.read_bytes() == before, "macOS's notification settings are only read"
    assert "self-check: " in notifier.LOG.read_text() and says.rstrip(";") in notifier.LOG.read_text(), \
        "the log keeps what the check found"


@pytest.mark.parametrize("persist,urgency,expiry", [(None, b"critical", True), (False, b"normal", False)])
def test_linux_desktop_alerts_stay_until_dismissed_by_default(monkeypatch, tmp_path, persist, urgency, expiry):
    from ttp import notifier
    monkeypatch.setattr(notifier.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    argv = _notifier_stubs(monkeypatch, tmp_path, ["notify-send"])
    monkeypatch.setattr(notifier, "CONF", tmp_path / "notifier.json")
    if persist is not None:
        notifier.CONF.write_text(json.dumps({"persistent": persist}))
    notifier.show("t", "b", severity="high")
    got = argv("notify-send")
    assert got[got.index(b"-u") + 1] == urgency and ((b"-t" in got and got[got.index(b"-t") + 1] == b"0") == expiry)
    notifier.show("t", "b", severity="critical")
    assert argv("notify-send")[argv("notify-send").index(b"-u") + 1] == b"critical", "critical stays critical"


def test_setup_builds_the_macos_app_and_sends_its_first_test_even_on_a_tested_code_version(env, monkeypatch, tmp_path):
    from ttp import notifier
    calls = _mac_app_stubs(monkeypatch, tmp_path)
    monkeypatch.delenv("TTP_TEST_NO_NOTIFIER")
    agent = tmp_path / "userhome" / "Library" / "LaunchAgents" / f"{notifier.LABEL}.plist"
    agent.parent.mkdir(parents=True)
    agent.write_text("<plist/>")
    monkeypatch.setattr(notifier, "install", lambda: "installed and started (test)")
    notifier.write_json(notifier.DELIVERY, {"tested": notifier.installed_code()})
    monkeypatch.setattr(notifier, "_service_on", lambda: False)
    assert notifier.refresh(send_test=True) == [] and not calls("osacompile"), "an agent turned off: no app, no test"
    monkeypatch.setattr(notifier, "_service_on", lambda: True)
    out = notifier.refresh(send_test=True)   # current macOS: no ncprefs file, `defaults` gives nothing either
    assert out[0].startswith("desktop notifier: notifier app ") and len(calls("open")) == 1, out
    assert any("the tt-project app posted its first notification" in ln for ln in out), "the Allow prompt"
    assert any("test notification sent (osascript)" in ln for ln in out) and len(calls("osascript")) == 1, \
        "unreadable settings never prove the app registered: the test keeps the sender"
    step = [ln for ln in out if "System Settings > Notifications > tt-project > Allow notifications on, alert style "
            "Persistent" in ln]
    assert len(step) == 1 and step[0].startswith("The tt-project app is not in System Settings") and \
        "macOS lets no program set another app's alert style" in step[0], "the one line naming what only the user can set"
    out = notifier.refresh(send_test=True)
    assert len(calls("open")) == 1 and len(calls("osacompile")) == 1 and "already went out" in out[-1], \
        "a rerun builds and sends nothing more"
    assert not [ln for ln in out if "alerts still come from" in ln], "the line prints once per setup run"
    assert notifier.show("t", "b")["backend"] == "osascript" and len(calls("open")) == 1, "alerts keep the sender"


def test_macos_notification_settings_are_read_through_defaults_when_the_plist_file_is_gone(monkeypatch, tmp_path):
    import plistlib
    from ttp import notifier
    calls = _mac_app_stubs(monkeypatch, tmp_path)
    notifier.ensure_app()
    prefs = tmp_path / "ncprefs.out"
    stub = tmp_path / "bin" / "defaults"
    stub.write_text(f"#!{sys.executable}\nimport sys\nopen({str(tmp_path / 'defaults.calls')!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                    f"sys.stdout.buffer.write(open({str(prefs)!r}, 'rb').read())\n")
    stub.chmod(0o755)
    for flags, style, sender in ((None, "not registered", "osascript"), (8, "banners", "tt-project"),
                                 (16, "persistent", "tt-project")):
        apps = [{"bundle-id": "com.example.other", "flags": 16}] if flags is None else \
            [{"bundle-id": "com.tt-project.notifier", "flags": flags}]
        prefs.write_bytes(plistlib.dumps({"apps": apps}))
        assert notifier.alert_style() == style and notifier.show("t", "b")["backend"] == sender, flags
    assert set((tmp_path / "defaults.calls").read_text().splitlines()) == {"export com.apple.ncprefs -"}, "read only"
    prefs.write_bytes(plistlib.dumps({"apps": []}))
    assert notifier.register_app()[0].startswith("the tt-project app posted its first notification")
    assert len(calls("open")) == 3 and notifier.show("t", "b")["backend"] == "osascript", \
        "readable settings that do not list the app keep the sender, even after its first post"
    prefs.write_bytes(plistlib.dumps({}))   # an export with no apps key at all (current macOS): says nothing
    assert notifier.alert_style() == "unknown" and notifier.show("t", "b")["backend"] == "osascript"
    assert notifier.register_app()[0].startswith("the tt-project app posted its first notification")
    assert len(calls("open")) == 4 and notifier.show("t", "b")["backend"] == "osascript", "never switches senders"


@pytest.mark.parametrize("answer", ["fails", "times out", "empty"])
def test_a_failed_timed_out_or_empty_defaults_export_never_switches_senders(monkeypatch, tmp_path, answer):
    import subprocess
    from ttp import notifier
    calls = _mac_app_stubs(monkeypatch, tmp_path)
    assert notifier.ensure_app()[0]
    stub = tmp_path / "bin" / "defaults"
    stub.write_text(f"#!{sys.executable}\nimport sys\nsys.exit({1 if answer == 'fails' else 0})\n")
    stub.chmod(0o755)
    run = subprocess.run

    def fake_run(args, *a, **kw):
        if answer == "times out" and args[0] == "defaults":
            raise subprocess.TimeoutExpired(args, kw.get("timeout"))
        return run(args, *a, **kw)
    monkeypatch.setattr(notifier.subprocess, "run", fake_run)
    for _ in range(2):   # before and after the app's first post
        assert notifier.alert_style() == "unknown" and not notifier.app_registered()
        assert notifier.show("t", "b")["backend"] == "osascript", answer
        notifier.register_app()
    assert len(calls("open")) == 2 and len(calls("osascript")) == 2 and not notifier.delivery().get("app_allowed")


def _usernoted_log(tmp_path, text=None, rc=0, after=0):
    """A stub `log` that prints `text` (usernoted's lines) from its read number `after` + 1 on (nothing before:
    the record is not there yet) and logs its argv to log.calls; None: no `log`."""
    stub = tmp_path / "bin" / "log"
    if text is None:
        stub.unlink(missing_ok=True)
        return
    calls = str(tmp_path / 'log.calls')
    stub.write_text(f"#!{sys.executable}\nimport json, sys\n"
                    f"open({calls!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    f"if len(open({calls!r}).read().splitlines()) > {after}:\n    sys.stdout.write({text!r})\n"
                    f"sys.exit({rc})\n")
    stub.chmod(0o755)


_TS = "2026-01-01 09:00:00.123 Df usernoted[512:1a2b] [com.apple.unc:application]"   # `log show --style compact`


@pytest.mark.parametrize("text,rc,switches", [
    ('usernoted: Delivering <NotificationRecord app:"com.tt-project.notifier" ident:"1">\n', 0, True),
    ('usernoted: Presenting <NotificationRecord app:"com.tt-project.notifier" ident:"1">\n', 0, True),
    ('usernoted: Delivering <NotificationRecord app:"com.tt-project.notifier">\n'
     'usernoted: com.tt-project.notifier: authorization denied\n', 0, False),
    ("", 0, False),   # nothing in the log
    # real compact records, one spanning lines: the bundle is on a continuation line
    (f'Timestamp               Ty Process[PID:TID]\n{_TS} Delivering <NotificationRecord app:"com.tt-project.notifier" '
     'ident:"1" req:"A1" uuid:"B2" priority:2(high) interrupt:1 actions:0>\n', 0, True),
    (f'Timestamp               Ty Process[PID:TID]\n{_TS} Presenting <NotificationRecord\n'
     '    app:"com.tt-project.notifier" ident:"1" req:"A1" uuid:"B2"\n    priority:2(high) interrupt:1 actions:0>\n', 0, True),
    # held back or off: never allowed, whatever else the log says
    (f'{_TS} Not presenting <NotificationRecord app:"com.tt-project.notifier" ident:"1">: muted by display state\n',
     0, False),
    (f'{_TS} Not presenting <NotificationRecord app:"com.tt-project.notifier" ident:"1">: suppressed by Focus\n',
     0, False),
    (f'{_TS} Not delivering <NotificationRecord app:"com.tt-project.notifier" ident:"1">: notifications disabled\n',
     0, False),
    (f'{_TS} Not delivering <NotificationRecord\n    app:"com.tt-project.notifier" ident:"1">: notifications disabled\n',
     0, False),
    (f'{_TS} Delivering <NotificationRecord app:"com.tt-project.notifier" ident:"1">\n'
     f'{_TS} Not presenting <NotificationRecord app:"com.tt-project.notifier" ident:"1">: suppressed by Focus\n', 0, False),
    (f'{_TS} Withdrawing <NotificationRecord app:"com.tt-project.notifier" ident:"1">\n', 0, False),   # unknown
    ('usernoted: Delivering <NotificationRecord app:"com.tt-project.notifier">\n', 1, False),   # log show fails
    (f'{_TS} Error delivering <NotificationRecord app:"com.tt-project.notifier" ident:"1">\n', 0, False),
    (f'{_TS} Will not be delivering <NotificationRecord app:"com.tt-project.notifier" ident:"1">\n', 0, False),
    (None, 0, False),   # no `log` at all
])
def test_where_macos_settings_list_no_apps_usernoted_log_decides_the_app_is_allowed(monkeypatch, tmp_path,
                                                                                      text, rc, switches):
    from ttp import notifier
    calls = _mac_app_stubs(monkeypatch, tmp_path)   # no ncprefs file and no `defaults`: settings say nothing
    _usernoted_log(tmp_path, text, rc)
    ok, lines = notifier.test()
    sender = "tt-project" if switches else "osascript"
    assert ok and calls("open")[0][-2] == "tt-project alerts come from this app once you allow it.", "its first post"
    assert f"test notification sent ({sender}); macOS does not say whether it was shown" in lines, lines
    assert len(calls("open")) == (2 if switches else 1) and bool(calls("osascript")) != switches
    step = [ln for ln in lines if ln.startswith("alerts still come from Script Editor")]
    assert not step, "the pending app's step prints once, from fix_steps"
    pend = [ln for ln in lines if "is not in System Settings > Notifications yet" in ln]
    assert len(pend) == (0 if switches else 1), lines
    assert any(ln.startswith("alert style: macOS does not say here") for ln in lines) == switches
    assert notifier.show("t", "b")["backend"] == ("tt-project" if switches else "osascript")
    if text is not None:
        argv = json.loads((tmp_path / "log.calls").read_text().splitlines()[0])
        assert argv[:2] == ["show", "--style"] and "--start" in argv and \
            argv[-1] == 'process == "usernoted" AND eventMessage CONTAINS "com.tt-project.notifier"', "read only"
    n = len((tmp_path / "log.calls").read_text().splitlines()) if text is not None else 0
    notifier.show("t", "b")
    m = len((tmp_path / "log.calls").read_text().splitlines()) if text is not None else 0
    assert m == n, "an alert re-reads the log at most every LOG_CHECK_S, and never once it showed the app allowed"


@pytest.mark.parametrize("text,switches", [
    (f'{_TS} Delivering <NotificationRecord app:"com.tt-project.notifier" ident:"1">\n', True),
    ("", False),
])
def test_setup_polls_usernoted_log_after_the_first_post_and_never_backs_off_on_an_early_read(monkeypatch, tmp_path,
                                                                                            text, switches):
    from ttp import notifier
    _mac_app_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(notifier, "LOG_SETTLE_S", 1.5)
    monkeypatch.setattr(notifier, "LOG_POLL_S", 0.5)
    # A test clock only the notifier reads, moved only by its sleeps: the read count decides, not load.
    clock = {"t": float(int(time.time()))}

    class _Clock:
        def __getattr__(self, name):
            return getattr(time, name)

        def time(self):
            return clock["t"]

        def sleep(self, s):
            clock["t"] += s
    monkeypatch.setattr(notifier, "time", _Clock())
    _usernoted_log(tmp_path, text, after=2)   # the record shows only from the third read on (usernoted is slower)
    t0 = notifier.time.time()
    ok, lines = notifier.test()
    reads = len((tmp_path / "log.calls").read_text().splitlines())
    assert ok and "test notification sent (osascript); macOS does not say whether it was shown" in lines, lines
    assert ("macOS shows the tt-project app allowed: alerts come from it from now on" in lines) == switches
    d = notifier.delivery()
    assert reads >= 3 and bool(d.get("app_allowed")) == switches
    assert notifier.show("t", "b")["backend"] == ("tt-project" if switches else "osascript")
    if switches:
        assert notifier.time.time() - t0 < notifier.LOG_SETTLE_S, "a decisive record ends the poll early"
    else:
        assert d["app_log_checked"] >= d["app_posted"] + notifier.LOG_SETTLE_S, "only a read after the window backs off"


def test_an_alert_read_of_usernoted_log_before_the_window_ends_sets_no_back_off(monkeypatch, tmp_path):
    from ttp import notifier
    _mac_app_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(notifier, "LOG_SETTLE_S", 3600)
    notifier.ensure_app()
    _usernoted_log(tmp_path, f'{_TS} Delivering <NotificationRecord app:"com.tt-project.notifier" ident:"1">\n', after=1)
    notifier.register_app()
    assert notifier.show("t", "b")["backend"] == "osascript" and not notifier.delivery().get("app_log_checked")
    assert notifier.show("t", "b")["backend"] == "tt-project", "the next alert reads again, no LOG_CHECK_S back-off"


def test_the_app_allowance_in_the_log_is_looked_for_only_after_its_post_and_listed_apps_decide_first(
        monkeypatch, tmp_path):
    from ttp import notifier
    calls = _mac_app_stubs(monkeypatch, tmp_path)
    notifier.ensure_app()
    _usernoted_log(tmp_path, 'usernoted: Delivering <NotificationRecord app:"com.tt-project.notifier">\n')
    assert notifier.show("t", "b")["backend"] == "osascript" and not (tmp_path / "log.calls").exists(), \
        "no post through the app yet: nothing to look for"
    _ncprefs(tmp_path)   # settings list apps, not this one: they decide, the log is not read
    notifier.register_app()
    assert notifier.show("t", "b")["backend"] == "osascript" and not (tmp_path / "log.calls").exists()
    (tmp_path / "userhome" / "Library" / "Preferences" / "com.apple.ncprefs.plist").unlink()
    d = notifier.delivery()
    notifier.write_json(notifier.DELIVERY, dict(d, app_posted=d["app_posted"] - notifier.LOG_WINDOW_S - 1))
    assert notifier.show("t", "b")["backend"] == "osascript" and not (tmp_path / "log.calls").exists(), \
        "a post older than LOG_WINDOW_S is not looked for"
    assert len(calls("open")) == 1


@pytest.mark.parametrize("line,says", [
    ("Not presenting <NotificationRecord app:\"com.apple.ScriptEditor2\">: muted by display state",
     "macOS muted the test notification while the screen is shared or mirrored"),
    ("Not presenting <NotificationRecord app:\"com.apple.ScriptEditor2\">: suppressed by Focus",
     "macOS held the test notification back for a Focus mode"),
    ("Presenting <NotificationRecord app:\"com.apple.ScriptEditor2\">", None),
])
def test_ttp_notifier_test_says_when_macos_muted_the_test_for_screen_sharing_or_focus(monkeypatch, tmp_path, line, says):
    from ttp import notifier
    _mac_app_stubs(monkeypatch, tmp_path, tools=("osascript",))   # no app: the test goes through osascript
    _usernoted_log(tmp_path, f"usernoted: {line}\n")
    ok, lines = notifier.test()
    assert ok and [ln for ln in lines if ln.startswith("macOS muted") or ln.startswith("macOS held")] == \
        ([] if says is None else [next(ln for ln in lines if ln.startswith(says))])
    assert json.loads((tmp_path / "log.calls").read_text().splitlines()[0])[-1].endswith(
        'CONTAINS "com.apple.ScriptEditor2"'), "only the test sender's lines"
