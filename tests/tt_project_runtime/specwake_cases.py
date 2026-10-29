# Spec-change wake tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new tests of a waiting task woken by a spec change (Daemon._spec_wake) here.
from __future__ import annotations


def _spec_sleeper(p, probe="exit 1", labels=None, depends_on=None, status="queued"):
    """A task asleep for an hour on a `waiting` hand-off with `probe`, which the daemon finished on its
    current spec (so its hand-off keeps that spec's digest)."""
    from ttp.prompts import spec_digest
    now = time.time()
    tid = p.db.add_task("run the long job", "start the job and report", kind="work", tier="standard",
                        origin="user", labels=labels, depends_on=depends_on)
    hand = {"status": "waiting", "summary": "job running", "waiting_for": "the job", "retry_after_s": 3600,
            "retry_when": probe, "waiting_since": now - 60, "spec_seen": spec_digest(p.db.task(tid))}
    p.db.update_task(tid, status=status, not_before=now + 3600, result=json.dumps(hand))
    return tid


def _edit_spec(p, tid, text):
    from ttp import coordinator as coord
    assert coord.apply(p, [{"type": "task_update", "id": tid, "spec": text}]) == []


def _spec_daemon(p, monkeypatch):
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    monkeypatch.setattr(d, "_start_probe", lambda *a, **k: None)   # its probe never passes here
    return d


def _asleep(p, tid):
    return (p.db.task(tid)["not_before"] or 0) > time.time() + 600


def test_a_spec_change_wakes_a_task_asleep_on_its_retry_when_once(env, monkeypatch):
    p = make(env)
    tid = _spec_sleeper(p)
    d = _spec_daemon(p, monkeypatch)
    d.probe_waiting()
    assert _asleep(p, tid), "no spec change: it stays asleep"
    _edit_spec(p, tid, "also report the peak memory")
    _edit_spec(p, tid, "and the wall time")   # two edits between ticks: one wake
    d.probe_waiting()
    t = p.db.task(tid)
    res = json.loads(t["result"])
    assert t["status"] == "queued" and t["not_before"] <= time.time(), "the spec change did not wake it"
    assert res["woke"].startswith("its spec changed") and len(res["spec_wakes"]) == 1
    assert t["blocked_reason"].startswith("woken: its spec changed")
    from ttp.prompts import spec_digest
    assert res["spec_seen"] == spec_digest(t)
    # Put back to sleep without a run (as a wake-gate skip would): the same spec wakes it no more.
    p.db.update_task(tid, not_before=time.time() + 3600, result=json.dumps({**res, "woke": ""}))
    for _ in range(3):
        d.probe_waiting()
    assert _asleep(p, tid) and len(json.loads(p.db.task(tid)["result"])["spec_wakes"]) == 1


def test_a_task_woken_by_a_spec_change_that_sleeps_again_on_that_spec_stays_asleep(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 3600)   # its probe ran once; ticks here only check the spec
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps(
        {"status": "waiting", "summary": "job running", "waiting_for": "the job", "retry_after_s": 3600,
         "retry_when": "exit 1", "spec_seen": "a worker cannot set this", "spec_wakes": []}))
    tid = p.db.add_task("run the long job", "start the job", kind="work", tier="light", origin="user")
    d = dmod.Daemon(p.base)
    runs = lambda: len(p.db.q("SELECT id FROM runs WHERE task=?", (tid,)))
    idle = lambda: p.db.task(tid)["status"] == "queued" and (p.db.task(tid)["not_before"] or 0) > time.time() \
        and not p.db.q("SELECT id FROM runs WHERE status='running'")
    assert _run_until(d, p, idle) and runs() == 1
    from ttp.prompts import spec_digest
    assert json.loads(p.db.task(tid)["result"])["spec_seen"] == spec_digest(p.db.task(tid)), \
        "the hand-off keeps the digest of the spec it slept on, not the worker's"
    for _ in range(4):
        d.tick()
    assert runs() == 1, "no spec change: no run"
    _edit_spec(p, tid, "also report the peak memory")
    assert _run_until(d, p, lambda: runs() == 2 and idle()), "the spec change did not start a run"
    res = json.loads(p.db.task(tid)["result"])
    assert res["spec_seen"] == spec_digest(p.db.task(tid)) and len(res["spec_wakes"]) == 1, res
    for _ in range(6):
        d.tick()
        time.sleep(0.05)
    assert runs() == 2, "a worker that slept again on the same spec was woken again"


def test_a_spec_change_does_not_wake_a_task_another_hold_keeps(env, monkeypatch):
    p = make(env)
    from ttp import coordinator as coord
    dep = p.db.add_task("build first", "s", kind="work", tier="light", origin="user", not_before=time.time() + 9999)
    held = {
        "an unmet dependency": _spec_sleeper(p, depends_on=[dep]),
        "a blocked waits_on": _spec_sleeper(p, labels=["waits:when:exit 1"], status="blocked"),
        "a resource pause": _spec_sleeper(p, labels=["resource:board"]),
        "a start_when deferral": _spec_sleeper(p, labels=["start_when:exit 1"]),
        "a start_after deferral": _spec_sleeper(p, labels=[f"start_after:{time.time() + 7200}"]),
        "a busy resource": _spec_sleeper(p, probe="ttp lock --probe board"),
    }
    free = _spec_sleeper(p)   # the control: held by nothing but its probe
    coord.pause_resource(p, "board", True, reason="maintenance")
    d = _spec_daemon(p, monkeypatch)
    before = {why: p.db.task(tid)["not_before"] for why, tid in held.items()}
    for tid in [*held.values(), free]:
        _edit_spec(p, tid, "also report the peak memory")
    for _ in range(2):
        d.probe_waiting()
    assert not _asleep(p, free), "the control task was not woken"
    for why, tid in held.items():
        t = p.db.task(tid)
        assert t["not_before"] == before[why] and not json.loads(t["result"]).get("woke"), f"woken through {why}"


def test_a_spec_change_wake_waits_for_the_hold_to_end(env, monkeypatch):
    p = make(env)
    from ttp import coordinator as coord
    tid = _spec_sleeper(p, labels=["resource:board"])
    coord.pause_resource(p, "board", True, reason="maintenance")
    d = _spec_daemon(p, monkeypatch)
    _edit_spec(p, tid, "also report the peak memory")
    d.probe_waiting()
    assert _asleep(p, tid)
    coord.pause_resource(p, "board", False)
    p.db.update_task(tid, not_before=time.time() + 3600, result=json.dumps(
        {k: v for k, v in json.loads(p.db.task(tid)["result"]).items() if k != "woke"}))
    d.probe_waiting()
    assert not _asleep(p, tid), "the spec changed while held: it wakes once the hold is over"


def test_a_workers_own_note_or_status_does_not_wake_its_waiting_task(env, monkeypatch):
    p = make(env)
    tid = _spec_sleeper(p)
    d = _spec_daemon(p, monkeypatch)
    res = json.loads(p.db.task(tid)["result"])
    p.db.update_task(tid, blocked_reason="waiting for the job; 40% done",
                     result=json.dumps({**res, "summary": "job at 40%", "retry_when": "exit 1 # still running"}))
    p.db.x("INSERT INTO runs(task,role,provider,started,status,note) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "ok", json.dumps({"progress": "40%"})))
    for _ in range(3):
        d.probe_waiting()
    assert _asleep(p, tid) and not json.loads(p.db.task(tid)["result"]).get("woke")
    _edit_spec(p, tid, "also report the peak memory")   # the control: an edit from outside the worker does
    d.probe_waiting()
    assert not _asleep(p, tid)


def test_spec_change_wakes_of_one_task_are_capped_per_hour(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    tid = _spec_sleeper(p)
    d = _spec_daemon(p, monkeypatch)
    for i in range(dmod.SPEC_WAKES_PER_H + 2):
        _edit_spec(p, tid, f"edit {i}")
        d.probe_waiting()
        woke = not _asleep(p, tid)
        assert woke == (i < dmod.SPEC_WAKES_PER_H), (i, woke)
        res = json.loads(p.db.task(tid)["result"])
        p.db.update_task(tid, not_before=time.time() + 3600, result=json.dumps({**res, "woke": ""}))
    assert len(json.loads(p.db.task(tid)["result"])["spec_wakes"]) == dmod.SPEC_WAKES_PER_H
    # An hour on, the guard lets the change that waited through it wake the task.
    res = json.loads(p.db.task(tid)["result"])
    p.db.update_task(tid, result=json.dumps({**res, "spec_wakes": [x - 3601 for x in res["spec_wakes"]]}))
    d.probe_waiting()
    assert not _asleep(p, tid)


def test_a_hand_off_from_before_the_spec_digest_takes_its_spec_as_the_baseline(env, monkeypatch):
    p = make(env)
    tid = _spec_sleeper(p)
    res = json.loads(p.db.task(tid)["result"])
    res.pop("spec_seen")
    p.db.update_task(tid, result=json.dumps(res))
    d = _spec_daemon(p, monkeypatch)
    d.probe_waiting()
    assert _asleep(p, tid) and json.loads(p.db.task(tid)["result"])["spec_seen"]
    _edit_spec(p, tid, "also report the peak memory")
    d.probe_waiting()
    assert not _asleep(p, tid)


def test_the_retry_when_docs_say_a_spec_change_wakes_the_task_once():
    for rel in ("skills/tt-project-harness/SKILL.md", "docs/design.md"):
        text = " ".join((RUNTIME.parent / rel).read_text().split())
        assert "spec update wakes it once" in text, rel


def test_a_spec_change_wakes_a_task_waiting_on_its_checks_at_its_own_tier(env, monkeypatch):
    # Task #4397: woken by a spec change while waiting on its own `ttp checks --result`, it ran as a light
    # "is the wait over?" check, saw the checks pass and handed off done without the addition.
    from ttp import budget as bud, push
    p = make(env)
    for tier, want in (("standard", "standard"), ("light", "light")):
        tid = _spec_sleeper(p, probe=f"{push._own_ttp(p)} checks --result /tmp/no-such-run")
        p.db.update_task(tid, tier=tier)
        assert bud.wake_tier(tier, json.loads(p.db.task(tid)["result"])) == "light"
        _edit_spec(p, tid, "also report the peak memory")
        _spec_daemon(p, monkeypatch).probe_waiting()
        res = json.loads(p.db.task(tid)["result"])
        assert res["wake_tier"] == tier and res["escalated_wake"] is True
        assert bud.wake_tier(tier, res) == want, (tier, res)
        assert "## Update" in res["woke"]


def test_passed_checks_after_a_spec_change_wake_the_task_instead_of_recording_its_on_pass(env, monkeypatch,
                                                                                          tmp_path):
    from ttp import budget as bud, daemon as dmod
    from ttp.prompts import spec_digest
    p = make(env)
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    tid, _ = _checks_wait_task(p, tmp_path, DONE_ON_PASS)
    res = json.loads(p.db.task(tid)["result"])
    p.db.update_task(tid, result=json.dumps({**res, "spec_seen": spec_digest(p.db.task(tid))}))
    _edit_spec(p, tid, "also report the parse time")
    p.db.update_task(tid, not_before=time.time() - 1)   # already due: the spec wake does not fire first
    _settle_probe(d, tid)
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert task["status"] == "queued" and _ready(p, tid), (task["status"], task["blocked_reason"], res)
    assert "its spec changed" in res["woke"] and res["spec_seen"] == spec_digest(task)
    assert bud.wake_tier(task["tier"], res) == "standard"
    assert not _on_pass_reviews(p), "the stale on_pass was recorded"


def _run_ended_after_a_spec_change(env, p, result, read=False, provider="claude"):
    """A worker run started on the task's first spec; the coordinator changed the spec while it ran (its
    update went to the run's steer.md) and the run then handed off `result`, having read its updates
    only if `read`."""
    from ttp.daemon import Daemon
    from ttp.hook import STEER_FILE, mark_seen
    from ttp.prompts import spec_digest
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task("fix the parser", "fix the parser", kind="work", tier="standard", origin="user")
    run_dir = env["tmp"] / f"run-spec-{tid}"
    run_dir.mkdir()
    note = {"spec_sha": spec_digest(p.db.task(tid))}
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,note) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", provider, time.time() - 600, "running", str(run_dir), json.dumps(note)))
    rid = p.db.one("SELECT id FROM runs WHERE task=?", (tid,))["id"]
    p.db.update_task(tid, status="running")
    _edit_spec(p, tid, "also report the parse time")
    steer = run_dir / STEER_FILE
    assert "also report the parse time" in steer.read_text(), "the update did not reach the running run"
    if read:
        mark_seen(run_dir, steer.stat().st_size)
    (run_dir / "result.json").write_text(json.dumps(result))
    d = Daemon(p.base)
    r = dict(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)))
    with d.p.db.tx():
        d._finish_worker(r, Usage(cost_usd=0.4), "ok", run_dir)
    return p.db.task(tid), note["spec_sha"]


def test_a_done_from_a_run_that_never_read_its_spec_update_is_sent_back_with_it(env):
    p = make(env)
    done = {"status": "done", "summary": "parser fixed"}
    task, _ = _run_ended_after_a_spec_change(env, p, done)
    res = json.loads(task["result"])
    assert task["status"] == "queued" and task["not_before"] <= time.time(), (task["status"], res)
    assert "also report the parse time" in res["woke"] and int(task["attempts"] or 0) == 0
    assert not _on_pass_reviews(p), "a review started on a hand-off that misses the update"
    # Read before the hand-off: the done stands.
    task, _ = _run_ended_after_a_spec_change(env, p, done, read=True)
    assert task["status"] == "done", json.loads(task["result"])


def test_a_waiting_hand_off_that_missed_a_spec_update_keeps_the_spec_it_started_on(env):
    from ttp.prompts import spec_digest
    p = make(env)
    wait = {"status": "waiting", "summary": "job running", "waiting_for": "the job", "retry_after_s": 3600,
            "retry_when": "exit 1"}
    task, started = _run_ended_after_a_spec_change(env, p, wait)
    res = json.loads(task["result"])
    assert task["status"] == "queued" and res["spec_seen"] == started != spec_digest(task)
    task, _ = _run_ended_after_a_spec_change(env, p, wait, read=True)
    assert json.loads(task["result"])["spec_seen"] == spec_digest(task)
