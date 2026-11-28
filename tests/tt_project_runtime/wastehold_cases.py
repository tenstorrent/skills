# Waste-hold tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. The failed-run guard holds the task or loop that keeps failing, never one
# that failed once.
from __future__ import annotations


def _wh_failures(p, task, costs, now, at=0):
    # `at` places the task's runs one after another with the others' (one worker, not several)
    for i, cost in enumerate(costs):
        t = now - 3500 + (at + i) * 300
        p.db.x("INSERT INTO runs(task,role,provider,status,started,ended,cost_usd) "
               "VALUES(?,'worker','claude','timeout',?,?,?)", (task, t, t + 200, cost))


def test_one_cheap_failure_holds_no_task_but_a_repeated_failure_does(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("UPDATE tasks SET status='done'")
    ordered = p.db.add_task("answer the user", "s", kind="work", tier="light", origin="user", reply_chat="c1")
    urgent_one = p.db.add_task("fast", "urgent: the user waits", kind="work", tier="light", origin="user", priority=1)
    loop = p.db.add_task("again and again", "s", kind="work", tier="light", origin="user")
    for at, t in enumerate((ordered, urgent_one)):
        _wh_failures(p, t, [4.5], now, at)
    _wh_failures(p, loop, [4.5, 4.5], now, 2)   # one worker's runs in turn: the three tasks' waste passes its limit
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert {h["task"] for h in g.waste_holds} == {loop}, g.waste_holds
    assert not bud.waste_hold(g, p.db.task(ordered)) and not bud.waste_hold(g, p.db.task(urgent_one))
    assert bud.waste_hold(g, p.db.task(loop))


def test_a_failing_source_does_not_hold_a_priority_task(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("UPDATE tasks SET status='done'")
    for i in range(3):
        _wh_failures(p, p.db.add_task(f"chore {i}", "s", kind="work", tier="light", origin="harness"), [4.0, 4.0], now, 2 * i)
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert {h["origin"] for h in g.waste_holds} - {None} == {"harness"}, (g.level, g.reasons, g.waste_holds)
    new = lambda **k: p.db.task(p.db.add_task("new", k.pop("spec", "s"), kind="work", tier="light", origin="harness", **k))
    assert bud.waste_hold(g, new())
    assert not bud.waste_hold(g, new(spec="urgent: ordered"), True), "the daemon passes Daemon._priority_task"
