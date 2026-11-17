# Model-free handling of a task's wait on its own checks or push (ownwait.py): tests of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations


def test_a_report_only_next_step_is_told_from_one_that_needs_a_model():
    from ttp import ownwait
    for step in ("report the push", "Report the push.", "report the landing and finish", "record the checks result",
                 "  report   the push result  ", "confirm the landed commit", "post the pass and stop"):
        assert ownwait.report_only(step), step
    # Bypasses: a second action, a compound, shell punctuation, a long step, a non-text step.
    for step in ("report the push, then rebase onto main", "report the push; ttp push --detach", "report the push && ttp push",
                 "report the push `ttp push`", "report the push $(ttp push)", "report the push\nand open a PR",
                 "open a PR after the push", "retry the push", "report the push " + "and the result " * 8, "",
                 "report", "report the push and", "report the push then", "rebase and report the push", "report the push or retry", None, 5, ["report the push"]):
        assert not ownwait.report_only(step), step


def test_a_hand_off_names_a_step_only_when_the_step_needs_a_model():
    from ttp import ownwait
    assert not ownwait.names_step({}) and not ownwait.names_step({"next_step": ""})
    assert not ownwait.names_step({"next_step": "report the push"})
    assert ownwait.names_step({"next_step": "ttp push --detach"})
    assert ownwait.names_step({"next_step": "report the push and open a PR"})
    assert not ownwait.names_step(None) and not ownwait.names_step("report the push")



def test_a_report_only_next_step_settles_a_push_or_checks_wait_without_a_model_run():
    from ttp import daemon as dmod
    code = {"kind": "code", "spec": "fix the parser"}
    hand = {"status": "waiting", "summary": "push running", "retry_when": "ttp push --result /tmp/m"}
    assert dmod.push_on_pass(code, hand)["status"] == "done"
    assert dmod.push_on_pass(code, {**hand, "next_step": "report the push"})["status"] == "done"
    assert dmod.default_on_pass(code, {**hand, "retry_when": "ttp checks --result /tmp/r",
                                       "next_step": "Report the checks result and finish."})["status"] == "done"
    # A step that needs a model keeps the wake, in both paths, and so does anything beside it.
    for step in ("ttp push --detach", "rebase onto main, then report the push", "report the push; open a PR"):
        assert dmod.push_on_pass(code, {**hand, "next_step": step}) is None, step
        assert dmod.default_on_pass(code, {**hand, "next_step": step}) is None, step
    for extra in ({"pr": "https://x/pull/1"}, {"on_pass_cmd": "true"}, {"wake_tier": "standard"}):
        assert dmod.push_on_pass(code, {**hand, "next_step": "report the push", **extra}) is None, extra


def test_checks_settle_accepts_a_report_only_next_step_and_refuses_any_other(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import checksettle
    tid, path = _checks_wait_task(p, tmp_path, rc="0")
    p.db.x("UPDATE tasks SET kind='work' WHERE id=?", (tid,))
    run_dir = tmp_path / f"run-{tid}"
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    hand = json.loads((run_dir / "result.json").read_text())
    on_pass = {"status": "done", "summary": "recorded"}
    (run_dir / "result.json").write_text(json.dumps({**hand, "next_step": "report the result", "on_pass": on_pass}))
    assert checksettle.settle(run_dir, head)[2] == on_pass
    for step in ("ttp push --detach", "report the result && ttp push", "report the result `id`"):
        (run_dir / "result.json").write_text(json.dumps({**hand, "next_step": step, "on_pass": on_pass}))
        rc, what, out = checksettle.settle(run_dir, head)
        assert rc == 1 and out is None and "next_step" in what, (step, what)
