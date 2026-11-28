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
    # The endpoint's own quota headers count too (no reset given: the end of this UTC hour), and a shorter reset
    # never shortens a later mark (a daily 429 outlasts a header's hourly reset).
    modelquota.note("tt-model", {"x-quota-remaining-llm_tokens": "0"}, now=7200.5)
    assert modelquota.until("tt-model", now=7200.5) == 10800
    modelquota.note("tt-model", {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "5s"}, now=7200.5)
    assert modelquota.until("tt-model", now=7200.5) == 10800
    modelquota.note("neg-model", {"x-quota-remaining-llm_requests": "-3"}, now=7200.5)
    assert modelquota.until("neg-model", now=7200.5) == 10800


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


def _mq_sitter(env, monkeypatch, on=True, **kw):
    """The daemon's watcher (babysit.Babysitter) on a stub endpoint: every call answers skip; `asked` lists the models."""
    from ttp import babysit, modelquota
    p, d = _mq_setup(env, monkeypatch, on=on, **kw)
    asked, lines = [], []

    def post(s, messages, timeout):
        asked.append(s["model"])
        if modelquota.until(s["model"]):   # an empty bucket answers a quota 429, as _post raises it
            raise babysit.QuotaWait("HTTP 429", modelquota.until(s["model"]))
        return {"choices": [{"message": {"content": "skip"}}], "usage": {}}
    assert isinstance(d.jev, babysit.Babysitter)
    d.jev.post, d.jev.jev, d.jev.log, d.jev.lines = post, _BsJev(), lines.append, lines
    return p, d, d.jev, asked


def test_model_quota_an_empty_bucket_moves_a_watcher_call_to_a_listed_alternate_with_room(env, monkeypatch):
    from ttp import modelquota
    p, d, b, asked = _mq_sitter(env, monkeypatch)
    assert b.decide("state", _BS_Q, purpose="wake_gate") == {"needed": {"noul": 0.0}} and asked == [_RT_MODEL]
    modelquota.mark(_RT_MODEL)
    asked.clear()
    assert b.decide("state", _BS_Q, purpose="wake_gate") == {"needed": {"noul": 0.0}}
    assert asked == ["alt-a"] and b.last_decision["model"] == "alt-a" and b.jev.calls == 0, "no call into the empty bucket"
    assert b.decision("state", "wake?", purpose="wake_gate")["model"] == "alt-a" and b.pause.until == 0


def test_model_quota_a_watcher_with_no_alternate_room_waits_for_the_reset_not_a_cool_down(env, monkeypatch, mock_chat):
    from ttp import babysit, endpoint, modelquota
    p, d, b, asked = _mq_sitter(env, monkeypatch)
    end = time.time() + 2400
    for m in (_RT_MODEL, "alt-a", "alt-b"):
        modelquota.mark(m, {"reset": end - (m == "alt-b") * 600})
    assert b.decide("state", _BS_Q, purpose="wake_gate") == {"needed": {"noul": 0.5}}, "Jev decides meanwhile"
    assert asked == [] and b.pause.until == end - 600, "held to the earliest reset, not COOL_S"
    assert b.decision("state", "wake?") is None and asked == [] and b.jev.calls == 1, "no repeated 429s"
    # A real quota 429 marks the asked-for model's bucket and carries the 429's reset.
    up = mock_chat((429, _QUOTA_BODY))
    monkeypatch.setattr(endpoint, "credentials", lambda alias: ("k", up.url))
    s = {"alias": "placeholder", "base_url": "", "model": "q-model"}
    with pytest.raises(babysit.QuotaWait) as e:
        babysit._post(s, [{"role": "user", "content": "hi"}], 5)
    assert str(e.value) == "HTTP 429" and e.value.until == modelquota.until("q-model") > time.time()


def test_model_quota_watcher_moves_never_reach_person_facing_runs(env, monkeypatch):
    from ttp import modelquota, route
    p, d, b, asked = _mq_sitter(env, monkeypatch)
    modelquota.mark(_RT_MODEL)
    assert b.decision("state", "wake?", purpose="wake_gate")["model"] == "alt-a"
    for n, task in enumerate(({"reply_chat": "c1"}, {"kind": "question"})):
        prov, _, note = _route_dispatch(p, d, monkeypatch, title=f"person {n}", **task)
        assert prov != "chat" and "to" not in (note.get(route.KEY) or {}).get(modelquota.KEY, {}), (task, note)


def test_model_quota_watcher_skips_an_unmeasured_flash_and_asks_the_core_provider_last(env, monkeypatch):
    from ttp import modelquota
    p, d, b, asked = _mq_sitter(env, monkeypatch, alternates=("x-flash", "alt-b"), served=("x-flash", "alt-b"))
    modelquota.mark(_RT_MODEL)
    assert b.decision("state", "wake?")["model"] == "alt-b" and asked == ["alt-b"], "flash only once measured"
    modelquota.mark("alt-b")
    asked.clear()
    assert b.decide("state", _BS_Q) == {"needed": {"noul": 0.5}} and asked == [] and b.jev.calls == 1
    assert "no listed alternate has room" in b.lines[-1] and b.lines[-1].endswith("; Jev decides"), b.lines


def test_model_quota_flag_off_leaves_the_watcher_unchanged(env, monkeypatch):
    from ttp import babysit, modelquota
    p, d, b, asked = _mq_sitter(env, monkeypatch, on=False)
    modelquota.mark(_RT_MODEL, {"reset": time.time() + 2400})
    t0 = time.time()
    assert b.decide("state", _BS_Q) == {"needed": {"noul": 0.5}} and asked == [_RT_MODEL], "asked as before"
    assert t0 + babysit.COOL_S <= b.pause.until <= time.time() + babysit.COOL_S, "the 429 cools down as before"


def test_model_quota_coordinator_wake_triage_skips_an_empty_bucket_while_the_flag_is_on(env, monkeypatch):
    from ttp import babysit, modelquota, waketriage
    p, d = _mq_setup(env, monkeypatch)
    asked = []
    monkeypatch.setattr(babysit, "_post", lambda s, messages, timeout: (asked.append(s["model"]), {
        "choices": [{"message": {"content": "skip"}}], "usage": {}})[1])
    modelquota.mark(_RT_MODEL)
    for n, (buckets, want) in enumerate(((None, _RT_MODEL), (p.state, "alt-a"))):
        now = time.time()
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
               (now, "test", "task_done", "normal", f"done {n}", "queued"))
        ev = dict(p.db.one("SELECT * FROM events ORDER BY id DESC LIMIT 1"))
        assert waketriage.fold(p.db, d.cfg, [ev], now, buckets=buckets)[0] and asked[-1] == want, asked
    assert d._quota_on(), "the daemon passes the state folder only while the flag is on"


def test_model_quota_records_each_empty_window_and_counts_empty_minutes_per_hour(env, monkeypatch):
    from ttp import glmshare, modelquota
    _mq_home(env, monkeypatch)
    h = 1_800_000_000 // 3600 * 3600
    # An empty llm header (lowest of the hourly, daily and monthly windows, no reset) marks to the hour's end;
    # past it the bucket has room, and the next run's 429 sets the longer window's real reset.
    modelquota.note("w-model", {"x-quota-remaining-llm_tokens": "0"}, now=h + 1800)
    assert modelquota.until("w-model", now=h + 3601) == 0.0
    modelquota.mark("w-model", {"reset": h + 3 * 3600}, now=h + 3600 + 600)
    modelquota.mark("w-model", now=h + 3600 + 900)   # still empty: its window grows, no new one
    assert modelquota._state()["w-model"]["empty"] == [[h + 1800, h + 3600], [h + 4200, h + 3 * 3600]]
    got = modelquota.empty_minutes(h, h + 3 * 3600)
    assert got == {"w-model": {h: 30.0, h + 3600: 50.0, h + 7200: 60.0}}
    assert modelquota.empty_minutes(h + 3600, h + 3600 + 1800) == {"w-model": {h + 3600: 20.0}}, "clipped to the window"
    line = glmshare.empty_lines(got)
    assert line == ["quota bucket of w-model empty (min per UTC hour): 01-15 08h 30, 01-15 09h 50, 01-15 10h 60"], line
    # A window older than EMPTY_S is dropped at the next mark.
    modelquota.mark("w-model", now=h + modelquota.EMPTY_S + 4 * 3600)
    assert [w[0] for w in modelquota._state()["w-model"]["empty"]] == [h + modelquota.EMPTY_S + 4 * 3600]


def test_model_quota_a_worse_alternate_is_rolled_back_in_its_project_alone(env, monkeypatch):
    from ttp import modelquota, rollout, route
    p, d = _mq_setup(env, monkeypatch)   # on by the project's own setting, the rollout level off
    assert rollout.level(modelquota.FLAG) == "off"
    worse = {"state": "worse", "why": "alternate alt-a: worse than the Claude baseline", "n": 4, "model": "alt-a"}
    recs = rollout.wave_tick(time.time(), lambda *a: worse, {p.name: p})
    assert [(r["op"], r["project"], r["model"]) for r in recs] == [("project_rollback", p.name, "alt-a")]
    assert d._quota_on(), "the flag stays on in the project"
    assert modelquota.rolled_back_models(p.name) == {"alt-a"} and modelquota.rolled_back_models("other") == set()
    # Its coordinator and the change lines name the alternate and the key that restores it alone.
    rollout.project_events(p)
    ev = p.db.q("SELECT text FROM events WHERE kind='rollout_wave'")
    assert len(ev) == 1 and f"`ttp rollout arm {modelquota.FLAG} {p.name}:alt-a`" in ev[0]["text"], ev
    assert any(f"{modelquota.FLAG} rolled back alternate alt-a for this project" in x
               for x in rollout.change_lines(p.name, 0)), rollout.change_lines(p.name, 0)
    modelquota.mark(_RT_MODEL)
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="skips alt-a")
    assert prov == "chat" and note[route.KEY]["model"] == "alt-b", note
    said = rollout.arm(modelquota.FLAG, project=f"{p.name}:alt-a")
    assert said == f"{modelquota.FLAG} re-armed in {p.name}: alternate alt-a is used there again", said
    assert rollout.armed_at(f"{p.name}:alt-a", modelquota.FLAG) > 0 == rollout.armed_at(p.name, modelquota.FLAG)
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="alt-a again")
    assert note[route.KEY]["model"] == "alt-a", note
