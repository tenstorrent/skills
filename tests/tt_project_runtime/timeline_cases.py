# Timeline tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file. Add new tests of
# `ttp timeline` here.
from __future__ import annotations


def _task_with_runs(env, created, runs, closed="done"):
    """A task with the given runs in a synthetic project. Each run is a dict:
    {"start": .., "end": .., "jobs": [(name, start, end, rc)], "result": {...}, "wait_event": ts}."""
    p = make(env)
    tid = p.db.add_task("device test", "spec")
    p.db.x("UPDATE tasks SET created=?, updated=?, status=? WHERE id=?", (created, runs[-1]["end"], closed, tid))
    for spec in runs:
        rid = p.db.x("INSERT INTO runs(task,role,provider,started,ended,status) VALUES(?,?,?,?,?,?)",
                     (tid, "worker", "fake", spec["start"], spec["end"], "ok"))
        d = p.runs / str(rid)
        d.mkdir(parents=True)
        reg = []
        for name, start, end, rc in spec.get("jobs", ()):
            (d / f"{name}.started").write_text("")
            os.utime(d / f"{name}.started", (start, start))
            (d / f"{name}.rc").write_text(f"{rc}")
            os.utime(d / f"{name}.rc", (end, end))
            reg.append({"name": name, "rc": str(d / f"{name}.rc"), "started": spec["start"]})
        if reg:
            (d / "detached.json").write_text(json.dumps(reg))
        if spec.get("result") is not None:
            (d / "result.json").write_text(json.dumps(spec["result"]))
        if spec.get("exit") is not None:
            (d / "exit.json").write_text(json.dumps({"started": spec["exit"], "ended": spec["end"], "rc": 0}))
        spec["id"] = rid
        if spec.get("wait_event") is not None:
            p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                   (spec["wait_event"], f"task:{tid}", "task_waiting", "low", "waiting", "handled", tid))
        p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(d), rid))
    return p, tid


def test_timeline_bucket_totals_of_a_task_with_two_runs_and_a_redone_job(env):
    from ttp import timeline
    p, tid = _task_with_runs(env, created=1000, runs=[
        {"start": 1010, "end": 1060, "exit": 1011, "jobs": [("full", 1015, 1040, 0), ("plain", 1041, 1050, 1)],
         "result": {"status": "waiting", "waiting_since": 1060,
                    "retry_when": "ttp detach --check /run1/plain.rc"}},
        {"start": 1120, "end": 1180, "exit": 1121, "jobs": [("plain", 1130, 1170, 0)],
         "result": {"status": "done"}},
    ])
    rep = timeline.task(p, p.db.task(tid), now=1180)
    assert rep["buckets"] == {"queue": 10.0, "start-up": 15.0, "test": 74.0, "wait": 81.0}, rep["buckets"]
    assert rep["wall_s"] == 180.0 and rep["unexplained_s"] == 0.0
    assert rep["failed_redone_s"] == 9.0 and rep["failed_redone_jobs"] == ["plain"]
    assert [s["kind"] for s in rep["steps"]] == ["created", "queued", "run", "job", "idle", "job", "wait", "run", "job"]
    job = next(s for s in rep["steps"] if s.get("name") == "plain" and s["rc"] == "1")
    assert (job["start"], job["end"]) == (1041.0, 1050.0)


def test_timeline_reads_a_remote_job_s_exit_code_from_its_local_mirror_record(env):
    """A remote job has no local files; its exit code and end come from the mirror its check kept."""
    from ttp import timeline
    p = make(env)
    tid = p.db.add_task("device test", "spec")
    p.db.x("UPDATE tasks SET created=2000, updated=2200, status='done' WHERE id=?", (tid,))
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,ended,status) VALUES(?,?,?,?,?,?)",
                 (tid, "worker", "fake", 2010, 2060, "ok"))
    rd = p.runs / str(rid)
    rd.mkdir(parents=True)
    probe = f"ttp detach --check --host a-box --mirror {rd} ~/.ttp-detach/9/load"
    (rd / "result.json").write_text(json.dumps({"status": "waiting", "waiting_since": 2060,
                                                "retry_when": probe}))
    p.db.x("INSERT INTO runs(task,role,provider,started,ended,status) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", 2160, 2200, "ok"))
    rd2 = p.runs / str(rid + 1)
    rd2.mkdir(parents=True)
    (rd2 / "result.json").write_text(json.dumps({"status": "done"}))
    (rd / "load.remote.json").write_text(json.dumps(
        {"host": "a-box", "path": "~/.ttp-detach/9/load", "end": {"rc": 3}, "moved": 2055}))
    (rd / "load.remote.rc").write_text("9")   # only its mtime is read
    os.utime(rd / "load.remote.rc", (2120, 2120))
    for name, ts in (("load.started", 2170), ("load.rc", 2190)):   # the next run redoes it locally
        (rd2 / name).write_text("0")
        os.utime(rd2 / name, (ts, ts))
    (rd2 / "detached.json").write_text(json.dumps([{"name": "load", "rc": str(rd2 / "load.rc"), "started": 2160}]))
    rep = timeline.task(p, p.db.task(tid), now=2200)
    jobs = [s for s in rep["steps"] if s["kind"] == "job"]
    assert [j["state"] for j in jobs][0] == "remote" and jobs[0]["name"] == "load", rep["steps"]
    assert jobs[0]["rc"] == "3" and (jobs[0]["start"], jobs[0]["end"]) == (2060.0, 2120.0)
    assert rep["buckets"] == {"queue": 10.0, "test": 80.0, "wait": 50.0, "model": 50.0, "start-up": 10.0}, rep["buckets"]
    assert rep["failed_redone_jobs"] == ["load"] and rep["failed_redone_s"] == 60.0


def test_timeline_counts_a_failed_job_as_redone_only_when_the_same_job_runs_again(env):
    from ttp import timeline
    p, tid = _task_with_runs(env, created=1000, runs=[
        {"start": 1010, "end": 1060, "jobs": [("setup", 1015, 1030, 2), ("report", 1031, 1040, 0)],
         "result": {"status": "done"}},
    ])
    rep = timeline.task(p, p.db.task(tid), now=1060)
    assert rep["failed_redone_jobs"] == [] and rep["failed_redone_s"] == 0.0


def test_timeline_marks_time_no_record_explains_as_unexplained(env):
    """An open task's tail after its last record: no waiting hand-off, no interval, so unexplained."""
    from ttp import timeline
    p, tid = _task_with_runs(env, created=3000, runs=[
        {"start": 3010, "end": 3020, "result": None, "jobs": []},
    ], closed="queued")
    p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps({"status": "done"}), tid))
    rep = timeline.task(p, p.db.task(tid), now=3050)
    assert rep["buckets"] == {"queue": 10.0, "model": 10.0} and rep["unexplained_s"] == 30.0


def test_timeline_cli_lists_the_steps_in_the_home_zone(env, capsys):
    from ttp import cli
    p, tid = _task_with_runs(env, created=1000, runs=[
        {"start": 1010, "end": 1060, "exit": 1011, "jobs": [("full", 1015, 1040, 0)],
         "result": {"status": "waiting", "waiting_since": 1060, "retry_when": "ttp detach --check /x.rc"}},
        {"start": 1120, "end": 1180, "exit": 1121, "jobs": [], "result": {"status": "done"}},
    ])
    cli.main(["timeline", p.name, str(tid)])
    out = capsys.readouterr().out
    assert "queued 10s (to run 1's dispatch)" in out and "job full" in out and "rc=0" in out, out
    assert "wait 1970-01-01" in out and "buckets:" in out and "queue 10s" in out, out
    cli.main(["timeline", p.name, "--json", str(tid)])
    rep = json.loads(capsys.readouterr().out)
    assert rep["task"] == tid and isinstance(rep["buckets"], dict)


def test_timeline_shows_an_open_wait_as_still_waiting(env, capsys):
    from ttp import cli, timeline
    res = {"status": "waiting", "waiting_since": 4060, "retry_when": "ttp checks --result runs/9",
           "waiting_for": "the checks to pass"}
    p, tid = _task_with_runs(env, created=4000, runs=[
        {"start": 4010, "end": 4060, "exit": 4011, "jobs": [], "result": res}])
    p.db.x("UPDATE tasks SET status='queued', result=?, updated=4100 WHERE id=?", (json.dumps(res), tid))
    rep = timeline.task(p, p.db.task(tid), now=4100)
    assert rep["buckets"] == {"queue": 10.0, "model": 50.0, "wait": 40.0}, rep["buckets"]
    wait = next(s for s in rep["steps"] if s["kind"] == "wait")
    assert wait["end"] is None, "an open wait has no wake yet"
    cli.main(["timeline", p.name, str(tid)])
    out = capsys.readouterr().out
    assert "still waiting" in out and "the checks to pass" in out, out


def test_timeline_keeps_a_sleeping_task_s_wait_open_to_now(env, capsys):
    """A task asleep on its retry_when is status 'queued' with a waiting result (the daemon never
    writes status 'waiting'); its wait runs to now, and row['updated'] is never a time source."""
    from ttp import cli, timeline
    res = {"status": "waiting", "waiting_since": 8060, "retry_when": "ttp checks --result runs/9",
           "waiting_for": "the checks to pass"}
    p, tid = _task_with_runs(env, created=8000, closed="queued", runs=[
        {"start": 8010, "end": 8060, "exit": 8011, "jobs": [], "result": res}])
    p.db.x("UPDATE tasks SET result=?, updated=9000 WHERE id=?", (json.dumps(res), tid))
    rep = timeline.task(p, p.db.task(tid), now=11660)
    assert rep["buckets"] == {"queue": 10.0, "model": 50.0, "wait": 3600.0}, rep["buckets"]
    assert [s["kind"] for s in rep["steps"]] == ["created", "queued", "run", "wait"]
    wait = rep["steps"][-1]
    assert wait["end"] is None and wait["start"] == 8060.0
    cli.main(["timeline", p.name, str(tid)])
    assert "still waiting" in capsys.readouterr().out
    # the same hand-off on a blocked task: status 'blocked' also keeps its last wait open
    res2 = dict(res, waiting_since=8560)
    p, tid = _task_with_runs(env, created=8500, closed="blocked", runs=[
        {"start": 8510, "end": 8560, "exit": 8511, "jobs": [], "result": res2}])
    p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps(res2), tid))
    rep = timeline.task(p, p.db.task(tid), now=8810)
    wait = next(s for s in rep["steps"] if s["kind"] == "wait")
    assert wait["end"] is None and rep["buckets"]["wait"] == 250.0, rep["buckets"]


def test_remote_waits_parse_the_probe_and_fall_back_to_the_wait_span(env):
    from ttp import timeline
    assert timeline.remote_waits("ttp detach --check --host a-box ~/.ttp-detach/9/load.rc") == [
        {"host": "a-box", "mirror": None, "path": "~/.ttp-detach/9/load.rc", "name": "load"}]
    got = timeline.remote_waits(
        "ttp detach --check --host=a-box --mirror=runs/11 --rows f:1 --after 2 ~/.ttp-detach/9/load ~/.ttp-detach/9/lint")
    assert [(g["path"], g["name"], g["mirror"]) for g in got] == [
        ("~/.ttp-detach/9/load", "load", "runs/11"), ("~/.ttp-detach/9/lint", "lint", "runs/11")], got
    assert timeline.remote_waits("ttp checks --result runs/9") == []
    assert timeline.remote_waits("ttp detach --check /local.rc") == []
    # without a mirror record, the wait's span stands for the job
    p2, tid = _task_with_runs(env, created=5000, runs=[
        {"start": 5010, "end": 5060, "exit": 5011, "jobs": [],
         "result": {"status": "waiting",
                    "retry_when": "ttp detach --check --host a-box ~/.ttp-detach/9/load ~/.ttp-detach/9/lint"},
         "wait_event": 5060},
        {"start": 5160, "end": 5200, "exit": 5161, "jobs": [], "result": {"status": "done"}},
    ])
    rep = timeline.task(p2, p2.db.task(tid), now=5200)
    jobs = [s for s in rep["steps"] if s["kind"] == "job"]
    assert [j["name"] for j in jobs] == ["load", "lint"], "every polled path is a step"
    assert all((j["start"], j["end"], j["rc"]) == (5060.0, 5160.0, None) for j in jobs), jobs
    assert rep["buckets"]["test"] == 100.0


def test_timeline_lists_idle_gaps_between_jobs(env, capsys):
    """The idle gaps between one run's jobs; the requeue inference is gone, so the gap between two
    runs with no waiting hand-off is the documented unexplained tail, not a queue interval."""
    from ttp import cli, timeline
    p, tid = _task_with_runs(env, created=6000, runs=[
        {"start": 6010, "end": 6100, "exit": 6011,
         "jobs": [("a", 6020, 6040, 0), ("b", 6060, 6090, 0)], "result": {"status": "done"}},
        {"start": 6200, "end": 6230, "exit": 6201, "jobs": [], "result": {"status": "done"}},
    ])
    rep = timeline.task(p, p.db.task(tid), now=6230)
    kinds = [s["kind"] for s in rep["steps"]]
    assert kinds == ["created", "queued", "run", "job", "idle", "job", "run"], kinds
    idle = rep["steps"][4]
    assert (idle["start"], idle["end"]) == (6040.0, 6060.0)
    assert rep["buckets"] == {"queue": 10.0, "start-up": 10.0, "test": 50.0, "wait": 30.0, "model": 30.0}, rep["buckets"]
    assert rep["unexplained_s"] == 100.0
    cli.main(["timeline", p.name, str(tid)])
    out = capsys.readouterr().out
    assert "idle between jobs 20s" in out, out


def test_timeline_counts_queued_time_as_queue_not_unexplained(env, capsys):
    from ttp import cli, timeline
    p, tid = _task_with_runs(env, created=7000, runs=[{"start": 7010, "end": 7020}])
    p.db.x("DELETE FROM runs WHERE task=?", (tid,))   # never ran
    p.db.x("UPDATE tasks SET status='queued', updated=7000 WHERE id=?", (tid,))
    rep = timeline.task(p, p.db.task(tid), now=7090)
    assert rep["buckets"] == {"queue": 90.0} and rep["unexplained_s"] == 0.0 and rep["wall_s"] == 90.0
    cli.main(["timeline", p.name, str(tid)])
    assert "still queued" in capsys.readouterr().out


def test_timeline_leaves_a_run_s_tail_unexplained_when_nothing_records_it(env):
    """A queued task with no waiting hand-off after its last run: the tail is unexplained, never a
    queue interval (the requeue inference is gone)."""
    from ttp import timeline
    p, tid = _task_with_runs(env, created=7100, runs=[
        {"start": 7110, "end": 7160, "jobs": [("a", 7120, 7150, 0)], "result": {"status": "done"}}],
        closed="queued")
    p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps({"status": "done"}), tid))
    rep = timeline.task(p, p.db.task(tid), now=7260)
    assert rep["buckets"] == {"queue": 10.0, "start-up": 10.0, "test": 30.0, "wait": 10.0}, rep["buckets"]
    assert rep["unexplained_s"] == 100.0 and "requeue" not in [s["kind"] for s in rep["steps"]]


def test_paint_takes_the_causes_rank_from_its_caller():
    from ttp import landings
    ivs = [(0.0, 10.0, "wait"), (4.0, 6.0, "test")]
    assert landings.paint(ivs, 0.0, 10.0, 0.0, {"test": 0, "wait": 1, "unknown": 2}) == [
        (0.0, 4.0, "wait"), (4.0, 6.0, "test"), (6.0, 10.0, "wait")]
    assert landings.paint(ivs, 0.0, 12.0, 0.0, {"wait": 0, "test": 1, "unknown": 2})[-1] == (10.0, 12.0, "unknown")
