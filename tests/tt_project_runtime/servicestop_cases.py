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

    def wait_for_start(p, since, wait_s, tick_wait_s=None, waits_from=None):
        froms.append((waits_from or since) - restarts[0])
        return judge(p, since, wait_s, tick_wait_s, waits_from)

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
