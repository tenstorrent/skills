# Branch-adoption tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. A task whose saved work sits on a reviewed shared branch adopts it with
# `ttp task <p> adopt-branch`, and its worktree's ownership follows a review parent to the code task
# that review reviewed.
from __future__ import annotations


def _ab_chain(p, worktree):
    """A done code task with its own branch, a branchless review of it and a code task continuing that
    review. Returns (code id, its branch, review id, continuing task id)."""
    code = p.db.add_task("shared change", "s", kind="code", tier="light", origin="user")
    shared = f"ttp/t{code}-shared-change"
    p.db.update_task(code, branch=shared, status="done")
    _git_out(p.root, "branch", shared, worktree.resolve_base(p))
    rev = p.db.add_task(f"Review #{code}: shared change", "s", kind="review", tier="light", origin="daemon",
                        labels=[f"auto_review:{code}"])
    p.db.update_task(rev, status="failed")
    tid = p.db.add_task("fix the review", "s", kind="code", tier="light", origin="daemon", labels=[f"continues:{rev}"])
    return code, shared, rev, tid


def test_a_worktree_on_the_branch_a_review_parent_reviewed_is_adopted_not_stuck(env):
    p = make(env)
    from ttp import worktree
    code, shared, rev, tid = _ab_chain(p, worktree)
    path, own = worktree.ensure(p, p.db.task(tid))
    _git_out(path, "checkout", "-q", shared)
    head = _commit_file_in(path, "fix.txt")
    assert worktree.ensure(p, p.db.task(tid)) == (path, shared), "the reviewed code task's branch was not owned"
    assert _git_out(path, "rev-parse", "HEAD") == head
    assert set(worktree.lineage(p, p.db.task(tid))) == {tid, rev, code}
    other = p.db.add_task("unrelated", "s", kind="code", tier="light", origin="user")
    assert not worktree._owns(p, p.db.task(other), shared)


def test_adopt_branch_rebinds_only_the_branch_and_records_it(env, capsys):
    p = make(env)
    from ttp import worktree
    from ttp.db import load_result
    code, shared, rev, tid = _ab_chain(p, worktree)
    path, own = worktree.ensure(p, p.db.task(tid))
    _git_out(path, "checkout", "-q", shared)
    head = _commit_file_in(path, "fix.txt")
    p.db.update_task(tid, status="done", result=json.dumps({"head": head}))
    before = p.db.task(tid)
    assert _ttp_task(p, "adopt-branch", str(tid), shared, "--why", "its saved work is on the shared branch") == 0
    after = p.db.task(tid)
    assert after["branch"] == shared and "now works on" in capsys.readouterr().out
    assert {k: after[k] for k in after if k not in ("branch", "updated")} == \
        {k: before[k] for k in before if k not in ("branch", "updated")}
    assert load_result(after["result"])["head"] == head and _git_out(path, "rev-parse", "HEAD") == head
    ev = p.db.one("SELECT * FROM events WHERE task=? AND kind='branch_adopted'", (tid,))
    data = json.loads(ev["data"])
    assert data["branch"] == shared and data["head"] == head and data["why"].startswith("its saved")
    assert _ttp_task(p, "adopt-branch", str(tid)) not in (0, None)


def test_adopt_branch_refuses_a_running_task_an_unrelated_owner_a_foreign_head_or_a_dirty_tree(env):
    p = make(env)
    from ttp import worktree
    code, shared, rev, tid = _ab_chain(p, worktree)
    path, own = worktree.ensure(p, p.db.task(tid))
    _git_out(path, "checkout", "-q", shared)
    head = _commit_file_in(path, "fix.txt")

    def refused(name, why):
        with pytest.raises(ValueError, match=why):
            worktree.adopt_branch(p, tid, name)
        assert p.db.task(tid)["branch"] in (None, own)

    p.db.update_task(tid, status="running")
    refused(shared, "is running")
    p.db.update_task(tid, status="done", result=json.dumps({"head": head}))
    user = p.db.add_task("follow-up", f"Builds on #{tid}.", kind="code", tier="light", origin="user")
    p.db.update_task(user, status="running")
    refused(shared, f"task {user} is running")
    p.db.update_task(user, status="cancelled")
    refused("no-such-branch", "no branch")
    (path / "fix.txt").write_text("uncommitted")
    refused(shared, "uncommitted changes")
    _git_out(path, "checkout", "-q", "--", "fix.txt")
    stranger = p.db.add_task("someone else", "s", kind="code", tier="light", origin="user")
    _git_out(p.root, "branch", "elsewhere", worktree.resolve_base(p))
    p.db.update_task(stranger, branch="elsewhere")
    refused("elsewhere", f"belongs to open task {stranger}")
    p.db.update_task(stranger, status="cancelled")
    foreign = worktree.resolve_base(p)
    _git_out(p.root, "branch", "foreign", foreign)
    p.db.update_task(tid, result=json.dumps({"head": "0" * 40}))
    p.db.update_task(code, branch=None)
    refused("foreign", "neither task")
    assert not p.db.one("SELECT 1 FROM events WHERE kind='branch_adopted'")
