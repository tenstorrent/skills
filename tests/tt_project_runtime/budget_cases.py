# Budget tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new budget tests here, not there.
from __future__ import annotations


def test_caps_gate_escalates_and_blocks_new_work(env):
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    assert bud.evaluate(p.db, cfg, "claude", []).level == "green"
    p.db.spend("claude", 65.0, "task:1")
    g = bud.evaluate(p.db, cfg, "claude", [])
    assert g.level in ("yellow", "red")   # 65/100 is yellow; the runaway guard may also fire
    p.db.spend("claude", 40.0, "task:2")
    g = bud.evaluate(p.db, cfg, "claude", [])
    assert g.level == "red" and not g.allow_new_work and g.max_parallel == 0



def test_plan_windows_run_every_worker_below_the_line_and_keep_the_reserve(env):
    """On a plan, unused capacity is lost at the reset: below the line all workers run, however fast
    the window burns; at the line nothing new starts."""
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    now = time.time()
    resets = now + 10 * 3600

    def reading(minutes_ago, util):
        p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
               (now - minutes_ago * 60, "claude", "a", "seven_day", util, resets))

    w = lambda u: [bud.Window("claude", "seven_day", u, resets)]  # noqa: E731
    # no burn measured yet: all slots are open
    g = bud.evaluate(p.db, cfg, "claude", w(50), now)
    assert g.regime == "windows" and g.level == "green" and g.max_parallel == 6
    # 8 points/h from 4 workers would pass 90% by the reset; an even pace cut it, the line does not:
    # each worker adds ~2 points over a 1 h run, and 40 points of headroom fit far more than 6
    reading(60, 42)
    reading(0, 50)
    for i in range(4):
        p.db.x("INSERT INTO runs(role,provider,started,status) VALUES('worker','claude',?,'running')", (now - 3600,))
    g = bud.evaluate(p.db, cfg, "claude", w(50), now)
    row = g.numbers["plan"][0]
    assert g.level == "green" and g.max_parallel == 6 and g.max_tier == "deep", (g.level, g.reasons, g.numbers)
    assert row["per_worker_per_h"] == 2.0 and row["headroom"] == 40.0 and row["running_add"] == 8.0, row
    assert g.numbers["starts"] is True and not g.reasons, g.reasons
    # at the line nothing new starts until the reset
    red = bud.evaluate(p.db, cfg, "claude", w(90), now)
    assert red.level == "red" and not red.allow_new_work and red.max_parallel == 0



def test_all_slots_busy_far_below_the_line_stays_green(env):
    """Six workers busy at 20% burning 1 point/h each: the headroom fits all six, so busy slots are
    not a hold. No orange, no 'no new starts', full parallelism."""
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    now = time.time()
    resets = now + 3 * 3600
    _plan_setup(p, now, resets, [(60, 14), (30, 17), (0, 20)], [(90, None)] * 6)
    g = bud.evaluate(p.db, cfg, "claude", [bud.Window("claude", "five_hour", 20, resets)], now)
    assert g.numbers["plan"][0]["per_worker_per_h"] == 1.0, g.numbers
    assert g.level == "green" and g.max_parallel == 6 and not g.reasons, (g.reasons, g.numbers)
    assert g.numbers["starts"] is True and g.allow_optional, g.numbers



def _plan_setup(p, now, resets, readings, runs):
    """Five-hour window readings (minutes ago, percent) and worker runs (minutes ago started, ended)."""
    for ago, util in readings:
        p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
               (now - ago * 60, "claude", "a", "five_hour", util, resets))
    for start, end in runs:
        p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','claude',?,?,?)",
               (now - start * 60, None if end is None else now - end * 60, "running" if end is None else "done"))



def test_near_the_line_running_work_counts_and_new_starts_stop(env):
    """Three workers burn 9 points/h, 3 each; a run lasts 1 h before any has ended. Each worker may
    add 3 more points: all six fit 20 points under the line, four fit 14, three fit 10 (all busy,
    nothing new starts, still yellow), and once the three running ones alone may pass it, orange."""
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    now = time.time()
    resets = now + 3 * 3600
    _plan_setup(p, now, resets, [(60, 61), (30, 65.5), (0, 70)], [(90, None)] * 3)
    w = lambda u: [bud.Window("claude", "five_hour", u, resets)]  # noqa: E731
    g = bud.evaluate(p.db, cfg, "claude", w(70), now)
    row = g.numbers["plan"][0]
    assert row["per_worker_per_h"] == 3.0 and row["horizon_h"] == 1.0 and row["running_add"] == 9.0, row
    assert g.level == "green" and g.max_parallel == 6, (g.reasons, row)
    # the last stretch: fewer workers, deep work at standard
    g = bud.evaluate(p.db, cfg, "claude", w(76), now)
    assert g.level == "yellow" and g.max_parallel == 4 and g.numbers["starts"], (g.reasons, g.numbers)
    assert "room for 4 workers" in g.reasons[0] and bud.clamp_tier("deep", g) == "standard", g.reasons
    # every slot the headroom allows is busy: no new start, but that is the plan working
    g = bud.evaluate(p.db, cfg, "claude", w(80), now)
    assert g.level == "yellow" and g.max_parallel == 3 and g.numbers["starts"] is False, (g.reasons, g.numbers)
    assert "room for 3 workers" in g.reasons[0] and g.allow_optional, g.reasons
    # the running work alone may reach the line: no new start, running work goes on
    g = bud.evaluate(p.db, cfg, "claude", w(82), now)
    assert g.level == "orange" and g.max_parallel <= 3 and g.numbers["starts"] is False, (g.reasons, g.numbers)
    assert g.allow_new_work and "no new starts" in g.reasons[0], g.reasons
    assert p.db.one("SELECT COUNT(*) n FROM runs WHERE status='running'")["n"] == 3
    # shorter runs here add less each: the same headroom fits all six again
    _plan_setup(p, now, resets, [], [(200, 170)] * 5)
    g = bud.evaluate(p.db, cfg, "claude", w(80), now)
    assert g.numbers["plan"][0]["horizon_h"] == 0.5 and g.level == "green" and g.max_parallel == 6, g.numbers
    # a reset close by caps how much more any run can add to this window
    soon = now + 0.25 * 3600
    _plan_setup(p, now, soon, [(60, 61), (30, 65.5), (0, 70)], [])
    g = bud.evaluate(p.db, cfg, "claude", [bud.Window("claude", "five_hour", 85, soon)], now)
    assert g.numbers["plan"][0]["horizon_h"] == 0.25 and g.level == "green", g.numbers



def test_busy_slots_the_headroom_allows_stay_yellow_and_one_too_many_is_orange(env):
    """15 points under the line, 4.5 points/h per worker over 1 h runs: room for 3. Dispatch filling
    the third slot changes nothing; a fourth running worker (started before the readings) does."""
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    now = time.time()
    resets = now + 3 * 3600
    w = [bud.Window("claude", "five_hour", 75, resets)]
    _plan_setup(p, now, resets, [(60, 66), (30, 70.5), (0, 75)], [(90, None)] * 2)
    two = bud.evaluate(p.db, cfg, "claude", w, now)
    assert two.level == "yellow" and two.max_parallel == 3 and two.numbers["starts"] is True, two.reasons
    _plan_setup(p, now, resets, [], [(0, None)])
    three = bud.evaluate(p.db, cfg, "claude", w, now)
    assert three.level == "yellow" and three.max_parallel == 3 and three.numbers["starts"] is False, three.reasons
    assert three.max_tier == "standard" and three.allow_optional and three.reasons == two.reasons
    _plan_setup(p, now, resets, [], [(0, None)])
    four = bud.evaluate(p.db, cfg, "claude", w, now)
    assert four.numbers["plan"][0]["allowed"] == 3 and four.level == "orange", (four.reasons, four.numbers)
    assert four.numbers["starts"] is False and "the 4 running workers may add ~18.0 points" in four.reasons[0]



def test_burn_from_other_sessions_holds_new_starts_near_the_line(env):
    """Nothing of this project ran while the account burned 12 points/h: a new run may add as much,
    so 5 points under the line nothing new starts, and the reason says why."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    resets = now + 3 * 3600
    _plan_setup(p, now, resets, [(60, 73), (0, 85)], [])
    g = bud.evaluate(p.db, p.config(), "claude", [bud.Window("claude", "five_hour", 85.0, resets)], now)
    assert g.level == "orange" and g.max_parallel == 0 and g.numbers["starts"] is False, (g.reasons, g.numbers)
    assert "one more run may add ~12.0 points" in g.reasons[0], g.reasons



def test_near_the_line_without_a_measured_burn_one_worker_runs(env):
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    now = time.time()
    resets = now + 3 * 3600
    near = bud.evaluate(p.db, cfg, "claude", [bud.Window("claude", "five_hour", 88.5, resets)], now)
    assert near.level == "orange" and near.max_parallel == 1 and near.max_tier == "light", near.numbers
    assert near.numbers["plan"][0]["allowed"] == 1
    at = bud.evaluate(p.db, cfg, "claude", [bud.Window("claude", "five_hour", 90.0, resets)], now)
    assert at.level == "red" and at.max_parallel == 0 and not at.allow_new_work, at.numbers
    assert at.numbers["plan"][0]["allowed"] == 0



def test_a_weekly_window_far_over_an_even_pace_keeps_every_slot(env):
    """A weekly window at 5% three hours after its reset, burning ~1.5 points/h, three times the rate
    that lands 90% at the reset: even pacing ran a third of one worker here. The line is far off."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    resets = now + 165 * 3600
    for ago, util in [(180, 0.5), (120, 2.0), (60, 3.5), (0, 5.0)]:
        p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
               (now - ago * 60, "claude", "a", "seven_day", util, resets))
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','claude',?,?,'done')",
           (now - 180 * 60, now))
    g = bud.evaluate(p.db, p.config(), "claude", [bud.Window("claude", "seven_day", 5.0, resets)], now)
    assert g.level == "green" and g.max_parallel == 6 and g.max_tier == "deep", (g.reasons, g.numbers)
    assert not hasattr(bud, "pace_hold") and "paced" not in g.numbers



def test_a_weekly_window_well_below_the_line_never_throttles(env):
    """Whole-percent readings of a weekly window burning steadily: every reading leaves all slots open."""
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    t0 = time.time()
    resets = t0 + 138 * 3600                       # period started 30 h ago
    p.db.x("INSERT INTO runs(role,provider,started,status) VALUES('worker','claude',?,'running')",
           (t0 - 30 * 3600,))
    gates = []
    for i in range(-30 * 12, 24 * 12):             # a reading every 5 min, 30 h back to 24 h ahead
        ts = t0 + i * 300
        util = float(int(0.56 * (ts - (resets - 168 * 3600)) / 3600))
        p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
               (ts, "claude", "a", "seven_day", util, resets))
        if i >= 0 and i % 6 == 0:
            g = bud.evaluate(p.db, cfg, "claude", [bud.Window("claude", "seven_day", util, resets)], ts)
            gates.append((g.level, g.max_parallel))
    assert set(gates) == {("green", 6)}, gates



def test_the_digest_gives_headroom_to_the_line_and_whether_starts_are_allowed(env):
    p = make(env)
    from ttp import budget as bud
    from ttp import coordinator as coord
    now = time.time()
    resets = now + 3 * 3600
    _plan_setup(p, now, resets, [(60, 61), (30, 65.5), (0, 70)], [(90, None)] * 3)
    for util, starts in ((70.0, "new starts allowed"), (80.0, "no new starts")):
        g = bud.evaluate(p.db, p.config(), "claude", [bud.Window("claude", "five_hour", util, resets)], now)
        d = coord.digest(p, {"claude": g.as_dict()}, [], [])
        line = next(x for x in d.splitlines() if x.startswith("- claude:"))
        assert f"five_hour {util}% used, {90 - util} points to the line" in line, line
        assert "~3.0/h per worker" in line and f" · {starts}" in line, line
        assert "pace" not in line and "needs" not in line, line



def test_near_the_line_no_task_starts_and_status_says_why(env, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    from ttp.web import health
    _no_events(p)
    d = Daemon(p.base)
    prov = d.cfg.get("core_provider", "claude")
    reason = "five_hour window at 85%, 5.0 points under the 90% line; one more run may add ~12.0 points: no new starts"
    gate = bud.Gate(provider=prov, level="orange", regime="windows", max_parallel=0, max_tier="light",
                    allow_optional=False, reasons=[reason],
                    numbers={"limit": 90.0, "running": 0, "starts": False,
                             "plan": [{"window": "five_hour", "utilization": 85.0, "headroom": 5.0}]})
    d.gates = {prov: gate}
    p.db.set_kv("gates", {prov: gate.as_dict()})
    p.db.add_task("user work", "s", kind="work", tier="light", origin="user")
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append(k["task"]["id"]) or 0)
    d.dispatch()
    assert started == [], "the reserve is hard, for the user's own tasks too"
    why = health(p, p.db)["why_idle"]
    assert "no new starts near the plan line: " + reason in why, why



def test_a_plan_stays_a_plan_when_readings_are_old(env):
    """An idle hour must not turn a plan account into a dollar-capped one."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 3 * 3600, "claude", "a", "seven_day", 40.0, now + 3600))
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 6 * 3600, "claude", "a", "five_hour", 70.0, now - 3600))
    wins = {w.window: w for w in bud.plan_windows(p.db, now)}
    assert wins["seven_day"].utilization == 40.0
    assert wins["five_hour"].utilization == 0.0 and wins["five_hour"].resets_at > now, "a reset window is empty"
    assert bud.evaluate(p.db, p.config(), "claude", list(wins.values()), now).regime == "windows"



def test_plan_readings_of_another_plan_account_stop_counting_at_once(env):
    """plan -> plan: the old account's windows leave the gate, pacing and budget line as soon as the
    provider runs under another account, not a week later; the new account's readings take over."""
    p = make(env)
    from ttp import budget as bud
    from ttp.web import budget_line
    now = time.time()
    _old_plan(p, now)
    wins = bud.plan_windows(p.db, now)
    assert {w.window: w.utilization for w in wins} == {"seven_day": 86.0, "five_hour": 10.0}
    assert "7d 86%" in budget_line(p.db, now, "claude") and "7d avg 70%" in budget_line(p.db, now, "claude")
    _run(p, "acct-b | plan", now - 60)                  # the next run starts under another account
    assert bud.plan_windows(p.db, now) == [] and bud.windows_from_snapshots(p.db, now) == []
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and any("no plan readings of its own" in r for r in g.reasons), g.reasons
    assert g.numbers["spent_24h"] == 0 and g.level == "green", "the old plan's spend stays plan-billed"
    line = budget_line(p.db, now, "claude", g.as_dict())
    assert "86%" not in line and "avg" not in line and "this project" in line, line
    assert bud.history(p.db)["window_peaks"] == []
    # the new account reports: its windows count, and burn is measured from its readings alone
    _reading(p, now - 30, "acct-b | plan", "seven_day", 20.0, now + 2 * 86400)
    assert [(w.window, w.utilization) for w in bud.plan_windows(p.db, now)] == [("seven_day", 20.0)]
    assert bud.burn_rate(p.db, "claude", "seven_day", now + 2 * 86400, now, "acct-b | plan") is None
    assert bud.burn_rate(p.db, "claude", "seven_day", now + 2 * 86400, now) is not None, "both accounts mixed"
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "windows" and g.numbers["utilization"] == 20.0, g.numbers
    assert budget_line(p.db, now, "claude", g.as_dict()).startswith("7d 20% - resets in")



def test_a_switch_to_a_usage_billed_account_drops_plan_windows_before_its_first_run(env):
    """plan -> usage-billed: the daemon's look at the login is enough; the gate applies the dollar
    caps to spend after the old plan's last reading, and the budget line shows no plan window."""
    p = make(env)
    from ttp import budget as bud
    from ttp.web import budget_line
    now = time.time()
    _old_plan(p, now)
    bud.note_account(p.db, "claude", "acct-a | org | usage-billed")
    assert bud.current_accounts(p.db) == {"claude": "acct-a | org | usage-billed"}
    assert bud.plan_windows(p.db, now) == []
    p.db.spend("claude", 70.0, "task:2", account="acct-a | org | usage-billed", ts=now - 60)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and g.numbers["spent_24h"] == 70.0 and g.level == "yellow", (g.numbers, g.reasons)
    line = budget_line(p.db, now, "claude", g.as_dict())
    assert line == "24h $70.00 this project, $0.00 of $200 global", "the old plan's spend was never billed: " + line
    # a run that started earlier under the plan account does not bring its windows back
    _run(p, "acct-a | plan", now - 7300)
    assert bud.plan_windows(p.db, now) == []



def test_account_switch_mid_window_counts_only_billed_spend(env):
    """$240 spent on plan account A before the switch was never billed: only B's $15 counts toward
    the daily and weekly caps, the gate and the budget line's 'actual', after A's windows are gone."""
    p = make(env)
    from ttp import budget as bud
    from ttp.web import budget_line
    now = time.time()
    _plan_then_billed(p, now)
    assert round(p.db.spent_since(now - 86400), 2) == 255.0
    assert round(p.db.spent_since(now - 86400, billed=True), 2) == 15.0
    assert bud.plan_windows(p.db, now) == []
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and g.numbers["spent_24h"] == 15.0 and g.numbers["spent_7d"] == 15.0, g.numbers
    assert g.level == "green" and g.allow_new_work, (g.level, g.reasons)
    assert budget_line(p.db, now, "claude", g.as_dict()) == "24h $15.00 this project, $0.00 of $200 global"
    assert budget_line(p.db, now, "claude") == "24h $15.00 actual"
    # B's run still going counts; a run that started under A before the switch does not
    _run(p, "acct-b | billed", now - 300, None, 2.5)
    _run(p, "acct-a | plan", now - 7 * 3600 - 300, None, 7.0)
    assert bud.in_flight(p.db, billed_at=now) == 2.5 and bud.in_flight(p.db) == 9.5
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.numbers["spent_24h"] == 17.5 and g.numbers["in_flight"] == 2.5, g.numbers
    # a long run that started under A and ended after the switch stays plan spend
    _run(p, "acct-a | plan", now - 7 * 3600 - 900, now - 2 * 3600, 50.0)
    p.db.spend("claude", 50.0, "task:4", account="acct-a | plan", ts=now - 2 * 3600)
    assert bud.evaluate(p.db, p.config(), "claude", [], now).numbers["spent_24h"] == 17.5
    # the caps still bite on billed spend: B spends past the $100 daily cap
    p.db.spend("claude", 90.0, "task:2", account="acct-b | billed", ts=now - 60)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.level == "red" and not g.allow_new_work and g.numbers["spent_24h"] == 107.5, g.numbers



def test_readings_without_an_account_count_until_another_account_or_usage_billing_shows(env):
    """Legacy readings recorded before readings were keyed: they still count (an upgrade must not
    drop a live plan), until a keyed reading arrives after them, or until paid runs of the current
    account end after them without any reading (it is billed by use)."""
    p = make(env)
    from ttp import budget as bud
    from ttp.web import budget_line
    now = time.time()
    _reading(p, now - 900, "", "seven_day", 86.0, now + 86400)
    _reading(p, now - 900, "", "five_hour", 30.0, now + 3600)
    _run(p, "acct-c | plan", now - 1200, now - 1000, 1.0)
    assert {w.window for w in bud.plan_windows(p.db, now)} == {"seven_day", "five_hour"}
    assert bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now).regime == "windows"
    assert "7d 86%" in budget_line(p.db, now, "claude")
    # the provider moves to another account and it reports: only keyed readings count from then on
    _run(p, "acct-d | plan", now - 150)
    _reading(p, now - 120, "acct-d | plan", "five_hour", 5.0, now + 3600)
    assert [(w.window, w.utilization) for w in bud.plan_windows(p.db, now)] == [("five_hour", 5.0)]
    assert "86%" not in budget_line(p.db, now, "claude")
    p.db.x("DELETE FROM snapshots WHERE account!=''")
    # no keyed reading, but the current account's paid runs report none: it is billed by use
    _run(p, "acct-c | plan", now - 500, now - 300, 1.0)
    assert {w.window for w in bud.plan_windows(p.db, now)} == {"seven_day", "five_hour"}, "one run is not proof"
    _run(p, "acct-c | plan", now - 250, now - 100, 1.0)
    assert bud.plan_windows(p.db, now) == []
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and "86%" not in budget_line(p.db, now, "claude", g.as_dict())



def test_runaway_guard_trips_on_a_spend_spike(env):
    p = make(env)
    from ttp import budget as bud
    p.set_config("budget.daily_usd", 1000)
    p.set_config("budget.weekly_usd", 5000)
    p.db.spend("claude", 300.0, "task:9")      # $300 in the last hour, ceiling is $250/h
    g = bud.evaluate(p.db, p.config(), "claude", [])
    assert g.level == "red" and any("runaway" in r for r in g.reasons)



def test_productive_burst_is_not_a_runaway_but_waste_is(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.set_config("budget.global_daily_usd", 0)
    wins = []   # billed by use: the waste signal counts dollars (on a plan it counts runs, test below)
    for i, cost in enumerate((4.3, 4.3, 1.5, 0.5, 0.9)):   # a busy first hour of real work
        p.db.x("INSERT INTO runs(role,provider,status,started,ended,cost_usd) VALUES('worker','claude','ok',?,?,?)",
               (now - 3000, now - 60 * i, cost))
        p.db.spend("claude", cost, f"task:{i}")
    assert bud.evaluate(p.db, p.config(), "claude", wins).level == "green"
    for cost in (4.0, 5.0):                                # the same hour, two runs that went nowhere
        p.db.x("INSERT INTO runs(role,provider,status,started,ended,cost_usd) VALUES('worker','claude','stalled',?,?,?)",
               (now - 2000, now - 30, cost))
    g = bud.evaluate(p.db, p.config(), "claude", wins)
    assert g.level == "red" and any("failed or stalled" in r for r in g.reasons)



def test_coordinator_runaway_guard_counts_only_self_started_turns(env):
    """The coordinator $/h guard (billed by use) sums only turns the daemon started on its own (events,
    schedules, idle wakes, sweeps); a turn started for a user message or an ask answer is the user's own work."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.set_config("budget.global_daily_usd", 0)
    wins = []   # no plan windows: billed by use, so the guard counts dollars

    def turn(cost, messages, ago, events=()):
        note = json.dumps({"messages": list(messages), "events": list(events),
                           "triggers": ["user message"] if messages else []})
        p.db.x("INSERT INTO runs(role,provider,effort,status,started,ended,cost_usd,note) "
               "VALUES('coordinator','claude','high','ok',?,?,?,?)", (now - ago - 60, now - ago, cost, note))
        p.db.spend("claude", cost, "coordinator")

    def coord_reasons():
        g = bud.evaluate(p.db, p.config(), "claude", wins, now=now)
        return g, [r for r in g.reasons if "coordinator spent" in r]

    # (1) an hour of 12 user-message turns (tricky turns), $7.20 against a $4/h limit
    for i in range(12):
        turn(0.60, [100 + i], 60 + i * 250)
    g, hit = coord_reasons()
    assert not hit and g.level != "red", g.reasons
    assert g.numbers["coordinator_1h"] == 0 and g.numbers["coordinator_user_1h"] == 7.2

    # (3) a mixed hour: self-started turns under the limit do not trip it...
    for i in range(6):
        turn(0.60, [], 90 + i * 300, events=[i])
    g, hit = coord_reasons()
    assert not hit, g.reasons
    # ...and trip it once the self-started share alone passes the limit
    turn(0.60, [], 120, events=[99])
    g, hit = coord_reasons()
    assert hit and g.level == "red" and "$4.20" in hit[0], g.reasons

    # (2) 40 self-wakes on the same digest within an hour still trip it
    p.db.x("DELETE FROM runs")
    for i in range(40):
        turn(0.15, [], 30 + i * 80, events=[7])
    g, hit = coord_reasons()
    assert hit and g.level == "red", g.reasons
    # an unreadable or missing note counts as self-started, never as the user's
    p.db.x("DELETE FROM runs")
    for note in (None, "not json", "[]"):
        p.db.x("INSERT INTO runs(role,provider,status,started,ended,cost_usd,note) "
               "VALUES('coordinator','claude','ok',?,?,1.5,?)", (now - 600, now - 300, note))
    assert coord_reasons()[1]



def test_usage_billed_known_from_its_own_runs_without_a_recorded_change(env):
    """The account's own paid runs that ended after the last plan reading without one of their own
    show it is billed by use, before any budget change is recorded (billing.py's lapse rule)."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    _old_plan(p, now)
    bud.note_account(p.db, "claude", "acct-b | contracted")
    _run(p, "acct-b | contracted", now - 500, now - 400, 0.3)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.numbers.get("account_mismatch"), "one run is not yet evidence"
    _run(p, "acct-b | contracted", now - 300, now - 200, 0.3)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert not g.numbers.get("account_mismatch") and g.numbers["billed_by_use_since"] == now - 500, g.reasons



def test_plan_billed_work_with_high_virtual_dollars_is_not_held(env):
    """On a plan with headroom, dollars are virtual: neither the dollar caps nor the runaway guard's
    dollar signals hold its work."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.set_config("budget.daily_usd", 10)
    p.set_config("budget.global_daily_usd", 10)
    wins = [bud.Window("claude", "five_hour", 7.0, now + 3600), bud.Window("claude", "seven_day", 30.0, now + 86400)]
    for i in range(30):                                   # $600 of plan spend in the hour, all productive
        p.db.x("INSERT INTO runs(role,provider,status,started,ended,cost_usd) VALUES('worker','claude','ok',?,?,20)",
               (now - 3000, now - 60 - i))
        p.db.spend("claude", 20.0, f"task:{i}")
    for i in range(8):                                    # coordinator turns of $1 each, all different
        p.db.x("INSERT INTO runs(role,provider,status,started,ended,cost_usd,note) "
               "VALUES('coordinator','claude','ok',?,?,1.0,?)",
               (now - 600 - i, now - 500 - i, json.dumps({"messages": [], "gate": {"bg": f"b{i}", "keys": []}})))
    g = bud.evaluate(p.db, p.config(), "claude", wins, now)
    assert g.regime == "windows" and g.numbers["billing"] == "plan" and g.level == "green", g.reasons
    assert not any("cap" in r or "runaway" in r for r in g.reasons), g.reasons



def test_runaway_guard_on_a_plan_counts_wasted_runs_not_virtual_dollars(env):
    """On a plan with headroom a few costly failed runs are not a loop; more failed runs than the
    per-worker cap are. Billed by use, the dollar limit still holds."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    wins = [bud.Window("claude", "five_hour", 7.0, now + 3600), bud.Window("claude", "seven_day", 30.0, now + 86400)]

    def fail(i):
        p.db.x("INSERT INTO runs(task,role,provider,status,started,ended,cost_usd) "
               "VALUES(1,'worker','claude','failed',?,?,12)", (now - 3000 + i * 300, now - 2900 + i * 300))
        p.db.spend("claude", 12.0, "task:1")

    for i in range(2):                                    # $24 lost, far over the $8 dollar limit
        fail(i)
    g = bud.evaluate(p.db, p.config(), "claude", wins, now)
    assert g.numbers["billing"] == "plan" and g.numbers["waste_runs_1h"] == 2, g.numbers
    assert g.level != "red" and not any("runaway" in r for r in g.reasons), g.reasons
    g = bud.evaluate(p.db, p.config(), "claude", [], now)  # billed by use: dollars
    assert any("$24.00 spent on failed or stalled runs" in r for r in g.reasons), g.reasons
    for i in range(2, 5):                                 # five failed runs of one task: over 4 per worker
        fail(i)
    g = bud.evaluate(p.db, p.config(), "claude", wins, now)
    assert g.level == "red" and any("5 failed or stalled runs in the last hour (limit 4)" in r
                                    for r in g.reasons), g.reasons



def test_runaway_guard_on_a_plan_trips_on_repeated_turns_not_virtual_dollars(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    wins = [bud.Window("claude", "five_hour", 7.0, now + 3600)]

    def turn(ago, bg, cost=0.1):
        p.db.x("INSERT INTO runs(role,provider,status,started,ended,cost_usd,note) "
               "VALUES('coordinator','claude','ok',?,?,?,?)",
               (now - ago - 30, now - ago, cost, json.dumps({"messages": [], "gate": {"bg": bg, "keys": ["k"]}})))
        p.db.spend("claude", cost, "coordinator")

    for i in range(3):                                    # three turns on the same evidence: fine
        turn(100 + i * 60, "same", cost=2.0)              # $6 > the $4 dollar limit, but virtual
    g = bud.evaluate(p.db, p.config(), "claude", wins, now)
    assert g.numbers["coordinator_repeats_1h"] == 3 and g.level != "red", g.reasons
    turn(50, "same")                                      # the fourth re-wake on the same evidence
    g = bud.evaluate(p.db, p.config(), "claude", wins, now)
    assert g.level == "red" and any("same evidence" in r for r in g.reasons), g.reasons
    p.db.x("DELETE FROM runs")
    for i in range(41):                                   # many cheap self-started turns, all different
        turn(30 + i * 60, f"bg{i}", cost=0.01)
    g = bud.evaluate(p.db, p.config(), "claude", wins, now)
    assert g.level == "red" and any("41 self-started coordinator turns" in r for r in g.reasons), g.reasons



def test_the_default_plan_turn_guard_fires_below_the_coordinators_own_turn_limit(env):
    """coordinator.max_turns_per_hour stops self-started turns by itself, so the plan turn guard's
    default sits below it (80%) or it could never fire; an explicit setting still wins."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    wins = [bud.Window("claude", "five_hour", 7.0, now + 3600)]
    limit = int(p.config()["coordinator"]["max_turns_per_hour"])
    assert bud.default_turn_cap(p.config()) < limit

    def turns(n):
        p.db.x("DELETE FROM runs")
        for i in range(n):
            p.db.x("INSERT INTO runs(role,provider,status,started,ended,cost_usd,note) "
                   "VALUES('coordinator','claude','ok',?,?,0.01,?)",
                   (now - 100 - i * 60, now - 70 - i * 60, json.dumps({"messages": [], "gate": {"bg": f"bg{i}"}})))
        return bud.evaluate(p.db, p.config(), "claude", wins, now)

    cap = bud.default_turn_cap(p.config())
    g = turns(cap + 1)
    assert cap + 1 < limit and g.level == "red", "the default turn guard cannot fire under max_turns_per_hour"
    assert any(f"{cap + 1} self-started coordinator turns in the last hour (limit {cap};" in r for r in g.reasons)
    assert g.numbers["guard_ratio"] > 1
    g = turns(cap)
    assert g.level != "red" and g.numbers["guard_ratio"] == 1.0, g.reasons
    p.set_config("budget.hourly_coordinator_turns", 50)
    assert turns(cap + 1).level != "red", "an explicit hourly_coordinator_turns still sets the guard"



def test_a_runaway_guard_hovering_at_its_limit_alerts_once_per_episode(env, monkeypatch):
    """Replays a flap: the coordinator guard crosses its $4 limit, drops just under it and crosses
    again. One alert per episode: it ends only below 80% of the limit, and a repeat within an hour
    of that end is no new alert. Gating is unchanged: every crossing stops new work."""
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon, RED_REPEAT_S
    d = Daemon(p.base)
    alerts = lambda: [m["text"] for m in p.db.q("SELECT text FROM messages WHERE kind='alert'")]  # noqa: E731

    def settle(spent: float) -> bud.Gate:
        gate = bud.Gate(provider="fake", regime="caps", numbers={"guard_ratio": round(spent / 4.0, 3)})
        if spent > 4.0:
            bud._raise(gate, "red", f"runaway guard: coordinator spent ${spent:.2f} on self-started turns in the "
                                    f"last hour (limit $4; turns for user messages not counted)")
            gate.allow_new_work, gate.max_parallel = False, 0
        monkeypatch.setattr(bud, "evaluate", lambda *a, **k: gate)
        d.update_gates()
        return d.gates["fake"]

    settle(2.0)
    for spent in (4.07, 3.9, 4.33, 3.5, 4.12, 3.95, 4.2):   # clears briefly, but stays above 80% ($3.20)
        g = settle(spent)
        assert (g.level == "red") == (spent > 4.0) and g.allow_new_work == (spent <= 4.0), "gating changed"
    assert len(alerts()) == 1 and alerts()[0].startswith("Budget for fake is now red: runaway guard"), alerts()
    settle(2.5)                                             # below 80%: the episode ends
    assert settle(4.1).level == "red"                       # red again within the hour: the same episode
    assert len(alerts()) == 1, alerts()
    settle(2.0)
    eps = p.db.kv("budget_red_episodes")
    assert not eps["fake"]["runaway guard"]["open"], eps
    eps["fake"]["runaway guard"]["cleared"] -= RED_REPEAT_S + 60   # clear for over an hour
    p.db.set_kv("budget_red_episodes", eps)
    settle(2.0)
    assert "runaway guard" not in p.db.kv("budget_red_episodes")["fake"], "an episode long over is forgotten"
    settle(4.4)
    assert len(alerts()) == 2 and alerts()[1].startswith("Budget for fake is now red: runaway guard"), alerts()



def test_budget_text_treats_zero_dollar_caps_as_unset(env):
    """A cap of 0 is unset: a reached cap names only the caps set, and a plan that stopped reporting
    its windows says nothing about dollar caps when none is set."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("INSERT INTO ledger(ts,provider,source,usd) VALUES(?,?,?,?)", (now - 7200, "claude", "task:1", 60.0))
    for daily, weekly, words, absent in ((50.0, 0, "$60.00/24h of $50", "7d"), (0, 50.0, "$60.00/7d of $50", "24h")):
        p.set_config("budget.daily_usd", daily)
        p.set_config("budget.weekly_usd", weekly)
        g = bud.evaluate(p.db, p.config(), "claude", [], now)
        cap = [r for r in g.reasons if r.startswith("cap reached")]
        assert cap == [f"cap reached: {words}"] and absent not in cap[0] and "of $0" not in cap[0], g.reasons
    p.db.x("DELETE FROM ledger")
    p.set_config("budget.weekly_usd", 200.0)
    # a plan whose paid runs report no windows any more: billed by use from its last reading on
    _reading(p, now - 5 * 3600, "acct-a | plan", "five_hour", 20.0, now + 3600)
    for h in (4.9, 4.5, 3):
        _run(p, "acct-a | plan", now - h * 3600 - 60, now - h * 3600, 1.0)

    def reasons():
        g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
        assert g.regime == "caps", g.reasons
        return g.reasons
    assert any("stopped reporting plan windows; its spend counts toward the dollar caps" in r
               for r in reasons()), reasons()
    for key in ("daily_usd", "weekly_usd", "global_daily_usd"):
        p.set_config(f"budget.{key}", 0)
    assert not any("plan windows" in r or "dollar cap" in r for r in reasons()), reasons()
    # the login on another account than the readings: told, but without the caps it does not change
    _run(p, "acct-b | plan", now - 100, now - 50, 1.0)
    bud.note_account(p.db, "claude", "acct-b | plan")
    assert any(r.endswith("has no plan readings of its own yet") for r in reasons()) \
        and not any("dollar cap" in r for r in reasons()), reasons()
    p.set_config("budget.daily_usd", 100.0)
    assert any("has no plan readings of its own yet; its spend counts toward the dollar caps" in r
               for r in reasons()), reasons()



def test_a_changed_wait_or_a_new_commit_is_progress_and_a_reboot_or_escalation_is_neither(env):
    from ttp import budget as bud
    from ttp.daemon import _stale_wakes, _wait_key
    prev = {"status": "waiting", "waiting_for": "CI on x", "retry_when": "ttp ci --branch x", "stale_wakes": 1}
    key = {"key": _wait_key(prev), "head": "a" * 40}
    prev["wait_key"] = dict(key)
    assert _stale_wakes(prev, key, False) == 2
    assert _stale_wakes(prev, {**key, "key": _wait_key({**prev, "waiting_for": "the device"})}, False) == 0
    assert _stale_wakes(prev, {**key, "key": _wait_key({**prev, "retry_when": "test -e m"})}, False) == 0
    assert _stale_wakes(prev, {**key, "head": "b" * 40}, False) == 0, "a new commit is progress"
    assert _stale_wakes({**prev, "waiting_for": "  ci ON  x "}, {**key, "key": _wait_key(
        {**prev, "waiting_for": "  ci ON  x "})}, False) == 2
    assert _stale_wakes(prev, key, True) == 1, "an escalating light wake neither counts nor resets"
    assert _stale_wakes({**prev, "woke": "the host rebooted"}, key, False) == 1
    assert _stale_wakes({**prev, "status": "failed"}, key, False) == 0, "a first wait starts at 0"
    old = {k: v for k, v in prev.items() if k != "wait_key"}
    assert _stale_wakes(old, key, False) == 2, "a hand-off from before the key was kept compares its fields"
    assert bud.wake_tier("deep", {**prev, "wake_tier": "deep"}) == "light"
    assert bud.wake_tier("deep", {**prev, "next_step": "read the logs, " * 20}) == "light"
    assert bud.wake_tier("standard", {**prev, "wake_tier": "standard", "escalated_wake": True}) == "standard"
    assert bud.wake_tier("standard", {**prev, "stale_wakes": "x", "wake_tier": "standard"}) == "standard"



@needs_sockets
def test_a_requeue_or_a_re_plan_starts_the_unchanged_wake_count_over(env):
    """Every way a person or the coordinator decides to run a stale wait again (a coordinator or web
    requeue, a new spec) drops its unchanged-wake count: the next run is at its own wake tier."""
    p = make(env)
    from ttp import budget as bud
    from ttp import coordinator as coord
    from ttp import web
    from ttp.daemon import _wait_key
    from ttp.db import dump_result, load_result
    wait = {"status": "waiting", "summary": "busy", "waiting_for": "a free board", "wake_tier": "standard",
            "waiting_since": time.time()}
    stale = {**wait, "stale_wakes": 2, "wait_key": {"key": _wait_key(wait), "head": ""}}

    def stale_task(status):
        tid = p.db.add_task(f"stale {status}", "s", kind="work", tier="standard", origin="user")
        p.db.update_task(tid, status=status, result=dump_result(stale))
        assert bud.wake_tier("standard", load_result(p.db.task(tid)["result"])) == "light"
        return tid

    def after(tid):
        res = load_result(p.db.task(tid)["result"])
        assert "stale_wakes" not in res and res["wait_key"] == stale["wait_key"], res
        assert bud.wake_tier("standard", res) == "standard"

    for status in ("blocked", "waiting"):
        tid = stale_task(status)
        assert coord.apply(p, [{"type": "task_update", "id": tid, "status": "queued"}]) == []
        after(tid)
    tid = stale_task("queued")
    assert coord.apply(p, [{"type": "task_update", "id": tid, "spec": "measure on any free board"}]) == []
    after(tid)
    tid = stale_task("queued")
    assert coord.apply(p, [{"type": "task_update", "id": tid, "priority": 1}]) == []
    assert load_result(p.db.task(tid)["result"])["stale_wakes"] == 2, "only a requeue or a new spec resets"
    tid = stale_task("blocked")
    port = _start_web(p)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/task/{tid}", method="POST",
                                 data=json.dumps({"status": "queued"}).encode(),
                                 headers={"X-TTP-Token": web.token(p), "Content-Type": "application/json"})
    for _ in range(50):
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                assert r.status == 200
            break
        except urllib.error.HTTPError:
            raise
        except OSError:
            time.sleep(0.1)
    after(tid)



def test_a_mechanical_next_step_wakes_at_light_and_that_run_finishes_it(env, monkeypatch):
    """A review whose detached checks finished, with only its push left (next_step), takes one light
    run that pushes and is done: no light check plus a rerun at the task's tier and effort."""
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    from ttp.db import dump_result
    wait = {"status": "waiting", "summary": "checks started", "waiting_for": "the checks", "wake_tier": "standard"}
    step = {"status": "waiting", "summary": "checks started", "waiting_for": "the checks", "next_step": "push"}
    assert bud.wake_tier("standard", step) == "light"
    assert bud.wake_tier("deep", {"status": "waiting", "next_step": "push"}) == "light"
    assert bud.wake_tier("standard", wait) == "standard"
    assert bud.wake_tier("standard", {**wait, "next_step": "  "}) == "standard", "a blank step is no step"
    esc = {"status": "waiting", "retry_after_s": 0, "wake_tier": "standard", "next_step": "push",
           "escalated_wake": True}
    assert bud.wake_tier("standard", esc) == "standard", "an escalated wake runs at the task's tier"
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({"status": "done", "summary": "checks green; pushed"}))
    tiers = {"light": {"effort": "low"}, "standard": {"effort": "high"}, "deep": {"effort": "max"}}
    p.set_config("providers.fake.tiers", tiers)
    mech = p.db.add_task("review and push", "s", kind="work", tier="standard", origin="user")
    p.db.update_task(mech, result=dump_result(step))
    plain = p.db.add_task("review and push, unmarked", "s", kind="work", tier="standard", origin="user")
    p.db.update_task(plain, result=dump_result(wait))
    d = Daemon(p.base)

    def runs(tid):
        return p.db.q("SELECT status, effort, note FROM runs WHERE task=? AND role='worker' ORDER BY id", (tid,))

    assert _run_until(d, p, lambda: all(p.db.task(t)["status"] == "done" for t in (mech, plain)))
    (one,) = runs(mech)
    assert json.loads(one["note"])["wake"] == {"tier": "light", "escalated": False}
    assert one["effort"] == "low"
    (other,) = runs(plain)
    assert json.loads(other["note"])["wake"]["tier"] == "standard"
    assert other["effort"] == "high"
    assert p.db.task(mech)["attempts"] == 1



def test_an_explicit_wake_tier_or_a_long_next_step_is_judgment_and_wakes_above_light(env):
    """Only a short next_step with no wake_tier above light is a mechanical step a light wake does;
    an explicit wake_tier wins over next_step, still capped by the task's own tier."""
    from ttp import budget as bud
    from ttp.prompts import worker_task
    from ttp.db import dump_result
    base = {"status": "waiting", "waiting_for": "the checks", "retry_when": "test -e rc", "next_step": "push"}
    assert bud.wake_tier("standard", base) == "light", "a mechanical step without a tier wakes light"
    assert bud.wake_tier("standard", {**base, "wake_tier": "light"}) == "light"
    assert bud.wake_tier("standard", {**base, "wake_tier": "bogus"}) == "light", "an unknown tier is no tier"
    assert bud.wake_tier("standard", {**base, "wake_tier": "standard"}) == "standard"
    assert bud.wake_tier("deep", {**base, "wake_tier": "deep"}) == "deep"
    assert bud.wake_tier("standard", {**base, "wake_tier": "deep"}) == "standard", "capped by the task's tier"
    assert bud.wake_tier("light", {**base, "wake_tier": "standard"}) == "light", "capped by the task's tier"
    esc = {**base, "retry_after_s": 0, "wake_tier": "standard", "escalated_wake": True}
    assert bud.wake_tier("deep", esc) == "standard", "an escalated wake still runs at its wake_tier"
    plan = {**base, "next_step": "read the logs, " * 20}
    assert len(plan["next_step"]) > bud.NEXT_STEP_MAX
    assert bud.wake_tier("standard", plan) == "standard", "a long next_step is a plan, not one step"
    assert bud.wake_tier("standard", {**plan, "wake_tier": "light"}) == "light", "an explicit light wins"
    assert bud.mechanical_step(base) == "push" and not bud.mechanical_step(plan)
    assert not bud.mechanical_step({**base, "wake_tier": "standard"})
    p = make(env)
    tid = p.db.add_task("review", "s", kind="review", tier="standard", origin="user")
    p.db.update_task(tid, result=dump_result({**base, "wake_tier": "standard"}))
    out = worker_task(p, p.db.task(tid), str(p.root), None, wake={"tier": "standard", "escalated": False})
    assert "mechanical step" not in out and "next step for after its wait: push" in out



def test_coordinator_cache_hit_rate_and_cost_per_turn(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    rows = [(now - 3600, 0.05, 100, 9000, 900),          # warm: 90% read
            (now - 7200, 0.15, 100, 1000, 8900),         # missed: the charter changed
            (now - 3 * 86400, 0.04, 100, 9800, 100)]     # older than a day
    for k, (started, cost, fresh, read, write) in enumerate(rows):
        note = {"cache_telemetry": {"complete": True, "requests": None}}
        if k == 0:
            note["digest"] = {"chars": 4000, "tokens": 1000}
        note = json.dumps(note)
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd,input_tokens,cache_read_tokens,"
               "cache_write_tokens,note) VALUES('coordinator','claude',?,?,'done',?,?,?,?,?)",
               (started, started + 60, cost, fresh, read, write, note))
    p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd,input_tokens,cache_read_tokens) "
           "VALUES('worker','claude',?,?,'done',1,10,0)", (now - 60, now))
    c = bud.coordinator_cache(p.db, now)
    # Cache writes per turn are kept apart from the miss rate; the digest size only where recorded.
    assert {k: c["24h"][k] for k in ("turns", "hit_pct", "miss_turns", "usd_per_turn", "write_per_turn",
                                    "digest_tokens")} == {"turns": 2, "hit_pct": 50, "miss_turns": 1,
                                                          "usd_per_turn": 0.1, "write_per_turn": 4900,
                                                          "digest_tokens": 1000}
    assert c["7d"]["turns"] == 3 and c["7d"]["hit_pct"] == 66 and c["7d"]["miss_turns"] == 1
    assert bud.history(p.db)["coordinator_cache"]["24h"]["turns"] == 2



def test_a_running_worker_spend_counts_before_it_ends(env, tmp_path):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    from ttp.web import health
    p.set_config("budget.daily_usd", 5)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    msg = {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
                                            "content": [{"type": "text", "text": "building"}]}}
    (run_dir / "output.jsonl").write_text(json.dumps(msg) + "\n")
    rid = p.db.x("INSERT INTO runs(role,provider,model,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
                 ("worker", "claude", "opus", time.time(), "running", str(run_dir), "x"))
    d = Daemon(p.base)
    d.meter_running()
    cost = p.db.one("SELECT cost_usd FROM runs WHERE id=?", (rid,))["cost_usd"]
    assert cost and cost > 5, "a running worker showed no spend"
    g = bud.evaluate(p.db, p.config(), "claude", [])
    assert g.level == "red" and g.numbers["in_flight"] == round(cost, 2), "the caps ignored spend in flight"
    assert health(p, p.db)["spend"]["in_flight"] == round(cost, 2)
    result = {"type": "result", "total_cost_usd": 1.25, "usage": {}, "result": "done", "subtype": "success"}
    with open(run_dir / "output.jsonl", "a") as f:
        f.write(json.dumps(result) + "\n")
    (run_dir / "exit.json").write_text(json.dumps({"rc": 0, "ended": time.time()}))
    d.reap_runs()
    row = p.db.one("SELECT cost_usd, cost_estimated FROM runs WHERE id=?", (rid,))
    assert (row["cost_usd"], row["cost_estimated"]) == (1.25, 0)
    assert bud.in_flight(p.db) == 0 and round(p.db.spent_since(0), 2) == 1.25, "counted twice once it ended"



def _claude_stream_without_result(path, messages=4):
    """A Claude run killed before its result line. Each API message streams as three events that
    repeat the same usage; 107,510 weighted tokens per message."""
    evs = [{"type": "system", "subtype": "init", "session_id": "s", "model": "m"}]
    usage = {"input_tokens": 10, "output_tokens": 5000, "cache_read_input_tokens": 800_000,
             "cache_creation_input_tokens": 2000}
    for i in range(messages):
        for block in ({"type": "thinking", "thinking": "..."}, {"type": "text", "text": f"step {i}"},
                      {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}):
            evs.append({"type": "assistant", "message": {"id": f"msg_{i}", "usage": usage, "content": [block]}})
    path.write_text("\n".join(json.dumps(e) for e in evs) + "\n")



def test_runs_without_a_result_line_count_their_estimated_cost(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("long job", "s", kind="work", tier="standard", origin="user", provider="claude",
                        budget_usd=10.0)

    def stalled_run(n):
        run_dir = tmp_path / f"run{n}"
        run_dir.mkdir()
        _claude_stream_without_result(run_dir / "output.jsonl")
        (run_dir / "exit.json").write_text(json.dumps({"rc": -15, "stopped": "stalled", "ended": time.time()}))
        p.db.update_task(tid, status="running")
        rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?,?)",
                     (tid, "worker", "claude", "opus", time.time() - 600, "running", str(run_dir), d.boot))
        d.reap_runs()
        return p.db.one("SELECT * FROM runs WHERE id=?", (rid,))

    first = stalled_run(1)
    assert first["cost_estimated"] == 1 and first["cost_usd"] == pytest.approx(4 * 107_510 * 15 / 1e6)
    assert first["cache_read_tokens"] == 4 * 800_000, "one message's usage was counted more than once"
    # four API messages; the first one's prompt is the prefix every later call re-reads (`ttp stats`)
    assert json.loads(first["note"])["cache_telemetry"] == {"complete": True, "requests": None, "source": "messages",
                                                            "calls": 4, "first_context": 802_010}
    assert p.db.one("SELECT estimated FROM ledger")["estimated"] == 1
    assert p.db.task(tid)["spent_usd"] == pytest.approx(first["cost_usd"])
    started = {}
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.update(k) or 0)
    p.db.update_task(tid, not_before=None)
    d.dispatch()
    assert started["budget_usd"] == pytest.approx(10.0 - first["cost_usd"]), "the retry got a fresh budget"
    stalled_run(2)
    g = bud.evaluate(p.db, p.config(), "claude", [])
    assert g.level == "red" and any("failed or stalled" in r for r in g.reasons)



def test_estimates_use_the_projects_own_observed_rate(env):
    p = make(env)
    from ttp import budget as bud
    tokens = {"input": 0, "output": 10_000, "cache_read": 500_000, "cache_write": 0}     # 100k weighted
    assert bud.estimate_cost(p.db, p.config(), "claude", "opus", tokens) == pytest.approx(1.5)
    # $1 reported for 200k weighted tokens: $5 per million from now on
    p.db.x("INSERT INTO runs(role,provider,model,status,started,ended,cost_usd,cost_estimated,output_tokens,"
           "cache_read_tokens) VALUES('worker','claude','opus','ok',?,?,1.0,0,20000,1000000)",
           (time.time() - 100, time.time() - 50))
    assert bud.estimate_cost(p.db, p.config(), "claude", "opus", tokens) == pytest.approx(0.5)



def test_dollar_caps_cover_the_whole_project(env):
    p = make(env)
    from ttp import budget as bud
    two_hours_ago = time.time() - 7200          # outside the runaway guard's last hour
    for prov in ("claude", "codex"):
        p.db.x("INSERT INTO ledger(ts,provider,source,usd) VALUES(?,?,?,?)", (two_hours_ago, prov, "task:1", 60.0))
    for prov in ("claude", "codex"):
        g = bud.evaluate(p.db, p.config(), prov, [])
        assert g.level == "red" and any("cap reached" in r for r in g.reasons), (prov, g.reasons)
    # A provider on plan windows is bounded by its windows, so its spend does not use up the caps.
    assert bud.evaluate(p.db, p.config(), "codex", [bud.Window("claude", "seven_day", 20)]).level == "yellow"
    # An idle plan provider has no fresh reading but is still on a plan: its unbilled cost stays out.
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (time.time() - 3600, "claude", "", "seven_day", 20, None))
    assert bud.evaluate(p.db, p.config(), "codex", []).level == "yellow"



def test_plan_provider_spend_after_its_last_window_counts_toward_caps(env):
    # A provider that stops reporting windows may have moved to usage billing: its later spend counts.
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 3 * 86400, "claude", "", "seven_day", 20, None))
    p.db.x("INSERT INTO ledger(ts,provider,source,usd) VALUES(?,?,?,?)", (now - 3 * 86400 + 600, "claude", "t", 500.0))
    assert bud.evaluate(p.db, p.config(), "codex", []).level == "green"
    p.db.x("INSERT INTO ledger(ts,provider,source,usd) VALUES(?,?,?,?)", (now - 7200, "claude", "t", 120.0))
    g = bud.evaluate(p.db, p.config(), "codex", [])
    assert g.level == "red" and any("cap reached" in r for r in g.reasons), g.reasons



def test_stale_plan_windows_do_not_extend_the_cap_exclusion(env):
    # The daemon passes plan_windows (readings up to a week old) to evaluate; a stale one must not
    # re-exclude the provider's spend since that reading.
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 3 * 86400, "claude", "", "seven_day", 20, None))
    p.db.x("INSERT INTO ledger(ts,provider,source,usd) VALUES(?,?,?,?)", (now - 7200, "claude", "t", 120.0))
    g = bud.evaluate(p.db, p.config(), "codex", bud.plan_windows(p.db, now), now)
    assert g.level == "red" and g.numbers["spent_24h"] == 120.0, (g.level, g.numbers)
    # A fresh reading covers spend up to now.
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 60, "claude", "", "seven_day", 25, None))
    p.db.x("INSERT INTO ledger(ts,provider,source,usd) VALUES(?,?,?,?)", (now - 30, "claude", "t", 50.0))
    g = bud.evaluate(p.db, p.config(), "codex", bud.plan_windows(p.db, now), now)
    assert g.numbers["spent_24h"] == 0.0, g.numbers



def test_a_refused_wake_keeps_the_wait_so_its_retry_stays_light(env):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    from ttp.db import dump_result, load_result
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task("job", "spec", kind="work", tier="deep", origin="user")
    p.db.update_task(tid, status="running", result=dump_result(
        {"status": "waiting", "summary": "build running", "waiting_for": "the build",
         "retry_when": "test -e done", "waits": 3}))
    d = Daemon(p.base)
    for status in ("limit", "auth"):
        d._finish_worker({"task": tid}, Usage(final_text="usage limit reached"), status, env["tmp"])
        t = p.db.task(tid)
        res = load_result(t["result"])
        assert t["status"] == "queued" and t["attempts"] == 0
        assert res["status"] == "waiting" and res["retry_when"] == "test -e done" and res["waits"] == 3
        assert bud.wake_tier(t["tier"], res) == "light"
        p.db.update_task(tid, status="running")



def test_a_budget_skipped_llm_schedule_retries_when_the_gate_opens(env):
    p = make(env)
    from ttp import budget as bud
    from ttp import schedule as sched
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    from ttp.web import health
    sched.upsert(p.db, "summary", "llm", "1d", payload={"spec": "sum up"})
    last = time.time() - 86400 - 60
    p.db.x("UPDATE schedules SET last_run=?, next_run=? WHERE name='summary'", (last, time.time() - 1))
    d = Daemon(p.base)
    core = d.cfg.get("core_provider", "claude")
    d.gates = {core: bud.Gate(provider=core, level="red", allow_optional=False)}
    before = time.time()
    d.run_schedules()
    s = p.db.one("SELECT * FROM schedules WHERE name='summary'")
    assert s["last_status"] == "skipped: budget red"
    assert s["last_run"] == last   # the period still counts as not run
    assert before + 1800 - 5 <= s["next_run"] <= time.time() + 1800
    assert "schedules waiting for budget: summary" in status_text(p)
    assert health(p, p.db)["schedules_waiting"] == "schedules waiting for budget: summary"
    app = (pathlib.Path(sched.__file__).parent / "web" / "app.js").read_text()
    assert 'startsWith("skipped: budget ") ? "waiting for budget"' in app
    # A short schedule retries after its own period, not later.
    sched.upsert(p.db, "fast", "llm", "10m", payload={"spec": "x"})
    row = p.db.one("SELECT * FROM schedules WHERE name='fast'")
    sched.mark_ran(p.db, row, "skipped: budget yellow", now=1000.0)
    assert p.db.one("SELECT next_run FROM schedules WHERE name='fast'")["next_run"] == 1600.0
    # Once the gate allows optional work, the next retry runs it, once, and the period moves on.
    d.gates = {}
    p.db.x("UPDATE schedules SET next_run=? WHERE name='summary'", (time.time() - 1,))
    d.run_schedules()
    s = p.db.one("SELECT * FROM schedules WHERE name='summary'")
    assert s["last_status"] == "queued" and s["last_run"] > last
    assert s["next_run"] >= s["last_run"] + 86400 - 1
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='schedule' AND labels=?",
                    (json.dumps(["summary"]),))["n"] == 1
    assert "waiting for budget: summary" not in status_text(p)



def test_hourly_guard_counts_long_runs_only_for_the_time_they_ran_in_the_hour(env):
    p = make(env)
    from ttp import budget as bud
    p.set_config("budget.hourly_floor_usd", 30)
    now = time.time()
    # $60 over three hours so far is $20 in the last hour: a busy run, not a runaway.
    p.db.x("INSERT INTO runs(role,provider,started,status,cost_usd) VALUES('worker','claude',?,'running',60)",
           (now - 3 * 3600,))
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert g.numbers["spent_1h"] == pytest.approx(20, abs=0.01)
    assert not any("runaway guard" in r for r in g.reasons), g.reasons
    # The same run, ended now: the ledger books all $60 at its end, the guard still counts $20.
    p.db.x("UPDATE runs SET status='ok', ended=?", (now,))
    p.db.spend("claude", 60.0, "task:1")
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert g.numbers["spent_1h"] == pytest.approx(20, abs=0.1)
    assert not any("runaway guard" in r for r in g.reasons), g.reasons
    # Spending that fast inside the hour still trips it.
    p.db.x("INSERT INTO runs(role,provider,started,status,cost_usd) VALUES('worker','claude',?,'running',15)",
           (now - 600,))
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert g.level == "red" and any("long runs pro rata" in r for r in g.reasons), g.reasons



def test_cancelled_runs_are_not_runaway_waste(env):
    """Redirecting a project cancels its running work; that must not pause the project."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    for cost in (4.6, 4.5):
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd) VALUES('worker','claude',?,?,?,?)",
               (now - 600, now - 60, "killed", cost))
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert not any("failed or stalled" in r for r in g.reasons), g.reasons



def test_plan_pacing_changes_do_not_alert_the_user(env, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    alerts = lambda: p.db.one("SELECT COUNT(*) n FROM messages WHERE kind='alert'")["n"]  # noqa: E731

    def settle(level: str) -> None:
        gate = bud.Gate(provider="fake", level=level, regime="windows")
        monkeypatch.setattr(bud, "evaluate", lambda *a, **k: gate)
        d.update_gates()

    settle("green")
    before = alerts()
    for level in ("yellow", "green", "yellow", "green"):
        settle(level)
    assert alerts() == before, "pacing between green and yellow alerted the user"
    settle("red")
    assert alerts() == before + 1, "hitting the plan limit must alert"



def test_only_reaching_the_plan_line_alerts_once_per_window_and_no_level_wakes(env, monkeypatch):
    """Green, yellow and orange flips post nothing and leave the wake state alone; reaching the line
    alerts once, and again only in the next window."""
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon, wake_fingerprint
    d = Daemon(p.base)
    alerts = lambda: [m["text"] for m in p.db.q("SELECT text FROM messages WHERE kind='alert'")]  # noqa: E731
    resets = [time.time() + 3600]

    def settle(level: str, util: float = 80.0) -> str:
        gate = bud.Gate(provider="fake", level=level, regime="windows", numbers={
            "limit": 90.0, "plan": [{"window": "five_hour", "utilization": util, "resets_at": resets[0]}]})
        if level == "red":
            gate.allow_new_work = False
        monkeypatch.setattr(bud, "evaluate", lambda *a, **k: gate)
        d.update_gates()
        return wake_fingerprint(p, p.db.kv("gates"))

    fp = settle("green")
    for level in ("yellow", "orange", "yellow", "orange", "green"):
        assert settle(level) == fp, f"{level} changed what wakes the coordinator"
    assert alerts() == [], alerts()
    assert settle("red", 90.0) != fp
    assert len(alerts()) == 1 and alerts()[0].startswith("Budget for fake is now red"), alerts()
    settle("orange", 89.0)
    settle("red", 90.0)
    assert len(alerts()) == 1, "the same plan window alerted twice"
    resets[0] += 5 * 3600
    settle("green", 0.0)
    settle("red", 90.0)
    assert len(alerts()) == 2, "a new plan window reaching the line must alert"



def test_burn_rate_is_steady_across_whole_percent_readings(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    resets = now + 3 * 3600
    # 6 points per hour, reported in whole percents every 10 minutes
    for i, minutes_ago in enumerate(range(60, -1, -10)):
        util = round(30 + 6 * (60 - minutes_ago) / 60)
        p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
               (now - minutes_ago * 60, "claude", "a", "five_hour", util, resets))
    rate = bud.burn_rate(p.db, "claude", "five_hour", resets, now)
    assert 5.0 <= rate <= 7.0, rate



def test_a_plan_that_stopped_reporting_windows_falls_back_to_the_caps(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 3 * 3600, "claude", "a", "seven_day", 40.0, now + 3 * 86400))
    def ledger(ts, provider, account, source, usd):   # a successful run that reported no windows
        p.db.x("INSERT INTO ledger(ts,provider,account,source,usd) VALUES(?,?,?,?,?)",
               (ts, provider, account, source, usd))
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd) VALUES('worker',?,?,?,'ok',?)",
               (provider, ts - 60, ts, usd))
    ledger(now - 3 * 3600 + 600, "claude", "a", "task:1", 90.0)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "windows", "one run without a reading is not yet a lapsed plan"
    for h in (2, 1):
        ledger(now - h * 3600 + 600, "claude", "a", "task:1", 90.0)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and g.level == "red" and not g.allow_new_work, (g.regime, g.level, g.reasons)
    assert g.numbers["spent_24h"] == 270.0, g.numbers
    # a new reading puts it back on the plan, and runs ending between a meter's reads keep it there
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 600, "claude", "a", "seven_day", 45.0, now + 3 * 86400))
    for m in (5, 1):
        ledger(now - m * 60, "claude", "a", "task:1", 1.0)
    assert bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now).regime == "windows"



def test_the_idle_slot_wake_fires_below_the_line_and_backs_off(env):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    _no_events(p)
    d = Daemon(p.base)
    prov = d.cfg.get("core_provider", "claude")
    plan = [{"window": "seven_day", "utilization": 50.0, "resets_at": time.time() + 36000, "hours_left": 10.0,
             "headroom": 40.0, "per_worker_per_h": 1.0}]
    d.gates = {prov: bud.Gate(provider=prov, regime="windows", max_parallel=6, numbers={"plan": plan})}

    def turn_after(seconds, same_state=False):
        before = _turns(p)
        if not same_state:
            p.db.set_kv("idle_wake", {})    # isolate this wake's own backoff from the unchanged-state one
        p.db.set_kv("last_coordinator_turn", time.time() - seconds)
        d.maybe_coordinate()
        _stop_all(p)
        p.db.x("UPDATE runs SET status='killed', ended=? WHERE status='running'", (time.time(),))
        return _turns(p) > before

    assert turn_after(301), "a plan below its line with free slots asks for work"
    assert not turn_after(1201, same_state=True), "an unchanged state backs the idle-slot wake off too"
    assert not turn_after(301) and turn_after(601), "a turn that added no task doubles the wait"
    p.db.add_task("new work", "s", kind="work", tier="light", origin="coordinator", status="done")
    assert turn_after(301), "a new task resets the wait"
    d.gates[prov].level, d.gates[prov].max_parallel = "yellow", 3
    p.db.add_task("more work", "s", kind="work", tier="light", origin="coordinator", status="done")
    assert not turn_after(3000), "a plan in its last stretch before the line needs no extra work"



def test_stall_check_ignores_tasks_held_by_a_paused_resource_a_logout_or_the_gate(env, monkeypatch):
    from ttp import budget as bud
    from ttp import coordinator as coord
    p, d, now, check = _stall_setup(env, monkeypatch)
    prov = d.cfg.get("core_provider", "claude")

    coord.pause_resource(p, "board", True, reason="maintenance", by="user")
    tid = p.db.add_task("needs the board", "s", kind="work", tier="light", origin="coordinator",
                        labels=["exclusive:board"])
    for h in (0, 3, 6):
        assert check(now + h * 3600) == [], "a task held only by a user-paused resource is no stall"
    p.db.update_task(tid, status="cancelled")

    p.db.add_task("ready", "s", kind="work", tier="light", origin="coordinator")
    d.gates = {prov: bud.Gate(provider=prov, regime="windows", max_parallel=1)}
    p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(NULL,'worker',?,?,'running')",
           (prov, now - 5 * 3600))
    for h in (14, 17, 20):
        assert check(now + h * 3600) == [], "every slot the gate (or pacing) allows is busy: dispatch starts nothing"
    d.gates = {prov: bud.Gate(provider=prov, regime="windows", max_parallel=4, allow_new_work=False)}
    for h in (21, 24, 27):
        assert check(now + h * 3600) == [], "a gate that holds new work: dispatch starts nothing"
    d.gates = {prov: bud.Gate(provider=prov, regime="windows", max_parallel=4)}
    assert check(now + 28 * 3600) == []
    assert len(check(now + 31 * 3600)) == 1, "with a free slot, the same task is a stall"
    p.db.x("UPDATE tasks SET status='cancelled'")
    p.db.x("UPDATE runs SET status='ok'")

    p.db.x("INSERT INTO runs(task,role,started,status) VALUES(NULL,'worker',?,'ok')", (now + 32 * 3600,))
    tid = p.db.add_task("on a logged-out provider", "s", kind="work", tier="light", origin="coordinator")
    p.db.update_task(tid, provider="fake")
    d.open_breaker("fake", "401")
    monkeypatch.setattr(d, "_may_probe", lambda prov, now: False)
    for h in (36, 39, 42):
        assert len(check(now + h * 3600)) == 1, "a task of a logged-out provider is no new stall"



def test_idle_capacity_stays_quiet_when_paused_gated_full_in_trouble_or_work_is_queued(env, monkeypatch):
    from ttp import budget as bud
    from ttp import coordinator as coord
    p, d, now, prov, check = _idle_setup(env, monkeypatch)
    coord.pause_resource(p, "rig-device", True, reason="maintenance", by="user")
    assert check(now) == [] and check(now + 3600) == [], "a user-paused device is never idle capacity"
    assert not p.db.kv("idle_capacity")
    coord.pause_resource(p, "rig-device", False)
    p.db.set_kv("resource_trouble", {"rig-device": now})
    assert check(now + 2 * 3600) == [] and check(now + 3 * 3600) == [], "a device in trouble is not idle"
    p.db.set_kv("resource_trouble", {})

    t = now + 4 * 3600
    for level in ("red", "orange"):
        d.gates = {prov: bud.Gate(provider=prov, regime="windows", max_parallel=2, level=level)}
        assert check(t) == [] and check(t + 3600) == [], f"a {level} gate raises nothing"
        t += 2 * 3600
    d.gates = {prov: bud.Gate(provider=prov, regime="windows", max_parallel=2, allow_new_work=False)}
    assert check(t) == [], "a gate that holds new work raises nothing"
    d.gates = {prov: bud.Gate(provider=prov, regime="windows", max_parallel=1)}
    run = p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(NULL,'worker',?,?,'running')",
                 (prov, t))
    assert check(t + 3600) == [], "no worker slot free: nothing could start for it"
    p.db.x("UPDATE runs SET status='ok' WHERE id=?", (run,))
    ready = p.db.add_task("ready work", "s", kind="work", tier="light", origin="coordinator")
    assert check(t + 2 * 3600) == [], "dispatch would start queued work first"
    p.db.update_task(ready, status="cancelled")
    waiting = p.db.add_task("waits on its device job", "s", kind="work", tier="light", origin="coordinator",
                            labels=["resource:rig-device"], not_before=t + 86400)
    assert check(t + 3 * 3600) == [] and check(t + 4 * 3600) == [], "queued work names the device"
    p.db.update_task(waiting, status="cancelled")
    # The stretch went on while only the slots or the gate held it back: once they free, it raises.
    d.gates = {prov: bud.Gate(provider=prov, regime="windows", max_parallel=2, level="red")}
    assert check(t + 5 * 3600) == [] and check(t + 6 * 3600) == []
    d.gates = {prov: bud.Gate(provider=prov, regime="windows", max_parallel=2, level="yellow")}
    assert len(check(t + 6 * 3600 + 60)) == 1, "a yellow gate with a free slot raises the idle stretch"



def test_a_run_booked_late_counts_toward_the_hour_it_ended_in(env):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    now = time.time()
    run_dir = p.runs / "late"
    run_dir.mkdir(parents=True)
    result = {"type": "result", "total_cost_usd": 40.0, "usage": {}, "result": "done", "subtype": "success"}
    (run_dir / "output.jsonl").write_text(json.dumps(result) + "\n")
    rid = p.db.x("INSERT INTO runs(role,provider,model,started,status,dir) VALUES(?,?,?,?,?,?)",
                 ("worker", "claude", "opus", now - 5 * 3600, "running", str(run_dir)))
    # It ended three hours ago; the daemon was down and books it only now.
    (run_dir / "exit.json").write_text(json.dumps({"rc": 0, "ended": now - 3 * 3600}))
    d.reap_runs()
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "ok"
    g = bud.evaluate(p.db, p.config(), "claude", [])
    assert g.numbers["spent_1h"] == pytest.approx(0, abs=0.01), "a run that ended hours ago counted as last-hour spend"
    assert p.db.spent_since(now - 4 * 3600) == pytest.approx(40), "its spend left the day's total"



def test_an_abandoned_run_keeps_its_live_spend(env):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    now = time.time()
    p.set_config("budget.hourly_floor_usd", 30)
    rid = p.db.x("INSERT INTO runs(role,provider,model,started,status,cost_usd,cost_estimated) "
                 "VALUES('worker','claude','opus',?,'running',6,1)", (now - 2 * 3600,))
    p.db.spend("claude", 10.0, "task:2")
    d._abandon_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)))
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "failed"
    assert p.db.spent_since(now - 3600) == pytest.approx(16), "the run's live spend vanished from the caps"
    g = bud.evaluate(p.db, p.config(), "claude", [])
    assert g.numbers["spent_1h"] == pytest.approx(13, abs=0.1), "other spend was discounted by the run's share"



def test_a_task_waiting_for_its_resource_keeps_its_reservation(env):
    p = make(env)
    from ttp import budget as bud
    from ttp import coordinator as coord
    from ttp import locks
    from ttp.daemon import Daemon
    assert coord.apply(p, [{"type": "task_add", "title": "reflash", "spec": "s", "tier": "light",
                            "resources": ["board"], "exclusive": True}]) == []
    task = p.db.one("SELECT * FROM tasks WHERE title='reflash'")
    d = Daemon(p.base)
    held = locks.try_take(d._slot_paths("board"), "ttp lock", "held by a command")
    assert held
    mark = locks.reserve_path(p.state / "locks", "board")
    provider = task["provider"] or d.cfg.get("core_provider", "claude")
    d.gates[provider] = bud.Gate(provider, regime="windows")
    assert [t["id"] for t in p.db.ready_tasks()] == [task["id"]]
    d.dispatch()
    assert locks.reserved_by(mark) == f"task #{task['id']}", "a task waiting for its resource lost its reservation"
    held.close()



def test_a_run_that_times_out_after_handing_off_keeps_its_result(env):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("measure", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status) VALUES(?,'worker','codex','',?,'running')",
                 (tid, time.time() - 3600))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"budget_usd": 8.0, "timeout_s": 3600}))
    (run_dir / "output.jsonl").write_text("")
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "measured 42"}))
    d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                 {"rc": -15, "started": time.time() - 3600, "ended": time.time(), "stopped": "timeout"})
    assert p.db.task(tid)["status"] == "done", "a finished result was thrown away on a timeout"
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "ok"
    assert not any("failed or stalled" in r for r in bud.evaluate(p.db, p.config(), "codex", []).reasons)



def test_dispatch_caps_tasks_on_a_shared_resource_and_fills_slots_with_other_work(env, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    p.set_config("resources", {"board": 1})
    assert coord.apply(p, [{"type": "task_add", "title": f"measure {i}", "spec": "s", "tier": "light",
                            "resources": ["board"], "priority": 1} for i in range(4)] +
                       [{"type": "task_add", "title": "docs", "spec": "s", "tier": "light", "priority": 3}]) == []
    d = Daemon(p.base)
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append(k["task"]["title"]) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda task: (str(p.root), None))
    provider = d.cfg.get("core_provider", "claude")
    d.gates[provider] = bud.Gate(provider, regime="windows", max_parallel=6)
    d.dispatch()
    assert sorted(started) == ["docs", "measure 0", "measure 1"], started
    held = p.db.q("SELECT status FROM tasks WHERE title IN ('measure 2', 'measure 3')")
    assert [t["status"] for t in held] == ["queued", "queued"]



def test_the_waste_limit_scales_with_parallel_workers(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    # Three workers side by side each lost a run to a timeout: not a loop.
    for task in (1, 2, 3):
        p.db.x("INSERT INTO runs(task,role,provider,status,started,ended,cost_usd) "
               "VALUES(?,'worker','claude','timeout',?,?,4.0)", (task, now - 3000, now - 60))
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert not any("failed or stalled" in r for r in g.reasons), g.reasons
    # One task failing over and over is a loop, however many workers the project may run; so are
    # new tasks failing one after another.
    for tasks in ((1, 1, 1), (1, 2, 3)):
        p.db.x("DELETE FROM runs")
        for i, task in enumerate(tasks):
            p.db.x("INSERT INTO runs(task,role,provider,status,started,ended,cost_usd) "
                   "VALUES(?,'worker','claude','timeout',?,?,4.0)", (task, now - 3000 + i * 900, now - 2200 + i * 900))
        g = bud.evaluate(p.db, p.config(), "claude", [], now)
        assert g.level == "red" and any("failed or stalled" in r for r in g.reasons), (tasks, g.reasons)
        assert g.numbers["waste_limit"] == 8.0, g.numbers



def test_a_lost_run_whose_hand_off_stood_is_not_runaway_waste(env, tmp_path):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tids, _ = _lost_deep_runs(p, tmp_path, d.boot, handoff={"status": "done", "summary": "measured"})
    d.reap_runs()
    assert all(p.db.task(t)["status"] == "done" for t in tids)
    g = bud.evaluate(p.db, p.config(), "fake", [])
    assert g.level != "red" and g.numbers["waste_1h"] == 0, g.reasons



def test_a_passed_probe_held_by_the_gate_is_logged_as_held(env, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp import daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    tid = p.db.add_task("measure", "s", kind="work", tier="light", origin="user",
                        not_before=time.time() + 3600)
    p.db.update_task(tid, result=json.dumps({"status": "waiting", "retry_when": "true"}))
    d.gates["fake"] = bud.Gate(provider="fake", level="red", allow_new_work=False)
    d.probe_waiting()
    d._probes[tid][0].wait(10)
    d.probe_waiting()
    logged = (p.logs / "daemon.log").read_text()
    assert f"task {tid} retry_when probe passed; held by gate red" in logged, logged[-500:]



def test_review_tier_follows_the_size_and_risk_of_the_diff(env):
    from ttp.budget import review_tier
    cfg = {"review": {"light_max_lines": 60, "risky_paths": ["src/state/*"]}}
    assert review_tier({"README.md": 400, "docs/guide.rst": 90}, cfg) == "light"
    assert review_tier({"src/app.py": 40, "README.md": 300}, cfg) == "light"
    assert review_tier({"src/app.py": 40, "tests/test_app.py": 21}, cfg) == "standard"
    assert review_tier({"src/state/db.py": 2}, cfg) == "standard"
    assert review_tier({"assets/logo.png": None}, cfg) == "standard"
    assert review_tier({"src/app.py": 60}, {}) == "light"
    assert review_tier({"CMakeLists.txt": 81}, {}) == "standard"
    assert review_tier({"docs/state/guide.md": 1}, {"review": {"risky_paths": ["docs/state/*"]}}) == "standard"



def test_review_tier_light_paths_is_an_allow_list(env):
    from ttp.budget import review_tier
    cfg = {"review": {"light_paths": ["tests/*", "scripts/*.sh"], "risky_paths": ["tests/state/*"]}}
    assert review_tier({"tests/test_app.py": 20, "scripts/run.sh": 5, "README.md": 300}, cfg) == "light"
    assert review_tier({"docs/guide.md": 500}, cfg) == "light"
    assert review_tier({"tests/test_app.py": 20, "src/app.py": 1}, cfg) == "standard"
    assert review_tier({"scripts/run.sh": 81}, cfg) == "standard"
    assert review_tier({"tests/state/db.py": 1}, cfg) == "standard"
    assert review_tier({"src/app.py": 1}, {"review": {"light_paths": []}}) == "light"



def test_review_pick_names_the_rule_and_keeps_risky_code_standard(env):
    from ttp.budget import review_pick
    from ttp.project import DEFAULT_CONFIG
    cfg = {"review": DEFAULT_CONFIG["review"]}
    # Tests, docs, changesets and template wording only: light at any size.
    assert review_pick({"tests/test_db.py": 900, ".changeset/x.md": 5, "template/prompts/worker.md": 300}, cfg) \
        == ("light", "tests and docs only")
    assert review_pick({"src/app_test.go": 400, "web/app.test.js": 200}, cfg)[0] == "light"
    assert review_pick({"README.md": 900}, cfg) == ("light", "docs only")
    # Small code outside the risky names: light up to 80 lines, standard above.
    assert review_pick({"src/app.py": 50, "tests/test_app.py": 30}, cfg) == ("light", "80 lines <= 80")
    assert review_pick({"src/app.py": 51, "tests/test_app.py": 30}, cfg) == ("standard", "81 lines > 80")
    # State, database, push, spend and upgrade code: standard however small.
    for path in ("runtime/ttp/db.py", "runtime/ttp/push.py", "runtime/ttp/pushq.py", "runtime/ttp/budget.py",
                 "src/billing.py", "app/localspend.py", "src/state.py", "src/game_state.rs", "migrations/0001.sql",
                 "src/schema.sql", "src/upgrade.sh", "src/user_db.py", "plugins/tt-project/runtime/ttp/release.py",
                 "plugins/tt-project/runtime/ttp/globalcap.py", "src/spend_cap.py"):
        assert review_pick({path: 1}, cfg) == ("standard", f"risky name {path}"), path
    for path in ("plugins/tt-project/runtime/ttp/release.py", "plugins/tt-project/runtime/ttp/globalcap.py"):
        assert review_pick({path: 30}, cfg) == ("standard", f"risky name {path}"), path
    # The cap glob does not catch names that only contain "cap".
    for path in ("src/capture.py", "src/escape.py", "src/caption.js"):
        assert review_pick({path: 1}, cfg)[0] == "light", path
    assert review_pick({"src/db.py": 1}, {"review": {"risky_names": []}})[0] == "light"
    assert review_pick({"src/app.py": 1}, {"review": {"risky_names": ["app*"]}})[0] == "standard"
    assert review_pick({"tests/test_x.py": 1}, {"review": {"risky_paths": ["tests/*"]}}) \
        == ("standard", "risky path tests/test_x.py")
    assert review_pick({"src/logo.png": None}, cfg) == ("standard", "binary file")



@pytest.mark.parametrize("rc", ["1", "killed"])
def test_failed_checks_wake_the_task_at_its_own_tier_with_their_output(env, monkeypatch, tmp_path, rc):
    p = make(env)
    from ttp import budget as bud, daemon as dmod
    from ttp.prompts import worker_task
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    tid, _ = _checks_wait_task(p, tmp_path, DONE_ON_PASS, rc=rc)
    _settle_probe(d, tid)
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert _ready(p, tid) and task["status"] == "queued" and task["attempts"] == 0
    assert bud.wake_tier(task["tier"], res) == "standard", res
    prompt = worker_task(p, task, str(p.root), None, wake={"tier": "standard", "escalated": True})
    assert "FAILED tests/test_x.py::test_parses_empty" in prompt
    assert ("killed" if rc == "killed" else "exit 1") in res["woke"]
    assert "mechanical step" not in prompt and "check whether the wait is over" not in prompt



def test_failed_checks_without_on_pass_also_wake_at_the_task_tier(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import budget as bud, daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    tid, _ = _checks_wait_task(p, tmp_path, rc="2")
    p.db.update_task(tid, result=json.dumps({**json.loads(p.db.task(tid)["result"]), "next_step": "push"}))
    _settle_probe(d, tid)
    assert bud.wake_tier("standard", json.loads(p.db.task(tid)["result"])) == "standard"



@pytest.mark.parametrize("change, why", [
    ("moved", "no longer the head"), ("dirty", "no longer the head"), ("no_rc", None), ("no_on_pass", None),
    ("waiting", "must be done or needs_review"), ("retry", "cannot carry retry_when"),
    ("not_handoff", "not the hand-off the task waits on")])
def test_an_unclear_checks_wait_falls_back_to_a_light_wake(env, monkeypatch, tmp_path, change, why):
    p = make(env)
    from ttp import budget as bud, daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    on_pass = {"waiting": {"status": "waiting", "summary": "x"},
               "retry": {**DONE_ON_PASS, "retry_when": "true"},
               "no_on_pass": None}.get(change, DONE_ON_PASS)
    tid, path = _checks_wait_task(p, tmp_path, on_pass, rc=None if change == "no_rc" else "0")
    run_dir = tmp_path / f"run-{tid}"
    if change == "moved":
        subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "more"], check=True, capture_output=True)
    elif change == "dirty":
        (path / "fix.txt").write_text("changed after the checks\n")
    elif change == "not_handoff":
        (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "x", "on_pass": DONE_ON_PASS}))
    _settle_probe(d, tid)
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert _ready(p, tid) and task["status"] == "queued", (task["status"], res)
    assert bud.wake_tier(task["tier"], res) == "light", res
    assert res["woke"].startswith("probe passed") and (why is None or why in res["woke"]), res["woke"]
    assert len(p.db.q("SELECT id FROM runs WHERE task=?", (tid,))) == 1



def test_result_waits_wake_at_light_whatever_they_ask_unless_escalated(env):
    from ttp import budget as bud
    long_step = "read the checks, then fix what failed and push " * 10
    for probe in ("/opt/x/bin/ttp checks --result /tmp/run-1", "ttp push --result '/tmp/a b/push-1.json'",
                  "ttp push --result /tmp/push-1.json --pushed", "ttp push --pushed --result /tmp/push-1.json"):
        hand = {"status": "waiting", "summary": "x", "waiting_for": "my checks", "retry_when": probe}
        for asks in ({"wake_tier": "standard"}, {"wake_tier": "deep", "next_step": long_step}, {}):
            assert bud.wake_tier("deep", {**hand, **asks}) == "light", (probe, asks)
        # The daemon escalates a failed result (failed checks, a push that did not land): the task's own tier.
        assert bud.wake_tier("standard", {**hand, "wake_tier": "standard", "escalated_wake": True}) == "standard"
    assert bud.result_wait("ttp push --result '/tmp/a b/push-1.json'") == ("push", "/tmp/a b/push-1.json")
    # A compound or other command is not a result wait: it keeps the tier it asks for.
    for probe in ("ttp checks --result /tmp/run-1 && test -e /tmp/x", "ttp checks --result", "ttp ci --repo o/r",
                  "ttp checks --result /tmp/run-1 --pushed", "mytool checks --result /tmp/run-1", "'unclosed"):
        assert bud.result_wait(probe) is None, probe
        assert bud.wake_tier("standard", {"status": "waiting", "retry_when": probe, "wake_tier": "standard"}) \
            == "standard", probe


def _push_wait_task(p, tmp_path, marker_body, tier="standard", pushed=False):
    """A code task waiting (asking for a standard wake) on a detached push whose marker holds `marker_body`;
    `pushed`: its probe is `ttp push --result <marker> --pushed`."""
    from ttp import push
    now = time.time()
    tid = p.db.add_task("land the fix", "s", kind="code", tier=tier, origin="user")
    marker = tmp_path / f"push-{tid}.json"
    marker.write_text(json.dumps({"target": "main", "log": str(tmp_path / "push.log"), **marker_body}))
    (tmp_path / "push.log").write_text("ttp push: rebasing\nerror: the remote rejected the push\n")
    p.db.update_task(tid, status="queued", not_before=now + 3600, result=json.dumps(
        {"status": "waiting", "summary": "pushing", "waiting_for": "my push", "retry_after_s": 900,
         "retry_when": push.result_probe(marker, p) + (" --pushed" if pushed else ""), "wake_tier": "standard",
         "next_step": "report the push", "waiting_since": now - 60}))
    return tid


@pytest.mark.parametrize("pushed", [False, True])
@pytest.mark.parametrize("body, escalated", [
    ({"status": "pushed", "sha": "a" * 40}, False),
    ({"status": "failed", "exit": 4, "reason": "a check failed"}, True),
    ({"kind": "batch", "outcome": "landed", "phase": "finished"}, False),
    ({"kind": "batch", "outcome": "nothing", "phase": "finished"}, False),
    ({"kind": "batch", "outcome": "conflict", "phase": "finished", "message": "rebase stopped"}, True),
    # its process died (killed, crashed or rebooted) before it wrote an outcome: its lock is free
    ({"kind": "batch", "status": "running", "phase": "push", "lock": "LOCK"}, True)])
def test_a_finished_push_result_wakes_at_light_and_a_push_that_did_not_land_at_the_task_tier(
        env, monkeypatch, tmp_path, body, escalated, pushed):
    p = make(env)
    from ttp import budget as bud, daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    if body.get("lock") == "LOCK":
        (tmp_path / "run.lock").write_text("")
        body = {**body, "lock": str(tmp_path / "run.lock")}
    # With --pushed a push that did not land (or had nothing to push) makes the probe exit 3: a final
    # result, never a broken probe.
    tid = _push_wait_task(p, tmp_path, body, pushed=pushed)
    _settle_probe(d, tid)
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert _ready(p, tid) and task["status"] == "queued", (task["status"], res)
    assert bud.wake_tier(task["tier"], res) == ("standard" if escalated else "light"), res
    assert bool(res.get("escalated_wake")) == escalated, res
    assert ("its push did not land" in res["woke"]) == escalated, res["woke"]
    assert "probe broken" not in res["woke"], res["woke"]
    if escalated:
        assert ("a check failed" if "reason" in body else "batch conflict (rebase stopped)" if "message" in body
                else "batch ended without an outcome") in res["woke"], res["woke"]


def test_a_running_batch_without_an_outcome_is_not_read_as_a_push_that_did_not_land(tmp_path):
    from ttp import locks, push
    lock, marker = tmp_path / "run.lock", tmp_path / "batch-1.json"
    marker.write_text(json.dumps({"kind": "batch", "status": "running", "phase": "push", "lock": str(lock)}))
    held = locks.try_take([lock], "batch 1")
    try:
        assert push.unpushed(marker) == ""
    finally:
        held.close()
    assert push.unpushed(marker).startswith("batch ended without an outcome")


def test_a_pending_checks_or_push_result_never_wakes_the_task(env, tmp_path):
    p = make(env)
    from ttp import daemon as dmod
    from ttp.runner import proc_start
    d = dmod.Daemon(p.base)
    push_tid = _push_wait_task(p, tmp_path, {"status": "queued", "queued": time.time()})
    checks_tid, _ = _checks_wait_task(p, tmp_path, DONE_ON_PASS, rc=None)
    run_dir = tmp_path / f"run-{checks_tid}"
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        (run_dir / "checks.pid").write_text(json.dumps({"pid": sleeper.pid, "started": proc_start(sleeper.pid)}))
        for _ in range(2):
            for tid in (push_tid, checks_tid):   # their timers ran out while the results are pending
                p.db.update_task(tid, not_before=time.time() - 1)
            d.probe_waiting()
            for tid in (push_tid, checks_tid):
                if tid in d._probes:
                    d._probes[tid][0].wait(_load_scaled(60))
            d.probe_waiting()
        for tid in (push_tid, checks_tid):
            task = p.db.task(tid)
            assert not _ready(p, tid) and "woke" not in json.loads(task["result"]), (tid, task["result"])
            assert "probe says not yet" in (task["blocked_reason"] or ""), dict(task)
        assert not p.db.q("SELECT id FROM runs WHERE task=? AND status IS NULL", (push_tid,))
    finally:
        sleeper.kill()
        sleeper.wait()



def test_a_wait_on_another_projects_reply_must_use_the_ack_wait_probe(env, tmp_path, monkeypatch):
    from ttp import budget as bud
    from ttp import project
    from ttp.daemon import Daemon
    p = make(env)
    monkeypatch.setattr(project, "load_registry", lambda: {"projects": {p.name: {}, "fleet-sim": {}}})
    d = Daemon(p.base)
    tid = p.db.add_task("needs fleet data", "s", kind="work", tier="standard", origin="user")
    t = _reply_wait_hand_off(p, d, tmp_path, tid, waiting_for="a reply from fleet-sim to note 0123abcd",
                             retry_when="exit 1")
    res = json.loads(t["result"])
    assert t["status"] == "queued" and t["attempts"] == 1 and res["status"] == "failed", dict(t)
    assert res["summary"].startswith("hand-off refused: it waits on another project's reply"), res["summary"]
    assert "ttp ack-wait probe --from fleet-sim --hash <note id>" in res["summary"]
    # With the probe it is a wait, and its wakes run at light whatever it asked for.
    t = _reply_wait_hand_off(p, d, tmp_path, tid, waiting_for="a reply from fleet-sim to note 0123abcd",
                             retry_when="ttp ack-wait probe --from fleet-sim --hash 0123abcd", wake_tier="standard",
                             on_pass_cmd="ttp ack-wait settle --from fleet-sim --hash 0123abcd")
    res = json.loads(t["result"])
    assert t["status"] == "queued" and res["status"] == "waiting" and t["attempts"] == 1, dict(t)
    assert bud.wake_tier("standard", res) == "light"
    assert bud.wake_tier("standard", dict(res, escalated_wake=True)) == "standard"
    # Waits on anything else, or on the tt-project fix (an upstream note), are not touched.
    for extra in ({"waiting_for": "CI to respond", "retry_when": "ttp ci --repo o/r --branch b"},
                  {"waiting_for": "a reply to the upstream note", "retry_when": "ttp upstream --fixed 0123abcd"}):
        t = _reply_wait_hand_off(p, d, tmp_path, tid, **extra)
        assert json.loads(t["result"])["status"] == "waiting", extra



def test_failed_or_silent_runs_do_not_say_a_plan_stopped_reporting_windows(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 6 * 3600, "claude", "a", "seven_day", 40.0, now + 3 * 86400))
    # A night of runs a sleeping laptop cut short: timed out, lost or failed, each booked an estimate.
    for i, status in enumerate(("timeout", "lost", "failed", "stalled", "timeout")):
        ts = now - (5 - i) * 3600
        p.db.x("INSERT INTO ledger(ts,provider,account,source,usd,estimated) VALUES(?,?,?,?,?,1)",
               (ts, "claude", "a", "coordinator", 2.0))
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd,cost_estimated) "
               "VALUES('coordinator','claude',?,?,?,2.0,1)", (ts - 60, ts, status))
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "windows", g.reasons
    assert not any("stopped reporting" in r for r in g.reasons), g.reasons
    # Successful runs without a reading still do.
    for m in (30, 20):
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd) VALUES('worker','claude',?,?,'ok',1.0)",
               (now - m * 60 - 60, now - m * 60))
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and any("stopped reporting" in r for r in g.reasons), (g.regime, g.reasons)



def test_a_resume_that_cannot_start_falls_back_to_a_fresh_start_at_no_attempt(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    from ttp.providers.fake import Fake
    _sessions(env, monkeypatch)
    monkeypatch.setattr(Fake, "session_saved", lambda self, sid, cwd, env=None: True)   # gone by the time it runs
    d = Daemon(p.base)
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, attempts=1)
    _lost_with_session(p, d, tmp_path, tid, session="vanished")
    d.dispatch()
    _finish_runs(p, d)
    run = p.db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))
    assert run["status"] == "failed" and json.loads(run["note"])["resumes"]["session"] == "vanished"
    assert not bud.wasted(run), "a resume that never started counted toward the runaway guard"
    t = p.db.task(tid)
    assert t["status"] == "queued" and t["attempts"] == 1 and not t["not_before"], dict(t)
    assert d._resumable(t, "fake") is None
    d.dispatch()
    run = p.db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))
    assert "resumes" not in json.loads(run["note"]) and "## Spec" in (pathlib.Path(run["dir"]) / "prompt.md").read_text()
    _finish_runs(p, d)
    assert p.db.task(tid)["status"] == "done" and p.db.task(tid)["attempts"] == 2



# A Codex stand-in whose prompt cache is keyed to the thread, as Codex's prompt_cache_key is: a
# fresh thread reads nothing, a resumed one reads what that thread sent before. Never a real agent.
CODEX_THREAD_CACHE = r'''#!{python}
import json, os, sys, uuid
from pathlib import Path
argv = sys.argv[1:]
state = Path(os.environ["FAKE_CODEX_STATE"])
state.mkdir(parents=True, exist_ok=True)
(state / "argv.jsonl").open("a").write(json.dumps(argv) + "\n")
prompt = sys.stdin.read()
system = next((a for a in argv if a.startswith("developer_instructions=")), "")
thread = argv[argv.index("resume") + 1] if "resume" in argv else str(uuid.uuid4())
hist = state / (thread + ".json")
sent = json.loads(hist.read_text()) if hist.exists() else 0
fresh = (len(system) + len(prompt)) // 4 if not sent else len(prompt) // 4
total = sent + fresh
day = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "10" / "13"
day.mkdir(parents=True, exist_ok=True)
(day / ("rollout-2026-10-13T10-00-00-" + thread + ".jsonl")).write_text("{{}}\n")
hist.write_text(json.dumps(total + 50))   # the answer joins the thread's history
for ev in ({{"type": "thread.started", "thread_id": thread}}, {{"type": "turn.started"}},
           {{"type": "item.completed", "item": {{"id": "i1", "type": "agent_message", "text": '{{"actions": []}}'}}}},
           {{"type": "turn.completed", "usage": {{"input_tokens": total, "cached_input_tokens": sent,
                                                "output_tokens": 50}}}}):
    print(json.dumps(ev), flush=True)
'''



def _codex_coordinator(env, monkeypatch, tmp_path):
    """A daemon whose coordinator turns go to CODEX_THREAD_CACHE, and a turn runner returning
    (run row, argv, prompt.md) of a finished turn."""
    from ttp.daemon import Daemon
    from ttp.providers import base
    from ttp.providers import codex as codex_provider
    from ttp import coordinator as coord
    exe = tmp_path / "codex"
    exe.write_text(CODEX_THREAD_CACHE.format(python=sys.executable))
    exe.chmod(0o755)
    monkeypatch.setenv("FAKE_CODEX_STATE", str(tmp_path / "codex-state"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(codex_provider.Codex, "binary", lambda self: str(exe))
    for args, text in ((("exec", "--help"), CODEX_EXEC_HELP), (("features", "list"), ""),
                       (("login", "--help"), "")):
        monkeypatch.setitem(base._CLI_OUTPUT, (str(exe), *args), text)
    p = make(env)
    p.set_config("core_provider", "codex")
    d = Daemon(p.base)

    def turn(digest="EVENTS", system=None):
        head, context = coord.prompt_parts(p)
        rid = d.start_run("coordinator", digest, "codex", "light", str(p.base), read_only=True,
                          schema=coord.ACTIONS_SCHEMA, system=system or head, context=context, cache_ttl="auto")
        exit_file = p.runs / str(rid) / "exit.json"
        deadline = time.time() + 60
        while not exit_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert exit_file.exists(), (p.runs / str(rid) / "runner.log").read_text()
        d.reap_runs()
        argv = json.loads((tmp_path / "codex-state" / "argv.jsonl").read_text().splitlines()[-1])
        return p.db.one("SELECT * FROM runs WHERE id=?", (rid,)), argv, (p.runs / str(rid) / "prompt.md").read_text()
    return p, d, turn



def test_codex_coordinator_turns_continue_the_warm_session_and_read_the_cache(env, monkeypatch, tmp_path):
    from ttp import budget as bud
    from ttp import coordinator as coord
    p, d, turn = _codex_coordinator(env, monkeypatch, tmp_path)
    first, argv, prompt = turn()
    assert first["status"] == "ok" and "resume" not in argv and prompt == "EVENTS"
    assert json.loads(first["note"])["turn"]["thread"] == {**json.loads(first["note"])["turn"]["thread"],
                                                           "turn": 1, "why": "no earlier turn"}
    for k in range(2, 5):
        r, argv, prompt = turn(f"EVENTS {k}")
        thread = json.loads(r["note"])["turn"]["thread"]
        assert argv[-3:] == ["resume", first["session_id"], "-"], argv
        assert r["status"] == "ok" and r["session_id"] == first["session_id"] and thread["turn"] == k, thread
        assert prompt == coord.THREAD_CONTINUED + f"\n\nEVENTS {k}", "a continued turn is told its history is stale"
        assert "except any the digest below lists as rejected" in coord.THREAD_CONTINUED, "rejected actions were not applied"
        assert any(a.startswith("developer_instructions=") for a in argv[:argv.index("resume")]), \
            "the same stable prefix goes with it"
        assert r["cache_read_tokens"] > 0 and r["cache_read_tokens"] >= r["input_tokens"], r
    c = bud.coordinator_cache(p.db)["24h"]
    assert c["continued"]["turns"] == 3 and c["continued"]["hit_pct"] >= 50 and c["continued"]["miss_turns"] == 0
    # A changed prefix (charter or memory) starts a session of its own.
    head = coord.prompt_parts(p)[0]
    r, argv, _ = turn(system=head + "\nnew charter line")
    assert "resume" not in argv and json.loads(r["note"])["turn"]["thread"]["why"] == "prefix changed"
    assert r["session_id"] != first["session_id"] and r["cache_read_tokens"] == 0



def test_codex_coordinator_cache_before_and_after_session_reuse(env, monkeypatch, tmp_path):
    """The before/after fixture: the same five turns, each in a fresh session (thread_max_turns 0,
    the old behaviour) and then continuing one session, measured by coordinator_cache."""
    from ttp import budget as bud
    p, d, turn = _codex_coordinator(env, monkeypatch, tmp_path)
    d.cfg["coordinator"]["thread_max_turns"] = 0
    for k in range(5):
        r, argv, _ = turn(f"EVENTS {k}")
        assert "resume" not in argv and json.loads(r["note"])["turn"]["thread"]["why"] == "off"
    before = bud.coordinator_cache(p.db)["24h"]
    assert before["hit_pct"] == 0 and before["states"]["zero"] == 5 and before["continued"]["turns"] == 0
    p.db.x("DELETE FROM runs")
    d.cfg["coordinator"]["thread_max_turns"] = 8
    for k in range(5):
        turn(f"EVENTS {k}")
    after = bud.coordinator_cache(p.db)["24h"]
    assert after["continued"]["turns"] == 4 and after["states"]["zero"] == 1
    assert after["hit_pct"] >= 60 and after["continued"]["hit_pct"] >= 75, after



def test_a_failed_resume_that_printed_events_but_no_tokens_is_free(env, monkeypatch):
    from ttp import budget as bud
    out = _codex_events({"type": "error", "message": f"no rollout found for thread id {THREAD}"})
    p, run, task, _, _ = _cli_run(env, monkeypatch, "codex", out, rc=1, help_text=CODEX_EXEC_HELP, resume=THREAD,
                                  note={"resumes": {"run": 1, "session": THREAD}})
    assert run["status"] == "failed" and run["cost_usd"] == 0, "a resume that never started was charged"
    assert not bud.wasted(run) and json.loads(run["note"])["not_waste"] == "resume"
    assert task["status"] == "queued" and task["attempts"] == 0, dict(task)
    # The same output from a run that resumed nothing is still priced as cut off.
    _, run, task, _, _ = _cli_run(env, monkeypatch, "codex", out, rc=1)
    assert run["status"] == "failed" and run["cost_usd"] > 0



def test_idle_slot_wake_ignores_open_asks(env):
    from ttp import budget as bud
    from ttp.daemon import starve_state
    p = make(env)
    gate = bud.Gate(provider="fake", regime="windows", numbers={"plan": [{"headroom": 40.0}]})
    p.db.post("out", "which option?", kind="ask")
    st = starve_state(p.db, p.config(), gate.as_dict(), time.time())
    assert st and st["wait"] > 0, "an open ask must not hold back idle-slot wakes"



def test_status_shows_the_idle_slot_wake_when_it_comes_first(env, monkeypatch):
    from ttp import budget as bud
    from ttp.web import health
    prov = "fake"
    plan = [{"window": "seven_day", "utilization": 50.0, "resets_at": time.time() + 36000, "hours_left": 10.0,
             "headroom": 40.0, "per_worker_per_h": 1.0}]
    gate = bud.Gate(provider=prov, regime="windows", max_parallel=6, numbers={"plan": plan})
    p, d, clock, starts = _wake_setup(env, monkeypatch, gate)
    assert p.config().get("core_provider") == prov
    c = p.config()["coordinator"]
    shown = health(p, p.db, now=clock[0])["coordinator"]["idle_wake"]
    assert shown == clock[0] + float(c["starve_wake_s"]) < clock[0] + float(c["idle_wake_s"]), shown
    fired = _first_turn(d, clock, starts, shown + 3600)
    assert fired is not None and 0 < fired - shown <= 30, (fired, shown)



def test_status_says_when_the_budget_gate_holds_the_idle_wake(env, monkeypatch):
    from ttp import budget as bud
    from ttp.web import health
    gate = bud.Gate(provider="fake", level="yellow", allow_optional=False)
    p, d, clock, starts = _wake_setup(env, monkeypatch, gate)
    idle_s = float(p.config()["coordinator"]["idle_wake_s"])
    h = health(p, p.db, now=clock[0] + 2 * idle_s)
    assert h["coordinator"]["idle_wake"] is None, "status showed a wake the gate holds back"
    assert h["coordinator"]["idle_held"] == "held by the budget gate (yellow)"
    assert "idle check is held by the budget gate" in h["why_idle"], h["why_idle"]
    assert _first_turn(d, clock, starts, clock[0] + 3 * idle_s) is None, "the daemon woke through the gate"



def test_deferred_task_does_not_hold_back_idle_slot_wake(env):
    from ttp import budget as bud
    from ttp import coordinator as coord
    from ttp.daemon import starve_state
    p = make(env)
    gate = bud.Gate(provider="fake", regime="windows", numbers={"pace": [{"need_per_h": 10, "burn_per_h": 1}]})
    now = time.time()
    assert coord.apply(p, [{"type": "task_add", "title": "later", "spec": "s", "start_when": "exit 1"},
                           {"type": "task_add", "title": "in 3 days", "spec": "s", "start_after": "3d"}]) == []
    assert starve_state(p.db, p.config(), gate.as_dict(), now), "deferred tasks do not start by themselves soon"
    assert coord.apply(p, [{"type": "task_add", "title": "ready", "spec": "s"}]) == []
    assert starve_state(p.db, p.config(), gate.as_dict(), now) is None, "ready queued work still holds the wake"



def test_a_plan_window_red_marker_commits_with_its_alert_or_not_at_all(env, monkeypatch):
    """A marker saved without its alert would silence that plan window's red alert for good."""
    import sqlite3
    import time as _t
    from ttp import budget as bud
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.update_gates()
    p.db.spend("fake", 150.0, "task:1")
    evaluate = bud.evaluate

    def at_line(*a, **kw):
        g = evaluate(*a, **kw)
        g.numbers = {**(g.numbers or {}), "limit": 90,
                     "plan": [{"window": "5h", "utilization": 95, "resets_at": _t.time() + 3600}]}
        return g
    monkeypatch.setattr(bud, "evaluate", at_line)
    post = d.p.db.post

    def locked(*a, **kw):
        if str(kw.get("ref", "")).startswith("budget:"):
            raise sqlite3.OperationalError("database is locked")
        return post(*a, **kw)
    monkeypatch.setattr(d.p.db, "post", locked)
    with pytest.raises(sqlite3.OperationalError):
        d.update_gates()
    assert not p.db.kv("budget_red_sent")
    monkeypatch.setattr(d.p.db, "post", post)
    d.update_gates()
    assert len(p.db.q("SELECT id FROM messages WHERE ref='budget:fake'")) == 1
    assert "5h" in p.db.kv("budget_red_sent")["fake"]



def test_stats_tools_reports_the_file_read_share_of_tool_output(env, tmp_path, capsys):
    from ttp import budget as bud
    p = make(env)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls = [("a", "Bash", {"command": "sed -n '1,400p' src/x.py"}, "r" * 400),
             ("b", "Bash", {"command": "cd src && cat -n y.py 2>&1"}, [{"type": "text", "text": "c" * 200}]),
             ("c", "Read", {"file_path": "z.py"}, "z" * 100),
             ("d", "Bash", {"command": "grep -n foo src/x.py"}, "g" * 100),
             ("e", "Bash", {"command": "sed -i 's/a/b/' src/x.py"}, ""),
             ("f", "Bash", {"command": "git log --oneline | head -5"}, "l" * 200)]
    with open(run_dir / "output.jsonl", "w") as f:
        for i, name, inp, out in calls:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": i, "name": name, "input": inp}]}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": i, "content": out}]}}) + "\n")
        f.write("not json\n")
    p.db.x("INSERT INTO runs(task,role,provider,model,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?,?)",
           (None, "worker", "claude", "opus", time.time(), "done", str(run_dir), "x"))
    s = bud.tool_output_stats(p.db, p.runs, 7)
    cats = {g["category"]: g for g in s["categories"]}
    assert s["runs"] == 1 and s["chars"] == 1000 and s["file_read_share"] == 0.7
    assert cats["file read (Bash)"]["chars"] == 600 and cats["file read (Bash)"]["calls"] == 2
    assert cats["file read (Read)"]["chars"] == 100 and cats["search"]["chars"] == 100
    assert cats["other bash"]["chars"] == 200 and cats["other bash"]["calls"] == 2
    assert "file reads 70%" in bud.tool_output_text(s)
    from ttp import cli
    cli.main(["stats", "demo", "--tools"])
    assert "file read (Bash): 2 calls" in capsys.readouterr().out



def test_idle_slot_wake_backs_off_to_long_waits_while_every_queued_task_is_gated(env, monkeypatch):
    from ttp import budget as bud
    from ttp import coordinator as coord
    from ttp.daemon import starve_state
    plan = [{"window": "seven_day", "utilization": 50.0, "resets_at": time.time() + 36000, "hours_left": 10.0,
             "headroom": 40.0, "per_worker_per_h": 1.0}]
    gate = bud.Gate(provider="fake", regime="windows", max_parallel=6, numbers={"plan": plan})
    p, d, clock, starts = _wake_setup(env, monkeypatch, gate)
    c = p.config()["coordinator"]
    assert coord.apply(p, [{"type": "task_add", "title": "on a probe", "spec": "s", "start_when": "exit 1"},
                           {"type": "task_add", "title": "in 3 days", "spec": "s", "start_after": "3d"}]) == []
    _no_events(p)
    t0 = clock[0]
    while clock[0] < t0 + 86400:
        d.maybe_coordinate()
        if starts and p.db.kv("last_coordinator_turn") == clock[0]:
            # Other turns (messages, events) in between start each wake's fingerprint afresh.
            p.db.set_kv("idle_wake", {})
        clock[0] += 60
    assert starts and starts[0] - t0 >= float(c["idle_wake_s"]), \
        "probe and time gates wake the coordinator themselves: no short idle-slot wakes meanwhile"
    assert len(starts) <= 5, f"{len(starts)} idle-slot turns in a day for work that only waits on its gates"
    # The gate opens and the queue empties: the idle-slot wake is capped at the idle wake again.
    p.db.x("UPDATE tasks SET status='done'")
    st = starve_state(p.db, p.config(), gate.as_dict(), clock[0])
    assert st and st["wait"] <= float(c["idle_wake_s"]), st
    # A new task starts the short wait afresh.
    p.db.x("UPDATE tasks SET status='queued'")
    assert coord.apply(p, [{"type": "task_add", "title": "later", "spec": "s", "start_when": "exit 1"}]) == []
    st = starve_state(p.db, p.config(), gate.as_dict(), clock[0])
    assert st and st["wait"] == float(c["idle_wake_s"]), st



def test_a_worker_that_rereads_too_much_context_is_told_once_to_split(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    from ttp.project import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["budget"]["split_reread_tokens"] == {"light": 2_000_000, "standard": 4_000_000,
                                                               "deep": 8_000_000}
    tid = p.db.add_task("long one", "spec", kind="code", tier="standard", origin="user")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def turn(n, cache_read):
        msg = {"type": "assistant", "message": {"id": f"m{n}", "content": [],
                                                "usage": {"input_tokens": 10, "cache_read_input_tokens": cache_read}}}
        with open(run_dir / "output.jsonl", "a") as f:
            f.write(json.dumps(msg) + "\n")

    rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?,?)",
                 (tid, "worker", "claude", "opus", time.time(), "running", str(run_dir), "x"))
    d = Daemon(p.base)
    turn(1, 1_500_000)
    turn(2, 1_000_000)
    d.meter_running()
    assert p.db.one("SELECT cache_read_tokens FROM runs WHERE id=?", (rid,))["cache_read_tokens"] == 2_500_000
    assert not (run_dir / "steer.md").exists(), "below the standard tier's 4 M line"
    assert "split" not in json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"] or "{}")
    stale = p.db.one("SELECT * FROM runs WHERE id=?", (rid,))
    turn(3, 1_000_000)
    d._metered.clear()
    d.meter_running()
    assert not (run_dir / "steer.md").exists(), "3.5 M is still below the standard tier's 4 M line"
    turn(4, 1_000_000)
    d._metered.clear()
    d.meter_running()
    steer = (run_dir / "steer.md").read_text()
    assert "re-read about 4.5 M tokens" in steer and "`continue:`" in steer and "commit" in steer
    # The hand-off it asks for names what a fresh hop would otherwise look up: tests and the next command.
    assert "the test ids you added or touched" in steer and "the exact next command to run" in steer, steer
    split = json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"])["split"]
    assert split["cache_read"] == 4_500_000 and split["limit"] == 4_000_000 and split["ts"] > 0, split
    turn(5, 2_000_000)
    d._metered.clear()
    d.meter_running()
    assert (run_dir / "steer.md").read_text() == steer, "told once per run"
    assert json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"])["split"] == split, "recorded once"
    # The run ends with a hand-off: its note keeps the steer (even from a row read before it) and adds how.
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "half, continue: the rest"}))
    d.finish_run(stale, {"rc": 0})
    row = p.db.one("SELECT note, cache_read_tokens FROM runs WHERE id=?", (rid,))
    assert json.loads(row["note"])["split"] == {**split, "handoff": "done"}
    assert row["cache_read_tokens"] == 6_500_000
    from ttp import budget as bud
    st = bud.reread_stats(p.db, 7)["steered"]
    assert st == {"runs": 1, "after_avg": 2_000_000, "after_max": 2_000_000, "handoffs": {"done": 1}}, st
    p.db.x("UPDATE runs SET status='running' WHERE id=?", (rid,))
    from ttp import hook
    assert "split line" in hook.unread_update(run_dir)[0], "the worker's hook hands it over"
    p.set_config("budget.split_reread_tokens", 0)
    d.cfg = p.config()
    (run_dir / "steer.md").unlink()
    d._metered.clear()
    turn(6, 1)
    d.meter_running()
    assert not (run_dir / "steer.md").exists(), "0 turns it off"



def test_ttp_stats_reports_reread_tokens_per_run_and_per_dollar_by_kind_and_tier(env, capsys):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    code = p.db.add_task("big code change", "s", kind="code", tier="deep", origin="user")
    review = p.db.add_task("review it", "s", kind="review", tier="standard", origin="user")
    for task, role, effort, usd, cr, out, age in ((code, "worker", "max", 6.0, 9_000_000, 100_000, 3600),
                                                  (code, "worker", "max", 2.0, 3_000_000, 20_000, 7200),
                                                  (review, "reviewer", "high", 1.0, 500_000, 10_000, 600),
                                                  (None, "coordinator", "low", 0.1, 20_000, 1_000, 60),
                                                  (code, "worker", "max", 9.0, 50_000_000, 1, 9 * 86400)):
        p.db.x("INSERT INTO runs(task,role,provider,effort,started,ended,status,cost_usd,cache_read_tokens,"
               "output_tokens) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (task, role, "claude", effort, now - age, now - age + 600, "done", usd, cr, out))
    s = bud.reread_stats(p.db, 7, now=now)
    assert s["runs"] == 4 and s["cache_read"] == 12_520_000 and s["usd"] == 9.1, "an old run counted"
    assert s["per_run"] == 3_130_000 and s["per_usd"] == round(12_520_000 / 9.1)
    # 1.252 M weighted re-read units of 1.252 M + 5 * 131 k output units
    assert s["share"] == round(1_252_000 / (1_252_000 + 5 * 131_000), 3)
    top = s["groups"][0]
    assert (top["role"], top["kind"], top["tier"], top["effort"], top["runs"]) == ("worker", "code", "deep", "max", 2)
    assert top["per_run"] == 6_000_000 and top["max"] == 9_000_000
    assert [t["cache_read"] for t in s["top"]] == [9_000_000, 3_000_000, 500_000, 20_000]
    assert s["top"][0]["title"] == "big code change" and s["top"][0]["minutes"] == 10
    monkey = pytest.MonkeyPatch()
    monkey.chdir(p.root)
    try:
        cli_rc = _ttp_main("stats", "--days", "7")
    finally:
        monkey.undo()
    out = capsys.readouterr().out
    assert cli_rc is None and "Context re-reads, last 7 days: 4 runs, $9.10, 12.52 M cache-read tokens" in out
    assert "worker/code/deep/max: 2 runs, $8.00, 12.00 M (6.00 M per run, median 6.00 M, max 9.00 M)" in out
    assert "big code change" in out
    assert "Split steer: no run steered" in out
    for cr, at, how in ((9_000_000, 4_000_000, "done"), (3_000_000, 2_800_000, "waiting")):
        p.db.x("INSERT INTO runs(task,role,provider,effort,started,ended,status,cost_usd,cache_read_tokens,note) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)", (code, "worker", "claude", "high", now - 60, now, "ok", 1.0, cr,
                                              json.dumps({"split": {"ts": now - 30, "cache_read": at, "handoff": how}})))
    p.db.x("INSERT INTO runs(task,role,provider,effort,started,status,cache_read_tokens,note) VALUES(?,?,?,?,?,?,?,?)",
           (code, "worker", "claude", "high", now - 60, "running", 4_000_000,
            json.dumps({"split": {"ts": now - 30, "cache_read": 3_100_000}})))
    s = bud.reread_stats(p.db, 7, now=now)
    assert s["steered"] == {"runs": 3, "after_avg": 2_033_333, "after_max": 5_000_000,
                            "handoffs": {"done": 1, "waiting": 1, "running": 1}}, s["steered"]
    assert ("Split steer: 3 runs steered, re-read after it 2.03 M avg (max 5.00 M); handed off: done 1, "
            "waiting 1, running 1") in bud.reread_text(s)



def test_ttp_stats_leaves_runs_with_no_model_call_out_of_per_run_averages(env, capsys):
    """Failed sign-ins (status auth, or no prompt tokens at all) cost a wake, not context: a burst of
    them must not move per-run or median re-reads, and is reported apart."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    code = p.db.add_task("code change", "s", kind="code", tier="standard", origin="user")

    def run(age, cr, status="done", inp=100, usd=1.0, calls=None):
        note = json.dumps({"cache_telemetry": {"complete": True, "requests": None, "source": "result",
                                               "calls": calls, "first_context": 40_000}}) if calls else None
        p.db.x("INSERT INTO runs(task,role,provider,effort,started,ended,status,cost_usd,input_tokens,"
               "cache_read_tokens,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (code, "worker", "claude", "high", now - age, now - age + 60, status, usd, inp, cr, note))

    run(3600, 2_000_000, calls=20)
    run(3000, 4_000_000, calls=40)
    run(2400, 9_000_000, calls=90)
    clean = bud.reread_stats(p.db, 7, now=now)
    for age in range(10, 174):   # 164 runs that never reached the model
        run(age, 0, status="auth", inp=0, usd=0.0)
    run(5, 50, status="auth", inp=3, usd=0.0)   # an auth status counts as no call whatever it shows
    s = bud.reread_stats(p.db, 7, now=now)
    assert s["runs"] == clean["runs"] == 3 and s["per_run"] == clean["per_run"] == 5_000_000
    assert s["median"] == 4_000_000 and s["no_work"] == 165 and clean["no_work"] == 0
    assert s["calls_per_run"] == 50.0 and s["first_context"] == 40_000
    g = s["groups"][0]
    assert (g["runs"], g["per_run"], g["median"], g["no_work"]) == (3, 5_000_000, 4_000_000, 165)
    assert all(t["cache_read"] for t in s["top"]), "a no-call run listed among the top runs"
    assert "165 runs with no model call left out" in bud.reread_text(s)



def test_ttp_stats_split_at_compares_before_and_after_a_change(env, capsys):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    at = now - 2 * 86400
    code = p.db.add_task("code change", "s", kind="code", tier="standard", origin="user")
    for age, cr, status in ((4 * 86400, 6_000_000, "done"), (3 * 86400, 6_000_000, "done"),
                            (86400, 5_000_000, "done"), (3600, 5_000_000, "done"),
                            *((600 + i, 0, "auth") for i in range(10)), (20 * 86400, 90_000_000, "done")):
        p.db.x("INSERT INTO runs(task,role,provider,effort,started,ended,status,cost_usd,input_tokens,"
               "cache_read_tokens) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (code, "worker", "claude", "high", now - age, now - age + 60, status, 1.0 if cr else 0.0,
                100 if cr else 0, cr))
    s = bud.reread_split(p.db, at, since=at - 7 * 86400, until=now, now=now)
    b, a = s["before"], s["after"]
    assert (b["runs"], b["per_run"], b["no_work"]) == (2, 6_000_000, 0), "the 20-day-old run counted"
    assert (a["runs"], a["per_run"], a["no_work"]) == (2, 5_000_000, 10)
    assert s["per_run_pct"] == -17 and s["groups"][0]["per_run_pct"] == -17
    assert s["groups"][0]["runs"] == (2, 2)
    monkey = pytest.MonkeyPatch()
    monkey.chdir(p.root)
    try:
        assert _ttp_main("stats", "--split-at", str(at), "--days", "7") is None
        out = capsys.readouterr().out
        assert "per run 6.00 M -> 5.00 M (-17%)" in out and "no model call 0 -> 10 (left out)" in out
        assert "worker/code/standard/high: 2 -> 2 runs" in out and "Before:" in out and "After:" in out
        iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(at))
        assert _ttp_main("stats", "--since", iso, "--json") is None
        after = json.loads(capsys.readouterr().out)
        assert after["runs"] == 2 and after["no_work"] == 10 and after["per_run"] == 5_000_000
        with pytest.raises(SystemExit):
            _ttp_main("stats", "--until", "last tuesday")
    finally:
        monkey.undo()



def test_the_budget_day_starts_at_a_fixed_hour_in_its_zone_across_dst(env, monkeypatch):
    from datetime import datetime, timezone
    from ttp import budget as bud
    from ttp import globalcap as gcap
    monkeypatch.setenv("TZ", "UTC")   # the host's zone plays no part
    time.tzset()
    try:
        b = {"day_start": "08:00", "timezone": "America/New_York"}
        utc = lambda *a: datetime(*a, tzinfo=timezone.utc).timestamp()  # noqa: E731
        # Clocks go back on 2026-11-01: 07:00 EST is before the day's start, so today began at
        # 08:00 EDT the day before and is 25 h long.
        start, end = gcap.day_bounds(b, utc(2026, 11, 1, 12, 0))
        assert (start, end) == (utc(2026, 10, 31, 12, 0), utc(2026, 11, 1, 13, 0))
        # Clocks go forward on 2026-03-08: that day is 23 h long.
        start, end = gcap.day_bounds(b, utc(2026, 3, 7, 14, 0))
        assert (start, end) == (utc(2026, 3, 7, 13, 0), utc(2026, 3, 8, 12, 0))
        assert gcap.day_bounds({"day_start": "", "timezone": "UTC"}, utc(2026, 3, 7, 14, 0)) is None
        assert gcap.day_bounds({"day_start": "08:00"}, utc(2026, 3, 7, 7, 0))[0] == utc(2026, 3, 6, 8, 0)   # UTC default
        assert gcap.setting_problems({"timezone": "Mars/Base"}) and gcap.setting_problems({"day_start": "8am"})
        # The project's daily cap counts this budget day, not the last 24 h.
        p = make(env)
        now = utc(2026, 11, 1, 14, 0)          # 09:00 EST: the day began an hour ago
        p.set_config("budget.timezone", "America/New_York")
        p.set_config("budget.day_start", "08:00")
        p.db.spend("fake", 90.0, "task:1", ts=now - 2 * 3600)   # yesterday's budget day
        p.db.spend("fake", 30.0, "task:2", ts=now - 1800)
        g = bud.evaluate(p.db, p.config(), "fake", [], now)
        assert g.numbers["spent_today"] == 30.0 and g.level != "red", (g.level, g.reasons)
        assert g.numbers["day_end"] == utc(2026, 11, 2, 13, 0)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()



def test_the_rolling_global_window_asks_each_machine_once_per_refresh_and_keeps_counting(env, monkeypatch):
    # With the global cap on and no budget day, the window must not move on every 3 s daemon tick:
    # one ssh per machine per REFRESH_S, a bounded local cache, and the remote total still counted.
    p = make(env)
    from ttp import budget as bud
    from ttp import globalcap as gcap
    from ttp.project import register
    register("far", {"host": "box2", "dir": "/srv/far"})
    p.set_config("budget.global_daily_usd", 100)
    b = p.config()["budget"]
    assert b["day_start"] == ""
    key = gcap.account_key("claude", "acct-a")
    asked = []
    monkeypatch.setattr(gcap, "fetch", lambda t, s, e: asked.append((t, s)) or {
        "host": "box2", "rows": [{"provider": "claude", "key": key, "usd": 50.0}], "projects": ["far"]})
    monkeypatch.setattr(gcap, "account_of", lambda prov: "acct-a")

    class Inline:                       # run the background refresh at once, in order
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

        def is_alive(self):
            return False

    monkeypatch.setattr(gcap.threading, "Thread", Inline)
    gcap._LOCAL.clear()
    t0 = (time.time() // gcap.REFRESH_S) * gcap.REFRESH_S + 1
    ticks = [t0 + 3 * i for i in range(int(2 * gcap.REFRESH_S / 3))]
    for now in ticks:
        gcap.refresh_async(b, now)
        g = bud.evaluate(p.db, p.config(), "claude", [], now)
        assert g.numbers["global_today"] == 50.0, (now - t0, g.numbers)
        assert len(gcap._LOCAL) <= 1, len(gcap._LOCAL)
    assert len(asked) == 2 and len({s for _, s in asked}) == 2, asked   # one per REFRESH_S
    # One failed ssh try keeps the last answer while it is within STALE_S: named stale, shown with what
    # it adds, never counted.
    def down(t, s, e):
        raise RuntimeError("ssh timed out")
    monkeypatch.setattr(gcap, "fetch", down)
    later = ticks[-1] + gcap.REFRESH_S + 1
    gcap.refresh_async(b, later)
    m = gcap.load_cache()["machines"]["box2"]
    assert m["ok"] is False and later - m["ts"] <= gcap.STALE_S, m
    start, end, rolling = gcap.window(b, later)
    t = gcap.total(p.db, "claude", start, end, later, rolling=rolling)
    assert rolling and t["stale"] == ["box2"] and t["usd"] == 0 and t["stale_usd"] == 50.0, t
    n = bud.evaluate(p.db, p.config(), "claude", [], later).numbers
    assert n["global_today"] == 0 and n["global_stale_usd"] == 50.0, n
    # A rolling answer older than STALE_S covers another window: named stale, not counted.
    start, end, rolling = gcap.window(b, ticks[-1] + gcap.STALE_S + 60)
    t = gcap.total(p.db, "claude", start, end, ticks[-1] + gcap.STALE_S + 60, rolling=rolling)
    assert rolling and t["stale"] == ["box2"] and t["usd"] == 0, t



def test_spend_today_and_the_push_carry_this_machines_other_sessions(env, tmp_path, monkeypatch, capsys):
    p = make(env)
    from ttp import budget as bud
    from ttp import globalcap as gcap
    from ttp import localspend as ls
    from ttp import upstream
    from ttp.cli import main
    from ttp.project import register
    est = {"usd": 4.25, "sessions": 2, "estimated": False, "unknown": [], "billing": "usage"}
    monkeypatch.setattr(ls, "estimate", lambda b, s, e, now=None: est)
    monkeypatch.setattr(gcap, "account_of", lambda prov: "acct-a")
    now = time.time()
    main(["spend-today", "--since", str(now - 60), "--until", str(now + 60), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out[gcap.SESSIONS] == {"provider": "claude", "key": gcap.account_key("claude", "acct-a"), "usd": 4.25,
                                  "sessions": 2, "estimated": True, "billing": "usage"}, out
    main(["spend-today", "--since", str(now - 60), "--until", str(now + 60)])
    assert "other Claude Code sessions $4.25 (estimated)" in capsys.readouterr().out
    # The push carries it in every window, and the receiver counts and labels it.
    register("far", {"host": "server", "dir": "/srv/far"})
    p.set_config("budget.global_daily_usd", 100)
    monkeypatch.setattr(gcap, "fetch", lambda *a: (_ for _ in ()).throw(RuntimeError("ssh: no route")))
    sent = []
    monkeypatch.setattr(upstream, "ssh_pipe", lambda t, args, data, timeout: sent.append(data) or (b'{"accepted": 1}\n', ""))
    assert gcap.push(p.config()["budget"], now) == 1
    wins = json.loads(sent[0])["windows"]
    assert wins and all(w[gcap.SESSIONS]["usd"] == 4.25 for w in wins), wins
    monkeypatch.setenv("TTP_HOST", "server")
    assert gcap.receive(sent[0], "lap", now)[1] == 0
    monkeypatch.setattr(gcap, "targets", lambda: [])
    monkeypatch.setattr(gcap, "OTHER_SOURCES", [])
    g = bud.evaluate(p.db, p.config(), "claude", [], now + 1)
    assert g.numbers["global_today"] == 4.25, g.numbers
    assert "other Claude Code sessions on 1 other machine ($4.25, estimated)" in g.numbers["global_includes"], g.numbers
    # Logs not read yet: null, said plainly.
    est = None
    main(["spend-today", "--since", str(now - 60), "--until", str(now + 60), "--json"])
    assert json.loads(capsys.readouterr().out)[gcap.SESSIONS] is None



def test_this_machine_pushes_its_spend_in_the_background_and_a_failed_push_never_blocks(env, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp import globalcap as gcap
    from ttp import upstream
    from ttp.project import register, set_account_setting
    register("far", {"host": "server", "dir": "/srv/far"})
    p.set_config("budget.global_daily_usd", 100)
    p.set_config("budget.day_start", "08:00")
    p.db.spend("claude", 3.0, "task:1", account="acct-a")
    b = p.config()["budget"]
    monkeypatch.setattr(gcap, "fetch", lambda *a: (_ for _ in ()).throw(RuntimeError("ssh: no route")))
    calls = []

    def pipe(target, args, data, timeout):        # the server: its own name, its own receive
        calls.append((target, args))
        monkeypatch.setenv("TTP_HOST", "server")
        try:
            ack, rc = gcap.receive(data, args.split()[-1])
        finally:
            monkeypatch.setenv("TTP_HOST", "testhost")
        return ((json.dumps(ack) + "\n").encode(), "") if rc == 0 else (None, f"exit {rc}")
    monkeypatch.setattr(upstream, "ssh_pipe", pipe)
    now = time.time()
    assert gcap.push(b, now) == 1
    assert calls == [("server", "spend-today --receive --via testhost")], calls
    got = gcap.load_pushed()["testhost"]["windows"]
    assert gcap._key(*gcap.window(b, now)[:2]) in got and "rolling" in got, got   # its day and the rolling 24 h
    assert got["rolling"]["rows"][0]["usd"] == 3.0
    assert gcap.push(b, now + 60) == 0 and len(calls) == 1, "pushed again before REFRESH_S"
    # A push that fails backs off and is retried later; it never raises.
    monkeypatch.setattr(upstream, "ssh_pipe", lambda *a: (None, "exit 255: no route"))
    t1 = now + gcap.REFRESH_S
    assert gcap.push(b, t1) == 0
    st = gcap._json(gcap.push_state_path())["targets"]["server"]
    assert st["ok"] is False and st["fails"] == 1 and st["next"] == t1 + gcap.PUSH_BACKOFF_S[0], st
    assert not gcap._push_due(t1 + 60)
    # `none` turns pushing off; a list names the targets.
    set_account_setting("budget.push_spend_to", "none")
    assert gcap.push_targets() == []
    set_account_setting("budget.push_spend_to", ["other"])
    assert gcap.push_targets() == ["other"]
    assert gcap.setting_problems({"push_spend_to": "everywhere"})
    set_account_setting("budget.push_spend_to", None)
    # A hung push runs in the background: the tick goes on and the gate is worked out meanwhile.
    gate, on_tick = threading.Event(), []

    def hung(*a):
        on_tick.append(threading.current_thread() is threading.main_thread())
        return gate.wait(30) and (None, "late")
    monkeypatch.setattr(upstream, "ssh_pipe", hung)
    gcap.push_state_path().unlink()
    gcap.refresh_async(b, time.time())
    g = bud.evaluate(p.db, p.config(), "claude", [], time.time())
    assert "global_today" in g.numbers, g.numbers
    assert gcap._THREAD["t"].is_alive(), "the tick waited for the push"
    gate.set()
    gcap._THREAD["t"].join(10)
    assert on_tick == [False], "the push ran on the tick's thread"



@pytest.mark.parametrize("via", ["refresh", "refresh_async"])
@pytest.mark.parametrize("day_start", ["", "08:00"])
def test_a_machine_that_is_down_is_asked_once_per_refresh_and_again_once_it_is_back(env, monkeypatch, day_start, via):
    # A failed try holds the machine off for REFRESH_S by its time alone. Before, a machine that never
    # answered, or last answered for an earlier window or budget day, was asked over ssh on every 3 s
    # daemon tick, by every project daemon on the machine: 400 times in 1200 s.
    import math
    from datetime import datetime, timezone
    from ttp import budget as bud
    from ttp import globalcap as gcap
    p, b, clock, asked, up = _box2(env, monkeypatch, day_start)

    def tick(now):
        clock[0] = now
        if via == "refresh":
            gcap.refresh(*gcap.window(b, now)[:2], now)
        else:
            gcap.refresh_async(b, now)

    def run(seconds):                       # the daemon's 3 s ticks from the clock on
        first = clock[0] + 3
        for i in range(int(seconds // 3)):
            tick(first + 3 * i)

    def asked_once_per_refresh():
        assert 2 <= len(asked) <= math.ceil(1200 / gcap.REFRESH_S) + 1, (len(asked), asked[:4])
        assert all(y - x >= gcap.REFRESH_S for x, y in zip(asked, asked[1:])), asked

    def global_today(now):
        return bud.evaluate(p.db, p.config(), "claude", [], now).numbers

    # 08:00 in New York is 12:00 UTC in October: a new budget day and a new rolling window.
    new = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc).timestamp()
    for at in (new, new + gcap.DAY, new + 2 * gcap.DAY):
        assert gcap.window(b, at - 3)[0] != gcap.window(b, at)[0]
    # Never answered, down for 1200 s across a window change.
    clock[0] = new - 303
    run(1200)
    asked_once_per_refresh()
    # Back between two tries: asked again within REFRESH_S, and its answer counts.
    run(300)
    up[0], back = True, clock[0] + 3
    asked.clear()
    run(gcap.REFRESH_S)
    assert asked and asked[0] - back < gcap.REFRESH_S, (back, asked)
    n = global_today(clock[0])
    assert n["global_today"] == 50.0 and n["global_stale"] == [], n
    # An answer for an earlier window or budget day is asked again on the new one's first tick.
    nxt = new + gcap.DAY
    tick(nxt - 3)
    asked.clear()
    tick(nxt)
    assert asked == [nxt] and global_today(nxt)["global_today"] == 50.0, asked
    # Last answered for an earlier window or budget day, then down: asked once per refresh.
    tick(nxt + gcap.DAY - 3)
    up[0] = False
    asked.clear()
    clock[0] = nxt + gcap.DAY - 3
    run(1200)
    assert asked[0] == nxt + gcap.DAY, asked[:2]
    asked_once_per_refresh()
    assert global_today(clock[0])["global_stale"] == ["box2"]



def test_a_malformed_answer_is_a_failed_try_and_the_global_total_still_counts(env, monkeypatch):
    # A partial or malformed answer from another machine is not saved as its total, where it would
    # make every global total fail (and leave only the project caps in charge).
    p = make(env)
    from ttp import budget as bud
    from ttp import globalcap as gcap
    from ttp.project import register
    register("far", {"host": "box2", "dir": "/srv/far"})
    p.set_config("budget.global_daily_usd", 100)
    b = p.config()["budget"]
    monkeypatch.setattr(gcap, "account_of", lambda prov: "acct-a")
    key = gcap.account_key("claude", "acct-a")

    def answer(data):
        monkeypatch.setattr(gcap.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
            returncode=0, stdout=json.dumps(data), stderr=""))
        now = time.time()
        gcap.refresh(*gcap.window(b, now)[:2], now, force=True)
        return gcap.load_cache()["machines"]["box2"], bud.evaluate(p.db, p.config(), "claude", [], now).numbers

    for bad in ({"host": "box2", "rows": [{"usd": 5.0}], "projects": []},
                {"host": "box2", "rows": [{"provider": "claude", "key": key, "usd": "lots"}], "projects": []},
                {"host": ["box2"], "rows": [], "projects": []},
                {"host": "box2", "rows": [], "projects": 3}):
        m, n = answer(bad)
        assert m["ok"] is False and m["error"] == "unexpected answer", (bad, m)
        assert n.get("global_today") == 0 and n["global_stale"] == ["box2"], (bad, n)
    # A whole answer counts, with a run of no provider as `ttp spend-today` reports one.
    m, n = answer({"host": "box2", "rows": [{"provider": "claude", "key": key, "usd": 5.0},
                                           {"provider": None, "key": gcap.account_key("", ""), "usd": 0.0}],
                   "projects": ["far"]})
    assert m["ok"] is True and n["global_today"] == 5.0 and n["global_stale"] == [], (m, n)



def test_the_global_cap_stops_new_starts_but_not_running_work_or_replies(env, tmp_path, monkeypatch):
    p, other = _global_cap_setup(env, tmp_path)
    from ttp import budget as bud
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    starts = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: starts.append(a))
    other.spend("fake", 12.0, "task:1")
    p.db.x("INSERT INTO runs(role,provider,started,status) VALUES('worker','fake',?,'running')", (time.time(),))
    d.update_gates()
    g = d.gates["fake"]
    assert g.level == "red" and not g.allow_new_work and not g.allow_optional and g.max_parallel == 0, g.reasons
    assert any(r.startswith("global daily cap reached: $12.00 of $10 today") for r in g.reasons), g.reasons
    assert bud.evaluate(p.db, p.config(), "fake", []).level == "red"
    # No task starts, no scheduled llm run; the running one goes on.
    p.db.add_task("new work", "s", origin="user")
    d.dispatch()
    assert not [a for a in starts if a and a[0] != "coordinator"], starts
    sched.upsert(p.db, "summary", "llm", "1d", payload={"spec": "sum up"})
    p.db.x("UPDATE schedules SET next_run=? WHERE name='summary'", (time.time() - 1,))
    d.run_schedules()
    assert p.db.one("SELECT last_status FROM schedules WHERE name='summary'")["last_status"] == "skipped: budget red"
    assert p.db.one("SELECT status FROM runs WHERE role='worker'")["status"] == "running"
    # A message from the user still gets a coordinator turn.
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    p.db.x("UPDATE events SET status='done'")
    p.set_config("coordinator.debounce_s", 0)
    d.cfg = p.config()
    p.db.post("in", "status?", chat="c1", kind="user")
    starts.clear()
    d.maybe_coordinate()
    assert starts, "no coordinator turn for a user message under the global cap"



def test_without_budget_settings_a_usage_billed_account_gets_a_200_global_cap(env, tmp_path):
    # No setting anywhere: a $200 global cap over the rolling 24 h, the project caps as before.
    p = make(env)
    from ttp import budget as bud
    from ttp.web import budget_line
    b = p.config()["budget"]
    assert (b["global_daily_usd"], b["day_start"], b["timezone"]) == (200, "", "UTC"), b
    now = time.time()
    p.db.spend("fake", 50.0, "task:1", ts=now - 25 * 3600)        # outside the last 24 h
    p.db.spend("fake", 30.0, "task:1", ts=now - 23 * 3600)        # inside it, whatever the hour
    _other_project(tmp_path, "other").spend("fake", 150.0, "task:1", ts=now - 60)
    g = bud.evaluate(p.db, p.config(), "fake", [], now)
    assert g.level == "green" and g.numbers["spent_24h"] == 30.0, (g.level, g.reasons)
    assert (g.numbers["global_cap"], g.numbers["global_today"]) == (200, 180.0), g.numbers
    assert "spent_today" not in g.numbers, g.numbers
    assert budget_line(p.db, now, "fake", g.as_dict()) == "24h $30.00 this project, $180 of $200 global"



def test_the_default_global_cap_stops_new_work_at_200(env, tmp_path):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.spend("fake", 30.0, "task:1", ts=now - 60)
    _other_project(tmp_path, "other").spend("fake", 175.0, "task:1", ts=now - 60)
    g = bud.evaluate(p.db, p.config(), "fake", [], now)
    assert g.level == "red" and not g.allow_new_work, (g.level, g.reasons)
    assert any(r.startswith("global daily cap reached: $205 of $200 today") for r in g.reasons), g.reasons



def test_the_default_global_cap_leaves_a_plan_account_alone(env, tmp_path):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 60, "fake", "", "five_hour", 4, now + 3.9 * 3600))
    p.db.spend("fake", 30.0, "task:1", ts=now - 60)
    _other_project(tmp_path, "other").spend("fake", 5000.0, "task:1", ts=now - 60)
    g = bud.evaluate(p.db, p.config(), "fake", bud.plan_windows(p.db, now), now)
    assert g.regime == "windows" and g.level != "red", (g.level, g.reasons)
    assert "global_today" not in g.numbers and not any("global" in r for r in g.reasons), g.reasons



def test_an_explicit_zero_turns_the_default_global_cap_off(env, tmp_path):
    p = make(env)
    from ttp import budget as bud
    from ttp.web import budget_line
    now = time.time()
    p.db.spend("fake", 30.0, "task:1", ts=now - 60)
    _other_project(tmp_path, "other").spend("fake", 5000.0, "task:1", ts=now - 60)
    p.set_config("budget.global_daily_usd", 0)
    g = bud.evaluate(p.db, p.config(), "fake", [], now)
    assert g.level == "green" and "global_today" not in g.numbers, (g.level, g.reasons)
    assert budget_line(p.db, now, "fake", g.as_dict()) == "24h $30.00 actual"



def test_an_account_level_global_cap_overrides_the_default(env, tmp_path):
    from ttp import project
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    _other_project(tmp_path, "other").spend("fake", 500.0, "task:1", ts=now - 60)
    project.set_account_setting("budget.global_daily_usd", 1000)
    g = bud.evaluate(p.db, p.config(), "fake", [], now)
    assert g.level == "green" and g.numbers["global_cap"] == 1000, (g.level, g.reasons)
    project.set_account_setting("budget.global_daily_usd", 0)
    g = bud.evaluate(p.db, p.config(), "fake", [], now)
    assert g.level == "green" and "global_today" not in g.numbers, (g.level, g.reasons)



def _mid_budget_day(p):
    """A fixed instant six hours into today's budget day: a suite run crossing the reset cannot move it."""
    from ttp import globalcap as gcap
    return gcap.day_bounds(p.config()["budget"], time.time())[0] + 6 * 3600



def test_the_coordinator_digest_shows_the_global_daily_total(env, tmp_path):
    p, other = _global_cap_setup(env, tmp_path, cap=1000)
    from ttp import budget as bud
    from ttp import coordinator as coord
    now = _mid_budget_day(p)
    p.db.spend("fake", 1.2, "task:1", ts=now - 60)
    other.spend("fake", 340.0, "task:1", ts=now - 60)
    g = bud.evaluate(p.db, p.config(), "fake", [], now).as_dict()
    d = coord.digest(p, {"fake": g}, [], [])
    assert "$1.20 of $100.0 today" in d and "global daily cap (whole account): $341.20 of $1000 today (" in d, d
    # Off: the money line stays as before.
    p.set_config("budget.global_daily_usd", 0)
    p.set_config("budget.day_start", "")
    g = bud.evaluate(p.db, p.config(), "fake", [], now).as_dict()
    d = coord.digest(p, {"fake": g}, [], [])
    assert "$1.20 of $100.0 per 24h" in d and "global daily cap" not in d, d



def test_the_today_total_follows_the_clock_across_the_budget_day_reset(env, tmp_path):
    p, other = _global_cap_setup(env, tmp_path, cap=1000)
    from ttp import budget as bud
    from ttp import globalcap as gcap
    start, end = gcap.day_bounds(p.config()["budget"], _mid_budget_day(p))
    p.db.spend("fake", 1.2, "task:1", ts=end - 60)
    before = bud.evaluate(p.db, p.config(), "fake", [], end - 30)
    after = bud.evaluate(p.db, p.config(), "fake", [], end + 30)
    assert before.numbers["spent_today"] == 1.2 and after.numbers["spent_today"] == 0, (before.numbers, after.numbers)



def test_a_global_cap_of_zero_is_off(env, tmp_path):
    p, other = _global_cap_setup(env, tmp_path, cap=0)
    from ttp import budget as bud
    other.spend("fake", 500.0, "task:1")
    g = bud.evaluate(p.db, p.config(), "fake", [])
    assert g.level == "green" and "global_today" not in g.numbers, (g.level, g.reasons)



def test_an_unknown_global_total_leaves_the_project_caps_in_charge(env, tmp_path, monkeypatch):
    p, other = _global_cap_setup(env, tmp_path)
    from ttp import budget as bud
    from ttp import globalcap as gcap
    monkeypatch.setattr(gcap, "total", lambda *a, **k: (_ for _ in ()).throw(OSError("unreadable")))
    now = _mid_budget_day(p)
    g = bud.evaluate(p.db, p.config(), "fake", [], now)
    assert g.level == "green" and any("the project caps still apply" in r for r in g.reasons), g.reasons
    p.db.spend("fake", 150.0, "task:1", ts=now - 60)
    assert bud.evaluate(p.db, p.config(), "fake", [], now).level == "red"



def test_the_budget_line_of_a_usage_billed_account_and_of_a_plan(env, tmp_path):
    p, other = _global_cap_setup(env, tmp_path, cap=1000)
    from ttp import budget as bud
    from ttp import globalcap as gcap
    from ttp.web import budget_line
    now = _mid_budget_day(p)
    p.db.spend("fake", 1.2, "task:1", ts=now - 60)
    other.spend("fake", 340.0, "task:1", ts=now - 60)
    g = bud.evaluate(p.db, p.config(), "fake", [], now).as_dict()
    line = budget_line(p.db, now, "fake", g)
    assert re.fullmatch(r"today \$1\.20 this project, \$341 of \$1000 global - resets in \d+\.\d h", line), line
    end = gcap.day_bounds(p.config()["budget"], now)[1]
    assert line.endswith(f"resets in {(end - now) / 3600:.1f} h")
    g["numbers"]["global_stale"] = ["box2"]
    assert budget_line(p.db, now, "fake", g).endswith(" h (1 machine stale)")
    # Global cap off: this project only.
    del g["numbers"]["global_today"]
    assert re.fullmatch(r"today \$1\.20 this project - resets in \d+\.\d h", budget_line(p.db, now, "fake", g))
    # A plan account keeps its line.
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 60, "fake", "", "five_hour", 4, now + 3.9 * 3600))
    plan = bud.evaluate(p.db, p.config(), "fake", bud.plan_windows(p.db, now), now).as_dict()
    assert plan["regime"] == "windows"
    assert budget_line(p.db, now, "fake", plan) == "5h 4% - resets in 3.9 h, 24h $1.20 virtual"



def test_no_local_logs_leave_the_estimate_unknown_and_the_gate_alone(env, tmp_path, monkeypatch):
    from ttp import budget as bud
    from ttp import localspend as ls
    from ttp import project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    p = make(env)
    project.set_account_setting("budget.global_daily_usd", 10)
    now = time.time()
    b = ls.account_budget()
    assert ls.scan(b, now)["ok"] is False
    day = __import__("ttp.globalcap", fromlist=["day_bounds"]).day_bounds(b, now) or (now - 86400, now)
    assert ls.estimate(b, day[0], day[1], now) is None
    assert ls.source("claude", "", now - 60, now) == (0.0, ls.source("claude", "", now - 60, now)[1], "usage")
    assert "unknown" in ls.source("claude", "", now - 60, now)[1] and ls.source("codex", "", 0, now)[:2] == (0.0, "")
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert g.level == "green" and g.numbers["global_today"] == 0, (g.level, g.reasons, g.numbers)
    assert "other Claude Code sessions on this machine (unknown" in g.numbers["global_includes"]



def test_two_projects_count_the_machines_other_sessions_once(env, tmp_path, monkeypatch):
    from ttp import budget as bud
    from ttp import globalcap as gcap
    from ttp import localspend as ls
    from ttp import project
    logs = _claude_logs(tmp_path, monkeypatch)
    p = make(env)
    other = _other_project(tmp_path, "other")
    now = time.time()
    b = _far_day(now)
    for k in ("day_start", "timezone"):
        project.set_account_setting(f"budget.{k}", b[k])
    project.set_account_setting("budget.global_daily_usd", 100)
    p.db.spend("claude", 1.0, "task:1")
    other.spend("claude", 2.0, "task:1")
    _log(logs, "s1", [_call("s1", "m1", now - 60, output_tokens=1_000_000)])
    ls.scan(ls.account_budget(), now)
    start, end = gcap.day_bounds(b, now)
    t = gcap.total(p.db, "claude", start, end, now)
    assert t["usd"] == 18 and "other Claude Code sessions on this machine ($15" in t["includes"], t
    assert "at list prices" in t["includes"] and "nor web, desktop or cloud sessions" in t["includes"]
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert g.numbers["global_today"] == 18, g.numbers



@pytest.mark.parametrize("regimes,runs", [({"claude": "windows"}, False), ({"claude": "caps"}, True),
                                          ({"claude": "windows", "codex": "caps"}, True)])
def test_global_cap_background_work_runs_only_with_a_usage_billed_provider(env, monkeypatch, regimes, runs):
    """The global cap applies only to providers billed by usage: a project whose providers are all on
    plan windows asks no other machines and scans no local sessions, even with the cap on."""
    p = make(env)
    from ttp import budget as bud, globalcap as gcap, localspend as ls
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    d.cfg["budget"]["global_daily_usd"] = 200
    d.cfg["core_provider"] = "claude"
    if "codex" in regimes:
        p.db.add_task("other", "on codex", kind="work", tier="light", origin="user", provider="codex")
    real = bud.evaluate
    monkeypatch.setattr(bud, "evaluate", lambda db, cfg, prov, *a, **k: setattr(
        g := real(db, cfg, prov, *a, **k), "regime", regimes[prov]) or g)
    started = []
    monkeypatch.setattr(gcap, "refresh_async", lambda b, *a: started.append("refresh"))
    monkeypatch.setattr(ls, "scan_async", lambda b, *a: started.append("scan"))
    d.update_gates()
    assert set(d.gates) == set(regimes)
    assert started == (["refresh", "scan"] if runs else []), started



@pytest.mark.parametrize("stopped,run_status", [("timeout", "ok"), ("stalled", "stalled")])
def test_a_run_cut_off_after_a_misplaced_handoff_is_not_counted_as_waste(env, stopped, run_status):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("measure", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    p.db.x("INSERT INTO runs(task,role,provider,model,started,status,dir) VALUES(?,'worker','codex','',?,'ok',?)",
           (tid, time.time() - 7200, str(p.runs / "old")))
    (p.runs / "old").mkdir(parents=True)
    started = time.time() - 3600
    rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status) VALUES(?,'worker','codex','',?,'running')",
                 (tid, started))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"budget_usd": 8.0, "timeout_s": 3600}))
    (run_dir / "output.jsonl").write_text("")
    (p.runs / "old" / "result.json").write_text(json.dumps({"status": "done", "summary": "measured 42"}))
    d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                 {"rc": -15, "started": started, "ended": time.time(), "stopped": stopped})
    assert p.db.task(tid)["status"] == "done"
    run = p.db.one("SELECT status, note FROM runs WHERE id=?", (rid,))
    assert run["status"] == run_status, run["status"]
    if run_status in bud.WASTED:
        assert json.loads(run["note"])["not_waste"] == "handoff", run["note"]
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind='handoff_misplaced'")["n"] == 1
    assert not any("failed or stalled" in r for r in bud.evaluate(p.db, p.config(), "codex", []).reasons)



def test_network_outage_stays_quiet_when_a_task_could_start_or_is_held_for_another_reason(env, monkeypatch):
    from ttp import budget as bud
    p, d, tids, now, check = _net_setup(env, monkeypatch)
    other = p.db.add_task("local work", "s", kind="work", tier="light", origin="coordinator")
    for k in range(5):
        assert check(now + k * 900) == [], "a task that could start: the project is not blocked"
    prov = d.cfg.get("core_provider", "claude")
    d.gates = {prov: bud.Gate(provider=prov, regime="windows", max_parallel=4, allow_new_work=False)}
    for k in range(5, 10):
        assert check(now + k * 900) == [], "a task held by the budget gate: mixed causes, nothing said"
    p.db.update_task(other, status="cancelled")
    d.gates = {}
    run = p.db.x("INSERT INTO runs(task,role,started,status) VALUES(NULL,'worker',?,'running')", (now,))
    for k in range(10, 15):
        assert check(now + k * 900) == [], "a worker still runs: the project is not idle"
    p.db.x("UPDATE runs SET status='ok' WHERE id=?", (run,))
    assert check(now + 14 * 900 + 60, rc=1) == [] and check(now + 16 * 3600, rc=1) == [], \
        "a probe saying plain not yet (exit 1) is no network outage"



def test_failover_moves_a_preference_pin_off_a_gate_that_allows_no_new_start_below_red(env):
    # Plan pacing just under the line raises orange with 'no new starts' (max_parallel 0): the
    # dispatcher starts nothing there, so a preference pin moves at once; a required one stays held.
    from ttp import budget as bud, failover
    p, d, now = _failover_setup(env, util=80.0, hours=100)
    pref = _codex_task(p, "preference")
    req = _codex_task(p, "required", labels=["pin:required"])
    d.update_gates()
    reason = "seven_day window at 80%, 2.0 points under the 90% line; one more run may add ~5.0 points: no new starts"
    plan = [{"window": "seven_day", "allowed": 0, "resets_at": now + 100 * 3600}]
    d.gates["codex"] = bud.Gate("codex", level="orange", reasons=[reason], regime="windows", max_parallel=0,
                                numbers={"plan": plan})
    assert failover.stop(d.gates["codex"], None, False, now) is None, "the threshold path stays red-only"
    assert failover.hold(d.gates["codex"], None, False, now) == (reason, now + 100 * 3600)
    d.failover_pinned()
    assert p.db.task(pref)["provider"] == "fake", "a preference pin sat held behind 'no new starts'"
    assert p.db.task(req)["provider"] == "codex"
    (ev,) = p.db.q("SELECT * FROM events WHERE kind='provider_failover'")
    assert ev["task"] == pref and "moved at once from codex" in ev["text"]
    # Orange with room for a run moves nothing.
    other = _codex_task(p, "room left")
    d.gates["codex"] = bud.Gate("codex", level="orange", reasons=["x"], regime="windows", max_parallel=1)
    d._failover_checked = 0.0
    d.failover_pinned()
    assert p.db.task(other)["provider"] == "codex"
    assert len(p.db.q("SELECT id FROM events WHERE kind='provider_failover'")) == 1



def test_the_budget_line_names_every_provider_holding_work(env):
    """Held work waits on a provider that is not the core one: the header line names it, its
    fullest window and how many tasks it holds; a provider that holds nothing is left out."""
    p = make(env)
    from ttp import budget as bud
    from ttp.web import budget_line, health, holding
    now = time.time()
    bud.record_windows(p.db, [bud.Window("claude", "five_hour", 4, now + 3600),
                              bud.Window("claude", "seven_day", 21, now + 5 * 86400)])
    tid = p.db.add_task("needs codex", "spec", provider="codex")
    p.db.add_task("runs on core", "spec")
    gates = {"claude": {"level": "green", "regime": "windows", "numbers": {}},
             "codex": {"level": "red", "regime": "windows", "allow_new_work": False,
                       "numbers": {"plan": [{"window": "5h", "utilization": 40.0},
                                            {"window": "7d", "utilization": 93.4}]}}}
    before = budget_line(p.db, now, "claude")
    assert "codex" not in before, before
    line = budget_line(p.db, now, "claude", gates=gates)
    assert line.startswith("5h 4% - resets in 1.0 h, 7d 21%"), line
    assert line.endswith(", codex 7d 93% holding 1 task") and "\n" not in line, line
    p.db.set_kv("gates", gates)
    assert health(p, p.db)["spend"]["headline"].endswith("codex 7d 93% holding 1 task")
    # The core provider is named too once it holds work; a logout and a limit pause say so.
    gates["claude"] = {"level": "red", "regime": "caps", "numbers": {}}
    p.db.set_kv("limited:codex", {"until": now + 600, "note": "rate limit"})
    assert holding(p.db, now, "claude", gates) == ["claude red holding 1 task", "codex limit holding 1 task"]
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    assert holding(p.db, now, "claude", gates) == ["claude red holding 1 task"]



@pytest.mark.parametrize("outcome", ["noCredit", "nothingToReset"])
def test_reset_consume_final_answers_are_not_retried_and_stop_offering_that_reset(env, outcome):
    from ttp import budget as bud, credits
    from ttp.web import budget_line
    p = make(env)
    cfg = p.config()
    now = time.time()
    wins = _held(p, now=now)
    bud.record_windows(p.db, wins)
    ans = {"listed": True, "resets": [{"id": "c1", "expires_at": now + 86400 * 9}], "free_resets": 1, "paid": {}}
    credits.record(p.db, "codex", ans, "acct", now)
    assert "1 free reset" in budget_line(p.db, now)
    prov = _ResetProvider([{"outcome": outcome}])
    done = credits.consume(p.db, cfg, "codex", prov, wins, ans, now)
    assert done["outcome"] == outcome and done["reason"] == credits.FINAL[outcome] and len(prov.calls) == 1
    assert done, "a truthy answer: the daemon reads the meter again at once"
    text = credits.report_text("codex", done)
    assert text.startswith("Did not use a free codex window reset") and outcome in text and done["reason"] in text
    # Final: never retried, also not after RETRY_S, nor with a re-read that still lists the same credit.
    credits.record(p.db, "codex", ans, "acct", now + 5)
    for t in (now + 60, now + credits.RETRY_S + 1, now + 3 * credits.RETRY_S):
        assert credits.consume(p.db, cfg, "codex", prov, wins, ans, t) is None
    assert len(prov.calls) == 1
    # Not offered as an unblock path from a reading that has not changed.
    r = credits.latest(p.db, "codex", now + 10)
    assert r["refused"] == outcome and credits.summary(r) == ""
    assert "free reset" not in budget_line(p.db, now + 10)
    path = credits.unblock_path(p.db, cfg, "codex", now + 10)
    assert "free window reset(s)" not in path and outcome in path, path
    # A new reading that changes things offers it again.
    changed = {**ans, "resets": [{"id": "c2", "expires_at": now + 86400 * 20}]}
    credits.record(p.db, "codex", changed, "acct", now + 20)
    assert "free window reset(s)" in credits.unblock_path(p.db, cfg, "codex", now + 20)
    assert credits.decide(p.db, cfg, "codex", wins, changed, now + 20) is None, "that window had its answer"
    if outcome == "nothingToReset":   # recorded against the window: the next one may try again
        nxt = _held(p, resets_in_h=40 + 7 * 24, now=now)
        assert credits.decide(p.db, cfg, "codex", nxt, ans, now + 20)["credit"] == "c1"
    else:   # no credit: the same stale list is not offered for any window
        nxt = _held(p, resets_in_h=40 + 7 * 24, now=now)
        assert credits.decide(p.db, cfg, "codex", nxt, ans, now + 20) is None



def test_paid_credits_open_a_full_window_but_the_dollar_and_spend_limits_still_hold(env):
    from ttp import budget as bud, credits
    p = make(env)
    cfg = p.config()
    now = time.time()
    wins = _held(p, util=95, now=now)
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 3600, "codex", "acct", "7d", 95.0, wins[0].resets_at))
    g = bud.evaluate(p.db, cfg, "codex", wins, now)
    assert g.level == "red" and not g.allow_new_work, "without credits the line holds"
    credits.record(p.db, "codex", credits.parse({"credits": {"hasCredits": True, "balance": "20"}}), "acct", now)
    g = bud.evaluate(p.db, cfg, "codex", wins, now)
    assert g.level != "red" and g.allow_new_work, (g.level, g.reasons)
    assert any("paid credits" in r for r in g.reasons), g.reasons
    assert g.numbers["paid_credits"]["usd"] == 0.0
    # Spend past the line is real money: it counts toward the project's daily cap (100).
    p.db.spend("codex", 60.0, "task:1", ts=now - 1800)
    p.db.spend("codex", 45.0, "task:2", ts=now - 600)
    p.db.spend("codex", 500.0, "task:0", ts=now - 7200)   # before the window reached the line: plan spend
    g = bud.evaluate(p.db, cfg, "codex", wins, now)
    assert g.numbers["paid_credits"]["usd"] == 105.0 and g.level == "red" and not g.allow_new_work, g.numbers
    assert any(r.startswith("cap reached") for r in g.reasons), g.reasons
    # The account's spend limit closes it whatever the credits say.
    credits.record(p.db, "codex", credits.parse({"credits": {"unlimited": True}, "spendControlReached": True}), "acct", now)
    g = bud.evaluate(p.db, cfg, "codex", wins, now)
    assert g.level == "red" and any("spend limit reached" in r for r in g.reasons), g.reasons
    # A stale reading says nothing about now.
    credits.record(p.db, "codex", credits.parse({"credits": {"hasCredits": True}}), "acct", now - credits.FRESH_S - 5)
    assert bud.evaluate(p.db, cfg, "codex", wins, now).level == "red"



def test_a_red_provider_is_remetered_every_10_min_and_starts_on_the_first_reading_under_the_line(env, monkeypatch):
    import threading
    from ttp import budget as bud, daemon as dm
    p = make(env)
    p.db.add_task("codex work", "x", kind="work", tier="light", origin="coordinator", provider="codex")
    d = dm.Daemon(p.base)
    now = time.time()
    prov = _MeterProvider(95.0)
    prov.at = now + 3 * 3600   # resets in 3 h: no free reset would be used anyway
    monkeypatch.setattr(dm, "get_provider", lambda name: prov)

    class Sync:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

        def is_alive(self):
            return False
    monkeypatch.setattr(threading, "Thread", Sync)
    clock = {"t": now}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    d._refresh_meters()
    d.update_gates()
    assert d.gates["codex"].level == "red" and prov.reads == 1
    clock["t"] = now + 300
    d._refresh_meters()
    assert prov.reads == 1, "not before 10 min"
    p.db.x("UPDATE tasks SET status='waiting' WHERE provider='codex'")   # held: only the red gate wants it
    prov.util = 40.0
    clock["t"] = now + dm.METER_EVERY_S + 1
    d._refresh_meters()
    assert prov.reads == 2, "a red provider is read every 10 min even with nothing queued"
    p.db.x("UPDATE tasks SET status='queued' WHERE provider='codex'")
    d.update_gates()
    assert d.gates["codex"].level != "red" and d.gates["codex"].allow_new_work
    assert [w.utilization for w in bud.plan_windows(p.db, clock["t"]) if w.provider == "codex"] == [40.0]



def test_plan_red_reason_and_held_task_show_home_reset_clock(env):
    from ttp import budget as bud, failover, timefmt
    p = make(env)
    p.set_config("home_timezone", "America/Los_Angeles")
    now = time.time()
    reset = now + 3600
    g = bud.evaluate(p.db, p.config(), "claude", [bud.Window("claude", "five_hour", 95, reset)], now)
    expected = f"resumes at {timefmt.long(reset, p)}"
    assert g.level == "red" and expected in "; ".join(g.reasons)
    assert expected in failover.stop(g.as_dict(), None, False, now)[0]



def _fake_plan(p, account, now, util=95.0):
    """The fake provider on a plan account: a finished run and readings of two windows, 7d at `util`."""
    p.db.x("INSERT INTO runs(role,provider,account,started,ended,status,cost_usd) VALUES('worker','fake',?,?,?,'ok',1)",
           (account, now - 900, now - 600))
    for window, u, resets in (("five_hour", 10.0, now + 3600), ("seven_day", util, now + 3 * 86400)):
        p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
               (now - 300, "fake", account, window, u, resets))
    return now + 3 * 86400



def _bk_deferral(task):
    from ttp import db as dbm
    return dbm.deferral(task)



def test_a_plan_to_billed_switch_releases_old_holds_and_wakes_the_coordinator_once(env, monkeypatch):
    """The dogfooding case: the login switched from a plan account to a usage-billed one while the
    coordinator held work until the 7d window resets. The daemon notices, releases the limit pause,
    the schedule held for budget and the 'until reset' deferrals at once, and queues one tricky-turn
    event listing what it released and the waits that are the coordinator's to re-point."""
    from ttp import budget as bud, budgetkind, coordinator as coord
    from ttp.daemon import Daemon
    monkeypatch.setattr(budgetkind, "DEBOUNCE_S", 0.0)
    p = make(env)
    now = time.time()
    resets = _fake_plan(p, ACCT_PLAN, now)
    bud.note_account(p.db, "fake", ACCT_PLAN)
    p.db.set_kv("limited:fake", {"until": now + 86400, "note": "usage limit"})
    p.db.x("UPDATE schedules SET enabled=1, next_run=?, last_status=? WHERE name='daily-review'",
           (now + 1800, "skipped: budget red"))
    by_why = p.db.add_task("reindex the docs", "x", labels=coord.defer_labels(resets, "true", "until the 7d window resets"),
                           not_before=resets)
    by_time = p.db.add_task("weekly sweep", "x", labels=coord.defer_labels(resets + 60, None), not_before=resets + 60)
    other = p.db.add_task("after the release", "x", labels=coord.defer_labels(now + 2 * 86400, "true", "after the tag is cut"),
                          not_before=now + 2 * 86400)
    waits = p.db.add_task("long job", "x", status="waiting")
    p.db.update_task(waits, result=json.dumps({"waiting_for": "the 5h window to reset"}))
    d = Daemon(p.base)
    _bk_tick(d, monkeypatch)
    assert d.gates["fake"].regime == "windows" and d.gates["fake"].level == "red"
    assert _bk_events(p) == [], "the first fingerprint is the baseline"

    bud.note_account(p.db, "fake", ACCT_BILLED)   # the login switched
    d.update_gates()
    assert _bk_events(p) == [], "a new fingerprint waits out its debounce"
    d.update_gates()
    ev = _bk_events(p)
    assert len(ev) == 1 and ev[0]["status"] == "queued", ev
    text = ev[0]["text"]
    assert "account switched" in text and "budget kind plan -> billed" in text, text
    assert "was plan windows (five_hour, seven_day)" in text and "dollar caps daily $100" in text, text
    assert "schedule daily-review" in text and "limit pause" in text, text
    assert f"#{by_why} reindex the docs: deferral" in text and f"#{by_time} weekly sweep: deferral" in text, text
    assert f"#{waits} long job: waiting" in text and f"#{other}" not in text, text
    # released model-free, re-evaluated against the new gate on this same update
    assert float(p.db.kv("limited:fake")["until"]) == 0
    assert d.gates["fake"].regime == "caps" and d.gates["fake"].allow_new_work, d.gates["fake"].reasons
    assert p.db.one("SELECT next_run FROM schedules WHERE name='daily-review'")["next_run"] <= time.time()
    assert _bk_deferral(p.db.task(by_why)) == {} and p.db.task(by_why)["not_before"] is None
    assert _bk_deferral(p.db.task(by_time)) == {}
    assert _bk_deferral(p.db.task(other)).get("why") == "after the tag is cut", "a deferral for something else stays"
    # one tricky turn, one feed line that never needs the user
    assert budgetkind.KIND in coord.effort_triggers(p.db, p.config(), [ev[0]["id"]], None)[0]
    feed = [m for m in p.db.q("SELECT * FROM messages WHERE direction='out'") if "budget changed" in m["text"]]
    assert len(feed) == 1 and feed[0]["kind"] == "info" and feed[0]["severity"] == "low", feed
    assert any("budget changed" in s.get("why", "") for s in p.db.kv("blocker_sweep_signals", []))
    for _ in range(3):
        d.update_gates()
    assert len(_bk_events(p)) == 1, "the same budget wakes nobody again"



def test_the_budget_fingerprint_survives_a_restart(env, monkeypatch):
    """A restart with the same budget is no change; a change made while the daemon was down is
    caught on its first updates, and raises one event across further restarts."""
    from ttp import budget as bud, budgetkind
    from ttp.daemon import Daemon
    monkeypatch.setattr(budgetkind, "DEBOUNCE_S", 0.0)
    p = make(env)
    now = time.time()
    _fake_plan(p, ACCT_PLAN, now, util=40.0)
    bud.note_account(p.db, "fake", ACCT_PLAN)
    _bk_tick(Daemon(p.base), monkeypatch)
    _bk_tick(Daemon(p.base), monkeypatch, 3)   # restarted, same budget
    assert _bk_events(p) == []
    bud.note_account(p.db, "fake", ACCT_BILLED)   # switched while the daemon was down
    d = Daemon(p.base)
    d.update_gates()
    _bk_tick(Daemon(p.base), monkeypatch, 3)   # and restarted again mid-debounce and after
    assert len(_bk_events(p)) == 1



def test_a_billed_to_plan_switch_wakes_the_coordinator(env, monkeypatch):
    from ttp import budget as bud, budgetkind
    from ttp.daemon import Daemon
    monkeypatch.setattr(budgetkind, "DEBOUNCE_S", 0.0)
    p = make(env)
    now = time.time()
    p.db.x("INSERT INTO runs(role,provider,account,started,ended,status,cost_usd) VALUES('worker','fake',?,?,?,'ok',2)",
           (ACCT_BILLED, now - 900, now - 600))
    bud.note_account(p.db, "fake", ACCT_BILLED)
    d = Daemon(p.base)
    _bk_tick(d, monkeypatch)
    assert d.gates["fake"].regime == "caps" and _bk_events(p) == []
    bud.note_account(p.db, "fake", ACCT_PLAN2)
    _fake_plan(p, ACCT_PLAN2, time.time(), util=20.0)
    _bk_tick(d, monkeypatch)
    ev = _bk_events(p)
    assert len(ev) == 1 and "budget kind billed -> plan" in ev[0]["text"], ev
    assert "now plan windows (five_hour, seven_day)" in ev[0]["text"]



def test_old_plan_readings_never_make_a_billed_account_plan(env, monkeypatch):
    """A usage-billed account holding only another account's old plan readings is billed on every
    gate update, never 'plan', so no billed -> plan event fires. A real login switch to a plan account
    whose run reports windows raises exactly one billed -> plan event."""
    from ttp import budget as bud, budgetkind
    from ttp.daemon import Daemon
    monkeypatch.setattr(budgetkind, "DEBOUNCE_S", 0.0)
    p = make(env)
    now = time.time()
    _fake_plan(p, ACCT_PLAN, now - 3600, util=40.0)   # account A's readings, an hour old
    bud.note_account(p.db, "fake", ACCT_BILLED)
    p.db.x("INSERT INTO runs(role,provider,account,started,ended,status,cost_usd) VALUES('worker','fake',?,?,?,'ok',2)",
           (ACCT_BILLED, now - 900, now - 600))
    d = Daemon(p.base)
    for _ in range(4):
        d.update_gates()
        st = p.db.kv(budgetkind.KV + "fake")
        assert d.gates["fake"].regime != "windows", d.gates["fake"].reasons
        assert st["parts"]["kind"] == "billed" and (st.get("pending") or {}).get("parts", {}).get("kind") != "plan", st
    assert _bk_events(p) == []
    bud.note_account(p.db, "fake", ACCT_PLAN2)
    _fake_plan(p, ACCT_PLAN2, time.time(), util=20.0)
    _bk_tick(d, monkeypatch, 4)
    ev = _bk_events(p)
    assert len(ev) == 1 and "budget kind billed -> plan" in ev[0]["text"], ev



def _bk_billed_only(p, d, now):
    """The budget line and detail of the provider's billed gate, with the global part left out so the
    line falls back to the generic form (the one that could show plan windows)."""
    from ttp.web import budget_line, gate_detail
    g = d.gates["fake"].as_dict()
    g["numbers"].pop("global_today", None)
    return budget_line(p.db, now, "fake", g), gate_detail(g, now)


def test_a_lapsed_plan_switch_to_billed_leaves_no_plan_text_holds_or_spend(env, monkeypatch):
    """The same account goes from a plan to billing by use (its runs stop reporting windows). The gate,
    the budget line and its detail use only the dollar caps: no old window, average or 'virtual'
    text, and spend the plan paid for stays out of the caps. The holds the plan made are released,
    and the coordinator hears of it once."""
    from ttp import budget as bud, budgetkind, coordinator as coord
    from ttp.daemon import Daemon
    monkeypatch.setattr(budgetkind, "DEBOUNCE_S", 0.0)
    p = make(env)
    now = time.time()
    resets = _fake_plan(p, ACCT_PLAN, now - 3600)   # at the line: red, holding work
    bud.note_account(p.db, "fake", ACCT_PLAN)
    p.db.spend("fake", 5.0, "task:1", account=ACCT_PLAN, ts=now - 4000)   # plan-paid spend
    p.db.set_kv("limited:fake", {"until": now + 86400, "note": "usage limit"})
    held = p.db.add_task("reindex the docs", "x", labels=coord.defer_labels(resets, "true", "until the 7d window resets"),
                         not_before=resets)
    d = Daemon(p.base)
    _bk_tick(d, monkeypatch)
    assert d.gates["fake"].regime == "windows" and _bk_events(p) == []
    for i in range(bud.PLAN_LAPSE_RUNS):   # paid runs of the same account with no windows: billed now
        p.db.x("INSERT INTO runs(role,provider,account,started,ended,status,cost_usd) VALUES('worker','fake',?,?,?,'ok',2)",
               (ACCT_PLAN, now - 900 + i, now - 600 + i))
        p.db.spend("fake", 2.0, f"task:{i + 2}", account=ACCT_PLAN, ts=now - 600 + i)
    _bk_tick(d, monkeypatch)
    g = d.gates["fake"]
    assert g.regime == "caps" and "plan" not in g.numbers and g.numbers["daily_cap"] == 100, g.numbers
    assert g.numbers["spent_24h"] == 4.0, "the plan-paid $5 stays out of the dollar caps"
    assert g.allow_new_work and g.max_parallel == int(p.config()["budget"].get("max_parallel_workers", 6)), g.reasons
    line, detail = _bk_billed_only(p, d, time.time())
    assert line.startswith("24h $4.00 actual"), line
    for stale in ("5h", "7d", "avg", "virtual", "resets in"):
        assert stale not in line, line
    assert "account use" not in detail and "$4.00 of $100" in detail, detail
    ev = _bk_events(p)
    assert len(ev) == 1 and "budget kind plan -> billed" in ev[0]["text"], ev
    assert float(p.db.kv("limited:fake")["until"]) == 0 and "limit pause" in ev[0]["text"]
    assert _bk_deferral(p.db.task(held)) == {} and p.db.task(held)["not_before"] is None
    for _ in range(3):
        d.update_gates()
    assert len(_bk_events(p)) == 1, "the same budget wakes nobody again"


def test_billed_to_plan_and_back_swaps_caps_and_windows_cleanly(env, monkeypatch):
    """Billed -> plan: the dollar caps, their text and the holds they made go; the plan runs every
    slot below its line. Plan -> billed again: the caps return without the plan's spend, and the
    windows leave the budget line. One event per switch, none after."""
    from ttp import budget as bud, budgetkind, coordinator as coord
    from ttp.daemon import Daemon
    monkeypatch.setattr(budgetkind, "DEBOUNCE_S", 0.0)
    monkeypatch.setattr(budgetkind, "MIN_GAP_S", 0.0)
    p = make(env)
    from ttp.web import budget_line, gate_detail
    now = time.time()
    most = int(p.config()["budget"].get("max_parallel_workers", 6))
    p.db.x("INSERT INTO runs(role,provider,account,started,ended,status,cost_usd) VALUES('worker','fake',?,?,?,'ok',3)",
           (ACCT_BILLED, now - 7200, now - 6900))
    p.db.spend("fake", 3.0, "task:1", account=ACCT_BILLED, ts=now - 6900)
    bud.note_account(p.db, "fake", ACCT_BILLED)
    p.db.x("UPDATE schedules SET enabled=1, next_run=?, last_status=? WHERE name='daily-review'",
           (now + 1800, "skipped: budget red"))
    capped = p.db.add_task("big sweep", "x", labels=coord.defer_labels(now + 6 * 3600, "true", "until the daily cap resets"),
                           not_before=now + 6 * 3600)
    d = Daemon(p.base)
    _bk_tick(d, monkeypatch)
    assert d.gates["fake"].regime == "caps" and d.gates["fake"].numbers["spent_24h"] == 3.0

    bud.note_account(p.db, "fake", ACCT_PLAN2)   # to a plan
    _fake_plan(p, ACCT_PLAN2, time.time(), util=20.0)
    p.db.spend("fake", 7.0, "task:2", account=ACCT_PLAN2, ts=time.time())   # plan-paid
    _bk_tick(d, monkeypatch)
    g = d.gates["fake"]
    assert g.regime == "windows" and g.numbers["billing"] == "plan", g.reasons
    for k in ("daily_cap", "weekly_cap", "spent_today", "spent_24h", "cap_ratio", "global_today"):
        assert k not in g.numbers, (k, g.numbers)
    assert g.level == "green" and g.allow_new_work and g.max_parallel == most, (g.level, g.max_parallel, g.reasons)
    line = budget_line(p.db, time.time(), "fake", g.as_dict())
    assert line.startswith("5h ") and " of $" not in line and "today" not in line and "this project" not in line, line
    assert "account use:" in gate_detail(g.as_dict(), time.time())
    ev = _bk_events(p)
    assert len(ev) == 1 and "budget kind billed -> plan" in ev[0]["text"], ev
    assert "schedule daily-review" in ev[0]["text"], ev[0]["text"]
    assert _bk_deferral(p.db.task(capped)) == {} and p.db.task(capped)["not_before"] is None
    assert p.db.one("SELECT next_run FROM schedules WHERE name='daily-review'")["next_run"] <= time.time()
    d.update_gates()
    assert len(_bk_events(p)) == 1

    bud.note_account(p.db, "fake", ACCT_BILLED)   # and back to billing by use
    _bk_tick(d, monkeypatch)
    g = d.gates["fake"]
    assert g.regime == "caps" and "plan" not in g.numbers, g.reasons
    assert g.numbers["daily_cap"] == 100 and g.numbers["weekly_cap"] == 200, g.numbers
    assert g.numbers["spent_24h"] == 3.0 and g.numbers["spent_7d"] == 3.0, "the plan's $7 is not billed spend"
    line, detail = _bk_billed_only(p, d, time.time())
    for stale in ("5h", "7d", "avg", "virtual"):
        assert stale not in line, line
    assert "$3.00 of $100" in detail and "account use" not in detail, detail
    ev = _bk_events(p)
    assert len(ev) == 2 and "budget kind plan -> billed" in ev[1]["text"], [e["text"] for e in ev]
    assert "dollar caps daily $100" in ev[1]["text"], ev[1]["text"]
    for _ in range(3):
        d.update_gates()
    assert len(_bk_events(p)) == 2, "each switch is told once"



def test_an_account_flap_on_the_same_billing_kind_wakes_nobody(env, monkeypatch):
    """The flapping case: the account label alternates between two usage-billed accounts with the
    same caps ('was billed by use ... now billed by use'). Each flap raised an event that woke the
    coordinator for a no-op turn. Same kind with nothing to release or re-point: no event, no feed
    line. A same-kind switch that released a hold still queues one, batched so it never wakes alone."""
    from ttp import budget as bud, budgetkind, coordinator as coord
    from ttp.daemon import Daemon
    monkeypatch.setattr(budgetkind, "DEBOUNCE_S", 0.0)
    monkeypatch.setattr(budgetkind, "MIN_GAP_S", 0.0)
    p = make(env)
    now = time.time()
    other = "b@example.org | Org | usage-billed"
    for acct in (ACCT_BILLED, other):
        p.db.x("INSERT INTO runs(role,provider,account,started,ended,status,cost_usd) VALUES('worker','fake',?,?,?,'ok',1)",
               (acct, now - 900, now - 600))
    bud.note_account(p.db, "fake", ACCT_BILLED)
    d = Daemon(p.base)
    _bk_tick(d, monkeypatch)
    for acct in (other, ACCT_BILLED, other, ACCT_BILLED):
        bud.note_account(p.db, "fake", acct)
        _bk_tick(d, monkeypatch)
        assert p.db.kv(budgetkind.KV + "fake")["parts"]["account"] == budgetkind.account_hash(acct)
    assert _bk_events(p) == [], [dict(e) for e in _bk_events(p)]
    assert not [m for m in p.db.q("SELECT * FROM messages WHERE direction='out'") if "budget changed" in m["text"]]
    # a real switch that lifts a usage-limit pause still tells the coordinator, batched
    p.db.set_kv("limited:fake", {"until": time.time() + 86400, "note": "usage limit"})
    bud.note_account(p.db, "fake", other)
    _bk_tick(d, monkeypatch)
    ev = _bk_events(p)
    assert len(ev) == 1 and "account switched" in ev[0]["text"] and "limit pause" in ev[0]["text"], ev
    assert float(p.db.kv("limited:fake")["until"]) == 0
    assert ev[0]["severity"] == "normal" and coord.batchable(ev[0]["kind"], ev[0]["severity"])



def test_budget_change_never_shows_the_account_in_clear(env, monkeypatch):
    """The account label (an email, an organization) is only ever kept as a hash: not in the state,
    the event, the feed or the daemon log."""
    from ttp import budget as bud, budgetkind
    from ttp.daemon import Daemon
    monkeypatch.setattr(budgetkind, "DEBOUNCE_S", 0.0)
    p = make(env)
    _fake_plan(p, ACCT_PLAN, time.time())
    bud.note_account(p.db, "fake", ACCT_PLAN)
    d = Daemon(p.base)
    _bk_tick(d, monkeypatch)
    bud.note_account(p.db, "fake", ACCT_BILLED)
    _bk_tick(d, monkeypatch)
    assert len(_bk_events(p)) == 1
    log_text = (p.logs / "daemon.log").read_text() if (p.logs / "daemon.log").exists() else ""
    assert "budget changed" in log_text
    stored = json.dumps([p.db.kv(budgetkind.KV + "fake"), [dict(e) for e in _bk_events(p)],
                         p.db.q("SELECT text FROM messages")]) + log_text
    for secret in ("a@example.org", "example.org", "Org |", "usage-billed |"):
        assert secret not in stored, secret
    assert p.db.kv(budgetkind.KV + "fake")["parts"]["account"] == budgetkind.account_hash(ACCT_BILLED)



def test_coordinator_cache_distinguishes_zero_partial_full_and_unknown(env):
    from ttp import budget as bud, audit
    from ttp.cli import status_text
    p = make(env)
    now = time.time()
    for fresh, read, write, metadata in (
        (100, 0, 0, {'complete': True, 'requests': 1}),
        (50, 50, 0, {'complete': True, 'requests': 1}),
        (0, 100, 0, {'complete': True, 'requests': 1}),
        (100, 0, 0, {'complete': False, 'requests': None}),
        (0, 0, 0, {'complete': True, 'requests': 1}),
        (100, 0, 0, None),
    ):
        note = {'cache_telemetry': metadata} if metadata is not None else {}
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd,input_tokens,cache_read_tokens,"
               "cache_write_tokens,note) VALUES('coordinator','claude',?,?,'ok',0.1,?,?,?,?)",
               (now - 60, now, fresh, read, write, json.dumps(note)))
    s = bud.coordinator_cache(p.db, now)['24h']
    assert s['turns'] == 6 and s['known_turns'] == 3
    # The turn with no cache_telemetry at all is from before the field existed: legacy, not missing.
    assert s['states'] == {'zero': 1, 'partial': 1, 'full': 1, 'missing': 2, 'legacy': 1}
    assert s['miss_turns'] == 1 and s['hit_pct'] == 50
    assert s['raw_tokens'] == {'input_tokens': 350, 'cache_read_tokens': 150, 'cache_write_tokens': 0}
    assert s['requests_unknown_turns'] == 2
    assert 'zero 1' not in status_text(p), "the coordinator cache is a --verbose line"
    for text in (status_text(p, verbose=True), '\n'.join(audit.lines(p.db, now, p=p))):
        assert 'zero 1' in text and 'partial 1' in text and 'full 1' in text
        assert 'missing 2 (unknown), legacy 1 (before telemetry)' in text
        assert 'request count unknown for 2 turns' in text, "some turns carry a request count"



def test_coordinator_cache_line_drops_request_clause_when_no_turn_counts_requests():
    from ttp import budget as bud

    def row(requests):
        return {'note': json.dumps({'cache_telemetry': {'complete': True, 'requests': requests}}),
                'input_tokens': 10, 'cache_read_tokens': 90, 'cache_write_tokens': 0, 'cost_usd': 0.1}
    none = bud.coordinator_cache_line(bud._cache_summary([row(None), row(None)]))
    assert 'request count' not in none and 'legacy' not in none
    some = bud.coordinator_cache_line(bud._cache_summary([row(None), row(3)]))
    assert some.endswith('; request count unknown for 1 turns')
    assert 'request count' not in bud.coordinator_cache_line(bud._cache_summary([row(2)]))



def test_web_app_shows_the_servers_coordinator_cache_line_as_is(env):
    """The CLI, the daily review and the web app word the cache status once, on the server: the web
    payload carries coordinator_cache_line() per window and app.js shows it without rebuilding it."""
    from ttp import budget as bud
    from ttp.web import state_payload
    p = make(env)
    now = time.time()
    recorded = {'cache_telemetry': {'complete': True, 'requests': None}}   # Claude counts no requests
    for fresh, read, note in ((10, 90, recorded), (100, 0, recorded), (100, 0, {})):
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd,input_tokens,cache_read_tokens,"
               "cache_write_tokens,note) VALUES('coordinator','claude',?,?,'ok',0.1,?,?,0,?)",
               (now - 600, now - 500, fresh, read, json.dumps(note)))
    cache = state_payload(p, p.db)['budget']['coordinator_cache']
    for window in ('24h', '3d', '7d'):
        assert cache[window]['line'] == bud.coordinator_cache_line(bud.coordinator_cache(p.db, now)[window])
    line = cache['24h']['line']
    assert 'request count unknown' not in line, line
    # The turn from before the field existed is counted as legacy, not dropped from the counts.
    assert 'missing 0 (unknown), legacy 1 (before telemetry)' in line, line
    js = '\n'.join(f.read_text() for f in sorted((RUNTIME / 'ttp' / 'web').glob('*.js')))
    assert 'cache.line' in js
    for wording in ('token hit', 'request count unknown', '<50% read', 'before telemetry'):
        assert wording not in js, f"the web app rebuilds the server's cache wording: {wording!r}"



def test_cache_telemetry_backfill_rebuilds_old_turns_from_output_and_marks_the_rest_legacy(env):
    from ttp import budget as bud
    from ttp.providers.claude import Claude
    p = make(env)
    now = time.time()
    usage = {'input_tokens': 40, 'cache_read_input_tokens': 900, 'cache_creation_input_tokens': 60}
    ids = []
    for fresh, read, write, output in ((40, 900, 60, True),     # tokens match its output: filled
                                       (40, 800, 60, True),     # stored tokens differ: legacy
                                       (40, 900, 60, False)):   # output gone: legacy
        rid = p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd,input_tokens,"
                     "cache_read_tokens,cache_write_tokens,note) VALUES('coordinator','claude',?,?,'ok',0.1,"
                     "?,?,?,'{}')", (now - 600, now - 500, fresh, read, write))
        ids.append(rid)
        if output:
            d = p.runs / str(rid)
            d.mkdir(parents=True, exist_ok=True)
            (d / 'output.jsonl').write_text(json.dumps({'type': 'result', 'usage': usage, 'num_turns': 1}) + '\n')
    before = bud.coordinator_cache(p.db, now)['24h']
    assert before['states']['legacy'] == 3 and before['states']['missing'] == 0
    assert bud.backfill_cache_telemetry(p.db, p.runs, now) == {'filled': 1, 'legacy': 2}
    notes = [json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (i,))['note'])['cache_telemetry'] for i in ids]
    parsed = Claude().parse(p.runs / str(ids[0]) / 'output.jsonl')
    assert (parsed.input_tokens, parsed.cache_read_tokens, parsed.cache_write_tokens) == (40, 900, 60)
    assert notes[0]['complete'] is True and notes[0]['backfilled'] is True
    assert notes[1]['source'] == notes[2]['source'] == 'legacy'
    after = bud.coordinator_cache(p.db, now)['24h']
    assert after['states'] == {'zero': 0, 'partial': 1, 'full': 0, 'missing': 0, 'legacy': 2}
    assert after['hit_pct'] == 90
    # Idempotent: a second pass finds nothing to do and changes no row.
    snapshot = [p.db.one("SELECT note FROM runs WHERE id=?", (i,))['note'] for i in ids]
    assert bud.backfill_cache_telemetry(p.db, p.runs, now) == {'filled': 0, 'legacy': 0}
    assert [p.db.one("SELECT note FROM runs WHERE id=?", (i,))['note'] for i in ids] == snapshot



def test_cache_gap_cohort_keeps_the_previous_turn_outside_the_window(env):
    from ttp import budget as bud
    p = make(env)
    now = time.time()
    note = json.dumps({'cache_telemetry': {'complete': True, 'requests': None}})
    for started, ended in ((now - 3 * 86400 - 300, now - 3 * 86400 - 200),
                           (now - 3 * 86400 + 100, now - 3 * 86400 + 110)):
        p.db.x("INSERT INTO runs(role,started,ended,status,input_tokens,cache_read_tokens,note) "
               "VALUES('coordinator',?,?,'ok',10,90,?)", (started, ended, note))
    cohort = bud.coordinator_cache(p.db, now)['3d']['gap_5_60m']
    assert cohort['turns'] == 1 and cohort['hit_pct'] == 90


def _claude_overage_gate(p, tmp_path, now, **overage):
    """A Claude run whose rate_limit_event puts five_hour at 92%, with the given overage fields,
    recorded the way the daemon records a finished run; then Claude's gate."""
    from ttp import budget as bud, daemon as dm
    from ttp.providers import get_provider
    out = tmp_path / "o.jsonl"
    out.write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", **overage, "unifiedWindows": {
            "five_hour": {"utilization": 0.92, "resetsAt": now + 3 * 3600},
            "seven_day": {"utilization": 0.4, "resetsAt": now + 4 * 86400}}}}) + "\n")
    u = get_provider("claude").parse(out)
    with p.db.tx():
        dm.Daemon._record_readings(p.db, "claude", "acct", u, now)
    return bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)


def test_claude_overage_allowed_opens_paid_credits_past_the_line_under_the_dollar_caps(env, tmp_path):
    from ttp import credits
    p = make(env)
    now = time.time()
    g = _claude_overage_gate(p, tmp_path, now, overageStatus="allowed", isUsingOverage=False)
    assert g.regime == "windows" and "paid_credits" in g.numbers, (g.regime, g.numbers)
    assert g.level != "red" and g.allow_new_work, (g.level, g.reasons)
    r = credits.latest(p.db, "claude", now)
    assert r["account"] == "acct" and credits.paid_open(r) and r["overage"]["status"] == "allowed"
    # Spend since the first reading at the line is real money: the project's daily cap (100) holds it.
    p.db.x("UPDATE snapshots SET ts=ts-600")
    p.db.spend("claude", 120.0, "task:1", ts=now - 60)
    p.db.spend("claude", 500.0, "task:0", ts=now - 3600)   # before the line: plan spend
    g = _claude_overage_gate(p, tmp_path, now, overageStatus="allowed", isUsingOverage=True)
    assert g.numbers["paid_credits"]["usd"] == 120.0 and g.level == "red" and not g.allow_new_work, g.numbers
    assert any(r.startswith("cap reached") for r in g.reasons), g.reasons


def test_claude_overage_rejected_or_disabled_holds_at_the_line_like_no_reading(env, tmp_path):
    from ttp import credits
    p = make(env)
    now = time.time()
    g = _claude_overage_gate(p, tmp_path, now, overageStatus="rejected")
    assert g.regime == "windows" and "paid_credits" not in g.numbers
    assert g.level == "red" and not g.allow_new_work, (g.level, g.reasons)
    assert credits.latest(p.db, "claude", now)["paid"]["has_credits"] is False
    # No overage fields: unknown, not open; the gate is the same one.
    bare = _claude_overage_gate(p, tmp_path, now)
    assert credits.latest(p.db, "claude", now)["paid"]["has_credits"] is None
    off = _claude_overage_gate(p, tmp_path, now, overageStatus="rejected", isUsingOverage=False,
                               overageDisabledReason="org_level_disabled")
    assert credits.latest(p.db, "claude", now)["overage"]["disabled_reason"] == "org_level_disabled"
    assert (off.level, off.regime, off.allow_new_work, off.reasons) == (
        bare.level, bare.regime, bare.allow_new_work, bare.reasons), (off.reasons, bare.reasons)
    assert "paid_credits" not in off.numbers


def test_an_unbudgeted_task_starts_capped_at_its_tier_default_and_commits_it_under_the_dollar_caps(env, monkeypatch):
    """A task made without a budget gets budget.task_default_usd[tier] at dispatch: its run gets
    --max-budget-usd, and its start counts toward committed spend, so one tick cannot start several
    unbudgeted runs past the daily cap."""
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    from ttp.providers import get_provider
    _no_events(p)
    d = Daemon(p.base)
    d.cfg["budget"]["task_default_usd"] = {"light": 3.0, "standard": 8.0}
    prov = d.cfg.get("core_provider", "claude")
    d.gates = {prov: bud.Gate(provider=prov, regime="caps", max_parallel=5,
                              numbers={"daily_cap": 12.0, "spent_24h": 0.0, "spent_7d": 0.0})}
    light = p.db.add_task("light work", "s", kind="work", tier="light", origin="user", priority=1)
    std = p.db.add_task("standard work", "s", kind="work", tier="standard", origin="user", priority=2)
    over = p.db.add_task("more work", "s", kind="work", tier="standard", origin="user", priority=3)
    assert p.db.task(light)["budget_usd"] is None
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append((k["task"]["id"], k["budget_usd"])) or 0)
    d.dispatch()
    assert started == [(light, 3.0), (std, 8.0)], "3 + 8 committed; another 8 would pass the $12 cap"
    assert p.db.task(light)["budget_usd"] == 3.0 and p.db.task(std)["budget_usd"] == 8.0
    assert p.db.task(over)["status"] == "queued"
    argv, _ = get_provider("claude").build(role="worker", model=None, effort=None, cwd=str(p.root),
                                           budget_usd=started[1][1], read_only=False, schema=None,
                                           restrictions={})
    assert argv[argv.index("--max-budget-usd") + 1] == "8.00"


def test_an_unbudgeted_task_that_already_spent_gets_its_tier_default_on_top(env, monkeypatch):
    """A task that ran before it had a budget is not stopped by the default: its cap starts at what it spent."""
    p = make(env)
    from ttp.daemon import Daemon
    _no_events(p)
    d = Daemon(p.base)
    d.cfg["budget"]["task_default_usd"] = {"standard": 8.0}
    tid = p.db.add_task("resumed work", "s", kind="work", tier="standard", origin="user")
    p.db.update_task(tid, spent_usd=11.5)
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append((k["task"]["id"], k["budget_usd"])) or 0)
    d.dispatch()
    assert started == [(tid, 8.0)]
    assert p.db.task(tid)["budget_usd"] == 19.5


def test_ttp_task_add_takes_a_budget_and_otherwise_stores_its_tier_default(env, capsys):
    p = make(env)
    from ttp import cli
    cli.main(["task", "demo", "add", "capped", "--budget", "2.5"])
    cli.main(["task", "demo", "add", "default", "--tier", "deep"])
    rows = {r["title"]: r["budget_usd"] for r in p.db.q("SELECT title, budget_usd FROM tasks")}
    assert rows["capped"] == 2.5
    assert rows["default"] == p.config()["budget"]["task_default_usd"]["deep"]
    for bad in (["add", "zero", "--budget", "0"], ["list", "--budget", "3"]):
        with pytest.raises(SystemExit):
            cli.main(["task", "demo", *bad])
    assert "--budget" in capsys.readouterr().err


def test_idle_capacity_still_reported_when_the_only_ready_task_is_branch_held(env, monkeypatch):
    """A task dispatch holds behind a running task's branch (_branch_clash in _start_hold) will not
    fill a free slot, so it never hides idle capacity (review #3783)."""
    p, d, now, prov, check = _idle_setup(env, monkeypatch)
    holder = p.db.add_task("on feat/x", "s", kind="code", tier="light", origin="coordinator",
                           labels=["pr_branch:feat/x"])
    p.db.update_task(holder, status="running")
    p.db.add_task("also feat/x", "s", kind="code", tier="light", origin="coordinator", labels=["pr_branch:feat/x"])
    check(now)
    assert len(check(now + 3 * 3600)) == 1, "a branch-held task hid the free slot"


# the global cap counts only usage-billed spend -------------------------------------------------------
@pytest.mark.parametrize("env_key,state,settings,creds,want", [
    ("sk-x", {}, {}, None, "usage"),                                            # API key in the environment
    ("", {"primaryApiKey": "k"}, {}, None, "usage"),                             # API key login
    ("", {}, {"apiKeyHelper": "/bin/key"}, None, "usage"),                       # key helper
    ("", {}, {"env": {"ANTHROPIC_API_KEY": "k"}}, None, "usage"),                 # key in settings' env
    ("", {"oauthAccount": {"seatTier": "enterprise_usage_based", "billingType": "x_subscription"}}, {}, None, "usage"),
    ("", {"oauthAccount": {"organizationType": "claude_max"}}, {}, None, "plan"),
    ("", {"oauthAccount": {}}, {}, {"claudeAiOauth": {"subscriptionType": "max"}}, "plan"),
    ("", {"oauthAccount": {"organizationType": "claude_pro"}}, {}, None, "plan"),
    ("", {"oauthAccount": {"organizationType": "mystery"}}, {}, None, "unknown"),  # a login it cannot tell
    ("", {}, {}, None, "unknown"),                                               # no login at all
    ("", "not json", "[]", "{", "unknown"),                                      # unreadable files
])
def test_billing_mode_is_read_from_the_claude_code_login(env, tmp_path, monkeypatch, env_key, state, settings, creds, want):
    from ttp import localspend as ls
    root = tmp_path / "cfg"
    root.mkdir()
    for name, body in ((".claude.json", state), ("settings.json", settings), (".credentials.json", creds)):
        if body is not None:
            (root / name).write_text(body if isinstance(body, str) else json.dumps(body))
    assert ls.billing_mode({"CLAUDE_CONFIG_DIR": str(root), "ANTHROPIC_API_KEY": env_key}) == want


def _sessions_day(env, tmp_path, monkeypatch, login):
    """A project, the global cap on at $100, and one $15 Claude Code session outside tt-project in
    this machine's logs under `login`; returns (project, budget, start, end, now)."""
    from ttp import localspend as ls
    from ttp import project
    logs = _claude_logs(tmp_path, monkeypatch, login=login)
    p = make(env)
    now = time.time()
    b = _far_day(now)
    for k in ("day_start", "timezone"):
        project.set_account_setting(f"budget.{k}", b[k])
    project.set_account_setting("budget.global_daily_usd", 100)
    p.db.spend("claude", 1.0, "task:1")
    _log(logs, "s1", [_call("s1", "m1", now - 60, output_tokens=1_000_000)])
    ls.scan(ls.account_budget(), now)
    from ttp import globalcap as gcap
    start, end = gcap.day_bounds(b, now)
    return p, b, start, end, now


@pytest.mark.parametrize("login,key,line", [
    ({"oauthAccount": {"organizationType": "claude_max"}}, "plan_usd",
     "not counted: $15.00 subscription-plan sessions on this machine (list price; metered by the plan's 5h and 7d windows"),
    ({}, "unbilled_usd", "not counted: $15.00 estimated, not billed: sessions on this machine whose billing is unknown"),
])
def test_plan_and_unknown_billing_sessions_are_shown_apart_and_never_counted(env, tmp_path, monkeypatch, login, key, line):
    from ttp import budget as bud
    from ttp import globalcap as gcap
    from ttp.web import usage_line
    p, b, start, end, now = _sessions_day(env, tmp_path, monkeypatch, login)
    t = gcap.total(p.db, "claude", start, end, now)
    assert t["usd"] == 1.0 and t[key] == 15.0 and line in t["includes"], t
    p.set_config("budget.global_daily_usd", 10)
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert g.level == "green" and g.numbers["global_today"] == 1.0 and g.numbers[f"global_{key}"] == 15.0, g.numbers
    word = "subscription plan" if key == "plan_usd" else "estimated, not billed"
    assert f"$1.00 of $10 global; not counted: $15.00 {word}" in usage_line(g.numbers, now), usage_line(g.numbers, now)


def test_usage_billed_sessions_count_and_trip_the_cap(env, tmp_path, monkeypatch):
    from ttp import budget as bud
    from ttp import globalcap as gcap
    from ttp.web import usage_line
    p, b, start, end, now = _sessions_day(env, tmp_path, monkeypatch,
                                          {"oauthAccount": {"seatTier": "enterprise_usage_based"}})
    t = gcap.total(p.db, "claude", start, end, now)
    assert t["usd"] == 16.0 and t["plan_usd"] == t["unbilled_usd"] == t["stale_usd"] == 0, t
    assert "not counted" not in t["includes"], t["includes"]
    p.set_config("budget.global_daily_usd", 10)
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert g.level == "red" and "global daily cap reached" in " ".join(g.reasons), (g.level, g.reasons)
    assert "not counted" not in usage_line(g.numbers, now)


def test_tt_project_runs_of_any_provider_and_other_models_are_not_other_sessions(env, tmp_path, monkeypatch):
    # A provider that drives Claude Code against another vendor's endpoint writes the same logs: its
    # runs are tt-project's own (booked by that provider), and a non-Claude model is never Anthropic
    # spend, so neither shows up as other Claude Code sessions.
    from ttp import globalcap as gcap
    from ttp import localspend as ls
    logs = _claude_logs(tmp_path, monkeypatch)
    p = make(env)
    other = _other_project(tmp_path, "other")
    now = time.time()
    M = 1_000_000
    _log(logs, "chat-run", [_call("chat-run", "c1", now - 60, output_tokens=M)])
    _log(logs, "glm", [_call("glm", "g1", now - 60, model="zai-org/GLM-5.3", output_tokens=M)])
    _log(logs, "user", [_call("user", "u1", now - 60, output_tokens=M),
                        _call("user", "u2", now - 59, model="other-vendor-model", output_tokens=M)])
    other.x("INSERT INTO runs(role,provider,started,ended,status,session_id) VALUES('worker','chat',?,?,'ok','chat-run')",
            (now - 100, now - 30))
    b = _far_day(now)
    ls.scan(b, now)
    start, end = gcap.day_bounds(b, now)
    e = ls.estimate(b, start, end, now)
    assert e["usd"] == 15 and e["sessions"] == 1 and e["unknown"] == [], e


def test_remote_sessions_count_only_from_a_usage_billed_login_and_a_stale_machine_never_trips_the_cap(env, monkeypatch):
    from ttp import budget as bud
    from ttp import globalcap as gcap
    p = make(env)
    now = time.time()
    start, end = now - 3600, now + 3600
    monkeypatch.setattr(gcap, "OTHER_SOURCES", [])
    monkeypatch.setattr(gcap, "targets", lambda: [])
    key = gcap.account_key("claude", "acct-a")

    def push(host, usd, sessions, billing):
        o = {"provider": "claude", "key": key, "usd": sessions, "sessions": 1, "estimated": True}
        if billing:
            o["billing"] = billing
        rec = {"v": 1, "host": host, "sent": now, "windows": [{"start": start, "end": end, "projects": ["far"],
                                                                "rows": [{"provider": "claude", "key": key, "usd": usd}],
                                                                gcap.SESSIONS: o}]}
        return gcap.receive((json.dumps(rec) + "\n").encode(), host, now)[1]
    assert push("api", 2.0, 3.0, "usage") == 0
    assert push("max", 4.0, 96.0, "plan") == 0
    assert push("old", 5.0, 7.0, None) == 0          # an older tt-project: billing cannot be told
    assert push("bad", 1.0, 1.0, "free") == 2         # an unknown billing word is a malformed record
    t = gcap.total(p.db, "claude", start, end, now + 1, account="acct-a")
    assert t["usd"] == 2 + 3 + 4 + 5 and t["remote_sessions_usd"] == 3.0, t
    assert t["plan_usd"] == 96.0 and t["unbilled_usd"] == 7.0 and t["stale_usd"] == 0, t
    assert "$96.00 subscription-plan sessions on max" in t["includes"] and "$7.00 estimated, not billed: sessions on old" in t["includes"]
    # Past STALE_S every machine is stale: its billed number is shown with what it adds, not counted.
    later = now + gcap.STALE_S + 1
    t = gcap.total(p.db, "claude", start, end, later, account="acct-a")
    assert t["usd"] == 0 and t["stale_usd"] == 14.0 and sorted(t["stale"]) == ["api", "max", "old"], t
    assert "$14.00 stale: api $5.00, max $4.00, old $5.00" in t["includes"], t["includes"]
    p.set_config("budget.global_daily_usd", 10)
    monkeypatch.setattr(gcap, "window", lambda b, n: (start, end, False))
    monkeypatch.setattr(gcap, "account_of", lambda prov: "acct-a")
    g = bud.evaluate(p.db, p.config(), "claude", [], later)
    assert g.level != "red" and g.numbers["global_stale_usd"] == 14.0, (g.level, g.reasons, g.numbers)
    g = bud.evaluate(p.db, p.config(), "claude", [], now + 1)
    assert g.level == "red", (g.level, g.reasons)
