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
    assert [c[1] for c in calls] == ["bootout", "bootstrap"] and calls[0][2].endswith(f"/{label}"), calls
    assert plistlib.loads(service._agent(p).read_bytes())["ProcessType"] == "Standard"


@pytest.mark.parametrize("bg, plist_type, want, cleared", [
    (1, None, "normal (left the background priority it inherited from its starter)", True),
    (1, "Standard", "normal (left the background priority it inherited from its starter)", True),
    (0, "Standard", "normal", False),
    (1, "Background", "background (its launchd plist sets ProcessType Background)", False),
    (None, None, "", False)])
def test_a_daemon_started_from_a_background_parent_leaves_background_unless_its_plist_says_so(
        env, monkeypatch, bg, plist_type, want, cleared):
    """Darwin background priority is inherited: a daemon the watchdog (a Background agent) or a hand or
    cron restart starts runs on efficiency cores with throttled I/O unless it leaves it. A ProcessType
    Background the user or a coordinator set in its plist is kept; off macOS (None) nothing happens."""
    import plistlib
    from ttp import service
    sets = []
    state = {"bg": bg}

    def fake(set_to=None):
        if set_to is not None:
            sets.append(set_to)
            state["bg"] = set_to
        return state["bg"]
    monkeypatch.setattr(service, "_darwin_bg", fake)
    plist = env["tmp"] / "agent.plist"
    if plist_type:
        plist.write_bytes(plistlib.dumps({"Label": "x", "ProcessType": plist_type}))
    out = service.leave_background(plist if plist_type else None)
    assert out == (f"cpu priority: {want}" if want else ""), out
    assert sets == ([0] if cleared else []), sets


def test_a_failed_leave_says_the_daemon_still_runs_background(env, monkeypatch):
    from ttp import service
    monkeypatch.setattr(service, "_darwin_bg", lambda set_to=None: 1)
    assert service.leave_background(None) == "cpu priority: background (inherited from its starter; leaving it failed)"


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("taskpolicy"), reason="macOS taskpolicy only")
def test_on_macos_a_process_started_background_runs_and_starts_children_at_normal_priority(env):
    """The real call: a process started at darwin background priority (taskpolicy -b, as from the
    watchdog's job) leaves it, and a child it starts afterwards (a worker, git) runs normal too."""
    code = ("import subprocess; from ttp import service; print(service._darwin_bg()); "
            "print(service.leave_background(None)); print(service._darwin_bg()); "
            "print(subprocess.run(['sh', '-c', 'ps -o pri= -p $$'], capture_output=True, text=True).stdout.strip())")
    r = subprocess.run(["taskpolicy", "-b", sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(RUNTIME)}, timeout=60)
    before, said, after, child_pri = r.stdout.split("\n")[:4]
    assert (before, after) == ("1", "0"), r.stdout + r.stderr
    assert said == "cpu priority: normal (left the background priority it inherited from its starter)"
    assert int(child_pri) > 4, f"the child still runs at darwin background priority {child_pri}"


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


def test_every_restart_route_outside_launchd_starts_the_daemon_module(env, monkeypatch):
    """The hand fallback (watchdog or upgrade when the agent is not loaded) and cron's lines start the
    daemon module, so its start sets the priority whichever process (Background or not) ran them."""
    from ttp import service
    p = make(env)
    monkeypatch.setenv("HOME", str(env["tmp"] / "userhome"))
    monkeypatch.setattr(service.sys, "platform", "darwin")
    f = service._agent(p)
    f.parent.mkdir(parents=True, exist_ok=True)
    import plistlib
    f.write_bytes(plistlib.dumps({"Label": service.launchd_label(p), **service.DAEMON_KEYS}))
    monkeypatch.setattr(service, "_run", lambda *a: subprocess.CompletedProcess(a, 3, "", "not loaded"))
    popen = []
    monkeypatch.setattr(service.subprocess, "Popen", lambda argv, **kw: popen.append(argv))
    assert service.restart_service(p) == "restarted"
    (argv,), daemon = popen, " ".join(shlex.quote(a) for a in service.daemon_argv(p))
    assert argv[:2] == ["sh", "-c"] and daemon in argv[2], argv
    assert service.daemon_argv(p)[1:3] == ["-m", "ttp.daemon"]
    assert all(daemon in line for line in service._cron_lines(p))
