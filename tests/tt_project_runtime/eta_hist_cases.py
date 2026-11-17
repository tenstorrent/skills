# ETA numbers from history (runtime/ttp/etahist.py and the p90, size, load and queue-depth parts of eta.py,
# etaledger.shown, `ttp progress report --expected-wait-s`) of tests/test_tt_project_runtime.py, which runs
# this file in its own namespace (see _include_cases there). Fake records only.
from __future__ import annotations


def _eh_runs(p, now, kind, runs, spec="s", wait=100):
    for d in runs:
        tid = p.db.add_task(f"past {kind} {d}", spec, kind=kind, status="done")
        c = now - 86400
        p.db.x("UPDATE tasks SET created=? WHERE id=?", (c, tid))
        p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?, 'worker', ?, ?, 'ok')",
               (tid, c + wait, c + wait + d))


def test_eta_percentile_is_the_median_at_half_and_survives_bad_input():
    from ttp import etahist
    import statistics
    for xs in ([5.0], [1.0, 9.0], [3.0, 1.0, 2.0], [4.0, 1.0, 3.0, 2.0, 10.0, 7.0]):
        assert etahist.pct(xs, 0.5) == statistics.median(xs)
    xs = list(range(1, 11))
    assert etahist.pct(xs, 0.9) == pytest.approx(9.1) and etahist.pct(xs, 1.0) == 10 and etahist.pct(xs, 0) == 1
    # Out of range, NaN and non-numbers never raise: clamped or skipped.
    assert etahist.pct(xs, 7) == 10 and etahist.pct(xs, -1) == 1 and etahist.pct(xs, float("nan")) == 1
    assert etahist.pct([1.0, float("inf"), float("nan"), True, "x", None, 3.0], 0.5) == 2.0
    assert etahist.pct([], 0.9) is None and etahist.pct(None, 0.9) is None


def test_eta_size_load_and_depth_factors_are_bounded_and_never_shrink():
    from ttp import etahist
    assert [etahist.size_of(n) for n in (0, 1499, 1500, 3999, 4000, 99999)] == [
        "short", "short", "medium", "medium", "long", "long"]
    assert etahist.size_of("x" * 5000) == "long"
    for bad in (None, -5, float("nan"), True, "", [], {}):
        assert etahist.size_of(bad) in ("short", "medium"), bad   # a bad length is medium, text counts its characters
    assert etahist.load_factor(0.3) == 1.0 and etahist.load_factor(1.0) == 1.0
    assert etahist.load_factor(2.0) == pytest.approx(1.25) and etahist.load_factor(500) == etahist.MAX_LOAD_X
    for bad in (None, float("nan"), True, "9"):
        assert etahist.load_factor(bad) == 1.0, bad
    assert etahist.depth_factor(0, 4) == 1.0 and etahist.depth_factor(4, 4) == 1.0
    assert etahist.depth_factor(6, 4) == pytest.approx(1.5) and etahist.depth_factor(999, 4) == etahist.MAX_DEPTH_X
    assert etahist.depth_factor(3, 0) == etahist.depth_factor(3, 1) == etahist.MAX_DEPTH_X and etahist.depth_factor("x", 2) == 1.0
    assert etahist.depth_factor(None, None) == 1.0


def test_eta_host_load_reads_the_override_and_never_raises(monkeypatch):
    from ttp import etahist, hostload
    monkeypatch.setenv("TTP_ETA_LOAD", "1.75")
    assert etahist.host_load() == 1.75
    for bad in ("off", "", "x", "nan", "inf", "-2"):
        monkeypatch.setenv("TTP_ETA_LOAD", bad)
        assert etahist.host_load() is None, bad
    monkeypatch.delenv("TTP_ETA_LOAD")
    monkeypatch.setattr(hostload, "loads", lambda: (8.0, 4.0, 2.0))
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    assert etahist.host_load() == 2.0
    monkeypatch.setattr(hostload, "loads", lambda: None)
    assert etahist.host_load() is None


def test_eta_conservative_time_is_the_p90_walk_and_never_before_the_likely_one(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eh_runs(p, now, "work", [600] * 8 + [3000, 6000])      # p50 600, p90 3000 + 0.1 * 3000
    t = p.db.add_task("next", "s", kind="work")
    e = eta.estimate(p.db, t, now)
    assert e["end"] == pytest.approx(now + 100 + 600)
    assert e["p90_end"] == pytest.approx(now + 100 + 3300)
    assert e["conservative"] == pytest.approx(now + 100 + 3300)     # no measured multiple yet: the p90 walk alone
    assert e["conservative"] > e["end"] and "conservative" in eta.field(e, p, now)
    # Identical samples have no spread: the conservative time falls back to the configured multiple.
    q = make(env)
    _eh_runs(q, now, "work", [600] * 6)
    u = q.db.add_task("next", "s", kind="work")
    e = eta.estimate(q.db, u, now)
    assert e["end"] == pytest.approx(now + 700) and e["conservative"] == pytest.approx(now + 2 * 700)


def test_eta_state_lines_give_urgent_tasks_the_p90_conservative_time(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eh_runs(p, now, "work", [600] * 8 + [3000, 6000])      # p50 600, p90 3300: far from the 2x fallback
    t = p.db.add_task("urgent next", "s", kind="work", priority=1)
    lines = eta.state_lines(p.db, p, now)
    assert "likely = measured median p50, conservative = p90" in lines[0]
    e = eta.estimate(p.db, t, now)
    assert e["conservative"] == pytest.approx(now + 100 + 3300)
    assert lines[1] == f"- #{t} urgent next: {eta.field(e, p, now)}"
    assert "conservative" in lines[1]


def test_eta_conservative_keeps_the_ledgers_measured_multiple_when_it_is_larger(env):
    from ttp import eta, etaledger
    p = make(env)
    now = time.time()
    _eh_runs(p, now, "work", [600] * 8 + [3000, 6000])
    t = p.db.add_task("next", "s", kind="work")
    p.db.set_kv(etaledger.KEY, {"on": True, "factors": {"work": 1.5}, "conservative_x": 6.0, "at": now})
    e = eta.estimate(p.db, t, now)
    left = 1.5 * 700
    assert e["end"] == pytest.approx(now + left) and e["factor"] == 1.5
    assert e["conservative"] == pytest.approx(now + 6.0 * left)     # the measured multiple beats the scaled p90 (4950)
    p.db.set_kv(etaledger.KEY, {"on": True, "factors": {"work": 1.5}, "conservative_x": 1.25, "at": now})
    e = eta.estimate(p.db, t, now)
    assert e["conservative"] == pytest.approx(now + 1.5 * 3400)     # the scaled p90 beats a small measured multiple


def test_eta_run_samples_are_by_kind_and_size_once_there_are_enough(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eh_runs(p, now, "work", [100] * 5, spec="s" * 5000)           # long specs: fast
    _eh_runs(p, now, "work", [1000] * 6, spec="s")                 # short specs: slow
    long_t = p.db.add_task("l", "s" * 5000, kind="work")
    short_t = p.db.add_task("s", "s", kind="work")
    assert eta.estimate(p.db, long_t, now)["end"] == pytest.approx(now + 100 + 100)
    assert eta.estimate(p.db, short_t, now)["end"] == pytest.approx(now + 100 + 1000)
    # A size with fewer than MIN_SIZE samples falls back to the kind's samples, never to a thin median.
    mid = p.db.add_task("m", "s" * 2000, kind="work")
    _eh_runs(p, now, "work", [9000] * 2, spec="s" * 2000)
    e = eta.estimate(p.db, mid, now)
    assert e["end"] == pytest.approx(now + 100 + eta.etahist.pct([100] * 5 + [1000] * 6 + [9000] * 2, 0.5))


def test_eta_host_load_and_queue_depth_only_lengthen_and_are_named(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eh_runs(p, now, "work", [1000] * 6)
    t = p.db.add_task("next", "s", kind="work")
    base = eta.estimate(p.db, t, now)
    assert base["end"] == pytest.approx(now + 1100) and not base["notes"]
    busy = eta._Est(p.db, now, load=2.0).task(t)
    assert busy["end"] == pytest.approx(now + 100 + 1250) and busy["notes"] == ["host load x1.25"]
    idle = eta._Est(p.db, now, load=0.1).task(t)
    assert idle["end"] == base["end"] and not idle["notes"]
    deep = eta._Est(p.db, now, load=None, depth=(6, 2)).task(t)
    assert deep["end"] == pytest.approx(now + 200 + 1000) and "queue depth x2.00" in deep["notes"]
    capped = eta._Est(p.db, now, load=99.0, depth=(999, 1)).task(t)
    assert capped["end"] == pytest.approx(now + 200 + 1500)          # both bounded
    assert "adjusted: queue depth x2.00" in eta.text(deep, p, now) and "p90" in eta.text(deep, p, now)
    # The queue count takes runnable queued tasks only (a task waiting on another is not ahead of anyone).
    dep = p.db.add_task("after", "s", kind="work", depends_on=[t])
    est = eta._Est(p.db, now, load=None)
    est._depthx()
    assert est._depth_in == (1, 0)
    assert dep


def test_eta_outside_wait_uses_an_expected_wait_from_the_handoff_and_reports(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eh_runs(p, now, "work", [1200] * 6)
    probe = "ttp detach --check --host box /runs/7/job"
    basis = "the job's own log: step 40 of 100 after 2 h"
    t = _eta_waiting(p, now, retry_when=probe, expected_wait_s=3000, eta_basis=basis, waiting_since=now - 1000)
    e = eta.estimate(p.db, t, now)
    assert e["end"] == pytest.approx(now + 2000 + 1200) and [k for k, _ in e["legs"]] == ["outside work", "run"]
    # An eta wins over an expected wait; a later report's expected wait counts from the report's own time.
    t2 = _eta_waiting(p, now, retry_when=probe, eta=now + 500, expected_wait_s=3000, eta_basis=basis,
                      waiting_since=now - 1000)
    assert eta.estimate(p.db, t2, now)["end"] == pytest.approx(now + 500 + 1200)
    rep = [{"task": str(t), "ts": now - 60, "expected_wait_s": 900, "eta_basis": basis}]
    assert eta.outside_wait(p.db.task(t), now, rep)["at"] == pytest.approx(now + 840)
    # Ignored: no basis, no start of the wait, past, zero, negative, over the cap, non-numbers, booleans.
    for bad in ({"expected_wait_s": 3000, "waiting_since": now - 10}, {"eta_basis": basis, "expected_wait_s": 3000},
                {"eta_basis": basis, "expected_wait_s": 500, "waiting_since": now - 1000},
                {"eta_basis": basis, "expected_wait_s": 0, "waiting_since": now - 10},
                {"eta_basis": basis, "expected_wait_s": -5, "waiting_since": now - 10},
                {"eta_basis": basis, "expected_wait_s": eta.MAX_WAIT_S + 1, "waiting_since": now - 10},
                {"eta_basis": basis, "expected_wait_s": "soon", "waiting_since": now - 10},
                {"eta_basis": basis, "expected_wait_s": True, "waiting_since": now - 10},
                {"eta_basis": basis, "expected_wait_s": float("inf"), "waiting_since": now - 10}):
        b = _eta_waiting(p, now, retry_when=probe, **bad)
        e = eta.estimate(p.db, b, now)
        assert e["end"] is None and "no measured finish yet" in e["why"], bad


def test_progress_report_carries_an_expected_wait_only_when_valid(env, capsys):
    from ttp import progress
    for good in (900, 12.5):
        assert progress.record("result", "k", "r", env={}, expected_wait_s=good)["expected_wait_s"] == float(good)
    for bad in (0, -1, True, "9", None, float("nan"), float("inf"), 10 ** 9):
        assert "expected_wait_s" not in progress.record("result", "k", "r", env={}, expected_wait_s=bad), bad
