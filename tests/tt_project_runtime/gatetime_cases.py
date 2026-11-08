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
                                   "3.00s setup    tests/a.py::test_slow\nttp-suite:    40.00s tests/b.py::test_mid\n")
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


def test_smoke_max_s_is_an_account_key_the_project_value_overrides(env):
    from ttp import cli, gatetime
    cli.main(["config", "--account", "checks.smoke_max_s", "900"])
    assert gatetime.max_s({}) == 900, "unset in the project: the account's"
    assert gatetime.max_s({"checks": {"smoke_max_s": 120}}) == 120, "project first"
    assert gatetime.max_s({"checks": {"smoke_max_s": "x"}}) == 900, "malformed: the account's"


def gatetime_default_is_ten_minutes() -> bool:
    from ttp import gatetime
    return gatetime.max_s({}) == 600 == gatetime.max_s({"checks": {"smoke_max_s": "x"}}) and gatetime.AIM_S == 300


def test_waits_line_gives_checks_wait_to_start_and_approved_to_landed_and_a_checks_run_records_its_wait(
        env, tmp_path, monkeypatch, capsys):
    from ttp import audit, cli, gatetime
    p = make(env)
    now = time.time()
    assert gatetime.waits_line(p.db, p.state, now) == ""
    for i, s in enumerate([0, 60, 600, 7200, 8100]):   # 0: started at once, still a sample
        gatetime.record_wait(p.state, s, now=now - 3600 + i)
    gatetime.record_wait(p.state, 99999, now=now - 2 * 86400)   # older than a day
    q = "INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)"
    for t, (created, st) in enumerate([(now - 7200, "landed"), (now - 3600, "pushed"), (now - 60, "approved")], 1):
        p.db.x(q, (t, f"b{t}", f"h{t}", "main", st, created, now - 10))
    line = gatetime.waits_line(p.db, p.state, now)
    assert line == ("waits (24 h): checks to start median 10 min, p90 2 h 15 min (5 runs); "
                    "approved to landed median 1 h 29 min, p90 1 h 59 min (2 changes)"), line
    assert any(x.startswith("waits (24 h): checks to start") and "approved" not in x
               for x in audit.lines(p.db, now=now, p=p))
    # A full `ttp checks` run keeps how long it waited from its arrival to its start.
    p2, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    before = len(gatetime._rows(p2.state, gatetime.WAIT_LOG))
    monkeypatch.delenv("TTP_CHECKS_WAIT_KEPT", raising=False)   # never inherited; the run keeps it to itself
    monkeypatch.setattr(cli, "_checks_arrived", lambda: time.time() - 120)
    cli.main(["checks", "--", "true"])
    rows = gatetime._rows(p2.state, gatetime.WAIT_LOG)
    assert len(rows) == before + 1 and 119 <= rows[-1][1] < 200, rows


def test_slow_landing_check_advice_never_moves_tests_to_post_land_checks_while_nothing_runs_them(env):
    # Until this release runs delivery.post_land_checks after landing, no advice (the gate_slow observation,
    # the `ttp status` line, the guide) tells a coordinator to move tests there: they would not run at all.
    from ttp import gatetime
    runs = getattr(gatetime, "POST_LAND_RUNS", False)
    p = make(env)
    now = time.time()
    for i in range(10):
        gatetime.record(p.state, 700 + i, now=now - 100 + i)
    line = gatetime.status_line(p.state, p.config(), now)
    text = gatetime.flag(p.db, p.state, p.config(), p.state / "no-logs", now)
    guide = (RUNTIME.parent / "docs" / "guide.md").read_text()
    smoke = guide[guide.index("- Smoke before landing:"):]
    smoke = smoke[:smoke.index("\n- ")]
    for said in (line, text, smoke):
        assert "post_land_checks" in said, said
        assert ("not run" in said or "nothing runs" in said) != runs, said
        assert runs or "which run after landing" not in said, said
    # The flag tells the truth: while it is off, no runtime module but the setting's own reads the list.
    readers = sorted(f.name for f in (RUNTIME / "ttp").glob("*.py")
                     if "post_land_checks" in f.read_text() and f.name not in ("project.py", "gatetime.py"))
    assert runs or not readers, readers


def test_ttp_checks_keeps_its_wait_recorded_flag_out_of_check_commands_and_later_runs(env, tmp_path, monkeypatch):
    # The flag that a rerun's wait was recorded is never inherited: not by the check commands, and not by a
    # later `ttp checks` in the same process, which records its own wait.
    from ttp import cli, gatetime
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv("TTP_CHECKS_WAIT_KEPT", raising=False)
    monkeypatch.setattr(cli, "_checks_arrived", lambda: time.time() - 60)
    seen = tmp_path / "seen"
    cli.main(["checks", "--", f"sh -c 'echo \"[${{TTP_CHECKS_WAIT_KEPT:-}}]\" >> {seen}'"])
    assert "TTP_CHECKS_WAIT_KEPT" not in os.environ
    cli.main(["checks", "--", f"sh -c 'echo \"<${{TTP_CHECKS_WAIT_KEPT:-}}>\" >> {seen}'"])
    assert seen.read_text().split() == ["[]", "<>"], seen.read_text()
    assert len(gatetime._rows(p.state, gatetime.WAIT_LOG)) == 2
