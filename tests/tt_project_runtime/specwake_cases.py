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


def _edit_spec(p, tid, text, wake_now=True):
    """A coordinator task_update adding `text` to the spec, by default with `wake_now` (a spec alone sleeps
    with the task until its wait ends: see the deferred-wake tests below)."""
    from ttp import coordinator as coord
    assert coord.apply(p, [{"type": "task_update", "id": tid, "spec": text, **({"wake_now": True} if wake_now else {})}]) == []


def _spec_daemon(p, monkeypatch):
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    monkeypatch.setattr(d, "_start_probe", lambda *a, **k: None)   # its probe never passes here
    return d


def _asleep(p, tid):
    return (p.db.task(tid)["not_before"] or 0) > time.time() + 600


def test_a_wake_now_spec_change_wakes_a_task_asleep_on_its_retry_when_once(env, monkeypatch):
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
        # Each wake ages past the coalesce window, so only the hourly guard holds the next one.
        aged = [min(x, time.time() - dmod.SPEC_WAKE_COALESCE_S - 1) for x in res.get("spec_wakes", [])]
        p.db.update_task(tid, not_before=time.time() + 3600, result=json.dumps({**res, "woke": "", "spec_wakes": aged}))
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


def test_the_retry_when_docs_say_a_spec_update_sleeps_with_the_task():
    for rel in ("skills/tt-project-harness/SKILL.md", "docs/design.md"):
        text = " ".join((RUNTIME.parent / rel).read_text().split())
        assert "spec update sleeps with" in text and "wake_now: true" in text, rel
    prompt = " ".join((RUNTIME.parent / "template/prompts/coordinator.md").read_text().split())
    assert "`wake_now` (true: wake a task asleep on its `retry_when` now" in prompt


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


def _deferred_flow(p, monkeypatch, tmp_path, tier="standard", hand_off=None):
    """A task at `tier` that ran once and handed off waiting on a file (a wake that would run light), with
    the daemon that ran it; returns (daemon, tid, the file, a count of its runs, whether it sleeps idle)."""
    from ttp import budget as bud, daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    tmp_path.mkdir(parents=True, exist_ok=True)
    flag = tmp_path / "job-done"
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps(
        {"status": "waiting", "summary": "job running", "waiting_for": "the job", "retry_after_s": 3600,
         "retry_when": f"test -e {flag}", **(hand_off or {})}))
    tid = p.db.add_task("run the long job", "start the job", kind="work", tier=tier, origin="user")
    d = dmod.Daemon(p.base)
    runs = lambda: len(p.db.q("SELECT id FROM runs WHERE task=?", (tid,)))
    idle = lambda: p.db.task(tid)["status"] == "queued" and (p.db.task(tid)["not_before"] or 0) > time.time() \
        and not p.db.q("SELECT id FROM runs WHERE status='running'")
    assert _run_until(d, p, idle) and runs() == 1
    assert bud.wake_tier(tier, json.loads(p.db.task(tid)["result"])) == "light", "the control: its own wake is light"
    return d, tid, flag, runs, idle


def _last_wake(p, tid):
    r = p.db.q("SELECT note FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))[0]
    return json.loads(r["note"] or "{}").get("wake") or {}


def test_a_spec_only_update_starts_no_run_until_the_wait_ends_then_runs_at_its_own_tier(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp.prompts import spec_digest
    d, tid, flag, runs, idle = _deferred_flow(p, monkeypatch, tmp_path)
    slept_on = json.loads(p.db.task(tid)["result"])["spec_seen"]
    _edit_spec(p, tid, "also report the peak memory", wake_now=False)
    for _ in range(6):
        d.tick()
        time.sleep(0.05)
    res = json.loads(p.db.task(tid)["result"])
    assert runs() == 1 and idle(), "a spec-only update woke the task before its wait ended"
    assert res["spec_seen"] == slept_on and not res.get("woke") and not res.get("spec_wakes"), res
    flag.write_text("")   # the wait ends: the run that follows reads the update at the task's own tier
    assert _run_until(d, p, lambda: runs() == 2), "the probe pass did not run the task"
    wake = _last_wake(p, tid)
    assert wake.get("tier") == "standard" and wake.get("escalated") is True, wake
    assert json.loads(p.db.task(tid)["result"])["spec_seen"] == spec_digest(p.db.task(tid))


def test_an_urgent_or_wake_now_spec_update_wakes_a_sleeping_task_at_once(env, monkeypatch, tmp_path):
    from ttp import coordinator as coord
    for how in ("wake_now", "urgent line", "wake_now alone"):
        p = make(env)
        d, tid, flag, runs, idle = _deferred_flow(p, monkeypatch, tmp_path / how.replace(" ", "-"))
        act = {"type": "task_update", "id": tid}
        if how != "wake_now alone":
            act["spec"] = "urgent: the user is waiting on this" if how == "urgent line" else "also report memory"
        if how.startswith("wake_now"):
            act["wake_now"] = True
        assert coord.apply(p, [act]) == [], how
        assert _run_until(d, p, lambda: runs() == 2), f"{how}: no run at once"
        wake = _last_wake(p, tid)
        assert wake.get("tier") == "standard" and wake.get("escalated") is True, (how, wake)
        assert "wake_now" not in json.loads(p.db.task(tid)["result"]), f"{how}: the request outlived its wake"


def test_wake_now_on_a_task_not_asleep_on_a_retry_when_is_ignored_with_a_note(env):
    from ttp import coordinator as coord
    p = make(env)
    fresh = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
    deferred = _spec_sleeper(p, labels=["start_when:exit 1"])
    for tid in (fresh, deferred):
        assert coord.apply(p, [{"type": "task_update", "id": tid, "wake_now": True}]) == []
        assert any(f"#{tid} wake_now ignored" in n for n in p.db.kv(coord.NOTES_KEY)), p.db.kv(coord.NOTES_KEY)
        assert "wake_now" not in json.loads(p.db.task(tid)["result"] or "{}")


def test_a_deferred_spec_change_rides_on_any_other_wake_at_its_own_tier(env, monkeypatch):
    # A wake for another reason (a broken probe, a failed job) after a deferred spec change: its run
    # reads the change at the task's own tier.
    from ttp import budget as bud
    from ttp.prompts import spec_digest
    p = make(env)
    tid = _spec_sleeper(p)
    d = _spec_daemon(p, monkeypatch)
    _edit_spec(p, tid, "also report the peak memory", wake_now=False)
    d.probe_waiting()
    assert _asleep(p, tid)
    d._wake_waiting(p.db.task(tid), "probe broken: exit 127", time.time())
    t = p.db.task(tid)
    res = json.loads(t["result"])
    assert res["woke"].startswith("probe broken") and "its spec changed" in res["woke"]
    assert res["spec_seen"] == spec_digest(t) and bud.wake_tier("standard", res) == "standard"


def test_the_daemons_own_spec_note_is_no_spec_change_but_keeps_a_pending_one(env, monkeypatch):
    # A probe-stall release appends the daemon's note to the spec: alone it reports no spec change;
    # after a deferred coordinator update the wake still carries that change.
    from ttp.prompts import spec_digest
    p = make(env)
    d = _spec_daemon(p, monkeypatch)
    quiet, busy = _spec_sleeper(p), _spec_sleeper(p)
    _edit_spec(p, busy, "also report the peak memory", wake_now=False)
    d.probe_waiting()
    assert _asleep(p, quiet) and _asleep(p, busy)
    for tid in (quiet, busy):
        d._probe_stall_act(p.db.task(tid), "release", "[probe stall] your probe has said not yet")
    q, b = p.db.task(quiet), p.db.task(busy)
    rq, rb = json.loads(q["result"]), json.loads(b["result"])
    assert rq["woke"] == "probe stale: released once by the blocker sweep" and rq["spec_seen"] == spec_digest(q)
    assert "its spec changed" in rb["woke"] and rb["spec_seen"] == spec_digest(b)
    assert "your probe has said not yet" in q["spec"] and "your probe has said not yet" in b["spec"]

def test_a_worker_cannot_ask_for_its_own_wake_now(env, monkeypatch, tmp_path):
    p = make(env)
    d, tid, flag, runs, idle = _deferred_flow(p, monkeypatch, tmp_path, hand_off={"wake_now": True})
    assert "wake_now" not in json.loads(p.db.task(tid)["result"])
    for _ in range(4):
        d.tick()
        time.sleep(0.05)
    assert runs() == 1 and idle(), "a worker's own wake_now woke it"


def test_a_reboot_wake_carries_a_deferred_spec_change_at_the_tasks_own_tier(env, tmp_path):
    # The boot-time wake writes its own result (Daemon.wake_after_reboot), and a lost detached job would
    # make it a light relaunch: a spec change that slept with the task still applies, at its own tier.
    from ttp import budget as bud
    from ttp.daemon import SPEC_WOKE
    from ttp.prompts import spec_digest
    p = make(env)
    tid = p.db.add_task("soak", "run the soak", kind="work", tier="standard", origin="user")
    rc, _ = _resumable_job_cut_by_reboot(p, tmp_path, tid, resumable=False)
    slept_on = spec_digest(p.db.task(tid))
    p.db.update_task(tid, status="queued", not_before=time.time() + 3600, result=json.dumps(
        {"status": "waiting", "summary": "job running", "retry_after_s": 3600, "retry_when": f"ttp detach --check {rc}",
         "waiting_since": time.time() - 900, "spec_seen": slept_on}))
    _edit_spec(p, tid, "also report the peak memory", wake_now=False)
    d = _boot_daemon(p)
    t = p.db.task(tid)
    res = json.loads(t["result"])
    assert _woken(p, tid), dict(t)
    assert res["woke"].startswith("the host rebooted and cut short detached job job "), res["woke"]
    assert res["woke"].endswith(f"; also {SPEC_WOKE}"), res["woke"]
    assert res["wake_tier"] == "standard" and res["escalated_wake"] is True, res
    assert bud.wake_tier(t["tier"], res, d.cfg) == "standard"
    assert res["spec_seen"] == spec_digest(t) != slept_on


def test_several_spec_only_updates_fold_into_the_one_run_its_probe_starts(env, monkeypatch, tmp_path):
    p = make(env)
    d, tid, flag, runs, idle = _deferred_flow(p, monkeypatch, tmp_path)
    for text in ("also report the peak memory", "and the wall time", "and the commit it ran on"):
        _edit_spec(p, tid, text, wake_now=False)
        d.tick()
    assert runs() == 1 and idle(), "a spec-only update woke the task before its wait ended"
    flag.write_text("")
    assert _run_until(d, p, lambda: runs() == 2), "the probe pass did not run the task"
    for _ in range(4):
        d.tick()
        time.sleep(0.05)
    assert runs() == 2, "the updates gave more than one run"
    assert p.db.task(tid)["spec"].count("## Update") == 3


def test_a_task_update_cannot_set_the_status_waiting_nothing_would_wake(env):
    # The daemon keeps a waiting task as status queued with its waiting hand-off and not_before
    # (db.waiting_handoff); a stored status 'waiting' is one no dispatch, probe or wake reads.
    from ttp import coordinator as coord
    p = make(env)
    fresh = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
    sleeper = _spec_sleeper(p)
    for tid in (fresh, sleeper):
        before = p.db.task(tid)
        probs = coord.apply(p, [{"type": "task_update", "id": tid, "status": "waiting"}])
        assert any(f"#{tid} rejected" in x and "retry_when" in x for x in map(str, probs)), probs
        after = p.db.task(tid)
        assert (after["status"], after["result"], after["not_before"]) == \
            (before["status"], before["result"], before["not_before"])
    assert [t["id"] for t in p.db.ready_tasks()] == [fresh]


def _update(p, tid, **act):
    from ttp import coordinator as coord
    assert coord.apply(p, [{"type": "task_update", "id": tid, **act}]) == []


def _sleep_again(p, tid):
    """Back to sleep after a wake, as its run's new waiting hand-off would leave it (keeping its wake times)."""
    res = json.loads(p.db.task(tid)["result"])
    res = {k: v for k, v in res.items() if k not in ("woke", "wake_tier", "escalated_wake")}
    p.db.update_task(tid, not_before=time.time() + 3600, result=json.dumps(res))


def test_wake_now_takes_a_tier_capped_at_the_tasks_own(env, monkeypatch):
    from ttp import budget as bud
    for own, asked, want in (("standard", True, "standard"), ("standard", "light", "light"),
                             ("standard", "deep", "standard"), ("light", "deep", "light"),
                             ("deep", "standard", "standard"), ("light", True, "light")):
        p = make(env)
        tid = _spec_sleeper(p)
        p.db.update_task(tid, tier=own)
        d = _spec_daemon(p, monkeypatch)
        _update(p, tid, spec="also report the peak memory", wake_now=asked)
        d.probe_waiting()
        res = json.loads(p.db.task(tid)["result"])
        assert not _asleep(p, tid) and res["wake_tier"] == want, (own, asked, res)
        assert bud.wake_tier(own, res) == want, (own, asked)


def test_the_wake_now_schema_takes_true_or_a_tier_and_stays_strict_valid():
    from ttp import coordinator as coord
    from ttp.providers.base import strict_schema
    sub = coord.ACTIONS_SCHEMA["properties"]["actions"]["items"]["properties"]["wake_now"]
    assert sub == {"anyOf": [{"type": "boolean"}, {"type": "string", "enum": ["light", "standard", "deep"]}]}
    wire = strict_schema(coord.ACTIONS_SCHEMA)["properties"]["actions"]["items"]["properties"]["wake_now"]
    assert {"type": "null"} in wire["anyOf"], wire


def test_wake_now_with_an_unknown_tier_is_ignored(env, monkeypatch):
    p = make(env)
    tid = _spec_sleeper(p)
    _update(p, tid, spec="also report the peak memory", wake_now="max")
    assert "wake_now" not in json.loads(p.db.task(tid)["result"])
    _spec_daemon(p, monkeypatch).probe_waiting()
    assert _asleep(p, tid)


def test_wake_now_requests_before_the_run_starts_make_one_wake_at_the_highest_tier(env, monkeypatch):
    p = make(env)
    tid = _spec_sleeper(p)
    p.db.update_task(tid, tier="deep")
    d = _spec_daemon(p, monkeypatch)
    _update(p, tid, spec="also report the peak memory", wake_now="light")
    _update(p, tid, spec="and the wall time", wake_now="standard")   # between ticks: merged
    _update(p, tid, wake_now="light")   # a lower ask never lowers it
    d.probe_waiting()
    res = json.loads(p.db.task(tid)["result"])
    assert not _asleep(p, tid) and res["wake_tier"] == "standard" and len(res["spec_wakes"]) == 1, res
    # Woken, its run not started: another request rides on that wake and raises its tier, still capped.
    _update(p, tid, spec="and the disk use", wake_now="deep")
    d.probe_waiting()
    t = p.db.task(tid)
    res = json.loads(t["result"])
    assert res["wake_tier"] == "deep" and len(res["spec_wakes"]) == 1 and "wake_now" not in res, res
    assert "and the disk use" in t["spec"] and "also report the peak memory" in t["spec"]
    from ttp import coordinator as coord
    assert not any(f"#{tid} wake_now ignored" in n for n in (p.db.kv(coord.NOTES_KEY) or []))


def test_a_wake_now_inside_the_coalesce_window_waits_for_it_to_end(env, monkeypatch):
    from ttp import daemon as dmod
    p = make(env)
    tid = _spec_sleeper(p)
    d = _spec_daemon(p, monkeypatch)
    _update(p, tid, spec="edit 1", wake_now=True)
    d.probe_waiting()
    assert not _asleep(p, tid)
    _sleep_again(p, tid)
    _update(p, tid, spec="edit 2", wake_now="light")
    _update(p, tid, spec="edit 3", wake_now=True)
    for _ in range(3):
        d.probe_waiting()
    res = json.loads(p.db.task(tid)["result"])
    assert _asleep(p, tid) and res["wake_now"] == "standard" and len(res["spec_wakes"]) == 1, res
    # The window ends: one wake carries both requests, at the highest tier asked.
    p.db.update_task(tid, result=json.dumps({**res, "spec_wakes": [x - dmod.SPEC_WAKE_COALESCE_S - 1 for x in res["spec_wakes"]]}))
    d.probe_waiting()
    t = p.db.task(tid)
    res = json.loads(t["result"])
    assert not _asleep(p, tid) and res["wake_tier"] == "standard" and len(res["spec_wakes"]) == 2, res
    assert "wake_now" not in res and "edit 2" in t["spec"] and "edit 3" in t["spec"]


def _age_spec_wakes(p, tid, by):
    res = json.loads(p.db.task(tid)["result"])
    p.db.update_task(tid, result=json.dumps({**res, "spec_wakes": [x - by for x in res.get("spec_wakes", [])]}))


def test_a_priority_1_task_asleep_on_its_retry_when_sleeps_on_through_a_spec_update(env, monkeypatch):
    # Standing priority 1 alone is no reason to wake: the update applies when its wait ends.
    p = make(env)
    tid = _spec_sleeper(p)
    p.db.update_task(tid, priority=1)
    d = _spec_daemon(p, monkeypatch)
    for i in range(3):
        _edit_spec(p, tid, f"also report memory {i}", wake_now=False)
        d.probe_waiting()
        assert _asleep(p, tid), f"spec update {i} woke a priority 1 task"
    assert "spec_wakes" not in json.loads(p.db.task(tid)["result"])
    # wake_now still wakes it, once.
    _update(p, tid, wake_now=True)
    d.probe_waiting()
    assert not _asleep(p, tid)
    _sleep_again(p, tid)
    d.probe_waiting()
    assert _asleep(p, tid), "one wake_now woke it twice"


def test_an_added_urgent_line_wakes_once_per_wait_without_the_coalesce_window(env, monkeypatch):
    from ttp import daemon as dmod
    p = make(env)
    tid = _spec_sleeper(p)
    d = _spec_daemon(p, monkeypatch)
    _edit_spec(p, tid, "urgent: the user is waiting", wake_now=False)
    d.probe_waiting()
    assert not _asleep(p, tid), "an added urgent: line did not wake it"
    res = json.loads(p.db.task(tid)["result"])
    assert res["spec_urgent"] == ["the user is waiting"] and res["spec_urgent_woke"] == dmod._wait_key(res), res
    _sleep_again(p, tid)
    for i, text in enumerate(("also report memory", "urgent: and the disk", "urgent: and the wall time")):
        _edit_spec(p, tid, text, wake_now=False)
        d.probe_waiting()
        assert _asleep(p, tid), (i, "a second urgent spec wake on the same wait")
    # Its wait changes (a new hand-off): the next added urgent: line wakes it at once, inside the 30 min window.
    res = json.loads(p.db.task(tid)["result"])
    p.db.update_task(tid, result=json.dumps({**res, "retry_when": "exit 2", "waiting_for": "stage two"}))
    _edit_spec(p, tid, "urgent: stage two blocks the user", wake_now=False)
    d.probe_waiting()
    res = json.loads(p.db.task(tid)["result"])
    assert not _asleep(p, tid) and len(res["spec_wakes"]) == 2, res


def test_a_wake_now_after_an_urgent_wake_on_the_same_wait_takes_the_coalesce_window(env, monkeypatch):
    from ttp import daemon as dmod
    p = make(env)
    tid = _spec_sleeper(p)
    d = _spec_daemon(p, monkeypatch)
    _update(p, tid, spec="urgent: edit 0", wake_now=True)
    d.probe_waiting()
    assert not _asleep(p, tid)
    _sleep_again(p, tid)
    _update(p, tid, spec="urgent: edit 1", wake_now=True)
    d.probe_waiting()
    assert _asleep(p, tid), "a wake_now after the urgent wake on this wait skipped the coalesce window"
    _age_spec_wakes(p, tid, dmod.SPEC_WAKE_COALESCE_S + 1)
    d.probe_waiting()
    assert not _asleep(p, tid) and "wake_now" not in json.loads(p.db.task(tid)["result"])


def test_a_waiting_hand_off_keeps_the_urgent_line_it_slept_on_and_its_urgent_wake_only_on_the_same_wait(env, monkeypatch, tmp_path):
    p = make(env)
    p.set_config("waiting.spec_wake_hold_s", 0)   # only the one-per-wait rule keeps the next wake out
    d, tid, flag, runs, idle = _deferred_flow(p, monkeypatch, tmp_path)
    _edit_spec(p, tid, "urgent: the user is waiting", wake_now=False)
    assert _run_until(d, p, lambda: runs() == 2 and idle()), "the added urgent: line did not start a run"
    res = json.loads(p.db.task(tid)["result"])
    assert res["spec_urgent"] == ["the user is waiting"] and res["spec_urgent_woke"] == res["wait_key"]["key"], res
    _edit_spec(p, tid, "urgent: and the disk use", wake_now=False)
    for _ in range(3):
        d.tick()
        time.sleep(0.05)
    assert runs() == 2 and idle(), "a second urgent spec wake on the same wait started a run"
    # A worker cannot forge either key: a new wait drops the urgent wake it woke on.
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps(
        {"status": "waiting", "summary": "stage two", "waiting_for": "stage two", "retry_after_s": 3600,
         "retry_when": f"test -e {tmp_path / 'two'}", "spec_urgent": [], "spec_urgent_woke": "forged"}))
    _age_spec_wakes(p, tid, 1801)
    _update(p, tid, wake_now=True)
    assert _run_until(d, p, lambda: runs() == 3 and idle())
    res = json.loads(p.db.task(tid)["result"])
    assert res["spec_urgent"] == ["the user is waiting", "and the disk use"] and "spec_urgent_woke" not in res, res


def test_a_hand_off_from_before_the_urgent_key_wakes_only_on_an_urgent_line_dated_after_it_slept(env, monkeypatch):
    from ttp import daemon as dmod, urgent
    p = make(env)
    tid = _spec_sleeper(p)
    t = p.db.task(tid)
    t = {**dict(t), "spec": t["spec"] + "\nurgent: now"}
    slept = {"status": "waiting", "waiting_since": time.time() + 1}   # after the task was made
    for labels, want in (([], ""), ([f"{urgent.SINCE_LABEL}{time.time() - 600:.0f}"], ""),
                         ([f"{urgent.SINCE_LABEL}{time.time() + 5:.0f}"], "now")):
        assert dmod._urgent_added({**t, "labels": json.dumps(labels)}, slept) == want, labels
    assert dmod._urgent_added(t, {**slept, "spec_urgent": ["now"]}) == ""
    assert dmod._urgent_added({**t, "spec": t["spec"] + "\nurgent: later"}, {**slept, "spec_urgent": ["now"]}) == "later"
    assert dmod._urgent_added({**t, "priority": 1, "spec": "plain"}, {**slept, "spec_urgent": []}) == ""


def test_an_obligation_note_appended_to_a_spec_changes_no_digest_and_wakes_nothing(env, monkeypatch):
    from ttp import obligations
    from ttp.prompts import spec_digest
    p = make(env)
    tid = _spec_sleeper(p)
    before = spec_digest(p.db.task(tid))
    obligations.add(p, "the promised report", owners=[tid],
                    milestones=[{"key": "m1", "due": time.time() + 7200, "evidence_required": "a file"}])
    t = p.db.task(tid)
    assert "## Obligation " in t["spec"] and spec_digest(t) == before
    d = _spec_daemon(p, monkeypatch)
    d.probe_waiting()
    assert _asleep(p, tid)
    # An update after the note still counts, and only its text does.
    _edit_spec(p, tid, "also report memory", wake_now=False)
    plain = {"spec": "start the job and report\n\n## Update\nalso report memory", "title": "x"}
    assert spec_digest(p.db.task(tid)) == spec_digest(plain) != before


def _spec_wake_flow(p, monkeypatch, tmp_path, second=None):
    """A task that ran once and handed off waiting on a file, then was woken by a wake_now spec update
    and ran again, handing back `second` (default: the same wait). Returns (daemon, tid, flag, runs, idle)."""
    from ttp import daemon as dmod
    d, tid, flag, runs, idle = _deferred_flow(p, monkeypatch, tmp_path)
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 3600)   # its probe ran; ticks here only check the spec
    if second is not None:
        monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps(second))
    _edit_spec(p, tid, "also report the peak memory")
    assert _run_until(d, p, lambda: runs() == 2 and idle()), "the wake_now did not start a run"
    # Past the coalesce window, so only the spec-wake hold can keep the next wake out.
    _age_spec_wakes(p, tid, dmod.SPEC_WAKE_COALESCE_S + 1)
    return d, tid, flag, runs, idle


def test_a_spec_wake_that_hands_back_the_same_wait_holds_the_next_spec_wakes_until_the_hold_ends(env, monkeypatch, tmp_path):
    from ttp import daemon as dmod
    p = make(env)
    d, tid, flag, runs, idle = _spec_wake_flow(p, monkeypatch, tmp_path)
    res = json.loads(p.db.task(tid)["result"])
    hold = res["spec_hold"]
    assert 0 < hold["until"] - time.time() <= dmod.SPEC_WAKE_HOLD_S and hold["key"] == res["wait_key"]["key"], res
    assert "spec_woke" not in res, "the spec-wake mark outlived the run it marked"
    assert "spec wakes held until" in p.db.task(tid)["blocked_reason"]
    for text in ("and the wall time", "and the disk use"):
        _edit_spec(p, tid, text)
        for _ in range(3):
            d.tick()
            time.sleep(0.05)
        assert runs() == 2 and idle(), "a spec wake inside the hold started a run"
    t = p.db.task(tid)
    assert t["blocked_reason"].count("spec wakes held until") == 1, t["blocked_reason"]
    assert json.loads(t["result"])["wake_now"], "the held wake_now was dropped, not kept for the hold's end"
    # The hold ends: the held wake_now wakes it once, with every update since.
    res = json.loads(t["result"])
    p.db.update_task(tid, result=json.dumps({**res, "spec_hold": {**res["spec_hold"], "until": time.time() - 1}}))
    assert _run_until(d, p, lambda: runs() == 3), "the wake_now held through the hold did not wake it after"


def test_a_spec_wake_that_hands_back_a_different_wait_holds_nothing(env, monkeypatch, tmp_path):
    p = make(env)
    # A worker cannot write the hold, or the mark that its run was a spec wake, itself.
    other = {"status": "waiting", "summary": "job queued", "waiting_for": "the job's second stage",
             "retry_after_s": 3600, "retry_when": f"test -e {tmp_path / 'stage-two'}",
             "spec_woke": time.time(), "spec_hold": {"until": time.time() + 7200, "key": "forged"}}
    d, tid, flag, runs, idle = _spec_wake_flow(p, monkeypatch, tmp_path, second=other)
    assert "spec_hold" not in json.loads(p.db.task(tid)["result"])
    _edit_spec(p, tid, "and the wall time")
    assert _run_until(d, p, lambda: runs() == 3), "a changed wait kept the next spec wake out"


def test_the_spec_wake_hold_leaves_the_tasks_own_retry_when_wake_alone(env, monkeypatch, tmp_path):
    from ttp import daemon as dmod
    p = make(env)
    d, tid, flag, runs, idle = _spec_wake_flow(p, monkeypatch, tmp_path)
    assert json.loads(p.db.task(tid)["result"])["spec_hold"]
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    flag.write_text("")
    assert _run_until(d, p, lambda: runs() == 3), "the hold kept its own probe pass from waking it"


def test_the_spec_wake_hold_is_a_setting_and_zero_turns_it_off(env, monkeypatch, tmp_path):
    from ttp import project as proj
    p = make(env)
    assert proj.DEFAULT_CONFIG["waiting"]["spec_wake_hold_s"] == 7200
    p.set_config("waiting.spec_wake_hold_s", 0)
    d, tid, flag, runs, idle = _spec_wake_flow(p, monkeypatch, tmp_path)
    assert "spec_hold" not in json.loads(p.db.task(tid)["result"])


def test_an_urgent_wake_now_on_a_changed_wait_goes_past_the_spec_wake_hold(env, monkeypatch):
    from ttp import daemon as dmod
    for urgent_, changed, wakes, hold in ((False, False, False, None), (False, True, False, None),
                                          (True, False, False, None), (True, True, True, None),
                                          (False, False, True, "garbled"), (False, False, True, {"until": "soon"}),
                                          (False, False, True, {"until": time.time() - 1})):
        p = make(env)
        tid = _spec_sleeper(p)
        p.db.update_task(tid, priority=1)   # priority 1 alone is not urgent here: only an added urgent: line
        res = json.loads(p.db.task(tid)["result"])
        key = dmod._wait_key(res)
        res["spec_hold"] = hold or {"until": time.time() + 3600, "key": key if not changed else "0" * 16}
        p.db.update_task(tid, result=json.dumps(res))
        spec = "urgent: the user is waiting" if urgent_ else "also report the peak memory"
        _update(p, tid, spec=spec, wake_now=True)
        _spec_daemon(p, monkeypatch).probe_waiting()
        assert (not _asleep(p, tid)) == wakes, (urgent_, changed, hold)


def test_spec_hold_after_needs_a_spec_wake_and_the_same_wait_and_head():
    from ttp import daemon as dmod
    key = {"key": "k1", "head": "h1"}
    woke = {"spec_woke": time.time() - 60, "wait_key": dict(key)}
    nb = time.time() + 99999
    assert dmod._spec_hold_after(woke, key, 7200, nb)["key"] == "k1"
    assert dmod._spec_hold_after({**woke, "spec_woke": None}, key, 7200, nb) is None, "not a spec wake"
    assert dmod._spec_hold_after(woke, {**key, "key": "k2"}, 7200, nb) is None, "a changed wait"
    assert dmod._spec_hold_after(woke, {**key, "head": "h2"}, 7200, nb) is None, "a new commit"
    assert dmod._spec_hold_after({**woke, "wait_key": None}, key, 7200, nb) is None, "no wait before"
    assert dmod._spec_hold_after(woke, key, 0, nb) is None, "off"
    soon = time.time() + 600
    assert dmod._spec_hold_after(woke, key, 7200, soon)["until"] == soon, "its own retry time ends it first"


class _TierJev:
    """Stands in for providers.jev.Jev on the wake tier question only (waketier.pick): a judgment score."""

    def __init__(self, judgment):
        self.judgment, self.calls, self.last_cost = judgment, 0, 0.0001

    def enabled(self):
        return True

    def decide(self, state, questions, purpose="decide", timeout=None):
        if "judgment" not in questions:
            return None
        self.calls += 1
        return {"judgment": {"noul": self.judgment}}


@pytest.mark.parametrize("judgment, tier", [(0.1, "light"), (0.9, "standard")])
def test_a_spec_change_wake_runs_light_when_jev_finds_no_judgment_needed(env, monkeypatch, tmp_path, judgment, tier):
    """User msg #2230: an escalated wake for a spec change that slept with the wait asks Jev for its tier:
    light unless Jev finds judgment is needed. The run note keeps the reason and who chose the tier, and
    a light run that asks for more settles Jev's call as wrong."""
    p = make(env)
    d, tid, flag, runs, idle = _deferred_flow(p, monkeypatch, tmp_path)
    d.jev = jev = _TierJev(judgment)
    _edit_spec(p, tid, "also report the peak memory", wake_now=False)
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps(
        {"status": "waiting", "summary": "found work", "retry_after_s": 0, "wake_tier": "standard",
         "retry_when": f"test -e {flag}"}))
    flag.write_text("")
    assert _run_until(d, p, lambda: runs() == 2), "the probe pass did not run the task"
    wake = _last_wake(p, tid)
    assert jev.calls == 1 and wake["tier"] == tier and wake["reason"] == "spec_change", wake
    assert wake["by"] == ("jev" if tier == "light" else "rule") and wake["escalated"] is (tier != "light"), wake
    call = p.db.one("SELECT * FROM jev_calls WHERE use='wake_tier'")
    assert call["ref"] == f"task:{tid}"
    if tier == "light":
        assert wake["pick"]["from"] == "standard" and wake["pick"]["call"] == call["id"]
        assert _run_until(d, p, lambda: runs() == 3), "the light run's ask for more did not run it again"
        assert p.db.one("SELECT outcome FROM jev_calls WHERE id=?", (call["id"],))["outcome"] == "wrong", \
            "a light run that asked for more settles Jev's pick as wrong"
        assert _last_wake(p, tid)["tier"] == "standard"


@pytest.mark.parametrize("kind", ["checks_failed", "job_failed", "match_fail", "asked", "wake_now"])
def test_an_escalated_wake_with_known_work_keeps_its_tier_and_asks_no_jev(env, kind):
    """A failed check, job or probe_match, a light wake that asked for more and a coordinator's wake_now
    keep the tier their wake set, with no Jev call; a spec change asks it."""
    from ttp import waketier
    p = make(env)
    tid = p.db.add_task("t", "s", kind="work", tier="standard", origin="user")
    prev = {"status": "waiting", "retry_when": "exit 1", "escalated_wake": True, "wake_tier": "standard",
            waketier.KEY: kind}
    jev = _TierJev(0.0)
    assert waketier.pick(p.db, p.config(), p.db.task(tid), prev, jev=jev) is None and jev.calls == 0
    got = waketier.pick(p.db, p.config(), p.db.task(tid), {**prev, waketier.KEY: "spec_change"}, jev=jev)
    assert jev.calls == 1 and got["tier"] == "light"
    lowered = waketier.lowered(prev, got)
    assert "escalated_wake" not in lowered and lowered["wake_tier"] == "light"
    p.set_config("jev.uses.wake_tier", "off")
    assert waketier.pick(p.db, p.config(), p.db.task(tid), {**prev, waketier.KEY: "spec_change"}, jev=jev) is None
    assert jev.calls == 1


def test_each_wake_records_its_reason_and_stats_counts_wakes_and_dollars_by_reason(env, tmp_path):
    """Every _wake_waiting stores its reason in the hand-off; `ttp stats --wakes` sums wake runs per home-zone
    day by reason, with their $ and how many handed the wait back."""
    from ttp import daemon as dmod, waketier
    p = make(env)
    dm = dmod.Daemon(p.base)
    tid = p.db.add_task("t", "s", kind="work", tier="standard", origin="user")
    p.db.update_task(tid, status="queued", not_before=time.time() + 3600,
                     result=json.dumps({"status": "waiting", "retry_when": "exit 1", "waiting_since": time.time()}))
    dm._wake_waiting(p.db.task(tid), "its checks failed (exit 1)", time.time(), kind="checks_failed")
    assert waketier.reason(json.loads(p.db.task(tid)["result"])) == "checks_failed"
    assert waketier.reason({"status": "waiting"}) == "timer"
    now = time.time()
    for i, (why, cost, back) in enumerate([("spec_change", 0.8, True), ("spec_change", 0.2, False), ("timer", 0.1, True)]):
        rd = tmp_path / f"r{i}"
        rd.mkdir()
        (rd / "result.json").write_text(json.dumps({"status": "waiting" if back else "done"}))
        p.db.x("INSERT INTO runs(task,role,provider,started,ended,status,dir,note,cost_usd) VALUES(?,?,?,?,?,?,?,?,?)",
               (tid, "worker", "fake", now - 60, now, "ok", str(rd),
                json.dumps({"wake": {"tier": "standard", "reason": why, "by": "rule"}}), cost))
    s = waketier.stats(p.db, "America/Los_Angeles", 1, now)
    (day,) = s["per_day"].values()
    assert day["spec_change"]["wakes"] == 2 and day["spec_change"]["usd"] == 1.0 and day["spec_change"]["rewaited"] == 1
    assert day["timer"]["wakes"] == 1 and day["spec_change"]["by"] == {"rule/standard": 2}
    assert "spec_change: 2 wakes, $1.00, 1 handed the wait back" in waketier.text(s)
