# Routine coordinator lane tests (coordroute.py: routine wakes on the chat model, shadow first) and the
# GLM-arm ask-Opus switches (glmassist.py) of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there): helpers, fixtures and imports come from that file. Add new
# routine-lane and advisor tests here.
from __future__ import annotations


def _cr_main(*args):
    """cli.main's exit code: 0 when it returns."""
    from ttp import cli
    try:
        cli.main(list(args))
    except SystemExit as e:
        return e.code or 0
    return 0


def _cr_db(env, **state):
    from ttp import coordroute, coordsession
    p = make(env)
    p.db.set_kv(coordsession.KEY, {"id": "s1"})
    p.db.set_kv(coordroute.KEY, {"last_opus": time.time() - 60, **state})
    return p


def _cr_cfg(**over):
    return {"coordinator": {"routine": {"mode": "on", "provider": "fake", "model": "m", **over}}}


def test_coordroute_is_off_by_default_and_off_without_a_lane_model():
    from ttp import coordroute
    assert coordroute.settings({})["mode"] == "off"
    s = coordroute.settings({"coordinator": {"routine": {"mode": "on"}}})
    assert s["mode"] == "off" and "no lane model" in s["why"]
    s = coordroute.settings({"coordinator": {"routine": {"mode": "bogus", "model": "m"}}})
    assert s["mode"] == "off" and "unknown mode" in s["why"]
    s = coordroute.settings(_cr_cfg())
    assert (s["mode"], s["provider"], s["model"], s["effort"]) == ("on", "fake", "m", "max")
    assert coordroute.settings({"coordinator": {"routine": {"mode": "shadow"}},
                                "route": {"glm_work": {"model": "w"}}})["model"] == "w"


def test_coordroute_release_flag_turns_on_shadow_only_and_the_projects_own_mode_wins(monkeypatch):
    from ttp import coordroute, rollout
    monkeypatch.setattr(rollout, "on", lambda flag, project, raw: flag == coordroute.FLAG)
    monkeypatch.setattr(rollout, "own_setting", lambda raw, key: False)
    cfg = coordroute.ring_gate({}, "demo", {})
    assert cfg["coordinator"]["routine"]["mode"] == "shadow"
    cfg = coordroute.ring_gate(_cr_cfg(mode="on"), "demo", {})
    assert cfg["coordinator"]["routine"]["mode"] == "on"
    monkeypatch.setattr(rollout, "own_setting", lambda raw, key: True)   # the project's own "off" stands
    assert "routine" not in coordroute.ring_gate({}, "demo", {}).get("coordinator", {})


def test_coordroute_mode_is_the_users_setting_alone():
    from ttp import coordinator
    assert "coordinator.routine.mode" in coordinator.NEEDS_USER
    assert "glm_coord" in __import__("ttp.rollout", fromlist=["FLAGS"]).FLAGS


def test_coordroute_classify_sends_anything_but_routine_events_to_the_coordinators_session(env):
    from ttp import coordroute
    p = _cr_db(env)
    cfg = _cr_cfg()
    ev = lambda kind, sev="normal", task=None: {"kind": kind, "severity": sev, "task": task}
    assert coordroute.classify(p.db, cfg, evs=[ev("task_done")]) == (True, [])
    plan = p.db.add_task("plan it", "s", kind="plan", tier="standard")
    review = p.db.add_task("review it", "s", kind="review", tier="standard")
    urgent = p.db.add_task("hot", "urgent: now\ndo it", kind="work", tier="light")
    cases = {
        "user or relayed message": {"msgs": [1]},
        "urgent message": {"urgent": [1]},
        "effort trigger": {"triggers": ["ask"]},
        "wake due": {"due": "heartbeat"},
        "must-act item tied to an order": {"must": "o12 promise overdue"},
        "event ask_answered": {"evs": [ev("ask_answered")]},
        "event charter_retry": {"evs": [ev("charter_retry")]},
        "event something_new": {"evs": [ev("something_new")]},
        "high event": {"evs": [ev("task_done", "high")]},
        f"plan #{plan} done": {"evs": [ev("task_done", task=plan)]},
        f"review #{review} task_failed": {"evs": [ev("task_failed", task=review)]},
        f"urgent task #{urgent}": {"evs": [ev("task_done", task=urgent)]},
        "task #99999 not found": {"evs": [ev("task_done", task=99999)]},
        "blocker sweep": {"sweep_event": {"id": 1}},
        "session rerun": {"rerun": True},
    }
    for want, kw in cases.items():
        routine, why = coordroute.classify(p.db, cfg, **kw)
        assert not routine and any(w.startswith(want) for w in why), (want, why)
    # The session's own needs: not started, a heartbeat, a turn limit, a suspension, an escalation.
    p.db.set_kv(coordroute.KEY, {"last_opus": time.time() - 5 * 3600})
    assert "heartbeat" in coordroute.classify(p.db, cfg)[1]
    p.db.set_kv(coordroute.KEY, {"last_opus": time.time() - 60, "turns": [{"ts": time.time()}] * 12})
    assert "lane turn limit" in coordroute.classify(p.db, cfg)[1]
    p.db.set_kv(coordroute.KEY, {"last_opus": time.time() - 60, "escalate": {"why": "x"}})
    assert "lane escalated" in coordroute.classify(p.db, cfg)[1]
    p.db.set_kv(coordroute.KEY, {"last_opus": time.time() - 60, "suspended": {"why": "x"}})
    assert coordroute.classify(p.db, cfg)[1][0].startswith("lane suspended")
    p.db.set_kv(coordroute.KEY, {})
    assert "no coordinator wake on record" in coordroute.classify(p.db, cfg)[1]


def test_coordroute_filter_actions_allows_only_the_lane_list_and_escalates_on_its_noop():
    from ttp import coordroute
    acts = [{"type": "task_add", "title": "t", "spec": "s", "why": "w"},
            {"type": "task_update", "id": 3, "priority": 2, "why": "w"},
            {"type": "look", "at": "30m", "text": "x"}, {"type": "noop"},
            {"type": "obligation", "key": "o1", "owners": [3]},
            {"type": "reply", "chat": "c1", "text": "hi"}, {"type": "notify", "text": "x"},
            {"type": "ask_user", "text": "?"}, {"type": "charter_update", "section": "Goals", "text": "x"},
            {"type": "config_set", "key": "a", "value": 1}, {"type": "memory_add", "text": "x"},
            {"type": "task_add", "title": "d", "spec": "s", "tier": "deep"},
            {"type": "task_add", "title": "p", "spec": "s", "model": "x"},
            {"type": "task_update", "id": 3, "status": "cancelled"},
            {"type": "task_update", "id": 3, "wake_now": True},
            {"type": "obligation", "key": "o1", "due": "2026-01-01T00:00:00Z"}]
    ok, refused, esc = coordroute.filter_actions(acts)
    assert ok == [0, 1, 2, 3, 4] and [i for i, _ in refused] == list(range(5, 16)) and esc == ""
    ok, refused, esc = coordroute.filter_actions([{"type": "noop", "why": "escalate: needs a judgement call"}])
    assert (ok, refused, esc) == ([], [], "needs a judgement call")
    assert coordroute.filter_actions("nope")[1] == [(-1, "no action list")]


def test_coordroute_record_turn_reads_back_once_and_suspends_on_a_left_must_act_item(env):
    from ttp import coordroute
    p = _cr_db(env)
    acts = [{"type": "task_update", "id": 7, "priority": 2, "why": "unblocked by #6"}]
    coordroute.record_turn(p.db, 41, {"event_kinds": ["task_done"]}, acts, [0], [], "")
    text = coordroute.digest_lines(p.db, lambda t: "10:00")
    assert "## Routine lane turns since your last wake" in text
    assert "run 41 at 10:00 on task_done: task_update #7 (unblocked by #6)" in text
    coordroute.opus_started(p.db, 42)
    assert coordroute.digest_lines(p.db) == "", "read back once"
    st = coordroute.record_turn(p.db, 43, {}, [{"type": "reply", "chat": "c1", "text": "x"}], [],
                                [(0, "reply is not a lane action")], "", summary="it wanted to answer")
    assert st["escalate"]["why"] == "reply is not a lane action"
    text = coordroute.digest_lines(p.db)
    assert "Escalated to you by lane run 43" in text and "Nothing of that turn was applied" in text
    assert coordroute.classify(p.db, _cr_cfg())[0] is False
    st = coordroute.record_turn(p.db, 44, {}, [], [], [], "", left_must=1)
    assert "left a must-act item" in st["suspended"]["why"]
    assert "The lane is suspended" in coordroute.digest_lines(p.db)
    assert coordroute.resume(p.db) and not coordroute.resume(p.db)
    assert not coordroute.state(p.db).get("suspended")


def test_coordroute_suspends_when_escalations_or_rejections_run_high():
    from ttp import coordroute
    calm = [{"run": i, "actions": [{"type": "noop"}]} for i in range(10)]
    assert coordroute.check_suspend({"turns": calm}) == ""
    jumpy = calm[:5] + [{"run": i, "escalated": "x"} for i in range(5)]
    assert "escalated 50%" in coordroute.check_suspend({"turns": jumpy})
    sloppy = [{"run": i, "actions": [{"type": "task_add"}], "problems": 1 if i < 3 else 0} for i in range(10)]
    assert "rejected-action rate 30%" in coordroute.check_suspend({"turns": sloppy, "opus_rejected_pct": 10})
    assert coordroute.check_suspend({"turns": sloppy, "opus_rejected_pct": 28}) == ""


def test_coordroute_shadow_pairs_compare_and_report(env, capsys):
    from ttp import cli, coordroute
    p = make(env)
    coordroute.shadow_started(p.state, 10, 11, [])
    coordroute.shadow_side(p.state, 10, "opus", [{"type": "task_update", "id": 3, "why": "a"}, {"type": "noop"}],
                           "ok", 0.8, 30)
    rec = coordroute.shadow_side(p.state, 10, "glm", [{"type": "task_update", "id": 3, "why": "b"}], "ok", 0.05, 40)
    assert rec["compare"]["agree"] and rec["compare"]["both"] == 1
    coordroute.shadow_started(p.state, 20, 21, [])
    coordroute.shadow_side(p.state, 20, "opus", [{"type": "task_add", "title": "Fix it"}], "ok", 1.0)
    rec = coordroute.shadow_side(p.state, 20, "glm", [{"type": "reply", "chat": "c1", "text": "x"}], "ok", 0.1)
    assert not rec["compare"]["agree"] and rec["compare"]["refused"] == 1
    assert rec["compare"]["opus_only"] == ["task_add fix it"] and rec["compare"]["glm_only"] == ["reply"]
    rep = coordroute.report(p.db, p.state)
    assert rep["shadow"]["compared"] == 2 and rep["shadow"]["agree"] == 0.5
    assert rep["shadow"]["cost_usd"] == {"opus": 1.8, "glm": 0.15}
    out = coordroute.text(rep, "demo")
    assert "same actions 50%" in out and "run 20: coordinator only task_add fix it" in out
    assert _cr_main("stats", "demo", "--coord-routine") == 0 and "2 compared" in capsys.readouterr().out


def test_coordroute_resume_lane_is_refused_inside_a_run(env, monkeypatch, capsys):
    from ttp import cli, coordroute
    p = make(env)
    p.db.set_kv(coordroute.KEY, {"suspended": {"why": "x"}})
    monkeypatch.setenv("TTP_RUN_ID", "5")
    assert _cr_main("stats", "demo", "--coord-routine", "--resume-lane") != 0
    assert "your own terminal" in capsys.readouterr().err
    assert coordroute.state(p.db).get("suspended")
    monkeypatch.delenv("TTP_RUN_ID")
    assert _cr_main("stats", "demo", "--coord-routine", "--resume-lane") == 0
    assert "routine lane resumed" in capsys.readouterr().out and not coordroute.state(p.db).get("suspended")


def _cr_event(p, kind="task_done", task=None):
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
           (time.time(), "test", kind, "normal", f"{kind} happened", "queued", task))


def _cr_project(env, monkeypatch, mode):
    from ttp.daemon import Daemon
    p = make(env)
    _sessions(env, monkeypatch)
    p.set_config("coordinator.debounce_s", 0)
    p.set_config("coordinator.event_hold_s", 0)
    p.set_config("coordinator.wake_gap_s", 0)
    p.set_config("coordinator.routine", {"mode": mode, "provider": "fake", "model": "lane-model"})
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    d = Daemon(p.base)
    p.db.post("in", "first", chat="c1")
    assert _cs_wake(p, d), "the first wake is the coordinator's own session"
    return p, d


def _cr_note(p, rid):
    return json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"])


def test_coordroute_a_live_lane_turn_applies_allowed_actions_and_the_coordinator_reads_them_back(env, monkeypatch):
    from ttp import coordroute
    p, d = _cr_project(env, monkeypatch, "on")
    tid = p.db.add_task("later", "do it later", kind="work", tier="light")
    _cr_event(p)
    monkeypatch.setenv("TTP_FAKE_ACTIONS", json.dumps(
        [{"type": "task_update", "id": tid, "priority": 2, "why": "its dependency finished"}]))
    rid = _cs_wake(p, d)
    note = _cr_note(p, rid)
    assert note.get(coordroute.LANE) == "routine" and "session" not in note.get("turn", {}), note
    assert coordroute.route_of(note)["model"] == "lane-model"
    assert p.db.task(tid)["priority"] == 2
    assert not p.db.q("SELECT id FROM events WHERE status='queued' AND source='test'")
    st = coordroute.state(p.db)
    assert st["turns"][-1]["actions"][0]["target"] == f"#{tid}"
    # A lane action off its list: nothing applied, the events stay for the coordinator's own session.
    _cr_event(p)
    monkeypatch.setenv("TTP_FAKE_ACTIONS", json.dumps(
        [{"type": "task_update", "id": tid, "priority": 1, "why": "x"}, {"type": "notify", "text": "hey"}]))
    rid = _cs_wake(p, d)
    assert _cr_note(p, rid).get(coordroute.LANE) == "routine"
    assert p.db.task(tid)["priority"] == 2, "an escalated turn applies nothing"
    assert p.db.q("SELECT id FROM events WHERE status='queued' AND source='test'")
    assert coordroute.state(p.db).get("escalate")
    monkeypatch.setenv("TTP_FAKE_ACTIONS", json.dumps([{"type": "noop"}]))
    rid = _cs_wake(p, d)
    note = _cr_note(p, rid)
    assert not coordroute.lane_of(note) and note["turn"].get("session"), "the escalation is the coordinator's"
    prompt = (p.runs / str(rid) / "prompt.md").read_text()
    assert "## Routine lane turns since your last wake" in prompt and "its dependency finished" in prompt
    assert "Escalated to you by lane run" in prompt


def test_coordroute_shadow_mode_records_the_lane_beside_the_coordinator_and_applies_nothing(env, monkeypatch):
    from ttp import coordroute
    p, d = _cr_project(env, monkeypatch, "shadow")
    tid = p.db.add_task("later", "do it later", kind="work", tier="light")
    _cr_event(p)
    monkeypatch.setenv("TTP_FAKE_ACTIONS", json.dumps(
        [{"type": "task_update", "id": tid, "priority": 4, "why": "w"}]))
    shadow_id = _cs_wake(p, d)   # the newest coordinator run: the shadow beside the coordinator's own wake
    shadow = _cr_note(p, shadow_id)
    assert coordroute.lane_of(shadow) == "shadow" and shadow["events"] == [], "the shadow handles no event"
    rid = shadow[coordroute.SHADOW]["of"]
    own = _cr_note(p, rid)
    assert not coordroute.lane_of(own) and own["turn"].get("session"), "the wake is the coordinator's own"
    _finish_runs(p, d)
    rec = json.loads(coordroute.shadow_path(p.state, rid).read_text())
    assert rec["opus"]["status"] == "ok" and rec["glm"]["status"] == "ok" and rec["compare"]["agree"]
    assert p.db.task(tid)["priority"] == 4, "the coordinator's own turn applied it once"
    assert coordroute.report(p.db, p.state)["shadow"]["compared"] == 1
    wk = coordroute.report(p.db, p.state)["wakes"]
    assert (wk["routine"], wk["shadow_started"]) == (1, 1) and wk["judged"] >= 1, wk


def test_coordroute_a_must_act_blocks_own_instructions_never_make_a_wake_the_coordinators(env):
    """The block's fixed text says "re-order or start its work": only its items may tie a wake to an order."""
    from ttp import coordroute, mustact, obligations
    p = _cr_db(env)
    tid = p.db.add_task("waits", "s", kind="work", tier="light")
    now = time.time()
    obligations._raise(p.db, now, "qw:1", mustact.QUEUE_KIND, "#%d waited 40 min on the checks queue" % tid, tid,
                       {"must_act": {"key": f"qwait-{tid}-checks", "chat": "all", "tasks": [tid],
                                     "kind": mustact.QUEUE_KIND}}, severity="normal")
    qid = p.db.one("SELECT id FROM events WHERE fingerprint='qw:1'")["id"]
    must = mustact.block(p.db, [qid])
    assert "re-order" in must, "the fixture must carry the block's own instructions"
    routine, why = coordroute.classify(p.db, _cr_cfg(), evs=[p.db.one("SELECT * FROM events WHERE id=?", (qid,))],
                                       must=must)
    assert routine and why == [], why
    obligations._raise(p.db, now, "pr:1", obligations.PROMISE_KIND, "promise o7 overdue", tid,
                       {"must_act": {"key": "o7", "chat": "c1", "tasks": [tid]}})
    pid = p.db.one("SELECT id FROM events WHERE fingerprint='pr:1'")["id"]
    why = coordroute.classify(p.db, _cr_cfg(), must=mustact.block(p.db, [qid, pid]))[1]
    assert "must-act item tied to an order or promise" in why


def test_coordroute_wakes_are_counted_by_reason_with_the_shadows_they_started(env, capsys):
    from ttp import coordroute
    p = _cr_db(env)
    now = time.time()
    coordroute.wake_seen(p.state, 1, False, ["effort trigger: user message, failed review", "event ask_answered"],
                         now=now - 30)
    coordroute.wake_seen(p.state, 2, False, ["urgent task #12"], now=now - 20)
    coordroute.wake_seen(p.state, 3, False, ["urgent task #13"], now=now - 15)
    coordroute.wake_seen(p.state, 4, True, [], "started", now=now - 10)
    coordroute.wake_seen(p.state, 5, True, [], "busy", now=now - 5)
    coordroute.wake_seen(p.state, 6, False, ["heartbeat"], now=now - 9 * 86400)
    wk = coordroute.report(p.db, p.state, now=now)["wakes"]
    assert (wk["judged"], wk["routine"], wk["shadow_started"], wk["shadow_busy"]) == (5, 2, 1, 1)
    by = {r["reason"]: (r["wakes"], r["alone"]) for r in wk["why"]}
    assert by["urgent task #"] == (2, 2) and by["trigger user message"] == (1, 0)
    assert by["trigger failed review"] == (1, 0) and "heartbeat" not in by, by
    assert wk["why"][0]["reason"] == "urgent task #", "the commonest reason first"
    out = coordroute.text(coordroute.report(p.db, p.state, now=now))
    assert "coordinator wakes judged: 5, routine 2 (shadow started 1, skipped while another ran 1)" in out
    assert "urgent task # 2/2" in out


def _cr_opus_fix(p, tid, run=900):
    """A tt-buddy-modelled turn: the coordinator's own session already made the harness fix and queued the audit."""
    from ttp import coordroute
    coordroute.record_opus_turn(
        p.db, run, {"event_kinds": ["task_done"]},
        [{"type": "task_add", "title": "harness: fix the stale resource probe", "why": "the probe held work past its end"},
         {"type": "task_update", "id": tid, "priority": 1, "why": "the fix landed"},
         {"type": "task_update", "id": 4242, "status": "queued", "why": "no such task"}, {"type": "noop"}],
        applied=[0, 1], problems=["task 4242 does not exist"], summary="queued the probe fix")


def test_coordroute_recent_actions_list_the_coordinators_fix_with_outcomes_and_honour_the_setting(env):
    from ttp import coordroute
    p = make(env)
    assert coordroute.recent_actions(p.db, 20) == "", "nothing recorded: no section"
    assert coordroute.settings(_cr_cfg())["recent_actions"] == coordroute.RECENT_ACTIONS == 20
    assert coordroute.settings(_cr_cfg(recent_actions=3))["recent_actions"] == 3
    _cr_opus_fix(p, 7)
    text = coordroute.recent_actions(p.db, 20)
    assert "## Recent coordinator actions (the last 3, oldest first)" in text and "Already done" in text
    assert "coordinator run 900: task_add harness fix the stale resource probe" in text
    assert text.count("-> applied") == 2 and text.count("-> rejected") == 1, text
    assert "had rejected: task 4242 does not exist" in text
    assert "noop" not in text, "a noop is no action"
    assert "task_update #7" in text
    last = coordroute.recent_actions(p.db, 1)
    assert "(the last 1, oldest first)" in last and "task_update #4242" in last and "task_update #7" not in last
    assert coordroute.recent_actions(p.db, 0) == "", "recent_actions 0 turns the view off"
    # An applied turn with nothing to show records nothing.
    n = len(coordroute.state(p.db)["turns"])
    coordroute.record_opus_turn(p.db, 901, {}, [{"type": "noop"}], [0])
    assert len(coordroute.state(p.db)["turns"]) == n


def test_coordroute_the_coordinators_turns_never_judge_the_lane_or_push_its_turns_out(env):
    from ttp import coordroute
    p = make(env)
    for i in range(coordroute.KEEP_TURNS + 5):
        _cr_opus_fix(p, 7, run=1000 + i)
    note = {"events": [], "event_kinds": ["task_done"]}
    st = coordroute.record_turn(p.db, 5, note, [{"type": "task_update", "id": 7, "why": "w"}], [0], [], "")
    for i in range(coordroute.KEEP_TURNS + 5):
        _cr_opus_fix(p, 7, run=2000 + i)
    turns = coordroute.state(p.db)["turns"]
    assert [t for t in turns if t.get("by") != coordroute.OPUS] and \
        sum(1 for t in turns if t.get("by") == coordroute.OPUS) == coordroute.KEEP_TURNS
    assert [t["run"] for t in coordroute._lane_turns(coordroute.state(p.db))] == [5]
    assert coordroute.check_suspend(coordroute.state(p.db)) == "", "rejected coordinator actions are not the lane's"
    assert coordroute.report(p.db, p.state)["live"]["turns"] == 1
    assert "lane run 5: task_update #7" in coordroute.recent_actions(p.db, 1000)
    coordroute._save(p.db, {**st, "suspended": {"why": "x"}})
    assert coordroute.resume(p.db)
    assert [t.get("by") for t in coordroute.state(p.db)["turns"]] == [coordroute.OPUS] * coordroute.KEEP_TURNS


def test_coordroute_lane_digest_swaps_the_collapsed_views_for_the_whole_ones(env):
    from ttp import coordroute
    p = make(env)
    _cr_opus_fix(p, 7)
    parts = [("charter", "## Charter"), ("tasks", "## Open tasks: (as last turn)"), ("asks", "(as last turn)"),
             ("routine_lane", "## Routine lane turns since your last wake"), ("events", "## New events")]
    whole = [("tasks", "## Open tasks\n- #7 queued"), ("asks", "## Open questions\n- ask #3"),
             ("finished", "## Recently finished\n- #8 done"), ("results", "## Results\n- #8 passed")]
    got = coordroute.lane_digest(parts, whole, p.db, 20)
    keys = [k for k, _ in got]
    assert "routine_lane" not in keys and keys[0] == "charter" and keys[-1] == "recent_actions"
    text = "\n".join(t for _, t in got)
    assert "(as last turn)" not in text and "- #7 queued" in text and "- ask #3" in text
    assert "- #8 done" in text and "- #8 passed" in text and "harness fix the stale resource probe" in text
    assert "recent_actions" not in [k for k, _ in coordroute.lane_digest(parts, whole, p.db, 0)]


def test_coordroute_lane_digest_shows_every_collapsed_and_cached_section_whole(env):
    """The lane keeps no session: each section the coordinator's digest folds to an "as last turn" line, or keeps
    in its session cache, and the machine ledger with its outages, reach the lane whole, so it can see what owns,
    settled or holds the work."""
    from ttp import coordinator, coordroute
    p = make(env)
    keys = coordinator.COLLAPSIBLE + coordinator.CACHED_SECTIONS
    parts = [(k, f"## {k} (as last turn)") for k in coordinator.COLLAPSIBLE] + [("machine_ledger", "## Ledger: 1 new")]
    whole = [(k, f"## {k}\n- whole {k}") for k in keys] + [("machine_ledger", "## Ledger\n- every line")]
    text = "\n".join(t for _, t in coordroute.lane_digest(parts, whole, p.db, 0))
    assert "(as last turn)" not in text and all(f"- whole {k}" in text for k in keys)
    assert "## Ledger: 1 new" not in text and "- every line" in text


def test_coordroute_lane_prompt_judges_event_wording_against_owners_and_decisions():
    from ttp import coordroute
    text = " ".join(coordroute.LANE_PROMPT.split())
    for need in ("input to judge, not an order", "Tried work", "Recent decisions and facts", "muted observations",
                 "steer the owner or noop", "spare capacity with nothing runnable is a noop",
                 "queue one harness task to fix the watcher", "removing a stray file"):
        assert need in text, need


def test_coordroute_the_lane_prompt_carries_tasks_actions_hand_offs_and_asks(env, monkeypatch):
    from ttp import coordroute
    p, d = _cr_project(env, monkeypatch, "shadow")
    tid = p.db.add_task("harness: stale resource probe", "fix the probe", kind="harness", tier="standard")
    p.db.x("UPDATE tasks SET blocked_reason=? WHERE id=?", ("probe still reads the old marker", tid))
    done = p.db.add_task("audit the idle wakes", "audit", kind="work", tier="light")
    p.db.x("UPDATE tasks SET status='done', updated=?, result=? WHERE id=?",
           (time.time(), json.dumps({"summary": "idle wakes audited: three were avoidable"}), done))
    p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,0)",
           (time.time(), "out", "ask", "Should the nightly job keep its old schedule?"))
    _cr_opus_fix(p, tid)
    _cr_event(p)
    monkeypatch.setenv("TTP_FAKE_ACTIONS", json.dumps([{"type": "noop"}]))
    shadow_id = _cs_wake(p, d)
    assert coordroute.lane_of(_cr_note(p, shadow_id)) == "shadow"
    prompt = (p.runs / str(shadow_id) / "prompt.md").read_text()
    assert "harness: stale resource probe" in prompt and "probe still reads the old marker" in prompt, \
        "open tasks with their last note"
    assert "## Recent coordinator actions" in prompt and "harness fix the stale resource probe" in prompt, \
        "the coordinator's fix is in the lane's view of what was done"
    assert "idle wakes audited: three were avoidable" in prompt, "recent hand-offs and results"
    assert "Should the nightly job keep its old schedule?" in prompt, "pending asks"
    assert "## Routine lane turns since your last wake" not in prompt
    _finish_runs(p, d)
    # recent_actions 0: the section is gone and the rest of the prompt stays.
    p.set_config("coordinator.routine", {"mode": "shadow", "provider": "fake", "model": "lane-model", "recent_actions": 0})
    _cr_event(p)
    rid = _cs_wake(p, d)
    prompt = (p.runs / str(rid) / "prompt.md").read_text()
    assert "## Recent coordinator actions" not in prompt and "Should the nightly job keep its old schedule?" in prompt


def test_coordroute_applied_coordinator_turns_are_recorded_for_the_lane(env, monkeypatch):
    from ttp import coordroute
    p, d = _cr_project(env, monkeypatch, "shadow")
    tid = p.db.add_task("later", "do it later", kind="work", tier="light")
    _cr_event(p)
    monkeypatch.setenv("TTP_FAKE_ACTIONS", json.dumps(
        [{"type": "task_update", "id": tid, "priority": 3, "why": "the dependency finished"}]))
    _cs_wake(p, d)
    _finish_runs(p, d)
    opus = [t for t in coordroute.state(p.db)["turns"] if t.get("by") == coordroute.OPUS]
    assert opus and opus[-1]["actions"][0]["target"] == f"#{tid}" and "outcome" not in opus[-1]["actions"][0]
    assert f"task_update #{tid} (the dependency finished) -> applied" in coordroute.recent_actions(p.db, 20)


def test_coordroute_report_covers_a_since_until_window(env, capsys):
    from ttp import coordroute
    p = make(env)
    folder = p.state / coordroute.SHADOW_DIR
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for i, age in enumerate((5, 3, 1)):
        (folder / f"{i + 1}.json").write_text(json.dumps(
            {"ts": now - age * 86400, "compare": {"agree": i == 2, "glm_ok": True, "opus_only": [], "glm_only": []}, "opus": {}, "glm": {}}))
    assert coordroute.report(p.db, p.state, days=7, now=now)["shadow"]["compared"] == 3
    assert coordroute.report(p.db, p.state, now=now, since=now - 2 * 86400)["shadow"]["compared"] == 1
    assert coordroute.report(p.db, p.state, now=now, since=now - 4 * 86400, until=now - 2 * 86400)["shadow"]["compared"] == 1
    assert _cr_main("stats", "demo", "--coord-routine", "--since", "2000-01-01") == 0
    assert "3 compared" in capsys.readouterr().out
    assert _cr_main("stats", "demo", "--coord-routine", "--since", "2999-01-01") == 0
    assert "3 compared" not in capsys.readouterr().out


def test_coordroute_lane_rows_never_count_as_the_coordinators_own_turn(env):
    from ttp import coordroute
    p = make(env)
    for note in ({}, {coordroute.LANE: "routine"}, {coordroute.SHADOW: {"of": 1}}):
        p.db.x("INSERT INTO runs(role,status,started,note) VALUES('coordinator','running',?,?)",
               (time.time(), json.dumps(note)))
    own = p.db.q("SELECT id FROM runs WHERE role='coordinator' AND " + coordroute.OWN_SQL)
    assert [r["id"] for r in own] == [1]
    assert [coordroute.lane_of_row(r) for r in p.db.q("SELECT note FROM runs ORDER BY id")] == ["", "routine", "shadow"]



@pytest.mark.parametrize("caller", ["wakemode.latest", "wakemode.today_counts", "ctxfork.coord_model",
                                    "ctxfork.coord_shape", "jevuse.mean_turn_cost", "quietgate._turn_usd",
                                    "wakesettle.median_usd"])
def test_coordinator_turn_readers_skip_a_newer_lane_or_shadow_turn(env, caller):
    from ttp import coordroute, ctxfork, jevuse, quietgate, timefmt, wakemode, wakesettle
    p = make(env)
    now = time.time()
    system = p.state / "coord-system.txt"
    system.write_text("the coordinator's system prompt")

    def turn(model, usd, note, ago):
        rid = p.db.x("INSERT INTO runs(role,provider,model,started,ended,status,cost_usd,note) "
                     "VALUES('coordinator','claude',?,?,?,'ok',?,?)", (model, now - ago, now - ago + 30, usd, json.dumps(note)))
        d = p.runs / str(rid)
        d.mkdir(parents=True, exist_ok=True)
        argv = ["claude", "--model", model, "--json-schema", "{}", "--append-system-prompt-file", str(system)]
        (d / "run.json").write_text(json.dumps({"argv": argv}))
        return rid
    own = turn("opus", 1.0, {"turn": {"session": {"mode": "resume"}}}, 120)
    turn("glm-x", 0.02, {coordroute.SHADOW: {"of": own}}, 60)
    turn("glm-x", 0.02, {coordroute.LANE: "routine"}, 30)
    got = {"wakemode.latest": lambda: wakemode.latest(p.db)[0],
           "wakemode.today_counts": lambda: wakemode.today_counts(p.db, timefmt.home(p), now=now),
           "ctxfork.coord_model": lambda: ctxfork.coord_model(p),
           "ctxfork.coord_shape": lambda: ctxfork.coord_shape(p)["run"],
           "jevuse.mean_turn_cost": lambda: jevuse.mean_turn_cost(p.db, p.config(), now=now),
           "quietgate._turn_usd": lambda: quietgate._turn_usd(p.db),
           "wakesettle.median_usd": lambda: wakesettle.median_usd(p.db, now)}[caller]()
    today = {"resume": 1, "new": 0, "off": 0} if timefmt.today(now - 120, timefmt.home(p)) == timefmt.today(now, timefmt.home(p)) \
        else {"resume": 0, "new": 0, "off": 0}
    assert got == {"wakemode.latest": "resume", "wakemode.today_counts": today, "ctxfork.coord_model": "opus",
                   "ctxfork.coord_shape": own}.get(caller, 1.0), got


@pytest.mark.parametrize("caller", ["course.idle_turns", "audit.coordinator_turns", "unblock.triggers_line",
                                    "costlevers.wake_tools_report"])
def test_coordinator_turn_reports_skip_a_newer_lane_or_shadow_turn(env, caller):
    """The turn reports count the coordinator's own wakes only: a newer shadow or lane turn changes
    none of their counts and does not break the idle streak."""
    from ttp import audit, coordroute, costlevers, course, unblock
    p = make(env)
    now = time.time()
    note = {"decided": 0, "wake_due": "idle", "triggers": ["task_blocked: #12"]}

    def turn(ago, note):
        rid = p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd,note) "
                     "VALUES('coordinator','claude',?,?,'ok',1.0,?)", (now - ago, now - ago + 30, json.dumps(note)))
        d = p.runs / str(rid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "output.jsonl").write_text("\n".join(json.dumps({"message": {"content": [
            {"type": "tool_use", "id": f"c{i}", "name": "Bash", "input": {}}]}}) for i in range(9)) + "\n")
        return rid

    own = [turn(240, note), turn(200, note)]
    turn(120, {coordroute.SHADOW: {"of": own[0]}})   # a shadow of the newest own turn
    turn(60, {coordroute.LANE: "routine"})           # a live lane turn, newest of all
    since = now - 3600
    got = {"course.idle_turns": lambda: course.idle_turns(p.db, 2),
           "audit.coordinator_turns": lambda: audit.coordinator_turns(p.db, since),
           "unblock.triggers_line": lambda: unblock.triggers_line(p.db, since),
           "costlevers.wake_tools_report": lambda: costlevers.wake_tools_report(p, since, now + 10)}[caller]()
    want = {"course.idle_turns": {"n": 2, "usd": 2.0, "hours": pytest.approx(40 / 3600), "wakes": {"idle": 2}},
            "audit.coordinator_turns": {"n": 2, "usd": 2.0, "all_usd": 4.0, "noop": 2, "noop_usd": 2.0,
                                        "idle": 2, "idle_usd": 2.0, "starve": 0, "starve_usd": 0.0, "logged": 2},
            "unblock.triggers_line": "triggered 2 (100%, $2.00), routine 0 (0%, $0.00); "
                                     "per trigger: task_blocked 2",
            "costlevers.wake_tools_report": {"wakes": 2, "heavy": 2, "usd": 2.0, "heavy_usd": 2.0, "max": 9}}
    assert got == want[caller], (caller, got)


def test_the_audit_spreads_all_coordinator_spend_over_finished_tasks(env):
    """The finished-tasks line spreads the window's whole coordinator spend, lane and shadow turns
    included, over the finished tasks, while the turns line counts the coordinator's own wakes."""
    from ttp import audit, coordroute
    p = make(env)
    now = time.time()
    run = "INSERT INTO runs(role,started,status,cost_usd,note) VALUES('coordinator',?,'ok',1.0,?)"
    p.db.x(run, (now - 200, json.dumps({"decided": 0})))          # the coordinator's own turn
    p.db.x(run, (now - 100, json.dumps({coordroute.LANE: "routine"})))
    t = p.db.add_task("t", "s", kind="code", tier="standard", origin="user")
    p.db.update_task(t, status="done", spent_usd=2.0)
    out = "\n".join(audit.lines(p.db, now))
    assert "coordinator turns: 1, $1.00 of own wakes" in out, out
    assert "coordinator $2.00 in the window ($2.00 per finished task)" in out, out


# The GLM-arm ask-Opus switches (glmassist.py) ----------------------------------------------------------------

def test_glmassist_switches_are_off_by_default_and_told_to_the_run_only_when_on():
    from ttp import codearm, glmassist, glmguard
    assert glmassist.for_run({}) is None and glmassist.prompt_lines(None) == ""
    s = glmassist.for_run({"route": {"glm_work": {"advisor": True}}})
    assert s["advisor"] and not s["plan_first"] and (s["max_calls"], s["max_minutes"]) == (3, 10.0)
    assert "ttp advise" in glmassist.prompt_lines(s) and "--plan" not in glmassist.prompt_lines(s)
    s = glmassist.for_run({"route": {"glm_work": {"plan_first": True, "advisor": {"on": False}}}})
    assert s["plan_first"] and not s["advisor"] and "ttp advise --plan" in glmassist.prompt_lines(s)
    s = glmassist.settings({"route": {"glm_work": {"advisor": {"on": True, "max_calls": 99, "max_minutes": -1}}}})
    assert (s["max_calls"], s["max_minutes"]) == (20, 0.0)
    assert "advise" not in codearm.contract() + glmguard.deny_list(), "the arm's prompt names advise only when it is on"


def _ga_reply(answer="do X then Y", cost=0.25, seen=None):
    def run(argv, text, timeout, cwd):
        if seen is not None:
            seen.append((argv, text, timeout, cwd))
        return {"result": answer, "total_cost_usd": cost, "usage": {"input_tokens": 100, "output_tokens": 20}}
    return run


def test_glmassist_ask_respects_its_caps_logs_each_call_and_books_claude_spend(env, tmp_path):
    from ttp import glmassist
    p = make(env)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(ValueError, match="runs only in a GLM-arm run"):
        glmassist.ask(run_dir, "q", cwd=str(tmp_path), run=_ga_reply(), binary="claude")
    glmassist.arm(run_dir, glmassist.for_run({"route": {"glm_work": {"advisor": {"on": True, "max_calls": 2}}}}))
    seen = []
    row = glmassist.ask(run_dir, "which file?", cwd=str(tmp_path), db=p.db, source="advisor:5",
                        run=_ga_reply(seen=seen), binary="claude")
    assert row["ok"] and row["answer"] == "do X then Y" and row["kind"] == "ask"
    argv = seen[0][0]
    assert argv[argv.index("--model") + 1] == "opus" and argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert "--no-session-persistence" in argv and seen[0][3] == str(tmp_path)
    spend = p.db.one("SELECT provider, usd, source FROM ledger ORDER BY rowid DESC LIMIT 1")
    assert (spend["provider"], spend["source"]) == ("claude", "advisor:5") and abs(spend["usd"] - 0.25) < 1e-9
    with pytest.raises(ValueError, match="plan-first is off"):
        glmassist.ask(run_dir, "plan", cwd=str(tmp_path), plan=True, run=_ga_reply(), binary="claude")
    glmassist.ask(run_dir, "again", cwd=str(tmp_path), run=_ga_reply(), binary="claude")
    with pytest.raises(ValueError, match="its limit"):
        glmassist.ask(run_dir, "third", cwd=str(tmp_path), run=_ga_reply(), binary="claude")
    assert [r["q"] for r in glmassist.calls(run_dir)] == ["which file?", "again"]
    # Minutes count too, a failed call's included.
    other = tmp_path / "run2"
    other.mkdir()
    glmassist.arm(other, glmassist.for_run({"route": {"glm_work": {"advisor": {"on": True, "max_minutes": 1}}}}))
    clock = iter([0.0, 120.0])

    def boom(argv, text, timeout, cwd):
        raise RuntimeError("endpoint down")
    row = glmassist.ask(other, "q", cwd=str(tmp_path), run=boom, binary="claude", now=lambda: next(clock))
    assert not row["ok"] and "endpoint down" in row["error"] and row["minutes"] == 2.0
    assert glmassist.hours(other) == pytest.approx(2 / 60)
    with pytest.raises(ValueError, match="minutes are used up"):
        glmassist.ask(other, "q2", cwd=str(tmp_path), run=_ga_reply(), binary="claude")


def test_glmassist_plan_first_holds_the_push_until_a_plan_is_on_record(env, tmp_path):
    from ttp import glmassist
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert glmassist.push_refusal(str(run_dir)) is None, "no switches: nothing held"
    glmassist.arm(run_dir, glmassist.for_run({"route": {"glm_work": {"plan_first": True}}}))
    assert "plan-first" in glmassist.push_refusal(str(run_dir))
    with pytest.raises(ValueError, match="advisor is off"):
        glmassist.ask(run_dir, "q", cwd=str(tmp_path), run=_ga_reply(), binary="claude")
    seen = []
    row = glmassist.ask(run_dir, "add a flag to foo", cwd=str(tmp_path), plan=True, run=_ga_reply("1. edit foo", seen=seen),
                        binary="claude")
    assert row["kind"] == "plan" and row["ok"] and seen[0][1].startswith(glmassist.PLAN)
    assert glmassist.push_refusal(str(run_dir)) is None
    with pytest.raises(ValueError, match="already has its plan"):
        glmassist.ask(run_dir, "again", cwd=str(tmp_path), plan=True, run=_ga_reply(), binary="claude")


def test_glmassist_cli_advise_refuses_outside_an_armed_run_and_answers_inside_one(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, glmassist
    p = make(env)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    assert _cr_main("advise", "which", "file?") != 0 and "GLM-arm run" in capsys.readouterr().err
    glmassist.arm(run_dir, glmassist.for_run({"route": {"glm_work": {"advisor": True}}}))
    stand_in = tmp_path / "claude"
    stand_in.write_text("#!/bin/sh\ncat >/dev/null\necho '{\"result\": \"look at foo.py\", \"total_cost_usd\": 0.1}'\n")
    stand_in.chmod(0o755)
    monkeypatch.setenv(glmassist.BIN_ENV, str(stand_in))
    assert _cr_main("advise", "which", "file?") == 0 and "look at foo.py" in capsys.readouterr().out
    assert glmassist.calls(run_dir)[0]["q"] == "which file?"


def test_glmassist_a_glm_work_run_is_launched_with_its_switches_in_its_note_and_prompt(env, monkeypatch):
    from ttp import configwho, glmassist
    from ttp import daemon as dmod
    p = make(env)
    p.set_config("core_provider", "claude")
    _route_config(p, route=None)
    p.set_config("route.glm_work", {"on": True, "model": _RT_MODEL, "holdout": 0, "advisor": True},
                 by=configwho.by("user", via="cli"))
    d = dmod.Daemon(p.base)
    prov, _tier, note = _route_dispatch(p, d, monkeypatch, title="sum the logs", kind="work", tier="standard")
    assert prov == "chat" and note[glmassist.KEY]["advisor"] and not note[glmassist.KEY]["plan_first"]
    # Off: the note carries nothing.
    p.set_config("route.glm_work", {"on": True, "model": _RT_MODEL, "holdout": 0},
                 by=configwho.by("user", via="cli"))
    d = dmod.Daemon(p.base)
    prov, _tier, note = _route_dispatch(p, d, monkeypatch, title="count the files", kind="work", tier="standard")
    assert prov == "chat" and glmassist.KEY not in note


def test_num_in_as_float_as_int():
    from ttp.util import as_float, as_int, num_in

    assert num_in(5, 1, 10, 2.0) == 5.0 and num_in(0, 1, 10, 2.0) == 2.0
    assert num_in(True, 0, 10, 2.0) == 2.0 and num_in("5", 1, 10, 2.0) == 2.0
    assert as_float("1.5", 0) == 1.5 and as_float(None, 3) == 3 and as_float("x", 3) == 3
    assert as_int("7") == 7 and as_int(None) is None and as_int("x") is None


def test_coordroute_resume_leaves_no_old_escalation_to_suspend_the_lane_again_at_once(env):
    """The lane's suspend rule judges only turns after a resume: the escalations that suspended it never count
    again, and new ones suspend it again."""
    from ttp import coordroute
    p = _cr_db(env)
    for i in range(coordroute.SUSPEND_MIN):
        st = coordroute.record_turn(p.db, i, {}, [], [], [], "needs a judgement call")
    assert "escalated" in st["suspended"]["why"]
    assert coordroute.resume(p.db)
    st = coordroute.record_turn(p.db, 100, {}, [{"type": "noop"}], [0], [], "")
    assert not st.get("suspended"), "a clean turn after the resume"
    for i in range(coordroute.SUSPEND_MIN):
        st = coordroute.record_turn(p.db, 200 + i, {}, [], [], [], "needs a judgement call")
    assert "escalated" in st["suspended"]["why"], "new escalations suspend it again"


def test_coordinator_model_and_status_read_the_own_turn_not_a_newer_lane_or_shadow_turn(env):
    from ttp import coordmodel, coordroute, coordstats, timefmt, web
    p = make(env)
    p.set_config("core_provider", "claude")
    now = time.time()

    def turn(provider, model, effort, status, note, ago):
        return p.db.x("INSERT INTO runs(role,provider,model,effort,started,ended,status,cost_usd,note) "
                      "VALUES('coordinator',?,?,?,?,?,?,0.1,?)",
                      (provider, model, effort, now - ago, now - ago + 30, status, json.dumps(note)))
    turn("claude", "opus", "xhigh", "ok", {"turn": {"session": {"mode": "resume"}}}, 120)
    turn("chat", "glm-x", "", "error", {coordroute.SHADOW: {"of": 1}}, 60)
    turn("chat", "glm-x", "", "error", {coordroute.LANE: "routine"}, 30)
    line = coordmodel.line(p.config(), p.db, p)
    assert "last turn" in line and "MISMATCH" not in line, line
    assert web.health(p, p.db, now=now)["coordinator"]["last_status"] == "ok"
    days = coordstats.report(p.db, p.runs, timefmt.home(p), 1, now=now)["by_day"]
    assert all(not d["mode_off"] for d in days), days
    turn("claude", "sonnet", "high", "ok", {}, 10)   # the coordinator's own turn ran otherwise
    assert "MISMATCH: it ran with model sonnet, effort high" in coordmodel.line(p.config(), p.db, p)


def test_coordroute_a_due_promise_its_owners_end_and_a_due_look_to_tell_a_chat_are_the_coordinators(env):
    """Model-free escalate triggers: an open promise due within the hour or overdue, a task_done of a task
    owning one (its receipt is out of lane), and a due look tied to an order or a chat. A look skipped
    unchanged, or one about routine checks, stays routine."""
    from ttp import coordroute, obligations as ob
    p = _cr_db(env)
    cfg = _cr_cfg()
    now = time.time()
    owner = p.db.add_task("ship it", "s", kind="work", tier="standard")
    mid = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)",
                 (now, "out", "reply", "I'll post the result here.", 1))
    ob.record_promises(p, [(mid, "c1", "I'll post the result here.")], {owner: "c1"}, now=now)
    o = ob.open_promises(p.db)[0]
    due = o["promise"]["due"]
    ev = {"kind": "task_done", "severity": "normal", "task": owner}
    routine, why = coordroute.classify(p.db, cfg, evs=[ev], now=due - 4 * 3600)
    assert not routine and f"task #{owner} owns a promise" in why and not any("due" in w for w in why), why
    assert coordroute.classify(p.db, cfg, now=due - 4 * 3600) == (True, [])
    assert f"promise {o['id']} due soon" in coordroute.classify(p.db, cfg, now=due - 600)[1]
    assert f"promise {o['id']} overdue" in coordroute.classify(p.db, cfg, now=due + 60)[1]
    looks = lambda line: [("looks", "## Looks you set (due now)\n" + line + "\nSet your next look with a `look` action.")]
    for line in ("- 10:31 (coordinator): send the answer to chat c1 once the deploy is live",
                 "- 10:31 (coordinator): o12 (due 14:15): check the task landed",
                 "- 10:31 (coordinator, read early with this turn): reply to the user's question"):
        routine, why = coordroute.classify(p.db, cfg, parts=looks(line), now=due - 4 * 3600)
        assert not routine and "due look tied to an order, a promise or a chat" in why, (line, why)
    for line in ("- 10:31 (coordinator): check #12's checks finished and requeue its review",
                 "- 10:31 skipped, its tasks, asks and push tip unchanged: tell chat c1 the result"):
        assert coordroute.classify(p.db, cfg, parts=looks(line), now=due - 4 * 3600) == (True, []), line


def test_coordroute_an_idle_answer_on_a_wake_that_needs_an_action_escalates(env):
    """A must-act item or a stall-class event needs an action: only noops and looks is an escalation, in the
    live lane (the note's needs) and in the shadow record; a routine wake without one may stay idle."""
    from ttp import coordroute
    ev = lambda kind: {"kind": kind, "severity": "normal", "task": None}
    assert coordroute.idle_needs([ev("task_done")], "") == []
    needs = coordroute.idle_needs([ev("stall"), ev("task_done"), ev("run_cap")], "## Must act now\n- #5 waits")
    assert needs == ["must-act item", "run_cap event", "stall event"]
    idle = [{"type": "noop", "why": "nothing to do"}, {"type": "look", "at": "30m", "text": "x"}]
    assert coordroute.filter_actions(idle) == ([0, 1], [], "")
    ok, refused, esc = coordroute.filter_actions(idle, needs)
    assert esc == "idle answer on must-act item, run_cap event, stall event" and not refused
    assert coordroute.filter_actions([{"type": "task_update", "id": 5, "status": "queued"}], needs)[2] == ""
    p = make(env)
    coordroute.shadow_started(p.state, 30, 31, [], needs=["stall event"])
    coordroute.shadow_side(p.state, 30, "opus", [{"type": "task_update", "id": 5, "status": "queued"}], "ok", 1.0)
    rec = coordroute.shadow_side(p.state, 30, "glm", idle, "ok", 0.1)
    assert rec["glm"]["escalated"] == "idle answer on stall event" and not rec["compare"]["agree"]


def test_coordroute_the_lane_sees_its_earlier_proposals_the_coordinator_did_not_take(env):
    """The lane has no session: its shadow proposals the coordinator did not take on the same wake are shown
    once each (newest first, within the window), so it does not propose them again; taken ones are not."""
    from ttp import coordroute
    p = make(env)
    now = time.time()
    for run, ago, glm in ((40, 3600, [{"type": "task_add", "title": "Review the sweep findings"},
                                      {"type": "task_update", "id": 9, "priority": 2}]),
                          (50, 1800, [{"type": "task_add", "title": "Review the sweep findings"}]),
                          (60, 20 * 3600, [{"type": "task_add", "title": "Old idea"}])):
        coordroute.shadow_started(p.state, run, run + 1, [], now=now - ago)
        coordroute.shadow_side(p.state, run, "opus", [{"type": "task_update", "id": 9, "priority": 2}], "ok", 1.0)
        coordroute.shadow_side(p.state, run, "glm", glm, "ok", 0.1)
    text = coordroute.declined_proposals(p.state, now)
    assert text.startswith("## Earlier lane proposals the coordinator did not take")
    assert text.count("task_add review the sweep findings") == 1
    assert "#9" not in text and "old idea" not in text
    assert coordroute.declined_proposals(p.state, now - 7200) == ""
    coordroute.shadow_started(p.state, 70, 71, [], now=now - 600)  # the coordinator's turn failed: nothing declined
    coordroute.shadow_side(p.state, 70, "opus", [], "failed", 0.0)
    coordroute.shadow_side(p.state, 70, "glm", [{"type": "task_add", "title": "Never looked at"}], "ok", 0.1)
    assert "never looked at" not in coordroute.declined_proposals(p.state, now)
    parts = coordroute.lane_digest([("tasks", "## Open tasks\n- #9")], [], p.db, 0, state_dir=p.state, now=now)
    assert parts[-1] == ("declined_proposals", text)
    assert "Earlier lane proposals" in coordroute.LANE_PROMPT


def test_coordroute_declined_proposals_skip_refused_actions_and_show_the_digest_time_format(env):
    """An action the lane may not take was refused, not declined, so it is not listed; times use the lane
    digest's format (the home zone with its abbreviation), not UTC."""
    from ttp import coordroute
    p = make(env)
    now = time.time()
    coordroute.shadow_started(p.state, 80, 81, [], now=now - 600)
    coordroute.shadow_side(p.state, 80, "opus", [], "ok", 1.0)
    coordroute.shadow_side(p.state, 80, "glm", [{"type": "reply", "chat": "c1", "text": "hi"},
                                                {"type": "task_add", "title": "Pinned one", "model": "x"},
                                                {"type": "task_add", "title": "Plain one"}], "ok", 0.1)
    text = coordroute.declined_proposals(p.state, now, fmt=lambda t: "09:30 PST")
    assert "- 09:30 PST task_add plain one" in text and "pinned one" not in text and "reply" not in text, text
    assert "Z " not in text
    parts = coordroute.lane_digest([], [], p.db, 0, lambda t: "09:30 PST", state_dir=p.state, now=now)
    assert parts[-1] == ("declined_proposals", text)


def test_coordroute_old_shadow_pair_files_are_pruned_and_the_report_still_counts_recent_pairs(env):
    """A new pair drops files last written over SHADOW_KEEP_DAYS ago; recent pairs stay and report() counts them."""
    from ttp import coordroute
    p = make(env)
    now = time.time()
    for run in (90, 91):
        coordroute.shadow_started(p.state, run, run + 1, [], now=now - 3600)
        coordroute.shadow_side(p.state, run, "opus", [], "ok", 1.0)
        coordroute.shadow_side(p.state, run, "glm", [], "ok", 0.1)
    old = coordroute.shadow_path(p.state, 90)
    gone = now - (coordroute.SHADOW_KEEP_DAYS + 1) * 86400
    os.utime(old, (gone, gone))
    coordroute.shadow_started(p.state, 92, 93, [], now=now)
    assert not old.exists() and coordroute.shadow_path(p.state, 91).exists()
    assert coordroute.report(p.db, p.state, now=now)["shadow"]["pairs"] >= 1


def test_coordroute_an_escalation_where_the_coordinator_acted_out_of_lane_is_counted_right(env):
    """compare() keeps strict agreement, and marks an escalation right when the coordinator's own answer
    needed an action outside the lane (a reply, a memory entry, a promise receipt)."""
    from ttp import coordroute
    p = make(env)
    esc = [{"type": "noop", "why": "escalate: a promise is due"}]
    for run, opus, right in ((70, [{"type": "reply", "chat": "c1", "text": "done"}], True),
                             (80, [{"type": "obligation", "key": "o3", "evidence": "landed"}], True),
                             (90, [{"type": "task_update", "id": 4, "priority": 1}], False)):
        coordroute.shadow_started(p.state, run, run + 1, [])
        coordroute.shadow_side(p.state, run, "opus", opus, "ok", 1.0)
        rec = coordroute.shadow_side(p.state, run, "glm", esc, "ok", 0.1)
        assert not rec["compare"]["agree"] and rec["compare"]["escalated_right"] is right, run
    assert coordroute.report(p.db, p.state)["shadow"]["escalated_right"] == round(2 / 3, 3)
