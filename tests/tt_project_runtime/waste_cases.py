# Waste ledger tests (waste.py, `ttp waste`, the daily review's waste lines) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
# Add new waste ledger tests here, not there.
from __future__ import annotations


def _waste_task(p, title, status="done", kind="code", labels=(), origin="coordinator", created=None):
    tid = p.db.add_task(title, "s", kind=kind, origin=origin, labels=list(labels))
    p.db.update_task(tid, status=status)
    if created is not None:
        p.db.x("UPDATE tasks SET created=? WHERE id=?", (created, tid))
    return tid


def _waste_run(p, task, cost, now, status="done", role="worker", started=None, note=None):
    started = now - 600 if started is None else started
    p.db.x("INSERT INTO runs(task, role, provider, status, started, ended, cost_usd, note) VALUES(?,?,?,?,?,?,?,?)",
           (task, role, "claude", status, started, started + 60, cost, note))


def test_waste_ledger_counts_each_run_once_in_its_first_bucket(env):
    from ttp import waste
    p = make(env)
    now = 1_000_000.0
    sha = "a" * 40
    good = _waste_task(p, "a good change")
    _waste_run(p, good, 10.0, now)                                    # no waste
    cancelled = _waste_task(p, "dropped work", status="cancelled")
    _waste_run(p, cancelled, 2.0, now)
    _waste_run(p, cancelled, 1.0, now, status="failed")             # cancelled wins over crashed
    gone = _waste_task(p, "re-landed elsewhere")
    p.db.x("INSERT INTO task_superseded(task, head, sha, ts) VALUES(?,?,?,?)", (gone, "h", "s", now))
    _waste_run(p, gone, 0.5, now)
    failed = _waste_task(p, "broken idea", status="failed")
    _waste_run(p, failed, 3.0, now)
    first = _waste_task(p, "Fix the thing", created=now - 3600)
    dup = _waste_task(p, "fix  the thing", created=now - 60)
    _waste_run(p, first, 1.0, now)
    _waste_run(p, dup, 4.0, now)                                     # same title within 7 days
    old = _waste_task(p, "far apart", created=now - 30 * 86400)
    again = _waste_task(p, "far apart", created=now - 60)
    sched = _waste_task(p, "[daily-review] review", origin="schedule", created=now - 7200)
    sched2 = _waste_task(p, "[daily-review] review", origin="schedule", created=now - 60)
    for t in (old, again, sched, sched2):
        _waste_run(p, t, 0.25, now)                                  # not duplicates
    r1 = _waste_task(p, "Review #1", kind="review", labels=[f"auto_review:{good}", f"review_head:{sha}@ab"])
    r2 = _waste_task(p, "Review #2", kind="review", labels=[f"continues:{r1}", f"review_head:{sha}@ab"])
    r3 = _waste_task(p, "Review #3", kind="review", labels=[f"continues:{r2}", f"review_head:{'b' * 40}@ab"])
    forced = _waste_task(p, "Review #4", kind="review", labels=[f"review_head:{sha}@ab", "review_forced"])
    _waste_run(p, r1, 1.0, now)
    _waste_run(p, r2, 1.5, now)                                      # same head as r1: re-review
    _waste_run(p, r3, 2.5, now)                                      # third round
    _waste_run(p, forced, 0.75, now)                                 # forced: not a re-review
    crashy = _waste_task(p, "finished after a crash")
    _waste_run(p, crashy, 0.6, now, status="timeout")
    _waste_run(p, crashy, 0.4, now, status="lost", note='{"not_waste": "reboot"}')   # the host rebooted
    _waste_run(p, crashy, 1.0, now)
    many = _waste_task(p, "too many runs")
    for i in range(6):
        _waste_run(p, many, 0.5, now, started=now - 3000 + i * 100)  # runs 5 and 6 count
    _waste_run(p, None, 3.0, now, role="coordinator")                # $2 over the $1 wake limit
    _waste_run(p, None, 0.9, now, role="coordinator")
    _waste_run(p, good, 50.0, now - 3 * 86400)                       # before the window
    rep = waste.report(p.db, now - 86400, now, p.config())
    got = {k: (round(v["usd"], 2), v["runs"]) for k, v in rep["buckets"].items() if v["runs"]}
    assert got == {"superseded": (3.5, 3), "failed_task": (3.0, 1), "duplicate": (4.0, 1), "rereview": (1.5, 1),
                   "review_rounds": (2.5, 1), "crashed": (0.6, 1), "over_runs": (1.0, 2), "costly_wake": (2.0, 1)}
    assert rep["buckets"]["duplicate"]["tasks"] == [dup]
    assert rep["buckets"]["superseded"]["tasks"] == [cancelled, gone]
    total = 10 + 3 + 0.5 + 3 + 5 + 1 + 1 + 1.5 + 2.5 + 0.75 + 2 + 3 + 3.9
    assert round(rep["total"], 2) == round(total, 2)
    assert round(rep["avoidable"], 2) == 18.1 and rep["share"] > 0.10
    assert waste.fix_bucket(rep) == "duplicate"
    head = waste.line("demo", rep)
    assert head.startswith("demo: avoidable $18.10 of $") and "most in duplicate tasks" in head
    assert "over the 10% target" in head
    lines = waste.lines(rep)
    assert lines[0].startswith("duplicate tasks: $4.00 (") and f"#{dup}" in lines[0]
    assert len(lines) == 8
    # A project move: every run started after it is the stand-in's, whatever else it was.
    p.db.set_kv("project_moved_at", now - 700)
    rep = waste.report(p.db, now - 86400, now, p.config())
    assert rep["buckets"]["stand_in"]["runs"] == 20 and round(rep["buckets"]["stand_in"]["usd"], 2) == round(total - 3, 2)
    assert rep["buckets"]["over_runs"]["runs"] == 2                  # `many` ran before the move: still its own


def test_waste_ledger_under_target_queues_no_fix_and_the_cli_prints_buckets(env, capsys):
    from ttp import cli, waste
    p = make(env)
    now = 1_000_000.0
    t = _waste_task(p, "fine work")
    _waste_run(p, t, 20.0, now)
    f = _waste_task(p, "small miss", status="failed")
    _waste_run(p, f, 1.0, now)
    rep = waste.report(p.db, now - 86400, now, p.config())
    assert round(rep["share"], 3) == round(1 / 21, 3) and not waste.over(rep) and waste.fix_bucket(rep) == ""
    assert "target" not in waste.line("demo", rep)
    empty = waste.report(p.db, now + 86400, now + 2 * 86400, p.config())
    assert empty["total"] == 0 and empty["share"] == 0 and waste.lines(empty) == [] and not waste.over(empty)
    p.set_config("waste.target_share", 0.01, by={"by": "user"})
    assert waste.fix_bucket(waste.report(p.db, now - 86400, now, p.config())) == "failed_task"
    import time as _time
    _waste_run(p, f, 2.0, _time.time() - 60)
    cli.main(["waste", p.name, "--since", "1h"])
    out = capsys.readouterr().out
    assert out.startswith(f"{p.name}: avoidable $2.00 of $2.00 (100%)") and "- failed tasks: $2.00 (100%), 1 run(s)" in out


def test_daily_review_carries_the_waste_ledger_alerts_low_and_queues_one_fix_task(env):
    import time as _time
    from ttp import daemon as dmod
    p = make(env)
    now = _time.time()
    f = _waste_task(p, "broken idea", status="failed")
    _waste_run(p, f, 5.0, now)
    ok = _waste_task(p, "fine work")
    _waste_run(p, ok, 5.0, now)
    d = dmod.Daemon(p.base)
    sent = []
    d.alert = lambda key, text, severity="high", every_s=0, outage=None: sent.append((key, text, severity))
    text = d._waste_ledger(p.db)
    assert "Waste ledger (last 24 h" in text and "- failed tasks: $5.00 (50%)" in text and "Queued fix task #" in text
    assert sent == [("waste-ledger", "Waste ledger (24 h): demo: avoidable $5.00 of $10.00 (50%), most in failed "
                                     "tasks; over the 10% target", "low")]
    fixes = p.db.q("SELECT id, kind, priority, spec FROM tasks WHERE labels LIKE '%waste_fix:failed_task%'")
    assert len(fixes) == 1 and fixes[0]["kind"] == "harness" and fixes[0]["priority"] == 2
    assert f"#{f}" in fixes[0]["spec"] and "upstream:" in fixes[0]["spec"]
    assert "still open" in d._waste_ledger(p.db)                     # one fix task at a time
    assert len(p.db.q("SELECT id FROM tasks WHERE labels LIKE '%waste_fix:%'")) == 1
