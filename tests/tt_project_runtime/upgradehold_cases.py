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


def _gone_pid():
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def test_a_push_lock_whose_holder_process_is_gone_does_not_hold_the_upgrade(env, monkeypatch):
    """A process that inherited a push's lock file keeps it taken after the push died: that holds nothing."""
    from ttp import release
    p, d, launches = _release_daemon(env, monkeypatch)
    f = _push_lock(p, "push:run-x", pid=_gone_pid())
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
    finally:
        f.close()


def test_the_push_hold_bound_is_configurable():
    from ttp import release
    assert release.push_hold_s({}) == release.PUSH_HOLD_MIN * 60
    assert release.push_hold_s({"upgrade": {"push_hold_min": 5}}) == 300
    for bad in (0, -1, "x", None):
        assert release.push_hold_s({"upgrade": {"push_hold_min": bad}}) == release.PUSH_HOLD_MIN * 60
