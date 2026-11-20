# Records kept in a task's name (taskreap.py, Daemon.reap_task_records, devq.drop) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file.

def _treap_setup(env, devq_dir, status, age=600):
    """A project whose task `status` (changed `age` s ago) has a queued device job on a local runner and a
    reservation of an exclusive resource. The runner is not started, so the job stays queued."""
    from ttp import devq, hold, locks, taskreap
    p = make(env)
    p.set_config("device", {"runners": {"r1": {"dir": str(devq_dir), **DEVQ_FAST}}})
    tid = p.db.add_task("uses the device", kind="code")
    p.db.update_task(tid, status=status)
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - age, tid))
    devq._dirs(devq_dir)
    spec = {"id": "j1", "cmd": "true", "task": str(tid), "project": p.name, "config": "j1"}
    devq._write(devq_dir / "queue" / f"{time.time_ns():020d}-j1.json", json.dumps(spec))
    taskreap.record_job(p, "r1", spec)
    res = locks.reserve_path(p.state / "locks", "board")
    locks.reserve(res, hold.who(tid))
    return p, tid, res


def _treap_alerts(p):
    return p.db.q("SELECT text FROM messages WHERE direction='out' AND ref LIKE 'task-record:%'")


@pytest.mark.parametrize("status", ["cancelled", "failed", "done", "exhausted"])
def test_taskreap_an_ended_task_loses_its_device_job_and_reservation_with_one_alert_each(env, devq_dir, status):
    from ttp import daemon as dm, locks
    p, tid, res = _treap_setup(env, devq_dir, status)
    assert locks.reserved_by(res) == f"task #{tid}"
    dm.Daemon(p.base).reap_task_records()
    assert not list((devq_dir / "queue").glob("*.json"))
    marker = json.loads((devq_dir / "done" / "j1.json").read_text())
    assert marker["status"] == "cancelled" and f"task #{tid} is {status}" in marker["reason"]
    assert _devq("probe", devq_dir, "j1").returncode == 0     # a waiter's probe still answers
    assert not res.exists()
    texts = [m["text"] for m in _treap_alerts(p)]
    assert len(texts) == 2, texts
    assert any("device-queue job" in t and f"task #{tid}" in t for t in texts)
    assert any("resource reservation board" in t and f"task #{tid}" in t for t in texts)
    dm.Daemon(p.base).reap_task_records()                      # a restart: nothing left, nothing repeated
    assert len(_treap_alerts(p)) == 2
    assert not list((p.state / "devq-jobs").glob("*/*.json"))


def test_taskreap_a_daemon_restart_reaps_a_record_an_earlier_daemon_left(env, devq_dir):
    """A run lost to a restart (status 'lost') is not live: the new daemon's first sweep releases."""
    from ttp import daemon as dm
    p, tid, res = _treap_setup(env, devq_dir, "cancelled")
    p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(?,'worker','claude',?,'lost')",
           (tid, time.time() - 900))
    dm.Daemon(p.base).reap_task_records()
    assert json.loads((devq_dir / "done" / "j1.json").read_text())["status"] == "cancelled"
    assert not res.exists()


@pytest.mark.parametrize("case", ["live run", "grace", "open task", "waiter"])
def test_taskreap_keeps_the_records_of_a_starting_run_an_open_task_or_a_waited_on_job(env, devq_dir, case):
    from ttp import daemon as dm, locks
    p, tid, res = _treap_setup(env, devq_dir, "queued" if case == "open task" else "cancelled",
                               age=10 if case == "grace" else 600)
    if case == "live run":   # a run still starting: its row is running before its agent does anything
        p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(?,'worker','claude',?,'running')",
               (tid, time.time()))
    if case == "waiter":     # another open task's hand-off probes the job
        other = p.db.add_task("waits on it", kind="code")
        p.db.update_task(other, status="waiting", result=json.dumps({"retry_when": "ttp devq probe r1 j1"}))
    dm.Daemon(p.base).reap_task_records()
    assert len(list((devq_dir / "queue").glob("*.json"))) == 1
    assert not (devq_dir / "done" / "j1.json").exists()
    assert (locks.reserved_by(res) is not None) == (case != "waiter")
    assert len(_treap_alerts(p)) == (1 if case == "waiter" else 0)


def test_devq_drop_stops_a_running_job_and_leaves_other_projects_and_unrecorded_jobs(devq_dir):
    cfg = {"dir": str(devq_dir)}
    _devq_submit(devq_dir, cfg, "long", "sleep 60", task="7", project="demo")
    pid = _devq_job_pid(devq_dir, "long", timeout=_load_scaled(20))
    _devq_submit(devq_dir, cfg, "other", "true", task="7", project="elsewhere")
    _devq_submit(devq_dir, cfg, "legacy", "true", task="7")
    r = _devq("drop", devq_dir, "demo", "7", "task #7 is cancelled")
    assert r.returncode == 0 and json.loads(r.stdout.splitlines()[-1]) == [{"id": "long", "was": "running"}]
    marker = _devq_marker(devq_dir, "long")
    assert marker["status"] == "cancelled" and "task #7 is cancelled" in marker["reason"], marker
    deadline = time.time() + _load_scaled(10)
    while time.time() < deadline and _treap_pid_running(pid):
        time.sleep(0.05)
    assert not _treap_pid_running(pid)
    for job in ("other", "legacy"):   # another project's job, and one with no project recorded, still run
        assert _devq_marker(devq_dir, job)["status"] == "done"
    assert not list((devq_dir / "running").glob("*.cancel"))


def _treap_pid_running(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def test_taskreap_keeps_the_device_job_a_done_tasks_own_hand_off_names(env, devq_dir):
    """A done task's `continue:` follow-up names its long device job: kept, as jobstop.HANDED_OFF keeps
    detached jobs (a longer id such as j10 is not it)."""
    from ttp import daemon as dm, taskreap
    p, tid, res = _treap_setup(env, devq_dir, "done")
    result = json.dumps({"status": "done", "followups": [{"title": "continue: `ttp devq probe r1 j1`, report"}]})
    p.db.x("UPDATE tasks SET result=?, updated=? WHERE id=?", (result, time.time() - 600, tid))
    assert not taskreap.names("continue: probe j10 and j1x", "j1")
    dm.Daemon(p.base).reap_task_records()
    assert len(list((devq_dir / "queue").glob("*.json"))) == 1
    assert not (devq_dir / "done" / "j1.json").exists()
    assert list((p.state / "devq-jobs").glob("*/*.json"))
    assert not any("device-queue job" in m["text"] for m in _treap_alerts(p))
    assert not res.exists()   # its reservation is still released


@pytest.mark.parametrize("runner", ["old", "new"])
def test_taskreap_releases_a_running_job_only_when_its_runner_honours_the_drop(env, devq_dir, runner):
    """A live runner started before CANCEL existed (no runner.cancel naming its pid) runs the job on: the
    ledger entry stays, no alert, asked again after RETRY_S. One that advertises it is released."""
    import fcntl
    from ttp import daemon as dm, devq, taskreap
    p, tid, res = _treap_setup(env, devq_dir, "cancelled")
    next((devq_dir / "queue").glob("*.json")).replace(devq_dir / "running" / "j1.json")
    (devq_dir / "runner.pid").write_text("4242\n")
    if runner == "new":
        (devq_dir / devq.CAN_CANCEL).write_text("4242\n")
    with open(devq_dir / "runner.lock", "a") as lock:   # the live runner holds its lock
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        d = dm.Daemon(p.base)
        d.reap_task_records()
    assert (devq_dir / "running" / "j1.cancel").exists()
    jobs = [m["text"] for m in _treap_alerts(p) if "device-queue job" in m["text"]]
    if runner == "old":
        assert jobs == [] and list((p.state / "devq-jobs").glob("*/*.json"))
        assert d._taskreap_state[("r1", tid)] > time.time() + taskreap.RETRY_S - 60
    else:
        assert len(jobs) == 1 and "(running)" in jobs[0], jobs
        assert not list((p.state / "devq-jobs").glob("*/*.json"))


def test_taskreap_a_job_submitted_through_ttp_devq_is_dropped_by_the_daemon_tick(env, devq_dir):
    """The production path: `ttp devq submit` (spec project + ledger entry), the task ends, the daemon's
    tick drops the running job. Removing any of those wirings fails this."""
    from ttp import daemon as dm
    p = make(env)
    p.set_config("device", {"runners": {"r1": {"dir": str(devq_dir), **DEVQ_FAST}}})
    tid = p.db.add_task("uses the device", kind="code")
    r = _ttp_run(p, "devq", "submit", "r1", "--id", "j1", "--", "sleep", "60", env={"TTP_TASK": str(tid)})
    assert r.returncode == 0, r.stderr
    pid = _devq_job_pid(devq_dir, "j1", timeout=_load_scaled(20))
    p.db.update_task(tid, status="cancelled")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - 600, tid))
    dm.Daemon(p.base).tick()
    marker = _devq_marker(devq_dir, "j1", timeout=_load_scaled(20))
    assert marker["status"] == "cancelled" and f"task #{tid} is cancelled" in marker["reason"], marker
    deadline = time.time() + _load_scaled(10)
    while time.time() < deadline and _treap_pid_running(pid):
        time.sleep(0.05)
    assert not _treap_pid_running(pid)
    assert any("device-queue job (running)" in m["text"] for m in _treap_alerts(p))
