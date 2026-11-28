# Binding-cap tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file. A project's own
# budget.global_daily_usd against the account's: the digest line, the web line and the hold reason
# name the cap that binds.
from __future__ import annotations


def _projcap_gate(env, tmp_path, own, spent):
    """The gate, its caps line and its usage line, with the account cap $100 and this project's own
    budget.global_daily_usd `own` (None: not set), after `spent` dollars of the account's today."""
    p, other = _global_cap_setup(env, tmp_path, cap=100.0)
    if own is not None:
        p.set_config("budget.global_daily_usd", own)
    other.spend("fake", spent, "task:1")
    from ttp import budget as bud
    from ttp.web import usage_line
    now = time.time()
    g = bud.evaluate(p.db, p.config(), "fake", [], now)
    return g, bud.caps_line(p.db, {"fake": g.as_dict()}, p.config()["budget"], now), usage_line(g.numbers, now)


def test_a_project_cap_below_the_account_cap_is_named_as_the_one_that_binds(env, tmp_path):
    g, line, web = _projcap_gate(env, tmp_path, 50, 60.0)
    assert g.level == "red" and not g.allow_new_work, g.reasons
    assert any(r.startswith("project daily cap $50 reached: $60.00 today") and "below the account cap of $100" in r
               for r in g.reasons), g.reasons
    assert not any("global daily cap" in r for r in g.reasons), g.reasons
    assert line.startswith("project daily cap $50, below the account cap of $100"), line
    assert "$60.00 of $50 today" in line, line
    assert "$60.00 of $50 project cap (below the account cap of $100)" in web, web
    # A hold the user clears, like any dollar cap: its class and reset are the global cap's.
    from ttp import budget as bud
    assert bud.red_class(g.reasons[0]) == "global cap" and bud.user_held(g.as_dict()), g.reasons


def test_a_project_cap_equal_to_the_account_cap_reads_as_the_account_cap(env, tmp_path):
    g, line, web = _projcap_gate(env, tmp_path, 100, 120.0)
    assert any(r.startswith("global daily cap reached: $120 of $100 today") for r in g.reasons), g.reasons
    assert "global_account_cap" not in g.numbers
    assert "global daily cap (whole account, per provider)" in line and "project daily cap" not in line, line
    assert "of $100 global" in web and "project cap" not in web, web


def test_a_project_cap_above_the_account_cap_says_it_is_the_projects_and_higher(env, tmp_path):
    g, line, web = _projcap_gate(env, tmp_path, 150, 160.0)
    assert g.numbers["global_cap"] == 150 and g.level == "red", g.reasons
    assert any(r.startswith("project daily cap $150 reached") and "above the account cap of $100" in r
               for r in g.reasons), g.reasons
    assert "project daily cap $150, above the account cap of $100" in line, line
    assert "of $150 project cap (above the account cap of $100)" in web, web


def test_an_unset_project_cap_uses_the_account_cap_and_says_nothing_of_a_project_cap(env, tmp_path):
    g, line, web = _projcap_gate(env, tmp_path, None, 120.0)
    assert any(r.startswith("global daily cap reached: $120 of $100 today") for r in g.reasons), g.reasons
    assert "global_account_cap" not in g.numbers and "project daily cap" not in line, line
    assert "global daily cap (whole account, per provider)" in line and "project cap" not in web, (line, web)
