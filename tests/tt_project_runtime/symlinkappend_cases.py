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
