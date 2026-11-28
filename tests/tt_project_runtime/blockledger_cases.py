# Blocked-time ledger tests (blockledger.py, `ttp block-log`, `ttp blocked`, the daily review's line) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file. Add new blocked-time ledger tests here.
from __future__ import annotations


def _bl_task(p, title, status, now, priority=3, blocked_ago=None, reason="x", kind="task_blocked"):
    tid = p.db.add_task(title)
    p.db.x("UPDATE tasks SET status=?, priority=?, created=?, updated=? WHERE id=?",
           (status, priority, now - 86400, now - 30, tid))
    if blocked_ago:
        p.db.x("INSERT INTO events(ts,source,kind,text,data,status,task) VALUES(?,?,?,?,?,?,?)",
               (now - blocked_ago, "t", kind, f"#{tid} {title} is blocked: {reason}", None, "handled", tid))
    return tid


def test_block_ledger_rows_name_the_rule_and_class_of_each_blocked_interval(env):
    from ttp import blockledger as bl
    p = make(env)
    now = time.time()
    a = _bl_task(p, "waits on a rule", "blocked", now, blocked_ago=7200, reason="held by R-ab12 until later")
    b = _bl_task(p, "waits on words", "blocked", now, blocked_ago=3600, reason="keep the 60 min hold for the nightly job")
    c = _bl_task(p, "in review", "review", now, blocked_ago=1800, kind="task_review", reason="review")
    rs = {r["task"]: r for r in bl.rows(p.db, now - 86400, now)}
    assert (rs[a]["rule"], rs[a]["minutes"], rs[a]["safety"]) == ("R-ab12", 120.0, "rule")
    assert rs[b]["rule"].startswith("keep the min hold") and rs[b]["safety"] == "convenience"
    assert rs[c]["safety"] == "review or push" and rs[c]["kind"] == "review"
    assert [r["minutes"] for r in bl.rows(p.db, now - 86400, now)] == sorted(
        [r["minutes"] for r in bl.rows(p.db, now - 86400, now)], reverse=True)


def test_block_ledger_counts_a_hold_that_began_before_the_window_and_ignores_the_title(env):
    from ttp import blockledger as bl
    p = make(env)
    now = time.time()
    old = _bl_task(p, "kernels", "blocked", now, blocked_ago=30 * 3600, reason="keep the 60 min hold for the nightly job")
    titled = _bl_task(p, "review the post-processing merge", "blocked", now, blocked_ago=3600, reason="keep the 60 min hold")
    rs = {r["task"]: r for r in bl.rows(p.db, now - 86400, now)}
    assert rs[old]["minutes"] == pytest.approx(24 * 60, abs=1) and rs[old]["start"] == pytest.approx(now - 86400, abs=1)
    assert rs[titled]["safety"] == "convenience"
    p.db.x("UPDATE events SET text=? WHERE task=?", (f"#{titled} review the post-processing merge: keep the 60 min hold", titled))
    plain = {r["task"]: r for r in bl.rows(p.db, now - 86400, now)}[titled]   # the waiting event's form, no 'blocked'
    assert plain["safety"] == "convenience" and plain["rule"].startswith("keep the min hold")


def test_block_ledger_clips_queue_waits_and_log_rows_to_the_window(env):
    from ttp import blockledger as bl, queuewait
    p = make(env)
    now = time.time()
    t = _bl_task(p, "queued long", "queued", now)
    p.db.set_kv(queuewait.KV, {"open": {"k": {"task": t, "queue": "checks", "since": now - 3 * 86400}}})
    bl.append(p.db, "keep 60 min", t, 3 * 86400, "a long loss")
    rs = bl.rows(p.db, now - 3600, now)
    assert {r["source"]: r["minutes"] for r in rs if r["source"] in ("queue", "cli")} == {"queue": 60.0, "cli": 60.0}


def test_block_ledger_counts_a_hold_that_began_before_the_window_and_ended_inside_it(env):
    from ttp import blockledger as bl
    p = make(env)
    now = time.time()
    t = _bl_task(p, "moved on", "queued", now, blocked_ago=5 * 3600, reason="keep the 60 min hold")
    p.db.x("INSERT INTO runs(task,role,status,started,ended) VALUES(?,?,?,?,?)", (t, "worker", "ok", now - 3 * 3600, now - 3 * 3600 + 60))
    rs = [r for r in bl.rows(p.db, now - 4 * 3600, now) if r["task"] == t]
    assert [r["minutes"] for r in rs] == [pytest.approx(60, abs=1)]


def test_block_ledger_refusals_are_one_row_per_run_and_rule(env):
    from ttp import blockledger as bl
    p = make(env)
    now = time.time()
    t = _bl_task(p, "refused often", "queued", now)
    p.db.x("INSERT INTO runs(task,role,status,started,ended) VALUES(?,?,?,?,?)", (t, "worker", "ok", now - 7200, now - 7000))
    d = p.state / "runs" / str(p.db.one("SELECT max(id) AS i FROM runs")["i"])
    d.mkdir(parents=True)
    (d / "refusals.jsonl").write_text("".join(json.dumps({"ts": now - 7100 + i, "kind": "full_suite"}) + "\n" for i in range(10)))
    rs = [r for r in bl.rows(p.db, now - 86400, now) if r["source"] == "refusal"]
    assert [(r["rule"], r["n"], r["start"]) for r in rs] == [("full_suite", 10, pytest.approx(now - 7100, abs=1))]


@pytest.mark.parametrize("reason,kind,want", [
    ("waiting on resource:blx-device", "waiting", "held resource"),
    ("check:abc123 failed", "blocked", "check"),
    ("per user msg #12", "blocked", "user message"),
    ("holds under R-1a2b", "blocked", "rule"),
    ("waits for the review", "waiting", "review or push"),
    ("anything", "review", "review or push"),
    ("the user's pause stands", "blocked", "a pause or hold the user set"),
    ("the daily budget is spent", "blocked", "credential, money or a step only the user holds"),
    ("task budget reached before the change landed", "blocked", "convenience"),
    ("the project spend cap is reached", "blocked", "credential, money or a step only the user holds"),
    ("resource:dev-box held, queue too large", "waiting", "held resource"),
    ("task budget reached, paused by the user", "blocked", "a pause or hold the user set"),
    ("a keep constant of 60 minutes", "waiting", "convenience")])
def test_block_ledger_safety_class(reason, kind, want):
    from ttp import blockledger as bl
    assert bl.safety_of(reason, kind) == want


def test_block_ledger_counts_guard_refusals_queue_waits_and_the_block_log(env):
    from ttp import blockledger as bl, queuewait
    p = make(env)
    now = time.time()
    t = _bl_task(p, "refused twice", "queued", now)
    p.db.x("INSERT INTO runs(task,role,status,started,ended) VALUES(?,?,?,?,?)", (t, "worker", "ok", now - 7200, now - 7000))
    rid = p.db.one("SELECT max(id) AS i FROM runs")["i"]
    p.db.x("INSERT INTO events(ts,source,kind,text,data,status,task) VALUES(?,?,?,?,?,?,?)",
           (now - 7010, "t", "task_blocked", "x", None, "handled", t))
    p.db.x("INSERT INTO runs(task,role,status,started,ended) VALUES(?,?,?,?,?)", (t, "worker", "ok", now - 3000, now - 2900))
    d = p.state / "runs" / str(rid)
    d.mkdir(parents=True)
    (d / "refusals.jsonl").write_text(
        json.dumps({"ts": now - 7100, "kind": "full_suite", "command": "pytest"}) + "\nnot json\n")
    (d / "glm_refusals.jsonl").write_text(
        json.dumps({"ts": now - 7050, "rule": "records", "class": "harmful", "n": 2, "why": "x"}) + "\n"
        + json.dumps({"ts": now - 7040, "rule": "redirected", "class": "redirect", "n": 1, "why": "y"}) + "\n")
    (d / "refusals.jsonl").write_text((d / "refusals.jsonl").read_text()
                                      + json.dumps({"ts": now - 7030, "kind": "no_device", "command": "tt-smi"}) + "\n")
    p.db.set_kv(queuewait.KV, {"hist": [{"task": t, "queue": "checks", "since": now - 5000, "end": now - 4400}]})
    bl.append(p.db, "keep 60 min", t, 300, "refused a restart")
    rs = bl.rows(p.db, now - 86400, now)
    by = {r["rule"]: r for r in rs if r["source"] != "task"}
    assert by["full_suite"]["source"] == "refusal" and by["full_suite"]["n"] == 1
    assert by["records"]["n"] == 1 and by["records"]["minutes"] > 60   # until the task next ran
    assert (by["records"]["safety"], by["no_device"]["safety"]) == ("guard", "guard")   # safety guards are no rules to retire
    assert by["full_suite"]["safety"] == by["redirected"]["safety"] == "convenience"
    assert all(r["end"] <= now - 3000 for r in bl.rows(p.db, now - 86400, now - 3000) if r["source"] == "refusal")
    assert by["queue:checks"]["minutes"] == 10.0
    assert (by["keep 60 min"]["minutes"], by["keep 60 min"]["source"]) == (5.0, "cli")
    assert [e["rule"] for e in bl.by_rule(rs, 1)] == [rs[0]["rule"]]


def test_block_ledger_share_counts_only_ordered_work_and_each_task_once(env):
    from ttp import blockledger as bl
    p = make(env)
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    start = time.mktime(time.strptime(day, "%Y-%m-%d"))
    now = max(now, start + 3 * 3600)   # a day old enough to hold the intervals below
    a = _bl_task(p, "ordered", "blocked", now, priority=1, blocked_ago=3600, reason="held by a keep constant")
    b = _bl_task(p, "routine", "blocked", now, blocked_ago=3600, reason="held by a keep constant")
    bl.append(p.db, "keep constant", a, 1800, "overlaps its blocked interval")
    p.db.x("UPDATE tasks SET created=? WHERE id IN (?,?)", (now - 7200, a, b))
    s = bl.window_share(p.db, now - 7200, now)
    assert s["ordered_min"] == pytest.approx(120, abs=1) and s["blocked_min"] == pytest.approx(60, abs=1)
    assert s["share"] == pytest.approx(0.5, abs=0.01)
    assert bl.TARGET == 0.05


def test_block_log_and_blocked_commands_and_the_daily_line(env, capsys, monkeypatch):
    from ttp import blockledger as bl, cli, unblock
    p = make(env)
    monkeypatch.chdir(p.root)
    now = time.time()
    t = _bl_task(p, "ordered", "blocked", now, priority=1, blocked_ago=1200, reason="held by a keep constant")
    cli.cmd_block_log(types.SimpleNamespace(rule="keep constant", task=t, seconds=90.0, why="restart refused"))
    log = (p.state / bl.LOG).read_text().splitlines()
    assert json.loads(log[0])["rule"] == "keep constant" and json.loads(log[0])["seconds"] == 90.0
    line = next(x for x in unblock.lines(p.db, now, {}) if x.startswith("Blocked time: "))
    assert "of ordered work (target 5%); top: " in line and "keep constant" in line
    cli.cmd_blocked(types.SimpleNamespace(days=1.0, json=True, done=None, how=None))
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["target"] == 0.05 and {e["rule"] for e in out["top"]} == {"held by a keep constant", "keep constant"}
    assert {e["safety"] for e in out["top"]} == {"convenience"}
    cli.cmd_blocked(types.SimpleNamespace(days=1.0, json=False, done=None, how=None))
    assert "of ordered work over 1 d (target 5%)" in capsys.readouterr().out


def _bl_wait(p, title, now, ago, probe, reason="x", status="waiting"):
    """A task stuck since `ago` s with its task_waiting event's retry_when probe."""
    tid = _bl_task(p, title, status, now, priority=1)
    p.db.x("INSERT INTO events(ts,source,kind,text,data,status,task) VALUES(?,?,?,?,?,?,?)",
           (now - ago, "t", "task_waiting", f"#{tid} {title} is waiting: {reason}",
            json.dumps({"wait": reason, "probe": probe}), "handled", tid))
    return tid


def test_block_ledger_a_wait_on_the_tasks_own_detached_job_is_work_not_blocked_time(env):
    from ttp import blockledger as bl, queuewait
    p = make(env)
    now = time.time()
    t = _bl_wait(p, "runs its suite", now, 3600, "ttp detach --check /r/runs/9/suite2.rc && ttp detach --check /r/x.rc")
    o = _bl_wait(p, "waits on a box", now, 3600, "ssh box true")
    p.db.x("UPDATE tasks SET created=? WHERE id IN (?,?)", (now - 7200, t, o))
    d = _bl_wait(p, "waits on a landing", now, 3600, "")
    job = "ttp detach --check /r/runs/9/suite2.rc && ttp detach --check /r/x.rc"
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 7200, d))
    p.db.set_kv(queuewait.KV, {"hist": [{"task": t, "queue": "own", "since": now - 3590, "end": now - 600, "probe": job}],
                               "open": {"k": {"task": o, "queue": "own", "since": now - 3590, "probe": "ssh box true"},
                                        "l": {"task": d, "queue": "own", "since": now - 3590, "probe": "landed:#4244"}}})
    rs = [r for r in bl.rows(p.db, now - 7200, now) if r["source"] == "queue"]
    assert {r["task"]: r["rule"] for r in rs} == {t: "own job", o: "queue:own", d: "dependency"}
    assert [e["rule"] for e in bl.top(p.db, now) if e["rule"] in ("own job", "queue:own")] == ["queue:own"]
    share = bl.window_share(p.db, now - 7200, now, rs)
    assert share["blocked_min"] == pytest.approx(60 + 60, abs=1), "the box and the landing waits count"


def test_block_ledger_review_rows_and_clock_gated_windows(env):
    from ttp import blockledger as bl
    p = make(env)
    now = time.time()
    r = _bl_task(p, "in review", "review", now, priority=1, blocked_ago=1800, kind="task_review", reason="held by R-ab12")
    w = _bl_wait(p, "lands at night", now, 3600,
                 'test "$(date +%s)" -ge 1796673600', reason="for landing on the push branch")
    m = _bl_wait(p, "box or deadline", now, 3600, 'ssh box true || test "$(date +%s)" -ge 1796673600', reason="the box")
    p.db.x("UPDATE tasks SET created=? WHERE id IN (?,?,?)", (now - 7200, r, w, m))
    rs = {x["task"]: x for x in bl.rows(p.db, now - 7200, now) if x["source"] == "task"}
    assert (rs[r]["rule"], rs[w]["rule"]) == ("review", "planned window")
    assert rs[m]["rule"] != "planned window", "a real wait with a deadline is no planned window"
    assert "planned window" not in [e["rule"] for e in bl.top(p.db, now)]
    assert bl.window_share(p.db, now - 7200, now)["blocked_min"] == pytest.approx(30 + 60, abs=1)


def test_block_ledger_redirect_refusals_hold_no_minutes_and_count_once(env):
    """A GLM redirect is logged by both glmguard and the hook: one row under the guard's name, its count
    kept, 0 minutes; a refusal that stops the run still holds time until the task next ran."""
    from ttp import blockledger as bl
    p = make(env)
    now = time.time()
    t = _bl_task(p, "redirected", "queued", now)
    p.db.x("INSERT INTO runs(task,role,status,started,ended) VALUES(?,?,?,?,?)", (t, "worker", "ok", now - 7200, now - 7000))
    p.db.x("INSERT INTO events(ts,source,kind,text,data,status,task) VALUES(?,?,?,?,?,?,?)",
           (now - 7010, "t", "task_blocked", "x", None, "handled", t))   # the run handed off blocked
    p.db.x("INSERT INTO runs(task,role,status,started,ended) VALUES(?,?,?,?,?)", (t, "worker", "ok", now - 3000, now - 2900))
    d = p.state / "runs" / str(p.db.one("SELECT min(id) AS i FROM runs WHERE task=?", (t,))["i"])
    d.mkdir(parents=True)
    (d / "glm_refusals.jsonl").write_text("".join(
        json.dumps({"ts": now - 7100 + i, "rule": "scratch", "class": "redirect", "n": 1, "why": "x"}) + "\n" for i in range(2)))
    (d / "refusals.jsonl").write_text("".join(
        json.dumps({"ts": now - 7100 + i, "kind": "worker_role", "guard": "scratch", "class": "redirect"}) + "\n"
        for i in range(2)) + json.dumps({"ts": now - 7050, "kind": "worker_role", "guard": "pytest_n", "class": "harmful"}) + "\n")
    rs = {r["rule"]: r for r in bl.rows(p.db, now - 86400, now) if r["source"] == "refusal"}
    assert set(rs) == {"scratch", "pytest_n"}
    assert (rs["scratch"]["n"], rs["scratch"]["minutes"]) == (2, 0.0)
    assert rs["pytest_n"]["minutes"] > 60
    top = [e["rule"] for e in bl.top(p.db, now, safety=True)]   # worker-role refusals are a safety guard
    assert "pytest_n" in top and "scratch" not in top


def test_the_hook_logs_the_refusing_guards_name(env, monkeypatch):
    """Refusals keep their kind (localfault reads it) and name the guard, which blockledger keys them by."""
    from ttp import hook
    from ttp.hook import REFUSALS_FILE
    p = make(env)
    run_dir = p.base / "guard-names"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.setenv("TTP_WAITS_IDLE", "1")
    monkeypatch.setenv("TTP_TEST_SLOTS", "4")
    for cmd in ("sleep 600", "python3 -m pytest -n 16 tests/a.py"):
        out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(p.base)})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd
    rows = [json.loads(x) for x in (run_dir / REFUSALS_FILE).read_text().splitlines()]
    assert [(r["kind"], r["guard"]) for r in rows] == [("worker_role", "sleep_wait"), ("worker_role", "pytest_n")]


def test_block_ledger_a_wait_on_another_tasks_landing_is_a_dependency_and_empty_rows_are_not_listed(env):
    from ttp import blockledger as bl
    p = make(env)
    now = time.time()
    a = _bl_wait(p, "after the fix", now, 1800, "ttp landed --task 4242", reason="for landing of task on relay")
    b = _bl_task(p, "deferred", "blocked", now, blocked_ago=1800, reason="start_when landed:#4243")
    _bl_task(p, "just now", "blocked", now, blocked_ago=1, reason="keep the 60 min hold")
    rs = {r["task"]: r for r in bl.rows(p.db, now - 7200, now) if r["source"] == "task"}
    assert (rs[a]["rule"], rs[b]["rule"]) == ("dependency", "dependency")
    assert bl.top(p.db, now) == []
    assert not bl.listed(bl.rows(p.db, now - 7200, now))


DATE_GATE = 'test "$(date +%s)" -ge "$(date -d \'2026-11-29 07:00 PST\' +%s)" || exit 1'


def test_block_ledger_a_fixed_date_clock_gate_is_a_planned_window_and_raises_no_own_wait(env):
    """A probe that only compares now with a fixed date (`date -d '<text>' +%s`, `test` or `[ ]`, an optional
    `|| exit 1`) is a planned window in the ledger, the unblock sweep and the queue-wait scan; one that also
    runs another command stays a real wait on the task's own probe."""
    from ttp import blockledger as bl, queuewait, unblock, unstarted
    p = make(env)
    now = time.time()
    other = 'ttp landed --check && ' + DATE_GATE
    for probe, ok in ((DATE_GATE, True), ('[ $(date +%s) -ge 1796673600 ]', True), ('test "$(date +%s)" -ge "1796673600"', True),
                      (other, False), ('test "$(date +%s)" -ge "$(date -d "$(cat f)" +%s)"', False),
                      (DATE_GATE + '; curl x', False)):
        assert bool(unstarted.TIME_ONLY.match(probe)) is ok, probe
        assert (unblock.classify_wait({"retry_when": probe}) == ("self", "planned window")) is ok, probe
    a, b = _bl_task(p, "starts at seven", "queued", now), _bl_task(p, "runs a check", "queued", now)
    p.db.set_kv(queuewait.KV, {"open": {"a": {"task": a, "queue": "own", "since": now - 3600, "probe": DATE_GATE},
                                        "b": {"task": b, "queue": "own", "since": now - 3600, "probe": other}}})
    rs = {r["task"]: r["rule"] for r in bl.rows(p.db, now - 7200, now) if r["source"] == "queue"}
    assert rs == {a: "planned window", b: "queue:own"}
    for probe, raised in ((DATE_GATE, False), (other, True)):
        t = p.db.add_task(probe[:20], "urgent: user asked", kind="code")
        p.db.update_task(t, not_before=now + 300, blocked_reason="waiting",
                         result=json.dumps({"status": "waiting", "retry_when": probe, "waiting_since": now - 3600}))
        got = [w for w in queuewait.collect(p, {"coordinator": {}, "budget": {}}, now, {"limit": 0, "running": [], "queued": []})
               if w["task"] == t]
        assert bool(got) is raised and all(w["queue"] == "own" for w in got), (probe, got)


def test_block_ledger_worker_role_and_pyaudit_refusals_without_a_class_are_redirects(env):
    """Worker-role, provenance and pyaudit refusals logged with no class showed the allowed way and the run went
    on: 0 minutes, count kept, a safety guard, even when the run later hands off waiting for a planned retry.
    A run handed back to Opus (glm_guard.json in its folder) still holds the time until the task next ran."""
    from ttp import blockledger as bl
    p = make(env)
    now = time.time()

    def run(handed_back, kind):
        t = _bl_task(p, f"{kind} {handed_back}", "queued", now)
        p.db.x("INSERT INTO runs(task,role,status,started,ended) VALUES(?,?,?,?,?)", (t, "worker", "ok", now - 7200, now - 7000))
        p.db.x("INSERT INTO events(ts,source,kind,text,data,status,task) VALUES(?,?,?,?,?,?,?)",
               (now - 7010, "t", "task_waiting", "x", json.dumps({"wait": "self", "why": "retry"}), "handled", t))
        p.db.x("INSERT INTO runs(task,role,status,started,ended) VALUES(?,?,?,?,?)", (t, "worker", "ok", now - 3000, now - 2900))
        d = p.state / "runs" / str(p.db.one("SELECT min(id) AS i FROM runs WHERE task=?", (t,))["i"])
        d.mkdir(parents=True)
        (d / "refusals.jsonl").write_text("".join(
            json.dumps({"ts": now - 7100 + i, "kind": kind, "guard": f"g-{kind}"}) + "\n" for i in range(3)))
        if handed_back:
            (d / "glm_guard.json").write_text("{}")
        return [r for r in bl.rows(p.db, now - 86400, now) if r["task"] == t]

    for kind in ("worker_role", "pyaudit"):
        [r] = run(False, kind)
        assert (r["n"], r["minutes"], r["safety"]) == (3, 0.0, "guard"), r
    [r] = run(True, "worker_role")
    assert r["minutes"] > 60 and r["safety"] == "guard"


def test_block_ledger_probe_class_names_what_a_held_wait_is_on(env):
    from ttp import blockledger as bl
    cls = lambda probe, down=None: bl.probe_class(probe, {}, down)
    assert cls("test -e /h/user-hold-x || exit 1") == "user_hold"
    assert cls("ttp detach --check /r/runs/9/suite.rc") == "own_job"
    assert cls("landed:#12") == cls("ttp landed --task 12") == "landing"
    assert cls("ssh box7 true", {"box7": {}}) == "outage" and cls("ssh box70 true", {"box7": {}}) == "other"
    assert cls("test -e /tmp/box7-done", {"box7": {}}) == "other", "a file name is no machine"
    assert cls("test -e /tmp/some-marker") == "other"


def test_block_ledger_a_done_target_nothing_can_land_is_a_stuck_landing_not_a_dependency(env):
    from ttp import blockledger as bl
    p = make(env)
    now = time.time()
    x = p.db.add_task("feature", "s", kind="code")
    p.db.update_task(x, status="done", branch=f"ttp/t{x}-feature")
    live = p.db.add_task("still running", "s", kind="code")
    p.db.update_task(live, status="running")
    a = _bl_wait(p, "after the feature", now, 3 * 3600, f"ttp landed --task {x}")
    b = _bl_wait(p, "after the live one", now, 3 * 3600, f"landed:#{live}")
    rs = {r["task"]: r for r in bl.rows(p.db, now - 86400, now) if r["source"] == "task"}
    assert (rs[a]["rule"], rs[b]["rule"]) == (bl.STUCK, "dependency")
    assert [e["rule"] for e in bl.top(p.db, now)] == [bl.STUCK], "a stuck landing is a blocker to retire"
    p.db.add_task(f"Review #{x}", "s", kind="review", depends_on=[x])   # an open review can land it
    rs = {r["task"]: r for r in bl.rows(p.db, now - 86400, now) if r["source"] == "task"}
    assert rs[a]["rule"] == "dependency"


def test_block_ledger_share_splits_safety_gates_from_other_blocked_time_and_keeps_its_old_keys(env, capsys, monkeypatch):
    from ttp import blockledger as bl, cli
    p = make(env)
    monkeypatch.chdir(p.root)
    now = time.time()
    a = _bl_task(p, "held by a rule", "blocked", now, priority=1, blocked_ago=3600, reason="held by R-ab12 until later")
    b = _bl_task(p, "held by words", "blocked", now, priority=1, blocked_ago=3600, reason="keep the 60 min hold")
    p.db.x("UPDATE tasks SET created=? WHERE id IN (?,?)", (now - 7200, a, b))
    s = bl.window_share(p.db, now - 7200, now)
    assert (s["share"], s["blocked_min"], s["ordered_min"]) == (0.5, pytest.approx(120, abs=1), pytest.approx(240, abs=1))
    assert s["safety_share"] == pytest.approx(0.25, abs=0.01) and s["non_safety_share"] == pytest.approx(0.25, abs=0.01)
    cli.cmd_blocked(types.SimpleNamespace(days=1.0, json=False, done=None, how=None))
    assert "safety gates 25.0%, other 25.0% (own running jobs count as running)" in capsys.readouterr().out
    cli.cmd_blocked(types.SimpleNamespace(days=1.0, json=True, done=None, how=None))
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["share"]["non_safety_share"] == pytest.approx(0.25, abs=0.01)
