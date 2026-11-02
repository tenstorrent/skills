# Course check self-review tests (runtime/ttp/coursecheck.py: missed deadlines, the same wait, races with
# the project's own pushes, a rule against a newer user message, a corrected mistake back again) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations


def _cc(p, now=None):
    from ttp import coursecheck, push
    return coursecheck.lines(p.db, p.config(), p.state / push.DETACHED, now)


def _cc_event(p, kind, fp, task=None, ts=None, data=None):
    p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task,data) VALUES(?,?,?,?,?,?,?,?,?)",
           (ts or time.time(), "daemon", kind, fp, "normal", "x", "handled", task,
            json.dumps(data) if data is not None else None))


def _cc_msg(p, direction, kind, text, ts, handled=0):
    return p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)",
                  (ts, direction, kind, text, handled))


def test_course_self_review_adds_nothing_to_an_idle_digest(env):
    from ttp import coordinator as coord
    p = make(env)
    p.db.add_task("plain work", "s", origin="user")
    assert _cc(p) == ([], "")
    assert "Course check" not in coord.digest(p, {}, [], [])


def test_course_self_review_flags_an_obligation_that_missed_two_deadlines(env):
    from ttp import obligations as ob
    p = make(env)
    tid = p.db.add_task("build the report", "s", origin="user")
    oid = ob.add(p, "the weekly report", [tid])
    _cc_event(p, "obligation_overdue", f"obligation:{oid}:1", tid)
    _cc_event(p, "obligation_overdue", f"obligation:{oid}:reown:{tid}", tid)   # no deadline of its own
    assert _cc(p)[0] == []
    _cc_event(p, "obligation_overdue", f"obligation:{oid}:milestone:draft:1", tid)
    lines, ident = _cc(p)
    assert len(lines) == 1 and f"missed deadline: obligation {oid}" in lines[0] and "missed 2 deadlines" in lines[0]
    assert f"#{tid}" in lines[0] and ident == f"missed:{oid}"
    ob.drop(p, oid, "no longer wanted")
    assert _cc(p)[0] == []


def test_course_self_review_flags_the_same_wait_met_again_with_no_change(env):
    from ttp import stalegate
    p = make(env)
    tid = p.db.add_task("wait for the window", "s", origin="user")
    now = time.time()
    snap = {"wait_stale": {"wait": f"{tid}:1", "snap": {"probe": "test -e /tmp/ready"}}}
    _cc_event(p, "wait_stale", f"wait_stale:{tid}:1", tid, now - 3600, snap)
    _cc_event(p, "wait_stale", f"wait_stale:{tid}:2", tid, now - 600,
              {"wait_stale": {"wait": f"{tid}:2", "snap": {"probe": "other probe"}}})   # a changed probe
    _cc_event(p, "wait_stale", f"wait_stale:{tid}:0", tid, now - 5 * 86400, snap)        # outside the window
    assert _cc(p, now)[0] == []
    stalegate.note_skip(p.db, f"{tid}:1", f"wait_stale:{tid}:1b", "unchanged", now - 60)  # a raise the gate skipped
    lines = _cc(p, now)[0]
    assert len(lines) == 1 and f"same wait: #{tid}" in lines[0] and "2 times" in lines[0]
    assert "`test -e /tmp/ready`" in lines[0]
    p.db.update_task(tid, status="cancelled")
    assert _cc(p, now)[0] == []


def test_course_self_review_flags_push_checks_invalidated_by_the_projects_own_pushes(env):
    from ttp import push
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.time() - 3600

    def marker(name, **m):
        (folder / f"{name}.json").write_text(json.dumps({"id": name, **m}))

    # Task 5's checks passed, then the branch moved: once a retry while task 6 pushed, once an external move.
    marker("t5-a", task="5", started=t0, status="pushed", ended=t0 + 400, exit=0,
           retries=[{"cause": push.NON_FF, "at": t0 + 100}, {"cause": push.NON_FF, "at": t0 + 300}])
    marker("t6-a", task="6", started=t0, status="pushed", ended=t0 + 50, exit=0)
    assert _cc(p)[0] == []   # one self-inflicted move (the second had no own push in its span)
    marker("t5-b", task="5", started=t0 + 1000, status="failed", ended=t0 + 1300, exit=push.KEPT_MOVING)
    marker("t7-a", task="7", started=t0 + 900, status="pushed", ended=t0 + 1200, exit=0)
    lines, ident = _cc(p)
    assert len(lines) == 1 and "self race: task #5" in lines[0] and "2 times" in lines[0], lines
    assert "#6" in lines[0] and "#7" in lines[0] and "re-run the gate" in lines[0] and ident == "self_race:5"
    (folder / "t5-b.json").write_text("{torn")   # an unreadable marker is skipped, never raised
    assert _cc(p)[0] == []


def _cc_batch(folder, name, outcome, started, ended, tasks, target="origin/main"):
    # A push-queue batch marker as pushq/batch write it: finished, an outcome, entries, no task.
    (folder / f"{name}.json").write_text(json.dumps({
        "id": name, "kind": "batch", "status": "finished", "phase": "finished", "outcome": outcome,
        "started": started, "ended": ended, "target": target, "rounds": 1,
        "entries": [{"id": i, "task": t, "branch": f"ttp/t{t}", "head": "a" * 40} for i, t in enumerate(tasks, 1)]}))


def test_course_self_review_flags_push_queue_batches_sent_back_moved_by_own_batches(env):
    from ttp import push
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.time() - 7200
    # Task 5's approval: its batch ended 'moved' twice while another of this project's batches pushed.
    _cc_batch(folder, "batch-a", "moved", t0, t0 + 600, [5])
    _cc_batch(folder, "batch-b", "pushed", t0 + 100, t0 + 400, [6, 8])
    _cc_batch(folder, "batch-c", "moved", t0 + 1000, t0 + 1600, [5])
    _cc_batch(folder, "batch-x", "pushed", t0 + 1100, t0 + 1200, [9], target="origin/other")   # another branch
    assert _cc(p)[0] == []   # only one move had an own push to the same branch in its span
    _cc_batch(folder, "batch-d", "tip_failed", t0 + 1100, t0 + 1300, [7])   # not a push: no landing
    assert _cc(p)[0] == []
    _cc_batch(folder, "batch-e", "pushed", t0 + 1100, t0 + 1300, [7])
    lines, ident = _cc(p)
    assert len(lines) == 1 and "self race: task #5's passed push checks were" in lines[0], lines
    assert "2 times" in lines[0] and "#6" in lines[0] and "#7" in lines[0] and "#9" not in lines[0]
    assert ident == "self_race:5"


def test_course_self_review_flags_review_approvals_sent_back_as_push_conflicts_by_own_pushes(env):
    from ttp import push
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.time() - 7200

    def conflict(created, updated, batch):
        p.db.x("INSERT INTO push_queue(task,branch,head,target,status,batch,created,updated) VALUES(?,?,?,?,?,?,?,?)",
               (12, "ttp/t11", "b" * 40, "origin/main", "conflict", batch, created, updated))

    # Review #12's approval is sent back as a push conflict twice; the batch it was in also pushed others.
    conflict(t0, t0 + 900, "batch-r1")
    _cc_batch(folder, "batch-r1", "pushed", t0 + 800, t0 + 900, [13])   # its own batch is not the cause
    _cc_batch(folder, "batch-p1", "pushed", t0 + 200, t0 + 500, [14])
    conflict(t0 + 1000, t0 + 1900, "batch-r2")
    conflict(t0 - 4 * 86400, t0 - 4 * 86400 + 900, "batch-old")   # outside the 48 h window
    assert _cc(p)[0] == []
    _cc_batch(folder, "batch-p2", "pushed", t0 + 1200, t0 + 1500, [15])
    lines, ident = _cc(p)
    assert len(lines) == 1 and "self race: task #12's passed review approval was (push conflict)" in lines[0], lines
    assert "2 times" in lines[0] and "#14" in lines[0] and "#15" in lines[0] and "#13" not in lines[0]
    assert ident == "self_race:12"


def test_course_self_review_flags_a_rule_cited_against_a_newer_user_message(env):
    p = make(env)
    now = time.time()
    ask = _cc_msg(p, "out", "ask", "May I push? The charter restriction forbids pushing the release branch "
                  "to main without review.", now - 3000)
    _cc_msg(p, "in", "user", "the release branch to main: fine before any of this", now - 4000)   # older
    _cc_msg(p, "in", "user", "what is the spend today?", now - 2000)                               # unrelated
    assert _cc(p, now)[0] == []
    msg = _cc_msg(p, "in", "user", "Go ahead, ship the release branch to main today.", now - 1000)
    lines, ident = _cc(p, now)
    assert len(lines) == 1 and f"ask #{ask} cites a rule that user message #{msg}" in lines[0], lines
    assert "branch, main, release" in lines[0] and ident == f"rule_clash:ask #{ask}:{msg}"
    p.db.x("UPDATE messages SET handled=1 WHERE id=?", (ask,))   # the ask was answered
    assert _cc(p, now)[0] == []
    tid = p.db.add_task("release", "s", origin="user")
    p.db.update_task(tid, status="blocked", blocked_reason="Policy: the release branch never goes to main.")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (now - 1500, tid))
    assert f"blocked #{tid} cites a rule that user message #{msg}" in _cc(p, now)[0][0]


def test_course_self_review_flags_a_corrected_mistake_that_comes_back(env):
    from ttp import correction
    p = make(env)
    now = time.time()
    first = {"msg": 10, "ts": now - 7200, "why": "phrase", "class": ["#42"], "fix": "#50", "shown": True}
    other = {"msg": 15, "ts": now - 3600, "why": "phrase", "class": ["key:budget.cap"], "fix": "", "shown": False}
    p.db.set_kv(correction.KEY, [first, other])
    assert _cc(p, now)[0] == []   # another class
    p.db.set_kv(correction.KEY, [first, other, {"msg": 20, "ts": now - 60, "why": "phrase", "class": ["#42"],
                                                 "fix": "", "shown": False}])
    lines, ident = _cc(p, now)
    assert len(lines) == 1 and "correction #20 repeats the class (#42) of correction #10" in lines[0]
    assert "fix #50" in lines[0] and ident == "recurring:20"
    p.db.set_kv(correction.KEY, [{**first, "fix": "reason"}, other,
                                 {"msg": 20, "ts": now - 60, "why": "phrase", "class": ["#42"], "fix": ""}])
    assert _cc(p, now)[0] == []   # no fix was recorded, so none failed: correction.py covers it


def test_course_self_review_lines_are_capped_and_join_the_digest_course_check(env):
    from ttp import coordinator as coord, coursecheck, obligations as ob
    p = make(env)
    tid = p.db.add_task("deliver", "s", origin="user")
    for i in range(8):
        oid = ob.add(p, f"deliverable {i}", [tid])
        for n in (1, 2):
            _cc_event(p, "obligation_overdue", f"obligation:{oid}:{n}", tid)
    lines = _cc(p)[0]
    assert len(lines) == coursecheck.MAX_LINES
    course_check = dict(coord.digest_parts(p, {}, [], [])[0])["course_check"]
    assert course_check.startswith("## Course check") and course_check.count("- missed deadline:") == 6
    # Turned off: no lines.
    p.set_config("coordinator.course_check.missed_deadlines", 0) if hasattr(p, "set_config") else None
    if hasattr(p, "set_config"):
        assert _cc(p)[0] == []


def test_coordinator_prompt_renders_the_self_review_paragraph(env):
    from ttp import prompts
    role = " ".join(prompts.coordinator_role(make(env)).split())
    assert "Self-review, each turn: on a Course check line" in role
    assert "fix the cause in this turn (`task_add`, `schedule_set`, `config_set`, `task_update`)" in role
    assert "another status line is no answer" in role and "never by re-running the gate" in role
    text = (RUNTIME.parent / "template/prompts/coordinator.md").read_text()
    start = text.index("- Self-review, each turn")
    para = text[start:].split("\n- ", 1)[0]
    assert para.count("\n") + 1 <= 8


def test_course_self_review_skips_a_detector_on_bad_state_and_keeps_the_rest(env):
    from ttp import coursecheck, obligations as ob, correction
    p = make(env)
    p.db.set_kv(ob.KV, "torn")   # obligations state that does not decode
    now = time.time()
    p.db.set_kv(correction.KEY, [{"msg": 1, "ts": now - 90, "class": ["#7"], "fix": "#8"},
                                 {"msg": 2, "ts": now - 30, "class": ["#7"], "fix": ""}])
    said = []
    lines, _ = coursecheck.lines(p.db, p.config(), p.state / "pushes", now, said.append)
    assert len(lines) == 1 and "recurring mistake" in lines[0]
    assert len(said) == 1 and "missed skipped" in said[0]


def _cc_task(p, *args):
    """`ttp task <p> <args>`: its exit code (0 when it returned)."""
    from ttp import cli
    try:
        cli.main(["task", p.name, *args])
    except SystemExit as e:
        return e.code
    return 0


def _cc_cancelled_fix(p, now):
    """A correction class whose fix task was cancelled, and a later correction of that class."""
    from ttp import correction
    first = p.db.add_task("harness: catch the missed case", "s", kind="harness", origin="coordinator")
    p.db.x("UPDATE tasks SET status='cancelled' WHERE id=?", (first,))
    p.db.set_kv(correction.KEY, [
        {"msg": 10, "ts": now - 7200, "why": "phrase", "class": ["#42"], "fix": f"#{first}", "shown": True},
        {"msg": 20, "ts": now - 60, "why": "phrase", "class": ["#42"], "fix": "", "shown": False}])
    return first


def test_a_cancelled_class_fix_linked_to_a_later_done_fix_stops_the_recurring_mistake_line(env, capsys):
    """`ttp task superseded <cancelled> <task|sha>` records the class's later fix: (a) a done fix task or
    a harness commit ends the recurring-mistake line; (b) a linked fix no longer done brings it back."""
    p = make(env)
    now = time.time()
    first = _cc_cancelled_fix(p, now)
    assert "recurring mistake" in _cc(p, now)[0][0]   # no link: unchanged
    fix = p.db.add_task("harness: the class fix, done in another task", "s", kind="harness", origin="coordinator")
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (fix,))
    assert _cc_task(p, "superseded", str(first), f"#{fix}") == 0
    assert f"fixed by task #{fix}" in capsys.readouterr().out
    assert _cc(p, now)[0] == []   # (a)
    p.db.x("UPDATE tasks SET status='queued' WHERE id=?", (fix,))   # (b) requeued: not done any more
    assert "recurring mistake" in _cc(p, now)[0][0]
    # A commit of the harness counts too.
    sha = subprocess.run(["git", "-C", str(p.harness), "rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert _cc_task(p, "superseded", str(first), sha[:12]) == 0
    assert _cc(p, now)[0] == []


def test_superseded_on_a_cancelled_class_fix_refuses_open_failed_foreign_unknown_or_missing_fixes(env, capsys):
    """(b) an open fix task is refused and the line stays; (d) unknown ids, another project's task, a failed
    or cancelled fix, a missing sha or a non-cancelled original with a task link exit non-zero, recording
    nothing; (c) a done task's superseded keeps its own checks."""
    p = make(env)
    now = time.time()
    first = _cc_cancelled_fix(p, now)
    open_fix = p.db.add_task("harness: still running", "s", kind="harness", origin="coordinator")
    failed = p.db.add_task("harness: failed", "s", kind="harness", origin="coordinator")
    p.db.x("UPDATE tasks SET status='failed' WHERE id=?", (failed,))
    for args in ([str(first), f"#{open_fix}"], [str(first), str(open_fix)], [str(first), f"#{failed}"],
                 [str(first), f"#{first}"], [str(first), "#9999"], [str(first), f"other#{open_fix}"],
                 [str(first), "deadbeefcafe1234"], [str(first), "not-a-sha"], ["9999", f"#{open_fix}"],
                 [str(first), f"#{open_fix}", "--ref", "main"]):
        assert _cc_task(p, "superseded", *args) not in (0, None), args
    assert p.db.q("SELECT * FROM task_class_fix") == []
    assert "recurring mistake" in _cc(p, now)[0][0]
    out = capsys.readouterr()
    assert "not done" in out.err + out.out and "of project other" in out.err + out.out
    # (c) a done task: the landing checks as before, never a class-fix link.
    done = p.db.add_task("code: shipped", "s", kind="code", origin="coordinator")
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (done,))
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (open_fix,))
    assert _cc_task(p, "superseded", str(done), f"#{open_fix}") not in (0, None)
    assert p.db.q("SELECT * FROM task_class_fix") == []
    assert "class is recorded" not in capsys.readouterr().out


def test_a_correction_followed_only_by_a_report_only_llm_schedule_keeps_its_line_open(env):
    """A correction whose only follow-up is an llm schedule citing it (or its fix task) keeps a line naming
    the missing model-free gate; a command schedule, a done harness or code task citing it (not the fix task
    or a schedule run), or a recorded later fix closes it; an uncited llm schedule is no follow-up."""
    from ttp import correction, schedule as sched
    p = make(env)
    now = time.time()
    fix = p.db.add_task("harness: watch for the missed case", "s", kind="harness", origin="coordinator")
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (fix,))
    p.db.set_kv(correction.KEY, [{"msg": 10, "ts": now - 7200, "why": "phrase", "class": ["#42"],
                                  "fix": f"#{fix}", "shown": True}])
    sched.upsert(p.db, "daily-audit", "llm", "1d", None, True, None, "audits the project", {"spec": "report"})
    assert _cc(p, now)[0] == []   # cites neither: no follow-up of this correction
    sched.upsert(p.db, "missed-case-watch", "llm", "1h", None, True, None, f"watch for #{fix}'s case",
                 {"spec": "report what you see"})
    lines, ident = _cc(p, now)
    assert len(lines) == 1 and "report-only follow-up: correction #10 (fix #" in lines[0], lines
    assert "missed-case-watch" in lines[0] and "model-free gate" in lines[0] and "command schedule" in lines[0]
    assert "detector with a test" in lines[0] and ident == "report_only:10"
    sched.upsert(p.db, "missed-case-watch", "llm", "1h", None, True, None, "w",
                 {"spec": "check what correction #10 asked for"})
    assert "report-only follow-up" in _cc(p, now)[0][0]   # cited by the correction id too
    # Not gates: a schedule's own run, the fix task itself, an open task, a task of another kind.
    run = p.db.add_task("[missed-case-watch] w", "correction #10", kind="harness", origin="schedule")
    other = p.db.add_task("look into correction #10", "s", kind="work", origin="coordinator")
    open_ = p.db.add_task("harness: detector for correction #10", "s", kind="harness", origin="coordinator")
    p.db.x(f"UPDATE tasks SET status='done' WHERE id IN ({run},{other})")
    p.db.x("UPDATE tasks SET spec='correction #10' WHERE id=?", (fix,))
    assert "report-only follow-up" in _cc(p, now)[0][0]
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (open_,))   # a done detector task closes it
    assert _cc(p, now)[0] == []
    p.db.x("UPDATE tasks SET status='queued' WHERE id=?", (open_,))
    sched.upsert(p.db, "missed-case-check", "command", "1h", None, True, None, "gate for correction #10",
                 {"command": "true"})
    assert _cc(p, now)[0] == []   # a command schedule closes it
    sched.set_enabled(p.db, "missed-case-check", False)
    assert "report-only follow-up" in _cc(p, now)[0][0]   # an off gate checks nothing
    p.db.x("UPDATE tasks SET status='cancelled' WHERE id=?", (fix,))
    later = p.db.add_task("harness: the detector", "s", kind="harness", origin="coordinator")
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (later,))
    assert _cc_task(p, "superseded", str(fix), f"#{later}") == 0
    assert _cc(p, now)[0] == []   # a recorded later fix closes it
    cfg = p.config()
    cfg.setdefault("coordinator", {}).setdefault("course_check", {})["recurring"] = 0
    p.db.x("DELETE FROM task_class_fix")
    from ttp import coursecheck, push
    assert coursecheck.lines(p.db, cfg, p.state / push.DETACHED, now)[0] == []   # 0 turns it off


_CC_ASK = ("May I push the widgets release branch? The charter policy keeps the widgets release branch frozen: check every run before "
           "pushing, only the runs listed at https://github.com/acme-org/widgets.\n\nMy recommendation: approve "
           "the frozen branch push.")


def test_an_open_ask_and_a_new_request_sharing_only_common_words_boilerplate_and_a_url_part_is_no_answer(env):
    """(1) A new request sharing only common words ('check', 'every', 'runs', 'only', 'before'), the ask's
    boilerplate ('recommendation') and a URL part (an org name) is no answer: no rule clash line and no
    correction flagged. (2) A reply sharing the ask's own terms, or a bare yes, is one."""
    from ttp import correction
    p = make(env)
    now = time.time()
    ask = _cc_msg(p, "out", "ask", _CC_ASK, now - 3000)
    new = _cc_msg(p, "in", "user", "Actually, check every runs of https://github.com/acme-org/other only before "
                  "lunch, with your recommendation for docs.acme-org.io/guide.", now - 1000)
    assert not correction.answered_open_ask(p.db.one("SELECT text FROM messages WHERE id=?", (new,))["text"], _CC_ASK)
    assert _cc(p, now)[0] == []
    rows = p.db.q("SELECT * FROM messages WHERE id=?", (new,))
    assert correction.flag(p.db, p.config(), rows, now) == {}
    # (2) a real answer still matches.
    real = _cc_msg(p, "in", "user", "Actually, the release branch may unfreeze: pushing widgets is fine now.", now - 500)
    lines, ident = _cc(p, now)
    assert len(lines) == 1 and f"ask #{ask} cites a rule that user message #{real}" in lines[0], lines
    assert "branch, pushing, release, widgets" in lines[0]
    for yes in ("yes", "No.", "go ahead", "Approved, do it"):
        assert correction.answered_open_ask(yes, _CC_ASK), yes
    assert correction.answered_open_ask("Unfreeze the release branch and push widgets.", _CC_ASK)
    assert not correction.answered_open_ask("Please check the runs before every merge.", _CC_ASK)


def test_recurring_drops_a_stored_open_ask_match_the_message_does_not_answer(env):
    """(3) Correction records stored as answering an open ask ('answers open ask #<id>', class 'ask:<id>')
    whose message shares only common words with it count for nothing in the recurring-mistake check, with
    no state rewrite; a record whose message does answer the ask still counts."""
    from ttp import correction
    p = make(env)
    now = time.time()
    ask = _cc_msg(p, "out", "ask", _CC_ASK, now - 9000)
    one = _cc_msg(p, "in", "user", "Check every runs before #42 only, per github.com/acme-org/tools.", now - 7200)
    two = _cc_msg(p, "in", "user", "Only check every #42 runs before the next one.", now - 60)
    recs = [{"msg": one, "ts": now - 7200, "why": f"answers open ask #{ask}", "class": ["#42"], "fix": "#50",
             "shown": True},
            {"msg": two, "ts": now - 60, "why": "answers open ask", "class": [f"ask:{ask}", "#42"], "fix": ""}]
    p.db.set_kv(correction.KEY, recs)
    assert _cc(p, now)[0] == []
    assert p.db.kv(correction.KEY) == recs   # nothing rewritten
    p.db.x("UPDATE messages SET text=? WHERE id=?", ("No: the widgets release branch stays frozen for #42.", two))
    p.db.x("UPDATE messages SET text=? WHERE id=?", ("Unfreeze the widgets release branch for #42.", one))
    lines = [x for x in _cc(p, now)[0] if "recurring mistake" in x]
    assert len(lines) == 1 and f"correction #{two} repeats the class (#42) of correction #{one}" in lines[0], lines
    # A record citing no ask, or an ask that is gone, is left as it was.
    p.db.set_kv(correction.KEY, [{**recs[0], "why": "phrase"}, {**recs[1], "class": ["ask:99999", "#42"]}])
    assert _cc(p, now)[1].endswith(f"recurring:{two}")


def test_a_later_harness_task_is_a_corrections_class_fix_only_when_it_is_about_that_correction(env):
    """(4) A harness task added after a correction is its class fix only when its title or spec cites the
    message, names the class anchor or shares its subject words; the next unrelated harness task is not."""
    from ttp import correction
    p = make(env)
    then = time.time() - 600
    mid = _cc_msg(p, "in", "user", "You missed that the nightly widgets build of #42 hung on key cache.gc_days",
                  then)
    rec = {"msg": mid, "ts": then - 1, "why": "phrase 'you missed'", "class": ["#42", "key:cache.gc_days"]}
    unrelated = p.db.add_task("upstream: chat relay drops a reply", "the relay loses a message on restart",
                              kind="harness", origin="coordinator")
    p.db.add_task("harness: #421 and cache.gc_days_max", "s", kind="harness", origin="coordinator")
    p.db.add_task("plain work on the nightly widgets build hang", "s", kind="work", origin="coordinator")
    assert unrelated and correction._later_fix(p.db, rec) == ""
    for title, spec in (("harness: catch a hung build", f"from correction #{mid}"),
                        ("harness: watch #42's builds", "s"), ("harness: check the cache.gc_days default", "s"),
                        ("upstream: nightly widgets build hang detector", "s")):
        tid = p.db.add_task(title, spec, kind="harness", origin="coordinator")
        assert correction._later_fix(p.db, rec) == f"#{tid}", title
        p.db.x("UPDATE tasks SET kind='work', title='x' WHERE id=?", (tid,))
