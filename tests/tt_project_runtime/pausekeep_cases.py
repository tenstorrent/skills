# Pause-survival tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file. A project pause (with who set it,
# why, since when and its end) and a resource pause outlive every upgrade path: `ttp upgrade` with a pending
# template merge, the host-side takeover (`--take-over-from`), the daemon's automatic upgrade and an
# old-state migration. Only `ttp resume` lifts one, and it names the pause it ended.
from __future__ import annotations

ACTORS = ["user", "worker run 7", "coordinator"]


def _pause_as(p, by):
    """Pause project p as `by` (with a reason and an end) and pause one resource the same way."""
    from ttp.coordinator import pause_resource
    p.db.set_paused(True, by=by, via="CLI", reason="handed back to its owner", end="until the owner resumes it")
    pause_resource(p, "box-a", True, reason="handed back to its owner", by=by)
    return p.db.kv("pause_info"), p.db.paused_resources()["box-a"]


def _assert_kept(p, info, res, step):
    db = getattr(p, "db", p)                            # a project, or a bare DB after a migration
    assert db.kv("paused") is True, f"{step} lifted the project pause"
    assert db.kv("pause_info") == info, f"{step} changed who paused it, why or its end"
    assert db.paused_resources().get("box-a") == res, f"{step} lifted or changed the resource pause"
    assert not db.q("SELECT id FROM events WHERE kind='project_pause' AND data LIKE '%\"resume\"%'"), step


@pytest.mark.parametrize("by", ACTORS)
def test_a_pause_survives_an_upgrade_with_a_pending_merge_its_takeover_and_a_migration(env, monkeypatch, capsys, by):
    """A paused project whose template merge conflicts: `ttp upgrade` queues the merge task, the host-side
    takeover of that task (files-only merge, daemon restart) and an old-state migration on reopen each keep
    the pause, its owner, reason and end, and the resource pause, whoever set them."""
    from ttp import cli, projpause, release
    from ttp.db import DB
    p, restarts = _conflicting_upgrade(env, monkeypatch)
    info, res = _pause_as(p, by)
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "demo"])                   # the merge conflicts: a harness task is queued for it
    tid = p.db.one("SELECT id FROM tasks WHERE kind='harness'")["id"]
    assert release.finish_holder(p)["task"] == tid
    _assert_kept(p, info, res, "an upgrade with a pending template merge")
    _install_template(env, {"kind-harness.md": "# Harness task, this project's way\n"})
    capsys.readouterr()
    cli.main(["upgrade", "demo", "--take-over-from", str(tid)])   # the host-side takeover
    assert "takes it over" in capsys.readouterr().out and restarts == [1]
    _assert_kept(p, info, res, "the host-side takeover")
    old = DB(p.db.path)                                 # an old-state migration: every migration runs on open
    old._migrate()
    _assert_kept(old, info, res, "a migration")
    assert projpause.current(old)["by"] == by


def test_an_unrecorded_old_pause_survives_a_migration_and_reads_as_unknown(env):
    """State from before pause records (the flag alone, no pause_info) stays paused through the migrations
    and reads as paused by unknown, never as running."""
    from ttp import projpause
    from ttp.db import DB
    p = make(env)
    p.db.set_kv("paused", True)
    db = DB(p.db.path)
    db._migrate()
    assert db.kv("paused") is True and projpause.current(db)["by"] == projpause.UNKNOWN


def test_the_automatic_upgrade_never_starts_or_resumes_a_paused_project(env, monkeypatch):
    """A paused project behind a release (a user's long pause with an upgrade pending): the daemon's
    automatic upgrade does not start while it is paused, and the pause and the resource pause stay."""
    p, d, launches = _release_daemon(env, monkeypatch)
    info, res = _pause_as(p, "user")
    for _ in range(3):
        d._release_due = 0
        d.check_release()
    assert launches == []
    _assert_kept(p, info, res, "the automatic upgrade")


@pytest.mark.parametrize("by", ["worker run 7", "coordinator"])
def test_resume_names_the_pause_it_ends_and_whose_it_was(env, capsys, by):
    """Replays a worker's hand-back pause lifted by `ttp resume` run from a chat outside any run: the
    event said 'resumed by the user (CLI)' and the next upgrade was blamed for it. Now `ttp resume`
    prints whose pause it ended, why and since when, and says it was not the user's."""
    from ttp import cli
    p = make(env)
    p.db.set_paused(True, by=by, via="CLI", reason="handed back to its owner")
    capsys.readouterr()
    cli.main(["resume", "demo"])
    out = capsys.readouterr().out
    assert "demo resumed" in out and "ended the pause: paused by " in out
    assert "handed back to its owner" in out and "it was not the user's" in out
    assert p.db.kv("paused") is False


def test_resume_of_a_users_own_pause_names_it_without_a_warning(env, capsys):
    from ttp import cli
    p = make(env)
    p.db.set_paused(True, by="user", via="CLI", reason="hold for now")
    capsys.readouterr()
    cli.main(["resume", "demo"])
    out = capsys.readouterr().out
    assert "ended the pause: paused by the user (CLI)" in out and "hold for now" in out
    assert "not the user's" not in out


def _as_run(monkeypatch, p, run="7", role=None):
    if role:   # caller() reads the run's role from its row: without one a run is a worker
        run = str(p.db.x("INSERT INTO runs(role,provider,started,status) VALUES(?,'claude',?,'running')",
                         (role, time.time())))
    monkeypatch.setenv("TTP_RUN_ID", run)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))


def test_a_worker_never_re_pauses_a_project_the_user_resumed(env, monkeypatch, capsys):
    """Replays a hand-back: a worker paused the project, the user resumed it to put it back to work, and
    minutes later a worker paused it again because a watcher said the other owner was live. Now a run's
    pause of a project the user resumed is refused and changes nothing; a newer user message, cited with
    --user-msg, still lets it through, and an older one does not."""
    from ttp import cli, projpause
    p = make(env)
    p.db.set_paused(True, by="worker run 5", via="CLI", reason="handed back to its owner")
    old_msg = p.db.x("INSERT INTO messages(ts,direction,text) VALUES(?,'in','keep it handed back')",
                     (time.time() - 60,))
    cli.main(["resume", "demo"])                         # the user, outside any run
    _as_run(monkeypatch, p)
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["pause", "demo", "--reason", "the other owner looks live"])
    assert e.value.code not in (0, None) and p.db.kv("paused") is False
    assert "the user resumed the project last" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["pause", "demo", "--reason", "x", "--user-msg", str(old_msg)])
    assert p.db.kv("paused") is False
    new_msg = p.db.x("INSERT INTO messages(ts,direction,text) VALUES(?,'in','pause it again')",
                     (time.time() + 1,))
    cli.main(["pause", "demo", "--reason", "the user asked again", "--user-msg", str(new_msg)])
    assert p.db.kv("paused") is True and projpause.current(p.db)["by"] == "worker run 7"


@pytest.mark.parametrize("role", [None, "coordinator"])
def test_a_run_never_re_pauses_a_resource_the_user_resumed(env, monkeypatch, capsys, role):
    from ttp import cli, projpause
    from ttp.coordinator import pause_resource
    p = make(env)
    pause_resource(p, "box-a", True, reason="handed back to its owner", by="worker")
    cli.main(["resume", "demo", "--resource", "box-a"])  # the user
    assert "box-a" not in p.db.paused_resources()
    _as_run(monkeypatch, p, role=role)
    assert cli._pause_by(p) == (role or "worker")
    with pytest.raises(SystemExit):
        cli.main(["pause", "demo", "--resource", "box-a", "--reason", "looks live", "--until", "2d"])
    assert "box-a" not in p.db.paused_resources() and "the user resumed resource box-a" in capsys.readouterr().err


def test_the_coordinators_resource_pause_action_never_re_pauses_what_the_user_resumed(env):
    """The coordinator's own resource_pause action is gated like `ttp pause --resource`; the same action
    in a turn carrying the user's words asking for it is the user's pause and goes through."""
    from ttp import cli
    from ttp import coordinator as coord
    from ttp.coordinator import pause_resource
    p = make(env)
    pause_resource(p, "box-a", True, reason="handed back to its owner", by="coordinator")
    cli.main(["resume", "demo", "--resource", "box-a"])  # the user
    act = {"type": "resource_pause", "resource": "box-a", "paused": True, "reason": "looks live", "until": "1d"}
    rejected = coord.apply(p, [act])
    assert rejected and "the user resumed resource box-a" in str(rejected)
    assert "box-a" not in p.db.paused_resources()
    msg = p.db.post("in", "Pause box-a again until I say so.")
    assert coord.apply(p, [act], user_turn=True, messages=[msg]) == []
    assert p.db.paused_resources()["box-a"]["by"] == "user"


@pytest.mark.parametrize("args", [["drain", "demo"], ["pause", "demo", "--until-reboot", "--reason", "kernel update"]])
def test_a_runs_host_cycle_pause_after_a_user_resume_goes_through(env, monkeypatch, args):
    """The own-host reboot procedure (`ttp drain`, a pause until reboot) is lifted by the run or the
    daemon itself, so a user's earlier resume never blocks it."""
    from ttp import cli
    p = make(env)
    p.db.set_paused(True, by="worker run 5", via="CLI", reason="handed back to its owner")
    cli.main(["resume", "demo"])                         # the user
    _as_run(monkeypatch, p)
    cli.main(args)
    assert p.db.kv("paused") is True


def test_a_users_resume_binds_runs_only_for_a_few_days(env, monkeypatch):
    from ttp import cli, projpause
    p = make(env)
    p.db.set_paused(True, by="worker run 5", via="CLI", reason="handed back to its owner")
    cli.main(["resume", "demo"])                         # the user
    p.db.x("UPDATE events SET ts=ts-? WHERE kind='project_pause'", (projpause.REPAUSE_GUARD_S + 60,))
    _as_run(monkeypatch, p)
    cli.main(["pause", "demo", "--reason", "host maintenance"])
    assert p.db.kv("paused") is True


def test_a_pause_by_a_run_after_a_runs_own_resume_is_allowed(env, monkeypatch):
    """Only the user's resume is protected: a run may pause again what a run resumed."""
    from ttp import cli
    p = make(env)
    _as_run(monkeypatch, p)
    cli.main(["pause", "demo", "--reason", "first"])
    cli.main(["resume", "demo"])
    cli.main(["pause", "demo", "--reason", "second"])
    assert p.db.kv("paused") is True


def _live_run(p, status, ended):
    p.db.x("INSERT INTO runs(role,provider,status,started,ended) VALUES('worker','claude',?,?,?)",
           (status, ended - 60, ended))


def test_a_project_whose_cli_is_logged_out_or_idle_is_not_live(env, capsys):
    """A hand-back watcher counted a project as live because its daemon process ran, while its CLI had
    been logged out for a day and nothing ran. `ttp alive` says live only after a recent successful model
    run with no provider logged out."""
    from ttp import alerts, cli
    p = make(env)

    def alive():
        capsys.readouterr()
        with pytest.raises(SystemExit) as e:
            cli.main(["alive", "demo"])
        return e.value.code, capsys.readouterr().out

    code, out = alive()
    assert code == 1 and "no successful model run yet" in out
    _live_run(p, "ok", time.time() - 30 * 3600)
    code, out = alive()
    assert code == 1 and "no successful model run in 30.0 h" in out
    _live_run(p, "failed", time.time() - 60)
    assert alive()[0] == 1
    _live_run(p, "ok", time.time() - 600)
    code, out = alive()
    assert code == 0 and "demo live" in out
    p.db.set_kv(alerts.BREAKER + "claude", {"open": True})
    code, out = alive()
    assert code == 1 and "logged out: claude" in out
