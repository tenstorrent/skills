# Keep-lock tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file, and pytest collects these
# tests under its name. A task worktree's git admin dir survives a `git worktree prune` run where its
# folder is out of view; a worktree that is really gone is still pruned. Add new keep-lock tests here.
from __future__ import annotations


def _wk_admin(p, path):
    """The admin dir (.git/worktrees/<name>) git keeps for the worktree at `path`."""
    return pathlib.Path(_git_out(path, "rev-parse", "--absolute-git-dir"))


def _wk_listed(p):
    return {pathlib.Path(line.removeprefix("worktree ")).name
            for line in _git_out(p.root, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")}


def _wk_prunes(p, scratch_root):
    """Each harness prune step once, as run where some worktree folders are out of view."""
    from ttp import batch, worktree
    for n, step in enumerate((lambda wt: worktree.remove_path(p.root, wt),
                              lambda wt: batch._remove_worktree(p.root, wt))):
        wt = scratch_root / f"scratch{n}"
        _git_out(p.root, "worktree", "add", "-q", "--detach", str(wt))
        step(wt)
    _git_out(p.root, "worktree", "prune")   # and a bare one, as a worker or another tool might run


def test_a_kept_task_worktree_hidden_during_a_prune_keeps_its_admin_dir(env, monkeypatch, tmp_path):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import worktree
    from ttp.daemon import Daemon
    # One the daemon hands to a run, and one made before the keep lock existed, which a sweep keeps.
    tid = p.db.add_task("running", "s", kind="code", tier="light", origin="user")
    path, _ = Daemon(p.base)._workdir_for(p.db.task(tid))
    run = pathlib.Path(path)
    old_tid, old, _ = _code_task(p, "kept", status="done")
    (old / "README.md").write_text("uncommitted\n")
    assert not (_wk_admin(p, old) / "locked").exists()
    assert [r["why"] for r in worktree.sweep(p, dry_run=True) if r["task"] == old_tid][0]
    assert not (_wk_admin(p, old) / "locked").exists(), "a dry run locked a worktree"
    res = [r for r in worktree.sweep(p) if r["task"] == old_tid][0]
    assert res["why"] and old.exists()
    for wt in (run, old):
        admin = _wk_admin(p, wt)
        hidden = tmp_path / f"hidden-{wt.name}"
        wt.rename(hidden)   # out of this view, as in a sandbox that mounts only part of the tree
        _wk_prunes(p, tmp_path)
        assert admin.is_dir(), f"a prune deleted the admin dir of {wt}"
        assert (admin / "locked").read_text().startswith(worktree.KEEP_LOCK)
        hidden.rename(wt)
        assert _git_out(wt, "rev-parse", "--abbrev-ref", "HEAD").startswith("ttp/t")
        assert wt.name in _wk_listed(p)
    assert (old / "README.md").read_text() == "uncommitted\n"
    # Once nothing is lost the sweep lifts its own lock and removes it.
    _git_out(old, "checkout", "--", "README.md")
    assert [r["why"] for r in worktree.sweep(p) if r["task"] == old_tid] == [None]
    assert not old.exists() and old.name not in _wk_listed(p)


def test_a_worktree_really_gone_is_still_pruned_and_a_foreign_lock_stays(env, tmp_path):
    p = make(env)
    from ttp import worktree
    gone = tmp_path / "gone"
    _git_out(p.root, "worktree", "add", "-q", "--detach", str(gone))
    admin = _wk_admin(p, gone)
    shutil.rmtree(gone)
    _wk_prunes(p, tmp_path)
    assert not admin.exists() and "gone" not in _wk_listed(p)
    # keep() leaves someone else's lock as it is, and unkeep() never lifts it.
    _, other, _ = _code_task(p, "theirs")
    _git_out(p.root, "worktree", "lock", "--reason", "a user's hold", str(other))
    worktree.keep(other)
    worktree.unkeep(other)
    assert (_wk_admin(p, other) / "locked").read_text().strip() == "a user's hold"
    with pytest.raises(RuntimeError):
        worktree.remove_path(p.root, other)
    assert other.exists()
    # Not a linked worktree: nothing to lock, no error.
    worktree.keep(tmp_path)
    worktree.unkeep(p.root)
