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
    coord.apply(p, [{"type": "reply", "text": _OH_CLAIM, "chat": "c1", "owners": [tid]}], turn=1)
    (got,) = _oh_sent(p)
    assert got != _OH_CLAIM and f"{onhost.MARK} Power cycling it now" in got
    assert f"#{tid}: queued for a worker, ETA" in got, got
    assert "nothing else touches the box" in got   # nothing of the message is dropped
    assert any("onhost:" in n for n in p.db.kv(coord.NOTES_KEY, []))   # the coordinator hears of it next turn
    # A notify naming the task is checked the same way.
    coord.apply(p, [{"type": "notify", "text": f"#{tid}: resetting the box right away."}], turn=2)
    alert = p.db.q("SELECT text FROM messages WHERE direction='out' AND kind='alert' ORDER BY id DESC LIMIT 1")[0]["text"]
    assert onhost.MARK in alert and "queued for a worker" in alert


def test_only_sentences_about_an_unstarted_host_task_are_marked(env):
    from ttp import coordinator as coord, onhost
    p = make(env)
    tid = _oh_task(p)
    # The chat's queued host task does not make a true sentence about other work read as planned.
    for text in ("The docs rewrite finished at 14:00 and landed.", _OH_CLAIM):
        coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=1)
        assert _oh_sent(p)[-1] == text
    # The chat's task is tied by its resource or its #id.
    coord.apply(p, [{"type": "reply", "text": "Power cycling box1 now.", "chat": "c1"}], turn=2)
    assert _oh_sent(p)[-1].startswith(f"{onhost.MARK} Power cycling box1 now.")
    done = p.db.add_task("tidy the docs", "s", kind="work", status="done", reply_chat="c1")
    coord.apply(p, [{"type": "reply", "text": f"#{done} finished. Power cycling #{tid} now.", "chat": "c1"}], turn=3)
    got = _oh_sent(p)[-1]
    assert got.startswith(f"#{done} finished. {onhost.MARK} Power cycling #{tid} now."), got
    assert f"#{tid}: queued for a worker" in got
    # Naming a done task alone, next to the reply's own unstarted task: unchanged.
    text = f"#{done} finished and landed."
    coord.apply(p, [{"type": "reply", "text": text, "chat": "c1", "owners": [tid]}], turn=4)
    assert _oh_sent(p)[-1] == text


def test_a_notify_keeps_held_worker_notifies_only_its_status_line_names(env):
    from ttp import coordinator as coord, onhost, workernotify
    p = make(env)
    a = p.db.add_task("tidy the docs", "s", kind="work", status="done")
    b = _oh_task(p, chat=None)
    assert workernotify.hold(p.db, p.db.task(b), "box1 is wedged", "r9", "21:00 PST", 900)
    coord.apply(p, [{"type": "notify", "text": f"#{a} finished; power cycling the box now."},
                    {"type": "task_update", "id": b, "priority": 1}], turn=1)
    alert = p.db.q("SELECT text FROM messages WHERE direction='out' AND kind='alert' ORDER BY id DESC LIMIT 1")[0]["text"]
    assert f"{onhost.MARK} power cycling the box now." in alert and f"Status now: #{b}: queued" in alert, alert
    # The coordinator never named #b: its held notify still goes to the user.
    assert [e["task"] for e in workernotify._entries(p.db)] == [b]


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


def _oh_remote(d, name, host="hostA"):
    from ttp import detach
    detach.mirror_save(d, name, host, f"~/.ttp-detach/r1/{name}")


def _oh_answers(monkeypatch, answers):
    """Stand-in for the ssh call of the job sweep's remote probe: `answers[host]` is the check's last line,
    None for a host that does not answer. The (host, path) of each call is recorded."""
    import subprocess
    from ttp import detach
    calls = []

    def probe(host, pre, d=None, rows=None, timeout=60):
        calls.append((host, pre, timeout))
        a = answers.get(host)
        if a is None:
            return subprocess.CompletedProcess([], 255, "", "ssh: connect timed out"), []
        return subprocess.CompletedProcess([], 0, a + "\n", ""), [a]
    monkeypatch.setattr(detach, "mirror_probe", probe)
    return calls


def test_a_cancelled_task_with_only_a_remote_detached_job_reads_stop_requested(env, monkeypatch):
    from ttp import coordinator as coord, jobstop, onhost
    p = make(env)
    tid = _oh_task(p, status="running")
    d = _oh_run(p, tid, env)
    _oh_remote(d, "bench")
    calls = _oh_answers(monkeypatch, {"hostA": "running 4242"})
    p.db.update_task(tid, status="cancelled")
    # Before any probe the job is not known to have ended: it counts.
    assert onhost.line(p, p.db.task(tid)) == "stop requested; 1 job still running on hostA"
    assert calls == []   # reading the line runs no ssh
    jobstop.sweep(p)
    assert [c[:2] for c in calls] == [("hostA", "~/.ttp-detach/r1/bench")] and calls[0][2] <= onhost.REMOTE_TIMEOUT_S
    assert onhost.line(p, p.db.task(tid)) == "stop requested; 1 job still running on hostA"
    assert "stop requested; 1 job still running on hostA" in coord.digest(p, {}, [], [])
    jobstop.sweep(p)
    assert len(calls) == 1   # asked again only after REMOTE_EVERY_S
    # The host says it ended: the line is gone and its end is mirrored.
    onhost.refresh_remote(p, time.time() + onhost.REMOTE_EVERY_S + 1)
    assert len(calls) == 2
    calls_end = _oh_answers(monkeypatch, {"hostA": "rc 0"})
    onhost.refresh_remote(p, time.time() + 2 * onhost.REMOTE_EVERY_S + 2)
    assert len(calls_end) == 1 and (d / "bench.remote.rc").read_text().strip() == "0"
    assert onhost.line(p, p.db.task(tid)) == ""
    onhost.refresh_remote(p, time.time() + 9 * onhost.REMOTE_EVERY_S)
    assert len(calls_end) == 1   # an ended job is never asked about again
    # A host that answers no driver is recorded at the path reads as not running either.
    _oh_remote(d, "gone", host="hostB")
    _oh_answers(monkeypatch, {"hostB": "none ~/.ttp-detach/r1/gone"})
    onhost.refresh_remote(p)
    assert onhost.line(p, p.db.task(tid)) == ""


def test_a_stop_line_counts_local_and_remote_detached_jobs(env, monkeypatch):
    from ttp import detach, jobstop, onhost
    p = make(env)
    tid = _oh_task(p, status="running")
    d = _oh_run(p, tid, env)
    rc = d / "drv.rc"
    (d / detach.REGISTRY).write_text(json.dumps([{"name": "drv", "rc": str(rc), "status": "running"}]))
    monkeypatch.setattr(jobstop, "running", lambda r: str(r) == str(rc))
    monkeypatch.setattr(jobstop, "job_resources", lambda p, r: {"box1-device"})
    monkeypatch.setattr(jobstop, "end", lambda *a, **k: None)   # the sweep's stop of the local job is not under test
    d2 = _oh_run(p, tid, env, name="r2")
    _oh_remote(d2, "bench")
    _oh_answers(monkeypatch, {"hostA": "running 77"})
    p.db.update_task(tid, status="failed")
    jobstop.sweep(p)
    assert onhost.line(p, p.db.task(tid)) == "stop requested; 2 jobs still running on box1-device, hostA"
    assert onhost.state(p, p.db.task(tid))["started"] is False


def test_a_remote_job_on_an_unreachable_host_reads_possibly_running(env, monkeypatch):
    from ttp import coordinator as coord, jobstop, onhost
    p = make(env)
    tid = _oh_task(p, status="running")
    d = _oh_run(p, tid, env)
    _oh_remote(d, "a")
    _oh_remote(d, "b")
    _oh_remote(d, "c", host="hostB")
    calls = _oh_answers(monkeypatch, {"hostA": None, "hostB": "running 9"})
    p.db.update_task(tid, status="cancelled")
    jobstop.sweep(p)
    # One call to the host that did not answer per sweep, not one per job.
    assert sorted(c[0] for c in calls) == ["hostA", "hostB"]
    got = onhost.line(p, p.db.task(tid))
    assert got == ("stop requested; 1 job still running on hostB; "
                   "2 jobs still possibly running on hostA (host unreachable)"), got
    # A reply saying it stopped is marked, never posted as a fact.
    coord.apply(p, [{"type": "reply", "text": f"#{tid} is stopped.", "chat": "c1"}], turn=1)
    (sent,) = _oh_sent(p)
    assert onhost.MARK in sent and "still possibly running on hostA (host unreachable)" in sent, sent
    # An unreachable host is asked again only after REMOTE_UNREACHABLE_S, and a failed ssh never ends a job.
    onhost.refresh_remote(p, time.time() + onhost.REMOTE_EVERY_S + 1)
    assert sorted(c[0] for c in calls) == ["hostA", "hostB", "hostB"]
    assert "2 jobs still possibly running on hostA" in onhost.line(p, p.db.task(tid))
    # The ssh time one sweep spends is bounded: a spent budget leaves the rest for the next sweep.
    assert onhost.refresh_remote(p, time.time() + onhost.REMOTE_UNREACHABLE_S + 1, budget=0) == 0


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
              "I'll power cycle it after the queue drains.", "Nothing changes for now.", "Once it's done I'll report.",
              f"{onhost.MARK} power cycling it now."):
        assert not onhost.claims(s), s



# The shape of a reply that marked plain facts as plans: past and present facts about other work around one
# line about the work the same batch queues (user msg #1924).
_OH_FACTS = ("The cut passed its review and is in the current push batch (24 changes, started 16:42 PST).",
             "That batch failed its first two check passes and is now working out which change broke them, "
             "so there is no landing time yet.",
             "Also, the chat provider stopped serving the model (alert at about 17:10 PST).")
_OH_FREE = {"type": "task_add", "title": "free the check slots held by stale runs", "spec": "s",
            "resources": ["box1"], "reply_chat": "c1"}


def test_facts_beside_a_newly_queued_host_task_are_not_marked_as_planned(env):
    from ttp import coordinator as coord, onhost
    p = make(env)
    plan = "I'm changing the rerun to use the model the provider serves now and to name that model in the results."
    text = " ".join(["Correction on the rerun: it has not started yet.", *_OH_FACTS[:2],
                     "The rerun starts on its own once the cut lands.", _OH_FACTS[2], plan])
    coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}, dict(_OH_FREE)], turn=1)
    (got,) = _oh_sent(p)
    assert got.count(onhost.MARK) == 1 and f"{onhost.MARK} {plan}" in got, got
    for fact in _OH_FACTS:
        assert f"{onhost.MARK} {fact}" not in got and fact in got, got
    assert "new task 'free the check slots held by stale runs': queued for a worker" in got, got
    # Facts and an honest line about the queued task alone: sent as written.
    text = " ".join([*_OH_FACTS, "Queued a task to free the stale check slots; a worker starts it when box1 is free."])
    coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}, dict(_OH_FREE)], turn=2)
    assert _oh_sent(p)[-1] == text


def test_a_premature_claim_beside_facts_is_still_marked(env):
    from ttp import coordinator as coord, onhost
    p = make(env)
    for turn, claim in enumerate(("The slot cleanup is running now.", "It's done.", "Freeing the check slots now.",
                                  "The job finished.", "This is running now."), 1):
        text = f"{_OH_FACTS[0]} {claim}"
        coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}, dict(_OH_FREE)], turn=turn)
        got = _oh_sent(p)[-1]
        assert got.startswith(f"{_OH_FACTS[0]} {onhost.MARK} {claim}"), got
        assert "new task 'free the check slots held by stale runs': queued for a worker" in got, got
