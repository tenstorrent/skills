# Cost-basis tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new tests of how a run's cost was known here, not there.
from __future__ import annotations


def _basis_run(d, provider, lines, *, budget_usd=None, timeout_s=None, note=None):
    """_finished_run, with a run.json carrying the run's budget and timeout (what _cut_off_cost reads)."""
    db = d.p.db
    tid = db.add_task("build", "", kind="work", tier="standard", priority=3, budget_usd=8.0, labels=[])
    db.update_task(tid, status="running")
    rid = db.x("INSERT INTO runs(task,role,provider,model,started,status,boot_id,note) "
               "VALUES(?,'worker',?,'opus',?,'running',?,?)",
               (tid, provider, time.time() - 600, d.boot, json.dumps(note or {})))
    run_dir = d.p.runs / str(rid)
    run_dir.mkdir(parents=True)
    db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
    (run_dir / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    (run_dir / "stderr.log").write_text("")
    if budget_usd is not None:
        (run_dir / "run.json").write_text(json.dumps({"budget_usd": budget_usd, "timeout_s": timeout_s, "argv": []}))
    d.finish_run(db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                 {"rc": 0, "started": time.time() - 600, "ended": time.time(), "stopped": None, "slept_s": 0})
    return db.one("SELECT * FROM runs WHERE id=?", (rid,))


def test_a_claude_run_with_a_reported_cost_is_booked_as_reported(env):
    from ttp.daemon import Daemon
    run = _basis_run(Daemon(make(env).base), "claude", OK_RUNS["claude"])
    assert run["cost_usd"] == pytest.approx(0.4) and run["cost_basis"] == "reported", dict(run)


def test_a_codex_run_priced_from_its_tokens_is_booked_as_tokens(env):
    from ttp.daemon import Daemon
    run = _basis_run(Daemon(make(env).base), "codex", OK_RUNS["codex"], budget_usd=4.0, timeout_s=1200)
    assert run["cost_usd"] > 0 and run["cost_estimated"] == 1 and run["cost_basis"] == "tokens", dict(run)
    assert run["cost_usd"] != pytest.approx(2.0), "priced from its tokens, not the elapsed half of its budget"


def test_a_run_with_no_usage_is_booked_as_an_elapsed_guess(env):
    """Cursor's result carries no usage: the daemon books the elapsed share of the budget, and says so."""
    from ttp.daemon import Daemon
    lines = [{"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "c1"}]
    run = _basis_run(Daemon(make(env).base), "cursor", lines, budget_usd=4.0, timeout_s=1200)
    assert run["cost_usd"] == pytest.approx(2.0, rel=0.05) and run["cost_basis"] == "elapsed", dict(run)


def test_a_cut_off_run_that_booked_nothing_is_not_an_elapsed_guess(env):
    """With no budget to take a share of, _cut_off_cost books $0: that is no guess."""
    from ttp.daemon import Daemon
    lines = [{"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "c1"}]
    run = _basis_run(Daemon(make(env).base), "cursor", lines)
    assert run["cost_usd"] == 0 and run["cost_basis"] == "tokens", dict(run)


def test_cost_basis_follows_how_the_provider_knew_the_figure():
    from ttp.daemon import _cost_basis
    from ttp.providers.base import RunUsage
    assert _cost_basis(RunUsage(cost_usd=0.3)) == "reported"
    assert _cost_basis(RunUsage(cost_usd=0.0)) == "reported", "a CLI that reported $0 still reported it"
    assert _cost_basis(RunUsage(estimated=True, input_tokens=5)) == "tokens"
    assert _cost_basis(RunUsage(estimated=True, cost_usd=0.2)) == "tokens", "a provider that priced its own tokens"
    priced = RunUsage(cost_usd=0.2)
    priced.extra["cost_basis"] = "pricing"   # a chat run repriced from its tokens at pricing.chat
    assert _cost_basis(priced) == "tokens"
    fallback = RunUsage(estimated=True, cost_usd=0.2)
    fallback.extra["cost_basis"] = "fallback"   # Claude Code's own figure, booked as estimated
    assert _cost_basis(fallback) == "reported"
    fallback.cost_usd = 0.0
    assert _cost_basis(fallback) == "tokens", "nothing reported: the daemon prices its tokens"


def test_a_routed_run_is_booked_as_tokens(env):
    from ttp import route
    from ttp.daemon import Daemon
    p = make(env)
    run = _basis_run(Daemon(p.base), "claude", OK_RUNS["claude"], note={route.KEY: {"to": "claude"}})
    assert run["cost_basis"] == "tokens", dict(run)


def test_the_cost_basis_migration_keeps_old_runs(env, tmp_path):
    import sqlite3
    from ttp.db import DB
    path = tmp_path / "old.db"
    DB(path).conn.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE runs DROP COLUMN cost_basis")
    conn.execute("INSERT INTO runs(role,provider,ended,status,cost_usd,cost_estimated) VALUES('worker','cursor',1,'ok',1.5,1)")
    conn.commit()
    conn.close()
    db = DB(path)
    rows = db.q("SELECT provider, ended, cost_usd, cost_estimated, cost_basis FROM runs")
    assert rows == [{"provider": "cursor", "ended": 1, "cost_usd": 1.5, "cost_estimated": 1, "cost_basis": None}], rows
    DB(path)   # a second open finds the column and changes nothing
    from ttp import audit
    assert audit.cost_bases(db, since=0) == [{"provider": "cursor", "basis": "unknown", "n": 1, "usd": 1.5}]


def _done_with_runs(p, now, runs):
    t = p.db.add_task("t", "s", kind="code", tier="standard", origin="user")
    p.db.update_task(t, status="done", spent_usd=sum(usd for _, _, usd in runs))
    for prov, basis, usd in runs:
        p.db.x("INSERT INTO runs(role,task,provider,started,ended,status,cost_usd,cost_basis) "
               "VALUES('worker',?,?,?,?,'ok',?,?)", (t, prov, now - 60, now, usd, basis))
    return t


def test_the_daily_review_names_spend_per_provider_and_basis_and_the_elapsed_share(env):
    from ttp import audit
    p = make(env)
    now = time.time()
    _done_with_runs(p, now, [("claude", "reported", 3.0), ("claude", "reported", 1.0), ("cursor", "elapsed", 0.4)])
    _done_with_runs(p, now, [("codex", "tokens", 0.75), ("codex", "elapsed", 0.25), ("claude", None, 0.5)])
    failed = p.db.add_task("f", "s", kind="code", tier="standard", origin="user")
    p.db.update_task(failed, status="failed", spent_usd=9.0)
    p.db.x("INSERT INTO runs(role,task,provider,started,ended,status,cost_usd,cost_basis) "
           "VALUES('worker',?,'cursor',?,?,'failed',9.0,'elapsed')", (failed, now - 60, now))
    bases = audit.cost_bases(p.db, audit.finished(p.db, now - 86400))
    assert [(g["provider"], g["basis"], g["n"]) for g in bases] == [
        ("claude", "reported", 2), ("codex", "tokens", 1), ("claude", "unknown", 1), ("cursor", "elapsed", 1),
        ("codex", "elapsed", 1)], "a failed task's runs are no outcome"
    lines = "\n".join(audit.lines(p.db, now))
    assert ("$ per done task by provider and cost basis: claude reported 2 runs $4.00, codex tokens 1 run $0.75, "
            "claude unknown 1 run $0.50, cursor elapsed 1 run $0.40, codex elapsed 1 run $0.25; "
            "elapsed guess: codex 25% of $1.00, cursor 100% of $0.40") in lines, lines
    assert audit.report(p.db, now)["cost_bases"] == bases


def test_the_daily_review_says_when_no_dollars_were_guessed(env):
    from ttp import audit
    p = make(env)
    now = time.time()
    _done_with_runs(p, now, [("claude", "reported", 1.0)])
    lines = "\n".join(audit.lines(p.db, now))
    assert "cost basis: claude reported 1 run $1.00; elapsed guess: none" in lines, lines


def test_the_web_spend_view_shows_runs_by_cost_basis(env):
    from ttp.web import health
    p = make(env)
    now = time.time()
    _done_with_runs(p, now, [("claude", "reported", 1.0), ("cursor", "elapsed", 0.5)])
    p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd,cost_basis) "
           "VALUES('worker','cursor',?,?,'ok',7.0,'elapsed')", (now - 9 * 86400, now - 8 * 86400))
    spend = health(p, p.db)["spend"]
    assert spend["bases_line"] == ("claude reported 1 run $1.00, cursor elapsed 1 run $0.50; "
                                   "elapsed guess: cursor 100% of $0.50"), "a run older than 7 days is left out"
    assert {(g["provider"], g["basis"]) for g in spend["bases_7d"]} == {("claude", "reported"), ("cursor", "elapsed")}


def test_a_running_run_metered_from_its_stream_carries_its_basis(env):
    """A reap that fails books the metered figure as it stands: its basis is already on the row."""
    from ttp.daemon import Daemon
    p = make(env)
    run_dir = p.runs / "meter"
    run_dir.mkdir(parents=True)
    msg = {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 1000, "output_tokens": 10},
                                            "content": [{"type": "text", "text": "building"}]}}
    (run_dir / "output.jsonl").write_text(json.dumps(msg) + "\n")
    rid = p.db.x("INSERT INTO runs(role,provider,model,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
                 ("worker", "claude", "opus", time.time(), "running", str(run_dir), "x"))
    Daemon(p.base).meter_running()
    row = p.db.one("SELECT cost_usd, cost_basis FROM runs WHERE id=?", (rid,))
    assert row["cost_usd"] > 0 and row["cost_basis"] == "tokens", row


def test_a_cut_off_codex_run_priced_from_its_rollout_tokens_is_booked_as_tokens(env):
    """A Codex run cut off mid-turn: its cost is priced from its rollout's running token counts
    (_priced_from_tokens), so its basis is "tokens", never the elapsed-time "elapsed" guess."""
    from ttp.daemon import Daemon, _priced_from_tokens
    from ttp.providers import get_provider
    home = env["tmp"] / "codex-home"
    (home / "sessions" / "2026" / "11" / "01").mkdir(parents=True)
    started = time.time() - 60
    stamp = lambda off: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(started + off))

    def _count(ts, inp, cached, out):
        usage = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}
        return {"timestamp": ts, "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": usage}}}

    counts = [_count(stamp(-5), 1000, 0, 200), _count(stamp(30), 6000, 2000, 500)]
    (home / "sessions" / "2026" / "11" / "01" / "rollout-2026-11-01T10-00-00-0199eeee-ffff.jsonl").write_text(
        "".join(json.dumps(rec) + "\n" for rec in counts))
    # no turn.completed: parse leaves the run unpriced (estimated, no cost), so finish_run prices it
    lines = [{"type": "thread.started", "thread_id": "0199eeee-ffff"},
             {"type": "item.completed", "item": {"type": "agent_message", "text": "half done"}}]
    p = make(env)
    p.set_config("pricing", {"codex": {"opus": [1.0, 0.1, 2.0]}})
    run = _basis_run(Daemon(p.base), "codex", lines, budget_usd=4.0, timeout_s=1200)
    assert run["cost_basis"] == "elapsed", dict(run)   # without its rollout it is an elapsed guess
    run_dir = Path(run["dir"])
    spec = json.loads((run_dir / "run.json").read_text())
    spec["env"] = {"CODEX_HOME": str(home)}
    (run_dir / "run.json").write_text(json.dumps(spec))
    exit_info = {"rc": 0, "started": started, "ended": time.time(), "stopped": "budget", "slept_s": 0}
    prov = get_provider("codex").use(run["model"], {"opus": [1.0, 0.1, 2.0]})
    priced = _priced_from_tokens(run_dir, exit_info, None, prov)
    assert priced and priced > 0, "the rollout's token counts should price the cut-off run"
    # the rollout's running totals cover the whole session: the count before the run's start is
    # subtracted as the base, so the run is priced at last - base = (5000, 2000, 300)
    assert priced == pytest.approx((5000 - 2000) * 1.0 / 1e6 + 2000 * 0.1 / 1e6 + 300 * 2.0 / 1e6), priced
    p.db.x("UPDATE runs SET status='running', cost_usd=0, cost_estimated=0, cost_basis=NULL WHERE id=?",
            (run["id"],))
    Daemon(p.base).finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (run["id"],)), exit_info)
    run = p.db.one("SELECT * FROM runs WHERE id=?", (run["id"],))
    assert run["cost_estimated"] == 1 and run["cost_basis"] == "tokens", dict(run)
    assert run["cost_usd"] == pytest.approx(priced), dict(run)
    assert run["cost_usd"] != pytest.approx(2.0), "priced from its tokens, not the elapsed half of its budget"


# A Claude run's own tokens priced at the model its result names, never the alias it asked for -------

def _alias_lines(model_usage, init="claude-opus-5-5", out_tokens=20_000, total=12.0):
    """A Claude stream: init on `init`, a result reporting `total` and naming `model_usage` ids."""
    return [{"type": "system", "subtype": "init", "session_id": "F1", "model": init},
            {"type": "result", "subtype": "success", "total_cost_usd": total, "result": "ok",
             "usage": {"input_tokens": 0, "output_tokens": out_tokens},
             "modelUsage": {m: {"outputTokens": out_tokens - i, "inputTokens": 0} for i, m in enumerate(model_usage)}}]


def test_an_opus_alias_fork_books_the_rates_of_the_model_its_result_names(env):
    """A run that asked for 'opus' (no price row) books its own tokens at the row of the model its
    result's modelUsage names, not the dearest row; an id the table lacks keeps the dearest row and is
    told once."""
    p = make(env)
    from ttp import localspend
    from ttp.daemon import Daemon
    from ttp.providers import get_provider
    d = Daemon(p.base)
    _, r = _fork_run(d)
    d.p.db.x("UPDATE runs SET model='opus' WHERE id=?", (r["id"],))
    r = d.p.db.one("SELECT * FROM runs WHERE id=?", (r["id"],))
    out = d.p.runs / "alias.jsonl"
    out.write_text("".join(json.dumps(x) + "\n" for x in _alias_lines(["claude-opus-5-5", "claude-haiku-5-5"])))
    u = get_provider("claude").parse(out)
    assert u.extra["model_usage"] == ["claude-opus-5-5", "claude-haiku-5-5"]
    want = 20_000 * 20.0 / 1e6   # Opus 5.5 output rate, not Opus 4's $75
    assert abs(d._net_of_session(r, u) - want) < 1e-6, "the alias was priced at the dearest row"
    # the session's own model wins over a more used subagent model; with no modelUsage the init model
    u = _reported(12.0, output_tokens=20_000)
    u.extra.update(model_usage=["claude-haiku-5-5", "claude-opus-5-5"], model="claude-opus-5-5")
    assert abs(d._net_of_session(r, u) - want) < 1e-6
    u = _reported(12.0, output_tokens=20_000)
    u.extra.update(model="claude-opus-5-5")
    assert abs(d._net_of_session(r, u) - want) < 1e-6
    # a prompt under the 100K long-prompt tier: the base Haiku 5.5 row
    usd, est = localspend.price("claude-haiku-5-5", {"input_tokens": 60_000, "output_tokens": 1_000_000,
                                                    "cache_read_input_tokens": 30_000})
    assert not est and abs(usd - 0.5063) < 1e-9
    for _ in range(2):
        u = _reported(30.0, output_tokens=20_000)
        u.extra.update(model_usage=["claude-unheard-9"])
        d._net_of_session(r, u)
    told = d.p.db.q("SELECT * FROM messages WHERE kind='alert' AND text LIKE '%claude-unheard-9%'")
    assert len(told) == 1 and told[0]["severity"] == "low"


def test_alias_priced_fork_rows_are_re_booked_once_at_their_runs_time(env):
    p = make(env)
    import datetime
    from ttp import daemon as dmod, timefmt
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    db = d.p.db
    at = datetime.datetime(2026, 11, 8, 10, 0, tzinfo=timefmt.zone(p)).timestamp()
    wrong, right = 20_000 * 75.0 / 1e6, 20_000 * 20.0 / 1e6

    def booked(lines, ended=at, renet=True, model_used=None):
        tid, r = _fork_run(d, cost=wrong, out_tokens=20_000, status="done")
        note = json.loads(r["note"])
        if renet:   # re-booked by the 0.2.978 fork re-net, at the dearest row
            note.update(session_cost_usd=12.0, fork_base_usd=12.0 - wrong)
        if model_used:
            note["model_used"] = model_used
        db.x("UPDATE runs SET model='opus', ended=?, note=? WHERE id=?", (ended, json.dumps(note), r["id"]))
        run_dir = d.p.runs / str(r["id"])
        run_dir.mkdir(parents=True)
        (run_dir / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        return tid, r["id"]

    no_model = [{"type": "system", "subtype": "init", "session_id": "F1"}]
    tid, fixed = booked(_alias_lines(["claude-opus-5-5"]))
    _, init_only = booked(_alias_lines([])[:1], renet=False)   # 'opus' asked, the init event names claude-opus-5-5
    _, noted = booked(no_model, model_used="claude-opus-5-5")   # only the note's model_used names it
    _, renet_old = booked(_alias_lines(["claude-opus-5-5"]), ended=at - 3 * 86400)   # re-net row before the window
    _, bare = booked(no_model)   # names no model: keeps its booking
    _, old = booked(_alias_lines(["claude-opus-5-5"]), ended=at - 3 * 86400, renet=False)   # before the window
    changed = d.reprice_alias_runs()
    assert [c[0] for c in changed] == [fixed, init_only, noted, renet_old]
    assert all(abs(c[2] - right) < 1e-6 for c in changed), changed
    row = db.one("SELECT cost_usd, note FROM runs WHERE id=?", (fixed,))
    note = json.loads(row["note"])
    assert abs(row["cost_usd"] - right) < 1e-6 and note["repriced_model"] == "claude-opus-5-5"
    assert abs(note["fork_base_usd"] - (12.0 - right)) < 1e-6
    assert abs(db.task(tid)["spent_usd"] - right) < 1e-6, "the task kept the phantom spend"
    fix = db.q("SELECT ts, usd FROM ledger WHERE usd<0 ORDER BY id")
    assert [f["ts"] for f in fix] == [at, at, at, at - 3 * 86400], "a correction not at its run's own time"
    assert all(abs(f["usd"] + (wrong - right)) < 1e-6 for f in fix)
    for rid in (bare, old):
        assert abs(db.one("SELECT cost_usd FROM runs WHERE id=?", (rid,))["cost_usd"] - wrong) < 1e-9
    assert db.kv(dmod.MODEL_REPRICE_KEY)["runs"] == 4
    assert d.reprice_alias_runs() is None, "a second pass ran again"
    db.set_kv(dmod.MODEL_REPRICE_KEY, None)
    assert d.reprice_alias_runs() == [], "a re-priced row was re-booked again"
    assert len(db.q("SELECT 1 FROM ledger WHERE usd<0")) == 4


# A CLI logged out or offline: it starts a turn, fails to reach its API and retries until the timeout.
CODEX_NO_CALL = [{"type": "thread.started", "thread_id": "t1"}, {"type": "turn.started"},
                 {"type": "error", "message": "Reconnecting... 1/5 (stream disconnected before completion)"},
                 {"type": "error", "message": "stream disconnected before completion: error sending request"}]


def _timed_out_run(d, provider, lines, *, took=2464.0):
    """A worker run of `provider` cut off at its 2464 s timeout with a $3.59 budget (a logged-out case)."""
    db = d.p.db
    tid = db.add_task("build", "", kind="work", tier="standard", priority=3, budget_usd=8.0, labels=[])
    db.update_task(tid, status="running")
    rid = db.x("INSERT INTO runs(task,role,provider,model,started,status,boot_id) "
               "VALUES(?,'worker',?,'gpt-5',?,'running',?)", (tid, provider, time.time() - took, d.boot))
    run_dir = d.p.runs / str(rid)
    run_dir.mkdir(parents=True)
    db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
    (run_dir / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    (run_dir / "stderr.log").write_text("")
    (run_dir / "run.json").write_text(json.dumps({"budget_usd": 3.59, "timeout_s": took, "argv": []}))
    d.finish_run(db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                 {"rc": None, "started": time.time() - took, "ended": time.time(), "stopped": "timeout",
                  "slept_s": 0})
    return db.one("SELECT * FROM runs WHERE id=?", (rid,))


def test_a_timed_out_run_that_made_no_model_call_books_0_and_counts_toward_no_cap(env):
    """The logged-out case: Codex logged out with no internet timed out after 2464 s with 0 tokens and was
    booked the elapsed share of its budget ($3.59) as spend. It made no call: $0, no cap moves."""
    from ttp.daemon import Daemon
    d = Daemon(make(env).base)
    t0 = time.time() - 3600
    run = _timed_out_run(d, "codex", CODEX_NO_CALL)
    assert run["status"] == "timeout" and run["cost_usd"] == 0, dict(run)
    assert d.p.db.task(run["task"])["spent_usd"] == 0
    assert d.p.db.spent_since(t0) == 0 and d.p.db.spent_since(t0, billed=True) == 0
    # The same cut-off run that got an answer keeps the elapsed estimate: it did spend.
    answered = CODEX_NO_CALL[:2] + [{"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}]
    run = _timed_out_run(d, "codex", answered)
    assert run["cost_usd"] == pytest.approx(3.59, rel=0.05) and run["cost_estimated"] == 1, dict(run)


@pytest.mark.parametrize("lines,called", [
    ([], False),
    (CODEX_NO_CALL, False),
    ([{"type": "turn.failed", "error": {"message": "401 Unauthorized"}}], False),
    ([{"id": "0", "msg": {"type": "task_started"}}, {"id": "0", "msg": {"type": "stream_error"}},
      {"id": "0", "msg": {"type": "token_count", "info": None}}], False),
    ([{"type": "system", "subtype": "init"},
      {"type": "assistant", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Not logged in"}]}},
      {"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}], False),
    ([{"type": "assistant", "message": {"id": "m1", "model": "opus"}}], True),
    ([{"type": "item.completed", "item": {"type": "reasoning"}}], True),
    ([{"type": "turn.completed", "usage": {"input_tokens": 0}}], True),
    ([{"id": "0", "msg": {"type": "agent_message", "message": "hi"}}], True),
    ([{"type": "result", "subtype": "success", "is_error": False, "result": "done"}], True),
    ([{"type": "something_new"}], True),   # an unknown stream keeps its estimate
])
def test_model_called_reads_a_call_from_any_providers_stream(tmp_path, lines, called):
    from ttp.daemon import _model_called
    out = tmp_path / "output.jsonl"
    out.write_text("not json\n" + "".join(json.dumps(x) + "\n" for x in lines))
    assert _model_called(out) is called
    assert _model_called(tmp_path / "missing.jsonl") is False


@pytest.mark.parametrize("lines,called", [
    ([{"type": "something_new"}], False),   # an unknown event type is no evidence the run reached the API
    ([{"type": "rate_limit_event"}, {"type": "system", "subtype": "init"}], False),
    ([{"type": "tool_call", "subtype": "started"}], True),
    ([{"type": "stream_event", "event": {"type": "message_start"}}], True),
    ([{"type": "assistant", "message": {"id": "m1", "model": "opus"}}], True),
])
def test_model_called_strict_counts_only_an_answer_from_the_api(tmp_path, lines, called):
    from ttp.daemon import _model_called
    out = tmp_path / "output.jsonl"
    out.write_text("".join(json.dumps(x) + "\n" for x in lines))
    assert _model_called(out, strict=True) is called


@pytest.mark.parametrize("provider,lines", [
    ("codex", CODEX_NO_CALL),
    ("claude", [{"type": "system", "subtype": "init"},
                {"type": "result", "is_error": True, "result": "API Error: Can't reach the API server (EAI_AGAIN)"}]),
    ("cursor", [{"type": "system", "subtype": "init"}, {"type": "something_new"}]),
    ("cursor", []),
])
def test_a_lost_run_with_no_provider_result_books_0_on_every_provider(env, provider, lines):
    """A run that never reached the API (no usage, no answer in its output) books $0, whatever its
    provider, whether it timed out or was lost: no estimate without evidence it called a model."""
    from ttp.daemon import Daemon
    d = Daemon(make(env).base)
    t0 = time.time() - 3600
    run = _timed_out_run(d, provider, lines)
    assert run["cost_usd"] == 0, (provider, dict(run))
    assert d.p.db.task(run["task"])["spent_usd"] == 0 and d.p.db.spent_since(t0) == 0


def test_the_no_usage_rebook_takes_back_estimates_with_no_evidence_once(env):
    from ttp.daemon import CALLS_REBOOK_DAYS, NO_CALL_REBOOK_KEY, NO_USAGE_REBOOK_KEY, Daemon
    d = Daemon(make(env).base)
    db = d.p.db
    now = time.time()

    def booked(provider, lines, ended, usd=2.5):
        tid = db.add_task("t", "", kind="work", tier="standard", priority=3, budget_usd=8.0, labels=[])
        rid = db.x("INSERT INTO runs(task,role,provider,started,ended,status,cost_usd,cost_estimated) "
                   "VALUES(?,'worker',?,?,?,'lost',?,1)", (tid, provider, ended - 900, ended, usd))
        run_dir = d.p.runs / str(rid)
        run_dir.mkdir(parents=True)
        db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
        if lines is not None:
            (run_dir / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        db.x("UPDATE tasks SET spent_usd=? WHERE id=?", (usd, tid))
        db.spend(provider, usd, "t", estimated=True, ts=ended)
        return rid, tid

    unknown, unknown_task = booked("cursor", [{"type": "something_new"}], now - 3600)
    gone, _ = booked("claude", None, now - 7200)   # output removed: no evidence either
    called, _ = booked("codex", [{"type": "item.completed", "item": {"type": "agent_message"}}], now - 3600)
    old, _ = booked("cursor", [], now - (CALLS_REBOOK_DAYS + 1) * 86400)
    since = now - 2 * 86400
    assert db.spent_since(since) == pytest.approx(7.5)
    d.rebook_no_usage_step()
    assert db.kv(NO_USAGE_REBOOK_KEY) is None, "it waits for the no-call re-book"
    assert d.rebook_no_usage(dry_run=True) == [(unknown, 2.5), (gone, 2.5)]
    assert db.spent_since(since) == pytest.approx(7.5) and db.kv(NO_USAGE_REBOOK_KEY) is None
    db.set_kv(NO_CALL_REBOOK_KEY, {"at": now})
    d.rebook_no_usage_step()
    assert db.spent_since(since) == pytest.approx(2.5), "only the run that called a model still counts"
    assert json.loads(db.one("SELECT note FROM runs WHERE id=?", (unknown,))["note"])["no_usage_rebooked"] == 2.5
    assert db.task(unknown_task)["spent_usd"] == 0
    assert [r["ts"] for r in db.q("SELECT ts FROM ledger WHERE usd<0 ORDER BY ts")] == \
        [pytest.approx(now - 7200), pytest.approx(now - 3600)], "taken back at each run's end"
    assert db.one("SELECT cost_usd FROM runs WHERE id=?", (called,))["cost_usd"] == 2.5
    assert db.one("SELECT cost_usd FROM runs WHERE id=?", (old,))["cost_usd"] == 2.5
    mark = db.kv(NO_USAGE_REBOOK_KEY)
    assert mark["runs"] == 2 and mark["usd_before"] - mark["usd_after"] == pytest.approx(5.0)
    assert d.rebook_no_usage() is None, "it runs once"
    assert Daemon(d.p.base).rebook_no_usage() is None and db.spent_since(since) == pytest.approx(2.5)


def test_the_no_call_rebook_takes_back_past_estimates_with_signed_rows_once(env):
    from ttp.daemon import CALLS_REBOOK_DAYS, Daemon
    d = Daemon(make(env).base)
    db = d.p.db
    now = time.time()

    def booked(lines, ended):
        tid = db.add_task("t", "", kind="work", tier="standard", priority=3, budget_usd=8.0, labels=[])
        rid = db.x("INSERT INTO runs(task,role,provider,started,ended,status,cost_usd,cost_estimated) "
                   "VALUES(?,'coordinator','codex',?,?,'timeout',3.59,1)", (tid, ended - 2464, ended))
        run_dir = d.p.runs / str(rid)
        run_dir.mkdir(parents=True)
        db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
        (run_dir / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        db.x("UPDATE tasks SET spent_usd=3.59 WHERE id=?", (tid,))
        db.spend("codex", 3.59, "t", estimated=True, ts=ended)
        return rid, tid

    wrong, wrong_task = booked(CODEX_NO_CALL, now - 86400)
    called, _ = booked([{"type": "item.completed", "item": {"type": "agent_message"}}], now - 86400)
    old, _ = booked(CODEX_NO_CALL, now - (CALLS_REBOOK_DAYS + 1) * 86400)
    since = now - 2 * 86400
    assert db.spent_since(since) == pytest.approx(7.18)
    assert d.rebook_no_call(dry_run=True) == [(wrong, 3.59)] and db.spent_since(since) == pytest.approx(7.18)
    assert d.rebook_no_call() == [(wrong, 3.59)]
    assert db.one("SELECT cost_usd FROM runs WHERE id=?", (wrong,))["cost_usd"] == 0
    assert json.loads(db.one("SELECT note FROM runs WHERE id=?", (wrong,))["note"])["no_call_rebooked"] == 3.59
    assert db.task(wrong_task)["spent_usd"] == 0
    assert db.one("SELECT usd FROM ledger WHERE usd<0")["usd"] == pytest.approx(-3.59)
    assert db.spent_since(since) == pytest.approx(3.59), "only the run that called a model still counts"
    assert db.one("SELECT cost_usd FROM runs WHERE id=?", (called,))["cost_usd"] == 3.59
    assert db.one("SELECT cost_usd FROM runs WHERE id=?", (old,))["cost_usd"] == 3.59
    assert d.rebook_no_call() is None, "it runs once"


# A forked run's subagents (Agent or Task tool) book with it, each call at its own model ---------------

def _sub_msg(mid, model, parent=None, **usage):
    ev = {"type": "assistant", "message": {"id": mid, "model": model, "content": [{"type": "text", "text": "x"}],
                                           "usage": {k: usage.get(k, 0) for k in (
                                               "input_tokens", "output_tokens", "cache_read_input_tokens",
                                               "cache_creation_input_tokens")}}}
    if parent:
        ev["parent_tool_use_id"] = parent
    return ev


def test_a_forked_run_books_its_subagent_calls_at_each_ones_model(env):
    """The result's usage counts the main thread only (Claude Code 2.1.285 and 2.1.296 alike); its
    total_cost_usd and modelUsage carry the parent session's cost too. The stream shows only some of a
    subagent's messages, with partial counts; the session's subagent transcripts, in the run's own config
    directory, hold them all. Each message books once, at its fullest reading and its own model."""
    p = make(env)
    from ttp import localspend
    from ttp.daemon import Daemon
    from ttp.providers import get_provider
    d = Daemon(p.base)
    _, r = _fork_run(d)
    run_dir = d.p.runs / str(r["id"])
    run_dir.mkdir(parents=True)
    cfg = p.base / "agent-config"
    (run_dir / "run.json").write_text(json.dumps({"env": {"CLAUDE_CONFIG_DIR": str(cfg)}, "argv": []}))
    lines = [{"type": "system", "subtype": "init", "session_id": "F1", "model": "claude-opus-5-5"},
             _sub_msg("m-main", "claude-opus-5-5", output_tokens=900),
             {"type": "assistant", "message": {"id": "m-main2", "model": "claude-opus-5-5", "content": [
                 {"type": "tool_use", "id": "toolu_A", "name": "Agent", "input": {"prompt": "p"}}]}},
             _sub_msg("m-s1", "claude-haiku-5-5", "toolu_A", input_tokens=2, output_tokens=17,
                      cache_creation_input_tokens=18_717),
             {"type": "result", "subtype": "success", "total_cost_usd": 12.0, "result": "ok", "session_id": "F1",
              "usage": {"input_tokens": 0, "output_tokens": 20_000},
              "modelUsage": {"claude-opus-5-5": {"outputTokens": 20_000}, "claude-haiku-5-5": {"outputTokens": 77}}}]
    (run_dir / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    sub = cfg / "projects" / "-some-worktree" / "F1" / "subagents"
    sub.mkdir(parents=True)
    full_s1 = dict(input_tokens=2, output_tokens=74, cache_creation_input_tokens=18_717)
    s2 = dict(input_tokens=2, output_tokens=3, cache_read_input_tokens=18_717, cache_creation_input_tokens=3_724)
    nested = dict(input_tokens=5, output_tokens=1_000, cache_read_input_tokens=40_000)
    (sub / "agent-a.jsonl").write_text("".join(json.dumps(x) + "\n" for x in (
        _sub_msg("m-s1", "claude-haiku-5-5", output_tokens=17, input_tokens=2, cache_creation_input_tokens=18_717),
        _sub_msg("m-s1", "claude-haiku-5-5", **full_s1), _sub_msg("m-s2", "claude-haiku-5-5", **s2),
        _sub_msg("m-syn", "<synthetic>", output_tokens=99_999))))
    (sub / "agent-b.jsonl").write_text(json.dumps(_sub_msg("m-s3", "claude-sonnet-4-5", **nested)) + "\n")

    def priced(model, **u):
        return localspend.price(model, u, overrides=localspend.overrides())[0]
    own = priced("claude-opus-5-5", output_tokens=20_000)
    subs = priced("claude-haiku-5-5", **full_s1) + priced("claude-haiku-5-5", **s2) + priced("claude-sonnet-4-5", **nested)
    u = get_provider("claude").parse(run_dir / "output.jsonl")
    assert abs(d._net_of_session(r, u) - (own + subs)) < 1e-6, "the fork left its subagents' calls unbooked"
    assert abs(u.extra["subagent_usd"] - subs) < 1e-6 and abs(u.extra["fork_base_usd"] - (12.0 - own - subs)) < 1e-6
    # No transcript on disk (another host's config, cleaned up): the streamed subagent message still books.
    for f in sub.iterdir():
        f.unlink()
    u = get_provider("claude").parse(run_dir / "output.jsonl")
    streamed = priced("claude-haiku-5-5", input_tokens=2, output_tokens=17, cache_creation_input_tokens=18_717)
    assert abs(d._net_of_session(r, u) - (own + streamed)) < 1e-6
    # A malformed transcript count books none of them, never fails the booking.
    (sub / "agent-c.jsonl").write_text(json.dumps(_sub_msg("m-bad", "claude-haiku-5-5", output_tokens="lots")) + "\n")
    u = get_provider("claude").parse(run_dir / "output.jsonl")
    assert abs(d._net_of_session(r, u) - own) < 1e-6 and "subagent_usd" not in u.extra
    # A run that is no fork books the report as it stands: it already covers its subagents.
    plain = dict(r, note=json.dumps({"coord_context": {"how": "disk"}}))
    u = get_provider("claude").parse(run_dir / "output.jsonl")
    assert d._net_of_session(plain, u) == 12.0 and "subagent_usd" not in u.extra


def test_a_haiku_5_5_call_over_100k_prompt_tokens_is_priced_at_its_long_prompt_tier(monkeypatch):
    """Review #7627: claude-haiku-5-5's row is for prompts up to 100K tokens (input, cache read and cache
    write); a longer prompt bills 5x on every rate, output included. Other rows have no tier."""
    from ttp import localspend
    monkeypatch.setattr(localspend, "overrides", lambda: {})
    short = {"input_tokens": 60_000, "cache_read_input_tokens": 40_000, "output_tokens": 1_000}
    usd, est = localspend.price("claude-haiku-5-5", short, overrides={})
    assert not est and usd == pytest.approx((60_000 * 0.1 + 40_000 * 0.01 + 1_000 * 0.5) / 1e6)
    long = {**short, "cache_creation_input_tokens": 1}
    assert localspend.price("claude-haiku-5-5", long, overrides={})[0] == \
        pytest.approx(5 * (60_000 * 0.1 + 40_000 * 0.01 + 1 * 0.125 + 1_000 * 0.5) / 1e6)
    split = {**short, "cache_creation": {"ephemeral_1h_input_tokens": 1}}
    assert localspend.price("claude-haiku-5-5", split, overrides={})[0] == \
        pytest.approx(5 * (60_000 * 0.1 + 40_000 * 0.01 + 1 * 0.2 + 1_000 * 0.5) / 1e6)
    assert localspend.price("claude-sonnet-5-5", long, overrides={})[0] == \
        pytest.approx((60_000 * 2.0 + 40_000 * 0.2 + 1 * 2.5 + 1_000 * 10.0) / 1e6), "no tier on other rows"


def test_a_haiku_5_5_runs_list_cost_books_the_long_prompt_tier_on_its_total_prompt(monkeypatch):
    """daemon._list_cost sees only a run's summed tokens: over 100K prompt tokens in total it books 5x
    (over-booking a run of short prompts, never under-booking a long one); at or under, the base rates."""
    from ttp import localspend
    from ttp.daemon import _list_cost
    from ttp.providers.base import RunUsage
    monkeypatch.setattr(localspend, "overrides", lambda: {})
    run = {"provider": "claude", "model": "claude-haiku-5-5"}
    under = RunUsage(input_tokens=50_000, cache_read_tokens=50_000, output_tokens=2_000)
    over = RunUsage(input_tokens=50_000, cache_read_tokens=50_000, cache_write_tokens=10, output_tokens=2_000)
    assert _list_cost(run, under) == pytest.approx(round((50_000 * 0.1 + 50_000 * 0.01 + 2_000 * 0.5) / 1e6, 6))
    assert _list_cost(run, over) == pytest.approx(
        round(5 * (50_000 * 0.1 + 50_000 * 0.01 + 10 * 0.125 + 2_000 * 0.5) / 1e6, 6))
