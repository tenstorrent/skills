# `ttp remove` tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there). Add new tests of projectremove.py here.
from __future__ import annotations


def _pr_shared(env, name, base):
    """Rows of `name` (and of another project) in the shared per-machine records."""
    import json
    from ttp import alerts, machinecap
    machinecap.publish(name, 1)
    machinecap.publish("other", 2)
    alerts.claims_path().write_text(json.dumps({
        "host|a": {"alert": "a", "owner": str(base), "ts": 1.0}, "host|b": {"alert": "b", "owner": "/elsewhere/tt-project", "ts": 1.0}}))


def test_machinecap_forget_drops_only_that_projects_row_and_creates_nothing(env):
    import json
    from ttp import machinecap
    assert machinecap.forget("gone", env["home"]) is False
    assert not (env["home"] / machinecap.STATE).exists()
    machinecap.publish("gone", 1, home=env["home"])
    machinecap.publish("kept", 2, home=env["home"])
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
    own = make(env, "own")   # one repo holds one project: the same project is "its own" first, then another run's scratch one
    monkeypatch.setattr(cli, "stop_project", lambda proj, kill: "stopped")
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"] / "run"))
    monkeypatch.setenv("TTP_PROJECT", str(own.base))
    with pytest.raises(SystemExit) as e:
        cli.main(["remove", "own"])
    assert e.value.code == 2 and "cannot remove its own project" in capsys.readouterr().err
    assert "own" in project.load_registry()["projects"]
    monkeypatch.setenv("TTP_PROJECT", str(env["tmp"] / "some-other-project" / "tt-project"))
    cli.main(["remove", "own"])
    assert "own" not in project.load_registry()["projects"]


def test_remove_is_listed_among_the_admin_commands_and_a_glm_run_cannot_run_it():
    from ttp import cli, glmguard
    assert "remove" in dict(cli.COMMAND_GROUPS)["admin"]
    assert glmguard.cli_refusal("remove")


def test_a_deploy_check_follows_only_projects_registered_here_and_never_alerts_for_a_scratch_one(env, tmp_path):
    """A scratch project that was never registered gets no check and no alert, even when named; a registered
    project behind the release still alerts."""
    from ttp import alerts, deploy, overview
    p = make(env)
    _deploy_lib("0.5.0")
    _, db = _deploy_project(tmp_path, "alpha", "0.4.9")
    now = time.time()
    got = deploy.record(p, overview.deployed(names=["ghost"]), now)
    assert got["rc"] == 0 and got["projects"] == [], got
    assert not (p.db.kv(deploy.CHECKS) or {}) and not p.db.q("SELECT key FROM alerts"), "a ghost raised something"
    pages = []
    deploy.tick(p, lambda key, text, severity, **_: pages.append(key), now + 10 * deploy.GRACE_S)
    assert pages == []
    # a registered one behind the release is still judged and alerted
    from ttp import release
    db.add_task(release.UPGRADE_TASK_TITLE, "merge", kind="harness")
    report = overview.deployed(names=["alpha"])
    report["projects"][0]["takeover_at"] = now + 1
    assert deploy.record(p, report, now)["rc"] == 0
    due = now + 1 + deploy.GRACE_S
    deploy.tick(p, lambda key, text, severity, **_: pages.append(key), due + deploy.PROBE_S)
    deploy.tick(p, lambda key, text, severity, **_: pages.append(key), due + deploy.PROBE_S * 2)
    assert pages == ["deploy-behind:alpha"], pages
    assert alerts.holds(p.db, "deploy-behind:alpha", now, due)
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


def _pr_fake_units(monkeypatch, env, projects):
    """Fake systemd unit files (never the host's): one per project, running in its own folder."""
    from ttp import service
    d = env["tmp"] / "fake-units"
    d.mkdir(parents=True, exist_ok=True)
    for q in projects:
        (d / f"{service.unit_name(q)}.service").write_text(f"[Service]\nWorkingDirectory={q.base}\n")
    monkeypatch.setattr(service, "_unit", lambda q: d / f"{service.unit_name(q)}.service")
    return d


def _pr_named_as(p, name):
    """Give p's harness/project.json another project's name (a scratch project built from a copied config)."""
    import json
    f = p.harness / "project.json"
    cfg = json.loads(f.read_text())
    cfg["name"] = name
    f.write_text(json.dumps(cfg))


def test_removing_a_nested_project_that_carries_the_outer_ones_name_leaves_the_outer_project_alone(env, monkeypatch, capsys):
    """The outage: a scratch project nested in another's root, its config copied from that project, was removed and
    its remove stopped, uninstalled and unregistered the outer one. Now it is refused and nothing of the outer moves."""
    import pytest
    from ttp import cli, project
    outer = make(env, "outer")
    inner_root = outer.root / "scratch-inner"
    inner = cli.bootstrap(inner_root, "inner", "scratch", "fake")
    project.register("inner", {"host": "testhost", "dir": str(inner.root)})
    _pr_named_as(inner, "outer")
    units = _pr_fake_units(monkeypatch, env, [outer])
    stopped = []
    monkeypatch.setattr(cli, "stop_project", lambda proj, kill: stopped.append(proj.name) or "stopped")
    for argv in (["remove", "inner"], ["remove", "inner", "--delete-files"], ["stop", "inner"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2 and "calls itself 'outer'" in capsys.readouterr().err, argv
    assert stopped == [] and (units / "tt-project-outer.service").exists()
    reg = project.load_registry()["projects"]
    assert reg["outer"]["dir"] == str(outer.root) and "inner" in reg and inner.base.is_dir()


def test_remove_and_stop_refuse_when_the_named_projects_unit_registry_row_or_daemon_is_anothers(env, monkeypatch, capsys):
    import os
    import pytest
    from ttp import cli, project, projectremove
    p = make(env, "scratch")
    stopped = []
    monkeypatch.setattr(cli, "stop_project", lambda proj, kill: stopped.append(proj.name) or "stopped")
    elsewhere = env["tmp"] / "elsewhere" / "tt-project"
    elsewhere.mkdir(parents=True)
    # a unit of that name running another folder
    from ttp import service
    d = env["tmp"] / "units"
    d.mkdir()
    (d / "tt-project-scratch.service").write_text(f"[Service]\nWorkingDirectory={elsewhere}\n")
    monkeypatch.setattr(service, "_unit", lambda q: d / f"{service.unit_name(q)}.service")
    for argv in (["remove", "scratch"], ["stop", "scratch"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2 and "another project's, left alone" in capsys.readouterr().err
    (d / "tt-project-scratch.service").write_text(f"[Service]\nWorkingDirectory={p.base}\n")
    # a live pid in its daemon.pid that is the daemon of another folder
    (p.state / "daemon.pid").write_text("4242\n")
    from ttp import daemon
    monkeypatch.setattr(daemon, "_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(projectremove, "pid_dir", lambda pid: elsewhere)
    with pytest.raises(SystemExit):
        cli.main(["remove", "scratch"])
    assert "not this project's daemon" in capsys.readouterr().err
    monkeypatch.setattr(projectremove, "pid_dir", lambda pid: p.base)
    assert projectremove.identity_refusal(p, "scratch") is None
    (p.state / "daemon.pid").unlink()
    assert stopped == [] and "scratch" in project.load_registry()["projects"]
    # removal still works for its own project, and never drops a row that points elsewhere
    lines = projectremove.remove(p, lambda: "stopped", unregister=lambda n: stopped.append(("unreg", n)))
    assert "unregistered scratch" in lines and ("unreg", "scratch") in stopped
    project.register("scratch", {"host": "testhost", "dir": str(elsewhere.parent)})
    assert "is at" in projectremove.identity_refusal(p, "scratch")
    assert projectremove.remove(p, lambda: "stopped")[0].startswith("refused:")


def test_stop_never_acts_on_a_parent_project_found_by_walking_up(env, monkeypatch, capsys):
    import pytest
    from ttp import cli
    p = make(env, "outer")
    sub = p.root / "some" / "scratch-dir"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    monkeypatch.delenv("TTP_PROJECT", raising=False)
    monkeypatch.setattr(cli, "stop_project", lambda proj, kill: pytest.fail("stopped a parent"))
    with pytest.raises(SystemExit) as e:
        cli.main(["stop"])
    assert e.value.code == 2 and "found by walking up" in capsys.readouterr().err
    stopped = []
    monkeypatch.setattr(cli, "stop_project", lambda proj, kill: stopped.append(proj.name) or "stopped")
    monkeypatch.chdir(p.root)
    cli.main(["stop"])
    cli.main(["stop", "outer"])
    assert stopped == ["outer", "outer"]


def test_new_refuses_a_dir_inside_another_projects_root_and_names_the_scratch_place(env, monkeypatch, capsys):
    import pytest
    from ttp import cli, project
    outer = make(env, "outer")
    for d in (outer.root / "nested", outer.base / "harness" / "x"):
        with pytest.raises(SystemExit) as e:
            cli.main(["new", "glmdry", "--dir", str(d), "--no-service", "--provider", "fake"])
        err = capsys.readouterr().err
        assert e.value.code == 2 and "inside project outer's folder" in err and "scratch/glmdry" in err, err
        assert not (d / "tt-project").exists()
    assert "glmdry" not in project.load_registry()["projects"]
    # a name registered for another live project is not taken over
    with pytest.raises(SystemExit):
        cli.main(["new", "outer", "--dir", str(env["tmp"] / "elsewhere"), "--no-service", "--provider", "fake"])
    assert "already registered" in capsys.readouterr().err
    assert project.load_registry()["projects"]["outer"]["dir"] == str(outer.root)
    assert cli.new_refusal("glmdry", cli.scratch_dir("glmdry")) == ""


def test_adopt_never_takes_over_another_projects_name(env, capsys):
    import pytest
    from ttp import cli, project
    p = make(env, "outer")
    other = env["tmp"] / "other"
    with pytest.raises(SystemExit):
        cli.main(["adopt", "outer", "--dir", str(other)])
    assert "still holds that project" in capsys.readouterr().err
    q = cli.bootstrap(other, "inner", "scratch", "fake")
    _pr_named_as(q, "outer")
    with pytest.raises(SystemExit):
        cli.main(["adopt", "inner", "--dir", str(other)])
    assert "calls itself 'outer'" in capsys.readouterr().err
    assert project.load_registry()["projects"]["outer"]["dir"] == str(p.root)


def test_a_deploy_leaves_out_a_project_removed_since_it_was_listed_never_failing_on_it(env, tmp_path):
    """cell deploy_skips_removed: a project named by the deploy that is no longer registered, or whose
    registered folder holds no project any more, is left out and named; the rest still decide the verdict."""
    import shutil
    from ttp import overview, project
    _deploy_lib("0.5.0")
    _, db = _deploy_project(tmp_path, "alpha", "0.5.0")
    base, db2 = _deploy_project(tmp_path, "gone", "0.5.0")
    db2.close()
    shutil.rmtree(base)   # its folder deleted, its registry row left behind
    assert overview.deployed("0.5.0", names=["alpha", "ghost"])["rc"] == 2   # a plain check names a typo
    got = overview.deployed("0.5.0", names=["alpha", "gone", "ghost"], skip_unregistered=True)
    assert got["rc"] == 0, got["text"]
    assert [r["name"] for r in got["projects"]] == ["alpha"] and sorted(got["gone"]) == ["ghost", "gone"]
    assert "ghost\tleft out: no longer registered" in got["text"] and "gone\tleft out: its folder" in got["text"]
    # a registered folder whose project database is gone (a half-removed scratch project) is left out too
    nodb, db3 = _deploy_project(tmp_path, "nodb", "0.5.0")
    db3.close()
    (nodb / "state" / "project.db").unlink()
    got = overview.deployed("0.5.0", names=["alpha", "nodb"], skip_unregistered=True)
    assert got["rc"] == 0 and got["gone"] == ["nodb"] and got["text"].count("nodb\tleft out") == 1
    # a removed project never hides a real failure of another one
    _deploy_project(tmp_path, "beta", "0.4.9")[1].close()
    assert overview.deployed("0.5.0", names=["beta", "gone"], skip_unregistered=True)["rc"] != 0
    assert overview.deployed("0.5.0", names=["gone"])["rc"] == 2   # a plain check still reports it
    assert "gone" in project.load_registry()["projects"]   # read only: the row is not touched
    db.close()


@pytest.mark.parametrize("delete", [False, True])
def test_remove_drops_the_registry_row_even_when_its_daemon_is_gone_or_a_step_fails(env, monkeypatch, capsys, delete):
    """A8: `ttp remove` (with or without --delete-files) always drops the removed project's registry row, also
    when its daemon or unit is already gone and the stop step fails, or a shared record cannot be pruned."""
    from ttp import cli, project, projectremove
    for name in ("scratch",):
        p = make(env, name)
        project.register("other", {"host": "testhost", "dir": str(env["home"] / "other-repo")})

        def boom(proj, kill):
            raise RuntimeError("no unit tt-project-" + proj.name)
        monkeypatch.setattr(cli, "stop_project", boom)
        monkeypatch.setattr(projectremove, "pruners", lambda q: [("machine-wide worker record", lambda: 1 / 0)])
        cli.main(["remove", name] + (["--delete-files"] if delete else []))
        out = capsys.readouterr().out
        assert "not stopped: no unit" in out and "not pruned machine-wide worker record" in out, out
        assert f"unregistered {name}" in out
        assert name not in project.load_registry()["projects"] and "other" in project.load_registry()["projects"]
        assert p.base.is_dir() != delete


def _remote_entries(*names):
    from ttp import project
    for n in names:
        project.register(n, {"host": "nowhere.invalid", "dir": f"/srv/{n}"})


def test_remove_force_drops_only_the_row_of_a_remote_project_without_contacting_its_host(env, monkeypatch, capsys):
    from ttp import cli, outbox, project
    _remote_entries("gone", "kept")
    outbox.add("gone", ["say", "hi"], "c1", None)
    monkeypatch.setattr(cli, "forward", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ssh was used")))
    monkeypatch.setattr(cli, "flush_outbox", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ssh was used")))
    cli.main(["remove", "gone", "--force"])
    out = capsys.readouterr().out
    assert "unregistered gone" in out and "not contacted" in out and "left as it was" in out and "1 message(s) still queued" in out
    assert list(project.load_registry()["projects"]) == ["kept"]


def test_remove_without_force_still_forwards_a_remote_project_and_keeps_its_row(env, monkeypatch):
    import sys
    import pytest
    from ttp import cli, project
    _remote_entries("far")
    sent = []
    monkeypatch.setattr(cli, "flush_outbox", lambda *a, **k: None)
    monkeypatch.setattr(cli, "forward", lambda entry, argv: sent.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["ttp", "remove", "far"])   # need() forwards the command line as typed
    with pytest.raises(SystemExit) as e:
        cli.main(["remove", "far"])
    assert e.value.code == 0 and sent and sent[0][0] == "remove"
    assert "far" in project.load_registry()["projects"]


def test_remove_force_on_a_local_project_removes_it_as_before(env, monkeypatch, capsys):
    from ttp import cli, project
    make(env, "scratch")
    monkeypatch.setattr(cli, "stop_project", lambda proj, kill: "stopped")
    cli.main(["remove", "scratch", "--force"])
    assert "unregistered scratch" in capsys.readouterr().out and "scratch" not in project.load_registry()["projects"]


def test_register_refuses_a_project_inside_a_runs_tmp_folder_unless_the_registry_is_inside_it_too(env, monkeypatch):
    import pytest
    from ttp import project
    scratch = env["tmp"] / "state" / "runs" / "7" / "tmp" / "demo"
    real_home = env["tmp"] / "real-home"
    monkeypatch.setattr(project, "HOME_DIR", real_home)
    with pytest.raises(project.RegistryRefused, match="TTP_HOME inside that tmp folder"):
        project.register("demo", {"host": "testhost", "dir": str(scratch)})
    assert not (real_home / "registry.json").exists()
    project.register("ok", {"host": "testhost", "dir": str(env["tmp"] / "state" / "runs" / "7" / "other")})   # not the tmp folder
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "state" / "runs" / "7" / "tmp" / "home")   # a test's own TTP_HOME
    project.register("demo", {"host": "testhost", "dir": str(scratch)})
    assert "demo" in project.load_registry()["projects"]


def test_adopt_into_a_registry_outside_a_runs_tmp_folder_is_refused_cleanly(env, monkeypatch, capsys):
    import pytest
    from ttp import cli, project
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "real-home")
    scratch = env["tmp"] / "state" / "runs" / "9" / "tmp" / "demo"
    with pytest.raises(SystemExit) as e:
        cli.main(["adopt", "demo", "--dir", str(scratch)])
    assert e.value.code != 0 and "not registering 'demo'" in capsys.readouterr().err
