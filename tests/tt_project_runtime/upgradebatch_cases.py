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
