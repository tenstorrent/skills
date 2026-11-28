# Budget raise and budget carry tests of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. A raise reaches a running task at once, and a task budget never
# stops a head whose checks passed.
from __future__ import annotations


def _br_daemon(p, monkeypatch, head=""):
    from ttp.daemon import Daemon
    d = Daemon(p.base); d.jev = None
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.base), None))
    monkeypatch.setattr(d, "start_run", lambda *a, **k: 0)
    monkeypatch.setattr(d, "_branch_head", lambda t: head)
    return d


def _br_running(p, tid, started):
    rid = p.db.x("INSERT INTO runs(task, role, status, started, note) VALUES(?,?,?,?,?)",
                 (tid, "worker", "running", started, "{}"))
    p.db.update_task(tid, status="running")
    run_dir = p.runs / str(rid); run_dir.mkdir(parents=True, exist_ok=True)
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
    return dict(p.db.one("SELECT * FROM runs WHERE id=?", (rid,))), run_dir


def _br_cap_stop(cost=2.0):
    from ttp.providers.base import RunUsage
    return RunUsage(cost_usd=cost, error="error_max_budget_usd: Reached maximum budget",
                    extra={"subtype": "error_max_budget_usd"})


def test_budget_raise_reaches_a_running_task_and_its_run_dir_at_once(env):
    from ttp import coordinator as coord, topup
    from ttp.runner import LIVE_BUDGET
    p = make(env)
    tid = p.db.add_task("busy", "s", budget_usd=2.0)
    _, run_dir = _br_running(p, tid, time.time() - 60)
    assert coord.apply(p, [{"type": "task_update", "id": tid, "budget_usd": 6}]) == []
    t = p.db.task(tid)
    assert (t["budget_usd"], t["status"]) == (6.0, "running")
    assert float((run_dir / LIVE_BUDGET).read_text()) == 6.0
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind=? AND task=?", (topup.RAISE_KIND, tid))["n"] == 1
    out = coord.apply(p, [{"type": "task_update", "id": tid, "budget_usd": 3}])
    assert any("only raises a budget" in x for x in out), out
    assert float((run_dir / LIVE_BUDGET).read_text()) == 6.0


def test_budget_raise_live_budget_only_ever_raises_the_runs_limit(tmp_path):
    from ttp.runner import LIVE_BUDGET, live_budget
    assert live_budget(tmp_path, 2.0) == 2.0
    (tmp_path / LIVE_BUDGET).write_text("6.00\n")
    assert live_budget(tmp_path, 2.0) == 6.0
    (tmp_path / LIVE_BUDGET).write_text("1.00\n")
    assert live_budget(tmp_path, 2.0) == 2.0
    (tmp_path / LIVE_BUDGET).write_text("junk")
    assert live_budget(tmp_path, 2.0) == 2.0
    assert live_budget(tmp_path, None) is None


def test_budget_raise_requeues_a_run_its_fixed_agent_cap_stopped_after_the_raise(env, monkeypatch, tmp_path):
    from ttp import coordinator as coord
    p = make(env)
    tid = p.db.add_task("routine", "s", kind="work", tier="light", origin="coordinator", budget_usd=2.0)
    run, _ = _br_running(p, tid, time.time() - 60)
    assert coord.apply(p, [{"type": "task_update", "id": tid, "budget_usd": 6}]) == []
    p.db.update_task(tid, spent_usd=2.0)
    d = _br_daemon(p, monkeypatch)
    d._finish_worker(run, _br_cap_stop(), "failed", tmp_path)
    t = p.db.task(tid)
    assert t["status"] == "queued" and not t["attempts"], dict(t)


def test_budget_raise_absent_a_cap_stop_still_spends_an_attempt(env, monkeypatch, tmp_path):
    p = make(env)
    tid = p.db.add_task("routine", "s", kind="work", tier="light", origin="coordinator", budget_usd=2.0)
    run, _ = _br_running(p, tid, time.time() - 60)
    p.db.update_task(tid, spent_usd=2.0)
    d = _br_daemon(p, monkeypatch)
    d._finish_worker(run, _br_cap_stop(), "failed", tmp_path)
    assert p.db.task(tid)["attempts"] == 1   # an ordinary failed run: its attempt is spent


def _br_code_task(p, head, passed=True):
    tid = p.db.add_task("code", "s", kind="code", tier="light", origin="coordinator", budget_usd=2.0)
    p.db.update_task(tid, branch=f"ttp/t{tid}-code", spent_usd=2.0)
    run, run_dir = _br_running(p, tid, time.time() - 60)
    (run_dir / "checks.json").write_text(json.dumps(
        {"head": head, "passed": passed, "commands": ["pytest -q tests/test_dev.py"], "purpose": "candidate"}))
    return tid, run


def test_budget_carry_hands_off_a_head_whose_checks_passed(env, monkeypatch, tmp_path):
    from ttp import budgetcarry
    p = make(env)
    tid, run = _br_code_task(p, "abc123def4567")
    d = _br_daemon(p, monkeypatch, head="abc123def4567")
    d._finish_worker(run, _br_cap_stop(), "failed", tmp_path)
    t = p.db.task(tid)
    assert t["status"] == "done", dict(t)
    res = json.loads(t["result"]) if isinstance(t["result"], str) else t["result"]
    assert res["budget_carry"]["head"] == "abc123def4567"
    assert res["budget_carry"]["commands"] == ["pytest -q tests/test_dev.py"]
    assert any("budget overrun" in f for f in res["followups"])
    ev = p.db.one("SELECT data FROM events WHERE kind=? AND task=?", (budgetcarry.KIND, tid))
    assert json.loads(ev["data"])["spent"] == 2.0


def test_budget_carry_never_hands_off_without_a_pass_on_the_current_head(env, monkeypatch, tmp_path):
    from ttp import budgetcarry
    p = make(env)
    failed, run = _br_code_task(p, "abc123def4567", passed=False)
    _br_daemon(p, monkeypatch, head="abc123def4567")._finish_worker(run, _br_cap_stop(), "failed", tmp_path)
    other, run2 = _br_code_task(p, "abc123def4567")
    _br_daemon(p, monkeypatch, head="fff999")._finish_worker(run2, _br_cap_stop(), "failed", tmp_path)
    for tid in (failed, other):
        assert p.db.task(tid)["status"] != "done"
    assert not p.db.one("SELECT 1 FROM events WHERE kind=?", (budgetcarry.KIND,))
