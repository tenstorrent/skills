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
    a.reference = True
    assert cli._model_bench_argv(a) == ["terminalbench", "--reference", "--models", "m-a,m-b"]


def test_terminalbench_reference_is_booked_once_resumes_and_the_cycle_never_runs_it(env, monkeypatch):
    from ttp import modelscore, tbcycle
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch, reference={"models": ["r-a"], "endpoint": "gw"})
    assert tbcycle.reference_view()["source"] == "subset", "nothing booked yet"
    calls = []
    with pytest.raises(RuntimeError):   # the reference runner dies after its first trial
        tbcycle.ref_runner(cfg, "r-a", {}, trial=_tb_trial(clock, {"t2": RuntimeError("x")}, calls),
                           clock=lambda: clock["t"])
    assert tbcycle.reference() == []
    calls.clear()
    ok = _tb_trial(clock, {"t3": 0.0}, calls)
    assert tbcycle.ref_runner(cfg, "r-a", {}, trial=ok, clock=lambda: clock["t"]) == 2
    assert calls == [("r-a", "t2"), ("r-a", "t3")] and sleeps == [], "resumes its next unrun task, unpaced"
    rows = tbcycle.reference()
    assert len(rows) == 1 and rows[0]["reference"] is True and rows[0]["model"] == "r-a"
    assert rows[0]["effort"] == "medium" and rows[0]["agent"] == "terminus-2" and rows[0]["date"]
    assert rows[0]["passed"] == 2 and rows[0]["trials"] == 3
    calls.clear()
    launched = []
    assert tbcycle.ref_runner(cfg, "r-a", {}, trial=ok, clock=lambda: clock["t"]) == 0 and calls == [], "booked once"
    assert tbcycle.ref_start(cfg, ["r-a"], launched.append)["booked"] == ["r-a"] and launched == []
    assert len((tbcycle.folder() / tbcycle.REF_FILE).read_text().splitlines()) == 1
    assert "r-a" not in modelscore.bench() and not list(d.glob("*.jsonl")), "never a ranked model-bench row"
    before = (tbcycle.folder() / tbcycle.REF_FILE).read_text()
    got = tbcycle.step(cfg, ["m-a"], clock["t"], launched.append)   # the hourly cycle
    assert launched == ["m-a"] and "r-a" not in json.dumps(got), "the cycle never starts or books a reference run"
    tbcycle.runner(cfg, "m-a", {}, trial=ok, sleep=sleep, clock=lambda: clock["t"])
    assert (tbcycle.folder() / tbcycle.REF_FILE).read_text() == before and calls[-1][0] == "m-a"


def test_terminalbench_reference_rows_show_marked_beside_the_ranking_else_the_marked_subset(env, monkeypatch):
    from ttp import modelscore, tbcycle
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch, reference={"models": ["r-a", "r-b"]})
    sub = tbcycle.reference_view()
    assert sub["source"] == "subset" and "subset" in sub["note"] and len(sub["rows"]) == 2
    text = modelscore.text(modelscore.build([]))
    assert "Terminal-Bench reference (no reference run booked; the 20-task subset" in text
    tbcycle.ref_runner(cfg, "r-a", {}, trial=_tb_trial(clock, {}, []), clock=lambda: clock["t"])
    view = tbcycle.reference_view()
    assert view["source"] == "reference" and [r["model"] for r in view["rows"]] == ["r-a"]
    text = modelscore.text(modelscore.build([]))
    assert "Terminal-Bench reference (fixed reference, run once, not ranked):" in text
    assert "r-a (effort medium): 3/3 passed, agent terminus-2" in text and "subset" not in text
    launched = []
    got = tbcycle.ref_start(cfg, ["r-a", "x"], launched.append)
    assert got == {"launched": [], "booked": ["r-a"], "running": [], "not_served": ["r-b"]}, "only served, unbooked ones"


def test_terminalbench_reference_without_a_saved_endpoint_says_so_and_starts_nothing(env, monkeypatch, capsys):
    import argparse as ap
    import types
    from ttp import cli, endpoint, harborslice, tbcycle
    started = []
    monkeypatch.setattr(tbcycle, "launch", lambda *x, **k: started.append(x))
    monkeypatch.setattr(harborslice, "install", lambda *x, **k: started.append("install") or (None, "no"))
    monkeypatch.setattr(endpoint, "list_models", lambda *x: started.append("list") or (True, "", ["r-a"]))
    monkeypatch.setattr(endpoint, "credentials", lambda alias: None)
    for ref in ({}, {"endpoint": "gw"}):   # no alias set; an alias with nothing saved under it
        d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch, reference=ref)
        p = types.SimpleNamespace(config=lambda: cfg)
        for runner in (None, "r-a"):
            a = ap.Namespace(action="terminalbench", models=None, runner=runner, reference=True)
            with pytest.raises(SystemExit):
                cli._terminalbench(p, a, clock["t"])
            err = capsys.readouterr().err.strip()
            assert len(err.splitlines()) == 1 and "ttp secret endpoint" in err
        shutil.rmtree(d.parent)
    assert started == [] and not tbcycle.folder().exists(), "nothing started, nothing written"
