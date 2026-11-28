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


def test_a_git_that_hangs_while_locking_never_stops_the_run_or_the_sweep(env, monkeypatch):
    p = make(env)
    from ttp import worktree
    tid, path, _ = _code_task(p, "kept", status="done")
    real = worktree._git

    def hang(cwd, *args, **kw):
        if args[:2] == ("worktree", "lock"):
            raise subprocess.TimeoutExpired(["git", *args], kw.get("timeout") or 0)
        return real(cwd, *args, **kw)
    monkeypatch.setattr(worktree, "_git", hang)
    worktree.keep(path)
    assert [r["task"] for r in worktree.sweep(p)] == [tid] and path.exists()


def _wk_locked(p):
    """Folder name -> lock reason of each locked worktree record of the project's repository."""
    out, name = {}, None
    for line in _git_out(p.root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            name = pathlib.Path(line.removeprefix("worktree ")).name
        elif line.startswith("locked"):
            out[name] = line.removeprefix("locked").strip()
    return out


def test_the_daemon_sweep_clears_a_kept_record_whose_folder_was_deleted(env, tmp_path):
    p = make(env)
    from ttp import worktree
    tid, path, branch = _code_task(p, "deleted", status="running")
    worktree.keep(path)
    _, mine, _ = _code_task(p, "foreign", status="running")
    _git_out(p.root, "worktree", "lock", "--reason", "a user's hold", str(mine))
    shutil.rmtree(path)
    shutil.rmtree(mine)
    # Not the daemon's full view, or a dry run: nothing changes.
    assert not [r for r in worktree.sweep(p) if r.get("gone")]
    assert [(r["task"], r["why"]) for r in worktree.sweep(p, full_view=True, dry_run=True) if r.get("gone")] \
        == [(tid, None)]
    assert _wk_locked(p)[path.name].startswith(worktree.KEEP_LOCK)
    with pytest.raises(Exception):
        _git_out(p.root, "worktree", "add", "-q", str(tmp_path / "again"), branch)
    # The daemon's sweep clears it and logs it.
    from ttp.daemon import Daemon
    gone = [r for r in Daemon(p.base).prune_worktrees(force=True) if r.get("gone")]
    assert [(r["task"], r["branch"], r["why"]) for r in gone] == [(tid, branch, None)]
    assert "cleared its kept record" in (p.logs / "daemon.log").read_text()
    assert path.name not in _wk_listed(p)
    _git_out(p.root, "worktree", "add", "-q", str(tmp_path / "again"), branch)
    assert _git_out(tmp_path / "again", "rev-parse", "--abbrev-ref", "HEAD") == branch
    # Someone else's lock stays, folder gone or not.
    assert _wk_locked(p)[mine.name] == "a user's hold"


def test_a_kept_record_under_a_hidden_or_unreadable_parent_stays(env, tmp_path):
    p = make(env)
    from ttp import worktree
    _, path, _ = _code_task(p, "hidden", status="running")
    worktree.keep(path)
    run_wt = p.runs / "900" / "wt"
    run_wt.parent.mkdir(parents=True)
    _git_out(p.root, "worktree", "add", "-q", "--detach", str(run_wt))
    worktree.keep(run_wt)
    # The worktrees folder out of view (a sandbox that mounts only part of the tree): its records stay.
    hidden = tmp_path / "hidden"
    p.worktrees.rename(hidden)
    try:
        assert worktree.gone_kept(p) == []
        assert not [r for r in worktree.sweep(p, full_view=True) if r.get("gone")]
    finally:
        hidden.rename(p.worktrees)
    # A run worktree's own parent folder unreadable, then missing: its record stays.
    shutil.rmtree(run_wt)
    if os.geteuid() != 0:
        run_wt.parent.chmod(0)
        try:
            assert not [r for r in worktree.sweep(p, full_view=True) if r.get("gone")]
        finally:
            run_wt.parent.chmod(0o755)
    run_wt.parent.rename(tmp_path / "run900")
    assert not [r for r in worktree.sweep(p, full_view=True) if r.get("gone")]
    assert {path.name, "wt"} <= set(_wk_locked(p))
    # Back in view with the folder really gone: cleared.
    (tmp_path / "run900").rename(run_wt.parent)
    assert [r["path"] for r in worktree.sweep(p, full_view=True) if r.get("gone")] == [str(run_wt.resolve())]
    assert "wt" not in _wk_listed(p) and path.name in _wk_listed(p)


def test_a_harness_worktree_hidden_during_a_prune_keeps_its_admin_dir_and_a_foreign_lock_stays(env, tmp_path):
    p = make(env)
    from ttp import harnesstree, worktree
    from ttp.daemon import Daemon
    a, b, c = _harness_task(p, "a"), _harness_task(p, "b"), _harness_task(p, "c")
    assert Daemon(p.base)._workdir_for(p.db.task(a))[0] == str(p.harness)   # the checkout: nothing to lock
    _harness_run(p, a)
    own = pathlib.Path(Daemon(p.base)._workdir_for(p.db.task(b))[0])
    assert own == harnesstree.path(p, b)
    admin = _wk_admin(p, own)
    assert (admin / "locked").read_text().startswith(worktree.KEEP_LOCK)
    _commit_in_harness_tree(own, "prompts/kept.md", "kept\n", "b: prompt")
    hidden = tmp_path / "hidden"
    own.rename(hidden)   # out of this view, as in a sandbox that mounts only part of the tree
    _hgit(p.harness, "worktree", "prune")
    assert admin.is_dir(), "a prune deleted the harness worktree's admin dir"
    hidden.rename(own)
    assert _hgit(own, "symbolic-ref", "--short", "HEAD").strip() == f"ttp/harness-t{b}"
    # Once its task ended the landing lifts its own lock and removes it.
    p.db.update_task(b, status="done")
    assert harnesstree.land(p, b) == "merged"
    assert not own.exists() and not admin.exists()
    assert (p.harness / "prompts" / "kept.md").read_text() == "kept\n"
    # Someone else's lock is never lifted: the worktree stays.
    other = pathlib.Path(Daemon(p.base)._workdir_for(p.db.task(c))[0])
    _hgit(p.harness, "worktree", "unlock", str(other))
    _hgit(p.harness, "worktree", "lock", "--reason", "a user's hold", str(other))
    Daemon(p.base)._workdir_for(p.db.task(c))
    p.db.update_task(c, status="done")
    assert harnesstree.land(p, c) == "kept" and other.exists()
    assert (_wk_admin(p, other) / "locked").read_text().strip() == "a user's hold"


def test_the_worktree_sweep_runs_no_git_call_per_registered_worktree(env, monkeypatch):
    """The tick's sweep tells a kept worktree is one from git's single `git worktree list` record and its
    .git entry, not from an is_git subprocess per worktree (18 kept worktrees held a tick 9+ minutes)."""
    from ttp import daemon as daemon_mod, worktree
    from ttp.daemon import Daemon
    _no_grace(monkeypatch)
    p = make(env)
    paths = []
    for i in range(3):
        _tid, path, _branch = _code_task(p, f"kept {i}")
        paths.append(path)
    calls = []
    real = worktree.is_git
    monkeypatch.setattr(worktree, "is_git", lambda path: calls.append(path) or real(path))
    d = Daemon(p.base)
    res = d.prune_worktrees(every_s=0, force=True)
    assert {r["path"] for r in res} == {str(path) for path in paths}, res
    assert not [c for c in calls if c in paths], calls
    # The tick sweeps at most every PRUNE_TICK_MIN_S, even when a task finished since.
    swept = []
    monkeypatch.setattr(worktree, "sweep", lambda *a, **k: swept.append(1) or [])
    _code_task(p, "finished just now")
    assert d.tick_prune_worktrees() == [] and not swept
    d._last_prune -= daemon_mod.PRUNE_TICK_MIN_S
    d.tick_prune_worktrees()
    assert swept == [1]


def test_the_worktree_sweep_reads_each_push_marker_once_and_ended_ones_not_again(env, monkeypatch):
    """The tick's sweep read every pushes/*.json marker once per worktree it judged (1109 markers times
    311 worktrees stalled dispatch): now one snapshot per sweep, and an ended marker is not read again
    while unchanged. A push that is queued in a worktree still keeps it, and one that starts after the
    snapshot is seen by the live check before the worktree goes."""
    from ttp import push, worktree
    from ttp.daemon import Daemon
    _no_grace(monkeypatch)
    p = make(env)
    paths = [_code_task(p, f"kept {i}")[1] for i in range(4)]
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(30):
        (folder / f"old{i:02d}.json").write_text(json.dumps({"id": f"old{i:02d}", "status": "pushed",
                                                            "repo": str(paths[i % 4])}))
    (folder / "live.json").write_text(json.dumps({"id": "live", "status": "queued", "repo": str(paths[0])}))
    reads = []
    real = push.read_dict
    monkeypatch.setattr(push, "read_dict", lambda m: reads.append(m.name) or real(m))
    d = Daemon(p.base)
    res = {r["path"]: r for r in d.prune_worktrees(every_s=0, force=True)}
    assert res[str(paths[0])]["why"] == "a detached `ttp push` runs in it", res[str(paths[0])]
    # One snapshot per sweep; removals recheck live, reading only the queued marker again.
    assert [r for r in reads if r.startswith("old")] == sorted(f"old{i:02d}.json" for i in range(30)), reads
    reads.clear()
    d.prune_worktrees(every_s=0, force=True)
    assert not [r for r in reads if r.startswith("old")], reads
    # An ended marker rewritten (a requeue) is read again.
    (folder / "old05.json").write_text(json.dumps({"id": "old05", "status": "queued", "repo": str(paths[1])}))
    reads.clear()
    assert [m["id"] for m in push.running(p)] == ["live", "old05"]
    assert [r for r in reads if r.startswith("old")] == ["old05.json"], reads
    # Held verdicts are the same with a snapshot as with a live read.
    task = p.db.task(int(paths[1].name[1:]))
    assert worktree.held_by(p, task, [], time.time(), pushes=push.running(p)) \
        == worktree.held_by(p, task, [], time.time()) == "a detached `ttp push` runs in it"
    assert worktree.held_by(p, task, [], time.time(), pushes=[]) is None


def test_a_push_that_starts_after_the_sweeps_snapshot_still_keeps_its_worktree(env, monkeypatch):
    from ttp import push, worktree
    from ttp.daemon import Daemon
    _no_grace(monkeypatch)
    p = make(env)
    _tid, path, _branch = _code_task(p, "pushing late")
    snaps = []
    real = push.running

    def late(proj):
        snaps.append(1)
        return [] if len(snaps) == 1 else [{"id": "late", "status": "queued", "repo": str(path)}]
    monkeypatch.setattr(push, "running", late)
    removed = []
    monkeypatch.setattr(worktree, "remove", lambda *a, **k: removed.append(a))
    res = {r["path"]: r for r in Daemon(p.base).prune_worktrees(every_s=0, force=True)}
    assert res[str(path)]["why"] == "a detached `ttp push` runs in it" and res[str(path)].get("held"), res
    assert not removed and path.is_dir()
    monkeypatch.setattr(push, "running", real)


def test_a_hung_git_keeps_its_worktree_and_the_daemon_sweep_stops_at_its_budget(env, monkeypatch, tmp_path):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import worktree
    done = [_code_task(p, f"hung{n}", status="done")[:2] for n in range(2)]
    # A git whose `submodule status` hangs (as on a stuck mount); the rest is the real git.
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "git").write_text('#!/bin/sh\nfor a; do [ "$a" = submodule ] && exec sleep 120; done\n'
                              f'exec {shutil.which("git")} "$@"\n')
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(worktree, "SWEEP_GIT_S", 1.0, raising=False)
    monkeypatch.setattr(worktree, "SWEEP_BUDGET_S", 0.5, raising=False)
    t0 = time.monotonic()
    res = {r["task"]: r for r in worktree.sweep(p, full_view=True)}
    assert time.monotonic() - t0 < 30, "the daemon's sweep waited on a hung git"
    (first, a), (second, b) = done
    assert "timed out" in res[first]["why"] and not res[first].get("deferred")   # kept, never taken as clean
    assert res[second].get("deferred") and res[second]["why"]   # past the budget: left for the next sweep
    assert a.exists() and b.exists()


def _wk_submodule_worktree(tmp_path):
    """A superproject with one submodule `lib`, and a worktree `wt` of it with `lib` checked out."""
    ident = ("-c", "user.name=t", "-c", "user.email=t@t", "-c", "protocol.file.allow=always")
    lib, top = tmp_path / "lib", tmp_path / "top"
    for repo in (lib, top):
        repo.mkdir()
        _git_out(repo, "init", "-q", "-b", "main")
        (repo / "f.txt").write_text(repo.name)
        _git_out(repo, "add", ".")
        _git_out(repo, *ident, "commit", "-qm", "base")
    _git_out(top, *ident, "submodule", "add", "-q", str(lib), "lib")
    _git_out(top, *ident, "commit", "-qm", "add lib")
    wt = tmp_path / "wt"
    _git_out(top, "worktree", "add", "-q", "-b", "task", str(wt))
    _git_out(wt, *ident, "submodule", "update", "-q", "--init")
    return top, wt, ident


def test_a_worktree_whose_submodules_are_clean_and_recorded_or_pushed_is_removed(tmp_path):
    from ttp import worktree
    top, wt, ident = _wk_submodule_worktree(tmp_path)
    assert worktree.inspect(wt)["why"] is None
    # a commit of the submodule's own already on its remote, recorded by the superproject: nothing to lose
    (wt / "lib" / "g.txt").write_text("g")
    _git_out(wt / "lib", "add", ".")
    _git_out(wt / "lib", *ident, "commit", "-qm", "g")
    _git_out(wt / "lib", "push", "-q", "origin", "HEAD:refs/heads/side")
    _git_out(wt / "lib", "fetch", "-q", "origin")
    _git_out(wt, "add", "lib")
    _git_out(wt, *ident, "commit", "-qm", "bump lib")
    _git_out(wt, "update-ref", "refs/remotes/origin/task", "HEAD")
    assert worktree.keep_reason(wt) is None
    # the submodule's HEAD moved on to a pushed commit the superproject does not record: still nothing to lose
    (wt / "lib" / "h.txt").write_text("h")
    _git_out(wt / "lib", "add", ".")
    _git_out(wt / "lib", *ident, "commit", "-qm", "h")
    _git_out(wt / "lib", "push", "-q", "origin", "HEAD:refs/heads/side2")
    _git_out(wt / "lib", "fetch", "-q", "origin")
    assert worktree.inspect(wt)["dirty"] == "tracked"            # the gitlink change is the superproject's own
    _git_out(wt, "checkout", "-q", "--", ".")
    _git_out(wt / "lib", "checkout", "-q", "--detach", "HEAD~1")
    assert worktree.keep_reason(wt) is None
    worktree.remove_path(top, wt)                                 # git refuses submodules; the check stands in
    assert not wt.exists()


def test_a_worktree_whose_submodule_has_changes_or_unpushed_commits_stays_naming_it(tmp_path):
    from ttp import worktree
    top, wt, ident = _wk_submodule_worktree(tmp_path)
    (wt / "lib" / "new.txt").write_text("only here")
    why = worktree.keep_reason(wt)
    assert why.startswith("submodule lib has uncommitted changes") and "new.txt" in why
    (wt / "lib" / "new.txt").unlink()
    (wt / "lib" / "f.txt").write_text("edited")
    assert worktree.keep_reason(wt).startswith("submodule lib has uncommitted changes")
    _git_out(wt / "lib", *ident, "commit", "-qam", "local only")
    _git_out(wt, "add", "lib")
    _git_out(wt, *ident, "commit", "-qm", "record it")           # recorded, but on none of lib's remote branches
    assert "submodule lib has commits on none of its remote branches" in worktree.keep_reason(wt)
    _git_out(top / "lib", "fetch", "-q", str(wt / "lib"), "HEAD")  # the main checkout's copy has it now
    assert worktree.keep_reason(wt) is None
    _git_out(wt / "lib", "checkout", "-q", "--detach", "HEAD")
    _git_out(wt / "lib", *ident, "commit", "-q", "--allow-empty", "-m", "one more")
    _git_out(wt, "checkout", "-q", "--", ".")
    assert "submodule lib has commits on none of its remote branches" in worktree.keep_reason(wt)
    with pytest.raises(RuntimeError, match="submodules"):
        worktree.remove_path(top, wt)
    assert (wt / "lib" / ".git").exists()


def test_a_worktree_whose_submodule_git_dir_cannot_be_read_stays_naming_it(tmp_path, monkeypatch):
    from ttp import gitmemo, worktree
    top, wt, ident = _wk_submodule_worktree(tmp_path)
    real = gitmemo.run

    def run(cmd, **kw):   # the superproject lists lib, but lib's own git dir cannot be read
        if cmd[3:6] == ["-C", "lib", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")
        return real(cmd, **kw)
    monkeypatch.setattr(gitmemo, "run", run)
    assert worktree.keep_reason(wt) == "submodule lib could not be read"
    with pytest.raises(RuntimeError, match="submodules"):
        worktree.remove_path(top, wt)
    assert (wt / "lib" / "f.txt").exists()
