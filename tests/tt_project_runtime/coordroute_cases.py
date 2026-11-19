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


def test_coordroute_lane_rows_never_count_as_the_coordinators_own_turn(env):
    from ttp import coordroute
    p = make(env)
    for note in ({}, {coordroute.LANE: "routine"}, {coordroute.SHADOW: {"of": 1}}):
        p.db.x("INSERT INTO runs(role,status,started,note) VALUES('coordinator','running',?,?)",
               (time.time(), json.dumps(note)))
    own = p.db.q("SELECT id FROM runs WHERE role='coordinator' AND " + coordroute.OWN_SQL)
    assert [r["id"] for r in own] == [1]
    assert [coordroute.lane_of_row(r) for r in p.db.q("SELECT note FROM runs ORDER BY id")] == ["", "routine", "shadow"]


# The GLM-arm ask-Opus switches (glmassist.py) ----------------------------------------------------------------

def test_glmassist_switches_are_off_by_default_and_told_to_the_run_only_when_on():
    from ttp import codearm, glmassist
    assert glmassist.for_run({}) is None and glmassist.prompt_lines(None) == ""
    s = glmassist.for_run({"route": {"glm_work": {"advisor": True}}})
    assert s["advisor"] and not s["plan_first"] and (s["max_calls"], s["max_minutes"]) == (3, 10.0)
    assert "ttp advise" in glmassist.prompt_lines(s) and "--plan" not in glmassist.prompt_lines(s)
    s = glmassist.for_run({"route": {"glm_work": {"plan_first": True, "advisor": {"on": False}}}})
    assert s["plan_first"] and not s["advisor"] and "ttp advise --plan" in glmassist.prompt_lines(s)
    s = glmassist.settings({"route": {"glm_work": {"advisor": {"on": True, "max_calls": 99, "max_minutes": -1}}}})
    assert (s["max_calls"], s["max_minutes"]) == (20, 0.0)
    assert "advise" not in codearm._guard_lines(), "the arm's prompt names advise only when it is on"


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
