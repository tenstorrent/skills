# Effort A/B (effortab) tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there). Add new effort A/B tests here, not there.
from __future__ import annotations


def _ab_on(p, **extra):
    cfg = p.config()
    cfg["effort_ab"] = {"enabled": True, **extra}
    return cfg


def _ab_task(p, arm, **kw):
    """A new code task queued at standard whose lineage lands in `arm`."""
    from ttp import effortab
    while True:
        tid = p.db.add_task("Port the parser", "Make it faster.", kind="code", tier="standard",
                            origin="coordinator", provider="claude", budget_usd=8.0, **kw)
        if effortab.arm_of(tid) == arm:
            return tid


def test_effort_ab_is_off_by_default_stable_per_lineage_and_skips_pinned_or_other_tasks(env):
    from ttp import coordinator as coord, effortab
    from ttp.project import DEFAULT_CONFIG
    p = make(env)
    assert DEFAULT_CONFIG["effort_ab"] == {"enabled": False, "jev_pick": False}
    assert coord.settable("effort_ab.enabled")("on") is True and coord.settable("effort_ab.enabled")("off") is False
    assert coord.settable("effort_ab.jev_pick")("true") is True
    med, hi = _ab_task(p, "medium"), _ab_task(p, "high")
    assert effortab.assign(p.db, p.config(), p.db.task(med), "standard", "claude") is None   # off by default
    cfg = _ab_on(p)
    # Stable: a hash of the root id, never Python's salted hash(), so every process agrees.
    assert [effortab.arm_of(i) for i in range(1, 9)] == [effortab.arm_of(i) for i in range(1, 9)]
    assert {effortab.arm_of(i) for i in range(1, 40)} == {"high", "medium"}
    assert effortab.assign(p.db, cfg, p.db.task(med), "standard", "claude") == \
        {"arm": "medium", "root": med, "effort": "medium"}
    assert effortab.assign(p.db, cfg, p.db.task(hi), "standard", "claude") == \
        {"arm": "high", "root": hi, "effort": "high"}
    # A retry of the same task keeps it; a deep retry or a light wake records the arm at its tier's effort.
    p.db.update_task(med, attempts=1, result=json.dumps({"status": "failed"}))
    assert effortab.assign(p.db, cfg, p.db.task(med), "standard", "claude")["effort"] == "medium"
    assert effortab.assign(p.db, cfg, p.db.task(med), "deep", "claude") == {"arm": "medium", "root": med, "effort": "max"}
    assert effortab.assign(p.db, cfg, p.db.task(med), "light", "claude")["effort"] == "low"
    # A review fix and a continuation hop of the lineage (continues:, the daemon's labels) keep its root's arm,
    # whatever their own ids hash to; a hop of a hop too.
    fix = p.db.add_task(f"Fix review #900: Port the parser", "f", kind="code", tier="standard", origin="daemon",
                        labels=[f"continues:{med}", "review_fix:900"])
    hop = p.db.add_task("continue: port the parser", "c", kind="code", tier="standard", origin="coordinator",
                        labels=[f"continues:{fix}"])
    for t in (fix, hop):
        assert effortab.assign(p.db, cfg, p.db.task(t), "standard", "claude") == \
            {"arm": "medium", "root": med, "effort": "medium"}
    # Never put in an arm: a task with its own effort pin (or a fix of a pinned root), one queued at deep or
    # light, a review, a work task.
    pinned = _ab_task(p, "medium", effort="high")
    pfix = p.db.add_task("Fix review #901: x", "f", kind="code", tier="standard", origin="daemon",
                         labels=[f"continues:{pinned}"])
    deep = p.db.add_task("Redesign", "s", kind="code", tier="deep", origin="coordinator")
    light = p.db.add_task("Typo", "s", kind="code", tier="light", origin="coordinator")
    rev = p.db.add_task(f"Review #{med}", "s", kind="review", tier="standard", origin="daemon")
    work = p.db.add_task("Tidy notes", "s", kind="work", tier="standard", origin="coordinator")
    for t in (pinned, pfix, deep, light, rev, work):
        assert effortab.assign(p.db, cfg, p.db.task(t), "standard", "claude") is None, t
    # A tier whose effort is not high (a project that moved it) keeps its own effort.
    cfg["providers"]["claude"]["tiers"]["standard"]["effort"] = "xhigh"
    assert effortab.assign(p.db, cfg, p.db.task(med), "standard", "claude")["effort"] == "xhigh"


def test_effort_ab_arm_goes_in_the_run_note_and_the_run_starts_at_its_effort(env, monkeypatch):
    from ttp import daemon as dmod, effortab
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("effort_ab.enabled", True)
    d = Daemon(p.base)
    d.jev = None
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.base), None))
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append((a, k)) or 0)
    med = _ab_task(p, "medium")
    d.dispatch()
    (args, kw), = [(a, k) for a, k in started if k["task"]["id"] == med]
    assert args[3] == "standard" and kw["note"][effortab.KEY] == {"arm": "medium", "root": med, "effort": "medium"}
    # Each code task the tick started carries its own lineage's arm.
    assert all(k["note"][effortab.KEY]["arm"] == effortab.arm_of(k["task"]["id"]) for a, k in started)
    monkeypatch.undo()

    class Proc:   # never a real runner
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    d = Daemon(p.base)
    ab = {"arm": "medium", "root": med, "effort": "medium"}
    rid = d.start_run("worker", "go", "claude", "standard", str(p.base), task=p.db.task(med), note={effortab.KEY: ab})
    row = p.db.one("SELECT effort, note FROM runs WHERE id=?", (rid,))
    assert row["effort"] == "medium" and json.loads(row["note"])[effortab.KEY] == {**ab, "effective": "medium"}
    # A task's own effort pin always wins over a note's arm.
    pinned = p.db.add_task("Pinned", "s", kind="code", tier="standard", origin="coordinator", effort="xhigh")
    rid = d.start_run("worker", "go", "claude", "standard", str(p.base), task=p.db.task(pinned), note={effortab.KEY: ab})
    assert p.db.one("SELECT effort FROM runs WHERE id=?", (rid,))["effort"] == "xhigh"
    # No arm: the tier's effort.
    rid = d.start_run("worker", "go", "claude", "standard", str(p.base), task=p.db.task(med))
    assert p.db.one("SELECT effort FROM runs WHERE id=?", (rid,))["effort"] == "high"


def test_a_conclusion_run_the_tier_floor_lifts_logs_its_effective_effort_and_leaves_the_arm_comparison(
        env, monkeypatch):
    """A conclusion run whose arm is medium under a high floor runs at high: its note records the arm's
    effort and the effective one, the A/B report counts its lineage in neither arm, and a lifted Jev
    pick settles as no change rather than scored right or wrong."""
    from ttp import daemon as dmod, effort, effortab, jevuse

    class Proc:   # never a real runner
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    p = make(env)
    d = dmod.Daemon(p.base)
    med, kept = _ab_task(p, "medium"), _ab_task(p, "medium")

    def start(task, ab, **kw):
        rid = d.start_run("worker", "go", "claude", "standard", str(p.base), task=p.db.task(task),
                          note={effortab.KEY: ab}, **kw)
        p.db.x("UPDATE runs SET status='ok', cost_usd=2 WHERE id=?", (rid,))
        row = p.db.one("SELECT effort, note FROM runs WHERE id=?", (rid,))
        return row["effort"], json.loads(row["note"])[effortab.KEY]

    ab = {"arm": "medium", "root": med, "effort": "medium"}
    assert start(med, ab, min_effort="high") == ("high", {**ab, "effective": "high", "lifted": True})
    got = start(kept, {**ab, "root": kept})
    assert got == ("medium", {**ab, "root": kept, "effective": "medium"})
    m, h = (effortab.report(p, days=30)["arms"][a] for a in ("medium", "high"))
    assert (m["changes"], m["runs"], m["usd"]) == (1, 1, 2.0), "only the run that ran at medium counts as medium"
    assert (h["changes"], h["runs"]) == (0, 0), "the lifted run is not counted as high either"
    # A later unlifted run of the lifted lineage does not bring it back into the comparison.
    start(med, ab)
    assert effortab.report(p, days=30)["arms"]["medium"]["runs"] == 1
    # Jev's medium pick under the floor never ran: no change, not scored right when the task ends.
    picked = p.db.add_task("Plan it", "conclusion: the plan", kind="code", tier="standard", origin="coordinator")
    cid = jevuse.record(p.db, "effort", {"effort_level": "medium"}, 0.01, avoided_usd=0.5,
                        ref=f"task:{picked}", changed=True)
    pick = {"by": "jev", "pick": "medium", "jev_call": cid, "root": picked, "effort": "medium"}
    assert start(picked, pick, min_effort="high")[1]["lifted"] is True
    effort.settle(p.db, picked, "done", 1)
    assert p.db.one("SELECT outcome FROM jev_calls WHERE id=?", (cid,))["outcome"] == jevuse.NO_CHANGE
    assert jevuse.stats(p.db, p.config())["effort"]["saved"] == 0.0
    # An unlifted pick is still scored when the task ends.
    other = p.db.add_task("Fix it", "s", kind="code", tier="standard", origin="coordinator")
    cid2 = jevuse.record(p.db, "effort", {"effort_level": "medium"}, 0.01, ref=f"task:{other}", changed=True)
    start(other, {**pick, "jev_call": cid2, "root": other}, min_effort="low")
    effort.settle(p.db, other, "done", 1)
    assert p.db.one("SELECT outcome FROM jev_calls WHERE id=?", (cid2,))["outcome"] == "right"


def _ab_run(p, task, arm, root, cost, out, calls=None, pick=None, checks=None, started=None):
    note = {"effort_ab": {"arm": arm, "root": root, "effort": arm}}
    if calls:
        note["cache_telemetry"] = {"calls": calls}
    if pick:
        note["pick"] = pick
    now = time.time()
    rid = p.db.x("INSERT INTO runs(task,role,provider,model,effort,status,started,ended,cost_usd,output_tokens,note) "
                 "VALUES(?,'worker','claude','opus',?,'ok',?,?,?,?,?)",
                 (task, arm, started or now - 600, (started or now - 600) + 100, cost, out, json.dumps(note)))
    if checks is not None:
        (p.runs / str(rid)).mkdir(parents=True, exist_ok=True)
        (p.runs / str(rid) / "checks.json").write_text(json.dumps({"head": "abc", "passed": checks}))
    return rid


def test_effort_ab_report_compares_the_arms_and_applies_the_end_rule(env, capsys):
    from ttp import effortab
    p = make(env)
    now = time.time()
    med, hi = _ab_task(p, "medium"), _ab_task(p, "high")
    fix = p.db.add_task(f"Fix review #1: x", "f", kind="code", tier="standard", origin="daemon",
                        labels=[f"continues:{med}", "review_fix:1"])
    _ab_run(p, med, "medium", med, 2.0, 10000, calls=20, checks=False)
    _ab_run(p, fix, "medium", med, 1.0, 6000, calls=10, checks=True)
    _ab_run(p, hi, "high", hi, 3.0, 20000, calls=30, checks=True)
    _ab_run(p, hi, "high", hi, 5.0, 30000, pick={"tier": "deep", "by": "retry"})
    _ab_run(p, hi, "high", hi, 9.0, 1, started=now - 400 * 86400)   # outside the window: not counted
    p.db.x("INSERT INTO runs(task,role,provider,status,started,cost_usd,note) VALUES(?,'worker','claude','ok',?,7,'{}')",
           (hi, now - 100))   # a run with no arm
    for subject, outcome in ((med, "changes_needed"), (fix, "approved"), (hi, "approved")):
        rv = p.db.add_task(f"Review #{subject}", "s", kind="review", origin="daemon", labels=[f"auto_review:{subject}"])
        p.db.x("INSERT INTO review_stats(task,ts,outcome) VALUES(?,?,?)", (rv, now - 300, outcome))
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,pushed_sha) "
           "VALUES(?,'b','h','t','pushed',?,?,'abc123')", (fix, now, now))
    r = effortab.report(p, days=30)
    m, h = r["arms"]["medium"], r["arms"]["high"]
    assert (m["changes"], m["landed"], m["runs"], m["usd"], m["usd_per_landed"]) == (1, 1, 2, 3.0, 3.0)
    assert (m["output_per_run"], m["calls_per_run"]) == (8000, 15.0)
    assert (m["reviews"], m["review_fail_pct"], m["checks"], m["checks_fail_pct"]) == (2, 50.0, 2, 50.0)
    assert (m["fix_hops"], m["fix_hops_per_change"], m["tier_ups"]) == (1, 1.0, 0)
    assert (h["changes"], h["landed"], h["runs"], h["usd"], h["usd_per_landed"]) == (1, 0, 2, 8.0, None)
    assert (h["calls_per_run"], h["review_fail_pct"], h["checks"], h["checks_fail_pct"], h["tier_ups"]) == \
        (30.0, 0.0, 1, 0.0, 1)
    assert r["verdict"] == "collecting"
    text = effortab.report_text(r)
    assert "medium 1 changes, 1 landed, 2 runs, $3.00; $3.0/landed change" in text and "still collecting" in text
    # End rule: 60+ landed changes per arm; medium wins only within 3 points on both fail rates.
    arms = {"high": {"landed": 60, "review_fail_pct": 10.0, "checks_fail_pct": 5.0},
            "medium": {"landed": 60, "review_fail_pct": 13.0, "checks_fail_pct": 8.0}}
    assert effortab.verdict(arms) == "medium"
    assert effortab.verdict({**arms, "medium": {**arms["medium"], "checks_fail_pct": 8.1}}) == "high"
    assert effortab.verdict({**arms, "medium": {**arms["medium"], "review_fail_pct": 13.5}}) == "high"
    assert effortab.verdict({**arms, "high": {**arms["high"], "landed": 59}}) == "collecting"
    # The CLI prints it.
    from ttp import cli
    cli.main(["stats", "demo", "--effort-ab", "--days", "30", "--json"])
    assert json.loads(capsys.readouterr().out)["arms"]["medium"]["landed"] == 1


def test_jev_effort_pick_is_off_by_default_picks_once_per_lineage_and_logs_the_pick(env):
    from ttp import effortab
    p = make(env)
    cfg = p.config()
    t = p.db.add_task("Rename a flag", "Rename --foo to --bar.", kind="code", tier="standard",
                      origin="coordinator", provider="claude")
    jev = _EffortJev(0.2)
    assert effortab.assign(p.db, cfg, p.db.task(t), "standard", "claude", jev) is None and jev.calls == 0
    cfg["effort_ab"] = {"enabled": False, "jev_pick": True}
    got = effortab.assign(p.db, cfg, p.db.task(t), "standard", "claude", jev)
    assert got == {"by": "jev", "pick": "medium", "score": 0.2, "jev_call": got["jev_call"], "root": t,
                   "effort": "medium"} and jev.calls == 1
    row = p.db.one("SELECT * FROM jev_calls WHERE id=?", (got["jev_call"],))
    assert row["use"] == "effort" and row["ref"] == f"task:{t}" and row["changed"] == 1
    assert json.loads(row["decision"])["effort_level"] == "medium"
    # Its run notes the pick; a retry or a fix of the lineage reuses it with no second call.
    p.db.x("INSERT INTO runs(task,role,provider,status,started,note) VALUES(?,'worker','claude','ok',?,?)",
           (t, time.time(), json.dumps({"effort_ab": got})))
    fix = p.db.add_task("Fix review #5: rename", "f", kind="code", tier="standard", origin="daemon",
                        labels=[f"continues:{t}", "review_fix:5"])
    again = effortab.assign(p.db, cfg, p.db.task(fix), "standard", "claude", jev)
    assert again["pick"] == "medium" and again["effort"] == "medium" and jev.calls == 1
    # The pick is settled with the task's outcome (effort.settle).
    from ttp import effort
    effort.settle(p.db, t, "done", 1)
    assert p.db.one("SELECT outcome FROM jev_calls WHERE id=?", (got["jev_call"],))["outcome"] == "right"
    # A hard spec stays high; Jev off or no answer falls back to the tier's effort.
    hard = p.db.add_task("Redesign the scheduler", "Make it fair.", kind="code", tier="standard", origin="coordinator")
    assert effortab.assign(p.db, cfg, p.db.task(hard), "standard", "claude", _EffortJev(2.5))["effort"] == "high"
    other = p.db.add_task("Other", "s", kind="code", tier="standard", origin="coordinator")
    assert effortab.assign(p.db, cfg, p.db.task(other), "standard", "claude", None) == \
        {"by": "tier", "root": other, "effort": "high"}
    # The A/B, once on, takes the lineage over: no Jev call.
    cfg["effort_ab"]["enabled"] = True
    jev.calls = 0
    assert "arm" in effortab.assign(p.db, cfg, p.db.task(other), "standard", "claude", jev) and jev.calls == 0
