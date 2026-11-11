# Daemon restart tests (ttp/service.py restart: the new daemon is judged only after the old one has exited,
# within its service's stop timeout) of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name.
from __future__ import annotations

_SS_OLD, _SS_NEW = 4242, 4343


def _slow_stop_service(env, monkeypatch, exits_at=120.2, ready_at=5.0, show="TimeoutStopUSec=2min\nMainPID=4242",
                       starts_at=0.0, load=0.0):
    """A systemd project whose old daemon takes exits_at s (fake clock) to stop after the restart call, as
    when systemd kills it at its 120 s stop timeout. `systemctl --user restart` times out after 60 s, and
    the stopping daemon removed its pid file at once. The new daemon starts as the old one is gone and
    completes its first tick ready_at s later (None: it dies 3 s in, never ready). Its restart after a rollback comes up at once.
    `starts_at`: the seconds after the old daemon's exit before the new one writes its start marker (a slow
    import); `load`: the host's 1-minute load per core while it starts.
    Returns (project, runtime head, clock, the waits_from each start check counted from)."""
    p = make(env)
    from ttp import daemon as dm, service
    monkeypatch.setenv("HOME", str(env["tmp"] / "userhome"))
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "load_per_core", lambda: load)
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
            if len(restarts) == 1 and since >= exits_at + starts_at and not (p.state / "daemon.start").exists():
                (p.state / "daemon.pid").write_text(str(_SS_NEW))
                (p.state / "daemon.start").write_text(json.dumps({"pid": _SS_NEW, "started": self.now}))
            if len(restarts) == 1 and ready_at is not None and since >= exits_at + starts_at + ready_at:
                if not (p.state / "heartbeat").exists():
                    (p.state / "heartbeat").write_text(json.dumps({"pid": _SS_NEW, "started": t0 + exits_at + starts_at}))

    clock = Clock()
    alive = lambda pid: (pid == _SS_OLD and (not restarts or clock.now < restarts[0] + exits_at)  # noqa: E731
                         or pid == _SS_NEW and len(restarts) == 1 and clock.now >= restarts[0] + exits_at
                         and (ready_at is not None or clock.now < restarts[0] + exits_at + starts_at + 3))
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


def test_a_new_daemon_slow_to_start_under_host_load_is_given_a_longer_window_not_rolled_back(env, monkeypatch):
    # An upgrade on a laptop at load 57-78 on 14 cores: the new daemon had not written its start marker 61 s
    # after the old one stopped, so its release was rolled back, yet the same runtime imported in 5 s from
    # a copy. Under load the start window stretches with the load per core.
    from ttp import service
    p, head, clock, froms = _slow_stop_service(env, monkeypatch, exits_at=1.0, starts_at=150, ready_at=5.0,
                                               load=78 / 14)
    res = service.restart(p)
    assert res.outcome == "running", res
    _no_rollback(p, head)
    assert service.start_window_s(60, 78 / 14) == service.START_WAIT_CAP_S
    assert service.start_window_s(60, 0.3) == 60 and service.start_window_s(600, 9) == 600


def test_an_idle_host_keeps_the_short_start_window_and_a_loaded_one_is_bounded(env, monkeypatch):
    from ttp import service
    p, head, clock, froms = _slow_stop_service(env, monkeypatch, exits_at=1.0, starts_at=150, ready_at=5.0)
    t0 = clock.now
    res = service.restart(p)
    assert res.outcome == "rolled_back" and clock.now - t0 < 150, res
    assert "load is 0.0 per core, so its start window was 60s" in res, res


def test_a_new_daemon_that_never_starts_under_load_is_rolled_back_at_the_bound(env, monkeypatch):
    from ttp import service
    p, head, clock, froms = _slow_stop_service(env, monkeypatch, exits_at=1.0, starts_at=10_000, ready_at=5.0,
                                               load=12.0)
    t0 = clock.now
    res = service.restart(p)
    assert res.outcome == "rolled_back", res
    assert service.START_WAIT_CAP_S <= clock.now - t0 <= service.START_WAIT_CAP_S + 70, clock.now - t0
    assert f"so its start window was {service.START_WAIT_CAP_S}s" in res, res


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



def _launchd_calls(monkeypatch):
    """service and tunnel on macOS with nothing run; returns the launchctl calls."""
    from ttp import service, tunnel
    calls = []
    run = lambda *a: (a[0] == "launchctl" and calls.append(a)) or subprocess.CompletedProcess(a, 0, "", "")
    monkeypatch.setattr(service, "_run", run)
    monkeypatch.setattr(tunnel, "_run", run)
    monkeypatch.setattr(service.sys, "platform", "darwin")
    return calls


def test_the_daemon_plist_runs_standard_and_the_watchdog_and_tunnels_stay_background(env, monkeypatch):
    """ProcessType Background puts the daemon and every child (workers, git, pytest) on efficiency cores
    with throttled I/O: the daemon is Standard (its workers and checks stay niced) and keeps
    AbandonProcessGroup; the watchdog and kept tunnels stay Background (owner's agent msg #169)."""
    import plistlib
    from ttp import service, tunnel
    p = make(env)
    monkeypatch.setenv("HOME", str(env["tmp"] / "userhome"))
    calls = _launchd_calls(monkeypatch)
    assert service._install_launchd(p).endswith(" with a watchdog"), calls
    daemon, watchdog = (plistlib.loads(service._agent(p, wd).read_bytes()) for wd in (False, True))
    assert daemon["ProcessType"] == "Standard" and daemon["AbandonProcessGroup"] is True, daemon
    assert watchdog["ProcessType"] == "Background", watchdog
    assert tunnel.launchd_plist("host-a", ["ssh", "host-a"])["ProcessType"] == "Background"


@pytest.mark.parametrize("old", [{"ProcessType": "Background", "AbandonProcessGroup": True},
                                 {"ProcessType": "Background"}, {"ProcessType": "Standard"}])
def test_an_upgrade_over_an_old_daemon_plist_rewrites_it_standard_and_reloads_it(env, monkeypatch, old):
    """`ttp upgrade` restarts through restart_service: a daemon plist an older tt-project installed
    (Background, or without AbandonProcessGroup) gets DAEMON_KEYS, keeps its other keys, and is
    reloaded by bootout and bootstrap (kickstart keeps the loaded job). The watchdog's stays; a second
    restart only kickstarts."""
    import plistlib
    from ttp import service
    p = make(env)
    monkeypatch.setenv("HOME", str(env["tmp"] / "userhome"))
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    calls = _launchd_calls(monkeypatch)
    for wd in (False, True):
        f = service._agent(p, wd)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(plistlib.dumps({"Label": service.launchd_label(p, wd), "Extra": "kept",
                                      **(old if not wd else {"ProcessType": "Background"})}))
    uid, label = os.getuid(), service.launchd_label(p)
    assert service.restart_service(p) == "restarted"
    job = plistlib.loads(service._agent(p).read_bytes())
    assert job == {"Label": label, "Extra": "kept", "ProcessType": "Standard", "AbandonProcessGroup": True}, job
    assert plistlib.loads(service._agent(p, True).read_bytes())["ProcessType"] == "Background"
    assert calls == [("launchctl", "bootout", f"gui/{uid}/{label}"),
                     ("launchctl", "bootstrap", f"gui/{uid}", str(service._agent(p)))], calls
    calls.clear()
    assert service.restart_service(p) == "restarted"
    assert calls == [("launchctl", "kickstart", "-k", f"gui/{uid}/{label}")], calls


def test_the_daemon_reloading_its_own_rewritten_plist_does_it_from_a_new_session(env, monkeypatch):
    """The daemon (or a worker under it) restarting itself: bootout would end the caller before its
    bootstrap, so both run in a new session. From the watchdog's job they run in the foreground, and a
    bootstrap refused falls back to a hand start."""
    import plistlib
    from ttp import service
    p = make(env)
    monkeypatch.setenv("HOME", str(env["tmp"] / "userhome"))
    calls = _launchd_calls(monkeypatch)
    spawned, popen = [], []
    monkeypatch.setattr(service, "_spawn", lambda p: spawned.append(p))
    monkeypatch.setattr(service.subprocess, "Popen", lambda argv, **kw: popen.append((argv, kw)))

    def old():
        f = service._agent(p)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(plistlib.dumps({"Label": service.launchd_label(p), "ProcessType": "Background"}))
    label = service.launchd_label(p)
    old()
    service._agent(p, watchdog=True).write_bytes(plistlib.dumps({"Label": "w", "ProcessType": "Background"}))
    monkeypatch.setenv("XPC_SERVICE_NAME", label)
    assert service.restart_service(p) == "restarted" and not calls and not spawned
    (argv, kw), = popen
    assert argv[:2] == ["sh", "-c"] and kw["start_new_session"], popen
    assert f"launchctl bootout gui/{os.getuid()}/{label};" in argv[2] and str(service._agent(p)) in argv[2]
    old()
    popen.clear()
    monkeypatch.setenv("XPC_SERVICE_NAME", service.launchd_label(p, watchdog=True))
    monkeypatch.setattr(service, "_run", lambda *a: (a[0] == "launchctl" and calls.append(a)) or subprocess.CompletedProcess(
        a, 5 if a[1:2] == ("bootstrap",) else 0, "", "refused"))
    assert service.restart_service(p) == "restarted" and spawned == [p] and not popen
    # The refused reload is followed by the not-loaded fallback's own bootstrap; refused again, a hand start.
    assert [c[1] for c in calls] == ["bootout", "bootstrap", "bootstrap"] and calls[0][2].endswith(f"/{label}"), calls
    assert "launchctl bootstrap and kickstart failed" in (p.logs / "daemon.out").read_text()
    assert plistlib.loads(service._agent(p).read_bytes())["ProcessType"] == "Standard"


@pytest.mark.parametrize("pris, plist_type, want, cleared", [
    ([4, 31], None, "normal (pri 31; left the background priority it inherited from its starter)", True),
    ([4, 31], "Standard", "normal (pri 31; left the background priority it inherited from its starter)", True),
    ([31], "Standard", "normal (pri 31)", False),
    ([4], "Background", "background (pri 4; its launchd plist sets ProcessType Background)", False),
    ([None], None, "unknown (ps could not read it)", False)])
def test_a_daemon_started_from_a_background_parent_leaves_background_unless_its_plist_says_so(
        env, monkeypatch, pris, plist_type, want, cleared):
    """The start log reports the priority `ps` reads (what the kernel applies), not getpriority's darwin
    background flag, which misses launchd's Background process type. A ProcessType Background the user or
    a coordinator set in its plist is kept."""
    import plistlib
    from ttp import service
    monkeypatch.setattr(service.sys, "platform", "darwin")
    left = []
    monkeypatch.setattr(service, "_ps_pri", lambda pid=None: pris[min(len(left), len(pris) - 1)])
    monkeypatch.setattr(service, "_leave_darwin_bg", lambda: left.append(1))
    plist = env["tmp"] / "agent.plist"
    if plist_type:
        plist.write_bytes(plistlib.dumps({"Label": "x", "ProcessType": plist_type}))
    assert service.leave_background(plist if plist_type else None) == f"cpu priority: {want}"
    assert left == ([1] if cleared else [])


def test_a_daemon_still_background_without_its_plist_saying_so_logs_a_warning(env, monkeypatch):
    """launchd's Background process type survives setpriority: the log says so, never 'normal'."""
    from ttp import service
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "_ps_pri", lambda pid=None: 4)
    monkeypatch.setattr(service, "_leave_darwin_bg", lambda: None)
    out = service.leave_background(None)
    assert out.startswith("warning: cpu priority: background (pri 4)") and "normal" not in out, out


def test_off_macos_leave_background_does_nothing(env, monkeypatch):
    from ttp import service
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "_ps_pri", lambda pid=None: pytest.fail("read on linux"))
    assert service.leave_background(None) == ""


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("taskpolicy"), reason="macOS taskpolicy only")
def test_on_macos_a_process_started_under_taskpolicy_b_leaves_it_and_says_so(env):
    """taskpolicy -b (a cron or shell start at background priority; not the watchdog, whose launchd
    Background type is the next test's): setpriority clears it, and the report matches ps."""
    code = ("from ttp import service; print(service._ps_pri()); print(service.leave_background(None)); "
            "print(service._ps_pri())")
    r = subprocess.run(["taskpolicy", "-b", sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(RUNTIME)}, timeout=60)
    before, said, after = r.stdout.split("\n")[:3]
    assert int(before) <= 4 < int(after), r.stdout + r.stderr
    assert said == f"cpu priority: normal (pri {after}; left the background priority it inherited from its starter)"


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("launchctl"), reason="macOS launchd only")
def test_on_macos_under_a_real_launchd_background_agent_the_reported_priority_matches_ps(env):
    """The watchdog runs as a launchd agent with ProcessType Background. A process started that way (as a
    hand restart from the watchdog would be) reports the priority ps reads, not 'normal' while at pri 4."""
    import plistlib
    import uuid
    label = f"com.tt-project.test-prio.{uuid.uuid4().hex[:12]}"
    out, plist = env["tmp"] / "probe.out", env["tmp"] / f"{label}.plist"
    code = ("import os, subprocess; from ttp import service; said = service.leave_background(None); "
            "pri = subprocess.run(['ps', '-o', 'pri=', '-p', str(os.getpid())], capture_output=True, "
            f"text=True).stdout.strip(); open({str(out)!r}, 'w').write(said + '\\n' + pri + '\\n')")
    plist.write_bytes(plistlib.dumps({
        "Label": label, "ProgramArguments": [sys.executable, "-c", code], "ProcessType": "Background",
        "RunAtLoad": True, "EnvironmentVariables": {"PYTHONPATH": str(RUNTIME)},
        "StandardErrorPath": str(env["tmp"] / "probe.err")}))
    domain = f"gui/{os.getuid()}"
    try:
        r = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            pytest.skip(f"no launchd gui domain here: {r.stderr.strip()}")
        deadline = time.time() + 60
        while time.time() < deadline and len(out.read_text().splitlines() if out.exists() else []) < 2:
            time.sleep(0.2)
        said, pri = out.read_text().splitlines()[:2]
    finally:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, timeout=30)
        plist.unlink(missing_ok=True)
    assert f"(pri {pri}" in said, (said, pri)
    assert ("normal" in said.split("(")[0]) == (int(pri) > 4), (said, pri)


class _PriorityChecked(BaseException):
    pass


def test_the_daemon_leaves_background_at_start_before_any_worker_with_its_own_plist(env, monkeypatch):
    """Every restart route (launchd kickstart or reload, the hand fallback the watchdog calls, cron)
    starts `python -m ttp.daemon`, whose run() sets its priority from its own plist before it starts any
    run, and logs the priority it runs at."""
    from ttp import service
    from ttp.daemon import Daemon
    p = make(env)
    seen = []

    def check(plist):
        seen.append(plist)
        raise _PriorityChecked
    monkeypatch.setattr(service, "leave_background", check)
    try:
        Daemon(p.base).run()
    except _PriorityChecked:
        pass
    assert seen == [service._agent(p)], seen
    assert not p.db.q("SELECT id FROM runs"), "a run started before the daemon set its priority"


def _darwin_agent(env, monkeypatch, plist=True):
    import plistlib
    from ttp import service
    p = make(env)
    monkeypatch.setenv("HOME", str(env["tmp"] / "userhome"))
    monkeypatch.setattr(service.sys, "platform", "darwin")
    if plist:
        f = service._agent(p)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(plistlib.dumps({"Label": service.launchd_label(p), **service.DAEMON_KEYS}))
    monkeypatch.setattr(service, "refresh", lambda p: False)   # its plists are current
    popen = []
    monkeypatch.setattr(service.subprocess, "Popen", lambda argv, **kw: popen.append(argv))
    return p, popen


def test_a_restart_of_a_not_loaded_agent_bootstraps_its_plist_and_never_starts_the_daemon_by_hand(env, monkeypatch):
    """The watchdog (Background) calls restart_service: when kickstart finds the agent not loaded, the
    daemon is started by launchd from its plist (its ProcessType), not as a child of the watchdog."""
    from ttp import service
    p, popen = _darwin_agent(env, monkeypatch)
    calls = []

    def run(*a):
        calls.append(a[:2])
        return subprocess.CompletedProcess(a, 113 if a[1] == "kickstart" and "-k" in a else 0, "", "")
    monkeypatch.setattr(service, "_run", run)
    assert service.restart_service(p) == "restarted"
    assert calls == [("launchctl", "kickstart"), ("launchctl", "bootstrap"), ("launchctl", "kickstart")], calls
    assert popen == [], "the daemon was started by hand from the caller"


def test_a_restart_without_a_plist_starts_the_daemon_by_hand_and_logs_its_inherited_priority(env, monkeypatch):
    from ttp import service
    p, popen = _darwin_agent(env, monkeypatch, plist=False)
    monkeypatch.setattr(service, "_run", lambda *a: pytest.fail(f"launchctl without a plist: {a}"))
    assert service.restart_service(p) == "restarted"
    assert len(popen) == 1
    assert "no launchd agent is installed" in (p.logs / "daemon.out").read_text()


def test_every_restart_route_outside_launchd_starts_the_daemon_module(env, monkeypatch):
    """When launchd refuses the agent (bootstrap fails) the daemon is still started by hand, as cron's
    lines do, with the reason and its inherited priority logged; both start the daemon module, whose start
    reports the priority it runs at."""
    from ttp import service
    p, popen = _darwin_agent(env, monkeypatch)
    monkeypatch.setattr(service, "_run", lambda *a: subprocess.CompletedProcess(a, 3, "", "not loaded"))
    assert service.restart_service(p) == "restarted"
    assert "launchctl bootstrap and kickstart failed" in (p.logs / "daemon.out").read_text()
    (argv,), daemon = popen, " ".join(shlex.quote(a) for a in service.daemon_argv(p))
    assert argv[:2] == ["sh", "-c"] and daemon in argv[2], argv
    assert service.daemon_argv(p)[1:3] == ["-m", "ttp.daemon"]
    assert all(daemon in line for line in service._cron_lines(p))
