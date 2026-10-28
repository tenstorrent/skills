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
