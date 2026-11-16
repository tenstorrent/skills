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


def test_a_packet_masks_model_ids_that_appear_as_dict_keys(env, tmp_path):
    """A usage map keyed by a model id (the CLI result record's shape) keeps no id as its key:
    the key is re-named (stably, so two models never collide) and the id appears nowhere in the packet."""
    from ttp import blindreview
    p = make(env)
    run = tmp_path / "run"
    run.mkdir()
    (run / "result.json").write_text(_json.dumps({
        "summary": "done", "provider": "zhipu", "effort": "max",
        "usage": {"glm-5.3": {"input_tokens": 10}, "opus": {"input_tokens": 20}}}))
    (run / "output.jsonl").write_text(_json.dumps({"type": "x", "usage": {"claude-sonnet-5-5": {"in": 1}}}) + "\n")
    got = blindreview.packet(p.state, "ab-2", str(run), None)
    text = "".join(f.read_text() for f in __import__("pathlib").Path(got).iterdir()).lower()
    for bad in ("glm-5.3", "opus", "claude-sonnet", "zhipu", "anthropic", '"provider"'):
        assert bad not in text, bad
    # the two usage keys survive as distinct masked keys: no two models collapse into one
    keys = _json.loads((__import__("pathlib").Path(got) / "result.json").read_text())["usage"]
    assert len(keys) == 2 and len(set(keys)) == 2


def test_each_packet_numbers_its_masked_keys_from_one_and_names_an_equal_key_alike(env, tmp_path):
    """Masked key names are numbered per packet: two packets built back to back both start at
    [model:1], whatever was masked before, and the same model id gets the same name across a packet's
    files."""
    from pathlib import Path
    from ttp import blindreview
    p = make(env)
    run = tmp_path / "run"
    run.mkdir()
    (run / "result.json").write_text(_json.dumps({"usage": {"glm-5.3": {"in": 1}, "opus": {"in": 2}}}))
    (run / "output.jsonl").write_text(_json.dumps({"usage": {"opus": {"in": 3}}}) + "\n")
    blindreview.mask({"usage": {"gemini-x": 1}})   # masking elsewhere first moves no packet's numbering
    for key in ("ab-1", "ab-2"):
        got = Path(blindreview.packet(p.state, key, str(run), None))
        usage = _json.loads((got / "result.json").read_text())["usage"]
        assert list(usage) == ["[model:1]", "[model:2]"], key
        assert list(_json.loads((got / "output.jsonl").read_text())["usage"]) == ["[model:2]"], key


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


def test_a_second_reviewer_copy_is_not_held_as_a_duplicate_of_the_first_reviewer(env):
    # upstream note 0d30b799dc7e85f8: the second reviewer's device review shares its title words with the
    # first; its label names another reviewer slot, so dispatch lets it start. A real repeat is still held.
    from ttp import supersede
    p = make(env)
    live = p.db.add_task("Calibrate the widget rig", "Run it.", kind="work", labels=["device_ab:arm-x"])
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (live,))
    kw = dict(kind="question", origin="daemon", priority=4)
    first = p.db.add_task(f"Device A/B review of #{live}", "Score the run.", labels=[f"device_ab_review:{live}"], **kw)
    second = p.db.add_task(f"Device A/B review of #{live} (second reviewer)", "Score the run.",
                           labels=[f"device_ab_review2:{live}"], **kw)
    s1 = p.db.add_task(f"Device shadow review of #{live} (s0a0a0a0a)", "Score the shadow.",
                       labels=[f"device_shadow_review:{live}:s0a0a0a0a"], **kw)
    s2 = p.db.add_task(f"Device shadow review of #{live} (s0a0a0a0a) (second reviewer)", "Score the shadow.",
                       labels=[f"device_shadow_review2:{live}:s0a0a0a0a"], **kw)
    assert supersede.duplicate_of(p.db, p.db.task(first)) is None
    assert supersede.duplicate_of(p.db, p.db.task(second)) is None
    assert supersede.duplicate_of(p.db, p.db.task(s2)) is None and supersede.dispatch_hold(p.db, p.db.task(s2)) == ""
    again = p.db.add_task(f"Device A/B review of #{live} (second reviewer)", "Score the run.",
                          labels=[f"device_ab_review2:{live}"], **kw)
    assert supersede.duplicate_of(p.db, p.db.task(again))["id"] == second
    assert s1


def test_daemon_device_reviews_do_not_fill_the_coordinators_new_task_cap(env):
    # upstream note 0d30b799dc7e85f8: they count under the separate review valve instead.
    from ttp import coordinator as coord
    p = make(env)
    kw = dict(kind="question", origin="daemon", priority=4)
    p.db.add_task("Device A/B review of #1", "Score.", labels=["device_ab_review:1", "blind_review"], **kw)
    p.db.add_task("Device A/B review of #1 (second reviewer)", "Score.", labels=["device_ab_review2:1"], **kw)
    p.db.add_task("Device shadow review of #1 (s1)", "Score.", labels=["device_shadow_review2:1:s1"], **kw)
    p.db.add_task("Find why the widget soak stalls", "Look.", kind="question", origin="coordinator")
    assert len(coord.tasks_made(p.db, 0)) == 1
    assert len(coord.tasks_made(p.db, 0, review=True)) == 3


def _blind_runs(p, tmp_path, tid, legs):
    """One run folder per leg of task `tid`, recorded oldest first like the daemon does (made-up names)."""
    from pathlib import Path
    dirs = []
    for i, (model, provider, lines) in enumerate(legs, 1):
        d = Path(tmp_path) / "runs" / str(90110 + i)
        d.mkdir(parents=True)
        (d / "run.json").write_text(_json.dumps({"provider": provider, "model": model}))
        (d / "result.json").write_text(_json.dumps({"status": "waiting" if i < len(legs) else "done",
                                                    "summary": f"leg {i} hand-off by {model}", "model": model,
                                                    "provider": provider, "run_id": 90110 + i}))
        (d / "progress.md").write_text(f"leg {i} progress\n")
        (d / "output.jsonl").write_text("".join(_json.dumps(x) + "\n" for x in lines))
        p.db.x("INSERT INTO runs(task, role, provider, model, dir, started, status) VALUES(?,?,?,?,?,?,?)",
               (tid, "worker", provider, model, str(d), float(i), "ok"))
        dirs.append(d)
    p.db.x("INSERT INTO runs(task, role, dir, started, status) VALUES(?,?,?,?,?)",
           (tid, "coordinator", str(Path(tmp_path) / "coord"), 9.0, "ok"))   # never a coordinator run
    return dirs


def test_a_task_with_three_runs_yields_three_ordered_masked_segments(env, tmp_path):
    """A blind packet holds every run of the task, oldest first, each named only by its order (run 1 of 3 ...):
    a waiting hand-off and its resume, or several device legs, are all in front of the reviewer."""
    from pathlib import Path
    from ttp import blindreview
    p = make(env)
    tid = p.db.add_task("Soak the widget board", "Soak it.", kind="work")
    legs = [("glm-5.3", "chat", [{"type": "text", "text": "leg 1 by GLM, session s1"}]),
            ("claude-opus-5-5", "claude", [{"type": "text", "text": "leg 2", "usage": {"opus": {"in": 1}}}]),
            ("gpt-9", "codex", [{"type": "text", "text": "leg 3 final"}])]
    dirs = _blind_runs(p, tmp_path, tid, legs)
    assert blindreview.run_dirs(p.db, tid) == [str(d) for d in dirs]
    got = Path(blindreview.packet(p.state, f"ab-{tid}", blindreview.run_dirs(p.db, tid)))
    assert sorted(f.name for f in got.iterdir()) == ["run-1-of-3", "run-2-of-3", "run-3-of-3", "runs.md"]
    for i in (1, 2, 3):
        seg = got / f"run-{i}-of-3"
        assert sorted(f.name for f in seg.iterdir()) == ["output.jsonl", "progress.md", "result.json"]
        assert _json.loads((seg / "result.json").read_text())["summary"].startswith(f"leg {i} hand-off")
        assert f"leg {i}" in (seg / "output.jsonl").read_text() and f"leg {i}" in (seg / "progress.md").read_text()
    text = "".join(f.read_text() for f in got.rglob("*") if f.is_file()).lower()
    for bad in ("glm", "opus", "claude-", "gpt", "codex", "zhipu", '"model"', '"provider"', "9011"):
        assert bad not in text, bad
    assert "run 1 of 3" in (got / "runs.md").read_text()


def test_a_single_run_tasks_packet_is_unchanged(env, tmp_path):
    """One run: its files sit in the packet itself as before (no run folders, no runs.md), the same bytes
    whether the run is passed alone or as the task's run list."""
    from pathlib import Path
    from ttp import blindreview
    p = make(env)
    tid = p.db.add_task("Soak the widget board", "Soak it.", kind="work")
    (d,) = _blind_runs(p, tmp_path, tid, [("glm-5.3", "chat", [{"type": "text", "text": "only leg"}])])
    one = Path(blindreview.packet(p.state, "one", str(d), "plan"))
    many = Path(blindreview.packet(p.state, "many", blindreview.run_dirs(p.db, tid), "plan"))
    assert sorted(f.name for f in one.iterdir()) == ["output.jsonl", "plan.md", "progress.md", "result.json"]
    assert {f.name: f.read_bytes() for f in one.iterdir()} == {f.name: f.read_bytes() for f in many.iterdir()}
    assert blindreview.packet(p.state, "none", [], None) == ""


def test_the_size_cap_trims_transcript_middles_and_keeps_every_hand_off_and_final_message(env, tmp_path,
                                                                                        monkeypatch):
    from pathlib import Path
    from ttp import blindreview
    p = make(env)
    monkeypatch.setattr(blindreview, "PACKET_MAX", 6000)
    tid = p.db.add_task("Soak the widget board", "Soak it.", kind="work")
    filler = [{"type": "text", "text": f"step {k} " + "x" * 80} for k in range(60)]
    legs = [("glm-5.3", "chat", [{"type": "text", "text": f"FIRST {i}"}] + filler
             + [{"type": "result", "text": f"FINAL {i} " + "y" * (2500 if i == 2 else 50)}]) for i in (1, 2, 3)]
    _blind_runs(p, tmp_path, tid, legs)
    got = Path(blindreview.packet(p.state, f"ab-{tid}", blindreview.run_dirs(p.db, tid)))
    total = 0
    for i in (1, 2, 3):
        seg = got / f"run-{i}-of-3"
        lines = [_json.loads(x) for x in (seg / "output.jsonl").read_text().splitlines()]
        assert lines[0]["text"] == f"FIRST {i}", "the head stays"
        assert lines[-1]["text"].startswith(f"FINAL {i}"), "the final message stays whole"
        assert len(lines[-1]["text"]) == len(legs[i - 1][2][-1]["text"])
        assert any(x.get("type") == "trimmed" for x in lines), "the middle went"
        assert _json.loads((seg / "result.json").read_text())["summary"] == f"leg {i} hand-off by [model]"
        total += (seg / "output.jsonl").stat().st_size
    assert total < 6000 + 3 * 400, total   # within the cap, give or take the trim notes and masking
    assert "trimmed" in (got / "runs.md").read_text()


def test_device_reviews_get_every_run_of_the_task(env, tmp_path):
    """The A/B and shadow reviews build their packets from all of the task's runs, and their specs say so."""
    from pathlib import Path
    from ttp import blindreview, deviceab
    p = make(env)
    live, rid = _blind_pair(p)
    p.db.x("DELETE FROM tasks WHERE id=?", (rid,))
    _blind_runs(p, tmp_path, live, [("glm-x", "chat", [{"type": "text", "text": f"leg {i}"}]) for i in (1, 2)])
    new = deviceab.queue_reviews(p.db, {"since": 0}, 0.0)
    assert new and "runs.md" in p.db.task(new[0])["spec"]
    got = Path(p.state) / "blind" / f"ab-{live}"
    assert sorted(f.name for f in got.iterdir()) == ["run-1-of-2", "run-2-of-2", "runs.md"]
