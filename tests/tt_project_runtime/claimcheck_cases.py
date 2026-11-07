# Status claims checked against live state before they reach the user (runtime/ttp/claimcheck.py) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations


def _cc_sent(p, kind="reply"):
    return [m["text"] for m in p.db.q("SELECT text FROM messages WHERE direction='out' AND kind=? ORDER BY id", (kind,))]


def _cc_false_events(p):
    from ttp import claimcheck
    return p.db.q("SELECT * FROM events WHERE kind=? AND status='queued'", (claimcheck.KIND,))


def test_a_false_running_claim_is_held_back_with_what_is_true_and_wakes_the_coordinator(env):
    from ttp import claimcheck, coordinator as coord
    p = make(env)
    tid = p.db.add_task("smoke the model", "s", kind="work", status="queued")
    coord.apply(p, [{"type": "reply", "text": f"Task #{tid} is running since 07:09.", "chat": "c1"}], turn=1)
    (got,) = _cc_sent(p)
    assert got.startswith(f"{claimcheck.FALSE_MARK} (#{tid} is queued): Task #{tid} is running"), got
    (ev,) = _cc_false_events(p)
    assert ev["severity"] == "high" and f"#{tid} is queued" in ev["text"] and "chat c1" in ev["text"]
    assert any("claimcheck:" in n for n in p.db.kv(coord.NOTES_KEY, []))
    # 'started' with no run at all, in a notify, is false the same way; the same false claims raise one event.
    coord.apply(p, [{"type": "notify", "text": f"Long runs started on #{tid}."}], turn=2)
    assert _cc_sent(p, "alert")[-1].startswith(f"{claimcheck.FALSE_MARK} (#{tid} is queued)")
    assert len(_cc_false_events(p)) == 2
    for _ in range(2):
        claimcheck.raise_false(p.db, [(f"Long runs started on #{tid}.", f"#{tid} is queued")], "your notify #9")
    assert len(_cc_false_events(p)) == 3
    # True once the task runs: sent as written.
    p.db.x("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    coord.apply(p, [{"type": "reply", "text": f"Task #{tid} is running.", "chat": "c1"}], turn=4)
    assert _cc_sent(p)[-1] == f"Task #{tid} is running."


def test_a_probe_backed_claim_is_sent_as_written_and_a_failing_probe_holds_it_back(env):
    from ttp import claimcheck, coordinator as coord
    p = make(env)

    def send(expect, turn):
        acts = [{"type": "reply", "text": "The smoke test is running on the box.", "chat": "c1",
                 "claims": [{"text": "smoke test is running", "expect": expect}]}]
        claimcheck.prerun(p, acts)   # the daemon runs it before the batch, outside its transaction
        coord.apply(p, acts, turn=turn)
        return _cc_sent(p)[-1]

    assert send("true", 1) == "The smoke test is running on the box."
    assert send("false", 2).startswith(f"{claimcheck.FALSE_MARK} (its check `false` exited 1)")
    assert len(_cc_false_events(p)) == 1
    # A probe that cannot answer (75: busy) or outlasts its time is no verdict: marked unverified.
    assert send("exit 75", 3) == "The smoke test is running on the box (unverified)."
    # Inside a database transaction no probe runs: a probe never holds the write lock.
    acts = [{"type": "reply", "text": "x is running.", "claims": [{"text": "x running", "expect": "true"}]}]
    with p.db.tx():
        claimcheck.prerun(p, acts)
    assert "_rc" not in acts[0]["claims"][0]


def test_an_unbacked_claim_is_marked_unverified_and_the_rest_kept(env):
    from ttp import coordinator as coord
    p = make(env)
    text = "Hi. The model is running since 07:09. Smoke deployed to the box!\nNext I'll look at the logs."
    coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=1)
    assert _cc_sent(p)[-1] == ("Hi. The model is running since 07:09 (unverified). Smoke deployed to the box "
                               "(unverified)!\nNext I'll look at the logs.")
    assert not _cc_false_events(p)


def test_a_cap_raised_claim_is_checked_against_the_live_config(env):
    from ttp import claimcheck, coordinator as coord
    p = make(env)
    p.set_config("budget.daily_usd", 100)
    for turn, text in enumerate(("Raised the daily cap to $500.", "budget.daily_usd is set to 500 now."), 1):
        coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=turn)
        assert _cc_sent(p)[-1] == f"{claimcheck.FALSE_MARK} (budget.daily_usd is 100): {text}", _cc_sent(p)[-1]
    p.set_config("budget.daily_usd", 500)
    coord.apply(p, [{"type": "reply", "text": "Raised the daily cap to $500.", "chat": "c1"}], turn=3)
    assert _cc_sent(p)[-1] == "Raised the daily cap to $500."


def test_plans_conditions_negations_and_questions_are_not_claims(env):
    from ttp import claimcheck, coordinator as coord
    p = make(env)
    for text in ("I'll start the smoke run now.", "I’ll have it running by 15:00.", "Once it is running I will tell you.",
                 "It will be deployed after review.", "The box has not restarted.", "Is the smoke running?",
                 "The test set is large.", "Run `ps aux | grep running` to see.", "Going to start it next.",
                 "We plan to get it deployed today."):
        assert claimcheck.detect(text) == [], text
        coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=1)
        assert _cc_sent(p)[-1] == text
    for text in ("Long runs started.", "The flag is set to true.", "Restarted the broker.", "It landed at 10:00."):
        assert claimcheck.detect(text) == [text], text


def test_a_worker_notify_claim_is_checked_against_the_records(env):
    from ttp import claimcheck, daemon
    p = make(env)
    tid = p.db.add_task("smoke", "s", kind="work", status="running")
    other = p.db.add_task("other", "s", kind="work", status="queued")
    run_dir = env["tmp"] / "ccrun"
    run_dir.mkdir()
    (run_dir / daemon.NOTIFY_FILE).write_text(
        json.dumps({"ts": time.time(), "severity": "low", "text": f"#{other} is running too."}) + "\n"
        + json.dumps({"ts": time.time(), "severity": "low", "text": "Smoke running on the box."}) + "\n")
    with p.db.tx():
        daemon.relay_worker_notifies(p, p.db.task(tid), run_dir, {"status": "done"})
    got = _cc_sent(p, "alert")
    assert got[0].startswith(f"#{tid} smoke: {claimcheck.FALSE_MARK} (#{other} is queued)"), got
    assert got[1].endswith("Smoke running on the box (unverified)."), got
    (ev,) = _cc_false_events(p)
    assert f"#{tid}'s `ttp notify`" in ev["text"]


def test_a_hand_off_claiming_a_live_state_without_evidence_reads_unverified(env):
    from ttp import claimcheck, coordinator as coord
    p = make(env)
    assert claimcheck.handoff_mark({"summary": "Smoke running on the box since 07:09."})
    for res in ({"summary": "Smoke running on the box; log at /tmp/run/smoke.log."},
                {"summary": "Smoke running on the box.", "artifacts": ["results/smoke.out"]},
                {"summary": "Wrote the docs and the tests."}, {"summary": "I will start it next run."}):
        assert claimcheck.handoff_mark(res) == "", res
    tid = p.db.add_task("smoke", "s", kind="work", status="done")
    p.db.x("UPDATE tasks SET result=?, updated=? WHERE id=?",
           (json.dumps({"status": "done", "summary": "Smoke started on the box."}), time.time(), tid))
    digest = coord.digest(p, {}, [], [])
    line = next(ln for ln in digest.splitlines() if ln.startswith(f"- #{tid} "))
    assert "unverified: claims a live state, cites no command output" in line, line
