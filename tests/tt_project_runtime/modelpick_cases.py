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
        assert set(questions["model"]["options"]) == {"routed-top", "core"}
        self.states.append(state)
        return {"model": {"choice": self.choice, "probabilities": {self.choice: self.p}}}


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
    assert note[modelpick.KEY] == {"by": "jev", "pick": "routed-top", "p": 0.9}
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
    assert note[modelpick.KEY] == {"by": "rules", "pick": "core", "p": None, "why": "jev_unavailable"}
    d.jev = None
    tid, prov, tier, note = _mp_dispatch(p, d, monkeypatch, title="Other")
    assert prov == d.cfg.get("core_provider") and note[modelpick.KEY]["why"] == "jev_unavailable"
    assert p.db.one("SELECT COUNT(*) n FROM jev_calls")["n"] == 0
    # A clear case never calls Jev; a light task keeps its tier's route.
    d.jev = _ModelJev()
    tid, prov, tier, note = _mp_dispatch(p, d, monkeypatch, title="Speed up", spec="Optimize the cache.")
    assert prov == d.cfg.get("core_provider") and note[modelpick.KEY] == {"by": "rules", "pick": "core", "p": None, "why": "hard"}
    tid, prov, tier, note = _mp_dispatch(p, d, monkeypatch, title="Look", kind="work", tier="light")
    assert prov == "chat" and modelpick.KEY not in note and "model_pick" not in note[route.KEY]
    assert d.jev.states == []
