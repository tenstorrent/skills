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


def test_each_task_and_run_is_judged_by_the_claim_of_its_own_clause(env):
    from ttp import claimcheck, coordinator as coord
    p = make(env)
    done = p.db.add_task("a", "s", kind="work", status="done")
    run = p.db.add_task("b", "s", kind="work", status="running")
    queued = p.db.add_task("c", "s", kind="work", status="queued")
    for turn, text in enumerate((f"#{done} landed and #{run} is running.", f"#{run} is running, #{done} finished.",
                                 f"#{done}, #{done} and #{done} landed.", f"#{run} is running but #{queued} will be "
                                 f"started later."), 1):
        coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=turn)
        assert _cc_sent(p)[-1] == text, _cc_sent(p)[-1]
    assert not _cc_false_events(p)
    # One false part: only its task is named.
    text = f"#{done} landed and #{queued} is running."
    coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=9)
    assert _cc_sent(p)[-1] == f"{claimcheck.FALSE_MARK} (#{queued} is queued): {text}", _cc_sent(p)[-1]
    # Bound to no one claim (a verbless clause with other words, two claims): unverified, never false.
    text = f"#{queued} on the box, #{run} is running and #{done} landed."
    coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=10)
    assert _cc_sent(p)[-1] == f"#{queued} on the box, #{run} is running and #{done} landed (unverified).", _cc_sent(p)
    assert len(_cc_false_events(p)) == 1


def test_an_ended_run_started_and_a_worker_notify_on_its_own_run_are_not_false(env):
    from ttp import claimcheck, coordinator as coord, daemon
    p = make(env)
    tid = p.db.add_task("smoke", "s", kind="work", status="done")
    p.db.x("INSERT INTO runs(id,task,role,status) VALUES(4321,?,'worker','done')", (tid,))
    text = "Run 4321 started at 13:00 and passed."
    coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=1)
    assert _cc_sent(p)[-1] == text, _cc_sent(p)
    coord.apply(p, [{"type": "reply", "text": "Run 4321 is running.", "chat": "c1"}], turn=2)
    assert _cc_sent(p)[-1].startswith(f"{claimcheck.FALSE_MARK} (run 4321 is done)"), _cc_sent(p)
    # Relayed when the run has ended: the worker's own task and run were running when it wrote this.
    run_dir = env["tmp"] / "4321"
    run_dir.mkdir()
    (run_dir / daemon.NOTIFY_FILE).write_text(
        json.dumps({"ts": time.time(), "severity": "low", "text": f"#{tid} is running the smoke in run 4321."}) + "\n")
    with p.db.tx():
        daemon.relay_worker_notifies(p, p.db.task(tid), run_dir, {"status": "done"})
    got = _cc_sent(p, "alert")
    assert got[-1].endswith(f"#{tid} is running the smoke in run 4321."), got
    assert len(_cc_false_events(p)) == 1


def test_an_id_after_the_claim_verb_or_a_hedge_in_its_clause_is_not_judged_as_its_subject(env):
    from ttp import claimcheck, coordinator as coord
    p = make(env)
    done = p.db.add_task("a", "s", kind="work", status="done")
    run = p.db.add_task("b", "s", kind="work", status="running")
    p.db.x("INSERT INTO runs(id,task,role,status) VALUES(777,?,'worker','done')", (done,))
    for turn, text in enumerate((f"#{run} is running the review of #{done}.", f"#{run} is running checks from run 777.",
                                 f"#{run} restarted after #{done} landed.", f"After #{done} landed, #{run} restarted.",
                                 f"#{done} and run 777 finished, #{run} is running."), 1):
        coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=turn)
        assert _cc_sent(p)[-1] == text, _cc_sent(p)[-1]
    assert not _cc_false_events(p)
    text = f"#{run} is running and #{done} is running."
    coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=9)
    assert _cc_sent(p)[-1] == f"{claimcheck.FALSE_MARK} (#{done} is done): {text}", _cc_sent(p)[-1]
    assert len(_cc_false_events(p)) == 1


def test_an_id_after_the_verb_with_a_noun_subject_is_never_false_but_a_start_on_a_task_that_never_ran_is(env):
    from ttp import claimcheck, coordinator as coord
    p = make(env)
    done = p.db.add_task("a", "s", kind="code", status="done")
    p.db.x("INSERT INTO runs(task,role,status) VALUES(?,'worker','done')", (done,))
    texts = (f"A review is running for #{done}.", f"Checks are running on #{done}.",
             f"Tests passed on #{done}.", f"A worker restarted on #{done}.", f"A review started for #{done}.")
    for turn, text in enumerate(texts, 1):
        coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=turn)
        assert not _cc_sent(p)[-1].startswith(claimcheck.FALSE_MARK), _cc_sent(p)[-1]
    assert not _cc_false_events(p)
    # The one exception: a start on a task that never ran stays false.
    never = p.db.add_task("b", "s", kind="work", status="queued")
    coord.apply(p, [{"type": "reply", "text": f"Long runs started on #{never}.", "chat": "c1"}], turn=9)
    assert _cc_sent(p)[-1].startswith(f"{claimcheck.FALSE_MARK} (#{never} is queued)"), _cc_sent(p)[-1]
    assert len(_cc_false_events(p)) == 1


def test_a_pgrep_f_probe_never_matches_the_shell_running_it(env):
    """`! pgrep -f <pattern>` run as `sh -c '<probe>'` matched its own shell, whose command line carries
    the pattern: a claim that a process was gone failed while none ran. Every probe runner passes the
    text through the environment instead (probespawn.hidden), and a live process still matches."""
    import os
    import shutil
    import subprocess
    import time
    import uuid
    import pytest
    from ttp import claimcheck, ends, looks, probespawn, schedrun
    if not shutil.which("pgrep"):
        pytest.skip("no pgrep here")
    p = make(env)
    tok = "ttp-probe-" + uuid.uuid4().hex   # built at run time: no command line above this test carries it
    gone = f"! pgrep -f {tok} >/dev/null"

    def answers():
        # One probe at a time: two concurrent `pgrep -f <token>` processes would match each other.
        acts = [{"type": "reply", "text": "x is gone.", "claims": [{"text": "x gone", "expect": gone}]}]
        claimcheck.prerun(p, acts)
        pend = probespawn.Pending(gone, cwd=str(p.root), env=None, stdout=False)
        pend.started.wait(10)
        pended = pend.wait(10)
        looked = looks.screen(p.db, p.root, [{"id": "l1", "at": time.time() - 1, "text": "t", "check": gone}],
                              time.time())
        return [acts[0]["claims"][0]["_rc"], pended, ends.probe_once(p, gone, 10)[0],
                schedrun.run_group(gone, cwd=str(p.root), timeout=10, env=None).returncode, 0 if not looked else 1]

    assert answers() == [0, 0, 0, 0, 0]
    live = subprocess.Popen(["sh", "-c", "sleep 30", tok], start_new_session=True)
    try:
        assert answers() == [1, 1, 1, 1, 1]
    finally:
        live.kill()
        live.wait()
    # Nothing a probe starts inherits its text, and a probe's own exit code still comes through.
    got = schedrun.run_group(f"env | grep -c {probespawn.PROBE_ENV}= ; exit 7", cwd=str(p.root), timeout=10, env=None)
    assert (got.returncode, got.stdout.strip()) == (7, "0")
    assert os.environ.get(probespawn.PROBE_ENV) is None


def test_a_cant_reach_claim_is_judged_by_each_machines_last_ssh_probe(env):
    """Upstream note 9c67e8613069a76c: a coordinator repeated an untested 'workers can't reach these boxes'
    note and every box answered when probed. The reach record (reach.py) keeps each listed machine's last
    `ssh <alias> true` answer and time; a reply saying a machine cannot be reached is false when it answered
    within ssh.reach_fresh_min, unverified with no probe that fresh, and sent as written when it failed one."""
    from ttp import claimcheck, coordinator as coord, machines, reach
    p = make(env)
    now = time.time()
    for h in ("box-a", "box-b", "box-c"):
        machines.add(h, hostname=f"{h}.example" if h == "box-c" else None)
    reach.record({"box-a": True, "box-b": False, "box-c": "agent", "not-listed": True}, now - 60)
    st = reach.read()
    assert st["box-a"]["ok"] is True and st["box-b"]["ok"] is False and set(st) == {"box-a", "box-b"}
    reach.record({"box-a": False}, now - 600)   # an older answer never overwrites a newer one
    assert reach.read()["box-a"]["ok"] is True
    got, false, unv = claimcheck.check(p, "Workers can't reach box-a or box-b.", now=now)
    assert got.startswith(f"{claimcheck.FALSE_MARK} (box-a answered ssh at ") and len(false) == 1 and not unv, got
    assert claimcheck.check(p, "box-b is unreachable.", now=now) == ("box-b is unreachable.", [], 0)
    # No probe of it (the agent answer was never kept), no machine named, or a probe past the window: unverified.
    for text in ("box-c.example is not reachable.", "The boxes are unreachable.", "box-a cannot be reached."):
        at = now + 31 * 60 if text.startswith("box-a") else now
        got, false, unv = claimcheck.check(p, text, now=at)
        assert unv == 1 and not false and claimcheck.UNVERIFIED in got, (text, got)
    # A setting widens the window; a question, a condition, code and a probe that held stay as written.
    p.set_config("ssh.reach_fresh_min", 60)
    assert claimcheck.check(p, "box-a cannot be reached.", now=now + 40 * 60)[1], "40 min old is fresh under 60"
    for text in ("Can workers not reach box-a?", "If box-a is unreachable, retry later.", "Ran `ssh box-a || echo unreachable`."):
        assert claimcheck.check(p, text, now=now) == (text, [], 0), text
    claims = [{"text": "box-a is unreachable", "expect": "true", "_rc": 0}]
    assert claimcheck.check(p, "box-a is unreachable.", claims, now=now)[1] == []
    # A status verb and a reach phrase in one sentence: the false one wins.
    tid = p.db.add_task("t", "s", kind="work", status="running")
    got, false, _ = claimcheck.check(p, f"Task #{tid} is running but box-a is unreachable.", now=now)
    assert got.startswith(claimcheck.FALSE_MARK) and "box-a answered ssh" in false[0][1]
    # Through apply: the reply goes marked and the coordinator gets the high claim_false event.
    coord.apply(p, [{"type": "reply", "text": "Workers can't reach box-a.", "chat": "c1"}], turn=1)
    assert _cc_sent(p)[-1].startswith(claimcheck.FALSE_MARK) and _cc_false_events(p)


def test_the_digest_shows_each_machines_reach_and_the_daemon_probes_due_machines(env, monkeypatch):
    from ttp import coordinator as coord, daemon as dm, machines, netwait, reach
    p = make(env)
    now = time.time()
    for h in ("box-a", "box-b", "box-old"):
        machines.add(h, tags="retired" if h == "box-old" else None)
    assert reach.due(now) == ["box-a", "box-b"]
    reach.record({"box-a": True}, now - 60)
    assert reach.due(now) == ["box-b"] and reach.due(now + 1200) == ["box-a", "box-b"]
    lines = reach.digest_lines(p, now)
    assert lines[0].startswith("- box-a: answered ssh at ") and lines[1] == "- box-b: never probed"
    assert len(lines) == 2, "a retired machine is left out"
    d = coord.digest(p, {}, [], [])
    assert "## Machine reach" in d and "- box-a: answered ssh at " in d
    # The daemon's step probes only the due machines, in a thread, with a harmless `true`.
    asked = []
    monkeypatch.setattr(netwait, "reach", lambda names, env=None: asked.append(list(names)) or {n: False for n in names})
    monkeypatch.setenv("TTP_TEST_REACH", "1")
    d = dm.Daemon(p.base)
    d.check_reach()
    d._reach.join(5)
    assert asked == [["box-b"]] and reach.read()["box-b"]["ok"] is False
    d.check_reach()
    assert d._reach is None or not d._reach.is_alive() and asked == [["box-b"]]
    # The daemon's own probe of a waiting task's host is kept too.
    d._host_up.clear()
    monkeypatch.setattr(netwait, "reach", lambda names, env=None: {n: True for n in names})
    d._check_hosts(["box-b"], time.time())
    assert reach.read()["box-b"]["ok"] is True




@pytest.mark.parametrize("block", [
    "---\nThe smoke test is running on the box.\n---",
    "```\nThe smoke test is running on the box.\n```",
    "> The smoke test is running on the box.",
])
def test_a_claim_in_a_block_meant_for_posting_is_marked_after_the_block_never_inside_it(env, block):
    from ttp import claimcheck
    p = make(env)
    text = f"Draft for the channel:\n{block}\nThe model is running since 07:09."
    got, false, unverified = claimcheck.check(p, text)
    assert block in got and unverified == 2, got
    after = got.split(block, 1)[1]
    assert after.startswith(f'\n{claimcheck.UNVERIFIED}: "The smoke test is running on the box."'), got
    assert got.endswith("The model is running since 07:09 (unverified)."), got
    tid = p.db.add_task("smoke the model", "s", kind="work", status="queued")
    quoted = f"---\nTask #{tid} is running now.\n---"
    got, false, _ = claimcheck.check(p, f"{quoted}\nThat is all.")
    assert got.startswith(quoted + f'\n{claimcheck.FALSE_MARK} (#{tid} is queued): "Task #{tid} is running now."')
    assert false == [(f"Task #{tid} is running now.", f"#{tid} is queued")]


def test_a_claim_whose_probe_passes_is_never_marked_though_the_daemon_did_not_run_it_first(env):
    from ttp import claimcheck, coordinator as coord
    p = make(env)
    acts = [{"type": "reply", "text": "The smoke test is running on the box.", "chat": "c1",
             "claims": [{"text": "smoke test running", "expect": "true"}]}]
    coord.apply(p, acts, turn=1)   # no prerun: a replayed batch, a worker notify
    assert _cc_sent(p)[-1] == "The smoke test is running on the box."
    # A sentence stating the claim shorter than its text still finds it.
    claims = [{"text": "the smoke test is running on the box since this morning", "expect": "true"}]
    assert claimcheck.check(p, "Smoke test running on the box.", claims)[0] == "Smoke test running on the box."


def test_a_rewritten_probe_that_cannot_tell_falls_back_to_the_probe_as_written(env, monkeypatch):
    from ttp import claimcheck, landed
    p = make(env)
    monkeypatch.setattr(landed, "probe_command", lambda expect, proj: "exit 2")   # 'ttp landed': CANNOT
    claims = [{"text": "fix landed on the branch", "expect": "true"}]
    claimcheck.prerun(p, [{"type": "reply", "text": "x", "claims": claims}])
    assert claims[0]["_rc"] == 0
    claims = [{"text": "fix landed on the branch", "expect": "false"}]
    claimcheck.prerun(p, [{"type": "reply", "text": "x", "claims": claims}])
    assert claims[0]["_rc"] == 2   # neither passes: the rewrite's answer stands
