# Model pick (modelpick: Jev use 'model') tests of tests/test_tt_project_runtime.py, which runs this
# file in its namespace (see _include_cases there). Add new model pick tests here.
from __future__ import annotations

_MP_URL, _MP_MODEL = "https://example.invalid/v1", "example-org/routed-model"


def _mp_config(p, enabled=True, **extra):
    p.set_config("providers.chat", {"endpoint": "placeholder", "base_url": _MP_URL, "tiers": {
        "light": {"model": "example-org/light-model", "route": True}},
        "dispatch": {"enabled": enabled, "min_p": 0.6, "options": {
            "routed-top": {"provider": "chat", "model": _MP_MODEL, "effort": "max"},
            "core": {"provider": p.config().get("core_provider") or "claude"}}, **extra}})


class _ModelJev:
    def __init__(self, choice="routed-top", p=0.9, cost=0.0003, fail=None):
        self.choice, self.p, self.last_cost, self.fail, self.states = choice, p, cost, fail, []

    def enabled(self):
        return True

    def decide(self, state, questions, purpose="decide", timeout=20.0):
        if self.fail:
            raise self.fail
        assert purpose == "model" and questions["model"]["type"] == "choice"
        # The live API refuses a choice without `criteria` (HTTP 422); it answers with this shape.
        assert set(questions["model"]["criteria"]) == {"routed-top", "core"}
        assert all(isinstance(v, str) and v for v in questions["model"]["criteria"].values())
        self.states.append(state)
        rest = {k: round(1.0 - self.p, 6) for k in questions["model"]["criteria"] if k != self.choice}
        return {"model": {"type": "choice", "choice": self.choice, "confidence": self.p,
                          "probabilities": {self.choice: self.p, **rest}}}


def _mp_dispatch(p, d, monkeypatch, title="Tidy the notes module", spec="Rename helpers in notes.py.", **task):
    """Dispatch one new standard task; returns (task id, provider, tier, note) of its start."""
    from ttp import budget as bud
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, prompt, provider, tier, cwd, **k: started.append(
        (provider, tier, k.get("note") or {})) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.root), None))
    for prov in (d.cfg.get("core_provider") or "claude", "chat"):
        d.gates[prov] = bud.Gate(prov, regime="windows", max_parallel=6)
    kw = {"kind": "code", "tier": "standard", "priority": 3, "budget_usd": 8.0, **task}
    tid = p.db.add_task(title, spec, **kw)
    d.dispatch()
    return (tid, *started[-1])


def test_model_pick_rules_send_every_clear_case_to_core_and_leave_routine_work_open(env, monkeypatch):
    from ttp import effort, modelpick
    from ttp.project import DEFAULT_CONFIG
    p = make(env)
    assert not modelpick.enabled(DEFAULT_CONFIG), "shipped default: dispatch off"
    assert modelpick.pick(p.db, p.config(), {"id": 1, "kind": "code"}, "standard", jev=_ModelJev()) is None
    _mp_config(p)
    cfg = p.config()
    assert modelpick.enabled(cfg) and modelpick.core_key(cfg) == "core"

    def task(**kw):
        tid = p.db.add_task(kw.pop("title", "Tidy notes"), kw.pop("spec", "Rename helpers."),
                            **{"kind": "code", "tier": "standard", "priority": 3, **kw})
        return p.db.task(tid)
    t = task()
    assert modelpick.rules(p.db, cfg, t, "standard") == ""
    assert modelpick.rules(p.db, cfg, task(kind="work"), "standard") == ""
    assert modelpick.rules(p.db, cfg, t, "light") is None, "the light tier follows its own route"
    assert modelpick.rules(p.db, cfg, task(model="opus"), "standard") is None, "a pin keeps its model"
    assert modelpick.rules(p.db, cfg, task(kind="review"), "standard") == "kind"
    assert modelpick.rules(p.db, cfg, task(kind="question"), "standard") == "kind"
    assert modelpick.rules(p.db, cfg, t, "deep") == "tier"
    assert modelpick.rules(p.db, cfg, {**t, "attempts": 1}, "standard") == "retry"
    assert modelpick.rules(p.db, cfg, task(priority=1), "standard") == "urgent"
    assert modelpick.rules(p.db, cfg, task(labels=["untrusted_input"]), "standard") == "untrusted"
    for spec in ("Optimize the matmul kernel.", "Improve throughput of the loader.", "Redesign the architecture.",
                 "Hard-debug the hang.", "Find the root cause of the deadlock."):
        assert modelpick.rules(p.db, cfg, task(spec=spec), "standard") == "hard", spec
    assert modelpick.rules(p.db, cfg, task(spec="Tidy.\nconclusion: tell the user"), "standard") == "conclusion"
    monkeypatch.setattr(effort, "device_task", lambda t: True)
    assert modelpick.rules(p.db, cfg, t, "standard") == "device"


def test_model_pick_jev_routes_open_work_logs_and_settles_it(env, monkeypatch):
    from ttp import jevuse, modelpick, route
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p = make(env)
    _mp_config(p)
    now = time.time()
    for cost in (2.0, 4.0):   # the stratum's core runs: code tasks at the standard tier's model
        old = p.db.add_task("old", "s", kind="code", tier="standard")
        core = p.config().get("core_provider") or "claude"
        model = ((p.config()["providers"].get(core) or {}).get("tiers", {}).get("standard") or {}).get("model") or ""
        p.db.x("INSERT INTO runs(task,role,provider,model,status,started,ended,cost_usd) "
               "VALUES(?,'worker',?,?,'ok',?,?,?)", (old, core, model, now - 3600, now - 3500, cost))
    d = Daemon(p.base)
    d.jev = _ModelJev()
    secret = "sk-placeholder-not-a-real-key-123456"
    monkeypatch.setattr("ttp.redact.pairs", lambda: [(secret, "[secret]")])
    tid, prov, tier, note = _mp_dispatch(p, d, monkeypatch, spec=f"Rename helpers in a/b/notes.py. {secret} " + "x" * 5000)
    assert (prov, tier) == ("chat", "standard")
    assert note[route.KEY]["model"] == _MP_MODEL and note[route.KEY]["model_pick"] is True
    assert note[route.KEY]["pick_effort"] == "max" and note[route.KEY]["fallback"] == "none"
    assert note[modelpick.KEY] == {"by": "jev", "pick": "routed-top", "p": 0.9, "tier": "standard"}
    assert "effort_ab" not in note, "the option's effort is not overridden by an A/B arm"
    st, = d.jev.states
    assert len(st) <= modelpick.STATE_CHARS and secret not in st and "paths named: 1: a/b/notes.py" in st
    assert "kind: code" in st and "tier: standard" in st
    row = p.db.one("SELECT * FROM jev_calls")
    assert row["use"] == "model" and row["ref"] == f"task:{tid}" and row["changed"] == 1
    assert row["avoided_usd"] == pytest.approx(3.0)
    # start_run runs the option's model and effort on the routed provider only.
    assert modelpick.run_model(note, "chat", "tier-model", "high") == (_MP_MODEL, "max")
    assert modelpick.run_model(note, core, "tier-model", "high") == ("tier-model", "high")
    assert modelpick.run_model({}, "chat", "tier-model", "high") == ("tier-model", "high")
    # A later start reuses the kept pick: no second call.
    assert modelpick.pick(p.db, d.cfg, p.db.task(tid), "standard", jev=d.jev)["pick"] == "routed-top"
    assert len(d.jev.states) == 1
    # Done on its first try with no fallback: right.
    p.db.x("INSERT INTO runs(task,role,provider,status,started,note) VALUES(?,'worker','chat','ok',?,?)",
           (tid, now, json.dumps({route.KEY: note[route.KEY]})))
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / "run-mp"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "renamed"}))
    d._finish_worker({"task": tid}, Usage(cost_usd=0.0), "ok", run_dir)
    assert p.db.one("SELECT outcome FROM jev_calls")["outcome"] == "right"
    assert p.db.kv(f"{modelpick.PICK}{tid}") is None
    assert "task model picking [model]: 1 calls" in "\n".join(jevuse.lines(p.db, p.config()))


def test_model_pick_settles_wrong_on_escalation_and_a_core_pick_as_no_change(env):
    from ttp import jevuse, modelpick, route
    p = make(env)
    _mp_config(p)
    cfg, now = p.config(), time.time()
    picks = {}
    for name, jev in (("fell", _ModelJev()), ("retried", _ModelJev()), ("unsure", _ModelJev(p=0.5)),
                      ("core", _ModelJev(choice="core", p=0.99))):
        tid = p.db.add_task(name, "Rename helpers.", kind="work", tier="standard", priority=3)
        picks[name] = (tid, modelpick.pick(p.db, cfg, p.db.task(tid), "standard", jev=jev, now=now))
    assert picks["unsure"][1]["pick"] == "core", "a routed pick wins only above min_p"
    assert picks["core"][1]["pick"] == "core"
    fell = picks["fell"][0]
    p.db.x("INSERT INTO runs(task,role,provider,status,started,note) VALUES(?,'worker','chat','fallback',?,?)",
           (fell, now, json.dumps({route.KEY: {"tier": "standard", "to": "chat", "model": _MP_MODEL,
                                               "fallback": "rate_limit", "model_pick": True}})))
    modelpick.settle(p.db, fell, "done", 1)
    modelpick.settle(p.db, picks["retried"][0], "done", 2)
    modelpick.settle(p.db, picks["unsure"][0], "done", 1)
    modelpick.settle(p.db, picks["core"][0], "failed", 1)
    out = {r["ref"]: r["outcome"] for r in p.db.q("SELECT ref, outcome FROM jev_calls")}
    assert out == {f"task:{picks['fell'][0]}": "wrong", f"task:{picks['retried'][0]}": "wrong",
                   f"task:{picks['unsure'][0]}": jevuse.NO_CHANGE, f"task:{picks['core'][0]}": jevuse.NO_CHANGE}
    # Waiting or blocked tasks are not settled yet.
    tid = p.db.add_task("w", "Rename.", kind="work", tier="standard", priority=3)
    modelpick.pick(p.db, cfg, p.db.task(tid), "standard", jev=_ModelJev(), now=now)
    modelpick.settle(p.db, tid, "waiting", 1)
    assert p.db.one("SELECT outcome FROM jev_calls WHERE ref=?", (f"task:{tid}",))["outcome"] is None
    # A retry of a routed pick runs on core.
    assert modelpick.pick(p.db, cfg, {**p.db.task(tid), "attempts": 1}, "standard")["pick"] == "core"


def test_model_pick_without_jev_falls_back_to_core(env, monkeypatch):
    from ttp import modelpick, route
    from ttp.daemon import Daemon
    from ttp.providers.jev import JevUnavailable
    p = make(env)
    _mp_config(p)
    d = Daemon(p.base)
    d.jev = _ModelJev(fail=JevUnavailable("HTTP 503"))
    tid, prov, tier, note = _mp_dispatch(p, d, monkeypatch)
    assert prov == d.cfg.get("core_provider") and route.KEY not in note
    assert note[modelpick.KEY] == {"by": "rules", "pick": "core", "p": None, "why": "jev_unavailable",
                                     "tier": "standard"}
    d.jev = None
    tid, prov, tier, note = _mp_dispatch(p, d, monkeypatch, title="Other")
    assert prov == d.cfg.get("core_provider") and note[modelpick.KEY]["why"] == "jev_unavailable"
    assert p.db.one("SELECT COUNT(*) n FROM jev_calls")["n"] == 0
    # A clear case never calls Jev; a light task keeps its tier's route.
    d.jev = _ModelJev()
    tid, prov, tier, note = _mp_dispatch(p, d, monkeypatch, title="Speed up", spec="Optimize the cache.")
    assert prov == d.cfg.get("core_provider") and note[modelpick.KEY] == {"by": "rules", "pick": "core", "p": None,
                                                                          "why": "hard", "tier": "standard"}
    tid, prov, tier, note = _mp_dispatch(p, d, monkeypatch, title="Look", kind="work", tier="light")
    assert prov == "chat" and modelpick.KEY not in note and "model_pick" not in note[route.KEY]
    assert d.jev.states == []


def test_model_pick_applies_only_at_its_own_tier_and_never_to_a_floor_run(env, monkeypatch):
    from ttp import modelpick, route
    from ttp.daemon import Daemon
    p = make(env)
    _mp_config(p)
    p.set_config("providers.chat.tiers.deep", {"model": "example-org/deep-model"})   # deep is not routed
    d = Daemon(p.base)
    d.jev = _ModelJev()
    now = time.time()
    # A core pick (the rules' 'hard' pick) leaves a later light wake to the light tier's route.
    core = p.db.add_task("Speed up", "Optimize the cache in notes.py.", kind="code", tier="standard")
    assert modelpick.pick(p.db, d.cfg, p.db.task(core), "standard", jev=d.jev)["why"] == "hard"
    assert d._route(p.db.task(core), "light", None, now) is None   # the route guard: optimization stays on core
    plain = p.db.add_task("Tidy", "Rename helpers in notes.py.", kind="code", tier="light")
    light = d._route(p.db.task(plain), "light", None, now)
    assert light and light["to"] == "chat" and light["model"] == "example-org/light-model" and "model_pick" not in light
    assert d._model_pick(p.db.task(core), "light", peek=True) is None
    # A routed pick runs only at the standard tier: a deep start and a floor run start on core.
    tid, prov, tier, note = _mp_dispatch(p, d, monkeypatch)
    assert prov == "chat" and note[route.KEY]["model_pick"] is True
    task = p.db.task(tid)
    assert d._route(task, "standard", None, now)["model_pick"] is True
    assert d._route(task, "deep", None, now) is None
    monkeypatch.setattr("ttp.tierfloor.target", lambda db, cfg, t: {"tier": "standard", "effort": "high"})
    assert d._route(task, "standard", None, now) is None
    # The kept pick still settles.
    assert p.db.kv(f"{modelpick.PICK}{tid}")["pick"] == "routed-top"


def test_model_pick_honours_providers_allowed_and_a_chat_core(env, monkeypatch):
    from ttp import modelpick, route
    from ttp.daemon import Daemon
    p = make(env)
    _mp_config(p)
    core = p.config().get("core_provider") or "claude"
    p.set_config("providers_allowed", [core])
    d = Daemon(p.base)
    d.jev = _ModelJev()
    assert modelpick.target(d.cfg, {"pick": "routed-top"}) is None
    tid, prov, tier, note = _mp_dispatch(p, d, monkeypatch)
    assert prov == core and route.KEY not in note
    assert not modelpick.routed({**d.cfg, "providers_allowed": None, "core_provider": "chat"}, "routed-top")


def test_model_pick_config_problems_name_each_bad_dispatch_setting():
    from ttp import modelpick, route

    def probs(dispatch, **top):
        return [x for x in route.config_problems({**top, "providers": {"chat": {"dispatch": dispatch}}})
                if ".dispatch" in x]
    ok = {"enabled": True, "min_p": 0.7, "options": {"routed-top": {"provider": "chat", "model": "m", "effort": "max"},
                                                    "core": {"provider": "claude"}}}
    assert route.config_problems({}) == [] and probs(ok) == [] and probs({"enabled": False}) == []
    assert probs({**ok, "min_p": 0}) == [] and probs({**ok, "min_p": 1}) == []
    # Off by default: no dispatch, or one that does not say enabled true.
    assert not modelpick.enabled({}) and not modelpick.enabled({"providers": {"chat": {"dispatch": {
        "options": ok["options"]}}}})
    # Enabled with no routed option: one problem, naming why.
    for opts in ({}, {"core": {"provider": "claude"}}, {"r": {"provider": "chat"}}, {"r": {"provider": "chat", "model": ""}}):
        got = probs({"enabled": True, "options": opts})
        assert got == ["providers.chat.dispatch.enabled: no option runs on the chat provider with a model (such as "
                       '{"provider": "chat", "model": "<model id>"}); dispatch is off'], (opts, got)
    assert probs({"enabled": True}) == got
    assert probs(ok, core_provider="chat") == ["providers.chat.dispatch.enabled: chat is the core provider; "
                                               "nothing to route"]
    assert probs(ok, providers_allowed=["codex"]) == ["providers.chat.dispatch.enabled: providers_allowed leaves "
                                                      "out chat; nothing is routed"]
    # min_p outside 0..1 (or not a number): named, and the default is used.
    for bad in (-0.1, 1.5, "0.7", True, None):
        assert probs({**ok, "min_p": bad}) == ["providers.chat.dispatch.min_p: give a number from 0 to 1; 0.6 is used"], bad
        assert modelpick.min_p({"providers": {"chat": {"dispatch": {"min_p": bad}}}}) == modelpick.MIN_P
    assert modelpick.min_p({"providers": {"chat": {"dispatch": ok}}}) == 0.7
    # Options that are not objects with a provider: one problem each; the routed one still counts.
    got = probs({**ok, "options": {**ok["options"], "a": "chat", "b": {"model": "m"}, "c": {"provider": " "},
                                   "d": {"provider": 3}}})
    assert got == [f"providers.chat.dispatch.options.{k}: give an object with a provider (and model, effort); ignored"
                   for k in "abcd"], got
    # ... and ignored indeed: options() keeps none of them, so none becomes a choice that runs on core.
    assert set(modelpick.options({"providers": {"chat": {"dispatch": {"options": {
        **ok["options"], "a": "chat", "b": {"model": "m"}, "c": {"provider": " "}, "d": {"provider": 3}}}}}})) \
        == {"routed-top", "core"}
    assert probs({"enabled": True, "options": ["routed-top"]}) == [
        "providers.chat.dispatch.options: give an object of option keys, each with a provider; no options",
        *probs({"enabled": True})]
    # Wrong shapes of dispatch itself and of enabled.
    assert probs("on") == ["providers.chat.dispatch: give an object (enabled, min_p, options); dispatch is off"]
    assert probs({**ok, "enabled": "yes"}) == ["providers.chat.dispatch.enabled: give true or false; dispatch is off"]
    assert not modelpick.enabled({"providers": {"chat": {"dispatch": {**ok, "enabled": "yes"}}}})
    # project.config_problems carries them.
    from ttp.project import config_problems
    assert [x for x in config_problems({"providers": {"chat": {"dispatch": {**ok, "min_p": 2}}}}) if "dispatch" in x] \
        == ["providers.chat.dispatch.min_p: give a number from 0 to 1; 0.6 is used"]
    # A valid dispatch is a known key to the rest of the config checks (no 'unknown key providers.chat.dispatch').
    assert [x for x in config_problems({"providers": {"chat": {"dispatch": ok}}}) if "dispatch" in x] == []


def test_model_pick_dispatch_keys_are_known_and_settable_with_ttp_config(env, capsys):
    """providers.chat.dispatch was missing from project.PROVIDER_KEYS: `ttp config` refused it as an
    unknown key, so the model pick could not be turned on from the CLI or chat."""
    from ttp import cli, project
    for key in ("providers.chat.dispatch", "providers.chat.dispatch.enabled", "providers.chat.dispatch.min_p",
                "providers.chat.dispatch.options.routed-top.model"):
        assert project.unknown_key_hint(key) is None, key
    assert project.unknown_key_hint("providers.chat.dispatchx") is not None, "a typo is still caught"
    p = make(env)
    cli.main(["config", "demo", "providers.chat.dispatch.enabled", "true"])
    cli.main(["config", "demo", "providers.chat.dispatch.min_p", "0.7"])
    assert p.raw_config()["providers"]["chat"]["dispatch"] == {"enabled": True, "min_p": 0.7}


def _trip_task(p, side, bad=False, kind="code", labels=(), at=None, checks=None, why=None):
    """One ended picked task: a routed or core sample of kind/standard; `bad` makes it fail; `why` makes
    it a rules pick of that reason."""
    from ttp import modelpick
    from ttp.prguard import CHECKS_FILE
    at = time.time() - 3600 if at is None else at
    tid = p.db.add_task(f"{side} task", "Rename helpers.", kind=kind, tier="standard", priority=3, labels=list(labels))
    p.db.update_task(tid, status="failed" if bad else "done")
    note = {modelpick.KEY: {"by": "jev", "pick": "routed-top" if side == "routed" else "core", "p": 0.9,
                            "tier": "standard"}}
    if why:
        note[modelpick.KEY].update(by="rules", p=None, why=why)
    rid = p.db.x("INSERT INTO runs(task,role,provider,status,started,note) VALUES(?,'worker','claude','ok',?,?)",
                 (tid, at, json.dumps(note)))
    if checks is not None:
        (p.runs / str(rid)).mkdir(parents=True, exist_ok=True)
        (p.runs / str(rid) / CHECKS_FILE).write_text(json.dumps({"passed": checks}))
    return tid


def _trip_fill(p, routed_bad, routed_n, core_bad, core_n, **kw):
    for side, bad, n in (("routed", routed_bad, routed_n), ("core", core_bad, core_n)):
        for i in range(n):
            _trip_task(p, side, bad=i < bad, **kw)


def test_model_trip_holds_back_under_sample_within_margin_and_while_routing_is_off(env):
    from ttp import modeltrip
    p = make(env)
    _trip_fill(p, 3, 10, 2, 10, kind="work")   # 30% against 20%: exactly the 10-point margin, not past it
    _mp_config(p)
    assert modeltrip.check(p, p.config()) == []
    _trip_fill(p, 9, 9, 0, 9)   # every routed code task failed, but only 9 samples a side
    assert modeltrip.check(p, p.config()) == [] and not p.db.kv(modeltrip.TRIPS)
    _trip_fill(p, 0, 1, 0, 1)   # 10 a side: routed 9/10 bad against core 0/10
    _mp_config(p, enabled=False)
    assert modeltrip.check(p, p.config()) == [], "inactive while routing is off"
    _mp_config(p, tripwire={"min_samples": 11})
    assert modeltrip.check(p, p.config()) == [], "a configurable sample size"
    _mp_config(p, tripwire={"margin_pts": 5})
    assert [t["stratum"] for t in modeltrip.check(p, p.config())] == ["code/standard/*", "work/standard/*"], \
        "a configurable margin"


def test_model_trip_past_margin_routes_the_stratum_to_core_records_and_notifies(env, monkeypatch):
    from ttp import modelpick, modeltrip
    from ttp.daemon import Daemon
    p = make(env)
    _trip_fill(p, 4, 10, 1, 10, labels=["docs", "continues:99999"])
    _trip_fill(p, 0, 10, 0, 10, kind="work")
    _mp_config(p)
    d = Daemon(p.base)
    alerts = []
    monkeypatch.setattr(d, "alert", lambda key, text, **k: alerts.append((key, text, k.get("severity"))))
    d.trip_model_routing()
    want = {"code/standard/*", "code/standard/docs"}   # an id label is no stratum; work stays routed
    assert set(p.db.kv(modeltrip.TRIPS)) == want
    assert {k for k, _, _ in alerts} == {f"model-trip:{s}" for s in want} and {s for *_, s in alerts} == {"low"}
    assert "4/10 (40%)" in alerts[0][1] and "1/10 (10%)" in alerts[0][1]
    mem = "\n".join(f.read_text() for f in p.memory_dir.glob("decision-*.md"))
    assert "code/standard/*" in mem and "head-to-head" in mem
    cfg = p.config()

    def rules(**kw):
        tid = p.db.add_task("Tidy notes", "Rename helpers.", **{"kind": "code", "tier": "standard", "priority": 3, **kw})
        return modelpick.rules(p.db, cfg, p.db.task(tid), "standard")
    assert rules() == "tripwire" and rules(labels=["docs"]) == "tripwire"
    assert rules(kind="work") == "", "another stratum stays open"
    d._model_trip_checked = 0.0
    d.trip_model_routing()   # already tripped: no second notice or memory
    assert len(alerts) == 2


def test_model_trip_re_enables_only_on_a_newer_passing_head_to_head(env):
    from ttp import modelpick, modeltrip
    p = make(env)
    now = time.time()
    _trip_fill(p, 5, 10, 0, 10, at=now - 7200)
    _mp_config(p)
    assert [t["stratum"] for t in modeltrip.check(p, p.config(), now=now - 600)] == ["code/standard/*"]
    task = {"kind": "code", "labels": "[]"}
    modeltrip.head_to_head(p.db, "code/standard/*", True, "bench/old", now=now - 1200)
    assert modeltrip.tripped(p.db, task, "standard"), "a result older than the trip"
    modeltrip.head_to_head(p.db, "code/standard/*", False, "bench/worse", now=now - 300)
    assert modeltrip.tripped(p.db, task, "standard"), "a worse result"
    modeltrip.head_to_head(p.db, "code/standard/docs", True, "bench/other", now=now - 300)
    assert modeltrip.tripped(p.db, task, "standard"), "another stratum's result"
    assert modeltrip.check(p, p.config(), now=now) == [], "no new trip, nothing re-enabled by time alone"
    modeltrip.head_to_head(p.db, "code/standard/*", True, "bench/new", now=now - 60)
    assert modeltrip.tripped(p.db, task, "standard") is None
    tid = p.db.add_task("Tidy notes", "Rename helpers.", kind="code", tier="standard", priority=3)
    assert modelpick.rules(p.db, p.config(), p.db.task(tid), "standard") == ""
    assert modeltrip.check(p, p.config(), now=now) == [], "samples before the head-to-head no longer count"
    out = _ttp_run(p, "model-trip").stdout
    assert "code/standard/*: routed" in out and "passed (bench/new)" in out and "code/standard/docs: routed" in out
    assert _ttp_run(p, "model-trip", "--h2h", "code/standard/*", "--ok", "--worse", "--ref", "x").returncode == 2
    r = _ttp_run(p, "model-trip", "--h2h", "code/standard/*", "--worse", "--ref", "bench/later")
    assert r.returncode == 0 and "worse" in r.stdout and not p.db.kv(modeltrip.H2H)["code/standard/*"]["ok"]
    modeltrip.head_to_head(p.db, "code/standard/*", True, "bench/new", now=now - 60)
    _trip_fill(p, 5, 10, 0, 10, at=now - 30)
    assert [t["stratum"] for t in modeltrip.check(p, p.config(), now=now)] == ["code/standard/*"], "it can trip again"


def test_model_trip_counts_each_kind_of_bad_outcome(env):
    from ttp import modeltrip
    p = make(env)
    _mp_config(p)
    now = time.time()
    good = _trip_task(p, "routed", checks=True)
    failed = _trip_task(p, "routed", bad=True)
    checks = _trip_task(p, "routed", checks=False)
    reviewed = _trip_task(p, "routed")
    rv = p.db.add_task(f"Review #{reviewed}", "s", kind="review", origin="daemon", labels=[f"auto_review:{reviewed}"])
    p.db.x("INSERT INTO review_stats(task,ts,outcome) VALUES(?,?,?)", (rv, now - 300, "changes_needed"))
    continued = _trip_task(p, "core")
    p.db.add_task("continue: the rest", "s", kind="code", labels=[f"continues:{continued}"])
    fixed = _trip_task(p, "core")
    rv2 = p.db.add_task(f"Review #{fixed}", "s", kind="review", origin="daemon", labels=[f"auto_review:{fixed}"])
    p.db.add_task(f"Fix review #{rv2}: x", "f", kind="code", labels=[f"review_fix:{rv2}"])
    got = modeltrip.samples(p, p.config(), now - 86400, now)
    assert {t: s["bad"] for t, s in got.items()} == {good: False, failed: True, checks: True, reviewed: True,
                                                     continued: True, fixed: True}
    assert got[good]["side"] == "routed" and got[fixed]["side"] == "core"


def test_model_trip_counts_only_tasks_open_to_routing(env):
    """Starts the rules force to core (hard, device, urgent, ...) are riskier by design: they count on
    neither side, so they cannot hide a worse routed rate behind an inflated core one."""
    from ttp import modelpick, modeltrip
    p = make(env)
    _mp_config(p)
    now = time.time()
    _trip_fill(p, 5, 10, 0, 10)
    forced = [_trip_task(p, "core", bad=True, why=w)
              for w in ("hard", "device", "urgent", "untrusted", "conclusion", "retry", "kind", "tier", "hard", "device")]
    held = _trip_task(p, "core", why="tripwire")
    no_jev = _trip_task(p, "core", why="jev_unavailable")
    retried = _trip_task(p, "routed")   # a later retry start on core still counts against the routed pick
    note = {modelpick.KEY: {"by": "rules", "pick": "core", "p": None, "why": "retry", "tier": "standard"}}
    rid = p.db.x("INSERT INTO runs(task,role,provider,status,started,note) VALUES(?,'worker','claude','ok',?,?)",
                 (retried, now - 1800, json.dumps(note)))
    (p.runs / str(rid)).mkdir(parents=True, exist_ok=True)
    (p.runs / str(rid) / "checks.json").write_text(json.dumps({"passed": False}))
    got = modeltrip.samples(p, p.config(), now - 86400, now)
    assert not set(forced) & set(got) and got[held]["side"] == got[no_jev]["side"] == "core"
    assert got[retried]["side"] == "routed"
    trips = modeltrip.check(p, p.config(), now=now)
    assert [(t["stratum"], t["routed"], t["core"]) for t in trips] == [("code/standard/*", [6, 11], [0, 12])]


def test_model_trip_margin_is_exact_and_settings_keep_a_sample_floor(env):
    from ttp import modeltrip
    p = make(env)
    _trip_fill(p, 4, 10, 3, 10, kind="work")   # 40% against 30%: exactly the 10-point margin (float says past)
    _trip_fill(p, 10, 10, 0, 0, kind="ask")    # routed only: no core side to compare against
    _mp_config(p, tripwire={"min_samples": 0})
    assert modeltrip.settings(p.config())["min_samples"] == 10, "a zero sample floor is ignored"
    assert modeltrip.check(p, p.config()) == []
    _mp_config(p, tripwire={"min_samples": 1, "margin_pts": 9.99})
    assert [t["stratum"] for t in modeltrip.check(p, p.config())] == ["work/standard/*"]


def test_model_trip_a_worse_head_to_head_keeps_the_sample_window(env):
    from ttp import modeltrip
    p = make(env)
    now = time.time()
    _trip_fill(p, 5, 10, 0, 10, at=now - 7200)
    _mp_config(p)
    modeltrip.head_to_head(p.db, "code/standard/*", False, "bench/worse", now=now - 600)
    assert [t["stratum"] for t in modeltrip.check(p, p.config(), now=now)] == ["code/standard/*"]


def test_route_guard_keeps_protected_classes_on_core_whatever_asks(env, monkeypatch):
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    _mp_config(p)
    d = Daemon(p.base)
    now = time.time()
    add = lambda title, spec="Rename helpers in notes.py.", **k: p.db.task(p.db.add_task(
        title, spec, **{"kind": "code", "tier": "light", **k}))
    plain = add("Tidy the notes module")
    assert d._route(plain, "light", None, now)["to"] == "chat"   # an unprotected class routes as its rule says
    for why, task in {"optimization": add("Optimize the cache", "Optimize notes.py."),
                      "untrusted": add("Triage", labels=["untrusted_input"])}.items():
        assert route.protected(p.db, d.cfg, task, "light") == why and d._route(task, "light", None, now) is None
    assert route.protected(p.db, d.cfg, plain, "deep") == "deep"
    for mod, fn, val, why in (("effort", "device_task", True, "device"), ("urgent", "is_urgent", True, "urgent")):
        with monkeypatch.context() as m:
            m.setattr(f"ttp.{mod}.{fn}", lambda t, v=val: v)
            assert route.protected(p.db, d.cfg, plain, "light") == why and d._route(plain, "light", None, now) is None
    with monkeypatch.context() as m:
        m.setattr("ttp.tierfloor.target", lambda db, c, t: {"tier": "standard"})
        assert d._route(plain, "light", None, now) is None
    # A model pick is refused the same way.
    with monkeypatch.context() as m:
        m.setattr("ttp.urgent.is_urgent", lambda t: True)
        d.jev = _ModelJev()
        assert d._route(plain, "standard", None, now) is None
