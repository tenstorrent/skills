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
    assert _json.loads(_json.dumps(project.DEFAULT_CONFIG["occupancy"])) == {"sources": {}, "quiet_s": 60}


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
    r = subprocess.run([sys.executable, str(TTP), "lock", "box-a", "--", "true"], env=e, capture_output=True,
                       text=True)
    assert r.returncode == 75 and "live realtime stream" in r.stderr, r
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
    out = occupancy.watch(p, p.db, now, lambda key, text: posts.append((key, text)))
    assert len(out["flags"]) == 1 and len(posts) == 1
    assert "box-a" in posts[0][1] and "#7" in posts[0][1]
    occupancy.watch(p, p.db, now + 1, lambda key, text: posts.append((key, text)))
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
    post = lambda key, text: posts.append((key, text))   # noqa: E731
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
