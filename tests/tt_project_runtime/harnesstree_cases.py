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


def _ht_memory_index_edit(p, own):
    """Harness task branch commit that rewrites MEMORY.md's first line, away from where the daemon
    appends its entries (the main line's index has older entries between them)."""
    old = "".join(f"- [older {i}](memory/fact-older-{i}.md) (fact)\n" for i in range(4))
    _commit_in_harness_tree(p.harness, "MEMORY.md", f"# Memory index\n\n{old}", "older entries")
    _hgit(own, "merge", "-q", "--no-edit", "main")
    _commit_in_harness_tree(own, "MEMORY.md", f"# Memory index (edited by the task)\n\n{old}", "b: index header")


def test_a_harness_merge_commits_the_daemons_uncommitted_memory_writes_first(env):
    """Before: a memory entry the daemon wrote but had not committed yet made git refuse the merge of a
    harness task branch that touched MEMORY.md, so its commits waited for a hand merge."""
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)   # a works in the checkout: b gets a worktree
    p.db.x("UPDATE runs SET status='done'")
    _commit_in_harness_tree(own, "prompts/extra.md", "more\n", "b: prompt")
    _ht_memory_index_edit(p, own)
    entry = p.add_memory("the daemon learned this", kind="fact", commit=False)
    _commit_in_harness_tree(p.harness, "prompts/keep.md", "v1\n", "keep")
    (p.harness / "prompts" / "keep.md").write_text("v2, not the daemon's\n")
    p.db.update_task(b, status="done")
    assert harnesstree.land(p, b) == "merged"
    assert _hgit(p.harness, "status", "--porcelain", "--", "prompts").strip() == "M prompts/keep.md"
    assert (p.harness / "prompts" / "extra.md").read_text() == "more\n"
    index = (p.harness / "MEMORY.md").read_text()
    assert f"memory/{entry.name}" in index and "(edited by the task)" in index
    assert entry.read_text().strip().endswith("the daemon learned this")
    assert not _hgit(p.harness, "status", "--porcelain", "--", "MEMORY.md", "memory").strip()
    assert f"memory/{entry.name}" in _hgit(p.harness, "log", "--name-only", "--format=", "-n", "3")
    assert not p.db.q("SELECT id FROM events WHERE kind='harness_merge_conflict'")


def test_a_harness_branch_not_touching_memory_merges_and_keeps_the_daemons_memory_write(env):
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)   # a works in the checkout: b gets a worktree
    p.db.x("UPDATE runs SET status='done'")
    _commit_in_harness_tree(own, "prompts/extra.md", "more\n", "b: prompt")
    entry = p.add_memory("kept across the merge", kind="fact", commit=False)
    p.db.update_task(b, status="done")
    assert harnesstree.land(p, b) == "merged"
    assert (p.harness / "prompts" / "extra.md").exists() and entry.exists()
    assert f"memory/{entry.name}" in (p.harness / "MEMORY.md").read_text()
    assert not _hgit(p.harness, "status", "--porcelain").strip()


def test_a_harness_branch_clashing_on_a_memory_file_aborts_keeps_the_write_and_reports(env):
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)   # a works in the checkout: b gets a worktree
    p.db.x("UPDATE runs SET status='done'")
    entry = p.add_memory("the daemon's side", kind="fact", title="shared entry", commit=False)
    _commit_in_harness_tree(own, f"memory/{entry.name}", "the task's side\n", "b: same entry")
    p.db.update_task(b, status="done")
    head = _hgit(p.harness, "rev-parse", "HEAD").strip()
    assert harnesstree.land(p, b) == "conflict"
    assert _hgit(p.harness, "rev-parse", "HEAD^").strip() == head   # only the daemon's memory commit
    assert subprocess.run(["git", "-C", str(p.harness), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          capture_output=True).returncode != 0   # merge aborted
    assert "the daemon's side" in entry.read_text() and f"memory/{entry.name}" in (p.harness / "MEMORY.md").read_text()
    assert not _hgit(p.harness, "status", "--porcelain").strip()
    assert _hgit(own, "show", f"HEAD:memory/{entry.name}") == "the task's side\n"   # the task's side waits
    (ev,) = p.db.q("SELECT * FROM events WHERE kind='harness_merge_conflict'")
    assert ev["task"] == b and f"ttp/harness-t{b}" in ev["text"]
    assert harnesstree.land(p, b) == "conflict-known"


def test_a_harness_merge_never_commits_other_uncommitted_files_of_the_checkout(env):
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)   # a works in the checkout: b gets a worktree
    p.db.x("UPDATE runs SET status='done'")
    _commit_in_harness_tree(own, "prompts/extra.md", "from b\n", "b: prompt")
    (p.harness / "prompts" / "extra.md").write_text("an edit in the checkout\n")   # untracked, in the way
    (p.harness / "prompts" / "wip.md").write_text("half done\n")
    p.db.update_task(b, status="done")
    head = _hgit(p.harness, "rev-parse", "HEAD").strip()
    assert harnesstree.land(p, b) == "conflict"   # git refuses, as before: reported, nothing committed
    assert _hgit(p.harness, "rev-parse", "HEAD").strip() == head
    assert (p.harness / "prompts" / "extra.md").read_text() == "an edit in the checkout\n"
    assert (p.harness / "prompts" / "wip.md").read_text() == "half done\n"
    (ev,) = p.db.q("SELECT * FROM events WHERE kind='harness_merge_conflict'")
    assert "would be overwritten" in ev["text"]


def test_a_harness_merge_leaves_memory_files_alone_while_another_harness_run_works_in_the_checkout(env):
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)   # a works in the checkout: its memory edits are its own, not the daemon's
    own = harnesstree.workdir(p, b)
    _ht_memory_index_edit(p, own)
    entry = p.add_memory("a's half-done entry", kind="fact", commit=False)
    p.db.update_task(b, status="done")
    head = _hgit(p.harness, "rev-parse", "HEAD").strip()
    assert harnesstree.land(p, b) == "busy"
    assert _hgit(p.harness, "rev-parse", "HEAD").strip() == head and entry.exists()
    assert _hgit(p.harness, "status", "--porcelain", "--", "MEMORY.md", "memory").strip()
