# Start-up file index tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new indexignore.py tests (the project root's .ignore block) here.
from __future__ import annotations


def _ii_project(tmp_path, git: bool = False):
    from ttp.project import Project
    root = tmp_path / "proj"
    (root / "tt-project" / "state" / "runs" / "1").mkdir(parents=True)
    if git:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
    return Project(root)


# Cached per session so the probe (which spawns a --version) runs once, not per test.
_II_RG = None


def _ii_rg():
    """A usable ripgrep, or None.

    PATH first, then Claude Code's bundled ripgrep (a `claude` binary that embeds
    ripgrep and routes on argv0 'rg'), probed once per session and validated with
    --version so the tests never run against something that is not ripgrep.
    """
    global _II_RG
    if _II_RG is not None:
        return _II_RG
    import atexit, shutil, subprocess, tempfile
    rg = shutil.which("rg")
    if not rg:
        # Claude Code's CLI embeds ripgrep and runs it when invoked with argv0 'rg'.
        # A wrapper script execs the resolved `claude` on PATH with argv0 'rg'; its
        # --version then prints a ripgrep version (the CLI's own version would not).
        claude = shutil.which("claude")
        if claude:
            wrapper = os.path.join(tempfile.gettempdir(), f"ii-rg-{os.getpid()}")
            try:
                with open(wrapper, "w") as f:
                    f.write(f'#!/bin/bash\nexec -a rg "{claude}" "$@"\n')
                os.chmod(wrapper, 0o755)
                out = subprocess.run([wrapper, "--version"], capture_output=True, text=True, timeout=30)
                if out.returncode == 0 and "ripgrep" in out.stdout:
                    rg = wrapper
                    atexit.register(os.unlink, wrapper)
                else:
                    os.unlink(wrapper)
            except OSError:
                rg = None
    if rg:
        try:
            v = subprocess.run([rg, "--version"], capture_output=True, text=True, timeout=30)
            ok = v.returncode == 0 and "ripgrep" in v.stdout
        except OSError:
            ok = False
        if not ok:
            rg = None
    _II_RG = rg or ""
    return _II_RG or None


def test_index_ignore_lists_the_bulky_folders_anchored_at_the_root(tmp_path):
    from ttp import indexignore
    p = _ii_project(tmp_path)
    assert indexignore.ensure(p).startswith("wrote ")
    lines = (p.root / ".ignore").read_text().splitlines()
    for d in ("/tt-project/state/", "/tt-project/worktrees/", "/results/", "/tmp/", "/runs/", "/worktrees/",
              "/.venv/", "/venv/"):
        assert d in lines
    # Never the harness checkout (charter, memory, prompts) nor the folder as a whole.
    assert not [ln for ln in lines if ln.startswith("/") and ln.rstrip("/") in ("/tt-project", "/tt-project/harness")]
    assert indexignore.ensure(p) == ""   # nothing to do the second time
    assert (p.root / ".ignore").read_text().splitlines() == lines


def test_index_ignore_keeps_a_projects_own_lines_and_refreshes_only_its_block(tmp_path):
    from ttp import indexignore
    p = _ii_project(tmp_path)
    (p.root / ".ignore").write_text("data/\n!data/keep.txt")   # no trailing newline
    assert indexignore.ensure(p).startswith("added ")
    text = (p.root / ".ignore").read_text()
    assert text.startswith("data/\n!data/keep.txt\n" + indexignore.BEGIN + "\n")
    stale = text.replace("/results/\n", "/results/\n/old-entry/\n") + "build/\n"
    (p.root / ".ignore").write_text(stale)
    assert indexignore.ensure(p).startswith("refreshed ")
    assert (p.root / ".ignore").read_text() == text + "build/\n"
    assert indexignore.ensure(p) == ""
    assert (p.root / ".ignore").read_text().count(indexignore.BEGIN) == 1


def test_index_ignore_leaves_a_tracked_ignore_file_without_its_block_alone(tmp_path):
    from ttp import indexignore
    p = _ii_project(tmp_path, git=True)
    (p.root / ".ignore").write_text("vendor/\n")
    g = ["git", "-C", str(p.root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(g + ["add", ".ignore"], check=True)
    subprocess.run(g + ["commit", "-qm", "own ignore"], check=True)
    assert indexignore.ensure(p) == ""
    assert (p.root / ".ignore").read_text() == "vendor/\n"
    assert subprocess.run(g + ["status", "--porcelain", "--", ".ignore"], capture_output=True,
                          text=True).stdout == ""


def test_index_ignore_it_creates_in_a_git_root_shows_in_no_git_status(tmp_path):
    from ttp import indexignore
    p = _ii_project(tmp_path, git=True)
    indexignore.ensure(p)
    (p.root / ".ignore").unlink()
    indexignore.ensure(p)   # made again: the exclude line is not written twice
    exclude = (p.root / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count("/.ignore") == 1
    st = subprocess.run(["git", "-C", str(p.root), "status", "--porcelain", "--untracked-files=all"],
                        capture_output=True, text=True).stdout
    assert ".ignore" not in st
    # Git itself still sees the listed folders: rg reads .ignore, git does not.
    (p.root / "results").mkdir()
    (p.root / "results" / "r.csv").write_text("1\n")
    st = subprocess.run(["git", "-C", str(p.root), "status", "--porcelain", "--untracked-files=all"],
                        capture_output=True, text=True).stdout
    assert "results/r.csv" in st


def test_index_ignore_in_a_subfolder_of_a_repository_touches_no_exclude_file(tmp_path):
    from ttp import indexignore
    from ttp.project import Project
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    root = tmp_path / "sub"
    (root / "tt-project").mkdir(parents=True)
    exclude = tmp_path / ".git" / "info" / "exclude"
    before = exclude.read_text() if exclude.exists() else None
    indexignore.ensure(Project(root))
    assert (root / ".ignore").is_file()
    assert (exclude.read_text() if exclude.exists() else None) == before


@pytest.mark.skipif(not _ii_rg(), reason="no ripgrep on PATH and none bundled with a CLI install")
def test_index_ignore_stops_the_start_up_walk_but_not_searches_below_or_by_path(tmp_path):
    """The walk a CLI does at start-up (rg --no-config --files --hidden <cwd>) skips the listed folders
    from the root and the harness folder; a worktree cwd, an explicit path and --no-ignore still see them."""
    from ttp import indexignore
    p = _ii_project(tmp_path)
    for rel in ("results/a/r.txt", "tmp/t.txt", "src/s.txt", "tt-project/state/runs/1/x.txt",
                "tt-project/harness/CHARTER.md", "tt-project/worktrees/t1/src/w.txt"):
        (p.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (p.root / rel).write_text("hit\n")
    indexignore.ensure(p)

    def files(cwd, *extra):
        out = subprocess.run([_ii_rg(), "--no-config", "--files", "--hidden", *extra], cwd=cwd,
                             capture_output=True, text=True).stdout
        return sorted(ln for ln in out.splitlines() if not ln.endswith(".ignore"))
    assert files(p.root) == ["src/s.txt", "tt-project/harness/CHARTER.md"]
    assert files(p.base) == ["harness/CHARTER.md"]
    assert files(p.base / "worktrees" / "t1") == ["src/w.txt"]
    assert files(p.root, "results") == ["results/a/r.txt"]
    assert "results/a/r.txt" in files(p.root, "--no-ignore")


@pytest.mark.skipif(not _ii_rg(), reason="no ripgrep on PATH and none bundled with a CLI install")
def test_index_ignore_cuts_the_start_up_walk_of_a_root_outside_git(tmp_path):
    """A project root that is no git repository reads no .gitignore: before the block the start-up walk
    lists every result, run folder and virtualenv file; after it, only the project's own files."""
    from ttp import indexignore
    p = _ii_project(tmp_path)
    assert subprocess.run(["git", "-C", str(p.root), "rev-parse", "--git-dir"], capture_output=True,
                          env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)}).returncode != 0
    for d, n in (("results/run", 1500), ("tt-project/state/runs/7", 1500), (".venv/lib", 500), ("tmp", 200)):
        (p.root / d).mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (p.root / d / f"f{i}.txt").write_text("x")
    for rel in ("scripts/run.sh", "tt-project/harness/CHARTER.md", "tt-project/harness/MEMORY.md"):
        (p.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (p.root / rel).write_text("x")

    def walk():
        out = subprocess.run([_ii_rg(), "--no-config", "--files", "--hidden"], cwd=p.root,
                             capture_output=True, text=True).stdout
        return sorted(ln for ln in out.splitlines() if not ln.endswith(".ignore"))
    assert len(walk()) == 3703
    indexignore.ensure(p)
    assert walk() == ["scripts/run.sh", "tt-project/harness/CHARTER.md", "tt-project/harness/MEMORY.md"]


def test_index_ignore_is_written_at_daemon_start(env, monkeypatch):
    from ttp import daemon as dm, indexignore, web
    p = make(env)
    (p.root / ".ignore").unlink(missing_ok=True)
    d = dm.Daemon(p.base)
    d.stopping = True   # start up, skip the loop
    monkeypatch.setattr(web, "serve", lambda daemon: None)
    monkeypatch.setattr(dm.signal, "signal", lambda *a: None)
    assert d.run() == 0
    assert indexignore.BEGIN in (p.root / ".ignore").read_text()


def test_index_ignore_is_written_at_bootstrap_new(env, monkeypatch):
    """`ttp new` writes the managed .ignore block at bootstrap, before any daemon runs."""
    from ttp import cli, indexignore
    from ttp.project import Project

    def none(*a, **k):
        raise AssertionError("no daemon or web app here")
    monkeypatch.setattr(cli, "_wait_for_daemon", none)
    cli.main(["new", "ii-boot", "--dir", str(env["repo"]), "--provider", "fake", "--no-service"])
    text = (env["repo"] / ".ignore").read_text()
    assert indexignore.BEGIN in text and "/results/" in text
    assert indexignore.ensure(Project(env["repo"])) == ""
    # a second bootstrap of the same project neither duplicates nor loses the block
    cli.main(["new", "ii-boot", "--dir", str(env["repo"]), "--provider", "fake", "--no-service"])
    assert (env["repo"] / ".ignore").read_text().count(indexignore.BEGIN) == 1


def test_index_ignore_never_raises_and_leaves_odd_files_and_home_alone(tmp_path, monkeypatch):
    from ttp import indexignore
    from ttp.project import Project
    p = _ii_project(tmp_path)
    (p.root / ".ignore").mkdir()   # a folder by that name
    assert indexignore.ensure(p) == "" and (p.root / ".ignore").is_dir()
    (p.root / ".ignore").rmdir()
    (tmp_path / "shared.ignore").write_text("x/\n")
    (p.root / ".ignore").symlink_to(tmp_path / "shared.ignore")
    assert indexignore.ensure(p) == "" and (tmp_path / "shared.ignore").read_text() == "x/\n"
    (p.root / ".ignore").unlink()
    (p.root / ".ignore").write_bytes(b"\xff\xfe\n")
    assert indexignore.ensure(p).startswith("left ")
    (p.root / ".ignore").unlink()
    p.root.chmod(0o555)   # cannot write the root
    try:
        assert indexignore.ensure(p).startswith("left ") or os.geteuid() == 0
    finally:
        p.root.chmod(0o755)
    home = tmp_path / "home"
    (home / "tt-project").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert indexignore.ensure(Project(home)) == "" and not (home / ".ignore").exists()
