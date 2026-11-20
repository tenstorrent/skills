# Reading another project's database (globalcap.connect_ro): a stopped project's folder is left as it
# was, and its committed rows are still read. Run from tests/test_tt_project_runtime.py (see
# _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations

_FR_CHILD = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
for v in sys.argv[2:]:
    conn.execute("INSERT INTO t VALUES (?)", (v,))
conn.commit()
os._exit(0)   # dies without a checkpoint: committed rows stay only in the -wal
"""


def _fr_unclean(folder, *rows):
    """A scratch WAL database in `folder` whose writer died with `rows` committed only in its -wal."""
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "scratch.db"
    subprocess.run([sys.executable, "-c", _FR_CHILD, str(db), *rows], check=True)
    assert (folder / "scratch.db-wal").stat().st_size, "the rows sit in the -wal"
    return db


def _fr_stamps(folder):
    return {f.name: (f.stat().st_size, f.stat().st_mtime_ns) for f in sorted(folder.iterdir())}


def _fr_rows(conn):
    try:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY rowid")]
    finally:
        conn.close()


def test_stopped_project_read_reads_wal_commits_and_changes_no_file(env, tmp_path, monkeypatch):
    """A plain read-only open of a stopped project's WAL database rewrote its -shm (and made one when
    missing); an immutable open missed the rows committed only in its -wal. A stopped project with a
    non-empty -wal is read from a private snapshot: its rows show and its folder stays as it was."""
    from ttp import globalcap, project
    monkeypatch.setattr(project, "HOME_DIR", tmp_path / "home")
    folder = tmp_path / "other" / "state"
    db = _fr_unclean(folder, "a", "b")
    before = _fr_stamps(folder)
    time.sleep(0.01)
    assert _fr_rows(globalcap.connect_ro(db)) == ["a", "b"]
    assert _fr_rows(globalcap.connect_ro(db)) == ["a", "b"]
    assert _fr_stamps(folder) == before
    with pytest.raises(sqlite3.OperationalError):   # why the snapshot: immutable alone misses them
        _fr_rows(sqlite3.connect(f"{db.as_uri()}?immutable=1", uri=True))
    snaps = sorted((tmp_path / "home" / "ro-snapshots").glob("*.db"))
    assert len(snaps) == 1, "an unchanged source reuses its snapshot"
    # the -shm gone too: none is made
    (folder / "scratch.db-shm").unlink()
    before = _fr_stamps(folder)
    assert _fr_rows(globalcap.connect_ro(db)) == ["a", "b"]
    assert _fr_stamps(folder) == before
    # a new commit: a fresh snapshot replaces the old one
    subprocess.run([sys.executable, "-c", _FR_CHILD, str(db), "c"], check=True)
    assert _fr_rows(globalcap.connect_ro(db)) == ["a", "b", "c"]
    assert [s.name for s in (tmp_path / "home" / "ro-snapshots").glob("*.db")] != [snaps[0].name]
    assert len(list((tmp_path / "home" / "ro-snapshots").glob("*.db"))) == 1


def test_cleanly_stopped_project_read_makes_no_side_file(tmp_path, monkeypatch):
    from ttp import globalcap, project
    monkeypatch.setattr(project, "HOME_DIR", tmp_path / "home")
    folder = tmp_path / "other" / "state"
    db = _fr_unclean(folder, "a")
    c = sqlite3.connect(db)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()   # the last connection closing removes the -wal and -shm
    assert sorted(f.name for f in folder.iterdir()) == ["scratch.db"]
    before = _fr_stamps(folder)
    assert _fr_rows(globalcap.connect_ro(db)) == ["a"]
    assert _fr_stamps(folder) == before
    assert not (tmp_path / "home" / "ro-snapshots").exists(), "nothing to fold in: no copy"


def test_running_project_read_sees_its_newest_commits(tmp_path, monkeypatch):
    from ttp import globalcap, project
    monkeypatch.setattr(project, "HOME_DIR", tmp_path / "home")
    folder = tmp_path / "other" / "state"
    folder.mkdir(parents=True)
    (folder / "daemon.pid").write_text(f"{os.getpid()}\n")
    assert globalcap.daemon_running(folder)
    db = folder / "scratch.db"
    w = sqlite3.connect(db)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("CREATE TABLE t (v TEXT)")
    w.execute("INSERT INTO t VALUES ('a')")
    w.commit()
    r = globalcap.connect_ro(db)
    assert [x[0] for x in r.execute("SELECT v FROM t")] == ["a"]
    w.execute("INSERT INTO t VALUES ('b')")
    w.commit()
    assert [x[0] for x in r.execute("SELECT v FROM t ORDER BY rowid")] == ["a", "b"]
    r.close()
    w.close()
    assert not (tmp_path / "home" / "ro-snapshots").exists()
    (folder / "daemon.pid").write_text("999999999\n")
    assert not globalcap.daemon_running(folder), "a pid file naming no process is a stopped daemon"


def test_cross_project_readers_leave_a_stopped_projects_folder_as_it_was(env, tmp_path, monkeypatch):
    """Each module that reads another project's database goes through connect_ro: on a stopped
    project none makes or changes a file in its state folder."""
    from ttp import hours, overseer, peerhold, project
    monkeypatch.setattr(project, "HOME_DIR", tmp_path / "snaphome")
    make(env)
    q = _second_project(env, "frother")
    q.db.post("in", "hello", chat=None, kind="user", provenance="web-session")
    q.db.close()
    q._db = None
    path = q.state / "project.db"
    for ext in ("-wal", "-shm"):
        assert not (q.state / f"project.db{ext}").exists()
    (q.state / "daemon.pid").unlink(missing_ok=True)
    before = _fr_stamps(q.state)
    time.sleep(0.01)
    hours.user_times(path, 0)
    hours.last_user_time(path)
    peerhold.state(q)
    overseer._ro_db(path).close()
    assert _fr_stamps(q.state) == before


def test_only_connect_ro_opens_another_projects_database():
    """One rule for reading another project: no module opens a database `mode=ro` itself, apart from
    connect_ro and userwords.open_ro (a project's own database)."""
    found = []
    for f in sorted((RUNTIME / "ttp").glob("*.py")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if "mode=ro" in line and "connect(" in line:
                found.append(f"{f.name}:{n}")
    assert [x.split(":")[0] for x in found] == ["globalcap.py", "userwords.py"], found
