# Run-folder worktree lock tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. The sweep's keep lock is for task worktrees only: a worktree a
# worker made in its run folder stays unlocked, so the worker (or anyone) can remove it with a plain
# `git worktree remove --force`, and a crashed run never leaves it behind locked.
from __future__ import annotations


def test_the_sweep_keeps_task_worktrees_but_never_locks_one_in_a_run_folder(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import worktree
    tid = p.db.add_task("runs", "s", kind="experiment", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    _, live = _run_worktree(p, tid, status="running")
    _, locked = _run_worktree(p, tid, status="running")
    worktree.keep(locked)   # as an older sweep left it
    _, task_wt, _ = _code_task(p, "kept", status="done")
    (task_wt / "README.md").write_text("uncommitted\n")   # so the sweep keeps it
    res = {pathlib.Path(r["path"]): r for r in worktree.sweep(p)}
    assert res[live.resolve()]["held"] and live.exists(), "a live run's worktree stays"
    admin = pathlib.Path(_git_out(live, "rev-parse", "--absolute-git-dir"))
    assert not (admin / "locked").exists(), "the sweep locked a run-folder worktree"
    old_admin = pathlib.Path(_git_out(locked, "rev-parse", "--absolute-git-dir"))
    assert not (old_admin / "locked").exists() and locked.exists(), "an older sweep's keep lock is lifted"
    task_admin = pathlib.Path(_git_out(task_wt, "rev-parse", "--absolute-git-dir"))
    assert (task_admin / "locked").read_text().startswith(worktree.KEEP_LOCK), "task worktrees keep their lock"
    _git_out(p.root, "worktree", "remove", "--force", str(live))   # the worker's own clean-up works
    _git_out(p.root, "worktree", "remove", "--force", str(locked))
    assert not live.exists() and not locked.exists()
