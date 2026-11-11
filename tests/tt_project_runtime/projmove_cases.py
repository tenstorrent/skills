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
