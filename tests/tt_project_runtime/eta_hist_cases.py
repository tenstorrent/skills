# ETA numbers from history (the p90, size, load and queue-depth parts of eta.py,
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
    from ttp import eta
    import statistics
    for xs in ([5.0], [1.0, 9.0], [3.0, 1.0, 2.0], [4.0, 1.0, 3.0, 2.0, 10.0, 7.0]):
        assert eta.pct(xs, 0.5) == statistics.median(xs)
    xs = list(range(1, 11))
    assert eta.pct(xs, 0.9) == pytest.approx(9.1) and eta.pct(xs, 1.0) == 10 and eta.pct(xs, 0) == 1
    # Out of range, NaN and non-numbers never raise: clamped or skipped.
    assert eta.pct(xs, 7) == 10 and eta.pct(xs, -1) == 1 and eta.pct(xs, float("nan")) == 1
    assert eta.pct([1.0, float("inf"), float("nan"), True, "x", None, 3.0], 0.5) == 2.0
    assert eta.pct([], 0.9) is None and eta.pct(None, 0.9) is None


def test_eta_size_load_and_depth_factors_are_bounded_and_never_shrink():
    from ttp import eta
    assert [eta.size_of(n) for n in (0, 1499, 1500, 3999, 4000, 99999)] == [
        "short", "short", "medium", "medium", "long", "long"]
    assert eta.size_of("x" * 5000) == "long"
    for bad in (None, -5, float("nan"), True, "", [], {}):
        assert eta.size_of(bad) in ("short", "medium"), bad   # a bad length is medium, text counts its characters
    assert eta.load_factor(0.3) == 1.0 and eta.load_factor(1.0) == 1.0
    assert eta.load_factor(2.0) == pytest.approx(1.25) and eta.load_factor(500) == eta.MAX_LOAD_X
    for bad in (None, float("nan"), True, "9"):
        assert eta.load_factor(bad) == 1.0, bad
    assert eta.depth_factor(0, 4) == 1.0 and eta.depth_factor(4, 4) == 1.0
    assert eta.depth_factor(6, 4) == pytest.approx(1.5) and eta.depth_factor(999, 4) == eta.MAX_DEPTH_X
    assert eta.depth_factor(3, 0) == eta.depth_factor(3, 1) == eta.MAX_DEPTH_X and eta.depth_factor("x", 2) == 1.0
    assert eta.depth_factor(None, None) == 1.0


def test_eta_host_load_reads_the_override_and_never_raises(monkeypatch):
    from ttp import eta, hostload
    monkeypatch.setenv("TTP_ETA_LOAD", "1.75")
    assert eta.host_load() == 1.75
    for bad in ("off", "", "x", "nan", "inf", "-2"):
        monkeypatch.setenv("TTP_ETA_LOAD", bad)
        assert eta.host_load() is None, bad
    monkeypatch.delenv("TTP_ETA_LOAD")
    monkeypatch.setattr(hostload, "loads", lambda: (8.0, 4.0, 2.0))
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    assert eta.host_load() == 2.0
    monkeypatch.setattr(hostload, "loads", lambda: None)
    assert eta.host_load() is None


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
    assert e["end"] == pytest.approx(now + 100 + eta.pct([100] * 5 + [1000] * 6 + [9000] * 2, 0.5))


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


def test_eta_a_wait_on_its_own_checks_ends_at_their_measured_wall_time_not_outside_work(env):
    from ttp import checkwall, eta
    p = make(env)
    now = time.time()
    _eh_runs(p, now, "code", [1200] * 6)
    own = _eta_waiting(p, now, kind="code", retry_when=f"ttp checks --result {p.state}/runs/7", waiting_since=now - 1000)
    gated = eta.estimate(p.db, own, now)
    assert "waits on its own checks" in eta.field(gated, p, now) and "outside work" not in eta.field(gated, p, now)
    for s in (1500, 1800, 2400):
        checkwall.record_run(p.state, s, True)
    e = eta.estimate(p.db, own, now)
    assert e["end"] == pytest.approx(now + 800 + 1200) and [k for k, _ in e["legs"]] == ["own checks", "run"]
    other = _eta_waiting(p, now, kind="code", retry_when="ttp detach --check /runs/7/job", waiting_since=now - 1000)
    assert "waits on outside work" in eta.field(eta.estimate(p.db, other, now), p, now)


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


def test_eta_history_shadow_adds_retries_and_a_code_tasks_review_and_landing(env):
    from ttp import eta, etaledger
    p = make(env)
    now = time.time()
    code = p.db.add_task("c", "s", kind="code", tier="standard")
    work = p.db.add_task("w", "s", kind="work", tier="standard")
    h = {"wait": [100.0], "run": {("code", "standard"): [600.0], ("work", "standard"): [600.0]}, "kind": {},
         "tries": {("code", "standard"): [1, 3]}, "tries_kind": {}, "review": [300.0], "land": [900.0], "batch": [5000.0]}
    # wait + run x mean runs (2) + review + landing; a work task has no review or landing and one run.
    assert etaledger.hist_end(p.db, f"t{code}", now, h) == pytest.approx(now + 100 + 1200 + 300 + 900)
    assert etaledger.hist_end(p.db, f"t{work}", now, h) == pytest.approx(now + 100 + 600)
    # No push-queue landings measured: a push batch's time stands in.
    assert etaledger.hist_end(p.db, f"t{code}", now, {**h, "land": []}) == pytest.approx(now + 100 + 1200 + 300 + 5000)
    # eta.history counts runs per done task by kind and tier.
    _eh_runs(p, now, "work", [600, 600])
    retried = p.db.add_task("retried", "s", kind="work", status="done")
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?, 'worker', ?, ?, 'failed'), "
           "(?, 'worker', ?, ?, 'ok')", (retried, now - 900, now - 800, retried, now - 700, now - 600))
    got = eta.history(p.db, now)
    assert sorted(got["tries_kind"]["work"]) == [1, 1, 2]


def test_eta_shadow_leaves_out_a_pair_delivered_before_its_eta_was_written(env):
    from ttp import etaledger
    p = make(env)
    now = time.time()
    etaledger._append(p.db, [
        {"ts": now - 3000, "ref": "t1", "kind": "work", "src": "state", "likely": now - 2000, "shown": now - 2000,
         "conservative": now - 1000, "hist": now - 2500},
        {"ts": now - 2900, "ref": "t1", "delivered": now - 2100},
        {"ts": now - 3000, "ref": "t2", "kind": "work", "src": "state", "likely": now - 2000, "shown": now - 2000,
         "conservative": now - 1000, "hist": now - 2500},
        {"ts": now - 2900, "ref": "t2", "delivered": now - 3500}])      # landed before its ETA row
    ps = {x["ref"]: x for x in etaledger.pairs(etaledger.read(p.db), now - 86400)}
    assert ps["t2"]["early"] and not ps["t1"]["early"]
    sh = etaledger.shadow(p.db, now - 86400)
    assert sh["count"] == 1 and sh["hist"]["median"] == pytest.approx(400)


def test_eta_row_alarm_flags_only_a_covered_task_with_no_row_and_clears_once_written(env, monkeypatch):
    import types
    from ttp import daemon, eta, etaledger
    p = make(env)
    now = time.time()
    _eh_runs(p, now, "work", [600] * 6)

    def dispatched(title, priority, at):
        tid = p.db.add_task(title, "s", kind="work", priority=priority, status="running")
        p.db.x("UPDATE tasks SET created=? WHERE id=?", (at - 10, tid))
        p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?, 'worker', ?, 'running')", (tid, at))
        return tid
    hot = dispatched("urgent", 1, now - 60)
    dispatched("routine", 3, now - 60)                         # not urgent: the rule gives it no task row
    dispatched("urgent long ago", 1, now - 3 * 3600)           # dispatched before the window
    # No coordinator wake since the dispatch: the writer has not had its chance, so no alarm yet.
    assert etaledger.missing_rows(p.db, p, now) == []
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(NULL, 'coordinator', ?, 'ok')", (now - 30,))
    assert [t["id"] for t in etaledger.missing_rows(p.db, p, now)] == [hot]
    seen = []
    fake = types.SimpleNamespace(p=p, observe=lambda *a, **k: seen.append((a, k)))

    def broken(*a):
        raise RuntimeError("writer down")
    monkeypatch.setattr(etaledger, "write_rows", broken)   # a failed model-free writer still lets the alarm run
    daemon.Daemon.check_eta_rows(fake)
    assert len(seen) == 1 and seen[0][0][0] == "daemon:eta-rows" and f"#{hot}" in seen[0][0][1]
    assert seen[0][0][2] == "normal" and seen[0][1]["key"] == "eta-rows-missing"
    p.set_config("eta.row_alarm_h", 0)
    assert etaledger.missing_rows(p.db, p, now) == []
    p.set_config("eta.row_alarm_h", 2)
    # The writer records it at the next digest; the alarm clears.
    eta.state_lines(p.db, p, now)
    assert any(r.get("ref") == f"t{hot}" for r in etaledger.read(p.db))
    assert etaledger.missing_rows(p.db, p, now) == []


def test_eta_row_alarm_follows_the_writer_when_the_corrected_estimate_disagrees(env, monkeypatch):
    from ttp import eta, etaledger
    p = make(env)
    now = time.time()
    _eh_runs(p, now, "work", [600] * 6)
    tid = p.db.add_task("urgent", "s", kind="work", priority=1, status="running")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 70, tid))
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?, 'worker', ?, 'running')", (tid, now - 60))
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(NULL, 'coordinator', ?, 'ok')", (now - 30,))
    assert [t["id"] for t in etaledger.missing_rows(p.db, p, now)] == [tid]
    # The correction the writer records through gives no end: no row is due, so the alarm stays quiet.
    real = etaledger.shown
    monkeypatch.setattr(etaledger, "shown", lambda db, e, n, kind: {**real(db, e, n, kind), "why": "waits on outside work"})
    assert etaledger.missing_rows(p.db, p, now) == []
    eta.state_lines(p.db, p, now)
    assert not any(r.get("ref") == f"t{tid}" for r in etaledger.read(p.db))


def test_eta_rows_written_model_free_between_wakes_for_covered_tasks_only(env, monkeypatch):
    import types
    from ttp import daemon, etaledger
    p = make(env)
    now = time.time()
    _eh_runs(p, now, "work", [600] * 6)

    def running(title, priority):
        tid = p.db.add_task(title, "s", kind="work", priority=priority, status="running")
        p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 70, tid))
        p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?, 'worker', ?, 'running')", (tid, now - 60))
        return tid
    hot, cold = running("urgent", 1), running("routine", 3)
    seen = []
    fake = types.SimpleNamespace(p=p, observe=lambda *a, **k: seen.append((a, k)))
    # No coordinator wake at all: the daemon's own pass writes the row, the same one STATE would.
    daemon.Daemon.check_eta_rows(fake)
    rows = [r for r in etaledger.read(p.db) if r.get("ref", "").startswith("t")]
    assert [(r["ref"], r["src"]) for r in rows] == [(f"t{hot}", "daemon")]
    assert rows[0]["shown"] > now and seen == []
    # Only the first row: later passes add none while it stands (moves are STATE's to record at a wake).
    fake._eta_rows_checked = 0.0
    daemon.Daemon.check_eta_rows(fake)
    assert len([r for r in etaledger.read(p.db) if r.get("ref") == f"t{hot}"]) == 1
    assert not any(r.get("ref") == f"t{cold}" for r in etaledger.read(p.db))
    # An urgent task whose estimate has no end (waits on outside work) still gets no row.
    later = running("urgent outside", 1)
    real = etaledger.shown
    monkeypatch.setattr(etaledger, "shown", lambda db, e, n, kind: {**real(db, e, n, kind), "why": "waits on outside work"})
    assert etaledger.write_rows(p.db, p, now) == 0
    assert not any(r.get("ref") == f"t{later}" for r in etaledger.read(p.db))
    p.set_config("eta.ledger", False)
    monkeypatch.setattr(etaledger, "shown", real)
    assert etaledger.write_rows(p.db, p, now) == 0


def test_eta_history_shadow_gives_a_promise_its_latest_owners_end(env):
    from ttp import etaledger, obligations
    p = make(env)
    now = time.time()
    h = {"wait": [100.0], "run": {("code", "standard"): [600.0], ("work", "standard"): [600.0]}, "kind": {},
         "tries": {}, "tries_kind": {}, "review": [300.0], "land": [900.0], "batch": []}
    code = p.db.add_task("c", "s", kind="code", tier="standard")
    work = p.db.add_task("w", "s", kind="work", tier="standard")
    done = p.db.add_task("d", "s", kind="work", tier="standard")
    oid = obligations.add(p, "owed", owners=[code, work, done])
    only_done = obligations.add(p, "owed too", owners=[done])
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (done,))
    assert etaledger.hist_end(p.db, oid, now, h) == pytest.approx(now + 100 + 600 + 300 + 900)
    assert etaledger.hist_end(p.db, only_done, now, h) == pytest.approx(now)   # a closed owner counts now
    assert etaledger.hist_end(p.db, obligations.add(p, "no owner"), now, h) is None


def test_eta_history_shadow_reads_a_promise_owned_through_promise_tasks_and_skips_dead_owners(env):
    from ttp import etaledger, obligations
    p = make(env)
    now = time.time()
    h = {"wait": [100.0], "run": {("work", "standard"): [600.0]}, "kind": {}, "tries": {}, "tries_kind": {},
         "review": [], "land": [], "batch": []}
    live = p.db.add_task("l", "s", kind="work", tier="standard")
    dead = p.db.add_task("x", "s", kind="work", tier="standard")
    oid = obligations.add(p, "promised in a reply")
    st = obligations._load(p.db)
    st["items"][oid]["promise"] = {"chat": "c", "message": 1, "due": now + 3600, "timed": True, "tasks": [live, dead]}
    p.db.set_kv(obligations.KV, st)
    p.db.update_task(dead, status="cancelled")
    assert not obligations.get(p.db, oid)["owners"]
    assert etaledger.hist_end(p.db, oid, now, h) == pytest.approx(now + 100 + 600)   # the cancelled owner adds none


def test_eta_calibration_and_shadow_split_quoted_dues_from_computed_etas_and_judge_promotion(env):
    from ttp import etaledger
    p = make(env)
    now = time.time()
    rows = []
    for i in range(10):     # ten code tasks: history closer than the current ETA
        rows += [{"ts": now - 9000, "ref": f"t{i}", "kind": "code", "src": "state", "likely": now - 7000,
                  "shown": now - 7000, "conservative": now - 6000, "hist": now - 7900},
                 {"ts": now - 8900, "ref": f"t{i}", "delivered": now - 8000}]
    # A promise backfilled with its own due, then computed: the computed row is its pair.
    rows += [{"ts": now - 9000, "ref": "o1", "kind": "work", "src": "backfill", "likely": now - 8800,
              "shown": now - 8800, "due": now - 8800},
             {"ts": now - 8990, "ref": "o1", "kind": "work", "src": "state", "likely": now - 7990,
              "shown": now - 7990, "hist": now - 7990},
             {"ts": now - 9000, "ref": "o2", "kind": "work", "src": "backfill", "likely": now - 8800,
              "shown": now - 8800, "due": now - 8800},
             {"ts": now - 8900, "ref": "o1", "delivered": now - 7990},
             {"ts": now - 8900, "ref": "o2", "delivered": now - 8000}]
    etaledger._append(p.db, rows)
    ps = {x["ref"]: x for x in etaledger.pairs(etaledger.read(p.db), now - 86400)}
    assert not ps["o1"]["quoted"] and ps["o1"]["promised"] == pytest.approx(1000) and ps["o2"]["quoted"]
    cal = etaledger.calibration(p.db, now)
    assert (cal["count_computed"], cal["count_quoted"]) == (11, 1)
    assert cal["median_ratio_quoted"] == pytest.approx(5.0)
    assert "; computed 0.50 (n=11), quoted dues 5.00 (n=1)" in etaledger.line(cal)
    sh = etaledger.shadow(p.db, now - 86400)
    assert sh["by_kind"]["code"]["count"] == 10 and sh["by_kind"]["promise"]["count"] == 1
    assert sh["promotable"]            # the promise has under ten pairs: not judged
    assert "; code 17m/17m vs 2m/2m (n=10)" in etaledger.shadow_line(sh) and etaledger.shadow_line(sh).endswith(
        "promotable: yes")
    etaledger._append(p.db, [x for i in range(10, 20) for x in (
        {"ts": now - 9000, "ref": f"t{i}", "kind": "code", "src": "state", "likely": now - 8000,
         "shown": now - 8000, "hist": now - 3000}, {"ts": now - 8900, "ref": f"t{i}", "delivered": now - 8000})])
    assert not etaledger.shadow(p.db, now - 86400)["promotable"]     # history now worse at the p90
