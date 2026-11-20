# Stale ref-lock sweep of the project root's own repository (gitlocks.clear_root_refs,
# Project.clear_stale_root_ref_locks, Daemon.heal_harness_git) of tests/test_tt_project_runtime.py, which
# runs this file in its own namespace (see _include_cases there): helpers, fixtures and imports come from
# that file. Real processes and the real /proc stand in for a writer that may still own a lock.

def _root_lock(gdir: pathlib.Path, rel: str, mtime: float) -> pathlib.Path:
    path = gdir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    os.utime(path, (mtime, mtime))
    return path


def _root_repo(tmp_path) -> tuple[pathlib.Path, pathlib.Path]:
    repo = tmp_path / "root"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo, repo / ".git"


def _kept_names(gdir: pathlib.Path) -> list[str]:
    from ttp import gitlocks
    return sorted(str(x.relative_to(gdir / gitlocks.KEEP_DIR)).split("/", 1)[1]
                  for x in (gdir / gitlocks.KEEP_DIR).rglob("*.lock"))


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_root_ref_sweep_moves_only_old_ref_locks_and_never_index_head_config_shallow_or_worktree_ones(tmp_path):
    sys.path.insert(0, str(RUNTIME))
    from ttp import gitlocks
    repo, gdir = _root_repo(tmp_path)
    old = time.time() - 7200
    ref = _root_lock(gdir, "refs/heads/feature/x.lock", old)
    packed = _root_lock(gdir, "packed-refs.lock", old)
    fresh = _root_lock(gdir, "refs/heads/young.lock", time.time() - 600)
    never = [_root_lock(gdir, rel, old) for rel in ("index.lock", "HEAD.lock", "config.lock", "shallow.lock",
                                                     "worktrees/w/refs/bisect/b.lock", "worktrees/w/index.lock",
                                                     "objects/pack/p.lock")]
    moved, kept = gitlocks.clear_root_refs(repo, 3600)
    assert sorted(p for p, _, _ in moved) == sorted([ref, packed]) and kept == []
    assert all(age > 7000 for _, age, _ in moved)
    assert not ref.exists() and not packed.exists() and fresh.exists()
    assert all(p.exists() for p in never), "only ref locks are ever touched"
    assert _kept_names(gdir) == ["packed-refs.lock", "refs/heads/feature/x.lock"], "moved aside, never deleted"
    assert gitlocks.clear_root_refs(repo, 3600) == ([], [])
    _root_lock(gdir, "refs/heads/off.lock", old)
    assert gitlocks.clear_root_refs(repo, 0) == ([], []), "0 turns it off"
    assert gitlocks.root_settings({}) == 3600
    assert gitlocks.root_settings({"harness_git": {"root_ref_lock_s": 0}}) == 0
    assert gitlocks.root_settings({"harness_git": {"root_ref_lock_s": "x"}}) == 3600


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_root_ref_sweep_keeps_a_lock_held_open_or_newer_than_a_live_in_repo_process(tmp_path):
    sys.path.insert(0, str(RUNTIME))
    from ttp import gitlocks
    repo, gdir = _root_repo(tmp_path)
    old = time.time() - 7200
    lock = _root_lock(gdir, "refs/heads/main.lock", old)
    # Held open by a process outside the repository: kept.
    holder = subprocess.Popen([sys.executable, "-c", "import sys,time; f=open(sys.argv[1]); time.sleep(60)",
                               str(lock)], cwd=str(tmp_path))
    try:
        deadline = time.time() + 10
        while str(lock) not in gitlocks._open_fds(holder.pid) and time.time() < deadline:
            time.sleep(0.05)
        moved, kept = gitlocks.clear_root_refs(repo, 3600)
        assert moved == [] and [(p, why) for p, _, why in kept] == [(lock, f"pid {holder.pid} has it open")]
    finally:
        holder.kill()
        holder.wait()
    # A live process working in the repository (cwd, or GIT_DIR from elsewhere) that started before the
    # lock was written may own it, even with the lock closed: kept. now is moved past the lock instead.
    for kw in ({"cwd": str(repo / "sub")}, {"cwd": str(tmp_path), "env": {**os.environ, "GIT_DIR": str(gdir)}}):
        (repo / "sub").mkdir(exist_ok=True)
        worker = subprocess.Popen(["sleep", "60"], **kw)
        try:
            time.sleep(0.2)
            young = time.time() + 5
            os.utime(lock, (young, young))
            moved, kept = gitlocks.clear_root_refs(repo, 3600, now=young + 7200)
            assert moved == [] and [why for _, _, why in kept] == [
                f"pid {worker.pid} works in the repository and started before the lock"], kw
            # The same process against a lock written well before it started cannot own it: moved.
            os.utime(lock, (old, old))
            moved, kept = gitlocks.clear_root_refs(repo, 3600)
            assert [p for p, _, _ in moved] == [lock] and kept == [], kw
        finally:
            worker.kill()
            worker.wait()
        lock = _root_lock(gdir, "refs/heads/main.lock", old)


def test_root_ref_sweep_does_nothing_without_proc(tmp_path):
    sys.path.insert(0, str(RUNTIME))
    from ttp import gitlocks
    repo, gdir = _root_repo(tmp_path)
    lock = _root_lock(gdir, "refs/heads/main.lock", time.time() - 7200)
    assert gitlocks.clear_root_refs(repo, 3600, proc=str(tmp_path / "no-proc")) == ([], [])
    assert lock.exists()
    assert gitlocks.clear_root_refs(tmp_path / "not-a-repo", 3600) == ([], [])


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_root_ref_sweep_uses_the_common_dir_of_a_worktree_root(tmp_path):
    sys.path.insert(0, str(RUNTIME))
    from ttp import gitlocks
    main, gdir = _root_repo(tmp_path)
    subprocess.run(["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "x"], check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt), "-b", "side"], check=True)
    old = time.time() - 7200
    lock = _root_lock(gdir, "refs/heads/side.lock", old)
    wt_index = _root_lock(gdir, "worktrees/wt/index.lock", old)
    moved, kept = gitlocks.clear_root_refs(wt, 3600)
    assert [p for p, _, _ in moved] == [lock] and kept == [] and wt_index.exists()
    assert _kept_names(gdir) == ["refs/heads/side.lock"]


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_daemon_sweep_moves_a_stale_root_ref_lock_aside_and_logs_its_age_and_check(env):
    p = make(env)
    from ttp import gitlocks
    from ttp.daemon import Daemon
    if gitlocks.git_dir(p.root) is None:
        subprocess.run(["git", "init", "-q", str(p.root)], check=True)
    gdir = gitlocks.common_dir(gitlocks.git_dir(p.root))
    lock = _root_lock(gdir, "refs/heads/main.lock", time.time() - 7200)
    keep = _root_lock(gdir, "index.lock", time.time() - 7200)
    Daemon(p.base).heal_harness_git()
    assert not lock.exists() and keep.exists() and _kept_names(gdir) == ["refs/heads/main.lock"]
    log = _daemon_log_text(p)
    assert f"root ref lock {lock} (2.0 h old, no holder) moved aside to {gdir / gitlocks.KEEP_DIR}" in log
    ev = p.db.one("SELECT severity,text FROM events WHERE kind='git_lock_cleared'")
    assert ev["severity"] == "low" and "refs/heads/main.lock" in ev["text"]
    lock = _root_lock(gdir, "refs/heads/main.lock", time.time() - 7200)
    p.set_config("harness_git.root_ref_lock_s", 0)
    assert p.clear_stale_root_ref_locks() == [] and lock.exists()


def test_root_ref_lock_holder_counts_unreadable_git_of_this_user_only_and_resolves_relative_git_dirs(tmp_path):
    sys.path.insert(0, str(RUNTIME))
    from ttp import gitlocks
    repo = tmp_path / "root"
    roots, lock, me = [repo, repo / ".git"], repo / ".git" / "refs/heads/m.lock", os.getuid()

    def row(pid, **kw):
        return {"pid": pid, "uid": me, "start": 100.0, "comm": "sleep", "cwd": str(tmp_path), "gitdirs": [],
                "fds": set(), **kw}

    check = lambda *rows: gitlocks.ref_lock_holder(lock, 200.0, roots, list(rows))
    assert check(row(1), row(2, cwd=None), row(3, uid=me + 1, cwd=None, comm="git")) == ""
    assert check(row(4, cwd=None, comm="git")) == "pid 4 cannot be checked"
    assert check(row(5, cwd=None, comm="git", start=500.0)) == "", "started after the lock"
    assert check(row(6, gitdirs=[str(repo / ".git")])).startswith("pid 6 works in the repository")
    assert check(row(7, cwd=str(repo / "a" / "b"), start=None)).startswith("pid 7 works"), "unknown start: may own it"
    assert check(row(8, cwd=str(repo / "a"), start=201.5)).startswith("pid 8 works"), "clock slack"
    assert check(row(9, fds={str(lock)}, start=900.0)) == "pid 9 has it open"


def _placeholder(gdir: pathlib.Path, mtime: float, body: str = "", mode: int = 0o444) -> pathlib.Path:
    path = gdir / "config.lock"
    if path.exists():
        path.chmod(0o644)
    path.write_text(body)
    path.chmod(mode)
    os.utime(path, (mtime, mtime))
    return path


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_root_ref_sweep_moves_a_stale_sandbox_config_lock_placeholder_and_keeps_any_git_may_own(tmp_path):
    sys.path.insert(0, str(RUNTIME))
    from ttp import gitlocks
    repo, gdir = _root_repo(tmp_path)
    old = time.time() - 7200
    # Non-empty, writable or young: possibly a real git config write, never a candidate.
    for body, mode, mtime in (("[core]\n", 0o444, old), ("", 0o644, old), ("", 0o600, old),
                              ("", 0o444, time.time() - 600)):
        lock = _placeholder(gdir, mtime, body, mode)
        assert gitlocks.clear_root_refs(repo, 3600) == ([], []), (body, oct(mode), mtime)
        assert lock.exists() and lock.read_text() == body
    # Held open by any process: kept.
    lock = _placeholder(gdir, old)
    holder = subprocess.Popen([sys.executable, "-c", "import sys,time; f=open(sys.argv[1]); time.sleep(60)",
                               str(lock)], cwd=str(tmp_path))
    try:
        deadline = time.time() + 10
        while str(lock) not in gitlocks._open_fds(holder.pid) and time.time() < deadline:
            time.sleep(0.05)
        moved, kept = gitlocks.clear_root_refs(repo, 3600)
        assert moved == [] and [(p, why) for p, _, why in kept] == [(lock, f"pid {holder.pid} has it open")]
    finally:
        holder.kill()
        holder.wait()
    # A live git process working in the repository that started before the placeholder: kept. A
    # non-git one (a long session whose sandbox made it) does not count.
    git_proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=str(repo), stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL)
    other = subprocess.Popen(["sleep", "60"], cwd=str(repo))
    try:
        time.sleep(0.2)
        young = time.time() + 5
        os.utime(lock, (young, young))
        moved, kept = gitlocks.clear_root_refs(repo, 3600, now=young + 7200)
        assert moved == [] and [why for _, _, why in kept] == [
            f"pid {git_proc.pid} works in the repository and started before the lock"]
    finally:
        git_proc.kill()
        git_proc.wait()
    try:
        moved, kept = gitlocks.clear_root_refs(repo, 3600, now=young + 7200)
        assert [p for p, _, _ in moved] == [lock] and kept == [], "only git processes count for it"
    finally:
        other.kill()
        other.wait()
    # The stale placeholder: moved aside, never deleted; git config writes work again.
    lock = _placeholder(gdir, old)
    moved, kept = gitlocks.clear_root_refs(repo, 3600)
    assert [p for p, _, _ in moved] == [lock] and kept == [] and not lock.exists()
    assert sorted(x.name for x in (gdir / gitlocks.KEEP_DIR).rglob("config.lock")) == ["config.lock"] * 2
    subprocess.run(["git", "-C", str(repo), "config", "ttp.test", "1"], check=True)
    assert gitlocks.clear_root_refs(repo, 3600, now=time.time() + 7200) == ([], []), "git's own config is no lock"


def test_root_lock_holder_git_only_counts_only_git_working_in_the_repository(tmp_path):
    sys.path.insert(0, str(RUNTIME))
    from ttp import gitlocks
    repo = tmp_path / "root"
    roots, lock, me = [repo, repo / ".git"], repo / ".git" / "config.lock", os.getuid()

    def row(pid, **kw):
        return {"pid": pid, "uid": me, "start": 100.0, "comm": "claude", "cwd": str(repo), "gitdirs": [],
                "fds": set(), **kw}

    check = lambda *rows: gitlocks.ref_lock_holder(lock, 200.0, roots, list(rows), git_only=True)
    assert check(row(1), row(2, gitdirs=[str(repo / ".git")])) == ""
    assert check(row(3, comm="git")).startswith("pid 3 works in the repository")
    assert check(row(4, comm="git-upload-pack", start=None)).startswith("pid 4 works")
    assert check(row(5, comm="git", start=500.0)) == "", "started after the lock"
    assert check(row(6, comm="git", cwd=None)) == "pid 6 cannot be checked"
    assert check(row(7, fds={str(lock)})) == "pid 7 has it open", "any opener counts"
