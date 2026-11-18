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
    assert code == 2 and "off, 0, 1 or 2" in out
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
    _roll(capsys, "set", "push_isolation", "1")
    assert {f: f.read_bytes() for f in before} == before
    data = json.loads(project.account_settings_path().read_text())
    assert data["rollout"] == {"flags": {"push_isolation": 1}, "rings": {"1": ["someone"]}}
    assert oct(project.account_settings_path().stat().st_mode & 0o777) == "0o600"
    assert (project.HOME_DIR / rollout.LEDGER).exists()
    st = json.loads(_roll(capsys, "status", "--json")[1])
    assert st["flags"]["push_isolation"]["level"] == 1 and st["rings"] == {"1": ["someone"]}
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


def _ro_landed(p, took_h, cost, ago_h):
    tid = p.db.add_task("t", "s", kind="code")
    land = time.time() - ago_h * _RH
    p.db.x("UPDATE tasks SET created=?, spent_usd=? WHERE id=?", (land - took_h * _RH, cost, tid))
    p.db.x("INSERT INTO task_landings(marker, task, sha, target, ts) VALUES(?,?,?,?,?)", (f"m{tid}", tid, "a" * 40, "main", land))


def test_health_signals_fail_only_past_their_threshold_and_sample_minimum(env):
    from ttp import rollout_health as rh
    p = make(env)
    th, now = rh.thresholds({"thresholds": {"min_runs": 20}}), time.time() + 60
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


def test_health_flags_a_new_high_alert_in_the_flags_area_a_forward_revert_and_a_stuck_task(env):
    from ttp import postland, rollout, rollout_health as rh
    p = make(env)
    th, now = rh.thresholds(None), time.time() + 60
    since = now - 24 * _RH
    area = rollout.FLAGS["push_isolation"]["area"]
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
    assert any("1 forward revert" in s for s in rh.signals(p.db, since, now, th, area)["failing"])
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
    assert got("push_isolation") == ["new alert after_push_failed", "new alert push_tip_failing"]
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
