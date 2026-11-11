# Spotlight tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new noindex.py tests (macOS's state.noindex/ and worktrees.noindex/) here.
from __future__ import annotations


def _ni_darwin(monkeypatch):
    from ttp import noindex
    monkeypatch.setattr(noindex, "wanted", lambda platform=None: True)


def _ni_git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def test_a_new_macos_project_keeps_state_and_worktrees_under_noindex_with_links(env, monkeypatch):
    from ttp import checkdirs, cli
    _ni_darwin(monkeypatch)
    root = env["tmp"] / "mac"
    shutil.copytree(env["repo"], root, symlinks=True, ignore=shutil.ignore_patterns("tt-project"))
    p = cli.bootstrap(root, "macdemo", MAKE_BRIEF, "fake")
    for name in ("state", "worktrees"):
        assert (p.base / name).is_symlink() and os.readlink(p.base / name) == f"{name}.noindex"
        assert (p.base / f"{name}.noindex").is_dir() and not (p.base / f"{name}.noindex").is_symlink()
    assert p.state == p.base / "state.noindex" and p.runs == p.base / "state.noindex" / "runs"
    assert p.worktrees == p.base / "worktrees.noindex" and (p.state / "project.db").is_file()
    # A run folder named the old way (a worker started before the move) still passes the no-follow open.
    (p.runs / "7").mkdir(parents=True)
    assert checkdirs.writable(str(p.base / "state" / "runs" / "7"))
    assert checkdirs.writable(str(p.runs / "7"))
    from ttp import indexignore
    assert "/tt-project/state.noindex/" in (root / ".ignore").read_text()
    assert indexignore.FOLDERS[:2] == ("tt-project/state/", "tt-project/worktrees/")


def test_linux_projects_keep_plain_folders(env, tmp_path):
    from ttp import noindex
    p = make(env)
    assert not (p.base / "state").is_symlink() and p.state == p.base / "state"
    assert p.worktrees == p.base / "worktrees"
    assert noindex.migrate(p.base, platform="linux") == [] and not (p.base / "state.noindex").exists()
    (tmp_path / "fresh").mkdir()
    noindex.ensure(tmp_path / "fresh", platform="linux")
    assert not list((tmp_path / "fresh").iterdir())


def test_a_macos_upgrade_moves_the_folders_with_a_task_open_and_loses_nothing(env, monkeypatch):
    from ttp import cli, noindex, service
    from ttp.project import Project
    p = make(env)
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    t = p.db.add_task("Open work", "x", kind="code", tier="standard", origin="coordinator")
    run = p.runs / "41"
    run.mkdir(parents=True)
    (run / "log.jsonl").write_text("line\n")
    held = open(run / "log.jsonl", "a")       # a running worker's open log
    wt = p.worktrees / f"t{t}"
    _ni_git("-C", str(p.root), "worktree", "add", "-q", "-b", f"t{t}", str(wt))
    (wt / "work.txt").write_text("uncommitted\n")
    _install_template(env, {})
    _ni_darwin(monkeypatch)
    try:
        cli.main(["upgrade", "demo"])
    except SystemExit as e:
        assert e.code in (0, None), e.code
    held.write("after\n")
    held.close()
    q = Project(p.root)
    assert q.state == q.base / "state.noindex" and q.worktrees == q.base / "worktrees.noindex"
    assert (q.runs / "41" / "log.jsonl").read_text() == "line\nafter\n"
    assert (q.worktrees / f"t{t}" / "work.txt").read_text() == "uncommitted\n"
    assert q.db.task(t)["status"] == "queued"
    listed = [ln.split(" ", 1)[1] for ln in _ni_git("-C", str(p.root), "worktree", "list", "--porcelain").splitlines()
              if ln.startswith("worktree ")]
    assert str(q.worktrees / f"t{t}") in listed
    assert _ni_git("status", "--porcelain", cwd=q.base / "worktrees" / f"t{t}").strip() == "?? work.txt"
    # Again: nothing to move, nothing changed.
    assert noindex.migrate(q.base) == []


def test_a_folder_recreated_during_the_move_is_kept_aside_and_a_foreign_link_is_left(tmp_path):
    from ttp import noindex
    (tmp_path / "state.noindex").mkdir()
    (tmp_path / "state").mkdir()                     # both real: never merged
    (tmp_path / "state" / "a").write_text("1")
    out = noindex.migrate(tmp_path, platform="darwin")
    assert any("both" in ln for ln in out) and (tmp_path / "state" / "a").read_text() == "1"
    # A crash between the rename and the link: the next run links it, and Project finds the moved one.
    (tmp_path / "state" / "a").unlink()
    (tmp_path / "state").rmdir()
    assert noindex.real(tmp_path, "state") == tmp_path / "state.noindex"
    noindex.migrate(tmp_path, platform="darwin")
    assert os.readlink(tmp_path / "state") == "state.noindex"
    # Something wrote a real folder where the link goes: it is set aside whole.
    assert os.readlink(tmp_path / "worktrees") == "worktrees.noindex"
    (tmp_path / "worktrees").unlink()
    (tmp_path / "worktrees").mkdir()
    (tmp_path / "worktrees" / "b").write_text("2")
    out = []
    noindex._link(tmp_path, "worktrees", out)
    aside = [d for d in tmp_path.iterdir() if d.name.startswith("worktrees.stray-")]
    assert len(aside) == 1 and (aside[0] / "b").read_text() == "2" and out
    # A link the user made elsewhere stays.
    other = tmp_path / "elsewhere"
    other.mkdir()
    (tmp_path / "state").unlink()
    os.symlink(other, tmp_path / "state")
    out = noindex.migrate(tmp_path, platform="darwin")
    assert any("elsewhere" in ln for ln in out) and os.readlink(tmp_path / "state") == str(other)
    assert noindex.real(tmp_path, "state") == tmp_path / "state"


def test_the_web_token_guard_names_the_moved_path_too():
    from ttp import hook
    assert hook.WEB_TOKEN_RE.search("cat tt-project/state.noindex/" + "web.token")
    assert hook.WEB_TOKEN_RE.search("cat tt-project/state/" + "web.token")
