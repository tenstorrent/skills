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
