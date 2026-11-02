# Premise check tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file, and pytest collects these
# tests under its name. Add new tests of premise.py (a code spec's `premise:` line runs as a cheap
# question first) here.
from __future__ import annotations


def _premise_project(env):
    p = make(env)
    p.db.x("UPDATE tasks SET status='done'")
    return p


def _code(p, spec="Goal: add the retry.\npremise: `ttp task show` has no retry line yet\nChange: add it.", **kw):
    return p.db.add_task("add the retry line", spec, kind="code", **kw)


def _end_check(p, qid, status="done", **result):
    p.db.update_task(qid, status=status, result=json.dumps({"status": status, **result}))


def test_a_code_task_with_a_premise_line_waits_on_a_light_question_check_first(env):
    from ttp import premise
    p = _premise_project(env)
    cid = _code(p, priority=2)
    got = premise.tend(p.db)
    q = p.db.one("SELECT * FROM tasks WHERE kind='question'")
    assert got == [(cid, f"premise check #{q['id']} queued")]
    assert (q["tier"], q["origin"], q["priority"], q["tier_source"]) == ("light", "daemon", 2, "premise_check")
    assert json.loads(q["labels"]) == [f"premise-check:{cid}"] and premise.is_check(q)
    assert "The premise to check: `ttp task show` has no retry line yet" in q["spec"]
    assert '"premise": "holds" or "fails"' in q["spec"] and "change no file" in q["spec"]
    code = p.db.task(cid)
    assert json.loads(code["depends_on"]) == [q["id"]] and json.loads(code["labels"]) == [f"premise:{q['id']}"]
    assert [t["id"] for t in p.db.ready_tasks()] == [q["id"]], "the code task waits on its check"
    assert premise.tend(p.db) == [] and p.db.one("SELECT COUNT(*) n FROM tasks WHERE kind='question'")["n"] == 1


def test_only_an_explicit_premise_line_on_a_fresh_code_task_is_split(env):
    from ttp import premise
    p = _premise_project(env)
    assert premise.line("- premise: the log shows it\nmore") == "the log shows it"
    assert premise.line("  * Premise:   x  ") == "x"
    plain = _code(p, spec="Fix the retry; its premise: is checked by hand.")       # a mid-line mention
    empty = _code(p, spec="Fix it.\npremise:   \nmore")                            # a key with no check
    work = p.db.add_task("look", "premise: x", kind="work")                         # not a code task
    hop = _code(p, labels=["continues:3"])                                          # a continue hop
    ran = _code(p)
    p.db.x("INSERT INTO runs(task,role,started,status) VALUES(?,?,?,?)", (ran, "worker", time.time(), "failed"))
    tried = _code(p)
    p.db.update_task(tried, attempts=1)
    blocked = _code(p)
    p.db.update_task(blocked, status="blocked")
    assert premise.tend(p.db) == []
    assert not p.db.q("SELECT 1 FROM tasks WHERE kind='question'")
    assert {t["id"] for t in p.db.ready_tasks()} >= {plain, empty, work, hop, ran, tried}


def test_a_held_premise_carries_its_check_into_the_code_spec_once_and_the_code_task_starts(env):
    from ttp import premise
    p = _premise_project(env)
    cid = _code(p)
    premise.tend(p.db)
    qid = p.db.one("SELECT id FROM tasks WHERE kind='question'")["id"]
    assert premise.tend(p.db) == [], "a running or queued check is waited on"
    _end_check(p, qid, premise="holds", summary="No retry line: cli.py:40 prints only the status.")
    assert premise.tend(p.db) == [(cid, f"premise holds (#{qid})")]
    code = p.db.task(cid)
    assert code["spec"].endswith(f"\n\nPremise check #{qid}: holds. No retry line: cli.py:40 prints only the status.")
    assert json.loads(code["labels"]) == [f"premise:{qid}:holds"] and json.loads(code["depends_on"]) == []
    assert [t["id"] for t in p.db.ready_tasks()] == [cid]
    assert premise.tend(p.db) == [] and p.db.task(cid)["spec"] == code["spec"], "settled once, never split again"


def test_a_failed_premise_closes_the_code_task_as_not_needed_with_a_done_event(env):
    from ttp import premise
    p = _premise_project(env)
    cid = _code(p)
    premise.tend(p.db)
    qid = p.db.one("SELECT id FROM tasks WHERE kind='question'")["id"]
    _end_check(p, qid, premise="fails", summary="Already there: cli.py:40 prints the retry line since abc123.")
    assert premise.tend(p.db) == [(cid, f"premise fails (#{qid})")]
    code = p.db.task(cid)
    res = json.loads(code["result"])
    assert code["status"] == "done" and res["outcome"] == "rejected" and res["premise_check"] == qid
    assert res["summary"] == (f"Not needed: premise check #{qid} found the premise does not hold. "
                              "Already there: cli.py:40 prints the retry line since abc123.")
    ev = p.db.one("SELECT * FROM events WHERE kind='task_done' AND task=?", (cid,))
    assert ev["status"] == "queued" and "done, not run" in ev["text"] and "abc123" in ev["text"]
    assert not p.db.q("SELECT 1 FROM runs WHERE task=?", (cid,)), "no code run was spent"


def test_a_premise_check_with_no_verdict_or_a_dead_end_still_runs_the_code_task(env):
    from ttp import premise
    from ttp.daemon import Daemon
    p = _premise_project(env)
    cases = {}
    for how in ("no verdict", "unknown word", "failed", "cancelled", "blocked hand-off"):
        cid = _code(p)
        premise.tend(p.db)
        cases[how] = (cid, json.loads(p.db.task(cid)["labels"])[0].split(":")[1])
    _end_check(p, int(cases["no verdict"][1]), summary="could not tell")
    _end_check(p, int(cases["unknown word"][1]), premise="maybe")
    _end_check(p, int(cases["failed"][1]), status="failed")
    _end_check(p, int(cases["cancelled"][1]), status="cancelled")
    _end_check(p, int(cases["blocked hand-off"][1]), status="done", premise="fails")
    p.db.update_task(int(cases["blocked hand-off"][1]), result=json.dumps({"status": "blocked", "premise": "fails"}))
    Daemon(p.base).reconcile_tasks()   # it settles them before blocking tasks on dead dependencies
    for how, (cid, qid) in cases.items():
        code = p.db.task(cid)
        assert code["status"] == "queued" and not code["blocked_reason"], how
        assert json.loads(code["labels"]) == [f"premise:{qid}:unknown"], how
        assert "check the premise yourself before changing code" in code["spec"], how
    assert "ended failed" in p.db.task(cases["failed"][0])["spec"]
    assert {t["id"] for t in p.db.ready_tasks()} == {c for c, _ in cases.values()}


def test_a_code_task_reconcile_blocked_on_its_dead_check_is_released(env):
    from ttp import premise
    p = _premise_project(env)
    cid = _code(p)
    premise.tend(p.db)
    qid = p.db.one("SELECT id FROM tasks WHERE kind='question'")["id"]
    _end_check(p, qid, status="failed")
    p.db.update_task(cid, status="blocked", blocked_reason=f"dependency #{qid} failed")
    premise.tend(p.db)
    assert (p.db.task(cid)["status"], p.db.task(cid)["blocked_reason"]) == ("queued", None)
    other = _code(p)
    premise.tend(p.db)
    q2 = json.loads(p.db.task(other)["labels"])[0].split(":")[1]
    _end_check(p, int(q2), premise="holds", summary="yes")
    p.db.update_task(other, status="blocked", blocked_reason="task budget exhausted")
    premise.tend(p.db)
    assert p.db.task(other)["status"] == "blocked", "a block for another reason stays"


def test_a_pending_check_whose_code_task_ended_is_cancelled_and_a_check_is_never_lifted(env):
    from ttp import premise, tierfloor
    p = _premise_project(env)
    cid = _code(p)
    premise.tend(p.db)
    q = p.db.one("SELECT * FROM tasks WHERE kind='question'")
    assert tierfloor.reasons(q) == [], "the daemon reads its answer, not the user"
    assert tierfloor.reasons({**q, "labels": "[]"}) == ["kind question"]
    p.db.update_task(cid, status="cancelled")
    assert premise.tend(p.db) == [(cid, f"premise check #{q['id']} cancelled: #{cid} cancelled")]
    assert p.db.task(q["id"])["status"] == "cancelled"


def test_dispatch_starts_the_premise_check_not_the_code_task_it_holds(env, monkeypatch):
    p, d, runs = _floor_daemon(env, monkeypatch)
    cid = _code(p)
    d.dispatch()
    started = [k["task"]["id"] for _, k in runs]
    qid = p.db.one("SELECT id FROM tasks WHERE kind='question'")["id"]
    assert started == [qid] and cid not in started
    assert [a[3] for a, _ in runs] == ["light"]
    assert p.db.task(qid)["tier"] == "light", "neither the effort pick nor the conclusion floor raised it"


def test_the_audit_counts_code_runs_with_no_code_change_and_premise_closes(env):
    from ttp import audit
    p = _premise_project(env)
    now = time.time()
    run = "INSERT INTO runs(task,role,started,ended,status,cost_usd) VALUES(?,?,?,?,?,?)"

    def done_code(title, usd, **kw):
        t = p.db.add_task(title, "s", kind="code")
        p.db.update_task(t, status="done", spent_usd=usd, **kw)
        p.db.x(run, (t, "worker", now - 900, now - 800, "ok", usd))
        return t

    idle = done_code("premise check only", 1.25, result=json.dumps({"status": "done", "summary": "no change"}))
    idle2 = done_code("feature already there", 0.75)
    reviewed = done_code("real change", 2.0)
    p.db.add_task("review it", "s", kind="review", labels=[f"auto_review:{reviewed}"])
    dep = done_code("reviewed by dependency", 2.0)
    p.db.add_task("review dep", "s", kind="review", depends_on=[dep])
    done_code("draft PR", 2.0, result=json.dumps({"status": "done", "pr": "https://example.invalid/pr/1"}))
    closed = p.db.add_task("closed by its check", "s", kind="code", labels=["premise:99:fails"])
    p.db.update_task(closed, status="done")
    got = audit.no_change_code(p.db, now - 86400)
    assert (got["n"], got["usd"], got["premise_closed"]) == (2, 2.0, 1)
    assert [r["id"] for r in got["rows"]] == [idle, idle2]
    out = "\n".join(audit.lines(p.db, now))
    assert (f"code runs with no code change: 2, $2.00; 1 closed by a premise check before any code run: "
            f"#{idle} premise check only $1.25, #{idle2} feature already there $0.75") in out
    assert audit.report(p.db, now)["no_change_code"]["n"] == 2
    p.db.x("UPDATE tasks SET updated=? WHERE kind='code'", (now - 3 * 86400,))
    assert "code runs with no code change" not in "\n".join(audit.lines(p.db, now))
