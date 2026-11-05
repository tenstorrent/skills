# Persistent coordinator session tests of tests/test_tt_project_runtime.py (coordsession.py:
# coordinator.session.mode on resumes one agent session per wake), which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations


def _cs_cfg(**over):
    return {"coordinator": {"session": {"mode": "on", **over}}}


def _cs_state(**over):
    return {"id": "s1", "provider": "fake", "model": "m", "effort": "high", "key": "k1", "started": 0,
            "wakes": 2, "context_tokens": 1000, "failures": [], "last_ok": True, **over}


def test_coordsession_decide_resumes_a_sound_session_and_names_each_rollover():
    from ttp import coordsession as cs
    now, yes = 1000.0, (lambda s: True)
    assert cs.decide({}, _cs_state(), "k1", "m", "high", yes, now) == ("stateless", "off"), "off by default"
    assert cs.decide(_cs_cfg(mode="shadow"), _cs_state(), "k1", "m", "high", yes, now)[0] == "stateless"
    assert cs.decide(_cs_cfg(), _cs_state(), "k1", "m", "high", yes, now) == ("resume", "continued")
    reasons = {
        "no session": (None, "k1", "m", "high", yes),
        "prefix changed": (_cs_state(), "k2", "m", "high", yes),
        "model or effort changed": (_cs_state(), "k1", "m2", "high", yes),
        "context reached 140000 tokens": (_cs_state(context_tokens=140000), "k1", "m", "high", yes),
        "last run not ok": (_cs_state(last_ok=False), "k1", "m", "high", yes),
        "session not saved": (_cs_state(), "k1", "m", "high", lambda s: False),
    }
    for why, (state, key, model, effort, saved) in reasons.items():
        assert cs.decide(_cs_cfg(), state, key, model, effort, saved, now) == ("new", why), why
    assert cs.decide(_cs_cfg(), _cs_state(), "k1", "m", "low", yes, now) == ("new", "model or effort changed")
    assert cs.decide(_cs_cfg(), _cs_state(id=""), "k1", "m", "high", yes, now) == ("new", "no session")
    assert cs.decide(_cs_cfg(rollover_tokens=500), _cs_state(), "k1", "m", "high", yes, now)[0] == "new"
    assert cs.decide(_cs_cfg(), _cs_state(context_tokens=139999), "k1", "m", "high", yes, now)[0] == "resume"
    # Garbage state never raises: it starts a session.
    for bad in ("x", [], 7, {"id": None}):
        assert cs.decide(_cs_cfg(), bad, "k1", "m", "high", yes, now) == ("new", "no session"), bad


def test_coordsession_a_tripped_spend_guard_runs_stateless_until_it_ends():
    from ttp import coordsession as cs
    now, yes = 1000.0, (lambda s: True)
    tripped = {"tripped": True, "until": now + 60}
    assert cs.decide(_cs_cfg(), _cs_state(), "k1", "m", "high", yes, now, guard=tripped) == \
        ("stateless", "spend guard tripped")
    assert cs.decide(_cs_cfg(), None, "k1", "m", "high", yes, now, guard=tripped)[0] == "stateless"
    for open_ in (None, {}, {"tripped": True, "until": now - 1}, {"tripped": False, "until": now + 60},
                  {"tripped": "yes", "until": now + 60}, {"tripped": True}, "junk"):
        assert cs.decide(_cs_cfg(), _cs_state(), "k1", "m", "high", yes, now, guard=open_)[0] == "resume", open_


def test_coordsession_failed_resumes_rerun_stateless_and_the_cap_pauses_the_mode():
    from ttp import coordsession as cs
    cfg, yes = _cs_cfg(), (lambda s: True)
    st, paused = cs.fail(cfg, _cs_state(), 1000.0)
    assert not paused and st["rerun"] and not st["last_ok"]
    assert cs.decide(cfg, st, "k1", "m", "high", yes, 1001.0) == ("stateless", "rerun of a failed resume")
    st = cs.record(st, None, "", {"mode": "stateless", "why": "rerun of a failed resume"}, True, 1002.0)
    assert "rerun" not in st and cs.decide(cfg, st, "k1", "m", "high", yes, 1003.0) == ("new", "last run not ok")
    # A failure older than an hour does not count toward the cap.
    st, paused = cs.fail(cfg, {**st, "failures": [1000.0 - 3700, 1000.0]}, 1100.0)
    assert not paused and len(st["failures"]) == 2
    st, paused = cs.fail(cfg, st, 1200.0)
    assert paused and st["paused_until"] == 1200.0 + 6 * 3600 and st["failures"] == []
    assert cs.decide(cfg, {**st, "rerun": False}, "k1", "m", "high", yes, 1300.0) == \
        ("stateless", "paused after repeated failed resumes")
    assert cs.decide(cfg, {**st, "rerun": False}, "k1", "m", "high", yes, 1200.0 + 6 * 3600 + 1) == \
        ("new", "last run not ok"), "the pause ends with a fresh session"
    assert cs.fail(_cs_cfg(max_failures_per_h=1, pause_h=1), None, 50.0)[0]["paused_until"] == 3650.0


def test_coordsession_record_counts_wakes_and_context_of_its_session():
    from types import SimpleNamespace
    from ttp import coordsession as cs
    turn = {"mode": "new", "why": "no session", "key": "k1", "model": "m", "effort": "high", "provider": "fake"}
    use = SimpleNamespace(session_id="", input_tokens=10, cache_read_tokens=100, cache_write_tokens=20, output_tokens=5)
    st = cs.record(None, use, "s1", turn, True, 10.0)
    assert {k: st[k] for k in ("id", "wakes", "context_tokens", "last_ok", "started", "last_why")} == \
        {"id": "s1", "wakes": 1, "context_tokens": 135, "last_ok": True, "started": 10.0, "last_why": "no session"}
    st = cs.record(st, SimpleNamespace(**{**vars(use), "cache_read_tokens": 900}), "s1",
                   {**turn, "mode": "resume", "why": "continued"}, True, 20.0)
    assert st["wakes"] == 2 and st["context_tokens"] == 935 and st["started"] == 10.0
    # A new session that failed, or one whose id is unknown, is never resumed.
    assert not cs.record(None, use, "s2", turn, False, 30.0)["last_ok"]
    assert not cs.record(None, use, "", turn, True, 30.0)["last_ok"]
    line = cs.status_line(_cs_cfg(), st)
    assert line == "coordinator session (on): s1 · 2 wakes · 935 context tokens · last: continued", line
    assert cs.status_line({}, None) == ""


def test_coordsession_mode_is_chat_settable(env, capsys):
    from ttp import cli
    from ttp.coordinator import settable
    cli.cmd_chat_settable("coordinator.session.mode", "on")
    assert capsys.readouterr().out.strip() == 'coordinator.session.mode: accepted as "on"'
    assert settable("coordinator.session.mode")(" OFF ") == "off"
    with pytest.raises(SystemExit) as e:
        cli.cmd_chat_settable("coordinator.session.mode", "always")
    assert e.value.code == 1 and "refused" in capsys.readouterr().out
    p = make(env)
    assert p.config()["coordinator"]["session"] == {"mode": "off", "rollover_tokens": 140000,
                                                   "max_failures_per_h": 3, "pause_h": 6}
    p.set_config("coordinator.session.mode", settable("coordinator.session.mode")("on"))
    assert p.config()["coordinator"]["session"]["mode"] == "on"
    assert p.config()["coordinator"]["session"]["rollover_tokens"] == 140000, "the other defaults stay"


def _cs_daemon(env, monkeypatch):
    """A fake-provider project with saved fake sessions and a turn runner returning (run row, argv,
    prompt.md, the turn's session note) of a finished coordinator turn."""
    from ttp.daemon import Daemon
    from ttp import coordinator as coord
    p = make(env)
    sessions = _sessions(env, monkeypatch)
    head, context = coord.prompt_parts(p)
    holder = {"d": Daemon(p.base)}

    def turn(digest="EVENTS"):
        d = holder["d"]
        d.cfg = p.config()
        rid = d.start_run("coordinator", digest, "fake", "light", str(p.base), read_only=True,
                          schema=coord.ACTIONS_SCHEMA, system=head, context=context, cache_ttl="auto")
        _finish_runs(p, d)
        r = p.db.one("SELECT * FROM runs WHERE id=?", (rid,))
        spec = json.loads((p.runs / str(rid) / "run.json").read_text())
        return r, spec["argv"], (p.runs / str(rid) / "prompt.md").read_text(), \
            (json.loads(r["note"])["turn"].get("session"))
    return p, holder, sessions, turn, head, context


def test_coordsession_mode_off_launches_today_s_turn_byte_for_byte(env, monkeypatch):
    from ttp import coordinator as coord
    from ttp import coordsession
    p, holder, _, turn, head, context = _cs_daemon(env, monkeypatch)
    runs = [turn("EVENTS golden")]
    monkeypatch.setattr(coordsession, "on", lambda *a: False)   # the code path before the session mode
    runs.append(turn("EVENTS golden"))
    for r, argv, prompt, session in runs:
        assert r["status"] == "ok" and session is None
        assert argv == [sys.executable, "-m", "ttp.providers.fake", "coordinator"], argv
        assert prompt == coord.join_prompt(head, context) + "\n\nEVENTS golden"
    assert runs[0][1:] == runs[1][1:]
    assert p.db.kv(coordsession.KEY) is None, "mode off saves no session"


def test_coordsession_on_resumes_the_same_session_across_wakes_and_a_restart(env, monkeypatch):
    from ttp import coordinator as coord
    from ttp import coordsession
    from ttp.daemon import Daemon
    p, holder, sessions, turn, head, context = _cs_daemon(env, monkeypatch)
    p.set_config("coordinator.session.mode", "on")
    first, argv, prompt, session = turn("EVENTS 1")
    assert first["status"] == "ok" and "--resume" not in argv and session["mode"] == "new", session
    assert session["why"] == "no session" and prompt.endswith("\n\nEVENTS 1")
    sid = first["session_id"]
    assert sid and p.db.kv(coordsession.KEY)["id"] == sid and (sessions / f"{sid}.jsonl").is_file()
    r, argv, prompt, session = turn("EVENTS 2")
    assert argv[-2:] == ["--resume", sid] and session == {**session, "mode": "resume", "why": "continued"}
    assert r["session_id"] == sid and prompt.endswith(coord.THREAD_CONTINUED + "\n\nEVENTS 2"), prompt[-300:]
    holder["d"] = Daemon(p.base)   # a restart resumes it from the database
    r, argv, _, session = turn("EVENTS 3")
    assert argv[-2:] == ["--resume", sid] and session["mode"] == "resume"
    st = p.db.kv(coordsession.KEY)
    assert st["wakes"] == 3 and st["id"] == sid and st["last_ok"] and st["last_why"] == "continued"
    assert len((sessions / f"{sid}.jsonl").read_text().splitlines()) == 3
    from ttp.cli import status_text
    assert f"coordinator session (on): {sid[:8]} · 3 wakes" in status_text(p, verbose=True)
    # A tripped spend guard runs the wake stateless and leaves the session for later.
    p.db.set_kv(coordsession.GUARD_KEY, {"tripped": True, "until": time.time() + 600})
    r, argv, _, session = turn("EVENTS 4")
    assert "--resume" not in argv and session["mode"] == "stateless" and session["why"] == "spend guard tripped"
    assert p.db.kv(coordsession.KEY)["id"] == sid and p.db.kv(coordsession.KEY)["wakes"] == 3
    p.db.set_kv(coordsession.GUARD_KEY, {"tripped": True, "until": time.time() - 1})
    assert turn("EVENTS 5")[1][-2:] == ["--resume", sid]
    # The transcript gone (a host move): a new session.
    (sessions / f"{sid}.jsonl").unlink()
    r, argv, _, session = turn("EVENTS 6")
    assert "--resume" not in argv and session["why"] == "session not saved" and r["session_id"] != sid


def _cs_wake(p, d, deadline_s=30):
    """One maybe_coordinate wake, run to its end: the run's id, or None when no turn started."""
    before = p.db.one("SELECT MAX(id) n FROM runs")["n"] or 0
    d.cfg = p.config()
    d.maybe_coordinate()
    _finish_runs(p, d, deadline_s)
    row = p.db.one("SELECT id FROM runs WHERE role='coordinator' AND id>? ORDER BY id DESC", (before,))
    return row["id"] if row else None


def test_coordsession_a_failed_resume_reruns_its_batch_stateless_and_handles_each_message_once(env, monkeypatch):
    from ttp import coordsession
    from ttp.daemon import Daemon
    from ttp.providers import fake
    p = make(env)
    sessions = _sessions(env, monkeypatch)
    p.set_config("coordinator.session.mode", "on")
    p.set_config("coordinator.debounce_s", 0)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    d = Daemon(p.base)
    p.db.post("in", "first", chat="c1")
    rid = _cs_wake(p, d)
    note = lambda i: json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (i,))["note"])["turn"]["session"]
    assert rid and note(rid)["mode"] == "new"
    sid = p.db.kv(coordsession.KEY)["id"]
    # The transcript vanishes after the check: the agent cannot resume it and the run fails.
    (sessions / f"{sid}.jsonl").unlink()
    monkeypatch.setattr(fake.Fake, "session_saved", lambda self, s, cwd, env=None: True)
    p.db.post("in", "second", chat="c1")
    failed = _cs_wake(p, d)
    assert note(failed)["mode"] == "resume"
    assert p.db.one("SELECT status FROM runs WHERE id=?", (failed,))["status"] != "ok"
    assert p.db.q("SELECT id FROM messages WHERE direction='in' AND handled=0"), "nothing of the failed turn applied"
    assert float(p.db.kv("coordinator_backoff_until", 0)) <= time.time(), "the rerun waits on no backoff"
    assert p.db.kv(coordsession.KEY)["rerun"] is True
    rerun = _cs_wake(p, d)
    assert rerun == failed + 1 and note(rerun) == {**note(rerun), "mode": "stateless", "why": "rerun of a failed resume"}
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rerun,))["status"] == "ok"
    assert not p.db.q("SELECT id FROM messages WHERE direction='in' AND handled=0")
    texts = [m["text"] for m in p.db.unread_for_chat("c1", 0)]
    assert texts.count("ack: first") == 1 and texts.count("ack: second") == 1, texts
    titles = [t["title"] for t in p.db.q("SELECT title FROM tasks")]
    assert titles.count("do: first") == 1 and titles.count("do: second") == 1, titles
    st = p.db.kv(coordsession.KEY)
    assert "rerun" not in st and st["failures"] and not st["last_ok"]
    p.db.post("in", "third", chat="c1")
    nxt = _cs_wake(p, d)
    assert note(nxt)["mode"] == "new" and note(nxt)["why"] == "last run not ok"


def test_coordsession_the_failure_cap_pauses_the_mode_and_raises_an_issue(env, monkeypatch):
    from types import SimpleNamespace
    from ttp import coordsession
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("coordinator.session.mode", "on")
    d = Daemon(p.base)
    d.cfg = p.config()
    seen = []
    monkeypatch.setattr(d, "observe", lambda source, text, *a, **k: seen.append((source, text, k.get("key"))))
    p.db.set_kv(coordsession.KEY, _cs_state())
    use = SimpleNamespace(session_id="", input_tokens=0, cache_read_tokens=0, cache_write_tokens=0, output_tokens=0)
    for n in range(3):
        assert d._coord_session_end({"id": n, "session_id": "s1"}, use, {"mode": "resume"}, False)
    st = p.db.kv(coordsession.KEY)
    assert st["paused_until"] > time.time() + 5 * 3600 and len(seen) == 1, seen
    assert seen[0][0] == "daemon:coord-session" and "stateless for the next 6 h" in seen[0][1]
    assert coordsession.decide(d.cfg, {**st, "rerun": False}, "k1", "m", "high", lambda s: True, time.time()) == \
        ("stateless", "paused after repeated failed resumes")
    # A wake that did not resume never takes the failed-resume path.
    assert not d._coord_session_end({"id": 9, "session_id": "s2"}, use, {"mode": "new"}, False)


def test_coordsession_the_failure_cap_trips_across_the_new_sessions_between_failed_resumes(env, monkeypatch):
    # The real cycle: new -> failed resume -> stateless rerun -> new ("last run not ok") -> failed
    # resume -> ...: a new session between failures keeps their count, so the third one pauses.
    from ttp import coordsession
    from ttp.daemon import Daemon
    from ttp.providers import fake
    p = make(env)
    sessions = _sessions(env, monkeypatch)
    p.set_config("coordinator.session.mode", "on")
    p.set_config("coordinator.debounce_s", 0)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    d = Daemon(p.base)
    seen = []
    real_observe = d.observe
    monkeypatch.setattr(d, "observe", lambda source, text, *a, **k: (
        seen.append(k.get("key")), real_observe(source, text, *a, **k))[1])
    monkeypatch.setattr(fake.Fake, "session_saved", lambda self, s, cwd, env=None: True)
    note = lambda i: json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (i,))["note"])["turn"]["session"]
    n = 0

    def wake():
        nonlocal n
        n += 1
        p.db.post("in", f"msg {n}", chat="c1")
        rid = _cs_wake(p, d)
        assert rid
        return note(rid)

    for cycle in range(3):
        first = wake()
        assert first["mode"] == "new", (cycle, first)
        # The transcript vanishes: the next wake resumes it and fails.
        (sessions / f"{p.db.kv(coordsession.KEY)['id']}.jsonl").unlink()
        assert wake()["mode"] == "resume"
        st = p.db.kv(coordsession.KEY)
        assert len(st.get("failures") or []) == (0 if cycle == 2 else cycle + 1), (cycle, st)
        if cycle < 2:
            assert not st.get("paused_until") and seen.count("coord-session-paused") == 0
            assert wake() == {**note(p.db.one("SELECT MAX(id) n FROM runs")["n"]), "mode": "stateless",
                              "why": "rerun of a failed resume"}
    st = p.db.kv(coordsession.KEY)
    assert st["paused_until"] > time.time() + 5 * 3600, st
    assert seen.count("coord-session-paused") == 1, seen
    assert wake()["mode"] == "stateless"
