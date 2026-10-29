# Memory-commit tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Each memory write or retirement is committed by the process that made
# it, with its own message, and never left on disk uncommitted for the harness sweep.
from __future__ import annotations


def _memory_commit_log(p, *paths):
    return subprocess.run(["git", "-C", str(p.harness), "log", "--format=%s", "--", *paths],
                          capture_output=True, text=True).stdout.splitlines()


def _committed_clean(p):
    assert _harness_dirty(p) == "", _harness_dirty(p)
    assert "sweep:" not in _daemon_log_text(p), _daemon_log_text(p)


def test_memory_writes_in_a_rolled_back_transaction_are_undone(env):
    """A transaction that rolls back after a retirement, an add or a supersede inside it leaves the
    entries as they were and the harness clean: nothing waits uncommitted for the sweep."""
    p = make(env)
    old = p.add_memory("An old decision.", kind="decision")
    other = p.add_memory("Another fact.", kind="fact")
    index = p.memory_index.read_text()
    for step in (lambda: p.forget_memory(old.stem, why="done"),
                 lambda: p.add_memory("A fact the rollback takes back.", kind="fact"),
                 lambda: p.supersede_memory("A merged fact.", [other.stem], kind="fact"),
                 lambda: (p.forget_memory(old.stem, why="done"), p.add_memory("Then a new fact.", kind="fact"))):
        with pytest.raises(RuntimeError):
            with p.db.tx():
                step()
                raise RuntimeError("a later write of the transaction failed")
        assert old.exists() and other.exists() and not list((p.memory_dir / "archive").glob("*"))
        assert p.memory_index.read_text() == index
        _committed_clean(p)
    # A line another process added meanwhile stays; only this transaction's change is undone.
    with pytest.raises(RuntimeError):
        with p.db.tx():
            p.forget_memory(old.stem, why="done")
            from ttp.project import durable_append
            durable_append(p.memory_index, "- [elsewhere](memory/fact-elsewhere.md) (fact)\n")
            raise RuntimeError("x")
    assert old.exists() and f"](memory/{old.name})" in p.memory_index.read_text()
    assert "fact-elsewhere.md" in p.memory_index.read_text()


def test_coordinator_memory_actions_in_the_run_end_transaction_commit_each_with_no_sweep(env):
    """The daemon applies a coordinator turn inside the run's end transaction (finish_run): the add
    and the retirement it supersedes each get their own commit once it commits, and the first
    commit's sweep never takes the retirement's files (os.replace keeps their old mtime)."""
    p = make(env)
    from ttp import coordinator as coord
    old = p.add_memory("An old decision.", kind="decision")
    with p.db.tx():
        probs = coord.apply(p, [{"type": "memory_add", "text": "A new decision.", "memory_kind": "decision",
                                 "supersedes": [old.stem], "why": "replaced"}], turn=7)
        assert probs == []
    _committed_clean(p)
    assert _memory_commit_log(p, f"memory/archive/{old.name}")[0].startswith(f"memory retired: {old.stem}")
    assert any(s.startswith("memory (decision): A new decision.") for s in _memory_commit_log(p))
    # A memory_forget alone, and an expiry retirement, the same way.
    later = p.add_memory("A later fact.", kind="fact")
    with p.db.tx():
        assert coord.apply(p, [{"type": "memory_forget", "name": later.stem}], turn=8) == []
    _committed_clean(p)
    assert (p.memory_dir / "archive" / later.name).exists()


def test_a_failing_after_commit_callback_never_drops_the_commits_queued_after_it(env):
    p = make(env)
    old = p.add_memory("An old decision.", kind="decision")
    with pytest.raises(OSError):
        with p.db.tx():
            p.db.after_commit(lambda: (_ for _ in ()).throw(OSError("an earlier side effect failed")))
            p.forget_memory(old.stem, why="done")
    _committed_clean(p)
    assert (p.memory_dir / "archive" / old.name).exists()


def test_memory_cli_add_forget_and_expiry_leave_the_harness_clean(env):
    p = make(env)
    from ttp import cli, ends
    old = p.add_memory("Old news.", kind="fact")
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["memory", "demo", "A new fact.", "--forget", old.stem])
    _committed_clean(p)
    assert _memory_commit_log(p, f"memory/archive/{old.name}")[0].startswith(f"memory retired: {old.stem}")
    temp = p.add_memory("A temporary fact.", kind="fact", end={"expires": time.time() + 60})
    assert ends.Ends(p).tick(time.time() + 120)
    _committed_clean(p)
    assert (p.memory_dir / "archive" / temp.name).exists()
    assert _memory_commit_log(p, f"memory/archive/{temp.name}")[0].startswith(f"memory retired: {temp.stem}")


@pytest.mark.parametrize("how", ["exit", "kill"])
def test_a_process_ending_inside_the_transaction_leaves_no_uncommitted_retirement(env, how):
    """sys.exit inside the transaction rolls it back and puts the entry back. A process killed there
    (os._exit: no undo runs) leaves the files moved; the replayed turn's retirement commits them."""
    p = make(env)
    old = p.add_memory("An old decision.", kind="decision")
    end = "sys.exit(3)" if how == "exit" else "os._exit(3)"
    # The child skips fsync as this process does (the env fixture): on a loaded host each one can stall
    # for tens of seconds, and what reaches the disk is not what this test checks.
    code = (f"import os, sys\nos.fsync = lambda fd: None\nfrom ttp.project import Project\n"
            f"p = Project({str(p.base)!r})\n"
            f"with p.db.tx():\n    p.forget_memory({old.stem!r}, why='done')\n    {end}\n")
    r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(RUNTIME)},
                       capture_output=True, text=True, timeout=HANG_S)
    assert r.returncode == 3, r.stderr
    if how == "exit":
        assert old.exists() and f"](memory/{old.name})" in p.memory_index.read_text()
        _committed_clean(p)
        return
    assert not old.exists() and old.name in _harness_dirty(p)
    p.forget_memory(old.stem, why="done")   # the replay finds it archived and commits what was left
    _committed_clean(p)
    assert _memory_commit_log(p, f"memory/archive/{old.name}")[0].startswith(f"memory retired: {old.stem}")
    # A replay of a retirement already committed makes no commit.
    n = len(_memory_commit_log(p))
    p.forget_memory(old.stem, why="done")
    assert len(_memory_commit_log(p)) == n


def test_a_replayed_coordinator_forget_runs_no_git_under_the_write_lock(env, monkeypatch):
    """A coordinator memory_forget of an entry already archived (a replayed turn) inside the run-end
    transaction checks whether a killed run left it uncommitted only once the transaction ends:
    git never runs while the write lock is held. It commits what was left, and a replay of a
    retirement already committed makes no commit."""
    p = make(env)
    from ttp import coordinator as coord
    old = p.add_memory("An old decision.", kind="decision")
    code = (f"import os\nfrom ttp.project import Project\np = Project({str(p.base)!r})\n"
            f"with p.db.tx():\n    p.forget_memory({old.stem!r}, why='done')\n    os._exit(3)\n")
    r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(RUNTIME)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 3, r.stderr
    assert not old.exists() and old.name in _harness_dirty(p)
    real, locked = subprocess.run, []

    def spy(cmd, *a, **kw):
        if cmd and cmd[0] == "git" and p.db.conn.in_transaction:
            locked.append(cmd)
        return real(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "run", spy)
    for _ in range(2):   # the replay that commits the leftovers, then one with nothing left
        n = len(_memory_commit_log(p))
        with p.db.tx():
            assert coord.apply(p, [{"type": "memory_forget", "name": old.stem}], turn=9) == []
        assert locked == []
        monkeypatch.setattr(subprocess, "run", real)
        _committed_clean(p)
        monkeypatch.setattr(subprocess, "run", spy)
    assert len(_memory_commit_log(p)) == n
    assert _memory_commit_log(p, f"memory/archive/{old.name}")[0].startswith(f"memory retired: {old.stem}")
    # Retiring a live entry and replaying an archived one in one transaction: no git under the lock either.
    fresh = p.add_memory("A fresh fact.", kind="fact")
    with p.db.tx():
        assert coord.apply(p, [{"type": "memory_forget", "name": old.stem},
                               {"type": "memory_forget", "name": fresh.stem}], turn=10) == []
    assert locked == []
    monkeypatch.setattr(subprocess, "run", real)
    _committed_clean(p)
    assert (p.memory_dir / "archive" / fresh.name).exists()


def test_an_undo_that_raises_on_rollback_is_logged_and_the_next_still_runs(env):
    p = make(env)
    ran = []

    def bad():
        raise OSError("disk gone")
    with pytest.raises(RuntimeError):
        with p.db.tx():
            p.db.on_rollback(lambda: ran.append(1))
            p.db.on_rollback(bad)
            raise RuntimeError("x")
    assert ran == [1]
    assert "rollback undo" in _daemon_log_text(p) and "disk gone" in _daemon_log_text(p)


def test_memory_cli_dry_run_writes_retires_and_commits_nothing(env):
    """--dry-run on an add, a --supersedes merge and a --forget only says what it would add and retire,
    marked as a dry run: the memory files, index and harness HEAD stay as they were."""
    from ttp import cli
    p = make(env)
    old = p.add_memory("An old decision.", kind="decision")
    other = p.add_memory("Another fact.", kind="fact")
    head = lambda: subprocess.run(["git", "-C", str(p.harness), "rev-parse", "HEAD"], capture_output=True,
                                  text=True).stdout
    snapshot = lambda: sorted((str(f.relative_to(p.memory_dir)), f.read_text())
                              for f in p.memory_dir.rglob("*") if f.is_file())
    before, index, h0 = snapshot(), p.memory_index.read_text(), head()
    for argv, said in ((["A merged fact.", "--title", "x", "--supersedes", other.stem],
                       ["would add", "fact-x.md", f"would retire to {p.memory_dir / 'archive' / other.name}"]),
                      (["--forget", old.stem, other.stem],
                       [f"archive/{old.name}", f"archive/{other.name}"]),
                      (["A new fact."], ["would add", "fact-a-new-fact.md"])):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["memory", "demo", *argv, "--dry-run"])
        lines = out.getvalue().splitlines()
        assert lines and all(x.startswith("dry run, nothing written or committed: ") for x in lines), lines
        assert all(s in out.getvalue() for s in said), out.getvalue()
        # An add or merge still shows the pinned worker-memory total (as it stands); a --forget alone does not.
        assert ("pinned worker memory: " in out.getvalue()) == ("--forget" not in argv), out.getvalue()
        assert snapshot() == before and p.memory_index.read_text() == index, f"{argv} wrote"
        assert head() == h0 and _harness_dirty(p) == "", f"{argv} committed or left changes"
    # A refused name is still refused in a dry run.
    with pytest.raises(SystemExit):
        cli.main(["memory", "demo", "A merged fact.", "--supersedes", "no-such-entry", "--dry-run"])
    assert snapshot() == before and head() == h0
    # Over its room, a dry-run merge still warns, marked as a dry run, and still writes nothing.
    big = [p.add_memory(f"RULE-{i:02} " + "r" * 900, kind="restriction") for i in range(30)]
    before, index, h0 = snapshot(), p.memory_index.read_text(), head()
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
        cli.main(["memory", "demo", "RULE-00 and RULE-01 merged.", "--kind", "restriction",
                  "--supersedes", big[0].stem, big[1].stem, "--dry-run"])
    assert all(x.startswith("dry run, nothing written or committed: ") for x in out.getvalue().splitlines())
    assert err.getvalue().startswith("dry run, nothing written or committed: warning: pinned worker memory "
                                     "over budget: "), err.getvalue()
    assert snapshot() == before and p.memory_index.read_text() == index and head() == h0
    assert _harness_dirty(p) == ""


def _memory_end(path):
    from ttp.ends import read_front_matter
    return read_front_matter(path.read_text().split("\n---\n", 1)[0])


def _live_memory(p):
    return sorted(f.stem for f in p.memory_dir.glob("*.md"))


def test_memory_supersedes_keeps_the_earliest_end_unless_a_new_end_or_permanent(env):
    """A merge never turns a temporary entry permanent: with no end of its own the new entry keeps the
    earliest expires of the entries it replaces with that entry's until and probe (else the first
    until), a new end wins, and --permanent drops it."""
    from ttp import cli
    p = make(env)
    now = time.time()
    soon = {"expires": now + 86400, "probe": "test -e /nonexistent/done"}

    def three():
        return [p.add_memory("A later fact.", kind="fact", end={"expires": now + 3 * 86400, "until": "it ships"}),
                p.add_memory("A sooner fact.", kind="fact", end=soon),
                p.add_memory("A permanent fact.", kind="fact")]
    olds = three()
    with contextlib.redirect_stdout(io.StringIO()) as out:
        cli.main(["memory", "demo", "The merged fact.", "--supersedes", *(o.stem for o in olds)])
    added = pathlib.Path(out.getvalue().splitlines()[0])
    end = _memory_end(added)
    assert end["probe"] == soon["probe"] and "until" not in end and abs(end["expires"] - soon["expires"]) < 61, end
    assert not any(o.exists() for o in olds)
    # Only an until among them: kept.
    olds = [p.add_memory("Until-only fact.", kind="fact", end={"until": "the box is back"}),
            p.add_memory("No-end fact.", kind="fact")]
    added, _ = p.supersede_memory("Merged until fact.", [o.stem for o in olds])
    assert _memory_end(added) == {"until": "the box is back"}
    # A new end wins whole.
    olds = three()
    added, _ = p.supersede_memory("Merged with its own end.", [o.stem for o in olds], end={"until": "a new end"})
    assert _memory_end(added) == {"until": "a new end"}
    # --permanent drops it.
    olds = three()
    with contextlib.redirect_stdout(io.StringIO()) as out:
        cli.main(["memory", "demo", "Merged for good.", "--supersedes", *(o.stem for o in olds), "--permanent"])
    assert _memory_end(pathlib.Path(out.getvalue().splitlines()[0])) == {}
    with pytest.raises(ValueError, match="contradict"):
        p.supersede_memory("Both.", [p.add_memory("X fact.", kind="fact").stem], end={"until": "x"}, permanent=True)
    with pytest.raises(SystemExit, match="--permanent goes with --supersedes"):
        cli.main(["memory", "demo", "A fact.", "--permanent"])
    _committed_clean(p)


def test_memory_supersedes_audience_default_clears_a_workers_audience_in_one_write(env):
    """--audience default stores no audience (default ranked selection) in the same commit as the
    merge, leaving one live entry; without it the shared audience is kept."""
    from ttp import cli
    p = make(env)
    old = p.add_memory("A fact for workers.", kind="fact", audience="workers")
    count = lambda: len(_memory_commit_log(p))
    before = count()
    with contextlib.redirect_stdout(io.StringIO()) as out:
        cli.main(["memory", "demo", "The fact, unpinned.", "--supersedes", old.stem, "--audience", "default"])
    added = pathlib.Path(out.getvalue().splitlines()[0])
    assert "audience:" not in added.read_text()
    assert count() == before + 1 and _live_memory(p) == [added.stem]
    _committed_clean(p)
    # No --audience: the shared one stays.
    old = p.add_memory("Another fact for workers.", kind="fact", audience="workers")
    added, _ = p.supersede_memory("Still for workers.", [old.stem])
    assert "audience: workers\n" in added.read_text()


def test_coordinator_memory_add_supersedes_carries_the_earliest_end_and_takes_the_default_audience(env):
    """The coordinator's memory_add with `supersedes` goes its own way (add, then retire) and behaves
    as `ttp memory --supersedes`: the earliest end carried over, its own end or `permanent` wins, and
    audience `default` stores none."""
    p = make(env)
    from ttp import coordinator as coord
    now = time.time()
    for turn, extra, want in ((1, {}, "carried"), (2, {"until": "a new end"}, {"until": "a new end"}),
                              (3, {"permanent": True}, {}), (4, {"audience": "default"}, "carried")):
        old = p.add_memory(f"Temporary fact {turn}.", kind="fact", audience="workers",
                           end={"expires": now + 86400, "until": "it ships"})
        later = p.add_memory(f"Later fact {turn}.", kind="fact", end={"expires": now + 5 * 86400})
        with p.db.tx():
            assert coord.apply(p, [{"type": "memory_add", "text": f"Merged fact {turn}.", "memory_kind": "fact",
                                    "supersedes": [old.stem, later.stem], **extra}], turn=turn) == []
        added = p.memory_dir / f"fact-merged-fact-{turn}.md"
        end = _memory_end(added)
        if want == "carried":
            assert end["until"] == "it ships" and abs(end["expires"] - now - 86400) < 61, end
        else:
            assert end == want, (extra, end)
        assert not old.exists() and not later.exists()
        assert "audience:" not in added.read_text()
    with p.db.tx():
        probs = coord.apply(p, [{"type": "memory_add", "text": "Contradiction.", "memory_kind": "fact",
                                 "supersedes": [added.stem], "until": "x", "permanent": True}], turn=9)
    assert probs and "contradict" in str(probs), probs
    assert added.exists()
    _committed_clean(p)
