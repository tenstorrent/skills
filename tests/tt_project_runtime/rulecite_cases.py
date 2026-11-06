# The Standing rules index in the coordinator's digest, the uncited-hold Course check flag and the daily
# rule-cites metric (runtime/ttp/rulecite.py, coordinator.digest_parts and apply, audit.lines). Run from
# test_tt_project_runtime.py.

_RC_CHARTER = """# Charter

## Goals
Ship the widget.

## Policies
- Delivery: a change is reviewed, then one quick smoke run, then pushed (user msg #41).

## Restrictions
- Never push to the default branch (user msg #7).
- Keep content generic.
"""


def _rc_project(env):
    from ttp import obligations as ob
    p = make(env)
    p.charter_path.write_text(_RC_CHARTER)
    p.add_memory("Reply in the chat the order came from (user msg #12).", kind="preference", standing=True)
    mid = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)",
                 (time.time(), "in", "user", "build the report by noon", 1))
    tid = p.db.add_task("build the report", "s", origin="user")
    ob.add_order(p, "build the report", mid, [tid], action_class="change")
    return p, mid, tid


def test_the_standing_rules_index_lists_rules_orders_and_holds_with_stable_ids_and_cites_in_every_digest(env):
    from ttp import coordinator as coord, rulecite
    p, mid, tid = _rc_project(env)
    coord.pause_resource(p, "board-a", True, reason="kept for a demo", by="user")
    coord.pause_resource(p, "board-b", True, reason="my own retry", by="coordinator")
    parts, _ = coord.digest_parts(p, {}, [], [])
    index = dict(parts)[rulecite.SECTION]
    rows = index.splitlines()[1:]
    assert any(r.startswith("- R-") and "Never push to the default branch" in r and "(msg #7)" in r for r in rows), index
    assert any(r.startswith("- P-") and "smoke run" in r and "(msg #41)" in r for r in rows), index
    assert any(r.startswith("- M-") and "(msg #12)" in r for r in rows), index
    assert any(r.startswith("- o") and f"user order (msg #{mid})" in r for r in rows), index
    assert "- H-board-a hold set by the user: kept for a demo" in rows, index
    assert not any("board-b" in r for r in rows), "a pause the coordinator set is no standing rule"
    # Goals are not indexed; ids stay the same while their rule does, and change with it.
    assert "widget" not in index
    again = dict(coord.digest_parts(p, {}, [], [])[0])[rulecite.SECTION]
    assert again == index
    p.charter_path.write_text(_RC_CHARTER.replace("Keep content generic.", "Keep content generic and short."))
    ids = {rid for rid, _ in rulecite.entries(p)}
    old = {r.split()[1] for r in rows}
    assert len(old - ids) == 1 and len(ids - old) == 1
    # The index is bounded: many standing entries are cut with a count line, within INDEX_CHARS.
    for n in range(80):
        p.add_memory(f"Standing rule number {n} about topic {n} (user msg #{100 + n}).", kind="preference",
                     standing=True)
    big = rulecite.index_lines(p)
    assert len(big) <= rulecite.MAX_LINES + 2 and sum(len(x) + 1 for x in big) <= rulecite.INDEX_CHARS + 200
    assert big[-1].endswith("the charter and memory carry them whole)")


def test_the_index_reaches_a_new_session_seed_and_its_delta_only_when_a_rule_changes(env):
    from ttp import coordinator as coord, coordsession as cs, rulecite
    p, _, _ = _rc_project(env)
    full = dict(coord.digest_parts(p, {}, [], [])[0])
    assert rulecite.SECTION in full and "Never push to the default branch" in full[rulecite.SECTION]
    before = cs.sections(list(full.items()))
    same = cs.sections(list(dict(coord.digest_parts(p, {}, [], [])[0]).items()))
    assert before[rulecite.SECTION] == same[rulecite.SECTION]   # no ages: an idle wake does not resend it
    p.charter_path.write_text(_RC_CHARTER + "- Never run load on a held host (user msg #50).\n")
    after = cs.sections(list(dict(coord.digest_parts(p, {}, [], [])[0]).items()))
    assert after[rulecite.SECTION] != before[rulecite.SECTION]


def test_an_uncited_hold_is_applied_flagged_once_in_the_next_course_check_and_counted(env):
    from ttp import coordinator as coord, rulecite
    p, _, tid = _rc_project(env)
    other = p.db.add_task("run the full suite", "s", origin="user")
    hold = {"type": "task_update", "id": tid, "status": "blocked", "waits_on": "until:2h",
            "why": "hold the push until the full suite also passes"}   # a bare time: no cite
    problems = coord.apply(p, [hold], turn=1)
    assert problems == []   # applied (as a deferral): flag only, never refused
    cc = dict(coord.digest_parts(p, {}, [], [])[0])["course_check"]
    assert cc.count("uncited hold:") == 1 and f"#{tid} task_update blocked" in cc, cc
    # A replay of the same turn records nothing twice.
    coord.apply(p, [hold], turn=1)
    assert len(rulecite._records(p.db)) == 1
    # Cited holds pass: an index id, a user msg, a safety rule, a dependency or a waits_on.
    rid = next(r for r, line in rulecite.entries(p) if line.startswith("Policy:"))
    cited = [
        {"type": "task_update", "id": other, "start_after": "1h", "why": f"per {rid}."},
        {"type": "task_add", "title": "smoke run", "spec": "Do: x", "start_after": "1h",
         "why": "the user asked for it tonight (user msg #41)"},
        {"type": "task_add", "title": "device pass", "spec": "Do: x", "start_when": "true",
         "why": "safety: device hold until the reset ends"},
        {"type": "task_add", "title": "report part 2", "spec": "Do: x", "depends_on": [tid],
         "why": f"needs the table #{tid} writes"},
        {"type": "resource_pause", "resource": "board-c", "paused": True, "until": "2h", "reason": "required checks fail on it"},
    ]
    assert coord.apply(p, cited, turn=2) == []
    assert "course_check" not in dict(coord.digest_parts(p, {}, [], [])[0])   # turn 1's flag was shown once
    # Bypass attempts that still cite nothing are flagged: a pause whose reason only says pause, a made-up
    # rule id, a bare task number with no needs word, a deferral with no why.
    uncited = [
        {"type": "resource_pause", "resource": "board-d", "paused": True, "until": "2h", "reason": "pause it for now"},
        {"type": "task_update", "id": other, "start_after": "4h", "why": "per R-ffff"},
        {"type": "task_add", "title": "follow-up", "spec": "Do: x", "start_after": "2h", "why": f"after #{tid} looks fine"},
        {"type": "task_add", "title": "later work", "spec": "Do: x", "start_after": "3h"},
    ]
    assert coord.apply(p, uncited, turn=3) == []
    cc = dict(coord.digest_parts(p, {}, [], [])[0])["course_check"]
    assert cc.count("- uncited hold:") == 4 and "1 more" in cc, cc
    # Not hold-type: a priority change, a lifted pause.
    n = len(p.db.q("SELECT id FROM events WHERE kind=?", (rulecite.HOLD_EVENT,)))
    coord.apply(p, [{"type": "task_update", "id": other, "priority": 1},
                    {"type": "resource_pause", "resource": "board-d", "paused": False}], turn=4)
    assert len(p.db.q("SELECT id FROM events WHERE kind=?", (rulecite.HOLD_EVENT,))) == n
    m = rulecite.metric(p)
    assert m["holds"] == 10 and m["cited"] == 5 and m["cited_pct"] == 50, m


def test_the_daily_audit_shows_the_rule_cites_metric_and_corrections_naming_a_standing_rule(env):
    from ttp import audit, coordinator as coord, correction, rulecite
    p, _, tid = _rc_project(env)
    assert rulecite.audit_line(p) == ""
    assert coord.apply(p, [{"type": "task_update", "id": tid, "start_after": "1h", "why": "safety: required checks"},
                           {"type": "task_add", "title": "t", "spec": "Do: x", "start_after": "2h", "why": "later"}],
                       turn=1) == []
    now = time.time()
    hit = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)",
                 (now, "in", "user", "why no push? my delivery rule is review, smoke run, then pushed", 1))
    miss = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)",
                  (now, "in", "user", "that is wrong, the colour is blue", 1))
    p.db.set_kv(correction.KEY, [{"msg": hit, "ts": now, "why": "phrase 'why no'"},
                                 {"msg": miss, "ts": now, "why": "phrase 'that is wrong'"}])
    m = rulecite.metric(p)
    assert m["holds"] == 2 and m["cited"] == 1 and m["naming_rule"] == [hit] and m["corrections"] == 2, m
    lines = audit.lines(p.db, p=p)
    line = next(x for x in lines if x.startswith("rule cites (24 h)"))
    assert "2 hold-type actions, 1 cite" in line and "(50%, target 100%)" in line and f"1 of 2 (#{hit})" in line
    assert audit.report(p.db, p=p)["rule_cites"]["cited_pct"] == 50
    # Older than the window: not counted.
    assert rulecite.metric(p, now + 2 * 86400)["holds"] == 0
