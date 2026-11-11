# Shared-branch integration tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add tests here for one review per shared branch, per-member
# findings and forward reverts, and integration-branch routing (integrate.py,
# Daemon._join_shared_review).
from __future__ import annotations


def _int_project(env):
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("budget.max_tasks_per_24h", 1000)
    return p


def _int_commit(path, name, text, msg):
    (path / name).write_text(text)
    _git_out(path, "add", ".")
    _git_out(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
    return _git_out(path, "rev-parse", "HEAD").strip()


def test_two_tasks_on_one_shared_branch_get_one_review_listing_both_members(env):
    """A second code task finishing on the branch a queued review already covers joins that review: no
    second review, the review depends on both, and its spec lists each member's id and acceptance
    criteria and asks each finding to name its task."""
    from ttp import integrate, worktree
    from ttp.daemon import Daemon
    p = _int_project(env)
    a, branch, _, (rev,) = _finish_code(env, p, "first part", {"first.py": 20})
    b = p.db.add_task("second part", f"pr_branch: {branch}\nAdd second.py.\nAcceptance: second.py has 20 lines.",
                      kind="code", tier="standard", origin="coordinator", labels=[f"pr_branch:{branch}"])
    path, bbranch = worktree.ensure(p, p.db.task(b))   # built on the shared branch, which #a's worktree holds
    assert _git_out(p.root, "merge-base", "--is-ancestor", branch, bbranch) == ""
    head = _int_commit(path, "second.py", "x\n" * 20, "second part")
    p.db.update_task(b, status="done", branch=bbranch)
    rid, added = Daemon(p.base)._auto_review(p.db.task(b), "second works")
    assert (rid, added) == (rev["id"], False)
    reviews = [t for t in p.db.q("SELECT * FROM tasks WHERE kind='review' AND status!='cancelled'")]
    assert [r["id"] for r in reviews] == [rev["id"]], "one review per shared branch"
    r = p.db.task(rid)
    assert [m["id"] for m in integrate.members(p.db, r)] == [a, b]
    assert integrate.MEMBERS_PREFIX in r["spec"] and f"task #{a} (first part)" in r["spec"]
    assert f"task #{b} (second part)" in r["spec"] and "acceptance: Acceptance: second.py has 20 lines." in r["spec"]
    assert "`task #<id>:`" in r["spec"]
    assert integrate.member_head(p.root, b) == head and f"Review head {head[:12]} of {bbranch}" in r["spec"]
    assert integrate.member_commits(p.root, b) == [head], "its own commits: from its fork point on #a's head"
    from ttp.cli import status_text
    assert f"integration branch {integrate.INTEGRATION_DEFAULT} (one draft PR): review #{rid} of {branch} covers " \
           f"#{a}, #{b}, not landed yet" in status_text(p)
    # Handled again (a restart re-reads the hand-off): the block is not repeated, nothing is added.
    assert Daemon(p.base)._auto_review(p.db.task(b), "second works") == (rid, False)
    assert p.db.task(rid)["spec"].count(integrate.MEMBERS_PREFIX) == 1
    # A fix of a failed review is no new member.
    f = p.db.add_task(f"Fix review #{rid}: x", f"pr_branch: {branch}\nfix", kind="code", origin="daemon")
    p.db.update_task(f, status="done", branch=bbranch)
    Daemon(p.base)._join_shared_review(p.db.task(f), p.db.task(rid))
    assert f not in json.loads(p.db.task(rid)["depends_on"])


def test_a_finding_names_its_member_and_a_failed_member_is_reverted_forward(env):
    """Findings are grouped by the `task #<id>` they name; reverting one member's commits on the shared
    branch adds one forward commit that undoes only its files, keeping the other member's work and the
    history. A conflicting revert changes nothing."""
    from ttp import integrate
    p = _int_project(env)
    fups = [{"title": "task #11: off by one", "spec": "x"}, {"title": "naming", "spec": "in task #12's helper"},
            {"title": "branch-wide lint", "spec": "both files"}, {"title": "task #11 and task #12 clash", "spec": ""}]
    grouped, loose = integrate.by_member(fups, [11, 12])
    assert [f["title"] for f in grouped[11]] == ["task #11: off by one"]
    assert [f["title"] for f in grouped[12]] == ["naming"]
    assert [f["title"] for f in loose] == ["branch-wide lint", "task #11 and task #12 clash"]
    assert integrate.finding_member({"title": "anything"}, [7]) == 7
    # Two members on one branch, the first's commits recorded from its fork point to its handed-off head.
    root = p.root
    _git_out(root, "checkout", "-qb", "shared")
    base = _int_commit(root, "base.txt", "base\n", "base")
    worktree_mod = __import__("ttp.worktree", fromlist=["x"])
    worktree_mod.record_fork_point(root, 11, base)
    _int_commit(root, "a.py", "a\n", "a one")
    ahead = _int_commit(root, "a2.py", "a2\n", "a two")
    integrate.record_member_head(root, 11, ahead)
    bhead = _int_commit(root, "b.py", "b\n", "b one")
    integrate.record_member_head(root, 12, bhead)
    commits = integrate.member_commits(root, 11)
    assert len(commits) == 2 and commits[-1] == ahead
    assert len(integrate.member_commits(root, 12, after=ahead)) == 1
    _git_out(root, "checkout", "-q", "--detach")   # nobody holds the branch: compare-and-swap move
    ok, new = integrate.revert_member(root, "shared", 11, commits)
    assert ok, new
    assert _git_out(root, "rev-parse", "shared").strip() == new
    files = _git_out(root, "ls-tree", "--name-only", "shared").split()
    assert "b.py" in files and "base.txt" in files and "a.py" not in files and "a2.py" not in files
    assert _git_out(root, "merge-base", "--is-ancestor", bhead, "shared") == "", "history kept: a forward revert"
    assert f"{integrate.REVERT_TRAILER}: 11" in _git_out(root, "log", "-1", "--format=%B", "shared")
    # A revert that conflicts (a later commit edited the same file) leaves the branch where it was.
    _git_out(root, "checkout", "-q", "shared")
    c1 = _int_commit(root, "c.py", "c\n", "c one")
    tip = _int_commit(root, "c.py", "c changed\n", "c two")
    ok, why = integrate.revert_member(root, "shared", 13, [c1])
    assert not ok and "conflicts" in why and _git_out(root, "rev-parse", "shared").strip() == tip
    assert not _git_out(root, "status", "--porcelain", "--untracked-files=no").strip()
    # A commit not on the branch is refused.
    ok, why = integrate.revert_member(root, "shared", 14, ["0" * 40])
    assert not ok


def test_the_integration_branch_is_the_push_branch_or_the_pr_projects_setting(env):
    """A push-branch project lands on its push branch; a PR project on delivery.integration_branch (a
    generic default when unset), through one draft PR."""
    from ttp import integrate
    p = _int_project(env)
    assert integrate.integration_branch(p.config()) == integrate.INTEGRATION_DEFAULT
    assert integrate.is_pr_project(p.config())
    p.set_config("delivery.integration_branch", "team/next")
    assert integrate.integration_branch(p.config()) == "team/next"
    p.set_config("delivery.push_branch", "work")
    assert integrate.integration_branch(p.config()) == "work" and not integrate.is_pr_project(p.config())


def test_a_conflicting_or_no_push_change_keeps_its_own_branch_and_status_says_why(env):
    """A change that merges cleanly onto the integration branch shares it; one that conflicts there (the
    files named) or whose spec has `no_push:` keeps its own branch, and status shows it with why."""
    from ttp import integrate
    p = _int_project(env)
    p.set_config("delivery.push_branch", "work")
    root = p.root
    _git_out(root, "checkout", "-qb", "work")
    _int_commit(root, "shared.txt", "one\n", "start")
    _git_out(root, "checkout", "-qb", "clean")
    clean = _int_commit(root, "new.txt", "new\n", "clean change")
    _git_out(root, "checkout", "-q", "work")
    _git_out(root, "checkout", "-qb", "clash")
    clash = _int_commit(root, "shared.txt", "theirs\n", "clashing change")
    _git_out(root, "checkout", "-q", "work")
    _int_commit(root, "shared.txt", "ours\n", "landed meanwhile")
    cfg = p.config()
    assert integrate.route(root, cfg, {"spec": "s"}, clean) == {"shared": True, "branch": "work", "files": []}
    d = integrate.route(root, cfg, {"spec": "s"}, clash)
    assert d["shared"] is False and d["why"] == "conflict" and d["files"] == ["shared.txt"]
    held = integrate.route(root, cfg, {"spec": "Do it.\nno_push: local notes only"}, clean)
    assert held["shared"] is False and held["why"] == "no_push"
    # No integration branch yet: the first change starts it.
    p.set_config("delivery.push_branch", "")
    assert integrate.route(root, p.config(), {"spec": "s"}, clean)["shared"]
    # Status: the integration branch, and a change kept apart with the conflict that kept it.
    p.set_config("delivery.push_branch", "work")
    assert integrate.status_lines(p.db, p.config(), quiet=True) == []
    t = p.db.add_task("clashing change", "s", kind="code", origin="coordinator",
                      labels=[integrate.own_branch_label(d)])
    p.db.update_task(t, branch="clash")
    lines = integrate.status_lines(p.db, p.config(), "skills")
    assert lines[0] == "skills: integration branch work (push branch): nothing waiting"
    assert lines[1] == f"skills: kept on their own branch: #{t} on clash (conflict in shared.txt)"
