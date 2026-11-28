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
    rs = {r["task"]: r for r in bl.rows(p.db, {}, now - 86400, now)}
    assert (rs[a]["rule"], rs[a]["minutes"], rs[a]["safety"]) == ("R-ab12", 120.0, "rule")
    assert rs[b]["rule"].startswith("keep the min hold") and rs[b]["safety"] == "convenience"
    assert rs[c]["safety"] == "review or push" and rs[c]["kind"] == "review"
    assert [r["minutes"] for r in bl.rows(p.db, {}, now - 86400, now)] == sorted(
        [r["minutes"] for r in bl.rows(p.db, {}, now - 86400, now)], reverse=True)


@pytest.mark.parametrize("reason,kind,want", [
    ("waiting on resource:blx-device", "waiting", "held resource"),
    ("check:abc123 failed", "blocked", "check"),
    ("per user msg #12", "blocked", "user message"),
    ("holds under R-1a2b", "blocked", "rule"),
    ("waits for the review", "waiting", "review or push"),
    ("anything", "review", "review or push"),
    ("the user's pause stands", "blocked", "a pause or hold the user set"),
    ("the daily budget is spent", "blocked", "credential, money or a step only the user holds"),
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
    rs = bl.rows(p.db, {}, now - 86400, now)
    by = {r["rule"]: r for r in rs if r["source"] != "task"}
    assert by["full_suite"]["source"] == "refusal" and by["full_suite"]["n"] == 1
    assert by["records"]["n"] == 2 and by["records"]["minutes"] > 60   # until the task next ran
    assert (by["records"]["safety"], by["no_device"]["safety"]) == ("guard", "guard")   # safety guards are no rules to retire
    assert by["full_suite"]["safety"] == by["redirected"]["safety"] == "convenience"
    assert all(r["end"] <= now - 3000 for r in bl.rows(p.db, {}, now - 86400, now - 3000) if r["source"] == "refusal")
    assert by["queue:checks"]["minutes"] == 10.0
    assert (by["keep 60 min"]["minutes"], by["keep 60 min"]["source"]) == (5.0, "cli")
    assert [e["rule"] for e in bl.top(rs, 1)] == [rs[0]["rule"]]


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
    s = bl.window_share(p.db, {}, now - 7200, now)
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
    cli.cmd_blocked(types.SimpleNamespace(days=1.0, json=True))
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["target"] == 0.05 and {e["rule"] for e in out["top"]} == {"held by a keep constant", "keep constant"}
    assert {e["safety"] for e in out["top"]} == {"convenience"}
    cli.cmd_blocked(types.SimpleNamespace(days=1.0, json=False))
    assert "of ordered work over 1 d (target 5%)" in capsys.readouterr().out
