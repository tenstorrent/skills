# Cost lever tests of tests/test_tt_project_runtime.py (costlevers.py, wakesettle.py: the levers of task #8790
# behind rings.fresh_continue, rings.coord_wake_settle and rings.output_trim, all off by default), which runs
# this file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file
# and the other case files (_roll, _fork_daemon, _fork_transcript, _sh_setup, _ev_x, _drive).
from __future__ import annotations

_LEVERS = ("fresh_continue", "coord_wake_settle", "output_trim")


def test_cost_levers_are_off_by_default_and_a_ring_or_the_projects_own_key_turns_each_on(env, capsys):
    from ttp import costlevers, rollout
    p = make(env)
    for flag in _LEVERS:
        assert flag in rollout.FLAGS
        assert not costlevers.lever_on(p, flag), flag
    task = {"kind": "work", "spec": "rerun the tests"}
    assert costlevers.prompt_line(p, task) == ""
    # The scratch ring turns one lever on; the others stay off.
    assert _roll(capsys, "ring", "demo", "0")[0] == 0
    assert _roll(capsys, "set", "output_trim", "0")[0] == 0
    assert costlevers.lever_on(p, "output_trim")
    assert not costlevers.lever_on(p, "fresh_continue") and not costlevers.lever_on(p, "coord_wake_settle")
    assert costlevers.prompt_line(p, task) == costlevers.TRIM_LINE
    # The project's own explicit value wins over the ring, both ways.
    p.set_config("output.trim", False)
    assert not costlevers.lever_on(p, "output_trim") and costlevers.prompt_line(p, task) == ""
    p.set_config("worker.fresh_continue", True)
    assert costlevers.lever_on(p, "fresh_continue")


def _settle_evs(p, ids):
    return [dict(r) for r in p.db.q(f"SELECT * FROM events WHERE id IN ({','.join('?' * len(ids))})", ids)]


def test_wake_decisions_count_every_skip_and_rule_settles_only_whole_routine_batches(env):
    from ttp import wakesettle
    p = make(env)
    p.db.x("UPDATE events SET status='handled'")
    now = time.time()
    clock = [now]
    # The counter: a quiet skip avoids one wake at the median cost (the default until coordinator runs exist).
    wakesettle.record(p.db, "quiet", "skip", [{"id": 1, "kind": "observation"}], now)
    wakesettle.record(p.db, "triage", "wake", [{"id": 2, "kind": "task_done"}], now)
    s = wakesettle.summary(p.db, now - 60, now + 1)
    assert s["skips"] == 1 and s["wakes"] == 1 and s["usd"] == wakesettle.DEFAULT_USD
    assert s["gates"]["quiet"]["skip"] == 1 and s["gates"]["triage"]["wake"] == 1
    assert "1 settled without a model (quiet 1)" in wakesettle.line(p.db, now - 60, now + 1)
    plain = lambda e: True   # noqa: E731
    # An upstream note alone is settled by rule.
    up = _ev_x(p, clock, "upstream_note", "a note for the maintainers", "normal", "upstream", None, None)
    ids, line = wakesettle.settle(p.db, p.config(), _settle_evs(p, [up]), now, plain)
    assert ids == [up] and "settled by rule" in line
    assert p.db.one("SELECT status FROM events WHERE id=?", (up,))["status"] == "handled"
    # A batch with one event a turn must see settles nothing (all or nothing).
    up2 = _ev_x(p, clock, "upstream_note", "another note", "normal", "upstream", None, None)
    hot = _ev_x(p, clock, "observation", "box down", "high", "watcher:x", None, None)
    assert wakesettle.settle(p.db, p.config(), _settle_evs(p, [up2, hot]), now, plain) == ([], "")
    assert p.db.one("SELECT status FROM events WHERE id=?", (up2,))["status"] == "queued"
    # A task_done whose hand-off asks for a follow-up, or that is not plain, waits for a turn.
    tid = p.db.add_task("tidy docs", "tidy the docs", kind="work", origin="user")
    p.db.x("UPDATE tasks SET status='done', result=? WHERE id=?",
           (json.dumps({"status": "done", "summary": "ok", "followups": ["continue: the rest"]}), tid))
    done = _ev_x(p, clock, "task_done", f"#{tid} tidy docs → done: ok", "normal", f"task:{tid}", tid, None)
    assert wakesettle.settle(p.db, p.config(), _settle_evs(p, [done]), now, plain) == ([], "")
    p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps({"status": "done", "summary": "ok"}), tid))
    assert wakesettle.settle(p.db, p.config(), _settle_evs(p, [done]), now, lambda e: False) == ([], "")
    assert wakesettle.settle(p.db, p.config(), _settle_evs(p, [done]), now, plain)[0] == [done]
    # A re-check observation settles only once a turn met the same one.
    o1 = _ev_x(p, clock, "observation", "docs index is stale", "normal", "watcher:docs", None, None)
    assert wakesettle.settle(p.db, p.config(), _settle_evs(p, [o1]), now, plain) == ([], "")
    p.db.x("UPDATE events SET status='handled' WHERE id=?", (o1,))
    o2 = _ev_x(p, clock, "observation", "docs index is stale", "normal", "watcher:docs", None, None)
    assert wakesettle.settle(p.db, p.config(), _settle_evs(p, [o2]), now, plain)[0] == [o2]
    # A later turn handling the same task's events counts the settle as reopened.
    assert wakesettle.reopened(p.db, now - 60, now + 10) == (3, 0)
    _ev_x(p, clock, "task_failed", f"#{tid} broke", "normal", f"task:{tid}", tid, None)
    p.db.x("UPDATE events SET status='handled' WHERE task=? AND kind='task_failed'", (tid,))
    assert wakesettle.reopened(p.db, now - 60, now + 10) == (3, 1)
    assert "reopened 1/3" in wakesettle.line(p.db, now - 60, now + 10)


def test_wake_settle_flag_keeps_the_daemon_from_waking_on_a_routine_batch(env, monkeypatch, tmp_path):
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path, hook=False)
    clock[0] += 60
    i = _ev_x(p, clock, "upstream_note", "a note for the maintainers", "normal", "upstream", None, None)
    assert _drive(d, clock, starts), "off by default: the batch wakes a turn"
    assert i in turns[-1]
    rows = p.db.q("SELECT gate, verb FROM wake_decisions")
    assert not [r for r in rows if r["gate"] == "rule"]
    p.set_config("coordinator.wake_settle", True)
    d.cfg = p.config()
    p.db.set_kv("last_coordinator_turn", clock[0] + 60)
    j = _ev_x(p, clock, "upstream_note", "a second note", "normal", "upstream", None, None)
    assert not _drive(d, clock, starts, ticks=4), "the rule settled it: no turn"
    assert p.db.one("SELECT status FROM events WHERE id=?", (j,))["status"] == "handled"
    assert p.db.one("SELECT COUNT(*) n FROM wake_decisions WHERE gate='rule' AND verb='skip'")["n"] == 1
    # A user message is never held by the rule.
    p.db.x("INSERT INTO messages(ts,direction,chat,kind,text,handled) VALUES(?,?,?,?,?,0)",
           (clock[0], "in", "web", "user", "status?"))
    _ev_x(p, clock, "upstream_note", "a third note", "normal", "upstream", None, None)
    assert _drive(d, clock, starts), "a user message wakes a turn"


def test_fresh_continue_skips_the_resume_keeps_the_fork_and_leads_with_the_handoff(env, monkeypatch, tmp_path):
    from ttp import coordsession, ctxfork
    p, d, cfg_dir, _run = _fork_daemon(env, monkeypatch, tmp_path)
    sid = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, sid)
    p.db.set_kv(coordsession.KEY, {"id": sid, "provider": "claude", "context_tokens": 50000})
    tid = p.db.add_task("fix the parser", "fix the parser bug", kind="code", origin="user")
    p.db.x("UPDATE tasks SET result=? WHERE id=?",
           (json.dumps({"status": "failed", "summary": "parser half fixed: tokenizer left"}), tid))
    old = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.root, old)   # the task's own earlier session, still warm
    p.db.x("INSERT INTO runs(role, task, provider, started, ended, status, session_id) VALUES(?,?,?,?,?,?,?)",
           ("worker", tid, "claude", time.time() - 120, time.time() - 60, "ok", old))

    def start():
        rid = d.start_run("worker", "TASK PROMPT", "claude", "standard", str(p.root), task=p.db.task(tid))
        row = p.db.one("SELECT note FROM runs WHERE id=?", (rid,))
        return (p.runs / str(rid) / "prompt.md").read_text(), json.loads(row["note"] or "{}").get(ctxfork.KEY)

    prompt, got = start()   # off: the task's own warm session is resumed
    assert got["how"] == "resume" and not got.get("fresh") and "# Fresh start" not in prompt, got["why"]
    p.set_config("worker.fresh_continue", True)
    d.cfg = p.config()
    prompt, got = start()
    assert got["how"] == "fork" and got.get("fresh") is True, got   # the coordinator fork stays (#2116)
    assert "fresh continue (rings.fresh_continue)" in got["why"]
    assert "# Fresh start (rings.fresh_continue)" in prompt and "tokenizer left" in prompt
    assert prompt.index("# Fresh start") < prompt.index("TASK PROMPT")
    from ttp import costlevers
    assert costlevers.arm_of({ctxfork.KEY: got}) == "fresh"
    assert costlevers.arm_of({ctxfork.KEY: {"how": "resume"}}) == "resumed"


def test_handoff_block_is_empty_without_history_and_capped(env):
    from ttp import costlevers
    p = make(env)
    tid = p.db.add_task("t", "s", kind="code", origin="user")
    assert costlevers.handoff_block(p, tid, None) == ""
    p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps({"status": "done", "summary": "x" * 100000}), tid))
    got = costlevers.handoff_block(p, tid, None)
    assert got.startswith("# Fresh start") and len(got) <= costlevers.HANDOFF_CHARS + 20


def test_output_trim_picks_medium_effort_only_for_mechanical_unpinned_work(env, capsys):
    from ttp import costlevers
    p = make(env)
    cfg = p.config()
    cfg = {**cfg, "providers": {**cfg["providers"], "claude": {
        **cfg["providers"]["claude"], "tiers": {**cfg["providers"]["claude"]["tiers"], "standard": {
            **cfg["providers"]["claude"]["tiers"]["standard"], "effort": "high"}}}}}
    task = {"kind": "work", "spec": "rerun the docs build and copy the output"}
    assert costlevers.effort_pick(p, cfg, task, "standard", "claude") is None   # off by default
    p.set_config("output.trim", True)
    got = costlevers.effort_pick(p, cfg, task, "standard", "claude")
    assert got["effort"] == "medium" and got["from"] == "high"
    for t, tier in (({**task, "kind": "code"}, "standard"), ({**task, "kind": "review"}, "standard"),
                    ({**task, "kind": "question"}, "standard"), (task, "light"),
                    ({**task, "effort": "xhigh"}, "standard"), ({**task, "model": "opus"}, "standard"),
                    ({**task, "spec": task["spec"] + "\nconclusion: the verdict"}, "standard"),
                    ({**task, "spec": "design the new scheduler"}, "standard")):
        assert costlevers.effort_pick(p, cfg, t, tier, "claude") is None, (t, tier)


def test_cost_lever_metrics_show_in_status_and_the_daily_review(env, capsys):
    from ttp import audit, costlevers, wakesettle
    p = make(env)
    now = time.time()
    wakesettle.record(p.db, "rule", "skip", [{"id": 9, "kind": "upstream_note"}], now - 10)
    assert costlevers.fresh_line(p, now - 86400, now) == "" and costlevers.output_line(p, now - 86400, now) == ""
    p.set_config("worker.fresh_continue", True)
    tid = p.db.add_task("t", "s", kind="work", origin="user")
    p.db.x("INSERT INTO runs(role, task, started, ended, status, cost_usd, output_tokens, note) VALUES(?,?,?,?,?,?,?,?)",
           ("worker", tid, now - 100, now - 50, "ok", 0.5, 4000, json.dumps({"output_trim": {"effort": "medium"}})))
    got = costlevers.status_lines(p, now)
    assert got and all(x.startswith("cost levers (7d): ") for x in got)
    text = "\n".join(got)
    assert "fresh continue [on]" in text and "landings:" in text
    assert "settled without a model (rule 1)" in text and "output trim [off]" in text
    assert "work 4.0k/$0.50" in text and "effort-picked 1 runs" in text
    assert costlevers.status_lines(p, now + 60) == got   # cached
    rep = costlevers.fresh_report(p, now - 86400, now)
    assert set(rep["landings"]) == {"done_code", "judged", "table_only"}
    out = audit.lines(p.db, now, 86400, 24, p)
    assert any("fresh continue" in x for x in out) and any("output trim" in x for x in out)
