# Corrupt kv row tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new corrupt kv tests here, not there.
from __future__ import annotations


@pytest.mark.parametrize("key", ["integrity", "obligations", "progress_state", "gates"])
def test_a_corrupt_kv_row_is_quarantined_and_the_daemon_turn_and_status_still_run(env, capsys, key):
    from ttp import cli
    from ttp.daemon import Daemon
    p = make(env)
    raw = '{"a": '
    p.db.x("INSERT INTO kv(key,value,ts) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
           (key, raw, time.time()))
    d = Daemon(p.base)
    d.check_integrity(start=True)   # what the daemon runs at each start
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    p.db.post("in", "hello there", chat="c1")
    p.set_config("coordinator.debounce_s", 0)
    deadline = time.time() + 60
    while time.time() < deadline:
        d.cfg = p.config()
        d.tick()
        if p.db.q("SELECT id FROM messages WHERE direction='out' AND chat='c1'"):
            break
        time.sleep(0.05)
    replies = [r for r in p.db.unread_for_chat("c1", 0) if r["chat"] == "c1"]
    assert len(replies) == 1 and "hello there" in replies[0]["text"]
    kept = p.db.q("SELECT key, value FROM kv WHERE key LIKE ?", (f"corrupt:{key}:%",))
    assert [r["value"] for r in kept] == [raw], "the raw value is kept under a quarantine key"
    alerts = p.db.q("SELECT severity FROM messages WHERE ref=?", (f"kv_corrupt:{key}",))
    assert [a["severity"] for a in alerts] == ["low"]
    capsys.readouterr()
    try:
        cli.main(["status", p.name])
    except SystemExit as e:
        assert e.code in (None, 0)


def test_a_corrupt_kv_row_reads_as_the_default_once_and_alerts_once(env):
    p = make(env)
    for raw in ('{"a": ', "not json"):
        p.db.x("INSERT INTO kv(key,value,ts) VALUES('demo_key',?,?) ON CONFLICT(key) DO UPDATE SET "
               "value=excluded.value", (raw, time.time()))
        assert p.db.kv("demo_key", {"d": 1}) == {"d": 1}
        assert p.db.kv("demo_key") is None, "the bad row is moved aside, not read again"
        assert p.db.kv_dict("demo_key") == ({}, None)
    kept = sorted(r["value"] for r in p.db.q("SELECT value FROM kv WHERE key LIKE 'corrupt:demo_key:%'"))
    assert kept == sorted(['{"a": ', "not json"])
    assert len(p.db.q("SELECT id FROM messages WHERE ref='kv_corrupt:demo_key'")) == 1, "one alert per key"
    line = [ln for ln in (p.logs / "daemon.log").read_text().splitlines() if "demo_key" in ln]
    assert len(line) == 2 and "corrupt" in line[0]
    # Inside a transaction that rolls back, the bad row stays and the next read moves it aside again.
    p.db.x("INSERT INTO kv(key,value,ts) VALUES('demo_key','[1,',?) ON CONFLICT(key) DO UPDATE SET "
           "value=excluded.value", (time.time(),))
    with pytest.raises(RuntimeError):
        with p.db.tx():
            assert p.db.kv("demo_key", 5) == 5
            raise RuntimeError("roll back")
    assert p.db.one("SELECT value FROM kv WHERE key='demo_key'")["value"] == "[1,"
    assert p.db.kv("demo_key", 5) == 5 and p.db.kv("demo_key") is None


def test_a_corrupt_kv_row_on_a_read_only_database_reads_as_the_default(env):
    p = make(env)
    p.db.x("INSERT INTO kv(key,value,ts) VALUES('demo_key','{',?)", (time.time(),))
    from ttp.db import DB
    ro = DB(p.db.path)
    ro.conn.execute("PRAGMA query_only=ON")
    assert ro.kv("demo_key", 7) == 7
    assert p.db.one("SELECT value FROM kv WHERE key='demo_key'")["value"] == "{"
