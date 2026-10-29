# Push tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new push tests here, not there.
from __future__ import annotations


def test_merge3_near_refuses_additions_that_nearly_repeat_each_other():
    """With `near`, lines both sides added at one spot, or in different lines of one section (hunks),
    are not both kept when one nearly repeats the other: the same leading key, or most of the text
    alike. Additions that only look alike in shape (other keys, other words, new defs) still settle."""
    from ttp.batch import merge3
    base = "{\n  \"a\": 1,\n}\n"
    add = lambda x: base.replace("1,\n", "1,\n" + x)
    key_o, key_t = add('  "deep": "the analysis problem",\n'), add('  "deep": "a problem",\n')
    assert merge3(key_o, base, key_t) is not None                       # without near: kept both
    assert merge3(key_o, base, key_t, near=True) is None                # same key
    assert merge3(add("timeout = 5\n"), base, add("timeout = 10\n"), near=True) is None
    alike_o, alike_t = add("Never run the suite twice per fix.\n"), add("Never run the suite twice for a fix.\n")
    assert merge3(alike_o, base, alike_t, near=True) is None             # most of the text alike
    kinds: set = set()
    both = merge3(add('  "deep": "x",\n'), base, add('  "pr": "y",\n'), near=True, kinds=kinds)
    assert both == add('  "deep": "x",\n  "pr": "y",\n') and kinds == {"added"}
    assert merge3(add("Check the device first.\n"), base, add("Write the hand-off last.\n"), near=True)
    assert merge3("def test_one(env):\n    pass\n", "", "def test_two(env):\n    pass\n", py=True, near=True)
    # hunks: each side changes its own line of one section and adds a version of one key
    hb = "A\nB\nC\n"
    ho, ht = "A1\nkey: one way\nB\nC\n", "A\nB1\nkey: another way\nC\n"
    assert merge3(ho, hb, ht, hunks=True) == "A1\nkey: one way\nB1\nkey: another way\nC\n"
    assert merge3(ho, hb, ht, hunks=True, near=True) is None


def test_merge3_superseded_takes_theirs_only_when_it_holds_our_change():
    from ttp.batch import merge3
    base = "x = 1\nif a:\n    y = 2\nz = 3\n"
    comment_only = "x = 1  # set\nif a:\n    y = 2\nz = 3\n"
    assert merge3(comment_only, base, "x = 5\nif a:\n    y = 2\nz = 3\n", py=True) is None
    assert merge3(comment_only, base, "x = 5\nif a:\n    y = 2\nz = 3\n", py=True,
                  superseded=True) == "x = 5\nif a:\n    y = 2\nz = 3\n"
    ours = "x = 1\ny = 2\nz = 3\n"         # dropped the `if`
    theirs = "x = 1\n# no more if\ny = 2\nw = 4\nz = 3\n"
    assert merge3(ours, base, theirs, py=True, superseded=True) == theirs
    assert merge3("x = 1\ny = 9\nz = 3\n", base, theirs, py=True, superseded=True) is None
    # a "#" inside a string is code, not a comment
    assert merge3('x = "a #b"\nif a:\n    y = 2\nz = 3\n', base, "x = 5\nif a:\n    y = 2\nz = 3\n",
                  py=True, superseded=True) is None
    # in prompts only spacing is set aside
    assert merge3("A  rule.\nB\n", "A rule.\nB\n", "A rule, reworded.\nB\n", superseded=True) == \
        "A rule, reworded.\nB\n"
    assert merge3("A rule!\nB\n", "A rule.\nB\n", "A rule, reworded.\nB\n", superseded=True) is None


def test_merge3_superseded_keeps_a_deletion_upstream_only_reworded():
    """A line the project removed counts as removed upstream too only when upstream dropped it, not
    when it reworded it: otherwise the project's removal would be undone without anyone seeing it."""
    from ttp.batch import merge3
    flags = dict(taken_in=True, hunks=True, superseded=True)
    base = "a = 1\nb = 2\ncheck_dangerous()\nc = 3\n"
    assert merge3("a = 1\nb = 2\nc = 3\n", base, "a = 1\nb = 20\ncheck_dangerous(strict=True)\nc = 3\n",
                  py=True, **flags) is None
    # also when the project changed another line the same way upstream did
    assert merge3("a = 5\nb = 2\nc = 3\n", base, "a = 5\nb = 2\ncheck_dangerous(strict=True)\nc = 3\n",
                  py=True, **flags) is None
    md = "# R\n\n- one\n- rule two: do X\n- three\n"
    assert merge3("# R\n\n- one\n- three\n", md, "# R\n\n- one\n- rule two: always do X first\n- three\n",
                  **flags) is None
    # a deletion-only change where upstream rewrote the whole section is left for judgment
    assert merge3("# R\n\n- one\n- three\n", md, "# R\n\nAll new text.\nMore of it.\n", **flags) is None
    # a line upstream truly dropped still settles
    assert merge3("# R\n\n- one\n- three\n", md, "# R\n\n- one\n- three\n- four\n", **flags) == \
        "# R\n\n- one\n- three\n- four\n"


def test_merge3_superseded_reads_a_hash_inside_a_python_string_as_text():
    """Prompt text kept in a Python string holds Markdown headings: a project's edit of one is a
    change, not a comment, so it is not set aside as superseded."""
    from ttp.batch import merge3
    base = 'T = """\n# Rules\nbe nice\n"""\n'
    theirs = 'T = """\n# Rules\nbe kind\n"""\n'
    assert merge3('T = """\n# Rules (ours: never push)\nbe nice\n"""\n', base, theirs, py=True,
                  superseded=True) is None
    assert merge3('T = """\n# Rules\nbe nice  # ours\n"""\n', base, theirs, py=True,
                  superseded=True) is None
    # a real comment beside the string is still set aside
    assert merge3('T = """\n# Rules\nbe nice\n"""  # ours\n', base, theirs, py=True,
                  superseded=True) == theirs


def test_a_finished_worktree_stays_while_a_detached_push_runs_in_it(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import locks, push, worktree
    tid, path, _ = _code_task(p, "change")
    _commit_file(path, "change")
    marker = p.state / push.DETACHED / "t1-x.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"status": "running", "repo": str(path), "pid": 1}))
    held = locks.try_take([push._run_lock(marker)], "detached push t1-x")
    (res,) = worktree.sweep(p)
    assert path.exists() and res["held"] and "detached `ttp push`" in res["why"]
    held.close()     # the push ended (or died): the worktree goes as usual
    (res,) = worktree.sweep(p)
    assert not path.exists() and res["why"] is None


def test_local_only_clears_a_branch_recorded_as_superseded_by_a_commit_on_the_push_branch(env, monkeypatch, capsys):
    """A re-land after a push conflict puts a task's work on the push branch as one new commit with
    another patch and subject: the check flags it until a task records the commit that carries it.
    The record needs that commit on the push branch, covers the branch head it was made at, and also
    answers `landed:#<id>`."""
    from ttp import cli, landed
    from ttp import daemon as dm
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    _commit(path, "more.txt", "a\nb\n")
    _commit(path, "more.txt", "a\nb\nc\n")
    mine = _git_out(path, "rev-parse", "HEAD")
    (other / "more.txt").write_text("a\nb, settled\nc\n")
    _git_out(other, "add", "more.txt")
    _git_out(other, "commit", "-qm", "re-land of the more work after a push conflict")
    relanded = _git_out(other, "rev-parse", "HEAD")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    d = _local_only_daemon(p)
    _check_local_only(d)
    assert [e["task"] for e in _local_only_events(p)] == [tid]
    assert f"ttp task {p.name} superseded {tid} <sha>" in _local_only_events(p)[0]["text"]
    assert landed.check(p, repo, tid=tid)[0] == 1

    def ttp_task(*args):
        try:
            cli.main(["task", p.name, *args])
        except SystemExit as e:
            return e.code
        return 0
    assert ttp_task("superseded", str(tid), mine) not in (0, None), "the branch's own head is not on the push branch"
    assert ttp_task("superseded", str(tid)) not in (0, None)
    assert p.db.q("SELECT * FROM task_superseded") == []
    assert ttp_task("superseded", str(tid), relanded[:12]) == 0
    assert f"recorded as superseded by {relanded[:10]}" in capsys.readouterr().out
    assert landed.superseded(p.db, [tid]) == {tid: [(mine, relanded)]}
    assert landed.check(p, repo, tid=tid)[0] == 0
    d._local_only_due = 0
    _check_local_only(d)
    assert p.db.kv(dm.KV_LOCAL_ONLY) is None
    _commit(path, "after.txt", "new work after the record\n")
    d._local_only_due = 0
    _check_local_only(d)
    assert set(p.db.kv(dm.KV_LOCAL_ONLY) or {}) == {str(tid)}, "work past the recorded head is flagged again"


def test_superseded_on_another_tasks_local_branch_counts_only_once_a_remote_has_the_commit(env, monkeypatch, capsys):
    """The work of a done task is re-landed as a commit on another task's local branch of the project
    root, not yet on the push branch: `--ref` names that branch and the record is made, but the
    local-only check (worktree.is_superseded) keeps flagging the branch until a remote-tracking branch
    holds the commit."""
    from ttp import cli, landed, worktree
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    _commit(path, "more.txt", "a\nb\n")
    mine = _git_out(path, "rev-parse", "HEAD")
    _git_out(repo, "fetch", "-q", "origin")
    _git_out(other, "pull", "-q", "--rebase", "origin", "proj")
    (other / "more.txt").write_text("a\nb, settled\n")
    _git_out(other, "add", "more.txt")
    _git_out(other, "commit", "-qm", "re-land of the more work")
    relanded = _git_out(other, "rev-parse", "HEAD")
    _git_out(repo, "fetch", "-q", str(other), "HEAD:refs/heads/other-task")
    assert not _git_out(repo, "branch", "-r", "--contains", relanded)
    with pytest.raises(SystemExit) as e:
        cli.main(["task", p.name, "superseded", str(tid), relanded])
    assert e.value.code not in (0, None), "off the push branch it needs --ref"
    capsys.readouterr()
    cli.main(["task", p.name, "superseded", str(tid), relanded[:12], "--ref", "other-task"])
    assert f"recorded as superseded by {relanded[:10]}" in capsys.readouterr().out
    records = landed.superseded(p.db, [tid])[tid]
    assert records == [(mine, relanded)]
    assert worktree.is_superseded(repo, mine, records) is False, "no remote has the commit yet"
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    assert worktree.is_superseded(repo, mine, records) is True


def test_superseded_refuses_a_commit_that_cannot_carry_the_branchs_work(env, monkeypatch, capsys):
    """A record hides the branch for good at its head, so it must name a commit that can hold the
    work: not the branch's base or another commit the branch already has, not a later push-branch
    commit that changes none of the branch's files, and only for a done task."""
    from ttp import cli, landed
    from ttp import daemon as dm
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    _commit(path, "more.txt", "a\nb\n")
    _commit(path, "more.txt", "a\nb\nc\n")
    _commit(other, "unrelated.txt", "other work\n")
    unrelated = _git_out(other, "rev-parse", "HEAD")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    base = _git_out(repo, "merge-base", branch, "origin/proj")
    d = _local_only_daemon(p)
    _check_local_only(d)
    assert set(p.db.kv(dm.KV_LOCAL_ONLY) or {}) == {str(tid)}

    def ttp_task(*args):
        try:
            cli.main(["task", p.name, *args])
        except SystemExit as e:
            return e.code
        return 0
    assert ttp_task("superseded", str(tid), base) not in (0, None)
    assert "predates the work" in capsys.readouterr().err
    assert ttp_task("superseded", str(tid), unrelated) not in (0, None)
    assert "changes none of the files" in capsys.readouterr().err
    with pytest.raises(ValueError, match="not done"):
        p.db.update_task(tid, status="running")
        landed.record_superseded(p, tid, unrelated)
    p.db.update_task(tid, status="done")
    assert p.db.q("SELECT * FROM task_superseded") == []
    d._local_only_due = 0
    _check_local_only(d)
    assert set(p.db.kv(dm.KV_LOCAL_ONLY) or {}) == {str(tid)}, "the undelivered work stays flagged"


def _run_in(p, tid, cwd):
    """A finished run of task `tid` whose run.json records `cwd` as its working directory."""
    rid = p.db.x("INSERT INTO runs(task, role, status, started, ended) VALUES(?,?,?,?,?)",
                 (tid, "worker", "done", time.time(), time.time()))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"cwd": str(cwd)}))
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))


def test_superseded_reads_a_task_branch_in_the_second_repository_it_worked_in(env, monkeypatch, tmp_path, capsys):
    """A task that worked in another repository's worktree (<repo>/wt/t<id>) and delivered to that
    repo's PR head branch has no branch in the project root: the record reads its branch and commit
    in the repository its runs worked in, accepts a commit another branch there (or --ref) holds,
    and names the repository when the commit is missing. A task of the project root's own repo keeps
    the push-branch rule even with its worktree recorded."""
    from ttp import cli, landed
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    _run_in(p, tid, path)   # the root's own worktree: not a second repository

    def ttp_task(*args):
        try:
            cli.main(["task", p.name, *args])
        except SystemExit as e:
            return e.code
        return 0
    _commit(path, "more.txt", "a\nb\n")
    mine = _git_out(path, "rev-parse", "HEAD")
    assert ttp_task("superseded", str(tid), mine) not in (0, None), "the push-branch rule still holds"
    assert "is not on" in capsys.readouterr().err

    origin2, second = tmp_path / "second.git", tmp_path / "second"
    _git_out(tmp_path, "init", "-q", "--bare", str(origin2))
    _git_out(tmp_path, "init", "-q", "-b", "main", str(second))
    _commit(second, "lib.txt", "one\n")
    _git_out(second, "remote", "add", "origin", str(origin2))
    _git_out(second, "push", "-q", "origin", "main")
    tid2 = p.db.add_task("work in the second repo", "s", kind="code", tier="light", origin="user")
    wt = second / "wt" / f"t{tid2}"
    _git_out(second, "worktree", "add", "-q", "-b", "feat-x", str(wt), "main")
    _commit(wt, "lib.txt", "one\ntwo\n")
    p.db.update_task(tid2, status="done", branch="feat-x")
    _run_in(p, tid2, wt)
    _git_out(second, "checkout", "-q", "-b", "pr-head", "main")
    _commit(second, "lib.txt", "one\ntwo, settled\n")
    relanded = _git_out(second, "rev-parse", "HEAD")
    _git_out(second, "push", "-q", "origin", "pr-head")
    _git_out(second, "checkout", "-q", "main")
    _git_out(second, "branch", "-q", "-D", "pr-head")   # only the remote ref holds it now

    assert ttp_task("superseded", str(tid2), "deadbeef" * 5) not in (0, None)
    err = capsys.readouterr().err
    assert f"no commit {'deadbeef' * 5} in {wt}" in err and "feat-x" in err
    with pytest.raises(ValueError, match="not on origin/main"):
        landed.record_superseded(p, tid2, relanded, ref="origin/main")
    assert p.db.q("SELECT * FROM task_superseded WHERE task=?", (tid2,)) == []
    assert ttp_task("superseded", str(tid2), relanded[:12]) == 0
    out = capsys.readouterr().out
    assert f"recorded as superseded by {relanded[:10]}" in out and "origin/pr-head" in out
    feat = _git_out(wt, "rev-parse", "HEAD")
    assert landed.superseded(p.db, [tid2]) == {tid2: [(feat, relanded)]}

    # Once the worktree is gone, the folder above it still names the repository.
    _git_out(second, "worktree", "remove", "--force", str(wt))
    p.db.x("DELETE FROM task_superseded WHERE task=?", (tid2,))
    assert landed.record_superseded(p, tid2, relanded, ref="origin/pr-head")
    # A branch in no repository the task worked in: the error lists each one checked.
    p.db.update_task(tid2, branch="gone-branch")
    with pytest.raises(ValueError, match=f"no branch gone-branch to record \\(checked {repo}, {second}/wt\\)"):
        landed.record_superseded(p, tid2, relanded)


def test_superseded_ref_names_the_branch_of_a_done_task_whose_work_is_in_a_second_repository(env, monkeypatch,
                                                                                               tmp_path, capsys):
    """A done task with no branch on record whose worker made its own worktree of a second repository
    in its run folder (runs/<id>/<name>): --ref names that branch, found in the run worktree's
    repository (or any repository the project's runs made worktrees of), and the supersession is
    recorded against a commit another branch there holds. Without --ref the error says to name it."""
    from ttp import cli, landed
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)

    def ttp_task(*args):
        try:
            cli.main(["task", p.name, *args])
        except SystemExit as e:
            return e.code
        return 0
    second = tmp_path / "second"
    _git_out(tmp_path, "init", "-q", "-b", "main", str(second))
    _commit(second, "lib.txt", "one\n")
    tid2 = p.db.add_task("work in the second repo", "s", kind="code", tier="light", origin="user")
    p.db.update_task(tid2, status="done", branch=None)
    _run_in(p, tid2, p.root)   # the run worked from the project root
    rid = p.db.one("SELECT id FROM runs WHERE task=?", (tid2,))["id"]
    wt = p.runs / str(rid) / "second"
    _git_out(second, "worktree", "add", "-q", "-b", f"ttp/t{tid2}-x", str(wt), "main")
    _commit(wt, "lib.txt", "one\ntwo\n")
    feat = _git_out(wt, "rev-parse", "HEAD")
    _git_out(second, "checkout", "-q", "-b", "pr-head", "main")
    _commit(second, "lib.txt", "one\ntwo, settled\n")
    relanded = _git_out(second, "rev-parse", "HEAD")
    _git_out(second, "checkout", "-q", "main")

    assert ttp_task("superseded", str(tid2), relanded) not in (0, None)
    err = capsys.readouterr().err
    assert "no branch on record" in err and "--ref <branch>" in err
    assert ttp_task("superseded", str(tid2), relanded[:12], "--ref", f"ttp/t{tid2}-missing") not in (0, None)
    assert str(second) in capsys.readouterr().err, "the error names the repositories checked"
    assert ttp_task("superseded", str(tid2), relanded[:12], "--ref", f"ttp/t{tid2}-x") == 0
    out = capsys.readouterr().out
    assert f"recorded as superseded by {relanded[:10]}" in out and "pr-head" in out
    assert landed.superseded(p.db, [tid2]) == {tid2: [(feat, relanded)]}
    assert landed.task_shas(p, tid2)[0] == [relanded]

    # A run folder of another task that made a worktree of the same repository still finds it.
    p.db.x("DELETE FROM task_superseded WHERE task=?", (tid2,))
    p.db.x("UPDATE runs SET task=? WHERE id=?", (tid, rid))
    assert "superseded by" in landed.record_superseded(p, tid2, relanded, ref=f"ttp/t{tid2}-x")


def test_superseded_with_ref_records_a_task_that_never_had_a_branch(env, monkeypatch, capsys):
    """A done task with no branch (it never pushed): without --ref the record is refused with a hint;
    with --ref it is made from that ref and commit alone (head ""), answers `landed:#<id>`, and is
    refused when the commit is not on the ref or does not exist."""
    from ttp import cli, landed
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    tid2 = p.db.add_task("work that never had a branch", "s", kind="code", tier="light", origin="user")
    p.db.update_task(tid2, status="done", branch=None)
    _commit(other, "elsewhere.txt", "carried\n")
    carried = _git_out(other, "rev-parse", "HEAD")
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/side")
    _git_out(repo, "fetch", "-q", "origin")

    def ttp_task(*args):
        try:
            cli.main(["task", p.name, *args])
        except SystemExit as e:
            return e.code
        return 0
    assert ttp_task("superseded", str(tid2), carried) not in (0, None)
    assert "no branch on record" in capsys.readouterr().err
    assert ttp_task("superseded", str(tid2), carried, "--ref", "origin/proj") not in (0, None)
    assert "is not on origin/proj" in capsys.readouterr().err
    assert ttp_task("superseded", str(tid2), "deadbeef" * 5, "--ref", "origin/side") not in (0, None)
    assert f"no commit {'deadbeef' * 5}" in capsys.readouterr().err
    assert p.db.q("SELECT * FROM task_superseded WHERE task=?", (tid2,)) == []
    assert landed.check(p, repo, tid=tid2)[0] != 0
    assert ttp_task("superseded", str(tid2), carried[:12], "--ref", "origin/side") == 0
    assert f"recorded as superseded by {carried[:10]}" in capsys.readouterr().out
    assert landed.superseded(p.db, [tid2]) == {tid2: [("", carried)]}
    assert landed.check(p, repo, tid=tid2, onto="origin/side")[0] == 0
    # A local branch that already holds the commit is a ref it is on, not the task's own branch.
    p.db.x("DELETE FROM task_superseded WHERE task=?", (tid2,))
    _git_out(repo, "branch", "side-local", "origin/side")
    assert "no branch; recorded as superseded" in landed.record_superseded(p, tid2, carried, ref="side-local")
    assert landed.superseded(p.db, [tid2]) == {tid2: [("", carried)]}


def test_a_task_whose_change_landed_under_another_commit_gets_no_review_or_fix(env, monkeypatch):
    """Replay of a duplicate: a code task's one commit (its change plus a version bump and changeset)
    sits in review while a fix chain of another task rebased the change and the push queue landed it
    as a new commit, without the bump and with other context around it, so git's patch-id differs. The
    sweep, the daemon's review and the fix of a failed review all find it landed: the task is recorded
    as superseded and closed, the coordinator is told once, and no review or fix is queued."""
    from ttp import landed
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    BUMP = {"files": ["VERSION"], "changeset_dir": ".changeset"}
    p.set_config("delivery.version_bump", BUMP)
    lines = [f"line {i}" for i in range(1, 11)]
    (repo / "VERSION").write_text("0.1.0\n")
    (repo / "app.txt").write_text("\n".join(lines) + "\n")
    _git_out(repo, "add", "VERSION", "app.txt")
    _git_out(repo, "commit", "-qm", "base")
    _git_out(repo, "push", "-q", "origin", "HEAD:proj")
    _git_out(other, "pull", "-q", "origin", "proj")
    tid, path, branch = _code_task(p, "duplicate change", status="review")
    p.db.update_task(tid, result=json.dumps({"status": "done", "summary": "made the change"}))
    change = lines[:5] + ["line 6 changed"] + lines[6:]
    (path / "app.txt").write_text("\n".join(change) + "\n")
    (path / "VERSION").write_text("0.1.1\n")
    (path / ".changeset").mkdir()
    (path / ".changeset" / "change.md").write_text("the change\n")
    _git_out(path, "add", "-A")
    _git_out(path, "commit", "-qm", "the change, with its version bump")
    head = _git_out(path, "rev-parse", "HEAD")
    assert landed.equivalent(p, branch, BUMP) is None, "not landed yet"
    # Another task's chain: a neighbouring change first, then the same change re-landed, then a fix.
    (other / "app.txt").write_text("\n".join(lines[:3] + ["line 4 by someone else"] + lines[4:]) + "\n")
    _git_out(other, "commit", "-qam", "a neighbouring change")
    (other / "app.txt").write_text("\n".join(lines[:3] + ["line 4 by someone else"] + change[4:]) + "\n")
    _git_out(other, "commit", "-qam", "re-land of the change under another task")
    relanded = _git_out(other, "rev-parse", "HEAD")
    _commit(other, "fix.txt", "a later fix of the change\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    tip = _git_out(repo, "rev-parse", "origin/proj")
    assert not landed.on_branch(repo, head, tip), "git's patch-id and the commit keys miss it"
    assert landed.equivalent(p, branch, None) is None, "the version files count unless excluded"
    assert landed.equivalent(p, branch, BUMP) == (relanded, head)

    def reset():
        p.db.x("DELETE FROM task_superseded")
        p.db.x("DELETE FROM events WHERE kind='task_superseded'")
        p.db.update_task(tid, status="review")

    def told():
        return p.db.q("SELECT status, text FROM events WHERE kind='task_superseded' AND task=?", (tid,))

    def closed():
        t = p.db.task(tid)
        assert t["status"] == "done" and json.loads(t["result"])["superseded_by"] == relanded
        assert landed.superseded(p.db, [tid]) == {tid: [(head, relanded)]}
        (e,) = told()
        assert e["status"] == "queued" and relanded[:10] in e["text"] and "no review or fix" in e["text"]
        assert not p.db.q("SELECT id FROM tasks WHERE id!=? AND status NOT IN ('done','failed','cancelled')",
                          (tid,)), "no review or fix queued"

    _ran(p, tid, time.time() - 1900)
    d = Daemon(p.base)
    _sweep(d)
    closed()
    _sweep(d)
    assert len(told()) == 1, "told once"
    reset()
    assert d._auto_review(p.db.task(tid), "made the change") is None
    closed()
    reset()
    review = p.db.add_task(f"Review #{tid}: duplicate change", "Review it.", kind="review", origin="coordinator",
                           depends_on=[tid])
    p.db.update_task(review, status="failed")
    assert d._fix_failed_review(p.db.task(review), [{"title": "fix it", "spec": "fix the finding"}], "bad") is None
    closed()


def test_a_reviewed_task_whose_branch_already_is_on_the_push_branch_closes_as_landed(env, monkeypatch):
    """A code task whose review passed but pushed nothing, because its branch already is on the push
    branch (pushed or fast-forwarded as is, or rebased there with the same author, date and subject),
    is closed as landed by the review sweep, the landed commit recorded. A review that failed, asked
    for changes, still runs or pushed, and a branch not on the push branch, leave it in review."""
    from ttp import landed
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    d = Daemon(p.base)

    def task(name):
        tid, path, branch = _code_task(p, name, status="review")
        p.db.update_task(tid, result=json.dumps({"status": "done", "summary": f"made {name}"}))
        _commit(path, f"{name}.txt", f"{name}\n")
        _ran(p, tid, time.time() - 1900)
        return tid, path, branch, _git_out(path, "rev-parse", "HEAD")

    def review(tid, status, result=None):
        rid = p.db.add_task(f"Review #{tid}", "Review it.", kind="review", origin="coordinator", depends_on=[tid])
        p.db.update_task(rid, status=status, result=json.dumps(result or {"status": status, "summary": "ok"}))
        p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() + 1, rid))
        return rid

    def status(tid):
        return p.db.task(tid)["status"]

    # Not on the push branch yet: a passed review leaves it in review (the push queue's to ship).
    tid, path, branch, head = task("plain")
    rid = review(tid, "done")
    assert landed.on_push_branch(p, branch) is None
    _sweep(d)
    assert status(tid) == "review" and not landed.superseded(p.db, [tid])
    # Pushed as is (a plain landing): an ancestor of the tip, which equivalent leaves out.
    _git_out(path, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    assert landed.equivalent(p, branch, None) is None and landed.on_push_branch(p, branch) == (head, head)
    for bad in ("failed", "running", "queued"):
        p.db.update_task(rid, status=bad)
        _sweep(d)
        assert status(tid) == "review", bad
    p.db.update_task(rid, status="done", result=json.dumps({"status": "done", "metrics": {"verdict": "changes needed"}}))
    _sweep(d)
    assert status(tid) == "review", "a review that asked for changes"
    p.db.update_task(rid, result=json.dumps({"status": "done", "summary": "ok"}))
    late = review(tid, "failed")
    _sweep(d)
    assert status(tid) == "review", "a later review failed"
    p.db.update_task(late, status="cancelled")
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated) VALUES(?,?,?,?,?,?,?)",
           (rid, branch, head, "origin/proj", "failed", time.time(), time.time()))
    _sweep(d)
    assert status(tid) == "review", "a review that pushed is the push queue's to settle"
    p.db.x("DELETE FROM push_queue WHERE task=?", (rid,))
    _sweep(d)
    t = p.db.task(tid)
    assert t["status"] == "done" and json.loads(t["result"])["landed_as"] == head
    assert landed.superseded(p.db, [tid]) == {tid: [(head, head)]}
    (e,) = p.db.q("SELECT status, text FROM events WHERE kind='task_done' AND task=?", (tid,))
    assert e["status"] == "handled" and "already is on origin/proj" in e["text"]
    _sweep(d)
    assert len(p.db.q("SELECT 1 FROM events WHERE kind='task_done' AND task=?", (tid,))) == 1, "once"
    # Rebased there under the same author, date and subject, with a touch-up: patch-ids and changed
    # lines differ, so only the commit's identity finds it.
    tid, path, branch, head = task("rebased")
    rid = review(tid, "done")
    _git_out(other, "pull", "-q", "origin", "proj")
    _commit(other, "neighbour.txt", "someone else\n")
    _git_out(other, "fetch", "-q", repo, branch)
    _git_out(other, "cherry-pick", "FETCH_HEAD")
    (other / "rebased.txt").write_text("rebased, touched up\n")
    _git_out(other, "commit", "-q", "--amend", "--no-edit", "-a")
    relanded = _git_out(other, "rev-parse", "HEAD")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    assert landed.equivalent(p, branch, None) is None
    assert landed.on_push_branch(p, branch) == (relanded, head)
    _sweep(d)
    t = p.db.task(tid)
    assert t["status"] == "done" and json.loads(t["result"])["landed_as"] == relanded


def test_backup_remote_refuses_to_name_a_branch(env):
    p = make(env)
    from ttp import coordinator as coord, push
    from ttp import daemon as dm
    from ttp.project import config_problems
    d = {"push_branch": "origin/team/work", "base_ref": "origin/dev"}
    for bad in ("main", "master", "origin/team/work", "team/work", "origin/dev", "dev"):
        assert "names a branch" in push.backup_problem({**d, "backup_remote": bad}) \
            or "not a git remote" in push.backup_problem({**d, "backup_remote": bad}), bad
        assert config_problems({"delivery": {**d, "backup_remote": bad}}), bad
    assert push.backup_problem({**d, "backup_remote": "backup"}) == ""
    assert push.backup_problem({**d, "backup_remote": ""}) == ""
    assert push.backup(env["repo"], "backup", "main")[0] == "refused"   # only ttp/t<id>-... branches
    p.set_config("delivery.base_ref", "dev")
    bad = {"type": "config_set", "key": "delivery.backup_remote", "value": "dev"}
    notes = coord.apply(p, [bad], user_turn=True)
    assert notes and "names a branch" in notes[0], notes
    assert "ask_user (blocking access)" in coord.apply(p, [{**bad, "value": "backup"}])[0]
    assert coord.apply(p, [{**bad, "value": "backup"}], user_turn=True) == []
    assert coord.apply(p, [{**bad, "value": ""}]) == []   # off needs no one's word
    # A key that names a branch anyway (edited by hand) pushes nothing and drops the queue.
    p.set_config("delivery.base_ref", "")
    remote = _backup_remote(env, "main")
    p.set_config("delivery.backup_remote", "main")
    tid, path, branch = _code_task(p, "work")
    _commit_file(path, "a")
    dm_ = dm.Daemon(p.base)
    dm_._queue_backup(p.db.task(tid))
    _finish_backup(dm_)
    assert p.db.kv(dm.KV_BACKUP) is None and not _remote_head(remote, branch)


def _ff_setup(env, monkeypatch, also):
    """_push_setup plus a `main` on origin at the target's tip, kept in step by delivery.fast_forward_also."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    if also is not None:
        p.set_config("delivery.fast_forward_also", also)
    return p, repo, origin, other


def test_push_fast_forwards_an_extra_branch_and_reads_it_back_from_the_remote(env, monkeypatch, capsys):
    from ttp import push
    p, repo, origin, other = _ff_setup(env, monkeypatch, ["main"])
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0
    sha = _git_out(origin, "rev-parse", "proj")
    assert _git_out(origin, "rev-parse", "main") == sha
    assert push.last_ff == [f"ff main {sha}"]
    # The detached marker records it, and the probe reports the remote's state.
    marker = p.state / "pushes" / "x.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"id": "x", "status": "pushed", "sha": sha, "target": "origin/proj",
                                  "fast_forward": [f"ff main {sha}", "not ff old: rejected"]}))
    capsys.readouterr()
    assert push.result(marker) == 0
    out = capsys.readouterr().out
    assert f"ttp push: ff main {sha}" in out and "ttp push: warning: not ff old: rejected" in out


def test_push_leaves_a_diverged_extra_branch_alone_and_says_not_ff(env, monkeypatch):
    from ttp import push
    p, repo, origin, other = _ff_setup(env, monkeypatch, ["main"])
    _commit(other, "hotfix.txt", "fix\n")
    _git_out(other, "push", "-q", "origin", "HEAD:main")
    main = _git_out(origin, "rev-parse", "main")
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0, "a not-ff never fails the main push"
    sha = _git_out(origin, "rev-parse", "proj")
    assert sha != main and _git_out(origin, "rev-parse", "main") == main, "the remote branch stays as it was"
    assert len(push.last_ff) == 1 and push.last_ff[0].startswith("not ff main: ") \
        and "not an ancestor" in push.last_ff[0], push.last_ff
    assert push.ff_warnings(push.last_ff) == push.last_ff


def test_push_without_fast_forward_also_moves_nothing_else(env, monkeypatch):
    from ttp import push
    p, repo, origin, other = _ff_setup(env, monkeypatch, None)
    main = _git_out(origin, "rev-parse", "main")
    for also in (None, [], ""):
        if also is not None:
            p.set_config("delivery.fast_forward_also", also)
        _commit(repo, "mine.txt", f"{also!r}\n")
        assert push.run(p, repo) == 0
        assert _git_out(origin, "rev-parse", "main") == main and push.last_ff == []


def test_fast_forward_also_scalar_is_reported_and_never_raises_after_a_push(env, monkeypatch):
    from ttp import push
    from ttp.project import config_problems
    for bad in (True, 5):
        with pytest.raises(ValueError):
            push.fast_forward_check(bad, "origin/proj")
        assert any("fast_forward_also" in l for l in
                   config_problems({"delivery": {"push_branch": "origin/proj", "fast_forward_also": bad}})), bad
        assert push.fast_forward_list(bad) == [] and push.exclude_list(bad) == []
    p, repo, origin, other = _ff_setup(env, monkeypatch, None)
    main = _git_out(origin, "rev-parse", "main")
    for bad in (True, 5):
        p.set_config("delivery.fast_forward_also", bad)
        _commit(repo, "mine.txt", f"{bad!r}\n")
        assert push.run(p, repo) == 0
        assert _git_out(origin, "rev-parse", "main") == main and push.last_ff == []


def test_scalar_push_checks_after_push_and_exclude_paths_are_reported_and_refused(env, monkeypatch, capsys):
    from ttp import push
    from ttp.coordinator import apply
    from ttp.project import config_problems
    p = make(env)
    for bad in (True, 5, 1.5):
        for key in ("push_checks", "after_push", "push_exclude_paths", "mirror_remotes"):
            assert config_problems({"delivery": {key: bad}}) == [f"delivery.{key}: {bad!r}: not a list"], (key, bad)
        with pytest.raises(ValueError, match="not a list"):
            push.checks_of(bad)
        probs = apply(p, [{"type": "config_set", "key": f"delivery.{k}", "value": bad}
                          for k in ("push_checks", "after_push")])
        assert len(probs) == 2 and all("not a list" in x for x in probs), probs
        assert "push_checks" not in (p.config().get("delivery") or {})
        probs = apply(p, [{"type": "config_set", "key": "delivery.mirror_remotes", "value": bad}])
        assert probs == [f"delivery.mirror_remotes: {bad!r}: not a list"] or "not a list" in str(probs), probs
        assert "mirror_remotes" not in (p.config().get("delivery") or {})
    for ok in (None, False, 0, "", [], "tmp", ["tmp"]):   # off or a list: no problem
        assert config_problems({"delivery": {"push_exclude_paths": ok}}) == [], ok
    # A scalar push_exclude_paths would turn the guard off: the push branch refuses, --own does not.
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.push_exclude_paths", 5)
    (repo / "tmp").mkdir()
    _git_out(repo, "checkout", "-q", "-b", "ttp/t31-exp")
    _commit(repo, "tmp/scratch.txt", "x\n")
    before = _git_out(origin, "rev-parse", "proj")
    assert _ttp_push() == 2 and "delivery.push_exclude_paths: 5: not a list" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "proj") == before
    monkeypatch.setenv("TTP_TASK", "31")
    assert _ttp("push", "--own") == 0


def test_an_ordinary_push_runs_after_push_at_the_pushed_commit_and_the_daemon_reports_it(env, monkeypatch):
    """after_push runs after every guarded `ttp push`, not only push queue batches: in a clean checkout
    at the pushed commit, with a durable receipt the daemon turns into one event."""
    from ttp import push, pushq
    seen = env["tmp"] / "after_push.txt"
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.after_push", [f"(env | grep '^TTP_PUSH'; echo head $(git rev-parse HEAD); pwd) > {seen}"])
    monkeypatch.setenv("TTP_TASK", "7")
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0
    sha = _git_out(origin, "rev-parse", "proj")
    lines = seen.read_text().splitlines()
    for want in (f"TTP_PUSHED_SHA={sha}", "TTP_PUSH_TARGET=origin/proj", "TTP_PUSH_TASKS=7", f"head {sha}"):
        assert want in lines, (want, lines)
    assert not any(x.startswith("TTP_PUSH_BATCH=") for x in lines), lines
    (r,) = _receipts(p)
    assert f"TTP_PUSH_RECEIPT={p.state / 'after_push' / (r['id'] + '.json')}" in lines, lines
    assert r["status"] == "finished" and r["after_push"]["status"] == "ok" and r["pushed_sha"] == sha
    assert push.last_after["status"] == "ok" and not any(p.worktrees.glob("after_push-*"))
    assert not list((p.state / "locks").glob("after_push:*")), "a finished after_push leaves no lock"
    alerts = []
    assert pushq.tend_receipts(p, lambda *a, **k: alerts.append(a)) == [r["id"]] and alerts == []
    ev = p.db.one("SELECT * FROM events WHERE kind='after_push_ok'")
    assert ev and sha[:7] in ev["text"] and ev["status"] == "handled"
    assert pushq.tend_receipts(p, lambda *a, **k: alerts.append(a)) == [], "reported once"
    assert p.db.kv(pushq.AFTER_PUSH_OK)


def test_after_push_does_not_run_on_a_refused_push_an_own_branch_push_or_when_unset(env, monkeypatch):
    from ttp import push
    ran = env["tmp"] / "ran"
    p, repo, origin, other = _push_setup(env, monkeypatch, ["test ! -f bad.txt"])
    p.set_config("delivery.after_push", [f"touch {ran}"])
    _commit(repo, "bad.txt", "bad\n")
    assert push.run(p, repo) == push.CHECKS_FAILED
    assert not ran.exists() and _receipts(p) == [] and push.last_after is None
    _git_out(repo, "rm", "-q", "bad.txt")
    _git_out(repo, "commit", "-qm", "fix")
    monkeypatch.setenv("TTP_TASK", "40")
    _git_out(repo, "checkout", "-q", "-b", "ttp/t40-feature")
    _commit(repo, "a.txt", "a\n")
    assert push.run(p, repo, own=True) == 0 and _git_out(origin, "rev-parse", "ttp/t40-feature")
    assert not ran.exists() and _receipts(p) == [], "an own-branch push never deploys"
    _git_out(repo, "checkout", "-q", "-B", "work", "HEAD")
    for unset in (None, [], ""):
        p.set_config("delivery.after_push", unset)
        _commit(repo, "b.txt", f"{unset!r}\n")
        assert push.run(p, repo) == 0
        assert not ran.exists() and _receipts(p) == [] and push.last_after is None


def test_a_failed_or_timed_out_after_push_keeps_the_push_and_alerts_once(env, monkeypatch, capsys):
    from ttp import alerts as alerts_mod, push, pushq
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.after_push", ["echo deploying; echo deploy-broke; exit 3"])
    _commit(repo, "mine.txt", "mine\n")
    capsys.readouterr()
    assert push.run(p, repo) == 0, "a failed after_push never fails the push"
    sha = _git_out(origin, "rev-parse", "proj")
    assert sha == _git_out(repo, "rev-parse", "HEAD")
    assert "warning: after_push failed (exit 3)" in capsys.readouterr().err
    (r,) = _receipts(p)
    assert (r["after_push"]["status"], r["after_push"]["exit"]) == ("failed", 3) and r["pushed_sha"] == sha
    raised = []
    pushq.tend_receipts(p, lambda key, text, **k: raised.append((key, text, k)))
    assert len(raised) == 1 and raised[0][0] == "after_push_failed" and "exit 3" in raised[0][1] \
        and raised[0][2]["severity"] == "high", raised
    ev = p.db.one("SELECT * FROM events WHERE kind='after_push_failed'")
    assert ev["status"] == "queued" and "deploy-broke" in ev["text"] and "The push stands" in ev["text"]
    pushq.tend_receipts(p, lambda key, text, **k: raised.append((key, text, k)))
    assert len(raised) == 1, "each receipt is reported once"
    # The alert lasts until an after_push succeeds.
    since = time.time() - 1
    assert alerts_mod.holds(p.db, "after_push_failed", since, time.time())
    # Timeout: the command is killed at after_push_timeout_s; the push still stands.
    p.set_config("delivery.after_push", ["sleep 60"])
    p.set_config("delivery.after_push_timeout_s", 1)
    _commit(repo, "two.txt", "two\n")
    t0 = time.time()
    assert push.run(p, repo) == 0 and time.time() - t0 < 30
    assert _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")
    assert push.last_after["status"] == "timeout"
    pushq.tend_receipts(p, lambda key, text, **k: raised.append((key, text, k)))
    assert len(raised) == 2 and "timed out" in raised[1][1]
    p.set_config("delivery.after_push", ["true"])
    p.set_config("delivery.after_push_timeout_s", 120)   # 1 s is too short for `true` on a loaded host
    _commit(repo, "three.txt", "three\n")
    assert push.run(p, repo) == 0 and push.last_after["status"] == "ok"
    pushq.tend_receipts(p)
    assert not alerts_mod.holds(p.db, "after_push_failed", since, time.time()), "a success clears it"


def test_an_after_push_whose_process_died_is_reported_as_killed(env, monkeypatch):
    from ttp import pushq
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    folder = p.state / "after_push"
    folder.mkdir(parents=True)
    (folder / "p1.json").write_text(json.dumps({"id": "p1", "status": "running", "pushed_sha": "a" * 40,
                                                "target": "origin/proj", "started": time.time()}))
    raised = []
    assert pushq.tend_receipts(p, lambda key, text, **k: raised.append(text)) == ["p1"]
    assert len(raised) == 1 and "stopped before it finished" in raised[0]
    assert json.loads((folder / "p1.json").read_text())["after_push"]["status"] == "killed"


def test_a_newer_ok_receipt_clears_an_after_push_failure_an_older_one_raised_in_the_same_tick(env, monkeypatch):
    """The tick's `now` is earlier than the alert an older failed receipt raises in that tick: the
    success a newer receipt records must still count as after it."""
    from ttp import alerts as alerts_mod, pushq
    p = make(env)
    folder = p.state / "after_push"
    folder.mkdir(parents=True)
    for rid, status in (("p1", "failed"), ("p2", "ok")):
        (folder / f"{rid}.json").write_text(json.dumps({
            "id": rid, "status": "finished", "pushed_sha": "a" * 40, "target": "origin/proj",
            "after_push": {"status": status, "exit": 3 if status == "failed" else 0}}))
    raised = []
    assert pushq.tend_receipts(p, lambda key, text, **k: raised.append(time.time()),
                               now=time.time() - 5) == ["p1", "p2"]
    assert len(raised) == 1
    assert not alerts_mod.holds(p.db, "after_push_failed", raised[0], time.time()), "the newer success clears it"


def test_an_ordinary_push_gives_after_push_the_version_at_the_pushed_commit(env, monkeypatch):
    from ttp import push
    seen = env["tmp"] / "version.txt"
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.after_push", [f"echo \"v=$TTP_PUSHED_VERSION\" > {seen}"])
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0 and push.last_after["status"] == "ok"
    assert seen.read_text().strip() == "v=0.1.0"
    assert _receipts(p)[0]["version"] == "0.1.0"
    p.set_config("delivery.version_bump", None)   # no version_bump: no version to give
    _commit(repo, "two.txt", "two\n")
    assert push.run(p, repo) == 0
    assert seen.read_text().strip() == "v="


def test_a_malformed_after_push_is_recorded_as_a_failure_that_alerts(env, monkeypatch, capsys):
    from ttp import push, pushq
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.after_push", 5)
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0, "the push stands"
    assert _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")
    assert push.last_after["status"] == "failed" and "not a list" in push.last_after["tail"]
    (r,) = _receipts(p)
    assert r["status"] == "finished" and r["after_push"]["status"] == "failed"
    raised = []
    pushq.tend_receipts(p, lambda key, text, **k: raised.append(key))
    assert raised == ["after_push_failed"]
    assert pushq._after_push_set(p, None), "a malformed after_push is not off: its failure stands"
    # A value that makes reading the commands raise is recorded the same way.
    p.set_config("delivery.after_push", ["./deploy"])
    real = push.check_list

    def check_list(v):
        if v == ["./deploy"]:
            raise ValueError("bad entry")
        return real(v)
    monkeypatch.setattr(push, "check_list", check_list)
    _commit(repo, "two.txt", "two\n")
    assert push.run(p, repo) == 0 and push.last_after["status"] == "failed"
    pushq.tend_receipts(p, lambda key, text, **k: raised.append(key))
    assert raised == ["after_push_failed"] * 2 and "bad entry" in max(_receipts(p), key=lambda r: r["started"])["after_push"]["tail"]


def test_fast_forward_also_refuses_the_push_branch_and_bad_names(env, monkeypatch):
    from ttp import push
    from ttp import coordinator as coord
    from ttp.project import config_problems
    p, repo, origin, other = _ff_setup(env, monkeypatch, ["proj", "a..b", "main"])
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0
    sha = _git_out(origin, "rev-parse", "proj")
    assert push.last_ff == ["not ff proj: it is the push branch itself (delivery.push_branch)",
                            "not ff a..b: not a valid branch name", f"ff main {sha}"]
    for bad in ("proj", "origin/proj", "a..b", "-x"):
        with pytest.raises(ValueError):
            push.fast_forward_check([bad], "origin/proj")
        assert config_problems({"delivery": {"push_branch": "origin/proj", "fast_forward_also": [bad]}}), bad
    assert push.fast_forward_check('["main", "main", " release "]', "origin/proj") == ["main", "release"]
    # From chat it needs the user's word; turning it off does not.
    bad = {"type": "config_set", "key": "delivery.fast_forward_also", "value": ["main"]}
    assert "ask_user (blocking restriction)" in coord.apply(p, [bad])[0]
    assert "push branch" in coord.apply(p, [{**bad, "value": ["proj"]}], user_turn=True)[0]
    assert coord.apply(p, [bad], user_turn=True) == [] and p.config()["delivery"]["fast_forward_also"] == ["main"]
    assert coord.apply(p, [{**bad, "value": []}]) == []



def _mirror_setup(env, monkeypatch, mirrors=None, branch="dev"):
    """_push_setup plus a second bare repository, git remote `mirror`, whose `branch` is at the
    target's tip, kept in step by delivery.mirror_remotes."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    mirror = env["tmp"] / "mirror.git"
    subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True)
    _git_out(repo, "remote", "add", "mirror", str(mirror))
    _git_out(repo, "push", "-q", "mirror", f"HEAD:refs/heads/{branch}")
    p.set_config("delivery.mirror_remotes", mirrors if mirrors is not None else [{"remote": "mirror", "branch": branch}])
    return p, repo, origin, mirror


def _no_force(monkeypatch):
    """Every git call mirror() makes, recorded; none may force."""
    from ttp import push
    calls, real = [], push._git_net
    monkeypatch.setattr(push, "_git_net", lambda repo, *a, **k: (calls.append(a), real(repo, *a, **k))[1])
    forced = lambda: [a for a in calls if a[0] == "push" and (  # noqa: E731
        {"-f", "--force", "--force-with-lease", "--mirror"} & set(a) or any(x.startswith("+") for x in a[1:]))]
    return calls, forced


def test_push_mirrors_the_pushed_head_to_another_remote_fast_forward_only(env, monkeypatch, capsys):
    from ttp import push
    p, repo, origin, mirror = _mirror_setup(env, monkeypatch, [{"remote": "mirror", "branch": "dev"}, "mirror/new"])
    calls, forced = _no_force(monkeypatch)
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0
    sha = _git_out(origin, "rev-parse", "proj")
    assert _git_out(mirror, "rev-parse", "dev") == sha and _git_out(mirror, "rev-parse", "new") == sha, \
        "the same commit goes to the mirror; a branch it lacks is created"
    assert push.last_ff == [f"mirror mirror/dev {sha}", f"mirror mirror/new {sha}"]
    assert any(a[0] == "fetch" for a in calls), "each mirror is fetched first"
    assert [a for a in calls if a[0] == "push"] and forced() == [], calls
    assert f"ttp push: mirror mirror/dev {sha}" in capsys.readouterr().err
    # Already there: nothing is pushed, and it still reads back as mirrored.
    calls.clear()
    assert push.mirror(repo, sha, push.mirror_list([{"remote": "mirror", "branch": "dev"}]), "origin/proj") == \
        [f"mirror mirror/dev {sha}"] and not [a for a in calls if a[0] == "push"]


def test_push_skips_a_diverged_mirror_names_both_commits_and_notes_it(env, monkeypatch):
    from ttp import push
    p, repo, origin, mirror = _mirror_setup(env, monkeypatch)
    run_dir = env["tmp"] / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    side = env["tmp"] / "side"
    subprocess.run(["git", "clone", "-q", "-b", "dev", str(mirror), str(side)], check=True)
    _commit(side, "hotfix.txt", "fix\n")
    _git_out(side, "push", "-q", "origin", "HEAD:dev")
    theirs = _git_out(mirror, "rev-parse", "dev")
    calls, forced = _no_force(monkeypatch)
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0, "a diverged mirror never fails the main push"
    sha = _git_out(origin, "rev-parse", "proj")
    assert _git_out(mirror, "rev-parse", "dev") == theirs, "the mirror stays as it was"
    assert not [a for a in calls if a[0] == "push"] and forced() == [], "nothing is pushed to a diverged mirror"
    [line] = push.last_ff
    assert line.startswith("not mirrored mirror/dev: ") and theirs in line and sha in line, line
    assert push.ff_warnings(push.last_ff) == push.last_ff
    note = (run_dir / "progress.md").read_text()
    assert "not mirrored mirror/dev" in note and theirs in note and sha in note, note


def test_a_mirror_that_fails_never_fails_the_main_push(env, monkeypatch):
    from ttp import push
    p, repo, origin, mirror = _mirror_setup(env, monkeypatch, [{"remote": "gone", "branch": "dev"},
                                                                {"remote": "mirror", "branch": "dev"},
                                                                {"remote": "nowhere", "branch": "dev"}])
    _git_out(repo, "remote", "add", "gone", str(env["tmp"] / "no-such-repo.git"))
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0
    sha = _git_out(origin, "rev-parse", "proj")
    assert push.last_ff[0].startswith("not mirrored gone/dev: gone cannot be read"), push.last_ff
    assert push.last_ff[1:] == [f"mirror mirror/dev {sha}", "not mirrored nowhere/dev: there is no git remote named nowhere"]
    # mirror() itself raising: the push stands and each mirror says why.
    monkeypatch.setattr(push, "mirror", lambda *a, **k: 1 / 0)
    _commit(repo, "more.txt", "more\n")
    assert push.run(p, repo) == 0 and _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")
    assert push.last_ff[0] == "not mirrored gone/dev: ZeroDivisionError: division by zero", push.last_ff
    # Off by default and when emptied: nothing else moves.
    for off in ([], ""):
        p.set_config("delivery.mirror_remotes", off)
        _commit(repo, f"off{off!r}.txt", "x\n")
        assert push.run(p, repo) == 0 and push.last_ff == []


def test_mirror_remotes_refuses_the_push_branch_and_bad_entries(env, monkeypatch):
    from ttp import push
    from ttp import coordinator as coord
    from ttp.project import config_problems
    p, repo, origin, mirror = _mirror_setup(env, monkeypatch)
    for bad in ([{"remote": "origin", "branch": "proj"}], ["origin/proj"], [{"remote": "mirror", "branch": "a..b"}],
                [{"remote": "-x", "branch": "dev"}], [{"branch": "dev"}], ["dev"], [{"remote": "mirror", "branch": ""}]):
        with pytest.raises(ValueError):
            push.mirror_check(bad, "origin/proj")
        assert config_problems({"delivery": {"push_branch": "origin/proj", "mirror_remotes": bad}}), bad
    assert push.mirror_check('[{"remote": "mirror", "branch": "dev"}, " mirror/dev ", "m2/a/b"]', "origin/proj") == \
        [{"remote": "mirror", "branch": "dev"}, {"remote": "m2", "branch": "a/b"}]
    assert push.mirror(repo, "ab" * 20, [{"remote": "origin", "branch": "proj"}], "origin/proj") == \
        ["not mirrored origin/proj: it is the push branch itself (delivery.push_branch)"]
    # From chat it needs the user's word; turning it off does not.
    ask = {"type": "config_set", "key": "delivery.mirror_remotes", "value": [{"remote": "mirror", "branch": "dev"}]}
    assert "ask_user (blocking restriction)" in coord.apply(p, [ask])[0]
    assert "push branch" in coord.apply(p, [{**ask, "value": ["origin/proj"]}], user_turn=True)[0]
    assert coord.apply(p, [ask], user_turn=True) == []
    assert p.config()["delivery"]["mirror_remotes"] == [{"remote": "mirror", "branch": "dev"}]
    assert coord.apply(p, [{**ask, "value": []}]) == []


def _plugin_commit(path, version, name, text):
    """Commit a file of plugin `p` and set both its manifests to `version`."""
    for m in (".claude-plugin", ".codex-plugin"):
        (path / "plugins" / "p" / m).mkdir(parents=True, exist_ok=True)
        (path / "plugins" / "p" / m / "plugin.json").write_text(json.dumps({"name": "p", "version": version}))
    (path / "plugins" / "p" / name).write_text(text)
    _git_out(path, "add", "plugins")
    _git_out(path, "commit", "-qm", f"p {version}")


def test_push_refuses_a_rebased_change_that_keeps_the_targets_plugin_version(env, monkeypatch, capsys):
    """Two batches both bumped 0.1.0 to 0.1.1: the second rebases cleanly onto the first, but it
    must not land as 0.1.1 too, or an upgrade to strictly newer versions skips it. The guard is
    `delivery.plugin_version_guard`, off by default: not every project's repository holds plugins."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    from ttp.project import config_problems
    assert any("plugin_version_guard" in x for x in config_problems({"delivery": {"plugin_version_guard": "yes"}}))
    assert not any("plugin_version_guard" in x for x in config_problems({"delivery": {"plugin_version_guard": True}}))
    from ttp import push as pushmod
    assert [pushmod.version_guard(d) for d in ({}, None, {"plugin_version_guard": "yes"},
                                              {"plugin_version_guard": True})] == [False, False, False, True]
    p.set_config("delivery.plugin_version_guard", True)
    _plugin_commit(repo, "0.1.0", "base.txt", "base\n")
    _git_out(repo, "push", "-q", "origin", "HEAD:proj")
    _git_out(other, "pull", "-q", "origin", "proj")
    _plugin_commit(other, "0.1.1", "theirs.txt", "theirs\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    moved = _git_out(origin, "rev-parse", "proj")
    _plugin_commit(repo, "0.1.1", "mine.txt", "mine\n")
    assert _ttp_push() == 4
    assert "plugins/p 0.1.1" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "proj") == moved, "a same-version batch was pushed"

    _plugin_commit(repo, "0.1.2", "mine.txt", "mine\n")
    assert _ttp_push() == 0, "bumped past the target's version, it goes through"
    assert _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")

    _commit(repo, "outside.txt", "not a plugin\n")
    assert _ttp_push() == 0, "a change outside plugins/ needs no bump"

    p.set_config("delivery.plugin_version_guard", False)
    _plugin_commit(repo, "0.1.2", "more.txt", "more\n")
    assert _ttp_push() == 0, "with the guard off, a plugin change that keeps the version goes through"
    assert _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")


def test_push_to_main_needs_allow_protected_push_branch_and_keeps_checks_rebase_and_no_force(env, monkeypatch, capsys):
    # main (here also the remote's default branch) is refused by default, with a message that names
    # the opt-in and forbids a raw `git push`. With delivery.allow_protected_push_branch on, the push
    # goes through the same checks and rebase as any other branch; a detached HEAD never does.
    from ttp import coordinator as coord, push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["test ! -f broken.txt"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    before = _git_out(origin, "rev-parse", "main")
    p.set_config("delivery.push_branch", "origin/main")
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 2
    err = capsys.readouterr().err
    assert "refusing to push to origin/main" in err and "delivery.allow_protected_push_branch" in err \
        and "never fall back to a raw `git push`" in err, err
    for off in (None, False, "false", "", 0):
        assert push.allow_protected({"allow_protected_push_branch": off}) is False, off
    assert push.allow_protected(None) is False
    # From chat it needs the user's word; turning it off does not.
    ask = {"type": "config_set", "key": "delivery.allow_protected_push_branch", "value": True}
    assert "ask_user (blocking restriction)" in coord.apply(p, [ask])[0]
    assert "allow_protected_push_branch" not in (p.config().get("delivery") or {})
    assert coord.apply(p, [{**ask, "value": False}]) == []
    assert coord.apply(p, [ask], user_turn=True) == [] and push.allow_protected(p.config()["delivery"])
    # Checks still run and still stop it.
    _commit(repo, "broken.txt", "x\n")
    assert _ttp_push() == 4 and _git_out(origin, "rev-parse", "main") == before
    _git_out(repo, "reset", "-q", "--hard", "HEAD~1")
    # Someone else moved main: the push rebases onto it, never forces.
    _commit(other, "theirs.txt", "theirs\n")
    _git_out(other, "push", "-q", "origin", "HEAD:main")
    moved = _git_out(origin, "rev-parse", "main")
    assert _ttp_push() == 0
    assert _git_out(origin, "rev-parse", "main") == _git_out(repo, "rev-parse", "HEAD") != moved
    assert _git_out(origin, "rev-parse", "main~1") == moved
    assert _git_out(origin, "show", "main:theirs.txt") == "theirs" and _git_out(origin, "show", "main:mine.txt") == "mine"
    # The opt-in never covers a detached HEAD, nor `--own` publishing to main.
    p.set_config("delivery.push_branch", "origin/HEAD")
    assert _ttp_push() == 2 and "refusing to push to origin/HEAD" in capsys.readouterr().err
    p.set_config("delivery.push_branch", "origin/proj")
    _git_out(repo, "checkout", "-q", "-B", "main")
    with pytest.raises(ValueError, match="shared branch"):
        push.own_target(p, repo)


def test_a_push_batch_and_the_queue_publish_to_main_only_with_allow_protected_push_branch(env, monkeypatch):
    from ttp import pushq
    p, repo, origin, other = _push_setup(env, monkeypatch, ["test ! -f broken.txt"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    p.set_config("delivery.push_branch", "origin/main")
    before = _git_out(origin, "rev-parse", "main")
    heads = [_entry(repo, "e1", {"f1.txt": "1\n"})]
    assert "allow_protected_push_branch" in pushq._refusal(p, "origin", "main")
    rc, m = _run_batch(p, _batch_marker(p, heads, target="origin/main"), monkeypatch)
    assert rc == 2 and _statuses(m) == ["refused"] and _git_out(origin, "rev-parse", "main") == before, m
    p.set_config("delivery.allow_protected_push_branch", True)
    assert pushq._refusal(p, "origin", "main", True) == "" and pushq._refusal(p, "origin", "HEAD", True)
    # The checks still run on the batch.
    bad = [_entry(repo, "e2", {"broken.txt": "x\n"})]
    rc, m = _run_batch(p, _batch_marker(p, bad, bid="b2", target="origin/main"), monkeypatch)
    assert _statuses(m) != ["pushed"] and _git_out(origin, "rev-parse", "main") == before, m
    rc, m = _run_batch(p, _batch_marker(p, heads, bid="b3", target="origin/main"), monkeypatch)
    assert rc == 0 and _statuses(m) == ["pushed"], m
    assert _git_out(origin, "show", "main:f1.txt") == "1" and _git_out(origin, "rev-parse", "main~1") == before


def test_push_checks_accept_the_forms_config_set_sends(env):
    sys.path.insert(0, str(RUNTIME))
    try:
        from ttp.push import check_list
    finally:
        sys.path.remove(str(RUNTIME))
    assert check_list('["pytest -q", "make lint"]') == ["pytest -q", "make lint"]
    assert check_list("pytest -q\n\nmake lint\n") == ["pytest -q", "make lint"]
    assert check_list(["pytest -q", " "]) == ["pytest -q"]
    assert check_list(None) == []


def test_a_push_that_waits_past_push_wait_s_exits_75_with_a_probe(env, monkeypatch, capsys):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.push_wait_s", 1)
    run_dir = env["tmp"] / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    from ttp import locks, push
    held = locks.try_take(push.lock_paths(p, "origin", "proj"), "task #7 (run 9)")
    before = _git_out(origin, "rev-parse", "proj")
    _commit(repo, "mine.txt", "mine\n")
    t0 = time.time()
    assert _ttp_push() == 75
    assert 1 <= time.time() - t0 < 15
    err = capsys.readouterr().err
    assert "task #7 (run 9)" in err and "retry_when: " in err, err
    assert _git_out(origin, "rev-parse", "proj") == before
    (w,) = json.loads((run_dir / "lock_waits.json").read_text()).values()
    assert w["end"] - w["start"] >= 1 and locks.waited(run_dir) >= 1, "the wait must stop the run's clock"
    # The printed probe works where the harness runs it: in the project root, without the run's env.
    probe = err.split("retry_when: ")[1].strip()
    assert probe.endswith("ttp push --free")
    penv = {k: v for k, v in os.environ.items() if not k.startswith("TTP_") or k == "TTP_HOME"}
    run_probe = lambda: subprocess.run(probe, shell=True, cwd=str(p.root), env=penv, capture_output=True).returncode
    assert run_probe() == 1
    held.close()
    assert run_probe() == 0


def test_push_free_tells_whether_a_push_holds_the_branch(env, monkeypatch):
    p, repo, origin, other = _push_setup(env, monkeypatch, [])
    from ttp import locks, push
    paths = push.lock_paths(p, "origin", "proj")
    assert _ttp("push", "--free") == 0 and not any(x.exists() for x in paths), "--free must not create the lock"
    held = locks.try_take(paths, "someone")
    assert _ttp("push", "--free") == 1
    # The harness runs the probe in the project root, which need not have the pushing repo's remotes.
    elsewhere = env["tmp"] / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert _ttp("push", "--free") == 1
    held.close()
    assert _ttp("push", "--free") == 0


def test_a_detached_push_returns_at_once_and_its_marker_reports_the_pushed_sha_and_version(env, monkeypatch, capsys):
    started = env["tmp"] / "started"
    gate = env["tmp"] / "gate"
    p, repo, origin, other = _bump_setup(env, monkeypatch, [_gated_check(started, gate)])
    pids = []
    try:
        _commit(repo, "plugins/p/rt/x.py", "x = 1\n")
        t0 = time.time()
        rc, marker, probe = _detach(capsys)
        pids += [json.loads(marker.read_text())["pid"]] if marker else []
        assert rc == 0 and time.time() - t0 < 10, "--detach must return at once"
        assert marker.parent == p.state / "pushes" and probe.endswith(f"ttp push --result {marker}")
        m = json.loads(marker.read_text())
        assert m["status"] == "running" and m["pid"] > 0 and m["target"] == "origin/proj" and m["log"]
        assert (m["remote"], m["branch"]) == ("origin", "proj")   # what its claim is checked against
        deadline = time.time() + 30
        while not started.exists() and time.time() < deadline:
            time.sleep(0.05)
        r = _probe(p, probe)
        assert r.returncode == 1 and "still running" in r.stdout, r
        assert _ttp("push", "--free") == 1, "the detached push holds the branch's turn"
        from ttp import release
        assert release.push_in_flight(p), "an upgrade must not swap the runtime under a detached push"
        gate.touch()
        r = _probe_until_done(p, probe)
        pushed = _git_out(origin, "rev-parse", "proj")
        assert r.returncode == 0 and f"pushed {pushed}" in r.stdout and "version 0.1.1" in r.stdout, r
        m = json.loads(marker.read_text())
        assert (m["status"], m["exit"], m["sha"], m["version"]) == ("pushed", 0, pushed, "0.1.1")
        assert m["ended"] >= m["started"] and "bumped" in pathlib.Path(m["log"]).read_text()
        _wait_gone(m["pid"], time.time() + 10)   # the outcome is written a moment before the lock goes
        assert _ttp("push", "--free") == 0 and not release.push_in_flight(p)
        from ttp import push
        assert not push._run_lock(marker).exists(), "a finished push leaves no lock file behind"
        # A month later the next detached push tidies the finished marker and its log away.
        month = time.time() - push.KEEP_S - 60
        os.utime(marker, (month, month))
        _commit(repo, "plugins/p/rt/y.py", "y = 1\n")
        gate.unlink()
        rc, second, probe2 = _detach(capsys)
        pids += [json.loads(second.read_text())["pid"]] if second else []
        assert rc == 0 and not marker.exists() and not pathlib.Path(m["log"]).exists() and second.exists()
        gate.touch()
        assert _probe_until_done(p, probe2).returncode == 0
    finally:
        _release_detached(gate, pids)


def test_push_picks_a_branchs_real_base_over_the_push_target_and_other_task_branches(env, monkeypatch):
    from ttp import push
    p, repo, origin = _foreign_setup(env, monkeypatch)
    real = lambda: push.real_base(p, repo, "origin", "ttp/t48-int",
                                  _git_out(repo, "symbolic-ref", "--short", "HEAD"))
    # Based on main, with ttp/t47-near (also main-based) and t49 present: main, never a ttp/* branch.
    _git_out(repo, "checkout", "-q", "-b", "ttp/t60-h3", "origin/main")
    _commit(repo, "mine.txt", "mine\n")
    assert real() == ("origin/main", 1)
    assert push.foreign_base(p, repo, "origin", "ttp/t48-int", "ttp/t60-h3") == ("origin/main", 1)
    # Its upstream (a colleague's branch) when it tracks one.
    _git_out(repo, "checkout", "-q", "-b", "ttp/t61-x", "--track", "origin/someone/feature")
    _commit(repo, "x.txt", "x\n")
    assert real() == ("origin/someone/feature", 1)
    # Based on the push target or on base_ref: the configured flow, as before.
    _git_out(repo, "checkout", "-q", "-b", "ttp/t62-y", "origin/ttp/t48-int")
    _commit(repo, "y.txt", "y\n")
    assert real() == ("origin/ttp/t48-int", 1)
    assert push.foreign_base(p, repo, "origin", "ttp/t48-int", "ttp/t62-y") is None
    _git_out(repo, "checkout", "-q", "-b", "ttp/t63-z", "origin/rt")
    _commit(repo, "z.txt", "z\n")
    assert push.foreign_base(p, repo, "origin", "ttp/t48-int", "ttp/t63-z") is None


def test_push_keeps_a_branch_on_the_push_target_that_merged_in_another_branch(env, monkeypatch):
    """A branch that descends from the push target's tip but merged in main (not base_ref), with main
    the candidate it has the fewest commits over, is on the flow: never refused as foreign."""
    from ttp import push
    p, repo, origin = _foreign_setup(env, monkeypatch)
    _git_out(repo, "checkout", "-q", "--detach", "origin/main")
    _commit(repo, "m3.txt", "m3\n")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git_out(repo, "fetch", "-q", "origin")
    _git_out(repo, "checkout", "-q", "-b", "ttp/t64-merged", "origin/ttp/t48-int")
    _git_out(repo, "merge", "-q", "--no-edit", "origin/main")
    _commit(repo, "w.txt", "w\n")
    assert push.real_base(p, repo, "origin", "ttp/t48-int", "ttp/t64-merged")[0] == "origin/main"
    assert push.foreign_base(p, repo, "origin", "ttp/t48-int", "ttp/t64-merged") is None


def test_a_detached_push_starts_promptly_when_the_base_detection_fetch_times_out(env, monkeypatch, capsys):
    """foreign_base runs in `ttp push --detach`'s foreground: its fetch of the push target is bounded,
    prompt-free and stdin-free, and a timeout skips base detection (as a failed fetch does), so the
    detached push still starts at once."""
    from ttp import push
    p, repo, origin = _foreign_setup(env, monkeypatch)
    target = _git_out(origin, "rev-parse", "ttp/t48-int")
    monkeypatch.setenv("TTP_TASK", "60")
    _git_out(repo, "checkout", "-q", "-b", "ttp/t60-h3", "origin/main")
    _commit(repo, "mine.txt", "mine\n")
    real, bounded, unbounded = subprocess.run, [], []

    def stalled(cmd, *a, **kw):
        if "fetch" in cmd and cmd[-1].endswith(":refs/remotes/origin/ttp/t48-int"):
            if kw.get("timeout") is None:
                unbounded.append(cmd)
                return subprocess.CompletedProcess(cmd, 1, "", "would hang")
            bounded.append(kw)
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return real(cmd, *a, **kw)
    monkeypatch.setattr(push.subprocess, "run", stalled)
    assert push.foreign_base(p, repo, "origin", "ttp/t48-int", "ttp/t60-h3") is None
    capsys.readouterr()
    start = time.monotonic()
    rc, marker, probe = _detach(capsys)
    assert rc == 0 and marker is not None and time.monotonic() - start < 30
    assert not unbounded, unbounded
    kw = bounded[0]
    assert kw["timeout"] == push.REACH_FETCH_S <= 60 and kw["stdin"] == subprocess.DEVNULL, kw
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0", kw
    monkeypatch.setattr(push.subprocess, "run", real)
    assert _probe_until_done(p, probe).returncode != 1
    assert _git_out(origin, "rev-parse", "ttp/t48-int") == target


def test_push_refuses_a_worktree_on_another_tasks_branch_at_once(env, monkeypatch, capsys):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.code_tasks_may_push", True)   # code tasks land only with it on (code_run_landing)
    _git_out(repo, "checkout", "-q", "-b", "ttp/t149-fix")
    _commit(repo, "mine.txt", "mine\n")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/ttp/t149-fix")   # published with --own
    shared = _git_out(origin, "rev-parse", "proj")
    worker = p.db.add_task("device check", kind="work")
    monkeypatch.setenv("TTP_TASK", str(worker))
    for args in (["push"], ["push", "--detach"]):
        assert _ttp(*args) == 2, args
        err = capsys.readouterr().err
        assert "task #149's branch ttp/t149-fix" in err and f"ttp/t{worker}-" in err, err
    assert not (p.state / "pushes").exists() and _git_out(origin, "rev-parse", "proj") == shared
    # The review of that change, a task built on it, or one that carries it as its branch may.
    review = p.db.add_task("review", kind="review")
    after = p.db.add_task("after", kind="code", depends_on=[149])
    carrier = p.db.add_task("carrier", kind="code", branch="ttp/t149-fix")
    for tid in (review, after, carrier):
        monkeypatch.setenv("TTP_TASK", str(tid))
        assert push.resolve(p, repo) == ("origin", "proj", False), tid
    monkeypatch.delenv("TTP_TASK")          # outside a run: the configured target as before
    assert push.resolve(p, repo) == ("origin", "proj", False)


def test_push_from_a_tasks_own_branch_lands_only_when_its_spec_asks_or_land_says_so(env, monkeypatch, capsys):
    # Replays the upstream note: from a task worktree on ttp/t<id>-..., whose spec allows pushing only that
    # branch, `ttp push --detach` targeted the push branch. It is refused at once, naming --own and --land.
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.code_tasks_may_push", True)   # code tasks land only with it on (code_run_landing)
    shared = _git_out(origin, "rev-parse", "proj")
    own_only = p.db.add_task("fix", kind="code", spec="Fix it. Push only your own ttp/* branch; never open PRs.")
    _git_out(repo, "checkout", "-q", "-b", f"ttp/t{own_only}-fix")
    _commit(repo, "mine.txt", "mine\n")
    monkeypatch.setenv("TTP_TASK", str(own_only))
    for args in (["push"], ["push", "--detach"]):
        assert _ttp(*args) == 2, args
        captured = capsys.readouterr()
        assert "pushing" not in captured.out and "origin/proj" not in captured.out, captured.out
        assert "does not ask it to land on origin/proj" in captured.err and "ttp push --own --detach" in captured.err \
            and "ttp push --land --detach" in captured.err, captured.err
    assert not (p.state / "pushes").exists() and _git_out(origin, "rev-parse", "proj") == shared
    # --own publishes the own branch, never the push branch.
    assert push.resolve(p, repo, True) == ("origin", f"ttp/t{own_only}-fix", True)
    # --land is the explicit target: it lands, also in the detached process (QUEUE_ENV).
    assert push.LAND_ENV in push.QUEUE_ENV
    monkeypatch.setenv(push.LAND_ENV, "1")
    assert push.resolve(p, repo) == ("origin", "proj", False)
    monkeypatch.delenv(push.LAND_ENV)
    assert _ttp("push", "--land", "--own") != 0
    _record_checks_pass(p, own_only, repo)   # a code task lands only tested work (untested_landing)
    # A spec with a landing step lands as before; a `no_push:` line or a negated step never counts.
    for spec, lands in (("Then land it with `ttp push`.", True), ("Land it on the push branch.", True),
                        ("Run `ttp push`.\nno_push: experiment only", False), ("Do not run `ttp push`.", False)):
        p.db.conn.execute("UPDATE tasks SET spec=? WHERE id=?", (spec, own_only))
        p.db.conn.commit()
        try:
            got = push.resolve(p, repo)
        except ValueError as e:
            got = str(e)
        assert (got == ("origin", "proj", False)) is lands, (spec, got)
    # A pr_branch label naming the push branch is an explicit target; with own pushes off the push
    # branch is the only one. Both land without a landing step in the spec.
    p.db.conn.execute("UPDATE tasks SET spec=? WHERE id=?", ("Fix it.", own_only))
    p.db.conn.commit()
    with pytest.raises(ValueError, match="does not ask it to land"):
        push.resolve(p, repo)
    p.db.conn.execute("UPDATE tasks SET labels=? WHERE id=?", ('["pr_branch:proj"]', own_only))
    p.db.conn.commit()
    assert push.resolve(p, repo) == ("origin", "proj", False)
    p.db.conn.execute("UPDATE tasks SET labels=? WHERE id=?", ("[]", own_only))
    p.db.conn.commit()
    p.set_config("delivery.own_push", "off")
    assert push.resolve(p, repo) == ("origin", "proj", False)
    p.set_config("delivery.own_push", "on")
    # A review lands from its own branch as before; outside a run nothing is gated.
    review = p.db.add_task("review", kind="review")
    _git_out(repo, "checkout", "-q", "-b", f"ttp/t{review}-review")
    monkeypatch.setenv("TTP_TASK", str(review))
    assert push.resolve(p, repo) == ("origin", "proj", False)
    monkeypatch.delenv("TTP_TASK")
    assert push.resolve(p, repo) == ("origin", "proj", False)


def _record_checks_pass(p, tid, repo, head=None, passed=True):
    """A `ttp checks` record in a run of task `tid` on `head` (default HEAD) of worktree `repo`."""
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(?,?,?,?,?)",
                 (tid, "worker", "fake", time.time(), "running"))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "checks.json").write_text(json.dumps({
        "head": head or _git_out(repo, "rev-parse", "HEAD"), "worktree": str(repo), "passed": passed,
        "commands": ["true"], "skipped": [], "ts": time.time()}))
    return run_dir


def test_a_code_workers_plain_push_lands_only_tested_work_unless_land_overrides_it(env, monkeypatch, capsys, tmp_path):
    # Replays the upstream note: a code task's worker ran plain `ttp push --detach` and it rebased the
    # task branch onto the push branch and pushed untested work there. A code task whose spec says to
    # land now needs `ttp checks` passed on HEAD; `--land` overrides it, explicitly and logged.
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.code_tasks_may_push", True)   # code tasks land only with it on (code_run_landing)
    shared = _git_out(origin, "rev-parse", "proj")
    tid = p.db.add_task("fix", kind="code", spec="Fix it, then land it with `ttp push`.")
    _git_out(repo, "checkout", "-q", "-b", f"ttp/t{tid}-fix")
    _commit(repo, "mine.txt", "mine\n")
    monkeypatch.setenv("TTP_TASK", str(tid))
    for args in (["push"], ["push", "--detach"]):
        assert _ttp(*args) == 2, args
        err = capsys.readouterr().err
        assert "`ttp checks` has not passed on this commit" in err and "ttp push --own --detach" in err \
            and "ttp push --land --detach" in err, err
    assert not (p.state / "pushes").exists() and _git_out(origin, "rev-parse", "proj") == shared
    # A failed record, a pass on another commit or a dirty tree is not tested work.
    _record_checks_pass(p, tid, repo, passed=False)
    with pytest.raises(ValueError, match="has not passed"):
        push.resolve(p, repo)
    _record_checks_pass(p, tid, repo, head=_git_out(repo, "rev-parse", "HEAD~1"))
    with pytest.raises(ValueError, match="has not passed"):
        push.resolve(p, repo)
    _record_checks_pass(p, tid, repo)
    assert push.resolve(p, repo) == ("origin", "proj", False)
    (repo / "mine.txt").write_text("changed\n")
    with pytest.raises(ValueError, match="has not passed"):
        push.resolve(p, repo)
    _git_out(repo, "checkout", "-q", "--", "mine.txt")
    # A new commit after the pass needs its own.
    _commit(repo, "more.txt", "more\n")
    with pytest.raises(ValueError, match="has not passed"):
        push.resolve(p, repo)
    # --land is the explicit override: it lands untested, and says so on stderr and in the run's notes.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.setenv(push.LAND_ENV, "1")
    assert push.resolve(p, repo) == ("origin", "proj", False)
    assert "--land: task #%d lands its own branch" % tid in capsys.readouterr().err
    assert "explicit override of the landing guard" in (run_dir / "progress.md").read_text()
    monkeypatch.delenv(push.LAND_ENV)
    # With own pushes off the refusal does not offer --own.
    p.set_config("delivery.own_push", "off")
    with pytest.raises(ValueError) as e:
        push.resolve(p, repo)
    assert "--own" not in str(e.value) and "ttp checks" in str(e.value)
    p.set_config("delivery.own_push", "on")
    # Unchanged: a review and other task kinds land untested as before (the push runs the checks
    # itself), and the push queue (no task in its environment) is not gated.
    for kind in ("review", "work"):
        other_tid = p.db.add_task(kind, "Land it with `ttp push`.", kind=kind)
        _git_out(repo, "checkout", "-q", "-B", f"ttp/t{other_tid}-x")
        monkeypatch.setenv("TTP_TASK", str(other_tid))
        assert push.resolve(p, repo) == ("origin", "proj", False), kind
    monkeypatch.delenv("TTP_TASK")
    assert push.resolve(p, repo) == ("origin", "proj", False)


def test_push_defaults_to_the_tasks_own_branch_on_the_remote_without_a_push_branch(env, monkeypatch, capsys):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    shared = _git_out(origin, "rev-parse", "proj")
    p.set_config("delivery.push_branch", "")
    _git_out(repo, "checkout", "-q", "-b", "ttp/t60-exp")
    _commit(repo, "mine.txt", "mine\n")
    monkeypatch.setenv("TTP_TASK", "60")
    # Not on the remote yet: nothing to default to, the usual refusal.
    assert _ttp("push") == 2 and "set delivery.push_branch" in capsys.readouterr().err
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/ttp/t60-exp")
    _commit(repo, "more.txt", "more\n")
    head = _git_out(repo, "rev-parse", "HEAD")
    assert push.resolve(p, repo) == ("origin", "ttp/t60-exp", True)
    rc = _ttp("push", "--detach")
    out = capsys.readouterr().out
    probe = next(ln.split(": ", 1)[1] for ln in out.splitlines() if ln.startswith("retry_when: "))
    assert rc == 0 and "origin/ttp/t60-exp" in out
    r = _probe_until_done(p, probe)
    assert r.returncode == 0 and f"pushed {head} to origin/ttp/t60-exp" in r.stdout, r
    assert _git_out(origin, "rev-parse", "ttp/t60-exp") == head and _git_out(origin, "rev-parse", "proj") == shared
    # A configured push branch still wins over the own branch.
    p.set_config("delivery.push_branch", "origin/proj")
    assert push.resolve(p, repo) == ("origin", "proj", False)
    assert _ttp("push") == 0 and _git_out(origin, "rev-parse", "proj") == head

def test_a_dead_detached_push_reads_as_failed_and_frees_the_branch(env, monkeypatch, capsys):
    started = env["tmp"] / "started"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"touch {started}; sleep 60"])
    _commit(repo, "mine.txt", "mine\n")
    rc, marker, probe = _detach(capsys)
    pid = json.loads(marker.read_text())["pid"]
    try:
        deadline = time.time() + 30
        while not started.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert started.exists() and _probe(p, probe).returncode == 1
        assert _ttp("push", "--free") == 1
        os.kill(pid, signal.SIGKILL)     # a crash or a reboot: no outcome is written
        deadline = time.time() + 10
        while _ttp("push", "--free") != 0 and time.time() < deadline:
            time.sleep(0.05)
        assert _ttp("push", "--free") == 0, "a dead push must free the branch"
        r = _probe(p, probe)
        assert r.returncode == 0 and "not pushed" in r.stdout and "without writing an outcome" in r.stdout, r
        m = json.loads(marker.read_text())
        assert m["status"] == "failed" and m["exit"] is None and m["reason"]
        from ttp import push
        assert not push._run_lock(marker).exists()
        assert _probe(p, probe).returncode == 0
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


def _wait_free(marker, timeout=10):
    from ttp import push, locks
    deadline = time.time() + timeout
    while not locks.any_free([push._run_lock(marker)]) and time.time() < deadline:
        time.sleep(0.05)


def test_a_detached_push_killed_with_its_launcher_leaves_a_log_and_says_it_never_began(env, monkeypatch, capsys):
    """A sandbox that ends the command's processes kills the push before it logs a line. The log
    must still say what happened and the verdict must name the early death, also for the daemon."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "mine.txt", "mine\n")
    rc, marker, probe = _detach(capsys)
    pid = json.loads(marker.read_text())["pid"]
    os.kill(pid, signal.SIGKILL)         # the sandbox ends with the command: no outcome, no output
    _wait_free(marker)
    from ttp import push
    assert pathlib.Path(json.loads(marker.read_text())["log"]).read_text().strip(), "the log is never empty"
    push.tend(p)                         # the daemon records the verdict without waiting for the probe
    m = json.loads(marker.read_text())
    assert m["status"] == "failed" and m["exit"] is None and "before it began" in m["reason"], m
    r = _probe(p, probe)
    assert r.returncode == 0 and "without writing an outcome before it began" in r.stdout, r
    assert "starting the push process" in r.stdout, "the log tail comes with the verdict"


def test_a_detached_push_from_a_sandboxed_command_is_queued_and_the_daemon_starts_it(env, monkeypatch, capsys):
    """Inside a PID namespace other than the run's, nothing started survives the command: the push
    is queued and the daemon starts it, with the run's task for `--own`."""
    from ttp import push
    if not push.pid_ns():
        pytest.skip("no /proc/self/ns/pid")
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "checkout", "-q", "-b", "ttp/t7-exp")
    _commit(repo, "mine.txt", "mine\n")
    monkeypatch.setenv("TTP_TASK", "7")
    monkeypatch.setenv("TTP_PIDNS", "pid:[1]")   # the run's namespace, not this one
    rc = _ttp("push", "--own", "--detach")
    out = capsys.readouterr().out
    marker = pathlib.Path(next(ln.split(": ", 1)[1] for ln in out.splitlines() if ln.startswith("marker: ")))
    probe = next(ln.split(": ", 1)[1] for ln in out.splitlines() if ln.startswith("retry_when: "))
    m = json.loads(marker.read_text())
    assert rc == 0 and "queued" in out and m["status"] == "queued" and "pid" not in m, out
    assert m["env"]["TTP_TASK"] == "7" and "TTP_PIDNS" not in m["env"]
    r = _probe(p, probe)
    assert r.returncode == 1 and "queued for the daemon" in r.stdout, r
    assert [x["id"] for x in push.running(p)] == [m["id"]], "its worktree stays while it is queued"
    monkeypatch.delenv("TTP_TASK")       # the daemon has none: the marker carries it
    monkeypatch.delenv("TTP_PIDNS")
    push.tend(p)
    assert json.loads(marker.read_text())["status"] in ("running", "pushed")
    r = _probe_until_done(p, probe)
    head = _git_out(repo, "rev-parse", "HEAD")
    assert r.returncode == 0 and f"pushed {head} to origin/ttp/t7-exp" in r.stdout, r
    assert _git_out(origin, "rev-parse", "ttp/t7-exp") == head


def test_a_queued_push_the_daemon_never_starts_reads_as_failed(env, monkeypatch, capsys):
    from ttp import push
    if not push.pid_ns():
        pytest.skip("no /proc/self/ns/pid")
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "mine.txt", "mine\n")
    monkeypatch.setenv("TTP_PIDNS", "pid:[1]")
    rc, marker, probe = _detach(capsys)
    m = json.loads(marker.read_text())
    m["queued"] -= push.QUEUED_S + 60
    marker.write_text(json.dumps(m))
    r = _probe(p, probe)
    assert r.returncode == 0 and "did not start the queued push" in r.stdout, r
    push.tend(p)
    assert json.loads(marker.read_text())["status"] == "failed", "a failed push is never started late"
    assert _git_out(origin, "rev-parse", "proj") != _git_out(repo, "rev-parse", "HEAD")


def test_a_push_that_died_in_a_reboot_says_so(env, monkeypatch):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    marker = p.state / push.DETACHED / "t1-x.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"id": "t1-x", "status": "running", "pid": 1, "boot": "an-earlier-boot",
                                  "alive": time.time(), "log": str(marker.with_suffix(".log"))}))
    push.tend(p)
    assert "the host rebooted while it ran" in json.loads(marker.read_text())["reason"]


# Someone else's next commit to ttp/t7-exp, pushed from the check (run $n) while the push checks it.
_THEIRS = ('echo "$n" > "$OTHER/{file}" && git -C "$OTHER" add {file} && git -C "$OTHER" commit -qm "theirs $n" '
           '&& git -C "$OTHER" push -q origin t7:refs/heads/ttp/t7-exp\n')


def _own_moving_setup(env, monkeypatch, script, retries=None):
    """A `ttp push --own --detach` of ttp/t7-exp whose check runs `script` with $n its run count and
    $OTHER a clone that holds someone else's work on that branch."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    count, chk = env["tmp"] / "runs", env["tmp"] / "check.sh"
    chk.write_text(f'n=$(( $(cat {count} 2>/dev/null || echo 0) + 1 )); echo $n > {count}\n' + script)
    p.set_config("delivery.push_checks", [f"OTHER={other} sh {chk}"])
    if retries is not None:
        p.set_config("delivery.push_retries", retries)
    _git_out(repo, "checkout", "-q", "-b", "ttp/t7-exp")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/ttp/t7-exp")
    _git_out(other, "fetch", "-q", "origin", "ttp/t7-exp")
    _git_out(other, "checkout", "-q", "-b", "t7", "FETCH_HEAD")
    monkeypatch.setenv("TTP_TASK", "7")
    _commit(repo, "mine.txt", "mine\n")
    return p, repo, origin, other, count


def _own_detach_until_done(p, capsys):
    """`ttp push --own --detach`, then its probe until the push ends: (probe result, marker as written)."""
    assert _ttp("push", "--own", "--detach") == 0
    out = capsys.readouterr().out
    lines = dict(ln.split(": ", 1) for ln in out.splitlines() if ln.startswith(("marker: ", "retry_when: ")))
    marker = pathlib.Path(lines["marker"])
    r = _probe_until_done(p, lines["retry_when"])
    m = json.loads(marker.read_text())
    _wait_gone(m["pid"], time.time() + 10)
    return r, json.loads(marker.read_text())


def test_a_detached_own_push_refused_as_not_a_fast_forward_rebases_checks_again_and_pushes(env, monkeypatch, capsys):
    """The branch moved on during the checks: with no setting, the push rebases onto the new tip, runs
    the checks again on the rebased commit and pushes it, never forced; the marker names the retry."""
    p, repo, origin, other, count = _own_moving_setup(
        env, monkeypatch, 'if [ "$n" -eq 1 ]; then\n' + _THEIRS.format(file="theirs.txt") + 'fi\n')
    r, m = _own_detach_until_done(p, capsys)
    head = _git_out(repo, "rev-parse", "HEAD")
    assert r.returncode == 0 and f"pushed {head} to origin/ttp/t7-exp" in r.stdout, r
    assert "attempt 2 of at most 3, retried after non_fast_forward" in r.stdout, r
    assert _git_out(origin, "rev-parse", "ttp/t7-exp") == head
    assert subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", _git_out(other, "rev-parse", "t7"),
                           head]).returncode == 0, "their commit is kept under ours"
    assert count.read_text().strip() == "2", "the rebased commit is checked before it is pushed"
    assert [x["cause"] for x in m["retries"]] == ["non_fast_forward"] and m["attempt"] == 2 and m["exit"] == 0


def test_a_detached_own_push_stops_on_a_rebase_conflict_after_a_non_fast_forward(env, monkeypatch, capsys):
    p, repo, origin, other, count = _own_moving_setup(
        env, monkeypatch, 'if [ "$n" -eq 1 ]; then\n' + _THEIRS.format(file="mine.txt") + 'fi\n')
    before = _git_out(repo, "rev-parse", "HEAD")
    r, m = _own_detach_until_done(p, capsys)
    assert r.returncode == 0 and "not pushed (non_fast_forward: rebase onto origin/ttp/t7-exp" in r.stdout, r
    assert m["exit"] == 3 and "conflicts" in m["reason"] and not m.get("retries")
    assert _git_out(repo, "rev-parse", "HEAD") == before and not _git_out(repo, "status", "--porcelain")
    assert _git_out(origin, "rev-parse", "ttp/t7-exp") == _git_out(other, "rev-parse", "t7")


def test_a_detached_own_push_stops_when_the_check_fails_on_the_rebased_commit(env, monkeypatch, capsys):
    p, repo, origin, other, count = _own_moving_setup(
        env, monkeypatch, 'if [ "$n" -eq 1 ]; then\n' + _THEIRS.format(file="theirs.txt")
        + 'else\necho fails-on-the-rebase; exit 1\nfi\n')
    r, m = _own_detach_until_done(p, capsys)
    assert r.returncode == 0 and "not pushed" in r.stdout and "check failed" in r.stdout, r
    assert m["exit"] == 4 and [x["cause"] for x in m["retries"]] == ["non_fast_forward"]
    assert _git_out(origin, "rev-parse", "ttp/t7-exp") == _git_out(other, "rev-parse", "t7")


def test_a_detached_own_push_retries_a_non_fast_forward_at_most_push_retries_times(env, monkeypatch, capsys):
    p, repo, origin, other, count = _own_moving_setup(
        env, monkeypatch, _THEIRS.format(file="theirs.txt"), retries=1)
    r, m = _own_detach_until_done(p, capsys)
    assert r.returncode == 0 and "not pushed (non_fast_forward" in r.stdout and "already retried 1 time(s)" in r.stdout, r
    assert m["exit"] == 6 and len(m["retries"]) == 1 and count.read_text().strip() == "2"
    assert _git_out(origin, "rev-parse", "ttp/t7-exp") == _git_out(other, "rev-parse", "t7")


def test_a_detached_push_cut_by_a_reboot_is_queued_again_and_pushes_within_its_retries(env, monkeypatch, capsys):
    """A reboot ends the push with no outcome: the daemon queues it again and starts it, model-free,
    instead of recording it as failed; once its retries are spent it is recorded as failed, saying so."""
    from ttp import push
    started, gate = env["tmp"] / "started", env["tmp"] / "gate"
    p, repo, origin, other = _push_setup(env, monkeypatch, [_gated_check(started, gate)])
    _commit(repo, "mine.txt", "mine\n")
    rc, marker, probe = _detach(capsys)
    pids = [json.loads(marker.read_text())["pid"]]
    try:
        deadline = time.time() + 30
        while not started.exists() and time.time() < deadline:
            time.sleep(0.05)
        os.killpg(pids[0], signal.SIGKILL)   # the reboot: no outcome written
        _wait_gone(pids[0], time.time() + 10)
        _wait_free(marker)
        m = json.loads(marker.read_text())
        marker.write_text(json.dumps({**m, "boot": "an-earlier-boot"}))
        gate.touch()
        lines = push.tend(p)
        assert any("requeued push marker" in ln and "retried after reboot" in ln for ln in lines), lines
        m = json.loads(marker.read_text())
        pids.append(m["pid"])
        assert m["status"] in ("running", "pushed") and [x["cause"] for x in m["retries"]] == ["reboot"]
        r = _probe_until_done(p, probe)
        head = _git_out(repo, "rev-parse", "HEAD")
        assert r.returncode == 0 and f"pushed {head} to origin/proj" in r.stdout, r
        assert "attempt 2 of at most 3, retried after reboot" in r.stdout, r
        _wait_gone(pids[1], time.time() + 10)
        # Spent: two reboots already retried, the third is recorded as failed and names the bound.
        spent = marker.with_name("t1-spent.json")
        spent.write_text(json.dumps({**json.loads(marker.read_text()), "id": "t1-spent", "status": "running",
                                     "boot": "an-earlier-boot", "retries": [{"cause": "reboot"}] * 2}))
        push.tend(p)
        m = json.loads(spent.read_text())
        assert m["status"] == "failed" and "rebooted" in m["reason"] and "reboot: already retried 2" in m["reason"], m
    finally:
        _release_detached(gate, pids)


def test_ttp_status_shows_a_detached_push_that_retried_with_its_cause_and_attempt(env):
    """A running or queued detached push that has retried shows in `ttp status` with its attempt and
    causes; one that has not retried, or has finished, adds no line."""
    from ttp import push
    from ttp.cli import status_text
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True)
    now = time.time()
    base = {"queued": now, "target": "origin/proj", "retry_max": 2, "log": str(folder / "x.log")}
    for rid, extra in {"t5-retried": {"status": "queued", "task": 5, "retries": [{"cause": "reboot"}]},
                       "t6-first": {"status": "queued", "task": 6},
                       "t7-done": {"status": "pushed", "task": 7, "retries": [{"cause": "non_fast_forward"}]}}.items():
        (folder / f"{rid}.json").write_text(json.dumps({**base, "id": rid, **extra}))
    text = status_text(p)
    assert "push t5-retried queued (task #5, origin/proj): attempt 2 of at most 3, retried after reboot" in text, text
    assert "t6-first" not in text and "t7-done" not in text, text


@pytest.mark.parametrize("retries", [3, "reboot", {"cause": "reboot"}, None, [1, {"cause": "reboot"}]])
def test_a_marker_whose_retries_is_not_a_list_breaks_neither_the_tick_nor_the_probe(env, capsys, retries):
    """`retries` damaged or hand-edited (a number, a string, a dict): tend never raises out of the
    daemon's tick, the probe answers with a verdict and status still renders."""
    from ttp import push
    from ttp.cli import status_text
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True)
    log = folder / "t1-bad.log"
    log.write_text("")
    for rid, status in (("t1-bad", "running"), ("t2-bad", "failed")):
        (folder / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "status": status, "pid": 1, "boot": "an-earlier-boot", "alive": time.time(),
             "log": str(log), "retry_max": 2, "retries": retries}))
    push.tend(p)
    status_text(p)
    for rid in ("t1-bad", "t2-bad"):
        assert push.result(folder / f"{rid}.json") == 0
        assert json.loads((folder / f"{rid}.json").read_text())["status"] == "failed"
    assert "not pushed" in capsys.readouterr().out


def test_a_malformed_queued_push_marker_is_recorded_as_failed_and_the_tick_goes_on(env):
    """A queued marker the daemon cannot use (a non-numeric queued time, no repo or log, an env that
    is not a dict) is recorded as a failed push with the reason, and the rest of the tick still runs."""
    from ttp import push
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True)
    now = time.time()
    bad = {"a-queued": {"queued": "soon"},
           "b-repo": {"queued": now, "log": str(folder / "b-repo.log")},
           "c-log": {"queued": now, "repo": str(p.root)},
           "d-env": {"queued": now, "repo": str(p.root), "log": str(folder / "d-env.log"), "env": ["X=1"]}}
    for rid, m in bad.items():
        from ttp import checkdirs
        (folder / f"{rid}.json").write_text(json.dumps({"id": rid, "status": "queued",
                                                     "check_run_dir": str(checkdirs.new(p.state)), **m}))
    tid = p.db.add_task("after the pushes", "s", kind="work", tier="light", origin="user")
    d.tick()
    assert p.db.q("SELECT id FROM runs WHERE task=?", (tid,)), "a malformed push marker stopped the tick"
    for rid in bad:
        m = json.loads((folder / f"{rid}.json").read_text())
        assert m["status"] == "failed" and m["exit"] is None and "malformed" in m["reason"], m
    d.tick()                             # recorded once: never retried or raised again
    assert all(json.loads((folder / f"{rid}.json").read_text())["status"] == "failed" for rid in bad)


def test_the_push_result_probe_reports_a_malformed_queued_marker_as_failed(env, capsys):
    """`ttp push --result` on a queued marker with a non-numeric queued time gives a verdict, not a traceback."""
    from ttp import push
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True)
    marker = folder / "x.json"
    marker.write_text(json.dumps({"id": "x", "status": "queued", "queued": "soon"}))
    assert push.result(marker) == 0
    assert "not pushed" in capsys.readouterr().out and "malformed" in json.loads(marker.read_text())["reason"]


def test_the_push_result_probe_reports_a_malformed_marker_it_cannot_rewrite(env, capsys, monkeypatch):
    """When the failed verdict cannot be written back, the probe still reports it, not a traceback."""
    from ttp import push
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True)
    marker = folder / "x.json"
    marker.write_text(json.dumps({"id": "x", "status": "queued", "queued": "soon"}))
    def unwritable(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(push, "write_json", unwritable)
    assert push.result(marker) == 0
    out = capsys.readouterr().out
    assert "not pushed (malformed marker" in out, out


def test_the_push_result_probe_reports_a_running_marker_with_a_bad_start_as_failed(env, capsys):
    """A running marker whose start time is not a number gets a verdict, not a traceback."""
    from ttp import locks, push
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True)
    marker = folder / "x.json"
    marker.write_text(json.dumps({"id": "x", "status": "running", "started": "soon", "pid": 1}))
    held = locks.try_take([push._run_lock(marker)], "detached push x")   # it really runs
    try:
        assert push.result(marker) == 0
    finally:
        held.close()
    out = capsys.readouterr().out
    assert "not pushed (malformed marker" in out, out

def test_runs_carry_the_daemons_pid_namespace(env):
    from ttp import push
    from ttp.daemon import Daemon
    if not push.pid_ns():
        pytest.skip("no /proc/self/ns/pid")
    p = make(env)
    d = Daemon(p.base)
    tid = p.db.add_task("build it", "s", kind="code", tier="light", origin="user")
    cwd, _ = d._workdir_for(p.db.task(tid))
    rid = d.start_run("worker", "go", "claude", "light", cwd, task=p.db.task(tid))
    (p.runs / str(rid) / "STOP").touch()
    assert json.loads((p.runs / str(rid) / "run.json").read_text())["env"]["TTP_PIDNS"] == push.pid_ns()


def test_version_bump_config_is_validated(env):
    from ttp import push
    from ttp.project import config_problems
    assert push.bump_of(None) is None and push.bump_of({}) is None
    assert push.bump_of('{"files": ["a/x.json", "a/b/y.py"]}') == \
        {"files": ["a/x.json", "a/b/y.py"], "paths": ["a"], "package": "", "changeset_dir": "", "changeset_file": ""}
    for bad in ("x.json", {"files": []}, {"files": ["/abs.json"]}, {"files": ["../up.json"]},
                {"files": ["x.json"], "extra": 1}, [1]):
        with pytest.raises(ValueError):
            push.bump_of(bad)
    assert config_problems({"delivery": {"version_bump": {"files": []}}}) == \
        ["delivery.version_bump.files must list the files that hold the version"]
    assert config_problems({"delivery": {"version_bump": {"files": ["a/x.json"]}}}) == []


def test_push_excluded_helper_ignores_deletions_and_patches_already_on_the_tip(env, monkeypatch):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    tip = _git_out(repo, "rev-parse", "HEAD")
    assert push.exclude_list(None) == [] and push.exclude_list("") == []
    assert push.exclude_list('["./tmp/", "notes/*.md"]') == ["tmp", "notes/*.md"]
    (repo / "tmp").mkdir()
    _commit(repo, "tmp/a.txt", "a\n")
    _commit(repo, "x.py", "1\n")
    assert push.excluded(repo, tip, "HEAD", ["tmp"]) == ["tmp/a.txt"]
    assert push.excluded(repo, tip, "HEAD", []) == []
    assert push.excluded(repo, "HEAD~1", "HEAD", ["tmp"]) == []
    _git_out(repo, "rm", "-q", "tmp/a.txt")
    _git_out(repo, "commit", "-qm", "rm")
    assert push.excluded(repo, "HEAD~1", "HEAD", ["tmp/**"]) == []


def test_a_push_batch_fast_forwards_the_extra_branches_it_is_given(env, monkeypatch, capsys):
    from ttp import batch
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git_out(other, "fetch", "-q", "origin")
    _git_out(other, "checkout", "-q", "-b", "old", "origin/main")
    _commit(other, "old.txt", "old\n")
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/old")
    old = _git_out(origin, "rev-parse", "old")
    p.set_config("delivery.fast_forward_also", ["main", "old"])
    heads = [_entry(repo, "e1", {"f1.txt": "1\n"})]
    marker = _batch_marker(p, heads)
    rc, m = _run_batch(p, marker, monkeypatch)
    sha = _git_out(origin, "rev-parse", "proj")
    assert rc == 0 and m["outcome"] == "pushed" and m["pushed_sha"] == sha, m
    assert _git_out(origin, "rev-parse", "main") == sha and _git_out(origin, "rev-parse", "old") == old
    assert m["fast_forward"][0] == f"ff main {sha}" and m["fast_forward"][1].startswith("not ff old: "), m
    capsys.readouterr()
    assert batch.summary(marker) == 0
    out = capsys.readouterr().out
    assert f"  ff main {sha}" in out and "  warning: not ff old: " in out



def test_a_push_batch_mirrors_its_pushed_commit_and_still_runs_after_push(env, monkeypatch, capsys):
    from ttp import batch
    p, repo, origin, mirror = _mirror_setup(env, monkeypatch)
    deployed = env["tmp"] / "deployed"
    p.set_config("delivery.after_push", [f'echo "$TTP_PUSHED_SHA" > {deployed}'])
    heads = [_entry(repo, "e1", {"f1.txt": "1\n"})]
    marker = _batch_marker(p, heads)
    rc, m = _run_batch(p, marker, monkeypatch)
    sha = _git_out(origin, "rev-parse", "proj")
    assert rc == 0 and m["outcome"] == "pushed" and m["pushed_sha"] == sha, m
    assert _git_out(mirror, "rev-parse", "dev") == sha and m["fast_forward"] == [f"mirror mirror/dev {sha}"], m
    assert m["after_push"]["status"] == "ok" and deployed.read_text().strip() == sha, "after_push still runs"
    capsys.readouterr()
    assert batch.summary(marker) == 0 and f"  mirror mirror/dev {sha}" in capsys.readouterr().out
    # A diverged mirror: the batch still pushes, the mirror stays, after_push still runs.
    side = env["tmp"] / "side"
    subprocess.run(["git", "clone", "-q", "-b", "dev", str(mirror), str(side)], check=True)
    _commit(side, "hotfix.txt", "fix\n")
    _git_out(side, "push", "-q", "origin", "HEAD:dev")
    theirs = _git_out(mirror, "rev-parse", "dev")
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e2", {"f2.txt": "2\n"})], bid="b2"), monkeypatch)
    sha = _git_out(origin, "rev-parse", "proj")
    assert rc == 0 and m["outcome"] == "pushed" and _git_out(mirror, "rev-parse", "dev") == theirs, m
    [line] = m["fast_forward"]
    assert line.startswith("not mirrored mirror/dev: ") and theirs in line and sha in line, line
    assert deployed.read_text().strip() == sha


def test_a_push_batch_names_each_entrys_own_rebased_commit_and_the_queue_records_it(env, monkeypatch):
    """A rebase lands each entry under a new sha: the marker names it per entry ("sha"), and the push
    queue keeps it per row (landed_sha), so `landed:#id` can find the task's change on the branch."""
    from ttp import pushq
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["true"])
    heads = [_entry(repo, f"e{n}", {f"plugins/p/f{n}.txt": f"{n}\n"}) for n in (1, 2)]
    on = _entry(repo, "e0", {"plugins/p/f0.txt": "0\n"})
    _git_out(repo, "push", "-q", "origin", f"{on}:refs/heads/proj")
    rc, m = _run_batch(p, _batch_marker(p, heads + [on]), monkeypatch)
    assert rc == 0 and _statuses(m) == ["pushed", "pushed", "landed"], m
    shas = [r.get("sha") for r in m["results"]]
    tip = _git_out(origin, "rev-parse", "proj")
    assert shas[2] == on and shas[0] != heads[0] and shas[1] != heads[1] and m["pushed_sha"] == tip
    assert _git_out(origin, "log", "-1", "--format=%s", shas[1]) == "e2: edit plugins/p/f2.txt"
    assert _git_out(origin, "rev-parse", f"{shas[1]}^") == shas[0] and _git_out(origin, "rev-parse", f"{tip}^") == shas[1]
    # pushq._apply keeps each row's own commit, and the batch's head for a row the marker names none of.
    now = time.time()
    p.db.x("INSERT INTO push_batches(id, marker, target, started) VALUES('b1', 'm', 'origin/proj', ?)", (now,))
    for n, h in enumerate(heads + [on], 1):
        p.db.x("INSERT INTO push_queue(id, task, branch, head, target, status, batch, created, updated) "
               "VALUES(?, ?, ?, ?, 'origin/proj', 'batched', 'b1', ?, ?)", (n, 100 + n, f"e{n}", h, now, now))
    m["results"][1].pop("sha")
    pushq._apply(p, {"id": "b1", "target": "origin/proj"}, m, lambda *a, **k: None, now)
    assert [r["landed_sha"] for r in p.db.q("SELECT landed_sha FROM push_queue ORDER BY id")] == [shas[0], tip, on]


def test_a_push_batch_pushes_the_entries_after_a_blamed_first_one_once_they_pass_without_it(env, monkeypatch, capsys):
    """Entry 1 fails the checks and entries 2..5 pass: the batch blames entry 1, replays 2..5 onto the
    tip, checks that head once more and pushes it, instead of requeueing them untested."""
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["test ! -e plugins/p/bad.txt"])
    tip = _git_out(origin, "rev-parse", "proj")
    heads = [_entry(repo, f"e{n}", {f"plugins/p/{'bad' if n == 1 else f'f{n}'}.txt": f"{n}\n"}) for n in range(1, 6)]
    rc, m = _run_batch(p, _batch_marker(p, heads), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed" and _statuses(m) == ["check_failed"] + ["pushed"] * 4, m
    assert "checking the 4 entries after 1 without it" in capsys.readouterr().out
    names = _git_out(origin, "ls-tree", "--name-only", "proj", "plugins/p/").split()
    assert [n for n in names if n.endswith(".txt")] == [f"plugins/p/f{n}.txt" for n in range(2, 6)]
    shas = [r.get("sha") for r in m["results"]]
    assert shas[0] is None and _git_out(origin, "rev-parse", f"{shas[1]}^") == tip, "replayed onto the tip"
    assert _git_out(origin, "rev-parse", f"{m['pushed_sha']}^") == shas[4] and _bump_commits(origin) == [
        "p: 0.1.1 (e2: edit plugins/p/f2.txt; +3 more)"]
    assert "e1:" not in _git_out(origin, "show", "proj:.changeset/p-b1.md")


def test_a_push_batch_checks_the_entries_after_a_blamed_one_once_only(env, monkeypatch, capsys):
    """Entries 2 and 4 of five both fail: entry 2 is blamed, 3..5 are checked once without it and fail
    (entry 4); the batch tries no further round, pushes entry 1 and requeues 3..5 as tested."""
    log = env["tmp"] / "checks.log"
    p, repo, origin, other = _bump_setup(env, monkeypatch, [f"echo run >> {log}",
                                                            "test -z \"$(ls plugins/p | grep bad)\""])
    heads = [_entry(repo, f"e{n}", {f"plugins/p/{'bad' if n in (2, 4) else 'f'}{n}.txt": f"{n}\n"})
             for n in range(1, 6)]
    rc, m = _run_batch(p, _batch_marker(p, heads), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed", m
    assert _statuses(m) == ["pushed", "check_failed", "requeued", "requeued", "requeued"], m
    out = capsys.readouterr().out
    assert out.count("without it") == 1 and "they fail too; requeued" in out
    # full head, prefixes 2 and 1, then the one extra round
    assert len(log.read_text().split()) == m["checks"]["runs"] == 4
    assert all("untested" not in r["detail"] for r in m["results"][2:]), "they were checked without entry 2"


def test_entries_after_a_blamed_one_are_requeued_untested_without_an_extra_round(env, monkeypatch):
    """With no extra round left, the entries after a blamed one are requeued "untested"; the queue
    records that on their rows, and the next batch takes them without waiting push_min_gap_s."""
    from ttp import batch
    monkeypatch.setattr(batch, "RESCUES", 0)
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["test ! -e plugins/p/bad.txt"])
    heads = [_entry(repo, f"e{n}", {f"plugins/p/{'bad' if n == 2 else f'f{n}'}.txt": f"{n}\n"}) for n in (1, 2, 3)]
    rc, m = _run_batch(p, _batch_marker(p, heads), monkeypatch)
    assert _statuses(m) == ["pushed", "check_failed", "requeued"] and m["results"][2]["detail"] == {"untested": True}, m
    assert m["results"][0]["detail"] == {}


def test_rows_a_batch_requeued_untested_skip_the_push_gap(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch, push_batch_s=10 ** 6, push_batch_max=5)
    _pq_earlier(s)
    rid = _pq_review(s)[1]
    now = time.time()
    s.p.db.x("INSERT INTO push_batches(id, marker, target, started) VALUES('b1', 'm', 'origin/proj', ?)", (now,))
    s.p.db.x("UPDATE push_queue SET status='batched', batch='b1' WHERE id=?", (rid,))
    m = {"outcome": "pushed", "ended": now, "results": [{"id": rid, "status": "requeued", "detail": {"untested": True}}]}
    pushq._apply(s.p, {"id": "b1", "target": "origin/proj"}, m, lambda *a, **k: None, now)
    s.p.db.x("UPDATE push_batches SET after_finalized=? WHERE id='b1'", (now,))
    got, why = pushq.due(s.p, now + 1)
    assert [r["id"] for r in got] == [rid] and "1 approval b1 requeued untested" in why, why
    s.p.db.x("UPDATE push_queue SET detail=NULL WHERE id=?", (rid,))
    got, why = pushq.due(s.p, now + 1)
    assert got == [] and "waiting for more approvals" in why, "a row the last batch checked waits as usual"
    s.p.db.x("UPDATE push_batches SET outcome='tip_failed' WHERE id='b1'")
    got, why = pushq.due(s.p, now + 1)
    assert got == [] and "s left of the 1800 s gap" in why, "after a failing tip it waits for the gap"


def test_merge3_keeps_lines_both_sides_added_and_leaves_real_conflicts(env):
    from ttp.batch import merge3
    assert merge3("a\nX\n", "a\n", "a\nY\n") == "a\nX\nY\n"
    assert merge3("a\nX\nb\n", "a\nb\n", "a\nY\nb\n") == "a\nX\nY\nb\n"
    assert merge3("X\na\nb\n", "a\nb\n", "a\nb\nY\n") == "X\na\nb\nY\n", "a clean merge is kept as it is"
    assert merge3("a\nX\n", "a\nb\n", "a\nY\n") is None, "both changed one line"
    assert merge3("a\nX\n", "a", "a\nY\n") is None, "a last line without a newline is a change of it"
    assert merge3("a\nX\n", "a\n", "a\n=======\nY\n") is None, "a line like a separator is ambiguous"
    assert merge3("a\n=======\nX\n", "a\n", "a\nY\n") == "a\n=======\nX\nY\n"
    old, ours = "def x():\n    pass\n", "def x():\n    pass\n\n\ndef a():\n    return 1\n"
    for gap in ("", "\n\n\n"):
        theirs = old + gap + "def b():\n    return 2\n"
        assert merge3(ours, old, theirs, py=True) == ours + "\n\ndef b():\n    return 2\n", "PEP 8's two blank lines"
        assert merge3(ours, old, theirs) == ours + gap + "def b():\n    return 2\n", "elsewhere each side's spacing"
    assert merge3(ours, old, old + "Y = 2\n", py=True) == ours + "Y = 2\n"


def test_merge3_taken_in_takes_theirs_only_where_it_holds_our_change(env):
    """`taken_in` (template upgrades): a conflict whose local change upstream already ships takes
    upstream's side; any other conflict is still left for judgment, and the default is unchanged."""
    from ttp.batch import merge3
    base, ours = "a\nline1\nline2\nz\n", "a\nline1 fixed\nline2\nz\n"
    theirs = "a\nline1 fixed\nline2 improved\nz\n"
    assert merge3(ours, base, theirs) is None
    assert merge3(ours, base, theirs, taken_in=True) == theirs
    assert merge3("a\nX fix\nz\n", "a\nz\n", "a\nW\nX fix\nz\n", taken_in=True) == "a\nW\nX fix\nz\n"
    assert merge3("a\nX\nz\n", "a\nz\n", "a\nY\nz\n", taken_in=True) == "a\nX\nY\nz\n", "additions keep both"
    assert merge3("a\nmine\nz\n", "a\nold\nz\n", "a\nnew\nz\n", taken_in=True) is None, "both changed it"
    assert merge3("a\nz\n", "a\nold\nz\n", "a\nold new\nz\n", taken_in=True) is None, \
        "ours removed a line theirs changed"
    assert merge3("a\nfix\nz\n", "a\nold\nz\n", "a\nfix\nold\nz\n", taken_in=True) is None, \
        "theirs keeps a line ours removed"


def test_merge3_never_keeps_a_line_twice_or_moves_one_into_another_block(env):
    """Keeping both additions is right only when they share no line: two changes adding one import,
    or a change replayed onto a tip that holds it already, would leave that line twice. Lines of
    brackets, punctuation or quotes alone repeat nothing, nor does a line in the body of a new
    top-level Python def (its name is checked apart)."""
    from ttp.batch import merge3
    assert merge3("x\nA1\nB1\ny\n", "x\ny\n", "x\nB1\nB2\ny\n") is None, "a shared line in larger additions"
    assert merge3("x\nA1\nB1\ny\n", "x\ny\n", "x\nB1\ny\n") is None, "theirs is in ours already"
    assert merge3("x\nB1\ny\n", "x\ny\n", "x\nA1\nB1\ny\n") is None, "ours is in theirs"
    assert merge3("x\nA1\n  - B1\ny\n", "x\ny\n", "x\n- B1\ny\n") is None, "indented alike or not"
    assert merge3("x\nf(1,\n)\n'''\ny\n", "x\ny\n", "x\ng(2,\n)\n'''\ny\n") == "x\nf(1,\n)\n'''\ng(2,\n)\n'''\ny\n"
    old = "import os\n\n\ndef x():\n    pass\n"
    ours = old + "\n\n@pytest.mark.slow\ndef test_a(env):\n    p = make(env)\n    assert p\n"
    theirs = old + "\n\n@pytest.mark.slow\ndef test_b(env):\n    p = make(env)\n    assert not p\n"
    assert merge3(ours, old, theirs, py=True) == ours + theirs[len(old):], "two new tests"
    assert merge3(ours, old, theirs) is None, "outside Python, a line is a line"
    assert merge3(ours, old, old + "\n\ndef test_a(env):\n    return\n", py=True) is None, "a def both add"
    assert merge3("import os\nimport re\n", "import os\n", "import os\nimport re\nimport sys\n", py=True) is None
    assert merge3("@slow\n@a\ndef f():\n", "def f():\n", "@slow\n@b\ndef f():\n", py=True) is None, \
        "a decorator both put on one old def"
    # Theirs continues the block it was written in; after ours' new def it would land in that one.
    assert merge3(ours, old, old + "    assert x\n", py=True) is None
    assert merge3(old + "    assert x\n", old, ours, py=True) == old + "    assert x\n" + ours[len(old):]
    assert merge3("- a\n## New\n", "- a\n", "- a\n  - under a\n") is None
    assert merge3("- a\n  - b\n", "- a\n", "- a\n  - c\n") == "- a\n  - b\n  - c\n"


def test_merge3_hunks_takes_both_changes_only_where_they_touch_different_lines(env):
    """git calls changes of adjacent lines a conflict. With `hunks` (the push batch), a conflict whose
    two sides changed different lines of the base takes both; one where they changed the same line,
    or where one inserts at the edge of the other's change, or both add one line, stays a conflict."""
    from ttp.batch import merge3
    base = "a\nb\nc\nd\n"
    ours, theirs = "A\nb\nc\nd\n", "a\nB\nc\nd\n"
    assert merge3(ours, base, theirs) is None, "without hunks, git's conflict stands"
    kinds = set()
    assert merge3(ours, base, theirs, hunks=True, kinds=kinds) == "A\nB\nc\nd\n" and kinds == {"hunks"}
    assert merge3("a\nb\nc\n", "a\nb\nX\nc\n", "a\nB\nX\nc\n", hunks=True) == "a\nB\nc\n", "a deletion next to an edit"
    assert merge3("a\nX\nb\n", "a\nb\n", "a\nB\n", hunks=True) is None, "an insertion at the edge of an edit"
    assert merge3("a\nX\n", "a\nb\n", "a\nY\n", hunks=True) is None, "both changed one line"
    assert merge3("A\nimport os\nb\n", "a\nb\n", "a\nB\nimport os\n", hunks=True) is None, "both add one line"
    kinds = set()
    assert merge3("a\nX\n", "a\n", "a\nY\n", hunks=True, kinds=kinds) == "a\nX\nY\n" and kinds == {"added"}
    py = "def f(x):\n    if x:\n        return 1\n    return 0\n"
    assert merge3(py.replace("if x:", "if x and y:"), py, py.replace("return 1", "return 2"), py=True, hunks=True) \
        == "def f(x):\n    if x and y:\n        return 2\n    return 0\n"


def test_merge3_keeps_the_final_newline_as_the_sides_had_it(env):
    """git ends each side of a conflict at the end of the file with a newline. The settled result ends
    as the sides had it: without one where none had it, with one where a side added it."""
    from ttp.batch import merge3
    assert merge3("a\nb", "a\nc", "z\nc", hunks=True) == "z\nb"
    assert merge3("a\r\nb", "a\r\nc", "z\r\nc", hunks=True) == "z\r\nb"
    assert merge3("a\nb\n", "a\nc\n", "z\nc\n", hunks=True) == "z\nb\n"
    assert merge3("a\nb", "a\nc", "z\nc\n", hunks=True) == "z\nb\n", "theirs added the newline"
    assert merge3("a\nb", "a\nc\n", "z\nc\n", hunks=True) == "z\nb", "ours took it away"
    assert merge3("A\nb\nc", "a\nb\nc", "a\nb\nC", hunks=True) == "A\nb\nC", "no conflict: git's own result"


def test_a_push_batch_counts_requeued_conflicts_and_not_rebase_stops(env, monkeypatch):
    """An entry that conflicts with one that then fails its checks is requeued, and still counts as
    conflicted; a rebase that stops without a conflict is sent back but counts as none."""
    from ttp import batch
    p, repo, origin, other = _push_setup(env, monkeypatch, ["test ! -e bad.txt"])
    _commit(other, "notes.txt", "a\nb\nc\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    heads = [_entry(repo, "e1", {"more.txt": "1\n"}), _entry(repo, "e2", {"notes.txt": "a\nB2\nc\n", "bad.txt": "2\n"}),
             _entry(repo, "e3", {"notes.txt": "a\nB3\nc\n"})]
    rebase = batch.Batch._rebase

    def stop_e1(self, onto, top):
        return (onto, ["(the rebase stopped: hook said no)"]) if top == heads[0] else rebase(self, onto, top)
    monkeypatch.setattr(batch.Batch, "_rebase", stop_e1)
    rc, m = _run_batch(p, _batch_marker(p, heads), monkeypatch)
    assert _statuses(m) == ["conflict", "check_failed", "requeued"], m
    assert m["conflicts"] == {"entries": 3, "conflicted": 1, "auto_resolved": 0, "by_hunks": 0, "sent_back": 0}


def test_the_push_queue_counts_conflicts_and_those_its_batches_resolved(env):
    from ttp import pushq
    p, rid = _pq_rows(env)
    now = time.time()
    assert pushq.conflict_stats(p.db, now - 3600)["entries"] == 0
    for status, detail in (("pushed", None), ("pushed", {"settled": ["a.py"], "settled_by": ["hunks"]}),
                           ("check_failed", {"cmd": "pytest", "settled": ["b.py"], "settled_by": ["added"]}),
                           ("conflict", {"files": ["c.py"]}), ("approved", None),
                           ("conflict", {"files": ["d.py"], "settled": ["d.py"], "settled_by": ["hunks"]})):
        _pq_row(p, rid, now, status=status, batch="b1")
        p.db.x("UPDATE push_queue SET detail=? WHERE id=(SELECT MAX(id) FROM push_queue)",
               (json.dumps(detail) if detail else None,))
    _pq_row(p, rid, now, status="conflict", batch="b0")
    p.db.x("UPDATE push_queue SET updated=? WHERE id=(SELECT MAX(id) FROM push_queue)", (now - 8 * 86400,))
    want = {"entries": 5, "conflicted": 4, "auto_resolved": 2, "sent_back": 2, "conflict_pct": 80, "sent_back_pct": 40}
    assert pushq.summary(p, now)["conflicts"] == want, "a week's settled rows; the older one and approved ones are out"
    assert "last 7 days: 5 entries done, 4 conflicted (80%), 2 resolved in the batch, 2 sent back (40%)" \
        in pushq.queue_text(p, now).splitlines()
    # A rebase that stopped or did not finish is no conflict, as in Batch.conflicts().
    for files in (["(the rebase stopped: boom)"], ["(the rebase did not finish)"]):
        _pq_row(p, rid, now, status="conflict", batch="b2")
        p.db.x("UPDATE push_queue SET detail=? WHERE id=(SELECT MAX(id) FROM push_queue)",
               (json.dumps({"files": files}),))
    got = pushq.conflict_stats(p.db, now - 3600)
    assert (got["entries"], got["conflicted"], got["sent_back"]) == (7, 4, 2), got


def test_a_push_batch_rebuilds_a_worktree_a_crash_left_mid_rebase(env, monkeypatch, capsys):
    """The batch process dies while it settles a conflict: the next batch with those entries finds
    the push worktree mid-rebase, makes it anew and pushes them."""
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["true"])
    _commit(other, "plugins/p/notes.txt", "a\nc\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    heads = [_entry(repo, f"e{n}", {"plugins/p/notes.txt": f"a\nX{n}\nc\n"}) for n in (1, 2)]
    before = _git_out(origin, "rev-parse", "proj")
    crash = ("import os, sys\nfrom ttp import batch, cli\n"
             "batch.settle = lambda *a, **k: os._exit(9)\ncli.main(['push', '--batch', sys.argv[1]])\n")
    marker = _batch_marker(p, heads)
    output = json.loads(marker.read_text())["check_report_dir"]
    r = subprocess.run([sys.executable, "-c", crash, str(marker)], cwd=str(repo), input="",
                       capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_RUN_DIR": output})
    from ttp import batch
    wt = p.worktrees / "push"
    assert r.returncode == 9 and batch._rebasing(wt), r.stdout + r.stderr
    assert _git_out(origin, "rev-parse", "proj") == before
    capsys.readouterr()
    rc, m = _run_batch(p, _batch_marker(p, heads, bid="b2"), monkeypatch)
    assert "preparing a fresh worktree" in capsys.readouterr().out
    assert rc == 0 and m["outcome"] == "pushed" and _statuses(m) == ["pushed", "pushed"], m
    assert _git_out(origin, "show", "proj:plugins/p/notes.txt") == "a\nX1\nX2\nc"
    assert _versions(origin) == ("0.1.1", "0.1.1") and not batch._rebasing(wt)


def test_the_daemon_leaves_a_push_batch_marker_to_the_batch(env, monkeypatch):
    """push.tend settles detached pushes by their run lock. A batch lets go of that lock before its
    after_push and writes its marker after it, so tend must not touch a batch marker: marking it
    failed would race the batch's last write, and its deploy would run again."""
    from ttp import locks, push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    marker = _batch_marker(p, [_entry(repo, "e1", {"f1.txt": "1\n"})])
    from ttp.project import write_json
    ap_lock = p.state / "locks" / "after_push:run-b1.0.lock"
    write_json(marker, {**push._read(marker), "phase": "after_push", "outcome": "pushed", "lock": str(ap_lock)})
    before = marker.read_bytes()
    held = locks.try_take([ap_lock], "push batch b1 (after_push)")
    try:
        assert locks.any_free([push._run_lock(marker)]), "the run lock is free, as in the after_push"
        push.tend(p)
        assert marker.read_bytes() == before and push._settle(marker) == push._read(marker)
    finally:
        held.close()
    push.tend(p)          # the batch died: the push queue judges it (pushq.finalize), not tend
    assert marker.read_bytes() == before


@pytest.mark.parametrize("names, fresh, want, runs, tried", [
    (["fix", "e2", "bad"], 1, ["pushed", "pushed", "check_failed"], 4, None),   # fixing prefix passes; search above
    (["fix", "e2", "bad"], 3, ["pushed", "pushed", "check_failed"], 4, None),   # all untried: search the whole batch
    (["fix", "bad", "e3"], 2, ["pushed", "check_failed", "pushed"], 4, None),    # the breaker is in the untried prefix
    (["e1", "fix", "bad"], 3, ["requeued"] * 3, 2, [1]),    # prefix 1 fails: only entry 1 is shown not to fix it
    (["e1", "e2"], 2, ["requeued", "requeued"], 2, [1]),                       # all untried, none fixes it
    (["e1", "fix"], 2, ["pushed", "pushed"], 1, None),                         # tip with the batch passes: it goes
    (["e1", "e2"], 1, ["requeued", "requeued"], 2, [1]),                       # the fresh prefix does not fix it
    (["e1", "e2"], 0, ["requeued", "requeued"], 1, []),                        # nothing new: one check, no tip alone
])
def test_a_push_batch_on_a_known_failing_tip_never_checks_it_alone_and_pushes_a_fix(env, monkeypatch, capsys, names,
                                                                                     fresh, want, runs, tried):
    """A tip the queue knows fails on its own (marker "bad_tip") is not checked alone again. The tip with
    the batch, and the tip with the entries not yet tried on it, are; an entry that fixes the tip goes,
    with the ones after it up to the first that breaks the checks. A batch that ends at prefix 1 failing
    names only the entries it showed do not fix the tip ("tip_tried"): a fix after them stays untried."""
    from ttp.project import write_json
    from ttp import push
    log = env["tmp"] / "checks.log"
    p, repo, origin, other = _bump_setup(env, monkeypatch, [
        f"echo run >> {log}", "test ! -e broken.txt || test -e plugins/p/fix.txt", "test ! -e plugins/p/bad.txt"])
    _commit(other, "broken.txt", "x\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    tip = _git_out(origin, "rev-parse", "proj")
    heads = [_entry(repo, n, {f"plugins/p/{n}.txt": f"{n}\n"}) for n in names]
    marker = _batch_marker(p, heads)
    write_json(marker, {**push._read(marker), "bad_tip": {"tip": tip, "fresh": fresh, "cmd": "make test"}})
    rc, m = _run_batch(p, marker, monkeypatch)
    assert _statuses(m) == want, m
    out = capsys.readouterr().out
    assert m["checks"]["runs"] == runs == len(log.read_text().split()) + out.count(": echo run"), m
    assert "checking the tip" not in out and "tip_passed" not in m
    if want[0] == "requeued":
        assert rc == 4 and m["outcome"] == "tip_failed" and _git_out(origin, "rev-parse", "proj") == tip
        assert m["tip_check"]["cmd"] == "make test" and "not checked alone" in m["message"]
        assert m["tip_tried"] == tried, m
        return
    assert rc == 0 and m["outcome"] == "pushed" and _git_out(origin, "show", "proj:plugins/p/fix.txt") == "fix"


def test_a_push_batch_finds_the_push_lock_busy_and_touches_nothing(env, monkeypatch):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    marker = _batch_marker(p, [_entry(repo, "e1", {"f1.txt": "1\n"})])
    worktrees = _git_out(repo, "worktree", "list")
    held = push.take(p, "origin", "proj", 0, who="someone else")
    monkeypatch.setenv("TTP_RUN_DIR", push._read(marker)["check_report_dir"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    try:
        assert _ttp("push", "--batch", str(marker)) == 75
    finally:
        held.close()
    m = push._read(marker)
    assert m["outcome"] == "busy" and _statuses(m) == ["requeued"] and "someone else" in m["message"], m
    assert _git_out(repo, "worktree", "list") == worktrees and not (p.worktrees / "push").exists()


def test_the_default_push_wait_outlasts_the_measured_check_run(env, monkeypatch):
    """The lock is held through the checks, so a waiter's default wait must cover them (measured
    150-535 s, longer than the old fixed 300 s); an explicit push_wait_s still wins."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["sleep 1"])
    from ttp import push
    assert push.last_check_s(p) is None
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 0
    assert 1 <= push.last_check_s(p) < 30, "a passing check run must be measured"
    waits = []
    monkeypatch.setattr(push, "take", lambda p, remote, branch, wait_s, **kw: waits.append(wait_s))
    _commit(repo, "more.txt", "more\n")
    assert _ttp_push() == 75 and waits[-1] == push.DEFAULT_WAIT_S == 900
    push.record_check_s(p, 535)
    assert _ttp_push() == 75 and waits[-1] == 2 * 535 + push.WAIT_MARGIN_S > 535
    p.set_config("delivery.push_wait_s", 5)
    assert _ttp_push() == 75 and waits[-1] == 5


def test_the_measured_push_wait_is_bounded(env, monkeypatch):
    """A corrupt or huge timing must not make a waiter sit for hours or forever: Infinity, NaN and
    negative values are ignored, and the measured default is capped; an explicit push_wait_s is not."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    from ttp import push
    for raw in ('{"last_s": Infinity}', '{"last_s": NaN}', '{"last_s": -Infinity}', '{"last_s": -5}'):
        (p.state / push.TIMINGS).write_text(raw)
        assert push.last_check_s(p) is None, raw
    assert push.default_wait(None) == push.DEFAULT_WAIT_S
    assert push.default_wait(10 ** 9) == push.MAX_DEFAULT_WAIT_S == 2 * 3600
    push.record_check_s(p, 10 ** 6)
    waits = []
    monkeypatch.setattr(push, "take", lambda p, remote, branch, wait_s, **kw: waits.append(wait_s))
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 75 and waits[-1] == push.MAX_DEFAULT_WAIT_S
    p.set_config("delivery.push_wait_s", 5 * 3600)
    assert _ttp_push() == 75 and waits[-1] == 5 * 3600


def test_a_failed_timing_write_does_not_stop_the_push(env, monkeypatch, capsys):
    """Recording how long the checks took runs between the checks and the push: a full disk or a
    directory in the way is logged, and the push still goes out."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    from ttp import push
    (p.state / push.TIMINGS).mkdir(parents=True)
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 0
    assert _git_out(repo, "rev-parse", "HEAD") == _git_out(origin, "rev-parse", "refs/heads/proj")
    assert "could not record" in capsys.readouterr().err


def _pq_review(s, priority=3):
    """Another review of the same branch whose approval waits in the queue."""
    from ttp import pushq
    rid = s.p.db.add_task("another review", "r", kind="review", origin="coordinator", priority=priority)
    s.p.db.update_task(rid, status="pushing")
    with s.p.db.tx():
        [row] = pushq.approve(s.p, rid, None, {"entries": [{"branch": s.branch, "head": s.head}],
                                               "target": "origin/proj"})
    return rid, row


def _pq_earlier(s):
    """An earlier change of the same head still held in the queue: every later approval shares its files,
    so none is independent (pushq.fast_ids) and the routine window, gap and cap apply to them."""
    rid = _pq_review(s)[1]
    s.p.db.x("UPDATE push_queue SET status='held' WHERE id=?", (rid,))
    return rid


@pytest.mark.parametrize("link", ["pr_branch", "continues"])
def test_a_re_review_approves_the_head_its_re_plan_left_on_its_own_branch_in_one_run(env, monkeypatch, link):
    """The coordinator queues a re-review with the re-plan; it names only the reviewed branch, and the
    re-plan's head is on the re-plan's own branch. The push queue takes that head at once, with no
    second review run, but only a commit that builds on the reviewed branch, on the newest fix branch."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    fix, path, branch, head = _re_plan(s, f"pr_branch:{s.branch}" if link == "pr_branch" else f"continues:{s.review}")
    s.p.db.update_task(fix, status="done")
    again = s.p.db.add_task("Re-review (narrowed)", f"Re-review branch {s.branch} after its re-plan.", kind="review",
                            origin="coordinator")
    # Not a commit that leaves the reviewed branch out, even on the newest fix branch.
    _git_out(path, "checkout", "-q", "-b", "elsewhere", s.head + "~1")
    _commit(path, "stray.txt", "not on the reviewed branch\n")
    stray = _git_out(path, "rev-parse", "HEAD")
    _git_out(path, "checkout", "-q", branch)
    check = pushq.check_approval(s.p, s.p.db.task(again), [{"branch": branch, "head": stray}])
    assert "none of the reviewed refs" in check["invalid"] and branch in check["invalid"]
    later = s.p.db.add_task("Re-plan again", "s", kind="code", origin="coordinator", labels=[f"pr_branch:{s.branch}"])
    s.p.db.update_task(later, branch="ttp/t%d-later" % later)
    _git_out(s.repo, "branch", f"ttp/t{later}-later", stray)
    assert "invalid" in pushq.check_approval(s.p, s.p.db.task(again), [{"branch": branch, "head": stray}])
    s.p.db.update_task(later, status="cancelled")
    t = _pq_hand_off(env, s, push=[{"branch": branch, "head": head}], task=again)
    assert t["status"] == "pushing" and "woke" not in json.loads(t["result"])
    [row] = s.p.db.q("SELECT * FROM push_queue")
    assert (row["task"], row["head"], row["status"]) == (again, head, "approved")


def test_a_batch_is_due_after_the_window_at_the_cap_for_priority_one_or_with_no_review_coming(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch, push_batch_s=900, push_batch_max=3)
    s.p.db.update_task(s.review, status="running")   # a review is still at work: approvals may wait for it
    now = time.time()
    _pq_earlier(s)
    first = [_pq_review(s)[1], _pq_review(s)[1]]
    rows, why = pushq.due(s.p, now)
    assert rows == [] and "waiting" in why
    assert [(r["id"], r["branch"], r["target"], r["tries"]) for r in pushq.summary(s.p)["approved"]] == [
        (first[0], s.branch, "origin/proj", 0), (first[1], s.branch, "origin/proj", 0)]
    rows, why = pushq.due(s.p, now + 901)
    assert [r["id"] for r in rows] == first and "waited" in why
    later = [_pq_review(s)[1], _pq_review(s)[1]]
    rows, why = pushq.due(s.p, now)
    assert [r["id"] for r in rows] == first + later[:1] and "approvals" in why, "push_batch_max starts and caps a batch"
    s.p.db.x("DELETE FROM push_queue WHERE id IN (?,?,?)", (*first, later[0]))
    urgent = _pq_review(s, priority=1)[1]
    rows, why = pushq.due(s.p, now)
    assert [r["id"] for r in rows] == [urgent] and "priority 1-2" in why, "priority 1 goes alone, ahead of routine ones"
    s.p.db.x("DELETE FROM push_queue WHERE id=?", (urgent,))
    assert pushq.due(s.p, now)[0] == []
    s.p.db.update_task(s.review, status="done")
    rows, why = pushq.due(s.p, now)
    assert [r["id"] for r in rows] == [later[1]] and "no review" in why


def test_a_batch_waits_push_min_gap_s_after_the_last_one_ended_unless_full_or_priority_one(env, monkeypatch):
    from ttp import project, pushq
    s = _pq(env, monkeypatch, push_batch_s=0, push_batch_max=3)
    _pq_earlier(s)
    rows = [_pq_review(s)[1]]
    now = time.time() + 1   # past every approval's creation: push_batch_s=0 starts at once
    assert pushq.due(s.p, now)[0], "no batch ran before: nothing to wait for"
    # The last batch died before writing an outcome (a reboot): finalize recorded when it ended.
    s.p.db.x("INSERT INTO push_batches(id,marker,target,started,ended,finalized,after_finalized,outcome) "
             "VALUES('b1','m','origin/proj',?,?,?,?,'died')", (now - 4000, now - 600, now - 600, now - 600))
    got, why = pushq.due(s.p, now)
    assert got == [] and "1200 s left of the 1800 s gap" in why, "the default gap counts from the recorded end"
    assert pushq.schedule(s.p) is None
    assert [r["id"] for r in pushq.due(s.p, now + 1200)[0]] == rows, "the wait is bounded by the gap"
    assert [r["id"] for r in pushq.due(s.p, now + 10 ** 6)[0]] == rows
    rows.append(_pq_review(s, priority=1)[1])
    got, why = pushq.due(s.p, now)
    assert [r["id"] for r in got] == rows[1:] and "1 an urgent review" in why, "an urgent approval starts at once, alone"
    s.p.db.x("DELETE FROM push_queue WHERE id=?", (rows.pop(),))
    rows += [_pq_review(s)[1], _pq_review(s)[1]]
    got, why = pushq.due(s.p, now)
    assert [r["id"] for r in got] == rows and "3 approvals" in why, "push_batch_max still forces a start"
    s.p.set_config("delivery.push_min_gap_s", 0)
    s.p.db.x("DELETE FROM push_queue WHERE id=?", (rows.pop(),))
    assert pushq.due(s.p, now)[0], "0 turns the gap off"
    s.p.set_config("delivery.push_min_gap_s", 300)
    assert pushq.due(s.p, now)[0], "the gap is configurable"
    s.p.db.x("UPDATE push_batches SET ended=?", (now + 3600,))
    assert pushq.due(s.p, now)[0], "an end in the future (the clock went back) never holds the queue"
    assert project.push_queue_number("push_min_gap_s", "0") == 0
    with pytest.raises(ValueError, match="push_min_gap_s.*default 1800"):
        project.push_queue_number("push_min_gap_s", -1)


def _pq_last_batch(s, outcome, ended, after_done=True):
    s.p.db.x("INSERT INTO push_batches(id,marker,target,started,ended,finalized,after_finalized,outcome) "
             "VALUES('b1','m','origin/proj',?,?,?,?,?)",
             (ended - 600, ended, ended, ended if after_done else None, outcome))


@pytest.mark.parametrize("outcome", ["pushed", "landed", "nothing"])
def test_an_approval_after_a_clean_batch_starts_at_once_with_nothing_running_or_queued(env, monkeypatch, outcome):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    s.p.db.update_task(s.review, status="done")   # no review running or ready
    now = time.time()
    _pq_last_batch(s, outcome, now - 60)
    _pq_earlier(s)
    rid = _pq_review(s)[1]
    got, why = pushq.due(s.p, now)
    assert [r["id"] for r in got] == [rid] and "no review" in why, why


def test_an_approval_waits_at_most_the_default_batch_s_for_a_running_review(env, monkeypatch):
    from ttp import project, pushq
    s = _pq(env, monkeypatch)
    s.p.db.update_task(s.review, status="running")
    assert pushq.DEFAULT_BATCH_S == project.PUSH_QUEUE_DEFAULTS["push_batch_s"] == 300
    now = time.time()
    _pq_last_batch(s, "pushed", now - 60)   # a clean batch just ended: no gap after it
    _pq_earlier(s)
    rid = _pq_review(s)[1]
    got, why = pushq.due(s.p, now + 1)
    assert got == [] and "waiting for more approvals" in why, why
    got, why = pushq.due(s.p, now + 301)
    assert [r["id"] for r in got] == [rid] and "waited 300 s" in why, why


def test_the_gap_and_the_bad_tip_hold_still_apply_after_a_failing_tip(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch, push_batch_s=0)
    s.p.db.update_task(s.review, status="done")
    now = time.time()
    _pq_last_batch(s, "tip_failed", now - 600)
    _pq_earlier(s)
    rid = _pq_review(s)[1]
    got, why = pushq.due(s.p, now)
    assert got == [] and "1200 s left of the 1800 s gap" in why, why
    assert [r["id"] for r in pushq.due(s.p, now + 1200)[0]] == [rid]
    tip = pushq._tracking_tip(s.p, "origin", "proj")
    s.p.db.set_kv(pushq.KV, {"bad_tip": {"tip": tip, "fails": 1, "tried": [rid], "at": now}})
    got, why = pushq.due(s.p, now + 1200)
    assert got == [] and "held: the tip" in why, "HOLD_S still holds approvals tried on the failing tip"
    assert pushq.due(s.p, now + pushq.HOLD_S + 1)[0]


def _pq_alone(s, name, priority=3, text=None):
    """Approve a head of its own that changes `name` over the target's tip (not the shared s.head)."""
    from ttp import pushq
    tip = pushq._tracking_tip(s.p, "origin", "proj")
    branch = f"ttp/t0-{name.replace('.', '-')}-{time.monotonic_ns()}"
    _git_out(s.path, "checkout", "-q", "-b", branch, tip)
    _commit(s.path, name, text or f"{name}\n")
    head = _git_out(s.path, "rev-parse", "HEAD")
    _git_out(s.path, "checkout", "-q", s.branch)
    rid = s.p.db.add_task(f"review {name}", "r", kind="review", origin="coordinator", priority=priority)
    s.p.db.update_task(rid, status="pushing")
    with s.p.db.tx():
        [row] = pushq.approve(s.p, rid, None, {"entries": [{"branch": branch, "head": head}], "target": "origin/proj"})
    return row


def test_an_independent_approval_goes_alone_at_once_through_the_gap_and_the_window(env, monkeypatch):
    """user msg #1221: an approval sharing no file with an earlier unlanded one, or of priority 1-2, starts a
    batch of its own at once; routine ones that overlap still wait the gap (here 1200 s) or the window."""
    from ttp import pushq
    s = _pq(env, monkeypatch, push_batch_s=900)
    s.p.db.update_task(s.review, status="running")   # a review is at work: routine approvals wait for it
    now = time.time()
    _pq_last_batch(s, "tip_failed", now - 600)       # and a failed batch: 1200 s left of the gap
    _pq_earlier(s)
    routine = _pq_review(s)[1]
    assert pushq.due(s.p, now) == ([], "1200 s left of the 1800 s gap since the last batch ended")
    alone = _pq_alone(s, "other.txt")
    got, why = pushq.due(s.p, now)
    assert [r["id"] for r in got] == [alone] and "share no file with an earlier unlanded one" in why, why
    stacked = _pq_alone(s, "feature.txt", text="another feature\n")   # same file as the earlier ones
    assert pushq.fast_ids(s.p, pushq._approved(s.p.db, "origin/proj"),
                          pushq._tracking_tip(s.p, "origin", "proj")) == {alone}, "overlap never goes fast"
    p2 = _pq_alone(s, "feature.txt", priority=2, text="a p2 fix\n")
    got, why = pushq.due(s.p, now)
    assert [r["id"] for r in got] == [p2, alone] and "priority 1-2" in why, "priority first; routine ones wait"
    assert routine not in {r["id"] for r in got} and stacked not in {r["id"] for r in got}
    tip = pushq._tracking_tip(s.p, "origin", "proj")
    assert alone not in pushq.fast_ids(s.p, pushq._approved(s.p.db, "origin/proj"), tip, skip={alone}), \
        "an approval already tried on a failing tip is never fast"
    s.p.db.x("DELETE FROM push_queue WHERE id IN (?,?)", (alone, p2))
    assert pushq.due(s.p, now)[0] == [], "the routine ones still wait for the gap"


def test_an_independent_approval_still_waiting_after_15_minutes_raises_one_event(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    now = time.time()
    _pq_earlier(s)
    _pq_review(s)
    alone = _pq_alone(s, "other.txt")
    assert pushq.fast_waits(s.p, "batch b9 is still going", now + 60) == [], "not yet 15 minutes"
    assert pushq.fast_waits(s.p, "batch b9 is still going", now + pushq.FAST_WAIT_S + 1) == [alone]
    [e] = s.p.db.q("SELECT * FROM events WHERE kind=?", (pushq.FAST_WAIT_EVENT,))
    assert e["status"] == "handled" and f"row {alone}" in e["text"] and "batch b9 is still going" in e["text"]
    assert pushq.fast_waits(s.p, "batch b9 is still going", now + 3 * pushq.FAST_WAIT_S) == [], "once per row"
    s.p.db.x("UPDATE push_queue SET status='landed' WHERE id=?", (alone,))
    pushq.fast_waits(s.p, "x", now + 3 * pushq.FAST_WAIT_S)
    assert s.p.db.kv(pushq.FAST_KV) == [], "rows no longer waiting are pruned"


def test_batch_max_still_caps_a_batch_started_at_once(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch, push_batch_max=2)
    s.p.db.update_task(s.review, status="done")
    _pq_last_batch(s, "pushed", time.time() - 60)
    _pq_earlier(s)
    rows = [_pq_review(s)[1] for _ in range(3)]
    got, why = pushq.due(s.p)
    assert [r["id"] for r in got] == rows[:2], why


def test_the_next_batch_waits_for_the_last_ones_after_push_deploy(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch, push_batch_s=0)
    s.p.db.update_task(s.review, status="done")
    now = time.time()
    _pq_last_batch(s, "pushed", now - 60, after_done=False)   # pushed; its after_push still runs
    rid = _pq_review(s)[1]
    got, why = pushq.due(s.p, now)
    assert got == [] and "b1 is still going" in why, why
    s.p.db.x("UPDATE push_batches SET after_push='ok', after_finalized=? WHERE id='b1'", (now,))
    assert [r["id"] for r in pushq.due(s.p, now)[0]] == [rid], "once it ends no gap follows a clean batch"


@pytest.mark.parametrize("blocker", ["paused branch", "paused push", "live batch", "busy branch", "detached push",
                                     "backoff", "other target", "not allowed"])
def test_no_batch_starts_while_something_holds_the_push(env, monkeypatch, blocker):
    from ttp import locks, push, pushq
    from ttp.db import PAUSED_RESOURCES_KEY
    s = _pq(env, monkeypatch)
    rid, row = _pq_review(s, priority=1)
    now = time.time()
    assert pushq.due(s.p, now)[0], "the priority-1 approval alone is due"
    held = None
    if blocker.startswith("paused"):
        name = push.lock_name("origin", "proj") if blocker == "paused branch" else "push"
        s.p.db.set_kv(PAUSED_RESOURCES_KEY, {name: {"reason": "user", "since": now, "by": "user"}})
    elif blocker == "live batch":   # its deploy included
        s.p.db.x("INSERT INTO push_batches(id,marker,target,started,finalized) VALUES('b','m','origin/proj',?,?)",
                 (now, now))
    elif blocker == "busy branch":
        held = locks.try_take(push.lock_paths(s.p, "origin", "proj"), "someone's ttp push")
    elif blocker == "detached push":
        held = locks.try_take([s.p.state / "locks" / "push:run-123.0.lock"], "a detached ttp push")
    elif blocker == "backoff":
        s.p.db.set_kv(pushq.KV, {"backoff_until": now + 60})
    elif blocker == "other target":
        s.p.set_config("delivery.push_branch", "origin/other")
    else:
        s.p.set_config("delivery.push_allowed", False)
    try:
        rows, why = pushq.due(s.p, now)
        assert rows == [], why
        assert pushq.schedule(s.p) is None and not s.p.db.q("SELECT id FROM push_batches WHERE id!='b'")
    finally:
        if held:
            held.close()
    if blocker == "other target":   # approvals for the old target go back to their reviews
        t = s.p.db.task(rid)
        assert t["status"] == "queued" and json.loads(t["result"])["woke"].startswith(
            "push target changed: approved for origin/proj, the target is now origin/other")
        assert s.p.db.one("SELECT status FROM push_queue WHERE id=?", (row,))["status"] == "cancelled"


def test_a_due_batch_starts_one_detached_process_that_holds_its_run_lock(env, monkeypatch):
    from ttp import locks, pushq
    s = _pq(env, monkeypatch)
    release = env["tmp"] / "go"
    _pq_plan(s, release=str(release), sha="ab" * 20, version="1.2.3")
    _pq_hand_off(env, s)
    bid = pushq.schedule(s.p)   # no review is running or ready: due at once
    assert bid
    child = pushq._children[bid]
    marker = s.p.state / "pushes" / f"{bid}.json"
    m = json.loads(marker.read_text())
    assert (m["kind"], m["status"], m["phase"], m["target"], m["repo"], m["pid"]) == (
        "batch", "running", "push", "origin/proj", str(s.p.root), child.pid)
    [e] = m["entries"]
    assert (e["task"], e["branch"], e["head"], e["ref"]) == (s.review, s.branch, s.head, f"refs/ttp/push/{e['id']}")
    assert m["lock"] == str(s.p.state / "locks" / f"push:run-{bid}.0.lock")
    assert not locks.any_free([pathlib.Path(m["lock"])]), "the batch does not hold its run lock"
    assert os.getsid(child.pid) == child.pid, "the batch should run in a session of its own"
    assert s.p.db.one("SELECT status, batch FROM push_queue") == {"status": "batched", "batch": bid}
    assert s.p.db.one("SELECT target, finalized FROM push_batches") == {"target": "origin/proj", "finalized": None}
    assert pushq.due(s.p, time.time() + 10 ** 6)[0] == [], "one batch at a time"
    assert pushq.finalize(s.p) == [] and s.p.db.task(s.review)["status"] == "pushing"
    sm = pushq.summary(s.p)
    assert sm["on"] and sm["approved"] == [] and sm["last"] == []
    assert (sm["live"]["id"], sm["live"]["phase"], sm["live"]["rows"], sm["live"]["target"]) == (bid, "push", 1,
                                                                                               "origin/proj")
    release.write_text("")
    child.wait(timeout=30)
    assert locks.any_free([pathlib.Path(m["lock"])])


def test_a_pushed_batch_closes_the_review_done_without_any_model_run(env, monkeypatch):
    from ttp import pushq
    from ttp.daemon import Daemon
    s = _pq(env, monkeypatch)
    sha = "ab" * 20
    _pq_plan(s, outcome="pushed", sha=sha, tip="cd" * 20, version="0.3.1")
    _pq_hand_off(env, s)
    mark, runs = _pq_mark(s.p), s.p.db.one("SELECT COUNT(*) n FROM runs")["n"]
    s.p.db.set_kv("last_coordinator_turn", time.time())   # no idle wake while it runs: that is not the queue's
    d = Daemon(s.p.base)
    # the review closes before the batch's after-push step settles: wait for both
    assert _run_until(d, s.p, lambda: s.p.db.task(s.review)["status"] == "done" and not s.p.db.one(
        "SELECT id FROM push_batches WHERE after_finalized IS NULL"), timeout=60)
    assert s.p.db.one("SELECT COUNT(*) n FROM runs")["n"] == runs, "the push cycle started a model run"
    t = s.p.db.task(s.review)
    res = json.loads(t["result"])
    [bid] = [b["id"] for b in s.p.db.q("SELECT id FROM push_batches")]
    assert res["pushed"] == [{"branch": s.branch, "head": s.head, "sha": sha, "version": "0.3.1", "batch": bid,
                              "status": "pushed"}]
    assert res["summary"] == f"looks good (pushed {sha[:7]} as 0.3.1 to origin/proj)" and res["status"] == "done"
    assert s.p.db.one("SELECT status, pushed_sha, version FROM push_queue") == {
        "status": "pushed", "pushed_sha": sha, "version": "0.3.1"}
    evs = _pq_events(s.p, mark)
    assert [e["status"] for e in evs if e["kind"] == "task_done"] == ["handled"]
    assert not [e for e in evs if e["status"] == "queued"], "a pushed review woke the coordinator"
    assert subprocess.run(["git", "-C", str(s.repo), "show-ref", "--verify", "--quiet", "refs/ttp/push/1"]).returncode, \
        "the pin of a pushed row of a finished task stays"
    sm = pushq.summary(s.p)
    assert sm["live"] is None and sm["approved"] == [] and sm["deaths"] == 0
    assert [(b["id"], b["outcome"], b["pushed_sha"], b["version"], b["after_push"]) for b in sm["last"]] == [
        (bid, "pushed", sha, "0.3.1", "skipped")]
    state = lambda: (s.p.db.q("SELECT * FROM push_queue"), s.p.db.q("SELECT * FROM push_batches"),  # noqa: E731
                     s.p.db.q("SELECT id FROM events"), s.p.db.task(s.review))
    before = state()
    assert pushq.finalize(s.p) == [] and pushq.tend(s.p) == [] and state() == before, "a second finalize changed things"


@pytest.mark.parametrize("case", ["off", "empty", "approved", "running", "deploying", "finished", "failed"])
def test_status_shows_one_push_queue_line_in_each_state_and_never_a_command(env, case):
    from ttp import pushq
    from ttp.cli import status_text
    p, rid = _pq_rows(env, on=case != "off")
    now = time.time()
    sha = "cd" * 20
    if case in ("approved", "running", "failed"):
        _pq_row(p, rid, now, age=361)
        _pq_row(p, rid, now, age=60, head="ef" * 20)
    if case == "running":
        _pq_batch_row(p, "b2", now, age=125, phase="push")
        _pq_row(p, rid, now, status="batched", batch="b2", age=200)
        _pq_row(p, rid, now, status="batched", batch="b2", age=150)
    if case in ("deploying", "finished", "failed"):
        _pq_batch_row(p, "b1", now, age=900, outcome="pushed", pushed_sha=sha, version="0.2.140", ended=now - 720,
                      finalized=now - 715, check_runs=1, check_s=242.4,
                      **({"after_push": "ok", "after_finalized": now - 700} if case != "deploying" else {}))
        if case == "deploying":   # the push phase is over (finalized); after_push still runs
            (p.state / "pushes" / "b1.json").write_text(json.dumps({"id": "b1", "phase": "after_push"}))
            _pq_row(p, rid, now, status="pushed", batch="b1", age=1000, sha=sha, version="0.2.140")
    if case == "failed":
        _pq_batch_row(p, "b3", now, age=400, outcome="tip_failed", tip="aa" * 20, ended=now - 300,
                      finalized=now - 290, after_push="skipped", after_finalized=now - 290, check_runs=1, check_s=30)
    line = pushq.status_line(p, now)
    want = {
        "off": None,
        "empty": "push queue: empty",
        "approved": "push queue: 2 approved (oldest 6 min)",
        "running": "push queue: 2 approved (oldest 6 min) · batch checking since 2 min (2 changes)",
        "deploying": f"push queue: batch deploying since 15 min (1 change) · last pushed {sha[:7]} as 0.2.140 to origin/proj 12 min ago",
        "finished": f"push queue: empty · last pushed {sha[:7]} as 0.2.140 to origin/proj 12 min ago · deploy ok",
        "failed": f"push queue: 2 approved (oldest 6 min) · last batch the branch tip fails its checks 5 min ago · "
                  f"last pushed {sha[:7]} as 0.2.140 to origin/proj 12 min ago · deploy ok",
    }[case]
    assert line == want
    assert [ln for ln in status_text(p).splitlines() if ln.startswith("push queue")] == ([want] if want else [])
    if want:
        assert "ttp " not in line and "`" not in line and " run " not in line, "the status line tells the user to act"


def test_the_push_queue_line_stays_once_the_queue_is_off_while_it_has_rows(env):
    from ttp import pushq
    p, rid = _pq_rows(env, on=False)
    now = time.time()
    _pq_row(p, rid, now, age=60)
    assert pushq.status_line(p, now) == "push queue (off): 1 approved (oldest 1 min)"


def test_a_push_notice_names_the_branch_and_sha_and_says_when_the_work_is_not_yet_on_its_intended_target(
        env, monkeypatch, capsys):
    """'pushed' must never read as landed: with delivery.base_ref naming another branch than the one
    pushed, `ttp push` names the branch and short sha and says the work is not on that branch yet,
    how far behind it is, and never assumes a branch it cannot read was reached."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    assert push.intended(p, repo, "origin/proj") == "", "the push branch itself names no other target"
    assert push.reach(p, repo, "origin/proj", _git_out(repo, "rev-parse", "HEAD")) is None
    for same in ("proj", "origin/proj", "refs/remotes/origin/proj", "refs/heads/proj", "ab" * 20):
        p.set_config("delivery.base_ref", same)
        assert push.intended(p, repo, "origin/proj") == "", same
    p.set_config("delivery.base_ref", "origin/main")
    _commit(repo, "a.txt", "a\n")
    _commit(repo, "b.txt", "b\n")
    capsys.readouterr()
    assert _ttp_push() == 0
    head = _git_out(origin, "rev-parse", "proj")
    err = capsys.readouterr().err
    assert f"pushed {head[:7]} to origin/proj, not yet on origin/main (origin/main is 2 commits behind)" in err, err
    assert push.last_reach == {"ref": "origin/main", "on": False, "behind": 2}
    # --own publishes another branch: the push branch is what the work is meant to reach.
    assert push.intended(p, repo, "origin/ttp/t7-exp") == "origin/proj"
    # The target catches up: the same push is on it too.
    _git_out(repo, "push", "-q", "origin", f"{head}:refs/heads/main")
    r = push.reach(p, repo, "origin/proj", head)
    assert r == {"ref": "origin/main", "on": True, "behind": 0}
    assert push.landing(head, "origin/proj", r, "0.1.1") == f"pushed {head[:7]} as 0.1.1 to origin/proj, also on origin/main"
    # A target that cannot be read is never taken as reached.
    p.set_config("delivery.base_ref", "origin/gone")
    assert push.landing(head, "origin/proj", push.reach(p, repo, "origin/proj", head)) == (
        f"pushed {head[:7]} to origin/proj, not yet seen on origin/gone (it could not be read)")
    # The detached push's verdict says it as well.
    marker = p.state / "pushes" / "x.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"id": "x", "status": "pushed", "exit": 0, "sha": head, "target": "origin/proj",
                                  "reach": {"ref": "origin/main", "on": False, "behind": 1}}))
    capsys.readouterr()
    assert push.result(marker) == 0
    assert capsys.readouterr().out.strip() == (
        f"ttp push: pushed {head} to origin/proj, not yet on origin/main (origin/main is 1 commit behind)")


def test_a_push_batch_records_whether_what_it_pushed_is_on_the_intended_target(env, monkeypatch, capsys):
    """A batch to the push branch, with delivery.base_ref naming another branch, records in its marker
    that the pushed commit is not on that branch yet, and its result says so; with no other branch
    named it records nothing."""
    from ttp import batch
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    h1 = _entry(repo, "e1", {"one.txt": "1\n"})
    rc, m = _run_batch(p, _batch_marker(p, [h1]), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed" and m["reach"] is None, m
    p.set_config("delivery.base_ref", "origin/main")
    h2 = _entry(repo, "e2", {"two.txt": "2\n"})
    capsys.readouterr()
    rc, m = _run_batch(p, _batch_marker(p, [h2], bid="b2"), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed", m
    assert m["reach"] == {"ref": "origin/main", "on": False, "behind": 2}, m
    sha = _git_out(origin, "rev-parse", "proj")
    out = capsys.readouterr().out
    assert f"pushed {sha[:7]} to origin/proj, not yet on origin/main (origin/main is 2 commits behind)" in out, out
    assert batch.summary(p.state / "pushes" / "b2.json") == 0
    out = capsys.readouterr().out
    assert f"to origin/proj, not yet on origin/main (origin/main is 2 commits behind)" in out.splitlines()[0], out


def test_a_reach_check_that_times_out_or_raises_never_changes_a_push_that_landed(env, monkeypatch, capsys):
    """The reach check runs after the push landed: its fetch is bounded, prompt-free and stdin-free, a
    timeout reads as 'could not be read', and an exception leaves `ttp push` at 0 and a batch's
    marker at outcome 'pushed' with no reach."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    p.set_config("delivery.base_ref", "origin/main")
    real, seen = subprocess.run, []

    def stalled(cmd, *a, **kw):
        if "fetch" in cmd and "refs/remotes/origin/main" in cmd[-1]:
            seen.append(kw)
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return real(cmd, *a, **kw)
    monkeypatch.setattr(push.subprocess, "run", stalled)
    _commit(repo, "a.txt", "a\n")
    capsys.readouterr()
    assert _ttp_push() == 0
    assert push.last_reach == {"ref": "origin/main", "on": None, "behind": None}
    assert "not yet seen on origin/main (it could not be read)" in capsys.readouterr().err
    kw = seen[0]
    assert kw["timeout"] == push.REACH_FETCH_S <= 60 and kw["stdin"] == subprocess.DEVNULL, kw
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0", kw
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e1", {"one.txt": "1\n"})]), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed" and m["reach"]["on"] is None, m
    monkeypatch.setattr(push.subprocess, "run", real)

    def broken(*a, **kw):
        raise RuntimeError("no git")
    monkeypatch.setattr(push, "reach", broken)
    _commit(repo, "b.txt", "b\n")
    capsys.readouterr()
    assert _ttp_push() == 0 and push.last_reach is None
    assert "cannot tell whether it is on the branch it is meant to reach: RuntimeError" in capsys.readouterr().err
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e2", {"two.txt": "2\n"})], bid="b2"), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed" and m["reach"] is None, m


def test_a_pushed_batch_tells_the_review_status_and_web_app_it_is_not_yet_on_the_intended_target(env, monkeypatch):
    """The review's summary, its done event, `ttp status`'s queue line, `ttp push --queue` and the web
    app's batch row all name the branch and short sha and say the work is not yet on the branch it is
    meant to reach, instead of letting 'pushed' read as landed."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    sha = "ab" * 20
    reach = {"ref": "origin/main", "on": False, "behind": 4}
    _pq_plan(s, outcome="pushed", sha=sha, tip="cd" * 20, version="0.3.1", reach=reach)
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _pq_batch(s)
    _pq_tend(s)
    words = f"pushed {sha[:7]} as 0.3.1 to origin/proj, not yet on origin/main (origin/main is 4 commits behind)"
    t = s.p.db.task(s.review)
    assert t["status"] == "done" and json.loads(t["result"])["summary"] == f"looks good ({words})"
    [ev] = [e for e in _pq_events(s.p, mark) if e["kind"] == "task_done"]
    assert f"({words}, batch " in ev["text"], ev
    now = time.time()
    sm = pushq.summary(s.p, now)
    assert sm["last"][0]["landing"] == words and sm["last_pushed"]["reach"] == reach
    line = pushq.status_line(s.p, now, sm)
    assert f"last pushed {sha[:7]} as 0.3.1 to origin/proj " in line and \
        "ago, not yet on origin/main (origin/main is 4 commits behind)" in line, line
    assert any(ln.endswith("ago: " + words + ", checks 1 run in 2 s, no deploy") for ln in
               pushq.queue_text(s.p, now).splitlines()), pushq.queue_text(s.p, now)
    assert pushq.web(s.p, now=now)["last"][0]["landing"] == words


def test_a_batch_records_its_reach_with_its_outcome_so_a_tick_meanwhile_cannot_settle_without_it(env, monkeypatch):
    """finalize settles a marker as soon as it has an outcome, even while the batch runs: the reach
    check runs before the outcome is written, so a tick during its fetch finds nothing to settle and
    the review's one-shot summary still says the work is not yet on the intended target."""
    from ttp import batch, push, pushq
    s = _pq(env, monkeypatch)
    _git_out(s.repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    s.p.set_config("delivery.base_ref", "origin/main")
    monkeypatch.setattr(pushq, "batch_argv", lambda marker: [sys.executable, "-c", "pass"])
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    marker = pathlib.Path(s.p.db.one("SELECT marker FROM push_batches WHERE id=?", (bid,))["marker"])
    real, seen = push.reach, []

    def reach(*a, **kw):
        seen.append(push._read(marker).get("outcome"))
        _pq_tend(s)                 # a daemon tick while the fetch runs
        seen.append(s.p.db.task(s.review)["status"])
        return real(*a, **kw)
    monkeypatch.setattr(push, "reach", reach)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("TTP_RUN_DIR", push._read(marker)["check_report_dir"])
    assert batch.run_batch(s.p, marker) == 0
    assert seen == [None, "pushing"], seen
    m = push._read(marker)
    assert m["outcome"] == "pushed" and m["reach"]["ref"] == "origin/main" and m["reach"]["on"] is False, m
    _pq_tend(s)
    t = s.p.db.task(s.review)
    assert t["status"] == "done" and "not yet on origin/main" in json.loads(t["result"])["summary"], t


def test_ttp_push_queue_lists_the_entries_and_the_last_ten_batches(env, monkeypatch, capsys):
    from ttp import cli, pushq
    p, rid = _pq_rows(env)
    now = time.time()
    sha = "cd" * 20
    for i in range(12):
        _pq_batch_row(p, f"b{i:02d}", now, age=7200 - i * 60, outcome="nothing", ended=now - 7000 + i * 60,
                      finalized=now, after_push="skipped", after_finalized=now)
    _pq_batch_row(p, "b20", now, age=900, outcome="pushed", pushed_sha=sha, version="0.2.140", ended=now - 720,
                  finalized=now, check_runs=2, check_s=242.4, after_push="failed", after_finalized=now)
    _pq_row(p, rid, now, status="pushed", batch="b20", age=1000, sha=sha, version="0.2.140")
    _pq_row(p, rid, now, age=361, head="ef" * 20)
    _pq_batch_row(p, "b21", now, age=30, phase="push")
    _pq_row(p, rid, now, status="batched", batch="b21", age=100, head="12" * 20)
    out = pushq.queue_text(p, now).splitlines()
    assert out[0].startswith("push queue: 1 approved (oldest 6 min) · batch checking since 0 min (1 change) · "
                             f"last pushed {sha[:7]} as 0.2.140 to origin/proj 12 min ago · deploy failed"), out[0]
    i = out.index("entries:")
    assert out[i + 1:i + 4] == [
        f"  #{rid} ttp/t1-feature 1212121 batched in b21, 1 min old: review feature",
        f"  #{rid} ttp/t1-feature efefefe approved, 6 min old: review feature",
        f"  #{rid} ttp/t1-feature abababa pushed in b20, 16 min old: review feature"]
    j = out.index("batches, newest first:")
    assert out[j + 1] == "  b21 running: checking since 0 min, 1 change"
    assert out[j + 2] == f"  b20 12 min ago: pushed {sha[:7]} as 0.2.140 to origin/proj, checks 2 runs in 242 s, deploy failed"
    assert out[j + 3].startswith("  b11 ") and out[j + 3].endswith("ago: nothing to push, no deploy")
    assert len(out) - j - 2 == 10, "the last 10 batches, plus the live one"
    # the CLI flag prints the same, from the project's folder, and pushes nothing
    monkeypatch.chdir(p.root)
    monkeypatch.setattr(sys, "argv", ["ttp", "push", "--queue"])
    cli.main()
    printed = capsys.readouterr().out
    assert "entries:" in printed and "b20 12 min ago: pushed" in printed


def test_a_conflicting_head_gets_no_rebase_review_past_the_review_cap_or_after_one_already_ran(env, monkeypatch):
    """The rolling review-task cap holds the automatic rebase back: the review fails to the coordinator
    as before. A head that already had a rebase review gets none again, whoever re-approves it."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    s.p.set_config("coordinator.max_review_tasks_per_day", 0)
    s.p.db.update_task(s.review, max_attempts=1)
    row = {"head": s.head, "branch": s.branch}
    with s.p.db.tx():
        assert pushq.auto_rebase(s.p, s.p.db.task(s.review), row, "origin/proj", "push conflict: x") is None
    s.p.set_config("coordinator.max_review_tasks_per_day", 50)
    other = s.p.db.add_task("Review again", "r", kind="review", origin="coordinator", depends_on=[s.code])
    with s.p.db.tx():
        first = pushq.auto_rebase(s.p, s.p.db.task(s.review), row, "origin/proj", "push conflict: x")
        assert first and pushq.auto_rebase(s.p, s.p.db.task(other), row, "origin/proj", "push conflict: x") is None
    assert pushq.rebase_head(s.p.db.task(first)) == s.head


def test_the_daemon_closes_code_tasks_left_in_review_whose_latest_review_pushed_them(env):
    """Clean-up of tasks left behind: only a task whose latest covering review is done and pushed its
    work after the task entered review is closed, once."""
    from ttp import pushq
    from ttp.daemon import Daemon
    p, _ = _pq_rows(env)
    db, now = p.db, time.time()

    def code(name, at):
        tid = db.add_task(name, "s", kind="code", tier="light", origin="user")
        db.update_task(tid, status="review", branch=f"ttp/t{tid}-{name}")
        db.x("UPDATE tasks SET updated=? WHERE id=?", (at, tid))
        return tid

    def review(title, status, row=None, at=now, **kw):
        rid = db.add_task(title, "r", kind="review", origin="coordinator", **kw)
        db.update_task(rid, status=status)
        if row:
            db.x("INSERT INTO push_queue(task, branch, head, target, status, batch, created, updated, pushed_sha, "
                 "version) VALUES(?,?,?,?,?,?,?,?,?,?)", (rid, row, "ab" * 20, "origin/proj", "pushed", "b1",
                                                          at - 60, at, "cd" * 20, "0.3.2"))
        return rid

    shipped, by_branch, by_dep = code("shipped", now - 500), code("branch", now - 500), code("dep", now - 500)
    reopened, pending, unreviewed = code("reopened", now - 100), code("pending", now - 500), code("none", now - 500)
    review(f"Review #{shipped}: x", "failed")                          # an earlier round, failed on a conflict
    done = review(f"Review #{shipped}: settle the conflict", "done", f"ttp/t{shipped}-shipped")
    review("land it", "done", f"ttp/t{by_branch}-branch")             # pushed the task's branch
    review("review the stack", "done", "ttp/other", depends_on=[by_dep])
    review(f"Review #{reopened}: x", "done", f"ttp/t{reopened}-reopened", at=now - 300)   # before it re-entered review
    review(f"Review #{pending}: x", "done", f"ttp/t{pending}-pending")
    review(f"Review #{pending}: again", "queued")                                          # a newer review still open
    Daemon(p.base).tend_pushes()
    st = {i: db.task(i)["status"] for i in (shipped, by_branch, by_dep, reopened, pending, unreviewed)}
    assert st == {shipped: "done", by_branch: "done", by_dep: "done", reopened: "review", pending: "review",
                  unreviewed: "review"}
    res = json.loads(db.task(shipped)["result"])
    assert res["shipped_by"] == done and res["pushed"][0]["sha"] == "cd" * 20 and res["pushed"][0]["version"] == "0.3.2"
    assert len(db.q("SELECT id FROM events WHERE kind='task_done'")) == 3
    assert pushq.settle_reviewed(db) == [] and len(db.q("SELECT id FROM events WHERE kind='task_done'")) == 3


def test_a_broken_target_tip_is_reported_once_and_held_until_the_tip_changes(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch, push_min_gap_s=0)   # the hold alone, no gap
    tip = _git_out(s.repo, "rev-parse", "refs/remotes/origin/proj")
    _pq_plan(s, outcome="tip_failed", tip=tip, row="requeued", message="the tip of origin/proj fails its checks",
             tip_check={"cmd": "make test", "tail": "the tip is red"})
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    for _ in range(2):   # the same tip fails twice: one report
        _pq_batch(s)
        _pq_tend(s)
    assert s.p.db.task(s.review)["status"] == "pushing"
    assert s.p.db.one("SELECT status FROM push_queue")["status"] == "approved"
    [ev] = [e for e in _pq_events(s.p, mark) if e["status"] == "queued"]
    assert ev["kind"] == "push_tip_failed" and "make test" in ev["text"] and "the tip is red" in ev["text"]
    rows, why = pushq.due(s.p, time.time() + 60)
    assert rows == [] and "held" in why
    rows, why = pushq.due(s.p, time.time() + pushq.HOLD_S + 60)
    assert rows == [] and "held" in why, "known bad after TIP_FAILS batches: held past HOLD_S"
    assert pushq.due(s.p, time.time() + pushq.BAD_TIP_RETRY_S + 60)[0], "the hold ends after BAD_TIP_RETRY_S"
    assert len(s.p.db.q("SELECT id FROM messages WHERE kind='alert' AND ref=?", (pushq.BAD_TIP_ALERT,))) == 1
    _commit(s.other, "theirs.txt", "theirs\n")
    _git_out(s.other, "push", "-q", "origin", "HEAD:proj")
    _git_out(s.repo, "fetch", "-q", "origin")
    rows, why = pushq.due(s.p, time.time() + 60)
    assert rows, f"a new tip should end the hold: {why}"
    _pq_plan(s, outcome="tip_failed", tip=_git_out(s.repo, "rev-parse", "refs/remotes/origin/proj"), row="requeued",
             tip_check={"cmd": "make test", "tail": "still red"})
    _pq_batch(s)
    _pq_tend(s)
    assert [e["kind"] for e in _pq_events(s.p, mark) if e["status"] == "queued"] == ["push_tip_failed"] * 2


def test_a_tip_failing_only_where_the_queue_checks_it_stops_being_retried_and_a_fixing_approval_lands(
        env, monkeypatch):
    """Replays a push branch tip whose checks fail only in the batch's own checkout (a path-dependent
    failure: they passed in the task's worktree) while more approvals wait than a batch takes. After
    TIP_FAILS tip_failed batches on that tip, one keyed alert is raised and the approvals already tried
    on it wait, not run again and again. A new approval goes first; the tip with it passes, so it is
    pushed, which clears the alert, and the waiting approval then lands on the fixed tip."""
    from ttp import alerts, pushq
    check = 'case "$(pwd -P)" in */push) test -e fix.txt ;; esac'
    s = _pq_real(env, monkeypatch, push_min_gap_s=0, push_batch_max=1, push_checks=[check])
    db = s.p.db
    _pq_hand_off(env, s)
    tip = _git_out(s.origin, "rev-parse", "proj")
    log = lambda bid: (s.p.state / "pushes" / f"{bid}.log").read_text()
    for n in range(1, pushq.TIP_FAILS + 1):
        bid = _pq_batch(s)
        m = json.loads((s.p.state / "pushes" / f"{bid}.json").read_text())
        assert m["outcome"] == "tip_failed" and m["checks"]["runs"] == 2 and "bad_tip" not in m, log(bid)
        _pq_tend(s)
        assert (db.kv("push_queue")["bad_tip"]["fails"], db.task(s.review)["status"]) == (n, "pushing")
    alerted = lambda: db.q("SELECT text FROM messages WHERE kind='alert' AND ref=?", (pushq.BAD_TIP_ALERT,))
    [a] = alerted()
    assert tip[:7] in a["text"] and check in a["text"]
    assert alerts.holds(db, pushq.BAD_TIP_ALERT, time.time(), time.time())
    rows, why = pushq.due(s.p, time.time() + pushq.HOLD_S + 60)
    assert rows == [] and "held" in why, "the tried approval is not checked on that tip again"
    code, path, branch = _code_task(s.p, "fix")
    _commit(path, "fix.txt", "fix\n")
    fix = db.add_task("review fix", "review the fix", kind="review", tier="standard", origin="coordinator",
                      depends_on=[code])
    _pq_hand_off(env, s, task=fix, push=[{"branch": branch, "head": _git_out(path, "rev-parse", "HEAD")}])
    rows, why = pushq.due(s.p, time.time() + 60)
    assert [r["task"] for r in rows] == [fix], why
    bid = _pq_batch(s)
    m = json.loads((s.p.state / "pushes" / f"{bid}.json").read_text())
    assert m["bad_tip"] == {"tip": tip, "fresh": 1, "cmd": check}, m
    assert m["outcome"] == "pushed" and m["checks"]["runs"] == 1, log(bid)
    _pq_tend(s)
    assert db.task(fix)["status"] == "done" and _git_out(s.origin, "show", "proj:fix.txt") == "fix"
    assert "bad_tip" not in db.kv("push_queue") and not alerts.holds(db, pushq.BAD_TIP_ALERT, 0, time.time())
    bid = _pq_batch(s)
    _pq_tend(s)
    assert db.task(s.review)["status"] == "done", log(bid)
    assert len(alerted()) == 1


def test_a_fix_behind_an_entry_that_does_not_fix_a_known_failing_tip_lands_in_the_next_batch(env, monkeypatch):
    """On a known-bad tip, a batch [e1, fix, bad] fails at prefix 1 and ends tip_failed. Only e1 is then
    tried on that tip: fix and bad stay untried, go first in the next batch at once (not held), and that
    batch pushes fix, blames bad, and pushes e1 too once it passes on the fixed tip without bad."""
    from ttp import pushq
    s = _pq_real(env, monkeypatch, push_min_gap_s=0, push_batch_max=3,
                 push_checks=["test ! -e broken.txt || test -e fix.txt", "test ! -e bad.txt"])
    db = s.p.db
    _commit(s.other, "broken.txt", "x\n")
    _git_out(s.other, "push", "-q", "origin", "HEAD:proj")
    _git_out(s.repo, "fetch", "-q", "origin")
    tip = _git_out(s.origin, "rev-parse", "proj")
    db.set_kv("push_queue", {"bad_tip": {"tip": tip, "fails": pushq.TIP_FAILS, "tried": [], "at": time.time(),
                                         "alerted": True}})
    _pq_hand_off(env, s)
    tasks = {}
    for name in ("fix", "bad"):
        code, path, branch = _code_task(s.p, name)
        _commit(path, f"{name}.txt", f"{name}\n")
        tasks[name] = db.add_task(f"review {name}", "r", kind="review", tier="standard", origin="coordinator",
                                  depends_on=[code])
        _pq_hand_off(env, s, task=tasks[name], push=[{"branch": branch, "head": _git_out(path, "rev-parse", "HEAD")}])
    row = {r["task"]: r["id"] for r in db.q("SELECT id, task FROM push_queue")}
    log = lambda bid: (s.p.state / "pushes" / f"{bid}.log").read_text()
    bid = _pq_batch(s)
    m = json.loads((s.p.state / "pushes" / f"{bid}.json").read_text())
    assert m["outcome"] == "tip_failed" and m["tip_tried"] == [row[s.review]], log(bid)
    _pq_tend(s)
    assert db.kv("push_queue")["bad_tip"]["tried"] == [row[s.review]]
    rows, why = pushq.due(s.p, time.time() + 60)
    assert [r["task"] for r in rows] == [tasks["fix"], tasks["bad"], s.review], why
    bid = _pq_batch(s)
    m = json.loads((s.p.state / "pushes" / f"{bid}.json").read_text())
    assert m["outcome"] == "pushed" and _statuses(m) == ["pushed", "check_failed", "pushed"], log(bid)
    _pq_tend(s)
    assert _git_out(s.origin, "show", "proj:fix.txt") == "fix" and "bad_tip" not in db.kv("push_queue")
    assert db.task(tasks["fix"])["status"] == "done" and db.task(tasks["bad"])["status"] != "done"
    assert db.task(s.review)["status"] == "done", log(bid)


def test_a_failing_push_tip_is_forgotten_once_the_branch_moves(env, monkeypatch):
    """The known-bad tip and its alert end when the branch moves past it, even with no batch to run."""
    from ttp import alerts, pushq
    s = _pq(env, monkeypatch)
    tip = _git_out(s.repo, "rev-parse", "refs/remotes/origin/proj")
    s.p.db.set_kv("push_queue", {"bad_tip": {"tip": tip, "fails": 2, "tried": [], "at": time.time(), "alerted": True}})
    pushq.schedule(s.p)
    assert alerts.holds(s.p.db, pushq.BAD_TIP_ALERT, time.time(), time.time()), "same tip: still failing"
    _commit(s.other, "theirs.txt", "theirs\n")
    _git_out(s.other, "push", "-q", "origin", "HEAD:proj")
    _git_out(s.repo, "fetch", "-q", "origin")
    pushq.schedule(s.p)
    assert "bad_tip" not in s.p.db.kv("push_queue")
    assert not alerts.holds(s.p.db, pushq.BAD_TIP_ALERT, time.time(), time.time())


@pytest.mark.parametrize("outcome, row, detail, passed, cleared", [
    ("nothing", "landed", {}, False, False),                             # its only entry is already on the tip
    ("landed", "landed", {}, False, False),
    ("nothing", "check_failed", {"cmd": "(stale plugin version)"}, False, False),   # the version guard
    ("nothing", "check_failed", {"cmd": "make test"}, True, True),       # the tip passed alone; the entry failed
    ("pushed", "pushed", {}, False, True),
])
def test_a_known_failing_push_tip_is_kept_until_a_batch_shows_it_passing(env, monkeypatch, outcome, row, detail,
                                                                       passed, cleared):
    """A batch on the known-bad tip that never checked it (nothing carried, or the version guard blamed
    the first entry) keeps the tip known bad and its alert up; the next batches do not check it alone."""
    from ttp import alerts, pushq
    s = _pq(env, monkeypatch)
    tip = _git_out(s.repo, "rev-parse", "refs/remotes/origin/proj")
    s.p.db.set_kv("push_queue", {"bad_tip": {"tip": tip, "fails": 2, "tried": [], "at": time.time(), "alerted": True}})
    _pq_plan(s, outcome=outcome, tip=tip, row=row, detail=detail, tip_passed=passed,
             sha="ab" * 20 if outcome == "pushed" else None)
    _pq_hand_off(env, s)
    _pq_batch(s)
    _pq_tend(s)
    st = s.p.db.kv("push_queue")
    assert ("bad_tip" not in st) == cleared, st
    assert alerts.holds(s.p.db, pushq.BAD_TIP_ALERT, time.time(), time.time()) != cleared
    if not cleared:
        assert st["bad_tip"]["tip"] == tip and st["bad_tip"]["fails"] == 2


@pytest.mark.parametrize("outcome, tries", [("moved", 1), ("busy", 0)])
def test_a_batch_that_could_not_push_puts_its_approvals_back_without_a_model_run(env, monkeypatch, outcome, tries):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_plan(s, outcome=outcome, row="requeued")
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": tries}
    assert s.p.db.task(s.review)["status"] == "pushing"
    assert not [e for e in _pq_events(s.p, mark) if e["status"] == "queued"]
    assert s.p.db.one("SELECT COUNT(*) n FROM runs")["n"] == 0
    assert pushq.due(s.p, time.time() + 10 ** 6)[0], "it goes out with the next batch"


def test_dead_batches_retry_with_a_growing_pause_and_three_in_a_row_raise_an_alert(env, monkeypatch):
    from ttp import alerts, pushq
    s = _pq(env, monkeypatch)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    for n in (1, 2, 3):
        before = time.time()
        _pq_batch(s)
        _pq_tend(s)
        assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": n}
        st = s.p.db.kv(pushq.KV)
        assert st["deaths"] == n and st["backoff_until"] >= before + min(1800, 300 * n)
        assert pushq.due(s.p, time.time())[0] == []
        assert not list((s.p.state / "locks").glob("push:run-*")), "a dead batch's run lock file stays"
        sent = s.p.db.q("SELECT severity FROM messages WHERE kind='alert' AND ref='push_queue_dying'")
        assert len(sent) == (1 if n == 3 else 0)
    assert [b["outcome"] for b in s.p.db.q("SELECT outcome FROM push_batches")] == ["died"] * 3
    assert not [e for e in _pq_events(s.p, mark) if e["status"] == "queued"]
    assert s.p.db.task(s.review)["status"] == "pushing" and s.p.db.one("SELECT COUNT(*) n FROM runs")["n"] == 0
    assert alerts.holds(s.p.db, "push_queue_dying", before, time.time())
    _pq_plan(s, outcome="pushed", sha="ab" * 20)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.task(s.review)["status"] == "done"
    assert [ep["key"] for ep in alerts.sweep(s.p.db)] == ["push_queue_dying"], "a finished batch clears the alert"


def _pq_boot(p, ts):
    """Record a host boot at `ts`, as the daemon does once it is back (Daemon._tell_reboot)."""
    p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status) VALUES(?,?,?,?,?,?,?,?)",
           (ts, "host", "boot", f"boot:{ts}", "normal", "the host rebooted (abrupt)", json.dumps({"boot_time": ts}),
            "record"))


@pytest.mark.parametrize("boot", ["inside", "before", "none"])
def test_a_batch_cut_short_by_a_reboot_runs_again_at_once_without_backoff(env, monkeypatch, boot):
    """A boot inside the batch's run: 'cut short by the HH:MM reboot', no try counted, no backoff, no
    death toward push_queue_dying, and due() takes it again at once (no gap). A batch that died
    with no boot inside its run (a kill) keeps the old path."""
    from ttp import pushq, timefmt
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(pushq.runner, "boot_time", lambda: None)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    if boot == "before":
        _pq_boot(s.p, time.time() - 60)
    bid = _pq_batch(s)
    started = s.p.db.one("SELECT started FROM push_batches WHERE id=?", (bid,))["started"]
    booted = time.time()   # after the batch started, before the daemon found it dead
    if boot == "inside":
        _pq_boot(s.p, booted)
    _pq_tend(s)
    st = s.p.db.kv(pushq.KV) or {}
    [ev] = [e for e in _pq_events(s.p, mark) if e["kind"] == "push_batch_died"]
    outcome = s.p.db.one("SELECT outcome FROM push_batches WHERE id=?", (bid,))["outcome"]
    if boot == "inside":
        assert f"cut short by the {timefmt.short(booted, s.p)} reboot" in ev["text"] and "at once" in ev["text"]
        assert ev["status"] == "handled" and outcome == "rebooted"
        assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": 0}
        assert not st.get("deaths") and not st.get("backoff_until")
        rows, why = pushq.due(s.p, time.time())
        assert rows and why == f"a rerun of {bid}, cut short by a reboot"
        mk = json.loads(__import__("pathlib").Path(s.p.db.one("SELECT marker FROM push_batches WHERE id=?", (bid,))["marker"]).read_text())
        assert mk["status"] == "finished" and mk["outcome"] == "rebooted" and mk["ended"]
        again = _pq_batch(s)
        mk = json.loads(__import__("pathlib").Path(s.p.db.one("SELECT marker FROM push_batches WHERE id=?",
                                                              (bid,))["marker"]).read_text())
        assert mk["rerun_by"] == again
    else:
        assert "ended before writing an outcome" in ev["text"] and outcome == "died"
        assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": 1}
        assert st["deaths"] == 1 and st["backoff_until"] > time.time()
        assert pushq.due(s.p, time.time())[0] == []


def test_reboot_cut_batches_alone_raise_no_dying_alert(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(pushq.runner, "boot_time", lambda: None)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    for _ in range(pushq.DYING_AFTER + 1):
        _pq_batch(s)
        _pq_boot(s.p, time.time())
        _pq_tend(s)
    assert not s.p.db.q("SELECT id FROM messages WHERE kind='alert' AND ref='push_queue_dying'")
    assert s.p.db.one("SELECT tries FROM push_queue")["tries"] == 0


def test_nothing_landing_for_hours_alerts_once_with_counts_and_clears_on_landing(env, monkeypatch):
    from ttp import alerts, pushq
    from ttp.daemon import Daemon
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(pushq.runner, "boot_time", lambda: None)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    _pq_batch(s)
    _pq_boot(s.p, time.time())
    _pq_tend(s)
    d = Daemon(s.p.base)
    later = time.time() + 5 * 3600
    assert not pushq.check_landing(s.p, d.cfg, d.alert, now=later), "one batch is not yet 'despite retries'"
    _pq_batch(s)          # killed, no reboot
    _pq_tend(s)
    assert not pushq.check_landing(s.p, d.cfg, d.alert, now=time.time() + 3 * 3600), "under 4 h"
    assert pushq.check_landing(s.p, d.cfg, d.alert, now=later)
    assert not pushq.check_landing(s.p, d.cfg, d.alert, now=later + 3600), "once per stall"
    [msg] = s.p.db.q("SELECT text, severity FROM messages WHERE kind='alert' AND ref=?", (pushq.LANDING_ALERT,))
    assert msg["severity"] == "high" and "Nothing has landed on origin/proj" in msg["text"]
    assert "2 push batches tried, 1 of them cut short by a host reboot" in msg["text"]
    assert alerts.holds(s.p.db, pushq.LANDING_ALERT, time.time() - 1, time.time())
    _pq_plan(s, outcome="pushed", sha="ab" * 20)
    _pq_batch(s)
    _pq_tend(s)
    assert pushq.LANDING_ALERT in [ep["key"] for ep in alerts.sweep(s.p.db)], "a landing clears it"


def test_the_landing_alert_is_off_at_zero(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch, push_landing_alert_h=0)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    for _ in range(3):
        _pq_batch(s)
        _pq_tend(s)
    assert not pushq.check_landing(s.p, now=time.time() + 10 ** 6)


def test_a_daemon_restart_mid_batch_finalizes_from_the_marker(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    release = env["tmp"] / "go"
    _pq_plan(s, release=str(release), outcome="pushed", sha="ab" * 20, version="1.0.1")
    _pq_hand_off(env, s)
    bid = _pq_batch(s, wait=False)
    _pq_tend(s)
    assert s.p.db.task(s.review)["status"] == "pushing"
    child = pushq._children.pop(bid)   # the daemon that started it is gone
    release.write_text("")
    child.wait(timeout=30)
    _pq_tend(s)                        # a new daemon reads the marker
    t = s.p.db.task(s.review)
    assert t["status"] == "done" and json.loads(t["result"])["pushed"][0]["version"] == "1.0.1"


def test_the_push_queue_and_the_real_batch_process_agree_on_the_marker(env, monkeypatch):
    """The daemon's side (pushq) starts the real `ttp push --batch`, which pushes the approved head;
    finalize closes the review from the marker the batch wrote, and nothing of the batch is left."""
    from ttp import pushq
    s = _pq_real(env, monkeypatch)
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    assert pushq._children[bid].returncode == 0, (s.p.state / "pushes" / f"{bid}.log").read_text()
    m = json.loads((s.p.state / "pushes" / f"{bid}.json").read_text())
    assert (m["status"], m["phase"], m["outcome"], m["rounds"]) == ("finished", "finished", "pushed", 1), m
    assert [(r["task"], r["status"]) for r in m["results"]] == [(s.review, "pushed")]
    _pq_tend(s)
    sha = _git_out(s.origin, "rev-parse", "proj")
    assert _git_out(s.origin, "show", "proj:feature.txt") == "feature"
    t = s.p.db.task(s.review)
    assert t["status"] == "done" and json.loads(t["result"])["pushed"][0]["sha"] == sha
    assert s.p.db.one("SELECT outcome, pushed_sha, after_push FROM push_batches") == {
        "outcome": "pushed", "pushed_sha": sha, "after_push": "skipped"}
    assert not list((s.p.state / "locks").glob(f"*run-{bid}*"))


def test_the_push_queue_resumes_a_cut_short_after_push_with_the_real_batch_process(env, monkeypatch):
    """A batch that died in after_push (a reboot) leaves its marker with an outcome, phase after_push
    and a free after_push:run lock. The queue starts the real batch once more under that same lock;
    it deploys the pushed commit only and finishes the marker."""
    from ttp import pushq
    deploys = env["tmp"] / "deploys.log"
    s = _pq_real(env, monkeypatch, after_push=[f"git rev-parse HEAD >> {deploys}"])
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    marker = s.p.state / "pushes" / f"{bid}.json"
    m = json.loads(marker.read_text())
    sha = m["pushed_sha"]
    assert m["after_push"]["status"] == "ok" and deploys.read_text().split() == [sha]
    # Cut short: as the batch left it once it handed over to its after_push lock.
    lock = s.p.state / "locks" / f"after_push:run-{bid}.0.lock"
    lock.write_text("")
    from ttp.project import write_json
    write_json(marker, {**{k: v for k, v in m.items() if k not in ("after_push", "ended")},
                        "status": "running", "phase": "after_push", "lock": str(lock)})
    _pq_tend(s)                    # applies the outcome, then runs the after_push once more
    assert s.p.db.task(s.review)["status"] == "done"
    child = pushq._children[bid]
    child.wait(timeout=60)
    assert child.returncode == 0, (s.p.state / "pushes" / f"{bid}.log").read_text()
    _pq_tend(s)
    m = json.loads(marker.read_text())
    assert (m["status"], m["phase"], m["after_push"]["status"]) == ("finished", "finished", "ok"), m
    assert deploys.read_text().split() == [sha, sha] and _git_out(s.origin, "rev-parse", "proj") == sha
    assert s.p.db.one("SELECT after_push, after_tries FROM push_batches") == {"after_push": "ok", "after_tries": 1}
    assert not lock.exists()


def test_after_push_failures_are_reported_and_a_deploy_cut_short_runs_once_more(env, monkeypatch):
    from ttp import alerts
    s = _pq(env, monkeypatch, after_push="./deploy.sh")
    _pq_plan(s, outcome="pushed", sha="ab" * 20, after="failed")
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.task(s.review)["status"] == "done", "the review stays done whatever the deploy does"
    assert s.p.db.one("SELECT after_push FROM push_batches")["after_push"] == "failed"
    [ev] = [e for e in _pq_events(s.p, mark) if e["status"] == "queued"]
    assert ev["kind"] == "after_push_failed" and "deploy said failed" in ev["text"]
    assert s.p.db.one("SELECT severity FROM messages WHERE kind='alert' AND ref='after_push_failed'")["severity"] == "high"
    # Cut short (a reboot) and cut short again when run once more: recorded as killed.
    rid, _ = _pq_review(s)
    _pq_plan(s, outcome="pushed", sha="cd" * 20, after="ok", die="after", die_resume=True)
    bid = _pq_batch(s)
    _pq_tend(s)   # applies the outcome and runs the after_push step once more
    assert s.p.db.task(rid)["status"] == "done"
    from ttp import pushq
    pushq._children[bid].wait(timeout=30)
    _pq_tend(s)
    assert s.p.db.one("SELECT after_push, after_tries FROM push_batches WHERE id=?", (bid,)) == {
        "after_push": "killed", "after_tries": 1}
    assert (s.p.state / "pushes" / f"{bid}.starts").read_text().split() == ["push", "after_push"]
    assert [e["kind"] for e in _pq_events(s.p, mark) if e["status"] == "queued"] == ["after_push_failed"] * 2
    # Cut short once, then it works: one handled event, and the alert clears.
    rid, _ = _pq_review(s)
    _pq_plan(s, outcome="pushed", sha="ef" * 20, after="ok", die="after", resume_after="ok")
    bid = _pq_batch(s)
    _pq_tend(s)
    pushq._children[bid].wait(timeout=30)
    _pq_tend(s)
    assert s.p.db.one("SELECT after_push FROM push_batches WHERE id=?", (bid,))["after_push"] == "ok"
    assert [e["status"] for e in _pq_events(s.p, mark) if e["kind"] == "after_push_ok"] == ["handled"]
    assert "after_push_failed" in [ep["key"] for ep in alerts.sweep(s.p.db)]


def _pq_pushing(s, sha, row="pushed", at=None):
    """The plan of a batch that dies after it began pushing `sha`, before writing its outcome."""
    rows = s.p.db.q("SELECT id, task FROM push_queue WHERE status='approved'")
    _pq_plan(s, die="push", pushing={"sha": sha, "tip": _git_out(s.origin, "rev-parse", "proj"), "version": "1.0.2",
                                     "at": time.time() if at is None else at, "checks": {"runs": 1, "seconds": 2.0}, "rounds": 1,
                                     "results": [{"id": r["id"], "task": r["task"], "status": row, "sha": sha,
                                                  "detail": {}} for r in rows]})


def test_a_batch_that_died_after_its_push_landed_counts_as_pushed_and_runs_its_after_push(env, monkeypatch):
    """A reboot between the batch's `git push` and its outcome: the head it was pushing is on the
    branch, so the queue settles the batch as pushed from what it recorded before the push. Its
    approvals are not queued again (no second push), the review is done, and its after_push runs."""
    from ttp import pushq
    s = _pq(env, monkeypatch, after_push="./deploy.sh")
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    sha = _git_out(s.origin, "rev-parse", "proj")   # what the dead batch pushed is the branch tip
    _pq_pushing(s, sha)
    bid = _pq_batch(s)
    _pq_tend(s)
    t = s.p.db.task(s.review)
    assert t["status"] == "done" and json.loads(t["result"])["pushed"][0]["sha"] == sha
    assert s.p.db.one("SELECT status, tries, landed_sha FROM push_queue") == {
        "status": "pushed", "tries": 0, "landed_sha": sha}
    assert s.p.db.one("SELECT outcome, pushed_sha, version FROM push_batches") == {
        "outcome": "pushed", "pushed_sha": sha, "version": "1.0.2"}
    assert pushq.due(s.p, time.time() + 10 ** 6)[0] == [], "nothing goes out again"
    assert (s.p.db.kv(pushq.KV) or {}).get("deaths", 0) == 0
    pushq._children[bid].wait(timeout=30)            # the after_push, run once more
    _pq_tend(s)
    assert s.p.db.one("SELECT after_push, after_tries FROM push_batches") == {"after_push": "ok", "after_tries": 1}
    assert (s.p.state / "pushes" / f"{bid}.starts").read_text().split() == ["push", "after_push"]
    evs = _pq_events(s.p, mark)
    assert [e["status"] for e in evs if e["kind"] == "push_batch_recovered"] == ["handled"]
    assert not [e for e in evs if e["status"] == "queued"], "recovered: the coordinator is not woken"
    assert not [e for e in evs if e["kind"] == "push_batch_died"]


def test_a_batch_that_died_before_its_push_landed_puts_each_approval_back_once(env, monkeypatch):
    """The dead batch had begun pushing, but its head is not on the branch: its approvals go back to
    the queue exactly once and are never called landed; the next batch pushes them."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _pq_pushing(s, s.head)                          # the reviewed head was never pushed
    _pq_batch(s)
    _pq_tend(s)
    _pq_tend(s)                                     # a second tick changes nothing
    assert s.p.db.one("SELECT status, tries, landed_sha FROM push_queue") == {
        "status": "approved", "tries": 1, "landed_sha": None}
    assert s.p.db.task(s.review)["status"] == "pushing"
    assert s.p.db.one("SELECT outcome FROM push_batches")["outcome"] == "died"
    assert [e["status"] for e in _pq_events(s.p, mark) if e["kind"] == "push_batch_died"] == ["handled"]
    assert len(pushq.due(s.p, time.time() + 10 ** 6)[0]) == 1
    _pq_plan(s, outcome="pushed", sha="ab" * 20)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.task(s.review)["status"] == "done"
    assert s.p.db.one("SELECT COUNT(*) n FROM push_queue")["n"] == 1


def test_a_batch_that_died_mid_push_waits_for_an_unreadable_branch_then_tells_the_coordinator_once(env, monkeypatch):
    """Whether the dead batch's push landed cannot be told while the remote cannot be read: the batch
    waits (its approvals neither pushed again nor called landed). Past RECOVER_S it counts as died,
    its approvals go back, and the coordinator gets one event, since an after_push may be missing."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _git_out(s.p.root, "remote", "set-url", "origin", str(env["tmp"] / "gone.git"))
    _pq_pushing(s, s.head)
    _pq_batch(s)
    pushq._recover_asked.clear()
    _pq_tend(s)
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "batched", "tries": 0}
    assert s.p.db.one("SELECT finalized FROM push_batches")["finalized"] is None
    assert pushq.due(s.p, time.time() + 10 ** 6)[0] == []
    later = time.time() + pushq.RECOVER_S + 1
    pushq.finalize(s.p, now=later)
    pushq.finalize(s.p, now=later + 600)
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": 1}
    [ev] = [e for e in _pq_events(s.p, mark) if e["status"] == "queued"]
    assert ev["kind"] == "push_batch_died" and "could not be read" in ev["text"] and "after_push" in ev["text"]


def test_a_batch_that_died_mid_push_before_a_long_outage_is_still_recovered_once_the_remote_answers(env, monkeypatch):
    """The host was down longer than RECOVER_S after the batch began `git push`, and the network is not
    up on the first look: the window counts from that look, not from the push, so a later look that
    finds the head on the branch settles it as pushed, with no coordinator event and one after_push."""
    from ttp import pushq
    monkeypatch.setattr(pushq, "RECOVER_GAP_S", 0)
    s = _pq(env, monkeypatch, after_push="./deploy.sh")
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    sha = _git_out(s.origin, "rev-parse", "proj")
    url = _git_out(s.p.root, "remote", "get-url", "origin")
    _pq_pushing(s, sha, at=time.time() - pushq.RECOVER_S - 3600)
    bid = _pq_batch(s)
    _git_out(s.p.root, "update-ref", "-d", "refs/remotes/origin/proj")   # only a fetch can tell
    _git_out(s.p.root, "remote", "set-url", "origin", str(env["tmp"] / "gone.git"))
    _pq_tend(s)
    assert s.p.db.one("SELECT finalized FROM push_batches")["finalized"] is None, "an old push is not died at once"
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "batched", "tries": 0}
    _git_out(s.p.root, "remote", "set-url", "origin", url)   # the network is back
    _pq_tend(s)
    assert s.p.db.one("SELECT outcome, pushed_sha FROM push_batches") == {"outcome": "pushed", "pushed_sha": sha}
    assert s.p.db.task(s.review)["status"] == "done"
    pushq._children[bid].wait(timeout=30)
    _pq_tend(s)
    assert s.p.db.one("SELECT after_push, after_tries FROM push_batches") == {"after_push": "ok", "after_tries": 1}
    assert (s.p.state / "pushes" / f"{bid}.starts").read_text().split() == ["push", "after_push"]
    evs = _pq_events(s.p, mark)
    assert not [e for e in evs if e["status"] == "queued"], "recovered: the coordinator is not woken"
    assert not [e for e in evs if e["kind"] == "push_batch_died"]


def test_the_mid_push_recovery_window_counts_from_the_first_look_and_survives_a_restart(env, monkeypatch):
    """The first look is kept in the marker (pushing.first_checked): a restart between looks (the
    daemon's memory gone) neither restarts the window nor ends it early, and past RECOVER_S from that
    look the batch still dies `unverified`, with one event for the coordinator."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _git_out(s.p.root, "remote", "set-url", "origin", str(env["tmp"] / "gone.git"))
    _pq_pushing(s, s.head, at=time.time() - pushq.RECOVER_S - 3600)
    bid = _pq_batch(s)
    pushq._recover_asked.clear()
    _pq_tend(s)
    marker = s.p.state / "pushes" / f"{bid}.json"
    first = json.loads(marker.read_text())["pushing"]["first_checked"]
    assert time.time() - 60 < first <= time.time()
    pushq._recover_asked.clear()                    # a restart: nothing kept in memory
    pushq.finalize(s.p, now=first + pushq.RECOVER_S - 60)
    assert json.loads(marker.read_text())["pushing"]["first_checked"] == first
    assert s.p.db.one("SELECT finalized FROM push_batches")["finalized"] is None
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "batched", "tries": 0}
    pushq._recover_asked.clear()
    pushq.finalize(s.p, now=first + pushq.RECOVER_S + 1)
    assert s.p.db.one("SELECT outcome FROM push_batches")["outcome"] == "died"
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": 1}
    [ev] = [e for e in _pq_events(s.p, mark) if e["status"] == "queued"]
    assert ev["kind"] == "push_batch_died" and "could not be read" in ev["text"]


def test_a_real_batch_records_what_it_pushes_before_pushing_and_recovers_after_a_reboot(env, monkeypatch):
    """The real batch writes `pushing` (its head and each entry's result if it lands) before `git
    push`, matching what it writes once pushed. Cut off right after the push (the marker as a reboot
    leaves it, with the run lock and the target lock files still labelled), the queue settles it as
    pushed and runs the after_push once; the stale lock files hold nothing up for the next batch."""
    from ttp import pushq
    from ttp.project import write_json
    s = _pq_real(env, monkeypatch)
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    marker = s.p.state / "pushes" / f"{bid}.json"
    m = json.loads(marker.read_text())
    sha = _git_out(s.origin, "rev-parse", "proj")
    assert m["outcome"] == "pushed" and m["pushing"]["sha"] == sha == m["pushed_sha"], m
    assert m["pushing"]["results"] == m["results"]
    # As a reboot leaves it: no outcome, phase push, the run lock and target lock files labelled but free.
    run_lock = s.p.state / "locks" / f"push:run-{bid}.0.lock"
    for path in (run_lock, s.p.state / "locks" / "push:origin%2Fproj.0.lock"):
        path.write_text(json.dumps({"holder": f"push batch {bid}", "since": time.time()}))
    gone = ("outcome", "results", "pushed_sha", "version", "tip", "checks", "rounds", "message", "conflicts",
            "reach", "after_push", "ended")
    write_json(marker, {**{k: v for k, v in m.items() if k not in gone},
                        "status": "running", "phase": "push", "lock": str(run_lock)})
    deploys = env["tmp"] / "deploys.log"
    s.p.set_config("delivery.after_push", [f"git rev-parse HEAD >> {deploys}"])
    _pq_tend(s)
    t = s.p.db.task(s.review)
    assert t["status"] == "done" and json.loads(t["result"])["pushed"][0]["sha"] == sha
    pushq._children[bid].wait(timeout=60)
    _pq_tend(s)
    assert deploys.read_text().split() == [sha]
    assert s.p.db.one("SELECT outcome, after_push FROM push_batches") == {"outcome": "pushed", "after_push": "ok"}
    assert not run_lock.exists()
    # The next approval goes out past the stale target lock file; the first is not pushed again.
    _commit(s.path, "more.txt", "more\n")
    rid, _ = _pq_review(s)
    with s.p.db.tx():
        s.p.db.x("UPDATE push_queue SET head=? WHERE task=?", (_git_out(s.path, "rev-parse", "HEAD"), rid))
    bid2 = _pq_batch(s)
    assert pushq._children[bid2].returncode == 0, (s.p.state / "pushes" / f"{bid2}.log").read_text()
    _pq_tend(s)
    assert s.p.db.task(rid)["status"] == "done"
    assert _git_out(s.origin, "log", "--format=%s", f"{sha}..proj").count("feature") == 0


@pytest.mark.parametrize("delivery, on", [
    ({"push_queue": True, "push_branch": "origin/proj"}, True),
    ({"push_queue": True, "push_branch": "origin/proj", "push_allowed": "yes"}, True),
    ({"push_queue": "yes", "push_branch": "origin/proj"}, False),
    ({"push_queue": 1, "push_branch": "origin/proj"}, False),
    ({"push_queue": True, "push_branch": " "}, False),
    ({"push_queue": True}, False),
    ({"push_queue": True, "push_branch": "origin/proj", "push_allowed": False}, False),
    ({"push_queue": True, "push_branch": "origin/proj", "push_allowed": "no"}, False),
    ({"push_queue": False, "push_branch": "origin/proj"}, False),
])
def test_the_daemon_and_the_review_prompt_agree_on_whether_the_push_queue_is_on(env, delivery, on):
    from ttp import pushq
    from ttp.project import push_queue_on
    p = make(env)
    cfg = {"delivery": delivery}
    assert push_queue_on(cfg) is on and pushq.enabled(p, cfg) is on
    for k, v in delivery.items():
        p.set_config(f"delivery.{k}", v)
    assert pushq.enabled(p) is on
    review = p.db.add_task("review it", "s", kind="review", tier="light", origin="user")
    from ttp.prompts import worker_task
    policy = next(x for x in worker_task(p, p.db.task(review), str(p.root), None).splitlines()
                  if x.startswith("delivery policy:"))
    assert ("push queue=True" in policy) is on
    if not on:   # an approval made anyway is not queued
        check = pushq.check_approval(p, p.db.task(review), [{"branch": "b", "head": "ab" * 20}])
        assert "entries" not in check, check


def test_an_after_push_failed_alert_clears_once_after_push_is_unset_not_when_the_queue_is_off(env, monkeypatch):
    from ttp import alerts, pushq
    s = _pq(env, monkeypatch, after_push="./deploy.sh")
    _pq_plan(s, outcome="pushed", sha="ab" * 20, after="failed")
    _pq_hand_off(env, s)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.one("SELECT after_push FROM push_batches")["after_push"] == "failed"
    raised = s.p.db.one("SELECT raised FROM alerts WHERE key='after_push_failed' AND cleared IS NULL")["raised"]
    assert alerts.sweep(s.p.db) == [] and alerts.holds(s.p.db, "after_push_failed", raised, time.time())
    s.p.set_config("delivery.after_push", None)
    _pq_tend(s)
    assert s.p.db.kv(pushq.AFTER_PUSH_OFF) is True
    assert [ep["key"] for ep in alerts.sweep(s.p.db)] == ["after_push_failed"], "after_push unset clears it"
    s.p.set_config("delivery.after_push", "./deploy.sh")
    _pq_tend(s)
    assert s.p.db.kv(pushq.AFTER_PUSH_OFF) is False
    assert alerts.holds(s.p.db, "after_push_failed", raised, time.time()), "set again, the failure still stands"
    s.p.set_config("delivery.push_queue", False)
    _pq_tend(s)
    assert alerts.holds(s.p.db, "after_push_failed", raised, time.time()), \
        "with the queue off ordinary pushes still run after_push: the failure still stands"
    s.p.db.set_kv(pushq.AFTER_PUSH_OK, time.time())   # an ordinary push's after_push succeeded
    assert not alerts.holds(s.p.db, "after_push_failed", raised, time.time())


def test_approvals_ask_the_remote_for_its_default_branch_once_per_hour_not_once_each(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    pushq._default_branches.clear()
    asked, real = [], pushq._git

    def git(p, *args, **kw):
        if args[0] == "ls-remote":
            asked.append(args)
        return real(p, *args, **kw)
    monkeypatch.setattr(pushq, "_git", git)
    entries = [{"branch": s.branch, "head": s.head}]
    for _ in range(5):
        assert pushq.check_approval(s.p, s.p.db.task(s.review), entries).get("entries"), "approved"
    assert len(asked) == 1, "five approvals in one tick, one ls-remote"
    # The remote's default branch becomes the target: refused once the cached answer expires.
    subprocess.run(["git", "-C", str(s.origin), "symbolic-ref", "HEAD", "refs/heads/proj"], check=True)
    now = time.time()
    assert pushq._default_branch(s.p, "origin", now + 60) != "proj" and len(asked) == 1
    assert pushq._default_branch(s.p, "origin", now + pushq.DEFAULT_BRANCH_TTL_S + 1) == "proj" and len(asked) == 2
    assert "the remote's default branch" in pushq.check_approval(s.p, s.p.db.task(s.review), entries)["invalid"]
    # An unreachable remote does not refuse, and is asked again only after a short pause.
    s.p.set_config("delivery.push_branch", "gone/proj")
    for _ in range(3):
        assert pushq._refusal(s.p, "gone", "proj") == ""
    assert len(asked) == 3
    assert pushq._default_branch(s.p, "gone", time.time() + pushq.DEFAULT_BRANCH_RETRY_S + 1) is None
    assert len(asked) == 4


def test_cancelling_or_requeueing_a_pushing_review_withdraws_its_approval(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_hand_off(env, s)
    s.p.db.update_task(s.review, status="cancelled")
    mark = _pq_mark(s.p)
    _pq_tend(s)
    assert s.p.db.one("SELECT status FROM push_queue")["status"] == "cancelled"
    assert [(e["kind"], e["status"]) for e in _pq_events(s.p, mark)] == [("push_cancelled", "handled")]
    assert pushq.schedule(s.p) is None
    # A row already in a batch is not withdrawn: the batch reports it.
    rid, row = _pq_review(s)
    release = env["tmp"] / "go"
    _pq_plan(s, release=str(release), outcome="pushed", sha="ab" * 20)
    bid = _pq_batch(s, wait=False)
    s.p.db.update_task(rid, status="queued")
    _pq_tend(s)
    assert s.p.db.one("SELECT status FROM push_queue WHERE id=?", (row,))["status"] == "batched"
    release.write_text("")
    pushq._children[bid].wait(timeout=30)
    _pq_tend(s)
    assert s.p.db.task(rid)["status"] == "queued"
    [ev] = [e for e in _pq_events(s.p, mark) if e["kind"] == "push_reported"]
    assert ev["status"] == "queued" and "pushed" in ev["text"], "work that landed for a requeued review is news"


def test_task_resources_accept_push_lock_names_in_their_canonical_form(env):
    p = make(env)
    from ttp import coordinator as coord, locks, push
    canon = "push:origin%2Fteam%2Ffeature"   # the scheduler's lock file is the one ttp push holds
    assert locks.slot_paths(p.state / "locks", canon, 1) == push.lock_paths(p, "origin", "team/feature")
    problems = coord.apply(p, [{"type": "task_add", "title": "rev", "spec": "x", "kind": "review",
                                "resources": ["push:origin/team/feature", "push:origin%2Fteam%2Ffeature",
                                              "push:"]}])
    assert len(problems) == 1 and "'push:' dropped" in problems[0]
    labels = json.loads(p.db.one("SELECT labels FROM tasks WHERE title='rev'")["labels"])
    assert set(labels) == {"resource:" + canon}


def test_the_push_queues_batch_process_runs_niced(env, monkeypatch):
    from ttp import pushq
    p = make(env)
    stub = env["tmp"] / "nice_stub.py"
    stub.write_text("import os, sys\nsys.stdin.read()\nprint('nice', os.nice(0))\n")
    monkeypatch.setattr(pushq, "batch_argv", lambda marker: [sys.executable, str(stub)])
    marker = env["tmp"] / "batch.json"
    from ttp import checkdirs
    marker.write_text(json.dumps({"check_run_dir": str(checkdirs.new(p.state))}))
    for level in (4, 0):
        p.set_config("runner.nice", level)
        with open(env["tmp"] / "batch.lock", "w") as lock:
            child = pushq._spawn(p, f"b{level}", marker, lock)
        child.stdin.close()
        assert child.wait(timeout=30) == 0
        assert marker.with_suffix(".log").read_text().split()[-1] == str(min(os.nice(0) + level, 19))


def test_auto_upgrade_waits_for_a_push_then_runs_once_and_notifies_once(env, monkeypatch):
    from ttp import locks, push
    p, d, launches = _release_daemon(env, monkeypatch)
    assert p.config()["upgrade"]["auto"] is True, "on by default"
    held = locks.try_take(push.lock_paths(p, "origin", "feature/x"), "task #1 (run 1)", "ttp push")
    d.check_release()
    assert not launches and p.db.kv("release"), "never during a push"
    assert d._release_due - time.time() < 600, "a held upgrade is looked at again soon, not in an hour"
    held.close()
    d._release_due = 0
    d.check_release()
    assert launches == [p.base] and _harness_commit(p) == "bbbb2222"
    assert p.db.kv("upgrade_auto")["outcome"] == "applied"
    notes = p.db.q("SELECT severity, text FROM messages WHERE text LIKE 'tt-project harness upgraded%'")
    assert len(notes) == 1 and notes[0]["severity"] == "low" and "(aaaa1111) to" in notes[0]["text"]
    for _ in range(2):
        d._release_due = 0
        d.check_release()
    assert launches == [p.base] and not p.db.kv("release")


def test_an_auto_upgrade_that_finds_a_push_at_its_swap_leaves_the_harness_and_retries(env, monkeypatch):
    from ttp import cli, locks, push
    p, d, launches = _release_daemon(env, monkeypatch)
    before = _git_out(p.harness, "rev-parse", "HEAD")
    held = []
    merge_upstream = cli._merge_upstream

    def push_starts_meanwhile(*a, **k):   # a push takes its lock while the upgrade merges
        out = merge_upstream(*a, **k)
        if not held:
            held.append(locks.try_take(push.lock_paths(p, "origin", "feature/x"), "task #1 (run 1)", "ttp push"))
        return out
    monkeypatch.setattr(cli, "_merge_upstream", push_starts_meanwhile)
    d.check_release()
    assert launches == [p.base] and _harness_commit(p) == "aaaa1111"
    assert _git_out(p.harness, "diff", "--stat", before, "HEAD", "--", "runtime") == "", "the live harness moved"
    assert p.db.kv("upgrade_auto")["outcome"] == "held" and p.db.kv("release")
    assert d._release_due - time.time() < 600, "retried soon, not in an hour"
    d._release_due = 0
    d.check_release()
    assert launches == [p.base], "never while the push is still in flight"
    held[0].close()
    d._release_due = 0
    d.check_release()
    assert launches == [p.base, p.base] and _harness_commit(p) == "bbbb2222"
    assert p.db.kv("upgrade_auto")["outcome"] == "applied"


def test_a_pushed_batch_resolves_the_upstream_notes_its_hand_offs_name_with_its_own_version(env, monkeypatch):
    """A code task's hand-off names the upstream notes it fixes (`resolves`); once the push queue's
    batch lands it, each is recorded as fixed in the version that batch pushed. Fingerprints of the
    task it continues count too; malformed ones are skipped; finalizing again records nothing more."""
    from ttp import pushq, upstream
    s = _pq(env, monkeypatch)
    notes = [{"title": f"upstream: n{i}", "spec": "s"} for i in range(3)]
    upstream.append("other", 2, notes)
    fps = [upstream.fingerprint(n["title"], "s") for n in notes]
    first = s.p.db.add_task("first try", "s", kind="code", origin="coordinator")
    s.p.db.update_task(first, status="failed", result=json.dumps({"status": "failed", "resolves": [fps[2]]}))
    s.p.db.update_task(s.code, result=json.dumps({"status": "done", "summary": "fixed",
                                                  "resolves": [fps[0].upper(), "not-a-fp", fps[1]]}),
                       labels=json.dumps([f"continues:{first}"]))
    _pq_plan(s, outcome="pushed", sha="ab" * 20, tip="cd" * 20, version="0.3.1")
    _pq_hand_off(env, s)
    assert upstream.state(fps[0]) == "open", "resolved before the push landed"
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.one("SELECT status FROM push_queue")["status"] == "pushed"
    for fp in fps:
        r = upstream.resolution(fp)
        assert r and r["fixed_in"] == "0.3.1" and r["by"] == "demo", fp
    assert f"#{s.code}" in upstream.resolution(fps[0])["note"]
    assert f"#{first}" in upstream.resolution(fps[2])["note"]
    [ev] = s.p.db.q("SELECT status, severity, text FROM events WHERE kind='upstream_resolved'")
    assert ev["status"] == "handled" and "0.3.1" in ev["text"] and all(fp in ev["text"] for fp in fps)
    n = len(upstream._records())
    pushq._resolve_upstream(s.p, [s.review], "0.3.1")
    _pq_tend(s)
    assert len(upstream._records()) == n, "the same resolution was recorded twice"
    pushq._resolve_upstream(s.p, [s.review], None)
    assert len(upstream._records()) == n, "a batch without a version resolved notes"


def test_a_pushed_batch_resolves_notes_named_only_in_the_specs_of_the_chain(env, monkeypatch):
    """No hand-off lists `resolves`: `[note <fp>]` in the original spec of the continued chain counts;
    an unknown or already-resolved fingerprint is ignored."""
    from ttp import upstream
    s = _pq(env, monkeypatch)
    notes = [{"title": f"upstream: s{i}", "spec": "s"} for i in range(2)]
    upstream.append("other", 2, notes)
    fps = [upstream.fingerprint(n["title"], "s") for n in notes]
    upstream.resolve(fps[1], "0.1.0", "earlier", "demo")
    unknown = "0123456789abcdef"
    first = s.p.db.add_task("first try", f"fix [note {fps[0]}] [note {fps[0].upper()}] [note {fps[1]}] [note {unknown}]",
                            kind="code", origin="coordinator")
    s.p.db.update_task(s.code, result=json.dumps({"status": "done", "summary": "fixed"}),
                       labels=json.dumps([f"continues:{first}"]))
    _pq_plan(s, outcome="pushed", sha="ab" * 20, tip="cd" * 20, version="0.3.1")
    _pq_hand_off(env, s)
    _pq_batch(s)
    _pq_tend(s)
    assert upstream.resolution(fps[0])["fixed_in"] == "0.3.1"
    assert upstream.resolution(fps[1])["fixed_in"] == "0.1.0"
    assert upstream.resolution(unknown) is None


def test_ttp_landed_finds_a_rebased_commit_by_patch_id_or_author_date_and_subject(env, monkeypatch, capsys):
    from ttp import cli, landed
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    raw = f"git merge-base --is-ancestor {head} origin/proj"
    assert subprocess.run(raw, shell=True, cwd=repo).returncode == 1, "the raw probe never passes after a rebase"
    assert landed.probe_command(raw) == f"ttp landed {head} --onto origin/proj"
    assert landed.probe_command("landed:#42") == "ttp landed --task 42" == landed.probe_command(" landed: 42 ")
    assert landed.probe_command("test -f x") == "test -f x"

    def ttp_landed(*args):
        with pytest.raises(SystemExit) as e:
            cli.main(["landed", *args])
        return e.value.code
    assert ttp_landed(head) == 0 and ttp_landed(head[:10], "--onto", "origin/proj") == 0
    assert ttp_landed(f"#{tid}") == 0 and ttp_landed("--task", str(tid)) == 0, "the task's branch, by patch"
    # A settled conflict changes the patch; the same author, date and subject still match.
    _git_out(other, "commit", "-q", "--amend", "--no-edit", "--allow-empty", "--only", "--", ".")
    (other / "feature.txt").write_text("feature, settled\n")
    _git_out(other, "commit", "-q", "--amend", "--no-edit", "-a")
    _git_out(other, "push", "-q", "-f", "origin", "HEAD:proj")
    assert ttp_landed(head) == 0
    # Not landed: a new commit, a commit nowhere, a task with nothing.
    _commit(path, "more.txt", "more\n")
    assert ttp_landed(_git_out(path, "rev-parse", "HEAD")) == 1 and ttp_landed(f"#{tid}") == 1
    assert ttp_landed("0" * 40) == 2 and ttp_landed("--task", "9999") == 2, "no such task: it never lands"
    assert "not on origin/proj yet" in capsys.readouterr().out


def test_a_fetch_then_ancestor_probe_goes_through_ttp_landed_and_a_queued_head_waits_on_its_task(env, monkeypatch):
    """`git fetch -q <remote> <branch> && git merge-base --is-ancestor <sha> FETCH_HEAD` (or
    <remote>/<branch>) never passes once the push queue rebased <sha>: it becomes `ttp landed`, and a
    sha a push-queue row holds as its head waits on the task owning that row's branch."""
    import shlex
    from ttp import cli, landed
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    probes = [f"git fetch -q origin proj && git merge-base --is-ancestor {head[:8]} FETCH_HEAD",
              f"git fetch origin proj&&git merge-base --is-ancestor {head} origin/proj",
              f" git fetch --quiet origin proj  &&  git merge-base --is-ancestor {head} origin/proj "]
    for raw in probes:
        assert subprocess.run(raw, shell=True, cwd=repo, capture_output=True).returncode == 1, raw
        cmd = landed.probe_command(raw)
        assert cmd.startswith("ttp landed ") and cmd.endswith(" --onto origin/proj"), cmd
        with pytest.raises(SystemExit) as e:
            cli.main(shlex.split(cmd)[1:])
        assert e.value.code == 0, "the rebased landing passes"
    # Unrelated compound probes run as written: another ref than the one fetched, more commands.
    for raw in (f"git fetch -q origin proj && git merge-base --is-ancestor {head} origin/other",
                f"git fetch -q origin proj && git merge-base --is-ancestor {head} FETCH_HEAD && rm -f x",
                f"git fetch -q origin proj; git merge-base --is-ancestor {head} FETCH_HEAD",
                "git fetch -q origin proj && test -f x"):
        assert landed.probe_command(raw) == raw == landed.probe_command(raw, p)
    # No push-queue row: the sha probe, even with the project.
    assert landed.probe_command(probes[0], p) == f"ttp landed {head[:8]} --onto origin/proj"
    review = p.db.add_task(f"Review #{tid}", "r", kind="review", origin="coordinator")
    now = time.time()
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated) "
           "VALUES(?, ?, ?, 'origin/proj', 'approved', ?, ?)", (review, branch, head, now, now))
    for raw in probes:
        assert landed.probe_command(raw, p) == f"ttp landed --task {tid}", raw
    assert landed.check(p, repo, tid=tid)[0] == 0
    # Only the push branch waits on the task: another ref keeps its ref and does not pass while the
    # head is only on the push branch; a bare ancestor probe keeps its ref as before.
    bare = f"git merge-base --is-ancestor {head} origin/proj"
    assert landed.probe_command(bare, p) == f"ttp landed {head} --onto origin/proj"
    for raw in (f"git merge-base --is-ancestor {head} origin/other",
                f"git fetch -q origin other && git merge-base --is-ancestor {head} FETCH_HEAD",
                f"git fetch upstream proj && git merge-base --is-ancestor {head} upstream/proj"):
        cmd = landed.probe_command(raw, p)
        assert "--task" not in cmd and cmd.endswith(("--onto origin/other", "--onto upstream/proj")), cmd
        with pytest.raises(SystemExit) as e:
            cli.main(shlex.split(cmd)[1:])
        assert e.value.code != 0, cmd
    # A row whose branch no task owns waits on the row's own task.
    p.db.x("UPDATE push_queue SET branch='gone'")
    assert landed.probe_command(probes[0], p) == f"ttp landed --task {review}"


def test_landed_task_prefers_the_push_queues_recorded_commit_and_waits_until_it_is_on_the_branch(env, monkeypatch):
    """A review's approval the queue landed counts for the code task it reviews: its landed_sha."""
    from ttp import landed
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    (path / "feature.txt").write_text("feature, redone\n")   # its branch no longer matches what landed
    _git_out(path, "commit", "-q", "--amend", "-am", "redone after the landing")
    assert landed.check(p, repo, tid=tid)[0] == 1
    review = p.db.add_task(f"Review #{tid}", "r", kind="review", origin="coordinator")
    rebased = _git_out(other, "rev-parse", "HEAD")
    now = time.time()
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated, pushed_sha, landed_sha) "
           "VALUES(?, 'other-branch', ?, 'origin/proj', 'approved', ?, ?, NULL, NULL)", (review, head, now, now))
    assert landed.check(p, repo, tid=tid)[0] == 1, "an approval still in the queue has not landed"
    p.db.x("UPDATE push_queue SET status='pushed', pushed_sha=?, landed_sha=?", (rebased, rebased))
    assert landed.task_shas(p, tid) == ([rebased], "its push queue landing")
    assert landed.check(p, repo, tid=tid) == (0, f"#{tid} (its push queue landing) is on origin/proj")


@pytest.mark.parametrize("status", ["running", "failed", "cancelled", "done"])
def test_landed_task_never_passes_for_a_task_that_has_not_landed_anything(env, monkeypatch, status):
    """The daemon makes a task's branch at the push branch's tip when it starts, so the branch head is
    already on the branch: that must not read as landed while the task runs, after it failed or was
    cancelled, or when it finished without a commit. A deferral on it stays deferred."""
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    from ttp import landed
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    tid, path, branch = _code_task(p, "feature", status=status)
    assert landed.on_branch(repo, _git_out(path, "rev-parse", "HEAD"), _git_out(repo, "rev-parse", "origin/proj"))
    rc, words = landed.check(p, repo, tid=tid)
    assert rc == 1 and landed.task_shas(p, tid)[0] == [], words
    assert ("no commits on its branch" if status == "done" else f"has not finished ({status})") in words
    assert coord.apply(p, [{"type": "task_add", "title": "after it", "spec": "s",
                            "start_when": f"landed:#{tid}"}]) == []
    after = _added(p, "after it")
    d = dmod.Daemon(p.base)
    _settle_deferred(d, after)
    _settle_deferred(d, after)
    assert not _ready(p, after) and not _events(p, after, "deferral_probe_broken")


def test_landed_task_counts_a_done_tasks_own_commits_once_they_are_on_the_branch(env, monkeypatch):
    """The branch fallback still works for a done task with a commit, landed by fast-forward."""
    from ttp import landed
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    tid, path, branch = _code_task(p, "feature", status="running")
    _commit(path, "feature.txt", "feature\n")
    assert landed.check(p, repo, tid=tid)[0] == 1, "still running"
    p.db.update_task(tid, status="done")
    assert landed.check(p, repo, tid=tid)[0] == 1, "done, not pushed yet"
    _git_out(path, "push", "-q", "origin", "HEAD:proj")
    assert landed.check(p, repo, tid=tid) == (0, f"#{tid} (its branch {branch}) is on origin/proj")


@pytest.mark.parametrize("chain", ["rebase", "review_fix"])
def test_landed_task_follows_a_successor_that_landed_its_work_under_other_patches(env, monkeypatch, chain):
    """A done task's change landed through a successor on a branch of its own with other patches
    (a rebase task that settled a conflict, or a review fix and its re-review): landed:#<orig> passes
    once the successor's push queue entry lands, and a task deferred on it starts."""
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    from ttp import landed
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    tid, path, branch = _code_task(p, "feature")
    _commit(path, "feature.txt", "feature\n")
    review = p.db.add_task(f"Review #{tid}: feature", "r", kind="review", status="done")
    unrelated, upath, ubranch = _code_task(p, "unrelated")
    if chain == "rebase":
        succ, spath, sbranch = _code_task(p, f"rebase #{tid} onto the tip")
        p.db.update_task(succ, labels=json.dumps([f"rebases:{branch}", f"continues:{review}"]))
        lander = p.db.add_task(f"Review #{succ}: rebased #{tid} on the tip", "r", kind="review", status="done")
    else:
        succ, spath, sbranch = _code_task(p, f"Fix review #{review}")
        p.db.update_task(succ, labels=json.dumps([f"continues:{tid}", f"review_fix:{review}"]))
        lander = p.db.add_task(f"Re-review #{review}: feature", "r", kind="review", status="done",
                               labels=[f"auto_review:{succ}", f"continues:{review}"])
    (spath / "feature.txt").write_text("feature, settled\n")   # not the original's patch
    (spath / "fix.txt").write_text("fix\n")
    _git_out(spath, "add", "-A")
    _git_out(spath, "commit", "-qm", "the change, settled on the tip")
    assert landed.check(p, repo, tid=tid)[0] == 1, "the original's own branch is not on the branch"
    assert coord.apply(p, [{"type": "task_add", "title": "after it", "spec": "s",
                            "start_when": f"landed:#{tid}"}]) == []
    after = _added(p, "after it")
    d = dmod.Daemon(p.base)
    _settle_deferred(d, after)
    assert not _ready(p, after)

    def land(task, br, path):
        _git_out(path, "fetch", "-q", "origin")
        _git_out(path, "rebase", "-q", "origin/proj")
        _git_out(path, "push", "-q", "origin", "HEAD:proj")
        sha, now = _git_out(path, "rev-parse", "HEAD"), time.time()
        p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated, pushed_sha, landed_sha) "
               "VALUES(?, ?, ?, 'origin/proj', 'pushed', ?, ?, ?, ?)", (task, br, sha, now, now, sha, sha))
        return sha
    _commit(upath, "unrelated.txt", "unrelated\n")
    land(unrelated, ubranch, upath)
    assert landed.check(p, repo, tid=tid)[0] == 1, "another task's landing is not this one's"
    land(lander, sbranch, spath)
    assert landed.check(p, repo, tid=tid) == (0, f"#{tid} (its push queue landing through #{lander}) is on origin/proj")
    assert landed.check(p, repo, tid=unrelated)[0] == 0 and set(landed.carriers(p.db, unrelated)) == {unrelated}
    _settle_deferred(d, after, until=lambda: _ready(p, after))
    assert _ready(p, after) and not _events(p, after, "deferral_probe_broken")


@pytest.mark.parametrize("result_status", ["waiting", "blocked", "failed", "needs_review"])
def test_a_detached_push_records_a_landing_even_with_a_non_done_handoff(env, monkeypatch, result_status):
    from ttp import landed, push
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    sha = _git_out(other, "rev-parse", "HEAD")
    # Its branch moved after the push: the marker's rebased sha is the evidence, not its head.
    _commit(path, "later.txt", "later\n")
    p.db.update_task(tid, status="running")
    marker = p.state / push.DETACHED / f"t{tid}-push.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"status": "pushed", "sha": sha, "head": head,
                                  "task": str(tid), "target": "origin/proj", "own": False}))
    run_dir = env["tmp"] / "push-run"
    run_dir.mkdir()
    result = {"status": result_status, "summary": "push finished; further work remains",
              "retry_when": "true", "waiting_for": "push", "retry_after_s": 1800}
    (run_dir / "result.json").write_text(json.dumps(result))
    d = Daemon(p.base)
    d._finish_worker({"task": tid, "note": "{}"}, RunUsage(), "ok", run_dir)
    assert landed.check(p, repo, tid=tid)[0] == 0
    assert json.loads(p.db.task(tid)["result"])["status"] == result_status
    d.tend_pushes()
    d.tend_pushes()
    assert p.db.q("SELECT task, sha FROM task_landings") == [{"task": tid, "sha": sha}]
    # Evidence survives marker pruning and removal of the task's worktree/branch.
    marker.unlink()
    p.db.update_task(tid, branch=None)
    assert landed.check(p, repo, tid=tid)[0] == 0


def test_daemon_restart_recovers_a_detached_push_landing_and_releases_its_dependents(env, monkeypatch):
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    from ttp import landed, push
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    p.db.update_task(tid, status="queued", branch=None,
                     result=json.dumps({"status": "waiting", "retry_when": "true"}))
    assert coord.apply(p, [{"type": "task_add", "title": "after detached push", "spec": "s",
                            "start_when": f"landed:#{tid}"}]) == []
    dependent = _added(p, "after detached push")
    dmod.Daemon(p.base)   # stopped before it could record the landing
    marker = p.state / push.DETACHED / f"t{tid}-push.json"
    marker.parent.mkdir()
    sha = _git_out(other, "rev-parse", "HEAD")
    marker.write_text(json.dumps({"status": "pushed", "sha": sha, "task": str(tid),
                                  "target": "origin/proj", "own": False}))
    p.db.x("DROP TABLE task_landings")   # upgrading a daemon whose DB predates landing receipts
    d = dmod.Daemon(p.base)
    d.tend_pushes()
    assert landed.check(p, repo, tid=tid)[0] == 0
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    _settle_deferred(d, dependent, until=lambda: _ready(p, dependent))
    assert _ready(p, dependent)
    dmod.Daemon(p.base).tend_pushes()
    assert p.db.q("SELECT task, sha FROM task_landings") == [{"task": tid, "sha": sha}]


@pytest.mark.parametrize("bad", [
    {"status": "queued"}, {"status": "running"}, {"status": "failed"},
    {"own": True}, {"kind": "batch"}, {"sha": None}, {"sha": "bad"},
    {"task": None}, {"task": "not-a-task"}, {"task": "99999"}, {"target": None},
])
def test_detached_push_landing_ignores_publications_and_incomplete_or_failed_markers(env, monkeypatch, bad):
    from ttp import landed, push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    tid, path, branch = _code_task(p, "feature", status="running")
    marker = p.state / push.DETACHED / f"t{tid}-push.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"status": "pushed", "sha": _git_out(repo, "rev-parse", "HEAD"),
                                  "task": str(tid), "target": "origin/proj", "own": False, **bad}))
    landed.reconcile_pushes(p, tid)
    assert not p.db.q("SELECT * FROM task_landings")
    assert landed.check(p, repo, tid=tid)[0] == 1


def test_a_recorded_detached_push_still_checks_the_push_branch_and_covers_its_reviewed_task(env, monkeypatch):
    from ttp import landed, push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    tid, path, branch = _code_task(p, "feature", status="running")
    _commit(path, "feature.txt", "feature\n")
    sha = _git_out(path, "rev-parse", "HEAD")
    review = p.db.add_task(f"Review #{tid}: feature", "review", kind="review", status="running")
    marker = p.state / push.DETACHED / f"t{review}-push.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"status": "pushed", "sha": sha, "task": str(review),
                                  "target": "origin/proj", "own": False}))
    landed.reconcile_pushes(p, review)
    assert landed.task_shas(p, tid) == ([sha], "its detached push landing")
    assert landed.check(p, repo, tid=tid)[0] == 1, "a receipt never replaces the branch check"
    _git_out(path, "push", "-q", "origin", "HEAD:proj")
    assert landed.check(p, repo, tid=tid)[0] == landed.check(p, repo, tid=review)[0] == 0


@pytest.mark.parametrize("cmd", [
    'h=$(git rev-parse HEAD); test "$h" = 0000 || pytest -q',
    'h="$(git rev-parse HEAD)" && test -n "$h"',
    "h=`git rev-parse HEAD`; true",
    "h=$(cd sub && git rev-parse $(echo HEAD)); pytest -q",
    "set -e; pytest -q",
    "x=1",
    'A=1 B="x y" pytest -q',
    "n=$((1+2)); pytest -q",
    'n="$(( 3 * 4 ))" && test "$n" = 12',
    "h=$(git rev-parse HEAD) pytest -q",
])
def test_check_problem_accepts_a_bare_assignment_holding_a_command_substitution(env, cmd):
    # A scoped check (`h=$(git rev-parse HEAD); ...`) is a command; shlex used to split the
    # substitution into words and reject `rev-parse` as the first one.
    from ttp.push import check_problem
    assert check_problem(cmd) is None


@pytest.mark.parametrize("cmd,why", [
    ("Run all the tests", "'Run' is not a program on PATH"),
    ("h=$(Run the tests); true", "'Run' is not a program on PATH"),
    ("Run=all the tests", "'the' is not a program on PATH"),
    ("h=$(git rev-parse HEAD", "does not parse as a shell command"),
    ("h=$(Run the tests) pytest -q", "'Run' is not a program on PATH"),
    ('h="$(Run the tests)" pytest -q', "'Run' is not a program on PATH"),
])
def test_check_problem_still_rejects_prose_and_unclosed_substitutions(env, cmd, why):
    from ttp.push import check_problem
    assert why in (check_problem(cmd) or "")


def test_push_check_path_args_and_unmatched(env, tmp_path):
    from ttp import push
    assert push.path_args("python3 -m pytest -q tests/test_a.py::test_x tests/b_*.py") == \
        ["tests/test_a.py", "tests/b_*.py"]
    assert push.path_args("make check") == []
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q", "-b", "work")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("")
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    checks = ["pytest tests/test_a.py tests/test_gone.py", "ruff check tests/*.py", "pytest tests/"]
    ref, missing = push.unmatched_paths(repo, "origin", "work", checks)
    assert ref == "work"
    assert missing == ["'pytest tests/test_a.py tests/test_gone.py': 'tests/test_gone.py'"]
    assert push.unmatched_paths(repo, "origin", "nope", checks) == ("", [])
    # pytest parametrised ids are not globs
    assert push.path_args("pytest tests/test_a.py[param-1] tests/test_a.py::test_x[a-b]") == \
        ["tests/test_a.py", "tests/test_a.py"]
    # after `cd <dir>` paths are relative to <dir>; the dir itself is checked too
    assert push.path_args("cd plugins/x && pytest tests/ ../../tests/test_a.py") == \
        ["plugins/x", "plugins/x/tests", "tests/test_a.py"]
    assert push.path_args("cd /tmp && pytest tests/") == []
    assert push.path_args("cd $HOME && pytest tests/") == []
    # Redirect targets and option values are not check targets.
    assert push.path_args("pytest tests/a.py > out/log.txt 2> out/err 2>>out/x >out/y < in/z") == ["tests/a.py"]
    assert push.path_args("pytest --junitxml out/r.xml --rootdir=sub/dir -q tests/a.py") == ["tests/a.py"]
    assert push.path_args("pytest --junitxml=out/r.xml -c cfg/p.ini tests/b.py") == ["tests/b.py"]
    (repo / "plugins" / "x" / "tests").mkdir(parents=True)
    (repo / "plugins" / "x" / "tests" / "test_p.py").write_text("")
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "p")
    cd_checks = ["cd plugins/x && pytest tests/ tests/test_p.py[a]", "cd plugins/gone && pytest tests/"]
    assert push.unmatched_paths(repo, "origin", "work", cd_checks)[1] == [
        "'cd plugins/gone && pytest tests/': 'plugins/gone'"]
    # the remote-tracking ref (what `ttp push` rebases onto) wins over a stale local branch
    g("checkout", "-qb", "newer")
    (repo / "tests" / "test_gone.py").write_text("")
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "y")
    g("update-ref", "refs/remotes/origin/work", "HEAD")
    assert push.unmatched_paths(repo, "origin", "work", checks) == ("origin/work", [])


def test_whitespace_check_skips_logs_and_no_diff_files(env, tmp_path, capsys):
    """The documented `git diff --check` form leaves out committed run logs (their captured lines
    keep trailing whitespace) and files .gitattributes marks -diff or binary, but still fails on
    source; doctor names a configured whitespace check that would fail on logs."""
    from ttp import push
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                  check=True, capture_output=True)
    g("init", "-q", "-b", "work")
    g("commit", "-q", "--allow-empty", "-m", "base")
    g("branch", "base")
    check = push.whitespace_check("base")
    run = lambda c: subprocess.run(c, shell=True, cwd=repo, capture_output=True, text=True)
    (repo / "runs").mkdir()
    (repo / "runs" / "device.log").write_text("step 1 ok   \nstep 2\t\n")
    (repo / "out.txt").write_text("captured   \n")
    (repo / "blob.dat").write_text("raw   \n")
    (repo / ".gitattributes").write_text("out.txt -diff\nblob.dat binary\n")
    g("add", ".")
    g("commit", "-qm", "logs")
    assert run("git diff --check base HEAD").returncode != 0   # the bare form fails on the log
    r = run(check)
    assert r.returncode == 0, r.stdout   # (a) the log and (c) the -diff and binary files are skipped
    (repo / "src.py").write_text("x = 1   \n")
    g("add", ".")
    g("commit", "-qm", "src")
    r = run(check)
    assert r.returncode != 0 and "src.py" in r.stdout and ".log" not in r.stdout   # (b) source still fails
    plain = "git diff --check origin/work HEAD"
    assert push.unexcluded_log_checks([plain, push.whitespace_check("origin/work"), "pytest -q",
                                       "git -C . diff --check a b -- . ':!*.log'", "git diff a b"]) == [plain]
    p = make(env)
    p.set_config("delivery.push_checks", [plain, "pytest -q"])
    from ttp import cli
    cli.main(["doctor", p.name])
    out = capsys.readouterr().out
    assert f"delivery.push_checks: {plain!r} also checks committed *.log output" in out
    assert p.config()["delivery"]["push_checks"] == [plain, "pytest -q"]   # explicit checks stay as set


def test_check_objects_are_validated_kept_in_config_and_plain_strings_stay_strict(env):
    from ttp import push
    from ttp.project import config_problems
    checks = push.check_list(["true", COND, {"run": "true", "if_exists": "tests/*.sh"}])
    assert checks == ["true", f"sh {NEW_TEST}", "true"]
    assert [c.if_exists for c in checks] == ["", NEW_TEST, "tests/*.sh"]
    assert push.check_list(json.dumps([COND]))[0].if_exists == NEW_TEST, "config_set sends JSON text"
    assert push.checks_of(["true", COND]) == ["true", COND], "the opt-in form must survive config_set"
    for bad in ({"if_exists": "x"}, {"run": "true", "if_exists": "/abs"}, {"run": "true", "if_exists": "../up"},
                {"run": "true", "if_exists": ""}, {"run": "true", "paths": "x"}, {"run": "true", "if_exists": 3}):
        with pytest.raises(ValueError):
            push.checks_of([bad])
        assert config_problems({"delivery": {"push_checks": [bad]}}), bad
    # A malformed object never skips: it runs on every head, as a plain string does.
    assert [c.if_exists for c in push.check_list([{"run": "true", "if_exists": "../up"},
                                                   {"run": "true", "if_exists": "x", "extra": 1}])] == ["", ""]
    p = make(env)
    from ttp import cli
    cli.main(["config", p.name, "delivery.push_checks", json.dumps(["true", COND])])
    assert p.config()["delivery"]["push_checks"] == ["true", COND]


def test_if_exists_with_a_trailing_slash_or_leading_dot_slash_still_matches(env, tmp_path):
    from ttp import push
    import subprocess
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "a.sh").write_text("true\n")
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    checks = push.check_list([{"run": "true", "if_exists": "tests/"}, {"run": "true", "if_exists": "./tests/a.sh"}])
    assert [c.if_exists for c in checks] == ["tests", "tests/a.sh"]
    assert [push.skip_reason(repo, "HEAD", c) for c in checks] == [None, None]


def test_check_commands_run_bare_pytest_with_the_repositorys_venv(tmp_path):
    from ttp import push
    repo, py = _venv_repo(tmp_path)
    q = shlex.quote(str(py))
    assert push.venv_cmd("pytest -q", repo) == f"{q} -m pytest -q"
    assert push.venv_cmd("python3 -m pytest -q tests/a.py", repo) == f"{q} -m pytest -q tests/a.py"
    assert push.venv_cmd("FOO=1 py.test -x && ruff check", repo) == f"FOO=1 {q} -m pytest -x && ruff check"
    assert push.venv_cmd("cd sub && pytest", repo) == f"cd sub && {q} -m pytest"
    for other in ("make test", "echo pytest", "pytest-benchmark run", "python3 tools/x.py"):
        assert push.venv_cmd(other, repo) == other
    # A linked worktree has no copy of the ignored venv: the main checkout's is used.
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "x", str(wt)], check=True)
    assert push.venv_cmd("pytest -q", wt) == f"{q} -m pytest -q"
    # No venv, or one without pytest: the command stays as written.
    bare, _ = _venv_repo(tmp_path, "nopytest", pytest_too=False)
    assert push.venv_cmd("pytest -q", bare) == "pytest -q"
    plain = tmp_path / "plain"
    subprocess.run(["git", "init", "-q", str(plain)], check=True)
    assert push.venv_cmd("pytest -q", plain) == "pytest -q"
    assert push.check_argv("pytest -q")[-1] == "pytest -q"   # without a worktree: as written


def _nested_worktree(tmp_path):
    """A project root with a pytest config, a conftest.py and a package `pkg`, and a git worktree nested
    below it whose `pkg` differs (it alone has `pkg.only_here`), with a test that imports it."""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / "conftest.py").write_text("")
    (root / "pkg" / "__init__.py").write_text("WHERE = 'root'\n")
    wt = root / "tt-project" / "worktrees" / "t1"
    (wt / "pkg").mkdir(parents=True)
    (wt / "tests").mkdir()
    (wt / "pkg" / "__init__.py").write_text("WHERE = 'worktree'\n")
    (wt / "pkg" / "only_here.py").write_text("")
    (wt / "tests" / "test_pkg.py").write_text(
        "import pkg\nfrom pkg import only_here\n\ndef test_pkg():\n    assert pkg.WHERE == 'worktree'\n")
    git = ["git", "-C", str(wt), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "a"], check=True)
    shim = tmp_path / "shim"   # the python3 a check command names: this interpreter, which has pytest
    shim.mkdir()
    # A wrapper, not a symlink: a symlinked venv python finds no pyvenv.cfg beside it and loses the venv's pytest.
    (shim / "python3").write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
    (shim / "python3").chmod(0o755)
    return root, wt, shim


def test_pytest_in_a_nested_worktree_imports_the_worktrees_package(env, tmp_path, monkeypatch):
    from ttp import cli, push
    root, wt, shim = _nested_worktree(tmp_path)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    cmd = "python3 -m pytest -q -p no:cacheprovider tests/test_pkg.py"
    # As written, pytest takes the project root as rootdir, loads its conftest.py and imports its pkg.
    plain = subprocess.run(["bash", "-c", cmd], cwd=wt, capture_output=True, text=True)
    assert plain.returncode != 0 and "only_here" in plain.stdout, plain.stdout
    assert push.nested_pytest(wt) and not push.nested_pytest(tmp_path / "shim")
    q = shlex.quote(str(wt))
    assert push.venv_cmd(cmd, wt) == (f" PYTHONPATH={q}${{PYTHONPATH:+:$PYTHONPATH}} python3 -m pytest "
                                      f"--rootdir={q} --confcutdir={q} -q -p no:cacheprovider tests/test_pkg.py")
    # A command's own --rootdir and PYTHONPATH stay its own.
    assert push.venv_cmd("cd x && PYTHONPATH=src pytest --rootdir=x", wt) == \
        f"cd x && PYTHONPATH={q}${{PYTHONPATH:+:$PYTHONPATH}} PYTHONPATH=src pytest --confcutdir={q} --rootdir=x"
    assert push.venv_cmd("make test", wt) == "make test"
    p = make(env)
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.chdir(wt)
    cli.main(["checks", "--", cmd])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["commands"] == [cmd], (run / "checks.log").read_text()


def test_check_commands_run_wrapped_pytest_with_the_venv(tmp_path):
    from ttp import push
    repo, py = _venv_repo(tmp_path)
    q = shlex.quote(str(py))
    assert push.venv_cmd("timeout 600 pytest -q", repo) == f"timeout 600 {q} -m pytest -q"
    assert push.venv_cmd("timeout -k 5 --preserve-status 10m python3 -m pytest", repo) == \
        f"timeout -k 5 --preserve-status 10m {q} -m pytest"
    assert push.venv_cmd("env X=1 pytest -x", repo) == f"env X=1 {q} -m pytest -x"
    assert push.venv_cmd("make && env A=1 timeout 60 pytest", repo) == f"make && env A=1 timeout 60 {q} -m pytest"
    for other in ("timeout pytest", "envoy pytest", "timeout 60 make test"):
        assert push.venv_cmd(other, repo) == other


def _editable(venv: Path, kind: str, src: Path) -> None:
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    if kind == "direct_url":
        (site / "mypkg-0.1.dist-info").mkdir(exist_ok=True)
        (site / "mypkg-0.1.dist-info" / "direct_url.json").write_text(
            json.dumps({"url": src.as_uri(), "dir_info": {"editable": True}}))
    elif kind == "finder":
        (site / "__editable___mypkg_0_1_finder.py").write_text(f"MAPPING = {{'mypkg': '{src / 'mypkg'}'}}\n")
    else:
        (site / "__editable__.mypkg-0.1.pth").write_text(f"{src}\n")


@pytest.mark.parametrize("kind", ["direct_url", "finder", "pth"])
def test_a_worktree_skips_the_main_checkouts_venv_with_an_editable_install_of_it(tmp_path, kind):
    from ttp import push
    repo, py = _venv_repo(tmp_path)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "x", str(wt)], check=True)
    q = shlex.quote(str(py))
    # A non-editable install, or one from elsewhere, keeps the venv in use.
    _editable(repo / ".venv", kind, tmp_path / "elsewhere")
    (tmp_path / "elsewhere" / "mypkg").mkdir(parents=True)
    assert push.venv_cmd("pytest -q", wt) == f"{q} -m pytest -q"
    # Installed editable from the main checkout: the worktree's tests would import its code, so the
    # command runs as written and the checks output says why.
    _editable(repo / ".venv", kind, repo)
    cmd = push.venv_cmd("pytest -q", wt)
    assert cmd.endswith("\npytest -q") and str(py) not in cmd.splitlines()[-1]
    said = subprocess.run(["bash", "-c", cmd.replace("pytest -q", "true")], capture_output=True, text=True).stderr
    assert "installed editable from" in said and str(repo) in said and "outside this worktree" in said
    # In the main checkout itself the install is its own code: the venv is used.
    assert push.venv_cmd("pytest -q", repo) == f"{q} -m pytest -q"


def test_env_problem_tells_missing_dependencies_from_code_failures(tmp_path):
    from ttp import push
    repo, _ = _venv_repo(tmp_path)
    why = push.env_problem(CONFTEST_FAIL.format(mod="numpy"), repo)
    assert why and "numpy" in why and push.ENV_FIX in why
    collect = ("==== ERRORS ====\n___ ERROR collecting tests/test_a.py ___\nImportError while importing test "
               "module '/w/tests/test_a.py'.\nE   ModuleNotFoundError: No module named 'yaml.loader'\n")
    assert "yaml" in push.env_problem(collect, repo)
    assert push.ENV_FIX in push.env_problem("/usr/bin/python3: No module named pytest\n", repo)
    assert push.ENV_FIX in push.env_problem("bash: line 1: pytest: command not found\n", repo)
    # The repository's own module failing to import is the head's code, as is any other failure.
    assert push.env_problem(CONFTEST_FAIL.format(mod="mypkg.sub"), repo) is None
    assert push.env_problem("E   AssertionError: 1 != 2\n1 failed in 0.1s\n", repo) is None
    assert push.env_problem("No module named 'numpy'\n", repo) is None   # not a collection or import failure



def test_if_changed_is_validated_kept_in_config_and_malformed_scopes_never_skip(env):
    from ttp import push
    from ttp.project import config_problems
    checks = push.check_list([SCOPED, {"run": "true", "if_changed": "./src/"}])
    assert [c.if_changed for c in checks] == [("src/**", "fail.sh"), ("src",)]
    assert push.checks_of([SCOPED, {"run": "true", "if_changed": "src"}]) == [SCOPED, {"run": "true", "if_changed": "src"}]
    assert push.check_list([{**COND, "if_changed": "src"}])[0].config() == {**COND, "if_changed": "src"}
    for bad in ({"run": "true", "if_changed": ""}, {"run": "true", "if_changed": []}, {"run": "true", "if_changed": "/abs"},
                {"run": "true", "if_changed": ["src", "../up"]}, {"run": "true", "if_changed": 3},
                {"run": "true", "if_changed": [""]}):
        with pytest.raises(ValueError):
            push.checks_of([bad])
        assert config_problems({"delivery": {"push_checks": [bad]}}), bad
        assert push.check_list([bad])[0].if_changed == (), "a malformed scope must run the check everywhere"


def test_push_runs_an_if_changed_check_only_when_the_diff_touches_its_paths(env, monkeypatch, capsys):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true", SCOPED])
    _commit(repo, "fail.sh", "exit 1\n")
    assert _ttp_push() == 4, "a diff touching the check's path must run it, and its failure stops the push"
    assert "skipped" not in capsys.readouterr().err
    _git_out(repo, "reset", "-q", "--hard", "origin/proj")
    _git_out(other, "pull", "-q")
    _commit(other, "fail.sh", "exit 1\n")   # on the branch already: not this change's diff
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "pull", "-q", "origin", "proj")
    _commit(repo, "notes.txt", "notes\n")
    assert _ttp_push() == 0, "a notes-only change was blocked by a check scoped to code"
    err = capsys.readouterr().err
    assert "skipped (not applicable:" in err and "changes nothing under src/**, fail.sh since" in err, err
    # The glob crosses subdirectories, and a move out of scope still counts as touching it.
    (repo / "src" / "a").mkdir(parents=True)
    _commit(repo, "src/a/x.py", "x\n")
    assert _ttp_push() == 4
    from ttp import push
    _git_out(repo, "reset", "-q", "--hard", "origin/proj")
    (repo / "src").mkdir()
    _commit(repo, "src/x.py", "x\n")
    base = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "mv", "src/x.py", "x.py")
    _git_out(repo, "commit", "-qm", "move")
    check = push.check_list([SCOPED])
    assert push.applicable(repo, "HEAD", check, print, base)[0] == check
    assert push.skip_reason(repo, "HEAD", check[0], base) is None   # after_push scopes the same way


def test_an_if_changed_check_fails_when_git_cannot_give_the_diff(env, monkeypatch, tmp_path):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, [SCOPED])
    _commit(repo, "notes.txt", "notes\n")
    said = []
    check = push.check_list([SCOPED])
    for base in ("0" * 40, "no-such-ref"):
        with pytest.raises(push.ScopeError) as e:
            push.applicable(repo, "HEAD", check, said.append, base)
        assert "cannot tell what HEAD changes since" in str(e.value) and e.value.check == "sh fail.sh"
    assert said == []
    # No push target to compare with: the check is not scoped, so it runs.
    assert push.applicable(repo, "HEAD", check, said.append, "") == (check, [])
    # Unrelated history (no merge base) is an error too, not "nothing changed".
    lone = tmp_path / "lone"
    subprocess.run(["git", "init", "-q", str(lone)], check=True)
    _commit(lone, "x.txt", "x\n")
    _git_out(repo, "fetch", "-q", str(lone), "HEAD:refs/heads/lone")
    with pytest.raises(push.ScopeError):
        push.applicable(repo, "HEAD", check, said.append, "lone")

@pytest.mark.parametrize("box", ["pidns", "cgroup"])
def test_sandboxed_checks_are_queued_and_finish_model_free(env, tmp_path, monkeypatch, capsys, box):
    from ttp import cli, detach, push
    from ttp.daemon import Daemon
    from ttp.runner import proc_start
    if not (push.pid_ns() if box == "pidns" else push.cgroup()):
        pytest.skip("no /proc")
    p = make(env)
    tid, repo = _checks_wait_task(p, tmp_path, {"status": "done", "summary": "checks passed"}, rc=None)
    run = pathlib.Path(p.db.one("SELECT dir FROM runs WHERE task=?", (tid,))["dir"])
    monkeypatch.chdir(repo)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    marker = "TTP_PIDNS" if box == "pidns" else "TTP_CGROUP"
    monkeypatch.setenv(marker, "another sandbox")
    monkeypatch.setenv("SOME_API_TOKEN", "keep-off-disk")
    monkeypatch.setenv("CHECK_VALUE", "preserved")
    cli.main(["checks", "--detach", "--fresh", "--", 'test "$CHECK_VALUE" = preserved'])
    assert "queued" in capsys.readouterr().out
    assert not (run / cli.CHECKS_PID).exists(), "the sandbox must launch no child"
    q = next((p.state / detach.QUEUE).glob("*.json"))
    assert "keep-off-disk" not in q.read_text() and marker not in json.loads(q.read_text())["env"]
    assert cli._checks_result(run) == 1 and not (run / cli.CHECKS_RC).exists()
    monkeypatch.delenv(marker)
    d = Daemon(p.base)
    d.boot_at = time.time() - 30   # queued work survives a daemon restart or host reboot
    d.wake_after_reboot()
    assert p.db.task(tid)["not_before"] and "woke" not in json.loads(p.db.task(tid)["result"])
    d.tend_pushes()
    info = cli._read_checks_pid(run)
    assert info["pid"] and info["started"] == proc_start(info["pid"])
    assert info["worktree"] == str(repo) and info["fresh"] is True
    assert _wait_precheck(run) == "0"
    assert not q.exists() and not detach.queued(run / cli.CHECKS_RC).exists()
    assert cli._checks_result(run) == 0
    _settle_probe(d, tid)
    assert p.db.task(tid)["status"] == "done"
    assert json.loads(p.db.task(tid)["result"])["run_status"] == "model-free"
    assert len(p.db.q("SELECT id FROM runs WHERE task=?", (tid,))) == 1


def test_queued_checks_keep_full(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    cli.main(["checks", "--detach", "--full", "--", "true"])
    assert "queued" in capsys.readouterr().out
    q = next((p.state / detach.QUEUE).glob("*.json"))
    assert json.loads(q.read_text())["checks"]["full"] is True
    monkeypatch.setattr(push, "_boxed", lambda: False)
    seen = _record_checks_argv(monkeypatch)
    detach.tend(p)
    assert seen and "--full" in seen[-1], seen
    assert cli._read_checks_pid(run)["full"] is True
    assert _wait_precheck(run) == "0"


def test_queued_checks_death_is_not_hidden_by_a_reused_pid(env, tmp_path, monkeypatch, capsys, reap_tmp_processes):
    from ttp import cli, detach, push
    from ttp.runner import proc_start
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    started = tmp_path / "started"
    monkeypatch.setattr(push, "_boxed", lambda: True)
    cli.main(["checks", "--detach", "--", "sh", "-c", f"touch {started}; sleep 60"])
    detach.tend(p)
    monkeypatch.setattr(push, "_boxed", lambda: False)   # the daemon's probe, on the host, judges pids
    info = cli._read_checks_pid(run)
    try:
        assert _wait_for(started.exists)
        os.killpg(info["pid"], signal.SIGKILL)
        assert _wait_for(lambda: _gone_pid(info["pid"]))
        assert not (run / cli.CHECKS_RC).exists()
        # Simulate the dead child's pid being reused by a live, unrelated process.
        info.update(pid=os.getpid(), started="previous process incarnation")
        assert proc_start(info["pid"]) != info["started"]
        (run / cli.CHECKS_PID).write_text(json.dumps(info))
        assert cli._checks_result(run) == 0, "a pid collision must not leave the task asleep"
        assert (run / cli.CHECKS_RC).read_text().strip() == "killed"
        os.kill(os.getpid(), 0)   # the unrelated process remains alive
    finally:
        if info["pid"] != os.getpid():
            try:
                os.killpg(info["pid"], signal.SIGKILL)
            except OSError:
                pass
        reap_tmp_processes(tmp_path)   # the check, a session of its own, outlives its killed supervisor


def test_push_priority_key_names_the_branch_host_wide(env, tmp_path):
    from ttp import push
    a, b = tmp_path / "a", tmp_path / "b"
    for r, url in ((a, "https://example.com/org/repo.git"), (b, "https://example.com/org/repo/")):
        subprocess.run(["git", "init", "-q", str(r)], check=True)
        subprocess.run(["git", "-C", str(r), "remote", "add", "origin", url], check=True)
    assert push.priority_key(a, "origin", "main") == push.priority_key(b, "origin", "main"), "two clones, one branch"
    assert push.priority_key(a, "origin", "main") != push.priority_key(a, "origin", "dev")
    assert push.priority_key(a, "nope", "main") != push.priority_key(b, "nope", "main"), "no URL: the repository"


def test_queued_checks_replace_pending_and_running_checks(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    started, child = tmp_path / "started", tmp_path / "child.pid"
    script = f"echo $$ > {child}; touch {started}; sleep 60"
    first, host = _detached_checks(run, capsys, "sh", "-c", script), 0
    try:
        assert _wait_for(started.exists)
        host = _host_pid(["sh", "-c", script])
        monkeypatch.setattr(push, "_boxed", lambda: True)
        cli.main(["checks", "--detach", "--", "false"])
        cli.main(["checks", "--detach", "--", "true"])
        assert len(list((p.state / detach.QUEUE).glob("*.json"))) == 1
        assert cli._checks_alive(cli._read_checks_pid(run)), "cancellation belongs to the daemon"
        assert cli._checks_result(run) == 1
        detach.tend(p)
        # the whole group got the signal; on a loaded host its members may take a moment to exit
        assert _wait_for(lambda: _gone_pid(first) and _gone_pid(host))
        assert _wait_precheck(run) == "0", "the latest request replaces the pending one"
        assert cli._read_checks_pid(run)["cmd"] == ["--", "true"]
    finally:
        for pid in (first, host):
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass


@pytest.mark.parametrize("failure", ["expired", "missing_worktree"])
def test_queued_checks_that_cannot_start_end_the_wait(env, tmp_path, monkeypatch, capsys, failure):
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    (run / cli.CHECKS_RC).write_text("0\n")   # an old pass must not settle a replacement
    cli.main(["checks", "--detach", "--", "true"])
    assert cli._checks_result(run) == 1
    q = next((p.state / detach.QUEUE).glob("*.json"))
    m = json.loads(q.read_text())
    if failure == "expired":
        m["queued"] -= detach.QUEUED_S + 1
    else:
        m["checks"]["worktree"] = str(tmp_path / "gone")
    q.write_text(json.dumps(m))
    detach.tend(p)
    assert cli._checks_result(run) == 0
    assert (run / cli.CHECKS_RC).read_text().strip() == "killed"
    assert "not started" in capsys.readouterr().out
    assert not q.exists() and not (run / cli.CHECKS_PID).exists()


def test_a_request_queued_while_the_daemon_starts_an_older_one_is_kept_and_runs_last(env, tmp_path, monkeypatch):
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    cli.main(["checks", "--detach", "--", "true"])
    real, seen = cli._checks_start, []

    def start(a, *args):
        seen.append(a.cmd)
        if len(seen) == 1:   # a newer request arrives while the daemon cancels and starts the older one
            cli.main(["checks", "--detach", "--", "false"])
        return real(a, *args)

    monkeypatch.setattr(cli, "_checks_start", start)
    detach.tend(p)
    assert seen == [["--", "true"]]
    assert len(list((p.state / detach.QUEUE).glob("*.json"))) == 1, "the newer request was deleted"
    assert detach.queued(run / cli.CHECKS_RC).exists()
    assert _wait_precheck(run) == "0"   # the older checks finish meanwhile ...
    assert cli._checks_result(run) == 1, "... and must not settle the wait while the newer one is queued"
    detach.tend(p)
    assert seen[-1] == ["--", "false"] and cli._read_checks_pid(run)["cmd"] == ["--", "false"]
    assert not list((p.state / detach.QUEUE).glob("*.json")) and not detach.queued(run / cli.CHECKS_RC).exists()
    assert _wait_precheck(run) == "1"
    monkeypatch.setattr(push, "_boxed", lambda: False)
    assert cli._checks_result(run) == 0 and (run / cli.CHECKS_RC).read_text().strip() == "1"


def test_a_daemon_tick_between_the_queue_writes_leaves_no_orphan_marker(env, tmp_path, monkeypatch):
    import threading
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    starting = threading.Event()
    monkeypatch.setattr(cli, "_checks_start", lambda *args: starting.set() or os.getpid())
    monkeypatch.setattr(detach, "renice", lambda *args: None)
    real_write, ticks = detach.write_json, []

    def publish(path, data, **kwargs):
        real_write(path, data, **kwargs)
        # The daemon's own tick takes the request before enqueue writes its .queued marker.
        tick = threading.Thread(target=detach.tend, args=(p,))
        tick.start()
        ticks.append(tick)
        assert starting.wait(10)
        tick.join(2)   # it finishes here unless enqueue's lock holds its cleanup back until the marker is written

    monkeypatch.setattr(detach, "write_json", publish)
    cli.main(["checks", "--detach", "--", "true"])
    ticks[0].join(10)
    assert not ticks[0].is_alive()
    assert not list((p.state / detach.QUEUE).glob("*.json"))
    assert not detach.queued(run / cli.CHECKS_RC).exists(), "an orphan marker was left"
    (run / cli.CHECKS_RC).write_text("0\n")
    assert cli._checks_result(run) == 0, "a completed check must end the wait"


def test_a_sandboxed_probe_never_calls_live_host_checks_killed(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, detach, push, runner
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "pid_ns", lambda: "pid:[host]")   # macOS has none to record
    monkeypatch.setattr(push, "_boxed", lambda: True)
    started = tmp_path / "started"
    cli.main(["checks", "--detach", "--", "sh", "-c", f"touch {started}; sleep 30"])
    detach.tend(p)
    info = cli._read_checks_pid(run)
    try:
        assert _wait_for(started.exists) and cli._checks_alive(info) and info["pidns"] == push.pid_ns()
        with monkeypatch.context() as box:   # boxed: the host pid is not visible from here
            box.setattr(runner, "proc_start", lambda pid: None)
            assert cli._checks_result(run) == 1, "live host checks were declared killed from the sandbox"
        with monkeypatch.context() as box:   # another PID namespace, where the same pid is another process
            box.setattr(push, "_boxed", lambda: False)
            box.setattr(push, "pid_ns", lambda: "pid:[another]")
            box.setattr(runner, "proc_start", lambda pid: "another process's start")
            assert cli._checks_result(run) == 1
        assert not (run / cli.CHECKS_RC).exists(), "the executor's exit marker was overwritten"
        monkeypatch.setattr(push, "_boxed", lambda: False)
        assert cli._checks_result(run) == 1, "the host probe sees them running"
        os.killpg(info["pid"], signal.SIGTERM)
        assert _wait_precheck(run) == "143"
        monkeypatch.setattr(push, "_boxed", lambda: True)
        assert cli._checks_result(run) == 0, "a real exit code is reported from the sandbox too"
        assert "exit code 143" in capsys.readouterr().out
    finally:
        try:
            os.killpg(info["pid"], signal.SIGKILL)
        except OSError:
            pass


def test_a_failed_identity_write_ends_the_new_child_and_the_queued_wait(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    marker, children = tmp_path / "ran", []

    def full(path, data, **kw):
        children.append(data["pid"])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli, "write_json", full)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    cli.main(["checks", "--detach", "--", f"touch {marker}"])
    detach.tend(p)
    assert children and _wait_for(lambda: _gone_pid(children[0])), "the child this launch made must end"
    assert not marker.exists() and not (run / cli.CHECKS_PID).exists()
    assert not list((p.state / detach.QUEUE).glob("*.json"))
    assert cli._checks_result(run) == 0 and "No space left" in capsys.readouterr().out


def test_status_does_not_warn_about_a_check_that_does_not_apply_on_the_branch(env, tmp_path):
    from ttp import push
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q", "-b", "work")
    (repo / "a.txt").write_text("")
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    checks = push.check_list([{"run": "pytest tests/test_new.py", "if_exists": "tests/test_new.py"},
                              "pytest tests/test_gone.py"])
    assert push.unmatched_paths(repo, "origin", "work", checks)[1] == [
        "'pytest tests/test_gone.py': 'tests/test_gone.py'"]


def test_a_pr_fix_task_publishes_onto_the_prs_branch_whether_it_is_free_or_held(env, monkeypatch, capsys):
    """The daemon's PR-fix task delivers with `ttp push --own` onto the PR's branch, the finished task's
    ttp/t<id>-... branch: from a worktree on that branch when it is free, and from its own branch onto
    the PR's when another worktree holds it. Fast-forward only, never a new remote branch."""
    from ttp import push, worktree
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    url = "https://github.com/acme/widgets/pull/7"
    src = p.db.add_task("Speed up the loader", "s", kind="code")
    pr_branch = f"ttp/t{src}-speed-up-the-loader"
    p.db.update_task(src, status="done", pr_url=url, branch=pr_branch)
    _git_out(repo, "branch", pr_branch)
    other_wt = env["tmp"] / "pr"
    _git_out(repo, "worktree", "add", "-q", str(other_wt), pr_branch)
    _commit(other_wt, "pr.txt", "pr\n")
    _git_out(other_wt, "push", "-q", "origin", f"HEAD:refs/heads/{pr_branch}")
    _git_out(repo, "worktree", "remove", str(other_wt))
    d = Daemon(p.base)
    rec = {"task": src, "branch": pr_branch, "url": url}

    def publish(fix):
        path, branch = worktree.ensure(p, fix)
        _commit(path, f"fix{fix['id']}.txt", "fix\n")
        monkeypatch.chdir(path)
        monkeypatch.setenv("TTP_TASK", str(fix["id"]))
        assert push.resolve(p, path, own=True) == ("origin", pr_branch, True), branch
        assert _ttp("push", "--own") == 0, capsys.readouterr().err
        assert _git_out(origin, "rev-parse", pr_branch) == _git_out(path, "rev-parse", "HEAD")
        return path, branch

    # Free: the fix works on the PR's branch itself.
    fix, made = d.pr_findings_owner("acme/widgets#7", rec, "CI failing: tests")
    assert made and fix["branch"] == pr_branch
    path, branch = publish(fix)
    assert branch == pr_branch
    p.db.update_task(fix["id"], status="done")
    monkeypatch.chdir(repo)
    # Held: another worktree keeps the PR's branch; the fix's own branch is published onto the PR's.
    fix, made = d.pr_findings_owner("acme/widgets#7", rec, "CI failing: tests")
    assert made and fix["branch"] is None and f'"pr_branch:{pr_branch}"' in fix["labels"]
    assert "checked out elsewhere" in fix["spec"]
    path2, branch2 = publish(fix)
    assert branch2 == f"ttp/t{fix['id']}-fix-pr-findings-speed-up-the-loader"
    remote = _git_out(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    assert branch2 not in remote and pr_branch in remote
    # Never a rewrite: a head that does not fast-forward the PR's branch is refused before the checks.
    _git_out(path2, "reset", "-q", "--hard", "HEAD~2")
    _commit(path2, "other.txt", "x\n")
    tip = _git_out(origin, "rev-parse", pr_branch)
    assert _ttp("push", "--own") == 2 and "not an ancestor" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", pr_branch) == tip
    # Another task, neither carrying nor continuing the PR's branch, still may not publish it.
    monkeypatch.chdir(path)
    monkeypatch.setenv("TTP_TASK", str(p.db.add_task("unrelated", kind="code")))
    with pytest.raises(ValueError):
        push.own_target(p, path)


def test_a_pr_fix_task_gets_a_worktree_of_the_prs_clone_when_the_root_is_another_repo(env, monkeypatch):
    """A PR-fix task whose PR is in another repository than the project root's (here a root that is no
    git repository) still gets its own worktree, worktrees/t<id>, as a linked worktree of a local
    clone of the PR's repository found through a worktree a task made by hand below the root or a
    clone directly in it: on the PR's branch when it is free, else on its own branch from that
    branch's head. A retry reuses it and the sweep's remove() removes it from that clone. A task
    with no clone of its PR's repository keeps working in the root."""
    from ttp import cli, worktree
    from ttp.daemon import Daemon
    from ttp.project import register
    for var, val in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                     ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(var, val)
    tmp = env["tmp"]
    root = tmp / "plain"
    root.mkdir()
    p = cli.bootstrap(root, "plain", MAKE_BRIEF, "fake")
    register("plain", {"host": "testhost", "dir": str(root)})
    assert not worktree.is_git(root)
    origin, clone = _bare_init(env, tmp / "acme" / "widgets.git"), tmp / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    _commit(clone, "a.txt", "a\n")
    _git_out(clone, "push", "-q", "origin", "HEAD:refs/heads/main")
    url, key = "https://github.com/acme/widgets/pull/7", "acme/widgets#7"
    src = p.db.add_task("Speed up the loader", "s", kind="code")
    pr_branch = f"ttp/t{src}-speed-up-the-loader"
    p.db.update_task(src, status="done", pr_url=url, branch=pr_branch)
    # The delivering task's worktree, made by hand below the root, holds the PR's branch.
    held_wt = root / "wt" / f"t{src}"
    _git_out(clone, "worktree", "add", "-q", "-b", pr_branch, str(held_wt))
    _commit(held_wt, "pr.txt", "pr\n")
    _git_out(held_wt, "push", "-q", "origin", f"HEAD:refs/heads/{pr_branch}")
    assert worktree.pr_clone(p, key) == clone.resolve()
    d = Daemon(p.base)
    rec = {"task": src, "branch": pr_branch, "url": url}
    fix, made = d.pr_findings_owner(key, rec, "CI failing: tests")
    assert made and fix["branch"] is None and "checked out elsewhere" in fix["spec"]
    cwd, branch = d._workdir_for(fix)
    path = p.worktrees / f"t{fix['id']}"
    assert cwd == str(path) and branch == f"ttp/t{fix['id']}-fix-pr-findings-speed-up-the-loader"
    assert worktree._repo_of(path) == clone.resolve()
    assert _git_out(path, "rev-parse", "HEAD") == _git_out(held_wt, "rev-parse", "HEAD")
    assert d._workdir_for(dict(fix, branch=branch)) == (cwd, branch)   # a retry reuses it
    p.db.update_task(fix["id"], status="done", branch=branch)
    worktree.remove(p, fix["id"])
    assert not path.exists() and str(path) not in _git_out(clone, "worktree", "list")
    # Free: the next fix works on the PR's branch itself, found through a clone directly in the root.
    _git_out(clone, "worktree", "remove", str(held_wt))
    shutil.move(str(clone), str(root / "widgets"))
    assert worktree.pr_clone(p, key) == (root / "widgets").resolve()
    fix, made = d.pr_findings_owner(key, rec, "CI failing: tests")
    assert made and fix["branch"] == pr_branch and "build on it" in fix["spec"]
    cwd, branch = d._workdir_for(fix)
    assert cwd == str(p.worktrees / f"t{fix['id']}") and branch == pr_branch
    assert _git_out(p.worktrees / f"t{fix['id']}", "rev-parse", "--abbrev-ref", "HEAD") == pr_branch
    # No clone of the PR's repository: the root, as before.
    other = p.db.add_task("Fix PR findings: other", "s", kind="code", labels=["pr_fix:acme/gadgets#3"])
    assert worktree.pr_repo(p, p.db.task(other)) is None
    assert d._workdir_for(p.db.task(other)) == (str(p.root), None)
    # The PR's branch is neither local nor on origin (a fork's): the root, not a workspace failure.
    lost = p.db.add_task("Fix PR findings: fork", "s", kind="code", labels=["pr_fix:acme/widgets#9"],
                         branch="ttp/gone-from-everywhere")
    assert worktree.pr_repo(p, p.db.task(lost))
    assert d._workdir_for(p.db.task(lost)) == (str(p.root), None)


def test_follow_ups_and_redos_carry_a_branch_only_when_they_should(env):
    """A follow-up (`continues` on a done task) that names a finished task's branch but has no
    `pr_branch:` line carries no branch, so it lands on the push branch with `ttp push`; one with its
    own line carries that branch. A continuation of a failed task keeps the branch that task carried,
    unless its spec names only the push branch or the coordinator sets `pr_branch` to "" (task_add, or
    task_update before it starts): then it carries none (push.pr_branch_of)."""
    p = make(env)
    from ttp import coordinator as coord
    from ttp import push

    def task(title):
        return p.db.one("SELECT * FROM tasks WHERE title=?", (title,))

    def labels(title):
        return json.loads(task(title)["labels"])

    p.set_config("delivery.push_branch", "origin/proj")
    src = p.db.add_task("Speed up the loader", "s", kind="code")
    b = f"ttp/t{src}-speed-up-the-loader"
    p.db.update_task(src, status="done", branch=b)
    rev = p.db.add_task("Review the loader", "s", kind="review")
    p.db.update_task(rev, status="done")
    # A follow-up naming the done task's branch as where its commit comes from: no label.
    assert coord.apply(p, [{"type": "task_add", "title": "Land the last commit", "kind": "code", "continues": rev,
                            "spec": f"Cherry-pick the last commit of {b} and land it on proj with `ttp push`."}]) == []
    assert labels("Land the last commit") == []
    assert push.pr_branch_of(p, task("Land the last commit")) == ""
    # A follow-up with its own line carries that branch.
    assert coord.apply(p, [{"type": "task_add", "title": "Fix the loader PR", "kind": "code", "continues": rev,
                            "spec": f"pr_branch: `{b}`\nFix the PR's CI."}]) == []
    assert labels("Fix the loader PR") == [f"pr_branch:{b}"]
    # A continuation of a failed task keeps the branch that task carried (it exists).
    subprocess.run(["git", "-C", str(p.root), "branch", "feature/x"], check=True, capture_output=True)
    gone = p.db.add_task("Fix the feature PR", "s", kind="code", labels=["pr_branch:feature/x"])
    p.db.update_task(gone, status="failed", branch=f"ttp/t{gone}-fix-the-feature-pr")
    assert coord.apply(p, [{"type": "task_add", "title": "Feature PR again", "kind": "code", "continues": gone,
                            "spec": "Finish the CI fix."}]) == []
    assert labels("Feature PR again") == [f"continues:{gone}"]
    assert push.pr_branch_of(p, task("Feature PR again")) == "feature/x"
    # A redo of a cancelled, mislabelled task whose spec names only the push branch carries none.
    bad = p.db.add_task("Land the entry", "s", kind="code", labels=[f"pr_branch:{b}"])
    p.db.update_task(bad, status="cancelled")
    assert coord.apply(p, [{"type": "task_add", "title": "Land the entry, redo", "kind": "code", "continues": bad,
                            "spec": "Land the entry on proj with `ttp push`; this task has no PR-branch label."}]) == []
    redo = task("Land the entry, redo")
    assert labels("Land the entry, redo") == [f"continues:{bad}", "no_pr_branch"]
    assert push.pr_branch_of(p, redo) == "" and push.pr_elsewhere(p, redo, p.config()["delivery"]) == ""
    assert any(f"#{redo['id']} continues #{bad} but not its branch {b}" in n for n in p.db.kv(coord.NOTES_KEY))
    # A branch never published (neither here nor on a remote) cannot be delivered onto: a redo that
    # does not name it carries none; one that names it keeps it.
    lost = p.db.add_task("Land the table", "s", kind="code", labels=["pr_branch:feature/never-made"])
    p.db.update_task(lost, status="cancelled")
    assert coord.apply(p, [{"type": "task_add", "title": "Land the table, redo", "kind": "code", "continues": lost,
                            "spec": "Land the table."},
                           {"type": "task_add", "title": "Make the table PR", "kind": "code", "continues": lost,
                            "spec": "Create feature/never-made with the table."}]) == []
    assert labels("Land the table, redo") == [f"continues:{lost}", "no_pr_branch"]
    assert any(f"#{task('Land the table, redo')['id']} continues #{lost} but not its branch feature/never-made "
               f"(never published)" in n for n in p.db.kv(coord.NOTES_KEY))
    assert labels("Make the table PR") == [f"continues:{lost}"]
    # Or the coordinator says so: pr_branch "" on task_add ...
    bad2 = p.db.add_task("Land the docs", "s", kind="code", labels=["pr_branch:feature/x"])
    p.db.update_task(bad2, status="cancelled")
    assert coord.apply(p, [{"type": "task_add", "title": "Land the docs, redo", "kind": "code", "continues": bad2,
                            "pr_branch": "", "spec": "Land the docs."}]) == []
    assert push.pr_branch_of(p, task("Land the docs, redo")) == ""
    # ... or on task_update before it starts, which also drops its own label; never once it runs.
    assert coord.apply(p, [{"type": "task_update", "id": task("Feature PR again")["id"], "pr_branch": ""},
                           {"type": "task_update", "id": task("Fix the loader PR")["id"], "pr_branch": ""}]) == []
    assert labels("Feature PR again") == [f"continues:{gone}", "no_pr_branch"]
    assert labels("Fix the loader PR") == ["no_pr_branch"]
    assert push.pr_branch_of(p, task("Feature PR again")) == ""
    p.db.update_task(task("Land the docs, redo")["id"], status="running")
    problems = coord.apply(p, [{"type": "task_update", "id": task("Land the docs, redo")["id"], "pr_branch": ""}])
    assert problems and "running" in problems[0]


def test_own_push_refuses_a_pr_branch_that_exists_nowhere(env, monkeypatch):
    """`ttp push --own` from a task's own branch publishes onto its pr_branch only when that branch
    exists here or on the remote; a missing one (e.g. a word parsed out of prose) or an invalid one is
    refused, and main/master never go."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])

    def target(label):
        t = p.db.add_task("Carry it", "s", kind="code", labels=[f"pr_branch:{label}"])
        own = f"ttp/t{t}-carry-it"
        _git_out(repo, "checkout", "-q", "-B", own)
        monkeypatch.setenv("TTP_TASK", str(t))
        return push.own_target(p, repo)

    with pytest.raises(ValueError, match="exists neither here nor on origin"):
        target("use")
    assert "use" not in _git_out(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    for bad in ("use that branch", "feat..ure"):
        with pytest.raises(ValueError, match="not one valid branch name"):
            target(bad)
    for shared in ("main", "master"):
        with pytest.raises(ValueError):
            target(shared)
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/feature/remote")
    assert target("feature/remote") == ("origin", "feature/remote")
    _git_out(repo, "branch", "feature/local", "HEAD")
    assert target("feature/local") == ("origin", "feature/local")


def test_own_push_refuses_another_tasks_branch_it_only_inherited(env, monkeypatch, capsys):
    """A reported case, replayed: a plan task whose worktree started from a finished task's branch
    carried `pr_branch:<that branch>` without its spec asking for it, so `ttp push --own` targeted the
    other task's branch. Such an inherited branch of another task is refused, saying how to deliver
    instead (`ttp push --own-branch`, which publishes the own branch under its own name); a spec line
    `pr_branch: <branch>`, a continuation of the branch's owner and a review of an authorized task
    still publish onto it."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    src = p.db.add_task("Profile the loader", "s", kind="code")
    theirs = f"ttp/t{src}-profile-the-loader"
    p.db.update_task(src, status="done", branch=theirs)
    _git_out(repo, "checkout", "-q", "-b", theirs)
    _commit(repo, "theirs.txt", "theirs\n")
    _git_out(repo, "push", "-q", "origin", theirs)
    shipped = _git_out(origin, "rev-parse", theirs)

    def task(title, spec="s", kind="plan", labels=(), on=None):
        t = p.db.add_task(title, spec, kind=kind, labels=list(labels))
        own = f"ttp/t{t}-{title.lower().replace(' ', '-')}"
        p.db.update_task(t, status="running", branch=on or own)
        _git_out(repo, "checkout", "-q", "-B", on or own, theirs)
        _commit(repo, f"t{t}.txt", "work\n")
        monkeypatch.setenv("TTP_TASK", str(t))
        return t, on or own

    # The plan task on its own branch, labelled with the other task's branch: refused, nothing pushed.
    plan, own = task("Plan the cache", labels=[f"pr_branch:{theirs}"])
    with pytest.raises(ValueError, match="only inherited") as e:
        push.own_target(p, repo)
    assert f"task #{src}'s branch" in str(e.value) and "ttp push --own-branch --detach" in str(e.value)
    capsys.readouterr()
    assert _ttp("push", "--own") == 2
    assert "only inherited" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", theirs) == shipped
    # --own-branch publishes the task's own branch under its own name. cmd_push sets OWN_BRANCH_ENV in
    # this process: registered unset first, so teardown unsets it rather than restoring its value.
    monkeypatch.delenv(push.OWN_BRANCH_ENV, raising=False)
    assert _ttp("push", "--own-branch") == 0, capsys.readouterr().err
    assert _git_out(origin, "rev-parse", own) == _git_out(repo, "rev-parse", "HEAD")
    assert _git_out(origin, "rev-parse", theirs) == shipped
    monkeypatch.delenv(push.OWN_BRANCH_ENV)
    # A worktree on the other task's branch itself (it started there): refused too.
    task("Plan again", labels=[f"pr_branch:{theirs}"], on=theirs)
    with pytest.raises(ValueError, match="only inherited"):
        push.own_target(p, repo)
    # Inherited through a continued task that only inherited it: refused.
    mid = p.db.add_task("Middle step", "s", kind="code", labels=[f"pr_branch:{theirs}"])
    p.db.update_task(mid, status="failed")
    task("Pick up the middle", kind="code", labels=[f"continues:{mid}", f"pr_branch:{theirs}"])
    with pytest.raises(ValueError, match="only inherited"):
        push.own_target(p, repo)
    # Allowed: a spec line naming it, a continuation of its owner, a review of an authorized task.
    named, _ = task("Fix its CI", spec=f"pr_branch: {theirs}\nFix CI there.", kind="code",
                    labels=[f"pr_branch:{theirs}"])
    assert push.own_target(p, repo) == ("origin", theirs)
    task("Finish the loader", kind="code", labels=[f"continues:{src}", f"pr_branch:{theirs}"])
    assert push.own_target(p, repo) == ("origin", theirs)
    rev = p.db.add_task(f"Review #{named}: Fix its CI", "s", kind="review", depends_on=[named],
                        labels=[f"pr_branch:{theirs}"])
    monkeypatch.setenv("TTP_TASK", str(rev))
    _git_out(repo, "checkout", "-q", f"ttp/t{named}-fix-its-ci")
    assert push.own_target(p, repo) == ("origin", theirs)


def test_a_continuation_does_not_inherit_a_third_tasks_branch_its_spec_does_not_name(env, monkeypatch):
    """A task_add continuing a task that only inherited another task's ttp/t<id>-... branch carries
    no branch (no_pr_branch) unless its own spec has a `pr_branch:` line naming it. The branch counts
    as published, so only its owner being another task keeps it off."""
    p = make(env)
    from ttp import coordinator as coord, push, worktree
    monkeypatch.setattr(push, "never_published", lambda *a, **k: False)
    src = p.db.add_task("Speed up the loader", "s", kind="code")
    b = f"ttp/t{src}-speed-up-the-loader"
    p.db.update_task(src, status="done", branch=b)
    mid = p.db.add_task("Plan the cache", "s", kind="plan", labels=[f"pr_branch:{b}"])
    p.db.update_task(mid, status="failed")

    def labels(title):
        return json.loads(p.db.one("SELECT labels FROM tasks WHERE title=?", (title,))["labels"])

    assert coord.apply(p, [{"type": "task_add", "title": "Plan it again", "kind": "plan", "continues": mid,
                            "spec": "Redo the cache plan."}]) == []
    assert worktree.NO_PR_BRANCH in labels("Plan it again")
    again = p.db.one("SELECT id FROM tasks WHERE title='Plan it again'")["id"]
    assert any(f"#{again} continues #{mid} but not its branch {b} (#{src}'s branch" in n
               for n in p.db.kv(coord.NOTES_KEY))
    assert coord.apply(p, [{"type": "task_add", "title": "Fix the loader PR", "kind": "code", "continues": mid,
                            "spec": f"pr_branch: {b}\nFix the PR."}]) == []
    assert f"pr_branch:{b}" in labels("Fix the loader PR") and worktree.NO_PR_BRANCH not in labels("Fix the loader PR")


def test_the_daemon_clears_push_markers_of_dead_runs_and_logs_it(env):
    """A push marker left `running` by a run that died (its run lock free: killed, crashed, rebooted)
    is recorded as failed by the daemon's sweep (push.tend), which returns a line for its log; a
    finished marker past KEEP_S is removed, and a live push (its lock held) is left alone."""
    from ttp import push, locks
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True, exist_ok=True)
    dead = folder / "t603-20261013-022410-57783.json"
    dead.write_text(json.dumps({"id": dead.stem, "status": "running", "task": "603", "pid": 57783,
                                "target": "origin/ttp/t598-plan", "started": time.time() - 3600}))
    live = folder / "t604-20261013-022410-57784.json"
    live.write_text(json.dumps({"id": live.stem, "status": "running", "task": "604", "pid": os.getpid(),
                                "target": "origin/ttp/t604-x", "started": time.time()}))
    old = folder / "t1-20260101-000000-1.json"
    old.write_text(json.dumps({"id": old.stem, "status": "pushed", "ended": 0}))
    os.utime(old, (time.time() - push.KEEP_S - 60,) * 2)
    held = locks.try_take([push._run_lock(live)], "test", "test")
    try:
        lines = push.tend(p)
        assert push.tend(p) == [], "a marker is reported once"
    finally:
        held.close()
    assert json.loads(dead.read_text())["status"] == "failed"
    assert json.loads(live.read_text())["status"] == "running"
    assert not old.exists()
    assert any(f"cleared stale push marker {dead.name} of task 603 (origin/ttp/t598-plan)" in ln for ln in lines), lines
    assert any("removed 1 finished push marker" in ln for ln in lines), lines
    assert not [ln for ln in lines if live.name in ln]


def test_own_push_refuses_a_task_branch_other_than_the_one_its_run_was_given(env, monkeypatch, capsys):
    """A worktree that left the branch its task's run was given (the row's branch) for another of the
    task's own ttp/t<id>-... branches is refused by `ttp push --own`, naming both branches: it would
    otherwise publish the new branch's head onto the carried branch, which another task owns. On the
    given branch it publishes as before: onto the carried branch, or under its own name."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    owner = p.db.add_task("First fix", "s", kind="code")
    carried = f"ttp/t{owner}-first-fix"
    _git_out(other, "push", "-q", "origin", f"HEAD:refs/heads/{carried}")
    theirs = _git_out(origin, "rev-parse", carried)
    tid = p.db.add_task("Follow up", f"pr_branch: {carried}", kind="code", labels=[f"pr_branch:{carried}"])
    p.db.update_task(tid, status="running", branch=carried)
    monkeypatch.setenv("TTP_TASK", str(tid))
    _git_out(repo, "checkout", "-q", "-B", carried, theirs)
    assert push.own_target(p, repo) == ("origin", carried)   # the normal case: on the given branch
    new = f"ttp/t{tid}-new"
    _git_out(repo, "checkout", "-q", "-B", new)
    _commit(repo, "new.py", "x\n")
    with pytest.raises(ValueError, match=rf"{re.escape(new)} checked out.*delivers from {re.escape(carried)}"):
        push.own_target(p, repo)
    capsys.readouterr()
    assert _ttp("push", "--own") != 0
    err = capsys.readouterr().err
    assert new in err and carried in err, err
    assert _git_out(origin, "rev-parse", carried) == theirs
    assert new not in _git_out(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    # Given its own branch (the carried one was held elsewhere): that branch publishes onto the
    # carried one; a second own branch is refused, naming the given branch and where it goes.
    given = f"ttp/t{tid}-follow-up"
    p.db.update_task(tid, branch=given)
    with pytest.raises(ValueError, match=rf"delivers from {re.escape(given)}, which it publishes onto {re.escape(carried)}"):
        push.own_target(p, repo)
    _git_out(repo, "checkout", "-q", "-B", given)
    assert push.own_target(p, repo) == ("origin", carried)
    # A task with no pr_branch: its given branch goes under its own name, another of its own does not.
    plain = p.db.add_task("Plain", "s", kind="code")
    p.db.update_task(plain, status="running", branch=f"ttp/t{plain}-plain")
    monkeypatch.setenv("TTP_TASK", str(plain))
    _git_out(repo, "checkout", "-q", "-B", f"ttp/t{plain}-plain")
    assert push.own_target(p, repo) == ("origin", f"ttp/t{plain}-plain")
    _git_out(repo, "checkout", "-q", "-B", f"ttp/t{plain}-other")
    with pytest.raises(ValueError, match=f"task #{plain} delivers from ttp/t{plain}-plain"):
        push.own_target(p, repo)
    # Its notes branch still publishes itself.
    _git_out(repo, "checkout", "-q", "-B", f"ttp/t{plain}-notes")
    assert push.own_target(p, repo) == ("origin", f"ttp/t{plain}-notes")


def test_own_push_publishes_a_fresh_own_branch_made_off_an_inherited_one(env, monkeypatch):
    """A task given another task's branch it only inherited (through `continues`, no `pr_branch:` line
    in its spec) whose spec asks for a fresh branch publishes its own ttp/t<id>-... branch under its
    own name with `ttp push --own`, never onto the inherited one. Another task's branch stays refused,
    and so does a second own branch when the given one is the task's own."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    owner = p.db.add_task("Earlier", "s", kind="code")
    inherited = f"ttp/t{owner}-earlier"
    _git_out(other, "push", "-q", "origin", f"HEAD:refs/heads/{inherited}")
    theirs = _git_out(origin, "rev-parse", inherited)
    p.db.update_task(owner, status="done", branch=inherited)
    tid = p.db.add_task("Fresh", "start a new ttp/* branch from the named base", kind="code",
                        labels=[f"continues:{owner}", f"pr_branch:{inherited}"])
    p.db.update_task(tid, status="running", branch=inherited)
    monkeypatch.setenv("TTP_TASK", str(tid))
    fresh = f"ttp/t{tid}-from-base"
    _git_out(repo, "checkout", "-q", "-B", fresh)
    _commit(repo, "fresh.py", "x\n")
    assert push.own_target(p, repo) == ("origin", fresh)
    assert _git_out(origin, "rev-parse", inherited) == theirs
    # Another task's branch checked out is still not this task's to publish.
    _git_out(repo, "checkout", "-q", "-B", f"ttp/t{owner + 1000}-elsewhere")
    with pytest.raises(ValueError, match="only this task's own branch"):
        push.own_target(p, repo)
    # Its spec naming the inherited branch makes it the one to deliver on: the fresh branch is refused.
    p.db.update_task(tid, spec=f"pr_branch: {inherited}")
    _git_out(repo, "checkout", "-q", fresh)
    with pytest.raises(ValueError, match=rf"{re.escape(fresh)} checked out.*delivers from {re.escape(inherited)}"):
        push.own_target(p, repo)


def test_a_review_publishes_the_pr_branch_its_subject_carries_and_nothing_else(env, monkeypatch, capsys):
    """A review whose subject task (named by the `Review #<id>` title or its depends_on) carries a
    pr_branch that is another task's ttp/t<id>-... branch publishes that branch with `ttp push --own`
    from a worktree on it, fast-forward only. Any other task's branch is still refused, and a head
    the remote already has is reported as pushed without running the checks or pushing again."""
    from ttp import push
    log = env["tmp"] / "checked"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"git rev-parse HEAD >> {log}"])
    src = p.db.add_task("Fix the mask", "s", kind="code")
    b = f"ttp/t{src}-fix-the-mask"
    p.db.update_task(src, status="done", branch=b)
    pub = p.db.add_task("Publish the mask fix", f"pr_branch: {b}\nPublish it.", kind="code",
                        labels=[f"pr_branch:{b}"])
    p.db.update_task(pub, status="done", branch=b)
    by_title = p.db.add_task(f"Review #{pub}: Publish the mask fix", "s", kind="review", labels=["review_only"])
    by_dep = p.db.add_task("Check the publish", "s", kind="review", depends_on=[pub])
    _git_out(repo, "checkout", "-q", "-B", b)
    _commit(repo, "mask.py", "fixed\n")   # local only, as the finished task left it
    head = _git_out(repo, "rev-parse", "HEAD")
    for rev in (by_title, by_dep):
        monkeypatch.setenv("TTP_TASK", str(rev))
        assert push.own_target(p, repo) == ("origin", b), rev
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert _git_out(origin, "rev-parse", b) == head
    assert log.read_text().split() == [head]
    # Already there: success, with no checks and no push.
    capsys.readouterr()
    assert _ttp("push", "--own") == 0
    assert "nothing to push" in capsys.readouterr().err
    assert log.read_text().split() == [head]
    # An unrelated task's branch is refused, for a review of any subject and for a non-review.
    stray = p.db.add_task("Something else", "s", kind="code", labels=["pr_branch:feature/x"])
    _git_out(repo, "checkout", "-q", "-B", f"ttp/t{stray}-something-else")
    _commit(repo, "stray.py", "x\n")
    plain = p.db.add_task("Not a review", "s", kind="code", depends_on=[pub])
    for task in (by_title, by_dep, plain):
        monkeypatch.setenv("TTP_TASK", str(task))
        with pytest.raises(ValueError, match="publishes only this task's own branch"):
            push.own_target(p, repo)
    # A review's subject carrying the branch is what lets it through: a non-review never publishes it.
    _git_out(repo, "checkout", "-q", b)
    monkeypatch.setenv("TTP_TASK", str(plain))
    with pytest.raises(ValueError, match="publishes only this task's own branch"):
        push.own_target(p, repo)
    assert _git_out(origin, "rev-parse", b) == head


def test_a_task_carrying_a_branch_works_on_it_in_a_worktree_and_publishes_onto_it(env, monkeypatch, capsys):
    """A task labelled pr_branch:<branch>, of any kind but review or harness, runs in a worktree on that
    branch (made from the remote's when there is no local one) and `ttp push --own` fast-forwards it.
    When it cannot check the branch out (another worktree holds it, or it is a shared branch), the task
    works on its own branch from that branch's head."""
    from pathlib import Path
    from ttp import prompts, push, worktree
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    src = p.db.add_task("Speed up the loader", "s", kind="code")
    b = f"ttp/t{src}-speed-up-the-loader"
    p.db.update_task(src, status="done", branch=b)
    _git_out(repo, "branch", b)
    wt = env["tmp"] / "pr"
    _git_out(repo, "worktree", "add", "-q", str(wt), b)
    _commit(wt, "pr.txt", "pr\n")
    _git_out(wt, "push", "-q", "origin", f"HEAD:refs/heads/{b}")
    _git_out(repo, "worktree", "remove", str(wt))
    _git_out(repo, "branch", "-D", b)   # only the remote has it now
    d = Daemon(p.base)
    task = p.db.task(p.db.add_task("Answer the PR comments", "s", kind="work",
                                   labels=[f"continues:{src}", f"pr_branch:{b}"]))
    cwd, branch = d._workdir_for(task)
    assert Path(cwd) == p.worktrees / f"t{task['id']}" and branch == b
    assert _git_out(cwd, "rev-parse", "HEAD") == _git_out(origin, "rev-parse", b)
    assert f"delivers onto branch {b}" in prompts.worker_task(p, task, cwd, branch)
    _commit(Path(cwd), "fix.txt", "fix\n")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("TTP_TASK", str(task["id"]))
    assert push.resolve(p, Path(cwd), own=True) == ("origin", b, True)
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert _git_out(origin, "rev-parse", b) == _git_out(cwd, "rev-parse", "HEAD")
    # Held by that worktree: the next one works on its own branch from the carried branch's head.
    monkeypatch.chdir(repo)
    held = p.db.task(p.db.add_task("More PR comments", f"pr_branch: `{b}`", kind="work",
                                   labels=[f"pr_branch:{b}"]))
    cwd2, branch2 = d._workdir_for(held)
    assert branch2 == f"ttp/t{held['id']}-more-pr-comments"
    assert _git_out(cwd2, "rev-parse", "HEAD") == _git_out(origin, "rev-parse", b)
    monkeypatch.setenv("TTP_TASK", str(held["id"]))
    assert push.resolve(p, Path(cwd2), own=True) == ("origin", b, True)
    # A shared branch is never checked out as the task's branch.
    shared = p.db.task(p.db.add_task("On the push branch", "s", kind="code", labels=["pr_branch:proj"]))
    assert worktree.on_carried(p, shared) == ""
    assert d._workdir_for(shared)[1] == f"ttp/t{shared['id']}-on-the-push-branch"
    # A review still works in the change's worktree.
    review = p.db.task(p.db.add_task("Review it", "s", kind="review", labels=[f"pr_branch:{b}"]))
    assert d._workdir_for(review) == (str(p.root), None)


# Wordings of the usual delivery rules (the review pushes, not the worker): none is a ban on pushing.
USUAL_PUSH_RULES = ("Do not push.", "Never push to the push branch yourself.",
    "Do not push to work directly; the review pushes it.", "Never push to main.",
    "Never push to the push branches of other projects.",
    "Only a change whose whole diff is kept off the push branch by "
    "delivery.push_exclude_paths is review only.",
    "With the push queue on, workers never push to the push branch; the daemon does.",
    "Do not push to the push branch: the review pushes it.",
    "Workers must not push to the push branch themselves.",
    "Fix: a notes commit cannot reach the push branch, so drop it.",
    "Never push it to the push branch yourself.",
    # Prose is never a ban, not even one naming the change: only the marker line.
    "Write the notes. They must not be pushed to the push branch.",
    "Never push this to work.", "This change must not reach the push branch.",
    "These notes must not be pushed to work.", "Keep this change off origin/work.",
    "or (b) the code task's spec or hand-off says the change must not be pushed "
    "to the push branch",
    "A review of a change whose spec says it must not reach the push branch is "
    "review only.",
    "Detect when the hand-off says this change must not be pushed to the push branch.",
    "If it must not reach the push branch, the review is review only.",
    'Such a spec names the change as what is banned ("this change must not be '
    'pushed to the push branch")',
    "Rely on one explicit spec marker on its own line, e.g. a line starting "
    "`no_push:` (the reason follows).",
    "no_push: false", "Set no_push: true in the hand-off.")


def test_the_review_of_a_change_that_must_not_reach_the_push_branch_has_no_push_step(env, monkeypatch):
    # A notes-only change (every file push-excluded), or one its spec or hand-off keeps off the push
    # branch, gets a review-only spec: no `ttp push` and no push-queue approval, with the reason.
    from ttp import push, pushq
    p = make(env)
    _with_origin(env, clone=False)   # a repo with no remote never pushes: review only
    p.set_config("delivery.push_branch", "work")
    p.set_config("delivery.push_exclude_paths", ["notes", "*.log"])
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)   # no prechecks: the spec text is the subject

    def review(title, files, spec=None, result=None):
        before = (lambda tid, head: p.db.update_task(tid, spec=spec)) if spec else None
        _, _, _, (rev,) = _finish_code(env, p, title, files, result=result, before=before)
        return rev

    def review_only(rev):
        return "ttp push` from" not in rev["spec"] and "Review only" in rev["spec"] \
            and push.REVIEW_ONLY_LABEL in json.loads(rev["labels"]) and rev["spec"].rstrip().endswith("findings.")

    def pushes(rev):
        return "ttp push` from" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"])

    for on in (False, True):
        p.set_config("delivery.push_queue", on)
        rev = review(f"notes {on}", {"notes/a.md": 5, "run.log": 2})
        assert review_only(rev) and "delivery.push_exclude_paths keeps off" in rev["spec"], (on, rev["spec"])
        assert pushes(review(f"mixed {on}", {"notes/b.md": 5, "app.py": 3})), on
        rev = review(f"forbidden {on}", {"c.py": 3}, spec="Write the notes.\nno_push: notes stay local\n")
        assert review_only(rev) and "notes stay local" in rev["spec"], on
        for i, ban in enumerate(("no_push:", "  no_push: `a local tool`", "Fix it.\n\nno_push: true\nThen stop.")):
            assert review_only(review(f"ban {on} {i}", {f"d{i}.py": 3}, spec=ban)), (on, ban)
        rev = review(f"flag {on}", {"e.py": 3}, result={"status": "done", "summary": "ok", "no_push": "a local tool"})
        assert review_only(rev) and "a local tool" in rev["spec"], on
        # The usual rule that the review pushes, not the worker, is not a prohibition. The review's
        # push step follows push.kept_off of the code task alone (the daemon passes it the task and its
        # diff), so each wording is checked there and two of them go through a whole review.
        d = p.config()["delivery"]
        for i, usual in enumerate(USUAL_PUSH_RULES):
            task = p.db.task(p.db.add_task(f"usual {on} {i}", usual, kind="code", origin="coordinator"))
            assert push.kept_off(task, {f"g{i}.py": 3}, d, p) == "", (on, usual)
        for i in (0, USUAL_PUSH_RULES.index("This change must not reach the push branch.")):
            assert pushes(review(f"usual {on} {i}", {f"g{i}.py": 3}, spec=USUAL_PUSH_RULES[i])), (on, i)
        rev = review(f"summary {on}", {"f.py": 3},
                     result={"status": "done", "summary": "Done; keep it off the push branch."})
        assert pushes(rev), on
    # No push branch: review only as before, without the reason.
    p.set_config("delivery.push_branch", "")
    rev = review("no branch", {"notes/c.md": 5})
    assert "Review only" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"])


def test_kept_off_explicit_no_push_false_in_a_continuation_ends_the_walk(env, monkeypatch):
    # `no_push: false` in a continuation's spec, or no_push false in its hand-off, lifts the
    # ancestor's ban for it and for its own continuations; silence still inherits.
    from ttp import push, pushq
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "work")
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    d = p.config()["delivery"]

    def code(title, spec="", labels=None, result=None):
        tid = p.db.add_task(title, spec, kind="code", origin="coordinator", labels=labels)
        if result:
            p.db.update_task(tid, result=json.dumps(result))
        return p.db.task(tid)

    root = code("notes", "no_push: notes stay local\n")
    lifted = code("continue: notes", "Go on.\nno_push: false\n", labels=[f"continues:{root['id']}"])
    assert push.kept_off(lifted, None, d, p) == ""
    assert push.kept_off(code("fix", "", labels=[f"continues:{lifted['id']}"]), None, d, p) == ""
    for flag in (False, "false", 0):
        h = code("handed", "", labels=[f"continues:{root['id']}"],
                 result={"status": "done", "summary": "ok", "no_push": flag})
        assert push.kept_off(h, None, d, p) == "", flag
    assert "notes stay local" in push.kept_off(code("quiet", "", labels=[f"continues:{root['id']}"]), None, d, p)


def test_kept_off_inherits_no_push_from_the_tasks_it_continues(env, monkeypatch):
    # A continuation (a `continue:` task, or a fix of a review's findings) keeps the nearest ancestor's
    # no_push: its review is review only too. A chain without the marker stays pushable; a cycle ends.
    from ttp import push, pushq
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "work")
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    d = p.config()["delivery"]

    def code(title, spec="", labels=None, result=None):
        tid = p.db.add_task(title, spec, kind="code", origin="coordinator", labels=labels)
        if result:
            p.db.update_task(tid, result=json.dumps(result))
        return p.db.task(tid)

    root = code("notes", "Write the notes.\nno_push: notes stay local\n")
    cont = code("continue: notes", "Go on.", labels=[f"continues:{root['id']}"])
    fix = code("Fix review #9: notes", "", labels=[f"continues:{cont['id']}", "review_fix:9"])
    why = push.kept_off(fix, {"a.py": 1}, d, p)
    assert "notes stay local" in why and f"#{root['id']} (which it continues)" in why, why
    assert "notes stay local" in push.kept_off(cont, None, d, p)
    assert push.kept_off(fix, None, d) == ""   # without the project only its own marker counts
    flagged = code("tool", "", result={"status": "done", "summary": "ok", "no_push": "a local tool"})
    assert "a local tool" in push.kept_off(code("more", "", labels=[f"continues:{flagged['id']}"]), None, d, p)
    # The nearest ancestor's reason wins; the task's own marker before any.
    mid = code("mid", "no_push: nearer", labels=[f"continues:{root['id']}"])
    assert "nearer" in push.kept_off(code("leaf", "", labels=[f"continues:{mid['id']}"]), None, d, p)
    assert "its spec" in push.kept_off(code("own", "no_push: mine", labels=[f"continues:{root['id']}"]), None, d, p)
    # No marker anywhere in the chain: pushable. A cycle of continues labels ends.
    plain = code("plain", "Just code.")
    assert push.kept_off(code("next", "", labels=[f"continues:{plain['id']}"]), None, d, p) == ""
    a = code("loop a", "")
    b = code("loop b", "", labels=[f"continues:{a['id']}"])
    p.db.update_task(a["id"], labels=json.dumps([f"continues:{b['id']}"]))
    assert push.kept_off(p.db.task(a["id"]), None, d, p) == ""
    # End to end: the review of a fix of a no_push task is review only; that of a plain chain pushes.
    _, _, _, (rev,) = _finish_code(env, p, "fix of notes", {"b.py": 3},
                                   labels=[f"continues:{root['id']}", "review_fix:9"])
    assert "Review only" in rev["spec"] and "notes stay local" in rev["spec"], rev["spec"]
    assert push.REVIEW_ONLY_LABEL in json.loads(rev["labels"]) and "ttp push` from" not in rev["spec"]
    _, _, _, (rev,) = _finish_code(env, p, "fix of plain", {"c.py": 3}, labels=[f"continues:{plain['id']}"])
    assert "ttp push` from" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"])


def test_allow_protected_push_branch_lets_reviews_push_to_main_and_without_it_they_never_git_push(env, monkeypatch, capsys):
    from ttp import cli, push, pushq
    from ttp.project import config_problems
    p = make(env)
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)   # no prechecks: the spec text is the subject
    for ref in ("main", "origin/master", "refs/heads/main"):
        assert push.push_branch_problem(ref, allow=True) == "", ref
        assert "allow_protected_push_branch" in push.push_branch_problem(ref)
    assert "always refuses" in push.push_branch_problem("HEAD", allow=True)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "main")

    def review(title):
        _, _, _, (rev,) = _finish_code(env, p, title, {f"{title}.py": 3})
        return rev

    rev = review("off")
    assert "Review only" in rev["spec"] and "never fall back to a raw `git push` to main" in rev["spec"]
    assert push.REVIEW_ONLY_LABEL in json.loads(rev["labels"]) and "ttp push` from" not in rev["spec"]
    cli.main(["doctor", p.name])
    out = capsys.readouterr().out
    assert "delivery.allow_protected_push_branch" in out and "raw `git push`" in out
    p.set_config("delivery.allow_protected_push_branch", True)
    assert not any("push_branch" in x for x in config_problems(p.raw_config(), p.root))
    rev = review("on")
    assert "ttp push` from" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"])
    # The prompts say the same: the reviewer never falls back to git push; only the user's word sets the key.
    text = (RUNTIME.parent / "template" / "prompts" / "kind-review.md").read_text()
    assert "NEVER fall back to a raw `git push` to that branch" in text
    coordinator = (RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text()
    rule = next(line for line in coordinator.splitlines() if line.startswith("| `config_set`"))
    assert "`delivery.allow_protected_push_branch`" in rule and rule.index("allow_protected") < rule.index("only on the user's word")
    assert "never a raw `git push` to it" in coordinator


def test_a_push_branch_no_push_can_reach_is_flagged_and_its_reviews_are_review_only(env, monkeypatch, capsys):
    # delivery.push_branch names main/master (ttp push always refuses it), or the code repo has no
    # remote: every review push would exit 2. The config check and doctor say so, and the review
    # spec has no push step.
    from ttp import cli, push, pushq
    from ttp.project import config_problems
    p = make(env)
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)   # no prechecks: the spec text is the subject
    # No repo to ask: only the name, its first part read either way.
    for ref in ("main", "master", "origin/main", "upstream/master", "refs/heads/main", "origin/refs/heads/master"):
        assert "always refuses" in push.push_branch_problem(ref), ref
    for ref in ("", None, "work", "origin/work", "maintenance", "main-next"):
        assert push.push_branch_problem(ref) == "", ref
    # A repo with no remote: any push branch.
    assert "no git remote" in push.push_branch_problem("work", p.root)
    assert any("no git remote" in x for x in config_problems({"delivery": {"push_branch": "work"}}, p.root))
    assert config_problems({"delivery": {"push_branch": "work"}}) == []
    # Not a git repo: only the name.
    assert push.push_branch_problem("work", env["tmp"] / "nowhere") == ""

    def review(title):
        _, _, _, (rev,) = _finish_code(env, p, title, {f"{title}.py": 3})
        return rev

    def review_only(rev, why):
        return "ttp push` from" not in rev["spec"] and "Review only" in rev["spec"] and why in rev["spec"] \
            and push.REVIEW_ONLY_LABEL in json.loads(rev["labels"]) and rev["spec"].rstrip().endswith("findings.")

    p.set_config("delivery.push_branch", "work")
    for on in (False, True):
        p.set_config("delivery.push_queue", on)
        assert review_only(review(f"noremote{on}"), "no git remote"), on
    cli.main(["doctor", p.name])
    assert "project.json: delivery.push_branch: the code repo has no git remote" in capsys.readouterr().out
    # With a remote: a protected branch, bare or behind the remote's name, never pushes either.
    _with_origin(env, clone=False)
    _git_out(p.root, "remote", "add", "upstream", str(env["tmp"] / "remote.git"))
    for i, ref in enumerate(("main", "origin/master", "upstream/main")):
        p.set_config("delivery.push_branch", ref)
        assert review_only(review(f"protected{i}"), "a branch ttp push always refuses"), ref
        assert any("always refuses" in x for x in config_problems(p.raw_config(), p.root)), ref
    cli.main(["doctor", p.name])
    assert "project.json: delivery.push_branch: upstream/main is main, a branch ttp push always refuses" \
        in capsys.readouterr().out
    # A remote the name does not use: origin is the fallback, and missing it never pushes.
    p.set_config("delivery.push_branch", "work")
    _git_out(p.root, "remote", "remove", "origin")
    assert review_only(review("noorigin"), "which is not a git remote")
    # A branch of its own on a remote the repo has: the review pushes as before; "team/main" is a
    # branch of origin, not main.
    _git_out(p.root, "remote", "add", "origin", str(env["tmp"] / "remote.git"))
    for i, ref in enumerate(("work", "upstream/work", "team/main")):
        p.set_config("delivery.push_branch", ref)
        rev = review(f"ok{i}")
        assert "ttp push` from" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"]), ref
        assert not any("push_branch" in x for x in config_problems(p.raw_config(), p.root)), ref


def test_the_push_queue_refuses_a_head_carrying_commits_of_a_failed_review_unless_its_review_accepts_them(
        env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    x, xbranch, xhead, _ = _pq_stacked(s, "rejected", review="failed")
    # Stacked on the rejected branch: refused, naming the task and its commit.
    c, cbranch, chead, crev = _pq_stacked(s, "stacked", on=xbranch, review="running")
    check = pushq.check_approval(s.p, s.p.db.task(crev), [{"branch": cbranch, "head": chead}])
    assert f"carries unreviewed commits from #{x}," in check.get("invalid", ""), check
    assert "edit rejected.txt" in check["invalid"] and '"inherited": [%d]' % x in check["invalid"]
    # The review judged them and lists the task: approved, and the feed line says so.
    check = pushq.check_approval(s.p, s.p.db.task(crev), [{"branch": cbranch, "head": chead, "inherited": [x]}])
    assert check.get("entries") and check["inherited"] == {chead: [x]}, check
    assert f"carries commits of #{x}" in pushq.queued_text(s.p.db.task(crev), check)
    # A rebased copy of the rejected commit (another sha, the same patch) is caught too.
    d, dbranch, dhead, drev = _pq_stacked(s, "copied", picks=[xhead], review="running")
    assert _git_out(s.path, "rev-parse", f"{dbranch}~1") != xhead
    check = pushq.check_approval(s.p, s.p.db.task(drev), [{"branch": dbranch, "head": dhead}])
    assert f"from #{x}," in check.get("invalid", ""), check
    # The rejected task's own re-review (or a fix that continues it) covers it: not refused.
    rr = s.p.db.add_task("re-review", "r", kind="review", origin="coordinator", depends_on=[x])
    assert pushq.check_approval(s.p, s.p.db.task(rr), [{"branch": xbranch, "head": xhead}]).get("entries")
    # A later passing review of the rejected task clears it.
    s.p.db.update_task(rr, status="done")
    assert pushq.check_approval(s.p, s.p.db.task(crev), [{"branch": cbranch, "head": chead}]).get("entries")
    # An unrelated head is approved as before.
    assert pushq.check_approval(s.p, s.p.db.task(s.review), [{"branch": s.branch, "head": s.head}]).get("entries")


def test_the_push_queue_accepts_a_head_stacked_on_a_passed_branch_landed_or_not(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    p_, pbranch, phead, prev = _pq_stacked(s, "passed", review="done")
    # A rejected sibling stacked on the passed branch holds its commits too: they stay the passed task's.
    _pq_stacked(s, "sibling", on=pbranch, review="failed")
    c, cbranch, chead, crev = _pq_stacked(s, "stacked", on=pbranch, review="running")
    entries = [{"branch": cbranch, "head": chead}]
    check = pushq.check_approval(s.p, s.p.db.task(crev), entries)
    assert check.get("entries") and "inherited" not in check, check
    # Landed as a rebased copy (as a batch lands it) and its review gone: its patch is on the tip.
    s.p.db.update_task(prev, status="cancelled")
    _git_out(s.other, "fetch", "-q", str(s.repo), pbranch)
    _git_out(s.other, "cherry-pick", "-x", "FETCH_HEAD")
    _git_out(s.other, "push", "-q", "origin", "HEAD:proj")
    _git_out(s.repo, "fetch", "-q", "origin")
    assert _git_out(s.repo, "rev-parse", "origin/proj") != phead
    check = pushq.check_approval(s.p, s.p.db.task(crev), entries)
    assert check.get("entries"), check


def test_a_daemon_queued_review_names_the_rejected_commits_its_head_carries(env, monkeypatch):
    from ttp import pushq
    from ttp.daemon import Daemon
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(Daemon, "_precheck", lambda self, task, head: None)
    x, xbranch, _, _ = _pq_stacked(s, "rejected", review="failed")
    c, cbranch, chead, _ = _pq_stacked(s, "stacked", on=xbranch)
    d = Daemon(s.p.base)
    rid, added = d._auto_review(s.p.db.task(c), "made the change")
    spec = s.p.db.task(rid)["spec"]
    assert added and f"carries unreviewed commits from tasks whose latest review failed (#{x}: " in spec, spec
    assert "edit rejected.txt" in spec and '"inherited": [%d]' % x in spec
    # What the spec names is what the queue would refuse the review's approval for, and accept once listed.
    entry = {"branch": cbranch, "head": chead}
    assert f"from #{x}," in pushq.check_approval(s.p, s.p.db.task(rid), [entry]).get("invalid", "")
    assert pushq.check_approval(s.p, s.p.db.task(rid), [{**entry, "inherited": [x]}]).get("entries")
    # A head with no rejected commits under it: no such line.
    f, _, _, _ = _pq_stacked(s, "fresh")
    rid, added = d._auto_review(s.p.db.task(f), "made the change")
    assert added and "unreviewed commits" not in s.p.db.task(rid)["spec"]


def _held_review(env, monkeypatch, fix_status="queued", s=None, name="rejected"):
    """A code task stacked on a rejected one (#x, review failed) whose fix is `fix_status`, and the
    daemon's review of it: (s, daemon, x, fix, review id). With `s`, in that _pq project."""
    from ttp.daemon import Daemon
    if s is None:
        s = _pq(env, monkeypatch)
        monkeypatch.setattr(Daemon, "_precheck", lambda self, task, head: None)
    x, xbranch, _, xrev = _pq_stacked(s, name, review="failed")
    fix = s.p.db.add_task(f"fix #{x}", "f", kind="code", origin="daemon",
                          labels=[f"continues:{x}", f"review_fix:{xrev}"])
    s.p.db.update_task(fix, status=fix_status)
    c, _, _, _ = _pq_stacked(s, f"on-{name}", on=xbranch)
    d = Daemon(s.p.base)
    rid, added = d._auto_review(s.p.db.task(c), "made the change")
    assert added
    return s, d, x, fix, rid


def _review_ready(db, tid):
    return tid in {t["id"] for t in db.ready_tasks()}


def test_a_daemon_queued_review_is_held_while_a_carried_change_s_fix_is_open(env, monkeypatch):
    from ttp import coordinator as coord, reviewhold
    from ttp.db import deferral
    s, d, x, fix, rid = _held_review(env, monkeypatch)
    t = s.p.db.task(rid)
    assert deferral(t)["when"] == f"landed:#{x}" and reviewhold.hold_of(t)[1] == [x], t["labels"]
    assert not _review_ready(s.p.db, rid)
    assert f"#{x}'s fix #{fix} lands" in coord.starts_text(t, plain=True), coord.starts_text(t, plain=True)
    d.release_review_holds(time.time())
    assert not _review_ready(s.p.db, rid) and reviewhold.hold_of(s.p.db.task(rid))
    # The hold lives in the task's labels: a restarted daemon keeps it.
    from ttp.daemon import Daemon
    Daemon(s.p.base).release_review_holds(time.time())
    assert not _review_ready(s.p.db, rid) and reviewhold.hold_of(s.p.db.task(rid))


def test_a_held_review_is_released_once_the_carried_fix_lands(env, monkeypatch):
    from ttp import reviewhold
    s, d, x, fix, rid = _held_review(env, monkeypatch)
    # The fix and its re-review passed and landed: the start_when probe passes.
    s.p.db.update_task(fix, status="done")
    rr = s.p.db.add_task(f"Review #{fix}: fix", "r", kind="review", origin="daemon", depends_on=[x, fix],
                         labels=[f"auto_review:{fix}"])
    s.p.db.update_task(rr, status="done")
    d._start_verdict(s.p.db.task(rid), 0, time.time())
    d.release_review_holds(time.time())
    t = s.p.db.task(rid)
    assert _review_ready(s.p.db, rid) and not reviewhold.hold_of(t), t["labels"]
    assert "unreviewed commits" not in t["spec"] and "ended" not in t["spec"], t["spec"]
    assert t["spec"].rstrip().splitlines()[-1].startswith("Return the verdict")


def test_a_held_review_wakes_at_once_when_the_carried_fix_chain_ends_failed_or_cancelled(env, monkeypatch):
    from ttp import reviewhold
    s = None
    for end in ("failed", "cancelled"):
        s, d, x, fix, rid = _held_review(env, monkeypatch, s=s, name=end)
        s.p.db.update_task(fix, status=end)
        d.release_review_holds(time.time())
        t = s.p.db.task(rid)
        assert _review_ready(s.p.db, rid) and not reviewhold.hold_of(t), t["labels"]
        assert f"The fix chain of carried task #{x} ended {end} (#{fix}) without landing" in t["spec"], t["spec"]
        # The inherited commits are still named, so the approval is not refused for them later.
        assert f"carries unreviewed commits from tasks whose latest review failed (#{x}: " in t["spec"]


def test_no_review_hold_when_the_carried_change_s_review_passed_none_ran_or_no_fix_is_open(env, monkeypatch):
    from ttp import reviewhold
    from ttp.daemon import Daemon
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(Daemon, "_precheck", lambda self, task, head: None)
    d = Daemon(s.p.base)
    for name, review in (("passed", "done"), ("unreviewed", None), ("unfixed", "failed")):
        x, xbranch, _, _ = _pq_stacked(s, name, review=review)
        c, _, _, _ = _pq_stacked(s, f"on-{name}", on=xbranch)
        rid, added = d._auto_review(s.p.db.task(c), "made the change")
        t = s.p.db.task(rid)
        assert added and not reviewhold.hold_of(t) and _review_ready(s.p.db, rid), (name, t["labels"])
    # A fix that already ended: nothing open to wait for.
    _, _, _, _, rid = _held_review(env, monkeypatch, fix_status="cancelled", s=s, name="fixed")
    assert not reviewhold.hold_of(s.p.db.task(rid)) and _review_ready(s.p.db, rid)


def test_the_review_hold_cap_releases_it_with_a_line_saying_so(env, monkeypatch):
    from ttp import reviewhold
    s, d, x, fix, rid = _held_review(env, monkeypatch)
    d.release_review_holds(time.time() + 23 * 3600)
    assert not _review_ready(s.p.db, rid)
    d.release_review_holds(time.time() + 25 * 3600)
    t = s.p.db.task(rid)
    assert _review_ready(s.p.db, rid) and not reviewhold.hold_of(t), t["labels"]
    assert f"for the fix of #{x} (#{fix} still open)" in t["spec"], t["spec"]
    # The cap is configurable; 0 turns it off.
    s, d, x, fix, rid = _held_review(env, monkeypatch, s=s, name="again")
    d.cfg["review"]["carried_hold_max_h"] = 0
    d.release_review_holds(time.time() + 1000 * 3600)
    assert not _review_ready(s.p.db, rid)
    d.cfg["review"]["carried_hold_max_h"] = 1
    d.release_review_holds(time.time() + 2 * 3600)
    assert _review_ready(s.p.db, rid)


def _pq_add_reviewed_tasks(db, start, stop):
    """Code tasks start..stop-1 on branches of their own, each with a finished review naming its branch
    in the spec (passed and failed in turn)."""
    with db.conn:
        for i in range(start, stop):
            db.conn.execute("INSERT INTO tasks (created, updated, title, spec, kind, status, branch) "
                            "VALUES (?, ?, ?, '', 'code', 'done', ?)",
                            (time.time(), time.time(), f"t{i}", f"ttp/t{9000 + i}-change-{i}"))
            db.conn.execute("INSERT INTO tasks (created, updated, title, spec, kind, status) "
                            "VALUES (?, ?, ?, ?, 'review', ?)",
                            (time.time(), time.time(), f"check {i}",
                             f"Check ttp/t{9000 + i}-change-{i} against the spec.", ("done", "failed")[i % 2]))


def test_the_inherited_commit_check_stays_fast_with_many_tasks_and_reviews(env, monkeypatch):
    # check_approval runs in the daemon's result path: its work must not grow with tasks x reviews.
    # Counted, not timed against the clock (a loaded machine makes any wall-clock bound flaky): git
    # calls stay the same however many tasks there are, rows read from the database grow linearly,
    # and its own CPU time grows sub-quadratically from n to 8n tasks and reviews.
    from ttp import pushq
    from ttp.db import DB
    s = _pq(env, monkeypatch)
    x, xbranch, _, _ = _pq_stacked(s, "rejected", review="failed")
    c, cbranch, chead, crev = _pq_stacked(s, "stacked", on=xbranch, review="running")
    entries = [{"branch": cbranch, "head": chead}]
    n = {"git": 0, "rows": 0}
    run, q, one = subprocess.run, DB.q, DB.one

    def counted_run(*a, **kw):
        n["git"] += 1
        return run(*a, **kw)

    def counted_q(self, *a, **kw):
        rows = q(self, *a, **kw)
        n["rows"] += len(rows)
        return rows

    def counted_one(self, *a, **kw):
        n["rows"] += 1
        return one(self, *a, **kw)

    def measure():
        task = s.p.db.task(crev)
        took = []
        for _ in range(3):
            n.update(git=0, rows=0)
            with monkeypatch.context() as m:
                m.setattr(pushq.subprocess, "run", counted_run)
                m.setattr(DB, "q", counted_q)
                m.setattr(DB, "one", counted_one)
                t0 = time.process_time()
                check = pushq.check_approval(s.p, task, entries)
                took.append(time.process_time() - t0)
            assert f"carries unreviewed commits from #{x}," in check.get("invalid", ""), check
        return dict(n, cpu=min(took))

    _pq_add_reviewed_tasks(s.p.db, 0, 100)
    small = measure()
    _pq_add_reviewed_tasks(s.p.db, 100, 800)
    big = measure()
    assert big["git"] == small["git"], f"git calls grew with the tasks: {small} -> {big}"
    assert big["rows"] - small["rows"] <= 10 * 700, f"rows read grew faster than the tasks: {small} -> {big}"
    # 8x the tasks: linear work grows at most 8x (much less, as git dominates), quadratic 64x.
    assert big["cpu"] < 16 * small["cpu"], f"CPU time grew quadratically: {small} -> {big}"


def test_a_review_only_stack_stays_review_only_through_its_fix_and_the_push_queue_ignores_its_approval(
        env, monkeypatch):
    from ttp import push, pushq
    from ttp.daemon import Daemon
    s = _pq(env, monkeypatch)
    entries = [{"branch": s.branch, "head": s.head}]
    assert pushq.check_approval(s.p, s.p.db.task(s.review), entries).get("entries"), "approved"
    s.p.db.update_task(s.review, labels=[push.REVIEW_ONLY_LABEL])
    check = pushq.check_approval(s.p, s.p.db.task(s.review), entries)
    assert "review only" in check.get("ignored", ""), check
    # A failed review-only review: its fix is told not to push, its re-review stays review only.
    s.p.db.update_task(s.review, status="failed")
    d = Daemon(s.p.base)
    out = d._fix_failed_review(s.p.db.task(s.review), [{"title": "bug", "spec": "fix it"}], "one bug")
    assert out, "no fix queued"
    fid, rid, _ = out
    assert "do not push it" in s.p.db.task(fid)["spec"] and "Leave the push" not in s.p.db.task(fid)["spec"]
    assert push.REVIEW_ONLY_LABEL in json.loads(s.p.db.task(rid)["labels"])
    # Its checks are not started for the push queue either.
    started = []
    monkeypatch.setattr(Daemon, "_precheck", lambda self, task, full: started.append(task["id"]))
    s.p.db.update_task(fid, branch=s.branch)
    d._precheck_open(s.p.db.task(fid), s.p.db.task(rid))
    assert started == []


def test_the_re_review_of_a_pr_branch_stack_carries_its_pr_branch_or_no_push_step(env, monkeypatch):
    """A code task labelled pr_branch:<branch> whose review fails: the daemon's fix and re-review keep
    delivering onto that branch. The re-review carries the label and says to publish the fix's head
    from the fix's branch, so `ttp push --own` from there (or from its own branch) publishes onto
    the pr_branch. A re-review that cannot carry it (the stack carries no pr_branch now, or own
    pushes are off) has no push step and never mentions --own."""
    from ttp import push, pushq, worktree
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/feature/x")

    def failed_stack(title):
        for t in p.db.q("SELECT id FROM tasks WHERE status NOT IN ('done','failed','cancelled')"):
            p.db.update_task(t["id"], status="cancelled")   # one stack at a time
        cid, _, _, (rev,) = _finish_code(env, p, title, {f"{title}.py": 3}, labels=["pr_branch:feature/x"])
        assert "`ttp push --own --detach`" in rev["spec"], rev["spec"]
        assert "pr_branch:feature/x" in json.loads(rev["labels"])
        p.db.update_task(rev["id"], status="failed")
        return cid, rev

    def fix_and_re_review(rev):
        out = Daemon(p.base)._fix_failed_review(p.db.task(rev["id"]), [{"title": "bug", "spec": "fix it"}], "bug")
        assert out, "no fix queued"
        return p.db.task(out[0]), p.db.task(out[1])

    cid, rev = failed_stack("one")
    fix, rr = fix_and_re_review(rev)
    labels = json.loads(rr["labels"])
    assert "pr_branch:feature/x" in labels and push.REVIEW_ONLY_LABEL in labels, labels
    assert (f"publish #{fix['id']}'s head onto its pr_branch feature/x with `ttp push --own --detach` from "
            f"#{fix['id']}'s worktree (branch {fix['branch']})") in rr["spec"], rr["spec"]
    # The fix leaves the push to the re-review.
    assert "do not push it" in fix["spec"] and "--own" not in fix["spec"]
    path, branch = worktree.ensure(p, fix)
    _commit(path, "fixed.py", "fixed\n")
    monkeypatch.setenv("TTP_TASK", str(rr["id"]))
    assert push.own_target(p, path) == ("origin", "feature/x")
    # From a branch of its own, too: the label is what it publishes onto.
    _git_out(path, "checkout", "-q", "-b", f"ttp/t{rr['id']}-re-review")
    assert push.own_target(p, path) == ("origin", "feature/x")

    # The stack no longer carries a pr_branch: no label, and the first review's --own step is left out.
    _, rev = failed_stack("two")
    for t in p.db.q("SELECT * FROM tasks WHERE kind='code' AND title='two'"):
        p.db.update_task(t["id"], labels=[worktree.NO_PR_BRANCH])
    _, rr = fix_and_re_review(rev)
    assert not any(lb.startswith("pr_branch:") for lb in json.loads(rr["labels"])), rr["labels"]
    assert "--own" not in rr["spec"] and "Review only" in rr["spec"], rr["spec"]
    assert "carries no pr_branch" in rr["spec"]

    # A first review older than its pr_branch label: its own step sentence still counts.
    _, rev = failed_stack("legacy")
    p.db.update_task(rev["id"], labels=[lb for lb in json.loads(rev["labels"]) if not lb.startswith("pr_branch:")])
    fix, rr = fix_and_re_review(rev)
    assert "pr_branch:feature/x" in json.loads(rr["labels"]), rr["labels"]
    assert f"publish #{fix['id']}'s head onto its pr_branch feature/x" in rr["spec"], rr["spec"]

    # Own pushes turned off since the first review: as the first review then would, the stack lands on
    # the push branch like any other change (no review-only label), and the re-review never mentions --own.
    _, rev = failed_stack("three")
    p.set_config("delivery.own_push", "off")
    fix, rr = fix_and_re_review(rev)
    labels = json.loads(rr["labels"])
    assert not any(lb.startswith("pr_branch:") for lb in labels) and push.REVIEW_ONLY_LABEL not in labels, labels
    assert "--own" not in rr["spec"] and "Review only" not in rr["spec"], rr["spec"]
    assert "Its branch label feature/x was ignored: own pushes are off" in rr["spec"], rr["spec"]
    assert f"push #{fix['id']}'s head with `ttp push` from #{fix['id']}'s worktree" in rr["spec"], rr["spec"]
    assert "do not push it" not in fix["spec"] and f"Leave the push to re-review #{rr['id']}" in fix["spec"]
    # ...unless nothing may reach the push branch: then review only, saying why.
    p.set_config("delivery.own_push", "on")
    _, rev = failed_stack("four")
    for t in p.db.q("SELECT * FROM tasks WHERE kind='code' AND title='four'"):
        p.db.update_task(t["id"], spec="no_push: an experiment")
    p.set_config("delivery.own_push", "off")
    _, rr = fix_and_re_review(rev)
    assert push.REVIEW_ONLY_LABEL in json.loads(rr["labels"]), rr["labels"]
    assert "--own" not in rr["spec"] and "Review only: own pushes are off" in rr["spec"], rr["spec"]
    assert "must not reach the push branch" in rr["spec"], rr["spec"]


def test_a_push_queue_stack_whose_spec_or_hand_off_mentions_own_keeps_its_push_step_on_re_review(env, monkeypatch):
    """A plain stack (no pr_branch) whose code spec and hand-off mention `ttp push --own`: the first
    review pushes with `ttp push`, and so does the re-review after it fails. The mention is the code
    task's text, not the review's push step: the re-review is not review only and keeps that text."""
    from ttp import push, pushq
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    spec = ("Publish with `ttp push --own --detach` when asked.\n"
            "It delivers onto its pr_branch nowhere, not here. If it passes, publish it there with "
            "`ttp push --own --detach`.")
    cid, _, _, (rev,) = _finish_code(env, p, "plain", {"plain.py": 3},
                                     result={"status": "done", "summary": "fixed the `ttp push --own` refusal"},
                                     before=lambda tid, head: p.db.update_task(tid, spec=spec))
    assert "push it with `ttp push`" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"])
    p.db.update_task(rev["id"], status="failed")
    fid, rid, _ = Daemon(p.base)._fix_failed_review(p.db.task(rev["id"]), [{"title": "bug", "spec": "fix it"}], "bug")
    rr, fix = p.db.task(rid), p.db.task(fid)
    labels = json.loads(rr["labels"])
    assert push.REVIEW_ONLY_LABEL not in labels and not any(lb.startswith("pr_branch:") for lb in labels), labels
    assert "Review only" not in rr["spec"] and "push it with `ttp push`" in rr["spec"], rr["spec"]
    assert "fixed the `ttp push --own` refusal" in rr["spec"] and "If it passes, publish it there with `ttp push --own --detach`." in rr["spec"], rr["spec"]
    assert f"Leave the push to re-review #{rid}" in fix["spec"]


def test_the_review_of_a_task_with_another_pr_branch_publishes_with_own_and_never_approves_for_the_queue(
        env, monkeypatch):
    """A code task whose pr_branch is the push branch gets the usual push step; one whose pr_branch is
    another branch gets a review that publishes with `ttp push --own` and is review only for the push
    branch: no plain `ttp push`, no push-queue approval."""
    from ttp import push, pushq
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "origin/work")
    p.set_config("delivery.own_push", "on")   # creating its branch takes own pushes set on
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)   # no prechecks: the spec text is the subject
    for on in (False, True):
        p.set_config("delivery.push_queue", on)
        _, _, _, (same,) = _finish_code(env, p, f"same {on}", {f"s{on}.py": 3}, labels=["pr_branch:work"])
        assert "ttp push` from" in same["spec"] and "--own" not in same["spec"], same["spec"]
        assert push.REVIEW_ONLY_LABEL not in json.loads(same["labels"])
        _, _, _, (other,) = _finish_code(env, p, f"other {on}", {f"o{on}.py": 3}, labels=["pr_branch:feature/x"])
        assert "ttp push` from" not in other["spec"], other["spec"]
        assert "pr_branch feature/x" in other["spec"] and "`ttp push --own --detach`" in other["spec"]
        assert "do not approve it for the push queue" in other["spec"]
        assert other["spec"].rstrip().endswith("the pushed commit.")
        assert push.REVIEW_ONLY_LABEL in json.loads(other["labels"])
    # A project that does not push: review only, no publish step.
    p.set_config("delivery.push_allowed", False)
    _, _, _, (rev,) = _finish_code(env, p, "no push", {"n.py": 3}, labels=["pr_branch:feature/x"])
    assert "Review only" in rev["spec"] and "--own --detach" not in rev["spec"], rev["spec"]
    coordinator = (RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text()
    assert "publish it with `ttp push --own --detach`\", never\n  plain `ttp push` or a push-queue approval" \
        in coordinator


def test_the_review_prompt_keeps_the_own_publish_step_when_the_push_queue_is_on(env, monkeypatch):
    """With the push queue on, a review whose spec says to publish with `ttp push --own` still gets
    the --own section with its --detach, retry_when and exit-code steps, and the queue section's
    'NEVER run `ttp push`' names it as the exception, so the two never contradict."""
    from ttp import pushq
    from ttp.prompts import worker_task
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "origin/work")
    p.set_config("delivery.own_push", "on")   # creating its branch takes own pushes set on
    p.set_config("delivery.push_queue", True)
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    _, _, _, (rev,) = _finish_code(env, p, "other", {"o.py": 3}, labels=["pr_branch:feature/x"])
    text = worker_task(p, rev, str(p.root), None)
    assert "push queue=True" in text and "`ttp push --own --detach`" in rev["spec"]
    own = text[text.index("## Publishing onto its own PR branch (when the spec says `ttp push --own`)"):]
    own = own[:own.index("\n## ")]
    assert "whatever the sections below say, with or without the push queue" in own
    assert "run `ttp push --own --detach` in the\n  change's worktree" in own
    assert "never approve it for the push queue" in own and "`waiting` with that\n  `retry_when`" in own
    for code in ("0 pushed", "3: rebase conflict", "4: a check failed", "5: the\n  branch kept moving",
                 "75: another push", "2 or 6: refused"):
        assert code in own, code
    queue = text[text.index("## Approving into the push queue"):]
    assert "NEVER run `ttp push` or `git push`" in queue
    assert "The one\n  exception: a spec that says to publish with `ttp push --own`" in queue
    # Without the queue the section is there too, next to the plain push section.
    p.set_config("delivery.push_queue", False)
    off = worker_task(p, rev, str(p.root), None)
    assert "## Publishing onto its own PR branch" in off and "## Approving into the push queue" not in off


def test_the_review_of_a_task_whose_pr_branch_is_absent_from_the_remote_delivers_to_the_push_branch(
        env, monkeypatch):
    """With the push queue on, a pr_branch the push remote has no branch of is ignored, as the queue's
    check_approval ignores it: the review gets the usual push step and the label is named as ignored.
    A task whose spec asks for `ttp push --own` (it creates that branch) keeps the --own step only with
    delivery.own_push on, and a pr_branch the remote has keeps it too."""
    from ttp import push, pushq, settinghold
    from ttp.daemon import Daemon
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "origin/work")
    p.set_config("delivery.push_queue", True)
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: True)
    monkeypatch.setattr(Daemon, "_precheck", lambda *a: None)   # no checks: the spec text is the subject
    _, _, _, (gone,) = _finish_code(env, p, "gone", {"g.py": 3}, labels=["pr_branch:feature/x"])
    assert "ttp push` from" in gone["spec"] and "--own" not in gone["spec"], gone["spec"]
    assert "pr_branch label feature/x was ignored: origin has no such branch" in gone["spec"]
    assert push.REVIEW_ONLY_LABEL not in json.loads(gone["labels"])

    def own(tid, head):
        p.db.update_task(tid, spec="Create feature/x: publish it with `ttp push --own --detach`.")
    # delivery.own_push unset (existing_only) creates no branch, whatever the spec asks: the push queue
    # takes it. A reported case: reviews created such branches with --own in a one-branch project.
    _, _, _, (unset,) = _finish_code(env, p, "unset", {"u.py": 3}, labels=["pr_branch:feature/x"], before=own)
    assert "was ignored" in unset["spec"] and "publish it with" not in unset["spec"].split("Its hand-off")[1]
    assert "ttp push` from" in unset["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(unset["labels"])
    p.set_config("delivery.own_push", "on")
    _, _, _, (mine,) = _finish_code(env, p, "own", {"m.py": 3}, labels=["pr_branch:feature/x"], before=own)
    assert "`ttp push --own --detach`" in mine["spec"] and "was ignored" not in mine["spec"], mine["spec"]
    assert push.REVIEW_ONLY_LABEL in json.loads(mine["labels"])
    # A pr_branch the remote has: its PR's branch, --own as before.
    monkeypatch.setattr(pushq, "_absent", lambda *a: False)
    _, _, _, (there,) = _finish_code(env, p, "there", {"t.py": 3}, labels=["pr_branch:feature/x"])
    assert "`ttp push --own --detach`" in there["spec"] and "was ignored" not in there["spec"]
    # asks_own reads an --own instruction, never a negated one or a plain push.
    assert settinghold.asks_own({"spec": "- ttp push --own --detach"})
    assert settinghold.asks_own({"title": "publish it with `ttp push --own`"})
    for spec in ("Do not use `ttp push --own` here.", "push it with `ttp push --detach`", "mentions --own"):
        assert not settinghold.asks_own({"spec": spec}), spec


def test_pr_elsewhere_compares_the_push_branchs_branch_part(env):
    """push_branch `team/x` is branch team/x of origin (team is no remote): pr_branch x is another
    branch; `origin/x` and `refs/heads/x` are x."""
    from ttp import push
    p = make(env)
    _with_origin(env, clone=False)
    t = {"id": 1, "labels": json.dumps(["pr_branch:x"])}
    assert push.pr_elsewhere(p, t, {"push_branch": "team/x"}) == "x"
    for same in ("origin/x", "x", "refs/heads/x", "origin/refs/heads/x"):
        assert push.pr_elsewhere(p, t, {"push_branch": same}) == "", same
    assert push.pr_elsewhere(p, {"id": 2, "labels": json.dumps(["pr_branch:team/x"])},
                             {"push_branch": "team/x"}) == ""


def test_the_push_queue_refuses_an_approval_of_a_task_with_another_pr_branch(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    entries = [{"branch": s.branch, "head": s.head}]
    s.p.db.update_task(s.code, labels=["pr_branch:proj"])   # the push branch itself: approved as usual
    assert pushq.check_approval(s.p, s.p.db.task(s.review), entries).get("entries")
    _git_out(s.p.root, "push", "-q", "origin", "HEAD:refs/heads/feature/x")   # on the remote: its PR's branch
    s.p.db.update_task(s.code, labels=["pr_branch:feature/x"])
    check = pushq.check_approval(s.p, s.p.db.task(s.review), entries)
    assert "pr_branch feature/x" in check.get("invalid", "") and "ttp push --own" in check["invalid"], check
    # The review's run approving it anyway runs again once, told why, and nothing enters the queue.
    task = _pq_hand_off(env, s)
    assert task["status"] == "queued" and not s.p.db.q("SELECT * FROM push_queue")


def test_the_push_queue_accepts_an_approval_whose_pr_branch_label_names_a_branch_absent_from_the_remote(
        env, monkeypatch):
    """A pr_branch label inherited through `continues` from a failed review may name a branch that
    exists only locally: no PR to deliver onto, so the approval is a push-branch delivery and the
    feed says the label was ignored. A remote that cannot be reached still refuses, as before."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _git_out(s.p.root, "branch", "local-only", s.head)
    s.p.db.update_task(s.code, labels=["pr_branch:local-only"])
    entries = [{"branch": s.branch, "head": s.head}]
    check = pushq.check_approval(s.p, s.p.db.task(s.review), entries)
    assert check.get("entries") and check["target"] == "origin/proj", check
    [note] = check["notes"]
    assert f"#{s.code}'s pr_branch label local-only was ignored" in note and "no such branch" in note
    assert note in pushq.queued_text(s.p.db.task(s.review), check)
    # Unreachable remote: no answer, so refused as before.
    absent = pushq._absent
    monkeypatch.setattr(pushq, "_absent", lambda *a: False)
    assert "ttp push --own" in pushq.check_approval(s.p, s.p.db.task(s.review), entries).get("invalid", "")
    monkeypatch.setattr(pushq, "_absent", absent)
    task = _pq_hand_off(env, s)
    assert task["status"] == "pushing", task
    [row] = s.p.db.q("SELECT * FROM push_queue")
    assert row["head"] == s.head and row["target"] == "origin/proj"
    [ev] = s.p.db.q("SELECT text FROM events WHERE kind='push_queued'")
    assert "local-only was ignored" in ev["text"]


def test_a_plain_push_refuses_a_head_whose_task_has_another_pr_branch_and_own_publishes_onto_it(
        env, monkeypatch):
    """Plain `ttp push` of a task that delivers onto a pr_branch other than the push branch is refused,
    naming --own, in the task's run, its review's and its review fix's; `ttp push --own` from the
    review publishes onto that pr_branch. A pr_branch that is the push branch pushes as before."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.code_tasks_may_push", True)   # code tasks land only with it on (code_run_landing)
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/feature/x")

    def task(labels, kind="code", **more):
        tid = p.db.add_task(f"t{len(p.db.q('SELECT id FROM tasks'))}", "s", kind=kind, labels=labels, **more)
        return tid, f"ttp/t{tid}-x"

    code, branch = task(["pr_branch:feature/x"])
    _git_out(repo, "checkout", "-q", "-B", branch)
    for runner in (str(code), str(task([f"auto_review:{code}"], kind="review", depends_on=[code])[0])):
        monkeypatch.setenv("TTP_TASK", runner)
        with pytest.raises(ValueError, match=r"pr_branch feature/x \(its own label\), not origin/proj.*"
                                             r"ttp push --own --detach"):
            push.resolve(p, repo)
        assert push.resolve(p, repo, own=True) == ("origin", "feature/x", True), runner
    # The review's run in the PR branch's own worktree (the task worked on it directly).
    _git_out(repo, "checkout", "-q", "-B", "feature/x")
    with pytest.raises(ValueError, match="ttp push --own"):
        push.resolve(p, repo)
    # A fix of a failed review continues the task: its branch delivers onto the same PR.
    fix, fbranch = task([f"continues:{code}"])
    _git_out(repo, "checkout", "-q", "-B", fbranch)
    monkeypatch.setenv("TTP_TASK", str(fix))
    # It names the task whose label it inherits and how the coordinator clears it.
    with pytest.raises(ValueError, match=rf"pr_branch feature/x \(the label of #{code}, which it continues\).*"
                                         rf"task_update pr_branch \"\".*task_add with continues #{fix} and "
                                         rf"pr_branch \"\""):
        push.resolve(p, repo)
    # A branch on the remote or here is published; one in neither place never was.
    d = p.config()["delivery"]
    assert not push.never_published(repo, d, "feature/x") and not push.never_published(repo, d, fbranch)
    assert push.never_published(repo, d, "feature/never-made")
    # A remote that cannot be read cannot tell.
    _git_out(repo, "remote", "add", "dead", str(env["tmp"] / "no-such-remote.git"))
    assert not push.never_published(repo, {"push_branch": "dead/proj"}, "feature/never-made")
    _git_out(repo, "remote", "remove", "dead")
    # pr_branch is the push branch: the usual target.
    same, sbranch = task(["pr_branch:proj"])
    _git_out(repo, "checkout", "-q", "-B", sbranch)
    monkeypatch.setenv("TTP_TASK", str(same))
    _record_checks_pass(p, same, repo)   # a code task lands only tested work
    assert push.resolve(p, repo) == ("origin", "proj", False)
    monkeypatch.delenv("TTP_TASK")
    assert push.resolve(p, repo) == ("origin", "proj", False)


def test_a_notes_branch_publishes_itself_and_never_rebases_onto_the_push_branch_or_a_pr_branch(
        env, monkeypatch):
    """A task's notes branch (ttp/t<id>-notes) is pushed under its own name as it is, whether or not
    the task carries a pr_branch label: a plain push never rebases it onto the push branch (it
    conflicted there) and --own never publishes it onto the pr_branch. The task's code branch and
    the pr_branch's own worktree still deliver onto the pr_branch."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    base = _git_out(repo, "rev-parse", "HEAD")
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/feature/x")
    _commit(other, "theirs.txt", "theirs\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    proj, feature = _git_out(origin, "rev-parse", "proj"), _git_out(origin, "rev-parse", "feature/x")
    for labels in ([], ["pr_branch:feature/x"]):
        tid = p.db.add_task(f"lessons {labels}", "s", kind="code", labels=labels)
        notes = f"ttp/t{tid}-notes"
        _git_out(repo, "checkout", "-q", "-B", notes, base)
        _commit(repo, f"notes{tid}.md", "notes\n")
        monkeypatch.setenv("TTP_TASK", str(tid))
        assert push.resolve(p, repo) == ("origin", notes, True), labels
        assert push.own_target(p, repo) == ("origin", notes), labels
        head = _git_out(repo, "rev-parse", "HEAD")
        assert _ttp_push() == 0, labels
        assert _git_out(origin, "rev-parse", notes) == head, labels   # as it is: not rebased
        assert _git_out(origin, "rev-parse", "proj") == proj
        assert _git_out(origin, "rev-parse", "feature/x") == feature
    # The same task's code branch, and the pr_branch's own worktree, still go to the pr_branch.
    _git_out(repo, "checkout", "-q", "-B", f"ttp/t{tid}-code")
    assert push.own_target(p, repo) == ("origin", "feature/x")
    with pytest.raises(ValueError, match="ttp push --own"):
        push.resolve(p, repo)
    _git_out(repo, "checkout", "-q", "-B", "feature/x")
    assert push.own_target(p, repo) == ("origin", "feature/x")
    with pytest.raises(ValueError, match="ttp push --own"):
        push.resolve(p, repo)
    # Only a whole `notes` word ends a notes branch.
    assert not push.NOTES_BRANCH.fullmatch(f"ttp/t{tid}-keynotes")
    assert push.NOTES_BRANCH.fullmatch(f"ttp/t{tid}-swirl-notes")


def test_a_code_task_whose_title_ends_in_notes_still_delivers_its_own_branch_as_code(env, monkeypatch):
    """A code task titled 'Update release notes' works on ttp/t<id>-update-release-notes, which
    matches NOTES_BRANCH by name: it is still the task's own code branch, so a plain push targets
    the push branch and own_target carries it onto the task's pr_branch."""
    from ttp import push, worktree
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.code_tasks_may_push", True)   # code tasks land only with it on (code_run_landing)
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/feature/x")
    for labels in ([], ["pr_branch:feature/x"]):
        tid = p.db.add_task("Update release notes", "s", kind="code", labels=labels)
        own = f"ttp/t{tid}-{worktree.slug('Update release notes')}"
        assert push.NOTES_BRANCH.fullmatch(own)
        _git_out(repo, "checkout", "-q", "-B", own)
        _commit(repo, f"rel{tid}.md", "release\n")
        monkeypatch.setenv("TTP_TASK", str(tid))
        assert not push.is_notes_branch(p, own)
        if labels:
            assert push.own_target(p, repo) == ("origin", "feature/x")
        else:   # its spec has no landing step: `--land` says it may land
            monkeypatch.setenv(push.LAND_ENV, "1")
            assert push.resolve(p, repo) == ("origin", "proj", False)
            monkeypatch.delenv(push.LAND_ENV)
    # A branch recorded on the task row is its own code branch too; the slug then no longer is.
    p.db.x("UPDATE tasks SET branch=? WHERE id=?", (f"ttp/t{tid}-fix-notes", tid))
    assert not push.is_notes_branch(p, f"ttp/t{tid}-fix-notes")
    assert push.is_notes_branch(p, own) and push.is_notes_branch(p, f"ttp/t{tid}-notes")


def test_batch_inherited_takes_only_the_named_fd(env, tmp_path, monkeypatch):
    """batch._inherited trusts only the descriptor in TTP_BATCH_LOCK_FD: a caller's own descriptor on
    the same lock file (batch run in-process) is never picked up or closed."""
    from ttp import batch
    path = tmp_path / "push:run-b1.0.lock"
    path.write_text("")
    mine = os.open(path, os.O_RDWR)
    try:
        monkeypatch.delenv(batch.LOCK_FD_ENV, raising=False)
        assert batch._inherited(path) is None
        other = tmp_path / "other.lock"
        other.write_text("")
        passed = os.open(other, os.O_RDWR)
        monkeypatch.setenv(batch.LOCK_FD_ENV, str(passed))
        assert batch._inherited(path) is None        # named fd is on another file
        assert batch._inherited(other) == passed
        os.close(passed)
        assert batch._inherited(other) is None       # named fd no longer open
        monkeypatch.setenv(batch.LOCK_FD_ENV, str(mine))
        assert batch._inherited(path) == mine
        os.fstat(mine)                               # still open
    finally:
        os.close(mine)


def test_ttp_checks_drops_an_extra_command_the_project_checks_already_run(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    count = tmp_path / "count"
    check = f"echo x >> {count}"
    p.set_config("delivery.push_checks", [check])
    cli.main(["checks", "--fresh", "--", check, "test -f a.txt"])
    out = capsys.readouterr().out
    assert len(count.read_text().splitlines()) == 1, "the push check ran once"
    assert json.loads((run / "checks.json").read_text())["commands"] == [check, "test -f a.txt"]
    assert out.count("delivery.push_checks already runs it") == 1 and repr(check) in out
    # The same command in another form, or given twice, is still one command.
    own = push.check_list(["pytest -q tests/a.py"])
    assert cli._extra_checks(["pytest", "-q", "tests/a.py"], own) == ([], ["pytest -q tests/a.py"])
    assert cli._extra_checks(["pytest  -q 'tests/a.py'"], own) == ([], ["pytest  -q 'tests/a.py'"])
    assert cli._extra_checks(["ruff check .", "ruff  check ."], own) == (["ruff check ."], ["ruff  check ."])
    # A scoped project check may be skipped on this head: the extra that matches it stays.
    scoped = push.check_list([{"run": "pytest -q", "if_changed": "src/**"}])
    assert cli._extra_checks(["pytest", "-q"], scoped) == (["pytest -q"], [])



def _arg_logging_wrapper(path, log, rc=None):
    """A stand-in lock wrapper: logs a CALL line and each argument on its own line, then runs its
    arguments (exit `rc` instead, when given)."""
    tail = f'echo "wrapper says no"; exit {rc}' if rc is not None else 'if [ "$1" = --tag ]; then shift 2; fi\nexec "$@"'
    path.write_text(f'#!/bin/sh\necho CALL >> {shlex.quote(str(log))}\n'
                    f'for a in "$@"; do printf "%s\\n" "$a" >> {shlex.quote(str(log))}; done\n{tail}\n')
    path.chmod(0o755)
    return path


def test_ttp_checks_extra_commands_run_behind_checks_wrap_cmd_and_project_checks_do_not(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    log, out, own = tmp_path / "wrap.log", tmp_path / "out", tmp_path / "own"
    wdir = tmp_path / "lock dir"
    wdir.mkdir()
    wrapper = _arg_logging_wrapper(wdir / "lock.sh", log)
    extra = f"printf '%s|' 'a  b' c > {shlex.quote(str(out))}"
    p.set_config("delivery.push_checks", [f"echo own >> {shlex.quote(str(own))}"])
    # Unset: nothing wraps, and a check's argv is what it was.
    cli.main(["checks", "--fresh", "--", extra])
    assert out.read_text() == "a  b|c|" and not log.exists()
    assert push.with_wrap(["true"], ()) == ["true"]
    assert push.check_argv(push.with_wrap(["true"], ())[0], isolate=False) == push.check_argv("true", isolate=False)
    capsys.readouterr()
    # Set: the extra runs as the wrapper's words then the command, as separate arguments.
    p.set_config("checks.wrap_cmd", f"{shlex.quote(str(wrapper))} --tag 'x  y'")
    out.unlink()
    cli.main(["checks", "--fresh", "--", extra])
    assert "run behind checks.wrap_cmd" in capsys.readouterr().out
    lines = log.read_text().splitlines()
    assert lines.count("CALL") == 1, "only the extra command runs behind the wrapper, never the project check"
    assert lines[1:3] == ["--tag", "x  y"] and "-c" in lines and lines[-1].endswith(extra), lines
    assert not any("echo own" in x for x in lines)
    assert out.read_text() == "a  b|c|" and own.read_text().count("own") == 2
    assert json.loads((run / "checks.json").read_text())["passed"] is True
    # A check run on a check host is never wrapped with this machine's wrapper.
    remote = push.with_wrap(push.with_host(["true"], "box1"), ("w",))
    assert remote[0].wrap == () and push.with_wrap(["true"], ("w",))[0].wrap == ("w",)


def test_ttp_checks_wrap_cmd_passes_its_exit_code_and_fails_when_missing(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, project, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    log, marker = tmp_path / "wrap.log", tmp_path / "ran"
    touch = f"touch {shlex.quote(str(marker))}"
    # The wrapper's own failure and output pass through: the checks fail, the command never ran.
    p.set_config("checks.wrap_cmd", str(_arg_logging_wrapper(tmp_path / "deny.sh", log, rc=3)))
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--fresh", "--", touch])
    assert e.value.code not in (0, None) and not marker.exists() and "CALL" in log.read_text()
    assert "wrapper says no" in (run / "checks.log").read_text() + capsys.readouterr().out
    assert json.loads((run / "checks.json").read_text())["passed"] is False
    # A command's exit code passes through a wrapper that runs it.
    p.set_config("checks.wrap_cmd", str(_arg_logging_wrapper(tmp_path / "ok.sh", log)))
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--fresh", "--", "exit 7"])
    assert e.value.code not in (0, None)
    # Missing, not executable, not on PATH or unparsable: a clear failure naming the key, nothing runs unwrapped.
    plain = tmp_path / "plain.sh"
    plain.write_text("#!/bin/sh\nexec \"$@\"\n")
    for wrap in (str(tmp_path / "nope.sh"), str(plain), "no-such-wrapper-for-ttp-checks", "'unclosed"):
        p.set_config("checks.wrap_cmd", wrap)
        capsys.readouterr()
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--fresh", "--", touch])
        err = capsys.readouterr().err
        assert e.value.code == 1 and "checks.wrap_cmd" in err and not marker.exists(), (wrap, err)
    assert any("checks.wrap_cmd" in x for x in project.config_problems({"checks": {"wrap_cmd": "'unclosed"}}))
    assert any("checks.wrap_cmd" in x for x in project.config_problems({"checks": {"wrap_cmd": ["a"]}}))
    assert not [x for x in project.config_problems({"checks": {"wrap_cmd": "flock /tmp/x"}}) if "wrap" in x]
    assert push.wrap_of({}) == () and push.wrap_of({"checks": {"wrap_cmd": ""}}) == ()
    # No extra commands: a broken wrapper does not stop the project's own checks.
    p.set_config("delivery.push_checks", ["true"])
    cli.main(["checks", "--fresh"])


def test_push_own_reruns_recorded_extras_behind_checks_wrap_cmd(env, monkeypatch, capsys, tmp_path):
    log = tmp_path / "wrap.log"
    p, repo, origin, other = _push_setup(env, monkeypatch, [COND])
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.setenv("TTP_TASK", "60")
    _git_out(repo, "checkout", "-q", "-b", "ttp/t60-exp")
    _commit(repo, "mine.dat", "mine\n")
    head = _git_out(repo, "rev-parse", "HEAD")
    rec = json.dumps({"head": head, "passed": True, "commands": ["true"], "skipped": [], "ts": 0})
    (run / "checks.json").write_text(rec)
    p.set_config("checks.wrap_cmd", str(tmp_path / "nope.sh"))
    capsys.readouterr()
    assert _ttp("push", "--own") == 4
    assert "checks.wrap_cmd" in capsys.readouterr().err and _git_out(origin, "branch", "--list", "ttp/t60-exp") == ""
    p.set_config("checks.wrap_cmd", str(_arg_logging_wrapper(tmp_path / "lock.sh", log)))
    assert _ttp("push", "--own") == 0
    assert log.read_text().splitlines()[0] == "CALL" and log.read_text().splitlines()[-1].endswith("true")
    assert _git_out(origin, "rev-parse", "ttp/t60-exp") == head

def _recording_venv(path, ran):
    """A venv stand-in whose python records how it was called, with a pytest on its PATH."""
    venv = _fake_venv(path)
    (venv / "bin" / "python").write_text(f'#!/bin/sh\necho "$@" >> {ran}\n')
    (venv / "bin" / "pytest").write_text(f"#!/bin/sh\necho venv-pytest >> {ran}\n")
    for name in ("python", "pytest"):
        (venv / "bin" / name).chmod(0o755)
    return venv


def test_ttp_checks_runs_an_extra_pytest_once_and_in_the_projects_venv(env, tmp_path, monkeypatch, capsys):
    """Replays `ttp checks -- 'pytest -q --no-device-tests'` beside a project check that already runs
    it: it ran twice, the second time with the pytest on PATH, whose python lacked the project's
    packages, so it failed at import after the first run passed."""
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    ran = tmp_path / "ran"
    venv = _recording_venv(p.root / ".venv", ran)
    stub = tmp_path / "stub"   # the pytest PATH finds outside the venv: no project packages
    stub.mkdir()
    (stub / "pytest").write_text(f"#!/bin/sh\necho path-pytest >> {ran}\nexit 1\n")
    (stub / "pytest").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")
    cmd = "pytest -q --no-device-tests"
    # A scoped project check that applies on this head runs it: the extra is dropped, not run again.
    p.set_config("delivery.push_checks", [{"run": cmd, "if_exists": "a.txt"}])
    cli.main(["checks", "--fresh", "--", cmd])
    out = capsys.readouterr().out
    from ttp import envfault, pushpass
    runs = lambda: [ln for ln in ran.read_text().splitlines()   # not the venv probe, runner precheck nor env_id's
                    if ln not in ("-c pass", " ".join(pushpass.VERSION_PROBE)) and not ln.startswith("-c " + envfault._FIND)]
    assert runs() == ["-m pytest -q --no-device-tests"], "once, with the project venv's python"
    assert "a scoped delivery.push_checks entry runs it on this head" in out
    # Another spelling of the same pytest run is the same command.
    ran.unlink()
    p.set_config("delivery.push_checks", [f"{venv}/bin/python -m pytest -q --no-device-tests"])
    cli.main(["checks", "--fresh", "--", cmd])
    assert len(runs()) == 1
    assert "delivery.push_checks already runs it" in capsys.readouterr().out
    # An extra pytest no project check runs still uses the project's venv, not the pytest on PATH.
    ran.unlink()
    p.set_config("delivery.push_checks", ["true"])
    cli.main(["checks", "--fresh", "--", "pytest -q tests/x.py"])
    assert runs() == ["-m pytest -q tests/x.py"]
    # The same with the venv already active (a run's environment): VIRTUAL_ENV picks it.
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    assert push.venv_cmd("pytest -q", repo) == f"{shlex.quote(str(venv / 'bin' / 'python'))} -m pytest -q"
    assert push.venv_cmd("pytest -q", repo, {}) == "pytest -q"
    # Spellings of one pytest run; shell syntax stays as written.
    keys = {cli._same_run_key(c, "/x/.venv/bin/python") for c in (
        "pytest -q", "py.test -q", "python -m pytest -q", "python3 -m pytest -q", "/x/.venv/bin/python -m pytest -q")}
    assert keys == {"pytest -q"}
    # Only those: another explicit interpreter runs as written, so it is not the same run.
    for c in ("/usr/bin/python3 -m pytest -q", "python3.11 -m pytest -q", ".venv/bin/pytest -q",
              "/x/.venv/bin/python -m pytest -q"):
        assert cli._same_run_key(c) == c
    assert cli._extra_checks(["/usr/bin/python3 -m pytest -q"], push.check_list(["pytest -q"])) == (
        ["/usr/bin/python3 -m pytest -q"], [])
    assert cli._extra_checks(["/x/.venv/bin/python -m pytest -q"], push.check_list(["pytest -q"]),
                             "/x/.venv/bin/python") == ([], ["/x/.venv/bin/python -m pytest -q"])
    assert cli._same_run_key("pytest -q; true") == "pytest -q; true"
    assert cli._same_run_key("python3 tools/pytest.py") == "python3 tools/pytest.py"


def test_ttp_checks_warns_of_a_broken_venv_with_the_command_that_makes_it_again(env, tmp_path, monkeypatch, capsys):
    """A project-root .venv link to a venv made on another OS (its python points at an interpreter
    missing here) leaves the checks without its packages: ttp checks says so and how to rebuild it."""
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (repo / ".gitignore").write_text(".venv\nvenv/\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\n[project.optional-dependencies]\ntest = ["pytest"]\n')
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "b"], check=True)
    real = _fake_venv(repo / "venv", python=False)
    (real / "bin" / "python").symlink_to(tmp_path / "elsewhere" / "python3")
    (repo / ".venv").symlink_to("venv")
    p.set_config("delivery.push_checks", ["true"])
    cli.main(["checks", "--fresh"])
    out = capsys.readouterr().out
    assert out.count("warning: the venv") == 1, out   # .venv and venv are one venv
    assert f"python3 -m venv --clear {real} && {real}/bin/python -m pip install -e '{repo}[test]'" in out
    assert "warning: the venv" in (run / "checks.log").read_text()
    assert push.venv_cmd("pytest -q", repo) == "pytest -q"   # never the broken venv's python
    # A link that dangles, and a python that does not run; a working venv is not named.
    (repo / ".venv").unlink()
    (repo / ".venv").symlink_to(tmp_path / "gone")
    (real / "bin" / "python").unlink()
    (real / "bin" / "python").write_text("#!/bin/sh\necho bad interpreter >&2\nexit 1\n")
    (real / "bin" / "python").chmod(0o755)
    found = dict(push.broken_venvs(repo))
    assert "which is missing" in found[repo / ".venv"] and "bad interpreter" in found[repo / "venv"]
    (real / "bin" / "python").unlink()
    (real / "bin" / "python").symlink_to(sys.executable)
    assert list(push.broken_venvs(repo)) == [(repo / ".venv", found[repo / ".venv"])]
    assert push.venv_rebuild_cmd(tmp_path / "plain" / ".venv").endswith("-m pip install pytest")


def test_ttp_checks_still_reads_a_checks_record_from_before_extras_were_split(env, tmp_path, monkeypatch):
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (run / "checks.json").write_text(json.dumps({"head": head, "worktree": str(repo), "passed": True,
                                                 "commands": ["pytest -q", "test -n 'a b'"], "skipped": [],
                                                 "ts": time.time()}))
    assert push.recorded_extras(push.recorded_checks(run), head, push.check_list(["pytest -q"])) == ["test -n 'a b'"]
    cli.main(["checks", "--", "test", "-n", "a b"])
    assert json.loads((run / "checks.json").read_text())["commands"] == ["test -n 'a b'"]


@pytest.mark.parametrize("box", ["pidns", "cgroup"])
def test_detach_from_a_sandboxed_command_is_queued_and_the_daemon_starts_it(env, box):
    """A command in a PID namespace or cgroup other than its run's may take everything it started
    along when it ends: the job is queued, reads as waiting, and the daemon starts it."""
    from ttp import detach, locks, push
    if not (push.pid_ns() if box == "pidns" else push.cgroup()):
        pytest.skip("no /proc")
    p = _device_project(env, device=False)
    run_dir = p.runs / "8"
    run_dir.mkdir(parents=True)
    other = {"TTP_PIDNS": "pid:[1]"} if box == "pidns" else {"TTP_CGROUP": "0::/elsewhere"}
    r = _ttp_run(p, "detach", "job1", "--", "sh", "-c", 'echo "hi $JOB_VAR"; exit 3',
                 env={"TTP_RUN_DIR": str(run_dir), "JOB_VAR": "there", "SOME_API_TOKEN": "s3cret", **other})
    assert r.returncode == 0 and "queued" in r.stdout, r
    rc = run_dir / "job1.rc"
    queue = list((p.state / detach.QUEUE).glob("*.json"))
    assert len(queue) == 1 and "s3cret" not in queue[0].read_text(), "secrets stay off disk"
    assert json.loads((run_dir / "detached.json").read_text())[0]["pid"] is None
    check = _ttp_run(p, "detach", "--check", str(rc))
    assert check.returncode == 1 and "queued for the daemon" in check.stdout, check
    assert not locks.job_ended(rc), "an exclusive task's resources stay held while its job waits"
    detach.tend(p)
    assert not list((p.state / detach.QUEUE).glob("*.json")) and not detach.queued(rc).exists()
    check = _check_until_ended(p, rc)
    assert check.returncode == 4 and "rc=3" in check.stdout, check
    assert (run_dir / "job1.log").read_text().strip() == "hi there"


def test_detach_inside_a_pid_namespace_that_ends_with_the_command_still_runs_the_job(env, tmp_path):
    """The reported failure, reproduced: a namespace that ends with the command kills what it started,
    so a job started in it never wrote a line. Queued, the daemon runs it to its exit code."""
    from ttp import detach, push
    unshare = shutil.which("unshare")
    if not (unshare and push.pid_ns()):
        pytest.skip("no unshare or /proc")
    box = [unshare, "--user", "--pid", "--fork", "--map-root-user", "--mount-proc"]
    if subprocess.run([*box, "true"], capture_output=True).returncode:
        pytest.skip("unprivileged PID namespaces are not allowed here")
    p = _device_project(env, device=False)
    run_dir = p.runs / "8"
    run_dir.mkdir(parents=True)
    e = {**os.environ, "TTP_PROJECT": str(p.base), "TTP_RUN_DIR": str(run_dir), "TTP_PIDNS": push.pid_ns()}
    # bare: as before, no run namespace to compare. Its job outlives a slow exit of `ttp detach` under
    # load, so only the namespace's end stops it.
    for name, pidns, nap in (("bare", "", 300), ("job", push.pid_ns(), 1)):
        r = subprocess.run([*box, sys.executable, str(TTP), "detach", name, "--", "sh", "-c", f"sleep {nap}; echo done; exit 5"],
                           env={**e, "TTP_PIDNS": pidns}, cwd=str(p.root), capture_output=True, text=True, timeout=240)   # ttp startup alone took >30 s at host load 187
        assert r.returncode == 0, r
    bare = _check_until_ended(p, run_dir / "bare.rc")
    assert "gone without an exit code (it " in bare.stdout and (run_dir / "bare.lost").exists(), bare
    assert _ttp_run(p, "detach", "--check", str(run_dir / "job.rc")).returncode == 1
    detach.tend(p)
    check = _check_until_ended(p, run_dir / "job.rc")
    assert check.returncode == 4 and "rc=5" in check.stdout, check
    assert (run_dir / "job.log").read_text().strip() == "done"


def test_macos_boot_id_prefers_bootsessionuuid_and_still_knows_the_boottime_seconds(env, tmp_path, monkeypatch):
    """kern.boottime moves when the clock is stepped, so the boot id is kern.bootsessionuuid where the host
    has it. A boot recorded by an older version as kern.boottime's seconds still reads as this boot, in the
    runner and the daemon, so an upgrade is never taken for a reboot."""
    from ttp import push, runner
    from ttp.daemon import Daemon
    p = make(env)
    fake, booted, uuid = tmp_path / "fakebin", tmp_path / "booted", tmp_path / "uuid"
    fake.mkdir()
    booted.write_text("1700000000")
    _fake_sysctl(fake, booted, uuid)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(runner, "BOOT_ID_FILE", tmp_path / "noproc" / "boot_id")
    # No uuid (an older macOS): kern.boottime's seconds, as before.
    assert runner.boot_id() == "1700000000" and runner.boot_ids() == ("1700000000", "1700000000")
    uuid.write_text("6A1C0E4B-0000-4000-8000-000000000001\n")
    assert runner.boot_id() == "6A1C0E4B-0000-4000-8000-000000000001"
    assert runner.boot_ids() == ("6A1C0E4B-0000-4000-8000-000000000001", "1700000000")
    assert runner.same_boot("1700000000") and runner.same_boot("6A1C0E4B-0000-4000-8000-000000000001")
    assert not runner.same_boot("1699999999") and not runner.same_boot("") and not runner.same_boot(None)
    assert "rebooted" not in push._dead_reason({"boot": "1700000000"})
    assert "rebooted" in push._dead_reason({"boot": "1699999999"})
    # A stepped clock moves kern.boottime but not the boot id.
    booted.write_text("1700000042")
    assert runner.boot_id() == "6A1C0E4B-0000-4000-8000-000000000001"
    # The daemon after an upgrade: a run, a pause until reboot and the last told boot recorded with the
    # seconds are this boot's; a run of an earlier boot is not.
    d = Daemon(p.base)
    assert d.boots == ("6A1C0E4B-0000-4000-8000-000000000001", "1700000042")
    assert d._this_boot("1700000042") and not d._this_boot("1700000000") and not d._this_boot(None)
    p.db.set_kv("paused", True)
    p.db.set_kv("pause_until_boot", {"boot": "1700000042", "since": time.time()})
    Daemon(p.base).tick()
    assert p.db.kv("paused"), "a pause until reboot recorded with the seconds is not lifted by an upgrade"


def test_daily_review_target_line_reads_the_last_pushed_batch_reach(env, tmp_path):
    from ttp import pushq
    from ttp.db import DB
    db = DB(tmp_path / "t.db")
    assert pushq.target_line(db) is None   # nothing pushed yet

    def batch(i, reach, marker=True, started=1.0):
        m = tmp_path / f"{i}.json"
        if marker:
            m.write_text(json.dumps({"reach": reach} if reach is not None else {}))
        db.x("INSERT INTO push_batches(id,marker,target,started,ended,finalized,outcome,pushed_sha) "
             "VALUES(?,?,?,?,?,?,'pushed','ab12345')", (i, str(m), "origin/proj", started, started, started))

    batch("none", None)   # same branch: push.reach recorded nothing
    assert pushq.target_line(db) is None
    batch("gone", {"ref": "origin/main", "on": False, "behind": 2}, marker=False, started=2.0)
    assert pushq.target_line(db) is None
    batch("behind", {"ref": "origin/main", "on": False, "behind": 4}, started=3.0)
    assert pushq.target_line(db) == ("Intended target origin/main is 4 commits behind origin/proj "
                                     "(as of the last push, 1970-01-01 00:00Z)")
    batch("one", {"ref": "origin/main", "on": False, "behind": 1}, started=4.0)
    assert "is 1 commit behind" in pushq.target_line(db)
    batch("on", {"ref": "origin/main", "on": True, "behind": 0}, started=5.0)
    assert "origin/main is up to date with origin/proj" in pushq.target_line(db)
    batch("unread", {"ref": "origin/main", "on": None, "behind": None}, started=6.0)
    assert pushq.target_line(db).startswith("Intended target origin/main could not be read at the last push")
    batch("same", {"ref": "origin/proj", "on": True, "behind": 0}, started=7.0)
    assert pushq.target_line(db) is None


def test_landed_falls_back_to_the_local_branch_in_a_repo_with_no_remote(tmp_path, monkeypatch):
    """A remote-less repo: the push target's remote does not exist, so look on refs/heads/<branch>."""
    from ttp import landed, push
    repo = tmp_path / "r"
    repo.mkdir()
    _git_out(repo, "init", "-q", "-b", "main")
    _git_out(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "a")
    sha = _git_out(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(push, "target", lambda p, r: ("origin", "main"))
    assert landed._push_ref(None, repo) == "refs/heads/main"
    assert landed.check(None, repo, sha=sha)[0] == 0
    _git_out(repo, "checkout", "-q", "-b", "side")
    _git_out(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "b")
    assert landed.check(None, repo, sha=_git_out(repo, "rev-parse", "HEAD"))[0] == 1


def test_run_notes_memory_ends_sweep_spend_and_deploy_times_use_the_home_zone_on_a_utc_box(env, utc_box, monkeypatch):
    """On a UTC box, a Los Angeles project's user-facing times carry PDT/PST, never UTC or 'Z': the run
    notes people read live (ttp note, notify, push and lock lines), a temporary memory's end in the
    prompts, the blocker sweep's deferred reason (digest and daily review), the account spend window
    and the deploy-behind alert after a push."""
    from datetime import datetime, timezone as dt_timezone
    from types import SimpleNamespace
    from ttp import cli, deploy, ends, push, sweep, timefmt
    from ttp import coordinator as coord
    pac = re.compile(r"\d\d:\d\d(:\d\d)? P[DS]T")
    p = make(env)
    p.set_config("home_timezone", "America/Los_Angeles")
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=dt_timezone.utc).timestamp()
    # The formatter for live lines: the project in the environment, else UTC.
    assert timefmt.clock({"TTP_PROJECT": str(p.base)}, now=summer) == "05:00:00 PDT"
    monkeypatch.delenv("TTP_PROJECT", raising=False)
    assert timefmt.clock({}, now=summer) == "12:00:00 UTC"
    # Run notes: ttp note, ttp notify and a push's own note.
    run = env["home"] / "run-tz"
    run.mkdir(parents=True)
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    cli.cmd_note(SimpleNamespace(text="milestone", to=None))
    cli.cmd_notify(SimpleNamespace(text="please review", severity="low"))
    push.run_note("pushed")
    notes = (run / "progress.md").read_text().splitlines()
    assert len(notes) == 3 and all(re.match(r"\d\d:\d\d:\d\d P[DS]T ", ln) for ln in notes), notes
    # A temporary memory's end, as the prompts show it; the file keeps its UTC stamp.
    end = {"expires": summer}
    assert ends.describe(end) == "ends 2026-07-01T12:00Z"
    assert ends.describe(end, p) == "ends 2026-07-01 05:00 PDT"
    (p.memory_dir / "fact-tz.md").write_text("---\nkind: fact\n" + ends.front_matter(end) + "---\nA box is lent out.\n")
    [line] = [e["line"] for e in p._memory_entries() if e["name"] == "fact-tz"]
    assert line.endswith("(ends 2026-07-01 05:00 PDT)") and "Z)" not in line, line
    # The blocker sweep's deferred reason, in the digest's settings and the daily review's project.
    now = time.time()
    tid = p.db.add_task("later", "spec", kind="work", not_before=now + 3600)
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(coord.defer_labels(now + 3600, None)), tid))
    for where in (p, p.config()):   # a planned deferral (start_after ahead) is no sweep item
        assert [i for i in sweep.items(p.db, now, where) if i["task"] == tid] == []
    assert "sweep.review_lines(db, where=self.p)" in (RUNTIME / "ttp" / "daemon.py").read_text()
    # A coordinator update in a run's steer.md (which workers quote) is stamped in the home zone.
    rd = p.runs / "9999"
    rd.mkdir(parents=True)
    coord._append_update(rd / "steer.md", "use the other board", "1.0")
    assert re.search(r"^## Update \d{4}-\d\d-\d\d \d\d:\d\d P[DS]T \(turn 1\.0\)$",
                     (rd / "steer.md").read_text(), re.M), (rd / "steer.md").read_text()
    # The deploy-behind alert an after_push's verdict raises.
    sent = []
    deploy._alert(p, {"name": "demo", "target": "9.9.9", "since": summer}, "behind", "files behind", summer + 7200,
                  alert=lambda key, text, **k: sent.append(text))
    assert "behind since 2026-07-01 05:00 PDT" in sent[0] and " UTC" not in sent[0], sent
    # The account spend window: the budget's zone, never the box's.
    monkeypatch.setattr(cli, "load_account_settings", lambda: {"budget": {"timezone": "America/Los_Angeles"}})
    import ttp.globalcap as gcap
    monkeypatch.setattr(gcap, "answer", lambda *a, **k: {"host": "h", "projects": [], "rows": [], "errors": []})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_spend_today(SimpleNamespace(receive=False, since=summer, until=None, json=False))
    assert out.getvalue().rstrip().endswith("since 2026-07-01 05:00 PDT"), out.getvalue()


def test_push_queue_backoff_shows_the_home_zone_on_a_utc_box(env, monkeypatch, utc_box):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_review(s, priority=1)
    s.p.set_config("home_timezone", "America/Los_Angeles")
    now = time.time()
    s.p.db.set_kv(pushq.KV, {"backoff_until": now + 600})
    rows, why = pushq.due(s.p, now)
    assert rows == [] and re.fullmatch(r"backing off until \d\d:\d\d P[DS]T", why), why


def test_prompt_headers_give_the_home_zone_date_past_utc_midnight(env, monkeypatch):
    """A home zone behind UTC, past UTC midnight: the worker header and the coordinator's STATE line
    give today's date in the home zone (with the UTC moment) and say it wins over any other 'today'
    line, so a run never dates user-facing text with the host's UTC date."""
    import time as _time
    from ttp import coordinator as coord
    from ttp import prompts
    p = make(env)
    p.set_config("home_timezone", "America/Los_Angeles")
    tid = p.db.add_task("t", "spec", kind="work")
    at = _utc(2026, 10, 28, 1, 45)  # 2026-10-27 18:45 PDT
    with monkeypatch.context() as m:
        m.setattr(_time, "time", lambda: at)
        text = prompts.worker_task(p, p.db.task(tid), str(p.root), None)
        d = coord.digest(p, {}, [], [])
    line = next(x for x in text.split("\n") if x.startswith("home time zone:"))
    assert "now 2026-10-27 18:45 PDT (2026-10-28 01:45 UTC)" in line, line
    assert "today is 2026-10-27" in line and "over any other" in line, line
    head = d.split("\n")[0]
    assert head.startswith("# STATE at 2026-10-27 18:45 PDT (2026-10-28 01:45 UTC; "), head
    assert "today is 2026-10-27" in head and "over any other" in head, head
    # A UTC home zone gives the time once.
    p.set_config("home_timezone", "UTC")
    assert prompts._zone_line(p, at).startswith("home time zone: UTC, now 2026-10-28 01:45 UTC; today is 2026-10-28 "), \
        prompts._zone_line(p, at)


def test_digest_daily_review_and_prompt_headers_carry_the_home_zone(env):
    from ttp import coordinator as coord
    from ttp import prompts
    from ttp import pushq
    p = make(env)
    p.set_config("home_timezone", "America/Los_Angeles")
    d = coord.digest(p, {}, [], [])
    assert re.search(r"^# STATE at \d{4}-\d\d-\d\d \d\d:\d\d P[DS]T \(.*the project's home zone, America/Los_Angeles\)",
                     d, re.M), d[:200]
    tid = p.db.add_task("t", "spec", kind="work")
    text = prompts.worker_task(p, p.db.task(tid), str(p.root), None)
    assert re.search(r"home time zone: America/Los_Angeles, now \d{4}-\d\d-\d\d \d\d:\d\d P[DS]T \(.* UTC\); today is "
                     r"\d{4}-\d\d-\d\d there: write dates and times for the user in it", text)
    tpl = RUNTIME.parent / "template" / "prompts"
    assert "home zone" in (tpl / "coordinator.md").read_text() and "home zone" in (tpl / "worker.md").read_text()
    assert "(HH:MM in the home zone)" in (tpl / "coordinator.md").read_text()
    # The daily review's delivery line: the home zone, not UTC.
    m = p.root / "reach.json"
    m.write_text(json.dumps({"reach": {"ref": "origin/main", "on": False, "behind": 2}}))
    p.db.x("INSERT INTO push_batches(id,marker,target,started,ended,finalized,outcome,pushed_sha) "
           "VALUES('b',?,'origin/proj',?,?,?,'pushed','ab12345')", (str(m),) + (_utc(2026, 7, 1, 16, 0),) * 3)
    assert pushq.target_line(p.db, "America/Los_Angeles").endswith("(as of the last push, 2026-07-01 09:00 PDT)")
    assert "pushq.target_line(db, timefmt.home(self.p))" in (RUNTIME / "ttp" / "daemon.py").read_text()


def test_push_target_line_is_in_the_home_zone_on_a_utc_box(utc_box, tmp_path):
    from ttp import pushq
    from ttp.db import DB
    db = DB(tmp_path / "t.db")
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"reach": {"ref": "origin/main", "on": False, "behind": 2}}))
    db.x("INSERT INTO push_batches(id,marker,target,started,ended,finalized,outcome,pushed_sha) "
         "VALUES('b',?,'origin/proj',1.0,1.0,1.0,'pushed','ab12345')", (str(m),))
    assert pushq.target_line(db, "America/Los_Angeles").endswith("(as of the last push, 1969-12-31 16:00 PST)")


def test_move_local_rechecks_the_worktree_after_the_checks(env, monkeypatch):
    """The checks take time: a worktree that took uncommitted changes meanwhile is refused at the
    move itself, and the branch keeps its tip; once clean, the move goes through there."""
    from ttp import push
    p, repo = _local_push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "branch", "feature/x", "proj")
    side = env["tmp"] / "side"
    _git_out(repo, "worktree", "add", "-q", str(side), "feature/x")
    _git_out(repo, "checkout", "-q", "-b", "work")
    _commit(repo, "a.txt", "a\n")
    head, before = _git_out(repo, "rev-parse", "HEAD"), _git_out(repo, "rev-parse", "feature/x")
    said = []
    (side / "x.txt").write_text("staged\n")
    _git_out(side, "add", "x.txt")
    assert push._move_local(repo, "feature/x", head, said.append) == push.REJECTED
    assert "uncommitted changes" in said[-1] and _git_out(repo, "rev-parse", "feature/x") == before
    _git_out(side, "reset", "-q", "--hard")
    assert push._move_local(repo, "feature/x", head, said.append) == 0
    assert _git_out(side, "rev-parse", "HEAD") == head


def test_push_own_with_a_git_remote_still_pushes_to_it_and_never_falls_back_to_the_local_branch(
        env, monkeypatch, capsys):
    """A repo with a remote is unchanged: an unreachable remote refuses (exit 2), never moving a local
    branch instead."""
    from ttp import push
    p, repo = _local_push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "remote", "add", "origin", str(env["tmp"] / "gone.git"))
    p.set_config("delivery.own_push", "on")   # unset (existing_only) would refuse before reaching the remote
    t = p.db.add_task("Speed it up", "s", kind="code", labels=["pr_branch:feature/x"])
    monkeypatch.setenv("TTP_TASK", str(t))
    _git_out(repo, "branch", "feature/x")
    _git_out(repo, "checkout", "-q", "-b", f"ttp/t{t}-speed-it-up")
    _commit(repo, "a.txt", "a\n")
    assert push.own_target(p, repo) == ("origin", "feature/x")
    before = _git_out(repo, "rev-parse", "feature/x")
    capsys.readouterr()
    assert _ttp("push", "--own") == 2 and "cannot reach origin" in capsys.readouterr().err
    assert _git_out(repo, "rev-parse", "feature/x") == before


@pytest.mark.parametrize("mode", [None, "lease"])
def test_a_rebase_task_builds_on_its_own_branch_and_its_review_publishes_back_onto_the_old_one(env, monkeypatch, mode):
    """A task whose title says it rebases the branch its spec names is labelled rebases:<branch>, not
    pr_branch: it starts from that branch's head on its own branch. (e) Under delivery.rebase_replace
    lease its worker prompt, task_add note, review spec and coordinator rules say --own publishes back
    onto the old branch under a lease (never a plain force push, never the queue); unset (new_branch)
    none of them has any lease-replace wording, and it delivers on its own new branch."""
    from ttp import coordinator as coord, push, pushq, worktree
    from ttp.prompts import coordinator_role, worker_task
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "origin/work")
    p.set_config("delivery.own_push", "on")   # creating its branch takes own pushes set on
    if mode:
        p.set_config("delivery.rebase_replace", mode)
    lease = mode == "lease"
    role = coordinator_role(p)
    assert ("--own then replaces it under a lease" in role) == lease
    assert ("it then delivers on its own new branch" in role) != lease
    assert "`delivery.rebase_replace` `lease`" in role and "forbids force-pushing" in role
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    _git_out(p.root, "branch", "feature/x")
    assert coord.apply(p, [{"type": "task_add", "title": "Rebase feature/x onto work", "kind": "code",
                            "spec": "pr_branch: `feature/x`\nRebase it onto the latest work."}]) == []
    t = p.db.one("SELECT * FROM tasks WHERE title='Rebase feature/x onto work'")
    assert json.loads(t["labels"]) == ["rebases:feature/x"]
    notes = " ".join(p.db.kv(coord.NOTES_KEY) or [])
    assert "rebases feature/x" in notes and ("under a lease" in notes) == lease
    assert ("its review publishes that new branch" in notes) != lease
    assert worktree.rebased_branch(t) == "feature/x" and worktree.carried_branch(t) == ""
    path, branch = worktree.ensure(p, t)
    assert branch.startswith(f"ttp/t{t['id']}-")
    assert _git_out(path, "rev-parse", "HEAD") == _git_out(p.root, "rev-parse", "feature/x")
    # Where the rebase started is recorded, so --own keeps what feature/x gains after it.
    assert worktree.rebase_start(p.root, t["id"]) == _git_out(p.root, "rev-parse", "feature/x")
    prompt = worker_task(p, t, str(path), branch)
    assert "rebases branch feature/x" in prompt and ("under a lease" in prompt) == lease
    assert (f"delivers there (`ttp push --own --detach`), never onto feature/x" in prompt) != lease
    assert not re.search(r"\blease\b", prompt) or lease
    # A title without "rebase" keeps pr_branch.
    assert coord.apply(p, [{"type": "task_add", "title": "Fix feature/x CI", "kind": "code",
                            "spec": "pr_branch: `feature/x`\nFix it."}]) == []
    # It shares feature/x with the open rebase: chained to start once that one ends.
    assert json.loads(p.db.one("SELECT labels FROM tasks WHERE title='Fix feature/x CI'")["labels"]) \
        == ["pr_branch:feature/x", f"waits_end:{t['id']}"]
    # Its review: published back onto feature/x with --own under a lease, review only for the queue.
    code, _, _, (rev,) = _finish_code(env, p, "rebase it", {"r.py": 3}, labels=["rebases:feature/x"])
    own = p.db.task(code)["branch"]
    assert push.pr_elsewhere(p, p.db.task(code), {"push_branch": "origin/work"}) == ("feature/x" if lease else own)
    if lease:
        assert "It rebases feature/x and delivers back onto feature/x" in rev["spec"], rev["spec"]
    else:
        assert f"It rebases feature/x and delivers onto its own branch {own}, not feature/x" in rev["spec"], rev["spec"]
    assert ("under a lease" in rev["spec"]) == lease and bool(re.search(r"\blease\b", rev["spec"])) == lease, rev["spec"]
    assert "`ttp push --own --detach`" in rev["spec"] and "do not approve it for the push queue" in rev["spec"]


def test_own_push_off_keeps_own_out_of_daemon_specs_and_a_rebase_lands_through_the_queue(env, monkeypatch):
    """With own pushes off, a rebases: task's review, its worker prompt and review.auto_notes never
    mention --own; the review approves it for the push queue, which takes it."""
    from ttp import coordinator as coord, push, pushq
    from ttp.prompts import OWN_SECTION, worker_task
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "origin/work")
    p.set_config("delivery.own_push", "off")
    p.set_config("review.auto_notes", "Run the linter. If it passes, publish it with `ttp push --own --detach`.")
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    _git_out(p.root, "branch", "feature/x")
    assert coord.apply(p, [{"type": "task_add", "title": "Rebase feature/x onto work", "kind": "code",
                            "spec": "pr_branch: `feature/x`\nRebase it onto the latest work."}]) == []
    t = p.db.one("SELECT * FROM tasks WHERE title='Rebase feature/x onto work'")
    assert json.loads(t["labels"]) == ["rebases:feature/x"]
    notes = " ".join(p.db.kv(coord.NOTES_KEY) or [])
    assert "own pushes are off" in notes and "push --own`" not in notes.replace("never write `--own`", "")
    prompt = worker_task(p, t, str(p.root), f"ttp/t{t['id']}-rebase")
    assert "own pushes are off" in prompt and "own pushes=off" in prompt
    code, _, _, (rev,) = _finish_code(env, p, "rebase it", {"r.py": 3}, labels=["rebases:feature/x"])
    d = p.config()["delivery"]
    assert push.pr_elsewhere(p, p.db.task(code), d) == ""
    assert "--own" not in rev["spec"] and "Run the linter." in rev["spec"], rev["spec"]
    assert "own pushes are off" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"])
    assert OWN_SECTION not in worker_task(p, rev, str(p.root), None)
    # The queue takes the review's approval instead of sending it to --own; with own pushes on it would not.
    p.set_config("delivery.push_queue", True)
    approval = [{"branch": p.db.task(code)["branch"], "head": "ab" * 20}]
    assert "delivers onto its pr_branch" not in str(pushq.check_approval(p, rev, approval))
    p.set_config("delivery.own_push", "on")
    assert "delivers onto its pr_branch" in str(pushq.check_approval(p, rev, approval))


def test_own_push_existing_only_reviews_a_branch_the_remote_lacks_only_when_no_queue_takes_it(env, monkeypatch):
    """delivery.own_push existing_only with the push queue off: a pr_branch the push remote has no
    branch of cannot be created with `ttp push --own`, so its review is review only, not an own push.
    One the remote has keeps the --own step; a rebase onto its own new branch is review only too."""
    from ttp import push, pushq
    from ttp.daemon import Daemon
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "origin/work")
    p.set_config("delivery.own_push", "existing_only")
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    monkeypatch.setattr(Daemon, "_precheck", lambda *a: None)
    _, _, _, (gone,) = _finish_code(env, p, "gone", {"g.py": 3}, labels=["pr_branch:feature/x"])
    assert "--own" not in gone["spec"] and "Review only" in gone["spec"], gone["spec"]
    assert "origin has no branch feature/x" in gone["spec"] and "existing_only" in gone["spec"]
    assert "the pushed commit" not in gone["spec"] and push.REVIEW_ONLY_LABEL in json.loads(gone["labels"])
    # With the queue on, a rebase still has no queue path (it never lands on the push branch): review only.
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: True)
    _git_out(p.root, "branch", "feature/y")
    _, _, _, (reb,) = _finish_code(env, p, "rebase y", {"y.py": 3}, labels=["rebases:feature/y"])
    assert "--own" not in reb["spec"] and "Review only" in reb["spec"], reb["spec"]
    # A pr_branch the remote has: its PR's branch, --own as before.
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    monkeypatch.setattr(pushq, "_absent", lambda *a: False)
    _, _, _, (there,) = _finish_code(env, p, "there", {"t.py": 3}, labels=["pr_branch:feature/x"])
    assert "`ttp push --own --detach`" in there["spec"] and "Review only" not in there["spec"], there["spec"]


def test_after_push_uses_the_push_module_the_process_started_with(monkeypatch):
    """The regression: run_after reaches batch.after_ordinary_push through the batch module loaded at
    start. A batch.py swapped on disk mid-push (an upgrade) is not imported into the running process."""
    import ttp.batch
    from ttp import push
    assert push.batch is ttp.batch and ttp.batch.push is push
    calls = []
    monkeypatch.setattr(ttp.batch, "after_ordinary_push", lambda *a: calls.append(a) or {"status": "ok"})
    monkeypatch.delitem(sys.modules, "ttp.batch")   # as if the file on disk were new: a re-import would differ
    monkeypatch.setattr(push, "pending_after", ("a" * 40, "origin/b", "b" * 40))
    assert push.run_after(None, pathlib.Path(".")) == {"status": "ok"}
    assert calls and "ttp.batch" not in sys.modules


@pytest.mark.parametrize("rc,output", [(2, "python3: can't open file 'tests/test_absent.py': [Errno 2] No such file or directory"),
                                     (4, "ERROR: file or directory not found: tests/test_absent.py"),
                                     (5, "no tests collected in 0.01s")])
def test_missing_check_target_requires_tree_evidence_and_preserves_related_failure(env, rc, output):
    from ttp import push
    p = make(env)
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    (p.root / "README.md").write_text("updated docs\n")
    _git_out(p.root, "add", ".")
    _git_out(p.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "docs")
    check = push.Check("python3 -m pytest tests/test_absent.py")
    failure = push.target_missing(p.root, "HEAD", check, rc, output, base)
    assert failure and failure["kind"] == "target_missing" and failure["unrelated"] is True
    # Untracked files and files in another commit must not make a missing tree target present.
    (p.root / "tests").mkdir(exist_ok=True)
    (p.root / "tests/test_absent.py").write_text("def test_ok(): pass\n")
    assert push.target_missing(p.root, "HEAD", check, rc, output, base)
    _git_out(p.root, "add", ".")
    _git_out(p.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "test added")
    assert push.target_missing(p.root, "HEAD", check, rc, output, base) is None
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    _git_out(p.root, "rm", "tests/test_absent.py")
    _git_out(p.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "test deleted")
    assert push.target_missing(p.root, "HEAD", check, rc, output, base)["unrelated"] is False
    assert push.target_missing(p.root, "HEAD", check, 1, "FAILED test_assertion", base) is None


@pytest.mark.parametrize("gap_kind", ["target_missing", "selector_missing"])
def test_missing_check_gap_has_one_owner_and_waits_across_three_reviews(env, monkeypatch, gap_kind):
    from ttp import checkgaps, push
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p = make(env)
    command = "python3 -m pytest tests/test_absent.py"
    if gap_kind == "selector_missing":
        (p.root / "scripts").mkdir()
        _commit_files(p.root, {"scripts/check_selector.py": "# mandatory selector\n"}, "selector")
        command = "python3 scripts/check_selector.py"
    p.set_config("delivery.push_checks", [command, "true"])
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    reviews, consumers = [], []
    for i in range(3):
        code, branch, _, (rev,) = _finish_code(env, p, f"docs {i}", {f"docs/{i}.md": 1})
        consumer = p.db.add_task(f"consumer {i}", depends_on=[rev["id"]])
        run = env["tmp"] / f"missing-{i}"
        run.mkdir()
        path = p.worktrees / f"t{code}"
        head = _git_out(path, "rev-parse", "HEAD").strip()
        output = (f"mandatory check selector: no case for HEAD {head}" if gap_kind == "selector_missing"
                  else "ERROR: file or directory not found: tests/test_absent.py")
        failure = push.applicability_failure(path, head, push.Check(command),
                                            1 if gap_kind == "selector_missing" else 4, output, base)
        (run / "check_failure.json").write_text(json.dumps(failure))
        (run / "checks.json").write_text(json.dumps({"commands": [command, "true", "git diff --check"]}))
        (run / "result.json").write_text(json.dumps({"status": "failed", "review_failure": "check_only",
                                                   "summary": "only the check target is missing",
                                                   "on_pass": {"status": "done", "summary": "review complete; checks passed"}}))
        # A real run row permits normal model-free check settlement after the config repair.
        p.db.x("INSERT INTO runs(task,role,status,dir) VALUES(?,?,?,?)", (rev["id"], "worker", "ok", str(run)))
        d = Daemon(p.base)
        d._finish_worker({"task": rev["id"], "note": "{}"}, RunUsage(), "ok", run)
        d.reconcile_tasks()
        reviews.append(rev["id"])
        consumers.append(consumer)
    gaps = p.db.kv(checkgaps.KEY)
    assert len(gaps) == 1
    gap = next(iter(gaps.values()))
    owners = p.db.q("SELECT * FROM tasks WHERE kind='harness' AND origin='daemon'")
    assert len(owners) == 1 and gap["owner"] == owners[0]["id"]
    assert set(gap["tasks"]) == set(map(str, reviews))
    for tid in reviews:
        task = p.db.task(tid)
        prev = json.loads(task["result"])
        assert task["status"] == "queued" and prev["status"] == "waiting"
        assert "checks --gap" in prev["retry_when"] and task["attempts"] == 0
    assert all(p.db.task(t)["status"] == "queued" for t in consumers)
    started = []
    monkeypatch.setattr(d, "_start_checks", lambda path, out, name, tid, extra: started.append((tid, extra)) or True)
    d.reconcile_tasks()
    assert not started
    repaired = [command + " --updated-head-cases", "true"] if gap_kind == "selector_missing" else ["true"]
    p.set_config("delivery.push_checks", repaired)
    d._load_config()
    d.reconcile_tasks()
    assert {tid for tid, _ in started} == set(reviews)
    assert all(extra == ["--fresh", "--", "git diff --check"] for _, extra in started)
    assert all("checks --result" in json.loads(p.db.task(t)["result"])["retry_when"] for t in reviews)
    d.reconcile_tasks()
    assert len(started) == 3, "a restart or another tick must not start the same rerun again"

    # Explicit review verdicts settle only after a passing record for each clean, unchanged tree.
    for tid in reviews:
        member = gap["tasks"][str(tid)]
        out = pathlib.Path(member["run_dir"])
        (out / "checks.rc").write_text("0\n")
        (out / "checks.json").write_text(json.dumps({"passed": True, "head": member["head"],
                                                    "worktree": member["worktree"], "commands": repaired}))
        d._probe_passed(p.db.task(tid), time.time())
        assert p.db.task(tid)["status"] == "done"
    assert all(p.db.task(t)["status"] == "queued" for t in consumers)


def test_missing_check_target_infers_component_scope_without_skipping(env):
    from ttp import push
    p = make(env)
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    check = push.Check("python3 -m pytest component/tests/test_absent.py")
    output = "ERROR: file or directory not found: component/tests/test_absent.py"
    (p.root / "other").mkdir()
    (p.root / "component").mkdir()
    _commit_files(p.root, {"other/app.py": "other\n"}, "other")
    assert push.target_missing(p.root, "HEAD", check, 4, output, base)["unrelated"] is True
    _commit_files(p.root, {"component/app.py": "related\n"}, "related")
    assert push.target_missing(p.root, "HEAD", check, 4, output, base)["unrelated"] is False
    assert push.applicable(p.root, "HEAD", [check], lambda _: None, base)[0] == [check]


def test_scoped_check_cannot_skip_a_missing_target_on_related_changes(env):
    from ttp import push
    p = make(env)
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    checks = push.check_list([{"run": "python3 -m pytest tests/test_absent.py",
                              "if_exists": "tests/test_absent.py", "if_changed": ["src/**", "tests/**"]}])
    _commit_files(p.root, {"README.md": "docs\n"}, "docs")
    assert not push.applicable(p.root, "HEAD", checks, lambda _: None, base)[0]
    (p.root / "src").mkdir()
    _commit_files(p.root, {"src/app.py": "code\n"}, "code")
    with pytest.raises(push.ScopeError, match="inside the check's scope"):
        push.applicable(p.root, "HEAD", checks, lambda _: None, base)
    with pytest.raises(push.ScopeError):
        push.skip_reason(p.root, "HEAD", checks[0], base)


@pytest.mark.parametrize("finding,related", [("check_only", True), ("mixed", False), ("substantive", False)])
def test_related_or_substantive_missing_check_failure_still_fails(env, finding, related):
    from ttp import checkgaps, push
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p = make(env)
    command = "python3 -m pytest tests/test_absent.py"
    p.set_config("delivery.push_checks", [command])
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    code, _, _, (rev,) = _finish_code(env, p, "change", {"app.py" if related else "NOTES.md": 1})
    path = p.worktrees / f"t{code}"
    head = _git_out(path, "rev-parse", "HEAD").strip()
    run = env["tmp"] / "failed-check-review"
    run.mkdir()
    failure = push.target_missing(path, head, push.Check(command), 4,
                                  "ERROR: file or directory not found: tests/test_absent.py", base)
    (run / "check_failure.json").write_text(json.dumps(failure))
    (run / "result.json").write_text(json.dumps({"status": "failed", "review_failure": finding,
                                               "summary": "review failed"}))
    Daemon(p.base)._finish_worker({"task": rev["id"], "note": "{}"}, RunUsage(), "ok", run)
    assert p.db.task(rev["id"])["status"] == "failed"
    assert not p.db.kv(checkgaps.KEY, {})


@pytest.mark.parametrize("diagnostic", ["mandatory check selector: no case for HEAD {head}",
                                        "check selector: no matching case for HEAD {head}",
                                        "ERROR: no verification case for HEAD {head}"])
def test_unmatched_check_selector_requires_the_exact_tested_head(env, diagnostic):
    from ttp import push
    p = make(env)
    (p.root / "scripts").mkdir()
    _commit_files(p.root, {"scripts/select.py": "# mandatory selector\n"}, "selector")
    head = _git_out(p.root, "rev-parse", "HEAD").strip()
    check = push.Check("python3 scripts/select.py")
    output = diagnostic.format(head=head)
    failure = push.applicability_failure(p.root, head, check, 1, output)
    assert failure and failure["kind"] == "selector_missing"
    assert failure["targets"] == [head] and failure["unrelated"] is True
    assert push.applicable(p.root, head, [check, push.Check("git diff --check")], lambda _: None)[0] == [check, "git diff --check"]
    for rc, text in [(0, output), (1, diagnostic.format(head="a" * 40)),
                     (1, "selector assertion failed"), (1, "byte-equivalent tree; checks passed previously")]:
        assert push.applicability_failure(p.root, head, check, rc, text) is None


def test_applicability_retry_start_failure_remains_retryable(env, monkeypatch):
    from ttp import checkgaps, push
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config('delivery.push_checks', ['true'])
    original = checkgaps.config_sig(p.config())
    head = _git_out(p.root, 'rev-parse', 'HEAD').strip()
    tid = p.db.add_task('waiting review', kind='review')
    run = env['tmp'] / 'retry-run'
    run.mkdir()
    sig, key = 'retry-signature', 'check:retry-signature'
    result = {'status': 'waiting', 'check_gap': key, 'retry_when': checkgaps.probe(p, sig, original)}
    (run / 'result.json').write_text(json.dumps(result))
    p.db.update_task(tid, result=json.dumps(result))
    owner = p.db.add_task('repair', kind='harness', origin='daemon')
    p.db.set_kv(checkgaps.KEY, {key: {'config': original, 'check': push.Check('true').config(), 'owner': owner, 'tasks': {str(tid): {
        'run_dir': str(run), 'worktree': str(p.root), 'head': head, 'config': original, 'extras': []}}}})
    p.set_config('delivery.push_checks', ['git diff --check'])
    d = Daemon(p.base)
    monkeypatch.setattr(d, '_start_checks', lambda *args: False)
    checkgaps.rerun(d)
    assert p.db.kv(checkgaps.KEY)[key]['tasks'][str(tid)]['config'] == original
    assert json.loads(p.db.task(tid)['result']) == result
    started = []
    monkeypatch.setattr(d, '_start_checks', lambda *args: started.append(args) or True)
    checkgaps.rerun(d)
    assert len(started) == 1
    assert 'checks --result' in json.loads(p.db.task(tid)['result'])['retry_when']


def _deploy_release_with_every_pending_state(tmp_path):
    """A release whose lagging projects are each pending with a known owner: two merges held by harness
    tasks, one held while that project pushes (a harness task too), one daemon restart pending."""
    from ttp import release
    _deploy_lib('0.5.0')
    dbs = {'self': _deploy_project(tmp_path, 'self', '0.5.0')[1]}   # the reporting project, upgraded
    for name in ('alpha', 'gamma', 'delta'):
        _, dbs[name] = _deploy_project(tmp_path, name, '0.4.9')
        dbs[name].add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    _, dbs['beta'] = _deploy_project(tmp_path, 'beta', '0.5.0', running='0.4.9')   # restart pending
    return dbs


def test_after_push_with_every_lagging_project_owned_pending_exits_0_with_one_low_note(env, tmp_path, monkeypatch, capsys):
    """Replays a deploy where every project behind is pending with an owner (merges held by harness
    tasks, a restart pending): `ttp deployed --after-push` exits 0, one low note, no alert, no page."""
    from ttp import pushq
    from ttp.cli import main
    p = make(env)
    names = list(_deploy_release_with_every_pending_state(tmp_path))
    monkeypatch.setenv('TTP_PROJECT', str(p.base))
    started = time.time()
    with pytest.raises(SystemExit) as e:
        main(['deployed', '0.5.0', '--commit', DEPLOY_C, '--projects', ','.join(names), '--after-push', '--json'])
    data = json.loads(capsys.readouterr().out)
    assert e.value.code == 0
    assert {r['name']: r['outcome'] for r in data['projects']} == {
        'self': 'done', 'alpha': 'pending', 'beta': 'pending', 'gamma': 'pending', 'delta': 'pending'}
    out = p.db.q("SELECT kind,severity,text FROM messages WHERE direction='out'")
    assert [(m['kind'], m['severity']) for m in out] == [('info', 'low')]
    assert all(n in out[0]['text'] for n in ('alpha', 'beta', 'gamma', 'delta'))
    assert not p.db.q('SELECT * FROM alerts')
    p.db.x("INSERT INTO push_batches(id,marker,target,started) VALUES(?,?,?,?)",
           ('batch-test', 'marker', 'origin/topic', started))
    pages = []
    pushq._after(p, {'id': 'batch-test'}, {'pushed_sha': DEPLOY_C, 'version': '0.5.0', 'after_push': {
        'exit': e.value.code, 'started': started, 'ended': time.time()}}, 'ok', lambda *a, **k: pages.append(a), time.time())
    assert not pages


@pytest.mark.parametrize('down', [['beta'], ['alpha', 'beta']])
def test_after_push_deploy_real_failure_sends_exactly_one_alert(env, tmp_path, monkeypatch, capsys, down):
    """A daemon that is down still fails the deploy: one alert per project behind (high), and the push
    queue sends no after_push alert of its own."""
    from ttp import deploy, pushq
    from ttp.cli import main
    p = make(env)
    dbs = _deploy_release_with_every_pending_state(tmp_path)
    names = list(dbs)
    for name in down:
        dbs[name].set_kv('daemon', {'pid': 0, 'host': 'testhost'})
    monkeypatch.setenv('TTP_PROJECT', str(p.base))
    started = time.time() - 60
    with pytest.raises(SystemExit) as e:
        main(['deployed', '0.5.0', '--commit', DEPLOY_C, '--projects', ','.join(names), '--after-push', '--json'])
    capsys.readouterr()
    assert e.value.code == 1
    alerts_out = p.db.q("SELECT severity,ref,text FROM messages WHERE kind='alert' ORDER BY id")
    assert [(a['severity'], a['ref']) for a in alerts_out] == [('high', f'deploy-behind:{n}') for n in down]
    assert all(n in a['text'] for n, a in zip(down, alerts_out))
    assert [a['key'] for a in p.db.q('SELECT key FROM alerts ORDER BY id')] == [f'deploy-behind:{n}' for n in down]
    p.db.x("INSERT INTO push_batches(id,marker,target,started) VALUES(?,?,?,?)",
           ('batch-test', 'marker', 'origin/topic', started))
    pages = []
    pushq._after(p, {'id': 'batch-test'}, {'pushed_sha': DEPLOY_C, 'version': '0.5.0', 'after_push': {
        'exit': 1, 'started': started, 'ended': time.time()}}, 'failed', lambda *a, **k: pages.append(a), time.time())
    assert not pages, 'one deploy outcome, one alert'
    assert p.db.one("SELECT id FROM events WHERE kind='after_push_failed'"), 'the coordinator still hears of it'
    pages = []
    deploy.tick(p, lambda *a, **k: pages.append(a), time.time() + deploy.PROBE_S)
    assert not pages, 'the probe after it does not repeat the alert'
    # An after_push failing for another reason (no deploy alert for that commit) still pages.
    p.db.x("INSERT INTO push_batches(id,marker,target,started) VALUES(?,?,?,?)",
           ('batch-other', 'marker', 'origin/topic', started))
    pushq._after(p, {'id': 'batch-other'}, {'pushed_sha': '1234567890abcdef', 'after_push': {
        'exit': 1, 'started': started, 'ended': time.time()}}, 'failed', lambda *a, **k: pages.append(a), time.time())
    assert [a[0] for a in pages] == ['after_push_failed']


def test_deploy_alerted_key_is_shared_by_deploy_and_the_push_queue():
    from ttp import deploy, pushq
    assert pushq.DEPLOY_ALERTED == deploy.ALERTED


def test_after_push_deploy_pending_success_does_not_page_push_queue(env, tmp_path):
    from ttp import deploy, overview, pushq, release
    p = make(env)
    _deploy_lib('0.5.0')
    _, db = _deploy_project(tmp_path, 'alpha', '0.4.9')
    db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    rc = deploy.record(p, overview.deployed(names=['alpha']))['rc']
    now = time.time()
    p.db.x("INSERT INTO push_batches(id,marker,target,started) VALUES(?,?,?,?)",
           ('batch-test', 'marker', 'origin/topic', now))
    pages = []
    pushq._after(p, {'id': 'batch-test'}, {'pushed_sha': DEPLOY_C, 'version': '0.5.0',
                 'after_push': {'exit': rc, 'started': now, 'ended': now}},
                 'ok' if rc == 0 else 'failed', lambda *a, **k: pages.append(a), now)
    assert not pages and not p.db.one("SELECT id FROM events WHERE kind='after_push_failed'")
    assert p.db.one("SELECT after_push FROM push_batches")['after_push'] == 'ok'


def test_a_project_stuck_across_releases_is_one_alert_tracking_the_newest(env, tmp_path):
    """Replays alerts #469-#476: a project stuck behind release after release got one alert per release.
    It is one ongoing alert per project, updated in place with the newest release it misses, that clears
    once its own daemon reports the release; two stuck projects are two alerts; the after_push failure of
    each deploy sends none of its own."""
    from ttp import alerts, deploy, overview, pushq
    p = make(env)
    p.set_config('home_timezone', 'America/Los_Angeles')
    _deploy_lib('0.4.9')
    a_base, a_db = _deploy_project(tmp_path, 'alpha', '0.4.8')
    b_base, _ = _deploy_project(tmp_path, 'beta', '0.4.8')
    for base in (a_base, b_base):   # stuck: upgrade.auto off, so no daemon of theirs owns the merge
        cfg = json.loads((base / 'harness' / 'project.json').read_text())
        (base / 'harness' / 'project.json').write_text(json.dumps(dict(cfg, upgrade={'auto': False})))
    pages, page = _deploy_pager(p)
    now = time.time()
    versions = [f'0.5.{i}' for i in range(6)]
    for i, v in enumerate(versions):
        t = now + 10 * i
        _deploy_lib(v)
        started = t - 5
        assert deploy.record(p, overview.deployed(names=['alpha', 'beta']), t)['rc'] == 1
        p.db.x("INSERT INTO push_batches(id,marker,target,started) VALUES(?,?,?,?)",
               (f'batch-{v}', 'marker', 'origin/topic', started))
        pushq._after(p, {'id': f'batch-{v}'}, {'pushed_sha': DEPLOY_C, 'version': v, 'after_push': {
            'exit': 1, 'started': started, 'ended': t}}, 'failed', lambda *a, **k: pages.append(a), t)
        deploy.tick(p, page, t + 1)
        deploy.tick(p, page, t + 1 + deploy.PROBE_S)
        out = p.db.q("SELECT ref,text FROM messages WHERE kind='alert' ORDER BY id")
        assert [m['ref'] for m in out] == ['deploy-behind:alpha', 'deploy-behind:beta'], (v, out)
        assert all(f'tt-project {v} ' in m['text'] for m in out), out
        assert all(f'tt-project {old} ' not in m['text'] for m in out for old in versions[:i])
        live = p.db.q("SELECT key,text FROM alerts WHERE cleared IS NULL ORDER BY id")
        assert [a['key'] for a in live] == ['deploy-behind:alpha', 'deploy-behind:beta']
        assert all(f'tt-project {v} ' in a['text'] and 'behind since' in a['text'] for a in live)
        open_checks = [r for r in p.db.kv(deploy.CHECKS).values() if not r.get('closed')]
        assert sorted((r['name'], r['target']) for r in open_checks) == [('alpha', v), ('beta', v)]
        assert {r['since'] for r in open_checks} == {now}, 'behind since its first missed release'
    assert not pages, 'no new page per release, and no after_push alert of its own'
    t = now + 10 * len(versions)
    _deploy_catch_up(a_base, a_db, versions[-1], DEPLOY_C, t)
    assert not alerts.holds(p.db, 'deploy-behind:alpha', now, t)
    assert alerts.holds(p.db, 'deploy-behind:beta', now, t)
    assert [e['key'] for e in alerts.sweep(p.db, now=t)] == ['deploy-behind:alpha']
    deploy.tick(p, page, t)
    assert not pages and [a['key'] for a in p.db.q("SELECT key FROM alerts WHERE cleared IS NULL")] == \
        ['deploy-behind:beta']


@pytest.mark.parametrize("queued", [False, pytest.param(True, marks=pytest.mark.skipif(
    sys.platform != "linux", reason="a launcher finds itself boxed only by its Linux PID namespace or cgroup "
    "(/proc); other hosts are never boxed and take the direct path"))], ids=["direct", "daemon-queued"])
def test_detached_checks_temp_survives_worker_cleanup(env, tmp_path, monkeypatch, queued, request,
                                                    reap_tmp_processes):
    from ttp import cli, detach, push, runner

    def reap():
        # The checks and their command (a session of its own) block on `release` until the test
        # writes it: however the test ends, nothing they started may outlive it.
        assert reap_tmp_processes(tmp_path) == [], "the detached checks outlived the test"
    request.addfinalizer(reap)
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    worker_tmp = run / "tmp"
    worker_tmp.mkdir()
    (run / "run.json").write_text(json.dumps({"tmp_dir": str(worker_tmp)}))
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(worker_tmp))
    ready, release = tmp_path / "ready", tmp_path / "release"
    os.mkfifo(release)
    code = ("import os, pathlib, tempfile; "
            "f = pathlib.Path(tempfile.mkdtemp()) / 'keep'; f.write_text('alive'); "
            f"pathlib.Path({str(ready)!r}).write_text(str(f)); "
            f"open({str(release)!r}).read(); "
            "assert f.read_text() == 'alive'; "
            "assert all(os.environ[k] == tempfile.gettempdir() for k in ('TMPDIR', 'TMP', 'TEMP'))")
    # Real marker mismatch, not a mock of _boxed: the disposable command queues, exits,
    # and leaves no child with a sandbox-local pid for the daemon to misidentify.
    launch = subprocess.run([sys.executable, "-m", "ttp", "checks", "--detach", "--",
                             shlex.join([sys.executable, "-c", code])], cwd=repo,
                            env={**os.environ, "PYTHONPATH": str(RUNTIME),
                                 **({"TTP_PIDNS": "pid:[disposable-command]"} if queued else {})},
                            capture_output=True, text=True, timeout=30)
    assert launch.returncode == 0, launch.stderr
    if queued:
        assert "queued" in launch.stdout and not (run / cli.CHECKS_PID).exists()
        detach.tend(p)
    info = cli._read_checks_pid(run)
    assert _wait_for(ready.exists), (run / cli.CHECKS_OUT).read_text()
    scratch = pathlib.Path(ready.read_text()).parent.parent
    runner.remove_private(run)
    assert not worker_tmp.exists()
    assert pathlib.Path(ready.read_text()).exists(), "worker cleanup deleted live checks temp files"
    monkeypatch.setattr(p, "runs", tmp_path)
    detach.tend(p)   # recovery must leave live checks' storage alone
    assert pathlib.Path(ready.read_text()).exists()
    release.write_text("go")
    assert _wait_precheck(run) == "0"
    assert _wait_for(lambda: not scratch.exists()), "completed checks left scratch behind"
    assert cli._checks_result(run) == 0
    assert info["pidns"] == push.pid_ns()


def test_leak_guard_finds_and_reaps_processes_naming_the_temp_root(tmp_path, leaked_tmp_processes,
                                                                    reap_tmp_processes):
    """The session guard (tests/conftest.py) reports a process left running that names the temp root,
    whether detached or in pytest's own process group, never one naming a sibling path, and stops
    them without touching pytest."""
    sleep = "import sys, time; time.sleep(120)"
    detached = subprocess.Popen([sys.executable, "-c", sleep, str(tmp_path / "a")], start_new_session=True)
    same_group = subprocess.Popen([sys.executable, "-c", sleep, str(tmp_path)])
    sibling = subprocess.Popen([sys.executable, "-c", sleep, f"{tmp_path}1/b"], start_new_session=True)
    try:
        want = {detached.pid, same_group.pid}
        assert _wait_for(lambda: {pid for pid, _, _ in leaked_tmp_processes(tmp_path)} == want), \
            leaked_tmp_processes(tmp_path)
        assert {pid: pgid for pid, pgid, _ in leaked_tmp_processes(tmp_path)}[detached.pid] == detached.pid
        assert reap_tmp_processes(tmp_path) == []
        assert detached.wait(timeout=10) == -signal.SIGTERM and same_group.wait(timeout=10) == -signal.SIGTERM
        assert sibling.poll() is None, "a process naming a sibling path was stopped"
        assert [pid for pid, _, _ in leaked_tmp_processes(f"{tmp_path}1")] == [sibling.pid]
    finally:
        for proc in (detached, same_group, sibling):
            proc.kill()
            proc.wait()


def test_sandboxed_detached_checks_without_daemon_refused_before_launch(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv("TTP_PROJECT")
    monkeypatch.setattr(push, "pid_ns", lambda: "pid:[host]")   # a PID namespace on every OS (macOS has none)
    monkeypatch.setenv("TTP_PIDNS", "pid:[disposable-command]")
    with pytest.raises(SystemExit) as refused:
        cli.main(["checks", "--detach", "--", "true"])
    assert refused.value.code == 2
    assert "need a project daemon" in capsys.readouterr().err
    assert not (run / cli.CHECKS_PID).exists() and not (run / cli.CHECKS_TMP).exists()


def test_detached_checks_start_directly_where_there_is_no_pid_namespace(env, tmp_path, monkeypatch, capsys):
    """macOS has no /proc: no PID namespace or cgroup to tell a sandbox by, so a launcher is never
    boxed, even with a run's markers set, and starts its checks itself instead of queueing them."""
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "pid_ns", lambda: None)
    monkeypatch.setattr(push, "cgroup", lambda: None)
    monkeypatch.setenv("TTP_PIDNS", "pid:[run]")
    monkeypatch.setenv("TTP_CGROUP", "0::/run")
    assert push._boxed() is False
    _detached_checks(run, capsys, "true")
    assert not list((p.state / detach.QUEUE).glob("*.json")), "queued for a daemon instead of started"
    assert _wait_precheck(run) == "0" and cli._read_checks_pid(run)["pidns"] is None
    assert cli._checks_result(run) == 0


def _prestage_setup(env, tmp_path, monkeypatch, prestage):
    """A checks repo whose project sets delivery.check_prestage; the detached launch is recorded, not run."""
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    if prestage is not None:
        p.set_config("delivery.check_prestage", prestage)
    started = []
    monkeypatch.setattr(cli, "_checks_detach", lambda a, run_dir, p, top="", head="", **kw: started.append(head))
    return p, run, started


def test_a_passing_check_prestage_runs_in_the_foreground_then_starts_the_full_checks(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    mark = tmp_path / "prestage-ran"
    p, run, started = _prestage_setup(env, tmp_path, monkeypatch, f"pwd > {mark}")
    cli.main(["checks", "--detach", "--", "true"])
    out = capsys.readouterr().out
    assert mark.read_text().strip() == str(tmp_path / "work"), "the pre-stage runs at the worktree's top"
    assert len(started) == 1 and "pre-stage passed" in out


def test_a_failing_check_prestage_starts_nothing_exits_non_zero_and_shows_its_tail(env, tmp_path, monkeypatch, capsys):
    from ttp import checkprestage, cli
    cmd = "for i in $(seq 1 100); do echo line-$i; done; echo boom-assert >&2; exit 3"
    p, run, started = _prestage_setup(env, tmp_path, monkeypatch, cmd)
    with pytest.raises(SystemExit) as failed:
        cli.main(["checks", "--detach", "--", "true"])
    out = capsys.readouterr().out
    assert failed.value.code == 1 and not started, "a failed pre-stage starts no background checks"
    assert "NOT started" in out and "boom-assert" in out and "line-100" in out
    assert "line-1\n" not in out, f"only the last {checkprestage.TAIL_LINES} lines are shown"
    assert not (run / cli.CHECKS_PID).exists() and not (run / cli.CHECKS_RC).exists()
    # The escape hatch for a broken pre-stage: the full checks start without it.
    cli.main(["checks", "--detach", "--no-prestage", "--", "true"])
    assert len(started) == 1 and "pre-stage" not in capsys.readouterr().out


@pytest.mark.parametrize("cmd", ["no-such-prestage-command-xyz", 17, {"run": "true", "timeout_s": -1}],
                         ids=["cannot-run", "malformed", "bad-timeout"])
def test_a_check_prestage_that_cannot_run_never_holds_the_full_checks_back(env, tmp_path, monkeypatch, capsys, cmd):
    from ttp import cli, project
    p, run, started = _prestage_setup(env, tmp_path, monkeypatch, cmd)
    cli.main(["checks", "--detach", "--", "true"])
    out = capsys.readouterr().out
    assert len(started) == 1 and "starting the full checks without it" in out
    assert any("delivery.check_prestage" in x for x in project.config_problems({"delivery": {"check_prestage": cmd}})) \
        == (cmd != "no-such-prestage-command-xyz")


def test_a_check_prestage_that_times_out_is_stopped_and_the_full_checks_start_anyway(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    from ttp.daemon import _alive
    pidf = tmp_path / "prestage.pid"
    p, run, started = _prestage_setup(env, tmp_path, monkeypatch,
                                      {"run": f"echo $$ > {pidf}; exec sleep 60", "timeout_s": 1})
    t0 = time.monotonic()
    cli.main(["checks", "--detach", "--", "true"])
    out = capsys.readouterr().out
    assert time.monotonic() - t0 < 30 and len(started) == 1 and "timed out after 1 s" in out
    assert not _alive(int(pidf.read_text())), "a timed-out pre-stage is stopped, not left running"


def test_without_a_check_prestage_detached_checks_start_as_before(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, project
    count = tmp_path / "count"
    p, run, started = _prestage_setup(env, tmp_path, monkeypatch, f"echo x >> {count}")
    cli.main(["checks", "--detach", "--", "true"])
    assert count.read_text() == "x\n" and "pre-stage" in capsys.readouterr().out
    for unset in ("", None):
        p.set_config("delivery.check_prestage", unset)
        cli.main(["checks", "--detach", "--", "true"])
        assert "pre-stage" not in capsys.readouterr().out
    assert count.read_text() == "x\n" and len(started) == 3
    # The foreground checks never run it: only a detached start does.
    p.set_config("delivery.check_prestage", f"echo x >> {count}")
    cli.main(["checks", "--", "true"])
    assert count.read_text() == "x\n"
    assert not project.config_problems({"delivery": {"check_prestage": {"run": "pytest -q t.py", "timeout_s": 120}}})


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP], ids=["term", "hup"])
def test_a_check_prestage_is_stopped_when_ttp_checks_is_killed(tmp_path, sig):
    import ttp
    from ttp.daemon import _alive
    pidf = tmp_path / "prestage.pid"
    code = ("from ttp import checkprestage; import os, sys; "
            f"checkprestage.run({{'run': 'echo $$ > {pidf}; exec sleep 60', 'timeout_s': 30}}, "
            f"{str(tmp_path)!r}, dict(os.environ), print); sys.exit(9)")
    # Nested in a run holding test slots: the pre-stage never waits on this host's real slot pool.
    env = {**os.environ, "PYTHONPATH": str(pathlib.Path(ttp.__file__).parent.parent), "TTP_TEST_SLOTS_HELD": "1"}
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while not (pidf.exists() and pidf.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.05)
        child = int(pidf.read_text())
        assert _alive(child)
        proc.send_signal(sig)
        assert proc.wait(timeout=15) == -sig, "the signal still ends `ttp checks` as it would have"
        deadline = time.monotonic() + 5
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child), "the pre-stage outlived the `ttp checks` that started it"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if pidf.exists() and pidf.read_text().strip() and _alive(int(pidf.read_text())):
            os.kill(int(pidf.read_text()), signal.SIGKILL)


def test_a_check_prestage_child_that_ignores_sigterm_dies_with_ttp_checks(tmp_path):
    """A SIGTERM to `ttp checks` kills the pre-stage's whole group: a process in it that ignores
    SIGTERM and outlives the shell that started it is not left behind."""
    import ttp
    from ttp.daemon import _alive
    pidf = tmp_path / "prestage.pid"
    stay = "sh -c 'trap \"\" TERM; exec sleep 60' & echo $! > " + str(pidf) + "; wait"
    code = ("from ttp import checkprestage; import os, sys; "
            f"checkprestage.run({{'run': {stay!r}, 'timeout_s': 30}}, {str(tmp_path)!r}, dict(os.environ), print); "
            "sys.exit(9)")
    # Nested in a run holding test slots: the pre-stage never waits on this host's real slot pool.
    env = {**os.environ, "PYTHONPATH": str(pathlib.Path(ttp.__file__).parent.parent), "TTP_TEST_SLOTS_HELD": "1"}
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.DEVNULL)
    child = None
    try:
        deadline = time.monotonic() + 20
        while not (pidf.exists() and pidf.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.05)
        child = int(pidf.read_text())
        time.sleep(0.2)   # the inner shell has set its trap and exec'd sleep
        assert _alive(child)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == -signal.SIGTERM
        deadline = time.monotonic() + 5
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child), "a pre-stage process that ignores SIGTERM outlived `ttp checks`"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if child and _alive(child):
            os.kill(child, signal.SIGKILL)


def test_a_check_prestage_is_skipped_on_a_tree_whose_checks_already_passed(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    p, run, started = _prestage_setup(env, tmp_path, monkeypatch, "echo flaky; exit 3")
    cli.main(["checks", "--", "true"])   # records a pass of these checks on this tree
    capsys.readouterr()
    cli.main(["checks", "--detach", "--", "true"])
    out = capsys.readouterr().out
    assert len(started) == 1 and "skipped: these checks already passed" in out and "flaky" not in out
    with pytest.raises(SystemExit) as failed:   # --fresh runs them all again, the pre-stage first
        cli.main(["checks", "--detach", "--fresh", "--", "true"])
    assert failed.value.code == 1 and len(started) == 1 and "flaky" in capsys.readouterr().out
    # Other commands than the recorded ones: the pre-stage runs.
    with pytest.raises(SystemExit):
        cli.main(["checks", "--detach", "--", "true", "--version"])
    assert len(started) == 1


def test_a_check_prestage_is_skipped_on_a_fast_path_diff(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    p, run, started = _prestage_setup(env, tmp_path, monkeypatch, "echo flaky; exit 3")
    repo = tmp_path / "work"
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    p.set_config("delivery.push_branch", "work")
    p.set_config("delivery.checks_fast_globs", ["*.md"])
    p.set_config("delivery.push_checks", ["true"])
    base = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    subprocess.run([*git, "update-ref", "refs/remotes/origin/work", base], check=True)
    (repo / "n.md").write_text("hi\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "docs"], check=True)
    cli.main(["checks", "--detach"])
    out = capsys.readouterr().out
    assert len(started) == 1 and "skipped: the fast path" in out and "flaky" not in out
    with pytest.raises(SystemExit) as failed:
        cli.main(["checks", "--detach", "--full"])
    assert failed.value.code == 1 and len(started) == 1 and "flaky" in capsys.readouterr().out


@pytest.mark.parametrize('run_env', [None, ''], ids=['unset', 'empty'])
def test_check_output_queued_push_rebuilt_after_restart(env, monkeypatch, capsys, run_env):
    from ttp import push
    from ttp.project import Project
    command = _check_output_paths_command()
    p, repo, origin, other = _push_setup(env, monkeypatch, [command])
    _commit(repo, 'mine.txt', 'mine\n')
    monkeypatch.setattr(push, '_boxed', lambda: True)
    pending = []
    for _ in range(2):
        assert _ttp('push', '--detach') == 0
        out = capsys.readouterr().out
        marker = pathlib.Path(next(ln.split(': ', 1)[1] for ln in out.splitlines() if ln.startswith('marker: ')))
        m = json.loads(marker.read_text())
        assert m.get('check_run_dir'), 'queue must persist the check output directory'
        output = pathlib.Path(m['check_run_dir'])
        pending.append((marker, output))
        shutil.rmtree(output)
    assert pending[0][1] != pending[1][1], 'queued invocations must not collide'
    if run_env is None:
        monkeypatch.delenv('TTP_RUN_DIR', raising=False)
    else:
        monkeypatch.setenv('TTP_RUN_DIR', run_env)
    monkeypatch.setenv('TTP_PIDNS', 'must not inherit')
    monkeypatch.setenv('TTP_CGROUP', 'must not inherit')
    push.tend(Project(p.base))  # restart reads only persisted queue data, without the worker's env
    for marker, output in pending:
        result = _probe_until_done(p, push.result_probe(marker, p))
        assert 'pushed' in result.stdout, result.stdout
        # Rebuilt storage has no owner record. The narrowed cleanup contract leaves it alone.
        assert _wait_for(lambda: json.loads(marker.read_text()).get('ended'))
        assert output.exists() and not (output / 'owner.json').exists()
        reports = pathlib.Path(json.loads(marker.read_text())['check_report_dir'])
        assert reports.parent == p.state / 'check-reports'
        assert (reports / 'pre-commit-cache').is_dir()
        paths = json.loads((reports / 'result.json').read_text())
        assert paths == {'output': str(reports), 'scratch': str(output / 'checks-tmp')}, \
            'a rebuilt allocation has no owner.json naming a short dir: it keeps checks-tmp'


@pytest.mark.parametrize('run_env', [None, ''], ids=['unset', 'empty'])
def test_check_output_queued_checks_after_restart_without_run_env(env, tmp_path, monkeypatch, capsys, run_env):
    from ttp import cli, detach, push, shorttmp
    from ttp.daemon import Daemon
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, 'pid_ns', lambda: 'pid:[host]')   # a PID namespace on every OS (macOS has none)
    monkeypatch.setenv('TTP_PIDNS', 'disposable-command')
    cli.main(['checks', '--detach', '--', _check_output_paths_command()])
    assert 'queued' in capsys.readouterr().out
    assert not (run / cli.CHECKS_PID).exists()
    q = next((p.state / detach.QUEUE).glob('*.json'))
    record = json.loads(q.read_text())
    if run_env is None:
        monkeypatch.delenv('TTP_RUN_DIR')
        record['env'].pop('TTP_RUN_DIR', None)
    else:
        monkeypatch.setenv('TTP_RUN_DIR', run_env)
        record['env']['TTP_RUN_DIR'] = run_env
    q.write_text(json.dumps(record))
    monkeypatch.delenv('TTP_PIDNS')
    Daemon(p.base).tend_pushes()  # a fresh daemon has only the persisted request
    assert _wait_precheck(run) == '0', (run / cli.CHECKS_OUT).read_text()
    info = cli._read_checks_pid(run)
    reports = pathlib.Path(info['check_report_dir'])
    assert reports.is_relative_to(p.state) and not reports.is_relative_to(repo)
    assert (reports / 'pre-commit-cache').is_dir()
    paths = json.loads((reports / 'result.json').read_text())
    assert paths['output'] == str(reports) and shorttmp.owned(paths['scratch']), paths
    assert _wait_for(lambda: not pathlib.Path(paths['scratch']).exists()), 'removed with its allocation'
    assert not q.exists()


@pytest.mark.parametrize('value', [None, '', '/', '//', 'relative'])
def test_check_output_missing_or_invalid_fails_closed(env, monkeypatch, value):
    from ttp import push
    if value is None:
        monkeypatch.delenv('TTP_RUN_DIR', raising=False)
    else:
        monkeypatch.setenv('TTP_RUN_DIR', value)
    with pytest.raises((OSError, ValueError), match='TTP_RUN_DIR'):
        push.check_env('target')


def _derived_setup(env, tmp_path, monkeypatch, prestage=None):
    """A checks repo whose project's own checks run pytest and whose tests/test_seed.py has a static
    failure; delivery.check_prestage is `prestage` (None: unset); the detached launch is recorded."""
    p, run, started = _prestage_setup(env, tmp_path, monkeypatch, prestage)
    for var in ("PYTEST_ADDOPTS", "TTP_PYTEST_RESUME", "TTP_PYTEST_DURATIONS", "TTP_PYTEST_SELECT"):
        monkeypatch.delenv(var, raising=False)   # the outer suite's plugins never reach the pre-stage
    repo = tmp_path / "work"
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    (repo / "tests").mkdir()
    (repo / "tests" / "test_seed.py").write_text(
        "def test_static_guard():\n    assert 0, 'seeded-static-failure'\n\n\n"
        "def test_fine():\n    pass\n\n\ndef test_slow_one():\n    assert 0, 'slow-test-ran'\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "tests"], check=True)
    p.set_config("delivery.push_checks", ["python3 -m pytest -q tests"])
    return p, run, started


def _seed_check_failure(p, rid, ids, detached=True, age_s=0.0, durations=None):
    """Run `rid`'s directory as a `ttp checks` run left it: its failure naming `ids` and, once it
    detached, the pid file; `durations` its slowest tests ({id: seconds})."""
    from ttp import cli, checkprestage, slowtests
    d = p.runs / str(rid)
    d.mkdir(parents=True, exist_ok=True)
    out = "".join(f"FAILED {i} - AssertionError\n" for i in ids) + f"{len(ids)} failed in 0.5s\n"
    f = d / checkprestage.FAILURE
    f.write_text(json.dumps({"kind": "check_failed", "purpose": "candidate", "output": out}))
    if detached:
        (d / cli.CHECKS_PID).write_text("{}")
    if durations:
        (d / slowtests.FILE).write_text(json.dumps({"tests": [{"id": i, "s": s} for i, s in durations.items()]}))
    if age_s:
        t = time.time() - age_s
        os.utime(f, (t, t))


SEED = "tests/test_seed.py::test_static_guard"


def test_a_derived_check_prestage_catches_a_frequent_failure_before_detaching(env, tmp_path, monkeypatch, capsys):
    from ttp import audit, checkprestage, cli
    p, run, started = _derived_setup(env, tmp_path, monkeypatch)
    for rid in (1, 2):
        _seed_check_failure(p, rid, [SEED])
    _seed_check_failure(p, 3, ["tests/test_seed.py::test_fine"])
    assert checkprestage.derive(p.runs) == [SEED, "tests/test_seed.py::test_fine"], "most distinct runs first"
    with pytest.raises(SystemExit) as failed:
        cli.main(["checks", "--detach"])
    out = capsys.readouterr().out
    assert failed.value.code == 1 and not started, "caught in the foreground: nothing detached"
    assert f"derived from this project's detached checks failures (delivery.check_prestage unset): {SEED}" in out
    assert "seeded-static-failure" in out and "NOT started" in out and "slow-test-ran" not in out
    line = checkprestage.audit_line(p.db, time.time() - 86400)
    assert SEED in line and "caught 1 failure(s) before detaching (1 wake(s) saved)" in line
    assert any(x == line for x in audit.lines(p.db))


def test_a_derived_check_prestage_leaves_out_a_test_recorded_over_5_s(env, tmp_path, monkeypatch, capsys):
    from ttp import checkprestage, cli, slowtests
    p, run, started = _derived_setup(env, tmp_path, monkeypatch)
    slow, quick = "tests/test_seed.py::test_slow_one", "tests/test_seed.py::test_fine"
    _seed_check_failure(p, 1, [slow, quick], durations={slow: 6.0, quick: 4.9})
    _seed_check_failure(p, 2, [slow])
    assert checkprestage.derive(p.runs) == [quick]
    cli.main(["checks", "--detach"])
    out = capsys.readouterr().out
    assert len(started) == 1 and "slow-test-ran" not in out and "pre-stage passed" in out
    # The pre-stage times its own tests: one found slow there is left out next time.
    times = run / checkprestage.DURATIONS
    assert [i for i, _ in slowtests.load(times)] == [quick]
    later = p.runs / "9" / checkprestage.DURATIONS   # a run's directory is under the project's runs
    later.parent.mkdir()
    later.write_text(json.dumps({"tests": [{"id": quick, "s": 5.0}]}))
    assert checkprestage.derive(p.runs) == []


def test_a_derived_check_prestage_that_times_out_leaves_its_unfinished_test_out_next_time(env, tmp_path, monkeypatch, capsys):
    """A derived list whose unrecorded test outlasts the pre-stage timeout: the tests that ended keep
    their times and the one cut off is recorded as slow, so the next derivation leaves it out and
    finishes, instead of timing out on every detach until its failures age out."""
    from ttp import checkprestage, cli, slowtests
    p, run, started = _derived_setup(env, tmp_path, monkeypatch)
    run = p.runs / "41"   # as in a real run, its directory is among the project's runs derive reads
    run.mkdir(parents=True)
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    repo = tmp_path / "work"
    (repo / "tests" / "test_hang.py").write_text(
        "import time\n\n\ndef test_quick():\n    pass\n\n\n"
        "def test_hangs():\n    time.sleep(120)\n\n\ndef test_after():\n    pass\n")
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "commit", "-qam", "hang", "--allow-empty"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "commit", "-qm", "hang"], check=True)
    quick, hangs, after = (f"tests/test_hang.py::{n}" for n in ("test_quick", "test_hangs", "test_after"))
    for rid in (1, 2):
        _seed_check_failure(p, rid, [hangs, quick, after])
    default = checkprestage.DEFAULT_TIMEOUT_S
    monkeypatch.setattr(checkprestage, "DEFAULT_TIMEOUT_S", 6.0)   # short enough to cut test_hangs off
    # The 6 s count from test_hangs' start (its live row), not pytest's start-up, which a loaded
    # host can stretch past them before any test begins: the real timeout then cuts it off.
    real, live = checkprestage.boundedchild.run, run / checkprestage.LIVE

    def hangs_began(pid):
        end = time.monotonic() + 120
        while time.monotonic() < end and not any(
                f'"{hangs}"' in ln and '"start"' in ln
                for ln in (live.read_text(errors="replace") if live.exists() else "").splitlines()):
            time.sleep(0.05)
        return True

    gated = []
    def first_gated(argv, timeout, **kw):
        if gated or "test_hang.py" not in str(argv):
            return real(argv, timeout, **kw)
        gated.append(argv)
        return real(argv, timeout, on_start=hangs_began, **kw)
    monkeypatch.setattr(checkprestage.boundedchild, "run", first_gated)
    assert checkprestage.derive(p.runs) == [after, hangs, quick]
    cli.main(["checks", "--detach"])
    out = capsys.readouterr().out
    assert len(started) == 1 and "pre-stage timed out after 6 s" in out
    times = dict(slowtests.load(run / checkprestage.DURATIONS))
    assert times.get(hangs, 0) >= checkprestage.DERIVE_FAST_S and times.get(quick, 9) < 1, times
    assert after not in times, "a test that never started is not guessed slow"
    assert checkprestage.derive(p.runs) == [after, quick]
    # Two quick tests now: the product default, so a loaded host cannot time them out at the 6 s above.
    monkeypatch.setattr(checkprestage, "DEFAULT_TIMEOUT_S", default)
    cli.main(["checks", "--detach"])
    assert len(started) == 2 and "pre-stage passed" in capsys.readouterr().out


def test_a_derived_check_prestage_timeout_learns_only_what_its_live_log_shows(tmp_path):
    from ttp import checkprestage, slowtests
    said = []
    ids = ["t.py::test_a", "t.py::test_b", "t.py::test_c"]
    live, times = tmp_path / checkprestage.LIVE, tmp_path / checkprestage.DURATIONS
    # Nothing started (collection or setup outlasted the timeout): every derived id is left out.
    checkprestage._learn_from_timeout(tmp_path, ids, 7.0, said.append)
    assert dict(slowtests.load(times)) == {i: 7.0 for i in ids} and ids[0] in said[-1]
    # One ended, one cut off (its end line cut short by the stop), one never started; earlier
    # times in the file are kept, its own new ones win.
    times.write_text(json.dumps({"tests": [{"id": "t.py::test_old", "s": 9.0}, {"id": "t.py::test_a", "s": 7.0}]}))
    now = time.time()
    live.write_text(json.dumps({"id": "t.py::test_a", "start": now - 3}) + "\n"
                    + json.dumps({"id": "t.py::test_a", "s": 0.2}) + "\n"
                    + json.dumps({"id": "t.py::test_b", "start": now - 1}) + "\n" + '{"id": "t.py::test_b", "s')
    checkprestage._learn_from_timeout(tmp_path, ids, 3.0, said.append)
    got = dict(slowtests.load(times))
    assert got["t.py::test_a"] == 0.2 and got["t.py::test_old"] == 9.0
    assert got["t.py::test_b"] >= checkprestage.DERIVE_FAST_S and "t.py::test_c" not in got, "never started"
    assert said[-1].endswith(": t.py::test_b") and not list(tmp_path.glob("*.tmp"))
    # An unreadable live log or DURATIONS never raises.
    live.write_text("not json\n")
    times.write_text("{")
    checkprestage._learn_from_timeout(tmp_path / "gone", ids, 3.0, said.append)
    checkprestage._learn_from_timeout(tmp_path, ids, 3.0, said.append)


def test_a_derived_check_prestage_id_that_no_longer_exists_never_fails_it(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    p, run, started = _derived_setup(env, tmp_path, monkeypatch)
    gone = ["tests/test_seed.py::test_renamed_away", "tests/test_deleted.py::test_x",
            "tests/test_seed.py::test_fine[gone-param]"]
    _seed_check_failure(p, 1, gone)
    cli.main(["checks", "--detach"])   # nothing selected (pytest exit 5): a pass
    out = capsys.readouterr().out
    assert len(started) == 1 and "pre-stage passed" in out and "not found" not in out
    # A stale id beside a live failing one: the live one still runs and is caught (pytest refuses a
    # whole run naming one missing id).
    _seed_check_failure(p, 2, [*gone, SEED])
    with pytest.raises(SystemExit):
        cli.main(["checks", "--detach"])
    assert len(started) == 1 and "seeded-static-failure" in capsys.readouterr().out
    # Every derived file gone: no pre-stage, the full checks start.
    for rid in (1, 2):
        _seed_check_failure(p, rid, ["tests/test_deleted.py::test_x"])
    cli.main(["checks", "--detach"])
    assert len(started) == 2 and "none of the derived tests' files exist" in capsys.readouterr().out


@pytest.mark.parametrize("off", ["", False], ids=["empty", "false"])
def test_an_explicit_empty_check_prestage_turns_the_derived_one_off(env, tmp_path, monkeypatch, capsys, off):
    from ttp import cli, project
    p, run, started = _derived_setup(env, tmp_path, monkeypatch, off)
    _seed_check_failure(p, 1, [SEED])
    cli.main(["checks", "--detach"])
    out = capsys.readouterr().out
    assert len(started) == 1 and "pre-stage" not in out and "seeded-static-failure" not in out
    assert not project.config_problems({"delivery": {"check_prestage": off}})


def test_no_check_failure_history_means_no_derived_check_prestage(env, tmp_path, monkeypatch, capsys):
    from ttp import checkprestage, cli
    p, run, started = _derived_setup(env, tmp_path, monkeypatch)
    # Older than the window, or a failure of foreground checks only: no history.
    _seed_check_failure(p, 1, [SEED], age_s=checkprestage.DERIVE_WINDOW_S + 3600)
    _seed_check_failure(p, 2, [SEED], detached=False)
    cli.main(["checks", "--detach"])
    out = capsys.readouterr().out
    assert len(started) == 1 and "none in the last 14 days, so none runs" in out
    assert "seeded-static-failure" not in out and "pre-stage passed" not in out
    assert checkprestage.audit_line(p.db, 0) == "derived check pre-stage: 0 test(s); caught 0 failure(s) " \
        "before detaching (0 wake(s) saved)"
    # A project whose checks run no pytest here gets none, and says nothing of it.
    p.set_config("delivery.push_checks", ["true"])
    _seed_check_failure(p, 3, [SEED])
    cli.main(["checks", "--detach"])
    assert len(started) == 2 and "pre-stage" not in capsys.readouterr().out


def test_an_explicit_check_prestage_is_used_unchanged_over_a_derived_one(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    p, run, started = _derived_setup(env, tmp_path, monkeypatch, "echo explicit-ran")
    _seed_check_failure(p, 1, [SEED])
    cli.main(["checks", "--detach"])
    out = capsys.readouterr().out
    assert len(started) == 1 and "pre-stage 'echo explicit-ran' (delivery.check_prestage" in out
    assert "explicit-ran" in out and "derived" not in out and "seeded-static-failure" not in out


def test_a_derived_check_prestage_exports_the_pytest_checks_env(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    p, run, started = _derived_setup(env, tmp_path, monkeypatch)
    monkeypatch.delenv("X", raising=False)
    repo = tmp_path / "work"
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    (repo / "tests" / "test_env.py").write_text(
        "import os\n\n\ndef test_needs_x():\n    assert os.environ.get('X') == '1', 'env-not-exported'\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "env test"], check=True)
    p.set_config("delivery.push_checks", [{"run": "python3 -m pytest -q tests", "env": {"X": "1"}}])
    _seed_check_failure(p, 1, ["tests/test_env.py::test_needs_x"])
    cli.main(["checks", "--detach"])
    out = capsys.readouterr().out
    assert "env-not-exported" not in out and "pre-stage passed" in out and len(started) == 1, out


def test_check_output_batch_child_has_own_writable_directory(env, monkeypatch):
    from ttp import pushq
    command = ('test -n "$TTP_RUN_DIR"; test "$TTP_RUN_DIR" != /; '
               'test -z "${TTP_PIDNS:-}"; test -z "${TTP_CGROUP:-}"; '
               'echo report > "$TTP_RUN_DIR/x"')
    s = _pq_real(env, monkeypatch, push_checks=[command])
    monkeypatch.setenv('TTP_PIDNS', 'worker-only')
    monkeypatch.setenv('TTP_CGROUP', 'worker-only')
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    marker = s.p.state / 'pushes' / f'{bid}.json'
    m = json.loads(marker.read_text())
    assert m['outcome'] == 'pushed', marker.with_suffix('.log').read_text()
    output = pathlib.Path(m['check_run_dir'])
    assert not output.exists()
    reports = pathlib.Path(m['check_report_dir'])
    assert reports.is_relative_to(s.p.state) and (reports / 'x').read_text() == 'report\n'


@pytest.mark.parametrize('bad', ['symlink', 'file', 'root', 'missing'])
def test_check_output_bad_queue_directory_runs_no_check(env, monkeypatch, capsys, bad):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ['true'])
    _commit(repo, 'mine.txt', 'mine\n')
    monkeypatch.setattr(push, '_boxed', lambda: True)
    assert _ttp('push', '--detach') == 0
    out = capsys.readouterr().out
    marker = pathlib.Path(next(ln.split(': ', 1)[1] for ln in out.splitlines() if ln.startswith('marker: ')))
    m = json.loads(marker.read_text())
    output = pathlib.Path(m['check_run_dir'])
    shutil.rmtree(output)
    if bad == 'symlink':
        output.symlink_to(repo, target_is_directory=True)
    elif bad == 'file':
        output.write_text('keep')
    elif bad == 'root':
        m['check_run_dir'] = '/'
    else:
        del m['check_run_dir']
    marker.write_text(json.dumps(m))
    real_popen = push.subprocess.Popen
    def never(argv, *a, **kw):
        if argv == ['git', '--version']:
            return real_popen(argv, *a, **kw)
        pytest.fail('started a check with invalid output storage')
    monkeypatch.setattr(push.subprocess, 'Popen', never)
    push.tend(p)
    m = json.loads(marker.read_text())
    assert m['status'] == 'failed' and 'TTP_RUN_DIR' in m['reason']


def test_check_output_missing_in_batch_process_fails_before_checks(env, monkeypatch):
    from ttp import batch, push
    check = env['tmp'] / 'must-not-run'
    p, repo, origin, other = _bump_setup(env, monkeypatch, [f'touch {shlex.quote(str(check))}'])
    heads = [_entry(repo, 'e1', {'feature.txt': 'feature'})]
    marker = _batch_marker(p, heads)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))
    monkeypatch.delenv('TTP_RUN_DIR', raising=False)
    assert batch.run_batch(p, marker) != 0
    m = push._read(marker)
    assert m['outcome'] == 'error' and 'TTP_RUN_DIR' in m['message']
    assert not check.exists()


@pytest.mark.parametrize('mode', ['detached', 'batch-env'])
def test_check_scratch_git_discovery_stops_before_project(env, tmp_path, monkeypatch, mode):
    from ttp import cli, checkdirs, pushq
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    subprocess.run(['git', 'init', '-q', str(p.root)], check=True)
    inherited = str(tmp_path / 'existing-ceiling')
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', inherited)
    code = ("import os, subprocess, tempfile; "
            f"assert {inherited!r} in os.environ['GIT_CEILING_DIRECTORIES'].split(os.pathsep); "
            "r = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=tempfile.gettempdir(), capture_output=True); "
            "assert r.returncode != 0, r.stdout; "
            f"r = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd={str(repo)!r}, capture_output=True, text=True); "
            f"assert r.returncode == 0 and r.stdout.strip() == {str(repo)!r}")
    if mode == 'detached':
        cli.main(['checks', '--detach', '--fresh', '--', shlex.join([sys.executable, '-c', code])])
        assert _wait_precheck(run) == '0', (run / cli.CHECKS_OUT).read_text()
    else:
        output = checkdirs.new(p.state)
        with checkdirs.take(output) as guard:
            child_env = pushq._child_env(p, str(output))
            child_env['TTP_CHECKS_TMP_FD'] = str(guard.fileno())
            r = subprocess.run([sys.executable, '-c', code], env=checkdirs.for_check(child_env),
                               pass_fds=(guard.fileno(),), capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


@pytest.mark.parametrize('kind', ['push', 'batch'])
def test_check_scratch_push_and_batch_live_across_recovery(env, tmp_path, monkeypatch, capsys, kind):
    from ttp import checkdirs, push, pushq
    from ttp.project import Project
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    os.mkfifo(release)
    code = ("import os, pathlib, subprocess, tempfile; "
            "os.fstat(int(os.environ['TTP_CHECKS_TMP_FD'])); "
            "f = pathlib.Path(tempfile.mkdtemp()) / 'keep'; f.write_text('alive'); "
            "assert subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=tempfile.gettempdir(), capture_output=True).returncode != 0; "
            "assert subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True).returncode == 0; "
            f"pathlib.Path({str(ready)!r}).write_text(str(f)); "
            f"open({str(release)!r}).read(); assert f.read_text() == 'alive'; "
            "pathlib.Path(os.environ['TTP_RUN_DIR'], 'report').write_text('kept')")
    command = shlex.join([sys.executable, '-c', code])
    if kind == 'push':
        p, repo, origin, other = _push_setup(env, monkeypatch, [command])
        _commit(repo, 'mine.txt', 'mine\n')
        monkeypatch.setattr(push, '_boxed', lambda: True)
        assert _ttp('push', '--detach') == 0
        output = capsys.readouterr().out
        marker = pathlib.Path(next(ln.split(': ', 1)[1] for ln in output.splitlines() if ln.startswith('marker: ')))
        scratch = pathlib.Path(push._read(marker)['check_run_dir'])
        # No child or lifetime lock exists while queued; a new daemon must preserve the allocation.
        checkdirs.recover(p.state, p.runs)
        assert scratch.exists()
        push.tend(Project(p.base))
    else:
        s = _pq_real(env, monkeypatch, push_checks=[command])
        p = s.p
        _pq_hand_off(env, s)
        bid = _pq_batch(s, wait=False)
        marker = p.state / 'pushes' / f'{bid}.json'
        scratch = pathlib.Path(push._read(marker)['check_run_dir'])
    try:
        assert _wait_for(ready.exists), marker.with_suffix('.log').read_text()
        fresh = Project(p.base)
        checkdirs.recover(fresh.state, fresh.runs)
        assert pathlib.Path(ready.read_text()).read_text() == 'alive'
        assert checkdirs.take(scratch) is None, 'push/batch did not hold the scratch lifetime lock'
        release.write_text('go')
        if kind == 'push':
            result = _probe_until_done(p, push.result_probe(marker, p))
            assert 'pushed' in result.stdout, result.stdout
        else:
            assert pushq._children[bid].wait(timeout=30) == 0
        assert _wait_for(lambda: not scratch.exists())
        reports = pathlib.Path(push._read(marker)['check_report_dir'])
        assert (reports / 'report').read_text() == 'kept'
    finally:
        if kind == 'batch':
            child = pushq._children[bid]
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
        else:
            pid = push._read(marker).get('pid')
            if pid and not _gone_pid(pid):
                os.killpg(pid, signal.SIGKILL)


@pytest.mark.parametrize('own', [False, True])
def test_check_output_foreground_push_preserves_worker_context(env, monkeypatch, own):
    from ttp import push
    seen = env['tmp'] / 'seen'
    command = (f'printf "%s\\n%s\\n" "$TTP_RUN_DIR" "$TMPDIR" > {shlex.quote(str(seen))}; '
               'echo report > "$TTP_RUN_DIR/report"')
    p, repo, origin, other = _push_setup(env, monkeypatch, [command])
    worker = env['tmp'] / 'run'
    worker.mkdir()
    if own:
        _git_out(repo, 'checkout', '-qb', 'task/check-output')
    monkeypatch.setenv('TTP_RUN_DIR', str(worker))
    before = dict(os.environ)
    real = push.take

    def take(*a, **kw):
        assert os.environ['TTP_RUN_DIR'] == str(worker), 'push bookkeeping lost the worker run'
        return real(*a, **kw)

    monkeypatch.setattr(push, 'take', take)
    _commit(repo, 'mine.txt', 'mine\n')
    assert push.run(p, repo, own=own) == 0
    report, scratch = map(pathlib.Path, seen.read_text().splitlines())
    assert report != worker and report.is_relative_to(p.state / 'check-reports')
    assert (report / 'report').read_text() == 'report\n'
    assert not scratch.exists()
    assert not (worker / 'report').exists()
    assert dict(os.environ) == before


@pytest.mark.parametrize('kind', ['detached-check', 'foreground-push'])
def test_check_scratch_failed_launch_or_check_cleans_allocation(env, tmp_path, monkeypatch, kind):
    from ttp import cli, checkdirs, push
    if kind == 'detached-check':
        p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
        real_popen = cli.subprocess.Popen
        def launch(argv, *args, **kw):
            if isinstance(argv, list) and argv[:3] == [sys.executable, '-m', 'ttp']:
                raise OSError('test launch refused')
            return real_popen(argv, *args, **kw)
        monkeypatch.setattr(cli.subprocess, 'Popen', launch)
        with pytest.raises(SystemExit):
            cli.main(['checks', '--detach', '--', 'true'])
    else:
        p, repo, origin, other = _push_setup(env, monkeypatch, ['false'])
        _commit(repo, 'mine.txt', 'mine\n')
        assert push.run(p, repo) == push.CHECKS_FAILED
    assert not [f for f in (p.state / checkdirs.FOLDER).iterdir() if f.is_dir()]


def test_live_after_push_keeps_ordinary_temp_after_batch_supervisor_dies(env, tmp_path, monkeypatch):
    from ttp import checkdirs, pushq, push
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    os.mkfifo(release)
    normal = tmp_path / 'daemon-tmp'
    normal.mkdir()
    for key in ('TMPDIR', 'TMP', 'TEMP'):
        monkeypatch.setenv(key, str(normal))
    code = ('import os,json,pathlib,tempfile; '
            "f=pathlib.Path(tempfile.mkdtemp())/'keep'; f.write_text('alive'); "
            f'pathlib.Path({str(ready)!r}).write_text(json.dumps([os.getpid(),str(f)])); '
            f'open({str(release)!r}).read(); assert f.read_text() == "alive"')
    s = _pq_real(env, monkeypatch, after_push=[shlex.join([sys.executable, '-c', code])])
    _pq_hand_off(env, s)
    bid = _pq_batch(s, wait=False)
    marker = s.p.state / 'pushes' / f'{bid}.json'
    child = pushq._children[bid]
    after_pid = None
    try:
        assert _wait_for(ready.exists), marker.with_suffix('.log').read_text()
        after_pid, temp = json.loads(ready.read_text())
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
        os.kill(after_pid, 0)
        checkdirs.recover(s.p.state, s.p.runs)
        assert pathlib.Path(temp).read_text() == 'alive'
        assert pathlib.Path(temp).is_relative_to(normal)
        assert not pathlib.Path(push._read(marker)['check_run_dir']).exists()
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
        if after_pid:
            try:
                os.kill(after_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_a_task_already_waiting_on_a_missing_task_is_flagged_once_and_shown_broken(env):
    """State from before the check: the daemon raises one event per task, does not run the probe, `ttp landed`
    says it cannot tell, and status and the web app show the task as broken, not as a plan."""
    from ttp import coordinator as coord
    from ttp import landed
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    from ttp.web import state_payload
    p = make(env)
    tid = p.db.add_task("ghost wait", "s", origin="user",
                        labels=coord.defer_labels(None, "landed:#0"))
    held = p.db.add_task("ghost hold", "s", origin="user")
    p.db.update_task(held, status="blocked", labels=json.dumps(["waits:when:ttp landed --task 77777"]))
    fine = p.db.add_task("fine wait", "s", origin="user", labels=coord.defer_labels(None, f"landed:#{held}"))
    d = Daemon(p.base)
    for _ in range(2):
        d.reconcile_tasks()
    ev = p.db.q("SELECT task, text, status FROM events WHERE kind='missing_task_ref' ORDER BY task")
    assert [e["task"] for e in ev] == [tid, held], ev
    assert "waits on #0, which does not exist" in ev[0]["text"] and ev[0]["status"] == "queued"
    assert "waits on #77777, which does not exist" in ev[1]["text"]
    assert coord.EFFORT_EVENT_TRIGGERS["missing_task_ref"] == "stuck"
    started = []
    d._start_probe = lambda tid_, probe, now, what="retry_when": (started.append(tid_),
                                                                   d._probe_rc.__setitem__(tid_, (1, now, probe)))
    d.probe_deferred(time.time())
    assert tid not in started and fine in started, started
    assert landed.check(p, p.root, None, 0)[0] == landed.CANNOT, "a probe on a missing task said 'not yet'"
    t = next(t for t in state_payload(p, p.db)["tasks"] if t["id"] == tid)
    assert t["starts"] == "broken: waits on #0, which does not exist", t
    out = status_text(p)
    assert f"  #{tid} broken: waits on #0, which does not exist: ghost wait" in out, out
    assert "starts when #0 lands" not in out
    assert "broken: waits on #0, which does not exist (start_when: landed:#0)" in coord.digest(p, {}, [], [])


def test_check_purpose_tag_is_validated_and_kept(tmp_path, monkeypatch):
    from ttp import push
    _fake_check_plan_ttp(tmp_path, monkeypatch, tmp_path / "log")
    harness = {"run": HARNESS_PLAN, "purpose": "harness"}
    gate = {"run": "true", "purpose": "candidate"}
    assert push.check_problems([harness, gate, "true"]) == []
    checks = push.check_list([harness, gate, "true"])
    assert [c.purpose for c in checks] == ["harness", "candidate", ""]
    assert [c.config() for c in checks] == [harness, gate, "true"]
    assert push.check_sig(checks[1]) != push.check_sig(push.Check("true")), "purpose is part of the signature"
    # An unknown purpose is a config error, never a silent skip.
    bad = push.check_problems([{"run": "true", "purpose": "publish"}])
    assert len(bad) == 1 and "purpose must be one of candidate, harness" in bad[0]
    with pytest.raises(ValueError):
        push.checks_of([{"run": "true", "purpose": ""}])
    # A harness entry is only a full-hex, exact check-plan harness command.
    for cmd in ("true", HARNESS_PLAN.replace("harness --def", "candidate --def"),
                HARNESS_PLAN.replace("a" * 40, "a" * 12), HARNESS_PLAN.replace("checks/harness.json", "/abs.json"),
                HARNESS_PLAN + " --extra", HARNESS_PLAN.replace(" --definition-path checks/harness.json", ""),
                HARNESS_PLAN + " --purpose harness"):
        assert any("purpose harness check must be" in b for b in push.check_problems([{"run": cmd, "purpose": "harness"}])), cmd
    assert push.check_problems([{"run": HARNESS_PLAN.replace("--definition-commit ", "--definition-commit="),
                                 "purpose": "harness"}]) == []
    at_head = HARNESS_PLAN.replace("--definition-commit " + "a" * 40, "--definition-at-head")
    assert push.check_problems([{"run": at_head, "purpose": "harness"}]) == []
    for cmd in (at_head + " --definition-commit " + "a" * 40, at_head.replace("-at-head", "-at-head=yes"),
                at_head.replace(" --definition-path checks/harness.json", ""),
                at_head.replace("--definition-at-head", "--definition-at-head --definition-at-head")):
        assert any("purpose harness check must be" in b for b in push.check_problems([{"run": cmd, "purpose": "harness"}])), cmd


def test_ttp_checks_purpose_harness_never_counts_as_candidate_evidence(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, prguard, push
    from ttp import daemon as daemon_mod
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    log = tmp_path / "log"
    _fake_check_plan_ttp(tmp_path, monkeypatch, log)
    selector = ("sh -c 'echo \"mandatory check selector: no case for HEAD $(git rev-parse HEAD)\"; "
                "echo gate >> " + str(log) + "; exit 1'")
    p.set_config("delivery.push_checks", [{"run": selector, "purpose": "candidate"},
                                          {"run": HARNESS_PLAN, "purpose": "harness"}, f"echo consumer >> {log}"])
    cli.main(["checks", "--purpose", "harness", "--", f"echo extra >> {log}"])
    out = capsys.readouterr().out
    assert log.read_text().split() == ["plan", "consumer", "extra"], "a candidate gate ran or a consumer was dropped"
    assert f"not run: candidate publication gate: {selector}" in out and "harness qualification only" in out
    rec = json.loads((run / "checks.json").read_text())
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert rec["passed"] is True and rec["purpose"] == "harness" and rec["head"] == head
    assert rec["not_run"] == [f"not run: candidate publication gate: {selector}"]
    assert selector not in rec["commands"]
    assert not (run / "check_failure.json").exists()
    # The PR gate, the on_pass head, `ttp push --own`'s reuse and the task's other runs ignore it.
    assert "harness qualification" in prguard._checks_problem(str(repo))
    assert daemon_mod._checked_head(run, {"kind": "code", "branch": ""}) == ""
    assert push.recorded_checks(run) is None
    monkeypatch.setenv("TTP_TASK", "1")
    assert prguard._task_pass(head, str(repo)) is None
    # The candidate default runs every entry but harness-tagged ones: no harness pass is reused, the gate fails it.
    log.unlink()
    with pytest.raises(SystemExit) as exc:
        cli.main(["checks"])
    assert exc.value.code == 1 and log.read_text().split() == ["gate"]
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["purpose"] == "candidate"
    assert json.loads((run / "check_failure.json").read_text())["kind"] == "selector_missing"
    # A record from before purposes existed still counts as candidate evidence.
    (run / "checks.json").write_text(json.dumps({"head": head, "worktree": str(repo), "passed": True,
                                                 "commands": ["true"], "skipped": [], "ts": 0}))
    assert prguard._checks_problem(str(repo)) is None and push.recorded_checks(run)["head"] == head


@pytest.mark.parametrize("repo_kind", ["other_repo", "harness_repo"])
def test_ttp_checks_candidate_skips_harness_checks(env, tmp_path, monkeypatch, capsys, repo_kind):
    """The candidate `ttp checks` (a code task's checks) never runs a `purpose: harness` entry, whether
    or not the worktree's repo has its definition commit: it passes on the repo's own checks and
    reports the harness check skipped, never passed. `--purpose harness` still runs it, and with no
    other check nothing applies."""
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    log = tmp_path / "log"
    _fake_check_plan_ttp(tmp_path, monkeypatch, log)
    plan = HARNESS_PLAN if repo_kind == "other_repo" else HARNESS_PLAN.replace(
        "a" * 40, subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip())
    p.set_config("delivery.push_checks", [{"run": plan, "purpose": "harness"}, f"echo consumer >> {log}"])
    cli.main(["checks"])
    out = capsys.readouterr().out
    assert log.read_text().split() == ["consumer"], "a candidate run ran the harness check"
    assert push.HARNESS_ONLY in out and "1 check(s) passed" in out and "1 skipped" in out
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["purpose"] == "candidate" and rec["commands"] == [f"echo consumer >> {log}"]
    assert len(rec["skipped"]) == 1 and plan in rec["skipped"][0]
    log.unlink()
    cli.main(["checks", "--purpose", "harness"])
    assert log.read_text().split() == ["plan", "consumer"], "a harness qualification skipped its check"
    assert json.loads((run / "checks.json").read_text())["purpose"] == "harness"
    p.set_config("delivery.push_checks", [{"run": plan, "purpose": "harness"}])
    with pytest.raises(SystemExit) as exc:
        cli.main(["checks"])
    assert exc.value.code == 1
    assert json.loads((run / "checks.json").read_text())["passed"] is False


def test_candidate_harness_skip_never_hides_an_outside_scope_change(env, tmp_path):
    """A candidate run that leaves out a harness check still sees a change outside every other check's
    `if_changed` scope as outside_scope, and a run of only harness checks as nothing applying."""
    from ttp import push
    harness, scoped = push.check_list([{"run": HARNESS_PLAN, "purpose": "harness"},
                                       {"run": "true", "if_changed": "src"}])
    out = push.Skipped(push.skipped_line(scoped, "x"))
    out.scoped = True
    left = push.Skipped(push.skipped_line(harness, push.HARNESS_ONLY))
    left.harness_only = True
    assert push.outside_scope([harness, scoped], [left, out])
    assert not push.outside_scope([harness], [left])


def test_push_own_skips_harness_checks_but_push_runs_them(env, monkeypatch, tmp_path):
    from ttp import push
    log = tmp_path / "log"
    _fake_check_plan_ttp(tmp_path, monkeypatch, log)
    p, repo, origin, other = _push_setup(env, monkeypatch, [])
    (tmp_path / "run").mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path / "run"))
    _git_out(repo, "checkout", "-q", "-b", "own")
    _commit(repo, "x.txt", "x\n")
    head = _git_out(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "y.txt", "y\n")
    for plan in (HARNESS_PLAN, HARNESS_PLAN.replace("a" * 40, head)):   # another repo's plan, then this repo's
        checks = push.check_list([{"run": plan, "purpose": "harness"}, f"echo consumer >> {log}"])
        said = []
        assert push.publish(repo, "origin", "own", checks, say=said.append) == 0
        assert log.read_text().split() == ["consumer"], said
        assert any(push.HARNESS_ONLY in m for m in said)
        log.unlink()
        _commit(repo, "z.txt", plan)


def test_ttp_checks_purpose_harness_falls_back_to_the_default_plan(env, tmp_path, monkeypatch, capsys):
    """With no purpose harness check configured, `ttp checks --purpose harness` runs the built-in plan:
    a whitespace check and pytest of the harness's own test folders it has (a missing one is skipped,
    never passed). A configured purpose harness check replaces it."""
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    (repo / "runtime" / "tests").mkdir(parents=True)
    (repo / "runtime" / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run([*git, "add", "runtime"], check=True)
    subprocess.run([*git, "commit", "-qm", "tests"], check=True)
    parent = subprocess.run([*git, "rev-parse", "HEAD^"], capture_output=True, text=True).stdout.strip()
    cli.main(["checks", "--purpose", "harness"])   # a clean harness repo: exits 0
    out = capsys.readouterr().out
    assert "running the default harness plan" in out
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["purpose"] == "harness"
    assert rec["commands"] == [push.whitespace_check(parent), "python3 -m pytest -q runtime/tests"]
    assert len(rec["skipped"]) == 1 and "bin/tests" in rec["skipped"][0]
    # A root commit is compared with the empty tree; with no test folder only the whitespace check runs.
    assert str(push.default_harness_checks(repo, "")[0]) == push.whitespace_check(parent)
    subprocess.run([*git, "checkout", "-q", "--orphan", "fresh"], check=True)
    subprocess.run([*git, "rm", "-rq", "--cached", "runtime"], check=True)
    (repo / "runtime" / "tests" / "test_ok.py").unlink()
    subprocess.run([*git, "commit", "-qm", "root"], check=True)
    cli.main(["checks", "--purpose", "harness", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["commands"] == [push.whitespace_check(push.EMPTY_TREE)]
    assert len(rec["skipped"]) == 2
    capsys.readouterr()
    # A configured purpose harness check wins: the default plan does not run.
    log = tmp_path / "log"
    _fake_check_plan_ttp(tmp_path, monkeypatch, log)
    p.set_config("delivery.push_checks", [{"run": HARNESS_PLAN, "purpose": "harness"}])
    cli.main(["checks", "--purpose", "harness", "--fresh"])
    assert "default harness plan" not in capsys.readouterr().out
    assert log.read_text().split() == ["plan"]
    assert json.loads((run / "checks.json").read_text())["commands"] == [HARNESS_PLAN]
    # The candidate purpose never gets the default plan.
    p.set_config("delivery.push_checks", [])
    cli.main(["checks", "--", "true"])
    assert json.loads((run / "checks.json").read_text())["commands"] == ["true"]


def test_a_continuation_checks_only_the_commits_after_its_inherited_marker(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, push, verified
    p, repo, run, git, counts = _verified_checks_repo(env, tmp_path, monkeypatch)
    first = p.db.add_task("first hop", kind="code")
    nxt = p.db.add_task("next hop", kind="code", labels=[f"continues:{first}"])
    monkeypatch.setenv("TTP_TASK", str(first))
    marked = _commit_in(git, repo, "src/a.py", "a = 1\n")
    cli.main(["checks"])
    assert counts() == (1, 1)
    assert verified.own(p.db, first, "checks")["sha"] == marked
    # The next hop inherits the marker: its commit touches nothing under src/**, so the scoped check is
    # verified through the marker; the unscoped one covers the whole tree and runs again.
    monkeypatch.setenv("TTP_TASK", str(nxt))
    monkeypatch.setenv("TTP_RUN_ID", "42")
    head = _commit_in(git, repo, "notes.txt", "notes\n")
    capsys.readouterr()
    cli.main(["checks"])
    out = capsys.readouterr().out
    assert counts() == (2, 1), out
    assert f"verified through {marked[:12]} (#{first}'s checks)" in out
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["verified_through"] == marked and len(rec["verified"]) == 1
    # Its own marker carries the verified check on, so the hop after it can skip it too.
    assert len(verified.own(p.db, nxt, "checks")["commands"]) == 2
    # The push gate never reads the marker: the scoped check still applies to the pushed head.
    todo, _ = push.applicable(repo, head, push.check_list(p.config()["delivery"]["push_checks"]),
                              lambda m: None, "origin/proj")
    assert len(todo) == 2
    # A rerun on the same head by the same task (e.g. the daemon's pre-started checks) reuses what is
    # known: the scoped check through its own marker at HEAD, the unscoped one through its recorded pass.
    capsys.readouterr()
    cli.main(["checks"])
    out = capsys.readouterr().out
    assert counts() == (2, 1), out
    assert "(recorded)" in out
    # --fresh runs every check again.
    cli.main(["checks", "--fresh"])
    assert counts() == (3, 2)
    # The first hop's own rerun on its head still hits its recorded pass of every check.
    monkeypatch.setenv("TTP_TASK", str(first))
    subprocess.run([*git, "checkout", "-q", marked], check=True)
    cli.main(["checks"])
    assert counts() == (3, 2)
    subprocess.run([*git, "checkout", "-q", "-"], check=True)
    monkeypatch.setenv("TTP_TASK", str(nxt))
    # A commit after the marker under src/** runs the scoped check again.
    _commit_in(git, repo, "src/b.py", "b = 1\n")
    cli.main(["checks"])
    assert counts() == (4, 3)


def test_a_review_only_verdict_does_not_stand_in_for_a_review_that_pushes(env, monkeypatch):
    """A review-only review (kept off, delivered as a PR, no push allowed) accepts a head without
    pushing it. In a project that pushes, a later code task or review of that head still gets its
    review, so the change is delivered; a later review-only review may reuse the verdict."""
    from ttp import coordinator as coord, push, pushq, reviewreuse
    s = _pq(env, monkeypatch)
    fix, _, _, head = _re_plan(s, f"pr_branch:{s.branch}")
    d, started = _reuse_daemon(s.p, monkeypatch)
    key = reviewreuse.check_key(d.cfg)
    ro = s.p.db.add_task("Review only", "Review only: do not push.", kind="review", origin="coordinator",
                         labels=[push.REVIEW_ONLY_LABEL, reviewreuse.head_label(head, key)])
    s.p.db.update_task(ro, status="done", result=json.dumps({"status": "done", "summary": "ok, review only"}))
    assert not reviewreuse.delivered(s.p, head)
    # The code task that ends on that head gets a review, not the review-only verdict.
    done = _finish_fix(env, s.p, fix)
    assert "already accepted" not in done["text"]
    queued = s.p.db.q("SELECT id, title FROM tasks WHERE kind='review' AND status='queued'")
    assert [r["title"][:len(f"Review #{fix}")] for r in queued] == [f"Review #{fix}"]
    s.p.db.update_task(queued[0]["id"], status="cancelled")
    # The coordinator may add one, and a review that would push runs at dispatch.
    add = {"type": "task_add", "title": f"Review #{fix} by hand", "kind": "review", "spec": "check it",
           "depends_on": [fix]}
    assert coord.apply(s.p, [add]) == []
    s.p.db.update_task(s.p.db.one("SELECT id FROM tasks WHERE title=?", (add["title"],))["id"], status="cancelled")
    runs = _reuse_review(s.p, fix)
    d.dispatch()
    assert started == [runs] and s.p.db.task(runs)["status"] != "done"
    # A later review-only review of the head may take the review-only verdict.
    only = s.p.db.add_task("Review only again", "Review only.", kind="review", tier="light", origin="coordinator",
                           provider="claude", budget_usd=8.0, depends_on=[fix], labels=[push.REVIEW_ONLY_LABEL])
    d.dispatch()
    assert started == [runs] and json.loads(s.p.db.task(only)["result"])["reused_review"] == ro
    # Once the head is delivered, its verdict covers a review that would push.
    s.p.db.update_task(runs, status="cancelled")
    s.p.db.x("INSERT INTO push_queue(task,run,branch,head,target,status,created,updated) "
             "VALUES(?,1,'b',?,'origin/proj','pushed',0,0)", (ro, head))
    after = _reuse_review(s.p, fix, "Review it after the push")
    d.dispatch()
    assert started == [runs] and s.p.db.task(after)["status"] == "done"
    # A cancelled row does not deliver it; the push target's tip containing it does.
    s.p.db.x("UPDATE push_queue SET status='cancelled'")
    assert not reviewreuse.delivered(s.p, head)
    remote, branch = pushq.target(s.p)
    _git_out(s.p.root, "update-ref", f"refs/remotes/{remote}/{branch}", head)
    assert reviewreuse.delivered(s.p, head)


def test_push_result_line_matches_the_documented_format_and_pushed_flag_exit_codes(tmp_path, capsys):
    import json
    from pathlib import Path
    from ttp import push
    root = Path(__file__).resolve().parents[1] / "plugins/tt-project/template/prompts"
    for f in ("kind-code.md", "kind-review.md"):
        assert "ttp push: pushed <sha> to <branch>" in (root / f).read_text()
    marker = tmp_path / "m.json"
    marker.write_text(json.dumps({"status": "pushed", "sha": "abc123", "target": "b"}))
    assert push.result(marker, pushed_only=True) == 0
    assert capsys.readouterr().out.startswith("ttp push: pushed abc123 to b")
    marker.write_text(json.dumps({"status": "failed", "exit": 4, "log": "/nonexistent"}))
    assert push.result(marker, pushed_only=True) == push.NOT_PUSHED
    assert push.result(marker) == 0


def _fast_repo(tmp_path, files):
    import subprocess as sp
    g = lambda *a: sp.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    g("add", "-A"); g("commit", "-qm", "base")
    base = g("rev-parse", "HEAD")
    for name, body in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(body)
    g("add", "-A"); g("commit", "-qm", "change")
    return base, g("rev-parse", "HEAD")


def test_checks_fast_path_only_for_docs_and_data_diffs(tmp_path):
    from ttp import push
    base, head = _fast_repo(tmp_path, {"README.md": b"hi\n", "data/x/y.csv": b"1,2\n"})
    assert push.fast_path(tmp_path, base, head, push.fast_globs({"delivery": {"checks_fast_globs": DOCS_GLOBS}}))
    assert not push.fast_path(tmp_path, base, head, push.fast_globs({"delivery": {"checks_fast_globs": []}}))
    assert not push.fast_path(tmp_path, base, head, [])
    assert not push.fast_path(tmp_path, "", head, DOCS_GLOBS)
    assert push.fast_globs({"delivery": {"checks_fast_globs": ["*.rst"]}}) == ["*.rst"]


@pytest.mark.parametrize("path", [
    "CMakeLists.txt", "sub/CMakeLists.txt", "requirements.txt", "requirements-dev.txt", "constraints.txt",
    "plugins/x/skills/y/SKILL.md", "skills/y/notes.md", "plugins/x/prompts/p.md", "AGENTS.md", "CLAUDE.md",
    "docs/conf.py", "data/gen.py", "a.sh", "m.cmake", "Makefile", "c.cpp", "h.hpp", "x.js", "x.ts", "p.json",
    "p.toml", "p.yaml", "p.yml", "p.cfg", "p.ini",
    "data/gen.rs", "data/main.go", "data/run.pl", "data/x.sql", "data/Dockerfile", "docs/build.bash",
    "tests/data/golden.txt", "test/fixture.md", "plugins/x/template/CHARTER.md", "plugins/x/templates/a.txt"])
def test_checks_fast_path_never_takes_code_build_or_installed_content(tmp_path, path):
    from ttp import push
    base, head = _fast_repo(tmp_path, {path: b"x\n"})
    assert not push.fast_path(tmp_path, base, head, DOCS_GLOBS)
    if not path.startswith(("data/", "docs/build")):   # these are kept out by the extension allowlist alone
        assert not push.fast_path(tmp_path, base, head, [*DOCS_GLOBS, "*"])
    assert push.fast_path(tmp_path, base, head, ["*"], exclude=[])


def test_checks_fast_path_is_off_without_configured_globs(tmp_path):
    from ttp import push
    assert push.FAST_GLOBS == [] and push.fast_globs(None) == [] and push.fast_globs({"delivery": {}}) == []
    base, head = _fast_repo(tmp_path, {"README.md": b"hi\n", "plugins/x/README.md": b"hi\n", "docs/n.rst": b"x\n"})
    assert not push.fast_path(tmp_path, base, head, push.fast_globs(None))


def test_checks_fast_path_falls_back_for_code_manifest_or_script(tmp_path):
    from ttp import push
    base, head = _fast_repo(tmp_path, {"README.md": b"hi\n", "tests/test_a.py": b"x\n"})
    assert not push.fast_path(tmp_path, base, head, DOCS_GLOBS)


def test_checks_fast_checks_flag_crlf_and_trailing_whitespace(tmp_path):
    import subprocess as sp
    from ttp import push
    for body, rc in ((b"ok\n", 0), (b"crlf\r\n", 1), (b"trail \n", 1)):
        d = tmp_path / str(rc) / str(len(body)); d.mkdir(parents=True)
        base, head = _fast_repo(d, {"n.md": body})
        rcs = [sp.run(c, shell=True, cwd=d, executable="/bin/bash", capture_output=True).returncode
               for c in push.fast_checks(base)]
        assert (max(rcs) != 0) == bool(rc), (body, rcs)


def test_push_result_pushed_flag_on_batch_markers(tmp_path, capsys):
    from ttp import push
    done = tmp_path / "b1.json"
    done.write_text(json.dumps({"kind": "batch", "id": "b1", "phase": "finished", "outcome": "failed",
                                "target": "proj", "log": "x.log"}))
    ok = tmp_path / "b2.json"
    ok.write_text(json.dumps({"kind": "batch", "id": "b2", "phase": "finished", "outcome": "pushed",
                              "pushed_sha": "a" * 40, "target": "proj", "log": "x.log"}))
    dead = tmp_path / "b3.json"
    dead.write_text(json.dumps({"kind": "batch", "id": "b3", "phase": "running", "target": "proj"}))
    assert push.result(done) == 0 and push.result(done, pushed_only=True) == push.NOT_PUSHED
    assert push.result(ok, pushed_only=True) == 0
    assert push.result(dead, pushed_only=True) == push.NOT_PUSHED


def test_venv_rebuild_cmd_keeps_declared_upper_bounds(tmp_path):
    from ttp import push
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["mcp>=1.2,<2", "requests"]\n'
        '[dependency-groups]\ndev = ["pytest>=8,<9"]\n')
    cmd = push.venv_rebuild_cmd(tmp_path / ".venv")
    assert "'mcp>=1.2,<2'" in cmd and "'pytest>=8,<9'" in cmd and "requests" not in cmd


def test_venv_rebuild_cmd_single_clause_bounds_and_scoped_tables(tmp_path):
    from ttp import push
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools<70"]\n'
        "[project]\nname = \"x\"\ndependencies = [\"mcp<2\", \"mcp[cli]<=1.9\", 'anyio<5', \"yarl~=1.2\", \"requests\"]\n"
        '[project.optional-dependencies]\ngpu = ["torch<3"]\ntest = ["pytest-x<4"]\n'
        '[dependency-groups]\ndev = ["pytest>=8,<9", "ruff"]\n'
        '[tool.foo]\ndeps = ["bad<1"]\n')
    cmd = push.venv_rebuild_cmd(tmp_path / ".venv")
    for want in ("'mcp<2'", "'mcp[cli]<=1.9'", "anyio<5", "yarl~=1.2", "pytest-x<4", "'pytest>=8,<9'", " ruff"):
        assert want in cmd, want
    for bad in ("torch", "setuptools", "bad<1", "requests"):
        assert bad not in cmd, bad


def test_venv_rebuild_cmd_hash_in_string_and_include_group(tmp_path):
    from ttp import push
    (tmp_path / "pyproject.toml").write_text(
        '[project] # main\nname = "x"\ndependencies = ["mcp<2"]\n'
        '[dependency-groups] # groups\n'
        'test = ["pytest>=8,<9"]\n'
        'dev = ["pkg @ git+https://host/r.git#subdirectory=py", "pytest>=8,<9", "ruff"]\n'
        'both = [{include-group = "test"}, "ruff"]\n')
    cmd = push.venv_rebuild_cmd(tmp_path / ".venv")
    for want in ("'pytest>=8,<9'", " ruff", "mcp<2"):
        assert want in cmd, want
    assert " , " not in cmd and not cmd.rstrip().endswith(",")
    (tmp_path / "pyproject.toml").write_text(
        '[dependency-groups]\ndev = [{include-group = "test"}, "ruff"]\n')
    cmd = push.venv_rebuild_cmd(tmp_path / ".venv")
    assert " ruff" in cmd and " test" not in cmd


def test_upstream_wontfix_closes_a_note_with_a_reason_and_no_release(env, monkeypatch, capsys):
    from ttp import cli, pushq, upstream
    p = make(env)
    upstream.append("demo", 4, [{"title": "upstream: nb", "spec": "y"}])
    fp = upstream.fingerprint("upstream: nb", "y")
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    assert _upstream_cli(cli, "--resolve", fp, "--wontfix", "") != 0
    assert upstream.state(fp) == "open"
    assert _upstream_cli(cli, "--resolve", fp, "--wontfix", "not a plugin bug") == 0
    assert upstream.state(fp) == "wontfix" and upstream.wontfixed(fp)["note"] == "not a plugin bug"
    assert upstream.resolution(fp) is None and fp in "\n".join(upstream.listing("demo"))
    assert "open" not in " ".join(upstream.listing("demo")).split(fp, 1)[1].split()[:1]
    s = _pq(env, monkeypatch)
    s.p.db.update_task(s.code, result=json.dumps({"status": "done", "resolves": [fp]}))
    n = len(upstream._records())
    pushq._resolve_upstream(s.p, [s.review], "0.3.1")
    assert len(upstream._records()) == n and upstream.resolution(fp) is None


def test_a_note_is_recorded_fixed_from_the_task_given_it_never_from_a_task_that_only_unblocked_it(env, monkeypatch):
    """A review that accepts an earlier task's commits (`inherited`) reads that task's hand-off: a note
    it lists is recorded with its own summary, even when the reviewed task (which only fixed a test in
    the way of its push) lists it too. A note a run closed as by design (won't-fix) is never recorded
    fixed, whichever hand-off lists it, and the listing shows no earlier fixed-in record for it."""
    from ttp import pushq, upstream
    s = _pq(env, monkeypatch)
    notes = [{"title": f"upstream: g{i}", "spec": "s"} for i in range(3)]
    upstream.append("other", 2, notes)
    shared, inherited_only, by_design = (upstream.fingerprint(n["title"], "s") for n in notes)
    orig = s.p.db.add_task("guard change", "s", kind="code", origin="coordinator")
    s.p.db.update_task(orig, status="done", result=json.dumps(
        {"status": "done", "summary": "the guard now allows the probe", "resolves": [shared, inherited_only]}))
    fix = s.p.db.add_task("fix a test in the way of the guard change's push", "s", kind="code", origin="coordinator")
    s.p.db.update_task(fix, status="done", result=json.dumps(
        {"status": "done", "summary": "a test runs on a fake clock", "resolves": [shared, by_design]}))
    s.p.db.update_task(s.review, status="done", labels=json.dumps([f"auto_review:{fix}"]), result=json.dumps(
        {"status": "done", "push": [{"branch": s.branch, "head": s.head, "inherited": [orig]}]}))
    upstream.resolve(by_design, "0.3.0", f"#{fix} an unrelated test fix", "demo")
    run_dir = env["tmp"] / "run-by-design"
    run_dir.mkdir()
    upstream.queue_wontfix(run_dir, by_design, "by design: a read-only probe")
    assert upstream.file_queued_acks(run_dir, "demo") == 1
    assert dict(pushq.resolves_of(s.p, s.review))[shared]["id"] == orig
    pushq._resolve_upstream(s.p, [s.review], "0.3.1")
    for fp in (shared, inherited_only):
        r = upstream.resolution(fp)
        assert r["fixed_in"] == "0.3.1" and r["note"] == f"#{orig} the guard now allows the probe", fp
    assert upstream.state(by_design) == "wontfix" and upstream.resolution(by_design)["fixed_in"] == "0.3.0"
    [line] = [ln for ln in upstream.listing(None) if ln.startswith(by_design)]
    assert "0.3.0" not in line and "unrelated" not in line and "by design: a read-only probe" in line


def test_landing_alert_clears_without_approvals_and_is_not_raised_while_paused(env, monkeypatch):
    from ttp import alerts, pushq
    from ttp.daemon import Daemon
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(pushq.runner, "boot_time", lambda: None)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    _pq_batch(s)
    _pq_tend(s)
    _pq_batch(s)
    _pq_tend(s)
    d = Daemon(s.p.base)
    later = time.time() + 5 * 3600
    s.p.db.set_kv("paused_resources", {"push": {"reason": "maintenance", "since": time.time()}})
    assert not pushq.check_landing(s.p, d.cfg, d.alert, now=later), "paused: the queue is not retrying"
    s.p.db.set_kv("paused_resources", {})
    assert pushq.check_landing(s.p, d.cfg, d.alert, now=later)
    assert alerts.holds(s.p.db, pushq.LANDING_ALERT, time.time() - 1, time.time())
    s.p.db.x("UPDATE push_queue SET status='withdrawn' WHERE status IN ('approved','batched')")
    assert not alerts.holds(s.p.db, pushq.LANDING_ALERT, time.time() - 1, time.time())


def _pq_review_run(s, push=None):
    """A running review run of `s` that handed off done with `push` (default: its branch at its head)."""
    s.p.db.update_task(s.review, status="running")
    rid = s.p.db.x("INSERT INTO runs(task,role,provider,model,started,status) VALUES(?,'worker','codex','',?,"
                   "'running')", (s.review, time.time() - 60))
    run_dir = s.p.runs / str(rid)
    run_dir.mkdir(parents=True)
    (run_dir / "output.jsonl").write_text("")
    (run_dir / "result.json").write_text(json.dumps(
        {"status": "done", "summary": "looks good",
         "push": [{"branch": s.branch, "head": s.head}] if push is None else push}))
    return s.p.db.one("SELECT * FROM runs WHERE id=?", (rid,))


def test_a_review_approved_at_run_end_asks_the_remote_before_the_transaction_never_inside(env, monkeypatch):
    """finish_run records a run's end in one write transaction. The approval's remote lookups (the
    default branch, a pr_branch the remote may lack) are asked before it opens, so the write lock never
    waits on the network (reported: 30 s holds, one REFUSAL_TIMEOUT_S ls-remote each, after a restart
    left the cache cold). Under the guard a git network command inside the transaction raises."""
    from ttp import pushq
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_TX_GUARD", "1")
    s = _pq(env, monkeypatch)
    s.p.db.update_task(s.code, labels=json.dumps(["pr_branch:feature/x"]))   # the remote has no such branch
    pushq._default_branches.clear()
    pushq._absent_seen.clear()
    asked, real = [], pushq._git

    def git(p, *args, **kw):
        if args[0] == "ls-remote":
            asked.append((args[1], p.db.conn.in_transaction))
        return real(p, *args, **kw)
    monkeypatch.setattr(pushq, "_git", git)
    Daemon(s.p.base).finish_run(_pq_review_run(s), {"rc": 0, "started": time.time() - 60, "ended": time.time()})
    assert sorted(asked) == [("--heads", False), ("--symref", False)], asked
    task = s.p.db.task(s.review)
    assert task["status"] == "pushing", (task["status"], task["result"])
    assert "feature/x was ignored" in json.dumps(_pq_events(s.p, task=s.review)), "the prefetched answer was used"


def test_the_remotes_default_branch_answer_survives_a_daemon_restart(env, monkeypatch):
    """The default-branch cache is kept in the project's kv with its expiry: a restarted daemon (empty
    process cache) reads it, also inside a transaction, where nothing asks the remote."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    subprocess.run(["git", "-C", str(s.origin), "symbolic-ref", "HEAD", "refs/heads/proj"], check=True)
    pushq._default_branches.clear()
    assert pushq._default_branch(s.p, "origin") == "proj"
    pushq._default_branches.clear()   # a restart
    monkeypatch.setattr(pushq, "_git", lambda *a, **k: pytest.fail("asked the remote again"))
    with s.p.db.tx():
        assert pushq._default_branch(s.p, "origin") == "proj"
        assert "the remote's default branch" in pushq._refusal(s.p, "origin", "proj")
    assert pushq._default_branch(s.p, "origin") == "proj"
    # Expired, a transaction reads it as unknown (refuses nothing, as an unreachable remote): never asks.
    with s.p.db.tx():
        assert pushq._default_branch(s.p, "origin", time.time() + pushq.DEFAULT_BRANCH_TTL_S + 1) is None
        assert not pushq._absent(s.p, "origin", "feature/x")


def test_upstream_orphan_shipped_notes_are_listed_then_closed_unless_regressed(env):
    from ttp import upstream
    day = 86400
    t0 = time.time() - 40 * day
    upstream.append("demo", 4, [{"title": "upstream: mine", "spec": "a"}])
    mine = upstream.fingerprint("upstream: mine", "a")
    orphan, held, young = "a" * 16, "b" * 16, "c" * 16
    upstream.resolve(mine, "0.3.0", now=t0)
    upstream.resolve(orphan, "0.3.0", now=t0)
    upstream.resolve(held, "0.3.0", now=t0)
    upstream.regress(held, "still broken", now=t0 + day)
    upstream.resolve(young, "0.3.0", now=time.time() - 15 * day)
    now = time.time()
    assert set(upstream.orphans(now)) == {orphan, young}      # the local filer's note is never an orphan
    assert upstream.orphans(now, upstream.ORPHAN_CLOSE_S) == [orphan]
    assert orphan in upstream.orphans_line(now)
    assert upstream.close_unreachable(now) == [orphan]
    assert upstream.state(orphan, now) == "closed" and upstream.acked(orphan)["project"] == upstream.UNREACHABLE
    assert upstream.state(held, now) == upstream.SHIPPED and upstream.state(mine, now) == upstream.SHIPPED
    assert upstream.close_unreachable(now) == []


def test_upstream_regress_queue_stops_the_close_and_orphans_reach_the_daily_review(env):
    from ttp import daemon as dm, upstream
    day = 86400
    t0 = time.time() - 40 * day
    held, orphan = "b" * 16, "a" * 16
    upstream.resolve(held, "0.3.0", now=t0)
    upstream.resolve(orphan, "0.3.0", now=t0)
    p = make(env)
    with pytest.raises(ValueError):
        upstream.regress("d" * 16, "x")
    run = p.base / "runs-demo"
    run.mkdir()
    upstream.queue_regress(run, held, "still broken")
    assert upstream.file_queued_acks(run, "demo") == 1
    assert upstream.close_unreachable() == [orphan]
    assert upstream.state(held, time.time()) == upstream.SHIPPED
    # The daily review lists orphans only for an ingesting project.
    upstream.resolve("c" * 16, "0.3.0", now=time.time() - 20 * day)
    for ingest in (True, False):
        p.db.x("DELETE FROM tasks")
        d = dm.Daemon(p.base)
        d.cfg = {**d.cfg, "upstream": {"ingest": ingest}}
        d._schedule_llm({"name": "daily-review", "budget_usd_day": None, "last_run": None, "description": "review"}, {})
        spec = p.db.one("SELECT spec FROM tasks WHERE origin='schedule'")["spec"]
        assert ("c" * 16 in spec) is ingest


def _claim_setup(env, monkeypatch, tmp_path, on_pass):
    """_push_setup plus a code task waiting on its own passed checks with `on_pass`, its head not on origin."""
    from ttp import daemon as dmod
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    tid, path = _checks_wait_task(p, tmp_path, None)
    head = _git_out(path, "rev-parse", "HEAD")
    hand = {**json.loads((tmp_path / f"run-{tid}" / "result.json").read_text()),
            "on_pass": {k: (v.format(head=head[:10]) if isinstance(v, str) else v) for k, v in on_pass.items()}}
    (tmp_path / f"run-{tid}" / "result.json").write_text(json.dumps(hand))
    p.db.update_task(tid, result=json.dumps({**hand, "waiting_since": time.time() - 60}))
    return p, dmod.Daemon(p.base), tid, path, head, origin


def test_a_checks_pass_done_claiming_a_push_the_remote_lacks_fails_and_never_records_done(env, monkeypatch, tmp_path):
    """Replay: checks pass, the on_pass done says 'pushed <sha> to <branch>' though nothing was published.
    The task waits in `pushing` until the remote is asked, then fails naming remote, ref and sha."""
    p, d, tid, path, head, _ = _claim_setup(env, monkeypatch, tmp_path,
                                            {"status": "done", "summary": "pushed {head} to origin/proj"})
    _settle_probe(d, tid)
    task = p.db.task(tid)
    assert task["status"] == "pushing", (task["status"], task["result"])
    assert "verifying its claimed push" in task["blocked_reason"]
    assert (tmp_path / f"run-{tid}" / "push_claim_handoff.json").is_file()
    assert not p.db.q("SELECT id FROM events WHERE kind='task_done' AND task=?", (tid,))
    d.verify_pushes()
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert task["status"] != "done" and res["status"] == "failed", (task["status"], res)
    assert "claimed a push that is not on the remote" in res["summary"]
    assert f"{head[:12]} is not on origin/proj" in res["summary"]   # remote, ref and sha
    assert not p.db.q("SELECT id FROM events WHERE kind='task_done' AND task=?", (tid,))


def test_a_checks_pass_done_whose_claimed_push_is_on_the_remote_is_done_with_the_proof(env, monkeypatch, tmp_path):
    p, d, tid, path, head, _ = _claim_setup(env, monkeypatch, tmp_path,
                                            {"status": "done", "summary": "pushed {head} to origin/proj"})
    _git_out(path, "push", "-q", "origin", "HEAD:refs/heads/proj")
    _settle_probe(d, tid)
    assert p.db.task(tid)["status"] == "pushing"
    d.verify_pushes()
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert task["status"] == "done", (task["status"], res)
    assert res["summary"].startswith(f"pushed {head[:10]} to origin/proj (push verified: ")
    assert f"{head[:12]} is the tip of origin/proj" in res["summary"]
    assert p.db.q("SELECT id FROM events WHERE kind='task_done' AND task=?", (tid,))
    assert "push_verify" not in task["result"]


def test_a_done_hand_off_that_claims_no_push_goes_straight_to_done(env, monkeypatch, tmp_path):
    p, d, tid, *_ = _claim_setup(env, monkeypatch, tmp_path,
                                 {"status": "done", "summary": "parser fixed; not pushed {head} yet"})
    _settle_probe(d, tid)
    task = p.db.task(tid)
    assert task["status"] == "done", (task["status"], task["result"])
    assert "push_verify" not in task["result"]


_SPLIT = {"status": "done", "summary": "split at the context line: the fix is local commit {head} in the "
          "worktree, still unpublished; a push runs checks first and they would fail on the tests left, so "
          "I stopped my own queued detached push (its pid); the continue task delivers the work",
          "followups": [{"title": "continue: finish the tests and deliver the fix", "spec": "branch and head..."}]}


def _own_push_marker(p, tid, head, **ended):
    """A detached push marker of task `tid`'s run for `head` on origin/proj, ended as `ended` says."""
    run = p.db.one("SELECT id FROM runs WHERE task=? ORDER BY id DESC", (tid,))["id"]
    pushes = p.state / "pushes"
    pushes.mkdir(parents=True, exist_ok=True)
    (pushes / f"t{tid}-wip.json").write_text(json.dumps({"run": run, "head": head, "target": "origin/proj",
                                                         "pid": 4242, "status": "failed", **ended}))


@pytest.mark.parametrize("ended, claims_it", [
    ({"exit": 1, "reason": "stopped by SIGTERM"}, False),   # the replay: stopped by its own run
    ({"exit": 4}, True),                                     # its checks failed: it ended on its own
    ({"exit": None}, True),                                  # killed with no outcome written
])
def test_a_split_hand_off_that_stopped_its_own_unpublished_push_keeps_its_outcome(
        env, monkeypatch, tmp_path, ended, claims_it):
    """Replay: a run splits at the context line, its work unpublished, and stops its own queued detached
    push (SIGTERM from its pid). Its push is no claim: no 'claimed a push' failure, no alert, done with
    its continue follow-up. A push that ended any other way stays a claim and fails as before."""
    p, d, tid, path, head, _ = _claim_setup(env, monkeypatch, tmp_path, _SPLIT)
    _own_push_marker(p, tid, head, **ended)
    _settle_probe(d, tid)
    d.verify_pushes()
    task = p.db.task(tid)
    res = json.loads(task["result"])
    if claims_it:
        assert task["status"] != "done" and res["status"] == "failed", (task["status"], res)
        assert f"{head[:12]} is not on origin/proj" in res["summary"]
        return
    assert task["status"] == "done" and res["status"] == "done", (task["status"], res)
    assert "push_verify" not in task["result"] and "claimed a push" not in task["result"]
    assert res["followups"][0]["title"].startswith("continue:")
    assert not p.db.q("SELECT id FROM events WHERE task=? AND kind IN ('push_verifying','task_failed')", (tid,))
    assert not p.db.q("SELECT id FROM events WHERE task=? AND severity IN ('high','critical')", (tid,))


@pytest.mark.parametrize("hand", [
    {"summary": "pushed {head} to origin/proj; continuing the tests"},          # its summary claims it
    {"summary": "fix committed", "pushed": [{"sha": "{head}", "branch": "proj"}]},  # its pushed list does
    {"summary": "fix committed and unpublished", "followups": []},              # no split
])
def test_a_hand_off_that_claims_a_push_or_does_not_split_still_fails_on_a_stopped_push(
        env, monkeypatch, tmp_path, hand):
    p, d, tid, path, head, _ = _claim_setup(env, monkeypatch, tmp_path, {**_SPLIT, **hand})
    if "pushed" in hand:   # _claim_setup fills in only the on_pass strings
        p.db.update_task(tid, result=p.db.task(tid)["result"].replace("{head}", head))
        rj = tmp_path / f"run-{tid}" / "result.json"
        rj.write_text(rj.read_text().replace("{head}", head))
    _own_push_marker(p, tid, head, exit=1, reason="stopped by SIGTERM")
    _settle_probe(d, tid)
    d.verify_pushes()
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert task["status"] != "done" and res["status"] == "failed", (task["status"], res)
    assert "claimed a push that is not on the remote" in res["summary"] and head[:12] in res["summary"]


def _no_remote_claim_setup(env, monkeypatch, tmp_path, on_branch):
    """A code repo with no git remote; a task waiting on its passed checks whose run's detached
    `push --own` (remote 'local') pushed its head to local branch `proj`, which holds that head
    only when `on_branch`."""
    from ttp import daemon as dmod
    for var, val in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                     ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(var, val)
    p = make(env)
    assert not _git_out(p.root, "remote")
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    tid, path = _checks_wait_task(p, tmp_path, DONE_ON_PASS)
    head = _git_out(path, "rev-parse", "HEAD")
    _git_out(p.root, "branch", "-f", "proj", head if on_branch else f"{head}~1")
    run = p.db.one("SELECT id FROM runs WHERE task=? ORDER BY id DESC", (tid,))["id"]
    pushes = p.state / "pushes"
    pushes.mkdir(parents=True, exist_ok=True)
    (pushes / f"t{tid}-own.json").write_text(json.dumps({
        "run": run, "repo": str(path), "head": head, "sha": head, "remote": "local", "branch": "proj",
        "target": "local/proj", "own": True, "status": "pushed", "exit": 0}))
    return p, dmod.Daemon(p.base), tid, head


@pytest.mark.parametrize("on_branch", [True, False])
def test_a_detached_own_push_in_a_repo_with_no_remote_is_checked_on_the_local_branch(
        env, monkeypatch, tmp_path, on_branch):
    """Replay: in a code repo with no git remote, `push --own` fast-forwards the local branch and its
    marker names remote 'local'. Its claim is checked there: done when the branch holds the sha, no
    'could not be verified on the remote' failure; failed naming the local branch and sha when not."""
    p, d, tid, head = _no_remote_claim_setup(env, monkeypatch, tmp_path, on_branch)
    _settle_probe(d, tid)
    assert p.db.task(tid)["status"] == "pushing"
    d.verify_pushes()
    task = p.db.task(tid)
    res = json.loads(task["result"])
    if not on_branch:
        assert task["status"] != "done" and res["status"] == "failed", (task["status"], res)
        assert "claimed a push that is not on the remote" in res["summary"]
        assert f"{head[:12]} is not on local branch proj" in res["summary"], res["summary"]
        return
    assert task["status"] == "done" and res["status"] == "done", (task["status"], res)
    assert f"{head[:12]} is the tip of local branch proj" in res["summary"], res["summary"]
    assert "claimed a push" not in task["result"] and "push_verify" not in task["result"]
    assert not p.db.q("SELECT id FROM events WHERE task=? AND (kind='task_failed' OR (kind='push_verifying' "
                      "AND severity!='low'))", (tid,))
    assert not p.db.q("SELECT id FROM events WHERE task=? AND severity IN ('high','critical')", (tid,))


def test_the_push_audit_confirms_an_own_push_on_the_local_branch_of_a_repo_with_no_remote(
        env, monkeypatch, tmp_path):
    from ttp import pushclaim
    p, d, tid, head = _no_remote_claim_setup(env, monkeypatch, tmp_path, True)
    _git_out(p.root, "checkout", "-q", "proj")
    _commit(p.root, "later.txt", "later\n")   # a later commit on the branch keeps it confirmed
    p.db.update_task(tid, status="done", result=json.dumps({"status": "done", "summary": "parser fixed"}))
    a = pushclaim.audit(p)
    assert a["gaps"] == [] and a["counts"]["confirmed"] == 1, a


def test_an_unreadable_remote_keeps_a_claimed_push_waiting_with_backoff_and_fails_it_past_the_bound(
        env, monkeypatch, tmp_path):
    from ttp import daemon as dmod, pushclaim
    p, d, tid, path, head, origin = _claim_setup(env, monkeypatch, tmp_path,
                                                 {"status": "done", "summary": "pushed {head} to origin/proj"})
    _settle_probe(d, tid)
    assert p.db.task(tid)["status"] == "pushing"
    _git_out(p.root, "remote", "set-url", "origin", str(tmp_path / "no-such-remote.git"))
    clock = [time.time()]
    fake = types.SimpleNamespace(**{k: getattr(time, k) for k in dir(time) if not k.startswith("_")})
    fake.time = lambda: clock[0]
    monkeypatch.setattr(dmod, "time", fake)

    def pv():
        t = p.db.task(tid)
        return t["status"], json.loads(t["result"]).get(pushclaim.KEY) or {}
    since = pv()[1]["since"]
    for tries in (1, 2, 3):
        d.verify_pushes()
        status, v = pv()
        assert status == "pushing" and v["tries"] == tries, (status, v)
        assert v["next"] == pytest.approx(clock[0] + pushclaim.delay_s(tries))
        assert "git ls-remote origin failed" in v["last"]
        clock[0] += 1
        d.verify_pushes()   # not due yet: untouched
        assert pv()[1]["tries"] == tries
        clock[0] = v["next"]
    clock[0] = since + pushclaim.BOUND_S + 1
    d.verify_pushes()
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert task["status"] != "done" and res["status"] == "failed", (task["status"], res)
    assert "could not be read for 6 h" in res["summary"] and head[:12] in res["summary"]


def test_verify_confirms_a_commit_below_the_tip_after_a_fetch_and_a_tag_elsewhere_is_missing(env, monkeypatch):
    from ttp import pushclaim
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    below = _git_out(repo, "rev-parse", "HEAD")
    _commit(repo, "b.txt", "b\n")
    tip = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    _git_out(repo, "tag", "v1", tip)
    _git_out(repo, "push", "-q", "origin", "refs/tags/v1")
    _git_out(repo, "update-ref", "-d", "refs/remotes/origin/proj")
    assert pushclaim.verify(repo, "origin", "refs/heads/proj", tip)[0] == pushclaim.CONFIRMED
    state, why = pushclaim.verify(repo, "origin", "refs/heads/proj", below)
    assert state == pushclaim.CONFIRMED and why == f"{below[:12]} is on origin/proj", why
    assert _git_out(repo, "rev-parse", "refs/remotes/origin/proj") == tip   # the fetch it needed
    state, why = pushclaim.verify(repo, "origin", "refs/tags/v1", below)
    assert state == pushclaim.MISSING and tip[:12] in why, why
    assert pushclaim.verify(repo, "origin", "refs/tags/v1", tip)[0] == pushclaim.CONFIRMED
    assert pushclaim.verify(repo, "origin", "refs/heads/gone", tip)[0] == pushclaim.MISSING
    _commit(repo, "c.txt", "c\n")
    state, why = pushclaim.verify(repo, "origin", "refs/heads/proj", _git_out(repo, "rev-parse", "HEAD"))
    assert state == pushclaim.MISSING and "is not on origin/proj" in why, why


def _split_push_url(env, monkeypatch):
    """_push_setup's repo, whose origin fetches from `origin.git` but pushes to a second bare repo
    (remote.origin.pushurl, as a fork is set up), given as a file:// URL."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    fork = _bare_init(env, env["tmp"] / "fork.git")
    _git_out(repo, "remote", "set-url", "--push", "origin", fork.as_uri())
    return p, repo, origin, fork


def test_own_push_existing_only_asks_the_push_url_whether_the_branch_exists(env, monkeypatch, capsys):
    """origin's push URL can differ from its fetch URL (fetch from an upstream, push to a fork). The
    existing_only gate of `ttp push --own` asks where `git push origin` goes: a branch only the push URL
    has is published onto, one only the fetch URL has is refused, and with several push URLs (git
    pushes to each) every one must have it. A remote with one URL is asked as before."""
    from ttp import push
    p, repo, origin, fork = _split_push_url(env, monkeypatch)
    p.set_config("delivery.own_push", "existing_only")
    _git_out(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "f.txt", "f\n")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/feature")   # goes to the push URL only
    assert _git_out(fork, "rev-parse", "feature") == _git_out(repo, "rev-parse", "HEAD")
    assert "feature" not in _git_out(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert push.push_sides(repo, "origin") == [fork.as_uri()]
    push.own_gate(p, repo, "origin", "feature")   # (a) only on the push URL: accepted
    _commit(repo, "g.txt", "g\n")
    capsys.readouterr()
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert _git_out(fork, "rev-parse", "feature") == _git_out(repo, "rev-parse", "HEAD")
    # (b) only on the fetch URL: refused, and nothing is created on the push URL
    _git_out(origin, "branch", "upstream-only", "proj")
    with pytest.raises(ValueError, match="origin has no branch upstream-only"):
        push.own_gate(p, repo, "origin", "upstream-only")
    assert "upstream-only" not in _git_out(fork, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    # several push URLs: git pushes to each, so each must have the branch
    second = _bare_init(env, env["tmp"] / "second.git")
    _git_out(repo, "remote", "set-url", "--add", "--push", "origin", second.as_uri())
    assert push.push_sides(repo, "origin") == [fork.as_uri(), second.as_uri()]
    with pytest.raises(ValueError, match="origin has no branch feature"):
        push.own_gate(p, repo, "origin", "feature")
    _git_out(repo, "push", "-q", second.as_uri(), "HEAD:refs/heads/feature")
    push.own_gate(p, repo, "origin", "feature")
    # (c) one URL: asked by the remote's name, as before
    _git_out(repo, "config", "--unset-all", "remote.origin.pushurl")
    assert push.push_sides(repo, "origin") == ["origin"]
    push.own_gate(p, repo, "origin", "upstream-only")
    with pytest.raises(ValueError, match="origin has no branch feature"):
        push.own_gate(p, repo, "origin", "feature")


def test_hand_off_push_checks_look_for_a_pushed_commit_on_the_push_url(env, monkeypatch):
    """The checks that a hand-off's pushed commit is on the remote (pushclaim.verify) and whether a
    proposed branch is published (the hand-off lint's lookup) ask origin's push URL, where the push
    went, not its fetch URL; a commit only the fetch URL has is missing; a one-URL remote is
    asked as before, its tracking ref included."""
    from ttp import handofflint, push, pushclaim
    p, repo, origin, fork = _split_push_url(env, monkeypatch)
    _git_out(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "a.txt", "a\n")
    below = _git_out(repo, "rev-parse", "HEAD")
    _commit(repo, "b.txt", "b\n")
    tip = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/feature")   # to the push URL
    # (a) on the push URL only: confirmed, as the tip and below it after a fetch
    assert pushclaim.verify(repo, "origin", "refs/heads/feature", tip)[0] == pushclaim.CONFIRMED
    state, why = pushclaim.verify(repo, "origin", "refs/heads/feature", below)
    assert state == pushclaim.CONFIRMED, why
    assert handofflint._ask_remote(p, repo, "feature") is True
    assert _git_out(repo, "rev-parse", push.tracking_ref(fork.as_uri(), "feature")) == tip   # fetched from there
    # (b) on the fetch URL only: missing
    _git_out(repo, "push", "-q", origin.as_uri(), "HEAD:refs/heads/upstream-only")
    assert pushclaim.verify(repo, "origin", "refs/heads/upstream-only", tip)[0] == pushclaim.MISSING
    assert handofflint._ask_remote(p, repo, "upstream-only") is False
    # (c) one URL: the fetch URL, as before
    _git_out(repo, "config", "--unset-all", "remote.origin.pushurl")
    assert pushclaim.verify(repo, "origin", "refs/heads/upstream-only", below, {})[0] == pushclaim.CONFIRMED
    assert _git_out(repo, "rev-parse", "refs/remotes/origin/upstream-only") == tip
    assert pushclaim.verify(repo, "origin", "refs/heads/feature", tip, {})[0] == pushclaim.MISSING
    assert handofflint._ask_remote(p, repo, "upstream-only") is True
    assert handofflint._ask_remote(p, repo, "feature") is False


def test_detached_push_markers_are_unknown_while_running_confirmed_by_sha_and_missing_once_failed(
        env, monkeypatch):
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    on = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    _commit(repo, "b.txt", "b\n")
    off = _git_out(repo, "rev-parse", "HEAD")
    tid = p.db.add_task("t", "s", kind="code", origin="user")
    pushes = p.state / "pushes"
    pushes.mkdir(parents=True, exist_ok=True)

    def state(marker):
        (pushes / f"t{tid}-x.json").write_text(json.dumps({"run": 7, "target": "origin/proj", **marker}))
        cs = pushclaim.marker_claims(p, tid, 7)
        assert len(cs) == 1, cs
        return pushclaim.check_all(p, cs)[:2]
    assert state({"status": "queued"})[0] == pushclaim.UNKNOWN
    assert state({"status": "running", "head": on})[0] == pushclaim.UNKNOWN
    assert state({"status": "pushed", "sha": on})[0] == pushclaim.CONFIRMED
    got, why = state({"status": "pushed", "sha": off})
    assert got == pushclaim.MISSING, why
    got, why = state({"status": "failed", "head": off, "exit": 4})
    assert got == pushclaim.MISSING and "ended failed (exit 4)" in why and off[:12] in why, why
    assert state({"status": "failed", "head": on})[0] == pushclaim.CONFIRMED   # pushed another way
    # Another run's finished marker is not this run's claim.
    (pushes / f"t{tid}-x.json").write_text(json.dumps({"run": 8, "status": "failed", "target": "origin/proj"}))
    assert pushclaim.marker_claims(p, tid, 7) == []


def test_a_detached_push_claim_whose_task_worktree_is_gone_is_checked_in_the_project_root(env, monkeypatch, tmp_path):
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    on = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    tid = p.db.add_task("t", "s", kind="code", origin="user")
    pushes = p.state / "pushes"
    pushes.mkdir(parents=True, exist_ok=True)
    pruned = str(tmp_path / "worktrees" / "gone")   # the task's worktree, removed since

    def state(marker):
        (pushes / f"t{tid}-x.json").write_text(json.dumps({"run": 7, "repo": pruned, **marker}))
        return pushclaim.check_all(p, pushclaim.marker_claims(p, tid, 7))[:2]
    got, why = state({"status": "pushed", "sha": on, "target": "origin/proj"})
    assert got == pushclaim.CONFIRMED, why
    # The remote and branch the push recorded win over its target text.
    got, why = state({"status": "pushed", "sha": on, "target": "nowhere/else", "remote": "origin", "branch": "proj"})
    assert got == pushclaim.CONFIRMED, why
    got, why = state({"status": "pushed", "sha": on, "remote": "nowhere", "branch": "x"})
    assert got == pushclaim.UNVERIFIABLE and "names no remote branch" in why, why


def test_a_detached_push_claim_the_remote_cannot_settle_is_unverifiable_and_ranked_below_missing(env, monkeypatch):
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    on = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    _commit(repo, "b.txt", "b\n")
    off = _git_out(repo, "rev-parse", "HEAD")
    tid = p.db.add_task("t", "s", kind="code", origin="user", branch="work")
    pushes = p.state / "pushes"
    pushes.mkdir(parents=True, exist_ok=True)

    def write(name, marker):
        (pushes / f"t{tid}-{name}.json").write_text(json.dumps({"run": 7, "repo": str(repo), **marker}))
    write("x", {"status": "failed", "head": off})                                 # no target
    assert pushclaim.check_all(p, pushclaim.marker_claims(p, tid, 7))[0] == pushclaim.UNVERIFIABLE
    write("x", {"status": "failed", "head": off, "target": "origin/proj", "exit": None})   # a reboot
    assert pushclaim.check_all(p, pushclaim.marker_claims(p, tid, 7))[0] == pushclaim.UNVERIFIABLE
    write("x", {"status": "failed", "head": on, "target": "origin/proj", "exit": None})    # pushed another way
    assert pushclaim.check_all(p, pushclaim.marker_claims(p, tid, 7))[0] == pushclaim.CONFIRMED
    write("x", {"status": "failed", "head": off, "target": "origin/proj", "exit": 4})
    assert pushclaim.check_all(p, pushclaim.marker_claims(p, tid, 7))[0] == pushclaim.MISSING
    # MISSING > UNVERIFIABLE > UNKNOWN.
    write("y", {"status": "failed", "head": off, "target": "origin/proj", "exit": None})
    assert pushclaim.check_all(p, pushclaim.marker_claims(p, tid, 7))[0] == pushclaim.MISSING
    (pushes / f"t{tid}-x.json").unlink()
    write("z", {"status": "running", "head": off, "target": "origin/proj"})
    state, why, each = pushclaim.check_all(p, pushclaim.marker_claims(p, tid, 7))
    assert state == pushclaim.UNVERIFIABLE and sorted(s for _, s, _ in each) == ["unknown", "unverifiable"], each
    # The audit counts it apart from missing, and raises no gap issue for it.
    (pushes / f"t{tid}-z.json").unlink()
    run = p.db.x("INSERT INTO runs(task,role,status) VALUES(?,?,?)", (tid, "worker", "ok"))
    write("y", {"run": run, "status": "failed", "head": off, "target": "origin/proj", "exit": None})
    p.db.update_task(tid, status="done", result=json.dumps({"status": "done", "summary": "handed off"}))
    a = pushclaim.audit(p)
    assert a["counts"] == {"confirmed": 0, "missing": 0, "unknown": 0, "unverifiable": 1}, a
    assert [g["state"] for g in a["gaps"]] == ["unverifiable"]
    assert pushclaim.summary_line(a).startswith("0 confirmed, 0 missing, 0 unknown across 1 done tasks")
    assert "; 1 unverifiable" in pushclaim.summary_line(a)


def test_a_push_queue_landing_is_checked_by_the_commit_it_landed_as(env, monkeypatch):
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    before_rebase = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "reset", "-q", "--hard", "HEAD~1")
    _commit(repo, "a2.txt", "a\n")
    landed = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    tid = p.db.add_task("t", "s", kind="code", origin="user")
    now = time.time()
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,pushed_sha,landed_sha) "
           "VALUES(?,?,?,?,?,?,?,?,?)", (tid, "b", before_rebase, "origin/proj", "landed", now, now,
                                         before_rebase, landed))
    cs = pushclaim.queue_claims(p, tid)
    assert [c["sha"] for c in cs] == [landed] and cs[0]["ref"] == "refs/heads/proj"
    assert pushclaim.check_all(p, cs)[0] == pushclaim.CONFIRMED
    p.db.x("UPDATE push_queue SET landed_sha=NULL WHERE task=?", (tid,))
    assert pushclaim.check_all(p, pushclaim.queue_claims(p, tid))[0] == pushclaim.MISSING


def test_text_claims_reads_push_phrases_and_skips_negated_ones_and_unknown_commits(env, monkeypatch):
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    sha = _git_out(repo, "rev-parse", "HEAD")
    s = sha[:10]
    one = [("origin", "refs/heads/proj", sha)]
    pick = lambda text: [(c["remote"], c["ref"], c["sha"]) for c in pushclaim.text_claims(p, text)]
    assert pick(f"pushed {s} to origin/proj") == one
    assert pick(f"Landed as `{s}` on origin/proj.") == one
    assert pick(f"pushed to origin/proj as {s}") == one
    assert pick(f"done; pushed {s}.") == one                  # no ref: the push branch
    assert pick(f"not pushed {s} to origin/proj") == []
    assert pick(f"it was never pushed {s}") == []
    assert pick(f"not yet pushed {s} to origin/proj") == []
    assert pick(f"it hasn't been pushed {s} to origin/proj") == []
    assert pick(f"didn't get pushed {s} to origin/proj") == []
    assert pick(f"{s} will be pushed to origin/proj by the review") == []
    assert pick("pushed deadbee1234 to origin/proj") == []    # no such commit here
    assert pick(f"pushed {s} to the branch") == one           # no real ref named: the push branch
    assert pick(f"pushed {s} to nowhere/else") == [("origin", "refs/heads/nowhere/else", sha)]   # a branch of origin
    _git_out(repo, "tag", "v9", sha)
    assert pick(f"pushed tag v9 at {s}") == [("origin", "refs/tags/v9", sha)]


def test_the_hourly_push_audit_raises_one_issue_per_missing_push_and_closes_them_once_fixed(env, monkeypatch, capsys):
    from ttp import daemon as dmod, pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    sha = _git_out(repo, "rev-parse", "HEAD")
    tid = p.db.add_task("ship it", "s", kind="code", origin="user", branch="work")
    p.db.update_task(tid, status="done", result=json.dumps({"status": "done", "summary": f"pushed {sha[:10]} to origin/proj"}))
    quiet = p.db.add_task("plain", "s", kind="code", origin="user", branch="work")
    p.db.update_task(quiet, status="done", result=json.dumps({"status": "done", "summary": "no push here"}))
    d = dmod.Daemon(p.base)
    open_ = lambda: p.db.q("SELECT watch_key, title FROM issues WHERE source='daemon:push-audit' AND status='open'")
    d.audit_pushes()
    got = open_()
    assert len(got) == 1 and got[0]["watch_key"] == f"push-gap:{tid}:{sha[:12]}" and f"#{tid} ship it" in got[0]["title"]
    assert pushclaim.daily_line(p.db).startswith("0 confirmed, 1 missing, 0 unknown across 2 done tasks")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    d.audit_pushes()                                  # within the hour: not run again
    assert len(open_()) == 1
    p.db.set_kv(pushclaim.AUDIT_KV, {**p.db.kv(pushclaim.AUDIT_KV), "at": time.time() - pushclaim.AUDIT_EVERY_S - 1})
    d.audit_pushes()
    assert open_() == []
    assert pushclaim.daily_line(p.db).startswith("1 confirmed, 0 missing, 0 unknown")
    from ttp import cli
    capsys.readouterr()
    with contextlib.suppress(SystemExit):
        cli.main(["push-audit", "demo", "--json"])
    assert json.loads(capsys.readouterr().out)["counts"] == {"confirmed": 1, "missing": 0, "unknown": 0}


def test_the_hourly_push_audit_closes_an_ask_only_once_every_task_it_names_landed(env, monkeypatch):
    """An ask for a task closes as 'resolved: its work landed' once the push audit confirms the task's
    push on the remote; an ask for a task whose push is missing, one naming a task only as context
    (done before it was asked) and one naming no task stay open."""
    from ttp import audit, daemon as dmod, pushclaim, unblock
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    now = time.time()
    _commit(repo, "a.txt", "a\n")
    sha = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    _commit(repo, "b.txt", "b\n")
    unpushed = _git_out(repo, "rev-parse", "HEAD")

    def ask(text, ago=3600):
        return p.db.x("INSERT INTO messages(ts,direction,kind,text,ref) VALUES(?,?,?,?,?)",
                      (now - ago, "out", "ask", text, "blocking:access"))
    asks = {}
    asks["landed"] = ask("The token needs the workflow scope before #{landed}'s CI job can be pushed.")
    asks["missing"] = ask("Grant the scope so #{missing} can push?")
    asks["both"] = ask("Unblock #{landed} and #{missing}?")
    asks["unlinked"] = ask("Grant the gh token the workflow scope?")
    asks["context"] = ask("Like #{old} (done before this ask), may #{old} change again?")
    asks["ask_ref"] = ask("Same question as ask #{landed}?")

    def done(title, summary):
        tid = p.db.add_task(title, "s", kind="code", origin="user", branch="work")
        p.db.update_task(tid, status="done", result=json.dumps({"status": "done", "summary": summary}))
        return tid
    ids = {"landed": done("ci job", f"pushed {sha[:10]} to origin/proj"),
           "missing": done("other", f"pushed {unpushed[:10]} to origin/proj"),
           "old": done("earlier", f"pushed {sha[:10]} to origin/proj")}
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (now - 7200, ids["old"]))
    for k, aid in asks.items():
        text = p.db.one("SELECT text FROM messages WHERE id=?", (aid,))["text"].format(**ids)
        p.db.x("UPDATE messages SET text=? WHERE id=?", (text, aid))
    dmod.Daemon(p.base).audit_pushes()
    assert set(pushclaim.audit(p)["landed"]) == {ids["landed"], ids["old"]}
    open_ = {r["id"] for r in p.db.q("SELECT id FROM messages WHERE kind='ask' AND handled=0")}
    assert asks["landed"] not in open_
    assert open_ == {asks[k] for k in ("missing", "both", "unlinked", "context", "ask_ref")}
    rows = {r["id"]: r for r in unblock.asks(p.db, now - 86400)}
    assert rows[asks["landed"]]["landed"]["tasks"] == [ids["landed"]] and not rows[asks["landed"]]["answered"]
    assert rows[asks["missing"]]["landed"] is None
    out = "\n".join(audit.lines(p.db))
    assert f"ask {asks['landed']} (access): " in out
    assert f"resolved: its work landed (#{ids['landed']}), after 1.0 h with no answer" in out
    assert f"ask {asks['unlinked']} (access): \"Grant the gh token the workflow scope?\"; no answer yet" in out
    assert f"ask {asks['missing']} (access): \"Grant the scope so #{ids['missing']} can push?\"; no answer yet" in out
    assert "asks: 6 sent, 0 answered, 0 handed back, 1 resolved as their work landed" in out
    assert unblock.close_landed(p.db, [ids["landed"], ids["old"]]) == [], "closed once"


def test_close_landed_never_closes_an_ask_without_a_confirmed_landed_task(env):
    from ttp import unblock
    p, now, msg = _ask_db(env)
    tid = p.db.add_task("work", "s", origin="user")
    a = msg("out", f"May #{tid} go ahead?", 600, "ask", ref="blocking:access")
    b = msg("out", "Anything else?", 600, "ask", ref="blocking:access")
    p.db.update_task(tid, status="done")
    assert unblock.close_landed(p.db, []) == [], "no confirmed landing: nothing closes"
    held = p.db.add_task("held", "s", origin="user")
    p.db.update_task(held, status="blocked", labels=[f"waits:ask:{b}"])
    assert unblock.ask_tasks(p.db, {"id": b, "text": "Anything else?"}) == [held], "an anchored task is linked"
    assert unblock.close_landed(p.db, [held]) == [], "a task not done keeps its ask open"
    assert unblock.close_landed(p.db, [tid]) == [a]


def test_an_ask_closes_when_the_push_queue_lands_the_named_task_through_its_review(env, monkeypatch):
    """In a push-queue project the queue row belongs to the review that approved the head and the code
    task claims no push: the ask naming only the code task closes once the review's landing is on the
    remote, even though the code task finished before the ask (a push-time ask). A task whose review
    landed before the ask (named as context) and one whose approval has not landed keep theirs open."""
    from ttp import audit, daemon as dmod, pushclaim, unblock
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    now = time.time()
    _commit(repo, "a.txt", "a\n")
    sha = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")

    def code(title, done_ago, landed_ago, status="landed"):
        tid = p.db.add_task(title, "s", kind="code", origin="user", branch=f"ttp/t-{title}")
        p.db.update_task(tid, status="done", result=json.dumps({"status": "done", "summary": f"Commits {sha[:8]} on ttp/t-{title}"}))
        p.db.x("UPDATE tasks SET updated=? WHERE id=?", (now - done_ago, tid))
        rev = p.db.add_task(f"Review #{tid}: {title}", "r", kind="review", origin="coordinator")
        p.db.update_task(rev, status="done", result=json.dumps({"status": "done", "summary": "approved"}))
        p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated, pushed_sha, landed_sha) "
               "VALUES(?,?,?,'origin/proj',?,?,?,?,?)", (rev, f"ttp/t-{title}", sha, status, now - landed_ago - 60,
                                                         now - landed_ago, sha if status == "landed" else None,
                                                         sha if status == "landed" else None))
        return tid, rev

    def ask(text, ago=3600):
        return p.db.x("INSERT INTO messages(ts,direction,kind,text,ref) VALUES(?,?,?,?,?)",
                      (now - ago, "out", "ask", text, "blocking:access"))
    ci, ci_rev = code("ci", 7200, 600)
    early, early_rev = code("early", 9000, 7200)
    queued, _ = code("queued", 7200, 600, status="approved")
    a_ci = ask(f"The token needs the workflow scope before #{ci}'s CI job can be pushed.")
    a_early = ask(f"Like #{early}, may this change too?")
    a_queued = ask(f"Grant the scope so #{queued} can push?")
    assert ci not in pushclaim.audit(p)["landed"] and ci_rev in pushclaim.audit(p)["landed"]
    dmod.Daemon(p.base).audit_pushes()
    open_ = {r["id"] for r in p.db.q("SELECT id FROM messages WHERE kind='ask' AND handled=0")}
    assert a_ci not in open_ and {a_early, a_queued} <= open_
    rows = {r["id"]: r for r in unblock.asks(p.db, now - 86400)}
    assert rows[a_ci]["landed"]["tasks"] == [ci]
    assert f"resolved: its work landed (#{ci})" in "\n".join(audit.lines(p.db))
    assert unblock.landed_at(p.db, ci_rev) == pytest.approx(now - 600)
    assert unblock.landed_at(p.db, early_rev) == pytest.approx(now - 7200)


def test_review_rules_do_not_tell_reviews_to_publish_another_tasks_branch():
    """The review template never invites --own onto a branch another task carries (the push refuses it)."""
    import re
    text = (RUNTIME.parent / "template" / "prompts" / "kind-review.md").read_text()
    assert "the change carries" not in text
    assert re.search(r"Never publish another task's `ttp/t<id>-\.\.\.` branch", text)


def _slow_claims(env, monkeypatch, n, sleep_s=0.1):
    """`n` tasks in `pushing`, each claiming the project's head on a branch of its own, with a
    pushclaim._git whose ls-remote sleeps `sleep_s` and answers that head; `calls` lists them."""
    from ttp import daemon as dmod, pushclaim
    from ttp.db import dump_result
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    sha = _git_out(repo, "rev-parse", "HEAD")
    calls, real = [], pushclaim._git

    def git(r, *args, timeout=30):
        if args[0] in ("ls-remote", "fetch"):
            calls.append(args)
            time.sleep(sleep_s)
            return subprocess.CompletedProcess(args, 0, f"{sha}\t{args[-1]}\n", "")
        return real(r, *args, timeout=timeout)
    monkeypatch.setattr(pushclaim, "_git", git)
    tids = []
    for i in range(n):
        tid = p.db.add_task(f"ship {i}", "s", kind="code", origin="user", branch="work")
        claim = {"remote": "origin", "ref": f"refs/heads/b{i}", "sha": sha, "source": "summary", "strict": True}
        p.db.update_task(tid, status="pushing", result=dump_result({"status": "pushing", "summary": f"pushed {i}",
                         pushclaim.KEY: {"claims": [claim], "since": time.time(), "tries": 0, "next": 0}}))
        tids.append(tid)
    return p, dmod.Daemon(p.base), tids, calls


def test_push_claim_checks_stay_within_the_tick_budget_and_the_rest_carry_over_untouched(env, monkeypatch):
    from ttp import pushclaim
    monkeypatch.setattr(pushclaim, "NET_BUDGET_S", 0.35)
    monkeypatch.setattr(pushclaim, "LS_TIMEOUT_S", 0.15)
    monkeypatch.setattr(pushclaim, "PER_TICK", 10)
    p, d, tids, calls = _slow_claims(env, monkeypatch, 6)
    took, slow = [], pushclaim._git

    def timed(r, *args, timeout=30):   # each git network call's own duration, as the Budget charges it
        began = time.monotonic()
        try:
            return slow(r, *args, timeout=timeout)
        finally:
            if args[0] in ("ls-remote", "fetch"):
                took.append(time.monotonic() - began)
    monkeypatch.setattr(pushclaim, "_git", timed)
    # Work between the calls (recording a hand-off) is not network time: a loaded host slows it, the
    # Budget does not charge it. Without this a wall-clock bound flakes under load.
    finish = d._finish_worker
    monkeypatch.setattr(d, "_finish_worker", lambda *a, **k: (time.sleep(0.25), finish(*a, **k))[1])
    status = lambda: [p.db.task(t)["status"] for t in tids]
    d._tick_began = time.monotonic()
    d.verify_pushes()
    checked = len(calls)
    # A later call starts only with min(timeout, 1 s) = 0.15 s of the 0.35 s left: at most 3 of 0.1 s fit,
    # and the tick stops at the first that does not (0.05 s for the Budget's own timing around each call).
    spent = [sum(took[:i]) for i in range(checked + 1)]
    assert 1 <= checked <= 3 and all(0.35 - s >= 0.15 for s in spent[1:checked]), (checked, took)
    assert 0.35 - spent[checked] < 0.15 + 0.05, (checked, took)
    assert status() == ["done"] * checked + ["pushing"] * (6 - checked)
    for t in tids[checked:]:   # not tried, not backed off, not failed
        pv = json.loads(p.db.task(t)["result"])[pushclaim.KEY]
        assert pv["tries"] == 0 and pv["next"] == 0 and "last" not in pv, pv
    d.verify_pushes()   # the same tick: its budget is spent, nothing more is asked
    assert len(calls) == checked
    for _ in range(6):
        if "pushing" not in status():
            break
        d._tick_began = time.monotonic()
        d.verify_pushes()
    assert status() == ["done"] * 6 and len(calls) == 6


def test_the_push_audit_out_of_time_resumes_next_tick_from_the_answers_it_has(env, monkeypatch):
    from ttp import pushclaim
    monkeypatch.setattr(pushclaim, "NET_BUDGET_S", 0.15)
    monkeypatch.setattr(pushclaim, "LS_TIMEOUT_S", 0.15)
    p, d, tids, calls = _slow_claims(env, monkeypatch, 3)
    sha = _git_out(env["repo"], "rev-parse", "HEAD")
    for i, t in enumerate(tids):
        p.db.update_task(t, status="done", result=json.dumps({"status": "done", "summary": f"pushed {sha[:10]} to origin/b{i}"}))
    for n in (1, 2):   # one ls-remote per tick: the audit is not recorded until it is whole
        d._tick_began = time.monotonic()
        d.audit_pushes()
        assert len(calls) == n and p.db.kv(pushclaim.AUDIT_KV) is None, calls
    d._tick_began = time.monotonic()
    d.audit_pushes()
    assert len(calls) == 3, calls   # earlier answers were kept, not asked again
    assert p.db.kv(pushclaim.AUDIT_KV)["counts"] == {"confirmed": 3, "missing": 0, "unknown": 0}


def test_push_claim_checks_ask_no_remote_while_the_network_is_held(env, monkeypatch):
    from ttp import pushclaim
    p, d, tids, calls = _slow_claims(env, monkeypatch, 2)
    held = []
    monkeypatch.setattr(d, "net_held", lambda prov: held.append(prov) or True)
    before = [p.db.task(t)["result"] for t in tids]
    d._tick_began = time.monotonic()
    d.verify_pushes()
    d.audit_pushes()
    assert calls == [] and len(held) == 2
    assert [p.db.task(t)["status"] for t in tids] == ["pushing"] * 2
    assert [p.db.task(t)["result"] for t in tids] == before and p.db.kv(pushclaim.AUDIT_KV) is None
    monkeypatch.setattr(d, "net_held", lambda prov: False)
    d.verify_pushes()
    assert len(calls) == 2 and [p.db.task(t)["status"] for t in tids] == ["done"] * 2


def test_a_push_check_call_cut_short_by_the_tick_budget_carries_over_and_an_uncut_timeout_is_unknown(monkeypatch):
    from ttp import pushclaim
    timeouts = []

    def git(r, *args, timeout=30):
        timeouts.append(timeout)
        return subprocess.CompletedProcess(args, 124, "", "TimeoutExpired: too slow")
    monkeypatch.setattr(pushclaim, "_git", git)
    b = pushclaim.Budget(30)
    assert b.call(".", "ls-remote", "o", "r", timeout=40).returncode == 124   # the first call: whole timeout
    b.left = 12
    with pytest.raises(pushclaim.OutOfTime):                                   # cut to what is left
        b.call(".", "fetch", "o", "r", timeout=40)
    b.left = 0.5
    with pytest.raises(pushclaim.OutOfTime):                                   # too little left to start
        b.call(".", "fetch", "o", "r", timeout=40)
    assert timeouts == [40, 12]
    cache = {pushclaim.Budget.KEY: pushclaim.Budget(30)}
    assert pushclaim.verify(".", "o", "refs/heads/r", "abc1234", cache)[0] == pushclaim.UNKNOWN


def test_answers_carried_from_an_out_of_time_tick_never_fail_a_push_that_landed_after_them(env, monkeypatch):
    """Tick 1 confirms A (behind origin/proj's tip: a fetch) and runs out on B's slow ref; Y is then
    pushed to proj and E claims it. The carried ls-remote and fetch of proj predate Y: E ends done."""
    from ttp import daemon as dmod, pushclaim
    from ttp.db import dump_result
    monkeypatch.setattr(pushclaim, "NET_BUDGET_S", 1.2)   # a later call needs 1 s left to start
    monkeypatch.setattr(pushclaim, "LS_TIMEOUT_S", 5)
    monkeypatch.setattr(pushclaim, "PER_TICK", 10)
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    x0 = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/slow")
    _commit(other, "x1.txt", "x1")
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/proj")
    real = pushclaim._git

    def git(r, *args, timeout=30):
        if args[0] == "ls-remote" and args[-1] == "refs/heads/slow":
            time.sleep(min(timeout, 1.4))
            if timeout < 1.4:
                return subprocess.CompletedProcess(args, 124, "", "TimeoutExpired: too slow")
        return real(r, *args, timeout=timeout)
    monkeypatch.setattr(pushclaim, "_git", git)

    def pushing(ref, sha):
        tid = p.db.add_task(f"ship {ref}", "s", kind="code", origin="user", branch="work")
        claim = {"remote": "origin", "ref": f"refs/heads/{ref}", "sha": sha, "source": "summary", "strict": True}
        p.db.update_task(tid, status="pushing", result=dump_result({"status": "pushing", "summary": f"pushed {sha[:10]}",
                         pushclaim.KEY: {"claims": [claim], "since": time.time(), "tries": 0, "next": 0}}))
        return tid
    a, b = pushing("proj", x0), pushing("slow", x0)
    d = dmod.Daemon(p.base)
    d._tick_began = time.monotonic()
    d.verify_pushes()
    assert p.db.task(a)["status"] == "done" and p.db.task(b)["status"] == "pushing"
    assert "verify" in d._push_carry
    _commit(other, "y.txt", "y")
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/proj")
    e = pushing("proj", _git_out(other, "rev-parse", "HEAD"))
    for _ in range(4):
        d._tick_began = time.monotonic()
        d.verify_pushes()
        assert p.db.task(e)["status"] in ("pushing", "done"), p.db.task(e)["result"]
    res = json.loads(p.db.task(e)["result"])
    assert p.db.task(e)["status"] == "done" and res["status"] == "done", res
    assert p.db.task(b)["status"] == "done"


def test_push_check_answers_are_not_carried_past_carry_s_or_a_network_hold(env, monkeypatch):
    from ttp import pushclaim
    p, d, _, _ = _slow_claims(env, monkeypatch, 0)
    k, held = (".", "origin", "refs/heads/x"), [False]
    monkeypatch.setattr(d, "net_held", lambda prov: held[0])

    def kept(name, age):
        d._push_keep(name, {k: (["a"], ""), "since": time.time() - age, pushclaim.CARRIED: set()})
        d._tick_began = time.monotonic()
        return d._push_net(name)
    for name in ("verify", "audit"):
        c = kept(name, pushclaim.CARRY_S / 2)
        assert c[k] == (["a"], "") and c[pushclaim.CARRIED] == {k}
        assert k not in kept(name, pushclaim.CARRY_S + 1)
        d._push_keep(name, {k: (["a"], ""), "since": time.time()})
        held[0] = True
        assert d._push_net(name) is None
        held[0] = False
        c = d._push_net(name)
        assert k not in c and not c[pushclaim.CARRIED]


def test_a_missing_that_rests_on_a_carried_answer_is_asked_again(env, monkeypatch):
    from ttp import pushclaim
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    sha = _git_out(repo, "rev-parse", "HEAD")
    stale = {(str(repo), "origin", "refs/heads/gone"): ([], "")}
    assert pushclaim.verify(repo, "origin", "refs/heads/gone", sha, dict(stale))[0] == pushclaim.MISSING
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/gone")
    cache = {**stale, pushclaim.CARRIED: set(stale)}
    assert pushclaim.verify(repo, "origin", "refs/heads/gone", sha, cache)[0] == pushclaim.CONFIRMED
    assert not cache[pushclaim.CARRIED]


def test_a_pushed_list_naming_a_branch_the_remote_lacks_fails_naming_it(env, monkeypatch, tmp_path):
    p, d, tid, path, head, _ = _claim_setup(env, monkeypatch, tmp_path, {"status": "done", "summary": "shipped"})
    _git_out(path, "push", "-q", "origin", "HEAD:refs/heads/proj")
    hand = json.loads((tmp_path / f"run-{tid}" / "result.json").read_text())
    hand["on_pass"]["pushed"] = [{"sha": head, "branch": "nope"}]
    (tmp_path / f"run-{tid}" / "result.json").write_text(json.dumps(hand))
    p.db.update_task(tid, result=json.dumps({**hand, "waiting_since": time.time() - 60}))
    _settle_probe(d, tid)
    assert p.db.task(tid)["status"] == "pushing"
    d.verify_pushes()
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert task["status"] != "done" and res["status"] == "failed", (task["status"], res)
    assert "origin has no refs/heads/nope" in res["summary"], res["summary"]


def test_the_push_audit_counts_an_on_pass_done_whose_on_pass_cmd_never_ran(env, monkeypatch, tmp_path, capsys):
    from ttp import cli, pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    on = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    _commit(repo, "b.txt", "b\n")
    off = _git_out(repo, "rev-parse", "HEAD")
    now = time.time()

    def done(title, summary, cmd=None, ran=False):
        tid = p.db.add_task(title, "s", kind="code", origin="user", branch="work")
        run_dir = tmp_path / f"run-{tid}"
        run_dir.mkdir()
        (run_dir / "result.json").write_text(json.dumps({"status": "waiting", "on_pass_cmd": cmd} if cmd else {}))
        if ran:
            (run_dir / pushclaim.ON_PASS_DIR).mkdir()
        p.db.x("INSERT INTO runs(task,role,provider,started,ended,status,dir) VALUES(?,?,?,?,?,?,?)",
               (tid, "worker", "fake", now - 120, now - 60, "ok", str(run_dir)))
        p.db.update_task(tid, status="done", result=json.dumps({"status": "done", "summary": summary,
                                                                "run_status": "model-free"}))
        return tid

    done("on", f"pushed {on[:10]} to origin/proj")
    done("off", f"pushed {off[:10]} to origin/proj")
    gap = done("skipped", "checks pass", cmd="ttp checks --result x")
    done("ran", "checks pass", cmd="ttp checks --result y", ran=True)
    a = pushclaim.audit(p)
    assert a["counts"] == {"confirmed": 1, "missing": 2, "unknown": 0}, a
    assert a["tasks"] == 4
    g = [g for g in a["gaps"] if g["source"] == "on_pass"]
    assert [x["task"] for x in g] == [gap] and "on_pass_cmd never ran: ttp checks --result x" in g[0]["why"]
    capsys.readouterr()
    with contextlib.suppress(SystemExit):
        cli.main(["push-audit", "demo", "--json"])
    assert json.loads(capsys.readouterr().out)["counts"] == {"confirmed": 1, "missing": 2, "unknown": 0}


def _audit_done(p, title, summary, branch="work", kind="code"):
    tid = p.db.add_task(title, "s", kind=kind, origin="user", branch=branch)
    p.db.update_task(tid, status="done", result=json.dumps({"status": "done", "summary": summary}))
    return tid


def test_the_push_audit_decides_landed_by_ancestry_not_by_the_ref_or_subject_named(env, monkeypatch):
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    _git_out(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "c.txt", "c\n")
    mine = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "checkout", "-q", "-")
    _git_out(repo, "cherry-pick", mine)         # a replay: another sha, the same change
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    _audit_done(p, "unrelated title", f"pushed {mine[:10]} to origin/elsewhere")
    a = pushclaim.audit(p)
    assert a["counts"]["missing"] == 0 and a["gaps"] == [], a


def test_the_push_audit_skips_tasks_with_no_branch_and_no_queue_claims(env, monkeypatch):
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    sha = _git_out(repo, "rev-parse", "HEAD")
    _audit_done(p, "question", f"pushed {sha[:10]} to origin/proj", branch=None, kind="question")
    _audit_done(p, "review", f"pushed {sha[:10]} to origin/proj", branch=None, kind="review")
    _audit_done(p, "no branch", f"pushed {sha[:10]} to origin/proj", branch=None)
    a = pushclaim.audit(p)
    assert a["gaps"] == [] and a["counts"]["missing"] == 0, a


def _queue_landed(p, tid, sha):
    now = time.time()
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated, pushed_sha, landed_sha) "
           "VALUES(?, 'work', ?, 'origin/proj', 'landed', ?, ?, ?, ?)", (tid, sha, now, now, sha, sha))


def test_the_push_audit_still_checks_a_branchless_review_s_queue_landing(env, monkeypatch):
    """The queue records a landing under the review that approved it, and reviews have no branch."""
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    sha = _git_out(repo, "rev-parse", "HEAD")     # never pushed
    review = _audit_done(p, "Review #1", "approved", branch=None, kind="review")
    _queue_landed(p, review, sha)
    a = pushclaim.audit(p)
    assert [(g["task"], g["state"], g["sha"]) for g in a["gaps"]] == [(review, "missing", sha)], a
    assert a["counts"]["missing"] == 1


def _landed_spy(monkeypatch):
    """Records each fetch the landed re-check could make: _tip(fetch=True), a landed `git fetch`, check()."""
    from ttp import landed
    seen = {"tips": 0, "fetches": 0}
    real_tip, real_git = landed._tip, landed._git

    def tip(repo, onto, fetch):
        seen["tips"] += 1
        seen["fetches"] += bool(fetch)
        return real_tip(repo, onto, fetch)

    def git(repo, *args, timeout=30):
        seen["fetches"] += args[0] == "fetch"
        return real_git(repo, *args, timeout=timeout)

    def check(*a, **k):
        raise AssertionError("landed.check may fetch: the audit's re-check reads local refs only")
    monkeypatch.setattr(landed, "_tip", tip)
    monkeypatch.setattr(landed, "_git", git)
    monkeypatch.setattr(landed, "check", check)
    return seen


def test_the_push_audit_re_check_of_unconfirmed_claims_never_fetches(env, monkeypatch):
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    seen = _landed_spy(monkeypatch)
    tids = []
    for i in range(3):
        _commit(repo, f"u{i}.txt", f"{i}\n")
        tids.append(_audit_done(p, f"t{i}", f"pushed {_git_out(repo, 'rev-parse', 'HEAD')[:10]} to origin/proj"))
    a = pushclaim.audit(p)
    assert a["counts"]["missing"] == 3, a
    assert seen == {"tips": 1, "fetches": 0}, seen      # one local read of the push branch, no fetch
    seen["tips"] = 0
    a = pushclaim.audit(p, max_refs=0)                  # past max_refs: unknown, not re-checked
    assert a["counts"] == {"confirmed": 0, "missing": 0, "unknown": 3} and seen["tips"] == 0, a


def test_the_push_audit_out_of_time_with_unconfirmed_claims_resumes_next_tick(env, monkeypatch):
    from ttp import daemon as dmod, pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    seen = _landed_spy(monkeypatch)
    for i in range(2):
        _commit(repo, f"u{i}.txt", f"{i}\n")
        _audit_done(p, f"t{i}", f"pushed {_git_out(repo, 'rev-parse', 'HEAD')[:10]} to origin/proj")
    d = dmod.Daemon(p.base)
    d._tick_began = time.monotonic()
    spent = pushclaim.Budget()
    spent.calls, spent.left = 1, 0.0
    d._push_budget = (d._tick_began, spent)
    d.audit_pushes()
    assert p.db.kv(pushclaim.AUDIT_KV) is None and seen["tips"] == 0   # nothing told, nothing re-checked
    d._tick_began = time.monotonic() + 1                                # the next tick: a fresh Budget
    d.audit_pushes()
    assert p.db.kv(pushclaim.AUDIT_KV)["counts"]["missing"] == 2 and seen["fetches"] == 0


def test_the_push_audit_reports_a_second_claim_on_one_commit_as_shared_not_missing(env, monkeypatch):
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    sha = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "branch", "owner", sha)
    _git_out(repo, "branch", "other", "HEAD~1")
    owner = _audit_done(p, "owner", f"pushed {sha[:10]} to origin/proj", branch="owner")
    other = _audit_done(p, "other", f"pushed {sha[:10]} to origin/proj", branch="other")
    a = pushclaim.audit(p)
    states = {g["task"]: g["state"] for g in a["gaps"]}
    assert states == {owner: "missing", other: "shared"}, a
    assert a["counts"]["shared"] == 1 and a["counts"]["missing"] == 1


def test_the_push_audit_checks_an_own_branch_push_on_the_target_its_marker_names(env, monkeypatch, tmp_path):
    """An own-branch push, its task worktree pruned since, whose commit is now below a later one on
    that branch: confirmed on the target the marker names, not reported unpushed."""
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    sha = _git_out(repo, "rev-parse", "HEAD")
    _commit(repo, "b.txt", "b\n")                       # a later commit on the task's branch
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/ttp/t1-own")
    _git_out(repo, "fetch", "-q", "origin")
    tid = _audit_done(p, "own push", "handed off", branch="ttp/t1-own")
    run = p.db.x("INSERT INTO runs(task,role,status) VALUES(?,?,?)", (tid, "worker", "ok"))
    pushes = p.state / "pushes"
    pushes.mkdir(parents=True, exist_ok=True)
    (pushes / f"t{tid}-x.json").write_text(json.dumps({
        "run": run, "repo": str(tmp_path / "worktrees" / "gone"), "own": True, "status": "pushed",
        "exit": 0, "sha": sha, "head": sha, "target": "origin/ttp/t1-own"}))
    a = pushclaim.audit(p)
    assert a["gaps"] == [] and a["counts"]["confirmed"] == 1, a
    # The remote out of reach: the local tracking ref settles it, with no fetch.
    _git_out(repo, "remote", "set-url", "origin", str(tmp_path / "unreachable"))
    a = pushclaim.audit(p)
    assert a["gaps"] == [] and a["counts"]["confirmed"] == 1, a
    # A commit the tracking ref does not hold is not confirmed from it.
    _commit(repo, "c.txt", "c\n")
    other = _git_out(repo, "rev-parse", "HEAD")
    (pushes / f"t{tid}-x.json").write_text(json.dumps({
        "run": run, "repo": str(tmp_path / "worktrees" / "gone"), "own": True, "status": "pushed",
        "exit": 0, "sha": other, "head": other, "target": "origin/ttp/t1-own"}))
    a = pushclaim.audit(p)
    assert a["counts"]["confirmed"] == 0 and len(a["gaps"]) == 1, a


def test_the_push_audit_does_not_flag_an_on_pass_cmd_whose_push_is_on_the_remote(env, monkeypatch, tmp_path):
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "a.txt", "a\n")
    sha = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    tid = _audit_done(p, "waited", f"pushed {sha[:10]} to origin/proj")
    p.db.update_task(tid, result=json.dumps({"status": "done", "summary": f"pushed {sha[:10]} to origin/proj",
                                             "run_status": "model-free"}))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "waiting", "on_pass_cmd": "ttp push --result x"}))
    now = time.time()
    p.db.x("INSERT INTO runs(task,role,provider,started,ended,status,dir) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", now - 120, now - 60, "ok", str(run_dir)))
    a = pushclaim.audit(p)
    assert a["gaps"] == [] and a["counts"]["confirmed"] == 1, a


def _letter_sha_commit(repo, n=10):
    """A commit on HEAD whose first `n` hex characters hold no digit (found by its message), checked out."""
    import hashlib
    tree, parent = _git_out(repo, "rev-parse", "HEAD^{tree}"), _git_out(repo, "rev-parse", "HEAD")
    head = f"tree {tree}\nparent {parent}\nauthor t <t@t> 0 +0000\ncommitter t <t@t> 0 +0000\n\n"
    for i in range(10 ** 6):
        body = (head + f"edit {i}\n").encode()
        sha = hashlib.sha1(b"commit %d\0" % len(body) + body).hexdigest()
        if not any(ch.isdigit() for ch in sha[:n]):
            break
    made = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"], input=body,
                          capture_output=True, check=True).stdout.decode().strip()
    assert made == sha
    _git_out(repo, "reset", "-q", "--hard", sha)
    return sha


def test_the_push_audit_reads_a_claimed_short_sha_that_has_no_digit(env, monkeypatch, tmp_path):
    """Replay: two audit tests failed together in one push batch because the commit they made in the same
    second had a 10-character prefix with no digit, which the summary parser did not read as a sha: the
    claim was dropped and the on_pass audit reported a gap. A hex-only word that names a ref, not a
    commit it prefixes, is still no claim."""
    from ttp import pushclaim
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    sha = _letter_sha_commit(repo)
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    tid = _audit_done(p, "waited", f"pushed {sha[:10]} to origin/proj")
    p.db.update_task(tid, result=json.dumps({"status": "done", "summary": f"pushed {sha[:10]} to origin/proj",
                                             "run_status": "model-free"}))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "waiting", "on_pass_cmd": "ttp push --result x"}))
    now = time.time()
    p.db.x("INSERT INTO runs(task,role,provider,started,ended,status,dir) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", now - 120, now - 60, "ok", str(run_dir)))
    a = pushclaim.audit(p)
    assert a["gaps"] == [] and a["counts"]["confirmed"] == 1, a
    assert [c["sha"] for c in pushclaim.text_claims(p, f"Pushed {sha[:10].upper()} to origin/proj")] == [sha]
    word = next(w for w in ("deadbeef", "facade0f", "decade00") if not sha.startswith(w))
    _git_out(repo, "branch", word, sha)
    assert pushclaim.text_claims(p, f"pushed {word} to origin/proj") == []


def _pinned_plan(repo, git_cmd, plan="checks/harness.json"):
    """A committed harness check plan that pins watch.py by digest; its at-HEAD check-plan command."""
    import hashlib
    (repo / "checks").mkdir(exist_ok=True)
    (repo / "watch.py").write_text("print('watch')\n")
    cover = {"paths": {"watch.py": hashlib.sha256(b"print('watch')\n").hexdigest()}, "commands": ["true"]}
    (repo / plan).write_text(json.dumps({"purpose": "harness", "coverage": cover}, indent=2) + "\n")
    subprocess.run([*git_cmd, "add", "watch.py", plan], check=True)
    subprocess.run([*git_cmd, "commit", "-qm", "harness plan"], check=True)
    return f"ttp check-plan --purpose harness --definition-at-head --definition-path {plan}"


def test_ttp_checks_purpose_harness_fails_a_stale_digest_before_queuing_for_a_slot(env, tmp_path, monkeypatch,
                                                                                   capsys):
    """Replay: a template upgrade's pre-merge commit changed a file the harness check plan pins, and the
    next unrelated harness task waited over an hour for a checks slot only to fail check-plan on that
    digest. The stale digest now fails at once, naming the commit that changed the input, and no slot
    is asked for; with the digest refreshed the checks run as before."""
    from ttp import checkplan, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    log = tmp_path / "log"
    _fake_check_plan_ttp(tmp_path, monkeypatch, log)
    cmd = _pinned_plan(repo, git)
    p.set_config("delivery.push_checks", [{"run": cmd, "purpose": "harness"}])
    assert checkplan.stale(repo, cmd) == ""
    (repo / "watch.py").write_text("print('watch, edited live')\n")
    subprocess.run([*git, "commit", "-qam", "local harness changes before template upgrade"], check=True)
    (repo / "b.txt").write_text("the task's own change\n")
    subprocess.run([*git, "add", "b.txt"], check=True)
    subprocess.run([*git, "commit", "-qm", "unrelated harness change"], check=True)
    why = checkplan.stale(repo / "checks", cmd)       # from a subdirectory too
    assert why.startswith("uncovered committed input: watch.py") and "before template upgrade" in why, why
    asked, slot = [], cli._checks_slot
    monkeypatch.setattr(cli, "_checks_slot", lambda *a, **k: asked.append(a) or None)
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--purpose", "harness"])
    out = capsys.readouterr().out
    assert e.value.code == 1 and not asked and not log.exists(), out
    assert "not queued for a checks slot" in out and "last changed in" in out and "before template upgrade" in out
    assert json.loads((run / "checks.json").read_text())["passed"] is False
    # Refreshed in a commit of its own: the plan runs (through the slot) and passes.
    assert checkplan.refresh(repo, "checks/harness.json", ["watch.py", "b.txt"]) == ["watch.py"]
    subprocess.run([*git, "commit", "-qam", "refresh the digest"], check=True)
    assert checkplan.stale(repo, cmd) == ""
    monkeypatch.setattr(cli, "_checks_slot", slot)
    cli.main(["checks", "--purpose", "harness", "--fresh"])
    assert log.read_text() == "plan\n"
    # Other problems (an absent definition commit, a malformed command) are left to the real run.
    assert checkplan.stale(repo, HARNESS_PLAN) == "" and checkplan.stale(repo, "ttp check-plan --bogus") == ""


@pytest.mark.parametrize("how", ["at_head", "running_task", "pinned"])
def test_upgrade_refreshes_the_digests_its_pre_merge_commit_changes(env, monkeypatch, capsys, how):
    """The pre-upgrade commit of live harness edits refreshes the digests a harness check plan read at
    HEAD pins for the inputs it commits, in the same commit, so the next harness task's checks do not
    fail on them. A plan pinned at a definition commit cannot be refreshed: the upgrade says so."""
    p = make(env)
    from ttp import checkplan, cli, service
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    _fake_check_plan_ttp(env["home"], monkeypatch, env["home"] / "plan.log")
    h = p.harness
    mark = env["home"] / "lib" / "current" / "runtime" / "ttp" / "SOURCE_COMMIT"
    _install_template(env)
    mark.write_text("aaaa1111\n")
    cli.main(["upgrade", "demo"])
    git_cmd = ["git", "-C", str(h), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    cmd = _pinned_plan(h, git_cmd)
    if how == "pinned":
        cmd = cmd.replace("--definition-at-head", "--definition-commit " + _git_out(h, "rev-parse", "HEAD"))
    p.set_config("delivery.push_checks", [{"run": cmd, "purpose": "harness"}])
    subprocess.run([*git_cmd, "commit", "-qam", "config"], check=True)
    (h / "watch.py").write_text("print('watch, edited live')\n")
    if how == "running_task":     # the edit predates the running harness task, so the upgrade commits it
        now = time.time()
        os.utime(h / "watch.py", (now - 7200, now - 7200))
        tid = p.db.add_task("Unrelated", "spec", kind="harness")
        p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(?,'worker','claude',?,'running')",
               (tid, now - 3600))
    capsys.readouterr()
    mark.write_text("bbbb2222\n")
    cli.main(["upgrade", "demo"])
    out = capsys.readouterr().out
    swept = _git_out(h, "log", "--format=%H", "--grep", "before template upgrade", "-1")
    files = _git_out(h, "show", "--name-only", "--format=", swept).split("\n")
    assert "watch.py" in files, out
    if how == "pinned":
        assert "checks/harness.json" not in files and "pinned at commit" in out, out
        assert checkplan.stale(h, cmd).startswith("uncovered committed input: watch.py")
    else:
        assert "checks/harness.json" in files and "refreshed the digests of watch.py" in out, out
        assert checkplan.stale(h, cmd) == "" and checkplan.run(h, None, "checks/harness.json", "harness") == 0


def test_superseded_repo_override_finds_a_commit_only_a_separate_clone_has(env, monkeypatch, tmp_path, capsys):
    """The commit and the task's branch sit in a clone no run record points at: without --repo the error
    names every path searched; --repo <path> searches that clone first and records; a path that is not a
    repository, or a commit nowhere, is refused."""
    from ttp import cli, landed
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)

    def ttp_task(*args):
        try:
            cli.main(["task", p.name, *args])
        except SystemExit as e:
            return e.code
        return 0
    clone = tmp_path / "clone"
    _git_out(tmp_path, "init", "-q", "-b", "main", str(clone))
    _commit(clone, "lib.txt", "one\n")
    tid2 = p.db.add_task("work in a clone", "s", kind="code", tier="light", origin="user")
    p.db.update_task(tid2, status="done", branch=f"ttp/t{tid2}-x")
    _git_out(clone, "checkout", "-q", "-b", f"ttp/t{tid2}-x")
    _commit(clone, "lib.txt", "one\ntwo\n")
    feat = _git_out(clone, "rev-parse", "HEAD")
    _git_out(clone, "checkout", "-q", "-b", "pr-head", "main")
    _commit(clone, "lib.txt", "one\ntwo, settled\n")
    relanded = _git_out(clone, "rev-parse", "HEAD")
    _git_out(clone, "checkout", "-q", "main")

    assert ttp_task("superseded", str(tid2), relanded, "--ref", "pr-head") not in (0, None)
    assert str(p.root) in capsys.readouterr().err
    assert ttp_task("superseded", str(tid2), relanded, "--ref", "pr-head", "--repo", str(tmp_path / "nope")) not in (0, None)
    assert "not a git repository" in capsys.readouterr().err
    assert ttp_task("superseded", str(tid2), "0" * 40, "--ref", "pr-head", "--repo", str(clone)) not in (0, None)
    err = capsys.readouterr().err
    assert str(clone) in err and str(p.root) in err
    assert ttp_task("superseded", str(tid2), relanded, "--ref", "pr-head", "--repo", str(clone)) == 0
    assert landed.superseded(p.db, [tid2]) == {tid2: [(feat, relanded)]}


def test_superseded_reads_the_cwd_repository_and_accepts_a_commit_already_on_the_ref(env, monkeypatch, capsys):
    """The commit is found in the repository `ttp` runs in when the task's own clone lacks it, and a commit
    already on --ref (local or remote-tracking) is accepted without the file-overlap check; without --ref
    the overlap error says what it compares and how to pass the ref."""
    from ttp import cli
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    _commit(path, "more.txt", "a\nb\n")
    _commit(other, "unrelated.txt", "other work\n")
    unrelated = _git_out(other, "rev-parse", "HEAD")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")

    def ttp_task(*args):
        try:
            cli.main(["task", p.name, *args])
        except SystemExit as e:
            return e.code
        return 0
    _git_out(other, "fetch", "-q", str(repo), f"{branch}:{branch}")   # a clone with the branch and the commit
    monkeypatch.chdir(other)   # the task's own clone has not fetched the commit
    assert ttp_task("superseded", str(tid), unrelated, "--ref", "origin/proj") == 0
    assert "recorded as superseded" in capsys.readouterr().out
    p.db.x("DELETE FROM task_superseded WHERE task=?", (tid,))
    monkeypatch.chdir(repo)
    _git_out(repo, "fetch", "-q", "origin")
    assert ttp_task("superseded", str(tid), unrelated) not in (0, None)
    err = capsys.readouterr().err
    assert "changes none of the files" in err and "compared:" in err and "--ref <branch>" in err
    assert ttp_task("superseded", str(tid), unrelated, "--ref", "origin/proj") == 0
    assert "recorded as superseded" in capsys.readouterr().out


# prompt-touching changes and prompt size caps ---------------------------------------------------------
_CAP_TAIL = """E       assert 30512 <= 30300  (in test_the_coordinator_role_prompt_stays_small)
E       assert 29400 <= 29000  (in test_a_heavy_project_keeps_the_worker_prompt_under_its_cap)
FAILED tests/test_a.py::test_the_coordinator_role_prompt_stays_small - AssertionError: 30512
FAILED tests/test_a.py::test_a_heavy_project_keeps_the_worker_prompt_under_its_cap - AssertionError: ('code', 29400)
2 failed, 900 passed in 61.20s"""


def _pq_change(s, name, files):
    """A review whose approval is a commit on its own branch off the reviewed head, changing `files`."""
    from ttp import pushq
    _git_out(s.path, "checkout", "-q", "-b", name, s.head)
    for f in files:
        (s.path / f).parent.mkdir(parents=True, exist_ok=True)
        _commit(s.path, f, f"{name}\n")
    head = _git_out(s.path, "rev-parse", "HEAD")
    rid = s.p.db.add_task(f"review {name}", "r", kind="review", origin="coordinator")
    s.p.db.update_task(rid, status="pushing")
    with s.p.db.tx():
        [row] = pushq.approve(s.p, rid, None, {"entries": [{"branch": name, "head": head}], "target": "origin/proj"})
    return row


def test_a_push_batch_takes_at_most_one_change_touching_the_prompt_files(env, monkeypatch):
    """Two prompt changes that each fit a size cap may not fit together: a batch takes at most one
    approval touching delivery.push_solo_paths (default the plugin's prompt templates); the others
    stay approved for a later batch, and the rest of the batch fills up as before."""
    from ttp import pushq
    s = _pq(env, monkeypatch, push_batch_max=3)
    _git_out(s.repo, "fetch", "-q", "origin")
    prompt = "plugins/tt-project/template/prompts/worker.md"
    a = _pq_change(s, "a", [prompt])
    b = _pq_change(s, "b", ["plain.txt"])
    c = _pq_change(s, "c", ["x.txt", "plugins/tt-project/template/prompts/coordinator.md"])
    d = _pq_change(s, "d", ["other.txt"])
    rows, why = pushq.due(s.p, time.time() + 10 ** 6)
    assert [r["id"] for r in rows] == [a, b, d], why
    assert "1 more approval touching delivery.push_solo_paths waits for a later batch" in why
    assert pushq.start(s.p, rows, "origin/proj")
    assert s.p.db.one("SELECT status FROM push_queue WHERE id=?", (c,))["status"] == "approved"
    # Another prompt file, the same rule; a text file of the prompt folder that is no .md is not one.
    s.p.db.x("DELETE FROM push_queue")
    s.p.db.x("DELETE FROM push_batches")
    e = _pq_change(s, "e", ["plugins/tt-project/template/prompts/notes.txt"])
    f = _pq_change(s, "f", [prompt])
    g = _pq_change(s, "g", ["plugins/tt-project/template/prompts/kind-code.md"])
    assert [r["id"] for r in pushq.due(s.p, time.time() + 10 ** 6)[0]] == [e, f]
    # Configurable: other paths, or off.
    s.p.set_config("delivery.push_solo_paths", ["*/notes.txt", "*/worker.md"])
    assert [r["id"] for r in pushq.due(s.p, time.time() + 10 ** 6)[0]] == [e, g]
    s.p.set_config("delivery.push_solo_paths", [])
    assert [r["id"] for r in pushq.due(s.p, time.time() + 10 ** 6)[0]] == [e, f, g]


def test_cap_failure_reads_only_a_complete_list_of_prompt_cap_test_failures_and_their_overshoots():
    from ttp import conflictfix
    got = conflictfix.cap_failure(_CAP_TAIL)
    assert got["ids"] == ["tests/test_a.py::test_a_heavy_project_keeps_the_worker_prompt_under_its_cap",
                          "tests/test_a.py::test_the_coordinator_role_prompt_stays_small"]
    assert got["over"] == [{"test": "test_the_coordinator_role_prompt_stays_small", "size": 30512, "cap": 30300,
                            "by": 212},
                           {"test": "test_a_heavy_project_keeps_the_worker_prompt_under_its_cap", "size": 29400,
                            "cap": 29000, "by": 400}]
    assert "test_the_coordinator_role_prompt_stays_small: 30512 > 30300 (over by 212)" in conflictfix.over_text(got)
    other = _CAP_TAIL.replace("2 failed", "3 failed") + "\nFAILED tests/test_a.py::test_push_lands - assert 1 == 2"
    assert conflictfix.cap_failure(other) is None, "a failure of another test is no cap failure"
    assert conflictfix.cap_failure(_CAP_TAIL.replace("2 failed", "5 failed")) is None, "an incomplete list"
    assert conflictfix.cap_failure("make: *** [test] Error 1") is None, "not pytest"
    bare = "FAILED tests/t.py::test_prompt_size - AssertionError\n1 failed, 3 passed in 1.00s"
    assert conflictfix.cap_failure(bare) == {"ids": ["tests/t.py::test_prompt_size"], "over": []}
    assert "tests/t.py::test_prompt_size show it" in conflictfix.over_text(conflictfix.cap_failure(bare))
    assert conflictfix.cap_failure(bare, r"never") is None, "delivery.push_cap_tests names the cap tests"
    dup = "E       assert 30512 <= 30300  (in test_the_coordinator_role_prompt_stays_small)\n" + _CAP_TAIL.replace(
        "E       assert 29400", "E       assert 30512 <= 30300\nE       assert 29400", 1)
    assert [o["size"] for o in conflictfix.cap_failure(dup)["over"]] == [30512, 29400], "listed once"
    assert conflictfix.cap_failure(dup)["over"][0]["test"] == "test_the_coordinator_role_prompt_stays_small"
    anon_first = "E       assert 30512 <= 30300\n" + _CAP_TAIL
    assert [o["test"] for o in conflictfix.cap_failure(anon_first)["over"] if o["size"] == 30512] == [
        "test_the_coordinator_role_prompt_stays_small"], "the entry with a test name wins"


def test_the_default_cap_test_pattern_matches_exactly_the_suites_size_cap_tests():
    import pathlib
    import re
    from ttp import conflictfix
    names = {n for f in pathlib.Path(__file__).resolve().parents[1].glob("**/*.py")
             for n in re.findall(r"^def (test_\w+)", f.read_text(), re.M)}
    got = sorted(n for n in names if re.search(conflictfix.CAP_TESTS, n))
    assert got == ["test_a_charter_and_memory_heavy_project_keeps_the_worker_prompt_under_its_cap",
                   "test_coordinator_timeout_defaults_ceiling_and_prompt_size",
                   "test_the_coordinator_role_prompt_carries_only_the_rules_of_features_that_are_on_and_stays_small",
                   "test_the_worker_system_prompt_carries_only_the_rules_its_task_needs_and_stays_small"]


def test_a_batch_check_tail_keeps_each_size_overshoot_from_anywhere_in_the_output(tmp_path):
    """The tail is the last lines only; the `assert <size> <= <cap>` line sits above them in pytest's
    failure section. The tail starts with each one over its cap, naming its test."""
    from ttp import batch
    out = tmp_path / "out.txt"
    out.write_text("\n".join(["_____ test_role_prompt_stays_small _____", "E       AssertionError: 30512",
                              "E       assert 30512 <= 30300", "_____ test_other _____", "E       assert 3 <= 9",
                              *[f"line {i}" for i in range(40)], "1 failed, 2 passed in 1.00s"]) + "\n")
    rc, tail, _ = batch._stream(["cat", str(out)], tmp_path)
    lines = tail.splitlines()
    assert rc == 0 and lines[0] == "E       assert 30512 <= 30300  (in test_role_prompt_stays_small)", lines[:3]
    assert "assert 3 <= 9" not in tail and lines[-1] == "1 failed, 2 passed in 1.00s"
    assert len(lines) == 1 + batch.push.LOG_TAIL


def test_a_batch_failing_only_on_prompt_caps_sends_its_change_a_light_trim_to_fit_fix(env, monkeypatch):
    """The blamed change fit its caps alone but not merged onto what landed before it: instead of a
    failed review (and a shared-check incident), its review runs again at once as a light trim-to-fit
    fix told each measured overshoot, at no attempt and with no coordinator turn. After TRIM_MAX of
    them a cap failure fails the review as before."""
    from ttp import conflictfix
    from ttp.daemon import Daemon
    from ttp.prompts import worker_task
    s = _pq(env, monkeypatch)
    _pq_plan(s, outcome="nothing", row="check_failed", tip="ab" * 20, detail={"cmd": "pytest -q", "tail": _CAP_TAIL})
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _pq_batch(s)
    _pq_tend(s)
    t = s.p.db.task(s.review)
    res = json.loads(t["result"])
    cf = res["conflict_fix"]
    assert t["status"] == "queued" and res[conflictfix.TRIMS] == 1, t
    assert (cf["head"], cf["branch"], cf["onto"]) == (s.head, s.branch, "ab" * 20) and "escalate" not in cf
    assert [o["by"] for o in cf["trim"]["over"]] == [212, 400]
    evs = _pq_events(s.p, mark, task=s.review)
    assert not [e for e in evs if e["status"] == "queued"], "no coordinator turn"
    [ev] = [e for e in evs if e["kind"] == "task_requeued"]
    assert "trim-to-fit" in ev["text"] and "over by 212" in ev["text"]
    task, pick = Daemon(s.p.base)._size_review(t)
    assert task["tier"] == "light" and pick["conflict_fix"]["tier"] == "light", pick
    short = worker_task(s.p, t, str(s.p.root), None, conflict=pick["conflict_fix"])
    assert "trim-to-fit fix" in short and "over by 400" in short and "never raise a cap" in short
    assert "review the feature" not in short and f'"push": [{{"branch": "{s.branch}"' in short
    full = worker_task(s.p, t, str(s.p.root), None, conflict={"tier": "standard", "why": "escalated"})
    assert "trims the change to fit its prompt size caps at standard" in full and "over by 212" in full
    # Each trim that still overshoots gets one more, up to TRIM_MAX; then the review fails.
    for n in range(2, conflictfix.TRIM_MAX + 2):
        _pq_hand_off(env, s, note={"conflict_fix": {"tier": "light"}})
        _pq_batch(s)
        _pq_tend(s)
        t = s.p.db.task(s.review)
        if n <= conflictfix.TRIM_MAX:
            assert t["status"] == "queued" and json.loads(t["result"])[conflictfix.TRIMS] == n
    assert t["status"] == "failed" and "pytest -q" in json.loads(t["result"])["summary"], t


def test_a_fix_of_a_failed_review_runs_at_least_standard(env, monkeypatch):
    """The daemon's fix of a failed review, and a queued task that continues a failed review, never run light."""
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _, _, _, (rev,) = _finish_code(env, p, "lt", {"lt.py": 3})
    code = p.db.task(json.loads(rev["labels"]) and rev["depends_on"] and json.loads(rev["depends_on"])[0])
    p.db.update_task(code["id"], tier="light")
    p.db.update_task(rev["id"], status="failed")
    fid, _, _ = Daemon(p.base)._fix_failed_review(p.db.task(rev["id"]), [{"title": "bug", "spec": "fix it"}], "bug")
    assert p.db.task(fid)["tier"] == "standard"
    queued = p.db.add_task("by hand", "x", kind="code", tier="light", labels=[f"continues:{rev['id']}"])
    assert p.db.task(queued)["tier"] == "standard"
    deep = p.db.add_task("deep", "x", kind="code", tier="deep", labels=[f"continues:{rev['id']}"])
    assert p.db.task(deep)["tier"] == "deep"
    plain = p.db.add_task("plain", "x", kind="code", tier="light")
    assert p.db.task(plain)["tier"] == "light"
    from ttp import effort
    monkeypatch.setattr(effort, "_jev_tier", lambda *a, **k: {"tier": "light", "by": "jev", "score": 0.2})
    d = Daemon(p.base)

    def started(tier, label):
        t = p.db.task(p.db.add_task("c", "x", kind="code", tier=tier, origin="coordinator", labels=[label]))
        got = d._pick_effort(t, "claude")
        return p.db.task(t["id"])["tier"]
    p.db.update_task(rev["id"], tier="standard")
    assert started("standard", f"continues:{rev['id']}") == "standard"
    p.db.update_task(rev["id"], tier="light")
    assert started("light", f"continues:{rev['id']}") == "standard"
    assert started("deep", f"continues:{rev['id']}") == "deep"
    assert p.db.task(fid)["tier"] == "standard"


def test_a_fix_of_a_failed_review_starts_at_standard_not_the_reviewed_tasks_raised_tier(env, monkeypatch):
    """A reported case, replayed: the daemon's 'Fix review #<id>' of a code task whose retry had run at
    deep was queued at deep too. A fix now starts at standard (one tier below the code task's queued
    tier, kept at standard by a fix's light floor), with tier_source fix_review_default and the code
    task's pins. Deep only when the user asked for deep on the chain (user_deep) or the review marks
    the fix as needing it (`needs_deep` in a finding or in its hand-off)."""
    from ttp import effort
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _, _, _, (rev,) = _finish_code(env, p, "rd", {"rd.py": 3})
    code = p.db.task(json.loads(rev["depends_on"])[0])
    assert code["assigned_tier"] == "standard"
    p.db.update_task(code["id"], tier="deep", tier_source="retry_tier_up", model="opus", effort="max")
    p.db.update_task(rev["id"], status="failed")
    fid, _, _ = Daemon(p.base)._fix_failed_review(p.db.task(rev["id"]), [{"title": "bug", "spec": "fix it"}], "bug")
    fix = p.db.task(fid)
    assert (fix["tier"], fix["tier_source"], fix["model"], fix["effort"]) == \
        ("standard", "fix_review_default", "opus", "max")
    review, code = p.db.task(rev["id"]), p.db.task(code["id"])
    plain = [{"title": "bug", "spec": "fix it"}]
    for assigned in ("deep", "standard", "light"):
        p.db.update_task(code["id"], assigned_tier=assigned)
        assert effort.fix_tier(p.db, p.db.task(code["id"]), review, plain) == \
            ({"deep": "standard"}.get(assigned, "light"), "fix_review_default")
    assert effort.fix_tier(p.db, code, review, [{"title": "race", "spec": "s", "needs_deep": "a lock-order redesign"}]) \
        == ("deep", "review_needs_deep")
    p.db.update_task(rev["id"], result=json.dumps({"status": "done", "needs_deep": "the design is wrong"}))
    assert effort.fix_tier(p.db, code, p.db.task(rev["id"]), plain) == ("deep", "review_needs_deep")
    p.db.update_task(rev["id"], result=json.dumps({"status": "done"}))
    p.db.update_task(code["id"], labels=json.dumps(json.loads(code["labels"] or "[]") + [effort.USER_DEEP]))
    assert effort.fix_tier(p.db, p.db.task(code["id"]), p.db.task(rev["id"]), plain) == ("deep", "user_deep")


def test_own_push_existing_only_refusal_of_another_tasks_branch_names_the_own_branch_route(env, monkeypatch, capsys):
    """A reported case, replayed: with delivery.own_push unset, a code task whose spec's `pr_branch:`
    line named another task's never-published ttp/t<id>-... branch was refused by `ttp push --own`
    and handed off blocked, though its own branch was a route that works. The refusal now names
    it (`ttp push --own-branch`), which creates only the task's own branch. An explicit existing_only
    stays strict: its refusal names no such route, and the own branch is not created either."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.own_push", None)
    first = p.db.add_task("Drop the link", "s", kind="code")
    theirs = f"ttp/t{first}-drop-the-link"
    t = p.db.add_task("Publish it", f"pr_branch: `{theirs}`\nPublish it.", kind="code",
                     labels=[f"pr_branch:{theirs}"])
    mine = f"ttp/t{t}-publish-it"
    _git_out(repo, "checkout", "-q", "-b", theirs)
    _commit(repo, "fix.txt", "fix\n")
    heads = lambda: _git_out(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    monkeypatch.setenv("TTP_TASK", str(t))
    capsys.readouterr()
    assert _ttp("push", "--own") == 2
    err = capsys.readouterr().err
    assert "not creating it" in err and "ttp push --own-branch --detach" in err and mine in err, err
    assert heads() == ["proj"]
    _git_out(repo, "checkout", "-q", "-b", mine)
    p.set_config("delivery.own_push", "existing_only")
    assert _ttp("push", "--own-branch") == 2 and heads() == ["proj"]
    _git_out(repo, "checkout", "-q", theirs)
    assert _ttp("push", "--own") == 2
    err = capsys.readouterr().err
    assert "not creating it" in err and "--own-branch" not in err, err
    p.set_config("delivery.own_push", None)
    _git_out(repo, "checkout", "-q", mine)
    assert _ttp("push", "--own-branch") == 0, capsys.readouterr().err
    assert heads() == ["proj", mine]
    assert _git_out(origin, "rev-parse", mine) == _git_out(repo, "rev-parse", "HEAD")


def test_push_own_reuses_a_ttp_checks_pass_only_on_the_same_tree_commands_and_env(env, monkeypatch, tmp_path, capsys):
    """`ttp push --own` runs again no check `ttp checks` recorded passing on exactly HEAD's tree with the
    same commands (or more that start with them) and environment, and says it reused that pass. A
    changed tree, command or environment, or a pass recorded without its environment, runs them."""
    from ttp import cli, push
    log = tmp_path / "ran"
    check = f"echo x >> {log}"
    p, repo, origin, other = _push_setup(env, monkeypatch, [check])
    run = tmp_path / "run"
    run.mkdir()
    for var, val in (("TTP_RUN_DIR", str(run)), ("TTP_RUN_ID", "41"), ("TTP_TASK", "40")):
        monkeypatch.setenv(var, val)
    for var in ("PYTEST_ADDOPTS", "PYTHONPATH"):
        monkeypatch.delenv(var, raising=False)
    _git_out(repo, "checkout", "-q", "-b", "ttp/t40-feature")
    ran = lambda: len(log.read_text().splitlines()) if log.exists() else 0
    n = iter(range(100))

    def push_own(*extra, change=lambda: None):
        _commit(repo, f"m{next(n)}.py", "x = 1\n")
        cli.main(["checks", *(["--", *extra] if extra else [])])
        change()
        before = ran()
        capsys.readouterr()
        assert push.run(p, repo, own=True) == 0
        err = capsys.readouterr().err
        assert _git_out(origin, "rev-parse", "ttp/t40-feature").strip() == _git_out(repo, "rev-parse", "HEAD").strip()
        return ran() - before, err

    runs, err = push_own()
    assert runs == 0 and "reused the pass of 1 check(s) `ttp checks` recorded in run 41" in err, err
    runs, err = push_own("true")   # the project's checks ran first, then the extra: a pass of them too
    assert runs == 0 and "reused the pass" in err, err
    # The tree changed after the checks (a new commit): they run again.
    runs, err = push_own(change=lambda: _commit(repo, "late.py", "y = 2\n"))
    assert runs == 1 and "reused" not in err, err
    # Another command than the one that passed.
    other_check = f"echo y >> {log}"
    runs, err = push_own(change=lambda: p.set_config("delivery.push_checks", [other_check]))
    assert runs == 1 and "reused" not in err, err
    p.set_config("delivery.push_checks", [check])
    # Another environment: a variable that changes what pytest runs, or another active venv.
    runs, err = push_own(change=lambda: monkeypatch.setenv("PYTEST_ADDOPTS", "-x"))
    assert runs == 1 and "reused" not in err, err
    monkeypatch.delenv("PYTEST_ADDOPTS")
    runs, err = push_own(change=lambda: monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "other-venv")))
    assert runs == 1 and "reused" not in err, err
    monkeypatch.delenv("VIRTUAL_ENV")
    # A pass recorded without its environment (an older runtime's) never serves.
    _commit(repo, "old.py", "z = 3\n")
    tree = _git_out(repo, "rev-parse", "HEAD^{tree}").strip()
    cli._record_pass(p, tree, push.check_list([check]))
    before = ran()
    assert push.run(p, repo, own=True) == 0
    assert ran() - before == 1 and "reused" not in capsys.readouterr().err


_CAP_SUITE = '''import glob
import os


def _log(word):
    with open(os.environ["CAP_SUITE_LOG"], "a") as f:
        f.write(word + "\\n")


def CAP_TEST():
    _log("cap")
    assert sum(os.path.getsize(f) for f in glob.glob("plugins/p/*.txt")) <= 7


def test_everything_else():
    _log("full")
'''


def _cap_suite_batch(env, monkeypatch):
    """_bump_setup whose push check is one plain pytest run of a fake suite: a cap test that keeps the
    plugin's .txt files to 7 bytes, and a test standing for the rest. Each test logs a line when run.
    Five entries of 2 bytes each: entry 4 breaks the cap, and so does entry 5 without it."""
    log = env["tmp"] / "suite.log"
    monkeypatch.setenv("CAP_SUITE_LOG", str(log))
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["python3 -m pytest -q -p no:cacheprovider suite"])
    (repo / "suite").mkdir()
    # named here, so the suite's own cap test list (CAP_TESTS) does not take it for one of its tests
    (repo / "suite/test_suite.py").write_text(_CAP_SUITE.replace("CAP_TEST", "test_the_role_prompt_stays_small"))
    _git_out(repo, "add", "suite")
    _git_out(repo, "commit", "-qm", "the suite")
    _git_out(repo, "push", "-q", "origin", "HEAD:proj")
    heads = [_entry(repo, f"e{n}", {f"plugins/p/f{n}.txt": f"{n}\n"}) for n in range(1, 6)]
    return p, origin, heads, log


def test_a_batch_that_breaks_only_a_cap_finds_the_entry_on_cap_probes_and_runs_one_full_suite(env, monkeypatch, capsys):
    """A 5-entry batch that fails only a prompt size cap test: cap probes (only the cap tests) of the
    full head and each prefix find entry 4, one full suite runs on entries 1..3, which push; entry 4
    is check_failed with a cap-only tail (pushq sends it back to be trimmed), and entry 5, which breaks
    the cap without it too, is requeued after one more probe. Before, each of those heads ran the full
    suite: five full runs."""
    from ttp import conflictfix, pushpass
    p, origin, heads, log = _cap_suite_batch(env, monkeypatch)
    rc, m = _run_batch(p, _batch_marker(p, heads), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed", m
    assert _statuses(m) == ["pushed"] * 3 + ["check_failed", "requeued"], m
    ran = log.read_text().split()
    assert ran.count("full") == m["checks"]["runs"] == 1, ran
    assert m["checks"]["cap_probes"] == 5 and ran.count("cap") == 6, (m["checks"], ran)   # 5 probes + the full run
    trim = conflictfix.cap_failure(m["results"][3]["detail"]["tail"])
    assert trim and trim["over"][0]["size"] == 8 and trim["over"][0]["cap"] == 7, m["results"][3]
    out = capsys.readouterr().out
    assert "searching on the cap tests alone" in out and "they break a prompt size cap too" in out
    assert len(json.loads((p.state / pushpass.CAPS).read_text())["probes"]) == 5


def test_a_batch_rerun_after_a_reboot_reuses_the_cap_probes_it_already_ran(env, monkeypatch, capsys):
    """A batch dies in its full suite after the cap probes found the entry that breaks the cap (a
    reboot); the rerun, a new batch of the same entries on the same tip, reads the probes of those
    prefixes back, failing ones included, and runs only the full suite and the one probe it never got to."""
    from ttp import batch
    p, origin, heads, log = _cap_suite_batch(env, monkeypatch)

    class Reboot(Exception):
        pass

    real = batch.Batch._check

    def dies(self, head, checks):
        raise Reboot("the host rebooted")
    monkeypatch.setattr(batch.Batch, "_check", dies)
    rc, m = _run_batch(p, _batch_marker(p, heads, bid="b1"), monkeypatch)
    assert m["outcome"] == "error" and m["checks"]["cap_probes"] == 4, m
    log.write_text("")
    monkeypatch.setattr(batch.Batch, "_check", real)
    rc, m = _run_batch(p, _batch_marker(p, heads, bid="b2"), monkeypatch)
    assert rc == 0 and _statuses(m) == ["pushed"] * 3 + ["check_failed", "requeued"], m
    assert m["checks"]["cap_reused"] == 4 and m["checks"]["cap_probes"] == 1, m["checks"]
    assert log.read_text().split().count("full") == 1
    assert "failed in b1" in capsys.readouterr().out


def test_a_single_entry_that_breaks_only_a_cap_probes_the_tip_instead_of_a_full_suite(env, monkeypatch):
    """One entry whose full suite fails only a cap test: the tip gets a cap probe, not a full suite."""
    p, origin, heads, log = _cap_suite_batch(env, monkeypatch)
    big = _entry(p.root, "e9", {"plugins/p/big.txt": "12345678\n"})
    rc, m = _run_batch(p, _batch_marker(p, [big]), monkeypatch)
    assert _statuses(m) == ["check_failed"] and m["outcome"] == "nothing", m
    ran = log.read_text().split()
    assert ran.count("full") == m["checks"]["runs"] == 1 and m["checks"]["cap_probes"] == 1, (ran, m["checks"])


def _fake_python(tmp_path, find_rc=0, version="", name="python3"):
    """A python3 on PATH whose runner precheck (envfault's find_spec probe) exits `find_rc` and prints
    `version`; any other run is logged to ran.txt."""
    stub = tmp_path / "fakepy"
    stub.mkdir(exist_ok=True)
    ran = tmp_path / "ran.txt"
    (stub / name).write_text(f'#!/bin/sh\ncase "$2" in *find_spec*) echo "{version}"; exit {find_rc};; esac\n'
                             f'echo "$@" >> {ran}\n')
    (stub / name).chmod(0o755)
    return stub, ran


def _env_events(p):
    return p.db.q("SELECT fingerprint, severity FROM events WHERE kind='observation' AND fingerprint LIKE 'env%'")


def test_ttp_checks_waits_on_a_python_that_cannot_import_pytest_and_counts_nothing(env, tmp_path, monkeypatch):
    """(a) The python of `python3 -m pytest` cannot import pytest: the checks exit 75 with the env-ok
    retry_when, write no check_failure.json, record env_fault, start no task and run nothing."""
    from ttp import cli, envfault
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    stub, ran = _fake_python(tmp_path, find_rc=3)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")
    tasks = p.db.one("SELECT COUNT(*) n FROM tasks")["n"]
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--fresh", "--", "python3 -m pytest -q"])
    assert e.value.code == 75
    assert not (run / "check_failure.json").exists()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["env_fault"] == "env:missing-runner:pytest"
    assert "--env-ok" in (run / "checks.log").read_text()
    assert not ran.exists(), "the check itself did not run"
    assert p.db.one("SELECT COUNT(*) n FROM tasks")["n"] == tasks, "no review or fix task"
    assert list(envfault.open_faults(p)) == ["env:missing-runner:pytest"]


def test_two_tasks_hitting_one_runner_fault_raise_one_observation(env, tmp_path, monkeypatch):
    """(b) Two tasks hit the same fault: one coordinator observation; kv env_faults lists both."""
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    stub, _ = _fake_python(tmp_path, find_rc=3)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")
    for task in ("11", "12"):
        monkeypatch.setenv("TTP_TASK", task)
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--fresh", "--", "python3 -m pytest -q"])
        assert e.value.code == 75
    assert len(_env_events(p)) == 1 and _env_events(p)[0]["severity"] == "high"
    assert p.db.kv("env_faults")["env:missing-runner:pytest"]["tasks"] == ["11", "12"]


def test_ttp_checks_with_its_runner_present_runs_as_before_and_records_no_fault(env, tmp_path, monkeypatch):
    """(c) A healthy runner: the check runs once, passes, no kv entry and no event."""
    from ttp import cli, envfault
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    stub, ran = _fake_python(tmp_path, find_rc=0)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")
    cli.main(["checks", "--fresh", "--", "python3 -m pytest -q"])
    assert json.loads((run / "checks.json").read_text())["passed"] is True
    from ttp import pushpass
    assert [ln for ln in ran.read_text().splitlines() if ln != " ".join(pushpass.VERSION_PROBE)] == ["-m pytest -q"]
    assert not envfault.open_faults(p) and not p.db.kv(envfault.KV) and not _env_events(p)


def test_a_typod_extra_command_fails_the_check_and_is_no_host_fault(env, tmp_path, monkeypatch):
    """A task's own extra command that is not on PATH is the command's failure: it fails as before,
    records no fault and raises no event (it must never hold the push queue)."""
    from ttp import cli, envfault
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    for cmd in ("no-such-program-ttp", "FOO=1 no-such-program-ttp -q && true", "timeout 5 no-such-program-ttp"):
        assert envfault.runners(cmd, repo) == []
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--fresh", "--", cmd])
        assert e.value.code not in (0, 75), cmd
        assert "not found" in (run / "checks.log").read_text()
    assert not p.db.kv("env_faults") and not _env_events(p)
    # The shared path's runners still are: a missing python, a bare pytest left as written.
    assert envfault.precheck(["python9.99 -m pytest"], repo)["sig"] == "env:missing-runner:python9.99"
    assert envfault.precheck(["pytest -q"], repo, {"PATH": str(tmp_path / "empty")})["sig"] == \
        "env:missing-runner:pytest"


def test_a_pytest_below_the_repositorys_minversion_is_an_environment_fault(tmp_path, monkeypatch):
    """The runner is there but older than the suite's pytest `minversion`: env:old-runner:pytest."""
    from ttp import envfault
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)   # pytest is not the repository's own
    (repo / "pyproject.toml").write_text('[project]\nname = "r"\n\n[tool.pytest.ini_options]\nminversion = "8.2"\n'
                                         'addopts = "-q"\n')
    stub, _ = _fake_python(tmp_path, find_rc=0, version="7.4.0")
    env = {**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}
    f = envfault.precheck(["python3 -m pytest -q"], repo, env)
    assert f and f["sig"] == "env:old-runner:pytest" and f["version"] == "7.4.0" and f["min"] == "8.2"
    assert "below the repository's minversion 8.2" in envfault.line(f)
    stub, _ = _fake_python(tmp_path, find_rc=0, version="8.3.1")
    assert envfault.precheck(["python3 -m pytest -q"], repo, env) is None
    (repo / "pyproject.toml").write_text('[project]\nname = "r"\n')   # no minversion: the version is not asked
    stub, _ = _fake_python(tmp_path, find_rc=0, version="1.0")
    assert envfault.precheck(["python3 -m pytest -q"], repo, env) is None
    (repo / "pytest.ini").write_text("[pytest]\nminversion = 6.0\n")
    assert envfault.minversion(repo) == "6.0"


def test_a_push_batch_whose_runner_is_missing_is_requeued_as_an_environment_fault(env, monkeypatch):
    """(d, the batch) A check's runner is missing on the host: the batch ends env_fault, every entry
    requeued, the fault recorded for the queue with one observation."""
    from ttp import envfault, push
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["python3 -m ttp_missing_runner_mod -q"])
    heads = [_entry(repo, "e1", {"plugins/p/f1.txt": "1\n"})]
    rc, m = _run_batch(p, _batch_marker(p, heads), monkeypatch)
    assert rc == push.BUSY and m["outcome"] == "env_fault" and _statuses(m) == ["requeued"], m
    faults = envfault.open_faults(p)
    assert list(faults) == ["env:missing-runner:ttp_missing_runner_mod"] and faults[
        "env:missing-runner:ttp_missing_runner_mod"]["queue"] is True
    assert len(_env_events(p)) == 1


def test_env_ok_with_a_sig_waits_only_on_that_fault_not_another_tasks(env, tmp_path, monkeypatch):
    """Two tasks hit two faults: each retry_when names its own sig, and once fault A's runner is back
    `ttp checks --env-ok <A>` exits 0 (closing A only) while B, never fixed, stays open."""
    from ttp import cli, envfault
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    sig = {m: "env:missing-runner:ttp_missing_runner_" + m for m in "ab"}
    for task, m in (("1", "a"), ("2", "b")):
        monkeypatch.setenv("TTP_TASK", task)
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--fresh", "--", f"python3 -m ttp_missing_runner_{m} -q"])
        assert e.value.code == 75
        assert f"checks --env-ok {sig[m]}" in (run / "checks.log").read_text()
    assert sorted(envfault.open_faults(p)) == [sig["a"], sig["b"]]
    mod = tmp_path / "mods"
    mod.mkdir()
    (mod / "ttp_missing_runner_a.py").write_text("")
    monkeypatch.setenv("PYTHONPATH", str(mod))   # A's runner is back; B's never is
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--env-ok", sig["a"]])
    assert e.value.code == 0
    assert list(envfault.open_faults(p)) == [sig["b"]], "only A closed"
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--env-ok", sig["b"]])
    assert e.value.code == 1
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--env-ok"])
    assert e.value.code == 1, "a bare --env-ok still waits on every open fault"
    assert envfault.retry_when(p, {"sig": sig["b"]}).endswith(f"checks --env-ok {sig['b']}")


def test_an_env_fault_batch_counts_no_tries_and_holds_the_queue_until_env_ok(env, monkeypatch):
    """(d, the queue) A batch that ended env_fault puts its approvals back with no try counted, and
    pushq.due() holds until `ttp checks --env-ok` passes, then resumes. A fault a task found in its
    own checkout, or one whose checkout is gone, holds nothing."""
    from ttp import cli, envfault, pushq
    s = _pq(env, monkeypatch)
    _pq_plan(s, outcome="env_fault", row="requeued")
    _pq_hand_off(env, s)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": 0}
    later = lambda: time.time() + 10 ** 6
    cmd = "python3 -m ttp_missing_runner_mod -q"
    f = envfault.precheck([cmd], s.repo)
    assert f and f["sig"] == "env:missing-runner:ttp_missing_runner_mod"
    envfault.record(s.p, f, "7")   # a task's own: the queue does not hold on it
    assert pushq.due(s.p, later())[0]
    envfault.record(s.p, f, queue=True)   # the batch's
    rows, why = pushq.due(s.p, later())
    assert rows == [] and why.startswith("held: environment fault env:missing-runner:ttp_missing_runner_mod"), why
    monkeypatch.chdir(s.p.root)
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--env-ok"])
    assert e.value.code == 1, "still missing"
    mod = env["tmp"] / "mods"
    mod.mkdir()
    (mod / "ttp_missing_runner_mod.py").write_text("")
    monkeypatch.setenv("PYTHONPATH", str(mod))   # the runner is back
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--env-ok"])
    assert e.value.code == 0 and not envfault.open_faults(s.p)
    assert pushq.due(s.p, later())[0], "resumes"
    # A queue fault whose checkout is gone (a removed worktree and its venv) closes instead of holding.
    monkeypatch.delenv("PYTHONPATH")
    gone = env["tmp"] / "gone"
    gone.mkdir()
    envfault.record(s.p, {**f, "cwd": str(gone), "python": str(gone / ".venv/bin/python")}, queue=True)
    gone.rmdir()
    assert pushq.due(s.p, later())[0] and not envfault.open_faults(s.p)


def test_a_long_queue_hold_on_an_environment_fault_raises_one_more_observation(env, monkeypatch):
    from ttp import envfault, pushq
    s = _pq(env, monkeypatch)
    _pq_hand_off(env, s)
    f = envfault.precheck(["python3 -m ttp_missing_runner_mod -q"], s.repo)
    envfault.record(s.p, f, queue=True)
    assert len(_env_events(s.p)) == 1
    soon = time.time() + 120
    assert pushq.due(s.p, soon)[1].startswith("held:") and len(_env_events(s.p)) == 1
    late = time.time() + envfault.HOLD_ALERT_S + 600
    for t in (late, late + 120):
        assert pushq.due(s.p, t)[1].startswith("held:")
    assert [e["fingerprint"].split(":")[0] for e in _env_events(s.p)] == ["env", "env-hold"]


# A push the remote refuses for a missing token scope (scopehold): the entries that need it are held,
# the rest pushes at once, and the held rows go back in the queue once the scope check passes.
_OAUTH = ("remote: error: refusing to allow an OAuth App to create or update workflow `.github/workflows/ci.yml` "
          "without `workflow` scope\nTo github.com:org/repo.git\n ! [remote rejected] proj -> proj "
          "(refusing to allow an OAuth App to create or update workflow `.github/workflows/ci.yml` without "
          "`workflow` scope)\nerror: failed to push some refs\n")


def test_a_scope_refusal_names_its_scope_and_files():
    from ttp import scopehold
    assert scopehold.classify(_OAUTH) == [{"scope": "workflow", "files": [".github/workflows/ci.yml"]}]
    pat = ("! [remote rejected] HEAD -> main (refusing to allow a Personal Access Token to create or update "
           "workflow `.github/workflows/a.yml` without `workflow` scope)")
    assert scopehold.classify(pat) == [{"scope": "workflow", "files": [".github/workflows/a.yml"]}]
    other = "remote: refusing to update `packages/x.tgz` without `write:packages` scope"
    assert scopehold.classify(other) == [{"scope": "write:packages", "files": ["packages/x.tgz"]}]
    assert scopehold.classify("! [remote rejected] proj -> proj (pre-receive hook declined)") == []
    assert scopehold.classify("") == []


def test_a_scope_refusal_matches_the_heads_that_change_its_paths_also_through_a_merge(env, monkeypatch):
    from ttp import scopehold
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    need = {"scope": "workflow", "files": [".github/workflows/ci.yml"]}
    tip = _git_out(repo, "rev-parse", "origin/proj")
    plain = _entry(repo, "e1", {"a.txt": "a\n"})
    flow = _entry(repo, "e2", {".github/workflows/other.yml": "on: push\n"})
    named = _entry(repo, "e3", {"docs/x.md": "x\n"})
    _git_out(repo, "checkout", "-q", "-B", "side", "origin/proj")
    (repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    _commit(repo, ".github/workflows/ci.yml", "on: push\n")
    _git_out(repo, "checkout", "-q", "-B", "e4", plain)
    _git_out(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    merged = _git_out(repo, "rev-parse", "HEAD")
    got = {h: scopehold.needs(scopehold.touched(repo, tip, h), need) for h in (plain, flow, named, merged)}
    assert got == {plain: [], flow: [".github/workflows/other.yml"], named: [],
                   merged: [".github/workflows/ci.yml"]}
    assert scopehold.needs(["pkg/x.tgz", "a.txt"], {"scope": "write:packages", "files": ["pkg/x.tgz"]}) == ["pkg/x.tgz"]


def _scope_hook(origin):
    """The bare origin refuses, as GitHub does without the `workflow` scope, any push that changes a
    workflow file."""
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nwhile read old new ref; do\n"
                    "  for f in $(git diff --name-only \"$old\" \"$new\" 2>/dev/null); do case $f in .github/workflows/*)\n"
                    "    echo \"error: refusing to allow an OAuth App to create or update workflow \\`$f\\` without "
                    "\\`workflow\\` scope\" >&2; exit 1;; esac; done\ndone\nexit 0\n")
    hook.chmod(0o755)


def test_a_push_batch_holds_the_heads_a_scope_refusal_names_and_pushes_the_rest_at_once(env, monkeypatch):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.push_rounds", 1)
    _scope_hook(origin)
    heads = [_entry(repo, "e1", {"a.txt": "a\n"}), _entry(repo, "e2", {".github/workflows/ci.yml": "on: push\n"}),
             _entry(repo, "e3", {"b.txt": "b\n"})]
    rc, m = _run_batch(p, _batch_marker(p, heads), monkeypatch)
    assert m["outcome"] == "pushed", m.get("message")
    assert [r["status"] for r in m["results"]] == ["pushed", "scope_held", "pushed"]
    assert m["results"][1]["detail"]["scope"] == "workflow"
    assert m["results"][1]["detail"]["files"] == [".github/workflows/ci.yml"]
    [s] = m["scope_held"]
    assert s["scope"] == "workflow" and s["entries"] == [2] and "gh auth status" in s["check"]
    files = _git_out(origin, "ls-tree", "-r", "--name-only", "proj").split()
    assert "a.txt" in files and "b.txt" in files and ".github/workflows/ci.yml" not in files


def _scope_pq(env, monkeypatch, check="false"):
    """_pq whose stub batch holds the review's row for the `workflow` scope, the check `check`."""
    s = _pq(env, monkeypatch, push_scope_check=check, push_scope_probe_s=60)
    _pq_plan(s, outcome="pushed", row="scope_held",
             detail={"scope": "workflow", "files": [".github/workflows/ci.yml"], "check": check})
    _pq_hand_off(env, s)
    _pq_batch(s)
    _pq_tend(s)
    return s


def _scope_alerts(p):
    return [m["text"] for m in p.db.q("SELECT text FROM messages WHERE kind='alert' AND ref='push_scope:workflow'")]


def test_a_held_push_raises_one_alert_and_requeues_once_the_scope_check_passes(env, monkeypatch):
    from ttp import alerts, pushq, scopehold
    s = _scope_pq(env, monkeypatch)
    [row] = s.p.db.q("SELECT * FROM push_queue WHERE task=?", (s.review,))
    assert row["status"] == "held"
    t = s.p.db.task(s.review)
    assert t["status"] == "pushing" and "`workflow`" in t["blocked_reason"]
    [text] = _scope_alerts(s.p)
    for want in ("lacks the `workflow` scope", ".github/workflows/ci.yml", f"#{s.review} review feature",
                 "Nothing else was in the batch.", "`gh auth refresh -s workflow`", "once `false` passes"):
        assert want in text, (want, text)
    assert pushq.tend_scope_holds(s.p, now=time.time() + 120) == []   # the check fails: still held
    assert s.p.db.one("SELECT status FROM push_queue WHERE id=?", (row["id"],))["status"] == "held"
    assert s.p.db.kv(scopehold.KV)["workflow"]["probe"]
    _pq_tend(s)
    assert len(_scope_alerts(s.p)) == 1, "one alert per condition"
    s.p.set_config("delivery.push_scope_check", "true")
    assert pushq.tend_scope_holds(s.p, now=time.time() + 10) == [], "not before the next probe is due"
    assert pushq.tend_scope_holds(s.p, now=time.time() + 400) == [row["id"]]
    assert s.p.db.one("SELECT status FROM push_queue WHERE id=?", (row["id"],))["status"] == "approved"
    assert not s.p.db.task(s.review)["blocked_reason"]
    assert alerts.holds(s.p.db, "push_scope:workflow", time.time() - 60, time.time()), "until it pushes"
    s.p.db.x("UPDATE push_queue SET status='pushed' WHERE id=?", (row["id"],))
    pushq.tend_scope_holds(s.p)
    assert not alerts.holds(s.p.db, "push_scope:workflow", time.time() - 60, time.time())
    assert len(_scope_alerts(s.p)) == 1


def test_the_push_queue_status_names_the_held_rows(env, monkeypatch):
    from ttp import pushq
    s = _scope_pq(env, monkeypatch)
    [h] = pushq.summary(s.p)["held"]
    assert h["task"] == s.review and h["scope"] == "workflow"
    assert "held for the token scope `workflow`" in pushq.status_line(s.p)


def test_a_held_push_row_shows_in_the_start_gate_and_counts_as_delivered(env, monkeypatch):
    from ttp import reviewreuse, startgate
    p = _pq(env, monkeypatch).p
    t = p.db.add_task("Producer", "x", kind="code", tier="light", origin="coordinator", provider="claude",
                      budget_usd=1.0)
    p.db.x("INSERT INTO push_queue(task,run,branch,head,target,status,detail,created,updated) "
           "VALUES(?,1,'b','abc123','origin/proj','held',?,0,0)", (t, json.dumps({"scope": "workflow"})))
    assert startgate.push_state(p.db, f"ttp landed --task {t}") == "held for the token scope workflow"
    assert reviewreuse.delivered(p, "abc123")


def test_version_bump_changeset_file_keeps_one_release_changeset(tmp_path):
    """`changeset_file` appends the batch's subjects to one release changeset (no duplicates), creates
    it when missing, leaves a batch's own edit of it alone, and unset keeps one file per branch."""
    import subprocess
    from ttp import push
    g = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q", "-b", "main")
    g("config", "user.email", "a@b.c")
    g("config", "user.name", "t")
    (tmp_path / "plug").mkdir()
    (tmp_path / "plug" / "p.json").write_text('{"name": "pk", "version": "1.0.0"}\n')
    (tmp_path / "plug" / "x.txt").write_text("0\n")
    (tmp_path / ".changeset").mkdir()
    (tmp_path / ".changeset" / "pk-release.md").write_text('---\n"pk": patch\n---\n\n`pk`:\n\n- old thing\n')
    g("add", "-A")
    g("commit", "-qm", "base")
    tip = g("rev-parse", "HEAD")
    base = {"files": ["plug/p.json"], "changeset_dir": ".changeset"}
    cfg = push.bump_of({**base, "changeset_file": ".changeset/pk-release.md"})
    assert cfg["changeset_file"] == ".changeset/pk-release.md"
    for bad in ("../x.md", "/x.md", "x.txt"):
        with pytest.raises(ValueError):
            push.bump_of({**base, "changeset_file": bad})

    def batch(*subjects, extra=None):
        g("reset", "-q", "--hard", tip)
        for i, s in enumerate(subjects):
            (tmp_path / "plug" / "x.txt").write_text(f"{s}{i}\n")
            g("commit", "-qam", f"pk: {s}")
        if extra:
            extra()
            g("add", "-A")
            g("commit", "-qm", "own changeset")

    batch("new a", "old thing", "new b")
    assert push.bump(tmp_path, tip, cfg, lambda m: None) == 0
    text = (tmp_path / ".changeset" / "pk-release.md").read_text()
    assert text.count("- old thing") == 1 and "- new a" in text and "- new b" in text
    assert sorted(p.name for p in (tmp_path / ".changeset").iterdir()) == ["pk-release.md"]
    assert '"version": "1.0.1"' in (tmp_path / "plug" / "p.json").read_text()
    # the same batch again (its bump commit dropped, the appended changeset kept) adds nothing
    before = text
    g("reset", "-q", "HEAD~1")
    g("checkout", "--", "plug/p.json")
    assert push.bump(tmp_path, tip, cfg, lambda m: None) == 0
    assert (tmp_path / ".changeset" / "pk-release.md").read_text() == before
    # missing file: created
    batch("only")
    g("rm", "-q", ".changeset/pk-release.md")
    g("commit", "-qm", "drop")
    cfg2 = push.bump_of({**base, "changeset_file": ".changeset/pk-other.md"})
    batch("only")
    assert push.bump(tmp_path, tip, cfg2, lambda m: None) == 0
    assert "- only" in (tmp_path / ".changeset" / "pk-other.md").read_text()
    # batch bringing its own edit of the release changeset: untouched
    own = lambda: (tmp_path / ".changeset" / "pk-release.md").write_text("own\n")
    batch("mine", extra=own)
    assert push.bump(tmp_path, tip, cfg, lambda m: None) == 0
    assert (tmp_path / ".changeset" / "pk-release.md").read_text() == "own\n"
    # unset: one file per branch, as before
    batch("plain")
    assert push.bump(tmp_path, tip, push.bump_of(base), lambda m: None) == 0
    assert (tmp_path / ".changeset" / "pk-main.md").is_file()


def test_a_push_batch_with_changeset_file_adds_to_the_release_changeset(env, monkeypatch):
    """With `changeset_file` set the push queue's bump keeps one release changeset: the first batch
    creates it, later ones append only new lines (none twice), an entry that edits it itself brings
    its own, and no per-batch file is written. Unset keeps <package>-<batch>.md (the batch test above)."""
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["true"])
    rel = ".changeset/p-release.md"
    p.set_config("delivery.version_bump", {"files": ["plugins/p/.claude-plugin/plugin.json", "plugins/p/rt/__init__.py"],
                                           "changeset_dir": ".changeset", "changeset_file": rel})
    sets = lambda: _git_out(origin, "ls-tree", "--name-only", "proj", ".changeset/").split()
    heads = [_entry(repo, f"e{n}", {f"plugins/p/f{n}.txt": f"{n}\n"}) for n in (1, 2)]
    rc, m = _run_batch(p, _batch_marker(p, heads, bid="b1"), monkeypatch)
    assert rc == 0 and _statuses(m) == ["pushed"] * 2, m
    assert sets() == [rel]
    assert _git_out(origin, "show", f"proj:{rel}") == (
        '---\n"p": patch\n---\n\n`p`:\n\n- e1: edit plugins/p/f1.txt\n- e2: edit plugins/p/f2.txt')
    heads = [_entry(repo, "e1", {"plugins/p/f1.txt": "again\n"}), _entry(repo, "e3", {"plugins/p/f3.txt": "3\n"})]
    rc, m = _run_batch(p, _batch_marker(p, heads, bid="b2"), monkeypatch)
    assert rc == 0 and _statuses(m) == ["pushed"] * 2, m
    assert sets() == [rel] and _versions(origin) == ("0.1.2", "0.1.2")
    assert _git_out(origin, "show", f"proj:{rel}").endswith(
        "\n\n- e1: edit plugins/p/f1.txt\n- e2: edit plugins/p/f2.txt\n- e3: edit plugins/p/f3.txt")
    own = _git_out(origin, "show", f"proj:{rel}") + "\n- written by hand\n"
    heads = [_entry(repo, "e4", {"plugins/p/f4.txt": "4\n", rel: own}), _entry(repo, "e5", {"plugins/p/f5.txt": "5\n"})]
    rc, m = _run_batch(p, _batch_marker(p, heads, bid="b3"), monkeypatch)
    assert rc == 0 and _statuses(m) == ["pushed"] * 2, m
    text = _git_out(origin, "show", f"proj:{rel}")
    assert sets() == [rel] and "e4:" not in text and text.endswith("\n- written by hand\n- e5: edit plugins/p/f5.txt")


def test_a_code_tasks_run_never_pushes_to_the_push_branch_while_code_tasks_may_push_is_off(env, monkeypatch, capsys):
    # Replays `ttp doctor --loop claude`: with delivery.code_tasks_may_push off (push queue and review on),
    # a light code worker whose spec said "land it on the push branch" pushed its commit there itself
    # within seconds, before any review. Neither `ttp push` (even with --land) nor a raw `git push`
    # through the run's git wrapper may land it; its own branch still publishes, and the repository's
    # own pre-push hook still runs.
    from ttp import push
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    shared = _git_out(origin, "rev-parse", "proj")
    tid = p.db.add_task("notes", kind="code",
                        spec="Add a line to NOTES.md, commit it and land it on the push branch proj through the review.")
    own = f"ttp/t{tid}-notes"
    _git_out(repo, "checkout", "-q", "-b", own)
    _commit(repo, "NOTES.md", "loop check\n")
    _record_checks_pass(p, tid, repo)
    run_dir = env["tmp"] / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_TASK", str(tid))
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    for args in (["push"], ["push", "--detach"], ["push", "--land"], ["push", "--land", "--detach"]):
        assert _ttp(*args) == 2, args
        assert "delivery.code_tasks_may_push is off" in capsys.readouterr().err, args
        monkeypatch.delenv(push.LAND_ENV, raising=False)   # cmd_push sets it in os.environ
    assert not (p.state / "pushes").exists() and _git_out(origin, "rev-parse", "proj") == shared
    # A raw `git push` in the run goes through the harness's bin/git, first on the run's PATH.
    wrapper = RUNTIME.parent / "template" / "bin" / "git"
    hooks = repo / _git_out(repo, "rev-parse", "--git-path", "hooks")
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-push").write_text(f"#!/bin/sh\ncat >/dev/null\necho \"$1\" >> {env['tmp'] / 'own-hook'}\n")
    (hooks / "pre-push").chmod(0o755)
    run_env = {**os.environ, "TTP_PYTHON": sys.executable}

    def git_push(*args):
        return subprocess.run([str(wrapper), "push", *args], cwd=repo, env=run_env, capture_output=True,
                              text=True, timeout=60)

    for args in (["origin", "HEAD:proj"], ["-q", "origin", "HEAD:refs/heads/proj"], ["--no-verify", "origin", "HEAD:proj"],
                 ["origin", f"+{own}:proj"], ["origin", ":proj"], ["-u", "origin", f"{own}:proj", own]):
        out = git_push(*args)
        assert out.returncode != 0 and "delivery.code_tasks_may_push is off" in out.stderr, (args, out.stderr)
    assert _git_out(origin, "rev-parse", "proj") == shared
    assert not (env["tmp"] / "own-hook").exists() or "--no-verify" not in (env["tmp"] / "own-hook").read_text()
    # Claude Code's PreToolUse hook denies git called by its path to push (it skips bin/git).
    from ttp import hook
    deny = lambda c: (hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": c}})[0] or {}).get(
        "hookSpecificOutput", {}).get("permissionDecision")
    assert deny("/usr/bin/git push origin HEAD:proj") == "deny"
    assert deny("cd x && env /usr/bin/git -C . push") == "deny"
    assert deny("git push origin HEAD:proj") is None and deny("/usr/bin/git log --oneline") is None
    out = git_push("origin", own)
    assert out.returncode == 0, out.stderr
    assert _git_out(origin, "rev-parse", own) == _git_out(repo, "rev-parse", "HEAD")
    assert "origin" in (env["tmp"] / "own-hook").read_text()   # the repository's own pre-push hook ran
    # A review, or code_tasks_may_push on, lands as before.
    review = p.db.add_task("review", kind="review")
    monkeypatch.setenv("TTP_TASK", str(review))
    assert push.resolve(p, repo) == ("origin", "proj", False)
    monkeypatch.setenv("TTP_TASK", str(tid))
    run_env["TTP_TASK"] = str(tid)
    p.set_config("delivery.code_tasks_may_push", True)
    assert push.resolve(p, repo) == ("origin", "proj", False)
    assert deny("/usr/bin/git push origin HEAD:proj") is None
    out = git_push("origin", "HEAD:proj")
    assert out.returncode == 0, out.stderr
    assert _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")


def test_the_run_git_wrapper_skips_other_wrappers_on_path_and_keeps_its_hook(env, monkeypatch):
    # Two harnesses' bin/ on one PATH (a scratch project's daemon started from a run, as
    # `ttp doctor --loop` does) made each wrapper exec the other forever: every git call hung.
    # A caller's own `-c core.hooksPath=<dir>` also replaced the guard's hook.
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    shared = _git_out(origin, "rev-parse", "proj")
    tid = p.db.add_task("notes", kind="code", spec="Add a line to NOTES.md and commit it.")
    _git_out(repo, "checkout", "-q", "-b", f"ttp/t{tid}-notes")
    _commit(repo, "NOTES.md", "loop check\n")
    run_dir = env["tmp"] / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_TASK", str(tid))
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    bins = []
    for name in ("h1", "h2"):
        (env["tmp"] / name / "bin").mkdir(parents=True)
        (env["tmp"] / name / "runtime").symlink_to(RUNTIME)
        shutil.copy2(RUNTIME.parent / "template" / "bin" / "git", env["tmp"] / name / "bin" / "git")
        bins.append(str(env["tmp"] / name / "bin"))
    run_env = {**os.environ, "TTP_PYTHON": sys.executable, "PATH": os.pathsep.join([*bins, os.environ["PATH"]])}
    own_hooks = env["tmp"] / "own-hooks"
    own_hooks.mkdir()
    (own_hooks / "pre-push").write_text(f"#!/bin/sh\ncat >/dev/null\necho ran >> {env['tmp'] / 'own-hook'}\n")
    (own_hooks / "pre-push").chmod(0o755)

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, env=run_env, capture_output=True, text=True, timeout=20)

    out = git("--version")
    assert out.returncode == 0 and out.stdout.startswith("git version"), out.stderr
    for args in (["push", "origin", "HEAD:proj"], ["-c", f"core.hooksPath={own_hooks}", "push", "origin", "HEAD:proj"],
                 ["-c", f"core.hooksPath={own_hooks}", "push", "--no-verify", "origin", "HEAD:proj"]):
        out = git(*args)
        assert out.returncode != 0 and "delivery.code_tasks_may_push is off" in out.stderr, (args, out.stderr)
    assert _git_out(origin, "rev-parse", "proj") == shared
    out = git("-c", f"core.hooksPath={own_hooks}", "push", "origin", f"ttp/t{tid}-notes")
    assert out.returncode == 0, out.stderr
    assert (env["tmp"] / "own-hook").read_text() == "ran\n"   # the caller's own hook still ran


def _land_setup(env, monkeypatch, checks):
    """A remote-less project whose push branch is local branch `proj`, a review task, and a branch
    `work` one commit ahead of `proj` checked out in the repo: (land, p, repo, review id, head)."""
    from ttp import land
    p, repo = _local_push_setup(env, monkeypatch, checks)
    t = p.db.add_task("review", "s", kind="review")
    _git_out(repo, "checkout", "-q", "-b", "work")
    _commit(repo, "a.txt", "a\n")
    return land, p, repo, t, _git_out(repo, "rev-parse", "HEAD")


def _worktrees(repo):
    return [ln for ln in _git_out(repo, "worktree", "list", "--porcelain").splitlines() if ln.startswith("worktree ")]


def _on_proj(repo, name, text):
    """A commit on `proj` made from the repo's checkout, which goes back to `work`."""
    _git_out(repo, "checkout", "-q", "proj")
    _commit(repo, name, text)
    _git_out(repo, "checkout", "-q", "work")
    return _git_out(repo, "rev-parse", "proj")


def test_ttp_land_replays_a_reviewed_head_onto_a_moved_local_branch_and_records_it(env, monkeypatch, capsys):
    """A review of a project whose push branch is a local branch with no git remote gets a `ttp land`
    step. `ttp land` replays the reviewed head onto the branch's new tip (each commit naming the one
    it replays), fast-forwards the branch, and records the landing: `landed:#<code>` passes, the
    review's verdict covers the new commit and the original head counts as delivered. Without the
    local branch the review stays review only (the test above)."""
    from ttp import cli, land, push, pushq, reviewreuse
    from ttp.project import config_problems
    for var, val in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                     ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(var, val)
    p = make(env)
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    _git_out(p.root, "branch", "work")
    p.set_config("delivery.push_branch", "work")
    p.set_config("delivery.push_checks", ["true"])
    assert "reviews land on it with `ttp land`" in push.push_branch_problem("work", p.root)
    # A valid setup, not a config problem (no low alert); `ttp doctor` still says how reviews land.
    assert config_problems({"delivery": {"push_branch": "work"}}, p.root) == []
    assert push.lands_locally("work", p.root) and not push.lands_locally("gone", p.root)
    assert "review only" in config_problems({"delivery": {"push_branch": "gone"}}, p.root)[0]
    cli.main(["doctor", p.name])
    assert "reviews land on it with `ttp land`" in capsys.readouterr().out
    for queue in (False, True):
        p.set_config("delivery.push_queue", queue)
        cid, branch, _, (rev,) = _finish_code(env, p, f"landme{queue}", {f"landme{queue}.py": 3})
        labels = json.loads(rev["labels"])
        assert land.STEP in rev["spec"] and push.LAND_LABEL in labels, rev["spec"][-800:]
        assert "ttp push` from" not in rev["spec"] and "Review only" not in rev["spec"]
        assert reviewreuse.pushes(p.config(), labels)
    head = _git_out(p.root, "rev-parse", branch)
    # `work` moves on before the landing, in a worktree that is then gone.
    side = env["tmp"] / "side"
    _git_out(p.root, "worktree", "add", "-q", str(side), "work")
    _commit(side, "other.txt", "other\n")
    _git_out(p.root, "worktree", "remove", str(side))
    tip = _git_out(p.root, "rev-parse", "work")
    key = reviewreuse.check_key(p.config())
    p.db.update_task(rev["id"], status="done", labels=reviewreuse.with_head(labels, head, key))
    n = len(_worktrees(p.root))
    said = []
    assert land.land(p, p.root, head, rev["id"], said.append) == 0, said
    new = _git_out(p.root, "rev-parse", "work")
    assert new != head and _git_out(p.root, "rev-parse", f"{new}^") == tip
    assert f"Ttp-Replayed-From: {head}" in _git_out(p.root, "log", "-1", "--format=%B", new)
    assert [r["sha"] for r in p.db.q("SELECT sha FROM task_landings WHERE task=?", (rev["id"],))] == [new]
    assert len(_worktrees(p.root)) == n
    with pytest.raises(SystemExit) as e:
        cli.main(["landed", "--task", str(cid)])
    assert e.value.code == 0
    assert reviewreuse.accepted(p.db, new, key)["id"] == rev["id"]
    assert reviewreuse.delivered(p, head)
    # Landing it again is a no-op that still exits 0.
    assert land.land(p, p.root, head, rev["id"], said.append) == 0 and _git_out(p.root, "rev-parse", "work") == new


def test_ttp_land_a_conflict_or_a_failing_check_lands_nothing(env, monkeypatch):
    """A conflict exits 3 and a failing check 4 (a check's own exit 3 too): the branch keeps its tip,
    no scratch worktree is left and no landing is recorded."""
    land, p, repo, t, head = _land_setup(env, monkeypatch, ["true"])
    tip = _on_proj(repo, "a.txt", "other\n")
    n = len(_worktrees(repo))
    said = []
    assert land.land(p, repo, head, t, said.append) == 3, said
    assert "conflict replaying" in said[0] and "a.txt" in said[0], said
    assert _git_out(repo, "rev-parse", "proj") == tip and len(_worktrees(repo)) == n
    assert not p.db.q("SELECT * FROM task_landings")
    _git_out(repo, "branch", "-f", "proj", f"{head}^")
    for checks in (["false"], ["exit 3"]):
        p.set_config("delivery.push_checks", checks)
        said = []
        assert land.land(p, repo, head, t, said.append) == 4, (checks, said)
        assert _git_out(repo, "rev-parse", "proj") == _git_out(repo, "rev-parse", f"{head}^")
        assert len(_worktrees(repo)) == n and not p.db.q("SELECT * FROM task_landings")


def test_ttp_land_drops_a_commit_already_on_the_branch_and_tells_other_git_failures_from_conflicts(
        env, monkeypatch):
    """A commit whose change the branch already has under another commit becomes empty on its tip: it
    is dropped, not reported as a conflict. A cherry-pick that fails with no unmerged paths exits 1."""
    land, p, repo, t, head = _land_setup(env, monkeypatch, ["true"])
    _commit(repo, "b.txt", "b\n")
    head = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "checkout", "-q", "proj")      # a.txt's change inside a bigger commit (another patch)
    (repo / "z.txt").write_text("z\n")
    _git_out(repo, "add", "z.txt")
    _commit(repo, "a.txt", "a\n")
    _git_out(repo, "checkout", "-q", "work")
    tip = _git_out(repo, "rev-parse", "proj")
    said = []
    assert land.land(p, repo, head, t, said.append) == 0, said
    new = _git_out(repo, "rev-parse", "proj")
    assert any(m.startswith(f"dropped {_git_out(repo, 'rev-parse', 'work^')[:10]}") for m in said), said
    assert _git_out(repo, "rev-parse", f"{new}^") == tip                            # b.txt alone
    assert _git_out(repo, "show", "--name-only", "--format=", new) == "b.txt"
    # Every commit already there in another form: nothing to replay, landed at the tip.
    _git_out(repo, "checkout", "-q", "-b", "again", f"{tip}^")
    _commit(repo, "a.txt", "a\n")
    again = _git_out(repo, "rev-parse", "HEAD")
    said = []
    assert land.land(p, repo, again, t, said.append) == 0 and _git_out(repo, "rev-parse", "proj") == new, said
    assert "already on local branch proj" in said[-1], said
    # A git failure that is not a conflict.
    _git_out(repo, "checkout", "-q", "work")
    _commit(repo, "d.txt", "d\n")
    real = land._git

    def broken(where, *args, **kw):
        if args[:1] == ("cherry-pick",) and "--abort" not in args:
            return subprocess.CompletedProcess(args, 128, "", "fatal: something else broke")
        return real(where, *args, **kw)
    monkeypatch.setattr(land, "_git", broken)
    said = []
    assert land.land(p, repo, _git_out(repo, "rev-parse", "HEAD"), t, said.append) == 1, said
    assert _git_out(repo, "rev-parse", "proj") == new and "not a conflict" in " ".join(said)


def test_ttp_land_starts_over_while_the_branch_moves_and_stops_after_its_tries(env, monkeypatch):
    """The branch moving during the checks starts the landing over on its new tip; one that keeps
    moving exits 5 after MOVE_TRIES. A dirty checkout that has the branch exits 6 and lands once clean."""
    land, p, repo, t, head = _land_setup(env, monkeypatch, ["true"])
    real, moves = land._checks, []

    def moving(p_, wt, new, base, say):
        if len(moves) < want:
            moves.append(_on_proj(repo, f"m{len(moves)}-{want}.txt", "m\n"))
        return real(p_, wt, new, base, say)
    monkeypatch.setattr(land, "_checks", moving)
    want = 1
    said = []
    assert land.land(p, repo, head, t, said.append) == 0, said
    assert _git_out(repo, "rev-parse", "proj^") == moves[0] and "starting over" in " ".join(said)
    _commit(repo, "e.txt", "e\n")
    head, moves[:], want = _git_out(repo, "rev-parse", "HEAD"), [], land.MOVE_TRIES
    said = []
    assert land.land(p, repo, head, t, said.append) == 5, said
    assert _git_out(repo, "rev-parse", "proj") == moves[-1]
    monkeypatch.setattr(land, "_checks", real)
    side = env["tmp"] / "side"
    _git_out(repo, "worktree", "add", "-q", str(side), "proj")
    (side / "x.txt").write_text("x")
    _git_out(side, "add", "x.txt")
    said = []
    assert land.land(p, repo, head, t, said.append) == 6, said
    _git_out(side, "reset", "-q", "--hard")
    assert land.land(p, repo, head, t, said.append) == 0, said
    assert _git_out(side, "rev-parse", "HEAD") == _git_out(repo, "rev-parse", "proj")


def test_ttp_land_reads_git_in_any_locale_and_starts_over_when_the_branch_moves_under_its_fast_forward(
        env, monkeypatch, tmp_path):
    """A commit that becomes empty is dropped under a user locale whose git messages are not English
    (a `git` stand-in that translates unless LC_ALL=C). The branch moving in the checkout that has it,
    between the tip check and the fast-forward there, is a moved branch (start over), not exit 6."""
    import shlex
    land, p, repo, t, head = _land_setup(env, monkeypatch, ["true"])
    _commit(repo, "b.txt", "b\n")
    head = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "checkout", "-q", "proj")      # a.txt's change inside a bigger commit: empty on proj
    (repo / "z.txt").write_text("z\n")
    _git_out(repo, "add", "z.txt")
    _commit(repo, "a.txt", "a\n")
    _git_out(repo, "checkout", "-q", "work")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "git").write_text(f"""#!/bin/sh
PATH={shlex.quote(os.environ["PATH"])}; export PATH
[ "$LC_ALL" = C ] && exec git "$@"
o=$(mktemp) e=$(mktemp)
git "$@" >"$o" 2>"$e"; rc=$?
sed 's/empty/leer/g' "$o"; sed 's/empty/leer/g' "$e" >&2; rm -f "$o" "$e"; exit $rc
""")
    (shim / "git").chmod(0o755)
    side = env["tmp"] / "side"
    _git_out(repo, "worktree", "add", "-q", str(side), "proj")
    real, moved = land._git, []

    def racing(where, *args, **kw):
        if args[:2] == ("merge", "--ff-only") and not moved:
            _commit(side, "m.txt", "m\n")
            moved.append(_git_out(repo, "rev-parse", "proj"))
        return real(where, *args, **kw)
    monkeypatch.setattr(land, "_git", racing)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    said = []
    assert land.land(p, repo, head, t, said.append) == 0, said
    assert any(m.startswith("dropped ") for m in said) and "starting over" in " ".join(said), said
    assert _git_out(repo, "rev-parse", "proj^") == moved[0] == _git_out(side, "rev-parse", "HEAD^")
    assert _git_out(repo, "show", "--name-only", "--format=", "proj") == "b.txt"


def test_ttp_land_waits_for_another_landing_and_runs_from_the_cli(env, monkeypatch):
    """A held landing lock exits 75; `ttp land <head>` lands for the run's own task (TTP_TASK)."""
    from ttp import cli, push
    land, p, repo, t, head = _land_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.push_wait_s", 0)
    held = push.take(p, push.LOCAL, "proj", 0)
    said = []
    try:
        assert land.land(p, repo, head, t, said.append) == 75, said
    finally:
        held.close()
    assert _git_out(repo, "rev-parse", "proj") != head
    monkeypatch.setenv("TTP_TASK", str(t))
    with pytest.raises(SystemExit) as e:
        cli.main(["land", head])
    assert e.value.code == 0 and _git_out(repo, "rev-parse", "proj") == head
    assert p.db.one("SELECT task FROM task_landings")["task"] == t


def _repo_state(repo):
    """Everything a dry run must leave alone: refs, worktrees, objects and the index."""
    objects = sorted(str(x.relative_to(repo)) for x in (repo / ".git" / "objects").rglob("*") if x.is_file())
    return (_git_out(repo, "for-each-ref", "--format=%(refname) %(objectname)"), _worktrees(repo), objects,
            (repo / ".git" / "index").read_bytes(), _git_out(repo, "status", "--porcelain"))


def test_ttp_land_dry_run_reports_a_clean_landing_or_a_conflict_and_writes_nothing(env, monkeypatch):
    """`ttp land --dry-run` replays in a throwaway clone: a clean head exits 0 naming the checks it would
    run (without running them) and the records it would write, a conflict exits 3, and neither moves a
    ref, adds an object or worktree, writes a record or takes the landing lock."""
    from ttp import push
    log = env["tmp"] / "checked"
    land, p, repo, t, head = _land_setup(env, monkeypatch, [f"echo ran >> {log}"])
    tip = _on_proj(repo, "b.txt", "b\n")
    before = _repo_state(repo)
    monkeypatch.setattr(push, "take", lambda *a, **k: pytest.fail("a dry run took the landing lock"))
    said = []
    assert land.land(p, repo, head, t, said.append, dry=True) == 0, said
    text = "\n".join(said)
    assert "replays 1 commit(s)" in text and f"echo ran >> {log}" in text and "not run now" in text, text
    assert f"would record a landing of task #{t}" in text and "nothing written" in said[-1], text
    assert not log.exists() and _repo_state(repo) == before
    assert not p.db.q("SELECT * FROM task_landings") and _git_out(repo, "rev-parse", "proj") == tip
    _on_proj(repo, "a.txt", "other\n")
    before = _repo_state(repo)
    said = []
    assert land.land(p, repo, head, t, said.append, dry=True) == 3, said
    assert "conflict replaying" in said[0] and "a.txt" in said[0] and "would exit 3" in said[-1], said
    assert _repo_state(repo) == before and not p.db.q("SELECT * FROM task_landings")
    # Its branch checked out with uncommitted changes: 6, as the landing would.
    _git_out(repo, "branch", "-f", "proj", tip)
    side = env["tmp"] / "side"
    _git_out(repo, "worktree", "add", "-q", str(side), "proj")
    (side / "b.txt").write_text("dirty\n")
    said = []
    assert land.land(p, repo, head, t, said.append, dry=True) == 6, said


def test_ttp_land_root_lands_in_another_repo_and_the_cli_dry_run_changes_nothing(env, monkeypatch, capsys):
    """`ttp land --root <repo>` works on that repo, not the current directory's: a temporary clone
    with its remote removed takes a dry run and then the landing while the project's repo keeps its
    branch; a missing --root is refused. --help says the push checks run on the replayed head. The
    clone is not the project's repo, so neither run records a landing or a review label for the task
    in the project's database; --root at the project's own repo still would."""
    from ttp import cli, reviewreuse
    land, p, repo, t, head = _land_setup(env, monkeypatch, ["true"])
    p.db.update_task(t, labels=reviewreuse.with_head([], head, reviewreuse.check_key(p.config())))
    labels = p.db.task(t)["labels"]
    tip = _on_proj(repo, "b.txt", "b\n")
    clone = env["tmp"] / "try"
    _git_out(env["tmp"], "clone", "-q", str(repo), str(clone))
    _git_out(clone, "remote", "remove", "origin")
    _git_out(clone, "branch", "proj", tip)
    monkeypatch.chdir(env["tmp"])
    before = _repo_state(clone)
    with pytest.raises(SystemExit) as e:
        cli.main(["land", head, "--root", str(clone), "--task", str(t), "--dry-run"])
    assert e.value.code == 0, capsys.readouterr().err
    err = capsys.readouterr().err
    assert "nothing written" in err and "would not record the landing" in err and "would record" not in err, err
    assert _repo_state(clone) == before and not p.db.q("SELECT * FROM task_landings")
    with pytest.raises(SystemExit) as e:
        cli.main(["land", head, "--root", str(clone), "--task", str(t)])
    assert e.value.code == 0, capsys.readouterr().err
    assert "not recording the landing" in capsys.readouterr().err
    assert _git_out(clone, "rev-parse", "proj^") == tip and _git_out(repo, "rev-parse", "proj") == tip
    assert not p.db.q("SELECT * FROM task_landings") and p.db.task(t)["labels"] == labels
    with pytest.raises(SystemExit) as e:
        cli.main(["land", head, "--root", str(repo), "--task", str(t), "--dry-run"])
    err = capsys.readouterr().err
    assert e.value.code == 0 and "would record a landing of task" in err and "not record" not in err, err
    with pytest.raises(SystemExit) as e:
        cli.main(["land", head, "--root", str(env["tmp"] / "nope")])
    assert e.value.code == 2 and "no such directory" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["land", "--help"])
    out = capsys.readouterr().out
    assert "--dry-run" in out and "--root" in out and "push checks" in out


def test_ttp_land_root_lands_on_master_in_a_temp_clone_and_the_projects_own_master_needs_the_flag(
        env, monkeypatch, capsys):
    """delivery.push_branch master without delivery.allow_protected_push_branch: `ttp land` in the
    project's own repo refuses, naming the setting and why. `--root` at a remote-less temporary clone
    lands on the clone's master (from the CLI, recording nothing), a conflict there exits 3 and a
    master moving under the checks starts over; the project's master keeps its tip. A clone that
    has a remote is still refused, so nothing is ever published to a protected branch."""
    from ttp import cli
    land, p, repo, t, head = _land_setup(env, monkeypatch, ["true"])
    _git_out(repo, "branch", "-f", "master", "proj")
    p.set_config("delivery.push_branch", "master")
    tip = _git_out(repo, "rev-parse", "master")
    said = []
    assert land.land(p, repo, head, t, said.append) == 2, said
    assert "delivery.allow_protected_push_branch" in said[-1] and "master" in said[-1] \
        and "--root" in said[-1] and "nothing to land on" not in said[-1], said
    clone = env["tmp"] / "try"
    _git_out(env["tmp"], "clone", "-q", str(repo), str(clone))
    _git_out(clone, "remote", "remove", "origin")
    _git_out(clone, "branch", "master", tip)
    monkeypatch.chdir(env["tmp"])
    with pytest.raises(SystemExit) as e:
        cli.main(["land", head, "--root", str(clone), "--task", str(t)])
    assert e.value.code == 0, capsys.readouterr().err
    assert "not recording the landing" in capsys.readouterr().err
    assert _git_out(clone, "rev-parse", "master^") == tip and _git_out(repo, "rev-parse", "master") == tip
    assert not p.db.q("SELECT * FROM task_landings")

    def on_master(name, text):
        _git_out(clone, "checkout", "-q", "master")
        _commit(clone, name, text)
        _git_out(clone, "checkout", "-q", "work")
        return _git_out(clone, "rev-parse", "master")
    _git_out(clone, "branch", "-f", "master", tip)
    other = on_master("a.txt", "other\n")
    said = []
    assert land.land(p, clone, head, t, said.append, recorded=False) == 3, said
    assert "conflict replaying" in " ".join(said) and _git_out(clone, "rev-parse", "master") == other
    _git_out(clone, "branch", "-f", "master", tip)
    real, moves = land._checks, []

    def moving(p_, wt, new, base, say):
        if not moves:
            moves.append(on_master("m.txt", "m\n"))
        return real(p_, wt, new, base, say)
    monkeypatch.setattr(land, "_checks", moving)
    said = []
    assert land.land(p, clone, head, t, said.append, recorded=False) == 0, said
    assert "starting over" in " ".join(said) and _git_out(clone, "rev-parse", "master^") == moves[0]
    _git_out(clone, "remote", "add", "origin", str(repo))
    said = []
    assert land.land(p, clone, head, t, said.append, recorded=False) == 2, said
    assert _git_out(repo, "rev-parse", "master") == tip and not p.db.q("SELECT * FROM task_landings")


_PRECHECK_FAIL_OUT = ("tests/test_a.py::test_one FAILED\n"
                      "FAILED tests/test_a.py::test_one - assert 1 == 2\n1 failed, 3 passed in 0.50s\n")


def _precheck_review(s, monkeypatch, rc="1", fail=None):
    """`s.review` carries the label of checks the daemon started on `s.head` that ended with `rc`
    (None: still unended) and wrote `fail` over a pytest failure's check_failure.json, as `ttp checks`
    does; the review runs on a daemon whose start_run only records it."""
    from ttp import cli
    from ttp.daemon import PRECHECK_LABEL, PRECHECKS_DIR
    out = s.p.state / PRECHECKS_DIR / f"t{s.code}-{s.head[:12]}"
    out.mkdir(parents=True)
    if rc is not None:
        (out / cli.CHECKS_RC).write_text(f"{rc}\n")
    (out / cli.CHECKS_OUT).write_text("".join(f"line {n}\n" for n in range(30)) + _PRECHECK_FAIL_OUT)
    (out / "check_failure.json").write_text(json.dumps({
        "kind": "check_failed", "sig": "x", "check": "python3 -m pytest -q", "targets": [], "head": s.head,
        "rc": 1, "output": _PRECHECK_FAIL_OUT, "unrelated": False, "purpose": "candidate", **(fail or {})}))
    s.p.db.update_task(s.review, provider="claude", budget_usd=8.0, labels=[PRECHECK_LABEL + out.name])
    return _reuse_daemon(s.p, monkeypatch)


def test_a_review_whose_precheck_failed_goes_to_a_fix_with_the_failing_tests_and_no_reviewer_run(env, monkeypatch):
    """Reviews read the daemon's failed pre-review checks and only then queued a fix: the daemon fails
    the review model-free with the failing ids and the end of checks.out, and queues the fix and
    re-review a failed review with fixes gets. It counts as a round of the change, not for the area cap."""
    from ttp import reviewcap, sharedchecks
    s = _pq(env, monkeypatch)
    d, started = _precheck_review(s, monkeypatch)
    d.dispatch()
    assert started == [], "a reviewer ran on a head whose checks failed"
    rev = s.p.db.task(s.review)
    res = json.loads(rev["result"])
    assert rev["status"] == "failed" and res["review_failure"] == "precheck"
    assert res[sharedchecks.FAILING_KEY] == ["tests/test_a.py::test_one"] and res[sharedchecks.COMPLETE_KEY]
    assert "line 29\n" in res["summary"] and "line 13\n" in res["summary"] and "line 12\n" not in res["summary"]
    [fix] = s.p.db.q("SELECT * FROM tasks WHERE kind='code' AND labels LIKE ?", (f'%"review_fix:{s.review}"%',))
    assert fix["title"].startswith(f"Fix review #{s.review}: ") and fix["status"] == "queued"
    assert "tests/test_a.py::test_one" in fix["spec"] and "no reviewer ran" in fix["spec"]
    [again] = s.p.db.q("SELECT * FROM tasks WHERE kind='review' AND id!=? AND status='queued'", (s.review,))
    assert fix["id"] in json.loads(again["depends_on"])
    assert s.p.db.one("SELECT COUNT(*) n FROM runs")["n"] == 0
    assert reviewcap.unjudged(rev) and s.review not in reviewcap.failed_reviews(s.p.db, 0)
    assert reviewcap.area(s.p, {"review": {"area_fail_cap": 1}}, rev) is None
    # Its failure is recorded, so another change failing the same test can be seen as shared.
    assert s.p.db.one("SELECT ids FROM checks_failures WHERE task=?", (s.review,))["ids"] == \
        json.dumps(["tests/test_a.py::test_one"])


@pytest.mark.parametrize("case", ["passed", "unended", "env fault", "env problem", "target missing", "timeout",
                                  "killed", "moved", "shared"])
def test_a_review_whose_precheck_did_not_fail_on_its_tests_still_gets_its_reviewer(env, monkeypatch, case):
    """A pass, checks still running or past the hold, an environment fault (exit 75 or an env_problem,
    as a missing runner), a missing target, a timeout or kill, a head that moved, or a failure shared
    with other changes (its incident path holds the reviewer's own checks): the reviewer runs as before."""
    from ttp import sharedchecks
    s = _pq(env, monkeypatch)
    rc = {"passed": "0", "unended": None, "env fault": "75", "killed": "killed"}.get(case, "1")
    fail = {"env problem": {"env_problem": "No module named pytest"},
            "target missing": {"kind": "target_missing"}, "timeout": {"rc": 124},
            "moved": {"head": "f" * 40}}.get(case, {})
    if case == "shared":   # the same test failed the same way in two other changes
        ids = ["tests/test_a.py::test_one"]
        sigs = sharedchecks.failure_sigs(_PRECHECK_FAIL_OUT, ids)
        for n in (1, 2):
            other = s.p.db.add_task(f"other {n}", "s", kind="code", origin="user", status="done")
            sharedchecks.record(s.p.db, other, f"{n}" * 40, ids, time.time(), sigs)
    d, started = _precheck_review(s, monkeypatch, rc, fail)
    d.dispatch()
    assert started == [s.review], case
    assert not s.p.db.q("SELECT id FROM tasks WHERE labels LIKE ?", (f'%"review_fix:{s.review}"%',))


def test_own_push_remote_names_where_own_branches_go_by_remote_name_or_url(env, monkeypatch, capsys):
    """delivery.own_push_remote: `ttp push --own` publishes the task branch to the remote it names, or
    to the URL it names (here per repo, keyed by the main checkout's folder name), never to origin.
    With own_push existing_only the branch-exists check asks that target, not origin."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    tmp = env["tmp"]
    pub, by_url = tmp / "pub.git", tmp / "by-url.git"
    _bare_init(env, pub)
    _bare_init(env, by_url)
    _git_out(repo, "remote", "add", "pub", str(pub))
    heads = lambda r: _git_out(r, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    _git_out(repo, "checkout", "-q", "-b", "ttp/t40-feature")
    _commit(repo, "a.txt", "a\n")
    p.set_config("delivery.own_push_remote", "pub")
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert heads(pub) == ["ttp/t40-feature"] and heads(origin) == ["proj"]
    url = f"file://{by_url}"
    p.set_config("delivery.own_push_remote", {repo.name: url, "*": "pub"})
    for n in range(2):   # the second publish reads the URL's tip back (fetched into a ref of its own)
        _commit(repo, f"b{n}.txt", "b\n")
        assert _ttp("push", "--own") == 0, capsys.readouterr().err
        assert _git_out(by_url, "rev-parse", "ttp/t40-feature") == _git_out(repo, "rev-parse", "HEAD")
    assert "ttp/t40-feature" not in heads(origin)
    assert _ttp("push", "--own") == 0 and "nothing to push" in capsys.readouterr().err
    # existing_only: the branch on origin does not count; on the configured URL it does
    p.set_config("delivery.own_push", "existing_only")
    _git_out(repo, "checkout", "-q", "-b", "feature/x")
    _commit(repo, "c.txt", "c\n")
    _git_out(repo, "push", "-q", "origin", "HEAD~1:refs/heads/feature/x")
    assert _ttp("push", "--own") == 2
    assert "has no branch feature/x" in capsys.readouterr().err
    assert "feature/x" not in heads(by_url)
    _git_out(repo, "push", "-q", url, "HEAD~1:refs/heads/feature/x")
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert _git_out(by_url, "rev-parse", "feature/x") == _git_out(repo, "rev-parse", "HEAD")
    # a name the repo has no remote by, or a value of another kind, pushes nothing
    for bad in ("nosuch", ["pub"]):
        p.set_config("delivery.own_push_remote", bad)
        assert _ttp("push", "--own") == 2
        assert "delivery.own_push_remote" in capsys.readouterr().err


def test_own_push_refuses_a_local_working_copy_or_another_projects_folder_as_origin(env, monkeypatch, capsys):
    """A reported case: a code repo's origin was a local working copy belonging to another project, and
    `ttp push --own` pushed a task branch into it. With delivery.own_push_remote unset, an origin that
    is a non-bare local path (also as file://), or any path inside another registered project's
    folder, is refused naming the setting, and no ref is created there. Set to that same path, it
    goes. A local bare repo of no other project, and network URLs, are as before."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    tmp = env["tmp"]
    heads = lambda r: _git_out(r, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    _git_out(repo, "checkout", "-q", "-b", "ttp/t40-feature")
    _commit(repo, "a.txt", "a\n")
    assert _ttp("push", "--own") == 0, capsys.readouterr().err   # a local bare origin: unchanged
    assert "ttp/t40-feature" in heads(origin)
    _commit(repo, "b.txt", "b\n")
    for url in (str(other), f"file://{other}"):   # `other` is a non-bare clone of origin
        _git_out(repo, "remote", "set-url", "origin", url)
        assert _ttp("push", "--own") == 2
        err = capsys.readouterr().err
        assert "a working copy" in err and "Set delivery.own_push_remote" in err and "nothing pushed" in err, err
        assert heads(other) == ["proj"]
    theirs = tmp / "elsewhere" / "code"
    theirs.mkdir(parents=True)
    their_bare = theirs / "mirror.git"
    _bare_init(env, their_bare)
    monkeypatch.setattr(push, "load_registry",
                        lambda: {"projects": {"neighbour": {"dir": str(theirs)}, p.name: {"dir": str(p.root)}}})
    _git_out(repo, "remote", "set-url", "origin", str(their_bare))
    assert _ttp("push", "--own") == 2
    err = capsys.readouterr().err
    assert "inside project neighbour's folder" in err and "delivery.own_push_remote" in err, err
    assert heads(their_bare) == []
    p.set_config("delivery.own_push_remote", str(their_bare))   # chosen explicitly: allowed
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert heads(their_bare) == ["ttp/t40-feature"]
    p.set_config("delivery.own_push_remote", None)
    for url in ("https://example.invalid/acme/widgets.git", "ssh://git@example.invalid/acme/widgets.git",
                "git@example.invalid:acme/widgets.git"):
        _git_out(repo, "remote", "set-url", "origin", url)
        assert push.local_remote_problem(p, repo, "origin") == ""
        assert push.tracking_ref("origin", "b") == "refs/remotes/origin/b"
    assert push.tracking_ref("https://example.invalid/x.git", "b").startswith("refs/ttp/url/")


def _clean_repo(env, tmp_path, monkeypatch):
    """_checks_repo with `*.log` ignored and clean checkouts made under tmp_path/tmp."""
    import tempfile
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    (repo / ".gitignore").write_text("*.log\nbuild/\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.txt").write_text("a\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "ignore logs"], check=True)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    return p, repo, run, git, tmp


def _no_checkouts(repo, tmp):
    assert not list(tmp.glob("ttp-checks-*")), "a clean checkout was left behind"
    wts = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"], capture_output=True,
                         text=True).stdout
    assert wts.count("worktree ") == 1, wts


def test_ttp_checks_fails_a_check_that_needs_an_ignored_file_only_on_disk_and_names_it(env, tmp_path, monkeypatch,
                                                                                         capsys):
    """A test fixture the `*.log` ignore rule covers was never committed but sat in the authoring
    worktree: the check read it there and its pass was recorded for a tree without it. The checks run
    in a clean checkout of the commit, so it fails, and the failure names the file."""
    from ttp import cli
    p, repo, run, git, tmp = _clean_repo(env, tmp_path, monkeypatch)
    (repo / "tests" / "fixture.log").write_text("data\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", "cat tests/fixture.log"])
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "present only in the authoring worktree, not committed: tests/fixture.log" in err
    assert not json.loads((run / "checks.json").read_text())["passed"]
    _no_checkouts(repo, tmp)
    # A check whose output names nothing: the ignored and untracked files under the folder it runs.
    (repo / "tests" / "new_case.txt").write_text("x\n")
    with pytest.raises(SystemExit):
        cli.main(["checks", "--", "ls tests >/dev/null; exit 3"])
    err = capsys.readouterr().err
    assert "tests/fixture.log" in err and "tests/new_case.txt" in err and "test_a.txt" not in err
    # Committed, it passes.
    subprocess.run([*git, "add", "-f", "tests/fixture.log"], check=True)
    subprocess.run([*git, "commit", "-qm", "fixture"], check=True)
    (repo / "tests" / "new_case.txt").unlink()
    cli.main(["checks", "--", "cat tests/fixture.log"])
    assert json.loads((run / "checks.json").read_text())["passed"]
    _no_checkouts(repo, tmp)


def test_ttp_checks_never_passes_on_uncommitted_files_and_says_which_commit_it_checked(env, tmp_path, monkeypatch,
                                                                                       capsys):
    """An uncommitted edit `ttp checks` tolerates (a tracked file the ignore rules match) and an
    untracked file would each make the check pass in the authoring worktree; the pass would be recorded
    for HEAD's tree, which has neither. Both fail, and the run says what was not checked and which
    commit was."""
    from ttp import cli
    p, repo, run, git, tmp = _clean_repo(env, tmp_path, monkeypatch)
    (repo / "state.log").write_text("no\n")
    subprocess.run([*git, "add", "-f", "state.log"], check=True)
    subprocess.run([*git, "commit", "-qm", "tracked log"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "state.log").write_text("yes\n")
    with pytest.raises(SystemExit):
        cli.main(["checks", "--fresh", "--", "grep -q yes state.log"])
    out = capsys.readouterr().out
    assert f"uncommitted changes in this worktree are not checked: the checks run on a clean checkout of commit {head[:12]}" in out
    (repo / "state.log").write_text("no\n")
    (repo / "flag").write_text("x\n")
    with pytest.raises(SystemExit):
        cli.main(["checks", "--fresh", "--", "test -f flag"])
    assert f"1 untracked file(s) in this worktree are not checked: the checks run on a clean checkout of commit {head[:12]}" \
        in capsys.readouterr().out
    rec = json.loads((run / "checks.json").read_text())
    assert rec["head"] == head and not rec["passed"]
    # A pass is recorded for HEAD's tree, from what HEAD has.
    cli.main(["checks", "--fresh", "--", "grep -q no state.log && test ! -e flag"])
    assert json.loads((run / "checks.json").read_text())["passed"]
    _no_checkouts(repo, tmp)


@pytest.mark.parametrize("how", ["pass", "fail", "stop", "interrupt", "timeout"])
def test_ttp_checks_removes_its_clean_checkout_however_the_checks_end(env, tmp_path, monkeypatch, how):
    from ttp import cli
    from pathlib import Path
    p, repo, run, git, tmp = _clean_repo(env, tmp_path, monkeypatch)
    cmd = {"pass": "true", "fail": "false", "timeout": "timeout 1 sleep 30"}.get(how, "true")
    if how in ("stop", "interrupt"):   # the stop handler raises SystemExit mid-check; Ctrl-C KeyboardInterrupt
        def stopped(argv, group, **kw):
            assert Path(kw["cwd"]).is_dir() and Path(kw["cwd"]).name == "tree"
            raise SystemExit(143) if how == "stop" else KeyboardInterrupt
        monkeypatch.setattr(cli, "_run_check", stopped)
    try:
        cli.main(["checks", "--", cmd])
    except (SystemExit, KeyboardInterrupt):
        assert how != "pass"
    _no_checkouts(repo, tmp)


def test_clean_checkouts_a_dead_run_left_are_pruned_and_live_ones_kept(env, tmp_path, monkeypatch):
    import fcntl
    from ttp import cleantree, cli
    p, repo, run, git, tmp = _clean_repo(env, tmp_path, monkeypatch)
    dead, live = cleantree.Checkout(repo, "HEAD"), cleantree.Checkout(repo, "HEAD")
    dead._lock.close()   # its run was killed: nothing holds the lock
    dead._lock = None
    old = tmp / "ttp-checks-1-old"   # killed before it took its lock, long ago
    old.mkdir()
    os.utime(old, (1, 1))
    cli.main(["checks", "--", "true"])
    assert not dead.dir.exists() and not old.exists() and live.path.is_dir()
    assert str(dead.path) not in subprocess.run([*git, "worktree", "list"], capture_output=True, text=True).stdout
    live.close()
    live.close()   # twice is safe
    _no_checkouts(repo, tmp)


def test_ttp_checks_cwd_authoring_runs_in_the_worktree_and_labels_the_pass(env, tmp_path, monkeypatch, capsys):
    """A check that needs build outputs made in the authoring worktree opts out with "cwd": "authoring":
    it runs there, and the output and the check record say it did not run from a clean tree."""
    from ttp import cli, push
    p, repo, run, git, tmp = _clean_repo(env, tmp_path, monkeypatch)
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_text("x")
    check = "test -f build/out.bin"
    p.set_config("delivery.push_checks", [{"run": check, "cwd": "authoring"}, "test -f a.txt"])
    cli.main(["checks"])
    out = capsys.readouterr().out
    assert f"running 1/2: {check}{cli.AUTHORING_NOTE}" in out
    assert "1 ran in the authoring worktree, NOT a clean checkout" in out and repr(check) in out
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] and rec["authoring_tree"] == [check]
    _no_checkouts(repo, tmp)
    # Without the opt-out the same check runs in the clean checkout and fails.
    p.set_config("delivery.push_checks", [check])
    with pytest.raises(SystemExit):
        cli.main(["checks"])
    assert "build/out.bin" in capsys.readouterr().err
    # Its pass is not the clean form's (nor the reverse), and only the two cwd values are valid.
    assert cli._command_key(push.check_list([{"run": check, "cwd": "authoring"}])[0]) != cli._command_key(check)
    assert push.check_list([{"run": check, "cwd": "clean"}])[0].config() == check
    assert push.check_problems([{"run": check, "cwd": "/somewhere"}])


def test_ttp_checks_clean_checkout_populates_submodules_from_the_worktrees_copies(env, tmp_path, monkeypatch, capsys):
    """Submodules come from the authoring worktree's copies, never the network; one whose commit is not
    there fails the checks with a clear message instead of running in the authoring tree."""
    from ttp import cli
    p, repo, run, git, tmp = _clean_repo(env, tmp_path, monkeypatch)
    src = tmp_path / "subsrc"
    sgit = ["git", "-C", str(src), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    (src / "f.txt").write_text("f\n")
    subprocess.run([*sgit, "add", "."], check=True)
    subprocess.run([*sgit, "commit", "-qm", "f"], check=True)
    subprocess.run([*git, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), "sub"], check=True)
    subprocess.run([*git, "commit", "-qm", "sub"], check=True)
    subprocess.run(["git", "-C", str(src), "remote", "add", "x", "/nonexistent"], check=True)
    cli.main(["checks", "--", "test -f sub/f.txt && git -C sub rev-parse HEAD"])
    assert json.loads((run / "checks.json").read_text())["passed"]
    _no_checkouts(repo, tmp)
    # A gitlink to a commit only the submodule's origin has: no fall back, a clear failure.
    (src / "g.txt").write_text("g\n")
    subprocess.run([*sgit, "add", "."], check=True)
    subprocess.run([*sgit, "commit", "-qm", "g"], check=True)
    sha = subprocess.run([*sgit, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    subprocess.run([*git, "update-index", "--cacheinfo", f"160000,{sha},sub"], check=True)
    subprocess.run([*git, "commit", "-qm", "bump sub"], check=True)
    subprocess.run([*git, "config", "submodule.sub.ignore", "all"], check=True)   # its copy lags: not a change
    with pytest.raises(SystemExit):
        cli.main(["checks", "--", "true"])
    err = capsys.readouterr().err
    assert f"cannot populate submodule sub at {sha[:12]}" in err and "did not run" in err
    _no_checkouts(repo, tmp)


def _harness_worktree(p, tmp_path, monkeypatch):
    """A committed harness repo and a worktree of it, as a harness task checks its change in."""
    h = ["git", "-C", str(p.harness), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*h, "add", "-A"], check=True)
    subprocess.run([*h, "commit", "-qm", "base", "--allow-empty"], check=True)
    wt = tmp_path / "hwt"
    subprocess.run([*h, "worktree", "add", "-q", "--detach", str(wt)], check=True)
    monkeypatch.chdir(wt)
    return wt


def test_harness_repo_never_runs_the_code_repos_push_checks(env, tmp_path, monkeypatch, capsys):
    """In a project whose harness repo is not its code repo, `ttp checks` on a harness change runs no
    code repo check: with no list of its own it runs the built-in harness default (and
    push_checks' purpose harness entries under --purpose harness), says so, and records where its
    checks came from. The code repo still runs delivery.push_checks."""
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    log = tmp_path / "log"
    p.set_config("delivery.push_checks", [f"echo code >> {log}"])
    cli.main(["checks"])
    assert log.read_text().split() == ["code"]
    assert json.loads((run / "checks.json").read_text())["checks_from"] == "delivery.push_checks"
    log.unlink()
    wt = _harness_worktree(p, tmp_path, monkeypatch)
    assert push.harness_repo(p, wt) and push.harness_repo(p, p.harness) and not push.harness_repo(p, repo)
    capsys.readouterr()
    cli.main(["checks"])
    out = capsys.readouterr().out
    assert not log.exists(), "a harness change ran the code repo's checks"
    assert push.HARNESS_UNSET in out and "never a pass of the code checks" in out
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["checks_from"] == "the built-in harness default"
    assert f"echo code >> {log}" not in rec["commands"] and rec["commands"][0].startswith("git diff --check")
    cli.main(["checks", "--purpose", "harness"])
    assert not log.exists()
    assert "running the default harness plan" in capsys.readouterr().out
    # A purpose harness entry of push_checks is the harness repo's own: it still runs there.
    _fake_check_plan_ttp(tmp_path, monkeypatch, log)
    p.set_config("delivery.push_checks", [f"echo code >> {log}", {"run": HARNESS_PLAN, "purpose": "harness"}])
    cli.main(["checks", "--purpose", "harness", "--fresh"])
    assert log.read_text().split() == ["plan"]


def test_push_checks_by_repo_picks_each_repos_list(env, tmp_path, monkeypatch, capsys):
    """delivery.push_checks_by_repo gives the harness repo (`harness`) and a code repo (its folder
    name, absolute path, or path relative to the project root) their own lists; a repo it does not
    name keeps delivery.push_checks, and the settings fingerprints change only once it is set."""
    from ttp import checkgaps, cli, push, reviewreuse
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    log = tmp_path / "log"
    p.set_config("delivery.push_checks", [f"echo code >> {log}"])
    sig, key = checkgaps.config_sig(p.config()), reviewreuse.check_key(p.config())
    by = {"harness": [f"echo harness >> {log}"], "work": [f"echo work >> {log}"]}
    cli.main(["config", p.name, "delivery.push_checks_by_repo", json.dumps(by)])
    assert p.config()["delivery"]["push_checks_by_repo"] == by
    assert checkgaps.config_sig(p.config()) != sig and reviewreuse.check_key(p.config()) != key
    cli.main(["checks"])
    assert log.read_text().split() == ["work"]
    assert json.loads((run / "checks.json").read_text())["checks_from"] == "delivery.push_checks_by_repo['work']"
    log.unlink()
    _harness_worktree(p, tmp_path, monkeypatch)
    cli.main(["checks"])
    assert log.read_text().split() == ["harness"]
    assert json.loads((run / "checks.json").read_text())["checks_from"] == "delivery.push_checks_by_repo.harness"
    d = {"push_checks": ["true"]}
    for k in (str(repo), os.path.relpath(repo, p.root)):
        assert push.repo_checks({**d, "push_checks_by_repo": {k: ["false"]}}, repo, p) == (
            ["false"], f"delivery.push_checks_by_repo[{k!r}]")
    assert push.repo_checks({**d, "push_checks_by_repo": {"other": ["false"]}}, repo, p) == (
        ["true"], "delivery.push_checks")
    # The harness key never matches a code repo, even one whose folder is called harness.
    assert push.repo_checks({**d, "push_checks_by_repo": {"harness": ["false"]}}, repo, p)[0] == ["true"]
    assert [str(c) for c in push.all_checks({**d, "push_checks_by_repo": by})] == [
        "true", f"echo harness >> {log}", f"echo work >> {log}"]


def test_single_repo_push_checks_are_unchanged(env, tmp_path, monkeypatch):
    """With no push_checks_by_repo, a code repo gets delivery.push_checks as before and every
    fingerprint of the check settings is the one it was."""
    import hashlib
    from ttp import checkgaps, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    d = {"push_checks": ["pytest -q", {"run": "true", "if_exists": "a.txt"}]}
    assert push.repo_checks(d, repo, p) == (d["push_checks"], "delivery.push_checks")
    assert push.repo_checks(d, repo) == (d["push_checks"], "delivery.push_checks")
    assert push.checks_config(d) == d["push_checks"]
    assert checkgaps.config_sig({"delivery": d}) == hashlib.sha256(
        json.dumps(d["push_checks"], sort_keys=True).encode()).hexdigest()
    assert [str(c) for c in push.all_checks(d)] == ["pytest -q", "true"]


@pytest.mark.parametrize("value", ['["pytest -q"]', '"pytest -q"', '{"harness": 5}', '{"harness": {"run": "x"}}',
                                   '{"harness": ["run the tests and make sure they pass"]}', '{" ": ["true"]}'])
def test_push_checks_by_repo_rejects_malformed_values(env, tmp_path, monkeypatch, capsys, value):
    from ttp import cli, project, push
    p = make(env)
    with pytest.raises(SystemExit) as e:
        cli.main(["config", p.name, "delivery.push_checks_by_repo", value])
    assert e.value.code and "delivery.push_checks_by_repo" in capsys.readouterr().err
    assert "push_checks_by_repo" not in (p.raw_config().get("delivery") or {})
    assert push.by_repo_problems(json.loads(value))
    problems = project.config_problems({"delivery": {"push_checks_by_repo": json.loads(value)}})
    assert any(x.startswith("delivery.push_checks_by_repo: ") for x in problems)
    assert not project.config_problems({"delivery": {"push_checks_by_repo": {"harness": ["true"]}}})


def test_recorded_pr_branch_reads_the_label_then_the_latest_update_line(env):
    """worktree.recorded_pr_branch, the one reader of a task's pr_branch: its label (task_add's, from the
    original spec's line, or task_update's field), unless a `pr_branch:` line a task_update appended to
    its spec (after `## Update`) says otherwise; the latest such line wins, `pr_branch: ""` clears it,
    and a line in the original spec or in running prose is not an update. Reviews go by labels only."""
    from ttp import push, worktree
    p = make(env)

    def row(spec, labels=(), kind="code"):
        return p.db.task(p.db.add_task("T", spec, kind=kind, labels=list(labels)))

    upd = "\n\n## Update\n"
    assert worktree.recorded_pr_branch(row("s")) is None
    assert worktree.recorded_pr_branch(row("pr_branch: `feature/a`\nDo it.", ["pr_branch:feature/a"])) == "feature/a"
    assert worktree.recorded_pr_branch(row("s", ["no_pr_branch"])) == ""
    assert worktree.recorded_pr_branch(row(f"s{upd}pr_branch: `feature/b`\nPush there.")) == "feature/b"
    assert worktree.recorded_pr_branch(row(f"s{upd}pr_branch: `feature/b`", ["pr_branch:feature/a"])) == "feature/b"
    assert worktree.recorded_pr_branch(row(f"s{upd}pr_branch: `feature/b`", ["no_pr_branch"])) == "feature/b"
    assert worktree.recorded_pr_branch(
        row(f"s{upd}pr_branch: `feature/b`{upd}Also:\n- pr_branch: feature/c", ["pr_branch:feature/a"])) == "feature/c"
    cleared = row(f"s{upd}pr_branch: `feature/b`{upd}pr_branch: \"\"", ["pr_branch:feature/a"])
    assert worktree.recorded_pr_branch(cleared) == "" and worktree.carried_branch(cleared) == ""
    assert push.pr_branch_of(p, cleared) == ""
    assert worktree.recorded_pr_branch(row(f"s{upd}Read the spec's pr_branch: line first.")) is None
    assert worktree.recorded_pr_branch(row(f"s{upd}pr_branch: `feature/b`", kind="review")) is None
    assert worktree.carried_branch(row(f"s{upd}pr_branch: `feature/b`", ["pr_branch:feature/a"])) == "feature/b"


def test_own_push_honors_the_pr_branch_an_update_records(env, monkeypatch, capsys):
    """`ttp push --own` publishes onto the branch a task is recorded as delivering onto, however that was
    set: a `pr_branch:` line of its original spec, task_update's `pr_branch` field, or a `pr_branch:` line
    a task_update appended to its spec (a reported case: a branch named only in later spec updates was
    refused). The later update wins; `pr_branch` "" means no own branch. A branch nothing names, the
    push branch, main and what delivery.own_push forbids are still refused."""
    from ttp import coordinator as coord, push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    for b in ("feature/a", "feature/b", "feature/c"):
        _git_out(other, "push", "-q", "origin", f"HEAD:refs/heads/{b}")
    owner = p.db.add_task("Speed up the loader", "s", kind="code")
    theirs = f"ttp/t{owner}-speed-up-the-loader"
    p.db.update_task(owner, status="done", branch=theirs)
    _git_out(other, "push", "-q", "origin", f"HEAD:refs/heads/{theirs}")

    def add(title, spec):
        assert coord.apply(p, [{"type": "task_add", "title": title, "kind": "code", "spec": spec}]) == []
        return p.db.one("SELECT id FROM tasks WHERE title=?", (title,))["id"]

    def update(tid, **a):
        assert coord.apply(p, [{"type": "task_update", "id": tid, **a}]) == []

    def target(tid):
        own = f"ttp/t{tid}-own"
        p.db.update_task(tid, branch=own)
        _git_out(repo, "checkout", "-q", "-B", own)
        monkeypatch.setenv("TTP_TASK", str(tid))
        return push.own_target(p, repo)

    # The original spec's line, as before.
    orig = add("Fix the CI", "pr_branch: `feature/a`\nFix CI there.")
    assert target(orig) == ("origin", "feature/a")
    # task_update's field, on a task not yet running: recorded, and it wins over the original line.
    update(orig, pr_branch="feature/b")
    assert target(orig) == ("origin", "feature/b")
    # A `pr_branch:` line a task_update appended to the spec (it may run already): the latest wins.
    plain = add("Tidy the docs", "Tidy them.")
    assert target(plain) == ("origin", f"ttp/t{plain}-own")
    update(plain, spec="Deliver it onto the PR branch.\npr_branch: `feature/a`")
    assert target(plain) == ("origin", "feature/a")
    p.db.update_task(plain, status="running")
    update(plain, spec="pr_branch: `feature/c`")
    assert target(plain) == ("origin", "feature/c")
    # Another task's ttp/t<id>-... branch named by an update is the task's to publish, not inherited.
    update(orig, spec=f"pr_branch: `{theirs}`")
    assert target(orig) == ("origin", theirs)
    # The field after an update line wins; "" means no own branch: its own branch, under its own name.
    update(orig, pr_branch="feature/c")
    assert target(orig) == ("origin", "feature/c")
    update(orig, pr_branch="")
    assert target(orig) == ("origin", f"ttp/t{orig}-own") and push.pr_branch_of(p, p.db.task(orig)) == ""
    problems = coord.apply(p, [{"type": "task_update", "id": orig, "pr_branch": "feat..ure"}])
    assert problems and "not one valid branch name" in problems[0]
    # Still refused: a branch nothing names, the push branch, main, and what own_push forbids.
    _git_out(repo, "checkout", "-q", "-B", theirs)
    with pytest.raises(ValueError, match="only this task's own branch"):
        push.own_target(p, repo)
    for shared in ("proj", "main"):
        update(plain, spec=f"pr_branch: `{shared}`")
        with pytest.raises(ValueError):
            target(plain)
    update(plain, spec="pr_branch: `feature/gone`")
    with pytest.raises(ValueError, match="exists neither here nor on origin"):
        target(plain)
    update(plain, spec="pr_branch: `feature/a`")
    remote, branch = target(plain)
    p.set_config("delivery.own_push", "off")
    with pytest.raises(ValueError, match="own pushes are off"):
        push.own_gate(p, repo, remote, branch)
    p.set_config("delivery.own_push", "existing_only")
    push.own_gate(p, repo, remote, branch)
    with pytest.raises(ValueError, match="has no branch"):
        push.own_gate(p, repo, remote, "feature/new")


def test_a_pr_branch_line_an_update_appends_rewrites_the_branch_label(env):
    """A `pr_branch:` line a task_update appends to a task's spec sets its branch label as the
    `pr_branch` field does (`pr_branch: ""` sets no_pr_branch), so label readers (landed.carriers,
    worktree.fix_branches, the daemon's review queueing) agree with worktree.recorded_pr_branch. The
    field wins over a line of the same update; reviews keep their labels. task_add labels its line."""
    from ttp import coordinator as coord, landed, push, worktree
    p = make(env)
    owner = p.db.add_task("Speed up the loader", "s", kind="code")
    theirs = f"ttp/t{owner}-speed-up-the-loader"
    p.db.update_task(owner, status="done", branch=theirs)

    def labels(tid):
        return push._labels(p.db.task(tid))

    def update(tid, **a):
        assert coord.apply(p, [{"type": "task_update", "id": tid, **a}]) == []
        t = p.db.task(tid)
        branch = [lb[10:] for lb in labels(tid) if lb.startswith("pr_branch:")]
        assert (branch or [""] if worktree.NO_PR_BRANCH in labels(tid) else branch or [None]) \
            == [worktree.recorded_pr_branch(t)], labels(tid)
        return t

    tid = p.db.add_task("Fix the loader PR", "Fix its CI.", kind="code")
    p.db.update_task(tid, branch=f"ttp/t{tid}-fix-the-loader-pr", status="running")
    assert tid not in landed.carriers(p.db, owner) and worktree.fix_branches(p, {"id": 0}, [theirs]) == {}
    t = update(tid, spec=f"Deliver it onto its PR.\npr_branch: `{theirs}`")
    assert labels(tid) == [f"pr_branch:{theirs}"]
    assert landed.carriers(p.db, owner)[tid][1] == owner
    assert worktree.fix_branches(p, {"id": 0}, [theirs]) == {f"ttp/t{tid}-fix-the-loader-pr": theirs}
    assert any(lb.startswith("pr_branch:") for lb in push._labels(t))   # the daemon's review-queue check
    # A later line replaces it; a cleared line carries no branch.
    update(tid, spec="pr_branch: `feature/b`\nThen `pr_branch: feature/c` is prose, not a line.")
    assert labels(tid) == ["pr_branch:feature/b"]
    update(tid, spec='pr_branch: ""')
    assert labels(tid) == [worktree.NO_PR_BRANCH] and tid not in landed.carriers(p.db, owner)
    # An update with no line leaves the label alone.
    update(tid, spec="Also fix the lint.")
    assert labels(tid) == [worktree.NO_PR_BRANCH]
    # Field and line in one update (in spec or text): the field wins, in both.
    p.db.update_task(tid, status="queued")
    update(tid, spec="pr_branch: `feature/b`", pr_branch="feature/c")
    assert labels(tid) == ["pr_branch:feature/c"]
    update(tid, text="pr_branch: `feature/b`", pr_branch="")
    assert labels(tid) == [worktree.NO_PR_BRANCH]
    # Reviews go by their labels only.
    rev = p.db.add_task("Review the loader PR", "s", kind="review", labels=[f"pr_branch:{theirs}"])
    update(rev, spec="pr_branch: `feature/b`")
    assert labels(rev) == [f"pr_branch:{theirs}"]
    # task_add's own line labels it already.
    assert coord.apply(p, [{"type": "task_add", "title": "Tidy the PR", "kind": "code",
                            "spec": "pr_branch: `feature/b`\nTidy it."}]) == []
    assert "pr_branch:feature/b" in labels(p.db.one("SELECT id FROM tasks WHERE title='Tidy the PR'")["id"])


def test_own_push_publishes_another_tasks_branch_checked_out_when_only_an_update_names_it(env, monkeypatch):
    """A reported case: a task's pr_branch was set to another task's ttp/t<id>-... branch only by an
    update (task_update's `pr_branch`, or a `pr_branch:` line appended to its spec), never by its
    original spec, and its worktree had that branch checked out. `ttp push --own` refused it as not
    the task's own branch; it now publishes it under its name. Without the update it is still refused."""
    from ttp import coordinator as coord, push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    owner = p.db.add_task("Speed up the loader", "s", kind="code")
    theirs = f"ttp/t{owner}-speed-up-the-loader"
    p.db.update_task(owner, status="done", branch=theirs)
    _git_out(other, "push", "-q", "origin", f"HEAD:refs/heads/{theirs}")
    _git_out(repo, "checkout", "-q", "-B", theirs)
    for how in ({"pr_branch": theirs}, {"spec": f"Deliver onto its PR.\npr_branch: `{theirs}`"}):
        tid = p.db.add_task("Fix the loader PR", "Fix its CI.", kind="code")
        monkeypatch.setenv("TTP_TASK", str(tid))
        with pytest.raises(ValueError, match="only this task's own branch"):
            push.own_target(p, repo)
        assert coord.apply(p, [{"type": "task_update", "id": tid, **how}]) == []
        p.db.update_task(tid, status="running", branch=theirs)
        assert push.own_target(p, repo) == ("origin", theirs), how


def _rebase_setup(env, monkeypatch, checks=("true",), spec_line="pr_branch: {old}", record=True, lease=True):
    """An earlier task's branch `old` on origin (the tip `theirs`, not on the push branch) and a
    running task labelled rebases:<old> whose spec names it, on its own branch with a rebased head
    that does not fast-forward `old`; `theirs` recorded as where its rebase started, as
    worktree.ensure records it (`record` False: nothing recorded, as for a task dispatched before).
    `lease`: delivery.rebase_replace lease (False: unset, which reads as new_branch)."""
    p, repo, origin, other = _push_setup(env, monkeypatch, list(checks))
    if lease:
        p.set_config("delivery.rebase_replace", "lease")
    owner = p.db.add_task("First example", "s", kind="code")
    old = f"ttp/t{owner}-example"
    _commit(other, "old.txt", "old\n")
    _git_out(other, "push", "-q", "origin", f"HEAD:refs/heads/{old}")
    theirs = _git_out(origin, "rev-parse", old)
    _git_out(repo, "fetch", "-q", "origin")
    tid = p.db.add_task("Rebase the example", spec_line.format(old=old) + "\nRebase it onto proj.",
                        kind="code", labels=[f"rebases:{old}"])
    own = f"ttp/t{tid}-rebase-the-example"
    p.db.update_task(tid, status="running", branch=own)
    monkeypatch.setenv("TTP_TASK", str(tid))
    _git_out(repo, "checkout", "-q", "-B", own)
    _commit(repo, "old.txt", "old, rebased\n")
    if record:
        from ttp import worktree
        worktree.record_rebase_start(repo, tid, theirs)
    return p, repo, origin, other, tid, old, theirs


def test_a_rebase_delivers_on_its_own_new_branch_by_default_and_never_leases(env, monkeypatch, capsys):
    """(a) delivery.rebase_replace unset (new_branch): a rebases:<old> task publishes its own branch,
    never with a lease, and a non-fast-forward --own onto an existing remote branch (the rebased one
    checked out, or its own branch rewritten) is refused, naming the setting."""
    from ttp import push, worktree
    leases = []
    real = push.publish
    monkeypatch.setattr(push, "publish", lambda *a, **k: leases.append(k.get("lease")) or real(*a, **k))
    p, repo, origin, other, tid, old, theirs = _rebase_setup(env, monkeypatch, lease=False)
    t = p.db.task(tid)
    own = t["branch"]
    assert worktree.delivery_branch(t) == (old, True) and push.delivery_source(p, t)[1:] == (own, False)
    assert push.own_target(p, repo) == ("origin", own) and not push.own_lease(p, own)
    head = _git_out(repo, "rev-parse", "HEAD")
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert _git_out(origin, "rev-parse", own) == head and _git_out(origin, "rev-parse", old) == theirs
    assert "under a lease" not in capsys.readouterr().err and leases and not any(leases)
    # Its own branch rewritten: git refuses the non-fast-forward, and the message names the setting.
    _git_out(repo, "commit", "-q", "--amend", "-m", "rewritten")
    assert _ttp("push", "--own") == push_rejected()
    assert "delivery.rebase_replace" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", own) == head
    # The rebased branch itself checked out: refused before any push, naming the setting.
    _git_out(repo, "checkout", "-q", "-B", old)
    with pytest.raises(ValueError, match="delivery.rebase_replace"):
        push.own_target(p, repo)
    assert _ttp("push", "--own") != 0
    assert "delivery.rebase_replace" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", old) == theirs and not any(leases)


def test_rebase_replace_lease_replaces_the_rebased_branch_as_before(env, monkeypatch, capsys):
    """(b) delivery.rebase_replace lease keeps the lease replace: --own publishes the rebase back onto
    the old branch with --force-with-lease. It passes config validation, and the coordinator sets it
    only on the user's word."""
    from ttp import coordinator as coord, project, push
    p, repo, origin, other, tid, old, theirs = _rebase_setup(env, monkeypatch, lease=True)
    assert project.config_problems(p.raw_config(), p.root) == [] or not any(
        "rebase_replace" in x for x in project.config_problems(p.raw_config(), p.root))
    t = p.db.task(tid)
    assert push.delivery_source(p, t)[1:] == (old, True) and push.own_lease(p, old)
    head = _git_out(repo, "rev-parse", "HEAD")
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert _git_out(origin, "rev-parse", old) == head and "under a lease" in capsys.readouterr().err
    p.set_config("delivery.rebase_replace", "new_branch")
    out = coord.apply(p, [{"type": "config_set", "key": "delivery.rebase_replace", "value": "lease"}])
    assert any("needs the user's approval" in x for x in out), out
    assert p.config()["delivery"]["rebase_replace"] == "new_branch"
    assert coord.apply(p, [{"type": "config_set", "key": "delivery.rebase_replace", "value": "lease"}],
                       user_turn=True) == []
    assert p.config()["delivery"]["rebase_replace"] == "lease"


def test_never_force_refuses_every_forced_git_push_and_a_lease(env, monkeypatch, capsys):
    """(c) delivery.never_force true: the run's git wrapper refuses a raw `git push` that forces in any
    form, the PreToolUse hook denies one by git's path, and --own refuses a lease push even under
    delivery.rebase_replace lease. A fast-forward push still goes."""
    from ttp import gitguard, hook, push
    p, repo, origin, other, tid, old, theirs = _rebase_setup(env, monkeypatch, lease=True)
    own = p.db.task(tid)["branch"]
    _git_out(repo, "push", "-q", "origin", f"{own}:refs/heads/{own}")
    _git_out(repo, "commit", "-q", "--amend", "-m", "rewritten")
    wrapper = RUNTIME.parent / "template" / "bin" / "git"
    run_env = {**os.environ, "TTP_PYTHON": sys.executable}

    def git_push(*args):
        return subprocess.run([str(wrapper), "push", *args], cwd=repo, env=run_env, capture_output=True,
                              text=True, timeout=60)

    forced = [["-f", "origin", own], ["--force", "origin", own], ["--force-with-lease", "origin", own],
              ["--force-with-lease=" + own, "origin", own], ["--force-if-includes", "origin", own],
              ["-uf", "origin", own], ["origin", f"+{own}:{own}"], ["-o", "x", "origin", "--", f"+HEAD:{own}"]]
    # Off (the default): a forced push to its own branch is git's business.
    assert gitguard.never_force_refusal(["push", "-f", "origin", own], dict(os.environ)) == ""
    p.set_config("delivery.never_force", True)
    for args in forced:
        out = git_push(*args)
        assert out.returncode != 0 and "delivery.never_force" in out.stderr, (args, out.stderr)
        assert gitguard.forced(args), args
    # Moving an existing tag needs force, so never_force refuses it too (documented, not widened).
    for args in (["origin", "+refs/tags/v1:refs/tags/v1"], ["--force-with-lease", "origin", "refs/tags/x"]):
        assert gitguard.forced(args), args
    for args in (["origin", own], ["-o", "+x", "origin", own], ["--no-force-with-lease", "origin", own],
                 ["--push-option", "-f", "origin", own]):
        assert gitguard.forced(args) == "", args
    assert _git_out(origin, "rev-parse", own) != _git_out(repo, "rev-parse", "HEAD")
    run_dir = env["tmp"] / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    deny = lambda c: (hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": c}})[0] or {}).get(
        "hookSpecificOutput", {}).get("permissionDecision")
    assert deny(f"/usr/bin/git push -f origin {own}") == "deny"
    assert "delivery.never_force" in hook.git_push_bypass([["/usr/bin/git", "push", "-f", "origin", own]])
    # A lease push is refused too, even with rebase_replace lease (validation flags that pair).
    _git_out(repo, "reset", "-q", "--hard", f"origin/{own}")
    assert _ttp("push", "--own") == push.REFUSED
    assert "delivery.never_force" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", old) == theirs
    # A fast-forward still goes.
    _commit(repo, "ff.txt", "ff\n")
    assert git_push("origin", f"HEAD:{own}").returncode == 0


def test_rebase_replace_lease_with_never_force_is_rejected_at_config_validation(env):
    """(d) delivery.rebase_replace lease together with delivery.never_force true is a config problem
    that says why, and config_set refuses to make that pair (either key last)."""
    from ttp import coordinator as coord, project
    p = make(env)
    raw = {**p.raw_config(), "delivery": {"rebase_replace": "lease", "never_force": True}}
    problems = project.config_problems(raw, p.root)
    assert any("delivery.rebase_replace: lease" in x and "never_force" in x and "force push" in x
               for x in problems), problems
    assert not any("rebase_replace" in x for x in project.config_problems(
        {**raw, "delivery": {"rebase_replace": "new_branch", "never_force": True}}, p.root))
    assert any("not one of" in x for x in project.config_problems(
        {**raw, "delivery": {"rebase_replace": "force"}}, p.root))
    assert coord.apply(p, [{"type": "config_set", "key": "delivery.never_force", "value": True}]) == []
    out = coord.apply(p, [{"type": "config_set", "key": "delivery.rebase_replace", "value": "lease"}], user_turn=True)
    assert any("never_force" in x for x in out), out
    p.set_config("delivery.never_force", False)
    assert coord.apply(p, [{"type": "config_set", "key": "delivery.rebase_replace", "value": "lease"}], user_turn=True) == []
    out = coord.apply(p, [{"type": "config_set", "key": "delivery.never_force", "value": True}])
    assert any("never_force" in x for x in out) and p.config()["delivery"]["never_force"] is False, out
    # The coordinator turns the safe sides on itself; never_force off needs the user.
    assert coord.apply(p, [{"type": "config_set", "key": "delivery.rebase_replace", "value": "new_branch"}]) == []
    out = coord.apply(p, [{"type": "config_set", "key": "delivery.never_force", "value": False}])
    assert out == [] or any("needs the user's approval" in x for x in out)


def test_an_upgraded_project_without_the_setting_rebases_onto_a_new_branch(env, monkeypatch):
    """(f) A project.json from before delivery.rebase_replace (no key at all) reads as new_branch: no
    migration, no config problem, and a rebase task delivers on its own branch, never under a lease."""
    from ttp import project, push
    p, repo, origin, other, tid, old, theirs = _rebase_setup(env, monkeypatch, lease=False)
    assert "rebase_replace" not in (p.raw_config().get("delivery") or {})
    assert project.rebase_replace(p.config()) == "new_branch" and not project.never_force(p.config())
    assert not any("rebase_replace" in x or "never_force" in x for x in project.config_problems(p.raw_config(), p.root))
    t = p.db.task(tid)
    assert push.delivery_source(p, t) == (t, t["branch"], False) and not push.own_lease(p, old)


@pytest.mark.parametrize("spec_line", ["pr_branch: {old}", "`pr_branch: {old}`", "pr_branch: `{old}`"])
def test_own_push_publishes_a_rebase_onto_the_branch_it_rebases_under_a_lease(env, monkeypatch, capsys, spec_line):
    """A task labelled rebases:<branch> whose spec names that branch in a `pr_branch:` line (plain, in
    backticks, or the whole line in backticks) publishes with --own onto that branch, as the dispatcher
    built it on it: from its own branch or with that branch itself checked out. The non-fast-forward
    update goes only with a lease on the tip it read."""
    from ttp import push, worktree
    p, repo, origin, other, tid, old, theirs = _rebase_setup(env, monkeypatch, spec_line=spec_line)
    t = p.db.task(tid)
    assert worktree.delivery_branch(t) == (old, True) and push.delivery_source(p, t)[1:] == (old, True)
    assert worktree.carried_head(p, t) == theirs   # what the dispatcher builds it on
    assert push.own_target(p, repo) == ("origin", old) and push.own_lease(p, old)
    head = _git_out(repo, "rev-parse", "HEAD")
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert _git_out(origin, "rev-parse", old) == head
    assert "under a lease" in capsys.readouterr().err
    # A fast-forward of what it published goes too.
    _commit(repo, "more.txt", "more\n")
    assert _ttp("push", "--own") == 0 and _git_out(origin, "rev-parse", old) == _git_out(repo, "rev-parse", "HEAD")
    # The reported case: the rebased branch itself checked out, replacing the tip the rebase started
    # from (a non-fast-forward).
    _git_out(other, "push", "-q", "-f", "origin", f"{theirs}:refs/heads/{old}")
    _git_out(repo, "checkout", "-q", "-B", old)
    head = _git_out(repo, "rev-parse", "HEAD")
    assert subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", theirs, head]).returncode != 0
    assert push.own_target(p, repo) == ("origin", old)
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert _git_out(origin, "rev-parse", old) == head
    assert "under a lease" in capsys.readouterr().err
    # A pr_branch task (no rebase) still only fast-forwards.
    p.db.update_task(tid, labels=json.dumps([f"pr_branch:{old}"]))
    assert not push.own_lease(p, old)


def test_own_push_lease_keeps_a_branch_that_moved_during_the_checks(env, monkeypatch, tmp_path):
    """The lease is on the tip read before the checks: someone pushing to the rebased branch while
    they run makes the push fail, and their commit stays. Never a plain force push."""
    mover = tmp_path / "move.sh"
    p, repo, origin, other, tid, old, theirs = _rebase_setup(env, monkeypatch, checks=(f"sh {mover}",))
    mover.write_text(f"git -C {other} commit -q --allow-empty -m moved && "
                     f"git -C {other} push -q origin HEAD:refs/heads/{old}\n")
    assert _ttp("push", "--own") == push_rejected()
    moved = _git_out(origin, "rev-parse", old)
    assert moved != theirs and moved == _git_out(other, "rev-parse", "HEAD")


def test_own_push_rebase_keeps_a_commit_pushed_after_the_rebase_started(env, monkeypatch, capsys):
    """A commit pushed to the rebased branch after the rebase was built on it, and before --own (so
    the tip read at push time already has it), is never overwritten: --own refuses, naming it. Once
    HEAD holds it (rebased again onto the new tip), the rebase goes, and the commit stays."""
    from ttp import push
    p, repo, origin, other, tid, old, theirs = _rebase_setup(env, monkeypatch)
    _commit(other, "late.txt", "late\n")
    late = _git_out(other, "rev-parse", "HEAD")
    _git_out(other, "push", "-q", "origin", f"HEAD:refs/heads/{old}")
    assert _ttp("push", "--own") == push.REJECTED
    err = capsys.readouterr().err
    assert late[:10] in err and "would lose" in err, err
    assert _git_out(origin, "rev-parse", old) == late
    # Rebased again onto the new tip: HEAD holds the late commit's change, so nothing is lost.
    _git_out(repo, "cherry-pick", late)
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    tip = _git_out(origin, "rev-parse", old)
    assert tip == _git_out(repo, "rev-parse", "HEAD") and (repo / "late.txt").read_text() == "late\n"


def test_own_push_rebase_with_no_recorded_start_keeps_every_commit_head_lacks(env, monkeypatch, capsys):
    """With no recorded start (a task dispatched before it was recorded), any commit of the remote
    branch HEAD lacks by patch refuses the rebase; recording the tip as the start lets it go."""
    from ttp import push, worktree
    p, repo, origin, other, tid, old, theirs = _rebase_setup(env, monkeypatch, record=False)
    assert worktree.rebase_start(repo, tid) == ""
    assert _ttp("push", "--own") == push.REJECTED
    err = capsys.readouterr().err
    assert theirs[:10] in err and "no rebase start is recorded" in err
    assert f"git update-ref {worktree.REBASE_START_REF}{tid} {theirs}" in err
    assert _git_out(origin, "rev-parse", old) == theirs
    worktree.record_rebase_start(repo, tid, theirs)
    assert _ttp("push", "--own") == 0 and _git_out(origin, "rev-parse", old) == _git_out(repo, "rev-parse", "HEAD")


def push_rejected():
    from ttp import push
    return push.REJECTED


def test_a_continuation_of_a_rebase_task_publishes_onto_the_same_branch(env, monkeypatch, capsys):
    """A task continuing a rebase task (no branch record of its own) resolves the same branch, with
    the lease, in the dispatcher's routing and in --own alike."""
    from ttp import push
    p, repo, origin, other, tid, old, theirs = _rebase_setup(env, monkeypatch)
    p.db.update_task(tid, status="failed")
    nxt = p.db.add_task("Rebase the example, again", "Finish the rebase.", kind="code",
                        labels=[f"continues:{tid}"])
    own = f"ttp/t{nxt}-again"
    p.db.update_task(nxt, status="running", branch=own)
    monkeypatch.setenv("TTP_TASK", str(nxt))
    _git_out(repo, "checkout", "-q", "-B", own)
    assert push.delivery_source(p, p.db.task(nxt))[1:] == (old, True)
    assert push.own_target(p, repo) == ("origin", old) and push.own_lease(p, old)
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert _git_out(origin, "rev-parse", old) == _git_out(repo, "rev-parse", "HEAD")
    # A cleared record (pr_branch "") stops the chain: it lands on the push branch, --own has no target.
    p.db.update_task(nxt, labels=json.dumps([f"continues:{tid}", "no_pr_branch"]))
    assert push.delivery_source(p, p.db.task(nxt)) == (None, "", False) and not push.own_lease(p, old)
    _git_out(repo, "checkout", "-q", "-B", old)
    with pytest.raises(ValueError, match="publishes only this task's own branch"):
        push.own_target(p, repo)


def test_own_push_refuses_a_rebase_branch_no_record_names_or_a_shared_one(env, monkeypatch, capsys):
    """A task with no rebases:/pr_branch record of a branch may not publish onto it; a record naming
    the push branch, main or master is refused, never forced."""
    from ttp import push
    p, repo, origin, other, tid, old, theirs = _rebase_setup(env, monkeypatch)
    stray = p.db.add_task("Unrelated", "s", kind="code")
    p.db.update_task(stray, status="running", branch=f"ttp/t{stray}-unrelated")
    monkeypatch.setenv("TTP_TASK", str(stray))
    _git_out(repo, "checkout", "-q", "-B", old)
    with pytest.raises(ValueError, match="publishes only this task's own branch"):
        push.own_target(p, repo)
    assert not push.own_lease(p, old)
    assert _ttp("push", "--own") != 0 and _git_out(origin, "rev-parse", old) == theirs
    monkeypatch.setenv("TTP_TASK", str(tid))
    _git_out(repo, "checkout", "-q", f"ttp/t{tid}-rebase-the-example")
    for shared in ("proj", "main", "master"):
        p.db.update_task(tid, spec=f"pr_branch: {shared}\nRebase it.", labels=json.dumps([f"rebases:{shared}"]))
        with pytest.raises(ValueError, match="shared branch"):
            push.own_target(p, repo)
    assert _git_out(origin, "rev-parse", "proj") != _git_out(repo, "rev-parse", "HEAD")


def test_own_push_rebase_obeys_own_push_off_and_existing_only(env, monkeypatch, capsys):
    """own_push off refuses a rebase's --own; existing_only (and on) never creates the branch a rebase
    names when the remote lacks it."""
    from ttp import push
    p, repo, origin, other, tid, old, theirs = _rebase_setup(env, monkeypatch)
    p.set_config("delivery.own_push", "off")
    assert _ttp("push", "--own") != 0 and "own pushes are off" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", old) == theirs
    _git_out(origin, "branch", "-D", old)
    for mode in ("existing_only", "on"):
        p.set_config("delivery.own_push", mode)
        assert _ttp("push", "--own") != 0, mode
        assert "never creates" in capsys.readouterr().err or mode == "existing_only"
        assert old not in _git_out(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()


def test_backticked_and_plain_pr_branch_lines_label_a_task_the_same(env):
    """task_add reads `pr_branch: <b>`, `pr_branch: \\`<b>\\`` and the whole line in backticks alike:
    a rebase title labels rebases:<b>, another title pr_branch:<b>."""
    from ttp import coordinator as coord, worktree
    p = make(env)
    for i, line in enumerate(("pr_branch: ttp/t1-example", "pr_branch: `ttp/t1-example`",
                              "`pr_branch: ttp/t1-example`", "- `pr_branch: ttp/t1-example`.")):
        assert worktree.spec_line_branch(line) == "ttp/t1-example", line
        for title, label in ((f"Rebase the example {i}", "rebases"), (f"Fix the example {i}", "pr_branch")):
            assert coord.apply(p, [{"type": "task_add", "title": title, "kind": "code", "force": True,
                                    "spec": f"{line}\nDo it."}]) == []
            labels = json.loads(p.db.one("SELECT labels FROM tasks WHERE title=?", (title,))["labels"])
            assert f"{label}:ttp/t1-example" in labels, (line, labels)
    assert worktree.spec_line_branch('`pr_branch: ""`') == ""


def test_prune_rebase_starts_removes_a_finished_tasks_ref_and_keeps_a_continued_one(env, monkeypatch):
    """The sweep deletes a finished rebase task's recorded start; an open task that continues it keeps
    the ref; pruning again, or with the ref already missing, raises nothing."""
    from ttp import worktree
    p, repo, origin, other, tid, old, theirs = _rebase_setup(env, monkeypatch)
    p.db.update_task(tid, status="done")
    cont = p.db.add_task("Continue the rebase", "s", kind="code", labels=[f"continues:{tid}"])
    assert worktree.prune_rebase_starts(p, p.db.q("SELECT * FROM tasks WHERE id=?", (cont,))) == []
    assert worktree.rebase_start(p.root, tid) == theirs
    p.db.update_task(cont, status="done")
    worktree.sweep(p)
    assert worktree.rebase_start(p.root, tid) == ""
    assert worktree.prune_rebase_starts(p, []) == []
    worktree.sweep(p)


def _split_with_a_landing_hop(env, p, title="abandon"):
    """Today's case through the real writers: a code task hands off done with a `continue:` follow-up
    (the daemon queues its hop and marks it a split), the hop finds nothing to do before the change
    lands and waits on landed:#<it>, and the coordinator then queues the review of its branch by
    hand. Returns (code task id, branch, review id, hop id)."""
    root, branch, _, reviews = _finish_code(env, p, title, {"a.py": 10}, result=_cont(title))
    assert not reviews, "a split got a review"
    (hop,) = p.db.q("SELECT * FROM tasks WHERE parent=?", (root,))
    from ttp import worktree
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    _, hbranch = worktree.ensure(p, p.db.task(hop["id"]))
    p.db.update_task(hop["id"], status="running", branch=hbranch)
    run_dir = env["tmp"] / f"run-{hop['id']}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(
        {"status": "waiting", "summary": f"nothing to do before #{root} lands", "retry_when": f"landed:#{root}",
         "waiting_for": f"#{root} landing", "retry_after_s": 1800}))
    Daemon(p.base)._finish_worker({"task": hop["id"], "note": "{}"}, Usage(cost_usd=1.0), "ok", run_dir)
    assert p.db.task(hop["id"])["status"] == "queued"
    rev = p.db.add_task(f"Review #{root}: {title}", f"Independently review code task #{root}, branch {branch}.",
                        kind="review", tier="standard", origin="coordinator")
    return root, branch, rev, hop["id"]


def test_a_failed_review_never_hands_its_findings_to_a_hop_that_waits_on_the_reviewed_change_landing(env):
    """Review #3624's case: the open hop continuing the reviewed task waited on that task landing,
    so it could never fix the branch, and its re-review would have run on the same head. The fix
    goes to a new Fix review task, and the re-review waits on that."""
    p = make(env)
    root, branch, rev, hop = _split_with_a_landing_hop(env, p)
    spec = p.db.task(hop)["spec"]
    _fail_review(env, p, rev, [{"title": "refuse an open review", "spec": "abandon refuses a task under review"}])
    fixes = p.db.q("SELECT * FROM tasks WHERE title LIKE ?", (f"Fix review #{rev}:%",))
    assert len(fixes) == 1, "no fix task: the hop waiting on the landing took the findings"
    fix = fixes[0]
    assert f"review_fix:{rev}" in _labels(p, fix["id"]) and f"continues:{root}" in _labels(p, fix["id"])
    assert p.db.task(hop)["spec"] == spec and f"review_fix:{rev}" not in _labels(p, hop)
    (rr,) = p.db.q("SELECT * FROM tasks WHERE kind='review' AND id!=?", (rev,))
    assert json.loads(rr["depends_on"]) == [fix["id"]]
    assert p.db.unmet_dependencies([rr])[rr["id"]] == [fix["id"]], "the re-review can run before its fix"


def test_a_failed_review_still_gives_a_working_split_hop_its_findings(env):
    """No regression: a hop that builds on the branch (no landed wait on its chain) still takes the
    findings, and the re-review waits on it."""
    p = make(env)
    root, branch, _, _ = _finish_code(env, p, "parser", {"a.py": 10}, result=_cont("parser"))
    (hop,) = p.db.q("SELECT * FROM tasks WHERE parent=?", (root,))
    rev = p.db.add_task(f"Review #{root}: parser", f"Independently review code task #{root}, branch {branch}.",
                        kind="review", tier="standard", origin="coordinator")
    _fail_review(env, p, rev, [{"title": "fix the parser", "spec": "the parser drops the last token"}])
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Fix review%'")
    assert f"review_fix:{rev}" in _labels(p, hop["id"])
    (rr,) = p.db.q("SELECT * FROM tasks WHERE kind='review' AND id!=?", (rev,))
    assert json.loads(rr["depends_on"]) == [hop["id"]]


def _fix_and_re_review(p, rev):
    (fix,) = p.db.q("SELECT * FROM tasks WHERE title LIKE ?", (f"Fix review #{rev}:%",))
    (rr,) = p.db.q("SELECT * FROM tasks WHERE kind='review' AND labels LIKE ?", (f'%"continues:{rev}"%',))
    return fix["id"], rr["id"]


@pytest.mark.parametrize("handoff", ["done", "needs_review"])
def test_a_re_review_never_runs_when_its_fix_ends_with_no_new_commits(env, handoff):
    """The re-review depends on its fix; a fix that hands off done (or needs_review) without adding a
    commit to the head its review saw leaves nothing to re-review: the re-review is blocked, not run on
    the same head, and the fix's hand-off wakes the coordinator saying so."""
    p = make(env)
    root, branch, rev, _ = _split_with_a_landing_hop(env, p)
    _fail_review(env, p, rev, [{"title": "refuse an open review", "spec": "abandon refuses a task under review"}])
    fid, rid = _fix_and_re_review(p, rev)
    head = _git_out(p.root, "rev-parse", "--short=12", branch).strip()
    assert f"fix_base:{head}" in _labels(p, rid)
    _hop(env, p, fid, {}, {"status": handoff, "summary": "nothing needed fixing"})
    rr = p.db.task(rid)
    assert rr["status"] == "blocked" and "no new commits" in rr["blocked_reason"] and head in rr["blocked_reason"]
    (ev,) = p.db.q("SELECT * FROM events WHERE task=? AND kind LIKE 'task_%' AND kind!='task_waiting'", (fid,))
    assert ev["status"] == "queued" and f"Re-review #{rid} blocked" in ev["text"]


def test_a_re_review_runs_once_its_fix_adds_commits(env):
    p = make(env)
    root, branch, rev, _ = _split_with_a_landing_hop(env, p)
    _fail_review(env, p, rev, [{"title": "refuse an open review", "spec": "abandon refuses a task under review"}])
    fid, rid = _fix_and_re_review(p, rev)
    rr = p.db.task(rid)
    assert p.db.unmet_dependencies([rr])[rid] == [fid], "the re-review can run before its fix"
    _hop(env, p, fid, {"guard.py": 3}, {"status": "done", "summary": "refuses an open review"})
    rr = p.db.task(rid)
    assert rr["status"] == "queued" and p.db.unmet_dependencies([rr])[rid] == []


def _review_events(p, rid):
    from ttp import deadwait
    return p.db.q("SELECT * FROM events WHERE task=? AND status='queued' AND kind IN (?,?,?)",
                  (rid, "task_failed", "task_changes_needed", deadwait.NOFIX_EVENT))


def test_a_failed_review_with_no_fix_follow_ups_gets_no_fix_or_re_review_and_one_event(env):
    p = make(env)
    _, _, _, (rev,) = _finish_code(env, p, "feature", {"a.py": 40})
    ev = _fail_review(env, p, rev["id"], [])
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Fix review%' OR title LIKE 'Re-review%'")
    (only,) = _review_events(p, rev["id"])
    assert only["id"] == ev["id"] and "No automatic fix: it proposed no fix follow-ups" in only["text"]


def test_a_third_failed_review_round_gets_no_fix_or_re_review_and_one_nofix_event(env):
    from ttp.daemon import AUTO_FIX_ROUNDS
    p = make(env)
    p.set_config("review.area_fail_cap", 10)   # the round cap alone, not the area cap
    _, _, _, (rev,) = _finish_code(env, p, "feature", {"a.py": 40})
    rid = rev["id"]
    for n in range(AUTO_FIX_ROUNDS):
        _fail_review(env, p, rid, [{"title": f"bug {n}", "spec": f"fix bug {n}"}])
        fid, rid = _fix_and_re_review(p, rid)
        _hop(env, p, fid, {f"fix{n}.py": 3}, {"status": "done", "summary": f"fixed bug {n}"})
    tasks = p.db.one("SELECT COUNT(*) n FROM tasks")["n"]
    _fail_review(env, p, rid, [{"title": "bug again", "spec": "fix it again"}])
    assert p.db.one("SELECT COUNT(*) n FROM tasks")["n"] == tasks, "a fix or re-review past the round cap"
    from ttp import deadwait
    nofix = [e for e in _review_events(p, rid) if e["kind"] == deadwait.NOFIX_EVENT]
    assert len(nofix) == 1 and f"{AUTO_FIX_ROUNDS + 1} reviews of this stack failed" in nofix[0]["text"]


def test_a_failed_review_at_the_review_area_cap_gets_no_fix_or_re_review_and_one_area_event(env):
    from ttp import reviewcap
    p = make(env)
    p.set_config("review.area_fail_cap", 2)
    fups = [{"title": "handle the edge case", "spec": "parse('') raises"}]
    code_a, _, _, (rev_a,) = _finish_code(env, p, "feature", {"a.py": 40})
    _fail_review(env, p, rev_a["id"], fups)
    tasks = p.db.one("SELECT COUNT(*) n FROM tasks")["n"]
    _, _, _, (rev_b,) = _finish_code(env, p, f"#{code_a} follow-up: feature", {"b.py": 40})
    failed = _fail_review(env, p, rev_b["id"], fups)
    assert p.db.one("SELECT COUNT(*) n FROM tasks")["n"] == tasks + 2, "a fix or re-review past the area cap"
    assert not p.db.q("SELECT id FROM tasks WHERE labels LIKE ?", (f'%"continues:{rev_b["id"]}"%',))
    area = p.db.q("SELECT * FROM events WHERE task=? AND kind=?", (rev_b["id"], reviewcap.REVIEW_AREA_EVENT))
    assert len(area) == 1 and reviewcap.TRIGGER in failed["text"]



def test_the_review_area_cap_event_names_each_failed_reviews_first_finding(env):
    """The re-plan of a capped area read every failed review's hand-off first. The event now carries
    each one's first finding in one line, so it needs no extra reads."""
    from ttp import reviewcap
    from ttp import coordinator as coord
    p = make(env)
    p.set_config("review.area_fail_cap", 2)
    code_a, _, _, (rev_a,) = _finish_code(env, p, "feature", {"a.py": 40})
    _fail_review(env, p, rev_a["id"], [{"title": "parse('') raises", "spec": "an empty\n input   must give []"},
                                       {"title": "second finding", "spec": "not shown"}])
    _, _, _, (rev_b,) = _finish_code(env, p, f"#{code_a} follow-up: feature", {"b.py": 40})
    _fail_review(env, p, rev_b["id"], [{"title": "x" * 400, "spec": "long"}])
    (ev,) = p.db.q("SELECT * FROM events WHERE task=? AND kind=?", (rev_b["id"], reviewcap.REVIEW_AREA_EVENT))
    found = json.loads(ev["data"])["findings"]
    assert found[str(rev_a["id"])] == "parse('') raises: an empty input must give []"
    assert len(found[str(rev_b["id"])]) == reviewcap.FINDING_CHARS and found[str(rev_b["id"])].endswith("…")
    assert f"\n- #{rev_a['id']} first finding: parse('') raises: an empty input must give []\n" in ev["text"]
    assert "second finding" not in ev["text"] and ev["text"].rstrip().endswith("touch those files.")
    assert reviewcap.first_finding({"result": json.dumps({"summary": "the lock  is\nnever freed"})}) \
        == "the lock is never freed"
    assert reviewcap.first_finding({"result": None}) == "no finding recorded"
    # The digest shows it whole.
    assert len(ev["text"]) <= coord.EVENT_CHARS_BY_KIND[reviewcap.REVIEW_AREA_EVENT]

def _ttp_task(p, *args):
    """`ttp task <p> <args>`: its exit code (0 when it returned)."""
    from ttp import cli
    try:
        cli.main(["task", p.name, *args])
    except SystemExit as e:
        return e.code
    return 0


def test_abandon_refuses_a_landed_a_not_done_or_another_kind_of_task_and_needs_a_reason(env, monkeypatch, capsys):
    """`ttp task abandon` takes only a done code task whose work has not landed, with a
    reason; it records why and when, keeps the branch, and `task show` prints it."""
    from ttp import landed
    p, repo, other, landed_tid, path, branch, head = _landed_setup(env, monkeypatch)
    assert landed.check(p, repo, tid=landed_tid, fetch=False)[0] == 0
    running, rpath, _ = _code_task(p, "still running", status="running")
    review = p.db.add_task("Review #1", "r", kind="review", status="done")
    harness = p.db.add_task("harness change", "h", kind="harness", status="done")   # review #3624 finding 2
    tid, tpath, tbranch = _code_task(p, "dropped approach")
    _commit(tpath, "dropped.txt", "dropped\n")
    for args, words in (([str(landed_tid), "dropped"], "has landed"), ([str(running), "dropped"], "is running, not done"),
                        ([str(review), "dropped"], "is a review task"),
                        ([str(harness), "dropped"], "is a harness task, not code"), ([str(tid), "  "], "needs a reason"),
                        (["x", "dropped"], "needs a task id")):
        capsys.readouterr()
        assert _ttp_task(p, "abandon", *args) not in (0, None), args
        assert words in capsys.readouterr().err, args
    assert p.db.q("SELECT * FROM task_abandoned") == []
    assert _ttp_task(p, "abandon", str(tid), "approach dropped; a stricter design landed instead") == 0
    assert f"task #{tid}: abandoned (approach dropped; a stricter design landed instead); its branch {tbranch} is kept" \
        in capsys.readouterr().out
    assert landed.abandoned(p.db, tid)[0] == "approach dropped; a stricter design landed instead"
    assert _git_out(repo, "rev-parse", "--verify", f"refs/heads/{tbranch}")
    assert _ttp_task(p, "abandon", str(tid), "again") not in (0, None)
    assert "already abandoned" in capsys.readouterr().err
    assert _ttp_task(p, "show", str(tid)) == 0
    assert "abandoned " in capsys.readouterr().out
    assert _ttp_task(p, "show", str(tid), "--json") == 0
    assert json.loads(capsys.readouterr().out)["abandoned"]["why"].startswith("approach dropped")


def test_an_abandoned_task_leaves_the_local_only_check_and_its_status_line(env, capsys):
    p = make(env)
    from ttp import daemon as dm
    from ttp.cli import status_text
    from ttp.web import health
    remote = env["tmp"] / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(env["repo"]), str(remote)], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "remote", "add", "origin", str(remote)], check=True)
    tid, path, branch = _code_task(p, "dropped work")
    _commit_file(path, "a")
    d = _local_only_daemon(p)
    _check_local_only(d)
    assert [e["task"] for e in _local_only_events(p)] == [tid]
    assert "1 done task with work only on this machine" in status_text(p)
    assert _ttp_task(p, "abandon", str(tid), "approach dropped") == 0
    assert "only on this machine" not in status_text(p), "the line drops it at once, before the next check"
    assert not health(p, p.db)["local_only"]
    d._local_only_due = 0
    _check_local_only(d)
    assert p.db.kv(dm.KV_LOCAL_ONLY) is None and len(_local_only_events(p)) == 1


def test_the_push_audit_leaves_out_an_abandoned_task(env, monkeypatch, tmp_path):
    from ttp import pushclaim
    p, d, tid, head = _no_remote_claim_setup(env, monkeypatch, tmp_path, False)
    p.db.update_task(tid, status="done", result=json.dumps({"status": "done", "summary": "parser fixed"}))
    assert [g["task"] for g in pushclaim.audit(p)["gaps"]] == [tid]
    assert _ttp_task(p, "abandon", str(tid), "approach dropped") == 0
    a = pushclaim.audit(p)
    assert a["gaps"] == [] and a["tasks"] == 0, a


def test_a_landed_wait_on_an_abandoned_task_is_raised_as_a_broken_probe_naming_why(env, monkeypatch):
    """`landed:#<id>` on an abandoned task never passes: the probe says it is abandoned and why, the
    waiter gets one deferral_probe_broken event naming that, and the dead-wait check raises nothing."""
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    from ttp import deadwait, landed
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    tid, path, branch = _code_task(p, "feature")
    _commit(path, "feature.txt", "feature\n")
    assert coord.apply(p, [{"type": "task_add", "title": "after it", "spec": "s",
                            "start_when": f"landed:#{tid}"}]) == []
    after = _added(p, "after it")
    cfg = {"waiting": {"landed_wait_dead_s": 0}}
    deadwait.check(p.db, cfg, {tid: [p.db.task(after)]}, time.time())
    dead = p.db.q("SELECT id FROM events WHERE kind=? AND status='queued'", (deadwait.DEAD_EVENT,))
    assert len(dead) == 1, "before: nothing open can land it"
    assert _ttp_task(p, "abandon", str(tid), "approach dropped for a stricter design") == 0
    rc, words = landed.check(p, repo, tid=tid)
    assert rc == landed.CANNOT and "abandoned" in words and "approach dropped for a stricter design" in words
    d = dmod.Daemon(p.base)
    _settle_deferred(d, after)
    _settle_deferred(d, after)
    assert not _ready(p, after)
    broken = _events(p, after, "deferral_probe_broken")
    assert len(broken) == 1 and "approach dropped for a stricter design" in broken[0]["text"], broken
    deadwait.check(p.db, cfg, {tid: [p.db.task(after)]}, time.time())
    assert not p.db.q("SELECT id FROM events WHERE kind=? AND status='queued'", (deadwait.DEAD_EVENT,))


def test_abandon_refuses_a_task_whose_review_is_open_or_whose_work_waits_in_the_push_queue(env, monkeypatch, capsys):
    """Review #3624 finding 1: a done code task whose daemon-queued review is still open, or whose
    branch a review approved into the push queue, can still land: abandon refuses it, naming the review
    or the queue entry, and records nothing until that is cancelled."""
    from ttp import landed
    from ttp.daemon import Daemon
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(Daemon, "_precheck", lambda self, task, head: None)
    d = Daemon(s.p.base)
    assert d._auto_review(s.p.db.task(s.code), "made the change") == (s.review, False), \
        "the coordinator's review that depends on it covers it"
    capsys.readouterr()
    assert _ttp_task(s.p, "abandon", str(s.code), "approach dropped") not in (0, None)
    assert f"review task #{s.review} that carries it is queued: cancel that first" in capsys.readouterr().err
    assert _ttp_task(s.p, "cancel", str(s.review)) == 0
    rid, added = d._auto_review(s.p.db.task(s.code), "made the change")
    assert added and s.p.db.task(rid)["title"].startswith(f"Review #{s.code}")
    capsys.readouterr()
    assert _ttp_task(s.p, "abandon", str(s.code), "approach dropped") not in (0, None)
    assert f"review task #{rid} that carries it is queued: cancel that first" in capsys.readouterr().err
    _pq_hand_off(env, s, task=rid)
    [q] = s.p.db.q("SELECT id, status FROM push_queue")
    assert q["status"] == "approved"
    capsys.readouterr()
    assert _ttp_task(s.p, "abandon", str(s.code), "approach dropped") not in (0, None)
    assert f"is queued to push (entry #{q['id']} of task #{rid}, approved): cancel that first" \
        in capsys.readouterr().err
    assert s.p.db.q("SELECT * FROM task_abandoned") == [] and landed.abandoned(s.p.db, s.code) is None


def test_a_landing_recorded_after_abandon_wins_over_it(env, monkeypatch, capsys):
    """Review #3624 finding 1, the other way round: work abandoned and then landed after all (pushed by
    the queue for a review the coordinator queued later, or recorded `superseded`) reads as landed,
    never CANNOT, and `superseded` clears the abandon record."""
    from ttp import landed
    s = _pq(env, monkeypatch)
    assert _ttp_task(s.p, "cancel", str(s.review)) == 0
    assert _ttp_task(s.p, "abandon", str(s.code), "approach dropped") == 0
    assert landed.check(s.p, s.repo, tid=s.code)[0] == landed.CANNOT
    later = s.p.db.add_task("review the feature again", f"review {s.branch}", kind="review", tier="standard",
                            origin="coordinator")
    _pq_hand_off(env, s, task=later)
    sha = _git_out(s.origin, "rev-parse", "proj")
    _pq_pushing(s, sha)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.one("SELECT landed_sha FROM push_queue")["landed_sha"] == sha
    rc, words = landed.check(s.p, s.repo, tid=s.code)
    assert rc == 0, words
    # superseded: the abandoned work lands as a rebased copy and is recorded so.
    p, repo, other = s.p, s.repo, s.other
    tid, path, branch = _code_task(p, "dropped, then landed")
    _commit(path, "late.txt", "late\n")
    assert _ttp_task(p, "abandon", str(tid), "approach dropped") == 0
    _git_out(other, "pull", "-q", "origin", "proj")
    _commit(other, "theirs.txt", "theirs\n")
    _git_out(other, "fetch", "-q", str(path), branch)
    _git_out(other, "cherry-pick", "FETCH_HEAD")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    capsys.readouterr()
    assert _ttp_task(p, "superseded", str(tid), _git_out(other, "rev-parse", "HEAD")) == 0
    assert "its abandon record is cleared" in capsys.readouterr().out
    assert landed.abandoned(p.db, tid) is None and landed.check(p, repo, tid=tid)[0] == 0


_FLAKY_ID = "tests/test_a.py::test_waits_on_a_clock"
_FLAKY_CHECK = "python3 -m pytest -q tests"


def _rerun_stub(monkeypatch, tip, plan, fail_out=None):
    """batch._stream stubbed as the check runner, no real test run: `plan(where, selected)` gives the exit
    code at the tip ("tip") or another head ("head"), `selected` the rerun's TTP_PYTEST_SELECT (None for
    the full check). A failure prints pytest's lines for _FLAKY_ID (`fail_out`: these lines instead).
    Returns the calls (where, selected)."""
    from ttp import batch
    calls = []

    def fake(cmd, cwd, env=None, timeout=None, group=False, pass_fds=()):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(cwd), capture_output=True, text=True).stdout.strip()
        where, selected = "tip" if head == tip else "head", (env or {}).get("TTP_PYTEST_SELECT")
        calls.append((where, selected))
        rc = plan(where, selected)
        out = (f"F...\nFAILED {_FLAKY_ID} - AssertionError: took too long\n1 failed, 3 passed in 0.50s"
               if rc == 1 else "....\n4 passed in 0.50s") if fail_out is None or rc == 0 else fail_out
        return rc, out, False

    monkeypatch.setattr(batch, "_stream", fake)
    return calls


def test_a_push_batch_reruns_a_failing_test_once_at_the_same_head_and_goes_on_when_it_passes(env, monkeypatch):
    """A push check fails on one test that passes on its one rerun at the same head: flaky. The batch
    pushes, the marker records the test, head and command, and the queue tells the coordinator once per
    test (a later batch with the same flake adds no second event within a day). A pass that came from a
    rerun is not recorded as the tree's pass."""
    from ttp import pushpass, pushq
    p, repo, origin, other = _push_setup(env, monkeypatch, [_FLAKY_CHECK])
    tip = _git_out(origin, "rev-parse", "proj")
    calls = _rerun_stub(monkeypatch, tip, lambda where, sel: 1 if sel is None else 0)
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e1", {"f1.txt": "1\n"})]), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed" and _statuses(m) == ["pushed"], m
    assert calls == [("head", None), ("head", "^(?:test_waits_on_a_clock)$")], "one rerun, only the failing test"
    [f] = m["flaky_tests"]
    assert f["ids"] == [_FLAKY_ID] and f["cmd"] == _FLAKY_CHECK and f["head"] == m["pushed_sha"]
    assert m["checks"]["flaky"] is True and m["checks"]["reruns"] == 1
    assert not pushpass.passed(p, pushpass.tree_of(p.root, m["pushed_sha"]), _FLAKY_CHECK,
                               pushpass.env_id(p.root, _FLAKY_CHECK, {}, tip)), "a rerun pass is not the tree's pass"
    st, now = {}, time.time()
    for bid in ("b1", "b2"):
        pushq._flaky_told(p.db, st, {"id": bid}, m, now)
    evs = p.db.q("SELECT * FROM events WHERE kind=?", (pushq.FLAKY_EVENT,))
    assert len(evs) == 1 and evs[0]["status"] == "queued", evs
    assert _FLAKY_ID in evs[0]["text"] and "b1" in evs[0]["text"] and "deterministic" in evs[0]["text"]
    pushq._flaky_told(p.db, st, {"id": "b3"}, m, now + pushq.FLAKY_TOLD_S + 1)
    assert len(p.db.q("SELECT * FROM events WHERE kind=?", (pushq.FLAKY_EVENT,))) == 2, "told again after a day"


@pytest.mark.parametrize("rc,out", [
    (1, f"F\nFAILED {_FLAKY_ID} - AssertionError: took too long\n!!!!! stopping after 1 failures !!!!!\n1 failed in 0.03s"),
    (2, f"F\nFAILED {_FLAKY_ID} - AssertionError: took too long\n!!!!! KeyboardInterrupt !!!!!\n"
        "/x/test_a.py:3: KeyboardInterrupt\n(to show a full traceback on KeyboardInterrupt use --full-trace)\n"
        "1 failed, 1 passed in 0.50s"),
    (2, f"F\nFAILED {_FLAKY_ID} - AssertionError: took too long\n1 failed in 0.50s"),
], ids=["stopped-after-1", "interrupted", "exit-2"])
def test_a_push_batch_never_reruns_a_check_pytest_stopped_early_or_that_did_not_exit_1(env, monkeypatch, rc, out):
    """A check pytest stopped early (-x, --maxfail: later tests never ran) or interrupted, or one that did
    not exit 1, names only the failures before the stop: a passing rerun of those would pass a suite that
    never ran in full. No rerun, at the head or on the tip: the entry ends check_failed, nothing flaky."""
    p, repo, origin, other = _push_setup(env, monkeypatch, [_FLAKY_CHECK])
    tip = _git_out(origin, "rev-parse", "proj")
    calls = _rerun_stub(monkeypatch, tip, lambda where, sel: 0 if where == "tip" else rc, fail_out=out)
    rc_, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e1", {"f1.txt": "1\n"})]), monkeypatch)
    assert _statuses(m) == ["check_failed"] and m["outcome"] != "tip_failed", m
    assert not m.get("flaky_tests") and m["checks"]["flaky"] is False and not m["checks"].get("reruns"), m
    assert all(sel is None for _, sel in calls), calls
    assert _git_out(origin, "rev-parse", "proj") == tip


def test_a_push_batch_reports_the_tip_broken_when_the_failing_tests_fail_on_the_tip_alone_too(env, monkeypatch):
    """The failing test fails again on its rerun at the head and on its one rerun on the tip without the
    batch: the tip is broken (tip_failed), not the change, and the tip is not checked alone in full."""
    p, repo, origin, other = _push_setup(env, monkeypatch, [_FLAKY_CHECK])
    tip = _git_out(origin, "rev-parse", "proj")
    calls = _rerun_stub(monkeypatch, tip, lambda where, sel: 1)
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e1", {"f1.txt": "1\n"})]), monkeypatch)
    assert m["outcome"] == "tip_failed" and _statuses(m) == ["requeued"] and rc != 0, m
    assert m["tip_check"]["cmd"] == _FLAKY_CHECK and _FLAKY_ID in m["tip_check"]["tail"]
    assert "fails its checks on its own" in m["message"] and not m.get("flaky_tests")
    sel = "^(?:test_waits_on_a_clock)$"
    assert calls == [("head", None), ("head", sel), ("tip", sel)], calls
    assert _git_out(origin, "rev-parse", "proj") == tip


def test_a_push_batch_blames_the_change_when_its_failing_test_fails_twice_on_it_and_passes_on_the_tip(env, monkeypatch):
    """A test that fails on both runs at the heads never passes, and one that passes on every rerun on the
    tip alone is the change's own failure: today's check_failed. Each test is rerun at most once per batch
    at a head and once in TIP_RERUNS runs on the tip, however many prefixes the search checks."""
    from ttp import batch
    p, repo, origin, other = _push_setup(env, monkeypatch, [_FLAKY_CHECK])
    tip = _git_out(origin, "rev-parse", "proj")
    calls = _rerun_stub(monkeypatch, tip, lambda where, sel: 0 if where == "tip" else 1)
    heads = [_entry(repo, n, {f"{n}.txt": f"{n}\n"}) for n in ("e1", "e2")]
    rc, m = _run_batch(p, _batch_marker(p, heads), monkeypatch)
    assert _statuses(m) == ["check_failed", "requeued"] and m["outcome"] != "tip_failed", m
    assert not m.get("flaky_tests") and m["checks"]["flaky"] is False and m["checks"]["reruns"] == 1 + batch.TIP_RERUNS
    assert m["checks"]["tip_reruns"] == batch.TIP_RERUNS and not m.get("pre_existing")
    sel = "^(?:test_waits_on_a_clock)$"
    assert [c for c in calls if c[1] == sel] == [("head", sel)] + [("tip", sel)] * batch.TIP_RERUNS, calls
    assert ("tip", None) not in calls, "the tip is not checked alone in full"
    assert calls.count(("head", None)) == 3, "full head, prefix 1, and the entry after it: no further reruns"
    assert _git_out(origin, "rev-parse", "proj") == tip


def _seq_plan(head_full, tip_sel):
    """A _rerun_stub plan: the full checks at a head exit with `head_full` in turn, a rerun at a head with
    1, the reruns on the tip with `tip_sel` in turn (0 once spent); the tip is never checked in full."""
    head_full, tip_sel = iter(head_full), iter(tip_sel)

    def plan(where, sel):
        assert not (where == "tip" and sel is None), "the tip is not checked alone in full"
        return next(head_full) if sel is None else 1 if where == "head" else next(tip_sel, 0)
    return plan


def test_a_push_batch_does_not_send_back_a_later_entry_whose_failing_test_fails_on_the_tip_now_and_then(env, monkeypatch, capsys):
    """A test already on the tip fails now and then: the full head fails on it, prefix 1 passes by
    chance, the full head fails again. Before entry 2 is blamed its failing test reruns on the tip alone;
    it fails there on the 2nd rerun: pre-existing. Entry 1 (which passed) goes, entry 2 is requeued, not
    sent back; the marker and the batch log record it and the reruns, and the coordinator is told once."""
    from ttp import batch, pushq
    p, repo, origin, other = _push_setup(env, monkeypatch, [_FLAKY_CHECK])
    tip = _git_out(origin, "rev-parse", "proj")
    calls = _rerun_stub(monkeypatch, tip, _seq_plan([1, 0, 1], [0, 1]))
    heads = [_entry(repo, n, {f"{n}.txt": f"{n}\n"}) for n in ("e1", "e2")]
    marker = _batch_marker(p, heads)
    rc, m = _run_batch(p, marker, monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed" and _statuses(m) == ["pushed", "requeued"], m
    [pre] = m["pre_existing"]
    assert pre["ids"] == [_FLAKY_ID] and pre["tip"] == tip and pre["entry"] == 2 and pre["cmd"] == _FLAKY_CHECK
    assert (pre["runs"], pre["of"]) == (2, batch.TIP_RERUNS) and m["checks"]["tip_reruns"] == 2
    sel = "^(?:test_waits_on_a_clock)$"
    assert calls.count(("tip", sel)) == 2, "it stops at the first failing rerun on the tip"
    assert _git_out(origin, "rev-parse", "proj") == m["pushed_sha"] != tip
    out = capsys.readouterr()
    log = out.out + out.err
    assert "failed on the tip" in log and "rerun 2 of 3" in log and "entry 2 is not sent back" in log, log
    pushq._flaky_told(p.db, {}, {"id": "b1"}, m, time.time())
    [ev] = p.db.q("SELECT * FROM events WHERE kind=?", (pushq.FLAKY_EVENT,))
    assert _FLAKY_ID in ev["text"] and "pre-existing" in ev["text"] and tip[:7] in ev["text"], ev["text"]


def test_a_push_batch_reports_the_tip_broken_when_entry_1s_failing_test_fails_on_a_later_tip_rerun(env, monkeypatch):
    """Entry 1 fails on a test that passes on its first rerun on the tip and fails on the second: the
    tip's own failure (tip_failed), not entry 1's; it is requeued, not sent back."""
    p, repo, origin, other = _push_setup(env, monkeypatch, [_FLAKY_CHECK])
    tip = _git_out(origin, "rev-parse", "proj")
    calls = _rerun_stub(monkeypatch, tip, _seq_plan([1], [0, 1]))
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e1", {"f1.txt": "1\n"})]), monkeypatch)
    assert m["outcome"] == "tip_failed" and _statuses(m) == ["requeued"] and rc != 0, m
    assert calls.count(("tip", "^(?:test_waits_on_a_clock)$")) == 2 and m["checks"]["tip_reruns"] == 2, calls
    assert _git_out(origin, "rev-parse", "proj") == tip


def test_a_push_batch_still_sends_back_a_later_entry_whose_failing_test_passes_on_the_tip_every_rerun(env, monkeypatch):
    """Prefix 1 passes and the full head fails twice on a test that passes on all TIP_RERUNS reruns on the
    tip alone: entry 2's own failure. It is sent back (check_failed) as before; entry 1 goes."""
    from ttp import batch
    p, repo, origin, other = _push_setup(env, monkeypatch, [_FLAKY_CHECK])
    tip = _git_out(origin, "rev-parse", "proj")
    calls = _rerun_stub(monkeypatch, tip, _seq_plan([1, 0, 1], []))
    heads = [_entry(repo, n, {f"{n}.txt": f"{n}\n"}) for n in ("e1", "e2")]
    rc, m = _run_batch(p, _batch_marker(p, heads), monkeypatch)
    assert m["outcome"] == "pushed" and _statuses(m) == ["pushed", "check_failed"], m
    assert not m.get("pre_existing") and m["checks"]["tip_reruns"] == batch.TIP_RERUNS
    assert calls.count(("tip", "^(?:test_waits_on_a_clock)$")) == batch.TIP_RERUNS, calls
    assert _git_out(origin, "rev-parse", "proj") == m["pushed_sha"] != tip


def test_stray_running_batch_file_is_closed_at_daemon_start_and_a_live_one_is_left(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(pushq.runner, "boot_time", lambda: None)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    row = s.p.db.one("SELECT marker FROM push_batches WHERE id=?", (bid,))
    marker = __import__("pathlib").Path(row["marker"])
    _pq_tend(s)
    # Put the file back as the old code left it, then let the daemon-start step close it.
    m = json.loads(marker.read_text())
    for k in ("outcome", "ended", "rerun_by"):
        m.pop(k, None)
    m.update(status="running", phase="push")
    marker.write_text(json.dumps(m))
    assert pushq.close_strays(s.p) == [bid]
    m = json.loads(marker.read_text())
    assert m["status"] == "finished" and m["outcome"] == "died" and m["ended"]
    # A row that is not final is never touched.
    m.update(status="running")
    m.pop("outcome")
    marker.write_text(json.dumps(m))
    s.p.db.x("UPDATE push_batches SET finalized=NULL WHERE id=?", (bid,))
    assert pushq.close_strays(s.p) == []
    assert json.loads(marker.read_text())["status"] == "running"


def test_a_stored_landed_wait_without_a_way_to_check_it_waits_on_the_task_being_done(env, monkeypatch):
    """No delivery.push_branch and no delivery.landed_probe: a `landed:#<id>` already stored (or a
    `ttp landed --task <id> && ...` chain) used to exit 2 on every probe, a broken-probe coordinator
    turn each time. The daemon's probes now wait on the task being done, as depends_on would, and no
    landed_wait_dead event is raised for a landing nothing could check."""
    from ttp import daemon as dmod
    p = make(env)
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    tid = p.db.add_task("producer", "s", kind="code", status="running")
    now = f"deferred_since:{time.time():.0f}"
    plain = p.db.add_task("after it", "s", kind="work", status="queued",
                          labels=[f"start_when:landed:#{tid}", now])
    chain = p.db.add_task("after it too", "s", kind="work", status="queued",
                          labels=[f"start_when:ttp landed --task {tid} && test -e ok", now])
    (p.root / "ok").write_text("")
    d = dmod.Daemon(p.base)
    for t in (plain, chain):
        _settle_deferred(d, t)
        assert not _ready(p, t) and not _events(p, t, "deferral_probe_broken"), t
    p.db.update_task(tid, status="done")
    for t in (plain, chain):
        _settle_deferred(d, t, until=lambda: _ready(p, t))
        assert _ready(p, t) and not _events(p, t, "deferral_probe_broken"), t
    # A done producer with nothing open to land it is no dead wait here: its end is the wait's end.
    from ttp import deadwait
    gone = p.db.add_task("done, never landed", "s", kind="code", status="done")
    held = p.db.add_task("after that", "s", kind="work", status="queued", labels=[f"start_when:landed:#{gone}"])
    rows = p.db.q("SELECT * FROM tasks WHERE id=?", (held,))
    for at in (time.time(), time.time() + 2 * deadwait.GRACE_S):
        d._dead_landed_waits(rows, at)
    assert not p.db.q("SELECT * FROM events WHERE kind='landed_wait_dead'")


def test_ttp_landed_by_hand_keeps_exit_2_where_no_landing_can_be_checked(env, monkeypatch, capsys):
    """`ttp landed` run by hand (no landed.DONE_ENV) keeps its exit 2 and NO_CHECK message even for a
    done task: only the daemon's probes read a done task as landed."""
    from ttp import cli, landed
    p = make(env)
    monkeypatch.chdir(p.base)
    monkeypatch.delenv(landed.DONE_ENV, raising=False)
    tid = p.db.add_task("producer", "s", kind="code", status="done")

    def run(*args):
        try:
            cli.main(["landed", *args])
            rc = 0
        except SystemExit as e:
            rc = int(e.code or 0)
        return rc, capsys.readouterr().out.strip()

    assert run(f"#{tid}") == (landed.CANNOT, landed.NO_CHECK)
    assert run("--task", str(tid)) == (landed.CANNOT, landed.NO_CHECK)
    monkeypatch.setenv(landed.DONE_ENV, "1")
    assert run("--task", str(tid))[0] == 0
    failed = p.db.add_task("failed producer", "s", kind="code", status="failed")
    assert landed.check(p, p.root, tid=failed)[0] == landed.NOT_YET


@pytest.mark.parametrize("probe,ids,rest", [
    ("landed:#5", [5], ""),
    ("ttp landed --task 5", [5], ""),
    ("ttp landed --task 5 && test -e x", [5], "test -e x"),
    ("test -e x && ttp landed '#5' && ttp landed --task=6", [5, 6], "test -e x"),
    ("ttp landed --task 5 && ttp landed --task 5", [5], ""),
    ("ttp landed --task 5 || true", [], "ttp landed --task 5 || true"),
    ("ttp landed --task 5; true", [], "ttp landed --task 5; true"),
    ("ttp landed --task 5 2>&1 && true", [], "ttp landed --task 5 2>&1 && true"),
    ("ttp landed --task 5 --onto x && true", [], "ttp landed --task 5 --onto x && true"),
    ("ttp landed --task 5 && echo 'a && b", [], "ttp landed --task 5 && echo 'a && b"),
    ("ttp landed --task 5 &&", [], "ttp landed --task 5 &&"),
    ("test $(ttp landed --task 5) && true", [], "test $(ttp landed --task 5) && true"),
])
def test_split_landed_takes_out_only_the_terms_of_a_plain_and_chain(probe, ids, rest):
    from ttp import landed
    assert landed.split_landed(probe) == (ids, rest)


def test_a_landed_wait_without_a_push_branch_runs_the_landed_probe_or_names_the_fix(env, monkeypatch):
    """No delivery.push_branch: `landed:#<id>` (`ttp landed --task <id>`) used to exit 2 with 'no
    target branch' on every check, each a broken-probe coordinator turn. Unset delivery.landed_probe:
    one clear error naming depends_on and the key. Set: its exit 0 and 1 are the answer, any other
    exit (or a timeout) is CANNOT; `{id}` is the task id; a local-only check does not run it; a
    deferred task starts once it passes. A push branch still wins over the key."""
    from ttp import daemon as dmod
    from ttp import landed
    p = make(env)
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    tid = p.db.add_task("producer", "s", kind="code", status="done")
    rc, words = landed.check(p, p.root, tid=tid)
    assert rc == landed.CANNOT and "no target branch" not in words, words
    assert "depends_on" in words and "delivery.landed_probe" in words and "delivery.push_branch" in words
    assert landed.check(p, p.root, tid=tid + 99) == (landed.CANNOT, f"no task #{tid + 99} in this project")
    p.set_config("delivery.landed_probe", "test -e landed/{id} || { echo waiting; exit 1; }")
    assert landed.check(p, p.root, tid=tid) == (landed.NOT_YET, f"#{tid} has not landed yet (delivery.landed_probe): waiting")
    assert landed.check(p, p.root, tid=tid, fetch=False)[0] == landed.NOT_YET
    after = p.db.add_task("after it", "s", kind="work", status="queued",
                          labels=[f"start_when:landed:#{tid}", f"deferred_since:{time.time():.0f}"])
    d = dmod.Daemon(p.base)
    _settle_deferred(d, after)
    assert not _ready(p, after) and not _events(p, after, "deferral_probe_broken")
    (p.root / "landed").mkdir()
    (p.root / "landed" / str(tid)).write_text("")
    assert landed.check(p, p.root, tid=tid)[0] == 0
    assert landed.check(p, p.root, tid=tid, fetch=False)[0] == landed.NOT_YET   # never run on a local check
    _settle_deferred(d, after, until=lambda: _ready(p, after))
    assert _ready(p, after)
    p.set_config("delivery.landed_probe", "exit 3")
    rc, words = landed.check(p, p.root, tid=tid)
    assert rc == landed.CANNOT and "delivery.landed_probe" in words and "exited 3" in words
    monkeypatch.setattr(landed, "FETCH_S", 1)
    p.set_config("delivery.landed_probe", "sleep 30")
    t0 = time.time()
    rc, words = landed.check(p, p.root, tid=tid)
    assert rc == landed.CANNOT and "ran over" in words and time.time() - t0 < 10
    # A push branch: the key is ignored and the landing is looked for on that branch.
    p.set_config("delivery.landed_probe", "true")
    p.set_config("delivery.push_branch", "no/such/branch")
    rc, words = landed.check(p, p.root, tid=tid)
    assert rc != 0 and "delivery.landed_probe" not in words, words


def test_a_cancelled_tasks_detached_push_is_stopped_and_its_queued_push_never_starts(env, monkeypatch, capsys,
                                                                                    reap_tmp_processes):
    """`ttp push --detach` leads a session of its own, outside every run and every `ttp detach` job: once its
    task is cancelled (and no open task waits on its marker) the daemon's jobstop pass ends its group and the
    marker says why; a push still queued for the daemon is recorded as failed and never started."""
    from ttp import jobstop, push
    started = env["tmp"] / "started"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"touch {started}; sleep 60"])
    tid = p.db.add_task("soak", "s", kind="code", tier="light", origin="user")
    monkeypatch.setenv("TTP_TASK", str(tid))
    _git_out(repo, "checkout", "-q", "-b", f"ttp/t{tid}-soak")
    _commit(repo, "mine.txt", "mine\n")

    def own_detach():
        rc = _ttp("push", "--own", "--detach")
        out = capsys.readouterr()
        lines = dict(ln.split(": ", 1) for ln in out.out.splitlines() if ln.startswith(("marker: ", "retry_when: ")))
        assert rc == 0 and "marker" in lines, out.err
        return pathlib.Path(lines["marker"]), lines["retry_when"]

    marker, probe = own_detach()
    pid = json.loads(marker.read_text())["pid"]
    try:
        assert _wait_for(started.exists)
        assert jobstop.sweep(p) == [], "the push of an open task was stopped"
        waiter = p.db.add_task("waiter", "s", kind="work", tier="light", origin="user")
        p.db.update_task(waiter, status="queued", result=json.dumps({"status": "waiting", "retry_when": probe}))
        p.db.update_task(tid, status="cancelled")
        assert jobstop.sweep(p) == [], "a push an open task waits on was stopped"
        p.db.update_task(waiter, status="done")
        (r,) = jobstop.sweep(p)
        assert r["job"] == "push" and r["pgid"] == pid and f"task #{tid} is cancelled" in r["text"]
        assert _wait_for(lambda: not jobstop.members(pid), 10), "the push's check outlived it"
        m = json.loads(marker.read_text())
        assert m["status"] == "failed" and f"task #{tid} is cancelled" in m["reason"]
        assert _probe(p, probe).returncode == 0
        assert subprocess.run(["git", "-C", str(origin), "rev-parse", "--verify", "-q", f"ttp/t{tid}-soak"],
                              capture_output=True).returncode != 0, "the stopped push went through"
        assert jobstop.sweep(p) == [] and len(_stopped_events(p)) == 1
        monkeypatch.setattr(push, "_boxed", lambda: True)
        queued, _ = own_detach()
        monkeypatch.setattr(push, "_boxed", lambda: False)
        assert json.loads(queued.read_text())["status"] == "queued"
        (r,) = jobstop.sweep(p)
        assert "never started" in r["text"] and r["rc"] == str(queued)
        push.tend(p)
        q = json.loads(queued.read_text())
        assert q["status"] == "failed" and "pid" not in q and f"task #{tid} is cancelled" in q["reason"]
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        reap_tmp_processes(env["tmp"])


def _safe_read(path):
    try:
        return path.read_bytes()
    except OSError:
        return b""


def test_a_push_check_past_its_time_limit_is_ended_and_fails_the_push_loudly(env, monkeypatch, capsys):
    """A push check that never ends (a hung test, or one waiting on a slot that never frees) must not hold
    the push, its host check slot and the task waiting on it forever."""
    from ttp import push
    started = env["tmp"] / "started"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"touch {started}; sleep 61.7; true"])
    before = _git_out(origin, "rev-parse", "proj")
    _commit(repo, "mine.txt", "mine\n")
    p.set_config("delivery.push_checks_timeout_s", 1)
    t0 = time.time()
    assert _ttp("push") == push.CHECKS_FAILED
    err = capsys.readouterr().err
    assert time.time() - t0 < 30 and started.exists()

    def sleeping():
        return [d for d in pathlib.Path("/proc").glob("[0-9]*")
                if (d / "cmdline").exists() and _safe_read(d / "cmdline") == b"sleep\x0061.7\x00"]
    assert _wait_for(lambda: not sleeping(), 5), "the timed-out check's children outlived it"
    assert "check timed out after 1 s and was ended" in err and "push_checks_timeout_s" in err, err
    assert _git_out(origin, "rev-parse", "proj") == before
    # Unset: four times the last measured check run, never under 4 h; 0 is no limit; nonsense is refused.
    assert push.check_limit_of(None, None) == push.check_limit_of("", 60.0) == 4 * 3600
    assert push.check_limit_of(None, 7200.0) == 4 * 7200
    assert push.check_limit_of(0, 7200.0) is None and push.check_limit_of("0", None) is None
    assert push.check_limit_of("90", None) == 90.0
    for bad in ("soon", -1, True, [1], "nan"):
        with pytest.raises(ValueError):
            push.check_limit_of(bad, None)
    p.set_config("delivery.push_checks_timeout_s", "soon")
    assert _ttp("push") == push.REFUSED and "push_checks_timeout_s" in capsys.readouterr().err


def test_task_show_prints_a_superseded_record_with_its_commit_and_whether_it_is_on_the_push_branch(env, monkeypatch, capsys):
    import json
    from ttp import landed
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    tip = _git_out(repo, "rev-parse", landed._push_ref(p, repo))
    p.db.x("INSERT INTO task_superseded(task, head, sha, ts) VALUES(?,?,?,?)", (tid, head, tip, 1000.0))
    p.db.x("INSERT INTO task_superseded(task, head, sha, ts) VALUES(?,?,?,?)", (tid, "b" * 40, "c" * 40, 2000.0))
    capsys.readouterr()
    assert _ttp_task(p, "show", str(tid)) == 0
    out = capsys.readouterr().out
    assert f"superseded by {tip[:10]} (on the push branch), recorded" in out
    assert f"superseded by {'c' * 10} (not on the push branch), recorded" in out
    assert _ttp_task(p, "show", str(tid), "--json") == 0
    rows = json.loads(capsys.readouterr().out)["superseded"]
    assert [(r["sha"], r["on_push_branch"]) for r in rows] == [(tip, True), ("c" * 40, False)]


def test_task_show_prints_an_abandoned_record_with_its_reason_and_time(env, monkeypatch, capsys):
    import json
    p, repo, other, landed_tid, path, branch, head = _landed_setup(env, monkeypatch)
    tid, tpath, tbranch = _code_task(p, "dropped approach")
    _commit(tpath, "dropped.txt", "dropped\n")
    assert _ttp_task(p, "abandon", str(tid), "stricter design landed instead") == 0
    capsys.readouterr()
    assert _ttp_task(p, "show", str(tid)) == 0
    assert "abandoned " in (out := capsys.readouterr().out) and ": stricter design landed instead" in out
    assert _ttp_task(p, "show", str(tid), "--json") == 0
    ab = json.loads(capsys.readouterr().out)["abandoned"]
    assert ab["why"] == "stricter design landed instead" and ab["ts"] > 0


_EXT_REPO = "example-org/example-widgets"


def _ext_handoff(head, summary="published elsewhere", **extra):
    return {"status": "done", "summary": summary,
            "published": {"repo": _EXT_REPO, "branch": "ttp/t1-example", "head": head, **extra}}


def test_the_published_handoff_field_is_validated_and_an_invalid_one_is_ignored_with_its_reason(env):
    """`published` names a change in another repository: branch and head required, the head a full or
    abbreviated hex sha, the repo owner/name or a remote URL without credentials. Anything else is
    ignored with a logged reason, never a crash, and queues no review."""
    from ttp import extchange
    sha = "ab" * 20
    ok = [{"repo": _EXT_REPO, "branch": "ttp/t1-x", "head": sha},
          {"repo": "https://git.example.com/acme/widgets.git", "branch": "feature/x", "head": "ABC1234"},
          {"repo": "ssh://git@git.example.com:2222/acme/widgets", "branch": "x", "head": sha},
          {"repo": "git@git.example.com:acme/widgets.git", "branch": "x", "head": sha, "extra": 1}]
    for v in ok:
        ch, why = extchange.parse(v)
        assert ch and not why and ch["head"] == v["head"].lower() and ch["repo"] == v["repo"], v
    assert extchange.parse(None) == (None, "") and extchange.parse({}) == (None, "")
    bad = {"not an object": ["acme/w ttp/x abc1234", ["acme/w"], 7],
           "no branch": [{"repo": _EXT_REPO, "head": sha}, {"repo": _EXT_REPO, "branch": " ", "head": sha}],
           "no head": [{"repo": _EXT_REPO, "branch": "x"}, {"repo": _EXT_REPO, "branch": "x", "head": 12345678}],
           "no repo": [{"branch": "x", "head": sha}],
           "not a hex commit sha": [{"repo": _EXT_REPO, "branch": "x", "head": "abc12"},
                                    {"repo": _EXT_REPO, "branch": "x", "head": "main"},
                                    {"repo": _EXT_REPO, "branch": "x", "head": "g" * 40}],
           "not a plain branch name": [{"repo": _EXT_REPO, "branch": b, "head": sha}
                                       for b in ("-x", "a..b", "a//b", "x/", "x.lock", "a b", "x;rm", "$(x)")],
           "neither owner/name nor a remote URL": [{"repo": r, "branch": "x", "head": sha} for r in (
               "widgets", "/srv/repos/widgets", "file:///srv/widgets", "https://user:secret@git.example.com/a/b",
               "acme/w;rm -rf", "-c/x", "acme/w/../x y", "`x`/y")]}
    for reason, values in bad.items():
        for v in values:
            ch, why = extchange.parse(v)
            assert ch is None and reason in why, (v, why)
    # In the daemon: an invalid entry is logged with its reason and no review is queued.
    from ttp.daemon import Daemon
    p = make(env)
    tid = p.db.add_task("bad entry", "spec", kind="code", origin="coordinator")
    p.db.update_task(tid, status="review", result=json.dumps(
        {"status": "done", "summary": "ok", "published": {"repo": _EXT_REPO, "branch": "x", "head": "zzz"}}))
    assert Daemon(p.base)._auto_review(p.db.task(tid), "ok") is None
    assert f"task {tid}: hand-off `published` ignored: head 'zzz' is not a hex commit sha" \
        in (p.logs / "daemon.log").read_text()
    assert not p.db.q("SELECT id FROM tasks WHERE kind='review'")


def test_a_code_task_that_published_in_another_repo_gets_one_review_only_review_per_head(env):
    """A finished code task whose project branch changes nothing (or that has no branch) but whose
    hand-off names a valid published change gets exactly one `Review #<id>` from the hand-off and the
    review sweep alike: standard (no diff to size), naming the repo, branch and head, fetched read-only
    into a scratch clone, review only with a no_push line. A sweep after it, a daemon restart or a
    repeated hand-off of the same head (also abbreviated) adds none; a new head gets its own."""
    from ttp import push
    from ttp.daemon import Daemon
    p = make(env)
    _with_origin(env)
    sha = "cd" * 20
    tid, branch, done, (rev,) = _finish_code(env, p, "widget fix", {}, result=_ext_handoff(sha))
    assert branch and rev["title"] == f"Review #{tid}: widget fix" and rev["origin"] == "daemon"
    assert rev["tier"] == "standard" and f"Review #{rev['id']} queued by the daemon" in done["text"]
    spec = rev["spec"]
    assert f"repo {_EXT_REPO}, branch ttp/t1-example, head {sha[:12]}" in spec
    assert f"gh repo clone {_EXT_REPO} scratch -- --no-checkout" in spec and "scratch clone under your own worktree" in spec
    assert f"review exactly head {sha}" in spec and "\nno_push: change lives in an external repo\n" in spec
    assert "Review only: if it passes, hand off `done`. Do not push anything" in spec
    assert "ttp push` from" not in spec and "ttp push --own" not in spec and "precheck" not in spec.lower()
    assert push.REVIEW_ONLY_LABEL in push._labels(rev) and f"published_head:{sha}" in push._labels(rev)
    d = Daemon(p.base)
    # Sweeps and a restart while it is open, and after it ended: no second review of this head.
    p.db.update_task(tid, status="review")
    _ran(p, tid, time.time() - 1900)
    for status in ("queued", "done", "failed"):
        p.db.update_task(rev["id"], status=status)
        _sweep(d)
        _sweep(Daemon(p.base))
        assert d._auto_review(p.db.task(tid), "again") == (rev["id"], False), status
        assert len(p.db.q("SELECT id FROM tasks WHERE kind='review'")) == 1, status
    # A repeated hand-off of the same head, abbreviated: still that review.
    p.db.update_task(tid, result=json.dumps(_ext_handoff(sha[:9].upper())))
    assert d._auto_review(p.db.task(tid), "again") == (rev["id"], False)
    # A new head is a new change: one review of it.
    new = "ef" * 20
    p.db.update_task(tid, result=json.dumps(_ext_handoff(new)))
    rid2, added = d._auto_review(p.db.task(tid), "again")
    assert added and f"head {new}" in p.db.task(rid2)["spec"]
    assert d._auto_review(p.db.task(tid), "again") == (rid2, False)
    # A task with no branch on record at all, left in review: the sweep queues its one review.
    bare = p.db.add_task("no branch", "spec", kind="code", origin="coordinator")
    p.db.update_task(bare, status="review", result=json.dumps(_ext_handoff(sha, "branchless")))
    _ran(p, bare, time.time() - 1900)
    _sweep(Daemon(p.base))
    (brev,) = p.db.q("SELECT * FROM tasks WHERE kind='review' AND title=?", (f"Review #{bare}: no branch",))
    assert f"repo {_EXT_REPO}, branch ttp/t1-example, head {sha[:12]}" in brev["spec"] and "branchless" in brev["spec"]
    _sweep(Daemon(p.base))
    assert len(p.db.q("SELECT id FROM tasks WHERE kind='review' AND title=?", (f"Review #{bare}: no branch",))) == 1
    # Its review never reuses another review's verdict on a head of the project's git.
    assert d._reuse_review(p.db.task(brev["id"])) is False


def test_a_published_entry_changes_nothing_for_other_tasks(env):
    """A code task with neither a project diff nor a published change still gets no review; a code
    task with a project diff is reviewed from the project's git as before, whatever it publishes;
    a non-code task's `published` queues nothing."""
    from ttp.daemon import Daemon
    p = make(env)
    _, _, _, reviews = _finish_code(env, p, "nothing at all", {})
    assert reviews == []
    tid, branch, _, (rev,) = _finish_code(env, p, "in repo", {"app.py": 5}, result=_ext_handoff("ab" * 20))
    assert branch in rev["spec"] and "external repository" not in rev["spec"] and "no_push:" not in rev["spec"]
    assert "published_head:" not in rev["labels"]
    other = p.db.add_task("a plan", "spec", kind="plan", origin="coordinator")
    p.db.update_task(other, status="review", result=json.dumps(_ext_handoff("ab" * 20)))
    _ran(p, other, time.time() - 1900)
    _sweep(Daemon(p.base))
    assert not p.db.q("SELECT id FROM tasks WHERE kind='review' AND title LIKE ?", (f"Review #{other}:%",))


def test_a_failed_review_of_an_external_change_gets_a_fix_that_publishes_to_the_same_branch(env):
    """A failed review of an external change follows the fix/re-review flow: the fix is told to publish
    onto the same external branch and name its new head in `published`; the re-review is review only;
    the fix's hand-off gives the queued re-review its new head once, and queues no second review."""
    from ttp import push
    from ttp.daemon import Daemon
    from ttp.db import continues_id, dependency_ids
    from ttp.providers.base import RunUsage as Usage
    p = make(env)
    sha = "cd" * 20
    code, _, _, (rev,) = _finish_code(env, p, "widget fix", {}, result=_ext_handoff(sha))
    waiter = p.db.add_task("use it", "s", origin="coordinator", depends_on=[rev["id"]])
    failed = _fail_review(env, p, rev["id"], [{"title": "add the missing test", "spec": "test_y must fail"}])
    (fix,) = p.db.q("SELECT * FROM tasks WHERE kind='code' AND origin='daemon'")
    (re_rev,) = p.db.q("SELECT * FROM tasks WHERE kind='review' AND id!=?", (rev["id"],))
    assert f"Fix #{fix['id']} and re-review #{re_rev['id']}" in failed["text"]
    assert continues_id(fix) == code and "test_y must fail" in fix["spec"]
    assert f"publish the fix to the same branch ttp/t1-example of {_EXT_REPO}" in fix["spec"]
    assert '"published": {"repo": "' + _EXT_REPO + '", "branch": "ttp/t1-example", "head": "<new head sha>"}' in fix["spec"]
    assert dependency_ids(re_rev) == [fix["id"]] and re_rev["tier"] == "standard"
    assert push.REVIEW_ONLY_LABEL in push._labels(re_rev) and "\nno_push: change lives in an external repo\n" in re_rev["spec"]
    assert "ttp push` from" not in re_rev["spec"] and dependency_ids(p.db.task(waiter)) == [re_rev["id"]]
    # The fix publishes a new head: the queued re-review learns it, once; no new review.
    new = "ef" * 20
    p.db.update_task(fix["id"], status="running")
    run_dir = env["tmp"] / f"run-{fix['id']}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(_ext_handoff(new, "fixed")))
    Daemon(p.base)._finish_worker({"task": fix["id"]}, Usage(cost_usd=1.0), "ok", run_dir)
    r = p.db.task(re_rev["id"])
    assert f"published_head:{new}" in push._labels(r) and r["spec"].count(f"review exactly head {new}") == 1
    assert len(p.db.q("SELECT id FROM tasks WHERE kind='review'")) == 2
    assert Daemon(p.base)._auto_review(p.db.task(fix["id"]), "fixed") == (re_rev["id"], False)
    assert p.db.task(re_rev["id"])["spec"].count(f"review exactly head {new}") == 1


def test_the_code_prompt_says_to_name_a_change_published_in_another_repo():
    text = " ".join((RUNTIME.parent / "template" / "prompts" / "kind-code.md").read_text().split())
    assert 'hand off `"published": {"repo": "<owner/name or URL>", "branch": "...", "head": "<sha>"}`' in text
    assert "so the daemon reviews it" in text


def _interp(path, version):
    """A stand-in interpreter that prints `version` as `sys.implementation.name, sys.version` would."""
    path.write_text(f"#!/bin/sh\necho cpython {version}\n")
    path.chmod(0o755)
    return path


def _wt_venv(wt, py, packages=("foo-1.0",)):
    """A venv of its own in worktree `wt`, its python linked to `py`, with `packages` installed and the
    worktree's own package installed editable (its source path is the worktree's)."""
    venv = wt / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(py)
    (venv / "bin" / "pytest").write_text("")
    (venv / "pyvenv.cfg").write_text(f"home = {py.parent}\nprompt = {wt.name}\ncommand = python3 -m venv {venv}\n")
    site = venv / "lib" / "python3" / "site-packages"
    for name in packages:
        (site / f"{name}.dist-info").mkdir(parents=True)
        (site / f"{name}.dist-info" / "RECORD").write_text(f"{name}/__init__.py,sha256=abc,1\n")
    (site / "proj-0.1.dist-info").mkdir(parents=True)
    (site / "proj-0.1.dist-info" / "METADATA").write_text("Name: proj\nVersion: 0.1\n")
    (site / "proj-0.1.dist-info" / "RECORD").write_text(f"__editable__.proj-0.1.pth,sha256={wt.name},9\n")
    (site / "proj-0.1.dist-info" / "direct_url.json").write_text(
        json.dumps({"url": f"file://{wt}", "dir_info": {"editable": True}}))
    (site / "__editable__.proj-0.1.pth").write_text(f"{wt}/src\n")
    return site


def _two_worktrees(env, monkeypatch, py1, py2=None):
    """Two worktrees of the project's HEAD, nested below a folder with a conftest.py (so their pytest
    runs name the worktree), each with its own venv."""
    from ttp import pushpass
    p, repo, origin, other = _push_setup(env, monkeypatch, ["pytest -q"])
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    outer = env["tmp"] / "outer"
    outer.mkdir()
    (outer / "conftest.py").write_text("")
    wts = []
    for name, py in (("t1", py1), ("t2", py2 or py1)):
        _git_out(repo, "worktree", "add", "-q", "--detach", str(outer / name), "HEAD")
        _wt_venv(outer / name, py)
        wts.append(outer / name)
    return p, wts, pushpass.tree_of(repo, "HEAD"), dict(os.environ)


def test_a_push_check_pass_serves_another_worktree_of_the_tree_with_its_own_venv(env, monkeypatch):
    """A pass recorded in one worktree is reused in another of the same tree whose own venv runs the
    same interpreter with the same packages: where the worktree and its venv live is not the
    environment (the venv python and --rootdir in the command, the editable install's source)."""
    from ttp import push, pushpass
    py = _interp(env["tmp"] / "python3.12", "3.12.1")
    p, (wt1, wt2), tree, cenv = _two_worktrees(env, monkeypatch, py)
    eid = pushpass.env_id(wt1, "pytest -q", cenv, "tip")
    assert str(wt1) in " ".join(push.check_argv("pytest -q", wt1, cenv, isolate=False)), "the run names its worktree"
    assert pushpass.record(p, tree, "pytest -q", eid, "b1") == ""
    hit = pushpass.passed(p, tree, "pytest -q", pushpass.env_id(wt2, "pytest -q", cenv, "tip"))
    assert hit and hit["batch"] == "b1", "the other worktree reuses the pass"
    assert pushpass.check_envs(wt1, ["pytest -q"], cenv) == pushpass.check_envs(wt2, ["pytest -q"], cenv)


def test_a_push_check_pass_misses_another_interpreter_or_packages_and_says_which_part(env, monkeypatch, capsys):
    """Another interpreter version or binary, or another set of installed packages (one more, one
    changed in place, an editable install from elsewhere), is another environment; the miss names
    the part of it that differs, for the push queue and for `ttp push --own` reusing `ttp checks`."""
    from ttp import cli, push, pushpass
    py1, py2 = _interp(env["tmp"] / "python3.12", "3.12.1"), _interp(env["tmp"] / "python3.13", "3.13.0")
    p, (wt1, wt2), tree, cenv = _two_worktrees(env, monkeypatch, py1, py2)
    parts1 = pushpass.env_parts(wt1, "pytest -q", cenv, "tip")
    assert pushpass.record(p, tree, "pytest -q", pushpass.digest(parts1), "b1", parts1) == ""

    def miss(wt):
        parts = pushpass.env_parts(wt, "pytest -q", cenv, "tip")
        assert pushpass.passed(p, tree, "pytest -q", pushpass.digest(parts)) is None
        return pushpass.missed(p, tree, "pytest -q", parts)

    assert miss(wt2) == "the pass from b1 on this tree ran in another environment: interpreter, version differ"
    # The same version from another binary (a rebuild) still misses, on the binary alone.
    (wt2 / ".venv" / "bin" / "python").unlink()
    rebuilt = _interp(env["tmp"] / "python3.12b", "3.12.1")
    rebuilt.write_text(rebuilt.read_text() + "# rebuilt\n")
    (wt2 / ".venv" / "bin" / "python").symlink_to(rebuilt)
    assert miss(wt2).endswith(": interpreter differ")
    (wt2 / ".venv" / "bin" / "python").unlink()
    (wt2 / ".venv" / "bin" / "python").symlink_to(py1)
    assert pushpass.passed(p, tree, "pytest -q", pushpass.env_id(wt2, "pytest -q", cenv, "tip")), "same again"
    site = wt2 / ".venv" / "lib" / "python3" / "site-packages"
    (site / "bar-2.0.dist-info").mkdir()
    assert miss(wt2).endswith(": deps differ"), "one more package"
    shutil.rmtree(site / "bar-2.0.dist-info")
    (site / "foo-1.0.dist-info" / "RECORD").write_text("foo-1.0/__init__.py,sha256=other,1\n")
    assert miss(wt2).endswith(": deps differ"), "a package reinstalled with other files"
    (site / "foo-1.0.dist-info" / "RECORD").write_text("foo-1.0/__init__.py,sha256=abc,1\n")
    (site / "__editable__.proj-0.1.pth").write_text(f"{wt1}/src\n")
    assert miss(wt2).endswith(": deps differ"), "the editable install points at another worktree's code"
    (site / "__editable__.proj-0.1.pth").write_text(f"{wt2}/src\n")
    assert pushpass.passed(p, tree, "pytest -q", pushpass.env_id(wt2, "pytest -q", cenv, "tip")), "restored: a hit"
    # `ttp checks` passes: `ttp push --own` names the part too.
    cmds = ["pytest -q"]
    envs1, cparts1 = cli._check_envs(wt1, cmds, cenv)
    cli._record_pass(p, tree, cmds, "candidate", "", envs1, cparts1)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x")
    envs2, cparts2 = cli._check_envs(wt2, cmds, {**cenv, "PYTEST_ADDOPTS": "-x"})
    assert cli._recorded_pass(p, tree, cmds, "candidate", envs2) is None
    assert push._own_pass(p, tree, cmds, envs2, cparts2) == ""
    assert "on this tree ran in another environment: vars differ" in capsys.readouterr().err
    assert cli._recorded_pass(p, tree, cmds, "candidate", cli._check_envs(wt2, cmds, cenv)[0]), "same env: a hit"


def test_push_check_passes_recorded_before_env_parts_just_miss(env, monkeypatch):
    """A pass keyed the old way, or with parts that are not what this runtime writes, misses once and
    never breaks the miss line."""
    from ttp import cli, pushpass
    py = _interp(env["tmp"] / "python3.12", "3.12.1")
    p, (wt1, wt2), tree, cenv = _two_worktrees(env, monkeypatch, py)
    for odd in ({}, {"parts": "x"}, {"parts": ["a"]}, {"parts": {"argv": 1}}):
        (p.state / pushpass.STORE).write_text(json.dumps({"passes": [
            {"key": "old", "tree": tree, "cmd": "pytest -q", "batch": "b0", "ts": time.time(), **odd}]}))
        parts = pushpass.env_parts(wt2, "pytest -q", cenv, "tip")
        assert pushpass.passed(p, tree, "pytest -q", pushpass.digest(parts)) is None
        why = pushpass.missed(p, tree, "pytest -q", parts)
        kept = isinstance(odd.get("parts"), dict) and odd["parts"]
        assert why.startswith("the pass from b0 on this tree ran in another environment: ") if kept else why == "", why
    for odd in ("x", ["x"], [{"argv": 1}], None):
        (p.state / cli.CHECK_PASSES).write_text(json.dumps({"passes": [
            {"tree": tree, "commands": cli._commands_hash(["pytest -q"]), "envs": ["old"], "env_parts": odd}]}))
        envs, parts = cli._check_envs(wt2, ["pytest -q"], cenv)
        assert cli._recorded_pass(p, tree, ["pytest -q"], "candidate", envs) is None
        assert isinstance(cli._env_miss(p, tree, ["pytest -q"], parts), str)


_FORK_GH = ("#!/bin/sh\necho \"$@\" >> \"$FAKE_GH_LOG\"\n"
            "if [ -n \"$FAKE_GH_SLEEP\" ]; then exec sleep \"$FAKE_GH_SLEEP\"; fi\n"
            "if [ -n \"$FAKE_GH_ERR\" ]; then echo \"$FAKE_GH_ERR\" >&2; exit 1; fi\ncat \"$FAKE_GH_JSON\"\n")


def _fork_pr(env, monkeypatch, repo, branch="feature-x", cross=True, err="", default="main"):
    """origin moved to <tmp>/acme/widgets.git and a fork of it at <tmp>/forker/widgets.git (default
    branch `default`), with a stub gh on PATH answering that `branch`'s open PR has its head on the fork
    (`cross`) or on origin, or failing with `err`. Returns (upstream, fork, gh log)."""
    from ttp import prhead
    tmp = env["tmp"]
    up, fork = tmp / "acme" / "widgets.git", tmp / "forker" / "widgets.git"
    up.parent.mkdir()
    fork.parent.mkdir()
    _git_out(repo, "clone", "-q", "--bare", _git_out(repo, "remote", "get-url", "origin"), str(up))
    _git_out(repo, "remote", "set-url", "origin", str(up))
    _git_out(repo, "clone", "-q", "--bare", str(up), str(fork))
    _git_out(repo, "push", "-q", str(fork), f"HEAD:refs/heads/{default}")
    _git_out(fork, "symbolic-ref", "HEAD", f"refs/heads/{default}")
    bin_, log = tmp / "gh-bin", tmp / "gh.log"
    bin_.mkdir()
    (bin_ / "gh").write_text(_FORK_GH)
    (bin_ / "gh").chmod(0o755)
    (tmp / "gh.json").write_text(json.dumps({
        "headRepository": {"name": "widgets"}, "headRepositoryOwner": {"login": "forker" if cross else "acme"},
        "isCrossRepository": cross, "headRefName": branch, "state": "OPEN",
        "url": "https://example.com/acme/widgets/pull/7"}))
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_JSON", str(tmp / "gh.json"))
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_ERR", err)
    monkeypatch.delenv("TTP_RUN_DIR", raising=False)
    monkeypatch.setattr(prhead, "_memo", {})
    return up, fork, log


def _pr_fix_task(p, repo, pr, start):
    """A code task labelled pr_branch:<pr> in a worktree on its own branch, one commit past `start`."""
    tid = p.db.add_task("Fix the PR", "s", kind="code", labels=[f"pr_branch:{pr}"])
    own = f"ttp/t{tid}-fix-the-pr"
    path = repo.parent / f"wt{tid}"
    _git_out(repo, "worktree", "add", "-q", "-b", own, str(path), start)
    p.db.update_task(tid, branch=own)
    _commit(path, f"fix{tid}.txt", "fix\n")
    return tid, path


def _fork_publish(env, monkeypatch, capsys, cross=True, err="", on_origin=False):
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    up, fork, log = _fork_pr(env, monkeypatch, repo, cross=cross, err=err)
    _git_out(repo, "push", "-q", str(up if on_origin else fork), "HEAD:refs/heads/feature-x")
    _git_out(repo, "fetch", "-q", "origin")
    tid, path = _pr_fix_task(p, repo, "feature-x", "HEAD")
    monkeypatch.chdir(path)
    monkeypatch.setenv("TTP_TASK", str(tid))
    rc = _ttp("push", "--own")
    return p, repo, path, up, fork, log, rc, capsys.readouterr().err, tid


def _branch_tip(bare, branch):
    r = subprocess.run(["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def test_a_pr_branch_whose_open_pr_has_its_head_on_a_fork_is_pushed_there_and_verified(env, monkeypatch, capsys):
    """(a) The open PR's head repository is a fork: `ttp push --own` publishes the pr_branch on a remote
    for the fork (added, origin's URL unchanged), origin gets nothing, and the claimed push (the push's
    record and the hand-off's line) is confirmed on the fork, not failed on origin."""
    from ttp import push, pushclaim
    p, repo, path, up, fork, log, rc, err, tid = _fork_publish(env, monkeypatch, capsys)
    head = _git_out(path, "rev-parse", "HEAD")
    assert rc == 0, err
    assert "has its head on forker/widgets" in err and "ttp-head-forker" in err
    assert _branch_tip(fork, "feature-x") == head
    assert _branch_tip(up, "feature-x") == ""
    assert _git_out(repo, "remote", "get-url", "origin") == str(up)
    assert _git_out(repo, "remote", "get-url", "ttp-head-forker") == str(fork)
    assert "pr view feature-x --json" in log.read_text()
    assert push.resolve(p, path, own=True)[:2] == ("ttp-head-forker", "feature-x")
    assert len(log.read_text().splitlines()) == 1   # answered once for the process
    # The push record names the fork's remote; the summary line names the bare branch (origin's).
    pushes = p.state / "pushes"
    pushes.mkdir(parents=True, exist_ok=True)
    (pushes / f"t{tid}-x.json").write_text(json.dumps({
        "repo": str(path), "remote": "ttp-head-forker", "branch": "feature-x", "target": "ttp-head-forker/feature-x",
        "status": "pushed", "sha": head, "head": head}))
    cs = pushclaim.claims(p, p.db.task(tid), {"summary": f"pushed {head[:10]} to feature-x"}, None)
    cs += [{"marker": f"t{tid}-x.json", "source": "its detached push"}]
    state, why, _ = pushclaim.check_all(p, cs)
    assert state == pushclaim.CONFIRMED, why
    assert "the open PR's head repository" in why


def test_a_same_repo_pr_branch_is_pushed_to_origin_as_before(env, monkeypatch, capsys):
    """(b) The open PR's head is on origin's own repository: the push goes to origin, no remote added."""
    p, repo, path, up, fork, log, rc, err, _ = _fork_publish(env, monkeypatch, capsys, cross=False, on_origin=True)
    assert rc == 0, err
    assert _branch_tip(up, "feature-x") == _git_out(path, "rev-parse", "HEAD")
    assert "ttp-head" not in _git_out(repo, "remote")
    assert "pr view feature-x" in log.read_text()


@pytest.mark.parametrize("gh_err, said", [
    ("HTTP 502: Bad Gateway (https://example.com/graphql)", "gh could not answer (HTTP 502"),
    ("no pull requests found for branch \"feature-x\"", "no open PR has feature-x as its head"),
])
def test_a_pr_branch_gh_cannot_place_goes_to_origin_saying_why(env, monkeypatch, capsys, gh_err, said):
    """(c) gh fails or finds no open PR: origin as before, with the reason on stderr."""
    p, repo, path, up, fork, log, rc, err, _ = _fork_publish(env, monkeypatch, capsys, err=gh_err, on_origin=True)
    assert rc == 0, err
    assert said in err and "pushing to origin as before" in err
    assert _branch_tip(up, "feature-x") == _git_out(path, "rev-parse", "HEAD")
    assert _branch_tip(fork, "feature-x") == ""
    assert "ttp-head" not in _git_out(repo, "remote")


@pytest.mark.parametrize("branch", ["main", "master", "trunk"])
def test_a_fork_heads_main_master_or_default_branch_is_never_pushed(env, monkeypatch, capsys, branch):
    """(d) A fork PR whose head branch is main, master or the fork's default branch is refused."""
    from ttp import prhead
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    up, fork, _ = _fork_pr(env, monkeypatch, repo, branch=branch, default="trunk")
    with pytest.raises(ValueError, match="never pushed this way"):
        prhead.fork_remote(repo, branch, "origin")
    if branch == "trunk":   # through `ttp push --own` too
        tid, path = _pr_fix_task(p, repo, branch, "HEAD")
        monkeypatch.chdir(path)
        monkeypatch.setenv("TTP_TASK", str(tid))
        tip = _branch_tip(fork, branch)
        assert _ttp("push", "--own") == 2
        assert "default branch of ttp-head-forker" in capsys.readouterr().err
        assert _branch_tip(fork, branch) == tip


def test_a_fork_pr_branch_head_that_does_not_fast_forward_is_refused(env, monkeypatch, capsys):
    """(d) No force push: a fork pr_branch that moved past the task's base is not rewritten."""
    p, repo, origin, _ = _push_setup(env, monkeypatch, ["true"])
    up, fork, _ = _fork_pr(env, monkeypatch, repo)
    _git_out(repo, "push", "-q", str(fork), "HEAD:refs/heads/feature-x")
    tid, path = _pr_fix_task(p, repo, "feature-x", "HEAD")
    other = env["tmp"] / "fork-clone"
    _git_out(repo, "clone", "-q", "-b", "feature-x", str(fork), str(other))
    _commit(other, "theirs.txt", "theirs\n")
    _git_out(other, "push", "-q", "origin", "feature-x")
    tip = _branch_tip(fork, "feature-x")
    monkeypatch.chdir(path)
    monkeypatch.setenv("TTP_TASK", str(tid))
    assert _ttp("push", "--own") == 2
    assert "not an ancestor" in capsys.readouterr().err
    assert _branch_tip(fork, "feature-x") == tip


def test_a_hand_pushed_pr_head_on_the_fork_is_verified_there_not_failed(env, monkeypatch, tmp_path):
    """(e) Replay: the push to origin was refused, the worker pushed the PR's fork head by hand and its
    done says 'pushed <sha> to <pr_branch>' (read as origin's). The daemon reads the fork, where the
    commit is the PR's head, and records done with the proof instead of failing the task."""
    p, d, tid, path, head, _ = _claim_setup(env, monkeypatch, tmp_path,
                                            {"status": "done", "summary": "pushed {head} to feature-x"})
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", ('["pr_branch:feature-x"]', tid))
    up, fork, log = _fork_pr(env, monkeypatch, env["repo"])
    _git_out(path, "push", "-q", str(fork), "HEAD:refs/heads/feature-x")
    run = p.db.one("SELECT id FROM runs WHERE task=? ORDER BY id DESC", (tid,))["id"]
    pushes = p.state / "pushes"   # its own `ttp push` to origin, refused before the push by hand
    pushes.mkdir(parents=True, exist_ok=True)
    (pushes / f"t{tid}-refused.json").write_text(json.dumps({
        "run": run, "head": head, "remote": "origin", "branch": "feature-x", "target": "origin/feature-x",
        "status": "failed", "exit": 6}))
    _settle_probe(d, tid)
    d.verify_pushes()
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert task["status"] == "done", (task["status"], res)
    assert "the open PR's head repository" in res["summary"]
    assert "pr view feature-x" in log.read_text()


def _pq_fork(env, monkeypatch, cross=True, err="", on_fork=True):
    """A push-queue project (_pq) whose code task is labelled pr_branch:feature-x, origin moved and
    forked as in _fork_pr, and feature-x pushed only to the fork (`on_fork`). Returns (s, fork, gh log,
    the git commands prhead ran)."""
    from ttp import prhead, pushq
    s = _pq(env, monkeypatch)
    up, fork, log = _fork_pr(env, monkeypatch, s.repo, cross=cross, err=err)
    if on_fork:
        _git_out(s.path, "push", "-q", str(fork), "HEAD:refs/heads/feature-x")
    s.p.db.update_task(s.code, labels=json.dumps(["pr_branch:feature-x"]))
    pushq._absent_seen.clear()
    ran, real = [], prhead._git
    monkeypatch.setattr(prhead, "_git", lambda repo, *a, **k: ran.append(a) or real(repo, *a, **k))
    return s, fork, log, ran


def test_a_review_approved_pr_branch_on_the_open_prs_fork_head_is_found_there_not_absent(env, monkeypatch):
    """(1) The push remote lacks the reviewed task's pr_branch, but its open PR has its head on a fork
    that has it: the push queue finds it there (the remote `ttp push --own` publishes to), so the
    label is not ignored as absent and the approval is refused toward `ttp push --own`. (A1) The
    fork's default branch is never read: no `ls-remote --symref`. origin is left as it was."""
    from ttp import pushq
    s, fork, log, ran = _pq_fork(env, monkeypatch)
    origin = _git_out(s.repo, "remote", "get-url", "origin")
    assert not pushq._absent(s.p, "origin", "feature-x", True)
    out = pushq.check_approval(s.p, s.p.db.task(s.review), [{"branch": s.branch, "head": s.head}])
    assert "ttp push --own" in out.get("invalid", ""), out
    assert "feature-x is on remote ttp-head-forker" in out["invalid"], out
    assert "pr view feature-x" in log.read_text()
    assert not [a for a in ran if "--symref" in a], ran
    assert _git_out(s.repo, "remote", "get-url", "origin") == origin


@pytest.mark.parametrize("cross,err,why", [
    (False, "", "has its head on origin's repository"),
    (True, 'no pull requests found for branch "feature-x"', "no open PR has feature-x as its head"),
    (True, "HTTP 502: Bad Gateway", "gh could not answer (HTTP 502: Bad Gateway)")])
def test_a_pr_branch_whose_pr_is_same_repo_absent_or_unreadable_stays_absent_with_the_reason(env, monkeypatch,
                                                                                            cross, err, why):
    """(2) A same-repository PR, no open PR, or gh failing: only the push remote's answer counts, as
    before (the fork having the branch changes nothing), and the reason is in the approval's note."""
    from ttp import pushq
    s, fork, log, ran = _pq_fork(env, monkeypatch, cross=cross, err=err)
    assert pushq._absent(s.p, "origin", "feature-x", True)
    out = pushq.check_approval(s.p, s.p.db.task(s.review), [{"branch": s.branch, "head": s.head}])
    assert "invalid" not in out, out
    assert any("feature-x was ignored" in n and why in n for n in out["notes"]), out["notes"]
    assert log.read_text().count("pr view feature-x") == 1


def test_a_pr_branch_missing_on_both_the_push_remote_and_the_fork_is_still_absent(env, monkeypatch):
    """(3) The open PR's head is a fork, but neither it nor the push remote has the branch: absent, and
    the note says the fork was read too."""
    from ttp import pushq
    s, fork, log, ran = _pq_fork(env, monkeypatch, on_fork=False)
    assert pushq._absent(s.p, "origin", "feature-x", True)
    out = pushq.check_approval(s.p, s.p.db.task(s.review), [{"branch": s.branch, "head": s.head}])
    assert any("remote ttp-head-forker has no feature-x either" in n for n in out.get("notes", [])), out
    assert not [a for a in ran if "--symref" in a], ran


def test_the_push_queues_fork_check_asks_gh_nothing_when_prheads_cache_has_the_answer(env, monkeypatch, tmp_path):
    """(4) A second check of the same pr_branch, in this process (prhead's memo) or after it (the run
    folder's pr_head.json), reads the kept answer: gh is asked once."""
    from ttp import prhead, pushq
    s, fork, log, ran = _pq_fork(env, monkeypatch)
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    assert not pushq._absent(s.p, "origin", "feature-x", True)
    pushq._absent_seen.clear()
    assert not pushq._absent(s.p, "origin", "feature-x", True)
    pushq._absent_seen.clear()
    monkeypatch.setattr(prhead, "_memo", {})
    assert not pushq._absent(s.p, "origin", "feature-x", True)
    assert (tmp_path / prhead.CACHE).is_file()
    assert log.read_text().count("pr view") == 1, log.read_text()


@pytest.mark.parametrize("left", ["cut", "spent"])
def test_a_slow_gh_in_a_push_claim_check_is_deferred_within_the_ticks_budget(env, monkeypatch, left):
    """(A2, A3) pushclaim._on_pr_head asks gh within the tick's Budget: with a slow gh its timeout is
    cut to what is left and the claim is deferred (OutOfTime), not failed; with the Budget spent gh is
    not run at all. The fork's default branch is never read (no `ls-remote --symref`)."""
    from ttp import prhead, pushclaim
    _, repo, _, _ = _push_setup(env, monkeypatch, ["true"])
    up, fork, log = _fork_pr(env, monkeypatch, repo)
    _git_out(repo, "push", "-q", str(fork), "HEAD:refs/heads/feature-x")
    monkeypatch.setenv("FAKE_GH_SLEEP", "30")
    ran, real = [], prhead._git
    monkeypatch.setattr(prhead, "_git", lambda r, *a, **k: ran.append(a) or real(r, *a, **k))
    b = pushclaim.Budget(2 if left == "cut" else 0.5)
    b.call(repo, "ls-remote", "origin", timeout=30)   # the tick's first call (it may take all its timeout)
    began = time.monotonic()
    with pytest.raises(pushclaim.OutOfTime):
        pushclaim._on_pr_head({"pr_branch": "feature-x"}, repo, "origin", "refs/heads/feature-x",
                              _git_out(repo, "rev-parse", "HEAD"), {pushclaim.Budget.KEY: b})
    assert time.monotonic() - began < 10
    assert (log.read_text().count("pr view") if log.exists() else 0) == (1 if left == "cut" else 0)
    assert not [a for a in ran if "--symref" in a], ran
    assert not prhead._memo, "a gh timeout is no answer: a later tick asks again"


def test_a_push_claim_on_the_fork_head_is_confirmed_without_reading_its_default_branch(env, monkeypatch):
    """(A1) The daemon's claim check confirms a pr_branch commit on the PR's fork head, within its
    Budget, and never runs `ls-remote --symref` (only a push refusal needs the default branch)."""
    from ttp import prhead, pushclaim
    _, repo, _, _ = _push_setup(env, monkeypatch, ["true"])
    up, fork, log = _fork_pr(env, monkeypatch, repo)
    _git_out(repo, "push", "-q", str(fork), "HEAD:refs/heads/feature-x")
    ran, real = [], prhead._git
    monkeypatch.setattr(prhead, "_git", lambda r, *a, **k: ran.append(a) or real(r, *a, **k))
    out = pushclaim._on_pr_head({"pr_branch": "feature-x"}, repo, "origin", "refs/heads/feature-x",
                                _git_out(repo, "rev-parse", "HEAD"), {pushclaim.Budget.KEY: pushclaim.Budget(30)})
    assert out and out[0] == pushclaim.CONFIRMED, out
    assert not [a for a in ran if "--symref" in a], ran


def test_an_approval_from_the_review_of_an_urgent_task_starts_its_batch_at_once(env, monkeypatch):
    """The review is routine priority, but the task it reviews is urgent (an `urgent:` spec line): its
    approval goes first and starts a batch while another review still runs; routine ones wait."""
    from ttp import pushq
    s = _pq(env, monkeypatch, push_batch_s=900)
    s.p.db.update_task(s.review, status="running")   # a review is still at work: approvals may wait for it
    now = time.time()
    _pq_earlier(s)
    routine = _pq_review(s)[1]
    assert pushq.due(s.p, now)[0] == []
    hot = s.p.db.add_task("time-sensitive fix", "Fix it.\nurgent: the user is waiting", kind="code",
                          origin="coordinator", priority=3)
    rid = s.p.db.add_task("Review of the fix", "r", kind="review", origin="daemon", priority=3, depends_on=[hot])
    s.p.db.update_task(rid, status="pushing")
    with s.p.db.tx():
        [row] = pushq.approve(s.p, rid, None, {"entries": [{"branch": s.branch, "head": s.head}],
                                               "target": "origin/proj"})
    rows, why = pushq.due(s.p, now)
    assert [r["id"] for r in rows] == [row] and "an urgent review" in why, "alone: the routine one waits " + why
    s.p.db.update_task(hot, spec="Fix it.")   # no longer urgent: it waits like the rest
    assert pushq.due(s.p, now)[0] == []


def test_checks_whose_request_was_taken_out_of_the_queue_answer_dropped_not_queued_for_ever(env, tmp_path, monkeypatch,
                                                                                           capsys):
    from ttp import checksgone, cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    cli.main(["checks", "--detach", "--", "true"])
    capsys.readouterr()
    probe = f"ttp checks --result {run}"
    assert cli._checks_result(run) == 1 and "queued for the daemon" in capsys.readouterr().out
    assert checksgone.state(run) == "queued" and checksgone.stuck(probe) == [], "a queued request stays 'not yet'"
    for q in (p.state / detach.QUEUE).glob("*.json"):
        q.unlink()   # taken out of the queue by hand: the daemon never starts it
    assert checksgone.state(run) == "gone" and checksgone.state(run) == "gone", "a second look still sees it"
    assert detach.REMOVED in detach.lost(run / cli.CHECKS_RC).read_text()
    assert cli._checks_result(run) == 0, "the standard probe answers"
    assert f"not started: {detach.REMOVED}" in capsys.readouterr().out
    assert (run / cli.CHECKS_RC).read_text().strip() == "killed" and cli._checks_dropped(run) == detach.REMOVED
    assert checksgone.state(run) == "finished" and checksgone.stuck(probe) == [run]


def test_gone_checks_settle_with_a_dropped_exit_code_and_a_rerun_clears_it(env, tmp_path, monkeypatch, capsys):
    from ttp import checksgone, cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    # Checks that started once and ended without an exit code (killed outright): no queue, no process.
    (run / cli.CHECKS_PID).write_text(json.dumps({"pid": 2 ** 22 + 7, "started": "1"}))
    probe = f"test -f {run / cli.CHECKS_RC} && ttp checks --result {run}"
    assert checksgone.run_dirs(probe) == [run] and checksgone.gone(probe) == [run]
    checksgone.settle(run)
    assert (run / cli.CHECKS_RC).read_text().strip() == "1" and "neither queued nor running" in cli._checks_dropped(run)
    assert "dropped from the checks queue" in (run / cli.CHECKS_OUT).read_text()
    monkeypatch.setattr(push, "_boxed", lambda: False)
    assert cli._checks_result(run) == 0
    out = capsys.readouterr().out
    assert "ttp checks: dropped: they are neither queued nor running" in out and "exit code 1" in out
    monkeypatch.setattr(push, "_boxed", lambda: True)
    cli.main(["checks", "--detach", "--", "true"])
    detach.tend(p)
    assert _wait_precheck(run) == "0" and cli._checks_dropped(run) == "", "a rerun starts with no dropped record"


def test_detached_checks_that_pass_answer_without_a_dropped_line(env, tmp_path, monkeypatch, capsys):
    from ttp import checksgone, cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    cli.main(["checks", "--detach", "--", "true"])
    detach.tend(p)
    assert _wait_precheck(run) == "0"
    capsys.readouterr()
    monkeypatch.setattr(push, "_boxed", lambda: False)
    assert cli._checks_result(run) == 0
    out = capsys.readouterr().out
    assert "finished with exit code 0" in out and "dropped" not in out
    assert checksgone.state(run) == "finished" and checksgone.stuck(f"ttp checks --result {run}") == []
    assert not detach.lost(run / cli.CHECKS_RC).exists()
