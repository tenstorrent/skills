# Symlink-append tests (project.open_append, project.durable_append and the daemon's state logs) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases
# there): helpers, fixtures and imports come from that file. Workers can write the state and run
# folders; the daemon appends there outside the sandbox, so a planted symlink is never followed.
from __future__ import annotations


def test_durable_append_never_follows_a_symlinked_state_log_and_the_daemon_records_it(env, tmp_path):
    from ttp import project as proj
    p = make(env)
    target = tmp_path / "victim.txt"
    target.write_text("keep me\n")
    log_file = p.state / "host-run.jsonl"
    log_file.unlink(missing_ok=True)
    log_file.symlink_to(target)
    proj.refused_appends()
    line = json.dumps({"argv": ["echo", "planted"], "rc": 126, "why": "refused"}) + "\n"
    assert proj.durable_append(log_file, line) is False
    assert target.read_text() == "keep me\n" and log_file.is_symlink()
    dangling = p.state / "other.jsonl"
    dangling.symlink_to(tmp_path / "not-yet")
    assert proj.durable_append(dangling, line) is False
    assert not (tmp_path / "not-yet").exists(), "a dangling symlink's target is never created"
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    d.tend_pushes()   # the step that records them: the daemon keeps going
    rows = p.db.q("SELECT * FROM events WHERE kind='append_refused' ORDER BY id")
    assert [json.loads(r["data"])["path"] for r in rows] == [str(log_file), str(dangling)]
    assert all(r["status"] == "handled" for r in rows)
    assert json.loads(rows[0]["data"])["text"] == line, "the refused text is kept, not lost"
    assert "refused to append to" in (p.logs / "daemon.log").read_text()
    assert proj.refused_appends() == []
    d.record_refused_appends()
    assert len(p.db.q("SELECT id FROM events WHERE kind='append_refused'")) == 2, "recorded once"


def test_durable_append_refuses_a_fifo_without_hanging(tmp_path):
    from ttp import project as proj
    for mode in ("ab", "ab+"):
        fifo = tmp_path / f"log-{len(mode)}"
        os.mkfifo(fifo)
        with pytest.raises(proj.AppendRefused):
            proj.open_append(fifo, mode)
    assert proj.durable_append(tmp_path / "log-2", "x\n") is False
    proj.refused_appends()


def test_durable_append_writes_and_syncs_a_normal_file_and_a_symlinked_folder(tmp_path, monkeypatch):
    from ttp import project as proj
    synced = []
    real = os.fsync
    monkeypatch.setattr(proj.os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (tmp_path / "state").symlink_to(real_dir)   # a folder symlink above the file is the user's own setup
    f = tmp_path / "state" / "host-run.jsonl"
    assert proj.durable_append(f, "one\n") is True
    assert len(synced) == 2, "the file and, as it is new, its folder"
    assert proj.durable_append(f, "two\n") is True
    assert len(synced) == 3
    assert (real_dir / "host-run.jsonl").read_text() == "one\ntwo\n"
    with proj.open_append(f) as fh:
        fh.write("three\n")
    assert f.read_text() == "one\ntwo\nthree\n"


def test_daemon_logs_never_follow_a_symlink(env, tmp_path):
    from ttp import churnwatch, daemon as dm, schedlog
    p = make(env)
    target = tmp_path / "victim.txt"
    target.write_text("keep me\n")
    p.logs.mkdir(parents=True, exist_ok=True)
    for name in ("daemon.log", churnwatch.FILE, "sched.jsonl"):
        (p.logs / name).unlink(missing_ok=True)
        (p.logs / name).symlink_to(target)
    dm.log(p, "worker text")
    p._git_log("worker text")
    warned = []
    assert churnwatch.record(p.logs / churnwatch.FILE, {"kind": "x"}, time.time(), {}, warned.append) is False
    line = {"picked": [1], "free": 1}
    assert schedlog.record(p.logs / "sched.jsonl", line, {}, warned.append) is False
    assert len(warned) == 2
    (p.state / dm.STACKS_FILE).unlink(missing_ok=True)
    (p.state / dm.STACKS_FILE).symlink_to(target)
    with pytest.raises(OSError):
        dm.Daemon(p.base)._stacks_file()
    assert target.read_text() == "keep me\n"


def _plant(path, target):
    """A worker's planted symlink at `path`, aimed at `target` (kept as "keep me")."""
    target.write_text("keep me\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    path.symlink_to(target)


def _fake_popen(monkeypatch, module, started):
    """`module`'s subprocess.Popen records its stdout instead of starting anything."""
    def popen(argv, **kw):
        started.append(kw.get("stdout"))
        return types.SimpleNamespace(pid=os.getpid(), stdin=io.BytesIO(), wait=lambda *a, **k: 0)
    monkeypatch.setattr(module.subprocess, "Popen", popen)


def test_open_append_or_null_and_append_text_refuse_a_symlink_and_record_it_in_the_project(env, tmp_path):
    from ttp import project as proj
    p = make(env)
    target = tmp_path / "victim.txt"
    _plant(p.state / "x.log", target)
    proj.refused_appends()
    assert proj.append_text(p.state / "x.log", "one\n", p) is False
    with proj.open_append_or_null(p.state / "x.log", "ab", "two\n") as f:
        f.write(b"two\n")
    assert target.read_text() == "keep me\n"
    rows = p.db.q("SELECT data, status FROM events WHERE kind='append_refused'")
    assert [json.loads(r["data"])["text"] for r in rows] == ["one\n"] and rows[0]["status"] == "handled"
    assert [r["text"] for r in proj.refused_appends()] == ["two\n"], "kept for the daemon without a project"
    assert proj.append_text(tmp_path / "plain.log", "ok\n") is True and (tmp_path / "plain.log").read_text() == "ok\n"


def test_ttp_checks_log_and_its_handover_line_never_follow_a_symlink(env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    target = tmp_path / "victim.txt"
    _plant(run / "checks.log", target)
    try:
        cli.main(["checks", "--", "true"])
    except SystemExit as e:
        assert e.code in (0, None), capsys.readouterr()
    assert target.read_text() == "keep me\n" and (run / "checks.log").is_symlink()
    assert p.db.q("SELECT id FROM events WHERE kind='append_refused'"), "recorded in the project at once"
    # The handover line (`ttp checks` in a tool call handing over to detached checks)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 0.2)
    monkeypatch.setattr(cli, "_checks_detach", lambda *a, **k: None)
    (repo / "b.txt").write_text("b\n")   # a new tree: no recorded pass to reuse
    subprocess.run([*git, "add", "b.txt"], check=True)
    subprocess.run([*git, "commit", "-qm", "b"], check=True)
    holder = _cap_holder("other project task #3 (run 9)")
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--", "true"])
    finally:
        holder.kill()
        holder.wait()
    assert e.value.code == 75 and cli.CHECKS_HANDOVER in capsys.readouterr().out
    assert target.read_text() == "keep me\n"


def test_checks_drop_notice_never_follows_a_symlink(tmp_path):
    from ttp import cli, project as proj
    run = tmp_path / "run"
    run.mkdir()
    target = tmp_path / "victim.txt"
    _plant(run / "checks.log", target)
    _plant(run / cli.CHECKS_OUT, target)
    proj.refused_appends()
    cli._checks_drop(run, "removed")
    assert target.read_text() == "keep me\n"
    assert (run / cli.CHECKS_RC).read_text().strip() == "1", "the drop is still recorded"
    assert len(proj.refused_appends()) == 2


def test_detached_checks_output_never_follows_a_symlink(env, tmp_path, monkeypatch):
    from ttp import cli, project as proj
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    target = tmp_path / "victim.txt"
    _plant(run / cli.CHECKS_OUT, target)
    proj.refused_appends()
    started = []
    real = subprocess.Popen

    def popen(argv, **kw):
        if "--rc" not in argv:
            return real(argv, **kw)
        started.append(kw.get("stdout"))
        return real(["true"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    try:
        cli.main(["checks", "--detach", "--", "true"])
    except SystemExit:
        pass
    assert started and started[0].name == os.devnull, "the detached checks' output goes nowhere, not through it"
    assert target.read_text() == "keep me\n"
    assert any(r["path"] == str(run / cli.CHECKS_OUT) for r in proj.refused_appends())


def test_checks_child_stop_line_never_follows_a_symlink(env, tmp_path, monkeypatch):
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    target = tmp_path / "victim.txt"
    _plant(run / "checks.log", target)
    monkeypatch.setitem(cli._CHECK_STOP, "stopped", None)
    handlers = {sig: signal.getsignal(sig) for sig in cli._CHECK_STOP_SIGNALS}
    monkeypatch.setattr(cli, "cmd_checks", lambda _a: cli._checks_stop(signal.SIGTERM, None))
    monkeypatch.setattr(cli.sys, "stdin", io.TextIOWrapper(io.BytesIO(b"go\n")))
    monkeypatch.setenv("TTP_CHECK_OUTPUT_DIR", str(run))
    monkeypatch.delenv("TTP_CHECKS_TMP_FD", raising=False)
    try:
        code = cli._checks_child(types.SimpleNamespace(rc=str(run / cli.CHECKS_RC)))
    finally:
        for sig, h in handlers.items():
            signal.signal(sig, h)
    assert code == 143 and (run / cli.CHECKS_RC).read_text().strip() == "143"
    assert target.read_text() == "keep me\n"


def test_upgrade_unfinished_merge_line_never_follows_a_symlinked_upgrade_log(env, tmp_path, monkeypatch, capsys):
    p = make(env)
    from ttp import cli, service
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    h = p.harness
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    _git_out(h, "checkout", "-qb", "side")
    (h / "prompts" / "worker.md").write_text("side change\n")
    _git_out(h, *ident, "commit", "-qam", "side")
    _git_out(h, "checkout", "-q", "-")
    _git_out(h, *ident, "merge", "-q", "--no-commit", "--no-ff", "side")
    _install_template(env)
    target = tmp_path / "victim.txt"
    _plant(p.logs / "upgrade.log", target)
    with pytest.raises(SystemExit) as e:
        cli.main(["upgrade", "demo"])
    err = capsys.readouterr().err
    assert e.value.code == 1 and "unfinished merge" in err and "refused to append to" in err
    assert target.read_text() == "keep me\n"
    rows = p.db.q("SELECT data FROM events WHERE kind='append_refused'")
    assert rows and "unfinished merge" in json.loads(rows[0]["data"])["text"]


def test_automatic_upgrade_launch_never_follows_a_symlinked_upgrade_log(env, tmp_path, monkeypatch):
    from ttp import project as proj, release
    p = make(env)
    target = tmp_path / "victim.txt"
    _plant(p.logs / "upgrade.log", target)
    started = []
    _fake_popen(monkeypatch, release, started)
    proj.refused_appends()
    release.launch(p)
    assert started and started[0].name == os.devnull, "the upgrade still starts"
    assert target.read_text() == "keep me\n"
    assert "automatic upgrade" in proj.refused_appends()[0]["text"]


def test_restart_request_never_follows_a_symlinked_restart_log(env, tmp_path, monkeypatch):
    from ttp import project as proj, service
    p = make(env)
    target = tmp_path / "victim.txt"
    _plant(p.logs / "restart.log", target)
    started = []
    _fake_popen(monkeypatch, service, started)
    (p.state / "restart.request").write_text(json.dumps({"at": time.time(), "pid": 1, "why": "upgrade"}))
    proj.refused_appends()
    try:
        assert service.take_restart_request(p, time.time() - 3600), "the restart still starts"
    finally:
        service._helpers.pop(str(p.base), None)
    assert started and started[0].name == os.devnull
    assert target.read_text() == "keep me\n"
    assert "restart requested" in proj.refused_appends()[0]["text"]


def test_takeover_log_lines_never_follow_a_symlink(env, tmp_path, monkeypatch):
    from ttp import deploy, project as proj, release
    p = make(env)
    target = tmp_path / "victim.txt"
    path = deploy.takeover_log(p, p.name)
    _plant(path, target)
    proj.refused_appends()
    deploy._log(p, p.name, "takeover outcome")
    started = []
    _fake_popen(monkeypatch, deploy, started)
    (tmp_path / "rel" / "bin").mkdir(parents=True)
    (tmp_path / "rel" / "bin" / "ttp").write_text("")
    monkeypatch.setattr(release, "installed", lambda: tmp_path / "rel")
    deploy.launch_takeover(p, p.name, 7)
    assert started and started[0].name == os.devnull, "the takeover still starts"
    assert target.read_text() == "keep me\n"
    texts = [r["text"] for r in proj.refused_appends()]
    assert "takeover outcome" in texts[0] and "host-side takeover" in texts[1]


def test_push_log_lines_and_push_output_never_follow_a_symlink(env, tmp_path, monkeypatch):
    from ttp import checkdirs, locks, project as proj, push
    p = make(env)
    target = tmp_path / "victim.txt"
    log = p.state / "pushes" / "1.log"
    _plant(log, target)
    proj.refused_appends()
    push._log_line(log, "a push line")   # no fsync on the null device: it carries on
    marker = p.state / "pushes" / "1.json"
    m = {"id": "1", "log": str(log), "repo": str(tmp_path)}
    marker.write_text(json.dumps(m))
    for name, fn in {"restore": lambda *a: tmp_path, "take": lambda out: open(tmp_path / "guard", "a+"),
                     "environment": lambda out, env: dict(env), "reports": lambda out: out,
                     "finish": lambda *a, **k: None, "cleanup": lambda *a, **k: None}.items():
        monkeypatch.setattr(checkdirs, name, fn)
    started = []
    _fake_popen(monkeypatch, push, started)
    monkeypatch.setattr(locks, "label_pid", lambda *a, **k: None)
    monkeypatch.setattr(push, "renice", lambda *a, **k: None)
    with open(tmp_path / "run.lock", "a+") as lock:
        assert push._start(p, marker, m, lock, dict(os.environ)) == os.getpid(), "the push still starts"
    assert started and started[0].name == os.devnull
    assert target.read_text() == "keep me\n"
    texts = [r["text"] for r in proj.refused_appends()]
    assert "a push line" in texts[0] and "check scratch:" in texts[1] and "starting the push process" in texts[2], texts


def test_durable_write_never_follows_or_removes_a_symlink_planted_at_its_temp_name(tmp_path, monkeypatch):
    from ttp import project as proj
    target = tmp_path / "victim.txt"
    target.write_text("keep me\n")
    path = tmp_path / "state.json"
    path.write_text("old\n")
    path.chmod(0o640)
    names = iter(["planted", "planted", "fresh"])
    monkeypatch.setattr(proj, "_tmp_suffix", lambda: next(names))
    planted = tmp_path / f".state.json.{os.getpid()}.planted.tmp"
    planted.symlink_to(target)
    proj.durable_write(path, "new\n")
    assert target.read_text() == "keep me\n", "the symlink's target is never written"
    assert planted.is_symlink(), "a file it did not create is never removed"
    assert path.read_text() == "new\n" and not path.is_symlink()
    assert path.stat().st_mode & 0o777 == 0o640
    assert not (tmp_path / f".state.json.{os.getpid()}.fresh.tmp").exists()
    monkeypatch.setattr(proj, "_tmp_suffix", lambda: "planted")
    with pytest.raises(FileExistsError):
        proj.durable_write(path, "never\n")
    assert path.read_text() == "new\n" and target.read_text() == "keep me\n" and planted.is_symlink()


def test_concurrent_refused_appends_are_all_recorded(tmp_path):
    import threading
    from ttp import project as proj
    link = tmp_path / "log.jsonl"
    link.symlink_to(tmp_path / "victim")
    proj.refused_appends()
    seen, n_threads, each = [], 8, 20
    start = threading.Barrier(n_threads + 1)

    def refuse(i):
        start.wait()
        for j in range(each):
            assert proj.durable_append(link, f"{i}-{j}\n") is False

    def drain():
        start.wait()
        for _ in range(200):
            seen.extend(proj.refused_appends())

    threads = [threading.Thread(target=refuse, args=(i,)) for i in range(n_threads)]
    threads.append(threading.Thread(target=drain))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seen.extend(proj.refused_appends())
    texts = [r["text"] for r in seen]
    assert sorted(texts) == sorted(f"{i}-{j}\n" for i in range(n_threads) for j in range(each))
    assert not (tmp_path / "victim").exists()
