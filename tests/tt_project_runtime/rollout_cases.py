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
    assert "frozen" in _roll(capsys, "rollback", "eta_history", "--reason", "failed runs up")[1]
    assert rollout.level("eta_history") == rollout.OFF and rollout.frozen("eta_history")["reason"] == "failed runs up"
    code, out = _roll(capsys, "set", "eta_history", "0")
    assert code == 2 and "frozen" in out and rollout.level("eta_history") == rollout.OFF
    assert _roll(capsys, "promote", "eta_history")[0] == 2
    assert "re-armed" in _roll(capsys, "arm", "eta_history")[1]
    assert rollout.level("eta_history") == rollout.OFF, "a rollback from off arms back at off"
    assert rollout.frozen("eta_history") is None
    assert "not frozen" in _roll(capsys, "arm", "eta_history")[1]
    assert _roll(capsys, "promote", "eta_history")[0] == 0 and rollout.level("eta_history") == 0
    assert [r["kind"] for r in rollout.ledger("eta_history")] == ["rollback", "arm", "promote"]


def test_a_run_may_read_or_roll_back_but_never_widen_the_rollout(env, capsys, monkeypatch):
    from ttp import rollout
    _roll(capsys, "set", "deslop", "1")
    monkeypatch.setenv("TTP_RUN_DIR", "/nonexistent")
    for argv in (("ring", "x", "0"), ("set", "deslop", "2"), ("promote", "deslop")):
        code, out = _roll(capsys, *argv)
        assert code == 2 and "only the user's terminal" in out
    code, out = _roll(capsys, "arm", "deslop")
    assert code == 2 and "unfreezing a flag stays with the user" in out, "a run never unfreezes a flag"
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
    assert d.rollout_watch() == ["deslop"] and not rollout.on("deslop", p.name)
    why = rollout.rolled_back("deslop")[p.name]["why"]
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


def test_the_daemon_switches_a_flag_off_for_its_own_project_alone_on_a_failing_signal_without_a_model(env):
    # cells: watch_project_off, watch_rearm_window
    from ttp import rollout
    p, d = _watch(env)
    rollout.set_ring(p.name, "1")
    rollout.set_ring("other", "1")
    _ro_flag("deslop", 1, 3)
    _ro_runs(p, 40, 4, 100)
    _ro_runs(p, 20, 20, 1)
    assert d.rollout_watch() == ["deslop"]
    assert rollout.level("deslop") == 1 and rollout.frozen("deslop") is None, "the fleet keeps the flag"
    assert not rollout.on("deslop", p.name) and rollout.on("deslop", "other")
    back = rollout.rolled_back("deslop")[p.name]
    assert "failed runs" in back["why"] and rollout.ledger("deslop")[-1]["by"] == f"daemon:{p.name}"
    note = p.db.one("SELECT text, severity FROM messages WHERE ref='rollout_rollback:deslop'")
    assert note["severity"] == "normal" and f"switched off in {p.name} alone" in note["text"]
    assert f"rollout arm deslop {p.name}" in note["text"]
    d._last_rolloutwatch = 0
    assert d.rollout_watch() == [], "switched off here: nothing more to do"
    assert [r["kind"] for r in rollout.ledger("deslop")] == ["set", "project_rollback"]
    assert "re-armed in" in rollout.arm("deslop", "fix landed", "user", p.name) and rollout.on("deslop", p.name)
    d._last_rolloutwatch = 0
    d.rollout_watch()
    assert p.name not in rollout.rolled_back("deslop"), "the runs that switched it off never count again"
    assert "-> off" in rollout.rollback("deslop", "by hand", "user") and rollout.frozen("deslop"), "the terminal's freeze stays"


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
    _ro_flag("deslop", 1, 80)
    assert d.rollout_watch() == []
    assert rollout.level("deslop") == 1 and rollout.frozen("deslop") is None
    note = p.db.one("SELECT text, severity FROM messages WHERE ref='rollout_thin:deslop'")
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
    for none in ("eta_history", "deslop"):
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
    for n in ("etaledger.py", "deslop.py", "daemon.py", "sched.py", "costlevers.py", "codearm.py"):
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
    for flag in ("deslop", "glm_code", "coord_wake_settle"):
        got = _revert_count(p, flag, repo)
        assert got["failing"] == [] and got["unattributed"] == [], f"{flag}: a revert of unrelated code is no signal"
    p.db.x("DELETE FROM tasks WHERE title LIKE 'Revert %'")
    repo, spec = _ro_repo(tmp_path / "b", "deslop.py")
    _ro_revert(p, spec)
    got = _revert_count(p, "deslop", repo)
    assert len(got["failing"]) == 1 and "1 forward revert(s) of a landed change" in got["failing"][0]
    assert _revert_count(p, "coord_wake_settle", repo)["failing"] == [], "another flag's code is not this one's"


def _ro_authored(p, monkeypatch, spec, provider, model, failing=""):
    """A code task run on `provider`/`model`, then the post-landing revert of its landing (the span `spec`
    names) and the revert's fix task naming the failing tests, both written by postland.apply."""
    import json
    from ttp import postland, pushq
    t = p.db.add_task("code: x", "s", kind="code", status="done")
    p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
           (t, "worker", provider, model, time.time(), "done", 0.0, json.dumps({})))
    base, sha = spec.split()[3].split("..")
    monkeypatch.setattr(pushq, "approve", lambda *a, **k: [])   # the queue row is not what is counted
    assert postland.apply(p, {"sha": sha, "target": "up/main"}, {
        "outcome": "guilty", "ids": failing.split(), "cmd": "pytest", "tail": "",
        "entry": {"task": t + 100, "branch": f"ttp/t{t}-x", "sha": sha, "base": base},
        "revert_head": "c" * 40, "revert_branch": "revert/x"}) == "guilty"


def test_a_revert_freezes_a_model_route_flag_only_when_its_arm_made_the_change_or_its_tests_failed(env, tmp_path,
                                                                                                    monkeypatch):
    # cells: other_arm_revert, glm_arm_revert, gated_test_revert
    from pathlib import Path
    p = make(env)
    repo, spec = _ro_repo(tmp_path, "codearm.py", "deslop.py", "sched.py")
    (Path(repo) / "tests" / "tt_project_runtime").mkdir(parents=True)
    (Path(repo) / "tests" / "tt_project_runtime" / "codearm_cases.py").write_text("def test_arm(env):\n    pass\n")
    (Path(repo) / "tests" / "test_other.py").write_text(
        "def test_unrelated(env):\n    pass\n\n\ndef test_share(env):\n    set_('route.code_sonnet.glm_share')\n")
    _ro_authored(p, monkeypatch, spec, "claude", "claude-opus-5-5", "tests/test_other.py::test_unrelated tests/x.py::test_gone[a]")
    got = _revert_count(p, "glm_code", repo)
    assert got["failing"] == [] and got["unattributed"] == [], "a claude-arm change touching codearm.py: no freeze"
    assert len(_revert_count(p, "deslop", repo)["failing"]) == 1, "a flag with no arm still counts touched files"
    for failing in ("tests/test_tt_project_runtime.py::test_arm", "tests/test_other.py::test_share[x]"):
        p.db.x("DELETE FROM tasks WHERE title LIKE 'Revert %' OR title LIKE 'Fix %'")
        _ro_authored(p, monkeypatch, spec, "claude", "claude-opus-5-5", failing)
        assert len(_revert_count(p, "glm_code", repo)["failing"]) == 1, f"{failing} exercises the flag's code"
    p.db.x("DELETE FROM tasks WHERE title LIKE 'Revert %' OR title LIKE 'Fix %'")
    _ro_authored(p, monkeypatch, spec, "chat", "glm-5.3", "tests/test_other.py::test_unrelated")
    got = _revert_count(p, "glm_code", repo)
    assert len(got["failing"]) == 1 and "forward revert(s) of a landed change" in got["failing"][0]
    repo2, spec2 = _ro_repo(tmp_path / "b", "sched.py")
    p.db.x("DELETE FROM tasks WHERE title LIKE 'Revert %' OR title LIKE 'Fix %'")
    _ro_authored(p, monkeypatch, spec2, "chat", "glm-5.3")
    assert _revert_count(p, "glm_code", repo2)["failing"] == [], "a GLM change outside the flag's code: as before"


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
    assert d.rollout_watch() == ["deslop"] and "forward revert" in rollout.rolled_back("deslop")[p.name]["why"]


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
    for argv in (("waves", "glm_work", "clear"), ("set", "glm_work", "auto")):
        code, out = _roll(capsys, *argv)
        assert code == 2 and "only the user's terminal" in out
    code, out = _roll(capsys, "arm", "glm_work", "alpha")
    assert code == 2 and "rollout.run_rearm is off" in out
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
    assert "glm_pct" not in recs[0], "a verdict without the comparison writes none"
    assert "rolled back in alpha alone" in _roll(capsys, "status")[1]
    recs = rollout.wave_tick(now + 3600, _judge(states), projs)
    assert [r["op"] for r in recs] == ["advance"], "a rolled-back project no longer holds its wave"
    assert "re-armed in alpha" in _roll(capsys, "arm", "glm_work", "alpha")[1]
    assert rollout.on("glm_work", "alpha") and rollout.ledger("glm_work")[-1]["op"] == "project_arm"
    assert "not rolled back" in _roll(capsys, "arm", "glm_work", "alpha")[1]
    assert rollout.frozen("glm_work") is None


def test_a_rollback_record_carries_both_pass_rates_and_the_baseline_window(env, capsys, tmp_path):
    from ttp import rollout
    _roll(capsys, "waves", "glm_work", "alpha")
    _roll(capsys, "set", "glm_work", "auto")
    projs, now = _wave_projects(tmp_path, "alpha"), time.time()
    v = {"state": "worse", "why": "worse; " + "x" * 400, "n": 25, "glm_pct": 40.0, "base_pct": 60.0,
         "base_window": [now - 86400, now]}
    rollout.wave_tick(now, lambda p, cfg, at: v, projs)
    last = rollout.ledger("glm_work")[-1]
    assert last["op"] == "project_rollback" and len(last["why"]) == 300, "the reason is cut, the numbers are not"
    assert (last["glm_pct"], last["base_pct"], last["base_window"]) == (40.0, 60.0, [now - 86400, now])


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


def _ro_task_runs(p, n, failed, ago_h, glm, role="worker", kind="code"):
    """`n` runs ending `ago_h` hours back on one task of `kind`, the first `failed` failed; on GLM or Claude."""
    tid = p.db.add_task("r", "s", kind=kind, tier="standard")
    end = time.time() - ago_h * _RH
    for i in range(n):
        p.db.x("INSERT INTO runs(task,role,provider,model,status,started,ended,note) VALUES(?,?,?,?,?,?,?,'{}')",
               (tid, role, "chat" if glm else "claude", "glm-5.3" if glm else "opus",
                "failed" if i < failed else "done", end - 5, end))
    return tid


_ARMS = ("glm_code", "glm_work", "glm_device")


def _flag_health(p, flag):
    from ttp import rollout, rollout_health as rh
    now = time.time() + 60
    return rh.signals(p.db, now - 24 * _RH, now, rh.thresholds(None), rollout.FLAGS[flag]["area"], flag)


def test_a_model_route_flag_is_blamed_for_failed_runs_only_on_its_arm(env):
    # cells: failed_runs_off_arm, failed_runs_on_arm, coord_turns
    p = make(env)
    _ro_task_runs(p, 40, 4, 100, glm=False)
    _ro_task_runs(p, 20, 20, 1, glm=False)            # Claude runs fail: the GLM route ran none of them
    for flag in _ARMS + ("glm_coord",):
        got = _flag_health(p, flag)
        assert got["failing"] == [] and got["runs"] == 0, flag
    assert any(s.startswith("failed runs") for s in _flag_health(p, "deslop")["failing"]), "no arm: project-wide"
    _ro_task_runs(p, 20, 20, 1, glm=True)
    for flag in _ARMS:
        got = _flag_health(p, flag)
        assert got["runs"] == 20 and any(s.startswith("failed runs 100% against 10%") for s in got["failing"]), flag
    assert _flag_health(p, "glm_coord")["failing"] == [], "worker runs are not coordinator turns"
    _ro_task_runs(p, 40, 4, 100, glm=False, role="coordinator")
    _ro_task_runs(p, 20, 20, 1, glm=True, role="coordinator")
    assert any(s.startswith("failed runs") for s in _flag_health(p, "glm_coord")["failing"])


def test_a_model_route_flag_is_blamed_for_landings_only_when_its_arm_ran_the_task(env):
    # cells: landings_off_arm, landings_on_arm
    p = make(env)

    def landed(took_h, cost, ago_h, glm):
        _ro_landed(p, took_h, cost, ago_h)
        tid = p.db.one("SELECT MAX(id) id FROM tasks")["id"]
        p.db.x("INSERT INTO runs(task,role,provider,model,status,started,ended,note) VALUES(?,?,?,?,?,?,?,'{}')",
               (tid, "worker", "chat" if glm else "claude", "glm-5.3" if glm else "opus", "done", 1, 2))
    for i in range(20):
        landed(1, 2.0, 100 + i, glm=False)
        landed(3, 9.0, 1 + i / 10, glm=False)          # slow and dear, all on Claude
    for flag in _ARMS + ("glm_coord",):
        assert not [s for s in _flag_health(p, flag)["failing"] if "landing" in s], flag
    assert len([s for s in _flag_health(p, "deslop")["failing"] if "landing" in s]) == 2, "no arm: project-wide"
    for i in range(20):
        landed(3, 9.0, 1 + i / 10, glm=True)
    for flag in _ARMS:
        failing = _flag_health(p, flag)["failing"]
        assert any("median time to land 3.0 h against 1.0 h" in s for s in failing), flag
        assert any("cost per landing $9.00 against $2.00" in s for s in failing), flag


def test_a_stuck_task_counts_against_a_glm_flag_only_when_its_own_glm_run_stalled(env):
    # cells: stuck_off_arm, stuck_on_arm, stuck_batch_member
    import json
    from ttp import batchrun
    p = make(env)
    stale = time.time() - 3 * _RH

    def stuck(*glm):
        tid = p.db.add_task("s", "s", kind="code")
        for i, g in enumerate(glm):
            p.db.x("INSERT INTO runs(task,role,provider,model,status,started,ended,note) VALUES(?,?,?,?,?,?,?,'{}')",
                   (tid, "worker", "chat" if g else "claude", "glm-5.3" if g else "opus",
                    "fallback" if g else "done", stale - 100 + i, stale - 50 + i))
        p.db.x("UPDATE tasks SET status='running', updated=? WHERE id=?", (stale, tid))
        return tid
    bounced = stuck(True, False)                       # a GLM run bounced to Claude, which ran last
    for flag in _ARMS + ("glm_coord",):
        got = _flag_health(p, flag)
        assert got["failing"] == [] and got["reported"] == [f"#{bounced}"], flag
    assert any(f"#{bounced}" in s for s in _flag_health(p, "deslop")["failing"]), "no arm: project-wide"
    own = stuck(False, True)                           # its own GLM run was the last one
    for flag in _ARMS:
        got = _flag_health(p, flag)
        assert any(f"#{own}" in s for s in got["failing"]) and got["reported"] == [f"#{bounced}"], flag
    assert not _flag_health(p, "glm_coord")["failing"], "a coordinator-turn arm runs no task"
    lead = p.db.add_task("lead", "s", kind="code", status="queued")
    p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps({"status": "waiting"}), lead))
    p.db.set_kv(batchrun.KV + str(lead), {"id": f"b{lead}", "lead": lead, "members": [bounced, own]})
    for flag in _ARMS + ("glm_coord", "deslop", "coord_wake_settle"):
        got = _flag_health(p, flag)
        assert not any("no run and no change" in s for s in got["failing"]) and got["reported"] == [], \
            f"{flag}: a task waiting in a live batch is not stuck"


@pytest.mark.parametrize("flag", ["glm_code", "glm_work", "glm_device", "glm_coord", "eta_history", "deslop",
                                  "coord_wake_settle"])
def test_every_flag_s_health_watch_switches_off_only_its_own_project(env, flag):
    # cells: glm_code, glm_work, glm_device, glm_coord, eta_history, deslop, coord_wake_settle
    from ttp import rollout
    assert set(rollout.FLAGS) == {"glm_code", "glm_work", "glm_device", "glm_coord", "eta_history", "deslop",
                                  "coord_wake_settle"}, "a new flag gets a cell here"
    p, d = _watch(env)
    rollout.set_ring(p.name, "1")
    rollout.set_ring("other", "1")
    _ro_flag(flag, 1, 3)
    role = "coordinator" if flag == "glm_coord" else "worker"
    _ro_task_runs(p, 40, 4, 100, glm=False, role=role)
    _ro_task_runs(p, 40, 4, 100, glm=True, role=role)
    _ro_task_runs(p, 20, 20, 1, glm=True, role=role)
    if rollout.FLAGS[flag].get("shadow"):
        assert d.rollout_watch() == [] and rollout.on(flag, p.name), "a shadow flag changes no run: never switched off"
        return
    assert d.rollout_watch() == [flag]
    assert rollout.level(flag) == 1 and rollout.frozen(flag) is None
    assert not rollout.on(flag, p.name) and rollout.on(flag, "other")
    assert [r["kind"] for r in rollout.ledger(flag)][-1] == "project_rollback"


def test_the_coordinator_digest_shows_flag_states_and_a_rearm_since_the_last_wake(env, capsys):
    from ttp import rollout
    p = make(env)
    last_wake = time.time() - 5
    got = rollout.digest_lines(p, last_wake)
    assert got[0] == rollout.DIGEST_HEADER and len(got) == 2, "nothing changed: only the state line"
    assert "glm_device: off" in got[1] and "glm_code: off" in got[1]
    # The wave gate rolls the project back for the flag, then the user re-arms it from the terminal.
    rollout.stop_project("glm_device", p.name, "a step GLM took on its own")
    assert _roll(capsys, "arm", "glm_device", p.name, "--reason", "repro passed")[0] == 0
    _roll(capsys, "set", "glm_code", "2")
    got = rollout.digest_lines(p, last_wake)   # the next wake
    text = "\n".join(got)
    assert "glm_device rolled back for this project by daemon at" in text
    assert "glm_device re-armed for this project by the user at" in text
    assert "glm_code set to 2" in text
    assert not any("eta_history" in line for line in got[2:]), "older entries stay out"
    # The next wake again: only newer moves show.
    assert rollout.digest_lines(p, time.time()) == [got[0], got[1]], "its change lines age out"
    # The coordinator's digest itself carries the section.
    from ttp import coordinator as coord
    secs = dict(coord.digest_parts(p, {}, [], [])[0])
    assert "re-armed for this project" in secs["rollout"], "the section reaches the coordinator"


def test_a_task_held_only_for_a_release_flag_wakes_when_the_flag_is_armed(env, monkeypatch, capsys):
    from ttp import daemon as dmod, rollout
    from ttp.daemon import Daemon
    p = make(env)
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    _roll(capsys, "set", "glm_device", "2")   # the flag is on for the project until its own rollback
    rollout.stop_project("glm_device", p.name, "a step GLM took on its own")
    tid = p.db.add_task("GLM device work", "s", kind="work", origin="user")
    hand = {"status": "waiting", "summary": "held only while the glm_device flag is off for this project",
            "waiting_for": "the glm_device flag", "retry_after_s": 3600,
            "retry_when": "ttp rollout on glm_device"}
    p.db.update_task(tid, status="queued", not_before=time.time() + 3600, result=json.dumps(hand))

    def cycle():
        d.probe_waiting()
        for proc, *_ in list(d._probes.values()):
            proc.wait(30)
        d.probe_waiting()
        return p.db.task(tid)

    d = Daemon(p.base)
    task = cycle()
    assert task["not_before"] > time.time() and d._probe_rc[tid][0] == 1, "the flag is off: it stays held"
    assert _roll(capsys, "arm", "glm_device", p.name)[0] == 0   # the user re-arms, no note sent
    task = cycle()
    woke = json.loads(task["result"])
    assert woke.get("woke") == "probe passed" and task["not_before"] <= time.time(), "woken by the flag alone"
    assert task["status"] == "queued", "dispatched with no coordinator turn"


def test_rollout_on_for_a_project_that_does_not_exist_is_a_broken_probe(env, capsys, monkeypatch):
    """`ttp rollout on FLAG PROJECT` with a PROJECT that does not exist must not fall back to the
    current project's flag: a typo in the name is a broken probe (exit 2), not a wrong answer."""
    p = make(env)
    _roll(capsys, "set", "glm_device", "2")   # the flag is on, so a probe for it would pass
    monkeypatch.chdir(p.root)   # with no name the probe would read this project
    code, out = _roll(capsys, "on", "glm_device", "no-such-project")
    assert code == 2 and "no project no-such-project" in out, "a typo in the project name"
    code, out = _roll(capsys, "on", "glm_device", p.name)
    assert code == 0 and p.name in out, "the named project's flag is still read"


def test_arm_restores_the_level_the_flag_had_before_the_rollback(env, capsys):
    """`arm` alone puts a rolled-back flag back at the level the rollback took it from: one command,
    no `set` after it."""
    from ttp import rollout
    _roll(capsys, "ring", "canary", "1")
    _roll(capsys, "set", "glm_code", "1")
    rollout.rollback("glm_code", "failed runs up", "daemon:canary")
    assert rollout.level("glm_code") == rollout.OFF and rollout.frozen("glm_code")
    code, out = _roll(capsys, "arm", "glm_code")
    assert code == 0 and "re-armed at 1, its level before the rollback" in out
    assert rollout.level("glm_code") == 1 and rollout.frozen("glm_code") is None
    assert rollout.on("glm_code", "canary") and not rollout.on("glm_code", "everyone")
    last = rollout.ledger("glm_code")[-1]
    assert last["kind"] == "arm" and last["to"] == 1


def test_arm_leaves_in_force_a_hand_set_level_or_off_for_auto_without_waves(env, capsys):
    """The level `arm` leaves in force, printed and ledgered (so the digest's change line names it too):
    a level set by hand after the rollback wins over the restored one, and auto restored onto no waves
    leaves the flag off."""
    from ttp import rollout
    from ttp import project
    _roll(capsys, "set", "deslop", "1")
    rollout.rollback("deslop", "failed runs up", "daemon:x")
    project.set_account_setting("rollout.flags", {"deslop": 2})   # a hand set while frozen: it wins
    code, out = _roll(capsys, "arm", "deslop")
    assert code == 0 and "re-armed at 2, the level set by hand" in out, "the hand-set level, not the restored 1"
    assert rollout.level("deslop") == 2 and rollout.frozen("deslop") is None
    last = rollout.ledger("deslop")[-1]
    assert last["kind"] == "arm" and last["to"] == 2
    _roll(capsys, "waves", "glm_code", "alpha,beta")
    _roll(capsys, "set", "glm_code", "auto")
    rollout.rollback("glm_code", "failed runs up", "daemon:x")
    _roll(capsys, "waves", "glm_code", "clear")   # auto restored onto no waves would turn nothing on
    assert "re-armed (off)" in _roll(capsys, "arm", "glm_code")[1]
    last = rollout.ledger("glm_code")[-1]
    assert last["kind"] == "arm" and last["to"] == rollout.OFF
    assert rollout.level("glm_code") == rollout.OFF


def test_arm_with_a_project_restores_only_that_project(env, capsys):
    """`arm FLAG PROJECT` clears that project's own rollback and no other project's: the flag is on
    for it again, the others stay as they were."""
    from ttp import rollout
    p = make(env)
    _roll(capsys, "set", "glm_device", "2")
    assert rollout.stop_project("glm_device", "alpha", "a step GLM took on its own")
    assert rollout.stop_project("glm_device", p.name, "another step")
    code, out = _roll(capsys, "arm", "glm_device", "alpha")
    assert code == 0 and "re-armed in alpha" in out and "on for it again" in out
    assert rollout.on("glm_device", "alpha"), "the project is back at its pre-rollback state"
    assert not rollout.on("glm_device", p.name), "the other rolled-back project stays rolled back"
    assert p.name in rollout.rolled_back("glm_device") and "alpha" not in rollout.rolled_back("glm_device")


def _rearm_setup(env, monkeypatch):
    """A fix task landed on the push branch, and a second project that runs a release built from it:
    what a run's re-arm checks. Returns (project, target, fix task, landed head)."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    target = _second_project(env, "rearmed")
    fix = p.db.add_task("the fix", "s", kind="code", status="done", branch="ttp/t9-fix")
    _commit(repo, "fix.txt", "the fix\n")
    _git_out(repo, "push", "-q", "origin", "HEAD:proj")
    head = _git_out(repo, "rev-parse", "HEAD")
    now = time.time()
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, landed_sha, pushed_sha, created, updated) "
           "VALUES(?,?,?,?,?,?,?,?,?)", (fix, "ttp/t9-fix", head, "origin/proj", "landed", head, head, now, now))
    return p, target, fix, head, _git_out(p.root, "rev-parse", head + "^")


def _rearm_runs(target, status):
    """A run of `target` whose hand-off says `status`, as its runs folder holds it."""
    (target.runs / "7").mkdir(parents=True, exist_ok=True)
    res = target.runs / "7" / "result.json"
    res.write_text(json.dumps({"status": status, "summary": "the replay"}))
    # Finished after the rollback: a file's time comes from a coarser clock than time.time() and can read a
    # few milliseconds before a rollback recorded just ahead of it, so it is set rather than left to the write.
    after = time.time() + 1
    os.utime(res, (after, after))
    return "7"


def _rearm_daemon(target, commit, ticked=True):
    """The target's daemon runs `commit`: its heartbeat carries a verified receipt naming it, its
    daemon record names the live pid identity.probe looks for, and (`ticked`) it completed a tick."""
    from ttp import identity
    from ttp.project import hostname
    pid = os.getpid()
    (target.state / "heartbeat").write_text(json.dumps({"pid": pid, "receipt": {
        "pid": pid, "version": "0.0.1", "commit": commit, "provenance": "verified", "instance": "i1"}}))
    target.db.set_kv("daemon", {"pid": pid, "host": hostname()})
    tick = target.state / identity.TICK_FILE
    tick.write_text(json.dumps({"instance": "i1"})) if ticked else tick.unlink(missing_ok=True)


def test_a_run_rearm_is_refused_without_the_setting_the_evidence_or_a_deployed_fix(env, monkeypatch, capsys):
    """A run's `arm FLAG PROJECT` needs the account setting on, a fix that landed and is deployed, and
    a replay that passed; each missing piece refuses with its own reason and nothing is re-armed."""
    from ttp import project, rollout
    p, target, fix, head, older = _rearm_setup(env, monkeypatch)
    _roll(capsys, "set", "glm_device", "2")
    rollout.stop_project("glm_device", target.name, "a step GLM took on its own")
    monkeypatch.setenv("TTP_RUN_DIR", str(p.state / "runs" / "55"))
    monkeypatch.setenv("TTP_RUN_ID", "55")
    monkeypatch.setenv("TTP_TASK", "77")
    code, out = _roll(capsys, "arm", "glm_device", target.name, "--fix", str(fix), "--replay", _rearm_runs(target, "done"))
    assert code == 2 and "rollout.run_rearm is off" in out, "the setting is off by default"
    project.set_account_setting("rollout.run_rearm", True)
    code, out = _roll(capsys, "arm", "glm_device", target.name)
    assert code == 2 and "carries its evidence" in out, "no --fix and --replay"
    never = p.db.add_task("a change that never landed", "s", status="done")
    path = env["tmp"] / "replay.json"
    path.write_text(json.dumps({"status": "done", "summary": "the replay"}))
    code, out = _roll(capsys, "arm", "glm_device", target.name, "--fix", str(never), "--replay", "7")
    assert code == 2 and "is not on the push branch" in out, "the named fix task never landed"
    code, out = _roll(capsys, "arm", "glm_device", target.name, "--fix", str(fix), "--replay", "7")
    assert code == 2 and "cannot tell what" in out and "runs" in out, "no verified running receipt names what the target runs"
    _rearm_daemon(target, older)
    code, out = _roll(capsys, "arm", "glm_device", target.name, "--fix", str(fix), "--replay", "7")
    assert code == 2 and "which does not contain fix task" in out, "what the target runs holds no fix"
    _rearm_daemon(target, head)
    code, out = _roll(capsys, "arm", "glm_device", target.name, "--fix", str(fix), "--replay", "8")
    assert code == 2 and "names neither a result file nor a run" in out, "no such run of the target"
    old = time.time() - 3600
    os.utime(path, (old, old))
    code, out = _roll(capsys, "arm", "glm_device", target.name, "--fix", str(fix), "--replay", str(path))
    assert code == 2 and "finished before the rollback" in out, "a replay from before the rollback answers nothing"
    code, out = _roll(capsys, "arm", "glm_device", target.name, "--fix", str(fix),
                      "--replay", _rearm_runs(target, "failed"))
    assert code == 2 and "did not pass" in out, "a failing replay is no evidence"
    path.write_text("not a result record")
    code, out = _roll(capsys, "arm", "glm_device", target.name, "--fix", str(fix), "--replay", str(path))
    assert code == 2 and "is not a result record" in out, "an unreadable path is no evidence"
    code, out = _roll(capsys, "arm", "glm_device", "unknown-project", "--fix", str(fix), "--replay", "7")
    assert code == 2 and "is not rolled back" in out, "a healthy project has nothing to re-arm"
    assert target.name in rollout.rolled_back("glm_device"), "nothing was re-armed"


def test_a_run_rearm_reads_the_running_daemon_not_the_files_on_disk(env, monkeypatch, capsys):
    """A re-arm reads the fix's deployment from the target daemon's verified receipt, as `ttp
    deployed` does: files an upgrade merged but the daemon has not restarted onto do not count."""
    from ttp import project, rollout
    p, target, fix, head, older = _rearm_setup(env, monkeypatch)
    _roll(capsys, "set", "glm_device", "2")
    rollout.stop_project("glm_device", target.name, "a step GLM took on its own")
    project.set_account_setting("rollout.run_rearm", True)
    monkeypatch.setenv("TTP_RUN_DIR", str(p.state / "runs" / "55"))
    monkeypatch.setenv("TTP_RUN_ID", "55")
    monkeypatch.setenv("TTP_TASK", "77")
    (target.harness / "runtime" / "ttp" / "SOURCE_COMMIT").write_text(head)   # the files hold the fix
    _rearm_daemon(target, older)   # ... while the daemon still runs the release before it
    code, out = _roll(capsys, "arm", "glm_device", target.name, "--fix", str(fix),
                      "--replay", _rearm_runs(target, "done"))
    assert code == 2 and "does not contain fix task" in out, "the files cannot answer for the running daemon"
    _rearm_daemon(target, head, ticked=False)   # restarted onto the fix, no completed tick yet
    code, out = _roll(capsys, "arm", "glm_device", target.name, "--fix", str(fix),
                      "--replay", _rearm_runs(target, "done"))
    assert code == 2 and "has not completed a tick" in out, "a restart counts once its daemon completed a tick"


def test_a_run_re_arms_one_project_on_evidence_and_records_it_in_the_ledger(env, monkeypatch, capsys):
    """With the setting on and every check passing, a run's `arm FLAG PROJECT --fix --replay` re-arms
    that one project, records who armed it and the evidence in the ledger, and status shows it."""
    from ttp import project, rollout
    p, target, fix, head, older = _rearm_setup(env, monkeypatch)
    _roll(capsys, "set", "glm_device", "2")
    rollout.stop_project("glm_device", target.name, "a step GLM took on its own")
    assert not rollout.on("glm_device", target.name)
    project.set_account_setting("rollout.run_rearm", True)
    _rearm_daemon(target, head)
    monkeypatch.setenv("TTP_RUN_DIR", str(p.state / "runs" / "55"))
    monkeypatch.setenv("TTP_RUN_ID", "55")
    monkeypatch.setenv("TTP_TASK", "77")
    code, out = _roll(capsys, "arm", "glm_device", target.name, "--fix", str(fix),
                      "--replay", _rearm_runs(target, "done"), "--reason", "fix landed and its replay passed")
    assert code == 0 and "re-armed in " + target.name in out and "on for it again" in out
    assert rollout.on("glm_device", target.name) and target.name not in rollout.rolled_back("glm_device")
    rec = rollout.ledger("glm_device")[-1]
    assert (rec["op"], rec["project"], rec["by"]) == ("project_arm", target.name, "run")
    assert (rec["run"], rec["run_task"], rec["fix"]) == ("55", "77", fix)
    assert rec["deployed"] == head and rec["replay"] == "7" and rec["run_project"] == str(p.base)
    code, out = _roll(capsys, "status")
    assert code == 0 and f"last re-armed in {target.name} by a run: fix #{fix}" in out and "replay 7" in out
