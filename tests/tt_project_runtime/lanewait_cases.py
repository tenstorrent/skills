# Task-to-task wait tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new lanewait.py tests (early start on an approved head, rebase notes,
# the lane_wait event) here.
from __future__ import annotations


def _approved_producer(p, status="approved"):
    """A done code task whose branch has a commit of its own and a push queue row (approved or batched)
    holding that head. Returns (producer id, head, row id)."""
    import subprocess
    prod = p.db.add_task("producer", "s", kind="code", origin="user")
    branch = f"ttp/t{prod}-producer"
    git = ["git", "-C", str(p.root)]
    subprocess.run(git + ["branch", branch], check=True, capture_output=True)
    wt = p.root.parent / f"lw-{prod}"
    subprocess.run(git + ["worktree", "add", "-q", str(wt), branch], check=True, capture_output=True)
    (wt / "feature.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "feature"], check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    subprocess.run(git + ["worktree", "remove", "--force", str(wt)], check=True, capture_output=True)
    p.db.update_task(prod, status="done", branch=branch)
    now = time.time()
    row = p.db.x("INSERT INTO push_queue(task,branch,head,target,status,batch,created,updated) "
                 "VALUES(?,?,?,?,?,?,?,?)", (prod, branch, head, "origin/main", status,
                                             "b1" if status == "batched" else None, now, now))
    return prod, head, row


@pytest.mark.parametrize("status", ["approved", "batched"])
def test_a_landed_wait_starts_on_the_producers_approved_head(env, monkeypatch, status):
    """Criterion 1: a start_when landed:#X is met once X's change is approved or batched in the push
    queue, and the dependent task's worktree branches from X's approved head, not the base."""
    from ttp import daemon as dmod, lanewait, worktree
    from ttp.db import deferral
    p = make(env)
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    prod, head, _ = _approved_producer(p, status)
    dep = p.db.add_task("after it", "s", kind="code", status="queued",
                        labels=[f"start_when:landed:#{prod}", f"deferred_since:{time.time():.0f}"])
    d = dmod.Daemon(p.base)
    d.probe_deferred(time.time())
    assert _ready(p, dep), "it starts without waiting for the batch and the landing"
    t = p.db.task(dep)
    assert lanewait.label_value(t, lanewait.START_FROM) == head and not deferral(t)
    path, _ = worktree.ensure(p, t)
    got = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert got == head and (path / "feature.txt").exists()


@pytest.mark.parametrize("kind", ["ops", "plan", "work", "review", "harness"])
def test_a_task_without_its_own_worktree_waits_for_the_real_landing(env, monkeypatch, kind):
    """Only a task that branches from the approved head starts early: any other kind would run on code
    without X, so it stays deferred on the landed probe while X is only approved."""
    from ttp import daemon as dmod
    from ttp.db import deferral
    p = make(env)
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    prod, _, _ = _approved_producer(p)
    dep = p.db.add_task("check it", "s", kind=kind, status="queued",
                        labels=[f"start_when:landed:#{prod}", f"deferred_since:{time.time():.0f}"])
    dmod.Daemon(p.base).probe_deferred(time.time())
    assert not _ready(p, dep) and deferral(p.db.task(dep)), f"a {kind} task waits for the landing"


def test_a_carried_branch_task_waits_for_the_real_landing(env, monkeypatch):
    """A code task carrying a branch works on that branch, not the approved head: no early start."""
    from ttp import daemon as dmod
    from ttp.db import deferral
    p = make(env)
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    prod, _, _ = _approved_producer(p)
    dep = p.db.add_task("fix pr", "s", kind="code", status="queued",
                        labels=[f"start_when:landed:#{prod}", f"deferred_since:{time.time():.0f}",
                                "pr_branch:feature/x"])
    dmod.Daemon(p.base).probe_deferred(time.time())
    assert not _ready(p, dep) and deferral(p.db.task(dep))


def test_a_held_or_missing_push_row_keeps_the_landed_wait(env, monkeypatch):
    """A row held for a token scope, or none at all, is no early start; neither is a longer probe."""
    from ttp import lanewait
    p = make(env)
    prod, _, row = _approved_producer(p)
    assert lanewait.batched(p.db, f"landed:#{prod} && test -e x") is None
    assert lanewait.batched(p.db, f"ttp landed --task {prod}") is None
    p.db.x("UPDATE push_queue SET status='held' WHERE id=?", (row,))
    assert lanewait.batched(p.db, f"landed:#{prod}") is None
    p.db.x("UPDATE push_queue SET status='pushed' WHERE id=?", (row,))
    assert lanewait.batched(p.db, f"landed:#{prod}") is None


@pytest.mark.parametrize("sent", ["conflict", "check_failed", "refused", "cancelled"])
def test_a_sent_back_batch_gives_the_early_task_a_rebase_note_not_a_cancel(env, monkeypatch, sent):
    """Criterion 2: X's row is sent back after the dependent task started on its head: the task keeps
    running and gets one rebase note, in its spec and its running run's steer.md."""
    from ttp import daemon as dmod, lanewait
    p = make(env)
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    prod, head, row = _approved_producer(p)
    dep = p.db.add_task("after it", "s", kind="code", status="queued", labels=[f"start_when:landed:#{prod}"])
    d = dmod.Daemon(p.base)
    d.probe_deferred(time.time())
    p.db.update_task(dep, status="running")
    run_dir = p.state / "runs" / "lw1"
    run_dir.mkdir(parents=True)
    p.db.x("INSERT INTO runs(task,role,status,started,dir) VALUES(?,'worker','running',?,?)",
           (dep, time.time(), str(run_dir)))
    d.probe_deferred(time.time())
    assert "Rebase note" not in p.db.task(dep)["spec"], "still approved: no note"
    p.db.x("UPDATE push_queue SET status=? WHERE id=?", (sent, row))
    for _ in range(2):
        d.probe_deferred(time.time())
    t = p.db.task(dep)
    assert t["status"] == "running" and t["spec"].count("Rebase note") == 1, t["spec"]
    assert f"#{prod}'s change was sent back from the push queue ({sent})" in t["spec"]
    assert head[:12] in t["spec"] and lanewait.label_value(t, lanewait.EARLY) is None
    assert (run_dir / "steer.md").read_text().count("Rebase note") == 1


def test_a_shipped_row_drops_the_early_label_without_a_note(env):
    from ttp import lanewait
    p = make(env)
    prod, head, row = _approved_producer(p)
    dep = p.db.add_task("after it", "s", kind="code", status="running",
                        labels=lanewait.early_labels([], (prod, row, head)))
    p.db.x("UPDATE push_queue SET status='landed' WHERE id=?", (row,))
    assert lanewait.rebase_notes(p.db) == []
    t = p.db.task(dep)
    assert "Rebase note" not in t["spec"] and lanewait.label_value(t, lanewait.EARLY) is None


@pytest.mark.parametrize("how", ["landed", "depends_on", "waits_end"])
def test_a_lane_wait_past_the_threshold_raises_one_event_cleared_on_start(env, how):
    """Criterion 3: a queued task waiting on another task's lane past the threshold raises exactly one
    lane_wait event naming both tasks, the holder's status and the hours; it clears once the task starts."""
    from ttp import lanewait
    p = make(env)
    holder = p.db.add_task("the holder", "s", kind="code", status="running")
    t0 = time.time() - 3 * 3600
    labels = {"landed": [f"start_when:landed:#{holder}", f"deferred_since:{t0:.0f}"],
              "depends_on": [], "waits_end": [f"waits_end:{holder}"]}[how]
    dep = p.db.add_task("the waiter", "s", kind="code", status="queued", labels=labels,
                        depends_on=[holder] if how == "depends_on" else None)
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (t0, dep))
    cfg = {"waiting": {"lane_wait_event_s": 600}}
    for _ in range(3):
        lanewait.check(p.db, cfg, time.time())
    ev = p.db.q("SELECT * FROM events WHERE kind='lane_wait'")
    assert len(ev) == 1 and ev[0]["status"] == "queued" and ev[0]["task"] == dep
    assert f"#{dep} the waiter" in ev[0]["text"] and f"#{holder} the holder (running" in ev[0]["text"]
    assert "3.0 h" in ev[0]["text"], ev[0]["text"]
    p.db.update_task(dep, status="running")
    lanewait.check(p.db, cfg, time.time())
    assert p.db.one("SELECT status FROM events WHERE id=?", (ev[0]["id"],))["status"] == "handled"
    assert not p.db.kv(lanewait.KEY, {})


def test_a_lane_wait_under_the_threshold_or_off_raises_nothing(env):
    from ttp import lanewait
    p = make(env)
    holder = p.db.add_task("the holder", "s", kind="code", status="running")
    dep = p.db.add_task("the waiter", "s", kind="work", status="queued", depends_on=[holder])
    lanewait.check(p.db, {}, time.time() + 300)
    lanewait.check(p.db, {"waiting": {"lane_wait_event_s": 0}}, time.time() + 9000)
    assert not p.db.q("SELECT * FROM events WHERE kind='lane_wait'")
    lanewait.check(p.db, {}, time.time() + 700)
    assert len(p.db.q("SELECT * FROM events WHERE kind='lane_wait' AND task=?", (dep,))) == 1


@pytest.mark.parametrize("labels", [["start_when:test -f secrets.env"],
                                    ["start_when:ttp hours --check"],
                                    ["start_after:{later}"]])
def test_a_wait_on_a_user_only_condition_raises_no_lane_wait(env, labels):
    """Criterion 4: a start_when naming no task (a secret, a sign-in, a cadence), even with a
    dependency also open, or a start_after still ahead, is no lane wait."""
    from ttp import lanewait
    p = make(env)
    holder = p.db.add_task("the holder", "s", kind="code", status="running")
    later = time.time() + 86400
    dep = p.db.add_task("the waiter", "s", kind="code", status="queued", depends_on=[holder],
                        labels=[lb.format(later=f"{later:.0f}") for lb in labels])
    if labels[0].startswith("start_after"):
        p.db.update_task(dep, not_before=later)
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (time.time() - 86400, dep))
    lanewait.check(p.db, {}, time.time())
    assert not p.db.q("SELECT * FROM events WHERE kind='lane_wait'")


def test_the_lane_wait_threshold_defaults_to_600_s_from_config(env):
    """Criterion 5: waiting.lane_wait_event_s, default 600, in the project defaults too."""
    from ttp import lanewait
    p = make(env)
    assert p.config()["waiting"]["lane_wait_event_s"] == 600
    assert lanewait.threshold({}) == 600 == lanewait.threshold(p.config())
    assert lanewait.threshold({"waiting": {"lane_wait_event_s": 120}}) == 120


def test_an_approved_head_that_does_not_resolve_here_branches_from_the_base(env):
    """START_FROM naming a commit this repo lacks (or no sha at all) falls back to the base."""
    from ttp import lanewait, worktree
    p = make(env)
    base = subprocess.run(["git", "-C", str(p.root), "rev-parse", worktree.resolve_base(p)], capture_output=True,
                          text=True).stdout.strip()
    for i, sha in enumerate(["0123456789abcdef0123456789abcdef01234567", "not-a-sha"]):
        dep = p.db.add_task(f"after it {i}", "s", kind="code", status="running",
                            labels=[f"{lanewait.START_FROM}{sha}"])
        assert worktree.early_head(p, p.db.task(dep)) is None
        path, _ = worktree.ensure(p, p.db.task(dep))
        got = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True)
        assert got.stdout.strip() == base
