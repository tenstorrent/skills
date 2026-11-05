# Staged-wait tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new tests of thenwaits.py (a waiting hand-off's `then` stages) here.
from __future__ import annotations


def _then_task(p, tmp_path, then, probe="true", tier="standard", **extra):
    """A task asleep on a `waiting` hand-off on `probe` with `then` stages, after a worker run."""
    now = time.time()
    tid = p.db.add_task("run the long job", "s", kind="work", tier=tier, origin="user")
    run_dir = tmp_path / f"then-run-{tid}"
    run_dir.mkdir()
    p.db.x("INSERT INTO runs(task,role,provider,started,ended,status,dir) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", now - 120, now - 60, "ok", str(run_dir)))
    hand = {"status": "waiting", "summary": "job running", "waiting_for": "the job", "retry_after_s": 900,
            "retry_when": probe, "then": then, **extra}
    (run_dir / "result.json").write_text(json.dumps(hand))
    p.db.update_task(tid, status="queued", not_before=now + 3600,
                     result=json.dumps({**hand, "waiting_since": now - 60}))
    return tid, run_dir


def _then_daemon(p, monkeypatch):
    from ttp import daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    return dmod.Daemon(p.base)


WRITES_DONE = "printf '{\"status\":\"done\",\"summary\":\"verdict: pass\"}' > \"$TTP_RESULT\""


def test_a_three_stage_chain_moves_through_its_stages_and_a_restart_keeps_the_stage(env, monkeypatch, tmp_path):
    """Each passed stage (an on_pass_cmd exiting 0 without a hand-off, or a bare probe) makes the next
    one the wait with no model run; a new daemon (a restart, as after a reboot) keeps the stage, and the
    last stage's on_pass_cmd records done."""
    p = make(env)
    d = _then_daemon(p, monkeypatch)
    ran, gate = tmp_path / "ran", tmp_path / "gate"
    tid, _ = _then_task(p, tmp_path, [{"retry_when": f"test -e {gate}"},
                                      {"retry_when": "test -e /", "on_pass_cmd": WRITES_DONE, "retry_after_s": 600}],
                        on_pass_cmd=f"echo first >> {ran}")
    _settle_on_pass(d, tid)
    res = json.loads(p.db.task(tid)["result"])
    assert ran.read_text() == "first\n" and p.db.task(tid)["status"] == "queued" and not _ready(p, tid)
    assert res["then_stage"] == 1 and res["retry_when"] == f"test -e {gate}" and "on_pass_cmd" not in res, res
    assert res["then"] == [{"retry_when": "test -e /", "on_pass_cmd": WRITES_DONE, "retry_after_s": 600}]
    assert "woke" not in res and res["retry_when_orig"] == "true" and res["on_pass_cmd_orig"] == f"echo first >> {ran}"
    d = _then_daemon(p, monkeypatch)   # a restart: the stage is the task's, not the daemon's
    _settle_probe(d, tid)
    assert json.loads(p.db.task(tid)["result"])["then_stage"] == 1 and not _ready(p, tid)
    gate.write_text("")
    _settle_probe(d, tid)
    res = json.loads(p.db.task(tid)["result"])
    assert res["then_stage"] == 2 and res["retry_when"] == "test -e /" and res["on_pass_cmd"] == WRITES_DONE, res
    assert "then" not in res and res["retry_after_s"] == 600 and not _ready(p, tid)
    assert "stage 3 of 3" in p.db.task(tid)["blocked_reason"]
    d = _then_daemon(p, monkeypatch)
    _settle_on_pass(d, tid)
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert task["status"] == "done", (task["status"], task["blocked_reason"], res)
    assert res["summary"] == "verdict: pass" and res["run_status"] == "model-free"
    assert ran.read_text() == "first\n", "an earlier stage's on_pass_cmd ran again"
    assert len(p.db.q("SELECT id FROM runs WHERE task=?", (tid,))) == 1, "a model run was started"
    assert "moved to its next `then` stage 2 model-free" in (p.logs / "daemon.log").read_text()


@pytest.mark.parametrize("stage", [0, 1])
def test_a_failing_stage_wakes_the_task_at_its_own_tier_with_the_output_tail(env, monkeypatch, tmp_path, stage):
    p = make(env)
    d = _then_daemon(p, monkeypatch)
    fail = "echo boom-the-verdict; exit 3"
    then = [{"retry_when": "true", "on_pass_cmd": fail if stage else "true"}, {"retry_when": "true"}]
    tid, _ = _then_task(p, tmp_path, then, tier="deep", on_pass_cmd=fail if not stage else None)
    for _ in range(stage + 1):
        _settle_on_pass(d, tid)
    res = json.loads(p.db.task(tid)["result"])
    assert _ready(p, tid) and p.db.task(tid)["status"] == "queued", res
    assert "exit 3" in res["woke"] and "boom-the-verdict" in res["woke"], res
    assert res["wake_tier"] == "deep" and res.get("escalated_wake"), res
    assert res.get("then_stage", 0) == stage
    assert len(p.db.q("SELECT id FROM runs WHERE task=?", (tid,))) == 1


def test_the_last_stage_records_on_pass_or_wakes_without_one(env, monkeypatch, tmp_path):
    p = make(env)
    d = _then_daemon(p, monkeypatch)
    gate = tmp_path / "gate"
    with_on_pass, _ = _then_task(p, tmp_path, [{"retry_when": f"test -e {gate}"}],
                                 on_pass={"status": "done", "summary": "landed and verified"})
    without, _ = _then_task(p, tmp_path, [{"retry_when": f"test -e {gate}"}])
    for tid in (with_on_pass, without):
        _settle_probe(d, tid)
        assert json.loads(p.db.task(tid)["result"])["then_stage"] == 1 and not _ready(p, tid)
    gate.write_text("")
    for tid in (with_on_pass, without):
        _settle_probe(d, tid)
    task = p.db.task(with_on_pass)
    assert task["status"] == "done" and json.loads(task["result"])["summary"] == "landed and verified"
    assert json.loads(task["result"])["run_status"] == "model-free"
    assert _ready(p, without) and "woke" in json.loads(p.db.task(without)["result"])
    assert len(p.db.q("SELECT id FROM runs")) == 2


def test_passed_checks_move_to_the_next_stage_and_failed_ones_wake_at_the_task_tier(env, monkeypatch, tmp_path):
    """Checks, then landed: a checks pass on the head still checked out moves on (its on_pass waits for
    the last stage); failed checks wake the task at its own tier, as without stages."""
    p = make(env)
    d = _then_daemon(p, monkeypatch)
    tid, _ = _checks_wait_task(p, tmp_path, DONE_ON_PASS)
    fid, _ = _checks_wait_task(p, tmp_path, DONE_ON_PASS, rc="1", tier="deep")
    gate = tmp_path / "landed"
    then = [{"retry_when": f"test -e {gate}"}]
    for t in (tid, fid):
        res = json.loads(p.db.task(t)["result"])
        p.db.update_task(t, result=json.dumps({**res, "then": then}))
        run_dir = Path(p.db.one("SELECT dir FROM runs WHERE task=?", (t,))["dir"])
        full = json.loads((run_dir / "result.json").read_text())
        (run_dir / "result.json").write_text(json.dumps({**full, "then": then}))
    _settle_probe(d, tid)
    res = json.loads(p.db.task(tid)["result"])
    assert p.db.task(tid)["status"] == "queued" and res["then_stage"] == 1 and res["retry_when"] == then[0]["retry_when"]
    assert not _ready(p, tid), res
    gate.write_text("")
    _settle_probe(d, tid)
    task = p.db.task(tid)
    assert task["status"] == "done" and json.loads(task["result"])["summary"] == DONE_ON_PASS["summary"]
    _settle_probe(d, fid)
    res = json.loads(p.db.task(fid)["result"])
    assert _ready(p, fid) and "checks failed" in res["woke"] and res["wake_tier"] == "deep", res
    assert "then_stage" not in res


def test_thenwaits_problem_refuses_each_malformed_list():
    from ttp import hook, thenwaits
    base = {"status": "waiting", "summary": "s", "retry_when": "true"}
    assert thenwaits.problem(base) is None
    assert thenwaits.problem({**base, "then": [{"retry_when": "landed:#3", "on_pass_cmd": "true",
                                                "retry_after_s": 600}]}) is None
    bad = {
        "a dict": {"retry_when": "true"}, "empty": [], "too long": [{"retry_when": "true"}] * 5,
        "a string stage": ["true"], "an unknown key": [{"retry_when": "true", "next_step": "x"}],
        "no probe": [{"on_pass_cmd": "true"}], "a number probe": [{"retry_when": 5}],
        "a bad landed": [{"retry_when": "landed: task 3"}], "prose": [{"retry_when": "the user's next message"}],
        "an empty cmd": [{"retry_when": "true", "on_pass_cmd": " "}],
        "a bool timer": [{"retry_when": "true", "retry_after_s": True}],
        "a negative timer": [{"retry_when": "true", "retry_after_s": -5}],
    }
    for name, then in bad.items():
        assert thenwaits.problem({**base, "then": then}), name
    assert thenwaits.problem({"status": "done", "summary": "s", "then": [{"retry_when": "true"}]})
    assert thenwaits.problem({"status": "waiting", "summary": "s", "then": [{"retry_when": "true"}]})


def test_the_stop_hook_refuses_a_malformed_then(tmp_path):
    from ttp import hook
    f = tmp_path / "result.json"
    f.write_text(json.dumps({"status": "waiting", "summary": "s", "retry_when": "true", "then": [{"retry_when": 5}]}))
    assert "stage 1 needs a retry_when" in hook.handoff_problem(f)
    f.write_text(json.dumps({"status": "waiting", "summary": "s", "retry_when": "true", "then": [{"retry_when": "true"}]}))
    assert hook.handoff_problem(f) is None


def test_a_malformed_then_is_refused_at_hand_off_and_a_valid_one_gets_its_run_variables(env, monkeypatch):
    p = make(env)
    d = _then_daemon(p, monkeypatch)
    for then in ([{"retry_when": "true", "bogus": 1}], [{"retry_when": "bash $TTP_RUN_DIR/verdict.sh"}],
                 [{"retry_when": "test -e ${TTP_RESULT}"}]):
        monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({
            "status": "waiting", "summary": "job running", "waiting_for": "the job", "retry_after_s": 900,
            "retry_when": "exit 1", "then": then}))
        tid = p.db.add_task(f"waits then {then[0]['retry_when']}", "s", kind="work", tier="light", origin="user")
        assert _run_until(d, p, lambda: p.db.one("SELECT id FROM events WHERE kind='task_waiting' AND task=?",
                                                 (tid,)))
        res = json.loads(p.db.task(tid)["result"])
        refused = p.db.q("SELECT text FROM events WHERE kind='handoff_probe_refused' AND task=?", (tid,))
        if "TTP_RUN_DIR" in then[0]["retry_when"]:
            run_dir = p.db.one("SELECT dir FROM runs WHERE task=?", (tid,))["dir"]
            assert res["then"] == [{"retry_when": f"bash {run_dir}/verdict.sh"}] and not refused, res
        else:
            assert "then" not in res and res["then_refused"] and res["retry_when"] == "exit 1", res
            assert len(refused) == 1 and "`then`" in refused[0]["text"], refused


def test_a_new_stage_starts_its_own_wait_clock(env, monkeypatch, tmp_path):
    """A stage change resets waiting_since (and drops the per-wait peer-hold keys), so a stage that has
    just begun after earlier stages took longer than max_hold_s raises no wait_stale."""
    from ttp import daemon as dmod
    p = make(env)
    every = dmod.PROBE_EVERY_S
    d = _then_daemon(p, monkeypatch)
    tid, _ = _then_task(p, tmp_path, [{"retry_when": "false"}])
    old = json.loads(p.db.task(tid)["result"])
    p.db.update_task(tid, result=json.dumps({**old, "waiting_since": time.time() - 7 * 3600,
                                             "peer_held_at": time.time() - 7 * 3600, "peer_hold_live": True}))
    _settle_probe(d, tid)
    res = json.loads(p.db.task(tid)["result"])
    assert res["then_stage"] == 1 and res["retry_when"] == "false", res
    assert abs(res["waiting_since"] - time.time()) < 30, res
    assert "peer_held_at" not in res and "peer_hold_live" not in res, res
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", every)
    _settle_probe(d, tid)
    p.db.update_task(tid, not_before=time.time() - 1)
    d.probe_waiting()          # due with a fresh "not yet": sleeps on, no stale event for a new stage
    assert not _ready(p, tid) and p.db.task(tid)["not_before"] > time.time() + 60
    assert not p.db.q("SELECT id FROM events WHERE task=? AND kind IN ('wait_stale', 'probe_never_passes')", (tid,))


def test_a_later_stage_keeps_an_urgent_tasks_capped_retry(env, monkeypatch, tmp_path):
    from ttp import daemon as dmod
    p = make(env)
    d = _then_daemon(p, monkeypatch)
    tid, _ = _then_task(p, tmp_path, [{"retry_when": "false", "retry_after_s": 3600}])
    monkeypatch.setattr(dmod.waitpoll, "capped", lambda cfg, task, result, retry_s: min(retry_s, 120))
    _settle_probe(d, tid)
    task = p.db.task(tid)
    assert json.loads(task["result"])["then_stage"] == 1
    assert task["not_before"] < time.time() + 200, task["not_before"] - time.time()
