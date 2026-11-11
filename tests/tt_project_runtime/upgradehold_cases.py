# Template upgrade hold-and-retry tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file. Each test uses
# a temporary project and a temporary installed release (_release_daemon), never a real harness.
from __future__ import annotations


def _start_merge(h):
    """A merge left in the live harness (MERGE_HEAD set), as a harness task's own merge leaves it."""
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    _git_out(h, "checkout", "-qb", "side")
    (h / "prompts" / "worker.md").write_text("side change\n")
    _git_out(h, *ident, "commit", "-qam", "side")
    _git_out(h, "checkout", "-q", "-")
    _git_out(h, *ident, "merge", "-q", "--no-commit", "--no-ff", "side")
    return _git_out(h, "rev-parse", "MERGE_HEAD")


def _stuck_alerts(p):
    return p.db.q("SELECT text FROM messages WHERE kind='alert' AND text LIKE '%unfinished merge%'")


def test_an_auto_upgrade_held_by_a_harness_merge_retries_once_the_merge_ends(env, monkeypatch):
    """A harness merge left MERGE_HEAD for minutes: --auto recorded 'failed' and the release was never tried
    again. Now it is held with the sha and when it was first seen, and the daemon starts it once it ends."""
    from ttp import release
    p, d, launches = _release_daemon(env, monkeypatch)
    sha = _start_merge(p.harness)
    d.check_release()
    rec = p.db.kv(release.KV_AUTO)
    assert launches == [p.base] and rec["outcome"] == "held" and rec["merge_head"] == sha and rec["since"]
    assert _harness_commit(p) == "aaaa1111"
    drift = release.drift(p)
    assert release.hold_reason(p, drift).endswith(release.RECHECK)
    assert d._release_due - time.time() < 600, "rechecked soon, not in an hour"
    d._release_due = 0
    d.check_release()
    assert launches == [p.base], "never started while the merge is still there"
    _git_out(p.harness, "merge", "--abort")
    assert release.hold_reason(p, drift) == ""
    d._release_due = 0
    d.check_release()
    assert launches == [p.base, p.base] and _harness_commit(p) == "bbbb2222"
    assert p.db.kv(release.KV_AUTO)["outcome"] == "applied" and not _stuck_alerts(p)


def test_the_same_merge_past_stale_s_fails_the_upgrade_with_exactly_one_alert(env, monkeypatch):
    from ttp import release
    p, d, launches = _release_daemon(env, monkeypatch)
    sha = _start_merge(p.harness)
    d.check_release()
    rec = p.db.kv(release.KV_AUTO)
    p.db.set_kv(release.KV_AUTO, {**rec, "since": time.time() - release.STALE_S + 60})
    d._release_due = 0
    d.check_release()
    assert p.db.kv(release.KV_AUTO)["outcome"] == "held" and not _stuck_alerts(p), "not stale yet"
    p.db.set_kv(release.KV_AUTO, {**p.db.kv(release.KV_AUTO), "since": time.time() - release.STALE_S - 1})
    for _ in range(3):
        d._release_due = 0
        d.check_release()
    rec = p.db.kv(release.KV_AUTO)
    assert rec["outcome"] == "failed" and sha[:12] in rec["why"] and "merge --abort" in rec["why"]
    assert len(_stuck_alerts(p)) == 1 and launches == [p.base]
    assert release.hold_reason(p, release.drift(p)) == "already tried for this release"


def test_a_new_merge_sha_restarts_the_stale_clock(env, monkeypatch):
    from ttp import release
    p, d, launches = _release_daemon(env, monkeypatch)
    _start_merge(p.harness)
    d.check_release()
    p.db.set_kv(release.KV_AUTO, {**p.db.kv(release.KV_AUTO), "merge_head": "0" * 40,
                                  "since": time.time() - release.STALE_S - 1})
    d._release_due = 0
    d.check_release()
    rec = p.db.kv(release.KV_AUTO)
    assert rec["outcome"] == "held" and rec["merge_head"] != "0" * 40 and time.time() - rec["since"] < 60
    assert not _stuck_alerts(p) and launches == [p.base]


def test_a_refusal_by_hand_after_a_failed_try_leaves_a_hold_the_daemon_retries_soon(env, monkeypatch, capsys):
    """A deploy or push batch runs `ttp upgrade` by hand and meets the merge: the project's own daemon
    retries soon once it ends, with no worker task."""
    from ttp import cli, release
    p, d, launches = _release_daemon(env, monkeypatch)
    drift = release.drift(p)
    p.db.set_kv(release.KV_AUTO, {"key": drift["key"], "outcome": "failed", "why": "an earlier try"})
    _start_merge(p.harness)
    d.check_release()
    assert not launches and d._release_due - time.time() > 600, "already tried: next look in an hour"
    with pytest.raises(SystemExit) as e:
        cli.main(["upgrade", p.name])
    assert e.value.code == 1 and "retries on its own" in capsys.readouterr().err
    rec = p.db.kv(release.KV_AUTO)
    assert rec["outcome"] == "held" and rec["key"] == drift["key"] and rec["merge_head"]
    d.check_release()
    assert not launches and d._release_due - time.time() < 600, "the recorded hold is looked at soon"
    _git_out(p.harness, "merge", "--abort")
    d._release_due = 0
    d.check_release()
    assert launches == [p.base] and _harness_commit(p) == "bbbb2222"


def test_a_setup_journal_holds_the_automatic_upgrade_until_it_is_gone(env, monkeypatch):
    from ttp import cli, release
    p, d, launches = _release_daemon(env, monkeypatch)
    drift = release.drift(p)
    jr = env["home"] / "lib" / cli.SETUP_JOURNAL
    jr.write_text("{}")
    why = release.hold_reason(p, drift)
    assert "ttp setup" in why and why.endswith(release.RECHECK)
    d.check_release()
    assert not launches and d._release_due - time.time() < 600
    jr.unlink()
    assert release.hold_reason(p, drift) == ""


@pytest.mark.parametrize("err", [shutil.Error, PermissionError])
def test_a_copy_error_is_held_and_retried_then_failed_at_the_third(env, monkeypatch, err):
    from ttp import cli, release
    p, d, launches = _release_daemon(env, monkeypatch)

    def broken(*a, **k):
        raise err("copy cut short")
    monkeypatch.setattr(cli, "_snapshot_upstream", broken)
    for n in (1, 2):
        d._release_due = 0
        d.check_release()
        rec = p.db.kv(release.KV_AUTO)
        assert len(launches) == n and rec["outcome"] == "held" and rec["copy_tries"] == n
        assert "copy cut short" in rec["why"] and d._release_due - time.time() < 600
        d._release_due = 0
        d.check_release()
        assert len(launches) == n, "not before HELD_RECHECK_S"
        p.db.set_kv(release.KV_AUTO, {**rec, "until": time.time() - 1})
    d._release_due = 0
    d.check_release()
    rec = p.db.kv(release.KV_AUTO)
    assert len(launches) == 3 and rec["outcome"] == "failed" and rec["copy_tries"] == 3
    d._release_due = 0
    d.check_release()
    assert len(launches) == 3 and _harness_commit(p) == "aaaa1111"


def test_a_copy_error_then_a_clean_copy_applies(env, monkeypatch):
    from ttp import cli, release
    p, d, launches = _release_daemon(env, monkeypatch)
    real, calls = cli._snapshot_upstream, []

    def once(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("no space left")
        return real(*a, **k)
    monkeypatch.setattr(cli, "_snapshot_upstream", once)
    d.check_release()
    assert p.db.kv(release.KV_AUTO)["outcome"] == "held"
    p.db.set_kv(release.KV_AUTO, {**p.db.kv(release.KV_AUTO), "until": 0})
    d._release_due = 0
    d.check_release()
    assert len(launches) == 2 and p.db.kv(release.KV_AUTO)["outcome"] == "applied"


def test_a_refusal_that_cannot_record_its_hold_still_refuses_and_promises_no_retry(env, monkeypatch, capsys):
    """A read-only state (a worker sandbox) cannot record the hold: the refusal stands as before."""
    from ttp import cli, release
    p, d, launches = _release_daemon(env, monkeypatch)
    _start_merge(p.harness)

    def read_only(*a, **k):
        raise sqlite3.OperationalError("attempt to write a readonly database")
    monkeypatch.setattr(release, "hold_merge", read_only)
    with pytest.raises(SystemExit) as e:
        cli.main(["upgrade", p.name])
    err = capsys.readouterr().err
    assert e.value.code == 1 and "unfinished merge" in err and "could not record the hold" in err
    assert "retries on its own" not in err and p.db.kv(release.KV_AUTO) is None


def _push_lock(p, name="push:origin%2Fmain", age_s=0.0, pid=None):
    """A push lock held by this process, as `ttp push` holds its branch's turn, taken `age_s` ago; `pid`
    names another holder process in its label."""
    from ttp import locks
    f = locks.try_take(locks.slot_paths(p.state / "locks", name, 1), "task #7 (run 9)", "ttp push")
    locks.relabel(f, {"since": time.time() - age_s, **({"pid": pid} if pid else {})})
    return f


def _exited_pid():
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def test_a_push_lock_whose_holder_process_is_gone_does_not_hold_the_upgrade(env, monkeypatch):
    """A process that inherited a push's lock file keeps it taken after the push died: that holds nothing."""
    from ttp import release
    p, d, launches = _release_daemon(env, monkeypatch)
    f = _push_lock(p, "push:run-x", pid=_exited_pid())
    try:
        assert release.live_pushes(p) == [] and release.hold_reason(p, release.drift(p)) == ""
        d.check_release()
        assert launches == [p.base] and _harness_commit(p) == "bbbb2222"
        assert p.db.kv(release.KV_AUTO)["outcome"] == "applied"
    finally:
        f.close()


def test_a_live_push_inside_the_bound_holds_the_upgrade_and_status_shows_since_when(env, monkeypatch):
    from ttp import release, timefmt
    p, d, launches = _release_daemon(env, monkeypatch)
    f = _push_lock(p, age_s=60)
    try:
        d.check_release()
        assert not launches and d._release_due - time.time() <= release.HELD_RECHECK_S
        rec = p.db.kv(release.KV_PUSH_HOLD)
        assert rec["why"] == "a push is in flight" and rec["key"] == release.drift(p)["key"]
        line = release.line(p, p.db, p.config())
        assert f"upgrade held since {timefmt.long(rec['since'], p)}: a push is in flight" in line, line
        d._release_due = 0
        d.check_release()
        assert p.db.kv(release.KV_PUSH_HOLD)["since"] == rec["since"], "since stays while it holds"
    finally:
        f.close()
    d._release_due = 0
    d.check_release()
    assert launches == [p.base] and p.db.kv(release.KV_PUSH_HOLD) is None
    assert "held" not in release.line(p, p.db, p.config())


def test_past_the_bound_only_a_push_step_holds_and_the_upgrade_applies_between_steps(env, monkeypatch, capsys):
    from ttp import cli, push, release
    p, d, launches = _release_daemon(env, monkeypatch)
    old = release.PUSH_HOLD_MIN * 60 + 60
    run = _push_lock(p, "push:run-y", age_s=old)     # its turn came long ago: a step is running
    f = _push_lock(p, age_s=old)
    steps = push.Steps(f)
    try:
        steps.go("checks")
        why = release.hold_reason(p, release.drift(p))
        assert why.startswith("a push is in its checks step since") and why.endswith(release.RECHECK)
        d.check_release()
        assert not launches and d._release_due - time.time() <= release.PUSH_STEP_RECHECK_S
        assert "a push is in its checks step" in release.line(p, p.db, p.config())
        p.db.set_kv(release.KV_AUTO, {"key": release.drift(p)["key"], "outcome": "running"})
        with pytest.raises(SystemExit) as e:          # the swap's own check: never inside a step
            cli.main(["upgrade", p.name, "--auto", "--project-dir", str(p.base)])
        assert e.value.code == 75 and "checks step" in capsys.readouterr().out
        p.db.set_kv(release.KV_AUTO, {**p.db.kv(release.KV_AUTO), "outcome": "held"})
        steps.between()            # e.g. waiting for a checks slot
        d._release_due = 0
        d.check_release()
        assert launches == [p.base] and _harness_commit(p) == "bbbb2222"
        assert p.db.kv(release.KV_AUTO)["outcome"] == "applied"
    finally:
        f.close()
        run.close()


def test_a_push_step_waits_for_a_running_upgrade_and_marks_itself_first(env):
    """The upgrade takes its lock, then reads the marks; a step marks itself, then reads the lock."""
    from ttp import locks, push, release
    p = make(env)
    f = _push_lock(p)
    up = locks.try_take([release.upgrade_lock(p)], "ttp upgrade", "ttp upgrade")
    said, t = [], None
    try:
        steps = push.Steps(f, said.append, poll_s=0.01)
        t = threading.Thread(target=steps.go, args=("push",))
        t.start()
        deadline = time.time() + 10
        while not said and time.time() < deadline:
            time.sleep(0.01)
        assert said and "the push step waits" in said[0] and t.is_alive()
        assert not locks.read_label(pathlib.Path(f.name)).get("step"), "unmarked while it waits"
    finally:
        up.close()
        if t:
            t.join(10)
    try:
        assert not t.is_alive() and locks.read_label(pathlib.Path(f.name))["step"] == "push"
        assert re.fullmatch(r"the push step waited \d+ s for the upgrade", said[-1]), said
    finally:
        f.close()


def test_the_push_hold_bound_is_configurable():
    from ttp import release
    assert release.push_hold_s({}) == release.PUSH_HOLD_MIN * 60
    assert release.push_hold_s({"upgrade": {"push_hold_min": 5}}) == 300
    for bad in (0, -1, "x", None):
        assert release.push_hold_s({"upgrade": {"push_hold_min": bad}}) == release.PUSH_HOLD_MIN * 60


def test_a_push_queue_batch_past_the_bound_holds_the_upgrade_only_inside_its_steps(env, monkeypatch):
    """The push queue's batch takes the branch lock itself (Batch.push_phase), not through push.push: its
    rebase, checks and push are marked too, and only its wait for a checks slot lets an upgrade go."""
    from ttp import batch, push, pushq, release
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(pushq, "batch_argv", lambda marker: [sys.executable, "-c", "pass"])
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    marker = pathlib.Path(s.p.db.one("SELECT marker FROM push_batches WHERE id=?", (bid,))["marker"])
    past = {"upgrade": {"push_hold_min": 1e-6}}      # every live push is past the bound
    seen = {}

    def sample(where):
        seen.setdefault(where, release.push_hold(s.p, cfg=past))

    for cls_or_mod, name, where in ((batch.Batch, "_prepare", "rebase"), (push, "check_slot", "slot wait"),
                                    (batch.Batch, "_run_check", "checks"), (batch.Batch, "_mark_pushing", "push")):
        real = getattr(cls_or_mod, name)

        def wrapped(*a, _real=real, _where=where, **kw):
            sample(_where)
            return _real(*a, **kw)
        monkeypatch.setattr(cls_or_mod, name, wrapped)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("TTP_RUN_DIR", push._read(marker)["check_report_dir"])
    assert batch.run_batch(s.p, marker) == 0
    assert push._read(marker)["outcome"] == "pushed"
    assert set(seen) == {"rebase", "slot wait", "checks", "push"}, seen
    for step in ("rebase", "checks", "push"):
        assert seen[step].startswith(f"{release.PUSH_HELD}in its {step} step since"), seen
    assert seen["slot wait"] == "", "waiting for a checks slot is between steps"


def _rolled_back_release(env, monkeypatch):
    """A project that merged a release (a new module, a new name in an old one), whose new daemon then did
    not start, so service._roll_back put runtime/ back to the last good commit as a commit of its own."""
    from ttp import cli, service
    p = make(env)
    h = p.harness
    p.db.set_kv("harness_good", {"commit": _git_out(h, "rev-parse", "HEAD")})
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    _install_template(env)
    lib = env["home"] / "lib" / "current" / "runtime" / "ttp"
    (lib / "SOURCE_COMMIT").write_text("bbbb2222\n")
    (lib / "relonly.py").write_text("RELEASE_ONLY = 1\n")
    (lib / "hostload.py").write_text((lib / "hostload.py").read_text() + "\n\ndef release_only():\n    return 1\n")
    cli.main(["upgrade", "demo"])
    assert (h / "runtime" / "ttp" / "relonly.py").exists()
    monkeypatch.setattr(service, "_judge_start", lambda *a, **k: ("running", 0.0, set()))
    res = service._roll_back(p, "restarted", 61, 1, 20, 60, None, lambda p: "restarted")
    assert res.outcome == "rolled_back", res
    assert not (h / "runtime" / "ttp" / "relonly.py").exists()
    return p, cli


def test_a_rerun_upgrade_re_applies_the_release_its_restart_rolled_back(env, monkeypatch, capsys):
    """A rerun after a rollback said "harness up to date" and kept the rolled-back modules as files main
    changed, so the project could never get the release back without a hand edit."""
    p, cli = _rolled_back_release(env, monkeypatch)
    capsys.readouterr()
    cli.main(["upgrade", "demo"])
    out = capsys.readouterr().out
    rt = p.harness / "runtime" / "ttp"
    assert "re-applied the runtime a restart rolled back" in out, out
    assert "kept runtime files main changed" not in out and "restored runtime files" not in out, out
    assert (rt / "relonly.py").exists() and "def release_only" in (rt / "hostload.py").read_text()
    assert (rt / "SOURCE_COMMIT").read_text().strip() == "bbbb2222"
    assert _git_out(p.harness, "status", "--porcelain", "--", "runtime") == ""
    capsys.readouterr()
    cli.main(["upgrade", "demo"])         # once re-applied, the next run has nothing to undo
    assert "re-applied" not in capsys.readouterr().out


def test_a_rollback_of_a_local_runtime_edit_is_kept_by_the_upgrade(env, monkeypatch, capsys):
    from ttp import cli, service
    p = make(env)
    h = p.harness
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    _install_template(env)
    cli.main(["upgrade", "demo"])
    p.db.set_kv("harness_good", {"commit": _git_out(h, "rev-parse", "HEAD")})
    _runtime_change(h)                     # a harness task's own edit, not a template merge
    monkeypatch.setattr(service, "_judge_start", lambda *a, **k: ("running", 0.0, set()))
    assert service._roll_back(p, "restarted", 61, 1, 20, 60, None, lambda p: "restarted").outcome == "rolled_back"
    rolled = _git_out(h, "rev-parse", "HEAD")
    capsys.readouterr()
    cli.main(["upgrade", "demo"])
    assert "re-applied" not in capsys.readouterr().out
    assert "# a runtime change" not in (h / "runtime" / "ttp" / "daemon.py").read_text()
    assert _git_out(h, "log", "-1", "--format=%H", "--", "runtime") == rolled


def _stale_finish_task(env, monkeypatch, conflict=False):
    """A finish task queued by a conflicting upgrade, then idle 7 h; the next release merges cleanly unless
    `conflict`. Its takeover runs `ttp upgrade <name> --take-over-from <task>` in this process."""
    from ttp import cli, release
    p, restarts = _conflicting_upgrade(env, monkeypatch)
    with pytest.raises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
        cli.main(["upgrade", "demo"])
    tid = p.db.one("SELECT id FROM tasks WHERE kind='harness'")["id"]
    if not conflict:
        _install_template(env, {"kind-harness.md": "# Harness task, this project's way\n"})
    p.db.x("UPDATE tasks SET status='queued', created=?, updated=? WHERE id=?",
           (time.time() - 9 * 3600, time.time() - 7 * 3600, tid))
    calls = []

    class Ended:
        def __init__(self, rc):
            self.pid, self.rc = 2 ** 22 + 7, rc

        def poll(self):
            return self.rc

    def launch(proj, task):
        calls.append(task)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["upgrade", proj.name, "--take-over-from", str(task)])
            return Ended(0)
        except SystemExit as e:
            return Ended(e.code)
    monkeypatch.setattr(release, "_launch_takeover", launch)
    monkeypatch.setattr(release, "_takeovers", {})
    return p, tid, calls, restarts


def _stuck_notes(tid):
    from ttp import upstream
    return [n for n in (json.loads(x) for x in upstream.path().read_text().splitlines())
            if n["task"] == tid] if upstream.path().exists() else []


def test_the_daemon_takes_over_a_finish_task_idle_past_six_hours_once(env, monkeypatch):
    """A finish task idle past STALE_S only got an upstream note and waited for someone to run a host-side
    `ttp upgrade`. The daemon's release check now runs that takeover itself, once: the task is cancelled as
    superseded, the release merged, and no note is sent."""
    from ttp import release
    p, tid, calls, restarts = _stale_finish_task(env, monkeypatch)
    assert "this project's daemon takes it over" in release.holder_line(p, release.finish_holder(p))
    done = release.escalate_stuck(p)
    assert done.startswith("took over: harness task #") and calls == [tid], done
    t = p.db.task(tid)
    assert t["status"] == "cancelled" and release.fenced(p, tid) and release.finish_holder(p) is None
    assert restarts == [1] and not p.db.q("SELECT id FROM tasks WHERE kind='harness' AND id<>?", (tid,))
    assert release.escalate_stuck(p) == "" and calls == [tid] and not _stuck_notes(tid)
    assert p.db.kv(release.KV_FINISH_STUCK) is None


def test_the_daemons_takeover_of_a_conflicting_merge_queues_one_new_merge_task(env, monkeypatch):
    from ttp import release
    p, tid, calls, _ = _stale_finish_task(env, monkeypatch, conflict=True)
    assert release.escalate_stuck(p).startswith("took over")
    new = p.db.q("SELECT id FROM tasks WHERE kind='harness' AND id<>?", (tid,))
    assert p.db.task(tid)["status"] == "cancelled" and len(new) == 1
    assert release.finish_holder(p)["task"] == new[0]["id"]
    for _ in range(2):          # the new task is fresh: no second takeover, no note
        assert release.escalate_stuck(p) == ""
    assert calls == [tid] and not _stuck_notes(tid) and not _stuck_notes(new[0]["id"])


@pytest.mark.parametrize("why", ["paused", "push"])
def test_the_daemons_takeover_waits_while_paused_or_a_push_is_in_flight(env, monkeypatch, why):
    from ttp import locks, release
    p, tid, calls, _ = _stale_finish_task(env, monkeypatch)
    if why == "paused":
        p.db.set_paused(True)
        assert "takes it over" not in release.holder_line(p, release.finish_holder(p))
    else:
        held = locks.try_take([p.state / "locks" / "push:run-b1.0.lock"], "push queue")
        assert held is not None
    for _ in range(2):
        assert release.escalate_stuck(p) == ""
    assert not calls and p.db.task(tid)["status"] == "queued" and not _stuck_notes(tid)
    if why == "paused":
        p.db.set_paused(False)
        assert release.escalate_stuck(p) == "" and not calls     # its clock starts again at the resume
    else:
        held.close()
        assert release.escalate_stuck(p).startswith("took over") and calls == [tid]


def test_a_failed_takeover_sends_one_note_and_a_restart_does_not_run_it_again(env, monkeypatch):
    """A takeover that ended with the same task still holding the harness sends ONE upstream note with its
    error. The takeover lives in kv: a restart while it runs waits for its process and never starts a
    second one; once it ended unseen, the restarted daemon reports it once."""
    from ttp import release
    from ttp.daemon import Daemon
    p, tid, calls, _ = _stale_finish_task(env, monkeypatch)

    class Running:
        pid = os.getpid()

        def poll(self):
            return None

    def launch(proj, task):
        calls.append(task)
        return Running()
    monkeypatch.setattr(release, "_launch_takeover", launch)
    p.set_config("upgrade.auto", False)
    d = Daemon(p.base)
    d.check_release()
    assert calls == [tid] and p.db.kv(release.KV_FINISH_STUCK)["took"]["pid"] == os.getpid()
    assert "is taking it over now" in release.holder_line(p, release.finish_holder(p))
    assert f"template upgrade took over: harness task #{tid}" in (p.logs / "daemon.log").read_text()
    release._takeovers.clear()                          # a restart: the process is someone else's now
    d2 = Daemon(p.base)
    d2._release_due = 0
    d2.check_release()
    assert calls == [tid] and not _stuck_notes(tid)     # still running: it waits
    rec = p.db.kv(release.KV_FINISH_STUCK)
    p.db.set_kv(release.KV_FINISH_STUCK, {**rec, "took": {**rec["took"], "pid": _exited_pid()}})
    (p.logs / "takeover.log").parent.mkdir(parents=True, exist_ok=True)
    (p.logs / "takeover.log").write_text("upgrade refused: Failed to connect to bus\n")
    for _ in range(3):
        d3 = Daemon(p.base)
        d3._release_due = 0
        d3.check_release()
    got = _stuck_notes(tid)
    assert calls == [tid] and len(got) == 1 and p.db.task(tid)["status"] == "queued"
    assert "takeover" in got[0]["spec"] and "ended while this daemon was down" in got[0]["spec"]
    assert "the maintainers were told" in release.holder_line(p, release.finish_holder(p))


def test_a_takeover_that_cannot_start_sends_one_note_with_its_error(env, monkeypatch):
    from ttp import release
    p, tid, calls, _ = _stale_finish_task(env, monkeypatch)

    def launch(proj, task):
        calls.append(task)
        raise FileNotFoundError("no installed release at lib/current/bin/ttp")
    monkeypatch.setattr(release, "_launch_takeover", launch)
    assert "failed" in release.escalate_stuck(p)
    assert release.escalate_stuck(p) == "" and calls == [tid]
    got = _stuck_notes(tid)
    assert len(got) == 1 and "no installed release" in got[0]["spec"] and "--apply` is refused" in got[0]["spec"]
