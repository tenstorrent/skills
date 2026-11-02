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
