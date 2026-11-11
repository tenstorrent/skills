# Ramp-up after an outage (rampup.py, Daemon.track_outages, the ramp check in Daemon._dispatch). Exec'd
# into test_tt_project_runtime.py: make, _slot_setup, _slot_tasks and its imports are in scope.
from __future__ import annotations


def test_an_ended_outage_starts_a_ramp_that_doubles_and_ends_at_the_normal_limit(env):
    from ttp import rampup
    p = make(env)
    cfg = {"budget": {"ramp_start": 2, "ramp_step_s": 100, "max_parallel_workers": 6}}
    count = lambda scope: 1   # noqa: E731
    assert rampup.observe(p.db, cfg, {"pause"}, "b1", count, now=1000) == "", "the first look starts nothing"
    assert rampup.observe(p.db, cfg, {"pause"}, "b1", count, now=1060) == ""
    assert rampup.observe(p.db, cfg, set(), "b1", count, now=1100) == "pause"
    ramp = rampup.active(p.db, cfg, 1100)
    assert ramp["base"] == 1 and ramp["scope"] is None
    assert [rampup.allowed(ramp, cfg, t) for t in (1100, 1199, 1200, 1300)] == [3, 3, 5, 9]
    assert rampup.active(p.db, cfg, 1250) and rampup.active(p.db, cfg, 1300) is None, "past the normal limit: over"
    # A lifted resource pause ramps only that resource's tasks; a new boot ramps everything.
    rampup.observe(p.db, cfg, {"resource:box-a"}, "b1", count, now=2000)
    assert rampup.observe(p.db, cfg, set(), "b1", count, now=2060) == "resource:box-a"
    ramp = rampup.active(p.db, cfg, 2060)
    assert ramp["scope"] == ["box-a"] and rampup.covers(ramp, {"box-a"}) and not rampup.covers(ramp, {"box-b"})
    assert rampup.observe(p.db, cfg, set(), "b2", count, now=2100) == "host reboot"
    assert rampup.active(p.db, cfg, 2100)["scope"] is None
    off = {"budget": {"ramp_start": 0}}
    rampup.observe(p.db, off, {"pause"}, "b2", count, now=3000)
    assert rampup.observe(p.db, off, set(), "b2", count, now=3060) == "" and rampup.active(p.db, off) is None


def test_track_outages_starts_a_ramp_when_the_project_resumes(env):
    from ttp import rampup
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.cfg = p.config()
    p.db.set_kv("paused", True)
    d.track_outages()
    assert rampup.active(p.db, d.cfg) is None
    p.db.set_kv("paused", False)
    d.track_outages()
    ramp = rampup.active(p.db, d.cfg)
    assert ramp and ramp["cause"] == "pause" and ramp["base"] == 0


def test_dispatch_after_an_outage_starts_urgent_work_first_and_widens_step_by_step(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    from ttp import rampup
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=6)
    try:
        p.set_config("budget.ramp_start", 1)
        p.set_config("budget.ramp_step_s", 100)
        d.cfg = p.config()
        routine = [p.db.add_task(f"routine {i}", "s", kind="work", tier="light", origin="user", priority=3)
                   for i in range(4)]
        urgent = p.db.add_task("urgent work", "s", kind="work", tier="light", origin="user", priority=1)
        now = time.time()
        p.db.set_kv(rampup.KEY, {"since": now, "cause": "pause", "base": 0, "scope": None})
        d.dispatch()
        assert _slot_tasks(p) == [urgent], "one start in the first step, the urgent task"
        d.dispatch()
        assert len(_slot_tasks(p)) == 1, "no more within the step"
        p.db.set_kv(rampup.KEY, {"since": now - 100, "cause": "pause", "base": 0, "scope": None})
        d.dispatch()
        assert _slot_tasks(p) == [urgent, routine[0]], "the next step doubles the starts"
        p.db.set_kv(rampup.KEY, {"since": now - 1000, "cause": "pause", "base": 0, "scope": None})
        d.dispatch()
        assert len(_slot_tasks(p)) == 5 and rampup.active(p.db, d.cfg) is None, "ramp over: the normal limit"
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"
