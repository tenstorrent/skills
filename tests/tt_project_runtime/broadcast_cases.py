# Broadcast tests (`ttp say --all`, broadcast.py) of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that
# file, and pytest collects these tests under its name. Add new broadcast tests here, not there.
from __future__ import annotations


def _say_all(cli, *argv):
    with pytest.raises(SystemExit) as e:
        cli.main(["say", "--all", *argv])
    return e.value.code


def _broadcasts(p):
    return [(r["text"], r["broadcast"], r["ref"]) for r in
            p.db.q("SELECT text, broadcast, ref FROM messages WHERE direction='in' AND broadcast IS NOT NULL ORDER BY id")]


def test_say_all_reaches_every_local_project_once_with_one_broadcast_id(env, monkeypatch, capsys):
    from ttp import broadcast, cli
    a, b, c = make(env, "alpha"), _second_project(env, "beta"), _second_project(env, "gamma")
    monkeypatch.chdir(env["tmp"])
    assert _say_all(cli, "--except", "gamma,nosuch", "--context", "relayed from chat", "--",
                    "Stop device work on the shared box.") == 0
    out = capsys.readouterr()
    assert "--except nosuch: no such project" in out.err
    got = _broadcasts(a) + _broadcasts(b)
    assert [t for t, _, _ in got] == ["Stop device work on the shared box."] * 2
    bid = got[0][1]
    assert bid.startswith("b-") and {x for _, x, _ in got} == {bid} and got[0][2] == f"client:{bid}"
    assert _broadcasts(c) == [], "--except leaves a project out"
    assert f"broadcast {bid}: sent to 2 project(s), 0 already had it, 1 left out" in out.out
    ctx = a.db.one("SELECT context FROM messages WHERE broadcast=?", (bid,))["context"]
    assert ctx == "relayed from chat"
    # The same words again, with other punctuation and wrapping, within 24 h: nobody gets them twice,
    # and the project left out the first time gets them now under the same id.
    assert _say_all(cli, "stop device work\non the  shared box") == 0
    assert len(_broadcasts(a)) == len(_broadcasts(b)) == 1
    assert [x for _, x, _ in _broadcasts(c)] == [bid]
    assert f"broadcast {bid}: sent to 1 project(s), 2 already had it" in capsys.readouterr().out
    assert _say_all(cli, "Stop device work on the shared box!") == 0
    assert "sent to 0 project(s), 3 already had it" in capsys.readouterr().out
    assert set(broadcast.find(bid)["delivered"]) == {"alpha", "beta", "gamma"}
    assert stat.S_IMODE(broadcast.path().stat().st_mode) == 0o600
    # After the reuse window the words are a new broadcast.
    later = time.time() + broadcast.REUSE_S + 60
    monkeypatch.setattr(time, "time", lambda: later)
    assert _say_all(cli, "Stop device work on the shared box.") == 0
    assert len(_broadcasts(a)) == 2 and _broadcasts(a)[1][1] != bid


def test_broadcast_key_ignores_case_punctuation_wrapping_and_relayer_context():
    from ttp import broadcast, relay
    k = broadcast.key("Pause the device, please.")
    assert broadcast.key("pause the  device please") == k
    assert broadcast.key("PAUSE THE\ndevice -- please!") == k
    assert broadcast.key(f"Pause the device, please.\n\n{relay.MARK}\nmy own context") == k
    assert broadcast.key("Pause the device, now.") != k
    assert broadcast.normalize("Don't stop: g15-box!") == "don t stop g15 box"


def test_a_plain_say_is_recorded_and_say_all_skips_that_project(env, monkeypatch, capsys):
    from ttp import broadcast, cli
    a, b = make(env, "alpha"), _second_project(env, "beta")
    monkeypatch.chdir(env["tmp"])
    cli.main(["say", "alpha", "--", "Freeze merges until Friday."])
    mid = a.db.one("SELECT MAX(id) AS m FROM messages WHERE direction='in'")["m"]
    assert a.db.one("SELECT broadcast FROM messages WHERE id=?", (mid,))["broadcast"] is None, \
        "a message to one project is no broadcast"
    capsys.readouterr()
    assert _say_all(cli, "freeze merges until friday") == 0
    out = capsys.readouterr().out
    assert f"alpha: already has it (#{mid})" in out and "sent to 1 project(s), 1 already had it" in out
    assert _broadcasts(a) == [] and len(_broadcasts(b)) == 1
    assert set(broadcast.find(_broadcasts(b)[0][1])["delivered"]) == {"alpha", "beta"}


def test_say_all_queues_for_an_unreachable_remote_project_with_its_broadcast_id(env, monkeypatch, capsys):
    from ttp import broadcast, cli, outbox
    from ttp.project import register
    p = make(env)
    register("demo", {"host": "far", "dir": str(env["repo"])})   # this machine is the laptop now
    monkeypatch.chdir(env["tmp"])
    codes, calls = [255], []
    _flaky_ssh(monkeypatch, env["repo"], codes, calls)
    assert _say_all(cli, "Hold all device jobs.") == 0
    queued = outbox.entries("demo")
    assert len(queued) == 1
    bid = queued[0]["id"]
    assert bid.startswith("b-") and queued[0]["argv"][-4:] == ["--broadcast", bid, "--", "Hold all device jobs."]
    assert broadcast.find(bid)["delivered"] == {"demo": "queued:far"}
    assert "demo: queued (queued:far)" in capsys.readouterr().out
    # A resend while queued sends nothing more; once reachable the queued one arrives once, with its id.
    assert _say_all(cli, "hold all device jobs") == 0
    assert len(outbox.entries("demo")) == 1
    capsys.readouterr()
    assert _say(cli, "demo", "next") == 0
    assert [(t, x) for t, x, _ in _broadcasts(p)] == [("Hold all device jobs.", bid)]


def test_an_older_remote_runtime_gets_the_broadcast_id_in_its_context(env, monkeypatch):
    from ttp import cli
    sent = []

    def ssh(entry, argv, capture=False):
        sent.append(argv)
        bad = next((f for f in ("--client-id", "--broadcast") if any(a.startswith(f) for a in argv)), None)
        if bad:
            return subprocess.CompletedProcess(argv, 2, "", f"ttp: error: unrecognized arguments: {bad}")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(cli, "_ssh", ssh)
    r = cli._send_say({"host": "far", "dir": "/x"}, ["say", "demo", "--broadcast", "b-1", "--", "hi"], "b-1")
    assert r.returncode == 0
    note = "broadcast b-1: the user sent these words to every project. Do not relay them to other projects."
    assert sent[-1] == ["say", "demo", "--context", note, "--", "hi"], "the id is never dropped"
    sent.clear()
    cli._send_say({"host": "far", "dir": "/x"}, ["say", "demo", "--context", "mine", "--broadcast", "b-1", "--", "hi"],
                  "b-1")
    assert sent[-1] == ["say", "demo", "--context", f"mine\n{note}", "--", "hi"]


def test_the_receiving_side_stores_and_labels_a_broadcast(env, monkeypatch, capsys, tmp_path):
    from ttp import cli
    from ttp import coordinator as coord
    p = make(env)
    monkeypatch.chdir(env["tmp"])
    # What `ttp say --all` on another machine runs here: the id is stored, a resend is a no-op.
    for _ in range(2):
        cli.main(["say", "--client-id", "b-abc", "--broadcast", "b-abc", "demo", "--", "Stop all device work."])
    assert "already received" in capsys.readouterr().out
    rows = _broadcasts(p)
    assert rows == [("Stop all device work.", "b-abc", "client:b-abc")]
    mid = p.db.one("SELECT id FROM messages WHERE broadcast='b-abc'")["id"]
    plain = p.db.post("in", "hello", chat="cli", provenance="cli-legacy")
    digest = coord.digest(p, {}, [], [mid, plain])
    line = next(ln for ln in digest.splitlines() if f"#{mid} " in ln)
    assert ("[broadcast b-abc: the user sent these words to every project (1 delivered). "
            "Do not relay them to other projects.] Stop all device work.") in line
    assert digest.count("broadcast b-") == 1
    # A database from before the column gets it.
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, direction TEXT "
                "NOT NULL, chat TEXT, channel TEXT NOT NULL DEFAULT 'chat', kind TEXT NOT NULL DEFAULT 'user', "
                "severity TEXT NOT NULL DEFAULT 'normal', text TEXT NOT NULL, ref TEXT, handled INTEGER NOT NULL "
                "DEFAULT 0, provenance TEXT, ext_id TEXT, context TEXT)")
    con.commit()
    con.close()
    from ttp.db import DB
    assert "broadcast" in {r["name"] for r in DB(old).q("PRAGMA table_info(messages)")}


def test_a_run_cannot_say_all(env, monkeypatch, capsys):
    from ttp import broadcast, cli
    make(env)
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    assert _say_all(cli, "stop everything") == 2
    assert "refused inside a run" in capsys.readouterr().err
    assert not broadcast.path().exists()
    monkeypatch.delenv("TTP_RUN_DIR")
    assert _say_all(cli, "demo", "two positionals") == 2
    assert _say(cli, "demo") == 2
    assert _say(cli, "demo", "--except", "x", "--", "hi") == 2
    assert _say_all(cli, "--chat", "c1", "hi") == 2


def test_a_sandboxed_command_with_the_run_environment_unset_cannot_say(env, monkeypatch, capsys):
    """In a PID namespace of its own the walk up the ancestors stops at the namespace's init, so the
    run's agent cannot be seen: `ttp say` is refused there, and where the namespace cannot be read."""
    from ttp import cli, locks, sandbox
    p = make(env)
    for var in ("TTP_RUN_ID", "TTP_RUN_DIR", "TTP_PIDNS"):
        monkeypatch.delenv(var, raising=False)
    before = p.db.one("SELECT COALESCE(MAX(id),0) n FROM messages")["n"]
    monkeypatch.setattr(sandbox, "inside", lambda: True)
    assert _say(cli, "demo", "approve it", "--chat", "c1") == 2
    assert "refused inside a run" in capsys.readouterr().err
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    monkeypatch.setenv("TTP_PIDNS", "pid:[1]")   # the run's namespace, not this one
    assert _say(cli, "demo", "approve it", "--chat", "c1") == 2
    monkeypatch.delenv("TTP_PIDNS")
    real_readlink = os.readlink
    monkeypatch.setattr(locks.os, "readlink", lambda path: (_ for _ in ()).throw(PermissionError(path))
                        if str(path).startswith("/proc/self") else real_readlink(path))
    assert _say(cli, "demo", "approve it", "--chat", "c1") == 2
    assert not p.db.q("SELECT id FROM messages WHERE id>?", (before,))
    monkeypatch.setattr(locks.os, "readlink", real_readlink)
    monkeypatch.setenv("TTP_PIDNS", real_readlink("/proc/self/ns/pid"))   # the run's own namespace
    cli.main(["say", "demo", "the user's own", "--chat", "c1"])
    assert [r["text"] for r in p.db.q("SELECT text FROM messages WHERE id>? AND direction='in'", (before,))] == [
        "the user's own"]


def test_a_full_ledger_keeps_the_open_line_a_claim_writes_and_compacts_only_what_can_go(env, monkeypatch):
    from ttp import broadcast, project
    now = time.time()
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)   # fill() appends outside the lock that creates it

    def fill(age, n):
        broadcast._append(*({"event": "open", "id": f"b-old{age}-{i}", "key": f"k{i}", "norm": "x " * 40,
                             "ts": now - age} for i in range(n)))

    fill(3600, 2000)    # an hour old: inside the reuse window, nothing can go
    assert broadcast.path().stat().st_size >= broadcast.COMPACT_BYTES
    writes = []
    real = project.durable_write
    monkeypatch.setattr(project, "durable_write", lambda *a, **k: (writes.append(a[0]), real(*a, **k)))
    b = broadcast.claim("Hold all merges.", now)
    broadcast.record(b["id"], "alpha", "7", now)
    again = broadcast.claim("hold all merges", now + 10)
    assert again["id"] == b["id"] and again["delivered"] == {"alpha": "7"}, "the open line survived"
    assert writes == [], "no rewrite while every line is recent"
    # Older lines go: first those past KEEP_S, else (all within it) those past the reuse window.
    fill(broadcast.KEEP_S + 60, 10)
    c = broadcast.claim("Resume merges.", now + 20)
    ids = {e["id"] for e in broadcast._read()}
    assert len(writes) == 1 and not any(i.startswith(f"b-old{broadcast.KEEP_S + 60}-") for i in ids)
    assert {b["id"], c["id"], "b-old3600-0"} <= ids
    fill(broadcast.REUSE_S + 60, 10)
    d = broadcast.claim("Pause merges.", now + 30)
    ids = {e["id"] for e in broadcast._read()}
    assert len(writes) == 2 and not any(i.startswith(f"b-old{broadcast.REUSE_S + 60}-") for i in ids)
    assert {b["id"], c["id"], d["id"]} <= ids
    assert broadcast.claim("hold all merges!", now + 40)["delivered"] == {"alpha": "7"}
    assert stat.S_IMODE(broadcast.path().stat().st_mode) == 0o600


def test_a_run_that_unset_its_environment_still_cannot_say(env, monkeypatch, capsys):
    """A run's agent among the command's ancestors refuses `ttp say`, whatever the environment says;
    a pid reused since (another start token) or a run that ended does not."""
    from ttp import cli
    from ttp.runner import proc_start
    p = make(env)
    run_dir = env["tmp"] / "run-agent"
    run_dir.mkdir()
    rid = p.db.x("INSERT INTO runs(role,status,started,dir) VALUES('worker','running',?,?)", (time.time(), str(run_dir)))
    for var in ("TTP_RUN_ID", "TTP_RUN_DIR"):
        monkeypatch.delenv(var, raising=False)
    before = p.db.one("SELECT COALESCE(MAX(id),0) n FROM messages")["n"]
    agent = os.getppid()   # the test's parent stands in for the run's agent
    (run_dir / "child.pid").write_text(f"{agent}\n{proc_start(agent)}\n")
    assert _say(cli, "demo", "approve it", "--chat", "c1") == 2
    assert "refused inside a run" in capsys.readouterr().err
    assert not p.db.q("SELECT id FROM messages WHERE id>?", (before,))
    (run_dir / "child.pid").write_text(f"{agent}\nnot-its-start\n")
    cli.main(["say", "demo", "reused pid", "--chat", "c1"])
    (run_dir / "child.pid").write_text(f"{agent}\n{proc_start(agent)}\n")
    p.db.x("UPDATE runs SET status='ok' WHERE id=?", (rid,))
    cli.main(["say", "demo", "the user's own", "--chat", "c1"])
    assert [r["text"] for r in p.db.q("SELECT text FROM messages WHERE id>? AND direction='in' ORDER BY id", (before,))] == [
        "reused pid", "the user's own"]


def test_a_run_whose_agent_is_its_namespaces_init_still_cannot_say(env, monkeypatch, capsys):
    """The ancestry walk reaches pid 1 too: a run's agent may be its PID namespace's init (a
    container's first process), as the test's parent is when one pytest process runs right under
    `ttp sandbox`."""
    from ttp import cli, locks
    p = make(env)
    run_dir = env["tmp"] / "run-agent"
    run_dir.mkdir()
    p.db.x("INSERT INTO runs(role,status,started,dir) VALUES('worker','running',?,?)", (time.time(), str(run_dir)))
    for var in ("TTP_RUN_ID", "TTP_RUN_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(locks.os, "getpid", lambda: 7)
    monkeypatch.setattr(locks, "_parent", {7: 1, 1: 0}.get)
    monkeypatch.setattr("ttp.runner.proc_start", lambda pid: "init-start" if pid == 1 else None)
    assert locks._ancestors() == {7, 1}
    (run_dir / "child.pid").write_text("1\ninit-start\n")
    before = p.db.one("SELECT COALESCE(MAX(id),0) n FROM messages")["n"]
    assert _say(cli, "demo", "approve it", "--chat", "c1") == 2
    assert "refused inside a run" in capsys.readouterr().err
    assert not p.db.q("SELECT id FROM messages WHERE id>?", (before,))
