# Express landing tests (ttp express, express.py) of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file
# and the other case files, and pytest collects these tests under its name.
from __future__ import annotations


def _express_marker(p, heads, bid="express-1"):
    """_batch_marker for an express landing (express.start's marker)."""
    from ttp import push
    from ttp.project import write_json
    marker = _batch_marker(p, heads, bid=bid)
    write_json(marker, {**push._read(marker), "express": {"why": "urgent: the user is waiting", "row": 1}})
    return marker


def test_an_express_landing_pushes_its_one_head_beside_a_held_push_lock_with_one_bump(env, monkeypatch, capsys):
    """(a) A batch holds the push lock: an express landing does not requeue behind it. It checks in its own
    worktree beside it, takes one version bump and changeset, and the target only fast-forwards."""
    from ttp import push
    log = env["tmp"] / "checks.log"
    p, repo, origin, other = _bump_setup(env, monkeypatch, [f"git rev-parse HEAD >> {log}"])
    before = _git_out(origin, "rev-parse", "proj")
    head = _entry(repo, "e1", {"plugins/p/f1.txt": "1\n"})
    held = push.take(p, "origin", "proj", 0, who="push batch batch-1")
    try:
        rc, m = _run_batch(p, _express_marker(p, [head]), monkeypatch)
    finally:
        held.close()
    assert rc == 0 and m["outcome"] == "pushed" and _statuses(m) == ["pushed"], m
    assert "the express landing checks beside it" in capsys.readouterr().out
    assert _versions(origin) == ("0.1.1", "0.1.1") and len(_bump_commits(origin)) == 1
    assert _git_out(origin, "merge-base", "--is-ancestor", before, "proj") == ""
    assert _git_out(origin, "rev-list", "--count", f"{before}..proj") == "2", "the entry and its bump"
    assert log.read_text().split() == [m["pushed_sha"]] and (p.worktrees / "push-express").exists()
    assert not (p.worktrees / "push").exists(), "a regular batch's worktree is never touched"


def test_an_express_landing_without_the_lock_requeues_nothing_when_a_regular_batch_would(env, monkeypatch):
    """The same held lock sends a regular batch back to the queue (busy): only express goes beside it."""
    from ttp import push
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["true"])
    head = _entry(repo, "e1", {"plugins/p/f1.txt": "1\n"})
    held = push.take(p, "origin", "proj", 0, who="push batch batch-1")
    try:
        _, regular = _run_batch(p, _batch_marker(p, [head], bid="b1"), monkeypatch)
    finally:
        held.close()
    assert regular["outcome"] == "busy" and _statuses(regular) == ["requeued"], regular


_EXPRESS_TESTS = '''import pathlib
LOG = pathlib.Path({log!r})


def test_one():
    LOG.open("a").write("one\\n")


def test_two():
    LOG.open("a").write("two\\n")
'''


def test_an_express_landing_runs_only_the_tests_its_diff_reaches(env, monkeypatch, capsys):
    """(b) An express landing's pytest push check is narrowed to the tests the entry changes (read from the
    entry's own tree); the rest is left to the full checks after it lands."""
    log = env["tmp"] / "ran.log"
    check = "python3 -m pytest -q tests/test_ex.py tests/test_other.py"
    p, repo, origin, other = _bump_setup(env, monkeypatch, [check])
    p.set_config("delivery.push_queue", True)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests/test_ex.py").write_text(_EXPRESS_TESTS.format(log=str(log)))
    (repo / "tests/test_other.py").write_text(
        f"import pathlib\n\n\ndef test_three():\n    pathlib.Path({str(log)!r}).open('a').write('three\\n')\n")
    _git_out(repo, "add", "tests")
    _git_out(repo, "commit", "-qm", "tests")
    _git_out(repo, "push", "-q", "origin", "HEAD:proj")
    head = _entry(repo, "e1", {"tests/test_ex.py": _EXPRESS_TESTS.format(log=str(log)).replace(
        'write("two\\n")', 'write("two\\n")\n    assert True')})
    rc, m = _run_batch(p, _express_marker(p, [head]), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed", m
    assert "express: " + check + ": targeted: " in capsys.readouterr().out
    assert log.read_text().split() == ["two"], "only the changed test ran"


def test_an_express_landing_goes_past_a_failing_test_that_also_fails_on_the_tip(env, monkeypatch):
    """(c) A failing test the unchanged tip shares is pre-existing: it does not block the express entry."""
    p, repo, origin, other = _push_setup(env, monkeypatch, [_FLAKY_CHECK])
    tip = _git_out(origin, "rev-parse", "proj")
    calls = _rerun_stub(monkeypatch, tip, lambda where, sel: 1)
    rc, m = _run_batch(p, _express_marker(p, [_entry(repo, "e1", {"f1.txt": "1\n"})]), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed" and _statuses(m) == ["pushed"], m
    assert ("tip", _SEL) in calls and m["pre_existing"][0]["ids"] == [_FLAKY_ID]
    assert _git_out(origin, "rev-parse", "proj") == m["pushed_sha"] != tip


def _express_row(s, review=None):
    """The queue id of the review's approval of s's head."""
    from ttp import pushq
    review = review or s.review
    s.p.db.update_task(review, status="pushing")
    with s.p.db.tx():
        [rid] = pushq.approve(s.p, review, None, {"entries": [{"branch": s.branch, "head": s.head}],
                                                  "target": "origin/proj"})
    return rid


def test_express_refuses_an_unreviewed_routine_or_oversized_head_with_its_reasons(env, monkeypatch):
    """(d) Eligibility is model-free and every failing reason is named: no passing review, not urgent,
    over the size limits, or express turned off."""
    from ttp import express
    s = _pq(env, monkeypatch)
    said: list[str] = []
    assert express.main(s.p, str(s.code), "urgent", say=said.append) == 2
    assert "no approved head for" in said[-1] and "independent review must pass" in said[-1]
    row = _express_row(s)
    assert express.main(s.p, str(s.code), "", say=said.append) == 2 and "--why is required" in said[-1]
    assert express.main(s.p, str(s.code), "fix", dry_run=True, say=said.append) == 2
    assert f"task #{s.review} is not urgent" in said[-1]
    s.p.db.update_task(s.code, priority=1)
    for ref in (str(s.code), str(s.review), f"#{s.code}", s.head[:8]):
        got, why = express.eligible(s.p, ref)
        assert got["id"] == row and why == [], (ref, why)
    s.p.set_config("delivery.express_max_lines", 0)
    assert express.main(s.p, str(s.code), "fix", dry_run=True, say=said.append) == 2
    assert "1 changed lines in 1 files is over delivery.express_max_lines 0" in said[-1]
    s.p.set_config("delivery.express_max_lines", 200)
    s.p.set_config("delivery.express_max_files", 0)
    assert "over delivery.express_max_lines 200 or delivery.express_max_files 0" in \
        "; ".join(express.eligible(s.p, str(s.code))[1])
    s.p.set_config("delivery.express_max_files", 8)
    s.p.set_config("delivery.express", False)
    assert express.eligible(s.p, str(s.code))[1] == ["delivery.express is off"]
    s.p.set_config("delivery.express", True)
    assert express.main(s.p, str(s.code), "fix", dry_run=True, say=said.append) == 0 and "may land express" in said[-1]


def test_express_starts_a_one_entry_batch_ahead_of_the_queued_approvals(env, monkeypatch):
    """(a) Another approval waits in the queue: express starts a batch of its own head alone, marked
    express, and leaves the other row for the next regular batch."""
    from ttp import express, push
    s = _pq(env, monkeypatch)
    other = _pq_review(s)[1]
    s.p.db.update_task(s.code, priority=1)
    row = _express_row(s)
    said: list[str] = []
    assert express.main(s.p, str(s.review), "urgent: the user is waiting", wait_s=30, say=said.append) == 0, said
    bid = said[0].split()[2]
    assert express.is_express(bid) and "alone on origin/proj" in said[0] and ": pushed" in said[-1], said
    m = push._read(s.p.state / push.DETACHED / f"{bid}.json")
    assert [e["id"] for e in m["entries"]] == [row] and m["express"]["why"] == "urgent: the user is waiting"
    assert s.p.db.one("SELECT status FROM push_queue WHERE id=?", (other,))["status"] == "approved"


def test_an_express_landing_gets_the_full_checks_after_it_lands_and_a_failure_reverts_only_it(
        env, tmp_path, monkeypatch):
    """(e) With no post-landing checks of its own, a project still runs the full push checks on an express
    landing once it lands (a regular batch is left alone); a failure there reverts that commit forward."""
    from ttp import postland
    p, g, pushed, entries = _pl_land(env, tmp_path, monkeypatch,
                                     f"sh -c \"grep -qs bad pl*.txt && {{ {_PL_FAIL}; }}; {_PL_PASS}\"", n=1, bad_at=0)
    check = p.config()["delivery"]["post_land_checks"][0]
    p.set_config("delivery.post_land_checks", [])
    assert postland.tend(p, p.config()) == "", "a regular batch: no post-landing checks"
    p.set_config("delivery.push_checks", [check])
    p.db.x("UPDATE push_batches SET id='express-1' WHERE id='b1'")
    p.db.x("UPDATE push_queue SET batch='express-1' WHERE batch='b1'")
    assert postland.tend(p, p.config()).startswith(f"post-landing checks started on {pushed[:10]}")
    job = p.db.kv(postland.KEY)["job"]
    assert job["batch"] == "express-1" and [c if isinstance(c, str) else c.get("run") for c in job["checks"]] \
        == [check], job
    assert postland.tend(p, p.config()) == "guilty"
    [rev] = p.db.q("SELECT * FROM tasks WHERE title LIKE 'Revert #%'")
    [row] = p.db.q("SELECT * FROM push_queue WHERE task=?", (rev["id"],))
    assert g("rev-parse", f"{row['head']}^") == pushed and g("diff", "--name-only", pushed, row["head"]) == "pl0.txt"
    assert postland.tend(p, p.config()) == "", "checked once"
