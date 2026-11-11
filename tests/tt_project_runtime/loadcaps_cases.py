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


def _sustained(mc, sig, n=3, step=30.0, start=1000.0):
    """n readings `step` s apart through advance(): the last one, with its carried window."""
    prev = None
    for i in range(n):
        prev = mc.advance(prev, {**sig, "at": start + i * step})
    return prev


def test_machine_limit_each_signal_lowers_it():
    from ttp import machinecap as mc
    assert mc.limit(8, _IDLE) == 8 and mc.words(_IDLE) == "nothing busy"
    for change, want in [({"mem_free": 0.08}, 6), ({"mem_free": 0.04}, 4), ({"pressure": 2}, 6),
                         ({"pressure": 4}, 4), ({"psi": 6.0}, 6), ({"psi": 25.0}, 4), ({"swapping": True}, 6),
                         ({"thermal": 2}, 4), ({"thermal": 3}, 4)]:
        assert mc.limit(8, {**_IDLE, **change}) == want, change
    for change in [{"mem_free": 0.15}, {"compressor": 0.6}, {"psi": 3.0}, {"thermal": 1}, {"call": True},
                   {"cpu_busy": 0.99}]:
        assert mc.limit(8, {**_IDLE, **change}) == 8, f"{change} alone is not shortness"
    worst = _sustained(mc, {**_IDLE, "cpu_busy": 1.0, "mem_free": 0.01, "call": True, "thermal": 3})
    assert mc.limit(8, worst) == 1 and mc.limit(2, worst) == 1, "never below one"
    assert mc.words(worst) == "cpu heavy, memory heavy, call heavy, thermal heavy"


def test_machine_limit_load_and_moderate_memory_with_idle_cpu_do_not_lower_it(env, tmp_path):
    """(a) A load average of 40 with 70% CPU idle and macOS memory 'moderate' (compressing, 38% free,
    pressure normal) is not short: no machine hold for null-base projects, a set base stays whole."""
    from ttp import machinecap as mc
    sig = _sustained(mc, {**_IDLE, "load": 40.0, "cpu_busy": 0.3, "mem_free": 0.38, "pressure": 1,
                          "compressor": 0.45})
    assert mc.levels(sig) == {} and mc.limit(8, sig) == 8
    assert mc.in_force(None, [], sig) is None, "healthy, nothing set: only the projects' own caps"
    home, now = tmp_path / "mhome", time.time()
    null_cfg = {"machine": {"worker_limit": "on", "max_workers": None}}
    assert all(mc.check("a", null_cfg, n, claim=True, now=now, home=home, read=lambda: {**sig, "at": now}) is None
               for n in range(20))


def test_machine_limit_needs_cpu_idle_low_for_a_minute(env, tmp_path):
    """(b) One low-idle sample does not lower the limit; idle under 15% in every reading for 60 s does;
    a reading with idle back up, or a gap in readings, starts the window again."""
    from ttp import machinecap as mc
    hot = {**_IDLE, "cpu_busy": 0.9}
    one = mc.advance(None, {**hot, "at": 1000.0})
    assert mc.levels(one) == {} and mc.limit(8, one) == 8, "a single low sample"
    assert mc.levels(_sustained(mc, hot, n=2)) == {}, "30 s is not a minute"
    assert mc.limit(8, _sustained(mc, hot)) == 6 and mc.words(_sustained(mc, hot)) == "cpu moderate"
    assert mc.limit(8, _sustained(mc, {**hot, "cpu_busy": 0.97})) == 4, "idle under 5% too: heavy"
    broken = mc.advance(mc.advance(_sustained(mc, hot, n=2), {**_IDLE, "at": 1060.0}), {**hot, "at": 1090.0})
    assert mc.levels(broken) == {}, "idle came back in between"
    gap = mc.advance(mc.advance(None, {**hot, "at": 1000.0}), {**hot, "at": 1000.0 + mc.SUSTAIN_GAP_S + 1})
    assert mc.levels(gap) == {}, "readings too far apart prove nothing"
    # through check(): the shared reading is advanced once per SIGNAL_S across calls
    home, cfg, t0 = tmp_path / "mhome", _mcap_cfg(top=8), time.time()
    for i in range(3):
        now = t0 + i * mc.SIGNAL_S
        why = mc.check("a", cfg, 6, now=now, home=home, read=lambda: {**hot, "at": now})
        assert (why is None) == (i < 2), (i, why)
    assert "6 of 6" in why and "cpu moderate" in why


def test_machine_limit_memory_pressure_and_swapping_lower_it():
    """(c) Memory warn or critical lowers it, and so does swap growing between readings; a large but
    steady swap file does not."""
    from ttp import machinecap as mc
    assert mc.limit(8, {**_IDLE, "pressure": mc.PRESSURE_WARN}) == 6
    assert mc.limit(8, {**_IDLE, "pressure": mc.PRESSURE_CRITICAL}) == 4
    gb = 1 << 30
    steady = mc.advance(mc.advance(None, {**_IDLE, "swap": 4 * gb, "at": 1000.0}), {**_IDLE, "swap": 4 * gb, "at": 1030.0})
    assert not steady["swapping"] and mc.limit(8, steady) == 8
    grow = mc.advance(mc.advance(None, {**_IDLE, "swap": 4 * gb, "at": 1000.0}),
                      {**_IDLE, "swap": 4 * gb + mc.SWAP_GROW_B, "at": 1030.0})
    assert grow["swapping"] and mc.limit(8, grow) == 6 and mc.words(grow) == "memory moderate"
    old = mc.advance(mc.advance(None, {**_IDLE, "swap": 0, "at": 1000.0}),
                     {**_IDLE, "swap": 8 * gb, "at": 1000.0 + mc.SWAP_GAP_S + 1})
    assert not old["swapping"], "growth over a long gap tells nothing"
    assert mc.linux_swap(pathlib.Path("/nonexistent/vmstat")) is None


def test_machine_limit_thermal_serious_lowers_it():
    """(d) Thermal pressure serious (2) or worse lowers it; moderate (1) does not."""
    from ttp import machinecap as mc
    assert mc.limit(8, {**_IDLE, "thermal": 1}) == 8
    assert mc.limit(8, {**_IDLE, "thermal": 2}) == 4 and mc.words({**_IDLE, "thermal": 2}) == "thermal heavy"
    assert mc.in_force(None, [], {**_IDLE, "thermal": 2}, cpus=12) == 6, "under pressure a null base is the cores"


def test_machine_limit_call_counts_only_with_low_cpu_idle():
    """(e) A call app alone does not lower it; a call while CPU idle is under 30% does (no minute
    needed: a call stutters at once)."""
    from ttp import machinecap as mc
    assert mc.limit(8, {**_IDLE, "call": True}) == 8 and mc.limit(8, {**_IDLE, "call": True, "cpu_busy": 0.6}) == 8
    busy_call = {**_IDLE, "call": True, "cpu_busy": 0.75}
    assert mc.limit(8, busy_call) == 4 and mc.words(busy_call) == "call heavy"
    assert mc.limit(8, {**busy_call, "cpu_busy": None}) == 8, "a call with unreadable CPU is not short"


def test_machine_limit_unreadable_signals_hold_it_at_base(env, tmp_path):
    """(f) Unreadable signals count as not short: the limit stays at its base, never 1."""
    from ttp import machinecap as mc
    blind = {"cpu_busy": None, "mem_free": None, "pressure": None, "compressor": None, "call": None,
             "thermal": None, "swap": None}
    assert mc.levels(blind) == {} and mc.levels(mc.advance(None, blind)) == {}
    assert mc.limit(8, blind) == 8 and mc.limit(8, {}) == 8
    assert mc.in_force(5, [], blind) == 5 and mc.in_force(None, [], blind) is None
    assert mc.base({}, cpus=64) == mc.BASE_MAX and mc.base({}, cpus=1) == mc.BASE_MIN
    assert mc.base({"machine": {"max_workers": 0}}) == 1 and mc.base({"machine": {"max_workers": "x"}}, cpus=8) == 8
    assert mc.set_base({"machine": {"max_workers": None}}) is None and mc.set_base(_mcap_cfg(top=3)) == 3
    assert mc.psi(tmp_path / "none") is None and mc.linux_thermal(tmp_path / "none") is None
    zone = tmp_path / "thermal" / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "temp").write_text("85000\n")
    assert mc.linux_thermal(tmp_path / "thermal") == 1
    (tmp_path / "vmstat").write_text("pgpgin 10\npswpout 3\n")
    assert mc.linux_swap(tmp_path / "vmstat") == 3 * 4096
    # a corrupt state file is replaced, never trusted; blind readings hold a set base of 2 at 2
    home = tmp_path / "mhome"
    home.mkdir()
    (home / mc.STATE).write_text("{not json")
    assert mc.check("a", _mcap_cfg(top=2), 0, claim=True, home=home, read=lambda: dict(blind, at=time.time())) is None
    assert mc.check("a", _mcap_cfg(top=2), 1, claim=True, home=home, read=lambda: dict(blind, at=time.time())) is None
    assert mc.check("a", _mcap_cfg(top=2), 2, home=home, read=lambda: dict(blind, at=time.time()))
    assert json.loads((home / mc.STATE).read_text())["projects"]["a"]["running"] == 2


def test_machine_limit_null_project_does_not_drag_the_others_down(env, tmp_path):
    """A project left at null publishes no base, so it never pulls the machine limit under another's;
    an older daemon's entry without a base still counts by its published limit."""
    from ttp import machinecap as mc
    home, now = tmp_path / "mhome", time.time()
    sig = dict(_IDLE, at=now)
    null_cfg = {"machine": {"worker_limit": "on", "max_workers": None}}
    mc.publish("nul", null_cfg, 1, now=now, home=home)
    assert json.loads((home / mc.STATE).read_text())["projects"]["nul"]["base"] is None
    assert mc.check("set", _mcap_cfg(top=6), 4, now=now, home=home, read=lambda: sig) is None, "1 + 4 < 6"
    assert mc.check("set", _mcap_cfg(top=6), 5, now=now, home=home, read=lambda: sig), "1 + 5 = 6"
    assert mc.check("nul", null_cfg, 30, now=now, home=home, read=lambda: sig) is None, "healthy: no machine hold"
    data = json.loads((home / mc.STATE).read_text())
    data["projects"]["old"] = {"pid": os.getpid(), "at": now, "running": 0, "limit": 3}
    (home / mc.STATE).write_text(json.dumps(data))
    assert mc.check("nul", null_cfg, 2, now=now, home=home, read=lambda: sig) is None
    assert mc.check("nul", null_cfg, 3, now=now, home=home, read=lambda: sig), "the old entry's 3 binds"


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


@pytest.mark.parametrize("word", ["null", "none", "auto", "Auto", None])
def test_machine_max_workers_can_be_unset_back_to_its_default(env, monkeypatch, word):
    """machine.max_workers null, none or auto (config_set or ttp config) is unset: the default base (half
    the cores, 2 to 8) applies again; 0, -1, 1.5, true and words stay refused."""
    from ttp import cli
    from ttp import coordinator as coord
    from ttp import machinecap as mc
    p = make(env)
    _config_clear_run_env(monkeypatch)
    assert coord.apply(p, [{"type": "config_set", "key": "machine.max_workers", "value": 3}]) == []
    assert p.raw_config()["machine"]["max_workers"] == 3
    assert coord.apply(p, [{"type": "config_set", "key": "machine.max_workers", "value": word}]) == []
    assert p.raw_config()["machine"]["max_workers"] is None and mc.set_base(p.config()) is None
    cli.main(["config", p.name, "machine.max_workers", "2"])
    assert p.raw_config()["machine"]["max_workers"] == 2
    cli.main(["config", p.name, "machine.max_workers", word or "null"])
    assert p.raw_config()["machine"]["max_workers"] is None
    for bad in (0, -1, 1.5, True, "lots"):
        assert coord.apply(p, [{"type": "config_set", "key": "machine.max_workers", "value": bad}]), bad
        with pytest.raises(SystemExit):
            cli.main(["config", p.name, "machine.max_workers", json.dumps(bad) if bad != "lots" else bad])
    assert p.raw_config()["machine"]["max_workers"] is None


def test_machine_limit_publish_alone_binds_a_lower_base_and_records_its_limit(tmp_path):
    """Each dispatch pass publishes a limit-on project's base and its limit from the stored reading, so a
    lower machine.max_workers binds the others before that project starts anything (its entry dropped or
    never claimed); with the limit off it publishes neither."""
    from ttp import machinecap as mc
    home, now = tmp_path / "mhome", time.time()
    sig = dict(_IDLE, at=now)
    assert mc.check("big", _mcap_cfg(top=8), 3, now=now, home=home, read=lambda: sig) is None, "alone: 3 < 8"
    mc.publish("small", _mcap_cfg(top=2), 0, now=now, home=home)   # no claim, nothing started
    entry = json.loads((home / mc.STATE).read_text())["projects"]["small"]
    assert entry["base"] == 2 and entry["limit"] == 2, entry
    assert mc.check("big", _mcap_cfg(top=8), 1, now=now, home=home, read=lambda: sig) is None
    assert mc.check("big", _mcap_cfg(top=8), 2, now=now, home=home, read=lambda: sig), "small's 2 binds"
    data = json.loads((home / mc.STATE).read_text())   # a heavy reading stored: the limit published is cut
    data["signals"] = _sustained(mc, dict(_IDLE, cpu_busy=0.99))
    (home / mc.STATE).write_text(json.dumps(data))
    mc.publish("small", _mcap_cfg(top=4), 0, now=now, home=home)
    stored = json.loads((home / mc.STATE).read_text())
    assert stored["projects"]["small"]["limit"] == mc.limit(4, stored["signals"]) < 4
    mc.publish("small", _mcap_cfg(on="off", top=2), 0, now=now, home=home)
    entry = json.loads((home / mc.STATE).read_text())["projects"]["small"]
    assert "base" not in entry and "limit" not in entry and entry["running"] == 0


def test_machine_limit_publish_follows_a_changed_max_workers_without_a_new_claim(tmp_path):
    """A project whose last claim published a low limit (14 x 0.75 = 10 under moderate memory pressure)
    publishes the limit from its current machine.max_workers on its next dispatch pass, with no new claim:
    raised to 20 it publishes 15 and no longer holds the others at 10; lowered to 8 it publishes 6 and binds
    them at once; a dead daemon's entry still counts for nothing."""
    from ttp import machinecap as mc
    home, now = tmp_path / "mhome", time.time()
    sig = dict(_IDLE, psi=6, at=now)   # memory moderate: x0.75
    assert mc.levels(sig) == {"memory": "moderate"}
    assert mc.check("a", _mcap_cfg(top=14), 2, claim=True, now=now, home=home, read=lambda: sig) is None
    entry = json.loads((home / mc.STATE).read_text())["projects"]["a"]
    assert entry["base"] == 14 and entry["limit"] == 10, entry
    mc.publish("a", _mcap_cfg(top=20), 3, now=now, home=home)   # raised, nothing started
    entry = json.loads((home / mc.STATE).read_text())["projects"]["a"]
    assert entry["base"] == 20 and entry["limit"] == 15 and entry["running"] == 3, entry
    assert mc.check("b", _mcap_cfg(top=20), 9, now=now, home=home, read=lambda: sig) is None, "12 of 15"
    why = mc.check("b", _mcap_cfg(top=20), 12, now=now, home=home, read=lambda: sig)
    assert why and "15 of 15" in why, why
    mc.publish("a", _mcap_cfg(top=8), 3, now=now, home=home)    # lowered, nothing started
    entry = json.loads((home / mc.STATE).read_text())["projects"]["a"]
    assert entry["base"] == 8 and entry["limit"] == 6, entry
    why = mc.check("b", _mcap_cfg(top=20), 3, now=now, home=home, read=lambda: sig)
    assert why and "6 of 6" in why, why
    data = json.loads((home / mc.STATE).read_text())
    data["projects"]["a"]["pid"] = 2 ** 22 + 12345   # a's daemon is gone: its low base binds nobody
    (home / mc.STATE).write_text(json.dumps(data))
    assert mc.check("b", _mcap_cfg(top=20), 14, now=now, home=home, read=lambda: sig) is None, "14 of 15"
    assert "a" not in json.loads((home / mc.STATE).read_text())["projects"]


def test_machine_pace_from_heavy_ignores_moderate_readings_and_other_projects_bases(env, tmp_path):
    """machine.pace_from heavy: moderate CPU or memory no longer cut the project's limit (heavy ones, a
    call and heat still do), and it holds the machine to its own base, not the lowest another project
    set; moderate (the default) keeps today's behaviour for the same readings and bases."""
    from ttp import machinecap as mc
    heavy = lambda top: {"machine": {"worker_limit": "on", "max_workers": top, "pace_from": "heavy"}}
    assert mc.pace_from({}) == "moderate" and mc.pace_from(_mcap_cfg()) == "moderate"
    assert mc.pace_from({"machine": {"pace_from": "bogus"}}) == "moderate"
    assert mc.pace_from(heavy(4)) == "heavy" and mc.pace_from({"machine": {"pace_from": " Heavy "}}) == "heavy"
    cpu_mod = _sustained(mc, dict(_IDLE, cpu_busy=0.90))       # idle 10%: moderate
    cpu_heavy = _sustained(mc, dict(_IDLE, cpu_busy=0.99))     # idle 1%: heavy
    for sig, mod, hvy in [(cpu_mod, 10, 14), ({**_IDLE, "mem_free": 0.08}, 10, 14), ({**_IDLE, "swapping": True}, 10, 14),
                          ({**_IDLE, "psi": 6.0}, 10, 14), (cpu_heavy, 7, 7), ({**_IDLE, "mem_free": 0.04}, 7, 7),
                          ({**_IDLE, "thermal": 2}, 7, 7), ({**cpu_mod, "call": True}, 5, 7)]:
        assert mc.limit(14, sig) == mod and mc.limit(14, sig, "heavy") == hvy, sig
    assert mc.words(cpu_mod, "heavy") == "nothing busy" and mc.words(cpu_mod) == "cpu moderate"
    assert mc.in_force(None, [], cpu_mod, pace="heavy") is None, "no base set and nothing heavy: no hold"
    assert mc.in_force(14, [10], _IDLE) == 10 and mc.in_force(14, [10], _IDLE, pace="heavy") == 14

    home, now = tmp_path / "mhome", 1060.0
    sig = dict(cpu_mod, at=now)
    mc.publish("mod", _mcap_cfg(top=10), 3, now=now, home=home)   # another project's lower base
    data = json.loads((home / mc.STATE).read_text())
    data["signals"] = sig   # the stored machine reading: CPU idle 10% for a minute
    (home / mc.STATE).write_text(json.dumps(data))
    assert mc.check("agents", _mcap_cfg(top=14), 6, now=now, home=home, read=lambda: sig), \
        "moderate: 3 + 6 >= the other's 10 cut to 7 by moderate CPU"
    assert mc.check("agents", heavy(14), 10, now=now, home=home, read=lambda: sig) is None, "heavy: 3 + 10 < 14"
    why = mc.check("agents", heavy(14), 11, now=now, home=home, read=lambda: sig)
    assert why and "14 of 14 running" in why and "machine.pace_from heavy" in why and "nothing busy" in why
    mc.publish("agents", heavy(14), 2, now=now, home=home)
    assert json.loads((home / mc.STATE).read_text())["projects"]["agents"]["limit"] == 14, "heavy: moderate CPU uncut"
    assert mc.check("mod", _mcap_cfg(top=10), 4, now=now, home=home, read=lambda: sig) is None, "2 + 4 < 7"
    assert mc.check("mod", _mcap_cfg(top=10), 5, now=now, home=home, read=lambda: sig), \
        "the moderate project still holds itself to its own paced limit: 2 + 5 >= 7"


def test_machine_pace_from_is_settable_and_checked(env):
    from ttp import coordinator as co
    from ttp import project as pj
    assert pj.DEFAULT_CONFIG["machine"]["pace_from"] == "moderate", "the shipped default stays moderate"
    assert co.USER_SETTABLE["machine.pace_from"](" Heavy") == "heavy"
    assert co.USER_SETTABLE["machine.pace_from"]("moderate") == "moderate"
    with pytest.raises(ValueError):
        co.USER_SETTABLE["machine.pace_from"]("light")
    assert not [w for w in pj.config_problems({"machine": {"pace_from": "heavy"}}) if "pace_from" in w]
    assert [w for w in pj.config_problems({"machine": {"pace_from": "light"}}) if "pace_from" in w]
