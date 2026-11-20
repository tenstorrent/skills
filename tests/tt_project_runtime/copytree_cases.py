# Runtime copy tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file. Copying the runtime
# (cli._copy_tree) skips .claude folders and entries that are neither files nor folders.
from __future__ import annotations


@contextlib.contextmanager
def _fails_after(seconds):
    """A copy that reads a FIFO blocks for good: fail the test instead of hanging the suite. The timer
    repeats, as shutil.copytree collects each error and goes on to the next FIFO."""
    def boom(*_):
        raise TimeoutError(f"still copying after {seconds}s (read a FIFO?)")
    old = signal.signal(signal.SIGALRM, boom)
    signal.setitimer(signal.ITIMER_REAL, seconds, 1)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def test_bootstrap_copies_a_runtime_holding_special_entries_and_claude_folders_without_them(env, monkeypatch,
                                                                                           capsys):
    # A worker whose working directory was inside runtime/ttp may leave special entries there (device
    # nodes, a .claude folder): bootstrap skips them, says so once, and copies files and symlinks as before.
    from ttp import cli
    runtime = env["home"].parent / "runtime"
    shutil.copytree(cli.RUNTIME, runtime, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    pkg = runtime / "ttp"
    os.mkfifo(pkg / "pipe")
    (pkg / ".claude" / "agents").mkdir(parents=True)
    os.mkfifo(pkg / ".claude" / "hooks")
    (pkg / ".claude" / "agents" / "a.md").write_text("x\n")
    (pkg / "sub").mkdir()
    os.mkfifo(pkg / "sub" / "fifo")
    (pkg / "linked.txt").symlink_to("SOURCE_COMMIT" if (pkg / "SOURCE_COMMIT").exists() else "cli.py")
    monkeypatch.setattr(cli, "RUNTIME", runtime)
    with _fails_after(60):
        p = make(env)
    copy = p.harness / "runtime" / "ttp"
    assert (copy / "cli.py").read_text() == (pkg / "cli.py").read_text()
    assert not (copy / "pipe").exists() and not (copy / ".claude").exists() and not (copy / "sub" / "fifo").exists()
    assert (copy / "sub").is_dir() and (copy / "linked.txt").is_file()
    err = capsys.readouterr().err
    assert err.count("skipped 3 special entries") == 1
    assert "ttp/.claude" in err and "ttp/pipe" in err and "ttp/sub/fifo" in err


def test_copy_tree_skips_a_socket_and_a_link_to_a_fifo_and_keeps_a_folder_merely_named_like_claude(tmp_path, capsys,
                                                                                                   monkeypatch):
    from ttp import cli
    src, dst = tmp_path / "s", tmp_path / "d"
    (src / ".claude-plugin").mkdir(parents=True)
    (src / ".claude-plugin" / "plugin.json").write_text("{}\n")
    (src / ".claude").write_text("a file, not a folder\n")
    os.mkfifo(tmp_path / "fifo")
    (src / "to-fifo").symlink_to(tmp_path / "fifo")
    sock = socket.socket(socket.AF_UNIX)
    monkeypatch.chdir(src)   # a relative name: macOS caps an AF_UNIX path at 104 bytes, under its long tmp path
    sock.bind("s.sock")
    try:
        with _fails_after(30):
            cli._copy_tree(src, dst)
    finally:
        sock.close()
    assert sorted(x.name for x in dst.iterdir()) == [".claude", ".claude-plugin"]
    assert "skipped 2 special entries (s.sock, to-fifo)" in capsys.readouterr().err
