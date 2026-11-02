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
def test_releases_inside_the_window_share_one_task_even_after_the_last_one_landed(env, monkeypatch, argv):
    """A project whose local edits conflict on every release ran one model task per release: a task that
    landed its release lifted the once-a-day limit. Now one task per upgrade.task_every_h (6 h) whatever
    the last one did; releases meanwhile only record the wait, and the next task targets the newest."""
    from ttp import cli, release
    p, restarts = _conflicting_upgrade(env, monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "demo"])
    first = p.db.one("SELECT id FROM tasks WHERE kind='harness'")["id"]
    rt = p.harness / "runtime"      # it landed: done, and the harness runs the release it targeted
    landed = f"Target release: tt-project {cli._runtime_version(rt)} ({cli.recorded_commit(rt)})"
    p.db.x("UPDATE tasks SET status='done', spec=? WHERE id=?", (f"merge it\n\n{landed}\n", first))
    if argv:
        argv += ["--project-dir", str(p.base)]
    for _ in range(3):      # three releases inside the window
        _next_release(env)
        with pytest.raises(SystemExit) as e:
            cli.main(["upgrade", "demo", *argv])
        assert e.value.code == 75 and _harness_tasks(p) == [first]
        rec = p.db.kv(release.KV_AUTO)
        assert rec["outcome"] == "deferred" and rec["until"] <= time.time() + 6 * 3600 + 5
    newest = cli._runtime_version(env["home"] / "lib" / "current" / "runtime")
    p.db.x("UPDATE tasks SET created=created-? WHERE id=?", (2 * 3600, first))
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "demo", *argv])
    assert _harness_tasks(p) == [first], "6 h not up yet"
    p.set_config("upgrade.task_every_h", 1)
    with pytest.raises(SystemExit) as e:
        cli.main(["upgrade", "demo", *argv])
    tids = _harness_tasks(p)
    assert e.value.code == 75 and len(tids) == 2 and release.finish_holder(p)["task"] == tids[1]
    assert f"Target release: tt-project {newest} (" in p.db.task(tids[1])["spec"] and not restarts


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


def test_task_every_h_reads_the_setting_with_a_6h_default():
    from ttp import release
    assert release.task_every_s({}) == 6 * 3600
    assert release.task_every_s({"upgrade": {"task_every_h": 2}}) == 2 * 3600
    assert release.task_every_s({"upgrade": {"task_every_h": "x"}}) == 6 * 3600


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
