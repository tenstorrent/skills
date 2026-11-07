# Template upgrade batching tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file. Each test uses
# a temporary project and a temporary installed release (make, _install_template), never a real harness.
from __future__ import annotations

_UP_IMPORTS = "from . import ackwait, startgate, userpause\n"


def _release_with_new_import(env):
    """The next release adds a name to upstream's one-line import list in progress.py."""
    _install_template(env)
    f = env["home"] / "lib" / "current" / "runtime" / "ttp" / "progress.py"
    text = f.read_text()
    assert text.count(_UP_IMPORTS) == 1
    f.write_text(text.replace(_UP_IMPORTS, "from . import ackwait, startgate, timefmt, userpause\n"))


def _harness_tasks(p):
    return [t["id"] for t in p.db.q("SELECT id FROM tasks WHERE kind='harness' ORDER BY id")]


@pytest.mark.parametrize("where", ["file end", "upstream's import list"])
def test_project_imports_at_the_file_end_merge_model_free_and_inside_upstreams_list_take_one_task(
        env, monkeypatch, capsys, where):
    """A project's own import next to upstream's `from . import ...` list conflicted on every release, one
    model task each. In a block at the end of the file the merge is clean: the upgrade commits, restarts the
    daemon and logs one feed line, with no model task and no alert. Inside upstream's list it takes one."""
    from ttp import cli, release, service
    p = make(env)
    restarts = []
    monkeypatch.setattr(service, "restart", lambda p: restarts.append(1) or "restarted")
    f = p.harness / "runtime" / "ttp" / "progress.py"
    if where == "file end":
        f.write_text(f.read_text() + "\n\n# project-local imports\nfrom . import release as _local_release  # noqa\n")
    else:
        f.write_text(f.read_text().replace(_UP_IMPORTS, "from . import ackwait, release, startgate, userpause\n"))
    _git_out(p.harness, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "local import")
    _release_with_new_import(env)
    capsys.readouterr()
    argv = ["upgrade", "demo", "--auto", "--project-dir", str(p.base)]
    if where == "file end":
        cli.main(argv)
        text = f.read_text()
        assert "timefmt, userpause" in text and text.rstrip().endswith("_local_release  # noqa")
        assert restarts == [1] and _harness_tasks(p) == [], "a clean merge starts no model task"
        assert p.db.kv(release.KV_AUTO)["outcome"] == "applied"
        out = p.db.q("SELECT kind, severity, text FROM messages WHERE direction='out'")
        assert [(m["kind"], m["severity"]) for m in out] == [("info", "low")], out
        assert "with no model task" in out[0]["text"] and "compiles and imports" in out[0]["text"]
        assert not p.db.q("SELECT id FROM alerts")
        assert not _git_out(p.harness, "status", "--porcelain")
        return
    for _ in range(2):      # the second try finds the task open: still one
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 75
    assert len(_harness_tasks(p)) == 1 and not restarts
    assert "from . import ackwait, release, startgate, userpause" in f.read_text(), "the live harness is untouched"


def test_a_clean_merge_that_fails_the_runtime_check_takes_one_task_and_leaves_the_harness_clean(
        env, monkeypatch, capsys):
    """No conflict, but the merged runtime no longer imports (upstream renamed a name the project's own
    import block uses): one model task, no restart, and the live harness exactly as it was."""
    from ttp import cli, service
    p = make(env)
    monkeypatch.setattr(service, "restart", lambda p: pytest.fail("a failing merge restarted the daemon"))
    d = p.harness / "runtime" / "ttp" / "daemon.py"
    d.write_text(d.read_text() + "\n\n# project-local imports\nfrom .release import CHECK_S as _LOCAL_CHECK_S  # noqa\n")
    _git_out(p.harness, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "local import")
    head = _git_out(p.harness, "rev-parse", "HEAD").strip()
    _install_template(env)
    lib = env["home"] / "lib" / "current" / "runtime" / "ttp"
    for name, old, new in (("release.py", "\nCHECK_S = 3600", "\nCHECK_EVERY_S = 3600"),
                           ("daemon.py", "release.CHECK_S", "release.CHECK_EVERY_S")):
        text = (lib / name).read_text()
        assert old in text
        (lib / name).write_text(text.replace(old, new))
    with pytest.raises(SystemExit) as e:
        cli.main(["upgrade", "demo", "--auto", "--project-dir", str(p.base)])
    assert e.value.code == 75
    tids = _harness_tasks(p)
    assert len(tids) == 1 and "fails `-c import ttp.daemon, ttp.cli`" in p.db.task(tids[0])["spec"]
    assert _git_out(p.harness, "rev-parse", "HEAD").strip() == head
    assert not _git_out(p.harness, "status", "--porcelain")
    assert not (p.harness / ".git" / "MERGE_HEAD").exists()
    assert len(_git_out(p.harness, "worktree", "list").splitlines()) == 1


@pytest.mark.parametrize("argv", [["--auto"], []])
def test_each_newer_release_queues_its_merge_task_at_once_after_the_last_one_ended(env, monkeypatch, argv):
    """No window of hours between merge tasks: a merge task that ended (landed or not) a minute ago does
    not hold off the next release's merge, which is queued at once and targets the newest release."""
    from ttp import cli, release
    p, restarts = _conflicting_upgrade(env, monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "demo"])
    first = p.db.one("SELECT id FROM tasks WHERE kind='harness'")["id"]
    rt = p.harness / "runtime"      # it landed: done, and the harness runs the release it targeted
    landed = f"Target release: tt-project {cli._runtime_version(rt)} ({cli.recorded_commit(rt)})"
    p.db.x("UPDATE tasks SET status='done', spec=?, updated=? WHERE id=?",
           (f"merge it\n\n{landed}\n", time.time() - 60, first))
    if argv:
        argv += ["--project-dir", str(p.base)]
    tids = [first]
    for _ in range(2):
        _next_release(env)
        with pytest.raises(SystemExit) as e:
            cli.main(["upgrade", "demo", *argv])
        assert e.value.code == 75 and len(_harness_tasks(p)) == len(tids) + 1
        tids = _harness_tasks(p)
        newest = cli._runtime_version(env["home"] / "lib" / "current" / "runtime")
        assert release.finish_holder(p)["task"] == tids[-1]
        assert f"Target release: tt-project {newest} (" in p.db.task(tids[-1])["spec"]
        p.db.update_task(tids[-1], status="failed")
    assert not restarts


def test_an_open_upgrade_task_absorbs_a_newer_release(env, monkeypatch, capsys):
    from ttp import cli, release
    p, restarts = _conflicting_upgrade(env, monkeypatch)
    argv = ["upgrade", "demo", "--auto", "--project-dir", str(p.base)]
    with pytest.raises(SystemExit):
        cli.main(argv)
    tids = _harness_tasks(p)
    _next_release(env)
    newest = cli._runtime_version(env["home"] / "lib" / "current" / "runtime")
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 75 and _harness_tasks(p) == tids and not restarts
    assert release.finish_holder(p)["target"].startswith(newest + " ")
    assert f"Target release: tt-project {newest} (" in p.db.task(tids[0])["spec"]


_PUSH_LAST = "from . import batch, cli, pushpass, verdictcarry  # noqa: E402\n"
_PUSH_LOCAL = "\n# this project's own imports\nfrom . import release as _local_release  # noqa: E402\n"


def _push_py_upgrade(env, monkeypatch, marked):
    """A harness whose push.py ends with upstream's import list, followed by the marker line (`marked`)
    or, as releases before it shipped, by nothing; the project then adds its own import block at the
    file end; the next release changes upstream's last import line (and, unmarked before, adds the
    marker). Returns the project, cli and the restarts list."""
    from ttp import cli, service
    p = make(env)
    restarts = []
    monkeypatch.setattr(service, "restart", lambda p: restarts.append(1) or "restarted")
    lib = env["home"] / "lib" / "current" / "runtime" / "ttp" / "push.py"
    _install_template(env)
    shipped = lib.read_text()
    assert shipped.endswith(_PUSH_LAST + "\n" + cli.LOCAL_IMPORTS + "\n")
    if not marked:
        lib.write_text(shipped[:-len("\n" + cli.LOCAL_IMPORTS + "\n")])
        cli.main(["upgrade", "demo"])
    f = p.harness / "runtime" / "ttp" / "push.py"
    assert f.read_text() == lib.read_text()
    f.write_text(f.read_text() + _PUSH_LOCAL)
    _git_out(p.harness, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "local import")
    _install_template(env)
    lib.write_text(shipped.replace(_PUSH_LAST, _PUSH_LAST.replace("verdictcarry", "verdictcarry, timefmt")))
    _next_release(env)
    restarts.clear()
    return p, cli, restarts


@pytest.mark.parametrize("marked", [True, False])
def test_a_project_import_block_at_the_end_survives_upstream_changing_its_last_import_line(
        env, monkeypatch, capsys, marked):
    """Upstream's push.py ends with its `from . import ...` list; a project's block of its own imports at
    the file end conflicted with every release that changed that list. After the marker line the merge
    is clean; a block still right after the list (before the marker shipped) is moved after the marker
    by the upgrade itself. Either way both import lists stay, with no model task."""
    p, cli, restarts = _push_py_upgrade(env, monkeypatch, marked)
    capsys.readouterr()
    cli.main(["upgrade", "demo", "--auto"])
    out = capsys.readouterr().out
    text = (p.harness / "runtime" / "ttp" / "push.py").read_text()
    new = _PUSH_LAST.replace("verdictcarry", "verdictcarry, timefmt")
    assert text.endswith(new + "\n" + cli.LOCAL_IMPORTS + "\n" + (_PUSH_LOCAL if marked else _PUSH_LOCAL.lstrip("\n"))), text[-400:]
    assert "<<<<<<<" not in text and text.count("from . import release as _local_release") == 1
    assert ("kept this project's own imports" in out) is not marked, out
    assert restarts == [1] and _harness_tasks(p) == [] and not _git_out(p.harness, "status", "--porcelain")


@pytest.mark.parametrize("tail, settles", [
    ("\nfrom . import release  # noqa\nimport os\n", True),
    ("\n# comment only\n", False),
    ("\nfrom . import release  # noqa\nX = 1\n", False),
    ("\nif True:\n    from . import release\n", False),
    ("\nfrom . import (\n    release,\n)\n", True),
])
def test_keep_local_imports_takes_only_a_tail_of_imports(tail, settles):
    """The upgrade moves a project's trailing block only when it holds nothing but imports and comments,
    and never when the project also changed a line upstream changed (that needs judgment)."""
    from ttp import cli
    base = "x = 1\nfrom . import a, b  # noqa\n"
    theirs = "x = 1\nfrom . import a, b, c  # noqa\n\n" + cli.LOCAL_IMPORTS + "\n"
    got = cli._keep_local_imports(base + tail, base, theirs)
    assert (got is not None) is settles
    if settles:
        assert got == theirs + tail.lstrip("\n")
    assert cli._keep_local_imports(base.replace("x = 1", "x = 2") + tail, base, theirs.replace("x = 1", "x = 3")) is None
    assert cli._keep_local_imports(base.replace("a, b", "a, b, d") + tail, base, theirs) is None


def test_every_runtime_module_ending_in_an_import_ends_with_the_project_imports_marker():
    """A project's own import block goes after the marker line, one unchanged line away from upstream's
    import list, so a release that changes the list never conflicts with it."""
    import ast
    from ttp import cli
    ending = []
    for f in sorted((RUNTIME / "ttp").rglob("*.py")):
        text = f.read_text()
        tree = ast.parse(text)
        if tree.body and isinstance(tree.body[-1], (ast.Import, ast.ImportFrom)):
            ending.append(f.name)
            lines = text.splitlines()
            assert lines[-1] == cli.LOCAL_IMPORTS and not lines[-2].strip(), f
    assert "push.py" in ending


def _linked_releases(env):
    """lib/current a link to release a; release b beside it, the same but for one worker.md line."""
    import shutil
    _install_template(env)
    lib = env["home"] / "lib"
    a, b = lib / "rel-a", lib / "rel-b"
    (lib / "current").rename(a)
    shutil.copytree(a, b)
    w = b / "template" / "prompts" / "worker.md"
    w.write_text(w.read_text() + "\nRELEASE B ONLY\n")
    (lib / "current").symlink_to(a)
    return lib, a, b


def test_an_upgrade_snapshot_reads_the_release_resolved_once_while_setup_re_points_lib_current(env, monkeypatch):
    """A `ttp setup` re-points lib/current while the upgrade copies the release onto `upstream`: every
    file still comes from the release the link named when the upgrade began."""
    from ttp import cli, release, service
    p = make(env)
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    lib, a, b = _linked_releases(env)
    real, moved = cli.replace_copy, []

    def copy(src, dst, **kw):
        if not moved:
            moved.append(1)
            release.point_current(b)
        return real(src, dst, **kw)
    monkeypatch.setattr(cli, "replace_copy", copy)
    cli.main(["upgrade", "demo", "--auto", "--project-dir", str(p.base)])
    assert moved and os.readlink(lib / "current") == str(b)
    assert "RELEASE B ONLY" not in _git_out(p.harness, "show", "upstream:prompts/worker.md")
    assert p.db.kv(release.KV_AUTO)["outcome"] == "applied"


@pytest.mark.parametrize("change", ["commit", "version", "removed"])
def test_an_upgrade_snapshot_whose_release_changes_under_the_copy_commits_nothing_and_fails(env, monkeypatch, change):
    """The resolved release is re-copied with another commit or version (or removed) after its runtime was
    copied: the snapshot is refused before any commit, upstream stays where it was and --auto records the failure."""
    import re as _re
    import shutil
    from ttp import cli, release, service
    p = make(env)
    monkeypatch.setattr(service, "restart", lambda p: pytest.fail("a refused snapshot restarted the daemon"))
    lib, a, b = _linked_releases(env)
    before = _git_out(p.harness, "rev-parse", "upstream").strip()
    head = _git_out(p.harness, "rev-parse", "HEAD").strip()
    real, done = cli.replace_copy, []

    def copy(src, dst, **kw):
        out = real(src, dst, **kw)
        if not done and "template" in Path(src).relative_to(a).parts:     # the runtime part is copied
            done.append(1)
            ttp = a / "runtime" / "ttp"
            if change == "commit":
                (ttp / cli.SOURCE_FILE).write_text("another-commit\n")
            elif change == "version":
                init = ttp / "__init__.py"
                init.write_text(_re.sub(r'__version__ = "[^"]+"', '__version__ = "99.0.0"', init.read_text()))
            else:
                shutil.rmtree(a / "runtime")
        return out
    monkeypatch.setattr(cli, "replace_copy", copy)
    with pytest.raises(cli.ReleaseMoved):
        cli.main(["upgrade", "demo", "--auto", "--project-dir", str(p.base)])
    assert done
    assert _git_out(p.harness, "rev-parse", "upstream").strip() == before, "nothing went onto upstream"
    assert _git_out(p.harness, "rev-parse", "HEAD").strip() == head
    rec = p.db.kv(release.KV_AUTO)
    assert rec["outcome"] == "failed" and "ReleaseMoved" in rec["why"], rec


def test_a_merge_that_ends_without_landing_raises_one_event_naming_the_project_and_files(env, monkeypatch):
    """The deploying project's coordinator hears once, per merge task, that a project's template merge
    failed or ended unlanded, with the conflicting files, so it takes it over at once."""
    import sqlite3
    from ttp import cli, deploy, overview, release
    p, first = _deferred_conflict(env, monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "demo"])
    target = cli._runtime_version(env["home"] / "lib" / "current" / "runtime")
    conn = sqlite3.connect(f"file:{p.db.path}?mode=ro", uri=True)
    db = overview._RO(conn)
    m = overview._merge_unlanded(db, target, {})
    assert m == {"task": first, "status": "done", "files": "conflicting files: prompts/kind-harness.md"}, m
    assert overview._merge_unlanded(db, "0.0.1", {}) is None        # another release's task
    rec, row = {"name": "demo", "target": target}, {"merge_unlanded": m}
    for _ in range(2):
        deploy._merge_event(p, rec, row, time.time())
    ev = p.db.q("SELECT text FROM events WHERE source='deploy' AND kind='observation'")
    assert len(ev) == 1 and "demo's template merge to tt-project" in ev[0]["text"], ev
    assert f"#{first} done" in ev[0]["text"] and "prompts/kind-harness.md" in ev[0]["text"]
    p.db.x("UPDATE tasks SET status='cancelled', blocked_reason=? WHERE id=?",
           (f"{release.FENCED_PREFIX} to x", first))
    assert overview._merge_unlanded(db, target, {}) is None          # the harness superseded it: not unlanded
    conn.close()


def test_an_unlanded_merge_is_retried_by_the_daemon_once_the_projects_own_files_change(env, monkeypatch):
    """A merge task that ended without landing left KV_AUTO at 'conflict' for the release, so the daemon's
    hold_reason said 'already tried' for good and an edit to the project's own files queued nothing. Now it
    holds only while those files match the ones the task was queued on; a commit to them lets the daemon's
    `ttp upgrade --auto` run again, which queues the next merge task."""
    from ttp import cli, release
    p, restarts = _conflicting_upgrade(env, monkeypatch)
    d = release.drift(p)
    argv = ["upgrade", "demo", "--auto", "--project-dir", str(p.base)]
    p.db.set_kv(release.KV_AUTO, {"key": d["key"], "outcome": "running", "ts": time.time()})   # release.start
    with pytest.raises(SystemExit):
        cli.main(argv)
    first = p.db.one("SELECT id FROM tasks WHERE kind='harness'")["id"]
    assert p.db.kv(release.KV_AUTO)["outcome"] == "conflict"
    assert release.hold_reason(p, d).startswith(f"harness task #{first} finishes"), "one merge task at a time"
    p.db.update_task(first, status="done")          # ended without landing: the harness still runs the old one
    assert release.hold_reason(p, d) == "already tried for this release", "same files: the same conflict"
    k = p.harness / "prompts" / "kind-harness.md"
    k.write_text(k.read_text() + "One more local rule.\n")
    _git_out(p.harness, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "local edit")
    assert release.hold_reason(p, d) == "", "an edit to the project's own files merges again"
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    tids = _harness_tasks(p)
    assert e.value.code == 75 and len(tids) == 2 and release.finish_holder(p)["task"] == tids[1], tids
    p.db.update_task(tids[1], status="failed")
    assert release.hold_reason(p, d) == "already tried for this release" and not restarts


@pytest.mark.parametrize("argv", [["--auto"], []])
def test_a_merge_task_the_project_cancelled_does_not_hold_off_the_next_one(env, monkeypatch, argv):
    """A project's coordinator cancelled its template-merge task, and that task still counted as the
    window's one task for 6 h: the project stayed behind. A task the project cancelled is left out of the
    window. No window holds a merge off any more: a cancelled task merged nothing, so it never does."""
    from ttp import cli, release
    p, restarts = _conflicting_upgrade(env, monkeypatch)
    if argv:
        argv = argv + ["--project-dir", str(p.base)]
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "demo", *argv])
    first = _harness_tasks(p)[0]
    p.db.update_task(first, status="cancelled", blocked_reason="cancelled by the coordinator: spending order")
    _next_release(env)
    with pytest.raises(SystemExit) as e:
        cli.main(["upgrade", "demo", *argv])
    tids = _harness_tasks(p)
    assert e.value.code == 75 and len(tids) == 2 and release.finish_holder(p)["task"] == tids[1]
    assert not restarts


def _cancelled_resolution(p, text):
    """The project's merge task merged `upstream` in its own detached worktree, resolved the conflict
    with `text`, committed, and was cancelled before its `--apply`. Returns the commit."""
    tid = _harness_tasks(p)[-1]
    tmp = p.root / "merge-tmp"
    _git_out(p.harness, "worktree", "add", "-q", "--detach", str(tmp), "HEAD")
    subprocess.run(["git", "-C", str(tmp), "merge", "upstream"], capture_output=True)
    (tmp / "prompts" / "kind-harness.md").write_text(text)
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    _git_out(tmp, "add", "-A")
    _git_out(tmp, *ident, "commit", "-qm", "merge upstream")
    p.db.update_task(tid, status="cancelled", blocked_reason="cancelled by the coordinator")
    return _git_out(tmp, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("case", ["clean", "conflict"])
def test_a_host_upgrade_finishes_a_cancelled_tasks_committed_merge_model_free(env, monkeypatch, case):
    """A cancelled merge task left its resolution committed in its worktree, and the project stayed behind
    until someone merged it by hand. A host-side `ttp upgrade` now merges that commit with the current
    template and main, checks it compiles and imports, and applies it with no task. A resolution that
    conflicts with the newer template still queues a task, and main is left as it was."""
    from ttp import cli
    p, restarts = _conflicting_upgrade(env, monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "demo"])
    resolved = "# Harness task, both ways\n"
    sha = _cancelled_resolution(p, resolved)
    (p.harness / "charter-note.md").write_text("main moved meanwhile\n")    # main must be merged too
    _git_out(p.harness, "add", "-A")
    _git_out(p.harness, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "charter note")
    head = _git_out(p.harness, "rev-parse", "HEAD").strip()
    if case == "conflict":      # the newer template changes the same lines again
        _install_template(env, {"kind-harness.md": "# Harness task, upstream's newer way\n"})
    _next_release(env)
    newest = cli._runtime_version(env["home"] / "lib" / "current" / "runtime")
    if case == "clean":
        cli.main(["upgrade", "demo"])
        h = p.harness
        assert restarts == [1] and len(_harness_tasks(p)) == 1, "applied with no new task"
        assert (h / "prompts" / "kind-harness.md").read_text() == resolved
        assert cli._runtime_version(h / "runtime") == newest and (h / "charter-note.md").exists()
        for c in (sha, head):
            assert subprocess.run(["git", "-C", str(h), "merge-base", "--is-ancestor", c, "HEAD"]).returncode == 0
        assert not _git_out(h, "status", "--porcelain")
        return
    with pytest.raises(SystemExit) as e:
        cli.main(["upgrade", "demo"])
    assert e.value.code == 75 and len(_harness_tasks(p)) == 2 and not restarts
    assert _git_out(p.harness, "rev-parse", "HEAD").strip() == head
    assert not (p.harness / ".git" / "MERGE_HEAD").exists()
