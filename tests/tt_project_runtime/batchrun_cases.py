# Batch run tests (batchrun.py) of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new tests of batch runs here.
from __future__ import annotations


def _batch_tasks(p, n=3, **kw):
    """`n` queued small code tasks; returns their rows."""
    ids = [p.db.add_task(f"small fix {i}", f"Fix it {i}.", kind="code", tier="standard",
                         origin="coordinator", **kw) for i in range(n)]
    return [p.db.task(i) for i in ids]


def _batch_daemon(p, monkeypatch, branch="ttp/t-lead"):
    """A daemon whose code tasks start in one fixed worktree and whose runs are only recorded."""
    from ttp.daemon import Daemon
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    d = Daemon(p.base)
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.base), branch))
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append((a, k)) or 0)
    return d, started


def test_batch_pick_takes_eligible_small_code_tasks(env):
    from ttp import batchrun
    p = make(env)
    lead, *rest = _batch_tasks(p, 4)
    got = batchrun.pick(p, p.config(), lead, [lead, *rest])
    assert [t["id"] for t in got] == [t["id"] for t in rest]
    # batch_max caps it; off takes none; a non-code lead takes none.
    cfg = p.config()
    cfg["dispatch"] = {"batch": True, "batch_max": 2}
    assert len(batchrun.pick(p, cfg, lead, rest)) == 2
    cfg["dispatch"] = {"batch": False, "batch_max": 5}
    assert batchrun.pick(p, cfg, lead, rest) == []
    assert batchrun.pick(p, p.config(), dict(lead, kind="work"), rest) == []


def test_batch_pick_leaves_out_resourced_deep_pinned_and_held(env):
    from ttp import batchrun
    p = make(env)
    lead, ok = _batch_tasks(p, 2)
    deep = dict(ok, id=900, tier="deep")
    dev = dict(ok, id=901, labels=json.dumps(["resource:box-device"]))
    user_deep = dict(ok, id=902, labels=json.dumps(["user_deep"]))
    pinned = dict(ok, id=903, model="other-model")
    own = dict(ok, id=904, branch="ttp/t904")
    fix = dict(ok, id=905, spec="pr_branch: ttp/t1\nfix it")
    work = dict(ok, id=906, kind="work")
    other_repo = dict(ok, id=907, labels=json.dumps(["pr_fix:org/other#3"]))
    spent = dict(ok, id=908, budget_usd=2.0, spent_usd=2.0)
    running = dict(ok, id=909, status="running")
    urgent = dict(ok, id=912, priority=1)   # urgent work runs in a worker of its own, in parallel
    got = batchrun.pick(p, p.config(), lead, [deep, dev, user_deep, pinned, own, fix, work, other_repo,
                                              spent, running, urgent, ok])
    assert [t["id"] for t in got] == [ok["id"]]
    # A resourced lead takes no batch.
    assert batchrun.pick(p, p.config(), dict(lead, labels=json.dumps(["exclusive:box"])), [ok]) == []
    # A task whose files a running task on another branch changes stays out; on the lead's branch it may join.
    (p.root / "pkg").mkdir(exist_ok=True)
    (p.root / "pkg" / "mod.py").write_text("x = 1\n")
    near = dict(ok, id=910, spec="Change pkg/mod.py.")
    busy = {"id": 911, "spec": "Rework pkg/mod.py.", "branch": "ttp/t911"}
    assert batchrun.pick(p, p.config(), lead, [near], inflight=[busy]) == []
    assert [t["id"] for t in batchrun.pick(p, p.config(), dict(lead, branch="ttp/t911"), [near],
                                           inflight=[busy])] == [910]


def test_batch_pick_orders_related_priority_and_dependency(env):
    from ttp import batchrun
    p = make(env)
    (p.root / "pkg").mkdir(exist_ok=True)
    (p.root / "pkg" / "mod.py").write_text("x = 1\n")
    lead, a, b, c = _batch_tasks(p, 4)
    lead = dict(lead, spec="Fix pkg/mod.py.")
    a = dict(a, priority=2)
    b = dict(b, priority=3, spec="Also touch pkg/mod.py.")   # related: first, whatever its priority
    c = dict(c, priority=3, depends_on=json.dumps([a["id"]]))
    late = dict(c, id=a["id"] - 1, priority=2, depends_on=json.dumps([c["id"]]))   # after what it needs
    got = [t["id"] for t in batchrun.pick(p, p.config(), lead, [c, a, b])]
    assert got == [b["id"], a["id"], c["id"]]
    assert [t["id"] for t in batchrun.order([late, c])] == [c["id"], late["id"]]


def test_batch_run_shares_one_worktree_and_branch(env, monkeypatch):
    from ttp import batchrun
    p = make(env)
    d, started = _batch_daemon(p, monkeypatch)
    urgent = p.db.add_task("urgent fix", "Fix now.", kind="code", tier="standard", origin="user", priority=1,
                           budget_usd=8.0)
    mates = [t["id"] for t in _batch_tasks(p, 2, budget_usd=4.0)]
    deep = p.db.add_task("big redesign", "Redo it.", kind="code", tier="deep", origin="coordinator")
    d.dispatch()
    lead_runs = [(a, k) for a, k in started if k["task"]["id"] == urgent]
    (args, kw), = lead_runs   # the urgent task leads, and its batch is one run
    assert kw["note"]["batch"] == {"id": f"b{urgent}", "members": mates}
    assert f"## Batch run b{urgent}" in args[1] and all(f"### Task #{m}:" in args[1] for m in mates)
    assert "Task: #" in args[1]
    assert kw["budget_usd"] == pytest.approx(16.0)   # 8 + 4 + 4, under the deep cap
    assert all(k["task"]["id"] not in mates for _, k in started)   # no second run for a member
    for m in mates:
        t = p.db.task(m)
        assert t["status"] == "running" and t["branch"] == "ttp/t-lead"
        assert t["blocked_reason"] == f"in batch run b{urgent} led by #{urgent}"
    assert p.db.task(deep)["status"] != "running" or not batchrun.members_of(p.db, deep)
    assert batchrun.view(p.db, urgent) == {"id": f"b{urgent}", "members": mates}
    assert batchrun.words(batchrun.view(p.db, urgent)) == f" (batch b{urgent}: +#{mates[0]}, #{mates[1]})"
    assert batchrun.members_of(p.db, mates[1])["lead"] == urgent
    # Members are not orphans while the lead runs: reconcile leaves them running.
    p.db.x("INSERT INTO runs(task,role,provider,status,started) VALUES(?,'worker','claude','running',?)",
           (urgent, time.time()))
    d.reconcile_tasks()
    assert all(p.db.task(m)["status"] == "running" for m in mates)
    # A wake of the lead never forms a second batch.
    assert d._batch_members(p.db.task(urgent), [p.db.task(deep)], set(), {}) == []


def test_batch_settle_isolates_failed_and_blocked_members(env, monkeypatch):
    from ttp import batchrun
    from ttp.db import dump_result, load_result
    p = make(env)
    d, started = _batch_daemon(p, monkeypatch)
    lead, ok, bad, stuck, quiet = _batch_tasks(p, 5)
    d.dispatch()
    (_, kw), = started
    assert kw["note"]["batch"]["members"] == [ok["id"], bad["id"], stuck["id"], quiet["id"]]
    members = [{"id": ok["id"], "status": "done", "summary": "fixed it", "followups": ["later: x"]},
               {"id": f"#{bad['id']}", "status": "failed", "reason": "test kept failing"},
               {"id": stuck["id"], "status": "blocked", "reason": "needs a decision"}]
    p.db.update_task(lead["id"], status="waiting")
    assert batchrun.tend(p) == []   # a waiting lead still holds them
    p.db.update_task(lead["id"], status="done", result=dump_result({"status": "done", "members": members}))
    d.reconcile_tasks()
    t = p.db.task(ok["id"])
    assert t["status"] == "done" and t["branch"] == "ttp/t-lead"
    assert load_result(t["result"])["summary"] == "fixed it"
    assert load_result(t["result"])["batch"]["lead"] == lead["id"]
    t = p.db.task(bad["id"])   # failed: back to the queue alone, off the batch's branch
    assert t["status"] == "queued" and t["branch"] is None and "test kept failing" in load_result(t["result"])["summary"]
    t = p.db.task(stuck["id"])
    assert t["status"] == "blocked" and t["blocked_reason"] == "needs a decision"
    t = p.db.task(quiet["id"])   # no outcome: queued again
    assert t["status"] == "queued" and "no outcome" in load_result(t["result"])["summary"]
    # The record stays readable for the batch's review and checks, never settles twice, and shows no live run.
    rec = batchrun.members_of(p.db, ok["id"], settled=True)
    assert rec["outcomes"] == {str(ok["id"]): "done", str(bad["id"]): "queued", str(stuck["id"]): "blocked",
                               str(quiet["id"]): "queued"}
    assert batchrun.members_of(p.db, ok["id"]) is None and batchrun.view(p.db, lead["id"]) is None
    assert batchrun.settle(p, lead["id"]) == []
    # A failed lead sends every member back.
    lead2, m2 = _batch_tasks(p, 2)
    batchrun.record(p.db, lead2, [m2], "ttp/t-two", p.config())
    p.db.update_task(m2["id"], status="running", branch="ttp/t-two")
    p.db.update_task(lead2["id"], status="failed")
    assert batchrun.tend(p) == [(m2["id"], "queued")]
    assert p.db.task(m2["id"])["branch"] is None
    # Outcomes handed off in a waiting hand-off's on_pass count, read from the run's result.json.
    lead3, m3 = _batch_tasks(p, 2)
    batchrun.record(p.db, lead3, [m3], "ttp/t-three", p.config())
    p.db.update_task(m3["id"], status="running", branch="ttp/t-three")
    run_dir = env["tmp"] / "run-batch-three"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "waiting", "on_pass": {
        "status": "done", "members": [{"id": m3["id"], "status": "done", "summary": "ok"}]}}))
    p.db.x("INSERT INTO runs(task,role,provider,status,started,dir) VALUES(?,'worker','claude','ok',?,?)",
           (lead3["id"], time.time(), str(run_dir)))
    p.db.update_task(lead3["id"], status="done")
    assert batchrun.tend(p) == [(m3["id"], "done")]


def test_batch_cost_split_pro_rata(env):
    from ttp import batchrun
    p = make(env)
    lead, a, b = _batch_tasks(p, 3)
    p.db.update_task(lead["id"], budget_usd=8.0)
    p.db.update_task(a["id"], budget_usd=4.0)
    lead, a = p.db.task(lead["id"]), p.db.task(a["id"])
    assert batchrun.split_cost(9.0, {1: 6.0, 2: 3.0}) == {1: 6.0, 2: 3.0}
    assert batchrun.split_cost(2.0, {1: 0.0, 2: 0.0}) == {1: 1.0, 2: 1.0}
    # b has no budget: its tier's default (standard 8) weighs it.
    batchrun.record(p.db, lead, [a, b], "ttp/t-lead", p.config())
    p.db.update_task(lead["id"], spent_usd=10.0)   # the run's cost, booked to the lead as usual
    shares = batchrun.book(p.db, lead["id"], 10.0)
    assert shares == pytest.approx({lead["id"]: 4.0, a["id"]: 2.0, b["id"]: 4.0})
    assert p.db.task(lead["id"])["spent_usd"] == pytest.approx(4.0)
    assert p.db.task(a["id"])["spent_usd"] == pytest.approx(2.0)
    assert p.db.task(b["id"])["spent_usd"] == pytest.approx(4.0)
    assert batchrun.batch_of(p.db, lead["id"])["spent"] == {str(lead["id"]): 4.0, str(a["id"]): 2.0,
                                                           str(b["id"]): 4.0}
    # The run's budget: the members' remaining budgets added, capped at the deep tier's default.
    a = p.db.task(a["id"])
    assert batchrun.budget(p.config(), lead, 8.0, [a]) == pytest.approx(10.0)   # a spent 2 of 4
    assert batchrun.budget(p.config(), lead, 8.0, [a, b, b, b]) == pytest.approx(25.0)


def test_batch_never_leads_or_takes_a_task_with_a_branch_of_its_own():
    # A PR's or another task's branch never collects other tasks' commits: a task with a pr_branch
    # line or label, a PR fix, a continuation or an open PR neither leads nor joins; a lead's own
    # recorded branch is its batch's.
    from ttp import batchrun
    plain = {"id": 1, "labels": "[]", "spec": "s"}
    assert not batchrun.own_branch(plain)
    assert not batchrun.own_branch(dict(plain, branch="ttp/t1-x"), recorded=False)
    assert batchrun.own_branch(dict(plain, branch="ttp/t1-x"))
    assert batchrun.own_branch(dict(plain, pr_url="https://example.invalid/pull/1"), recorded=False)
    assert batchrun.own_branch(dict(plain, spec="pr_branch: feat/x"), recorded=False)
    for lb in ("pr_branch:feat/x", "pr_fix:acme/widgets#7", "continues:5"):
        assert batchrun.own_branch(dict(plain, labels=f'["{lb}"]'), recorded=False), lb
