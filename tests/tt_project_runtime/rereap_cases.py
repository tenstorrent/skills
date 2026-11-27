# Cases for tests/test_tt_project_runtime.py: a run the reap quarantined whose result.json is complete
# is re-read once, model-free, so its task ends normally (ttp/rereap.py): helpers, fixtures and
# imports come from that file.
from __future__ import annotations


def _quarantined(p, tmp_path, result, cost=1.5):
    """A worker run whose reap was cut off twice (Daemon._quarantine_run), its run dir holding `result`."""
    from ttp import daemon as dm
    d = dm.Daemon(p.base)
    tid = p.db.add_task("job", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text(json.dumps({"_cost": cost}))
    (run_dir / "exit.json").write_text(json.dumps({"rc": 0, "ended": time.time()}))
    if result is not None:
        (run_dir / "result.json").write_text(result if isinstance(result, str) else json.dumps(result))
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id,cost_usd) VALUES(?,?,?,?,?,?,?,?)",
                 (tid, "worker", "fake", time.time(), "running", str(run_dir), d.boot, cost))
    d._quarantine_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)), "it kept failing")
    assert p.db.task(tid)["status"] == "failed" and p.db.task(tid)["spent_usd"] == cost
    return d, tid, rid


_DONE = {"status": "done", "summary": "all of it"}


def test_a_quarantined_run_with_a_complete_result_is_reaped_once_more_and_its_task_ends_done(env, tmp_path):
    from ttp import rereap
    p = make(env)
    d, tid, rid = _quarantined(p, tmp_path, _DONE)
    d.reap_runs()
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "ok"
    assert p.db.task(tid)["status"] == "done"
    assert p.db.task(tid)["spent_usd"] == 1.5 and p.db.spent_since(0) == 1.5, "the abandon's booking taken back, the run's booked once"
    assert str(rid) in (p.db.kv(rereap.KEY) or {})
    assert "re-read from its files" in (p.logs / "daemon.log").read_text()


@pytest.mark.parametrize("result", [None, "{not json", "[1]", {"summary": "no status"}, {"status": "nonsense"}])
def test_a_quarantined_run_with_a_missing_or_broken_result_stays_quarantined(env, tmp_path, result):
    from ttp import rereap
    p = make(env)
    d, tid, rid = _quarantined(p, tmp_path, result)
    d.reap_runs()
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "failed"
    assert p.db.task(tid)["status"] == "failed" and not p.db.kv(rereap.KEY)
    assert "missing or not a complete hand-off" in rereap.reread(p, rid)


def test_a_quarantined_end_is_read_again_once_only(env, tmp_path):
    from ttp import rereap
    p = make(env)
    d, tid, rid = _quarantined(p, tmp_path, _DONE)
    assert rereap.reread(p, rid) == ""
    assert "already re-read" in rereap.reread(p, rid)
    # Quarantined again (the reap cut off twice more): the sweep leaves it alone.
    p.db.x("UPDATE runs SET status='failed' WHERE id=?", (rid,))
    p.db.update_task(tid, status="failed")
    rereap.sweep(p, lambda *_: None)
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "failed"


def test_reread_refuses_a_run_that_was_not_quarantined_or_has_a_later_run(env, tmp_path):
    from ttp import rereap
    p = make(env)
    d, tid, rid = _quarantined(p, tmp_path, _DONE)
    assert "no run" in rereap.reread(p, 9999)
    p.db.x("DELETE FROM messages WHERE ref=?", (f"reap-quarantine:{rid}",))
    assert "not quarantined" in rereap.reread(p, rid)
    p.db.post("out", "q", kind="info", severity="low", ref=f"reap-quarantine:{rid}")
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "ok", str(tmp_path), "x"))   # the task already went on
    assert "not the quarantined last run" in rereap.reread(p, rid)
    assert p.db.task(tid)["status"] == "failed"


def test_the_sweep_re_reads_at_most_a_few_runs_a_tick(env, tmp_path):
    from ttp import rereap
    p = make(env)
    rids = []
    for n in range(rereap.PER_TICK + 1):
        sub = tmp_path / str(n)
        sub.mkdir()
        rids.append(_quarantined(p, sub, _DONE)[2])
    rereap.sweep(p, lambda *_: None)
    assert len(p.db.kv(rereap.KEY)) == rereap.PER_TICK
    rereap.sweep(p, lambda *_: None)
    assert len(p.db.kv(rereap.KEY)) == len(rids)


def test_ttp_task_reread_puts_a_quarantined_run_back_and_says_why_it_cannot(env, tmp_path, capsys):
    from ttp import cli
    p = make(env)
    d, tid, rid = _quarantined(p, tmp_path, _DONE)
    cli.main(["task", p.name, "reread", str(rid)])
    assert "read again from its files" in capsys.readouterr().out
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "running"
    d.reap_runs()
    assert p.db.task(tid)["status"] == "done"
    for bad in (str(rid), "x"):   # already re-read once; not a run id
        with pytest.raises(SystemExit):
            cli.main(["task", p.name, "reread", bad])
