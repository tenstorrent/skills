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


def _dab_log(p, cmds=("tt-smi -r 0",), refused=()):
    """A run folder whose tool log (the fake provider's `_shell` lines) holds `cmds`, by default a device reset, and
    then a Claude Code Bash call for each command in `refused` that a hook denied."""
    import json
    d = p.root / "runlogs" / str(len(list((p.root / "runlogs").glob("*"))) if (p.root / "runlogs").exists() else 0)
    d.mkdir(parents=True)
    (d / "output.jsonl").write_text("".join(json.dumps({"_shell": c}) + "\n" for c in cmds))
    with (d / "output.jsonl").open("a") as f:
        for i, c in enumerate(refused):
            f.write(json.dumps({"message": {"content": [{"type": "tool_use", "id": f"r{i}", "name": "Bash",
                                                         "input": {"command": c}}]}}) + "\n")
            f.write(json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": f"r{i}",
                                                         "is_error": True, "content": "PreToolUse:Bash hook error: no"}]}}) + "\n")
    return str(d)


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


def test_device_ab_excludes_and_counts_pinned_deep_plan_review_tasks_and_ends(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p, max_tasks=4, days=1)
    deviceab.start(p.db, deviceab.settings(cfg), 1000.0)
    cases = {"pin": dict(model="opus"), "deep": dict(tier="deep"), "plan": dict(kind="plan"),
             "review": dict(kind="review")}
    for why, kw in cases.items():
        kind = kw.pop("kind", "work")
        tid = p.db.add_task(f"t {why}", "Run it.", kind=kind, tier=kw.pop("tier", "standard"), origin="coordinator",
                            priority=3, labels=["needs_device"], **kw)
        assert deviceab.assign(p.db, cfg, p.name, p.db.task(tid), "claude", now=2000.0) is None
        assert f"device_ab_excluded:{why}" in p.db.task(tid)["labels"], why
    for pin in (dict(provider="claude"), dict(effort="max")):
        tid = p.db.add_task("t pin", "Run it.", kind="work", tier="standard", origin="coordinator", priority=3,
                            labels=["needs_device"], **pin)
        assert deviceab.assign(p.db, cfg, p.name, p.db.task(tid), "claude", now=2000.0) is None
    ud = _dab_task(p, labels=("needs_device", "user_deep"))
    assert deviceab.assign(p.db, cfg, p.name, p.db.task(ud), "claude", now=2000.0) is None
    # A tier-up retry's first run keeps its arm and runs on the control arm: never excluded.
    rt = _dab_task(p)
    got = deviceab.assign(p.db, cfg, p.name, p.db.task(rt), "claude", picked={"by": "retry"}, now=2000.0)
    assert got[deviceab.KEY]["ran"] == "opus" and got[deviceab.KEY]["retry"] and got["model"] == "opus"
    # max_tasks: four enrol (the retry included), the fifth is excluded as full; after `days` as ended.
    for _ in range(3):
        assert deviceab.assign(p.db, cfg, p.name, p.db.task(_dab_task(p)), "claude", now=2000.0)
    full = _dab_task(p)
    assert deviceab.assign(p.db, cfg, p.name, p.db.task(full), "claude", now=2000.0) is None
    assert "device_ab_excluded:full" in p.db.task(full)["labels"]
    late = _dab_task(p)
    assert deviceab.assign(p.db, _dab_cfg(p, days=1), p.name, p.db.task(late), "claude", now=1000.0 + 2 * 86400) is None
    r = deviceab.report(p.db.q, lambda k: p.db.kv(k, ""), cfg)
    assert r["excluded"] == {"pin": 3, "deep": 2, "plan": 1, "review": 1, "full": 1, "ended": 1}
    assert r["excluded_total"] == 9 and r["eligible"] == 4 and sum(r["flipped"].values()) == 4
    assert "eligible: 4 (flipped: " in deviceab.report_text(r)


def test_device_ab_flips_urgent_device_tasks(env):
    from ttp.util import task_labels
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    deviceab.start(p.db, deviceab.settings(cfg), 1000.0)
    arms = set()
    for _ in range(12):
        tid = p.db.add_task("Fix the box's hang", "urgent: the user asked today.\nRun it.", kind="work",
                            tier="standard", origin="coordinator", priority=1, labels=["needs_device"])
        got = deviceab.assign(p.db, cfg, p.name, p.db.task(tid), "claude", now=2000.0)
        assert got and not any(lb.startswith(deviceab.EXCLUDED) for lb in task_labels(p.db.task(tid)))
        arms.add(got[deviceab.KEY]["arm"])
    assert arms == {"sonnet", "opus"}


def test_device_ab_excludes_live_outage_work(env, monkeypatch):
    from ttp import deviceab, machine_ledger
    p = make(env)
    cfg = _dab_cfg(p)
    deviceab.start(p.db, deviceab.settings(cfg), 1000.0)
    # 1) an `outage:` spec line; 2) an outage task's label.
    a = p.db.add_task("Bring the box back", "outage: box1 lost its chips\nRun it.", kind="work", tier="standard",
                      origin="coordinator", priority=1, labels=["needs_device"])
    b = _dab_task(p, labels=("needs_device", "outage:box1:down@1000"))
    # 3) a resource machine with an open outage or machine condition when the task is first seen.
    monkeypatch.setattr(machine_ledger, "outages", lambda now=None, doc=None: {"box2": {"why": "repair"}})
    monkeypatch.setattr(machine_ledger, "conditions", lambda: {"box3": {"condition": "unreachable"}})
    c = _dab_task(p, labels=("needs_device", "resource:box2"))
    d = _dab_task(p, labels=("needs_device", "resource:box3"))
    # 4) a high outage event naming the task.
    e = _dab_task(p)
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,fingerprint,status) VALUES(?,?,?,?,?,?,?)",
           (p.db.task(e)["created"], "coordinator", "alert", "high", f"box1 is down; task #{e} restores it",
            "outage:box1", "queued"))
    for tid in (a, b, c, d, e):
        assert deviceab.assign(p.db, cfg, p.name, p.db.task(tid), "claude", now=2000.0) is None
        assert "device_ab_excluded:outage" in p.db.task(tid)["labels"], tid
    # A normal event naming a task, or a spec only mentioning an outage, is not outage work.
    f = p.db.add_task("Soak the box", "Run it; the last outage is over.", kind="work", tier="standard",
                      origin="coordinator", priority=3, labels=["needs_device"])
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,fingerprint,status) VALUES(?,?,?,?,?,?,?)",
           (p.db.task(f)["created"], "watcher:box", "obs", "normal", f"outage over; #{f} may run", "outage:box1",
            "queued"))
    assert deviceab.assign(p.db, cfg, p.name, p.db.task(f), "claude", now=2000.0)
    assert deviceab.report(p.db.q, lambda k: p.db.kv(k, ""), cfg)["excluded"] == {"outage": 5}


def test_device_ab_test_arm_is_sonnet_for_sorted_default_arms_or_the_named_one(env):
    from ttp import deviceab
    arms = {"opus": {"model": "opus", "effort": "high"}, "sonnet": {"model": "sonnet", "effort": "high"}}
    s = deviceab.settings({"route": {"device_ab": {"projects": ["x"], "arms": arms}}})
    assert (s["test"], s["control"]) == ("sonnet", "opus")
    s2 = deviceab.settings({"route": {"device_ab": {"projects": ["x"], "arms": arms, "test": "opus"}}})
    assert (s2["test"], s2["control"]) == ("opus", "sonnet")
    other = {"a": {"model": "m1"}, "b": {"model": "m2"}}
    assert deviceab.settings({"route": {"device_ab": {"projects": ["x"], "arms": other}}})["test"] == "a"


def test_device_ab_start_re_arms_the_window_and_lifts_a_stop(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p, days=1)
    s = deviceab.settings(cfg)
    assert deviceab.start(p.db, s, 1000.0)["ts"] == 1000.0
    # The same setting keeps its start; a deploy that only adds the defaults does not re-arm it.
    assert deviceab.start(p.db, deviceab.settings(cfg), 5000.0)["ts"] == 1000.0
    p.db.set_kv(deviceab.STOP_KV, '{"ts": 1500, "arm": "sonnet", "task": 1}')
    late = _dab_task(p)
    assert deviceab.assign(p.db, cfg, p.name, p.db.task(late), "claude", now=1000.0 + 2 * 86400) is None
    # A new `start` (ISO with offset, or epoch) re-arms: its time is the window's start and the stop is lifted.
    re = _dab_cfg(p, days=1, start="2026-11-15T10:00:00-08:00")
    s2 = deviceab.settings(re)
    assert s2["start"] == 1794765600.0 and s2["sig"] != s["sig"]
    got = deviceab.start(p.db, s2, 1794766000.0)
    assert got["ts"] == 1794765600.0 and deviceab.stopped(p.db) == {}
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (1794765700.0, late))
    fresh = p.db.task(_dab_task(p))
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (1794765700.0, fresh["id"]))
    assert deviceab.assign(p.db, re, p.name, p.db.task(fresh["id"]), "claude", now=1794766000.0)
    assert deviceab.start_ts(1794765600) == 1794765600.0 and deviceab.start_ts("soon") is None


def test_device_ab_share_alarm_fires_when_the_test_arm_gets_under_half_its_share(env):
    import json
    from ttp import deviceab, runclass
    p = make(env)
    cfg = _dab_cfg(p)
    now = 5000.0
    deviceab.start(p.db, deviceab.settings(cfg), 1000.0)
    for i in range(12):   # an exclusion reason the A/B does not keep out by design: counts against the share
        p.db.add_task(f"t{i}", "Run it.", kind="work", tier="standard", origin="coordinator", priority=3,
                      labels=["needs_device", "device_ab_excluded:something_new" if i < 10 else "device_ab:opus"])
    got = [a for a in runclass.share_alarms(p.db, cfg, now) if a["route"] == "device_ab"]
    assert got and got[0]["n"] == 12 and "something_new 10" in got[0]["text"]
    # Pins and outages are kept out by design: no alarm on a normal fill.
    p.db.x("UPDATE tasks SET labels=? WHERE labels LIKE ?", (json.dumps(["needs_device", "device_ab_excluded:pin"]),
                                                            "%something_new%"))
    for i in range(10):
        p.db.add_task(f"u{i}", "Run it.", kind="work", tier="standard", origin="coordinator", priority=3,
                      labels=["needs_device", f"device_ab:{'sonnet' if i % 2 else 'opus'}"])
    assert not [a for a in runclass.share_alarms(p.db, cfg, now) if a["route"] == "device_ab"]


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
        p.db.x("INSERT INTO runs(task, role, provider, model, effort, started, ended, status, cost_usd, note, dir) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (tid, "worker", "claude", arm, "high", 2000.0, 2600.0, "done", 1.5,
                                                json.dumps({deviceab.KEY: {"arm": arm, "ran": arm, "kind": "work"}}),
                                                _dab_log(p)))
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
           (2900.0, "watcher:box", "observation", "high", "box1-device: chip reset after a hang", "queued"))
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


def _dab_sonnet_run(p, cfg, labels, started=2000.0, ended=2600.0, cmds=("tt-smi -r 0",), refused=()):
    """A finished Sonnet-arm run of a fresh task with `labels` whose tool log holds `cmds` (None: no log), a guard
    having refused `refused`;
    returns (task id, run id)."""
    import json
    from ttp import deviceab
    s = deviceab.settings(cfg)
    deviceab.start(p.db, s, 1000.0)
    while True:
        tid = _dab_task(p, labels=labels)
        if deviceab.arm_of(s, p.name, tid) == "sonnet":
            break
    note = deviceab.assign(p.db, cfg, p.name, p.db.task(tid), "claude", now=started)
    rid = p.db.x("INSERT INTO runs(task, role, provider, model, effort, started, ended, status, cost_usd, note, dir) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (tid, "worker", "claude", "sonnet", "high", started, ended, "done",
                                                  1.0, json.dumps({deviceab.KEY: note[deviceab.KEY]}),
                                                  None if cmds is None else _dab_log(p, cmds, refused)))
    return tid, p.db.q("SELECT id FROM runs WHERE task=?", (tid,))[0]["id"]


def _dab_event(p, kind, text, source="coordinator", ts=2300.0, task=None, data=""):
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
           (ts, source, kind, "high", text, data, "queued", task))
    return p.db.q("SELECT max(id) AS i FROM events")[0]["i"]


def test_device_ab_a_coordinator_action_naming_the_host_is_not_a_device_incident(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    tid, _ = _dab_sonnet_run(p, cfg, ["resource:hostx", "needs_device"])   # no device resource: only a host name
    # The false stop: a coordinator action note with "hold" and the host name, in the run's window.
    for kind, source in (("action", "coordinator"), ("audit", "coordinator"), ("course_check", "daemon"),
                         ("flag", "daemon"), ("digest", "daemon"), ("note", "worker"), ("chat", "user"),
                         ("observation", "daemon")):
        _dab_event(p, kind, f"hold-type action Rollout hostx: kernel cache filled after deploy task_add #{tid} cites msg",
                   source=source)
    got = deviceab.tick(p.db, cfg, 2700.0)
    assert got["stop"] is None and not deviceab.stopped(p.db)
    # Even a real incident kind about the host, carrying the run's own task id, does not stop a run that changed
    # no device.
    _dab_event(p, "machine_condition", "hostx: reset after a hang")
    _dab_event(p, "resource_trouble", "hostx: a hang", task=tid)
    assert deviceab.tick(p.db, cfg, 2900.0)["stop"] is None


def test_device_ab_a_real_device_hang_on_the_runs_device_resource_stops_the_test_arm(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    tid, rid = _dab_sonnet_run(p, cfg, ["resource:box1-device", "needs_device"])
    _dab_event(p, "machine_condition", "box2-device: chip hang")   # another task's device: not this run's
    assert deviceab.tick(p.db, cfg, 2700.0)["stop"] is None
    eid = _dab_event(p, "machine_condition", "box1-device: chip hung, reset needed")
    got = deviceab.tick(p.db, cfg, 2800.0)
    assert got["stop"]["task"] == tid and got["stop"]["run"] == rid and got["stop"]["event"] == eid


def test_device_ab_a_device_incident_kind_naming_the_resource_only_in_its_data_stops_the_test_arm(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    tid, _ = _dab_sonnet_run(p, cfg, ["resource:box1-device", "needs_device"])
    _dab_event(p, "device_hang", "chip stuck", data='{"resource": "box1-device"}')
    assert deviceab.tick(p.db, cfg, 2700.0)["stop"]["task"] == tid


def test_device_ab_the_next_tick_lifts_a_false_stop_and_keeps_a_real_one(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    tid, rid = _dab_sonnet_run(p, cfg, ["resource:hostx", "needs_device"])
    false = _dab_event(p, "action", "hold-type action Rollout hostx: kernel cache filled")
    p.db.set_kv(deviceab.STOP_KV, {"ts": 2700.0, "arm": "sonnet", "task": tid, "run": rid, "event": false,
                                   "text": "hold-type action Rollout hostx"})
    got = deviceab.tick(p.db, cfg, 3000.0)
    assert got["lifted"] and "lifted" in got["lifted"] and not deviceab.stopped(p.db)
    ev = p.db.q("SELECT kind, severity, text FROM events WHERE kind='device_ab'")
    assert len(ev) == 1 and ev[0]["severity"] == "low" and "not a device incident" in ev[0]["text"]
    assert deviceab.tick(p.db, cfg, 3100.0)["lifted"] is None   # nothing more to lift


def test_device_ab_a_real_stop_survives_the_next_ticks_recheck(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    _dab_sonnet_run(p, cfg, ["resource:box1-device", "needs_device"])
    real = _dab_event(p, "machine_condition", "box1-device: chip hang")
    assert deviceab.tick(p.db, cfg, 2700.0)["stop"]["event"] == real
    got = deviceab.tick(p.db, cfg, 2800.0)
    assert got["lifted"] is None and deviceab.stopped(p.db)["event"] == real


def test_device_ab_a_deferral_notice_about_another_task_is_not_a_device_incident(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    tid, rid = _dab_sonnet_run(p, cfg, ["resource:box1-device", "needs_device"])
    text = ('task #9999 defers our work to another team: "Review #9998: retest on box1-device once the encoder '
            'fixes the hang after a shape release". Nobody here owns the fix. Queue our own fix now')
    # The live false stop: a self-blocking notice naming another task and the run's device in passing.
    for source in ("watcher:self-blocking", "watcher:course", "coordinator"):
        _dab_event(p, "observation", text, source=source, task=tid)
    _dab_event(p, "self_blocking", text, source="daemon", task=tid)
    # A device watcher's observation that cites another task is not this run's incident either.
    _dab_event(p, "observation", text, source="watcher:device-health")
    assert deviceab.tick(p.db, cfg, 2700.0)["stop"] is None and not deviceab.stopped(p.db)
    # A device watcher naming a hang on the run's own device, citing no other task, still stops the arm.
    real = _dab_event(p, "observation", "box1-device: chip hang, reset needed", source="watcher:device-health")
    assert deviceab.tick(p.db, cfg, 2800.0)["stop"]["event"] == real


def test_device_ab_the_next_tick_lifts_a_recorded_deferral_notice_stop(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    tid, rid = _dab_sonnet_run(p, cfg, ["resource:box1-device", "needs_device"])
    eid = _dab_event(p, "observation",
                     'task #9999 defers our work to another team: "retest on box1-device after the hang fix"',
                     source="watcher:self-blocking", task=tid)
    p.db.set_kv(deviceab.STOP_KV, {"ts": 2700.0, "arm": "sonnet", "task": tid, "run": rid, "event": eid,
                                   "text": "task #9999 defers our work"})
    got = deviceab.tick(p.db, cfg, 3000.0)
    assert got["lifted"] and not deviceab.stopped(p.db)


def test_device_ab_a_fleet_report_counts_only_when_it_names_the_runs_device_as_affected_or_cites_the_run(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    tid, rid = _dab_sonnet_run(p, cfg, ["resource:box1-device", "needs_device"])
    # A fleet report about other boxes that lists the run's device only as paused, checked or as a reporter.
    fleet = ("fleet report: known outage unchanged: hostb, hostc\n"
             "hostb: automatic recovery is not overridden; reset ladder: 25 attempts in 24 h\n"
             "box1-device is paused by the user: no problems are raised for it\n"
             "Fleet has 3 problems (checked hosta, hostb, hostc; paused: hostd, box1-device):\n"
             "- box1-device cannot reach hostb after its reset (shows it offline).\n"
             "- hostc: power cycle failed.")
    _dab_event(p, "observation", fleet, source="watcher:fleet-health")
    assert deviceab.tick(p.db, cfg, 2700.0)["stop"] is None and not deviceab.stopped(p.db)
    e = {"kind": "observation", "source": "watcher:fleet-health", "text": fleet}
    assert not deviceab.incident_event(e, tid, ["box1-device"], rid)
    # The same report naming the run's device as affected stops the arm.
    affected = dict(e, text=fleet + "\n- box1-device: chip hang, broker reset failed.")
    assert deviceab.incident_event(affected, tid, ["box1-device"], rid)
    assert deviceab.incident_event(dict(e, text="hold on box1-device after a hang"), tid, ["box1-device"], rid)
    # Citing the run (its run id or task id) stops it too, even when the device is only listed.
    for cite in (f"run {rid}", f"runs/{rid}", f"#{tid}"):
        assert deviceab.incident_event(dict(e, text=fleet + f"\n- left by {cite}"), tid, ["box1-device"], rid), cite
    # Another run's or task's id does not.
    for cite in (f"run {rid + 1}", f"#{tid + 1}"):
        assert not deviceab.incident_event(dict(e, text=fleet + f"\n- left by {cite}"), tid, ["box1-device"], rid)
    # A longer name that only starts with the device's is not the device.
    assert not deviceab.incident_event(dict(e, text="box1-device2: chip hang"), tid, ["box1-device"], rid)
    real = _dab_event(p, "observation", fleet + "\n- box1-device: chip hang, broker reset failed.",
                      source="watcher:fleet-health")
    assert deviceab.tick(p.db, cfg, 2800.0)["stop"]["event"] == real


def test_device_ab_and_shadow_review_specs_carry_a_read_budget_not_read_them_all(env):
    from ttp import blindreview, deviceab, deviceshadow
    task = {"id": 7, "kind": "work", "title": "Run the soak", "spec": "Run it.", "status": "done"}
    for spec in (deviceab.review_spec(task, "/runs/7"), deviceshadow.review_spec(task, "plan", "/runs/7")):
        assert blindreview.READ_BUDGET in spec
        assert "about 15 tool calls" in spec and "at most 3 cited facts" in spec
        assert "read them all" not in spec and "where you can read them" not in spec


def test_device_ab_a_run_that_only_read_state_or_a_recovery_report_never_stops_the_test_arm(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    tid, rid = _dab_sonnet_run(p, cfg, ["resource:box-a-device", "needs_device"])
    back = "box-a-device serves again; hold cleared after recovery"
    _dab_event(p, "observation", back, source="watcher:fleet-health")
    _dab_event(p, "machine_condition", "box-a-device recovered, back online")
    assert deviceab.tick(p.db, cfg, 2700.0)["stop"] is None   # a recovery report is not an incident
    hang = {"kind": "observation", "source": "watcher:fleet-health", "text": "box-a-device: chip hang, reset needed"}
    assert deviceab.incident_event(hang, tid, deviceab.device_names(p.db.task(tid), cfg), rid)
    # A question, a report task or `read:` resources change no device: nothing is charged to them.
    for upd in ({"kind": "question"}, {"title": "Report box-a-device health"},
                {"labels": ["read:box-a-device", "needs_device"]}):
        task = dict(p.db.task(tid), **upd)
        assert deviceab.device_names(task, cfg) == [] and not deviceab.incident_event(hang, tid, [], rid)


def test_device_ab_trouble_on_another_box_never_stops_a_run_that_changed_box_a(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    tid, _ = _dab_sonnet_run(p, cfg, ["resource:box-a-device", "needs_device"])
    # Trouble on box-b lists the tasks it holds up, this one too: still not this run's incident.
    _dab_event(p, "resource_trouble", "box-b-device: chip hang, 3 failures", task=tid)
    _dab_event(p, "machine_condition", "box-b-device: reset after a hang", data='{"resource": "box-b-device"}')
    assert deviceab.tick(p.db, cfg, 2700.0)["stop"] is None


def test_device_ab_a_state_changing_run_stops_only_within_the_window_and_a_re_arm_skips_the_cleared_stop(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    tid, rid = _dab_sonnet_run(p, cfg, ["resource:box-a-device", "needs_device"])
    _dab_event(p, "machine_condition", "box-a-device: chip hang", ts=2600.0 + deviceab.INCIDENT_AFTER_S + 1)
    assert deviceab.tick(p.db, cfg, 9000.0)["stop"] is None   # past the window after the run
    eid = _dab_event(p, "machine_condition", "box-a-device: chip hang, reset needed")
    got = deviceab.tick(p.db, cfg, 9100.0)["stop"]
    assert (got["task"], got["run"], got["event"]) == (tid, rid, eid)
    # A re-armed start (new setting, same window start) lifts the stop and skips the events at or before it.
    re = _dab_cfg(p, max_tasks=135, start=1000)
    assert deviceab.tick(p.db, re, 9200.0)["stop"] is None and not deviceab.stopped(p.db)
    assert deviceab.start(p.db, deviceab.settings(re), 9300.0)["after"] == 9100.0
    tid2, _ = _dab_sonnet_run(p, re, ["resource:box-a-device", "needs_device"], started=9400.0, ended=9500.0)
    new = _dab_event(p, "machine_condition", "box-a-device: chip hang", ts=9450.0)
    assert deviceab.tick(p.db, re, 9600.0)["stop"]["event"] == new


def test_device_ab_a_test_arm_run_that_changed_no_host_state_is_never_charged(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    # Modeled on a run that only read state: reads, a read under the device lock, a project daemon restart and a
    # reset a guard refused.
    tid, rid = _dab_sonnet_run(p, cfg, ["resource:box-a-device", "needs_device"],
                               cmds=("tt-smi -s", "ttp machines activity", "ttp lock box-a-device -- tt-smi -ls",
                                     "grep reset /var/log/syslog", "ipmitool power status", "git reset --hard",
                                     "ttp restart"), refused=("tt-smi -r 0",))
    _dab_event(p, "machine_condition", "box-a-device: chip hang, reset needed")
    assert deviceab.tick(p.db, cfg, 2700.0)["stop"] is None and not deviceab.stopped(p.db)
    assert not deviceab.changed_state(p.db.q("SELECT dir FROM runs WHERE id=?", (rid,))[0]["dir"])
    # No readable log: not charged either.
    _dab_sonnet_run(p, cfg, ["resource:box-a-device", "needs_device"], cmds=None)
    assert deviceab.tick(p.db, cfg, 2800.0)["stop"] is None
    assert not deviceab.changed_state(None) and not deviceab.changed_state(str(p.root / "nowhere"))
    # The run's own reset, reboot or service change stops the arm.
    for cmd in ("tt-smi -r 0", "ssh box-a sudo reboot", "sudo systemctl restart the-broker",
                "ttp machines change add box-a 'x' --undo true"):
        assert deviceab.changed_state(_dab_log(p, ("tt-smi -s", cmd))), cmd
    tid2, rid2 = _dab_sonnet_run(p, cfg, ["resource:box-a-device", "needs_device"], cmds=("tt-smi -r 0",))
    got = deviceab.tick(p.db, cfg, 2900.0)["stop"]
    assert (got["task"], got["run"]) == (tid2, rid2)


def test_device_ab_an_incident_in_another_tasks_recorded_machine_change_is_not_charged(env):
    from ttp import deviceab, machine_ledger, machines
    p = make(env)
    cfg = _dab_cfg(p)
    machines.add("box-a")
    tid, rid = _dab_sonnet_run(p, cfg, ["resource:box-a-device", "needs_device"])
    # Modeled on a planned reboot of the machine another project's task recorded before it ran.
    machine_ledger.add_power_cycle("box-a", "5m", by="other-project", task=tid, now=2000.0)   # at 2300
    _dab_event(p, "machine_condition", "box-a-device: chip hang after the machine went down", ts=2350.0)
    assert deviceab.tick(p.db, cfg, 2700.0)["stop"] is None
    # Outside that window, the run's own reset is still charged.
    real = _dab_event(p, "machine_condition", "box-a-device: chip hang, reset needed", ts=2950.0)
    assert deviceab.tick(p.db, cfg, 3000.0)["stop"]["event"] == real
    # A change this project recorded for the run's own task excuses nothing; one for another of its tasks does.
    entry = machine_ledger.add_change("box-a", "deploy the broker", "undo it", by=cfg.get("name"), task=tid,
                                      expires="1h", now=4000.0)
    assert deviceab.others_change(cfg, ["box-a-device"], tid, 4100.0) is None
    assert deviceab.others_change(cfg, ["box-a-device"], tid + 1, 4100.0)["id"] == entry["id"]
    assert deviceab.others_change(cfg, ["box-b-device"], tid + 1, 4100.0) is None


def test_device_ab_the_next_tick_lifts_a_stop_the_old_rule_charged_to_a_run_that_changed_nothing(env):
    from ttp import deviceab, machine_ledger, machines
    p = make(env)
    cfg = _dab_cfg(p)
    tid, rid = _dab_sonnet_run(p, cfg, ["resource:box-a-device", "needs_device"], cmds=("tt-smi -s",))
    eid = _dab_event(p, "machine_condition", "box-a-device: chip hang, reset needed")
    p.db.set_kv(deviceab.STOP_KV, {"ts": 2700.0, "arm": "sonnet", "task": tid, "run": rid, "event": eid,
                                   "text": "box-a-device: chip hang"})
    got = deviceab.tick(p.db, cfg, 3000.0)
    assert "no command that changed a host's state" in got["lifted"] and got["stop"] is None
    assert not deviceab.stopped(p.db)
    # A stop inside another task's planned power cycle is lifted too.
    machines.add("box-a")
    tid2, rid2 = _dab_sonnet_run(p, cfg, ["resource:box-a-device", "needs_device"], started=5000.0, ended=5600.0)
    machine_ledger.add_power_cycle("box-a", "5m", by="other-project", now=5000.0)
    eid2 = _dab_event(p, "machine_condition", "box-a-device: chip hang", ts=5300.0)
    p.db.set_kv(deviceab.STOP_KV, {"ts": 5700.0, "arm": "sonnet", "task": tid2, "run": rid2, "event": eid2,
                                   "text": "box-a-device: chip hang"})
    got = deviceab.tick(p.db, cfg, 5800.0)
    assert "another task's recorded change on box-a" in got["lifted"] and not deviceab.stopped(p.db)


def test_device_ab_a_re_arm_ignores_events_from_before_it_but_charges_a_run_started_earlier_for_a_later_one(env):
    from ttp import deviceab
    p = make(env)
    cfg = _dab_cfg(p)
    tid, rid = _dab_sonnet_run(p, cfg, ["resource:box-a-device", "needs_device"])
    old = _dab_event(p, "machine_condition", "box-a-device: chip hang, reset needed", ts=2650.0)
    # A new setting with the same window start re-arms it: the earlier event and a stop recorded on it are dropped.
    re = _dab_cfg(p, max_tasks=135, start=1000)
    assert deviceab.tick(p.db, re, 2700.0)["stop"] is None
    p.db.set_kv(deviceab.STOP_KV, {"ts": 2710.0, "arm": "sonnet", "task": tid, "run": rid, "event": old,
                                   "text": "box-a-device: chip hang"})
    assert "before the A/B was re-armed" in deviceab.tick(p.db, re, 2750.0)["lifted"]
    # The same run (started before the re-arm, its own reset in its log) is charged for a later event.
    new = _dab_event(p, "machine_condition", "box-a-device: chip hang, reset needed", ts=2800.0)
    got = deviceab.tick(p.db, re, 2850.0)["stop"]
    assert (got["run"], got["event"]) == (rid, new)
    assert deviceab.tick(p.db, re, 2900.0)["lifted"] is None   # the recheck keeps it
