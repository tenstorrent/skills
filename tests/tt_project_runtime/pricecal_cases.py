# Price calibration tests (localspend.calibrate, Daemon._check_price_table) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases
# there): helpers, fixtures and imports come from that file. The project corrects its own price-table
# drift within bounds and tells the user once per model and release, never asking them to edit it.
from __future__ import annotations

_PRICED = 15 + 0.3 + 3   # each run's log below at the claude-sonnet-4-5 row


def _priced_runs(tmp_path, monkeypatch, n=4, model="claude-sonnet-4-5-20250929"):
    logs = _claude_logs(tmp_path, monkeypatch)
    now, M = time.time(), 1_000_000
    for i in range(n):
        _log(logs, f"r{i}", [_call(f"r{i}", f"m{i}", now, model=model, output_tokens=M, cache_read_input_tokens=M)])
        _log(logs, f"r{i}", [_call(f"r{i}", f"s{i}", now, model=model, input_tokens=M)], sub="agent-x")
    return logs


def _price_daemon(env, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    d.cfg["budget"]["global_daily_usd"] = 100
    sent = []
    monkeypatch.setattr(d, "alert", lambda key, text, severity="high", every_s=6 * 3600:
                        sent.append((key, text, severity)))
    return p, d, sent


def _finish(d, sid, usd):
    d._check_price_table({"provider": "claude"}, types.SimpleNamespace(estimated=False, cost_usd=usd, session_id=sid))


def test_a_steady_small_drift_is_corrected_without_an_alert(env, tmp_path, monkeypatch):
    from ttp import globalcap as gcap
    from ttp import localspend as ls
    logs = _priced_runs(tmp_path, monkeypatch)
    p, d, sent = _price_daemon(env, monkeypatch)
    for day in range(3):   # day after day: still no alert
        for i in range(4):
            _finish(d, f"r{i}", _PRICED * 1.03)   # Claude Code reports 3% more than the table
    assert sent == [], sent
    m = ls.load_calibration()["models"]["claude-sonnet-4-5"]
    assert m["applied"] and abs(m["factor"] - 1.03) < 1e-3 and m["samples"] == ls.CALIBRATION_KEEP, m
    assert m["release"] and m["rates"] == list(ls.PRICES["claude-sonnet-4-5"])
    # The estimate of other sessions now counts the measured cost, not the table's.
    now = time.time()
    _log(logs, "other", [_call("other", "o1", now - 60, output_tokens=1_000_000)])
    b = _far_day(now)
    ls.scan(b, now)
    start, end = gcap.day_bounds(b, now)
    assert abs(ls.estimate(b, start, end, now)["usd"] - 15 * 1.03) < 1e-3
    # A run's own calibration still prices at the table, so the factor never feeds on itself.
    assert ls.session_cost("r0") == (_PRICED, False)


def test_a_large_drift_is_corrected_and_alerted_once_per_model_and_release(env, tmp_path, monkeypatch):
    from ttp import localspend as ls
    _priced_runs(tmp_path, monkeypatch)
    p, d, sent = _price_daemon(env, monkeypatch)
    for _ in range(3):
        for i in range(4):
            _finish(d, f"r{i}", _PRICED / 1.3)   # the table prices 30% above the reported cost
    assert len(sent) == 1, sent
    key, text, severity = sent[0]
    assert key.startswith("claude_price_table:drift:claude-sonnet-4-5:") and severity == "low"
    assert "30% above" in text and "scales" in text and "0.77" in text, text
    assert "update" not in text.lower() and "edit" not in text.lower() and "pricing.claude" not in text
    assert ls.factors(ls.table()[0]) == {"claude-sonnet-4-5": pytest.approx(1 / 1.3, abs=1e-3)}
    # Another daemon on this machine (a restart, another project) does not tell it again.
    from ttp.daemon import Daemon
    d2 = Daemon(p.base)
    d2.cfg["budget"]["global_daily_usd"] = 100
    monkeypatch.setattr(d2, "alert", lambda *a, **k: sent.append(a))
    _finish(d2, "r0", _PRICED / 1.3)
    assert len(sent) == 1
    # A new release may tell it once more.
    import ttp
    monkeypatch.setattr(ttp, "__version__", ttp.__version__ + ".1")
    _finish(d, "r1", _PRICED / 1.3)
    _finish(d, "r2", _PRICED / 1.3)
    assert len(sent) == 2


def test_an_unpriced_model_alerts_once_and_is_not_calibrated(env, tmp_path, monkeypatch):
    from ttp import localspend as ls
    _priced_runs(tmp_path, monkeypatch, model="claude-newmodel-9")
    p, d, sent = _price_daemon(env, monkeypatch)
    for _ in range(2):
        for i in range(4):
            _finish(d, f"r{i}", 5.0)
    assert len(sent) == 1, sent
    key, text, _ = sent[0]
    assert key.startswith("claude_price_table:unpriced:") and "claude-newmodel-9" in text, sent
    assert "edit" not in text.lower() and "update" not in text.lower()
    assert ls.load_calibration()["models"] == {} and not ls.load().get("calibration")


def test_a_factor_out_of_bounds_is_not_applied_and_alerts_once(env, tmp_path, monkeypatch):
    from ttp import globalcap as gcap
    from ttp import localspend as ls
    logs = _priced_runs(tmp_path, monkeypatch)
    p, d, sent = _price_daemon(env, monkeypatch)
    for i in range(4):
        _finish(d, f"r{i}", _PRICED * 1.1)   # in bounds: applied
    assert ls.factors(ls.table()[0])
    for _ in range(3):
        for i in range(4):
            _finish(d, f"r{i}", _PRICED * 3)   # 3x the table: past FACTOR_MAX
    assert [s[0].split(":")[1] for s in sent] == ["bound"], sent
    assert "outside" in sent[0][1] and "edit" not in sent[0][1].lower()
    m = ls.load_calibration()["models"]["claude-sonnet-4-5"]
    assert not m["applied"] and m["factor"] > ls.FACTOR_MAX, m
    assert ls.factors(ls.table()[0]) == {}, "the earlier in-bound factor still applies"
    now = time.time()
    _log(logs, "other", [_call("other", "o1", now - 60, output_tokens=1_000_000)])
    b = _far_day(now)
    ls.scan(b, now)
    start, end = gcap.day_bounds(b, now)
    assert ls.estimate(b, start, end, now)["usd"] == 15
    # A state file edited by hand past the bounds, or measured against other rates, is not applied.
    rates = list(ls.PRICES["claude-sonnet-4-5"])
    for bad in ({"factor": 5.0, "applied": True, "rates": rates}, {"factor": 0.1, "applied": True, "rates": rates},
                {"factor": 1.1, "applied": True, "rates": [1, 2, 3, 4, 5]}, {"factor": "x", "applied": True},
                {"factor": 1.1, "applied": True, "rates": 5}, {"factor": 1.1, "applied": "yes", "rates": rates},
                "1.1", [1.1]):
        ls.project.write_json(ls.calibration_path(), {"models": {"claude-sonnet-4-5": bad}})
        assert ls.factors(ls.table()[0]) == {}, bad
    for raw in ("{torn", "[]", '{"models": [1]}', '{"models": {"claude-sonnet-4-5": null}}'):
        ls.calibration_path().write_text(raw)
        assert ls.factors(ls.table()[0]) == {}, raw
        assert ls.scan(b, now)["ok"]
    # calibrate() starts the file over from a broken one.
    ls.calibration_path().write_text('{"models": [1], "alerted": {"x": 1}}')
    assert ls.calibrate("r0", _PRICED * 3)["samples"] and isinstance(ls.load_calibration()["models"], dict)


def test_inconsistent_runs_or_mixed_model_runs_change_no_factor(env, tmp_path, monkeypatch):
    from ttp import localspend as ls
    _priced_runs(tmp_path, monkeypatch)
    for i, x in enumerate((0.8, 1.0, 1.25, 1.5)):   # no DRIFT_RUNS of them agree within CONSISTENT
        got = ls.calibrate(f"r{i}", _PRICED * x)
    assert got["samples"] == 0 and not got["applied"] and ls.factors(ls.table()[0]) == {}, got
    # A run mostly on another model is not a pair of either.
    logs = ls.logs_root()
    now = time.time()
    _log(logs, "mix", [_call("mix", "a", now, output_tokens=1_000_000),
                       _call("mix", "b", now, model="claude-opus-4-1", output_tokens=1_000_000)])
    n = len(ls.load()["calibration"])
    assert ls.calibrate("mix", 90.0)["model"] == "claude-opus-4-1" and len(ls.load()["calibration"]) == n


def test_the_global_cap_still_trips_on_the_calibrated_estimate(env, tmp_path, monkeypatch):
    from ttp import budget
    from ttp import localspend as ls
    p = make(env)
    logs = _priced_runs(tmp_path, monkeypatch)
    for i in range(4):
        ls.calibrate(f"r{i}", _PRICED * 0.97)   # the table prices 3% above the reported cost
    assert ls.factors(ls.table()[0]) == {"claude-sonnet-4-5": pytest.approx(0.97, abs=1e-3)}
    now = time.time()
    _log(logs, "other", [_call("other", f"o{k}", now - 60, output_tokens=1_000_000) for k in range(7)])   # 7 x $15
    b = ls.account_budget()
    ls.scan(b, now)
    # The runs' own logs count too: (7 x 15 + 4 x 18.3) x 0.97 = 172.85; at the table it would be 178.2.
    for cap, red in ((172.0, True), (175.0, False)):
        g = budget.Gate("claude")
        budget._global(p.db, g, {**b, "global_daily_usd": cap}, "claude", None, now)
        assert (g.level == "red") is red, (cap, g.level, g.numbers)
        assert g.numbers["global_today"] == pytest.approx(172.85, abs=0.01), g.numbers
