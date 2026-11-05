# Runwatch tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file. Add new tests of the live
# run record (runwatch.py, provider `activity`, `ttp runs live`) here.
from __future__ import annotations


def _rw_line(obj) -> str:
    return json.dumps(obj) + "\n"


def _rw_claude_call(cid, name, **inp):
    return _rw_line({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": cid, "name": name,
                                                                    "input": inp}]}})


def _rw_claude_result(cid, error=False):
    return _rw_line({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": cid,
                                                               "is_error": error, "content": "x"}]}})


def _rw_codex(ev, iid, **item):
    return _rw_line({"type": ev, "item": {"id": iid, **item}})


def _rw_repo(tmp_path) -> pathlib.Path:
    """A worktree on a branch off main with one commit and one file written only by the shell."""
    wt = tmp_path / "wt"
    wt.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True)   # noqa: E731
    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    git("checkout", "-q", "-b", "work")
    (wt / "committed.py").write_text("x = 1\n")
    git("add", "committed.py")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "work")
    subprocess.run(f"cat > {wt}/heredoc.py <<'EOF'\ny = 2\nEOF", shell=True, check=True)
    return wt


def _rw_run(p, provider, run_dir, cwd=None, role="worker", budget=4.0, cost=1.0):
    t = p.db.add_task("watched work", "s", kind="code")
    p.db.x("UPDATE tasks SET budget_usd=?, attempts=2 WHERE id=?", (budget, t))
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"cwd": str(cwd or ""), "timeout_s": 3600}))
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,cost_usd) VALUES(?,?,?,?,?,?,?)",
                 (t, role, provider, time.time() - 600, "running", str(run_dir), cost))
    return t, rid


def test_claude_activity_reads_whole_lines_from_an_offset_and_marks_failed_results(tmp_path):
    from ttp.providers.claude import Claude
    out = tmp_path / "output.jsonl"
    text = (_rw_line({"type": "system", "subtype": "init"}) + _rw_claude_call("a", "Bash", command="make " + "x" * 200)
            + _rw_claude_result("a", error=True) + _rw_claude_call("b", "Edit", file_path="/w/f.py", old_string="q"))
    partial = _rw_claude_result("b")
    out.write_text(text + partial[:20])
    events, off = Claude().activity(out, 0)
    assert [(e["kind"], e["id"], e["tool"], e["is_error"]) for e in events] == [
        ("call", "a", "Bash", None), ("result", "a", "", True), ("call", "b", "Edit", None)]
    assert events[0]["input_head"] == ("make " + "x" * 200)[:80] and events[2]["input_head"] == "/w/f.py"
    assert off == len(text.encode()), "a line still being written is left for the next read"
    out.write_text(text + partial)
    events, off2 = Claude().activity(out, off)
    assert [(e["kind"], e["id"], e["is_error"]) for e in events] == [("result", "b", False)]
    assert off2 == out.stat().st_size and Claude().activity(out, off2) == ([], off2)
    assert Claude().activity(tmp_path / "gone.jsonl", 0) == ([], 0)


def test_codex_activity_reads_items_and_counts_a_non_zero_exit_as_failed(tmp_path):
    from ttp.providers.codex import Codex
    out = tmp_path / "output.jsonl"
    out.write_text(_rw_line({"type": "thread.started", "thread_id": "t"})
                   + _rw_codex("item.started", "i1", type="command_execution", command="bash -lc 'pytest -q'",
                               status="in_progress")
                   + _rw_codex("item.completed", "i1", type="command_execution", command="bash -lc 'pytest -q'",
                               exit_code=1, status="failed")
                   + _rw_codex("item.completed", "i2", type="agent_message", text="hi")
                   + _rw_codex("item.completed", "i3", item_type="file_change",
                               changes=[{"path": "/w/a.py", "kind": "update"}], status="completed"))
    events, off = Codex().activity(out, 0)
    assert [(e["kind"], e["id"], e["tool"], e["is_error"]) for e in events] == [
        ("call", "i1", "command_execution", None), ("result", "i1", "command_execution", True),
        ("result", "i3", "file_change", False)]
    assert events[2]["input_head"] == "/w/a.py" and off == out.stat().st_size


def test_runwatch_records_a_claude_run_live_incrementally_with_git_files_checks_and_steer(env, tmp_path):
    from ttp import runwatch
    p = make(env)
    p.set_config("delivery.base_ref", "main")
    wt = _rw_repo(tmp_path)
    run_dir = p.runs / "rw1"
    t, rid = _rw_run(p, "claude", run_dir, cwd=wt)
    out = run_dir / "output.jsonl"
    out.write_text(_rw_claude_call("a", "Bash", command="pytest -q") + _rw_claude_result("a", error=True)
                   + _rw_claude_call("b", "Bash", command="cat > heredoc.py <<'EOF'"))
    (run_dir / "progress.md").write_text("10:00:00 PST building the parser\n10:05:00 PST checks: queued\n")
    (run_dir / "steer.md").write_text("\n## Update Thu 10:06 PST\nstop and commit\n")
    now = time.time()
    e = runwatch.tick(p, now)["runs"][str(rid)]
    assert e["task"] == t and e["attempt"] == 2 and e["role"] == "worker"
    assert e["note"].endswith("building the parser") and e["checks"].endswith("checks: queued")
    assert e["last_tool"]["tool"] == "Bash" and not e["last_tool"]["done"] and e["pending_s"] == 0
    assert e["failed_last_20"] == 1 and e["offset"] == out.stat().st_size
    assert e["files"] == ["committed.py", "heredoc.py"], "a shell-only edit is found through git"
    assert e["spend"] == {"run_usd": 1.0, "task_usd": 1.0, "budget_usd": 4.0, "share": 0.25}
    assert e["wall"]["limit_s"] == 3600 and 0.15 < e["wall"]["share"] < 0.2
    assert e["steer"]["last"].startswith("Update") and e["steer"]["read"] is False
    assert "stop and commit" in e["steer"]["text"]
    assert "running" in runwatch.doing(e) and "cat > heredoc.py" in runwatch.doing(e)
    # Next tick: only the new bytes are read; the pending call ends; the steer update was read.
    out.write_text(out.read_text() + _rw_claude_result("b"))
    (run_dir / "steer.offset").write_text(str((run_dir / "steer.md").stat().st_size))
    (wt / "later.py").write_text("z = 3\n")
    e2 = runwatch.tick(p, now + 90)["runs"][str(rid)]
    assert e2["last_tool"]["done"] and e2["pending_s"] is None and e2["failed_last_20"] == 1
    assert [c["id"] for c in e2["recent"]] == ["a", "b"] and e2["offset"] == out.stat().st_size
    assert e2["steer"]["read"] is True
    assert e2["files"] == ["committed.py", "heredoc.py"], "cached by HEAD and the index mtime within the TTL"
    e3 = runwatch.tick(p, now + 90 + runwatch.GIT_TTL_S)["runs"][str(rid)]
    assert "later.py" in e3["files"] and e3["files_n"] == 3
    # A run that ended leaves the record.
    p.db.x("UPDATE runs SET status='ok' WHERE id=?", (rid,))
    assert runwatch.tick(p)["runs"] == {} and runwatch.load(p)["runs"] == {}


def test_runwatch_records_a_codex_run_and_leaves_its_steer_read_state_unknown(env, tmp_path):
    from ttp import runwatch
    p = make(env)
    run_dir = p.runs / "rw2"
    _, rid = _rw_run(p, "codex", run_dir)
    (run_dir / "output.jsonl").write_text(
        _rw_codex("item.started", "i1", type="command_execution", command="make", status="in_progress")
        + _rw_codex("item.completed", "i1", type="command_execution", command="make", exit_code=2))
    (run_dir / "steer.md").write_text("\n## Update Thu 10:06 PST\nnew order\n")
    e = runwatch.tick(p)["runs"][str(rid)]
    assert e["failed_last_20"] == 1 and e["last_tool"]["is_error"] and "(failed)" in runwatch.doing(e)
    assert e["steer"]["read"] is None, "Codex has no update hook: it reads steer.md itself"
    assert e["files"] == [] and e["note"] == ""
    # A broken line and a result whose call came before the record started: counted, never the last call.
    with open(run_dir / "output.jsonl", "a") as f:
        f.write('{"type": "item.completed", "item": {"type": "command_execution" BROKEN\n')
        f.write(_rw_codex("item.started", "i2", type="command_execution", command="ls", status="in_progress"))
    e = runwatch.tick(p)["runs"][str(rid)]
    assert e["last_tool"]["id"] == "i2" and not e["last_tool"]["done"] and e["failed_last_20"] == 1


def test_runwatch_keeps_the_last_call_when_a_result_arrives_for_a_call_it_never_saw(env, tmp_path):
    from ttp import runwatch
    p = make(env)
    run_dir = p.runs / "rw8"
    _, rid = _rw_run(p, "claude", run_dir)
    out = run_dir / "output.jsonl"
    out.write_text(_rw_claude_call("a", "Read", file_path="/w/x.py"))
    runwatch.tick(p)
    out.write_text(out.read_text() + _rw_claude_result("zz", error=True))
    e = runwatch.tick(p)["runs"][str(rid)]
    assert e["last_tool"]["id"] == "a" and e["failed_last_20"] == 1
    out.write_text(_rw_claude_call("n", "Grep", pattern="def"))   # a shorter stream: a restarted run
    e = runwatch.tick(p)["runs"][str(rid)]
    assert e["last_tool"]["id"] == "n" and [c["id"] for c in e["recent"]] == ["n"] and e["failed_last_20"] == 0


def test_runwatch_falls_back_to_progress_and_git_for_a_provider_without_activity(env, tmp_path):
    from ttp import runwatch
    p = make(env)
    p.set_config("delivery.base_ref", "main")
    wt = _rw_repo(tmp_path)
    run_dir = p.runs / "rw3"
    _, rid = _rw_run(p, "fake", run_dir, cwd=wt, budget=None)
    (run_dir / "output.jsonl").write_text(_rw_claude_call("a", "Bash", command="ls"))
    (run_dir / "progress.md").write_text("10:00:00 PST writing tests\n")
    e = runwatch.tick(p)["runs"][str(rid)]
    assert e["last_tool"] is None and e["recent"] == [] and e["failed_last_20"] == 0
    assert e["note"].endswith("writing tests") and e["files"] == ["committed.py", "heredoc.py"]
    assert e["spend"]["share"] is None and e["steer"] is None


def test_runwatch_marks_a_vanished_run_dir_and_skips_the_coordinator(env, tmp_path):
    from ttp import runwatch
    p = make(env)
    _, gone = _rw_run(p, "claude", p.runs / "rw4")
    shutil.rmtree(p.runs / "rw4")
    _, coord = _rw_run(p, "claude", p.runs / "rw5", role="coordinator")
    runs = runwatch.tick(p)["runs"]
    assert runs[str(gone)]["gone"] is True and str(coord) not in runs
    assert "run dir gone" in runwatch.text({"runs": runs})


def test_runwatch_incremental_read_of_a_2mb_stream_stays_under_50ms(env, tmp_path):
    from ttp import runwatch
    p = make(env)
    run_dir = p.runs / "rw6"
    _, rid = _rw_run(p, "claude", run_dir)
    out = run_dir / "output.jsonl"
    chunk = "".join(_rw_claude_call(f"c{i}", "Bash", command="echo " + "y" * 400) + _rw_claude_result(f"c{i}")
                     + _rw_line({"type": "assistant", "message": {"content": [{"type": "text", "text": "z" * 2000}]}})
                     for i in range(800))
    out.write_text(chunk)
    assert out.stat().st_size > 2_000_000
    now = time.time()
    runwatch.tick(p, now)
    with open(out, "a") as f:
        f.write(_rw_claude_call("last", "Bash", command="pytest"))
    old = runwatch.load(p)["runs"]
    best = min(_rw_timed(lambda: runwatch._tools("claude", out, old[str(rid)], now + 60)) for _ in range(3))
    assert best < 0.05, f"incremental read took {best:.3f} s"
    assert runwatch.tick(p, now + 60)["runs"][str(rid)]["last_tool"]["id"] == "last"


def _rw_timed(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def test_runs_live_command_and_web_view_read_the_record(env, tmp_path, capsys):
    from ttp import cli
    from ttp.daemon import Daemon
    from ttp.web import health
    p = make(env)
    run_dir = p.runs / "rw7"
    t, rid = _rw_run(p, "claude", run_dir)
    (run_dir / "output.jsonl").write_text(_rw_claude_call("a", "Bash", command="make all"))
    (run_dir / "progress.md").write_text("10:00:00 PST compiling\n")
    d = Daemon(p.base)
    d.watch_runs()
    assert str(rid) in json.loads((p.state / "runwatch.json").read_text())["runs"]
    cli.main(["runs", "live", p.name, "--task", f"#{t}", "--json"])
    got = json.loads(capsys.readouterr().out)["runs"]
    assert [e["run"] for e in got] == [rid] and got[0]["last_tool"]["input_head"] == "make all"
    cli.main(["runs", "live", p.name])
    text = capsys.readouterr().out
    assert f"run {rid} #{t}" in text and "doing: Bash: make all" in text and "note:" in text
    cli.main(["runs", "live", p.name, "--task", "999999"])
    assert "no running worker" in capsys.readouterr().out
    w = next(w for w in health(p, p.db)["working"] if w["run"] == rid)
    assert w["doing"].startswith("Bash: make all") and w["note"].endswith("compiling") and "dir" not in w
