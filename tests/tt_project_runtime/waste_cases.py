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
    r1 = _waste_task(p, "Review #1", kind="review", labels=[f"auto_review:{good}", f"review_head:{sha}@ab", "review_payload:p1"])
    r2 = _waste_task(p, "Review #2", kind="review", labels=[f"continues:{r1}", f"review_head:{sha}@ab", "review_payload:p1"])
    r3 = _waste_task(p, "Review #3", kind="review", labels=[f"continues:{r2}", f"review_head:{'b' * 40}@ab"])
    forced = _waste_task(p, "Review #4", kind="review", labels=[f"review_head:{sha}@ab", "review_payload:p1", "review_forced"])
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


def test_rereview_waste_needs_the_same_head_and_the_same_payload(env):
    from ttp import waste
    p = make(env)
    now = 1_000_000.0
    head = f"review_head:{'a' * 40}@ab"
    first = _waste_task(p, "Review A", kind="review", labels=[head, "review_payload:p1"])
    same = _waste_task(p, "Review B", kind="review", labels=[head, "review_payload:p1"])
    changed = _waste_task(p, "Review C", kind="review", labels=[head, "review_payload:p2"])
    unknown = _waste_task(p, "Review D", kind="review", labels=[head])
    for t in (first, same, changed, unknown):
        _waste_run(p, t, 1.0, now)
    rep = waste.report(p.db, now - 86400, now, {"review_rounds": 0, "max_runs": 0})
    assert rep["buckets"]["rereview"]["tasks"] == [same]   # a changed payload is separate work; none recorded is unknown
    # The review start records the payload identity next to the head, from the spec it starts on.
    from ttp import reviewreuse
    one = reviewreuse.with_payload([head], "the  body\n")
    assert one[0] == head and reviewreuse.with_payload(one, "the body") == one
    assert reviewreuse.with_payload(one, "another body") != one


def test_waste_ledger_keeps_gross_cancelled_spend_and_shows_what_a_successor_kept_apart(env):
    """[note e12db02988af9727] A cancelled task whose successor `continues` it keeps its full spend in the
    superseded bucket, and the part the successor kept is shown apart, never as saved; the fix bucket is
    picked on spend less retained, and the ledger changes no task."""
    from ttp import waste
    p = make(env)
    now = 1_000_000.0
    old = _waste_task(p, "integration run", status="cancelled")
    for c in (3.0, 2.5, 2.0):
        _waste_run(p, old, c, now)
    _waste_task(p, "integration run, continued", status="running", labels=[f"continues:{old}"])
    dropped = _waste_task(p, "dropped idea", status="cancelled")
    _waste_run(p, dropped, 1.0, now)
    lost = _waste_task(p, "a second try", status="cancelled")
    _waste_run(p, lost, 0.5, now)
    _waste_task(p, "a second try, again", status="cancelled", labels=[f"continues:{lost}"])   # gone too: not kept
    broken = _waste_task(p, "broken idea", status="failed")
    _waste_run(p, broken, 4.0, now)
    good = _waste_task(p, "fine work")
    _waste_run(p, good, 10.0, now)
    before = p.db.q("SELECT id, status, labels FROM tasks ORDER BY id")
    rep = waste.report(p.db, now - 86400, now, p.config())
    b = rep["buckets"]["superseded"]
    assert round(b["usd"], 2) == 9.0 and b["runs"] == 5                  # gross: nothing dropped
    assert round(b["retained_usd"], 2) == 7.5 and b["retained_tasks"] == [old]
    assert round(rep["avoidable"], 2) == 13.0 and round(rep["retained"], 2) == 7.5
    assert waste.fix_bucket(rep) == "failed_task"                         # $4 failed beats $1.50 net cancelled
    line = next(x for x in waste.lines(rep) if x.startswith("cancelled or superseded work"))
    assert line.startswith("cancelled or superseded work: $9.00 (")
    assert f"$7.50 of it kept by a successor's work (#{old}), not saved" in line
    assert "$7.50 of it kept by successors" in waste.line("demo", rep)
    assert [dict(r) for r in p.db.q("SELECT id, status, labels FROM tasks ORDER BY id")] == [dict(r) for r in before]
    # Nothing kept: no retained figure anywhere.
    p.db.x("UPDATE tasks SET labels='[]'")
    rep = waste.report(p.db, now - 86400, now, p.config())
    assert rep["retained"] == 0 and "kept" not in " ".join(waste.lines(rep)) + waste.line("demo", rep)
    assert waste.fix_bucket(rep) == "superseded"


def test_waste_share_and_target_check_use_avoidable_spend_less_what_successors_kept(env):
    """Spend a successor kept is not a loss: the share and over() use avoidable less retained, so kept work
    alone never puts a project over target; the daily line still shows the gross figure and the kept one."""
    from ttp import waste
    p = make(env)
    now = 1_000_000.0
    old = _waste_task(p, "integration run", status="cancelled")
    _waste_run(p, old, 4.0, now)
    _waste_task(p, "integration run, continued", status="running", labels=[f"continues:{old}"])
    good = _waste_task(p, "fine work")
    _waste_run(p, good, 16.0, now)
    rep = waste.report(p.db, now - 86400, now, p.config())
    assert round(rep["avoidable"], 2) == 4.0 and round(rep["retained"], 2) == 4.0   # gross 20%, over 10%
    assert rep["share"] == 0 and not waste.over(rep) and waste.fix_bucket(rep) == ""
    head = waste.line("demo", rep)
    assert head.startswith("demo: avoidable $4.00 of $20.00, $4.00 of it kept by successors (net 0%)"), head
    assert "target" not in head
    # Net spend past the target still counts: $4 kept, $3 lost of $23 is 13% net.
    lost = _waste_task(p, "broken idea", status="failed")
    _waste_run(p, lost, 3.0, now)
    rep = waste.report(p.db, now - 86400, now, p.config())
    assert round(rep["share"], 3) == round(3 / 23, 3) and waste.over(rep)
    head = waste.line("demo", rep)
    assert "avoidable $7.00 of $23.00, $4.00 of it kept by successors (net 13%)" in head
    assert "over the 10% target" in head


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


def test_the_daily_review_spec_carries_the_waste_ledger_through_the_schedule_path(env):
    import time as _time
    from ttp import daemon as dmod
    p = make(env)
    p.db.x("DELETE FROM messages")
    f = _waste_task(p, "broken idea", status="failed")
    _waste_run(p, f, 5.0, _time.time())
    d = dmod.Daemon(p.base)
    d.cfg = p.config()
    d.alert = lambda *a, **k: None
    s = {"name": "daily-review", "budget_usd_day": None, "last_run": None, "description": "review"}
    assert d._schedule_llm(s, {}) == "queued"
    spec = p.db.one("SELECT spec FROM tasks WHERE origin='schedule'")["spec"]
    assert "Self-efficiency audit (last 24 h):" in spec
    assert "Waste ledger (last 24 h; `ttp waste` for more): demo: avoidable $5.00 of $5.00" in spec
    assert spec.index("Self-efficiency audit (last 24 h):") < spec.index("Waste ledger (last 24 h")


def test_the_self_efficiency_audit_survives_a_push_ref_error(env, monkeypatch):
    from ttp import audit, landed
    p = make(env)

    def boom(*a, **k):
        raise RuntimeError("git: no such ref")
    monkeypatch.setattr(landed, "_push_ref", boom)
    import time as _time
    from ttp import gatejudge
    gatejudge.quarantine(p.state, ["tests/x.py::test_gone"], {"why": "flaky"}, _time.time())
    out = audit.lines(p.db, p=p)                                     # the push-ref error costs only its line
    assert out and not any(gatejudge.NOT_FOUND in line for line in out)


def test_failed_reviews_with_a_verdict_are_rejected_changes_and_a_broken_run_a_failed_task(env):
    from ttp import waste
    p = make(env)
    now = 1_000_000.0
    code = _waste_task(p, "a change")
    sup = _waste_task(p, "Review sup", kind="review", status="failed", labels=[f"auto_review:{code}"])
    p.db.update_task(sup, result=json.dumps({"status": "changes_needed", "summary": "two findings",
                                             "review_superseded": {"by_review": 9, "how": "passed"}}))
    fail = _waste_task(p, "Review fail", kind="review", status="failed")
    p.db.update_task(fail, result=json.dumps({"status": "failed", "summary": "a bug", "review_failure": "substantive",
                                              "followups": [{"title": "Fix the bug", "spec": "s"}]}))
    broken = _waste_task(p, "Review broken", kind="review", status="failed")
    p.db.update_task(broken, result=json.dumps({"status": "no_handoff", "summary": "ended without a hand-off",
                                                "review_superseded": {"by_review": 9, "how": "passed"}}))
    bare = _waste_task(p, "crashed work", status="failed")
    for t, cost in ((code, 1.0), (sup, 2.0), (fail, 3.0), (broken, 4.0), (bare, 5.0)):
        _waste_run(p, t, cost, now)
    b = waste.report(p.db, now - 3600, now + 1)["buckets"]
    assert (b["rejected"]["usd"], sorted(b["rejected"]["tasks"])) == (5.0, sorted([code, fail]))  # by the change
    assert (b["failed_task"]["usd"], sorted(b["failed_task"]["tasks"])) == (9.0, sorted([broken, bare]))
    assert "rejected changes: $5.00" in "\n".join(waste.lines(waste.report(p.db, now - 3600, now + 1)))


def test_waste_ledger_counts_no_costly_wake_after_a_long_host_sleep_and_queues_no_fix_for_one_small_one(env):
    from ttp import waste
    p = make(env)
    now = 1_000_000.0
    _waste_run(p, None, 0.5, now, role="coordinator", started=now - 3 * 86400)
    p.db.set_kv("host_sleeps", [[now - 3 * 86400 + 100, now - 900]])   # days asleep: no cache outlives it
    _waste_run(p, None, 3.0, now, role="coordinator", started=now - 600)
    _waste_run(p, None, 2.5, now, role="coordinator", started=now - 300)   # awake since: over the limit
    rep = waste.report(p.db, now - 86400, now, p.config())
    assert (round(rep["buckets"]["costly_wake"]["usd"], 2), rep["buckets"]["costly_wake"]["runs"]) == (1.5, 1)
    # One wake under $0.50 over the limit is no fix task's worth, though it tops an over-target share.
    later = now + 10 * 86400   # a later day, awake throughout
    _waste_run(p, None, 1.3, later, role="coordinator")
    _waste_run(p, _waste_task(p, "fine work"), 1.0, later)
    rep = waste.report(p.db, later - 86400, later, p.config())
    assert waste.over(rep) and waste.fix_bucket(rep) == "" and "most in coordinator wakes" in waste.line("demo", rep)
    _waste_run(p, None, 1.3, later, role="coordinator")
    assert waste.fix_bucket(waste.report(p.db, later - 86400, later, p.config())) == "costly_wake"


def _handoff_run(p, task, cost, started, result=None, status="ok"):
    """A run with a run dir; `result` (a dict) is its result.json, None leaves it unwritten."""
    import json as _json
    d = p.base / "runs-test" / f"{task}-{int(started)}"
    d.mkdir(parents=True, exist_ok=True)
    if result is not None:
        (d / "result.json").write_text(_json.dumps(result))
    p.db.x("INSERT INTO runs(task, role, provider, status, dir, started, ended, cost_usd) VALUES(?,?,?,?,?,?,?,?)",
           (task, "worker", "claude", status, str(d), started, started + 60, cost))


def test_over_runs_keeps_a_continuation_with_a_new_result_apart_and_counts_repeats_and_unknown(env, monkeypatch):
    from ttp import runcap, waste
    p = make(env)
    now = 1_000_000.0
    useful = _waste_task(p, "long bench", kind="work")
    repeat = _waste_task(p, "stuck wait", kind="work")
    unknown = _waste_task(p, "silent code", kind="code")
    rework = _waste_task(p, "code that says the same", kind="code")
    for t in (useful, repeat, unknown, rework):
        for i in range(4):   # runs 1-4 are within the limit
            _handoff_run(p, t, 0.1, now - 5000 + i * 100, {"status": "waiting", "summary": f"step {i}"})
    _handoff_run(p, useful, 1.0, now - 3000, {"status": "waiting", "summary": "cells a, b measured"})
    _handoff_run(p, useful, 2.0, now - 2900, {"status": "done", "summary": "all cells measured"})
    _handoff_run(p, repeat, 1.5, now - 3000, {"status": "waiting", "summary": "step 3"})   # its previous hand-off again
    _handoff_run(p, repeat, 0.5, now - 2900, {"status": "failed", "summary": "no change"})  # no progress
    _handoff_run(p, unknown, 0.7, now - 3000)   # no hand-off; without `p` its commits are unknown
    _handoff_run(p, rework, 0.3, now - 3000, {"status": "waiting", "summary": "step 3"})   # same words; commits?
    rep = waste.report(p.db, now - 86400, now, p.config())
    b = rep["buckets"]["over_runs"]
    assert round(b["usd"], 2) == 3.0 and b["runs"] == 4 and set(b["tasks"]) == {repeat, unknown, rework}
    c = rep["continued"]
    assert round(c["usd"], 2) == 3.0 and c["limit"] == 4 and [(x["task"], x["rank"]) for x in c["runs"]] == \
        [(useful, 5), (useful, 6)]
    assert round(rep["avoidable"], 2) == 3.0 and round(rep["total"], 2) == round(0.1 * 16 + 6.0, 2)   # raw total
    line = next(x for x in waste.lines(rep) if "brought a new result" in x)
    run5 = c["runs"][0]["run"]
    assert line.startswith("runs past the per-task limit (4) that brought a new result: $3.00, 2 run(s), "
                           "not avoidable") and f"run {run5} (#{useful}, run 5)" in line
    # the run stop reads the same test: the repeat's failed run is idle, the useful ones are not
    rows = {r["id"]: r for r in p.db.q("SELECT * FROM runs")}
    task = lambda i: p.db.one("SELECT * FROM tasks WHERE id=?", (i,))   # noqa: E731
    assert not runcap.idle(p, task(useful), rows[run5])
    assert runcap.idle(p, task(repeat), max((r for r in rows.values() if r["task"] == repeat), key=lambda r: r["id"]))
    # a commit inside a code run is a new result when `p` can read git, even under a repeated hand-off
    from ttp import chaincap
    monkeypatch.setattr(chaincap, "code_commits", lambda *a, **k: 1)
    rep2 = waste.report(p.db, now - 86400, now, p.config(), p=p)
    assert [x["task"] for x in rep2["continued"]["runs"]] == [useful, useful, unknown, rework]


def test_over_runs_skips_runs_that_never_reached_the_model_in_rank_and_repeat(env):
    """A run that ended on a limit, auth or fallback (runcap.FREE) is no run to over_runs, as to the run stop:
    it takes no rank and is not the previous hand-off the next run is compared with."""
    from ttp import runcap, waste
    p = make(env)
    now = 1_000_000.0
    for free in runcap.FREE:
        t = _waste_task(p, f"wait through a {free}", kind="work")
        for i in range(4):
            _handoff_run(p, t, 0.1, now - 5000 + i * 100, {"status": "waiting", "summary": f"step {i}"})
        _handoff_run(p, t, 0.0, now - 3100, status=free)
        _handoff_run(p, t, 0.4, now - 3000, {"status": "waiting", "summary": "step 3"})   # run 4's again
    rep = waste.report(p.db, now - 86400, now, p.config())
    assert rep["buckets"]["over_runs"]["runs"] == len(runcap.FREE) and not rep["continued"]["runs"]


def test_the_waste_fix_task_is_not_queued_again_for_runs_a_diagnosis_already_covers(env):
    import time as _time
    from ttp import daemon as dmod
    p = make(env)
    now = _time.time()
    f = _waste_task(p, "broken idea", status="failed")
    _waste_run(p, f, 5.0, now)
    d = dmod.Daemon(p.base)
    d.alert = lambda *a, **k: None
    assert "Queued fix task #" in d._waste_ledger(p.db)
    fix = p.db.one("SELECT id, spec FROM tasks WHERE labels LIKE '%waste_fix:failed_task%'")
    run = p.db.one("SELECT id FROM runs WHERE task=?", (f,))["id"]
    assert f"\nFlagged runs: {run}" in fix["spec"]
    p.db.update_task(fix["id"], status="done")                    # diagnosed, same runs still in the window
    assert f"Fix task #{fix['id']} already covers" in d._waste_ledger(p.db)
    assert len(p.db.q("SELECT id FROM tasks WHERE labels LIKE '%waste_fix:%'")) == 1
    p.db.update_task(fix["id"], status="failed")                  # a failed one diagnosed nothing
    assert "Queued fix task #" in d._waste_ledger(p.db)
    p.db.x("UPDATE tasks SET status='done' WHERE labels LIKE '%waste_fix:%'")
    _waste_run(p, f, 3.0, now)                                    # a new flagged run: a new diagnosis
    assert "Queued fix task #" in d._waste_ledger(p.db)
    assert len(p.db.q("SELECT id FROM tasks WHERE labels LIKE '%waste_fix:%'")) == 3
