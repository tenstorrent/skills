# Stale-gate sweep tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects these
# tests under its name. Add new stalegates.py tests (re-home, dead or unneeded dependency, unreasoned gate,
# promise owners, detector, must-act settle) here.
from __future__ import annotations

_SG_MACHINES = {"box-a": {"tags": ["cpu"]}, "box-b": {"tags": ["cpu"]}, "box-c": {"tags": ["gpu"]}}
_SG_CFG = {"coordinator": {"stale_gate_min": 30}, "machines": {"allowed": ["box-a", "box-b", "box-c"]}}
_SG_OLD = 3600   # seconds past the task's creation: well over the 30 min threshold


def _sg_env(monkeypatch):
    from ttp import machines
    monkeypatch.setattr(machines, "load", lambda: dict(_SG_MACHINES))
    monkeypatch.setattr(machines, "trouble", lambda db, now=None, seen=None: {})


def _sg_task(p, title="run the suite", spec="s", labels=(), status="queued", deps=(), kind="work"):
    tid = p.db.add_task(title, spec, kind=kind)
    p.db.update_task(tid, labels=list(labels), depends_on=list(deps), status=status)
    return tid


def _sg_labels(p, tid):
    return json.loads(p.db.task(tid)["labels"])


def _sg_events(p, status=None):
    return p.db.q("SELECT * FROM events WHERE kind='stale_gate'" + (f" AND status='{status}'" if status else "")
                  + " ORDER BY id")


def _sg_hold(p, name, by="user", reason="hold"):
    from ttp.db import PAUSED_RESOURCES_KEY
    p.db.set_kv(PAUSED_RESOURCES_KEY, {**p.db.kv(PAUSED_RESOURCES_KEY, {}),
                                       name: {"by": by, "reason": reason, "since": time.time()}})


def test_stalegate_held_machine_rehomes_the_task_and_keeps_the_hold(env, monkeypatch):
    """A queued task on a machine the user paused moves at once to another allowed machine of the same tag;
    the user's pause stays as it was, and the move is logged on the task."""
    from ttp import stalegates
    from ttp.db import PAUSED_RESOURCES_KEY
    _sg_env(monkeypatch)
    p = make(env)
    _sg_hold(p, "box-a-dev")
    held = p.db.kv(PAUSED_RESOURCES_KEY, {})
    tid = _sg_task(p, labels=["resource:box-a-dev", "exclusive:box-a-dev"])
    done = stalegates.sweep(p, _SG_CFG, time.time())
    assert done and _sg_labels(p, tid) == ["resource:box-b-dev", "exclusive:box-b-dev"]
    assert p.db.task(tid)["status"] == "queued" and p.db.kv(PAUSED_RESOURCES_KEY, {}) == held
    obs = p.db.q("SELECT text FROM events WHERE fingerprint LIKE ?", (f"stale_gate_cleared:{tid}:%",))
    assert obs and "box-a-dev is held (the user's pause" in obs[0]["text"] and "box-b" in obs[0]["text"]
    assert stalegates.sweep(p, _SG_CFG, time.time()) == []   # nothing left to fix


def test_stalegate_lent_machine_rehomes_a_blocked_task(env, monkeypatch):
    """A machine lent to another team (a project hold ending on a probe) frees a task blocked on it: moved to
    another allowed machine, requeued, its blocked reason gone."""
    from ttp import stalegates
    _sg_env(monkeypatch)
    p = make(env)
    _sg_hold(p, "box-a", by="project", reason="lent to another team until the hand-back probe passes")
    tid = _sg_task(p, labels=["resource:box-a"], status="blocked")
    p.db.update_task(tid, blocked_reason="box-a is held")
    stalegates.sweep(p, _SG_CFG, time.time())
    t = p.db.task(tid)
    assert t["status"] == "queued" and t["blocked_reason"] is None and _sg_labels(p, tid) == ["resource:box-b"]


def test_stalegate_outage_rehomes_and_machine_specific_or_no_alternative_raises(env, monkeypatch):
    """A machine marked out of service moves a task off it; a task naming the machine itself, or one whose machine
    has no allowed alternative, is raised as stale_gate-<task> once it is over 30 min old, never moved."""
    from ttp import machine_ledger, stalegates
    _sg_env(monkeypatch)
    p = make(env)
    machine_ledger.set_outage("box-a", "repair", "demo", "2h")
    machine_ledger.set_outage("box-c", "repair", "demo", "2h")
    moved = _sg_task(p, labels=["resource:box-a"])
    named = _sg_task(p, title="check box-a firmware", labels=["resource:box-a"])
    alone = _sg_task(p, labels=["resource:box-c"])
    now = time.time()
    stalegates.sweep(p, _SG_CFG, now)
    assert _sg_labels(p, moved) == ["resource:box-b"]
    assert _sg_labels(p, named) == ["resource:box-a"] and _sg_labels(p, alone) == ["resource:box-c"]
    assert _sg_events(p) == []   # under 30 min: not yet raised
    stalegates.sweep(p, _SG_CFG, now + _SG_OLD)
    ev = _sg_events(p, "queued")
    assert {json.loads(e["data"])["must_act"]["key"] for e in ev} == {f"stale_gate-{named}", f"stale_gate-{alone}"}
    assert any("names the machine itself" in e["text"] for e in ev) and any("no other machine" in e["text"].lower() for e in ev)
    assert len(stalegates.sweep(p, _SG_CFG, now + _SG_OLD + 60)) == 0   # raised once


def test_stalegate_dead_dependency_dropped_unless_the_spec_names_it(env, monkeypatch):
    """A cancelled dependency the task never names is dropped and the task requeued; one its spec names is
    raised for the coordinator to re-point instead."""
    from ttp import stalegates
    _sg_env(monkeypatch)
    p = make(env)
    dead = _sg_task(p, status="cancelled")
    other = _sg_task(p, status="done")
    free = _sg_task(p, deps=[dead, other], status="blocked")
    p.db.update_task(free, blocked_reason=f"dependency #{dead} failed")
    named = _sg_task(p, spec=f"continue the work of #{dead}", deps=[dead], status="blocked")
    now = time.time()
    stalegates.sweep(p, _SG_CFG, now)
    t = p.db.task(free)
    assert json.loads(t["depends_on"]) == [other] and t["status"] == "queued"
    assert p.db.task(named)["status"] == "blocked" and _sg_events(p) == []
    stalegates.sweep(p, _SG_CFG, now + _SG_OLD)
    assert [json.loads(e["data"])["must_act"]["key"] for e in _sg_events(p, "queued")] == [f"stale_gate-{named}"]


def test_stalegate_unneeded_dependency_dropped_after_30_min_only(env, monkeypatch):
    """A live dependency whose output the task neither names nor shares a file with is dropped, but only after
    the gate is 30 min old; a task naming a file the dependency touches, or a review, keeps its gate."""
    from ttp import stalegates
    _sg_env(monkeypatch)
    p = make(env)
    dep = _sg_task(p, title="edit lib/core.py", spec="change lib/core.py", status="queued")
    running = _sg_task(p, title="build the image", spec="build", status="running")
    ahead = _sg_task(p, title="write notes", spec="notes", deps=[running])
    loose = _sg_task(p, title="write the docs", spec="docs/guide.md", deps=[dep])
    shared = _sg_task(p, title="test lib/core.py", spec="run lib/core.py tests", deps=[dep])
    review = _sg_task(p, title="review the change", spec="s", deps=[dep], kind="review")
    now = time.time()
    stalegates.sweep(p, _SG_CFG, now)
    assert json.loads(p.db.task(loose)["depends_on"]) == [dep]   # too young
    stalegates.sweep(p, _SG_CFG, now + _SG_OLD)
    assert json.loads(p.db.task(loose)["depends_on"]) == []
    assert json.loads(p.db.task(shared)["depends_on"]) == [dep]
    assert json.loads(p.db.task(review)["depends_on"]) == [dep]
    assert json.loads(p.db.task(ahead)["depends_on"]) == [running]   # a running dependency is making progress


def test_stalegate_unreasoned_gate_cleared_after_30_min_and_cited_one_kept(env, monkeypatch):
    """A start_when gate over 30 min old that states no reason is cleared and the task requeued; one with a
    why, a cited rule id or a `needs #id` stays."""
    from ttp import stalegates
    _sg_env(monkeypatch)
    p = make(env)
    since = time.time() - _SG_OLD
    gate = lambda extra=(): ["start_when:test -e /nowhere", f"deferred_since:{since}", *extra]   # noqa: E731
    bare = _sg_task(p, labels=gate())
    why = _sg_task(p, labels=gate(["start_why:the data lands"]))
    rule = _sg_task(p, spec="waits per R-hold-17", labels=gate())
    needs = _sg_task(p, spec="needs #3 output", labels=gate())
    now = time.time()
    stalegates.sweep(p, _SG_CFG, now - 20 * 60)   # the gate is 40 min old at that moment: over the threshold
    assert _sg_labels(p, bare) == []
    for tid in (why, rule, needs):
        assert "start_when:test -e /nowhere" in _sg_labels(p, tid)
    assert p.db.q("SELECT id FROM events WHERE fingerprint LIKE ?", (f"stale_gate_cleared:{bare}:gate:%",))


def test_stalegate_promise_whose_only_owner_is_stuck_is_repointed_or_raised(env, monkeypatch):
    """A promise whose every live owner sits behind a stale gate gets its one clear replacement; with none,
    it is raised as stale_gate-<id>; a later open task naming the owner becomes its owner."""
    from ttp import obligations, stalegates
    _sg_env(monkeypatch)
    p = make(env)
    _sg_hold(p, "box-c")
    owner = _sg_task(p, title="the promised work", labels=["resource:box-c"])   # no alternative of its tag
    now = time.time()
    oid = obligations.add(p, "ship the promised work", [owner], now=now)
    stalegates.sweep(p, _SG_CFG, now + _SG_OLD)
    keys = {json.loads(e["data"])["must_act"]["key"] for e in _sg_events(p, "queued")}
    assert keys == {f"stale_gate-{owner}", f"stale_gate-{oid}"}
    carrier = _sg_task(p, title="carry on", spec=f"carries on #{owner}")
    stalegates.sweep(p, _SG_CFG, now + _SG_OLD + 60)
    assert carrier in obligations._owner_ids(obligations.get(p.db, oid))


def test_stalegate_detector_fires_in_the_course_check_and_off_switch(env, monkeypatch):
    """The Course check names a task held over 30 min behind a held machine and nothing under 30 min; the
    threshold 0 turns the sweep and the detector off."""
    from ttp import coursecheck, stalegates
    _sg_env(monkeypatch)
    p = make(env)
    _sg_hold(p, "box-c")
    tid = _sg_task(p, labels=["resource:box-c"])
    now = time.time()
    assert not any("stale gate" in x for x in coursecheck.lines(p.db, _SG_CFG, None, now)[0])
    lines, ident = coursecheck.lines(p.db, _SG_CFG, None, now + _SG_OLD)
    assert any(f"#{tid}" in x and "stale gate" in x and "box-c is held" in x for x in lines), lines
    assert f"stale_gate:{tid}" in ident
    off = {**_SG_CFG, "coordinator": {"stale_gate_min": 0}}
    assert stalegates.findings(p.db, off, now + _SG_OLD) == [] and stalegates.sweep(p, off, now + _SG_OLD) == []


def test_stalegate_a_machine_name_sharing_a_prefix_is_not_renamed_and_a_probe_naming_it_is_raised(env, monkeypatch):
    """Only resources on the held machine move: `box-ab` is another machine than `box-a`; a task whose probe
    waits on the held machine is not moved but raised; a dead dependency whose files the task shares is raised."""
    from ttp import stalegates
    _sg_env(monkeypatch)
    p = make(env)
    _sg_hold(p, "box-a")
    two = _sg_task(p, labels=["resource:box-a", "read:box-ab"])
    probe = _sg_task(p, labels=["resource:box-a", "start_when:ssh box-a true", f"deferred_since:{time.time() - _SG_OLD}"])
    dead = _sg_task(p, title="fix lib/core.py", spec="lib/core.py", status="failed")
    shared = _sg_task(p, title="test lib/core.py", spec="run lib/core.py", deps=[dead], status="blocked")
    now = time.time()
    stalegates.sweep(p, _SG_CFG, now)
    assert _sg_labels(p, two) == ["resource:box-b", "read:box-ab"]
    assert "resource:box-a" in _sg_labels(p, probe) and p.db.task(shared)["status"] == "blocked"
    stalegates.sweep(p, _SG_CFG, now + _SG_OLD)
    keys = {json.loads(e["data"])["must_act"]["key"] for e in _sg_events(p, "queued")}
    assert keys == {f"stale_gate-{probe}", f"stale_gate-{shared}"}


def test_stalegate_a_busy_lock_in_trouble_is_not_a_down_machine(env, monkeypatch):
    """Resources the trouble report lists that are no known machine (a lock, a pool) never move or raise a task."""
    from ttp import machines, stalegates
    _sg_env(monkeypatch)
    monkeypatch.setattr(machines, "trouble", lambda db, now=None, seen=None: {"device": {}, "box-c": {}})
    p = make(env)
    pooled = _sg_task(p, labels=["resource:device"])
    gone = _sg_task(p, labels=["resource:box-c"])
    now = time.time() + _SG_OLD
    stalegates.sweep(p, _SG_CFG, now)
    assert _sg_labels(p, pooled) == ["resource:device"] and _sg_events(p) != []
    assert {json.loads(e["data"])["must_act"]["key"] for e in _sg_events(p)} == {f"stale_gate-{gone}"}


def test_stalegate_a_planned_future_start_after_is_never_cleared(env, monkeypatch):
    """A start_after still in the future is a planned time deferral, never an unreasoned gate: it is
    kept with no why and no cited id, however old the deferral; a past one is still cleared."""
    from ttp import stalegates
    _sg_env(monkeypatch)
    p = make(env)
    now = time.time()
    planned = _sg_task(p, labels=[f"start_after:{now + 2 * 3600:.0f}",
                                  f"deferred_since:{now - 3600:.0f}"])
    past = _sg_task(p, labels=[f"start_after:{now - 60:.0f}", f"deferred_since:{now - 3600:.0f}"])
    stalegates.sweep(p, _SG_CFG, now)
    assert _sg_labels(p, planned) == [f"start_after:{now + 2 * 3600:.0f}", f"deferred_since:{now - 3600:.0f}"]
    assert _sg_labels(p, past) == []


def test_stalegate_a_task_blocked_on_a_user_ask_stays_blocked(env, monkeypatch):
    """Dropping a dead dependency never starts a task held for another reason: one blocked on a user
    ask keeps its status and blocked reason, only the dependency is dropped."""
    from ttp import stalegates
    _sg_env(monkeypatch)
    p = make(env)
    dead = _sg_task(p, status="failed")
    ask = _sg_task(p, deps=[dead], status="blocked")
    p.db.update_task(ask, blocked_reason="ask #42: waiting on the user's word")
    stalegates.sweep(p, _SG_CFG, time.time())
    t = p.db.task(ask)
    assert json.loads(t["depends_on"]) == [] and t["status"] == "blocked"
    assert t["blocked_reason"] == "ask #42: waiting on the user's word"


def test_stalegate_a_wait_whose_retry_when_is_the_dead_dependency_is_released(env, monkeypatch):
    """A waiting task whose retry probe waits on the dead dependency is released once the sweep
    drops it: requeued at once, its retry timer cleared."""
    from ttp import stalegates
    from ttp.db import dump_result
    _sg_env(monkeypatch)
    p = make(env)
    dead = _sg_task(p, status="cancelled")
    waiting = _sg_task(p, deps=[dead])
    p.db.update_task(waiting, not_before=time.time() + 1800,
                     result=dump_result({"status": "waiting", "summary": "waits",
                                         "retry_when": f"landed:#{dead}"}))
    stalegates.sweep(p, _SG_CFG, time.time())
    t = p.db.task(waiting)
    assert json.loads(t["depends_on"]) == [] and t["status"] == "queued" and t["not_before"] is None


def test_stalegate_rehomes_only_onto_a_resource_the_target_machine_has(env, monkeypatch):
    """A resource is re-homed only when the swapped name lands on the target machine: when the new
    name is another machine's, the task is left where it was instead of re-pointed wrong."""
    from ttp import machines, stalegates
    _sg_env(monkeypatch)
    monkeypatch.setattr(machines, "load", lambda: {"box-a": {"tags": ["cpu"]},
                                                   "box-b": {"tags": ["cpu"]},
                                                   "box-b-dev": {"tags": ["cpu"]}})
    p = make(env)
    _sg_hold(p, "box-a")
    clash = _sg_task(p, labels=["resource:box-a-dev"])
    plain = _sg_task(p, labels=["resource:box-a"])
    stalegates.sweep(p, _SG_CFG, time.time())
    assert _sg_labels(p, clash) == ["resource:box-a-dev"]      # box-b-dev is another machine
    assert _sg_labels(p, plain) == ["resource:box-b"]
