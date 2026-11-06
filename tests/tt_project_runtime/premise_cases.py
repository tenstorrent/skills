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
    _end_check(p, qid, premise="fails", summary="Already there: cli.py:40 prints the retry line since abc123.",
               covered=[{"condition": "ttp task show prints a retry line", "at": "runtime/ttp/cli.py:40"}])
    assert premise.tend(p.db) == [(cid, f"premise fails (#{qid})")]
    code = p.db.task(cid)
    res = json.loads(code["result"])
    assert code["status"] == "done" and res["outcome"] == "rejected" and res["premise_check"] == qid
    assert res["summary"] == (f"Not needed: premise check #{qid} found the premise does not hold. "
                              "Already there: cli.py:40 prints the retry line since abc123.")
    ev = p.db.one("SELECT * FROM events WHERE kind='task_done' AND task=?", (cid,))
    assert ev["status"] == "queued" and "done, not run" in ev["text"] and "abc123" in ev["text"]
    assert not p.db.q("SELECT 1 FROM runs WHERE task=?", (cid,)), "no code run was spent"


def test_a_fails_answer_closes_the_code_task_only_with_file_line_cover_for_every_named_condition(env):
    from ttp import premise
    p = _premise_project(env)
    disk = {"condition": "check scratch on disk", "at": "runtime/ttp/checks.py:210"}
    ram = {"condition": "check scratch on the RAM disk", "at": "runtime/ttp/checks.py:180"}
    partial = {
        "one condition uncited": dict(covered=[ram, {"condition": "check scratch on disk", "at": ""}]),
        "an uncovered list": dict(covered=[ram], uncovered=["check scratch on disk"]),
        "an uncovered string": dict(covered=[ram], uncovered="check scratch on disk"),
        "no covered list": dict(),
        "an empty covered list": dict(covered=[]),
        "a cite with no line": dict(covered=[ram, {"condition": "check scratch on disk", "at": "checks.py"}]),
        "a host:port, no file": dict(covered=[ram, {"condition": "check scratch on disk", "at": "localhost:8080"}]),
        "a cite with no condition": dict(covered=[ram, {"at": "runtime/ttp/checks.py:210"}]),
        "a bare string with no cite": dict(covered=["check scratch on disk: handled"]),
        "covered not a list": dict(covered="runtime/ttp/checks.py:180"),
    }
    full = {"dicts": dict(covered=[ram, disk]),
            "strings": dict(covered=["RAM disk: runtime/ttp/checks.py:180", "on disk: checks.py:210-214"]),
            "empty uncovered": dict(covered=[ram, disk], uncovered=[])}
    answers = {**partial, **{"full " + k: v for k, v in full.items()}}
    cids = {how: _code(p) for how in answers}
    premise.tend(p.db)
    cases = {how: (cid, int(json.loads(p.db.task(cid)["labels"])[0].split(":")[1])) for how, cid in cids.items()}
    for how, (cid, qid) in cases.items():
        _end_check(p, qid, premise="fails", summary="Already exists: checks.py:180 keeps the scratch.", **answers[how])
    got = dict(premise.tend(p.db))
    for how in partial:
        cid, qid = cases[how]
        code = p.db.task(cid)
        assert got[cid] == f"premise holds (#{qid})", how
        assert code["status"] == "queued" and json.loads(code["labels"]) == [f"premise:{qid}:holds"], how
        assert "without file:line cover for every condition" in code["spec"], how
        assert "Already exists: checks.py:180 keeps the scratch." in code["spec"], how
        assert not p.db.q("SELECT 1 FROM events WHERE kind='task_done' AND task=?", (cid,)), how
    assert "uncovered: check scratch on disk" in p.db.task(cases["an uncovered list"][0])["spec"]
    assert "no file:line for: check scratch on disk" in p.db.task(cases["one condition uncited"][0])["spec"]
    for how in full:
        cid, qid = cases["full " + how]
        code = p.db.task(cid)
        assert got[cid] == f"premise fails (#{qid})", how
        assert code["status"] == "done" and json.loads(code["result"])["outcome"] == "rejected", how
    assert {t["id"] for t in p.db.ready_tasks()} == {cases[h][0] for h in partial}


def test_a_fails_answer_naming_a_missing_target_closes_the_code_task_without_cover(env):
    from ttp import premise
    p = _premise_project(env)
    answers = {"missing list": (dict(missing=["runtime/ttp/holdhook.py:_allowed"]), "fails"),
               "missing string": (dict(missing="runtime/ttp/holdhook.py"), "fails"),
               "missing but uncovered": (dict(missing=["x.py"], uncovered=["on-disk case"]), "holds"),
               "empty missing": (dict(missing=["", " "]), "holds")}
    cids = {how: _code(p) for how in answers}
    premise.tend(p.db)
    for how, cid in cids.items():
        qid = int(json.loads(p.db.task(cid)["labels"])[0].split(":")[1])
        _end_check(p, qid, premise="fails", summary="No such function at the base ref.", **answers[how][0])
    got = dict(premise.tend(p.db))
    for how, cid in cids.items():
        assert got[cid].startswith(f"premise {answers[how][1]} "), how
        assert p.db.task(cid)["status"] == ("done" if answers[how][1] == "fails" else "queued"), how


def test_the_premise_check_prompt_asks_a_fails_answer_to_cite_cover_for_every_named_condition(env):
    from ttp import premise
    p = _premise_project(env)
    _code(p)
    premise.tend(p.db)
    spec = p.db.one("SELECT spec FROM tasks WHERE kind='question'")["spec"]
    assert "every condition, case or branch the premise or the spec below names" in spec
    assert '"covered": [{"condition": "...", "at": "path/file.py:123"}]' in spec
    assert 'answer "holds" and list the rest in "uncovered"' in spec
    assert 'A "fails" with any condition uncited is read as "holds" and the code task runs.' in spec


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
    assert (got["n"], got["usd"], got["premise_closed"], got["premise_saved_usd"]) == (2, 2.0, 1, 2.0)
    assert [r["id"] for r in got["rows"]] == [idle, idle2]
    out = "\n".join(audit.lines(p.db, now))
    assert (f"code runs with no code change: 2, $2.00; 1 closed by a premise check before any code run "
            f"(about $2.00 saved at the tier's median code run): "
            f"#{idle} premise check only $1.25, #{idle2} feature already there $0.75") in out
    assert audit.report(p.db, now)["no_change_code"]["n"] == 2
    p.db.x("UPDATE tasks SET updated=? WHERE kind='code'", (now - 3 * 86400,))
    assert "code runs with no code change" not in "\n".join(audit.lines(p.db, now))


def test_a_premise_check_spec_requires_named_targets_to_exist_at_the_base_ref(env):
    from ttp import premise
    p = _premise_project(env)
    _code(p, spec="Goal: fix the hold hook.\npremise: runtime/ttp/holdhook.py's _allowed misses a form\n"
                   "Change: fix it. [note abc123]")
    premise.tend(p.db)
    spec = p.db.one("SELECT spec FROM tasks WHERE kind='question'")["spec"]
    assert "git ls-tree" in spec and "git grep" in spec and "base ref" in spec, "(a) confirm each named target exists"
    assert "adjacent" in spec and "found instead" in spec, "(b) a nearby function is no match: fails, naming it"
    assert "upstream note" in spec and "outside the plugin" in spec and "won't-fix" in spec, "(c) foreign paths"


def test_an_urgent_code_task_checks_its_premise_in_its_own_run_and_drops_a_queued_check(env):
    """An urgent code task (priority 1 or an `urgent:` line) gets no separate premise question first:
    its spec says to check the premise as its first step and it is ready at once. A question queued
    before it turned urgent is cancelled if it never started; one that started settles as usual."""
    from ttp import premise
    p = _premise_project(env)
    p1 = _code(p, priority=1)
    line = _code(p, spec="Goal: add the retry.\nurgent: the user is waiting\npremise: no retry line yet")
    routine = _code(p, priority=2)
    got = dict(premise.tend(p.db))
    assert got[p1] == got[line] == "premise checked in its own run"
    qs = p.db.q("SELECT * FROM tasks WHERE kind='question'")
    assert [json.loads(q["labels"]) for q in qs] == [[f"premise-check:{routine}"]], "only the routine one splits"
    for tid, check in ((p1, "`ttp task show` has no retry line yet"), (line, "no retry line yet")):
        t = p.db.task(tid)
        assert json.loads(t["labels"]) == ["premise:self"] and json.loads(t["depends_on"] or "[]") == []
        assert f"as your first step, check that it holds: {check}." in t["spec"]
    assert {p1, line} <= {t["id"] for t in p.db.ready_tasks()}
    assert premise.tend(p.db) == [] and p.db.task(p1)["spec"].count("as your first step") == 1, "once"
    # Turned urgent after its check was queued: the unstarted check is cancelled, the task freed.
    qid = qs[0]["id"]
    p.db.update_task(routine, priority=1)
    assert premise.tend(p.db) == [(routine, f"premise checked in its own run (#{qid} cancelled)")]
    t = p.db.task(routine)
    assert p.db.task(qid)["status"] == "cancelled" and json.loads(t["depends_on"]) == []
    assert json.loads(t["labels"]) == [f"premise:{qid}:self"] and premise.pending(t) is None
    # A check that already started is settled as usual.
    late = _code(p, priority=2)
    premise.tend(p.db)
    q2 = p.db.one("SELECT id FROM tasks WHERE kind='question' AND status='queued'")["id"]
    p.db.x("INSERT INTO runs(task,role,started,status) VALUES(?,?,?,?)", (q2, "worker", time.time(), "running"))
    p.db.update_task(late, priority=1)
    assert premise.tend(p.db) == [] and p.db.task(q2)["status"] == "queued"
    _end_check(p, q2, premise="holds", summary="no retry line")
    assert premise.tend(p.db) == [(late, f"premise holds (#{q2})")]


def test_a_check_that_rejects_the_premise_without_a_premise_key_counts_as_fails(env):
    """A check answering `"outcome": "rejected"` and no `premise` key (#6396's shape) said fails in its
    own words: with full cover its code task is closed, short of cover it still runs."""
    from ttp import premise
    p = _premise_project(env)
    cover = [{"condition": "the renamed helper", "at": "tests/x_cases.py:449"}]
    answers = {"covered": (dict(outcome="rejected", covered=cover), "done"),
               "uncited": (dict(outcome="rejected"), "queued"),
               "says holds": (dict(outcome="rejected", premise="holds", covered=cover), "queued")}
    cids = {how: _code(p) for how in answers}
    premise.tend(p.db)
    for how, cid in cids.items():
        qid = int(json.loads(p.db.task(cid)["labels"])[0].split(":")[1])
        _end_check(p, qid, summary="Nothing was lost.", **answers[how][0])
    premise.tend(p.db)
    for how, cid in cids.items():
        assert p.db.task(cid)["status"] == answers[how][1], how
    assert premise.verdict({"status": "done", "outcome": "kept"}) == ""


def _failed_check(p, check, cover=True, age_days=0.0):
    """A code task with premise `check` and its check, ended "fails" (with or without full cover)."""
    from ttp import premise
    cid = _code(p, spec=f"Goal: restore lost lines.\npremise: {check}\nSteps: compare.")
    premise.tend(p.db)
    qid = int(json.loads(p.db.task(cid)["labels"])[0].split(":")[1])
    _end_check(p, qid, premise="fails", summary="Nothing was lost: every line is in the landing.",
               **({"covered": [{"condition": "each added line", "at": "tests/x_cases.py:449"}]} if cover else {}))
    premise.tend(p.db)
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - age_days * 86400, qid))
    return cid, qid


def test_task_add_refuses_a_premise_a_check_already_proved_false_for_two_weeks(env):
    """The repro: three code tasks re-asked whether commit e72d3d2's lines survived its landing
    e734140 after check #6376 had proved they did. A new task whose premise names only commits a
    failed premise named (or, naming none, says the same words) is refused naming the check; other
    commits, a "fails" short of cover, a check older than 14 days or a stated difference pass."""
    from ttp import coordinator as coord, premise
    p = _premise_project(env)
    cid, qid = _failed_check(p, "a line commit e72d3d2 adds to x_cases.py is missing from e734140's copy")
    _failed_check(p, "a line commit 0a1b2c3 adds is missing from 4d5e6f7's copy", cover=False)
    _failed_check(p, "a line commit 9a8b7c6 adds is missing from 5d4e3f2's copy", age_days=premise.REPEAT_DAYS + 1)
    _failed_check(p, "the `ttp runs` command has no help line")

    def add(title, check, **kw):
        return coord.apply(p, [{"type": "task_add", "title": title, "kind": "code", **kw,
                                "spec": f"Goal: check it.\npremise: {check}\nSteps: compare."}])

    got = add("verify e72d3d2 survived", "any test line of e72d3d2bbbb00 lost on origin's copy")
    assert len(got) == 1 and f"premise already proved false by premise check #{qid} (for #{cid})" in got[0] \
        and "Nothing was lost" in got[0] and f"differs from #{qid}" in got[0], got
    assert add("check the help line again", "The `ttp runs` command has no help line.")[0].startswith(
        "task_add: premise already proved false"), "no commit named: the same words match"
    assert add("verify forced", "e72d3d2 lost a line", force=True)[0].startswith("task_add: premise already")
    assert not p.db.q("SELECT 1 FROM tasks WHERE title LIKE 'verify %' OR title LIKE 'check the help%'")
    assert add("verify three commits", "a line of d472bdd, 01690fb or e72d3d2 is missing") == [], "a new commit"
    assert add("verify uncovered", "a line commit 0a1b2c3 adds is lost") == [], "a fails short of cover"
    assert add("verify old", "a line commit 9a8b7c6 adds is lost") == [], "older than the window"
    assert add("verify other help", "the `ttp logs` command has no help line") == [], "other words"
    forced = add("verify e72d3d2 with force", f"e72d3d2 lost a line; differs from #{qid}: checks the cli copy",
                 force=True)
    assert forced == [] and any(f"forced past premise check #{qid}" in n for n in p.db.kv(coord.NOTES_KEY))


def _cmd_daemon(env, monkeypatch, tmp_path, cmd, **kw):
    """A recording daemon (_floor_daemon) with a scratch HOME and a code task whose premise is `cmd`."""
    for k in ("HOME", "XDG_CONFIG_HOME", "GH_CONFIG_DIR"):   # never the real ~/.config/gh or ~/.ssh
        monkeypatch.setenv(k, str(tmp_path / "scratch-home" / k.lower()))
    p, d, runs = _floor_daemon(env, monkeypatch)
    cid = _code(p, spec=f"Goal: add the retry.\npremise_cmd: {cmd}\nChange: add it.", **kw)
    return p, d, runs, cid


def _premise_ran(d, cid):
    """Wait (bounded) for task `cid`'s premise command the daemon started to end."""
    d._premise_runs[cid][0].wait(timeout=20)


def test_a_premise_command_that_exits_0_lets_the_code_task_dispatch_with_no_question(env, monkeypatch, tmp_path):
    p, d, runs, cid = _cmd_daemon(env, monkeypatch, tmp_path, f"test -d {tmp_path}")
    d.dispatch()
    assert runs == [] and cid in d._premise_runs, "held until its premise command answered"
    _premise_ran(d, cid)
    d.dispatch()
    assert [k["task"]["id"] for _, k in runs] == [cid]
    assert not p.db.q("SELECT 1 FROM tasks WHERE kind='question'"), "no model run checked the premise"


def test_a_premise_command_that_exits_1_closes_the_task_with_its_record_and_one_event(env, monkeypatch, tmp_path):
    from ttp import premise
    cmd = f"test -e {tmp_path}/landed.flag && exit 1 || exit 0"
    (tmp_path / "landed.flag").write_text("")
    p, d, runs, cid = _cmd_daemon(env, monkeypatch, tmp_path, cmd)
    d.dispatch()
    _premise_ran(d, cid)
    d.dispatch()
    t = p.db.task(cid)
    res = json.loads(t["result"])
    assert runs == [] and t["status"] == "done" and premise.CMD_FAILS in json.loads(t["labels"])
    assert res["outcome"] == "rejected" and res["premise_cmd"]["cmd"] == cmd and res["premise_cmd"]["rc"] == 1
    assert "premise command exited 1" in res["summary"]
    ev = p.db.q("SELECT * FROM events WHERE kind='task_done' AND task=?", (cid,))
    assert len(ev) == 1 and ev[0]["status"] == "queued" and "not run" in ev[0]["text"]
    assert not p.db.q("SELECT 1 FROM runs WHERE task=?", (cid,)), "no worker started"
    d.dispatch()
    assert len(p.db.q("SELECT 1 FROM events WHERE kind='task_done' AND task=?", (cid,))) == 1
    # task_add's 14-day refusal covers the same command; the audit counts the close
    hit = premise.repeat(p.db, f"Goal: again.\npremise_cmd:  {cmd}")
    assert hit and hit["id"] == cid and "premise command" in premise.rejection(hit)
    assert premise.repeat(p.db, "premise_cmd: test -d /") is None


def test_a_premise_command_with_another_exit_or_a_timeout_falls_back_to_the_light_question(env, monkeypatch, tmp_path):
    from ttp import premise
    p, d, runs, cid = _cmd_daemon(env, monkeypatch, tmp_path, "test -d / && exit 3")
    d.dispatch()
    _premise_ran(d, cid)
    d.dispatch()
    q = p.db.one("SELECT * FROM tasks WHERE kind='question'")
    code = p.db.task(cid)
    assert q and q["tier"] == "light" and [k["task"]["id"] for _, k in runs] == [q["id"]]
    assert premise.CMD_UNKNOWN in json.loads(code["labels"]) and json.loads(code["depends_on"]) == [q["id"]]
    assert "premise command `test -d / && exit 3`" in q["spec"]
    assert p.db.one("SELECT 1 FROM events WHERE kind='premise_check' AND text LIKE '%exit 3%'")
    # a timeout: killed, then the same fallback; a later dispatch never reruns it
    p2, d2, runs2, cid2 = _cmd_daemon(env, monkeypatch, tmp_path / "t", "sleep 30", priority=1)
    d2.dispatch()
    proc = d2._premise_runs[cid2][0]
    d2._premise_cmds(time.time() + premise.CMD_TIMEOUT_S + 1)
    assert proc.started.wait(10) and cid2 not in d2._premise_runs
    assert proc.proc is None or proc.proc.wait(timeout=10) is not None, "killed, nothing left running"
    assert premise.CMD_UNKNOWN in json.loads(p2.db.task(cid2)["labels"])
    assert p2.db.one("SELECT 1 FROM events WHERE kind='premise_check' AND text LIKE '%timeout%'")
    d2.dispatch()
    assert cid2 not in d2._premise_runs and [k["task"]["id"] for _, k in runs2] == [cid2], \
        "urgent: it checks its premise in its own run"
    assert "check that it holds" in p2.db.task(cid2)["spec"]


def test_a_premise_command_reruns_before_a_dispatch_after_a_long_queue_wait(env, monkeypatch, tmp_path):
    cmd = f"test -e {tmp_path}/landed.flag && exit 1 || exit 0"
    p, d, runs, cid = _cmd_daemon(env, monkeypatch, tmp_path, cmd)
    d.dispatch()
    _premise_ran(d, cid)
    d._premise_cmds(time.time())   # it passed; the task still waits in the queue (say, for a slot)
    assert d._premise_ok[cid][1] == cmd and cid not in d._premise_runs
    d._premise_cmds(time.time())
    assert cid not in d._premise_runs, "a fresh pass is not rerun"
    (tmp_path / "landed.flag").write_text("")          # its change landed while it waited
    d._premise_ok[cid] = (time.time() - 31 * 60, cmd)   # past premise.recheck_min (30)
    d.dispatch()
    assert cid in d._premise_runs
    _premise_ran(d, cid)
    d.dispatch()
    assert p.db.task(cid)["status"] == "done" and runs == [] and cid not in d._premise_ok


def test_a_premise_command_runs_only_under_the_read_only_guard(env, monkeypatch, tmp_path):
    from ttp import premise
    ok = ("git merge-base --is-ancestor 1a2b3c4 origin/main && exit 1 || exit 0", "grep -q x a.py 2>/dev/null",
          "test -f a >/dev/null 2>&1", "ttp landed --task 12", "git branch --contains 1a2b3c4", "grep -q x rm.txt")
    assert [c for c in ok if premise.guard(c)] == []
    bad = ("echo x > f", "echo x 2>f", "awk 1 a>>b", "cat a | tee b", "rm -f x", "sed -i s/a/b/ f", "mkdir -p d",
           "git fetch origin", "git -C . push", "git --git-dir=x commit -m a", "git -c a=b reset --hard",
           "git log -1; git stash", "ttp task add x", "ssh host true", "srun hostname", "ttp devq run x",
           "test -d / ||", "test -d / || true")
    assert [c for c in bad if not premise.guard(c)] == []
    flag = tmp_path / "written"
    p, d, runs, cid = _cmd_daemon(env, monkeypatch, tmp_path, f"touch {flag}")
    d.dispatch()
    assert cid not in d._premise_runs and not flag.exists(), "a refused command never runs"
    assert premise.CMD_UNKNOWN in json.loads(p.db.task(cid)["labels"])
    assert p.db.one("SELECT 1 FROM tasks WHERE kind='question'"), "it falls back to the light question"


def test_only_a_premise_cmd_line_runs_and_the_audit_estimates_what_premise_closes_saved(env):
    from ttp import audit, premise
    assert premise.command("- premise_cmd:  test -d /  \nmore") == "test -d /"
    assert premise.command("premise: test -d /") == "" and premise.line("premise_cmd: test -d /") == ""
    p = _premise_project(env)
    now = time.time()
    for usd in (1.0, 3.0, 5.0):
        t = p.db.add_task("ran", "s", kind="code", tier="standard")
        p.db.update_task(t, status="done", spent_usd=usd)
        p.db.x("INSERT INTO runs(task,role,started,status) VALUES(?,?,?,?)", (t, "worker", now, "ok"))
    for labels in ([premise.CMD_FAILS], ["premise:7:fails"]):
        t = p.db.add_task("closed", "s", kind="code", tier="standard", labels=labels)
        p.db.update_task(t, status="done")
    got = audit.no_change_code(p.db, now - 86400)
    assert (got["premise_closed"], got["premise_saved_usd"]) == (2, 6.0)
    assert "2 closed by a premise check before any code run (about $6.00 saved at the tier's median code run)" \
        in "\n".join(audit.lines(p.db, now))
