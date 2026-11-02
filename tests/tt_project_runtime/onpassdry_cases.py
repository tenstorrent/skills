# on_pass_cmd dry-run (onpassdry.py) tests of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there). Add new on_pass_cmd dry-run tests here, not there.
from __future__ import annotations

from ttp.db import load_result


def _hand_off_waiting(env, p, d, monkeypatch, cmd, title="waits on its job", probe="false"):
    """A fake worker run of a new task that hands off `waiting` with on_pass_cmd `cmd`; the task's row."""
    monkeypatch.setenv("TTP_FAKE_SESSION", "s1")
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({
        "status": "waiting", "summary": "job runs", "waiting_for": "its job", "retry_when": probe,
        "retry_after_s": 600, "on_pass_cmd": cmd, "on_pass": {"status": "done", "summary": "job passed"}}))
    tid = p.db.add_task(title, "run the job", kind="work", tier="light", origin="user")
    d.dispatch()
    _finish_runs(p, d)
    return p.db.task(tid)


def _last_run(p, tid):
    return p.db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))


def test_an_on_pass_cmd_whose_dry_run_crashes_hands_the_run_back_to_its_session_once(env, monkeypatch):
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    t = _hand_off_waiting(env, p, d, monkeypatch, "python3 -c 'import no_such_mod_xyz'")
    res = load_result(t["result"])
    assert t["status"] == "queued" and (t["not_before"] or 0) <= time.time() + 1, dict(t)
    assert res[onpassdry.KEY]["session"] == "s1" and "waiting_since" not in res
    assert "ModuleNotFoundError" in res["woke"] and "no_such_mod_xyz" in res["woke"]
    first = _last_run(p, t["id"])
    got = json.loads((pathlib.Path(first["dir"]) / onpassdry.FILE).read_text())
    assert got["verdict"] == "crash" and got["rc"] == 1 and got["session"] == "s1"
    back = d._resumable(t, "fake")
    assert back["cause"] == "dry_run" and back["session"] == "s1" and back["run"] == first["id"]
    # Its next run continues the same session and is told why its hand-off was not taken.
    d.dispatch()
    run = _last_run(p, t["id"])
    assert run["id"] != first["id"]
    run_dir = pathlib.Path(run["dir"])
    assert json.loads((run_dir / "run.json").read_text())["argv"][-2:] == ["--resume", "s1"]
    prompt = (run_dir / "prompt.md").read_text()
    assert "was not taken" in prompt and "ModuleNotFoundError" in prompt and "do not start them again" in prompt
    _finish_runs(p, d)
    # The handed-back run hands off the same: once per hand-off, so it stands now with a warning.
    t = p.db.task(t["id"])
    res = load_result(t["result"])
    assert t["status"] == "queued" and res.get("waiting_since"), dict(t)
    assert onpassdry.KEY not in res and res[onpassdry.NOTE_KEY].startswith("WARNING: on_pass_cmd dry run crashed")
    assert res["summary"].startswith("WARNING: on_pass_cmd dry run crashed (exit 1)"), res["summary"]
    assert "WARNING: on_pass_cmd dry run crashed" in (run_dir / "progress.md").read_text()
    assert d._resumable(t, "fake") is None


def test_a_crashing_dry_run_with_no_session_to_continue_keeps_the_hand_off_with_a_warning(env, monkeypatch):
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    monkeypatch.delenv("TTP_FAKE_SESSIONS", raising=False)
    d = Daemon(p.base)
    t = _hand_off_waiting(env, p, d, monkeypatch, "exit 127")
    res = load_result(t["result"])
    assert t["status"] == "queued" and res.get("waiting_since") and onpassdry.KEY not in res
    assert res[onpassdry.NOTE_KEY].startswith("WARNING: on_pass_cmd dry run crashed (exit 127)")
    assert res["summary"].startswith("WARNING:") and "job runs" in res["summary"]
    assert (t["not_before"] or 0) > time.time() + 300, "the hand-off's retry_after_s stands"


def test_a_dry_run_that_exits_early_on_its_variable_leaves_the_hand_off_as_is(env, monkeypatch):
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    cmd = '[ "$TTP_ON_PASS_DRY_RUN" = 1 ] && exit 0; exit 1'
    t = _hand_off_waiting(env, p, d, monkeypatch, cmd)
    res = load_result(t["result"])
    assert onpassdry.KEY not in res and onpassdry.NOTE_KEY not in res and res.get("waiting_since")
    assert res["summary"] == "job runs"
    got = json.loads((pathlib.Path(_last_run(p, t["id"])["dir"]) / onpassdry.FILE).read_text())
    assert got["verdict"] == "ok" and got["rc"] == 0 and got["cmd"] == cmd


def test_a_dry_run_that_fails_or_times_out_is_inconclusive_and_the_hand_off_stands(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    t = _hand_off_waiting(env, p, d, monkeypatch, "exit 1")
    res = load_result(t["result"])
    assert res[onpassdry.NOTE_KEY] == "dry run inconclusive (exit 1)" and onpassdry.KEY not in res
    assert res.get("waiting_since") and res["summary"] == "job runs"
    # A timeout kills the command's whole group and is no crash.
    got = onpassdry.run("sleep 5", tmp_path, dict(os.environ), timeout=0.5)
    assert got["rc"] is None and got["verdict"] == "inconclusive" and got["took_s"] < 4
    assert onpassdry.inconclusive(got).startswith("dry run inconclusive (timed out after")
    # Crash signatures: output, or a shell's own exit codes; any other exit is not one.
    assert onpassdry.classify(0, "Traceback (most recent call last):\n") == "crash"
    assert onpassdry.classify(1, "sh: 1: nope: command not found") == "crash"
    assert onpassdry.classify(2, "") == "crash" and onpassdry.classify(126, "") == "crash"
    assert onpassdry.classify(3, "checks failed") == "inconclusive" and onpassdry.classify(0, "ok") == "ok"


def test_the_dry_run_can_be_switched_off(env, monkeypatch):
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    d.cfg["waiting"]["on_pass_dry_run"] = False
    t = _hand_off_waiting(env, p, d, monkeypatch, "python3 -c 'import no_such_mod_xyz'")
    res = load_result(t["result"])
    assert not (pathlib.Path(_last_run(p, t["id"])["dir"]) / onpassdry.FILE).exists()
    assert onpassdry.KEY not in res and onpassdry.NOTE_KEY not in res and res.get("waiting_since")


def test_under_a_dry_run_ttp_note_notify_and_push_do_nothing_and_lock_is_refused(env, tmp_path):
    p = make(env)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    e = {"TTP_ON_PASS_DRY_RUN": "1", "TTP_RUN_DIR": str(run_dir)}
    flag = tmp_path / "ran"
    calls = (("note", "x"), ("note", "--to", "other", "x"), ("notify", "x"), ("push",), ("land", "HEAD"),
             ("lock", "r", "--", "touch", str(flag)), ("devq", "submit", "board", "--", "touch", str(flag)),
             ("devq", "start", "board"), ("devq", "clear", "board"), ("detach", "job", "--", "touch", str(flag)))
    got = [_ttp_run(p, *args, env=e) for args in calls]
    assert [x.returncode for x in got] == [0, 0, 0, 0, 0, 75, 75, 75, 75, 75], \
        [(x.returncode, x.stderr) for x in got]
    assert not flag.exists() and not list(run_dir.glob("job*"))
    # Reading a probe is no side effect: `detach --check` still answers (4: the job is not there).
    assert _ttp_run(p, "detach", "--check", str(tmp_path / "none.rc"), env=e).returncode == 4
    assert all("dry run" in x.stderr for x in got)
    assert not (run_dir / "progress.md").exists()
    assert not p.db.q("SELECT id FROM events WHERE kind LIKE 'notif%'")
    # Without the variable, `ttp note` writes as before.
    assert _ttp_run(p, "note", "x", env={"TTP_RUN_DIR": str(run_dir)}).returncode == 0
    assert (run_dir / "progress.md").exists()


def test_an_on_pass_cmd_that_exits_0_without_writing_ttp_result_is_handed_back_at_hand_off(env, monkeypatch):
    # A passed probe of such a command never records done (it wakes a model run): caught when its run hands off.
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    t = _hand_off_waiting(env, p, d, monkeypatch, "echo checked")
    res = load_result(t["result"])
    assert t["status"] == "queued" and res[onpassdry.KEY]["session"] == "s1" and "waiting_since" not in res
    assert res[onpassdry.KEY]["no_result"] and "without writing a hand-off to $TTP_RESULT" in res["woke"]
    got = json.loads((pathlib.Path(_last_run(p, t["id"])["dir"]) / onpassdry.FILE).read_text())
    assert got["verdict"] == onpassdry.NO_RESULT and got["rc"] == 0 and got["wrote"] is None
    assert d._resumable(t, "fake")["cause"] == "dry_run"
    # The handed-back run hands off the same: it stands now, with the reason in its summary.
    d.dispatch()
    _finish_runs(p, d)
    res = load_result(p.db.task(t["id"])["result"])
    assert res.get("waiting_since") and onpassdry.KEY not in res
    assert res["summary"].startswith("WARNING: on_pass_cmd dry run: it exited 0 without writing"), res["summary"]


def test_a_valid_on_pass_cmd_passes_its_dry_run_and_still_records_done_model_free(env, monkeypatch):
    p = make(env)
    from ttp import onpassdry, daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    _sessions(env, monkeypatch, "s1")
    d = dmod.Daemon(p.base)
    cmd = 'printf \'{"status":"done","summary":"job verified"}\' > "$TTP_RESULT"'
    t = _hand_off_waiting(env, p, d, monkeypatch, cmd, probe="true")
    res = load_result(t["result"])
    assert onpassdry.KEY not in res and onpassdry.NOTE_KEY not in res and res.get("waiting_since")
    got = json.loads((pathlib.Path(_last_run(p, t["id"])["dir"]) / onpassdry.FILE).read_text())
    assert got["verdict"] == "ok" and got["wrote"] == {"status": "done", "keys": ["status", "summary"]}
    _settle_on_pass(d, t["id"])
    task = p.db.task(t["id"])
    res = load_result(task["result"])
    assert task["status"] == "done" and res["run_status"] == "model-free" and res["summary"] == "job verified"
    assert len(p.db.q("SELECT id FROM runs WHERE task=?", (t["id"],))) == 1


def test_an_on_pass_cmd_that_sets_ttp_result_itself_is_not_dry_run_and_is_handed_back(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    target = tmp_path / "hand-off.json"
    t = _hand_off_waiting(env, p, d, monkeypatch, f"TTP_RESULT={target} bash -c 'echo {{}} > $TTP_RESULT'")
    res = load_result(t["result"])
    assert res[onpassdry.KEY]["no_result"] and "sets TTP_RESULT itself" in res["woke"]
    assert not target.exists(), "the dry run must not write where the command points TTP_RESULT"
    got = json.loads((pathlib.Path(_last_run(p, t["id"])["dir"]) / onpassdry.FILE).read_text())
    assert got["verdict"] == onpassdry.NO_RESULT and onpassdry.said(got) == "not run"
    # Bypasses: export, env, a quoted path; passing the daemon's own path on is fine.
    for cmd in ("export TTP_RESULT=/x; run", "env TTP_RESULT=/x run", 'TTP_RESULT="/x" run', "a; TTP_RESULT=$HOME/r run"):
        assert onpassdry.sets_result(cmd), cmd
    for cmd in ('TTP_RESULT="$TTP_RESULT" run', "TTP_RESULT=${TTP_RESULT} run", "echo > $TTP_RESULT",
                "MY_TTP_RESULT=/x run", 'printf x > "$TTP_RESULT"'):
        assert not onpassdry.sets_result(cmd), cmd


def test_an_exit_0_dry_run_is_judged_on_what_it_wrote_and_on_a_deliberate_early_exit(env, tmp_path):
    from ttp import onpassdry
    from ttp.daemon import WAIT_KEYS, _checks_result_wait
    env_ = dict(os.environ)

    def why(cmd, fallback=False):
        return onpassdry.no_result(onpassdry.run(cmd, tmp_path, env_, 10), cmd, tmp_path, fallback, WAIT_KEYS)
    assert "without writing a hand-off" in why("true")
    assert why('printf \'{"status":"done","summary":"s"}\' > "$TTP_RESULT"') == ""
    # A final hand-off that carries a wait is refused once the probe passes: so is its dry run.
    assert "carries retry_after_s" in why('echo \'{"status":"done","summary":"s","retry_after_s":60}\' > "$TTP_RESULT"')
    assert "no JSON object" in why('echo "[1]" > "$TTP_RESULT"') and "no JSON object" in why('echo x > "$TTP_RESULT"')
    # A failed dry run stays inconclusive, as before; a `ttp checks --result` wait falls back to its on_pass.
    assert why("exit 1") == "" and why("true", fallback=True) == ""
    assert _checks_result_wait("ttp checks --result /r/1") and not _checks_result_wait("ttp checks --result /r/1 && true")
    # A script that exits early on the dry run's variable on purpose, by absolute or relative path.
    early = tmp_path / "on_pass.py"
    early.write_text("import os, sys\nif os.environ.get('TTP_ON_PASS_DRY_RUN'):\n    sys.exit(0)\n")
    plain = tmp_path / "plain.sh"
    plain.write_text("echo nothing\n")
    assert why(f"python3 {early}") == "" and why("python3 on_pass.py") == ""
    assert "without writing" in why(f"bash {plain}") and "without writing" in why(f"true --script={plain}")
    assert "without writing" in why("true " + "x" * 5000)   # a word too long for a path is no script


def test_an_empty_on_pass_cmd_names_none_so_a_passed_check_records_on_pass(env, monkeypatch):
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    t = _hand_off_waiting(env, p, d, monkeypatch, "  ")
    res = load_result(t["result"])
    assert "on_pass_cmd" not in res and res.get("waiting_since") and onpassdry.KEY not in res
    assert res["on_pass"] == {"status": "done", "summary": "job passed"}
