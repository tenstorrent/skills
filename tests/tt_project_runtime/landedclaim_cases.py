# Landed-claim tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations


def _claim_replay_setup(env, monkeypatch, landed):
    """A project whose repo pushes to a bare origin, and a code task whose branch holds one commit
    (`landed`: that commit is also on the push branch). Returns (p, repo, head)."""
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
    "pushed everything to a cache", "it was pushed to the cache") is no claim of a landing, and
    neither is a push or merge naming another destination: those sentences stay as written, a
    sha-less claim being checked only against the push branch it names or a plain 'landed'."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    for text in ("pushed the log files to the results folder.",
                 "pushed everything to the results folder.",
                 "it was pushed to the artifacts cache.",
                 "Pushed the fix to my branch for review.",
                 "Merged main into the work branch and reran the tests.",
                 "Pushed branch ttp/t12-x and opened a draft PR.",
                 "The change landed in the upstream repository.",
                 "The change landed onto my/repo."):
        assert pushclaim.correct_landed_claims(p, text, p.root, head) == text, text


def test_a_plain_landed_sentence_that_names_no_destination_is_checked(env, monkeypatch):
    """A plain 'landed' sentence whose remainder merely mentions a report, a 'main'-prefixed word, a
    count, a file path, 'and/or', 'forked' or 'the branch' names no other destination, so the claim
    is checked against the task head and corrected when nothing landed."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    for text in ("The change landed and the report is attached.",
                 "The change landed, mainly in the parser.",
                 "The change landed and maintains the old API.",
                 "The change landed: smoke 682/682 passed.",
                 "The change landed in plugins/tt-project/runtime/ttp/pushclaim.py.",
                 "The fix landed and/or was deployed.",
                 "The fix landed in the forked worktree.",
                 "The fix landed on the branch."):
        fixed = pushclaim.correct_landed_claims(p, text, p.root, head)
        assert "not landed" in fixed and head[:12] in fixed, (text, fixed)


def test_a_landed_claim_s_sentence_ends_at_its_nearest_stop(env, monkeypatch):
    """The next sentence's words (a branch, a PR, an upstream note) never change the claim's
    destination: each plain claim is checked against the task head."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    for text in ("The fix landed. Next: rebase on main.",
                 "The fix landed. I opened a PR for the docs.",
                 "The fix landed\nSee the upstream note later."):
        fixed = pushclaim.correct_landed_claims(p, text, p.root, head)
        assert "not landed" in fixed and head[:12] in fixed, (text, fixed)


def test_a_claim_naming_a_sha_this_repo_does_not_have_stays_as_written(env, monkeypatch):
    """A sentence naming a sha the sha-phrase parse drops (a sha this repository does not hold)
    is no sha-less claim checked against the task head: the claim is about another repository's
    commit, which no check here can read, so the sentence stays as written."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    for text in ("Sent the fix to the push queue; it landed as 1a2b3c4.",
                 "The fix landed as 1a2b3c4 and the checks passed."):
        assert pushclaim.correct_landed_claims(p, text, p.root, head) == text, text


def test_the_foreign_sha_rule_follows_the_one_sha_phrase_parse(env, monkeypatch):
    """The sha a sentence names is read by the one sha-phrase parse, so its case,
    negation and word rules hold here too: an uppercase foreign sha leaves the claim as written; a
    negated phrase ('never pushed as <sha>') or an all-hex word ('defaced') names no commit, so the
    sentence's bare claim is still checked against the task head."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    text = "The fix landed as 1A2B3C4 and the checks passed."
    assert pushclaim.correct_landed_claims(p, text, p.root, head) == text
    for text in ("The change landed; the draft was never pushed as 1a2b3c4.",
                 "The fix landed as defaced."):
        fixed = pushclaim.correct_landed_claims(p, text, p.root, head)
        assert "not landed" in fixed and head[:12] in fixed, (text, fixed)


def test_a_sha_less_claim_naming_the_push_branch_is_checked(env, monkeypatch):
    """A sha-less claim whose sentence names the push branch is checked against the task head even
    when its verb is a push or a merge: the correction still reads 'not landed' when the branch
    does not hold the head."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    for text in ("The change landed on the push branch.",
                 "Pushed the fix to the push branch.",
                 "Merged the change into origin/proj."):
        fixed = pushclaim.correct_landed_claims(p, text, p.root, head)
        assert "not landed" in fixed and head[:12] in fixed, (text, fixed)


def _in_flight_finish(env, monkeypatch, handoff_status: str):
    """The daemon's finish of a hand-off (`handoff_status`) claiming 'landed at <head>' while its
    run's detached push is still running. Returns (task, result, head)."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    from ttp import push
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    tid = p.db.add_task("harden the runtime", "s", kind="code", origin="coordinator")
    p.db.update_task(tid, status="running", branch="ttp/t1")
    (p.state / push.DETACHED).mkdir(parents=True, exist_ok=True)
    (p.state / push.DETACHED / f"t{tid}-x.json").write_text(json.dumps(
        {"run": 9, "status": "running", "target": "origin/proj", "head": head}))
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(
        {"status": handoff_status, "summary": f"Hardening landed at {head[:10]}."}))
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    task = p.db.task(tid)
    return task, json.loads(task["result"]), head


def test_a_hand_off_with_a_push_still_in_flight_is_not_locally_corrected(env, monkeypatch, tmp_path):
    """A hand-off whose run started a push that is still running is settled by the remote check:
    the local landed-claim check must not write 'not landed' about a push that has not happened
    yet, into the pushing record that the remote check then verifies."""
    task, res, head = _in_flight_finish(env, monkeypatch, "done")
    assert task["status"] == "pushing", (task["status"], res)   # its push is checked on the remote
    assert res["summary"] == f"Hardening landed at {head[:10]}.", res   # as written, not 'not landed'


@pytest.mark.parametrize("handoff_status", ["blocked", "failed", "needs_review"])
def test_a_hand_off_not_deferred_to_the_remote_check_is_corrected_despite_a_push_in_flight(
        env, monkeypatch, handoff_status):
    """Only a hand-off the remote check will settle skips the local correction. A blocked,
    failed or approval-needing one is never deferred, so its false 'landed at <sha>' still
    reads 'not landed'."""
    task, res, head = _in_flight_finish(env, monkeypatch, handoff_status)
    assert task["status"] != "pushing", task["status"]
    assert "not landed" in res["summary"] and head[:12] in res["summary"], res


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


def _daemon_finish(env, monkeypatch, fetchable: bool):
    """The daemon's real finish of a hand-off that claims a landing that never happened: the claim's
    one refresh fetch runs in the pre-transaction prefetch (_prefetch_lint), so inside the finish
    transaction the check only reads the fetched ref. Returns (project, result)."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    monkeypatch.setenv("TTP_TX_GUARD", "1")   # a fetch left inside the transaction is refused
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    if not fetchable:
        _git_out(repo, "remote", "set-url", "origin", str(env["tmp"] / "gone.git"))
    tid = p.db.add_task("harden the runtime", "s", kind="code", origin="coordinator")
    p.db.update_task(tid, status="running", branch="ttp/t1")
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    handoff = {"status": "done", "summary": _claim_text("sha", head)}
    (run_dir / "result.json").write_text(json.dumps(handoff))
    d = Daemon(p.base)
    d._prefetch_lint(tid, handoff, run_dir)   # the daemon asks before the transaction opens
    with d._finish_tx(tid):
        d._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    return p, json.loads(p.db.task(tid)["result"])


def test_the_daemon_s_finish_transaction_corrects_a_false_landed_claim(env, monkeypatch):
    """Replay of the daemon's finish path under the transaction guard: the claim's refresh fetch
    runs before the write transaction opens, so a hand-off claiming a landing that never happened
    still reads 'not landed' once the finish is recorded."""
    p, res = _daemon_finish(env, monkeypatch, fetchable=True)
    assert "not landed" in res["summary"] and "landed at" not in res["summary"], res["summary"]


def test_the_daemon_s_finish_transaction_marks_a_claim_it_cannot_check(env, monkeypatch):
    """The same finish with a push branch that cannot be fetched: the claim is marked
    '(unverified)', never left reading a plain 'landed'."""
    p, res = _daemon_finish(env, monkeypatch, fetchable=False)
    assert "(unverified)" in res["summary"] and "not landed" not in res["summary"], res["summary"]


# The reply-path check: a coordinator reply or notify's own 'landed'/'live' lines are checked
# against the push branch and the daemon's running commit before the message is sent.

_REPLY_4303 = (
    "The 'landed' check is live on every project (tt-project 0.2.1525). When a run says it "
    "landed something, the harness now checks the push branch without a model, and the message "
    "reads 'not landed' if the commit isn't there.\n\n"
    "The rollback-count fix is live too. another project's GLM device stops that already-live fixes "
    "covered no longer count, so only 1 of its 6 counts (the refused git push). GLM device work "
    "in tt-buddy stays off until the broker-only check (task #9695) is live. I'll turn it back "
    "on then and tell you.")

_REPLY_4310 = (
    "The project's GLM code route is back on since 10:30 PST. Both failed runs behind its 05:47 PST "
    "switch-off were cut by the dev box's reboots; without them it's 0 of 22 "
    "failed. Runs a host reboot or crash kills no longer count toward any switch-off, on every "
    "flag and project (task #9712).\n\n"
    "another project's GLM device route: its fix is in a second review, so it's later than the 10:05 "
    "PST I gave. Likely live about 12:30 PST; I turn the route on then and tell you.\n\n"
    "Daily GLM code report: GLM landed 38 code tasks since 11-18, at about Opus's rate and a bit "
    "cheaper ($3.66 vs $4.26 per landing). But fewer pass review first time (55% vs 62%) and they "
    "take twice as long to land (102 vs 50 min median). GLM did 45% of model-hours here in the "
    "last 24 h. All 10 bounces to Opus were guard stops; none got through.")


def test_a_reply_s_false_live_lines_are_marked_and_true_ones_pass(env, monkeypatch):
    """Replay of the 09:41 reply: its two 'live' lines name no commit, so both
    are marked 'not live yet: no commit named' before the reply is sent."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    base = _git_out(repo, "rev-parse", "HEAD~1")
    expected = ("The 'landed' check not live yet: no commit named on every project (tt-project "
                "0.2.1525). When a run says it not landed: no commit named something, the harness "
                "now checks the push branch without a model, and the message reads 'not landed' "
                "if the commit isn't there.\n\nThe rollback-count fix not live yet: no commit "
                "named too. another project's GLM device stops that already-live fixes covered no longer "
                "count, so only 1 of its 6 counts (the refused git push). GLM device work in "
                "tt-buddy stays off until the broker-only check (task #9695) is live. I'll turn "
                "it back on then and tell you.")
    fixed = pushclaim.correct_reply_claims(p, _REPLY_4303, p.root, running=head)
    assert fixed == expected, (fixed, expected)   # every line, whole: the gap shows here
    # a true live line: the named commit is an ancestor of the running one
    text = f"The fix is live (commit {base})."
    assert pushclaim.correct_reply_claims(p, text, p.root, running=head) == text
    # a landed line naming a commit on the push branch passes; one that names none is marked
    assert pushclaim.correct_reply_claims(p, f"Hardening landed at {head[:10]}.", p.root) == \
        f"Hardening landed at {head[:10]}."
    fixed = pushclaim.correct_reply_claims(p, "The fix landed.", p.root)
    assert "not landed: no commit named" in fixed, fixed
    _commit(repo, "off-branch.txt", "off\n")            # a commit the push branch does not hold
    off = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "reset", "-q", "--hard", head)
    fixed = pushclaim.correct_reply_claims(p, f"Hardening landed at {off[:10]}.", p.root)
    assert f"not landed: {off[:12]} is not on origin/proj" in fixed, fixed


def test_a_reply_with_no_landed_or_live_claim_goes_unchanged(env, monkeypatch):
    """Replay of the GLM report reply: its 'landed'/'live' words are about other tasks and
    other projects, so none is a claim and the reply posts exactly as written."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    assert pushclaim.correct_reply_claims(p, _REPLY_4310, p.root, running=head) == _REPLY_4310


def test_a_live_claim_naming_a_commit_the_running_version_lacks_is_marked(env, monkeypatch):
    """A 'live' line naming a commit the daemon's running version does not contain reads 'not
    live yet'; a version number instead of a commit is no commit at all."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    _commit(repo, "side.txt", "side\n")
    side = _git_out(repo, "rev-parse", "HEAD")   # a commit the running version does not hold
    _git_out(repo, "reset", "-q", "--hard", head)
    fixed = pushclaim.correct_reply_claims(p, f"The fix is live (commit {side[:10]}).", p.root, running=head)
    assert f"not live yet: {side[:10]} is not in the running version" in fixed, fixed
    fixed = pushclaim.correct_reply_claims(p, "The fix is live (0.2.1525).", p.root, running=head)
    assert "not live yet: no commit named" in fixed, fixed
    # a sha this repository does not hold is another repo's commit: unchecked, as written
    fixed = pushclaim.correct_reply_claims(p, "The fix is live (commit 9aa4c1f88ea7).", p.root, running=head)
    assert fixed == "The fix is live (commit 9aa4c1f88ea7).", fixed
    # the running version unknown: a live claim naming a commit cannot be checked, so it is
    # marked (unverified), never left reading a plain 'live'
    fixed = pushclaim.correct_reply_claims(p, f"The fix is live (commit {head[:10]}).", p.root)
    assert "(unverified)" in fixed and "not live yet" not in fixed, fixed
    # a sha-less live claim is a hard mark only when the check was possible: with the running
    # commit unknown it reads (unverified), never a 'not live yet' the check cannot support
    fixed = pushclaim.correct_reply_claims(p, "The fix is live.", p.root)
    assert "(unverified)" in fixed and "not live yet" not in fixed, fixed


def test_a_live_claim_with_a_running_commit_the_repo_lacks_reads_unverified(env, monkeypatch):
    """A project whose repo does not hold the daemon's running commit (any project but the
    plugin's own) cannot check a live claim at all: sha-less or not, it reads (unverified),
    never a false 'not live yet' mark in the user's chat."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    outside = "f1d0" * 10   # a running commit this repository does not hold
    fixed = pushclaim.correct_reply_claims(p, "The fix is live.", p.root, running=outside)
    assert "(unverified)" in fixed and "not live yet" not in fixed, fixed
    fixed = pushclaim.correct_reply_claims(p, f"The fix is live (commit {head[:10]}).", p.root,
                                           running=outside)
    assert "(unverified)" in fixed and "not live yet" not in fixed, fixed


def test_hedged_landed_and_live_claims_are_not_claims(env, monkeypatch):
    """'should have landed' and its hedge kin hedge a landed claim out of existence (the
    ponytail-lane follow-up); the unhedged sha-less landed claim is still corrected."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    for text in ("The fix should have landed.", "The fix may have landed.",
                 "The fix might have landed.", "The fix could have landed.",
                 "The fix should have gone live."):
        assert pushclaim.correct_reply_claims(p, text, p.root, running=head) == text, text
    fixed = pushclaim.correct_reply_claims(p, "The fix landed.", p.root)
    assert "not landed: no commit named" in fixed, fixed


def test_a_live_claim_inside_a_landed_fix_span_is_left_to_the_landed_fix(env, monkeypatch):
    """A sentence both parses catch ("was pushed <sha> live"): the landed fix owns the span and
    the live fix never splices into the same text twice, which corrupts the message."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    base = _git_out(repo, "rev-parse", "HEAD~1")
    fixed = pushclaim.correct_reply_claims(p, f"The fix was pushed {head[:10]} live.", p.root,
                                           running=base)
    assert "not landed" in fixed and "is not on origin/proj" in fixed, fixed
    assert "not live yet" not in fixed, fixed   # one fix per span, not two spliced into one


def test_two_live_phrases_in_one_sentence_get_one_mark(env, monkeypatch):
    """"is live" and "in the running version" are one claim, not two: the sentence is marked
    once, never a duplicated 'not live yet' line."""
    from ttp import pushclaim
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    fixed = pushclaim.correct_reply_claims(p, "The fix is now live in the running version.", p.root,
                                           running=head)
    assert fixed.count("not live yet") == 1, fixed


def test_the_reply_path_never_marks_or_rewrites_a_live_or_landed_line(env, monkeypatch):
    """The send path never marks, rewrites or negates a reply's 'landed'/'live' lines: the
    reply-claim check is off, so the texts it corrupted reach the chat as written."""
    from ttp import coordinator as coord
    p = make(env)
    for text in ("I'll tell you when the fix is live here",
                 "2bf5c16bce is live in 0.2.1547",
                 "Commit X has landed on the branch"):
        assert coord._deploy_checked(p, text, []) == text, text
    coord.apply(p, [{"type": "reply", "chat": "c1",
                     "text": "I'll tell you when the fix is live here"}])
    (sent,) = p.db.q("SELECT text FROM messages WHERE direction='out' AND kind='reply'")
    assert sent["text"] == "I'll tell you when the fix is live here", sent["text"]
    assert not [n for n in p.db.kv(coord.NOTES_KEY, []) if "pushclaim" in n], p.db.kv(coord.NOTES_KEY)


def test_a_reply_s_unverified_landed_claim_is_never_marked_twice(env):
    """A reply's sha-less landed claim in a project with no push branch reads (unverified); a
    second pass over the marked text adds no second mark (review of the reply branch)."""
    from ttp import pushclaim
    p = make(env)
    once = pushclaim.correct_reply_claims(p, "The change landed.", p.root)
    assert "landed (unverified)" in once, once
    assert pushclaim.correct_reply_claims(p, once, p.root) == once
