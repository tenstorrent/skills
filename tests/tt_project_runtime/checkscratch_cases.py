# Check scratch placement tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new tests on where a check's scratch goes here.
from __future__ import annotations

_GIB = 1 << 30


def _ram_disk(tmp_path, monkeypatch, *, fstype="tmpfs", free=100 * _GIB, size=128 * _GIB,
              total=200 * _GIB, avail=150 * _GIB):
    """A folder the checks take for the host's RAM disk: its mount type, space and the RAM as given."""
    from ttp import shorttmp
    ram = tmp_path / "ram"
    ram.mkdir()
    real = os.path.realpath(ram)
    (tmp_path / "mounts").write_text(f"proc /proc proc rw 0 0\nnone {real} {fstype} rw 0 0\n")
    (tmp_path / "meminfo").write_text(f"MemTotal: {total // 1024} kB\nMemAvailable: {avail // 1024} kB\n")
    monkeypatch.setenv("TTP_TEST_RAM_DISK", str(ram))
    monkeypatch.setattr(shorttmp, "MOUNTS", str(tmp_path / "mounts"))
    monkeypatch.setattr(shorttmp, "MEMINFO", str(tmp_path / "meminfo"))
    monkeypatch.setattr(shorttmp, "_space", lambda path: (free, size))
    return shorttmp.tmpfs_root()


def test_detached_checks_put_scratch_and_clean_checkout_on_the_ram_disk_and_leave_nothing(env, tmp_path,
                                                                                         monkeypatch):
    """Concurrent suites on a disk filesystem waited on its locks (about 3x slower than on tmpfs): a
    detached check's TMPDIR and its clean checkout go on the RAM disk when it has room; other short dirs
    (a worker's, a detached job's) stay under /tmp; nothing is left behind after the run. The detached
    run is another process, so this needs tmp_path itself on a tmpfs with room."""
    from ttp import cli, shorttmp
    p, repo, run, git, tmp = _clean_repo(env, tmp_path, monkeypatch)
    (tmp_path / "ram").mkdir()
    monkeypatch.setenv("TTP_TEST_RAM_DISK", str(tmp_path / "ram"))
    ram = shorttmp.tmpfs_room(shorttmp.need(repo, "HEAD", ["sh"]))
    if ram is None:
        pytest.skip("tmp_path is not on a tmpfs with room for a check's scratch")
    seen = tmp_path / "seen"
    cli.main(["checks", "--detach", "--fresh", "--", f"sh -c 'echo $TMPDIR; pwd' > {seen}"])
    assert _wait_precheck(run) == "0", (run / cli.CHECKS_OUT).read_text()
    tmpdir, cwd = seen.read_text().split()
    assert pathlib.Path(tmpdir).parent == ram and shorttmp.owned(tmpdir)
    assert pathlib.Path(cwd).is_relative_to(tmpdir) and pathlib.Path(cwd).name == "tree"
    assert not list(ram.iterdir()), "the run's scratch was left on the RAM disk"
    _no_checkouts(repo, tmp)
    other = shorttmp.make()
    assert other.parent == shorttmp.root() and not shorttmp.on_tmpfs(other)
    shorttmp.remove(other)


@pytest.mark.parametrize("why", ["not-tmpfs", "full", "others", "low-ram", "unusable", "no-ram-disk"])
def test_check_scratch_stays_under_tmp_when_the_ram_disk_has_no_room(env, tmp_path, monkeypatch, why):
    """No RAM disk, or one without room for this run beside the others' scratch, keeping a quarter of it
    free and a fifth of the RAM available: the check's scratch and checkout stay where they were."""
    from ttp import checkdirs, shorttmp
    kw = {"not-tmpfs": {"fstype": "ext4"}, "full": {"free": 35 * _GIB, "size": 128 * _GIB},
          "low-ram": {"total": 100 * _GIB, "avail": 23 * _GIB}}.get(why, {})
    ram = _ram_disk(tmp_path, monkeypatch, **kw)
    if why == "others":   # 3 runs' scratch already there: 4 x 4 GiB leaves under a quarter of 40 GiB
        monkeypatch.setattr(shorttmp, "_space", lambda path: (25 * _GIB, 40 * _GIB))
        assert shorttmp.tmpfs_room(4 * _GIB) == ram
        for name in ("aaaaaaa1", "aaaaaaa2", "aaaaaaa3"):
            (ram / name).mkdir(parents=True)
    elif why == "unusable":   # room, but its ttp-<uid> is a link: never followed
        (tmp_path / "elsewhere").mkdir()
        ram.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    elif why == "no-ram-disk":
        monkeypatch.delenv("TTP_TEST_RAM_DISK")
        assert shorttmp.tmpfs_root() is None
    p = make(env)
    output = checkdirs.new(p.state, need=4 * _GIB)
    tmp = json.loads((output / checkdirs.OWNER).read_text())["tmp"]
    assert pathlib.Path(tmp).parent == shorttmp.root() and shorttmp.on_tmpfs(tmp) is None
    assert not (tmp_path / "elsewhere").exists() or not list((tmp_path / "elsewhere").iterdir())
    checkdirs.finish(output)
    assert not pathlib.Path(tmp).exists()


@pytest.mark.parametrize("others", [0, 1])
def test_room_on_the_ram_disk_counts_the_other_runs_scratch_and_both_limits_at_their_edges(env, tmp_path,
                                                                                          monkeypatch, others):
    """This run's need plus 4 GiB per other run; a quarter of the disk and a fifth of the RAM kept, exactly."""
    from ttp import shorttmp
    need = 4 * _GIB * (others + 1)
    ram = _ram_disk(tmp_path, monkeypatch)
    for i in range(others):
        (ram / f"bbbbbbb{i}").mkdir(parents=True)
    (ram / "not-a-run").mkdir(parents=True)   # only the short dirs count
    for free, ok in ((25 * _GIB + need, True), (25 * _GIB + need - 1, False)):
        monkeypatch.setattr(shorttmp, "_space", lambda path, free=free: (free, 100 * _GIB))
        assert (shorttmp.tmpfs_room(4 * _GIB) == ram) is ok, free
    monkeypatch.setattr(shorttmp, "_space", lambda path: (100 * _GIB, 128 * _GIB))
    for avail, ok in ((20 * _GIB + need, True), (20 * _GIB + need - 1024, False)):
        (tmp_path / "meminfo").write_text(f"MemTotal: {100 * _GIB // 1024} kB\nMemAvailable: {avail // 1024} kB\n")
        assert (shorttmp.tmpfs_room(4 * _GIB) == ram) is ok, avail
    (tmp_path / "meminfo").write_text("garbage\n")   # unreadable RAM figures: no RAM disk
    assert shorttmp.tmpfs_room(4 * _GIB) is None


@pytest.mark.parametrize("end", ["crash", "reboot"])
def test_a_killed_runs_ram_disk_scratch_and_checkout_go_at_recovery(env, tmp_path, monkeypatch, end):
    from ttp import checkdirs, cleantree, shorttmp
    p, repo, run, git, tmp = _clean_repo(env, tmp_path, monkeypatch)
    ram = _ram_disk(tmp_path, monkeypatch)
    output = checkdirs.new(p.state, need=shorttmp.need(repo, "HEAD", ["pytest -q"]))
    scratch = shorttmp.on_tmpfs(json.loads((output / checkdirs.OWNER).read_text())["tmp"])
    assert scratch is not None and scratch.parent == ram
    clean = cleantree.Checkout(repo, "HEAD", scratch)
    assert clean.dir.parent == scratch and (clean.path / ".git").exists()
    clean._lock.close()   # the run was killed
    clean._lock = None
    owner = json.loads((output / checkdirs.OWNER).read_text())
    owner.update(pid=2 ** 22 + 1) if end == "crash" else owner.update(boot="an-earlier-boot")
    (output / checkdirs.OWNER).write_text(json.dumps(owner))
    checkdirs.recover(p.state, p.runs)
    assert not output.exists() and not scratch.exists() and not list(ram.iterdir())
    cleantree.Checkout(repo, "HEAD").close()   # the next run's close prunes the dead checkout's record
    _no_checkouts(repo, tmp)


def test_only_ram_disk_check_dirs_count_as_ours(env, tmp_path, monkeypatch):
    """owned/on_tmpfs/remove and the leftover-tree reaper accept a RAM disk dir only as a direct child of
    its ttp-<uid> with the short name: never a sibling, a nested dir, a /tmp dir, or through a link."""
    from ttp import checkreap, cleantree, shorttmp
    ram = _ram_disk(tmp_path, monkeypatch)
    good = shorttmp.make(4 * _GIB)
    assert good.parent == ram and shorttmp.on_tmpfs(good) == good and shorttmp.owned(good)
    for bad in (ram.parent / "abcdef12", good / "abcdef12", ram / "not-hex!", ram / "ABCDEF12",
                shorttmp.root() / "abcdef12", "", None, 7):
        assert not shorttmp.on_tmpfs(bad), bad
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    (ram / "abcdef34").symlink_to(victim, target_is_directory=True)
    shorttmp.remove(ram / "abcdef34")
    assert (victim / "keep").exists(), "never removed through a link"
    tree = good / f"{cleantree.PREFIX}1-x"
    tree.mkdir()
    stray = victim / f"{cleantree.PREFIX}1-x"
    stray.mkdir()
    assert checkreap._remove_tree(str(stray), "") == 0 and stray.exists()
    assert checkreap._remove_tree(str(ram / "abcdef34" / stray.name), "") == 0 and stray.exists(), "via a link"
    assert checkreap._remove_tree(str(tree), "") == 1 and not tree.exists()
    shorttmp.remove(good)
    assert not good.exists() and (victim / "keep").exists()


def test_a_fenced_worker_may_write_the_ram_disk_check_dirs_and_its_own_temp_stays_under_tmp(env, tmp_path,
                                                                                           monkeypatch):
    """A worker starts checks itself: its sandbox grants the RAM disk's ttp-<uid> beside /tmp's, or its
    checks could not make their scratch there."""
    from ttp import shorttmp
    ram = _ram_disk(tmp_path, monkeypatch)
    roots, run_env = _codex_worker_launch(env, make(env), env["repo"])
    assert str(ram) in roots and str(shorttmp.root()) in roots, roots
    assert pathlib.Path(run_env["TMPDIR"]).parent == shorttmp.root()


def _sized_repo(tmp_path, name, files):
    """A committed repo holding `files` (relative path -> bytes)."""
    repo = tmp_path / name
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    for rel, data in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"], check=True)
    return repo


@pytest.mark.parametrize("case", ["small", "large", "submodule", "cmake", "make", "ninja"])
def test_ram_disk_scratch_is_sized_by_the_real_tree_and_build_checks_stay_under_tmp(env, tmp_path, monkeypatch,
                                                                                   case):
    """A run plans its clean checkout's real size (submodules it clones included) with margin plus temp
    files, at most a quarter of the RAM disk, and a check that builds keeps its output off it: a small
    tree goes there, a large one or a cmake/make/ninja check stays under /tmp. A flat per-run estimate
    put all of these on the RAM disk."""
    from ttp import checkdirs, shorttmp
    mib = 1 << 20
    small = _sized_repo(tmp_path, "small", {"a.py": b"x = 1\n"})
    tree = {"large": {"big.bin": os.urandom(mib)}}.get(case, {"a.py": b"x = 1\n"})
    repo = small if case in ("small", "cmake", "make", "ninja") else _sized_repo(tmp_path, "sized", tree)
    if case == "submodule":   # 1 MiB only in a checked-out submodule: its files and objects count
        sub = _sized_repo(tmp_path, "sub", {"big.bin": os.urandom(mib)})
        subprocess.run(["git", "-C", str(repo), "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                        str(sub), "vendor/sub"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "s"],
                       check=True)
    cmd = {"cmake": "cmake --build build -j 8 && ctest", "make": "make -C tests check",
           "ninja": "cd build && ninja"}.get(case, "pytest -q tests")
    need = shorttmp.need(repo, "HEAD", ["ruff check .", cmd])
    # A RAM disk a quarter of which holds a small tree's run with room to spare, not 1 MiB more.
    size = 4 * (shorttmp.TEMP_BYTES + mib)
    ram = _ram_disk(tmp_path, monkeypatch, free=size, size=size)
    if case in ("cmake", "make", "ninja"):
        assert need is None
    elif case == "small":
        assert shorttmp.TEMP_BYTES < need < shorttmp.TEMP_BYTES + 1024
    else:
        assert need >= shorttmp.TEMP_BYTES + 1.5 * mib, need
    p = make(env)
    output = checkdirs.new(p.state, need=need)
    tmp = json.loads((output / checkdirs.OWNER).read_text())["tmp"]
    where = pathlib.Path(tmp).parent
    assert where == (ram if case == "small" else shorttmp.root()), (case, need, where)
    checkdirs.finish(output)
    assert shorttmp.need(tmp_path / "no-repo", "HEAD", ["pytest"]) is None   # unreadable tree: /tmp


def test_the_ram_disk_test_hook_is_not_the_suites_tmpfs_opt_out(env, tmp_path, monkeypatch):
    """TTP_TEST_TMPFS=0 opts the suite out of tmpfs (conftest.py); it never names a RAM disk folder."""
    from ttp import shorttmp
    monkeypatch.delenv("TTP_TEST_RAM_DISK", raising=False)
    monkeypatch.chdir(tmp_path)
    for value in ("0", "1", str(tmp_path)):
        monkeypatch.setenv("TTP_TEST_TMPFS", value)
        assert shorttmp.tmpfs_root() is None, value
        tmp = shorttmp.make(4 * _GIB)
        assert tmp.parent == shorttmp.root() and not (tmp_path / "0").exists()
        shorttmp.remove(tmp)


_BIND = ("import os, socket, sys, tempfile; t = tempfile.gettempdir(); "
         "d = os.path.join(t, 'pytest-of-someone', 'pytest-12', 'test_a_check_that_binds_a_socket0'); "
         "os.makedirs(d); s = socket.socket(socket.AF_UNIX)\n"
         "try:\n    s.bind(os.path.join(d, 'sock')); ok = 'bound'\n"
         "except OSError as e:\n    ok = 'failed'\n"
         "open(sys.argv[1], 'w').write(t + '\\n' + ok + '\\n')")


@pytest.mark.parametrize("recorded", ["missing", "reused", "none-possible"])
def test_a_push_queued_from_a_sandbox_runs_its_checks_in_a_short_temp_dir(env, tmp_path, monkeypatch, capsys,
                                                                          recorded):
    """A push queued from a worker's sandbox records a short scratch that may exist only in there: the
    daemon makes a new one when the recorded one is missing, reuses one that is still there and this
    user's, and only when no short dir can be made logs and uses the long one under check-runs, where
    an AF_UNIX socket under a pytest basetemp passes the path limit."""
    from ttp import checkdirs, push, shorttmp
    from ttp.project import Project
    seen, script = tmp_path / "seen", tmp_path / "bind.py"
    script.write_text(_BIND)
    p, repo, origin, other = _push_setup(env, monkeypatch, [shlex.join([sys.executable, str(script), str(seen)])])
    _commit(repo, "mine.txt", "mine\n")
    monkeypatch.setattr(push, "_boxed", lambda: True)
    assert _ttp("push", "--detach") == 0
    marker = pathlib.Path(next(ln.split(": ", 1)[1] for ln in capsys.readouterr().out.splitlines()
                               if ln.startswith("marker: ")))
    output = pathlib.Path(push._read(marker)["check_run_dir"])
    queued = json.loads((output / checkdirs.OWNER).read_text())["tmp"]
    assert shorttmp.owned(queued) and pathlib.Path(queued).is_dir()
    if recorded != "reused":
        shorttmp.remove(queued)   # it was made in the worker's sandbox, gone with it
    if recorded == "none-possible":
        monkeypatch.setattr(shorttmp, "make", lambda need=None: None)
    push.tend(Project(p.base))
    result = _probe_until_done(p, push.result_probe(marker, p))
    log = marker.with_suffix(".log").read_text()
    assert "ttp push: pushed" in result.stdout, log
    tmpdir, bound = seen.read_text().split()
    line = next(ln for ln in log.splitlines() if "check scratch: " in ln)
    if recorded == "none-possible":
        assert pathlib.Path(tmpdir) == output / checkdirs.SCRATCH
        assert f"check scratch: {output / checkdirs.SCRATCH} (long fallback" in line, line
        return
    assert "long fallback" not in log
    assert pathlib.Path(tmpdir).parent == shorttmp.root() and shorttmp.owned(tmpdir)
    assert (tmpdir == queued) is (recorded == "reused"), (tmpdir, queued)
    assert f"check scratch: {tmpdir} (" in line
    assert ("recorded when it was queued" if recorded == "reused" else "made at start") in line, line
    assert len(tmpdir) < len(str(output / checkdirs.SCRATCH))
    sock = len(os.path.join(tmpdir, "pytest-of-someone/pytest-12/test_a_check_that_binds_a_socket0/sock"))
    if sock <= 104:   # the short test root itself may be long in a sandbox refusing /tmp
        assert bound == "bound"
    assert _wait_for(lambda: not pathlib.Path(tmpdir).exists()), "the push's short scratch was left behind"


@pytest.mark.parametrize("recorded", ["file", "link", "foreign", "no-record"])
def test_a_recorded_short_scratch_that_is_not_this_users_directory_is_replaced(env, tmp_path, monkeypatch,
                                                                              recorded):
    """The daemon never reuses a recorded short scratch that is a file, a link or another user's
    directory: it makes and records a new one, leaving the old one alone. An allocation with no owner
    record gets none, since nothing would remove it."""
    from ttp import checkdirs, shorttmp
    p = make(env)
    output = checkdirs.new(p.state)
    old = pathlib.Path(json.loads((output / checkdirs.OWNER).read_text())["tmp"])
    if recorded in ("file", "link"):
        old.rmdir()
        if recorded == "file":
            old.write_text("x")
        else:
            (tmp_path / "elsewhere").mkdir()
            old.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    elif recorded == "foreign":
        ino, real = old.stat().st_ino, os.fstat

        def fstat(fd):
            st = real(fd)
            return os.stat_result((*st[:4], st.st_uid + 1, *st[5:])) if st.st_ino == ino else st
        monkeypatch.setattr(checkdirs.os, "fstat", fstat)
    else:
        (output / checkdirs.OWNER).unlink()
    with checkdirs.take(output) as guard:
        assert guard is not None
        tmp, how = checkdirs.short(output, lambda: None)
        if recorded == "no-record":
            assert (tmp, how) == (None, "long") and not (output / checkdirs.OWNER).exists()
            return
        assert how == "made" and tmp != str(old) and shorttmp.owned(tmp) and pathlib.Path(tmp).is_dir()
        assert json.loads((output / checkdirs.OWNER).read_text())["tmp"] == tmp
        assert checkdirs.short(output) == (tmp, "reused")
        assert os.path.lexists(old)   # not ours to remove
    checkdirs.finish(output)
    assert not pathlib.Path(tmp).exists()


def test_detached_checks_off_the_ram_disk_keep_their_clean_tree_and_tmpdir_out_of_the_callers_tmpdir(
        env, tmp_path, monkeypatch):
    """With the scratch on disk (no RAM disk), a detached run made its clean checkout in the launching
    session's TMPDIR: that session ending mid-run removed the tree and the run showed test failures.
    The tree and the checks' TMPDIR go in the managed scratch, so removing the caller's TMPDIR after
    the start fails nothing."""
    from ttp import cli, shorttmp
    p, repo, run, git, tmp = _clean_repo(env, tmp_path, monkeypatch)
    assert shorttmp.tmpfs_root() is None
    caller = tmp_path / "caller-tmp"
    caller.mkdir()
    monkeypatch.setenv("TMPDIR", str(caller))
    seen = tmp_path / "seen"
    cli.main(["checks", "--detach", "--fresh", "--",
              f"sh -c 'echo $TMPDIR; pwd; rm -rf {caller}; sleep 0.2; cat tests/test_a.txt' > {seen}"])
    assert _wait_precheck(run) == "0", (run / cli.CHECKS_OUT).read_text()
    tmpdir, cwd, data = seen.read_text().split()
    assert not pathlib.Path(tmpdir).is_relative_to(caller) and not pathlib.Path(cwd).is_relative_to(caller)
    assert pathlib.Path(cwd).is_relative_to(tmpdir) and pathlib.Path(cwd).name == "tree" and data == "a"
    assert not caller.exists()


@pytest.mark.parametrize("how", ["check", "added-tests"])
def test_a_clean_tree_removed_under_a_detached_check_reports_killed_not_test_failures(env, tmp_path, monkeypatch,
                                                                                      capsys, how):
    """A check whose clean checkout went while it ran failed with what the missing files made of it
    (FAILED lines, files 'present only in the authoring worktree'). It is killed: rc `killed`, so the
    waits run the checks again, and the report names the removed tree only."""
    from ttp import addedtests, cli
    p, repo, run, git, tmp = _clean_repo(env, tmp_path, monkeypatch)
    (repo / "tests" / "fixture.log").write_text("data\n")   # only in the authoring worktree
    gone = "rm -rf \"$PWD\"; echo FAILED tests/test_x.py::test_y; cat tests/fixture.log; exit 1"
    if how == "added-tests":   # the tree goes on the rerun of the tests the diff adds
        monkeypatch.setattr(addedtests, "added", lambda *a: [("tests/test_x.py", "test_y")])
        monkeypatch.setattr(addedtests, "eligible", lambda c: True)
        cmd = f"sh -c 'test -n \"$TTP_ADDED_TESTS_RUN\" || exit 0; {gone}'"
        monkeypatch.setattr(addedtests, "run", lambda tests, c, run_check, path, e: (
            run_check({**e, "TTP_ADDED_TESTS_RUN": "1"}, None),
            {"status": "failed", "rc": 1, "flaky": [], "tests": tests, "rounds": 1, "why": "exit 1"})[1])
        with pytest.raises(SystemExit) as e:   # in the foreground: 75, neither passed nor failed
            cli.main(["checks", "--fresh", "--", cmd])
        assert e.value.code == 75 and isinstance(e.value, cli.ChecksKilled)
        err = capsys.readouterr().err
    else:
        cli.main(["checks", "--detach", "--fresh", "--", f"sh -c '{gone}'"])
        assert _wait_precheck(run) == "killed", (run / cli.CHECKS_OUT).read_text()
        assert cli._checks_result(run) == 0
        err = capsys.readouterr().out + (run / cli.CHECKS_OUT).read_text()
    assert f"ttp checks: {cli.TREE_GONE}: " in err
    assert not [l for l in err.splitlines() if l.startswith("FAILED")]
    assert "present only in the authoring worktree" not in err
    assert json.loads((run / "checks.json").read_text())["killed"].startswith(cli.TREE_GONE)
    assert not (run / "check_failure.json").exists()
    _no_checkouts(repo, tmp)
