# Self-review area: the daily review never stops silently. A missing, silently disabled or skipped schedule,
# or no review receipt for over 26 h while work happened, is restored and wakes the coordinator once with
# its cause; a user's own switch-off is left alone. Exec'd into test_tt_project_runtime.py: make and its
# imports are in scope.

H = 3600


def _sr_daemon(p, created_ago=3 * 86400):
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    d.gates = {}
    d.cfg["created"] = time.time() - created_ago
    return d


def _sr_receipt(p, ago_h, summary="reviewed; needless asks: 0 of 1", status="done"):
    """A daily-review run task that ended `ago_h` hours ago."""
    db, ended = p.db, time.time() - ago_h * H
    tid = db.add_task("[daily-review] review", "s", origin="schedule", labels=["daily-review"], status=status)
    db.x("UPDATE tasks SET result=?, created=?, updated=? WHERE id=?",
         (json.dumps({"status": status, "summary": summary}), ended - 600, ended, tid))
    db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)",
         (tid, "worker", ended - 600, ended, "done"))
    return tid


def _sr_work(p, ago_h=1):
    """Ordinary worker activity `ago_h` hours ago."""
    tid = p.db.add_task("other work", "s")
    t = time.time() - ago_h * H
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)", (tid, "worker", t - 60, t, "done"))


def _sr_events(p):
    return p.db.q("SELECT text, status FROM events WHERE source='daemon:self-review'")


def _sr_runs(p, status=None):
    return p.db.q("SELECT id, priority, status FROM tasks WHERE origin='schedule' AND labels=?"
                  + (" AND status=?" if status else ""), (json.dumps(["daily-review"]),) + ((status,) if status else ()))


def _sr_check(d):
    d.check_self_review(every_s=0)


def test_the_template_has_the_daily_review_and_the_fallback_matches_it(env):
    from ttp import selfreview
    assert selfreview.template_entry() == selfreview.FALLBACK


def test_a_healthy_project_raises_nothing_and_calls_no_model(env, monkeypatch):
    from ttp import selfreview
    p = make(env)
    d = _sr_daemon(p)
    _sr_receipt(p, 5)
    _sr_work(p)
    called = []
    monkeypatch.setattr(d, "_schedule_llm", lambda *a, **k: called.append(a) or "queued")
    monkeypatch.setattr(d, "observe", lambda *a, **k: called.append(a))
    _sr_check(d)
    assert not called and not _sr_events(p) and len(_sr_runs(p)) == 1
    assert p.db.kv(selfreview.STATE_KEY) is None
    assert selfreview.line(p.db, time.time(), d.cfg["created"]) == "self-review: last ran 5 h ago"


def test_an_idle_project_is_not_overdue(env):
    p = make(env)
    d = _sr_daemon(p)
    _sr_receipt(p, 40)   # nothing happened since (the harness's own posts are none): a review would report nothing
    p.db.post("in", "the user wrote", chat=None, channel="system", kind="user", provenance="system")
    _sr_check(d)
    assert not _sr_events(p) and len(_sr_runs(p)) == 1


def test_a_young_project_with_no_review_yet_is_not_overdue(env):
    q = make(env)
    d = _sr_daemon(q, created_ago=5 * H)
    _sr_work(q)
    _sr_check(d)
    assert not _sr_events(q) and not _sr_runs(q)


def test_a_missing_schedule_is_restored_a_run_queued_at_priority_2_and_one_event_names_the_cause(env):
    from ttp import selfreview
    p = make(env)
    p.db.x("DELETE FROM schedules WHERE name='daily-review'")   # a project from before the template had it
    d = _sr_daemon(p)
    _sr_work(p)
    _sr_check(d)
    row = p.db.one("SELECT * FROM schedules WHERE name='daily-review'")
    assert row and row["enabled"] and json.loads(row["payload"])["prompt"] == "daily-review.md"
    runs = _sr_runs(p)
    assert [r["priority"] for r in runs] == [2] and runs[0]["status"] == "queued"
    ev = _sr_events(p)
    assert len(ev) == 1 and ev[0]["text"].startswith("daily review overdue: its schedule was missing")
    assert "restored from the template" in ev[0]["text"] and f"queued run #{runs[0]['id']}" in ev[0]["text"]
    _sr_check(d)   # the next tick: its run is on the way; no second run, no second event
    assert len(_sr_runs(p)) == 1 and len(_sr_events(p)) == 1
    assert selfreview.line(p.db, time.time(), d.cfg["created"]).startswith(f"self-review: run #{runs[0]['id']} queued")
    # The run ends done with its receipt: the episode clears with its condition.
    p.db.x("UPDATE tasks SET status='done', result=? WHERE id=?", (json.dumps({"summary": "ok"}), runs[0]["id"]))
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)",
           (runs[0]["id"], "worker", time.time() - 60, time.time(), "done"))
    _sr_check(d)
    assert p.db.kv(selfreview.STATE_KEY) is None and not p.db.q(
        "SELECT id FROM events WHERE source='daemon:self-review' AND status='queued'")
    assert selfreview.line(p.db, time.time(), d.cfg["created"]) == "self-review: last ran 0 h ago"


def test_a_missing_schedule_is_written_back_to_a_projects_schedules_file(env):
    from ttp import schedule as sched
    p = make(env)
    sched.write_file(p, "export", create=True)
    p.db.x("DELETE FROM schedules WHERE name='daily-review'")
    sched.write_file(p, "lost it")
    d = _sr_daemon(p)
    _sr_check(d)
    assert "daily-review" in sched.file_path(p).read_text()


def test_a_schedule_the_user_turned_off_or_removed_is_left_alone_with_no_event(env):
    from ttp import schedule as sched, selfreview
    p = make(env)
    d = _sr_daemon(p)
    _sr_work(p)
    sched.set_enabled(p.db, "daily-review", False)
    selfreview.record_off(p.db, "daily-review", False, "the web app", True)
    _sr_check(d)
    assert not p.db.one("SELECT enabled FROM schedules WHERE name='daily-review'")["enabled"]
    assert not _sr_events(p) and not _sr_runs(p)
    assert selfreview.line(p.db, time.time(), d.cfg["created"]) == "self-review: off by the user (the web app)"
    # Removed in a harness commit: the file is the user's record; nothing comes back.
    sched.write_file(p, "export", create=True)
    path = sched.file_path(p)
    path.write_text(json.dumps([e for e in json.loads(path.read_text()) if e["name"] != "daily-review"]))
    assert sched.sync_file(p) == (True, None)
    _sr_check(d)
    assert not p.db.one("SELECT name FROM schedules WHERE name='daily-review'") and not _sr_events(p)
    assert p.db.kv(selfreview.OFF_KEY)["how"] == "removed"


@pytest.mark.parametrize("record", [None, ("the coordinator", False)])
def test_a_schedule_turned_off_without_the_users_word_is_turned_back_on(env, record):
    from ttp import schedule as sched, selfreview
    p = make(env)
    d = _sr_daemon(p)
    _sr_receipt(p, 30)
    _sr_work(p)
    sched.set_enabled(p.db, "daily-review", False)
    if record:
        selfreview.record_off(p.db, "daily-review", False, *record)
    _sr_check(d)
    assert p.db.one("SELECT enabled FROM schedules WHERE name='daily-review'")["enabled"]
    ev = _sr_events(p)
    assert len(ev) == 1 and "off with no record of the user turning it off" in ev[0]["text"]
    assert "turned back on" in ev[0]["text"] and ("turned off by the coordinator" in ev[0]["text"]) == bool(record)
    assert [r["priority"] for r in _sr_runs(p, "queued")] == [2]


def test_coordinator_schedule_set_off_records_whether_it_was_a_user_turn(env):
    from ttp import coordinator as coord, selfreview
    p = make(env)
    act = [{"type": "schedule_set", "name": "daily-review", "kind": "llm", "every": "1d", "enabled": False}]
    coord.apply(p, act)
    assert p.db.kv(selfreview.OFF_KEY)["user"] is False
    assert "the self-review check turns it back on" in json.dumps(p.db.kv(coord.NOTES_KEY))
    coord.apply(p, act, user_turn=True)
    assert p.db.kv(selfreview.OFF_KEY)["user"] is True
    coord.apply(p, [{**act[0], "enabled": True}])
    assert p.db.kv(selfreview.OFF_KEY) is None


@pytest.mark.parametrize("status,cause", [
    ("skipped: precheck: nothing new", "its precheck skipped it (nothing new)"),
    ("skipped: last run done 1.0 h ago on unchanged evidence", "the debounce skipped it on unchanged evidence"),
    ("skipped: nothing happened since the last run", "the idle guard skipped it, though work happened"),
    ("deferred: daily budget used", "its own daily budget (budget_usd_day) was used up"),
    ("skipped: budget yellow", "the budget gate held it (yellow)"),
])
def test_skipped_triggers_are_named_and_the_forced_run_passes_the_debounce_idle_guard_and_precheck(env, status, cause):
    from ttp import schedule as sched
    p = make(env)
    d = _sr_daemon(p)
    _sr_receipt(p, 50)
    _sr_work(p)
    row = p.db.one("SELECT * FROM schedules WHERE name='daily-review'")
    payload = {**json.loads(row["payload"]), "precheck": "echo nothing new", "debounce_h": 1000}
    p.db.x("UPDATE schedules SET payload=?, next_run=? WHERE name='daily-review'",
           (json.dumps(payload), time.time() + 86400))
    sched.record_skip(p.db, "daily-review", status)
    _sr_check(d)
    ev = _sr_events(p)
    assert len(ev) == 1 and "1 trigger skipped since; the latest: " + cause in ev[0]["text"], ev
    assert [r["priority"] for r in _sr_runs(p, "queued")] == [2]


def test_the_budget_gate_is_named_not_overridden(env):
    from ttp import budget as bud
    p = make(env)
    d = _sr_daemon(p)
    _sr_receipt(p, 30)
    _sr_work(p)
    prov = d.cfg.get("core_provider") or "claude"
    g = bud.Gate(prov)
    g.level, g.allow_optional = "yellow", False
    d.gates = {prov: g}
    _sr_check(d)
    ev = _sr_events(p)
    assert len(ev) == 1 and "could not queue a run (skipped: budget yellow)" in ev[0]["text"]
    assert not _sr_runs(p, "queued")
    # A run queued earlier and held past the grace: what holds it is named.
    tid = p.db.add_task("[daily-review] r", "s", origin="schedule", labels=["daily-review"])
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (time.time() - 3 * H, tid))
    from ttp import selfreview
    a = selfreview.assess(p.db, time.time(), d.cfg["created"], d._self_review_held)
    assert a["cause"] == f"its run #{tid} is held (the budget gate (yellow) holds scheduled work)"


@pytest.mark.parametrize("summary,status,cause", [
    ("", "done", "ended done without a review receipt (no summary)"),
    ("gave up", "failed", "ended failed"),
])
def test_a_last_run_with_no_receipt_or_a_failed_one_is_overdue(env, summary, status, cause):
    p = make(env)
    d = _sr_daemon(p)
    _sr_receipt(p, 50)
    tid = _sr_receipt(p, 20, summary=summary, status=status)
    _sr_work(p)
    _sr_check(d)
    ev = _sr_events(p)
    assert len(ev) == 1 and f"its last run #{tid} {cause}" in ev[0]["text"] and "last receipt 50 h ago" in ev[0]["text"]
    assert [r["priority"] for r in _sr_runs(p, "queued")] == [2]


def test_a_still_overdue_episode_forces_again_only_after_the_gap_and_rewakes_after_a_day(env, monkeypatch):
    from ttp import selfreview
    p = make(env)
    d = _sr_daemon(p)
    _sr_receipt(p, 30)
    _sr_work(p)
    _sr_check(d)
    first = _sr_runs(p, "queued")[0]["id"]
    p.db.x("UPDATE tasks SET status='failed' WHERE id=?", (first,))
    _sr_check(d)   # failed again within the gap: no new run, no new event
    assert len(_sr_runs(p)) == 2 and len(_sr_events(p)) == 1
    later = time.time() + 27 * H
    monkeypatch.setattr(time, "time", lambda: later)
    _sr_check(d)
    assert len(_sr_runs(p, "queued")) == 1 and len(_sr_events(p)) == 2
    assert f"its last run #{first} ended failed" in _sr_events(p)[-1]["text"]
    assert selfreview.line(p.db, later, d.cfg["created"]).startswith("self-review: run #")


def test_status_and_the_web_header_show_the_self_review_line(env):
    from ttp import web
    p = make(env)
    _sr_receipt(p, 3)
    h = web.health(p, p.db)
    assert h["self_review"] == "self-review: last ran 3 h ago"
    assert "self-review: last ran 3 h ago" in __import__("ttp.cli", fromlist=["x"]).status_text(p)
