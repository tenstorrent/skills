# Blind reviews (blindreview.py) tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there). A blind review or grading run must not learn which model, provider,
# effort or model family made the work it scores, through any `ttp` command it may run.
from __future__ import annotations

import json as _json

import pytest as _pytest


def _blind_pair(p):
    """A finished A/B task run by an open model, and a blind review of it (made-up names)."""
    from ttp import blindreview
    live = p.db.add_task("Soak the widget board", "Run the soak and report.", kind="work", model="glm-x",
                         provider="chat", effort="max", labels=["device_ab:glm", "needs_device"])
    p.db.x("UPDATE tasks SET status='done', spent_usd=0, result=? WHERE id=?",
           (_json.dumps({"summary": "glm-x found the soak passed", "model": "glm-x"}), live))
    rid = p.db.add_task("Device A/B review of #%d" % live, "Score it.", kind="question", model="opus",
                        effort="high", labels=[f"device_ab_review:{live}", blindreview.BLIND_LABEL])
    return live, rid


def _show(cli, capsys, *argv):
    capsys.readouterr()
    cli.main(["task", *argv])
    return capsys.readouterr().out


def test_a_blind_review_reads_no_model_provider_effort_or_arm_through_task_show_or_list(env, monkeypatch, capsys):
    from ttp import blindreview, cli
    p = make(env)
    live, rid = _blind_pair(p)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    # A normal caller (the coordinator, a normal worker) keeps the full data.
    monkeypatch.delenv("TTP_TASK", raising=False)
    full = _json.loads(_show(cli, capsys, "show", str(live), "--json"))
    assert (full["model"], full["provider"], full["effort"]) == ("glm-x", "chat", "max")
    assert "device_ab:glm" in full["labels"]
    monkeypatch.setenv("TTP_TASK", str(live))
    assert "device_ab:glm" in _show(cli, capsys, "show", str(live), "--json")
    # The blind reviewer, found by its task's label alone (no TTP_BLIND in its env).
    monkeypatch.setenv("TTP_TASK", str(rid))
    monkeypatch.delenv("TTP_BLIND", raising=False)
    got = _json.loads(_show(cli, capsys, "show", str(live), "--json"))
    for key in ("model", "provider", "effort", "spent_usd", "budget_usd"):
        assert key not in got, key
    assert "device_ab" not in got["labels"] and "needs_device" in got["labels"]
    outs = [_json.dumps(got), _show(cli, capsys, "show", str(live)), _show(cli, capsys, "show", str(rid), "--json"),
            _show(cli, capsys, "list", "--json"), _show(cli, capsys, "list")]
    for out in outs:
        assert "glm" not in out.lower() and "opus" not in out.lower(), out
    assert "found the soak passed" in outs[1]   # the hand-off itself stays readable
    # The run's environment flag alone (a provider without the task id, a bench grader) is blind too.
    monkeypatch.delenv("TTP_TASK")
    monkeypatch.setenv("TTP_BLIND", "1")
    assert "glm" not in _show(cli, capsys, "show", str(live), "--json").lower()
    assert blindreview.env(p.db.task(rid)) == {"TTP_BLIND": "1"} and blindreview.env(p.db.task(live)) == {}


@_pytest.mark.parametrize("argv", [["stats"], ["device-shadow"], ["overview"], ["task", "steer-me", "1"]])
def test_a_blind_review_cannot_run_ttp_commands_that_report_models_or_runs(env, monkeypatch, capsys, argv):
    from ttp import blindreview, cli
    p = make(env)
    _live, rid = _blind_pair(p)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_TASK", str(rid))
    fn = {"stats": "cmd_stats", "device-shadow": "cmd_device_shadow", "overview": "cmd_overview"}.get(argv[0], "cmd_task")
    assert blindreview.refusal(fn, argv[1] if fn == "cmd_task" else "")
    assert blindreview.refusal("cmd_note", "") is None and blindreview.refusal("cmd_task", "show") is None

    class A:
        action = argv[1] if fn == "cmd_task" else None
    A.fn = type("F", (), {"__name__": fn})
    with _pytest.raises(SystemExit) as e:
        cli.blind_gate(A)
    assert e.value.code == 2 and "blind review" in capsys.readouterr().err
    monkeypatch.delenv("TTP_TASK")
    cli.blind_gate(A)   # a normal caller passes


def test_a_blind_review_starts_without_the_coordinator_context_and_reads_a_masked_copy_of_the_run(env, monkeypatch,
                                                                                                tmp_path):
    from ttp import blindreview, ctxfork
    p = make(env)
    live, rid = _blind_pair(p)
    monkeypatch.setenv("TTP_TEST_WORKER_CONTEXT", "disk")
    assert ctxfork.prepare(p, p.config(), None, "claude", str(tmp_path), {}, task=rid)["how"] == "none"
    assert ctxfork.prepare(p, p.config(), None, "claude", str(tmp_path), {}, task=live)["how"] == "disk"
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(_json.dumps({"provider": "chat", "model": "glm-x", "effort": "max"}))
    (run / "result.json").write_text(_json.dumps({"summary": "ok", "model": "glm-x", "usd": 0}))
    (run / "output.jsonl").write_text(_json.dumps({"type": "system", "model": "claude-opus-9"}) + "\n"
                                      + _json.dumps({"type": "text", "text": "I am Sonnet; read ~/.claude/x"}) + "\n")
    (run / "progress.md").write_text("step 1 on GLM-5.9\n")
    got = blindreview.packet(p.state, "ab-1", str(run), "plan by gemini-3")
    names = sorted(f.name for f in __import__("pathlib").Path(got).iterdir())
    assert names == ["output.jsonl", "plan.md", "progress.md", "result.json"], "never run.json"
    text = "".join(f.read_text() for f in __import__("pathlib").Path(got).iterdir()).lower()
    for word in ("glm", "opus", "sonnet", "gemini", '"model"', '"usd"'):
        assert word not in text, word
    assert "/.claude/x" in text and "ok" in text   # paths and the work itself stay readable


def test_device_reviews_are_flagged_blind_and_point_at_the_masked_copy_not_the_run_folder(env, tmp_path):
    from ttp import blindreview, deviceab
    p = make(env)
    live, rid = _blind_pair(p)
    p.db.x("DELETE FROM tasks WHERE id=?", (rid,))   # no review yet: both reviewers get one
    run = tmp_path / "rawrun"
    run.mkdir()
    (run / "result.json").write_text("{}")
    p.db.x("INSERT INTO runs(task, role, dir, started, status) VALUES(?,?,?,?,?)", (live, "worker", str(run), 1.0, "ok"))
    s = {"since": 0}
    new = deviceab.queue_reviews(p.db, s, 0.0, second={"model": "glm-x", "effort": "max", "provider": "chat"})
    assert len(new) == 2
    for t in (p.db.task(i) for i in new):
        assert blindreview.BLIND_LABEL in t["labels"] and str(run) not in t["spec"]
        assert str(__import__("pathlib").Path(p.state) / "blind") in t["spec"]
