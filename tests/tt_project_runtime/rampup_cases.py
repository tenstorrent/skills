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


def _cut_run(p, tmp_path, tid, boot, pid, lease_age=20.0):
    """A run of task tid recorded running under `boot` with supervisor `pid`, its lease `lease_age` s old."""
    p.db.update_task(tid, status="running")
    run_dir = tmp_path / f"run{tid}"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text("")
    (run_dir / "lease").touch()
    os.utime(run_dir / "lease", (time.time() - lease_age, time.time() - lease_age))
    return p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id,pid) VALUES(?,?,?,?,?,?,?,?)",
                  (tid, "worker", "fake", time.time() - 1800, "running", str(run_dir), boot, pid))


def test_runs_a_reboot_cut_resume_on_the_first_tick_ahead_of_its_slow_steps_and_the_ramp(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """A fast reboot: runs recorded running on the earlier boot, their pids gone, their leases still
    young. The first tick reaps them and starts every one before its slow steps run, though the ramp
    after the reboot allows one start; work that was not running waits for the ramp."""
    import subprocess
    from ttp import rampup
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=6)
    try:
        p.set_config("budget.ramp_start", 1)
        d.cfg = p.config()
        gone = subprocess.Popen(["true"])
        gone.wait()
        cut = [p.db.add_task(f"cut {i}", "s", kind="work", tier="light", origin="user", priority=3) for i in range(3)]
        routine = p.db.add_task("routine", "s", kind="work", tier="light", origin="user", priority=1)
        lost = {_cut_run(p, tmp_path, tid, "an-earlier-boot", gone.pid) for tid in cut}
        p.db.set_kv(rampup.KEY, {"since": time.time(), "cause": "host reboot", "base": 0, "scope": None})
        began, seen = time.monotonic(), []
        fresh = lambda: sorted(r["task"] for r in p.db.q(   # noqa: E731
            "SELECT task FROM runs WHERE status='running' AND id>?", (max(lost),)))
        monkeypatch.setattr(d, "tick_prune_worktrees", lambda: seen.append(fresh()))
        d.tick()
        assert seen == [sorted(cut)], "the resumes start before the tick's later steps"
        assert time.monotonic() - began < 120
        assert {r["id"] for r in p.db.q("SELECT id FROM runs WHERE status='lost'")} == lost, \
            "a young lease from an earlier boot is no sign of life"
        assert fresh() == sorted(cut), \
            "every cut run resumes; new work waits for the ramp"
        assert all(p.db.task(t)["attempts"] == 0 for t in cut)
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_run_alive_on_this_boot_is_never_resumed_twice(env, monkeypatch, tmp_path, reap_tmp_processes):
    import subprocess
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=6)
    live = subprocess.Popen(["sleep", "60"])
    try:
        tid = p.db.add_task("running", "s", kind="work", tier="light", origin="user", priority=1)
        rid = _cut_run(p, tmp_path, tid, d.boot, live.pid, lease_age=999)
        for _ in range(2):
            d.reap_runs()
            d.resume_after_reboot()
            d.dispatch()
        assert [r["id"] for r in p.db.q("SELECT id FROM runs WHERE task=?", (tid,))] == [rid]
        assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "running"
        assert p.db.task(tid)["status"] == "running" and d._reboot_resumes() == set()
    finally:
        live.kill()
        live.wait()
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"
