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


def _ht_write_during_merge(p, monkeypatch, write):
    """Run `write(project)` on another thread (its own Project and database connection) once land
    starts its merge, giving it 0.5 s to finish before the merge runs. Returns the record: `early`
    True when the write finished before the merge (nothing made it wait), `err` its exception."""
    from ttp import harnesstree
    from ttp.project import Project
    real, seen = harnesstree._git, {}

    def run():
        try:
            write(Project(p.base))
        except Exception as e:   # pragma: no cover - surfaced by the test's assertion
            seen["err"] = e

    def git(cwd, *args, **kw):
        if args[:1] == ("merge",) and "--abort" not in args and "t" not in seen:
            seen["t"] = threading.Thread(target=run)
            seen["t"].start()
            seen["t"].join(0.5)
            seen["early"] = not seen["t"].is_alive()
        return real(cwd, *args, **kw)
    monkeypatch.setattr(harnesstree, "_git", git)
    return seen


def test_a_memory_append_during_a_harness_merge_of_memory_md_waits_and_survives(env, monkeypatch):
    """Before: an append landing while land merged a branch that changed MEMORY.md made git refuse the
    merge (or lose the line); now the append waits for the merge and lands on the merged index."""
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)   # a works in the checkout: b gets a worktree
    p.db.x("UPDATE runs SET status='done'")
    _ht_memory_index_edit(p, own)
    p.db.update_task(b, status="done")
    got = {}
    seen = _ht_write_during_merge(p, monkeypatch, lambda q: got.setdefault("e", q.add_memory("learned mid-merge")))
    assert harnesstree.land(p, b) == "merged"
    seen["t"].join(30)
    assert not seen.get("err") and seen["early"] is False   # the append waited for the merge
    index = (p.harness / "MEMORY.md").read_text()
    assert "(edited by the task)" in index and f"memory/{got['e'].name}" in index
    assert f"memory/{got['e'].name}" in _hgit(p.harness, "show", "HEAD:MEMORY.md")   # and in a commit
    assert not _hgit(p.harness, "status", "--porcelain", "--", "MEMORY.md", "memory").strip()


def test_a_memory_retirement_during_a_harness_merge_of_memory_md_waits_for_it(env, monkeypatch):
    p = make(env)
    from ttp import harnesstree
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)   # a works in the checkout: b gets a worktree
    p.db.x("UPDATE runs SET status='done'")
    _ht_memory_index_edit(p, own)
    gone = p.add_memory("retired mid-merge", kind="fact")   # committed on the main line, below b's edit
    p.db.update_task(b, status="done")
    seen = _ht_write_during_merge(p, monkeypatch, lambda q: q.forget_memory(gone.stem, why="test"))
    assert harnesstree.land(p, b) == "merged"
    seen["t"].join(30)
    assert not seen.get("err") and seen["early"] is False   # the rewrite waited for the merge
    index = (p.harness / "MEMORY.md").read_text()
    assert "(edited by the task)" in index and gone.name not in index and "older 3" in index
    assert (p.memory_dir / "archive" / gone.name).exists() and not gone.exists()
    assert not _hgit(p.harness, "status", "--porcelain", "--", "MEMORY.md", "memory").strip()


def test_a_memory_lock_held_past_its_bound_never_deadlocks_land_or_the_writers(env, monkeypatch):
    import fcntl
    from ttp import harnesstree, project
    p = make(env)
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)
    p.db.x("UPDATE runs SET status='done'")
    _ht_memory_index_edit(p, own)
    old = p.add_memory("retired past the stuck lock", kind="fact")
    p.db.update_task(b, status="done")
    with p.memory_locked() as outer, p.memory_locked() as inner:
        assert outer and inner   # re-entrant in one thread
    monkeypatch.setattr(project, "MEMORY_LOCK_WAIT_S", 0.2)
    monkeypatch.setattr(harnesstree, "LAND_LOCK_WAIT_S", 0.2)
    fd = os.open(str(p.state / project.MEMORY_LOCK), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)   # a hung holder: never lets go on its own
        head, t0 = _hgit(p.harness, "rev-parse", "HEAD"), time.monotonic()
        assert harnesstree.land(p, b) == "busy" and _hgit(p.harness, "rev-parse", "HEAD") == head
        entry = p.add_memory("written past the stuck lock", commit=False)   # waits, then writes anyway
        p.forget_memory(old.stem, why="test")
        assert time.monotonic() - t0 < 10
        index = (p.harness / "MEMORY.md").read_text()
        assert f"memory/{entry.name}" in index and old.name not in index
    finally:
        os.close(fd)
    assert harnesstree.land(p, b) == "merged"   # the next pass, once the holder let go
    index = (p.harness / "MEMORY.md").read_text()
    assert "(edited by the task)" in index and f"memory/{entry.name}" in index and old.name not in index


def _ht_dirty_copy_setup(p):
    """Harness task b, in a worktree of its own, whose branch changes prompts/shared.md twice (v1, then
    v2) and adds prompts/new [x] y.md; HEAD holds prompts/shared.md as v0. Returns (b, own)."""
    from ttp import harnesstree
    _commit_in_harness_tree(p.harness, "prompts/shared.md", "v0\n", "shared v0")
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)   # a works in the checkout: b gets a worktree
    p.db.x("UPDATE runs SET status='done'")
    _commit_in_harness_tree(own, "prompts/shared.md", "v1\n", "b: shared v1")
    _commit_in_harness_tree(own, "prompts/shared.md", "v2\n", "b: shared v2")
    _commit_in_harness_tree(own, "prompts/new [x] y.md", "new\n", "b: new file")
    p.db.update_task(b, status="done")
    return b, own


def _ht_backups(p):
    root = p.state / "harness-land-backups"
    return {str(f.relative_to(d)): f.read_text() for d in sorted(root.iterdir()) for f in d.rglob("*")
            if f.is_file()} if root.is_dir() else {}


def test_a_dirty_checkout_copy_equal_to_the_branch_head_lands_as_it_is(env):
    """Before: git refused the merge ('would be overwritten') and the commits waited for a hand cleanup;
    then the copy was backed up and put back with `checkout HEAD --` before the merge rewrote it."""
    p = make(env)
    from ttp import harnesstree
    b, own = _ht_dirty_copy_setup(p)
    (p.harness / "prompts" / "shared.md").write_text("v2\n")   # someone copied the branch's file in
    (p.harness / "prompts" / "wip.md").write_text("half done\n")
    ino = (p.harness / "prompts" / "shared.md").stat().st_ino
    assert harnesstree.land(p, b) == "merged"
    assert _hgit(p.harness, "show", "HEAD:prompts/shared.md") == "v2\n"
    assert (p.harness / "prompts" / "shared.md").read_text() == "v2\n"
    assert (p.harness / "prompts" / "shared.md").stat().st_ino == ino   # never rewritten
    assert _hgit(p.harness, "status", "--porcelain", "--", "prompts").strip() == "?? prompts/wip.md"
    assert not _ht_backups(p)
    assert "already held the merged content" in (p.logs / "daemon.log").read_text()
    assert not p.db.q("SELECT id FROM events WHERE kind='harness_merge_conflict'")


def test_a_dirty_checkout_copy_equal_to_an_earlier_branch_version_is_left_and_the_land_refused(env):
    """Before: the copy was backed up and put back with `checkout HEAD --`, then the merge rewrote it."""
    p = make(env)
    from ttp import harnesstree
    b, own = _ht_dirty_copy_setup(p)
    (p.harness / "prompts" / "shared.md").write_text("v1\n")   # the branch's version before its head
    head = _hgit(p.harness, "rev-parse", "HEAD").strip()
    assert harnesstree.land(p, b) == "conflict"
    assert _hgit(p.harness, "rev-parse", "HEAD").strip() == head
    assert (p.harness / "prompts" / "shared.md").read_text() == "v1\n" and not _ht_backups(p)
    (ev,) = p.db.q("SELECT * FROM events WHERE kind='harness_merge_conflict'")
    assert "would be overwritten" in ev["text"] and "prompts/shared.md" in ev["text"]


def test_a_differing_dirty_checkout_copy_is_left_alone_and_the_land_refused_naming_it(env):
    p = make(env)
    from ttp import harnesstree
    b, own = _ht_dirty_copy_setup(p)
    (p.harness / "prompts" / "shared.md").write_text("an edit of its own\n")
    (p.harness / "prompts" / "new [x] y.md").write_text("new\n")   # equal, but git refuses anyway: left
    head = _hgit(p.harness, "rev-parse", "HEAD").strip()
    assert harnesstree.land(p, b) == "conflict"
    assert _hgit(p.harness, "rev-parse", "HEAD").strip() == head
    assert (p.harness / "prompts" / "shared.md").read_text() == "an edit of its own\n"
    assert (p.harness / "prompts" / "new [x] y.md").read_text() == "new\n"
    (ev,) = p.db.q("SELECT * FROM events WHERE kind='harness_merge_conflict'")
    assert "would be overwritten" in ev["text"] and "prompts/shared.md" in ev["text"]
    assert "harness-land-backups" not in ev["text"] and not _ht_backups(p)


def test_a_refused_land_retried_each_pass_sets_nothing_aside(env):
    """Before: each pass backed up the equal copy into a new folder, logged it and rewrote the checkout."""
    p = make(env)
    from ttp import harnesstree
    b, own = _ht_dirty_copy_setup(p)
    (p.harness / "prompts" / "shared.md").write_text("an edit of its own\n")
    (p.harness / "prompts" / "new [x] y.md").write_text("new\n")
    assert [harnesstree.land(p, b) for _ in range(3)] == ["conflict", "conflict-known", "conflict-known"]
    assert not _ht_backups(p)
    log = p.logs / "daemon.log"
    assert "set aside" not in (log.read_text() if log.exists() else "")
    assert (p.harness / "prompts" / "new [x] y.md").read_text() == "new\n"


def test_an_unrelated_staged_change_never_stops_a_land_that_is_no_fast_forward(env):
    """Before: git refused the non-fast-forward merge over the staged prompts/other.md, yet each pass
    set the equal untracked copy aside: a new backup folder, a log line and a checkout rewrite. Merged
    aside, the checkout only moves forward: it lands, both copies as they were."""
    p = make(env)
    from ttp import harnesstree
    b, own = _ht_dirty_copy_setup(p)
    _commit_in_harness_tree(p.harness, "prompts/other.md", "o0\n", "other o0")   # the merge is no fast-forward
    (p.harness / "prompts" / "other.md").write_text("staged o1\n")
    _hgit(p.harness, "add", "prompts/other.md")
    (p.harness / "prompts" / "new [x] y.md").write_text("new\n")
    assert harnesstree.land(p, b) == "merged"
    assert not _ht_backups(p)
    assert (p.harness / "prompts" / "new [x] y.md").read_text() == "new\n"
    assert _hgit(p.harness, "show", "HEAD:prompts/new [x] y.md") == "new\n"
    assert _hgit(p.harness, "show", ":prompts/other.md") == "staged o1\n"
    assert (p.harness / "prompts" / "other.md").read_text() == "staged o1\n"


def test_an_unrelated_staged_change_does_not_stop_a_fast_forward_land(env):
    p = make(env)
    from ttp import harnesstree
    b, own = _ht_dirty_copy_setup(p)
    (p.harness / "prompts" / "staged.md").write_text("staged\n")
    _hgit(p.harness, "add", "prompts/staged.md")   # a file the merge never touches
    (p.harness / "prompts" / "new [x] y.md").write_text("new\n")
    assert harnesstree.land(p, b) == "merged"
    assert _hgit(p.harness, "show", ":prompts/staged.md") == "staged\n"
    assert _hgit(p.harness, "status", "--porcelain", "--", "prompts").strip() == "A  prompts/staged.md"
    assert not _ht_backups(p)


def test_staged_content_differing_from_an_equal_working_copy_is_never_lost(env):
    """Before: the working copy (the branch's v2) was backed up and reset to HEAD; the staged X was lost."""
    p = make(env)
    from ttp import harnesstree
    b, own = _ht_dirty_copy_setup(p)
    (p.harness / "prompts" / "shared.md").write_text("staged X\n")
    _hgit(p.harness, "add", "prompts/shared.md")
    (p.harness / "prompts" / "shared.md").write_text("v2\n")
    assert harnesstree.land(p, b) == "conflict"
    assert _hgit(p.harness, "show", ":prompts/shared.md") == "staged X\n"
    assert (p.harness / "prompts" / "shared.md").read_text() == "v2\n" and not _ht_backups(p)


def test_an_untracked_checkout_copy_equal_to_a_file_the_branch_adds_lands_as_it_is(env):
    p = make(env)
    from ttp import harnesstree
    b, own = _ht_dirty_copy_setup(p)
    (p.harness / "prompts" / "new [x] y.md").write_text("new\n")
    assert harnesstree.land(p, b) == "merged"
    assert _hgit(p.harness, "show", "HEAD:prompts/new [x] y.md") == "new\n"
    assert not _hgit(p.harness, "status", "--porcelain").strip()
    assert not _ht_backups(p)


def test_dirty_copies_are_never_set_aside_while_another_harness_run_works_in_the_checkout(env):
    p = make(env)
    from ttp import harnesstree
    b, own = _ht_dirty_copy_setup(p)
    _harness_run(p, _harness_task(p, "c"))   # c's run works in the checkout: its copies are its own
    (p.harness / "prompts" / "shared.md").write_text("v1\n")
    assert harnesstree.land(p, b) == "busy"
    assert (p.harness / "prompts" / "shared.md").read_text() == "v1\n" and not _ht_backups(p)


def test_a_harness_land_never_resets_or_aborts_in_the_checkout_and_keeps_its_uncommitted_work(env):
    """Before: a branch that conflicted was merged in the checkout and undone with `merge --abort`
    (reflog 'reset: moving to HEAD') each time HEAD moved; a run's staged edit there was taken back."""
    p = make(env)
    from ttp import harnesstree
    _commit_in_harness_tree(p.harness, "prompts/edit.md", "e0\n", "edit e0")
    _commit_in_harness_tree(p.harness, "prompts/staged.md", "s0\n", "staged s0")
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)   # a works in the checkout: b gets a worktree
    p.db.x("UPDATE runs SET status='done'")
    _commit_in_harness_tree(own, "prompts/clash.md", "from b\n", "b")
    _commit_in_harness_tree(p.harness, "prompts/clash.md", "from a\n", "a")
    p.db.update_task(b, status="done")
    (p.harness / "prompts" / "edit.md").write_text("e1, uncommitted\n")
    (p.harness / "prompts" / "untracked.md").write_text("new\n")
    (p.harness / "prompts" / "staged.md").write_text("s1, staged\n")
    _hgit(p.harness, "add", "prompts/staged.md")

    def kept():
        assert (p.harness / "prompts" / "edit.md").read_text() == "e1, uncommitted\n"
        assert (p.harness / "prompts" / "untracked.md").read_text() == "new\n"
        assert _hgit(p.harness, "show", ":prompts/staged.md") == "s1, staged\n"
        assert (p.harness / "prompts" / "staged.md").read_text() == "s1, staged\n"
        assert "reset: moving to HEAD" not in _hgit(p.harness, "reflog")
    head = _hgit(p.harness, "rev-parse", "HEAD").strip()
    assert harnesstree.land(p, b) == "conflict"
    _commit_in_harness_tree(p.harness, "prompts/other.md", "x\n", "a2")   # HEAD moved: tried again
    assert harnesstree.land(p, b) == "conflict"
    kept()
    assert _hgit(p.harness, "rev-parse", "HEAD^").strip() == head
    assert subprocess.run(["git", "-C", str(p.harness), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          capture_output=True).returncode != 0
    assert not (p.state / harnesstree.DIR / harnesstree.MERGE_ASIDE).exists()
    assert harnesstree.MERGE_ASIDE not in _hgit(p.harness, "worktree", "list")
    _hgit(own, "merge", "-q", "--no-edit", "-X", "theirs", "main")   # resolved on the branch: it lands
    _commit_in_harness_tree(own, "prompts/landed.md", "l\n", "b: landed")
    before = _hgit(p.harness, "rev-parse", "HEAD").strip()
    assert harnesstree.land(p, b) == "merged"
    kept()
    assert set(_hgit(p.harness, "diff", "--name-only", before, "HEAD").split()) == {"prompts/landed.md"}
    assert (p.harness / "prompts" / "landed.md").read_text() == "l\n"


def _ht_done_with_commit_on(p, tmp, branch, title="side edit", base="main"):
    """A done harness task (no worktree of its own) whose hand-off names a commit made on `branch` in
    another worktree of the harness repo ({id} in `branch` is the task's id)."""
    tid = _harness_task(p, title)
    b = branch.format(id=tid)
    side = pathlib.Path(tmp) / f"side-{tid}"
    _hgit(p.harness, "worktree", "add", "-q", "-b", b, str(side), base)
    _commit_in_harness_tree(side, f"prompts/{title.replace(' ', '-')}.md", "side\n", title)
    sha = _hgit(side, "rev-parse", "HEAD").strip()
    p.db.update_task(tid, status="done", result=json.dumps({"status": "done", "commit": sha,
                                                            "summary": f"committed {sha[:12]}"}))
    return tid, sha, side


def _ht_stranded_events(p, tid=None):
    from ttp import daemon as dm
    return p.db.q("SELECT * FROM events WHERE kind=?" + (" AND task=?" if tid else ""),
                  (dm.HARNESS_STRANDED,) + ((tid,) if tid else ()))


def test_a_done_harness_tasks_commit_on_its_own_slug_branch_elsewhere_is_landed(env, tmp_path):
    """Before: a worker that committed on ttp/t<id>-<slug> in another worktree of the harness repo had
    its commit stranded there: land only knew ttp/harness-t<id>, and nothing merged or flagged it."""
    from ttp.daemon import Daemon
    p = make(env)
    tid, sha, _ = _ht_done_with_commit_on(p, tmp_path, "ttp/t{id}-fix-the-watcher")
    d = Daemon(p.base)
    for _ in range(2):
        d.land_harness_worktrees()
    assert subprocess.run(["git", "-C", str(p.harness), "merge-base", "--is-ancestor", sha, "HEAD"]).returncode == 0
    assert (p.harness / "prompts" / "side-edit.md").read_text() == "side\n"
    assert _hgit(p.harness, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert not _ht_stranded_events(p)
    assert f"task {tid}: merged its harness commits" in (p.logs / "daemon.log").read_text()


def test_a_done_harness_tasks_commit_only_on_an_unrelated_branch_is_raised_once(env, tmp_path):
    from ttp.daemon import Daemon
    p = make(env)
    before = _hgit(p.harness, "rev-parse", "HEAD").strip()
    other = _harness_task(p, "someone else")
    tid, sha, _ = _ht_done_with_commit_on(p, tmp_path, f"ttp/t{other}-theirs")   # another task's slug branch
    d = Daemon(p.base)
    for _ in range(3):
        d.land_harness_worktrees()
    (ev,) = _ht_stranded_events(p)
    assert ev["task"] == tid and ev["status"] == "queued"
    assert f"#{tid}" in ev["text"] and sha[:10] in ev["text"] and f"ttp/t{other}-theirs" in ev["text"]
    assert _hgit(p.harness, "rev-parse", "HEAD").strip() == before   # nothing merged


def test_a_done_harness_task_with_no_named_commit_or_one_on_head_does_nothing_new(env, tmp_path):
    from ttp import daemon as dm
    from ttp.daemon import Daemon
    p = make(env)
    head = _hgit(p.harness, "rev-parse", "HEAD").strip()
    none = _harness_task(p, "nothing named")
    p.db.update_task(none, status="done", result=json.dumps({"status": "done", "summary": "no change needed"}))
    on_head = _harness_task(p, "already in")
    p.db.update_task(on_head, status="done", result=json.dumps({"status": "done", "commit": head}))
    foreign = _harness_task(p, "other repo")
    p.db.update_task(foreign, status="done", result=json.dumps({"status": "done", "commit": "0123456789abcdef"}))
    d = Daemon(p.base)
    d.land_harness_worktrees()
    assert not _ht_stranded_events(p) and _hgit(p.harness, "rev-parse", "HEAD").strip() == head
    assert set(p.db.kv(dm.HARNESS_STRANDED_KEY, {})) == {str(none), str(on_head), str(foreign)}   # checked once


def test_a_stranded_commit_of_a_task_that_ended_long_ago_is_left_alone(env, tmp_path):
    from ttp import daemon as dm
    from ttp.daemon import Daemon
    p = make(env)
    tid, sha, _ = _ht_done_with_commit_on(p, tmp_path, "elsewhere")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - dm.HARNESS_STRANDED_WINDOW_S - 60, tid))
    Daemon(p.base).land_harness_worktrees()
    assert not _ht_stranded_events(p) and not p.db.kv(dm.HARNESS_STRANDED_KEY, {})


def test_a_stranded_slug_branch_that_conflicts_is_reported_and_tried_again_not_settled(env, tmp_path):
    from ttp import daemon as dm
    from ttp.daemon import Daemon
    p = make(env)
    tid, sha, _ = _ht_done_with_commit_on(p, tmp_path, "ttp/t{id}-clash", title="clash")
    _commit_in_harness_tree(p.harness, "prompts/clash.md", "main's\n", "main: clash")
    d = Daemon(p.base)
    for _ in range(2):
        d.land_harness_worktrees()
    (ev,) = p.db.q("SELECT * FROM events WHERE kind='harness_merge_conflict' AND task=?", (tid,))
    assert f"ttp/t{tid}-clash" in ev["text"] and "wait on that branch" in ev["text"]
    assert str(tid) not in (p.db.kv(dm.HARNESS_STRANDED_KEY, {}) or {}) and not _ht_stranded_events(p)
    assert not _hgit(p.harness, "status", "--porcelain").strip()


def test_a_done_harness_task_with_its_own_worktree_still_lands_from_its_own_branch(env):
    from ttp import harnesstree
    from ttp.daemon import Daemon
    p = make(env)
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)
    _commit_in_harness_tree(own, "prompts/b.md", "b\n", "b")
    sha = _hgit(own, "rev-parse", "HEAD").strip()
    p.db.update_task(b, status="done", result=json.dumps({"status": "done", "commit": sha}))
    p.db.x("UPDATE runs SET status='done'")
    Daemon(p.base).land_harness_worktrees()
    assert (p.harness / "prompts" / "b.md").read_text() == "b\n" and not own.exists()
    assert _hgit(p.harness, "branch", "--list", f"ttp/harness-t{b}").strip() == ""
    assert not _ht_stranded_events(p)


def _ht_git_calls(monkeypatch):
    """Record every git command harnesstree and upjournal run: [(cwd, args)]."""
    from ttp import harnesstree, upjournal
    calls = []
    for mod in (harnesstree, upjournal):
        real = mod._git

        def rec(cwd, *a, _real=real, **k):
            calls.append((str(cwd), a))
            return _real(cwd, *a, **k)
        monkeypatch.setattr(mod, "_git", rec)
    return calls


def _ht_no_overwriting_git(p, calls):
    for cwd, a in calls:
        if cwd == str(p.harness):
            assert not {"checkout", "reset", "stash", "restore", "--abort"} & set(a), a
    assert not {"reset", "checkout"} & set(_hgit(p.harness, "reflog", "--format=%gs").split())


def test_uncommitted_staged_and_untracked_edits_survive_a_land_whose_merge_touches_the_same_files(env, monkeypatch):
    """Before: a copy equal to the branch's content was backed up and put back with `checkout HEAD --`, and
    a fast-forward refused after writing put the touched files back the same way."""
    p = make(env)
    from ttp import harnesstree
    calls = _ht_git_calls(monkeypatch)
    _commit_in_harness_tree(p.harness, "prompts/edit.md", "e0\n", "edit e0")
    _commit_in_harness_tree(p.harness, "prompts/staged.md", "s0\n", "staged s0")
    a, b = _harness_task(p, "a"), _harness_task(p, "b")
    _harness_run(p, a)
    own = harnesstree.workdir(p, b)
    p.db.x("UPDATE runs SET status='done'")
    _commit_in_harness_tree(own, "prompts/edit.md", "e from b\n", "b: edit")
    _commit_in_harness_tree(own, "prompts/staged.md", "s from b\n", "b: staged")
    _commit_in_harness_tree(own, "prompts/untracked.md", "u from b\n", "b: untracked")
    p.db.update_task(b, status="done")
    h = p.harness / "prompts"
    (h / "edit.md").write_text("e1, uncommitted\n")
    (h / "staged.md").write_text("s1, staged\n")
    _hgit(p.harness, "add", "prompts/staged.md")
    (h / "staged.md").write_text("s2, on top of the staged s1\n")
    (h / "untracked.md").write_text("u, my own\n")
    head = _hgit(p.harness, "rev-parse", "HEAD").strip()

    def kept(edit, staged_index, staged, untracked):
        assert (h / "edit.md").read_text() == edit
        assert _hgit(p.harness, "show", ":prompts/staged.md") == staged_index
        assert (h / "staged.md").read_text() == staged
        assert (h / "untracked.md").read_text() == untracked
        _ht_no_overwriting_git(p, calls)
    for want in ("conflict", "conflict-known"):
        assert harnesstree.land(p, b) == want
        assert _hgit(p.harness, "rev-parse", "HEAD").strip() == head
        kept("e1, uncommitted\n", "s1, staged\n", "s2, on top of the staged s1\n", "u, my own\n")
        assert _hgit(p.harness, "ls-files", "--others", "--", "prompts/untracked.md").strip()
    (ev,) = p.db.q("SELECT * FROM events WHERE kind='harness_merge_conflict'")
    assert all(f"prompts/{f}.md" in ev["text"] for f in ("edit", "staged", "untracked"))
    assert not _ht_backups(p)
    # each copy now holds the merged content (staged.md in the index, with an edit of its own on top)
    (h / "edit.md").write_text("e from b\n")
    (h / "staged.md").write_text("s from b\n")
    _hgit(p.harness, "add", "prompts/staged.md")
    (h / "staged.md").write_text("s from b, and more\n")
    (h / "untracked.md").write_text("u from b\n")
    _commit_in_harness_tree(own, "prompts/landed.md", "l\n", "b: landed")   # a new head: tried again
    assert harnesstree.land(p, b) == "merged"
    kept("e from b\n", "s from b\n", "s from b, and more\n", "u from b\n")
    assert _hgit(p.harness, "show", "HEAD:prompts/staged.md") == "s from b\n"
    assert (h / "landed.md").read_text() == "l\n"
    assert _hgit(p.harness, "status", "--porcelain", "--", "prompts").strip() == "M prompts/staged.md"


def test_a_refused_ref_update_writes_no_file_and_takes_its_staging_back(env, monkeypatch):
    """Before: git wrote the touched files, the reference-transaction hook (the template guard) refused
    the ref, and land put them back with `checkout HEAD --`. The ref now moves first."""
    p = make(env)
    from ttp import harnesstree
    calls = _ht_git_calls(monkeypatch)
    b, own = _ht_dirty_copy_setup(p)
    hooks = Path(_hgit(p.harness, "rev-parse", "--path-format=absolute", "--git-path", "hooks").strip())
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "reference-transaction").write_text('#!/bin/sh\n[ "$1" = prepared ] || exit 0\n'
                                                 'grep -q " refs/heads/main$" && exit 1\nexit 0\n')
    (hooks / "reference-transaction").chmod(0o755)
    (p.harness / "prompts" / "new [x] y.md").write_text("new\n")   # equal to the merge's: staged, then taken back
    head = _hgit(p.harness, "rev-parse", "HEAD").strip()
    assert harnesstree.land(p, b) == "busy"
    assert _hgit(p.harness, "rev-parse", "HEAD").strip() == head
    assert (p.harness / "prompts" / "shared.md").read_text() == "v0\n"   # never written
    assert _hgit(p.harness, "status", "--porcelain").strip() == "?? \"prompts/new [x] y.md\""
    _ht_no_overwriting_git(p, calls)


def test_a_deleted_or_symlinked_checkout_copy_of_a_file_the_merge_changes_stops_the_land(env):
    import os
    p = make(env)
    from ttp import harnesstree
    b, own = _ht_dirty_copy_setup(p)
    shared = p.harness / "prompts" / "shared.md"
    shared.unlink()   # deleted, not staged: the merge would write it back
    head = _hgit(p.harness, "rev-parse", "HEAD").strip()
    assert harnesstree.land(p, b) == "conflict" and not os.path.lexists(shared)
    (p.harness / "elsewhere.md").write_text("v2\n")
    shared.symlink_to(p.harness / "elsewhere.md")   # its target holds the merged content: not the file
    _commit_in_harness_tree(own, "prompts/more.md", "m\n", "b: more")
    assert harnesstree.land(p, b) == "conflict" and shared.is_symlink()
    assert _hgit(p.harness, "rev-parse", "HEAD").strip() == head
