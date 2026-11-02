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


def _ancestry_line(tmp_path, n):
    repo = tmp_path / "line"
    repo.mkdir()
    _git_out(repo, "init", "-q", "-b", "main")
    return repo, [_commit_file_in(repo, f"c{i}.txt") for i in range(n)]


def test_landed_ancestry_of_several_shas_against_one_tip_costs_one_git_call_then_none(env, tmp_path, monkeypatch):
    from ttp import gitcache, landed
    repo, shas = _ancestry_line(tmp_path, 5)
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
    repo, shas = _ancestry_line(tmp_path, 2)
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
    repo, _ = _ancestry_line(tmp_path, 1)
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


def test_gitcache_git_takes_the_scope_cap_and_a_failed_system_config_lookup_is_not_kept(env, tmp_path, monkeypatch):
    from ttp import gitcache, gitmemo
    monkeypatch.delenv("GIT_CONFIG_SYSTEM", raising=False)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    monkeypatch.setattr(gitcache, "_system", [])
    repo, shas = _ancestry_line(tmp_path, 2)
    real, timeouts, hang = subprocess.run, [], [True]

    def run(args, *a, **k):
        if isinstance(args, (list, tuple)) and args[:1] == ["git"]:
            timeouts.append((args[-1], k.get("timeout")))
            if hang[0] and "var" in args:
                raise subprocess.TimeoutExpired(args, k.get("timeout"))
        return real(args, *a, **k)
    monkeypatch.setattr(subprocess, "run", run)
    with gitmemo.bounded(0.5):
        assert gitcache._system_config() is None          # a hung lookup is not kept as "no system config"
        assert gitcache._config_stamp(gitcache._dirs(repo)) is None
        gitcache.ancestors(repo, shas, shas[-1])
    assert timeouts and all(t is not None and t <= 0.5 for _, t in timeouts), timeouts
    assert gitcache._system == []
    hang[0] = False
    assert gitcache._system_config() and gitcache._system      # asked again, and kept once git answers


def _it_picked(tmp_path):
    """A repo whose main has a cherry-pick of side's commit, plus one more: on_branch(side, main) is
    True only through git cherry. Returns (repo, side commit, main tip, an unpicked side commit)."""
    repo, shas = _ancestry_line(tmp_path, 2)
    _git_out(repo, "checkout", "-q", "-b", "side", shas[0])
    side = _commit_file_in(repo, "s.txt", "s")
    _git_out(repo, "checkout", "-q", "-b", "other", shas[0])
    other = _commit_file_in(repo, "o.txt", "o")
    _git_out(repo, "checkout", "-q", "main")
    _git_out(repo, "cherry-pick", side)
    tip = _commit_file_in(repo, "m.txt", "m")
    return repo, side, tip, other


def _it_failing(monkeypatch, landed, fails):
    """landed._git with the calls `fails(args)` picks exiting 128, as a read error or signal would."""
    real = landed._git

    def fake(repo, *args, **k):
        if fails(args):
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: read error")
        return real(repo, *args, **k)
    monkeypatch.setattr(landed, "_git", fake)
    return real


def test_on_branch_a_failed_git_cherry_answers_false_and_is_not_kept(env, tmp_path, monkeypatch):
    from ttp import landed
    repo, side, tip, other = _it_picked(tmp_path)
    real = _it_failing(monkeypatch, landed, lambda a: a[0] == "cherry")
    assert landed.on_branch(repo, other, tip) is False     # was True: an empty cherry read as "all on tip"
    assert landed.on_branch(repo, side, tip) is False      # cannot tell
    monkeypatch.setattr(landed, "_git", real)
    assert landed.on_branch(repo, side, tip) is True       # asked again once git answers
    assert landed.on_branch(repo, other, tip) is False


def test_on_branch_a_failed_merge_base_or_log_is_asked_again_on_the_next_call(env, tmp_path, monkeypatch):
    from ttp import gitcache, landed
    repo, side, _, _ = _it_picked(tmp_path)
    # A side commit on main only through a replay trailer: its answer needs every log read.
    _git_out(repo, "checkout", "-q", "side")
    rep = _commit_file_in(repo, "r.txt", "r")
    _git_out(repo, "checkout", "-q", "main")
    (repo / "r2.txt").write_text("other")
    _git_out(repo, "add", "r2.txt")
    _git_out(repo, "commit", "-q", "-m", f"replay\n\n{landed.REPLAYED}: {rep}")
    tip = _git_out(repo, "rev-parse", "HEAD")
    for sha, fails in ((side, lambda a: a[0] == "merge-base" and a[1] != "--is-ancestor"),
                       (side, lambda a: a[:2] == ("merge-base", "--is-ancestor")),
                       (side, lambda a: a[:2] == ("log", "-1")),
                       (rep, lambda a: a[:2] == ("log", "--no-merges")),
                       (rep, lambda a: a[:2] == ("log", "--format=%B"))):
        gitcache.clear()                      # the last round's (good) answer is kept
        real = _it_failing(monkeypatch, landed, fails)
        assert landed.on_branch(repo, sha, tip) is False
        monkeypatch.setattr(landed, "_git", real)
        assert landed.on_branch(repo, sha, tip) is True


def test_gitcache_keeps_nothing_where_history_can_change_under_the_same_ids(env, tmp_path, monkeypatch):
    from ttp import gitcache, landed
    repo, shas = _ancestry_line(tmp_path, 3)
    _git_out(repo, "checkout", "-q", "-b", "side", shas[0])
    side = _commit_file_in(repo, "side.txt")
    _git_out(repo, "checkout", "-q", "main")
    shallow = tmp_path / "shallow"
    _git_out(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow))
    grafts = tmp_path / "grafts"
    _git_out(tmp_path, "clone", "-q", str(repo), str(grafts))
    (grafts / ".git" / "info").mkdir(exist_ok=True)
    (grafts / ".git" / "info" / "grafts").write_text(f"{shas[1]}\n")

    def uncached(r, sha, tip):
        assert gitcache.is_ancestor(r, sha, tip) is None
        n = []
        assert gitcache.fixed(r, "q", sha, tip, lambda: n.append(1) or True) is True
        assert gitcache.fixed(r, "q", sha, tip, lambda: n.append(1) or True) is True
        assert len(n) == 2
        seen = _git_spy(monkeypatch)
        for _ in range(2):
            landed._git(r, "rev-parse", "--verify", "--quiet", f"{tip}^{{commit}}")
            landed._git(r, "merge-base", "--is-ancestor", sha, tip)
        assert seen["rev-parse --verify"] == 2 and seen["merge-base --is-ancestor"] == 2, seen
    uncached(shallow, shas[-1], shas[-1])
    uncached(grafts, shas[0], shas[-1])
    # Answers kept before a replace ref appears are not served after it, loose or packed.
    seen = []
    assert gitcache.is_ancestor(repo, shas[0], shas[-1]) is True
    assert gitcache.fixed(repo, "q", shas[0], shas[-1], lambda: seen.append(1) or True) is True
    _git_out(repo, "replace", shas[1], side)
    uncached(repo, shas[0], shas[-1])
    _git_out(repo, "pack-refs", "--all")
    assert not any((repo / ".git" / "refs" / "replace").iterdir())
    assert "refs/replace/" in (repo / ".git" / "packed-refs").read_text()
    uncached(repo, shas[0], shas[-1])


def test_gitcache_a_negative_ancestry_answer_is_asked_again_once_the_packs_change(env, tmp_path, monkeypatch):
    from ttp import gitcache, landed
    repo, shas = _ancestry_line(tmp_path, 3)
    _git_out(repo, "checkout", "-q", "-b", "side", shas[0])
    side = _commit_file_in(repo, "side.txt")
    _git_out(repo, "checkout", "-q", "main")
    tip = shas[-1]
    assert landed._commit(repo, side) == side
    seen = _git_spy(monkeypatch)
    n = []
    for _ in range(2):
        assert landed._git(repo, "merge-base", "--is-ancestor", side, tip).returncode == 1
        assert gitcache.fixed(repo, "q", side, tip, lambda: n.append(1) or False) is False
    assert seen["merge-base --is-ancestor"] == 0 and len(n) == 1, seen
    _git_out(repo, "repack", "-q", "-a", "-d")
    assert landed._git(repo, "merge-base", "--is-ancestor", side, tip).returncode == 1
    assert gitcache.fixed(repo, "q", side, tip, lambda: n.append(1) or False) is False
    assert seen["merge-base --is-ancestor"] == 1 and len(n) == 2, seen


def test_gitcache_a_rewound_or_force_moved_tip_reads_its_ancestors_in_full(env, tmp_path, monkeypatch):
    from ttp import gitcache, landed
    repo, shas = _ancestry_line(tmp_path, 4)
    _git_out(repo, "checkout", "-q", "--orphan", "lone")
    lone = [_commit_file_in(repo, f"l{i}.txt") for i in range(2)]
    every = [*shas, *lone]
    for s in every:                           # a "no" is given only for a commit rev-parse found
        assert landed._commit(repo, s) == s

    def truth(sha, tip):
        return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, tip]).returncode == 0
    assert gitcache.ancestors(repo, every, shas[-1]) == {s: truth(s, shas[-1]) for s in every}
    for tip in (shas[1], lone[-1]):           # rewound to an ancestor, then moved to unrelated history
        seen = _git_spy(monkeypatch)
        assert gitcache.ancestors(repo, every, tip) == {s: truth(s, tip) for s in every}
        assert seen["rev-list --boundary"] == 1 and seen["rev-list --max-count=100000"] == 1, seen


def test_files_and_git_config_changed_within_the_racy_window_are_read_every_time(env, tmp_path, monkeypatch):
    import types
    from ttp import gitcache, push, statcache
    assert statcache.RACY_S == gitcache.RACY_S == 2.0
    f = tmp_path / "a.json"
    f.write_text('{"a": 1}')
    reads = []
    real_read = pathlib.Path.read_text
    monkeypatch.setattr(pathlib.Path, "read_text", lambda self, *a, **k: reads.append(self.name) or real_read(self, *a, **k))
    for _ in range(3):
        assert statcache.load_json(f) == {"a": 1}
    assert reads.count("a.json") == 3
    monkeypatch.setattr(pathlib.Path, "read_text", real_read)
    (tmp_path / "global").write_text("[gc]\n\tauto = 0\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global"))
    repo, _ = _ancestry_line(tmp_path, 1)
    _git_out(repo, "remote", "add", "origin", str(tmp_path / "a.git"))
    seen = _git_spy(monkeypatch)
    for _ in range(3):
        assert push._git(repo, "remote").stdout.split() == ["origin"]
        assert push._git(repo, "remote", "get-url", "origin").stdout.strip() == str(tmp_path / "a.git")
    assert seen["remote"] == 3 and seen["remote get-url"] == 3, seen
    # Once the window has passed, both are kept.
    later = time.time() + 10
    monkeypatch.setattr(gitcache, "time", types.SimpleNamespace(time_ns=lambda: int(later * 1e9)))
    monkeypatch.setattr(statcache, "time", types.SimpleNamespace(time=lambda: later))
    for _ in range(3):
        push._git(repo, "remote")
        statcache.load_json(f)
    assert seen["remote"] == 4, seen
    assert statcache._kept


def test_gitcache_and_statcache_never_keep_more_than_their_bounds(env, tmp_path, monkeypatch):
    from ttp import gitcache, landed, statcache
    monkeypatch.setattr(gitcache, "MAX_TIPS", 2)
    monkeypatch.setattr(gitcache, "MAX_ENTRIES", 3)
    monkeypatch.setattr(statcache, "MAX_FILES", 2)
    monkeypatch.setattr(statcache, "RACY_S", -1.0)
    repo, shas = _ancestry_line(tmp_path, 6)
    for tip in shas:
        for s in shas:
            landed._commit(repo, s)
            gitcache.is_ancestor(repo, s, tip)
            gitcache.fixed(repo, "q", s, tip, lambda: True)
            assert len(gitcache._sets) <= 2 and len(gitcache._pairs) <= 3 and len(gitcache._answers) <= 3
    for i in range(5):
        (tmp_path / f"{i}.json").write_text(str(i))
        assert statcache.load_json(tmp_path / f"{i}.json") == i
        assert len(statcache._kept) <= 2


def _billing_db(tmp_path):
    from ttp.db import DB
    d = DB(tmp_path / "b.db")
    now = time.time()
    d.conn.executemany("INSERT INTO runs(task,role,provider,account,started,ended,status,cost_usd,dir) VALUES(?,?,?,?,?,?,?,?,?)",
                       [(1, "worker", "claude", "a" if i % 3 else "", now - 86400 * 20 + i * 1800, now - 86400 * 20 + i * 1800 + 600,
                         "ok", 1.0, "") for i in range(900)])
    d.conn.executemany("INSERT INTO snapshots(ts,provider,account,window) VALUES(?,?,?,?)",
                       [(now - 86400 * 9 + i * 600, "claude", "a", "5h") for i in range(0, 1300) if not 400 < i < 700])
    for i in range(300):
        d.spend("claude", 0.5, "worker", account="a" if i % 2 else "", ts=now - 86400 * 8 + i * 2400)
    return d, now


def test_billing_memo_matches_the_uncached_reads_and_ends_at_any_write(tmp_path):
    """The plan readings and runs billed_by_account reads are reused within a tick (DB.billing_memo)
    only while nothing wrote the database: totals always equal the uncached path's."""
    import sqlite3 as sq
    from ttp import billing
    d, now = _billing_db(tmp_path)

    def both(since):
        cached = billing.billed_by_account(d.conn, since, running_at=now, memo=d.billing_memo)
        assert cached == billing.billed_by_account(d.conn, since, running_at=now)
        return cached

    for since in (now - 3600, now - 86400, now - 7 * 86400, now - 3 * 86400):
        both(since)
    assert len(d.billing_memo) == 2   # one entry per query, sliced for every later window
    before = both(now - 7 * 86400)
    other = sq.connect(str(tmp_path / "b.db"))   # another connection's commit: PRAGMA data_version
    other.execute("DELETE FROM snapshots WHERE ts > ?", (now - 86400 * 5,))
    other.commit()
    other.close()
    after = both(now - 7 * 86400)
    assert after != before
    # this connection's own write: total_changes
    d.conn.executemany("INSERT INTO snapshots(ts,provider,account,window) VALUES(?,?,?,?)",
                       [(now - 86400 * 5 + i * 600, "claude", "a", "5h") for i in range(700)])
    assert both(now - 7 * 86400) != after


def test_idle_reads_use_their_indexes(tmp_path):
    """The reads an idle tick makes on a long history use an index, never a scan of every row."""
    from ttp.db import DB, OPEN_TASKS
    d = DB(tmp_path / "i.db")

    def plan(sql, args=()):
        return " ".join(r[-1] for r in d.conn.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall())

    assert "tasks_open" in plan(f"SELECT * FROM tasks WHERE {OPEN_TASKS} ORDER BY id")
    assert "SCAN" not in plan("SELECT MAX(updated) m FROM tasks WHERE status IN (?,?,?)", ("done", "failed", "cancelled"))
    assert "SCAN" not in plan("SELECT MAX(ts) m FROM messages WHERE direction='in'")
    assert "SCAN" not in plan("SELECT * FROM messages WHERE kind='ask' AND handled=0")
    assert "runs_ended" in plan("SELECT provider FROM runs WHERE ended>=? ORDER BY ended", (0,))


def test_unmet_dependencies_reads_only_the_tasks_depended_on(tmp_path):
    """Done dependencies are met, open, failed and missing ones are not, the same as reading all tasks."""
    from ttp.db import DB
    d = DB(tmp_path / "u.db")
    ids = {}
    for name, status in (("done", "done"), ("open", "queued"), ("failed", "failed")):
        ids[name] = d.add_task(title=name, spec="s", kind="work")
        d.x("UPDATE tasks SET status=? WHERE id=?", (status, ids[name]))
    t = d.add_task(title="t", spec="s", kind="work", depends_on=[ids["done"], ids["open"], ids["failed"], 9999, "x"])
    got = d.unmet_dependencies([d.task(t), d.task(ids["done"])])
    assert got == {t: [ids["open"], ids["failed"], 9999, None], ids["done"]: []}
