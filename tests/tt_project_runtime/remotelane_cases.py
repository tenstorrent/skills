# Remote-lane tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file. Add tests of the project's
# checks lanes for runs on other hosts (testslots.remote_lane) here.
from __future__ import annotations


def test_remote_runs_of_different_heads_share_the_project_cap_not_one_lane(env, tmp_path, monkeypatch):
    from ttp import checkcap, testslots
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    monkeypatch.setenv("TTP_TEST_PROJECT_WORKERS", "0")
    for k in (testslots.HELD_ENV, checkcap.HELD_ENV):
        monkeypatch.delenv(k, raising=False)
    key = testslots.repo_key(repo)
    told: list[str] = []
    lanes = []
    try:
        for n in range(3):   # a fixed cap of 3: three runs of one repository run side by side
            g = testslots.remote_lane(p.name, 3, told.append, 2.0, who=f"run {n}", repo=key)
            assert g is not None, f"run {n} waited behind another although the project cap of 3 has room"
            lanes.append(g)
        assert not any("one at a time" in t for t in told), told
        # The cap is the bound that holds: a fourth run waits and its line names checks.test_project_max.
        told.clear()
        assert testslots.remote_lane(p.name, 3, told.append, 0.3, who="run 3", repo=key) is None
        assert told and "3 at a time" in told[0] and "checks.test_project_max" in told[0], told
    finally:
        for g in lanes:
            g.close()
