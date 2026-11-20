# Live model demotion (modeldemote.py: chat model vs the next eligible model per class) tests of
# tests/test_tt_project_runtime.py, which runs this file in its namespace (see _include_cases there).
from __future__ import annotations

_DM_USER = {"TTP_RUN_ID": "", "TTP_RUN_DIR": "", "TTP_NOTE_RUN_DIR": "", "TTP_TASK_ID": "", "TTP_TASK": ""}


def _dm_task(p, model, bad=False, tier="light", wall=60, fallback="none", at=None):
    """One ended task whose first worker run ran on the chat provider with `model`."""
    from ttp import route
    at = time.time() - 3600 if at is None else at
    tid = p.db.add_task(f"{model} task", "Rename helpers.", kind="code", tier=tier, priority=3)
    p.db.update_task(tid, status="failed" if bad else "done")
    note = {route.KEY: {"tier": tier, "to": "chat", "model": model, "fallback": fallback, "wall_s": wall}}
    p.db.x("INSERT INTO runs(task,role,provider,model,status,started,note) VALUES(?,'worker','chat',?,'ok',?,?)",
           (tid, model, at, json.dumps(note)))
    return tid


def _dm_fill(p, model, bad, n, **kw):
    for i in range(n):
        _dm_task(p, model, bad=i < bad, **kw)


def _dm_project(env, monkeypatch, ranking=("m-a", "m-b")):
    from ttp import project
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "bench-home")   # no host bench record leaks in
    p = make(env)
    _route_config(p, model_ranking=list(ranking))
    return p


def test_model_demote_needs_min_samples_and_the_margin(env, monkeypatch):
    from ttp import modeldemote
    p = _dm_project(env, monkeypatch)
    _dm_fill(p, "m-a", 5, 7)   # 7 samples: under min_samples 8
    _dm_fill(p, "m-b", 0, 8)
    assert modeldemote.check(p, p.config()) == [], "too few samples: no action"
    _dm_fill(p, "m-a", 0, 1)   # 5/8 bad against 0/8, but within a 70-point margin
    p.set_config("providers.chat.demote", {"margin_pts": 70})
    assert modeldemote.check(p, p.config()) == []
    assert not modeldemote.record(p.db, "m-a", "light")


def test_model_demote_past_the_margin_demotes_alerts_once_and_routes_the_next_model(env, monkeypatch):
    from ttp import modeldemote, modelrank, route
    from ttp.daemon import Daemon
    p = _dm_project(env, monkeypatch)
    _dm_fill(p, "m-a", 4, 8)    # 50% bad
    _dm_fill(p, "m-b", 1, 8)    # 12.5% bad: 37.5 points better
    _dm_fill(p, "m-a", 0, 8, tier="standard")   # another class stays open
    _dm_fill(p, "m-b", 0, 8, tier="standard")
    d = Daemon(p.base)
    alerts = []
    monkeypatch.setattr(d, "alert", lambda key, text, **k: alerts.append((key, text, k.get("severity"))))
    d.trip_model_routing()
    assert [(k, s) for k, _, s in alerts] == [("model-demote:m-a|light", "low")]
    assert "4/8 against m-b's 1/8" in alerts[0][1]
    rec = modeldemote.record(p.db, "m-a", "light")
    assert rec["state"] == "demoted" and rec["by"]["by"] == "harness" and rec["peer"] == "m-b"
    now = time.time()
    route.write_served(p.state, "chat", True, "HTTP 200", ["m-a", "m-b"], now)
    cfg = p.config()
    assert modelrank.pick(p.db, cfg, p.state, "m-a", now, "light") == ("m-b", "demoted for light")
    assert modelrank.pick(p.db, cfg, p.state, "m-a", now, "standard") is None, "up for its other class"
    assert modelrank.better(p.db, cfg, p.state, "m-b", now, True, "light") != "m-a"
    lines = modeldemote.digest_lines(p.db, now - 7200, now)
    assert len(lines) == 1 and lines[0].startswith("- demote m-a (light, by harness)")
    assert modeldemote.digest_lines(p.db, now + 1, now + 2) == [], "once: only since the last coordinator turn"
    d._model_trip_checked = 0.0
    d.trip_model_routing()   # already demoted: no second alert
    assert len(alerts) == 1


def test_model_demote_ends_only_at_a_newer_passing_bench(env, monkeypatch):
    from ttp import modelbench, modeldemote
    p = _dm_project(env, monkeypatch)
    _dm_fill(p, "m-a", 4, 8)
    _dm_fill(p, "m-b", 0, 8)
    now = time.time()
    assert [a["action"] for a in modeldemote.check(p, p.config(), now)] == ["demote"]
    assert modeldemote.demoted(p.db, "m-a", "light", now + 60)
    path = modelbench.home() / modelbench.MODELS
    modelbench._write(path, {"m-a": {"at": now - 60, "score": 1.0, "weak": False}})   # older bench: no change
    assert modeldemote.demoted(p.db, "m-a", "light", now + 60)
    modelbench._write(path, {"m-a": {"at": now + 30, "score": 1.0, "weak": False}})
    assert not modeldemote.demoted(p.db, "m-a", "light", now + 60), "a newer passing bench ends it"
    assert modeldemote.check(p, p.config(), now + 60) == [], "the old samples do not demote it again"


def test_model_demote_a_slow_model_in_a_fast_class(env, monkeypatch):
    from ttp import modeldemote
    p = _dm_project(env, monkeypatch)
    _dm_fill(p, "m-a", 0, 8, tier="babysit", wall=50)
    _dm_fill(p, "m-b", 0, 8, tier="babysit", wall=20)
    _dm_fill(p, "m-a", 0, 8, wall=50)   # light is no fast class: speed alone does not demote
    _dm_fill(p, "m-b", 0, 8, wall=20)
    got = modeldemote.check(p, p.config())
    assert [(a["model"], a["class"]) for a in got] == [("m-a", "babysit")]
    assert "median 50s against m-b's 20s" in got[0]["why"]


def test_model_park_on_its_own_faults_and_a_bench_under_the_floor(env, monkeypatch):
    from ttp import modelbench, modeldemote, modelrank
    p = _dm_project(env, monkeypatch, ranking=("m-a", "m-b", "m-c"))
    for i in range(8):
        _dm_task(p, "m-a", fallback="malformed" if i < 2 else "none")   # 2/8 = 25% > 20%
    _dm_fill(p, "m-b", 0, 8)
    now = time.time()
    modelbench._write(modelbench.home() / modelbench.MODELS, {"m-b": {"at": now - 600, "score": 1.0},
                                                                "m-c": {"at": now - 600, "score": 0.4}})
    got = modeldemote.check(p, p.config(), now)
    assert {(a["model"], a["action"]) for a in got} == {("m-a", "park"), ("m-c", "park")}
    assert modelrank.parked(p.db, "m-a", now) and modelrank.parked(p.db, "m-c", now)
    assert modeldemote.check(p, p.config(), now + 60) == [], "parked once"


def test_ttp_model_records_the_setter_and_the_user_word_stands(env, monkeypatch):
    from ttp import configwho, modeldemote, modelrank
    p = _dm_project(env, monkeypatch)
    r = _ttp_run(p, "model", "demote", "m-a", "--class", "light", "--why", "slow today", env=_DM_USER)
    assert r.returncode == 0, r.stderr
    rec = modeldemote.record(p.db, "m-a", "light")
    assert rec["state"] == "demoted" and configwho.role(rec["by"]) == "user" and rec["why"] == "slow today"
    coord = {**_DM_USER, "TTP_RUN_ID": "77"}
    r = _ttp_run(p, "model", "promote", "m-a", "--class", "light", env=coord)
    assert r.returncode != 0 and "only the user changes that" in r.stderr
    assert modeldemote.record(p.db, "m-a", "light")["state"] == "demoted"
    assert _ttp_run(p, "model", "promote", "m-a", "--class", "light", env=_DM_USER).returncode == 0
    _dm_fill(p, "m-a", 4, 8, at=time.time())
    _dm_fill(p, "m-b", 0, 8, at=time.time())
    assert modeldemote.check(p, p.config(), time.time() + 1) == [], "the user's promote holds the detector off"
    r = _ttp_run(p, "model", "park", "m-b", env=coord)
    assert r.returncode == 0, r.stderr
    assert configwho.role(p.db.kv(modelrank.PARKED + "m-b")["by"]) == "coordinator"
    assert _ttp_run(p, "model", "park", "m-b", "--class", "light", env=_DM_USER).returncode == 2
    assert _ttp_run(p, "model", "unpark", "m-b", env=_DM_USER).returncode == 0
    assert not p.db.kv(modelrank.PARKED + "m-b")
    assert "only the user" in _ttp_run(p, "model", "park", "m-b", env=coord).stderr
    out = _ttp_run(p, "model", env=_DM_USER).stdout
    assert "m-a|light: promoted by user" in out


def test_a_parked_model_is_out_of_the_daily_bench_canary(env, monkeypatch):
    from ttp import configwho, modelbench, modeldemote, modelrank
    p = _dm_project(env, monkeypatch)
    now = time.time()
    assert modelbench.arms(["m-a", "m-b"], ["m-a", "m-b"], modelrank.parked_models(p.db), now) == \
        {"m-a": "daily", "m-b": "daily"}
    assert modeldemote.set_state(p.db, "m-a", modeldemote.ANY, "park", configwho.by("user", via="cli"), now) == ""
    assert modelbench.arms(["m-a", "m-b"], ["m-a", "m-b"], modelrank.parked_models(p.db), now + 60) == \
        {"m-b": "daily"}, "parked: weekly recheck only"
    assert modelbench.arms(["m-a", "m-b"], ["m-a", "m-b"], modelrank.parked_models(p.db), now + 8 * 86400) == \
        {"m-b": "daily", "m-a": "weekly"}
