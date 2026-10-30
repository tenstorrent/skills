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
    monkeypatch.setenv("TTP_TEST_TMPFS", str(ram))
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
    monkeypatch.setenv("TTP_TEST_TMPFS", str(tmp_path / "ram"))
    ram = shorttmp.tmpfs_room()
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
        assert shorttmp.tmpfs_room() == ram
        for name in ("aaaaaaa1", "aaaaaaa2", "aaaaaaa3"):
            (ram / name).mkdir(parents=True)
    elif why == "unusable":   # room, but its ttp-<uid> is a link: never followed
        (tmp_path / "elsewhere").mkdir()
        ram.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    elif why == "no-ram-disk":
        monkeypatch.delenv("TTP_TEST_TMPFS")
        assert shorttmp.tmpfs_root() is None
    p = make(env)
    output = checkdirs.new(p.state)
    tmp = json.loads((output / checkdirs.OWNER).read_text())["tmp"]
    assert pathlib.Path(tmp).parent == shorttmp.root() and shorttmp.on_tmpfs(tmp) is None
    assert not (tmp_path / "elsewhere").exists() or not list((tmp_path / "elsewhere").iterdir())
    checkdirs.finish(output)
    assert not pathlib.Path(tmp).exists()


@pytest.mark.parametrize("others", [0, 1])
def test_room_on_the_ram_disk_counts_the_other_runs_scratch_and_both_limits_at_their_edges(env, tmp_path,
                                                                                          monkeypatch, others):
    """4 GiB per run including this one; a quarter of the disk and a fifth of the RAM kept, exactly."""
    from ttp import shorttmp
    need = 4 * _GIB * (others + 1)
    ram = _ram_disk(tmp_path, monkeypatch)
    for i in range(others):
        (ram / f"bbbbbbb{i}").mkdir(parents=True)
    (ram / "not-a-run").mkdir(parents=True)   # only the short dirs count
    for free, ok in ((25 * _GIB + need, True), (25 * _GIB + need - 1, False)):
        monkeypatch.setattr(shorttmp, "_space", lambda path, free=free: (free, 100 * _GIB))
        assert (shorttmp.tmpfs_room() == ram) is ok, free
    monkeypatch.setattr(shorttmp, "_space", lambda path: (100 * _GIB, 128 * _GIB))
    for avail, ok in ((20 * _GIB + need, True), (20 * _GIB + need - 1024, False)):
        (tmp_path / "meminfo").write_text(f"MemTotal: {100 * _GIB // 1024} kB\nMemAvailable: {avail // 1024} kB\n")
        assert (shorttmp.tmpfs_room() == ram) is ok, avail
    (tmp_path / "meminfo").write_text("garbage\n")   # unreadable RAM figures: no RAM disk
    assert shorttmp.tmpfs_room() is None


@pytest.mark.parametrize("end", ["crash", "reboot"])
def test_a_killed_runs_ram_disk_scratch_and_checkout_go_at_recovery(env, tmp_path, monkeypatch, end):
    from ttp import checkdirs, cleantree, shorttmp
    p, repo, run, git, tmp = _clean_repo(env, tmp_path, monkeypatch)
    ram = _ram_disk(tmp_path, monkeypatch)
    output = checkdirs.new(p.state)
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
    good = shorttmp.make(check=True)
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
