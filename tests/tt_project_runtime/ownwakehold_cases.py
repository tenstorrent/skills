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


def test_a_plain_needs_review_code_hand_off_is_reviewed_at_once(env):
    from ttp import reviewnow
    p = make(env)
    cfg = p.config()
    code = {"kind": "code"}
    assert reviewnow.at_once(cfg, code, "review", True)
    assert not reviewnow.at_once(cfg, code, "review", False), "notes or follow-ups still go to the coordinator"
    assert not reviewnow.at_once(cfg, code, "done", True)
    assert not reviewnow.at_once(cfg, {"kind": "work"}, "review", True)
    p.set_config("review.at_once", False)
    assert not reviewnow.at_once(p.config(), code, "review", True), "the off-switch"
    _with_origin(env, clone=False)
    p.set_config("review.at_once", True)
    _, _, _, reviews = _finish_code(env, p, "plain", {"x.py": 3}, result={"status": "needs_review", "summary": "ok"})
    assert len(reviews) == 1
    _, _, _, reviews = _finish_code(env, p, "with notes", {"y.py": 3}, result={
        "status": "needs_review", "summary": "ok", "followups": [{"title": "later", "spec": "more"}]})
    assert reviews == [], "a hand-off with follow-ups is the coordinator's"


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
