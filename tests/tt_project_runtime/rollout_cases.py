# Release-ring tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new release-ring tests here, not there.
from __future__ import annotations


def _roll(capsys, *argv):
    from ttp import cli
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["rollout", *argv])
    out = capsys.readouterr().out
    return e.value.code, out


def test_a_flag_is_off_until_its_ring_is_reached_and_a_projects_own_setting_wins(env, capsys):
    from ttp import rollout
    for who, ring in (("scratch1", "0"), ("canary", "1")):
        assert _roll(capsys, "ring", who, ring)[0] == 0
    assert _roll(capsys, "ring", "trays", "exclude")[0] == 0
    names = ("scratch1", "canary", "everyone", "trays")
    assert not any(rollout.on("deslop", n) for n in names), "flags are off by default"
    for lv, want in (("0", {"scratch1"}), ("1", {"scratch1", "canary"}), ("2", {"scratch1", "canary", "everyone"})):
        assert _roll(capsys, "set", "deslop", lv)[0] == 0
        assert {n for n in names if rollout.on("deslop", n)} == want, "an excluded project never takes a flag"
    assert _roll(capsys, "set", "deslop", "off")[0] == 0
    own = {"deslop": {"enabled": False}}
    assert rollout.on("deslop", "trays", own) and rollout.on("deslop", "everyone", own), "its own value wins"
    assert not rollout.on("deslop", "everyone", {"deslop": {}})
    assert not rollout.on("no_such_flag", "scratch1")


def test_ring_moves_and_exclusion_are_listed_once_and_undone(env, capsys):
    from ttp import rollout
    for r in ("0", "1", "exclude", "include", "2"):
        assert _roll(capsys, "ring", "alpha", r)[0] == 0
    assert rollout.ring_of("alpha") == 2 and rollout.settings().get("rings", {}) == {}
    _roll(capsys, "ring", "alpha", "1")
    _roll(capsys, "ring", "alpha", "0")
    assert rollout.settings()["rings"] == {"0": ["alpha"]} and rollout.ring_of("alpha") == 0
    code, out = _roll(capsys, "ring", "alpha", "9")
    assert code == 2 and "ring is" in out
    assert _roll(capsys, "ring", "alpha")[0] == 2


def test_promote_walks_off_0_1_2_and_every_change_is_ledgered(env, capsys):
    from ttp import rollout
    for want in (0, 1, 2):
        assert _roll(capsys, "promote", "glm_code", "--reason", "clean")[0] == 0
        assert rollout.level("glm_code") == want
    assert "every ring already" in _roll(capsys, "promote", "glm_code")[1]
    recs = rollout.ledger("glm_code")
    assert [(r["kind"], r["from"], r["to"]) for r in recs] == [("promote", "off", 0), ("promote", 0, 1), ("promote", 1, 2)]
    assert all(r["by"] == "user" and r["ts"] for r in recs) and recs[0]["reason"] == "clean"
    assert _roll(capsys, "set", "glm_code", "2")[1].endswith("already; nothing changed\n")
    assert len(rollout.ledger("glm_code")) == 3, "a no-op writes nothing"
    code, out = _roll(capsys, "set", "glm_code", "7")
    assert code == 2 and "off, 0, 1 or 2" in out
    code, out = _roll(capsys, "promote", "nope")
    assert code == 2 and "unknown flag" in out


def test_rollback_turns_the_flag_off_and_freezes_it_until_armed(env, capsys):
    from ttp import rollout
    _roll(capsys, "set", "eta_history", "1")
    assert "frozen" in _roll(capsys, "rollback", "eta_history", "--reason", "failed runs up")[1]
    assert rollout.level("eta_history") == rollout.OFF and rollout.frozen("eta_history")["reason"] == "failed runs up"
    code, out = _roll(capsys, "set", "eta_history", "0")
    assert code == 2 and "frozen" in out and rollout.level("eta_history") == rollout.OFF
    assert _roll(capsys, "promote", "eta_history")[0] == 2
    assert "re-armed" in _roll(capsys, "arm", "eta_history")[1]
    assert rollout.frozen("eta_history") is None
    assert "not frozen" in _roll(capsys, "arm", "eta_history")[1]
    assert _roll(capsys, "promote", "eta_history")[0] == 0 and rollout.level("eta_history") == 0
    assert [r["kind"] for r in rollout.ledger("eta_history")] == ["set", "rollback", "arm", "promote"]


def test_a_run_may_read_or_roll_back_but_never_widen_the_rollout(env, capsys, monkeypatch):
    from ttp import rollout
    _roll(capsys, "set", "deslop", "1")
    monkeypatch.setenv("TTP_RUN_DIR", "/nonexistent")
    for argv in (("ring", "x", "0"), ("set", "deslop", "2"), ("promote", "deslop"), ("arm", "deslop")):
        code, out = _roll(capsys, *argv)
        assert code == 2 and "only the user's terminal" in out
    assert rollout.level("deslop") == 1 and rollout.settings().get("rings") is None
    assert _roll(capsys, "status")[0] == 0
    assert _roll(capsys, "rollback", "deslop", "--reason", "bad")[0] == 0
    assert rollout.level("deslop") == rollout.OFF and rollout.ledger("deslop")[-1]["by"] == "run"


def test_rollout_state_is_account_wide_and_touches_no_project_file(env, capsys):
    from ttp import project, rollout
    p = make(env)
    before = {f: f.read_bytes() for f in p.base.rglob("*") if f.is_file() and f.suffix in (".json", ".md")}
    _roll(capsys, "ring", "someone", "1")
    _roll(capsys, "set", "push_isolation", "1")
    assert {f: f.read_bytes() for f in before} == before
    data = json.loads(project.account_settings_path().read_text())
    assert data["rollout"] == {"flags": {"push_isolation": 1}, "rings": {"1": ["someone"]}}
    assert oct(project.account_settings_path().stat().st_mode & 0o777) == "0o600"
    assert (project.HOME_DIR / rollout.LEDGER).exists()
    st = json.loads(_roll(capsys, "status", "--json")[1])
    assert st["flags"]["push_isolation"]["level"] == 1 and st["rings"] == {"1": ["someone"]}
    for key in ("flags", "rings", "excluded", "thresholds", "scratch_cap_usd"):
        assert f"rollout.{key}" in project.ACCOUNT_USER_ONLY and key in project.ACCOUNT_KEYS["rollout"]
