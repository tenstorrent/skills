# Landing check time tests (gatetime) of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file. Add new
# gatetime tests here.
from __future__ import annotations


def test_gate_time_is_recorded_by_each_passing_push_check_and_measured_over_a_day_and_the_last_runs(env):
    from ttp import gatetime, push
    p = make(env)
    assert gatetime.status_line(p.state, p.config()) == ""
    push.record_check_s(p, 120)   # the push path's record of a passing push check run keeps its history
    now = time.time()
    for i, s in enumerate([900, 300, 240, 200, 180, 160, 150, 140, 130, 120]):   # all older than a day
        gatetime.record(p.state, s, now=now - 2 * 86400 + i)
    gatetime.record(p.state, 0)   # no sample
    st = gatetime.stats(p.state, now)
    assert st["day"] == {"n": 1, "median_s": 120.0, "p90_s": 120.0}
    assert st["last"]["n"] == 10 and st["last"]["median_s"] == 170.0 and st["last"]["p90_s"] == 300.0
    line = gatetime.status_line(p.state, p.config(), now)
    assert line.startswith("landing checks: 24 h median 2.0 min") and "over the" not in line
    from ttp import audit
    assert any(x.startswith("landing checks:") for x in audit.lines(p.db, now=now, p=p))


def test_gate_time_over_smoke_max_raises_one_coordinator_observation_a_day_naming_the_slowest_tests(env):
    from ttp import gatetime
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()
    for i in range(10):
        gatetime.record(p.state, 700 + i, now=now - 100 + i)
    logs = p.state / "pushes"
    logs.mkdir(exist_ok=True)
    (logs / "t1-a.log").write_text("== slowest durations ==\n95.10s call     tests/a.py::test_slow\n"
                                   "3.00s setup    tests/a.py::test_slow\n40.00s call     tests/b.py::test_mid\n")
    assert "over the 10.0 min smoke limit" in gatetime.status_line(p.state, p.config(), now)
    d = Daemon(p.base)
    d.check_gate_time(every_s=0, now=now)
    d.check_gate_time(every_s=0, now=now + 60)   # once a day
    rows = p.db.q("SELECT kind, status, text FROM events WHERE fingerprint LIKE 'gate_slow:%'")
    assert len(rows) == 1 and rows[0]["status"] == "queued"
    text = rows[0]["text"]
    assert "tests/a.py::test_slow 95s, tests/b.py::test_mid 40s" in text and "delivery.post_land_checks" in text
    d.cfg = {**d.cfg, "checks": {**(d.cfg.get("checks") or {}), "smoke_max_s": 0}}   # 0: off
    assert gatetime.flag(p.db, p.state, d.cfg, logs, now + 2 * 86400) == ""
    # under the limit: nothing
    assert gatetime.flag(p.db, p.state, {"checks": {"smoke_max_s": 1000}}, logs, now + 2 * 86400) == ""
    # no --durations lines: it says how to get them
    (logs / "t1-a.log").write_text("5 passed\n")
    assert "--durations=30" in gatetime.flag(p.db, p.state, {}, logs, now + 3 * 86400)


def test_post_land_checks_and_smoke_max_s_are_known_settings_and_post_land_checks_are_validated(env):
    from ttp import project
    good = {"delivery": {"post_land_checks": ["pytest -q tests", {"run": "make long", "host": "local"}]},
            "checks": {"smoke_max_s": 300}}
    assert not [x for x in project.config_problems(good) if "post_land" in x or "smoke_max" in x]
    bad = project.config_problems({"delivery": {"post_land_checks": [{"host": "local"}]}})
    assert any(x.startswith("delivery.post_land_checks:") for x in bad)
    assert gatetime_default_is_ten_minutes()


def gatetime_default_is_ten_minutes() -> bool:
    from ttp import gatetime
    return gatetime.max_s({}) == 600 == gatetime.max_s({"checks": {"smoke_max_s": "x"}}) and gatetime.AIM_S == 300
