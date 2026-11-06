# Drift tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file and runwatch_cases.py. Add
# new tests of the running-run drift flags and their ladder (drift.py) here.
from __future__ import annotations


def _dr_entry(now, **kw):
    e = {"run": 1, "task": 1, "role": "worker", "title": "t", "started": now - 3600, "active_at": now,
         "recent": [], "fails": {}, "files": [], "last_tool": None,
         "spend": {"run_usd": 0.5, "task_usd": 0.5, "budget_usd": 8.0, "share": 0.0625},
         "wall": {"elapsed_s": 3600, "limit_s": 7200, "share": 0.5}, "git": None}
    e.update(kw)
    return e


def _dr_flags(e, now, named=(), waiting=False, **cfg):
    from ttp import drift
    return drift.detect(e, drift.settings({"coordinator": {"drift": cfg}}), list(named), waiting, now)


def test_drift_repeat_fail_fires_at_three_failures_of_one_command_and_not_at_two():
    now = time.time()
    assert "repeat_fail" in _dr_flags(_dr_entry(now, fail_streak={"pytest -q x": 3, "make": 1}), now)
    assert "repeat_fail" not in _dr_flags(_dr_entry(now, fail_streak={"pytest -q x": 2, "make": 2}), now)
    assert "repeat_fail" not in _dr_flags(_dr_entry(now, fails={"pytest -q x": 3}), now), "fixed since"
    assert "repeat_fail" not in _dr_flags(_dr_entry(now, fail_streak={"pytest -q x": 9}), now, repeat_fail=0), "0: off"


def test_drift_repeat_fail_counts_tries_of_one_command_that_differ_only_in_wrappers(env, tmp_path):
    from ttp import runwatch
    p = make(env)
    run_dir = p.runs / "df1"
    t, rid = _rw_run(p, "claude", run_dir)
    cmds = ["cd /w && pytest -q tests/a.py 2>&1 | tail -20", "ttp clip -- pytest -q tests/a.py",
            "timeout 300 pytest   -q tests/a.py | head -5"]
    (run_dir / "output.jsonl").write_text("".join(_rw_claude_call(f"c{i}", "Bash", command=c)
                                                  + _rw_claude_result(f"c{i}", error=True) for i, c in enumerate(cmds)))
    e = runwatch.tick(p, time.time())["runs"][str(rid)]
    assert e["fails"] == {"pytest -q tests/a.py": 3}
    assert _dr_flags(e, time.time())["repeat_fail"].startswith("`pytest -q tests/a.py` failed 3 times")


def test_drift_loop_fires_on_four_same_inputs_in_the_last_30_calls_only():
    now = time.time()
    call = lambda h: {"id": h, "tool": "Read", "error": False, "h": h, "cmd": ""}   # noqa: E731
    same4 = [call("x")] * 4 + [call(f"o{i}") for i in range(10)]
    assert "loop" in _dr_flags(_dr_entry(now, recent=same4), now)
    assert "loop" not in _dr_flags(_dr_entry(now, recent=[call("x")] * 3 + [call(f"o{i}") for i in range(10)]), now)
    old = [call("x")] + [call(f"o{i}") for i in range(27)] + [call("x")] * 3   # the 4th is 31 calls ago
    assert len(old) == 31 and "loop" not in _dr_flags(_dr_entry(now, recent=old), now)
    assert "loop" not in _dr_flags(_dr_entry(now, recent=same4), now, loop=0)
    failing = [{"id": f"f{i}", "tool": "Bash", "error": True, "h": "m", "cmd": "make"} for i in range(4)]
    flags = _dr_flags(_dr_entry(now, recent=failing, fail_streak={"make": 4}), now)
    assert "repeat_fail" in flags and "loop" not in flags, "one repeated failing command is one flag"
    assert "loop" in _dr_flags(_dr_entry(now, recent=failing, fail_streak={"make": 4}), now, repeat_fail=0)


def test_drift_outside_spec_counts_only_files_outside_named_paths_and_their_folders():
    from ttp import drift
    spec = ("Build plugins/tt-project/runtime/ttp/drift.py beside runwatch.py and one line in "
            "template/prompts/worker.md; see https://example.com/a/b for nothing.")
    named = drift.spec_paths(spec)
    assert "plugins/tt-project/runtime/ttp/drift.py" in named and "template/prompts/worker.md" in named
    assert not any(n.startswith("http") or "example.com" in n for n in named)
    allowed = ["plugins/tt-project/runtime/ttp/daemon.py",            # the named file's folder
               "plugins/tt-project/template/prompts/worker.md",       # a named path's tail
               "plugins/tt-project/template/prompts/coordinator.md",  # its folder, by the tail
               "tests/tt_project_runtime/drift_cases.py", ".changeset/x.md", "plugins/tt-project/docs/guide.md",
               "README.md"]
    out3 = ["plugins/other/a.py", "scripts/b.py", "plugins/tt-project/web/c.js"]
    now = time.time()
    e = _dr_entry(now, files=allowed + out3)
    assert drift.outside(e["files"], named) == out3
    assert "outside_spec" in _dr_flags(e, now, named=named) and e["outside_n"] == 3
    assert "outside_spec" not in _dr_flags(_dr_entry(now, files=allowed + out3[:2]), now, named=named)
    assert "outside_spec" not in _dr_flags(_dr_entry(now, files=out3), now, named=[]), "a spec naming no path"
    bare = ["plugins/tt-project/runtime/ttp/runwatch.py", "plugins/tt-project/runtime/ttp/daemon.py"]
    assert drift.outside(bare + ["web/x.js"], ["runwatch.py"]) == ["web/x.js"], "a bare name: the folder it is in"


def test_drift_outside_spec_judges_only_a_code_workers_own_changes_never_a_reviewer(env, tmp_path):
    from ttp import drift
    p = make(env)
    spec = "Change plugins/tt-project/runtime/ttp/drift.py only."
    out3 = ["plugins/other/a.py", "scripts/b.py", "plugins/tt-project/web/c.js"]

    def flags(kind, role, first, later):
        t = p.db.add_task("t", spec, kind=kind)
        run_dir = tmp_path / f"r{t}"
        run_dir.mkdir()
        rid = p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
                     (t, role, "claude", time.time(), "running", str(run_dir)))
        now = time.time()
        data = {"runs": {str(rid): _dr_entry(now, run=rid, task=t, role=role, files=first)}}
        drift.check(p, p.config(), data, now)
        data["runs"][str(rid)]["files"] = later
        drift.check(p, p.config(), data, now + 60)
        return data["runs"][str(rid)]["flags"]

    assert "outside_spec" in flags("code", "worker", [], out3), "a code worker's own edits"
    assert "outside_spec" not in flags("code", "worker", out3, out3), "changed before the run: inherited"
    assert "outside_spec" not in flags("review", "reviewer", out3, out3)
    assert "outside_spec" not in flags("review", "reviewer", [], out3), "a reviewer edits nothing to judge"
    assert "outside_spec" not in flags("review", "worker", [], out3)


def test_drift_quiet_fires_after_quiet_s_unless_a_lock_checks_or_detach_wait_is_pending():
    now = time.time()
    assert "quiet" in _dr_flags(_dr_entry(now, active_at=now - 900), now)
    assert "quiet" not in _dr_flags(_dr_entry(now, active_at=now - 899), now)
    assert "quiet" not in _dr_flags(_dr_entry(now, active_at=now - 5000), now, waiting=True)
    assert "quiet" not in _dr_flags(_dr_entry(now, active_at=now - 5000, drift={"head_at": now - 60}), now), \
        "a commit counts as activity"
    for cmd in ("ttp lock dev -- make", "ttp checks -- 'pytest -q a.py'", "ttp detach --check --host h d/j"):
        e = _dr_entry(now, active_at=now - 5000,
                      last_tool={"id": "a", "tool": "Bash", "input_head": cmd, "since": now - 5000, "done": False})
        assert "quiet" not in _dr_flags(e, now), cmd
    e = _dr_entry(now, active_at=now - 5000,
                  last_tool={"id": "a", "tool": "Bash", "input_head": "ttp lock dev -- make", "done": True})
    assert "quiet" in _dr_flags(e, now), "a wait that already ended does not count"


def test_drift_pace_fires_on_spend_ahead_of_wall_or_most_of_the_budget_with_no_commit():
    now = time.time()
    spend = lambda run, task: {"run_usd": run * 8, "task_usd": task * 8, "budget_usd": 8.0, "share": task}  # noqa: E731
    wall = lambda share: {"elapsed_s": 1, "limit_s": 2, "share": share}  # noqa: E731
    assert "pace" in _dr_flags(_dr_entry(now, spend=spend(0.5, 0.5), wall=wall(0.33)), now)
    assert "pace" not in _dr_flags(_dr_entry(now, spend=spend(0.5, 0.5), wall=wall(0.34)), now)
    assert "pace" not in _dr_flags(_dr_entry(now, spend=spend(0.49, 0.49), wall=wall(0.01)), now), "under the floor"
    git = lambda c: {"head": "h", "committed": c}  # noqa: E731
    assert "pace" in _dr_flags(_dr_entry(now, spend=spend(0.4, 0.85), wall=wall(0.9), git=git([])), now)
    assert "pace" not in _dr_flags(_dr_entry(now, spend=spend(0.4, 0.85), wall=wall(0.9), git=git(["a.py"])), now)
    assert "pace" not in _dr_flags(_dr_entry(now, spend=spend(0.4, 0.79), wall=wall(0.9), git=git([])), now)
    assert "pace" not in _dr_flags(_dr_entry(now, spend=spend(0.4, 0.85), wall=wall(0.9)), now), "no worktree"


def _dr_project(env, tmp_path, quiet=True, **cost):
    p = make(env)
    if not quiet:   # a ladder test that leaves the run idle for long stretches on purpose
        p.set_config("coordinator.drift.quiet_s", 0)
    run_dir = p.runs / "dl1"
    t, rid = _rw_run(p, "claude", run_dir, **cost)
    return p, run_dir, t, rid


def _dr_tick(p, now):
    from ttp import drift, runwatch
    data = runwatch.tick(p, now, write=False)
    ids = drift.check(p, p.config(), data, now)
    runwatch.write(p, data)
    return data, ids


def test_drift_ladder_steers_once_then_one_ride_along_event_and_never_repeats(env, tmp_path):
    from ttp import pagesev
    p, run_dir, t, rid = _dr_project(env, tmp_path, quiet=False)
    (run_dir / "output.jsonl").write_text("".join(_rw_claude_call(f"c{i}", "Bash", command="make all")
                                                  + _rw_claude_result(f"c{i}", error=True) for i in range(3)))
    now = time.time()
    data, ids = _dr_tick(p, now)
    e = data["runs"][str(rid)]
    assert set(e["flags"]) == {"repeat_fail"} and ids == []
    steer = (run_dir / "steer.md").read_text()
    assert steer.count("## Update") == 1 and "`make all` failed 3 times" in steer and "ttp note" in steer
    assert _dr_tick(p, now + 599)[1] == []
    assert json.loads(runwatch_path(p).read_text())["runs"][str(rid)]["flags"], "flags kept in the record"
    data, ids = _dr_tick(p, now + 600)
    assert len(ids) == 1
    ev = p.db.one("SELECT * FROM events WHERE id=?", (ids[0],))
    assert ev["kind"] == "worker_drift" and ev["task"] == t and pagesev.rides_along(ev)
    assert "`make all` failed 3 times" in ev["text"]
    for later in (1200, 5000):
        assert _dr_tick(p, now + later)[1] == []
    assert (run_dir / "steer.md").read_text().count("## Update") == 1
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind='worker_drift'")["n"] == 1


def test_drift_step_two_skips_a_command_that_works_again_after_the_update(env, tmp_path):
    p, run_dir, t, rid = _dr_project(env, tmp_path, quiet=False)
    out = run_dir / "output.jsonl"
    out.write_text("".join(_rw_claude_call(f"c{i}", "Bash", command="make all")
                           + _rw_claude_result(f"c{i}", error=True) for i in range(3)))
    now = time.time()
    assert set(_dr_tick(p, now)[0]["runs"][str(rid)]["flags"]) == {"repeat_fail"}
    with open(out, "a") as f:
        f.write(_rw_claude_call("ok", "Bash", command="cd /w && make all 2>&1 | tail -5") + _rw_claude_result("ok"))
    data, ids = _dr_tick(p, now + 600)
    e = data["runs"][str(rid)]
    assert ids == [] and e["flags"] == {} and e["fails"] == {"make all": 3} and e["fail_streak"] == {}
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind='worker_drift'")["n"] == 0


def test_drift_step_two_skips_a_flag_the_worker_answered_with_a_note_after_the_update(env, tmp_path):
    p2, run_dir2, t2, rid2 = _dr_project(env, tmp_path, quiet=False)
    now = time.time()
    (run_dir2 / "output.jsonl").write_text("".join(_rw_claude_call(f"c{i}", "Bash", command="make all")
                                                   + _rw_claude_result(f"c{i}", error=True) for i in range(3)))
    (run_dir2 / "progress.md").write_text("17:00 started\n")
    assert "repeat_fail" in _dr_tick(p2, now)[0]["runs"][str(rid2)]["flags"]
    with open(run_dir2 / "progress.md", "a") as f:
        f.write("17:02 make all fails on purpose: the test reproduces the bug before the fix\n")
    data, ids = _dr_tick(p2, now + 600)
    assert "repeat_fail" in data["runs"][str(rid2)]["flags"] and ids == [], "the note answers step 2"


def runwatch_path(p):
    from ttp import runwatch
    return runwatch.path(p)


def test_drift_step_three_wakes_the_coordinator_once_for_six_failures(env, tmp_path):
    from ttp import pagesev
    p, run_dir, t, rid = _dr_project(env, tmp_path, quiet=False)
    out = run_dir / "output.jsonl"
    out.write_text("".join(_rw_claude_call(f"c{i}", "Bash", command="make all")
                           + _rw_claude_result(f"c{i}", error=True) for i in range(3)))
    now = time.time()
    assert _dr_tick(p, now)[1] == []
    with open(out, "a") as f:
        f.write("".join(_rw_claude_call(f"d{i}", "Bash", command="make all")
                        + _rw_claude_result(f"d{i}", error=True) for i in range(3)))
    ids = _dr_tick(p, now + 60)[1]
    assert len(ids) == 1
    ev = p.db.one("SELECT * FROM events WHERE id=?", (ids[0],))
    assert ev["severity"] == "normal" and not pagesev.rides_along(ev), "step 3 wakes the coordinator"
    assert _dr_tick(p, now + 2000)[1] == [], "no ride-along after the wake, and no second wake"
    assert (run_dir / "steer.md").read_text().count("## Update") == 1


def test_drift_a_run_waiting_on_a_lock_is_never_flagged_quiet(env, tmp_path):
    from ttp import locks
    p, run_dir, t, rid = _dr_project(env, tmp_path)
    (run_dir / "output.jsonl").write_text(_rw_claude_call("a", "Bash", command="make") + _rw_claude_result("a"))
    now = time.time()
    _dr_tick(p, now)
    os.utime(run_dir / "progress.md", (now - 9000, now - 9000)) if (run_dir / "progress.md").exists() else None
    locks.record_wait(run_dir, "w1", now, None, ["dev"])
    try:
        for later in (900, 3600, 7200):
            data, ids = _dr_tick(p, now + later)
            assert "quiet" not in data["runs"][str(rid)]["flags"] and ids == []
    finally:
        locks.record_wait(run_dir, "w1", now, now + 7200, ["dev"])
    data, _ = _dr_tick(p, now + 7300)
    assert "quiet" in data["runs"][str(rid)]["flags"], "once the wait ends, the quiet clock counts"


def test_daemon_watch_runs_writes_drift_flags_into_the_record_and_runs_live_shows_them(env, tmp_path, capsys):
    from ttp import cli
    from ttp.daemon import Daemon
    p, run_dir, t, rid = _dr_project(env, tmp_path)
    (run_dir / "output.jsonl").write_text("".join(_rw_claude_call(f"c{i}", "Bash", command="make all")
                                                  + _rw_claude_result(f"c{i}", error=True) for i in range(3)))
    Daemon(p.base).watch_runs()
    e = json.loads((p.state / "runwatch.json").read_text())["runs"][str(rid)]
    assert list(e["flags"]) == ["repeat_fail"] and e["drift"]["fired"]["repeat_fail"]["steered"]
    assert "Drift check" in (run_dir / "steer.md").read_text()
    cli.main(["runs", "live", p.name])
    assert "drift: repeat_fail: `make all` failed 3 times" in capsys.readouterr().out
