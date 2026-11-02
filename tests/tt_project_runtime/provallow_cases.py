# providers_allowed tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file (and earlier case files).
from __future__ import annotations


def test_providers_allowed_keeps_tier_routing_off_a_provider_it_leaves_out(env, monkeypatch):
    from ttp import provallow, route
    from ttp.daemon import Daemon
    from ttp.project import config_problems
    p = make(env)
    _route_config(p)
    core = p.config().get("core_provider", "claude")
    p.set_config("providers_allowed", [core])
    d = Daemon(p.base)
    assert route.target(d.cfg, "light") is None
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="stays on core")
    assert (prov, tier) == (core, "light") and route.KEY not in note
    assert any("providers_allowed leaves out chat" in x for x in config_problems(p.raw_config())), \
        "a routed tier the list switches off is named"
    # Listing chat (or unsetting the list) routes as before.
    for allowed in ([core, "chat"], []):
        p.set_config("providers_allowed", allowed)
        d._load_config()
        assert provallow.allows(d.cfg, "chat")
        prov, _, note = _route_dispatch(p, d, monkeypatch, title=f"routed {allowed}")
        assert prov == "chat" and note[route.KEY]["fallback"] == "none", allowed


def test_providers_allowed_keeps_failover_inside_the_list_and_says_why_a_task_waits(env, monkeypatch):
    # Codex is the core provider with a red plan window for longer than the threshold: without the
    # list its task moves to fake (provider_failover_to); with the list it waits, with a status line
    # that clears once it starts.
    from ttp import budget as bud, failover, provallow
    p, d, now = _failover_setup(env, hours=150)
    p.set_config("core_provider", "codex")
    p.set_config("providers.fake", {"tiers": {}})
    p.set_config("coordinator.provider_failover_to", ["fake"])
    p.set_config("providers_allowed", ["codex"])
    d._load_config()
    assert failover.targets(d.cfg) == ["codex"]
    tid = p.db.add_task("on the core provider", "Do it.", kind="work", tier="standard")
    waiting = p.db.add_task("waits on a job", "Do it.", kind="work", tier="standard")
    p.db.update_task(waiting, status="waiting", blocked_reason="its own wait")
    d.update_gates()
    assert d.gates["codex"].level == "red"
    d.failover_pinned()
    t = p.db.task(tid)
    assert t["provider"] is None and not p.db.q("SELECT id FROM events WHERE kind='provider_failover'"), \
        "moved to a provider providers_allowed leaves out"
    assert t["blocked_reason"].startswith(provallow.HELD_NOTE) and "codex cannot start new work" in t["blocked_reason"]
    assert p.db.task(waiting)["blocked_reason"] == "its own wait", "a waiting task's own line was overwritten"
    assert any("provider_failover_to: providers_allowed leaves out fake" in x for x in
               __import__("ttp.project", fromlist=["x"]).config_problems(p.raw_config()))
    # The gate opens: the task starts on codex and its line is gone.
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, prompt, provider, tier, cwd, **k: started.append(provider) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.root), None))
    monkeypatch.setattr(d, "update_gates", lambda *a, **k: None)
    d.gates["codex"] = bud.Gate("codex", regime="windows", max_parallel=6)
    d.dispatch()
    assert started == ["codex"] and p.db.task(tid)["blocked_reason"] is None
    # Unset, the same stop moves a task as before.
    p.set_config("providers_allowed", [])
    d._load_config()
    other = p.db.add_task("second", "Do it.", kind="work", tier="standard")
    d.gates.pop("codex")
    monkeypatch.undo()
    d.update_gates()
    d._failover_checked = 0
    d.failover_pinned()
    assert p.db.task(other)["provider"] == "fake"


def test_providers_allowed_preference_pin_moves_only_to_an_allowed_provider(env):
    # The core provider (fake) is always allowed: a codex preference pin off a red plan window moves
    # there, never to a listed-out failover target ahead of it.
    from ttp import failover
    p, d, now = _failover_setup(env)
    p.set_config("providers.cursor", {"tiers": {}})
    p.set_config("coordinator.provider_failover_to", ["cursor"])
    p.set_config("providers_allowed", ["codex"])
    d._load_config()
    assert failover.targets(d.cfg) == ["fake"]
    tid = _codex_task(p)
    d.update_gates()
    d.failover_pinned()
    assert p.db.task(tid)["provider"] == "fake"


def test_task_add_refuses_a_pin_outside_providers_allowed_and_an_older_pin_waits(env, monkeypatch):
    from ttp import budget as bud, cli, provallow
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    p = make(env)
    core = p.config().get("core_provider", "claude")
    p.set_config("providers_allowed", ["claude"])
    probs = coord.apply(p, [{"type": "task_add", "title": "pinned", "spec": "s", "provider": "codex"}])
    assert any("'codex' is not in this project's providers_allowed" in x for x in probs), probs
    assert not p.db.q("SELECT id FROM tasks WHERE title='pinned'")
    assert coord.apply(p, [{"type": "task_add", "title": "on core", "spec": "s", "provider": "claude"}]) == []
    (ok,) = p.db.q("SELECT id FROM tasks WHERE title='on core'")
    probs = coord.apply(p, [{"type": "task_update", "id": ok["id"], "provider": "codex"}])
    assert any("providers_allowed" in x for x in probs) and p.db.task(ok["id"])["provider"] == "claude"
    # A pin made before the list waits queued, unspent, and says why; it starts once the list names it.
    old = p.db.add_task("older pin", "s", kind="work", tier="light", provider="codex", priority=1)
    d = Daemon(p.base)
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, prompt, provider, tier, cwd, **k: started.append(provider) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.root), None))
    for prov in (core, "codex"):
        d.gates[prov] = bud.Gate(prov, regime="windows", max_parallel=6)
    p.db.update_task(ok["id"], status="done")
    d.dispatch()
    t = p.db.task(old)
    assert "codex" not in started and t["status"] == "queued" and t["attempts"] == 0
    assert t["blocked_reason"] == provallow.held(d.cfg, "codex")
    # The CLI checks the names; "" unsets the list.
    with pytest.raises(SystemExit):
        cli.main(["config", p.name, "providers_allowed", '["nosuch"]'])
    cli.main(["config", p.name, "providers_allowed", '""'])
    assert p.config()["providers_allowed"] == [] and provallow.names(p.config()) is None
    assert provallow.of("codex, codex", {"codex": {}}) == ["codex"]
    for bad in (3, {"a": 1}):
        with pytest.raises(ValueError):
            provallow.of(bad, {"codex": {}})
