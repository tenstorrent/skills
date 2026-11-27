# GLM work fallback tests (route.glm_work.fallback, glmwork.fallback) of tests/test_tt_project_runtime.py, which
# runs this file in its own namespace (see _include_cases there): the model a GLM-routed run takes on core when
# the endpoint cannot serve it. Add new GLM fallback tests here.
from __future__ import annotations


def _gf_setup(env, monkeypatch, cause, fallback=None, on=True):
    """A covered project whose dispatch sees `cause` from the route's cooling check, with a queued work task."""
    from ttp import configwho
    from ttp import daemon as dmod
    p = make(env)
    p.set_config("core_provider", "claude")
    _route_config(p, route=None)
    gw = {"on": on, "model": _RT_MODEL, "holdout": 0, **({"fallback": fallback} if fallback else {})}
    p.set_config("route.glm_work", gw, by=configwho.by("user", via="cli"))
    monkeypatch.setattr(dmod.route, "cooling", lambda db, name, now: cause)
    return p, dmod.Daemon(p.base)


def _gf_refused_run(p, tid, age_s):
    """The task's own earlier run on the endpoint, refused its model `age_s` ago (the model left mid-run)."""
    note = json.dumps({"route": {"tier": "standard", "to": "chat", "model": _RT_MODEL, "fallback": "model_missing",
                                 "glm_work": "glm"}})
    p.db.x("INSERT INTO runs (task, role, provider, started, status, note) VALUES (?,?,?,?,?,?)",
           (tid, "worker", "chat", time.time() - age_s, "fallback", note))
    return p.db.one("SELECT max(id) AS id FROM runs")["id"]


@pytest.mark.parametrize("cause", ["model_missing", "down", "busy"])
@pytest.mark.parametrize("fb,want", [(None, ("sonnet", "high")), ({"model": "haiku", "effort": "low"}, ("haiku", "low"))])
def test_a_glm_work_run_the_endpoint_cannot_take_starts_on_the_set_fallback_model(env, monkeypatch, cause, fb, want):
    """The model is not served, the endpoint is down, or a quota or plan gate holds it: the run's note names
    the fallback model; set, `route.glm_work.fallback` picks it; unset, the default is Sonnet at high."""
    from ttp import route, runclass
    p, d = _gf_setup(env, monkeypatch, cause, fb)
    prov, _, note = _route_dispatch(p, d, monkeypatch, title="sum the logs", kind="work", tier="standard")
    assert prov == "claude" and note[route.KEY]["fallback"] == cause
    assert note[route.KEY]["fallback_model"] == want[0]
    assert (note[runclass.CHEAP_KEY]["model"], note[runclass.CHEAP_KEY]["effort"]) == want


def test_the_next_run_of_a_task_whose_glm_model_left_mid_run_takes_the_fallback_then_returns_to_glm(env, monkeypatch):
    """A run refused its model mid-run sends the task's next runs to core for an hour ('rerun'): those take the
    fallback model, not the tier's (Opus); past the hour the task routes to GLM again by itself."""
    from ttp import route, runclass
    p, d = _gf_setup(env, monkeypatch, "")
    tid = p.db.add_task("sum the logs", "s", kind="work", tier="standard", priority=3, budget_usd=8.0, labels=[])
    before = _gf_refused_run(p, tid, 60)
    prov, tier, k = _dispatch_task(p, d, monkeypatch, tid)
    e = k["note"][route.KEY]
    assert (prov, tier, e["fallback"], e["after"]) == ("claude", "standard", "rerun", before)
    assert e["fallback_model"] == "sonnet" and k["note"][runclass.CHEAP_KEY]["model"] == "sonnet"
    p.db.x("UPDATE runs SET started=? WHERE id=?", (time.time() - route.MISSING_TASK_S - 60, before))
    p.db.x("UPDATE tasks SET status='queued' WHERE id=?", (tid,))
    prov, _, k = _dispatch_task(p, d, monkeypatch, tid)
    assert prov == "chat" and k["note"][route.KEY]["fallback"] == "none"


@pytest.mark.parametrize("cause", ["no_handoff", "needs_opus"])
def test_a_rerun_after_the_tasks_own_glm_run_failed_keeps_its_core_tier_model(env, monkeypatch, cause):
    """A rerun whose cause is the GLM run's own end (no hand-off, or a bounce asking for Opus) is no endpoint refusal: it keeps core at its
    tier, as before; so does any run when the work route is off."""
    from ttp import glmwork, route
    p, d = _gf_setup(env, monkeypatch, "")
    tid = p.db.add_task("sum the logs", "s", kind="work", tier="standard", priority=3, budget_usd=8.0, labels=[])
    run = _gf_refused_run(p, tid, 60)
    p.db.x("UPDATE runs SET note=replace(note, 'model_missing', ?) WHERE id=?", (cause, run))
    prov, _, k = _dispatch_task(p, d, monkeypatch, tid)
    e = k["note"].get(route.KEY) or {}
    assert prov == "claude" and e.get("fallback") in ("rerun", None) and "fallback_model" not in e
    assert glmwork.fallback(d.cfg, "rerun", p.db, run) is None and glmwork.fallback(d.cfg, "rerun") is None
    assert glmwork.fallback({"route": {"glm_work": {"on": False, "model": _RT_MODEL}}}, "down") is None


@pytest.mark.parametrize("kw", [{"kind": "review"}, {"kind": "plan"}, {"tier": "deep"}, {"reply_chat": "chat-1"},
                                {"spec": "Sum the logs.\nThen post the summary to the user on slack."},
                                {"pin_required": True, "provider": "claude"}])
def test_runs_the_work_route_keeps_on_core_never_take_the_fallback_model(env, monkeypatch, kw):
    """Reviews, plans, the deep tier and a required pin never route to GLM, and a run that answers or posts to a
    person keeps its tier's model, so an endpoint refusal never moves them to the fallback model."""
    from ttp import route, runclass
    p, d = _gf_setup(env, monkeypatch, "model_missing")
    got = _route_dispatch(p, d, monkeypatch, title="sum the logs", **{"kind": "work", "tier": "standard", **kw})
    note = got[2] if got else {}
    assert "fallback_model" not in (note.get(route.KEY) or {}), kw
    assert "glm_work fallback" not in str((note.get(runclass.CHEAP_KEY) or {}).get("why")), kw
