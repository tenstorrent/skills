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


def test_post_landing_wording_needs_this_task_landing_and_a_live_step_in_that_sentence():
    """Landing talk about something else ('when ... landing', 'after the push queue reports landing') or a
    live-step word elsewhere in the spec or in a file name is a real continuation, still a split."""
    from ttp import chaincap
    for fup in ({"title": "continue: part 2, the sweep safety net",
                 "spec": "When a split head's landing is awaited by an open task, queue a review. Then run ttp setup "
                         "to install it locally and test."},
                {"title": "continue: wire the landing check",
                 "spec": "after the push queue reports landing, the daemon marks it; update install.sh"},
                {"title": "continue: part 2", "spec": "After it lands, the sweep reviews it. Then install it."},
                {"title": "continue: part 2", "spec": "After it lands, update install.sh and the docs."},
                {"title": "continue: part 2", "spec": "When it lands, deploy it."}):
        assert not chaincap.post_landing(fup, 7), fup
    for fup in ({"title": "continue: deploy", "spec": "Do it after this change is pushed."},
                {"title": "continue: part 2", "spec": "Write docs. Once #7 has landed, upgrade each box."},
                {"title": "continue: part 2", "spec": "After the change reaches the push branch, roll it out."}):
        assert chaincap.post_landing(fup, 7), fup


def test_a_landed_wait_on_the_own_task_anywhere_in_a_follow_up_is_decisive_for_post_landing():
    """`landed:#<own id>` or `ttp landed --task <own id>` in the title or spec makes a follow-up post-landing,
    live step or not, also under a title that carries the change; another task's landing does not."""
    from ttp import chaincap
    for fup in ({"title": "continue: part 2", "spec": "Continues #6; wait landed:#7. One target per task."},
                {"title": "continue: part 2 (landed:#7)", "spec": "Update the changelog."},
                {"title": "continue: part 2", "spec": "Start when `ttp landed --task 7` passes, then check it."},
                {"title": "continue: push it", "spec": "Then wait for landed: #7 and update the notes."}):
        assert chaincap.post_landing(fup, 7), fup
    for fup in ({"title": "continue: part 2", "spec": "Continues #6; wait landed:#70. One target per task."},
                {"title": "continue: part 2", "spec": "Rebase once landed:#8 is in, then add the tests."},
                {"title": "continue: part 2", "spec": "Like `ttp landed --task 70`, add a probe."}):
        assert not chaincap.post_landing(fup, 7), fup


def test_after_landing_wording_takes_a_landed_ref_and_a_short_noun_phrase():
    from ttp import chaincap
    for fup in ({"title": "continue: deploy", "spec": "After landed:#7, deploy by the rollout command on target A."},
                {"title": "continue: part 2", "spec": "Once the cutover mask fix lands, restart the service."},
                {"title": "continue: part 2", "spec": "After the parser lands, deploy it."},
                {"title": "continue: part 2", "spec": "Write docs. Once the new unit has landed, roll out to target A."}):
        assert chaincap.post_landing(fup, 7), fup
    for fup in ({"title": "continue: part 2", "spec": "Describe the landscape after the release, then deploy it."},
                {"title": "continue: part 2", "spec": "After landed:#8, deploy it on target A."},
                {"title": "continue: part 2", "spec": "After the fix in #12 lands, deploy it."},
                {"title": "continue: part 2", "spec": "After the release of the new big shiny parser lands, deploy."},
                {"title": "continue: part 2", "spec": "After the parser lands, update the docs."}):
        assert not chaincap.post_landing(fup, 7), fup
    assert chaincap.live_step({"title": "continue: restart the service"})
    assert chaincap.live_step({"spec": "the one live step left"}) and not chaincap.live_step({"spec": "docs"})


def _no_hop_waits_on_its_parent(p):
    from ttp import chaincap
    for t in p.db.q("SELECT * FROM tasks"):
        if (prev := chaincap.prev_of(t)) is not None and chaincap.auto_continued(p.db, prev):
            assert not chaincap.waits_landing(t["spec"], prev), t["spec"]


def test_post_landing_live_step_continues_are_proposed_as_deploys_never_auto_hopped(env):
    """The two shapes that were hopped: a follow-up that waits on its own parent's landing in its spec, and one
    that deploys 'after landed:#<parent>'. Neither is queued or makes a split; the coordinator gets them NOT
    queued, with one deploy task per target waiting on the parent's landing."""
    from ttp import chaincap
    shapes = [lambda tid: [{"title": "continue: roll out to each target",
                            "spec": f"Continues #{tid - 1}; wait landed:#{tid}. One target per task through the "
                                    f"rollout script."}],
              lambda tid: [{"title": "continue: deploy to target A",
                            "spec": f"After landed:#{tid}, deploy by the rollout start command on target A."}]]
    p = make(env)
    for i, fups in enumerate(shapes):
        tid, _, done, reviews = _finish_with_followups(env, p, f"unit change {i}", fups)
        assert not chaincap.split_head(p.db.task(tid)) and len(reviews) == 1, i
        assert not _hops_of(p, tid) and not p.db.q("SELECT 1 FROM tasks WHERE parent=?", (tid,))
        assert not p.db.q("SELECT 1 FROM events WHERE kind=? AND task=?", (chaincap.AUTO_EVENT, tid))
        want = f"one deploy task per target with start_when landed:#{tid}"
        assert "NOT queued by the daemon" in done["text"] and want in done["text"], done["text"]
        (prop,) = p.db.q("SELECT * FROM events WHERE kind='followup_proposed' AND task=?", (tid,))
        assert "NOT queued by the daemon" in prop["text"] and want in prop["text"] and prop["status"] == "queued"
    _no_hop_waits_on_its_parent(p)


def test_post_landing_continues_without_a_live_step_and_plain_continues_keep_their_paths(env):
    from ttp import chaincap
    p = make(env)
    # Post-landing, no live step: no hop, no split, a review, and the plain NOT-queued words.
    tid, _, done, reviews = _finish_with_followups(env, p, "notes change", lambda tid: [
        {"title": "continue: changelog", "spec": f"Wait landed:#{tid}, then update the changelog."}])
    assert not chaincap.split_head(p.db.task(tid)) and len(reviews) == 1 and not _hops_of(p, tid)
    assert f"add it with continues #{tid} or drop it" in done["text"] and "deploy task" not in done["text"]
    # Not post-landing: today's split and its daemon hop, which waits on nothing.
    tid, branch, done, reviews = _finish_with_followups(env, p, "lexer part", lambda tid: [
        {"title": "continue: tests for the lexer", "spec": "Add the tests on this branch, then deploy nothing."}])
    assert chaincap.split_head(p.db.task(tid)) and not reviews
    (hop,) = _hops_of(p, tid)
    assert p.db.q("SELECT 1 FROM events WHERE kind=? AND task=?", (chaincap.AUTO_EVENT, tid))
    assert f"Continue hop #{hop['id']} queued by the daemon" in done["text"]
    _no_hop_waits_on_its_parent(p)


def test_a_deploy_task_waiting_on_the_landing_takes_up_a_post_landing_continue(env):
    """check_unqueued_continues counts a later task that waits on the parent's landing (what deploy_proposal
    asks for), so following the proposal raises no continue_unqueued; with none, its words repeat it."""
    import time
    from ttp import daemon as dmod
    from ttp.daemon import Daemon
    p = make(env)
    ids = []
    for i in range(2):
        tid, *_ = _finish_with_followups(env, p, f"svc change {i}", lambda tid: [
            {"title": "continue: deploy", "spec": f"After landed:#{tid}, deploy to target A."}])
        ids.append(tid)
    p.db.add_task("deploy to target A", "Deploy.", kind="code", origin="coordinator",
                  labels=[f"start_when:landed:#{ids[0]}"])
    old = time.time() - dmod.CONTINUE_UNQUEUED_AFTER_S - 60
    p.db.x("UPDATE events SET ts=? WHERE kind='task_done'", (old,))
    d = Daemon(p.base)
    d._continues_checked = 0
    d.check_unqueued_continues()
    rows = p.db.q("SELECT * FROM events WHERE kind=?", (dmod.CONTINUE_UNQUEUED,))
    assert [r["task"] for r in rows] == [ids[1]]
    assert f"one deploy task per target with start_when landed:#{ids[1]}" in rows[0]["text"]


def test_a_landing_waiter_never_takes_up_a_split_hop(env):
    """A split's `continue:` hops are no post-landing step: a later task that only waits on the parent's
    landing (a dependent, not a hop) takes none of them up, so continue_unqueued still fires (the split
    head never lands without its hop, and the dependent would wait forever)."""
    import time
    from ttp import chaincap
    from ttp import daemon as dmod
    from ttp.daemon import Daemon
    p = make(env)
    tid, *_ = _finish_with_followups(env, p, "split change", lambda tid: [
        {"title": "continue: finish the parser", "spec": "Carry on with the parser."},
        {"title": "continue: finish the writer", "spec": "Carry on with the writer."}])
    assert not _hops_of(p, tid) and chaincap.split_head(p.db.task(tid))
    p.db.add_task("use the new parser", "Use it.", kind="code", origin="coordinator",
                  labels=[f"start_when:landed:#{tid}"])
    p.db.x("UPDATE events SET ts=? WHERE kind='task_done'", (time.time() - dmod.CONTINUE_UNQUEUED_AFTER_S - 60,))
    d = Daemon(p.base)
    d._continues_checked = 0
    d.check_unqueued_continues()
    rows = p.db.q("SELECT * FROM events WHERE kind=?", (dmod.CONTINUE_UNQUEUED,))
    assert [r["task"] for r in rows] == [tid]
    assert f"continues #{tid}" in rows[0]["text"] and "'continue: finish the parser'" in rows[0]["text"]
    # Mixed: a landing waiter takes up the post-landing deploy, never the split hop beside it.
    mix, *_ = _finish_with_followups(env, p, "mixed change", lambda tid: [
        {"title": "continue: deploy", "spec": f"After landed:#{tid}, deploy to target A."},
        {"title": "continue: finish the writer", "spec": "Carry on with the writer."}])
    p.db.add_task("deploy to target A", "Deploy.", kind="code", origin="coordinator",
                  labels=[f"start_when:landed:#{mix}"])
    p.db.x("UPDATE events SET ts=? WHERE kind='task_done'", (time.time() - dmod.CONTINUE_UNQUEUED_AFTER_S - 60,))
    d._continues_checked = 0
    d.check_unqueued_continues()
    (row,) = p.db.q("SELECT * FROM events WHERE kind=? AND task=?", (dmod.CONTINUE_UNQUEUED, mix))
    assert "'continue: finish the writer'" in row["text"] and f"continues #{mix}" in row["text"]


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


def test_a_continue_that_asks_measures_or_waits_on_its_landing_is_no_split_and_gets_its_review_now(env):
    """A done code task whose only `continue:` follow-up is no code hop (a question or measurement, another
    kind, or one that says it runs after this task lands without a live step) is no split: one review now,
    no daemon hop. A review the coordinator queued ahead stays the one review."""
    from ttp import chaincap
    p = make(env)
    cases = [
        ("digest cache", lambda tid: [{"title": f"continue: measure coordinator write_per_turn after #{tid} lands",
                                       "spec": "Read the turn stats a day later."}]),
        ("stats line", lambda tid: [{"title": "continue: stats line, part 2", "kind": "question",
                                     "spec": "Which tasks read the line?"}]),
        ("readme pass", lambda tid: [{"title": "continue: docs", "spec": "After it lands, update the README."}]),
        ("cache hit", lambda tid: [{"title": "continue: check whether the cache hit rate rose", "spec": "Read it."}]),
    ]
    for title, fups in cases:
        tid, branch, done, reviews = _finish_with_followups(env, p, title, fups)
        assert not chaincap.split_head(p.db.task(tid)), title
        assert [r["title"] for r in reviews] == [f"Review #{tid}: {title}"], title
        assert "Split at head" not in done["text"] and not _hops_of(p, tid), title
    # A real code hop is still a split, with no review until its chain's last hop.
    tid, branch, done, reviews = _finish_with_followups(env, p, "lexer two", lambda tid: [
        {"title": "continue: lexer two, part 2", "kind": "code", "spec": "Add the tests on this branch."}])
    assert chaincap.split_head(p.db.task(tid)) and not reviews and "Split at head" in done["text"]
    # Another task's landing, or a title that lands the change, keeps it a code hop.
    assert chaincap.code_hop({"title": "continue: rest", "spec": "After #99999 lands, add the tests."}, 7)
    assert chaincap.code_hop({"title": "continue: push it after it lands", "spec": "x"}, 7)
    assert not chaincap.code_hop({"title": "continue: rest", "spec": "Once #7 has landed, add the tests."}, 7)
    assert not chaincap.code_hop({"title": "follow up: rest", "spec": "x"}, 7)
    assert not chaincap.code_hop({"title": "continue: why did it slow down", "spec": "x"}, 7)

    # The coordinator queued the review ahead: still exactly that one.
    def ahead(tid, head):
        p.db.add_task(f"Review #{tid}: planner", "spec", kind="review", origin="coordinator",
                      depends_on=[tid], labels=[f"auto_review:{tid}"])
        (env["tmp"] / f"run-{tid}" / "result.json").write_text(json.dumps(
            {"status": "done", "summary": "planner works",
             "followups": [{"title": "continue: measure the planner", "spec": "Time it."}]}))
    tid, _, _, reviews = _finish_code(env, p, "planner", {"planner.py": 5}, before=ahead)
    assert not chaincap.split_head(p.db.task(tid))
    assert [r["origin"] for r in reviews] == ["coordinator"]


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


def test_hop_stall_counts_the_user_a_start_when_a_block_and_a_long_deferral_only():
    """A hop that runs or starts soon carries its split head (today's rule); one that waits on the user,
    a start_when, a block or a deferral past HOP_WAIT_S does not."""
    import time
    from ttp import chaincap
    now = time.time()

    def hop(status="queued", result=None, labels=(), not_before=None, reason=None):
        return {"status": status, "result": json.dumps(result) if result else None, "labels": json.dumps(
            ["continues:1", *labels]), "not_before": not_before, "blocked_reason": reason}
    for t in (hop(), hop("running", {"status": "waiting", "wait_kind": "user"}), hop("done"),
              hop(result={"status": "waiting", "wait_kind": "self", "retry_when": "test -e x"}),
              hop(result={"status": "waiting", "wait_kind": "external"}, not_before=now + 600),
              hop(not_before=now + chaincap.HOP_WAIT_S - 60), hop(labels=["start_when:now"]), None):
        assert chaincap.hop_stall(t, now) == "", t
    assert "waits on the user (sign-in)" in chaincap.hop_stall(
        hop(result={"status": "waiting", "wait_kind": "user", "waiting_for": "sign-in"}), now)
    assert "waits on the user" in chaincap.hop_stall(hop(result={"status": "waiting", "wait_kind": "user-action"}), now)
    assert "start_when (signed in)" in chaincap.hop_stall(
        hop(labels=["start_when:tool status", "start_why:signed in"]), now)
    assert "is blocked (needs a key)" in chaincap.hop_stall(hop("blocked", reason="needs a key"), now)
    assert "is deferred 3.0 h" in chaincap.hop_stall(hop(not_before=now + 3 * 3600), now)
    assert "is deferred" in chaincap.hop_stall(hop(labels=[f"start_after:{now + 3 * 3600:.0f}"]), now)


def _hop_on(env, p, tid, title="continue: live step", commit=True):
    """A code hop of split task `tid` on its own branch from the split head, with one commit when `commit`."""
    from ttp import worktree
    hid = p.db.add_task(title, "Carry on.", kind="code", origin="coordinator", labels=[f"continues:{tid}"])
    path, branch = worktree.ensure(p, p.db.task(hid))
    if commit:
        (path / f"{hid}.py").write_text("more\n")
        _git_out(path, "add", ".")
        _git_out(path, *_IDENT, "commit", "-qm", title)
    p.db.update_task(hid, branch=branch)
    return hid, _git_out(path, "rev-parse", "HEAD").strip()


def test_a_split_review_moved_to_a_hop_that_waits_on_the_user_reviews_the_delivered_head_now(env):
    """The reported case, with generic names: a split task's review moved onto its `continue:` hop (task_add's
    dependents take-over) while the hop waits on the user's sign-in. The review waits on the split task again
    and covers its delivered head only; a hop that runs normally keeps it (one review of the chain), and a
    review of the hop itself stays on the hop."""
    from ttp import chaincap
    from ttp import daemon as dm
    p = make(env)
    tid, _, head = _unreviewed(env, p, "split change")
    _split_at(p, tid, head)
    hop, _ = _hop_on(env, p, tid, commit=False)
    rev = p.db.add_task(f"Review #{tid}: split change", "Review it.", kind="review", origin="daemon",
                        depends_on=[hop], labels=[f"auto_review:{tid}"])
    own = p.db.add_task(f"Review #{hop}: continue: live step", "Review it.", kind="review", origin="coordinator",
                        depends_on=[hop])
    d = dm.Daemon(p.base)
    assert d.sweep_unreviewed(force=True) == []
    assert json.loads(p.db.task(rev)["depends_on"]) == [hop]   # a running hop carries the split head
    p.db.update_task(hop, result=json.dumps({"status": "waiting", "wait_kind": "user", "waiting_for": "sign-in",
                                             "retry_when": "tool status"}))
    assert d.sweep_unreviewed(force=True) == []   # the moved review is the split head's one review
    r = p.db.task(rev)
    assert json.loads(r["depends_on"]) == [tid] and f"{chaincap.SPLIT_REVIEW_LABEL}{head}" in r["labels"]
    assert f"#{hop} waits on the user (sign-in)" in r["spec"] and f"head {head[:12]} only" in r["spec"]
    assert json.loads(p.db.task(own)["depends_on"]) == [hop]
    assert not p.db.unmet_dependencies([p.db.task(rev)]).get(rev)   # ready now: the split task is done
    assert d.sweep_unreviewed(force=True) == [] and json.loads(p.db.task(rev)["depends_on"]) == [tid]
    assert [e["task"] for e in p.db.q("SELECT task FROM events WHERE kind='observation' AND text LIKE ?",
                                      (f"Review #{rev} of split task #{tid}%",))] == [rev]


def test_a_split_head_with_no_review_whose_hop_stalls_gets_one_now_and_a_hop_starting_soon_does_not(env):
    """A split head no review covers, whose only hop waits on a start_when or a long deferral, gets its review
    from the sweep, labelled and worded as the split head's own; one whose hop is deferred briefly, or has no
    commits past the split head while waiting on the user, gets none."""
    import time
    from ttp import chaincap
    from ttp import daemon as dm
    p = make(env)
    d = dm.Daemon(p.base)
    for title, labels, not_before in (("waits on sign-in", ["start_when:tool status"], None),
                                      ("deferred long", [], time.time() + 3 * 3600)):
        tid, _, head = _unreviewed(env, p, title)
        _split_at(p, tid, head)
        hop = p.db.add_task("continue: rest", "s", kind="code", origin="coordinator",
                            labels=[f"continues:{tid}", *labels], not_before=not_before)
        assert d.sweep_unreviewed(force=True) == [tid], title
        (rev,) = _daemon_reviews(p, tid)
        assert f"{chaincap.SPLIT_REVIEW_LABEL}{head}" in rev["labels"], title
        assert f"Its continuation #{hop} " in rev["spec"] and "own later commits get their own review" in rev["spec"]
    tid, _, head = _unreviewed(env, p, "deferred briefly")
    _split_at(p, tid, head)
    p.db.add_task("continue: rest", "s", kind="code", origin="coordinator", labels=[f"continues:{tid}"],
                  not_before=time.time() + 600)
    assert d.sweep_unreviewed(force=True) == [] and not _daemon_reviews(p, tid)
    # A hop waiting on the user with no commit of its own past the split head: only the split head is reviewed.
    tid, _, head = _unreviewed(env, p, "waits on the user")
    _split_at(p, tid, head)
    hop, hop_head = _hop_on(env, p, tid, commit=False)
    assert hop_head == head
    p.db.update_task(hop, status="queued", result=json.dumps({"status": "waiting", "wait_kind": "user"}))
    assert d.sweep_unreviewed(force=True) == [tid] and not _daemon_reviews(p, hop)


def test_a_done_hop_with_no_commits_past_an_unreviewed_split_head_still_gets_a_review(env):
    """A done hop with no commits past its split head, whose own finish-time review was never queued (a review
    cap, a failed diff read, a crash), is reviewed by the sweep: its split task counts it as the carrier, so
    skipping it too would leave the chain unreviewed. With a split review of that head (queued, running or
    passed) it is not reviewed twice."""
    from ttp import chaincap
    from ttp import daemon as dm
    p = make(env)
    d = dm.Daemon(p.base)
    for same_branch in (True, False):
        tid, branch, head = _unreviewed(env, p, f"split change {same_branch}")
        _split_at(p, tid, head)
        hop, hop_head = _hop_on(env, p, tid, commit=False)
        if same_branch:
            p.db.update_task(hop, branch=branch)
        assert hop_head == head
        p.db.update_task(hop, status="done", result=json.dumps({"status": "done", "summary": "nothing new"}))
        when = time.time() - 600
        p.db.x("UPDATE tasks SET updated=? WHERE id=?", (when, hop))
        _ran(p, hop, when)
        assert d.sweep_unreviewed(force=True) == [hop], same_branch
        assert len(_daemon_reviews(p, hop)) == 1 and not _daemon_reviews(p, tid)
    for st in ("queued", "running", "done"):
        tid, _, head = _unreviewed(env, p, f"reviewed split {st}")
        _split_at(p, tid, head)
        hop, _ = _hop_on(env, p, tid, commit=False)
        p.db.update_task(hop, status="done", result=json.dumps({"status": "done", "summary": "nothing new"}))
        when = time.time() - 600
        p.db.x("UPDATE tasks SET updated=? WHERE id=?", (when, hop))
        _ran(p, hop, when)
        rev = p.db.add_task(f"Review #{tid}", "Review it.", kind="review", origin="daemon", depends_on=[tid],
                            labels=[f"auto_review:{tid}", f"{chaincap.SPLIT_REVIEW_LABEL}{head}"])
        p.db.update_task(rev, status=st)
        assert d.sweep_unreviewed(force=True) == [], st
        assert not _daemon_reviews(p, hop), st


def test_the_hop_delivering_after_its_split_head_review_gets_a_review_of_only_its_new_commits(env):
    """Once the split head's own review ran, the hop's later delivery gets a review of only the commits after
    that head; while that review is still queued, it folds into the hop's review (one review of the chain)."""
    from ttp import chaincap
    from ttp import daemon as dm
    p = make(env)
    d = dm.Daemon(p.base)
    for ran in (True, False):
        tid, _, head = _unreviewed(env, p, f"split change {ran}")
        _split_at(p, tid, head)
        hop, hop_head = _hop_on(env, p, tid)
        rev = p.db.add_task(f"Review #{tid}: split change", "Review it.", kind="review", origin="daemon",
                            depends_on=[tid], labels=[f"auto_review:{tid}", f"{chaincap.SPLIT_REVIEW_LABEL}{head}"])
        if ran:
            p.db.update_task(rev, status="done")
        p.db.update_task(hop, status="done", result=json.dumps({"status": "done", "summary": "measured"}))
        got, added = d._auto_review(p.db.task(hop), "measured")
        if ran:
            assert added and got != rev
            spec = p.db.task(got)["spec"]
            assert f"git log -p {head}..{hop_head}" in spec and f"#{rev}" in spec
            assert "review the chain's whole change as one" not in spec
        else:
            assert (got, added) == (rev, False)
            r = p.db.task(rev)
            assert json.loads(r["depends_on"]) == [hop] and chaincap.SPLIT_REVIEW_LABEL not in r["labels"]
            assert f"#{hop} delivered the rest of its split chain" in r["spec"]


def _two_hop_split(env, p, title):
    """Split task S1 -> its hop H1 (done, itself a split, one commit) -> H2, H1's hop with no commit yet.
    (S1, H1, H1's head, H2)."""
    tid, _, head = _unreviewed(env, p, title)
    _split_at(p, tid, head)
    h1, h1_head = _hop_on(env, p, tid)
    when = time.time() - 600
    p.db.update_task(h1, status="done", labels=[f"continues:{tid}", f"split:{h1_head}"],
                     result=json.dumps({"status": "done", "summary": "Split here: half done"}))
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (when, h1))
    _ran(p, h1, when)
    h2, _ = _hop_on(env, p, h1, commit=False)
    return tid, h1, h1_head, h2


def test_a_chain_review_of_an_earlier_split_task_is_released_when_a_later_hop_stalls(env):
    """S1 -> H1 (done split) -> H2 waiting on the user: the chain's review, titled or keyed for S1 and moved
    onto H2 by each hop in turn, waits on H1 again and reviews H1's head now; the sweep queues no second
    review of that head beside it."""
    from ttp import chaincap
    from ttp import daemon as dm
    p = make(env)
    d = dm.Daemon(p.base)
    for keyed in (True, False):
        s1, h1, h1_head, h2 = _two_hop_split(env, p, f"split change {keyed}")
        rev = p.db.add_task(f"Review #{s1}: split change", "Review it.", kind="review", origin="daemon",
                            depends_on=[h2], labels=[f"auto_review:{s1}"] if keyed else [])
        own = p.db.add_task(f"Review #{h2}: continue: live step", "Review it.", kind="review",
                            origin="coordinator", depends_on=[h2], labels=[f"auto_review:{h2}"])
        assert d.sweep_unreviewed(force=True) == [], keyed   # a running hop carries the chain
        assert json.loads(p.db.task(rev)["depends_on"]) == [h2]
        p.db.update_task(h2, result=json.dumps({"status": "waiting", "wait_kind": "user", "waiting_for": "sign-in",
                                                "retry_when": "tool status"}))
        for _ in range(2):
            assert d.sweep_unreviewed(force=True) == [], keyed
            r = p.db.task(rev)
            assert json.loads(r["depends_on"]) == [h1] and f"{chaincap.SPLIT_REVIEW_LABEL}{h1_head}" in r["labels"]
            assert f"#{h2} waits on the user (sign-in)" in r["spec"]
            assert f"head {h1_head[:12]} (the split chain #{s1} to #{h1}) only" in r["spec"]
            assert [x["id"] for x in p.db.q("SELECT id FROM tasks WHERE kind='review' AND id>?", (rev,))] == [own]
            assert json.loads(p.db.task(own)["depends_on"]) == [h2]   # H2's own review stays on H2
        assert not p.db.unmet_dependencies([p.db.task(rev)]).get(rev)
        p.db.update_task(rev, status="done")   # passed: the next round sees only its own chain
        p.db.update_task(own, status="cancelled")


def test_the_last_hop_of_a_three_hop_chain_delivering_after_the_released_review_reviews_only_its_commits(env):
    """After the S1 chain review was released to H1's head: once it ran, H2's delivery is reviewed for
    H1-head..H2-tip only; while it is still queued, it folds into H2's review (one review of the chain)."""
    from ttp import chaincap
    from ttp import daemon as dm
    from ttp import worktree
    p = make(env)
    d = dm.Daemon(p.base)
    for ran in (True, False):
        s1, h1, h1_head, h2 = _two_hop_split(env, p, f"split change {ran}")
        rev = p.db.add_task(f"Review #{s1}: split change", "Review it.", kind="review", origin="daemon",
                            depends_on=[h2], labels=[f"auto_review:{s1}"])
        p.db.update_task(h2, result=json.dumps({"status": "waiting", "wait_kind": "user", "retry_when": "x"}))
        assert d.sweep_unreviewed(force=True) == [] and json.loads(p.db.task(rev)["depends_on"]) == [h1]
        if ran:
            p.db.update_task(rev, status="done")
        path, _ = worktree.ensure(p, p.db.task(h2))
        (path / f"{h2}.py").write_text("rest\n")
        _git_out(path, "add", ".")
        _git_out(path, *_IDENT, "commit", "-qm", "rest")
        h2_head = _git_out(path, "rev-parse", "HEAD").strip()
        p.db.update_task(h2, status="done", result=json.dumps({"status": "done", "summary": "measured"}))
        got, added = d._auto_review(p.db.task(h2), "measured")
        if ran:
            assert added and got != rev
            spec = p.db.task(got)["spec"]
            assert f"git log -p {h1_head}..{h2_head}" in spec and f"#{rev}" in spec
            assert "review the chain's whole change as one" not in spec
        else:
            assert (got, added) == (rev, False)
            r = p.db.task(rev)
            assert json.loads(r["depends_on"]) == [h2] and chaincap.SPLIT_REVIEW_LABEL not in r["labels"]
            assert f"#{h2} delivered the rest of its split chain" in r["spec"]


def _failed_parent(p, d, raised, title="Port the parser"):
    """A coordinator code task queued at standard that failed; `raised`: its retry ran a tier up first."""
    import json
    t = p.db.add_task(title, "Port it.", kind="code", tier="standard", origin="coordinator", provider="claude")
    if raised:
        p.db.update_task(t, attempts=1, result=json.dumps({"status": "failed", "summary": "stuck"}))
        assert d._pick_effort(p.db.task(t), "claude")["by"] == "retry"
        assert (p.db.task(t)["tier"], p.db.task(t)["tier_source"]) == ("deep", "retry_tier_up")
    p.db.update_task(t, status="failed", attempts=2 if raised else 1,
                     result=json.dumps({"status": "failed", "summary": "stuck"}))
    return p.db.task(t)


def test_a_continue_hop_of_a_failed_parent_starts_at_its_queued_tier_not_a_retry_raised_one(env):
    """A reported case, replayed: the continue hop of a failed parent queued at standard ran at deep, both
    when the parent's retry had raised it to deep and when it failed at standard (the hop's first start
    counted as a retry of it). The hop now starts at the parent's queued tier (tier_source parent_copy),
    also a daemon hop of a done parent whose retry was raised; a failure of the hop itself still raises
    its retry a tier, and so does a parent's `needs_deep` hand-off."""
    import json
    from ttp import chaincap, coordinator as coord
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.jev = None
    for n, (raised, name) in enumerate(((True, "Port the lexer"), (False, "Tune scheduler queues"))):
        parent = _failed_parent(p, d, raised, name)
        assert chaincap.hop_tier(parent, {"title": "continue: port"}) == "standard"
        assert not coord.apply(p, [{"type": "task_add", "title": f"continue: {name.lower()}", "spec": "Go on.",
                                    "kind": "code", "continues": parent["id"], "force": True}])
        hop = p.db.one("SELECT * FROM tasks WHERE title=?", (f"continue: {name.lower()}",))
        assert (hop["tier"], hop["assigned_tier"], hop["tier_source"]) == ("standard", "standard", "parent_copy")
        d._pick_effort(hop, "claude")
        assert p.db.task(hop["id"])["tier"] == "standard", raised
        p.db.update_task(hop["id"], attempts=1, result=json.dumps({"status": "failed", "summary": "still stuck"}))
        assert d._pick_effort(p.db.task(hop["id"]), "claude")["by"] == "retry"
        hop = p.db.task(hop["id"])
        assert (hop["tier"], hop["assigned_tier"], hop["tier_source"]) == ("deep", "standard", "retry_tier_up")
    # A failed try that handed off needs_deep: its continuation's first start runs a tier up.
    asked = _failed_parent(p, d, False, "Hard part")
    p.db.update_task(asked["id"], result=json.dumps({"status": "failed", "needs_deep": "too hard at standard"}))
    assert not coord.apply(p, [{"type": "task_add", "title": "continue: hard part", "spec": "s", "kind": "code",
                                "continues": asked["id"], "force": True}])
    hop = p.db.one("SELECT * FROM tasks WHERE title='continue: hard part'")
    assert d._pick_effort(hop, "claude")["by"] == "retry" and p.db.task(hop["id"])["tier"] == "deep"
    # The daemon's own hop of a done parent whose retry ran at deep copies its queued tier, or a lower one.
    for title, fup, want in (("Split work", {"title": "continue: split work"}, ("standard", "parent_copy")),
                             ("Push work", {"title": "mechanical: push it"}, ("light", "hop_followup"))):
        t = p.db.add_task(title, "s", kind="code", tier="standard", origin="coordinator")
        p.db.update_task(t, tier="deep", tier_source="retry_tier_up", status="done")
        hop_id = p.db.add_task("placeholder", "s")   # auto_hop finds its hop by parent and title
        p.db.update_task(hop_id, status="cancelled")
        assert chaincap.hop_tier(p.db.task(t), fup) == want[0]
        monkey = coord.apply
        try:
            coord.apply = lambda p_, acts, **k: [] if p_.db.add_task(
                acts[0]["title"], "", kind="code", tier=acts[0]["tier"], parent=t, origin="coordinator") else ["x"]
            new_id, _ = chaincap.auto_hop(p, p.config(), p.db.task(t), fup, why="test")
        finally:
            coord.apply = monkey
        assert (p.db.task(new_id)["tier"], p.db.task(new_id)["tier_source"]) == want


def test_user_deep_chains_and_explicit_coordinator_tiers_keep_their_tier(env):
    """The user's deep stays deep along its continue chain; a tier the coordinator gives a continue or fix
    task wins over the hop and fix defaults; explicit model and effort pins carry over as before."""
    import json
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.jev = None
    assert not coord.apply(p, [{"type": "task_add", "title": "Hard redesign", "spec": "s", "kind": "code",
                                "tier": "deep", "user_deep": True, "model": "opus", "effort": "max"}])
    deep = p.db.one("SELECT * FROM tasks WHERE title='Hard redesign'")
    assert (deep["tier"], deep["tier_source"], deep["model"], deep["effort"]) == ("deep", "user_deep", "opus", "max")
    p.db.update_task(deep["id"], status="failed", attempts=1, result=json.dumps({"status": "failed"}))
    assert not coord.apply(p, [{"type": "task_add", "title": "continue: redesign", "spec": "s", "kind": "code",
                                "continues": deep["id"], "model": "opus", "effort": "max", "force": True}])
    hop = p.db.one("SELECT * FROM tasks WHERE title='continue: redesign'")
    assert (hop["tier"], hop["tier_source"], hop["model"], hop["effort"]) == ("deep", "parent_copy", "opus", "max")
    assert d._pick_effort(hop, "claude") is None and p.db.task(hop["id"])["tier"] == "deep"
    # Explicit coordinator tiers on a continue task and on a fix it queues by title.
    old = p.db.add_task("Old try", "s", kind="code", tier="deep", origin="coordinator")
    p.db.update_task(old, status="failed", attempts=1, result=json.dumps({"status": "failed"}))
    assert not coord.apply(p, [{"type": "task_add", "title": "Redo it at standard", "spec": "s", "kind": "code",
                                "tier": "standard", "continues": old, "force": True},
                               {"type": "task_add", "title": "Fix review #77: by hand", "spec": "s", "kind": "code",
                                "tier": "deep"}])
    for title, tier in (("Redo it at standard", "standard"), ("Fix review #77: by hand", "deep")):
        t = p.db.one("SELECT * FROM tasks WHERE title=?", (title,))
        assert (t["tier"], t["tier_source"]) == (tier, "task_add"), title


def test_task_show_names_why_a_task_has_its_tier_and_old_records_still_load(env, tmp_path):
    """`ttp task show` prints the tier's source (and the queued tier when a retry raised it); a task
    database from before the record gains the columns, and its tasks load, show and hop as before."""
    import sqlite3
    from ttp import chaincap
    from ttp.cli import show_task
    from ttp.db import DB, TIER_SOURCES
    p = make(env)
    t = p.db.add_task("Raised", "s", kind="code", tier="standard", origin="coordinator")
    p.db.update_task(t, tier="deep", tier_source="retry_tier_up")
    assert "tier: deep (retry_tier_up, queued at standard)" in show_task(p.db.task(t))
    q = p.db.add_task("Plain", "s", kind="question")
    assert "tier: standard (default)\n" in show_task(p.db.task(q)) + "\n"
    assert {"task_add", "parent_copy", "retry_tier_up", "review_diff", "fix_review_default",
            "user_deep"} <= set(TIER_SOURCES)
    path = tmp_path / "old.db"
    DB(path).conn.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE tasks DROP COLUMN tier_source")
    con.execute("ALTER TABLE tasks DROP COLUMN assigned_tier")
    con.execute("INSERT INTO tasks(title, kind, tier) VALUES('legacy', 'code', 'deep')")
    con.commit()
    con.close()
    old = DB(path).task(1)
    assert old["tier_source"] is None and old["assigned_tier"] is None
    assert "tier: deep\n" in show_task(old) + "\n" and "(" not in show_task(old).splitlines()[1]
    assert chaincap.hop_tier(old, {"title": "continue: x"}) == "deep"


def test_a_continue_over_a_soft_cap_with_a_reason_passes_in_the_same_action_and_hard_caps_hold(env):
    """Plan #4141: every continue refusal passed on the next turn once a `reason` came, so each cost a
    turn. A reason sent with the first task_add passes a soft cap (stalls, review area) at once and is
    recorded with the cap it lifted; one without it is told the field to add; max_usd and max_hops
    stay hard whatever the reason."""
    import json
    p = make(env)
    from ttp import chaincap
    from ttp import coordinator as coord
    why = "new evidence: the header parser was the slow step, a different approach caches it"
    _, _, tip = _chain(p, 2)   # two done hops with no progress: the stall cap
    act = {"type": "task_add", "title": "continue: parser, next", "spec": "carry on", "kind": "code", "continues": tip}
    assert "chains.max_stalls is 2" in chaincap.refusal(p, p.config(), chaincap.chain(p.db, tip))
    assert coord.apply(p, [dict(act, reason=why)]) == []
    (new,) = p.db.q("SELECT * FROM tasks WHERE parent=?", (tip,))
    (ev,) = p.db.q("SELECT * FROM events WHERE kind=?", (chaincap.OVERRIDE_EVENT,))
    data = json.loads(ev["data"])
    assert (data["cap"], data["reason"], ev["task"]) == ("max_stalls", why, tip)
    assert not p.db.q("SELECT 1 FROM events WHERE kind=?", (chaincap.CAP_EVENT,)), "no extra turn"
    # No reason: refused, naming the field to add and the words it needs.
    _, _, tip2 = _chain(p, 2)
    (bare,) = coord.apply(p, [dict(act, title="continue: lexer", continues=tip2)])
    assert "refused" in bare and "it has no `reason`" in bare and "add a `reason` field" in bare, bare
    assert "new evidence, a different approach or a narrower scope" in bare
    for wording in ("narrower scope: only the header parser, not the body",
                    "new evidence: the profile shows the header is the slow step"):
        assert chaincap.reason_ok(wording), wording
    # Over max_usd (or max_hops) a valid reason is still refused, and the event says no reason lifts it.
    ids = _chain(p, 1)
    p.db.x("UPDATE tasks SET spent_usd=25 WHERE id IN (?,?)", ids)
    (over,) = coord.apply(p, [dict(act, title="continue: emitter", continues=ids[-1], reason=why)])
    assert "chains.max_usd is $40" in over and "chains.max_usd is hard" in over, over
    assert not p.db.q("SELECT id FROM tasks WHERE parent=?", (ids[-1],))
    cap = p.db.one("SELECT * FROM events WHERE kind=? AND task=?", (chaincap.CAP_EVENT, ids[-1]))
    assert "no `reason` lifts it" in cap["text"] and json.loads(cap["data"])["cap"] == "max_usd"
    assert len(p.db.q("SELECT 1 FROM events WHERE kind=?", (chaincap.OVERRIDE_EVENT,))) == 1
    p.set_config("chains.max_hops", 2)
    ids = _chain(p, 2)
    (hops,) = coord.apply(p, [dict(act, title="continue: linker", continues=ids[-1], reason=why)])
    assert "chains.max_hops is hard" in hops, hops


def _harness_done(env, p, title, fup, metrics=None):
    """A harness task that handed off done with the one follow-up `fup` (and `metrics`: its progress)."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task(title, f"spec of {title}", kind="harness", tier="deep", origin="coordinator")
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": f"{title}: checked",
                                                     "metrics": metrics or {"checked": 3}, "followups": [fup]}))
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    return tid


def test_a_harness_parents_continue_hop_that_publishes_a_code_branch_runs_as_code_onto_it(env, monkeypatch):
    """A `continue:` follow-up of a harness task whose spec publishes a code task's branch (`pr_branch:`)
    is queued as kind code with that pr_branch and continues:<the branch's owner>, not the parent's kind
    and tier, and `ttp push --own` accepts it; its lineage stays the harness parent's."""
    from ttp import chaincap, push
    from ttp.db import continues_id
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    owner = p.db.add_task("Widget feature", "s", kind="code", origin="coordinator")
    branch = f"ttp/t{owner}-widget-feature"
    p.db.update_task(owner, status="done", branch=branch)
    _git_out(repo, "branch", branch)
    spec = f"Publish the reviewed head onto the PR.\npr_branch: {branch}\nThen update the PR description."
    parent = _harness_done(env, p, "Check the widget PR", {"title": "continue: publish the widget PR head",
                                                            "spec": spec})
    (hop,) = p.db.q("SELECT * FROM tasks WHERE parent=?", (parent,))
    labels = json.loads(hop["labels"])
    assert hop["kind"] == "code" and hop["status"] == "queued" and f"pr_branch:{branch}" in labels
    assert continues_id(hop) == owner and f"{chaincap.FOLLOWS}{parent}" in labels
    assert (hop["tier"], hop["tier_source"]) == ("standard", "default"), "the harness parent's deep tier was copied"
    assert [t["id"] for t in chaincap.chain(p.db, hop["id"])] == [parent, hop["id"]]
    _git_out(repo, "checkout", "-q", branch)
    monkeypatch.setenv("TTP_TASK", str(hop["id"]))
    assert push.own_target(p, repo) == ("origin", branch)
    assert push._carries(p, str(hop["id"]), str(owner), branch)
    # A `ttp push --own` step onto the code task's branch it names (no pr_branch: line) is the same.
    fup = {"title": "continue: push the widget head", "spec": f"Run `ttp push --own --detach` from {branch}."}
    parent2 = _harness_done(env, p, "Recheck the widget PR", fup)
    (hop2,) = p.db.q("SELECT * FROM tasks WHERE parent=?", (parent2,))
    assert hop2["kind"] == "code" and f"pr_branch:{branch}" in json.loads(hop2["labels"]) and continues_id(hop2) == owner


def test_a_publish_hop_whose_branch_owner_is_ambiguous_is_refused_and_stays_pending(env):
    """Two code lineages carry the branch: the hop is not queued; the coordinator's done event names the
    kind mismatch and keeps the follow-up pending. task_add (coordinator or CLI) refuses it the same way."""
    from ttp import coordinator as coord
    p = make(env)
    for title in ("Shared feature, part a", "Shared feature, part b"):
        t = p.db.add_task(title, "s", kind="code", origin="coordinator", labels=["pr_branch:feature/shared"])
        p.db.update_task(t, status="done")
    spec = "Publish the reviewed head.\npr_branch: feature/shared"
    parent = _harness_done(env, p, "Check the shared PR", {"title": "continue: publish the shared head", "spec": spec})
    assert not p.db.q("SELECT id FROM tasks WHERE parent=?", (parent,))
    done = p.db.one("SELECT * FROM events WHERE task=? AND kind='task_done'", (parent,))
    want = "kind harness cannot publish branch feature/shared (pr_branch:): code tasks"
    assert done["status"] == "queued" and want in done["text"] and "add it as kind code with continues:" in done["text"]
    fp = p.db.one("SELECT * FROM events WHERE task=? AND kind='followup_proposed'", (parent,))
    assert fp and fp["status"] == "queued" and "continue: publish the shared head" in fp["text"]
    (problem,) = coord.apply(p, [{"type": "task_add", "title": "Publish the shared head", "spec": spec,
                                  "kind": "harness"}])
    assert want in problem
    # A ttp/t<id> branch of no code task is refused too: nothing is guessed.
    (problem,) = coord.apply(p, [{"type": "task_add", "title": "Publish a lost head", "kind": "plan",
                                  "spec": "pr_branch: ttp/t9999-lost-head"}])
    assert "kind plan cannot publish branch ttp/t9999-lost-head (pr_branch:): no task #9999 owns it" in problem


def test_follow_ups_without_branch_delivery_keep_their_kind_and_a_code_parents_hop_is_unchanged(env):
    """A harness follow-up with no pr_branch: line or publish step stays harness at its parent's tier; a
    code parent's hop is queued as before (kind code, its tier, continues the parent)."""
    from ttp.db import continues_id
    p = make(env)
    parent = _harness_done(env, p, "Audit the queue", {"title": "continue: audit the queue",
                                                       "spec": "Read the next 50 events and fix what is wrong."})
    (hop,) = p.db.q("SELECT * FROM tasks WHERE parent=?", (parent,))
    assert (hop["kind"], hop["tier"], hop["tier_source"]) == ("harness", "deep", "parent_copy")
    assert "pr_branch" not in hop["labels"] and continues_id(hop) is None
    root, branch, done, reviews = _finish_code(env, p, "feature", {"a.py": 10}, result=_cont("feature"))
    (hop,) = p.db.q("SELECT * FROM tasks WHERE parent=?", (root,))
    assert (hop["kind"], hop["tier"], hop["tier_source"]) == ("code", "standard", "parent_copy")
    assert continues_id(hop) == root and "pr_branch" not in hop["labels"]


def test_a_queued_non_code_task_with_a_pr_branch_line_is_corrected_or_blocked_before_dispatch(env):
    """The model-free check before dispatch: a harness task whose spec gained a pr_branch: line runs as
    kind code with continues:<owner>; one whose owner is ambiguous is blocked with the mismatch, unstarted."""
    from ttp.daemon import Daemon
    from ttp.db import continues_id
    p = make(env)
    owner = p.db.add_task("Gadget feature", "s", kind="code", origin="coordinator")
    p.db.update_task(owner, status="done", branch=f"ttp/t{owner}-gadget-feature")
    ok = p.db.add_task("Publish gadget", f"pr_branch: ttp/t{owner}-gadget-feature", kind="harness",
                       tier="deep", tier_source="parent_copy", origin="coordinator")
    bad = p.db.add_task("Publish lost", "pr_branch: ttp/t9999-lost", kind="work", origin="coordinator")
    plain = p.db.add_task("Plain harness", "Fix the digest.", kind="harness", origin="coordinator")
    d = Daemon(p.base)
    assert d._publish_kind(p.db.task(plain))["kind"] == "harness"
    t = d._publish_kind(p.db.task(ok))
    assert t["kind"] == "code" and continues_id(t) == owner and (t["tier"], t["tier_source"]) == ("standard", "default")
    assert f"pr_branch:ttp/t{owner}-gadget-feature" in json.loads(t["labels"])
    assert d._publish_kind(p.db.task(bad)) is None
    row = p.db.task(bad)
    assert row["status"] == "blocked" and "kind work cannot publish branch ttp/t9999-lost" in row["blocked_reason"]
    assert not row["attempts"]
    assert p.db.one("SELECT status FROM events WHERE task=? AND kind='task_blocked'", (bad,))["status"] == "queued"


def test_a_publish_task_that_takes_over_failed_work_may_still_push_onto_the_branch(env, monkeypatch):
    """A task that continues failed harness work and publishes a code task's branch through a
    `ttp push --own` step is made kind code with continues:<owner> after continues:<failed>: own_target
    accepts it, though the owner is not the first continues: link."""
    from ttp import coordinator as coord, push
    from ttp.db import continues_id
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    owner = p.db.add_task("Lever feature", "s", kind="code", origin="coordinator")
    branch = f"ttp/t{owner}-lever-feature"
    p.db.update_task(owner, status="done", branch=branch)
    _git_out(repo, "branch", branch)
    failed = p.db.add_task("Publish the lever head", "s", kind="harness", origin="coordinator")
    p.db.update_task(failed, status="failed")
    coord.apply(p, [{"type": "task_add", "title": "Publish the lever head again", "kind": "harness",
                     "continues": failed, "spec": f"Run `ttp push --own --detach` from {branch}."}])
    (new,) = p.db.q("SELECT * FROM tasks WHERE title='Publish the lever head again'")
    labels = json.loads(new["labels"])
    assert new["kind"] == "code" and f"pr_branch:{branch}" in labels and f"continues:{owner}" in labels
    assert continues_id(new) == failed
    _git_out(repo, "checkout", "-q", branch)
    monkeypatch.setenv("TTP_TASK", str(new["id"]))
    assert push.own_target(p, repo) == ("origin", branch)
    # Only the pr_branch: label the conversion wrote with it counts: a bare second continues: does not.
    stray = p.db.add_task("Stray", "s", kind="code", origin="coordinator",
                          labels=[f"continues:{failed}", f"continues:{owner}"])
    assert push.foreign_owner(p, p.db.task(stray), branch) == owner


def test_a_follow_up_that_only_mentions_ttp_push_own_keeps_its_kind(env):
    """A non-code follow-up of a code task whose spec mentions `ttp push --own` but names no branch to
    publish keeps its kind: no branch is inferred from the task it continues or follows."""
    from ttp import coordinator as coord, publishkind
    p = make(env)
    owner = p.db.add_task("Dial feature", "s", kind="code", origin="coordinator")
    p.db.update_task(owner, status="done", branch=f"ttp/t{owner}-dial-feature")
    for i, spec in enumerate(("Read the run log. Do not run `ttp push --own` or edit anything.",
                              "Find why `ttp push --own` was refused in the last run.")):
        coord.apply(p, [{"type": "task_add", "title": f"Look at the dial run {i}", "kind": "harness",
                         "continues": owner, "spec": spec}])
        (t,) = p.db.q("SELECT * FROM tasks WHERE title=?", (f"Look at the dial run {i}",))
        assert t["kind"] == "harness" and "pr_branch" not in (t["labels"] or ""), (spec, t["labels"])
        assert publishkind.resolve(p.db, "harness", spec) is None


def _own_clone(p, tid, commit_now):
    """A standalone clone at task `tid`'s worktree path (as a worker makes one where the project root is
    no git repo): one old commit, and one made now when `commit_now`."""
    path = p.worktrees / f"t{tid}"
    path.mkdir(parents=True)
    old = {**os.environ, "GIT_COMMITTER_DATE": "@1000000000 +0000", "GIT_AUTHOR_DATE": "@1000000000 +0000"}
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "base.py").write_text("base\n")
    for args, e in ((["add", "."], None), ([*_IDENT, "commit", "-qm", "base"], old)):
        subprocess.run(["git", "-C", str(path), *args], check=True, env=e)
    if commit_now:
        (path / "more.py").write_text("more\n")
        _git_out(path, "add", ".")
        _git_out(path, *_IDENT, "commit", "-qm", "more")
    return path


def _code_hop_done(env, p, commit_now, metrics=None):
    """A done code root and a hop of it with no branch recorded, run in its own clone, that hands off a
    `continue:` and `metrics`: (root, hop)."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    root = _chain_root(p, "port the parser", 1.0)
    hop = p.db.add_task("continue: port the parser", "Carry on.", kind="code", origin="coordinator",
                        labels=[f"continues:{root}"])
    p.db.update_task(hop, status="running")
    _own_clone(p, hop, commit_now)
    run_dir = env["tmp"] / f"run-{hop}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(
        {"status": "done", "summary": "parser: part done", "metrics": metrics or {"passed": 3},
         "followups": [{"title": "continue: port the parser", "spec": "Carry on with the parser."}]}))
    Daemon(p.base)._finish_worker({"task": hop}, Usage(cost_usd=1.0), "ok", run_dir)
    return root, hop


def test_a_code_hop_whose_own_clone_has_a_new_commit_gets_its_continue_queued(env):
    """No branch recorded (a project root that is no git repo): the commit made in the hop's own clone
    during its run is its progress, and its continue is queued by the daemon."""
    from ttp import chaincap
    p = make(env)
    root, hop = _code_hop_done(env, p, commit_now=True)
    (nxt,) = p.db.q("SELECT * FROM tasks WHERE parent=?", (hop,))
    assert nxt["title"] == "continue: port the parser" and nxt["status"] == "queued"
    assert chaincap.progress(p, chaincap.chain(p.db, hop), 1) == "1 new commit"
    assert not p.db.q("SELECT id FROM events WHERE kind=?", (chaincap.NO_COMMIT_EVENT,))


def test_a_code_hop_with_new_metrics_but_no_commit_raises_one_replan_event_and_no_continue(env):
    """Metrics alone are no code hop's progress: no continue is queued (the commit in its clone predates
    its run) and exactly one NO_COMMIT_EVENT names the task and its continue for the coordinator."""
    from ttp import chaincap
    from ttp import coordinator as coord
    p = make(env)
    root, hop = _code_hop_done(env, p, commit_now=False, metrics={"passed": 12})
    assert not p.db.q("SELECT id FROM tasks WHERE parent=?", (hop,)), "metrics alone queued a code hop"
    (ev,) = p.db.q("SELECT * FROM events WHERE kind=?", (chaincap.NO_COMMIT_EVENT,))
    assert ev["status"] == "queued" and ev["task"] == hop and ev["text"].startswith(chaincap.NO_COMMIT_TRIGGER)
    assert f"#{hop}" in ev["text"] and "'continue: port the parser'" in ev["text"]
    data = json.loads(ev["data"])
    assert data["continue"] == "continue: port the parser" and data["root"] == root and data["task"] == hop
    assert chaincap.progress(p, chaincap.chain(p.db, hop), 1) == ""
    # Once only: the same hand-off checked again raises no second event.
    fup = {"title": "continue: port the parser", "spec": "Carry on."}
    assert not chaincap.no_commit_event(p, p.config(), p.db.task(hop), fup)
    assert len(p.db.q("SELECT id FROM events WHERE kind=?", (chaincap.NO_COMMIT_EVENT,))) == 1
    assert chaincap.NO_COMMIT_TRIGGER in coord.effort_triggers(p.db, p.config(), [ev["id"]], None)[0]


def test_code_hop_commits_read_its_clone_run_windows_and_named_shas_where_the_root_is_no_git_repo(env, tmp_path):
    """Where the project root is no git repo: only commits inside the hop's runs count, from its own
    clone's HEAD or a sha its hand-off names; a non-code hop keeps metrics as evidence."""
    from ttp import chaincap
    p = make(env)
    plain = tmp_path / "plain-root"
    plain.mkdir()
    q = types.SimpleNamespace(db=p.db, root=plain, worktrees=tmp_path / "wts")
    root = _chain_root(p, "port", 1.0)
    hop = _chain_hop(p, root, "continue: port", 1.0)
    p.db.update_task(hop, result=json.dumps({"status": "done", "metrics": {"passed": 3}}))
    path = _own_clone(q, hop, commit_now=True)
    ch = chaincap.chain(p.db, hop)
    assert chaincap.progress(q, ch, 1) == "1 new commit"
    # A run that ended before the commit: nothing counts, metrics or not.
    now = time.time()
    p.db.x("INSERT INTO runs(task, role, started, ended) VALUES(?,?,?,?)", (hop, "worker", now - 600, now - 300))
    assert chaincap.code_commits(q, p.db.task(hop)) == 0 and chaincap.progress(q, chaincap.chain(p.db, hop), 1) == ""
    # A named sha inside a run counts even off HEAD (the clone moved on to another branch).
    p.db.x("UPDATE runs SET ended=? WHERE task=?", (now + 60, hop))
    head = _git_out(path, "rev-parse", "HEAD").strip()
    _git_out(path, "checkout", "-q", "HEAD~1")
    p.db.update_task(hop, result=json.dumps({"status": "done", "summary": f"committed {head[:12]}"}))
    assert chaincap.named_shas(p.db.task(hop)) == [head[:12]]
    assert chaincap.code_commits(q, p.db.task(hop)) == 1
    # A non-code hop: new metrics are still its evidence.
    work = p.db.add_task("measure", "s", kind="work", origin="daemon", labels=[f"continues:{hop}"])
    p.db.update_task(work, status="done", result=json.dumps({"status": "done", "metrics": {"latency_ms": 9}}))
    assert chaincap.progress(q, chaincap.chain(p.db, work), 2) == "new evidence: latency_ms"


def test_a_non_code_hop_with_new_metrics_still_gets_its_continue_queued(env):
    from ttp import chaincap
    p = make(env)
    fup = {"title": "continue: check the gate", "spec": "Check the next gate."}
    tid = _harness_done(env, p, "check the gate", fup, metrics={"checked": 4})
    assert p.db.q("SELECT id FROM tasks WHERE parent=?", (tid,)), "a non-code hop with new metrics was not continued"
    assert not p.db.q("SELECT id FROM events WHERE kind=?", (chaincap.NO_COMMIT_EVENT,))
