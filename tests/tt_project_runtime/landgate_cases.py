# Area case file for tests/test_tt_project_runtime.py: landgate (the size and lean-code checks never stop a
# head whose checks passed; they add follow-up tasks instead).
from __future__ import annotations


def _lg_checks(p, tid, head, passed=True):
    """A finished run of task `tid` that recorded a candidate `ttp checks` result on `head`."""
    rid = p.db.x("INSERT INTO runs(task, role, status, started, note) VALUES(?,?,?,?,?)",
                 (tid, "worker", "ok", time.time() - 60, "{}"))
    (p.runs / str(rid)).mkdir(parents=True, exist_ok=True)
    (p.runs / str(rid) / "checks.json").write_text(json.dumps(
        {"head": head, "passed": passed, "commands": ["pytest -q"], "purpose": "candidate"}))


def _lg_review(env, p, rid, verdict, checked_head=""):
    """Review `rid` handed off done with lane verdict `verdict`; with `checked_head`, its run recorded a
    candidate `ttp checks` pass on that head."""
    from ttp import ponytail
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p.db.update_task(rid, status="running")
    run_dir = env["tmp"] / f"lg-run-{rid}"
    run_dir.mkdir()
    (run_dir / ponytail.FILE).write_text(json.dumps(verdict))
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "correct"}))
    if checked_head:
        _lg_checks(p, rid, checked_head)
    d = Daemon(p.base)
    d._finish_worker({"task": rid}, Usage(cost_usd=1.0), "ok", run_dir)
    d.reconcile_tasks()
    return p.db.task(rid)


def _lg_followups(p):
    from ttp import landgate
    return p.db.q("SELECT * FROM tasks WHERE labels LIKE ?", (f"%{landgate.KIND}:%",))


def test_landgate_a_lane_block_on_a_checked_head_becomes_a_followup_not_a_failed_review(env, monkeypatch):
    from ttp.daemon import CHANGES_NEEDED
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    code, branch, _, (rev,) = _finish_code(env, p, "ordered", {"b.py": 30})
    head = _git_out(p.root, "rev-parse", branch).strip()
    fail = {"verdict": "fail", "head": head, "findings": ["helper.py should not exist"]}
    t = _lg_review(env, p, rev["id"], fail, checked_head=head)
    assert t["status"] == "done", t["result"]
    res = json.loads(t["result"])
    (f,) = _lg_followups(p)
    assert res["ponytail_followup"] == f["id"] and "not blocking" in res["summary"]
    assert f["title"].startswith("Fix the ponytail lane's findings on #")
    assert f"start_when:landed:#{f['title'].split('#')[1].split()[0]}" in f["labels"]
    assert "quoted user order" in f["spec"]

    _, branch, _, (rev,) = _finish_code(env, p, "unchecked", {"c.py": 30})
    head = _git_out(p.root, "rev-parse", branch).strip()
    t = _lg_review(env, p, rev["id"], dict(fail, head=head))
    assert t["status"] == "failed" and json.loads(t["result"])["status"] == CHANGES_NEEDED
    assert len(_lg_followups(p)) == 1


def test_landgate_the_size_rule_on_a_checked_head_adds_explain_or_split(env, monkeypatch):
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    p.set_config("review.ponytail_land_lines", 60)
    _, branch, _, (rev,) = _finish_code(env, p, "big", {"big.py": 45})
    p.set_config("review.ponytail_land_lines", 30)
    head = _git_out(p.root, "rev-parse", branch).strip()
    t = _lg_review(env, p, rev["id"], {"verdict": "pass", "head": "", "findings": []}, checked_head=head)
    assert t["status"] == "done", t["result"]
    (f,) = _lg_followups(p)
    assert f["title"].startswith("Explain or split #") and "why this size" in f["spec"]


def test_landgate_defer_needs_a_pass_on_that_head_and_adds_one_followup(env):
    from ttp import landgate
    p = make(env)
    tid = p.db.add_task("code", "s", kind="code", tier="light", origin="coordinator")
    why = "it adds 400 lines, over review.ponytail_land_lines (150)"
    assert landgate.defer(p, tid, "abc123def4567", why) is None
    _lg_checks(p, tid, "abc123def4567", passed=False)
    assert landgate.defer(p, tid, "abc123def4567", why) is None
    _lg_checks(p, tid, "abc123def4567")
    said = []
    fid = landgate.defer(p, tid, "abc123def4567", why, land=False, say=said.append)
    assert fid and landgate.defer(p, tid, "abc123def4567", why) == fid
    f = p.db.task(fid)
    assert f["title"] == f"Explain or split #{tid}: it went on over the size rule"
    assert "start_when" not in f["labels"] and "not refused" in said[0]
    assert p.db.one("SELECT 1 FROM events WHERE kind=? AND task=?", (landgate.KIND, tid))
