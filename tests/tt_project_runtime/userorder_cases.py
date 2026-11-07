# Newer user order tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file (and lock_cases.py).
# A user's order on a resource holds the older tasks there: `ttp lock`, dispatch, steer.md and the digest.
from __future__ import annotations


def _ordered(env, tmp_path, cite=False):
    """Old tasks on rig (#old, running, and #respec, re-specified after the order), one on another
    resource (#other), then the user's order on rig and its task (#order), and one added after it (#newer)."""
    from ttp import coordinator as coord
    p = make(env)
    now = time.time()

    def add(title, res):
        assert coord.apply(p, [{"type": "task_add", "title": title, "spec": f"work on {res}", "tier": "light",
                                "resources": [res]}]) == []
        return p.db.one("SELECT id FROM tasks WHERE title=?", (title,))["id"]
    ids = {k: add(f"{k} soak", r) for k, r in (("old", "rig"), ("respec", "rig-device"), ("other", "bench"))}
    p.db.x("UPDATE tasks SET created=?", (now - 600,))
    run_dir = tmp_path / "run-old"
    run_dir.mkdir()
    p.db.update_task(ids["old"], status="running")
    p.db.x("INSERT INTO runs(task,role,status,started,dir) VALUES(?,'worker','running',?,?)",
           (ids["old"], now - 500, str(run_dir)))
    other_dir = tmp_path / "run-other"
    other_dir.mkdir()
    p.db.update_task(ids["other"], status="running")
    p.db.x("INSERT INTO runs(task,role,status,started,dir) VALUES(?,'worker','running',?,?)",
           (ids["other"], now - 500, str(other_dir)))
    msg = p.db.x("INSERT INTO messages(ts,direction,kind,text) VALUES(?,'in','user','power-cycle rig now')",
                 (now - 60,))
    act = {"type": "task_add", "title": "power-cycle rig", "spec": f"user msg #{msg}: power-cycle rig" if cite
           else "power-cycle rig", "tier": "light", "resources": ["rig"]}
    assert coord.apply(p, [act if cite else {**act, "user_msg": msg}], messages=[msg] if cite else None) == []
    notes = p.db.kv(coord.NOTES_KEY, [])
    ids["order"] = p.db.one("SELECT id FROM tasks WHERE title='power-cycle rig'")["id"]
    assert any(f"#{ids['order']} is the latest user order (msg #{msg}) on rig" in n for n in notes), notes
    assert coord.apply(p, [{"type": "task_update", "id": ids["respec"], "spec": "fit the power cycle"}]) == []
    ids["newer"] = add("newer soak", "rig")
    return p, ids, msg, run_dir, other_dir


def _order_probe(monkeypatch, p, task, res):
    from ttp import cli
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_TASK", str(task))
    with pytest.raises(SystemExit) as e:
        cli.main(["lock", "--probe", res])
    return e.value.code


@pytest.mark.parametrize("cite", [False, True])
def test_a_newer_user_order_holds_only_older_tasks_on_its_resource_at_ttp_lock(env, tmp_path, monkeypatch, capsys,
                                                                                 cite):
    p, ids, msg, _, _ = _ordered(env, tmp_path, cite)
    assert _order_probe(monkeypatch, p, ids["old"], "rig") == 75
    out = capsys.readouterr().out
    assert f"user msg #{msg}" in out and f"task #{ids['order']}" in out, out
    assert _order_probe(monkeypatch, p, ids["old"], "rig-device") == 75   # a resource on the ordered machine
    for task, res in ((ids["order"], "rig"), (ids["respec"], "rig"), (ids["newer"], "rig"),
                      (ids["other"], "bench"), (ids["old"], "bench")):
        assert _order_probe(monkeypatch, p, task, res) == 0, (task, res, capsys.readouterr())
    # A real take is refused (exit 75) and runs nothing; the order's own task takes it.
    ran = tmp_path / "ran"
    take = _lock(_lock_env(env, p, TTP_TASK=str(ids["old"])), "rig", cmd=("touch", str(ran)))
    _, err = take.communicate(timeout=60)
    assert take.returncode == 75 and "refused" in err and f"user msg #{msg}" in err and not ran.exists(), err
    take = _lock(_lock_env(env, p, TTP_TASK=str(ids["order"])), "rig", cmd=("touch", str(ran)))
    take.communicate(timeout=60)
    assert take.returncode == 0 and ran.exists()
    # The order's task ends: the hold ends with it.
    p.db.update_task(ids["order"], status="done")
    assert _order_probe(monkeypatch, p, ids["old"], "rig") == 0


def test_a_user_order_holds_older_queued_tasks_at_dispatch_and_steers_older_running_ones(env, tmp_path):
    from ttp import daemon as dmod
    p, ids, msg, run_dir, other_dir = _ordered(env, tmp_path)
    p.db.update_task(ids["old"], status="queued")
    d = dmod.Daemon(p.base)
    hold = d._resource_hold(p.db.task(ids["old"]))
    assert hold and hold["kind"] == "order" and hold["resource"] == "rig", hold
    assert f"user msg #{msg}" in dmod.hold_reason(hold) and f"#{ids['order']}" in dmod.hold_reason(hold)
    for k in ("order", "respec", "newer", "other"):
        assert (d._resource_hold(p.db.task(ids[k])) or {}).get("kind") != "order", k
    assert d._resource_hold(p.db.task(ids["order"])) is None   # the order's own task is free to start
    steer = (run_dir / "steer.md").read_text()
    assert "Newer user order" in steer and f"user msg #{msg}" in steer and "ttp lock --probe rig" in steer
    assert not (other_dir / "steer.md").exists()   # a running task on another resource is not told


def test_an_order_held_task_never_reserves_the_ordered_resource_ahead_of_the_orders_task(env):
    """An older exclusive task that outranks the order's task (better priority) and that the order holds
    off the resource is no slot contender there: it reserves nothing, so the order's task is free."""
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    from ttp import locks
    p = make(env)
    now = time.time()
    assert coord.apply(p, [{"type": "task_add", "title": "old soak", "spec": "soak rig", "tier": "light",
                            "resources": ["rig"], "exclusive": True, "priority": 1}]) == []
    old = p.db.one("SELECT id FROM tasks WHERE title='old soak'")["id"]
    p.db.x("UPDATE tasks SET created=?", (now - 600,))
    msg = p.db.x("INSERT INTO messages(ts,direction,kind,text) VALUES(?,'in','user','power-cycle rig now')",
                 (now - 60,))
    assert coord.apply(p, [{"type": "task_add", "title": "power-cycle rig", "spec": "power-cycle rig",
                            "tier": "light", "resources": ["rig"], "exclusive": True, "priority": 3,
                            "user_msg": msg}]) == []
    order = p.db.one("SELECT id FROM tasks WHERE title='power-cycle rig'")["id"]
    d = dmod.Daemon(p.base)
    assert (d._resource_hold(p.db.task(old)) or {}).get("kind") == "order"
    assert d._resource_hold(p.db.task(order)) is None
    lock = locks.canonical(d.cfg, "rig")
    assert d._slot_reservers(time.time()).get(lock) in (None, order)
    assert old not in [t["id"] for t in d._slot_contenders(time.time())[0].get(lock, [])]
    # Without the order (its task ended), the older task outranks again and reserves the resource.
    p.db.update_task(order, status="done")
    assert d._slot_reservers(time.time()).get(lock) == old


def test_the_digest_lists_the_open_tasks_that_predate_a_user_order_until_respecified(env, tmp_path):
    from ttp import coordinator as coord
    p, ids, msg, _, _ = _ordered(env, tmp_path)
    text = coord.digest(p, {}, [], [])
    sec = text.split("## Newer user orders on a resource", 1)[1].split("\n## ", 1)[0]
    assert f"user msg #{msg}" in sec and f"#{ids['old']} (running)" in sec, sec
    older = sec.split("Older:", 1)[1]
    for k in ("order", "respec", "newer", "other"):
        assert f"#{ids[k]} (" not in older, (k, sec)
    assert coord.apply(p, [{"type": "task_update", "id": ids["old"], "spec": "pause until the cycle is done"}]) == []
    assert "Newer user orders" not in coord.digest(p, {}, [], [])


def test_an_order_from_an_older_message_never_replaces_a_newer_one_and_uncited_turns_record_none(env):
    from ttp import coordinator as coord, userorder
    p = make(env)
    now = time.time()
    new = p.db.x("INSERT INTO messages(ts,direction,kind,text) VALUES(?,'in','user','b')", (now - 10,))
    old = p.db.x("INSERT INTO messages(ts,direction,kind,text) VALUES(?,'in','user','a')", (now - 100,))
    gone = p.db.one("SELECT COALESCE(MAX(id), 0) + 1000 AS n FROM tasks")["n"]
    assert userorder.record(p.db, gone, new, ["rig"]) == []   # no such open task: nothing kept
    coord.apply(p, [{"type": "task_add", "title": "t1", "spec": "s", "resources": ["rig"]},
                    {"type": "task_add", "title": "t2", "spec": "s", "resources": ["rig"]}])
    t1, t2 = (p.db.one("SELECT id FROM tasks WHERE title=?", (t,))["id"] for t in ("t1", "t2"))
    assert userorder.record(p.db, t1, new, ["rig"]) == ["rig"]
    assert userorder.record(p.db, t2, old, ["rig"]) == []
    assert userorder.active(p.db)["rig"]["task"] == t1
    # A spec citing a message outside this turn's is no order.
    assert userorder.cited(p.db, f"user msg #{old}", [new]) is None
    assert userorder.cited(p.db, f"user msgs #{old} and #{new}", [new, old]) == max(new, old)
    assert not userorder.same(None, "rig", "rigs") and userorder.same(None, "rig", "rig-device")


def test_a_ttp_lock_already_waiting_when_the_order_arrives_is_refused_on_its_next_try(env, tmp_path):
    """The incident's shape: an older task's step queued for the machine before the user's order must not
    take it after the order."""
    from ttp import coordinator as coord
    p = make(env)
    coord.apply(p, [{"type": "task_add", "title": "soak", "spec": "s", "resources": ["rig"]},
                    {"type": "task_add", "title": "probe", "spec": "s", "resources": ["rig"]}])
    soak, probe = (p.db.one("SELECT id FROM tasks WHERE title=?", (t,))["id"] for t in ("soak", "probe"))
    p.db.x("UPDATE tasks SET created=?", (time.time() - 600,))
    marks, go, ran = tmp_path / "marks", tmp_path / "go", tmp_path / "ran"
    holder = _lock(_lock_env(env, p, TTP_TASK=str(probe), TTP_TEST_POLL_S="0.05"), "rig", cmd=_mark(marks, "h", go))
    waiter = None
    try:
        assert _until_true(lambda: marks.exists() and "start h" in marks.read_text()), holder.stderr.read()
        waiter = _lock(_lock_env(env, p, TTP_TASK=str(soak), TTP_TEST_POLL_S="0.05"), "rig",
                       cmd=("touch", str(ran)))
        seen = _stderr_until(waiter, "ttp lock: waiting for rig")
        assert "waiting for rig" in seen, seen
        msg = p.db.x("INSERT INTO messages(ts,direction,kind,text) VALUES(?,'in','user','cycle rig')", (time.time(),))
        coord.apply(p, [{"type": "task_add", "title": "cycle rig", "spec": "s", "resources": ["rig"], "user_msg": msg}])
        go.touch()
        rc = _ended([holder, waiter])
        err = seen + waiter.stderr.read()
        assert rc[1] == 75 and f"user msg #{msg}" in err and not ran.exists(), (rc, err)
    finally:
        go.touch()
        for x in (holder, waiter):
            if x and x.poll() is None:
                x.kill()


def test_a_respec_citing_the_turns_message_is_no_new_order_and_one_order_keeps_its_first_task(env):
    from ttp import coordinator as coord, userorder
    p = make(env)
    msg = p.db.x("INSERT INTO messages(ts,direction,kind,text) VALUES(?,'in','user','cycle rig')", (time.time(),))
    coord.apply(p, [{"type": "task_add", "title": "old soak", "spec": "s", "resources": ["rig"]}])
    old = p.db.one("SELECT id FROM tasks WHERE title='old soak'")["id"]
    coord.apply(p, [{"type": "task_update", "id": old, "spec": f"user msg #{msg}: stop the soak"}], messages=[msg])
    assert userorder.active(p.db) == {}
    coord.apply(p, [{"type": "task_add", "title": "cycle", "spec": f"user msg #{msg}", "resources": ["rig"]},
                    {"type": "task_add", "title": "after cycle", "spec": f"after user msg #{msg}",
                     "resources": ["rig"], "force": True}], messages=[msg])
    first = p.db.one("SELECT id FROM tasks WHERE title='cycle'")["id"]
    assert userorder.active(p.db)["rig"]["task"] == first


def _order_run(p, tmp_path, title, spec="s", chat=None, resources=(), ago=600):
    """A task created `ago` s back with a running worker run; returns (task id, run dir)."""
    tid = p.db.add_task(title, spec, kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running", reply_chat=chat,
                     labels=json.dumps([f"resource:{r}" for r in resources]))
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (time.time() - ago, tid))
    run_dir = tmp_path / f"run-{tid}"
    run_dir.mkdir()
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - ago, "running", str(run_dir)))
    return tid, run_dir


def _order_fanouts(p):
    from ttp import userorder
    return [json.loads(r["data"]) | {"text": r["text"]}
            for r in p.db.q("SELECT text, data FROM events WHERE kind=? ORDER BY id", (userorder.FANOUT_KIND,))]


def _order_msg(p, text, chat="c1", ago=60):
    return p.db.x("INSERT INTO messages(ts,direction,kind,chat,text) VALUES(?,'in','user',?,?)",
                  (time.time() - ago, chat, text))


def test_a_user_order_reaches_its_running_owner_and_tasks_answering_its_chat_once_quoting_the_users_words(
        env, tmp_path):
    from ttp import coordinator as coord, obligations
    p = make(env)
    owner, owner_dir = _order_run(p, tmp_path, "soak")
    asker, asker_dir = _order_run(p, tmp_path, "report", chat="c1")
    other, other_dir = _order_run(p, tmp_path, "unrelated", chat="c2")
    msg = _order_msg(p, "stop the soak after this cell\nand keep the logs")
    act = {"type": "obligation", "order": msg, "text": "stop the soak, keep logs", "owners": [owner],
           "action_class": "read", "supersedes": ["step 3: rerun every cell"]}
    assert coord.apply(p, [act]) == []
    steer = (owner_dir / "steer.md").read_text()
    assert "user's words (verbatim):\n> stop the soak after this cell\n> and keep the logs" in steer, steer
    assert "step 3: rerun every cell" in steer and "you own it" in steer and f"(turn user-order-{msg}-" in steer
    assert "you answer its chat" in (asker_dir / "steer.md").read_text()
    assert not (other_dir / "steer.md").exists(), "a task on another chat naming nothing of the order was told"
    log = _order_fanouts(p)
    assert len(log) == 1 and sorted(log[0]["tasks"]) == sorted([owner, asker]) and log[0]["msg"] == msg, log
    assert any(f"user msg #{msg} reached running #{owner}, #{asker}" in n for n in p.db.kv(coord.NOTES_KEY, []))
    # The same order again (linked by key, attached once more): no run gets it twice.
    oid = next(o["id"] for o in obligations.items(p.db) if (o.get("promise") or {}).get("order") == msg)
    assert coord.apply(p, [act, {"type": "obligation", "key": oid, "order": msg, "owners": [owner]}]) == []
    for d in (owner_dir, asker_dir):
        assert (d / "steer.md").read_text().count("user's words (verbatim)") == 1, d
    # A task created after the message carries the order in its spec: not told.
    late, late_dir = _order_run(p, tmp_path, "after", chat="c1", ago=5)
    coord.apply(p, [act])
    assert not (late_dir / "steer.md").exists()


def test_a_user_order_reaches_running_tasks_whose_spec_branch_or_resource_names_what_it_names(
        env, tmp_path, monkeypatch):
    from ttp import coordinator as coord, overlap
    p = make(env)
    by_spec, spec_dir = _order_run(p, tmp_path, "edit", spec="change plugins/x/runtime/ttp/gate.py to log")
    by_branch, branch_dir = _order_run(p, tmp_path, "branch work")
    by_res, res_dir = _order_run(p, tmp_path, "rig soak", resources=["rig-device"])
    other, other_dir = _order_run(p, tmp_path, "docs", spec="edit plugins/x/README.md", resources=["bench"])
    monkeypatch.setattr(overlap, "branches", lambda p, skip=(): [
        {"id": by_branch, "branch": "b", "head": "h", "open": True, "files": [("M", "plugins/x/runtime/ttp/gate.py")]},
        {"id": other, "branch": "o", "head": "h2", "open": True, "files": [("M", "plugins/x/docs/a.md")]}])
    msg = _order_msg(p, "the gate in runtime/ttp/gate.py must fail open; leave rig alone today")
    assert coord.apply(p, [{"type": "obligation", "order": msg, "text": "gate fails open",
                            "action_class": "read"}]) == []
    assert "your spec names runtime/ttp/gate.py" in (spec_dir / "steer.md").read_text()
    assert "your branch changes runtime/ttp/gate.py" in (branch_dir / "steer.md").read_text()
    assert "your resource rig-device" in (res_dir / "steer.md").read_text()
    assert not (other_dir / "steer.md").exists()
    assert sorted(_order_fanouts(p)[-1]["tasks"]) == sorted([by_spec, by_branch, by_res])


def test_an_order_that_matches_no_running_task_is_logged_and_tells_nobody(env, tmp_path):
    from ttp import coordinator as coord
    p = make(env)
    _, run_dir = _order_run(p, tmp_path, "unrelated", chat="c2")
    msg = _order_msg(p, "what is the weather")
    assert coord.apply(p, [{"type": "obligation", "order": msg, "text": "answer", "action_class": "read"}]) == []
    assert not (run_dir / "steer.md").exists()
    log = _order_fanouts(p)
    assert log[-1]["tasks"] == [] and f"user msg #{msg} matched no running task" in log[-1]["text"], log


def test_a_restrictions_change_reaches_every_running_run_with_the_users_words_and_is_logged(env, tmp_path):
    from ttp import coordinator as coord
    p = make(env)
    runs = [_order_run(p, tmp_path, f"job {i}", chat=f"c{i}") for i in range(3)]
    msg = _order_msg(p, "Never push to main.", ago=5)
    assert coord.apply(p, [{"type": "obligation", "order": msg, "text": "no pushes to main",
                            "action_class": "read"}]) == []   # job 1 answers its chat: told the order first
    assert coord.apply(p, [{"type": "charter_update", "section": "Restrictions", "text": "Never push to main.",
                            "source": G}], user_turn=True, messages=[msg]) == []
    for _, d in runs:
        steer = (d / "steer.md").read_text()
        assert "New binding restriction: Never push to main." in steer and "> Never push to main." in steer, steer
    assert sorted(_order_fanouts(p)[-1]["tasks"]) == sorted(t for t, _ in runs)
    # A Goals change reaches only the running tasks it touches; a run with no task gets only restrictions.
    gate, gate_dir = _order_run(p, tmp_path, "gate", spec="edit plugins/x/runtime/ttp/gate.py")
    bare = tmp_path / "bare"
    bare.mkdir()
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(NULL,'worker','fake',?,'running',?)",
           (time.time(), str(bare)))
    assert coord.apply(p, [{"type": "charter_update", "section": "Goals",
                            "text": "Make runtime/ttp/gate.py fail open."}]) == []
    assert "Goals changed (Goals and success criteria): Make runtime/ttp/gate.py fail open." in (gate_dir / "steer.md").read_text()
    assert all("fail open" not in (d / "steer.md").read_text() for _, d in runs)
    assert not (bare / "steer.md").exists() and _order_fanouts(p)[-1]["tasks"] == [gate]
    assert coord.apply(p, [{"type": "charter_update", "section": "Restrictions", "text": "Never reboot rig.",
                            "source": G}]) == []
    assert "Never reboot rig." in (bare / "steer.md").read_text()


def test_a_goals_change_and_its_order_on_one_user_turn_both_reach_a_run_once_and_goals_skip_the_chat(
        env, tmp_path):
    from ttp import coordinator as coord
    p = make(env)
    t, d = _order_run(p, tmp_path, "gate", spec="edit runtime/ttp/gate.py", chat="c1")
    asker, asker_dir = _order_run(p, tmp_path, "report", chat="c1")
    msg = _order_msg(p, "Make runtime/ttp/gate.py fail open.")
    goals = {"type": "charter_update", "section": "Goals", "text": "Make runtime/ttp/gate.py fail open."}
    order = {"type": "obligation", "order": msg, "text": "gate fails open", "owners": [t], "action_class": "read",
             "supersedes": ["step 4: add retries"]}
    assert coord.apply(p, [goals, order], user_turn=True, messages=[msg]) == []
    steer = (d / "steer.md").read_text()
    assert "Goals changed (" in steer and "step 4: add retries" in steer and "you own it" in steer, steer
    assert f"(turn user-goals-{msg}-" in steer and f"(turn user-order-{msg}-" in steer, steer
    # The Goals change reaches only the tasks it touches: the asker hears the order (its chat), not the Goals.
    ask = (asker_dir / "steer.md").read_text()
    assert "you answer its chat" in ask and "Goals changed" not in ask, ask
    # The same Goals change and order again for that message: neither is appended twice to one run.
    coord.apply(p, [goals, order], user_turn=True, messages=[msg])
    steer = (d / "steer.md").read_text()
    assert steer.count("Goals changed (") == 1 and steer.count("step 4: add retries") == 1, steer
    # A different Goals change on the same message still reaches the run.
    coord.apply(p, [dict(goals, text="Keep runtime/ttp/gate.py logging.")], user_turn=True, messages=[msg])
    assert (d / "steer.md").read_text().count("Goals changed (") == 2


def test_an_order_naming_a_resource_keeps_its_resource_steer_and_is_not_told_twice(env, tmp_path):
    from ttp import coordinator as coord
    p, ids, msg, run_dir, other_dir = _ordered(env, tmp_path)
    steer = (run_dir / "steer.md").read_text()
    assert "Newer user order" in steer and "ttp lock --probe rig" in steer
    assert coord.apply(p, [{"type": "obligation", "order": msg, "text": "power-cycle rig",
                            "owners": [ids["order"]], "action_class": "device"}]) == []
    assert (run_dir / "steer.md").read_text() == steer, "the run told of the order on rig got it again"
    assert not (other_dir / "steer.md").exists()


def _uo_add(cite_order, p):
    """A task_add that is a user order on rig (a nonempty note) or a plain one (an empty note)."""
    act = {"type": "task_add", "title": "power-cycle rig", "spec": "power-cycle rig", "tier": "light",
           "resources": ["rig"]}
    if cite_order:
        act["user_msg"] = p.db.x("INSERT INTO messages(ts,direction,kind,text) VALUES(?,'in','user','power-cycle rig')",
                                 (time.time() - 60,))
    return act


@pytest.mark.parametrize("cite_order", [False, True])
def test_task_updates_after_a_task_add_in_one_batch_all_apply_with_or_without_its_user_order_note(env, cite_order):
    """apply's action-order list is never overwritten by a task_add's user-order note: every later
    task_update applies, and a hold on `ask:new` still sees the ask_user later in the batch."""
    from ttp import coordinator as coord
    p = make(env)
    ids = [p.db.add_task(f"soak {n}", "spec", kind="work", tier="light", origin="user") for n in range(4)]
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    out = coord.apply(p, [_uo_add(cite_order, p),
                          *({"type": "task_update", "id": t, "priority": 1} for t in ids[:3]),
                          {"type": "task_update", "id": ids[3], "status": "blocked", "waits_on": "ask:new"},
                          {"type": "ask_user", "text": "May I spend $40 on the long run?", "blocking": "spend"}])
    assert out == [], out
    assert [p.db.task(t)["priority"] for t in ids[:3]] == [1, 1, 1]
    aid = p.db.one("SELECT MAX(id) id FROM messages WHERE kind='ask'")["id"]
    assert p.db.task(ids[3])["status"] == "blocked" and f'"waits:ask:{aid}"' in p.db.task(ids[3])["labels"]
    notes = p.db.kv(coord.NOTES_KEY, [])
    assert any("is the latest user order" in n for n in notes) == cite_order, notes


@pytest.mark.parametrize("cite_order", [False, True])
def test_the_started_on_host_check_after_a_task_add_still_sees_the_rest_of_the_batch(env, cite_order):
    """A reply after a task_add is checked against the batch's later actions: the task it speaks of is
    cancelled later in the batch, so the claim is sent as written, with no check failure."""
    from ttp import coordinator as coord, onhost
    p = make(env)
    text = "Power cycling rig now, nothing else touches it."
    coord.apply(p, [_uo_add(cite_order, p),
                    {"type": "reply", "text": text, "chat": "c1"},
                    {"type": "task_update", "id": 1, "status": "cancelled"}], turn=1)
    assert p.db.task(1)["title"] == "power-cycle rig" and p.db.task(1)["status"] == "cancelled"
    assert [m["text"] for m in p.db.q("SELECT text FROM messages WHERE direction='out' AND chat='c1'")] == [text]
    assert not any("started-on-host check failed" in n for n in p.db.kv(coord.NOTES_KEY, []))
