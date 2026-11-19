# Server-wait accounting tests (serverhealth.py, benchrun server cells and reruns, the codex proxy, the
# planner-then-executor arm) of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file. Add new tests of the
# server-wait rule here.
from __future__ import annotations


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


def _healthy_rows(n=12, tps=40.0, wait=0.4, out=200):
    return [{"tps": tps, "wait_s": wait, "out": out, "st": "ok", "dur_s": out / tps} for _ in range(n)]


def test_serverhealth_a_call_records_wait_tokens_and_tokens_per_second():
    from ttp import serverhealth
    clock = _Clock()
    calls = serverhealth.Calls(clock=clock)
    c = calls.begin()
    clock.tick(3.0)          # the server took 3 s to answer
    c.byte()
    for _ in range(10):      # then 10 s of generation, a chunk a second
        clock.tick(1.0)
        c.byte()
    c.end(400)
    (row,) = calls.rows
    assert row["wait_s"] == 3.0 and row["out"] == 400 and row["st"] == "ok"
    assert row["tps"] == round(400 / 10.0, 2)
    s = calls.summary()
    assert s["calls"] == 1 and s["wait_s"] == 3.0 and s["tps_median"] == 40.0 and s["failed_calls"] == 0


def test_serverhealth_a_stream_gap_and_an_open_call_count_as_server_wait():
    from ttp import serverhealth
    clock = _Clock()
    calls = serverhealth.Calls(clock=clock)
    c = calls.begin()
    clock.tick(1.0)
    c.byte()
    clock.tick(30.0)         # a stall in the stream: the server, not the model
    c.byte()
    assert calls.wait_s() == 30.0 + 1.0 * 0   # the closed gap counts live
    clock.tick(20.0)         # the stream is silent now: still waiting
    assert calls.wait_s() >= 30.0 + 20.0
    c.end(100)
    assert calls.rows[0]["wait_s"] >= 50.0
    calls.sleep_wait(7.0)    # a capacity retry the bridge slept itself
    assert calls.summary()["wait_s"] >= 57.0


def test_serverhealth_a_failed_call_is_all_wait_and_a_slow_call_is_charged_for_its_excess():
    from ttp import serverhealth
    clock = _Clock()
    calls = serverhealth.Calls(clock=clock)
    calls.configure(normal_tps=40.0, factor=2.0)
    c = calls.begin()
    clock.tick(12.0)
    c.end(0, "fault")
    assert calls.rows[-1]["wait_s"] == 12.0 and calls.rows[-1]["tps"] == 0.0
    c = calls.begin()
    c.byte()
    for _ in range(40):      # 400 tokens in 40 s is 10 tokens/s, under half of normal (40)
        clock.tick(1.0)
        c.byte()
    c.end(400)
    row = calls.rows[-1]
    assert row["tps"] == 10.0 and row["slow_s"] == 30.0 and row["wait_s"] == 30.0   # 40 s - the 10 s normal speed needs


def test_serverhealth_the_limit_counts_agent_time_and_a_wall_ceiling_stops_a_hung_server():
    from ttp import serverhealth
    assert serverhealth.agent_s(100.0, 30.0) == 70.0 and serverhealth.agent_s(10.0, 99.0) == 0.0
    assert serverhealth.ceiling_s(3600) == 3 * 3600 and serverhealth.ceiling_s(3600, explicit=5000) == 5000
    # 4000 s of wall with 1000 s waiting on the server is 3000 s of the agent's own time: inside a 3600 s limit.
    assert serverhealth.limit_hit(4000.0, 1000.0, 3600.0, 10800.0) == ""
    assert serverhealth.limit_hit(4000.0, 0.0, 3600.0, 10800.0) == "timeout"
    assert serverhealth.limit_hit(11000.0, 9000.0, 3600.0, 10800.0) == "ceiling"
    assert serverhealth.limit_hit(3700.0, 0.0, 3600.0, 10800.0, credit_s=200.0) == ""   # lock wait is credited


def test_serverhealth_history_gives_a_models_normal_speed_from_its_own_healthy_calls(tmp_path):
    from ttp import serverhealth
    h = serverhealth.History(tmp_path / "hist.json", window=20)
    assert h.normal("m") is None
    h.add("m", _healthy_rows(5) + [{"tps": 5.0, "wait_s": 90.0, "out": 4, "st": "ok"},   # too few tokens: ignored
                                   {"tps": 0.0, "wait_s": 90.0, "out": 0, "st": "fault"}])
    assert h.normal("m") is None, "fewer than MIN_CALLS healthy calls: no normal yet"
    h.add("m", _healthy_rows(30))
    n = h.normal("m")
    assert n == {"calls": 20, "tps": 40.0, "wait_s": 0.4}, "the window keeps the latest calls"
    assert h.normal("other") is None
    assert serverhealth.History(None).add("m", _healthy_rows()) == 0


def test_serverhealth_a_cut_cell_is_server_only_when_its_calls_ran_slower_than_normal():
    from ttp import serverhealth
    normal = {"calls": 50, "tps": 40.0, "wait_s": 0.5}
    slow = [{"tps": 12.0, "wait_s": 0.5, "out": 300, "st": "ok", "dur_s": 25.0} for _ in range(20)]
    fast = _healthy_rows(20)
    why = serverhealth.classify("timeout", {"server": {"per_call": slow}}, normal)
    assert why and why[0] == "hit the limit" and any("tokens/s" in w for w in why)
    assert serverhealth.classify("ceiling", {"server": {"per_call": slow}}, normal)[0] == "hit the ceiling"
    assert serverhealth.classify("timeout", {"server": {"per_call": fast}}, normal) == [], "normal speed: the model's"
    assert serverhealth.classify("timeout", {"server": {"per_call": slow}}, None) == [], "no normal known: cannot tell"
    assert serverhealth.classify(None, {"server": {"per_call": slow}}, normal) == [], "not cut: nothing to explain"
    # A server that slowed late counts, though the cell's median looked normal.
    late = fast + [{"tps": 5.0, "wait_s": 40.0, "out": 300, "st": "ok", "dur_s": 60.0}] * 8
    assert serverhealth.classify("timeout", {"server": {"per_call": late}}, normal)


def test_serverhealth_a_capacity_refusal_the_bridge_gave_up_on_is_the_servers():
    from ttp import serverhealth
    gave_up = {"busy": {"waits": 6, "gave_up": 1, "waited_s": 300.0}, "last_fault": "rate_limit",
               "upstream": {"status": 429, "code": "rate_limit_exceeded"}}
    assert serverhealth.refused(gave_up)
    why = serverhealth.classify(None, gave_up, None)
    assert why and "capacity refusal" in why[0]
    assert serverhealth.refused({"upstream": {"status": 503}}) and serverhealth.refused({"last_fault": "not_served"})
    assert not serverhealth.refused({"last_fault": "malformed"}) and not serverhealth.refused({})


def test_serverhealth_mark_and_queue_keep_a_server_cell_unscored_until_its_rerun():
    from ttp import serverhealth
    old = {"task": "1", "rep": 1, "arm": "a", "outcome": "fail", "passed": False, "hang_guard": "x"}
    row = serverhealth.mark(dict(old), ["hit the limit"])
    assert row["outcome"] == "server" and row["passed"] is None and "hang_guard" not in row
    assert serverhealth.describe(row) == "not scored (server)" and serverhealth.describe(old) == "fail"
    rerun = {"task": "1", "rep": 1, "arm": "a", "outcome": "done", "passed": True}
    other = {"task": "2", "rep": 1, "arm": "a", "outcome": "server", "passed": None}
    assert serverhealth.queue([old, row, other]) == [row, other]
    assert serverhealth.queue([old, row, other, rerun]) == [other], "the latest row of a cell wins"


def test_serverhealth_a_probe_must_answer_at_normal_speed_and_the_wait_ends_when_it_does():
    from ttp import serverhealth
    normal = {"calls": 50, "tps": 40.0, "wait_s": 0.5}
    assert serverhealth.healthy({"ok": True, "tps": 38.0, "wait_s": 0.6}, normal)
    assert not serverhealth.healthy({"ok": True, "tps": 12.0, "wait_s": 0.6}, normal)
    assert not serverhealth.healthy({"ok": True, "tps": 38.0, "wait_s": 30.0}, normal)
    assert not serverhealth.healthy({"ok": False, "why": "HTTPError"}, normal)
    assert serverhealth.healthy({"ok": True}, None)
    answers = iter([{"ok": False}, {"ok": True, "tps": 5.0, "wait_s": 0.1}, {"ok": True, "tps": 41.0, "wait_s": 0.4}])
    slept, clock = [], _Clock(0.0)
    ok, last = serverhealth.wait_healthy(lambda: next(answers), normal, every_s=60.0, max_s=600.0,
                                         sleep=lambda s: (slept.append(s), clock.tick(s)), clock=clock)
    assert ok and last["tps"] == 41.0 and slept == [60.0, 60.0]
    never = lambda: {"ok": False}
    clock = _Clock(0.0)
    ok, _ = serverhealth.wait_healthy(never, normal, every_s=100.0, max_s=250.0,
                                      sleep=lambda s: clock.tick(s), clock=clock)
    assert not ok and clock.t <= 250.0


def _server_out(tmp_path, rows):
    out = tmp_path / "out"
    out.mkdir()
    (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return out


def test_benchrun_server_status_marks_a_slow_cut_cell_and_leaves_a_slow_pass_alone(tmp_path):
    from ttp import benchrun, serverhealth
    hist = tmp_path / "hist.json"
    serverhealth.History(hist).add("m", _healthy_rows(30))
    opts = {"server_history": str(hist)}
    slow = [{"tps": 10.0, "wait_s": 0.5, "out": 300, "st": "ok", "dur_s": 30.0} for _ in range(15)]
    ended = {"stopped": "timeout", "bridge": {"server": {"calls": 15, "per_call": slow}},
             "limits": {"rule": "agent_time", "timeout_s": 3600}}
    row = {"model": "m", "outcome": "fail", "passed": False}
    benchrun.server_status(row, ended, opts)
    assert row["outcome"] == "server" and row["passed"] is None and row["limit_rule"] == "agent_time"
    assert row["server_reason"][0] == "hit the limit" and "per_call" not in row["server"]
    passed = {"model": "m", "outcome": "done", "passed": True}
    benchrun.server_status(passed, {**ended, "stopped": None}, opts)
    assert passed["outcome"] == "done" and passed["passed"] is True
    # A cut cell at normal speed is the model's fail, and its calls feed the model's normal speed.
    fine = {"model": "m", "outcome": "fail", "passed": False}
    benchrun.server_status(fine, {"stopped": "timeout", "bridge": {"server": {"per_call": _healthy_rows(15)}}}, opts)
    assert fine["outcome"] == "fail" and fine["limit_rule"] == "wall_time"
    assert serverhealth.History(hist).normal("m")["calls"] == 45


def test_benchrun_recheck_stops_marks_older_stops_server_keeping_what_they_were(tmp_path):
    from ttp import benchrun, serverhealth
    out = _server_out(tmp_path, [
        {"task": "1", "rep": 1, "arm": "a", "outcome": "fail", "passed": False, "wall_s": 3601.0, "hang_guard": "timeout"},
        {"task": "2", "rep": 1, "arm": "a", "outcome": "done", "passed": True}])
    added = benchrun.recheck_stops(out, [("1", 1, "a"), ("2", 1, "a"), ("3", 1, "a")], "recorded before accounting")
    assert [r["task"] for r in added] == ["1"]
    assert added[0]["outcome"] == "server" and added[0]["server_reason"] == ["recorded before accounting"]
    assert added[0]["was"]["outcome"] == "fail" and added[0]["was"]["wall_s"] == 3601.0
    rows = [json.loads(x) for x in (out / "results.jsonl").read_text().splitlines()]
    assert [r["task"] for r in serverhealth.queue(rows)] == ["1"]
    assert benchrun.recheck_stops(out, [("1", 1, "a")], "again") == [], "already server: no second row"


def test_benchrun_rerun_cells_run_one_at_a_time_after_a_healthy_probe_and_replace_the_row(tmp_path, monkeypatch):
    from ttp import benchrun, serverhealth
    out = _server_out(tmp_path, [
        {"task": "1", "rep": 1, "arm": "a", "outcome": "server", "passed": None, "server_reason": ["x"]},
        {"task": "2", "rep": 1, "arm": "a", "outcome": "done", "passed": True},
        {"task": "3", "rep": 1, "arm": "a", "outcome": "server", "passed": None, "server_reason": ["y"]}])
    kept = out / "cells" / "1-r1-a"
    kept.mkdir(parents=True)
    (kept / "marker").write_text("old attempt")
    order = []
    monkeypatch.setattr(benchrun, "run_cell", lambda bd, o, task, cell, arm, opts, cfg: (
        order.append(task["task"]) or {"task": task["task"], "rep": cell["rep"], "arm": cell["arm"],
                                       "outcome": "done", "passed": task["task"] == "1"}))
    probes = iter([{"ok": False}, {"ok": True, "tps": 40.0, "wait_s": 0.4}, {"ok": True, "tps": 40.0, "wait_s": 0.4}])
    slept, locks = [], []

    class Lock:
        def __enter__(self):
            locks.append("in")

        def __exit__(self, *a):
            locks.append("out")
    arms = {"a": {"provider": "chat", "model": "m", "effort": "max"}}
    tasks = {"1": {"task": "1"}, "3": {"task": "3"}}
    added = benchrun.rerun_cells(tmp_path, out, tasks, arms, [], 15, {"probe_every_s": 5, "server_history": ""}, {},
                                 probe=lambda m, e: next(probes), sleep=slept.append, lock=Lock)
    assert order == ["1", "3"] and slept == [5] and locks == ["in", "out", "in", "out"]
    assert [r["outcome"] for r in added] == ["done", "done"] and added[0]["rerun_of"]["outcome"] == "server"
    assert added[0]["rerun_of"]["server_reason"] == ["x"] and added[0]["rerun_of"]["attempts"] == 2
    assert (out / "cells" / "1-r1-a.server1" / "marker").read_text() == "old attempt"
    rows = [json.loads(x) for x in (out / "results.jsonl").read_text().splitlines()]
    assert serverhealth.queue(rows) == [], "the new rows replace the server rows"
    assert benchrun.rerun_cells(tmp_path, out, tasks, arms, [], 15, {}, {}, probe=lambda m, e: {"ok": True}) == []


def test_benchrun_recheck_stops_never_marks_a_measured_run_or_a_finished_rerun(tmp_path):
    from ttp import benchrun
    out = _server_out(tmp_path, [
        {"task": "1", "rep": 0, "arm": "a", "outcome": "timeout", "passed": False, "hang_guard": True,
         "server": {"calls": 9, "wait_s": 0.4}},
        {"task": "2", "rep": 0, "arm": "a", "outcome": "flagged", "passed": False,
         "rerun_of": {"outcome": "server", "attempts": 2}}])
    assert benchrun.recheck_stops(out, [("1", 0, "a"), ("2", 0, "a")], "recorded before accounting") == []
    assert len((out / "results.jsonl").read_text().splitlines()) == 2


def test_benchrun_rerun_cells_keep_an_earlier_attempt_folder_under_the_next_free_name(tmp_path, monkeypatch):
    from ttp import benchrun
    out = _server_out(tmp_path, [{"task": "1", "rep": 0, "arm": "a", "outcome": "server", "passed": None}])
    for name, text in (("1-r0-a", "attempt 3"), ("1-r0-a.server1", "attempt 1"), ("1-r0-a.server2", "attempt 2")):
        (out / "cells" / name).mkdir(parents=True)
        (out / "cells" / name / "marker").write_text(text)
    monkeypatch.setattr(benchrun, "run_cell", lambda *a, **k: {"task": "1", "rep": 0, "arm": "a", "outcome": "done"})
    added = benchrun.rerun_cells(tmp_path, out, {"1": {"task": "1"}}, {"a": {"provider": "chat", "model": "m",
                                 "effort": "max"}}, [], 0, {"server_history": ""}, {}, probe=lambda m, e: {"ok": True})
    assert [r["outcome"] for r in added] == ["done"]
    assert [(out / "cells" / f"1-r0-a.server{i}" / "marker").read_text() for i in (1, 2, 3)] == \
        ["attempt 1", "attempt 2", "attempt 3"]


def test_serverhealth_a_recheck_mark_on_a_measured_run_is_void():
    from ttp import serverhealth
    rerun = {"task": "1", "rep": 0, "arm": "a", "outcome": "done", "server": {"calls": 9}, "rerun_of": {"attempts": 2}}
    stale = {**rerun, "outcome": "server", "passed": None, "server_reason": ["before"], "was": {"outcome": "done"}}
    mark = {"task": "2", "rep": 0, "arm": "a", "outcome": "server", "was": {"outcome": "timeout"}}
    judged = {"task": "3", "rep": 0, "arm": "a", "outcome": "server", "server": {"calls": 9}}
    assert [serverhealth.void(r) for r in (stale, mark, judged)] == [True, False, False]
    assert serverhealth.latest([rerun, stale, mark, judged])[("1", 0, "a")] is rerun
    assert [r["task"] for r in serverhealth.queue([rerun, stale, mark, judged])] == ["2", "3"]


def test_benchrun_rerun_cells_leave_a_cell_queued_while_the_server_stays_slow_and_filter_by_only(tmp_path, monkeypatch):
    from ttp import benchrun
    out = _server_out(tmp_path, [
        {"task": "1", "rep": 1, "arm": "a", "outcome": "server", "passed": None},
        {"task": "2", "rep": 1, "arm": "b", "outcome": "server", "passed": None}])
    ran = []
    monkeypatch.setattr(benchrun, "run_cell", lambda *a, **k: ran.append(a) or {})
    arms = {n: {"provider": "chat", "model": "m", "effort": "max"} for n in "ab"}
    added = benchrun.rerun_cells(tmp_path, out, {"1": {"task": "1"}, "2": {"task": "2"}}, arms, [], 15,
                                 {"probe_every_s": 1, "probe_max_s": 1}, {}, probe=lambda m, e: {"ok": False},
                                 sleep=lambda s: None, only=[(None, "b")])
    assert ran == [] and [r["task"] for r in added] == ["2"]
    assert added[0]["outcome"] == "server" and added[0]["rerun_waited"] is True and added[0]["probe"] == {"ok": False}


def test_benchrun_chat_probe_never_raises_and_says_so_when_it_cannot_tell(monkeypatch):
    from ttp import benchrun, chatbridge
    monkeypatch.setattr(chatbridge, "settings", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no key")))
    assert benchrun.chat_probe({}, "m") == {"ok": False, "why": "RuntimeError"}


def test_benchrun_parse_arm_takes_codex_and_a_planner_without_naming_any_host_or_key():
    from ttp import benchrun
    name, arm = benchrun.parse_arm("g=chat:some-model:max:codex@1.2.3")
    assert name == "g" and arm == {"provider": "chat", "model": "some-model", "effort": "max", "harness": "codex",
                                   "harness_version": "1.2.3"}
    name, arm = benchrun.parse_arm("p=chat:some-model:max:codex:plan=claude~planner-model~high")
    assert arm["planner"] == {"provider": "claude", "model": "planner-model", "effort": "high"}
    assert benchrun.parse_arm("c=claude:m:high:codex")[1]["harness"] == "codex"
    for bad in ("p=chat:m:max:codex:plan=claude~m", "p=chat:m:max:codex:plan=chat~m~high"):
        with pytest.raises(benchrun.BenchError):
            benchrun.parse_arm(bad)
    with pytest.raises(benchrun.BenchError):
        benchrun.parse_arm("x=claude:m:high:nope")


def test_codexproxy_config_and_script_use_loopback_the_run_token_and_no_key_or_host(tmp_path):
    from ttp import codexproxy
    cfg = codexproxy.config_yaml("some-model", "http://127.0.0.1:4000/v1")
    assert "os.environ/TTP_BRIDGE_TOKEN" in cfg and "0.0.0.0" not in cfg and "sk-" not in cfg
    assert "use_chat_completions_api: true" in cfg and "hook.proxy_handler_instance" in cfg
    files = codexproxy.codex_config("some-model", "${TTP_RUN_PROXY_PORT}", "max")
    assert "http://127.0.0.1:${TTP_RUN_PROXY_PORT}/v1" in files["config.toml"] and 'wire_api = "responses"' in files["config.toml"]
    prof = files[f"{codexproxy.PROFILE}.config.toml"]
    assert 'model_reasoning_effort = "high"' in prof, "an effort Codex does not know falls back; the bridge sets the real one"
    assert 'approval_policy = "never"' in prof and 'sandbox_mode = "danger-full-access"' in prof
    sh = codexproxy.script("/bin/codex", "some-model", "/work", "/scratch/codex-home")
    assert sh.startswith("export CODEX_HOME=/scratch/codex-home") and "exec /bin/codex exec -p bench -m some-model" in sh
    assert "--dangerously-bypass-approvals-and-sandbox" in sh and sh.rstrip().endswith("-")


def test_codexproxy_hook_normalizes_effort_drops_client_metadata_and_skips_empty_events():
    from ttp import codexproxy
    ns: dict = {}
    import types, sys
    for mod in ("litellm", "litellm.integrations", "litellm.integrations.custom_logger"):
        sys.modules.setdefault(mod, types.ModuleType(mod))
    sys.modules["litellm.integrations.custom_logger"].CustomLogger = object   # the hook only subclasses it
    exec(codexproxy.HOOK_SOURCE, ns)
    norm, has = ns["normalize"], ns["has_data"]
    assert norm({"reasoning": {"effort": "high"}, "client_metadata": {"a": 1}, "x": 1}) == {"reasoning_effort": "high", "x": 1}
    assert norm({"reasoning_effort": "xhigh"}) == {"reasoning_effort": "high"}
    assert norm({"reasoning": {"effort": "bogus"}}) == {}
    assert has({"a": 1}) and not has({}) and not has(None) and not has("") and has("x")


def test_codexproxy_stop_ends_only_its_own_process_group(tmp_path):
    import subprocess, sys
    from ttp import codexproxy
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    codexproxy.stop(proc)
    assert proc.poll() is not None
    codexproxy.stop(proc)   # already gone: no error
    codexproxy.stop(None)


def test_benchdaily_day_record_and_table_show_a_server_cell_as_not_scored(tmp_path):
    from ttp import benchdaily, serverhealth
    rows = {("t1", 0, "glm"): {"task": "t1", "arm": "glm", "provider": "chat", "model": "m", "effort": "max",
                               "outcome": serverhealth.OUTCOME, "server_reason": ["hit the limit while slow"]},
            ("t2", 0, "glm"): {"task": "t2", "arm": "glm", "provider": "chat", "model": "m", "effort": "max",
                               "outcome": "done"}}
    server = benchdaily.server_cells(rows, {"t1": {"project": "proj"}})
    assert [(x["task"], x["status"], x["project"]) for x in server] == [("t1", serverhealth.NOT_SCORED, "proj")]
    root = tmp_path / "daily"
    (root / "days").mkdir(parents=True)
    (root / "days" / "2026-11-15.json").write_text(json.dumps({"day": "2026-11-15", "cells": [], "server": server}))
    cum = benchdaily.cumulative(root)
    assert cum["server"][0]["day"] == "2026-11-15" and cum["server"][0]["task"] == "t1"
    md = benchdaily.table_md(cum)
    assert "Not scored (server)" in md and "t1 glm" in md and "hit the limit while slow" in md
    assert "fail" in md.lower()


def _rerun_day_fixture(tmp_path, monkeypatch, new_row):
    """A day whose GLM cell stopped at the limit before the server's waits were measured; the rerun adds `new_row`."""
    from ttp import benchdaily, benchrun
    root = tmp_path / "daily"
    out = root / "runs" / "2026-11-15"
    (out / "bench").mkdir(parents=True)
    (root / "days").mkdir(parents=True)
    (out / "picked.json").write_text(json.dumps({"day": "2026-11-15", "order": [], "dropped": []}))
    stop = {"task": "P-1", "rep": 0, "arm": "glm", "outcome": "timeout", "hang_guard": True, "wall_s": 3600, "cost_usd": 0}
    (out / "results.jsonl").write_text(json.dumps(stop) + "\n")
    old_cell = {"project": "P", "task": "P-1", "source_task": 1, "arm": "glm", "model": "chat:m:max", "claude": False,
                "criteria": 0, "correctness": 0, "tests": 0, "scope": 0, "landed": False, "cost_usd": 0, "minutes": 60,
                "stopped": True, "outcome": "timeout"}
    (root / "days" / "2026-11-15.json").write_text(json.dumps({
        "day": "2026-11-15", "cells": [old_cell], "skipped": [], "dropped": [], "spend": {},
        "arms": {"glm": {"provider": "chat", "model": "m", "effort": "max", "harness": ""}}}))
    monkeypatch.setattr(benchdaily, "build_tasks", lambda *a, **k: {"P-1": {"project": "P", "source_task": 1,
                                                                            "source_root": str(tmp_path), "state": str(tmp_path)}})
    seen = {}

    def fake_rerun(bench_dir, out_, tasks, arms, cells, seed, opts, cfg, probe, sleep, lock, only, task_opts=None):
        rows = [json.loads(x) for x in (out_ / "results.jsonl").read_text().splitlines()]
        seen["marked"] = rows[-1]
        seen["task_opts"] = task_opts(tasks["P-1"])
        row = {**new_row, "task": "P-1", "rep": 0, "arm": "glm", "rerun_of": {"was": rows[-1].get("was")}}
        with open(out_ / "results.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
        return [row]
    monkeypatch.setattr(benchrun, "rerun_cells", fake_rerun)
    monkeypatch.setattr(benchdaily, "blind_review", lambda *a, **k: {
        "scored": {"i1": ("P-1", "glm", {"scores": {"criteria": 4, "correctness": 4}})}, "unscored": []})
    return root, seen


def test_rerun_day_marks_the_stop_reruns_it_and_names_the_server_as_the_cause(tmp_path, monkeypatch):
    from ttp import benchdaily
    root, seen = _rerun_day_fixture(tmp_path, monkeypatch, {"outcome": "done", "wall_s": 600, "cost_usd": 0})
    rec = benchdaily.rerun_day("2026-11-15", [("1", "glm")], root=root, progress=lambda s: None)
    assert seen["marked"]["outcome"] == "server" and seen["marked"]["was"]["outcome"] == "timeout"
    assert set(seen["task_opts"]) == {"repo", "state"}
    assert [(c["task"], c["landed"], c["stopped"]) for c in rec["cells"]] == [("P-1", True, False)]
    assert rec["reruns"][0]["cause"] == "server" and rec["reruns"][0]["was"]["outcome"] == "timeout"
    assert "server" not in rec
    md = (root / "table.md").read_text()
    assert "Server reruns" in md and "P-1 glm: done on rerun (first run timeout; the server stopped the first run)" in md
    assert json.loads((root / "days" / "2026-11-15.json").read_text())["reruns"] == rec["reruns"]


def test_rerun_day_names_the_model_when_the_rerun_stops_again_at_normal_speed(tmp_path, monkeypatch):
    from ttp import benchdaily
    root, _ = _rerun_day_fixture(tmp_path, monkeypatch, {"outcome": "timeout", "hang_guard": True, "wall_s": 3600})
    rec = benchdaily.rerun_day("2026-11-15", [("1", "glm")], root=root, progress=lambda s: None)
    assert rec["reruns"][0]["cause"] == "model"


def test_rerun_day_a_cell_still_slow_stays_not_scored_and_names_no_cause_yet(tmp_path, monkeypatch):
    from ttp import benchdaily
    root, _ = _rerun_day_fixture(tmp_path, monkeypatch, {"outcome": "server", "server_reason": ["still slow"]})
    rec = benchdaily.rerun_day("2026-11-15", [("1", "glm")], root=root, progress=lambda s: None)
    assert rec["cells"][0]["stopped"] is True, "the old stop's cell stays out of the score until it is rerun"
    assert rec["server"][0]["task"] == "P-1" and rec["reruns"][0]["cause"] == "server (still slow)"
    with pytest.raises(ValueError):
        benchdaily.rerun_day("2026-11-15", [("9", "glm")], root=root, progress=lambda s: None)


def test_rerun_day_again_on_cells_whose_reruns_ended_keeps_them_and_lists_their_outcome(tmp_path, monkeypatch):
    """A second --rerun-stops on cells already rerun (a stale `server` mark from an earlier call is in the file and
    their first attempt's folder is already kept as .server1): nothing is marked, rerun or graded again, nothing
    raises, and the table names each rerun's outcome, not the stale mark."""
    from ttp import benchdaily, benchrun
    root = tmp_path / "daily"
    out = root / "runs" / "2026-11-15"
    (out / "bench").mkdir(parents=True)
    (root / "days").mkdir(parents=True)
    (out / "picked.json").write_text(json.dumps({"day": "2026-11-15", "order": [], "dropped": []}))
    stop = {"task": "P-1", "rep": 0, "arm": "glm", "outcome": "flagged", "passed": False, "wall_s": 3600}
    mark = {**stop, "outcome": "server", "passed": None, "server_reason": [benchdaily.RERUN_WHY],
            "was": {"outcome": "flagged"}}
    rerun = {**stop, "wall_s": 1994, "server": {"calls": 40, "wait_s": 0.5}, "server_normal": True,
             "rerun_of": {"outcome": "server", "was": {"outcome": "flagged"}, "attempts": 2}}
    stale = {**rerun, "outcome": "server", "passed": None, "server_reason": [benchdaily.RERUN_WHY],
             "was": {"outcome": "flagged"}}
    (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (stop, mark, rerun, stale)))
    for name in ("P-1-r0-glm", "P-1-r0-glm.server1"):
        (out / "cells" / name).mkdir(parents=True)
        (out / "cells" / name / "marker").write_text(name)
    reruns = [{"task": "P-1", "arm": "glm", "was": {"outcome": "flagged"}, "cause": "server",
               "rerun_outcome": "flagged", "rerun_minutes": 33.23}]
    (root / "days" / "2026-11-15.json").write_text(json.dumps({
        "day": "2026-11-15", "cells": [], "skipped": [], "dropped": [], "spend": {}, "reruns": reruns,
        "arms": {"glm": {"provider": "chat", "model": "m", "effort": "max", "harness": ""}}}))
    monkeypatch.setattr(benchdaily, "build_tasks", lambda *a, **k: {"P-1": {"project": "P", "source_task": 1,
                                                                            "source_root": str(tmp_path), "state": str(tmp_path)}})
    monkeypatch.setattr(benchrun, "run_cell", lambda *a, **k: pytest.fail("a finished rerun ran again"))
    monkeypatch.setattr(benchdaily, "blind_review", lambda *a, **k: pytest.fail("a graded rerun was graded again"))
    before = (out / "results.jsonl").read_text()
    rec = benchdaily.rerun_day("2026-11-15", [("1", "glm")], root=root, probe=lambda m, e: {"ok": True},
                               opts={"server_history": ""}, progress=lambda s: None)
    assert (out / "results.jsonl").read_text() == before, "no row appended"
    assert rec["reruns"] == reruns and "server" not in rec
    assert benchdaily.read_rows(out / "results.jsonl")[("P-1", 0, "glm")] == rerun, "the stale mark does not count"
    md = (root / "table.md").read_text()
    assert "P-1 glm: flagged on rerun (first run flagged; the server stopped the first run)" in md
    assert ": server\n" not in md


class _FakeUsage:
    def __init__(self, text, cost=0.31, error=""):
        self.final_text, self.cost_usd, self.error = text, cost, error
        self.input_tokens, self.output_tokens, self.cache_read_tokens, self.cache_write_tokens = 100, 50, 10, 5


def _plan_step_fixture(tmp_path, monkeypatch, text, hung=False):
    from ttp import benchrun, cellenv
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(cellenv, "make", lambda *a, **k: tmp_path / "venv")
    monkeypatch.setattr(cellenv, "env", lambda *a, **k: {})
    seen = {}

    class Prov:
        def parse(self, out, err):
            return _FakeUsage(text)

    def fake_launch(run, pws, prompt, planner, opts, cfg, hidden, cell_env, conf, own, cell, read_only=False):
        run.mkdir()
        (run / "output.jsonl").write_text("{}\n")
        seen.update(read_only=read_only, prompt=prompt, planner=planner, cwd=pws)
        return Prov(), 42.0, hung
    monkeypatch.setattr(benchrun, "launch", fake_launch)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    arm = {"planner": {"provider": "claude", "model": "m", "effort": "high"}}
    task = {"spec": "do the thing", "head": "h", "repo": None}
    row, keep = {}, tmp_path / "keep"
    return benchrun, ws, tmp, arm, task, row, keep, seen


def test_plan_step_the_planner_reads_a_copy_read_only_and_its_last_message_is_the_plan(tmp_path, monkeypatch):
    benchrun, ws, tmp, arm, task, row, keep, seen = _plan_step_fixture(tmp_path, monkeypatch, "1. edit a.py\n2. run tests")
    plan, why = benchrun.plan_step(tmp, ws, task, arm, {"isolate": False}, {}, [], row, keep)
    assert (plan, why) == ("1. edit a.py\n2. run tests", "")
    assert seen["read_only"] is True and seen["cwd"] != ws, "the planner works on its own copy of the code"
    assert "do the thing" in seen["prompt"] and seen["planner"]["model"] == "m"
    assert row["planner_wall_s"] == 42.0 and row["planner_cost_usd"] == 0.31 and row["plan_chars"] == len(plan)
    assert row["planner_tokens"]["output"] == 50
    assert (keep / "plan.md").read_text().strip() == plan and (keep / "planner-output.jsonl").exists()


def test_plan_step_an_empty_answer_or_a_hung_planner_is_an_error_not_a_plan(tmp_path, monkeypatch):
    benchrun, ws, tmp, arm, task, row, keep, _ = _plan_step_fixture(tmp_path, monkeypatch, "  ")
    plan, why = benchrun.plan_step(tmp, ws, task, arm, {"isolate": False}, {}, [], row, keep)
    assert plan == "" and "no plan" in why
    benchrun, ws, tmp, arm, task, row, keep, _ = _plan_step_fixture(tmp_path / "b", monkeypatch, "a plan", hung=True)
    plan, why = benchrun.plan_step(tmp, ws, task, arm, {"isolate": False}, {}, [], row, keep)
    assert plan == "" and "hung" in why


def test_claudebridge_a_streaming_request_gets_the_answer_as_chat_completion_chunks():
    from ttp import claudebridge
    answer = {"id": "c1", "object": "chat.completion", "created": 1, "model": "m",
              "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok", "tool_calls": [
                  {"id": "call_1", "type": "function", "function": {"name": "sh", "arguments": "{\"command\": \"ls\"}"}}]},
                           "finish_reason": "tool_calls"}],
              "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
    chunks = claudebridge.stream_chunks(answer)
    deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
    assert deltas[0]["role"] == "assistant" and {"content": "ok"} in deltas
    assert deltas[2]["tool_calls"][0]["function"]["arguments"] == "{\"command\": \"ls\"}"
    assert chunks[-2]["choices"][0]["finish_reason"] == "tool_calls" and chunks[-1]["usage"]["total_tokens"] == 8

    class H:
        def __init__(self):
            self.sent, self.wfile = [], io.BytesIO()
        def send_response(self, c): self.sent.append(c)
        def send_header(self, k, v): self.sent.append((k, v))
        def end_headers(self): pass
    h = H()
    claudebridge.reply_stream(h, answer)
    wire = h.wfile.getvalue()
    assert h.sent[0] == 200 and ("Content-Type", "text/event-stream") in h.sent
    assert b"data: [DONE]" in wire and wire.endswith(b"0\r\n\r\n")


def test_claudebridge_parse_reply_reads_the_xml_parameter_form_of_a_tool_with_several_parameters():
    from ttp import claudebridge
    tools = [{"type": "function", "function": {"name": "run", "parameters": {"type": "object", "properties": {
        "cmd": {"type": "string"}, "wait_ms": {"type": "integer"}, "tty": {"type": "boolean"}}}}}]
    reply = 'first\n<tool_call name="run">\n<parameter name="cmd">ls -la</parameter>\n<parameter name="wait_ms">500</parameter>\n<parameter name="tty">true</parameter>\n</tool_call>'
    text, calls = claudebridge.parse_reply(reply, tools)
    assert text == "first" and len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"cmd": "ls -la", "wait_ms": 500, "tty": True}
    text, calls = claudebridge.parse_reply('<tool_call name="run">not json, no tags</tool_call>', tools)
    assert calls == [] and "not json" in text


def test_claudebridge_parse_reply_takes_both_call_forms_of_one_reply_and_leaves_no_markup():
    from ttp import claudebridge
    tools = [{"type": "function", "function": {"name": "run", "parameters": {"type": "object", "properties": {
        "cmd": {"type": "string"}, "dir": {"type": "string"}}}}}]
    reply = ('Reading.\n<tool_call name="run">{"cmd": "ls"}</tool_call>\n'
             '<invoke name="run"><parameter name="cmd">pwd</parameter><parameter name="dir">/x</parameter></invoke>')
    text, calls = claudebridge.parse_reply(reply, tools)
    assert text == "Reading." and [json.loads(c["function"]["arguments"]) for c in calls] == [
        {"cmd": "ls"}, {"cmd": "pwd", "dir": "/x"}]


def test_claudebridge_parse_reply_tolerates_a_mismatched_closing_tag():
    from ttp import claudebridge
    tools = [{"type": "function", "function": {"name": "run", "parameters": {"type": "object", "properties": {
        "cmd": {"type": "string"}, "dir": {"type": "string"}}}}}]
    text, calls = claudebridge.parse_reply('<tool_call name="run">\n<parameter name="cmd">ls</parameter>\n</invoke>', tools)
    assert text == "" and json.loads(calls[0]["function"]["arguments"]) == {"cmd": "ls"}


def test_serverhealth_failed_calls_split_by_kind_and_an_old_fault_counts_as_other():
    from ttp import serverhealth
    rows = [{"st": s, "out": 0, "tps": 0.0, "wait_s": 1.0, "dur_s": 1.0} for s in ("ok", "stream", "parse", "capacity", "capacity", "fault")]
    s = serverhealth.summarize(rows)
    assert s["failed_calls"] == 5
    assert s["failed_by"] == {"stream": 1, "parse": 1, "capacity": 2, "other": 1}
    assert "failed_by" not in serverhealth.summarize(rows[:1]), "no failed call: no split"


def _cap_refused_row(pct, outcome="pass", **kw):
    """A cell row whose 100 model calls had `pct` capacity refusals (429/503)."""
    return {"task": "t", "rep": 1, "arm": "a", "model": "m", "outcome": outcome, "passed": outcome == "pass",
            "server": {"calls": 100, "failed_calls": pct, "failed_by": {"capacity": pct}}, **kw}


def test_serverhealth_a_cell_over_the_capacity_refusal_threshold_is_not_run_server(tmp_path):
    """A cell with over 20% capacity refusals is 'not run (server)', not scored and not flagged;
    the threshold is the config key bench.server.capacity_refusal_max (default 20%)."""
    from ttp import benchdaily, benchrun, cli, serverhealth
    hist = serverhealth.History(tmp_path / "h.json")
    bridge = lambda row: {"server": row.pop("server")}
    for pct, outcome in ((21, "pass"), (74, "flagged"), (25, "fail")):
        row = _cap_refused_row(pct, outcome, flagged=["runs 'git log --all'"] if outcome == "flagged" else [])
        why = serverhealth.judge(row, None, bridge(row), hist, scored=outcome != "pass")
        assert why == [f"{pct}% capacity refusals ({pct} of 100 calls; over 20%)"], why
        assert row["outcome"] == serverhealth.OUTCOME and row["passed"] is None and not row.get("flagged"), row
        assert serverhealth.describe(row) == serverhealth.NOT_SCORED and row["scored_as"] == outcome
        assert benchrun.hard_flags(row.get("flagged")) == [] and row["capacity_refusal_max"] == 0.2
        if outcome == "flagged":
            assert row["unscored_flags"] == ["runs 'git log --all'"], "kept for the record, never counted"
    for pct, outcome in ((19, "pass"), (20, "fail"), (3, "flagged")):
        row = _cap_refused_row(pct, outcome, flagged=["runs 'gh pr'"] if outcome == "flagged" else [])
        assert serverhealth.judge(row, None, bridge(row), hist, scored=False) == []
        assert row["outcome"] == outcome and "capacity_refusals" not in row, "at or under the threshold: scored"
    # A cell no model ran stays as it is; a cell with no calls has no share.
    row = _cap_refused_row(50, "not_run")
    assert serverhealth.judge(row, None, bridge(row), hist) == [] and row["outcome"] == "not_run"
    assert serverhealth.over_refused({"outcome": "pass", "server": {"calls": 0, "failed_by": {"capacity": 0}}}) is False
    # The config key sets the threshold, recorded on the row so a later table judges it the same way.
    opts = cli._server_opts(type("P", (), {"state": tmp_path})(), {"server": {"capacity_refusal_max": 0.5}})
    assert opts["capacity_refusal_max"] == 0.5
    row = _cap_refused_row(30)
    assert serverhealth.judge(row, None, bridge(row), hist, refusal_max=0.5) == [] and row["capacity_refusal_max"] == 0.5
    for bad in (2, -1, "x", True):
        assert serverhealth.refusal_limit({}, bad) == serverhealth.REFUSAL_MAX
    # Rows written before the rule are re-judged when read (a marked copy; the file is unchanged), out of every
    # denominator: the day's table takes only the cells left.
    old, ok = _cap_refused_row(21, "pass", rep=1), _cap_refused_row(19, "pass", rep=2)
    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps(old) + "\n" + json.dumps(ok) + "\n")
    rows = benchdaily.read_rows(path)
    assert [rows[("t", r, "a")]["outcome"] for r in (1, 2)] == [serverhealth.OUTCOME, "pass"]
    assert "capacity_refusals" not in json.loads(path.read_text().splitlines()[0]), "the file is never rewritten"
    assert serverhealth.queue(list(rows.values())) == [rows[("t", 1, "a")]], "queued for a rerun, not scored"
    from ttp import modelsuite
    assert modelsuite._rate(list(rows.values())) == 1.0, "only the 19% cell is in the denominator"
    assert serverhealth.latest([old], limit=0.25)[("t", 1, "a")]["outcome"] == "pass", "the configured limit applies"
