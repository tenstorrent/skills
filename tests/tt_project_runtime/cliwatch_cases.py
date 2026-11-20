# The CLI-upgrade watch: a changed agent CLI version gets one capped live row (ttp/live.py, Daemon.watch_cli_versions).
from __future__ import annotations


def _versioned(tmp_path, name="claude"):
    """A fake provider whose `--version` prints the text of a file, and the file."""
    from ttp.providers.fake import Fake
    vfile = tmp_path / f"{name}-version.txt"
    exe = tmp_path / f"fake-{name}"
    exe.write_text(f'#!/bin/sh\ncat "{vfile}"\n')
    exe.chmod(0o755)

    class Versioned(Fake):
        def binary(self):
            return str(exe)

        def login_check(self):
            return True
    Versioned.name = name
    return Versioned(), vfile


def _row_argv(tmp_path, row: dict | None, rc: int = 0):
    """A stand-in for `ttp doctor --live <p> --json`: prints `row` (none when None), exits `rc`, and
    counts its calls in a file."""
    calls = tmp_path / "live-calls"
    body = (f"open({str(calls)!r}, 'a').write('x');"
            + (f"print({json.dumps(json.dumps(row))});" if row is not None else "")
            + f"raise SystemExit({rc})")
    return (lambda provider: [sys.executable, "-c", body]), calls


def _wait_rc(run: Path) -> None:
    deadline = time.time() + 30
    while not (run / "rc").exists() and time.time() < deadline:
        time.sleep(0.05)
    assert (run / "rc").exists(), list(run.iterdir())


def _row(**checks):
    return {"provider": "claude", "version": "2", "date": "2026-01-02", "available": True, "spend_usd": 0.05,
            "checks": {k: {"status": "pass", "note": ""} for k in ("launch", "resume")} | checks}


def test_cli_watch_runs_one_live_row_per_version_change_and_at_most_one_a_day(env, tmp_path):
    from ttp import live
    prov, vfile = _versioned(tmp_path)
    argv, calls = _row_argv(tmp_path, _row())
    home = tmp_path / "watch"
    day = time.mktime((2026, 1, 2, 9, 0, 0, 0, 0, -1))
    vfile.write_text("1.0.0 (Fake)\n")
    assert live.check_versions("p", [prov], home=home, now=day, argv=argv) == [], "the first sight is only recorded"
    seen = json.loads((home / live.WATCH_DIR / live.VERSIONS).read_text())["claude"]
    assert seen["version"] == seen["measured"] == "1.0.0 (Fake)"
    # Same version, after the hourly gap: no run.
    assert live.check_versions("p", [prov], home=home, now=day + 3700, argv=argv) == []
    # A change inside the hour is not read yet; after it, exactly one run.
    vfile.write_text("1.0.1 (Fake)\n")
    assert live.check_versions("p", [prov], home=home, now=day + 3800, argv=argv) == [], "read at most hourly"
    assert live.check_versions("p", [prov], home=home, now=day + 7400, argv=argv) == ["claude"]
    run = home / live.WATCH_DIR / "claude-2026-01-02"
    _wait_rc(run)
    assert calls.read_text() == "x"
    assert json.loads((run / "claim.json").read_text())["version"] == "1.0.1 (Fake)"
    assert live.check_versions("p", [prov], home=home, now=day + 11000, argv=argv) == [], "measured: no rerun"
    # A second change the same day: the day's run is spent, so it waits for the next day.
    vfile.write_text("1.0.2 (Fake)\n")
    assert live.check_versions("p", [prov], home=home, now=day + 14600, argv=argv) == [], "one run a day"
    assert live.check_versions("p2", [prov], home=home, now=day + 18200, argv=argv) == [], \
        "another project's daemon on the host shares the cap"
    nxt = day + 86400
    assert live.check_versions("p", [prov], home=home, now=nxt, argv=argv) == ["claude"], "measured the next day"
    _wait_rc(home / live.WATCH_DIR / "claude-2026-01-03")
    assert calls.read_text() == "xx"
    # Not installed, logged out or unreadable: nothing recorded, nothing run.
    vfile.write_text("1.0.3 (Fake)\n")
    prov.login_check = lambda: False
    assert live.check_versions("p", [prov], home=home, now=nxt + 2 * 86400, argv=argv) == []
    prov.login_check = lambda: True
    vfile.unlink()
    assert live.check_versions("p", [prov], home=home, now=nxt + 2 * 86400, argv=argv) == []
    assert json.loads((home / live.WATCH_DIR / live.VERSIONS).read_text())["claude"]["version"] == "1.0.2 (Fake)"
    assert calls.read_text() == "xx"


def test_cli_watch_reports_a_failing_check_or_a_missing_row_once_to_the_project_that_ran_it(env, tmp_path):
    from ttp import live
    home = tmp_path / "watch"
    cases = {"ok": (_row(), 0, 0),
             "bad": (_row(resume={"status": "fail", "note": "did not recall"}), 1, 1),
             "crash": (None, 2, 1)}
    for i, (name, (row, rc, _)) in enumerate(cases.items()):
        argv, _calls = _row_argv(tmp_path, row, rc)
        assert live.start(name, f"9.{i}", "p", home=home, now=time.time(), argv=argv)
        _wait_rc(next((home / live.WATCH_DIR).glob(f"{name}-*")))
    assert live.harvest("other", home=home) == [], "only the claiming daemon reports"
    got = dict(live.harvest("p", home=home))
    assert set(got) == {"cli-live:bad:9.1", "cli-live:crash:9.2"}
    assert "failed resume: did not recall" in got["cli-live:bad:9.1"] and "launch" not in got["cli-live:bad:9.1"]
    assert "gave no row (exit 2)" in got["cli-live:crash:9.2"]
    assert live.harvest("p", home=home) == [], "each run is reported once"
    assert json.loads((home / live.WATCH_DIR / "bad.last.json").read_text())["checks"]["resume"]["status"] == "fail"
    assert not (home / live.WATCH_DIR / "crash.last.json").exists()
    # A run killed before it wrote its rc (a reboot): reported once it is overdue, not before.
    killed = home / live.WATCH_DIR / "gone-2026-01-02"
    killed.mkdir()
    (killed / "claim.json").write_text(json.dumps({"provider": "gone", "version": "3", "owner": "p", "at": 1000.0}))
    assert live.harvest("p", home=home, now=1000.0 + live.WATCH_TIMEOUT_S) == []
    got = live.harvest("p", home=home, now=1000.0 + live.WATCH_TIMEOUT_S + 601)
    assert [k for k, _ in got] == ["cli-live:gone:3"] and "gave no row (exit -1)" in got[0][1]
    assert live.harvest("p", home=home, now=1e10) == []


def test_daemon_cli_watch_step_raises_one_low_observation_and_never_runs_under_test_unasked(env, tmp_path, monkeypatch):
    import ttp.daemon as dm
    from ttp import live
    p = make(env)
    d = dm.Daemon(p.base)
    prov, vfile = _versioned(tmp_path)
    vfile.write_text("1 (Fake)\n")
    monkeypatch.setattr(dm, "get_provider", lambda name: prov)
    d.watch_cli_versions()
    assert d._cli_watch is None, "no real CLI is read under test unless a test asks"
    monkeypatch.setenv("TTP_TEST_CLI_WATCH", "1")
    d.watch_cli_versions()
    d._cli_watch.join(30)
    assert json.loads((live.watch_dir() / live.VERSIONS).read_text())["claude"]["version"] == "1 (Fake)"
    first = d._cli_watch
    d.watch_cli_versions()
    assert d._cli_watch is first, "at most every few minutes"
    # A finished row with a failing check: one low observation (an issue; low wakes no coordinator), once.
    argv, _calls = _row_argv(tmp_path, _row(launch={"status": "fail", "note": "exit 1"}), 1)
    assert live.start("claude", "2 (Fake)", str(p.base), now=time.time(), argv=argv)
    _wait_rc(next(live.watch_dir().glob("claude-*")))
    d.watch_cli_versions()
    d.watch_cli_versions()
    rows = p.db.q("SELECT severity, title, count FROM issues WHERE source='cli-watch'")
    assert len(rows) == 1 and rows[0]["severity"] == "low" and rows[0]["count"] == 1, rows
    assert "failed launch: exit 1" in rows[0]["title"], rows


def test_live_check_max_usd_caps_the_three_runs_together(env, monkeypatch):
    from ttp import live
    sessions = env["tmp"] / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("TTP_FAKE_SESSIONS", str(sessions))
    monkeypatch.setenv("TTP_FAKE_COST", "0.01")
    monkeypatch.setattr(live, "POLL_S", 0.05)
    r = live.row("fake", base=env["tmp"] / "live", max_usd=0.015)
    assert r["spend_usd"] == 0.01, r
    assert "cap was spent before the resume" in r["checks"]["resume"]["note"]
    assert "cap was spent before the coordinator turn" in r["checks"]["structured"]["note"]
