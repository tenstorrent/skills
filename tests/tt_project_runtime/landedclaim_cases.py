# Landed-claim tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations


def _claim_replay_setup(env, monkeypatch, landed):
    """A project whose repo pushes to a bare origin, and a code task whose branch holds one commit
    (`landed`: that commit is also on the push branch). Returns (p, head, summary words)."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "change.txt", "change\n")
    head = _git_out(repo, "rev-parse", "HEAD")
    if landed:
        _git_out(repo, "push", "-q", "origin", f"{head}:refs/heads/proj")
    return p, repo, head


def _claim_text(style, head=""):
    """A hand-off summary shaped like the replayed one: a landed-claim with a sha, without one, or
    a plain claim word."""
    return {
        "sha": f"Hardening landed at {head[:10]} (4 commits on a base). Checks passed.",
        "head": "Hardening landed (4 commits on a base). Checks passed.",
        "none": "Hardening done; checks passed.",
    }[style]


def test_a_summary_claiming_a_landing_the_push_branch_lacks_reads_not_landed(env, monkeypatch):
    """Replay of a hand-off that says its change landed while nothing was pushed: the stored text
    reads 'not landed' naming the push branch, and the task goes to its review, not failed."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    tid = p.db.add_task("harden the runtime", "s", kind="code", origin="coordinator")
    p.db.update_task(tid, status="running", branch="ttp/t1")
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": _claim_text("sha", head)}))
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    res = json.loads(p.db.task(tid)["result"])
    assert "not landed" in res["summary"] and f"{head[:12]} is not on origin/proj" in res["summary"], res
    assert "landed at" not in res["summary"]
    # the false word costs the run nothing beyond the correction: its work goes on as usual
    assert p.db.task(tid)["status"] == "done", p.db.task(tid)["status"]
    assert not p.db.q("SELECT id FROM events WHERE kind='task_failed' AND task=?", (tid,))


def test_a_summary_claim_whose_commit_is_on_the_push_branch_stays_landed(env, monkeypatch):
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    from ttp import pushclaim
    text = _claim_text("sha", head)
    assert pushclaim.correct_landed_claims(p, text, p.root) == text


def test_a_claim_naming_no_sha_is_checked_against_the_task_head(env, monkeypatch):
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    from ttp import pushclaim
    fixed = pushclaim.correct_landed_claims(p, _claim_text("head"), p.root, head)
    assert "not landed" in fixed and head[:12] in fixed and "origin/proj" in fixed, fixed
    _git_out(repo, "push", "-q", "origin", f"{head}:refs/heads/proj")   # the landing makes the claim true
    assert "not landed" not in pushclaim.correct_landed_claims(p, _claim_text("head"), p.root, head)


def test_a_claim_this_project_cannot_check_is_marked_unverified(env, monkeypatch):
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    p.set_config("delivery.push_branch", "")
    from ttp import pushclaim
    fixed = pushclaim.correct_landed_claims(p, _claim_text("sha", head), p.root, head)
    assert "(unverified)" in fixed and "not landed" not in fixed, fixed


def test_a_receipt_holds_a_claim_only_when_it_holds_its_commit(env, monkeypatch):
    """A claim naming no sha stands only when a push receipt holds the task head; a receipt for
    another commit does not make the claim true."""
    from ttp import landed, pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    monkeypatch.setattr(landed, "recorded", lambda *a: (["f" * 40],))
    fixed = pushclaim.correct_landed_claims(p, _claim_text("head"), p.root, head, tid=7)
    assert "not landed" in fixed, fixed
    monkeypatch.setattr(landed, "recorded", lambda *a: ([head],))
    assert "not landed" not in pushclaim.correct_landed_claims(p, _claim_text("head"), p.root, head, tid=7)


def test_a_push_of_something_else_is_no_landing_claim(env, monkeypatch):
    """A push verb whose subject is not the run's own work ("pushed the logs to a folder",
    "pushed everything to a cache", "it was pushed to the cache") is no claim of a landing."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    for text in ("pushed the log files to the results folder.",
                 "pushed everything to the results folder.",
                 "it was pushed to the artifacts cache."):
        assert pushclaim.correct_landed_claims(p, text, p.root, head) == text, text


def test_a_text_claim_the_local_check_cannot_settle_is_corrected_by_the_remote_check(env, monkeypatch, tmp_path):
    """Replay of a code task whose hand-off claims a push that never happened: the local check
    cannot read the branch, so the claim waits for the remote one; when the remote says the push is
    missing the text is corrected and the task goes to its normal review, never failed for it."""
    from ttp import daemon as dmod, landed, pushclaim
    p, d, tid, path, head, _ = _claim_setup(env, monkeypatch, tmp_path,
                                             {"status": "done", "summary": "pushed {head} to origin/proj"})
    monkeypatch.setattr(landed, "check", lambda *a, **k: (landed.CANNOT, "cannot read the ref"))
    _settle_probe(d, tid)
    task = p.db.task(tid)
    assert task["status"] == "pushing", (task["status"], task["result"])
    assert "(unverified)" in json.loads(task["result"])["summary"]   # never a plain 'pushed'
    d.verify_pushes()
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert task["status"] not in ("failed", "pushing"), (task["status"], res)
    assert "not landed" in res["summary"] and head[:12] in res["summary"], res
    assert not p.db.q("SELECT id FROM events WHERE kind='task_failed' AND task=?", (tid,))
    assert "not landed" in _digest_result(p, tid)


def _digest_result(p, tid):
    """The task's stored summary as the digest reads it."""
    return json.loads(p.db.task(tid)["result"])["summary"]



def test_a_landing_the_tracking_ref_lags_is_refreshed_before_not_landed(env, monkeypatch):
    """Replay of a hand-off whose push another route landed: the local tracking ref still lags
    behind the remote, and the landing is real. The claim is refreshed from the remote before it
    is called 'not landed', so the stored summary keeps the landing as a landing."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    old = _git_out(repo, "rev-parse", "refs/remotes/origin/proj")
    _commit(repo, "change.txt", "change\n")
    head = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", f"{head}:refs/heads/proj")
    _git_out(repo, "update-ref", "refs/remotes/origin/proj", old)   # the tracking ref lags the push
    tid = p.db.add_task("harden the runtime", "s", kind="code", origin="coordinator")
    p.db.update_task(tid, status="running", branch="ttp/t1")
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done",
        "summary": f"Hardening landed at {head[:10]}.",
        "push": [{"branch": "origin/proj", "head": head}]}))
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    res = json.loads(p.db.task(tid)["result"])
    assert "not landed" not in res["summary"], res["summary"]


def test_a_refresh_that_cannot_run_marks_the_claim_unverified(env, monkeypatch):
    """A claim the local ref cannot confirm is refreshed from the remote; a refresh that cannot
    run leaves the claim marked '(unverified)', never 'not landed' on the stale local ref."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    _git_out(repo, "remote", "set-url", "origin", str(env["tmp"] / "gone.git"))
    fixed = pushclaim.correct_landed_claims(p, _claim_text("sha", head), p.root)
    assert "(unverified)" in fixed and "not landed" not in fixed, fixed


def test_a_remote_confirmed_hand_off_keeps_its_landed_claim(env, monkeypatch):
    """A hand-off the remote check already confirmed is left as written: its verdict outranks the
    local tracking ref, which may lag the pushes it confirmed."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "change.txt", "change\n")
    head = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", f"{head}:refs/heads/proj")
    _git_out(repo, "update-ref", "refs/remotes/origin/proj", _git_out(repo, "rev-parse", "HEAD~1"))
    tid = p.db.add_task("harden the runtime", "s", kind="code", origin="coordinator")
    p.db.update_task(tid, status="running", branch="ttp/t1")
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done",
        "summary": f"Hardening landed at {head[:10]}."}))
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir,
                                  push_verified=True)
    res = json.loads(p.db.task(tid)["result"])
    assert "not landed" not in res["summary"] and head[:10] in res["summary"], res["summary"]
