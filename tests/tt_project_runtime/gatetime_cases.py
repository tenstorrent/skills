# Landing check time tests (gatetime) of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file. Add new
# gatetime tests here.
from __future__ import annotations


def test_gate_time_is_recorded_by_each_passing_push_check_and_measured_over_a_day_and_the_last_runs(env):
    from ttp import gatetime, push
    p = make(env)
    assert gatetime.status_line(p.state, p.config()) == ""
    push.record_check_s(p, 120)   # the push path's record of a passing push check run keeps its history
    now = time.time()
    for i, s in enumerate([900, 300, 240, 200, 180, 160, 150, 140, 130, 120]):   # all older than a day
        gatetime.record(p.state, s, now=now - 2 * 86400 + i)
    gatetime.record(p.state, 0)   # no sample
    st = gatetime.stats(p.state, now)
    assert st["day"] == {"n": 1, "median_s": 120.0, "p90_s": 120.0}
    assert st["last"]["n"] == 10 and st["last"]["median_s"] == 170.0 and st["last"]["p90_s"] == 300.0
    line = gatetime.status_line(p.state, p.config(), now)
    assert line.startswith("landing checks: 24 h median 2.0 min") and "over the" not in line
    from ttp import audit
    assert any(x.startswith("landing checks:") for x in audit.lines(p.db, now=now, p=p))


def test_gate_time_over_smoke_max_raises_one_coordinator_observation_a_day_naming_the_slowest_tests(env):
    from ttp import gatetime
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()
    for i in range(10):
        gatetime.record(p.state, 700 + i, now=now - 100 + i)
    logs = p.state / "pushes"
    logs.mkdir(exist_ok=True)
    (logs / "t1-a.log").write_text("== slowest durations ==\n95.10s call     tests/a.py::test_slow\n"
                                   "3.00s setup    tests/a.py::test_slow\nttp-suite:    40.00s tests/b.py::test_mid\n")
    assert "over the 5.0 min smoke limit" in gatetime.status_line(p.state, p.config(), now)
    d = Daemon(p.base)
    d.check_gate_time(every_s=0, now=now)
    d.check_gate_time(every_s=0, now=now + 60)   # once a day
    rows = p.db.q("SELECT kind, status, text FROM events WHERE fingerprint LIKE 'gate_slow:%'")
    assert len(rows) == 1 and rows[0]["status"] == "queued"
    text = rows[0]["text"]
    assert "tests/a.py::test_slow 95s, tests/b.py::test_mid 40s" in text and "delivery.post_land_checks" in text
    # one priority-1 test speed-up task, naming the slowest tests; never a second while it is open
    speed = p.db.q("SELECT id, priority, kind, spec, status FROM tasks WHERE labels LIKE '%\"test_speed\"%'")
    assert len(speed) == 1 and speed[0]["priority"] == 1 and speed[0]["kind"] == "code"
    assert f"task #{speed[0]['id']}" in text
    assert "urgent:" in speed[0]["spec"] and "tests/a.py::test_slow: 95s" in speed[0]["spec"]
    assert "already open" in gatetime.flag(p.db, p.state, {}, logs, now + 86400)
    assert len(p.db.q("SELECT id FROM tasks WHERE labels LIKE '%\"test_speed\"%'")) == 1
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (speed[0]["id"],))
    assert "Queued test speed-up task" in gatetime.flag(p.db, p.state, {}, logs, now + 4 * 86400)
    assert len(p.db.q("SELECT id FROM tasks WHERE labels LIKE '%\"test_speed\"%'")) == 2
    d.cfg = {**d.cfg, "checks": {**(d.cfg.get("checks") or {}), "smoke_max_s": 0}}   # 0: off
    assert gatetime.flag(p.db, p.state, d.cfg, logs, now + 2 * 86400) == ""
    # under the limit: nothing
    assert gatetime.flag(p.db, p.state, {"checks": {"smoke_max_s": 1000}}, logs, now + 2 * 86400) == ""
    # no --durations lines: it says how to get them
    (logs / "t1-a.log").write_text("5 passed\n")
    assert "--durations=30" in gatetime.flag(p.db, p.state, {}, logs, now + 3 * 86400)


def test_post_land_checks_and_smoke_max_s_are_known_settings_and_post_land_checks_are_validated(env):
    from ttp import project
    good = {"delivery": {"post_land_checks": ["pytest -q tests", {"run": "make long", "host": "local"}]},
            "checks": {"smoke_max_s": 300}}
    assert not [x for x in project.config_problems(good) if "post_land" in x or "smoke_max" in x]
    bad = project.config_problems({"delivery": {"post_land_checks": [{"host": "local"}]}})
    assert any(x.startswith("delivery.post_land_checks:") for x in bad)
    assert gatetime_default_is_five_minutes()


def test_smoke_max_s_is_an_account_key_the_project_value_overrides(env):
    from ttp import cli, gatetime
    cli.main(["config", "--account", "checks.smoke_max_s", "900"])
    assert gatetime.max_s({}) == 900, "unset in the project: the account's"
    assert gatetime.max_s({"checks": {"smoke_max_s": 120}}) == 120, "project first"
    assert gatetime.max_s({"checks": {"smoke_max_s": "x"}}) == 900, "malformed: the account's"


def gatetime_default_is_five_minutes() -> bool:
    from ttp import gatetime
    return gatetime.max_s({}) == 300 == gatetime.max_s({"checks": {"smoke_max_s": "x"}}) and gatetime.AIM_S == 240


def test_waits_line_gives_checks_wait_to_start_and_approved_to_landed_and_a_checks_run_records_its_wait(
        env, tmp_path, monkeypatch, capsys):
    from ttp import audit, cli, gatetime
    p = make(env)
    now = time.time()
    assert gatetime.waits_line(p.db, p.state, now) == ""
    for i, s in enumerate([0, 60, 600, 7200, 8100]):   # 0: started at once, still a sample
        gatetime.record_wait(p.state, s, now=now - 3600 + i)
    gatetime.record_wait(p.state, 99999, now=now - 2 * 86400)   # older than a day
    q = "INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)"
    for t, (created, st) in enumerate([(now - 7200, "landed"), (now - 3600, "pushed"), (now - 60, "approved")], 1):
        p.db.x(q, (t, f"b{t}", f"h{t}", "main", st, created, now - 10))
    line = gatetime.waits_line(p.db, p.state, now)
    assert line == ("waits (24 h): checks to start median 10 min, p90 2 h 15 min (5 runs); "
                    "approved to landed median 1 h 29 min, p90 1 h 59 min (2 changes)"), line
    assert any(x.startswith("waits (24 h): checks to start") and "approved" not in x
               for x in audit.lines(p.db, now=now, p=p))
    # A full `ttp checks` run keeps how long it waited from its arrival to its start.
    p2, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    before = len(gatetime._rows(p2.state, gatetime.WAIT_LOG))
    monkeypatch.delenv("TTP_CHECKS_WAIT_KEPT", raising=False)   # never inherited; the run keeps it to itself
    monkeypatch.setattr(cli, "_checks_arrived", lambda: time.time() - 120)
    cli.main(["checks", "--", "true"])
    rows = gatetime._rows(p2.state, gatetime.WAIT_LOG)
    assert len(rows) == before + 1 and 119 <= rows[-1][1] < 200, rows


def test_slow_landing_check_advice_never_moves_tests_to_post_land_checks_while_nothing_runs_them(env):
    # Until this release runs delivery.post_land_checks after landing, no advice (the gate_slow observation,
    # the `ttp status` line, the guide) tells a coordinator to move tests there: they would not run at all.
    from ttp import gatetime
    runs = getattr(gatetime, "POST_LAND_RUNS", False)
    p = make(env)
    now = time.time()
    for i in range(10):
        gatetime.record(p.state, 700 + i, now=now - 100 + i)
    line = gatetime.status_line(p.state, p.config(), now)
    text = gatetime.flag(p.db, p.state, p.config(), p.state / "no-logs", now)
    guide = (RUNTIME.parent / "docs" / "guide.md").read_text()
    smoke = guide[guide.index("- Smoke before landing:"):]
    smoke = smoke[:smoke.index("\n- ")]
    for said in (line, text, smoke):
        assert "post_land_checks" in said, said
        assert ("not run" in said or "nothing runs" in said) != runs, said
        assert runs or "which run after landing" not in said, said
    # The flag tells the truth: while it is off, no runtime module but the setting's own reads the list.
    readers = sorted(f.name for f in (RUNTIME / "ttp").glob("*.py")
                     if "post_land_checks" in f.read_text() and f.name not in ("project.py", "gatetime.py"))
    assert runs or not readers, readers


def test_ttp_checks_keeps_its_wait_recorded_flag_out_of_check_commands_and_later_runs(env, tmp_path, monkeypatch):
    # The flag that a rerun's wait was recorded is never inherited: not by the check commands, and not by a
    # later `ttp checks` in the same process, which records its own wait.
    from ttp import cli, gatetime
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv("TTP_CHECKS_WAIT_KEPT", raising=False)
    monkeypatch.setattr(cli, "_checks_arrived", lambda: time.time() - 60)
    seen = tmp_path / "seen"
    cli.main(["checks", "--", f"sh -c 'echo \"[${{TTP_CHECKS_WAIT_KEPT:-}}]\" >> {seen}'"])
    assert "TTP_CHECKS_WAIT_KEPT" not in os.environ
    cli.main(["checks", "--", f"sh -c 'echo \"<${{TTP_CHECKS_WAIT_KEPT:-}}>\" >> {seen}'"])
    assert seen.read_text().split() == ["[]", "<>"], seen.read_text()
    assert len(gatetime._rows(p.state, gatetime.WAIT_LOG)) == 2


# Post-landing checks (postland): after a landing, delivery.post_land_checks run on the pushed sha; a new failure
# is bisected over the landed entries, the guilty one reverted forward through the push queue, one fix task queued.
_PL_FAIL = "printf 'FAILED tests/t.py::test_x - boom\\n= 1 failed in 0.1s =\\n'; exit 1"
_PL_PASS = "echo '= 1 passed in 0.1s ='"


def _pl_land(env, tmp_path, monkeypatch, check, n=3, bad_at=None, rows_reversed=False):
    """A project whose push branch up/main got one batch of `n` entries (entry `bad_at` adds a file named bad)
    plus a version bump, recorded as pushed; post_land_checks = [check]; the driver runs inline. rows_reversed:
    the queue rows are added last entry first (a batch replays in priority order, not id order)."""
    import json as _json
    import subprocess as sp
    from ttp import postland
    p = make(env)
    g = lambda *a: sp.run(["git", "-C", str(p.root), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()
    sp.run(["git", "init", "-q", "--bare", str(tmp_path / "up.git")], check=True)
    g("remote", "add", "up", str(tmp_path / "up.git"))
    tip, entries = g("rev-parse", "HEAD"), []
    for i in range(n):
        (p.root / f"pl{i}.txt").write_text("bad\n" if i == bad_at else "ok\n")
        g("add", f"pl{i}.txt"); g("commit", "-qm", f"entry {i}")
        entries.append(g("rev-parse", "HEAD"))
    (p.root / "PLVERSION").write_text("2\n"); g("add", "PLVERSION"); g("commit", "-qm", "bump")
    pushed = g("rev-parse", "HEAD")
    g("push", "-q", "up", "HEAD:refs/heads/main")
    now = time.time()
    p.db.x("INSERT INTO push_batches(id,marker,target,started,ended,outcome,pushed_sha,tip,finalized) "
           "VALUES(?,?,?,?,?,?,?,?,?)", ("b1", "m", "up/main", now - 60, now - 30, "pushed", pushed, tip, now - 30))
    for i, sha in (reversed if rows_reversed else list)(list(enumerate(entries))):
        t = p.db.add_task(f"change {i}", kind="review", status="done")
        p.db.x("INSERT INTO push_queue(task,branch,head,target,status,batch,created,updated,pushed_sha,landed_sha) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)", (t, f"ch{i}", sha, "up/main", "landed", "b1", now, now, pushed, sha))
    p.set_config("delivery.post_land_checks", [check])

    def inline(p_, job):
        job.with_suffix(".result.json").write_text(_json.dumps(postland.drive(p_, _json.loads(job.read_text()))))
        return 0
    monkeypatch.setattr(postland, "spawn", inline)
    p._pl_tip = tip
    return p, g, pushed, entries


def test_post_land_checks_that_pass_record_the_pushed_sha_and_run_once_per_landing(env, tmp_path, monkeypatch):
    from ttp import postland
    p, g, pushed, _ = _pl_land(env, tmp_path, monkeypatch, f"sh -c \"grep -qs bad pl*.txt && {{ {_PL_FAIL}; }}; {_PL_PASS}\"")
    assert postland.tend(p, {}) == "", "no post_land_checks: nothing"
    assert postland.tend(p, p.config()).startswith(f"post-landing checks started on {pushed[:10]}")
    assert postland.tend(p, p.config()) == f"post-landing checks passed on {pushed[:10]}"
    st = p.db.kv(postland.KEY)
    assert st["last"] == {"outcome": "pass", "sha": pushed} and st["job"] is None
    assert postland.tend(p, p.config()) == "", "the same landing is not checked twice"
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Revert%' OR title LIKE 'Fix%'")


def test_post_land_failure_is_bisected_to_its_entry_reverted_through_the_push_queue_and_one_fix_task_queued(
        env, tmp_path, monkeypatch):
    import subprocess as sp
    from ttp import postland
    p, g, pushed, entries = _pl_land(env, tmp_path, monkeypatch,
                                     f"sh -c \"grep -qs bad pl*.txt && {{ {_PL_FAIL}; }}; {_PL_PASS}\"", n=3, bad_at=0,
                                     rows_reversed=True)
    postland.tend(p, p.config())
    assert postland.tend(p, p.config()) == "guilty"
    fix = p.db.q("SELECT * FROM tasks WHERE title LIKE 'Fix #%'")
    assert len(fix) == 1 and fix[0]["kind"] == "code" and "ch0" in fix[0]["title"], fix
    assert "tests/t.py::test_x" in fix[0]["spec"] and "the only ones to rerun" in fix[0]["spec"]
    rev = p.db.q("SELECT * FROM tasks WHERE title LIKE 'Revert #%'")
    assert len(rev) == 1 and rev[0]["status"] == "pushing", rev
    rows = p.db.q("SELECT * FROM push_queue WHERE task=?", (rev[0]["id"],))
    assert len(rows) == 1 and rows[0]["status"] == "approved" and rows[0]["target"] == "up/main"
    assert rows[0]["branch"] == f"ttp/postland-revert-{entries[0][:10]}"
    head = rows[0]["head"]
    assert g("rev-parse", f"{head}^") == pushed, "a forward revert on the tip, never a rewrite"
    assert g("diff", "--name-only", pushed, head) == "pl0.txt", "only the guilty entry is reverted"
    assert sp.run(["git", "-C", str(p.root), "cat-file", "-e", f"{head}:pl0.txt"]).returncode != 0
    assert f"start_when:landed:#{rev[0]['id']}" in fix[0]["labels"], "the fix starts once the revert has landed"
    assert fix[0]["id"] > rev[0]["id"] and f"{p._pl_tip[:10]}..{entries[0][:10]}" in fix[0]["spec"]
    assert head[:10] in fix[0]["spec"] and "on top of the revert" in fix[0]["spec"]
    assert p.db.q("SELECT id FROM events WHERE text LIKE '%fails tests/t.py::test_x; revert #%; fix task #% starts "
                  "once it lands' AND status='queued'")


def _pl_own(p, g, name, content, ts):
    """A task's own `ttp push` onto up/main: one commit adding `name`, pushed, recorded in task_landings."""
    (p.root / name).write_text(content); g("add", name); g("commit", "-qm", f"own {name}")
    sha = g("rev-parse", "HEAD")
    g("push", "-q", "up", "HEAD:refs/heads/main")
    t = p.db.add_task(f"own {name}", kind="code", status="done")
    p.db.x("UPDATE tasks SET branch=? WHERE id=?", (f"ttp/own-{name}", t))
    p.db.x("INSERT INTO task_landings(marker,task,sha,target,ts) VALUES(?,?,?,?,?)",
           (f"t{t}-own-{name}.json", t, sha, "up/main", ts))
    return t, sha


def test_post_land_checks_run_after_a_tasks_own_push_landing_and_only_once(env, tmp_path, monkeypatch):
    from ttp import postland
    p, g, pushed, _ = _pl_land(env, tmp_path, monkeypatch, f"sh -c \"grep -qs bad *.txt && {{ {_PL_FAIL}; }}; {_PL_PASS}\"")
    p.set_config("delivery.push_branch", "up/main")
    postland.tend(p, p.config()); postland.tend(p, p.config())
    assert postland.tend(p, p.config()) == "", "the batch is checked"
    t, own = _pl_own(p, g, "own.txt", "ok\n", time.time())
    assert postland.tend(p, p.config()) == f"post-landing checks started on {own[:10]}"
    job = p.db.kv(postland.KEY)["job"]
    assert [(e["sha"], e["base"], e["task"]) for e in job["points"]] == [(own, pushed, t)], job["points"]
    assert job["good"] == pushed
    assert postland.tend(p, p.config()) == f"post-landing checks passed on {own[:10]}"
    assert postland.tend(p, p.config()) == "", "an own landing already checked is not checked again"


def test_post_land_orders_a_batch_and_an_own_push_by_branch_position_and_reverts_the_own_push(
        env, tmp_path, monkeypatch):
    from ttp import postland
    p, g, pushed, entries = _pl_land(env, tmp_path, monkeypatch,
                                     f"sh -c \"grep -qs bad *.txt && {{ {_PL_FAIL}; }}; {_PL_PASS}\"")
    p.set_config("delivery.push_branch", "up/main")
    p.db.set_kv(postland.KEY, {"since": 1})
    # the own push sits after the batch on the branch but its marker ended before the batch did
    t, own = _pl_own(p, g, "own.txt", "bad\n", time.time() - 100)
    assert postland.tend(p, p.config()) == f"post-landing checks started on {own[:10]}"
    job = p.db.kv(postland.KEY)["job"]
    assert [e["sha"] for e in job["points"]] == [*entries, pushed, own], "by position on the branch, not by time"
    assert job["points"][-1]["base"] == pushed and job["good"] == p._pl_tip
    assert postland.tend(p, p.config()) == "guilty"
    rev = p.db.q("SELECT * FROM tasks WHERE title LIKE 'Revert #%'")
    assert len(rev) == 1, rev
    row = p.db.one("SELECT * FROM push_queue WHERE task=?", (rev[0]["id"],))
    assert g("rev-parse", f"{row['head']}^") == own and g("diff", "--name-only", own, row["head"]) == "own.txt"
    fix = p.db.q("SELECT * FROM tasks WHERE title LIKE 'Fix #%'")
    assert len(fix) == 1 and f"{pushed[:10]}..{own[:10]}" in fix[0]["spec"], fix


def test_post_land_check_on_a_busy_test_lock_backs_off_and_still_checks_the_landing(env, tmp_path, monkeypatch):
    from ttp import postland
    n = tmp_path / "count"
    check = f"sh -c \"c=\\$(cat {n} 2>/dev/null || echo 0); echo x >> {n}; [ \\$c = 0 ] && exit 75; {_PL_PASS}\""
    p, g, pushed, _ = _pl_land(env, tmp_path, monkeypatch, check)
    p.set_config("checks.judge_busy_wait_s", 0.05)
    postland.tend(p, p.config())
    assert postland.tend(p, p.config()) == f"post-landing checks passed on {pushed[:10]}"
    assert len(n.read_text().split()) == 2, "the busy run is retried after the backoff"
    assert p.db.kv(postland.KEY)["last"] == {"outcome": "pass", "sha": pushed}
    assert not p.db.q("SELECT id FROM events WHERE kind='observation'"), "a busy lock raises nothing"


def _pl_stub(monkeypatch, plan):
    """spawn() stub: each call takes the next of `plan`: 'busy' writes a busy result for that attempt's own
    file; 'running' writes nothing and reports this (live) process as the driver. Returns the spawned paths."""
    import json as _json
    import os as _os
    from ttp import postland
    spawned = []

    def stub(p_, job):
        spawned.append(job)
        if plan.pop(0) == "busy":
            job.with_suffix(".result.json").write_text(_json.dumps({"outcome": "busy", "why": "lock"}))
        return _os.getpid()
    monkeypatch.setattr(postland, "spawn", stub)
    return spawned


def test_post_land_busy_retry_never_reads_an_earlier_attempts_result_and_ends_unattributed_once(
        env, tmp_path, monkeypatch):
    import json as _json
    from ttp import postland
    p, g, pushed, _ = _pl_land(env, tmp_path, monkeypatch, _PL_PASS)
    plan = ["busy", "running"]
    spawned = _pl_stub(monkeypatch, plan)
    for _ in range(3):   # the first busy result reruns the landing (within a pass or two)
        postland.tend(p, p.config())
        if len(spawned) == 2:
            break
    assert len(spawned) == 2 and spawned[0] != spawned[1], "each attempt has its own job file"
    for _ in range(3):   # the new driver still runs: the earlier attempt's busy result is never read again
        assert postland.tend(p, p.config()) == ""
    assert p.db.kv(postland.KEY)["busy"] == 1 and len(spawned) == 2, "no second driver while one is alive"
    spawned[1].with_suffix(".result.json").write_text(_json.dumps({"outcome": "busy", "why": "lock"}))
    plan.append("busy")
    assert "busy, retried" in postland.tend(p, p.config()) and p.db.kv(postland.KEY)["busy"] == 2
    out = postland.tend(p, p.config())
    st = p.db.kv(postland.KEY)
    assert st["last"]["outcome"] == "unattributed" and st["job"] is None and st["busy"] == 0, (out, st)
    assert len(p.db.q("SELECT id FROM events WHERE text LIKE '%busy for 3 passes%'")) == 1
    assert postland.tend(p, p.config()) == "" and len(spawned) == 3, "applied once; the landing is settled"
    assert len(p.db.q("SELECT id FROM events WHERE text LIKE '%busy for 3 passes%'")) == 1


def test_post_land_busy_retry_on_the_first_run_keeps_that_landings_range(env, tmp_path, monkeypatch):
    import json as _json
    from ttp import postland
    p, g, pushed, entries = _pl_land(env, tmp_path, monkeypatch, _PL_PASS)
    now, root = time.time(), g("rev-list", "--max-parents=0", "HEAD")
    for i, ago in enumerate((600, 300)):   # two earlier landings, before post_land_checks ran at all
        p.db.x("INSERT INTO push_batches(id,marker,target,started,ended,outcome,pushed_sha,tip,finalized) "
               "VALUES(?,?,?,?,?,?,?,?,?)", (f"b0{i}", "m", "up/main", now - ago - 10, now - ago, "pushed",
                                             p._pl_tip if i else root, root if i else "", now - ago))
    spawned = _pl_stub(monkeypatch, ["busy", "running"])
    postland.tend(p, p.config())
    first = _json.loads(spawned[0].read_text())
    assert first["good"] == p._pl_tip and first["sha"] == pushed
    for _ in range(2):
        postland.tend(p, p.config())
        if len(spawned) == 2:
            break
    again = _json.loads(spawned[1].read_text())
    assert again["good"] == p._pl_tip and again["sha"] == pushed, "the retry keeps only this landing's range"
    assert [x["sha"] for x in again["points"]] == [x["sha"] for x in first["points"]] == [*entries, pushed]


def test_post_land_failure_that_passes_on_rerun_is_flaky_quarantined_and_never_reverted(env, tmp_path, monkeypatch):
    from ttp import gatejudge, postland
    n = tmp_path / "count"
    check = f"sh -c \"c=\\$(cat {n} 2>/dev/null || echo 0); echo x >> {n}; [ \\$c = 0 ] && {{ {_PL_FAIL}; }}; {_PL_PASS}\""
    p, g, pushed, _ = _pl_land(env, tmp_path, monkeypatch, check)
    postland.tend(p, p.config())
    assert postland.tend(p, p.config()) == "flaky"
    assert p.db.kv(postland.KEY)["last"]["ids"] == ["tests/t.py::test_x"]
    assert "tests/t.py::test_x" in gatejudge.active(p.state, time.time())
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Revert%' OR title LIKE 'Fix%'")
    assert not p.db.q("SELECT id FROM push_queue WHERE status='approved'")
    # A fix owner, as the tests gate's flakes get: the day's deflake task of its module lists it.
    (t,) = p.db.q("SELECT * FROM tasks WHERE title LIKE 'Deflake %'")
    assert t["title"] == "Deflake tests/t.py: test_x" and t["kind"] == "code" and t["status"] == "queued"
    assert "- tests/t.py::test_x" in t["spec"] and pushed[:12] in t["spec"] and "post_land_checks" in t["spec"]
    assert p.db.kv(postland.KEY)["last"]["deflake"] == [t["id"]]
    assert p.db.q("SELECT id FROM events WHERE text LIKE ?", (f"%flaky%deflake task #{t['id']}",))
    assert gatejudge.file_orphans(p) == [], "already owned: the daemon's sweep files nothing more"


_PL_X = "tests/t.py::test_x"
_PL_BAD = f"sh -c \"grep -qs bad pl*.txt && {{ {_PL_FAIL}; }}; {_PL_PASS}\""


def _pl_owner(p, ids=(_PL_X,)):
    """`ids` quarantined with their module's deflake task filed, as the tests gate leaves a flake; the task id."""
    from ttp import gatejudge
    now = time.time()
    gatejudge.quarantine(p.state, list(ids), {"head": "0" * 12, "check": "the tests gate"}, now)
    (tid,) = gatejudge.deflake(p.db, list(ids), {"head": "0" * 12}, now, p)
    return tid


def test_post_land_failure_only_of_quarantined_tests_with_an_open_owner_goes_on_its_spec_no_fix_no_revert(
        env, tmp_path, monkeypatch):
    from ttp import gatejudge, postland
    p, g, pushed, _ = _pl_land(env, tmp_path, monkeypatch, _PL_BAD, n=3, bad_at=0)
    tid = _pl_owner(p)
    postland.tend(p, p.config())
    assert postland.tend(p, p.config()) == "owned"
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Revert%' OR title LIKE 'Fix%'"), "no duplicate fix"
    assert not p.db.q("SELECT id FROM push_queue WHERE status='approved'"), "nothing reverted"
    assert p.db.kv(postland.KEY)["last"]["owned"] == {_PL_X: tid}
    line = f"{gatejudge.SEEN_AGAIN} again at {pushed[:12]}: {_PL_X}"
    spec = p.db.task(tid)["spec"]
    assert spec.count(line) == 1, spec
    postland.apply(p, p.db.kv(postland.KEY)["last"] | {"target": "up/main"}, p.db.kv(postland.KEY)["last"])
    assert p.db.task(tid)["spec"].count(line) == 1, "one line per sha"
    # The day's deflake task gathering another flake keeps the post-landing sighting on its rewritten spec.
    gatejudge.deflake(p.db, ["tests/t.py::test_z"], {"head": "1" * 12}, time.time(), p)
    spec = p.db.task(tid)["spec"]
    assert "- tests/t.py::test_z" in spec and line in spec, spec
    assert p.db.q("SELECT id FROM events WHERE text LIKE ? AND status='handled'", (f"%only quarantined%#{tid}%",))


def test_post_land_mixed_failure_reverts_and_fixes_only_the_tests_not_quarantined(env, tmp_path, monkeypatch):
    from ttp import gatejudge, postland
    both = "printf 'FAILED tests/t.py::test_x - boom\\nFAILED tests/t.py::test_y - boom\\n= 2 failed in 0.1s =\\n'; exit 1"
    check = f"sh -c \"grep -qs bad pl*.txt && {{ {both}; }}; {_PL_FAIL}\""   # test_x fails everywhere
    p, g, pushed, entries = _pl_land(env, tmp_path, monkeypatch, check, n=3, bad_at=1)
    tid = _pl_owner(p)
    postland.tend(p, p.config())
    assert postland.tend(p, p.config()) == "guilty"
    last = p.db.kv(postland.KEY)["last"]
    assert last["ids"] == ["tests/t.py::test_y"] and last["owned"] == {_PL_X: tid}
    (fix,) = p.db.q("SELECT * FROM tasks WHERE title LIKE 'Fix #%'")
    assert "ch1" in fix["title"] and "the only ones to rerun: tests/t.py::test_y\n" in fix["spec"], fix["spec"]
    assert f"not this task's: {_PL_X} (deflake #{tid})" in fix["spec"]
    (rev,) = p.db.q("SELECT * FROM push_queue WHERE status='approved'")
    msg = g("log", "-1", "--format=%B", rev["head"])
    assert "failing: tests/t.py::test_y" in msg and "test_x" not in msg, msg
    assert g("diff", "--name-only", pushed, rev["head"]) == "pl1.txt"
    assert f"again at {pushed[:12]}: {_PL_X}" in p.db.task(tid)["spec"]
    assert _PL_X in gatejudge.active(p.state, time.time()), "still quarantined, never counted as passed"


def test_post_land_failure_of_a_quarantined_test_whose_owner_finished_is_reverted_and_fixed(env, tmp_path, monkeypatch):
    from ttp import postland
    p, g, pushed, _ = _pl_land(env, tmp_path, monkeypatch, _PL_BAD, n=3, bad_at=0)
    tid = _pl_owner(p)
    p.db.update_task(tid, status="cancelled")
    postland.tend(p, p.config())
    assert postland.tend(p, p.config()) == "guilty", "no open owner: any other failure's path"
    assert "owned" not in p.db.kv(postland.KEY)["last"]
    assert len(p.db.q("SELECT id FROM tasks WHERE title LIKE 'Fix #%'")) == 1
    assert len(p.db.q("SELECT id FROM push_queue WHERE status='approved'")) == 1
    assert "again at" not in p.db.task(tid)["spec"]


def test_flake_seen_at_a_head_before_its_landed_deflake_fix_is_not_quarantined_again(env, tmp_path, monkeypatch):
    from ttp import gatejudge, landed
    p, g, pushed, _ = _pl_land(env, tmp_path, monkeypatch, _PL_BAD, n=3, bad_at=0)
    tid = _pl_owner(p)
    gatejudge.lift(p.state, [_PL_X], "its fix landed")
    p.db.update_task(tid, status="done")
    contains = {"v": False}
    monkeypatch.setattr(landed, "check", lambda *a, onto=None, **k: (0 if not onto or contains["v"] else 1, ""))
    monkeypatch.setattr(gatejudge, "_push_tip", lambda p_: (Path(p.root), "tip"))
    assert gatejudge.before_fix(p, [_PL_X, "tests/t.py::test_other"], "a" * 12) == [_PL_X], "head lacks the fix"
    contains["v"] = True
    assert gatejudge.before_fix(p, [_PL_X], "a" * 12) == [], "head has the fix: a real flake again"
    contains["v"] = False
    p.db.update_task(tid, status="queued")
    assert gatejudge.before_fix(p, [_PL_X], "a" * 12) == [], "owner not landed"


def test_post_land_first_full_run_spills_and_its_reruns_bisect_and_revert_run_here(env, tmp_path, monkeypatch):
    """The post-landing suite's first full run on the landed sha goes to a spill host (checkspill.spill_run, as
    the push batch's first runs do) and its claim is closed after it; every rerun of the failing tests (at the
    head, before the landing, the bisect and the revert's check) runs here on the check itself. The guilty entry
    is still pulled out end to end. A spill attempt that errors keeps the run here."""
    from ttp import checkspill, postland, push
    spilled, closed, asked = tmp_path / "spilled", [], []

    class Claim:
        host = "check-host-a"

        def close(self):
            closed.append(1)

    def fake_spill_run(p_, cfg, cmds, tell, what="", off="", why_here=""):
        asked.append(what)
        c = push.check_list([f"echo x >> {spilled}; {cmds[0]}"])[0]
        c.spilled, c.was = True, cmds[0]
        return Claim(), [c]

    monkeypatch.setattr(checkspill, "spill_run", fake_spill_run)
    p, g, pushed, entries = _pl_land(env, tmp_path, monkeypatch,
                                     f"sh -c \"grep -qs bad pl*.txt && {{ {_PL_FAIL}; }}; {_PL_PASS}\"", n=3, bad_at=1)
    postland.tend(p, p.config())
    assert postland.tend(p, p.config()) == "guilty"
    assert spilled.read_text().count("x") == 1, "only the first full run spills"
    assert asked == [f"post-landing checks on {pushed[:10]}"] and closed == [1]
    rows = p.db.q("SELECT branch FROM push_queue WHERE status='approved'")
    assert [r["branch"] for r in rows] == [f"ttp/postland-revert-{entries[1][:10]}"]
    assert len(p.db.q("SELECT id FROM tasks WHERE title LIKE 'Fix #%'")) == 1

    def broken(*a, **k):
        raise OSError("no spill pool")

    monkeypatch.setattr(checkspill, "spill_run", broken)
    said = []
    got = postland._spill(p, push.check_list(["true"])[0], pushed, said.append)
    assert got[0] is None and str(got[1]) == "true" and "the checks run here" in said[0]


def test_push_checks_running_only_a_marked_subset_with_no_post_land_checks_raise_one_observation_a_day(env):
    """Push checks that run a pytest `-m`/`-k` subset while delivery.post_land_checks is empty leave the rest of
    the suite unrun: one model-free observation a day names the fix (the full suite as post_land_checks, as an
    exact command); none once post_land_checks is set, for an unmarked run, or with checks.subset_flag false."""
    from ttp import gatetime
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()
    smoke = {"run": "python3 -m pytest -q -m smoke tests/a.py tests/b.py", "if_exists": "tests/smoke.txt"}
    cfg = {"delivery": {"push_checks": [smoke, "tool validate --strict"]}}
    text = gatetime.subset_flag(p.db, cfg, now, p.name)
    assert "delivery.post_land_checks is empty" in text
    assert (f"`ttp config {p.name} delivery.post_land_checks "
            "'[{\"run\": \"python3 -m pytest -q tests/a.py tests/b.py\"}]'`") in text
    assert gatetime.subset_flag(p.db, cfg, now + 60, p.name) == "", "once a day"
    assert gatetime.subset_flag(p.db, cfg, now + 86400, p.name)
    assert len(p.db.q("SELECT id FROM events WHERE kind='observation' AND fingerprint LIKE 'checks_subset:%'")) == 2
    later = now + 3 * 86400
    k = {"delivery": {"push_checks": ["pytest -k 'fast or quick' tests"]}}
    assert "pytest tests" in gatetime.subset_flag(p.db, k, later, p.name)
    for quiet in ({"delivery": {**cfg["delivery"], "post_land_checks": ["python3 -m pytest -q tests"]}},
                  {"delivery": {"push_checks": ["python3 -m pytest -q --maxfail=1 tests"]}},
                  {**cfg, "checks": {"subset_flag": False}}):
        assert gatetime.subset_flag(p.db, quiet, later + 86400, p.name) == "", quiet
    d = Daemon(p.base)
    d.cfg = {**d.cfg, **cfg}
    d.check_gate_time(now=later + 2 * 86400)
    assert p.db.one("SELECT id FROM events WHERE kind='observation' AND fingerprint=?",
                    (f"checks_subset:{time.strftime('%Y-%m-%d', time.gmtime(later + 2 * 86400))}",))


def test_checks_to_start_counts_the_host_slot_wait_the_queue_wait_marks_count(env):
    """#8438: 'checks to start' read 0 min over 321 runs while 209 checks queue-wait marks passed 30 min.
    With slot waits logged (checkcap's WAITS_LOG: arrival to grant, the span queuewait counts) the metric
    takes them, ordinary ("queue") slots only; a push batch's slot and the older WAIT_LOG samples do not."""
    from ttp import checkcap, gatetime
    p = make(env)
    now = time.time()
    gatetime.record_wait(p.state, 1.5, now=now - 60)
    rows = [("queue", 0), ("queue", 40 * 60), ("queue", 50 * 60), ("push", 0), ("push", 0)]
    (p.state / checkcap.WAITS_LOG).write_text("".join(
        json.dumps({"project": p.name, "slot": slot, "queued_at": now - 7200 - s, "granted_at": now - 7200}) + "\n"
        for slot, s in rows))
    line = gatetime.waits_line(p.db, p.state, now, landed=False)
    assert line == "waits (24 h): checks to start median 40 min, p90 50 min (3 runs)", line
