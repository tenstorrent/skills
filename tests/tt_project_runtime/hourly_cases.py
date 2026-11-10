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
    assert hourly.fork_share(p.db, now - 3600) == {"claude": [2, 4], "codex": [1, 3]}
    row = hourly.rows(p, now)[0]
    assert row.startswith("Fork rate: last hour 3/7 (43%; claude 2/4, codex 1/3)"), row
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


def test_the_table_posts_hourly_or_at_a_milestone_and_only_when_a_row_changed(env):
    from ttp import hourly
    p = make(env)
    p.db.post("in", "hello", chat="main")
    now = time.time() - 3600
    assert hourly.post(p, now - 3700) is None, "the first pass only sets the baseline"
    p.db.add_task("first", "spec")
    first = hourly.post(p, now)
    msg = p.db.one("SELECT * FROM messages WHERE id=?", (first,))
    assert msg["chat"] == "main" and msg["ref"] == hourly.REF and msg["text"].startswith("Status at ")
    assert "(a batch landed)" not in msg["text"] and "Queue: 1 open tasks" in msg["text"]
    assert hourly.post(p, now + 60) is None, "within the hour, no milestone"
    hourly.sample(p.state, now + 3700, load1=3.0, cores=4)
    assert hourly.post(p, now + 3700) is None, "an hour on, only the load moved: no post"
    t = p.db.add_task("next", "spec")
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated) "
           "VALUES(?, 'b', 'h', 'origin/x', 'landed', ?, ?)", (t, now + 100, now + 200))
    second = hourly.post(p, now + 300)
    assert second and "(a batch landed)" in p.db.one("SELECT text FROM messages WHERE id=?", (second,))["text"]
    assert hourly.post(p, now + 400) is None, "one post per milestone"
    assert p.db.one("SELECT COUNT(*) n FROM messages WHERE ref=?", (hourly.REF,))["n"] == 2


def test_the_hourly_module_names_no_host():
    """Hosts are named only by their alias from the machines list at run time: no host-like literal ships."""
    import ast
    src = (RUNTIME / "ttp" / "hourly.py").read_text()
    hostish = re.compile(r"\b[a-z]{2,}\d{2,}[a-z0-9]*\b|\b[a-z]+-[a-z]+\d+-[a-z0-9]+\b|\.(?:com|net|local|internal)\b",
                         re.I)
    found = [n.value for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and hostish.search(n.value)]
    assert not found, found
