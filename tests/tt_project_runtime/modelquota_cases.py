# Per-model quota buckets (modelquota.py, release flag glm_quota_alt): an empty bucket is never dispatched to
# before its reset; a routed run takes the first listed alternate with room, else the core provider.


def _mq_home(env, monkeypatch):
    from ttp import project
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "mq-home")
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    return project.HOME_DIR


def _mq_alternates(home, models):
    from ttp import cli, modelquota
    cli.main(["config", "--account", modelquota.ALT_KEY, json.dumps(list(models))])
    assert json.loads((home / "settings.json").read_text())["providers"]["chat"]["quota_alternates"] == list(models)


def _mq_setup(env, monkeypatch, alternates=("alt-a", "alt-b"), served=("alt-a", "alt-b"), on=True):
    """A routed light tier on the chat endpoint, the flag on by the project's own key, `served` listed."""
    from ttp import route
    from ttp.daemon import Daemon
    home = _mq_home(env, monkeypatch)
    _mq_alternates(home, alternates)
    p = make(env)
    _route_config(p)
    if on:
        p.set_config("route.glm_work", {"quota_alt": True})
    d = Daemon(p.base)
    d._load_config()
    route.write_served(p.state, "chat", True, "HTTP 200", [_RT_MODEL, *served], time.time())
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    monkeypatch.setattr(d, "alert", lambda *a, **k: None)
    return p, d


def test_model_quota_bucket_is_set_by_a_429_or_an_empty_header_and_clears_at_its_reset(env, monkeypatch, mock_chat):
    from ttp import chatbridge, modelquota
    _mq_home(env, monkeypatch)
    ok = {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
          "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    up = mock_chat((200, ok, {"x-ratelimit-remaining-tokens": "5"}),
                   (200, ok, {"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "1m30s"}))
    with chatbridge.Bridge(up.url, _CB_KEY, "hdr-model") as b:
        _post(b, {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
        assert modelquota.until("hdr-model") == 0.0, "tokens left: room"
        t0 = time.time()
        _post(b, {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
    t = modelquota.until("hdr-model")
    assert t0 + 85 < t < time.time() + 95, t
    assert modelquota.until("hdr-model", now=t + 1) == 0.0, "past its reset the bucket has room again"
    # A quota 429 (its resets_at already past here) marks the asked-for model to the end of this UTC hour.
    up = mock_chat((429, _QUOTA_BODY))
    with chatbridge.Bridge(up.url, _CB_KEY, "q-model") as b:
        _post(b, {"max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
    st = json.loads((env["tmp"] / "mq-home" / "quota" / "_models.json").read_text())
    assert st["q-model"]["until"] == (int(st["q-model"]["at"]) // 3600 + 1) * 3600
    assert (st["q-model"]["used"], st["q-model"]["limit"]) == (105_832_148, 100_000_000)
    assert _CB_KEY not in json.dumps(st) and "hdr-model" in st and "up-model" not in st
    # A later reset in the 429 is kept; header resets read as seconds, an epoch or a duration.
    modelquota.mark("r-model", {"reset": 2e10}, now=1e9)
    assert modelquota.until("r-model", now=1e9 + 1) == 2e10
    assert modelquota._reset({"x-ratelimit-reset": "30"}, 1e9) == 1e9 + 30
    assert modelquota._reset({"x-ratelimit-reset": "1900000000"}, 1e9) == 1.9e9
    assert modelquota._reset({"x-ratelimit-reset-tokens": "1h2m3s"}, 0) == 3723
    assert modelquota._reset({"x-ratelimit-reset-tokens": "6ms"}, 0) == 0.006
    assert modelquota._reset({}, 7200.5) == 10800
    # Requests at 0 reads the requests reset, not the tokens one.
    modelquota.note("rq-model", {"x-ratelimit-remaining-tokens": "9", "x-ratelimit-reset-tokens": "5s",
                                 "x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "2m"}, now=1e9)
    assert modelquota.until("rq-model", now=1e9) == 1e9 + 120


def test_model_quota_flag_off_by_default_and_routing_unchanged_while_off(env, monkeypatch):
    from ttp import modelquota, rollout, route
    assert modelquota.FLAG in rollout.FLAGS and rollout.level(modelquota.FLAG) == "off"
    assert rollout.FLAGS[modelquota.FLAG]["key"] == "route.glm_work.quota_alt"
    p, d = _mq_setup(env, monkeypatch, on=False)
    assert modelquota.alternates() == ["alt-a", "alt-b"]
    modelquota.mark(_RT_MODEL)
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="off")
    assert prov == "chat" and note[route.KEY]["model"] == _RT_MODEL and modelquota.KEY not in note[route.KEY]


def test_model_quota_empty_bucket_takes_the_first_served_alternate_with_room(env, monkeypatch):
    from ttp import modelpick, modelquota, route
    p, d = _mq_setup(env, monkeypatch)
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="room")
    assert prov == "chat" and note[route.KEY]["model"] == _RT_MODEL and modelquota.KEY not in note[route.KEY]
    modelquota.mark(_RT_MODEL)
    modelquota.mark("alt-a")   # the first alternate is empty too: the second one
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="empty", kind="code")
    q = note[route.KEY][modelquota.KEY]
    assert prov == "chat" and note[route.KEY]["model"] == "alt-b" and note[route.KEY]["fallback"] == "none"
    assert (q["from"], q["to"], q["n"]) == (_RT_MODEL, "alt-b", 2) and q["until"] > time.time()
    assert modelpick.run_model(note, "chat", _RT_MODEL, "") == ("alt-b", ""), "the run starts on the alternate"
    assert q["why"].startswith(f"{_RT_MODEL} quota bucket empty until ") and q["why"].endswith(": alternate 2, alt-b")
    # An alternate the endpoint does not serve is skipped; none with room: the core provider, fallback quota.
    route.write_served(p.state, "chat", True, "HTTP 200", [_RT_MODEL, "alt-a"], time.time())
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="none left")
    assert prov != "chat" and note[route.KEY]["fallback"] == "quota" and "quota" in route.REASONS
    assert note[route.KEY][modelquota.KEY]["why"].endswith(": no alternate has room, fallback to the core provider")


def test_model_quota_never_puts_judgement_or_person_facing_runs_on_an_alternate(env, monkeypatch):
    from ttp import modelquota, route
    p, d = _mq_setup(env, monkeypatch)
    modelquota.mark(_RT_MODEL)
    for n, task in enumerate(({"kind": "review"}, {"kind": "plan"}, {"kind": "question"}, {"reply_chat": "c1"},
                              {"spec": "Goal: x\nconclusion: the verdict"})):
        prov, _, note = _route_dispatch(p, d, monkeypatch, title=f"judgement {n}", **task)
        assert prov != "chat" and "to" not in (note.get(route.KEY) or {}).get(modelquota.KEY, {}), (task, note)
    assert not modelquota.eligible({"kind": "work"}, "coordinator") and modelquota.eligible({"kind": "work"}, "worker")


def test_model_quota_takes_a_flash_alternate_only_under_five_percent_refusals(env, monkeypatch):
    from ttp import modelquota, route
    p, d = _mq_setup(env, monkeypatch, alternates=("x-flash",), served=("x-flash",))
    modelquota.mark(_RT_MODEL)
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="unmeasured")
    assert prov != "chat" and note[route.KEY]["fallback"] == "quota", "too few calls to know its refusals"
    modelquota.count("x-flash", 100, 5)   # 5%: not under
    assert modelquota.room(p.db, d.cfg, p.state, _RT_MODEL, time.time()) is None
    modelquota.count("x-flash", 100, 4)   # 9 of 200
    assert modelquota.room(p.db, d.cfg, p.state, _RT_MODEL, time.time()) == (1, "x-flash")
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="measured")
    assert prov == "chat" and note[route.KEY]["model"] == "x-flash"
    # Counts start over after a week.
    modelquota.count("x-flash", 1, 1, now=time.time() + modelquota.COUNT_S + 1)
    assert modelquota._state()["x-flash"]["calls"]["n"] == 1


def test_model_quota_a_chat_model_pin_waits_while_its_bucket_is_empty(env, monkeypatch):
    from ttp import modelquota
    p, d = _mq_setup(env, monkeypatch)
    now = time.time()
    assert d._required_model_down({"model": "pinned-m"}, "chat", now) == ""
    modelquota.mark("pinned-m", now=now)
    assert "pinned-m's quota bucket on chat is empty until" in d._required_model_down({"model": "pinned-m"}, "chat", now)
    assert d._required_model_down({"model": "pinned-m"}, "claude", now) == ""
    assert d._required_model_down({"model": "pinned-m"}, "chat", modelquota.until("pinned-m") + 1) == ""
    # An unpinned run on chat starts on its tier's model: it waits while that bucket is empty too.
    assert d._required_model_down({"tier": "light"}, "chat", now) == ""
    modelquota.mark(_RT_MODEL, now=now)
    assert f"{_RT_MODEL}'s quota bucket on chat is empty until" in d._required_model_down({"tier": "light"}, "chat", now)


def test_model_quota_stop_with_an_alternate_free_neither_pauses_nor_cools_the_endpoint(env, monkeypatch):
    from ttp import modelquota, route
    p, d = _mq_setup(env, monkeypatch)
    tid = p.db.add_task("build", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    end = time.time() + 2400
    modelquota.mark(_RT_MODEL, {"reset": end})   # the bridge marks it on the 429
    run, task = _route_finish(p, d, tid, {"stopped": "quota", "bridge": {
        "last_fault": "rate_limit", "quota_until": end, "upstream": {"status": 429, "code": "quota_exceeded"}}})
    assert run["status"] == "limit" and task["status"] == "queued" and route.of(run["note"])["fallback"] == "quota"
    assert p.db.kv("limited:chat") is None and p.db.kv(route.COOL + "chat") is None
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="next")
    assert prov == "chat" and note[route.KEY]["model"] == "alt-a"
    # No alternate with room: the provider's pause holds chat to the window's end, as before.
    modelquota.mark("alt-a", {"reset": end})
    modelquota.mark("alt-b", {"reset": end})
    tid = p.db.add_task("build 2", "", kind="work", tier="light", priority=3, budget_usd=8.0, labels=[])
    _route_finish(p, d, tid, {"stopped": "quota", "bridge": {"last_fault": "rate_limit", "quota_until": end}})
    assert p.db.kv("limited:chat")["until"] == end
