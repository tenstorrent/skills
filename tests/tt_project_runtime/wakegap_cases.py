# Wake gap and wake triage tests of tests/test_tt_project_runtime.py (eventhold.gap_until: event-only
# batches that may wait share one coordinator turn per coordinator.wake_gap_s, a free slot included;
# waketriage.py: the watcher model folds a routine batch into the next wake), which runs this file in its
# own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations

_WG_CHAT = {"endpoint": "placeholder", "base_url": "https://chat.example.invalid/v1",
            "tiers": {"light": {"model": "example-org/small-model", "route": True}}}


def _wg_setup(env, monkeypatch, busy=0, chat=False, haiku=False):
    """A turn that just ended, batch_s off, a free worker slot by default; `chat` turns the watcher on,
    `haiku` sets the Haiku fallback (coordinator.wake_triage_fallback, on by default) explicitly."""
    from ttp import configwho
    p, d, clock, starts = _batch_setup(env, monkeypatch, busy=busy)
    p.set_config("coordinator.batch_s", 0)
    if chat:
        p.set_config("providers.chat", _WG_CHAT, by=configwho.by("user", via="cli"))
    if haiku:
        p.set_config("coordinator.wake_triage_fallback", True, by=configwho.by("user", via="cli"))
    d.cfg = p.config()
    p.db.x("INSERT INTO runs(role, started, ended, status) VALUES('coordinator', ?, ?, 'ok')",
           (clock[0] - 60, clock[0]))
    p.db.set_kv("last_coordinator_turn", clock[0] - 60)
    notes = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: (starts.append(clock[0]),
                                                          notes.append((k.get("note") or {}).get("events"))))
    return p, d, clock, starts, notes


def test_wakegap_routine_events_in_the_gap_share_one_wake_even_with_a_free_slot(env, monkeypatch):
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch, busy=0)
    ended = clock[0]
    assert float(p.config()["coordinator"]["wake_gap_s"]) == 300
    for kind in ("task_done", "task_failed", "blocker_sweep", "urgent_check_wait"):
        clock[0] += 40   # events at +40, +110, +180 and +250 s, each checked for 30 s
        _event(p, clock, kind)
        assert not _turn_within(d, clock, starts, 30), f"{kind} woke the coordinator inside the wake gap"
    assert _turn_within(d, clock, starts, 120), "the wake gap ran past wake_gap_s"
    assert len(starts) == 1 and starts[0] >= ended + 300


def test_wakegap_user_messages_and_urgent_events_wake_at_once(env, monkeypatch):
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
    clock[0] += 10
    _event(p, clock, "task_done")
    p.db.x("INSERT INTO messages(ts,direction,chat,text,handled) VALUES(?,?,?,?,0)", (clock[0], "in", "web", "hi"))
    assert _turn_within(d, clock, starts, 60), "a user message waited on the wake gap"
    for kind, sev in (("task_done", "high"), ("obligation_overdue", "normal"), ("ask_timeout", "normal"),
                      ("machine_condition", "normal"), ("promise_overdue", "normal"), ("queue_wait", "normal"),
                      ("charter_retry", "normal"), ("outside_writes", "normal")):
        p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
        clock[0] += 10
        _event(p, clock, "task_done")
        _event(p, clock, kind, sev)
        assert _turn_within(d, clock, starts, 60), f"{kind}/{sev} waited on the wake gap"


def test_wakegap_off_wakes_a_free_slot_at_once(env, monkeypatch):
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
    p.set_config("coordinator.wake_gap_s", 0)
    d.cfg = p.config()
    clock[0] += 10
    _event(p, clock, "task_done")
    assert _turn_within(d, clock, starts, 60)


def test_waketriage_skip_folds_the_batch_into_the_next_wake(env, monkeypatch):
    from ttp import babysit
    p, d, clock, starts, notes = _wg_setup(env, monkeypatch, chat=True)
    asked = []
    monkeypatch.setattr(babysit, "_post", lambda s, messages, timeout: (asked.append(messages), {
        "choices": [{"message": {"content": "skip"}}], "usage": {"prompt_tokens": 40, "completion_tokens": 1}})[1])
    clock[0] += 400   # past the wake gap
    _event(p, clock, "task_done")
    eid = p.db.one("SELECT MAX(id) n FROM events")["n"]
    assert not _turn_within(d, clock, starts, 300), "a watcher skip still woke the coordinator"
    assert len(asked) == 1, "the same folded batch was asked again"
    assert "task_done happened" in asked[0][1]["content"]
    assert p.db.one("SELECT COUNT(*) n FROM ledger WHERE source='babysit:coordinator_wake'")["n"] == 1
    assert p.db.one("SELECT status FROM events WHERE id=?", (eid,))["status"] == "queued"   # kept on disk
    p.db.x("INSERT INTO messages(ts,direction,chat,text,handled) VALUES(?,?,?,?,0)", (clock[0], "in", "web", "hi"))
    assert _turn_within(d, clock, starts, 60)
    assert eid in notes[-1], "the folded event was missing from the next wake's digest"
    # Left folded, the batch still wakes once its oldest event is wake_triage_max_s old.
    p, d, clock, starts, notes = _wg_setup(env, monkeypatch, chat=True)
    clock[0] += 400
    _event(p, clock, "task_done")
    assert not _turn_within(d, clock, starts, 600)
    assert _turn_within(d, clock, starts, 1300), "a folded batch never woke the coordinator"


def test_waketriage_a_watcher_error_wakes_and_logs_the_model(env, monkeypatch, capsys):
    from ttp import babysit, daemon
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch, chat=True)
    lines = []
    monkeypatch.setattr(daemon, "log", lambda proj, text, *a, **k: lines.append(text))

    def down(s, messages, timeout):
        raise babysit.Unavailable("HTTP 503")
    monkeypatch.setattr(babysit, "_post", down)
    clock[0] += 400
    _event(p, clock, "task_done")
    assert _turn_within(d, clock, starts, 60), "a watcher error held the wake"
    assert any("triage failed" in t and "example-org/small-model" in t and "HTTP 503" in t for t in lines), lines


def _wg_haiku(monkeypatch, answer="wake: no"):
    """No chat provider; Haiku on, answering `answer` at once (no background thread); its calls listed."""
    from ttp import cheapdecide
    monkeypatch.delenv(cheapdecide.OFF_ENV, raising=False)
    real_settings = cheapdecide.settings   # the test project's core provider is fake; Claude's otherwise
    monkeypatch.setattr(cheapdecide, "settings", lambda cfg: real_settings(dict(cfg or {}, core_provider="claude")))
    monkeypatch.setattr(cheapdecide.Haiku, "binary", lambda self: "claude")
    real, calls = cheapdecide.Haiku, []
    monkeypatch.setattr(cheapdecide, "Haiku", lambda cfg, db=None, **k: real(
        cfg, db, run=lambda argv, text, timeout: (calls.append((argv, text)), {
            "result": answer, "usage": {"input_tokens": 300, "output_tokens": 2}})[1], background=False))
    return calls


def test_waketriage_without_a_chat_provider_haiku_holds_routine_events_until_max_s(env, monkeypatch):
    from ttp import waketriage
    calls = _wg_haiku(monkeypatch)
    p, d, clock, starts, notes = _wg_setup(env, monkeypatch, haiku=True)
    assert not (d.cfg.get("providers") or {}).get("chat") and waketriage.on(d.cfg)
    clock[0] += 400   # past the wake gap
    _event(p, clock, "task_done")
    _event(p, clock, "upstream_note")
    assert not _turn_within(d, clock, starts, 600), "a Haiku skip still woke the coordinator"
    assert len(calls) == 1 and "--model" in calls[0][0] and "low" in calls[0][0]
    assert "task_done happened" in calls[0][1] and "upstream_note happened" in calls[0][1]
    assert p.db.one("SELECT COUNT(*) n FROM ledger WHERE source='haiku:wake_decision'")["n"] == 1
    assert _turn_within(d, clock, starts, 1300), "a folded batch never woke the coordinator"
    assert len(calls) == 1
    # A user message wakes at once, without asking.
    p, d, clock, starts, notes = _wg_setup(env, monkeypatch, haiku=True)
    clock[0] += 400
    _event(p, clock, "task_done")
    assert not _turn_within(d, clock, starts, 60)
    n = len(calls)
    p.db.x("INSERT INTO messages(ts,direction,chat,text,handled) VALUES(?,?,?,?,0)", (clock[0], "in", "web", "hi"))
    assert _turn_within(d, clock, starts, 30) and len(calls) == n
    # A high event or an ask_ kind is never held.
    for kind, sev in (("task_done", "high"), ("ask_timeout", "normal")):
        p, d, clock, starts, _ = _wg_setup(env, monkeypatch, haiku=True)
        clock[0] += 400
        _event(p, clock, kind, sev)
        n = len(calls)
        assert _turn_within(d, clock, starts, 60) and len(calls) == n, (kind, sev)


def test_waketriage_haiku_wake_answer_wakes_and_the_fallback_switch_turns_it_off(env, monkeypatch):
    from ttp import configwho, waketriage
    calls = _wg_haiku(monkeypatch, answer="wake: yes")
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch, haiku=True)
    clock[0] += 400
    _event(p, clock, "task_done")
    assert _turn_within(d, clock, starts, 60) and len(calls) == 1
    p.set_config("coordinator.wake_triage_fallback", False, by=configwho.by("user", via="cli"))
    assert not waketriage.on(p.config())


def test_waketriage_haiku_fallback_is_on_by_default_and_its_off_switch_wakes_without_asking(env, monkeypatch):
    from ttp import configwho, project, waketriage
    calls = _wg_haiku(monkeypatch)
    assert project.DEFAULT_CONFIG["coordinator"]["wake_triage_fallback"] is True
    # On by default: with no config set, Haiku is asked and folds a routine event-only batch.
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
    assert waketriage.fallback_on(d.cfg)
    clock[0] += 400   # past the wake gap
    _event(p, clock, "task_done")
    assert not _turn_within(d, clock, starts, 60) and len(calls) == 1
    # Off-switch: coordinator.wake_triage_fallback false wakes without asking Haiku.
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
    p.set_config("coordinator.wake_triage_fallback", False, by=configwho.by("user", via="cli"))
    d.cfg = p.config()
    assert not waketriage.fallback_on(d.cfg)
    n = len(calls)
    clock[0] += 400
    _event(p, clock, "task_done")
    assert _turn_within(d, clock, starts, 60) and len(calls) == n, "Haiku decided a wake after its off-switch"


def test_waketriage_haiku_is_asked_yes_or_no_only_and_its_no_folds_the_batch(env, monkeypatch):
    """Haiku's question carries only ASK's yes/no form: the chat provider's skip/wake schema words in it let
    Haiku answer "skip", which ASK cannot read, so the coordinator woke anyway and the call was wasted."""
    from ttp import cheapdecide, waketriage
    ask = waketriage.ASK["wake"]["instructions"]
    assert "yes or no" in ask and not re.search(r"(?i)answer skip|wake coordinator", ask), ask
    assert "skip" in waketriage.QUESTION and "wake coordinator" in waketriage.QUESTION   # babysit's schema
    assert cheapdecide._read(waketriage.ASK, {"result": "skip"}) is None   # why the form must not offer it
    for answer, folded_want in (("no", True), ("wake: no", True), ("No.", True), ("yes", False)):
        with monkeypatch.context() as m:
            calls = _wg_haiku(m, answer=answer)
            p = make(env)
            p.db.x("DELETE FROM kv WHERE key=?", (waketriage.KEY,))
            p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) "
                   "VALUES(100,'task:1','task_done','normal','x','queued')")
            evs = [dict(r) for r in p.db.q("SELECT * FROM events")][-1:]
            folded, line = waketriage.fold(p.db, dict(p.config(), providers={}), evs, 160)
        assert len(calls) == 1 and not re.search(r"(?i)answer skip|wake coordinator", calls[0][1]), calls
        assert folded is folded_want and "triage failed" not in line, (answer, line)


def test_waketriage_haiku_pending_holds_the_batch_but_never_past_max_s(env):
    from ttp import waketriage
    p = make(env)
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(100,'task:1','task_done','normal','x','queued')")
    evs = [dict(r) for r in p.db.q("SELECT * FROM events")]

    class Pending:
        pending = True

        def decide(self, *a, **k):
            return None
    cfg = dict(p.config(), providers={})
    assert waketriage.fold(p.db, cfg, evs, 160, ask=waketriage._haiku_ask(Pending())) == (True, "")
    folded, line = waketriage.fold(p.db, cfg, evs, 100 + waketriage.MAX_S, ask=waketriage._haiku_ask(Pending()))
    assert not folded and "waited" in line
    Pending.pending = False   # no answer and none on its way: wake
    folded, line = waketriage.fold(p.db, cfg, evs, 160, ask=waketriage._haiku_ask(Pending()))
    assert not folded and "triage failed" in line


def _wg_msg(p, clock, text, provenance="cli-peer", context=None):
    p.db.x("INSERT INTO messages(ts,direction,chat,text,handled,provenance,context) VALUES(?,?,?,?,0,?,?)",
           (clock[0], "in", "web", text, provenance, context))


def test_wakegap_a_burst_of_relayed_status_messages_is_one_wake(env, monkeypatch):
    """#7778 part A: relayed messages with no order or question share one turn within relay_window_s."""
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
    assert float(p.config()["coordinator"]["relay_window_s"]) == 90
    first = clock[0]
    for text, ctx in (("status: the train is green, 3 changes landed", None), ("ok, thanks", None),
                      ("", "finding: the box is reachable again")):
        _wg_msg(p, clock, text, context=ctx)
        assert not _turn_within(d, clock, starts, 25), f"{text or ctx!r} woke a turn inside the relay window"
    assert _turn_within(d, clock, starts, 60), "the relay window ran past relay_window_s"
    assert len(starts) == 1 and first + 90 <= starts[0] < first + 100
    # Off (0): the same burst wakes at the debounce, one turn per spaced message as before.
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
    p.set_config("coordinator.relay_window_s", 0)
    d.cfg = p.config()
    _wg_msg(p, clock, "status: the train is green")
    assert _turn_within(d, clock, starts, 25)


def test_wakegap_a_relayed_order_or_question_or_the_users_own_message_still_wakes_within_the_window(env, monkeypatch):
    from ttp import msgwake
    for text, prov, ctx in (("Please stop task 12 now", "cli-peer", None), ("is the train green?", "cli-legacy", None),
                            ("thanks. Deploy the release to the box", "cli-peer", None),
                            ("as said", "cli-peer", "the user asks: why did 7 fail?"),
                            ("status: all green", "slack", None), ("ok", "web", None)):
        p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
        _wg_msg(p, clock, "status: the train is green")   # a waiting status message comes first
        clock[0] += 10
        _wg_msg(p, clock, text, prov, ctx)
        assert _turn_within(d, clock, starts, 25), f"{text!r} via {prov} waited on the relay window"
    m = lambda text, ctx=None: {"provenance": "cli-peer", "text": text, "context": ctx}   # noqa: E731
    assert not any(msgwake.urgent(m(t)) for t in ("ok, thanks", "noted", "status: 3 landed, train green",
                                                  "landed the fix; all checks pass"))
    assert all(msgwake.urgent(m(t)) for t in ("you must keep it under $300", "run the smoke suite",
                                              "check #7 and then fix it", "Never push to main", "why?"))


def test_wakegap_a_slow_tick_step_never_forces_a_coordinator_turn(env, monkeypatch):
    """#7778: a slow tick step's reports (slow, degraded, incident) ride along and never age into a turn."""
    from ttp import pagesev
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
    for kind, status in (("tick_step_slow", "handled"), ("tick_step_degraded", "queued"),
                         ("tick_step_incident", "queued")):
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,data) VALUES(?,?,?,?,?,?,?)",
               (clock[0] - pagesev.RIDE_ALONG_MAX_S - 600, "daemon", kind, "low", "slow", status,
                json.dumps({pagesev.RIDE_ALONG: True})))
    assert not _turn_within(d, clock, starts, 120), "a slow tick step's report started a turn"
    # Another ride-along that old still starts its own turn, as before.
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,data) VALUES(?,?,?,?,?,?,?)",
           (clock[0] - pagesev.RIDE_ALONG_MAX_S - 600, "daemon", "note", "low", "x", "queued",
            json.dumps({pagesev.RIDE_ALONG: True})))
    assert _turn_within(d, clock, starts, 120)


def test_a_tick_step_incident_rides_along(env, monkeypatch):
    from ttp import pagesev
    from ttp.daemon import Daemon
    d = Daemon(make(env).base)
    from ttp import daemon as dmod
    d._step_worst = (90.0, "item x", None)
    for i in range(dmod.STEP_INCIDENT_N):
        d._step_incident("probe", time.time() + i)
    (ev,) = d.p.db.q("SELECT severity, data FROM events WHERE kind='tick_step_incident'")
    assert pagesev.rides_along(dict(ev)) and json.loads(ev["data"])["step"] == "probe"
