# Superseded review tests (reviewsup.py) of tests/test_tt_project_runtime.py, which runs this file in
# its own namespace (see _include_cases there). Add new tests of closing failed reviews here.
from __future__ import annotations

from ttp.db import review_subject


def _changes_needed_chain(env, p):
    """A code task whose review asked for changes, then its re-review (continuing it) asked again: (first
    review, re-review, the second re-review queued after it, the change's branch)."""
    _, branch, _, (rev,) = _finish_code(env, p, "feature", {"app.py": 10})
    asked = {"status": "done", "metrics": {"verdict": "changes_needed"}, "summary": "needs a test"}
    _fail_review(env, p, rev["id"], [{"title": "add a test", "spec": "test_x"}], **asked)
    (re1,) = p.db.q("SELECT * FROM tasks WHERE kind='review' AND labels LIKE ?", (f'%"continues:{rev["id"]}"%',))
    _fail_review(env, p, re1["id"], [{"title": "test the edge", "spec": "test_y"}], **asked)
    # The second fix and its re-review, as the daemon queues them once the first fix lands.
    fix = p.db.add_task("fix again", "s", kind="code", origin="daemon",
                        labels=[f"continues:{review_subject(rev)}", f"review_fix:{re1['id']}"])
    p.db.update_task(fix, status="done", branch=branch)
    re2 = p.db.task(p.db.add_task("re-review", "s", kind="review", origin="daemon",
                                  labels=[f"auto_review:{fix}", f"continues:{re1['id']}"]))
    return p.db.task(rev["id"]), p.db.task(re1["id"]), re2, branch


def _ttp_task(p, *args):
    from ttp import cli
    try:
        cli.main(["task", p.name, *args])
    except SystemExit as e:
        return e.code
    return 0


def test_a_passing_re_review_supersedes_the_failed_reviews_of_its_chain(env, capsys):
    """Auto: once the last re-review of a chain passes, every earlier changes_needed review in it is
    recorded superseded by it with its approved head, model-free; the verdict is kept, readers show
    `superseded`, and a rerun changes nothing."""
    from ttp import reviewsup, sweep
    from ttp.db import CHANGES_NEEDED, SUPERSEDED, task_outcome
    p = make(env)
    rev, re1, re2, _ = _changes_needed_chain(env, p)
    assert task_outcome(rev) == task_outcome(re1) == CHANGES_NEEDED
    assert p.db.status_counts().get(CHANGES_NEEDED) == 2
    _fail_review(env, p, re2["id"], status="done", summary="each finding fixed")
    head = (p.db.one("SELECT head FROM review_stats WHERE task=?", (re2["id"],)) or {}).get("head") or ""
    for r in (rev, re1):
        t = p.db.task(r["id"])
        assert t["status"] == "failed" and task_outcome(t) == SUPERSEDED, t
        rec = reviewsup.record_of(t)
        assert (rec["by_review"], rec["sha"], rec["how"]) == (re2["id"], head, "auto"), rec
        kept = json.loads(t["result"])
        assert kept["summary"] == json.loads(r["result"])["summary"] and kept["status"] == CHANGES_NEEDED
    counts = p.db.status_counts()
    assert counts.get(SUPERSEDED) == 2 and CHANGES_NEEDED not in counts, counts
    before = p.db.task(rev["id"])["result"]
    assert reviewsup.auto(p.db, p.db.task(re2["id"])) == [], "idempotent"
    assert p.db.task(rev["id"])["result"] == before
    # The open lists leave them out; show and the sweep's diagnosis name what superseded them.
    assert _ttp_task(p, "list", "--status", CHANGES_NEEDED) == 0
    assert f"#{rev['id']}\t" not in capsys.readouterr().out
    assert _ttp_task(p, "list", "--status", SUPERSEDED) == 0
    out = capsys.readouterr().out
    assert f"#{rev['id']}\tsuperseded" in out and f"#{re1['id']}\tsuperseded" in out, out
    assert _ttp_task(p, "show", str(rev["id"])) == 0
    out = capsys.readouterr().out
    assert "status: superseded" in out and f"review superseded by review #{re2['id']}" in out, out
    subject = review_subject(rev)
    assert "superseded by review" in sweep.review_diagnosis(p.db, subject, time.time())
    # A passing review with no failed review before it marks nothing.
    _, _, _, (lone,) = _finish_code(env, p, "other", {"b.py": 3})
    _fail_review(env, p, lone["id"], status="done", summary="fine")
    assert reviewsup.earlier(p.db, p.db.task(lone["id"])) == []


def test_superseded_by_hand_takes_a_passed_re_review_or_a_reachable_commit(env, capsys):
    """Manual: a failed review is recorded superseded only on evidence: --by-review naming a passed
    review of the same chain, or a commit on the push branch or --ref. No evidence, an unreachable or
    unknown commit, a failed or unrelated --by-review are refused and record nothing; a non-review
    failed task keeps the old refusal; a second record is a no-op."""
    from ttp import reviewsup
    from ttp.db import CHANGES_NEEDED, SUPERSEDED, task_outcome
    p = make(env)
    rev, re1, re2, branch = _changes_needed_chain(env, p)
    _, obranch, _, (other,) = _finish_code(env, p, "unrelated", {"u.py": 2})
    _fail_review(env, p, other["id"], status="done", summary="fine")
    # No evidence: refused, naming both forms.
    assert _ttp_task(p, "superseded", str(rev["id"])) not in (0, None)
    err = capsys.readouterr().err
    assert "--by-review <passed re-review id>" in err and "<covering commit> [--ref <ref>]" in err, err
    # A failed re-review, or a passed review of another change: not evidence.
    assert _ttp_task(p, "superseded", str(rev["id"]), "--by-review", str(re1["id"])) not in (0, None)
    assert "not a passed review" in capsys.readouterr().err
    assert _ttp_task(p, "superseded", str(rev["id"]), "--by-review", str(other["id"])) not in (0, None)
    assert "not a later review" in capsys.readouterr().err
    # A commit that is on no given ref, or no commit at all.
    _git_out(p.root, "checkout", "-q", "-b", "side")
    _commit(p.root, "side.txt", "x\n")
    side = _git_out(p.root, "rev-parse", "HEAD").strip()
    _git_out(p.root, "checkout", "-q", "-")
    assert _ttp_task(p, "superseded", str(rev["id"]), side, "--ref", branch) not in (0, None)
    assert "not recorded" in capsys.readouterr().err
    assert _ttp_task(p, "superseded", str(rev["id"]), "0" * 40, "--ref", branch) not in (0, None)
    assert "no commit" in capsys.readouterr().err
    assert all(task_outcome(p.db.task(r["id"])) == CHANGES_NEEDED for r in (rev, re1))
    # A reachable commit records it.
    tip = _git_out(p.root, "rev-parse", branch).strip()
    assert _ttp_task(p, "superseded", str(rev["id"]), tip[:10], "--ref", branch) == 0
    assert "recorded as superseded (commit" in capsys.readouterr().out
    rec = reviewsup.record_of(p.db.task(rev["id"]))
    assert (rec["sha"], rec["ref"], rec["by_review"], rec["how"]) == (tip, branch, None, "manual"), rec
    # Idempotent: a second record keeps the first.
    assert _ttp_task(p, "superseded", str(rev["id"]), side, "--ref", "side") == 0
    assert "already recorded" in capsys.readouterr().out
    assert reviewsup.record_of(p.db.task(rev["id"]))["sha"] == tip
    # --by-review: a passed re-review of the same chain.
    _fail_review(env, p, re2["id"], status="done", summary="fixed")
    p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps(json.loads(re1["result"])), re1["id"]))  # undo auto
    assert task_outcome(p.db.task(re1["id"])) == CHANGES_NEEDED
    assert _ttp_task(p, "superseded", str(re1["id"]), "--by-review", str(re2["id"])) == 0
    assert f"by review #{re2['id']}" in capsys.readouterr().out
    assert task_outcome(p.db.task(re1["id"])) == SUPERSEDED
    # A failed task that is not a review keeps today's refusal; --by-review goes with a review only.
    code = p.db.add_task("broken", "s", kind="code")
    p.db.update_task(code, status="failed")
    assert _ttp_task(p, "superseded", str(code), tip) not in (0, None)
    assert "is failed, not done" in capsys.readouterr().err
    assert _ttp_task(p, "superseded", str(code), "--by-review", str(re2["id"])) not in (0, None)
    assert "--by-review goes with a failed review only" in capsys.readouterr().err
    assert _ttp_task(p, "show", str(code), "--by-review", "1") not in (0, None)
    assert "go with superseded" in capsys.readouterr().err
    del obranch

