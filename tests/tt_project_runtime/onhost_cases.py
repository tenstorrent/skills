# Started-on-host state and the check on coordinator replies (runtime/ttp/onhost.py) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations

_OH_CLAIM = "Power cycling it now, nothing else touches the box."


def _oh_task(p, status="queued", chat="c1", labels=("resource:box1-device",)):
    return p.db.add_task("power cycle the box", "s", kind="work", status=status, reply_chat=chat, labels=list(labels))


def _oh_run(p, tid, env, name="r1", status="running"):
    d = env["tmp"] / name
    d.mkdir(exist_ok=True)
    p.db.x("INSERT INTO runs(task, role, started, status, dir) VALUES(?, 'worker', ?, ?, ?)",
           (tid, time.time() - 120, status, str(d)))
    return d


def _oh_sent(p, chat="c1"):
    return [m["text"] for m in p.db.q("SELECT text FROM messages WHERE direction='out' AND chat=? ORDER BY id", (chat,))]


def test_a_claim_about_a_queued_host_task_is_sent_marked_with_the_honest_status(env):
    from ttp import coordinator as coord, onhost
    p = make(env)
    tid = _oh_task(p)
    coord.apply(p, [{"type": "reply", "text": _OH_CLAIM, "chat": "c1"}], turn=1)
    (got,) = _oh_sent(p)
    assert got != _OH_CLAIM and f"{onhost.MARK} Power cycling it now" in got
    assert f"#{tid}: queued for a worker, ETA" in got, got
    assert "nothing else touches the box" in got   # nothing of the message is dropped
    assert any("onhost:" in n for n in p.db.kv(coord.NOTES_KEY, []))   # the coordinator hears of it next turn
    # A notify naming the task is checked the same way.
    coord.apply(p, [{"type": "notify", "text": f"#{tid}: resetting the box right away."}], turn=2)
    alert = p.db.q("SELECT text FROM messages WHERE direction='out' AND kind='alert' ORDER BY id DESC LIMIT 1")[0]["text"]
    assert onhost.MARK in alert and "queued for a worker" in alert


def test_the_same_claim_after_the_lock_grant_is_sent_unchanged(env, monkeypatch):
    from ttp import cli, coordinator as coord, onhost, privlock
    p = make(env)
    tid = _oh_task(p, status="running")
    d = _oh_run(p, tid, env)
    # A running worker that holds no lock yet is waiting, not on the host.
    assert onhost.state(p, p.db.task(tid))["line"].startswith("worker running, waiting for box1-device since ")
    # The grant itself writes the record: `ttp lock` inside the task's run.
    monkeypatch.setattr(privlock, "no_new_privs", lambda: None)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_TASK", str(tid))
    monkeypatch.setenv("TTP_RUN_DIR", str(d))
    with pytest.raises(SystemExit) as e:
        cli.main(["lock", "box1-device", "--", "true"])
    assert e.value.code in (0, None)
    assert "box1-device" in json.loads((d / onhost.FILE).read_text())
    st = onhost.state(p, p.db.task(tid))
    assert st["started"] and st["line"].startswith("on box1-device since ")
    coord.apply(p, [{"type": "reply", "text": _OH_CLAIM, "chat": "c1"}], turn=1)
    assert _oh_sent(p) == [_OH_CLAIM]
    # The digest row says where it is.
    assert f"on box1-device since" in coord.digest(p, {}, [], [])


def test_a_cancelled_task_with_a_live_detached_job_reads_stop_requested(env, monkeypatch):
    from ttp import coordinator as coord, detach, jobstop, onhost
    p = make(env)
    tid = _oh_task(p, status="running")
    d = _oh_run(p, tid, env)
    rc = d / "drv.rc"
    (d / detach.REGISTRY).write_text(json.dumps([{"name": "drv", "rc": str(rc), "status": "running"}]))
    live = {str(rc)}
    monkeypatch.setattr(jobstop, "running", lambda r: str(r) in live)
    monkeypatch.setattr(jobstop, "job_resources", lambda p, r: {"box1-device"})
    # Cancelled in the same batch as a reply saying it stopped: the jobs still run, so it is not.
    coord.apply(p, [{"type": "reply", "text": "Stopped the runs on the box.", "chat": "c1"},
                    {"type": "task_update", "id": tid, "status": "cancelled"}], turn=1)
    (got,) = _oh_sent(p)
    assert onhost.MARK in got and "stop requested; 1 job still running on box1-device" in got, got
    assert p.db.task(tid)["status"] == "cancelled"
    assert onhost.line(p, p.db.task(tid)) == "stop requested; 1 job still running on box1-device"
    assert "stop requested; 1 job still running on box1-device" in coord.digest(p, {}, [], [])
    # Once the job sweep has seen it end, the line is gone and a stop may be reported.
    live.clear()
    assert onhost.line(p, p.db.task(tid)) == "" and "Stops not yet confirmed" not in coord.digest(p, {}, [], [])


def test_a_reply_about_non_device_work_is_unaffected(env):
    from ttp import coordinator as coord, onhost
    p = make(env)
    tid = p.db.add_task("tidy the docs", "s", kind="work", reply_chat="c1")
    text = f"Started #{tid} now: rewriting the docs, and the build is running."
    coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=1)
    assert _oh_sent(p) == [text]
    assert onhost.line(p, p.db.task(tid)) == ""


def test_a_host_task_added_later_in_the_batch_is_covered(env):
    from ttp import coordinator as coord, onhost
    p = make(env)
    coord.apply(p, [{"type": "reply", "text": "Only the quick repro runs on the device.", "chat": "c1"},
                    {"type": "task_add", "title": "quick repro on the device", "spec": "run it", "needs_device": True,
                     "reply_chat": "c1"}], turn=1)
    (got,) = _oh_sent(p)
    assert got.startswith(onhost.MARK) and "new task 'quick repro on the device': queued for a worker" in got, got


def test_onhost_claims_tell_claims_from_plans():
    from ttp import onhost
    for s in ("Power cycling it alone now.", "Only the quick DRAM repro runs.", "I stopped the runs.",
              "The reset is in progress.", "Started the job.", "Resetting the board.", "It's running now."):
        assert onhost.claims(s), s
    for s in ("Queued #4; a worker starts it when the lock is free.", "It will be stopped once the run ends.",
              "Waiting for the lock.", "The runs are not started yet.", "Once it is started I'll report.",
              "I'll power cycle it after the queue drains.", "Nothing changes for now.",
              f"{onhost.MARK} power cycling it now."):
        assert not onhost.claims(s), s

