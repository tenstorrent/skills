# Started-on-host state and the check on coordinator replies (runtime/ttp/onhost.py) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations

_OH_CLAIM = "Power cycling box1 now, nothing else touches the box."


def _oh_task(p, status="queued", chat="c1", labels=("resource:box1-device",)):
    return p.db.add_task("power cycle the box", "s", kind="work", status=status, reply_chat=chat, labels=list(labels))


def _oh_run(p, tid, env, name="r1", status="running"):
    d = env["tmp"] / name
    d.mkdir(exist_ok=True)
    p.db.x("INSERT INTO runs(task, role, started, status, dir) VALUES(?, 'worker', ?, ?, ?)",
           (tid, time.time() - 120, status, str(d)))
    return d


def _oh_sent(p, chat="c1"):
    """The replies sent to `chat`, without claimcheck's marks: these tests judge onhost.guard alone."""
    from ttp import claimcheck
    drop = re.compile(rf" {re.escape(claimcheck.UNVERIFIED)}|{re.escape(claimcheck.FALSE_MARK)} \([^)]*\): ")
    return [drop.sub("", m["text"])
            for m in p.db.q("SELECT text FROM messages WHERE direction='out' AND chat=? ORDER BY id", (chat,))]


def test_a_claim_about_a_queued_host_task_is_sent_marked_with_the_honest_status(env):
    from ttp import coordinator as coord, onhost
    p = make(env)
    tid = _oh_task(p)
    coord.apply(p, [{"type": "reply", "text": _OH_CLAIM, "chat": "c1", "owners": [tid]}], turn=1)
    (got,) = _oh_sent(p)
    assert got != _OH_CLAIM and f"{onhost.MARK} Power cycling box1 now" in got
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
    for text in ("The docs rewrite finished at 14:00 and landed.", "Power cycling it now, nothing else touches it."):
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
    coord.apply(p, [{"type": "reply", "text": "Stopped the runs on box1.", "chat": "c1"},
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
    plan = "I'm changing the rerun on box1 to use the model the provider serves now and to name that model in the results."
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
    for turn, claim in enumerate(("The slot cleanup is running on box1 now.", "Freeing the check slots on box1 now.",
                                  "The box1 job finished."), 1):
        text = f"{_OH_FACTS[0]} {claim}"
        coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}, dict(_OH_FREE)], turn=turn)
        got = _oh_sent(p)[-1]
        assert got.startswith(f"{_OH_FACTS[0]} {onhost.MARK} {claim}"), got
        assert "new task 'free the check slots held by stale runs': queued for a worker" in got, got


def _oh_order_batch(p, env, reply):
    """A batch as one turn sent it: a task_add carrying the user's order on a resource (its note once rebound
    the batch's action list), then a reply, then a task_update of another task."""
    other = p.db.add_task("tidy the docs", "s", kind="work", reply_chat="c1")
    msg = p.db.x("INSERT INTO messages(ts,direction,kind,text) VALUES(?,'in','user','power-cycle rig now')",
                 (time.time(),))
    return other, [{"type": "task_add", "title": "power-cycle rig", "spec": "power-cycle rig", "tier": "light",
                    "resources": ["rig"], "user_msg": msg, "reply_chat": "c1"},
                   {"type": "reply", "text": reply, "chat": "c1"},
                   {"type": "task_update", "id": other, "priority": 1}]


def test_a_user_order_task_add_leaves_the_rest_of_its_batch_applied_and_checked(env):
    """A task_add recording a user order once rebound the batch's action list to its note: the later
    task_update failed with 'not enough values to unpack' and the reply after it went out unchecked."""
    from ttp import coordinator as coord, onhost
    p = make(env)
    other, batch = _oh_order_batch(p, env, "Power cycling rig now.")
    assert coord.apply(p, batch, turn=1) == []
    assert p.db.task(other)["priority"] == 1
    notes = p.db.kv(coord.NOTES_KEY, [])
    assert not any("went unchecked" in n for n in notes), notes
    (got,) = _oh_sent(p)
    assert got.startswith(f"{onhost.MARK} Power cycling rig now") and "queued for a worker" in got, got


def test_a_fault_in_the_started_on_host_check_is_a_harness_event_never_a_silent_pass(env, monkeypatch):
    from ttp import coordinator as coord, onhost
    p = make(env)
    tid = _oh_task(p)

    def boom(*a, **k):
        raise ValueError("not enough values to unpack (expected 2, got 1)")
    monkeypatch.setattr(onhost, "guard", boom)
    coord.apply(p, [{"type": "reply", "text": _OH_CLAIM, "chat": "c1", "owners": [tid]}], turn=1)
    assert _oh_sent(p) == [_OH_CLAIM]   # never lost to the check
    ev = p.db.q("SELECT source, kind, status, text FROM events WHERE kind='onhost_fault'")
    assert len(ev) == 1 and ev[0]["source"] == "harness" and ev[0]["status"] == "queued", ev
    assert "unchecked" in ev[0]["text"] and "ValueError" in ev[0]["text"] and "reply" in ev[0]["text"], ev
    assert any("went unchecked" in n for n in p.db.kv(coord.NOTES_KEY, []))
    # The batch's remaining actions are read whatever shape they come in: no fault, no event.
    monkeypatch.undo()
    notes = []
    out = coord._onhost_checked(p, _OH_CLAIM, "c1", {"owners": [tid]}, list("text") + [(5, None), (6,)], {}, notes)
    assert out.startswith(onhost.MARK) and not any("unchecked" in n for n in notes), (out, notes)
    assert len(p.db.q("SELECT id FROM events WHERE kind='onhost_fault'")) == 1


@pytest.mark.parametrize("err", [ValueError("not enough values to unpack (expected 2, got 1)"), KeyError("x"),
                                 TypeError("'NoneType' object is not subscriptable")])
def test_an_internal_error_applying_an_action_is_named_a_harness_fault_never_a_bare_error(env, monkeypatch, err):
    from ttp import coordinator as coord
    p = make(env)
    tid = p.db.add_task("tidy the docs", "s", kind="work", reply_chat="c1")

    def boom(*a, **k):
        raise err
    monkeypatch.setattr(coord.chainprio, "follow", boom)
    (got,) = coord.apply(p, [{"type": "task_update", "id": tid, "priority": 1}], turn=1)
    assert got.startswith("task_update: not applied: an internal harness error") and type(err).__name__ in got, got
    assert "fields: id, priority, type" in got and "harness fault" in got and "coordinator.py:" in got, got
    ev = p.db.q("SELECT source, status, text FROM events WHERE kind='action_fault'")
    assert len(ev) == 1 and ev[0]["source"] == "harness" and ev[0]["status"] == "handled", ev   # a record
    # A refusal of a field the coordinator wrote stays as it was: it names the field, value and form.
    (got,) = coord.apply(p, [{"type": "task_update", "id": tid, "status": "blocked", "waits_on": "nonsense"}], turn=2)
    assert "waits_on 'nonsense' is not one of ask:<id>" in got and "internal" not in got, got
    assert len(p.db.q("SELECT id FROM events WHERE kind='action_fault'")) == 1


def test_the_batch_loop_never_rebinds_its_action_list_or_position():
    """`order` (the batch's (index, action) pairs), `k` and `i` are read by later actions of the batch
    (order[k + 1:], failed.add(i)): rebinding one inside the loop corrupts every action after it."""
    import ast
    import inspect
    import textwrap
    from ttp import coordinator as coord
    fn = ast.parse(textwrap.dedent(inspect.getsource(coord.apply)))
    loop = next(n for n in ast.walk(fn) if isinstance(n, ast.For) and isinstance(n.iter, ast.Call)
                and getattr(n.iter.func, "id", "") == "enumerate" and getattr(n.iter.args[0], "id", "") == "order")
    scopes = (ast.FunctionDef, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    bad = []

    def walk(node):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, scopes):
                continue   # its own scope
            names = []
            if isinstance(ch, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.For)):
                for tg in (ch.targets if isinstance(ch, ast.Assign) else [ch.target]):
                    names += [x.id for x in ast.walk(tg) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)]
            elif isinstance(ch, ast.NamedExpr):
                names.append(ch.target.id)
            bad.extend((n, ch.lineno) for n in names if n in ("order", "k", "i"))
            walk(ch)
    for stmt in loop.body:
        walk(stmt)
    assert not bad, bad


def _oh_promises(p):
    from ttp import obligations as ob
    return [o for o in ob.items(p.db) if o.get("promise")]


def test_the_status_footer_mints_no_promise_and_names_only_the_claimed_task(env):
    """Upstream notes 10b38a1469583bae and b3b79b0f2535fb2a: the footer's queue forecast ('ETA 21:00 to
    start') once became a promise nobody wrote, and it listed every unstarted task the reply covered."""
    from ttp import coordinator as coord, obligations as ob, onhost, wakemode
    p = make(env)
    tid = _oh_task(p)
    other = _oh_task(p)
    coord.apply(p, [{"type": "reply", "text": f"Power cycling #{tid} now.", "chat": "c1", "owners": [tid, other]}],
                turn=1)
    (got,) = _oh_sent(p)
    assert f"\n{onhost.STATUS_HEAD}#{tid}: queued for a worker, ETA" in got, got
    assert f"#{other}:" not in got   # a covered task the message made no claim about gets no forecast
    assert _oh_promises(p) == []
    # The harness's footers are never read as the coordinator's own commitments.
    footer = f"\n{onhost.STATUS_HEAD}#{tid}: queued for a worker, ETA 21:00 to start, finish 23:00"
    assert ob.commitments("Noted." + footer) == []
    assert ob.commitments(f"Noted.\n{wakemode.HEAD} resumed, ETA 21:00") == []
    # An authored commitment still mints one, footer or not.
    assert ob.commitments("I will send the bench table by 21:00." + footer) == ["I will send the bench table by 21:00."]
    coord.apply(p, [{"type": "reply", "text": f"I will send the bench table by 21:00. Power cycling #{tid} now.",
                     "chat": "c1", "owners": [tid]}], turn=2)
    assert onhost.STATUS_HEAD in _oh_sent(p)[-1]
    (promise,) = _oh_promises(p)
    assert "bench table" in promise["text"] and "queued for a worker" not in promise["text"], promise


def test_status_footer_off_marks_the_claim_and_appends_no_status(env):
    from ttp import coordinator as coord, onhost
    p = make(env)
    tid = _oh_task(p)
    p.set_config("coordinator.status_footer", False)
    coord.apply(p, [{"type": "reply", "text": _OH_CLAIM, "chat": "c1", "owners": [tid]}], turn=1)
    (got,) = _oh_sent(p)
    assert got.startswith(f"{onhost.MARK} Power cycling box1 now") and onhost.STATUS_HEAD not in got, got
    note = next(n for n in p.db.kv(coord.NOTES_KEY, []) if n.startswith("onhost:"))
    assert f"#{tid}: queued for a worker" in note and "status_footer is off" in note


# A block meant to be posted as written (a comment for another tool, a quote): never edited (upstream note
# a4b54adbe049e531).
_OH_BLOCKS = ("Post this:\n---\nPower cycling box1 now.\nIt is on the release branch.\n---\nThat is all.",
              "Post this:\n```\nPower cycling box1 now.\nIt is on the release branch.\n```\nThat is all.",
              "Post this:\n> Power cycling box1 now.\n> It is on the release branch.\nThat is all.")


@pytest.mark.parametrize("text", _OH_BLOCKS)
def test_no_marker_goes_inside_a_block_meant_for_posting(env, text):
    from ttp import coordinator as coord, onhost
    p = make(env)
    tid = _oh_task(p)
    coord.apply(p, [{"type": "reply", "text": text, "chat": "c1", "owners": [tid]}], turn=1)
    (got,) = _oh_sent(p)
    block, rest = text[:text.index("\nThat is all.")], "\nThat is all."
    # The block is sent word for word; the marker follows it on its own line, quoting the sentence.
    assert got.startswith(f"{block}\n{onhost.MARK} \"Power cycling box1 now.\"{rest}"), got
    assert f"#{tid}: queued for a worker" in got, got


def test_segments_join_back_and_tell_blocks_from_plain_text():
    from ttp import onhost
    for text in (*_OH_BLOCKS, "a\n---\nlone rule", "```\nunclosed fence\nstill code", "", "x\n\n> q\n",
                 "---\n```\n---\n```\n---\nafter"):
        segs = onhost.segments(text)
        assert "".join(c for c, _ in segs) == text, segs
    assert [b for _, b in onhost.segments("a\n---\nlone rule")] == [False]   # one '---' is a rule, not a block
    assert onhost.segments("```\nunclosed\n")[0] == ("```\nunclosed", True)
    # A '---' inside a fenced block does not close the '---' block around it.
    assert onhost.segments("---\n```\n---\n```\n---\nafter")[0] == ("---\n```\n---\n```\n---", True)


def test_a_sentence_naming_no_task_id_or_host_is_never_marked(env):
    from ttp import coordinator as coord, onhost
    p = make(env)
    tid = _oh_task(p)
    # The reply speaks for one unstarted host task, but these sentences name neither it nor its host.
    for turn, text in enumerate(("It is on the release branch (commit 5088f11) and running there.",
                                 "Resetting the board.", "The reset is in progress.",
                                 f"#{tid} is queued. It landed on the release branch."), 1):
        coord.apply(p, [{"type": "reply", "text": text, "chat": "c1", "owners": [tid]}], turn=turn)
        assert _oh_sent(p)[-1] == text and onhost.MARK not in _oh_sent(p)[-1]


def test_a_queued_task_whose_detached_driver_is_live_counts_as_under_way(env, monkeypatch):
    from ttp import coordinator as coord, onhost
    p = make(env)
    tid = _oh_task(p)
    d = _oh_run(p, tid, env, status="done")
    _oh_remote(d, "driver")   # not known to have ended: live
    st = onhost.state(p, p.db.task(tid))
    assert st["started"] and st["kind"] == "on" and st["line"] == "detached job running on hostA", st
    text = f"#{tid} is running its driver on box1 now."
    coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=1)
    assert _oh_sent(p) == [text]
    # Its host answers that it ended: queued again, and the same claim is marked.
    _oh_answers(monkeypatch, {"hostA": "rc 0"})
    onhost.refresh_remote(p, time.time() + onhost.REMOTE_EVERY_S + 1)
    assert not onhost.state(p, p.db.task(tid))["started"]
    coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=2)
    assert _oh_sent(p)[-1].startswith(f"{onhost.MARK} {text}")
