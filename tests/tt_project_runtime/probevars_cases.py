# Probe run-variable tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new tests of probevars.py (run variables in stored probes) here.
from __future__ import annotations


def test_probevars_pin_writes_in_both_forms_and_refuses_an_unset_one():
    from ttp.probevars import pin
    env = {"TTP_RUN_DIR": "/state/runs/7", "TTP_TASK_ID": "42"}
    assert pin("bash $TTP_RUN_DIR/probe.sh", "retry_when", env) == "bash /state/runs/7/probe.sh"
    assert pin('test -e "${TTP_RUN_DIR}/x" && echo $TTP_TASK_ID', "retry_when", env) == \
        'test -e "/state/runs/7/x" && echo 42'
    # Left as written: another variable, a longer name, an escaped `$`, a shell default, no `$` at all.
    for probe in ("ls $TTP_PROJECT", "echo $TTP_RUN_DIRS", r"echo \$TTP_RUN_DIR", "test -e ${TTP_RUN_DIR:-x}",
                  "landed:#3", "test -e out/done"):
        assert pin(probe, "retry_when", {}) == probe
    for probe in ("bash $TTP_RESULT", "bash ${TTP_RESULT}"):
        with pytest.raises(ValueError, match=r"start_when uses \$TTP_RESULT, .*absolute path or one relative to "
                                             r"the project root"):
            pin(probe, "start_when", env)
    with pytest.raises(ValueError, match="no plain path"):   # a value the shell would split or expand
        pin("bash $TTP_RUN_DIR/p.sh", "retry_when", {"TTP_RUN_DIR": "/a b"})


@pytest.mark.parametrize("form", ["$TTP_RUN_DIR", "${TTP_RUN_DIR}"])
def test_set_when_writes_in_a_runs_variables_and_refuses_them_outside_one(env, monkeypatch, capsys, tmp_path, form):
    """A retry_when (waiting task) or start_when (queued task) naming $TTP_RUN_DIR gets the run's own folder
    when set from inside a run; outside one it is refused and the task keeps its old probe."""
    p = make(env)
    from ttp import cli
    from ttp import coordinator as coord
    waiting = _due_waiting_task(p, "exit 1")
    coord.apply(p, [{"type": "task_add", "title": "later", "spec": "s", "start_after": "2h"}])
    queued = _added(p, "later")
    for tid in (waiting, queued):
        before = (p.db.task(tid)["result"], p.db.task(tid)["labels"])
        with pytest.raises(SystemExit):
            cli.main(["task", "demo", "set-when", str(tid), f"bash {form}/probe.sh"])
        assert "uses $TTP_RUN_DIR, which only a worker run sets" in capsys.readouterr().err
        assert (p.db.task(tid)["result"], p.db.task(tid)["labels"]) == before
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path / "run"))
    cli.main(["task", "demo", "set-when", str(waiting), f"bash {form}/probe.sh"])
    cli.main(["task", "demo", "set-when", str(queued), f"bash {form}/probe.sh"])
    assert f"retry_when set: bash {tmp_path}/run/probe.sh" in capsys.readouterr().out
    assert json.loads(p.db.task(waiting)["result"])["retry_when"] == f"bash {tmp_path}/run/probe.sh"
    assert f"start_when:bash {tmp_path}/run/probe.sh" in _labels(p, queued)


def test_a_coordinator_start_when_naming_a_run_variable_is_refused(env):
    """The coordinator's actions are applied in the daemon, where no run's variables are set."""
    p = make(env)
    from ttp import coordinator as coord
    problems = coord.apply(p, [{"type": "task_add", "title": "later", "spec": "s",
                                "start_when": "test -e ${TTP_RUN_DIR}/done"}])
    assert problems and "start_when uses $TTP_RUN_DIR" in problems[0], problems
    assert not p.db.q("SELECT id FROM tasks WHERE title='later'")


def test_a_hand_offs_retry_when_gets_its_runs_variables_and_an_unset_one_is_refused(env, monkeypatch):
    """A worker's `bash $TTP_RUN_DIR/probe.sh` is stored with that run's folder (its own text kept as
    retry_when_orig); `$TTP_RESULT`, which no worker run has, is refused and not stored."""
    p = make(env)
    from ttp import daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    for probe in ("bash $TTP_RUN_DIR/probe.sh", "test -e ${TTP_RESULT}"):
        monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({
            "status": "waiting", "summary": "job running", "waiting_for": "the job", "retry_after_s": 900,
            "retry_when": probe}))
        tid = p.db.add_task(f"waits on {probe}", "s", kind="work", tier="light", origin="user")
        assert _run_until(d, p, lambda: p.db.one("SELECT id FROM events WHERE kind='task_waiting' AND task=?",
                                                 (tid,)))
        res = json.loads(p.db.task(tid)["result"])
        refused = _events(p, tid, "handoff_probe_refused")
        if "RESULT" in probe:
            assert "retry_when" not in res and res["retry_when_refused"] == probe, res
            assert len(refused) == 1 and "uses $TTP_RESULT" in refused[0]["text"], refused
        else:
            run_dir = p.db.one("SELECT dir FROM runs WHERE task=?", (tid,))["dir"]
            assert res["retry_when"] == f"bash {run_dir}/probe.sh" and res["retry_when_orig"] == probe, res
            assert not refused


def test_a_probe_running_a_missing_file_is_broken_never_not_yet(env, monkeypatch, tmp_path):
    """`bash <missing file>` exits 127: a waiting task wakes as a broken probe, and a start_when raises
    deferral_probe_broken; neither sleeps on it as "not yet"."""
    p = make(env)
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    from ttp.prompts import worker_task
    probe = f"bash {tmp_path}/missing/probe.sh"
    d = dmod.Daemon(p.base)
    waiting = _due_waiting_task(p, probe)
    _settle_probe(d, waiting)
    p.db.update_task(waiting, not_before=time.time() - 1)
    d.probe_waiting()
    assert _ready(p, waiting)
    assert "Woken because: probe broken: exit 127" in worker_task(p, p.db.task(waiting), str(p.root), None)
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    coord.apply(p, [{"type": "task_add", "title": "deferred", "spec": "s", "start_when": probe}])
    tid = _added(p, "deferred")
    for _ in range(3):
        d.probe_waiting()
        if tid in d._probes:
            d._probes[tid][0].wait(_load_scaled(60))
            d.probe_waiting()
    ev = _events(p, tid, "deferral_probe_broken")
    assert len(ev) == 1 and "broken (exit 127" in ev[0]["text"], ev
    assert not _ready(p, tid)


def test_wakes_handing_back_the_same_run_variable_probe_count_as_stale(env, monkeypatch):
    """Each run writes its own folder into `test -e $TTP_RUN_DIR/x`, but the wait it hands back is the
    same: the stale-wake count compares the run's own text, so the third such wake still blocks."""
    from pathlib import Path
    from ttp import daemon as dmod
    from ttp.daemon import _wait_key
    p = make(env)
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    probe = "test -e $TTP_RUN_DIR/x"
    same = {"status": "waiting", "summary": "still busy", "waiting_for": "the job", "retry_after_s": 300,
            "retry_when": probe}
    assert _wait_key({**same, "retry_when": "test -e /s/runs/1/x", "retry_when_orig": probe}) == \
        _wait_key({**same, "retry_when": "test -e /s/runs/2/x", "retry_when_orig": probe}) == _wait_key(same)
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps(same))
    tid = p.db.add_task("waits on its job", "s", kind="work", tier="standard", origin="user")
    d = dmod.Daemon(p.base)

    def runs():
        return p.db.q("SELECT status, dir FROM runs WHERE task=? AND role='worker' ORDER BY id", (tid,))

    for n in (1, 2, 3, 4):
        assert _run_until(d, p, lambda: len(runs()) == n and runs()[-1]["status"] != "running"
                          and p.db.task(tid)["status"] in ("queued", "blocked"))
        res = json.loads(p.db.task(tid)["result"])
        assert res["retry_when"] == f"test -e {runs()[-1]['dir']}/x" and res["retry_when_orig"] == probe, res
        assert res["stale_wakes"] == (0 if n in (1, 4) else n - 1), res
        if n < 4:
            Path(runs()[-1]["dir"], "x").touch()
            p.db.update_task(tid, not_before=None)
    t = p.db.task(tid)
    assert t["status"] == "blocked" and "no progress after 3 wakes" in t["blocked_reason"], t["blocked_reason"]
