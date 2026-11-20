# Absent outside-wait tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new absentwait.py tests here.
from __future__ import annotations

_AW_PROBE = "curl -fsS https://models.example.com/v1/models | grep -q example-model"


def _aw_waiting(p, probe=_AW_PROBE, kind="work", **res):
    """A task that handed off waiting on `probe` through the daemon's hand-off path: status queued, last
    result waiting (the daemon never writes a task status 'waiting')."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    tid = p.db.add_task("use example-model", "run the eval on example-model", kind=kind, tier="light", origin="user")
    p.db.update_task(tid, status="running")
    run_dir = p.base / f"aw-run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "waiting", "summary": "waits on the listing",
                                                     "retry_after_s": 1800, "retry_when": probe, **res}))
    Daemon(p.base)._finish_worker({"task": tid, "note": "{}"}, RunUsage(cost_usd=0.1), "ok", run_dir)
    t = p.db.task(tid)
    assert t["status"] == "queued" and json.loads(t["result"])["status"] == "waiting", (t["status"], t["result"])
    return tid


def _aw_answers(p, tid, probe, reasons, t0, rc=1):
    from ttp import progress
    for i, reason in enumerate(reasons):
        progress.note_probe(p.db, tid, probe, rc, reason, t0 + 200 * i)
    return t0 + 200 * len(reasons)


def test_absent_wait_closes_after_n_identical_answers_with_one_reopen_watch(env):
    from ttp import absentwait
    from ttp.daemon import Daemon
    p = make(env)
    tid = _aw_waiting(p, waiting_for="example-model on the provider's listing")
    t0 = time.time()
    now = _aw_answers(p, tid, _AW_PROBE, ["HTTP 404: model example-model not found"] * 2, t0)
    d = Daemon(p.base)
    d.close_absent_waits(now)
    assert p.db.task(tid)["status"] == "queued"   # two identical answers: not yet settled
    now = _aw_answers(p, tid, _AW_PROBE, ["HTTP 404: model example-model not found"], now)
    d.close_absent_waits(now)
    t = p.db.task(tid)
    res = json.loads(t["result"])
    assert t["status"] == "done" and res["outcome"] == "unavailable" and res["absent"]["same"] == 3
    assert "404" in res["summary"] and res["waited_for"]
    st = absentwait.load(p.db)
    assert list(st["watches"]) == [str(tid)] and st["watches"][str(tid)]["probe"] == _AW_PROBE
    d.close_absent_waits(now + 60)   # closing again registers no second watch, no second event
    assert len(absentwait.load(p.db)["watches"]) == 1
    assert len(p.db.q("SELECT id FROM events WHERE kind='task_done' AND task=?", (tid,))) == 1


def test_absent_wait_counts_only_identical_answers_in_a_row(env):
    from ttp import absentwait, progress
    from ttp.daemon import _current_probe
    p = make(env)
    tid = _aw_waiting(p)
    t0 = time.time()
    now = _aw_answers(p, tid, _AW_PROBE, ["HTTP 404 not found", "HTTP 404 not found", "HTTP 502 unavailable",
                                          "HTTP 404 not found"], t0)
    assert progress.load(p.db)["probes"][str(tid)]["same"] == 1
    assert absentwait.due(p.db, {}, now, _current_probe) == []
    assert absentwait.due(p.db, {"waiting": {"absent_close_n": 1}}, now, _current_probe)
    assert absentwait.due(p.db, {"waiting": {"absent_close_n": 0}}, now, _current_probe) == []   # 0: off


def test_absent_wait_closes_on_settled_evidence_in_the_handoff(env):
    from ttp import absentwait
    from ttp.daemon import _current_probe
    p = make(env)
    tid = _aw_waiting(p, summary="The provider's model listing returned 20 HTTP 404s; example-model is not listed.")
    t0 = time.time()
    assert absentwait.due(p.db, {}, t0, _current_probe) == []   # its probe has not said not yet
    now = _aw_answers(p, tid, _AW_PROBE, ["not yet"], t0)
    (c,) = absentwait.due(p.db, {}, now, _current_probe)
    assert "settled evidence" in c["why"]
    assert absentwait.close(p.db, c, now) and p.db.task(tid)["status"] == "done"
    assert absentwait.close(p.db, c, now) is False


def test_absent_wait_reopen_watch_pass_queues_one_followup(env):
    from ttp import absentwait
    from ttp.daemon import _current_probe
    p = make(env)
    tid = _aw_waiting(p)
    t0 = time.time()
    now = _aw_answers(p, tid, _AW_PROBE, ["HTTP 404 not found"] * 3, t0)
    (c,) = absentwait.due(p.db, {}, now, _current_probe)
    absentwait.close(p.db, c, now, every_s=3600, for_s=86400)
    ran = []
    answers = iter([1, None, 0])
    run = lambda probe: ran.append(probe) or next(answers)
    assert absentwait.run_watches(p.db, now + 60, run) == [] and ran == []   # not due before its cadence
    absentwait.run_watches(p.db, now + 3600, run)                            # still absent: next in an hour
    assert absentwait.load(p.db)["watches"][str(tid)]["next"] == now + 7200
    absentwait.run_watches(p.db, now + 7200, run)                            # running: judged later
    (e,) = absentwait.run_watches(p.db, now + 7260, run)                     # passes
    assert e["event"] == "reopened" and ran == [_AW_PROBE] * 3
    f = p.db.task(e["followup"])
    assert f["status"] == "queued" and f["title"] == "reopen: use example-model"
    assert f"reopens:{tid}" in json.loads(f["labels"]) and "run the eval on example-model" in f["spec"]
    st = absentwait.load(p.db)
    assert st["watches"] == {} and st["reopened"] == {str(tid): e["followup"]}
    assert absentwait.run_watches(p.db, now + 90000, lambda probe: 0) == []
    assert len(p.db.q("SELECT id FROM tasks WHERE title LIKE 'reopen:%'")) == 1


def test_absent_wait_reopen_watch_lapses_after_its_period(env):
    from ttp import absentwait
    from ttp.daemon import _current_probe
    p = make(env)
    tid = _aw_waiting(p)
    now = _aw_answers(p, tid, _AW_PROBE, ["HTTP 404 not found"] * 3, time.time())
    (c,) = absentwait.due(p.db, {}, now, _current_probe)
    absentwait.close(p.db, c, now, every_s=3600, for_s=7200)
    (e,) = absentwait.run_watches(p.db, now + 7200, lambda probe: 0)
    assert e["event"] == "lapsed" and absentwait.load(p.db)["watches"] == {}
    assert p.db.q("SELECT id FROM tasks WHERE title LIKE 'reopen:%'") == []


def test_absent_wait_leaves_reset_credential_pending_and_local_waits_to_their_deadline(env):
    from ttp import absentwait
    from ttp.daemon import Daemon, _current_probe
    p = make(env)
    gw = "curl -fsS https://gateway.example.com/v1/models | grep -q example-model"
    budget = _aw_waiting(p, gw, waiting_for="the gateway's daily spend reset")
    quota = _aw_waiting(p, gw)
    login = _aw_waiting(p, gw, waiting_for="provider login")
    pending = _aw_waiting(p)
    local = _aw_waiting(p, "test -f out/example-model.bin")
    code = _aw_waiting(p, kind="code")
    own = _aw_waiting(p)   # a work task on its own branch: a close would skip its commits
    p.db.update_task(own, branch=f"ttp/t{own}-example-eval")
    mine = _aw_waiting(p, wait_kind="self")
    busy = _aw_waiting(p)
    t0 = time.time()
    for tid, reason in ((budget, "HTTP 404 not found"), (quota, "HTTP 429: quota exceeded, not found"),
                        (login, "HTTP 404 not found"), (pending, "not found yet: listing pending"),
                        (local, "No such file: not found"), (code, "HTTP 404 not found"), (own, "HTTP 404 not found"),
                        (mine, "HTTP 404 not found")):
        now = _aw_answers(p, tid, _current_probe(p.db.task(tid)), [reason] * 5, t0)
    _aw_answers(p, busy, _AW_PROBE, ["HTTP 404 not found"] * 5, t0, rc=75)
    assert absentwait.due(p.db, {}, now, _current_probe) == []
    Daemon(p.base).close_absent_waits(now)
    for tid in (budget, quota, login, pending, local, code, own, mine, busy):
        assert p.db.task(tid)["status"] == "queued", tid
    assert absentwait.load(p.db)["watches"] == {}


def test_absent_wait_reads_only_the_handoff_state_the_daemon_writes(env):
    from ttp import absentwait
    from ttp.daemon import _current_probe
    from ttp.db import waiting_handoff
    p = make(env)
    tid = _aw_waiting(p, waiting_for="example-model on the provider's listing")
    assert waiting_handoff(p.db.task(tid))["retry_when"] == _AW_PROBE
    now = _aw_answers(p, tid, _AW_PROBE, ["HTTP 404 not found"] * 3, time.time())
    assert [c["task"] for c in absentwait.due(p.db, {}, now, _current_probe)] == [tid]
    # A queued task whose stored result is no waiting hand-off is no wait, whatever its old retry_when says.
    p.db.update_task(tid, result=json.dumps({"status": "done", "retry_when": _AW_PROBE}))
    assert waiting_handoff(p.db.task(tid)) == {} and absentwait.due(p.db, {}, now, _current_probe) == []
    # Nor is a running task, though its last result was a waiting hand-off.
    p.db.update_task(tid, status="running", result=json.dumps({"status": "waiting", "retry_when": _AW_PROBE}))
    assert waiting_handoff(p.db.task(tid)) == {} and absentwait.due(p.db, {}, now, _current_probe) == []
    # A start_when deferral is judged on its own probe and start_why, never as a waiting hand-off.
    labels = json.dumps([f"start_when:{_AW_PROBE}", "start_why:example-model gives 404s, not listed"])
    p.db.update_task(tid, status="queued", labels=labels, result=json.dumps({"status": "waiting", "wait_kind": "self",
                                                                             "retry_when": "test -f x"}))
    assert waiting_handoff(p.db.task(tid)) == {}
    (c,) = absentwait.due(p.db, {}, now, _current_probe)
    assert c["task"] == tid and c["probe"] == _AW_PROBE   # its stale wait_kind self is no wait of its own
