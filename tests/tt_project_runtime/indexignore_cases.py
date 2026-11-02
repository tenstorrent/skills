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


def _ii_rg():
    return shutil.which("rg")


def test_index_ignore_lists_the_bulky_folders_anchored_at_the_root(tmp_path):
    from ttp import indexignore
    p = _ii_project(tmp_path)
    assert indexignore.ensure(p).startswith("wrote ")
    lines = (p.root / ".ignore").read_text().splitlines()
    for d in ("/tt-project/state/", "/tt-project/worktrees/", "/results/", "/tmp/", "/runs/", "/worktrees/"):
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


@pytest.mark.skipif(not shutil.which("rg"), reason="needs ripgrep")
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
