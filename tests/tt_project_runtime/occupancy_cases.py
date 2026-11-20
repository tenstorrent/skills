# Box occupancy tests (occupancy.py: the shared "does a live realtime stream hold this box?" check, its call
# sites, the watchdog and the runner stop switch) of tests/test_tt_project_runtime.py, which runs this file
# in its own namespace (see _include_cases there).
from __future__ import annotations


def _occ_home(tmp_path, monkeypatch):
    from ttp import occupancy, project
    home = tmp_path / "occ-home"
    home.mkdir()
    monkeypatch.setattr(project, "HOME_DIR", home)
    return occupancy


class _OccProj:
    def __init__(self, name, base, sources, quiet_s=60):
        self.name, self.base = name, base
        self._cfg = {"occupancy": {"sources": sources, "quiet_s": quiet_s}}

    def config(self):
        return self._cfg


def _occ_flag_file(tmp_path, name, text):
    f = tmp_path / name
    f.write_text(text)
    return f


def test_occupancy_command_source_live_free_unknown_and_timeout(tmp_path, monkeypatch):
    occ = _occ_home(tmp_path, monkeypatch)
    for alias, cmd, want in (("box-live", "exit 0", "live"), ("box-free", "exit 1", "free"),
                             ("box-odd", "exit 3", "unknown"), ("box-slow", "sleep 5", "unknown")):
        occ.publish(_OccProj("proj-" + alias, tmp_path, {alias: {"cmd": cmd, "timeout_s": 0.5}}))
        res = occ.check(alias, "test", fresh=True)
        row = res["sources"][0]
        assert row["read"] == want, (alias, row)
        assert row["project"] == "proj-" + alias and row["at"]
        assert res["blocks"] is (want != "free"), res   # unknown counts as live: never a silent pass
    assert "timed out" in occ.check("box-slow", fresh=True)["sources"][0]["detail"] or \
        occ.check("box-slow", fresh=True)["state"] == "unknown"


def test_occupancy_no_source_is_free_and_changes_nothing(tmp_path, monkeypatch):
    occ = _occ_home(tmp_path, monkeypatch)
    res = occ.check("box-a", "reset")
    assert res["state"] == "free" and not res["blocks"] and res["sources"] == []
    assert occ.blocks("box-a", "deploy") == "" and occ.blocks("", "test") == ""
    import json as _json
    from ttp import project
    assert _json.loads(_json.dumps(project.DEFAULT_CONFIG["occupancy"])) == {"sources": {}, "quiet_s": 60, "alerts": True}


def test_occupancy_quiet_window_holds_a_minute_after_the_stream_ends(tmp_path, monkeypatch):
    occ = _occ_home(tmp_path, monkeypatch)
    flag = _occ_flag_file(tmp_path, "stream", "live")
    occ.publish(_OccProj("streamer", tmp_path, {"box-a": {"file": str(flag), "max_age_s": 10_000}}))
    t0 = time.time()
    assert occ.check("box-a", "test", now=t0, fresh=True)["state"] == "live"
    flag.write_text("free")
    after = occ.check("box-a", "test", now=t0 + 5, fresh=True)
    assert after["blocks"] and after["sources"][0]["state"] == "quiet"
    assert abs(after["clear_at"] - (t0 + 5 + 60)) < 6
    assert occ.check("box-a", "test", now=t0 + 40, fresh=True)["blocks"]
    late = occ.check("box-a", "test", now=t0 + 70, fresh=True)
    assert not late["blocks"] and late["state"] == "free"


def test_occupancy_quiet_after_an_unknown_read_says_unknown_never_stream_ended(tmp_path, monkeypatch):
    occ = _occ_home(tmp_path, monkeypatch)
    flag = _occ_flag_file(tmp_path, "state", "exit 3")
    occ.publish(_OccProj("streamer", tmp_path, {"box-a": {"cmd": f"exit $(cat {flag})", "timeout_s": 2}}))
    t0 = time.time()
    assert occ.check("box-a", "test", now=t0, fresh=True)["state"] == "unknown"
    flag.write_text("1")
    why = occ.check("box-a", "test", now=t0 + 5, fresh=True)["why"]
    assert "unknown (its source could not tell at " in why and "quiet until " in why and "stream ended" not in why, why
    # a live read then free keeps the stream-ended wording
    flag.write_text("0")
    assert occ.check("box-a", "test", now=t0 + 10, fresh=True)["state"] == "live"
    flag.write_text("1")
    assert "its stream ended, quiet until " in occ.check("box-a", "test", now=t0 + 15, fresh=True)["why"]


def test_occupancy_sources_of_two_projects_for_one_alias_both_count(tmp_path, monkeypatch):
    occ = _occ_home(tmp_path, monkeypatch)
    occ.publish(_OccProj("proj-one", tmp_path, {"box-a": {"cmd": "exit 1"}}))
    occ.publish(_OccProj("proj-two", tmp_path, {"box-a": {"cmd": "exit 0"}}))
    res = occ.check("box-a", "test", fresh=True)
    assert {r["project"] for r in res["sources"]} == {"proj-one", "proj-two"}
    assert res["blocks"] and "proj-two" in res["why"]
    # a project dropping its source removes only its own registration
    occ.publish(_OccProj("proj-two", tmp_path, {}))
    assert not occ.check("box-a", "test", fresh=True)["blocks"]


def test_occupancy_front_box_blocks_deploy_not_test(tmp_path, monkeypatch):
    occ = _occ_home(tmp_path, monkeypatch)
    occ.publish(_OccProj("streamer", tmp_path, {"box-a": {"cmd": "exit 0", "fronts": ["router-a"]}}))
    assert occ.blocks("router-a", "deploy") and occ.blocks("router-a", "restart")
    for step in ("test", "bench", "warmup", "health", "reset"):
        assert occ.blocks("router-a", step) == "", step
    assert occ.blocks("box-a", "test")
    # once the stream is over (and its quiet window), the front box is free for deploys again
    occ.publish(_OccProj("streamer", tmp_path, {"box-a": {"cmd": "exit 1", "fronts": ["router-a"]}}))
    assert occ.blocks("router-a", "deploy", now=time.time() + 600) == ""


def test_occupancy_broker_work_without_a_stream_is_never_blocked(tmp_path, monkeypatch):
    occ = _occ_home(tmp_path, monkeypatch)
    occ.publish(_OccProj("streamer", tmp_path, {"box-a": {"cmd": "exit 1"}}))
    for step in occ.STEPS:
        assert occ.blocks("box-a", step, now=time.time() + 600) == "", step
    assert occ.blocks("box-b-device", "test") == ""


def test_occupancy_cli_check_lock_and_detach_refuse_while_live(env, tmp_path):
    p = make(env)
    p.set_config("occupancy.sources", {"box-a": {"cmd": "exit 0"}})
    e = {**os.environ, "TTP_PROJECT": str(p.root), "TTP_RUN_DIR": str(tmp_path / "run")}
    (tmp_path / "run").mkdir()
    r = subprocess.run([sys.executable, str(TTP), "occupancy", "box-a"], env=e, capture_output=True, text=True)
    assert r.returncode == 75 and "live" in r.stdout, r
    r = subprocess.run([sys.executable, str(TTP), "occupancy", "box-a", "--json"], env=e, capture_output=True,
                       text=True)
    assert json.loads(r.stdout)["blocks"] is True
    # A held start waits (looking again on its own) instead of being refused; past its --timeout the task is
    # told to hand back waiting on the probe, which answers busy while the box is held.
    r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "box-a", "--", "true"],
                       env={**e, "TTP_TEST_POLL_S": "0.2"}, capture_output=True, text=True)
    assert r.returncode == 75 and "live realtime stream" in r.stderr and "waiting:" in r.stderr, r
    assert "retry_when `ttp lock --probe box-a`" in r.stderr, r
    r = subprocess.run([sys.executable, str(TTP), "occupancy", "--wait", "--timeout", "1", "box-a"],
                       env={**e, "TTP_TEST_POLL_S": "0.2"}, capture_output=True, text=True)
    assert r.returncode == 75 and "waiting:" in r.stderr, r
    r = subprocess.run([sys.executable, str(TTP), "lock", "--probe", "box-a"], env=e, capture_output=True, text=True)
    assert r.returncode == 75, r
    r = subprocess.run([sys.executable, str(TTP), "detach", "--remote", "box-a", "job1", "--", "true"], env=e,
                       capture_output=True, text=True)
    assert r.returncode == 75 and "not started" in r.stderr, r
    r = subprocess.run([sys.executable, str(TTP), "occupancy", "box-free"], env=e, capture_output=True, text=True)
    assert r.returncode == 0, r


def test_occupancy_daemon_restart_upgrade_heal_and_spill_hold_while_live(env, monkeypatch):
    from ttp import checkspill, heal, occupancy, project, service
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    here = project.hostname()
    occ.publish(_OccProj("streamer", p.root, {here: {"cmd": "exit 0"}, "box-a": {"cmd": "exit 0"}}))
    assert "was not restarted" in service.occupied(p)
    assert heal._occupied(p, {"machine": "box-a"}, "health")
    assert heal._occupied(p, {"resource": "box-b"}, "reset") == ""
    assert occupancy.blocks_any(["box-a", "", "elsewhere"], "test")
    src = pathlib.Path(checkspill.__file__).read_text()
    assert 'occupancy.blocks_any([alias, machine or "", host or ""], "test", now)' in src
    cli_src = (pathlib.Path(checkspill.__file__).parent / "cli.py").read_text()
    assert '_occupancy_hold(p, a, "deploy"' in cli_src and 'devq {a.op}: not sent' in cli_src


def test_occupancy_watchdog_flags_an_overlap_and_posts_once(env, monkeypatch):
    from ttp import activity, occupancy
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    occ.publish(_OccProj("streamer", p.root, {"box-a": {"cmd": "exit 0"}}))
    now = time.time()
    span = {"id": "j1", "kind": "lock", "start": now, "end": None, "resource": "box-a", "machine": "box-a",
            "task": 7, "cmd": "pytest", "disruptive": False}
    monkeypatch.setattr(activity, "entries", lambda state: [span])
    monkeypatch.setattr(activity, "spans", lambda recs, here: [span])
    posts = []
    out = occupancy.watch(p, p.db, now, lambda key, text, sev="high": posts.append((key, text, sev)))
    assert len(out["flags"]) == 1 and len(posts) == 1
    assert "box-a" in posts[0][1] and "#7" in posts[0][1]
    occupancy.watch(p, p.db, now + 1, lambda key, text, sev="high": posts.append((key, text, sev)))
    assert len(posts) == 1   # posted once
    assert any("box-a" in line for line in occupancy.status_lines(p.db, now + 2))
    # a job on a box no stream holds is never flagged
    span2 = {**span, "id": "j2", "resource": "box-z", "machine": "box-z"}
    monkeypatch.setattr(activity, "spans", lambda recs, here: [span2])
    assert occupancy.watch(p, p.db, now + 3, None)["flags"] == []


def test_occupancy_watchdog_front_box_flags_only_deploys(env, monkeypatch):
    from ttp import activity, occupancy
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    occ.publish(_OccProj("streamer", p.root, {"box-a": {"cmd": "exit 0", "fronts": ["router-a"]}}))
    now = time.time()
    test_span = {"id": "t1", "kind": "lock", "start": now, "end": None, "resource": "router-a",
                 "machine": "router-a", "task": 3, "cmd": "pytest"}
    deploy_span = {**test_span, "id": "d1", "step": "deploy"}
    monkeypatch.setattr(activity, "entries", lambda state: [])
    monkeypatch.setattr(activity, "spans", lambda recs, here: [test_span, deploy_span])
    out = occupancy.watch(p, p.db, now, None)
    assert [f["step"] for f in out["flags"]] == ["deploy"]


def _watch_with(monkeypatch, spans):
    from ttp import activity
    monkeypatch.setattr(activity, "entries", lambda state: [])
    monkeypatch.setattr(activity, "spans", lambda recs, here: spans)


def test_occupancy_watchdog_flags_only_device_use_of_the_box(env, monkeypatch):
    """A CPU check, a test suite and a host process driving other hosts (ssh, a cluster, docker over ssh) that
    ran on the box's host during a live window are not flagged: only spans on the box's own devices are."""
    from ttp import occupancy, project
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    here = project.hostname()
    occ.publish(_OccProj("streamer", p.root, {here: {"cmd": "exit 0"}}))
    now = time.time()
    base = {"kind": "lock", "start": now, "end": None, "host": here, "task": 5, "cmd": "run"}
    spans = [{**base, "id": "cpu", "resource": "checks-slot"},            # a ttp checks run
             {**base, "id": "suite", "kind": "job"},                      # a test suite, no resource
             {**base, "id": "ssh", "resource": "cluster-a"},              # drives another host
             {**base, "id": "drv", "machine": "box-far"},                 # a remote job elsewhere
             {**base, "id": "dev", "resource": f"{here}-device"},         # the box's device lock
             {**base, "id": "rst", "step": "reset"}]                      # a reset step on this host
    _watch_with(monkeypatch, spans)
    out = occupancy.watch(p, p.db, now, None)
    assert sorted(f["key"].split(":")[0] for f in out["flags"]) == ["dev", "rst"], out["flags"]


def test_occupancy_watchdog_counts_a_host_job_only_against_the_box_it_drives(env, monkeypatch):
    """A job launched on host H that drives box B over ssh (`ttp lock B -- ssh B ...`, a reset step naming B, a
    remote job on B) never counts against H while a stream holds H; it counts against B only while B is held and
    it uses B's own devices. Device work on H itself is still flagged against H."""
    from ttp import occupancy, project
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    here = project.hostname()

    def flagged(now, tag):
        base = {"kind": "lock", "start": now, "end": None, "host": here, "task": 9, "cmd": "ssh box-b run"}
        _watch_with(monkeypatch, [
            {**base, "id": f"lockb{tag}", "resource": "box-b"},                  # ttp lock box-b -- ssh box-b ...
            {**base, "id": f"rstb{tag}", "step": "reset", "resource": "box-b"},  # a reset of box-b from H
            {**base, "id": f"devb{tag}", "resource": "box-b-device"},            # box-b's device lock
            {**base, "id": f"remb{tag}", "kind": "job", "machine": "box-b", "devices": 1},   # a remote device job
            {**base, "id": f"devh{tag}", "resource": f"{here}-device", "cmd": "bench"}])   # device work on H
        return sorted((f["key"].split(":")[0], f["alias"]) for f in occupancy.watch(p, p.db, now, None)["flags"])

    now = time.time()
    # host_job_driving_other_box_not_flagged: only H is held; nothing that drives box-b counts against H.
    occ.publish(_OccProj("streamer", p.root, {here: {"cmd": "exit 0"}, "box-b": {"cmd": "exit 1"}}))
    assert flagged(now, 1) == [("devh1", here)]
    # device_job_on_held_box_flagged: once box-b is held too, its device work counts against box-b, never H.
    occ.publish(_OccProj("streamer", p.root, {here: {"cmd": "exit 0"}, "box-b": {"cmd": "exit 0"}}))
    later = now + occupancy.CACHE_S + 1
    assert flagged(later, 2) == sorted([("devb2", "box-b"), ("devh2", here), ("lockb2", "box-b"),
                                        ("remb2", "box-b"), ("rstb2", "box-b")])


def test_occupancy_watchdog_never_flags_a_job_that_started_before_the_stream(env, monkeypatch):
    from ttp import occupancy
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    occ.publish(_OccProj("streamer", p.root, {"box-a": {"cmd": "exit 0"}}))
    now = time.time()
    old = {"id": "old", "kind": "lock", "start": now - 3600, "end": None, "resource": "box-a-device", "task": 1,
           "cmd": "bench"}
    new = {**old, "id": "new", "start": now + 2, "task": 2}
    _watch_with(monkeypatch, [old, new])
    out = occupancy.watch(p, p.db, now + 2, None)
    assert [f["task"] for f in out["flags"]] == [2], out["flags"]


def test_occupancy_watchdog_posts_one_alert_per_stream_event_with_the_source_reason(env, monkeypatch):
    from ttp import occupancy
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    occ.publish(_OccProj("streamer", p.root, {"box-a": {"cmd": "echo hold with no owner shown; exit 0"}}, quiet_s=0))
    now = time.time()
    mk = lambda i, t: {"id": f"d{i}", "kind": "lock", "start": t, "end": None, "resource": "box-a-device",   # noqa: E731
                       "task": i, "cmd": "bench"}
    posts = []
    post = lambda key, text, sev="high": posts.append((key, text, sev))   # noqa: E731
    _watch_with(monkeypatch, [mk(i, now) for i in range(12)])
    out = occupancy.watch(p, p.db, now, post)
    assert len(out["flags"]) == 12 and len(posts) == 1, posts
    text = posts[0][1]
    assert "12 job(s)" in text and "+2 more" in text and "hold with no owner shown" in text, text
    assert text.count("task #") == 10, text
    # a job joining the same event is kept for status but posts nothing more
    _watch_with(monkeypatch, [mk(i, now) for i in range(12)] + [mk(99, now + 5)])
    assert len(occupancy.watch(p, p.db, now + 5, post)["flags"]) == 1 and len(posts) == 1
    assert len(occupancy.recent_flags(p.db, 86400, now + 6)) == 13


def test_occupancy_stop_switch_and_should_stop(env, tmp_path):
    p = make(env)
    e = {**os.environ, "TTP_PROJECT": str(p.root)}
    run = lambda *a: subprocess.run([sys.executable, str(TTP), "occupancy", *a], env=e, capture_output=True,
                                    text=True)
    assert run("--should-stop", "bench-1").returncode == 1
    assert run("--stop-switch", "bench-1").returncode == 0
    r = run("--should-stop", "bench-1")
    assert r.returncode == 0 and "stop switch set" in r.stdout
    assert "cleared" in run("--clear-stop", "bench-1").stdout
    assert run("--should-stop", "bench-1").returncode == 1
    # the runner also stops while a live stream holds its box
    p.set_config("occupancy.sources", {"box-a": {"cmd": "exit 0"}})
    assert run("--should-stop", "bench-1", "box-a").returncode == 0
    assert run("--should-stop", "../x").returncode != 0


def test_occupancy_detach_stop_reaches_a_schedule_runner(env):
    from ttp import occupancy, schedrun
    p = make(env)
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        p.db.set_kv(schedrun.GROUPS_KEY, {str(proc.pid): {"schedule": "warm-up", "start": schedrun.proc_start(proc.pid)}})
        e = {**os.environ, "TTP_PROJECT": str(p.root)}
        e.pop("TTP_RUN_DIR", None)
        r = subprocess.run([sys.executable, str(TTP), "detach", "--stop", "warm-up"], env=e, capture_output=True,
                           text=True)
        assert r.returncode == 0 and "stop switch set" in r.stdout, r
        assert proc.wait(timeout=10) != 0
        assert occupancy.should_stop(p, "warm-up")
        assert not (p.db.kv(schedrun.GROUPS_KEY, {}) or {})
    finally:
        if proc.poll() is None:
            proc.kill()


def test_occupancy_registry_drops_a_project_whose_folder_is_gone(tmp_path, monkeypatch):
    occ = _occ_home(tmp_path, monkeypatch)
    gone = tmp_path / "deleted-project"
    gone.mkdir()
    occ.publish(_OccProj("stale", gone, {"box-a": {"cmd": "exit 0"}}))
    occ.publish(_OccProj("alive", tmp_path, {"box-b": {"cmd": "exit 1"}}))
    assert occ.blocks("box-a", "test")   # its folder exists: the registration counts
    gone.rmdir()
    assert set(occ.registry()) == {"alive"}
    assert occ.blocks("box-a", "test") == "" and not occ.check("box-a", "test", fresh=True)["sources"]
    assert occ.check("box-b", "test", fresh=True)["sources"]   # other projects' registrations stay


def test_occupancy_internal_error_holds_a_box_with_a_source_and_frees_the_rest(tmp_path, monkeypatch):
    occ = _occ_home(tmp_path, monkeypatch)
    occ.publish(_OccProj("streamer", tmp_path, {"box-a": {"cmd": "exit 1", "fronts": ["router-a"]}}))

    def boom(*a, **k):
        raise RuntimeError("internal fault")
    monkeypatch.setattr(occ, "check", boom)
    why = occ.blocks("box-a", "test")
    assert why and "unknown" in why and "box-a" in why
    assert occ.blocks("box-a-device", "bench")   # a resource on the box is held too
    assert occ.blocks("router-a", "deploy") and occ.blocks("router-a", "restart")
    assert occ.blocks("router-a", "test") == ""   # a front box is covered only for deploys and restarts
    assert occ.blocks("box-other", "test") == ""   # no source registered: stays free
    log = (occ._path("occupancy-errors.log")).read_text()
    assert "RuntimeError: internal fault" in log
    # a registry too broken to read still holds nothing it cannot tie to a source, and never raises
    monkeypatch.setattr(occ, "covered", boom)
    assert occ.blocks("box-a", "test") and occ.blocks("box-other", "test") == ""
    occ._path(occ.SOURCES).write_text("not json")
    assert occ.blocks("box-a", "test") == ""


def test_occupancy_watchdog_never_flags_a_record_with_no_live_process(env, monkeypatch):
    """A record whose process is gone is closed (reaped) on the pass and never flagged; one whose liveness is
    unknown (another PID namespace or host) is not flagged for starting before the window; a job of the window is."""
    from ttp import activity, occupancy, project
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    here = project.hostname()
    occ.publish(_OccProj("streamer", p.root, {here: {"cmd": "exit 0"}}))
    now = time.time()
    t0 = now - 300
    lock = lambda n: str(p.state / f"{n}.rc")   # noqa: E731
    live_lock = open(str(p.state / "live.lock"), "w")
    import fcntl
    fcntl.flock(live_lock, fcntl.LOCK_EX)
    try:
        rec = lambda i, **kw: {"ev": "job", "id": i, "project": p.name, "host": here, "task": 3, "start": t0,   # noqa: E731
                               "resource": f"{here}-device", "cmd": "bench", **kw}
        for r in (rec("stale", rcfile=lock("stale")), rec("alive", rcfile=lock("live")),
                  rec("far", host="elsewhere"), rec("remote", machine=here, host="elsewhere")):
            activity.append(p.state, r)
        out = occupancy.watch(p, p.db, now, None)   # the stream goes live now; the records started before it
        assert out["reaped"] == ["stale"], out
        spans = {s["id"]: s for s in activity.spans(activity.entries(p.state), here)}
        assert spans["stale"]["state"] == "ended" and spans["stale"]["reaped"] is True
        assert spans["alive"]["state"] == "running" and spans["far"]["state"] == "unknown"
        # a fresh pass reaps nothing more; a job started inside the window and still alive is flagged, and so is
        # a job on the box from another host (unknown liveness) by its start, while the old ones are not
        activity.append(p.state, rec("new", rcfile=lock("live"), start=now + 5))
        activity.append(p.state, rec("newfar", host="elsewhere", start=now + 5))
        out = occupancy.watch(p, p.db, now + 10, None)
        assert "reaped" not in out and sorted(f["key"].split(":")[0] for f in out["flags"]) == ["new", "newfar"], out
    finally:
        live_lock.close()


def test_occupancy_unknown_read_holds_the_box_but_charges_no_job_and_says_so_once_per_source(env, monkeypatch):
    """A source that cannot tell (an odd exit, a timeout) still holds the box for new work, but a job that ran
    only while it could not tell gets no flag and no stay-off alert: one low line per source instead. A real
    live stream flags and alerts at high severity as before."""
    from ttp import occupancy
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    occ.publish(_OccProj("streamer", p.root, {"box-a": {"cmd": "exit 0"}, "box-b": {"cmd": "exit 3"},
                                              "box-c": {"cmd": "exit 3"}}, quiet_s=0))
    now = time.time()
    posts = []
    post = lambda key, text, sev="high": posts.append((key, text, sev))   # noqa: E731
    mk = lambda i, box: {"id": f"{box}{i}", "kind": "lock", "start": now, "end": None, "resource": f"{box}-device",   # noqa: E731
                         "task": i, "cmd": "bench"}
    _watch_with(monkeypatch, [mk(i, b) for i in range(30) for b in ("box-a", "box-b", "box-c")])
    out = occupancy.watch(p, p.db, now, post)
    assert {f["alias"] for f in out["flags"]} == {"box-a"} and all(f["reading"] == "live" for f in out["flags"])
    keys = sorted((k, sev) for k, _, sev in posts)
    assert keys == [("occupancy-source-unknown:streamer:box-b", "low"), ("occupancy-source-unknown:streamer:box-c", "low"),
                    (keys[2][0], "high")] and keys[2][0].startswith("occupancy:violation:box-a"), keys
    low = dict((k, t) for k, t, _ in posts)["occupancy-source-unknown:streamer:box-b"]
    assert "could not tell" in low and "box-b" in low and "streamer" in low
    assert sorted(h["key"] for h in out["unknown_hits"]) == ["occupancy-source-unknown:streamer:box-b",
                                                             "occupancy-source-unknown:streamer:box-c"]
    # the fail-safe hold stays: the unknown box still keeps new work off
    for box in ("box-b", "box-c"):
        res = occupancy.check(box, "test", now + 1)
        assert res["blocks"] and res["state"] == "unknown", res
    # later passes, new jobs in the same unknown span and a restart (kept in the database) post nothing more
    _watch_with(monkeypatch, [mk(i, b) for i in range(30, 33) for b in ("box-b", "box-c")])
    out = occupancy.watch(p, p.db, now + 25, post)
    assert out["flags"] == [] and out["unknown_hits"] == [] and len(posts) == 3
    assert not any("box-b" in f["alias"] or "box-c" in f["alias"] for f in occupancy.recent_flags(p.db, now=now + 26))


def test_occupancy_mixed_live_and_unknown_span_alerts_only_on_its_live_part(env, monkeypatch, tmp_path):
    """A source that reads live, then cannot tell, then live again: jobs that started in a live part are flagged
    and alerted; a job that started only while it could not tell is not (one low line), though the box held."""
    from ttp import occupancy
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    rc = tmp_path / "rc"
    rc.write_text("0")
    occ.publish(_OccProj("streamer", p.root, {"box-a": {"cmd": f"exit $(cat {rc})"}}, quiet_s=1))
    t0 = time.time()
    posts = []
    post = lambda key, text, sev="high": posts.append((key, sev))   # noqa: E731
    mk = lambda name, at: {"id": name, "kind": "lock", "start": at, "end": None, "resource": "box-a-device",   # noqa: E731
                           "task": 5, "cmd": "bench"}
    _watch_with(monkeypatch, [])
    occupancy.watch(p, p.db, t0, post)   # live from t0
    rc.write_text("3")
    jobs = [mk("in-live", t0 + 10)]
    _watch_with(monkeypatch, jobs)
    out = occupancy.watch(p, p.db, t0 + 30, post)   # the read that cannot tell ends the live part at t0+30
    assert [f["key"].split(":")[0] for f in out["flags"]] == ["in-live"]
    jobs.append(mk("in-unknown", t0 + 40))
    out = occupancy.watch(p, p.db, t0 + 60, post)
    assert out["flags"] == [] and [h["stream"] for h in out["unknown_hits"]] == ["box-a"]
    assert occupancy.check("box-a", "test", t0 + 61)["blocks"]   # held all along
    rc.write_text("0")
    jobs.append(mk("in-live-again", t0 + 95))
    occupancy.watch(p, p.db, t0 + 90, post)   # live again from t0+90
    out = occupancy.watch(p, p.db, t0 + 120, post)
    assert [f["key"].split(":")[0] for f in out["flags"]] == ["in-live-again"]
    assert [sev for _, sev in posts] == ["high", "low", "high"], posts
    assert [k.split(":")[0] for k, _ in posts] == ["occupancy", "occupancy-source-unknown", "occupancy"]


def test_occupancy_reading_written_before_live_spans_counts_only_a_live_hold(env, monkeypatch):
    """A shared reading written by an older release (no live spans) charges jobs only when its last hold was a
    live read; its next read starts keeping live spans."""
    from ttp import occupancy
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    occ.publish(_OccProj("streamer", p.root, {"box-a": {"cmd": "exit 3"}, "box-b": {"cmd": "exit 3"}}, quiet_s=0))
    now = time.time()
    old = lambda state: {"state": state, "detail": "old", "at": now, "spans": [[now - 100, None]],   # noqa: E731
                         "last_live": now, "live_state": state, "live_detail": "old", "quiet_s": 0}
    occ._save(occ.READINGS, {"streamer:box-a": old("live"), "streamer:box-b": old("unknown")})
    mk = lambda box: {"id": box, "kind": "lock", "start": now - 50, "end": None, "resource": f"{box}-device",   # noqa: E731
                      "task": 1, "cmd": "bench"}
    _watch_with(monkeypatch, [mk("box-a"), mk("box-b")])
    out = occupancy.watch(p, p.db, now, None)
    assert [f["alias"] for f in out["flags"]] == ["box-a"] and [h["alias"] for h in out["unknown_hits"]] == ["box-b"]
    rec = occ._reading("streamer", "box-b", {"cmd": "exit 3"}, now + 1, True)
    assert rec["live_spans"] == [] and rec["spans"] == [[now - 100, None]]


def test_occupancy_watchdog_flags_a_remote_job_started_inside_the_window_once(env, monkeypatch):
    """A `ttp detach --remote <box>` job logs only its start here, so its liveness stays unknown: it is flagged
    when it started inside the live window (once), and not when it started before the window."""
    from ttp import activity, occupancy, project
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    here = project.hostname()
    occ.publish(_OccProj("streamer", p.root, {"box-a": {"cmd": "exit 0"}}, quiet_s=0))
    now = time.time()
    rec = lambda i, **kw: {"ev": "job", "id": i, "project": p.name, "host": here, "task": 3, "machine": "box-a",   # noqa: E731
                           "cmd": "bench", "devices": 1, **kw}
    activity.append(p.state, rec("old", start=now - 600))
    out = occupancy.watch(p, p.db, now, None)   # the stream goes live now
    assert out["flags"] == [], out
    activity.append(p.state, rec("new", start=now + 5))
    activity.append(p.state, rec("other", start=now + 5, machine="box-far"))
    posts = []
    post = lambda key, text, sev="high": posts.append((key, text, sev))   # noqa: E731
    for t in (10, 20):
        out = occupancy.watch(p, p.db, now + t, post)
        assert sorted(f["key"].split(":")[0] for f in out["flags"]) == (["new"] if t == 10 else []), out
    assert len(posts) == 1 and "box-a" in posts[0][1] and posts[0][2] == "high"


def _occ_alert_post(p):
    """The daemon's post (Daemon.watch_occupancy): an outbound alert keyed by the event, which opens an episode."""
    return lambda key, text, sev="high": p.db.post("out", text, chat=None, kind="alert", severity=sev, ref=key)


def _occ_episodes(p):
    return [(r["key"].split(":")[2], r["cleared"] is not None, r["cleared_why"])
            for r in p.db.q("SELECT key, cleared, cleared_why FROM alerts WHERE key LIKE 'occupancy:violation:%' ORDER BY id")]


def test_occupancy_stay_off_alert_closes_once_its_jobs_end_and_a_later_job_in_the_window_raises_it_again(env, monkeypatch):
    """One alert per box and stream window, whatever its jobs and passes; it closes (told once) when the last job
    it flagged ends, and a job started later in the same window raises that one alert again."""
    from ttp import occupancy
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    occ.publish(_OccProj("streamer", p.root, {"box-a": {"cmd": "exit 0"}}, quiet_s=0))
    now = time.time()
    post = _occ_alert_post(p)
    mk = lambda i, t, end=None: {"id": f"d{i}", "kind": "lock", "start": t, "end": end,   # noqa: E731
                                 "resource": "box-a-device", "task": i, "cmd": "bench"}
    _watch_with(monkeypatch, [mk(1, now), mk(2, now)])
    occupancy.watch(p, p.db, now, post)
    _watch_with(monkeypatch, [mk(1, now), mk(2, now), mk(3, now + 5)])
    occupancy.watch(p, p.db, now + 5, post)
    assert _occ_episodes(p) == [("box-a", False, None)]   # one alert for three jobs over two passes
    _watch_with(monkeypatch, [mk(1, now, now + 8), mk(2, now, now + 9), mk(3, now + 5)])
    occupancy.watch(p, p.db, now + 10, post)
    assert _occ_episodes(p) == [("box-a", False, None)]   # one job still runs there
    _watch_with(monkeypatch, [mk(1, now, now + 8), mk(2, now, now + 9), mk(3, now + 5, now + 12)])
    occupancy.watch(p, p.db, now + 15, post)
    assert _occ_episodes(p) == [("box-a", True, "every job it flagged ended")]
    told = p.db.q("SELECT text FROM messages WHERE kind='resolved' AND ref LIKE 'occupancy:violation:%'")
    assert len(told) == 1 and "box-a" in told[0]["text"], told
    occupancy.watch(p, p.db, now + 20, post)
    assert len(_occ_episodes(p)) == 1   # nothing reopens on its own
    _watch_with(monkeypatch, [mk(1, now, now + 8), mk(4, now + 25)])
    occupancy.watch(p, p.db, now + 25, post)
    assert [e[1] for e in _occ_episodes(p)] == [True, False]


def test_occupancy_stay_off_alert_of_a_remote_job_closes_once_the_stream_ends(env, monkeypatch):
    """A remote job's end is known only on its host: its alert holds while the stream is live and closes once
    the stream's window ended; a job still known to run on the box keeps it open past the window."""
    from ttp import occupancy
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    flag = _occ_flag_file(env["home"], "stream", "live")
    occ.publish(_OccProj("streamer", p.root, {"box-a": {"file": str(flag), "max_age_s": 10_000},
                                              "box-b": {"file": str(flag), "max_age_s": 10_000}}, quiet_s=0))
    now = time.time()
    post = _occ_alert_post(p)
    remote = {"id": "r1", "kind": "job", "start": now, "end": None, "state": "unknown", "machine": "box-a",
              "task": 5, "cmd": "bench", "devices": 1}
    local = {"id": "l1", "kind": "lock", "start": now, "end": None, "resource": "box-b-device", "task": 6,
             "cmd": "bench"}
    _watch_with(monkeypatch, [remote, local])
    occupancy.watch(p, p.db, now, post)
    occupancy.watch(p, p.db, now + 30, post)
    assert sorted(_occ_episodes(p)) == [("box-a", False, None), ("box-b", False, None)]
    flag.write_text("free")
    occupancy.watch(p, p.db, now + 60, post)   # the read that ends the stream's span
    occupancy.watch(p, p.db, now + 100, post)
    assert [e[1] for e in sorted(_occ_episodes(p))] == [False, False]   # inside the quiet minute after it
    occupancy.watch(p, p.db, now + 125, post)
    assert sorted(_occ_episodes(p)) == [
        ("box-a", True, "the stream's window ended and no job it flagged is known to run there"),
        ("box-b", False, None)]


def test_occupancy_a_hit_overlapping_an_event_window_joins_it(env, monkeypatch):
    """A stream back live inside its quiet tail (a new span whose start moved) is the same event: one alert."""
    from ttp import occupancy
    events = {"box-a:streamer:main:100": 100.0}   # kept before events stored their window: joins by key only
    assert occupancy._event_for(events, "box-a", "streamer", "main", (100.0, 160.0), 110.0) == "box-a:streamer:main:100"
    assert "closed" not in events["box-a:streamer:main:100"]   # never posted again
    assert occupancy._event_for(events, "box-a", "streamer", "main", (150.0, 220.0), 150.0) == "box-a:streamer:main:100"
    assert events["box-a:streamer:main:100"]["to"] == 220.0
    k = occupancy._event_for(events, "box-a", "streamer", "main", (400.0, 460.0), 400.0)
    assert k == "box-a:streamer:main:400" and events[k]["closed"] == 400.0   # a new window: its own alert
    assert occupancy._event_for(events, "box-b", "streamer", "main", (150.0, 220.0), 150.0) == "box-b:streamer:main:150"


class _OccClock:
    """A fake clock and sleep for occupancy.wait_free: each sleep moves time on and runs `on_sleep`."""
    def __init__(self, on_sleep=None, step=30.0):
        self.t, self.naps, self.on_sleep, self.step = time.time(), [], on_sleep, step

    def clock(self):
        return self.t

    def sleep(self, s):
        self.naps.append(s)
        self.t += self.step
        if self.on_sleep:
            self.on_sleep(len(self.naps))


def test_occupancy_wait_free_start_waits_through_the_quiet_window_then_runs(tmp_path, monkeypatch):
    occ = _occ_home(tmp_path, monkeypatch)
    flag = _occ_flag_file(tmp_path, "stream", "live")
    occ.publish(_OccProj("streamer", tmp_path, {"box-a": {"file": str(flag), "max_age_s": 10_000}}))
    fake = _OccClock(lambda n: n == 1 and flag.write_text("free"))
    said = []
    got = occ.wait_free(["box-a"], "test", poll_s=15, say=said.append, sleep=fake.sleep, clock=fake.clock)
    assert got["ok"] and got["move"] == "" and got["why"] == ""
    # live at the first look, then inside the quiet window (60 s after the stream ended), then free
    assert 2 <= len(fake.naps) <= 4 and all(n == 15 for n in fake.naps), fake.naps
    assert said and said[0].startswith("waiting: ") and "looking again every 15 s" in said[0]
    assert got["waited_s"] >= 60


def test_occupancy_wait_free_moves_to_a_free_host(tmp_path, monkeypatch):
    occ = _occ_home(tmp_path, monkeypatch)
    occ.publish(_OccProj("streamer", tmp_path, {"box-a": {"cmd": "exit 0"}, "box-b": {"cmd": "exit 0"},
                                                "box-c": {"cmd": "exit 1"}}))
    fake, said = _OccClock(), []
    got = occ.wait_free(["box-a"], "test", moves=["box-b", ["box-c", "box-c.example"]], say=said.append,
                        sleep=fake.sleep, clock=fake.clock)
    assert got == {**got, "ok": True, "move": "box-c"} and fake.naps == [], got
    assert said == [f"moved to box-c: {got['why']}"] and "box-a" in got["why"]
    # every host held, with a bound: it hands back not ok, having waited
    got = occ.wait_free(["box-a"], "test", moves=["box-b"], max_wait_s=40, sleep=fake.sleep, clock=fake.clock)
    assert got["ok"] is False and got["move"] == "" and got["why"] and fake.naps


def test_occupancy_wait_free_unreadable_source_counts_as_held_and_says_why(tmp_path, monkeypatch):
    occ = _occ_home(tmp_path, monkeypatch)
    occ.publish(_OccProj("streamer", tmp_path, {"box-a": {"cmd": "exit 3"}}))
    fake, said = _OccClock(), []
    got = occ.wait_free(["box-a"], "test", max_wait_s=60, say=said.append, sleep=fake.sleep, clock=fake.clock)
    assert got["ok"] is False
    assert len(said) == 1 and "could not tell" in said[0] and "counted as live" in said[0], said
    # an internal error is held too, never a silent pass
    monkeypatch.setattr(occ, "blocks_any", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    said.clear()
    got = occ.wait_free(["box-a"], "test", max_wait_s=30, say=said.append, sleep=fake.sleep, clock=fake.clock)
    assert got["ok"] is False and "counted as live" in got["why"] and said


def test_occupancy_running_job_is_lowered_then_restored_and_never_killed(env, monkeypatch):
    import os as _os
    from ttp import activity, locks, occupancy, project
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    here = project.hostname()
    occ.publish(_OccProj("streamer", p.root, {here: {"cmd": "exit 0"}}, quiet_s=60))
    now = time.time()
    job = {"id": "j1", "kind": "job", "state": "running", "start": now - 5, "job": "soak", "task": 9,
           "rcfile": "/nonexistent/soak.rc"}
    monkeypatch.setattr(activity, "entries", lambda state: [{"host": here}])
    monkeypatch.setattr(activity, "spans", lambda recs, h: [job])
    calls = []
    monkeypatch.setattr(occupancy, "_job_group", lambda s: 4242)
    monkeypatch.setattr(locks, "start_time", lambda pid: "t-4242")
    real_getpgid, real_kill, real_killpg = _os.getpgid, _os.kill, _os.killpg
    monkeypatch.setattr(_os, "getpgid", lambda pid: pid if pid == 4242 else real_getpgid(pid))
    monkeypatch.setattr(occupancy, "_nice_get", lambda g: 0)
    monkeypatch.setattr(occupancy, "_io_get", lambda g: "2 4")
    monkeypatch.setattr(occupancy, "_autogroup_get", lambda g: 0)
    monkeypatch.setattr(occupancy, "_autogroup_set", lambda g, n: calls.append(("autogroup", g, n)) or "")
    monkeypatch.setattr(occupancy, "_nice_set", lambda g, n: calls.append(("nice", g, n)) or "")
    monkeypatch.setattr(occupancy, "_io_set", lambda g, v: calls.append(("io", g, v)) or "")

    def guard(real):
        def call(pid, sig):
            if abs(pid) == 4242:   # the job's group; the source reader still cleans up its own child
                raise AssertionError(f"a running job was signalled: {pid} {sig}")
            return real(pid, sig)
        return call
    monkeypatch.setattr(_os, "kill", guard(real_kill))
    monkeypatch.setattr(_os, "killpg", guard(real_killpg))
    logged = []
    monkeypatch.setattr(activity, "append", lambda state, rec: logged.append(rec))

    out = occupancy.lower_running(p, p.db, now)
    assert out == {"lowered": ["j1"], "restored": []}
    assert calls == [("autogroup", 4242, 19), ("io", 4242, "3 0")]
    assert logged[-1]["ev"] == "lowered" and logged[-1]["pgid"] == 4242 and logged[-1]["why"]
    assert occupancy.lower_running(p, p.db, now + 1)["lowered"] == []   # once, while it stays live
    # the stream ends; past its quiet window the job gets back what it had
    occ.publish(_OccProj("streamer", p.root, {here: {"cmd": "exit 1"}}, quiet_s=60))
    calls.clear()
    out = occupancy.lower_running(p, p.db, now + 600)
    assert out == {"lowered": [], "restored": ["j1"]}
    assert calls == [("autogroup", 4242, 0), ("io", 4242, "2 4")]
    assert logged[-1]["ev"] == "restored" and logged[-1]["restored"]["io"] == "2 4" and "gone" not in logged[-1]
    assert p.db.kv(occupancy.LOWERED_KEY) == {}


def test_occupancy_lowering_without_autogroup_records_a_nice_it_could_not_raise_back(env, monkeypatch):
    import os as _os
    from ttp import activity, locks, occupancy, project
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    here = project.hostname()
    occ.publish(_OccProj("streamer", p.root, {here: {"cmd": "exit 3"}}))   # unreadable: held
    now = time.time()
    job = {"id": "j2", "kind": "job", "state": "running", "start": now, "job": "bench"}
    monkeypatch.setattr(activity, "entries", lambda state: [{"host": here}])
    monkeypatch.setattr(activity, "spans", lambda recs, h: [job])
    monkeypatch.setattr(occupancy, "_job_group", lambda s: 777)
    monkeypatch.setattr(locks, "start_time", lambda pid: "t")
    real_getpgid = _os.getpgid
    monkeypatch.setattr(_os, "getpgid", lambda pid: pid if pid == 777 else real_getpgid(pid))
    monkeypatch.setattr(occupancy, "_nice_get", lambda g: 0)
    monkeypatch.setattr(occupancy, "_io_get", lambda g: "")
    monkeypatch.setattr(occupancy, "_autogroup_get", lambda g: None)
    monkeypatch.setattr(occupancy, "_io_set", lambda g, v: "")
    monkeypatch.setattr(occupancy, "_nice_set", lambda g, n: "" if n == 19 else "Permission denied")
    logged = []
    monkeypatch.setattr(activity, "append", lambda state, rec: logged.append(rec))
    assert occupancy.lower_running(p, p.db, now)["lowered"] == ["j2"]
    occ.publish(_OccProj("streamer", p.root, {here: {"cmd": "exit 1"}}, quiet_s=0))
    assert occupancy.lower_running(p, p.db, now + 600)["restored"] == ["j2"]
    assert "raising it back needs privilege" in logged[-1]["errors"][0]


def test_occupancy_detach_local_start_waits_inside_the_job(env, tmp_path, monkeypatch):
    import argparse
    from ttp import cli, occupancy, project
    _occ_home(tmp_path, monkeypatch)
    monkeypatch.setattr(occupancy, "blocks_any", lambda targets, step="test", now=None:
                        "box-a: a live realtime stream holds it" if "box-a" in targets else "")
    a = argparse.Namespace(remote=None, lock=["box-a"], step=None)
    assert cli._detach_occupied(a, None) == [project.hostname(), "box-a"]
    assert cli._detach_occupied(argparse.Namespace(remote=None, lock=[], step=None), None) == []
    with pytest.raises(SystemExit) as ex:   # a --remote start on a held box is still not started
        cli._detach_occupied(argparse.Namespace(remote="box-a", lock=[], step=None), None)
    assert ex.value.code == 75
    argv = cli._occupancy_wait_argv(["box-free"], "test", ["sh", "-c", "echo ran"])
    assert argv[:2] == ["/bin/sh", "-c"] and "occupancy --wait --step test box-free" in argv[2]
    r = subprocess.run(argv, env={**os.environ, "TTP_HOME": str(tmp_path / "occ-home")}, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "ran", r


def test_occupancy_detach_step_none_never_waits_and_the_default_still_does(env, tmp_path, monkeypatch, capsys):
    import argparse
    from ttp import cli, occupancy, project
    _occ_home(tmp_path, monkeypatch)
    asked = []
    monkeypatch.setattr(occupancy, "blocks_any", lambda targets, step="test", now=None:
                        asked.append(step) or "box-a: a live realtime stream holds it")
    assert cli._detach_occupied(argparse.Namespace(remote=None, lock=[], step="none"), None) == []
    for remote, rhost, lock in (("box-a", None, []), (None, "box-a", []), (None, None, ["box-a"])):
        with pytest.raises(SystemExit):   # a job that uses another host or box touches it
            cli._detach_occupied(argparse.Namespace(remote=remote, lock=lock, step="none"), rhost)
        assert "--step none cannot go with" in capsys.readouterr().err
    assert asked == []   # a job that touches no box asks no stream
    assert cli._detach_occupied(argparse.Namespace(remote=None, lock=["box-a"], step=None), None) == \
        [project.hostname(), "box-a"] and asked == ["test"]
    with pytest.raises(SystemExit):
        cli.main(["detach", "--help"])
    out = "".join(capsys.readouterr().out.split())   # help wraps at spaces and hyphens
    assert "none:ajobthataddsnohostload" in out and "notwith--remote,--remote-hostor--lock" in out


def test_occupancy_harness_ab_cell_start_waits_for_the_box(monkeypatch, tmp_path):
    from ttp import benchrun, cellns, occupancy

    class Asked(Exception):
        pass
    asked = []
    monkeypatch.setattr(cellns, "backend", lambda: "fake")
    monkeypatch.setattr(occupancy, "wait_here", lambda step, who="", **k: asked.append((step, who)) or (_ for _ in ()).throw(Asked()))
    with pytest.raises(Asked):
        benchrun.run_cell(tmp_path, tmp_path / "out", {"task": "t"}, {"rep": 1, "arm": "a"}, {"arm": "a"}, {}, {})
    assert asked == [("bench", "benchrun")]
    asked.clear()   # a dry run starts nothing on the box: it does not wait
    try:
        benchrun.run_cell(tmp_path, tmp_path / "out", {"task": "t"}, {"rep": 1, "arm": "a"}, {"arm": "a"}, {"dry_run": True}, {})
    except Exception:
        pass
    assert asked == []


def test_occupancy_tbcycle_runner_trial_waits_for_its_gate(monkeypatch, tmp_path):
    from ttp import tbcycle
    order = []
    monkeypatch.setattr(tbcycle, "_locked", lambda path: type("L", (), {"close": lambda self: None})())
    cyc = {"n": 1, "tasks": ["t1"], "last_start": 0}
    seq = iter([(cyc, "t1"), (cyc, None)])
    monkeypatch.setattr(tbcycle, "_next", lambda cfg, home, model, now: next(seq))
    monkeypatch.setattr(tbcycle, "settings", lambda cfg: {"cycle_h": 0})
    monkeypatch.setattr(tbcycle, "folder", lambda: tmp_path)
    import ttp.modelbench as mb
    monkeypatch.setattr(mb, "home", lambda: tmp_path)
    tbcycle.runner({}, "m", {}, trial=lambda m, t, root: order.append("trial") or {},
                   gate=lambda: order.append("gate"), sleep=lambda s: None)
    assert order[:2] == ["gate", "trial"], order


def _occ_two_aliases(env, monkeypatch, alerts=None):
    """A live, open stream on box-a, registered under box-a and box-a-host (one box: both name this host in
    the machine list) with router-a as its front; the watching project runs on this same host."""
    import json
    from ttp import project
    p = make(env)
    occ = _occ_home(env["home"], monkeypatch)
    here = project.hostname()
    (project.HOME_DIR / "machines.json").write_text(json.dumps({"machines": {
        "box-a": {"tags": [], "hostname": here}, "box-a-host": {"tags": [], "hostname": here}}}))
    src = {"cmd": "echo stream live; exit 0", "fronts": ["router-a"]}
    occ.publish(_OccProj("streamer", p.root, {"box-a": src, "box-a-host": src}))
    if alerts is not None:
        cfg = p.config()
        cfg.setdefault("occupancy", {})["alerts"] = alerts
        monkeypatch.setattr(type(p), "config", lambda self: cfg)
    return p, occ, here


def test_occupancy_replay_host_processes_on_a_held_box_are_never_flagged(env, monkeypatch):
    """Replay of a false-alert burst: a live stream on box-a, whose host also runs this
    project. Bench drivers and lock holders for another box's work, a waiter with no resource, an ssh log read
    on another box and on box-a itself, and a span started 2 days earlier all run on box-a's host. No broker job
    is on box-a: nothing is flagged and nothing is posted."""
    from ttp import occupancy
    p, occ, here = _occ_two_aliases(env, monkeypatch)
    now = time.time()
    occupancy.watch(p, p.db, now, None)   # the stream goes live now and stays open
    t = now + 5
    host = {"start": t, "end": None, "host": here, "task": 4}
    spans = [{**host, "id": "drv", "kind": "job", "cmd": "ssh box-b bench"},                  # driver for box-b
             {**host, "id": "lockb", "kind": "lock", "resource": "box-b-device", "cmd": "bench"},
             {**host, "id": "lockw", "kind": "job", "cmd": "ttp lock --wait box-b -- run"},  # a waiter
             {**host, "id": "tailb", "kind": "job", "machine": "box-b", "state": "unknown", "cmd": "tail log"},
             {**host, "id": "taila", "kind": "job", "machine": "box-a", "state": "unknown", "cmd": "tail log"},
             {**host, "id": "old", "kind": "lock", "start": now - 2 * 86400, "resource": "box-a-device",
              "cmd": "bench"}]
    _watch_with(monkeypatch, spans)
    posts = []
    out = occupancy.watch(p, p.db, t, lambda key, text, sev="high": posts.append((key, text, sev)))
    assert out["flags"] == [] and posts == [], (out["flags"], posts)


def test_occupancy_a_broker_job_on_a_held_box_gives_one_alert_whatever_its_aliases(env, monkeypatch):
    """A device job on the held box that starts inside the live window gives exactly one alert, even with the
    box registered under two aliases: a device lock, a remote job that declared devices, and a reset step on
    the box's host each count once against the box, and a later pass posts nothing more."""
    from ttp import occupancy
    p, occ, here = _occ_two_aliases(env, monkeypatch)
    now = time.time()
    occupancy.watch(p, p.db, now, None)
    t = now + 5
    host = {"start": t, "end": None, "host": here, "task": 6}
    spans = [{**host, "id": "dev", "kind": "lock", "resource": "box-a-device", "cmd": "bench"},
             {**host, "id": "rem", "kind": "job", "machine": "box-a", "state": "unknown", "devices": 1,
              "cmd": "render"},
             {**host, "id": "rst", "kind": "job", "step": "reset", "cmd": "reset"}]
    _watch_with(monkeypatch, spans)
    posts = []
    post = lambda key, text, sev="high": posts.append((key, text, sev))   # noqa: E731
    out = occupancy.watch(p, p.db, t, post)
    assert sorted(f["key"].split(":")[0] for f in out["flags"]) == ["dev", "rem", "rst"], out["flags"]
    assert len(posts) == 1 and posts[0][2] == "high" and "3 job(s)" in posts[0][1], posts
    assert occupancy.watch(p, p.db, t + 30, post)["flags"] == [] and len(posts) == 1


def test_occupancy_alert_for_an_open_stream_never_says_quiet_until(env, monkeypatch):
    """A stream still live has no known end: its alert says live since <time> and that the box clears about
    1 minute after the stream ends; once the stream ended, the quiet-until time is given."""
    from ttp import occupancy
    p, occ, here = _occ_two_aliases(env, monkeypatch)
    now = time.time()
    occupancy.watch(p, p.db, now, None)
    _watch_with(monkeypatch, [{"id": "d1", "kind": "lock", "start": now + 5, "end": None, "host": here,
                               "resource": "box-a-device", "task": 2, "cmd": "bench"}])
    posts = []
    occupancy.watch(p, p.db, now + 5, lambda key, text, sev="high": posts.append(text))
    assert len(posts) == 1 and "quiet until" not in posts[0], posts
    assert f"live since {occupancy._clock(now)}" in posts[0] and "about 1 minute after it ends" in posts[0], posts
    closed = {"open": False, "live_from": now, "quiet_to": now + 60, "quiet_s": 60}
    assert f"quiet until {occupancy._clock(now + 60)}" in occupancy._window_words([closed])


def test_occupancy_alerts_switch_off_keeps_flags_and_posts_nothing(env, monkeypatch):
    """`occupancy.alerts` off: the job is still flagged and kept for status and the daily review, but no alert
    is posted and no event is opened; the pre-step check still refuses. Back on, a new job in the window posts."""
    from ttp import coordinator, occupancy
    assert occupancy.settings({})["alerts"] is True
    assert occupancy.settings({"occupancy": {"alerts": "off"}})["alerts"] is False
    assert coordinator.settable("occupancy.alerts")("false") is False
    p, occ, here = _occ_two_aliases(env, monkeypatch, alerts=False)
    now = time.time()
    occupancy.watch(p, p.db, now, None)
    mk = lambda i, t: {"id": f"d{i}", "kind": "lock", "start": t, "end": None, "host": here,   # noqa: E731
                       "resource": "box-a-device", "task": i, "cmd": "bench"}
    _watch_with(monkeypatch, [mk(1, now + 5)])
    posts = []
    post = lambda key, text, sev="high": posts.append(key)   # noqa: E731
    out = occupancy.watch(p, p.db, now + 5, post)
    assert len(out["flags"]) == 1 and posts == [] and p.db.kv(occupancy.EVENTS_KEY, {}) == {}
    occupancy.WHERE = p
    try:
        assert any("alerts paused" in line for line in occupancy.status_lines(p.db, now + 6))
    finally:
        occupancy.WHERE = None
    # precheck_unchanged: the pre-step check holds the box either way
    assert occupancy.blocks("box-a", "test", now + 6) and occupancy.blocks("box-a-host", "bench", now + 6)
    p.config()["occupancy"]["alerts"] = True
    _watch_with(monkeypatch, [mk(1, now + 5), mk(2, now + 7)])
    assert len(occupancy.watch(p, p.db, now + 7, post)["flags"]) == 1 and len(posts) == 1


def test_occupancy_remote_detach_records_its_declared_device_use(env):
    """`ttp detach --remote` records its step and declared devices, which the watchdog reads (a bare remote
    command is a process on that host, not device use)."""
    import pathlib
    from ttp import cli
    src = (pathlib.Path(cli.__file__)).read_text()
    assert '("devices", getattr(a, "devices", None))' in src
    from ttp import occupancy
    assert not occupancy._uses_devices({}, "box-a", {"machine": "box-a"}, "test")
    assert occupancy._uses_devices({}, "box-a", {"machine": "box-a", "devices": 2}, "test")
    assert occupancy._uses_devices({}, "box-a", {"machine": "box-a", "step": "bench"}, "bench")
    assert not occupancy._uses_devices({}, "box-a", {"machine": "box-b", "devices": 2}, "test")
