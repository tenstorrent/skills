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


def _sw_world(tmp_path, n_own):
    """This project's run 7 (task 42) with n_own processes in its own group 500, one of its processes in
    group 950 beside another user's, another project's busy group 900 and the daemon (pid 100)."""
    base = tmp_path / "proj" / "tt-project"
    other = tmp_path / "other" / "tt-project"
    base.mkdir(parents=True, exist_ok=True)
    other.mkdir(parents=True, exist_ok=True)
    mine = {"TTP_PROJECT": str(base), "TTP_RUN_ID": "7", "TTP_TASK_ID": "42"}
    procs = {100: (1, 100, 50, {"PATH": "/bin"}), 101: (100, 100, 5, {"PATH": "/bin"}),
             500: (1, 500, 10, mine), 950: (1, 950, 10, None), 951: (950, 950, 10, mine)}
    procs.update({1000 + i: (500, 500, 10, mine) for i in range(n_own)})
    procs.update({5000 + i: (1, 900, 10, {"TTP_PROJECT": str(other), "TTP_RUN_ID": "7"}) for i in range(n_own)})
    procs.update({8000 + i: (950, 950, 10, None) for i in range(n_own)})
    return base, procs


def test_selfwatch_stops_its_own_runaway_run_after_the_window_and_leaves_other_processes_alone(tmp_path):
    from ttp import selfwatch
    s = {**selfwatch.DEFAULTS, "procs": 0, "load_x": 0}
    root = tmp_path / "proc"
    base, procs = _sw_world(tmp_path, 3)
    _sw_proc(root, 1000, procs)
    w = selfwatch.Watch(base, proc=root, daemon_pid=100, cores=64)
    first = w.step(s, 0.0)
    assert first["sampled"] and first["runaway"] == [] and first["host"]["forks_s"] is None, "no rate before two samples"
    assert first["tree"]["procs"] == 2 + 5 and first["tree"]["runs"] == 1, first["tree"]   # daemon 2 + run 7 (500, 951 and 3)
    for t, n in ((30.0, 40), (60.0, 80), (90.0, 120)):
        base, procs = _sw_world(tmp_path, n)
        _sw_proc(root, 1000 + int(t) * 3400, procs, load="158")
        snap = w.step(s, t)
        assert snap["host"]["forks_s"] == 3400.0
        assert [b["run"] for b in snap["breach"]] == ["7"], snap["breach"]
        if t < 90:
            assert snap["runaway"] == [], "past the limit since 30 s: not before the window"
    r = snap["runaway"][0]
    assert r["run"] == "7" and r["task"] == "42" and r["spawn_s"] >= selfwatch.SPAWN_S and "processes/s" in r["why"]
    assert r["procs"] == 122 and r["spawn_s"] < 3400, "only its share of the host's forks counts"
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
    w = selfwatch.Watch(base, proc=root, daemon_pid=100, cores=64)
    w.step(s, 0.0)
    # Another project's group grows by thousands: the host churns, this project's tree does not.
    procs.update({20000 + i: (1, 900, 10, {"TTP_PROJECT": str(tmp_path / "other")}) for i in range(4000)})
    _sw_proc(root, 120000, procs, load="158")
    snap = w.step(s, 30.0)
    assert snap["foreign_churn"] and not snap["breach"] and snap["runaway"] == []
    assert "not from this project's processes" in selfwatch.line(snap, 30.0)
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
    monkeypatch.setattr(os, "killpg", lambda t, sig: sent.append(("group", t, sig)))
    monkeypatch.setattr(os, "kill", lambda t, sig: sent.append(("pid", t, sig)))
    d = Daemon(p.base)
    d._selfwatch = selfwatch.Watch(p.base, proc=root, daemon_pid=100, cores=64)
    d.check_self_watch()
    assert {t for _k, t, _s in sent} == {500, 951}, sent
    ev = p.db.q("SELECT * FROM events WHERE kind='selfwatch_runaway'")
    assert len(ev) == 1 and ev[0]["task"] == 42 and "22 processes (limit 10)" in ev[0]["text"], ev
    out = [m["text"] for m in p.db.q("SELECT text FROM messages WHERE text LIKE 'Self-watch stopped%'")]
    assert len(out) == 1, out
    d._selfwatch_at = 0.0
    d.check_self_watch()
    assert len(p.db.q("SELECT * FROM events WHERE kind='selfwatch_runaway'")) == 1, "once per episode"
    snap = p.db.kv(selfwatch.KV)
    assert snap["stopped"]["run"] == "7" and "runaway" not in snap
    assert "stopped runaway run 7 (task #42)" in state_payload(p, p.db)["selfwatch"]
