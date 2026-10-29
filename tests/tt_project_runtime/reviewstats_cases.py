# Review stats and re-review fix-delta tests of tests/test_tt_project_runtime.py, which runs this file
# in its own namespace (see _include_cases there). Add new review-routing tests here, not there.
from __future__ import annotations


def test_review_stats_areas_split_runtime_prompts_and_tests():
    from ttp import reviewstats
    assert reviewstats.area("plugins/x/runtime/ttp/daemon.py") == "runtime"
    assert reviewstats.area("plugins/x/skills/tt-x/SKILL.md") == "skills-prompts-config"
    assert reviewstats.area("plugins/x/template/prompts/worker.md") == "skills-prompts-config"
    assert reviewstats.area("plugins/x/.claude-plugin/plugin.json") == "skills-prompts-config"
    assert reviewstats.area("tests/test_x.py") == "tests-docs"
    assert reviewstats.area("tests/tt_project_runtime/push_cases.py") == "tests-docs"
    assert reviewstats.area("README.md") == "tests-docs"
    assert reviewstats.area(".changeset/a.md") == "tests-docs"
    assert reviewstats.areas({"a.py": 3, "README.md": 1, "tests/t.py": 2}) == ["runtime", "tests-docs"]
    assert reviewstats.areas(None) == []
    assert [reviewstats.size_bucket(n) for n in (None, 49, 50, 200, 201)] == ["unknown", "<50", "50-200",
                                                                             "50-200", ">200"]


def test_review_stats_bucket_reviews_by_non_test_lines_too(env):
    from ttp import audit, reviewstats
    assert reviewstats.code_lines({"a.py": 3, "tests/t.py": 300, "x/test_b.py": 9, "README.md": 50,
                                   "docs/g.txt": 4, ".changeset/c.md": 2, "s/SKILL.md": 5, "bin.so": None}) == 8
    assert reviewstats.code_lines({}) == 0 and reviewstats.code_lines(None) is None
    p = make(env)
    # Mostly tests and docs: over 200 lines in all, under 50 outside them.
    _, _, _, (rev,) = _finish_code(env, p, "tested", {"app.py": 40, "tests/test_app.py": 250, "docs/app.md": 20})
    _fail_review(env, p, rev["id"], [{"title": "split it", "spec": "x"}])
    row = p.db.one("SELECT * FROM review_stats WHERE task=?", (rev["id"],))
    assert (row["lines"], row["code_lines"]) == (310, 40), row
    p.db.x("UPDATE review_stats SET code_lines=NULL WHERE task=?", (rev["id"],))   # a row from before the field
    _, _, _, (rev2,) = _finish_code(env, p, "big", {"big.py": 230})
    _fail_review(env, p, rev2["id"], status="done", summary="fine")
    s = reviewstats.summary(p.db, 0)
    assert {k: (g["n"], g["failed"]) for k, g in s["by_size"].items()} == {">200": (2, 1)}, s
    assert {k: (g["n"], g["failed"]) for k, g in s["by_code_size"].items()} == {">200": (1, 0), "unknown": (1, 1)}, s
    line = next(x for x in audit.lines(p.db, p=p) if x.startswith("reviews finished: 2"))
    assert "by diff lines: >200 2 (1 failed" in line, line
    assert "by non-test lines: >200 1 (0 failed" in line and "unknown 1 (1 failed" in line, line


def test_review_stats_table_is_added_to_an_existing_database(env):
    import sqlite3
    from ttp.db import DB
    path = env["tmp"] / "old.db"
    old = DB(path)
    tid = old.add_task("kept", "s", kind="review")
    old.conn.execute("DROP TABLE review_stats")   # a database from before the table
    old.conn.close()
    with sqlite3.connect(path) as c:
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='review_stats'").fetchall()
    db = DB(path)
    assert db.task(tid)["title"] == "kept"
    assert db.q("SELECT * FROM review_stats") == []
    db.conn.close()
    assert DB(path).q("SELECT * FROM review_stats") == []   # opening again is a no-op


def test_a_re_review_leads_with_the_fix_delta_and_each_review_is_recorded(env):
    from ttp import audit, reviewstats, worktree
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p = make(env)
    _, branch, _, (rev,) = _finish_code(env, p, "feature", {"app.py": 40, "docs/notes.md": 2})
    failed = _git_out(p.root, "rev-parse", branch).strip()
    p.db.x("UPDATE tasks SET spent_usd=1.5 WHERE id=?", (rev["id"],))   # booked before the hand-off is read
    _fail_review(env, p, rev["id"], [{"title": "add the missing test", "spec": "test_x"}])
    row = p.db.one("SELECT * FROM review_stats WHERE task=?", (rev["id"],))
    assert row["head"] == failed and row["lines"] == 42 and row["areas"] == "runtime,tests-docs", row
    assert (row["rereview"], row["outcome"], row["cost_usd"], row["tier"]) == (0, "failed", 1.5, rev["tier"])
    assert reviewstats.failed_head(p.db, rev["id"]) == failed
    (fix,) = p.db.q("SELECT * FROM tasks WHERE kind='code' AND origin='daemon'")
    (re_rev,) = p.db.q("SELECT * FROM tasks WHERE kind='review' AND id!=?", (rev["id"],))
    fbranch = fix["branch"]
    # Queued with the fix: it names the failed head and where the new head will be, and leads with the delta.
    assert f"Fix delta: failed head {failed[:12]}, new head the tip of {fbranch} once #{fix['id']}" in re_rev["spec"]
    assert f"`git diff {failed[:12]}..{fbranch}`: check each finding above against it first" in re_rev["spec"]
    assert "full diff against the merge-base only where the delta touches it" in re_rev["spec"]
    path, _ = worktree.ensure(p, p.db.task(fix["id"]))
    (path / "test_x.py").write_text("def test_x(): pass\n")
    _git_out(path, "add", ".")
    _git_out(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fix")
    new = _git_out(path, "rev-parse", "HEAD").strip()
    p.db.update_task(fix["id"], status="running")
    run_dir = env["tmp"] / f"run-{fix['id']}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "fixed"}))
    Daemon(p.base)._finish_worker({"task": fix["id"]}, Usage(cost_usd=1.0), "ok", run_dir)
    # Once the fix is done: the new head and the delta's size, once per head.
    spec = p.db.task(re_rev["id"])["spec"]
    line = f"New head: {fbranch} at {new[:12]}. The fix delta {failed[:12]}..{new[:12]} changes 1 lines"
    assert spec.count(line) == 1, spec
    Daemon(p.base)._name_new_head(p.db.task(fix["id"]), p.db.task(re_rev["id"]))
    assert p.db.task(re_rev["id"])["spec"] == spec
    _fail_review(env, p, re_rev["id"], status="done", summary="each finding fixed")
    row = p.db.one("SELECT * FROM review_stats WHERE task=?", (re_rev["id"],))
    assert (row["head"], row["lines"], row["areas"]) == (new, 43, "runtime,tests-docs"), row
    assert (row["rereview"], row["outcome"]) == (1, "approved"), row
    # The efficiency audit reads them.
    line = next(x for x in audit.lines(p.db, p=p) if x.startswith("reviews finished: 2"))
    assert "re-review 1 (0 failed" in line and "first 1 (1 failed" in line and "<50 2 (1 failed" in line, line
    rep = audit.report(p.db, p=p)["reviews"]
    assert rep["n"] == 2 and {r["task"] for r in rep["rows"]} == {rev["id"], re_rev["id"]}


def test_each_review_row_records_whether_the_reviewed_hand_off_carried_a_self_check(env):
    from ttp import audit, reviewstats
    p = make(env)
    ok = {"status": "done", "summary": "works"}
    cases = [("ticked", {**ok, "self_check": "steps 1-3 ticked; tried exit 2 and a killed run"}, 1),
             ("none", ok, 0),
             ("blank", {**ok, "self_check": "  "}, 0),
             ("not text", {**ok, "self_check": True}, 0),
             ("on pass", {"status": "waiting", "summary": "checks", "on_pass": {**ok, "self_check": "ticked"}}, 1)]
    for title, result, want in cases:
        if result["status"] == "waiting":   # the final hand-off is recorded from its on_pass
            tid, _, _, _ = _finish_code(env, p, title, {f"{title}.py": 3}, result=ok)
            p.db.update_task(tid, result=json.dumps(result))
            (rev,) = p.db.q("SELECT * FROM tasks WHERE kind='review' AND depends_on=?", (json.dumps([tid]),))
        else:
            _, _, _, (rev,) = _finish_code(env, p, title, {f"{title}.py": 3}, result=result)
        _fail_review(env, p, rev["id"], status="done", summary="fine")
        row = p.db.one("SELECT * FROM review_stats WHERE task=?", (rev["id"],))
        assert row["self_check"] == want, (title, row)
    assert reviewstats.has_self_check(p.db, {"title": "Review #99999: gone"}) is None, "no reviewed task"
    assert reviewstats.has_self_check(p.db, {"title": "a review naming no task"}) is None
    p.db.x("UPDATE review_stats SET self_check=NULL WHERE task=?", (rev["id"],))   # a row from before the field
    s = reviewstats.summary(p.db, 0)["by_self_check"]
    assert {k: g["n"] for k, g in s.items()} == {"self-checked": 1, "not self-checked": 3, "unknown": 1}, s
    line = next(x for x in audit.lines(p.db, p=p) if x.startswith("reviews finished: 5"))
    assert "not self-checked 3 (0 failed" in line and "self-checked 1 (0 failed" in line and "unknown 1" in line, line


def test_the_self_check_column_is_added_to_an_existing_review_stats_table(env):
    from ttp.db import DB
    path = env["tmp"] / "old-stats.db"
    old = DB(path)
    old.conn.execute("DROP TABLE review_stats")
    old.conn.execute("CREATE TABLE review_stats (task INTEGER PRIMARY KEY, ts REAL NOT NULL, head TEXT, "
                     "lines INTEGER, areas TEXT, tier TEXT, rereview INTEGER NOT NULL DEFAULT 0, outcome TEXT, "
                     "cost_usd REAL NOT NULL DEFAULT 0)")
    old.conn.execute("INSERT INTO review_stats(task,ts,outcome) VALUES(7,1.0,'approved')")
    old.conn.commit()
    old.conn.close()
    db = DB(path)
    row = db.one("SELECT * FROM review_stats WHERE task=7")
    assert row["self_check"] is None and row["code_lines"] is None, "older rows keep NULL"
    db.conn.close()
    assert {"self_check", "code_lines"} <= {r["name"] for r in DB(path).q("PRAGMA table_info(review_stats)")}


def test_a_coordinator_fix_review_leads_with_the_delta_from_the_recorded_failed_head(env):
    p = make(env)
    _, branch, _, (rev,) = _finish_code(env, p, "feature", {"app.py": 40})
    failed = _git_out(p.root, "rev-parse", branch).strip()
    _fail_review(env, p, rev["id"])   # no fix specs: the coordinator queues the fix
    _, fbranch, _, (rev2,) = _finish_code(env, p, "redo", {"app2.py": 3}, labels=[f"continues:{rev['id']}"])
    new = _git_out(p.root, "rev-parse", fbranch).strip()
    spec = p.db.task(rev2["id"])["spec"]
    assert f"Review #{rev['id']} failed on an earlier head. Fix delta: failed head {failed[:12]}, " \
           f"new head {new[:12]}. Lead with `git diff {failed[:12]}..{new[:12]}`: check each of its findings" in spec
    # No recorded head (a review from before the stats): the earlier guidance.
    p.db.x("DELETE FROM review_stats")
    _, _, _, (rev3,) = _finish_code(env, p, "redo again", {"app3.py": 3}, labels=[f"continues:{rev['id']}"])
    assert f"Review #{rev['id']} failed on an earlier head: check each of its findings is fixed" in \
        p.db.task(rev3["id"])["spec"]




def _rebased(p, old_branch, name, *, patch=None, extra=False, merge=False):
    """A code task branched from the base after it moved on, carrying `old_branch`'s commit
    (cherry-picked, or with `patch` as that file's text), plus an extra commit or a merge."""
    _commit_file(p.root, f"base-{name}")
    tid, path, branch = _code_task(p, name)
    if patch is None:
        _git_out(path, *_IDENT, "cherry-pick", old_branch)
    else:
        (path / "a.txt").write_text(patch)
        _git_out(path, "add", ".")
        _git_out(path, *_IDENT, "commit", "-qm", "a")
    if extra:
        _commit_file(path, "more")
    if merge:
        _git_out(path, "checkout", "-qb", f"side-{name}", "HEAD~1")
        _commit_file(path, "side")
        _git_out(path, "checkout", "-q", branch)
        _git_out(path, *_IDENT, "merge", "-q", "--no-ff", "-m", "merge side", f"side-{name}")
    return tid, branch, _git_out(path, "rev-parse", "HEAD")


def _accepted_old_head(p, monkeypatch):
    """A code task with one commit whose head a review accepted: (daemon, started, task id, branch, head)."""
    tid, path, branch = _code_task(p, "make it")
    _commit_file(path, "a")
    d, started = _reuse_daemon(p, monkeypatch)
    first = _reuse_review(p, tid)
    d.dispatch()
    p.db.update_task(first, status="done", result=json.dumps({"status": "done", "summary": "ok"}))
    return d, started, tid, branch, _git_out(path, "rev-parse", "HEAD")


def _rebase_review(p, tid, old_branch, old_head):
    """A review of rebase task `tid` whose spec also names the old branch and head."""
    return p.db.add_task("Review the rebase", f"Review {old_branch} ({old_head}) rebased.", kind="review",
                         tier="light", origin="coordinator", provider="claude", budget_usd=8.0, depends_on=[tid])


def test_a_rebase_review_reviews_its_dependency_head_not_the_older_spec_named_one(env, monkeypatch):
    from ttp import reviewreuse, worktree
    p = make(env)
    d, started, tid, branch, old = _accepted_old_head(p, monkeypatch)
    new_tid, new_branch, new = _rebased(p, branch, "rebase it")
    review = _rebase_review(p, new_tid, branch, old)
    assert worktree.reviewed_head(p, p.db.task(review)) == new, "the dependency's head, not the old one"
    d.dispatch()
    assert started[-1] == review and p.db.task(review)["status"] != "done", "the rebased head gets its own run"
    assert reviewreuse.recorded(p.db.task(review))[0] == new
    workdir = worktree.review_workdir(p, p.db.task(review))
    assert workdir and workdir.resolve() == (p.worktrees / f"t{new_tid}").resolve(), "it runs in the rebased one"


def test_verdict_reuse_needs_the_same_patches_on_every_covered_head(env, monkeypatch):
    from ttp import reviewreuse
    p = make(env)
    d, started, tid, branch, old = _accepted_old_head(p, monkeypatch)
    same = _rebased(p, branch, "same")
    changed = _rebased(p, branch, "changed", patch="other text")
    extra = _rebased(p, branch, "extra", extra=True)
    merged = _rebased(p, branch, "merged", merge=True)

    def covering(t):
        return {"id": None, "spec": f"Review {old}.", "depends_on": json.dumps([t[0]])}
    assert reviewreuse.rebased_away(p, covering(same), old) == "", "an identical rebase may reuse"
    assert "changed patch" in reviewreuse.rebased_away(p, covering(changed), old)
    assert "2 commit(s) against 1" in reviewreuse.rebased_away(p, covering(extra), old)
    assert "merge" in reviewreuse.rebased_away(p, covering(merged), old)
    # A head the accepted one contains (the code task's own branch) is no difference.
    assert reviewreuse.rebased_away(p, {"id": None, "spec": "", "depends_on": json.dumps([tid])}, old) == ""


def test_the_daemon_and_task_add_refuse_reuse_when_a_covered_head_differs(env, monkeypatch):
    from ttp import coordinator as coord, worktree
    p = make(env)
    d, started, tid, branch, old = _accepted_old_head(p, monkeypatch)
    new_tid, _, new = _rebased(p, branch, "changed", patch="other text")
    # A review that still picks the old head (as before dependency heads came first) must not reuse it.
    monkeypatch.setattr(worktree, "reviewed_head", lambda p, r: old)
    review = _rebase_review(p, new_tid, branch, old)
    d.dispatch()
    assert started[-1] == review and p.db.task(review)["status"] != "done"
    assert coord._rebased_away(p, [new_tid], f"Review {old}.", old)
    same_tid, _, _ = _rebased(p, branch, "same")
    assert coord._rebased_away(p, [same_tid], f"Review {old}.", old) == ""


def _quoting_review(p, spec, depends_on=()):
    return p.db.add_task("Review it", spec, kind="review", tier="light", origin="daemon", provider="claude",
                         budget_usd=8.0, depends_on=list(depends_on))


def test_a_review_diff_has_only_its_branch_files_not_a_commit_its_spec_quotes(env):
    from ttp import reviewcap, worktree
    p = make(env)
    tid, path, branch = _code_task(p, "the change")
    head = _commit_files(path, {"daemon.py": "a\nb\n"}, "change")
    _, other, _ = _code_task(p, "an unrelated one")
    quoted = _commit_files(other, {"notifier.py": "x\ny\nz\n"}, "notifier")
    spec = (f"Independently review the change of code task #{tid}: branch {branch}, head {head[:12]}.\n"
            f"Its spec: Incident: the steps for notifier head {quoted[:8]} never arrived.")
    for deps in ((), (tid,)):
        review = p.db.task(_quoting_review(p, spec, deps))
        assert quoted[:8] not in worktree.named_refs(p, review), "a quoted commit off the branch is evidence"
        assert set(worktree.diff_lines(p, worktree.reviewed_refs(p, review))) == {"daemon.py"}
        assert reviewcap.main_file(p, review) == "daemon.py"
        assert worktree.reviewed_head(p, review) == head
    # A quoted commit on the branch (an earlier head) still counts.
    later = _commit_files(path, {"daemon.py": "a\nb\nc\n"}, "more")
    review = p.db.task(_quoting_review(p, spec + f"\nNew head: {branch} at {later[:12]}."))
    assert {head[:12], later[:12]} <= set(worktree.named_refs(p, review))
    assert worktree.reviewed_head(p, review) == later


def test_the_head_a_review_is_said_to_review_counts_even_off_its_branch(env):
    from ttp import worktree
    p = make(env)
    tid, path, branch = _code_task(p, "the change")
    old = _commit_files(path, {"daemon.py": "a\n"}, "change")
    _git_out(path, "reset", "-q", "--hard", "HEAD~1")
    _commit_files(path, {"other.py": "b\n"}, "rewritten")
    for spec in (f"Review the change: branch {branch}, head {old[:12]}.", f"Branch: {branch} at {old}.",
                 f"{branch} rebased as {old}: push it"):
        review = p.db.task(_quoting_review(p, spec))
        assert old[:12] in worktree.named_refs(p, review) or old in worktree.named_refs(p, review)
        assert "daemon.py" in worktree.diff_lines(p, worktree.reviewed_refs(p, review))
    # A commit built on the branch (a fix, a settled conflict) counts however the spec words it.
    tip = _git_out(path, "rev-parse", "HEAD")
    built = _git_out(path, "commit-tree", f"{old}^{{tree}}", "-p", tip, "-m", "settle")
    review = p.db.task(_quoting_review(p, f"Push {built} of {branch}."))
    assert built in worktree.named_refs(p, review)


def test_a_review_spec_naming_only_a_hash_still_reviews_it(env):
    from ttp import worktree
    p = make(env)
    _, path, _ = _code_task(p, "the change")
    head = _commit_files(path, {"daemon.py": "a\n"}, "change")
    review = p.db.task(_quoting_review(p, f"Review commit {head[:10]}."))
    assert worktree.named_refs(p, review) == [head[:10]]
    assert set(worktree.diff_lines(p, worktree.reviewed_refs(p, review))) == {"daemon.py"}
