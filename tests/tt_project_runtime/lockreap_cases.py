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


_OWN = "/srv/own/tt-project"   # the sweeping project's folder (Reaper project=); a fake process is its own
                                # unless its env names another TTP_PROJECT


class _ReapProcs:
    """A fake process table: pid -> {"start", "pgid", "env", "cmd"}; `fds` lists who has each lock open."""

    def __init__(self, monkeypatch, procs: dict, fds: list | None):
        from ttp import lockreap, locks, procowner
        self.procs, self.fds, self.sent = procs, fds, []
        monkeypatch.setattr(locks, "_alive", lambda pid: int(pid) in self.procs)
        monkeypatch.setattr(locks, "kthread", lambda pid, proc="/proc": False)
        monkeypatch.setattr(locks, "start_time", lambda pid, proc="/proc": (self.procs.get(int(pid)) or {}).get("start"))
        monkeypatch.setattr(lockreap, "_pgid", lambda pid: (self.procs.get(pid) or {}).get("pgid"))
        monkeypatch.setattr(lockreap, "environ", lambda pid: (self.procs.get(pid) or {}).get("env") or {})
        monkeypatch.setattr(procowner, "environ", lambda pid: {"TTP_PROJECT": _OWN, **((self.procs.get(pid) or {}).get("env") or {})})
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
    r = lockreap.Reaper(project=_OWN)
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


def test_lockreap_same_run_compares_the_project_root_when_the_label_names_one(env, monkeypatch, tmp_path):
    """Every project's folder is named tt-project: the label's resolved project_root tells them apart."""
    from ttp import lockreap
    _ReapProcs(monkeypatch, {
        1: {"env": {"TTP_RUN_ID": "70", "TTP_PROJECT": "/srv/a/tt-project"}},
        2: {"env": {"TTP_RUN_ID": "70", "TTP_PROJECT": "/srv/b/tt-project"}},
        3: {"env": {"TTP_RUN_ID": "70"}},
        4: {"env": {"TTP_RUN_ID": "70", "TTP_PROJECT": "/srv/a"}}}, [])   # a project root names the same project
    lbl = {"run_id": "70", "project": "tt-project", "project_root": "/srv/a/tt-project"}
    assert lockreap._same_run(lbl, 1) and lockreap._same_run(lbl, 4)
    assert not lockreap._same_run(lbl, 2)   # another project's run 70, same folder name
    assert not lockreap._same_run(lbl, 3)   # says no project: never reaped as a's
    old = {"run_id": "70", "project": "tt-project"}   # an older release's label: run id alone
    assert all(lockreap._same_run(old, x) for x in (1, 2, 3))
    assert not lockreap._same_run({"run_id": "71", "project_root": "/srv/a/tt-project"}, 1)


@pytest.mark.parametrize("label_root", [None, _OWN, "/srv/other/tt-project"])
def test_lockreap_never_signals_another_projects_process_with_the_same_run_and_task(env, monkeypatch, tmp_path, label_root):
    """Run and task ids overlap across projects sharing the host's lock folders: a dead holder's slot kept
    open by another project's process with the same TTP_RUN_ID and TTP_TASK is left and named, whatever
    the label says; this project's own stale holder still is reaped (when the label is its own or old)."""
    import signal as sg
    from ttp import lockreap
    path = tmp_path / "gate.0.lock"
    extra = {"project_root": label_root} if label_root else {}
    f = _reap_slot(path, pid=4100, pid_start="11", pgid=4100, run_id="70", task_id="7", project="tt-project", **extra)
    try:
        theirs = {"TTP_RUN_ID": "70", "TTP_TASK": "7", "TTP_TASK_ID": "7", "TTP_PROJECT": "/srv/other/tt-project",
                  "TTP_RUN_DIR": "/srv/other/tt-project/state/runs/70"}
        fake = _ReapProcs(monkeypatch, {5200: {"start": "1", "pgid": 5200, "env": theirs}}, [5200])
        got = _reap_sweep([path])
        assert not got["released"] and not fake.sent and "left running" in got["kept"][str(path)]["left"]
        # a run dir alone inside another project is not ours either, nor is a process with no project at all
        fake.procs[5200]["env"] = {**theirs, "TTP_PROJECT": ""}
        assert not _reap_sweep([path])["released"] and not fake.sent
        # no sweeping project (an older caller): nothing is reaped
        fake.procs[5200]["env"] = {"TTP_RUN_ID": "70", "TTP_TASK": "7"}
        r = lockreap.Reaper()
        r.sweep([path], 0)
        assert not r.sweep([path], lockreap.GRACE_S + 1)["released"] and not fake.sent
        # this project's own stale holder, by TTP_PROJECT or by a run dir in its folder
        for mine in ({"TTP_PROJECT": _OWN}, {"TTP_PROJECT": "", "TTP_RUN_DIR": _OWN + "/state/runs/70"}):
            fake.procs[5200]["env"] = {"TTP_RUN_ID": "70", "TTP_TASK": "7", **mine}
            fake.sent.clear()
            got = _reap_sweep([path])
            want = [] if label_root == "/srv/other/tt-project" else [(5200, sg.SIGTERM)]
            assert fake.sent == want, (mine, got)
    finally:
        f.close()


def test_lockreap_sigkills_a_reaped_process_that_outlives_sigterm(env, monkeypatch, tmp_path):
    import signal as sg
    from ttp import lockreap
    path = tmp_path / "gate.0.lock"
    f = _reap_slot(path, pid=4100, pid_start="11", run_id="70")
    try:
        fake = _ReapProcs(monkeypatch, {5200: {"start": "1", "env": {"TTP_RUN_ID": "70"}}}, [5200])
        r = lockreap.Reaper(project=_OWN)
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


def _lock_wait_rows(p, key):
    return p.db.q("SELECT text, channel FROM messages WHERE direction='out' AND ref=? ORDER BY id", (key,))


def _prime_reaper(dmn, p):
    """Give daemon `dmn` a reaper that first saw today's dead holders past the grace, so its next sweep judges them."""
    from ttp import checkcap, lockreap
    r = lockreap.Reaper(ended=lambda h: lockreap.ended_job(p, h), project=p.base)
    r.sweep(lockreap.slot_files([p.state / "locks", checkcap.slot_dir()]), time.time() - lockreap.GRACE_S - 5)
    dmn._lock_reaper = r


def test_lock_wait_alert_stays_off_chat_while_the_holder_lives_and_posts_once_when_it_dies_unreleased(env, monkeypatch):
    from ttp import daemon as dm, locks
    p = make(env)
    d = p.state / "locks"
    f = _reap_slot(d / "gate.0.lock", pid=4100, pid_start="11", pgid=4100, run_id="70", task_id="7")
    procs = {4100: {"start": "11", "pgid": 4100}, 5200: {"start": "1", "cmd": "sleep 999", "env": {"TTP_PROJECT": "/srv/other"}}}
    _ReapProcs(monkeypatch, procs, [4100, 5200])
    ticket = locks.enqueue(d, "gate", "task #8", since=time.time() - 31 * 60)
    try:
        dmn = dm.Daemon(p.base)
        dmn.reap_locks()
        dmn.reap_locks()
        rows = _lock_wait_rows(p, "lock-wait:gate")
        assert [r["channel"] for r in rows] == ["quiet"], rows      # holder alive: web app and status only
        assert "holder alive" in rows[0]["text"]
        del procs[4100]                                             # it dies; another project's process keeps the slot
        dmn.reap_locks()
        assert [r["channel"] for r in _lock_wait_rows(p, "lock-wait:gate")] == ["quiet"], "the sweep may still release it"
        _prime_reaper(dmn, p)
        for _ in range(3):
            dmn.reap_locks()
        dm.Daemon(p.base).reap_locks()
        rows = _lock_wait_rows(p, "lock-wait:gate")
        assert [r["channel"] for r in rows] == ["quiet", "chat"], rows   # posted once, not per tick
        assert "dead" in rows[1]["text"] and "pid 4100" in rows[1]["text"]
    finally:
        locks.dequeue(ticket)
    dmn.reap_locks()
    assert not p.db.one("SELECT id FROM alerts WHERE key='lock-wait:gate' AND cleared IS NULL")
    rows = _lock_wait_rows(p, "lock-wait:gate")
    assert rows[-1]["text"].startswith("Cleared:") and rows[-1]["channel"] == "chat", rows
    f.close()


def test_lock_wait_alert_behind_a_dead_holder_the_sweep_kept_posts_to_chat_at_once(env, monkeypatch):
    from ttp import daemon as dm, locks
    p = make(env)
    d = p.state / "locks"
    f = _reap_slot(d / "gate.0.lock", pid=4100, pid_start="11", pgid=4100, run_id="70", task_id="7")
    _ReapProcs(monkeypatch, {5200: {"start": "1", "cmd": "sleep 999", "env": {"TTP_PROJECT": "/srv/other"}}}, [5200])
    ticket = locks.enqueue(d, "gate", "task #8", since=time.time() - 31 * 60)
    try:
        dmn = dm.Daemon(p.base)
        _prime_reaper(dmn, p)
        dmn.reap_locks()
        dmn.reap_locks()
        rows = _lock_wait_rows(p, "lock-wait:gate")
        assert [r["channel"] for r in rows] == ["chat"], rows
    finally:
        locks.dequeue(ticket)
        f.close()


def test_dead_unreleased_counts_a_dead_holder_once_the_sweep_had_time_to_release_it():
    from ttp import lockreap
    w = {"lock": "gate", "holders": [{"state": "dead", "path": "/l/gate.0.lock", "pid": 4100}]}
    seen = {}
    assert not lockreap.dead_unreleased(w, {}, seen, 1000.0)          # within the sweep's grace
    assert not lockreap.dead_unreleased(w, {}, seen, 1000.0 + lockreap.RELEASE_WITHIN_S - 1)
    assert lockreap.dead_unreleased(w, {}, seen, 1000.0 + lockreap.RELEASE_WITHIN_S)
    assert lockreap.dead_unreleased(w, {"/l/gate.0.lock": {}}, {}, 1000.0)   # kept: at once
    assert lockreap.dead_unreleased({"lock": "devq:r", "holders": [{"state": "dead", "path": "", "pid": 7}]}, None, {}, 1.0)
    assert not lockreap.dead_unreleased({"lock": "gate", "holders": [{"state": "alive", "path": "/x", "pid": 1},
                                                                     {"state": "unknown", "path": "/y", "pid": 2}]}, {}, {}, 1e9)


def test_lockreap_never_reaps_a_detached_jobs_lock(env, monkeypatch, tmp_path):
    """A detached job's lock (label via "job") is held by every process of the job by design: a process
    of the same run that left the job's group keeps it, dead wrapper or not, and nothing is signalled."""
    from ttp import lockreap
    path = tmp_path / "run" / "job.lock"
    f = _reap_slot(path, pid=4100, pid_start="11", pgid=4100, run_id="70", task_id="7", via="job")
    try:
        fake = _ReapProcs(monkeypatch, {5200: {"start": "90", "pgid": 5200, "env": {"TTP_RUN_ID": "70", "TTP_TASK": "7"},
                                           "cmd": "build-server --daemon"}}, [5200])
        r = lockreap.Reaper(project=_OWN)
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
    from ttp import devq, liveness
    monkeypatch.setattr(devq, "runner_alive", lambda d: True)
    monkeypatch.setenv("TTP_PIDNS", liveness.pid_ns() or "")   # the runner's namespace is this one
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


def _ended_job_slot(p, tmp_path, status, **label):
    """A slot held for detached job `job` of a task with `status`, its .rc written, and labelled for its
    command (pid 4100, group 4100, run 70) unless `label` says otherwise. Returns (open file, slot path)."""
    tid = p.db.add_task("uses the rig", kind="code")
    p.db.x("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    rc = p.runs / "70" / "job.rc"
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_text("0\n")
    path = p.state / "locks" / "rig-z.0.lock"
    fields = {"pid": 4100, "pid_start": "11", "pgid": 4100, "via": "command", "run_id": "70", "task_id": str(tid),
              "project": p.base.name, "job": "job", "job_rc": str(rc.resolve()), **label}
    return _reap_slot(path, **fields), path


@pytest.mark.parametrize("case", ["freed", "holder alive", "taker alive", "task running", "task queued", "unknown pid",
                                  "no rc", "job lives", "other project"])
def test_lockreap_frees_a_slot_an_ended_detached_jobs_leftover_keeps_only_once_job_and_task_are_over(
        env, monkeypatch, tmp_path, case):
    """A slot whose label names a detached job that wrote its .rc, has no process holding its job lock and
    whose task ended: a leftover of the holder's process group that keeps it is reaped (it was a
    'legitimate hold' before). Any live, unknown or unfinished part keeps the hold and signals nothing."""
    import fcntl
    import signal as sg
    from ttp import lockreap, locks
    p = make(env)
    label = {"taker alive": {"taker_pid": 4000}, "unknown pid": {"node": "elsewhere"},
             "other project": {"project": "another"}}.get(case, {})
    status = {"task running": "running", "task queued": "queued"}.get(case, "cancelled")
    f, path = _ended_job_slot(p, tmp_path, status, **label)
    rc = p.runs / "70" / "job.rc"
    held = []
    if case == "no rc":
        rc.unlink()
    if case == "job lives":   # a process of the job still holds the job's own lock
        held.append(open(locks.job_lock(rc), "a+"))
        fcntl.flock(held[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
    procs = {4101: {"start": "12", "pgid": 4100, "env": {}, "cmd": "keeper"}}   # a leftover of the group, no run env
    if case == "holder alive":
        procs[4100] = {"start": "11", "pgid": 4100}
    if case == "taker alive":
        procs[4000] = {"start": "5", "pgid": 4100}
    try:
        fake = _ReapProcs(monkeypatch, procs, [4101])
        r = lockreap.Reaper(ended=lambda h: lockreap.ended_job(p, h), project=_OWN)
        assert not r.sweep([path], 1000.0)["released"]
        got = r.sweep([path], 1000.0 + lockreap.GRACE_S + 1)
        if case != "freed":
            assert not got["released"] and not fake.sent, got
            return
        assert fake.sent == [(4101, sg.SIGTERM)]
        [rel] = got["released"]
        assert "job ended" in rel["dead_why"] and "is cancelled" in rel["dead_why"], rel
        assert "is cancelled" in lockreap.release_text(rel)
        # Without the ended-job check (an older daemon) the same leftover is a legitimate group hold.
        fake.sent.clear()
        assert not _reap_sweep([path])["released"] and not fake.sent
    finally:
        f.close()
        for x in held:
            x.close()


@pytest.mark.parametrize("status", ["done", "failed"])
def test_lockreap_ended_job_counts_every_ended_task_state(env, tmp_path, status):
    from ttp import lockreap
    p = make(env)
    f, path = _ended_job_slot(p, tmp_path, status)
    try:
        assert f"is {status}" in lockreap.ended_job(p, lockreap.label(path))
    finally:
        f.close()


def _cycle_ticket(d, res, text, pid, since=None):
    """A live queue ticket of `res` in lock dir `d` from waiter process `pid` (an flock this process holds)."""
    import fcntl
    q = d / f"{res}.queue"
    q.mkdir(parents=True, exist_ok=True)
    f = open(q / f"{(since or time.time()):017.6f}-{pid}", "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    f.write(text)
    f.flush()
    return f


def _cycle_party(d, held, waits, text, pid, project=None, task=None, since=None, **label):
    """A party that holds lock `held` (None: nothing) and waits for lock `waits` (None: nothing)."""
    fs = []
    if held:
        fs.append(_reap_slot(d / f"{held}.0.lock", holder=text, pid=pid, pid_start="11", pgid=pid,
                             **({"task_id": str(task)} if task else {}), **({"project": project} if project else {}),
                             **label))
    if waits:
        fs.append(_cycle_ticket(d, waits, text, label.get("waiter_pid") or pid, since))
    return fs


def _cycle_procs(monkeypatch, parents: dict, cmds: dict | None = None):
    from ttp import lockcycle
    monkeypatch.setattr(lockcycle, "_ppid", lambda pid: parents.get(pid))
    monkeypatch.setattr(lockcycle, "cmdline", lambda pid: (cmds or {}).get(pid, ""))


def test_lock_cycle_two_parties_task_and_command_outside_a_task_one_critical_alert_folds_waits_and_clears(
        env, monkeypatch):
    # A task holds a per-host lock and waits for a site-wide one; a recovery command outside any task
    # holds the site-wide lock and waits for the per-host one (its waiter runs below its command).
    from ttp import daemon as dm, lockcycle
    p = make(env)
    d = p.state / "locks"
    _ReapProcs(monkeypatch, {4100: {"start": "11", "pgid": 4100}, 5200: {"start": "11", "pgid": 5200}}, [4100])
    _cycle_procs(monkeypatch, {5300: 5250, 5250: 5200, 5200: 5199, 4300: 4100}, {5300: "ttp lock host-a -- reset"})
    old = time.time() - 40 * 60   # past locks.wait_alert_min: the lock-wait alerts fold into the cycle's
    fs = _cycle_party(d, "host-a", "site-cycle", "task #12 (run 99)", 4100, task=12, since=old, waiter_pid=4300,
                      command="run-tests host-a")
    fs += _cycle_party(d, "site-cycle", "host-a", "task #? (run ?)", 5200, since=old, waiter_pid=5300,
                       command="recover.sh host-a", taker_pid=5199)

    def sent(like):
        return p.db.q("SELECT text, severity FROM messages WHERE direction='out' AND ref LIKE ?", (like,))
    try:
        dmn = dm.Daemon(p.base)
        dmn.reap_locks()
        dmn.reap_locks()
        dm.Daemon(p.base).reap_locks()
        [msg] = sent("lock-cycle:%")                 # once, not per check
        assert msg["severity"] == "critical"
        for want in (f"project {p.name} task #12", "run 99", "pid 4100", "a command outside any task (pid 5200",
                     "recover.sh host-a", "host-a", "site-cycle",
                     "waits for narrower lock host-a while holding wider lock site-cycle"):
            assert want in msg["text"], (want, msg["text"])
        assert "task #12 (run 99, pid 4100" in msg["text"] and "waits for narrower lock site-cycle" not in msg["text"]
        assert not sent("lock-wait:%"), "the per-lock waits fold into the cycle alert"
        assert p.db.one("SELECT id FROM alerts WHERE key LIKE 'lock-cycle:%' AND cleared IS NULL")
    finally:
        fs[0].close()   # the task lets go of host-a: the cycle breaks
    dm.Daemon(p.base).reap_locks()
    assert not p.db.one("SELECT id FROM alerts WHERE key LIKE 'lock-cycle:%' AND cleared IS NULL")
    dm.Daemon(p.base).reap_locks()
    assert len(sent("lock-cycle:%")) == 2 and "Cleared" in sent("lock-cycle:%")[-1]["text"]   # no repeat
    for f in fs[1:]:
        f.close()
    assert lockcycle.width("host-a") == "narrow" and lockcycle.width("site-cycle") == "wide"
    assert lockcycle.width("bay-7", {"locks": {"wide": ["bay-7"]}}) == "wide"


def test_lock_cycle_of_three_tasks_across_shared_dirs_is_one_cycle_with_every_party(tmp_path, monkeypatch):
    from ttp import lockcycle
    _ReapProcs(monkeypatch, {4101: {"start": "11"}, 4102: {"start": "11"}, 4103: {"start": "11"}}, None)
    _cycle_procs(monkeypatch, {})
    a, b, c = (tmp_path / n for n in ("lock-a", "lock-b", "lock-c"))
    fs = (_cycle_party(a, "lock-a", None, "alpha task #1 (run 10)", 4101, task=1)
          + _cycle_party(b, "lock-b", None, "beta task #2 (run 20)", 4102, task=2)
          + _cycle_party(c, "lock-c", None, "alpha task #3 (run 30)", 4103, task=3)
          + [_cycle_ticket(b, "lock-b", "alpha task #1 (run 10)", 7001),
             _cycle_ticket(c, "lock-c", "beta task #2 (run 20)", 7002),
             _cycle_ticket(a, "lock-a", "alpha task #3 (run 30)", 7003)])
    try:
        [cyc] = lockcycle.find([(a, None), (b, None), (c, None)])
        assert [m["id"] for m in cyc["members"]] == ["alpha task #1", "alpha task #3", "beta task #2"]
        assert cyc["locks"] == ["lock-a", "lock-b", "lock-c"] and cyc["projects"] == ["alpha", "beta"]
        text = lockcycle.text(cyc)
        assert "3 parties" in text and "project beta task #2 (run 20, pid 4102" in text
        assert "of one width" in text   # no wide lock: no wait breaks the documented order
        assert lockcycle.key(cyc) == lockcycle.key(lockcycle.find([(c, None), (a, None), (b, None)])[0])
    finally:
        for f in fs:
            f.close()


def test_lock_cycle_a_plain_chain_and_a_free_slot_raise_nothing(env, monkeypatch):
    from ttp import daemon as dm, lockcycle
    p = make(env)
    d = p.state / "locks"
    _ReapProcs(monkeypatch, {4101: {"start": "11"}, 4102: {"start": "11"}, 4103: {"start": "11"}}, None)
    _cycle_procs(monkeypatch, {})
    # 1 holds l1 and waits for l2; 2 holds l2 and waits for l3; 3 holds l3 and waits for nothing.
    fs = (_cycle_party(d, "l1", "l2", "task #1 (run 1)", 4101, task=1)
          + _cycle_party(d, "l2", "l3", "task #2 (run 2)", 4102, task=2)
          + _cycle_party(d, "l3", None, "task #3 (run 3)", 4103, task=3))
    try:
        g = lockcycle.graph([(d, p.name)])
        assert sorted(e[:2] for e in g["edges"]) == [(f"{p.name} task #1", f"{p.name} task #2"),
                                                      (f"{p.name} task #2", f"{p.name} task #3")]
        dm.Daemon(p.base).reap_locks()
        assert not p.db.q("SELECT id FROM messages WHERE ref LIKE 'lock-cycle:%'")
        # 3 also waits for l1, but l1 has a second, free slot: 3 is not stuck behind 1.
        free = d / "l1.1.lock"
        free.write_text("{}")
        fs.append(_cycle_ticket(d, "l1", "task #3 (run 3)", 4103))
        assert not lockcycle.find([(d, p.name)])
        free.unlink()
        assert lockcycle.find([(d, p.name)])
    finally:
        for f in fs:
            f.close()


def test_lock_cycle_groups_nested_holders_outside_a_task_and_skips_cycles_of_other_projects(env, monkeypatch):
    from ttp import daemon as dm, lockcycle
    p = make(env)
    d = p.state / "locks"
    _ReapProcs(monkeypatch, {6100: {"start": "11"}, 6200: {"start": "11"}, 6300: {"start": "11"}}, None)
    # One script holds x, and inside it a nested `ttp lock y`; its waiter for z runs below both. Another
    # script holds z and waits for x: a two-party cycle, the nested holder in the first party.
    _cycle_procs(monkeypatch, {6200: 6150, 6150: 6100, 6250: 6200, 6350: 6300})
    fs = (_cycle_party(d, "x", None, "task #? (run ?)", 6100)
          + _cycle_party(d, "y", "z", "task #? (run ?)", 6200, waiter_pid=6250)
          + _cycle_party(d, "z", "x", "task #? (run ?)", 6300, waiter_pid=6350)
          + [_cycle_ticket(d, "y", "task #? (run ?)", 6400)])   # a bystander waits for y: its holder counts
    try:
        [cyc] = lockcycle.find([(d, p.name)])
        assert [m["id"] for m in cyc["members"]] == ["pid 6100", "pid 6300"]
        assert cyc["members"][0]["holds"] == {"x", "y"}
        other = lockcycle.find([(d, "elsewhere")])
        assert other and other[0]["projects"] == ["elsewhere"]
        # The daemon of a project that is in no party of the cycle raises nothing for it.
        monkeypatch.setattr(lockcycle, "find", lambda dirs: other)
        dm.Daemon(p.base).reap_locks()
        assert not p.db.q("SELECT id FROM messages WHERE ref LIKE 'lock-cycle:%'")
    finally:
        for f in fs:
            f.close()


def test_lock_cycle_needs_both_reads_and_skips_shared_waits_behind_shared_holders(tmp_path, monkeypatch):
    import fcntl
    from ttp import lockcycle
    _ReapProcs(monkeypatch, {4101: {"start": "11"}, 4102: {"start": "11"}}, None)
    _cycle_procs(monkeypatch, {})
    d = tmp_path / "locks"
    fs = _cycle_party(d, "p", None, "task #1 (run 1)", 4101, task=1) + [_cycle_ticket(d, "q", "task #1 (run 1)", 7001)]
    from ttp import locks
    g = locks.try_take_shared([d / "q.0.lock"], "task #2 (run 2)")   # task 2 holds q shared
    fs += [g, _cycle_ticket(d, "p", "task #2 (run 2)", 7002)]
    try:
        assert lockcycle.find([(d, "demo")])   # p is held exclusively: task 2's exclusive wait is stuck
        fs[-1].close()
        fs.append(_cycle_ticket(d, "p-x", "task #2 (run 2)", 7002))   # not a wait for p any more
        (d / "p.queue").rename(d / "p-old.queue")
        sw = d / "p.queue"
        sw.mkdir()
        h = open(sw / f"{time.time():017.6f}-7003{__import__('ttp').locks.SHARED_TICKET}", "a+")
        fcntl.flock(h, fcntl.LOCK_EX)
        h.write("task #2 (run 2)")
        h.flush()
        fs.append(h)
        assert lockcycle.find([(d, "demo")])   # a shared wait behind an exclusive holder is stuck too
        # 1 waits for q, held only shared by 2: an exclusive waiter is stuck behind it; a shared one is not.
        fs[1].close()
        q = d / "q.queue"
        for x in q.iterdir():
            x.unlink()
        s1 = open(q / f"{time.time():017.6f}-7004{__import__('ttp').locks.SHARED_TICKET}", "a+")
        fcntl.flock(s1, fcntl.LOCK_EX)
        s1.write("task #1 (run 1)")
        s1.flush()
        fs.append(s1)
        assert not lockcycle.find([(d, "demo")])
        # A cycle seen in one read only (a release and a new wait read between two files) raises nothing.
        monkeypatch.setattr(lockcycle, "graph", lambda dirs: next(seen))
        fake = {"parties": {"a": {"id": "a", "holds": {"x"}, "waits": {"y"}}, "b": {"id": "b", "holds": {"y"}, "waits": {"x"}}},
                "edges": [("a", "b", "y"), ("b", "a", "x")]}
        seen = iter([fake, {"parties": {}, "edges": []}])
        assert not lockcycle.find([(d, "demo")])
        seen = iter([fake, fake])
        assert len(lockcycle.find([(d, "demo")])) == 1
    finally:
        for f in fs:
            f.close()


def test_lock_cycle_takes_shared_holders_from_their_own_labels_not_the_stale_slot_label(tmp_path, monkeypatch):
    # A shared hold (`ttp lock --shared`) labels itself under <res>.shared; the slot file keeps the
    # label of its last exclusive holder, who may hold nothing now.
    from ttp import lockcycle, locks
    _cycle_procs(monkeypatch, {})
    d = tmp_path / "locks"
    q, r = d / "q.0.lock", d / "r.0.lock"
    locks.try_take([q], "task #1 (run 1)").close()     # task 1 held q and let go
    fs = [locks.try_take_shared([q], "task #2 (run 2)"),  # task 2 holds q shared
          locks.try_take([r], "task #3 (run 3)"),          # task 3 holds r
          _cycle_ticket(d, "r", "task #1 (run 1)", 7001),  # task 1 waits for r
          _cycle_ticket(d, "q", "task #3 (run 3)", 7003)]  # task 3 waits for q (exclusive)
    try:
        g = lockcycle.graph([(d, "demo")])
        assert ("demo task #3", "demo task #2", "q") in g["edges"]
        assert not any(h == "demo task #1" for _, h, _ in g["edges"])
        assert not lockcycle.find([(d, "demo")]), "task 1 holds nothing: no deadlock"
        fs.append(_cycle_ticket(d, "r", "task #2 (run 2)", 7002))   # task 2 now waits for r: a real cycle
        [cyc] = lockcycle.find([(d, "demo")])
        assert [m["id"] for m in cyc["members"]] == ["demo task #2", "demo task #3"]
        assert cyc["locks"] == ["q", "r"]
        assert "task #2 (run 2" in lockcycle.text(cyc) and "(shared)" not in lockcycle.text(cyc)
    finally:
        for f in fs:
            f.close()


def test_procowner_proves_a_process_is_this_projects_by_its_full_path_never_by_name_or_ids(tmp_path):
    """TTP_PROJECT (root or folder, symlinks resolved) or a TTP_RUN_DIR inside the folder proves ownership;
    the same folder name elsewhere, the same task and run ids, a run dir outside or no identity never do."""
    from ttp import procowner
    mine, theirs = tmp_path / "a" / "tt-project", tmp_path / "b" / "tt-project"
    for d in (mine / "state" / "runs" / "7", theirs / "state" / "runs" / "7"):
        d.mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "a")
    ids = {"TTP_TASK": "7", "TTP_TASK_ID": "7", "TTP_RUN_ID": "7"}
    assert procowner.owns_env(mine, {**ids, "TTP_PROJECT": str(mine)})
    assert procowner.owns_env(mine.parent, {"TTP_PROJECT": str(tmp_path / "link")})   # a root, via a symlink
    assert procowner.owns_env(mine, {"TTP_RUN_DIR": str(mine / "state" / "runs" / "7")})
    assert not procowner.owns_env(mine, {**ids, "TTP_PROJECT": str(theirs)})          # same name, other root
    assert not procowner.owns_env(mine, {**ids, "TTP_RUN_DIR": str(theirs / "state" / "runs" / "7")})
    assert not procowner.owns_env(mine, {"TTP_RUN_DIR": str(tmp_path / "a-tt-project-run")})
    assert not procowner.owns_env(mine, ids) and not procowner.owns_env(mine, {}) and not procowner.owns_env(mine, None)
    assert not procowner.owns_env(None, {"TTP_PROJECT": str(mine)})
    assert procowner.foreign_env(mine, {"TTP_PROJECT": str(theirs)})
    assert not procowner.foreign_env(mine, {"TTP_PROJECT": str(mine.parent)}) and not procowner.foreign_env(mine, ids)


def test_procowner_reads_a_live_processs_identity_and_none_for_a_gone_one(tmp_path):
    import subprocess
    import sys
    from ttp import procowner
    if not Path("/proc/self/environ").exists():
        pytest.skip("no /proc")
    base = tmp_path / "tt-project"
    proc = subprocess.Popen([sys.executable, "-c", "import time; print(1, flush=True); time.sleep(30)"],
                            env={"PATH": os.environ.get("PATH", ""), "TTP_PROJECT": str(base), "TTP_TASK": "7"},
                            stdout=subprocess.PIPE)
    try:
        proc.stdout.readline()   # past its exec: /proc shows its own environment
        assert procowner.owns(base, proc.pid) and procowner.owns(tmp_path, proc.pid)
        assert not procowner.owns(tmp_path / "other" / "tt-project", proc.pid)
    finally:
        proc.kill()
        proc.wait()
    assert procowner.environ(proc.pid) is None and not procowner.owns(base, proc.pid)


def test_lock_labels_name_the_holders_full_project_root(env, monkeypatch, tmp_path):
    from ttp import locks
    monkeypatch.setenv("TTP_PROJECT", str(tmp_path / "proj"))
    monkeypatch.setenv("TTP_RUN_ID", "7")
    assert locks.run_ids()["project_root"] == os.path.realpath(tmp_path / "proj" / "tt-project")


def test_lock_records_and_the_lock_wait_alert_name_the_registered_project_not_the_harness_folder(
        env, monkeypatch, tmp_path):
    # Every project's harness folder is named tt-project: a holder or waiter record names the project as
    # registered (`ttp list`), and the lock-wait alert built on it does too.
    from ttp import lockreap, locks
    p = make(env, "alpha")
    assert p.base.name == "tt-project"
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_ID", "70")
    monkeypatch.setenv("TTP_TASK", "7")
    ids = locks.run_ids()
    assert ids["project"] == "alpha" and ids["project_from"] == "registry"
    assert ids["project_root"] == os.path.realpath(p.base)
    d = tmp_path / "shared" / "gate"
    f = locks.try_take(locks.slot_paths(d, "gate", 1), "alpha task #7 (run 70)", "run-gate")
    sh = locks.try_take_shared(locks.slot_paths(d, "rig", 1), "alpha task #7 (run 70)", "use-rig")
    t = locks.enqueue(d, "gate", "alpha task #8 (run 80)", since=time.time() - 31 * 60)
    try:
        assert json.loads((d / "gate.0.lock").read_text())["project"] == "alpha"
        [rec] = locks.shared_holders(locks.slot_paths(d, "rig", 1))
        assert rec["project"] == "alpha"
        [w] = lockreap.long_waits([d], 30 * 60)
        assert w["holders"][0]["project"] == "alpha"
        text = lockreap.wait_text(w)
        assert "project alpha" in text and "project tt-project" not in text, text
    finally:
        locks.dequeue(t)
        sh.close()
        f.close()
    # Outside any project: no project at all, not a guess.
    monkeypatch.delenv("TTP_PROJECT")
    assert "project" not in locks.run_ids() and locks.project_of(locks.run_ids()) is None


def test_an_old_lock_record_resolves_its_project_through_the_registry_or_says_unknown(env, monkeypatch, tmp_path):
    from ttp import locks
    p = make(env, "alpha")
    old = {"holder": "task #7 (run 70)", "project": "tt-project"}   # an older release: the folder's name
    assert locks.project_of({**old, "project_root": str(p.base)}) == "alpha"
    assert locks.project_of(old, p.state / "locks") == "alpha"     # from the project's own lock dir
    gone = tmp_path / "gone" / "tt-project"
    assert locks.project_of({**old, "project_root": str(gone)}) == f"unknown project at {gone.parent}"
    assert locks.project_of(old, tmp_path / "shared" / "gate") == "unknown project"
    # A name recorded from the registry stands once the registry no longer has its root.
    assert locks.project_of({"project": "beta", "project_from": "registry", "project_root": str(gone)}) == "beta"
    assert locks.project_of({"holder": "task #7 (run 70)"}) is None
    from ttp.project import registry_path
    for bad in ("[1, 2]", '{"projects": [1]}', "not json"):   # a malformed registry names nobody, fails no lock
        registry_path().write_text(bad)
        assert locks.project_of({**old, "project_root": str(p.base)}) == f"unknown project at {p.root}"
        monkeypatch.setenv("TTP_PROJECT", str(p.base))
        assert "project" not in locks.run_ids()


def test_two_projects_with_same_named_harness_folders_are_told_apart_in_lock_records(env, monkeypatch, tmp_path):
    from ttp import lockcycle, lockreap, locks
    from ttp.project import register
    a, b_base = make(env, "alpha"), tmp_path / "beta" / "tt-project"
    b_base.mkdir(parents=True)
    register("beta", {"host": "testhost", "dir": str(b_base.parent)})
    assert a.base.name == b_base.name
    for name, base in (("alpha", a.base), ("beta", b_base)):
        monkeypatch.setenv("TTP_PROJECT", str(base))
        assert locks.run_ids()["project"] == name
        # Old-format records (both "tt-project") resolve by their roots.
        old = {"project": "tt-project", "project_root": str(base), "task_id": "3", "run_id": "30"}
        assert locks.project_of(old) == name
        assert lockcycle._task_of("", old, None)["project"] == name   # a shared lock's party
        assert lockcycle._project_of("", old, None) == name
    # A detached job's slot of beta is never alpha's to free, though both folders share a name.
    rc = a.runs / "70" / "job.rc"
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_text("0\n")
    tid = a.db.add_task("uses the rig", kind="code")
    a.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    h = {"job_rc": str(rc), "task_id": str(tid), "project": "tt-project"}
    assert lockreap.ended_job(a, {**h, "project_root": str(a.base)})
    assert not lockreap.ended_job(a, {**h, "project_root": str(b_base)})


_DEAD_TAKER = ("import sys; from pathlib import Path; from ttp import locks; "
               "f = locks.try_take([Path(sys.argv[1])], sys.argv[2], 'ttp checks -- pytest'); "
               "sys.exit(0 if f else 3)")


def _taken_by_a_dead_process(path, holder="task #9 (run 90)"):
    """Slot file `path` labelled by the real writer (locks.try_take) in a process that then exited: a free
    slot whose record names a gone pid, as one a crashed or killed holder leaves."""
    env = {k: v for k, v in os.environ.items() if k not in ("TTP_PIDNS", "TTP_RUN_DIR")}
    subprocess.run([sys.executable, "-c", _DEAD_TAKER, str(path), holder], check=True, timeout=60,
                   env={**env, "PYTHONPATH": str(RUNTIME)})
    h = json.loads(path.read_text())
    assert h["holder"] == holder and h["pid"] and h.get("pid_start")
    return h


def _probe_storm(path, stop):
    """Another reader on the host probing `path` again and again: an exclusive flock held for an instant."""
    import fcntl
    while not stop.is_set():
        with open(path) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                time.sleep(0.0005)
            except OSError:
                pass
        time.sleep(0.002)


def test_free_lock_dead_pid_not_holder(env, monkeypatch, tmp_path):
    import threading
    from ttp import checkcap, locks, lockreap
    sd = tmp_path / "check-slots"
    monkeypatch.setattr(checkcap, "slot_dir", lambda: sd)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    path = checkcap.all_paths()[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    _taken_by_a_dead_process(path)
    stop = threading.Event()
    storm = threading.Thread(target=_probe_storm, args=(path, stop))
    storm.start()
    try:
        for _ in range(25):   # every reader, while other readers' probes collide with its own
            assert locks._slot_labels([path]) == []
            assert locks.holder_records([path]) == []
            assert lockreap._held(path) == "free"
            s = checkcap.state()
            assert s["running"] == [] and s["held"] == [], s
    finally:
        stop.set()
        storm.join(10)
    assert "task #9" not in checkcap.line()


def test_live_holder_shown(env, monkeypatch, tmp_path):
    from ttp import checkcap, locks, lockreap
    sd = tmp_path / "check-slots"
    monkeypatch.setattr(checkcap, "slot_dir", lambda: sd)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    path = checkcap.all_paths()[0]
    f = locks.try_take([path], "task #4 (run 40)", "ttp checks -- pytest")
    try:
        assert locks.holder_live(locks.read_label(path))
        assert locks._slot_labels([path]) == ["task #4 (run 40)"]
        assert [r["holder"] for r in locks.holder_records([path])] == ["task #4 (run 40)"]
        assert lockreap._held(path) == "held"
        assert checkcap.state()["running"] == ["task #4 (run 40)"]
        # A holder that died while a process it left keeps the lock is still named (lockreap frees it).
        locks.relabel(f, {"pid": 4100, "pid_start": "11"})
        monkeypatch.setattr(locks, "_alive", lambda pid: int(pid) != 4100)
        assert locks._slot_labels([path]) == ["task #4 (run 40)"]
        assert lockreap._held(path) == "held"
    finally:
        f.close()


def test_tick_clears_under_lock(env, monkeypatch, tmp_path):
    from ttp import checkcap, daemon as dm, locks
    p = make(env)
    sd = tmp_path / "check-slots"
    monkeypatch.setattr(checkcap, "slot_dir", lambda: sd)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "2")
    stale, live = checkcap.all_paths()[:2]
    stale.parent.mkdir(parents=True, exist_ok=True)
    h = _taken_by_a_dead_process(stale)
    f = locks.try_take([live], "task #4 (run 40)", "ttp checks -- pytest")
    took = []
    real = locks.write_label

    def spy(fh, label):
        import fcntl
        with open(fh.name) as g:   # the clear holds the slot's own lock while it writes
            try:
                fcntl.flock(g, fcntl.LOCK_EX | fcntl.LOCK_NB)
                took.append(True)
            except OSError:
                took.append(False)
        return real(fh, label)
    monkeypatch.setattr(locks, "write_label", spy)
    logged = []
    real_log = dm.log
    monkeypatch.setattr(dm, "log", lambda proj, text, *a, **k: logged.append(text) or real_log(proj, text, *a, **k))
    try:
        dm.Daemon(p.base).reap_locks()
        assert any(f"cleared the stale record of free checks slot {stale.name} (task #9 (run 90), pid {h['pid']}" in t
                   for t in logged), logged
        assert took == [False], "written only under the slot's lock"
        assert stale.read_text() == "{}" and json.loads(stale.read_text() or "{}") == {}, \
            "an empty object: what older releases read as no holder"
        assert locks.read_label(stale) == {}
        assert locks.read_label(live)["holder"] == "task #4 (run 40)", "a live holder's record stays"
        assert locks.clear_stale([stale, live]) == [], "nothing left to clear"
    finally:
        f.close()


def test_no_race_with_acquirer(env, monkeypatch, tmp_path):
    from ttp import locks
    a, b = tmp_path / "checks.0.lock", tmp_path / "checks.1.lock"
    _taken_by_a_dead_process(a)
    # A held slot whose record names a dead pid (a holder that left a process keeping the lock) is left alone.
    held = locks.try_take([b], "task #4 (run 40)")
    locks.relabel(held, {"pid": 4100, "pid_start": "11"})
    real_alive = locks._alive
    monkeypatch.setattr(locks, "_alive", lambda pid: int(pid) != 4100 and real_alive(pid))
    before = b.read_text()
    assert [h["path"] for h in locks.clear_stale([a, b])] == [str(a)]
    assert b.read_text() == before
    held.close()
    # A taker arriving while the clear holds a slot finds it taken and takes the next: its record is never wiped.
    _taken_by_a_dead_process(a)
    got = []
    real = locks.write_label

    def acquirer_meanwhile(fh, label):
        if not got:
            got.append(None)   # the take below writes its own label through here too
            got[0] = locks.try_take([a, b], "task #5 (run 50)")
        return real(fh, label)
    monkeypatch.setattr(locks, "write_label", acquirer_meanwhile)
    try:
        assert [h["path"] for h in locks.clear_stale([a, b])] == [str(a)]
        assert got[0] is not None and got[0].name == str(b)
        assert locks.read_label(b)["holder"] == "task #5 (run 50)"
        assert locks.read_label(a) == {}
    finally:
        if got and got[0]:
            got[0].close()


def test_running_count_live_only(env, monkeypatch, tmp_path):
    from ttp import checkcap, locks
    sd = tmp_path / "check-slots"
    monkeypatch.setattr(checkcap, "slot_dir", lambda: sd)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "3")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    paths = checkcap.all_paths()
    paths[0].parent.mkdir(parents=True, exist_ok=True)
    for x in paths[:2]:
        _taken_by_a_dead_process(x)
    f = locks.try_take(paths[2:3], "task #4 (run 40)", "ttp checks -- pytest")
    try:
        s = checkcap.state()
        assert s["running"] == ["task #4 (run 40)"], s
        assert "1 running of cap 3" in checkcap.line(), checkcap.line()
        assert "task #9" not in checkcap.line(activity=True)
    finally:
        f.close()
