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
    usd, est = localspend.price("claude-haiku-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                                                    "cache_read_input_tokens": 1_000_000})
    assert not est and abs(usd - 0.61) < 1e-9
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

    def booked(lines, ended=at):
        tid, r = _fork_run(d, cost=wrong, out_tokens=20_000, status="done")
        note = {**json.loads(r["note"]), "session_cost_usd": 12.0, "fork_base_usd": 12.0 - wrong}
        db.x("UPDATE runs SET model='opus', ended=?, note=? WHERE id=?", (ended, json.dumps(note), r["id"]))
        run_dir = d.p.runs / str(r["id"])
        run_dir.mkdir(parents=True)
        (run_dir / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        return tid, r["id"]

    tid, fixed = booked(_alias_lines(["claude-opus-5-5"]))
    _, bare = booked(_alias_lines([])[:1])   # no modelUsage: keeps its booking
    _, old = booked(_alias_lines(["claude-opus-5-5"]), ended=at - 3 * 86400)   # before the window
    changed = d.reprice_alias_runs()
    assert [c[0] for c in changed] == [fixed] and abs(changed[0][2] - right) < 1e-6
    row = db.one("SELECT cost_usd, note FROM runs WHERE id=?", (fixed,))
    note = json.loads(row["note"])
    assert abs(row["cost_usd"] - right) < 1e-6 and note["repriced_model"] == "claude-opus-5-5"
    assert abs(note["fork_base_usd"] - (12.0 - right)) < 1e-6
    assert abs(db.task(tid)["spent_usd"] - right) < 1e-6, "the task kept the phantom spend"
    fix = db.q("SELECT ts, usd FROM ledger WHERE usd<0")
    assert len(fix) == 1 and fix[0]["ts"] == at and abs(fix[0]["usd"] + (wrong - right)) < 1e-6
    for rid in (bare, old):
        assert abs(db.one("SELECT cost_usd FROM runs WHERE id=?", (rid,))["cost_usd"] - wrong) < 1e-9
    assert db.kv(dmod.MODEL_REPRICE_KEY)["runs"] == 1
    assert d.reprice_alias_runs() is None, "a second pass ran again"
    db.set_kv(dmod.MODEL_REPRICE_KEY, None)
    assert d.reprice_alias_runs() == [], "a re-priced row was re-booked again"
    assert len(db.q("SELECT 1 FROM ledger WHERE usd<0")) == 1
