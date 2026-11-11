# Worktree-need tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. An unfinished task that only reads a finished task's commits (a work or
# question task depending on it) holds its worktree only while removing it could lose something.
from __future__ import annotations


def _wn_landed(p, tid, path):
    """Record task `tid`'s head as landed, as the push queue does."""
    p.db.x("INSERT INTO task_landings(marker, task, sha, target, ts) VALUES(?,?,?,?,?)",
           (f"m{tid}", tid, _git_out(path, "rev-parse", "HEAD"), "work", time.time()))


def _wn_needs(p, tid, kind, spec="Read its commits."):
    return p.db.add_task(f"{kind} after #{tid}", spec, kind=kind, tier="light", origin="user", depends_on=[tid])


def _wn_why(p, tid, **kw):
    from ttp import worktree
    return [r["why"] for r in worktree.sweep(p, **kw) if r["task"] == tid][0]


def test_a_landed_clean_worktree_goes_when_only_reading_tasks_need_it_and_its_branch_stays(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    tid, path, branch = _code_task(p, "landed")
    _commit_file(path, "change")
    _wn_landed(p, tid, path)
    _wn_needs(p, tid, "work")
    p.db.add_task("check it", f"Measure what task {tid} changed on {branch}.", kind="question", tier="light",
                  origin="user")
    assert _wn_why(p, tid) is None
    assert not path.exists()
    assert _git_out(p.root, "rev-parse", "--verify", branch), "the sweep deleted a kept branch"


def test_a_worktree_whose_head_is_on_the_base_counts_as_landed(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import worktree
    tid, path, _ = _code_task(p, "merged")
    _commit_file(path, "change")
    _wn_needs(p, tid, "work")
    assert "not landed" in _wn_why(p, tid, dry_run=True)
    base = worktree.resolve_base(p)
    _git_out(p.root, "merge", "-q", "--ff-only", _git_out(path, "rev-parse", "HEAD"))
    assert worktree.is_ancestor(p, _git_out(path, "rev-parse", "HEAD"), base)
    assert _wn_why(p, tid) is None and not path.exists()


def test_a_task_that_may_work_in_the_worktree_still_holds_it_when_landed(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    tid, path, _ = _code_task(p, "landed")
    _commit_file(path, "change")
    _wn_landed(p, tid, path)
    # A code task may branch from it, a review runs `ttp push` there, a continuation picks up its
    # branch, and a reader whose spec names the worktree's path reads files in it.
    holders = [_wn_needs(p, tid, "code"), _wn_needs(p, tid, "review"),
               p.db.add_task("more", "Carry on.", kind="work", tier="light", origin="user",
                             labels=[f"continues:{tid}"]),
               _wn_needs(p, tid, "work", spec=f"Read tt-project/worktrees/t{tid}/notes.txt.")]
    for holder in holders:
        for other in holders:
            p.db.update_task(other, status="queued" if other == holder else "done")
        why = _wn_why(p, tid)
        assert why == f"task #{holder} (queued) may still use it", why
        assert path.exists()
        p.db.update_task(holder, status="done")
    assert _wn_why(p, tid) is None and not path.exists()


def test_a_reading_task_still_holds_an_unlanded_or_dirty_worktree(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    tid, path, _ = _code_task(p, "unlanded")
    _commit_file(path, "change")
    reader = _wn_needs(p, tid, "work")
    assert _wn_why(p, tid) == f"task #{reader} (queued) may still use it; its commits have not landed"
    assert path.exists()
    _wn_landed(p, tid, path)
    (path / "change.txt").write_text("edited, not committed\n")
    (path / "new.txt").write_text("untracked\n")
    for kw in ({}, {"patch_tracked": True}):   # the disk guard's sweep too
        why = _wn_why(p, tid, **kw)
        assert why == f"task #{reader} (queued) may still use it; it holds work that is not committed", why
        assert (path / "change.txt").read_text() == "edited, not committed\n" and (path / "new.txt").exists()
    # A task kept with no branch has nothing else holding its commits.
    from ttp import worktree
    assert worktree.reader_hold(p, {"id": tid, "branch": None}, [p.db.task(reader)], path) == ""


def test_may_work_in_reads_the_dependent_kind_and_spec(env):
    p = make(env)
    from ttp import worktree
    task = {"id": 7, "branch": "ttp/t7-x"}
    row = lambda **kw: {"id": 8, "status": "queued", "depends_on": "[7]", "labels": None, "spec": "s", **kw}  # noqa: E731
    assert worktree.may_work_in(p, task, row(kind="code"))
    assert worktree.may_work_in(p, task, row(kind="review"))
    assert worktree.may_work_in(p, task, row())   # a row without its kind is read as one that may
    assert not worktree.may_work_in(p, task, row(kind="work"))
    assert not worktree.may_work_in(p, task, row(kind="question", spec="see worktrees/t70"))
    assert worktree.may_work_in(p, task, row(kind="question", spec="cd worktrees/t7 && ls"))
    assert worktree.may_work_in(p, task, row(kind="work", labels='["continues:7"]'))
    p.set_config("worktree.kinds", ["work"])
    assert worktree.may_work_in(p, task, row(kind="work"))
