# A held .git/config.lock (a sandbox's empty placeholder, another worktree being made) never refuses a
# task's workspace: worktree add writes no config (--no-track), and a lock refusal is retried as
# contention, not a counted try. tests/test_tt_project_runtime.py runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations

LOCK_ERROR = ("Command 'git worktree add -B ttp/t7-x /w origin/main' failed: error: could not lock config "
              "file .git/config: File exists")


def _held_config_lock(repo):
    """An empty, read-only .git/config.lock, as the sandbox leaves it."""
    lock = pathlib.Path(subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-common-dir"], capture_output=True,
                                       text=True, check=True).stdout.strip())
    lock = (lock if lock.is_absolute() else repo / lock) / "config.lock"
    lock.write_text("")
    lock.chmod(0o444)
    return lock


def test_a_code_task_workspace_is_made_while_git_config_is_locked(env):
    p = make(env)
    from ttp import worktree
    remote = env["tmp"] / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(env["repo"]), str(remote)], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "push", "-q", "origin", "HEAD:refs/heads/work/fast"], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "fetch", "-q", "origin"], check=True)
    p.set_config("delivery.base_ref", "work/fast")
    assert worktree.resolve_base(p) == "origin/work/fast", "the start is a remote branch: -b would track it"
    lock = _held_config_lock(env["repo"])
    try:
        tid = p.db.add_task("t", "s", kind="code", tier="light", origin="user")
        path, branch = worktree.ensure(p, dict(p.db.task(tid)))
        assert path.exists() and branch
        cfg = subprocess.run(["git", "-C", str(env["repo"]), "config", "--get-regexp", rf"^branch\."],
                             capture_output=True, text=True).stdout
        assert f"branch.{branch}." not in cfg, cfg
    finally:
        lock.unlink()


def test_a_harness_worktree_is_made_while_git_config_is_locked(env, monkeypatch):
    p = make(env)
    from ttp import harnesstree
    if not harnesstree._is_repo(p):
        subprocess.run(["git", "init", "-q", "-b", "main", str(p.harness)], check=True)
        subprocess.run(["git", "-C", str(p.harness), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)
    # A local start tracks only under autoSetupMerge=always: set so the add would write config without --no-track.
    subprocess.run(["git", "-C", str(p.harness), "config", "branch.autoSetupMerge", "always"], check=True)
    monkeypatch.setattr(harnesstree, "checkout_busy", lambda p, tid: True)
    lock = _held_config_lock(p.harness)
    try:
        own = harnesstree.workdir(p, 77)
    finally:
        lock.unlink()
    assert own != p.harness and own.exists()
    cfg = subprocess.run(["git", "-C", str(p.harness), "config", "--get-regexp", r"^branch\."],
                         capture_output=True, text=True).stdout
    assert harnesstree.branch(77) not in cfg, cfg


def test_a_config_lock_refusal_requeues_without_using_a_workspace_try(env):
    p = make(env)
    from ttp.daemon import CONFIG_LOCK_BACKOFF_S, CONFIG_LOCK_RETRIES, Daemon
    p.set_config("worktree.create_retries", 2)
    d = Daemon(p.base)
    tid = p.db.add_task("t", "s", kind="code", tier="light", origin="user")
    for msg in (LOCK_ERROR, "error: could not lock config file /r/.git/worktrees/x/config.worktree: File exists",
                "fatal: Unable to create '/r/.git/config.lock': File exists."):
        before = time.time()
        d._workspace_failed(p.db.task(tid), RuntimeError(msg))
        t = p.db.task(tid)
        assert t["status"] == "queued", msg
        assert before + CONFIG_LOCK_BACKOFF_S - 1 <= t["not_before"] <= time.time() + CONFIG_LOCK_BACKOFF_S + 1
        assert d._workspace_tries(tid) == 0, "a lock refusal used a workspace try"
    # Any other failure still counts.
    d._workspace_failed(p.db.task(tid), TimeoutError("timed out"))
    assert d._workspace_tries(tid) == 1
    # A lock held through every contention retry is stale: from then on each refusal counts, and ends blocked.
    d._workspace_tries(tid, clear=True)   # a workspace made: both counts start afresh
    for _ in range(CONFIG_LOCK_RETRIES):
        d._workspace_failed(p.db.task(tid), RuntimeError(LOCK_ERROR))
        assert p.db.task(tid)["status"] == "queued" and d._workspace_tries(tid) == 0
    for n in (1, 2):
        d._workspace_failed(p.db.task(tid), RuntimeError(LOCK_ERROR))
        assert p.db.task(tid)["status"] == "queued" and d._workspace_tries(tid) == n
    d._workspace_failed(p.db.task(tid), RuntimeError(LOCK_ERROR))
    t = p.db.task(tid)
    assert t["status"] == "blocked" and "could not lock config file" in t["blocked_reason"]
    # A requeue starts the contention count afresh; a closed task's is dropped.
    p.db.requeue(tid)
    d._workspace_failed(p.db.task(tid), RuntimeError(LOCK_ERROR))
    assert p.db.task(tid)["status"] == "queued" and d._workspace_tries(tid) == 0
    p.db.update_task(tid, status="cancelled")
    d.retry_workspaces()
    assert str(tid) not in (p.db.kv("workspace_lock_tries", {}) or {})


def test_a_final_workspace_error_naming_a_lock_is_still_blocked_at_once(env):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("t", "s", kind="code", tier="light", origin="user")
    e = RuntimeError(LOCK_ERROR)
    e.final = True
    d._workspace_failed(p.db.task(tid), e)
    assert p.db.task(tid)["status"] == "blocked"


def test_a_task_in_workspace_backoff_never_delays_a_higher_priority_start(env, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    p.db.x("UPDATE tasks SET status='done'")
    d = Daemon(p.base)
    low = p.db.add_task("low", "s", kind="code", tier="light", origin="user", priority=3)
    d._workspace_failed(p.db.task(low), RuntimeError(LOCK_ERROR))
    hi = p.db.add_task("high", "s", kind="code", tier="light", origin="user", priority=1)
    assert p.db.task(low)["not_before"] > time.time()
    assert [t["id"] for t in p.db.ready_tasks()] == [hi]
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.base), None))
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append(k["task"]["id"]) or 0)
    d.dispatch()
    assert started == [hi] and p.db.task(low)["status"] == "queued"
