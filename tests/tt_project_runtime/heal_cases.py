# Heal tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new heal tests here, not there.
from __future__ import annotations


@pytest.mark.parametrize("outage", ["auth", "network", "disk", "heal"])
def test_alerts_ack_refuses_a_live_outage_without_mutation(env, outage):
    from ttp import alerts, cli, heal, netwait
    from ttp.daemon import Daemon
    from ttp.web import attention
    if outage == "heal":
        p, d, up = _heal_setup(env, fix=None, outage=True)
        spec, now = _heal_spec(p), time.time()
        heal.run(d, "svc", spec, now)
        heal.run(d, "svc", spec, now + heal.OUTAGE_S)
        p.db.update_task(heal.state(p.db, "svc")["task"], status="failed")
        heal.run(d, "svc", spec, now + heal.OUTAGE_S + 1)
        key = "heal:svc"
    else:
        p = make(env)
        key = {"auth": "auth:fake", "network": netwait.KEY, "disk": "disk"}[outage]
        if outage == "auth":
            p.db.set_kv(alerts.BREAKER + "fake", {"open": True, "ts": time.time()})
        p.db.set_kv("network_outage" if outage == "network" else "disk_low",
                    {"alerted": True} if outage == "network" else {"free_gb": 1})
        Daemon(p.base).alert(key, f"{key} is down", "high")
    mid = p.db.one("SELECT id FROM messages WHERE kind='alert' AND ref=? ORDER BY id DESC", (key,))["id"]
    before = p.db.one("SELECT COUNT(*) n FROM messages")["n"]
    with pytest.raises(SystemExit) as exc:
        cli.main(["alerts", "demo", "--ack", str(mid), "--why", "known"])
    assert exc.value.code != 0
    assert _episodes(p, key)[-1]["cleared"] is None and not p.db.kv(alerts.ACKS)
    assert p.db.one("SELECT COUNT(*) n FROM messages")["n"] == before
    assert [m["id"] for m in attention(p.db, time.time())] == [mid], "the outage left the top section"


def _no_project_env(monkeypatch):
    """The daemon's environment as a service manager may start it: no TTP_PROJECT, no ttp on PATH."""
    for var in ("TTP_PROJECT", "TTP_RUN_DIR", "TTP_LOCKS_HELD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


def test_daemon_run_commands_resolve_the_project_without_ttp_project(env, monkeypatch):
    """`ttp lock <res> -- cmd` exits 2 without a project. A command schedule, a heal check and fix,
    an until_probe and a pause's end_when all run it in their project's context, so it runs."""
    p = _device_project(env, device=False)
    from ttp import coordinator as coord, ends, heal, pauseends
    from ttp.daemon import Daemon
    from ttp.project import command_env
    _no_project_env(monkeypatch)
    locked = "ttp lock board -- true"
    d = Daemon(p.base)
    assert d._run_command_watcher({"name": "w"}, {"command": locked}) == "ok (0 observations)"
    assert not p.db.q("SELECT text FROM events WHERE source IN ('watcher:w', 'watcher-error:w')")
    spec = heal.validate({"check": locked})
    assert heal.run(d, "svc", spec, time.time()) == ("ok (healthy)", None), heal.state(p.db, "svc")
    flag = p.root / "fixed"
    st = {}
    assert heal.run_fix(dict(spec, fix=f"ttp lock board -- touch {flag}"), str(p.root),
                        command_env(p)) == (0, "") and flag.exists()
    assert heal.check(spec, st, str(p.root), command_env(p))[0] == 0
    # A pause whose end_when takes a lock passes; without TTP_PROJECT it would be "broken (exit 2)".
    assert coord.apply(p, [{"type": "resource_pause", "resource": "rig", "paused": True,
                            "end_when": "ttp lock other -- true", "reason": "flaky"}]) == []
    pe, now = pauseends.PauseEnds(p), time.time()
    pe.tick(now)
    for proc, _, _ in list(pe._procs.values()):
        proc.wait(30)
    assert pe.tick(now + 1) == ["rig"]
    due = p.db.one("SELECT text FROM events WHERE kind='pause_end_due'")["text"]
    assert "probe `ttp lock other -- true` passed" in due, due


def test_failed_daemon_run_commands_keep_a_bounded_stderr_tail(env, monkeypatch):
    p = _device_project(env, device=False)
    from ttp import coordinator as coord, ends, heal, pauseends
    from ttp.daemon import Daemon
    from ttp.project import OUTPUT_TAIL
    _no_project_env(monkeypatch)
    noisy = "printf 'x%.0s' $(seq 5000) >&2; echo >&2; echo the-real-cause >&2; exit 3"
    d = Daemon(p.base)
    d._run_command_watcher({"name": "w"}, {"command": noisy})
    text = p.db.one("SELECT text FROM events WHERE source='watcher-error:w'")["text"]
    assert text.startswith("watcher command failed rc=3: ") and text.endswith("the-real-cause"), text[-80:]
    assert len(text) <= OUTPUT_TAIL + 40
    # A watcher that printed reports and then failed keeps both: its reports, and its failure with why.
    report = shlex.quote(json.dumps({"text": "box-a is down", "severity": "high"}))
    d._run_command_watcher({"name": "v"}, {"command": f"echo {report}; echo why-it-broke >&2; exit 2"})
    got = [r["text"] for r in p.db.q("SELECT text FROM events WHERE source IN ('watcher:v', 'watcher-error:v') "
                                     "ORDER BY id")]
    assert sorted(got) == ["box-a is down", "watcher command exited rc=2: why-it-broke"], got
    # An until_probe that neither passes nor says "not yet" is broken: its stderr tail says why.
    assert coord.apply(p, [{"type": "charter_update", "section": "Restrictions", "text": "Hands off the rig.",
                            "until": "the rig is fixed", "until_probe": noisy, "source": {"origin": "gate"}}]) == []
    e, now = ends.Ends(p), time.time()
    e.tick(now)
    e._procs[next(iter(e._procs))][0].wait(30)
    e.tick(now + 1)
    (why,) = (p.db.kv(ends.BROKEN_KEY) or {}).values()
    assert why.startswith("exit 3: ") and why.endswith("the-real-cause") and len(why) <= OUTPUT_TAIL + 10, why[-80:]
    assert not e._errs, "each probe's stderr file is closed once read"
    # So does a pause's broken end_when, and it still wakes the coordinator.
    assert coord.apply(p, [{"type": "resource_pause", "resource": "rig", "paused": True,
                            "end_when": "echo no-such-box >&2; exit 4", "reason": "flaky"}]) == []
    pe = pauseends.PauseEnds(p)
    pe.tick(now)
    for proc, _, _ in list(pe._procs.values()):
        proc.wait(30)
    assert pe.tick(now + 1) == ["rig"]
    due = p.db.one("SELECT text FROM events WHERE kind='pause_end_due'")["text"]
    assert "is broken (exit 4): it would never end. Its stderr: no-such-box" in due, due
    # A heal check's output, stderr included, is kept bounded in its state for the self-fix task.
    assert heal.run(d, "svc", heal.validate({"check": noisy}), now)[0].startswith("error: heal check exited 3")
    st = heal.state(p.db, "svc")
    assert st["last_out"].endswith("the-real-cause") and len(st["last_out"]) <= 1000


def test_heal_block_is_validated_in_the_file_and_schedule_set(env):
    from ttp import heal
    from ttp import schedule as sched
    with pytest.raises(ValueError, match="needs `check`"):
        heal.validate({"fix": "x"})
    with pytest.raises(ValueError, match="unknown key"):
        heal.validate({"check": "true", "unit": "x"})
    with pytest.raises(ValueError, match="needs `unit`"):
        heal.validate({"preset": "systemd"})
    s = heal.validate({"preset": "systemd", "unit": "w.service", "user": True, "host": "box-a"})
    assert s["fix"] == "ssh -o BatchMode=yes box-a 'systemctl --user restart w.service'" and s["max_fixes"] == 3
    ok = [{"name": "svc", "kind": "command", "every": "5m", "payload": {"heal": {"check": "true"}}}]
    assert sched.parse_file(json.dumps(ok))
    with pytest.raises(ValueError, match="only a command schedule"):
        sched.parse_file(json.dumps([dict(ok[0], kind="llm")]))
    with pytest.raises(ValueError, match="grace_s"):
        sched.parse_file(json.dumps([dict(ok[0], payload={"heal": {"check": "true", "grace_s": -1}})]))
    p = make(env)
    from ttp import coordinator as coord
    coord.apply(p, [{"type": "schedule_set", "name": "svc", "kind": "command", "every": "5m",
                             "heal": {"check": "true", "fix": "true"}}])
    assert json.loads(p.db.one("SELECT payload FROM schedules WHERE name='svc'")["payload"])["heal"]["fix"] == "true"


def test_heal_dead_service_is_fixed_without_coordinator_turns_or_asks(env):
    from ttp import daemon as dm
    from ttp import heal
    p, d, up = _heal_setup(env)
    p.db.x("UPDATE schedules SET next_run=0 WHERE name='svc'")
    fp, events = dm.wake_fingerprint(p, {}), p.db.one("SELECT COUNT(*) n FROM events")["n"]
    tasks, msgs = p.db.q("SELECT id, status FROM tasks"), p.db.one("SELECT COUNT(*) n FROM messages")["n"]
    d.run_schedules()
    assert up.exists() and heal.state(p.db, "svc")["phase"] == "settling"
    assert p.db.one("SELECT next_run FROM schedules WHERE name='svc'")["next_run"] <= time.time()
    d.run_schedules()   # the settle recheck
    row = p.db.one("SELECT last_status FROM schedules WHERE name='svc'")
    assert row["last_status"] == "fixed (1 today)"
    assert heal.state(p.db, "svc")["status"] == "healthy"
    assert p.db.q("SELECT id, status FROM tasks") == tasks, p.db.q("SELECT id, title, origin FROM tasks")
    assert p.db.one("SELECT COUNT(*) n FROM messages")["n"] == msgs + 1   # the info line only
    assert not p.db.q("SELECT id FROM messages WHERE kind IN ('ask','alert')")
    assert p.db.one("SELECT COUNT(*) n FROM events")["n"] == events
    assert not p.db.q("SELECT id FROM runs WHERE role='coordinator'")
    assert dm.wake_fingerprint(p, {}) == fp
    info = p.db.q("SELECT text, severity FROM messages WHERE kind='info'")
    assert len(info) == 1 and info[0]["severity"] == "low" and "Self-healed svc" in info[0]["text"]
    assert heal.digest_lines(p.db, 0) == ["## Self-healed (nothing to do): fixed svc, 1 today"]
    assert heal.line(p.db) == "health: 1 ok, 1 fixed today, 0 failing"


def test_heal_grace_unknown_and_broken_check_never_fix(env):
    from ttp import heal
    p, d, up = _heal_setup(env, grace_s=300)
    spec, now = _heal_spec(p), time.time()
    assert heal.run(d, "svc", spec, now) == ("unhealthy (0 s, grace 300 s)", now + 300)
    assert heal.run(d, "svc", spec, now + 100)[0] == "unhealthy (100 s, grace 300 s)" and not up.exists()
    for rc, status in ((75, "ok (unknown, rc 75)"), (255, "ok (unknown, rc 255)")):
        assert heal.run(d, "svc", dict(spec, check=f"exit {rc}"), now + 400) == (status, None)
    assert heal.run(d, "svc", dict(spec, check="exit 2"), now + 400)[0].startswith("error: heal check exited 2")
    assert not up.exists() and heal.summary(p.db)["unknown"] == 1
    up.touch()
    assert heal.run(d, "svc", spec, now + 500) == ("ok (healthy)", None)   # healthy clears, no fix recorded
    st = heal.state(p.db, "svc")
    assert st["status"] == "healthy" and "since" not in st and "unknown_since" not in st
    assert heal.fixed_today(p.db) == 0
    up.unlink()
    assert heal.run(d, "svc", spec, now + 600)[0] == "unhealthy (0 s, grace 300 s)"   # a new stint


def test_heal_backs_off_to_healthy_every_after_healthy_runs_and_shows_it(env, capsys, monkeypatch):
    from ttp import cli, heal
    p, d, up = _heal_setup(env, healthy_every="1h")
    up.touch()
    for n in (1, 2, 3):
        p.db.x("UPDATE schedules SET next_run=0 WHERE name='svc'")
        d.run_schedules()
        row = p.db.one("SELECT * FROM schedules WHERE name='svc'")
        assert row["last_status"] == "ok (healthy)" and heal.state(p.db, "svc")[heal.STREAK_KEY] == n
        assert row["next_run"] - row["last_run"] == pytest.approx(3600 if n == heal.HEALTHY_RUNS else 300), n
    r = p.db.one("SELECT * FROM schedules WHERE name='svc'")
    assert "; every 1h (3 healthy in a row)" in heal.describe(r, _heal_spec(p), p.db)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    cli.main(["schedules", p.name])
    assert "every 5m (now 1h: healthy)" in capsys.readouterr().out
    up.unlink()   # the first unhealthy run goes straight back to `every`
    p.db.x("UPDATE schedules SET next_run=0 WHERE name='svc'")
    d.run_schedules()
    row = p.db.one("SELECT * FROM schedules WHERE name='svc'")
    assert heal.STREAK_KEY not in heal.state(p.db, "svc") and row["next_run"] - row["last_run"] <= 300
    assert "; every 5m, 1h after 3 healthy in a row (0 so far)" in heal.describe(row, _heal_spec(p), p.db)
    # Without `healthy_every` a healthy streak changes nothing.
    p2, d2, up2 = _heal_setup(env)
    up2.touch()
    for _ in range(4):
        p2.db.x("UPDATE schedules SET next_run=0 WHERE name='svc'")
        d2.run_schedules()
    row = p2.db.one("SELECT * FROM schedules WHERE name='svc'")
    assert row["next_run"] - row["last_run"] == pytest.approx(300)


@pytest.mark.parametrize("result", ["unhealthy", "unknown", "error", "known", "fixed"])
def test_heal_any_result_but_plain_healthy_drops_back_to_every(env, result):
    from ttp import heal
    p, d, up = _heal_setup(env, healthy_every="1h", healthy_runs=2)
    spec, now, row = _heal_spec(p), time.time(), dict(p.db.one("SELECT * FROM schedules WHERE name='svc'"))
    up.touch()
    for i in range(2):
        assert heal.run(d, "svc", spec, now + i)[0] == "ok (healthy)"
    assert heal.interval(p.db, row, spec) == 3600
    if result == "unhealthy":
        up.unlink()
        run = dict(spec, grace_s=300)
    else:
        run = dict(spec, check={"unknown": "exit 75", "error": "exit 2",
                                "known": "echo 'ttp-heal: known ops: planned work'; exit 75",
                                "fixed": f"test -e {up} && test -e {up}.fixed"}.get(result, spec["check"]),
                   fix=f"touch {up}.fixed")
    status = heal.run(d, "svc", run, now + 10)[0]
    assert status != "ok (healthy)" and heal.interval(p.db, row, spec) == 300, status
    if result == "fixed":   # the fix ran and its recheck is healthy: still not a plain healthy run
        assert heal.run(d, "svc", run, now + 20)[0].startswith("fixed")
        assert heal.interval(p.db, row, spec) == 300
    up.touch()
    assert heal.run(d, "svc", spec, now + 30)[0] == "ok (healthy)"   # a new streak starts at one
    assert heal.state(p.db, "svc")[heal.STREAK_KEY] == 1 and heal.interval(p.db, row, spec) == 300


def test_heal_healthy_every_is_validated_against_every(env):
    from ttp import coordinator as coord
    from ttp import heal
    from ttp import schedule as sched
    assert heal.validate({"check": "true", "healthy_every": "1h"})["healthy_runs"] == heal.HEALTHY_RUNS
    assert heal.validate({"check": "true", "healthy_every": 3600, "healthy_runs": 5}, every_s=3600)
    for bad, why in (({"healthy_every": "soon"}, "is an interval"), ({"healthy_every": 0}, "is an interval"),
                     ({"healthy_every": True}, "is an interval"), ({"healthy_runs": 2}, "goes with"),
                     ({"healthy_every": "1h", "healthy_runs": 0}, "`healthy_runs` is an integer"),
                     ({"healthy_every": "1m"}, "must be at least")):
        with pytest.raises(ValueError, match=why):
            heal.validate({"check": "true", **bad}, every_s=300)
    entry = {"name": "svc", "kind": "command", "every": "5m", "payload": {"heal": {"check": "true", "healthy_every": "1h"}}}
    assert sched.parse_file(json.dumps([entry]))
    with pytest.raises(ValueError, match="must be at least"):
        sched.parse_file(json.dumps([dict(entry, every="2h")]))
    p = make(env)
    assert coord.apply(p, [{"type": "schedule_set", "name": "svc", "kind": "command", "every": "5m",
                            "heal": {"check": "true", "healthy_every": "1h"}}]) == []
    for act in ({"heal": {"check": "true", "healthy_every": "1m"}}, {"every": "2h"}):   # a kept block too
        coord.apply(p, [{"type": "schedule_set", "name": "svc", "kind": "command", **act}])
        row = p.db.one("SELECT every_s, payload FROM schedules WHERE name='svc'")
        assert row["every_s"] == 300 and json.loads(row["payload"])["heal"]["healthy_every"] == "1h", act
    assert "healthy_every" in coord.HEAL_SCHEMA["properties"] and "healthy_runs" in coord.HEAL_SCHEMA["properties"]


def test_heal_cap_then_one_self_fix_task_then_outage_alert_that_clears(env):
    from ttp import alerts, heal
    p, d, up = _heal_setup(env, max_fixes=2, window_h=1)
    spec, now = _heal_spec(p), time.time()
    for i in range(2):   # it dies again after each fix
        assert heal.run(d, "svc", spec, now + i * 10)[0] == "unhealthy (fixed; rechecking)"
        assert heal.run(d, "svc", spec, now + i * 10 + 1)[0] == "fixed (%d today)" % (i + 1)
        up.unlink()
    status, _ = heal.run(d, "svc", spec, now + 30)
    assert "cap" in status and not up.exists()
    t = p.db.one("SELECT * FROM tasks WHERE origin='daemon'")
    assert t["priority"] == 1 and json.loads(t["labels"]) == ["heal:svc"] and "test -e" in t["spec"]
    assert heal.run(d, "svc", spec, now + 40)[0] == f"unhealthy (self-fix task #{t['id']} open)"
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert'")
    p.db.update_task(t["id"], status="failed")
    assert "alerted" in heal.run(d, "svc", spec, now + 50)[0]
    heal.run(d, "svc", spec, now + 60)
    al = p.db.q("SELECT * FROM messages WHERE kind='alert'")
    assert len(al) == 1 and al[0]["severity"] == "high" and al[0]["ref"] == "heal:svc"
    assert alerts.holds(p.db, "heal:svc", now, now + 60)
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1
    up.touch()
    assert heal.run(d, "svc", spec, now + 70)[0] == "ok (healthy)"
    assert [e["key"] for e in alerts.sweep(p.db)] == ["heal:svc"]
    assert "passes again" in p.db.one("SELECT text FROM messages WHERE kind='resolved'")["text"]


def _heal_to_self_fix_failed(p, d, up, spec, now):
    """Drive the check (no fix) to its self-fix task, then fail that task."""
    from ttp import heal
    assert "it has no fix" in heal.run(d, "svc", spec, now)[0]
    tid = heal.state(p.db, "svc")["task"]
    p.db.update_task(tid, status="failed")
    return tid


def test_heal_escalate_user_alerts_and_coordinator_observes_once_per_episode(env):
    from ttp import heal
    p, d, up = _heal_setup(env, fix=None)
    spec, now = _heal_spec(p), time.time()
    assert heal.escalate_to(d, spec) == "user"
    tid = _heal_to_self_fix_failed(p, d, up, spec, now)
    assert "alerted" in heal.run(d, "svc", spec, now + 10)[0]
    assert [a["ref"] for a in p.db.q("SELECT ref FROM messages WHERE kind='alert'")] == ["heal:svc"]
    assert not p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'heal:%'")
    up.touch()
    heal.run(d, "svc", spec, now + 20)
    up.unlink()
    p.db.x("DELETE FROM messages WHERE kind='alert'")

    co = dict(spec, escalate="coordinator")
    t0 = now + 100
    tid2 = _heal_to_self_fix_failed(p, d, up, co, t0)
    assert tid2 != tid
    for i in range(3):   # repeated runs in one episode add nothing
        assert "coordinator told" in heal.run(d, "svc", co, t0 + 10 + i)[0]
    ev = p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'heal:%'")
    assert len(ev) == 1 and ev[0]["kind"] == "observation" and ev[0]["severity"] == "high"
    assert ev[0]["status"] == "queued" and ev[0]["fingerprint"] == f"heal:svc:{t0:.0f}" and "svc" in ev[0]["text"]
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert'"), "coordinator mode alerted the user"
    st = heal.state(p.db, "svc")
    st.pop("observed")   # a lost state flag still finds the episode's event
    heal._save(p.db, "svc", st)
    heal.run(d, "svc", co, t0 + 20)
    assert len(p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'heal:%'")) == 1

    up.touch()   # healthy ends the episode; the next one is told again
    heal.run(d, "svc", co, t0 + 30)
    up.unlink()
    t1 = t0 + 200
    _heal_to_self_fix_failed(p, d, up, co, t1)
    heal.run(d, "svc", co, t1 + 10)
    fps = [e["fingerprint"] for e in p.db.q("SELECT fingerprint FROM events WHERE fingerprint LIKE 'heal:%' ORDER BY id")]
    assert fps == [f"heal:svc:{t0:.0f}", f"heal:svc:{t1:.0f}"]
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert'")


@pytest.mark.parametrize("escalate", ["user", "coordinator"])
def test_heal_suppress_is_honoured_after_the_self_fix_task_ended(env, escalate):
    """A planned shutdown that starts while the episode is escalated: `suppress` exiting 0 tells no one;
    once it says no, the escalation goes out as before."""
    from ttp import heal
    down = env["repo"] / "planned-down"
    p, d, up = _heal_setup(env, fix=None, suppress=f"test -e {down}", escalate=escalate)
    spec, now = _heal_spec(p), time.time()
    _heal_to_self_fix_failed(p, d, up, spec, now)
    down.touch()
    for dt in (10, 20):
        assert heal.run(d, "svc", spec, now + dt) == ("unhealthy (suppressed: deliberately down)", None)
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert'")
    assert not p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'heal:%'")
    down.unlink()
    told = heal.run(d, "svc", spec, now + 30)[0]
    assert ("alerted" if escalate == "user" else "coordinator told") in told
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert'")) == (escalate == "user")
    assert len(p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'heal:%'")) == (escalate == "coordinator")


@pytest.mark.parametrize("escalate", ["user", "coordinator"])
def test_heal_escalated_episode_tells_a_new_finding_once(env, escalate):
    """While escalated, a different finding (normalized: counters and times do not count) is told once
    more under its own fingerprint; the same or an earlier finding stays quiet."""
    from ttp import heal
    msg = env["repo"] / "finding"
    p, d, up = _heal_setup(env, fix=None, check=f"cat {msg}; exit 1", escalate=escalate)
    spec, now = _heal_spec(p), time.time()

    def told():
        if escalate == "user":
            return [m["text"] for m in p.db.q("SELECT text FROM messages WHERE kind='alert' AND ref='heal:svc' ORDER BY id")]
        return [e["fingerprint"] for e in p.db.q("SELECT fingerprint FROM events WHERE fingerprint LIKE 'heal:%' ORDER BY id")]

    msg.write_text("ttp-heal: not-outage chip 0 lost after 12 s")
    _heal_to_self_fix_failed(p, d, up, spec, now)
    heal.run(d, "svc", spec, now + 10)
    assert len(told()) == 1
    msg.write_text("ttp-heal: not-outage chip 0 lost after 40 s")   # the same finding, normalized
    heal.run(d, "svc", spec, now + 20)
    assert len(told()) == 1
    msg.write_text("ttp-heal: not-outage chip 1 is off the bus")
    for dt in (30, 40):
        heal.run(d, "svc", spec, now + dt)
    assert len(told()) == 2 and "different finding" in (told()[1] if escalate == "user" else
                                                        p.db.q("SELECT text FROM events ORDER BY id DESC")[0]["text"])
    if escalate == "coordinator":
        since = heal.state(p.db, "svc")["since"]
        assert told()[0] == f"heal:svc:{since:.0f}" and told()[1].startswith(f"heal:svc:{since:.0f}:")
    msg.write_text("ttp-heal: not-outage chip 0 lost after 99 s")   # told before in this episode
    heal.run(d, "svc", spec, now + 50)
    assert len(told()) == 2


@pytest.mark.parametrize("change", ["coordinator", "project_default", "known_fault", "finding_known_fault"])
@pytest.mark.parametrize("run_check", [False, True])
def test_heal_config_change_closes_an_existing_user_alert(env, change, run_check):
    from ttp import alerts, heal
    from ttp.web import attention
    finding = change == "finding_known_fault"
    p, d, up = _heal_setup(env, fix=None, outage=finding,
                          check="echo 'ttp-heal: not-outage'; exit 1" if finding else "exit 1")
    spec, now = _heal_spec(p), time.time()
    _heal_to_self_fix_failed(p, d, up, spec, now)
    heal.run(d, "svc", spec, now + 1)
    assert len(attention(p.db, now + 1)) == 1
    payload = json.loads(p.db.one("SELECT payload FROM schedules WHERE name='svc'")["payload"])
    if change == "project_default":
        p.set_config("heal.escalate", "coordinator")
        d.cfg = p.config()
    else:
        payload["heal"]["escalate" if change == "coordinator" else "known_fault"] = (
            "coordinator" if change == "coordinator" else "vendor bug")
        p.db.x("UPDATE schedules SET payload=? WHERE name='svc'", (json.dumps(payload),))
    # Readers hide it immediately, even before the next scheduled check or daemon sweep.
    assert attention(p.db, now + 2) == []
    if run_check:
        heal.run(d, "svc", _heal_spec(p), now + 2)
    alerts.sweep(p.db, now + 3)
    ep = _episodes(p, "heal:svc")[0]
    assert ep["cleared"] and ("coordinator" if change in ("coordinator", "project_default") else "known_fault") in ep["cleared_why"]
    assert heal.state(p.db, "svc")["status"] == "unhealthy", "closing a user alert does not repair the check"
    resolved = p.db.q("SELECT text FROM messages WHERE kind='resolved'")
    assert len(resolved) == 1 and "passes again" not in resolved[0]["text"]
    for dt in (4, 5):
        heal.run(d, "svc", _heal_spec(p), now + dt)
        alerts.sweep(p.db, now + dt)
    assert len(p.db.q("SELECT id FROM messages WHERE kind='resolved'")) == 1
    assert len(_episodes(p, "heal:svc")) == 1
    if change in ("coordinator", "project_default"):
        assert len(p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'heal:%'")) == 1
    else:
        assert not {"task", "escalated", "alerted", "observed"} & set(heal.state(p.db, "svc"))


@pytest.mark.parametrize("unknown", [False, True])
def test_heal_known_fault_does_not_close_a_real_outage_alert(env, unknown):
    from ttp import alerts, heal
    p, d, up = _heal_setup(env, fix=None, outage=True, check="exit 255" if unknown else "exit 1")
    spec, now = _heal_spec(p), time.time()
    heal.run(d, "svc", spec, now)
    heal.run(d, "svc", spec, now + heal.OUTAGE_S)
    tid = heal.state(p.db, "svc")["task"]
    p.db.update_task(tid, status="failed")
    heal.run(d, "svc", spec, now + heal.OUTAGE_S + 1)
    payload = json.loads(p.db.one("SELECT payload FROM schedules WHERE name='svc'")["payload"])
    payload["heal"]["known_fault"] = "vendor bug"
    p.db.x("UPDATE schedules SET payload=? WHERE name='svc'", (json.dumps(payload),))
    assert alerts.holds(p.db, "heal:svc", now, now + heal.OUTAGE_S + 2)
    assert alerts.sweep(p.db, now + heal.OUTAGE_S + 2) == []
    assert _episodes(p, "heal:svc")[0]["cleared"] is None


def test_heal_user_override_keeps_its_alert_when_the_project_default_changes(env):
    from ttp import alerts, heal
    p, d, up = _heal_setup(env, fix=None, escalate="user")
    spec, now = _heal_spec(p), time.time()
    _heal_to_self_fix_failed(p, d, up, spec, now)
    heal.run(d, "svc", spec, now + 1)
    p.set_config("heal.escalate", "coordinator")
    d.cfg = p.config()
    heal.run(d, "svc", spec, now + 2)
    assert alerts.sweep(p.db, now + 3) == []
    assert _episodes(p, "heal:svc")[0]["cleared"] is None


def test_heal_escalate_project_default_and_validation(env):
    from ttp import heal
    with pytest.raises(ValueError, match="escalate"):
        heal.validate({"check": "true", "escalate": "chat"})
    p, d, up = _heal_setup(env, fix=None)
    spec = _heal_spec(p)
    d.cfg["heal"] = {"escalate": "coordinator"}
    assert heal.escalate_to(d, spec) == "coordinator"
    assert heal.escalate_to(d, dict(spec, escalate="user")) == "user", "the block's own setting wins"
    now = time.time()
    _heal_to_self_fix_failed(p, d, up, spec, now)
    assert "coordinator told" in heal.run(d, "svc", spec, now + 10)[0]
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert'")
    from ttp import project
    assert project.unknown_key_hint("heal.escalate") is None


def test_heal_fix_that_fails_or_does_not_help_queues_the_task(env):
    from ttp import heal
    p, d, up = _heal_setup(env, fix="echo boom; exit 3")
    spec, now = _heal_spec(p), time.time()
    assert "the fix exited 3" in heal.run(d, "svc", spec, now)[0]
    assert "boom" in p.db.one("SELECT spec FROM tasks WHERE origin='daemon'")["spec"]
    p.db.x("DELETE FROM tasks WHERE origin='daemon'")
    p.db.set_kv(heal.STATE_KEY + "svc", {})
    assert heal.run(d, "svc", dict(spec, fix="true"), now)[0] == "unhealthy (fixed; rechecking)"
    assert "still fails" in heal.run(d, "svc", dict(spec, fix="true"), now + 1)[0]
    p.db.x("DELETE FROM tasks WHERE origin='daemon'")
    p.db.set_kv(heal.STATE_KEY + "svc", {})
    assert "it has no fix" in heal.run(d, "svc", dict(spec, fix=None), now)[0]


def test_heal_suppress_exit_0_skips_the_fix_and_escalation_until_it_says_no(env):
    """A deliberate shutdown: `suppress` exits 0, so nothing is fixed or escalated and the log says so;
    once it exits 1 the still-open episode heals normally."""
    from ttp import heal
    down = env["repo"] / "planned-down"
    down.touch()
    p, d, up = _heal_setup(env, suppress=f"test -e {down}", max_fixes=0)
    spec, now = _heal_spec(p), time.time()
    for t in (now, now + 600):
        assert heal.run(d, "svc", spec, t) == ("unhealthy (suppressed: deliberately down)", None)
    assert not up.exists() and not heal.state(p.db, "svc").get("fixes")
    assert not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")   # max_fixes 0 would escalate at once
    assert "svc: suppressed: deliberately down" in (p.logs / "daemon.log").read_text()
    down.unlink()
    p2, d2, up2 = _heal_setup(env, suppress=f"test -e {down}")
    assert heal.run(d2, "svc", _heal_spec(p2), now)[0] == "unhealthy (fixed; rechecking)"
    assert up2.exists() and "probe broken" not in (p2.logs / "daemon.log").read_text()


def test_heal_suppress_quiets_the_non_outage_unknown_cap_message_until_it_says_no(env):
    from ttp import heal
    down = env["repo"] / "planned-down"
    down.touch()
    p, d, up = _heal_setup(env, grace_s=300, unknown_cap_s=600, escalate="user", suppress=f"test -e {down}")
    spec, now = _heal_spec(p), time.time()
    assert not spec.get("outage")
    dark = dict(spec, check="exit 255")
    heal.run(d, "svc", dark, now)
    for t in (700, 800):
        heal.run(d, "svc", dark, now + t)
    assert _heal_told(p) == ([], [])
    assert "unknown_told" not in heal.state(p.db, "svc")
    assert (p.logs / "daemon.log").read_text().count("svc: suppressed: deliberately down") == 1
    down.unlink()
    assert "told" in heal.run(d, "svc", dark, now + 900)[0]
    msgs, _ = _heal_told(p)
    assert [m["ref"] for m in msgs] == ["heal-unknown:svc"]


@pytest.mark.parametrize("probe", ["exit 7", "no-such-suppress-probe-xyz", "sleep 5"])
def test_heal_broken_or_hung_suppress_heals_normally_and_is_logged(env, monkeypatch, probe):
    from ttp import heal
    monkeypatch.setattr(heal, "SUPPRESS_S", 1)
    p, d, up = _heal_setup(env, suppress=probe)
    assert heal.run(d, "svc", _heal_spec(p), time.time())[0] == "unhealthy (fixed; rechecking)"
    assert up.exists()
    log = (p.logs / "daemon.log").read_text()
    assert "svc: suppress probe broken (exit " in log and "healing normally" in log, log
    assert "timed out" in log if probe.startswith("sleep") else True


def test_heal_suppress_is_validated_text_and_in_the_coordinator_schema(env):
    from ttp import coordinator, heal
    with pytest.raises(ValueError, match="suppress"):
        heal.validate({"check": "false", "suppress": 1})
    assert heal.validate({"check": "false", "suppress": "true"})["suppress"] == "true"
    assert coordinator.HEAL_SCHEMA["properties"]["suppress"] == {"type": "string"}


def test_heal_restart_mid_fix_counts_the_fix_and_rechecks(env):
    from ttp import heal
    p, d, up = _heal_setup(env, max_fixes=1)
    spec, now = _heal_spec(p), time.time()
    # The daemon died while the fix ran: the state says so, the database kept it.
    p.db.set_kv(heal.STATE_KEY + "svc", {"status": "unhealthy", "since": now - 60, "phase": "fixing",
                                         "fix_started": now - 5, "fixes": [now - 5]})
    up.touch()
    assert heal.run(d, "svc", spec, now) == ("fixed (1 today)", None)
    up.unlink()
    assert "cap" in heal.run(d, "svc", spec, now + 1)[0]   # the interrupted fix used the cap
    p.db.x("DELETE FROM tasks WHERE origin='daemon'")
    p.db.set_kv(heal.STATE_KEY + "svc", {"status": "unhealthy", "since": now - 60, "phase": "fixing",
                                         "fixes": [now - 5]})
    assert "still fails" in heal.run(d, "svc", spec, now)[0]


def test_heal_fix_runs_under_its_lock_and_waits_out_a_pause(env):
    from ttp import heal
    from ttp.db import PAUSED_RESOURCES_KEY
    held, up = env["repo"] / "held", env["repo"] / "demo-up"
    p, d, _ = _heal_setup(env, resource="dev-x", check=f"test -e {up}",
                          fix=f'echo "$TTP_LOCKS_HELD" > {held}; touch {up}')
    spec, now = _heal_spec(p), time.time()
    p.db.set_kv(PAUSED_RESOURCES_KEY, {"dev-x": {"reason": "user hold", "since": now}})
    assert heal.run(d, "svc", spec, now)[0] == "unhealthy (fix deferred: dev-x paused)"
    assert heal.run(d, "svc", spec, now + 1800)[0] == "unhealthy (fix deferred: dev-x paused)"   # waits out the pause
    assert not up.exists() and heal.state(p.db, "svc")["fixes"] == []
    assert not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")
    p.db.set_kv(PAUSED_RESOURCES_KEY, {})
    assert heal.run(d, "svc", spec, now + 7210)[0] == "unhealthy (fixed; rechecking)", heal.state(p.db, "svc")
    assert up.exists() and "dev-x" in held.read_text()


def test_heal_fix_that_itself_exits_75_under_a_lock_is_counted(env):
    from ttp import heal
    p, d, up = _heal_setup(env, resource="dev-x", fix="echo own; exit 75", max_fixes=2)
    spec, now = _heal_spec(p), time.time()
    status = heal.run(d, "svc", spec, now)[0]
    assert "the fix exited 75" in status and "self-fix task" in status, status
    st = heal.state(p.db, "svc")
    assert st["fixes"] == [now] and st["fix_rc"] == 75 and "own" in st["fix_out"]
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1


def test_heal_lock_busy_past_window_escalates(env, monkeypatch):
    from ttp import heal
    p, d, up = _heal_setup(env, resource="dev-x", window_h=1)
    spec, now = _heal_spec(p), time.time()
    monkeypatch.setattr(heal, "run_fix", lambda *a: (75, "dev-x stayed busy for 30 s. Hand the task back"))
    assert heal.run(d, "svc", spec, now)[0] == "unhealthy (fix deferred: dev-x busy)"
    assert heal.run(d, "svc", spec, now + 1800)[0] == "unhealthy (fix deferred: dev-x busy)"
    assert heal.state(p.db, "svc")["fixes"] == [] and not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")
    status = heal.run(d, "svc", spec, now + 3600)[0]
    assert "could not get the lock dev-x for 1.0 h" in status, status
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1



def _lock_free(p, res):
    from ttp.project import command_env
    return subprocess.run([sys.executable, "-m", "ttp", "lock", "--probe", res], env=command_env(p),
                          capture_output=True).returncode == 0


def test_heal_detached_fix_holds_its_lock_and_reads_busy_until_it_ends(env):
    """fix_detached: the fix runs as a tracked job past timeout_s, under its resource lock the whole time;
    the check reads 75 (busy) and is not run while it runs, a daemon restart keeps it, and its end settles."""
    from ttp import heal
    from ttp.daemon import Daemon
    held, go, runs = (env["repo"] / n for n in ("held", "go", "runs"))
    p, d, up = _heal_setup(env, resource="dev-x", fix_detached=True, check=f"echo x >> {runs}; test -e {env['repo'] / 'up'}",
                           fix=f'echo "$TTP_LOCKS_HELD" > {held}; while [ ! -e {go} ]; do {_nap(0.1)}; done; '
                               f'touch {env["repo"] / "up"}')
    spec, now = _heal_spec(p), time.time()
    assert spec["fix_timeout_s"] == heal.FIX_TIMEOUT_S > heal.MAX_TIMEOUT_S
    assert heal.run(d, "svc", spec, now) == ("unhealthy (detached fix started; rechecking when it ends)",
                                             now + heal.FIX_POLL_S)
    assert _wait_for(held.exists) and "dev-x" in held.read_text()
    assert not _lock_free(p, "dev-x"), "the detached fix runs under its lock"
    st = heal.state(p.db, "svc")
    assert st["phase"] == "fixing" and st["fixes"] == [now] and d._healing()
    assert heal.check(spec, st, str(p.root))[0] == 75
    checks = runs.read_text().count("x")
    d2 = Daemon(p.base)   # a daemon restart keeps tracking it
    status, again = heal.run(d2, "svc", spec, now + 600)
    assert status.startswith("unhealthy (detached fix running 600 s") and again == now + 600 + heal.FIX_POLL_S
    assert runs.read_text().count("x") == checks, "the check never runs under its fix"
    go.touch()
    assert _wait_for(lambda: pathlib.Path(heal.state(p.db, "svc")["fix_job"]["rc"]).exists())
    assert _wait_for(lambda: _lock_free(p, "dev-x")), "the lock goes with the fix"
    assert heal.run(d2, "svc", spec, now + 700) == ("unhealthy (fixed; rechecking)", now + 700)
    st = heal.state(p.db, "svc")
    assert "fix_job" not in st and st["fix_rc"] == 0 and st["fixes"] == [now]
    assert heal.run(d2, "svc", spec, now + 701)[0] == "fixed (1 today)"


def test_heal_detached_fix_past_its_timeout_is_killed_and_escalates(env):
    from ttp import detach, heal
    p, d, up = _heal_setup(env, fix_detached=True, fix_timeout_s=5, fix="sleep 120")
    spec, now = _heal_spec(p), time.time()
    assert heal.run(d, "svc", spec, now)[0].startswith("unhealthy (detached fix started")
    rc = pathlib.Path(heal.state(p.db, "svc")["fix_job"]["rc"])
    assert heal.run(d, "svc", spec, now + 2)[1] == now + 5   # comes back at its deadline
    status = heal.run(d, "svc", spec, now + 6)[0]
    assert "the fix exited 124" in status and "self-fix task" in status, status
    assert _wait_for(lambda: detach.state(rc)[0] != "running"), "its process group was killed"
    st = heal.state(p.db, "svc")
    assert "fix_job" not in st and st["fix_rc"] == 124 and "fix_timeout_s" in st["fix_out"]


def test_heal_detached_fix_gone_without_an_exit_code_is_a_failed_fix(env):
    from ttp import heal
    p, d, up = _heal_setup(env, fix_detached=True)
    spec, now = _heal_spec(p), time.time()
    job = heal.fix_dir(p, "svc") / "fix.rc"
    job.parent.mkdir(parents=True)
    (job.parent / "fix.started").write_text("1\n")   # it started, then a reboot or a kill ended it
    p.db.set_kv(heal.STATE_KEY + "svc", {"status": "unhealthy", "since": now - 60, "phase": "fixing",
                                         "fixes": [now - 30], "fix_job": {"rc": str(job), "pid": 0,
                                                                          "started": now - 30, "fixes": []}})
    status = heal.run(d, "svc", spec, now)[0]
    assert "the fix exited 1" in status, status
    st = heal.state(p.db, "svc")
    assert "without an exit code" in st["fix_out"] and "fix_job" not in st and st["fixes"] == [now - 30]   # it counts


def test_heal_fix_timeout_s_needs_fix_detached_and_is_capped():
    from ttp import heal
    with pytest.raises(ValueError, match="fix_detached"):
        heal.validate({"check": "true", "fix": "true", "fix_timeout_s": 600})
    assert heal.validate({"check": "true", "fix_detached": True, "fix_timeout_s": 10 ** 6})["fix_timeout_s"] \
        == heal.FIX_MAX_TIMEOUT_S
    assert "fix_timeout_s" not in heal.validate({"check": "true", "fix": "true"})
    with pytest.raises(ValueError, match="fix_detached"):
        heal.validate({"check": "true", "fix_detached": "yes"})

def _heal_told(p):
    """What a heal check has told anyone: user alerts and lines, asks, and coordinator observations."""
    return (p.db.q("SELECT kind, severity, ref, text FROM messages WHERE kind IN ('alert','info','ask') ORDER BY id"),
            p.db.q("SELECT fingerprint, severity, text FROM events WHERE kind='observation' ORDER BY id"))


@pytest.mark.parametrize("escalate", ["user", "coordinator"])
def test_heal_unknown_check_without_outage_escalates_once_per_stretch_past_its_cap(env, escalate):
    from ttp import heal
    p, d, up = _heal_setup(env, grace_s=300, escalate=escalate)
    spec, now = _heal_spec(p), time.time()
    assert spec["unknown_cap_s"] == 3600
    dark = dict(spec, check="echo probing; echo 'dev-x is paused (bench work)'; exit 75")
    for t in (0, 30 * 60, 59 * 60):
        assert heal.run(d, "svc", dark, now + t) == ("ok (unknown, rc 75)", None)
    assert _heal_told(p) == ([], [])

    def stretch(start, n):
        status = heal.run(d, "svc", dark, start + 61 * 60)[0]
        assert status == f"ok (unknown, rc 75; {escalate} told)", status
        heal.run(d, "svc", dark, start + 180 * 60)   # the same stretch: nothing more
        msgs, events = _heal_told(p)
        if escalate == "user":
            assert events == [] and [(m["kind"], m["severity"], m["ref"]) for m in msgs] == \
                [("alert", "normal", "heal-unknown:svc")] * n
            text = msgs[-1]["text"]
        else:
            assert msgs == [] and [(e["fingerprint"], e["severity"]) for e in events][-1] == \
                (f"heal:svc:unknown:{start:.0f}", "high") and len(events) == n
            text = events[-1]["text"]
        assert "could not tell for 61 min (exit 75: dev-x is paused (bench work))" in text and "probing" in text
        assert not up.exists() and not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")

    stretch(now, 1)
    # A healthy result ends the stretch; a new one can escalate again.
    up.touch()
    assert heal.run(d, "svc", spec, now + 200 * 60) == ("ok (healthy)", None)
    up.unlink()
    assert heal.run(d, "svc", dark, now + 201 * 60)[0] == "ok (unknown, rc 75)"
    stretch(now + 201 * 60, 2)
    # So does an unhealthy one (here within its grace).
    assert heal.run(d, "svc", spec, now + 400 * 60)[0] == "unhealthy (0 s, grace 300 s)"
    assert "unknown_told" not in heal.state(p.db, "svc")
    assert heal.run(d, "svc", dark, now + 401 * 60)[0] == "ok (unknown, rc 75)"
    stretch(now + 401 * 60, 3)


def test_heal_check_marks_a_known_owned_state_with_one_output_line():
    from ttp import heal
    assert heal.known_run(75, "probing\nttp-heal: known recovery: power work on the box\n") == \
        ("recovery", "power work on the box")
    assert heal.known_run(1, "  ttp-heal: known the user: resource paused (bench)  ") == \
        ("the user", "resource paused (bench)")
    assert heal.known_run(255, "ttp-heal: known recovery: x") is None, "only on exit 75 or 1"
    assert heal.known_run(0, "ttp-heal: known recovery: x") is None
    assert heal.known_run(75, "ttp-heal: known recovery:") is None, "it needs a reason"
    assert heal.known_run(75, "ttp-heal: known : why") is None, "and an owner"
    assert heal.known_run(75, "it says ttp-heal: known recovery: x inline") is None, "only a line of its own counts"


def test_heal_known_owned_runs_never_tell_fix_or_queue_and_a_run_without_the_line_is_unknown_again(env):
    from ttp import heal
    p, d, up = _heal_setup(env, grace_s=300, unknown_cap_s=3600, escalate="coordinator")
    spec, now = _heal_spec(p), time.time()
    owned = dict(spec, check="echo 'ttp-heal: known recovery: power work on the box'; exit 75")
    for t in (0, 61 * 60, 300 * 60):
        assert heal.run(d, "svc", owned, now + t) == ("ok (known, recovery: power work on the box)", None)
    assert heal.describe({"name": "svc"}, spec, p.db, now + 300 * 60).startswith(
        "svc: known (recovery: power work on the box);")
    assert heal.summary(p.db, now + 300 * 60)["unknown"] == 1
    # Exit 1 with the line: past its grace, still no fix and no task.
    bad = dict(spec, check="echo 'ttp-heal: known the user: resource paused'; exit 1")
    assert heal.run(d, "svc", bad, now + 310 * 60)[0] == "ok (known, the user: resource paused)"
    assert heal.run(d, "svc", bad, now + 320 * 60)[0] == "ok (known, the user: resource paused)"
    assert _heal_told(p) == ([], []) and not up.exists() and not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")
    # A run without the line starts the ordinary unknown stretch; a known run in it ends that stretch.
    dark = dict(spec, check="exit 75")
    t0 = now + 400 * 60
    assert heal.run(d, "svc", dark, t0) == ("ok (unknown, rc 75)", None)
    assert "known_run" not in heal.state(p.db, "svc")
    assert heal.run(d, "svc", owned, t0 + 50 * 60)[0].startswith("ok (known")
    assert heal.run(d, "svc", dark, t0 + 61 * 60) == ("ok (unknown, rc 75)", None)
    assert _heal_told(p) == ([], [])
    status = heal.run(d, "svc", dark, t0 + 122 * 60)[0]
    assert status == "ok (unknown, rc 75; coordinator told)", status
    assert [e["fingerprint"] for e in _heal_told(p)[1]] == [f"heal:svc:unknown:{t0 + 61 * 60:.0f}"]


@pytest.mark.parametrize("exemption", ["owner line", "known outage", "known fault"])
def test_heal_no_exemption_covers_automatic_recovery_switched_off(env, exemption):
    """An override that switches a service's last automatic recovery step off must surface, whoever owns the
    machine: neither an owner line, a known machine outage nor a known_fault keeps it quiet. The same
    exemption still quiets a result without the recovery-off line."""
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    kf = {"known_fault": "handed to the backup owner"} if exemption == "known fault" else {}
    p, d, up = _heal_setup(env, fix=None, machine="box-a", escalate="coordinator", **kf)
    spec, now = _heal_spec(p), time.time()
    owner = "echo 'ttp-heal: known backup-owner: recovery drop-in on purpose'; " if exemption == "owner line" else ""
    if exemption == "known outage":
        ml.set_outage("box-a", "repair handed to the backup owner", "user", "2d", now=now)
    quiet = dict(spec, check=f"{owner}echo 'auto power cycle is off'; exit 1")
    off = dict(spec, check=f"{owner}echo 'auto power cycle is off'; echo '{heal.RECOVERY_OFF}'; exit 75")
    assert "known" in heal.run(d, "svc", quiet, now)[0]
    assert not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")
    p.db.set_kv(heal.STATE_KEY + "svc", {})
    assert heal.classify(75, f"ttp-heal: known x: y\n{heal.RECOVERY_OFF}") == "unhealthy, automatic recovery off"
    for t in (0, 3600, 86400):   # a day of it: escalated on the first run past grace, never kept quiet
        out = heal.run(d, "svc", off, now + 1 + t)[0]
        assert "known" not in out and "quiet" not in out, out
    assert heal.state(p.db, "svc")["status"] == "unhealthy"
    assert len(p.db.q("SELECT id FROM tasks WHERE origin='daemon'")) == 1


def test_heal_broker_preset_reads_the_services_own_recovery_off_report(env, monkeypatch):
    """The config flags may read on while the service reports at runtime that a recovery step is disabled:
    that report counts as recovery off."""
    from ttp import heal
    status = {}
    monkeypatch.setattr(heal, "_shell", lambda cmd, cwd, timeout: (0, json.dumps(status)))
    bk = heal.validate({"preset": "broker", "status_command": "status", "recovery_off_field": "recovery.disabled"})
    flags = {"held": False, "auto_power_cycle": True, "auto_reboot": True}
    for events, off in (([], False), (0, False), (None, False), (["power_cycle disabled"], True), (2, True),
                        (True, True), ("false", False), ("none", False), ("", False), ({}, False)):
        status.clear()
        status.update(flags, recovery={"disabled": events})
        rc, out = heal.check(bk, {}, ".")
        assert (rc, heal.recovery_off(out)) == ((1, True) if off else (0, False)), (events, out)
    status.update(flags, auto_reboot=False, recovery={})
    rc, out = heal.check(bk, {}, ".")
    assert rc == 1 and heal.recovery_off(out) and "auto_reboot is off" in out


def test_heal_known_fault_bookkeeping_skips_a_recovery_off_result(env):
    """known_key/known_since are set only for a result without the recovery-off line (`kf and not off`)."""
    from ttp import heal
    p, d, up = _heal_setup(env, fix=None, escalate="coordinator", known_fault="handed to the backup owner")
    spec, now = _heal_spec(p), time.time()
    off = dict(spec, check=f"echo x; echo '{heal.RECOVERY_OFF}'; exit 1")
    heal.run(d, "svc", off, now)
    heal.run(d, "svc", off, now + 3600)
    assert "known_key" not in heal.state(p.db, "svc")
    heal.run(d, "svc", dict(spec, check="echo x; exit 1"), now + 7200)
    assert "known_key" in heal.state(p.db, "svc")


def test_heal_check_command_exits_1_for_recovery_off(env, capsys):
    """`ttp heal check` agrees with the daemon: a recovery-off run is unhealthy whatever its exit code."""
    from ttp import cli, heal
    assert heal.classify(75, heal.RECOVERY_OFF) == "unhealthy, automatic recovery off"
    src = open(cli.__file__).read()
    assert "sys.exit(1 if heal.recovery_off(out) else" in src


def test_heal_project_pause_still_quiets_a_recovery_off_result(env):
    """Choice: a pause the project set on the check's own resource quiets a recovery-off result too."""
    from ttp import coordinator as coord
    from ttp import heal
    p, d, up = _heal_setup(env, fix=None, escalate="coordinator", outage=True, resource="box-a-device")
    coord.pause_resource(p, "box-a-device", True, reason="maintenance", by="user")
    spec, now = _heal_spec(p), time.time()
    off = dict(spec, check=f"echo x; echo '{heal.RECOVERY_OFF}'; exit 1", resource="box-a-device", outage=True)
    for t in (0, 3600, 86400):
        heal.run(d, "svc", off, now + t)
    assert not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")


def test_heal_outage_check_known_owned_runs_never_escalate_as_an_outage(env):
    from ttp import heal
    p, d, up = _heal_setup(env, outage=True, grace_s=600)
    spec, now = _heal_spec(p), time.time()
    owned = dict(spec, check="echo 'ttp-heal: known recovery: handed to the recovery owner'; exit 75")
    for t in (0, 30 * 60, 120 * 60):
        assert heal.run(d, "svc", owned, now + t)[0] == "ok (known, recovery: handed to the recovery owner)"
    assert _heal_told(p) == ([], []) and not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")
    # Without the line the outage window counts from the first unknown run.
    dark = dict(spec, check="exit 75")
    assert heal.run(d, "svc", dark, now + 130 * 60) == ("ok (unknown, rc 75)", now + 130 * 60 + 600)
    assert "could not tell for 10 min" in heal.run(d, "svc", dark, now + 140 * 60)[0]
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1


def test_heal_unknown_cap_is_validated_and_zero_turns_it_off(env):
    from ttp import heal
    with pytest.raises(ValueError, match="unknown_cap_s"):
        heal.validate({"check": "true", "unknown_cap_s": -1})
    p, d, up = _heal_setup(env, unknown_cap_s=0)
    spec, now = _heal_spec(p), time.time()
    for t in (0, 3600, 10 * 3600):
        assert heal.run(d, "svc", dict(spec, check="exit 255"), now + t) == ("ok (unknown, rc 255)", None)
    assert _heal_told(p) == ([], [])


def test_heal_outage_check_unknown_keeps_its_outage_escalation_not_the_unknown_cap(env):
    from ttp import heal
    p, d, up = _heal_setup(env, outage=True, grace_s=3000, unknown_cap_s=600)
    spec, now = _heal_spec(p), time.time()
    dark = dict(spec, check="exit 75")
    assert heal.run(d, "svc", dark, now) == ("ok (unknown, rc 75)", now + 3000)
    assert heal.run(d, "svc", dark, now + 11 * 60) == ("ok (unknown, rc 75)", now + 3000)
    assert _heal_told(p) == ([], [])
    assert "could not tell for 50 min" in heal.run(d, "svc", dark, now + 3000)[0]
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1
    assert _heal_told(p) == ([], []) and "unknown_told" not in heal.state(p.db, "svc")


@pytest.mark.parametrize("by", ["user", "coordinator"])
def test_heal_fix_deferred_on_a_pause_past_the_cap_tells_once_by_who_set_it(env, monkeypatch, by):
    from ttp import heal
    from ttp.db import PAUSED_RESOURCES_KEY
    p, d, up = _heal_setup(env, resource="dev-x")
    spec, now = _heal_spec(p), time.time()
    pause = {"dev-x": {"reason": "bench work", "since": now - 600, "by": by}}
    p.db.set_kv(PAUSED_RESOURCES_KEY, pause)
    monkeypatch.setattr(heal, "run_fix", lambda *a: (75, "dev-x is paused (bench work); hand the task back"))
    for t in (0, 30 * 60, 59 * 60):
        assert heal.run(d, "svc", spec, now + t)[0] == "unhealthy (fix deferred: dev-x paused)"
    assert _heal_told(p) == ([], [])
    who = "user's pause: told" if by == "user" else "coordinator told"
    assert heal.run(d, "svc", spec, now + 61 * 60)[0] == f"unhealthy (fix deferred: dev-x paused; {who})"
    for t in (120, 600):   # the same pause: nothing more
        assert heal.run(d, "svc", spec, now + t * 60)[0] == "unhealthy (fix deferred: dev-x paused)"
    msgs, events = _heal_told(p)
    if by == "user":
        assert events == [] and [(m["kind"], m["severity"], m["ref"]) for m in msgs] == [("info", "low", "heal:svc")]
        text = msgs[0]["text"]
        assert "paused by the user (bench work)" in text
    else:
        assert msgs == [] and [(e["fingerprint"], e["severity"]) for e in events] == [(f"heal:svc:paused:{now:.0f}", "high")]
        text = events[0]["text"]
        assert "paused by coordinator (bench work)" in text
    assert "waited 1.0 h on dev-x" in text and f"Lifting the pause lets its fix run: touch {up}" in text
    assert not p.db.q("SELECT id FROM tasks WHERE origin='daemon'") and heal.state(p.db, "svc")["fixes"] == []
    assert p.db.kv(PAUSED_RESOURCES_KEY) == pause   # never lifted here
    # The fix running ends the stretch; a later pause stretch can tell again.
    monkeypatch.setattr(heal, "run_fix", lambda *a: (0, "fixed"))
    assert heal.run(d, "svc", spec, now + 700 * 60)[0] == "unhealthy (fixed; rechecking)"
    assert not {"deferred_since", "deferred_on", "deferred_told"} & set(heal.state(p.db, "svc"))
    monkeypatch.setattr(heal, "run_fix", lambda *a: (75, "dev-x is paused (bench work); hand the task back"))
    st = heal.state(p.db, "svc")
    p.db.set_kv(heal.STATE_KEY + "svc", {k: v for k, v in st.items() if k not in ("phase", "fixes")})
    assert heal.run(d, "svc", spec, now + 701 * 60)[0] == "unhealthy (fix deferred: dev-x paused)"
    assert heal.run(d, "svc", spec, now + 762 * 60)[0].endswith(f"; {who})")
    assert sum(map(len, _heal_told(p))) == 2


def test_heal_paused_then_busy_lock_restarts_the_deferral_clock(env, monkeypatch):
    from ttp import heal
    p, d, up = _heal_setup(env, resource="dev-x", window_h=1)
    spec, now = _heal_spec(p), time.time()
    monkeypatch.setattr(heal, "run_fix", lambda *a: (75, "dev-x is paused; hand the task back"))
    heal.run(d, "svc", spec, now)
    monkeypatch.setattr(heal, "run_fix", lambda *a: (75, "dev-x stayed busy for 30 s. Hand the task back"))
    assert heal.run(d, "svc", spec, now + 3000)[0] == "unhealthy (fix deferred: dev-x busy)"
    assert heal.run(d, "svc", spec, now + 3700)[0] == "unhealthy (fix deferred: dev-x busy)"
    assert "could not get the lock" in heal.run(d, "svc", spec, now + 3000 + 3600)[0]


def test_heal_timeout_is_capped_below_the_daemon_watchdog(env):
    from ttp import daemon as dm
    from ttp import heal
    assert heal.validate({"check": "true", "timeout_s": 100000})["timeout_s"] == heal.MAX_TIMEOUT_S
    assert heal.validate({"check": "true", "timeout_s": 30})["timeout_s"] == 30
    assert heal.MAX_TIMEOUT_S == dm.WATCHER_MAX_S
    assert heal.MAX_TIMEOUT_S + heal.LOCK_WAIT_S + 10 < dm.WATCHDOG_S   # the longest single segment


def test_heal_timeout_kills_the_whole_process_group(env, tmp_path):
    from ttp import heal
    pidf = tmp_path / "group.pid"
    # the check's shell records its pid, its process group's id (a session of its own); on a busy machine the
    # timeout can fire before the shell wrote it, so retry with a longer one
    for timeout in (1, 5, 15):
        pidf.unlink(missing_ok=True)
        t0 = time.time()
        rc, out = heal._exec(f"sleep 60 & echo $$ > {pidf}.tmp; mv {pidf}.tmp {pidf}; wait", str(tmp_path), timeout)
        assert rc == 124 and "timed out" in out and time.time() - t0 < timeout + 10
        if pidf.exists():
            break
    group = int(pidf.read_text())

    def gone() -> bool:
        """No process is left in the group but zombies (killed, not yet reaped). Portable: no /proc."""
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return True
        ps = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True, timeout=30).stdout
        return all(st.startswith("Z") for g, st in (line.split(None, 1) for line in ps.splitlines() if line.strip())
                   if int(g) == group)
    deadline = time.time() + 30   # generous: reaping an orphan can lag on a busy machine
    while not gone() and time.time() < deadline:
        time.sleep(0.1)
    assert gone(), "the check's child outlived its timeout"


def test_heal_step_pings_the_watchdog_between_check_and_fix_and_skips_own_command_after_a_fix(env):
    from ttp import heal
    trail, ran = env["repo"] / "trail", env["repo"] / "own-ran"
    p, d, up = _heal_setup(env, check=f"echo check >> {trail}; test -e {env['repo'] / 'never'}",
                           fix=f"echo fix >> {trail}")
    pings = []
    d._progress = lambda: pings.append(trail.read_text())
    payload = {"command": f"touch {ran}", "heal": json.loads(
        p.db.one("SELECT payload FROM schedules WHERE name='svc'")["payload"])["heal"]}
    status, again = d._run_heal(dict(p.db.one("SELECT * FROM schedules WHERE name='svc'")), payload)
    assert status == "unhealthy (fixed; rechecking)" and again is not None
    assert pings[0] == "check\n" and pings[-1] == "check\nfix\n", pings
    assert not ran.exists()   # the own command waits for the recheck
    d._run_heal(dict(p.db.one("SELECT * FROM schedules WHERE name='svc'")), payload)
    assert ran.exists()   # the recheck failed and escalated: no fix this step, so the own command ran


def test_heal_restart_mid_fix_still_waits_settle(env):
    from ttp import heal
    p, d, up = _heal_setup(env, settle_s=60)
    spec, now = _heal_spec(p), time.time()
    p.db.set_kv(heal.STATE_KEY + "svc", {"status": "unhealthy", "since": now - 120, "phase": "fixing",
                                         "fix_started": now - 5, "fixes": [now - 5]})
    up.touch()
    assert heal.run(d, "svc", spec, now) == ("unhealthy (fix interrupted; rechecking)", now + 55)
    assert "last_check" not in heal.state(p.db, "svc")   # no check ran early
    assert heal.run(d, "svc", spec, now + 56) == ("fixed (1 today)", None)


def test_heal_outage_check_that_stays_unknown_escalates_without_fixing(env):
    from ttp import alerts, heal
    p, d, up = _heal_setup(env, outage=True, grace_s=600)
    spec, now = _heal_spec(p), time.time()
    dark = dict(spec, check="exit 75")   # unknown, but not only its route (255: its own test)
    assert heal.run(d, "svc", dark, now) == ("ok (unknown, rc 75)", now + 600)
    assert heal.run(d, "svc", dark, now + 300)[0] == "ok (unknown, rc 75)"
    assert not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")
    status = heal.run(d, "svc", dark, now + 600)[0]
    assert "could not tell for 10 min (exit 75)" in status and not up.exists(), status
    t = p.db.one("SELECT * FROM tasks WHERE origin='daemon'")
    assert json.loads(t["labels"]) == ["heal:svc"] and heal.summary(p.db)["failing"] == 1
    assert heal.run(d, "svc", dark, now + 900)[0] == f"unhealthy (self-fix task #{t['id']} open)"
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert'")
    p.db.update_task(t["id"], status="failed")
    assert "alerted" in heal.run(d, "svc", dark, now + 1200)[0] and not up.exists()
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert' AND ref='heal:svc'")) == 1
    up.touch()
    assert heal.run(d, "svc", spec, now + 1500) == ("ok (healthy)", None)
    assert [e["key"] for e in alerts.sweep(p.db)] == ["heal:svc"]
    st = heal.state(p.db, "svc")
    assert st["status"] == "healthy" and not {"unknown_since", "task", "escalated", "alerted"} & set(st)
    # Without outage, a check that stays unknown never fixes; past unknown_cap_s it only says so once.
    plain = dict(spec, outage=False, check="exit 255")
    p.db.set_kv(heal.STATE_KEY + "svc", {})
    up.unlink()
    assert heal.run(d, "svc", plain, now + 2000) == ("ok (unknown, rc 255)", None)
    for dt in (3600, 10 * 3600):
        assert heal.run(d, "svc", plain, now + 2000 + dt)[0].startswith("ok (unknown, rc 255")
    assert not up.exists() and p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1


def test_heal_known_fault_stays_quiet_unless_a_whole_box_is_out(env):
    from ttp import heal
    p, d, up = _heal_setup(env, known_fault="repair is the user's call", grace_s=7200)
    spec, now = _heal_spec(p), time.time()
    assert heal.run(d, "svc", spec, now)[0] == "unhealthy (known fault: kept quiet)"
    assert heal.run(d, "svc", spec, now + 3 * 3600)[0] == "unhealthy (known fault: kept quiet)"
    p.db.set_kv(heal.STATE_KEY + "svc", {})
    box = dict(spec, outage=True)
    assert heal.run(d, "svc", box, now)[0] == "unhealthy (known fault: kept quiet)"
    assert heal.run(d, "svc", box, now + heal.OUTAGE_S)[0] == "unhealthy (fixed; rechecking)"


def test_heal_known_fault_object_is_validated_and_strings_still_load(env):
    from datetime import datetime, timezone
    from ttp import heal
    from ttp import schedule as sched
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat()
    assert heal.validate({"check": "true", "known_fault": "vendor bug"})["known_fault"] == "vendor bug"
    ok = [{"name": "svc", "kind": "command", "every": "5m",
           "payload": {"heal": {"check": "true", "known_fault": "vendor bug"}}}]
    assert sched.parse_file(json.dumps(ok))
    kf = {"why": "vendor bug", "match": "err [0-9]+", "until": iso(time.time() + 6 * 86400)}
    assert heal.validate({"check": "true", "known_fault": kf})["known_fault"] == kf
    assert heal.validate({"check": "true", "known_fault": {"why": "vendor bug"}})
    for bad, msg in [({"why": "x", "owner": "y"}, "unknown key"), ({"match": "x"}, "needs `why`"),
                     ({"why": " "}, "needs `why`"), ({"why": "x", "match": "("}, "not a valid regex"),
                     ({"why": "x", "match": 3}, "is text"), ({"why": "x", "until": "soon"}, "ISO time"),
                     ({"why": "x", "until": iso(time.time() + 8 * 86400)}, "at most 7 d"), (["x"], "text or an object")]:
        with pytest.raises(ValueError, match=msg):
            heal.validate({"check": "true", "known_fault": bad})


@pytest.mark.parametrize("fix", [True, False])
def test_heal_known_fault_match_keeps_only_matching_failures_quiet(env, fix):
    from ttp import heal
    p, d, up = _heal_setup(env, known_fault={"why": "vendor bug", "match": "err [0-9]+: vendor"},
                          **({} if fix else {"fix": None}))
    spec, now = dict(_heal_spec(p), check=f"test -e {up} || {{ echo 'err 42: vendor fault'; exit 1; }}"), time.time()
    assert heal.run(d, "svc", spec, now)[0] == "unhealthy (known fault: kept quiet)"
    assert not up.exists() and not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")
    other = dict(spec, known_fault={"why": "vendor bug", "match": "power lost"})
    p.db.set_kv(heal.STATE_KEY + "svc", {})
    out = heal.run(d, "svc", other, now)[0]
    if fix:
        assert out == "unhealthy (fixed; rechecking)" and up.exists()
    else:
        assert "self-fix task" in out and len(p.db.q("SELECT id FROM tasks WHERE origin='daemon'")) == 1


@pytest.mark.parametrize("form", ["string", "until"])
def test_heal_known_fault_ends_escalates_and_asks_renewal_once(env, form):
    from datetime import datetime, timezone
    from ttp import heal
    now = time.time()
    end = now + 3600 if form == "until" else now + heal.KNOWN_FAULT_S
    kf = ("vendor bug" if form == "string"
          else {"why": "vendor bug", "until": datetime.fromtimestamp(end, timezone.utc).isoformat()})
    p, d, up = _heal_setup(env, fix=None, known_fault=kf)
    spec = _heal_spec(p)
    renewals = lambda: p.db.q("SELECT * FROM messages WHERE direction='out' AND text LIKE '%known_fault (vendor bug) ended%'")
    observed = lambda: p.db.q("SELECT * FROM events WHERE kind='observation' AND fingerprint LIKE 'heal:svc:known_fault:%'")
    assert heal.run(d, "svc", spec, now)[0] == "unhealthy (known fault: kept quiet)"
    assert heal.state(p.db, "svc")["known_since"] == now
    # A healthy spell keeps its end: a flapping fault never renews itself.
    up.touch()
    assert heal.run(d, "svc", spec, now + 60)[0] == "ok (healthy)"
    up.unlink()
    assert heal.run(d, "svc", spec, end - 60)[0] == "unhealthy (known fault: kept quiet)"
    assert not renewals() and heal.holds(p.db, "svc", end - 60) is False
    out = heal.run(d, "svc", spec, end + 1)[0]
    assert "self-fix task" in out, out
    assert len(renewals()) == 1 and renewals()[0]["severity"] == "low"
    assert len(observed()) == 1 and observed()[0]["severity"] == "low"
    tid = heal.state(p.db, "svc")["task"]
    p.db.update_task(tid, status="failed")
    assert heal.run(d, "svc", spec, end + 120)[0] == f"unhealthy (self-fix task #{tid} failed; alerted)"
    assert len(renewals()) == 1 and len(observed()) == 1
    # Renewing (a changed entry) keeps it quiet again.
    renewed = dict(spec, known_fault={"why": "vendor bug, renewed"})
    assert heal.run(d, "svc", renewed, end + 180)[0] == "unhealthy (known fault: kept quiet)"


@pytest.mark.parametrize("via", ["machine", "resource", "outage_unknown"])
def test_heal_known_machine_outage_keeps_the_check_quiet_then_grace_after_it(env, via):
    """Replay: a check on a box with a known `ttp machines outage` opened a self-fix task; once the outage
    ended a check still failing went straight to escalation with no grace."""
    from ttp import heal, timefmt
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    block = {"machine": "box-a"} if via == "machine" else {"resource": "box-a-device"}
    if via == "outage_unknown":
        block = {"resource": "box-a-device", "outage": True, "check": "exit 75"}
    p, d, up = _heal_setup(env, fix=None, grace_s=600, **block)
    spec, now = _heal_spec(p), time.time()
    tid = _heal_to_self_fix_failed(p, d, up, dict(spec, check=f"test -e {up}", grace_s=0), now)   # an earlier episode
    rec = ml.set_outage("box-a", "hardware repair", "user", "2h", now=now)
    until = timefmt.long(rec["until"], p)
    if via == "outage_unknown":   # unknown within its window, then quiet past it instead of escalating
        assert heal.run(d, "svc", spec, now + 30)[0] == "ok (unknown, rc 75)"
    for dt in (660, 3600, 7000):
        assert heal.run(d, "svc", spec, now + dt)[0] == f"unhealthy (known outage until {until})"
    st = heal.state(p.db, "svc")
    assert st["known_outage"] == "box-a" and not {"task", "escalated", "alerted", "observed"} & set(st)
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert'") and not up.exists()
    assert p.db.task(tid)["status"] == "failed"
    # The outage ends and the box still fails: a fresh episode with its grace, not an escalation.
    ml.clear_outage("box-a")
    t1 = now + 7300
    first = heal.run(d, "svc", spec, t1)[0]
    if via == "outage_unknown":
        assert first == "ok (unknown, rc 75)"
    else:
        assert first == "unhealthy (0 s, grace 600 s)"
    assert "self-fix task" in heal.run(d, "svc", spec, t1 + 600)[0]
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 2


def test_heal_known_outage_end_keeps_a_still_open_self_fix_task(env):
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, up = _heal_setup(env, fix=None, grace_s=0, machine="box-a")
    spec, now = _heal_spec(p), time.time()
    ml.set_outage("box-a", "hardware repair", "user", "2h", now=now)
    heal.run(d, "svc", spec, now + 10)
    tid = p.db.add_task("open self-fix", kind="code") if hasattr(p.db, "add_task") else None
    assert tid
    st = heal.state(p.db, "svc")
    st["task"] = tid
    heal._save(p.db, "svc", st)
    ml.clear_outage("box-a")
    heal.run(d, "svc", spec, now + 60)
    heal.run(d, "svc", spec, now + 120)
    assert heal.state(p.db, "svc")["task"] == tid
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] <= 1


@pytest.mark.parametrize("escalate", ["user", "coordinator"])
def test_heal_unknown_check_under_a_known_machine_outage_tells_no_one(env, escalate):
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, up = _heal_setup(env, machine="box-a", unknown_cap_s=600, escalate=escalate)
    spec, now = _heal_spec(p), time.time()
    dark = dict(spec, check="exit 255")
    ml.set_outage("box-a", "hardware repair", "user", "4h", now=now)
    for t in (0, 11 * 60, 3600, 3 * 3600):
        assert heal.run(d, "svc", dark, now + t) == ("ok (unknown, rc 255)", None)
    assert _heal_told(p) == ([], []) and "unknown_told" not in heal.state(p.db, "svc")
    # The outage over, the same stretch says so once past its cap again.
    ml.clear_outage("box-a")
    assert heal.run(d, "svc", dark, now + 4 * 3600 + 60) == ("ok (unknown, rc 255)", None)
    assert "told" in heal.run(d, "svc", dark, now + 4 * 3600 + 60 + 601)[0]
    assert sum(map(len, _heal_told(p))) == 1


@pytest.mark.parametrize("block", [{"machine": "box-b"}, {"resource": "box-ab-device"}, {"resource": "box-a2"}])
def test_heal_outage_on_another_machine_does_not_quiet_the_check(env, block):
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    mm.add("box-b", tags="device")
    p, d, up = _heal_setup(env, fix=None, unknown_cap_s=600, escalate="coordinator", **block)
    spec, now = _heal_spec(p), time.time()
    ml.set_outage("box-a", "hardware repair", "user", "4h", now=now)
    assert heal.known_outage(spec, now) is None
    heal.run(d, "svc", dict(spec, check="exit 255"), now)
    assert "coordinator told" in heal.run(d, "svc", dict(spec, check="exit 255"), now + 601)[0]
    assert "it has no fix" in heal.run(d, "svc", spec, now + 700)[0]
    assert "known_outage" not in heal.state(p.db, "svc")


@pytest.mark.parametrize("finding", ["chip", "unknown"])
def test_heal_outage_with_covers_quiets_only_the_findings_it_explains(env, finding):
    """[note f7b7938ee8de232f] An outage recorded for one service (`--covers demo-serve`) quieted every unhealthy
    result on the machine, so a chip loss went unnoticed for hours. Only a check whose identity names a
    covered part stays quiet."""
    from ttp import heal, timefmt
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, up = _heal_setup(env, fix=None, unknown_cap_s=600, escalate="coordinator", machine="box-a")
    spec, now = _heal_spec(p), time.time()
    rec = ml.set_outage("box-a", "service off for testing", "user", "4h", now=now, covers="DEMO-SERVE")
    assert rec["covers"] == ["demo-serve"]
    until = timefmt.long(rec["until"], p)
    # The covered service's own check stays quiet, as under a whole-machine outage.
    serve = dict(spec, unit="demo-serve.service", check="echo 'demo-serve service inactive'; exit 1")
    assert heal.outage_explains(rec, "svc", serve, 1, "demo-serve service inactive")
    assert heal.run(d, "svc", serve, now)[0] == f"unhealthy (known outage until {until})"
    assert heal.state(p.db, "svc")["known_outage"] == "box-a"
    if finding == "chip":   # a chip loss is not explained: it escalates as usual
        lost = dict(spec, check="echo 'chip 0 missing from the device list'; exit 1")
        assert not heal.outage_explains(rec, "svc", lost, 1, "chip 0 missing from the device list")
        assert "self-fix task" in heal.run(d, "svc", lost, now + 60)[0]
        assert "known_outage" not in heal.state(p.db, "svc")
        assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1
    else:   # an unreachable box is not explained by one service either: its long stretch is told once
        dark = dict(spec, check="exit 255")
        heal.run(d, "svc", dark, now + 60)
        assert "coordinator told" in heal.run(d, "svc", dark, now + 60 + 601)[0]
        # An outage covering the host explains it: nothing more is told.
        ml.set_outage("box-a", "host repair", "user", "4h", now=now, covers="host")
        assert heal.outage_explains(ml.outages()["box-a"], "svc", dark, 255, "")
        assert heal.run(d, "svc", dark, now + 900) == ("ok (unknown, rc 255)", None)
        assert heal.state(p.db, "svc")["known_outage"] == "box-a"
    # Covered words match whole words of the identity (a plural too), never inside another word.
    chip = {"covers": ["chip"]}
    assert heal.outage_explains(chip, "chips", spec, 1, "")
    assert heal.outage_explains(chip, "svc", dict(spec, resource="box-a-chip"), 1, "")
    assert not heal.outage_explains(chip, "chipset-fan", spec, 1, "")
    assert heal.outage_explains({}, "svc", spec, 1, "anything at all")


def test_heal_outage_covers_match_the_checks_identity_never_its_output(env):
    """[note f7b7938ee8de232f] Under `--covers demo-serve`, a health check reporting the held service and a lost
    chip escalates: its output never makes the outage explain it. A check named after the service stays
    quiet, even for a chip loss: the accepted gap (heal module doc)."""
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, up = _heal_setup(env, fix=None, escalate="coordinator", machine="box-a")
    spec, now = _heal_spec(p), time.time()
    rec = ml.set_outage("box-a", "service off for testing", "user", "4h", now=now, covers="demo-serve")
    out = "demo-serve: held (paused by user)\nchip 0 missing"
    health = dict(spec, check="echo 'demo-serve: held (paused by user)'; echo 'chip 0 missing'; exit 1")
    assert not heal.outage_explains(rec, "svc", health, 1, out)
    assert "self-fix task" in heal.run(d, "svc", health, now)[0]
    assert "known_outage" not in heal.state(p.db, "svc")
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1
    # The check's name, resource or unit naming the service: quiet, whatever its output says.
    assert heal.outage_explains(rec, "demo-serve", spec, 1, out)
    assert heal.outage_explains(rec, "svc", dict(spec, resource="box-a-demo-serve"), 1, out)
    assert heal.outage_explains(rec, "svc", dict(spec, unit="demo-serve.service"), 1, out)
    assert heal.run(d, "demo-serve", health, now)[0].startswith("unhealthy (known outage until ")
    assert heal.state(p.db, "demo-serve")["known_outage"] == "box-a"
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1


def test_heal_outage_covering_the_host_explains_only_unknown_runs_that_are_no_finding(env):
    """Under `--covers host` (power and network alike) only a run that cannot reach the box, exit 75 or
    255, is explained: never one carrying the not-outage FINDING line (the box is up), nor another exit."""
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, up = _heal_setup(env, fix=None, escalate="coordinator", machine="box-a")
    spec, now = _heal_spec(p), time.time()
    rec = ml.set_outage("box-a", "host repair", "user", "4h", now=now, covers="host")
    lost = f"chip 0 missing\n{heal.FINDING}"
    assert heal.outage_explains(rec, "svc", spec, 255, "")
    assert heal.outage_explains(rec, "svc", spec, 75, "ssh: connect to host timed out")
    for rc in (75, 255):
        assert not heal.outage_explains(rec, "svc", spec, rc, lost)
    for rc in (1, 2, 124):
        assert not heal.outage_explains(rec, "svc", spec, rc, "")
    # Run by run: the finding escalates as plain unhealthy, another exit is reported, the dark run is quiet.
    found = dict(spec, check=f"echo 'chip 0 missing'; echo '{heal.FINDING}'; exit 75")
    assert "self-fix task" in heal.run(d, "svc", found, now)[0]
    assert "known_outage" not in heal.state(p.db, "svc")
    assert heal.run(d, "two", dict(spec, check="exit 2"), now)[0].startswith("error: heal check exited 2")
    assert "known_outage" not in heal.state(p.db, "two")
    assert heal.run(d, "dark", dict(spec, check="exit 255"), now) == ("ok (unknown, rc 255)", None)
    assert heal.state(p.db, "dark")["known_outage"] == "box-a"


def test_heal_outage_that_does_not_explain_a_result_keeps_its_episode(env):
    """Under `--covers host`, a check alternating between a dark run (explained) and a chip loss (not)
    escalates once its grace has passed: a result the live outage does not explain never restarts the
    episode. Only the outage's end does."""
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, up = _heal_setup(env, fix=None, grace_s=600, escalate="coordinator", machine="box-a")
    spec, now = _heal_spec(p), time.time()
    ml.set_outage("box-a", "host repair", "user", "4h", now=now, covers="host")
    dark, lost = dict(spec, check="exit 255"), dict(spec, check="echo 'chip 0 missing'; exit 1")
    first = heal.run(d, "svc", lost, now)[0]
    assert first.startswith("unhealthy") and "self-fix task" not in first   # within its grace
    assert heal.run(d, "svc", dark, now + 300) == ("ok (unknown, rc 255)", None)
    assert "self-fix task" in heal.run(d, "svc", lost, now + 660)[0]
    assert "known_outage" not in heal.state(p.db, "svc")


def test_heal_back_from_a_long_unknown_stretch_starts_a_fresh_episode(env):
    from ttp import heal
    p, d, up = _heal_setup(env, fix=None, grace_s=600, escalate="coordinator")
    spec, now = _heal_spec(p), time.time()
    dark = dict(spec, check="exit 75")
    tid = _heal_to_self_fix_failed(p, d, up, dict(spec, grace_s=0), now)
    assert "coordinator told" in heal.run(d, "svc", spec, now + 10)[0]
    # A short unknown blip keeps the episode.
    heal.run(d, "svc", dark, now + 20)
    assert "coordinator told" in heal.run(d, "svc", spec, now + 30)[0]
    # A long one (past grace and FRESH_UNKNOWN_S) with the task closed: grace again, then a new task.
    heal.run(d, "svc", dark, now + 40)
    t1 = now + 40 + heal.FRESH_UNKNOWN_S + 1
    assert heal.run(d, "svc", spec, t1)[0] == "unhealthy (0 s, grace 600 s)"
    st = heal.state(p.db, "svc")
    assert st["since"] == t1 and not {"task", "escalated", "observed"} & set(st)
    assert "it has no fix" in heal.run(d, "svc", spec, t1 + 600)[0]
    assert heal.state(p.db, "svc")["task"] != tid
    # An open task survives a long unknown stretch: the episode goes on.
    heal.run(d, "svc", dark, t1 + 700)
    assert "open" in heal.run(d, "svc", spec, t1 + 700 + 10 * heal.FRESH_UNKNOWN_S)[0]


def test_heal_fresh_episode_after_a_long_unknown_stretch_keeps_the_fix_cap(env, tmp_path):
    from ttp import heal
    p, d, up = _heal_setup(env, fix=f"echo fixed >> {tmp_path}/fixes", grace_s=0, max_fixes=1, window_h=2,
                           escalate="coordinator")
    spec, now = _heal_spec(p), time.time()
    assert heal.run(d, "svc", spec, now)[0] == "unhealthy (fixed; rechecking)"
    assert "the fix ran but" in heal.run(d, "svc", spec, now + 10)[0]
    p.db.update_task(heal.state(p.db, "svc")["task"], status="failed")
    heal.run(d, "svc", dict(spec, check="exit 75"), now + 20)
    assert "used up its cap (1)" in heal.run(d, "svc", spec, now + 20 + heal.FRESH_UNKNOWN_S + 1)[0]
    assert (tmp_path / "fixes").read_text().count("fixed") == 1


def test_heal_reboots_keep_the_fix_cap_and_an_open_self_fix_task(env, tmp_path):
    """Known gap (module doc): a reboot starts no fresh episode, so a crash-looping box gets no fix per boot."""
    from ttp import heal
    boot = tmp_path / "boot"
    boot.write_text("aaaaaaaa-1111")
    p, d, up = _heal_setup(env, grace_s=300, max_fixes=1, escalate="coordinator")
    spec, now = _heal_spec(p), time.time()
    spec = dict(spec, check=f'echo "boot_id: $(cat {boot})"; test -e {up}', fix=f"echo fixed >> {tmp_path}/fixes")
    assert "grace" in heal.run(d, "svc", spec, now)[0]
    assert heal.run(d, "svc", spec, now + 300)[0] == "unhealthy (fixed; rechecking)"
    assert "the fix ran but" in heal.run(d, "svc", spec, now + 310)[0]
    tid = heal.state(p.db, "svc")["task"]
    for i, t in enumerate((400, 900, 1500)):
        boot.write_text(f"bbbbbbbb-{i}")   # the host rebooted and still fails
        assert heal.run(d, "svc", spec, now + t)[0] == f"unhealthy (self-fix task #{tid} open)"
    p.db.update_task(tid, status="failed")
    for i, t in enumerate((2000, 2600, 3500)):
        boot.write_text(f"cccccccc-{i}")
        assert "coordinator told" in heal.run(d, "svc", spec, now + t)[0]
    assert (tmp_path / "fixes").read_text().count("fixed") == 1
    assert len(heal.state(p.db, "svc")["fixes"]) == 1
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1


@pytest.mark.parametrize("escalate", ["user", "coordinator"])
@pytest.mark.parametrize("task_status", ["running", "done", "failed", "blocked", "gone", "escalated-only"])
def test_heal_known_fault_added_after_self_fix_stays_quiet(env, escalate, task_status):
    from ttp import heal
    p, d, up = _heal_setup(env, outage=True, fix=None, escalate=escalate,
                          check="echo 'ttp-heal: not-outage'; exit 1")
    spec, now = _heal_spec(p), time.time()
    assert "self-fix task" in heal.run(d, "svc", spec, now)[0]
    tid = heal.state(p.db, "svc")["task"]
    p.db.update_task(tid, status="running")
    assert heal.run(d, "svc", spec, now + 1)[0] == f"unhealthy (self-fix task #{tid} open)"

    # The self-fix identifies a known fault instead of repairing the hardware.
    payload = json.loads(p.db.one("SELECT payload FROM schedules WHERE name='svc'")["payload"])
    payload["heal"]["known_fault"] = "vendor bug"
    p.db.x("UPDATE schedules SET payload=? WHERE name='svc'", (json.dumps(payload),))
    quiet = _heal_spec(p)
    if task_status == "gone":
        p.db.x("DELETE FROM tasks WHERE id=?", (tid,))
    elif task_status == "escalated-only":
        st = heal.state(p.db, "svc")
        st.pop("task")
        heal._save(p.db, "svc", st)
    else:
        p.db.update_task(tid, status=task_status)
    for dt in (2, heal.OUTAGE_S, 3 * heal.OUTAGE_S):
        assert heal.run(d, "svc", quiet, now + dt) == ("unhealthy (known fault: kept quiet)", None)
        st = heal.state(p.db, "svc")
        assert st["finding"] and st["status"] == "unhealthy"
        assert not {"task", "escalated", "alerted", "observed"} & set(st)
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert'")
    assert not p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'heal:%'")
    assert p.db.one("SELECT COUNT(*) n FROM tasks")["n"] == (0 if task_status == "gone" else 1)
    if task_status not in ("gone", "escalated-only"):
        assert p.db.task(tid)["status"] == task_status

    # A real outage must still escalate even though the same fault is known.
    outage = dict(quiet, check="exit 1")
    assert "self-fix task" in heal.run(d, "svc", outage, now + 4 * heal.OUTAGE_S)[0]
    outage_tid = heal.state(p.db, "svc")["task"]
    p.db.update_task(outage_tid, status="failed")
    assert ("alerted" if escalate == "user" else "coordinator told") in heal.run(
        d, "svc", outage, now + 4 * heal.OUTAGE_S + 1)[0]


def test_heal_check_marks_a_non_outage_finding_with_one_output_line():
    from ttp import heal
    assert heal.finding(1, "keeper stale\nttp-heal: not-outage\n")
    assert heal.finding(2, "  ttp-heal: not-outage  ")
    assert not heal.finding(0, "ttp-heal: not-outage"), "a healthy check is healthy"
    assert not heal.finding(1, "box down"), "no line: the default classification"
    assert not heal.finding(1, "it says ttp-heal: not-outage inline"), "only a line of its own counts"


def test_heal_outage_check_with_a_non_outage_finding_queues_an_ordinary_self_fix_task(env):
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, up = _heal_setup(env, outage=True, machine="box-a", fix=None, grace_s=7200)
    spec, now = _heal_spec(p), time.time()
    # Default (no line): an outage check exiting 1 escalates as an outage within OUTAGE_S, reported down.
    assert heal.run(d, "svc", spec, now)[0].startswith("unhealthy (")
    assert list(ml.conditions()) == ["box-a:down"]
    # The same check now says the box is up: the down report clears and its own grace_s applies.
    found = dict(spec, check=f"test -e {up} || {{ echo 'keeper stale'; echo 'ttp-heal: not-outage'; exit 2; }}")
    status = heal.run(d, "svc", found, now + 60)
    assert status == ("unhealthy (60 s, grace 7200 s)", now + 7200), status
    assert ml.conditions() == {} and heal.state(p.db, "svc")["finding"] is True
    assert heal.run(d, "svc", found, now + heal.OUTAGE_S)[0] == f"unhealthy ({heal.OUTAGE_S} s, grace 7200 s)"
    assert not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")
    status = heal.run(d, "svc", found, now + 7200)[0]
    assert "self-fix task" in status and not status.startswith("error"), status
    t = p.db.one("SELECT * FROM tasks WHERE origin='daemon'")
    assert json.loads(t["labels"]) == ["heal:svc"] and "non-outage finding" in t["spec"]
    p.db.update_task(t["id"], status="failed")
    assert "alerted" in heal.run(d, "svc", found, now + 7500)[0]
    alert = p.db.one("SELECT text FROM messages WHERE kind='alert' AND ref='heal:svc'")["text"]
    assert alert.startswith("Heal check svc still fails") and "Outage" not in alert, alert
    up.touch()
    assert heal.run(d, "svc", found, now + 7800) == ("ok (healthy)", None)
    assert "finding" not in heal.state(p.db, "svc")
    # A known fault stays quiet past the outage window when the check reports a finding.
    p.db.set_kv(heal.STATE_KEY + "svc", {})
    up.unlink()
    quiet = dict(found, known_fault="vendor bug", grace_s=0)
    for dt in (0, heal.OUTAGE_S, 3 * heal.OUTAGE_S):
        assert heal.run(d, "svc", quiet, now + 9000 + dt)[0] == "unhealthy (known fault: kept quiet)"
    # Without the line the same exit 2 stays today's unknown-and-error result.
    p.db.set_kv(heal.STATE_KEY + "svc", {})
    plain = dict(spec, check="echo 'keeper stale'; exit 2", grace_s=600)
    assert heal.run(d, "svc", plain, now + 20000)[0].startswith("error: heal check exited 2")


def test_heal_presets_read_systemd_and_broker_status(env, monkeypatch):
    from ttp import heal
    out = {}
    monkeypatch.setattr(heal, "_shell", lambda cmd, cwd, timeout: out[cmd.split()[0]])
    sd = heal.validate({"preset": "systemd", "unit": "w.service"})
    st: dict = {}
    out["systemctl"] = (0, "ActiveState=active\nNRestarts=2")
    assert heal.check(sd, st, ".")[0] == 0 and st["nrestarts"] == 2
    out["systemctl"] = (0, "ActiveState=active\nNRestarts=5")
    assert heal.check(sd, st, ".") == (1, "w.service is crash-looping: NRestarts 2 -> 5")
    out["systemctl"] = (0, "ActiveState=failed\nNRestarts=5")
    assert heal.check(sd, st, ".")[0] == 1
    bk = heal.validate({"preset": "broker", "status_command": "status --json"})
    out["status"] = (0, json.dumps({"held": False, "auto_power_cycle": True, "auto_reboot": True}))
    assert heal.check(bk, {}, ".") == (0, "no hold; automatic recovery on")
    out["status"] = (0, json.dumps({"held": True, "auto_power_cycle": False, "auto_reboot": True}))
    assert heal.check(bk, {}, ".") == (1, f"held is set; auto_power_cycle is off\n{heal.RECOVERY_OFF}")
    out["status"] = (0, json.dumps({"held": False}))
    assert heal.check(bk, {}, ".")[0] == 75
    out["status"] = (255, "ssh: connect failed")
    assert heal.check(bk, {}, ".")[0] == 255
    with pytest.raises(ValueError, match="needs `url`"):
        heal.validate({"preset": "http", "url": "ftp://x"})


def _hold_check(p, holder):
    """A check that, like one standing down for a known outage, exits 75: it prints the untenanted-hold line
    while `holder` is missing."""
    from ttp import heal
    return heal.validate({"check": f"test -e {holder} || echo '{heal.UNTENANTED}'; exit 75", "machine": "box-a",
                          "outage": True, "grace_s": 0})


@pytest.mark.parametrize("escalate", ["user", "coordinator"])
def test_heal_pages_an_untenanted_hold_past_its_ceiling_even_under_a_known_outage(env, escalate):
    from ttp import alerts, heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import schedule as sched
    mm.add("box-a", tags="device")
    p, d, up = _heal_setup(env)
    holder = p.root / "holder"
    spec = {**_hold_check(p, holder), "escalate": escalate}
    sched.upsert(p.db, "svc", "command", "5m", payload={"heal": spec})
    now = time.time()
    ml.set_outage("box-a", "unrelated repair", "user", "1d", now=now)
    ceiling = heal.UNTENANTED_HOLD_S

    def pages():
        return (p.db.q("SELECT ref FROM messages WHERE kind='alert' AND ref LIKE 'heal-hold:%'"),
                p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'heal-hold:%'"))

    for t in (now, now + ceiling / 2, now + ceiling - 60):
        heal.run(d, "svc", spec, t)
    assert pages() == ([], [])
    for t in (now + ceiling, now + ceiling + 900):   # past the ceiling: once
        heal.run(d, "svc", spec, t)
    alerts_, events = pages()
    if escalate == "user":
        assert [a["ref"] for a in alerts_] == ["heal-hold:svc"] and events == []
        text = p.db.one("SELECT text FROM messages WHERE ref='heal-hold:svc'")["text"]
    else:
        assert alerts_ == [] and len(events) == 1 and events[0]["severity"] == "high"
        text = events[0]["text"]
    assert "held with no holder" in text and "unrelated repair" in text and "box-a" in text
    # outagemsg: when it began, how long so far, who acts now and what was tried.
    assert "so far)" in text and "Tried: " in text and "Next: find who holds it" in text, text
    assert ("Acting now: nobody: escalating to the user." if escalate == "user" else
            f"Acting now: {p.name} (its coordinator).") in text, text
    # Unknown runs without the line keep the page; a run that answers without it clears it.
    heal.run(d, "svc", {**spec, "check": "exit 75"}, now + ceiling + 1000)
    assert alerts.sweep(p.db, now + ceiling + 1001) == [] and "hold_since" in heal.state(p.db, "svc")
    holder.touch()
    heal.run(d, "svc", {**spec, "check": "exit 1"}, now + ceiling + 1200)
    assert "hold_since" in heal.state(p.db, "svc")   # one clear run does not end the episode
    clear = now + ceiling + 1200 + heal.HOLD_CLEAR_S
    heal.run(d, "svc", {**spec, "check": "exit 1"}, clear)
    assert "hold_since" not in heal.state(p.db, "svc")
    if escalate == "user":
        assert [e["key"] for e in alerts.sweep(p.db, clear + 1)] == ["heal-hold:svc"]
        assert p.db.one("SELECT cleared_why FROM alerts WHERE key='heal-hold:svc'")["cleared_why"] == \
            "condition cleared"
    # The project's heal.untenanted_hold_s sets the ceiling.
    d.cfg.setdefault("heal", {})["untenanted_hold_s"] = 600
    holder.unlink()
    t1 = now + 3 * ceiling
    heal.run(d, "svc", spec, t1)
    heal.run(d, "svc", spec, t1 + 600)
    assert len(pages()[0 if escalate == "user" else 1]) == 2


def test_heal_hold_age_counts_from_the_episode_not_the_last_automatic_reset(env):
    """A loop of failing automatic resets frees the device for a moment between runs: a run that lands in
    such a gap does not restart the hold's age, so the page still goes at the ceiling from the first run."""
    from ttp import heal
    p, d, up = _heal_setup(env)
    holder = p.root / "holder"
    spec = _hold_check(p, holder)
    now, ceiling = time.time(), heal.UNTENANTED_HOLD_S
    gap = {**spec, "check": "echo 'reset ran; device free'; exit 1"}
    t = now
    while t < now + ceiling:
        heal.run(d, "svc", spec, t)
        heal.run(d, "svc", gap, t + 600)   # the reset frees it, then it is held again
        t += 1800
    heal.run(d, "svc", spec, now + ceiling)
    assert heal.state(p.db, "svc")["hold_since"] == now
    assert [a["ref"] for a in p.db.q("SELECT ref FROM messages WHERE kind='alert' AND ref LIKE 'heal-hold:%'")] \
        == ["heal-hold:svc"]


def test_heal_stays_quiet_for_an_ordinary_outage_and_a_user_paused_hold(env):
    from ttp import coordinator as coord
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, up = _heal_setup(env)
    now = time.time()
    ml.set_outage("box-a", "hardware repair", "user", "1d", now=now)
    # A check standing down for the outage without the line: no hold page, however long.
    plain = heal.validate({"check": "echo 'known outage: standing down'; exit 75", "machine": "box-a", "grace_s": 0})
    for t in (now, now + 3600, now + 5 * heal.UNTENANTED_HOLD_S):
        heal.run(d, "svc", plain, t)
    assert "hold_since" not in heal.state(p.db, "svc")
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert' AND ref LIKE 'heal-hold:%'")
    # An untenanted hold on a resource the user paused is known: quiet too.
    spec = {**_hold_check(p, p.root / "holder"), "resource": "box-a-device"}
    coord.pause_resource(p, "box-a-device", True, reason="maintenance", by="user")
    t0 = now + 6 * heal.UNTENANTED_HOLD_S
    for t in (t0, t0 + heal.UNTENANTED_HOLD_S + 60):
        heal.run(d, "svc", spec, t)
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert' AND ref LIKE 'heal-hold:%'")


@pytest.mark.parametrize("ender", ["schedule disabled", "schedule deleted", "escalate coordinator", "user pause"])
def test_heal_hold_page_ends_like_the_heal_alert(env, ender):
    """The untenanted-hold page shares the heal alert's lifecycle (cleared_why): the sweep ends it once the
    check has no enabled schedule with a heal block, escalation moves to the coordinator or the user pauses
    its resource, and the next run does not page the user again."""
    from ttp import alerts, heal
    from ttp import coordinator as coord
    from ttp import schedule as sched
    p, d, up = _heal_setup(env)
    spec = {**_hold_check(p, p.root / "holder"), "resource": "box-a-device"}
    sched.upsert(p.db, "svc", "command", "5m", payload={"heal": spec})
    now, ceiling = time.time(), heal.UNTENANTED_HOLD_S
    for t in (now, now + ceiling):
        heal.run(d, "svc", spec, t)

    def page():
        return p.db.one("SELECT * FROM alerts WHERE key='heal-hold:svc' ORDER BY id DESC LIMIT 1")

    assert page()["cleared"] is None
    assert alerts.sweep(p.db, now + ceiling + 1) == [] and page()["cleared"] is None   # still held: kept
    if ender == "schedule disabled":
        p.db.x("UPDATE schedules SET enabled=0 WHERE name='svc'")
    elif ender == "schedule deleted":
        p.db.x("DELETE FROM schedules WHERE name='svc'")
    elif ender == "escalate coordinator":
        p.set_config("heal.escalate", "coordinator")
        d.cfg = p.config()
    else:
        coord.pause_resource(p, "box-a-device", True, reason="maintenance", by="user")
    assert [e["key"] for e in alerts.sweep(p.db, now + ceiling + 2)] == ["heal-hold:svc"]
    why = {"schedule disabled": "the check has no enabled schedule with a heal block",
           "schedule deleted": "the check has no enabled schedule with a heal block",
           "escalate coordinator": "escalation moved to coordinator",
           "user pause": "box-a-device is paused (by the user)"}[ender]
    assert page()["cleared_why"] == why
    if ender in ("escalate coordinator", "user pause"):   # the hold goes on: no second user page
        heal.run(d, "svc", spec, now + ceiling + 300)
        assert len(p.db.q("SELECT id FROM alerts WHERE key='heal-hold:svc'")) == 1


def test_heal_broker_preset_reports_a_hold_with_no_holder(env, tmp_path):
    from ttp import heal
    status = tmp_path / "status.json"
    spec = heal.validate({"preset": "broker", "status_command": f"cat {status}", "holder_field": "holder"})
    status.write_text(json.dumps({"held": True, "holder": "", "auto_power_cycle": True, "auto_reboot": True}))
    rc, out = heal.check(spec, {}, str(tmp_path))
    assert rc == 1 and heal.untenanted(out) and "holder is empty" in out
    status.write_text(json.dumps({"held": True, "holder": "job 12", "auto_power_cycle": True, "auto_reboot": True}))
    rc, out = heal.check(spec, {}, str(tmp_path))
    assert rc == 1 and not heal.untenanted(out)
    rc, out = heal.check(heal.validate({"preset": "broker", "status_command": f"cat {status}"}), {}, str(tmp_path))
    assert not heal.untenanted(out)   # no holder_field: a hold alone is not called untenanted


def test_heal_open_task_matches_labels_exactly_not_as_like_wildcards(env):
    from ttp import heal
    p = make(env)
    ids = {n: p.db.add_task(f"t {n}", "s", origin="daemon", labels=[heal.LABEL + n]) for n in ("axb", "a%c")}
    assert heal.open_task(p.db, "a_b") is None and heal.open_task(p.db, "a%") is None
    assert heal.open_task(p.db, "%") is None and heal.open_task(p.db, "a_c") is None
    assert heal.open_task(p.db, "axb")["id"] == ids["axb"] and heal.open_task(p.db, "a%c")["id"] == ids["a%c"]


def test_heal_queued_self_fix_task_is_cancelled_when_the_check_recovers_a_running_one_is_not(env):
    from ttp import heal
    p, d, up = _heal_setup(env)
    spec, now = _heal_spec(p), time.time()
    nofix = dict(spec, fix="")
    for status in ("queued", "running"):
        assert "self-fix task" in heal.run(d, "svc", nofix, now)[0]
        t = heal.open_task(p.db, "svc")
        p.db.update_task(t["id"], status=status)
        up.touch()
        assert heal.run(d, "svc", nofix, now + 10)[0] == "ok (healthy)"
        t = p.db.task(t["id"])
        if status == "queued":
            assert t["status"] == "cancelled" and "recovered" in t["blocked_reason"]
        else:
            assert t["status"] == "running"
            p.db.update_task(t["id"], status="done")
        up.unlink()


def test_heal_outage_check_exiting_an_error_code_gets_one_task(env):
    import unittest.mock
    from ttp import alerts, heal
    from ttp import schedule as sched
    p, d, up = _heal_setup(env, outage=True, grace_s=600)
    payload = json.loads(p.db.one("SELECT payload FROM schedules WHERE name='svc'")["payload"])
    payload["heal"]["check"] = "exit 124"
    sched.upsert(p.db, "svc", "command", "5m", payload=payload)
    spec, now = _heal_spec(p), time.time()

    def tick(at):   # one daemon run of the schedule, due now
        p.db.x("UPDATE schedules SET next_run=0 WHERE name='svc'")
        with unittest.mock.patch("time.time", return_value=at):
            d.run_schedules()
        return p.db.one("SELECT last_status FROM schedules WHERE name='svc'")["last_status"]

    assert tick(now).startswith("error: heal check exited 124")
    tick(now + 300)   # second failure: the daemon queues its schedule_fix task
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1
    status = tick(now + 700)   # past the outage window: heal adopts that task
    tasks = p.db.q("SELECT * FROM tasks WHERE origin='daemon'")
    assert len(tasks) == 1 and f"self-fix task #{tasks[0]['id']}" in status, status
    assert heal.has_open_task(p.db, "svc")
    for dt in (1000, 1300):
        tick(now + dt)
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1
    p.db.update_task(tasks[0]["id"], status="failed")
    assert "alerted" in heal.run(d, "svc", spec, now + 1600)[0]
    assert p.db.q("SELECT id FROM messages WHERE kind='alert' AND ref='heal:svc'")
    up.touch()
    assert heal.run(d, "svc", dict(spec, check=f"test -e {up}"), now + 1900)[0] == "ok (healthy)"
    assert "heal:svc" in [e["key"] for e in alerts.sweep(p.db)]


def test_responsibility_inventory_credits_a_cotenants_fresh_healthy_heal_check_without_an_owner(env):
    from ttp import heal
    from ttp import machines as mm
    from ttp import responsibilities as rs
    from ttp import schedule as sched
    mm.add("box-a", tags="device")
    me, gamma = make(env), _second_project(env, "gamma")
    _resources(me, "Machines: box-a")
    n = len(rs.inventory(me))
    # A co-tenant's heal check that never ran, ran unhealthy, or ran healthy too long ago does not cover it.
    sched.upsert(gamma.db, "box-a-up", "command", "5m", payload={"heal": {"check": "ssh box-a true", "outage": True}})
    assert rs.refresh(me, now=10_000.0)["line"] == f"health coverage: {n - 1}/{n} (missing: machine box-a)"
    gamma.db.set_kv(heal.STATE_KEY + "box-a-up", {"status": "unhealthy", "last_check": 9_900.0, "last_rc": 1})
    assert rs.inventory(me, now=10_000.0)[0]["covered_by"] == []
    gamma.db.set_kv(heal.STATE_KEY + "box-a-up", {"status": "healthy", "last_check": 10_000.0 - 3 * 300, "last_rc": 0})
    assert rs.inventory(me, now=10_000.0)[0]["covered_by"] == [], "stale: not covered"
    # Healthy within its interval: covered by the co-tenant, with no recovery owner set, and gamma is only read.
    gamma.db.set_kv(heal.STATE_KEY + "box-a-up", {"status": "healthy", "last_check": 9_800.0, "last_rc": 0})
    mtime = (gamma.base / "state" / "project.db").stat().st_mtime_ns
    rec = rs.refresh(me, now=10_000.0)
    assert rec["items"][0] == {"kind": "machine", "name": "box-a", "covered_by": ["gamma/box-a-up"],
                               "via": "gamma heal check"}
    assert rec["line"] == f"health coverage: {n}/{n} (machine box-a covered by gamma heal check)"
    assert (gamma.base / "state" / "project.db").stat().st_mtime_ns == mtime
    advice = rs.review_advice(me.db)
    assert "Give each missing one" not in advice and "no duplicate check" in advice
    # Its check going stale makes the machine missing again, and the review advises a check.
    rec = rs.refresh(me, now=9_800.0 + 3 * 300)
    assert rec["line"] == f"health coverage: {n - 1}/{n} (missing: machine box-a)"
    assert "Give each missing one a heal check" in rs.review_advice(me.db)
    # A recovery owner's heal check still covers it whatever its state, and wins over the co-tenant's.
    mm.set_recovery("box-a", "alpha", None)
    alpha = _second_project(env, "alpha")
    sched.upsert(alpha.db, "box-a-own", "command", "5m", payload={"heal": {"check": "ssh box-a true"}})
    gamma.db.set_kv(heal.STATE_KEY + "box-a-up", {"status": "healthy", "last_check": 20_000.0, "last_rc": 0})
    assert rs.refresh(me, now=20_000.0)["line"] == f"health coverage: {n}/{n} (machine box-a covered by owner alpha)"
    # The owner set but without a check of its own: the co-tenant's healthy check covers it.
    alpha.db.x("DELETE FROM schedules WHERE name='box-a-own'")
    assert rs.refresh(me, now=20_000.0)["line"] == f"health coverage: {n}/{n} (machine box-a covered by gamma heal check)"
    # The daily review spec carries the coverage line and no advice to add a duplicate check.
    from ttp import daemon as dm
    sched.upsert(me.db, "daily-review", "llm", "1d", payload={})
    me.db.x("UPDATE schedules SET next_run=0 WHERE name='daily-review'")
    dm.Daemon(me.base).run_schedules()
    t = me.db.one("SELECT spec FROM tasks WHERE labels LIKE '%daily-review%'")
    assert t and "covered by gamma heal check" in t["spec"] and "Give each missing one" not in t["spec"]


def test_heal_check_reports_its_machine_down_or_held_to_the_ledger_and_clears_it(env):
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, up = _heal_setup(env, machine="box-a", fix=None, grace_s=600)
    spec = _heal_spec(p)
    assert heal.machine_of(spec) == "box-a"
    now = time.time()
    heal.run(d, "svc", spec, now)
    c = ml.conditions()["box-a:down"]
    assert c["seen_by"] == ["demo"] and c["grace_s"] == 600 and "heal check svc" in c["text"]
    up.touch()
    heal.run(d, "svc", spec, now + 60)
    assert ml.conditions() == {} and "machine_reported" not in heal.state(p.db, "svc")
    # A known fault on a non-outage check is not reported; an unknown result only past the outage window.
    with pytest.raises(ValueError, match="condition"):
        heal.validate({"check": "true", "condition": "gone"})
    assert heal.machine_of(heal.validate({"check": "x", "resource": "box-a-device"})) is None
    assert heal.machine_of(heal.validate({"check": "x", "resource": "box-a-device", "outage": True})) == "box-a"
    up.unlink()
    quiet = heal.validate({"check": f"test -e {up}", "machine": "box-a", "known_fault": "vendor bug"})
    heal.run(d, "svc", quiet, now + 120)
    assert ml.conditions() == {}
    dark = heal.validate({"check": "exit 75", "machine": "box-a", "outage": True, "grace_s": 600})
    heal.run(d, "svc", dark, now + 180)
    assert ml.conditions() == {}
    heal.run(d, "svc", dark, now + 180 + 600)
    assert ml.conditions()["box-a:down"]["grace_s"] == 0


def test_an_outage_check_whose_only_route_failed_for_an_hour_reports_no_machine_down(env):
    """Replay: an outage check unknown for over 60 min only because its ssh route failed paged the
    machine's recovery owner for nothing. Now it escalates as its own route problem and reports nothing."""
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    assert heal.route_failed(255, "") and heal.route_failed(75, "ssh: connect to host x port 22: Connection refused")
    assert heal.route_failed(124, "No route to host") and heal.route_failed(75, "Network is unreachable")
    assert not heal.route_failed(75, "broker status: no answer") and not heal.route_failed(1, "Connection refused")
    assert not heal.route_failed(0, "Connection timed out")
    mm.add("box-a", tags="device")
    mm.set_recovery("box-a", owner="alpha")
    p, d, _ = _heal_setup(env)
    dark = heal.validate({"check": "echo 'ssh: connect to host box-a port 22: Connection timed out' >&2; exit 255",
                          "machine": "box-a", "outage": True, "grace_s": 0})
    t0 = time.time()
    for dt in (0, 1800, 3700, 7200):
        heal.run(d, "svc", dark, t0 + dt)
        assert ml.conditions() == {}, dt
    assert ml.route(t0 + 7200, able=lambda n: (True, ""), healthy=lambda *a: []) == []
    st = heal.state(p.db, "svc")
    assert st["route_only"] and st["status"] == "unhealthy" and "machine_reported" not in st
    task = p.db.one("SELECT * FROM tasks WHERE labels LIKE '%heal:svc%'")
    assert "own route problem" in st["why"] and "route (ssh, a tunnel) failed" in task["spec"]
    # One unknown run that is not a route failure ends route_only: past the window the machine is down.
    other = heal.validate({"check": "echo 'status: no answer'; exit 75", "machine": "box-a", "outage": True,
                           "grace_s": 0})
    heal.run(d, "svc", other, t0 + 7300)
    assert not heal.state(p.db, "svc")["route_only"] and list(ml.conditions()) == ["box-a:down"]
    # A run that is a route failure again does not make the stretch route-only, so the report stays.
    heal.run(d, "svc", dark, t0 + 7400)
    assert list(ml.conditions()) == ["box-a:down"]
    # A route-only stretch after a down report clears it.
    p.root.joinpath("svc-up").touch()
    heal.run(d, "svc", heal.validate({"check": f"test -e {p.root / 'svc-up'}", "machine": "box-a"}), t0 + 7500)
    assert ml.conditions() == {}
    for dt in (7600, 7600 + 3700):
        heal.run(d, "svc", dark, t0 + dt)
    assert ml.conditions() == {} and heal.state(p.db, "svc")["route_only"]


def test_an_outage_check_with_ping_host_tells_a_box_that_is_off_from_its_own_route(env, monkeypatch):
    """Upstream note: every ssh 255 counted as this project's route problem, so a box that was powered off
    or rebooting got a self-fix task instead of an outage report. `ping_host` (and a `ping_peer`) tell them apart."""
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    mm.set_recovery("box-a", owner="alpha")
    p, d, _ = _heal_setup(env)
    answers: dict = {}
    pinged: list = []
    monkeypatch.setattr(heal, "_pings", lambda h, cwd: pinged.append(h) or answers.get(h))
    block = {"check": "echo 'ssh: connect to host box-a port 22: No route to host' >&2; exit 255",
             "machine": "box-a", "outage": True, "grace_s": 0, "ping_host": "box-a", "ping_peer": "box-b"}
    spec = heal.validate(block)
    t0 = time.time()
    # Both silent, the box answering, or ping unable to tell: still our own route, nothing reported.
    for i, (box, peer) in enumerate(((False, False), (True, True), (None, True), (False, None))):
        answers.update({"box-a": box, "box-b": peer})
        heal.run(d, f"r{i}", spec, t0)
        heal.run(d, f"r{i}", spec, t0 + 3700)
        st = heal.state(p.db, f"r{i}")
        assert st["route_only"] and "own route problem" in st["why"] and ml.conditions() == {}, (box, peer)
    # The box silent while its peer answers: it is off or rebooting, an outage reported down.
    answers.update({"box-a": False, "box-b": True})
    heal.run(d, "off", spec, t0)
    heal.run(d, "off", spec, t0 + 3700)
    st = heal.state(p.db, "off")
    assert not st["route_only"] and list(ml.conditions()) == ["box-a:down"]
    assert "box-a does not answer ping while box-b does" in st["why"] and "ping: box-a" in st["last_out"]
    # Only ssh's own exit 255 pings: a route failure with another exit code stays our route, as do runs
    # without a route failure or without the ping options.
    pinged.clear()
    heal.run(d, "other", heal.validate({**block, "check": block["check"].replace("255", "2")}), t0)
    heal.run(d, "other", heal.validate({**block, "check": block["check"].replace("255", "2")}), t0 + 3700)
    assert heal.state(p.db, "other")["route_only"] and list(ml.conditions()) == ["box-a:down"]
    heal.run(d, "plain", heal.validate({**block, "check": "echo 'status: no answer'; exit 75"}), t0)
    heal.run(d, "noping", heal.validate({**block, "ping_host": None, "ping_peer": None}), t0)
    assert pinged == []
    # A silent box with no peer to compare is never called down: the two options are set together.
    for bad in ({"ping_host": "-f", "ping_peer": "b"}, {"ping_host": "a b", "ping_peer": "b"},
                {"ping_host": 3, "ping_peer": "b"}, {"ping_peer": "box-b"}, {"ping_host": "box-a"}):
        with pytest.raises(ValueError):
            heal.validate({"check": "true", **bad})


def test_heal_pings_reads_only_no_reply_as_silent(monkeypatch):
    from ttp import heal
    for platform, silent in (("linux", 1), ("darwin", 2)):
        monkeypatch.setattr(heal.sys, "platform", platform)
        for rc, want in ((0, True), (silent, False), (127, None), (68, None)):
            monkeypatch.setattr(heal, "_exec", lambda argv, cwd, t, rc=rc: (rc, ""))
            assert heal._pings("box-a", ".") is want, (platform, rc)


def test_a_route_only_outage_check_is_not_an_outage_alert(env):
    from ttp import heal
    from ttp import schedule as sched
    p, d, _ = _heal_setup(env)
    block = {"check": "exit 255", "machine": "box-a", "outage": True, "grace_s": 0}
    sched.upsert(p.db, "svc", "command", "5m", payload={"heal": block})
    spec = _heal_spec(p)
    t0 = time.time()
    heal.run(d, "svc", spec, t0)
    heal.run(d, "svc", spec, t0 + 3700)
    assert heal.state(p.db, "svc")["route_only"] and not heal.is_outage(p.db, "svc")
    sched.upsert(p.db, "svc", "command", "5m", payload={"heal": {**block, "check": "exit 75"}})
    heal.run(d, "svc", _heal_spec(p), t0 + 3800)
    assert heal.is_outage(p.db, "svc")


def test_the_owners_own_healthy_heal_check_holds_a_machine_page_and_sends_one_low_note(env):
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import schedule as sched
    from ttp import upstream
    from ttp.daemon import Daemon
    mm.add("box-a", tags="device")
    mm.set_recovery("box-a", owner="alpha", fallback="beta")
    me, alpha = make(env), _second_project(env, "alpha")
    sched.upsert(alpha.db, "box-a-up", "command", "5m", payload={"heal": {"check": "ssh box-a true", "outage": True}})
    t0 = time.time()
    alpha.db.set_kv(heal.STATE_KEY + "box-a-up", {"status": "healthy", "last_check": t0 - 60, "last_rc": 0})
    mtime = (alpha.base / "state" / "project.db").stat().st_mtime_ns
    able = lambda n: (True, "")
    ml.report("gamma", "box-a", "down", "heal check up: unknown", grace_s=0, now=t0)
    pages = ml.route(t0 + 1, able=able)
    assert [(x["target"], x["kind"], x["severity"]) for x in pages] == [("alpha", "machine_condition_held", "low")]
    assert "by gamma" in pages[0]["text"] and "alpha/box-a-up" not in pages[0]["text"]
    assert "box-a-up" in pages[0]["text"] and "No recovery page" in pages[0]["text"]
    assert (alpha.base / "state" / "project.db").stat().st_mtime_ns == mtime, "the owner is only read"
    assert ml.delivered(pages[0])
    assert ml.route(t0 + 300, able=able) == [], "one note per episode, and no recovery page while held"
    assert ml.conditions()["box-a:down"]["page"]["held"]["by"] == "alpha"
    _resources(me, "Machines: box-a")
    assert "page held: alpha's heal check sees it healthy" in "\n".join(ml.digest_lines(me, t0 + 300))
    # The owner's check going stale (or unhealthy) lifts the hold: the usual recovery page goes out.
    ml.report("gamma", "box-a", "down", now=t0 + 1000)
    pages = ml.route(t0 + 1000, able=able)
    assert [(x["target"], x["kind"]) for x in pages] == [("alpha", "machine_condition")]
    assert "severity" not in pages[0] and "held" not in ml.conditions()["box-a:down"]["page"]
    # The owner reporting the condition itself is never held by its own other check.
    ml.report("gamma", "box-a", "down", cleared=True)
    alpha.db.set_kv(heal.STATE_KEY + "box-a-up", {"status": "healthy", "last_check": t0 + 1990, "last_rc": 0})
    ml.report("alpha", "box-a", "down", grace_s=0, now=t0 + 2000)
    assert [x["kind"] for x in ml.route(t0 + 2000, able=able)] == ["machine_condition"]
    # A broker hold comes from a check that reached the box: a healthy reachability check never holds its page.
    ml.report("alpha", "box-a", "down", cleared=True)
    ml.report("gamma", "box-a", "held", grace_s=0, now=t0 + 2010)
    got = ml.route(t0 + 2010, able=able)
    assert [(x["target"], x["kind"]) for x in got] == [("alpha", "machine_condition")]
    assert "held" not in ml.conditions()["box-a:held"]["page"]
    # Only the owner's own healthy broker check of the machine (no hold there) holds it, with no route wording.
    ml.report("gamma", "box-a", "held", cleared=True)
    sched.upsert(alpha.db, "box-a-broker", "command", "5m", payload={"heal": {
        "preset": "broker", "status_command": "true", "resource": "box-a-device"}})
    alpha.db.set_kv(heal.STATE_KEY + "box-a-broker", {"status": "healthy", "last_check": t0 + 2020, "last_rc": 0})
    ml.report("gamma", "box-a", "held", grace_s=0, now=t0 + 2030)
    got = ml.route(t0 + 2030, able=able)
    assert [(x["target"], x["kind"]) for x in got] == [("alpha", "machine_condition_held")]
    assert "box-a-broker" in got[0]["text"] and "route problem" not in got[0]["text"]
    assert ml.conditions()["box-a:held"]["page"]["held"]["checks"] == ["box-a-broker"]
    ml.report("gamma", "box-a", "held", cleared=True)
    # A recovery page recorded before the hold began is not delivered while the hold is on.
    ml.report("gamma", "box-a", "down", grace_s=0, now=t0 + 2040)
    nobody = lambda who, alias, now, cond: []
    first = ml.route(t0 + 2040, able=able, healthy=nobody)
    assert [x["kind"] for x in first] == ["machine_condition"]
    assert ml.route(t0 + 2040 + ml.CLAIM_S, able=able) == [], "unclaimed while held"
    assert ml.route(t0 + 2040 + ml.CLAIM_S, able=able, healthy=nobody)[0]["id"] == first[0]["id"]
    ml.report("gamma", "box-a", "down", cleared=True)
    # The fallback's healthy check holds it when the owner has none.
    ml.report("gamma", "box-a", "down", grace_s=0, now=t0 + 2100)
    got = ml.route(t0 + 2100, able=able, healthy=lambda who, alias, now, cond: ["up"] if who == "beta" else [])
    assert [(x["target"], x["kind"]) for x in got] == [("beta", "machine_condition_held")]
    # Delivery: a low inbox note to another project; a low feed line, no coordinator event, at home.
    d = Daemon(me.base)
    assert d._machine_page({**got[0], "target": "alpha"}, t0)
    note = [json.loads(ln) for ln in upstream.path().read_text().splitlines()][-1]
    assert (note["to"], note["severity"]) == ("alpha", "low")
    home = {**got[0], "target": me.name}
    assert d._machine_page(home, t0) and d._machine_page(home, t0)
    assert me.db.one("SELECT COUNT(*) n FROM messages WHERE ref=?", (home["id"],))["n"] == 1
    assert me.db.one("SELECT id FROM events WHERE kind='machine_condition_held'") is None


def test_heal_broker_check_reports_held_and_not_a_missing_auto_recovery(env, tmp_path):
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    out = tmp_path / "status.json"
    p, d, _ = _heal_setup(env)
    spec = heal.validate({"preset": "broker", "status_command": f"cat {out}", "resource": "box-a-device",
                          "grace_s": 0})
    out.write_text(json.dumps({"held": False, "auto_power_cycle": False, "auto_reboot": True}))
    heal.run(d, "svc", spec, time.time())
    assert ml.conditions() == {}, "automatic recovery off is not a held or down machine"
    out.write_text(json.dumps({"held": True, "auto_power_cycle": True, "auto_reboot": True}))
    heal.run(d, "svc", spec, time.time())
    assert list(ml.conditions()) == ["box-a:held"]
    out.write_text(json.dumps({"held": False, "auto_power_cycle": True, "auto_reboot": True}))
    heal.run(d, "svc", spec, time.time())
    assert ml.conditions() == {}


def test_mute_heal_and_lock_holder_times_show_the_home_zone_on_a_utc_box(env, utc_box):
    """Mute lines and their end summary, a heal check's self-fix task and lock holders (local and
    shared) are in the project's home zone with its abbreviation, never the box's UTC."""
    from types import SimpleNamespace
    from ttp import heal, locks, shared
    from ttp import screen as scr
    pac = re.compile(r"\d\d:\d\d P[DS]T")
    p = make(env)
    p.set_config("home_timezone", "America/Los_Angeles")
    now = time.time()
    scr.mute(p.db, "watcher:hw", "held", 1, now=now - 3600)
    with p.db.tx():
        [m] = p.db.kv(scr.MUTES_KEY)
        m.update(count=2, last_at=now - 600)
        p.db.set_kv(scr.MUTES_KEY, [m])
    line = scr.mute_line(m, now, p)
    assert pac.search(line.split("last at", 1)[1]) and " UTC" not in line, line
    assert scr.expire_mutes(p.db, now + 60, where=p)
    ended = p.db.one("SELECT text FROM events WHERE text LIKE 'mute ended:%'")["text"]
    assert pac.search(ended.split("last at", 1)[1]) and " UTC" not in ended, ended
    heal._escalate(SimpleNamespace(p=p), "svc", {"check": "false", "fix": "true"},
                   {"since": now - 7200, "last_rc": 1}, now, "the fix exited 1")
    spec = p.db.one("SELECT spec FROM tasks WHERE title LIKE 'Self-fix: heal check svc%'")["spec"]
    assert re.search(r"Unhealthy since \d{4}-\d\d-\d\d \d\d:\d\d P[DS]T\.", spec), spec
    assert "exit 75 (known outage, unknown) is the expected end state" in spec, spec
    assert "do not try to force a pass" in spec, spec
    p.set_config("shared_resources", ["board"])
    slots = {"own": p.state / "locks" / "dev.0.lock", "shared": shared.root() / "board" / "board.0.lock"}
    held = []
    try:
        for path in slots.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            held.append(locks.try_take([path], "task #3 (run 9)", "x"))
        assert all(held)
        assert re.fullmatch(r"task #3 \(run 9\) since \d\d:\d\d P[DS]T", locks.holders([slots["own"]], p)[0])
        for who in shared.held(p) + locks.held(p.state / "locks", p):
            assert pac.search(who) and " UTC" not in who, who
        assert len(shared.held(p)) == 2, shared.held(p)
    finally:
        for f in held:
            if f:
                f.close()


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_coordinator_wire_schema_closes_nullable_heal_and_covers_prompt(env, tmp_path, provider):
    from ttp import coordinator as coord
    from ttp.providers import get_provider
    argv, _ = get_provider(provider).build(role='coordinator', model='', effort='low', cwd=str(tmp_path),
                                           budget_usd=1, read_only=True, schema=coord.ACTIONS_SCHEMA,
                                           restrictions={})
    flag = '--output-schema' if provider == 'codex' else '--json-schema'
    raw = argv[argv.index(flag) + 1]
    wire = json.loads(pathlib.Path(raw).read_text() if provider == 'codex' else raw)
    _assert_openai_strict_schema(wire)
    item = wire['properties']['actions']['items']['properties']
    prompt = (RUNTIME.parent / 'template/prompts/coordinator.md').read_text()
    documented = set(re.findall(r'^\| `([a-z_]+)` \|', prompt, re.M))
    assert set(item['type']['enum']) == documented == set(coord.ACTION_TYPES)
    from ttp import heal
    block = item['heal']
    assert set(block['properties']) == heal._COMMON | set().union(*heal.PRESETS.values())
    for sub in block['properties'].values():
        assert 'null' in sub.get('type', []) or {'type': 'null'} in sub.get('anyOf', [])


@pytest.mark.parametrize('preset, fields', [(None, {'check': 'true'}),
                                           ('systemd', {'unit': 'example.service'}),
                                           ('http', {'url': 'http://example.invalid'}),
                                           ('broker', {'status_command': 'true'}),
                                           ('broker', {'status_command': 'true', 'expected_count': 4,
                                                       'max_recent_failures': 0})])
def test_strict_heal_padding_uses_defaults_and_preset_keys(env, preset, fields):
    from ttp import coordinator as coord, heal
    p = make(env)
    assert set(fields) <= set(coord.HEAL_SCHEMA['properties'])   # strict output sends only schema keys
    block = dict.fromkeys(coord.HEAL_SCHEMA['properties'])
    block.update(preset=preset, **fields)
    action = dict.fromkeys(coord.ACTIONS_SCHEMA['properties']['actions']['items']['properties'])
    action.update(type='schedule_set', name='probe', kind='command', heal=block)
    assert coord.apply(p, [action]) == []
    stored = json.loads(p.db.one("SELECT payload FROM schedules WHERE name='probe'")['payload'])['heal']
    assert all(v is not None for v in stored.values()) and {k: stored[k] for k in fields} == fields
    assert heal.validate(stored)['timeout_s'] == heal.DEFAULTS['timeout_s']


def test_heal_field_types_are_what_validate_takes():
    """heal.py types each key once for the coordinator's wire schema: a value of every declared type
    passes validate() for its preset, and no key has two types."""
    from ttp import heal
    sample = {'string': 'x', 'integer': 2, 'number': 1.5, 'boolean': True}
    fixed = {'escalate': 'coordinator', 'condition': 'down', 'url': 'http://example.invalid',
             'healthy_every': '1h'}
    seen = {}
    for preset, fields in [(None, {}), *heal.PRESET_FIELDS.items()]:
        for k, t in [*heal.COMMON_FIELDS.items(), *fields.items()]:
            assert seen.setdefault(k, t) == t, k
        typed = {**heal.COMMON_FIELDS, **fields}
        typed = {k: t.get('anyOf', [t])[0] for k, t in typed.items()}   # known_fault: text, or an object
        block = {k: fixed.get(k, [sample[t['items']['type']]] if t['type'] == 'array' else sample[t['type']])
                 for k, t in typed.items() if k != 'preset'}
        out = heal.validate({**block, **({'preset': preset} if preset else {})})
        assert all(out[k] == v for k, v in block.items()), preset


@pytest.mark.parametrize("shared", [False, True])
def test_health_pause_lifts_once_and_wakes_waiters_and_sweep(env, shared):
    from ttp import heal, sweep
    p, d, _ = _health_pause(env, shared=shared)
    now = time.time()
    tid = p.db.add_task("use board", "s", labels=["resource:board"])
    p.db.update_task(tid, status="queued", not_before=now + 9000,
                     result=json.dumps({"status": "waiting", "waiting_for": "board",
                                        "retry_when": "ttp lock --probe board"}))
    for dt in (0, 900):
        heal.run(d, "svc", _heal_spec(p), now + dt)
        d.check_pause_ends()
        assert "board" in p.db.paused_resources()
    heal.run(d, "svc", _heal_spec(p), now + 1800)
    d.check_pause_ends()
    d.check_pause_ends()
    assert "board" not in p.db.paused_resources()
    assert p.db.task(tid)["not_before"] is None
    assert [s["why"] for s in p.db.kv(sweep.SIGNAL_KEY)] == ["pause ended: board"]
    events = p.db.q("SELECT * FROM events WHERE kind='pause_ended'")
    assert len(events) == 1 and events[0]["source"] == "daemon"
    assert "svc" in events[0]["text"] and "3" in events[0]["text"]
    assert not p.db.q("SELECT * FROM events WHERE kind='pause_end_due'")


@pytest.mark.parametrize("rc", [1, 75, 255])
def test_health_pause_failure_or_unknown_resets_clean_window(env, monkeypatch, rc):
    from ttp import heal
    p, d, _ = _health_pause(env)
    now = time.time()
    verdict = [0]
    monkeypatch.setattr(heal, "check", lambda *a: (verdict[0], "result"))
    for dt, code in [(0, 0), (900, 0), (1200, rc), (1800, 0), (2700, 0), (3600, 0)]:
        verdict[0] = code
        heal.run(d, "svc", _heal_spec(p), now + dt)
        d.check_pause_ends()
        assert ("board" in p.db.paused_resources()) == (dt < 3600)


def test_health_pause_restart_keeps_count_and_ticks_do_not_replay_results(env):
    from ttp import heal
    from ttp.daemon import Daemon
    p, d, _ = _health_pause(env)
    assert p.db.paused_resources()["board"]["end_when"] == HEALTH_END
    now = time.time()
    heal.run(d, "svc", _heal_spec(p), now)
    for _ in range(4):
        d.check_pause_ends()
    assert "board" in p.db.paused_resources()
    heal.run(d, "svc", _heal_spec(p), now + 900)
    restarted = Daemon(p.base)
    heal.run(restarted, "svc", _heal_spec(p), now + 1800)
    restarted.check_pause_ends()
    assert "board" not in p.db.paused_resources()


@pytest.mark.parametrize("replacement", ["coordinator", "user"])
def test_health_pause_changed_after_evidence_is_not_lifted(env, replacement):
    from ttp import coordinator as coord, heal, sweep
    p, d, _ = _health_pause(env)
    before = p.db.paused_resources()["board"]["generation"]
    now = time.time()
    for dt in (0, 900, 1800):
        heal.run(d, "svc", _heal_spec(p), now + dt)
    coord.pause_resource(p, "board", True, reason="new cause", by=replacement,
                         end={"end_when": {"heal": "svc", "healthy_n": 3, "clean_s": 1800}})
    assert p.db.paused_resources()["board"]["generation"] != before
    d.check_pause_ends()
    assert "board" in p.db.paused_resources() and not p.db.kv(sweep.SIGNAL_KEY)


def test_health_pause_user_end_only_reports_once_and_never_lifts(env):
    from ttp import heal, sweep
    p, d, _ = _health_pause(env, by="user")
    now = time.time()
    for dt in (0, 900, 1800, 2700):
        heal.run(d, "svc", _heal_spec(p), now + dt)
        d.check_pause_ends()
    assert p.db.paused_resources()["board"]["by"] == "user"
    assert not p.db.kv(sweep.SIGNAL_KEY)
    (event,) = p.db.q("SELECT * FROM events WHERE kind='observation'")
    assert event["severity"] == "low" and "The pause stays" in event["text"] and "never say it is being lifted" in event["text"]


@pytest.mark.parametrize("field,value", [("expected_count", 4), ("max_recent_failures", 0)])
def test_broker_heal_checks_optional_count_and_recent_failures(env, monkeypatch, field, value):
    from ttp import heal
    spec = heal.validate({"preset": "broker", "status_command": "status", field: value})
    data = {"held": False, "auto_power_cycle": True, "auto_reboot": True,
            "count": 4, "recent_failures": 0}
    monkeypatch.setattr(heal, "_shell", lambda *args: (0, json.dumps(data)))
    assert heal.check(spec, {}, ".")[0] == 0
    data["count" if field == "expected_count" else "recent_failures"] = 3 if field == "expected_count" else 1
    assert heal.check(spec, {}, ".")[0] == 1
    data.pop("count" if field == "expected_count" else "recent_failures")
    assert heal.check(spec, {}, ".")[0] == 75


def test_health_pause_check_started_before_replacement_cannot_count(env, monkeypatch):
    from ttp import coordinator as coord, heal, pauseends
    p, d, _ = _health_pause(env)
    now = time.time()
    original = heal.check
    def replace_during_check(*args):
        coord.pause_resource(p, "board", True, by="coordinator", reason="changed during check",
                             end={"end_when": {"heal": "svc", "healthy_n": 3, "clean_s": 0}})
        return 0, "healthy old generation"
    monkeypatch.setattr(heal, "check", replace_during_check)
    heal.run(d, "svc", _heal_spec(p), now)
    assert not p.db.kv(pauseends.HEALTH_KEY)
    monkeypatch.setattr(heal, "check", original)
    for dt in (900, 1800):
        heal.run(d, "svc", _heal_spec(p), now + dt)
    d.check_pause_ends()
    assert "board" in p.db.paused_resources()


@pytest.mark.parametrize("change", ["shared_pause", "health_failure"])
def test_health_pause_compare_and_swap_rechecks_pause_and_evidence(env, monkeypatch, change):
    from ttp import coordinator as coord, heal, pauseends, shared, sweep
    p, d, _ = _health_pause(env, shared=change == "shared_pause")
    now = time.time()
    for dt in (0, 900, 1800):
        heal.run(d, "svc", _heal_spec(p), now + dt)
    if change == "shared_pause":
        update = shared.update_pause
        def race(name, fn):
            update(name, lambda w: {**w, "by": "user", "generation": "replacement"})
            return update(name, fn)
        monkeypatch.setattr(shared, "update_pause", race)
    else:
        owned = pauseends.owned
        calls = [0]
        def race(project, db=None):
            calls[0] += 1
            if calls[0] == 2:
                # The evidence changes as the fenced transaction starts: write through its own
                # connection (another connection would wait out the busy timeout, about 90 s).
                (db or project.db).set_kv(pauseends.HEALTH_KEY, None)
            return owned(project, db)
        monkeypatch.setattr(pauseends, "owned", race)
    d.check_pause_ends()
    assert "board" in p.db.paused_resources() and not p.db.kv(sweep.SIGNAL_KEY)
    assert not p.db.q("SELECT * FROM events WHERE kind='pause_ended'")
    daemon_log = p.logs / "daemon.log"
    assert "pause ends failed" not in (daemon_log.read_text() if daemon_log.exists() else "")


@pytest.mark.parametrize("stale", [True, False])
def test_health_pause_old_or_disabled_check_cannot_lift(env, stale):
    from ttp import heal
    p, d, _ = _health_pause(env)
    now = time.time()
    for dt in (0, 900, 1800):
        heal.run(d, "svc", _heal_spec(p), now + dt)
    if not stale:
        p.db.x("UPDATE schedules SET enabled=0 WHERE name='svc'")
    d._pause_ends.tick(now + 3700 if stale else now + 1800)   # past the 1800 s freshness bound
    assert "board" in p.db.paused_resources()


@pytest.mark.parametrize("probe", [None, "false"])
def test_coordinator_pause_with_until_still_ends_on_health(env, probe):
    # `until` is the deadline the coordinator is told about, not a reason to drop the health end; a
    # probe of another cause replaces it.
    from ttp import heal
    p, d, _ = _health_pause(env, end={"until": "1d", **({"end_when": probe} if probe else {})})
    held = p.db.paused_resources()["board"]
    assert held["by"] == "coordinator" and held["end_when"] == (probe or HEALTH_END)
    assert 86400 - 60 < held["until"] - time.time() <= 86400
    now = time.time()
    try:
        for dt in (0, 900, 1800):
            heal.run(d, "svc", _heal_spec(p), now + dt)
            d._pause_ends.tick(now + dt)
            for proc, _, _ in list(d._pause_ends._procs.values()):
                proc.wait(60)   # it ends in real time before the fake clock jumps past its timeout
    finally:
        d._pause_ends.stop()
    assert ("board" in p.db.paused_resources()) == bool(probe)
    assert len(p.db.q("SELECT * FROM events WHERE kind='pause_ended'")) == (0 if probe else 1)
    assert not p.db.q("SELECT * FROM events WHERE kind='pause_end_due'")


@pytest.mark.parametrize("how", ["chat", "repause"])
def test_users_pause_with_until_reports_once_and_is_never_lifted(env, how):
    # Only the coordinator's own pause gets the health end beside its `until`. The user's (asked in
    # chat, or already theirs when the coordinator re-pauses it) reports its deadline once and stays.
    from ttp import coordinator as coord, heal, pauseends
    p, d, _ = _health_pause(env, by="user" if how == "repause" else "coordinator")
    action = {"type": "resource_pause", "resource": "board", "paused": True, "until": "1d"}
    if how == "chat":
        msg = p.db.post("in", "Pause board until I say so.")
        assert coord.apply(p, [action], user_turn=True, messages=[msg]) == []
    else:
        assert coord.apply(p, [action]) == []
    held = p.db.paused_resources()["board"]
    assert held["by"] == "user" and "end_when" not in held
    now = time.time()
    for dt in (0, 900, 1800, 2700):
        heal.run(d, "svc", _heal_spec(p), now + dt)
        d._pause_ends.tick(now + dt)
    assert not p.db.kv(pauseends.HEALTH_KEY)
    assert not p.db.q("SELECT * FROM events WHERE kind IN ('pause_ended','pause_end_due','observation')")
    for dt in (86400 + 60, 86400 + 120):
        d._pause_ends.tick(now + dt)
    (event,) = p.db.q("SELECT * FROM events WHERE kind IN ('pause_ended','pause_end_due','observation')")
    assert event["kind"] == "observation" and "The pause stays" in event["text"] and "never say it is being lifted" in event["text"]
    assert p.db.paused_resources()["board"]["by"] == "user"


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("until", [True, False])
def test_health_pause_users_chat_pause_is_theirs_and_never_lifted(env, shared, until):
    from ttp import coordinator as coord, heal, sweep
    p, d, _ = _health_pause(env, shared=shared)
    # The user's words replace the coordinator's own health pause, with an end given or defaulted:
    # only the defaulted one gets the health end, and that only reports.
    msg = p.db.post("in", "Pause board until I explicitly allow it again.")
    end = {"until": "1d"} if until else {}
    assert coord.apply(p, [{"type": "resource_pause", "resource": "board", "paused": True,
                            "reason": "the user asked", **end}], user_turn=True, messages=[msg]) == []
    held = p.db.paused_resources()["board"]
    assert held["by"] == "user" and held.get("end_when") == (None if until else HEALTH_END)
    now = time.time()
    for dt in (0, 900, 1800, 2700):
        heal.run(d, "svc", _heal_spec(p), now + dt)
        d._pause_ends.tick(now + dt)
    assert p.db.paused_resources()["board"]["by"] == "user" and not p.db.kv(sweep.SIGNAL_KEY)
    assert not p.db.q("SELECT * FROM events WHERE kind IN ('pause_ended','pause_end_due')")
    assert len(p.db.q("SELECT * FROM events WHERE kind='observation'")) == (0 if until else 1)


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("until", [True, False])
@pytest.mark.parametrize("words", ["Pause box until I say so.", "Please stop using box for now."])
def test_users_chat_pause_naming_the_host_is_theirs_and_never_lifted(env, shared, until, words):
    from ttp import coordinator as coord, heal
    # Users name the host (`box`), not the resource (`box-device`); the pause must still be theirs.
    p, d, up = _heal_setup(env, resource="box-device")
    up.touch()
    if shared:
        p.set_config("shared_resources", ["box-device"])
    msg = p.db.post("in", words)
    end = {"until": "1d"} if until else {}
    assert coord.apply(p, [{"type": "resource_pause", "resource": "box-device", "paused": True,
                            "reason": "the user asked", **end}], user_turn=True, messages=[msg]) == []
    assert p.db.paused_resources()["box-device"]["by"] == "user"
    now = time.time()
    for dt in (0, 900, 1800, 2700):
        heal.run(d, "svc", _heal_spec(p), now + dt)
        d._pause_ends.tick(now + dt)
    assert p.db.paused_resources()["box-device"]["by"] == "user"
    assert not p.db.q("SELECT * FROM events WHERE kind='pause_ended'")


@pytest.mark.parametrize("gap,lifts", [(600, True), (900, False)])
def test_health_pause_gap_past_freshness_restarts_the_window(env, gap, lifts):
    from ttp import heal
    p, d, _ = _health_pause(env, every_s=300)   # results stay fresh 600 s
    now = time.time()
    times = [0, 300] + [300 + gap + 300 * k for k in range(5)]
    for t in times:
        heal.run(d, "svc", _heal_spec(p), now + t)
        d._pause_ends.tick(now + t)
    # 1800 s of clean results exist only if the gap did not break them.
    assert ("board" not in p.db.paused_resources()) == lifts


def test_health_pause_stale_gap_resets_with_or_without_a_tick(env):
    from ttp import heal, pauseends
    for tick in (True, False):
        p, d, _ = _health_pause(env, every_s=300)
        now = time.time()
        for dt in (0, 300):
            heal.run(d, "svc", _heal_spec(p), now + dt)
        if tick:   # the daemon saw the gap: the stale window is dropped
            d._pause_ends.tick(now + 3600)
            assert not p.db.kv(pauseends.HEALTH_KEY)
        heal.run(d, "svc", _heal_spec(p), now + 3600)   # after downtime: one fresh result only
        d._pause_ends.tick(now + 3600)
        assert "board" in p.db.paused_resources()
        assert p.db.kv(pauseends.HEALTH_KEY)["board"]["n"] == 1
        p.db.x("DELETE FROM kv")


def test_health_pause_disabled_then_reenabled_check_starts_over(env):
    from ttp import heal, pauseends
    p, d, _ = _health_pause(env, every_s=300)
    now = time.time()
    for dt in (0, 300, 600):
        heal.run(d, "svc", _heal_spec(p), now + dt)
    p.db.x("UPDATE schedules SET enabled=0 WHERE name='svc'")
    d._pause_ends.tick(now + 700)
    assert not p.db.kv(pauseends.HEALTH_KEY)
    p.db.x("UPDATE schedules SET enabled=1 WHERE name='svc'")
    for dt in (900, 1200, 1500, 1800):
        heal.run(d, "svc", _heal_spec(p), now + dt)
        d._pause_ends.tick(now + dt)
    assert "board" in p.db.paused_resources() and p.db.kv(pauseends.HEALTH_KEY)["board"]["n"] == 4


@pytest.mark.parametrize("by", ["coordinator", "user"])
@pytest.mark.parametrize("evidence", ["none", "stale", "disabled"])
def test_health_pause_until_is_reported_once_without_health(env, by, evidence):
    from ttp import coordinator as coord, heal
    from ttp.daemon import Daemon
    p, d, _ = _health_pause(env)
    now = time.time()
    coord.pause_resource(p, "board", True, by=by,
                         end={"until": now + 60, "end_when": {"heal": "svc", "healthy_n": 3, "clean_s": 1800}})
    if evidence == "stale":
        heal.run(d, "svc", _heal_spec(p), now - 7200)
    elif evidence == "disabled":
        p.db.x("UPDATE schedules SET enabled=0 WHERE name='svc'")
    d._pause_ends.tick(now)
    assert not p.db.q("SELECT * FROM events WHERE kind IN ('pause_end_due','observation')")
    d._pause_ends.tick(now + 120)
    Daemon(p.base)._pause_ends.tick(now + 240)   # a restart does not report it again
    kind = "pause_end_due" if by == "coordinator" else "observation"
    (event,) = p.db.q("SELECT * FROM events WHERE kind IN ('pause_end_due','observation')")
    assert event["kind"] == kind and "end time" in event["text"]
    assert p.db.paused_resources()["board"]["by"] == by   # a deadline never lifts it by itself


def test_health_pause_until_then_health_release_each_once(env):
    from ttp import coordinator as coord, heal, sweep
    p, d, _ = _health_pause(env)
    now = time.time()
    coord.pause_resource(p, "board", True, by="coordinator",
                         end={"until": now + 60, "end_when": {"heal": "svc", "healthy_n": 3, "clean_s": 1800}})
    for dt in (0, 900, 1800):
        heal.run(d, "svc", _heal_spec(p), now + dt)
        d._pause_ends.tick(now + dt + 120)
    assert "board" not in p.db.paused_resources()
    assert len(p.db.q("SELECT * FROM events WHERE kind='pause_end_due'")) == 1
    assert len(p.db.q("SELECT * FROM events WHERE kind='pause_ended'")) == 1
    assert [s["why"] for s in p.db.kv(sweep.SIGNAL_KEY)] == ["pause ended: board"]


def _released_once(p):
    from ttp import pauseends, sweep
    assert len(p.db.q("SELECT * FROM events WHERE kind='pause_ended'")) == 1
    assert [s["why"] for s in p.db.kv(sweep.SIGNAL_KEY)] == ["pause ended: board"]
    assert not p.db.kv(pauseends.RELEASE_KEY)


@pytest.mark.parametrize("cut", ["before_removal", "after_removal", "after_commit"])
def test_health_pause_shared_release_survives_interruption(env, monkeypatch, cut):
    from ttp import heal, pauseends, shared, sweep
    from ttp.daemon import Daemon
    p, d, _ = _health_pause(env, shared=True)
    now = time.time()
    for dt in (0, 900, 1800):
        heal.run(d, "svc", _heal_spec(p), now + dt)
    signal, update = sweep.signal, shared.update_pause
    if cut == "before_removal":   # the receipt is saved, then the file write is cut off
        def cut_short(name, fn):
            fn(shared.read_pause(name))
            raise RuntimeError("interrupted before removal")
        monkeypatch.setattr(shared, "update_pause", cut_short)
    elif cut == "after_removal":   # the file is gone, the database commit is cut off
        monkeypatch.setattr(sweep, "signal", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("interrupted")))
    if cut == "after_commit":
        d._pause_ends.tick(now + 1800)
    else:
        with pytest.raises(RuntimeError):
            d._pause_ends.tick(now + 1800)
    monkeypatch.setattr(sweep, "signal", signal)
    monkeypatch.setattr(shared, "update_pause", update)
    assert (shared.read_pause("board") is None) == (cut != "before_removal")
    for _ in range(2):   # recovery runs once, however often the restarted daemon ticks
        restarted = Daemon(p.base)
        restarted.sync_shared_pauses()
        restarted._pause_ends.tick(now + 1800)
    assert shared.read_pause("board") is None
    _released_once(p)


def test_health_pause_shared_recovery_keeps_a_replacement_users_pause(env, monkeypatch):
    from ttp import coordinator as coord, heal, shared, sweep
    from ttp.daemon import Daemon
    p, d, _ = _health_pause(env, shared=True)
    now = time.time()
    for dt in (0, 900, 1800):
        heal.run(d, "svc", _heal_spec(p), now + dt)
    signal = sweep.signal
    monkeypatch.setattr(sweep, "signal", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError):
        d._pause_ends.tick(now + 1800)
    monkeypatch.setattr(sweep, "signal", signal)
    coord.pause_resource(p, "board", True, reason="mine now", by="user", end={"until": now + 86400})
    restarted = Daemon(p.base)
    restarted._pause_ends.tick(now + 1800)
    restarted._pause_ends.tick(now + 1900)
    assert shared.read_pause("board")["by"] == "user"
    _released_once(p)


def test_health_pause_shared_replaced_in_place_before_removal_is_not_a_release(env, monkeypatch):
    from ttp import coordinator as coord, heal, pauseends, shared, sweep
    from ttp.daemon import Daemon
    p, d, _ = _health_pause(env, shared=True)
    now = time.time()
    for dt in (0, 900, 1800):
        heal.run(d, "svc", _heal_spec(p), now + dt)
    update = shared.update_pause
    def cut_short(name, fn):
        fn(shared.read_pause(name))
        raise RuntimeError("interrupted before removal")
    monkeypatch.setattr(shared, "update_pause", cut_short)
    with pytest.raises(RuntimeError):
        d._pause_ends.tick(now + 1800)
    monkeypatch.setattr(shared, "update_pause", update)
    coord.pause_resource(p, "board", True, reason="still mine", by="user", end={"until": now + 86400})
    Daemon(p.base)._pause_ends.tick(now + 1800)
    assert shared.read_pause("board")["by"] == "user" and not p.db.kv(pauseends.RELEASE_KEY)
    assert not p.db.q("SELECT * FROM events WHERE kind='pause_ended'") and not p.db.kv(sweep.SIGNAL_KEY)


def test_an_outage_check_whose_resource_is_paused_neither_escalates_nor_reports_the_machine(env):
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, up = _heal_setup(env, resource="box-a-device", outage=True, fix=None, grace_s=0)
    spec, now = _heal_spec(p), time.time()
    p.db.set_kv("paused_resources", {"box-a-device": {"reason": "box out", "since": now, "by": "coordinator"}})
    for dt in (0, 3600, 3 * heal.OUTAGE_S):
        assert heal.run(d, "svc", spec, now + dt) == ("unhealthy (box-a-device paused: not escalated)", None)
    dark = dict(spec, check="exit 255")
    p.db.set_kv(heal.STATE_KEY + "svc", {})
    for dt in (0, 2 * heal.OUTAGE_S):
        heal.run(d, "svc", dark, now + dt)
    assert ml.conditions() == {}
    assert not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert'")
    # The pause lifted: the same outage escalates and reports the machine down.
    p.db.set_kv("paused_resources", {})
    assert "paused" not in heal.run(d, "svc", spec, now + 3 * heal.OUTAGE_S + 60)[0]
    assert "box-a:down" in ml.conditions()

def test_ttr_records_one_incident_per_machine_from_conditions_outages_and_heal_checks(env):
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import schedule as sched
    from ttp import ttr
    p = make(env, "alpha")
    for a in ("box-a", "box-b", "box-c", "box-d"):
        mm.add(a)
    _resources(p, "Machines: box-a, box-c")
    t0 = 1_800_000_000.0
    # A co-tenant's down condition on a watched machine opens one incident, from its first report.
    ml.report("other", "box-a", "down", "no ping", now=t0)
    assert ttr.observe(p, t0 + 30) == (["box-a"], [])
    assert ttr.observe(p, t0 + 90) == ([], []), "still down: no second incident"
    # A machine its own heal check reports is watched too (box-b is not in Resources); box-d is watched by nobody.
    sched.upsert(p.db, "box-b-up", "command", "10m", payload={"heal": {"check": "true", "machine": "box-b"}})
    p.db.set_kv(heal.STATE_KEY + "box-b-up", {"status": "unhealthy", "since": t0 + 60,
                                               "machine_reported": ["box-b", "down"]})
    ml.report("other", "box-d", "down", now=t0)
    # A known outage opens one as well.
    ml.set_outage("box-c", "fan repair", "other", expires="1d", now=t0 + 100)
    assert ttr.observe(p, t0 + 120) == (["box-b", "box-c"], [])
    rows = {r["alias"]: r for r in p.db.q("SELECT * FROM ttr_incidents")}
    assert set(rows) == {"box-a", "box-b", "box-c"}
    assert (rows["box-a"]["kind"], rows["box-a"]["started"]) == ("down", t0)
    assert "no ping" in rows["box-a"]["cause"] and "other" in rows["box-a"]["cause"]
    assert (rows["box-b"]["kind"], rows["box-b"]["cause"], rows["box-b"]["started"]) == ("down", "heal check box-b-up", t0 + 60)
    assert (rows["box-c"]["kind"], rows["box-c"]["started"]) == ("outage", t0 + 100)
    # Healthy again: each ends once, at the tick that sees it.
    ml.report("other", "box-a", "down", cleared=True, now=t0 + 600)
    p.db.set_kv(heal.STATE_KEY + "box-b-up", {"status": "healthy"})
    ml.clear_outage("box-c")
    assert ttr.observe(p, t0 + 660) == ([], ["box-a", "box-b", "box-c"])
    assert ttr.observe(p, t0 + 720) == ([], [])
    assert [r["ended"] for r in p.db.q("SELECT ended FROM ttr_incidents ORDER BY alias")] == [t0 + 660] * 3
    # Down again with an old first report: a new incident that starts no earlier than the last one ended.
    ml.report("other", "box-a", "held", now=t0 + 800)
    p.db.x("UPDATE ttr_incidents SET ended=? WHERE alias='box-a'", (t0 + 900,))   # (a late end, for the bound)
    assert ttr.observe(p, t0 + 950) == (["box-a"], [])
    r = p.db.one("SELECT * FROM ttr_incidents WHERE alias='box-a' AND ended IS NULL")
    assert (r["kind"], r["started"]) == ("held", t0 + 900)
    # A condition nobody reported for CONDITION_GONE_S is over.
    assert ttr.observe(p, t0 + 800 + ml.CONDITION_GONE_S) == ([], ["box-a"])


def test_ttr_ignores_a_frozen_heal_report_while_the_project_is_paused(env):
    from ttp import heal
    from ttp import machines as mm
    from ttp import schedule as sched
    from ttp import ttr
    p = make(env, "alpha")
    mm.add("box-b")
    t0 = 1_800_000_000.0
    sched.upsert(p.db, "box-b-up", "command", "10m", payload={"heal": {"check": "true", "machine": "box-b"}})
    p.db.set_kv(heal.STATE_KEY + "box-b-up", {"status": "unhealthy", "since": t0, "last_check": t0,
                                               "machine_reported": ["box-b", "down"]})
    assert ttr.observe(p, t0 + 60) == (["box-b"], [])
    # Paused: heal checks stop, so the report freezes. Within its period plus the margin it still counts.
    p.db.set_kv("paused", True)
    assert ttr.observe(p, t0 + 600 + ttr.HEAL_STALE_MARGIN_S - 1) == ([], [])
    # Past that the frozen report is ignored, so the pause does not stretch the incident.
    late = t0 + 600 + ttr.HEAL_STALE_MARGIN_S + 1
    assert ttr.observe(p, late) == ([], ["box-b"])
    # (It ends at its last unhealthy sample plus the check's period, no later than that tick.)
    assert p.db.one("SELECT ended FROM ttr_incidents WHERE alias='box-b'")["ended"] == t0 + 600
    # Not paused: the same stale report counts again (the check runs and keeps it current).
    p.db.set_kv("paused", False)
    assert ttr.observe(p, late + 60) == (["box-b"], [])


def test_ttr_incident_hands_over_to_the_source_that_keeps_the_machine_down(env):
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import schedule as sched
    from ttp import ttr
    p = make(env, "alpha")
    mm.add("box-b")
    t0 = 1_800_000_000.0
    sched.upsert(p.db, "box-b-up", "command", "10m", payload={"heal": {"check": "true", "machine": "box-b"}})
    p.db.set_kv(heal.STATE_KEY + "box-b-up", {"status": "unhealthy", "since": t0, "last_check": t0,
                                               "machine_reported": ["box-b", "down"]})
    assert ttr.observe(p, t0 + 60) == (["box-b"], [])
    # A known outage recorded while the check still fails: the heal check stays the incident's source.
    ml.set_outage("box-b", "board swap", "other", expires="7d", now=t0 + 300)
    assert ttr.observe(p, t0 + 360) == ([], [])
    # The same check turning held keeps the incident (same source).
    p.db.set_kv(heal.STATE_KEY + "box-b-up", {"status": "unhealthy", "since": t0, "last_check": t0 + 400,
                                               "machine_reported": ["box-b", "held"]})
    assert ttr.observe(p, t0 + 420) == ([], [])
    # The check recovers while the outage keeps the machine down: the heal incident ends when the outage
    # began and an outage incident opens from its own since, with its own cause.
    p.db.set_kv(heal.STATE_KEY + "box-b-up", {"status": "healthy", "last_check": t0 + 600})
    assert ttr.observe(p, t0 + 660) == (["box-b"], ["box-b"])
    rows = p.db.q("SELECT * FROM ttr_incidents ORDER BY id")
    assert [(r["kind"], r["cause"], r["started"], r["ended"]) for r in rows] == [
        ("down", "heal check box-b-up", t0, t0 + 300),
        ("outage", "known outage: board swap", t0 + 300, None)]
    assert ttr.observe(p, t0 + 720) == ([], []), "no further handover while the outage holds"
    text = ttr.lines(p.db, "UTC", now=t0 + 720)[0]
    assert "open: outage since" in text and "board swap" in text
    ml.clear_outage("box-b")
    assert ttr.observe(p, t0 + 780) == ([], ["box-b"])
    assert p.db.one("SELECT ended FROM ttr_incidents WHERE kind='outage'")["ended"] == t0 + 780


def test_ttr_labels_incidents_in_this_projects_test_changes_and_keeps_them_out_of_the_stats(env, monkeypatch, capsys):
    """An incident starting inside an open or closed `--test` change of the same project on the same machine
    is a test, labelled at report time (rows recorded before too): out of the counts and median, p90 and
    max, and listed apart. A non-test entry, another project's or another machine's does not relabel one."""
    from ttp import cli
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import ttr
    p = make(env, "alpha")
    for a in ("box-a", "box-c"):
        mm.add(a)
    t0 = time.time() - 86400
    closed = ml.add_change("box-a", "restarts the idle broker for a test", "systemctl start svc", "alpha",
                           expires="1d", now=t0, test=True)
    ml.close_change(closed["id"], "done", "alpha", now=t0 + 1000)
    plain = ml.add_change("box-a", "a drop-in", "rm drop-in", "alpha", expires="1d", now=t0 + 2000)
    other = ml.add_change("box-a", "a test of theirs", "true", "beta", expires="1d", now=t0 + 3000, test=True)
    ml.close_change(other["id"], "done", "beta", now=t0 + 3800)
    elsewhere = ml.add_change("box-c", "a test elsewhere", "true", "alpha", expires="1d", now=t0 + 4000, test=True)
    opened = ml.add_change("box-a", "another test", "true", "alpha", expires="1d", now=t0 + 5000, test=True)
    got = {c["id"]: c for c in ml.changes(open_only=False)}
    assert got[closed["id"]]["test"] and got[closed["id"]]["closed"] == t0 + 1000, "the flag survives the close"
    assert "test" not in got[plain["id"]] and "a test" not in ml.change_line(got[plain["id"]], t0)
    assert ml.change_line(got[opened["id"]], t0).count("; a test") == 1
    # Rows recorded before any of this (no sample columns filled): labelled at report time.
    rows = [(t0 + 100, t0 + 1060), (t0 + 1500, t0 + 1600), (t0 + 2100, t0 + 2400), (t0 + 3100, t0 + 3700),
            (t0 + 4100, t0 + 4200), (t0 + 5100, None)]
    ids = [p.db.x("INSERT INTO ttr_incidents(alias, kind, cause, started, ended) VALUES('box-a','held',?,?,?)",
                  (f"heal check up{i}", s, e)) for i, (s, e) in enumerate(rows)]
    now = t0 + 6000
    s = ttr.report(p.db, now=now, project="alpha")["box-a"]
    assert (s["incidents"], s["recovered"]) == (4, 4)
    assert (s["median_s"], s["p90_s"], s["max_s"]) == (200, 600, 600)
    assert s["open"] == [] and s["last"]["id"] == ids[4]
    assert [(t["id"], t["kind"], t["source_kind"], t["change"], t["ttr_s"]) for t in s["tests"]] == [
        (ids[0], "test", "held", closed["id"], 960), (ids[5], "test", "held", opened["id"], None)]
    # Without a project nothing is relabelled; another project sees only its own test changes.
    assert ttr.report(p.db, now=now)["box-a"]["incidents"] == 6
    beta = ttr.report(p.db, now=now, project="beta")["box-a"]
    assert [t["id"] for t in beta["tests"]] == [ids[3]] and beta["incidents"] == 5
    text = ttr.lines(p.db, "UTC", now=now, project="alpha")[0]
    assert text.startswith("box-a: 4 incidents in 30 d; TTR median 3 min, p90 10 min, max 10 min")
    assert "tests (not in TTR): 2: held " in text and f"16 min, change #{closed['id']}" in text
    assert f"open, change #{opened['id']}" in text
    assert ttr.review_lines(p, now=now) == [ttr.line("box-a", ttr.report(p.db, days=ttr.REVIEW_DAYS, now=now,
                                                                        project="alpha")["box-a"], p, ttr.REVIEW_DAYS)]
    assert "tests (not in TTR): 2" in ttr.review_lines(p, now=now)[0]
    # A machine with only test incidents: no TTR and no last one, the tests still listed.
    p.db.x("DELETE FROM ttr_incidents WHERE id NOT IN (?, ?)", (ids[0], ids[5]))
    only = ttr.report(p.db, now=now, project="alpha")["box-a"]
    assert (only["incidents"], only["last"], len(only["tests"])) == (0, None, 2)
    assert ttr.line("box-a", only, "UTC", 30).startswith("box-a: 0 incidents in 30 d; tests (not in TTR): 2: ")
    # More tests than TEST_LISTED: the latest are listed, the rest counted.
    for i in range(ttr.TEST_LISTED + 1):
        p.db.x("INSERT INTO ttr_incidents(alias, kind, cause, started, ended) VALUES('box-a','down','x',?,?)",
               (t0 + 200 + i, t0 + 300 + i))
    many = ttr.line("box-a", ttr.report(p.db, now=now, project="alpha")["box-a"], "UTC", 30)
    assert f"tests (not in TTR): {ttr.TEST_LISTED + 3}: " in many and "; 3 earlier" in many
    # The CLI: --test records the flag, the listing shows it, and ttp ttr labels by the project it runs in.
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.chdir(p.root)
    cli.main(["machines", "change", "add", "box-c", "restarts a service for a test", "--undo", "true",
              "--expires", "1h", "--test"])
    assert "; a test" in capsys.readouterr().out and ml.changes()[-1]["test"] is True
    cli.main(["machines", "change", "list", "box-c"])
    assert capsys.readouterr().out.count("; a test") == 2
    cli.main(["ttr", "--json"])
    listed = [t["id"] for t in json.loads(capsys.readouterr().out)["machines"]["box-a"]["tests"]]
    assert len(listed) == ttr.TEST_LISTED + 3 and {ids[0], ids[5]} <= set(listed), "--json lists every test"
    del elsewhere, other


def test_ttr_relabels_incidents_once_a_change_is_flagged_a_test_after_the_fact(env, monkeypatch, capsys):
    """`ttp machines change edit <id> --test` flags a change, open or closed, after the fact: incidents already
    recorded inside its window become tests at the next report, with an edit trail; `--no-test` restores them.
    Only the project that recorded it or the machine's recovery owner may flip it; outside the window nothing moves."""
    from ttp import cli
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import ttr
    p = make(env, "alpha")
    mm.add("box-a")
    t0 = time.time() - 86400
    c = ml.add_change("box-a", "a repro driver", "true", "alpha", expires="1d", now=t0)
    ml.close_change(c["id"], "done", "alpha", now=t0 + 1000)
    inside = p.db.x("INSERT INTO ttr_incidents(alias, kind, cause, started, ended) VALUES('box-a','down','x',?,?)",
                    (t0 + 100, t0 + 400))
    outside = p.db.x("INSERT INTO ttr_incidents(alias, kind, cause, started, ended) VALUES('box-a','held','y',?,?)",
                     (t0 + 2000, t0 + 2300))
    now = t0 + 3000
    before = ttr.report(p.db, now=now, project="alpha")["box-a"]
    assert (before["incidents"], before["tests"]) == (2, [])
    # Another project, not the machine's recovery owner, is refused; nothing is written.
    with pytest.raises(ValueError, match="recorded by alpha: only that project may flip its test flag, not beta"):
        ml.set_test(c["id"], "beta", True, now=now)
    assert "edits" not in ml.changes(open_only=False)[0]
    got = ml.set_test(c["id"], "alpha", True, now=now)
    assert got["test"] is True and got["closed"] == t0 + 1000 and got["what"] == "a repro driver"
    assert got["edits"] == [{"at": now, "by": "alpha", "was": {"test": False}, "test": True}]
    s = ttr.report(p.db, now=now, project="alpha")["box-a"]
    assert s["incidents"] == 1 and s["last"]["id"] == outside
    assert [(t["id"], t["source_kind"], t["change"]) for t in s["tests"]] == [(inside, "down", c["id"])]
    with pytest.raises(ValueError, match="already flagged"):
        ml.set_test(c["id"], "alpha", True, now=now)
    # The machine's recovery owner may clear it; the trail keeps both edits.
    mm.set_recovery("box-a", owner="gamma")
    with pytest.raises(ValueError, match=r"or box-a's recovery owner \(gamma\)"):
        ml.set_test(c["id"], "beta", False, now=now)
    got = ml.set_test(c["id"], "gamma", False, now=now + 10)
    assert "test" not in got and got["edits"][-1] == {"at": now + 10, "by": "gamma", "was": {"test": True},
                                                       "test": False}
    restored = ttr.report(p.db, now=now, project="alpha")["box-a"]
    assert (restored["incidents"], restored["tests"]) == (2, [])
    # The CLI: inside the project, --test flips it; another project's --by is refused with a clear message.
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.chdir(p.root)
    cli.main(["machines", "change", "edit", str(c["id"]), "--test"])
    assert "now flagged as a test" in capsys.readouterr().out
    assert ml.changes(open_only=False)[0]["edits"][-1]["by"] == "alpha"
    with pytest.raises(SystemExit):
        cli.main(["machines", "change", "edit", str(c["id"]), "--no-test", "--by", "beta"])
    assert "only that project or box-a's recovery owner (gamma) may flip" in capsys.readouterr().err
    cli.main(["machines", "change", "edit", str(c["id"]), "--no-test"])
    assert "no longer flagged" in capsys.readouterr().out and "test" not in ml.changes(open_only=False)[0]


def test_machine_change_add_since_backdates_its_start_into_ttr_windows(env, monkeypatch, capsys):
    """`ttp machines change add --since` backdates a new change's start (a delay back or an ISO time in the home
    zone): an incident since then is a test of a --test change. A future time or one past SINCE_MAX_S is refused."""
    from ttp import cli
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import ttr
    p = make(env, "alpha")
    mm.add("box-a")
    now = 1_800_000_000.0
    assert ml.parse_since("2h", now) == now - 7200
    assert ml.parse_since("2027-01-14 22:00", now, "America/Los_Angeles") == now - 7200   # 06:00 UTC
    assert ml.parse_since("2027-01-15T06:00Z", now, "America/Los_Angeles") == now - 7200
    for bad, why in (("2027-01-15 09:00", "in the future"), ("8d", "more than 7 days back"), ("soon", "delay back")):
        with pytest.raises(ValueError, match=why):
            ml.parse_since(bad, now, "UTC")
    with pytest.raises(ValueError, match="within the last 7 days"):
        ml.add_change("box-a", "x", "true", "alpha", expires="1d", now=now, since=now + 60)
    t = time.time()
    early = p.db.x("INSERT INTO ttr_incidents(alias, kind, cause, started, ended) VALUES('box-a','down','x',?,?)",
                   (t - 3 * 3600, t - 2.5 * 3600))
    inside = p.db.x("INSERT INTO ttr_incidents(alias, kind, cause, started, ended) VALUES('box-a','held','y',?,?)",
                    (t - 1800, t - 1200))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.chdir(p.root)
    cli.main(["machines", "change", "add", "box-a", "a repro driver", "--undo", "true", "--expires", "1h",
              "--test", "--since", "2h"])
    out = capsys.readouterr().out
    c = ml.changes()[-1]
    assert "; backdated to " in out and abs(c["since"] - (t - 7200)) < 60 and c["added"] >= t
    s = ttr.report(p.db, project="alpha")["box-a"]
    assert [x["id"] for x in s["tests"]] == [inside] and s["last"]["id"] == early
    for bad, why in (("1h", "future"), ("8d", "more than 7 days back")):
        with pytest.raises(SystemExit):
            cli.main(["machines", "change", "add", "box-a", "x", "--undo", "true", "--expires", "1h",
                      "--since", bad if bad != "1h" else "2099-01-01T00:00"])
        assert why in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["machines", "change", "add", "box-a", "x", "--power-cycle-at", "1h", "--since", "1h"])
    assert "cannot be backdated" in capsys.readouterr().err
    assert len(ml.changes()) == 1


def test_machine_change_test_flag_is_queued_from_a_read_only_ledger(env, monkeypatch):
    """From a sandbox that keeps the shared ledger read-only, a flag edit is queued for the project's daemon,
    which sets it (once, with its trail) on its next tick, keeping the ownership rule."""
    import errno
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env, "alpha")
    mm.add("box-a")
    c = ml.add_change("box-a", "a repro driver", "true", "alpha", expires="1d")
    real = ml._write

    def ro(*a, **k):
        raise OSError(errno.EROFS, "Read-only file system")
    monkeypatch.setattr(ml, "_write", ro)
    req = ml.set_test(c["id"], "alpha", True, now=100.0, queue=p.state)
    assert req["op"] == "test" and req["queued"] and ml.set_test(c["id"], "alpha", True, queue=p.state)["queued"] == req["queued"]
    with pytest.raises(ValueError, match="only that project"):
        ml.set_test(c["id"], "beta", True, queue=p.state)
    monkeypatch.setattr(ml, "_write", real)
    done = ml.apply_requests(p.state, log=lambda _m: None)
    assert [d["id"] for d in done] == [c["id"]] and ml.pending(p.state) == []
    got = ml.changes()[0]
    assert got["test"] is True and got["edits"] == [{"at": 100.0, "by": "alpha", "was": {"test": False}, "test": True}]


def test_ttr_caps_a_heal_incident_at_its_last_unhealthy_sample_plus_the_check_period(env, tmp_path):
    """A sparse check no longer stretches a short fault: a heal-sourced incident ends no later than the
    check's last unhealthy sample plus its period; another source keeping it open clears the cap, and rows
    from before the samples were recorded (and databases from before the columns) keep the old end."""
    from ttp import heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import schedule as sched
    from ttp import ttr
    from ttp.db import DB
    p = make(env, "alpha")
    mm.add("box-b")
    t0 = 1_800_000_000.0
    sched.upsert(p.db, "box-b-up", "command", "10m", payload={"heal": {"check": "true", "machine": "box-b"}})
    key = heal.STATE_KEY + "box-b-up"
    p.db.set_kv(key, {"status": "unhealthy", "since": t0, "last_check": t0, "machine_reported": ["box-b", "held"]})
    assert ttr.observe(p, t0 + 60) == (["box-b"], [])
    r = p.db.one("SELECT * FROM ttr_incidents")
    assert (r["last_bad"], r["period_s"]) == (t0, 600)
    p.db.set_kv(key, {"status": "unhealthy", "since": t0, "last_check": t0 + 600, "machine_reported": ["box-b", "held"]})
    assert ttr.observe(p, t0 + 660) == ([], [])
    assert p.db.one("SELECT last_bad FROM ttr_incidents")["last_bad"] == t0 + 600
    # The recovering sample comes late (t0 + 1500), and the tick later still: the end is capped.
    p.db.set_kv(key, {"status": "healthy", "last_check": t0 + 1500})
    assert ttr.observe(p, t0 + 1560) == ([], ["box-b"])
    assert p.db.one("SELECT ended FROM ttr_incidents")["ended"] == t0 + 1200
    # Another source of the same kind keeps it open after the check recovers: no cap, it ends at its tick.
    p.db.set_kv(key, {"status": "unhealthy", "since": t0 + 2000, "last_check": t0 + 2000,
                      "machine_reported": ["box-b", "held"]})
    assert ttr.observe(p, t0 + 2060) == (["box-b"], [])
    ml.report("other", "box-b", "held", "slow", now=t0 + 2100)
    p.db.set_kv(key, {"status": "healthy", "last_check": t0 + 2600})
    assert ttr.observe(p, t0 + 2660) == ([], [])
    r = p.db.one("SELECT * FROM ttr_incidents WHERE ended IS NULL")
    assert (r["last_bad"], r["period_s"]) == (None, None)
    ml.report("other", "box-b", "held", cleared=True, now=t0 + 4000)
    assert ttr.observe(p, t0 + 4060) == ([], ["box-b"])
    assert p.db.one("SELECT ended FROM ttr_incidents WHERE id=?", (r["id"],))["ended"] == t0 + 4060
    # The cap never ends one before it started.
    p.db.x("INSERT INTO ttr_incidents(alias, kind, cause, started, last_bad, period_s) "
           "VALUES('box-b','down','heal check box-b-up',?,?,?)", (t0 + 5000, t0 + 4000, 600))
    assert ttr.observe(p, t0 + 5100) == ([], ["box-b"])
    assert p.db.one("SELECT ended FROM ttr_incidents WHERE started=?", (t0 + 5000,))["ended"] == t0 + 5000
    # An old open row without samples ends at the tick that sees the machine healthy.
    p.db.x("INSERT INTO ttr_incidents(alias, kind, cause, started) VALUES('box-b','down','heal check box-b-up',?)",
           (t0 + 6000,))
    assert ttr.observe(p, t0 + 9000) == ([], ["box-b"])
    assert p.db.one("SELECT ended FROM ttr_incidents WHERE started=?", (t0 + 6000,))["ended"] == t0 + 9000
    # A database from before the columns gains them and keeps its rows.
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE ttr_incidents (id INTEGER PRIMARY KEY AUTOINCREMENT, alias TEXT NOT NULL, "
                "kind TEXT NOT NULL, cause TEXT, started REAL NOT NULL, ended REAL)")
    con.execute("INSERT INTO ttr_incidents(alias, kind, started, ended) VALUES('box-b','down',1,61)")
    con.commit()
    con.close()
    db = DB(old)
    [row] = db.q("SELECT * FROM ttr_incidents")
    assert (row["ended"], row["last_bad"], row["period_s"]) == (61, None, None)
    assert ttr.report(db, now=100, days=1)["box-b"]["max_s"] == 60


def test_goal_service_up_passes_fails_and_reports_could_not_run(env, tmp_path, monkeypatch):
    import http.server
    from ttp import goalchecks as gc, heal
    replies = {"/ok": (200, b"ready: yes"), "/down": (503, b"no")}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code, body = replies.get(self.path, (404, b""))
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        [up] = gc.service_up(url=f"{base}/ok", body_contains="ready")
        assert up["ok"] and up["status"] == 200 and up["key"] == f"service_up:{base}/ok" and "error" not in up, up
        assert gc.service_up(url=f"{base}/down", expect=[503])[0]["ok"]
        # a non-2xx status's body is read too
        assert gc.service_up(url=f"{base}/down", expect=[503], body_contains="no")[0]["ok"]
        assert not gc.service_up(url=f"{base}/down", expect=[503], body_contains="ready")[0]["ok"]
        for kwargs in ({"url": f"{base}/down"}, {"url": f"{base}/ok", "body_contains": "never"},
                       {"url": f"{base}/ok", "expect": [204]}):
            [down] = gc.service_up(**kwargs)
            assert not down["ok"] and "error" not in down, (kwargs, down)
    finally:
        srv.shutdown()
        srv.server_close()
    [gone] = gc.service_up(url=base)
    assert not gone["ok"] and "error" not in gone and "does not answer" in gone["text"], gone
    [bad] = gc.service_up(url="not a url")
    assert bad["error"] is True
    page = tmp_path / "page.html"
    page.write_text("x")
    [local] = gc.service_up(url=page.as_uri())
    assert local["error"] is True and local["ok"] is False and gc.exit_code([local]) == gc.UNKNOWN, local
    # the unit: systemctl's answer decides; systemctl itself failing is could-not-run
    shown = {}

    def fake_shell(cmd, cwd, timeout, env=None):
        shown["cmd"] = cmd
        return shown["reply"]
    monkeypatch.setattr(heal, "_shell", fake_shell)
    for reply, ok in (((0, "ActiveState=active\nNRestarts=0"), True), ((0, "ActiveState=failed\nNRestarts=2"), False),
                      ((1, "Failed to connect to bus"), None)):
        shown["reply"] = reply
        [o] = gc.service_up(unit="demo.service", user=True)
        assert o["key"] == "service_up:demo.service" and "--user" in shown["cmd"], (o, shown)
        assert (o["ok"], o.get("error")) == ((True, None) if ok else (False, None) if ok is False else (False, True)), o
    shown["reply"] = (0, "ActiveState=active\nNRestarts=0")
    both = gc.service_up(unit="demo.service", url=base)
    assert [o["ok"] for o in both] == [True, False] and gc.exit_code(both) == 1
    [none] = gc.service_up()
    assert none["error"] is True and gc.exit_code([none]) == 75


def test_heal_to_pages_another_project_once_per_episode_instead_of_escalating(env):
    from ttp import heal
    with pytest.raises(ValueError, match="`to`"):
        heal.validate({"check": "true", "to": 5})
    p, d, up = _heal_setup(env, fix=None, to="ops")
    _peer(env, "ops")
    spec, now = _heal_spec(p), time.time()
    _heal_to_self_fix_failed(p, d, up, spec, now)
    for i in range(3):
        assert "ops told" in heal.run(d, "svc", spec, now + 10 + i)[0]
    notes = _notes_to("ops")
    assert len(notes) == 1 and notes[0]["severity"] == "high" and "Heal check svc of project demo, unhealthy since" in notes[0]["spec"]
    assert _heal_told(p) == ([], [])
    up.touch()   # healthy ends the episode; the next one pages again
    heal.run(d, "svc", spec, now + 30)
    up.unlink()
    _heal_to_self_fix_failed(p, d, up, spec, now + 200)
    heal.run(d, "svc", spec, now + 210)
    assert len(_notes_to("ops")) == 2
    # An unknown target escalates as without `to`.
    lost = dict(spec, to="nope")
    up.touch()
    heal.run(d, "svc", lost, now + 300)
    up.unlink()
    _heal_to_self_fix_failed(p, d, up, lost, now + 400)
    assert "alerted" in heal.run(d, "svc", lost, now + 410)[0]


def test_heal_to_gets_the_unknown_stretch_message_too(env):
    from ttp import heal
    p, d, up = _heal_setup(env, unknown_cap_s=600, to="ops")
    _peer(env, "ops")
    spec, now = _heal_spec(p), time.time()
    dark = dict(spec, check="exit 75")
    heal.run(d, "svc", dark, now)
    assert "ops told" in heal.run(d, "svc", dark, now + 700)[0]
    heal.run(d, "svc", dark, now + 800)
    notes = _notes_to("ops")
    assert len(notes) == 1 and "could not tell" in notes[0]["spec"] and _heal_told(p) == ([], [])


def test_heal_to_pages_again_once_a_known_run_or_a_fresh_episode_ends_the_last_one(env):
    """Its page marks clear wherever the unknown stretch or the episode they belong to ends: a known,
    owned run (`ttp-heal: known`) and a fresh episode after a long unknown stretch."""
    from ttp import heal
    p, d, up = _heal_setup(env, fix=None, unknown_cap_s=600, to="ops")
    _peer(env, "ops")
    spec, now = _heal_spec(p), time.time()
    dark = dict(spec, check="exit 75")
    owned = dict(spec, check="echo 'ttp-heal: known repair: board swap'; exit 75")
    heal.run(d, "svc", dark, now)
    assert "ops told" in heal.run(d, "svc", dark, now + 700)[0]
    assert "known" in heal.run(d, "svc", owned, now + 800)[0]   # ends that stretch
    heal.run(d, "svc", dark, now + 900)
    assert "ops told" in heal.run(d, "svc", dark, now + 1600)[0]
    assert len([n for n in _notes_to("ops") if "could not tell" in n["spec"]]) == 2
    _heal_to_self_fix_failed(p, d, up, spec, now + 1700)
    assert "ops told" in heal.run(d, "svc", spec, now + 1710)[0]
    heal.run(d, "svc", dict(dark, unknown_cap_s=0), now + 1800)   # a long unknown stretch, its task over
    _heal_to_self_fix_failed(p, d, up, spec, now + 3000)   # back unhealthy: a fresh episode
    assert "ops told" in heal.run(d, "svc", spec, now + 3010)[0]
    assert len([n for n in _notes_to("ops") if "still fails" in n["spec"]]) == 2


def test_heal_self_fix_note_is_appended_to_the_self_fix_spec_only_when_set(env):
    """A heal block's `self_fix_note` reaches its self-fix task under a label; without it the spec is
    unchanged; a non-text note is refused, a long one cut, and schedule_set keeps it."""
    from types import SimpleNamespace
    from ttp import heal
    from ttp import coordinator as coord
    p = make(env)
    now = time.time()
    st = {"since": now - 7200, "last_rc": 1}
    plain = heal.validate({"check": "false", "fix": "true"})
    assert "self_fix_note" not in plain
    heal._escalate(SimpleNamespace(p=p), "a", plain, dict(st), now, "the fix exited 1")
    noted = heal.validate({"check": "false", "fix": "true", "self_fix_note": "  Check the unit's env first.  "})
    assert noted["self_fix_note"] == "Check the unit's env first."
    heal._escalate(SimpleNamespace(p=p), "b", noted, dict(st), now, "the fix exited 1")
    heal._escalate(SimpleNamespace(p=p), "c", {**plain, "self_fix_note": ""}, dict(st), now, "the fix exited 1")
    specs = {r["title"].split()[3]: r["spec"]
             for r in p.db.q("SELECT title, spec FROM tasks WHERE title LIKE 'Self-fix: heal check %'")}
    assert "Project note for this check" not in specs["a"]
    assert specs["c"] == specs["a"].replace("`a`", "`c`").replace("heal test a", "heal test c")
    assert specs["b"].endswith("\n\nProject note for this check:\nCheck the unit's env first.")
    assert specs["b"].startswith(specs["a"].replace("`a`", "`b`").replace("heal test a", "heal test b"))
    for bad in (3, ["x"], {"x": 1}, True):
        with pytest.raises(ValueError, match="self_fix_note"):
            heal.validate({"check": "false", "self_fix_note": bad})
    assert len(heal.validate({"check": "false", "self_fix_note": "x" * 5000})["self_fix_note"]) == heal.NOTE_MAX
    action = {"type": "schedule_set", "name": "probe", "kind": "command", "every": "30m",
              "heal": {"check": "false", "self_fix_note": "note"}}
    assert coord.apply(p, [action]) == []
    stored = json.loads(p.db.one("SELECT payload FROM schedules WHERE name='probe'")["payload"])["heal"]
    assert heal.validate(stored)["self_fix_note"] == "note"


def test_heal_unknown_past_the_outage_window_honours_suppress(env):
    """A box shut down on purpose makes the check unknown: `suppress` exit 0 means no escalation and one log
    line; with the probe saying no (or absent) the outage escalates as before."""
    from ttp import heal
    down = env["repo"] / "planned-down"
    down.touch()
    p, d, up = _heal_setup(env, fix=None, outage=True, check="exit 255", suppress=f"test -e {down}")
    spec, now = _heal_spec(p), time.time()
    heal.run(d, "svc", spec, now)
    for t in (heal.OUTAGE_S, heal.OUTAGE_S + 600):
        assert heal.run(d, "svc", spec, now + t)[0] == "ok (unknown, suppressed: deliberately down)"
    assert not p.db.q("SELECT id FROM tasks WHERE origin='daemon'")
    assert (p.logs / "daemon.log").read_text().count("svc: suppressed: deliberately down") == 1
    down.unlink()
    heal.run(d, "svc", spec, now + heal.OUTAGE_S + 1200)
    assert heal.state(p.db, "svc").get("escalated") or p.db.q("SELECT id FROM tasks WHERE origin='daemon'")
    p2, d2, _ = _heal_setup(env, fix=None, outage=True, check="exit 255")
    spec2 = _heal_spec(p2)
    heal.run(d2, "svc", spec2, now)
    heal.run(d2, "svc", spec2, now + heal.OUTAGE_S)
    assert heal.state(p2.db, "svc").get("escalated") or p2.db.q("SELECT id FROM tasks WHERE origin='daemon'")


def test_project_machines_skip_an_alias_the_resources_section_only_prohibits(env, capsys):
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import responsibilities as rs
    from ttp.cli import main
    known = {"box-a": {}, "box-b": {}, "box-c": {}, "box-d": {}}
    charter = "# P\n\n## Resources\n{}\n\n## Restrictions\n- never box-c\n"
    # Positive mentions still count; a read-only note or a negating parenthesis keeps the alias itself.
    assert ml.charter_machines(charter.format("Machines: box-a, box-b (read only; never reset it)."),
                               known) == ["box-a", "box-b"]
    for line in ("No authorization for box-c or other shared nodes.", "Never use box-c.",
                 "box-c is excluded.", "box-c: prohibited", "Don't run on box-c.", "Not box-c.",
                 "Off-limits: box-c, box-d.", "box-c and box-d are excluded.", "No access to box-c, box-d."):
        assert ml.charter_machines(charter.format(f"Machines: box-a.\n{line}"), known) == ["box-a"], line
    # A negation that only limits how an authorized machine is used keeps it.
    for line in ("Device boxes: box-a, box-b, each through its broker, never directly.",
                 "Slurm nodes box-a and box-b, no more than 2 at a time.",
                 "box-a: the project host, not shared with other teams.", "Use box-a without sudo.",
                 "box-a and box-b: read-only, do not modify.", "No more than one job at a time on box-a.",
                 "Never directly, use box-a.", "Run without sudo on box-a.",
                 "Do not reboot box-a.", "Never reset box-a; use it for builds.",
                 "Not allowed to reboot box-a.", "Do not reboot or upgrade box-a."):
        want = ["box-a", "box-b"] if "box-b" in line else ["box-a"]
        assert ml.charter_machines(charter.format(line), known) == want, line
    # An alias named both ways counts; one named only in another section does not.
    assert ml.charter_machines(charter.format("Machines: box-a, box-c. Never reset box-c."),
                               known) == ["box-a", "box-c"]
    # An explicit allowlist in the config wins over the charter text.
    text = charter.format("Machines: box-a, box-b.")
    assert ml.project_machines(text, {"shared_resources": ["box-d-device"]}, known) == ["box-d"]
    assert ml.project_machines(text, {"resources": {"box-b-device": 1}}, known) == ["box-b"]
    assert ml.project_machines(text, {"resources": {"device": 1}, "shared_resources": "box-d"}, known) == ["box-a", "box-b"]
    # End to end: health coverage and `machines status` leave out the project that only prohibits it.
    mm.add("box-a", tags="device")
    mm.add("box-c", tags="device")
    me, other = make(env), _second_project(env, "gamma")
    _resources(me, "Machines: box-a.\nNo authorization for box-c or other shared nodes.")
    _resources(other, "Machines: box-c.")
    assert [i["name"] for i in rs.inventory(me) if i["kind"] == "machine"] == ["box-a"]
    main(["machines", "status", "box-c", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["projects"] == ["gamma"]
    me.set_config("shared_resources", ["box-c-device"])
    assert ml.resource_machines(me) == ["box-c"]
    main(["machines", "status", "box-c", "--json"])
    assert sorted(json.loads(capsys.readouterr().out)[0]["projects"]) == sorted([me.name, "gamma"])


def test_project_machines_and_idle_capacity_skip_retired_machines(env):
    """A machine whose list entry is decommissioned or says "Do not use", or that the charter's Resources
    calls retired, is not the project's: no idle-capacity candidate, even if an older paragraph names it."""
    from ttp import idlecap
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    charter = "# P\n\n## Resources\n{}\n"
    listed = charter.format("Machines: box-a, box-b, box-c.")
    known = {"box-a": {"tags": ["device"]}, "box-b": {"tags": ["device", "decommissioned"]},
             "box-c": {"tags": ["device"], "note": "Decommissioned last month. Do not use."}}
    assert ml.project_machines(listed, {}, known) == ["box-a"]
    # A config naming only retired machines names none, so the charter's live ones stand.
    assert ml.project_machines(listed, {"shared_resources": ["box-b-device", "box-c"]}, known) == ["box-a"]
    for note, gone in (("Do not use", True), ("do not use.", True), ("Don't use this box!", True),
                       ("Retired", True), ("Do not use for builds; device runs only", False),
                       ("not retired", False), ("build box", False), ("Not yet decommissioned", False),
                       ("Will be decommissioned next month", False), ("To be retired", False),
                       ("never retired; do not use sudo", False), ("Retired. Was to be retired in May", True)):
        assert mm.retired({"note": note}) is gone, note
    assert mm.retired({"tags": "x86,retired"}) and not mm.retired({}) and not mm.retired(None)
    # The charter's own Resources text retires an alias an older paragraph still lists.
    live = {a: {"tags": ["device"]} for a in ("box-a", "box-b", "box-c", "box-d")}
    for line, want in (("box-c is retired.", ["box-a", "box-b"]),
                       ("box-c (decommissioned).", ["box-a", "box-b"]),
                       ("box-c: do not use.", ["box-a", "box-b"]),
                       ("Retired: box-b, box-c.", ["box-a"]),
                       ("box-b and box-c are decommissioned.", ["box-a"]),
                       ("box-c was decommissioned last month.", ["box-a", "box-b"]),
                       ("box-b, box-c (decommissioned).", ["box-a", "box-b"]),
                       ("Do not use box-c.", ["box-a", "box-b"]),
                       ("Do not use: box-b, box-c.", ["box-a"]),
                       ("box-c: do not use it.", ["box-a", "box-b"]),
                       ("box-b (read only; never reset it).", ["box-a", "box-b", "box-c"]),
                       # A qualified, temporary, negated or future restriction keeps the machine.
                       ("box-c: do not use for builds.", ["box-a", "box-b", "box-c"]),
                       ("Do not use box-c while its live service runs.", ["box-a", "box-b", "box-c"]),
                       ("Do not use box-c during working hours.", ["box-a", "box-b", "box-c"]),
                       ("box-c: do not use sudo.", ["box-a", "box-b", "box-c"]),
                       ("box-c is out of service until Monday.", ["box-a", "box-b", "box-c"]),
                       ("box-c is out of service.", ["box-a", "box-b", "box-c"]),
                       ("box-c is not yet decommissioned.", ["box-a", "box-b", "box-c"]),
                       ("box-c will be decommissioned next month.", ["box-a", "box-b", "box-c"]),
                       ("To be retired: box-c.", ["box-a", "box-b", "box-c"]),
                       ("box-b and box-c are not retired.", ["box-a", "box-b", "box-c"])):
        got = ml.charter_machines(charter.format(f"Machines: box-a, box-b, box-c.\n\nLater: {line}"), live)
        assert got == want, (line, got)
    # Idle capacity: neither a retired machine nor its `<alias>-device` resource is a candidate.
    cfg = {"shared_resources": ["box-b-device", "box-a-device"]}
    assert idlecap.candidates(cfg, {"box-c-device"}, ml.project_machines(listed, {}, known), known) == {"box-a-device"}
    assert idlecap.candidates({}, set(), ["box-a", "box-b", "box-c"], known) == {"box-a"}


def test_health_coverage_counts_report_only_checks_named_by_schedule_and_known_outages(env):
    """A command watcher without a heal block covers the machine its schedule name, command or fields
    name, and the line names it report-only; a machine no check covers is covered by a recorded known
    outage until that outage ends."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import responsibilities as rs
    from ttp import schedule as sched
    mm.add("box-a", tags="device")
    mm.add("box-b", tags="device")
    me = make(env)
    _resources(me, "Machines: box-a, box-b")
    n = len(rs.inventory(me))
    assert rs.refresh(me, now=1000.0)["line"] == f"health coverage: {n - 2}/{n} (missing: machine box-a, machine box-b)"
    # A report-only command schedule named after the machine, whose command does not name it.
    sched.upsert(me.db, "box-a", "command", "5m", payload={"command": "bin/watch_fleet.sh"})
    rec = rs.refresh(me, now=2000.0)
    n = len(rec["items"])   # the schedule itself is an item now
    assert [i for i in rec["items"] if i["kind"] == "machine" and i["name"] == "box-a"] == [
        {"kind": "machine", "name": "box-a", "covered_by": ["box-a"], "report_only": True}]
    assert rec["line"] == f"health coverage: {n - 1}/{n} (missing: machine box-b; report-only: machine box-a)"
    # A heal check naming it as well: no longer report-only.
    sched.upsert(me.db, "box-a-up", "command", "5m", payload={"heal": {"check": "true", "resource": "box-a"}})
    assert "report_only" not in rs.inventory(me, now=2000.0)[0]
    # A known outage covers the other machine until it ends.
    now = time.time()
    ml.set_outage("box-b", "hardware repair", "user", "2h", now=now)
    rec = rs.refresh(me, now=now)
    assert [i for i in rec["items"] if i["kind"] == "machine" and i["name"] == "box-b"] == [
        {"kind": "machine", "name": "box-b", "covered_by": ["known outage"], "via": rs.OUTAGE_VIA}]
    assert rec["line"] == f"health coverage: {n}/{n} (machine box-b covered by a known outage)"
    advice = rs.review_advice(me.db)
    assert "before that outage ends" in advice and "another project's heal check" not in advice
    assert rs.refresh(me, now=now + 3 * 3600)["line"] == f"health coverage: {n - 1}/{n} (missing: machine box-b)"


def test_health_coverage_trusts_a_recovery_owner_on_another_host_and_names_exclusions(env):
    """A machine's recovery owner whose database is readable here covers it only with a heal check naming
    it; an owner registered on another host (or not on this one) is trusted as declared; config
    health.exclude_machines opts a machine out with its reason; no owner set stays a gap."""
    from ttp import machines as mm
    from ttp import project as pj
    from ttp import responsibilities as rs
    from ttp import schedule as sched
    for a in ("box-a", "box-b", "box-c", "box-d"):
        mm.add(a, tags="device")
    me = make(env)
    _resources(me, "Machines: box-a, box-b, box-c, box-d")
    cfg = me.config()

    def machine(name, c=cfg):
        return next(i for i in rs.inventory(me, c) if i["kind"] == "machine" and i["name"] == name)

    # Same-host owner: its heal check covers the machine; with no check it stays missing (no declared trust).
    mm.set_recovery("box-a", owner="alpha")
    alpha = _second_project(env, "alpha")
    assert machine("box-a")["covered_by"] == []
    sched.upsert(alpha.db, "box-a-up", "command", "5m", payload={"heal": {"check": "ssh box-a true"}})
    assert machine("box-a") == {"kind": "machine", "name": "box-a", "covered_by": ["alpha/box-a-up"],
                                "via": "owner alpha"}
    # Other-host owner (registered elsewhere) and an owner not registered here: covered as declared.
    mm.set_recovery("box-b", owner="beta")
    pj.register("beta", {"host": "other-host", "dir": str(env["tmp"] / "nowhere")})
    assert machine("box-b") == {"kind": "machine", "name": "box-b", "covered_by": ["declared: recovery owner beta"],
                                "via": "owner beta (declared)"}
    mm.set_recovery("box-c", owner="gamma")
    assert machine("box-c")["via"] == "owner gamma (declared)"
    # This project as its own owner is no declared cover; no owner set is a real gap.
    mm.set_recovery("box-c", owner=me.name)
    assert machine("box-c")["covered_by"] == [] and machine("box-d")["covered_by"] == []
    items = rs.inventory(me, cfg)
    n = len(items)
    assert rs.line_of(items) == (f"health coverage: {n - 2}/{n} (missing: machine box-c, machine box-d; "
                                 f"machine box-a covered by owner alpha; machine box-b covered by owner beta (declared))")
    assert "box-b" not in str(rs.check_cover(items))
    # An explicit exclusion covers a machine with its reason; one with no alias is ignored.
    ex = {**cfg, "health": {"exclude_machines": [{"alias": "box-d", "reason": "bench box, checked by hand"},
                                                 {"reason": "no alias"}]}}
    assert rs.excluded_machines(ex) == {"box-d": "bench box, checked by hand"}
    assert machine("box-d", ex) == {"kind": "machine", "name": "box-d",
                                    "covered_by": ["excluded: bench box, checked by hand"],
                                    "excluded": "bench box, checked by hand"}
    items = rs.inventory(me, ex)
    line = rs.line_of(items)
    assert line.startswith(f"health coverage: {n - 1}/{n} (missing: machine box-c;")
    assert line.endswith("; machine box-d excluded: bench box, checked by hand)")
    assert "box-d" not in str(rs.check_cover(items))
    assert pj.unknown_key_hint("health.exclude_machines") is None


@pytest.mark.parametrize("ending", ["timeout", "raises", "watcher"])
def test_heal_schedule_level_error_drops_back_to_every(env, monkeypatch, ending):
    from ttp import heal
    p, d, up = _heal_setup(env, healthy_every="1h")
    up.touch()
    for _ in range(heal.HEALTHY_RUNS):
        p.db.x("UPDATE schedules SET next_run=0 WHERE name='svc'")
        d.run_schedules()
    row = p.db.one("SELECT * FROM schedules WHERE name='svc'")
    assert row["next_run"] - row["last_run"] == pytest.approx(3600)
    if ending == "timeout":   # the run_schedules timeout path records this; heal.run never returned
        d._schedule_ran(dict(row), "error: timed out after 900 s", None)
    elif ending == "watcher":   # healthy, but its own command failed
        d._schedule_ran(dict(row), "ok (healthy); error: watcher exited 2", None)
    else:
        def boom(*a, **k):
            raise RuntimeError("check crashed")
        monkeypatch.setattr(heal, "_run", boom)
        p.db.x("UPDATE schedules SET next_run=0 WHERE name='svc'")
        d.run_schedules()
    row = p.db.one("SELECT * FROM schedules WHERE name='svc'")
    assert row["last_status"].startswith("error" if ending != "watcher" else "ok (healthy); error"), row["last_status"]
    assert row["next_run"] - row["last_run"] <= 300, row["last_status"]
    assert heal.STREAK_KEY not in heal.state(p.db, "svc")
    assert heal.interval(p.db, row, _heal_spec(p)) == 300


@pytest.mark.parametrize("hook", ["_run", "_save"])
def test_heal_timed_out_run_that_ends_healthy_late_does_not_extend_the_streak(env, monkeypatch, hook):
    # The daemon's timeout records the run failed (end_streak) while heal.run is still in its check
    # (_run) or saving the streak (_save); the healthy result it returns later must not count.
    from ttp import heal, schedrun
    p, d, up = _heal_setup(env, healthy_every="1h")
    up.touch()
    for _ in range(heal.HEALTHY_RUNS - 1):
        p.db.x("UPDATE schedules SET next_run=0 WHERE name='svc'")
        d.run_schedules()
    assert heal.state(p.db, "svc")[heal.STREAK_KEY] == heal.HEALTHY_RUNS - 1
    inside, go, real = threading.Event(), threading.Event(), getattr(heal, hook)

    def held(*a, **k):
        if hook == "_save" and heal.STREAK_KEY not in a[2]:
            return real(*a, **k)
        out = real(*a, **k) if hook == "_run" else None
        inside.set()
        assert go.wait(10)
        return out if hook == "_run" else real(*a, **k)
    monkeypatch.setattr(heal, hook, held)
    row = dict(p.db.one("SELECT * FROM schedules WHERE name='svc'"))
    run = d._start_schedule(row, json.loads(row["payload"]))
    assert inside.wait(10)
    run.timed_out = True   # what Daemon.run_schedules does at the run's deadline
    schedrun.kill(p.db, run)
    d._schedule_ran(run.s, "error: timed out after 1 s", None)
    go.set()
    assert run.done.wait(10) and run.result[0] == "ok (healthy)"   # its late result, which the daemon drops
    assert heal.STREAK_KEY not in heal.state(p.db, "svc")
    monkeypatch.setattr(heal, hook, real)
    p.db.x("UPDATE schedules SET next_run=0 WHERE name='svc'")
    d.run_schedules()   # the next healthy run starts a new streak and stays at `every`
    row = p.db.one("SELECT * FROM schedules WHERE name='svc'")
    assert heal.state(p.db, "svc")[heal.STREAK_KEY] == 1
    assert row["next_run"] - row["last_run"] == pytest.approx(300)


def test_activity_since_reads_a_zone_less_time_in_the_home_zone(env, utc_box):
    """`machines activity --since 2027-01-14 22:00` on a UTC box is 22:00 in the project's home zone (06:00 UTC
    next day); a time with an offset or a delay is unchanged."""
    from ttp import activity
    now = 1_800_000_000.0
    assert activity.parse_since("2027-01-14 22:00", now, where="America/Los_Angeles") == now - 7200
    assert activity.parse_since("2027-01-15T06:00Z", now, where="America/Los_Angeles") == now - 7200
    assert activity.parse_since("2h", now, where="America/Los_Angeles") == now - 7200
    p = make(env)
    p.set_config("home_timezone", "America/Los_Angeles")
    assert activity.parse_since("2027-01-14 22:00", now, where=p) == now - 7200


TRIVIAL_PROBES = ["true", ":", "exit 0", "test -e x || true", "test -e x || :", "test -e x || exit 0",
                  "test -e x; true", "test -e x; :", "test -e x; exit 0", " test -e x  ||   true ;\n",
                  "test -e x||true;", "test -e x\ntrue\n", "a && b || true", "grep -q a f 2>&1 || true",
                  "a | true", "(test -e x || true)", "{ test -e x || true; }", "test -e x & true",
                  "test -e x || true # comment", "true >/dev/null 2>&1", "X=1 true", "a || (exit 0)", "/bin/true"]
REAL_PROBES = ["test -e x || exit 1", "cmd && true", "grep -q a f", "(a || true) && b", "echo 'a || true'",
               "test \"$(a || true)\" = x", "a && b | true", "a || true && false", "test -e x && exit 0; exit 1",
               "exit", "exit 1", "true; false", "test -e x # || true", "if a; then true; fi", "cmd 2>&1",
               "cmd >&2 || exit 1", "sh -c 'exit 0'", "echo $(true)", "! true", "test -e x || { echo no; exit 1; }",
               "case a in a) true;; esac", "a || true | false", "test -e x | grep -q y", ""]


@pytest.mark.parametrize("probe", TRIVIAL_PROBES)
def test_always_succeeds_flags_a_probe_that_cannot_fail(probe):
    from ttp import ends
    assert ends.always_succeeds(probe), probe
    with pytest.raises(ValueError, match=r"always exits 0.*\|\| exit 1"):
        ends.check_not_trivial(probe, "--until-probe")


@pytest.mark.parametrize("probe", REAL_PROBES)
def test_always_succeeds_leaves_a_real_test_alone(probe):
    from ttp import ends
    assert not ends.always_succeeds(probe), probe
    ends.check_not_trivial(probe, "--until-probe")


def test_machine_change_add_and_edit_refuse_an_until_probe_that_always_exits_0(env, capsys):
    """An until-probe ending in `|| true` (and the like) would make the change look over at once, so its
    undo is due while the cause is still there: add and edit refuse it on one line and write nothing."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp.cli import main
    p = make(env)
    mm.add("box-a", tags="device")
    for probe in ("test -e /tmp/held || true", "check-hold; exit 0", "true"):
        with pytest.raises(SystemExit):
            main(["machines", "change", "add", "box-a", "held", "--undo", "release", "--until-probe", probe,
                  "--by", p.name])
        err = capsys.readouterr().err
        assert "always exits 0" in err and "|| exit 1" in err and len(err.strip().splitlines()) == 1, err
        assert ml.changes() == [], "nothing written"
    with pytest.raises(ValueError, match="always exits 0"):
        ml.add_change("box-a", "held", "release", p.name, until_probe="test -e x || :")
    main(["machines", "change", "add", "box-a", "held", "--undo", "release", "--until-probe",
          "test -e /tmp/held || exit 1", "--by", p.name])
    before = ml.changes()
    for probe in ("test -e /tmp/held || exit 0", ":"):
        with pytest.raises(SystemExit):
            main(["machines", "change", "edit", "1", "--until-probe", probe, "--by", p.name])
        err = capsys.readouterr().err
        assert "always exits 0" in err and "|| exit 1" in err and len(err.strip().splitlines()) == 1, err
        assert ml.changes() == before, "the entry is unchanged"
    with pytest.raises(ValueError, match="always exits 0"):
        ml.edit_change(1, p.name, until_probe="x; true")


def test_machine_change_add_refuses_an_until_probe_that_passes_already_unless_allowed(env, capsys):
    """An until-probe that exits 0 when the change is recorded makes the entry overdue at once: add runs it
    once, from the project root as the daemon does, and refuses it on one line, writing nothing;
    --allow-met-probe records it with a warning, and a probe that says "not yet" records with none."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp.cli import main
    p = make(env)
    mm.add("box-a", tags="device")
    (p.root / "staged").write_text("x")   # relative: the probe runs in the project root
    add = ["machines", "change", "add", "box-a", "staged a file", "--undo", "rm staged", "--by", p.name]
    with pytest.raises(SystemExit) as e:
        main(add + ["--until-probe", "test -e staged"])
    assert e.value.code != 0
    err = capsys.readouterr().err
    assert "already passes" in err and "overdue at once" in err and "--expires" in err, err
    assert "--allow-met-probe" in err and len(err.strip().splitlines()) == 1, err
    assert ml.changes() == [], "nothing written"
    main(add + ["--until-probe", "test -e staged", "--allow-met-probe"])
    out, err = capsys.readouterr()
    assert out.startswith("recorded ") and "warning" in err and "already passes" in err, (out, err)
    assert [c["probe"] for c in ml.changes()] == ["test -e staged"]
    main(add + ["--until-probe", "test ! -e staged"])
    out, err = capsys.readouterr()
    assert out.startswith("recorded ") and not err, (out, err)
    with pytest.raises(SystemExit):
        main(add + ["--expires", "2h", "--allow-met-probe"])
    assert "goes with --until-probe" in capsys.readouterr().err and len(ml.changes()) == 2


@pytest.mark.parametrize("probe,says", [("sleep 30", "did not finish"), ("exit 3", "exits 3"),
                                        ("exit 255", "exits 255")])
def test_machine_change_add_records_a_timed_out_or_broken_until_probe_with_a_warning(env, monkeypatch, capsys,
                                                                                    probe, says):
    """Only exit 0 refuses: a probe that times out (killed within add's short timeout), or exits with a code
    the daemon reads as broken, or 255 (unreachable or broken), is recorded with a warning naming it."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp.cli import main
    p = make(env)
    mm.add("box-a", tags="device")
    monkeypatch.setattr(ml, "ADD_PROBE_TIMEOUT_S", 0.5)
    t0 = time.monotonic()
    main(["machines", "change", "add", "box-a", "paused", "--undo", "resume", "--until-probe", probe,
          "--by", p.name])
    assert time.monotonic() - t0 < 10
    out, err = capsys.readouterr()
    assert out.startswith("recorded ") and "warning" in err and says in err, (out, err)
    assert [c["probe"] for c in ml.changes()] == [probe]


def test_machine_change_add_with_expires_only_runs_no_probe(env, monkeypatch, capsys):
    from ttp import ends
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp.cli import main
    p = make(env)
    mm.add("box-a", tags="device")
    monkeypatch.setattr(ends, "probe_once", lambda *a, **k: pytest.fail("no probe to run"))
    main(["machines", "change", "add", "box-a", "paused", "--undo", "resume", "--expires", "2h", "--by", p.name])
    out, err = capsys.readouterr()
    assert out.startswith("recorded ") and not err and ml.changes()[0]["probe"] is None


def test_machine_change_edit_refuses_a_new_until_probe_that_passes_already_unless_allowed(env, capsys):
    """edit runs a new until-probe once, from the project root, as add does: exit 0 is refused on one line
    and the entry is left unchanged; --allow-met-probe stores it with a warning; "not yet" stores it quietly."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp.cli import main
    p = make(env)
    mm.add("box-a", tags="device")
    (p.root / "staged").write_text("x")   # relative: the probe runs in the project root
    main(["machines", "change", "add", "box-a", "staged a file", "--undo", "rm staged", "--expires", "2h",
          "--by", p.name])
    capsys.readouterr()
    before = ml.changes()
    edit = ["machines", "change", "edit", "1", "--by", p.name]
    for extra in ([], ["--expires", "3h"]):
        with pytest.raises(SystemExit) as e:
            main(edit + ["--until-probe", "test -e staged", *extra])
        assert e.value.code != 0
        err = capsys.readouterr().err
        assert "already passes" in err and "overdue at once" in err and "--allow-met-probe" in err, err
        assert len(err.strip().splitlines()) == 1, err
        assert ml.changes() == before, "the entry is unchanged"
    main(edit + ["--until-probe", "test -e staged", "--allow-met-probe"])
    out, err = capsys.readouterr()
    assert out.startswith("edited ") and "warning" in err and "already passes" in err, (out, err)
    assert ml.changes()[0]["probe"] == "test -e staged"
    # The same probe again is judged afresh, so it is checked again too.
    with pytest.raises(SystemExit):
        main(edit + ["--until-probe", "test -e staged"])
    assert "already passes" in capsys.readouterr().err
    main(edit + ["--until-probe", "test ! -e staged"])
    out, err = capsys.readouterr()
    assert out.startswith("edited ") and not err and ml.changes()[0]["probe"] == "test ! -e staged", (out, err)
    with pytest.raises(SystemExit):
        main(edit + ["--what", "x", "--allow-met-probe"])
    assert "goes with --until-probe" in capsys.readouterr().err and ml.changes()[0]["what"] == "staged a file"


@pytest.mark.parametrize("probe,says", [("sleep 30", "did not finish"), ("exit 3", "exits 3"),
                                        ("exit 255", "exits 255")])
def test_machine_change_edit_stores_a_timed_out_or_broken_until_probe_with_a_warning(env, monkeypatch, capsys,
                                                                                    probe, says):
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp.cli import main
    p = make(env)
    mm.add("box-a", tags="device")
    main(["machines", "change", "add", "box-a", "paused", "--undo", "resume", "--expires", "2h", "--by", p.name])
    capsys.readouterr()
    monkeypatch.setattr(ml, "ADD_PROBE_TIMEOUT_S", 0.5)
    t0 = time.monotonic()
    main(["machines", "change", "edit", "1", "--until-probe", probe, "--by", p.name])
    assert time.monotonic() - t0 < 10
    out, err = capsys.readouterr()
    assert out.startswith("edited ") and "warning" in err and says in err, (out, err)
    assert ml.changes()[0]["probe"] == probe


def test_machine_change_edit_stores_an_until_probe_that_cannot_run_with_a_warning(env, monkeypatch, capsys):
    from ttp import ends
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp.cli import main
    p = make(env)
    mm.add("box-a", tags="device")
    main(["machines", "change", "add", "box-a", "paused", "--undo", "resume", "--expires", "2h", "--by", p.name])
    capsys.readouterr()
    monkeypatch.setattr(ends, "probe_once", lambda *a, **k: ("cannot run: no shell", ""))
    main(["machines", "change", "edit", "1", "--until-probe", "check-it", "--by", p.name])
    out, err = capsys.readouterr()
    assert out.startswith("edited ") and "warning" in err and "cannot run" in err, (out, err)
    assert ml.changes()[0]["probe"] == "check-it"


def test_machine_change_edit_without_an_until_probe_runs_no_probe(env, monkeypatch, capsys):
    """Edits of the text, undo, --expires or --until-task run no probe, nor do edits of a closed or unknown id."""
    from ttp import ends
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp.cli import main
    p = make(env)
    mm.add("box-a", tags="device")
    main(["machines", "change", "add", "box-a", "paused", "--undo", "resume", "--until-probe", "test -e /no/such",
          "--by", p.name])
    capsys.readouterr()
    monkeypatch.setattr(ends, "probe_once", lambda *a, **k: pytest.fail("no probe to run"))
    for extra in (["--what", "paused the queue"], ["--undo", "resume-queue"], ["--expires", "3h"]):
        main(["machines", "change", "edit", "1", *extra, "--by", p.name])
        out, err = capsys.readouterr()
        assert out.startswith("edited ") and not err, (out, err)
    c = ml.changes()[0]
    assert (c["what"], c["undo"], c["probe"]) == ("paused the queue", "resume-queue", None)
    ml.close_change(1, "done", p.name)
    for cid, says in (("1", "is closed"), ("9", "no machine change #9")):
        with pytest.raises(SystemExit):
            main(["machines", "change", "edit", cid, "--until-probe", "test -e /x", "--by", p.name])
        assert says in capsys.readouterr().err


def test_machine_change_with_a_stored_trivial_until_probe_is_never_ended_by_it_and_is_reported(env):
    """An entry stored before add refused such probes (or written by an older version) never turns
    overdue because its probe passed, even with a pass recorded: Probes does not run it and queues one
    broken-probe event, its listing line warns, and the progress watcher raises no due undo. Its time
    still ends it."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import progress
    p = make(env)
    mm.add("box-a", tags="device")
    now = time.time()
    c = ml.add_change("box-a", "held a kit", "rm -r kit", p.name, until_probe="test -e /tmp/kit || exit 1",
                      expires="2d", now=now)
    with ml._locked():
        doc = ml._read(ml.LEDGER, {"next": 1, "changes": []})
        doc["changes"][0].update(probe="test -e /tmp/kit || true", probe_passed=now, probe_out="")
        ml._write(ml.LEDGER, doc)
    [stored] = ml.changes()
    later = now + ml.PROBE_GRACE_S + 1
    assert ml.trivial_probe(stored) and ml.overdue(stored, later) == ""
    assert not [x for x in ml.route(later, able=lambda n: (True, "")) if x["kind"] == "machine_change_overdue"]
    logs = []
    probes = ml.Probes(p, log=logs.append)
    for at in (later, later + 1000):
        probes._probed.clear()
        probes.tick(at)
        assert c["id"] not in probes._procs, "never run"
    evs = p.db.q("SELECT text FROM events WHERE kind=?", (ml.PROBE_BROKEN_KIND,))
    assert len(evs) == 1 and "always exits 0" in evs[0]["text"] and "|| exit 1" in evs[0]["text"], evs
    assert not ml.changes()[0].get("closed")
    assert "always exits 0" in ml.change_line(ml.changes()[0], later)
    t = p.db.add_task("holds the kit", "x")
    p.db.update_task(t, status="waiting")
    got, _ = progress.change_probes(p.db, {"change_probe_m": 15}, {"raised": {}},
                                    [dict(ml.changes()[0], task=t)], later + 3600)
    assert got == [], "no due undo from its pass"
    assert ml.overdue(ml.changes()[0], now + 2 * 86400 + 1).startswith("expired"), "its time still ends it"


NOOP_UNDOS = ("true", ":", "none", "NONE: nothing left to undo", "None - gone with the reboot", "none;",
              "  none\tnothing  ", "")
NOT_NOOP_UNDOS = ("true && rm x", "none_cleanup.sh", "nonexistent", "true;", ": ; rm x", "falsetrue",
                  "systemctl start svc", "none.sh", "rm -f drop-in; true")


def _set_undo(ml, cid, undo):
    """Store `undo` as is (add refuses an empty one; an older entry may hold it)."""
    with ml._locked():
        doc = ml._read(ml.LEDGER, {"next": 1, "changes": []})
        next(c for c in doc["changes"] if c["id"] == cid)["undo"] = undo
        ml._write(ml.LEDGER, doc)


def _probe_round(probes, at):
    """One Probes tick that reaps the last run and starts the next, then waits for it to end."""
    probes._probed.clear()
    probes.tick(at)
    for proc, _, _ in list(probes._procs.values()):
        proc.wait(30)


def test_noop_undo_grammar_is_strict():
    from ttp import machine_ledger as ml
    assert all(ml.noop_undo(u) for u in NOOP_UNDOS)
    assert not any(ml.noop_undo(u) for u in NOT_NOOP_UNDOS) and not ml.noop_undo(None)


def test_an_expired_change_with_a_noop_undo_closes_itself_once_and_a_real_undo_still_pages(env, capsys):
    """Each no-op undo form closes at the first route past its expiry, model-free and with no page; a
    near-miss or real undo pages as before. A second route (a restarted daemon) closes nothing again."""
    from ttp import cli
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    make(env)
    mm.add("box-a", tags="device")
    t0 = time.time()
    noop = {ml.add_change("box-a", f"noop {i}", "x", "alpha", expires="1h", now=t0)["id"]: u
            for i, u in enumerate(NOOP_UNDOS)}
    real = {ml.add_change("box-a", f"real {i}", u, "alpha", expires="1h", now=t0)["id"]: u
            for i, u in enumerate(NOT_NOOP_UNDOS)}
    for cid, u in noop.items():
        _set_undo(ml, cid, u)
    logs = []
    assert ml.route(t0 + 60, able=lambda n: (True, ""), log=logs.append) == [] and not logs, "not yet expired"
    assert len(ml.changes()) == len(noop) + len(real)
    later = t0 + 3600 + 1
    pages = ml.route(later, able=lambda n: (True, ""), claimer="alpha", log=logs.append)
    paged = {int(x["key"].split(":")[1]) for x in pages if x["kind"] == "machine_change_overdue"}
    assert paged == set(real), "a real or near-miss undo pages as before"
    assert {c["id"] for c in ml.changes()} == set(real)
    closed = {c["id"]: c for c in ml.changes(open_only=False) if c.get("closed")}
    assert set(closed) == set(noop)
    for cid, c in closed.items():
        assert c["closed"] == later and c["closed_why"] == "auto-closed: no-op undo, expired"
        assert c["closed_by"] == "auto-close: no-op undo, expired" and "page" not in c
        assert c["auto_closed"] == {"at": later, "rule": "no-op undo, expired", "sweeper": "alpha"}
    assert len(logs) == len(noop) and all("auto-closed (no-op undo, expired)" in m and "box-a" in m for m in logs)
    assert any(f"#{min(noop)} on box-a auto-closed" in m and "noop 0" in m for m in logs)
    # A restarted daemon (or a co-tenant's) sweeping again: no second close, no second record.
    before = ml.changes(open_only=False)
    ml.route(later + 600, able=lambda n: (True, ""), claimer="beta", log=logs.append)
    assert len(logs) == len(noop)
    assert [c for c in ml.changes(open_only=False) if c["id"] in noop] == [c for c in before if c["id"] in noop]
    # The listing's history and --json show it and the rule that closed it.
    cli.main(["machines", "change", "list", "--all"])
    out = capsys.readouterr().out
    assert out.count(": auto-closed: no-op undo, expired)") == len(noop), out
    cli.main(["machines", "change", "list", "--all", "--json"])
    rows = {c["id"]: c for c in json.loads(capsys.readouterr().out)}
    assert all(rows[cid]["closed_by"] == "auto-close: no-op undo, expired" for cid in noop)


def test_a_noop_undo_change_closes_only_after_its_until_probe_passes_twice_in_a_row(env, tmp_path):
    """An until-probe entry with a no-op undo closes once its probe passed PROBE_PASSES times in a row,
    never after one pass, while it still fails (even past its --expires), nor when its probe always exits 0."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env)
    mm.add("box-a", tags="device")
    t0 = time.time()
    flag = tmp_path / "over"
    probe = f"test -e {flag} || exit 1"
    ids = [ml.add_change("box-a", f"held {i}", u, p.name, until_probe=probe, expires="1h", now=t0)["id"]
           for i, u in enumerate(NOOP_UNDOS[:4])]
    real = ml.add_change("box-a", "held for real", "release it", p.name, until_probe=probe, now=t0)["id"]
    probes, able = ml.Probes(p, log=lambda m: None), (lambda n: (True, ""))
    at = t0 + ml.PROBE_GRACE_S + 1
    for i in range(3):   # still failing: open (past its --expires: the next test)
        _probe_round(probes, at + i)
        assert ml.route(at + i, able=able) == []
    assert {c["id"] for c in ml.changes()} >= set(ids), "a failing probe never closes it"
    flag.touch()
    _probe_round(probes, at + 10)   # reaps the last failing run, starts a passing one
    _probe_round(probes, at + 11)   # pass 1 of 2
    ml.route(at + 11, able=able)
    assert {c["id"] for c in ml.changes()} >= set(ids), "one pass is not enough"
    _probe_round(probes, at + 12)   # pass 2 of 2: marked passed
    pages = ml.route(at + 12, able=able, claimer=p.name)
    left = {c["id"] for c in ml.changes()}
    assert not left & set(ids) and real in left
    assert [x["key"] for x in pages if x["kind"] == "machine_change_overdue"] == [f"machine-change:{real}"]
    for c in ml.changes(open_only=False):
        if c["id"] in ids:
            rule = f"no-op undo, until-probe passed {ml.PROBE_PASSES} times"
            assert c["closed_why"] == f"auto-closed: {rule}" and c["closed_by"] == f"auto-close: {rule}"


def test_a_noop_undo_change_closes_itself_once_its_until_task_is_done_failed_or_cancelled(env):
    """An --until-task entry with a no-op undo closes itself (no page) once its project's daemon saw the task
    done, failed or cancelled, the rule naming the task. A real undo still pages; an open task keeps it open;
    an until-task that cannot be resolved (deleted, unknown id, or a project whose daemon never marks it)
    pages as before."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env)
    mm.add("box-a", tags="device")
    t0 = time.time()
    task = {n: p.db.add_task(n, "s", kind="work", tier="light", origin="user")
            for n in ("done", "failed", "cancelled", "real", "open", "deleted", "elsewhere")}
    noop = "none: the jobs end themselves"
    ids = {n: ml.add_change("box-a", n, noop, p.name, until_task=task[n], now=t0)["id"]
           for n in ("done", "failed", "cancelled", "open", "deleted")}
    _set_undo(ml, ids["failed"], "true")
    ids["real"] = ml.add_change("box-a", "real", "release it", p.name, until_task=task["real"], now=t0)["id"]
    ids["unknown"] = ml.add_change("box-a", "unknown", noop, p.name, until_task=99999, now=t0)["id"]
    ids["elsewhere"] = ml.add_change("box-a", "elsewhere", noop, "gone-project", until_task=task["elsewhere"],
                                     now=t0)["id"]
    for n in ("done", "failed", "cancelled", "real"):
        p.db.x("UPDATE tasks SET status=? WHERE id=?", ("done" if n == "real" else n, task[n]))
    p.db.x("DELETE FROM tasks WHERE id=?", (task["deleted"],))
    able, logs = (lambda n: (True, "")), []
    ml.Probes(p, log=lambda m: None).tick(t0 + 1)
    pages = ml.route(t0 + 2, able=able, claimer=p.name, log=logs.append)
    paged = {int(x["key"].split(":")[1]) for x in pages if x["kind"] == "machine_change_overdue"}
    assert paged == {ids["real"], ids["deleted"], ids["unknown"]}, pages
    left = {c["id"] for c in ml.changes()}
    assert left == {ids[n] for n in ("real", "open", "deleted", "unknown", "elsewhere")}
    closed = {c["id"]: c for c in ml.changes(open_only=False) if c.get("closed")}
    for n in ("done", "failed", "cancelled"):
        rule = f"no-op undo, its until-task #{task[n]} is {n}"
        c = closed[ids[n]]
        assert c["closed_why"] == f"auto-closed: {rule}" and c["closed_by"] == f"auto-close: {rule}"
        assert c["auto_closed"]["rule"] == rule and "page" not in c
    assert len(logs) == 3 and all("auto-closed (no-op undo, its until-task #" in m for m in logs)
    [still] = [c for c in ml.changes() if c["id"] == ids["open"]]
    assert not ml.overdue(still, t0 + 2) and not ml.auto_close_rule(still, t0 + 2)
    # The open task ends later: the next pass closes it too.
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (task["open"],))
    ml.Probes(p, log=lambda m: None).tick(t0 + 3)
    pages = ml.route(t0 + 4, able=able, claimer=p.name)
    assert ids["open"] not in {int(x["key"].split(":")[1]) for x in pages}
    assert ids["open"] not in {c["id"] for c in ml.changes()}
    # One whose project never marks its task ends at its latest end and pages, as before.
    late = t0 + ml.PROBE_END_S + 1
    pages = ml.route(late, able=able, claimer=p.name)
    assert f"machine-change:{ids['elsewhere']}" in {x["key"] for x in pages}
    assert ids["elsewhere"] in {c["id"] for c in ml.changes()}
    # An edit to a new until-task forgets the old task's end.
    c = ml.edit_change(ids["real"], p.name, undo=noop, until_task=task["open"], now=t0 + 5)
    assert "task_ended" not in c and "task_status" not in c


def test_a_noop_undo_change_with_a_failing_broken_or_trivial_probe_is_never_auto_closed(env):
    """Past its --expires, an entry whose probe still fails, broke, or always exits 0 (ends.always_succeeds)
    keeps paging, as does a pass whose job failed."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env)
    mm.add("box-a", tags="device")
    t0 = time.time()
    cases = {"failing": {}, "trivial": {"probe": "test -e /tmp/kit || true", "probe_passed": t0},
             "job failed": {"probe_passed": t0, "probe_job_failed": "exit 3"}}
    ids = {}
    for name, fields in cases.items():
        ids[name] = ml.add_change("box-a", name, "none: nothing to undo", p.name,
                                  until_probe="test -e /tmp/kit || exit 1", expires="1h", now=t0)["id"]
        with ml._locked():
            doc = ml._read(ml.LEDGER, {"next": 1, "changes": []})
            next(c for c in doc["changes"] if c["id"] == ids[name]).update(fields)
            ml._write(ml.LEDGER, doc)
    pages = ml.route(t0 + 7200, able=lambda n: (True, ""))
    assert {x["key"] for x in pages if x["kind"] == "machine_change_overdue"} == \
        {f"machine-change:{i}" for i in ids.values()}
    assert len(ml.changes()) == 3 and all(not ml.auto_close_rule(c, t0 + 7200) for c in ml.changes())


def test_two_concurrent_sweepers_close_a_noop_undo_change_once(env):
    """Co-tenant daemons route at the same moment: the ledger lock lets one close it; the other sees it
    closed and records nothing."""
    import threading
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    make(env)
    mm.add("box-a", tags="device")
    t0 = time.time()
    cid = ml.add_change("box-a", "a repro driver", "none", "alpha", expires="1h", now=t0)["id"]
    logs, start = [], threading.Barrier(4)

    def sweep(name):
        start.wait()
        ml.route(t0 + 3601, able=lambda n: (True, ""), claimer=name, log=lambda m: logs.append((name, m)))

    threads = [threading.Thread(target=sweep, args=(f"d{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(logs) == 1, logs
    [c] = [c for c in ml.changes(open_only=False) if c["id"] == cid]
    assert c["closed"] and c["auto_closed"]["sweeper"] == logs[0][0]


def _auto_change(ml, p, undo, now, **kw):
    """An --auto-undo entry on box-a by project `p`, due an hour after `now` (its --expires)."""
    kw.setdefault("expires", "1h")
    return ml.add_change("box-a", "a scratch dir for a repro", undo, p.name, now=now, auto_undo=True, **kw)["id"]


def _undo_round(probes, at):
    """One Probes tick (it reaps the last auto-undo run and may start one), then waits for what it started."""
    probes.tick(at)
    for proc, _, _, _ in list(probes._undos.values()):
        proc.wait(30)


def _change_entry(ml, cid):
    return next(c for c in ml.changes(open_only=False) if c["id"] == cid)


def _overdue_pages(ml, at, claimer="alpha"):
    return [x for x in ml.route(at, able=lambda n: (True, ""), claimer=claimer) if x["kind"] == "machine_change_overdue"]


def _undo_gone(pid):
    """Whether process `pid` ended (gone, or a zombie nobody reaped yet)."""
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return False


def test_an_auto_undo_change_runs_its_undo_once_due_in_its_own_project_and_exit_0_closes_it(env, tmp_path, capsys):
    """Not before it is due, only in the recording project's daemon, once: the claim is recorded before the run,
    exit 0 closes it as 'auto-undo ok', nobody is paged, and list and --json show the run."""
    from ttp import cli
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env)
    mm.add("box-a", tags="device")
    t0 = time.time()
    ran = tmp_path / "ran"
    cid = _auto_change(ml, p, f"echo ran >> {ran}; echo removed the scratch dir", t0)
    other = ml.add_change("box-a", "someone else's", f"echo other >> {ran}", "alpha", expires="1h", now=t0,
                          auto_undo=True)["id"]
    probes = ml.Probes(p, log=lambda m: None)
    _undo_round(probes, t0 + 60)
    assert not ran.exists() and not probes._undos, "not due yet"
    due = t0 + 3601
    assert f"machine-change:{cid}" not in {x["key"] for x in _overdue_pages(ml, due)}, "left to its auto-undo"
    probes.tick(due)
    claim = _change_entry(ml, cid)["auto_undo_claim"]
    assert claim["try"] == 1 and claim["by"] == p.name and claim["at"] == due, "claimed before it ran"
    assert set(probes._undos) == {cid}, "only its own project's entries run"
    for proc, _, _, _ in probes._undos.values():
        proc.wait(30)
    _undo_round(probes, due + 1)
    c = _change_entry(ml, cid)
    assert c["closed"] == due + 1 and c["closed_why"] == "auto-undo ok" and c["closed_by"] == "auto-undo"
    assert "auto_undo_claim" not in c and "page" not in c
    [run] = c["auto_undo_runs"]
    assert run["rc"] == 0 and run["outcome"] == "ok" and "removed the scratch dir" in run["tail"]
    for at in (due + 2, due + 400, due + 4000):
        _undo_round(probes, at)
    assert ran.read_text() == "ran\n", "once"
    assert f"machine-change:{cid}" not in {x["key"] for x in _overdue_pages(ml, due + 4000)}
    cli.main(["machines", "change", "list", "--all"])
    out = capsys.readouterr().out
    assert ": auto-undo ok)" in out and "auto-undo try 1 at" in out and "removed the scratch dir" in out, out
    cli.main(["machines", "change", "list", "--all", "--json"])
    rows = {c["id"]: c for c in json.loads(capsys.readouterr().out)}
    assert rows[cid]["auto_undo_runs"][0]["outcome"] == "ok" and rows[cid]["auto_undo"] is True


@pytest.mark.parametrize("rc", [1, 75, 255])
def test_an_auto_undo_that_exits_not_yet_retries_after_the_grace_then_pages_with_its_tail(env, tmp_path, rc):
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env)
    mm.add("box-a", tags="device")
    t0 = time.time()
    ran = tmp_path / "ran"
    cid = _auto_change(ml, p, f"echo try >> {ran}; echo still in use; exit {rc}", t0)
    probes = ml.Probes(p, log=lambda m: None)
    at = t0 + 3601
    for n in range(1, ml.AUTO_UNDO_TRIES + 1):
        _undo_round(probes, at)
        assert ran.read_text().count("try") == n
        _undo_round(probes, at + 1)   # reaps it
        c = _change_entry(ml, cid)
        assert len(c["auto_undo_runs"]) == n and c["auto_undo_runs"][-1]["rc"] == rc
        if n < ml.AUTO_UNDO_TRIES:
            assert c["auto_undo_next"] == at + 1 + ml.PROBE_GRACE_S and not c.get("auto_undo_failed")
            _undo_round(probes, at + 2)
            assert not probes._undos and ran.read_text().count("try") == n, "not before the grace"
            assert not _overdue_pages(ml, at + 2), "a retry waits: nobody paged"
            at = c["auto_undo_next"]
    c = _change_entry(ml, cid)
    assert not c.get("closed") and c["auto_undo_failed"]["why"] == \
        f"exit {rc} on each of {ml.AUTO_UNDO_TRIES} tries"
    [page] = _overdue_pages(ml, at + 2)
    assert page["target"] == p.name and "auto-undo gave up" in page["text"] and "still in use" in page["text"]
    _undo_round(probes, at + 5000)
    assert ran.read_text().count("try") == ml.AUTO_UNDO_TRIES, "no run after it gave up"


def test_an_auto_undo_with_another_exit_or_a_timeout_pages_at_once_and_kills_only_its_own_group(env, tmp_path,
                                                                                                monkeypatch):
    import os
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env)
    mm.add("box-a", tags="device")
    t0 = time.time()
    grp, kid = tmp_path / "pgid", tmp_path / "kid"
    bad = _auto_change(ml, p, f"ps -o pgid= -p $$ > {grp}; echo $$ >> {grp}; echo boom; exit 2", t0)
    slow = _auto_change(ml, p, f"echo started; sleep 60 & echo $! > {kid}; wait", t0)
    monkeypatch.setattr(ml, "AUTO_UNDO_TIMEOUT_S", 30)
    probes = ml.Probes(p, log=lambda m: None)
    at = t0 + 3601
    probes.tick(at)
    probes._undos[bad][0].wait(30)
    for _ in range(100):
        if kid.exists() and kid.read_text().strip():
            break
        time.sleep(0.05)
    sleeper = int(kid.read_text())
    probes.tick(at + 10)   # reaps the failed one; the slow one still runs
    assert slow in probes._undos and not _undo_gone(sleeper)
    pgid, shell = grp.read_text().split()
    assert pgid == shell and int(pgid) != os.getpgrp(), "its own process group, never the daemon's"
    c = _change_entry(ml, bad)
    assert c["auto_undo_failed"]["why"] == "exit 2" and len(c["auto_undo_runs"]) == 1
    probes.tick(at + 31)   # past its timeout: its whole group is killed
    assert slow not in probes._undos
    for _ in range(100):
        if _undo_gone(sleeper):
            break
        time.sleep(0.05)
    assert _undo_gone(sleeper), "the timeout kills what the undo started too"
    s = _change_entry(ml, slow)
    assert s["auto_undo_runs"][-1]["rc"] == "timeout" and "timed out after 30s" in s["auto_undo_failed"]["why"]
    assert "started" in s["auto_undo_failed"]["tail"]
    pages = {x["key"]: x["text"] for x in _overdue_pages(ml, at + 32)}
    assert "boom" in pages[f"machine-change:{bad}"] and "exit 2" in pages[f"machine-change:{bad}"]
    assert "timed out" in pages[f"machine-change:{slow}"] and "started" in pages[f"machine-change:{slow}"]


def test_auto_undo_is_refused_for_a_noop_a_pattern_kill_a_trivial_probe_or_an_until_task_at_add_and_edit(env, capsys):
    from ttp import cli
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env)
    mm.add("box-a", tags="device")
    t0 = time.time()
    for undo in ("none: nothing left", "true", ":"):
        with pytest.raises(ValueError, match="no-op undo"):
            _auto_change(ml, p, undo, t0)
    for undo in ("pkill -f repro", "killall repro", "kill $(pgrep repro)", "rm -rf /tmp/x; /usr/bin/pkill x"):
        with pytest.raises(ValueError, match="kills by name or pattern"):
            _auto_change(ml, p, undo, t0)
    with pytest.raises(ValueError, match="always exits 0"):
        _auto_change(ml, p, "rm -rf /tmp/x", t0, until_probe="test -e /tmp/y || true")
    with pytest.raises(ValueError, match="until-task"):
        ml.check_auto_undo("rm -rf /tmp/x", None, 7)
    assert not ml.changes(), "nothing recorded"
    plain = ml.add_change("box-a", "plain", "pkill repro", p.name, expires="1h", now=t0)["id"]
    with pytest.raises(ValueError, match="kills by name or pattern"):
        ml.edit_change(plain, p.name, auto_undo=True, now=t0)
    auto = _auto_change(ml, p, "rm -rf /tmp/x", t0)
    with pytest.raises(ValueError, match="no-op undo"):
        ml.edit_change(auto, p.name, undo="none", now=t0)
    with pytest.raises(ValueError, match="kills by name or pattern"):
        ml.edit_change(auto, p.name, undo="killall x", now=t0)
    old = ml.add_change("box-a", "old probe", "rm -rf /tmp/x", p.name, until_probe="test -e /tmp/y || exit 1",
                        now=t0)["id"]
    with ml._locked():   # stored before add and edit refused trivial probes
        doc = ml._read(ml.LEDGER, {"next": 1, "changes": []})
        next(c for c in doc["changes"] if c["id"] == old)["probe"] = "test -e /tmp/y || true"
        ml._write(ml.LEDGER, doc)
    with pytest.raises(ValueError, match="always exits 0"):
        ml.edit_change(old, p.name, auto_undo=True, now=t0)
    assert not any(c.get("auto_undo") for c in ml.changes() if c["id"] != auto)
    c = ml.edit_change(auto, p.name, auto_undo=False, now=t0)
    assert "auto_undo" not in c and c["edits"][-1]["was"] == {"auto_undo": True}
    with pytest.raises(SystemExit):
        cli.main(["machines", "change", "add", "box-a", "x", "--undo", "pkill -f x", "--expires", "1h",
                  "--auto-undo", "--by", p.name])
    assert "kills by name or pattern" in capsys.readouterr().err
    cli.main(["machines", "change", "edit", str(auto), "--auto-undo", "--by", p.name])
    assert _change_entry(ml, auto)["auto_undo"] is True and "; auto-undo" in capsys.readouterr().out


def test_an_auto_undo_claim_survives_a_restart_and_is_paged_never_run_twice(env, tmp_path):
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env)
    mm.add("box-a", tags="device")
    t0 = time.time()
    ran, gate = tmp_path / "ran", tmp_path / "gate"
    cid = _auto_change(ml, p, f"echo ran >> {ran}; while ! test -e {gate}; do {_nap()}; done", t0)
    crashed = _auto_change(ml, p, f"echo crashed >> {ran}", t0)
    at = t0 + 3601
    first = ml.Probes(p, log=lambda m: None)
    assert first._claim_undo(crashed, at), "claimed, then the daemon died before it started the run"
    first.tick(at)   # starts the other one (the crashed claim is first's own record, not a run of first's)
    assert cid in first._undos
    logs = []
    second = ml.Probes(p, log=logs.append)   # the daemon restarted while the run went on
    _undo_round(second, at + 60)
    _undo_round(second, at + 4000)
    gate.touch()
    for proc, _, _, _ in first._undos.values():
        proc.wait(30)
    first.tick(at + 4001)   # the old run's outcome no longer counts: it was settled as interrupted
    assert ran.read_text() == "ran\n", "never run again after a restart"
    for i in (cid, crashed):
        c = _change_entry(ml, i)
        assert not c.get("closed") and "auto_undo_claim" not in c and len(c["auto_undo_runs"]) == 1
        assert c["auto_undo_runs"][0]["rc"] == "interrupted"
        assert "interrupted" in c["auto_undo_failed"]["why"] and "claim: try 1" in c["auto_undo_failed"]["why"]
    pages = {x["key"]: x["text"] for x in _overdue_pages(ml, at + 4002)}
    assert set(pages) == {f"machine-change:{cid}", f"machine-change:{crashed}"}
    assert all("daemon restart interrupted" in t and "claim: try 1" in t for t in pages.values())
    assert any("auto-undo try 1: gave up: a daemon restart" in m for m in logs)


@pytest.mark.parametrize("hold", ["outage", "own pause", "shared pause"])
def test_an_auto_undo_waits_unrun_and_unpaged_while_its_machine_is_out_or_paused(env, tmp_path, hold):
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import shared
    from ttp.db import PAUSED_RESOURCES_KEY
    p = make(env)
    mm.add("box-a", tags="device")
    t0 = time.time()
    ran = tmp_path / "ran"
    cid = _auto_change(ml, p, f"echo ran >> {ran}", t0)
    at = t0 + 3601
    if hold == "outage":
        ml.set_outage("box-a", "hardware repair", "user", "2h", now=at - 10)
    elif hold == "own pause":
        p.db.set_kv(PAUSED_RESOURCES_KEY, {"box-a-device": {"reason": "user hold", "since": at}})
    else:
        (shared.root() / "box-a-device").mkdir(parents=True, exist_ok=True)
        (shared.root() / "box-a-device" / shared.PAUSE_FILE).write_text(json.dumps({"reason": "hold", "by": "x"}))
    logs = []
    probes = ml.Probes(p, log=logs.append)
    for i in range(3):
        _undo_round(probes, at + i * 700)
        assert not _overdue_pages(ml, at + i * 700)
    assert not ran.exists() and not probes._undos and "auto_undo_claim" not in _change_entry(ml, cid)
    assert sum("its auto-undo waits" in m for m in logs) == 1, logs
    if hold == "outage":
        ml.clear_outage("box-a", "user", now=at + 2200)
    elif hold == "own pause":
        p.db.set_kv(PAUSED_RESOURCES_KEY, {})
    else:
        (shared.root() / "box-a-device" / shared.PAUSE_FILE).unlink()
    _undo_round(probes, at + 2300)
    _undo_round(probes, at + 2301)
    assert ran.read_text() == "ran\n" and _change_entry(ml, cid)["closed_why"] == "auto-undo ok"


def test_an_auto_undo_retry_whose_probe_says_not_yet_again_is_paged_once_expired(env, tmp_path):
    """A retry holds back the page only while the entry stays due: once its until-probe says "not yet" again
    the episode ends, and past --expires route pages it. An exit 0 after a close by hand keeps who closed it."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env)
    mm.add("box-a", tags="device")
    t0 = time.time()
    probe = f"test -e {tmp_path / 'done'} || exit 1"
    cid = _auto_change(ml, p, "echo busy; exit 75", t0, until_probe=probe, expires="2h")
    probes = ml.Probes(p, log=lambda m: None)
    probes._mark({(cid, probe): (0, "")}, set(), t0 + 60)
    assert ml.auto_undo_due(_change_entry(ml, cid), t0 + 60)
    claim = probes._claim_undo(cid, t0 + 60)
    probes._settle_undo(cid, claim, 75, "busy", t0 + 61)
    assert _change_entry(ml, cid)["auto_undo_next"] == t0 + 61 + ml.PROBE_GRACE_S
    assert not _overdue_pages(ml, t0 + 62), "a retry waits while due"
    probes._mark({}, {(cid, probe)}, t0 + 120)
    c = _change_entry(ml, cid)
    assert "auto_undo_next" not in c and "auto_undo_tries" not in c, "a later pass starts afresh"
    late = t0 + 3 * 3600
    assert ml.overdue(c, late) and not ml.auto_undo_waits(c, late)
    assert f"machine-change:{cid}" in {x["key"] for x in _overdue_pages(ml, late)}
    # a stored retry with no probe pass (as before this fix's _mark) is paged too
    other = _auto_change(ml, p, "echo busy; exit 75", t0, until_probe=probe, expires="2h")
    with ml._locked():
        doc = ml._read(ml.LEDGER, {})
        next(x for x in doc["changes"] if x["id"] == other).update(auto_undo_next=late + 600, auto_undo_tries=1)
        ml._write(ml.LEDGER, doc)
    assert f"machine-change:{other}" in {x["key"] for x in _overdue_pages(ml, late)}
    # closed by hand while its undo ran: the run is recorded, the close is theirs
    hand = _auto_change(ml, p, "echo ok", t0)
    claim = probes._claim_undo(hand, t0 + 3601)
    ml.close_change(hand, "undone by hand", "alpha", now=t0 + 3602)
    probes._settle_undo(hand, claim, 0, "ok", t0 + 3603)
    c = _change_entry(ml, hand)
    assert c["closed"] == t0 + 3602 and c["closed_by"] == "alpha" and c["closed_why"] == "undone by hand"
    assert c["auto_undo_runs"][-1]["outcome"] == "ok" and c["auto_undone"]["try"] == 1


def test_downwait_probe_gap_backs_off_from_seconds_and_never_passes_the_normal_interval():
    from ttp import downwait
    assert [downwait.gap(n, 180) for n in range(6)] == [180, 15, 30, 60, 120, 120]
    assert downwait.gap(3, 20) == 20


def test_a_task_waiting_on_a_down_machine_runs_its_heal_fix_at_once_and_retries_when_the_host_returns(env):
    """User report: a wait on a down host polled on a flat timer for hours. The daemon now probes it in
    seconds, runs the machine's heal check (its fix) at once, once per episode, and wakes the task as soon
    as the host answers."""
    from ttp import daemon as dmod
    from ttp import downwait, heal
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, up = _heal_setup(env, machine="box-a")
    later = time.time() + 7200   # the check backed off while healthy: it would run in 2 h
    p.db.x("UPDATE schedules SET next_run=? WHERE name='svc'", (later,))
    _ssh_255(p)
    probe = "ssh -o BatchMode=yes box-a true"
    tid = _due_waiting_task(p, probe)
    p.db.update_task(tid, not_before=time.time() + 3600)
    _settle_probe(d, tid)
    assert p.db.one("SELECT next_run FROM schedules WHERE name='svc'")["next_run"] <= time.time(), \
        "the heal check was not made due at once"
    assert d._probe_gap(tid) == downwait.BACKOFF_S[0]
    assert downwait.episodes(p.db)["box-a"]["action"] == "fix"
    assert "machine box-a down while task" in (p.logs / "daemon.log").read_text()
    # Still down on the next probe: no second kick within the episode, a longer gap.
    p.db.x("UPDATE schedules SET next_run=? WHERE name='svc'", (later,))
    d._probed[tid] -= 20
    _settle_probe(d, tid)
    assert p.db.one("SELECT next_run FROM schedules WHERE name='svc'")["next_run"] == later
    assert d._probe_gap(tid) == downwait.BACKOFF_S[1]
    # The kicked check runs its fix once.
    heal.run(d, "svc", _heal_spec(p))
    assert up.exists() and len(heal.state(p.db, "svc")["fixes"]) == 1
    # The host answers: the very next probe passes and the task is due, the episode over.
    _ssh_255(p, up=True)
    d._probed[tid] -= downwait.BACKOFF_S[1] + 1
    _settle_probe(d, tid)
    assert _ready(p, tid) and downwait.episodes(p.db) == {} and d._probe_gap(tid) == dmod.PROBE_EVERY_S


def test_a_down_machine_is_not_acted_on_under_a_pause_a_held_lock_or_an_open_machine_change(env):
    from ttp import downwait, heal
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp.db import PAUSED_RESOURCES_KEY
    mm.add("box-a", tags="device")
    p, d, _ = _heal_setup(env, machine="box-a", resource="box-a-device")
    checks, known = heal.checks(p.db), mm.load()
    later = time.time() + 7200
    p.db.x("UPDATE schedules SET next_run=? WHERE name='svc'", (later,))

    def kicked():
        return p.db.one("SELECT next_run FROM schedules WHERE name='svc'")["next_run"] < later

    # A user pause on the machine (or its device resource): nothing runs, and the pause stays.
    for res in ("box-a", "box-a-device"):
        p.db.set_kv(PAUSED_RESOURCES_KEY, {res: {"reason": "user hold", "since": time.time(), "by": "user"}})
        got = downwait.act(p.db, "box-a", 1, p.name, known, checks)
        assert got["action"] == "blocked" and "paused by user" in got["why"] and not kicked(), res
        assert downwait.act(p.db, "box-a", 1, p.name, known, checks) is None   # said once
        assert res in p.db.paused_resources()
        downwait.clear(p.db, "box-a")
    p.db.set_kv(PAUSED_RESOURCES_KEY, {})
    # Its lock held.
    got = downwait.act(p.db, "box-a", 1, p.name, known, checks, lock_busy=lambda r: r == "box-a-device")
    assert got["action"] == "blocked" and got["why"] == "lock box-a-device is held" and not kicked()
    # A recorded live change on the machine.
    downwait.clear(p.db, "box-a")
    ml.add_change("box-a", "service drop-in for a test", "none: test", by="other", expires=time.time() + 3600)
    got = downwait.act(p.db, "box-a", 1, p.name, known, checks, lock_busy=lambda r: False)
    assert got["action"] == "blocked" and "machine change #" in got["why"] and not kicked()
    # The blocker gone: the same episode acts at once.
    for c in ml.changes(["box-a"]):
        ml.close_change(c["id"], "test over", "other")
    got = downwait.act(p.db, "box-a", 1, p.name, known, checks, lock_busy=lambda r: False)
    assert got["action"] == "fix" and kicked()


def test_a_down_machine_owned_by_another_project_gets_one_note_and_no_action(env):
    from ttp import downwait, heal
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    mm.set_recovery("box-a", owner="other-project")
    p, d, up = _heal_setup(env, machine="box-a")
    later = time.time() + 7200
    p.db.x("UPDATE schedules SET next_run=? WHERE name='svc'", (later,))
    sent: list = []
    for _ in range(3):
        downwait.act(p.db, "box-a", 7, p.name, mm.load(), heal.checks(p.db), send=lambda o, t: sent.append((o, t)))
    assert len(sent) == 1 and sent[0][0] == "other-project" and "box-a is down" in sent[0][1]
    assert p.db.one("SELECT next_run FROM schedules WHERE name='svc'")["next_run"] == later and not up.exists()


def test_a_down_machine_with_no_recovery_raises_one_event_naming_it_and_its_owner(env):
    from ttp import downwait
    from ttp import machines as mm
    p = make(env)
    mm.add("box-b")
    for _ in range(2):
        downwait.act(p.db, "box-b", 3, p.name, mm.load(), [])
    ev = p.db.q("SELECT * FROM events WHERE kind=?", (downwait.EVENT,))
    assert len(ev) == 1 and "box-b is down" in ev[0]["text"] and "no recovery owner" in ev[0]["text"]
    mm.set_recovery("box-b", owner=p.name)   # its own project owns it, still with no fix: named as owner
    downwait.clear(p.db, "box-b")
    downwait.act(p.db, "box-b", 3, p.name, mm.load(), [])
    ev = p.db.q("SELECT * FROM events WHERE kind=? ORDER BY id", (downwait.EVENT,))
    assert len(ev) == 2 and f"its recovery owner {p.name}" in ev[1]["text"]


def test_a_machine_down_for_hours_gets_one_note_or_event_per_episode_and_a_new_one_after_it_answers(env):
    """Review #4196: a still-down machine was acted on again every EPISODE_S, a new host_down event (or a
    high note to its owner) each hour. Now once per episode, until the daemon's ssh check reaches it."""
    from ttp import daemon as dmod
    from ttp import downwait
    from ttp import machines as mm
    p = make(env)
    mm.add("box-b")
    mm.add("box-c")
    mm.set_recovery("box-c", owner="other-project")
    sent: list = []
    t0 = time.time()

    def events():
        return p.db.q("SELECT * FROM events WHERE kind=?", (downwait.EVENT,))

    def down_for_hours(start):
        for h in (0, 2, 5, 20, 30):   # hours: the probe keeps finding both down (seen every few minutes)
            for m in range(0, 60 if h == 30 else 1, 5):
                at = start + h * 3600 + m * 60
                downwait.act(p.db, "box-b", 3, p.name, mm.load(), [], now=at)
                downwait.act(p.db, "box-c", 3, p.name, mm.load(), [], now=at, send=lambda o, t: sent.append(o))

    down_for_hours(t0)
    assert len(events()) == 1 and sent == ["other-project"]
    # The daemon's ssh check (not a probe) reaches them: their episodes end; the next outage is told again.
    d = dmod.Daemon(p.base)
    later = t0 + 31 * 3600
    d._host_up.update({"box-b": (True, t0 - 60), "box-c": (False, later)})   # older than the episode / still down
    d._end_down_episodes()
    assert set(downwait.episodes(p.db)) == {"box-b", "box-c"}
    _ssh_255(p, up=True)
    d._check_hosts(["box-b", "user@box-c"], later)
    d._end_down_episodes()
    assert downwait.episodes(p.db) == {}
    down_for_hours(later + 60)
    assert len(events()) == 2 and sent == ["other-project"] * 2
    # A wait that ended while its host was down leaves a stale episode: a day with no probe naming it ends it.
    gone = later + 40 * 3600 + downwait.STALE_S
    downwait.act(p.db, "box-b", 4, p.name, mm.load(), [], now=gone)
    assert len(events()) == 3


def test_a_down_machine_heal_fix_is_kicked_again_after_an_episode_hour(env):
    from ttp import downwait, heal
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, _ = _heal_setup(env, machine="box-a")
    checks, known, t0 = heal.checks(p.db), mm.load(), time.time()
    far = t0 + 10 * 3600

    def kicked_at(at):
        p.db.x("UPDATE schedules SET next_run=? WHERE name='svc'", (far,))
        downwait.act(p.db, "box-a", 1, p.name, known, checks, now=at, lock_busy=lambda r: False)
        return p.db.one("SELECT next_run FROM schedules WHERE name='svc'")["next_run"] == at

    assert kicked_at(t0)
    assert not kicked_at(t0 + 600) and not kicked_at(t0 + downwait.EPISODE_S - 1)
    assert kicked_at(t0 + downwait.EPISODE_S)
    assert not kicked_at(t0 + downwait.EPISODE_S + 600) and kicked_at(t0 + 2 * downwait.EPISODE_S + 1)


def test_an_outage_check_with_a_fix_runs_it_when_ping_shows_the_box_off(env, monkeypatch):
    """A box confirmed off (ping_host silent, ping_peer answering) is unhealthy for a check with a fix,
    so the fix runs past its grace instead of waiting out the unknown outage window to escalate."""
    from ttp import heal
    from ttp import machines as mm
    mm.add("box-a", tags="device")
    p, d, _ = _heal_setup(env)
    fixed = p.root / "fixed"
    monkeypatch.setattr(heal, "_pings", lambda h, cwd: h == "box-b")
    block = {"check": "echo 'ssh: connect to host box-a port 22: No route to host' >&2; exit 255",
             "fix": f"touch {fixed}", "machine": "box-a", "outage": True, "grace_s": 60, "settle_s": 0,
             "ping_host": "box-a", "ping_peer": "box-b"}
    spec, t0 = heal.validate(block), time.time()
    status, again = heal.run(d, "off", spec, t0)
    assert "grace" in status and not fixed.exists() and again == t0 + 60
    heal.run(d, "off", spec, t0 + 61)
    assert fixed.exists() and len(heal.state(p.db, "off")["fixes"]) == 1
    # Our own route (the peer silent too) still never runs the fix blind.
    monkeypatch.setattr(heal, "_pings", lambda h, cwd: False)
    fixed.unlink()
    heal.run(d, "route", spec, t0)
    heal.run(d, "route", spec, t0 + 3700)
    assert not fixed.exists() and heal.state(p.db, "route")["route_only"]
