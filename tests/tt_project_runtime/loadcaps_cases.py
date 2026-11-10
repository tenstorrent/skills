# Defaults sized to real load (loadcaps.py) of tests/test_tt_project_runtime.py, which runs this file in
# its own namespace (see _include_cases there): helpers, fixtures and imports come from that file, and
# pytest collects these tests under its name. Add tests of convenience caps' defaults, their upgrade
# migration and Daemon.check_cap_wait here.
from __future__ import annotations


def test_review_sweep_and_review_stall_defaults_hold_ready_work_minutes_not_hours():
    from ttp.project import DEFAULT_CONFIG
    assert 0 < DEFAULT_CONFIG["review"]["sweep_after_s"] <= 300
    assert 0 < DEFAULT_CONFIG["coordinator"]["review_stall_s"] <= 1800


def test_the_review_sweep_queues_a_left_review_after_five_minutes_by_default(env):
    from ttp.daemon import Daemon
    p = make(env)
    tid, _, _, reviews = _finish_code(env, p, "noted fix", {"app.py": 20}, result={
        "status": "needs_review", "summary": "noted fix works", "followups": [{"title": "later", "spec": "more"}]})
    assert p.db.task(tid)["status"] == "review" and not reviews
    _ran(p, tid, time.time() - 360)
    _sweep(Daemon(p.base))
    assert len(p.db.q("SELECT id FROM tasks WHERE kind='review'")) == 1, "6 min after its run: swept"


def test_a_dependency_in_review_half_an_hour_is_raised_by_default(env):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    rev = p.db.add_task("needs a look", "s", origin="user")
    p.db.update_task(rev, status="review")
    p.db.add_task("child", "s", origin="user", depends_on=[rev])
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
           (time.time() - 35 * 60, f"task:{rev}", "task_review", "normal", "handed off", "handled", rev))
    d._raise_review_stalls(time.time())
    assert len(p.db.q("SELECT id FROM events WHERE kind='review_stall'")) == 1


def test_the_unreviewed_sweep_queues_one_review_per_worker_slot_by_default(env):
    from ttp import daemon as dm
    p = make(env)
    tids = [_unreviewed(env, p, f"change {i}")[0] for i in range(3)]
    assert dm.Daemon(p.base).sweep_unreviewed(force=True) == tids, "6 worker slots: all 3 in one pass"
    p.set_config("budget.max_parallel_workers", 1)
    more = [_unreviewed(env, p, f"later {i}")[0] for i in range(2)]
    assert dm.Daemon(p.base).sweep_unreviewed(force=True) == more[:1]
    p.set_config("review.unreviewed_per_pass", 2)      # set by the project: kept
    p.set_config("budget.max_parallel_workers", 6)
    assert dm.Daemon(p.base).sweep_unreviewed(force=True) == more[1:]


def test_the_first_unreviewed_sweep_after_an_upgrade_logs_its_backlog_once(env):
    """The first pass at a runtime version logs how many done code tasks have no landing, push-queue
    entry or superseded record (a task the queue holds is not one); later passes and restarts at the
    same version log nothing more, and an upgrade counts again."""
    from ttp import daemon as dm
    p = make(env)
    p.set_config("review.unreviewed_per_pass", 1)
    tids = [_unreviewed(env, p, f"change {i}")[0] for i in range(3)]
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated) VALUES(?,?,?,?,?,?,?)",
           (tids[0], p.db.task(tids[0])["branch"], "0" * 40, "origin/main", "approved", time.time(), time.time()))
    log = lambda: (p.logs / "daemon.log").read_text() if (p.logs / "daemon.log").exists() else ""
    dm.Daemon(p.base).sweep_unreviewed(force=True)
    counted = [ln for ln in log().splitlines() if "unreviewed sweep, first pass" in ln]
    assert len(counted) == 1 and "2 done code task(s)" in counted[0], counted
    dm.Daemon(p.base).sweep_unreviewed(force=True)
    assert log().count("unreviewed sweep, first pass") == 1, "a restart at the same version counted again"
    p.db.set_kv(dm.KV_UNREVIEWED_COUNTED, "0.0.1")
    dm.Daemon(p.base).sweep_unreviewed(force=True)
    assert log().count("unreviewed sweep, first pass") == 2, "an upgrade did not count"


def test_device_max_tasks_is_derived_from_the_projects_devices_and_worker_slots(env):
    from ttp import loadcaps
    from ttp.daemon import Daemon
    assert loadcaps.device_max_tasks({}, 0) == loadcaps.device_max_tasks({}, 1) == 2
    assert loadcaps.device_max_tasks({}, 5) == 5 and loadcaps.device_max_tasks({}, 30) == 6
    assert loadcaps.device_max_tasks({"budget": {"max_parallel_workers": 12}}, 30) == 12
    assert loadcaps.device_max_tasks({"device": {"max_tasks": 1}}, 30) == 1, "a set value is kept"
    assert loadcaps.device_max_tasks({"device": {"max_tasks": "x"}}, 4) == 4, "unreadable: derived"
    p = make(env)
    p.set_config("device.locks", ["device-a"])
    p.set_config("resources", {f"box{i}-device": 1 for i in range(4)})
    assert Daemon(p.base)._device_max_tasks() == 5, "4 device resources and the device lock"
    p.set_config("device.max_tasks", 3)
    assert Daemon(p.base)._device_max_tasks() == 3


def test_upgrade_migrates_only_old_defaults_and_a_rerun_changes_nothing(env):
    from ttp import cli
    p = make(env)
    assert cli._migrate_load_caps(p) == "", "nothing set: nothing to change"
    p.set_config("device.max_tasks", 2)
    p.set_config("device.locks", ["device-a"])
    p.set_config("review.sweep_after_s", 1800)
    p.set_config("review.unreviewed_per_pass", 4)       # the project's own value
    p.set_config("coordinator.review_stall_s", 14400)
    p.set_config("budget.max_parallel_workers", 2)
    before = p.raw_config()
    note = cli._migrate_load_caps(p)
    assert "device.max_tasks 2" in note and "review.sweep_after_s 1800" in note \
        and "coordinator.review_stall_s 14400" in note and "unreviewed_per_pass" not in note, note
    raw = p.raw_config()
    assert "max_tasks" not in raw["device"] and raw["device"]["locks"] == ["device-a"]
    assert "sweep_after_s" not in raw["review"] and raw["review"]["unreviewed_per_pass"] == 4
    assert "review_stall_s" not in raw["coordinator"] and raw["budget"]["max_parallel_workers"] == 2
    assert {k: v for k, v in before.items() if k not in ("device", "review", "coordinator")} == \
        {k: v for k, v in raw.items() if k not in ("device", "review", "coordinator")}
    cfg = p.config()
    assert cfg["review"]["sweep_after_s"] == 300 and cfg["coordinator"]["review_stall_s"] == 1800
    text = p.config_path.read_text()
    assert cli._migrate_load_caps(p) == "" and p.config_path.read_text() == text, "a rerun is a no-op"


def test_upgrade_migration_over_a_broken_project_json_keeps_the_broken_bytes(env):
    """The load-caps migration that writes the last good copy over a project.json that does not read
    keeps the broken hand edit in project.json.broken-<stamp> first (as set_config does)."""
    from ttp import cli
    from ttp.project import Project
    p = make(env)
    p.set_config("device.max_tasks", 2)
    p.config()
    p.config_path.write_text("{ broken")
    assert "device.max_tasks 2" in cli._migrate_load_caps(Project(p.base))
    kept = list(p.harness.glob("project.json.broken-*"))
    assert [k.read_text() for k in kept] == ["{ broken"]
    assert "max_tasks" not in json.loads(p.config_path.read_text()).get("device", {})
    assert str(kept[0]) in (p.logs / "daemon.log").read_text()


def test_a_device_task_held_by_the_device_cap_with_the_device_idle_raises_one_cap_wait(env, monkeypatch):
    from ttp import loadcaps
    from ttp.daemon import Daemon
    p = _device_project(env)
    p.set_config("device.max_tasks", 1)
    _dev_task(p, ["needs_device"], status="running", title="preparing")
    q = _dev_task(p, ["resource:device-a"], title="held by the cap")
    d = Daemon(p.base)
    start = time.time()

    def check(minutes):
        d._cap_wait_checked = start + minutes * 60 - 60
        monkeypatch.setattr(time, "time", lambda: start + minutes * 60)
        d.check_cap_wait()
        return p.db.q("SELECT * FROM events WHERE kind=?", (loadcaps.CAP_WAIT,))

    assert check(0) == [] and check(4) == []
    (ev,) = check(5)
    assert ev["task"] == q["id"] and "device.max_tasks (1)" in ev["text"] and ev["severity"] == "normal"
    assert len(check(6)) == 1 and len(check(30)) == 1, "one per stay in the queue"
    # The device lock taken: its capacity is not idle, so the clock stops.
    from ttp import locks
    f = locks.try_take(d._slot_paths("device-a"), "a device job")
    try:
        assert len(check(31)) == 1 and q["id"] not in d._cap_waits
    finally:
        f.close()
    # The cap raised: it holds nothing.
    p.set_config("device.max_tasks", 5)
    d.cfg = p.config()
    assert len(check(32)) == 1 and q["id"] not in d._cap_waits



def test_a_short_external_wait_keeps_its_unfinished_head_unreviewed_while_review_is_swept_at_5_min(env):
    # review.sweep_after_s (300) paces only tasks in review: a worker that committed part of its
    # work and waits on a busy lock (external) gets no review of that head for unreviewed_wait_s.
    from ttp import daemon as dm
    p = make(env)
    busy = {"status": "waiting", "summary": "half done", "waiting_for": "device-a busy",
            "retry_when": "ttp lock --probe device-a", "wait_kind": "external"}
    waiting, _, _ = _unreviewed(env, p, "waits on a lock", status="queued", age=600, result=busy)
    assert dm.Daemon(p.base).sweep_unreviewed(force=True) == [], "10 min external wait: not reviewed"
    tid, _, _, reviews = _finish_code(env, p, "left in review", {"app.py": 20}, result={
        "status": "needs_review", "summary": "it works", "followups": [{"title": "later", "spec": "more"}]})
    assert p.db.task(tid)["status"] == "review" and not reviews
    _ran(p, tid, time.time() - 360)
    _sweep(dm.Daemon(p.base))
    assert [r["depends_on"] for r in p.db.q("SELECT depends_on FROM tasks WHERE kind='review'")] == [f"[{tid}]"]
    long_wait, _, _ = _unreviewed(env, p, "waits long", status="queued", age=2400, result=busy)
    assert dm.Daemon(p.base).sweep_unreviewed(force=True) == [long_wait], "40 min: its head is reviewed"
    assert not _daemon_reviews(p, waiting)


# -- the machine-wide worker limit (machinecap.py) -------------------------------------------------

_IDLE = {"cpu_busy": 0.1, "mem_free": 0.6, "pressure": 1, "compressor": 0.05, "call": False, "thermal": 0}


def _mcap_cfg(on="on", top=4):
    return {"machine": {"worker_limit": on, "max_workers": top}}


def test_machine_limit_sum_over_two_projects_never_passes_it(env, tmp_path):
    """Two projects claiming starts in turn: the sum stops at the limit, whichever project asks."""
    from ttp import machinecap as mc
    home, cfg, now = tmp_path / "mhome", _mcap_cfg(top=5), time.time()
    sig = dict(_IDLE, at=now)
    running = {"a": 0, "b": 0}
    for _ in range(10):
        for name in running:
            if mc.check(name, cfg, running[name], claim=True, now=now, home=home, read=lambda: sig) is None:
                running[name] += 1
            assert sum(running.values()) <= 5
    assert sum(running.values()) == 5 and min(running.values()) >= 2
    why = mc.check("b", cfg, running["b"], now=now, home=home, read=lambda: sig)
    assert why and "5 of 5 running across all projects" in why and "machine.worker_limit" in why
    # a finished worker (published each dispatch pass) frees a place for the other project
    mc.publish("a", cfg, running["a"] - 1, now=now, home=home)
    assert mc.check("b", cfg, running["b"], now=now, home=home, read=lambda: sig) is None
    # a dead daemon's entry no longer counts
    data = json.loads((home / mc.STATE).read_text())
    data["projects"]["a"]["pid"] = 2 ** 22 + 12345
    (home / mc.STATE).write_text(json.dumps(data))
    assert mc.check("b", cfg, 4, now=now, home=home, read=lambda: sig) is None


def test_machine_limit_project_cap_above_it_is_clipped(env, tmp_path):
    """A project whose own cap (6) is above the machine limit (3) gets only the limit's places, and the
    lowest limit a live project publishes binds the others."""
    from ttp import machinecap as mc
    home, now = tmp_path / "mhome", time.time()
    sig = dict(_IDLE, at=now)
    started = 0
    for _ in range(6):   # budget.max_parallel_workers 6
        if mc.check("big", _mcap_cfg(top=3), started, claim=True, now=now, home=home, read=lambda: sig) is None:
            started += 1
    assert started == 3
    mc.publish("big", _mcap_cfg(top=3), 0, now=now, home=home)
    mc.check("small", _mcap_cfg(top=2), 0, claim=True, now=now, home=home, read=lambda: sig)
    assert mc.check("big", _mcap_cfg(top=8), 1, now=now, home=home, read=lambda: sig), \
        "small's limit of 2 caps the machine: 1 + 1 running"


def test_machine_limit_each_signal_lowers_it():
    from ttp import machinecap as mc
    assert mc.limit(8, _IDLE) == 8 and mc.words(_IDLE) == "nothing busy"
    for change, want in [({"cpu_busy": 0.8}, 6), ({"cpu_busy": 0.95}, 4),
                         ({"mem_free": 0.15}, 6), ({"mem_free": 0.05}, 4), ({"pressure": 2}, 6),
                         ({"pressure": 4}, 4), ({"compressor": 0.35}, 6), ({"compressor": 0.6}, 4),
                         ({"psi": 6.0}, 6), ({"psi": 25.0}, 4),
                         ({"call": True}, 4), ({"thermal": 1}, 6), ({"thermal": 2}, 4)]:
        assert mc.limit(8, {**_IDLE, **change}) == want, change
    worst = {**_IDLE, "cpu_busy": 1.0, "mem_free": 0.01, "call": True, "thermal": 3}
    assert mc.limit(8, worst) == 1 and mc.limit(2, worst) == 1, "never below one"
    assert mc.words(worst) == "cpu heavy, memory heavy, call heavy, thermal heavy"


def test_machine_limit_unreadable_signals_fall_back_to_a_lower_finite_limit(env, tmp_path):
    from ttp import machinecap as mc
    blind = {"cpu_busy": None, "mem_free": None, "pressure": None, "compressor": None, "call": None,
             "thermal": None}
    assert mc.levels(blind) == {"cpu": "moderate", "memory": "moderate"}
    assert 1 <= mc.limit(8, blind) == 4 < 8 and mc.limit(8, {}) == 4, "unknown is never unlimited"
    assert mc.base({}, cpus=64) == mc.BASE_MAX and mc.base({}, cpus=2) == mc.BASE_MIN
    assert mc.base({"machine": {"max_workers": 0}}) == 1 and mc.base({"machine": {"max_workers": "x"}}, cpus=8) == 4
    assert mc.psi(tmp_path / "none") is None and mc.linux_thermal(tmp_path / "none") is None
    zone = tmp_path / "thermal" / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "temp").write_text("85000\n")
    assert mc.linux_thermal(tmp_path / "thermal") == 1
    # a corrupt state file is replaced, never trusted
    home = tmp_path / "mhome"
    home.mkdir()
    (home / mc.STATE).write_text("{not json")
    assert mc.check("a", _mcap_cfg(top=2), 0, claim=True, home=home, read=lambda: dict(blind, at=time.time())) is None
    assert json.loads((home / mc.STATE).read_text())["projects"]["a"]["running"] == 1


def test_machine_limit_is_on_for_laptops_and_off_for_servers(tmp_path):
    from ttp import machinecap as mc
    assert mc.enabled({}, darwin=True, battery=lambda: False)
    assert mc.enabled({}, darwin=False, battery=lambda: True)
    assert not mc.enabled({}, darwin=False, battery=lambda: False)
    assert not mc.enabled(_mcap_cfg("off"), darwin=True) and mc.enabled(_mcap_cfg("on"), darwin=False)
    bat = tmp_path / "ps" / "BAT0"
    bat.mkdir(parents=True)
    (bat / "type").write_text("Battery\n")
    assert mc.has_battery(tmp_path / "ps") and not mc.has_battery(tmp_path / "missing")
    assert mc.check("a", _mcap_cfg("off", 1), 50, read=lambda: {}) is None, "off holds nothing"


def test_machine_limit_reads_a_call_from_power_assertions():
    from ttp import machinecap as mc
    zoom = ("   pid 9149(zoom.us): [0x0001] 00:10:00 NoDisplaySleepAssertion named: \"zoom.us is "
            "preventing display sleep\"\n")
    meet = "   pid 77(Google Chrome): [0x02] 00:01:00 NoDisplaySleepAssertion named: \"WebRTC has active PeerConnections\"\n"
    video = "   pid 77(Google Chrome): [0x02] 00:01:00 NoDisplaySleepAssertion named: \"Video Wake Lock\"\n"
    idle = "   pid 120(powerd): [0x03] 00:00:00 PreventUserIdleSystemSleep named: \"Powerd - Prevent sleep\"\n"
    assert mc.in_call(zoom) and mc.in_call(meet)
    assert mc.in_call(video) is False and mc.in_call(idle) is False and mc.in_call("") is False


def test_machine_limit_holds_a_dispatch_start_with_its_words(env, monkeypatch):
    """The daemon checks the machine limit before each start and claims a place when it starts one."""
    from ttp import machinecap as mc
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("machine.worker_limit", "on")
    p.set_config("machine.max_workers", 2)
    monkeypatch.setattr(mc, "read_signals", lambda darwin=None: dict(_IDLE, at=time.time()))
    d = Daemon(p.base)
    core = d.cfg.get("core_provider", "claude")
    t = p.db.task(p.db.add_task("light work", "s", kind="work", tier="light", origin="user", priority=2))

    def ctx(n):
        return {"now": time.time(), "paused": {}, "committed": None, "busy": {core: n}, "logged_out": {}}
    c = ctx(2)
    assert d._start_hold(t, core, c) == "held"
    assert "machine-wide worker limit: 2 of 2" in d._hold_words(t, "held", c)
    assert d._start_hold(t, core, ctx(1)) is None
    assert d._start_hold(t, core, ctx(1), reserve=True) is None
    entry = json.loads((mc._home() / mc.STATE).read_text())["projects"][p.name]
    assert entry["running"] == 2 and entry["limit"] == 2, "a start claims its place"
    p.set_config("machine.worker_limit", "off")
    assert Daemon(p.base)._start_hold(t, core, ctx(5)) is None, "off: only the project's own cap"
