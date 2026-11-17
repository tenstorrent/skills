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


def _fake_docker(tmp, ok=False):
    """A fake docker on PATH: `docker compose version` exits 0 only when DOCKER_CONFIG/cli-plugins holds an
    executable docker-compose (or ok)."""
    b = tmp / "bin"
    b.mkdir(parents=True, exist_ok=True)
    d = b / "docker"
    d.write_text("#!/bin/sh\n" + ("exit 0\n" if ok else
                 '[ "$1" = compose ] && [ -x "${DOCKER_CONFIG:-/nonexistent}/cli-plugins/docker-compose" ] '
                 "&& exit 0\nexit 1\n"))
    d.chmod(0o755)
    return str(d)


def test_terminalbench_compose_is_found_in_a_plugin_folder_or_the_setting_else_one_message(tmp_path, monkeypatch):
    from ttp import harborslice
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(harborslice, "PLUGIN_DIRS", (str(tmp_path / "sys-plugins"),))
    root = tmp_path / "bench"
    env = {"PATH": os.environ.get("PATH", "")}
    st = {"compose": ""}
    assert harborslice.compose(root, st, env, which=lambda n: None)[0] is None, "no docker: stops"
    docker = _fake_docker(tmp_path / "ok", ok=True)
    assert harborslice.compose(root, st, env, which=lambda n: docker) == ({}, ""), "docker's own lookup works"
    docker = _fake_docker(tmp_path)
    extra, why = harborslice.compose(root, st, env, which=lambda n: docker)
    assert extra is None and why.startswith("docker compose is missing") and "model_bench.terminalbench.compose" in why
    assert str(tmp_path / "sys-plugins") in why and not root.exists(), "names where it looked; writes nothing"
    plug = tmp_path / "elsewhere" / "docker-compose"
    plug.parent.mkdir()
    plug.write_text("#!/bin/sh\nexit 0\n")
    plug.chmod(0o755)
    for setting in (str(plug), str(plug.parent)):   # the plugin file, or a folder holding it
        extra, why = harborslice.compose(root, {"compose": setting}, env, which=lambda n: docker)
        own = root / "docker-config"
        assert extra == {"DOCKER_CONFIG": str(own)} and why == ""
        link = own / "cli-plugins" / "docker-compose"
        assert link.is_symlink() and link.resolve() == plug.resolve()
        assert sorted(p.name for p in own.rglob("*")) == ["cli-plugins", "docker-compose"], "only the link"
    shutil.rmtree(root)
    sysdir = tmp_path / "sys-plugins"
    sysdir.mkdir()
    (sysdir / "docker-compose").symlink_to(plug)
    extra, _ = harborslice.compose(root, st, env, which=lambda n: docker)
    assert extra and (root / "docker-config" / "cli-plugins" / "docker-compose").resolve() == plug.resolve()
    assert harborslice.settings({"model_bench": {"terminalbench": {"compose": "/x"}}})["compose"] == "/x"


def test_terminalbench_step_without_compose_stops_before_any_trial_with_one_message(env, monkeypatch, capsys):
    import argparse as ap
    import types
    from ttp import cli, endpoint, harborslice, tbcycle
    started = []
    monkeypatch.setenv("HOME", str(env["tmp"] / "home"))
    monkeypatch.setattr(harborslice, "PLUGIN_DIRS", ())
    monkeypatch.setattr(tbcycle, "launch", lambda *x, **k: started.append(x))
    monkeypatch.setattr(tbcycle, "step", lambda *x, **k: started.append("step") or {})
    monkeypatch.setattr(endpoint, "list_models", lambda *x: (True, "", ["m-a"]))
    monkeypatch.setattr(endpoint, "credentials", lambda alias: types.SimpleNamespace(url="u", key="k"))
    docker = _fake_docker(env["tmp"])
    monkeypatch.setattr(harborslice.shutil, "which", lambda n: docker if n == "docker" else None)
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch)
    cfg["providers"] = {"chat": {"endpoint": "gw"}}
    p = make(env)
    p.config = lambda: cfg
    a = ap.Namespace(action="terminalbench", models=None, runner=None, reference=False)
    with pytest.raises(SystemExit):
        cli._terminalbench(p, a, clock["t"])
    err = capsys.readouterr().err.strip()
    assert len(err.splitlines()) == 1 and "docker compose is missing" in err
    assert started == [], "no step, no runner launched"
    from ttp import benchowner, modelbench
    assert json.loads((modelbench.home() / f"streak-{benchowner.TB_SCHEDULE}.json").read_text())["fails"] == 1, \
        "the failure is counted"


def test_terminalbench_compose_binary_in_the_benchs_own_folder_needs_no_setting(tmp_path, monkeypatch):
    from ttp import harborslice
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(harborslice, "PLUGIN_DIRS", ())
    root = tmp_path / "bench"
    docker = _fake_docker(tmp_path)
    env = {"PATH": os.environ.get("PATH", "")}
    assert harborslice.compose(root, {"compose": ""}, env, which=lambda n: docker)[0] is None
    (root / "compose").mkdir(parents=True)
    plug = root / "compose" / "docker-compose"
    plug.write_text("#!/bin/sh\nexit 0\n")
    plug.chmod(0o755)
    extra, why = harborslice.compose(root, {"compose": ""}, env, which=lambda n: docker)
    assert extra == {"DOCKER_CONFIG": str(root / "docker-config")} and why == ""
    assert (root / "docker-config" / "cli-plugins" / "docker-compose").resolve() == plug.resolve()
    from ttp import modelbench, tbcycle   # the CLI looks in the bench's own folder, not its harbor venv folder
    monkeypatch.setattr(modelbench, "home", lambda: root.parent)
    monkeypatch.setattr(tbcycle, "DIR", root.name)
    monkeypatch.setattr(harborslice.shutil, "which", lambda n: docker)
    assert tbcycle.compose({}, env) == ({"DOCKER_CONFIG": str(root / "docker-config")}, "")


def test_terminalbench_settings_are_known_to_ttp_config_and_limit_zero_keeps_each_tasks_own_limit(env, tmp_path):
    from ttp import cli, harborslice, project
    for key in ("model_bench.terminalbench", "model_bench.terminalbench.compose", "model_bench.terminalbench.limit_s",
                "model_bench.terminalbench.reference.bridge"):
        assert project.unknown_key_hint(key) is None, key
    assert project.unknown_key_hint("model_bench.terminalbenchx") is not None, "a typo is still caught"
    p = make(env)
    cli.main(["config", "demo", "model_bench.terminalbench.compose", "/opt/compose"])
    assert p.raw_config()["model_bench"]["terminalbench"]["compose"] == "/opt/compose"
    assert harborslice.settings({"model_bench": {"terminalbench": {"limit_s": 0}}})["limit_s"] == 0
    assert harborslice.settings({"model_bench": {"terminalbench": {"limit_s": 5}}})["limit_s"] == harborslice.LIMIT_S
    ds = tmp_path / "ds"
    (ds / "t-a").mkdir(parents=True)
    (ds / "t-a" / "task.toml").write_text("[agent]\ntimeout_sec = 1800.0\n")
    st = {**harborslice.settings({"model_bench": {"terminalbench": {"limit_s": 0}}}), "dataset": str(ds)}
    calls = []

    def runner(cmd, e, timeout):
        calls.append((cmd, timeout))
        _fake_trial(cmd, 1.0)
        return 0, ""
    harborslice.trial("m", "t-a", tmp_path / "jobs", {}, st, runner, sleep=lambda s: None)
    assert "--agent-timeout-multiplier" not in calls[0][0], "no multiplier: the task's own limit, the same for all"
    assert calls[0][1] >= 1800 * 4, "the process ceiling follows the task's own limit"


class _FakeBridge:
    """The claude CLI bridge's surface (claudebridge.ClaudeCliBridge) with no process."""
    made = []

    def __init__(self, model, effort):
        self.model, self.effort, self.state = model, effort, []
        _FakeBridge.made.append(self)

    def start(self):
        self.state.append("start")
        return self

    def stop(self):
        self.state.append("stop")

    def openai_env(self):
        return {"OPENAI_BASE_URL": "http://127.0.0.1:1/v1", "OPENAI_API_KEY": "run-token"}

    def cost_so_far(self):
        return 1.2345

    def stats(self):
        return {"claude_cli": {"version": "9.9 (Claude Code)"}, "server": {"calls": 2}}


def test_terminalbench_reference_through_the_claude_bridge_has_no_endpoint_its_own_effort_and_books_cost(env, monkeypatch):
    from ttp import harborslice, tbcycle
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch, reference={
        "models": ["claude-sonnet-5-5"], "bridge": ["claude-sonnet-5-5"], "efforts": {"claude-sonnet-5-5": "high"}})
    seen = []

    def fake_trial(model, task, root, e, st, runner=None, command=None, home=None, **k):
        seen.append((task, e.get("OPENAI_BASE_URL"), e.get("OPENAI_API_KEY"), command))
        return {"reward": 1.0, "min": 1.0, "error": None}
    monkeypatch.setattr(harborslice, "trial", fake_trial)
    _FakeBridge.made.clear()
    assert tbcycle.ref_runner(cfg, "claude-sonnet-5-5", {"A": "b"}, clock=lambda: clock["t"], bridge=_FakeBridge) == 3
    assert len(_FakeBridge.made) == 3, "one bridge per trial: its server wait and tokens/s are the trial's own"
    b = _FakeBridge.made[0]
    assert (b.model, b.effort, b.state) == ("claude-sonnet-5-5", "high", ["start", "stop"]), "up for the trial, then down"
    assert {x[1:3] for x in seen} == {("http://127.0.0.1:1/v1", "run-token")}, "the agent reaches the bridge, no key"
    assert all("reasoning_effort" not in x[3] for x in seen), "the CLI carries the effort, not the call arguments"
    row = tbcycle.reference()[0]
    assert row["route"] == "claude-cli" and row["effort"] == "high" and row["cost_usd"] == 3.7035, "the trials' bridges summed"
    assert row["claude_cli"] == "9.9 (Claude Code)" and row["passed"] == 3
    got = tbcycle.ref_start(cfg, ["something-else"], [].append)   # a bridge model needs no endpoint listing
    assert got["not_served"] == []


def test_terminalbench_reference_runs_parallel_tasks_and_wraps_endpoint_runners_in_the_chat_server_lock(env, monkeypatch):
    import threading
    import types
    from ttp import tbcycle
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch, tasks=[f"t{i}" for i in range(6)], reference={
        "models": ["open-m", "claude-x"], "bridge": ["claude-x"], "parallel": 3, "endpoint": "gw"})
    now, peak, gate = [0], [0], threading.Lock()

    def trial(model, task, root):
        with gate:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.05)
        with gate:
            now[0] -= 1
        return {"reward": 1.0, "min": 1.0, "error": None}
    assert tbcycle.ref_runner(cfg, "open-m", {}, trial=trial, clock=lambda: clock["t"]) == 6
    assert 1 < peak[0] <= 3, "reference.parallel tasks at once, never more"
    assert len((tbcycle.folder() / "reference" / "open-m.jsonl").read_text().splitlines()) == 6, "one row each"
    argv = []
    monkeypatch.setattr(tbcycle.subprocess, "Popen", lambda a, **k: argv.append(a))
    p = types.SimpleNamespace(root=env["tmp"], config=lambda: cfg)
    tbcycle.launch(p, "open-m", reference=True)
    tbcycle.launch(p, "claude-x", reference=True)
    tbcycle.launch(p, "open-m")
    assert argv[0][3:6] == ["lock", "chat-server", "--"] and argv[0][-3:] == ["--reference", "--runner", "open-m"]
    assert "lock" not in argv[1] and "--reference" in argv[1], "the claude arm takes no chat-server lock"
    assert "lock" not in argv[2], "the paced cycle is unchanged"
    cfg["model_bench"]["terminalbench"]["reference"]["lock"] = ""
    tbcycle.launch(p, "open-m", reference=True)
    assert "lock" not in argv[3], "an empty lock setting wraps nothing"


def test_terminalbench_preflight_raises_one_event_on_the_third_failure_in_a_row_and_a_pass_resets(env, monkeypatch):
    from ttp import tbcycle
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch)
    p = make(env)
    events = lambda: p.db.q("SELECT source, kind, severity, text FROM events WHERE source = 'model-bench'")
    why = "docker compose is missing: no working plugin"
    assert [tbcycle.preflight(p, why, clock["t"] + i) for i in (1, 2)] == [1, 2] and events() == []
    assert tbcycle.preflight(p, why, clock["t"] + 3) == 3
    ev = events()
    assert len(ev) == 1 and ev[0]["kind"] == "observation" and why in ev[0]["text"] and "3 times in a row" in ev[0]["text"]
    assert tbcycle.preflight(p, why, clock["t"] + 4) == 4 and len(events()) == 1, "one event per streak"
    assert tbcycle.preflight(p, "", clock["t"] + 5) == 0, "a pass resets the count"
    tbcycle.preflight(p, why, clock["t"] + 6)
    tbcycle.preflight(p, why, clock["t"] + 7)
    assert len(events()) == 1, "two failures after a pass raise nothing"
    tbcycle.preflight(p, why, clock["t"] + 8)
    assert len(events()) == 2, "a new streak of three raises its own event"


def _tb_calls(n, tps, wait):
    return [{"st": "ok", "out": 400, "tps": tps, "wait_s": wait} for _ in range(n)]


class _FakeChat:
    """The chat relay's surface (chatbridge.Bridge) with no process; per_call is the next trial's calls."""
    made, plan = [], []

    def __init__(self, model):
        self.model, self.per_call = model, (_FakeChat.plan.pop(0) if _FakeChat.plan else _tb_calls(12, 50.0, 1.0))
        _FakeChat.made.append(self)

    def start(self):
        return self

    def stop(self):
        pass

    def openai_env(self):
        return {"OPENAI_BASE_URL": "http://127.0.0.1:2/v1", "OPENAI_API_KEY": "run-token"}

    def stats(self):
        if not self.per_call:
            return {}   # like chatbridge: no "server" block when the agent made no call
        return {"server": {"calls": len(self.per_call), "wait_median_s": self.per_call[0]["wait_s"],
                           "tps_median": self.per_call[0]["tps"], "per_call": self.per_call,
                           **({"failed_calls": n} if (n := sum(c["st"] != "ok" for c in self.per_call)) else {})}}


def test_terminalbench_reference_chat_route_holds_out_a_slow_server_cut_reruns_it_healthy_and_writes_the_table(
        env, monkeypatch):
    from ttp import serverhealth, tbcycle
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch, tasks=("t1", "t2", "t3"), limit_s=0, reference={
        "models": ["open-m"], "chat": ["open-m"], "efforts": {"open-m": "max"}, "run": "r89", "parallel": 1,
        "note": "older rows are not comparable"})
    serverhealth.History(tbcycle.folder() / tbcycle.HISTORY).add("open-m", _tb_calls(20, 50.0, 1.0))
    _FakeChat.made.clear()
    _FakeChat.plan[:] = [_tb_calls(12, 50.0, 1.0), _tb_calls(12, 5.0, 30.0), _tb_calls(12, 50.0, 1.0), _tb_calls(12, 50.0, 1.0)]
    tries = []

    def trial(model, task, root):
        tries.append(task)
        if task == "t2" and tries.count("t2") == 1:
            return {"reward": 0.0, "min": 15.0, "error": "AgentTimeoutError"}   # cut while the server ran slow
        if task == "t3":
            return {"reward": 0.0, "min": 15.0, "error": "AgentTimeoutError"}   # cut at normal speed: the model's
        return {"reward": 1.0, "min": 3.0, "error": None}
    probes = []
    monkeypatch.setattr(serverhealth, "probe", lambda b, m, *a, **k: probes.append(m) or
                        {"ok": True, "wait_s": 1.0, "tps": 50.0})
    assert tbcycle.ref_runner(cfg, "open-m", {}, trial=trial, clock=lambda: clock["t"], chat=_FakeChat,
                              sleep=sleep) == 4
    assert tries == ["t1", "t2", "t3", "t2"] and probes == ["open-m"], "the server's cut reran after a healthy probe"
    lines = [json.loads(x) for x in (tbcycle.ref_dir("r89") / "open-m.jsonl").read_text().splitlines()]
    assert lines[1]["outcome"] == "server" and lines[1]["reward"] is None and lines[1]["heldout_reward"] == 0.0
    assert lines[2].get("outcome") != "server", "a cut at normal speed stays the model's fail"
    assert lines[3]["task"] == "t2" and lines[3]["rerun"] == 1 and lines[3]["reward"] == 1.0
    row = tbcycle.reference("r89")[0]
    assert (row["passed"], row["trials"], row.get("server", 0)) == (2, 3, 0) and row["route"] == "chat"
    assert row["limit_s"] == "each task's own (task.toml)" and "probe shows it healthy" in row["rerun_rule"]
    assert not tbcycle.reference(), "a named run books apart from the default reference"
    md = (tbcycle.ref_dir("r89") / "table.md").read_text()
    assert "| open-m | max | chat | 2 of 3 |" in md
    assert "open-m / t2: server (" in md and "rerun on a healthy server: passed" in md
    assert "open-m / t3: model: cut at the limit while the server ran at normal speed" in md
    assert "request no effort field (none in providers.chat.effort_fields)" in md, "how the effort reached the server"
    assert "## How each arm was called" in md and "older rows are not comparable" in md
    # one run per model: each row gives its 95% interval and the table names no winner, so a model comparison
    # in it passes the bench verdict rule (corrections #3064, #3111); no verdict word in the arm wording either
    assert re.search(r"\| 2 of 3 \| \d+%-\d+% \|", md) and "cannot separate models whose intervals overlap" in md
    assert not re.search(r"\bbehind\b", md)


def test_terminalbench_reference_rejudges_a_cut_made_before_the_models_normal_speed_was_known(env, monkeypatch):
    from ttp import serverhealth, tbcycle
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch, tasks=("t1", "t2"), reference={
        "models": ["open-m"], "chat": ["open-m"], "parallel": 1, "rounds": 0})
    _FakeChat.made.clear()
    _FakeChat.plan[:] = [_tb_calls(3, 5.0, 30.0), _tb_calls(12, 50.0, 1.0)]   # t1's slow calls come first: no normal yet

    def trial(model, task, root):
        return {"reward": 0.0, "min": 15.0, "error": "AgentTimeoutError"} if task == "t1" else \
            {"reward": 1.0, "min": 3.0, "error": None}
    monkeypatch.setattr(serverhealth, "MIN_CALLS", 10)
    tbcycle.ref_runner(cfg, "open-m", {}, trial=trial, clock=lambda: clock["t"], chat=_FakeChat, sleep=sleep)
    got = tbcycle._rows(tbcycle.ref_dir("") / "open-m.jsonl", ["t1", "t2"])
    assert got["t1"]["outcome"] == "server" and "server_calls" not in got["t1"], "judged again once normal was known"
    row = tbcycle.reference()[0]
    assert (row["passed"], row["trials"], row["server"]) == (1, 1, 1), "rounds 0: held out, never failed"


def test_terminalbench_chat_reference_model_needs_no_reference_endpoint(env, monkeypatch):
    from ttp import tbcycle
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch, reference={
        "models": ["a", "b", "c"], "bridge": ["a"], "chat": ["b"]})
    s = tbcycle.settings(cfg)
    assert [tbcycle.route(s, m) for m in "abc"] == ["claude-cli", "chat", "endpoint"]
    got = tbcycle.ref_start(cfg, ["c"], [].append)
    assert got["not_served"] == [], "only the endpoint route is checked against the endpoint's model list"
    bad = {"model_bench": {"terminalbench": {"reference": {"run": "../x", "rounds": -1}}}}
    assert tbcycle.settings(bad)["reference"]["run"] == "" and tbcycle.settings(bad)["reference"]["rounds"] == 3


def test_bench_parity_marks_rows_with_a_different_limit_task_set_or_rerun_rule_not_comparable():
    from ttp import benchparity
    base = {"harness": "harbor 0.26.0", "agent": "terminus-2", "task_set": benchparity.task_set(["b", "a"]),
            "tasks": 2, "limit_s": 900, "limit_counts": "agent wall time", "rerun_rule": "server cut-offs rerun",
            "provider": "chat", "kind": "relay", "model": "m1", "effort": "max"}
    assert benchparity.task_set(["a", "b"]) == base["task_set"], "order does not change the task set"
    rows = [base, {**base, "model": "m2"}, {**base, "model": "m3", "limit_s": 0},
            {**base, "model": "m4", "task_set": benchparity.task_set(["a", "c"])},
            {**base, "model": "m5", "rerun_rule": "unlimited reruns"}]
    got = benchparity.compare(rows)
    assert set(got["differs"]) == {"limit_s", "task_set", "rerun_rule"}
    assert got["not_comparable"] == ["m3 (max)", "m4 (max)", "m5 (max)"]
    md = "\n".join(benchparity.section(rows))
    assert "Not comparable: m3 (max), m4 (max), m5 (max)" in md and "- m1 (max): provider chat; kind relay" in md
    assert "All 2 rows ran under the same conditions" in "\n".join(benchparity.section(rows[:2]))
    assert "(not recorded)" in "\n".join(benchparity.section([base, {"model": "old", "effort": "x"}]))


def test_every_scheduled_bench_cycle_raises_one_event_naming_its_cause_on_the_third_failure_in_a_row(env, monkeypatch, capsys):
    import argparse as ap
    from ttp import benchowner, cli, modelbench
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch)
    p = make(env)
    events = lambda: [r["text"] for r in p.db.q("SELECT text FROM events WHERE source = 'model-bench'")]
    out = {"v": {"skipped": "no working key for chat endpoint gw: 401", "report": True,
                 "failed": "no working key for chat endpoint gw: 401"}}
    monkeypatch.setattr(modelbench, "run", lambda *x, **k: out["v"])
    monkeypatch.setattr(cli, "here", lambda: p)
    p.config = lambda: cfg
    daily = ap.Namespace(action="run", detach=False, force=False)
    for _ in range(2):
        cli.cmd_model_bench(daily)
    assert events() == []
    out["v"] = {"skipped": "another project on this host is running today's bench", "report": False}
    cli.cmd_model_bench(daily)
    assert events() == [], "a busy lock is no run: the streak neither grows nor resets"
    out["v"] = {"skipped": "no working key for chat endpoint gw: 401", "report": False,
                "failed": "no working key for chat endpoint gw: 401"}
    cli.cmd_model_bench(daily)
    ev = events()
    assert len(ev) == 1 and "model-bench failed 3 times in a row" in ev[0] and "401" in ev[0], ev
    cli.cmd_model_bench(daily)
    assert len(events()) == 1, "one event per streak"
    out["v"] = {"date": "d", "results": {}}
    cli.cmd_model_bench(daily)
    assert json.loads((modelbench.home() / "streak-model-bench.json").read_text())["fails"] == 0, "a pass resets"

    def suite(fail):   # the suite (with its device A/B) dies with its cause on stderr
        def fn():
            if fail:
                cli.die("model-bench suite: no complete bench folder pair")
        return lambda: cli._bench_cycle(p, benchowner.SUITE_SCHEDULE, clock["t"], fn)
    for _ in range(3):
        with pytest.raises(SystemExit):
            suite(True)()
    ev = [t for t in events() if "model-suite" in t]
    assert len(ev) == 1 and "no complete bench folder pair" in ev[0] and "ttp: " not in ev[0], ev
    assert "no complete bench folder pair" in capsys.readouterr().err, "the cause still reaches the log"

    def crash():
        raise RuntimeError("step broke")
    for _ in range(3):   # a Terminal-Bench step that crashes counts in the same streak as its pre-flight
        with pytest.raises(RuntimeError):
            cli._bench_cycle(p, benchowner.TB_SCHEDULE, clock["t"], crash, dies=False)
    ev = [t for t in events() if "terminalbench" in t]
    assert len(ev) == 1 and "RuntimeError: step broke" in ev[0], ev
    for _ in range(3):
        with pytest.raises(SystemExit):   # its pre-flight already counted a die: not counted twice
            cli._bench_cycle(p, benchowner.TB_SCHEDULE, clock["t"] + 10, lambda: cli.die("off"), dies=False)
    assert json.loads((modelbench.home() / "streak-terminalbench.json").read_text())["fails"] == 3


def test_terminalbench_reference_marks_a_trial_with_no_model_call_or_broken_calls_not_run_and_reruns_it(
        env, monkeypatch):
    from ttp import serverhealth, tbcycle
    d, cfg, clock, sleeps, sleep = _tb_setup(env, monkeypatch, tasks=("t1", "t2", "t3"), limit_s=0, reference={
        "models": ["open-m"], "chat": ["open-m"], "run": "nr", "parallel": 1, "rounds": 1})
    serverhealth.History(tbcycle.folder() / tbcycle.HISTORY).add("open-m", _tb_calls(20, 50.0, 1.0))
    broken = _tb_calls(12, 50.0, 1.0) + [{"st": "fail", "out": 0, "tps": None, "wait_s": 1.0}]
    _FakeChat.made.clear()
    _FakeChat.plan[:] = [[], broken, _tb_calls(12, 50.0, 1.0), _tb_calls(12, 50.0, 1.0)]
    tries = []

    def trial(model, task, root):
        tries.append(task)
        return {"reward": 0.0 if task == "t3" else 1.0, "min": 3.0, "error": None}
    monkeypatch.setattr(serverhealth, "probe", lambda *a, **k: {"ok": True, "wait_s": 1.0, "tps": 50.0})
    tbcycle.ref_runner(cfg, "open-m", {}, trial=trial, clock=lambda: clock["t"], chat=_FakeChat, sleep=sleep)
    got = tbcycle._rows(tbcycle.ref_dir("nr") / "open-m.jsonl", ["t1", "t2", "t3"])
    assert tries == ["t1", "t2", "t3", "t1"], "only the zero-call trial reruns; t2 passed despite a failed call"
    assert got["t1"]["rerun"] == 1 and got["t1"]["reward"] == 1.0 and not tbcycle.not_run(got["t1"])
    assert not tbcycle.not_run(got["t3"]), "a model that made calls and did not solve the task failed"
    lines = [json.loads(x) for x in (tbcycle.ref_dir("nr") / "open-m.jsonl").read_text().splitlines()]
    assert lines[0]["outcome"] == tbcycle.NOT_RUN and lines[0]["reward"] is None and lines[0]["heldout_reward"] == 1.0
    md = (tbcycle.ref_dir("nr") / "table.md").read_text()
    assert "| chat | 2 of 3 | 21%-94% | 3 | 0 | 0 | 0 |" in md, "passed 2 (95% interval 21-94%), scored 3, no server cut, no fault, none not run"
    assert "| chat | 2 of 3 | 21%-94% | 3 | 0 | 0 | 0 | 0 | 1 of 37 |" in md, "1 failed call of the 37 over every trial run"


def test_terminalbench_not_run_covers_no_calls_and_server_broken_unsolved_trials_only():
    from ttp import tbcycle
    assert tbcycle.not_run({"outcome": tbcycle.NOT_RUN})
    assert tbcycle.not_run({"reward": 0.0, "server": {"calls": 9, "failed_calls": 2}}), "the server broke it"
    assert not tbcycle.not_run({"reward": 1.0, "server": {"calls": 9, "failed_calls": 2}}), "a pass stands"
    assert not tbcycle.not_run({"reward": 0.0, "server": {"calls": 9}}), "worked and failed: the model's fail"
    assert not tbcycle.not_run({"outcome": "server", "reward": None, "server": {"failed_calls": 3}}), "a server cut"
    lost = {"reward": None, "error": "harbor rc 1: Docker daemon is not running.", "server": {"calls": 18}}
    assert tbcycle.not_run(lost), "harbor failed before grading: never graded, so not run, never a fail"
    assert not tbcycle.not_run({**lost, "reward": 0.0}), "a graded trial with a harbor error stands"
    assert not tbcycle.not_run({"reward": None, "error": "AgentTimeoutError", "server": {"calls": 9}})
    md_rows = {"t1": {"task": "t1", "reward": 0.0, "server": {"calls": 4, "failed_calls": 1}}}
    assert [t for t, r in md_rows.items() if tbcycle.not_run(r)] == ["t1"]
