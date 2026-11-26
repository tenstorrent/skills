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


# The org's real remaining (X-Quota-Remaining-llm_tokens), our own hourly cap and its self-lift.

_GQ_HDR = "X-Quota-Remaining-llm_tokens"
_GQ_USAGE = {"usage": {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 900}}}


def _gq_account(**kv):
    from ttp import project
    for k, v in kv.items():
        project.set_account_setting(f"providers.chat.{k}", v)


def _gq_cap_cfg(reserve=0.1):
    return {"providers": {"chat": {"endpoint": "cloud", "quota": {"size": _GQ_SIZE, "reserve": reserve}}}}


def test_glm_quota_header_reading_is_recorded_with_its_time_and_a_bad_or_missing_one_is_ignored(monkeypatch, tmp_path):
    from ttp import glmquota
    _gq_home(monkeypatch, tmp_path)
    now = _GQ_HOUR + 600
    assert glmquota.note("cloud", {_GQ_HDR: "73000000"}, now=now) == 73_000_000
    assert glmquota.note("cloud", {"x-quota-remaining-LLM_TOKENS": " 71000000 "}, now=now + 10) == 71_000_000
    for bad in ({}, None, {_GQ_HDR: "lots"}, {_GQ_HDR: "-5"}, {_GQ_HDR: ""}):
        assert glmquota.note("cloud", bad, now=now + 20) is None
    st = json.loads((tmp_path / "quota" / "cloud.json").read_text())
    assert st["remaining"] == {"v": 71_000_000, "at": now + 10}, "the newest good reading, with its time"
    assert glmquota.view(_GQ_CFG, now + 30)["remaining"] == 71_000_000
    assert glmquota.view(_GQ_CFG, _GQ_HOUR + 3600 + 30)["remaining"] is None, "a reading of the last UTC hour is stale"


def test_glm_quota_answer_counts_tokens_and_the_reading_in_one_write_and_never_raises(monkeypatch, tmp_path):
    from ttp import glmquota
    _gq_home(monkeypatch, tmp_path)
    now = _GQ_HOUR + 600
    glmquota.answer("cloud", {_GQ_HDR: "90000000"}, _GQ_USAGE, now=now)
    v = glmquota.view(_GQ_CFG, now)
    assert (v["used"], v["ours_hour"], v["remaining"]) == (1050, 1050, 90_000_000), "cached tokens at full weight"
    glmquota.answer("cloud", {}, {"choices": []}, now=now)           # no usage, no header: nothing
    glmquota.answer("cloud", None, {"usage": "x"}, now=now)           # junk usage
    glmquota.answer("", {_GQ_HDR: "5"}, _GQ_USAGE, now=now)           # no alias: nothing to key on
    glmquota.answer("cloud", object(), _GQ_USAGE, now=now)           # headers that are not a mapping
    assert glmquota.view(_GQ_CFG, now)["used"] == 1050 * 2
    assert glmquota.view(_GQ_CFG, now)["remaining"] == 90_000_000


def test_glm_quota_off_bridge_posts_take_no_project_setting_and_always_count_cached_tokens(monkeypatch, tmp_path):
    """The off-bridge posts (babysit, benches, probes) carry only the endpoint alias, never a project's
    providers.chat.quota: they record cached tokens at full weight even where a project sets count_cached
    false, and there is no switch a caller could forget to pass."""
    import inspect
    from ttp import glmquota
    _gq_home(monkeypatch, tmp_path)
    assert "count_cached" not in inspect.signature(glmquota.answer).parameters
    assert not hasattr(glmquota, "count_cached")
    off = {"providers": {"chat": {"endpoint": "cloud", "quota": {"size": _GQ_SIZE, "count_cached": False}}}}
    now = _GQ_HOUR + 600
    glmquota.answer("cloud", None, _GQ_USAGE, now=now)
    v = glmquota.view(off, now)
    assert (v["used"], v["ours_hour"]) == (1050, 1050), "cached tokens at full weight off the bridge"



def test_glm_quota_hourly_cap_counts_cached_in_full_runs_out_mid_hour_and_resumes_next_utc_hour(monkeypatch, tmp_path):
    from ttp import glmquota
    _gq_home(monkeypatch, tmp_path)
    cfg = _gq_cap_cfg()
    plain = {"kind": "work", "priority": 1, "spec": "Goal: fix the parser."}
    now = _GQ_HOUR + 1800
    glmquota.add("cloud", 20_000_000, now=now - 60, cached=19_000_000)
    assert glmquota.view(cfg, now)["cap"] == 0 and glmquota.paced(cfg, plain, "worker", now) == "", "no setting: no cap"
    _gq_account(quota_hourly_cap=50_000_000)
    v = glmquota.view(cfg, now)
    assert (v["cap"], v["ours_hour"], v["exhausted"]) == (50_000_000, 20_000_000, False), "mostly cache reads, still counted"
    glmquota.add("cloud", 24_000_000, now=now, cached=23_000_000)   # 44M of the 45M (50M less the 10% reserve)
    assert not glmquota.view(cfg, now)["exhausted"]
    glmquota.add("cloud", 2_000_000, now=now + 5, cached=1_900_000)
    v = glmquota.view(cfg, now + 10)
    assert v["exhausted"] and "our 50M tokens an hour" in v["why"], v
    assert glmquota.paced(cfg, plain, "worker", now + 10) == "paced"
    assert glmquota.paced(cfg, {"kind": "work", "priority": 5}, "coordinator", now + 10) == "paced", "a coordinator wake too"
    nxt = _GQ_HOUR + 3600 + 5
    assert glmquota.view(cfg, nxt)["ours_hour"] == 0 and glmquota.paced(cfg, plain, "worker", nxt) == "", \
        "GLM resumes at the next UTC hour"
    assert "50M" in glmquota.line(cfg, now + 10) and "cap" in glmquota.line(cfg, now + 10)
    _gq_account(quota_hourly_cap=0)
    assert glmquota.paced(cfg, plain, "worker", now + 10) == "", "0 clears the cap"


def test_glm_quota_paces_on_the_smaller_of_our_budget_and_the_orgs_remaining(monkeypatch, tmp_path):
    from ttp import glmquota
    _gq_home(monkeypatch, tmp_path)
    cfg = _gq_cap_cfg()
    _gq_account(quota_hourly_cap=50_000_000)
    plain = {"kind": "work", "priority": 1, "spec": "Goal: x"}
    now = _GQ_HOUR + 1800
    glmquota.add("cloud", 10_000_000, now=now - 60)
    glmquota.note("cloud", {_GQ_HDR: "60000000"}, now=now - 30)   # the org has plenty: our own cap rules
    v = glmquota.view(cfg, now)
    assert not v["exhausted"] and "our" not in v["why"] and v["remaining"] == 60_000_000
    glmquota.note("cloud", {_GQ_HDR: "9000000"}, now=now)         # only 9M left, the 10M reserve is gone: stop
    v = glmquota.view(cfg, now + 1)
    assert v["exhausted"] and "org's quota has 9.0M" in v["why"], v
    assert glmquota.paced(cfg, plain, "worker", now + 1) == "paced"
    glmquota.note("cloud", {_GQ_HDR: "40000000"}, now=now + 60)   # others eased off: 30M over the reserve for us
    v = glmquota.view(cfg, now + 61)
    assert not v["exhausted"] and glmquota.paced(cfg, plain, "worker", now + 61) == ""
    assert glmquota.paced(cfg, plain, "worker", _GQ_HOUR + 3600 + 1) == "", "last hour's reading says nothing of the new one"


def test_glm_quota_lift_needs_repeated_evidence_that_cached_tokens_stopped_counting(monkeypatch, tmp_path):
    import random
    from ttp import glmquota
    _gq_home(monkeypatch, tmp_path)
    cfg = _gq_cap_cfg()
    _gq_account(quota_hourly_cap=50_000_000)

    def run(key, weight, start, steps=330, cached_scale=1.0, late=False):
        rng, left, owed = random.Random(7), 80_000_000, 0
        glmquota.note(key, {_GQ_HDR: str(left)}, now=start)
        for i in range(1, steps + 1):
            t = start + 10 * i
            cached, fresh = int(rng.uniform(0, 1.2e6) * cached_scale), int(rng.uniform(0, 4e5))
            glmquota.add(key, cached + fresh, now=t - 1, cached=cached)
            charge = fresh + weight * cached
            if late:   # the charge shows in the next answer's reading, not this one's
                charge, owed = owed, charge
            if t % 3600 < 10:
                left = 80_000_000   # a new UTC hour: the quota refilled
            left -= int(10 * 28000 * rng.uniform(0.7, 1.3) + charge)   # others use ~28k tokens/s
            glmquota.note(key, {_GQ_HDR: str(left)}, now=t)
        return json.loads((tmp_path / "quota" / f"{key}.json").read_text())

    full = run("cloud", 1.0, _GQ_HOUR)      # cached tokens still count in full: never lifts
    assert not (full.get("lift") or {}).get("lifted") and (full["lift"].get("weight") or 0) > 0.7, full.get("lift")
    assert glmquota.view(cfg, _GQ_HOUR + 3500)["cap"] == 50_000_000
    free = run("free", 0.0, _GQ_HOUR)       # same use, cached tokens now free: lifts after 3 evaluations in a row
    assert free["lift"]["lifted"] and free["lift"]["votes"] >= glmquota.LIFT_VOTES and free["lift"]["weight"] < 0.2
    cfg_free = {"providers": {"chat": {"endpoint": "free", "quota": {"size": _GQ_SIZE}}}}
    v = glmquota.view(cfg_free, _GQ_HOUR + 3500)
    assert v["lifted"] and v["cap"] == 0 and "cap is lifted" in glmquota.line(cfg_free, _GQ_HOUR + 3500), v
    assert "lifted by itself" in glmquota.lift_notice("free", _GQ_HOUR + 3600), "one low notify's text"
    assert glmquota.lift_notice("free", _GQ_HOUR + 3601) == "", "told once"
    # Paced at the cap (about 40M an hour), one evaluation's rows hold too little cached traffic: they pile up
    # across evaluations instead of voting no, so the lift still comes.
    assert run("paced", 0.0, _GQ_HOUR, steps=1500, cached_scale=0.2)["lift"]["lifted"]
    # Charges one reading late, cached tokens still counting in full: the lagged fit sees them, no lift.
    assert not (run("late", 1.0, _GQ_HOUR, late=True).get("lift") or {}).get("lifted")
    # A run too short (not enough rows) or with too few cached tokens (no power) never votes yes.
    assert not (run("short", 0.0, _GQ_HOUR, steps=60).get("lift") or {}).get("lifted")
    assert not (run("light", 0.0, _GQ_HOUR, cached_scale=0.01).get("lift") or {}).get("lifted"), "no cached tokens, no proof"


def test_glm_quota_lift_vote_count_resets_on_a_failed_evaluation_and_modes_force_it_on_or_off(monkeypatch, tmp_path):
    from ttp import glmquota
    _gq_home(monkeypatch, tmp_path)
    import random

    def rows(w, n=60, t0=1000.0):   # drops: others' use (noise) + our uncached in full + cached at weight w
        rng, out = random.Random(3), []
        for i in range(n):
            unc, cached = rng.randint(100_000, 300_000), rng.randint(700_000, 1_100_000)
            out.append([t0 + 10 * i, 10.0, unc, cached, int(rng.uniform(0.2, 0.4) * 1e6 + unc + w * cached)])
        return out
    st = {"rows": rows(0.0), "lift": {"votes": 2, "at": 0}}
    glmquota._judge(st, 5000.0, mode="auto")
    assert st["lift"]["lifted"] and st["lift"]["votes"] == 3
    st = {"rows": rows(0.0), "lift": {"votes": 2, "at": 0}}
    glmquota._judge(st, 5000.0, mode="off")
    assert st["lift"] == {"votes": 2, "at": 0}, "off: never evaluates"
    st = {"rows": rows(1.0), "lift": {"votes": 2, "at": 0}}
    glmquota._judge(st, 5000.0, mode="auto")
    assert st["lift"]["votes"] == 0 and not st["lift"]["lifted"], "one failing evaluation starts over"
    st = {"rows": rows(0.0), "lift": {"votes": 0, "at": 4800.0}}
    glmquota._judge(st, 5000.0, mode="auto")
    assert st["lift"]["at"] == 4800.0, "at most one evaluation per LIFT_EVERY_S"
    _gq_account(quota_cap_lift="on", quota_hourly_cap=50_000_000)
    v = glmquota.view(_gq_cap_cfg(), _GQ_HOUR + 60)
    assert v["lifted"] and v["cap"] == 0, "on: lifted at once"
    _gq_account(quota_cap_lift="off")
    glmquota.add("cloud", 1, now=_GQ_HOUR)
    path = tmp_path / "quota" / "cloud.json"
    st = json.loads(path.read_text())
    path.write_text(json.dumps({**st, "lift": {"lifted": _GQ_HOUR, "votes": 3}}))
    v = glmquota.view(_gq_cap_cfg(), _GQ_HOUR + 60)
    assert not v["lifted"] and v["cap"] == 50_000_000, "off: the cap holds whatever was found"


def test_glm_quota_settings_are_account_keys_with_checked_values(monkeypatch, tmp_path):
    from ttp import cli, glmquota, project
    _gq_home(monkeypatch, tmp_path)
    assert glmquota.CAP_KEY in project.ACCOUNT_NESTED_KEYS and glmquota.LIFT_KEY in project.ACCOUNT_NESTED_KEYS
    assert glmquota.cap_setting() == 0 and glmquota.lift_mode() == "auto", "shipped default: no cap"
    for k, bad in ((glmquota.CAP_KEY, "lots"), (glmquota.CAP_KEY, -1), (glmquota.CAP_KEY, True),
                   (glmquota.LIFT_KEY, "maybe")):
        assert glmquota.check_setting(k, bad), (k, bad)
    for k, ok in ((glmquota.CAP_KEY, 50_000_000), (glmquota.CAP_KEY, 0), (glmquota.LIFT_KEY, "off")):
        assert glmquota.check_setting(k, ok) == ""
    project.set_account_setting(glmquota.CAP_KEY, 50_000_000)
    project.set_account_setting(glmquota.LIFT_KEY, "off")
    assert glmquota.cap_setting() == 50_000_000 and glmquota.lift_mode() == "off"


def test_glm_quota_bridge_records_the_header_on_each_successful_answer_and_not_on_a_429(mock_chat, monkeypatch, tmp_path):
    from ttp import chatbridge, glmquota
    _gq_home(monkeypatch, tmp_path)
    ok = {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}], **_GQ_USAGE}
    up = mock_chat((200, ok, {_GQ_HDR: "88000000"}), (200, ok), (429, _QUOTA_BODY, {_GQ_HDR: "0"}))
    now = time.time()
    with chatbridge.Bridge(up.url, _CB_KEY, "up-model", quota={"key": "cloud", "window_s": 3600}) as b:
        for _ in range(3):
            _post(b, {"max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
    st = json.loads((tmp_path / "quota" / "cloud.json").read_text())
    assert st["remaining"]["v"] == 88_000_000 and st["remaining"]["at"] >= now, "the 429's header is not a reading"
    assert st["hours"][str(int(now - now % 3600))][0] >= 2100 and st["hours"][str(int(now - now % 3600))][1] >= 1800


def test_glm_quota_off_run_posts_and_the_models_poll_land_in_the_quota_file(mock_chat, monkeypatch, tmp_path):
    from ttp import babysit, endpoint, glmquota, modelbench, toolprobe
    _gq_home(monkeypatch, tmp_path)
    ok = {"choices": [{"message": {"content": "wake"}, "finish_reason": "stop"}], **_GQ_USAGE}
    up = mock_chat((200, ok, {_GQ_HDR: "70000000"}), (200, ok, {_GQ_HDR: "69000000"}), (200, ok, {_GQ_HDR: "68000000"}))
    up.models, up.get_headers = ["m"], {_GQ_HDR: "67000000"}
    monkeypatch.setattr(endpoint, "credentials", lambda alias: endpoint.Credentials("k", up.url))
    babysit._post({"alias": "cloud", "base_url": "", "model": "m"}, [{"role": "user", "content": "x"}], 10)
    assert glmquota.view(_GQ_CFG)["used"] == 1050, "babysit's post"
    modelbench._post(up.url, "k", "m", "hi", alias="cloud")
    assert glmquota.view(_GQ_CFG)["used"] == 2100, "the model bench's post"
    toolprobe.endpoint_post("cloud")({"model": "m", "messages": []})
    assert glmquota.view(_GQ_CFG)["used"] == 3150, "the tool probe's post"
    assert glmquota.view(_GQ_CFG)["remaining"] == 68_000_000
    assert endpoint.list_models(up.url, "k", alias="cloud")[0]
    assert glmquota.view(_GQ_CFG)["remaining"] == 67_000_000 and glmquota.view(_GQ_CFG)["used"] == 3150, \
        "the /models poll gives a reading and no tokens"
    before = glmquota.view(_GQ_CFG)["remaining"]
    assert endpoint.list_models(up.url, "k")[0]
    assert glmquota.view(_GQ_CFG)["remaining"] == before, "no alias, no reading"
