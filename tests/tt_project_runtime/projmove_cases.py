# Planned project moves (projmove.py, `ttp move`, Daemon.check_move and the hold in Daemon._dispatch).
# Exec'd into test_tt_project_runtime.py: make, _slot_setup, _slot_tasks and its imports are in scope.
from __future__ import annotations


def test_the_old_copy_stops_on_the_first_check_after_the_new_copy_runs(env):
    from ttp import projmove, projpause
    p = make(env)
    projmove.record(p.db, "source", "newhost", "demo", now=1000)
    calls = []

    def call(host, cmd):
        calls.append((host, cmd))
        return (0, "demo live") if len(calls) > 1 else (1, "demo not live: no successful model run yet")
    assert projmove.step(p.db, None, now=1100, call=call) == "" and not p.db.kv("paused", False)
    assert calls == [("newhost", "alive demo --within 100s")]
    assert projmove.step(p.db, None, now=1130, call=call) == "" and len(calls) == 1, "one check a minute"
    assert "this copy stopped" in projmove.step(p.db, None, now=1160, call=call)
    assert p.db.kv("paused", False) and projpause.current(p.db)["via"] == projmove.VIA
    p.db.set_kv(projpause.INFO_KEY, dict(projpause.current(p.db), since=0))
    assert projpause.alert_due(p.db, {}, now=10 ** 6) is None, "a moved copy raises no long-pause alert"
    assert projmove.step(p.db, None, now=2000, call=call) == "" and len(calls) == 2, "done: no more checks"


def test_the_new_copy_starts_workers_only_once_the_old_one_stopped_or_stays_unreachable(env):
    from ttp import projmove
    p = make(env)
    projmove.record(p.db, "target", "oldhost", "demo", now=1000)
    assert projmove.dispatch_hold(p.db).startswith(projmove.HELD_NOTE)
    calls = []
    assert projmove.step(p.db, lambda t: False, now=1100, call=lambda h, c: calls.append(c)) == ""
    assert calls == [], "no coordinator turn here yet: the old copy is not asked"
    down = lambda h, c: (calls.append((h, c)) or (255, "ssh: connect timed out"))  # noqa: E731
    assert projmove.step(p.db, lambda t: t == 1000, now=1200, call=down) == ""
    assert calls == [("oldhost", "move demo --confirmed")] and projmove.dispatch_hold(p.db)
    assert "did not answer" in projmove.step(p.db, lambda t: True, now=1200 + projmove.UNREACHABLE_S, call=down)
    assert projmove.dispatch_hold(p.db) == ""
    projmove.record(p.db, "target", "oldhost", "demo", now=5000)
    assert "stopped: workers start here" in projmove.step(p.db, lambda t: True, now=5100,
                                                           call=lambda h, c: (0, "demo stopped here"))
    assert projmove.dispatch_hold(p.db) == ""


def test_ttp_move_confirmed_stops_the_old_copy_and_the_held_new_copy_starts_nothing(
        env, monkeypatch, tmp_path, reap_tmp_processes, capsys):
    from ttp import cli, projmove
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=2)
    try:
        cli.main(["move", p.name, "--from", "oldhost"])
        assert "moving here from oldhost" in capsys.readouterr().out
        assert p.db.one("SELECT 1 FROM events WHERE kind='project_move' AND status='queued'"), "wakes the coordinator"
        p.db.add_task("routine", "s", kind="work", tier="light", origin="user", priority=3)
        d.dispatch()
        assert _slot_tasks(p) == [], "held while the old copy may still start work"
        cli.main(["move", p.name, "--cancel"])
        cli.main(["move", p.name, "--to", "newhost"])
        cli.main(["move", p.name, "--confirmed"])
        assert "stopped here" in capsys.readouterr().out and p.db.kv("paused", False)
        assert projmove.current(p.db)["done"]
    finally:
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_the_move_check_never_blocks_the_tick_on_a_hung_remote_and_applies_its_answer_later(env, monkeypatch):
    import subprocess
    import threading
    from ttp import daemon, projmove
    p = make(env)
    projmove.record(p.db, "source", "newhost", "demo", now=time.time() - 120)
    release, calls = threading.Event(), []

    def hung(host, cmd, timeout):   # a down host: ssh hangs until its timeout
        calls.append((host, cmd))
        release.wait(30)
        return subprocess.CompletedProcess([], 0, "demo live\n", "")
    monkeypatch.setattr(projmove, "_ssh", hung)
    d = daemon.Daemon(p.base)
    t0 = time.monotonic()
    d.check_move()
    d.check_move()
    assert time.monotonic() - t0 < 2, "the ssh call ran inside the tick"
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1 and not p.db.kv("paused", False), "one call in flight, nothing applied yet"
    release.set()
    deadline = time.monotonic() + 5
    while d._move_call.job.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    d.check_move()
    assert p.db.kv("paused", False) and projmove.current(p.db)["done"], "the answer applies on a later tick"
    assert p.db.one("SELECT 1 FROM events WHERE kind='project_move' AND severity='low'")
    assert len(calls) == 1


def test_the_new_copy_asks_the_old_one_only_after_its_own_coordinator_run_since_the_move(env):
    from ttp import daemon, projmove
    p = make(env)
    since = time.time() - 300
    projmove.record(p.db, "target", "oldhost", "demo", now=since)
    calls = []
    d = daemon.Daemon(p.base)
    d._move_call = lambda h, c: (calls.append(c) or (0, "demo stopped here"))
    for role, status, ended in (("coordinator", "ok", since - 10), ("coordinator", "error", since + 10),
                                ("worker", "ok", since + 10)):
        p.db.x("INSERT INTO runs(role,status,started,ended) VALUES(?,?,?,?)", (role, status, ended - 5, ended))
    d.check_move()
    assert calls == [] and projmove.dispatch_hold(p.db), "an old, failed or worker run is no coordinator turn here"
    p.db.x("INSERT INTO runs(role,status,started,ended) VALUES('coordinator','ok',?,?)", (since + 5, since + 20))
    p.db.set_kv(projmove.KEY, dict(projmove.current(p.db), checked=0))
    d.check_move()
    assert calls == ["move demo --confirmed"] and projmove.dispatch_hold(p.db) == ""
    ev = p.db.one("SELECT severity, text FROM events WHERE kind='project_move'")
    assert ev["severity"] == "low" and "workers start here" in ev["text"]


def test_background_drops_an_answer_for_another_host_and_reports_a_crashed_call(env):
    from ttp import projmove
    gate = __import__("threading").Event()

    def call(host, cmd):
        gate.wait(5)
        if host == "bad":
            raise RuntimeError("boom")
        return 0, f"{host} ok"
    bg = projmove.Background(call)
    assert bg("a", "alive demo --within 5s") is None
    gate.set()
    bg.job.join(5)
    assert bg("b", "alive demo --within 9s") is None, "a's answer is not b's: a new call starts"
    bg.job.join(5)
    assert bg("b", "alive demo --within 70s") == (0, "b ok")
    assert bg("bad", "move demo --confirmed") is None
    bg.job.join(5)
    assert bg("bad", "move demo --confirmed")[0] == 255


def test_ttp_alive_on_the_new_copy_counts_no_run_copied_over_from_the_old_host(env, capsys):
    from ttp import cli, projmove
    p = make(env)
    now = time.time()
    p.db.x("INSERT INTO runs(role,status,started,ended,cost_usd) VALUES('coordinator','ok',?,?,0.1)", (now - 100, now - 90))
    projmove.record(p.db, "target", "oldhost", p.name, now=now - 60)
    with pytest.raises(SystemExit) as e:
        cli.main(["alive", p.name, "--within", "1h"])
    assert e.value.code == 1 and "not live" in capsys.readouterr().out, "the old host's run, copied over, counted"
    p.db.x("INSERT INTO runs(role,status,started,ended,cost_usd) VALUES('coordinator','ok',?,?,0.1)", (now - 30, now - 20))
    with pytest.raises(SystemExit) as e:
        cli.main(["alive", p.name, "--within", "1h"])
    assert e.value.code == 0
