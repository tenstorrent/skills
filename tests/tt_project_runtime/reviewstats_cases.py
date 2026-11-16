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
    assert [reviewstats.size_bucket(n) for n in (None, 49, 50, 100, 101, 200, 201)] == [
        "unknown", "<50", "50-100", "50-100", "101-200", "101-200", ">200"]


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
    p.set_config("chains.split_lines", 0)   # reviewed unsplit, not sent back for a split (splitgate)
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


def test_a_waiting_hand_offs_self_check_reaches_the_final_hand_off_and_metrics_self_check_counts(env):
    """#4579: a waiting hand-off's top-level self_check survives its on_pass becoming the final
    hand-off (never over one the on_pass has), and has_self_check also reads metrics.self_check."""
    from ttp import reviewstats
    from ttp.daemon import with_waiting_followups
    done = {"status": "done", "summary": "works"}
    waited = {"status": "waiting", "self_check": "steps ticked", "on_pass": done}
    assert with_waiting_followups(done, waited) == {**done, "self_check": "steps ticked"}
    own = {**done, "self_check": "its own"}
    assert with_waiting_followups(own, waited) == own, "never overwritten"
    assert with_waiting_followups({**done, "self_check": " "}, waited)["self_check"] == "steps ticked"
    for no in ({"status": "waiting"}, {"self_check": " "}, {"self_check": True}, None):
        assert with_waiting_followups(done, no) == done, no
    later = {"title": "later", "spec": "x"}
    assert with_waiting_followups(done, {**waited, "followups": [later]}) == \
        {**done, "self_check": "steps ticked", "followups": [later]}, "followups still carried too"
    p = make(env)
    cases = [({**done, "metrics": {"self_check": "ticked"}}, 1),
             ({"status": "waiting", "on_pass": {**done, "metrics": {"self_check": "ticked"}}}, 1),
             ({**done, "metrics": {"self_check": " "}}, 0),
             ({**done, "metrics": {"self_check": 1}}, 0),
             ({**done, "metrics": "self_check"}, 0),
             ({"status": "waiting", "on_pass": "done"}, 0),
             (done, 0)]
    for result, want in cases:
        tid = p.db.add_task("a change", "s", kind="code", tier="standard", origin="user")
        p.db.update_task(tid, result=json.dumps(result))
        assert reviewstats.has_self_check(p.db, {"title": f"Review #{tid}: a change"}) == want, result


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



def test_a_finished_reviews_named_refs_are_computed_once_and_an_open_ones_every_time(env, monkeypatch):
    from ttp import worktree
    p = make(env)
    tid, path, branch = _code_task(p, "the change")
    head = _commit_files(path, {"daemon.py": "a\n"}, "change")
    spec = f"Review the change of code task #{tid}: branch {branch}, head {head[:12]}."
    calls = []
    real = worktree._named_refs
    monkeypatch.setattr(worktree, "_named_refs", lambda p, t: calls.append(t["id"]) or real(p, t))
    monkeypatch.setattr(worktree, "_NAMED_MEMO", {})
    review = p.db.task(_quoting_review(p, spec, (tid,)))
    first = worktree.named_refs(p, review)
    assert worktree.named_refs(p, review) == first and len(calls) == 2, "an open review is never remembered"
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (review["id"],))
    done = p.db.task(review["id"])
    assert worktree.named_refs(p, done) == first and worktree.named_refs(p, done) == first
    assert len(calls) == 3, "a finished review is computed once"
    # A changed spec is a different entry; an expired one is computed again.
    worktree.named_refs(p, {**done, "spec": spec + " More."})
    assert len(calls) == 4
    monkeypatch.setattr(worktree, "_NAMED_MEMO_S", 0.0)
    worktree.named_refs(p, done)
    assert len(calls) == 5

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


def test_a_review_spec_quoting_many_hashes_is_sorted_in_a_fixed_number_of_git_calls(env, monkeypatch):
    """Run 247: the reap step re-ran `merge-base --is-ancestor` for every quoted hash against every
    branch head, both ways (0.5-0.8 s each under load), and never finished within its budget. Now one
    cat-file drops the hashes that name no commit, then two rev-lists per head sort the rest."""
    from ttp import gitmemo, worktree
    p = make(env)
    tid, path, branch = _code_task(p, "the change")
    first = _commit_files(path, {"daemon.py": "a\n"}, "change")
    head = _commit_files(path, {"daemon.py": "a\nb\n"}, "more")
    built = _git_out(path, "commit-tree", f"{head}^{{tree}}", "-p", head, "-m", "settle")
    _git_out(path, "reset", "-q", "--hard", "HEAD~2")
    off = _commit_files(path, {"other.py": "x\n"}, "elsewhere")
    _git_out(path, "reset", "-q", "--hard", head)
    missing = ["49572d71", "a2667391", "deadbeefcafe"]
    spec = (f"Review #{tid}. Earlier {first[:10]}, fix {built[:10]}, unrelated {off[:10]}; "
            + ", ".join(f"see {m}" for m in missing) + ".")
    calls = []
    real = gitmemo.run
    monkeypatch.setattr(gitmemo, "run", lambda argv, **kw: calls.append(list(argv)) or real(argv, **kw))
    refs = worktree._named_refs(p, p.db.task(_quoting_review(p, spec, (tid,))))
    assert first[:10] in refs and built[:10] in refs, refs
    assert off[:10] not in refs and not set(missing) & set(refs), refs
    assert not [c for c in calls if "--is-ancestor" in c], calls
    assert len([c for c in calls if {"cat-file", "rev-list", "merge-base", "rev-parse"} & set(c)]) <= 4, calls


def _ancestry_repo(p, path):
    """Commits in `path` (a task worktree of p): a line c0-a1-a2, a side branch c0-b1, a merge m of a2
    and b1, a commit after it, and an unrelated history r1-r2. {name: full id}."""
    c = {"c0": _git_out(path, "rev-parse", "HEAD")}
    c["a1"] = _commit_files(path, {"a.py": "1\n"}, "a1")
    c["a2"] = _commit_files(path, {"a.py": "2\n"}, "a2")
    _git_out(path, "reset", "-q", "--hard", c["c0"])
    c["b1"] = _commit_files(path, {"b.py": "1\n"}, "b1")
    tree = _git_out(path, "rev-parse", f"{c['a2']}^{{tree}}")
    c["m"] = _git_out(path, *_IDENT, "commit-tree", tree, "-p", c["a2"], "-p", c["b1"], "-m", "merge")
    _git_out(path, "reset", "-q", "--hard", c["m"])
    c["after"] = _commit_files(path, {"c.py": "1\n"}, "after")
    c["r1"] = _git_out(path, *_IDENT, "commit-tree", tree, "-m", "unrelated root")
    c["r2"] = _git_out(path, *_IDENT, "commit-tree", tree, "-p", c["r1"], "-m", "unrelated")
    return c


def test_batched_ancestry_answers_every_pair_as_merge_base_is_ancestor_would(env):
    """worktree.ancestry answers (older, newer) pairs in a fixed number of git calls: the same answer as
    one `merge-base --is-ancestor` per pair, over branches, a merge, an unrelated history, short ids,
    a branch name and names that name no commit, for random subsets (each has its own octopus base)."""
    import random
    import subprocess
    from ttp import worktree
    p = make(env)
    _, path, branch = _code_task(p, "the change")
    c = _ancestry_repo(p, path)
    names = [*c.values(), c["a1"][:9], c["b1"][:12], branch,
             "deadbeefcafe", "0" * 40, "no-such-branch"]

    def one(a, b):
        return subprocess.run(["git", "-C", str(p.root), "merge-base", "--is-ancestor", a, b],
                              capture_output=True).returncode == 0
    every = [(a, b) for a in names for b in names]
    truth = {pair: one(*pair) for pair in every}
    assert truth[(c["c0"], c["after"])] and truth[(c["b1"], c["m"])] and not truth[(c["b1"], c["a2"])]
    assert not truth[(c["r1"], c["after"])] and truth[(c["r1"], c["r2"])]
    assert worktree.ancestry(p, every) == truth
    rng = random.Random(7815)
    for _ in range(25):
        pairs = rng.sample(every, rng.randint(1, 12))
        assert worktree.ancestry(p, pairs) == {pair: truth[pair] for pair in pairs}, pairs
    assert worktree.ancestry(p, []) == {}


def test_batched_ancestry_spawns_a_fixed_number_of_git_calls_whatever_the_pairs(env, monkeypatch):
    """At most three git calls (cat-file, merge-base --octopus, rev-list) for 1 pair or 200; reviewed_head
    and a review naming many hashes the same: the count does not grow with the pairs. A call that times
    out answers no."""
    import subprocess
    from ttp import gitmemo, worktree
    p = make(env)
    _, path, branch = _code_task(p, "the change")
    c = _ancestry_repo(p, path)
    ids = list(c.values())
    calls = []
    real = gitmemo.run
    monkeypatch.setattr(gitmemo, "run", lambda argv, **kw: calls.append(list(argv)) or real(argv, **kw))

    def count(pairs):
        calls.clear()
        worktree.ancestry(p, pairs)
        return len(calls)
    few = count([(c["a1"], c["m"])])
    many = count([(a, b) for a in ids for b in ids] + [(a[:10], branch) for a in ids])
    assert few <= 3 and many <= 3 and not [x for x in calls if "--is-ancestor" in x], calls

    def slow(argv, **kw):
        if "merge-base" in argv or "rev-list" in argv:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout") or 1)
        return real(argv, **kw)
    monkeypatch.setattr(gitmemo, "run", slow)
    out = worktree.ancestry(p, [(c["a1"], c["m"]), (c["m"], c["m"]), ("deadbeefcafe", "deadbeefcafe")])
    assert out == {(c["a1"], c["m"]): False, (c["m"], c["m"]): True, ("deadbeefcafe", "deadbeefcafe"): False}


def test_a_review_with_many_heads_and_hashes_sorts_them_in_the_same_git_calls_as_with_few(env, monkeypatch):
    """_named_refs and reviewed_head over a review with 1 dependency quoting 2 hashes, then 6
    dependencies quoting 30: the git spawn count stays the same (it was 2 per hash per head)."""
    from ttp import gitmemo, worktree
    p = make(env)
    calls = []
    real = gitmemo.run
    monkeypatch.setattr(gitmemo, "run", lambda argv, **kw: calls.append(list(argv)) or real(argv, **kw))

    def count(n_deps, n_hashes):
        deps, quoted = [], []
        for i in range(n_deps):
            tid, path, _ = _code_task(p, f"change {n_deps}-{i}")
            quoted += [_commit_files(path, {f"f{i}.py": f"{k}\n"}, f"c{k}")[:10] for k in range(n_hashes // n_deps)]
            deps.append(tid)
        quoted += [f"{k:08x}" for k in range(5)]   # name no commit
        review = p.db.task(_quoting_review(p, "Review it. " + ", ".join(quoted) + ".", deps))
        calls.clear()
        refs = worktree._named_refs(p, review)
        assert set(quoted[:-5]) <= set(refs) and not set(quoted[-5:]) & set(refs), refs
        named = len(calls)
        calls.clear()
        assert worktree.reviewed_head(p, review)
        return named, len(calls)
    small, large = count(1, 2), count(6, 30)
    assert large[0] <= small[0] + 1 and large[1] <= small[1] + 1, (small, large)
    assert large[0] <= 4 and large[1] <= 7, (large, [" ".join(x[3:5]) for x in calls])   # _named_refs, then +3


def test_a_review_spec_naming_only_a_hash_still_reviews_it(env):
    from ttp import worktree
    p = make(env)
    _, path, _ = _code_task(p, "the change")
    head = _commit_files(path, {"daemon.py": "a\n"}, "change")
    review = p.db.task(_quoting_review(p, f"Review commit {head[:10]}."))
    assert worktree.named_refs(p, review) == [head[:10]]
    assert set(worktree.diff_lines(p, worktree.reviewed_refs(p, review))) == {"daemon.py"}


def _rereview_setup(env):
    """A code task's branch whose review failed at its head (recorded in review_stats), and a done
    fix on the same branch that the re-review depends on."""
    import json
    from ttp import worktree
    from ttp.daemon import Daemon
    p = make(env)
    first = p.db.add_task("stack", "s", kind="code", tier="standard", origin="user")
    path, branch = worktree.ensure(p, p.db.task(first))

    def commit(name, n, msg="c"):
        (path / name).write_text("".join(f"x{i} = {i}\n" for i in range(n)))
        _git_out(path, "add", ".")
        _git_out(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
        return _git_out(path, "rev-parse", "HEAD")

    failed_head = commit("app.py", 30)
    p.db.update_task(first, status="done", branch=branch)
    old = p.db.add_task("review", f"Review branch {branch}.", kind="review", tier="light", origin="coordinator",
                        depends_on=[first])
    p.db.update_task(old, status="failed")
    p.db.x("INSERT INTO review_stats(task,ts,head,tier,rereview,outcome,cost_usd) VALUES(?,?,?,?,?,?,?)",
           (old, 0, failed_head, "light", 0, "failed", 0.1))
    fix = p.db.add_task(f"fix {old}", "s", kind="code", origin="coordinator", labels=[f"continues:{old}"])
    p.db.update_task(fix, status="done", branch=branch)

    def rereview(tier="standard"):
        rid = p.db.add_task("review", f"Review branch {branch}.", kind="review", tier=tier, origin="daemon",
                            depends_on=[fix])
        return p.db.task(rid)

    return p, Daemon(p.base), path, commit, rereview, old


def test_a_re_review_of_a_small_fix_delta_runs_light(env):
    p, d, _, commit, rereview, old = _rereview_setup(env)
    commit("app2.py", 5)
    task, pick = d._size_review(rereview())
    assert task["tier"] == "light", pick
    assert pick["rule"] == f"re-review after failed review #{old}: fix delta 5 lines <= 80", pick
    assert pick["lines"] == 5 and pick["files"] == 1
    # A retry of it does not run light.
    t = rereview()
    p.db.update_task(t["id"], attempts=1)
    assert d._size_review(p.db.task(t["id"]))[0]["tier"] == "standard"


def test_a_re_review_of_a_large_fix_delta_runs_standard(env):
    _, d, _, commit, rereview, _ = _rereview_setup(env)
    commit("app2.py", 200)
    task, pick = d._size_review(rereview())
    assert task["tier"] == "standard" and pick["rule"].endswith("fix delta 200 lines > 80"), pick


def test_a_re_review_whose_fix_delta_touches_a_risky_name_runs_standard(env):
    _, d, _, commit, rereview, _ = _rereview_setup(env)
    commit("push_queue.py", 3)
    task, pick = d._size_review(rereview())
    assert task["tier"] == "standard" and "risky name push_queue.py" in pick["rule"], pick


def test_a_re_review_whose_fix_delta_spans_a_rebase_runs_standard(env):
    from ttp import worktree
    p, d, path, commit, rereview, _ = _rereview_setup(env)
    base = worktree.resolve_base(p)
    # New upstream code lands on the base; the branch takes it in by a merge, then by a rebase.
    up = worktree.ensure(p, p.db.task(p.db.add_task("up", "s", kind="code", origin="user")))[0]
    (up / "upstream.py").write_text("u = 1\n")
    _git_out(up, "add", ".")
    _git_out(up, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "upstream")
    upstream = _git_out(up, "rev-parse", "HEAD")
    _git_out(p.root, "update-ref", f"refs/heads/{base}" if not base.startswith("origin/") else f"refs/remotes/{base}",
             upstream)
    _git_out(path, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-edit", upstream)
    task, pick = d._size_review(rereview())
    assert task["tier"] == "standard" and "spans a rebase onto new upstream code" in pick["rule"], pick
    _git_out(path, "reset", "-q", "--hard", "HEAD~1")
    commit("app2.py", 2)
    _git_out(path, "-c", "user.name=t", "-c", "user.email=t@t", "rebase", "-q", upstream)
    task, pick = d._size_review(rereview())
    assert task["tier"] == "standard" and "spans a rebase onto new upstream code" in pick["rule"], pick


def test_a_deep_re_review_stays_deep_and_an_unrecorded_failed_head_stays_standard(env):
    p, d, _, commit, rereview, old = _rereview_setup(env)
    commit("app2.py", 5)
    deep = rereview("deep")
    assert d._size_review(deep) == (deep, None)
    p.db.x("UPDATE review_stats SET head=NULL WHERE task=?", (old,))
    task, pick = d._size_review(rereview())
    assert task["tier"] == "standard" and "(head (none recorded) not found)" in pick["rule"], pick
    # No fix delta at all: the failed head is reviewed again at standard.
    p, d, _, _, rereview, _ = _rereview_setup(env)
    task, pick = d._size_review(rereview())
    assert task["tier"] == "standard" and "no fix delta" in pick["rule"], pick


def _land_on_base(p, branch):
    """Fast-forward the base branch to `branch` (its reflog gains an entry), as a push that landed it."""
    from ttp import worktree
    base = worktree.resolve_base(p)
    before = _git_out(p.root, "rev-parse", base)
    _git_out(p.root, "update-ref", "-m", "landed", f"refs/heads/{base}", _git_out(p.root, "rev-parse", branch))
    return base, before


def test_an_approved_review_whose_head_already_landed_keeps_its_size(env):
    """An approved review's head is often on the base when its row is written: `base...head` is then
    empty. It is measured against the base tip it landed on (from the reflog), and a later verdict
    that measures nothing keeps the row's known size."""
    from ttp import reviewstats
    p = make(env)
    _, branch, _, (rev,) = _finish_code(env, p, "landed", {"app.py": 30, "tests/test_app.py": 5})
    head = _git_out(p.root, "rev-parse", branch)
    base, _ = _land_on_base(p, branch)
    _fail_review(env, p, rev["id"], status="done", summary="fine")
    row = p.db.one("SELECT * FROM review_stats WHERE task=?", (rev["id"],))
    assert (row["head"], row["lines"], row["code_lines"], row["outcome"]) == (head, 35, 30, "approved"), row
    assert row["areas"] == "runtime,tests-docs", row
    # The reflog is gone: a later verdict of the same review replaces the row and keeps its size.
    _git_out(p.root, "reflog", "expire", "--expire=now", "--all")
    again = reviewstats.record(p, p.db.task(rev["id"]), "failed")
    row = p.db.one("SELECT * FROM review_stats WHERE task=?", (rev["id"],))
    assert again["outcome"] == "failed" and (row["lines"], row["code_lines"], row["outcome"]) == (35, 30, "failed")
    assert row["areas"] == "runtime,tests-docs", row


def test_a_landed_head_is_measured_against_the_push_queues_pre_push_tip(env):
    from ttp import reviewstats
    p = make(env)
    _, branch, _, (rev,) = _finish_code(env, p, "queued", {"app.py": 12})
    head = _git_out(p.root, "rev-parse", branch)
    base, before = _land_on_base(p, branch)
    _git_out(p.root, "reflog", "expire", "--expire=now", "--all")   # only the push queue knows the tip
    p.db.x("INSERT INTO push_batches(id,marker,target,started,tip) VALUES('b1','m','t',0,?)", (before,))
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,batch,created,updated) "
           "VALUES(?,?,?,'t','pushed','b1',0,0)", (rev["id"], branch, head))
    row = reviewstats.record(p, p.db.task(rev["id"]), "done")
    assert (row["head"], row["lines"], row["code_lines"]) == (head, 12, 12), row
    # A recorded tip that already contains the head measures nothing: no size, not a zero.
    p.db.x("DELETE FROM review_stats")
    p.db.x("UPDATE push_batches SET tip=?", (head,))
    assert reviewstats.record(p, p.db.task(rev["id"]), "done")["lines"] is None


def test_a_review_whose_subject_has_no_branch_measures_the_commit_its_title_or_spec_names(env):
    from ttp import reviewstats
    p = make(env)
    _, branch, _, _ = _finish_code(env, p, "side", {"lib.py": 7})
    head = _git_out(p.root, "rev-parse", branch)
    by_title = p.db.add_task(f"Review {head[:12]}: the side change", "check it", kind="review")
    row = reviewstats.record(p, p.db.task(by_title), "done")
    assert (row["head"], row["lines"]) == (head, 7), row
    by_spec = p.db.add_task("Review the side change", f"Review commit {head[:10]} for regressions.", kind="review")
    row = reviewstats.record(p, p.db.task(by_spec), "done")
    assert (row["head"], row["lines"]) == (head, 7), row
    none = p.db.add_task("Review the side change", "nothing named", kind="review")
    assert reviewstats.record(p, p.db.task(none), "done")["head"] is None


def test_a_harness_task_review_is_measured_in_the_harness_repo_against_its_main_line(env):
    from ttp import harnesstree, reviewstats
    p = make(env)
    tid = p.db.add_task("tune a prompt", "s", kind="harness")
    h, b = p.harness, harnesstree.branch(tid)
    _git_out(h, "branch", b, harnesstree.main_branch(p))
    wt = env["tmp"] / "harness-wt"
    _git_out(h, "worktree", "add", "-q", str(wt), b)
    (wt / "prompts").mkdir(exist_ok=True)
    (wt / "prompts" / "extra.md").write_text("".join(f"line {i}\n" for i in range(9)))
    _git_out(wt, "add", ".")
    _git_out(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "prompt")
    rid = p.db.add_task(f"Review #{tid}: tune a prompt", "s", kind="review", labels=[f"auto_review:{tid}"])
    row = reviewstats.record(p, p.db.task(rid), "done")
    assert (row["head"], row["lines"], row["areas"]) == (
        _git_out(h, "rev-parse", b), 9, "skills-prompts-config"), row


def test_landed_from_reads_the_whole_reflog_in_a_fixed_number_of_git_calls(env, monkeypatch):
    """The tip a head landed on, from the branch's reflog: one ancestry batch for every entry (it was a
    merge-base per entry, up to 50, on each review the daemon records)."""
    from ttp import gitmemo, worktree
    p = make(env)
    _, path, branch = _code_task(p, "the change")
    before = _git_out(path, "rev-parse", "HEAD")
    landed = _commit_files(path, {"a.py": "1\n"}, "landed")
    for k in range(8):
        _commit_files(path, {"b.py": f"{k}\n"}, f"later {k}")
    calls = []
    real = gitmemo.run
    monkeypatch.setattr(gitmemo, "run", lambda argv, **kw: calls.append(list(argv)) or real(argv, **kw))
    assert worktree.landed_from(p.root, branch, landed) == before
    assert len(calls) <= 4 and not [c for c in calls if "--is-ancestor" in c], calls
    assert worktree.landed_from(p.root, branch, "0" * 40) == _git_out(path, "rev-parse", "HEAD"), \
        "a head that names no commit is on no entry: the newest"


_OTHER_HEAD = "0123456789abcdef0123456789abcdef01234567"   # a commit no test repository holds
_SUM_A, _SUM_B = "a" * 63 + "1", "b" * 63 + "2"


def test_a_review_key_is_scoped_by_other_repo_heads_and_packets_only(env):
    from ttp import reviewreuse
    p = make(env)
    tid, path, branch = _code_task(p, "make it")
    head = _commit_files(path, {"a.py": "a\n"}, "change")
    key = reviewreuse.check_key(p.config())

    def k(spec):
        return reviewreuse.review_key(p, p.config(), {"spec": spec})
    assert k(f"Review {branch} at {head}. Note: [note 5f9d96bf17a446ea], run 12345678901.") == key, \
        "this repository's head, a note's fingerprint and a number leave the plain key"
    other = k(f"Review repo other/b, branch x, head {_OTHER_HEAD}.")
    short = k(f"Review the fix, commit {_OTHER_HEAD[8:17]} in another clone.")
    a, b = (k(f"Review the packet reviews/p.md sha256 {s}.") for s in (_SUM_A, _SUM_B))
    moved = k(f"Review the packet reviews/q.md sha256 {_SUM_A}.")
    assert len({key, other, short, a, b, moved}) == 6 and all(x.startswith(key + "+") for x in (other, a, b))
    assert k(f"Review the packet reviews/p.md sha256 {_SUM_A}.") == a, "an exact repeat keeps its key"
    assert reviewreuse.target(p, {"spec": f"reviews/p.md sha256 {_SUM_A}"}) == ([], [["reviews/p.md", _SUM_A]])
    lb = reviewreuse.head_label(head, a)
    assert reviewreuse.recorded({"labels": json.dumps([lb])}) == (head, a)


def test_another_repos_head_or_packet_neither_blocks_nor_reuses_a_verdict_but_an_exact_repeat_does(env, monkeypatch):
    from ttp import reviewreuse, verified
    p = make(env)
    tid, path, branch = _code_task(p, "make it")
    _commit_file(path, "a")
    head = _git_out(path, "rev-parse", "HEAD")
    d, started = _reuse_daemon(p, monkeypatch)
    ok = json.dumps({"status": "done", "summary": "ok"})
    other_spec = f"Review repo other/b, branch x, head {_OTHER_HEAD}; this branch {branch} holds its evidence."
    packet_spec = f"Review the detached packet reviews/p.md sha256 {_SUM_A}."
    # Repository B's head is reviewed first: the head here it names as evidence is not accepted by it.
    other = _quoting_review(p, other_spec, (tid,))
    d.dispatch()
    assert started == [other] and reviewreuse.recorded(p.db.task(other))[1] != reviewreuse.check_key(d.cfg)
    p.db.update_task(other, status="done", result=ok)
    d._mark_verified(p.db.task(other), None, {})
    assert verified.own(p.db, tid, "review") is None, "another repository's verdict marks no head here"
    assert d._auto_review(p.db.task(tid), "made it", add=False) is None, "nor satisfies this head's review"
    plain = _reuse_review(p, tid)
    d.dispatch()
    assert started == [other, plain], "repo B's verdict does not satisfy a review of this head"
    p.db.update_task(plain, status="done", result=ok)
    # A packet with another sha256 is neither blocked nor satisfied; the same packet is.
    first = _quoting_review(p, packet_spec, (tid,))
    d.dispatch()
    assert started[-1] == first, "this head's plain verdict does not cover a packet"
    p.db.update_task(first, status="done", result=ok)
    changed = _quoting_review(p, packet_spec.replace(_SUM_A, _SUM_B), (tid,))
    d.dispatch()
    assert started[-1] == changed and p.db.task(changed)["status"] != "done"
    for spec, prior in ((packet_spec, first), (other_spec, other), ("Review the change.", plain)):
        again = _quoting_review(p, spec, (tid,))
        d.dispatch()
        res = json.loads(p.db.task(again)["result"] or "{}")
        assert again not in started and res.get("reused_review") == prior, "an exact repeat reuses its verdict"
        assert res["reviewed_head"] == head


def test_task_add_refuses_only_a_review_of_the_exact_accepted_target(env, monkeypatch):
    from ttp import coordinator as coord
    p = make(env)
    tid, path, branch = _code_task(p, "make it")
    _commit_file(path, "a")
    d, started = _reuse_daemon(p, monkeypatch)
    first = _quoting_review(p, f"Review the detached packet reviews/p.md sha256 {_SUM_A}.", (tid,))
    d.dispatch()
    p.db.update_task(first, status="done", result=json.dumps({"status": "done", "summary": "ok"}))
    add = {"type": "task_add", "kind": "review", "depends_on": [tid]}
    for n, spec in enumerate((f"Review the detached packet reviews/p.md sha256 {_SUM_B}.",
                              f"Review repo other/b, branch x, head {_OTHER_HEAD}.", "Review the change.")):
        assert coord.apply(p, [dict(add, title=f"Review target {n}", spec=spec)]) == [], spec
    problems = coord.apply(p, [dict(add, title="Review the packet once more",
                                    spec=f"Review the detached packet reviews/p.md sha256 {_SUM_A}.")])
    assert len(problems) == 1 and f"review #{first}" in problems[0], problems


def test_task_add_is_not_refused_by_a_failed_review_of_another_target(env, monkeypatch):
    from ttp import coordinator as coord
    p = make(env)
    tid, path, branch = _code_task(p, "make it")
    _commit_file(path, "a")
    d, started = _reuse_daemon(p, monkeypatch)
    plain = _reuse_review(p, tid)
    d.dispatch()
    assert started == [plain]
    p.db.update_task(plain, status="failed", result=json.dumps(
        {"status": "changes_needed", "summary": "fix x", "followups": [{"title": "fix x", "spec": "fix x"}]}))
    add = {"type": "task_add", "kind": "review", "depends_on": [tid]}
    for n, spec in enumerate((f"Review the detached packet reviews/p.md sha256 {_SUM_B}.",
                              f"Review repo other/b, branch x, head {_OTHER_HEAD}.")):
        assert coord.apply(p, [dict(add, title=f"Review target {n}", spec=spec)]) == [], spec
    problems = coord.apply(p, [dict(add, title="Review the change once more", spec="Review the change.")])
    assert len(problems) == 1 and f"review #{plain}" in problems[0], problems


def test_a_spec_naming_a_commit_its_head_lacks_neither_reuses_nor_marks_that_head_verified(env, monkeypatch):
    from ttp import reviewreuse, verified
    p = make(env)
    tid, path, branch = _code_task(p, "make it")
    _commit_file(path, "a")
    head = _git_out(path, "rev-parse", "HEAD")
    _, other, _ = _code_task(p, "the repair")
    _commit_file(other, "fix")
    repair = _git_out(other, "rev-parse", "HEAD")
    d, started = _reuse_daemon(p, monkeypatch)
    ok = json.dumps({"status": "done", "summary": "ok"})
    first = _reuse_review(p, tid)
    d.dispatch()
    p.db.update_task(first, status="done", result=ok)
    # The labels still lead to the accepted head, but the spec targets the repair commit it lacks.
    lb = reviewreuse.head_label(head, reviewreuse.check_key(d.cfg))
    target = _quoting_review(p, f"Review the repair at commit {repair} (fix_base {head[:12]}).", (tid,))
    p.db.update_task(target, labels=json.dumps([lb]))
    assert reviewreuse.uncovered(p, p.db.task(target), head) == [repair]
    d.dispatch()
    assert started[-1] == target and p.db.task(target)["status"] != "done", "no verdict reused for another commit"
    p.db.update_task(target, status="done", result=ok)
    d._mark_verified(p.db.task(target), None, {})
    assert verified.own(p.db, tid, "review") is None, "nor is the head it never covered shown as verified"
    # A spec naming the accepted head itself, or an ancestor of it, still reuses the verdict.
    again = _quoting_review(p, f"Review {branch} at {head}.", (tid,))
    d.dispatch()
    assert again not in started and json.loads(p.db.task(again)["result"])["reused_review"] == first


def test_a_detached_repair_published_elsewhere_is_admitted_for_re_review(env, monkeypatch):
    from ttp.daemon import FIX_BASE_LABEL
    p = make(env)
    tid, path, branch = _code_task(p, "fix review #1: make it")
    _commit_file(path, "a")
    base = _git_out(path, "rev-parse", "HEAD")
    p.db.update_task(tid, labels=json.dumps(["review_fix:1"]))
    rr = _quoting_review(p, "Re-review the fix.", (tid,))
    p.db.update_task(rr, status="queued", labels=json.dumps([f"{FIX_BASE_LABEL}{base}"]))
    d, _ = _reuse_daemon(p, monkeypatch)
    assert [r["id"] for r in d._fix_without_commits(p.db.task(tid))] == [rr], "no new commit here: blocked"
    published = {"status": "done", "published": {"repo": "other/b", "branch": "x", "head": _OTHER_HEAD}}
    assert d._fix_without_commits(dict(p.db.task(tid), result=json.dumps(published))) == [], \
        "a repair published in another repository is new to review"
