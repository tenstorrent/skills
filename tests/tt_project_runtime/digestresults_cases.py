# Digest task-result tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file. Add new tests of
# digestresults.py (task results and the last push batch in the coordinator digest) here.
from __future__ import annotations


def _dr_event(p, text, kind="push_failed", source="push"):
    return p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                  (time.time(), source, kind, "normal", text, "new"))


def _dr_done(p, title, summary, run_dir=None):
    tid = p.db.add_task(title, "s", kind="code")
    p.db.update_task(tid, status="done", result=json.dumps({"status": "done", "summary": summary}))
    if run_dir:
        p.db.x("INSERT INTO runs(task, role, dir, status) VALUES(?,?,?,?)", (tid, "worker", run_dir, "done"))
    return tid


def _dr_section(parts, key="results"):
    return "\n".join(t for k, t in parts if k == key)


def _dr_ack(p, shown, sid="s1", **extra):
    """The session acknowledged the wake that left `shown` as the baseline."""
    from ttp import coordsession as cs
    p.db.set_kv(cs.KEY, {"id": sid, cs.DIGEST: {"seen": cs.fingerprint(shown), "sections": {}}, **extra})


def test_digest_results_show_a_named_finished_task_once_with_its_path_and_the_push_batch(env, tmp_path):
    from ttp import coordinator as coord, digestresults as dr
    p = make(env)
    p.db.x("UPDATE events SET status='handled'")
    tid = _dr_done(p, "fix the parser", "parser fixed; 3 tests added", str(tmp_path / "runs" / "7"))
    marker = tmp_path / "batch.json"
    marker.write_text(json.dumps({"message": "required suite failed: test_parser_edge"}))
    p.db.x("INSERT INTO push_batches(id, marker, target, started, ended, outcome) VALUES(?,?,?,?,?,?)",
           ("batch-1", str(marker), "main", time.time() - 60, time.time(), "check_failed"))
    ev = _dr_event(p, f"push of #{tid} failed its checks")
    whole: list = []
    _dr_ack(p, {})
    parts, shown = coord.digest_parts(p, {}, [ev], [], {}, None, whole)
    got = _dr_section(parts)
    assert f"#{tid} done: fix the parser" in got and "parser fixed; 3 tests added" in got
    assert f"result: {tmp_path}/runs/7/result.json" in got
    assert "last push batch batch-1: check_failed, failed: required suite failed: test_parser_edge" in got
    # The next wake, nothing changed: one line, the entry stays in the whole digest.
    _dr_ack(p, shown)
    whole = []
    parts, shown2 = coord.digest_parts(p, {}, [_dr_event(p, f"#{tid} still named")], [], shown, None, whole)
    got = _dr_section(parts)
    assert f"#{tid}" not in got and "none new since your last wake" in got
    assert f"#{tid} done" in _dr_section(whole) and "last push batch" in _dr_section(whole)
    # A changed result goes out again.
    p.db.update_task(tid, result=json.dumps({"status": "done", "summary": "parser fixed again"}))
    _dr_ack(p, shown2)
    parts, _ = coord.digest_parts(p, {}, [], [], shown2)
    assert "parser fixed again" in _dr_section(parts) and "last push batch" not in _dr_section(parts)
    # A task whose whole hand-off is in this wake's events is left to NEW EVENTS.
    other = _dr_done(p, "other", "other summary")
    ev = _dr_event(p, "hand-off", kind="task_done", source=f"task:{other}")
    parts, _ = coord.digest_parts(p, {}, [ev], [], {})
    assert f"#{other} done: other" not in _dr_section(parts)
    assert dr.named(f"user msg #{tid} and task:{other}, #5") == {other, 5}


def test_digest_results_are_resent_after_a_compaction_a_new_session_or_an_unacknowledged_wake(env):
    from ttp import coordinator as coord, coordsession as cs
    p = make(env)
    p.db.x("UPDATE events SET status='handled'")
    tid = _dr_done(p, "build docs", "docs built")
    _dr_ack(p, {})
    parts, shown = coord.digest_parts(p, {}, [], [], {})
    assert f"#{tid} done" in _dr_section(parts)
    for state in ({"restore": True}, {"sid": "s2"}, None):
        _dr_ack(p, shown)
        parts, again = coord.digest_parts(p, {}, [], [], shown)
        assert f"#{tid} done" not in _dr_section(parts)
        if state is None:   # the last wake's digest was never acknowledged
            p.db.set_kv(cs.KEY, {"id": "s1", cs.DIGEST: {"seen": "other", "sections": {}}})
        elif "sid" in state:
            _dr_ack(p, again, sid=state["sid"])
        else:
            _dr_ack(p, again, **state)
        parts, _ = coord.digest_parts(p, {}, [], [], again)
        assert f"#{tid} done" in _dr_section(parts), state
    # A compacted session gets the whole digest, the entries included.
    whole: list = []
    parts, shown = coord.digest_parts(p, {}, [], [], shown, None, whole)
    sent, _, why = cs.wake_digest("resume", {"restore": True, cs.DIGEST: {"sections": {}}}, parts, whole,
                                  cs.fingerprint(shown), "x", "g")
    assert why.startswith("full") and f"#{tid} done" in "\n".join(t for _, t in sent)


def test_digest_results_keep_to_their_size_cap_and_name_what_is_left_out(env):
    from ttp import coordinator as coord, digestresults as dr
    p = make(env)
    for i in range(30):
        _dr_done(p, f"task {i}", "x" * 1000)
    parts, _ = coord.digest_parts(p, {}, [], [], {})
    got = _dr_section(parts)
    entries = [e for e in got.split("\n- ") if e.startswith("#")]
    assert entries and sum(len(e) for e in entries[:-1]) < dr.CAP
    assert len(got) < dr.CAP + 1200 and "more, over the size cap: #" in got


def test_digest_fold_never_hides_a_section_naming_a_task_this_wake_names(env, monkeypatch):
    from ttp import coordinator as coord
    p = make(env)
    p.db.x("UPDATE events SET status='handled'")
    monkeypatch.setattr(coord.tried, "digest_lines", lambda *a, **k: ["## Tried work", "- #4242 shelved: slow path"])
    parts, shown = coord.digest_parts(p, {}, [], [], {})
    assert "#4242 shelved" in _dr_section(parts, "tried")
    parts, _ = coord.digest_parts(p, {}, [], [], shown)
    assert "(as last turn)" in _dr_section(parts, "tried"), "an unchanged section was sent whole"
    parts, _ = coord.digest_parts(p, {}, [_dr_event(p, "about #4242")], [], shown)
    assert "#4242 shelved" in _dr_section(parts, "tried"), "a fold hid the item this wake names"
    monkeypatch.setattr(coord.tried, "digest_lines", lambda *a, **k: ["## Tried work", "- #4243 landed"])
    parts, _ = coord.digest_parts(p, {}, [], [], shown)
    assert "#4243 landed" in _dr_section(parts, "tried"), "a changed section was folded"


def test_digest_results_off_switch_and_its_validator(env):
    from ttp import coordinator as coord
    p = make(env)
    _dr_done(p, "some task", "done well")
    assert _dr_section(coord.digest_parts(p, {}, [], [], {})[0])
    p.set_config("coordinator.digest_results", False)
    assert not _dr_section(coord.digest_parts(p, {}, [], [], {})[0])
    check = coord.settable("coordinator.digest_results")
    assert check(" Off ") is False and check("on") is True
    with pytest.raises(ValueError):
        check("sometimes")
