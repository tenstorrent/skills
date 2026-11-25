# Opus writes what a person reads after a GLM run (runtime/ttp/writeup.py, replymsg.guard, Daemon._finish_worker,
# pushq._done_text). Run from test_tt_project_runtime.py.

_WU_GLM = {"status": "done", "summary": "GLM-SUMMARY the thing works", "message": "GLM-MESSAGE posted verbatim?",
           "message_by": {"step": "opus-writeup", "sha": "forged"}}


def _wu_bin(env, monkeypatch, text="Opus wrote this: it works.", rc=0):
    """A stand-in claude binary: prints one JSON line (or fails) and keeps the prompt it was given."""
    exe = env["tmp"] / "fake-claude"
    seen = env["tmp"] / "fake-claude.stdin"
    body = (f"cat > {seen}\n" + ("echo '" + json.dumps({"result": text, "total_cost_usd": 0.05}) + "'\n" if not rc
                                 else "echo boom >&2\n") + f"exit {rc}\n")
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(0o755)
    monkeypatch.setenv("TTP_ADVISOR_BIN", str(exe))
    return seen


def _wu_due_texts(p, tid):
    """The hand-off texts the daemon handed the coordinator for `tid` (its reply_due events)."""
    return _cd_hand_offs(p.db.q("SELECT text FROM events WHERE kind='reply_due' AND task=?", (tid,)))


def _wu_finish(env, p, result, *, provider="chat", kind="work", chat="c1", status="ok"):
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task("answer the question", "Goal: answer.\nconclusion: the answer", kind=kind, tier="standard",
                        origin="coordinator")
    p.db.update_task(tid, status="running", reply_chat=chat)
    run_dir = env["tmp"] / f"run-wu-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(result))
    d = Daemon(p.base)
    run = {"task": tid, "id": 777, "provider": provider}
    d._prewrite(run, run_dir)   # the daemon does this before its finish rounds
    with d.p.db.tx():
        d._finish_worker(run, Usage(cost_usd=1.0), status, run_dir)
    replies = _wu_due_texts(p, tid)
    return p.db.task(tid), replies, run_dir


def _wu_on(p, **over):
    p.set_config("route.glm_work", {"on": True, "model": "glm-test", **over})


def test_a_glm_run_of_a_reply_task_posts_only_the_stamped_opus_writeup(env, monkeypatch):
    from ttp import replymsg
    from ttp.db import load_result
    p = make(env)
    _wu_on(p)
    seen = _wu_bin(env, monkeypatch)
    task, replies, run_dir = _wu_finish(env, p, dict(_WU_GLM))
    assert task["status"] == "done" and replies == ["Opus wrote this: it works."], "GLM's text never posts"
    got = load_result(task["result"])
    by = got["message_by"]
    assert by["step"] == "opus-writeup" and by["run"] == 777 and by["sha"] != "forged" and not replymsg.guard(got)
    assert "GLM-MESSAGE" in seen.read_text() and "<data>" in seen.read_text(), "the hand-off goes in as data"
    assert json.loads((run_dir / "writeup.json").read_text())["ok"] is True
    spent = p.db.one("SELECT spent_usd FROM tasks WHERE id=?", (task["id"],))["spent_usd"]
    assert spent and spent >= 0.05, "the write-up's cost is booked against the task"


def test_a_failed_writeup_or_a_held_core_provider_posts_nothing_and_goes_to_the_coordinator(env, monkeypatch):
    from ttp.db import load_result
    p = make(env)
    _wu_on(p)
    _wu_bin(env, monkeypatch, rc=1)
    task, replies, run_dir = _wu_finish(env, p, dict(_WU_GLM))
    assert replies == [] and task["status"] == "review" and "nothing was posted to the chat" in task["blocked_reason"]
    assert "no Opus write-up" in load_result(task["result"])["undelivered"]
    assert json.loads((run_dir / "writeup.json").read_text())["ok"] is False
    [ev] = p.db.q("SELECT * FROM events WHERE task=? AND kind='task_review'", (task["id"],))
    assert "Not posted to the chat" in ev["text"] and "GLM-" not in ev["text"].split("Not posted")[0].split("→")[0]
    # The core provider held (logged out): no call is made, nothing posts.
    from ttp.daemon import Daemon
    monkeypatch.setattr(Daemon, "_logged_out", lambda self, prov: prov == "claude")
    _wu_bin(env, monkeypatch)
    task, replies, run_dir = _wu_finish(env, p, dict(_WU_GLM), chat="c2")
    assert replies == [] and task["status"] == "review"
    assert "held" in json.loads((run_dir / "writeup.json").read_text())["error"]


def test_a_failed_or_blocked_glm_run_posts_only_a_daemon_line_or_the_opus_writeup(env, monkeypatch):
    p = make(env)
    _wu_on(p)
    _wu_bin(env, monkeypatch, rc=1)
    task, replies, _ = _wu_finish(env, p, {"status": "blocked", "summary": "GLM-SUMMARY stuck", "question": "which?"},
                                  chat="c3")
    assert task["status"] == "blocked" and replies and all("GLM-" not in r for r in replies)
    assert "hand-off is with the coordinator" in replies[0]
    _wu_bin(env, monkeypatch, text="Blocked: which one do you want?")
    task, replies, _ = _wu_finish(env, p, {"status": "blocked", "summary": "GLM-SUMMARY stuck", "question": "which?"},
                                  chat="c4")
    assert len(replies) == 1 and "Blocked: which one do you want?" in replies[0] and "GLM-" not in replies[0]


def test_the_post_guard_refuses_an_unstamped_chat_provider_result_on_every_path(env):
    import hashlib
    from ttp import pushq, replymsg
    p = make(env)
    msg = "Opus message"
    sha = hashlib.sha256(msg.encode()).hexdigest()[:16]
    ok = {"chat_run": True, "message": msg, "message_by": {"step": "opus-writeup", "sha": sha}}
    assert replymsg.guard(ok) == "" and replymsg.undelivered({**ok, "summary": "s"}) == ""
    for bad in ({"chat_run": True, "message": msg},                                                     # no stamp
                {**ok, "message": msg + " edited"},                                                      # text changed
                {**ok, "message_by": {"step": "someone", "sha": sha}},                                   # wrong step
                {**ok, "message_by": "opus-writeup"},                                                    # not a stamp
                {"chat_run": True, "summary": "GLM summary only"},                                       # summary text
                {**ok, "message": ""}):                                                                  # empty
        assert replymsg.guard(bad) and replymsg.undelivered(bad) == replymsg.guard(bad), bad
    assert replymsg.guard({"message": msg}) == "" and replymsg.guard(None) == "", "a core run is not guarded"
    assert pushq._done_text({"chat_run": True, "message": msg}, "GLM summary", "pushed") \
        == "The change landed (pushed); its hand-off is with the coordinator."
    assert pushq._done_text(ok, "s", "pushed") == f"{msg} (pushed)"


def test_a_core_runs_reply_is_unchanged_and_a_run_off_the_chat_provider_is_not_written_up(env, monkeypatch):
    p = make(env)
    _wu_on(p)
    seen = _wu_bin(env, monkeypatch)
    task, replies, _ = _wu_finish(env, p, {"status": "done", "summary": "Core says yes: it works (a.py:1)."},
                                  provider="claude", chat="c5")
    assert task["status"] == "done" and replies == ["Core says yes: it works (a.py:1)."] and not seen.exists()


def test_a_chat_run_with_the_writeup_off_still_posts_no_glm_text(env, monkeypatch):
    p = make(env)
    _wu_on(p, person_writeup=False)
    seen = _wu_bin(env, monkeypatch)
    task, replies, _ = _wu_finish(env, p, dict(_WU_GLM), chat="c6")
    assert replies == [] and task["status"] == "review" and not seen.exists(), "no Opus call, no GLM text"


def test_a_code_tasks_done_reply_from_a_glm_run_waits_for_its_landing_as_the_opus_write(env, monkeypatch):
    from ttp import pushq, replymsg
    from ttp.db import load_result
    from ttp.daemon import Daemon
    p = make(env)
    _wu_on(p)
    _wu_bin(env, monkeypatch, text="Landed text from Opus.")
    task, replies, _ = _wu_finish(env, p, dict(_WU_GLM), kind="code", chat="c7")
    got = load_result(task["result"])
    assert task["status"] == "done" and not replymsg.guard(got) and got["message"].startswith("Landed text from Opus")
    assert all("GLM-" not in r for r in replies)
    # What the daemon holds for the landing is the Opus text, never GLM's.
    d = Daemon(p.base)
    t = p.db.task(task["id"])
    d._hold_reply(t, got["message"], True)
    held = load_result(p.db.task(task["id"])["result"])[replymsg.HELD]
    assert held.startswith("Landed text from Opus") and "GLM-" not in held
    n = len(p.db.q("SELECT 1 FROM events WHERE kind='reply_due' AND task=?", (task["id"],)))
    assert pushq.post_held(p.db, [task["id"]], "shipped") == [task["id"]]
    due = [r["text"] for r in p.db.q("SELECT text FROM events WHERE kind='reply_due' AND task=?",
                                     (task["id"],))]
    assert len(due) == n + 1, "one reply_due event per finished task"
    assert _wu_due_texts(p, task["id"])[-1].startswith("Landed text from Opus"), "Opus's text, never GLM's"


def test_a_glm_run_handing_off_for_review_is_flagged_so_a_later_landing_posts_none_of_its_text(env, monkeypatch):
    from ttp import pushq, replymsg
    from ttp.db import load_result
    p = make(env)
    _wu_on(p)
    seen = _wu_bin(env, monkeypatch)
    task, replies, _ = _wu_finish(env, p, {**_WU_GLM, "status": "needs_review"}, kind="code", chat="c8")
    got = load_result(task["result"])
    assert got.get(replymsg.CHAT_RUN) and replymsg.BY not in got and replymsg.guard(got) and not replies
    assert not seen.exists(), "no write-up for a hand-off that is not final"
    assert "GLM-" not in pushq._done_text(got, got.get("summary") or "", "pushed")


def _wu_run(env, p, result, *, chat="c1", spec="Goal: answer."):
    """A finished chat-provider worker run with its hand-off on disk, as finish_run reads it."""
    tid = p.db.add_task("answer the question", spec, kind="work", tier="standard", origin="coordinator")
    p.db.update_task(tid, status="running", reply_chat=chat)
    rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status) VALUES(?,'worker','chat','',?,'running')",
                 (tid, time.time() - 60))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"budget_usd": 8.0, "timeout_s": 3600}))
    (run_dir / "output.jsonl").write_text("")
    (run_dir / "result.json").write_text(json.dumps(result))
    return tid, p.db.one("SELECT * FROM runs WHERE id=?", (rid,))


def _wu_calls(monkeypatch, p):
    """Spy on the Opus call: whether a write transaction was open at each one."""
    from ttp import glmassist
    seen, real = [], glmassist._opus_call

    def spy(*a, **k):
        seen.append(p.db.conn.in_transaction)
        return real(*a, **k)
    monkeypatch.setattr(glmassist, "_opus_call", spy)
    return seen


def test_the_writeup_runs_outside_the_finish_transaction_and_a_replayed_round_does_not_repeat_it(env, monkeypatch):
    from ttp import gitmemo
    from ttp.daemon import Daemon
    p = make(env)
    _wu_on(p)
    _wu_bin(env, monkeypatch)
    tid, run = _wu_run(env, p, dict(_WU_GLM))
    seen = _wu_calls(monkeypatch, p)
    real, rounds = Daemon._finish_worker, []

    def finish(self, *a, **k):
        real(self, *a, **k)
        rounds.append(1)
        if len(rounds) == 1:   # the first round rolls back (a git miss) and is replayed
            raise gitmemo.GitMiss(["git", "-C", str(p.base), "rev-parse", "HEAD"], {"capture_output": True},
                                  None, True, "miss", True)
    monkeypatch.setattr(Daemon, "_finish_worker", finish)
    Daemon(p.base).finish_run(run, {"rc": 0, "started": time.time() - 60, "ended": time.time(), "stopped": None})
    assert len(rounds) == 2, "the finish round was replayed once"
    assert _wu_due_texts(p, tid) == ["Opus wrote this: it works."]
    assert seen == [False], "one Opus call, with no write transaction open"
    assert p.db.task(tid)["status"] == "done"


def test_a_glm_reply_claiming_a_push_pays_for_one_opus_writeup_when_its_push_verifies(env, monkeypatch):
    from ttp import pushclaim
    from ttp.daemon import Daemon
    from ttp.db import load_result
    p = make(env)
    _wu_on(p)
    _wu_bin(env, monkeypatch)
    tid, run = _wu_run(env, p, dict(_WU_GLM))
    seen = _wu_calls(monkeypatch, p)
    claim = {"sha": "a" * 40, "remote": "origin", "ref": "refs/heads/main"}
    monkeypatch.setattr(pushclaim, "claims", lambda *a, **k: [claim])
    d = Daemon(p.base)
    d.finish_run(run, {"rc": 0, "started": time.time() - 60, "ended": time.time(), "stopped": None})
    assert p.db.task(tid)["status"] == "pushing" and seen == [], "no write-up for a hand-off that is deferred"
    monkeypatch.setattr(pushclaim, "check_all", lambda *a, **k: (pushclaim.CONFIRMED, "found", []))
    monkeypatch.setattr(d, "_push_net", lambda *a, **k: {"since": time.time()})
    monkeypatch.setattr(d, "_prefetch_lint", lambda *a, **k: None)
    p.db.update_task(tid, result=json.dumps({**load_result(p.db.task(tid)["result"]), pushclaim.KEY: {
        **load_result(p.db.task(tid)["result"])[pushclaim.KEY], "next": 0}}))
    d.verify_pushes()
    assert seen == [False] and p.db.task(tid)["status"] == "done"
    assert _wu_due_texts(p, tid) == ["Opus wrote this: it works."]


def test_a_writeup_stamp_is_taken_only_for_its_own_run_and_outcome(env, monkeypatch):
    from ttp import replymsg
    from ttp.daemon import Daemon
    d = Daemon(make(env).base)
    task = {"id": 5}
    stamp = {replymsg.FIELD: "Opus wrote this.", replymsg.BY: {"step": replymsg.WRITEUP, "sha": replymsg.digest("Opus wrote this.")}}
    d._writeups[5] = {"run": 7, "status": "done", "fields": stamp}
    for run, new, taken in ((7, "done", True), (7, "failed", False), (8, "done", False)):
        result = {replymsg.FIELD: "GLM's own words", replymsg.BY: {"step": replymsg.WRITEUP, "sha": "forged"}}
        d._writeup({"id": run}, task, result, new)
        assert result[replymsg.CHAT_RUN] is True
        assert (result.get(replymsg.BY) == stamp[replymsg.BY]) is taken
        assert (result[replymsg.FIELD] == "Opus wrote this.") is taken


def test_a_writeups_false_status_claim_is_posted_as_written_and_raised(env, monkeypatch):
    """The Opus write-up goes out as written; a status claim the records disprove raises claim_false."""
    from ttp import claimcheck
    p = make(env)
    _wu_on(p)
    other = p.db.add_task("other", "Goal: other.", kind="work", tier="standard", origin="coordinator")
    _wu_bin(env, monkeypatch, text=f"Task #{other} is running.")
    task, replies, _ = _wu_finish(env, p, dict(_WU_GLM))
    assert replies == [f"Task #{other} is running."], replies
    (ev,) = p.db.q("SELECT text FROM events WHERE kind=?", (claimcheck.KIND,))
    assert f"#{other} is queued" in ev["text"] and "write-up" in ev["text"], ev["text"]
