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


def _cd_haiku(answer="no"):
    calls = []

    def run(argv, text, timeout):
        calls.append((argv, text))
        return {"result": answer, "usage": {"input_tokens": 400, "output_tokens": 2}}
    return run, calls


def _cd_fallback(monkeypatch, jev, answer="no", cfg=None, db=None):
    from ttp import cheapdecide
    monkeypatch.delenv(cheapdecide.OFF_ENV, raising=False)
    monkeypatch.setattr(cheapdecide, "_REFUSED", {})
    monkeypatch.setattr(cheapdecide, "_COOL", type("C", (), {"until": 0.0})())
    run, calls = _cd_haiku(answer)
    cfg = cfg or {}
    return cheapdecide.Fallback(jev, cheapdecide.Haiku(cfg, db, run=run, binary="claude-stub")), calls


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
    f, calls = _cd_fallback(monkeypatch, _CdJev(on=False), "skip it; rm -rf ~")
    q = {"needed": {"type": "noul", "instructions": "?", "criteria": {"true": "y", "false": "n"}}}
    assert f.decide("s", q, purpose="effort") is None and not calls   # effort and model stay Jev-only
    assert f.decide("s", q, purpose="screen") == {"needed": {"noul": 1.0}}
    sev = {"severity": {"type": "score", "instructions": "?", "criteria": ["a", "b", "c", "d"]}}
    assert f.decide("s", sev, purpose="screen") == {"severity": {"score": 3.0}}
    g, _ = _cd_fallback(monkeypatch, _CdJev(on=False), "1")
    assert g.decide("s", sev, purpose="screen") == {"severity": {"score": 1.0}}
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
