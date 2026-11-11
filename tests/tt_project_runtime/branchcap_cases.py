# Open-branch cap (branchcap) tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there). Add new open-branch count, alarm and branch cleanup tests here.
from __future__ import annotations


def _cap_task(p, name, path_commit=True, status="done"):
    tid, path, branch = _code_task(p, name, status=status)
    if path_commit:
        _commit(path, f"{name}.txt", f"{name}\n")
    return tid, path, branch


def _cap_land(p, repo, path, tid):
    """Push the task's branch to the push branch as is and record the landing."""
    _git_out(path, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    p.db.x("INSERT INTO task_landings(marker,task,sha,target,ts) VALUES(?,?,?,?,?)",
           (f"m{tid}", tid, _git_out(path, "rev-parse", "HEAD"), "origin/proj", 0))


def test_open_branches_and_threads_are_counted_and_over_cap_raise_one_alarm(env, monkeypatch):
    """scan counts task branches with commits not on the push branch (landed, cancelled and failed tasks'
    apart) and running workers, without a model call; status, the web payload and the Course check read it.
    Over delivery.max_open_branches or max_work_threads one alarm line asks to fold work onto open
    branches, saying urgent and user-ordered work is never held; at or under the caps nothing is raised."""
    from ttp import branchcap, coordinator, web
    p, repo, _origin, _other = _push_setup(env, monkeypatch, ["true"])
    a, _, _ = _cap_task(p, "alpha", status="running")
    b, _, _ = _cap_task(p, "beta", status="review")
    c, cpath, _ = _cap_task(p, "gamma")
    _cap_land(p, repo, cpath, c)
    d, _, _ = _cap_task(p, "delta", status="cancelled")
    _cap_task(p, "empty", path_commit=False)   # no commit of its own: merged, not open
    q = p.db.add_task("a question", "s", kind="question", origin="user")
    p.db.update_task(q, status="running")
    got = branchcap.scan(p)
    assert (got["open_branches"], got["stale_branches"], got["threads"]) == (2, 1, 2)
    assert got["open_tasks"] == [a, b] and got["thread_tasks"] == [a, q]
    assert web.state_payload(p, p.db)["branches"]["open_branches"] == 2
    line = branchcap.status_line(p.db, p.config())
    assert "2 open code branches (cap 6)" in line and "OVER CAP" not in line
    assert branchcap.digest_lines(p.db, p.config()) == []
    p.set_config("delivery.max_open_branches", 1)
    alarm = branchcap.digest_lines(p.db, p.config())
    assert len(alarm) == 1 and "2 open code branches (cap 1)" in alarm[0] and "running workers" not in alarm[0]
    assert "pr_branch:" in alarm[0] and "never held" in alarm[0] and f"#{a}" in alarm[0]
    assert "OVER CAP" in branchcap.status_line(p.db, p.config())
    p.set_config("delivery.max_work_threads", 1)
    assert "2 running workers (cap 1)" in branchcap.digest_lines(p.db, p.config())[0]
    digest = coordinator.digest(p, {}, [], [])
    assert "- open branches: 2 open code branches (cap 1) and 2 running workers (cap 1)" in digest
    # 0 turns a cap off.
    p.set_config("delivery.max_open_branches", 0)
    p.set_config("delivery.max_work_threads", 0)
    assert branchcap.digest_lines(p.db, p.config()) == []
    assert "cap none" in branchcap.status_line(p.db, p.config())


def test_landed_task_branches_are_deleted_once_their_worktree_is_gone_with_a_recovery_list(env, monkeypatch):
    """branchcap.sweep (the daemon's hourly branch_cap, and `ttp prune`) deletes a landed finished task's
    local branch once its worktree is gone, and writes branch and sha to state/branch-deletions.jsonl. It
    keeps a branch whose worktree is still there, one not landed, one with a commit by another author,
    one on a remote (it may carry a PR), one whose task has a PR, and one an unfinished task shares
    (user msg #402's conditions); a dry run deletes nothing."""
    import json
    from ttp import branchcap, worktree
    from ttp.daemon import Daemon
    p, repo, origin, _other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "config", "user.email", "t@t")

    def landed_task(name, keep_worktree=False):
        tid, path, branch = _cap_task(p, name)
        _cap_land(p, repo, path, tid)
        if not keep_worktree:
            worktree.remove(p, tid)
        return tid, path, branch

    def exists(branch):
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                              capture_output=True).returncode == 0

    gone, _, gone_b = landed_task("gone")
    wt, _, kept_wt = landed_task("worktree", keep_worktree=True)
    nl, _, not_landed = _cap_task(p, "notlanded")
    worktree.remove(p, nl)
    fid, fpath, foreign = _cap_task(p, "foreign")
    (fpath / "f.txt").write_text("f\n")
    _git_out(fpath, "add", "f.txt")
    _git_out(fpath, "-c", "user.email=other@x", "commit", "-qm", "theirs", "--author", "O <other@x>")
    _cap_land(p, repo, fpath, fid)
    worktree.remove(p, fid)
    _, _, on_remote = landed_task("remote")
    _git_out(repo, "push", "-q", "origin", f"refs/heads/{on_remote}:refs/heads/{on_remote}")
    _git_out(repo, "fetch", "-q", "origin")
    pr, _, with_pr = landed_task("pr")
    p.db.update_task(pr, pr_url="https://example.invalid/pull/1")
    sh, _, shared = landed_task("shared")
    fix = p.db.add_task("fix on shared", "s", kind="code", origin="daemon")
    p.db.update_task(fix, status="queued", branch=shared)

    assert [r["branch"] for r in branchcap.sweep(p, p.config(), dry=True)] == [gone_b]
    assert exists(gone_b) and not (p.state / branchcap.RECOVERY).exists()
    sha = _git_out(repo, "rev-parse", f"refs/heads/{gone_b}")
    d = Daemon(p.base)
    assert [r["branch"] for r in d.branch_cap()] == [gone_b]
    assert not exists(gone_b)
    for b in (kept_wt, not_landed, foreign, on_remote, with_pr, shared):
        assert exists(b), b
    rows = [json.loads(x) for x in (p.state / branchcap.RECOVERY).read_text().splitlines()]
    assert rows == [{"ts": rows[0]["ts"], "task": gone, "branch": gone_b, "sha": sha, "why": "landed",
                     "saved": f"refs/ttp-archive/{gone_b}"}]
    assert _git_out(repo, "rev-parse", rows[0]["saved"]) == sha   # the tip is kept for recovery
    assert p.db.kv(branchcap.KEPT_KV) == {foreign: _git_out(repo, "rev-parse", f"refs/heads/{foreign}")}   # not read again
    assert d.branch_cap() == []   # hourly: the next tick does not sweep again
    got = p.db.kv(branchcap.KV)
    assert (got["open_branches"], got["unlanded_branches"]) == (0, 1)   # notlanded: done, never approved
    p.set_config("delivery.delete_landed_branches", False)
    worktree.remove(p, wt)   # now removable, but the key is off
    assert branchcap.sweep(p, p.config()) == [] and exists(kept_wt)


def test_dead_branches_are_deleted_after_the_delay_and_only_live_work_counts_as_open(env, monkeypatch):
    """A branch whose tasks all ended without a push queue approval and with no unfinished dependent is
    deleted once older than delivery.dead_branch_days (archived, why 'dead'); a younger one, one an
    unfinished task depends on, one with another author's commit and one with a PR are kept. scan counts
    a done-but-unapproved branch apart, not as open; an approved one awaiting its push is open."""
    import json
    import time
    from ttp import branchcap, worktree
    p, repo, _origin, _other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "config", "user.email", "t@t")

    def ended(name, status="done"):
        tid, path, branch = _cap_task(p, name, status=status)
        worktree.remove(p, tid)
        return tid, path, branch

    def exists(branch):
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                              capture_output=True).returncode == 0

    _, _, dead = ended("dead")
    _, _, failed = ended("failed", status="failed")
    dep, _, needed = ended("needed")
    user = p.db.add_task("needs it", "s", kind="code", origin="user")
    p.db.update_task(user, status="queued", depends_on=json.dumps([dep]))
    fid, fpath, foreign = _cap_task(p, "foreign")
    (fpath / "f.txt").write_text("f\n")
    _git_out(fpath, "add", "f.txt")
    _git_out(fpath, "-c", "user.email=other@x", "commit", "-qm", "theirs", "--author", "O <other@x>")
    worktree.remove(p, fid)
    pr, _, with_pr = ended("pr")
    p.db.update_task(pr, pr_url="https://example.invalid/pull/1")
    ap, _, approved = ended("approved")
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
           (ap, approved, _git_out(repo, "rev-parse", f"refs/heads/{approved}"), "origin/proj", "approved", 0, 0))

    got = branchcap.scan(p)
    assert (got["open_branches"], got["unlanded_branches"], got["stale_branches"]) == (2, 3, 1)
    assert sorted(got["open_tasks"]) == sorted([dep, ap])

    assert branchcap.sweep(p, p.config()) == []   # younger than the delay
    later = time.time() + 4 * 86400
    p.set_config("delivery.dead_branch_days", 0)
    assert branchcap.sweep(p, p.config(), now=later) == []   # 0: off
    p.set_config("delivery.dead_branch_days", 3)
    sha = _git_out(repo, "rev-parse", f"refs/heads/{dead}")
    rows = branchcap.sweep(p, p.config(), now=later)
    assert sorted(r["branch"] for r in rows) == sorted([dead, failed])
    assert {r["why"] for r in rows} == {"dead"}
    assert not exists(dead) and _git_out(repo, "rev-parse", f"refs/ttp-archive/{dead}") == sha
    for b in (needed, foreign, with_pr, approved):
        assert exists(b), b
    saved = [json.loads(x)["branch"] for x in (p.state / branchcap.RECOVERY).read_text().splitlines()]
    assert sorted(saved) == sorted([dead, failed])


def test_sweep_never_deletes_a_branch_checked_out_anywhere_and_rereads_not_landed_keeps(env, monkeypatch, tmp_path):
    """A landed branch checked out in another worktree is kept (update-ref -d would not refuse it). A
    landed task's branch git does not yet show on the push branch is kept only until the tip moves:
    once it lands, the next sweep deletes it."""
    from ttp import branchcap, worktree
    p, repo, _origin, _other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "config", "user.email", "t@t")
    tid, path, out = _cap_task(p, "checkedout")
    _cap_land(p, repo, path, tid)
    worktree.remove(p, tid)
    _git_out(repo, "worktree", "add", "-q", str(tmp_path / "elsewhere"), out)
    lid, lpath, later = _cap_task(p, "later")
    p.db.x("INSERT INTO task_landings(marker,task,sha,target,ts) VALUES(?,?,?,?,?)",
           (f"m{lid}", lid, _git_out(lpath, "rev-parse", "HEAD"), "origin/proj", 0))   # recorded, not yet on git
    worktree.remove(p, lid)
    assert branchcap.sweep(p, p.config()) == []
    assert later in (p.db.kv(branchcap.KEPT_KV) or {})
    _git_out(repo, "push", "-q", "origin", f"refs/heads/{later}:proj")
    _git_out(repo, "fetch", "-q", "origin")
    assert [r["branch"] for r in branchcap.sweep(p, p.config())] == [later]
    _git_out(repo, "worktree", "remove", "--force", str(tmp_path / "elsewhere"))
    assert [r["branch"] for r in branchcap.sweep(p, p.config())] == [out]


def test_branch_cap_course_id_keys_on_which_caps_are_exceeded_not_the_counts(env, monkeypatch):
    """The Course check id names the exceeded caps, so it stays '(as last turn)' while only the counts move."""
    from ttp import branchcap
    p, _repo, _origin, _other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.max_open_branches", 1)
    p.db.set_kv(branchcap.KV, {"open_branches": 3, "threads": 2, "open_tasks": [1]})
    assert branchcap.course_key(p.db, p.config()) == "branches"
    p.db.set_kv(branchcap.KV, {"open_branches": 5, "threads": 3, "open_tasks": [2]})
    assert branchcap.course_key(p.db, p.config()) == "branches"
    p.db.set_kv(branchcap.KV, {"open_branches": 5, "threads": 9, "open_tasks": [2]})
    assert branchcap.course_key(p.db, p.config()) == "branches,threads"
    assert branchcap.course_key(p.db, {"delivery": {"max_open_branches": 0, "max_work_threads": 0}}) == ""
