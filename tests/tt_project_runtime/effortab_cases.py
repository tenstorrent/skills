# Jev's effort pick (effortab) tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there). Add new effort pick tests here, not there.
from __future__ import annotations


def _effort_pick_on(p):
    cfg = p.config()
    cfg["effort_ab"] = {"jev_pick": True}
    return cfg


def _effort_task(p, **kw):
    """A new code task queued at standard."""
    return p.db.add_task("Port the parser", "Make it faster.", kind="code", tier="standard",
                         origin="coordinator", provider="claude", budget_usd=8.0, **kw)


def test_jev_effort_pick_takes_the_lineage_root_and_skips_pinned_or_other_tasks(env):
    from ttp import coordinator as coord, effortab
    from ttp.project import DEFAULT_CONFIG
    p = make(env)
    assert DEFAULT_CONFIG["effort_ab"] == {"jev_pick": False}
    assert coord.settable("effort_ab.jev_pick")("true") is True
    med = _effort_task(p)
    # Off by default: no pick, the tier's effort.
    assert effortab.assign(p.db, p.config(), p.db.task(med), "standard", "claude", _EffortJev(0.2)) is None
    cfg = _effort_pick_on(p)

    def pick(t, tier="standard", c=cfg):
        got = effortab.assign(p.db, c, p.db.task(t), tier, "claude", _EffortJev(0.2))
        return got and {k: got[k] for k in ("by", "pick", "root", "effort") if k in got}
    assert pick(med) == {"by": "jev", "pick": "medium", "root": med, "effort": "medium"}
    # A deep retry or a light wake records the pick at its tier's effort.
    assert pick(med, "deep")["effort"] == "max" and pick(med, "light")["effort"] == "low"
    # A review fix and a continuation hop of the lineage (continues:, the daemon's labels) take its root;
    # a hop of a hop too.
    fix = p.db.add_task("Fix review #900: Port the parser", "f", kind="code", tier="standard", origin="daemon",
                        labels=[f"continues:{med}", "review_fix:900"])
    hop = p.db.add_task("continue: port the parser", "c", kind="code", tier="standard", origin="coordinator",
                        labels=[f"continues:{fix}"])
    for t in (fix, hop):
        assert pick(t)["root"] == med
    # Never picked: a task with its own effort pin (or a fix of a pinned root), one queued at deep or
    # light, a review, a work task.
    pinned = _effort_task(p, effort="high")
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
    assert pick(med)["effort"] == "xhigh"


def test_effort_pick_goes_in_the_run_note_and_the_run_starts_at_its_effort(env, monkeypatch):
    from ttp import daemon as dmod, effortab
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("effort_ab.jev_pick", True)
    p.set_config("dispatch.batch", False)   # each task its own run: batch runs (1ed160fc4) would fold them into one
    d = Daemon(p.base)
    d.jev = None
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.base), None))
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append((a, k)) or 0)
    med = _effort_task(p)
    d.dispatch()
    (args, kw), = [(a, k) for a, k in started if k["task"]["id"] == med]
    # Jev off: the tier's effort, noted.
    assert args[3] == "standard" and kw["note"][effortab.KEY] == {"by": "tier", "root": med, "effort": "high"}
    monkeypatch.undo()

    class Proc:   # never a real runner
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    d = Daemon(p.base)
    ab = {"by": "jev", "pick": "medium", "root": med, "effort": "medium"}
    rid = d.start_run("worker", "go", "claude", "standard", str(p.base), task=p.db.task(med), note={effortab.KEY: ab})
    row = p.db.one("SELECT effort, note FROM runs WHERE id=?", (rid,))
    assert row["effort"] == "medium" and json.loads(row["note"])[effortab.KEY] == {**ab, "effective": "medium"}
    # A task's own effort pin always wins over a note's pick.
    pinned = p.db.add_task("Pinned", "s", kind="code", tier="standard", origin="coordinator", effort="xhigh")
    rid = d.start_run("worker", "go", "claude", "standard", str(p.base), task=p.db.task(pinned), note={effortab.KEY: ab})
    assert p.db.one("SELECT effort FROM runs WHERE id=?", (rid,))["effort"] == "xhigh"
    # No pick: the tier's effort.
    rid = d.start_run("worker", "go", "claude", "standard", str(p.base), task=p.db.task(med))
    assert p.db.one("SELECT effort FROM runs WHERE id=?", (rid,))["effort"] == "high"


def test_a_conclusion_run_the_tier_floor_lifts_logs_its_effective_effort_and_settles_the_pick(env, monkeypatch):
    """A conclusion run whose pick is medium under a high floor runs at high: its note records the pick's
    effort and the effective one, and the lifted Jev pick settles as no change rather than scored right
    or wrong."""
    from ttp import daemon as dmod, effort, effortab, jevuse

    class Proc:   # never a real runner
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    p = make(env)
    d = dmod.Daemon(p.base)

    def start(task, ab, **kw):
        rid = d.start_run("worker", "go", "claude", "standard", str(p.base), task=p.db.task(task),
                          note={effortab.KEY: ab}, **kw)
        p.db.x("UPDATE runs SET status='ok', cost_usd=2 WHERE id=?", (rid,))
        row = p.db.one("SELECT effort, note FROM runs WHERE id=?", (rid,))
        return row["effort"], json.loads(row["note"])[effortab.KEY]

    # Jev's medium pick under the floor never ran: no change, not scored right when the task ends.
    picked = p.db.add_task("Plan it", "conclusion: the plan", kind="code", tier="standard", origin="coordinator")
    cid = jevuse.record(p.db, "effort", {"effort_level": "medium"}, 0.01, avoided_usd=0.5,
                        ref=f"task:{picked}", changed=True)
    pick = {"by": "jev", "pick": "medium", "jev_call": cid, "root": picked, "effort": "medium"}
    assert start(picked, pick, min_effort="high") == ("high", {**pick, "effective": "high", "lifted": True})
    effort.settle(p.db, picked, "done", 1)
    assert p.db.one("SELECT outcome FROM jev_calls WHERE id=?", (cid,))["outcome"] == jevuse.NO_CHANGE
    assert jevuse.stats(p.db, p.config())["effort"]["saved"] == 0.0
    # An unlifted pick is still scored when the task ends.
    other = p.db.add_task("Fix it", "s", kind="code", tier="standard", origin="coordinator")
    cid2 = jevuse.record(p.db, "effort", {"effort_level": "medium"}, 0.01, ref=f"task:{other}", changed=True)
    start(other, {**pick, "jev_call": cid2, "root": other}, min_effort="low")
    effort.settle(p.db, other, "done", 1)
    assert p.db.one("SELECT outcome FROM jev_calls WHERE id=?", (cid2,))["outcome"] == "right"


def test_jev_effort_pick_is_off_by_default_picks_once_per_lineage_and_logs_the_pick(env):
    from ttp import effortab
    p = make(env)
    cfg = p.config()
    t = p.db.add_task("Rename a flag", "Rename --foo to --bar.", kind="code", tier="standard",
                      origin="coordinator", provider="claude")
    jev = _EffortJev(0.2)
    # Off by default: Jev is never asked.
    assert effortab.assign(p.db, cfg, p.db.task(t), "standard", "claude", jev) is None and jev.calls == 0
    cfg["effort_ab"] = {"jev_pick": True}
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
