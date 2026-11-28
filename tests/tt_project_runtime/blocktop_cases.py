# Daily top-blockers tests (blockledger.top, status, digest_lines, `ttp blocked --done`) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file (_bl_task is blockledger_cases.py's). Add new top-blocker tests here.
from __future__ import annotations


def _bt_hold(p, now, minutes, reason, title="held"):
    return _bl_task(p, title, "blocked", now, blocked_ago=minutes * 60, reason=reason)


def test_block_top_lists_convenience_rules_by_minutes_and_safety_apart(env):
    from ttp import blockledger as bl
    p = make(env)
    now = time.time()
    _bt_hold(p, now, 30, "keep the 60 min hold for the nightly job")
    _bt_hold(p, now, 90, "waiting for the shipping window to open")
    _bt_hold(p, now, 600, "held by R-ab12 until later")
    top = bl.top(p.db, now)
    assert [e["rule"] for e in top] == ["for the shipping window to open", "keep the min hold for the"]
    assert [e["rule"] for e in bl.top(p.db, now, safety=True)] == ["R-ab12"]
    assert all(e["safety"] == "convenience" and e["key"] for e in top)


def test_block_top_counts_the_plugin_gates_but_not_the_dollar_caps(env):
    from ttp import blockledger as bl
    p = make(env)
    now = time.time()
    _bt_hold(p, now, 40, "task budget reached before the change landed")
    _bt_hold(p, now, 50, "change size over the limit, split it")
    _bt_hold(p, now, 60, "the project spend cap is reached")
    assert {e["rule"].split()[0] for e in bl.top(p.db, now)} == {"task", "change"}
    assert [e["rule"] for e in bl.top(p.db, now, safety=True)] == ["the project spend cap is reached"]


def test_block_top_digest_lines_show_until_done_and_cap_at_five(env):
    from ttp import blockledger as bl, rulecite
    p = make(env)
    now = time.time()
    for i in range(7):
        _bt_hold(p, now, 10 + i, f"waiting for window {'abcdefg'[i] * 3} to open")
    lines = [l for l in rulecite.digest_lines(p.db, now) if l.startswith("- top blocker")]
    assert len(lines) == 5 and "for 16 min today" in lines[0]
    assert "retire it (memory_forget / config_set / code task) or rewrite it" in lines[0]
    key = bl.top(p.db, now)[0]["key"]
    assert f"`ttp blocked --done {key} --how" in lines[0]
    bl.done(p.db, key, "dropped the window rule")
    after = [l for l in rulecite.digest_lines(p.db, now) if l.startswith("- top blocker")]
    assert all(f"top blocker {key}:" not in l for l in after) and len(after) == 5


def test_block_top_a_done_rule_that_holds_time_again_comes_back(env):
    from ttp import blockledger as bl, rulecite
    p = make(env)
    now = time.time()
    t = _bt_hold(p, now, 120, "keep the 60 min hold for the nightly job")
    key = bl.top(p.db, now)[0]["key"]
    bl.done(p.db, key, "lowered it to 5 min")
    p.db.x("UPDATE kv SET ts=? WHERE key=?", (now - 3600, bl.DONE + key))   # done an hour ago
    assert not any("came back" in l for l in rulecite.digest_lines(p.db, now)), "a hold open across --done is not new"
    p.db.x("UPDATE events SET ts=? WHERE task=?", (now - 1800, t))   # the hold began after it: the rule came back
    got = rulecite.digest_lines(p.db, now)
    assert f"- came back: {key} (lowered it to 5 min did not stop it)" in got
    assert not any(l.startswith("- top blocker") for l in got)
    assert "top blockers open: 0, came back: 1" in bl.daily_line(p.db, now)
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (t,))
    p.db.x("UPDATE events SET ts=? WHERE task=?", (now - 5 * 3600, t))
    p.db.x("INSERT INTO events(ts,source,kind,text,data,status,task) VALUES(?,?,?,?,?,?,?)",
           (now - 2 * 3600, "t", "task_started", f"#{t} held started", None, "handled", t))
    quiet = rulecite.digest_lines(p.db, now)
    assert not any("came back" in l for l in quiet)


def test_blocked_done_command_records_the_key_and_needs_how(env, capsys, monkeypatch):
    from ttp import blockledger as bl, cli
    p = make(env)
    monkeypatch.chdir(p.root)
    cli.main(["blocked", "--done", "abc", "--how", "dropped the rule"])
    assert "abc done" in capsys.readouterr().out and p.db.kv(bl.DONE + "abc") == {"how": "dropped the rule"}
    with pytest.raises(SystemExit) as e:
        cli.main(["blocked", "--done", "abd"])
    assert e.value.code and p.db.kv(bl.DONE + "abd") is None
