# Own-run wake hold (ownrunhold.py), the review at once (reviewnow.py) and the review that no longer waits
# for its pre-checks, of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there). Add new tests of these here.
from __future__ import annotations


def _hold_checks_wait(p, tmp_path, alive=True, rc=None):
    """A waiting code task whose own `ttp checks` run (or ended with `rc`, or died); returns (tid, run dir)."""
    from ttp import cli
    from ttp.runner import proc_start
    tid, path = _checks_wait_task(p, tmp_path, rc=rc)
    run_dir = tmp_path / f"run-{tid}"
    out = json.loads((run_dir / "result.json").read_text())["retry_when"].rsplit(" ", 1)[1].strip("'")
    if alive:
        (Path(out) / cli.CHECKS_RUNNING).write_text(json.dumps({"since": time.time() - 30}))
        (Path(out) / cli.CHECKS_PID).write_text(json.dumps({"pid": os.getpid(), "started": proc_start(os.getpid()),
                                                            "ts": time.time() - 60}))
    return tid, run_dir


def test_a_spec_or_probe_wake_waits_while_the_tasks_own_checks_run(env, tmp_path):
    from ttp import ownrunhold
    from ttp.db import load_result
    p = make(env)
    tid, run_dir = _hold_checks_wait(p, tmp_path, alive=True)
    res, cfg = load_result(p.db.task(tid)["result"]), p.config()
    why = ownrunhold.hold(p, cfg, res)
    assert why and "checks still run" in why and "until it ends" in why, why
    assert ownrunhold.hold(p, cfg, res, urgent=True) is None, "an urgent update never waits"
    p.set_config("waiting.hold_own_runs", False)
    assert ownrunhold.hold(p, p.config(), res) is None, "the off-switch"
    assert ownrunhold.hold(p, cfg, None) is None and ownrunhold.hold(p, cfg, {"status": "done"}) is None


def test_a_dead_or_finished_own_run_holds_no_wake(env, tmp_path):
    from ttp import ownrunhold
    from ttp.db import load_result
    p = make(env)
    tid, _ = _hold_checks_wait(p, tmp_path, alive=False)   # no live process, no exit code: a fault, not a wait
    assert ownrunhold.hold(p, p.config(), load_result(p.db.task(tid)["result"])) is None
    tid, _ = _hold_checks_wait(p, tmp_path, alive=True, rc="0")   # ended: it settles model-free
    assert ownrunhold.hold(p, p.config(), load_result(p.db.task(tid)["result"])) is None


def test_a_plain_needs_review_hand_off_with_a_change_is_reviewed_at_once_whatever_its_kind(env):
    from ttp import reviewnow
    p = make(env)
    cfg = p.config()
    assert reviewnow.at_once(cfg, "review", True)
    assert not reviewnow.at_once(cfg, "review", False), "notes or follow-ups still go to the coordinator"
    assert not reviewnow.at_once(cfg, "done", True)
    assert not reviewnow.at_once(cfg, "review", True, {"question": "which one?"}), "a question is the coordinator's"
    p.set_config("review.at_once", False)
    assert not reviewnow.at_once(p.config(), "review", True), "the off-switch"
    _with_origin(env, clone=False)
    p.set_config("review.at_once", True)
    _, _, _, reviews = _finish_code(env, p, "plain", {"x.py": 3}, result={"status": "needs_review", "summary": "ok"})
    assert len(reviews) == 1
    # One predicate (Daemon._code_change) decides: a work task whose branch changes files is reviewed like a code task.
    _, _, _, reviews = _finish_code(env, p, "plain work", {"w.py": 3}, kind="work",
                                    result={"status": "needs_review", "summary": "ok"})
    assert len(reviews) == 1
    _, _, _, reviews = _finish_code(env, p, "no change", {}, kind="work",
                                    result={"status": "needs_review", "summary": "ok"})
    assert reviews == [], "a work task whose branch changes nothing has nothing to review"
    _, _, _, reviews = _finish_code(env, p, "with notes", {"y.py": 3}, result={
        "status": "needs_review", "summary": "ok", "followups": [{"title": "later", "spec": "more"}]})
    assert reviews == [], "a hand-off with follow-ups is the coordinator's"


def _own_checks_sleeper(p, tmp_path):
    """A waiting task on its own live `ttp checks --result`, with its spec digest taken (so a spec edit
    counts), and a daemon whose probes never start; returns (daemon, tid)."""
    from ttp import daemon as dmod
    from ttp.prompts import spec_digest
    tmp_path.mkdir(parents=True, exist_ok=True)
    tid, _ = _hold_checks_wait(p, tmp_path, alive=True)
    res = json.loads(p.db.task(tid)["result"])
    p.db.update_task(tid, result=json.dumps({**res, "spec_seen": spec_digest(p.db.task(tid))}))
    d = dmod.Daemon(p.base)
    d._start_probe = lambda *a, **k: None
    return d, tid


def test_a_wake_now_spec_change_waits_while_the_tasks_own_checks_run_but_an_urgent_line_wakes_it(env, monkeypatch, tmp_path):
    p = make(env)
    d, tid = _own_checks_sleeper(p, tmp_path)
    d.probe_waiting()
    _edit_spec(p, tid, "also report the peak memory")   # a coordinator task_update with wake_now
    d.probe_waiting()
    assert _asleep(p, tid), "a spec change woke a task whose own checks still run"
    assert "woke" not in json.loads(p.db.task(tid)["result"])
    _edit_spec(p, tid, "urgent: the user is waiting")
    d.probe_waiting()
    assert not _asleep(p, tid), "an urgent line waits for the checks"
    assert "spec" in json.loads(p.db.task(tid)["result"]).get("woke", "")
    p2 = make(env)
    d2, tid2 = _own_checks_sleeper(p2, tmp_path / "off")
    p2.set_config("waiting.hold_own_runs", False)
    d2.cfg = p2.config()
    _edit_spec(p2, tid2, "also report the peak memory")
    d2.probe_waiting()
    assert not _asleep(p2, tid2), "with the hold off, a wake_now spec change wakes it as before"


def test_a_changed_probe_answer_waits_while_the_tasks_own_checks_run(env, monkeypatch, tmp_path):
    from ttp import daemon as dmod
    for hold, woke in ((True, False), (False, True)):
        p = make(env)
        d, tid = _own_checks_sleeper(p, tmp_path / f"h{hold}")
        p.set_config("waiting.hold_own_runs", hold)
        d.cfg = p.config()
        probe = dmod._current_probe(p.db.task(tid))
        monkeypatch.setattr(d, "_gate_answer", lambda *a, **k: "changed")
        d._probe_left = [(tid, object(), probe, 2, "", "", "")]   # its probe answered exit 2: a new answer
        d.probe_waiting()
        assert (not _asleep(p, tid)) is woke, (hold, json.loads(p.db.task(tid)["result"]))


def test_a_routine_review_runs_beside_its_prechecks_unless_the_hold_is_turned_on(env, tmp_path, monkeypatch):
    from ttp import cli, daemon, reviewnow
    from ttp.daemon import PRECHECK_LABEL
    out = tmp_path / daemon.PRECHECKS_DIR / "t1-abc"
    out.mkdir(parents=True)
    monkeypatch.setattr(cli, "_read_checks_pid", lambda o: {"ts": 1000.0})
    monkeypatch.setattr(cli, "_checks_alive", lambda info: True)
    (out / cli.CHECKS_RUNNING).write_text(json.dumps({"since": 1000.5}))
    task = {"id": 2, "kind": "review", "priority": 3, "spec": "check it", "labels": json.dumps([PRECHECK_LABEL + "t1-abc"])}
    assert not reviewnow.hold_for_precheck({}), "off by default"
    assert not daemon.precheck_holds(tmp_path, task, 1001.0, hold=reviewnow.hold_for_precheck({}))
    assert reviewnow.hold_for_precheck({"review": {"hold_for_precheck": True}})
    assert daemon.precheck_holds(tmp_path, task, 1001.0, hold=True)
