# Continue-chain (chaincap) audit tests of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there). Add new continue-chain tests here, not there.
from __future__ import annotations


def _chain_hop(p, prev, title, usd, labels=(), parent=None):
    tid = p.db.add_task(title, "s", kind="code", origin="daemon", parent=parent,
                        labels=list(labels) or [f"continues:{prev}"])
    p.db.update_task(tid, status="done", spent_usd=usd)
    return tid


def _chain_root(p, title, usd):
    tid = p.db.add_task(title, "s", kind="code", origin="user")
    p.db.update_task(tid, status="done", spent_usd=usd)
    return tid


def test_continue_chain_audit_counts_review_fix_hops_apart_from_continuations(env):
    """A review fix (the daemon's review_fix: label, or a 'Fix review #N' title) is no continuation: the
    review-loop line counts its spend, so the chain audit shows the two kinds as separate figures. A
    `continue:` or FOLLOWS hop that took a failed review's findings over (review_fix: added later) stays a
    continuation."""
    import time
    from ttp import audit, chaincap
    p = make(env)
    since = time.time() - 60
    # Pure review-fix lineage: two fix rounds, one by label, one the coordinator added by title only.
    fr = _chain_root(p, "fix lineage", 2.0)
    f1 = _chain_hop(p, fr, "Fix review #901: fix lineage", 0.5, [f"continues:{fr}", "review_fix:901"])
    f2 = _chain_hop(p, f1, "Fix review #902: fix lineage", 0.25)
    # Pure continuation lineage: a continue: hop by parent, a FOLLOWS hop, and a continue: hop that took
    # a review's findings over.
    cr = _chain_root(p, "split lineage", 3.0)
    c1 = _chain_hop(p, cr, "continue: split lineage, part 2", 1.0, ["x"], parent=cr)
    c2 = _chain_hop(p, c1, "split lineage, part 3", 1.5, [f"{chaincap.FOLLOWS}{c1}"])
    c3 = _chain_hop(p, c2, "continue: split lineage, part 4", 0.75, [f"continues:{c2}", "review_fix:903"])
    # Mixed lineage: a continuation, then a fix of its review.
    mr = _chain_root(p, "mixed lineage", 4.0)
    m1 = _chain_hop(p, mr, "continue: mixed lineage", 2.0)
    m2 = _chain_hop(p, m1, "Fix review #904: mixed lineage", 0.5, [f"continues:{m1}", "review_fix:904"])
    by = {x["root"]: x for x in chaincap.lineages(p.db, since)}
    assert {k: by[fr][k] for k in ("hops", "fix_hops", "fix_usd", "cont_hops", "cont_usd", "usd", "tip")} == \
        {"hops": 2, "fix_hops": 2, "fix_usd": 0.75, "cont_hops": 0, "cont_usd": 0.0, "usd": 2.75, "tip": f2}
    assert {k: by[cr][k] for k in ("hops", "fix_hops", "fix_usd", "cont_hops", "cont_usd", "usd", "tip")} == \
        {"hops": 3, "fix_hops": 0, "fix_usd": 0.0, "cont_hops": 3, "cont_usd": 3.25, "usd": 6.25, "tip": c3}
    assert {k: by[mr][k] for k in ("hops", "fix_hops", "fix_usd", "cont_hops", "cont_usd", "usd", "tip")} == \
        {"hops": 2, "fix_hops": 1, "fix_usd": 0.5, "cont_hops": 1, "cont_usd": 2.0, "usd": 6.5, "tip": m2}
    line = chaincap.audit_line(p.db, since)
    assert line.startswith("continue chains: 3 lineage(s), 7 hops, $15.50 with their first tasks; "
                           "continuations: 4 hops in 2 lineage(s), $5.25; "
                           "review fixes (also in the review-loop line): 3 hops in 2 lineage(s), $1.25; "), line
    assert f"#{fr} fix lineage: 2 hops (2 review fixes) to #{f2}" in line
    assert f"#{cr} split lineage: 3 hops to #{c3}" in line
    assert f"#{mr} mixed lineage: 2 hops (1 review fix) to #{m2}" in line
    # The 'tasks continued' line lists continuations; review fixes are one separate figure.
    assert [t["id"] for t in audit.continued_tasks(p.db, since)] == [m1, c3]
    assert sorted(t["id"] for t in audit.continued_tasks(p.db, since, fixes=True)) == sorted([f1, f2, m2])
    out = "\n".join(audit.lines(p.db))
    assert "tasks continued from an earlier one: 2 (+ 3 review fixes, $1.25, counted in the review-loop line): " \
           f"#{m1} continue: mixed lineage continues #{mr}" in out, out
    assert set(audit.report(p.db)) >= {"continued_tasks", "review_fix_tasks"}


def test_review_fix_kind_reads_labels_and_falls_back_to_the_fix_title():
    from ttp import chaincap
    fix = chaincap.is_review_fix
    assert fix({"title": "anything", "labels": '["continues:1", "review_fix:2"]'})
    assert fix({"title": "  fix review #12: x", "labels": '["continues:1"]'})
    assert not fix({"title": "continue: x", "labels": '["continues:1", "review_fix:2"]'})
    assert not fix({"title": "x", "labels": '["follows:1", "review_fix:2"]'})
    assert not fix({"title": "Fix reviewer docs", "labels": '["continues:1"]'})
    assert not fix({"title": "x", "labels": "not json"}) and not fix({"title": "x", "labels": '{"review_fix:1": 1}'})
    assert not fix({"title": None, "labels": None})


def _finish_with_followups(env, p, title, fups):
    """_finish_code whose done hand-off proposes `fups(tid)` (follow-ups that may name the task's id)."""
    def before(tid, head):
        (env["tmp"] / f"run-{tid}" / "result.json").write_text(json.dumps(
            {"status": "done", "summary": f"{title} works", "followups": fups(tid)}))
    return _finish_code(env, p, title, {f"{title.replace(' ', '_')}.py": 5}, before=before)


def _hops_of(p, tid):
    return p.db.q("SELECT * FROM tasks WHERE labels LIKE ?", (f'%"continues:{tid}"%',))


def test_a_finished_code_task_whose_only_continue_waits_on_its_landing_is_no_split_and_gets_its_review(env):
    """A done code task whose only `continue:` follow-up is a post-landing step (start_when landed:#<it>)
    is no split: the daemon queues its normal review, and queues no hop that would wait forever."""
    from ttp import chaincap
    p = make(env)
    tid, branch, done, reviews = _finish_with_followups(env, p, "installer fix", lambda tid: [
        {"title": "continue: deploy and verify", "spec": "Run the installer on the hosts.",
         "start_when": f"landed:#{tid}"}])
    assert not chaincap.split_head(p.db.task(tid))
    (rev,) = reviews
    assert rev["title"] == f"Review #{tid}: installer fix" and branch in rev["spec"]
    assert "Split at head" not in done["text"] and not _hops_of(p, tid)
    # A wait on another task's landing is no post-landing step of this one.
    assert not chaincap.post_landing({"title": "continue: deploy", "start_when": f"landed:#{tid}0"}, tid)
    assert chaincap.post_landing({"title": "continue: deploy", "retry_when": f"ttp landed --task {tid}"}, tid)


def test_a_wording_only_post_landing_continue_is_no_split(env):
    """No wait field, but the follow-up says it runs once this task lands and names a live step: no split,
    a review, and no daemon hop started before the landing."""
    from ttp import chaincap
    p = make(env)
    tid, _, done, reviews = _finish_with_followups(env, p, "service unit", lambda tid: [
        {"title": "continue: roll it out", "spec": "After it lands, deploy and verify the live install."}])
    assert not chaincap.split_head(p.db.task(tid)) and len(reviews) == 1 and not _hops_of(p, tid)
    assert chaincap.post_landing({"title": "continue: live check", "spec": f"Once #{tid} lands, verify it on each "
                                                                         f"host."}, tid)
    # Landing words without a live step, a live step without landing words, or another task's landing: no.
    assert not chaincap.post_landing({"title": "continue: docs", "spec": "After it lands, update the README."}, tid)
    assert not chaincap.post_landing({"title": "continue: install docs", "spec": "Write the install section."}, tid)
    assert not chaincap.post_landing({"title": "continue: deploy", "spec": f"Once #{tid + 1} lands, deploy."}, tid)


def test_a_continue_titled_to_land_push_or_fix_the_change_is_still_a_split(env):
    from ttp import chaincap
    for title in ("continue: land the change, then deploy", "continue: push it and roll out",
                  "continue: fix the review findings", "continue: finish the installer and install it"):
        assert not chaincap.post_landing({"title": title, "spec": "After it lands, deploy it."}, 7), title
    assert chaincap.post_landing({"title": "continue: deploy the fix", "spec": "After it lands, deploy."}, 7)
    p = make(env)
    tid, branch, _, reviews = _finish_with_followups(env, p, "parser fix", lambda tid: [
        {"title": "continue: push the change, then deploy it", "spec": "After it lands, deploy it on each host."}])
    assert chaincap.split_head(p.db.task(tid)) == _git_out(env["repo"], "rev-parse", branch).strip()
    assert not reviews


def test_a_same_branch_continuation_is_still_a_split_beside_a_post_landing_one(env):
    """A real `continue:` hop keeps today's split (no review until the last hop), also when a post-landing
    follow-up rides along."""
    from ttp import chaincap
    p = make(env)
    tid, branch, _, reviews = _finish_with_followups(env, p, "lexer", lambda tid: [
        {"title": "continue: tests for the lexer", "spec": "Add the tests on this branch, then hand off."},
        {"title": "continue: deploy", "spec": "Deploy it.", "start_when": f"landed:#{tid}"}])
    assert chaincap.split_head(p.db.task(tid)) == _git_out(env["repo"], "rev-parse", branch).strip()
    assert not reviews
    assert not chaincap.post_landing({"title": "continue: tests for the lexer",
                                      "spec": "Add the tests on this branch, then hand off."}, tid)


def _split_at(p, tid, head):
    """Label an _unreviewed task a split at `head`, keeping its age (the sweep skips fresh tasks)."""
    when = p.db.task(tid)["updated"]
    p.db.update_task(tid, labels=[f"split:{head}"])
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (when, tid))


def test_the_unreviewed_sweep_reviews_a_split_head_an_open_task_waits_on_to_land(env):
    """A split head with no review that an open task waits on landing (its start_when, or a waiting
    hand-off's retry_when) gets exactly one review from the sweep, none beside it later; a split head
    a real hop carries gets none."""
    from ttp import daemon as dm
    p = make(env)
    tid, _, head = _unreviewed(env, p, "stuck head")
    _split_at(p, tid, head)
    p.db.add_task("deploy it", "s", kind="code", origin="coordinator",
                  labels=[f"continues:{tid}", f"start_when:landed:#{tid}"])
    d = dm.Daemon(p.base)
    assert d.sweep_unreviewed(force=True) == [tid]
    (rev,) = _daemon_reviews(p, tid)
    assert rev["title"] == f"Review #{tid}: stuck head"
    for st in ("queued", "running", "done"):
        p.db.update_task(rev["id"], status=st)
        assert d.sweep_unreviewed(force=True) == [], st
    assert len(_daemon_reviews(p, tid)) == 1
    # A waiting hand-off's retry_when names the landing.
    t2, _, h2 = _unreviewed(env, p, "stuck head two")
    _split_at(p, t2, h2)
    w = p.db.add_task("live check", "s", kind="code", origin="coordinator", labels=[f"continues:{t2}"])
    p.db.update_task(w, status="queued", result=json.dumps({"status": "waiting", "retry_when": f"landed:#{t2}"}))
    assert d.sweep_unreviewed(force=True) == [t2] and len(_daemon_reviews(p, t2)) == 1
    # A real hop carries its split head: no review.
    t3, _, h3 = _unreviewed(env, p, "real split")
    _split_at(p, t3, h3)
    p.db.add_task("continue: tests", "s", kind="code", origin="daemon", labels=[f"continues:{t3}"])
    assert d.sweep_unreviewed(force=True) == [] and not _daemon_reviews(p, t3)
