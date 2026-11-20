# Cost lever tests of tests/test_tt_project_runtime.py (costlevers.py, wakesettle.py: the cost lever
# behind rings.coord_wake_settle, off by default), which runs this file
# in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file
# and the other case files (_roll, _batch_setup).
from __future__ import annotations

_LEVERS = ("coord_wake_settle",)


def _sh_setup(env, monkeypatch):
    p, d, clock, starts = _batch_setup(env, monkeypatch, busy=0)
    p.set_config("coordinator.batch_s", 0)
    p.set_config("coordinator.event_hold_s", 0)
    p.set_config("coordinator.plain_done_hold_max_s", 0)
    d.cfg = p.config()
    p.db.set_kv("last_coordinator_turn", clock[0] + 60)   # no idle or starve wake inside a test's ticks
    turns = []

    def turn(*a, **k):   # a turn that handles its events
        starts.append(clock[0])
        turns.append(k["note"]["events"])
        p.db.x("UPDATE events SET status='handled' WHERE id IN (%s)" % ",".join(map(str, turns[-1] or [0])))
    monkeypatch.setattr(d, "start_run", turn)
    return p, d, clock, starts, turns


def _ev_x(p, clock, kind, text, severity, source, task):
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
           (clock[0], source, kind, severity, text, None, "queued", task))
    return p.db.one("SELECT MAX(id) i FROM events")["i"]


def _drive(d, clock, starts, ticks=12):
    """Ticks 20 s apart until a turn starts."""
    n = len(starts)
    for _ in range(ticks):
        d.maybe_coordinate()
        clock[0] += 20
        if len(starts) > n:
            return True
    return False


def test_cost_levers_are_off_by_default_and_a_ring_or_the_projects_own_key_turns_each_on(env, capsys):
    from ttp import costlevers, rollout
    p = make(env)
    for flag in _LEVERS:
        assert flag in rollout.FLAGS
        assert not costlevers.lever_on(p, flag), flag
    # The scratch ring turns the lever on.
    assert _roll(capsys, "ring", "demo", "0")[0] == 0
    assert _roll(capsys, "set", "coord_wake_settle", "0")[0] == 0
    assert costlevers.lever_on(p, "coord_wake_settle")
    # The project's own explicit value wins over the ring, both ways.
    p.set_config("coordinator.wake_settle", False)
    assert not costlevers.lever_on(p, "coord_wake_settle")
    p.set_config("coordinator.wake_settle", True)
    assert costlevers.lever_on(p, "coord_wake_settle")


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
    up = _ev_x(p, clock, "upstream_note", "a note for the maintainers", "normal", "upstream", None)
    ids, line = wakesettle.settle(p.db, p.config(), _settle_evs(p, [up]), now, plain)
    assert ids == [up] and "settled by rule" in line
    assert p.db.one("SELECT status FROM events WHERE id=?", (up,))["status"] == "handled"
    # A batch with one event a turn must see settles nothing (all or nothing).
    up2 = _ev_x(p, clock, "upstream_note", "another note", "normal", "upstream", None)
    hot = _ev_x(p, clock, "observation", "box down", "high", "watcher:x", None)
    assert wakesettle.settle(p.db, p.config(), _settle_evs(p, [up2, hot]), now, plain) == ([], "")
    assert p.db.one("SELECT status FROM events WHERE id=?", (up2,))["status"] == "queued"
    # A task_done whose hand-off asks for a follow-up, or that is not plain, waits for a turn.
    tid = p.db.add_task("tidy docs", "tidy the docs", kind="work", origin="user")
    p.db.x("UPDATE tasks SET status='done', result=? WHERE id=?",
           (json.dumps({"status": "done", "summary": "ok", "followups": ["continue: the rest"]}), tid))
    done = _ev_x(p, clock, "task_done", f"#{tid} tidy docs → done: ok", "normal", f"task:{tid}", tid)
    assert wakesettle.settle(p.db, p.config(), _settle_evs(p, [done]), now, plain) == ([], "")
    p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps({"status": "done", "summary": "ok"}), tid))
    assert wakesettle.settle(p.db, p.config(), _settle_evs(p, [done]), now, lambda e: False) == ([], "")
    assert wakesettle.settle(p.db, p.config(), _settle_evs(p, [done]), now, plain)[0] == [done]
    # A re-check observation settles only once a turn met the same one.
    o1 = _ev_x(p, clock, "observation", "docs index is stale", "normal", "watcher:docs", None)
    assert wakesettle.settle(p.db, p.config(), _settle_evs(p, [o1]), now, plain) == ([], "")
    p.db.x("UPDATE events SET status='handled' WHERE id=?", (o1,))
    o2 = _ev_x(p, clock, "observation", "docs index is stale", "normal", "watcher:docs", None)
    assert wakesettle.settle(p.db, p.config(), _settle_evs(p, [o2]), now, plain)[0] == [o2]
    # A plain low-severity watcher observation is never held back by never_reason.
    obs = {"kind": "observation", "severity": "low", "source": "watcher:docs", "text": "docs index is stale"}
    assert wakesettle.never_reason(obs, None, set()) is None
    # A later turn handling the same task's events counts the settle as reopened.
    assert wakesettle.reopened(p.db, now - 60, now + 10) == (3, 0)
    _ev_x(p, clock, "task_failed", f"#{tid} broke", "normal", f"task:{tid}", tid)
    p.db.x("UPDATE events SET status='handled' WHERE task=? AND kind='task_failed'", (tid,))
    assert wakesettle.reopened(p.db, now - 60, now + 10) == (3, 1)
    assert "reopened 1/3" in wakesettle.line(p.db, now - 60, now + 10)


def test_wake_settle_flag_keeps_the_daemon_from_waking_on_a_routine_batch(env, monkeypatch):
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch)
    clock[0] += 60
    i = _ev_x(p, clock, "upstream_note", "a note for the maintainers", "normal", "upstream", None)
    assert _drive(d, clock, starts), "off by default: the batch wakes a turn"
    assert i in turns[-1]
    rows = p.db.q("SELECT gate, verb FROM wake_decisions")
    assert not [r for r in rows if r["gate"] == "rule"]
    p.set_config("coordinator.wake_settle", True)
    d.cfg = p.config()
    p.db.set_kv("last_coordinator_turn", clock[0] + 60)
    j = _ev_x(p, clock, "upstream_note", "a second note", "normal", "upstream", None)
    assert not _drive(d, clock, starts, ticks=4), "the rule settled it: no turn"
    assert p.db.one("SELECT status FROM events WHERE id=?", (j,))["status"] == "handled"
    assert p.db.one("SELECT COUNT(*) n FROM wake_decisions WHERE gate='rule' AND verb='skip'")["n"] == 1
    # A user message is never held by the rule.
    p.db.x("INSERT INTO messages(ts,direction,chat,kind,text,handled) VALUES(?,?,?,?,?,0)",
           (clock[0], "in", "web", "user", "status?"))
    _ev_x(p, clock, "upstream_note", "a third note", "normal", "upstream", None)
    assert _drive(d, clock, starts), "a user message wakes a turn"


def test_retired_output_trim_and_device_prewarm_leave_old_state_and_config_working(env, capsys):
    # cells: rollout_state, project_config
    from ttp import cli, rollout
    from ttp.project import unknown_key_hint
    p = make(env)
    for flag in ("output_trim", "device_prewarm"):
        assert flag not in rollout.FLAGS
        _ro_flag(flag, 1, 1)   # account state written before the flags were retired
    assert not rollout.on("output_trim", p.name)
    code, out = _roll(capsys, "status")
    assert code == 0 and "output_trim" not in out and "device_prewarm" not in out and "eta_history" in out
    assert _roll(capsys, "status", "--json")[0] == 0
    assert _roll(capsys, "set", "eta_history", "0")[0] == 0 and _roll(capsys, "status")[0] == 0
    raw = p.raw_config()
    raw["output"] = {"trim": True}
    p.config_path.write_text(json.dumps(raw))
    assert p.config()
    assert "output.trim is retired" in unknown_key_hint("output.trim")
    notes = cli._drop_retired_keys(p)
    assert len(notes) == 1 and "output.trim" in notes[0]
    assert not cli._config_has(p.raw_config(), "output.trim")


def test_cost_lever_metrics_show_in_status_and_the_daily_review(env, capsys):
    from ttp import audit, costlevers, wakesettle
    p = make(env)
    now = time.time()
    wakesettle.record(p.db, "rule", "skip", [{"id": 9, "kind": "upstream_note"}], now - 10)
    got = costlevers.status_lines(p, now)
    assert got and all(x.startswith("cost levers (7d): ") for x in got)
    assert "settled without a model (rule 1)" in "\n".join(got)
    assert costlevers.status_lines(p, now + 60) == got   # cached
    assert any("settled without a model" in x for x in audit.lines(p.db, now, 86400, 24, p))


def _wake_run(p, tools, cost=0.4, ago=100):
    """A finished coordinator run whose transcript holds `tools` tool calls; returns its id."""
    now = time.time()
    rid = p.db.x("INSERT INTO runs(role, started, ended, status, cost_usd, provider) VALUES(?,?,?,?,?,?)",
                 ("coordinator", now - ago, now - ago + 30, "ok", cost, "claude"))
    d = p.runs / str(rid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.jsonl").write_text("\n".join(json.dumps({"message": {"content": [
        {"type": "tool_use", "id": f"c{i}", "name": "Bash", "input": {}}]}}) for i in range(tools)) + "\n")
    return rid


def test_a_wake_of_8_tool_calls_steers_the_next_digest_only_with_the_lever_on(env):
    from ttp import costlevers, coordinator
    p = make(env)
    heavy, light = _wake_run(p, 8), _wake_run(p, 7)
    # Lever off: nothing is read or stored, and the digest carries no steer.
    costlevers.note_wake_tools(p, heavy, 8, time.time())
    assert p.db.kv(costlevers.STEER_KEY, None) is None and costlevers.steer_lines(p) == []
    p.set_config("coordinator.wake_settle", True)
    costlevers.note_wake_tools(p, light, 7, time.time())
    assert costlevers.steer_lines(p) == []
    costlevers.note_wake_tools(p, heavy, None, time.time())   # no transcript: unknown, not heavy
    assert costlevers.steer_lines(p) == []
    costlevers.note_wake_tools(p, heavy, 8, time.time())
    steer = costlevers.steer_lines(p)
    assert len(steer) == 1 and "8 tool calls" in steer[0] and "one shell call" in steer[0] and "worker" in steer[0]
    assert "## Tool calls" in dict(coordinator.digest_parts(p, {}, [], [])[0])["tool_steer"]
    costlevers.note_wake_tools(p, light, 7, time.time())      # a lighter wake clears it
    assert costlevers.steer_lines(p) == []
    costlevers.note_wake_tools(p, heavy, 9, time.time())
    p.set_config("coordinator.wake_settle", False)
    assert costlevers.steer_lines(p) == [] and "tool_steer" not in dict(coordinator.digest_parts(p, {}, [], [])[0])


def test_wake_tool_metric_counts_heavy_wakes_and_their_dollars_in_status_and_the_daily_review(env):
    from ttp import audit, costlevers
    p = make(env)
    now = time.time()
    assert costlevers.wake_tools_line(p, now - 7 * 86400, now) == ""
    _wake_run(p, 9, cost=1.5, ago=300)
    _wake_run(p, 8, cost=0.5, ago=200)
    _wake_run(p, 3, cost=0.25, ago=100)
    rep = costlevers.wake_tools_report(p, now - 7 * 86400, now)
    assert rep == {"wakes": 3, "heavy": 2, "usd": 2.25, "heavy_usd": 2.0, "max": 9}
    line = costlevers.wake_tools_line(p, now - 7 * 86400, now)
    assert line == "wake tools [off]: 2 of 3 coordinator wakes made 8+ tool calls ($2.00 of $2.25; most 9)"
    assert any("wake tools [off]" in x for x in costlevers.status_lines(p, now))
    assert any("wake tools [off]" in x for x in audit.lines(p.db, now, 86400, 24, p))


def test_the_daily_waste_check_runs_from_the_daemon_once_a_day_and_only_with_the_lever_on(env, monkeypatch):
    from ttp import daemon as dmod
    p = make(env)
    d = dmod.Daemon(p.base)
    calls = []
    monkeypatch.setattr(d, "_waste_ledger", lambda db: calls.append(1))
    d.check_daily_waste(0)
    assert calls == [], "off by default"
    p.set_config("coordinator.wake_settle", True)
    d.cfg = p.config()
    d.check_daily_waste(0)
    assert calls == [1]
    p.db.set_kv(dmod.WASTE_LEDGER_KV, time.time() - 3600)   # the ledger ran an hour ago (the review's own call counts)
    d.check_daily_waste(0)
    assert calls == [1]
    p.db.set_kv(dmod.WASTE_LEDGER_KV, time.time() - 90000)
    d.check_daily_waste(0)
    assert calls == [1, 1]
    d.check_daily_waste(600)   # throttled in memory
    assert calls == [1, 1]
    d._waste_ledger = dmod.Daemon._waste_ledger.__get__(d)
    d._waste_ledger(p.db)
    assert time.time() - float(p.db.kv(dmod.WASTE_LEDGER_KV, 0)) < 60


def test_pass_record_only_accepts_just_text_that_records_a_passed_probe(env):
    from ttp import prompts, wakesettle
    base = "Goal: ship the change.\nWhere: runtime/ttp/x.py"
    seen = prompts.digest_of(base, "t")
    ok = lambda spec, n=len(base), s=seen: wakesettle.pass_record_only(spec, s, n, "t")   # noqa: E731
    assert ok(base) and ok(base + "\n\n")
    assert ok(base + "\n\n## Update\nUpdate: its start_when probe passed.\n")
    assert ok(base + "\n\n## Update 2026-11-17 19:32 PST (turn t12)\nprobe passed\n")
    assert ok(base + "\n\n## Update\nretry_when passed\n\n## Update\nthe probe is met\n")
    for bad in ("\n\n## Update\nIts probe passed, but do not push; user msg #12 changes the goal.\n",
                "\n\n## Update\nUse the new approach from the review.\n",
                "\n\n## Update\nprobe passed\nline 2\nline 3\nline 4\n",
                "\n\nA second goal line.\n",
                "\n\n## Update\nretry_when passed\n\nand also: rename x\n",
                "\n\n## Update\n## Update: cancel the push and start from scratch",   # text on a header line
                "\n\n## Update: probe passed\nprobe passed\n",
                "\n\n## Update 2026-11-17 19:32 PST cancel the push\nprobe passed\n",
                "\n\n## Update\n",                                                     # an empty block
                "\n\n## Update\n\n\n",
                "\n\n## Update\nprobe passed\n\n## Update\n"):
        assert not ok(base + bad), bad
    assert not ok("Goal: other.\nWhere: runtime/ttp/x.py\n\n## Update\nprobe passed")   # the spec itself changed
    assert not wakesettle.pass_record_only(base + "\n\n## Update\nprobe passed", seen, None, "t")
    assert not wakesettle.pass_record_only(base + "\n\n## Update\nprobe passed", seen, len(base) + 5, "t")
    assert not wakesettle.pass_record_only(base + "\n\n## Update\nprobe passed", seen, True, "t")


def _waiting_on_probe(p, spec):
    from ttp import prompts
    from ttp.daemon import SPEC_SEEN_KEY, SPEC_SEEN_LEN_KEY
    tid = p.db.add_task("wait", spec, kind="work", origin="user")
    t = p.db.task(tid)
    res = {"status": "waiting", "summary": "waiting", "retry_when": "true",
           SPEC_SEEN_KEY: prompts.spec_digest(t), SPEC_SEEN_LEN_KEY: len(prompts.spec_base(t))}
    p.db.update_task(tid, status="waiting", result=json.dumps(res))
    return tid


def test_a_spec_edit_that_only_records_the_probe_pass_wakes_no_model_with_the_lever_on(env, monkeypatch):
    from ttp import prompts
    from ttp.daemon import Daemon, SPEC_SEEN_KEY, SPEC_SEEN_LEN_KEY
    p = make(env)
    d = Daemon(p.base)
    woke = []
    monkeypatch.setattr(d, "_wake_waiting", lambda task, why, now, **k: woke.append((why, k.get("kind"))))
    now = time.time()

    def edit(tid, text):
        p.db.update_task(tid, spec=p.db.task(tid)["spec"] + "\n\n## Update\n" + text + "\n")

    # Lever off (the default): the edit is a spec change and wakes a run with the change.
    t1 = _waiting_on_probe(p, "Goal: wait for the deploy.")
    edit(t1, "Update: its retry_when probe passed.")
    d._probe_passed(p.db.task(t1), now)
    assert [k for _, k in woke] == ["spec_change"]
    assert p.db.one("SELECT COUNT(*) n FROM wake_decisions WHERE gate='probe'")["n"] == 0
    woke.clear()
    p.set_config("coordinator.wake_settle", True)
    d.cfg = p.config()
    # Lever on: the same edit is recorded as the new baseline and settled without the spec-change wake.
    d._probe_passed(p.db.task(t1), now)
    assert "spec_change" not in [k for _, k in woke]
    res = json.loads(p.db.task(t1)["result"])
    assert res[SPEC_SEEN_KEY] == prompts.spec_digest(p.db.task(t1))
    assert res[SPEC_SEEN_LEN_KEY] == len(prompts.spec_base(p.db.task(t1)))
    row = p.db.one("SELECT verb, usd FROM wake_decisions WHERE gate='probe'")
    assert row["verb"] == "skip" and row["usd"] == 0
    woke.clear()
    # An edit that says more than the pass still wakes a run with the change.
    t2 = _waiting_on_probe(p, "Goal: wait for the deploy.")
    edit(t2, "Its probe passed. Also change the goal to the other branch.")
    d._probe_passed(p.db.task(t2), now)
    assert [k for _, k in woke] == ["spec_change"]
    assert json.loads(p.db.task(t2)["result"])[SPEC_SEEN_KEY] != prompts.spec_digest(p.db.task(t2)), "unsettled"
