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


# The reply footer: a coordinator reply, notify or ask goes out exactly as written;
# when it says 'live' or 'landed', each commit it names is checked once, and those that fail add
# one 'Unverified: ...' footer line at the end. No inline tags, no sentence parsing.

_REPLY_4368 = (
    "The check you asked for is live: commit {sha}, running here in ttp 0.1.1. "
    "Every 'live' or 'landed' line, mine included, must now name its commit. Before a "
    "message goes out, the reply path checks that the commit is in the running version. "
    "A line that fails is marked unverified instead of being sent as fact.\n\n"
    "One gap on a held machine: our own machine-reach check still tried an ssh to it "
    "despite the hold. The fix is queued at top priority. I'll tell you when it is live. "
    "Nothing for you to do.")


def _footer(p, text, running):
    """reply_footer of `text`, asserting the message itself comes back byte-exact."""
    from ttp import pushclaim
    out = pushclaim.reply_footer(p, text, p.root, running)
    assert out.startswith(text), (out, text)
    return out


def _side(repo, head, name="side"):
    """A commit off `head` that neither the running release nor the push branch holds."""
    _commit(repo, f"{name}.txt", f"{name}\n")
    side = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "reset", "-q", "--hard", head)
    return side


def test_true_live_replies_go_out_byte_identical(env, monkeypatch):
    """Replayed replies: every commit they name is in the
    running release, so each goes out exactly as written, with no footer."""
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    text = _REPLY_4368.format(sha=head)
    assert _footer(p, text, {"version": "0.1.1", "commit": head}) == text
    shas = []
    for name in ("box-order", "reply-crash", "held-machine"):
        _commit(repo, f"{name}.txt", f"{name}\n")
        shas.append(_git_out(repo, "rev-parse", "HEAD"))
    running = {"version": "0.1.1", "commit": shas[-1]}
    for sha, name in zip(shas, ("box-order", "reply-crash", "held-machine")):
        text = f"The {name} fix is live in the running release 0.1.1 ({shas[-1]}), commit {sha[:10]}."
        assert _footer(p, text, running) == text


def test_a_live_reply_naming_a_commit_the_running_release_lacks_gets_one_footer(env, monkeypatch):
    """A live line naming a side commit: the body stays byte-identical and one footer names the side commit;
    two failing commits share that one line."""
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    side, other = _side(repo, head), _side(repo, head, "other")
    running = {"version": "0.2.1550", "commit": head}
    text = f"The fix is live in 0.2.1550 ({head[:12]}). Next: commit {side[:10]} waits for its review."
    assert _footer(p, text, running) == f"{text}\n\nUnverified: {side[:10]} not in running 0.2.1550"
    text = f"Both are live: {side[:10]} and {other[:10]} (and {side[:10]} again)."
    out = _footer(p, text, running)
    assert out == (f"{text}\n\nUnverified: {side[:10]} not in running 0.2.1550, "
                   f"{other[:10]} not in running 0.2.1550"), out
    assert out.count("Unverified:") == 1


def test_a_commit_the_check_cannot_run_on_reads_not_checked(env, monkeypatch):
    """A named commit the running-release check cannot settle reads 'not checked'; a running
    commit this repository does not hold (another project's) checks nothing."""
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    text = f"The fix is live (commit {head[:10]})."
    assert _footer(p, text, None) == f"{text}\n\nUnverified: {head[:10]} not checked"
    assert _footer(p, text, {"version": "0.1.1", "commit": "f1d0" * 10}) == text
    # a sha this repository does not hold is another repository's commit: not checked here
    assert _footer(p, "The fix is live (commit 9aa4c1f88ea7).", {"commit": head}) == \
        "The fix is live (commit 9aa4c1f88ea7)."


def test_a_commit_named_by_a_prefix_without_a_letter_is_never_checked(env, monkeypatch):
    """A 10-character prefix of digits only (about 1 in 110 commits) reads as no commit, so the tests'
    commit helper re-dates such a commit until its prefix is checked."""
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=False)
    tree = _git_out(repo, "rev-parse", "HEAD^{tree}")
    for n in range(100_000):   # re-dated per try until the prefix has no letter
        env_dates = {**os.environ, "GIT_AUTHOR_DATE": f"{1_700_000_000 + n} +0000",
                     "GIT_COMMITTER_DATE": f"{1_700_000_000 + n} +0000"}
        bare = subprocess.run(["git", "-C", str(repo), "commit-tree", tree, "-p", head, "-m", "x"],
                              capture_output=True, text=True, check=True, env=env_dates).stdout.strip()
        if bare[:10].isdigit():
            break
    _git_out(repo, "reset", "-q", "--hard", bare)
    text = f"The fix is live (commit {bare[:10]})."
    assert _footer(p, text, None) == text   # the claim check does not see a commit in it
    assert _sha_like(repo) != bare
    text = f"The fix is live (commit {_git_out(repo, 'rev-parse', 'HEAD')[:10]})."
    assert _footer(p, text, None).endswith("not checked")


def test_a_message_with_no_live_or_landed_word_is_never_checked(env, monkeypatch):
    """No live/landed word: nothing is checked, even a commit the running release lacks; a
    negated word is no claim either."""
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    side = _side(repo, head)
    running = {"version": "0.1.1", "commit": head}
    for text in (f"Commit {side[:10]} waits for its review.",
                 f"The fix ({side[:10]}) is not live yet.",
                 f"The fix ({side[:10]}) is not deployed yet.",
                 f"It will be live after the restart ({side[:10]}).",
                 "The project's GLM code route is back on since 10:30 PST."):
        assert _footer(p, text, running) == text, text


def test_a_landed_reply_is_checked_against_the_push_branch(env, monkeypatch):
    """With only a 'landed' word, each named commit is checked on the push branch's local ref (no
    fetch); a project with no push branch checks nothing."""
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    off = _side(repo, head, "off")
    assert _footer(p, f"Hardening landed at {head[:10]}.", None) == f"Hardening landed at {head[:10]}."
    text = f"Hardening landed at {off[:10]}."
    assert _footer(p, text, None) == f"{text}\n\nUnverified: {off[:10]} not on origin/proj"
    q = make(env)
    assert _footer(q, "The change landed.", None) == "The change landed."


def test_a_coordinator_reply_and_ask_carry_the_footer_and_never_an_inline_tag(env, monkeypatch):
    """The send path: a reply and an ask go out as written plus at most one footer line, which
    also counts a failed `claims` item; a reply with nothing to flag goes out unchanged."""
    from ttp import claimcheck, coordinator as coord
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    side = _side(repo, head)
    receipt = {"pid": os.getpid(), "commit": head, "version": "0.1.1"}
    (p.state / "heartbeat").write_text(json.dumps({"pid": os.getpid(), "receipt": receipt}))
    text = f"The fix is live (commit {side[:10]})."
    acts = [{"type": "reply", "chat": "c1", "text": text,
             "claims": [{"text": "the fix is live", "expect": "false"}]}]
    claimcheck.prerun(p, acts)   # the daemon runs the probes before the batch
    coord.apply(p, acts)
    (sent,) = p.db.q("SELECT text FROM messages WHERE direction='out' AND kind='reply'")
    assert sent["text"] == (f"{text}\n\nUnverified: 1 status claim did not hold, "
                            f"{side[:10]} not in running 0.1.1"), sent["text"]
    coord.apply(p, [{"type": "ask_user", "text": f"{text} May I close the task?", "blocking": "spend",
                     "recommendation": "yes"}])
    ask = p.db.one("SELECT text FROM messages WHERE kind='ask'")["text"]
    assert ask.startswith(f"{text} May I close the task?"), ask
    assert ask.endswith(f"\n\nUnverified: {side[:10]} not in running 0.1.1") and ask.count("Unverified:") == 1, ask
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "I'll tell you when the fix is live here."}])
    last = p.db.q("SELECT text FROM messages WHERE direction='out' AND kind='reply' ORDER BY id DESC LIMIT 1")
    assert last[0]["text"] == "I'll tell you when the fix is live here.", last


def _reverted_setup(env, monkeypatch):
    """A push branch holding X (lines a file did not have) and then R, git's revert of X.
    Returns (p, repo, x, r)."""
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    _commit(repo, "fix.txt", "".join(f"the fix, line {i}\n" for i in range(4)))
    x = _git_out(repo, "rev-parse", "HEAD")
    _commit(repo, "other.txt", "unrelated\n")
    _git_out(repo, "revert", "--no-edit", x)
    r = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    return p, repo, x, r


def test_a_commit_a_later_revert_took_out_is_neither_landed_nor_live(env, monkeypatch):
    """An ancestor whose change a later revert took out reads not landed, naming the revert,
    and a live line naming it gets one footer item 'reverted by <R>, not in running'; a plain ancestor
    stays landed and a commit off the branch is not."""
    from ttp import landed
    p, repo, x, r = _reverted_setup(env, monkeypatch)
    tip = _git_out(repo, "rev-parse", "origin/proj")
    assert landed.reverted_by(repo, x, tip) == r
    assert not landed.on_branch(repo, x, tip)
    assert landed.check(p, repo, x, fetch=False) == (landed.NOT_YET, f"{x[:10]} reverted by {r[:10]} on origin/proj")
    other = _git_out(repo, "rev-parse", "HEAD~1")
    assert landed.check(p, repo, other, fetch=False)[0] == 0, "a plain ancestor is landed"
    off = _side(repo, tip, "off")
    assert landed.check(p, repo, off, fetch=False)[0] == landed.NOT_YET, "a non-ancestor is not"
    text = f"The fix is live in 0.2.9 (commit {x[:10]})."
    assert _footer(p, text, {"version": "0.2.9", "commit": tip}) == \
        f"{text}\n\nUnverified: {x[:10]} reverted by {r[:10]}, not in running 0.2.9"
    text = f"The fix landed at {x[:10]}."
    assert _footer(p, text, None) == f"{text}\n\nUnverified: {x[:10]} reverted by {r[:10]}, not on origin/proj"


def test_a_reverted_change_applied_again_reads_landed_again(env, monkeypatch):
    """Reverting the revert puts X's change back: X is landed and live again."""
    from ttp import landed
    p, repo, x, r = _reverted_setup(env, monkeypatch)
    _git_out(repo, "revert", "--no-edit", r)
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    tip = _git_out(repo, "rev-parse", "origin/proj")
    assert landed.reverted_by(repo, x, tip) == "" and landed.on_branch(repo, x, tip)
    assert landed.check(p, repo, x, fetch=False)[0] == 0
    text = f"The fix is live (commit {x[:10]})."
    assert _footer(p, text, {"version": "0.2.9", "commit": tip}) == text


def test_a_later_edit_of_the_same_lines_with_no_revert_keeps_a_commit_landed(env, monkeypatch):
    """A later commit rewrote X's lines but no revert names X: ancestry's answer stands."""
    from ttp import landed
    p, repo, head = _claim_replay_setup(env, monkeypatch, landed=True)
    _commit(repo, "fix.txt", "first\n")
    x = _git_out(repo, "rev-parse", "HEAD")
    _commit(repo, "fix.txt", "rewritten\n")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    tip = _git_out(repo, "rev-parse", "origin/proj")
    assert landed.reverted_by(repo, x, tip) == "" and landed.check(p, repo, x, fetch=False)[0] == 0


def test_an_unresolvable_reverts_range_poisons_no_landed_check_at_its_tip(env, monkeypatch):
    """A "Reverts A..B" whose range this repo cannot resolve (shas a rewrite, another fork or a typo left
    behind) names no commit we can know: an untouched ancestor stays landed, and a real revert elsewhere in
    the same history still reads as reverted."""
    from ttp import landed
    p, repo, a = _claim_replay_setup(env, monkeypatch, landed=True)
    _commit(repo, "one.txt", "one\n")
    one = _git_out(repo, "rev-parse", "HEAD")
    _commit(repo, "two.txt", "two\n")
    two = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "revert", "--no-edit", one)   # a real revert elsewhere in the same history
    rev = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "commit", "--allow-empty", "-qm", "Reverts deadbeef1..deadbeef2")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    tip = _git_out(repo, "rev-parse", "origin/proj")
    assert landed.holds(repo, two, tip) is True, "nothing reverts 'two', whatever the tip's other messages"
    assert landed.reverted_by(repo, two, tip) == ""
    assert landed.reverted_by(repo, one, tip) == rev
    assert landed.check(p, repo, two, fetch=False)[0] == 0
    assert landed.check(p, repo, one, fetch=False)[0] == landed.NOT_YET


def test_a_push_queue_range_revert_takes_out_each_commit_of_its_range(env, monkeypatch):
    """The push queue's post-landing revert ('Reverts A..B', A left out): each commit in the range reads
    reverted by it; A and commits before it stay landed."""
    from ttp import landed
    p, repo, a = _claim_replay_setup(env, monkeypatch, landed=True)
    _commit(repo, "one.txt", "one\n")
    one = _git_out(repo, "rev-parse", "HEAD")
    _commit(repo, "two.txt", "two\n")
    two = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "revert", "--no-commit", f"{a}..{two}")
    _git_out(repo, "commit", "-qm", f"Revert b: post-landing tests fail\n\nReverts {a[:10]}..{two[:10]}; failing: t")
    rev = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    tip = _git_out(repo, "rev-parse", "origin/proj")
    assert landed.reverted_by(repo, one, tip) == rev == landed.reverted_by(repo, two, tip)
    assert landed.reverted_by(repo, a, tip) == "" and landed.check(p, repo, a, fetch=False)[0] == 0


def test_a_landed_wait_on_a_task_whose_landing_was_reverted_does_not_pass(env, monkeypatch):
    """start_when landed:#<id> runs `ttp landed --task <id>`: a task whose recorded landing a later revert
    took out is not landed, and the words name the revert."""
    from ttp import landed
    p, repo, x, r = _reverted_setup(env, monkeypatch)
    tid = p.db.add_task("the fix", "spec", kind="code")
    now = time.time()
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, landed_sha, created, updated) "
           "VALUES(?, 'b', ?, 'origin/proj', 'landed', ?, ?, ?)", (tid, x, x, now, now))
    assert landed.probe_command(f"landed:#{tid}") == f"ttp landed --task {tid}"
    rc, why = landed.check(p, repo, tid=tid, fetch=False)
    assert rc == landed.NOT_YET and f"reverted by {r[:10]}" in why, why


def test_a_revert_test_that_cannot_run_reads_cannot_tell_never_not_landed(env, monkeypatch):
    """A git failure inside the revert test leaves the claim unverified (CANNOT), never 'not landed'."""
    from ttp import landed
    p, repo, x, r = _reverted_setup(env, monkeypatch)
    monkeypatch.setattr(landed, "_present", lambda *a: None)
    landed._reverted.clear()
    rc, why = landed.check(p, repo, x, fetch=False)
    assert rc == landed.CANNOT and "cannot tell whether" in why, why
    landed._reverted.clear()


def test_a_revert_the_local_ref_still_shows_is_rechecked_on_the_remote_that_landed_the_change_again(env, monkeypatch):
    """The local tracking ref still ends at the revert while the remote already put X's change back: the
    fetched tip decides, so X reads landed, not 'reverted'."""
    from ttp import landed
    p, repo, x, r = _reverted_setup(env, monkeypatch)
    _git_out(repo, "revert", "--no-edit", r)
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    _git_out(repo, "update-ref", "refs/remotes/origin/proj", r)   # the tracking ref lags the re-landing
    assert landed.check(p, repo, x, fetch=False)[0] == landed.NOT_YET
    assert landed.check(p, repo, x)[0] == 0


def test_a_task_whose_recorded_landing_a_later_revert_took_out_reads_dropped(env, monkeypatch):
    """The daemon's dropped-landing sweep (drops/carried) names a done task whose landed commit a later
    revert took off the push branch, naming the revert; with the change applied again it is carried."""
    from ttp import landed
    p, repo, x, r = _reverted_setup(env, monkeypatch)
    tid = p.db.add_task("the fix", "spec", kind="code")
    p.db.update_task(tid, status="done")
    now = time.time()
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, landed_sha, created, updated) "
           "VALUES(?, 'b', ?, 'origin/proj', 'landed', ?, ?, ?)", (tid, x, x, now, now))
    ((got, why),) = landed.drops(p, record=False)
    assert got == tid and f"reverted by {r[:10]}" in why, why
    _git_out(repo, "revert", "--no-edit", r)
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    assert landed.drops(p, record=False) == []


def _edit_fix(repo):
    """A later batch commit that rewrites the first line X added: X's own diff no longer reverse-applies."""
    f = repo / "fix.txt"
    f.write_text(f.read_text().replace("the fix, line 0\n", "the fix, line 0, edited later\n"))
    _git_out(repo, "commit", "-qam", "a later batch edits the fix")


@pytest.mark.parametrize("how", ["revert", "reapply", "pick"])
def test_a_revert_a_later_commit_undid_no_longer_takes_a_commit_off_the_branch(env, monkeypatch, how):
    """X was reverted (R), then put back by a revert of R, a 'Re-apply A..B' naming X's range, or a
    cherry-pick with X's patch-id; a later batch edited X's lines. X is landed: R no longer stands."""
    from ttp import landed
    p, repo, x, r = _reverted_setup(env, monkeypatch)
    if how == "revert":
        _git_out(repo, "revert", "--no-edit", r)
    elif how == "pick":
        _git_out(repo, "cherry-pick", x)
    else:   # the change and an extra file in one commit: no patch-id twin, only the message names X
        _git_out(repo, "cherry-pick", "--no-commit", x)
        _commit(repo, "extra.txt", "extra\n")
        base = _git_out(repo, "rev-parse", f"{x}~1")
        _git_out(repo, "commit", "--amend", "-qm", f"Re-apply {base[:10]}..{x[:10]} (the fix)")
    _edit_fix(repo)
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    tip = _git_out(repo, "rev-parse", "origin/proj")
    assert landed.reverted_by(repo, x, tip) == "" and landed.on_branch(repo, x, tip)
    assert landed.check(p, repo, x, fetch=False)[0] == 0


def test_a_revert_that_still_stands_keeps_a_commit_off_the_branch(env, monkeypatch):
    """A revert alone takes X out; so does reverting X's revert and then reverting that again."""
    from ttp import landed
    p, repo, x, r = _reverted_setup(env, monkeypatch)
    tip = _git_out(repo, "rev-parse", "origin/proj")
    assert landed.reverted_by(repo, x, tip) == r and not landed.on_branch(repo, x, tip)
    _git_out(repo, "revert", "--no-edit", r)
    _git_out(repo, "revert", "--no-edit", "HEAD")   # the revert of the revert, reverted again
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    tip = _git_out(repo, "rev-parse", "origin/proj")
    assert landed.reverted_by(repo, x, tip) == r and not landed.on_branch(repo, x, tip)
    assert landed.check(p, repo, x, fetch=False)[0] == landed.NOT_YET


def test_a_task_whose_superseding_commit_is_on_the_branch_raises_no_landing_dropped(env, monkeypatch):
    """The push queue landed X, a revert took it out, and the change landed again as S (recorded with
    `ttp task superseded`): the dropped-landing sweep raises no landing_dropped and `ttp landed --task`
    reads it landed, both by the same rule."""
    from ttp import landed
    p, repo, x, r = _reverted_setup(env, monkeypatch)
    _git_out(repo, "cherry-pick", "--no-commit", x)
    _commit(repo, "extra.txt", "extra\n")
    s = _git_out(repo, "rev-parse", "HEAD")
    _edit_fix(repo)
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    tid = p.db.add_task("the fix", "spec", kind="code")
    p.db.update_task(tid, status="done")
    now = time.time()
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, landed_sha, created, updated) "
           "VALUES(?, 'b', ?, 'origin/proj', 'landed', ?, ?, ?)", (tid, x, x, now, now))
    p.db.x("INSERT INTO task_superseded(task, head, sha, ts) VALUES(?,?,?,?)", (tid, x, s, now))
    assert landed.drops(p) == []
    assert not p.db.q("SELECT id FROM events WHERE kind=?", (landed.DROP_EVENT,))
    assert landed.check(p, repo, tid=tid, fetch=False)[0] == 0


def test_a_revert_checked_again_on_the_same_tip_reads_no_patch_ids_again(env, monkeypatch):
    """The cherry-pick check of a revert runs git patch-id once per revert and tip; a second check answers
    the same from what it read."""
    import subprocess
    from ttp import landed
    p, repo, x, r = _reverted_setup(env, monkeypatch)
    tip = _git_out(repo, "rev-parse", "origin/proj")
    log, calls, run = landed._revert_log(repo, tip), [], subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: (calls.append(cmd), run(cmd, *a, **k))[1])
    first = landed._undone(repo, log, r, x, tip)
    n = sum("patch-id" in c for c in calls)
    assert n and landed._undone(repo, log, r, x, tip) == first is False
    assert sum("patch-id" in c for c in calls) == n
