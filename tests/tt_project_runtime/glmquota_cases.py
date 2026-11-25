# GLM quota pacing (glmquota.py, providers.chat.quota): the endpoint's hourly token quota is counted per host,
# synced from its 429s, and spread over the window, so it does not run dry before the reset.

_GQ_SIZE = 100_000_000                  # the endpoint's hourly llm_tokens limit, as its 429 body names it
_GQ_HOUR = 1795593600.0                 # 2026-11-25 08:00 UTC, a window start
_GQ_CFG = {"providers": {"chat": {"endpoint": "cloud", "quota": {"size": _GQ_SIZE}}}}


def _gq_home(monkeypatch, tmp_path):
    from ttp import project
    monkeypatch.setattr(project, "HOME_DIR", tmp_path)


def test_glm_quota_pacing_over_a_simulated_day_spreads_each_window_and_sheds_low_priority_first(
        monkeypatch, tmp_path):
    from ttp import glmquota
    _gq_home(monkeypatch, tmp_path)
    # A fake provider: each minute 5 work runs start (priorities 1-5), each burning 500k tokens in that minute:
    # 150M tokens an hour of demand against a 100M quota, which unpaced runs dry at minute 40 of every hour.
    per_run, unpaced_dry = 500_000, None
    for minute in range(60):
        glmquota.add("unpaced", 5 * per_run, now=_GQ_HOUR + minute * 60)
        if unpaced_dry is None and glmquota.view(
                {"providers": {"chat": {"endpoint": "unpaced", "quota": {"size": _GQ_SIZE, "reserve": 0}}}},
                _GQ_HOUR + minute * 60 + 59)["exhausted"]:
            unpaced_dry = minute
    assert unpaced_dry == 39
    hours = []
    for hour in range(24):
        on_glm = {p: 0 for p in range(1, 6)}
        for minute in range(60):
            now = _GQ_HOUR + hour * 3600 + minute * 60
            for p in range(1, 6):
                if not glmquota.paced(_GQ_CFG, {"kind": "work", "priority": p}, "worker", now):
                    on_glm[p] += 1
                    glmquota.add("cloud", per_run, now=now + 30)
        end = glmquota.view(_GQ_CFG, _GQ_HOUR + hour * 3600 + 3599)
        assert not end["exhausted"] and end["used"] <= _GQ_SIZE * 0.95, (hour, end)   # inside the reserve
        hours.append((on_glm, end["used"]))
    for on_glm, used in hours:
        assert on_glm[1] == 60, "priority 1 never leaves GLM for pacing"
        assert on_glm[5] < on_glm[3] <= on_glm[1], on_glm
        assert used >= _GQ_SIZE * 0.85, "the quota is used, not hoarded"
    # 150M of demand an hour against 90M of budget: about 62% of the runs (4488 of 7200) stay on GLM, all day.
    assert 0.55 <= sum(sum(h[0].values()) for h in hours) / (24 * 300) <= 0.64


def test_glm_quota_short_keeps_judgement_and_conclusion_runs_off_glm_and_spent_moves_every_run(monkeypatch, tmp_path):
    from ttp import glmquota
    _gq_home(monkeypatch, tmp_path)
    now = _GQ_HOUR + 1800
    plain = {"kind": "work", "priority": 1, "spec": "Goal: fix the parser."}
    assert glmquota.paced(_GQ_CFG, plain, "worker", now) == ""
    glmquota.add("cloud", 48_000_000, now=now - 60)   # half way through, 3M (3%) ahead of an even 45M: short
    v = glmquota.view(_GQ_CFG, now)
    assert v["short"] and not v["exhausted"] and 0.02 < v["ahead"] < 0.05, v
    assert glmquota.paced(_GQ_CFG, plain, "worker", now) == "", "priority 1 stays while the quota is only short"
    for task, cls in (({"kind": "review", "priority": 1}, "worker"), ({"kind": "question", "priority": 1}, "worker"),
                      ({"kind": "plan", "priority": 1}, "worker"), ({"kind": "work", "priority": 1}, "coordinator"),
                      ({"kind": "code", "priority": 1, "spec": "Goal: x\nconclusion: the table of causes"}, "worker")):
        assert glmquota.paced(_GQ_CFG, task, cls, now) == "paced", task
    glmquota.add("cloud", 52_000_000, now=now)
    v = glmquota.view(_GQ_CFG, now)
    assert v["exhausted"] and glmquota.paced(_GQ_CFG, plain, "worker", now) == "paced"
    assert "spent" in glmquota.line(_GQ_CFG, now)
    # The next window starts fresh: back on GLM, and the line says so.
    nxt = _GQ_HOUR + 3600 + 900
    assert glmquota.view(_GQ_CFG, nxt)["used"] == 0 and glmquota.paced(_GQ_CFG, plain, "worker", nxt) == ""
    assert glmquota.line(_GQ_CFG, nxt, lambda t: "10:00") == \
        "GLM quota: 0.0M of 100M tokens used (resets 10:00); pacing on, on pace"


def test_glm_quota_429_numbers_set_size_used_and_reset_and_the_off_switch_turns_it_all_off(monkeypatch, tmp_path):
    from ttp import glmquota
    _gq_home(monkeypatch, tmp_path)
    body = json.dumps(_QUOTA_BODY)
    got = glmquota.parse(body)
    assert got == {"limit": 100_000_000, "used": 105_832_148, "reset": _QUOTA_END, "window_s": 3600}
    assert glmquota.parse("{}", {"X-RateLimit-Limit-Tokens": "500", "x-ratelimit-remaining-tokens": "120"}) == \
        {"limit": 500, "used": 380}
    cfg = {"providers": {"chat": {"endpoint": "cloud"}}}   # no size set: counted, never paced
    now = _QUOTA_END - 1200
    glmquota.add("cloud", 1_000_000, now=now - 60)
    v = glmquota.view(cfg, now)
    assert v["size"] == 0 and glmquota.paced(cfg, {"kind": "review"}, "worker", now) == ""
    assert "size unknown" in glmquota.line(cfg, now)
    glmquota.sync("cloud", got, now=now)   # the server's count is larger: it wins; its reset and size are taken
    v = glmquota.view(cfg, now)
    assert (v["size"], v["used"], v["reset"], v["size_from"], v["exhausted"]) == \
        (100_000_000, 105_832_148, _QUOTA_END, "429", True)
    assert glmquota.view(cfg, _QUOTA_END + 10)["used"] == 0, "the reset starts a new window"
    off = {"providers": {"chat": {"endpoint": "cloud", "quota": {"on": False}}}}
    assert glmquota.conf(off) == {} and glmquota.view(off, now) == {} and glmquota.line(off, now) == ""
    assert glmquota.paced(off, {"kind": "review"}, "worker", now) == ""
    assert glmquota.conf({"providers": {"chat": {}}}) == {}, "no endpoint: nothing to count"


def test_glm_quota_bridge_counts_each_answer_and_syncs_a_quota_429(mock_chat, monkeypatch, tmp_path):
    from ttp import chatbridge, glmquota
    _gq_home(monkeypatch, tmp_path)
    assert chatbridge.settings(_GQ_CFG, "light", "m")["quota"] == {"key": "cloud", "window_s": 3600, "count_cached": True}
    up = mock_chat((200, {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                                    "prompt_tokens_details": {"cached_tokens": 900}}}),
                   (429, _QUOTA_BODY))
    with chatbridge.Bridge(up.url, _CB_KEY, "up-model", quota={"key": "cloud", "window_s": 3600}) as b:
        _post(b, {"max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
        assert glmquota.view(_GQ_CFG)["used"] == 1050, "prompt (cached too) and completion tokens"
        _post(b, {"max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
        # The run's exit record keeps the 429's numbers only.
        assert b.stats()["quota"] == {"limit": 100_000_000, "used": 105_832_148, "reset": _QUOTA_END, "window_s": 3600}
    st = json.loads((tmp_path / "quota" / "cloud.json").read_text())
    assert (st["size"], st["used"], st["size_from"]) == (100_000_000, 105_832_148, "429")
    assert _CB_KEY not in json.dumps(st) and "Quota exceeded" not in json.dumps(st), "numbers only"


def test_glm_quota_short_starts_a_routed_run_on_core_with_the_fallback_model(env, monkeypatch, tmp_path):
    from ttp import glmquota, route
    from ttp.daemon import Daemon
    _gq_home(monkeypatch, tmp_path)
    p = make(env)
    _route_config(p, quota={"size": 1000})
    d = Daemon(p.base)
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="before")
    assert prov == "chat" and note[route.KEY]["fallback"] == "none"
    glmquota.add("placeholder", 5000)   # the window's quota is spent
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="spent")
    assert prov == d.cfg.get("core_provider", "claude") and note[route.KEY]["fallback"] == "paced", note
    assert "paced" in route.REASONS
    # ttp status and the web app show used, size, reset and whether pacing acts.
    from ttp import cli, web
    assert "GLM quota: 0.0M of 0M tokens used" in cli.status_text(p) and "spent" in cli.status_text(p)
    assert web.health(p, p.db)["spend"]["quota_line"].startswith("GLM quota: 0.0M of 0M tokens used")


def test_glm_quota_short_keeps_routine_coordinator_wakes_in_the_opus_session(env, monkeypatch, tmp_path):
    from ttp import coordroute, glmquota
    _gq_home(monkeypatch, tmp_path)
    p = _cr_db(env)
    cfg = {**_cr_cfg(), **_GQ_CFG}
    now = time.time()
    assert coordroute.classify(p.db, cfg, now=now) == (True, [])
    glmquota.add("cloud", _GQ_SIZE, now=now)   # spent this window
    routine, why = coordroute.classify(p.db, cfg, now=now)
    assert not routine and "GLM quota short" in why


def test_glm_quota_without_a_limit_paces_on_the_tokens_counted_when_a_quota_429_came(monkeypatch, tmp_path):
    from ttp import glmquota
    _gq_home(monkeypatch, tmp_path)
    cfg = {"providers": {"chat": {"endpoint": "cloud"}}}   # no size set, and the 429 names no limit
    bare = glmquota.parse('{"error": {"code": "quota_exceeded", "message": "Quota exceeded"}}')
    assert bare == {}
    glmquota.add("cloud", 30_000_000, now=_GQ_HOUR + 1200)
    glmquota.sync("cloud", bare, now=_GQ_HOUR + 1500, spent=True)
    v = glmquota.view(cfg, _GQ_HOUR + 1600)
    assert v["exhausted"] and v["size_from"] == "observed" and v["size"] == 30_000_000, v
    assert glmquota.paced(cfg, {"kind": "work", "priority": 1}, "worker", _GQ_HOUR + 1600) == "paced"
    assert "last quota 429 at" in glmquota.line(cfg, _GQ_HOUR + 1600)
    # The next window paces against that observation: a third of it gone by 20 minutes is on pace.
    nxt = _GQ_HOUR + 3600
    glmquota.add("cloud", 9_000_000, now=nxt + 1200)
    v = glmquota.view(cfg, nxt + 1200)
    assert not v["exhausted"] and not v["short"] and v["size"] == 30_000_000
    assert glmquota.line(cfg, nxt + 1200, lambda t: "T").startswith("GLM quota: 9.0M of 30M observed tokens used")
    glmquota.add("cloud", 6_000_000, now=nxt + 1300)   # 15M by minute 22: well ahead, judgement runs leave GLM
    assert glmquota.paced(cfg, {"kind": "review", "priority": 1}, "worker", nxt + 1320) == "paced"
    # A day later the observation has aged out: counted, not paced.
    assert glmquota.view(cfg, _GQ_HOUR + 1500 + glmquota.OBSERVED_KEEP_S + 60)["size"] == 0


def test_glm_quota_429_that_names_its_window_rolls_windows_on_it(monkeypatch, tmp_path):
    from ttp import glmquota
    _gq_home(monkeypatch, tmp_path)
    cfg = {"providers": {"chat": {"endpoint": "cloud", "quota": {"size": 60_000_000}}}}
    got = glmquota.parse('{"error": {"code": "quota_exceeded", "message": "per minute limit exceeded"}}')
    assert got == {"window_s": 60}
    glmquota.sync("cloud", got, now=_GQ_HOUR + 120, spent=True)
    glmquota.add("cloud", 1_000_000, now=_GQ_HOUR + 121)
    assert glmquota.view(cfg, _GQ_HOUR + 122)["used"] == 1_000_000
    assert glmquota.view(cfg, _GQ_HOUR + 181)["used"] == 0, "the 429's own minute window rolled"
    glmquota.add("cloud", 1_000_000, now=_GQ_HOUR + 240)   # minute 4: its own fresh window
    v = glmquota.view(cfg, _GQ_HOUR + 250)
    assert v["used"] == 1_000_000 and -0.16 < v["ahead"] < -0.13, (v, "ahead on the 429's minute window")
    assert glmquota.view(cfg, _GQ_HOUR + 301)["used"] == 0, "the second rollover on the 429's window"
