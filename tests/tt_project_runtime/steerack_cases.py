# Mid-run update delivery and live read checks (hook.take_update, cli `ttp note` / `ttp steer`,
# steerack.py) of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file. Add new steerack tests here.
from __future__ import annotations


def _steer_run(env, monkeypatch, p, provider, status="running"):
    """A task with a worker run row on `provider` whose run dir is the worker's TTP_RUN_DIR."""
    tid = p.db.add_task("steered task", "s", kind="code", origin="user")
    run = env["tmp"] / f"steer-run-{provider}-{tid}"
    run.mkdir()
    rid = p.db.x("INSERT INTO runs(task,role,provider,status,started,dir) VALUES(?,'worker',?,?,?,?)",
                 (tid, provider, status, time.time(), str(run)))
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.setenv("TTP_TASK", str(tid))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    return tid, rid, run


def _deliver(provider, run, monkeypatch, tmp_path, capsys) -> str:
    """Hand the run's unread update over as `provider` would: Claude Code's PostToolUse hook, Codex's
    hook command from its argv, or (no hook) `ttp note` then `ttp steer`. Returns what the worker saw."""
    from ttp import cli, hook
    if provider == "claude":
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
            {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}})))
        hook.main(["hook", "PostToolUse"])
        return capsys.readouterr().out
    if provider == "codex":
        worker, _, repo = _codex_hooks(monkeypatch, tmp_path)
        cmd = _codex_hook_commands(worker()[1])["PostToolUse"]
        payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"},
                   "tool_response": "ok\n", "cwd": str(repo)}
        out = subprocess.run(cmd, shell=True, input=json.dumps(payload), capture_output=True, text=True,
                             env=dict(os.environ), cwd=str(repo), timeout=60)
        assert out.returncode == 0, out.stderr
        return out.stdout
    cli.main(["note", "step one done"])
    return capsys.readouterr().out


@pytest.mark.parametrize("provider", ["claude", "codex", "cursor"])
def test_a_mid_run_update_reaches_the_worker_on_every_provider_and_marks_it_read(env, monkeypatch, tmp_path,
                                                                                  capsys, provider):
    from ttp import cli, coordinator as coord
    from ttp.hook import OFFSET_FILE, READS_FILE, unread_update
    p = make(env)
    tid, _, run = _steer_run(env, monkeypatch, p, provider)
    coord._append_update(run / "steer.md", "Also say PINEAPPLE.", "k1")
    got = _deliver(provider, run, monkeypatch, tmp_path, capsys)
    assert "PINEAPPLE" in got and f"Update for your task #{tid}" in got, got
    assert int((run / OFFSET_FILE).read_text()) == (run / "steer.md").stat().st_size
    assert unread_update(run)[0] == "" and (run / READS_FILE).read_text().split()[1] == (run / OFFSET_FILE).read_text()
    assert "PINEAPPLE" not in _deliver(provider, run, monkeypatch, tmp_path, capsys), "handed over twice"
    if provider == "cursor":
        assert "step one done" in (run / "progress.md").read_text(), "the note itself is still recorded"
        coord._append_update(run / "steer.md", "Now say MANGO.", "k2")
        cli.main(["steer"])
        out = capsys.readouterr().out
        assert "MANGO" in out and "PINEAPPLE" not in out
        cli.main(["steer"])
        assert capsys.readouterr().out.strip() == "no new update for your task"


def test_ttp_note_prints_no_update_outside_a_worker_run(env, monkeypatch, capsys):
    # A model-free completion command has only the note context (NOTE_RUN_ENV): it must not mark the
    # worker's update read.
    from ttp import cli, coordinator as coord
    from ttp.hook import OFFSET_FILE
    p = make(env)
    _, _, run = _steer_run(env, monkeypatch, p, "cursor")
    coord._append_update(run / "steer.md", "Also say PINEAPPLE.", "k1")
    monkeypatch.delenv("TTP_RUN_DIR")
    monkeypatch.setenv(cli.NOTE_RUN_ENV, str(run))
    cli.main(["note", "on_pass done"])
    assert "PINEAPPLE" not in capsys.readouterr().out and not (run / OFFSET_FILE).exists()


def test_an_unread_update_raises_one_event_after_the_delay_and_none_once_read(env, monkeypatch):
    from ttp import coordinator as coord, steerack
    from ttp.hook import take_update
    p = make(env)
    tid, rid, run = _steer_run(env, monkeypatch, p, "cursor")
    p.set_config("steer.ack_after_s", 120)
    cfg, state = p.config(), {}
    runs = lambda: p.db.q("SELECT * FROM runs WHERE status='running'")
    events = lambda: p.db.q("SELECT * FROM events WHERE kind=?", (steerack.KIND,))
    t0 = time.time()
    assert steerack.check(p.db, cfg, runs(), state, t0) == [], "no update yet"
    coord._append_update(run / "steer.md", "Stop after the build and hand off.", "k1")
    os.utime(run / "steer.md", (t0, t0))
    assert steerack.check(p.db, cfg, runs(), state, t0 + 60) == [], "inside the delay"
    raised = steerack.check(p.db, cfg, runs(), state, t0 + 121)
    assert len(raised) == 1
    ev = events()[0]
    assert ev["status"] == "queued" and ev["task"] == tid and f"#{tid}" in ev["text"]
    assert "Stop after the build" in ev["text"] and f"run {rid}" in ev["text"]
    assert steerack.check(p.db, cfg, runs(), state, t0 + 600) == [], "one event per unread update"
    steerack.check(p.db, cfg, runs(), {}, t0 + 900)   # a daemon restart: the fingerprint still holds it
    assert len(events()) == 1
    assert "Stop after the build" in take_update(run)
    steerack.check(p.db, cfg, runs(), state, t0 + 901)
    assert events()[0]["status"] == "handled", "read before the coordinator took it up"
    # A read update raises nothing, however old.
    state.clear()
    assert steerack.check(p.db, cfg, runs(), state, t0 + 5000) == []
    # A second update, unread, is a new stretch: one more event.
    coord._append_update(run / "steer.md", "Also run the lint.", "k2")
    os.utime(run / "steer.md", (t0 + 5000, t0 + 5000))
    assert len(steerack.check(p.db, cfg, runs(), state, t0 + 5200)) == 1 and len(events()) == 2


def test_no_unread_event_for_an_ended_run_a_coordinator_run_or_when_off(env, monkeypatch):
    from ttp import coordinator as coord, steerack
    p = make(env)
    old = time.time() - 3600
    _, ended, run = _steer_run(env, monkeypatch, p, "cursor", status="ok")
    coord._append_update(run / "steer.md", "Too late.", "k1")
    _, rid, run2 = _steer_run(env, monkeypatch, p, "claude")
    p.db.x("UPDATE runs SET role='coordinator' WHERE id=?", (rid,))
    coord._append_update(run2 / "steer.md", "Not a worker.", "k1")
    for f in (run / "steer.md", run2 / "steer.md"):
        os.utime(f, (old, old))
    all_runs = p.db.q("SELECT * FROM runs")
    running = [r for r in all_runs if r["status"] == "running"]
    assert steerack.check(p.db, p.config(), running, {}) == []
    # Off: nothing even for a running worker with an old unread update.
    _, _, run3 = _steer_run(env, monkeypatch, p, "codex")
    coord._append_update(run3 / "steer.md", "Unread.", "k1")
    os.utime(run3 / "steer.md", (old, old))
    p.set_config("steer.ack_after_s", 0)
    running = p.db.q("SELECT * FROM runs WHERE status='running'")
    assert steerack.check(p.db, p.config(), running, {}) == []
    assert not p.db.q("SELECT id FROM events WHERE kind=?", (steerack.KIND,))


def test_the_daemon_tick_runs_the_unread_update_check(env, monkeypatch):
    from ttp import coordinator as coord, steerack
    from ttp.daemon import Daemon
    p = make(env)
    tid, _, run = _steer_run(env, monkeypatch, p, "codex")
    coord._append_update(run / "steer.md", "Switch to the other approach.", "k1")
    old = time.time() - 3600
    os.utime(run / "steer.md", (old, old))
    d = Daemon(p.base)
    d.check_steer_reads()
    ev = p.db.q("SELECT * FROM events WHERE kind=?", (steerack.KIND,))
    assert len(ev) == 1 and ev[0]["task"] == tid and "Switch to the other approach" in ev[0]["text"]
    import inspect
    assert "self.check_steer_reads" in inspect.getsource(Daemon.tick)
