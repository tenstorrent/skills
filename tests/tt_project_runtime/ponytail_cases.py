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
    _, _, _, (rev,) = _finish_code(env, p, "big", {"big.py": 45},
                                   result={"status": "done", "summary": "why this size: one table"})
    assert "it adds 45 lines, over review.ponytail_land_lines (30)" in rev["spec"]
    t = _lane_review(env, p, rev["id"], passed)
    assert t["status"] == "done", t["result"]
    p.set_config("review.ponytail_land_lines", 60)   # under the limit when queued, over it once handed off
    _, _, _, (rev,) = _finish_code(env, p, "big2", {"big2.py": 45})
    _, _, _, (rev3,) = _finish_code(env, p, "big3", {"big3.py": 45})
    p.set_config("review.ponytail_land_lines", 30)
    t = _lane_review(env, p, rev["id"], passed)
    assert t["status"] == "failed" and "why this size" in t["result"]
    t = _lane_review(env, p, rev3["id"], passed, summary="why this size: one generated table\nlooks right")
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
    why = ponytail.size_problem(cfg, 30, "", run)
    assert "on its own line, starting with `why this size:` and followed by the reason" in why, why
    assert ponytail.size_problem(cfg, 30, "Noted, why this size: one parser.", run) == why   # mid-line: not read
    assert "ponytail_pr_lines (10)" in ponytail.size_problem(cfg, 15, "", run, pr=True)
    assert "recorded no verdict" in ponytail.size_problem(cfg, 30, "- why this size: a table", run, head)
    (run / ponytail.FILE).write_text(json.dumps({"verdict": "pass", "head": base}))
    assert "passed " + base[:12] in ponytail.size_problem(cfg, 30, "why this size: x", run, head)
    (run / ponytail.FILE).write_text(json.dumps({"verdict": "pass", "head": head[:10]}))
    assert ponytail.size_problem(cfg, 30, "why this size: x", run, head) == ""
    # a line-start reason holds whatever the rest says: one whose reason is quoted or backticked, and
    # one in text with an unbalanced quote elsewhere
    assert ponytail.size_problem(cfg, 30, 'why this size: "`schema.json` is one generated table"', run, head) == ""
    assert ponytail.size_problem(cfg, 30, "it's big, see 'the plan\nwhy this size: one parser", run, head) == ""
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


def test_a_why_this_size_only_mid_line_is_refused_with_the_hint_to_put_it_on_its_own_line(tmp_path):
    from ttp.hook import handoff_problem
    f = tmp_path / "result.json"
    f.write_text(json.dumps({"status": "done", "summary": "Added the table. why this size: it needs both columns"}))
    assert "put it on its own line" in handoff_problem(f)
    for ok in ("Added the table.\nwhy this size: it needs both columns", "- why this size: x", "no size reason at all"):
        f.write_text(json.dumps({"status": "done", "summary": ok}))
        assert not handoff_problem(f), ok


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


def test_a_detached_push_from_a_lane_review_carries_the_lanes_verdict_to_its_push_process(env, tmp_path, monkeypatch):
    """`ttp push --detach` records the lane's verdict from the review's run dir in its marker, and the push
    process, whose TTP_RUN_DIR is its check output, puts it there for ponytail_landing; none recorded still refuses."""
    import io
    from ttp import checkdirs, ponytail, push
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    repo, base, head = _lean_repo(tmp_path)
    rid = p.db.add_task("review it", "s", kind="review", origin="daemon", labels=[ponytail.label(80)])
    p.set_config("delivery.push_checks", ["true"])
    monkeypatch.setattr(push, "resolve", lambda p, top, own: ("origin", "main", own))
    monkeypatch.setattr(push, "_boxed", lambda: True)
    monkeypatch.setattr(checkdirs, "release", lambda output: None)
    monkeypatch.setattr(push.signal, "signal", lambda *a: None)
    monkeypatch.setenv("TTP_TASK", str(rid))
    seen = []
    monkeypatch.setattr(push, "_run_retrying", lambda *a: (
        seen.append(push.ponytail_landing(p, str(rid), base, {}, repo)), (1, "stop here"))[1])

    def child(verdict):
        run = tmp_path / f"run-{len(seen)}"
        run.mkdir()
        if verdict:
            (run / ponytail.FILE).write_text(json.dumps(verdict))
        monkeypatch.setenv("TTP_RUN_DIR", str(run))
        before = set((p.state / push.DETACHED).glob("*.json"))
        assert push.detach(p, repo) == 0
        marker, = set((p.state / push.DETACHED).glob("*.json")) - before
        out = checkdirs.reports(Path(json.loads(marker.read_text())["check_run_dir"]))
        monkeypatch.setenv("TTP_RUN_DIR", str(out))
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert push.run_detached(p, repo, marker) == 1
        return seen[-1]

    assert child({"verdict": "pass", "head": head}) == ""
    assert "recorded no verdict" in child(None)
    assert "not " + head[:12] in child({"verdict": "pass", "head": base})


def _pony_pq_hand_off(env, s, verdict=None, **more):
    """As _pq_hand_off, the review's lane having written `verdict` in its run dir."""
    from ttp import ponytail
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    s.p.db.update_task(s.review, status="running")
    run_dir = env["tmp"] / f"run-{s.review}-{time.monotonic_ns()}"
    run_dir.mkdir()
    if verdict is not None:
        (run_dir / ponytail.FILE).write_text(json.dumps(verdict))
    (run_dir / "result.json").write_text(json.dumps(
        {"status": "done", "summary": "looks good", "push": [{"branch": s.branch, "head": s.head}], **more}))
    Daemon(s.p.base)._finish_worker({"task": s.review, "id": 7, "note": "{}"}, Usage(cost_usd=1.0), "ok", run_dir)
    return s.p.db.task(s.review)


def test_an_unlabelled_review_of_an_oversized_code_change_enters_the_push_queue_only_with_its_reason_and_lane(env, monkeypatch):
    """The push queue measures each approved head of a code change itself, so a review queued with no
    ponytail label (a coordinator's, an external branch's) cannot skip the size gate or the lane."""
    from ttp import ponytail
    monkeypatch.delenv("TTP_TEST_PONYTAIL", raising=False)
    s = _pq(env, monkeypatch)
    s.p.set_config("review.ponytail_land_lines", 5)
    _commit(s.path, "big.txt", "".join(f"{i}\n" for i in range(20)))
    s.head = _git_out(s.path, "rev-parse", "HEAD").strip()
    assert ponytail.labelled(s.p.db.task(s.review)) is None
    t = _pony_pq_hand_off(env, s, {"verdict": "pass", "head": s.head})
    assert t["status"] == "queued" and not s.p.db.q("SELECT * FROM push_queue")
    assert json.loads(t["result"])["push_invalid"] == 1
    assert ponytail.labelled(t) is not None                 # its rerun runs the lane
    t = _pony_pq_hand_off(env, s, {"verdict": "pass", "head": "0" * 40},
                          summary="looks good\nwhy this size: one generated table")   # a pass on another head
    assert t["status"] != "pushing" and not s.p.db.q("SELECT * FROM push_queue")
    s.p.db.update_task(s.review, result=json.dumps({}))
    t = _pony_pq_hand_off(env, s, {"verdict": "pass", "head": s.head},
                          summary="looks good\nwhy this size: one generated table")
    assert t["status"] == "pushing", t["result"]


def _size_failed_review(env, p, monkeypatch, files, verdict):
    """A code change over review.ponytail_land_lines whose lane review failed on `verdict`, labelled with its
    head as the daemon does: (code task id, review id, head, check key)."""
    from ttp import reviewreuse
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    tid, branch, _, (rev,) = _finish_code(env, p, "big", files)   # the limit is set once it is queued
    p.set_config("review.ponytail_land_lines", 30)
    head, key = _git_out(p.root, "rev-parse", branch).strip(), reviewreuse.check_key(p.config())
    t = _lane_review(env, p, rev["id"], dict({"head": head, "findings": []}, **verdict))
    assert t["status"] == "failed"
    p.db.update_task(rev["id"], labels=reviewreuse.with_head(json.loads(t["labels"]), head, key))
    return tid, rev["id"], head, key


def test_a_size_only_failed_verdict_stands_until_a_why_this_size_line_is_given(env, monkeypatch):
    """The size rule alone failed the review, so its fix task (decided by Daemon._code_change) rightly makes no
    commit and adds `why this size:`. The head is unchanged, but the re-review must run (the lane included)."""
    from ttp import reviewreuse
    p = make(env)
    tid, rev, head, key = _size_failed_review(env, p, monkeypatch, {"big.py": 45}, {"verdict": "pass"})
    assert json.loads(p.db.task(rev)["result"])[reviewreuse.SIZE_ONLY_KEY] is True
    assert reviewreuse.judged(p.db, head, key)["id"] == rev, "no reason given yet: the verdict stands"
    (fix,) = p.db.q("SELECT * FROM tasks WHERE kind='code' AND origin='daemon'")
    p.db.update_task(fix["id"], status="done", result=json.dumps(
        {"status": "done", "summary": "no change needed\nwhy this size: one generated table"}))
    assert reviewreuse.judged(p.db, head, key) is None, "the reason is there: the head gets a fresh review"
    assert reviewreuse.judged(p.db, head, key, skip=0) is None


def test_a_size_only_failed_verdict_does_not_stand_once_the_code_task_hands_off_a_no_split_reason(env, monkeypatch):
    from ttp import reviewreuse, splitgate
    p = make(env)
    tid, rev, head, key = _size_failed_review(env, p, monkeypatch, {"big.py": 45}, {"verdict": "pass"})
    assert reviewreuse.judged(p.db, head, key)["id"] == rev, "no reason given yet: the verdict stands"
    p.db.update_task(tid, result=json.dumps({"status": "done", "summary": "done", splitgate.FIELD: "one generated table"}))
    assert reviewreuse.judged(p.db, head, key) is None, "the lane accepts a no_split reason: the head gets a fresh review"


def test_the_size_gate_reads_a_reason_from_a_reviewed_code_task_hand_off_summary(env, tmp_path, monkeypatch):
    """A size reason may sit in a reviewed code task's hand-off summary, not only its spec: the review's
    own spec and hand-off carry none, the lane passed, and only the code task's summary has the WHY
    line, at a line start (a mid-line mention is none)."""
    from ttp import ponytail
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    cfg = {"review": {"ponytail_land_lines": 30}}
    run = tmp_path / "lane-run"
    run.mkdir()
    (run / ponytail.FILE).write_text(json.dumps({"verdict": "pass", "head": "a" * 40, "findings": []}))
    task = {"labels": json.dumps([ponytail.label(45)])}   # a labelled review: 45 added lines, over the cap

    def code_task(summary):
        return {"spec": "make the change", "result": json.dumps({"status": "done", "summary": summary})}

    assert ponytail.gate(cfg, task, run, {"summary": "correct"},
                         [code_task("all landed.\nwhy this size: one schema change")]) == ("", [])
    why = ponytail.gate(cfg, task, run, {"summary": "correct"},
                        [code_task("all landed; why this size: only mid-line")])
    assert why[0] and "why this size" in why[0], "a mid-line mention is no size reason"


def test_a_failed_verdict_with_another_finding_is_still_reused_on_an_unchanged_head(env, monkeypatch):
    from ttp import reviewreuse
    p = make(env)
    tid, rev, head, key = _size_failed_review(
        env, p, monkeypatch, {"big.py": 45}, {"verdict": "fail", "findings": ["helper.py should not exist"]})
    assert reviewreuse.SIZE_ONLY_KEY not in json.loads(p.db.task(rev)["result"])
    p.db.update_task(tid, spec="why this size: one generated table")
    assert reviewreuse.judged(p.db, head, key)["id"] == rev, "a lane finding is not answered by a size reason"


def test_the_lane_fails_on_a_block_finding_or_one_with_no_severity_and_passes_with_notes_only(env, monkeypatch):
    from ttp import ponytail
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    block = {"severity": "block", "text": "helper.py should not exist"}
    note = {"severity": "note", "text": "one unused import"}
    run = env["tmp"] / "lane-run"
    run.mkdir()
    for findings, ok in (([block], False), ([note, block], False), (["bare string"], False),
                         ([{"text": "no severity"}], False), ([note], True), ([], True),
                         ([{"level": "note", "text": "t"}], True), ([{"level": "block", "text": "t"}], False),
                         ([{"level": "note", "text": "t"}, {"text": "neither key"}], False),
                         *(([{"severity": sev, "text": "t"}], False) for sev in ("blocker", "Critical", "high")),
                         *(([{"severity": sev, "text": "t"}], True) for sev in ("info", "minor", "low", "nit", "suggestion"))):
        (run / ponytail.FILE).write_text(json.dumps({"verdict": "pass", "head": "a" * 40, "findings": findings}))
        assert (ponytail.lane_problem(run, "a" * 40) == "") is ok, findings
    (run / ponytail.FILE).write_text(json.dumps({"verdict": "fail", "findings": []}))
    assert ponytail.FAILED in ponytail.lane_problem(run)

    _, branch, _, (rev,) = _finish_code(env, p, "lean", {"c.py": 5})
    head = _git_out(p.root, "rev-parse", branch).strip()
    t = _lane_review(env, p, rev["id"], {"verdict": "pass", "head": head, "findings": [note, {"severity": "note", "text": "x"}]})
    assert t["status"] == "done", t["result"]
    assert json.loads(t["result"])["ponytail_notes"] == ["one unused import", "x"]
    assert "Ponytail follow-ups (not blocking): one unused import; x" in json.loads(t["result"])["summary"]
    assert not p.db.q("SELECT * FROM tasks WHERE kind='code' AND origin='daemon'")

    _, branch, _, (rev,) = _finish_code(env, p, "lean2", {"d.py": 5})
    head = _git_out(p.root, "rev-parse", branch).strip()
    t = _lane_review(env, p, rev["id"], {"verdict": "pass", "head": head, "findings": [note, block]})
    assert t["status"] == "failed"
    (fix,) = p.db.q("SELECT * FROM tasks WHERE kind='code' AND origin='daemon'")
    assert "helper.py should not exist" in fix["spec"] and "unused import" not in fix["spec"]
    assert json.loads(t["result"])["ponytail_notes"] == ["one unused import"]


def test_a_passed_lane_with_only_info_findings_passes_the_review_and_queues_no_fix(env, monkeypatch):
    """A severity other than a blocking one (info, minor, low...) is a note: the review stays done and its
    findings are listed as not blocking, never queued as fix tasks."""
    from ttp import ponytail
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    info = [{"severity": "info", "text": f"info {i}"} for i in range(3)] + [{"severity": "minor", "text": "minor"}]
    _, branch, _, (rev,) = _finish_code(env, p, "lean", {"c.py": 5})
    head = _git_out(p.root, "rev-parse", branch).strip()
    t = _lane_review(env, p, rev["id"], {"verdict": "pass", "head": head, "findings": info})
    assert t["status"] == "done", t["result"]
    assert json.loads(t["result"])["ponytail_notes"] == ["info 0", "info 1", "info 2", "minor"]
    assert not p.db.q("SELECT * FROM tasks WHERE kind='code' AND origin='daemon'")
    assert ponytail.gate(p.config(), t, env["tmp"] / f"run-{rev['id']}", {}) == ("", [])


def test_the_lane_text_names_the_skill_file_to_read():
    """The lane step names the ponytail review skill's own file, resolved where this release keeps
    its skills, and says to read it there: a reviewer with no plugin cache finds the skill without
    searching the disk for it."""
    from ttp import ponytail
    line = ponytail.spec_line({}, None)
    m = re.search(r"read skill `tt-ponytail-review` at `([^`]+)` and apply it", line)
    assert m, line
    assert m.group(1).endswith("tt-ponytail-review/SKILL.md") and Path(m.group(1)).is_file(), line


def test_the_lane_pins_a_model_the_reviews_provider_can_start_and_falls_back_on_a_refused_one():
    """A Claude review keeps review.ponytail_model (opus); a Codex-only project's review names no Claude
    model its subagent tool would refuse (the Codex tiers use the account default), and a provider whose
    deep tier names a model gets that one. Every line says what to do when the tool refuses the named
    model, so the lane still runs and records the model it ran on."""
    import copy
    from ttp import ponytail
    from ttp.project import DEFAULT_CONFIG
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    assert ponytail.lane_model(cfg) == "opus" and "pinned to model `opus`" in ponytail.spec_line(cfg, None)
    codex = {**cfg, "core_provider": "codex", "providers_allowed": ["codex"]}
    for line in (ponytail.spec_line(codex, None), ponytail.spec_line(cfg, None, "codex"),
                 ponytail.spec_line(cfg, None, "cursor")):
        assert "opus" not in line and "on the strongest model your subagent tool offers" in line, line
    named = copy.deepcopy(codex)
    named["providers"]["codex"]["tiers"]["deep"]["model"] = "codex-deep"
    assert "pinned to model `codex-deep`" in ponytail.spec_line(named, None)
    named["review"]["ponytail_model"] = "codex-deep"
    named["providers"]["codex"]["tiers"]["deep"]["model"] = "codex-big"
    named["providers"]["codex"]["tiers"]["light"]["model"] = "codex-deep"
    assert ponytail.lane_model(named) == "codex-deep"
    named["providers"]["codex"]["tiers"]["deep"]["model"] = "default"
    named["review"]["ponytail_model"] = "opus"
    assert ponytail.lane_model(named) == ""
    named["providers"]["chat"] = {"tiers": {"deep": {"model": "endpoint-model"}}}
    assert ponytail.lane_model(named, "chat") == "opus"
    for line in (ponytail.spec_line(cfg, None), ponytail.spec_line(codex, None)):
        assert "if the tool refuses that model, start it on the strongest model the tool offers" in line, line
        assert '"model": "<the model it ran on>"' in line and "only with no subagent tool at all" in line, line


def test_a_code_workers_own_lane_names_the_model_of_the_provider_it_runs_on(env):
    """The code worker's own lane step names opus on Claude and no Claude model on Codex, with the
    refused-model fallback either way."""
    from ttp import prompts
    p = make(env)
    tid = p.db.add_task("fix", "a spec", kind="code", tier="standard", priority=3, budget_usd=8.0, labels=[])
    claude = " ".join(prompts.worker_task(p, p.db.task(tid), str(p.root), None).split())
    codex = " ".join(prompts.worker_task(p, p.db.task(tid), str(p.root), None, provider="codex").split())
    assert "subagent pinned to model `opus` (refused: the tool's strongest)" in claude
    assert "subagent on the strongest model your subagent tool offers" in codex and "`opus`" not in codex
    assert "{ponytail_pin}" not in claude + codex


def test_a_code_tasks_own_ponytail_lane_names_the_skill_file_and_a_repository_search(env):
    """A code worker's own lane step names the ponytail review skill's file, as a review's does, and
    has the subagent search the repository for an existing function that already decides each rule
    the diff adds: the lane missed such helpers when it named the skill alone."""
    from ttp import ponytail, prompts
    p = make(env)
    tid = p.db.add_task("fix", "a spec", kind="code", tier="standard", priority=3, budget_usd=8.0, labels=[])
    text = prompts.worker_task(p, p.db.task(tid), str(p.root), None)
    m = re.search(r"reads skill\s+`tt-ponytail-review` at `([^`]+)`", text)
    assert m and m.group(1) == ponytail.skill_file() and Path(m.group(1)).is_file(), text
    assert "{ponytail_skill}" not in text
    assert re.search(r"searching the repository \(`git grep`\) for\s+an existing function that already "
                     r"decides each rule", text), text


def test_an_oversized_change_without_a_reason_goes_back_to_its_worker_with_no_review(env, monkeypatch):
    """Over review.ponytail_land_lines with no `why this size:` line in the code task's spec or hand-off,
    no review is queued (it could only fail): the task is re-queued to its worker with the reason, once
    per head. With the line, the review is queued as before."""
    from ttp.daemon import Daemon
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    p.set_config("review.ponytail_land_lines", 30)
    tid, branch, _, reviews = _finish_code(env, p, "big", {"big.py": 45})
    assert reviews == [] and not p.db.q("SELECT id FROM tasks WHERE kind='review'")
    t = p.db.task(tid)
    assert t["status"] == "queued", t["status"]
    assert "review refused before it ran, head " in t["spec"] and "it adds 45 lines" in t["spec"]
    p.db.update_task(tid, status="done")   # the same head handed off again: not sent back twice
    assert Daemon(p.base)._auto_review(p.db.task(tid), "again") is None
    assert p.db.task(tid)["status"] == "done" and not p.db.q("SELECT id FROM tasks WHERE kind='review'")
    # Not sent back again: the coordinator is told once for that head, never silently left done.
    assert Daemon(p.base)._auto_review(p.db.task(tid), "again") is None
    (ev,) = p.db.q("SELECT text, status FROM events WHERE kind='review_unqueued' AND task=?", (tid,))
    assert ev["status"] == "queued" and "it adds 45 lines" in ev["text"]
    _, _, _, (rev,) = _finish_code(env, p, "big2", {"big2.py": 45},
                                   result={"status": "done", "summary": "done\nwhy this size: one table"})
    assert "it adds 45 lines" in rev["spec"]


def test_a_head_whose_patch_is_already_on_the_push_branch_gets_no_review(env, monkeypatch):
    """A head with no commit of its own off the push branch (each patch already there, under other
    commits) is refused before a review runs."""
    from ttp import ponytail
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    p.set_config("delivery.push_branch", "work")

    def land_first(tid, head):   # the same patch lands on the push branch as another commit
        _git_out(p.root, "branch", "-f", "work", f"{head}~1")
        wt = env["tmp"] / "land"
        _git_out(p.root, "worktree", "add", "-q", str(wt), "work")
        _git_out(wt, "-c", "user.name=o", "-c", "user.email=o@o", "cherry-pick", head)
        _git_out(p.root, "worktree", "remove", "--force", str(wt))

    tid, branch, _, reviews = _finish_code(env, p, "dup", {"dup.py": 5}, before=land_first)
    head = _git_out(p.root, "rev-parse", branch).strip()
    assert _git_out(p.root, "rev-parse", "work").strip() != head
    assert ponytail.own_size(p, p.db.task(tid), branch, head)[1] is False
    assert reviews == [] and not p.db.q("SELECT id FROM tasks WHERE kind='review'")
    _, _, _, (rev,) = _finish_code(env, p, "new", {"new.py": 5})   # a change of its own is reviewed


def test_a_change_after_a_history_rewrite_is_measured_by_its_own_commits(env, monkeypatch):
    """After the push branch was rewritten (its old tip retired), a task branched from the old history is
    measured by its own commits only, not by the whole old history its base no longer shares."""
    from ttp import landed, ponytail, worktree
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    p.set_config("delivery.push_branch", "work")
    p.set_config("review.ponytail_land_lines", 30)

    def rewrite(tid, head):   # the old history (60 lines) is rewritten on `work`
        old = f"{head}~1"
        _git_out(p.root, "update-ref", "-d", f"{worktree.FORK_POINT_REF}{tid}")   # an old task: none recorded
        tree = _git_out(p.root, "rev-parse", f"{old}^{{tree}}").strip()
        new = _git_out(p.root, "-c", "user.name=o", "-c", "user.email=o@o", "commit-tree", tree, "-p", f"{old}~1",
                       "-m", "squash")
        _git_out(p.root, "update-ref", "refs/heads/work", new.strip())
        landed.retire_tip(p, _git_out(p.root, "rev-parse", old).strip())

    def old_history(path):
        (path / "old.py").write_text("".join(f"old {i}\n" for i in range(60)))
        _git_out(path, "add", "old.py")
        _git_out(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "old history")

    tid, branch, _, (rev,) = _finish_code(env, p, "small", {"small.py": 5}, edit=old_history, before=rewrite)
    assert ponytail.labelled(rev) == "5", rev["labels"]
