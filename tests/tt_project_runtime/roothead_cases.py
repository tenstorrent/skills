# Project-root HEAD guard tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new root HEAD guard tests here, not there.
from __future__ import annotations


def _roothead_setup(env):
    p = make(env)
    from ttp import daemon as dm
    from ttp import worktree
    repo = env["repo"]
    home = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    d = dm.Daemon(p.base)
    d.guard_root_head()   # sees the root on its branch: recorded as where it belongs
    assert worktree.root_branch(p) == home
    return p, d, dm, worktree, repo, home


def _roothead_commit(repo, msg):
    _git_out(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", msg)
    return _git_out(repo, "rev-parse", "HEAD")


def test_a_root_left_detached_is_put_back_before_a_worktree_is_branched_and_its_commits_kept(env):
    p, d, dm, worktree, repo, home = _roothead_setup(env)
    tip = _git_out(repo, "rev-parse", "HEAD")
    # A task ran `git checkout --detach` in the root and committed there.
    _git_out(repo, "checkout", "-q", "--detach", tip)
    stray = _roothead_commit(repo, "made while detached")
    t = p.db.add_task("needs a worktree", "s", kind="code", tier="light", origin="user")
    path, _ = d._workdir_for(p.db.task(t))
    assert _git_out(repo, "symbolic-ref", "--short", "HEAD") == home
    assert _git_out(repo, "rev-parse", home) == tip, "the branch is not moved"
    assert _git_out(repo, "rev-parse", f"rescue/root-guard-{stray[:12]}") == stray, "the detached commits are kept"
    assert subprocess.run(["git", "-C", path, "merge-base", "--is-ancestor", stray, "HEAD"]).returncode == 1, \
        "the worktree does not start from the stray HEAD"
    got = p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref LIKE 'root-head-restored:%'")
    assert len(got) == 1 and got[0]["severity"] == "low" and home in got[0]["text"]
    assert not p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='root-head'")
    assert not p.db.kv(dm.ROOT_HEAD_KEY)


def test_a_root_with_local_changes_is_never_touched_alerted_once_and_cleared_when_back(env):
    p, d, dm, worktree, repo, home = _roothead_setup(env)
    tip = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "checkout", "-q", "--detach", tip)
    stray = _roothead_commit(repo, "made while detached")
    (repo / "README.md").write_text("edited while detached\n")
    alerts = lambda: p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='root-head'")
    for n in range(2):
        t = p.db.add_task(f"needs a worktree {n}", "s", kind="code", tier="light", origin="user")
        path, _ = d._workdir_for(p.db.task(t))
        assert _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD", "left as it is"
        assert _git_out(repo, "rev-parse", "HEAD") == stray
        assert (repo / "README.md").read_text() == "edited while detached\n", "nothing discarded"
        assert subprocess.run(["git", "-C", path, "merge-base", "--is-ancestor", stray, "HEAD"]).returncode == 1, \
            "the worktree does not start from the stray HEAD"
    got = alerts()
    assert len(got) == 1, "one alert per condition"
    assert "uncommitted changes" in got[0]["text"] and "README.md" in got[0]["text"] and home in got[0]["text"]
    assert p.db.kv(dm.ROOT_HEAD_KEY)["at"] == stray
    assert worktree.root_branch(p) == home, "a moved root is never adopted as its branch"
    # Fixed by hand: the sweep clears it.
    _git_out(repo, "checkout", "-q", "README.md")
    _git_out(repo, "checkout", "-q", home)
    d._root_head_checked = 0.0
    d.guard_root_head()
    assert not p.db.kv(dm.ROOT_HEAD_KEY)
    assert p.db.q("SELECT * FROM messages WHERE kind='resolved' AND ref='root-head'")


def test_restore_root_refuses_uncommitted_tracked_changes(env):
    p, d, dm, worktree, repo, home = _roothead_setup(env)
    _git_out(repo, "checkout", "-q", "-b", "elsewhere")
    (repo / "README.md").write_text("edited elsewhere\n")
    ok, why = worktree.restore_root(repo, home, 7)
    assert not ok and "uncommitted changes" in why and "README.md" in why
    assert _git_out(repo, "symbolic-ref", "--short", "HEAD") == "elsewhere"
    assert (repo / "README.md").read_text() == "edited elsewhere\n"
    _git_out(repo, "checkout", "-q", "README.md")
    assert worktree.restore_root(repo, home, 7) == (True, "")
    assert _git_out(repo, "symbolic-ref", "--short", "HEAD") == home


def test_worktree_base_is_never_a_detached_or_moved_root_head(env):
    p, d, dm, worktree, repo, home = _roothead_setup(env)
    if worktree.named_base(p):
        p.set_config("delivery.base_ref", "")
    assert not worktree.named_base(p), "this test is about the checked-out-branch fallback"
    tip = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "checkout", "-q", "--detach", tip)
    assert worktree.base_ref(p) == home and worktree.resolve_base(p) == home
    # Nothing known: refused with the reason, not branched from HEAD.
    p.db.set_kv(worktree.ROOT_HOME_KEY, None)
    assert worktree.base_ref(p) == ""
    with pytest.raises(RuntimeError, match="detached HEAD"):
        worktree.resolve_base(p)
    # On another branch than worktree.root_branch: that branch, not the one checked out.
    _git_out(repo, "checkout", "-q", "-b", "moved")
    p.set_config("worktree.root_branch", home)
    assert worktree.base_ref(p) == home
    # A user's own switch, with no root alert open and no configured branch, is adopted.
    p.set_config("worktree.root_branch", "")
    d.guard_root_head()
    assert _git_out(repo, "symbolic-ref", "--short", "HEAD") == "moved"
    assert worktree.root_branch(p) == "moved" and worktree.base_ref(p) == "moved"
    assert not p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref LIKE 'root-head%'")


def test_a_task_in_a_root_behind_the_push_branch_is_told_how_far_and_how_to_read_current_code(env):
    p = make(env)
    from ttp import prompts
    repo = env["repo"]
    remote = env["tmp"] / "rootbehind.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
    _remote_add(repo, remote)
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/team/work")
    p.set_config("delivery.push_branch", "origin/team/work")
    prompts._root_fetched.clear()
    tid = p.db.add_task("look around", "s", kind="work", tier="light", origin="user")
    assert "behind" not in prompts.root_behind_line(p, str(p.root)), "a current root gets no line"
    assert "project root checkout" not in prompts.worker_task(p, p.db.task(tid), str(p.root), None)
    # The push branch moves on without the root.
    other = env["tmp"] / "rootbehind-clone"
    subprocess.run(["git", "clone", "-q", "-b", "team/work", str(remote), str(other)], check=True)
    for n in range(3):
        _roothead_commit(other, f"later {n}")
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/team/work")
    head = _git_out(repo, "rev-parse", "HEAD")
    prompts._root_fetched.clear()
    text = prompts.worker_task(p, p.db.task(tid), str(p.root), None)
    line = next(ln for ln in text.splitlines() if ln.startswith("project root checkout"))
    assert "3 commits behind origin/team/work" in line
    assert "`git show origin/team/work:<path>`" in line and "`git grep <pattern> origin/team/work`" in line
    assert "never move the root's HEAD" in line
    assert _git_out(repo, "rev-parse", "HEAD") == head, "the fetch never moves the root's HEAD"
    # Only for a task in the root: a worktree elsewhere gets no line.
    assert prompts.root_behind_line(p, str(env["tmp"])) == ""
    # A fetch that fails adds no line rather than a stale count.
    _git_out(repo, "remote", "set-url", "origin", str(env["tmp"] / "gone.git"))
    prompts._root_fetched.clear()
    assert prompts.root_behind_line(p, str(p.root)) == ""


def test_a_code_task_that_left_the_root_detached_gets_its_rescue_branch_on_record(env, monkeypatch):
    """The daemon puts a root a code task's run left detached back on its branch and keeps the commits on a
    rescue branch; with no branch of its own on record, that rescue branch becomes the task's branch, so its
    review and the push queue resolve it. A task that has a branch keeps it."""
    p = make(env)
    from ttp import daemon as dm

    class Proc:
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dm.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    repo = env["repo"]
    tip = _git_out(repo, "rev-parse", "HEAD")
    d = dm.Daemon(p.base)
    for kept in ("", "ttp/its-own"):
        t = p.db.add_task(f"commits in the root {kept}", "s", kind="code", tier="light", origin="user")
        p.db.update_task(t, branch=kept or None)
        row = _root_run(p, d, t)
        _git_out(repo, "checkout", "-q", "--detach", tip)
        made = _roothead_commit(repo, f"made while detached {kept}")
        _end_run(p, d, row)
        rescue = f"rescue/root-run{row['id']}-{made[:12]}"
        assert _git_out(repo, "rev-parse", rescue) == made
        assert p.db.task(t)["branch"] == (kept or rescue)
