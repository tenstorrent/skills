# Device A/B (route.device_ab, deviceab.py) tests of tests/test_tt_project_runtime.py, which runs this file in
# its own namespace (see _include_cases there). Add new device A/B tests here, not there.
from __future__ import annotations


def _dab_cfg(p, **extra):
    cfg = p.config()
    cfg["core_provider"] = "claude"
    cfg.setdefault("route", {})["device_ab"] = {
        "projects": [p.name], "arms": {"sonnet": {"model": "sonnet", "effort": "high"},
                                       "opus": {"model": "opus", "effort": "high"}},
        "share": 0.5, "days": 3, "max_tasks": 60, "seed": 7, **extra}
    return cfg


def _dab_task(p, kind="work", labels=("needs_device", "resource:box1-device"), **kw):
    return p.db.add_task(f"Run the soak on the box {kind} {len(p.db.q('SELECT id FROM tasks'))}", "Run it.",
                         kind=kind, tier="standard", origin="coordinator", priority=3, labels=list(labels), **kw)


def test_device_ab_is_off_by_default_and_a_project_and_account_key(env):
    from ttp import deviceab
    from ttp.project import ACCOUNT_KEYS, EXTRA_KEYS
    p = make(env)
    assert deviceab.settings(p.config()) is None
    assert "device_ab" in ACCOUNT_KEYS["route"] and "device_ab" in EXTRA_KEYS["route"]
    t = p.db.task(_dab_task(p))
    assert deviceab.assign(p.db, {**p.config(), "core_provider": "claude"}, p.name, t, "claude") is None
    # Not two arms, no projects or not an object: off.
    for bad in ({"projects": []}, {"projects": ["x"], "arms": {"a": {"model": "m"}}}, "on", ["x"]):
        assert deviceab.settings({"route": {"device_ab": bad}}) is None, bad
    s = deviceab.settings(_dab_cfg(p))
    assert (s["test"], s["control"], s["share"], s["days"], s["max_tasks"]) == ("sonnet", "opus", 0.5, 3.0, 60)


def test_device_ab_flips_a_stable_arm_before_the_first_run_and_keeps_it_on_every_run(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    s = deviceab.settings(cfg)
    st = deviceab.start(p.db, s, 1000.0)
    assert st["ts"] == 1000.0 and deviceab.start(p.db, s, 5000.0)["ts"] == 1000.0   # recorded once
    # A task created before the start is out of scope (no label, no count).
    p.db.x("UPDATE kv SET value=? WHERE key=?", ('{"ts": 9e12, "sig": "%s"}' % s["sig"], deviceab.START_KV))
    old = p.db.task(_dab_task(p))
    assert deviceab.assign(p.db, cfg, p.name, old, "claude") is None and "device_ab" not in old["labels"]
    p.db.x("UPDATE kv SET value=? WHERE key=?", ('{"ts": 1000.0, "sig": "%s"}' % s["sig"], deviceab.START_KV))
    arms = {}
    for _ in range(12):
        tid = _dab_task(p)
        got = deviceab.assign(p.db, cfg, p.name, p.db.task(tid), "claude", "hard", None, 2000.0)
        arm = deviceab.arm_of(s, p.name, tid)
        assert got["model"] == s["arms"][arm]["model"] and got["effort"] == "high" and got["provider"] == "claude"
        assert got[deviceab.KEY] == {"arm": arm, "ran": arm, "kind": "work"}
        assert f"device_ab:{arm}" in p.db.task(tid)["labels"]   # recorded before its first run
        arms[tid] = arm
    assert set(arms.values()) == {"sonnet", "opus"}
    # Stable: the same hash in every process; a later run keeps the recorded arm.
    for tid, arm in arms.items():
        assert deviceab.arm_of(s, p.name, tid) == arm
        assert deviceab.assign(p.db, cfg, p.name, p.db.task(tid), "claude", now=2100.0)[deviceab.KEY]["arm"] == arm
    # A tier-up retry runs on the control arm (Opus), marked.
    sonnet = next(t for t, a in arms.items() if a == "sonnet")
    got = deviceab.assign(p.db, cfg, p.name, p.db.task(sonnet), "claude", picked={"by": "retry"}, now=2200.0)
    assert got["model"] == "opus" and got[deviceab.KEY] == {"arm": "sonnet", "ran": "opus", "kind": "work",
                                                            "retry": True}
    # Not a device task, another project, the chat provider or a non-Claude core: never in the A/B.
    plain = p.db.task(_dab_task(p, labels=()))
    assert deviceab.assign(p.db, cfg, p.name, plain, "claude", now=2000.0) is None
    t = p.db.task(_dab_task(p))
    assert deviceab.assign(p.db, cfg, "other", t, "claude", now=2000.0) is None
    assert deviceab.assign(p.db, cfg, p.name, t, "chat", now=2000.0) is None
    assert deviceab.assign(p.db, {**cfg, "core_provider": "codex"}, p.name, t, "claude", now=2000.0) is None


def test_device_ab_excludes_and_counts_urgent_pinned_deep_plan_review_and_retry_tasks_and_ends(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p, max_tasks=3, days=1)
    deviceab.start(p.db, deviceab.settings(cfg), 1000.0)
    cases = {"urgent": dict(priority=1), "pin": dict(model="opus"), "deep": dict(tier="deep"),
             "plan": dict(kind="plan"), "review": dict(kind="review")}
    for why, kw in cases.items():
        kind = kw.pop("kind", "work")
        tid = p.db.add_task(f"t {why}", "Run it.", kind=kind, tier=kw.pop("tier", "standard"), origin="coordinator",
                            priority=kw.pop("priority", 3), labels=["needs_device"], **kw)
        assert deviceab.assign(p.db, cfg, p.name, p.db.task(tid), "claude", now=2000.0) is None
        assert f"device_ab_excluded:{why}" in p.db.task(tid)["labels"], why
    ud = _dab_task(p, labels=("needs_device", "user_deep"))
    assert deviceab.assign(p.db, cfg, p.name, p.db.task(ud), "claude", now=2000.0) is None
    rt = _dab_task(p)
    assert deviceab.assign(p.db, cfg, p.name, p.db.task(rt), "claude", picked={"by": "retry"}, now=2000.0) is None
    assert "device_ab_excluded:retry" in p.db.task(rt)["labels"]
    # max_tasks: three enrol, the fourth is excluded as full; after `days` new tasks are excluded as ended.
    for _ in range(3):
        assert deviceab.assign(p.db, cfg, p.name, p.db.task(_dab_task(p)), "claude", now=2000.0)
    full = _dab_task(p)
    assert deviceab.assign(p.db, cfg, p.name, p.db.task(full), "claude", now=2000.0) is None
    assert "device_ab_excluded:full" in p.db.task(full)["labels"]
    late = _dab_task(p)
    assert deviceab.assign(p.db, _dab_cfg(p, days=1), p.name, p.db.task(late), "claude", now=1000.0 + 2 * 86400) is None
    r = deviceab.report(p.db.q, lambda k: p.db.kv(k, ""), cfg)
    assert r["excluded"] == {"urgent": 1, "pin": 1, "deep": 2, "plan": 1, "review": 1, "retry": 1, "full": 1,
                             "ended": 1} and r["excluded_total"] == 9


def test_device_ab_stops_the_test_arm_on_a_device_incident_with_one_high_alert_and_queues_masked_reviews(env):
    import json
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    s = deviceab.settings(cfg)
    deviceab.start(p.db, s, 1000.0)
    tasks = {}
    while len(set(tasks.values())) < 2:
        tid = _dab_task(p, kind="code" if len(tasks) % 2 else "work")
        tasks[tid] = deviceab.assign(p.db, cfg, p.name, p.db.task(tid), "claude", now=2000.0)[deviceab.KEY]["arm"]
    son = next(t for t, a in tasks.items() if a == "sonnet")
    opus = next(t for t, a in tasks.items() if a == "opus")
    for tid, arm in ((son, "sonnet"), (opus, "opus")):
        p.db.x("INSERT INTO runs(task, role, provider, model, effort, started, ended, status, cost_usd, note) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)", (tid, "worker", "claude", arm, "high", 2000.0, 2600.0, "done", 1.5,
                                              json.dumps({deviceab.KEY: {"arm": arm, "ran": arm, "kind": "work"}})))
        p.db.update_task(tid, status="done", result=json.dumps({"summary": "ok"}))
    # An incident on an Opus-arm run's box, or a quiet event, stops nothing.
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
           (2100.0, "watcher:box", "obs", "normal", "box1-device: all chips healthy", "queued"))
    got = deviceab.tick(p.db, cfg, 2700.0)
    assert got["stop"] is None and len(got["reviews"]) == 2
    rv = [p.db.task(i) for i in got["reviews"]]
    for t in rv:   # masked: Opus at high effort, and the spec names no arm or model that ran it
        assert (t["model"], t["effort"], t["kind"]) == ("opus", "high", "question")
        assert "sonnet" not in t["spec"].lower() and "device_ab_scores" in t["spec"]
    assert deviceab.tick(p.db, cfg, 2800.0)["reviews"] == []   # once per task
    # A reset within INCIDENT_AFTER_S of the Sonnet-arm run, naming its resource: the test arm stops, once.
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
           (2900.0, "watcher:box", "obs", "high", "box1-device: chip reset after a hang", "queued"))
    got = deviceab.tick(p.db, cfg, 3000.0)
    assert got["stop"]["task"] == son and got["stop"]["arm"] == "sonnet"
    assert "box1-device" in deviceab.stop_text(got["stop"])
    assert deviceab.tick(p.db, cfg, 3100.0)["stop"] is None   # one alert only
    # The stopped arm's open tasks run on the control arm; new Sonnet picks are excluded.
    assert deviceab.assign(p.db, cfg, p.name, p.db.task(son), "claude", now=3200.0)[deviceab.KEY] == \
        {"arm": "sonnet", "ran": "opus", "kind": p.db.task(son)["kind"], "stopped": True}
    while True:
        tid = _dab_task(p)
        if deviceab.arm_of(s, p.name, tid) == "sonnet":
            break
    assert deviceab.assign(p.db, cfg, p.name, p.db.task(tid), "claude", now=3200.0) is None
    assert "device_ab_excluded:test_arm_stopped" in p.db.task(tid)["labels"]
    # Scores, spend and wall time per arm and kind; a follow-up fix counts on its source task.
    p.db.update_task(rv[0]["id"], status="done",
                     result=json.dumps({"device_ab_scores": {"answered": 4, "supported": 3, "why": "ok"}}))
    p.db.add_task("Fix it", "x", kind="code", origin="coordinator", labels=[f"continues:{son}"])
    r = deviceab.report(p.db.q, lambda k: p.db.kv(k, ""), cfg)
    src = int(rv[0]["title"].rsplit("#", 1)[1])
    arm = tasks[src]
    kind = "device code" if p.db.task(src)["kind"] == "code" else "device work"
    assert r["arms"][arm][kind]["answered"] == 4.0 and r["arms"][arm][kind]["supported"] == 3.0
    sk = "device code" if p.db.task(son)["kind"] == "code" else "device work"
    a = r["arms"]["sonnet"][sk]
    assert a["done"] == 1 and a["incidents"] == 1 and a["fixes"] == 1 and a["usd_per_task"] == 1.5
    assert a["minutes_per_task"] == 10.0
    assert r["stopped"]["task"] == son and "sonnet" in deviceab.report_text(r, p.name)


def test_device_ab_dispatch_runs_the_recorded_arm_model_at_its_effort(env, monkeypatch):
    from ttp import budget as bud
    from ttp import deviceab
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("core_provider", "claude")
    p.set_config("route.device_ab", {"projects": [p.name], "seed": 3})
    d = Daemon(p.base)
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, prompt, provider, tier, cwd, **k: started.append(
        (provider, k.get("note") or {})) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.root), None))
    d.gates["claude"] = bud.Gate("claude", regime="windows", max_parallel=6)
    deviceab.start(p.db, deviceab.settings(d.cfg), time.time() - 60)   # the daemon tick records it
    tid = _dab_task(p)
    d.dispatch()
    prov, note = started[-1]
    arm = deviceab.arm_of(deviceab.settings(d.cfg), p.name, tid)
    assert prov == "claude" and note[deviceab.KEY]["arm"] == arm
    from ttp import runclass
    cheap = runclass.cheap_run(note, "claude")
    assert (cheap["model"], cheap["effort"]) == (arm, "high")   # the default arms: sonnet and opus at high
