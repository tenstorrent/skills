# The landing time breakdown (runtime/ttp/landings.py: `ttp landings`, the open tasks' blocking step, the daily
# review lines) and the ETA ledger with calibration (runtime/ttp/etaledger.py: `ttp eta --calibration`) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations


def _landed_code(p, now, created=-7200, runs=((-6600, -6000),), pushed=-1000, title="landed change"):
    """A done code task created at `now+created` whose runs are (start, end) offsets and whose commit was pushed at `now+pushed`."""
    tid = p.db.add_task(title, "s", kind="code", status="done")
    p.db.x("UPDATE tasks SET created=?, updated=? WHERE id=?", (now + created, now + pushed, tid))
    for a, b in runs:
        p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?, 'worker', ?, ?, 'ok')",
               (tid, now + a, now + b))
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,pushed_sha) VALUES(?,?,?,?,?,?,?,?)",
           (tid, "b", "h", "t", "pushed", now + pushed - 300, now + pushed, "abc1234"))
    return tid


def test_landings_split_the_wall_time_into_queue_run_and_gaps_that_add_up(env):
    from ttp import landings
    p = make(env)
    now = time.time()
    tid = _landed_code(p, now, created=-7200, runs=((-6600, -6000), (-5000, -4400)), pushed=-1000)
    rows = landings.landed(p.db, now - 86400, now)
    assert [r["task"] for r in rows] == [tid]
    b = rows[0]
    assert b["total_s"] == pytest.approx(6200)
    assert b["queue_first_s"] == pytest.approx(600)          # created to the first run
    assert b["run_s"] == pytest.approx(1200) and b["runs"] == 2
    assert sum(b["causes"].values()) == pytest.approx(b["total_s"])   # every second has one cause
    assert b["causes"]["run"] == pytest.approx(1200)
    assert b["gap_s"] == pytest.approx(b["total_s"] - 1200 - 600)
    sm = landings.summary(rows)
    assert sm["count"] == 1 and sm["median_s"] == pytest.approx(6200) and sm["runs_per_task"]["mean"] == 2
    assert sm["slowest"][0]["task"] == tid
    assert "landed" in landings.text(sm, 1) or "1" in landings.text(sm, 1)


def test_landings_name_the_cause_of_a_gap_between_runs(env):
    from ttp import landings
    p = make(env)
    now = time.time()
    tid = _landed_code(p, now, created=-7200, runs=((-7000, -6500), (-3000, -2500)), pushed=-1000)
    # Between the runs the task waited on a job it owns, said by its own event.
    p.db.x("INSERT INTO events(ts, source, kind, text, task, data) VALUES(?,?,?,?,?,?)",
           (now - 6400, "daemon", "task_waiting", "waiting", tid, '{"wait": "self", "why": "detached jobs"}'))
    p.db.x("INSERT INTO events(ts, source, kind, text, task, data) VALUES(?,?,?,?,?,?)",
           (now - 3100, "daemon", "task_queued", "queued", tid, "{}"))
    b = landings.landed(p.db, now - 86400, now)[0]
    assert sum(b["causes"].values()) == pytest.approx(b["total_s"])
    assert b["causes"].get("own job", 0) > 2000
    assert landings.dominant(b) == "own job"


def test_landings_open_task_shows_its_current_step_and_how_long(env):
    from ttp import landings
    p = make(env)
    now = time.time()
    tid = p.db.add_task("waiting change", "s", kind="code", status="queued")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 1800, tid))
    got = landings.blocking(p.db, None, now)
    assert got[tid]["step"] == "queue" and got[tid]["waited_s"] == pytest.approx(1800, abs=5)
    assert landings.blocking_words(got[tid]).startswith("in queue for ")
    p.db.update_task(tid, status="running")
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?, 'worker', ?, 'running')", (tid, now - 300))
    got = landings.blocking(p.db, None, now)
    assert got[tid]["step"] == "run" and landings.blocking_words(got[tid]).startswith("running for ")


def test_landings_audit_lines_name_the_dominant_cause_and_queue_one_fix_only(env):
    from ttp import landings
    p = make(env)
    now = time.time()
    _landed_code(p, now, created=-7200, runs=((-7000, -6500),), pushed=-600, title="slow landing")
    out = landings.audit_lines(p.db, now)
    assert any(x.startswith("slowest landings, 24 h") for x in out)
    top = [x for x in out if x.startswith("top recurring landing delay")]
    assert top and "queue one fix task titled 'landing time: " in top[0]
    cause = top[0].split("landing delay, 7 d: ")[1].split(" ")[0:1]
    # Once a fix task for the cause is open, the line says so and asks for no duplicate.
    for c in landings.CAUSES:
        p.db.add_task(landings.FIX_TITLE + c, "s", kind="code")
    out = landings.audit_lines(p.db, now)
    assert not any("queue one fix task" in x for x in out)
    assert cause


def test_landings_push_batch_deaths_are_counted_by_cause(env):
    from ttp import landings
    p = make(env)
    now = time.time()
    for text in ("daemon restarted after a reboot", "worker was killed", "host reboot", "ended before writing an outcome (a reboot, a kill or a crash)"):
        p.db.x("INSERT INTO events(ts, source, kind, text) VALUES(?,?,?,?)", (now - 100, "push", "push_batch_died", text))
    got = landings.push_batch_deaths(p.db, now - 86400)
    assert got["count"] == 4 and got["causes"] == {"reboot": 2, "kill": 1, "no outcome written": 1}
    assert landings.push_batch_deaths(p.db, now)["count"] == 0


def test_eta_ledger_records_once_settles_on_landing_and_calibrates(env):
    from ttp import etaledger
    p = make(env)
    now = time.time()
    tid = _landed_code(p, now, created=-7200, runs=((-7000, -6500),), pushed=-600)
    e = {"end": now - 7000 + 1000, "path": [tid]}
    assert etaledger.record(p.db, p, [(f"t{tid}", "code", e)], now - 7000) == 1
    assert etaledger.record(p.db, p, [(f"t{tid}", "code", e)], now - 6990) == 0      # repeated within the hour
    assert etaledger.settle(p.db, now) == 1
    assert etaledger.settle(p.db, now) == 0                                          # once
    cal = etaledger.calibration(p.db, now, {})
    assert cal["count"] == 1 and cal["median_ratio"] == pytest.approx(6400 / 1000, rel=0.05)
    assert cal["share_by_conservative"] == 0
    assert "1 delivered" in etaledger.line(cal) and "target 80%" in etaledger.line(cal)
    assert "no delivered ETA yet" in etaledger.line(etaledger.calibration(p.db, now, {}, days=0.0001))


def test_eta_ledger_is_append_only_and_off_switch_records_nothing(env):
    from ttp import etaledger
    p = make(env)
    now = time.time()
    e = {"end": now + 3600, "path": []}
    assert etaledger.record(p.db, p, [("t1", "code", e)], now) == 1
    before = etaledger._path(p.db).read_text()
    etaledger.record(p.db, p, [("t2", "code", e)], now)
    assert etaledger._path(p.db).read_text().startswith(before)
    assert etaledger.record(p.db, {"eta": {"ledger": False}}, [("t3", "code", e)], now) == 0


def test_eta_ledger_backfills_promises_from_obligation_records_once(env):
    from ttp import etaledger
    p = make(env)
    now = time.time()
    from ttp import obligations
    p.db.set_kv(obligations.KV, {"next": 2, "runs": {}, "items": {"o1": {
        "id": "o1", "text": "deliver", "created": now - 7200, "owners": {},
        "promise": {"due": now - 3600, "set_at": now - 7200}, "receipt": {"at": now - 4000}}}})
    assert etaledger.backfill(p.db, now) == 2
    assert etaledger.backfill(p.db, now) == 0
    pr = etaledger.pairs(etaledger.read(p.db), now - 86400 * 2)
    assert pr and pr[0]["actual"] == pytest.approx(3200) and pr[0]["promised"] == pytest.approx(3600)


def test_eta_calibration_keeps_default_dues_out_of_the_quoted_ratio_and_the_headline_on_a_line_of_their_own(env):
    # A due nobody named (the default due of an untimed order) is no quoted ETA: it is counted apart.
    from ttp import etaledger, obligations
    p = make(env)
    now = time.time()
    p.db.set_kv(obligations.KV, {"next": 3, "runs": {}, "items": {
        "o1": {"id": "o1", "text": "named due", "created": now - 7200, "owners": {},
               "promise": {"due": now - 3600, "set_at": now - 7200, "timed": True}, "receipt": {"at": now - 4000}},
        "o2": {"id": "o2", "text": "default due", "created": now - 7200, "owners": {},
               "promise": {"due": now - 3600, "set_at": now - 7200, "timed": False}, "receipt": {"at": now - 7000}}}})
    assert etaledger.backfill(p.db, now) == 4
    cal = etaledger.calibration(p.db, now, {})
    assert cal["count"] == 1 and cal["count_quoted"] == 1
    assert cal["median_ratio"] == pytest.approx(3200 / 3600) == cal["median_ratio_quoted"]
    assert cal["count_default"] == 1 and cal["median_ratio_default"] == pytest.approx(200 / 3600)
    ln = etaledger.line(cal).splitlines()
    assert "quoted dues 0.89 (n=1)" in ln[0] and "0.06" not in ln[0]
    assert ln[1] == "ETA default dues (no time named; not in the ratio above): 0.06 (n=1)"


def test_eta_factor_is_bounded_needs_samples_and_has_an_off_switch(env):
    from ttp import eta, etaledger
    p = make(env)
    now = time.time()
    rows = []
    for i in range(6):   # six code deliveries that took 10x the raw ETA
        rows.append({"ts": now - 5000 - i, "ref": f"t{100 + i}", "kind": "code", "src": "state", "likely": now - 5000 - i + 100,
                     "shown": now - 5000 - i + 100, "conservative": now - 5000 - i + 200})
        rows.append({"ts": now - 10, "ref": f"t{100 + i}", "delivered": now - 5000 - i + 1000})
    etaledger._append(p.db, rows)
    f = etaledger.factors(p.db, p, now)
    assert f["factors"]["code"] == 3.0                    # clamped to the upper bound
    p.db.set_kv("eta_factors", f)
    e = {"end": now + 1000, "path": []}
    got = etaledger.shown(p.db, e, now, "code")
    assert got["end"] == pytest.approx(now + 3000) and got["raw_end"] == e["end"] and got["factor"] == 3.0
    assert got["conservative"] > got["end"]
    # A long outside-work leg whose p90 walk ends before the x3 likely time: neither conservative time
    # is earlier than the likely one, in `ttp eta`, its --json, STATE's line or the ledger row.
    leg = {"end": now + 10.4 * 3600, "p90_end": now + 18.8 * 3600, "why": None, "overrun": False,
           "path": [7], "legs": [("outside work", 10.35 * 3600)]}
    got = etaledger.shown(p.db, leg, now, "code")
    assert got["end"] == pytest.approx(now + 31.2 * 3600) and got["raw_p90_end"] == leg["p90_end"]
    assert got["conservative"] >= got["end"] and got["p90_end"] >= got["end"]
    assert "conservative" in eta.field(got, p, now) and "corrected x3" in eta.text(got, p, now)
    etaledger.record(p.db, p, [("t7", "code", got), ("t8", "code", {**got, "conservative": now})], now, src="cli")
    rows = [r for r in etaledger.read(p.db) if r.get("ref") in ("t7", "t8")]
    assert len(rows) == 2 and all(r["conservative"] >= r["shown"] for r in rows), rows
    assert etaledger.shown(p.db, e, now, "work")["end"] == e["end"]                  # no samples: untouched
    off = etaledger.factors(p.db, {"eta": {"correction": False}}, now)
    p.db.set_kv("eta_factors", off)
    assert etaledger.factor(p.db, "code") == 1.0 and eta.corrected(p.db, e, now)["end"] == e["end"]
    few = etaledger.factors(p.db, {"eta": {"min_samples": 50}}, now)
    assert few["factors"] == {}


def test_eta_cli_calibration_and_landings_commands_run(env, capsys):
    from ttp import cli
    p = make(env)
    now = time.time()
    _landed_code(p, now)
    for argv in (["eta", p.name, "--calibration"], ["landings", p.name, "--days", "7", "--json"],
                 ["landings", p.name, "--top", "3"]):
        cli.main(argv)
    out = capsys.readouterr().out
    assert "ETA calibration" in out and '"count"' in out
