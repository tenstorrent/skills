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
    assert not d.wake and d.reason.startswith("its checks still run"), d
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


class _GateWatcher:
    """Stands in for babysit.Babysitter: the watcher's verb (or None: it fell back) and the Jev behind it."""

    def __init__(self, verb=None, delay_s=None, jev=None, model="example-org/watcher"):
        self.verb, self.delay_s, self.jev, self.model, self.calls = verb, delay_s, jev, model, 0

    def enabled(self):
        return True

    def decision(self, state, question, target="coordinator", purpose="decide"):
        self.calls += 1
        if self.verb is None:
            return None
        return {"verb": self.verb, "tier": "light", "parsed": True, "reason": "the build still runs",
                "model": self.model, **({"delay_s": self.delay_s} if self.delay_s else {})}


def test_the_wake_gate_cap_asks_the_watcher_which_may_skip_until_the_hard_ceiling(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import waitgate
    monkeypatch.setattr(waitgate, "pending", lambda p_, cfg, probe: "its detached job still runs")
    tid = _gate_task(p, tmp_path, ended_ago=4 * 3600 + 5)
    w = _GateWatcher("skip", delay_s=120)
    d = _gate_decide(p, tid, jev=w)   # at the soft cap the watcher, not the model-free rule, decides
    assert not d.wake and d.by == "babysit" and w.calls == 1 and d.reason.startswith("at the cap (0 skip(s)"), d
    assert d.delay_s == 600.0   # its next look, but at least backoff_s: no re-ask each minute
    row = p.db.one("SELECT decision, by FROM wake_gate WHERE id=?", (d.log_id,))
    assert dict(row) == {"decision": "skip", "by": "babysit"}
    tid = _gate_task(p, tmp_path, ended_ago=4 * 3600 + 5)
    _gate_set(p, tid, wake_gate={"log": [1, 2, 3, 4, 5, 6]})
    d = _gate_decide(p, tid, jev=_GateWatcher("wake"))
    assert d.wake and d.by == "babysit" and d.extra["tier"] == "light", d
    # The watcher gave no answer and nothing stands behind it: the cap wakes as before.
    tid = _gate_task(p, tmp_path, ended_ago=4 * 3600 + 5)
    d = _gate_decide(p, tid, jev=_GateWatcher(None))
    assert d.wake and d.by == "cap" and "the watcher gave no answer" in d.reason, d
    assert _gate_decide(p, _gate_task(p, tmp_path, ended_ago=4 * 3600 + 5)).by == "cap"   # no watcher at all
    # A probe that answered at the cap takes its model-free path: no watcher skip.
    w = _GateWatcher("skip")
    d = _gate_decide(p, _gate_task(p, tmp_path, ended_ago=4 * 3600 + 5), jev=w, probe=(4, "job failed"))
    assert d.wake and d.by == "cap" and w.calls == 0
    # Near the hard ceiling a skip ends at it; past it the task wakes with no question.
    d = _gate_decide(p, _gate_task(p, tmp_path, ended_ago=24 * 3600 - 300), jev=_GateWatcher("skip", delay_s=3600))
    assert not d.wake and 250 <= d.delay_s <= 300, d.delay_s
    w = _GateWatcher("skip")
    d = _gate_decide(p, _gate_task(p, tmp_path, ended_ago=24 * 3600 + 5), jev=w)
    assert d.wake and d.by == "cap" and "hard ceiling" in d.reason and w.calls == 0, d
    cfg = p.config()
    cfg["waiting"]["wake_gate"] = {**cfg["waiting"]["wake_gate"], "cap_hard_h": 48}
    assert not _gate_decide(p, _gate_task(p, tmp_path, ended_ago=24 * 3600 + 5), cfg=cfg, jev=_GateWatcher("skip")).wake


def test_a_wake_regated_before_its_turn_starts_is_the_same_wake_with_no_new_row_or_question(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod, waitgate
    monkeypatch.setattr(waitgate, "pending", lambda p_, cfg, probe: "its detached job still runs")
    dm = dmod.Daemon(p.base)
    dm.jev = w = _GateWatcher("wake")
    tid = _gate_task(p, tmp_path, ended_ago=4 * 3600 + 5)
    p.db.update_task(tid, not_before=time.time() - 1)
    notes = [dm._wake_gate(p.db.task(tid)) for _ in range(5)]   # the slots stay full: five ticks, no turn
    assert all(not skipped and n["by"] == "babysit" and n["tier"] == "light" for skipped, n in notes), notes
    assert len({n["log"] for _, n in notes}) == 1 and w.calls == 1
    assert p.db.one("SELECT count(*) n FROM wake_gate WHERE task=?", (tid,))["n"] == 1
    # The watcher fell back: the cap's wake is kept the same way, not re-logged every tick.
    w.verb = None
    tid = _gate_task(p, tmp_path, ended_ago=4 * 3600 + 5)
    p.db.update_task(tid, not_before=time.time() - 1)
    for _ in range(3):
        assert dm._wake_gate(p.db.task(tid))[1]["by"] == "cap"
    assert p.db.one("SELECT count(*) n FROM wake_gate WHERE task=?", (tid,))["n"] == 1 and w.calls == 2
    # A new hand-off (its turn ran) is a new decision.
    _gate_set(p, tid, waiting_since=time.time())
    dm._wake_gate(p.db.task(tid))
    assert p.db.one("SELECT count(*) n FROM wake_gate WHERE task=?", (tid,))["n"] == 2


def test_the_wake_gates_jev_decision_names_the_model_that_answered(env, tmp_path):
    p = make(env)
    from ttp import waitgate
    haiku = _GateJev(0.1)
    haiku.last_by = "example-org/haiku-model"   # cheapdecide.Fallback after Jev was off: Haiku answered
    tid = _gate_task(p, tmp_path)
    d = _gate_decide(p, tid, jev=_GateWatcher(None, jev=haiku), probe=(1, "not yet"))
    assert not d.wake and d.by == "jev" and d.reason == "jev needed=0.10 (example-org/haiku-model)", d
    assert d.note({}, [])["model"] == "example-org/haiku-model"
    assert json.loads(p.db.one("SELECT decision FROM jev_calls WHERE id=?", (d.jev_call,))["decision"])["model"] \
        == "example-org/haiku-model"
    jev = _GateJev(0.1)
    jev.last_by = "jev"
    d = _gate_decide(p, _gate_task(p, tmp_path), jev=jev, probe=(1, "not yet"))
    assert d.reason == "jev needed=0.10" and d.extra["model"] == "jev"


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
    assert line.startswith("Wait-wake gate over the last 7 d: 3 decisions, 2 skipped (1 by rules, 0 by the watcher, 1 by Jev), 1 woke")
    assert "Jev 1 calls $0.01" in line and "estimated saving $0.80, net $0.79" in line, line
    s = {"name": "daily-review", "budget_usd_day": None, "last_run": None, "description": "review"}
    assert dm._schedule_llm(s, {}) == "queued"
    spec = p.db.one("SELECT spec FROM tasks WHERE origin='schedule'")["spec"]
    assert f"- {line}" in spec


# Deferred device tasks (coordinator.lull_note): a broker serializes device jobs, so a deferral that
# waits for a lull, an ack or a pause to end gets advice on its result note, never a rejection.
# User msg #1571: a reset, reboot or power cycle of a held box runs when needed, never after a lull.
def test_the_coordinator_prompt_enqueues_device_jobs_now_and_endorses_no_lull_for_reboots():
    rule = " ".join((RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text().split())
    assert "Device jobs: enqueue now; reset, reboot, power-cycle held boxes as needed." in rule
    assert "No lull wait: only user holds, allowlists, safety gates stop it. Shared services: ask test." in rule
    assert "Work that would disturb others: run it in a lull" not in rule
    assert not re.search(r"lulls?\b[^.]*\b(reboot|power[- ]cycle)", rule, re.I), "a lull endorsed for reboots"


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
    assert "device jobs behind a broker are enqueued now" in note and "no reason to wait" in note
    assert "lull is only for" not in note and not re.search(r"reboot|power cycle|restart", note), note   # user msg #1571
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


_MATCH = {"pass": "ALL DONE", "fail": ["FAILED", "Traceback"], "wait": "still running"}


def _match_task(p, tmp_path, monkeypatch, out, probe=None, **extra):
    """A daemon and a task asleep on a probe that prints `out` (or runs `probe`) and exits 0, with a
    probe_match and an on_pass."""
    from ttp import daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    dm = dmod.Daemon(p.base)
    hand = {"probe_match": _MATCH, "on_pass": {"status": "done", "summary": "The job ended: {probe_out}"}, **extra}
    tid = _gate_task(p, tmp_path, probe or f"echo '{out}'", **hand)
    return dm, tid


def _match_notes(tmp_path, tid):
    f = tmp_path / f"gate-run-{tid}" / "progress.md"
    return f.read_text() if f.exists() else ""


def test_a_probe_match_wait_keeps_the_task_asleep_and_a_timer_wake_re_defers_it(env, monkeypatch, tmp_path):
    p = make(env)
    dm, tid = _match_task(p, tmp_path, monkeypatch, "3 of 11 trials done, still running")
    _settle_probe(dm, tid)
    task = p.db.task(tid)
    assert not _ready(p, tid) and "woke" not in json.loads(task["result"]), task["result"]
    assert "probe_match: still waiting, settled model-free: 3 of 11" in _match_notes(tmp_path, tid)
    # Its timer runs out with new progress that still says "wait": the gate re-defers with a back-off.
    _gate_set(p, tid, retry_when="echo '4 of 11 trials done, still running'")
    _settle_probe(dm, tid)
    assert not _ready(p, tid) and _match_notes(tmp_path, tid).count("still waiting") == 2
    p.db.update_task(tid, not_before=time.time() - 1)
    skipped, _ = dm._wake_gate(p.db.task(tid))
    assert skipped and (p.db.task(tid)["not_before"] or 0) > time.time() + 60
    row = p.db.one("SELECT * FROM wake_gate ORDER BY id DESC LIMIT 1")
    assert row["decision"] == "skip" and "probe_match wait" in row["reason"], dict(row)


def test_a_probe_match_pass_records_on_pass_with_the_probe_output_model_free(env, monkeypatch, tmp_path):
    p = make(env)
    dm, tid = _match_task(p, tmp_path, monkeypatch, "ALL DONE: 11 of 11 trials")
    _settle_probe(dm, tid)
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert task["status"] == "done" and res["run_status"] == "model-free", (task["status"], res)
    assert res["summary"] == "The job ended: ALL DONE: 11 of 11 trials", res["summary"]
    assert "its output matched its probe_match pass" in (p.logs / "daemon.log").read_text()
    assert "probe_match: pass, recorded its on_pass model-free" in _match_notes(tmp_path, tid)


def test_a_probe_match_fail_wakes_the_task_at_its_own_tier_with_the_output(env, monkeypatch, tmp_path):
    p = make(env)
    dm, tid = _match_task(p, tmp_path, monkeypatch, "trial 3 FAILED: exit 2")
    _settle_probe(dm, tid)
    res = json.loads(p.db.task(tid)["result"])
    assert _ready(p, tid) and res["escalated_wake"] and res["wake_tier"] == "standard", res
    assert res["woke"].startswith("probe passed; its output matched its probe_match fail:\ntrial 3 FAILED"), res["woke"]
    assert "probe_match: fail, waking the task" in _match_notes(tmp_path, tid)


@pytest.mark.parametrize("out, said", [
    ("ALL DONE, but trial 3 FAILED", "its output matched pass and fail of its probe_match, so it is unclear"),
    ("ALL DONE yet still running", "its output matched pass and wait of its probe_match, so it is unclear"),
    ("something else entirely", "its output matched none of its probe_match")])
def test_an_unclear_probe_match_still_wakes_the_model(env, monkeypatch, tmp_path, out, said):
    p = make(env)
    dm, tid = _match_task(p, tmp_path, monkeypatch, out)
    _settle_probe(dm, tid)
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert _ready(p, tid) and task["status"] == "queued" and said in res["woke"], res
    skipped, note = dm._wake_gate(task)
    assert not skipped and note["by"] == "event", note


def test_a_probe_match_reads_only_a_probe_that_exits_0(env, monkeypatch, tmp_path):
    p = make(env)
    dm, tid = _match_task(p, tmp_path, monkeypatch, "ALL DONE")
    _gate_set(p, tid, retry_when="echo 'ALL DONE'; exit 1")
    _settle_probe(dm, tid)
    assert not _ready(p, tid) and p.db.task(tid)["status"] == "queued"
    _gate_set(p, tid, retry_when="echo 'trial FAILED'; exit 1")
    _settle_probe(dm, tid)
    assert not _ready(p, tid) and "woke" not in json.loads(p.db.task(tid)["result"])


def test_a_probe_match_pass_with_an_on_pass_cmd_leaves_the_command_to_decide(env, monkeypatch, tmp_path):
    p = make(env)
    dm, tid = _match_task(p, tmp_path, monkeypatch, "ALL DONE", on_pass_cmd="exit 3")
    started = []
    monkeypatch.setattr(dm, "_start_on_pass", lambda task, now, **kw: started.append(task["id"]) or True)
    _settle_probe(dm, tid)
    assert started == [tid] and p.db.task(tid)["status"] == "queued"


@pytest.mark.parametrize("raw", [
    "ALL DONE", {}, {"pass": "x", "done": "y"}, {"pass": []}, {"pass": ""}, {"fail": ["ok", 3]},
    {"wait": ["x"] * 9}, {"pass": "x" * 201}])
def test_a_malformed_probe_match_is_refused(raw):
    from ttp import probematch
    assert probematch.problem(raw)
    assert probematch.verdict({"probe_match": raw}, "ALL DONE x ok") is None


def test_a_waiting_hand_off_with_a_malformed_probe_match_drops_it_with_an_event(env, tmp_path):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    tid = p.db.add_task("watch the job", "s", kind="work", tier="standard", origin="user")
    t = _reply_wait_hand_off(p, d, tmp_path, tid, waiting_for="the job", retry_when="exit 1",
                             probe_match={"pass": "x", "done": "y"})
    res = json.loads(t["result"])
    assert res["status"] == "waiting" and "probe_match" not in res, res
    ev = p.db.one("SELECT text FROM events WHERE kind='handoff_probe_refused' AND task=?", (tid,))
    assert ev and "probe_match was not stored: probe_match has unknown keys: done" in ev["text"], ev


_PAD = "head -c 600 /dev/zero | tr '\\0' x; echo"   # 600 chars of output, past the 400-char wait-note clip


def test_a_probe_match_pass_word_followed_by_600_chars_still_settles_pass(env, monkeypatch, tmp_path):
    p = make(env)
    dm, tid = _match_task(p, tmp_path, monkeypatch, "", probe=f"echo 'ALL DONE: 11 of 11 trials'; {_PAD}")
    _settle_probe(dm, tid)
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert task["status"] == "done" and res["run_status"] == "model-free", (task["status"], res)
    assert res["summary"].startswith("The job ended: ALL DONE: 11 of 11 trials\n" + "x" * 600), res["summary"]


def test_a_probe_match_fail_word_far_before_a_wait_word_wakes_the_model_as_unclear(env, monkeypatch, tmp_path):
    p = make(env)
    dm, tid = _match_task(p, tmp_path, monkeypatch, "x")
    _gate_set(p, tid, retry_when=f"echo 'trial 3 FAILED'; {_PAD}; echo 'still running'")
    _settle_probe(dm, tid)
    res = json.loads(p.db.task(tid)["result"])
    assert _ready(p, tid) and "matched fail and wait of its probe_match, so it is unclear" in res.get("woke", ""), res
    # The wake gate reads the same raw tail: no "still waiting" skip on it.
    skipped, note = dm._wake_gate(p.db.task(tid))
    assert not skipped, note


def test_a_probe_match_wait_word_far_before_the_end_keeps_its_window_of_4000_chars(env, monkeypatch, tmp_path):
    from ttp import probematch
    p = make(env)
    dm, tid = _match_task(p, tmp_path, monkeypatch, "x")
    _gate_set(p, tid, retry_when=f"echo 'still running'; {_PAD}")
    _settle_probe(dm, tid)
    assert not _ready(p, tid) and "woke" not in json.loads(p.db.task(tid)["result"])
    assert len(dm._match_out[tid][0]) <= probematch.OUT_CHARS
    # Past the window the wait word is no longer read: the model wakes ("matched none").
    _gate_set(p, tid, retry_when=f"echo 'still running'; head -c {probematch.OUT_CHARS + 10} /dev/zero | tr '\\0' x")
    _settle_probe(dm, tid)
    assert _ready(p, tid) and "matched none of its probe_match" in json.loads(p.db.task(tid)["result"])["woke"]


def test_a_probe_stall_re_check_pass_on_a_probe_match_wait_output_sleeps_on(env, monkeypatch, tmp_path):
    p = make(env)
    dm, tid = _match_task(p, tmp_path, monkeypatch, "3 of 11 trials done, still running")
    _settle_probe(dm, tid)
    assert not _ready(p, tid)
    # The re-check reads only the exit code: its last output still says it waits.
    dm._probe_stall_act(p.db.task(tid), "passed", "")
    task = p.db.task(tid)
    assert task["status"] == "queued" and not _ready(p, tid) and "woke" not in json.loads(task["result"]), task["result"]


def test_a_gate_skip_sleeps_on_a_new_answer_that_fails_for_gh_login_while_the_host_says_logged_out(
        env, monkeypatch, tmp_path):
    """`ttp ci` moving from not yet to gh's exit 4 during a logout wakes no model per task: the host's
    gh-auth alert covers it. Once the host check says logged in, the same answer wakes as before."""
    p = make(env)
    from ttp import daemon as dmod, ghauth, waitgate
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    home = tmp_path / "ghhome"
    (home / "gh").mkdir(parents=True)
    for var in ("HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("GH_CONFIG_DIR", str(home / "gh"))   # never the real gh login
    dm = dmod.Daemon(p.base)
    ghauth._write(None, {"ok": False, "since": time.time(), "checked_at": time.time(), "error": "x"})
    tid = _gate_task(p, tmp_path, "echo 'broken: gh is not logged in'; exit 4")
    _gate_set(p, tid, wake_gate={"seen": waitgate.evidence(1, "running: ci"), "log": [1]})
    _settle_probe(dm, tid)
    assert not _ready(p, tid), json.loads(p.db.task(tid)["result"]).get("woke")
    ghauth._write(None, {"ok": True, "checked_at": time.time()})
    _gate_set(p, tid, wake_gate={"seen": waitgate.evidence(1, "running: ci"), "log": [1]})
    _settle_probe(dm, tid)
    assert _ready(p, tid) and json.loads(p.db.task(tid)["result"])["woke"] == "its probe's answer changed (exit 4)"


# Check plans (waketier: a waiting hand-off's planned checks) and their daemon, gate and coordinator hooks.

def test_a_check_plan_puts_an_interim_check_soon_and_one_at_the_measured_end(env):
    p = make(env)
    from ttp import waketier as wt
    cfg, since = p.config(), 1_000_000.0
    job = {"retry_when": "ttp detach --check /tmp/j.rc"}
    cp = wt.plan(p.db, cfg, job, since, now=since)
    assert cp["kind"] == "job" and cp["at"] == [since + 540, since + 3600] and cp["done"] == 0, cp   # unmeasured: fallback
    assert (cp["tier"], cp["by"], cp["src"]) == ("light", "jev", "default")
    for n in (600, 700, 800):   # three measured checks waits: the end is their median
        wt.record_wait(p.db, {"retry_when": "ttp checks --result /r", "waiting_since": since}, since + n)
    cp = wt.plan(p.db, cfg, {"retry_when": "ttp checks --result /r2"}, since, now=since)
    assert cp["at"] == [since + 120, since + 700] and cp["measured"] == 3, cp
    assert wt.plan(p.db, cfg, {"waiting_for": "x"}, since) is None, "no retry_when: no plan"
    assert wt.plan(p.db, {"waiting": {"check_plan": False}}, job, since) is None
    over = wt.plan(p.db, {"waiting": {"check_plan": False}}, job, since, {"checks": 1, "by": "model"}, now=since)
    assert over["at"] == [since + 540] and over["by"] == "model" and over["src"] == "coordinator", "an override plans anyway"
    assert wt.plan(p.db, cfg, job, since, {"checks": 3}, now=since)["at"] == [since + 540, since + 3600, since + 5400]
    assert wt.plan(p.db, cfg, job, since, {"checks": 0}, now=since)["at"] == []
    assert wt.plan(p.db, cfg, job, since, {"at_s": [60.0]}, now=since + 100)["done"] == 1, "a past check counts as done"
    # due: each check once, in order, named by where it falls against the expected end
    prev = {wt.PLAN: wt.plan(p.db, cfg, job, since, {"checks": 3}, now=since)}
    assert wt.due(prev, since + 500) is None and wt.next_at(prev) == since + 540
    seen = []
    for t in (since + 600, since + 3700, since + 5500):
        chk = wt.due(prev, t)
        seen.append(chk["what"])
        prev = {wt.PLAN: chk["plan"]}
    assert seen == ["interim", "end", "late"] and wt.due(prev, since + 99999) is None


def test_the_check_plan_override_keeps_only_known_fields():
    from ttp import waketier as wt
    assert wt.override(None) is None and wt.override("3") is None
    assert wt.override({"checks": 99, "at_s": [600, -5, "x", 60], "tier": "huge", "by": "jev", "x": 1}) == \
        {"checks": 12, "at_s": [60.0, 600.0], "by": "jev"}
    assert wt.override({"checks": True}) is None and wt.override({}) is None, "a bool is no count; {} clears"


def test_a_planned_check_wakes_once_at_its_tier_and_the_same_wait_keeps_its_plan(env, monkeypatch, tmp_path):
    """The waiting hand-off gets a plan; a due check on a "not yet" probe wakes the task light as a
    plan_check; its run handing back the same wait keeps the plan (no new checks) and counts no stale wake."""
    p = make(env)
    from ttp import budget as bud, daemon as dmod, waketier as wt
    d, tid, flag, runs, idle = _deferred_flow(p, monkeypatch, tmp_path / "plan")
    res = json.loads(p.db.task(tid)["result"])
    cp = res[wt.PLAN]
    assert cp["kind"] == "probe" and len(cp["at"]) == 2 and cp["done"] == 0, cp
    assert p.db.task(tid)["not_before"] > time.time() + 3000, "the hand-off's own timer is left alone"
    _settle_probe(d, tid)
    assert idle(), "not due yet: it sleeps"
    _gate_set(p, tid, **{wt.PLAN: {**cp, "at": [time.time() - 1, cp["at"][1]]}})
    d._probe_rc[tid] = (1, time.time(), res["retry_when"])
    d.probe_waiting()
    t = p.db.task(tid)
    res = json.loads(t["result"])
    assert t["not_before"] <= time.time() and res[wt.KEY] == "plan_check" and res[wt.PLAN]["done"] == 1, res
    assert res[wt.PLAN_BY] == "jev" and "planned interim check (1 of 2)" in res["woke"]
    assert bud.wake_tier("deep", res) == "light"
    d.jev = _GateJev(needed=0.9)   # Jev judges the check worth a turn (with no verdict it would sleep)
    assert _run_until(d, p, lambda: runs() == 2 and idle()), "the planned check did not run and sleep again"
    res = json.loads(p.db.task(tid)["result"])
    assert res[wt.PLAN]["done"] == 1 and res[wt.PLAN]["at"][1] == cp["at"][1], "the same wait keeps its plan"
    assert res.get("stale_wakes", 0) == 0 and wt.PLAN_BY not in res
    d._probe_rc[tid] = (1, time.time(), res["retry_when"])
    d.probe_waiting()
    assert idle(), "the end check is not due yet"


@pytest.mark.parametrize("rc", [0, 2])
def test_a_planned_check_needs_a_not_yet_answer_since_the_wait_began(env, tmp_path, rc):
    p = make(env)
    from ttp import daemon as dmod, waketier as wt
    dm = dmod.Daemon(p.base)
    tid = _gate_task(p, tmp_path)
    prev = json.loads(p.db.task(tid)["result"])
    _gate_set(p, tid, **{wt.PLAN: wt.plan(p.db, p.config(), prev, prev["waiting_since"], {"at_s": [1.0]}, now=0)})
    task = p.db.task(tid)
    assert not dm._plan_check(task, json.loads(task["result"]), time.time()), "no answer yet"
    dm._probe_rc[tid] = (1, prev["waiting_since"] - 5, "exit 1")
    assert not dm._plan_check(task, json.loads(task["result"]), time.time()), "an answer from before the wait"
    dm._probe_rc[tid] = (rc, time.time(), "exit 1")
    assert not dm._plan_check(task, json.loads(task["result"]), time.time()), "a pass or a broken probe takes its own path"


@pytest.mark.parametrize("by, wake, gate_by", [("model", True, "event"), ("jev", False, "jev"), ("plain", False, "rule")])
def test_the_wake_gate_judges_a_planned_check_by_its_plan(env, tmp_path, by, wake, gate_by):
    """model: always a turn; jev: Jev judges even while the job runs; plain: a running job is no turn."""
    p = make(env)
    rc, lk = _running_job(tmp_path)
    tid = _gate_task(p, tmp_path, f"ttp detach --check {rc}")
    _gate_set(p, tid, woke="its planned interim check (1 of 2): ...", wake_reason="plan_check", plan_by=by, plan_tier="light")
    jev = _GateJev(needed=0.05)
    d = _gate_decide(p, tid, jev=jev)
    assert (d.wake, d.by) == (wake, gate_by), d
    assert jev.calls == (1 if by == "jev" else 0)
    from ttp import waitgate
    assert not {"plan_by", "plan_tier", "woke"} & set(waitgate.skipped(json.loads(p.db.task(tid)["result"]), d, time.time()))
    lk.close()


class _NoKeyJev(_GateJev):
    def enabled(self):
        return False


@pytest.mark.parametrize("jev, off", [(None, False), (_NoKeyJev(needed=0.9), False), (_GateJev(needed=0.9), True),
                                      (_GateJev(error=RuntimeError("down")), False), (_GateJev(), False)])
def test_a_jev_planned_check_without_a_jev_verdict_takes_the_plain_rule(env, tmp_path, jev, off):
    """No Jev (no key, its use switched off, an error, no answer): a planned check is no event, so a
    running job is a skip by rule, never the gate's paid always-wake fallback."""
    from ttp import jevuse, waitgate
    p = make(env)
    rc, lk = _running_job(tmp_path)
    tid = _gate_task(p, tmp_path, f"ttp detach --check {rc}")
    if off:
        p.db.set_kv(jevuse.OFF_KEY, {waitgate.USE: {"at": time.time(), "why": "test"}})
    _gate_set(p, tid, woke="its planned interim check (1 of 2): ...", wake_reason="plan_check", plan_by="jev", plan_tier="light")
    d = _gate_decide(p, tid, jev=jev)
    assert (d.wake, d.by) == (False, "rule") and "still runs" in d.reason, d
    lk.close()


@pytest.mark.parametrize("by", ["jev", "plain"])
def test_a_planned_check_no_rule_or_jev_judged_sleeps_and_brings_no_cap_closer(env, tmp_path, by):
    """A probe no rule reads and no Jev: a planned check sleeps (its pass or failure still wakes the
    task), and its skips do not count toward max_skips; a plain due wake still takes the fallback."""
    from ttp import waitgate
    p = make(env)
    tid = _gate_task(p, tmp_path, "test -e /nonexistent/marker")
    _gate_set(p, tid, wake_gate={"log": [1, 2, 3], "plan_skips": 0})
    plan = dict(woke="its planned end check (2 of 2): ...", wake_reason="plan_check", plan_by=by, plan_tier="light")
    _gate_set(p, tid, **plan)
    d = _gate_decide(p, tid, jev=None)
    assert (d.wake, d.by) == (False, "rule") and "sleeps" in d.reason, d
    _gate_set(p, tid, **waitgate.skipped(json.loads(p.db.task(tid)["result"]), d, time.time()))
    gate = json.loads(p.db.task(tid)["result"])["wake_gate"]
    assert len(gate["log"]) == 4 and gate["plan_skips"] == 1, gate
    _gate_set(p, tid, woke="", wake_reason="", plan_by=None, plan_tier=None)
    d = _gate_decide(p, tid, jev=None)
    assert (d.wake, d.by) == (True, "fallback"), "3 counted skips: below the cap, and no plan: the fallback"
    _gate_set(p, tid, wake_gate={**gate, "plan_skips": 0})
    assert _gate_decide(p, tid, jev=None).by == "cap"


def test_the_coordinator_sets_and_clears_a_tasks_check_plan(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord, waketier as wt
    tid = _gate_task(p, tmp_path)
    nb = p.db.task(tid)["not_before"]
    assert coord.apply(p, [{"type": "task_update", "id": tid,
                            "check_plan": {"checks": 1, "at_s": [1800], "tier": "standard", "by": "model"}}]) == []
    res = json.loads(p.db.task(tid)["result"])
    assert res[wt.PLAN_SET] == {"checks": 1, "at_s": [1800.0], "tier": "standard", "by": "model"}
    assert res[wt.PLAN]["at"] == [round(res["waiting_since"] + 1800)] and res[wt.PLAN]["src"] == "coordinator"
    assert res["status"] == "waiting" and p.db.task(tid)["not_before"] == nb
    notes = coord.apply(p, [{"type": "task_update", "id": tid, "check_plan": {}}])
    assert notes == [], notes
    res = json.loads(p.db.task(tid)["result"])
    assert wt.PLAN_SET not in res and res[wt.PLAN]["src"] == "default"


def test_a_passed_probe_records_its_wait_for_the_daily_review(env, tmp_path):
    p = make(env)
    from ttp import daemon as dmod, waketier as wt
    dm = dmod.Daemon(p.base)
    tid = _gate_task(p, tmp_path, "ttp push --result /x", ended_ago=300)
    dm._probe_passed(p.db.task(tid), time.time())
    assert wt.median_s(p.db, "push") == (300.0, 1)
    lines = wt.plan_lines(p.db, p.config())
    assert lines[0].startswith("check plans on: interim 0.15") and "push waits: median 5 min over 1" in "\n".join(lines)


@pytest.mark.parametrize("losing", ["net", "idle"])
def test_the_watchers_free_skips_never_keep_a_losing_jev_wake_gate_use_switched_on(env, losing):
    """The chat watcher records its decisions under Jev's use (by 'babysit'), free and with the wake
    it avoided: they are counted apart, so Jev's review judges Jev's own calls only."""
    p = make(env)
    from ttp import jevuse, waitgate
    cfg = p.config()
    now = time.time()
    for i in range(40):   # the watcher's skips: free, each avoiding a wake, each changing the rules' decision
        jevuse.record(p.db, waitgate.USE, {"task": i, "by": "babysit", "model": "m", "verb": "skip", "tier": "",
                                           "skip": True}, 0.0, avoided_usd=0.4, ref=f"task:{i}", changed=True, now=now)
    for i in range(jevuse.MIN_CALLS if losing == "net" else jevuse.IDLE_CALLS):
        jevuse.record(p.db, waitgate.USE, {"task": i, "needed": 0.9, "skip": False}, 0.01, ref=f"task:{i}",
                      changed=False if losing == "idle" else None, now=now)
    s = jevuse.stats(p.db, cfg, now)[waitgate.USE]
    assert s["calls"] == (jevuse.MIN_CALLS if losing == "net" else jevuse.IDLE_CALLS) and s["saved"] == 0
    assert jevuse.watcher_stats(p.db, cfg, now)[waitgate.USE]["calls"] == 40
    assert [u for u, _ in jevuse.review(p.db, cfg, now)] == [waitgate.USE]
    assert not jevuse.allowed(p.db, cfg, waitgate.USE)
    assert any(line.startswith("chat watcher, wait-wake gating [wake_gate]: 40 decisions (not Jev's)")
               for line in jevuse.lines(p.db, cfg, now))
    waitgate.log(p.db, 1, waitgate.Decision(False, "babysit", "babysit m: skip"), avoided_usd=0.4)
    line = waitgate.line(p.db, cfg, now)
    n = jevuse.MIN_CALLS if losing == "net" else jevuse.IDLE_CALLS
    assert f"Jev {n} calls ${n * 0.01:.2f}" in line and "the watcher 40 calls (free)" in line, line


def _probe_dispatch(p, d, monkeypatch):
    """Dispatch once with start_run stood in: each start inserts a run row with the note it was given.
    Returns the (prompt, note) of each start."""
    from ttp import budget as bud
    started = []

    def start(role, prompt, provider, tier, cwd, **k):
        started.append((prompt, k.get("note") or {}))
        p.db.x("INSERT INTO runs(task,role,provider,started,ended,status,note) VALUES(?,?,?,?,?,?,?)",
               (k["task"]["id"], role, provider, time.time(), time.time(), "ok", json.dumps(k.get("note") or {})))
        p.db.update_task(k["task"]["id"], status="queued", not_before=time.time() + 3600)
        return 0
    monkeypatch.setattr(d, "start_run", start)
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.root), None))
    monkeypatch.setattr(d, "_wake_gate", lambda t: (False, None))
    for prov in {d.cfg.get("core_provider", "claude"), "claude", "fake"}:
        d.gates[prov] = bud.Gate(prov, regime="windows", max_parallel=6)
    d.dispatch()
    return started


def test_a_probe_woken_worker_gets_the_probes_bounded_output_and_its_run_keeps_it(env, monkeypatch, tmp_path,
                                                                                   capsys):
    """Upstream note 07c8ca8a4f2ccce0: a probe wake names what it saw. The woken worker's prompt and its
    run's note carry the probe's command, exit code, first 2 KB of output and time; a later probe run
    leaves the earlier run's record as it was; `ttp task show` prints it."""
    p = make(env)
    from ttp import cli, daemon as dmod, probewake
    d = dmod.Daemon(p.base)
    probe = "ttp detach --check job"
    tid = _gate_task(p, tmp_path, probe)
    now = time.time() - 600
    big = "line one: job finished rc=0\n" + "é" * 1500   # 3000+ bytes: kept to its first 2048
    d._probe_rc[tid] = (0, now, probe)
    d._probe_out[tid] = (big, now, probe)
    d._wake_waiting(p.db.task(tid), "probe passed", now, kind="probe_passed")
    started = _probe_dispatch(p, d, monkeypatch)
    assert len(started) == 1, started
    prompt, note = started[0]
    rec = note[probewake.KEY]
    assert rec["cmd"] == probe and rec["rc"] == 0 and rec["at"] == now and rec["cut"] is True
    assert len(rec["out"].encode()) <= probewake.OUT_BYTES and rec["out"].startswith("line one: job finished")
    assert f"`{probe}`" in prompt and "line one: job finished rc=0" in prompt and "first 2048 bytes, cut" in prompt
    first = p.db.one("SELECT id, note FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))
    # A later probe run with another answer: a new wake gets its own record; the earlier run's stays.
    later = now + 120
    d._probe_rc[tid] = (0, later, probe)
    d._probe_out[tid] = ("second answer", later, probe)
    d._wake_waiting(p.db.task(tid), "probe passed", later, kind="probe_passed")
    started = _probe_dispatch(p, d, monkeypatch)
    assert started[0][1][probewake.KEY]["out"] == "second answer" and "second answer" in started[0][0]
    assert p.db.one("SELECT note FROM runs WHERE id=?", (first["id"],))["note"] == first["note"]
    assert probewake.of_run(first["note"]) == rec
    # A timer wake carries none, and drops the earlier wake's record from the hand-off.
    d._wake_waiting(p.db.task(tid), "timer", later + 60, kind="timer")
    assert probewake.KEY not in json.loads(p.db.task(tid)["result"])
    cli.main(["task", p.name, "show", str(tid), "--json"])
    assert json.loads(capsys.readouterr().out)[probewake.KEY]["out"] == "second answer"
    cli.main(["task", p.name, "show", str(tid)])
    shown = capsys.readouterr().out
    assert "woken by its probe at" in shown and "output: second answer" in shown, shown


def test_a_passed_start_when_hands_its_probe_output_to_the_first_run(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod, probewake
    d = dmod.Daemon(p.base)
    tid = p.db.add_task("after the job", "s", kind="work", tier="standard", origin="user")
    now = time.time()
    p.db.update_task(tid, labels=["start_when:test -e /tmp/x", f"deferred_since:{now:.0f}"])
    d._probe_out[tid] = ("ready: build 42", now, "test -e /tmp/x")
    d._start_verdict(p.db.task(tid), 0, now)
    started = _probe_dispatch(p, d, monkeypatch)
    assert len(started) == 1 and started[0][1][probewake.KEY]["out"] == "ready: build 42"
    assert "ready: build 42" in started[0][0] and started[0][1][probewake.KEY]["cmd"] == "test -e /tmp/x"
    assert p.db.kv(probewake.KV_PREFIX + str(tid)) is None, "the start record outlived its dispatch"


def _own_checks(monkeypatch, tmp_path, name="own-checks"):
    """A run dir whose detached checks run (pid file, alive) with a fresh checks.log; the alive flag list."""
    from ttp import cli
    d = tmp_path / name
    d.mkdir()
    (d / cli.CHECKS_PID).write_text(json.dumps({"pid": 1, "started": "x"}))
    (d / "checks.log").write_text("smoke 3/9\n")
    alive = [True]
    monkeypatch.setattr(cli, "_checks_alive", lambda info: alive[0])
    return d, alive


def test_running_own_checks_hold_a_timer_spec_change_or_new_progress_line_until_they_end_or_stall(
        env, monkeypatch, tmp_path):
    """#8307: a run waiting on its own `ttp checks --result` is not woken while those checks run and their
    log advances: not by its timer, a spec change or a changed progress line. An escalated wake, a real
    wake reason and the hard cap still wake it; checks that stalled or died fall back to the old path."""
    p = make(env)
    from ttp import waitgate
    d, alive = _own_checks(monkeypatch, tmp_path)
    tid = _gate_task(p, tmp_path, f"ttp checks --result {d}", note={"spec_sha": "old"})
    _gate_set(p, tid, wake_gate={"seen": waitgate.evidence(1, "checks running 3/9"), "log": [1]})
    for kw in ({}, {"spec_sha": "new"}, {"probe": (1, "checks running 7/9")}):
        r = _gate_decide(p, tid, **{"spec_sha": "old", **kw})
        assert not r.wake and r.by == "rule" and r.extra["hold"] == "checks", (kw, r)
    assert "spec change applies when they end" in _gate_decide(p, tid, spec_sha="new").reason
    # a hold skip brings no cap wake closer
    held = waitgate.skipped(json.loads(p.db.task(tid)["result"]), r, time.time())
    assert held["wake_gate"]["hold_skips"] == 1 and held["wake_gate"]["end_skips"] == 0
    _gate_set(p, tid, woke="its checks failed (exit 1)")
    assert _gate_decide(p, tid).wake
    _gate_set(p, tid, woke="")
    hard = _gate_task(p, tmp_path, f"ttp checks --result {d}", ended_ago=25 * 3600)
    assert _gate_decide(p, hard).by == "cap"
    old = time.time() - 3600
    os.utime(d / "checks.log", (old, old))
    os.utime(d / "checks.pid", (old, old))
    r = _gate_decide(p, tid, spec_sha="new")   # stalled: the spec change wakes it as before
    assert r.wake and r.reason == "its spec changed since the last turn", r
    alive[0] = False   # died: not held either
    assert waitgate.checks_hold(p.config(), f"ttp checks --result {d}", time.time()) is None


def test_own_checks_that_ended_under_a_stale_not_yet_answer_re_defer_a_minute_for_the_model_free_path(
        env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import checksgone, cli, waitgate
    d, _ = _own_checks(monkeypatch, tmp_path)
    probe = f"ttp checks --result {d}"
    tid = _gate_task(p, tmp_path, probe)
    before = time.time() - 1
    assert not checksgone.ended_since(probe, before)
    (d / cli.CHECKS_RC).write_text("0\n")
    assert checksgone.ended_since(probe, before) and not checksgone.ended_since("exit 1", 0)
    r = _gate_decide(p, tid, probe=(1, "checks running 7/9"))
    assert not r.wake and r.extra.get("ended") and r.delay_s == 60.0, r
    assert waitgate.skipped(json.loads(p.db.task(tid)["result"]), r, time.time())["wake_gate"]["end_skips"] == 1
    assert _gate_decide(p, tid, probe=(0, "checks passed")).wake   # the probe answered: no hold
    _gate_set(p, tid, wake_gate={"end_skips": 4, "hold_skips": 4, "log": [1, 2, 3, 4]})
    assert _gate_decide(p, tid, probe=(1, "checks running 7/9")).wake   # at most max_skips such re-defers
