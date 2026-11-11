# Self-watch (selfwatch.py, Daemon.check_self_watch) of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
# Add tests of the host and own-process-tree watch, its runaway stop and its status line here.
from __future__ import annotations


def _sw_proc(root, forks, procs, load="2.0", mem_kb=64 * 2**20):
    """A fake /proc: procs maps pid -> (ppid, pgid, ticks, env or None); None leaves environ unreadable
    (another user's process). The tree is rewritten whole, so a pid left out is gone."""
    import shutil as _sh
    if root.exists():
        _sh.rmtree(root)
    root.mkdir(parents=True)
    (root / "stat").write_text(f"cpu 1 2 3\nprocesses {forks}\n")
    (root / "loadavg").write_text(f"{load} 1.0 1.0 1/100 999\n")
    (root / "meminfo").write_text(f"MemTotal: {2 * mem_kb} kB\nMemAvailable: {mem_kb} kB\n")
    for pid, (ppid, pgid, ticks, env) in procs.items():
        d = root / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} (x y) S {ppid} {pgid} 0 0 0 0 0 0 0 0 {ticks} 0 0 0 20 0 1 0 {pid + 7} 0 256\n")
        if env is not None:
            (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")


def _sw_blink(root, env, per_listing):
    """A sleep for Watch's pid burst: each call adds per_listing new processes of run env in group 500
    to the fake /proc (they are gone once _sw_proc rewrites it)."""
    calls = []

    def blink(_s):
        calls.append(1)
        for i in range(per_listing):
            d = root / str(700000 + 1000 * len(calls) + i)
            d.mkdir()
            (d / "stat").write_text(f"{d.name} (x) S 500 500 0 0 0 0 0 0 0 0 1 0 0 0 20 0 1 0 9 0 256\n")
            (d / "environ").write_bytes(b"\0".join(f"{a}={b}".encode() for a, b in env.items()) + b"\0")
    return blink


def _sw_world(tmp_path, n_own, n_other=None):
    """This project's run 7 (task 42) with n_own processes in its own group 500, one of its processes in
    group 950 beside another user's, another project's busy group 900 and the daemon (pid 100); the
    other project and the other user have n_other processes each (default n_own)."""
    n_other = n_own if n_other is None else n_other
    base = tmp_path / "proj" / "tt-project"
    other = tmp_path / "other" / "tt-project"
    base.mkdir(parents=True, exist_ok=True)
    other.mkdir(parents=True, exist_ok=True)
    mine = {"TTP_PROJECT": str(base), "TTP_RUN_ID": "7", "TTP_TASK_ID": "42"}
    procs = {100: (1, 100, 50, {"PATH": "/bin"}), 101: (100, 100, 5, {"PATH": "/bin"}),
             500: (1, 500, 10, mine), 950: (1, 950, 10, None), 951: (950, 950, 10, mine)}
    procs.update({1000 + i: (500, 500, 10, mine) for i in range(n_own)})
    procs.update({5000 + i: (1, 900, 10, {"TTP_PROJECT": str(other), "TTP_RUN_ID": "7"}) for i in range(n_other)})
    procs.update({8000 + i: (950, 950, 10, None) for i in range(n_other)})
    return base, procs


def test_selfwatch_stops_its_own_runaway_run_after_the_window_and_leaves_other_processes_alone(tmp_path):
    from ttp import selfwatch
    s = {**selfwatch.DEFAULTS, "procs": 0, "load_x": 0}
    root = tmp_path / "proc"
    base, procs = _sw_world(tmp_path, 3)
    _sw_proc(root, 1000, procs)
    mine = {"TTP_PROJECT": str(base), "TTP_RUN_ID": "7", "TTP_TASK_ID": "42"}
    w = selfwatch.Watch(base, proc=root, daemon_pid=100, cores=64, sleep=_sw_blink(root, mine, 100))
    first = w.step(s, 0.0)
    assert first["sampled"] and first["runaway"] == [] and first["host"]["forks_s"] is None, "no rate before two samples"
    assert first["tree"]["procs"] == 2 + 5 and first["tree"]["runs"] == 1, first["tree"]   # daemon 2 + run 7 (500, 951 and 3)
    # Run 7 itself storms (2000 new processes of its own seen per second of the pid burst), while another
    # project's and another user's groups grow too.
    for t, n in ((30.0, 400), (60.0, 800), (90.0, 1200)):
        base, procs = _sw_world(tmp_path, n, n // 10)
        _sw_proc(root, 1000 + int(t) * 3400, procs, load="158")
        snap = w.step(s, t)
        assert snap["host"]["forks_s"] == 3400.0
        assert [b["run"] for b in snap["breach"]] == ["7"], snap["breach"]
        if t < 90:
            assert snap["runaway"] == [], "past the limit since 30 s: not before the window"
    r = snap["runaway"][0]
    assert r["run"] == "7" and r["task"] == "42" and r["spawn_s"] >= selfwatch.SPAWN_S and "processes/s" in r["why"]
    assert r["procs"] == 1202 and r["spawn_s"] == 2000.0, "its own counted rate, never the host's fork rate"
    sent = []
    done = selfwatch.stop(w, r, 0.0, kill=lambda t, sig, group: sent.append((t, sig, group)), sleep=lambda _s: None)
    import signal as _sig
    assert done["groups"] == [500] and done["pids"] == [951], done
    assert (500, _sig.SIGTERM, True) in sent and (500, _sig.SIGKILL, True) in sent, "TERM, then KILL what is left"
    signalled = {t for t, _s, _g in sent}
    assert signalled == {500, 951}, "never another project's (900), another user's (950, 8000+) or the daemon's (100)"
    assert w.step(s, 100.0)["runaway"] == [], "once per episode"
    line = selfwatch.line({**snap, "stopped": {"ts": 90.0, "run": "7", "task": "42", "why": r["why"]}}, 91.0)
    for part in ("self-watch: host load 158 on 64 cores", "3400 new processes/s", "64 GB memory", "own tree",
                 "past a limit: run 7", "stopped runaway run 7 (task #42)"):
        assert part in line, (part, line)


def test_selfwatch_never_acts_on_churn_it_cannot_attribute_or_on_a_burst_shorter_than_the_window(tmp_path):
    from ttp import selfwatch
    s = dict(selfwatch.DEFAULTS)
    root = tmp_path / "proc"
    base, procs = _sw_world(tmp_path, 3)
    _sw_proc(root, 0, procs)
    w = selfwatch.Watch(base, proc=root, daemon_pid=100, cores=64, sleep=lambda _s: None)
    w.step(s, 0.0)
    # Another project's group grows by thousands: the host churns, this project's tree does not.
    procs.update({20000 + i: (1, 900, 10, {"TTP_PROJECT": str(tmp_path / "other")}) for i in range(4000)})
    _sw_proc(root, 120000, procs, load="158")
    snap = w.step(s, 30.0)
    assert snap["churn"] == "unattributed" and not snap["breach"] and snap["runaway"] == []
    assert snap["churn_new"] and "unattributed churn: 4000 new processes/s" in selfwatch.line(snap, 30.0)
    # This project's run bursts past the process limit for less than the window, then drops back.
    s2 = {**s, "procs": 50}
    base, procs = _sw_world(tmp_path, 60)
    _sw_proc(root, 120000, procs)
    assert w.step(s2, 40.0)["breach"] and w.step(s2, 80.0)["runaway"] == [], "40 s past the limit: not yet"
    base, procs = _sw_world(tmp_path, 3)
    _sw_proc(root, 120000, procs)
    assert w.step(s2, 95.0)["breach"] == [] and "7" not in w.since, "the episode ends when the limit stops holding"
    assert selfwatch.Watch(base, proc=tmp_path / "no-proc").step(s, 0.0)["sampled"] is False, "no /proc (macOS): nothing"
    assert selfwatch.line({"sampled": False}) == ""


def test_selfwatch_settings_read_config_then_env_and_zero_turns_a_limit_off(monkeypatch):
    from ttp import selfwatch
    monkeypatch.setenv("TTP_SELFWATCH_SPAWN_S", "250")
    monkeypatch.setenv("TTP_SELFWATCH_ACT", "off")
    s = selfwatch.settings({"selfwatch": {"procs": 0, "window_s": "bad"}})
    assert s["spawn_s"] == 250 and s["procs"] == 0 and s["act"] is False and s["window_s"] == selfwatch.WINDOW_S
    assert selfwatch._past({"spawn_s": 9e9, "procs": 9e9, "cpu": 0}, {"load1": 1, "cores": 1}, {**s, "spawn_s": 0}) == ""


def test_the_daemon_stops_its_own_runaway_marks_the_task_and_notifies_once(env, monkeypatch, tmp_path):
    from ttp import selfwatch
    from ttp.daemon import Daemon
    from ttp.web import state_payload
    p = make(env)
    monkeypatch.setenv("TTP_SELFWATCH_EVERY_S", "30")
    monkeypatch.setenv("TTP_SELFWATCH_PROCS", "10")
    monkeypatch.setenv("TTP_SELFWATCH_WINDOW_S", "0")
    monkeypatch.setenv("TTP_SELFWATCH_GRACE_S", "0")
    root = tmp_path / "proc"
    _, procs = _sw_world(tmp_path, 20)
    mine = {"TTP_PROJECT": str(p.base), "TTP_RUN_ID": "7", "TTP_TASK_ID": "42"}
    procs = {pid: (pp, pg, t, mine if e and e.get("TTP_TASK_ID") else e) for pid, (pp, pg, t, e) in procs.items()}
    _sw_proc(root, 0, procs)
    sent = []
    monkeypatch.setattr(os, "kill", lambda t, sig: sent.append(("pid", t, sig)))
    import fcntl
    import json as _json
    from ttp import selfaudit
    lockf = p.state / "locks" / "dev.0.lock"
    lockf.parent.mkdir(parents=True, exist_ok=True)
    lockf.write_text(_json.dumps({"run_id": "7", "task_id": "42", "project_root": str(p.base)}))
    held = open(lockf)
    fcntl.flock(held, fcntl.LOCK_EX)   # the run's lock, let go once its processes are signalled
    monkeypatch.setattr(os, "killpg", lambda t, sig: (sent.append(("group", t, sig)), held.close()))
    d = Daemon(p.base)
    d._selfwatch = selfwatch.Watch(p.base, proc=root, daemon_pid=100, cores=64)
    tag = f"# tt-project:{p.base}"
    d._selfaudit = selfaudit.Audit(p.base, "tt-project-x", crontab=lambda: [f"@reboot start {tag}"],
                                   units=lambda: [], ssh=lambda alias: None)
    d.check_self_watch()
    assert {t for _k, t, _s in sent} == {500, 951}, sent
    ev = p.db.q("SELECT * FROM events WHERE kind='selfwatch_runaway'")
    assert len(ev) == 1 and ev[0]["task"] == 42 and "22 processes (limit 10)" in ev[0]["text"], ev
    assert "1 of its 1 lock(s) released." in ev[0]["text"], ev
    out = [m["text"] for m in p.db.q("SELECT text FROM messages WHERE text LIKE 'Self-watch stopped%'")]
    assert len(out) == 1, out
    d._selfwatch_at = 0.0
    d.check_self_watch()
    assert len(p.db.q("SELECT * FROM events WHERE kind='selfwatch_runaway'")) == 1, "once per episode"
    snap = p.db.kv(selfwatch.KV)
    assert snap["stopped"]["run"] == "7" and "runaway" not in snap
    assert "stopped runaway run 7 (task #42)" in state_payload(p, p.db)["selfwatch"]
    assert "installed: 1 crontab entry, units none" in state_payload(p, p.db)["selfwatch"]


def test_selfwatch_never_puts_a_storm_it_does_not_see_its_run_make_on_that_run(tmp_path):
    """Review #7224: a host fork storm too short-lived to be seen (another user's, or thread churn) while
    a run of this project starts one process per sample: the host rate is not shared out to the run."""
    from ttp import selfwatch
    s = dict(selfwatch.DEFAULTS)
    root = tmp_path / "proc"
    base, procs = _sw_world(tmp_path, 0)
    _sw_proc(root, 0, procs)
    w = selfwatch.Watch(base, proc=root, daemon_pid=100, cores=64, sleep=lambda _s: None)
    w.step(s, 0.0)
    for k in range(1, 13):
        base, procs = _sw_world(tmp_path, k, 0)
        _sw_proc(root, k * 30000, procs)
        snap = w.step(s, k * 10.0)
        assert snap["host"]["forks_s"] == 3000.0
        assert not snap["breach"] and snap["runaway"] == [], (k, snap["breach"])
        assert snap["tree"]["spawn_s"] < 1 and snap["churn"] == "unattributed", snap
        assert snap["churn_new"] == (k == 1), "reported once per episode"
    line = selfwatch.line(snap, 120.0)
    assert "unattributed churn" in line and "past a limit" not in line, line


def test_selfwatch_stops_its_own_storm_of_short_lived_processes_at_the_default_limits(tmp_path):
    """Review #7226: run 7 makes all of the host's 1000 forks/s, each process living 2 s (2000 fresh ones
    of its own at every 30 s sample, 1000 seen in the 1 s pid burst): stopped after the window, its churn
    explained. Then a storm whose processes live too briefly to be at the sample is caught by the burst,
    and a pid first seen in a burst is not new again at the next sample."""
    from ttp import selfwatch
    s = dict(selfwatch.DEFAULTS)
    root = tmp_path / "proc"
    base, world = _sw_world(tmp_path, 0, 0)
    mine = {"TTP_PROJECT": str(base), "TTP_RUN_ID": "7", "TTP_TASK_ID": "42"}
    _sw_proc(root, 0, world)
    w = selfwatch.Watch(base, proc=root, daemon_pid=100, cores=64, sleep=_sw_blink(root, mine, 50))
    w.step(s, 0.0)
    for k in range(1, 4):
        _sw_proc(root, k * 30000, {**world, **{100000 * k + i: (500, 500, 1, mine) for i in range(2000)}})
        snap = w.step(s, k * 30.0)
        assert snap["host"]["forks_s"] == 1000.0 and [b["run"] for b in snap["breach"]] == ["7"], snap
        assert snap["churn"] is None and "unattributed" not in selfwatch.line(snap, k * 30.0)
    assert [r["run"] for r in snap["runaway"]] == ["7"] and snap["runaway"][0]["spawn_s"] == 1000.0

    # Only 5 of its new processes are left at each sample; 50 more appear in each of the burst's listings.
    w2 = selfwatch.Watch(base, proc=root, daemon_pid=100, cores=64, sleep=_sw_blink(root, mine, 50))
    _sw_proc(root, 0, world)
    w2.step(s, 0.0)
    _sw_proc(root, 30000, {**world, **{900000 + i: (500, 500, 1, mine) for i in range(5)}})
    snap = w2.step(s, 30.0)
    assert snap["breach"] and snap["breach"][0]["run"] == "7" and snap["breach"][0]["spawn_s"] == 1000.0, snap
    # The 1000 burst pids are still alive at the next sample (no new forks, so no burst): none is new again.
    snap = w2.step(s, 40.0)
    assert snap["tree"]["spawn_s"] == 0 and not snap["breach"], snap["tree"]


def test_selfwatch_never_stops_a_run_with_long_lived_new_processes_beside_unseen_host_churn(tmp_path):
    """Review #7307: the host gains 3000 forks/s that are never visible (threads, sub-ms processes) while
    run 7 starts 25 long-lived processes per 10 s sample: no breach, no runaway, churn unattributed."""
    from ttp import selfwatch
    s = dict(selfwatch.DEFAULTS)
    root = tmp_path / "proc"
    base, world = _sw_world(tmp_path, 0, 0)
    mine = {"TTP_PROJECT": str(base), "TTP_RUN_ID": "7", "TTP_TASK_ID": "42"}
    _sw_proc(root, 0, world)
    w = selfwatch.Watch(base, proc=root, daemon_pid=100, cores=64, sleep=lambda _s: None)
    w.step(s, 0.0)
    for k in range(1, 10):
        own = {200000 + 100 * k + i: (500, 500, 1, mine) for i in range(25)}
        _sw_proc(root, k * 30000, {**world, **own})
        snap = w.step(s, k * 10.0)
        assert snap["host"]["forks_s"] == 3000.0 and snap["top"]["spawn_s"] == 2.5, snap["top"]
        assert not snap["breach"] and snap["runaway"] == [] and snap["churn"] == "unattributed", snap
    assert "unattributed churn: 2997.5 new processes/s" in selfwatch.line(snap, 90.0)


def _sa_probe(procs, cores=8, load="3.5", mem_kb=16 * 2**20, disk_kb=100 * 2**20):
    return (f"{cores}\n{load} 2.0 1.0 3/400 999\nprocesses {procs}\nMemAvailable: {mem_kb} kB\n"
            f"/dev/sda1 {disk_kb * 2} {disk_kb} {disk_kb} 50% /home\n")


def test_selfaudit_probes_held_machines_never_a_paused_one_and_skips_an_unreachable_one():
    from ttp import selfaudit
    asked, counter = [], {"a": 1000}
    def ssh(alias):
        asked.append(alias)
        if alias == "c":
            return None
        counter["a"] += 500
        return _sa_probe(counter["a"])
    a = selfaudit.Audit("/x/tt-project", "tt-project-x", crontab=lambda: [], units=lambda: [], ssh=ssh)
    a.probe(0.0, ["a", "b", "c"], held=lambda alias: alias == "b")
    a.wait()
    got = a.probe(10.0, ["a", "b", "c"], held=lambda alias: alias == "b")   # not due: last results
    assert asked == ["a", "c"], "a paused or held machine is never probed"
    assert got["b"] == {"skipped": "paused or held"} and got["c"] == {"skipped": "unreachable"}
    assert got["a"]["cores"] == 8 and got["a"]["load1"] == 3.5 and got["a"]["mem_avail_gb"] == 16.0
    assert got["a"]["disk_free_gb"] == 100.0 and got["a"]["forks_s"] is None, "one probe gives no rate yet"
    a.probe(selfaudit.PROBE_EVERY_S, ["a"], held=lambda alias: False)
    a.wait()
    got = a.probe(selfaudit.PROBE_EVERY_S + 1, ["a"], held=lambda alias: False)
    assert set(got) == {"a"}, "a machine no running task names any more leaves the line"
    assert got["a"]["forks_s"] and got["a"]["forks_s"] > 0, got
    line = selfaudit.line({"machines": {**got, "c": {"skipped": "unreachable"}}})
    assert line.startswith("held machines: a load 3.5 on 8 cores,") and "c unreachable" in line, line
    assert selfaudit.parse_probe("garbage") is None and selfaudit.line({}) == ""


def test_selfaudit_lists_its_own_installed_entries_and_flags_a_new_one_with_the_run_it_appeared_during():
    from ttp import selfaudit
    tag = "# tt-project:/x/tt-project"
    cron = [f"@reboot start {tag}", f"@reboot other # tt-project:/x/tt-project-two", "0 * * * * user-own"]
    units = ["tt-project-x.service", "tt-project-x-watchdog.timer", "tt-project-x-two.service"]
    a = selfaudit.Audit("/x/tt-project", "tt-project-x", crontab=lambda: list(cron), units=lambda: list(units),
                        ssh=lambda alias: None)
    got = a.entries(0.0, {"7"})
    assert got == {"crontab": 1, "units": ["tt-project-x-watchdog.timer", "tt-project-x.service"], "new": []}, \
        "the first read is the baseline; another project's tag or unit is not ours"
    cron.append("* * * * * runaway-loop")
    units.append("stray.timer")
    assert a.entries(30.0, {"7"})["new"] == [], "read at most every ENTRIES_EVERY_S"
    got = a.entries(selfaudit.ENTRIES_EVERY_S, {"7", "12"})
    assert [(n["kind"], n["entry"], n["runs"]) for n in got["new"]] == [
        ("crontab", "* * * * * runaway-loop", ["7", "12"]), ("unit", "stray.timer", ["7", "12"])], got
    a.entries(2 * selfaudit.ENTRIES_EVERY_S, set())   # run 12 was still running at the last read: nothing new
    cron.append("5 * * * * the-users-own")
    got = a.entries(3 * selfaudit.ENTRIES_EVERY_S, set())
    assert len(got["new"]) == 2, "an entry that appears while no run of ours runs is not flagged"
    line = selfaudit.line({"installed": got})
    assert "installed: 1 crontab entry, units tt-project-x-watchdog.timer, tt-project-x.service" in line, line
    assert "new crontab entry during run(s) 7, 12: * * * * * runaway-loop" in line, line


def test_selfaudit_default_readers_never_read_the_real_crontab_units_or_hosts_under_the_suite(env):
    from ttp import selfaudit
    assert os.environ.get("TTP_TEST_NO_SELFAUDIT_READ") == "1"
    assert selfaudit.read_crontab() is None and selfaudit.read_units() is None and selfaudit.ssh_probe("a") is None


def test_selfaudit_holdings_count_only_this_projects_held_locks_its_keeper_holds_and_live_jobs(env, monkeypatch, tmp_path):
    import fcntl
    import json as _json
    from ttp import locks, selfaudit
    p = make(env)
    d = tmp_path / "locks"
    d.mkdir()
    fds = []
    def slot(name, label, held):
        f = d / name
        f.write_text(_json.dumps(label))
        if held:
            fh = open(f)
            fcntl.flock(fh, fcntl.LOCK_EX)
            fds.append(fh)
    slot("dev.0.lock", {"run_id": "7", "task_id": "42", "project_root": str(p.base)}, True)
    slot("dev.1.lock", {"run_id": "8", "project_root": str(p.base)}, False)
    slot("box.0.lock", {"run_id": "7", "project_root": str(tmp_path / "other" / "tt-project")}, True)
    rd = tmp_path / "run7"
    rd.mkdir()
    (rd / "detached.json").write_text(_json.dumps([{"rc": str(rd / "a.rc")}, {"rc": str(rd / "done.rc")}]))
    monkeypatch.setattr(locks, "job_ended", lambda rc: "done" in str(rc))
    try:
        assert [x["lock"] for x in selfaudit.run_locks([d], p.base, "7")] == ["dev"]
        got = selfaudit.holdings([d], p.base, tmp_path / "holds", [rd])
        assert got == {"locks": ["dev"], "holds": 0, "jobs": 1}, got
        assert "holding 1 lock(s) (dev), 0 keeper hold(s), 1 detached job(s)" in selfaudit.line({"holding": got})
    finally:
        for fh in fds:
            fh.close()
    assert selfaudit.run_locks([d], p.base, "7") == [], "a lock its holder let go is not held"
