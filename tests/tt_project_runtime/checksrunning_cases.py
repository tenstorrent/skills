# Checks-running marker tests (cli.CHECKS_RUNNING) of tests/test_tt_project_runtime.py, which runs this file
# in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file, and
# pytest collects these tests under its name. Add new tests of how a run's checks say they ended here.
from __future__ import annotations

import types


def _ended_checks_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _marker(run) -> dict:
    return json.loads((run / "checks.running").read_text())


def _result(run, capsys) -> tuple[int, str]:
    from ttp import cli
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--result", str(run)])
    return e.value.code, capsys.readouterr().out


def test_a_foreground_checks_marker_whose_process_died_reads_as_ended_incomplete(tmp_path, monkeypatch, capsys):
    """A foreground `ttp checks` killed with its tool call leaves checks.running: the probe passes and says
    the checks did not finish, so the woken task reruns them; the marker is then finish-marked."""
    run = tmp_path / "run-orchid"
    run.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    (run / "checks.running").write_text(json.dumps({"since": time.time(), "pid": _ended_checks_pid(), "started": "1"}))
    code, out = _result(run, capsys)
    assert code == 0, out
    assert "incomplete" in out and "ttp checks --detach" in out, out
    assert _marker(run)["rc"] == "killed" and "ended" in _marker(run)
    # Read again: the same answer, nothing rewritten.
    assert _result(run, capsys)[0] == 0
    # A marker of the old shape ({since, pid}) with a dead pid ends the same way.
    (run / "checks.running").write_text(json.dumps({"since": time.time(), "pid": _ended_checks_pid()}))
    code, out = _result(run, capsys)
    assert code == 0 and "incomplete" in out, out


def test_a_foreground_checks_marker_of_a_live_process_is_not_yet_and_a_reused_pid_is_ended(
        tmp_path, monkeypatch, capsys):
    from ttp.runner import proc_start
    run = tmp_path / "run-heron"
    run.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    me = os.getpid()
    (run / "checks.running").write_text(json.dumps({"since": time.time(), "pid": me, "started": proc_start(me)}))
    code, out = _result(run, capsys)
    assert code == 1 and f"running in the foreground (pid {me})" in out, out
    assert "ended" not in _marker(run)
    # Same pid, another start time: a reused pid, so the recorded run is gone.
    (run / "checks.running").write_text(json.dumps({"since": time.time(), "pid": me, "started": "not-it"}))
    code, out = _result(run, capsys)
    assert code == 0 and "incomplete" in out, out


def test_foreground_checks_finish_mark_their_marker_on_pass_and_fail(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    cli.main(["checks", "--", "true"])
    m = _marker(run)
    assert m["pid"] == os.getpid() and m["rc"] == 0 and m["ended"] >= m["since"] and not m["detached"], m
    code, out = _result(run, capsys)
    assert code == 0 and "finished with exit code 0" in out, out
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--fresh", "--", "false"])
    assert e.value.code != 0
    m = _marker(run)
    assert m["rc"] == e.value.code and "ended" in m, m
    code, out = _result(run, capsys)
    assert code == 0 and f"finished with exit code {e.value.code}" in out, out


def test_a_stopped_foreground_checks_run_records_killed_and_says_how_to_wait(tmp_path, monkeypatch, capsys):
    """A stop signal (the tool call ending) is recorded as killed, never as the checks' own exit code."""
    from ttp import cli
    run = tmp_path / "run-larch"
    run.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run))

    def stopped(a):
        (run / "checks.running").write_text(json.dumps({"since": time.time(), "pid": os.getpid()}))
        cli._CHECK_STOP["stopped"] = 143
        raise SystemExit(143)

    monkeypatch.setattr(cli, "_cmd_checks", stopped)
    monkeypatch.setitem(cli._CHECK_STOP, "stopped", None)
    with pytest.raises(SystemExit):
        cli.cmd_checks(types.SimpleNamespace(result=None, own_group=False))
    assert "ttp checks --result <run dir>" in capsys.readouterr().err
    assert _marker(run)["rc"] == "killed"
    code, out = cli._checks_result(run), capsys.readouterr().out   # _cmd_checks is replaced here
    assert code == 0 and "incomplete" in out and "were stopped" in out, out
    # A marker another process wrote is never finish-marked by this one.
    (run / "checks.running").write_text(json.dumps({"since": time.time(), "pid": _ended_checks_pid()}))
    monkeypatch.setattr(cli, "_cmd_checks", lambda a: None)
    cli.cmd_checks(types.SimpleNamespace(result=None, own_group=False))
    assert "ended" not in _marker(run)


def test_an_older_foreground_marker_never_answers_for_newer_detached_checks(tmp_path, monkeypatch, capsys):
    run = tmp_path / "run-wren"
    run.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    (run / "checks.running").write_text(json.dumps({"since": time.time() - 600, "pid": _ended_checks_pid()}))
    (run / "checks.pid").write_text(json.dumps({"pid": _ended_checks_pid(), "ts": time.time()}))
    (run / "checks.rc").write_text("0\n")
    code, out = _result(run, capsys)
    assert code == 0 and "finished with exit code 0" in out and "incomplete" not in out, out


def test_a_hand_off_waiting_on_the_checks_running_marker_is_refused(env, monkeypatch):
    """`test ! -e <run>/checks.running` never passes once a killed run leaves the marker: refused, with a
    pointer to `ttp checks --result`; that probe itself is stored."""
    p = make(env)
    from ttp import daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    for probe in ("test ! -e /tmp/run-finch/checks.running", "test ! -e '/tmp/run-finch/checks'.runn*",
                  "ttp checks --result /tmp/run-finch"):
        monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({
            "status": "waiting", "summary": "checks running", "waiting_for": "the checks", "retry_after_s": 900,
            "retry_when": probe}))
        tid = p.db.add_task(f"waits on {probe}", "s", kind="work", tier="light", origin="user")
        assert _run_until(d, p, lambda: p.db.one("SELECT id FROM events WHERE kind='task_waiting' AND task=?",
                                                 (tid,)))
        res = json.loads(p.db.task(tid)["result"])
        refused = [e for e in _events(p, tid, "handoff_probe_refused") if "checks.running" in e["text"]]
        if "--result" in probe:
            assert res.get("retry_when") == probe and not refused, res
        else:
            assert "retry_when" not in res and res["retry_when_refused"] == probe, res
            assert len(refused) == 1 and "ttp checks --result" in refused[0]["text"], refused


def _result_json(run, capsys) -> tuple[int, dict]:
    from ttp import cli
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--result", str(run), "--json"])
    return e.value.code, json.loads(capsys.readouterr().out)


def test_checks_result_and_config_show_the_effective_timeouts_unset_and_overridden(env, tmp_path, monkeypatch, capsys):
    """Unset checks timeouts read as the values in force, never {}; a project's own value wins."""
    from ttp import checklane, checkwall, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    (run / "checks.rc").write_text("0\n")
    code, rec = _result_json(run, capsys)
    assert code == 0 and rec["phase"] == "finished" and rec["exit"] == 0, rec
    lim = rec["limits"]
    assert lim["max_wait_s"] == {"s": checklane.MAX_WAIT_S, "from": "default"}, lim
    assert lim["wall_cap_s"]["s"] == checkwall.DEFAULT_S and lim["wall_cap_s"]["from"].startswith("default"), lim
    assert lim["stall_s"]["s"] == checklane.HANG_LOG_MIN_S and lim["stall_s"]["from"].startswith("default"), lim
    code, out = _result(run, capsys)
    assert f"ttp checks: phase finished; limits: max_wait_s {checklane.MAX_WAIT_S:g} s (default)" in out, out
    cli.main(["config", p.name, "checks.max_wait_s"])
    assert json.loads(capsys.readouterr().out) == checklane.MAX_WAIT_S
    p.set_config("checks.max_wait_s", 120)
    p.set_config("checks.wall_cap_s", 600)
    p.set_config("checks.stall_s", 0)
    code, rec = _result_json(run, capsys)
    assert rec["limits"] == {"max_wait_s": {"s": 120.0, "from": "checks.max_wait_s"},
                             "wall_cap_s": {"s": 600.0, "from": "checks.wall_cap_s"},
                             "stall_s": {"s": 0.0, "from": "checks.stall_s"}}, rec
    assert "stall_s 0 s (checks.stall_s) off" in _result(run, capsys)[1]
    cli.main(["config", p.name, "checks.wall_cap_s"])
    assert json.loads(capsys.readouterr().out) == 600


def test_checks_result_says_the_admission_phase_of_a_queued_a_running_and_a_cut_run(env, tmp_path, monkeypatch, capsys):
    from ttp.runner import proc_start
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    me = os.getpid()
    (run / "checks.pid").write_text(json.dumps({"pid": me, "started": proc_start(me), "ts": time.time() - 5}))
    (run / "checks-slot.wait").write_text(json.dumps({"since": time.time(), "pid": me}))
    code, rec = _result_json(run, capsys)
    assert code == 1 and rec["phase"] == "waiting_slot", rec
    assert any("queued for a host-wide checks slot" in line for line in rec["lines"]), rec
    (run / "checks-slot.wait").write_text(json.dumps({"since": time.time(), "pid": me, "suite": str(tmp_path / "lane")}))
    assert _result_json(run, capsys)[1]["phase"] == "waiting_lane"
    (run / "checks-slot.wait").unlink()
    code, out = _result(run, capsys)
    assert code == 1 and f"running (pid {me})" in out and "ttp checks: phase running; limits:" in out, out
    # Stopped at the wall-time cap or a silence limit: a timeout, told apart from a plain failure.
    (run / "checks.rc").write_text("1\n")
    assert _result_json(run, capsys)[1]["phase"] == "finished"
    (run / "checks.json").write_text(json.dumps({"ts": time.time(), "passed": False,
                                                 "timed_out": {"check": "pytest", "cap_s": 600, "how": "checks.wall_cap_s"}}))
    assert _result_json(run, capsys)[1]["phase"] == "cut_wall_cap"
    (run / "checks.json").write_text(json.dumps({"ts": time.time(), "passed": False,
                                                 "timed_out": {"check": "pytest", "silent_s": 900}}))
    assert _result_json(run, capsys)[1]["phase"] == "cut_silent"
