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
        "context reached 100000 tokens": (_cs_state(context_tokens=100000, handover=True), "k1", "m", "high", yes),
        "last run not ok": (_cs_state(last_ok=False), "k1", "m", "high", yes),
        "session not saved": (_cs_state(), "k1", "m", "high", lambda s: False),
    }
    for why, (state, key, model, effort, saved) in reasons.items():
        assert cs.decide(_cs_cfg(), state, key, model, effort, saved, now) == ("new", why), why
    assert cs.decide(_cs_cfg(), _cs_state(), "k1", "m", "low", yes, now) == ("new", "model or effort changed")
    assert cs.decide(_cs_cfg(), _cs_state(id=""), "k1", "m", "high", yes, now) == ("new", "no session")
    # At the limit (no first prompt recorded: rollover_growth_tokens) one handover wake resumes the
    # session first (its session_note), then a new one.
    assert cs.decide(_cs_cfg(), _cs_state(context_tokens=100000), "k1", "m", "high", yes, now) == ("resume", "handover")
    assert cs.decide(_cs_cfg(rollover_tokens=500), _cs_state(), "k1", "m", "high", yes, now) == ("resume", "handover")
    assert cs.decide(_cs_cfg(rollover_tokens=500), _cs_state(handover=True), "k1", "m", "high", yes, now)[0] == "new"
    # A session that cannot resume rolls over at once, without the handover wake.
    assert cs.decide(_cs_cfg(), _cs_state(context_tokens=100000, last_ok=False), "k1", "m", "high", yes, now) == \
        ("new", "last run not ok")
    assert cs.decide(_cs_cfg(), _cs_state(context_tokens=100000), "k2", "m", "high", yes, now) == ("new", "prefix changed")
    assert cs.decide(_cs_cfg(), _cs_state(context_tokens=99999), "k1", "m", "high", yes, now)[0] == "resume"
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


def _cs_stream(path, calls):
    """A Claude stream-json transcript of one turn: one assistant message per (prompt, output) call."""
    lines = [{"type": "system", "subtype": "init", "session_id": "s1", "model": "m"}]
    for n, (prompt, out) in enumerate(calls):
        lines.append({"type": "assistant", "message": {"id": f"msg{n}", "content": [], "usage": {
            "input_tokens": 5, "cache_read_input_tokens": prompt - 5 if n else 0,
            "cache_creation_input_tokens": 0 if n else prompt - 5, "output_tokens": out}}})
    total = {"input_tokens": 5 * len(calls), "output_tokens": sum(o for _, o in calls),
             "cache_read_input_tokens": sum(p - 5 for p, _ in calls[1:]),
             "cache_creation_input_tokens": calls[0][0] - 5}
    lines.append({"type": "result", "subtype": "success", "session_id": "s1", "result": "{}", "usage": total})
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path


def test_coordsession_a_multi_call_turn_under_the_limit_resumes(tmp_path):
    """The session's size is its last call's prompt and answer, not every call's prompt summed."""
    from ttp import coordsession as cs
    from ttp.providers.claude import Claude
    turn = {"mode": "new", "why": "no session", "key": "k1", "model": "m", "effort": "high", "provider": "claude"}
    use = Claude().parse(_cs_stream(tmp_path / "a.jsonl", [(30000, 500), (32000, 400), (35000, 600), (38000, 900)]))
    assert use.extra["cache_telemetry"]["last_context"] == 38900
    assert use.input_tokens + use.cache_read_tokens + use.cache_write_tokens + use.output_tokens > 100000, \
        "the turn's sum alone would have rolled over"
    st = cs.record(None, use, "", turn, True, 10.0)
    assert (st["context_tokens"], st["base_tokens"]) == (38900, 30000)
    assert cs.decide(_cs_cfg(), st, "k1", "m", "high", lambda s: True, 20.0) == ("resume", "continued")
    # A resumed wake keeps the session's first prompt as its base.
    st = cs.record(st, use, "", {**turn, "mode": "resume", "why": "continued"}, True, 30.0)
    assert st["base_tokens"] == 30000 and st["wakes"] == 2


def test_coordsession_a_last_call_over_the_limit_rolls_over(tmp_path):
    from ttp import coordsession as cs
    from ttp.providers.claude import Claude
    yes, turn = (lambda s: True), {"mode": "new", "why": "no session", "key": "k1", "model": "m",
                                    "effort": "high", "provider": "claude"}
    st = cs.record(None, Claude().parse(_cs_stream(tmp_path / "a.jsonl", [(30000, 500)])), "", turn, True, 10.0)
    grown = Claude().parse(_cs_stream(tmp_path / "b.jsonl", [(60000, 500), (129600, 400)]))
    st = cs.record(st, grown, "", {**turn, "mode": "resume", "why": "continued"}, True, 20.0)
    assert (st["context_tokens"], st["base_tokens"]) == (130000, 30000)
    # Over the limit, one handover wake resumes the session (its session_note), then a new one starts.
    assert cs.decide(_cs_cfg(), st, "k1", "m", "high", yes, 30.0) == ("resume", "handover")
    st = cs.record(st, Claude().parse(_cs_stream(tmp_path / "h.jsonl", [(130500, 300)])), "",
                   {**turn, "mode": "resume", "why": "handover"}, True, 35.0)
    assert cs.decide(_cs_cfg(), st, "k1", "m", "high", yes, 36.0) == ("new", "context reached 130000 tokens")
    # A rollover starts the next session's base afresh.
    st = cs.record(st, Claude().parse(_cs_stream(tmp_path / "c.jsonl", [(20000, 100)])), "",
                   {**turn, "why": "context reached 130000 tokens"}, True, 40.0)
    assert (st["context_tokens"], st["base_tokens"], st["wakes"]) == (20100, 20000, 1)
    # A known window caps the limit, and a fixed rollover_tokens replaces it.
    st = {**_cs_state(), "base_tokens": 30000, "context_tokens": 110000}
    assert cs.decide(_cs_cfg(context_window_tokens=150000), st, "k1", "m", "high", yes, 50.0) == ("resume", "handover")
    assert cs.decide(_cs_cfg(), st, "k1", "m", "high", yes, 50.0) == ("resume", "continued")
    assert cs.decide(_cs_cfg(rollover_tokens=110000), st, "k1", "m", "high", yes, 50.0) == ("resume", "handover")
    assert cs.limit(_cs_cfg(), st, "claude-x[1m]") == 130000, "growth stays the bound under a long window"
    assert cs.limit(_cs_cfg(rollover_growth_tokens=2000000), st, "claude-x[1m]") == 960000
    # Without per-call usage (another agent), the turn's sum still bounds it: early, never late.
    from types import SimpleNamespace
    use = SimpleNamespace(session_id="s", input_tokens=60000, cache_read_tokens=50000, cache_write_tokens=0,
                          output_tokens=0, extra={"cache_telemetry": {"last_context": True}})
    assert cs.context_tokens(use) == 110000


def test_coordsession_a_fresh_prompt_above_the_old_140k_default_still_resumes(tmp_path):
    """A project whose first call alone is 145k tokens resumes; its first wake grew to 186k."""
    from ttp import coordsession as cs
    from ttp.providers.claude import Claude
    turn = {"mode": "new", "why": "no session", "key": "k1", "model": "m", "effort": "high", "provider": "claude"}
    use = Claude().parse(_cs_stream(tmp_path / "a.jsonl",
                                    [(145065, 800), (160000, 900), (172000, 700), (185500, 848)]))
    st = cs.record(None, use, "", turn, True, 10.0)
    assert (st["context_tokens"], st["base_tokens"]) == (186348, 145065)
    assert cs.limit(_cs_cfg(), st, "m") == 245065
    assert cs.decide(_cs_cfg(), st, "k1", "m", "high", lambda s: True, 20.0) == ("resume", "continued")
    # Observed first calls per wake (stateless 139.7k and 140.1k, fresh sessions 142.5k-153.9k):
    # a five-call turn sums to 700k+, yet its last call stays well under the limit.
    for first in (139700, 140100, 142500, 148500, 153900):
        use = Claude().parse(_cs_stream(tmp_path / f"w{first}.jsonl",
                                        [(first + 3000 * n, 600) for n in range(5)]))
        assert use.input_tokens + use.cache_read_tokens + use.cache_write_tokens + use.output_tokens > 700000
        st = cs.record(None, use, "", turn, True, 10.0)
        assert (st["context_tokens"], st["base_tokens"]) == (first + 12600, first)
        assert cs.decide(_cs_cfg(), st, "k1", "m", "high", lambda s: True, 20.0) == ("resume", "continued")


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
    assert p.config()["coordinator"]["session"] == {"mode": "off", "rollover_tokens": 0,
                                                   "rollover_growth_tokens": 100000, "context_window_tokens": 0,
                                                   "rollover_margin_tokens": 40000,
                                                   "max_failures_per_h": 3, "pause_h": 6}
    p.set_config("coordinator.session.mode", settable("coordinator.session.mode")("on"))
    assert p.config()["coordinator"]["session"]["mode"] == "on"
    assert p.config()["coordinator"]["session"]["rollover_growth_tokens"] == 100000, "the other defaults stay"


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


def _cstat_run(p, started, cost, session=None, sid="", fc=None, tools=None, read=0, inp=0, write=0):
    """A finished coordinator run for `ttp stats --coord-session`: its session note, first-call context
    and a transcript holding `tools` tool_use blocks (None: no transcript)."""
    note = {**({"turn": {"session": {"mode": session, "why": "x"}}} if session else {}),
            **({"cache_telemetry": {"calls": 3, "first_context": fc}} if fc is not None else {})}
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,ended,status,cost_usd,input_tokens,cache_read_tokens,"
                 "cache_write_tokens,note,session_id,boot_id) VALUES(NULL,'coordinator','claude',?,?,'ok',?,?,?,?,?,?,'x')",
                 (started, started + 30, cost, inp, read, write, json.dumps(note), sid))
    d = p.runs / str(rid)
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(d), rid))
    if tools is not None:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "output.jsonl", "w") as f:
            for i in range(tools):   # a stream repeats a message per block: each tool_use counts once
                ev = json.dumps({"type": "assistant", "message": {"id": f"m{i}", "content": [
                    {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}]}})
                f.write(ev + "\n" + ev + "\n")
            f.write('{"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}\nnot json\n[]\n')
    return rid


def test_coord_session_stats_report_each_figure_per_day_against_the_stateless_baseline(env):
    from ttp import coordstats
    p = make(env)
    day = 86400
    t0 = 1793750400.0   # 2026-11-04 00:00 UTC
    first = t0 + 3600
    for i in range(14):   # the baseline: 14 stateless turns at $0.50 in the 7 days before the first session wake
        _cstat_run(p, first - 7 * day + i * day / 2, 0.5)
    _cstat_run(p, first - 8 * day, 9.0)   # before the baseline's window
    # 2026-11-04: a new session, two resumes, a stateless rerun and a coordinator run outside the session.
    _cstat_run(p, first, 0.3, "new", "s1", 50000, tools=2, read=40000, inp=1000, write=9000)
    _cstat_run(p, first + 600, 0.1, "resume", "s1", 60000, tools=1, read=58000, inp=2000)
    _cstat_run(p, first + 1200, 0.1, "resume", "s1", 75000, tools=0, read=70000, inp=5000)
    _cstat_run(p, first + 1800, 0.2, "stateless", inp=10000)
    _cstat_run(p, first + 2400, 0.4)
    # 2026-11-05 (today): a resume that grew 35k context with 4 tool calls, then a rollover.
    _cstat_run(p, t0 + day + 600, 0.2, "resume", "s1", 110000, tools=4, read=100000)
    _cstat_run(p, t0 + day + 1200, 0.3, "new", "s2", 50000, read=40000, inp=500)
    now = t0 + day + 3600
    s = coordstats.report(p.db, p.runs, "UTC", 7, now=now)
    assert s["first_session_wake"] == first
    assert s["baseline"] == {"since": first - 7 * day, "until": first, "turns": 14, "days": 7.0,
                             "turns_per_day": 2.0, "usd_per_turn": 0.5, "usd_per_day": 1.0}
    d1, d2 = s["by_day"]
    assert d1 == {"day": "2026-11-04", "partial": False, "wakes": 4, "resumed": 2, "rollovers": 1, "stateless": 1,
                  "usd": 0.7, "usd_per_wake": 0.175, "growth_per_wake": 12500.0, "growth_wakes": 2,
                  "tool_calls_per_wake": 1.0, "tool_call_wakes": 3, "cache_read_ratio": round(168000 / 195000, 3),
                  "coordinator_usd": 1.1, "over_baseline": True, "break_even": True}
    assert d2["day"] == "2026-11-05" and d2["partial"] and (d2["wakes"], d2["resumed"], d2["rollovers"]) == (2, 1, 1)
    assert d2["growth_per_wake"] == 35000.0 and d2["tool_calls_per_wake"] == 4.0 and d2["tool_call_wakes"] == 1
    assert d2["break_even"] is False and d2["over_baseline"] is False and d2["coordinator_usd"] == 0.5
    assert d2["cache_read_ratio"] == round(140000 / 140500, 3)
    # The previous wake of a session counts from before the window: day 2 alone still grows by 35k.
    [only] = coordstats.report(p.db, p.runs, "UTC", 0.5, now=now)["by_day"]
    assert only["day"] == "2026-11-05" and only["growth_per_wake"] == 35000.0
    out = coordstats.text(s, "demo")
    assert "2.0 turns/day, $0.500/turn, $1.00/day" in out
    assert "2026-11-04: 4 wakes (2 resumed, 1 new, 1 stateless), $0.175/wake, context growth +12.5k/wake" in out
    assert "1.0 tool calls/wake, cache reads 86%; break-even" in out and ": met; coordinator $1.10" in out
    assert "OVER baseline" in out and "2026-11-05 (so far):" in out and ": missed; coordinator $0.50" in out
    # The day bounds follow the home zone: 01:00 UTC on 11-04 is still 11-03 in Los Angeles.
    assert coordstats.report(p.db, p.runs, "America/Los_Angeles", 7, now=now)["by_day"][0]["day"] == "2026-11-03"


def test_coord_session_stats_without_session_wakes_or_baseline_say_so(env, capsys):
    from ttp import cli, coordstats
    p = make(env)
    now = time.time()
    s = coordstats.report(p.db, p.runs, "UTC", 7, now=now)
    assert s["by_day"] == [] and s["first_session_wake"] is None and s["baseline"]["turns_per_day"] is None
    assert "no session-mode wakes" in coordstats.text(s) and "n/a turns/day" in coordstats.text(s)
    # A session wake with no baseline, no transcript and no first-call context: unknown, not a pass.
    _cstat_run(p, now - 60, 0.1, "new", "s1")
    [d] = coordstats.report(p.db, p.runs, "UTC", 7, now=now)["by_day"]
    assert d["break_even"] is None and d["over_baseline"] is None and d["cache_read_ratio"] is None
    _cstat_run(p, now - 30, 0.1, "resume", "s1", tools=1)
    [d] = coordstats.report(p.db, p.runs, "UTC", 7, now=now)["by_day"]
    assert d["growth_per_wake"] is None and d["break_even"] is None, "no first-call context: growth unknown"
    for bad in ('{"turn": "session", "cache_telemetry": 5}', '{"turn": {"session": "on"}}', '[1]', 'not json'):
        p.db.x("INSERT INTO runs(role,started,status,cost_usd,note) VALUES('coordinator',?,'ok',0.1,?)", (now - 10, bad))
    [d] = coordstats.report(p.db, p.runs, "UTC", 7, now=now)["by_day"]
    assert d["wakes"] == 2 and d["coordinator_usd"] == 0.6, "broken notes are no session wakes but still cost"
    cli.main(["stats", "demo", "--coord-session"])
    out = capsys.readouterr().out
    assert "Coordinator session of demo" in out and "wakes" in out and "break-even" in out and "unknown" in out
    cli.main(["stats", "demo", "--coord-session", "--json"])
    assert json.loads(capsys.readouterr().out)["by_day"][0]["wakes"] >= 1


# The session role's own room over the stateless coordinator role budget (coordinator_role_budget.txt,
# which stays the stateless role's cap): the order-ledger block the session always carries plus
# prompts/coordinator-session.md (2900), plus the session-only `self_change` row of coordinator.md's
# action table (1300, selfimprove.py). Session mode only; it never widens coordinator_role(p)'s cap.
SESSION_ROLE_ROOM = 2900 + 1300

_CS_CHARTER = ("# demo\n\n## Goals\nShip v1.\n\n## Restrictions\n- Never push to main (user msg #1).\n\n"
               "## Policies\nLabel every number.\n")


def _cs_session_project(env, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    sessions = _sessions(env, monkeypatch)
    p.charter_path.write_text(_CS_CHARTER)
    p.set_config("coordinator.session.mode", "on")
    p.set_config("coordinator.debounce_s", 0)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    d = Daemon(p.base)

    def wake(text):
        p.db.post("in", text, chat="c1")
        rid = _cs_wake(p, d)
        assert rid and p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "ok"
        run = p.runs / str(rid)
        return (json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"])["turn"]["session"],
                (run / "system.md").read_text(), (run / "prompt.md").read_text())
    return p, d, sessions, wake


def test_coordsession_rollover_saves_the_handover_and_seeds_the_new_session_with_each_part(env, monkeypatch):
    from ttp import coordsession, obligations
    from ttp.prompts import restrictions_block
    p, d, sessions, wake = _cs_session_project(env, monkeypatch)
    monkeypatch.setenv("TTP_FAKE_SESSION_NOTE", "1. Open threads: task #7 waits on its review (msg #2).")
    rules = restrictions_block(p, advisory=False)
    assert "Never push to main (user msg #1)." in rules
    first, system1, prompt1 = wake("first order")
    assert first["mode"] == "new" and rules in system1 and "# Persistent session" in system1
    assert "## Session handover" not in prompt1
    sid = p.db.kv(coordsession.KEY)["id"]
    oid = obligations.add(p, "Report the soak result by 17:00 PST")
    second, system2, _ = wake("second order")
    assert second["mode"] == "resume", second
    assert not (p.state / coordsession.NOTE_DIR).exists(), "no note outside a handover"
    # The context reaches its limit: one last resumed wake asks for and saves the handover.
    p.db.set_kv(coordsession.KEY, {**p.db.kv(coordsession.KEY), "context_tokens": 150000})
    last, _, prompt3 = wake("third order")
    assert (last["mode"], last["why"]) == ("resume", "handover") and coordsession.HANDOVER_HEAD in prompt3
    note = p.state / coordsession.NOTE_DIR / f"{sid}.md"
    assert note.read_text().strip() == os.environ["TTP_FAKE_SESSION_NOTE"]
    assert coordsession.load_note(p, sid).strip() == os.environ["TTP_FAKE_SESSION_NOTE"]
    # The next wake starts the new session, seeded with the ledger, the handover and the chat verbatim.
    new, system4, prompt4 = wake("fourth order")
    assert (new["mode"], new["why"]) == ("new", "context reached 150000 tokens"), new
    st = p.db.kv(coordsession.KEY)
    assert st["id"] != sid and st["prev"] == sid and "handover" not in st
    seed = prompt4.split("# NEW COORDINATOR SESSION", 1)[1]
    assert "## Order ledger" in seed and "Report the soak result by 17:00 PST" in seed and oid in seed
    assert f"## Handover from the previous session {sid[:8]}" in seed and os.environ["TTP_FAKE_SESSION_NOTE"] in seed
    assert "chat exchanges, verbatim" in seed
    for text in ("first order", "ack: first order", "second order", "ack: third order"):
        assert text in seed, text
    assert seed.index("first order") < seed.index("third order")
    assert "## Last 3 chat exchanges, verbatim" in seed
    assert "fourth order" in seed.split("## Last 3 chat exchanges", 1)[1], "the digest follows the seed"
    # The Restrictions stay verbatim after the rollover, in the same byte-stable system prompt.
    assert rules in system4 and system4 == system1 == system2
    assert coordsession.HANDOVER_HEAD not in prompt4


def test_coordsession_system_prompt_is_byte_identical_between_wakes_with_an_unchanged_charter(env, monkeypatch):
    from ttp import coordsession
    from ttp.prompts import coordinator_role, coordinator_session_role
    p, d, sessions, wake = _cs_session_project(env, monkeypatch)
    parts = coordsession.session_prompt_parts(p)
    first, system1, _ = wake("one")
    p.add_memory("The soak runs nightly.", kind="decision")   # memory and time move, the prompt does not
    second, system2, prompt2 = wake("two")
    assert coordsession.session_prompt_parts(p) == parts
    assert second["mode"] == "resume" and second["key"] == first["key"] and system2 == system1
    assert "The soak runs nightly." not in system2
    # A charter change gives a new prefix: a new session, its Restrictions block updated.
    p.charter_path.write_text(_CS_CHARTER.replace("Ship v1.", "Ship v2."))
    third, system3, _ = wake("three")
    assert (third["mode"], third["why"]) == ("new", "prefix changed") and "Ship v2." in system3
    assert "Ship v1." in system1 and system3 != system1
    # The session role keeps to the stateless budget plus its own room; the stateless role keeps the old cap (sa17).
    assert len(coordinator_session_role(p)) <= _role_budget() + SESSION_ROLE_ROOM
    assert len(coordinator_role(p)) <= _role_budget()


def test_coordsession_note_and_chat_lines_keep_to_their_caps(env):
    from ttp import coordsession
    p = make(env)
    assert coordsession.save_note(p, "s1", "  ") == "" and coordsession.save_note(p, "", "x") == ""
    path = coordsession.save_note(p, "../a b", "x" * (coordsession.NOTE_CHARS + 500))
    assert path and Path(path).parent == p.state / coordsession.NOTE_DIR and Path(path).name == "_a_b.md"
    text = Path(path).read_text()
    assert len(text) <= coordsession.NOTE_CHARS + 100 and "4,000-token note cap" in text
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    for i in range(30):
        mid = p.db.post("in", f"order {i} " + "y" * 200, chat="c1")
        p.db.x("UPDATE messages SET handled=1 WHERE id=?", (mid,))
        p.db.post("out", f"ack {i}", chat="c1", kind="reply")
    p.db.post("in", "still unhandled", chat="c1")
    lines = "\n".join(coordsession.chat_lines(p.db, "", 20, 100000))
    assert "order 29 " in lines and "order 10 " in lines and "order 9 " not in lines and "still unhandled" not in lines
    small = "\n".join(coordsession.chat_lines(p.db, "", 20, 1500))
    assert "order 29 " in small and "order 10 " not in small and len(small) < 2500 and "older left out" in small


def _cs_parts(tasks="- #1 | queued | t", paused=True, at="10:00"):
    parts = [("budget", f"# STATE at {at}\n## Project budget\n- ok"), ("tasks", f"## Open tasks (id | status)\n{tasks}")]
    parts += [("paused", "## Paused resources (held)\n- box: paused")] if paused else []
    return parts + [("events", "\n# NEW EVENTS\n- [user message #1] hi\n\nRespond with the JSON actions object only.")]


def test_coordsession_delta_omits_unchanged_sections_and_sends_a_changed_one_whole():
    from ttp import coordsession as cs
    full = _cs_parts() + [("ends", "## Temporary instructions possibly over\n- x")]   # the uncollapsed build
    sent, ack, why = cs.wake_digest("new", None, _cs_parts(), full, "seen0", "seen1", "g1")
    assert sent == full and why == "full: new session"
    ack = cs.wake_digest("new", None, _cs_parts(), _cs_parts(), "seen0", "seen1", "g1")[1]
    state = {"id": "s1", cs.DIGEST: ack}
    # Nothing changed but the clock: the time header, the delta line and the new events only.
    sent, ack2, why = cs.wake_digest("resume", state, _cs_parts(at="10:05"), full, "seen1", "seen2", "g1")
    assert why == "delta: 3 unchanged, 0 gone", why
    assert [k for k, _ in sent] == ["budget", cs.DELTA, "events"] and sent[0][1] == "# STATE at 10:05"
    assert "Unchanged, not repeated: Project budget, Open tasks, Paused resources." in sent[1][1], sent[1][1]
    assert ack2["sections"] == ack["sections"] and ack2["seen"] == "seen2"
    # A changed section is sent whole; a section gone is named.
    rows = "- #1 | running | t\n- #2 | queued | u"
    sent, _, why = cs.wake_digest("resume", {cs.DIGEST: ack2}, _cs_parts(rows, paused=False), full, "seen2", "s3", "g1")
    assert dict(sent)["tasks"] == f"## Open tasks (id | status)\n{rows}" and "paused" not in dict(sent)
    assert "Unchanged, not repeated: Project budget. Gone (now empty): Paused resources." in dict(sent)[cs.DELTA]
    # A changed budget is sent whole, its time header with it.
    changed = [("budget", "# STATE at 10:09\n## Project budget\n- red")] + _cs_parts()[1:]
    assert dict(cs.wake_digest("resume", state, changed, full, "seen1", "x", "g1")[0])["budget"] == changed[0][1]
    # Any state the saved hashes may not match sends the whole digest, uncollapsed (`full`).
    for st, seen, gen, reason in ((state, "seen1", "g2", "daemon restarted"),
                                  (state, "other", "g1", "another turn's digest since"),
                                  ({"id": "s1"}, "seen1", "g1", "no acknowledged digest"),
                                  ({cs.DIGEST: {"sections": "x"}}, "seen1", "g1", "no acknowledged digest"),
                                  ("garbage", "seen1", "g1", "no acknowledged digest")):
        sent, _, why = cs.wake_digest("resume", st, _cs_parts(at="11:00"), full, seen, "n", gen)
        assert why == f"full: {reason}" and sent == full, why
    assert cs.asked_full([{"type": "reply"}, {"type": "noop", "key": cs.FULL_ASK}])
    assert not cs.asked_full([{"type": "noop"}]) and not cs.asked_full(None)


def test_coordsession_only_an_ok_session_wake_keeps_its_digest_acknowledged():
    from types import SimpleNamespace
    from ttp import coordsession as cs
    use = SimpleNamespace(session_id="s1", input_tokens=1, cache_read_tokens=0, cache_write_tokens=0, output_tokens=1)
    ack = {"sections": {"tasks": ["h", "Open tasks"]}, "seen": "f", "gen": "g"}
    turn = {"mode": "resume", "why": "continued", "digest": ack}
    st = cs.record(_cs_state(), use, "s1", turn, True, 1.0)
    assert st[cs.DIGEST] == ack
    assert cs.DIGEST not in cs.record(st, use, "s1", turn, False, 2.0), "a failed wake acknowledges nothing"
    assert cs.DIGEST not in cs.record(st, use, "s1", {"mode": "resume", "why": "continued"}, True, 2.0)
    assert cs.DIGEST not in cs.record(st, use, "s1", {"mode": "stateless", "why": "spend guard tripped"}, True, 2.0), \
        "a stateless turn the session did not see: its next wake gets the whole digest"
    assert cs.DIGEST not in cs.fail(_cs_cfg(), st, 3.0)[0]


def test_coordsession_wakes_send_a_delta_and_the_whole_digest_after_restart_rollover_fallback_or_ask(env, monkeypatch):
    from ttp import coordinator as coord
    from ttp import coordsession
    from ttp.daemon import Daemon
    p = make(env)
    _sessions(env, monkeypatch)
    p.set_config("coordinator.session.mode", "on")
    p.set_config("coordinator.debounce_s", 0)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    d = Daemon(p.base)
    n = [0]

    def wake(daemon=None):
        n[0] += 1
        p.db.post("in", f"m{n[0]}", chat="c1")
        rid = _cs_wake(p, daemon or d)
        note = json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"])
        return note["turn"].get("session"), (p.runs / str(rid) / "prompt.md").read_text(), note["digest"]

    s, prompt, size = wake()
    assert s["mode"] == "new" and s["sent"] == "full: new session" and "Since your last wake" not in prompt
    assert "as last turn" not in prompt.split("# STATE", 1)[1], "the whole digest, uncollapsed"
    full_chars = s["sent_chars"]
    s, prompt, size = wake()
    assert s["mode"] == "resume" and s["sent"].startswith("delta: "), s
    assert "Since your last wake in this session" in prompt and "## Open tasks" in prompt, "changed: sent whole"
    same = prompt.split("Unchanged, not repeated: ")[1].split(".")[0].split(", ")
    assert same and "Open tasks" not in same and "- [user message #" in prompt
    for label in same:
        assert f"## {label}" not in prompt, label
    assert s["sent_chars"] < full_chars and s["sent_chars"] < size["chars"]
    assert p.db.kv(coordsession.KEY)[coordsession.DIGEST]["sections"] == s["digest"]["sections"]
    # A daemon restart: the whole digest.
    d = Daemon(p.base)
    s, prompt, _ = wake()
    assert s["mode"] == "resume" and s["sent"] == "full: daemon restarted" and "Since your last wake" not in prompt
    assert wake()[0]["sent"].startswith("delta: ")
    # A rollover: a new session with the whole digest.
    p.set_config("coordinator.session.rollover_tokens", 1)
    p.db.set_kv(coordsession.KEY, {**p.db.kv(coordsession.KEY), "context_tokens": 5})
    s, prompt, _ = wake()   # the handover wake resumes the session first, its digest a delta
    assert s["mode"] == "resume" and s["why"] == "handover" and s["sent"].startswith("delta: ")
    assert coordsession.HANDOVER_HEAD in prompt
    s, prompt, _ = wake()
    assert s["mode"] == "new" and s["sent"] == "full: new session" and "Since your last wake" not in prompt
    p.set_config("coordinator.session.rollover_tokens", 140000)
    assert wake()[0]["sent"].startswith("delta: ")
    # A fallback to a stateless turn: today's digest then, the whole digest on the next session wake.
    p.db.set_kv(coordsession.GUARD_KEY, {"tripped": True, "until": time.time() + 600})
    s, prompt, _ = wake()
    assert s["mode"] == "stateless" and "sent" not in s and "Since your last wake" not in prompt
    p.db.set_kv(coordsession.GUARD_KEY, {"tripped": False})
    s, prompt, _ = wake()
    assert s["mode"] == "resume" and s["sent"] == "full: no acknowledged digest", s
    # The session asks for the whole digest, or compacts its history: the next wake sends it whole.
    monkeypatch.setenv("TTP_FAKE_ACTIONS", json.dumps([{"type": "noop", "key": coordsession.FULL_ASK}]))
    assert wake()[0]["sent"].startswith("delta: ")
    monkeypatch.delenv("TTP_FAKE_ACTIONS")
    assert wake()[0]["sent"] == "full: no acknowledged digest"
    assert wake()[0]["sent"].startswith("delta: ")
    monkeypatch.setattr(Daemon, "_compacted", lambda self, r: True)
    wake()
    monkeypatch.setattr(Daemon, "_compacted", lambda self, r: False)
    assert wake()[0]["sent"] == "full: no acknowledged digest"
    # Mode off: today's digest, and nothing of the delta runs.
    p.set_config("coordinator.session.mode", "off")
    monkeypatch.setattr(coordsession, "wake_digest", lambda *a: pytest.fail("off sends no delta"))
    s, prompt, _ = wake()
    assert s is None and "Since your last wake" not in prompt


def test_coordsession_delta_digest_size_on_recorded_wakes():
    """Recorded wakes (digestdelta_replay.json, sizes and hashes only) replayed through wake_digest:
    each wake resumes the session of the wake before. Prints prompt tokens per wake, full vs delta."""
    from ttp import coordsession as cs
    wakes = json.loads((_CASES / "digestdelta_replay.json").read_text())["wakes"]

    def parts(w):
        out = []
        for sec in w:
            if sec[0] == "head":
                out.append((cs.HEAD, "# STATE ".ljust(sec[1], "t") + "\n" + sec[3].ljust(sec[2], "b")))
            else:
                out.append((sec[0], f"## {sec[0]}\n{sec[2]}".ljust(sec[1], "x")))
        return out

    state, full_chars, sent_chars = None, 0, 0
    for i, w in enumerate(wakes):
        got = parts(w)
        sent, ack, why = cs.wake_digest("resume" if i else "new", state, got, got, "f", "f", "g")
        full_chars += len("\n".join(t for _, t in got))
        sent_chars += len("\n".join(t for _, t in sent))
        assert why.startswith("delta: ") if i else why == "full: new session", why
        state = {cs.DIGEST: ack}
    full_tok, delta_tok = full_chars // len(wakes) // 4, sent_chars // len(wakes) // 4
    print(f"digest prompt tokens per wake over {len(wakes)} recorded wakes: full {full_tok}, delta {delta_tok}")
    assert len(wakes) >= 100 and delta_tok < full_tok


def test_coordsession_whole_digest_keeps_the_lines_of_one_shot_section_builders(env, monkeypatch):
    """The whole digest (new session, daemon restart) comes from the turn's one digest build: a
    second build would find the ends and coverage builders already told and leave their lines out."""
    from ttp import coordinator as coord
    from ttp import ends, responsibilities
    from ttp.daemon import Daemon
    p = make(env)
    _sessions(env, monkeypatch)
    p.set_config("coordinator.session.mode", "on")
    p.set_config("coordinator.debounce_s", 0)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    assert coord.apply(p, [{"type": "memory_add", "text": "Hold releases.", "memory_kind": "restriction",
                            "until": "the freeze is lifted"}], turn=2) == []
    builds = []
    real = coord.digest_parts
    monkeypatch.setattr(coord, "digest_parts", lambda *a, **k: builds.append(1) or real(*a, **k))
    n = [0]

    def wake(d):
        n[0] += 1
        ends.digest_lines(p, 0, time.time())   # the entry recorded, then due: listed once
        p.db.set_kv(ends.LISTED_KEY, {k: v - ends.RECHECK_S - 1 for k, v in (p.db.kv(ends.LISTED_KEY) or {}).items()})
        p.db.set_kv(responsibilities.TOLD_KV, None)
        monkeypatch.setattr(responsibilities, "digest_lines", lambda db, n=n[0]: (
            [] if db.kv(responsibilities.TOLD_KV) else db.set_kv(responsibilities.TOLD_KV, [f"m{n}"]) or
            [f"## Health coverage changed: wake {n}"]))   # told once, like the real builder
        p.db.post("in", f"m{n[0]}", chat="c1")
        builds.clear()
        rid = _cs_wake(p, d)
        note = json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"])
        return note["turn"]["session"], (p.runs / str(rid) / "prompt.md").read_text()

    for d, sent in ((Daemon(p.base), "full: new session"), (Daemon(p.base), "full: daemon restarted")):
        s, prompt = wake(d)
        assert s["sent"] == sent, s
        assert builds == [1], "the digest is built once per turn"
        assert "## Temporary instructions possibly over" in prompt and "until the freeze is lifted" in prompt, sent
        assert f"## Health coverage changed: wake {n[0]}" in prompt, sent
