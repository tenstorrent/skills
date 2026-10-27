# Wait-wake gate tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new wait-wake gate tests (waitgate.py and its daemon hooks) here.
from __future__ import annotations


class _GateJev:
    """Stands in for providers.jev.Jev on the wake gate's question: a noul score, no answer, or an error."""

    def __init__(self, needed=None, error=None, cost=0.0002):
        self.needed, self.error, self.last_cost, self.calls = needed, error, cost, 0

    def enabled(self):
        return True

    def decide(self, state, questions, purpose="decide", timeout=None):
        self.calls += 1
        if self.error:
            raise self.error
        return None if self.needed is None else {"needed": {"noul": self.needed}}


def _gate_task(p, tmp_path, probe="exit 1", ended_ago=60.0, note=None, **extra):
    """A task asleep on a `waiting` hand-off with `probe`, after a worker run that ended `ended_ago` s ago
    with runs.note `note`."""
    now = time.time()
    tid = p.db.add_task("run the long job", "s", kind="work", tier="standard", origin="user")
    run_dir = tmp_path / f"gate-run-{tid}"
    run_dir.mkdir()
    p.db.x("INSERT INTO runs(task,role,provider,started,ended,status,dir,note) VALUES(?,?,?,?,?,?,?,?)",
           (tid, "worker", "fake", now - ended_ago - 60, now - ended_ago, "ok", str(run_dir), json.dumps(note or {})))
    hand = {"status": "waiting", "summary": "job running", "waiting_for": "the job", "retry_after_s": 60,
            "retry_when": probe, **extra}
    (run_dir / "result.json").write_text(json.dumps(hand))
    p.db.update_task(tid, status="queued", not_before=now + 3600,
                     result=json.dumps({**hand, "waiting_since": now - ended_ago}))
    return tid


def _gate_decide(p, tid, cfg=None, now=None, **kw):
    from ttp import waitgate
    task = p.db.task(tid)
    last = p.db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))
    return waitgate.decide(p, cfg or p.config(), task, json.loads(task["result"]), now or time.time(),
                           last_run=last, **kw)


def _gate_set(p, tid, **fields):
    p.db.update_task(tid, result=json.dumps({**json.loads(p.db.task(tid)["result"]), **fields}))


def _running_job(tmp_path, name="job"):
    """A detached job's .rc path whose job lock is held (it still runs), and the open lock file."""
    import fcntl
    jobs = tmp_path / "jobs"
    jobs.mkdir(exist_ok=True)
    rc = jobs / f"{name}.rc"
    f = open(rc.with_suffix(".lock"), "a")
    fcntl.flock(f, fcntl.LOCK_EX)
    return rc, f


def test_the_wake_gate_rules_skip_only_what_is_mechanically_still_pending(env, monkeypatch, tmp_path):
    """A job still running, a lock held by a live holder, checks or a push still pending: a skip by
    rule. Once over, no rule skips: with no Jev the model wakes (fallback)."""
    p = make(env)
    from ttp import cli, lockprobe, push
    rc, lk = _running_job(tmp_path)
    tid = _gate_task(p, tmp_path, f"ttp detach --check {rc}")
    d = _gate_decide(p, tid)
    assert not d.wake and d.by == "rule" and "still runs with no exit code" in d.reason, d
    lk.close()
    rc.write_text("0\n")
    d = _gate_decide(p, tid)
    assert d.wake and d.by == "fallback", d

    lock_says = [(75, "held by task 9, alive")]
    monkeypatch.setattr(lockprobe, "answer", lambda p_, cfg, res, shared: lock_says[0])
    tid = _gate_task(p, tmp_path, "ttp lock --probe dev-a")
    d = _gate_decide(p, tid)
    assert not d.wake and d.by == "rule" and "the lock is still held (held by task 9, alive)" == d.reason, d
    lock_says[0] = (0, "free")
    assert _gate_decide(p, tid).wake

    checks = tmp_path / "checks-run"
    checks.mkdir()
    (checks / cli.CHECKS_PID).write_text(json.dumps({"pid": 1, "started": "x"}))
    alive = [True]
    monkeypatch.setattr(cli, "_checks_alive", lambda info: alive[0])
    tid = _gate_task(p, tmp_path, f"ttp checks --result {checks}")
    d = _gate_decide(p, tid)
    assert not d.wake and d.reason == "its checks still run", d
    alive[0] = False
    assert _gate_decide(p, tid).wake
    alive[0] = True
    (checks / cli.CHECKS_RC).write_text("0\n")   # ended: no rule holds it, whatever the pid says
    assert _gate_decide(p, tid).wake

    marker = {"status": "queued"}
    monkeypatch.setattr(push, "_read", lambda path: marker)
    tid = _gate_task(p, tmp_path, f"ttp push --result {tmp_path / 'push.json'}")
    d = _gate_decide(p, tid)
    assert not d.wake and d.reason == "its push is queued", d
    marker["status"] = "done"
    assert _gate_decide(p, tid).wake
    rows = p.db.q("SELECT decision, by FROM wake_gate ORDER BY id")
    assert [r["decision"] for r in rows] == ["skip", "wake", "skip", "wake", "skip", "wake", "wake", "skip", "wake"]


def test_the_wake_gate_skips_a_probe_pass_its_last_turn_already_saw(env, tmp_path):
    p = make(env)
    from ttp import waitgate
    out = "job finished rc=0 at 10:01:02"
    hand = {"status": "waiting", "waiting_for": "the job", "retry_when": "exit 0"}
    seen = waitgate.evidence(0, out)
    tid = _gate_task(p, tmp_path, "exit 0", note={"wake_gate": {"seen": seen, "key": waitgate.wait_key(hand)}},
                     woke="probe passed")
    # The same answer (its clock time masked): the last turn saw it and handed the same wait back.
    d = _gate_decide(p, tid, probe=(0, "job finished rc=0 at 10:31:40"))
    assert not d.wake and d.by == "rule" and "already saw" in d.reason, d
    d = _gate_decide(p, tid, probe=(0, "job finished rc=0; 5 new rows"))
    assert d.wake and d.by == "event" and d.reason == "its probe passed", d
    _gate_set(p, tid, waiting_for="another job")   # the last turn waited on something else
    assert _gate_decide(p, tid, probe=(0, out)).wake


@pytest.mark.parametrize("case", ["escalated", "woke", "spec", "changed", "answered", "failed_job"])
def test_the_wake_gate_wakes_at_once_on_an_event_even_when_a_rule_would_skip(env, monkeypatch, tmp_path, case):
    p = make(env)
    from ttp import waitgate
    monkeypatch.setattr(waitgate, "pending", lambda p_, cfg, probe: "its detached job still runs")
    tid = _gate_task(p, tmp_path, note={"spec_sha": "old"})
    assert not _gate_decide(p, tid, probe=(1, "running"), spec_sha="old").wake   # no event: the rule skips
    kw = {"probe": (1, "running"), "spec_sha": "old"}
    if case == "escalated":
        _gate_set(p, tid, escalated_wake=True)
    elif case == "woke":
        _gate_set(p, tid, woke="its checks failed (exit 1)")
    elif case == "spec":
        kw["spec_sha"] = "new"
    elif case == "changed":
        _gate_set(p, tid, wake_gate={"seen": waitgate.evidence(1, "running"), "log": [1]})
        kw["probe"] = (1, "running; 3 of 9 steps done")
    elif case == "answered":
        kw["probe"] = (0, "job finished rc=0")
    else:
        kw["probe"] = (4, "job finished rc=3")
    d = _gate_decide(p, tid, **kw)
    assert d.wake and d.by == "event", (case, d)


def test_the_wake_gate_cap_wakes_after_max_skips_or_max_skip_h(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import waitgate
    monkeypatch.setattr(waitgate, "pending", lambda p_, cfg, probe: "its detached job still runs")
    tid = _gate_task(p, tmp_path, ended_ago=3600)
    _gate_set(p, tid, wake_gate={"log": [1, 2, 3]})
    assert not _gate_decide(p, tid).wake
    _gate_set(p, tid, wake_gate={"log": [1, 2, 3, 4]})
    d = _gate_decide(p, tid)
    assert d.wake and d.by == "cap" and d.reason.startswith("4 skip(s)"), d
    tid = _gate_task(p, tmp_path, ended_ago=4 * 3600 + 5)
    d = _gate_decide(p, tid)
    assert d.wake and d.by == "cap", d
    cfg = p.config()
    cfg["waiting"]["wake_gate"] = {**cfg["waiting"]["wake_gate"], "max_skip_h": 8}
    assert not _gate_decide(p, tid, cfg=cfg).wake


def test_the_wake_gate_backoff_doubles_within_max_backoff_s_and_the_cap(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod, waitgate
    monkeypatch.setattr(waitgate, "pending", lambda p_, cfg, probe: "its detached job still runs")
    dm = dmod.Daemon(p.base)
    tid = _gate_task(p, tmp_path, ended_ago=60)
    delays = []
    for _ in range(4):
        task = p.db.task(tid)
        p.db.update_task(tid, not_before=time.time() - 1)
        before = time.time()
        assert dm._wake_gate(p.db.task(tid)) == (True, None)
        task = p.db.task(tid)
        delays.append(round((task["not_before"] - before) / 100) * 100)
        res = json.loads(task["result"])
        assert "woke" not in res and res["wake_gate"]["by"] == "rule" and "wake gate skipped a turn" in task["blocked_reason"]
    # retry_after_s 60 is below backoff_s: 600 s first, doubling, never past max_backoff_s.
    assert delays == [600, 1200, 2400, 3600], delays
    assert len(json.loads(p.db.task(tid)["result"])["wake_gate"]["log"]) == 4
    p.db.update_task(tid, not_before=time.time() - 1)
    skipped, note = dm._wake_gate(p.db.task(tid))
    assert not skipped and note["by"] == "cap" and len(note["skips"]) == 4, note
    # Near the cap the re-defer ends at it (60 s at least), not a full backoff later.
    tid = _gate_task(p, tmp_path, ended_ago=4 * 3600 - 300)
    d = _gate_decide(p, tid)
    assert not d.wake and 250 <= d.delay_s <= 300, d.delay_s
    cfg = p.config()
    cfg["waiting"]["wake_gate"] = {**cfg["waiting"]["wake_gate"], "max_backoff_s": 1000}
    tid = _gate_task(p, tmp_path)
    _gate_set(p, tid, wake_gate={"log": [1, 2]})
    assert _gate_decide(p, tid, cfg=cfg).delay_s == 1000


@pytest.mark.parametrize("jev, wake, by", [
    (_GateJev(0.1), False, "jev"),
    (_GateJev(0.9), True, "jev"),
    (_GateJev(error=TimeoutError("slow")), True, "fallback"),
    (_GateJev(None), True, "fallback"),
])
def test_the_wake_gate_asks_jev_when_no_rule_decides_and_wakes_when_it_cannot(env, tmp_path, jev, wake, by):
    p = make(env)
    tid = _gate_task(p, tmp_path)
    d = _gate_decide(p, tid, jev=jev, probe=(1, "not yet"))
    assert (d.wake, d.by) == (wake, by) and jev.calls == 1, d
    calls = p.db.q("SELECT * FROM jev_calls WHERE use='wake_gate'")
    assert len(calls) == (1 if by == "jev" else 0)
    if calls:
        assert d.jev_call == calls[0]["id"] and calls[0]["ref"] == f"task:{tid}"
        assert p.db.one("SELECT jev_call FROM wake_gate WHERE id=?", (d.log_id,))["jev_call"] == calls[0]["id"]
    # Its Jev use switched off: no call, the model wakes.
    p.set_config("jev.uses.wake_gate", "off")
    d = _gate_decide(p, tid, jev=_GateJev(0.1))
    assert d.wake and d.by == "fallback"


def test_the_wake_gate_off_or_failing_wakes_the_model_as_before(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod, waitgate
    monkeypatch.setattr(waitgate, "pending", lambda p_, cfg, probe: "its detached job still runs")
    dm = dmod.Daemon(p.base)
    tid = _gate_task(p, tmp_path)
    p.db.update_task(tid, not_before=time.time() - 1)
    cfg = p.config()
    cfg["waiting"]["wake_gate"] = {**cfg["waiting"]["wake_gate"], "enabled": False}
    assert _gate_decide(p, tid, cfg=cfg) is None
    dm.cfg = cfg
    assert dm._wake_gate(p.db.task(tid)) == (False, None)
    cfg["waiting"]["wake_gate"] = False
    assert dm._wake_gate(p.db.task(tid)) == (False, None)
    assert not p.db.q("SELECT id FROM wake_gate")
    dm.cfg = p.config()
    monkeypatch.setattr(waitgate, "decide", lambda *a, **k: 1 / 0)
    assert dm._wake_gate(p.db.task(tid)) == (False, None)
    assert f"task {tid}: wake gate failed, waking: ZeroDivisionError" in (p.logs / "daemon.log").read_text()
    assert _ready(p, tid)


def test_a_wake_keeps_the_probe_answer_it_came_on_for_the_gate(env, tmp_path):
    """_wake_waiting keeps the verdict it woke on (_woke_on): the gate compares it with what the last
    turn saw, so a pass the last turn already handed back is skipped with no model turn."""
    p = make(env)
    from ttp import daemon as dmod, waitgate
    dm = dmod.Daemon(p.base)
    out = "job finished rc=0"
    hand = {"status": "waiting", "waiting_for": "the job", "retry_when": "exit 0"}
    tid = _gate_task(p, tmp_path, "exit 0",
                     note={"wake_gate": {"seen": waitgate.evidence(0, out), "key": waitgate.wait_key(hand)}})
    now = time.time()
    dm._probe_rc[tid] = (0, now, "exit 0")
    dm._probe_out[tid] = (out, now, "exit 0")
    dm._wake_waiting(p.db.task(tid), "probe passed", now)
    assert tid not in dm._probe_rc and dm._woke_on[tid] == (0, now, "exit 0")
    assert dm._wake_gate(p.db.task(tid)) == (True, None)
    assert tid not in dm._woke_on and not _ready(p, tid)
    assert p.db.one("SELECT by, reason FROM wake_gate")["by"] == "rule"


@pytest.mark.parametrize("job_rc", [0, 3])
def test_a_gate_skipped_job_wait_still_takes_its_model_free_paths_once_the_job_ends(env, monkeypatch, tmp_path,
                                                                                   job_rc):
    """Skipped on 'job still running', then the job ends: exit 0 runs the on_pass_cmd with no model
    run; a failed job (probe exit 4) goes through _job_failed: an escalated wake at the task's tier."""
    p = make(env)
    from ttp import budget as bud, daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    dm = dmod.Daemon(p.base)
    rc, lk = _running_job(tmp_path)
    ran = tmp_path / "ran"
    cmd = (f"echo ran > {ran}; printf '{{\"status\":\"done\",\"summary\":\"job verified\"}}' > \"$TTP_RESULT\""
           if job_rc == 0 else None)
    tid, _ = _on_pass_cmd_task(p, tmp_path, cmd, probe=f"ttp detach --check {rc}")
    _settle_probe(dm, tid)
    assert dm._probe_rc[tid][0] == 1 and not _ready(p, tid)
    p.db.update_task(tid, not_before=time.time() - 1)
    assert dm._wake_gate(p.db.task(tid)) == (True, None)
    assert json.loads(p.db.task(tid)["result"])["wake_gate"]["reason"].endswith("still runs with no exit code")
    _settle_probe(dm, tid)   # the same "running" answer: it stays asleep
    assert not _ready(p, tid) and "woke" not in json.loads(p.db.task(tid)["result"])
    lk.close()
    rc.write_text(f"{job_rc}\n")
    _settle_on_pass(dm, tid)
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert len(p.db.q("SELECT id FROM runs WHERE task=?", (tid,))) == 1, "a model run was started"
    if job_rc == 0:
        assert ran.read_text() == "ran\n", "the on_pass_cmd did not run"
        assert task["status"] == "done" and res["run_status"] == "model-free", (task["status"], res)
    else:
        assert _ready(p, tid) and res["escalated_wake"] is True and "job finished rc=3" in res["woke"], res
        assert bud.wake_tier(task["tier"], res) == "standard"
        skipped, note = dm._wake_gate(task)
        assert not skipped and note["by"] == "event"


def test_gate_skipped_checks_that_pass_record_on_pass_model_free(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod, waitgate
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    dm = dmod.Daemon(p.base)
    tid, _ = _checks_wait_task(p, tmp_path, DONE_ON_PASS)
    prev = json.loads(p.db.task(tid)["result"])
    d = waitgate.Decision(False, "rule", "its checks still run", seen=waitgate.evidence(1, "checks still running"))
    d.log_id = waitgate.log(p.db, tid, d)
    p.db.update_task(tid, result=json.dumps(waitgate.skipped(prev, d, time.time())))
    _settle_probe(dm, tid)
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert task["status"] == "done" and res["run_status"] == "model-free", (task["status"], res)
    assert "recorded its on_pass (done) model-free" in (p.logs / "daemon.log").read_text()


def test_a_gate_skip_wakes_on_a_new_answer_no_other_path_handles(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod, waitgate
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    dm = dmod.Daemon(p.base)
    tid = _gate_task(p, tmp_path, "echo moved; exit 1")
    _gate_set(p, tid, wake_gate={"seen": waitgate.evidence(1, "still"), "log": [1]})
    _settle_probe(dm, tid)   # changed, but "not yet": it sleeps on
    assert not _ready(p, tid)
    _gate_set(p, tid, retry_when="echo broke; exit 2", wake_gate={"seen": waitgate.evidence(1, "still"), "log": [1]})
    _settle_probe(dm, tid)
    assert _ready(p, tid) and json.loads(p.db.task(tid)["result"])["woke"] == "its probe's answer changed (exit 2)"


@pytest.mark.parametrize("handed_back", [True, False])
def test_a_worker_finish_settles_the_wake_gate_decisions_behind_its_run(env, tmp_path, handed_back):
    p = make(env)
    from ttp import daemon as dmod, jevuse, waitgate
    from ttp.providers.base import RunUsage as Usage
    dm = dmod.Daemon(p.base)
    tid = _gate_task(p, tmp_path)
    call = jevuse.record(p.db, waitgate.USE, {"skip": True}, 0.0002, ref=f"task:{tid}")
    skip = waitgate.Decision(False, "jev", "jev needed=0.10", jev_call=call)
    skip_id = waitgate.log(p.db, tid, skip)
    wake_id = waitgate.log(p.db, tid, waitgate.Decision(True, "fallback", "no rule or Jev decided"))
    prev = json.loads(p.db.task(tid)["result"])
    note = {"wake_gate": {"log": wake_id, "by": "fallback", "skips": [skip_id], "seen": "", "key": waitgate.wait_key(prev)}}
    run_dir = tmp_path / "finish"
    run_dir.mkdir()
    out = ({k: prev[k] for k in ("status", "summary", "waiting_for", "retry_after_s", "retry_when")}
           if handed_back else {"status": "done", "summary": "job verified"})
    (run_dir / "result.json").write_text(json.dumps(out))
    p.db.update_task(tid, status="running")
    dm._finish_worker({"task": tid, "note": json.dumps(note)}, Usage(cost_usd=0.3), "ok", run_dir)
    rows = {r["id"]: r for r in p.db.q("SELECT * FROM wake_gate")}
    assert rows[wake_id]["outcome"] == ("miss" if handed_back else "right")
    assert rows[skip_id]["outcome"] == ("right" if handed_back else "miss")
    assert p.db.one("SELECT outcome FROM jev_calls WHERE id=?", (call,))["outcome"] == (
        "right" if handed_back else "wrong")


def test_the_daily_review_reports_the_wake_gate(env):
    p = make(env)
    from ttp import daemon as dmod, jevuse, waitgate
    dm = dmod.Daemon(p.base)
    assert waitgate.line(p.db, p.config()) == ""
    call = jevuse.record(p.db, waitgate.USE, {"skip": True}, 0.01, ref="task:1")
    waitgate.log(p.db, 1, waitgate.Decision(False, "rule", "its push is queued"), avoided_usd=0.4)
    waitgate.log(p.db, 1, waitgate.Decision(False, "jev", "jev needed=0.10", jev_call=call), avoided_usd=0.4)
    waitgate.log(p.db, 1, waitgate.Decision(True, "fallback", "no rule or Jev decided"))
    line = waitgate.line(p.db, p.config())
    assert line.startswith("Wait-wake gate over the last 7 d: 3 decisions, 2 skipped (1 by rules, 1 by Jev), 1 woke")
    assert "Jev 1 calls $0.01" in line and "estimated saving $0.80, net $0.79" in line, line
    s = {"name": "daily-review", "budget_usd_day": None, "last_run": None, "description": "review"}
    assert dm._schedule_llm(s, {}) == "queued"
    spec = p.db.one("SELECT spec FROM tasks WHERE origin='schedule'")["spec"]
    assert f"- {line}" in spec


# Deferred device tasks (coordinator.lull_note): a broker serializes device jobs, so a deferral that
# waits for a lull, an ack or a pause to end gets advice on its result note, never a rejection.
def test_the_coordinator_prompt_keeps_a_lull_for_disruptive_work_and_enqueues_device_jobs_now():
    rule = " ".join((RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text().split())
    assert "Device jobs behind a broker or cooperative queue: enqueue now, not after a lull or ack." in rule
    assert "Lulls (`start_when`) are for restarts, upgrade rollouts, reboots, power cycles." in rule
    assert "Work that would disturb others: run it in a lull" not in rule


@pytest.mark.parametrize("why", ["wait for a lull on the box", "until the other project acks",
                                 "after the other team's test pause ends", "once the box is idle"])
def test_a_deferred_device_task_waiting_for_a_lull_or_ack_gets_the_enqueue_now_advice(env, why):
    from ttp import coordinator as coord
    p = make(env)
    assert coord.apply(p, [{"type": "task_add", "title": "run the device tests", "spec": "Run the suite.",
                            "resources": ["board-device"], "start_after": "6h", "why": why}]) == []
    t = p.db.one("SELECT * FROM tasks WHERE title=?", ("run the device tests",))
    assert t["not_before"]   # applied as usual
    note = next(n for n in p.db.kv(coord.NOTES_KEY) if n.startswith(f"task_add: #{t['id']} deferred"))
    assert "device jobs behind a broker are enqueued now" in note and "a lull is only for disruptive work" in note
    # The same deferral set by task_update advises too.
    tid = p.db.add_task("run the bench", "Bench it.", kind="work", labels=["resource:board-device"])
    assert coord.apply(p, [{"type": "task_update", "id": tid, "start_after": "3h", "why": why}]) == []
    assert p.db.task(tid)["not_before"]
    assert any(n.startswith(f"task_update: #{tid} deferred on a device resource") for n in p.db.kv(coord.NOTES_KEY))


@pytest.mark.parametrize("title,resources,why", [
    ("roll out the broker upgrade", ["board-device"], "wait for a lull"),
    ("run the device tests", ["board-device"], "restart the service in a quiet hour"),
    ("power cycle the board", ["board-device"], "once the box is idle"),
    ("run the docs build", ["build-box"], "wait for a lull"),
    ("run the device tests", ["board-device"], "after the nightly window opens"),
])
def test_no_lull_advice_for_disruptive_work_a_task_without_a_device_or_another_why(env, title, resources, why):
    from ttp import coordinator as coord
    p = make(env)
    assert coord.apply(p, [{"type": "task_add", "title": title, "spec": "Do it.", "resources": resources,
                            "start_after": "6h", "why": why}]) == []
    assert not any("deferred on a device resource" in n for n in p.db.kv(coord.NOTES_KEY) or [])
    # Not deferred: no advice whatever its why.
    assert coord.apply(p, [{"type": "task_add", "title": "now", "spec": "Do it.", "resources": ["board-device"],
                            "why": "wait for a lull"}]) == []
    assert not any("deferred on a device resource" in n for n in p.db.kv(coord.NOTES_KEY) or [])
    # The control: the same device task waiting for a lull, with no disruptive word, is advised.
    assert coord.apply(p, [{"type": "task_add", "title": "control", "spec": "Run it.", "resources": ["board-device"],
                            "start_when": "test -e /tmp/ttp-lull-control", "why": "wait for a lull"}]) == []
    assert sum("deferred on a device resource" in n for n in p.db.kv(coord.NOTES_KEY)) == 1
