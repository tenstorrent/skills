# `ttp remove` tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there). Add new tests of projectremove.py here.
from __future__ import annotations


def _pr_shared(env, name, base):
    """Rows of `name` (and of another project) in the shared per-machine records."""
    import json
    from ttp import alerts, machinecap
    machinecap.publish(name, {}, 1)
    machinecap.publish("other", {}, 2)
    alerts.claims_path().write_text(json.dumps({
        "host|a": {"alert": "a", "owner": str(base), "ts": 1.0}, "host|b": {"alert": "b", "owner": "/elsewhere/tt-project", "ts": 1.0}}))


def test_machinecap_forget_drops_only_that_projects_row_and_creates_nothing(env):
    import json
    from ttp import machinecap
    assert machinecap.forget("gone", env["home"]) is False
    assert not (env["home"] / machinecap.STATE).exists()
    machinecap.publish("gone", {}, 1, home=env["home"])
    machinecap.publish("kept", {}, 2, home=env["home"])
    assert machinecap.forget("gone", env["home"]) is True and machinecap.forget("gone", env["home"]) is False
    assert list(json.loads((env["home"] / machinecap.STATE).read_text())["projects"]) == ["kept"]


def test_alert_claims_of_a_removed_project_are_dropped_and_others_kept(env):
    import json
    from ttp import alerts
    env["home"].mkdir(parents=True, exist_ok=True)
    alerts.claims_path().write_text(json.dumps({
        "h|a": {"alert": "a", "owner": "/x/tt-project", "ts": 1.0}, "h|b": {"alert": "b", "owner": "/y/tt-project", "ts": 1.0}}))
    assert alerts.forget_claims("/x/tt-project") == 1 and alerts.forget_claims("/x/tt-project") == 0
    assert list(json.loads(alerts.claims_path().read_text())) == ["h|b"]
    alerts.claims_path().unlink()
    assert alerts.forget_claims("/x/tt-project") == 0   # no file: nothing made


def test_remove_stops_prunes_shared_rows_and_unregisters_but_keeps_files_by_default(env, monkeypatch, capsys):
    import json
    from ttp import cli, machinecap, project
    p = make(env, "scratch")
    project.register("other", {"host": "testhost", "dir": str(env["home"] / "other-repo")})   # registered only: one repo holds one project
    _pr_shared(env, "scratch", p.base)
    monkeypatch.setattr(cli, "stop_project", lambda proj, kill: f"stopped {proj.name} kill={kill}")
    cli.main(["remove", "scratch"])
    out = capsys.readouterr().out
    assert "stopped scratch kill=True" in out and "pruned machine-wide worker record" in out and "unregistered scratch" in out
    assert "files kept" in out and p.base.is_dir()
    assert "scratch" not in project.load_registry()["projects"] and "other" in project.load_registry()["projects"]
    rows = json.loads((env["home"] / machinecap.STATE).read_text())["projects"]
    assert "scratch" not in rows and "other" in rows
    from ttp import alerts
    claims = json.loads(alerts.claims_path().read_text())
    assert list(claims) == ["host|b"]


def test_remove_deletes_the_tt_project_folder_only_with_the_flag_and_keeps_the_code_repo(env, monkeypatch, capsys):
    from ttp import cli, project
    p = make(env, "scratch")
    root, base = p.root, p.base
    (root / "notes.txt").write_text("the user's own file\n")
    monkeypatch.setattr(cli, "stop_project", lambda proj, kill: "stopped")
    cli.main(["remove", "scratch", "--delete-files"])
    assert "deleted" in capsys.readouterr().out
    assert not base.exists() and root.is_dir() and (root / "notes.txt").read_text() == "the user's own file\n"
    assert "scratch" not in project.load_registry()["projects"]


def test_remove_refuses_to_delete_a_folder_that_is_not_a_tt_project_folder(env, tmp_path):
    from types import SimpleNamespace
    from ttp import projectremove
    odd = tmp_path / "home-like"
    (odd / "harness").mkdir(parents=True)
    p = SimpleNamespace(base=odd, root=tmp_path, state=odd / "state", worktrees=odd / "worktrees", name="odd")
    assert "not a tt-project folder" in projectremove.delete_refusal(p)
    assert projectremove.delete_files(p)[0].startswith("files kept") and odd.is_dir()


def test_a_run_cannot_remove_its_own_project_but_can_remove_a_scratch_one(env, monkeypatch, capsys):
    import pytest
    from ttp import cli, project
    own, scratch = make(env, "own"), make(env, "scratch")
    monkeypatch.setattr(cli, "stop_project", lambda proj, kill: "stopped")
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"] / "run"))
    monkeypatch.setenv("TTP_PROJECT", str(own.base))
    with pytest.raises(SystemExit) as e:
        cli.main(["remove", "own"])
    assert e.value.code == 2 and "cannot remove its own project" in capsys.readouterr().err
    assert "own" in project.load_registry()["projects"]
    cli.main(["remove", "scratch"])
    assert "scratch" not in project.load_registry()["projects"]


def test_remove_is_listed_among_the_admin_commands_and_a_glm_run_cannot_run_it():
    from ttp import cli, glmguard
    assert "remove" in dict(cli.COMMAND_GROUPS)["admin"]
    assert "remove" not in glmguard.SUBS_ALLOWED


def test_a_deploy_check_follows_only_projects_registered_here_and_never_alerts_for_a_scratch_one(env, tmp_path):
    """A scratch project that was never registered gets no check and no alert, even when named; a registered
    project behind the release still alerts."""
    from ttp import alerts, deploy, overview
    p = make(env)
    _deploy_lib("0.5.0")
    _, db = _deploy_project(tmp_path, "alpha", "0.5.0", running="0.4.9")
    now = time.time()
    got = deploy.record(p, overview.deployed(names=["ghost"]), now)
    assert got["rc"] == 0 and got["projects"] == [], got
    assert not (p.db.kv(deploy.CHECKS) or {}) and not p.db.q("SELECT key FROM alerts"), "a ghost raised something"
    pages = []
    deploy.tick(p, lambda key, text, severity, **_: pages.append(key), now + 10 * deploy.GRACE_S)
    assert pages == []
    # a registered one behind the release is still judged and alerted
    db.set_kv("daemon", {"pid": 0, "host": "testhost"})
    deploy.record(p, overview.deployed(names=["alpha"]), now)
    deploy.tick(p, lambda key, text, severity, **_: pages.append(key), now + 10 * deploy.GRACE_S)
    assert pages == ["deploy-behind:alpha"], pages
    assert alerts.holds(p.db, "deploy-behind:alpha", now, now + 10 * deploy.GRACE_S)
    db.close()


def test_an_open_deploy_alert_closes_when_its_project_is_removed(env, tmp_path):
    from ttp import alerts, deploy, overview, project
    p = make(env)
    _deploy_lib("0.5.0")
    _, db = _deploy_project(tmp_path, "scratch", "0.5.0", running="0.4.9")
    db.set_kv("daemon", {"pid": 0, "host": "testhost"})
    now = time.time()
    deploy.record(p, overview.deployed(names=["scratch"]), now)
    deploy.tick(p, lambda *a, **k: None, now + 10 * deploy.GRACE_S)
    t = now + 10 * deploy.GRACE_S
    assert deploy.holds_behind(p.db, "scratch", t) and alerts.holds(p.db, "deploy-behind:scratch", now, t)
    project.unregister("scratch")
    t += deploy.PROBE_S
    pages = []
    deploy.tick(p, lambda key, text, severity, **_: pages.append(key), t)
    recs = [r for r in (p.db.kv(deploy.CHECKS) or {}).values() if r.get("name") == "scratch"]
    assert recs and all(r.get("closed") and r.get("outcome") == "removed" for r in recs), recs
    assert pages == [] and not deploy.holds_behind(p.db, "scratch", t)
    assert not alerts.holds(p.db, "deploy-behind:scratch", now, t), "its alert must clear"
    db.close()
