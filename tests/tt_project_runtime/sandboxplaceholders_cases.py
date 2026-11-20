# Sandbox placeholder tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file. The worker sandbox leaves
# an empty file at each path it mounts over (providers.claude.SANDBOX_PLACEHOLDERS); a template upgrade
# must never commit those, and untracks the empty ones an older upgrade committed.
from __future__ import annotations

_PLACEHOLDERS = (".bashrc", ".profile", ".gitconfig", ".gitmodules", ".mcp.json", ".ripgreprc", ".idea",
                 ".vscode", ".claude/agents", ".claude/hooks")


def _leave_placeholders(root, names=_PLACEHOLDERS):
    """Empty files where the sandbox mounted over a path, as bubblewrap leaves them."""
    for n in names:
        (root / n).parent.mkdir(parents=True, exist_ok=True)
        (root / n).write_bytes(b"")


def _upgraded_harness(env, monkeypatch, capsys):
    from ttp import cli, service
    p = make(env)
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    _install_template(env)
    mark = env["home"] / "lib" / "current" / "runtime" / "ttp" / "SOURCE_COMMIT"
    mark.write_text("aaaa1111\n")
    cli.main(["upgrade", "demo"])
    capsys.readouterr()
    return p, mark


def test_placeholder_list_has_one_definition_and_matches_the_reported_paths():
    from ttp import cli
    from ttp.providers.claude import SANDBOX_PLACEHOLDERS
    assert set(_PLACEHOLDERS) <= set(SANDBOX_PLACEHOLDERS)
    src = (pathlib.Path(cli.__file__).parent).glob("**/*.py")
    defined = [f.name for f in src if '".ripgreprc"' in f.read_text()]
    assert defined == ["claude.py"]
    assert cli._sandbox_path(".claude/hooks/x.sh") and not cli._sandbox_path(".claude/hooksy")
    assert not cli._sandbox_path("tools/.bashrc") and not cli._sandbox_path(".claude")


@pytest.mark.parametrize("running", [False, True])
def test_pre_upgrade_commit_stages_no_sandbox_placeholder(env, monkeypatch, capsys, running):
    """The pre-upgrade commit staged with `git add -A` and swept the sandbox's empty mount-point files into
    the harness; every sandboxed task worktree then failed to check out or rebase. They stay out now, also
    when a worker staged one, while a new harness file still goes in; a placeholder path with real content
    is left out too and named."""
    from ttp import cli
    p, mark = _upgraded_harness(env, monkeypatch, capsys)
    h = p.harness
    before = _git_out(h, "rev-parse", "HEAD")
    if running:
        tid = p.db.add_task("edit", "spec", kind="harness")
        p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(?,'worker','claude',?,'running')",
               (tid, time.time() + 3600))
    _leave_placeholders(h)
    (h / ".mcp.json").write_text('{"mcpServers": {}}\n')
    (h / "tools").mkdir(exist_ok=True)
    (h / "tools" / "new_check.py").write_text("print('new')\n")
    _git_out(h, "add", "--", ".gitconfig")       # a worker's `git add -A` staged one
    (h / ".claude" / ".cc-writes").mkdir()       # an ignored one: excluding it by name would fail the add
    (h / ".claude" / ".cc-writes" / "w").write_bytes(b"")
    with open(h / _git_out(h, "rev-parse", "--git-path", "info/exclude"), "a") as fh:
        fh.write("/.claude/.cc-writes\n")
    mark.write_text("bbbb2222\n")
    cli.main(["upgrade", "demo"])
    out = capsys.readouterr().out
    sha = _git_out(h, "log", "--format=%H", "--grep", "before template upgrade", "-1")
    assert sha and _git_out(h, "merge-base", "--is-ancestor", before, sha) == ""
    assert _git_out(h, "show", "--name-only", "--format=", sha).split("\n") == ["tools/new_check.py"]
    tracked = _git_out(h, "ls-files").split("\n")
    assert not [n for n in tracked if cli._sandbox_path(n)]
    assert "which hold content: .mcp.json;" in out
    assert "untracked " not in out        # nothing committed to untrack
    for n in _PLACEHOLDERS:
        assert (h / n).exists()


def test_upgrade_untracks_committed_empty_placeholders_once_and_keeps_ones_with_content(env, monkeypatch, capsys):
    """A harness an older upgrade swept placeholders into: the empty ones (files, and a directory of only
    empty files) are untracked in one commit that takes nothing else staged; one with content is kept and
    named. The next upgrade has nothing to do."""
    from ttp import cli
    p, mark = _upgraded_harness(env, monkeypatch, capsys)
    h = p.harness
    _leave_placeholders(h, (".bashrc", ".zshrc", ".claude/hooks", ".vscode/settings.json", ".idea/a", ".idea/b"))
    (h / ".claude" / "settings.json").write_text('{"model": "x"}\n')      # real content: kept
    (h / ".idea" / "b").write_text("real\n")                               # one real file keeps the directory
    _git_out(h, "add", "-A")
    _git_out(h, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "local harness changes before "
             "template upgrade")
    swept = _git_out(h, "rev-parse", "HEAD")
    tid = p.db.add_task("edit", "spec", kind="harness")        # a running worker's newer staged file
    p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(?,'worker','claude',?,'running')",
           (tid, time.time() - 60))
    (h / "tools").mkdir(exist_ok=True)
    (h / "tools" / "wip.py").write_text("print('wip')\n")
    _git_out(h, "add", "tools/wip.py")
    mark.write_text("bbbb2222\n")
    cli.main(["upgrade", "demo"])
    out = capsys.readouterr().out
    shas = _git_out(h, "log", "--format=%H", f"{swept}..HEAD", "--grep", "untrack sandbox placeholder").split()
    assert len(shas) == 1
    gone = _git_out(h, "show", "--name-status", "--format=", shas[0]).split("\n")
    assert sorted(gone) == ["D\t.bashrc", "D\t.claude/hooks", "D\t.vscode/settings.json", "D\t.zshrc"]
    assert "untracked 4 empty sandbox placeholder path(s)" in out
    assert "which hold content: .claude/settings.json, .idea" in out
    tracked = set(_git_out(h, "ls-files").split("\n"))
    assert {".claude/settings.json", ".idea/a", ".idea/b", "tools/wip.py"} <= tracked
    assert ".bashrc" not in tracked and (h / ".bashrc").exists()          # kept on disk, untracked
    assert _git_out(h, "diff", "--cached", "--name-only") == "tools/wip.py"   # still the worker's, staged
    mark.write_text("cccc3333\n")
    cli.main(["upgrade", "demo"])
    out = capsys.readouterr().out
    assert "untracked " not in out and len(_git_out(h, "log", "--format=%H", "--grep",
                                                    "untrack sandbox placeholder").split()) == 1


def test_task_worktree_checks_out_and_rebases_after_the_upgrade_with_placeholders_present(env, monkeypatch,
                                                                                          capsys, tmp_path):
    """A task worktree holding the sandbox's untracked placeholders could neither check out nor rebase onto
    a harness main that tracked them ('untracked working tree files would be overwritten'); after the
    upgrade untracks them, both succeed."""
    from ttp import cli
    p, mark = _upgraded_harness(env, monkeypatch, capsys)
    h = p.harness
    base = _git_out(h, "rev-parse", "HEAD")
    _leave_placeholders(h)
    _git_out(h, "add", "-A")
    _git_out(h, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "swept placeholders")
    main = _git_out(h, "rev-parse", "--abbrev-ref", "HEAD")
    wt = tmp_path / "task-wt"
    _git_out(h, "worktree", "add", "-q", "-b", "task", str(wt), base)
    (wt / "notes.md").write_text("task work\n")
    _git_out(wt, "add", "notes.md")
    _git_out(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "task work")
    _leave_placeholders(wt)
    r = subprocess.run(["git", "-C", str(wt), "checkout", "-q", "--detach", main], capture_output=True, text=True)
    assert r.returncode != 0 and "would be overwritten" in r.stderr
    mark.write_text("bbbb2222\n")
    cli.main(["upgrade", "demo"])
    subprocess.run(["git", "-C", str(wt), "checkout", "-q", "--detach", main], check=True)
    subprocess.run(["git", "-C", str(wt), "checkout", "-q", "task"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", "rebase", "-q", main],
                   check=True, capture_output=True)
    assert (wt / "notes.md").exists() and _git_out(wt, "merge-base", "--is-ancestor", main, "HEAD") == ""


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
    # A worker whose working directory was inside runtime/ttp leaves sandbox placeholders there (device
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
