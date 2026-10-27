# Suite cost watch tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there). Add new suite-cost tests here, not there.
from __future__ import annotations


def _suite_out(cpu, tests=100):
    return f"{tests} passed in 3.0s\nttp-suite: wall 10.0s cpu {cpu}s workers 4\n"


def _suite_events(db):
    return db.q("SELECT * FROM events WHERE kind='suite_cost_regression'")


def test_suite_cost_parse_and_missing_line(env):
    from ttp import suitecost
    assert suitecost.parse(_suite_out(50)) == {"wall": 10.0, "cpu": 50.0, "tests": 100}
    assert suitecost.parse("100 passed in 3s\n") is None
    assert suitecost.parse("ttp-suite: wall 1.0s cpu 2.0s workers 2\n") is None
    p = make(env)
    assert suitecost.record(p.db, "b1", "abc", "1.0.0", "100 passed\n") == ""
    assert not p.db.kv(suitecost.SAMPLES_KEY) and suitecost.audit_line(p.db) == ""


def test_suite_cost_threshold_dedupe_and_rearm(env):
    from ttp import suitecost
    p = make(env)
    t = 1_000_000.0
    for i in range(5):   # five samples: no judgement yet, even for a huge one
        assert suitecost.record(p.db, f"b{i}", "s", "1.0.0", _suite_out(50), t + i) == ""
    assert suitecost.record(p.db, "b5", "s", "1.0.1", _suite_out(70), t + 5) == ""   # 1.4x: under 1.5x
    text = suitecost.record(p.db, "b6", "s6", "1.0.2", _suite_out(100), t + 6)       # 2x
    assert "batch b6" in text and "1.0.2" in text and "first batch over 1.5x: b6" in text
    assert len(_suite_events(p.db)) == 1 and _suite_events(p.db)[0]["severity"] == "low"
    assert suitecost.record(p.db, "b7", "s7", "1.0.3", _suite_out(110), t + 7) == ""   # same episode
    assert len(_suite_events(p.db)) == 1
    # back under 1.2x of the median re-arms; the next regression raises again, naming its own first batch
    for i in range(8, 20):
        suitecost.record(p.db, f"b{i}", "s", "1.0.4", _suite_out(50), t + i)
    assert p.db.kv(suitecost.STATE_KEY) == {}
    assert "first batch over 1.5x: b20" in suitecost.record(p.db, "b20", "s", "1.0.5", _suite_out(500), t + 20)
    assert len(_suite_events(p.db)) == 2
    line = suitecost.audit_line(p.db, t + 21)
    assert "cpu 500s" in line and "7-day median" in line and "ms cpu/test" in line


_SLOW_SUITE = {
    "conftest.py": (
        "import pytest\n"
        "FAKE = {'test_a': 0.5, 'test_b': 2.0, 'test_c': 1.0, 'test_d': 3.0}   # a fake clock: no test sleeps\n"
        "@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    out = yield\n"
        "    out.get_result().duration = {'setup': 0.25, 'call': FAKE.get(item.name, 0.0)}.get(call.when, 0.0)\n"),
    "test_s.py": ("import os\n"
                  "def test_a(): assert 'TTP_PYTEST_DURATIONS' not in os.environ\n"
                  "def test_b(): assert 'ttp_pytest_durations' not in os.environ.get('PYTEST_ADDOPTS', '')\n"
                  "def test_c(): pass\n"),
}


def _slow_run(suite, record, top=None, files=(".",)):
    from ttp import slowtests
    base = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTHONPATH")}
    e = {**base, **slowtests.env(record, base)}
    if top:
        e["TTP_PYTEST_DURATIONS_TOP"] = str(top)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *files], cwd=suite,
                          env=e, capture_output=True, text=True)


def test_the_durations_plugin_saves_the_slowest_tests_and_selects_nothing(env, tmp_path):
    from ttp import slowtests
    suite = tmp_path / "suite"
    suite.mkdir()
    for name, text in _SLOW_SUITE.items():
        (suite / name).write_text(text)
    record = tmp_path / slowtests.FILE
    r = _slow_run(suite, record, top=2)
    assert r.returncode == 0 and "3 passed" in r.stdout, r.stdout   # every test ran, and none saw the plugin
    data = json.loads(record.read_text())
    assert data["count"] == 3
    # setup + call + teardown, as --durations counts them; only the 2 slowest kept
    assert [(t["id"], t["s"]) for t in data["tests"]] == [("test_s.py::test_b", 2.25), ("test_s.py::test_c", 1.25)]
    # a second pytest run of the same check merges into the file: its own times win
    (suite / "test_t.py").write_text("def test_d(): pass\n")
    assert _slow_run(suite, record, files=("test_t.py",)).returncode == 0
    assert slowtests.load(record) == [("test_t.py::test_d", 3.25), ("test_s.py::test_b", 2.25),
                                      ("test_s.py::test_c", 1.25)]
    assert not list(tmp_path.glob("*.tmp")), "no temporary file left behind"
    # no file asked for: the plugin writes nothing
    base = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTHONPATH", "TTP_PYTEST_DURATIONS")}
    env2 = {**base, **slowtests.env(record, base)}
    env2.pop("TTP_PYTEST_DURATIONS")
    before = record.read_text()
    assert subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "."], cwd=suite,
                          env=env2, capture_output=True).returncode == 0
    assert record.read_text() == before


def test_the_durations_plugin_logs_each_tests_start_and_end_as_they_happen(tmp_path):
    from ttp import slowtests
    suite = tmp_path / "suite"
    suite.mkdir()
    for name, text in _SLOW_SUITE.items():
        (suite / name).write_text(text)
    (suite / "test_live.py").write_text("import os\ndef test_e(): assert 'TTP_PYTEST_DURATIONS_LIVE' not in os.environ\n")
    record, live = tmp_path / slowtests.FILE, tmp_path / "live.jsonl"
    base = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTHONPATH")}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "."], cwd=suite,
                       env={**base, **slowtests.env(record, base, live=live)}, capture_output=True, text=True)
    assert r.returncode == 0 and "4 passed" in r.stdout, r.stdout   # no test saw the live log's setting
    rows = [json.loads(x) for x in live.read_text().splitlines()]
    assert [(x["id"], "start" in x) for x in rows] == [(i, k) for i in ("test_live.py::test_e", "test_s.py::test_a",
                                                                       "test_s.py::test_b", "test_s.py::test_c")
                                                       for k in (True, False)], "each start, then its end"
    assert {x["id"]: x["s"] for x in rows if "s" in x}["test_s.py::test_b"] == 2.25
    assert all(abs(x["start"] - time.time()) < 600 for x in rows if "start" in x)
    assert "live.jsonl" not in str(slowtests.env(record, base)), "no live log unless asked for"


def test_slowest_tests_load_and_display(tmp_path):
    sys.path.insert(0, str(RUNTIME))
    from ttp import slowtests
    f = tmp_path / slowtests.FILE
    assert slowtests.load(f) == [] and slowtests.lines(f) == [], "no file: nothing to show"
    for bad in ("not json", "[]", '{"tests": [{"id": "x"}]}', '{"tests": [{"id": "x", "s": "slow"}]}'):
        f.write_text(bad)
        assert slowtests.load(f) == [] and slowtests.lines(f) == [], bad
    f.write_text(json.dumps({"tests": [{"id": f"t.py::t{i}", "s": i * 1.5} for i in range(8)], "count": 8}))
    assert slowtests.lines(f) == [f"slowest 5 tests ({f}):", "    10.5s t.py::t7", "     9.0s t.py::t6",
                                  "     7.5s t.py::t5", "     6.0s t.py::t4", "     4.5s t.py::t3"]
    assert len(slowtests.lines(f, 2)) == 3


def test_ttp_checks_saves_a_pytest_checks_slowest_tests_and_its_result_shows_the_top_5(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, slowtests
    p = make(env)
    repo, py = _venv_repo(tmp_path)
    rows = [{"id": f"t.py::t{i}", "s": float(i)} for i in range(30)]
    # the venv's python stands in for pytest and its plugin: it writes where TTP_PYTEST_DURATIONS says
    py.write_text(f"#!/bin/sh\necho \"$PYTEST_ADDOPTS\" >> {tmp_path / 'opts'}\n"
                  f"[ -n \"$TTP_PYTEST_DURATIONS\" ] && echo '{json.dumps({'tests': rows, 'count': 30})}' "
                  "> \"$TTP_PYTEST_DURATIONS\"\nexit 0\n")
    run = tmp_path / "run"
    run.mkdir()
    (run / slowtests.FILE).write_text('{"tests": [{"id": "stale::t", "s": 99}]}')
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.chdir(repo)
    cli.main(["checks", "--", "pytest -q"])
    assert "-p ttp_pytest_durations" in (tmp_path / "opts").read_text()
    assert slowtests.load(run / slowtests.FILE)[0] == ("t.py::t29", 29.0), "this run's, never an earlier one's"
    (run / cli.CHECKS_RC).write_text("0\n")
    capsys.readouterr()
    assert cli._checks_result(run) == 0
    out = capsys.readouterr().out
    assert "ttp checks: slowest 5 tests" in out and "29.0s t.py::t29" in out and "25.0s t.py::t25" in out
    assert "t.py::t24" not in out and "stale" not in out
