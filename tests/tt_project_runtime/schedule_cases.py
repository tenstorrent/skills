# Schedules area: a schedule that starts whole periods late (the host or daemon down, a tick held up) is
# reported with its missed runs and likely cause, never silently resumed. Exec'd into
# test_tt_project_runtime.py: make and its imports are in scope.


def test_a_schedule_counts_whole_periods_missed_but_not_short_delays_sleep_or_a_first_run(env):
    from ttp import schedule as sched
    now = time.time()
    row = lambda late, every=1800, last=now - 86400: {"next_run": now - late, "every_s": every, "last_run": last}
    assert sched.missed(row(60), now) == 0                       # on time
    assert sched.missed(row(3 * 3600), now) == 6                 # 3 h late every 30 min
    assert sched.missed(row(50 * 60, every=300), now) == 0       # under GAP_MIN_S, though 10 periods
    assert sched.missed(row(3 * 3600, last=None), now) == 0      # never ran: nothing it recorded has a hole
    assert sched.missed(row(3 * 3600), now, [[now - 4 * 3600, now - 60]]) == 0   # the host slept through it
    assert sched.missed(row(3 * 3600), now, [[now - 3 * 3600, now - 2 * 3600]]) == 4


def _late_schedule(p, late_s, every="30m"):
    from ttp import schedule as sched
    sched.upsert(p.db, "watch", "command", every, payload={"command": "true", "timeout_s": 30})
    now = time.time()
    p.db.x("UPDATE schedules SET last_run=?, next_run=? WHERE name='watch'", (now - late_s - 1800, now - late_s))


def _gap_events(p):
    return p.db.q("SELECT severity, status, text FROM events WHERE source='daemon:schedule-gap'")


def test_an_abrupt_reboot_that_stopped_a_schedule_wakes_the_coordinator_once_with_the_cause(env):
    """The case behind this check: the host went down abruptly for ~40 h, the daemon came back at boot and
    the schedule simply resumed, so nothing but its own output's hole showed the missed runs."""
    from ttp.daemon import Daemon
    p = make(env)
    _late_schedule(p, 40 * 3600)
    d = Daemon(p.base)
    d.gates = {}
    now = time.time()
    d.boot_at, d._started = now - 120, now - 60
    p.db.set_kv("boot_prev", {"boot": d.boot, "prev_boot": "earlier-boot", "held": [], "shutdown": "abrupt",
                              "last_heartbeat": now - 40 * 3600 - 600})
    d.run_schedules()
    ev = _gap_events(p)
    assert len(ev) == 1 and ev[0]["status"] == "queued" and ev[0]["severity"] in ("normal", "high")
    assert "watch 80 (every 30m" in ev[0]["text"] and "host went down (abrupt" in ev[0]["text"]
    assert p.db.one("SELECT last_run FROM schedules WHERE name='watch'")["last_run"] >= now - 5   # it ran
    d.run_schedules()
    assert len(_gap_events(p)) == 1   # on time again: told once


def test_a_schedule_held_up_while_the_daemon_ran_wakes_the_coordinator(env):
    from ttp.daemon import Daemon
    p = make(env)
    _late_schedule(p, 3 * 3600)
    d = Daemon(p.base)
    d.gates = {}
    d._started = time.time() - 10 * 3600
    d.run_schedules()
    ev = _gap_events(p)
    assert len(ev) == 1 and ev[0]["status"] == "queued" and "daemon was running" in ev[0]["text"]


def test_a_clean_daemon_stop_is_a_record_that_wakes_nobody(env):
    from ttp.daemon import Daemon
    p = make(env)
    _late_schedule(p, 3 * 3600)
    d = Daemon(p.base)
    d.gates = {}
    now = time.time()
    d._started, d._last_stop = now - 60, {"boot": d.boot, "ts": now - 3 * 3600 - 300}
    d.run_schedules()
    ev = _gap_events(p)
    assert len(ev) == 1 and ev[0]["status"] == "record" and "daemon was stopped" in ev[0]["text"]


def test_a_schedule_missed_while_the_host_slept_is_no_gap(env):
    from ttp.daemon import Daemon
    p = make(env)
    _late_schedule(p, 3 * 3600)
    now = time.time()
    p.db.set_kv("host_sleeps", [[now - 4 * 3600, now - 30]])
    d = Daemon(p.base)
    d.gates = {}
    d._started = now - 10 * 3600
    d.run_schedules()
    assert not _gap_events(p)


def test_overlapping_sleep_and_pause_windows_are_taken_off_once(env):
    from ttp import schedule as sched
    now = time.time()
    row = {"next_run": now - 5 * 3600, "every_s": 1800, "last_run": now - 86400}
    both = [[now - 3 * 3600, now - 3600], [now - 2 * 3600, now - 1800]]   # 2.5 h off, not 3 h
    assert sched.missed(row, now, both) == 5


def test_a_project_pause_while_the_daemon_ran_is_no_gap(env):
    """Nothing runs while the project is paused (Daemon.tick returns before run_schedules), by design:
    after the resume every schedule is hours late, which is no held-up tick."""
    from ttp import projpause
    from ttp.daemon import Daemon
    p = make(env)
    _late_schedule(p, 3 * 3600)
    now = time.time()
    projpause.pause(p.db, "user", "maintenance", now=now - 3 * 3600 - 60)   # as db.set_paused does
    p.db.set_kv("paused", True)
    projpause.resume(p.db, "user", now=now - 30)
    p.db.set_kv("paused", False)
    d = Daemon(p.base)
    d.gates = {}
    d._started = now - 10 * 3600
    d.run_schedules()
    assert not _gap_events(p)
    assert p.db.one("SELECT last_run FROM schedules WHERE name='watch'")["last_run"] >= now - 5   # it ran


def test_a_schedule_disabled_then_re_enabled_is_no_gap(env):
    """Re-enabling (the web app, sched.set_enabled; or the schedules file, upsert) starts from the next
    slot from now, so the hours it was off are no missed runs."""
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    p = make(env)
    for how in ("web", "file"):
        _late_schedule(p, 3 * 3600)
        p.db.x("UPDATE schedules SET enabled=0 WHERE name='watch'")
        now = time.time()
        if how == "web":
            sched.set_enabled(p.db, "watch", True)
        else:
            sched.upsert(p.db, "watch", "command", "30m", payload={"command": "true", "timeout_s": 30})
        row = p.db.one("SELECT enabled, next_run FROM schedules WHERE name='watch'")
        assert row["enabled"] == 1 and now < row["next_run"] <= time.time() + 1800, how
        d = Daemon(p.base)
        d.gates = {}
        d._started = now - 10 * 3600
        d.run_schedules()
        assert not _gap_events(p), how
    sched.set_enabled(p.db, "watch", False)   # turning one off keeps its next run
    assert p.db.one("SELECT enabled FROM schedules WHERE name='watch'")["enabled"] == 0


def _gap_issue(p):
    return p.db.one("SELECT * FROM issues WHERE source='daemon:schedule-gap'")


def test_issues_close_ends_a_stale_schedule_gap_with_its_reason_and_a_new_sighting_reopens_it(env, capsys,
                                                                                               monkeypatch):
    """A schedule-gap issue that nothing closes by itself (say after a host move) is closed with
    `ttp issues close`, not a DB write: the reason and time go in its history and the activity feed,
    and the same condition seen again reopens it and wakes, so a close never hides a live one."""
    from ttp import alerts, cli
    from ttp.daemon import Daemon
    for k in ("TTP_RUN_DIR", "TTP_TASK", "TTP_PROJECT"):
        monkeypatch.delenv(k, raising=False)
    p = make(env)
    d = Daemon(p.base)
    text = "Schedules missed runs since 10:00: watch 6. Likely cause: the daemon was not running."
    d.observe("daemon:schedule-gap", text, "normal", whole=True, key="schedule-gap:1700000000")
    issue = _gap_issue(p)
    assert issue["status"] == "open" and issue["watch_key"] == "schedule-gap:1700000000"
    cli.main(["issues", "list", "--project", "demo"])
    assert f"#{issue['id']} " in capsys.readouterr().out
    cli.main(["issues", "close", "schedule-gap:1700000000", "--why", "host moved; the gap is known",
              "--project", "demo"])
    assert f"closed issue #{issue['id']}" in capsys.readouterr().out
    row = _gap_issue(p)
    assert row["status"] == "fixed" and row["closed"] and "host moved; the gap is known" in row["cleared_why"]
    [h] = json.loads(row["history"])
    assert h["action"] == "closed" and h["why"] == "host moved; the gap is known" and h["by"] and h["ts"]
    feed = [m for m in alerts.feed(p.db, time.time()) if f"issue #{issue['id']}" in m["text"]]
    assert feed and "host moved" in feed[0]["text"]
    cli.main(["issues", "list", "--project", "demo"])
    assert "no open issues" in capsys.readouterr().out
    cli.main(["issues", "close", str(issue["id"]), "--why", "again", "--project", "demo"])
    assert "already closed" in capsys.readouterr().out
    assert len(json.loads(_gap_issue(p)["history"])) == 1
    queued = len(p.db.q("SELECT id FROM events WHERE source='daemon:schedule-gap' AND status='queued'"))
    d.observe("daemon:schedule-gap", text, "normal", whole=True, key="schedule-gap:1700000000")
    assert _gap_issue(p)["status"] == "open" and _gap_issue(p)["closed"] is None
    assert [h["action"] for h in json.loads(_gap_issue(p)["history"])] == ["closed", "reopened"]
    assert len(p.db.q("SELECT id FROM events WHERE source='daemon:schedule-gap' AND status='queued'")) == queued + 1


def test_issues_close_refuses_an_unknown_issue_an_empty_why_and_another_projects_issue_in_a_run(env, capsys,
                                                                                                 monkeypatch):
    from ttp import cli
    from ttp.daemon import Daemon
    for k in ("TTP_RUN_DIR", "TTP_TASK", "TTP_PROJECT"):
        monkeypatch.delenv(k, raising=False)
    p = make(env)
    Daemon(p.base).observe("daemon:schedule-gap", "Schedules missed runs since 10:00: watch 6.", "normal",
                           whole=True, key="schedule-gap:1")
    iid = _gap_issue(p)["id"]
    for argv in (["issues", "close", "9999", "--why", "x"], ["issues", "close", "no-such-key", "--why", "x"],
                 ["issues", "close", str(iid), "--why", "  "], ["issues", "close", str(iid)]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv + ["--project", "demo"])
        assert e.value.code != 0, argv
    assert _gap_issue(p)["status"] == "open" and _gap_issue(p)["history"] is None
    assert not p.db.q("SELECT id FROM messages WHERE text LIKE 'Closed issue%'")
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"] / "run"))
    with pytest.raises(SystemExit):  # a run with no TTP_PROJECT cannot name a project either
        cli.main(["issues", "close", str(iid), "--why", "no project", "--project", "demo"])
    assert _gap_issue(p)["status"] == "open"
    monkeypatch.chdir(p.base)  # no --project: the resolved project is named, never 'None'
    with pytest.raises(SystemExit):
        cli.main(["issues", "close", str(iid), "--why", "no project"])
    err = capsys.readouterr().err
    assert "None" not in err and ("demo" in err or str(p.base) in err)
    monkeypatch.setenv("TTP_PROJECT", str(env["tmp"] / "another-project"))
    monkeypatch.setenv("TTP_TASK", "7")
    with pytest.raises(SystemExit):
        cli.main(["issues", "close", str(iid), "--why", "not mine", "--project", "demo"])
    assert _gap_issue(p)["status"] == "open"
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    cli.main(["issues", "close", f"#{iid}", "--why", "stale", "--project", "demo"])
    assert json.loads(_gap_issue(p)["history"])[0]["by"] == "task #7"


def test_an_issues_table_from_before_history_gains_the_column(env):
    import sqlite3
    from ttp.db import DB
    path = env["tmp"] / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE issues (id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT UNIQUE, source TEXT, "
              "first_seen REAL, last_seen REAL, count INTEGER DEFAULT 1, title TEXT, severity TEXT DEFAULT 'normal', "
              "status TEXT DEFAULT 'open', task INTEGER, screen TEXT)")
    c.commit()
    c.close()
    db = DB(path)
    assert "history" in {r["name"] for r in db.q("PRAGMA table_info(issues)")}


def test_a_schedule_name_outside_the_safe_set_is_refused_everywhere_but_a_stored_one_keeps_working(env):
    """Schedule names show in the web app: letters, digits and . _ - only (1-64). upsert, the schedules
    file and schedule_set refuse others; a row stored before the check can still be changed."""
    from ttp import coordinator as coord
    from ttp import schedule as sched
    p = make(env)
    for bad in ("x');alert(1);//", 'a"b', "has space", "", "x" * 65, None):
        with pytest.raises(ValueError, match="not allowed"):
            sched.upsert(p.db, bad, "llm", "1d")
    sched.upsert(p.db, "nightly-build.v2_a", "llm", "1d")
    out = coord.apply(p, [{"type": "schedule_set", "name": "it's", "kind": "llm", "every": "1d", "spec": "go"}])
    assert len(out) == 1 and "schedule_set rejected" in out[0] and "not allowed" in out[0], out
    assert not p.db.one("SELECT name FROM schedules WHERE name=?", ("it's",))
    with pytest.raises(ValueError, match="entry 1: schedule name"):
        sched.parse_file(json.dumps([{"name": "a'b", "kind": "llm", "every": "1d"}]))
    # A legacy row: changed and switched off as before, in upsert and in the file.
    p.db.x("INSERT INTO schedules(name,kind,every_s,enabled,payload) VALUES('old name','llm',86400,1,'{}')")
    sched.upsert(p.db, "old name", "llm", "2d", enabled=False)
    assert p.db.one("SELECT every_s, enabled FROM schedules WHERE name='old name'") == {"every_s": 172800, "enabled": 0}
    assert sched.parse_file(json.dumps([{"name": "old name", "kind": "llm", "every": "1d"}]), {"old name"})


def _run_precheck_schedule(d):
    """run_schedules, then record a prechecked run still going after the tick's short wait."""
    d.run_schedules()
    for run in list(d._sched_runs.values()):
        run.done.wait(30)
    d.run_schedules()


def _prechecked(p, precheck, **extra):
    """An llm schedule `summary`, due now, whose precheck is `precheck`."""
    from ttp import schedule as sched
    sched.upsert(p.db, "summary", "llm", "1d", payload={"spec": "sum up", "precheck": precheck, **extra})
    p.db.x("UPDATE schedules SET next_run=? WHERE name='summary'", (time.time() - 1,))


def _summary(p):
    s = p.db.one("SELECT last_status FROM schedules WHERE name='summary'")["last_status"]
    n = p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='schedule' AND labels=?", (json.dumps(["summary"]),))["n"]
    log = (p.logs / "daemon.log").read_text() if (p.logs / "daemon.log").exists() else ""
    return s, n, log


def test_an_llm_precheck_that_exits_0_skips_the_trigger_with_its_first_line(env):
    """The case behind it: a recurring llm schedule spent model runs on empty windows a script could judge."""
    from ttp.daemon import Daemon
    p = make(env)
    _prechecked(p, "echo; echo '  nothing new in the window  '; echo second line")
    d = Daemon(p.base)
    d.gates = {}
    _run_precheck_schedule(d)
    status, n, log = _summary(p)
    assert status == "skipped: precheck: nothing new in the window" and n == 0, (status, log)
    row = p.db.one("SELECT last_run, next_run FROM schedules WHERE name='summary'")
    assert row["last_run"] and row["next_run"] > time.time() + 86400 - 120   # a full period on, not owed
    _prechecked(p, "true")   # no output: still a skip, with a reason
    _run_precheck_schedule(d)
    assert _summary(p)[:2] == ("skipped: precheck: exit 0", 0)


@pytest.mark.parametrize("precheck,extra,logged", [
    ("echo busy; echo why >&2; exit 3", {}, "precheck exited rc=3 (why); the run goes ahead"),
    ("sleep 5", {"precheck_timeout_s": 1}, "precheck timed out after 1 s; the run goes ahead"),
])
def test_an_llm_precheck_that_fails_or_times_out_lets_the_run_go_ahead(env, precheck, extra, logged):
    from ttp.daemon import Daemon
    p = make(env)
    _prechecked(p, precheck, **extra)
    d = Daemon(p.base)
    d.gates = {}
    _run_precheck_schedule(d)
    status, n, log = _summary(p)
    assert status == "queued" and n == 1, status
    assert f"schedule summary: {logged}" in log, log


def test_an_llm_precheck_that_raises_lets_the_run_go_ahead(env, monkeypatch):
    from ttp import schedrun
    from ttp.daemon import Daemon
    p = make(env)
    _prechecked(p, "true")

    def boom(*a, **k):
        raise OSError("no shell")
    monkeypatch.setattr(schedrun, "run_group", boom)
    d = Daemon(p.base)
    d.gates = {}
    _run_precheck_schedule(d)
    status, n, log = _summary(p)
    assert status == "queued" and n == 1, status
    assert "schedule summary: precheck failed (OSError: no shell); the run goes ahead" in log, log


def test_an_llm_precheck_is_not_run_when_an_earlier_gate_already_skipped(env, tmp_path):
    from types import SimpleNamespace
    from ttp.daemon import Daemon
    p = make(env)
    ran = tmp_path / "ran"
    d = Daemon(p.base)
    # The open-run gate.
    _prechecked(p, f"touch {ran}")
    tid = p.db.add_task("[summary] open", "x", origin="schedule")
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(["summary"]), tid))
    d.gates = {}
    _run_precheck_schedule(d)
    assert _summary(p)[0] == "skipped: previous run still open" and not ran.exists()
    p.db.update_task(tid, status="done")
    # The budget gate, then the schedule's daily budget.
    _prechecked(p, f"touch {ran}")
    d.gates = {d.cfg.get("core_provider", "claude"): SimpleNamespace(allow_optional=False, level="tight")}
    _run_precheck_schedule(d)
    assert _summary(p)[0] == "skipped: budget tight" and not ran.exists()
    d.gates = {}
    p.db.x("UPDATE schedules SET budget_usd_day=1, next_run=? WHERE name='summary'", (time.time() - 1,))
    p.db.x("INSERT INTO ledger(ts,source,usd) VALUES(?,?,?)", (time.time() - 60, "schedule:summary", 2.0))
    _run_precheck_schedule(d)
    assert _summary(p)[0] == "skipped: daily budget used" and not ran.exists()
    # The idle gate: nothing happened since its last run.
    p.db.x("UPDATE schedules SET budget_usd_day=NULL WHERE name='summary'")
    _prechecked(p, f"touch {ran}", skip_if_idle=True)
    p.db.x("UPDATE messages SET ts=?", (time.time() - 2 * 86400,))
    p.db.x("UPDATE runs SET started=?, ended=?", (time.time() - 2 * 86400, time.time() - 2 * 86400))
    p.db.x("UPDATE schedules SET last_run=? WHERE name='summary'", (time.time() - 86400,))
    _run_precheck_schedule(d)
    assert _summary(p)[0] == "skipped: nothing happened since the last run" and not ran.exists()


def test_a_precheck_is_set_and_cleared_by_schedule_set_and_shown_and_validated(env, capsys):
    from ttp import coordinator as coord
    from ttp import schedule as sched
    p = make(env)
    pay = lambda: json.loads(p.db.one("SELECT payload FROM schedules WHERE name='summary'")["payload"])
    assert coord.apply(p, [{"type": "schedule_set", "name": "summary", "kind": "llm", "spec": "sum up",
                            "precheck": " scripts/window-empty ", "precheck_timeout_s": 30}]) == []
    assert pay()["precheck"] == "scripts/window-empty" and pay()["precheck_timeout_s"] == 30
    assert coord.apply(p, [{"type": "schedule_set", "name": "summary", "tier": "light"}]) == []
    assert pay()["precheck"] == "scripts/window-empty"   # left out: kept
    from ttp import cli
    cli.main(["schedules", p.name])
    assert "precheck: scripts/window-empty" in capsys.readouterr().out
    assert coord.apply(p, [{"type": "schedule_set", "name": "summary", "precheck": None}]) == []
    assert "precheck" not in pay()
    for kind, payload, why in [("command", {"command": "true", "precheck": "true"}, "only an llm schedule"),
                               ("llm", {"precheck": 3}, "is a shell command"),
                               ("llm", {"precheck": "true", "precheck_timeout_s": 0}, "number of seconds")]:
        with pytest.raises(ValueError, match=why):
            sched.parse_file(json.dumps([{"name": "x", "kind": kind, "every": "1d", "payload": payload}]))


def test_turning_a_schedule_off_or_removing_it_ends_its_mutes_and_the_audit_skips_mutes_of_an_off_schedule(env):
    """A schedule turned off still held its observation mutes, and the daily audit listed them every day as
    overrides held long, though a schedule that does not run has nothing to mute."""
    from ttp import audit, schedule as sched, screen
    p = make(env)
    now = time.time()
    for name in ("watch", "probe", "gone", "live"):
        sched.upsert(p.db, name, "command", "30m", payload={"command": "true", "timeout_s": 30})
        screen.mute(p.db, f"watcher:{name}", "disk nearly full", 48, why="known")
    screen.mute(p.db, "watcher-error:watch", "timed out after", 48, why="known")
    screen.mute(p.db, "daemon:other", "something noisy", 48, why="known")
    active = lambda: {m["source"] for m in screen.mutes(p.db)}

    sched.set_enabled(p.db, "watch", False)   # the web app's switch
    assert "watcher:watch" not in active() and "watcher-error:watch" not in active()
    ended = p.db.q("SELECT status, text FROM events WHERE text LIKE 'mute ended:%watcher:watch%'")
    assert ended and all("schedule turned off" in e["text"] and e["status"] == "handled" for e in ended)
    sched.upsert(p.db, "probe", "command", "30m", enabled=False, payload={"command": "true"})   # schedule_set
    assert "watcher:probe" not in active()
    assert active() == {"watcher:gone", "watcher:live", "daemon:other"}, "other mutes are unchanged"

    sched.file_path(p).parent.mkdir(parents=True, exist_ok=True)
    sched.file_path(p).write_text(json.dumps([sched.entry(r) for r in p.db.q(
        "SELECT * FROM schedules WHERE name!='gone'")]))
    assert sched.sync_file(p, force=True) == (True, None)
    assert active() == {"watcher:live", "daemon:other"}
    assert p.db.q("SELECT 1 FROM events WHERE text LIKE 'mute ended:%watcher:gone%schedule removed%'")

    # A mute left on an off schedule (set before this change, or written directly) is no override held.
    mutes = p.db.kv(screen.MUTES_KEY)
    p.db.set_kv(screen.MUTES_KEY, [{**m, "since": now - 30 * 3600} for m in mutes]
                + [{**mutes[0], "source": "watcher:watch", "since": now - 30 * 3600}])
    held = audit.overrides(p, p.db, now, 12)
    assert any(h.startswith("mute of watcher:live") for h in held), "a running schedule's mute still counts"
    assert any(h.startswith("mute of daemon:other") for h in held)
    assert not any("watcher:watch" in h for h in held), "the off schedule's mute is skipped"
    p.db.x("DELETE FROM schedules WHERE name='live'")
    assert not any("watcher:live" in h for h in audit.overrides(p, p.db, now, 12)), "a gone schedule's too"


def test_a_schedule_gap_issue_closes_itself_once_every_schedule_it_names_ran_again_or_is_off(env):
    """A one-time gap is over once each schedule it named has run again (or was turned off or removed):
    the tick's sweep closes its issue with the reason, so none stays open for days until a hand close."""
    from ttp import schedule as sched
    from ttp.daemon import Daemon, SCHEDULE_GAP_CLEARED, SCHEDULE_GAP_KEY
    p = make(env)
    _late_schedule(p, 3 * 3600)
    sched.upsert(p.db, "other", "command", "30m", payload={"command": "true", "timeout_s": 30})
    sched.upsert(p.db, "gone", "command", "30m", payload={"command": "true", "timeout_s": 30})
    now = time.time()
    p.db.x("UPDATE schedules SET last_run=?, next_run=? WHERE name IN ('other','gone')", (now - 4 * 3600, now - 3 * 3600))
    d = Daemon(p.base)
    d.gates = {}
    d._started = now - 10 * 3600
    d._schedule_gaps([dict(r) for r in p.db.q("SELECT * FROM schedules")])
    issue = _gap_issue(p)
    assert issue["status"] == "open"
    assert p.db.kv(SCHEDULE_GAP_KEY)[issue["watch_key"]] == ["gone", "other", "watch"]
    assert d.sweep_schedule_gaps() == 0 and _gap_issue(p)["status"] == "open"   # none ran yet
    seen = float(issue["last_seen"])
    p.db.x("UPDATE schedules SET last_run=? WHERE name IN ('watch','gone')", (seen + 1,))
    assert d.sweep_schedule_gaps() == 0 and _gap_issue(p)["status"] == "open"   # 'other' has not run
    p.db.x("UPDATE schedules SET last_run=? WHERE name='gone'", (seen - 1,))
    p.db.x("UPDATE schedules SET last_run=? WHERE name='other'", (seen + 2,))
    assert d.sweep_schedule_gaps() == 0 and _gap_issue(p)["status"] == "open"   # 'gone' ran before it
    sched.set_enabled(p.db, "gone", False)   # the rest is off
    assert d.sweep_schedule_gaps() == 1
    row = _gap_issue(p)
    assert row["status"] == "fixed" and row["closed"] and row["cleared_why"].startswith(SCHEDULE_GAP_CLEARED)
    assert "off" in row["cleared_why"] and json.loads(row["history"])[-1]["action"] == "closed"
    assert not p.db.kv(SCHEDULE_GAP_KEY)   # its names go with it; a new gap records them again
    assert d.sweep_schedule_gaps() == 0


def test_a_schedule_gap_issue_closes_once_all_named_ran_in_the_tick_and_one_without_names_stays_open(env):
    from ttp.daemon import Daemon, SCHEDULE_GAP_CLEARED
    p = make(env)
    _late_schedule(p, 3 * 3600)
    d = Daemon(p.base)
    d.gates = {}
    d._started = time.time() - 10 * 3600
    d.run_schedules()   # observes the gap, then runs 'watch'
    assert p.db.one("SELECT last_run FROM schedules WHERE name='watch'")["last_run"] > _gap_issue(p)["last_seen"]
    d.observe("daemon:schedule-gap", "Schedules missed runs since 09:00: old 3.", "normal", whole=True,
              key="schedule-gap:1")   # from before names were recorded: left for a hand close
    d.tick()   # its issue sweep closes the gap that is over
    rows = {r["watch_key"]: r for r in p.db.q("SELECT * FROM issues WHERE source='daemon:schedule-gap'")}
    assert rows["schedule-gap:1"]["status"] == "open"
    gap = next(r for k, r in rows.items() if k != "schedule-gap:1")
    assert gap["status"] == "fixed" and gap["cleared_why"] == SCHEDULE_GAP_CLEARED


def _filler(p, budget_day=None, **payload):
    """An llm schedule `filler`, due now, with daily cap `budget_day`."""
    from ttp import schedule as sched
    sched.upsert(p.db, "filler", "llm", "1h", budget_usd_day=budget_day, description="filler work",
                 payload={"spec": "do filler work", **payload})
    _filler_due(p)


def _filler_due(p):
    p.db.x("UPDATE schedules SET next_run=? WHERE name='filler'", (time.time() - 1,))


def _budget_issues(p, status="open"):
    return p.db.q("SELECT watch_key, status FROM issues WHERE source='daemon:schedule-budget' AND status=?", (status,))


def _budget_wakes(p):
    return p.db.q("SELECT text FROM events WHERE source='daemon:schedule-budget' AND kind='observation'")


def test_an_own_daily_budget_skip_is_kept_and_raises_one_keyed_observation_until_the_schedule_runs(env):
    """The case behind it: a schedule with too low a daily cap was skipped about 13 times in 2 h with a
    green gate; each skip only overwrote last_status, so nobody noticed until the user looked by hand."""
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.gates = {}
    _filler(p, budget_day=1)
    p.db.x("INSERT INTO ledger(ts,source,usd) VALUES(?,?,?)", (time.time() - 60, "schedule:filler", 2.0))
    d.run_schedules()
    assert [x["reason"] for x in sched.skips(p.db, "filler")] == ["skipped: daily budget used"]
    assert _budget_issues(p) == [{"watch_key": "schedule-budget:filler", "status": "open"}]
    wakes = _budget_wakes(p)
    assert len(wakes) == 1 and "filler" in wakes[0]["text"] and "$2.00 of $1.00" in wakes[0]["text"], wakes
    # A second skip the same day: kept, but no second issue or wake, even once the first is closed by hand.
    _filler_due(p)
    d.run_schedules()
    p.db.x("UPDATE issues SET status='fixed' WHERE source='daemon:schedule-budget'")
    _filler_due(p)
    d.run_schedules()
    assert len(sched.skips(p.db, "filler")) == 3
    assert len(_budget_wakes(p)) == 1 and not _budget_issues(p)
    # Its spend back under the cap: it runs, and the open issue closes itself.
    p.db.x("UPDATE issues SET status='open' WHERE source='daemon:schedule-budget'")
    p.db.x("DELETE FROM ledger WHERE source='schedule:filler'")
    _filler_due(p)
    d.run_schedules()
    assert p.db.one("SELECT last_status FROM schedules WHERE name='filler'")["last_status"] == "queued"
    assert not _budget_issues(p) and len(_budget_issues(p, "fixed")) == 1
    assert len(sched.skips(p.db, "filler")) == 3, "a run never overwrites the skip history"


def _finished_runs(p, name, costs):
    for usd in costs:
        tid = p.db.add_task(f"[{name}] run", "x", origin="schedule")
        p.db.x("UPDATE tasks SET labels=?, status='done', spent_usd=? WHERE id=?", (json.dumps([name]), usd, tid))


def test_a_suggested_daily_cap_is_runs_a_day_times_the_median_run_cost_and_needs_three_runs(env):
    from ttp import audit, schedule as sched
    p = make(env)
    _filler(p, budget_day=1)
    row = lambda: p.db.one("SELECT * FROM schedules WHERE name='filler'")
    _finished_runs(p, "filler", [0.20, 0.0, 0.90])   # a run that cost nothing never counts
    assert sched.suggest_cap(p.db, row()) is None, "under 3 runs: no suggestion"
    _finished_runs(p, "other", [5.0, 5.0, 5.0])      # another schedule's runs are not its own
    _finished_runs(p, "filler", [0.10, 0.20])
    assert sched.suggest_cap(p.db, row()) == {"runs_day": 24, "median_usd": 0.2, "runs": 4, "usd": 6.0}
    _finished_runs(p, "filler", [0.20] * 10)          # only the last 10
    hint = sched.suggest_cap(p.db, row())
    assert hint == {"runs_day": 24, "median_usd": 0.2, "runs": 10, "usd": 6.0}, hint
    assert sched.cap_text(hint) == "suggested cap $6.00 = 24 runs/day x $0.20 median (10 runs) x 1.25"
    assert sched.below_suggestion(p.db) == ["filler cap $1.00/day, " + sched.cap_text(hint)]
    assert any(x.startswith("schedule caps under their suggestion: 1: filler cap $1.00/day")
               for x in audit.lines(p.db, p=p))
    sched.upsert(p.db, "filler", "llm", "1h", budget_usd_day=6, payload={"spec": "x"})
    assert sched.below_suggestion(p.db) == [] and not any("under their suggestion" in x for x in audit.lines(p.db, p=p))
    sched.upsert(p.db, "watch", "command", "1h", payload={"command": "true"})
    _finished_runs(p, "watch", [1.0] * 3)
    assert sched.suggest_cap(p.db, p.db.one("SELECT * FROM schedules WHERE name='watch'")) is None, "llm only"


def test_ttp_schedules_shows_the_suggested_cap_next_to_the_daily_cap(env, capsys):
    from ttp import cli
    p = make(env)
    _filler(p, budget_day=1)
    _finished_runs(p, "filler", [0.20] * 5)
    capsys.readouterr()
    cli.main(["schedules", p.name])
    listed = capsys.readouterr().out
    cli.main(["schedules", p.name, "show", "filler"])
    shown = json.loads(capsys.readouterr().out)
    assert "cap $1.00/day (suggested cap $6.00 = 24 runs/day x $0.20 median (5 runs) x 1.25)" in listed, listed
    assert shown["budget_usd_day"] == 1 and shown["state"]["suggested_budget_usd_day"]["usd"] == 6.0, shown


def _cap_issues(p, status="open"):
    return p.db.q("SELECT watch_key FROM issues WHERE source='daemon:schedule-cap' AND status=?", (status,))


def _cap_wakes(p):
    return p.db.q("SELECT text FROM events WHERE source='daemon:schedule-cap' AND kind='observation'")


def test_a_schedule_past_80_percent_of_its_own_cap_is_observed_once_a_budget_day_only_on_a_green_gate(env):
    from types import SimpleNamespace
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    gate = lambda level, optional=True: {d.cfg.get("core_provider", "claude"):
                                         SimpleNamespace(allow_optional=optional, level=level)}
    sched.upsert(p.db, "filler", "llm", "1h", budget_usd_day=1, payload={"spec": "x"})
    spend = lambda usd: p.db.x("INSERT INTO ledger(ts,source,usd) VALUES(?,?,?)",
                               (time.time() - 60, "schedule:filler", usd))
    spend(0.70)
    d.gates = gate("green")
    d._schedule_cap_warn()
    assert not _cap_wakes(p), "under 80%"
    spend(0.15)
    for level, optional in (("yellow", True), ("yellow", False), ("orange", False), ("red", False)):
        d.gates = gate(level, optional)
        d._schedule_cap_warn()
    assert not _cap_wakes(p), "only while the gate is green"
    d.gates = gate("green")
    d._schedule_cap_warn()
    wakes = _cap_wakes(p)
    assert len(wakes) == 1 and "$0.85 of its own $1.00" in wakes[0]["text"], wakes
    assert _cap_issues(p) == [{"watch_key": "schedule-cap-80:filler"}]
    spend(0.10)
    d._schedule_cap_warn()
    p.db.x("UPDATE issues SET status='fixed' WHERE source='daemon:schedule-cap'")
    d._schedule_cap_warn()
    assert len(_cap_wakes(p)) == 1, "once a budget day, even once the first is closed by hand"
    # A new budget day: the open warning closes, and it may fire again.
    p.db.x("UPDATE issues SET status='open' WHERE source='daemon:schedule-cap'")
    warned = p.db.kv("schedule_cap_warned")
    p.db.set_kv("schedule_cap_warned", {"filler": {**warned["filler"], "at": time.time() - 86401}})
    d.gates = gate("yellow")
    d._schedule_cap_warn()
    assert not _cap_issues(p) and len(_cap_issues(p, "fixed")) == 1 and len(_cap_wakes(p)) == 1
    d.gates = gate("green")
    d._schedule_cap_warn()
    assert len(_cap_wakes(p)) == 2


def test_the_80_percent_warning_follows_a_fixed_budget_day(env):
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.gates = {}
    d.cfg.setdefault("budget", {})["day_start"] = "00:00"
    sched.upsert(p.db, "filler", "llm", "1h", budget_usd_day=1, payload={"spec": "x"})
    p.db.x("INSERT INTO ledger(ts,source,usd) VALUES(?,?,?)", (time.time() - 60, "schedule:filler", 0.9))
    d._schedule_cap_warn()
    d._schedule_cap_warn()
    assert len(_cap_wakes(p)) == 1
    p.db.set_kv("schedule_cap_warned", {"filler": {"day": 0.0, "at": time.time()}})   # raised on an earlier day
    d._schedule_cap_warn()
    assert len(_cap_wakes(p)) == 2


def test_expected_schedule_skips_are_kept_but_never_raised(env, tmp_path):
    """A gate held by the budget, a debounce, an open run and a precheck exit 0 are planned skips."""
    from types import SimpleNamespace
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.gates = {d.cfg.get("core_provider", "claude"): SimpleNamespace(allow_optional=False, level="yellow")}
    _filler(p, budget_day=1)
    p.db.x("INSERT INTO ledger(ts,source,usd) VALUES(?,?,?)", (time.time() - 60, "schedule:filler", 2.0))
    d.run_schedules()   # the gate holds it before its own cap is read
    d.gates = {}
    p.db.x("DELETE FROM ledger")
    tid = p.db.add_task("[filler] open", "x", origin="schedule")
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(["filler"]), tid))
    _filler_due(p)
    d.run_schedules()   # its previous run still open
    p.db.update_task(tid, status="done")
    _prechecked(p, "echo window empty")
    _run_precheck_schedule(d)
    assert [x["reason"] for x in sched.skips(p.db, "filler")] == ["skipped: previous run still open",
                                                                  "skipped: budget yellow"]
    assert [x["reason"] for x in sched.skips(p.db, "summary")] == ["skipped: precheck: window empty"]
    assert not _budget_wakes(p) and not p.db.q("SELECT id FROM issues WHERE source='daemon:schedule-budget'")
    # A debounce: the last run done on unchanged evidence.
    sched.upsert(p.db, "filler", "llm", "1h", payload={"spec": "do filler work", "debounce_h": 6})
    _filler_due(p)
    d.run_schedules()
    run = p.db.one("SELECT id FROM tasks WHERE labels=? AND status='queued'", (json.dumps(["filler"]),))["id"]
    p.db.update_task(run, status="done")
    p.db.x("INSERT INTO runs(task,role,status,started,ended) VALUES(?,?,?,?,?)",
           (run, "worker", "done", time.time() - 120, time.time() - 60))
    _filler_due(p)
    d.run_schedules()
    assert sched.skips(p.db, "filler", 1)[0]["reason"].startswith("skipped: last run done"), sched.skips(p.db, "filler")
    assert not _budget_wakes(p)


def test_a_schedule_runs_budget_is_its_tier_default_or_its_own_and_never_more_than_the_day_has_left(env):
    """Each run's budget was the whole daily cap, so raising the cap also raised every single run's cap."""
    from ttp import coordinator as coord
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.gates = {}
    defaults = p.config()["budget"]["task_default_usd"]

    def queued(budget_day, spent=0.0, **payload):
        p.db.x("DELETE FROM tasks")
        p.db.x("DELETE FROM ledger")
        if spent:
            p.db.x("INSERT INTO ledger(ts,source,usd) VALUES(?,?,?)", (time.time() - 60, "schedule:filler", spent))
        _filler(p, budget_day=budget_day, **payload)
        d.run_schedules()
        return p.db.one("SELECT budget_usd FROM tasks WHERE labels=?", (json.dumps(["filler"]),))["budget_usd"]

    assert queued(100) == defaults["standard"]                      # not the daily cap
    assert queued(None, tier="light") == defaults["light"]
    assert queued(100, budget_usd_run=3) == 3.0
    assert queued(5, spent=4.0) == pytest.approx(1.0)               # what is left of the day
    assert queued(5, spent=4.5, budget_usd_run=3) == pytest.approx(0.5)
    # schedule_set sets and clears it; the file and the action reject a bad one.
    assert coord.apply(p, [{"type": "schedule_set", "name": "filler", "budget_usd_run": 2.5}]) == []
    pay = lambda: json.loads(p.db.one("SELECT payload FROM schedules WHERE name='filler'")["payload"])
    assert pay()["budget_usd_run"] == 2.5
    assert coord.apply(p, [{"type": "schedule_set", "name": "filler", "tier": "deep"}]) == []
    assert pay()["budget_usd_run"] == 2.5   # left out: kept
    assert coord.apply(p, [{"type": "schedule_set", "name": "filler", "budget_usd_run": None,
                            "clear_fields": ["budget_usd_run"]}]) == []
    assert "budget_usd_run" not in pay()
    out = coord.apply(p, [{"type": "schedule_set", "name": "filler", "budget_usd_run": -1}])
    assert len(out) == 1 and "budget_usd_run" in out[0], out
    for kind, payload, why in [("llm", {"budget_usd_run": 0}, "positive number"),
                               ("command", {"command": "true", "budget_usd_run": 1}, "only an llm schedule")]:
        with pytest.raises(ValueError, match=why):
            sched.parse_file(json.dumps([{"name": "x", "kind": kind, "every": "1d", "payload": payload}]))
    assert sched.parse_file(json.dumps([{"name": "x", "kind": "llm", "every": "1d",
                                         "payload": {"budget_usd_run": 1.5}}]))


def test_the_skip_history_survives_a_daemon_restart_is_capped_and_shown(env, capsys):
    from ttp import cli
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    p = make(env)
    _filler(p, budget_day=1)
    for i in range(sched.SKIP_KEEP + 5):
        sched.record_skip(p.db, "filler", f"skipped: precheck: empty {i}", time.time() - 3600 + i)
    sched.record_skip(p.db, "other", "skipped: budget red")
    p.db.conn.close()
    p = type(p)(p.base)   # a new process: a fresh connection to the same database
    d = Daemon(p.base)
    d.gates = {}
    kept = sched.skips(p.db, "filler", 100)
    assert len(kept) == sched.SKIP_KEEP and kept[0]["reason"] == f"skipped: precheck: empty {sched.SKIP_KEEP + 4}"
    assert [x["reason"] for x in sched.skips(p.db, "other")] == ["skipped: budget red"]
    cli.main(["schedules", p.name, "show", "filler"])
    shown = json.loads(capsys.readouterr().out)["state"]["recent_skips"]
    assert len(shown) == 10 and shown[0]["reason"].endswith(f"empty {sched.SKIP_KEEP + 4}")
    cli.main(["schedules", p.name])
    assert f"({sched.SKIP_KEEP} skip(s) in 24 h)" in capsys.readouterr().out
    row = next(r for r in sched.with_costs(p.db) if r["name"] == "filler")
    assert row["skips_24h"] == sched.SKIP_KEEP and len(row["skips"]) == 5


def test_schedule_skip_records_leave_the_quiet_gate_settling_all_held_idle_wakes(env):
    """A skip-history row is a record: it never changes the quiet gate's 'nothing new arrived'
    fingerprint, so an all-held idle wake after a precheck or debounce skip is still settled quiet,
    even when coordinator.quiet_held_kinds names other kinds."""
    from ttp import quietgate, schedule as sched
    from ttp.daemon import Daemon
    p = make(env)
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    t = p.db.add_task("tuning", "s", origin="user")
    p.db.update_task(t, labels=["start_after:" + str(time.time() + 86400)])
    _prechecked(p, "echo window empty")
    d = Daemon(p.base)
    assert sched.SKIP_KIND in quietgate.RECORD_KINDS
    for cfg in ({}, {"coordinator": {"quiet_held_kinds": ["other"]}}):
        fp = quietgate.fingerprint(p.db, cfg, "wake")
        sched.record_skip(p.db, "filler", "skipped: last run done on unchanged evidence")
        assert quietgate.fingerprint(p.db, cfg, "wake") == fp
    cfg, now = {}, time.time()
    fp = quietgate.fingerprint(p.db, cfg, "wake")
    quietgate.left(p.db, fp, 1, now - 60)
    _run_precheck_schedule(d)
    assert [x["reason"] for x in sched.skips(p.db, "summary")] == ["skipped: precheck: window empty"]
    fp2 = quietgate.fingerprint(p.db, cfg, "wake")
    assert fp2 == fp and quietgate.all_held(p.db, cfg, now)
    assert quietgate.quiet(p.db, cfg, [], [], lambda: [], "idle", fp2, now)


def test_schedule_set_refuses_a_timeout_above_its_cap_and_names_the_cap_and_the_value(env):
    """The case behind it: a command schedule set with timeout_s 400 was cut at the daemon's 240 s watcher cap
    with no word of it, so its author believed it had 400 s."""
    from ttp import coordinator as coord
    from ttp import daemon as dm
    from ttp import heal
    from ttp import schedule as sched
    p = make(env)
    row = lambda name: p.db.one("SELECT payload FROM schedules WHERE name=?", (name,))
    base = {"type": "schedule_set", "name": "slow", "kind": "command", "every": "30m", "command": "true"}
    for extra, said in [({"timeout_s": 400}, f"`timeout_s` 400 is above its cap of {dm.WATCHER_MAX_S} s"),
                        ({"timeout_s": "400"}, "`timeout_s` 400 is above its cap"),   # the daemon reads text too
                        ({"timeout_s": 240.5}, "`timeout_s` 240.5 is above its cap"),
                        ({"heal": {"check": "true", "timeout_s": 400}}, "`heal.timeout_s` 400 is above its cap of 240 s"),
                        ({"heal": {"check": "true", "fix": "true", "fix_detached": True, "fix_timeout_s": 20000}},
                         f"`heal.fix_timeout_s` 20000 is above its cap of {heal.FIX_MAX_TIMEOUT_S} s"),
                        ({"kind": "llm", "spec": "sum up", "precheck": "true", "precheck_timeout_s": 600},
                         "`precheck_timeout_s` 600 is above its cap of 240 s")]:
        out = coord.apply(p, [{**base, **extra}])
        assert len(out) == 1 and "schedule_set 'slow' rejected" in out[0] and said in out[0], out
        assert row("slow") is None
    # At the cap it is set as before, and a later change that leaves timeout_s out keeps it.
    assert coord.apply(p, [{**base, "timeout_s": dm.WATCHER_MAX_S}]) == []
    assert json.loads(row("slow")["payload"])["timeout_s"] == dm.WATCHER_MAX_S
    # A stored over-cap value never blocks another change (switching it off, a new command).
    sched.upsert(p.db, "old", "command", "30m", payload={"command": "true", "timeout_s": 400})
    assert coord.apply(p, [{"type": "schedule_set", "name": "old", "enabled": False}]) == []
    assert coord.apply(p, [{"type": "schedule_set", "name": "old", "command": "date"}]) == []
    assert json.loads(row("old")["payload"]) == {"command": "date", "timeout_s": 400}


def _timed_out_runs(p, monkeypatch, name, payload, runs=2):
    """Store command schedule `name` with `payload`, run it `runs` times through the daemon with every
    command timing out; returns (the timeouts it ran with, its last status, daemon.log)."""
    from ttp import daemon as dm
    from ttp import schedule as sched
    seen = []

    def run_group(cmd, **kw):
        seen.append(kw["timeout"])
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(dm.schedrun, "run_group", run_group)
    sched.upsert(p.db, name, "command", "30m", payload=payload)
    d = dm.Daemon(p.base)
    d.gates = {}
    for _ in range(runs):
        p.db.x("UPDATE schedules SET next_run=? WHERE name=?", (time.time() - 1, name))
        _run_precheck_schedule(d)
    status = p.db.one("SELECT last_status FROM schedules WHERE name=?", (name,))["last_status"]
    log = (p.logs / "daemon.log").read_text() if (p.logs / "daemon.log").exists() else ""
    return seen, status, log


def test_a_stored_over_cap_schedule_runs_at_the_cap_warns_once_and_its_timeout_names_both(env, monkeypatch, capsys):
    from ttp import cli
    from ttp import daemon as dm
    p = make(env)
    seen, status, log = _timed_out_runs(p, monkeypatch, "slow", {"command": "sleep 999", "timeout_s": 400})
    assert seen == [dm.WATCHER_MAX_S] * 2
    assert status == f"error: timed out after {dm.WATCHER_MAX_S} s (cap; asked 400)", status
    warn = f"schedule slow: `timeout_s` 400 is above its cap of {dm.WATCHER_MAX_S} s; it runs at the cap"
    assert log.count(warn) == 1, log   # once per schedule, not per run
    err = p.db.q("SELECT text FROM events WHERE source='watcher-error:slow'")
    assert any(f"timed out after {dm.WATCHER_MAX_S} s (cap; asked 400): sleep 999" in e["text"] for e in err), err
    capsys.readouterr()
    cli.main(["schedules", p.name])
    assert "timeout 400s (runs at the cap: `timeout_s` 400 is above its cap of 240 s)" in capsys.readouterr().out
    # Back at the cap the warning's record goes, so a later over-cap value warns again.
    d = dm.Daemon(p.base)
    d._warn_over_cap({"name": "slow", "kind": "command"}, {"command": "x", "timeout_s": 200})
    assert "slow" not in (p.db.kv(d.CAP_WARNED_KEY) or {})
    d._warn_over_cap({"name": "slow", "kind": "command"}, {"command": "x", "timeout_s": 500})
    assert "`timeout_s` 500 is above" in (p.logs / "daemon.log").read_text()


def test_a_schedule_at_or_under_the_cap_times_out_as_before_with_no_warning(env, monkeypatch):
    from ttp import daemon as dm
    p = make(env)
    seen, status, log = _timed_out_runs(p, monkeypatch, "quick", {"command": "sleep 999", "timeout_s": dm.WATCHER_MAX_S})
    assert seen == [dm.WATCHER_MAX_S] * 2 and status == f"error: timed out after {dm.WATCHER_MAX_S} s", status
    assert "cap" not in log and not p.db.kv(dm.Daemon.CAP_WARNED_KEY)
    err = p.db.q("SELECT text FROM events WHERE source='watcher-error:quick'")
    assert err and all(e["text"] == "watcher command timed out: sleep 999" for e in err), err


def test_an_over_cap_precheck_and_heal_timeout_name_the_cap_and_the_value_asked(env, monkeypatch):
    from ttp import daemon as dm
    from ttp import heal
    p = make(env)

    def run_group(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(dm.schedrun, "run_group", run_group)
    _prechecked(p, "sleep 999", precheck_timeout_s=400)
    d = dm.Daemon(p.base)
    d.gates = {}
    _run_precheck_schedule(d)
    log = _summary(p)[2]
    assert "schedule summary: precheck timed out after 240 s (cap; asked 400); the run goes ahead" in log, log
    assert log.count("schedule summary: `precheck_timeout_s` 400 is above its cap of 240 s") == 1
    # A heal block: loaded at the cap, and its timed-out check or fix says so; at the cap, as before.
    spec = heal.validate({"check": "sleep 999", "fix": "true", "timeout_s": 400})
    assert spec["timeout_s"] == heal.MAX_TIMEOUT_S
    monkeypatch.setattr(heal, "_shell", lambda cmd, cwd, t, env=None: (124, f"timed out after {t:.0f} s"))
    assert heal.run_fix(spec, str(p.root), {}) == (124, "timed out after 240 s (cap; asked 400)")
    assert heal.check(spec, {}, str(p.root))[1].startswith("timed out after 240 s (cap; asked 400)")
    under = heal.validate({"check": "sleep 999", "fix": "true", "timeout_s": 240})
    assert "timeout_asked_s" not in under and heal.run_fix(under, str(p.root), {}) == (124, "timed out after 240 s")
    fix = heal.validate({"check": "true", "fix": "true", "fix_detached": True, "fix_timeout_s": 20000})
    assert fix["fix_timeout_s"] == heal.FIX_MAX_TIMEOUT_S and fix["fix_timeout_asked_s"] == 20000


def test_schedule_and_heal_commands_read_eof_not_the_daemons_open_stdin_pipe(tmp_path):
    """The daemon's stdin an open pipe nobody writes to or closes: a command schedule (or precheck) and a
    heal check that read stdin get EOF at once instead of blocking until their timeout."""
    from ttp import heal, schedrun
    rd, wr = os.pipe()
    saved = os.dup(0)
    os.dup2(rd, 0)
    try:
        t0 = time.monotonic()
        out = schedrun.run_group("cat; echo done", cwd=str(tmp_path), timeout=10, env=None)
        assert out.returncode == 0 and out.stdout == "done\n" and time.monotonic() - t0 < 5, out
        t0 = time.monotonic()
        assert heal._exec("cat; echo done", str(tmp_path), 10) == (0, "done") and time.monotonic() - t0 < 5
    finally:
        os.dup2(saved, 0)
        for fd in (saved, rd, wr):
            os.close(fd)
