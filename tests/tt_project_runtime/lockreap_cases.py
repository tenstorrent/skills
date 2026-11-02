# Stale-lock sweep and lock-wait alert tests (lockreap.py, Daemon.reap_locks) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file. A fake process table stands in for real processes.

def _reap_slot(path, **label):
    """Hold slot file `path` (an exclusive flock on an open file of this process) labelled `label`."""
    import fcntl
    from ttp import locks
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    f.seek(0)
    f.truncate()
    f.write(json.dumps({"holder": "task #7 (run 70)", "since": time.time() - 3600, "command": "make gate",
                        "node": os.uname().nodename, "pidns": locks._pid_ns(), **label}))
    f.flush()
    return f


class _ReapProcs:
    """A fake process table: pid -> {"start", "pgid", "env", "cmd"}; `fds` lists who has each lock open."""

    def __init__(self, monkeypatch, procs: dict, fds: list | None):
        from ttp import lockreap, locks
        self.procs, self.fds, self.sent = procs, fds, []
        monkeypatch.setattr(locks, "_alive", lambda pid: int(pid) in self.procs)
        monkeypatch.setattr(locks, "kthread", lambda pid, proc="/proc": False)
        monkeypatch.setattr(locks, "start_time", lambda pid, proc="/proc": (self.procs.get(int(pid)) or {}).get("start"))
        monkeypatch.setattr(lockreap, "_pgid", lambda pid: (self.procs.get(pid) or {}).get("pgid"))
        monkeypatch.setattr(lockreap, "environ", lambda pid: (self.procs.get(pid) or {}).get("env") or {})
        monkeypatch.setattr(lockreap, "cmdline", lambda pid: (self.procs.get(pid) or {}).get("cmd") or "")
        monkeypatch.setattr(lockreap, "fd_holders", lambda path: None if self.fds is None else list(self.fds))

        def _signal(pid, start, sig):
            if (self.procs.get(pid) or {}).get("start") != start:
                return False
            self.sent.append((pid, sig))
            return True
        monkeypatch.setattr(lockreap, "_signal", _signal)


def _reap_sweep(paths, now0=1000.0):
    from ttp import lockreap
    r = lockreap.Reaper()
    first = r.sweep(paths, now0)
    assert not first["released"], "nothing is reaped before the grace"
    return r.sweep(paths, now0 + lockreap.GRACE_S + 1)


def test_lockreap_releases_a_lock_whose_holder_pid_is_gone(env, monkeypatch, tmp_path):
    import signal as sg
    from ttp import audit, lockreap
    p = make(env)
    path = tmp_path / "locks" / "gate.0.lock"
    f = _reap_slot(path, pid=4100, pid_start="11", pgid=4100, run_id="70", task_id="7")
    try:
        fake = _ReapProcs(monkeypatch, {5200: {"start": "90", "pgid": 5200, "env": {"TTP_RUN_ID": "70", "TTP_TASK": "7"},
                                           "cmd": "build-server --daemon"}}, [5200])
        got = _reap_sweep(lockreap.slot_files([tmp_path / "locks"]))
        assert fake.sent == [(5200, sg.SIGTERM)], fake.sent
        [rel] = got["released"]
        assert rel["lock"] == "gate.0.lock" and rel["pid"] == 4100 and "gone" in rel["dead_why"]
        lockreap.record(p.db, got["released"])
        ev = p.db.one("SELECT text, status FROM events WHERE kind=?", (lockreap.EVENT_KIND,))
        assert "gate.0.lock" in ev["text"] and "pid 4100" in ev["text"] and "5200" in ev["text"], ev["text"]
        assert any("stale locks released (holder dead): 1" in x for x in audit.lines(p.db, p=p))
    finally:
        f.close()


def test_lockreap_releases_a_lock_whose_pid_was_reused(env, monkeypatch, tmp_path):
    from ttp import lockreap
    path = tmp_path / "gate.0.lock"
    f = _reap_slot(path, pid=4100, pid_start="11", pgid=4100, run_id="70")
    try:
        # pid 4100 lives, but started at another time: another process got the pid.
        fake = _ReapProcs(monkeypatch, {4100: {"start": "999", "pgid": 1}, 5200: {"start": "90", "env": {"TTP_RUN_ID": "70"}}},
                      [5200])
        assert lockreap.verdict(lockreap.label(path))[0] == "dead"
        got = _reap_sweep([path])
        assert [r["lock"] for r in got["released"]] == ["gate.0.lock"] and "another process" in got["released"][0]["dead_why"]
        assert [x[0] for x in fake.sent] == [5200]
    finally:
        f.close()


def test_lockreap_keeps_a_live_holder_and_a_live_process_group_member(env, monkeypatch, tmp_path):
    from ttp import lockreap
    path = tmp_path / "gate.0.lock"
    f = _reap_slot(path, pid=4100, pid_start="11", pgid=4100, run_id="70")
    try:
        fake = _ReapProcs(monkeypatch, {4100: {"start": "11", "pgid": 4100}}, [4100])
        got = _reap_sweep([path])
        assert not got["released"] and not got["kept"] and not fake.sent   # alive: a long hold stays
        # The holder died, a live member of its process group still has it open: still a legitimate hold.
        fake.procs = {4101: {"start": "12", "pgid": 4100, "env": {"TTP_RUN_ID": "70"}}}
        fake.fds = [4101]
        got = _reap_sweep([path])
        assert not got["released"] and not fake.sent, got
    finally:
        f.close()


def test_lockreap_old_labels_without_a_start_time_are_dead_only_when_the_pid_is_gone(env, monkeypatch, tmp_path):
    from ttp import lockreap
    path = tmp_path / "gate.0.lock"
    f = _reap_slot(path, pid=4100, pgid=4100, run_id="70")   # no pid_start: an older release's label
    try:
        fake = _ReapProcs(monkeypatch, {4100: {"start": "999"}, 5200: {"start": "1", "env": {"TTP_RUN_ID": "70"}}}, [5200])
        assert lockreap.verdict(lockreap.label(path))[0] == "alive"
        assert not _reap_sweep([path])["released"]
        del fake.procs[4100]
        assert lockreap.verdict(lockreap.label(path))[0] == "dead"
        assert _reap_sweep([path])["released"]
    finally:
        f.close()


def test_lockreap_leaves_a_process_of_another_run_and_names_it(env, monkeypatch, tmp_path):
    from ttp import lockreap
    path = tmp_path / "gate.0.lock"
    f = _reap_slot(path, pid=4100, pid_start="11", pgid=4100, run_id="70")
    try:
        fake = _ReapProcs(monkeypatch, {5200: {"start": "1", "env": {"TTP_RUN_ID": "71"}, "cmd": "sleep 9999"},
                                    6300: {"start": "2", "env": {}, "cmd": "other"}}, [5200])
        got = _reap_sweep([path])
        assert not got["released"] and not fake.sent
        kept = got["kept"][str(path)]
        assert [x["pid"] for x in kept["survivors"]] == [5200] and "left running" in kept["left"]
        fake.fds = [6300]   # no run at all in its environment: left as well
        assert not _reap_sweep([path])["released"] and not fake.sent
        fake.fds = None     # cannot list who has it open: never reaped
        assert not _reap_sweep([path])["released"] and not fake.sent
    finally:
        f.close()


def test_lockreap_releases_a_sandboxed_takes_lock_once_its_agent_pid_is_reused(env, monkeypatch, tmp_path):
    from ttp import lockreap, locks
    (tmp_path / "child.pid").write_text("4100\n11\n")
    with monkeypatch.context() as m:   # a `ttp lock` in the run's sandbox: its label names the agent
        m.setattr(locks, "_pgid", lambda pid: 1)
        m.setattr(locks.os, "getpid", lambda: 13)
        m.setattr(locks, "_pid_ns", lambda: "pid:[2]")
        m.setattr(locks, "kthread", lambda pid, proc="/proc": False)
        t = locks.taker({"TTP_PIDNS": "pid:[9]", "TTP_RUN_DIR": str(tmp_path)})
    assert (t["pid"], t["pid_start"]) == (4100, "11")
    path = tmp_path / "gate.0.lock"
    f = _reap_slot(path, pid=t["pid"], pid_start=t["pid_start"], pgid=t["pgid"], run_id="70")
    try:
        fake = _ReapProcs(monkeypatch, {4100: {"start": "11", "pgid": 4100}, 5200: {"start": "90", "env": {"TTP_RUN_ID": "70"}}},
                          [5200])
        assert lockreap.verdict(lockreap.label(path))[0] == "alive"   # the agent itself still runs
        fake.procs[4100] = {"start": "999", "pgid": 1}                  # same pid, another process
        assert lockreap.verdict(lockreap.label(path))[0] == "dead"
        assert [r["lock"] for r in _reap_sweep([path])["released"]] == ["gate.0.lock"]
        assert [x[0] for x in fake.sent] == [5200]
    finally:
        f.close()


def test_lockreap_same_run_compares_the_project_when_the_label_names_one(env, monkeypatch, tmp_path):
    from ttp import lockreap
    _ReapProcs(monkeypatch, {
        1: {"env": {"TTP_RUN_ID": "70", "TTP_PROJECT": "/srv/a/alpha"}},
        2: {"env": {"TTP_RUN_ID": "70", "TTP_PROJECT": "/srv/b/beta"}},
        3: {"env": {"TTP_RUN_ID": "70"}}}, [])
    lbl = {"run_id": "70", "project": "alpha"}
    assert lockreap._same_run(lbl, 1)
    assert not lockreap._same_run(lbl, 2)   # another project's run 70
    assert not lockreap._same_run(lbl, 3)   # says no project: never reaped as alpha's
    old = {"run_id": "70"}                  # an older release's label: run id alone, as before
    assert all(lockreap._same_run(old, x) for x in (1, 2, 3))
    assert not lockreap._same_run({"run_id": "71", "project": "alpha"}, 1)
    # end to end: a dead holder's lock kept open by another project's run of the same id stays
    path = tmp_path / "gate.0.lock"
    f = _reap_slot(path, pid=4100, pid_start="11", pgid=4100, run_id="70", project="alpha")
    try:
        fake = _ReapProcs(monkeypatch, {5200: {"start": "1", "env": {"TTP_RUN_ID": "70", "TTP_PROJECT": "/srv/b/beta"}}},
                          [5200])
        got = _reap_sweep([path])
        assert not got["released"] and not fake.sent and str(path) in got["kept"]
        fake.procs[5200]["env"]["TTP_PROJECT"] = "/srv/a/alpha"
        assert _reap_sweep([path])["released"] and [x[0] for x in fake.sent] == [5200]
    finally:
        f.close()


def test_lockreap_sigkills_a_reaped_process_that_outlives_sigterm(env, monkeypatch, tmp_path):
    import signal as sg
    from ttp import lockreap
    path = tmp_path / "gate.0.lock"
    f = _reap_slot(path, pid=4100, pid_start="11", run_id="70")
    try:
        fake = _ReapProcs(monkeypatch, {5200: {"start": "1", "env": {"TTP_RUN_ID": "70"}}}, [5200])
        r = lockreap.Reaper()
        r.sweep([path], 0)
        r.sweep([path], 61)
        r.sweep([path], 62)                                   # still within KILL_AFTER_S: nothing more
        r.sweep([path], 61 + lockreap.KILL_AFTER_S + 1)
        assert fake.sent == [(5200, sg.SIGTERM), (5200, sg.SIGKILL)], fake.sent
    finally:
        f.close()


def test_lockreap_skips_free_and_shared_slots(env, monkeypatch, tmp_path):
    import fcntl
    from ttp import lockreap
    free, sh = tmp_path / "gate.0.lock", tmp_path / "gate.1.lock"
    free.write_text(json.dumps({"pid": 4100}))
    sh.write_text(json.dumps({"pid": 4100}))
    g = open(sh)
    fcntl.flock(g, fcntl.LOCK_SH)
    try:
        fake = _ReapProcs(monkeypatch, {5200: {"start": "1", "env": {"TTP_RUN_ID": "70"}}}, [5200])
        assert lockreap.slot_files([tmp_path]) == [free, sh]
        got = _reap_sweep([free, sh])
        assert not got["released"] and not got["kept"] and not fake.sent
    finally:
        g.close()


def test_lock_wait_alert_fires_once_after_the_threshold_names_the_holder_and_clears(env, monkeypatch):
    from ttp import daemon as dm, lockreap, locks
    p = make(env)
    d = p.state / "locks"
    f = _reap_slot(d / "gate.0.lock", pid=4100, pid_start="11", pgid=4100, run_id="70", task_id="7",
                   project="demo", command="run-gate --all")
    _ReapProcs(monkeypatch, {4100: {"start": "11", "pgid": 4100}}, [4100])
    t0 = time.time()
    tickets = [locks.enqueue(d, "gate", "task #8", since=t0 - 10 * 60),
               locks.enqueue(d, "gate", "task #9", since=t0 - 5 * 60)]

    def sent():
        return p.db.q("SELECT text FROM messages WHERE direction='out' AND ref='lock-wait:gate'")
    try:
        dm.Daemon(p.base).reap_locks()
        assert not sent()                          # 10 min: under the threshold
        tickets.append(locks.enqueue(d, "gate", "task #10", since=t0 - 31 * 60))
        dmn = dm.Daemon(p.base)
        dmn.reap_locks()
        dmn.reap_locks()
        dm.Daemon(p.base).reap_locks()
        msgs = sent()
        assert len(msgs) == 1, msgs                # one alert per lock, not per waiter or tick
        text = msgs[0]["text"]
        for want in ("Lock gate", "31 min", "3 waiting", "pid 4100", "run-gate --all", "task #7",
                     "project demo", "held 60 min", "holder alive"):
            assert want in text, (want, text)
        assert p.db.one("SELECT id FROM alerts WHERE key='lock-wait:gate' AND cleared IS NULL")
    finally:
        for t in tickets:
            locks.dequeue(t)
    dm.Daemon(p.base).reap_locks()
    assert not p.db.one("SELECT id FROM alerts WHERE key='lock-wait:gate' AND cleared IS NULL")
    f.close()
    assert lockreap.wait_alert_s({"locks": {"wait_alert_min": 0}}) == 0
    assert lockreap.wait_alert_s({}) == 30 * 60


def test_lockreap_never_reaps_a_detached_jobs_lock(env, monkeypatch, tmp_path):
    """A detached job's lock (label via "job") is held by every process of the job by design: a process
    of the same run that left the job's group keeps it, dead wrapper or not, and nothing is signalled."""
    from ttp import lockreap
    path = tmp_path / "run" / "job.lock"
    f = _reap_slot(path, pid=4100, pid_start="11", pgid=4100, run_id="70", task_id="7", via="job")
    try:
        fake = _ReapProcs(monkeypatch, {5200: {"start": "90", "pgid": 5200, "env": {"TTP_RUN_ID": "70", "TTP_TASK": "7"},
                                           "cmd": "build-server --daemon"}}, [5200])
        r = lockreap.Reaper()
        for t in (1000.0, 1000.0 + lockreap.GRACE_S + 1, 1000.0 + 2 * lockreap.GRACE_S):
            assert r.sweep([path], t) == {"released": [], "kept": {}}
        assert fake.sent == []
    finally:
        f.close()


def test_a_checks_slot_wait_alerts_only_behind_a_holder_not_alive_once_and_clears(env, monkeypatch, tmp_path):
    from ttp import checkcap, daemon as dm, locks
    p = make(env)
    sd = tmp_path / "check-slots"
    monkeypatch.setattr(checkcap, "slot_dir", lambda: sd)
    f = _reap_slot(sd / "checks.0.lock", pid=4100, pid_start="11", pgid=4100, run_id="70", task_id="7",
                   project="other", command="ttp checks -- pytest")
    procs = {4100: {"start": "11", "pgid": 4100}, 5200: {"start": "1", "cmd": "sleep 999"}}
    _ReapProcs(monkeypatch, procs, [5200])
    t0 = time.time()
    theirs = locks.enqueue(sd, "checks", "other task #3 (run 30) ttp checks", since=t0 - 90 * 60)
    tickets = [theirs, locks.enqueue(sd, "checks", f"{p.name} task #8 (run 80) ttp checks", since=t0 - 31 * 60)]

    def sent():
        return [m for m in p.db.q("SELECT text FROM messages WHERE direction='out' AND ref='lock-wait:checks'")
                if not m["text"].startswith("Cleared:")]
    try:
        dm.Daemon(p.base).reap_locks()
        assert not sent(), "a wait behind live, running checks is normal queueing"
        del procs[4100]                            # the holder died; another process keeps the slot
        dmn = dm.Daemon(p.base)
        dmn.reap_locks()
        dmn.reap_locks()
        dm.Daemon(p.base).reap_locks()
        msgs = sent()
        assert len(msgs) == 1, msgs
        text = msgs[0]["text"]
        for want in ("Lock checks", "31 min", "1 waiting", "pid 4100", "task #7", "dead: its pid 4100 is gone",
                     "within about 2 min"):
            assert want in text, (want, text)
        locks.dequeue(tickets.pop())               # only the other project's waiter is left
        dm.Daemon(p.base).reap_locks()
        assert not p.db.one("SELECT id FROM alerts WHERE key='lock-wait:checks' AND cleared IS NULL")
        assert len(sent()) == 1
    finally:
        for t in tickets:
            locks.dequeue(t)
        f.close()


def test_long_waits_only_and_stuck_only_filter_queues(tmp_path, monkeypatch):
    from ttp import lockreap, locks
    f = _reap_slot(tmp_path / "checks.0.lock", pid=4100, pid_start="11", pgid=4100)
    g = _reap_slot(tmp_path / "gate.0.lock", pid=4100, pid_start="11", pgid=4100)
    procs = {4100: {"start": "11", "pgid": 4100}}
    _ReapProcs(monkeypatch, procs, [4100])
    t0 = time.time()
    ts = [locks.enqueue(tmp_path, r, "x", since=t0 - 3600) for r in ("checks", "gate")]
    try:
        assert {w["lock"] for w in lockreap.long_waits([tmp_path], 1800, t0)} == {"checks", "gate"}
        assert [w["lock"] for w in lockreap.long_waits([tmp_path], 1800, t0, only={"checks"})] == ["checks"]
        assert not lockreap.long_waits([tmp_path], 1800, t0, stuck_only=True)
        procs[4100]["start"] = "12"                # a reused pid counts as not alive
        assert {w["lock"] for w in lockreap.long_waits([tmp_path], 1800, t0, stuck_only=True)} == {"checks", "gate"}
        monkeypatch.setattr(locks, "_judgeable", lambda h: False)   # one this process cannot judge, too
        assert len(lockreap.long_waits([tmp_path], 1800, t0, only={"checks"}, stuck_only=True)) == 1
    finally:
        for t in ts:
            locks.dequeue(t)
        f.close()
        g.close()


def _devq_dir(tmp_path, waiting="jb", arrived_ago=31 * 60, running="ja", task="12", pid=None, ran_ago=600):
    d = tmp_path / "devq"
    for sub in ("queue", "running", "done"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "queue" / f"{int((time.time() - arrived_ago) * 1e9):020d}-{waiting}.json").write_text("{}")
    if running:
        (d / "running" / f"{running}.json").write_text(json.dumps({"task": task}))
        cur = {"started": time.time() - ran_ago, **({"pid": pid} if pid else {})}
        (d / "running" / f"{running}.state.json").write_text(json.dumps({"cur": cur}))
    return d


def test_the_devq_probe_says_how_long_a_queued_job_waited_and_behind_which_job(tmp_path, monkeypatch, capsys):
    from ttp import devq
    monkeypatch.setattr(devq, "runner_alive", lambda d: True)
    d = _devq_dir(tmp_path, pid=os.getpid())
    assert devq.probe(d, "jb") == 1
    out = capsys.readouterr().out
    got = devq.parse_wait("build: x\n" + out)
    assert got["job"] == "jb" and 31 * 60 - 5 <= got["waited_s"] <= 31 * 60 + 5 and got["ahead"] == 1, out
    h = got["holder"]
    assert h["job"] == "ja" and h["task"] == "12" and h["pid"] == os.getpid() and h["alive"] is True
    assert 595 <= h["run_s"] <= 605
    gone = subprocess.Popen(["true"])
    gone.wait()
    (d / "running" / "ja.state.json").write_text(json.dumps({"cur": {"pid": gone.pid}}))
    devq.probe(d, "jb")
    h = devq.parse_wait(capsys.readouterr().out)["holder"]
    assert h["alive"] is False and h["run_s"] is None
    for f in (d / "running").iterdir():
        f.unlink()
    devq.probe(d, "jb")
    got = devq.parse_wait(capsys.readouterr().out)
    assert got["holder"] is None and got["ahead"] == 0
    for text in ("jb: running", "jb: queued, but no runner is alive (runner down)", "", "jb: queued 5 s"):
        assert devq.parse_wait(text) is None, text


def test_a_devq_queue_wait_raises_one_lock_wait_alert_names_the_running_job_and_clears(env, tmp_path):
    from ttp import daemon as dm, lockreap
    p = make(env)
    d = dm.Daemon(p.base)
    fake = Path(_fake_ttp(tmp_path, 1, "r1-2: queued 600 s, 1 ahead; running: r1-1 task #12 for 900 s, pid 4242 alive"))
    probe = f"{fake} devq probe r1 r1-2"
    tid = _due_waiting_task(p, probe)

    def sent():
        return [m for m in p.db.q("SELECT text FROM messages WHERE direction='out' AND ref='lock-wait:devq:r1'")
                if not m["text"].startswith("Cleared:")]

    def answer(rc, out):
        fake.write_text(f"#!/bin/sh\necho '{out}'\nexit {rc}\n")
        _reprobe(d, tid)
        d.reap_locks()
    _settle_probe(d, tid)
    d.reap_locks()
    assert p.db.kv(lockreap.DEVQ_WAITS_KEY)[str(tid)]["holder"]["job"] == "r1-1"
    assert not sent(), "10 min: under the threshold"
    answer(1, "r1-2: queued 1900 s, 1 ahead; running: r1-1 task #12 for 2200 s, pid 4242 alive")
    assert not sent(), "the wait started 10 min ago, as first seen: a later answer does not move it"
    w = p.db.kv(lockreap.DEVQ_WAITS_KEY)
    p.db.set_kv(lockreap.DEVQ_WAITS_KEY, {k: {**v, "since": time.time() - 31 * 60} for k, v in w.items()})
    d.reap_locks()
    answer(1, "r1-2: queued 1960 s, 1 ahead; running: r1-1 task #12 for 2260 s, pid 4242 alive")
    dm.Daemon(p.base).reap_locks()
    msgs = sent()
    assert len(msgs) == 1, msgs
    for want in ("Lock devq:r1", "31 min", "device job r1-1", "task #12", "pid 4242", "holder alive"):
        assert want in msgs[0]["text"], (want, msgs[0]["text"])
    before = p.db.kv(lockreap.DEVQ_WAITS_KEY)
    for rc in (None, 255):                       # a timeout or an unreachable host keeps the wait as it was
        lockreap.note_devq_wait(p.db, tid, "r1", probe, "", rc)
    assert p.db.kv(lockreap.DEVQ_WAITS_KEY) == before
    answer(1, "r1-2: running")
    assert not p.db.one("SELECT id FROM alerts WHERE key='lock-wait:devq:r1' AND cleared IS NULL")
    assert not p.db.kv(lockreap.DEVQ_WAITS_KEY) and len(sent()) == 1


def test_a_devq_wait_ends_when_its_task_stops_waiting_on_that_probe(env):
    from ttp import daemon as dm, lockreap
    p = make(env)
    tid = _due_waiting_task(p, "ttp devq probe r1 r1-2")
    line = "r1-2: queued 2000 s, 0 ahead; running: r1-1 for 50 s, pid 77 gone"
    lockreap.note_devq_wait(p.db, tid, "r1", "ttp devq probe r1 r1-2", line, 1)
    d = dm.Daemon(p.base)
    d.reap_locks()
    text = p.db.one("SELECT text FROM alerts WHERE key='lock-wait:devq:r1' AND cleared IS NULL")["text"]
    assert "dead: its pid 77 is gone" in text, text
    p.db.update_task(tid, status="done")
    d.reap_locks()
    assert not p.db.one("SELECT id FROM alerts WHERE key='lock-wait:devq:r1' AND cleared IS NULL")
    assert not p.db.kv(lockreap.DEVQ_WAITS_KEY)
