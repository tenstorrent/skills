# Throughput review (throughput.py, `ttp throughput`) and the speed-complaint trigger (slowcomplaint.py).
# Run from tests/test_tt_project_runtime.py (see _include_cases there): helpers, fixtures and imports
# come from that file, and pytest collects these tests under it.
from __future__ import annotations

from pathlib import Path


def _tp_task(p, title, created, prio=3, **kw):
    tid = p.db.add_task(title, "s", priority=prio, origin="user", **{k: v for k, v in kw.items() if k != "labels"})
    p.db.x("UPDATE tasks SET created=?, labels=? WHERE id=?", (created, json.dumps(kw.get("labels") or []), tid))
    return tid


def _tp_run(p, task, started, ended=None, role="worker"):
    return p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)",
                  (task, role, started, ended, "done" if ended else "running"))


def _tp_busy_fixture(p, now, slots=2):
    """`slots` runs filling every worker slot from now-3h on, a priority 1 task that waited 40 min behind
    them, two plain tasks queued 2 h ago and still waiting, and one task that started at once."""
    p.set_config("budget.max_parallel_workers", slots)
    for i in range(slots):
        _tp_run(p, _tp_task(p, f"long {i}", now - 3 * 3600), now - 3 * 3600, None)
    urgent = _tp_task(p, "urgent fix", now - 2 * 3600, prio=1)
    _tp_run(p, urgent, now - 2 * 3600 + 2400, now - 3600)
    waiting = [_tp_task(p, f"queued {i}", now - 2 * 3600) for i in range(2)]
    quick = _tp_task(p, "quick", now - 7 * 3600)
    _tp_run(p, quick, now - 7 * 3600 + 5, now - 6 * 3600)
    return urgent, waiting


def _tp_lane(rep, name):
    return next(ln for ln in rep["lanes"] if ln["lane"] == name)


def test_throughput_measures_waits_per_lane_from_task_run_and_push_records(env):
    from ttp import throughput
    p = make(env)
    now = time.time()
    urgent, waiting = _tp_busy_fixture(p, now)
    # A landed:# chain, a device task, a review queued 40 min after its task's run ended, a push row
    # whose batch started 50 min after approval.
    _tp_task(p, "after #5", now - 5 * 3600, labels=["start_when:landed:#5", f"deferred_since:{now - 5 * 3600}"])
    p.set_config("resources", {"box": 1})
    dev = _tp_task(p, "device run", now - 3600, labels=["exclusive:box"])
    code = _tp_task(p, "code change", now - 4 * 3600)
    _tp_run(p, code, now - 4 * 3600, now - 3 * 3600)
    rv = _tp_task(p, "Review #x", now - 3 * 3600 + 2400, labels=[f"auto_review:{code}"])
    p.db.x("UPDATE tasks SET kind='review' WHERE id=?", (rv,))
    _tp_run(p, rv, now - 3 * 3600 + 3000, now - 3 * 3600 + 4000, role="reviewer")
    p.db.x("INSERT INTO push_batches(id, marker, target, started) VALUES('b1','m','br',?)", (now - 3600,))
    p.db.x("INSERT INTO push_queue(task, branch, head, target, batch, created, updated) VALUES(?,?,?,?,?,?,?)",
           (code, "b", "h", "br", "b1", now - 3600 - 3000, now))
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    w = _tp_lane(rep, "worker_slots")
    assert w["limit_key"] == "budget.max_parallel_workers" and w["limit"] == 2
    assert w["waiting_now"] == 2 and w["p1_count"] == 1 and w["p1_p90_s"] == 2400 and w["slow"]
    assert w["at_limit"] == 3 and w["idle"] == 0, "every wait over threshold met all slots busy"
    assert _tp_lane(rep, "landed_chain")["count"] == 1 and _tp_lane(rep, "landed_chain")["p90_s"] >= 5 * 3600 - 1
    assert _tp_lane(rep, "resource:box")["waiting_now"] == 1 and dev
    rs, rt = _tp_lane(rep, "review_sweep"), _tp_lane(rep, "review_stall")
    assert (rs["count"], rs["p90_s"], rt["p90_s"]) == (1, 2400, 3000)
    assert _tp_lane(rep, "push_batch")["p90_s"] == 3000
    assert _tp_lane(rep, "checks_slots")["count"] == 0 and "dependency" not in {ln["lane"] for ln in rep["lanes"]}
    assert rep["lanes"][0]["slow"], "slow lanes come first"
    # A task that started before the window is outside it; the text names lanes and limits.
    assert _tp_lane(throughput.measure(p.db, p.config(), since_s=3600, now=now, gates={}), "worker_slots")["count"] == 2
    txt = throughput.text({**rep, "changes": [], "kept": []})
    assert "worker_slots SLOW" in txt and "limit budget.max_parallel_workers=2" in txt


def test_throughput_apply_raises_only_at_limit_with_headroom_bounded_by_load_and_undoes(env, monkeypatch):
    from ttp import throughput
    p = make(env)
    now = time.time()
    _tp_busy_fixture(p, now)
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    monkeypatch.setattr(throughput.os, "cpu_count", lambda: 64)
    # A spend gate below the slot count, a red gate or a busy CPU: nothing changes.
    prov = p.config()["core_provider"]
    for gates, cpu in (({prov: {"level": "yellow", "allow_new_work": True, "max_parallel": 1}}, True),
                       ({prov: {"level": "red", "allow_new_work": False, "max_parallel": 2}}, True),
                       ({}, False)):
        monkeypatch.setattr(throughput, "cpu_free", lambda cpu=cpu: (cpu, "" if cpu else "host load high"))
        rep = throughput.review(p, p.config(), do_apply=True, gates=gates, now=now)
        assert rep["changes"] == [] and p.config()["budget"]["max_parallel_workers"] == 2, rep["kept"]
        assert any(k["lane"] == "worker_slots" for k in rep["kept"])
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    rep = throughput.review(p, p.config(), do_apply=True, gates={}, trigger="t", now=now)
    (c,) = [c for c in rep["changes"] if c["key"] == "budget.max_parallel_workers"]
    # Step from load: 3 waits at the limit, at most doubling: 2 -> 4.
    assert (c["old"], c["new"]) == (2, 4) and p.config()["budget"]["max_parallel_workers"] == 4
    assert c["undo"] == f"ttp throughput --undo {c['id']}" and "while all 2 slots ran" in c["why"]
    assert "budget.max_parallel_workers 2->4" in throughput.status_line(p.db)
    # Never above the host's share of cores.
    monkeypatch.setattr(throughput.os, "cpu_count", lambda: 6)
    assert throughput.load_size(3, 10, 3) == 3 and throughput.load_size(2, 10, 3) == 3
    assert throughput.load_size(4, 1, 100) == 5 and throughput.load_size(4, 50, 100) == 8
    # Undo: back to the old value, once; not over a later hand edit.
    assert throughput.undo(p, c["id"]).endswith("back to 2") and p.config()["budget"]["max_parallel_workers"] == 2
    assert "already undone" in throughput.undo(p, c["id"])
    rep = throughput.review(p, p.config(), do_apply=True, gates={}, now=now)
    (c2,) = [c for c in rep["changes"] if c["key"] == "budget.max_parallel_workers"]
    p.set_config("budget.max_parallel_workers", 9)
    assert "left as it is" in throughput.undo(p, c2["id"]) and p.config()["budget"]["max_parallel_workers"] == 9
    assert "no throughput change" in throughput.undo(p, 999)
    # A second review lists the earlier changes.
    assert {e["id"] for e in throughput.review(p, p.config(), gates={}, now=now)["earlier_changes"]} >= {c["id"], c2["id"]}


def test_throughput_worker_raise_moves_no_spend_limit(env, monkeypatch):
    from ttp import budget as bud, throughput
    p = make(env)
    now = time.time()
    _tp_busy_fixture(p, now)
    prov = p.config()["core_provider"]
    assert not p.config()["budget"].get("hourly_floor_usd"), "the floor is sized from the worker count"
    # Four failed runs at once in the last hour: the waste cap is sized per worker, up to the slot count.
    for i in range(4):
        t = _tp_task(p, f"failing {i}", now - 1800)
        p.db.x("INSERT INTO runs(task, role, provider, started, ended, status, cost_usd) VALUES(?,?,?,?,?,?,?)",
               (t, "worker", prov, now - 1800, now - 600, "failed", 1.0))
    before = bud.evaluate(p.db, p.config(), prov, [], now=now).numbers
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    monkeypatch.setattr(throughput.os, "cpu_count", lambda: 64)
    rep = throughput.review(p, p.config(), do_apply=True, gates={}, now=now)
    (c,) = [c for c in rep["changes"] if c["key"] == "budget.max_parallel_workers"]
    assert c["new"] > c["old"] == 2 and p.config()["budget"]["max_parallel_workers"] == c["new"]
    after = bud.evaluate(p.db, p.config(), prov, [], now=now).numbers
    assert (after["hourly_ceiling"], after["waste_limit"]) == (before["hourly_ceiling"], before["waste_limit"])
    assert throughput.spend_workers(p.db, c["new"]) == 2
    # A hand edit after the raise is the user's own count; an undo ends the pin; old records stay pinned.
    assert throughput.spend_workers(p.db, 7) == 7
    p.db.set_kv(throughput.KV, [{**r, "ts": now - 30 * 86400} for r in p.db.kv(throughput.KV)])
    throughput.apply(p, p.config(), {"lanes": [], "thresholds_s": {"p1": 600, "other": 1800}}, now=now)
    assert throughput.spend_workers(p.db, c["new"]) == 2
    throughput.undo(p, c["id"])
    assert throughput.spend_workers(p.db, 2) == 2 and throughput.spend_workers(p.db, c["new"]) == c["new"]


def test_throughput_dependency_waits_are_their_own_lane_and_raise_no_workers(env, monkeypatch):
    from ttp import throughput
    p = make(env)
    now = time.time()
    p.set_config("budget.max_parallel_workers", 1)
    _tp_run(p, _tp_task(p, "long", now - 3 * 3600), now - 3 * 3600, None)
    # A code task held by a deferral and a priority 1 review queued behind it for 2 h.
    code = _tp_task(p, "code", now - 2 * 3600, labels=["start_when:landed:#5", f"deferred_since:{now - 2 * 3600}"])
    rv = _tp_task(p, "Review", now - 2 * 3600, prio=1, depends_on=[code])
    p.db.x("UPDATE tasks SET kind='review' WHERE id=?", (rv,))
    # A task whose dependency was done 1 h ago and that started a minute later waited a minute.
    dep = _tp_task(p, "dep", now - 4 * 3600)
    _tp_run(p, dep, now - 4 * 3600 + 5, now - 3 * 3600)
    p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (now - 3600, dep))
    later = _tp_task(p, "later", now - 4 * 3600, prio=1, depends_on=[dep])
    _tp_run(p, later, now - 3600 + 60, now - 1800)
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    d = _tp_lane(rep, "dependency")
    assert (d["count"], d["waiting_now"], d["limit_key"]) == (1, 1, None) and d["p1_p90_s"] >= 2 * 3600 - 1
    w = _tp_lane(rep, "worker_slots")
    assert w["p1_count"] == 1 and w["p1_p90_s"] == 60 and w["waiting_now"] == 0 and not w["slow"]
    changes, _ = throughput.plan(p.db, p.config(), rep)
    assert not [c for c in changes if c["key"] == "budget.max_parallel_workers"]


def test_throughput_measures_requeue_waits_by_reason_and_a_slow_checks_lane_gets_a_p1_followup(env, monkeypatch):
    from ttp import slowcomplaint, throughput
    p = make(env)
    now = time.time()
    checks = "waiting for ttp checks: waiting (pid 5, not running) for this project's other full checks to end"

    def requeued(title, gap_end, status, reason, wait_for=None, data=True, ended=now - 4 * 3600):
        # As the daemon records it: the run ends 'ok' (finish_run's process outcome), the waiting hand-off
        # is a task_waiting event and the task's status and blocked_reason.
        t = _tp_task(p, title, ended - 3600, prio=1)
        p.db.x("INSERT INTO runs(task, role, started, ended, status, note) VALUES(?,?,?,?,?,?)",
               (t, "worker", ended - 3600, ended, "ok", json.dumps({"spec_sha": "x", "pick": {}})))
        w = reason if wait_for is None else wait_for
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
               (ended + 1, f"task:{t}", "task_waiting", "low", f"#{t} {title}: {w}",
                json.dumps({"wait": "self", "why": "hand-off", "for": w} if data else {}), "handled", t))
        if gap_end:
            _tp_run(p, t, gap_end, gap_end + 600)
        p.db.x("UPDATE tasks SET status=?, blocked_reason=? WHERE id=?", (status, reason, t))
        return t

    requeued("ran again", now - 2 * 3600, "done", "", wait_for=checks)
    requeued("still queued", None, "queued", checks)
    requeued("own job", None, "waiting", "own ttp checks --detach running")
    requeued("retry", None, "waiting", "CI on the branch")
    requeued("old done", None, "done", checks)
    # The title names checks, the wait does not: the "#<id> <title>: " prefix is not the reason.
    requeued("ttp checks lanes", now - 3 * 3600, "done", "", wait_for="CI on the branch", data=False)
    # A task that ran three days ago and waits again: a re-queue wait, not a first start for a slot.
    requeued("ran long ago", None, "queued", "", ended=now - 3 * 86400)
    # Read as its own checks, but the checks slot's records show the slot held it.
    ev = requeued("slot event", now - 2 * 3600, "done", "", wait_for="the detached ttp checks to pass")
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
           (now - 3 * 3600, "daemon", "checks_slot_wait", "low", f"Checks slot wait: #{ev} has waited 15 min", "handled", ev))
    lg = requeued("slot log", now - 2 * 3600, "done", "", wait_for="the detached ttp checks to pass")
    from ttp import checkcap
    (Path(p.db.path).parent / checkcap.WAITS_LOG).write_text(json.dumps(
        {"task": lg, "queued_at": now - 4 * 3600 + 60, "granted_at": now - 2 * 3600 - 60, "slot": "queue"}) + "\n")
    short = requeued("short slot", now - 2 * 3600, "done", "", wait_for="the detached ttp checks to pass")
    with open(Path(p.db.path).parent / checkcap.WAITS_LOG, "a") as fh:   # 10 of 120 min: its own job held it
        fh.write(json.dumps({"task": short, "queued_at": now - 3 * 3600, "granted_at": now - 3 * 3600 + 600}) + "\n")
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    ck = _tp_lane(rep, "checks_slots")
    assert (ck["count"], ck["waiting_now"], ck["p1_p90_s"]) == (4, 1, 4 * 3600) and ck["slow"]
    assert (_tp_lane(rep, "own_job")["count"], _tp_lane(rep, "retry_wait")["count"]) == (2, 3)
    assert _tp_lane(rep, "retry_wait")["waiting_now"] == 2
    assert "worker_slots" not in {ln["lane"] for ln in rep["lanes"] if ln["count"] and ln["lane"] != "dependency"} \
        or _tp_lane(rep, "worker_slots")["waiting_now"] == 0
    _, kept = throughput.plan(p.db, p.config(), rep)
    assert any(k["lane"] == "checks_slots" for k in kept)
    (fid,) = [t for t in slowcomplaint._followups(p.db, {**rep, "kept": kept}, [1])
              if "checks_slots" in p.db.one("SELECT title FROM tasks WHERE id=?", (t,))["title"]]
    assert p.db.one("SELECT priority FROM tasks WHERE id=?", (fid,))["priority"] == 1


def test_throughput_keeps_push_window_and_sweep_unless_they_held_the_waits(env):
    from ttp import throughput
    p = make(env)
    now = time.time()
    p.set_config("delivery.push_batch_s", 3600)
    # A row that waited 50 min while another batch ran: the batch held it, not the window.
    code = _tp_task(p, "code change", now - 4 * 3600)
    _tp_run(p, code, now - 4 * 3600, now - 4 * 3600 + 600)
    p.db.x("INSERT INTO push_batches(id, marker, target, started, ended) VALUES('b0','m','br',?,?)",
           (now - 3 * 3600, now - 3600 - 60))
    p.db.x("INSERT INTO push_batches(id, marker, target, started) VALUES('b1','m','br',?)", (now - 3600,))
    p.db.x("INSERT INTO push_queue(task, branch, head, target, batch, created, updated) VALUES(?,?,?,?,?,?,?)",
           (code, "b", "h", "br", "b1", now - 3600 - 3000, now))
    # A review queued at finish, 40 min after the work ended (not by the sweep).
    rv = _tp_task(p, "Review", now - 4 * 3600 + 3000, labels=[f"auto_review:{code}"])
    p.db.x("UPDATE tasks SET kind='review' WHERE id=?", (rv,))
    _tp_run(p, rv, now - 4 * 3600 + 3100, now - 4 * 3600 + 3600, role="reviewer")
    p.set_config("review.sweep_after_s", 1200)
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    assert _tp_lane(rep, "push_batch")["slow"] and _tp_lane(rep, "review_sweep")["slow"]
    levers = ("delivery.push_batch_s", "review.sweep_after_s")
    changes, kept = throughput.plan(p.db, p.config(), rep)
    assert not [c for c in changes if c["key"] in levers], changes
    why = {k["lane"]: k["why"] for k in kept}
    assert "1 of 1 row(s) waited behind a running batch" in why["push_batch"]
    assert "1 of 1 review(s) were queued at finish, not by the sweep" in why["review_sweep"]
    # The window alone held the row, and the sweep queued the review: both levers change.
    p.db.x("DELETE FROM push_batches WHERE id='b0'")
    p.db.x("INSERT INTO events(ts, source, kind, severity, text, status, task) VALUES(?,?,?,?,?,?,?)",
           (now - 4 * 3600 + 3000, "daemon", "review_swept", "normal", f"#{code} x; review #{rv} queued by the "
            "daemon.", "handled", code))
    changes, _ = throughput.plan(p.db, p.config(), throughput.measure(p.db, p.config(), now=now, gates={}))
    assert {(c["key"], c["new"]) for c in changes if c["key"] in levers} == {("delivery.push_batch_s", 1800), ("review.sweep_after_s", 600)}


def test_throughput_idle_slots_are_not_raised_and_resource_lanes_are_only_reported(env, monkeypatch):
    from ttp import throughput
    p = make(env)
    now = time.time()
    p.set_config("budget.max_parallel_workers", 6)
    p.set_config("resources", {"box": 1})
    _tp_task(p, "waits idle", now - 2 * 3600)       # nothing ran: slots sat idle
    _tp_task(p, "box job", now - 2 * 3600, labels=["resource:box"])
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    rep = throughput.review(p, p.config(), do_apply=True, gates={}, now=now)
    assert rep["changes"] == [] and _tp_lane(rep, "worker_slots")["idle"] == 1
    kept = {k["lane"]: k["why"] for k in rep["kept"]}
    assert "idle" in kept["worker_slots"] and "not measurable" in kept["resource:box"]
    assert p.config()["resources"] == {"box": 1}


def test_throughput_never_changes_spend_lock_pause_or_restriction_keys(env, monkeypatch):
    from ttp import throughput
    from ttp.coordinator import USER_SETTABLE, settable
    for key in ("budget.daily_usd", "budget.weekly_usd", "budget.global_daily_usd", "budget.reserve_pct",
                "delivery.push_queue", "review.auto", "locks.x", "resources.box", "coordinator.provider_failover_to",
                "checks.test_project_max", "spend_cap", "delivery.never_force", "restrictions"):
        assert not throughput.safe(key), key
    for key in throughput.LEVERS:
        assert throughput.safe(key) and key in USER_SETTABLE and settable(key), key
    p = make(env)
    # plan() offers only safe keys, whatever the lanes say.
    before = p.config()["budget"].get("daily_usd")
    _tp_busy_fixture(p, time.time())
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    rep = throughput.measure(p.db, p.config(), gates={})
    changes, _ = throughput.plan(p.db, p.config(), rep)
    assert changes and all(throughput.safe(c["key"]) for c in changes)
    assert p.config()["budget"].get("daily_usd") == before
    # The levers a coordinator may set are listed by `ttp config --chat-settable list`.
    r = _ttp_run(p, "config", "--chat-settable", "list")
    assert all(k in r.stdout.split() for k in throughput.LEVERS), r.stdout
    assert _ttp_run(p, "config", "--chat-settable", "review.unreviewed_per_pass", "-1").returncode == 1


def test_speed_complaint_detector_matches_complaints_not_plain_messages():
    from ttp import slowcomplaint as sc
    for text in ("This is so slow", "Why is everything stuck?", "Fix your throughput asap", "tasks sit in the queue",
                 "It's taking forever", "the review is still not done after a day", "Again nothing for hours",
                 "speed up the reviews", "I yelled at it for being slow"):
        assert sc.detect(text), text
    for text in ("Approved, go ahead", "Queue a task for the docs", "What is the status of the build?",
                 "Thanks, looks good", "Use the slowtests report", ""):
        assert not sc.detect(text), text


def test_speed_complaint_on_the_turn_start_runs_the_review_applies_and_queues_p1_followups(env, monkeypatch):
    from ttp import throughput
    from ttp.daemon import Daemon
    p = make(env)
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    now = time.time()
    _tp_busy_fixture(p, now)
    _tp_task(p, "after #5", now - 5 * 3600, labels=["start_when:landed:#5", f"deferred_since:{now - 5 * 3600}"])
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    d = Daemon(p.base)
    d.update_gates()
    calls = []
    monkeypatch.setattr(d, "start_run", lambda role, prompt, *a, **k: calls.append((prompt, k)) or 1)
    mid = p.db.post("in", "Everything is so slow, fix your throughput asap", chat="c1")
    later = time.time() + 600   # past the debounce
    monkeypatch.setattr(time, "time", lambda: later)
    d.maybe_coordinate()
    prompt = calls[-1][0]
    assert f"Speed complaint: measured throughput review (speed complaint #{mid}" in prompt
    assert "worker_slots SLOW" in prompt and "Changed #" in prompt and "budget.max_parallel_workers" in prompt
    fu = p.db.q("SELECT * FROM tasks WHERE title LIKE 'throughput: %'")
    assert [t["priority"] for t in fu] == [1] and "landed_chain" in fu[0]["title"] and "urgent:" in fu[0]["spec"]
    # A second complaint the same day runs it again, lists the earlier change, queues no second task.
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    p.db.post("in", "Still waiting for hours again", chat="c1")
    monkeypatch.setattr(time, "time", lambda: later + 900)
    d.maybe_coordinate()
    assert "Earlier (last 24 h) #" in calls[-1][0]
    assert len(p.db.q("SELECT * FROM tasks WHERE title LIKE 'throughput: %'")) == 1
    # A plain message adds no section.
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    p.db.post("in", "Thanks, approved", chat="c1")
    monkeypatch.setattr(time, "time", lambda: later + 1800)
    d.maybe_coordinate()
    assert "Speed complaint" not in calls[-1][0]


def test_ttp_throughput_cli_prints_text_and_json(env):
    p = make(env)
    _tp_busy_fixture(p, time.time())
    r = _ttp_run(p, "throughput")
    assert "Throughput over the last 24h00m" in r.stdout and "worker_slots" in r.stdout
    data = json.loads(_ttp_run(p, "throughput", "--json", "--since", "6h").stdout)
    assert data["since_s"] == 6 * 3600 and {"lanes", "thresholds_s", "changes", "kept", "earlier_changes"} <= set(data)
    assert {"lane", "limit_key", "limit", "count", "median_s", "p90_s", "p1_p90_s", "at_limit", "idle",
            "waiting_now", "slow"} <= set(data["lanes"][0])
    assert _ttp_run(p, "throughput", "--since", "soon").returncode != 0
