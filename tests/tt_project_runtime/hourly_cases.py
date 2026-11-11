# The hourly status table (hourly.py, Daemon.post_hourly_status, `ttp status --hourly`) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file. Add tests of the table's rows, its load sampler and
# when it posts here.
from __future__ import annotations


def _hourly_run(p, provider, how, started, **extra):
    note = {} if how is None else {"coord_context": {"how": how, **extra}}
    p.db.x("INSERT INTO runs(role, provider, status, started, note) VALUES('worker', ?, 'done', ?, ?)",
           (provider, started, json.dumps(note)))


def test_the_fork_rate_counts_forks_and_resumes_of_a_run_per_provider(env):
    from ttp import hourly
    p = make(env)
    now = time.time()
    _hourly_run(p, "claude", "fork", now - 60, session="s1")
    _hourly_run(p, "claude", "resume", now - 60, run=7)
    _hourly_run(p, "claude", "resume", now - 60)          # no source run recorded: not a resume of a run
    _hourly_run(p, "claude", "disk", now - 60)
    _hourly_run(p, "codex", "fork", now - 60)
    _hourly_run(p, "codex", "none", now - 60)
    _hourly_run(p, "codex", None, now - 60)               # no note: counted, never forked
    _hourly_run(p, "codex", "fork", now - 2 * 86400)      # another day
    p.db.x("INSERT INTO runs(role, provider, status, started, note) VALUES('coordinator', 'claude', 'done', ?, ?)",
           (now - 60, json.dumps({"coord_context": {"how": "fork"}})))
    # Task #8308: forks, resumes and disk-block starts apart; a disk-block start is never a fork.
    assert hourly.fork_share(p.db, now - 3600) == {"claude": [1, 1, 1, 4], "codex": [1, 0, 0, 3]}
    row = hourly.rows(p, now)[0]
    assert row.startswith("Fork rate: last hour 3/7 (43%: fork 2, resume 1, disk 1; claude 2/4, codex 1/3)"), row
    assert "target 90%" in row and "below target last hour" in row, row


def test_the_load_sampler_writes_its_history_and_the_row_reads_it(env, monkeypatch):
    from ttp import hourly, machines
    p = make(env)
    now = time.time()
    assert hourly.sample(p.state, now - 600, load1=4.0, cores=16)
    assert not hourly.sample(p.state, now - 590, load1=9.0, cores=16), "never two readings within SAMPLE_S"
    assert hourly.sample(p.state, now, load1=8.0, cores=16)
    lines = (p.state / hourly.SAMPLES).read_text().splitlines()
    assert [json.loads(x)["load1"] for x in lines] == [4.0, 8.0]
    monkeypatch.setattr(machines, "here", lambda known=None: ("box-a", {}))
    row = hourly.rows(p, now)[1]
    assert row.startswith("Project host box-a: load 0.50/core now, mean 0.38, max 0.50 in the last hour (2 samples)"), row
    monkeypatch.setattr(machines, "here", lambda known=None: None)
    assert hourly.rows(p, now)[1].startswith("Project host the project host: load"), "unlisted: no host name"


def test_the_spill_row_counts_runs_placed_passed_and_run_again_here(env):
    from ttp import hourly
    from ttp.prguard import CHECKS_FILE
    p = make(env)
    now = time.time()
    for i, rec in enumerate(({"spilled_to": "spill-a", "passed": True}, {"spilled_to": "spill-a", "passed": False},
                             {"passed": True, "spill_off": "spill-b:fail"}, {"spilled_to": "spill-a", "passed": True,
                                                                             "ts": now - 2 * 86400})):
        d = p.runs / f"r{i}"
        d.mkdir(parents=True)
        (d / CHECKS_FILE).write_text(json.dumps({"ts": now - 60, **rec}))
        p.db.x("INSERT INTO runs(role, status, started, dir) VALUES('worker', 'done', ?, ?)", (now - 120, str(d)))
    assert hourly.spill_use(p.db, p.runs, now - 86400) == {"spill-a": [2, 1, 0], "spill-b": [1, 0, 1]}
    assert ("Spill hosts in 24 h: spill-a 2 placed, 1 passed, 0 run again here; spill-b 1 placed, 0 passed, "
            "1 run again here") in hourly.rows(p, now)[1]


def _block(p, title="stuck"):
    t = p.db.add_task(title, "spec")
    p.db.x("UPDATE tasks SET status='blocked', blocked_reason='needs a look' WHERE id=?", (t,))
    return t


def test_the_table_posts_only_when_something_important_changed(env):
    from ttp import hourly
    p = make(env)
    p.set_config("notify.chat_status", True)   # the opt-in: the table goes to the main chat
    p.db.post("in", "hello", chat="main")
    now = time.time() - 3600
    assert hourly.post(p, now - 3700) is None, "the first pass only sets the baseline"
    p.db.add_task("first", "spec")
    assert hourly.post(p, now) is None, "a new task alone (a count) is not important"
    t = _block(p)
    first = hourly.post(p, now + hourly.CHECK_S + 1)
    msg = p.db.one("SELECT * FROM messages WHERE id=?", (first,))
    assert msg["chat"] == "main" and msg["ref"] == hourly.REF and msg["text"].startswith("Status at ")
    assert "(a blocker appeared)" in msg["text"] and "Queue: 2 open tasks" in msg["text"]
    assert hourly.post(p, now + hourly.CHECK_S + 60) is None, "nothing new"
    p.db.x("UPDATE tasks SET status='queued' WHERE id=?", (t,))
    second = hourly.post(p, now + 2 * hourly.CHECK_S + 61)
    assert second and "(a blocker cleared)" in p.db.one("SELECT text FROM messages WHERE id=?", (second,))["text"]
    assert p.db.one("SELECT COUNT(*) n FROM messages WHERE ref=?", (hourly.REF,))["n"] == 2


def test_counts_times_loads_or_a_landed_batch_alone_post_nothing(env, monkeypatch):
    from ttp import hourly
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    p.db.post("in", "hello", chat="main")
    now = time.time() - 3 * 3600
    assert hourly.post(p, now) is None
    for i in range(3):   # a new task, a landed batch, a deploy record, a load reading and an hour each time
        t = p.db.add_task(f"t{i}", "spec")
        p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated) "
               "VALUES(?, 'b', 'h', 'origin/x', 'landed', ?, ?)", (t, now + i * 3600, now + i * 3600 + 1))
        hourly.sample(p.state, now + (i + 1) * 3600, load1=float(i + 1), cores=4)
        assert hourly.post(p, now + (i + 1) * 3600 + 5) is None
    assert not p.db.q("SELECT id FROM messages WHERE ref=?", (hourly.REF,))


def test_two_milestones_minutes_apart_post_once_and_the_next_names_both(env, monkeypatch):
    from ttp import hourly, obligations
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    p.set_config("notify.chat_status", True)   # the chat's status gap applies only when the chat gets the table
    p.db.post("in", "hello", chat="main")
    now = time.time() - 3 * 3600
    a, b = obligations.add(p, "first order", now=now), obligations.add(p, "second order", now=now)
    assert hourly.post(p, now) is None, "baseline"
    obligations.record(p, a, "receipt", "commit abc1234", None, now=now + 60)
    first = hourly.post(p, now + hourly.CHECK_S + 1)
    assert first, "the first important change posts"
    assert "(an order closed)" in p.db.one("SELECT text FROM messages WHERE id=?", (first,))["text"]
    obligations.record(p, b, "receipt", "commit def5678", None, now=now + hourly.CHECK_S + 200)
    _block(p, "another")
    assert hourly.post(p, now + hourly.CHECK_S + 301) is None, "5 minutes later: the chat's status gap holds it"
    assert hourly.post(p, now + 2 * hourly.CHECK_S + 2) is None, "still inside the hour"
    second = hourly.post(p, now + hourly.CHECK_S + 3602)
    assert second, "after the gap, the held change posts"
    assert "(an order closed, a blocker appeared)" in p.db.one("SELECT text FROM messages WHERE id=?", (second,))["text"]
    assert p.db.one("SELECT COUNT(*) n FROM messages WHERE ref=?", (hourly.REF,))["n"] == 2


def test_the_table_stays_in_the_web_app_by_default_and_its_test_line_has_no_dollars(env):
    """#8356: without notify.chat_status the hourly table is recorded for the web app only (no chat, the
    quiet channel), with no main chat too; its test runs are CPU work, with no $ figure."""
    from ttp import alerts, hourly
    p = make(env)
    now = time.time() - 3600
    assert hourly.post(p, now - 3700) is None, "baseline"
    _block(p, "first")   # an important change (#8341) makes the table due
    m = p.db.one("SELECT * FROM messages WHERE id=?", (hourly.post(p, now),))
    assert m["chat"] is None and m["channel"] == alerts.QUIET and m["ref"] == hourly.REF
    (tests,) = [r for r in m["text"].split("\n") if r.startswith("Tests in 24 h")]
    runs, _, woken = tests.partition(". Model spend of runs woken")   # only the wakes' model spend has a $
    assert "CPU work, no model spend" in runs and "$" not in runs and woken


def test_a_spill_off_whose_rerun_went_to_another_spill_host_is_not_run_again_here(env):
    from ttp import hourly
    from ttp.prguard import CHECKS_FILE
    p = make(env)
    now = time.time()
    d = p.runs / "r0"
    d.mkdir(parents=True)
    (d / CHECKS_FILE).write_text(json.dumps({"ts": now - 60, "spilled_to": "spill-a", "passed": True,
                                             "spill_off": "spill-b:env.k.1"}))
    p.db.x("INSERT INTO runs(role, status, started, dir) VALUES('worker', 'done', ?, ?)", (now - 120, str(d)))
    assert hourly.spill_use(p.db, p.runs, now - 86400) == {"spill-a": [1, 1, 0], "spill-b": [1, 0, 0]}


def test_a_correction_marks_the_hourly_post_that_stated_the_wrong_value_and_the_next_table_says_so(env):
    from ttp import correction, hourly
    p = make(env)
    p.db.post("in", "hello", chat="main")
    now = time.time() - 3600
    _hourly_run(p, "claude", "disk", now - 60)
    assert hourly.post(p, now - 3700) is None, "baseline"
    _block(p)
    first = hourly.post(p, now)
    assert "below target" in p.db.one("SELECT text FROM messages WHERE id=?", (first,))["text"]
    assert hourly.post(p, now + 30) is None
    assert correction.remember(p.db, "corr-1", "Correction (user msg #42): the fork rate was on target, "
                                               "not below target", now + 40)
    second = hourly.post(p, now + 60)
    assert second, "a new correction posts at once, rows unchanged or not"
    text = p.db.one("SELECT text FROM messages WHERE id=?", (second,))["text"]
    assert "(a correction" in text and "said 'below' in its Fork rate row; user msg #42 corrected it" in text
    mark = hourly.corrected(p)[str(first)]
    assert mark["msg"] == 42 and mark["entry"] == "corr-1" and str(second) not in hourly.corrected(p)
    assert hourly.post(p, now + 120) is None, "one post per correction"


def test_the_table_carries_no_eta_so_an_expired_one_never_shows(env):
    from ttp import hourly
    p = make(env)
    now = time.time()
    from ttp import eta
    from ttp.db import deferral
    t = p.db.add_task("late", "spec")
    # A deferral as the harness stores it (a start_after label and not_before), and a waiting hand-off
    # whose measured ETA eta.py reads: one still ahead an hour ago, expired now.
    p.db.x("UPDATE tasks SET status='waiting', labels=?, not_before=?, result=? WHERE id=?",
           (json.dumps([f"start_after:{now - 7200}"]), now - 7200,
            json.dumps({"status": "waiting", "retry_when": "ttp detach --check run/job", "eta": now - 3600,
                        "eta_basis": "median of earlier runs", "waiting_since": now - 7200,
                        "summary": "ETA 09:00"}), t))
    row = dict(p.db.task(t))
    assert deferral(row)["after"] == now - 7200
    assert eta.outside_wait(row, now - 5400) == {"at": now - 3600, "follows": False}, "eta.py reads this ETA"
    out = "\n".join([*hourly.rows(p, now), hourly.table(p, now)]).lower()
    assert "eta" not in out and "expected finish" not in out


def test_an_unchanged_signature_is_rechecked_at_most_every_check_s(env, monkeypatch):
    from ttp import hourly
    p = make(env)
    p.db.post("in", "hello", chat="main")
    now = time.time() - 3600
    assert hourly.post(p, now - 3700) is None
    calls = []
    real = hourly.important
    monkeypatch.setattr(hourly, "important", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert hourly.post(p, now) is None and len(calls) == 1, "nothing important changed: no post"
    assert hourly.post(p, now + 60) is None and len(calls) == 1, "rechecked only after CHECK_S"
    assert hourly.post(p, now + hourly.CHECK_S + 1) is None and len(calls) == 2


def test_the_change_check_reads_only_orders_blockers_outages_and_the_gate():
    from ttp import hourly
    base = {"open": ["o1", "o2"], "closed": ["o0"], "overdue": [], "blocked": ["#5"], "outages": [],
            "gate": ["claude:green"]}
    assert hourly.changes(base, dict(base)) == []
    assert hourly.changes(base, {**base, "open": ["o2"], "closed": ["o0", "o1"]}) == ["an order closed"]
    assert hourly.changes(base, {**base, "closed": ["o0", "o3"]}) == ["an order closed"], "opened and closed between"
    assert hourly.changes(base, {**base, "closed": []}) == [], "a receipt ageing out is no change"
    assert hourly.changes(base, {**base, "overdue": ["o1"]}) == ["an order went overdue"]
    assert hourly.changes(base, {**base, "blocked": ["#5", "ask 9"]}) == ["a blocker appeared"]
    assert hourly.changes(base, {**base, "blocked": []}) == ["a blocker cleared"]
    assert hourly.changes(base, {**base, "outages": ["box-down"]}) == ["an outage appeared"]
    assert hourly.changes(base, {**base, "gate": ["claude:yellow"]}) == ["the spend gate changed level"]
    assert hourly.changes(base, {**base, "open": ["o1", "o2", "o3"]}) == [], "a new order alone posts nothing"


def test_the_hourly_module_names_no_host():
    """Hosts are named only by their alias from the machines list at run time: no host-like literal ships."""
    import ast
    src = (RUNTIME / "ttp" / "hourly.py").read_text()
    hostish = re.compile(r"\b[a-z]{2,}\d{2,}[a-z0-9]*\b|\b[a-z]+-[a-z]+\d+-[a-z0-9]+\b|\.(?:com|net|local|internal)\b",
                         re.I)
    found = [n.value for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and hostish.search(n.value)]
    assert not found, found


def test_the_tests_row_shows_no_total_spend_and_labels_the_model_spend_of_check_waits_by_reason(env):
    """#8307: tests are CPU work. The row shows check runs and CPU-hours, never the day's total spend, and
    apart, labelled, the runs woken while their task waited on its own checks: wakes, $ and wake reasons."""
    from ttp import hourly, trainstats
    p = make(env)
    now = time.time()
    tid = p.db.add_task("land a change", "s", kind="code", tier="standard", origin="user")
    other = p.db.add_task("unrelated", "s", kind="work", tier="standard", origin="user")
    wait = p.runs / "wait"
    wait.mkdir(parents=True)
    (wait / "result.json").write_text(json.dumps({"status": "waiting", "retry_when": f"ttp checks --result {wait}"}))

    def run(task, started, usd, reason=None, d=None):
        note = {"wake": {"reason": reason}} if reason else {}
        p.db.x("INSERT INTO runs(task, role, status, started, cost_usd, note, dir) VALUES(?, 'worker', 'done', ?, ?, "
               "?, ?)", (task, started, usd, json.dumps(note), str(d or p.runs / f"none-{started}")))
    run(tid, now - 3000, 40.0, d=wait)               # the run that handed off on its checks
    run(tid, now - 2000, 0.5, "timer", d=wait)      # woken while waiting: counted, and waits again
    run(tid, now - 1000, 1.25, "checks_failed")     # woken by the checks' failure: counted
    run(other, now - 900, 100.0, "timer")           # spend unrelated to checks: in no test figure
    rows = trainstats.checkwait(p.db, now - 86400, now)
    assert [(w["reason"], w["usd"]) for w in rows] == [("timer", 0.5), ("checks_failed", 1.25)]
    day = trainstats.report(p.db, p.runs, "UTC", days=1, now=now)["days"][-1]
    assert (day["checkwait_wakes"], day["checkwait_usd"]) == (2, 1.75)
    row = next(r for r in hourly.rows(p, now) if r.startswith("Tests in 24 h"))
    assert row.startswith("Tests in 24 h (CPU work, no model spend): 0 check runs, 0.0 CPU-h; 0 landed"), row
    assert ("Model spend of runs woken while waiting on their checks: 2 wakes, $1.75: "
            "checks_failed 1 $1.25, timer 1 $0.50") in row, row
    assert "141" not in row and "$40" not in row and "$100" not in row
