# Daemon restart tests (ttp/service.py restart: the new daemon is judged only after the old one has exited,
# within its service's stop timeout) of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name.
from __future__ import annotations

_SS_OLD, _SS_NEW = 4242, 4343


def _slow_stop_service(env, monkeypatch, exits_at=120.2, ready_at=5.0, show="TimeoutStopUSec=2min\nMainPID=4242"):
    """A systemd project whose old daemon takes exits_at s (fake clock) to stop after the restart call, as
    when systemd kills it at its 120 s stop timeout. `systemctl --user restart` times out after 60 s, and
    the stopping daemon removed its pid file at once. The new daemon starts as the old one is gone and
    completes its first tick ready_at s later (None: it dies 3 s in, never ready). Its restart after a rollback comes up at once.
    Returns (project, runtime head, clock, the waits_from each start check counted from)."""
    p = make(env)
    from ttp import daemon as dm, service
    monkeypatch.setenv("HOME", str(env["tmp"] / "userhome"))
    monkeypatch.setattr(service.sys, "platform", "linux")
    service._unit(p).parent.mkdir(parents=True, exist_ok=True)
    service._unit(p).write_text(service._unit_text(p))
    h = p.harness
    p.db.set_kv("harness_good", {"commit": _git_out(h, "rev-parse", "HEAD")})
    head = _runtime_change(h)
    (p.state / "daemon.pid").write_text(str(_SS_OLD))
    restarts = []

    class Clock:
        now = time.time()

        def time(self):
            return self.now

        def sleep(self, s):
            self.now += s
            self.tick()

        def tick(self):
            if not restarts:
                return
            t0, since = restarts[0], self.now - restarts[0]
            if len(restarts) == 1 and since >= exits_at and not (p.state / "daemon.start").exists():
                (p.state / "daemon.pid").write_text(str(_SS_NEW))
                (p.state / "daemon.start").write_text(json.dumps({"pid": _SS_NEW, "started": self.now}))
            if len(restarts) == 1 and ready_at is not None and since >= exits_at + ready_at:
                if not (p.state / "heartbeat").exists():
                    (p.state / "heartbeat").write_text(json.dumps({"pid": _SS_NEW, "started": t0 + exits_at}))

    clock = Clock()
    alive = lambda pid: (pid == _SS_OLD and (not restarts or clock.now < restarts[0] + exits_at)  # noqa: E731
                         or pid == _SS_NEW and len(restarts) == 1 and clock.now >= restarts[0] + exits_at
                         and (ready_at is not None or clock.now < restarts[0] + exits_at + 3))
    monkeypatch.setattr(service, "time", clock)
    monkeypatch.setattr(dm, "_alive", alive)
    monkeypatch.setattr(dm, "_is_daemon", alive)

    def run(*a):
        if a[:3] == ("systemctl", "--user", "show"):
            return subprocess.CompletedProcess(a, 0, show + "\n", "")
        if a[:3] == ("systemctl", "--user", "restart"):
            restarts.append(clock.now)
            if len(restarts) == 1:
                (p.state / "daemon.pid").unlink()       # the stopping daemon's pid file is gone at once
                clock.now += 60
                clock.tick()
                raise subprocess.TimeoutExpired(list(a), 60)
            for f in ("daemon.start", "heartbeat"):     # the rollback's restart: up on the old runtime
                (p.state / f).unlink(missing_ok=True)
            (p.state / "heartbeat").write_text(json.dumps({"pid": 4444, "started": clock.now}))
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(service, "_run", run)
    froms = []
    judge = service.wait_for_start

    def wait_for_start(p, since, wait_s, tick_wait_s=None, waits_from=None, **kw):
        froms.append((waits_from or since) - restarts[0])
        return judge(p, since, wait_s, tick_wait_s, waits_from, **kw)

    monkeypatch.setattr(service, "wait_for_start", wait_for_start)
    return p, head, clock, froms


def test_an_old_daemon_slower_to_stop_than_the_start_wait_is_never_rolled_back(env, monkeypatch):
    # 0.2.932's deploy: the old daemon was killed at systemd's 120 s stop timeout, and a 60 s start wait
    # counted from the restart call rolled back a release that never had a chance to start.
    from ttp import service
    p, head, clock, froms = _slow_stop_service(env, monkeypatch, exits_at=120.2, ready_at=8.0)
    res = service.restart(p)
    assert res.outcome == "running", res
    assert froms and froms[0] >= 120.2, f"the new daemon was judged {froms[0]:.0f}s in, before the old one exited"
    _no_rollback(p, head)
    assert not p.db.one("SELECT id FROM messages WHERE kind='alert' AND severity='high'")


def test_a_new_daemon_that_never_becomes_ready_after_a_slow_stop_still_rolls_back_naming_the_phase(
        env, monkeypatch):
    from ttp import service
    p, head, clock, froms = _slow_stop_service(env, monkeypatch, exits_at=120.2, ready_at=None)
    res = service.restart(p)
    assert res.outcome == "rolled_back" and "running again" in res, res
    assert froms[0] >= 120.2, "the new daemon was judged before the old one exited"
    assert "Failed phase: the new daemon was not ready" in res and "took 12" in res, res
    assert _git_out(p.harness, "rev-parse", "HEAD") != head
    alert = p.db.one("SELECT text FROM messages WHERE kind='alert' AND severity='high'")
    assert alert and "Failed phase: the new daemon was not ready" in alert["text"]


def test_an_old_daemon_that_outlives_its_stop_timeout_defers_without_rollback_within_the_bound(env, monkeypatch):
    from ttp import service
    p, head, clock, froms = _slow_stop_service(env, monkeypatch, exits_at=10_000,
                                               show="TimeoutStopUSec=1min 30s\nMainPID=4242")
    t0 = clock.now
    res = service.restart(p)
    assert res.outcome == "deferred" and "old daemon has not exited" in res and "(90s)" in res, res
    assert not froms, "a new daemon was judged while the old one still ran"
    assert 90 <= clock.now - t0 <= 90 + service.STOP_MARGIN_S + 2, "the wait is bounded by the stop timeout"
    _no_rollback(p, head)


def test_the_restart_reads_the_stop_timeout_from_the_service_manager(env, monkeypatch):
    from ttp import service
    span = service._systemd_span_s
    assert (span("2min"), span("1min 30s"), span("500ms"), span("45"), span("1h")) == (120, 90, 0.5, 45, 3600)
    assert span("infinity") == float("inf") and span("") is None and span("soon") is None
    p, head, clock, froms = _slow_stop_service(env, monkeypatch, show="TimeoutStopUSec=infinity\nMainPID=77")
    assert service._manager_view(p) == (service.STOP_WAIT_CAP_S, 77)
    p, head, clock, froms = _slow_stop_service(env, monkeypatch, show="no such property")
    assert service._manager_view(p) == (service.STOP_TIMEOUT_S, None)
    # launchd: the agent's ExitTimeOut, else launchd's own default.
    import plistlib
    monkeypatch.setattr(service.sys, "platform", "darwin")
    service._agent(p).parent.mkdir(parents=True, exist_ok=True)
    service._agent(p).write_bytes(plistlib.dumps({"Label": "x"}))
    assert service._manager_view(p) == (service.LAUNCHD_EXIT_TIMEOUT_S, None)
    service._agent(p).write_bytes(plistlib.dumps({"Label": "x", "ExitTimeOut": 45}))
    assert service._manager_view(p) == (45, None)
    # launchd reads ExitTimeOut 0 as no timeout: the wait is the cap, not launchd's default.
    service._agent(p).write_bytes(plistlib.dumps({"Label": "x", "ExitTimeOut": 0}))
    assert service._manager_view(p) == (service.STOP_WAIT_CAP_S, None)


def _ss_deferred(env, monkeypatch, ready_at):
    """A restart deferred because the old daemon (90 s stop timeout) exits only 200 s in."""
    from ttp import service
    p, head, clock, froms = _slow_stop_service(env, monkeypatch, exits_at=200, ready_at=ready_at,
                                               show="TimeoutStopUSec=1min 30s\nMainPID=4242")
    assert service.restart(p).outcome == "deferred"
    assert service.check_deferred(p) == "pending", "judged while the old daemon still ran"
    clock.sleep(120)                           # the old daemon exits; the new one starts on the new runtime
    return p, head, clock


def test_a_deferred_restart_whose_new_daemon_dies_is_rolled_back_later_once(env, monkeypatch):
    from ttp import service
    p, head, clock = _ss_deferred(env, monkeypatch, ready_at=None)
    # A newer restart's record written between the read and the claim is put back, not judged.
    path, replace = p.state / service.RESTART_DEFERRED, service.os.replace

    def newer_first(src, dst):
        if Path(dst).name != f"{service.RESTART_DEFERRED}.{os.getpid()}":   # only the claim
            return replace(src, dst)
        rec = json.loads(path.read_text())
        path.write_text(json.dumps({**rec, "at": rec["at"] + 1}))
        monkeypatch.setattr(service.os, "replace", replace)
        replace(src, dst)
    monkeypatch.setattr(service.os, "replace", newer_first)
    assert service.check_deferred(p) == "pending" and _git_out(p.harness, "rev-parse", "HEAD") == head
    assert path.exists() and not p.db.one("SELECT id FROM messages WHERE kind='alert'")
    assert service.check_deferred(p) == "rolled_back"
    assert _git_out(p.harness, "rev-parse", "HEAD") != head
    alerts = p.db.q("SELECT text FROM messages WHERE kind='alert' AND severity='high'")
    assert len(alerts) == 1 and "deferred" in alerts[0]["text"] and "watchdog" in alerts[0]["text"], alerts
    assert "running again" in alerts[0]["text"]
    assert service.check_deferred(p) is None and service.check_deferred(p, "new daemon") is None
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert' AND severity='high'")) == 1


def test_a_deferred_restart_whose_new_daemon_runs_is_left_alone(env, monkeypatch):
    from ttp import service
    p, head, clock = _ss_deferred(env, monkeypatch, ready_at=8.0)
    assert service.check_deferred(p, "new daemon") == "running"
    assert not (p.state / service.RESTART_DEFERRED).exists()
    _no_rollback(p, head)


def test_a_deferred_restart_is_never_rolled_back_again_once_the_harness_moved_on(env, monkeypatch):
    from ttp import service
    # The updater already rolled back (or deployed again): the harness moved past the deferred commit.
    p, head, clock = _ss_deferred(env, monkeypatch, ready_at=None)
    _runtime_change(p.harness)
    assert service.check_deferred(p) == "superseded"
    assert not p.db.one("SELECT id FROM messages WHERE kind='alert' AND severity='high'")
    # A later restart judges the daemon itself: it drops the deferred record.
    service._defer(p, clock.now, "restarted", set(), 90, 60, None)
    service.restart(p, restart_fn=lambda p: "restarted")
    assert not (p.state / service.RESTART_DEFERRED).exists()


def test_a_deferred_restart_survives_a_commit_outside_the_runtime(env, monkeypatch):
    from ttp import service
    # A memory or charter commit after the deploy leaves the runtime it was to start: still judged.
    p, head, clock = _ss_deferred(env, monkeypatch, ready_at=None)
    (p.harness / "MEMORY.md").write_text("a memory entry\n")
    _git_out(p.harness, "add", "MEMORY.md")
    _git_out(p.harness, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "memory: an entry")
    assert service.check_deferred(p) == "rolled_back"
    assert p.db.one("SELECT id FROM messages WHERE kind='alert' AND severity='high'")


def _ss_slow_rollback(env, monkeypatch, budget_s):
    """A deferred restart whose new daemon died, judged by a watchdog run killed budget_s from now: the
    rollback's restart starts no new daemon, so its judge would wait out a 600 s start window."""
    from ttp import service
    p, head, clock = _ss_deferred(env, monkeypatch, ready_at=None)
    path = p.state / service.RESTART_DEFERRED
    path.write_text(json.dumps({**json.loads(path.read_text()), "wait_s": 600}))
    clock.sleep(5)                             # the dead new daemon started before this run
    killed_at, sleep = clock.now + budget_s, clock.sleep

    def sleep_until_killed(s):
        if clock.now + s > killed_at:
            raise SystemExit("the watchdog's unit timed out")
        sleep(s)
    clock.sleep = sleep_until_killed
    res = service.check_deferred(p, restart_fn=lambda p: "restarted", budget_s=budget_s)
    return p, head, clock, res, killed_at


def test_a_watchdog_rollback_posts_its_alert_before_the_unit_timeout_kills_it(env, monkeypatch):
    from ttp import service
    p, head, clock, res, killed_at = _ss_slow_rollback(env, monkeypatch, budget_s=service.WATCHDOG_RUN_S - 40)
    assert res == "rolled_back" and _git_out(p.harness, "rev-parse", "HEAD") != head
    alert = p.db.one("SELECT text FROM messages WHERE kind='alert' AND severity='high'")
    assert alert and "not confirmed" in alert["text"] and "Rolled back:" in alert["text"], alert
    assert clock.now <= killed_at - service.JUDGE_MARGIN_S + 2


def test_a_watchdog_run_with_too_little_time_left_leaves_the_rollback_to_a_later_run(env, monkeypatch):
    from ttp import service
    budget = service.JUDGE_MARGIN_S + service.ROLLBACK_MIN_S - 1
    p, head, clock, res, killed_at = _ss_slow_rollback(env, monkeypatch, budget_s=budget)
    assert res == "pending" and (p.state / service.RESTART_DEFERRED).exists()
    _no_rollback(p, head)


def test_the_watchdog_run_judges_a_deferred_restart(env, monkeypatch):
    from ttp import service, watchdog
    p, head, clock = _ss_deferred(env, monkeypatch, ready_at=None)
    monkeypatch.setattr(watchdog, "check", lambda p: "ok")
    monkeypatch.setattr(watchdog.sys, "argv", ["ttp.watchdog", str(p.base)])
    assert watchdog.main() == 0
    assert not (p.state / service.RESTART_DEFERRED).exists()
    assert _git_out(p.harness, "rev-parse", "HEAD") != head
    alert = p.db.one("SELECT text FROM messages WHERE kind='alert' AND severity='high'")
    assert alert and "the watchdog checked" in alert["text"] and "running again" in alert["text"], alert


def test_a_new_daemons_first_tick_ends_a_deferred_restart(env):
    p = make(env)
    from ttp import service
    from ttp.daemon import Daemon
    service._defer(p, time.time() - 5, "restarted", set(), 90, 60, None)
    assert json.loads((p.state / service.RESTART_DEFERRED).read_text())["runtime"]
    Daemon(p.base)._beat()
    assert not (p.state / service.RESTART_DEFERRED).exists(), "the new daemon's tick left the restart unjudged"
