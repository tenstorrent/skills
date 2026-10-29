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
