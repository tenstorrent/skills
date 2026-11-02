from __future__ import annotations


def _git_spy(monkeypatch):
    """Count the git processes started through subprocess.run, by their verb."""
    import collections
    seen = collections.Counter()
    real = subprocess.run

    def run(args, *a, **k):
        if isinstance(args, (list, tuple)) and args and args[0] == "git":
            rest = [x for x in args[1:] if not str(x).startswith("-C")]
            rest = rest[1:] if args[1:2] == ["-C"] else rest
            seen[" ".join(map(str, rest[:2]))] += 1
        return real(args, *a, **k)
    monkeypatch.setattr(subprocess, "run", run)
    return seen


def _line(tmp_path, n):
    repo = tmp_path / "line"
    repo.mkdir()
    _git_out(repo, "init", "-q", "-b", "main")
    return repo, [_commit_file_in(repo, f"c{i}.txt") for i in range(n)]


def test_landed_ancestry_of_several_shas_against_one_tip_costs_one_git_call_then_none(env, tmp_path, monkeypatch):
    from ttp import gitcache, landed
    repo, shas = _line(tmp_path, 5)
    tip = shas[-1]
    _git_out(repo, "checkout", "-q", "-b", "side", shas[1])
    side = _commit_file_in(repo, "side.txt")
    _git_out(repo, "checkout", "-q", "main")
    for s in (*shas, side):
        assert landed._commit(repo, s) == s
    seen = _git_spy(monkeypatch)
    assert gitcache.ancestors(repo, [*shas, side], tip) == {**{s: True for s in shas}, side: False}
    assert sum(seen.values()) == 1 and seen["rev-list --max-count=100000"] == 1
    seen.clear()
    # The same shas again, as landed's own is-ancestor reads and whole on_branch answers: no git at all.
    assert [landed._git(repo, "merge-base", "--is-ancestor", s, tip).returncode for s in (*shas, side)] == [0] * 5 + [1]
    assert landed.on_branch(repo, shas[2], tip) and landed.on_branch(repo, shas[2], tip)
    assert sum(seen.values()) == 0, seen
    # The tip moves: a fresh answer, read with one rev-list from the old tip's set.
    _git_out(repo, "merge", "-q", "--no-edit", "--no-ff", "side")
    new = _git_out(repo, "rev-parse", "HEAD")
    seen.clear()
    assert gitcache.ancestors(repo, [side, new, shas[0]], new) == {side: True, new: True, shas[0]: True}
    assert sum(seen.values()) == 1 and seen["rev-list --boundary"] == 1, seen
    assert gitcache.ancestors(repo, [side], tip) == {side: False}   # the old tip's answer is unchanged


def test_landed_ancestry_of_an_abbreviated_or_missing_commit_still_asks_git(env, tmp_path, monkeypatch):
    from ttp import landed
    repo, shas = _line(tmp_path, 2)
    seen = _git_spy(monkeypatch)
    missing = "0" * 40
    assert landed._git(repo, "merge-base", "--is-ancestor", missing, shas[1]).returncode not in (0, 1)
    assert landed._git(repo, "merge-base", "--is-ancestor", shas[0][:10], shas[1]).returncode == 0
    assert seen["merge-base --is-ancestor"] == 2


def test_git_remote_reads_are_kept_until_the_repository_config_changes(env, tmp_path, monkeypatch):
    from ttp import gitcache, push
    monkeypatch.setattr(gitcache, "RACY_S", -1.0)
    (tmp_path / "global").write_text("[gc]\n\tauto = 0\n")    # the tests' own global config includes others
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global"))
    repo, _ = _line(tmp_path, 1)
    _git_out(repo, "remote", "add", "origin", str(tmp_path / "a.git"))
    seen = _git_spy(monkeypatch)
    for _ in range(3):
        assert push._git(repo, "remote").stdout.split() == ["origin"]
        assert push._git(repo, "remote", "get-url", "origin").stdout.strip() == str(tmp_path / "a.git")
    assert seen["remote"] == 1 and seen["remote get-url"] == 1
    _git_out(repo, "remote", "add", "other", str(tmp_path / "b.git"))
    _git_out(repo, "remote", "set-url", "origin", str(tmp_path / "c.git"))
    assert push._git(repo, "remote").stdout.split() == ["origin", "other"]
    assert push._git(repo, "remote", "get-url", "origin").stdout.strip() == str(tmp_path / "c.git")
    assert seen["remote"] == 2 and seen["remote get-url"] == 2
    # A config that includes another file is never kept: the included file's changes would not show.
    (repo / ".git" / "config").write_text((repo / ".git" / "config").read_text() + "[include]\n\tpath = extra\n")
    push._git(repo, "remote")
    push._git(repo, "remote")
    assert seen["remote"] == 4


def test_an_idle_daemon_tick_reads_unchanged_settings_files_once(env, monkeypatch):
    import collections
    from ttp import daemon as dmod, machines, project as projmod, statcache
    monkeypatch.setattr(statcache, "RACY_S", -1.0)
    p = make(env)
    projmod.HOME_DIR.mkdir(parents=True, exist_ok=True)
    projmod.write_json(projmod.account_settings_path(), {"budget": {"global_daily_usd": 50}})
    projmod.write_json(machines.path(), {"machines": {"box": {"tags": ["device"]}}})
    d = dmod.Daemon(p.base)
    d.tick()
    reads = collections.Counter()
    real = pathlib.Path.read_text

    def read_text(self, *a, **k):
        reads[self.name] += 1
        return real(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    for _ in range(3):
        d._last_cfg = time.time()
        d.tick()
    assert not {n: reads[n] for n in ("project.json", "settings.json", "machines.json") if reads[n]}, reads
    # A change is read at once, and a file that stops parsing reads as before: the default, no crash.
    projmod.write_json(machines.path(), {"machines": {"box": {"tags": []}, "two": {"tags": []}}})
    assert sorted(machines.load()) == ["box", "two"]
    machines.path().write_text("{not json")
    assert machines.load() == {}
    projmod.account_settings_path().write_text("[1, 2")
    assert projmod.load_account_settings() == {}


def test_a_json_file_read_from_memory_is_a_private_copy(env, tmp_path, monkeypatch):
    from ttp import statcache
    monkeypatch.setattr(statcache, "RACY_S", -1.0)
    f = tmp_path / "a.json"
    f.write_text('{"a": {"b": [1]}}')
    first = statcache.load_json(f)
    first["a"]["b"].append(2)
    assert statcache.load_json(f) == {"a": {"b": [1]}}
