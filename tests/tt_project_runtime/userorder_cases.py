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
    steer = (run_dir / "steer.md").read_text()
    assert "Newer user order" in steer and f"user msg #{msg}" in steer and "ttp lock --probe rig" in steer
    assert not (other_dir / "steer.md").exists()   # a running task on another resource is not told


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
