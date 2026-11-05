# Queue trim tests of tests/test_tt_project_runtime.py (queuetrim.py: the stale queued tail expires,
# repeated status and report tasks are merged or refused, the digest shows the backlog against
# throughput), which runs this file in its own namespace (see _include_cases there): helpers, fixtures
# and imports come from that file.
from __future__ import annotations


def _qt_aged(p, title, hours, spec="do the thing", **kw):
    tid = p.db.add_task(title, spec, **kw)
    p.db.x("UPDATE tasks SET created=?, updated=? WHERE id=?",
           (time.time() - hours * 3600, time.time() - hours * 3600, tid))
    return tid


def test_queuetrim_expires_only_the_stale_never_started_tail_with_reason_and_undo(env):
    from ttp import queuetrim
    p = make(env)
    db = p.db
    stale = _qt_aged(p, "tidy the old notes", 50, kind="work")
    fresh = _qt_aged(p, "tidy the new notes", 10, kind="work")
    urgent = _qt_aged(p, "fix the outage", 50, priority=1)
    urgent_line = _qt_aged(p, "fix the other outage", 50, spec="urgent: users are down")
    by_user = _qt_aged(p, "the user's own task", 50, origin="user")
    cites = _qt_aged(p, "page rows (user msg #12)", 50)
    review = _qt_aged(p, "Review #1: something", 50, kind="review")
    ran = _qt_aged(p, "it ran once and waits", 50)
    db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?, 'worker', ?, ?, 'ok')",
         (ran, time.time() - 3600, time.time() - 3000))
    live = _qt_aged(p, "live dependency", 1)
    db.update_task(live, status="running")
    waits = _qt_aged(p, "waits on live work", 50, depends_on=[live])
    deferred = _qt_aged(p, "deferred on a probe", 50, labels=["start_when:test -e /nonexistent"])
    lines = queuetrim.expire(db, p.config(), p.name)
    assert lines == [f"task {stale} expired: queued 50 h without starting (queue.expire_h 48)"], lines
    t = db.task(stale)
    assert t["status"] == "cancelled"
    assert t["blocked_reason"] == (f"expired: queued 50 h without starting (queue.expire_h 48); "
                                   f"undo: ttp task {p.name} requeue {stale}")
    for kept in (fresh, urgent, urgent_line, by_user, cites, review, ran, waits, deferred):
        assert db.task(kept)["status"] == "queued", (kept, db.task(kept)["title"])
    ev = db.one("SELECT * FROM events WHERE kind='task_expired'")
    assert ev["task"] == stale and ev["status"] == "handled"
    # Run twice: nothing more, one record.
    assert queuetrim.expire(db, p.config(), p.name) == []
    assert [r["task"] for r in db.kv(queuetrim.KEY)] == [stale]
    # The undo puts it back in the queue and clears the reason.
    assert db.requeue(stale) == ""
    assert db.task(stale)["status"] == "queued" and db.task(stale)["blocked_reason"] is None
    # 0 turns it off.
    p.set_config("queue.expire_h", 0)
    p.set_config("queue.dead_dep_h", 0)
    assert queuetrim.expire(db, p.config(), p.name) == []


def test_queuetrim_expires_a_task_blocked_on_a_dead_dependency_after_the_grace(env):
    from ttp import queuetrim
    p = make(env)
    db = p.db
    dead = _qt_aged(p, "dead dependency", 30)
    db.update_task(dead, status="cancelled")
    old = _qt_aged(p, "held on the dead one", 30, depends_on=[dead])
    db.x("UPDATE tasks SET status='blocked', blocked_reason=? WHERE id=?", (f"dependency #{dead} cancelled", old))
    new = _qt_aged(p, "held a short while", 2, depends_on=[dead])
    db.x("UPDATE tasks SET status='blocked', blocked_reason=? WHERE id=?", (f"dependency #{dead} failed", new))
    asked = _qt_aged(p, "blocked on a question", 30)
    db.x("UPDATE tasks SET status='blocked', blocked_reason='needs the user to choose' WHERE id=?", (asked,))
    urgent = _qt_aged(p, "urgent held one", 30, priority=1, depends_on=[dead])
    db.x("UPDATE tasks SET status='blocked', blocked_reason=? WHERE id=?", (f"dependency #{dead} cancelled", urgent))
    lines = queuetrim.expire(db, p.config(), p.name)
    assert lines == [f"task {old} expired: blocked 30 h on dependency #{dead} cancelled"], lines
    assert [db.task(t)["status"] for t in (new, asked, urgent)] == ["blocked"] * 3
    # A task changed since the scan (started, requeued) keeps its state: the update is conditional.
    other = _qt_aged(p, "another stale one", 60)
    cands = queuetrim.candidates(db, p.config())
    assert [t["id"] for t, _ in cands] == [other]
    db.update_task(other, status="running")
    assert queuetrim.expire(db, p.config(), p.name) == []


def test_queuetrim_daily_review_lists_each_expired_task_once(env):
    from ttp import queuetrim
    p = make(env)
    tid = _qt_aged(p, "an old idea", 72)
    queuetrim.expire(p.db, p.config(), p.name)
    first = queuetrim.review_lines(p.db)
    assert len(first) == 1 and first[0].startswith(f"#{tid} an old idea: queued 72 h")
    assert f"`ttp task {p.name} requeue {tid}`" in first[0]
    assert queuetrim.review_lines(p.db) == []


def test_queuetrim_digest_shows_backlog_against_throughput_and_expiries(env):
    from ttp import queuetrim
    p = make(env)
    db = p.db
    now = time.time()
    for i in range(3):   # 3 done in 3 days: 1 per day
        t = db.add_task(f"done {i}", "x")
        db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (now - 3600 * (i + 1), t))
    assert queuetrim.digest_lines(db, now - 60, now) == []   # nothing queued
    ids = [db.add_task(f"queued {i}", "x") for i in range(4)]
    db.update_task(ids[0], priority=1)
    lines = queuetrim.digest_lines(db, now - 60, now)
    assert lines[0].startswith("## Queued backlog")
    assert lines[1] == "- 4 queued (1 urgent) against 1.0 done per day over 3 days: 4.0 days to clear at that rate."
    assert lines[2] == "- Oldest non-urgent: " + ", ".join(f"#{i}" for i in ids[1:])
    # Nothing done in 3 days: no end.
    db.x("UPDATE tasks SET updated=? WHERE status='done'", (now - 5 * 86400,))
    assert "no end (nothing done in 3 days)" in queuetrim.digest_lines(db, now - 60, now)[1]
    # The digest carries the section.
    from ttp import coordinator as coord
    parts, _ = coord.digest_parts(p, {}, [], [])
    assert any(k == "queue_backlog" for k, _ in parts)


def test_queuetrim_repeated_report_task_is_merged_refused_or_forced(env):
    from ttp import coordinator as coord, queuetrim
    p = make(env)
    db = p.db

    def add(title, spec, **kw):
        return coord.apply(p, [{"type": "task_add", "title": title, "spec": spec, "kind": "work", **kw}])

    def count():
        return db.one("SELECT COUNT(*) n FROM tasks")["n"]

    q = db.add_task("Soak run status report", "Read the soak logs and post the table.", kind="work")
    # A reworded request with a different spec: merged into the queued one.
    got = add("Status of the soak run (user msg #40)", "Collect pass and fail counts per host since 14:00.")
    assert len(got) == 1 and got[0].startswith(f"task_add: merged into queued task #{q}"), got
    assert count() == 1 and "pass and fail counts per host" in db.task(q)["spec"]
    # Running: refused, naming it.
    db.update_task(q, status="running")
    got = add("soak run summary today", "Summarise failures by host.")
    assert got and f"task #{q} (running)" in got[0] and coord.INFO_MARK in got[0], got
    # A user-ordered or urgent request is never refused.
    assert add("soak run summary (user msg #41)", "Add the restart column.") == [] and count() == 2
    db.update_task(db.one("SELECT MAX(id) id FROM tasks")["id"], status="done")
    assert add("soak run summary for the outage", "Count the outage hosts.", priority=1) == [] and count() == 3
    db.x("DELETE FROM tasks WHERE id!=?", (q,))
    # Done 2 h ago: still covered; done 30 h ago: free again.
    db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (time.time() - 7200, q))
    assert add("Check on the soak run", "Look at the latest soak numbers.") and count() == 1
    # force without a reason does not pass; with one it does.
    assert add("Check on the soak run", "Look at the latest soak numbers.", force=True) and count() == 1
    assert add("Check on the soak run", "Look at the latest soak numbers.", force=True,
               reason="the soak restarted after the fix") == []
    assert count() == 2
    db.x("UPDATE tasks SET status='done', updated=? WHERE status!='done' OR id=?", (time.time() - 30 * 3600, q))
    db.x("UPDATE tasks SET updated=? WHERE status='done'", (time.time() - 30 * 3600,))
    assert add("soak run status", "Post the soak table again.") == [] and count() == 3
    # Another target, another kind, a task that is no report, a continue hop: not covered.
    assert queuetrim.covering(db, "power table status", "work") is None
    assert queuetrim.covering(db, "soak run status", "code") is None
    assert queuetrim.covering(db, "fix the soak run", "work") is None
    # A `continue:` title is no way around it; a real continue hop (task_add continues) is.
    assert queuetrim.covering(db, "continue: soak run status", "work")["status"] == "queued"
    prep = db.add_task("soak prep", "Set up the soak hosts.", kind="work")
    db.update_task(prep, status="done")
    assert add("continue: soak run status", "Finish the soak table.", continues=prep) == []
    assert queuetrim.covering(db, "soak run status", "work")["status"] == "queued"


def test_queuetrim_targets_ignore_message_refs_dates_and_report_words(env):
    from ttp import queuetrim
    assert queuetrim.same_target("TYO2 page (user msg #5642): read every result", "TYO2 page rows (owner msg #5726)")
    assert queuetrim.same_target("daily status report 2026-11-04", "Status report")
    assert not queuetrim.same_target("status of #9036", "status of #9040")
    assert not queuetrim.same_target("TYO2 page rows", "TYO2 page consistency")
    assert queuetrim.is_report("[daily-review] summary of the day")
    assert not queuetrim.is_report("fix the parser: it drops the status field")


def test_queuetrim_pauses_hold_work_without_ageing_it(env):
    from ttp import projpause, queuetrim
    p = make(env)
    db = p.db
    held = _qt_aged(p, "uses the paused box", 60, labels=["resource:box1"])
    db.set_kv("paused_resources", {"box1": {"reason": "the user's hold", "since": time.time() - 3600}})
    plain = _qt_aged(p, "plain old task", 60)
    # The project is paused: nothing expires.
    projpause.pause(db, "user", "a break")
    db.set_kv("paused", True)   # the flag `ttp pause` sets beside the record
    assert queuetrim.candidates(db, p.config()) == []
    # Resumed after a 30 h pause inside its life: 30 h of age, not 60, so it waits; the held one waits too.
    db.x("DELETE FROM events WHERE kind='project_pause'")
    db.set_kv("paused", False)
    now = time.time()
    db.x("INSERT INTO events(ts,source,kind,severity,text,status,data) VALUES(?,?,?,?,?,?,?)",
         (now - 3600, "harness", "project_pause", "low", "resumed", "handled",
          json.dumps({"action": "resume", "paused": {"since": now - 31 * 3600}})))
    assert queuetrim.candidates(db, p.config(), now) == []
    db.x("UPDATE tasks SET created=? WHERE id=?", (now - 80 * 3600, plain))
    assert [t["id"] for t, _ in queuetrim.candidates(db, p.config(), now)] == [plain]
    assert db.task(held)["status"] == "queued"
