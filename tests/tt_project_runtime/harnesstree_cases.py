# Harness task worktree branch tests of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there): helpers, fixtures and imports come from that file. A
# harness task's worktree is on its own branch ttp/harness-t<id>, started from the harness's main line
# whatever the checkout has checked out, and work on another branch is never reported as delivered.
from __future__ import annotations


def _ht_park_checkout_on(p, name):
    """Leave the harness checkout on branch `name` (made from main) with one commit main lacks."""
    _hgit(p.harness, "checkout", "-q", "-b", name)
    _commit_in_harness_tree(p.harness, "prompts/parked.md", "parked\n", "parked work")
    return _hgit(p.harness, "rev-parse", "HEAD").strip()


def test_a_harness_worktree_starts_its_own_branch_from_main_when_the_checkout_is_on_another_tasks(env):
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    main = _hgit(p.harness, "rev-parse", "main").strip()
    stray = _ht_park_checkout_on(p, f"ttp/harness-t{a}")
    # Nothing runs in the checkout, but it is not on main: b does not work there, nor branch from it.
    own = harnesstree.workdir(p, b)
    assert own == harnesstree.path(p, b) and own != p.harness
    assert _hgit(own, "symbolic-ref", "--short", "HEAD").strip() == f"ttp/harness-t{b}"
    assert _hgit(own, "rev-parse", "HEAD").strip() == main
    assert subprocess.run(["git", "-C", str(own), "merge-base", "--is-ancestor", stray, "HEAD"]).returncode == 1
    assert _hgit(p.harness, "symbolic-ref", "--short", "HEAD").strip() == f"ttp/harness-t{a}"   # left alone


def test_a_busy_checkout_on_a_side_branch_never_seeds_a_harness_worktree(env):
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    main = _hgit(p.harness, "rev-parse", "main").strip()
    _ht_park_checkout_on(p, "side")   # not a task branch: a busy checkout there still gives main
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)
    assert _hgit(own, "symbolic-ref", "--short", "HEAD").strip() == f"ttp/harness-t{b}"
    assert _hgit(own, "rev-parse", "HEAD").strip() == main


def test_an_existing_harness_task_branch_is_reused_with_its_commits(env):
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)
    _commit_in_harness_tree(own, "prompts/kept.md", "kept\n", "b: earlier run")
    head = _hgit(own, "rev-parse", "HEAD").strip()
    _hgit(p.harness, "worktree", "remove", str(own))   # gone, its branch stays (a retry or continue)
    assert harnesstree.workdir(p, b) == own
    assert _hgit(own, "symbolic-ref", "--short", "HEAD").strip() == f"ttp/harness-t{b}"
    assert _hgit(own, "rev-parse", "HEAD").strip() == head


def test_a_harness_worktree_on_the_wrong_branch_is_refused_and_the_task_not_started_there(env):
    p = make(env)
    from ttp import harnesstree
    from ttp.daemon import Daemon
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)
    _hgit(own, "checkout", "-q", "-b", f"ttp/harness-t{a}-copy")
    with pytest.raises(RuntimeError, match=rf"on ttp/harness-t{a}-copy, not its own branch ttp/harness-t{b}"):
        harnesstree.workdir(p, b)
    with pytest.raises(RuntimeError, match="not its own branch"):
        Daemon(p.base)._workdir_for({"id": b, "kind": "harness"})
    _hgit(own, "checkout", "-q", "--detach")
    with pytest.raises(RuntimeError, match="on a detached HEAD"):
        harnesstree.workdir(p, b)
    _hgit(own, "checkout", "-q", f"ttp/harness-t{b}")
    assert harnesstree.workdir(p, b) == own


def test_a_harness_worktree_made_on_another_branch_is_refused(env, monkeypatch):
    """Whatever git did, the worktree's branch is checked right after it was made."""
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    real = harnesstree.on_branch
    monkeypatch.setattr(harnesstree, "on_branch",
                        lambda cwd: f"ttp/harness-t{a}" if pathlib.Path(cwd) == harnesstree.path(p, b) else real(cwd))
    with pytest.raises(RuntimeError, match=f"is on ttp/harness-t{a}, not its own branch ttp/harness-t{b}"):
        harnesstree.workdir(p, b)


def test_no_main_line_to_start_from_refuses_the_harness_worktree(env):
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _ht_park_checkout_on(p, f"ttp/harness-t{a}")
    _hgit(p.harness, "branch", "-q", "-m", "main", "trunk")
    assert harnesstree.main_branch(p) == ""
    with pytest.raises(RuntimeError, match="no main branch to start"):
        harnesstree.workdir(p, b)
    assert not _hgit(p.harness, "branch", "--list", f"ttp/harness-t{b}").strip()


def test_landing_never_merges_into_another_tasks_branch_the_checkout_sits_on(env):
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)
    _commit_in_harness_tree(own, "prompts/b.md", "b\n", "b: prompt")
    p.db.update_task(b, status="done")
    stray = _ht_park_checkout_on(p, f"ttp/harness-t{a}")
    assert harnesstree.land(p, b) == "busy"
    assert _hgit(p.harness, "rev-parse", "HEAD").strip() == stray and own.is_dir()
    _hgit(p.harness, "checkout", "-q", "main")
    assert harnesstree.land(p, b) == "merged"
    assert (p.harness / "prompts" / "b.md").read_text() == "b\n"


def test_harness_work_off_its_own_branch_is_flagged_once_and_never_closed_as_delivered(env):
    from ttp import daemon as dm
    from ttp import harnesstree
    from ttp.daemon import Daemon
    p = make(env)
    tid, _ = _unmerged_harness_commit(p, "wandered edit")
    own = harnesstree.path(p, tid)
    _hgit(own, "checkout", "-q", "-b", "elsewhere")
    p.db.update_task(tid, status="review", result=json.dumps({"status": "needs_review", "summary": "ok"}))
    _ran(p, tid, time.time() - 1900)
    d = Daemon(p.base)
    for _ in range(2):
        _sweep(d)
        assert p.db.task(tid)["status"] == "review"
        (ev,) = p.db.q("SELECT * FROM events WHERE kind=? AND task=?", (dm.HARNESS_OFF_BRANCH, tid))
        assert ev["status"] == "queued" and f"#{tid}" in ev["text"] and "branch elsewhere" in ev["text"]
    assert not _daemon_reviews(p, tid) and str(tid) not in (p.db.kv(dm.HARNESS_MERGED_KEY, {}) or {})
    _hgit(own, "checkout", "-q", f"ttp/harness-t{tid}")
    _sweep(d)
    assert len(_daemon_reviews(p, tid)) == 1   # back on its branch: the normal review path


def test_a_harness_commit_on_a_parked_task_branch_is_not_taken_as_merged(env):
    """Before: a task that worked in a checkout left on another task's branch had its commit seen in
    HEAD and was closed as delivered, though the commit never reached main."""
    from ttp import daemon as dm
    from ttp.daemon import Daemon
    p = make(env)
    a = _harness_task(p, "a")
    _ht_park_checkout_on(p, f"ttp/harness-t{a}")
    _commit_in_harness_tree(p.harness, "prompts/x.md", "x\n", "x: edit")
    sha = _hgit(p.harness, "rev-parse", "HEAD").strip()
    tid = _harness_in_review(p, "edit on a parked branch", f"committed {sha[:12]}", time.time() - 1900)
    d = Daemon(p.base)
    _sweep(d)
    assert p.db.task(tid)["status"] == "review" and not p.db.kv(dm.HARNESS_MERGED_KEY, {})
    (ev,) = p.db.q("SELECT * FROM events WHERE kind=? AND task=?", (dm.HARNESS_OFF_BRANCH, tid))
    assert f"branch ttp/harness-t{a}" in ev["text"]
