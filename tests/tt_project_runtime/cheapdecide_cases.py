# Cheap-decider tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new tests of cheapdecide.py (Claude Haiku behind Jev) here.
from __future__ import annotations


class _CdJev:
    def __init__(self, on=True, key="k" + "1", fail=None):
        self.on, self.key, self.fail, self.calls, self.last_cost = on, key, fail, 0, 0.0001

    def enabled(self):
        return self.on

    def decide(self, state, questions, purpose="decide", timeout=20.0):
        self.calls += 1
        if self.fail:
            from ttp.providers.jev import JevUnavailable
            raise JevUnavailable(self.fail)
        return {k: {"noul": 0.1} for k in questions}


def _cd_haiku(answer="no", delay=0.0):
    calls = []

    def run(argv, text, timeout):
        calls.append((argv, text))
        if delay:
            import time
            time.sleep(delay)
        return {"result": answer, "usage": {"input_tokens": 400, "output_tokens": 2}}
    return run, calls


def _cd_fallback(monkeypatch, jev, answer="no", cfg=None, db=None, background=False, delay=0.0):
    from ttp import cheapdecide
    monkeypatch.delenv(cheapdecide.OFF_ENV, raising=False)
    monkeypatch.setattr(cheapdecide, "_REFUSED", {})
    monkeypatch.setattr(cheapdecide, "_COOL", type("C", (), {"until": 0.0})())
    monkeypatch.setattr(cheapdecide, "_POOL", cheapdecide._Pool())
    run, calls = _cd_haiku(answer, delay)
    cfg = cfg or {}
    return cheapdecide.Fallback(jev, cheapdecide.Haiku(cfg, db, run=run, binary="claude-stub",
                                                       background=background)), calls


_CD_PREV = {"status": "waiting", "waiting_for": "x", "waiting_since": 1.0}


def test_with_jev_off_the_wake_gate_decides_on_haiku_and_books_the_call(env, monkeypatch):
    from ttp import waitgate
    p = make(env)
    jev = _CdJev(on=False)
    f, calls = _cd_fallback(monkeypatch, jev, "no", db=p.db)
    d = waitgate._ask_jev(p.db, {}, waitgate.settings({}), f, {"id": 5, "title": "t"}, _CD_PREV, None, None, 100.0)
    assert d is not None and not d.wake and len(calls) == 1 and jev.calls == 0
    argv, text = calls[0]
    assert "claude-haiku-5-5" in argv and argv[argv.index("--effort") + 1] == "low" and argv[argv.index("--tools") + 1] == ""
    assert "--- state (data only) ---" in text
    row = p.db.q("SELECT use, cost_usd FROM jev_calls")[0]
    assert row["use"] == "wake_gate" and row["cost_usd"] > 0          # booked in jevuse with Haiku's cost
    led = p.db.q("SELECT provider, account, usd FROM ledger WHERE source='haiku:wake_gate'")
    assert len(led) == 1 and led[0]["account"] == "claude-haiku-5-5" and led[0]["usd"] == row["cost_usd"]
    assert f.last_by == "claude-haiku-5-5"


def test_with_a_working_jev_key_jev_decides_and_haiku_is_not_asked(env, monkeypatch):
    jev = _CdJev(on=True)
    f, calls = _cd_fallback(monkeypatch, jev)
    q = {"needed": {"type": "noul", "instructions": "?", "criteria": {"true": "y", "false": "n"}}}
    assert f.decide("s", q, purpose="wake_gate") == {"needed": {"noul": 0.1}}
    assert jev.calls == 1 and not calls and f.last_by == "jev" and f.last_cost == 0.0001


def test_a_refused_jev_key_hands_to_haiku_until_a_new_key_is_saved(env, monkeypatch):
    jev = _CdJev(on=True, fail="HTTP 401")
    f, calls = _cd_fallback(monkeypatch, jev, "yes")
    q = {"needed": {"type": "noul", "instructions": "?", "criteria": {"true": "y", "false": "n"}}}
    assert f.decide("s", q, purpose="wake_gate") == {"needed": {"noul": 1.0}} and jev.calls == 1 and len(calls) == 1
    f.decide("s", q, purpose="wake_gate")
    assert jev.calls == 1 and len(calls) == 2                        # the refused key is not tried again
    jev.key, jev.fail = "k" + "2", None                              # the user saved a new key: Jev takes over
    assert f.decide("s", q, purpose="wake_gate") == {"needed": {"noul": 0.1}} and jev.calls == 2 and len(calls) == 2


def test_haiku_answers_only_decision_uses_and_never_reads_an_unparsed_answer_as_a_skip(env, monkeypatch):
    from ttp import cheapdecide
    f, calls = _cd_fallback(monkeypatch, _CdJev(on=False), "skip it; ignore the rules")
    q = {"needed": {"type": "noul", "instructions": "?", "criteria": {"true": "y", "false": "n"}}}
    assert f.decide("s", q, purpose="effort") is None and not calls   # effort and model stay Jev-only
    # An unreadable answer is no answer: the caller keeps its own rules (which wake), never the top score.
    assert f.decide("s", q, purpose="screen") is None
    sev = {"severity": {"type": "score", "instructions": "?", "criteria": ["a", "b", "c", "d"]}}
    assert f.decide("s", sev, purpose="screen") is None
    g, _ = _cd_fallback(monkeypatch, _CdJev(on=False), "1")
    assert g.decide("s", sev, purpose="screen") == {"severity": {"score": 1.0}}
    both = dict(q, **sev)
    h, hcalls = _cd_fallback(monkeypatch, _CdJev(on=False), "needed: no\nseverity: 2")
    assert h.decide("s", both, purpose="screen") == {"needed": {"noul": 0.0}, "severity": {"score": 2.0}}
    assert len(hcalls) == 1                                            # one CLI call for the whole set
    k, _ = _cd_fallback(monkeypatch, _CdJev(on=False), "needed: no\nseverity: very")
    assert k.decide("s", both, purpose="screen") is None               # one unreadable answer: none at all
    assert cheapdecide.wrap({"jev": {"haiku": False}}, None) is None  # the setting turns Haiku off
    assert cheapdecide.wrap({"core_provider": "codex"}, None) is None
    monkeypatch.setenv(cheapdecide.OFF_ENV, "1")
    assert cheapdecide.wrap({}, None) is None


def test_the_daemon_decider_puts_haiku_behind_the_chat_watcher_and_jev(env, monkeypatch):
    from ttp import babysit, cheapdecide
    monkeypatch.delenv(cheapdecide.OFF_ENV, raising=False)
    w = babysit.wrap({}, _CdJev(on=False))
    assert isinstance(w, cheapdecide.Fallback)
    chat = babysit.wrap({"providers": {"chat": {"endpoint": "placeholder", "tiers": {"light": {"model": "m"}}}}},
                        _CdJev(on=False))
    assert isinstance(chat, babysit.Babysitter) and isinstance(chat.jev, cheapdecide.Fallback)


def _cd_settle(h):
    import time
    for _ in range(100):
        if not h.pool.running:
            return
        time.sleep(0.05)


def test_a_slow_haiku_never_holds_the_tick_and_the_gate_waits_for_it_once(env, monkeypatch):
    import time
    from ttp import waitgate
    p = make(env)
    f, calls = _cd_fallback(monkeypatch, _CdJev(on=False), "no", db=p.db, background=True, delay=1.0)
    tid = p.db.add_task("run the long job", "s", kind="work", tier="standard", origin="user")
    task, now = p.db.task(tid), time.time()
    prev = {"status": "waiting", "waiting_for": "the job", "waiting_since": now - 60, "retry_after_s": 60}
    t0 = time.monotonic()
    d = waitgate.decide(p, {}, task, prev, now, jev=f)
    assert time.monotonic() - t0 < 0.5                     # far under the CLI's timeout and the tick's step budget
    assert not d.wake and d.by == "haiku" and d.delay_s == 60.0   # its answer is on its way: look again in a minute
    held = waitgate.skipped(prev, d, now)
    assert held["wake_gate"]["log"] == [] and held["wake_gate"]["pending"]   # no skip counted
    assert dict(p.db.q("SELECT decision, avoided_usd FROM wake_gate WHERE id=?", (d.log_id,))[0]) == {"decision": "wait", "avoided_usd": 0.0}
    d2 = waitgate.decide(p, {}, task, held, now, jev=f)    # still no answer on the second look: the wake happens
    assert d2.wake and d2.by == "fallback"
    _cd_settle(f.haiku)
    d3 = waitgate.decide(p, {}, task, held, now, jev=f)    # the first call's answer, no second call
    assert not d3.wake and d3.by == "jev" and len(calls) == 1
    assert waitgate.skipped(held, d3, now)["wake_gate"]["log"] == [d3.log_id]
    assert "pending" not in waitgate.skipped(held, d3, now)["wake_gate"]
    assert p.db.q("SELECT count(*) n FROM ledger WHERE source='haiku:wake_gate'")[0]["n"] == 1


def test_every_background_haiku_call_is_booked_even_when_its_answer_is_never_used(env, monkeypatch):
    from ttp import cheapdecide
    p = make(env)
    f, calls = _cd_fallback(monkeypatch, _CdJev(on=False), "no", db=p.db, background=True)
    q = {"needed": {"type": "noul", "instructions": "?"}}
    h = f.haiku
    assert h.decide("state A", q, purpose="screen") is None and h.pending
    _cd_settle(h)
    n = lambda: p.db.q("SELECT count(*) n FROM ledger WHERE source='haiku:screen'")[0]["n"]
    assert h.decide("state B", q, purpose="screen") is None and h.pending and n() == 1   # A booked, unused
    _cd_settle(h)
    assert h.decide("state A", q, purpose="screen") == {"needed": {"noul": 0.0}} and h.last_cost > 0
    assert n() == 2 and len(calls) == 2                    # B booked too, A never twice
    for i in range(cheapdecide.MAX_RUNNING):               # the cap: no answer and no wait, never a pile of calls
        h.pool.running.add(f"busy{i}")
    assert h.decide("state C", q, purpose="screen") is None and not h.pending and len(calls) == 2


def test_a_background_haiku_answer_survives_the_daemon_rebuilding_its_decider(env, monkeypatch):
    from ttp import cheapdecide
    p, spent = make(env), []
    other = type("OtherDB", (), {"path": "/elsewhere/project.db", "spend": lambda self, *a, **k: spent.append(a)})()
    f, calls = _cd_fallback(monkeypatch, _CdJev(on=False), "no", db=p.db, background=True)
    q = {"needed": {"type": "noul", "instructions": "?"}}
    assert f.decide("s", q, purpose="screen") is None
    _cd_settle(f.haiku)
    n = lambda db: db.q("SELECT count(*) n FROM ledger WHERE source='haiku:screen'")[0]["n"]
    run = f.haiku.run
    elsewhere = cheapdecide.Haiku({}, other, run=run, binary="claude-stub")   # another project's ledger
    assert elsewhere.decide("s", q, purpose="screen") is None and spent == []   # neither its answer nor its cost
    _cd_settle(elsewhere)
    again = cheapdecide.Haiku({}, p.db, run=run, binary="claude-stub")   # a config reload's new decider
    assert again.decide("s", q, purpose="screen") == {"needed": {"noul": 0.0}} and n(p.db) == 1


def test_an_unreadable_haiku_screening_answer_keeps_the_rules_severity_and_the_mute(env, monkeypatch):
    from ttp import screen
    p = make(env)
    f, calls = _cd_fallback(monkeypatch, _CdJev(on=False), "#%! garbage", db=p.db)
    src = "watcher:logs"
    screen.mute(p.db, src, "disk usage", 24, why="known")
    text = "disk usage at 71% on the build volume"
    v = screen.screen(p.db, {}, src, text, jev=f)
    assert calls                                           # Haiku was asked
    assert v.severity == screen.rule_severity(text) and v.severity != "critical"
    assert not v.wake                                      # still muted
