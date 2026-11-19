# Release-ring tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new release-ring tests here, not there.
from __future__ import annotations


def _roll(capsys, *argv):
    from ttp import cli
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["rollout", *argv])
    out = capsys.readouterr().out
    return e.value.code, out


def test_a_flag_is_off_until_its_ring_is_reached_and_a_projects_own_setting_wins(env, capsys):
    from ttp import rollout
    for who, ring in (("scratch1", "0"), ("canary", "1")):
        assert _roll(capsys, "ring", who, ring)[0] == 0
    assert _roll(capsys, "ring", "excl", "exclude")[0] == 0
    names = ("scratch1", "canary", "everyone", "excl")
    assert not any(rollout.on("deslop", n) for n in names), "flags are off by default"
    for lv, want in (("0", {"scratch1"}), ("1", {"scratch1", "canary"}), ("2", {"scratch1", "canary", "everyone"})):
        assert _roll(capsys, "set", "deslop", lv)[0] == 0
        assert {n for n in names if rollout.on("deslop", n)} == want, "an excluded project never takes a flag"
    assert _roll(capsys, "set", "deslop", "off")[0] == 0
    own = {"deslop": {"enabled": False}}
    assert rollout.on("deslop", "excl", own) and rollout.on("deslop", "everyone", own), "its own value wins"
    assert not rollout.on("deslop", "everyone", {"deslop": {}})
    assert not rollout.on("no_such_flag", "scratch1")


def test_ring_moves_and_exclusion_are_listed_once_and_undone(env, capsys):
    from ttp import rollout
    for r in ("0", "1", "exclude", "include", "2"):
        assert _roll(capsys, "ring", "alpha", r)[0] == 0
    assert rollout.ring_of("alpha") == 2 and rollout.settings().get("rings", {}) == {}
    _roll(capsys, "ring", "alpha", "1")
    _roll(capsys, "ring", "alpha", "0")
    assert rollout.settings()["rings"] == {"0": ["alpha"]} and rollout.ring_of("alpha") == 0
    code, out = _roll(capsys, "ring", "alpha", "9")
    assert code == 2 and "ring is" in out
    assert _roll(capsys, "ring", "alpha")[0] == 2


def test_promote_walks_off_0_1_2_and_every_change_is_ledgered(env, capsys):
    from ttp import rollout
    for want in (0, 1, 2):
        assert _roll(capsys, "promote", "glm_code", "--reason", "clean")[0] == 0
        assert rollout.level("glm_code") == want
    assert "every ring already" in _roll(capsys, "promote", "glm_code")[1]
    recs = rollout.ledger("glm_code")
    assert [(r["kind"], r["from"], r["to"]) for r in recs] == [("promote", "off", 0), ("promote", 0, 1), ("promote", 1, 2)]
    assert all(r["by"] == "user" and r["ts"] for r in recs) and recs[0]["reason"] == "clean"
    assert _roll(capsys, "set", "glm_code", "2")[1].endswith("already; nothing changed\n")
    assert len(rollout.ledger("glm_code")) == 3, "a no-op writes nothing"
    code, out = _roll(capsys, "set", "glm_code", "7")
    assert code == 2 and "off, 0, 1, 2 or auto" in out
    code, out = _roll(capsys, "promote", "nope")
    assert code == 2 and "unknown flag" in out


def test_rollback_turns_the_flag_off_and_freezes_it_until_armed(env, capsys):
    from ttp import rollout
    _roll(capsys, "set", "eta_history", "1")
    assert "frozen" in _roll(capsys, "rollback", "eta_history", "--reason", "failed runs up")[1]
    assert rollout.level("eta_history") == rollout.OFF and rollout.frozen("eta_history")["reason"] == "failed runs up"
    code, out = _roll(capsys, "set", "eta_history", "0")
    assert code == 2 and "frozen" in out and rollout.level("eta_history") == rollout.OFF
    assert _roll(capsys, "promote", "eta_history")[0] == 2
    assert "re-armed" in _roll(capsys, "arm", "eta_history")[1]
    assert rollout.frozen("eta_history") is None
    assert "not frozen" in _roll(capsys, "arm", "eta_history")[1]
    assert _roll(capsys, "promote", "eta_history")[0] == 0 and rollout.level("eta_history") == 0
    assert [r["kind"] for r in rollout.ledger("eta_history")] == ["set", "rollback", "arm", "promote"]


def test_a_run_may_read_or_roll_back_but_never_widen_the_rollout(env, capsys, monkeypatch):
    from ttp import rollout
    _roll(capsys, "set", "deslop", "1")
    monkeypatch.setenv("TTP_RUN_DIR", "/nonexistent")
    for argv in (("ring", "x", "0"), ("set", "deslop", "2"), ("promote", "deslop"), ("arm", "deslop")):
        code, out = _roll(capsys, *argv)
        assert code == 2 and "only the user's terminal" in out
    assert rollout.level("deslop") == 1 and rollout.settings().get("rings") is None
    assert _roll(capsys, "status")[0] == 0
    assert _roll(capsys, "rollback", "deslop", "--reason", "bad")[0] == 0
    assert rollout.level("deslop") == rollout.OFF and rollout.ledger("deslop")[-1]["by"] == "run"


def test_rollout_state_is_account_wide_and_touches_no_project_file(env, capsys):
    from ttp import project, rollout
    p = make(env)
    before = {f: f.read_bytes() for f in p.base.rglob("*") if f.is_file() and f.suffix in (".json", ".md")}
    _roll(capsys, "ring", "someone", "1")
    _roll(capsys, "set", "eta_history", "1")
    assert {f: f.read_bytes() for f in before} == before
    data = json.loads(project.account_settings_path().read_text())
    assert data["rollout"] == {"flags": {"eta_history": 1}, "rings": {"1": ["someone"]}}
    assert oct(project.account_settings_path().stat().st_mode & 0o777) == "0o600"
    assert (project.HOME_DIR / rollout.LEDGER).exists()
    st = json.loads(_roll(capsys, "status", "--json")[1])
    assert st["flags"]["eta_history"]["level"] == 1 and st["rings"] == {"1": ["someone"]}
    for key in ("flags", "rings", "excluded", "thresholds", "scratch_cap_usd"):
        assert f"rollout.{key}" in project.ACCOUNT_USER_ONLY and key in project.ACCOUNT_KEYS["rollout"]


_RH = 3600.0


def _ro_flag(flag, to, ago_h, kind="set"):
    """The flag set to `to` `ago_h` hours back: its account value and a ledger record with that time."""
    from ttp import project, rollout
    rollout._put("flags", flag, to)
    rec = {"ts": time.time() - ago_h * _RH, "kind": kind, "flag": flag, "from": "off", "to": to, "reason": "t", "by": "user",
           "snapshot": {}}
    project.durable_append(project.HOME_DIR / rollout.LEDGER, json.dumps(rec) + "\n")


def _ro_runs(p, n, failed, ago_h, status_fail="failed"):
    for i in range(n):
        p.db.x("INSERT INTO runs(task,role,status,started,ended) VALUES(1,'worker',?,?,?)",
               (status_fail if i < failed else "done", time.time() - ago_h * _RH - 5, time.time() - ago_h * _RH))


def _ro_landed(p, took_h, cost, ago_h, tier="standard"):
    tid = p.db.add_task("t", "s", kind="code", tier=tier)
    land = time.time() - ago_h * _RH
    p.db.x("UPDATE tasks SET created=?, spent_usd=? WHERE id=?", (land - took_h * _RH, cost, tid))
    p.db.x("INSERT INTO task_landings(marker, task, sha, target, ts) VALUES(?,?,?,?,?)", (f"m{tid}", tid, "a" * 40, "main", land))


def test_health_signals_fail_only_past_their_threshold_and_sample_minimum(env):
    from ttp import rollout_health as rh
    p = make(env)
    th, now = rh.thresholds({"thresholds": {"min_runs": 20, "min_landings": 3}}), time.time() + 60
    since = now - 24 * _RH
    _ro_runs(p, 40, 4, 100)                # baseline: 10% failed
    _ro_runs(p, 19, 19, 5)                 # too few runs to judge
    assert rh.signals(p.db, since, now, th)["failing"] == [] and not rh.signals(p.db, since, now, th)["enough"]
    _ro_runs(p, 1, 1, 5, "lost")           # 20 runs, all failed
    _ro_runs(p, 30, 30, 5, "auth")         # auth failures are not counted
    v = rh.signals(p.db, since, now, th)
    assert v["runs"] == 20 and any(s.startswith("failed runs 100% against 10%") for s in v["failing"])
    _ro_landed(p, 1, 2.0, 100)
    _ro_landed(p, 1, 2.0, 101)
    for _ in range(2):
        _ro_landed(p, 3, 9.0, 3)
    assert not [s for s in rh.signals(p.db, since, now, th)["failing"] if "landing" in s], "two landings are too few"
    _ro_landed(p, 3, 9.0, 2)
    failing = rh.signals(p.db, since, now, th)["failing"]
    assert any("median time to land 3.0 h against 1.0 h" in s for s in failing)
    assert any("cost per landing $9.00 against $2.00" in s for s in failing)
    assert rh.thresholds({"thresholds": {"cost_x": 5, "bogus": 1, "stuck_h": "x"}})["cost_x"] == 5.0
    assert "bogus" not in rh.thresholds({"thresholds": {"bogus": 1}})


def _ro_kind_runs(p, kind, n, failed, ago_h):
    tid = p.db.add_task("r", "s", kind=kind)
    for i in range(n):
        p.db.x("INSERT INTO runs(task,role,status,started,ended) VALUES(?,'worker',?,?,?)",
               (tid, "failed" if i < failed else "done", time.time() - ago_h * _RH - 5, time.time() - ago_h * _RH))


def test_a_cost_or_rate_check_needs_20_landings_by_default_and_the_sample_is_configurable(env):
    # cells: min_sample: the 2026-11-18 false trip (cost per landing $5.46 against $3.65 on 6 landings)
    from ttp import rollout_health as rh
    p = make(env)
    now = time.time() + 60
    since = now - 24 * _RH
    for i in range(10):
        _ro_landed(p, 1, 3.65, 100 + i)
    for i in range(6):
        _ro_landed(p, 5, 5.46, 3 + i)
    assert rh.DEFAULTS["min_landings"] == 20
    v = rh.signals(p.db, since, now, rh.thresholds(None), "eta", "deslop")
    assert v["failing"] == [] and v["matched_landings"] == 6 and not v["enough"], "6 landings are too few to judge"
    got = rh.signals(p.db, since, now, rh.thresholds({"thresholds": {"min_landings": 6}}), "eta", "deslop")["failing"]
    assert any(s.startswith("cost per landing $5.46 against $3.65 for the same task kinds (6 landings") for s in got)


def test_cost_time_and_failure_rate_compare_the_same_task_kinds_never_a_raw_average(env):
    # cells: like_for_like
    from ttp import rollout_health as rh
    p = make(env)
    now = time.time() + 60
    since, th = now - 24 * _RH, rh.thresholds(None)
    for i in range(20):                      # baseline: cheap light work and dearer standard work, raw $3.00
        _ro_landed(p, 0.5, 1.0, 100 + i, "light")
        _ro_landed(p, 2, 5.0, 100 + i, "standard")
    for i in range(22):                      # window: only standard work, each at its own kind's baseline
        _ro_landed(p, 2, 5.0, 2 + i / 10, "standard")
    for i in range(3):                       # a kind the baseline lacks is left out of the sample
        _ro_landed(p, 9, 40.0, 1, "deep")
    _ro_kind_runs(p, "review", 40, 12, 100)  # baseline: reviews fail 30%, code 0%: raw 15%
    _ro_kind_runs(p, "code", 40, 0, 100)
    _ro_kind_runs(p, "review", 30, 9, 2)     # window: only reviews, still 30%
    v = rh.signals(p.db, since, now, th, "review", "deslop")
    assert v["failing"] == [], v["failing"]
    assert v["landings"] == 25 and v["matched_landings"] == 22 and set(v["kinds"]) == {"code/standard"}
    assert v["matched_runs"] == 30 and v["enough"]
    _ro_kind_runs(p, "review", 10, 10, 1)    # now 19 of 40 reviews failed: 47.5% against their own 30%
    got = rh.signals(p.db, since, now, th, "review", "deslop")["failing"]
    assert got == ["failed runs 48% against 30% for the same task kinds (40 runs)"], got


def test_a_shadow_flag_is_exempt_from_every_health_judgment_and_the_daemon_never_rolls_it_back(env, tmp_path):
    # cells: shadow_exempt
    from ttp import rollout, rollout_health as rh
    p, d = _watch(env)
    rollout.set_ring(p.name, "1")
    _ro_flag("eta_history", 1, 30)
    for i in range(25):
        _ro_landed(p, 1, 1.0, 100 + i)
        _ro_landed(p, 9, 20.0, 1 + i / 10)
    _ro_runs(p, 40, 4, 100)
    _ro_runs(p, 30, 30, 1)
    repo, spec = _ro_repo(tmp_path, "etaledger.py")
    d.p.root = type(p.root)(repo)
    _ro_revert(p, spec)
    stuck = p.db.add_task("s", "s", kind="code", status="running")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - 5 * _RH, stuck))
    p.db.x("INSERT INTO alerts(key,severity,text,raised) VALUES('after_push_failed','high','x',?)", (time.time() - _RH,))
    v = rh.verdict(p.db, "eta_history", 1, rollout.ledger(), {}, time.time() + 60, repo)
    assert v["failing"] == [] and v["shadow"]
    exempt = " | ".join(v["exempt"])
    for what in ("failed runs", "cost per landing", "median time to land", "forward revert", "no run and no change"):
        assert what in exempt, what
    d.rollout_watch()                        # a clean vote may promote it; it is never rolled back
    assert rollout.frozen("eta_history") is None and rollout.level("eta_history") in (1, 2)
    assert p.db.one("SELECT 1 FROM messages WHERE ref='rollout_rollback:eta_history'") is None, "no alert either"
    saved = dict(rollout.FLAGS["eta_history"])
    try:
        rollout.FLAGS["eta_history"].pop("shadow")
        assert rh.verdict(p.db, "eta_history", 1, rollout.ledger(), {}, time.time() + 60, repo)["failing"]
    finally:
        rollout.FLAGS["eta_history"].clear()
        rollout.FLAGS["eta_history"].update(saved)
    assert "-> off" in rollout.rollback("eta_history", "by hand", "user") and rollout.level("eta_history") == rollout.OFF
    assert rollout.status()["flags"]["eta_history"]["shadow"] and not rollout.status()["flags"]["deslop"]["shadow"]


def test_a_real_per_kind_regression_over_20_landings_still_rolls_the_flag_back_and_says_why(env):
    # cells: real_trip
    from ttp import rollout
    p, d = _watch(env)
    rollout.set_ring(p.name, "1")
    _ro_flag("deslop", 1, 30)
    for i in range(20):
        _ro_landed(p, 1, 2.0, 100 + i)
        _ro_landed(p, 1, 4.0, 1 + i / 10)
    assert d.rollout_watch() == ["deslop"] and rollout.level("deslop") == rollout.OFF
    why = rollout.frozen("deslop")["reason"]
    assert "cost per landing $4.00 against $2.00 for the same task kinds (20 landings: code/standard 20 at $4.00 " \
           "against $2.00)" in why, why
    note = p.db.one("SELECT text FROM messages WHERE ref='rollout_rollback:deslop'")["text"]
    assert "cost per landing $4.00" in note and "20 landings" in note


def test_health_flags_a_new_high_alert_in_the_flags_area_a_forward_revert_and_a_stuck_task(env):
    from ttp import postland, rollout, rollout_health as rh
    p = make(env)
    th, now = rh.thresholds(None), time.time() + 60
    since = now - 24 * _RH
    area = "push"   # the push queue's area (push_isolation graduated: it runs for every project)
    ins = "INSERT INTO alerts(key, raised, severity, text) VALUES(?,?,?,'x')"
    p.db.x(ins, ("push_not_landing", now - 100 * _RH, "high"))     # seen in the baseline: not new
    p.db.x(ins, ("push_not_landing", now - 5 * _RH, "high"))
    p.db.x(ins, ("push_queue_dying", now - 5 * _RH, "low"))        # not high
    p.db.x(ins, ("some_other_key", now - 5 * _RH, "high"))         # in no area
    assert rh.signals(p.db, since, now, th, area)["failing"] == []
    p.db.x(ins, ("after_push_failed", now - 4 * _RH, "high"))
    assert rh.signals(p.db, since, now, th, area)["failing"] == ["new alert after_push_failed"]
    rid = p.db.add_task("Redo #7 alone: x", "s", kind="code", origin="daemon")   # reverted before landing: routine
    assert not any("revert" in s for s in rh.signals(p.db, since, now, th, area)["failing"])
    p.db.x("DELETE FROM tasks WHERE id=?", (rid,))
    p.db.add_task(f"Revert #7 ttp/t7-x{postland.REVERT_TITLE_TAIL}", "Forward revert of aaa..bbb by the post-landing checks.",
                  kind="review", status="pushing")
    got = rh.signals(p.db, since, now, th, area, "glm_code")
    assert not any("revert" in s for s in got["failing"]) and got["unattributed"], "an unreadable revert counts against no flag"
    p.db.x("DELETE FROM tasks WHERE title LIKE 'Revert %'")
    tid = p.db.add_task("stuck", "s", kind="code")
    p.db.x("UPDATE tasks SET status='running', updated=? WHERE id=?", (now - 3 * _RH, tid))
    assert any(f"#{tid}" in s for s in rh.signals(p.db, since, now, th)["failing"])
    p.db.x("INSERT INTO runs(task,role,status,started) VALUES(?,'worker','running',?)", (tid, now - 3 * _RH))
    assert not any(f"#{tid}" in s for s in rh.signals(p.db, since, now, th)["failing"]), "a live run is not stuck"


def test_a_flags_window_starts_when_it_reached_the_ring_and_a_rollback_resets_it(env):
    from ttp import rollout, rollout_health as rh
    recs = [{"flag": "f", "kind": k, "to": t, "ts": ts} for k, t, ts in
            (("set", 0, 10), ("promote", 1, 20), ("promote", 2, 30), ("rollback", "off", 40), ("arm", "off", 41),
             ("clean", 0, 42), ("set", 1, 50))]
    assert rh.entered("f", 0, recs[:3]) == 10 and rh.entered("f", 1, recs[:3]) == 20 and rh.entered("f", 2, recs[:3]) == 30
    assert rh.entered("f", 0, recs) == 50 and rh.entered("f", 1, recs) == 50 and rh.entered("f", 2, recs) is None
    assert rh.entered("f", 2, recs[:1]) is None


def test_a_flag_promotes_only_with_every_listed_projects_clean_vote_after_the_soak(env):
    from ttp import rollout, rollout_health as rh
    now = time.time()
    _ro_flag("deslop", 1, 30)
    rollout.vote("deslop", "alpha", {})
    rollout.vote("deslop", "alpha", {})
    assert [r["reason"] for r in rollout.ledger("deslop") if r["kind"] == "clean"] == ["alpha clean"], "one vote each"
    names = ["alpha", "beta"]
    recs = lambda: rollout.ledger()
    assert not rh.ready("deslop", 1, names, recs(), {}, now), "beta has not voted"
    rollout.vote("deslop", "beta", {})
    assert rh.ready("deslop", 1, names, recs(), {}, now + 1)
    assert not rh.ready("deslop", 1, names, recs(), {"thresholds": {"soak_h_1": 40}}, now), "the soak has not passed"
    assert not rh.ready("deslop", 2, names, recs(), {}, now) and not rh.ready("deslop", 1, [], recs(), {}, now)
    assert rollout.frozen("deslop") is None, "votes never thaw or freeze a flag"


def _watch(env, name="demo"):
    from ttp.daemon import Daemon
    p = make(env, name)
    return p, Daemon(p.base)


def test_the_daemon_rolls_a_flag_back_for_everyone_on_a_failing_signal_without_a_model(env):
    from ttp import rollout
    p, d = _watch(env)
    rollout.set_ring(p.name, "1")
    _ro_flag("deslop", 1, 3)
    _ro_runs(p, 40, 4, 100)
    _ro_runs(p, 20, 20, 1)
    assert d.rollout_watch() == ["deslop"]
    assert rollout.level("deslop") == rollout.OFF and rollout.frozen("deslop")["by"] == f"daemon:{p.name}"
    assert "failed runs" in rollout.frozen("deslop")["reason"] and rollout.frozen("deslop")["snapshot"]["failing"]
    note = p.db.one("SELECT text, severity FROM messages WHERE ref='rollout_rollback:deslop'")
    assert note["severity"] == "normal" and "rolled back for every project" in note["text"] and "rollout arm deslop" in note["text"]
    d._last_rolloutwatch = 0
    assert d.rollout_watch() == [], "frozen: nothing more to do"
    assert [r["kind"] for r in rollout.ledger("deslop")] == ["set", "rollback"]
    assert rollout.rollback("deslop", "again", f"daemon:other").endswith("already")


def test_the_daemon_leaves_a_projects_own_setting_and_unlisted_rings_alone_and_runs_every_15_minutes(env):
    from ttp import rollout
    p, d = _watch(env)
    _ro_flag("deslop", 1, 3)
    _ro_runs(p, 40, 4, 100)
    _ro_runs(p, 20, 20, 1)
    assert d.rollout_watch() == [] and rollout.level("deslop") == 1, "ring 2 is not covered by level 1"
    rollout.set_ring(p.name, "1")
    cfg = p.read_config()[0]
    cfg.setdefault("deslop", {})["enabled"] = False
    p.config_path.write_text(json.dumps(cfg))
    d._load_config()
    d._last_rolloutwatch = 0
    assert d.rollout_watch() == [] and rollout.level("deslop") == 1
    assert d.rollout_watch() == [] and d._last_rolloutwatch > time.time() - 5


def test_the_daemon_votes_clean_and_promotes_when_the_ring_is_complete(env):
    from ttp import rollout
    p, d = _watch(env)
    rollout.set_ring(p.name, "1")
    _ro_flag("eta_history", 1, 30)
    _ro_runs(p, 40, 4, 100)
    _ro_runs(p, 24, 2, 1)
    assert d.rollout_watch() == ["eta_history"] and rollout.level("eta_history") == 2
    assert [r["kind"] for r in rollout.ledger("eta_history")] == ["set", "clean", "promote"]
    assert rollout.ledger("eta_history")[-1]["by"] == f"daemon:{p.name}"


def test_a_ring_with_no_samples_for_72_hours_stays_and_leaves_a_low_note(env):
    from ttp import rollout
    p, d = _watch(env)
    rollout.set_ring(p.name, "1")
    _ro_flag("device_prewarm", 1, 80)
    assert d.rollout_watch() == []
    assert rollout.level("device_prewarm") == 1 and rollout.frozen("device_prewarm") is None
    note = p.db.one("SELECT text, severity FROM messages WHERE ref='rollout_thin:device_prewarm'")
    assert note["severity"] == "low" and "too few runs" in note["text"]


def _spend(p, usd, ago_h=1):
    p.db.x("INSERT INTO ledger(ts,provider,source,usd) VALUES(?,?,?,?)", (time.time() - ago_h * _RH, "claude", "task:1", usd))


def test_a_scratch_project_has_a_25_dollar_daily_cap_that_only_it_gets_and_that_can_be_raised_or_off(env):
    from ttp import rollout
    p = make(env, "scratch1")
    assert rollout.scratch_state(p.db, p.name) is None, "a project in no ring 0 list has no scratch cap"
    rollout.set_ring(p.name, "0")
    _spend(p, 10.0)
    _spend(p, 99.0, ago_h=30)
    assert rollout.scratch_state(p.db, p.name) == (25.0, 10.0), "the default; spend older than a day does not count"
    assert rollout.scratch_state(p.db, p.name, {"scratch_cap_usd": 60})[0] == 60.0, "the project's own value wins"
    from ttp.project import set_account_setting
    set_account_setting("rollout.scratch_cap_usd", 40)
    assert rollout.scratch_state(p.db, p.name)[0] == 40.0
    assert rollout.scratch_state(p.db, p.name, {"scratch_cap_usd": 0}) is None, "0 turns the cap off"
    rollout.set_ring(p.name, "1")
    assert rollout.scratch_state(p.db, p.name) is None, "a ring 1 project is never capped"


def test_the_scratch_cap_holds_only_routine_work_and_never_ordered_or_urgent_work():
    from ttp import rollout
    over, under = (25.0, 26.0), (25.0, 5.0)
    plain = {"id": 7, "priority": 3, "spec": "do a thing", "reply_chat": None}
    assert rollout.scratch_hold(None, plain) is None
    assert rollout.scratch_hold(under, plain) is None
    why = rollout.scratch_hold(over, plain)
    assert why and why.startswith(rollout.SCRATCH_NOTE) and "$26.00 of $25" in why
    assert rollout.scratch_hold(over, dict(plain, reply_chat="chat1")) is None, "answering the user's chat"
    assert rollout.scratch_hold(over, plain, owners={7: 0}) is None, "an owner of an open order"
    assert rollout.scratch_hold(over, dict(plain, priority=1)) is None, "urgent"
    assert rollout.scratch_hold(over, dict(plain, spec="urgent: the user waits")) is None


def test_dispatch_leaves_a_routine_task_queued_past_the_scratch_cap_and_starts_a_chat_task(env):
    from ttp import rollout
    from ttp.daemon import Daemon
    p = make(env, "scratch2")
    rollout.set_ring(p.name, "0")
    _spend(p, 30.0)
    routine = p.db.add_task("routine", "do a thing", kind="work", tier="light", priority=3)
    d = Daemon(p.base)
    started = []
    d._start_hold = lambda task, provider, ctx, reserve=False: started.append(task["id"]) or "logged_out"
    d._dispatch({})
    assert routine not in started, "past $25 a day a routine task is not even offered a start"
    assert p.db.task(routine)["blocked_reason"].startswith(rollout.SCRATCH_NOTE)
    ordered = p.db.add_task("ordered", "answer", kind="work", tier="light", priority=3, reply_chat="chat1")
    d._dispatch({})
    assert ordered in started and routine not in started, "work the user ordered is never held by the cap"


def test_a_new_alert_votes_rollback_only_for_the_flag_in_its_own_area_by_the_keys_the_harness_raises(env):
    from ttp import rollout, rollout_health as rh
    p = make(env)
    th, now = rh.thresholds(None), time.time() + 60
    since = now - 24 * _RH
    raise_ = lambda key, sev: p.db.post("out", "x", kind="alert", severity=sev, ref=key)   # the path every alert takes
    for key, sev in (("auth:glm", "high"), ("model-trip:glm|code", "low"), ("model-demote:m|code", "low"),
                     ("route-model-missing:std|m", "normal"), ("after_push_failed", "high"), ("push_tip_failing", "high"),
                     ("auth:claude", "low"), ("eta_ledger_stale", "high")):
        raise_(key, sev)
    area = lambda flag: rollout.FLAGS[flag]["area"]
    got = lambda flag: rh.signals(p.db, since, now, th, area(flag))["failing"]
    assert got("glm_code") == ["new alert auth:glm"], "model routing trips are low and never stored: not judged"
    assert rh.signals(p.db, since, now, th, "push")["failing"] == ["new alert after_push_failed", "new alert push_tip_failing"]
    assert "push_isolation" not in rollout.FLAGS, "graduated: the push batch survival runs for every project"
    for none in ("eta_history", "device_prewarm", "deslop"):
        assert got(none) == [], none
    assert rh.signals(p.db, since, now, th)["failing"] == [], "no area, no alert votes"
    p.db.x("UPDATE alerts SET raised=? WHERE key='auth:glm'", (now - 100 * _RH,))   # already in the baseline: not new
    assert got("glm_code") == []
    recs = [{"flag": "glm_code", "kind": "set", "to": 1, "ts": since}]
    assert rh.verdict(p.db, "glm_code", 0, recs, {}, now)["failing"] == got("glm_code"), "the daemon's path judges the same"


def test_every_alert_prefix_of_an_area_is_a_key_the_runtime_raises():
    import re
    from pathlib import Path
    from ttp import rollout, rollout_health as rh
    src = {f.name: f.read_text() for f in Path(rh.__file__).parent.glob("*.py") if f.name != "rollout_health.py"}
    assert {a["area"] for a in rollout.FLAGS.values()} <= set(rh.ALERT_AREAS)
    for area, prefixes in rh.ALERT_AREAS.items():
        for pre in prefixes:
            pat = re.compile(r"(alert\(\s*f?|_ALERT\s*=\s*)[\"']" + re.escape(pre))
            assert any(pat.search(t) for t in src.values()), f"{area}: nothing in the runtime raises {pre!r}"


def _raised_severities(pre: str) -> set[str]:
    """The severities the runtime's alert(...) calls give a key starting with `pre` (a call without one is high,
    the default of Daemon.alert); none found: the key is raised some other way, as high."""
    import ast
    from pathlib import Path
    from ttp import rollout_health as rh
    found: set[str] = set()
    for f in Path(rh.__file__).parent.glob("*.py"):
        for n in ast.walk(ast.parse(f.read_text())):
            if not (isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) == "alert" and n.args):
                continue
            key = n.args[0]
            head = (key.values[0] if isinstance(key, ast.JoinedStr) and key.values else key)
            if not (isinstance(head, ast.Constant) and isinstance(head.value, str) and head.value.startswith(pre)):
                continue
            sev = {k.arg: k.value for k in n.keywords}.get("severity") or (n.args[2] if len(n.args) > 2 else None)
            found.add(sev.value if isinstance(sev, ast.Constant) else "high")
    return found


def test_every_alert_prefix_of_an_area_is_stored_where_the_health_watch_reads_it(env):
    from ttp import alerts, rollout_health as rh
    p = make(env)
    th, now = rh.thresholds(None), time.time() + 60
    since = now - 24 * _RH
    for area, prefixes in rh.ALERT_AREAS.items():
        keys = []
        for pre in prefixes:
            assert "high" in (_raised_severities(pre) or {"high"}), f"{area}: {pre} is never raised high: not stored"
            keys.append(pre + "x" if pre.endswith(":") else pre)
        for key in keys:
            assert alerts.tracked("alert", "high", key), f"{area}: {key} raised high is not stored as an alert"
            p.db.post("out", "x", kind="alert", severity="high", ref=key)
        assert rh.signals(p.db, since, now, th, area)["failing"] == [f"new alert {k}" for k in sorted(keys)], area


def _eta_pair(p, now, shown_s, history=3):
    """A landed code task with an ETA of `shown_s` seconds logged when it started, after `history` earlier
    landed code tasks (the history estimate reads them); returns its ref."""
    from ttp import etaledger
    for i in range(history):
        _landed_code(p, now, created=-40000 - 900 * i, runs=((-39900 - 900 * i, -39300 - 900 * i),),
                     pushed=-39000 - 900 * i)
    tid = _landed_code(p, now, created=-7200, runs=((-7000, -6500),), pushed=-600)
    etaledger.record(p.db, p, [(f"t{tid}", "code", {"end": now - 7000 + shown_s, "path": [tid]})], now - 7000)
    etaledger.settle(p.db, now)
    return f"t{tid}"


def test_eta_history_shadow_logs_a_pair_and_a_baseline_only_with_the_flag_on(env, capsys):
    from ttp import cli, costlevers, etaledger
    p = make(env)
    now = time.time()
    assert not costlevers.lever_on(p, "eta_history")
    _eta_pair(p, now, 3600)
    rows = [json.loads(x) for x in etaledger._path(p.db).read_text().splitlines()]
    assert rows and all("hist" not in r for r in rows), "flag off: nothing new is read or logged"
    assert etaledger.shadow(p.db, now - 7 * 86400)["count"] == 0
    assert costlevers.eta_shadow_line(p, now - 7 * 86400) == ""
    capsys.readouterr()
    cli.main(["eta", p.name, "--calibration"])
    assert "ETA calibration" in (out := capsys.readouterr().out) and "ETA shadow" not in out
    # Flag on: each ETA is logged beside its history estimate, and the baseline line renders.
    p.set_config("eta.history", True)
    _eta_pair(p, now, 1800)
    rows = [json.loads(x) for x in etaledger._path(p.db).read_text().splitlines()]
    assert sum("hist" in r for r in rows) == 1
    sh = etaledger.shadow(p.db, now - 7 * 86400)
    assert sh["count"] == 1 and sh["current"]["median"] >= 0 and sh["hist"]["median"] >= 0
    line = costlevers.eta_shadow_line(p, now - 7 * 86400)
    assert line.startswith("ETA shadow (eta_history, 7 d): 1 compared; abs error median/p90 current ")
    assert any("ETA shadow" in x for x in costlevers.status_lines(p, now))
    capsys.readouterr()
    cli.main(["eta", p.name, "--calibration"])
    assert "ETA shadow (eta_history, 7 d): 1 compared" in capsys.readouterr().out


_RT = "plugins/tt-project/runtime/ttp/"


def _git(repo, *argv):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *argv],
                          capture_output=True, text=True, check=True).stdout.strip()


def _ro_repo(tmp_path, *changed):
    """A throwaway repo: a base commit, then one commit changing the runtime files `changed`. Returns
    (repo, the revert task's spec naming that span)."""
    repo = tmp_path / "repo"
    (repo / _RT).mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    for n in ("etaledger.py", "deslop.py", "daemon.py", "sched.py", "costlevers.py"):
        (repo / _RT / n).write_text("x = 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    for n in changed:
        (repo / _RT / n).write_text("x = 1\n")
    _git(repo, "commit", "-qam", "change")
    sha = _git(repo, "rev-parse", "HEAD")
    return str(repo), f"Forward revert of {base[:10]}..{sha[:10]} by the post-landing checks."


def _ro_revert(p, spec):
    from ttp import postland
    return p.db.add_task(f"Revert #7 ttp/t7-x{postland.REVERT_TITLE_TAIL}", spec, kind="review", status="pushing")


def _revert_count(p, flag, repo):
    from ttp import rollout, rollout_health as rh
    now = time.time() + 60
    return rh.signals(p.db, now - 24 * _RH, now, rh.thresholds(None), rollout.FLAGS[flag]["area"], flag, repo)


def test_a_revert_counts_only_against_a_flag_whose_gated_code_the_reverted_commit_touched(env, tmp_path):
    # cells: unrelated_revert, gated_revert
    p = make(env)
    repo, spec = _ro_repo(tmp_path, "sched.py")
    _ro_revert(p, spec)
    for flag in ("deslop", "output_trim", "glm_code", "coord_wake_settle"):
        got = _revert_count(p, flag, repo)
        assert got["failing"] == [] and got["unattributed"] == [], f"{flag}: a revert of unrelated code is no signal"
    p.db.x("DELETE FROM tasks WHERE title LIKE 'Revert %'")
    repo, spec = _ro_repo(tmp_path / "b", "deslop.py")
    _ro_revert(p, spec)
    got = _revert_count(p, "deslop", repo)
    assert len(got["failing"]) == 1 and "1 forward revert(s) of a landed change" in got["failing"][0]
    assert _revert_count(p, "output_trim", repo)["failing"] == [], "another flag's code is not this one's"


def test_a_revert_of_a_run_schedules_change_does_not_trip_the_eta_flag_in_shadow(env, tmp_path):
    # cells: shadow_flag, unrelated_revert: the 2026-11-17 false trip (a daemon.py change reverted, eta_history rolled back)
    from ttp import rollout
    p = make(env)
    repo, spec = _ro_repo(tmp_path, "daemon.py")
    _ro_revert(p, spec)
    assert _revert_count(p, "eta_history", repo)["failing"] == []
    assert _revert_count(p, "deslop", repo)["failing"] == []
    assert rollout.FLAGS["eta_history"]["shadow"] is True


def test_a_shadow_flag_never_counts_a_revert_even_of_its_own_code(env, tmp_path):
    # cells: shadow_flag, shadow_exempt
    p = make(env)
    repo, spec = _ro_repo(tmp_path, "etaledger.py")
    _ro_revert(p, spec)
    assert _revert_count(p, "eta_history", repo)["failing"] == []
    from ttp import rollout
    saved = dict(rollout.FLAGS["eta_history"])
    try:
        rollout.FLAGS["eta_history"].pop("shadow")
        assert len(_revert_count(p, "eta_history", repo)["failing"]) == 1, "the same revert counts once the flag is not shadow"
    finally:
        rollout.FLAGS["eta_history"].clear()
        rollout.FLAGS["eta_history"].update(saved)


def test_a_revert_whose_commit_cannot_be_resolved_is_listed_unattributed_and_fails_no_flag(env, tmp_path):
    # cells: unattributed_revert
    from ttp import rollout, rollout_health as rh
    p = make(env)
    repo, _ = _ro_repo(tmp_path, "deslop.py")
    gone = _ro_revert(p, "Forward revert of 1234567890..abcdef1234 by the post-landing checks.")
    none = _ro_revert(p, "no span in this text")
    for flag in rollout.FLAGS:
        got = _revert_count(p, flag, repo)
        assert got["failing"] == [] and got["unattributed"] == [f"#{gone}", f"#{none}"], flag
    assert _revert_count(p, "deslop", None)["unattributed"] == [f"#{gone}", f"#{none}"], "no repo: unresolved too"
    now = time.time() + 60
    recs = [{"flag": "deslop", "kind": "set", "to": 1, "ts": now - 24 * _RH}]
    v = rh.verdict(p.db, "deslop", 1, recs, {}, now, repo)
    assert v["unattributed"] == [f"#{gone}", f"#{none}"] and v["failing"] == []


def test_the_daemon_passes_its_project_root_so_a_gated_revert_rolls_the_flag_back_and_an_unrelated_one_does_not(env, tmp_path):
    from ttp import rollout
    p, d = _watch(env)
    rollout.set_ring(p.name, "1")
    _ro_flag("deslop", 1, 3)
    repo, spec = _ro_repo(tmp_path, "sched.py")
    d.p.root = type(p.root)(repo)
    _ro_revert(p, spec)
    assert d.rollout_watch() == [] and rollout.level("deslop") == 1
    repo, spec = _ro_repo(tmp_path / "g", "deslop.py")
    d.p.root = type(p.root)(repo)
    p.db.x("DELETE FROM tasks WHERE title LIKE 'Revert %'")
    _ro_revert(p, spec)
    d._last_rolloutwatch = 0
    assert d.rollout_watch() == ["deslop"] and "forward revert" in rollout.frozen("deslop")["reason"]


def test_every_flag_call_site_lies_in_the_files_the_flags_code_list_names():
    # cells: gated_revert: the health watch trusts rollout.FLAGS[flag]["code"], so a new on() call site must be listed
    import re
    from pathlib import Path
    from ttp import rollout
    rt = Path(rollout.__file__).parent
    pat = re.compile(r"(?:rollout\.on|lever_on|costlevers\.lever_on)\(\s*(?:[\w.]+\s*,\s*)?[\"'](\w+)[\"']")
    sites: dict[str, set[str]] = {}
    for f in rt.glob("*.py"):
        if f.name in ("rollout.py", "rollout_health.py"):
            continue
        for flag in pat.findall(f.read_text()):
            sites.setdefault(flag, set()).add(_RT + f.name)
    assert set(sites) <= set(rollout.FLAGS), f"on() names a flag that is not in FLAGS: {set(sites) - set(rollout.FLAGS)}"
    assert "eta_history" in sites and "glm_code" in sites, "the scan must find the known call sites"
    for flag, files in sites.items():
        assert files <= set(rollout.FLAGS[flag]["code"]), f"{flag}: on() outside its code list: {sorted(files - set(rollout.FLAGS[flag]['code']))}"
    for flag, meta in rollout.FLAGS.items():
        assert all((rt.parent.parent.parent.parent / c).is_file() for c in meta["code"]), f"{flag}: a listed code file does not exist"


# Waves: auto's ordered project lists and the model-free wave gate (rollout.wave_tick).

def _judge(states):
    """A judge answering `states[name]` (ok, worse or wait) for the project of that name."""
    def judge(p, cfg, now):
        return {"state": states[p.name], "why": f"{p.name} {states[p.name]}", "n": 12}
    return judge


def _wave_projects(tmp_path, *names):
    from types import SimpleNamespace
    return {n: SimpleNamespace(name=n, config_path=tmp_path / "none" / "project.json") for n in names}


def test_waves_are_set_and_cleared_only_from_the_terminal(env, capsys, monkeypatch):
    from ttp import project, rollout
    assert _roll(capsys, "waves", "glm_work", "alpha,beta", "gamma")[0] == 0
    assert rollout.waves("glm_work") == [["alpha", "beta"], ["gamma"]] and rollout.wave_at("glm_work") == 0
    assert rollout.ledger("glm_work")[-1]["kind"] == "waves"
    code, out = _roll(capsys, "waves", "glm_work", "alpha", "alpha,beta")
    assert code == 2 and "one wave only" in out
    code, out = _roll(capsys, "waves", "deslop", "alpha")
    assert code == 2 and "no wave gate" in out
    monkeypatch.setenv("TTP_RUN_DIR", "/nonexistent")
    for argv in (("waves", "glm_work", "clear"), ("set", "glm_work", "auto"), ("arm", "glm_work", "alpha")):
        code, out = _roll(capsys, *argv)
        assert code == 2 and "only the user's terminal" in out
    monkeypatch.delenv("TTP_RUN_DIR")
    assert _roll(capsys, "set", "glm_work", "auto")[0] == 0
    code, out = _roll(capsys, "waves", "glm_work", "clear")
    assert code == 2 and "another level" in out
    _roll(capsys, "set", "glm_work", "off")
    assert _roll(capsys, "waves", "glm_work", "clear")[0] == 0 and rollout.waves("glm_work") == []
    for key in ("waves", "wave_at", "project_rollback"):
        assert f"rollout.{key}" in project.ACCOUNT_USER_ONLY and key in project.ACCOUNT_KEYS["rollout"]


def test_auto_needs_waves_and_covers_the_waves_up_to_the_current_one(env, capsys):
    from ttp import rollout
    code, out = _roll(capsys, "set", "glm_work", "auto")
    assert code == 2 and "no waves" in out and rollout.level("glm_work") == rollout.OFF
    code, out = _roll(capsys, "set", "deslop", "auto")
    assert code == 2 and "no wave gate" in out
    _roll(capsys, "waves", "glm_work", "alpha,beta", "gamma")
    assert _roll(capsys, "set", "glm_work", "auto")[0] == 0 and rollout.level("glm_work") == rollout.AUTO
    assert {n for n in ("alpha", "beta", "gamma", "delta") if rollout.on("glm_work", n)} == {"alpha", "beta"}
    rollout._put("wave_at", "glm_work", 1)
    assert {n for n in ("alpha", "beta", "gamma", "delta") if rollout.on("glm_work", n)} == {"alpha", "beta", "gamma"}
    rollout.set_ring("beta", "exclude")
    assert not rollout.on("glm_work", "beta"), "an excluded project never takes a flag"
    assert rollout.on("glm_work", "delta", {"route": {"glm_work": {"on": False}}}), "its own setting wins"
    assert "auto" in _roll(capsys, "promote", "glm_work")[1] and rollout.level("glm_work") == rollout.AUTO


def test_the_one_paste_rollout_is_harmless_to_paste_again_and_keeps_the_current_wave(env, capsys):
    from ttp import rollout
    paste = (("waves", "glm_work", "alpha,beta", "gamma", "delta"), ("set", "glm_work", "auto", "--reason", "go"))
    for argv in paste:
        assert _roll(capsys, *argv)[0] == 0
    rollout._put("wave_at", "glm_work", 1)
    entries = len(rollout.ledger("glm_work"))
    outs = [_roll(capsys, *argv) for argv in paste]
    assert [c for c, _ in outs] == [0, 0] and "unchanged" in outs[0][1] and "already" in outs[1][1]
    assert rollout.level("glm_work") == rollout.AUTO and rollout.wave_at("glm_work") == 1
    assert len(rollout.ledger("glm_work")) == entries, "a second paste records nothing"


def test_the_wave_gate_waits_then_advances_once_every_project_of_the_wave_is_ok_once_an_hour(env, capsys, tmp_path):
    from ttp import rollout
    _roll(capsys, "waves", "glm_work", "alpha,beta", "gamma")
    _roll(capsys, "set", "glm_work", "auto")
    projs, now = _wave_projects(tmp_path, "alpha", "beta", "gamma"), time.time()
    states = {"alpha": "ok", "beta": "wait", "gamma": "ok"}
    assert rollout.wave_tick(now, _judge(states), projs) == [] and rollout.wave_at("glm_work") == 0
    states["beta"] = "ok"
    assert rollout.wave_tick(now + 1800, _judge(states), projs) == [], "once an hour per flag"
    recs = rollout.wave_tick(now + 3600, _judge(states), projs)
    assert [(r["op"], r["wave"], r["projects"]) for r in recs] == [("advance", 1, ["gamma"])]
    assert rollout.wave_at("glm_work") == 1 and rollout.on("glm_work", "gamma")
    last = rollout.ledger("glm_work")[-1]
    assert last["op"] == "advance" and last["flag"] == "glm_work" and "alpha ok" in last["why"]
    assert rollout.wave_tick(now + 7200, _judge(states), projs) == [], "the last wave: nothing more to open"
    assert rollout.wave_tick(now + 9000, _judge(states), {}) == [] and rollout.wave_at("glm_work") == 1


def test_a_worse_project_is_rolled_back_alone_and_the_terminal_rearms_it(env, capsys, tmp_path):
    from ttp import rollout
    _roll(capsys, "waves", "glm_work", "alpha,beta", "gamma")
    _roll(capsys, "set", "glm_work", "auto")
    projs, now = _wave_projects(tmp_path, "alpha", "beta", "gamma"), time.time()
    states = {"alpha": "worse", "beta": "ok", "gamma": "ok"}
    recs = rollout.wave_tick(now, _judge(states), projs)
    assert [(r["op"], r["project"]) for r in recs] == [("project_rollback", "alpha")], "no advance in a tick that rolled back"
    assert not rollout.on("glm_work", "alpha") and rollout.on("glm_work", "beta") and rollout.level("glm_work") == "auto"
    assert rollout.rolled_back("glm_work")["alpha"]["why"] == "alpha worse"
    assert "rolled back in alpha alone" in _roll(capsys, "status")[1]
    recs = rollout.wave_tick(now + 3600, _judge(states), projs)
    assert [r["op"] for r in recs] == ["advance"], "a rolled-back project no longer holds its wave"
    assert "re-armed in alpha" in _roll(capsys, "arm", "glm_work", "alpha")[1]
    assert rollout.on("glm_work", "alpha") and rollout.ledger("glm_work")[-1]["op"] == "project_arm"
    assert "not rolled back" in _roll(capsys, "arm", "glm_work", "alpha")[1]
    assert rollout.frozen("glm_work") is None


def test_each_wave_move_or_rollback_raises_one_event_in_the_project_it_concerns(env, capsys, tmp_path):
    from ttp import rollout
    g = make(env, "gamma")
    projs = {**_wave_projects(tmp_path, "alpha", "delta"), "gamma": g}
    _roll(capsys, "waves", "glm_work", "alpha", "gamma", "delta")
    _roll(capsys, "set", "glm_work", "auto")
    now = time.time()
    states = {"alpha": "ok", "gamma": "ok", "delta": "ok"}
    assert [r["projects"] for r in rollout.wave_tick(now, _judge(states), projs)] == [["gamma"]]
    assert [r["projects"] for r in rollout.wave_tick(now + 3600, _judge(states), projs)] == [["delta"]]
    states["gamma"] = "worse"
    assert [r["op"] for r in rollout.wave_tick(now + 7200, _judge(states), projs)] == ["project_rollback"]
    assert rollout.project_events(g, now) == ["rollout_wave", "rollout_wave"], "wave 2 is not about it"
    assert rollout.project_events(g, now) == [], "reported once"
    texts = [r["text"] for r in g.db.q("SELECT text FROM events WHERE kind='rollout_wave' ORDER BY id")]
    assert "opened wave 1" in texts[0] and "ttp rollout arm glm_work gamma" in texts[1]


def test_status_shows_a_projects_overriding_own_setting_since_it_was_set(env, capsys):
    from ttp import rollout, timefmt
    p = make(env, "alpha")
    p.set_config("route.code_sonnet.glm_share", 0)
    _roll(capsys, "set", "glm_code", "2")
    o = rollout.status()["flags"]["glm_code"]["overrides"]
    assert [(x["project"], x["value"]) for x in o] == [("alpha", 0)]
    assert o[0]["since"] == rollout.override_since(p, "route.code_sonnet.glm_share") and o[0]["since"] <= time.time()
    out = _roll(capsys, "status")[1]
    assert f"overridden in alpha by its own setting since {timefmt.long(o[0]['since'], p.raw_config())}" in out
    raw = p.read_config()[0]
    raw.setdefault("deslop", {})["enabled"] = True
    p.config_path.write_text(json.dumps(raw))
    _roll(capsys, "set", "deslop", "0")
    assert rollout.status()["flags"]["deslop"]["overrides"][0]["since"] == p.config_path.stat().st_mtime, \
        "no recorded set: project.json's time"
    _roll(capsys, "set", "glm_code", "off")
    assert rollout.status()["flags"]["glm_code"]["overrides"] == [], "only for a flag the user turned on"


def test_an_overriding_own_setting_raises_one_event_a_day(env, capsys):
    from ttp import rollout
    p = make(env, "alpha")
    p.set_config("route.code_sonnet.glm_share", 0)
    now = time.time()
    assert rollout.project_events(p, now) == [], "the flag is off: nothing overridden"
    _roll(capsys, "set", "glm_code", "1")
    assert rollout.project_events(p, now) == ["rollout_override"]
    assert rollout.project_events(p, now + 60) == []
    assert rollout.project_events(p, now + 86400) == ["rollout_override"]
    text = p.db.one("SELECT text FROM events WHERE kind='rollout_override'")["text"]
    assert "route.code_sonnet.glm_share = 0 overrides it" in text and "ttp config alpha" in text
