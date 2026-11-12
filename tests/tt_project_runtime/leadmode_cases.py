# Lead mode tests (leadmode.py) of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there). Add new lead-mode tests here, not there.
from __future__ import annotations


def _lead_task(**k):
    return {"kind": "work", "attempts": 0, "branch": None, "spec": "", "origin": "coordinator", **k}


def test_lead_mode_leaves_work_for_the_coordinator_and_gives_workers_the_rest():
    from ttp import leadmode
    w = lambda **k: leadmode.worker_task(_lead_task(**k))
    assert not w() and not w(kind="question") and not w(kind="plan") and not w(kind="harness")
    assert w(kind="review") and w(kind="code")
    assert w(attempts=1) and w(branch="ttp/t1-x") and w(spent_usd=0.4), "work a worker began stays with workers"
    assert w(spec="measure it\nworker: a 6 h soak nobody watches")
    # What the daemon queued on its own waits for the lead first, code too; its reviews do not.
    assert not w(kind="code", origin="daemon") and w(kind="review", origin="daemon")


def test_lead_mode_dispatch_holds_the_coordinators_tasks_and_off_lets_them_go(env, monkeypatch):
    from ttp import leadmode
    p, d, clock, _ = _batch_setup(env, monkeypatch, busy=0)
    work = p.db.add_task("read the floor", "s", kind="work", origin="coordinator")
    code = p.db.add_task("fix it", "s", kind="code", origin="coordinator")
    p.set_config("coordinator.lead", True)
    d.cfg = p.config()
    ready = p.db.ready_tasks()
    assert [t["id"] for t in d._lead_gate(ready)] == [code]
    assert p.db.task(work)["blocked_reason"] == leadmode.HELD_NOTE
    assert [t["id"] for t in d._lead_ready(ready)] == [code], "stall and idle checks count only worker tasks"
    p.set_config("coordinator.lead", False)
    d.cfg = p.config()
    assert {t["id"] for t in d._lead_gate(p.db.ready_tasks())} == {work, code}
    assert p.db.task(work)["blocked_reason"] is None


def test_lead_mode_wakes_the_coordinator_while_its_list_has_work_and_backs_off_when_nothing_moves(env, monkeypatch):
    p, d, clock, _ = _batch_setup(env, monkeypatch, busy=0)
    p.set_config("coordinator.idle_wake_s", 6 * 3600)
    p.set_config("coordinator.starve_wake_s", 6 * 3600)
    p.set_config("coordinator.lead", True)
    d.cfg = p.config()
    turns = []

    def start_run(*a, **k):   # each turn ends at once and changes nothing
        turns.append((clock[0], (k.get("note") or {}).get("wake_due"), a[1] if len(a) > 1 else ""))
        p.db.x("INSERT INTO runs(role, started, ended, status) VALUES('coordinator', ?, ?, 'ok')",
               (clock[0], clock[0]))
        return 1
    monkeypatch.setattr(d, "start_run", start_run)
    p.db.x("INSERT INTO runs(role, started, ended, status) VALUES('coordinator', ?, ?, 'ok')", (clock[0], clock[0]))
    p.db.set_kv("last_coordinator_turn", clock[0])
    assert not _turn_within(d, clock, turns, 600, step=10), "an empty list woke the lead"
    tid = p.db.add_task("measure the floor", "s", kind="work", origin="coordinator")
    assert _turn_within(d, clock, turns, 120, step=10), "the lead's work did not wake it"
    assert turns[-1][1] == "lead" and f"#{tid}" in turns[-1][2] and "Lead work list" in turns[-1][2]
    gaps = []
    for _ in range(3):   # nothing moved: each next wake waits longer
        t0 = clock[0]
        assert _turn_within(d, clock, turns, 4000, step=10)
        gaps.append(turns[-1][0] - t0)
    assert gaps[0] < gaps[1] < gaps[2], gaps
    p.db.update_task(tid, spec="s\nprogress: f101 measured, f202 next")
    assert _turn_within(d, clock, turns, 120, step=10), "progress did not reset the back-off"
    p.db.update_task(tid, status="done")
    assert not _turn_within(d, clock, turns, 3600, step=30), "a lead wake with nothing on its list"


def test_lead_mode_rules_are_in_the_session_prompt_only_when_on(env):
    from ttp.prompts import coordinator_session_role
    p = make(env)
    off = coordinator_session_role(p)
    assert "Lead mode" not in off and "still go to workers" in off
    p.set_config("coordinator.lead", True)
    on = coordinator_session_role(p)
    assert "## Lead mode: you do the work" in on and "still go to workers" not in on
    assert "<!-- ttp:" not in on and "<!-- /ttp:" not in on
