# Paced Terminal-Bench cycle tests (tbcycle.py, `ttp model-bench terminalbench`) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file. Fakes only: no harbor, endpoint or model is called.
from __future__ import annotations


def _tb_setup(env, monkeypatch, tasks=("t1", "t2", "t3"), **tb):
    from ttp import project
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "tb-home")
    d = project.HOME_DIR / "model-bench"
    d.mkdir(parents=True)
    (d / "harbor-tasks.txt").write_text("".join(t + "\n" for t in tasks))
    cfg = {"model_bench": {"terminalbench": {"cycle_h": 3, "new_model_h": 0, **tb}}}
    clock = {"t": 1_800_000_000.0}
    sleeps = []

    def sleep(s):
        sleeps.append(round(s))
        clock["t"] += s
    return d, cfg, clock, sleeps, sleep


def _tb_trial(clock, rewards, calls):
    def trial(model, task, root):
        calls.append((model, task))
        clock["t"] += 60
        r = rewards.get(task, 1.0)
        if isinstance(r, Exception):
            raise r
        return {"reward": r, "min": 1.0, "error": None}
    return trial


def test_terminalbench_cycle_paces_one_trial_per_model_resumes_and_books_only_complete_cycles(env, monkeypatch):
    import fcntl
    from ttp import modelbench, modelscore, tbcycle
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch)
    launched = []
    got = tbcycle.step(cfg, ["m-a"], clock["t"], launched.append)
    assert got["launched"] == ["m-a"] and got["booked"] == [], "a due model with no runner gets one; nothing to book"
    calls = []
    boom = _tb_trial(clock, {"t2": RuntimeError("killed")}, calls)
    with pytest.raises(RuntimeError):   # the runner dies (a reboot) after its first trial
        tbcycle.runner(cfg, "m-a", {}, trial=boom, sleep=sleep, clock=lambda: clock["t"])
    assert calls == [("m-a", "t1"), ("m-a", "t2")] and sleeps == [3540], "starts 1 h apart (3 h / 3 tasks); the trial took 60 s"
    calls.clear()
    sleeps.clear()
    ok = _tb_trial(clock, {"t2": 0.0}, calls)
    assert tbcycle.runner(cfg, "m-a", {}, trial=ok, sleep=sleep, clock=lambda: clock["t"]) == 2
    assert calls == [("m-a", "t2"), ("m-a", "t3")], "a restart resumes the cycle's next unrun task"
    assert tbcycle.step(cfg, ["m-a"], clock["t"], launched.append)["booked"] == ["m-a"]
    rows = [json.loads(x) for f in d.glob("*.jsonl") for x in f.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["suite"] == rows[0]["class"] == "terminalbench"
    assert rows[0]["passed"] == 2 and rows[0]["trials"] == 3 and rows[0]["cycle"] == 1
    assert rows[0]["effort"] and rows[0]["agent"] == "terminus-2", "stored with effort and agent"
    assert modelbench.models()["m-a"]["source"] == "terminalbench", "the verdict follows modelsuite.verdict"
    score = modelscore.bench()["m-a"]["scores"]["*"]["terminalbench"][1]
    assert round(score, 1) == 66.7
    launched.clear()
    assert tbcycle.step(cfg, ["m-a"], clock["t"], launched.append)["launched"] == [], "not before cycle_h"
    clock["t"] += 3 * 3600
    assert tbcycle.step(cfg, ["m-a"], clock["t"], launched.append)["launched"] == ["m-a"], "the next cycle is due"
    held = open(tbcycle.folder() / "running" / "m-a.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:   # one trial per model at once: a running runner is never doubled
        assert tbcycle.step(cfg, ["m-a"], clock["t"], launched.append)["waiting"]["m-a"] == "its runner is running"
        assert tbcycle.runner(cfg, "m-a", {}, trial=ok, sleep=sleep, clock=lambda: clock["t"]) == 0
    finally:
        held.close()
    calls.clear()
    with pytest.raises(RuntimeError):   # cycle 2 half run: the ranking keeps cycle 1's score
        tbcycle.runner(cfg, "m-a", {}, trial=_tb_trial(clock, {"t2": RuntimeError("x")}, calls), sleep=sleep,
                       clock=lambda: clock["t"])
    assert tbcycle.step(cfg, ["m-a"], clock["t"], launched.append)["booked"] == []
    assert round(modelscore.bench()["m-a"]["scores"]["*"]["terminalbench"][1], 1) == 66.7


def test_terminalbench_a_newly_served_model_waits_new_model_h_then_runs_at_the_same_pace(env, monkeypatch):
    from ttp import tbcycle
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch, new_model_h=3)
    launched = []
    got = tbcycle.step(cfg, ["m-new"], clock["t"], launched.append)
    assert got["launched"] == [] and "m-new" in got["waiting"], "first seen now: not on the first hour"
    clock["t"] += 3 * 3600 + 1
    assert tbcycle.step(cfg, ["m-new"], clock["t"], launched.append)["launched"] == ["m-new"], "within the day"


def test_terminalbench_owner_schedule_is_additive_owner_only_and_follows_the_on_setting(env):
    from ttp import benchowner
    p = make(env)
    sched = lambda n: p.db.one("SELECT enabled, every_s, payload FROM schedules WHERE name=?", (n,))
    assert benchowner.ensure_tb(p, p.db, False, True) == "", "a non-owner with no schedule: nothing"
    assert benchowner.ensure_tb(p, p.db, True, True) == "on"
    row = sched(benchowner.TB_SCHEDULE)
    assert row["enabled"] and row["every_s"] == 3600
    assert json.loads(row["payload"])["command"] == "ttp model-bench terminalbench --detach"
    assert sched(benchowner.SCHEDULE) is None, "separate from the daily model-bench schedule"
    assert benchowner.ensure_tb(p, p.db, True, True) == "", "unchanged: no rewrite"
    assert benchowner.ensure_tb(p, p.db, True, False) == "off", "model_bench.terminalbench.on false"
    assert benchowner.ensure_tb(p, p.db, True, True) == "on"
    assert benchowner.ensure_tb(p, p.db, False, True) == "off", "the lease moved away"
    assert not sched(benchowner.TB_SCHEDULE)["enabled"]


def test_terminalbench_detached_child_keeps_its_action_and_models():
    import argparse as ap
    from ttp import cli
    a = ap.Namespace(action="terminalbench", models="m-a,m-b", force=False)
    assert cli._model_bench_argv(a) == ["terminalbench", "--models", "m-a,m-b"]
