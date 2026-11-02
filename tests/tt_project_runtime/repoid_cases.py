# Repository identity cache tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new ttp/repoid.py tests here, not there.
from __future__ import annotations


def _repoid_rig(env, tmp_path, monkeypatch):
    """A repository with a commit, a subfolder, a linked worktree and an origin; the global config a
    temp file. Returns (repo, sub, wt, ask, fresh, runs): ask(path, *args) reads through repoid.read
    (runs counts its git calls), fresh(path, *args) asks git directly."""
    from ttp import repoid
    for var, val in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                     ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(var, val)
    glob = tmp_path / "global.gitconfig"
    glob.write_text((pathlib.Path(os.environ["GIT_CONFIG_GLOBAL"]).read_text()
                     if os.environ.get("GIT_CONFIG_GLOBAL") else ""))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "sub").mkdir()
    (repo / "sub" / "f").write_text("x\n")
    _git_out(repo, "add", ".")
    _git_out(repo, "commit", "-qm", "i")
    _git_out(repo, "remote", "add", "origin", "https://example.com/a/b.git")
    wt = tmp_path / "wt"
    _git_out(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    runs: list = []

    def fresh(path, *args):
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
        return (r.returncode, r.stdout)

    def ask(path, *args):
        def run():
            runs.append(args)
            return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
        r = repoid.read(path, args, run)
        return (r.returncode, r.stdout)
    repoid.clear()
    return repo, repo / "sub", wt, ask, fresh, runs


_REPOID_READS = [("rev-parse", "--show-toplevel"), ("rev-parse", "--git-common-dir"),
                 ("rev-parse", "--path-format=absolute", "--git-common-dir"),
                 ("rev-parse", "--git-dir", "--git-common-dir"), ("rev-parse", "--is-bare-repository"),
                 ("rev-parse", "--is-inside-work-tree")]


def test_repoid_answers_as_git_does_and_asks_once(env, tmp_path, monkeypatch):
    repo, sub, wt, ask, fresh, runs = _repoid_rig(env, tmp_path, monkeypatch)
    for path in (repo, sub, wt):
        for args in _REPOID_READS:
            want = fresh(path, *args)
            assert want[0] == 0, (path, args)
            runs.clear()
            assert ask(path, *args) == want and ask(path, *args) == want, (path, args)
            assert len(runs) == 1, (path, args, runs)


def test_repoid_asks_again_when_the_repository_changes(env, tmp_path, monkeypatch):
    repo, sub, wt, ask, fresh, runs = _repoid_rig(env, tmp_path, monkeypatch)

    def same(path, *args):
        runs.clear()
        got = ask(path, *args)
        assert got == fresh(path, *args), (path, args, got)
        return len(runs)
    for path in (repo, sub, wt):
        for args in _REPOID_READS:
            ask(path, *args)
    # any config write, the repository's or the global one, is a new stamp (seen from both checkouts)
    from ttp import repoid
    for edit in [("remote", "add", "upstream", "https://example.com/u/b.git"),
                 ("config", "core.bare", "false"),
                 ("config", "--global", "url.https://mirror.example.com/.insteadOf", "https://example.com/")]:
        before = [repoid.stamp(p) for p in (repo, wt)]
        _git_out(repo, *edit)
        for path, old in zip((repo, wt), before):
            assert repoid.stamp(path) not in (None, old), (edit, path)
            assert same(path, "rev-parse", "--show-toplevel") == 1, (edit, path)
    # a repository made inside: the subfolder's toplevel moves to it
    subprocess.run(["git", "init", "-q", str(sub)], check=True)
    for args in _REPOID_READS[:5]:
        assert same(sub, *args) == 1, args
    # the worktree removed and a repository of its own made at its path
    _git_out(repo, "worktree", "remove", "--force", str(wt))
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    for args in _REPOID_READS[:5]:
        assert same(wt, *args) == 1, args
    # a folder removed: git fails there, and so does the read, every time
    shutil.rmtree(sub)
    runs.clear()
    assert ask(sub, "rev-parse", "--show-toplevel")[0] != 0 and ask(sub, "rev-parse", "--show-toplevel")[0] != 0
    assert len(runs) == 2


def test_repoid_keeps_nothing_it_cannot_vouch_for(env, tmp_path, monkeypatch):
    from ttp import repoid
    repo, sub, wt, ask, fresh, runs = _repoid_rig(env, tmp_path, monkeypatch)
    top = ("rev-parse", "--show-toplevel")

    def runs_of(path, args, n=2):
        runs.clear()
        for _ in range(n):
            ask(path, *args)
        return len(runs)
    assert runs_of(repo, top) == 1
    # a failed call: asked again
    failed = []
    for _ in range(2):
        repoid.read(repo, top, lambda: failed.append(1) or subprocess.CompletedProcess(["git"], 128, "", "fatal"))
    assert len(failed) == 2
    # a helper's "" or None for a failure is not kept either
    calls = []
    for answer in ("", None):
        repoid.clear()
        calls.clear()
        for _ in range(2):
            repoid.read(repo, top, lambda a=answer: calls.append(1) or a)
        assert len(calls) == 2
    # a bare repository and a path inside a git directory: discovery not followed here
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    assert repoid.stamp(bare) is None and repoid.stamp(repo / ".git" / "refs") is None
    assert runs_of(bare, ("rev-parse", "--is-bare-repository")) == 2
    # GIT_DIR and the like: never kept
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    assert repoid.stamp(sub) is None and runs_of(sub, top) == 2
    monkeypatch.delenv("GIT_DIR")
    # a walk up into a GIT_CEILING_DIRECTORIES entry is not followed (git would not find the repo)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(repo))
    assert repoid.stamp(sub) is None and repoid.stamp(repo) is not None
    assert ask(sub, *top) == fresh(sub, *top)
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES")
    # another git setting in the environment is another key
    assert runs_of(repo, top) == 0 or runs_of(repo, top) == 0
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    assert runs_of(repo, top) == 1
    # an answer older than MAX_AGE_S is read again
    monkeypatch.setattr(repoid, "MAX_AGE_S", 0.0)
    assert runs_of(repo, top) == 2


def test_repoid_takes_identity_reads_only_and_the_git_helpers_use_it(env, tmp_path, monkeypatch):
    from ttp import gitcache, land, landed, push, repoid, worktree
    for args in _REPOID_READS:
        assert repoid.identity(args), args
    for args in [("rev-parse", "HEAD"), ("rev-parse", "--verify", "HEAD"), ("status", "--porcelain"),
                 ("symbolic-ref", "--quiet", "--short", "HEAD"), ("remote", "add", "x", "y"),
                 ("remote", "get-url"), ("remote", "get-url", "--push"), ("remote", "-v"),
                 ("rev-parse", "--show-toplevel", "HEAD"), ("worktree", "list", "--porcelain"),
                 ("remote", "set-url", "origin", "u"),
                 # gitcache.run owns the remote reads (it follows [include] and its racy window)
                 ("remote",), ("remote", "get-url", "origin"), ("remote", "get-url", "--push", "--all", "origin")]:
        assert not repoid.identity(args), args
    repo, sub, wt, ask, fresh, runs = _repoid_rig(env, tmp_path, monkeypatch)
    real = subprocess.run
    seen: list = []

    def counting(argv, *a, **k):
        seen.append(tuple(argv[3:]) if argv[:2] == ["git", "-C"] else tuple(argv))
        return real(argv, *a, **k)
    monkeypatch.setattr(subprocess, "run", counting)
    monkeypatch.setattr(gitcache, "RACY_S", -1.0)   # the rig's config was just written
    (tmp_path / "global.gitconfig").write_text("[gc]\n\tauto = 0\n")   # gitcache keeps nothing under [include]
    gitcache.clear()
    for _ in range(3):
        assert push._git(repo, "remote").stdout.split() == ["origin"]
        assert push._git(wt, "rev-parse", "--show-toplevel").stdout.strip() == str(wt.resolve())
        assert landed._git(repo, "remote", "get-url", "origin").stdout.strip() == "https://example.com/a/b.git"
        assert land._git(sub, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip() == \
            str((repo / ".git").resolve())
        assert worktree._git(wt, "rev-parse", "--git-common-dir") == str((repo / ".git").resolve())
        assert worktree._git(repo, "remote") == "origin"
        assert push._git(repo, "rev-parse", "HEAD").returncode == 0
    assert seen.count(("rev-parse", "HEAD")) == 3   # HEAD is read every time
    # each identity read once (repoid), each remote read once (gitcache, shared by every helper)
    for args in [("remote",), ("remote", "get-url", "origin"), ("rev-parse", "--show-toplevel"),
                 ("rev-parse", "--path-format=absolute", "--git-common-dir"), ("rev-parse", "--git-common-dir")]:
        assert seen.count(args) == 1, (args, seen)
    # helpers answering in another shape keep their own answers for the same read
    assert push._git(wt, "rev-parse", "--git-common-dir").stdout.strip() == str((repo / ".git").resolve())
    assert worktree._git(wt, "rev-parse", "--git-common-dir") == str((repo / ".git").resolve())
    # the push queue's view after a remote is added: read again
    _git_out(repo, "remote", "add", "fork", str(tmp_path / "fork.git"))
    assert push._git(repo, "remote").stdout.split() == ["fork", "origin"]
    # repoid keeps no remote read gitcache refuses: under an [include] every helper asks git each time
    cfg = repo / ".git" / "config"
    cfg.write_text(cfg.read_text() + "[include]\n\tpath = extra\n")
    seen.clear()
    for _ in range(2):
        push._git(repo, "remote")
        landed._git(repo, "remote", "get-url", "origin")
        worktree._git(repo, "remote")
    assert seen.count(("remote",)) == 4 and seen.count(("remote", "get-url", "origin")) == 2, seen
