# Area case file for tests/test_tt_project_runtime.py: ponytail (the review's ponytail lane and the size gate).
from __future__ import annotations


def _lane_review(env, p, rid, verdict=None, **handoff):
    """Review `rid` ran and handed off `handoff` (done by default), its lane's verdict `verdict` in its run dir."""
    from ttp import ponytail
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p.db.update_task(rid, status="running")
    run_dir = env["tmp"] / f"run-{rid}"
    run_dir.mkdir()
    if verdict is not None:
        (run_dir / ponytail.FILE).write_text(json.dumps(verdict))
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "correct", **handoff}))
    d = Daemon(p.base)
    d._finish_worker({"task": rid}, Usage(cost_usd=1.0), "ok", run_dir)
    d.reconcile_tasks()
    return p.db.task(rid)


def _lean_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_out(repo, "init", "-q")
    (repo / "a.py").write_text("x\n")
    _git_out(repo, "add", ".")
    _git_out(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base = _git_out(repo, "rev-parse", "HEAD").strip()
    (repo / "b.py").write_text("".join(f"{i}\n" for i in range(30)))
    (repo / "docs").mkdir()
    (repo / "docs" / "n.md").write_text("".join(f"{i}\n" for i in range(50)))
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 4)
    _git_out(repo, "add", ".")
    _git_out(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "change")
    return repo, base, _git_out(repo, "rev-parse", "HEAD").strip()


def test_a_code_review_runs_a_ponytail_lane_that_must_pass_beside_correctness(env, monkeypatch):
    """A daemon-queued code review carries the lane (label with the change's added lines, an Opus
    tt-ponytail-review step in its spec). Its `done` stands only with a passed verdict on file: none is a
    form failure with nothing to fix; a fail becomes the fix task's findings, and the re-review keeps the lane."""
    from ttp import ponytail
    from ttp.daemon import CHANGES_NEEDED
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    _, _, _, (rev,) = _finish_code(env, p, "feature", {"app.py": 40})
    assert ponytail.labelled(rev) == "40", rev["labels"]
    assert "tt-ponytail-review" in rev["spec"] and "model `opus`" in rev["spec"] and "vendored:" in rev["spec"]
    assert "Size:" not in rev["spec"].split("Ponytail lane")[1].split("\n")[0]
    t = _lane_review(env, p, rev["id"])
    assert t["status"] == "failed" and "recorded no verdict" in t["result"]
    assert not p.db.q("SELECT * FROM tasks WHERE kind='code' AND origin='daemon'")

    _, _, _, (rev,) = _finish_code(env, p, "bloat", {"b.py": 30})
    t = _lane_review(env, p, rev["id"], {"verdict": "fail", "head": "", "findings": ["helper.py should not exist"]})
    assert t["status"] == "failed" and json.loads(t["result"])["status"] == CHANGES_NEEDED
    (fix,) = p.db.q("SELECT * FROM tasks WHERE kind='code' AND origin='daemon'")
    assert "helper.py should not exist" in fix["spec"]
    (again,) = p.db.q("SELECT * FROM tasks WHERE kind='review' AND id>?", (rev["id"],))
    assert ponytail.labelled(again) is not None

    _, branch, _, (rev,) = _finish_code(env, p, "lean", {"c.py": 5})
    head = _git_out(p.root, "rev-parse", branch).strip()
    t = _lane_review(env, p, rev["id"], {"verdict": "pass", "head": head, "findings": [], "thread": True})
    assert t["status"] == "done", t["result"]


def test_a_change_over_the_landing_size_needs_a_reason_and_the_lanes_pass(env, monkeypatch):
    """Over review.ponytail_land_lines, the review spec names the size and a passed lane is not enough:
    the spec or hand-off needs a `why this size:` line. With the lane off, no label and no step."""
    from ttp import ponytail
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    p.set_config("review.ponytail_land_lines", 30)
    passed = {"verdict": "pass", "head": "", "findings": []}
    _, _, _, (rev,) = _finish_code(env, p, "big", {"big.py": 45})
    assert "it adds 45 lines, over review.ponytail_land_lines (30)" in rev["spec"]
    t = _lane_review(env, p, rev["id"], passed)
    assert t["status"] == "failed" and "why this size" in t["result"]
    _, _, _, (rev,) = _finish_code(env, p, "big2", {"big2.py": 45})
    t = _lane_review(env, p, rev["id"], passed, summary="why this size: one generated table\nlooks right")
    assert t["status"] == "done", t["result"]
    p.set_config("review.ponytail_lane", False)
    _, _, _, (rev,) = _finish_code(env, p, "off", {"off.py": 45})
    assert ponytail.labelled(rev) is None and "tt-ponytail-review" not in rev["spec"]
    assert _lane_review(env, p, rev["id"])["status"] == "done"


def test_ponytail_size_and_verdict_rules(env, tmp_path, monkeypatch):
    """Added lines leave out binaries and excluded globs; the size gate needs both the reason and a pass
    on the same head; a labelled review's hand-off needs a verdict on file, a valid fail included."""
    from ttp import ponytail
    from ttp.hook import handoff_problem
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    repo, base, head = _lean_repo(tmp_path)
    assert ponytail.added_lines(repo, base, head) == 80
    assert ponytail.added_lines(repo, base, head, ["docs/**"]) == 30
    assert ponytail.added_lines(repo, "no-such-ref", head) is None
    cfg = {"review": {"ponytail_land_lines": 20, "ponytail_pr_lines": 10}}
    run = tmp_path / "run"
    run.mkdir()
    assert ponytail.size_problem(cfg, 20, "", run) == ""
    assert "needs a `why this size" in ponytail.size_problem(cfg, 30, "", run)
    assert "ponytail_pr_lines (10)" in ponytail.size_problem(cfg, 15, "", run, pr=True)
    assert "recorded no verdict" in ponytail.size_problem(cfg, 30, "- why this size: a table", run, head)
    (run / ponytail.FILE).write_text(json.dumps({"verdict": "pass", "head": base}))
    assert "passed " + base[:12] in ponytail.size_problem(cfg, 30, "why this size: x", run, head)
    (run / ponytail.FILE).write_text(json.dumps({"verdict": "pass", "head": head[:10]}))
    assert ponytail.size_problem(cfg, 30, "why this size: x", run, head) == ""
    assert ponytail.size_problem({"review": {"ponytail_land_lines": 0}}, 10**6, "", run) == ""
    (run / "result.json").write_text(json.dumps({"status": "done", "summary": "ok"}))
    assert not handoff_problem(run / "result.json", lane=True)
    (run / ponytail.FILE).write_text(json.dumps({"verdict": "fail", "findings": ["x should not exist"]}))
    assert not handoff_problem(run / "result.json", lane=True)
    (run / ponytail.FILE).unlink()
    assert "ponytail" in handoff_problem(run / "result.json", lane=True)
    assert not handoff_problem(run / "result.json", lane=False)
    (run / ponytail.FILE).write_text("{not json")
    assert "ponytail" in handoff_problem(run / "result.json", lane=True)


def test_a_draft_pr_over_the_pr_size_is_refused_without_a_reason(env, tmp_path, monkeypatch):
    """The gh wrapper refuses a run's draft PR adding more than review.ponytail_pr_lines over its base
    unless the body or task spec gives `why this size:` and the lane passed this head."""
    from ttp import ponytail
    from ttp.prguard import _size_problem
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    repo, base, head = _lean_repo(tmp_path)
    _git_out(repo, "update-ref", "refs/remotes/origin/main", base)
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.delenv("TTP_TASK", raising=False)
    cfg = {"review": {"ponytail_pr_lines": 50}}
    args = ["--draft", "--base", "main", "--title", "t"]
    assert "draft PR too big: it adds 80 lines" in _size_problem(args, None, cfg, cwd=str(repo))
    (run / ponytail.FILE).write_text(json.dumps({"verdict": "pass", "head": head}))
    assert _size_problem(args + ["--body", "x\nwhy this size: a fixture"], None, cfg, cwd=str(repo)) is None
    assert _size_problem(args, None, {"review": {"ponytail_pr_lines": 100}}, cwd=str(repo)) is None
    monkeypatch.delenv("TTP_RUN_DIR")
    assert _size_problem(args, None, cfg, cwd=str(repo)) is None


def test_a_lane_reviews_own_push_lands_only_on_its_pass_and_within_the_size(env, tmp_path, monkeypatch):
    """`ttp push` from a lane review needs the lane's pass on HEAD; over review.ponytail_land_lines (excluded
    paths left out) it also needs a `why this size:` line."""
    from ttp import ponytail, push
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    repo, base, head = _lean_repo(tmp_path)
    rid = p.db.add_task("review it", "s", kind="review", origin="daemon", labels=[ponytail.label(80)])
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    assert "recorded no verdict" in push.ponytail_landing(p, str(rid), base, {}, repo)
    (run / ponytail.FILE).write_text(json.dumps({"verdict": "pass", "head": head}))
    assert push.ponytail_landing(p, str(rid), base, {}, repo) == ""
    p.set_config("review.ponytail_land_lines", 50)
    assert "why this size" in push.ponytail_landing(p, str(rid), base, {}, repo)
    assert push.ponytail_landing(p, str(rid), base, {"push_exclude_paths": ["docs/**"]}, repo) == ""
